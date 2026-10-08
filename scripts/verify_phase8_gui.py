"""Capture honest production Vulkan mode/navigation acceptance evidence.

This tests the real application, not a replacement viewport. It records
unimplemented or timed-out modes as failures, and never certifies VTK parity
without reference captures. --build-normals authorizes the normal MISS build.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "diagnostics/phase8_gui")
    parser.add_argument("--build-normals", action="store_true")
    parser.add_argument("--normal-timeout", type=float, default=180)
    parser.add_argument("--streaming", action="store_true")
    parser.add_argument("--surface-timeout", type=float, default=300)
    parser.add_argument("--surface-parity", type=Path)
    args = parser.parse_args()
    dataset = args.dataset.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    os.environ["NAKSHA_RENDER_BACKEND"] = "vulkan"
    os.environ["NAKSHA_VULKAN_MAIN_VIEWPORT"] = "1"
    os.environ["NAKSHA_VULKAN_PREVIEW"] = ""
    os.environ["NAKSHA_LATENCY_PROBE"] = "1"
    os.environ.pop("QT_QPA_PLATFORM", None)
    if args.streaming:
        os.environ["NAKSHA_RENDER_MODE"] = "STREAMING_LOD"
    from PySide6.QtWidgets import QApplication, QMessageBox
    from PySide6.QtCore import QSettings
    from gui.app_window import NakshaApp
    from vulkan_engine_test import _suppress_modal_dialogs
    from PIL import Image
    import numpy as np
    _suppress_modal_dialogs()
    QMessageBox.question = lambda *a, **k: QMessageBox.Yes
    QMessageBox.exec = lambda *a, **k: QMessageBox.Ok
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(output / "settings"))
    app = QApplication.instance() or QApplication([])
    window = NakshaApp()
    evidence = {"dataset": str(dataset), "pid": os.getpid(), "modes": [],
                "vtk_visual_parity": "UNVERIFIED", "complete_mission": False}
    def save():
        (output / "acceptance.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    def pump(seconds=.1):
        until = time.perf_counter() + seconds
        while time.perf_counter() < until:
            app.processEvents()
            time.sleep(.005)
    def wait(predicate, timeout):
        until = time.perf_counter() + timeout
        while time.perf_counter() < until:
            if predicate():
                return True
            pump(.05)
        return bool(predicate())
    try:
        window.resize(1400, 790 if args.surface_parity else 900)
        window.show()
        pump(1)
        rb = window.render_backend
        if not rb.active or not rb.vulkan_backend:
            raise RuntimeError("Vulkan main viewport did not initialize")
        window.open_file(filenames=[str(dataset)], prompt_import=False)
        if not wait(lambda: getattr(window, "naksha_stream", None) is not None
                    and window.naksha_stream._points_resident > 0, 180):
            raise RuntimeError("streaming dataset did not open")
        mgr = window.naksha_stream
        backend = rb.vulkan_backend
        pump(3)
        if args.surface_parity:
            rb.apply_main_fit(390499.99, 685499.99, rb.main_camera.center_z, 524.9947)
            pump(.5)
        evidence.update(backend=rb.active_backend,
                        vulkan_visible=rb.vulkan_widget.isVisible(),
                        render_mode=mgr.render_mode,
                        source_points=mgr.source_point_count,
                        visible_vtk_lidar_actors=sum(
                            bool(actor.GetVisibility())
                            for actor in rb._vtk_lidar_actor_list()))
        for mode in ("class", "shaded", "surface", "shaded", "surface", "class"):
            if mode == "shaded" and mgr.normal_source != "stored" and not args.build_normals:
                evidence["modes"].append({"mode": mode, "status": "MISS_NOT_BUILT"})
                continue
            uploads = mgr._xyz_upload_count()
            started = time.perf_counter()
            mgr.set_display_mode(mode)
            timeout = args.surface_timeout if mode == "surface" else args.normal_timeout if mode == "shaded" else 30 if mode == "class" else 10
            ready = wait(lambda: mgr.display_mode == mode and mgr._atomic_swap().pending is None, timeout)
            pump(.2)
            row = dict(mode=mode, activated=ready, active=mgr.display_mode,
                       elapsed_ms=(time.perf_counter()-started)*1000,
                       xyz_upload_delta=mgr._xyz_upload_count()-uploads,
                       readiness=mgr.mode_readiness(),
                       frame_tick_errors=mgr.frame_tick_error_count)
            if mode == "surface" and ready:
                row["draw_proof"] = backend.get_surface_draw_proof()
                row["draw_proof"]["active_mode"] = mgr.display_mode.upper()
                print("[SURFACE DRAW PROOF] " + json.dumps(row["draw_proof"]), flush=True)
            surface = getattr(mgr, "surface_service", None)
            if surface is not None:
                row.update(surface_status=surface.status, surface_error=surface.error,
                           surface_timestamps=dict(surface.timestamps),
                           triangulation_calls=surface.triangulation_calls, cache_hits=surface.cache_hits)
            pixels = backend.capture_frame()
            if pixels is not None:
                capture_path = output / f"{len(evidence['modes']):02}_{mode}.png"
                Image.fromarray(pixels).save(capture_path)
                from surface_visual_evidence import save_camera, capture_surface_reference
                save_camera(capture_path, window, mgr.display_mode)
                if args.surface_parity and mode == "surface" and ready and "surface_parity" not in evidence:
                    evidence["surface_parity"] = capture_surface_reference(window, mgr, pixels, args.surface_parity)
                rgb = pixels[:, :, :3]
                row.update(nonzero_pixels=int(np.count_nonzero(np.any(rgb, axis=2))),
                           mean_rgb=rgb.mean(axis=(0, 1)).tolist())
            evidence["modes"].append(row)
            save()
            print(f"[GUI ACCEPTANCE] {mode}: activated={ready} active={mgr.display_mode}", flush=True)
        mgr.set_display_mode("neutral")
        wait(lambda: mgr.display_mode == "neutral", 10)
        # Camera operations use the same owner as the UI and must not recreate
        # the swapchain. Capture timings for a deterministic pan/reversal trace.
        camera = rb.main_camera
        before = backend.get_frame_stats()
        xyz_before_navigation = mgr._xyz_upload_count()
        camera_ms = []
        callback_ms = []
        previous = 0
        for delta in (1, 2, 4, 8, 4, 2, 1, 0, -1, -2, -4, 0):
            tick = time.perf_counter()
            rb.apply_main_pan((delta-previous)*10, 0, camera.viewport_height)
            callback_ms.append((time.perf_counter()-tick)*1000)
            previous = delta
            pump(.05)
            camera_ms.append((time.perf_counter()-tick)*1000)
        after = backend.get_frame_stats()
        evidence["navigation"] = dict(swapchain_recreations=after[2]-before[2],
                                     xyz_upload_delta=mgr._xyz_upload_count()-xyz_before_navigation,
                                     camera_callback_p50_ms=float(np.percentile(callback_ms, 50)),
                                     camera_callback_p95_ms=float(np.percentile(callback_ms, 95)),
                                     event_loop_p50_ms=float(np.percentile(camera_ms, 50)),
                                     event_loop_p95_ms=float(np.percentile(camera_ms, 95)))
        evidence["frame_stats"] = list(after)
        service = getattr(mgr, "normal_service", None)
        evidence["normals"] = dict(
            source=mgr.normal_source, resident_points=mgr.normal_resident_points,
            uploads=mgr.normal_upload_count,
            cache_status=getattr(mgr.normal_report, "status", "UNKNOWN"),
            timestamps=service.timestamps.report() if service else {})
    except Exception as exc:
        evidence["error"] = repr(exc)
    finally:
        save()
        service = getattr(getattr(window, "naksha_stream", None), "normal_service", None)
        if service is not None:
            service.shutdown(timeout=5)
        window.close()
        pump(.1)
    print(json.dumps(evidence, indent=2))
    return 0 if all(row.get("activated") for row in evidence["modes"]) and not evidence.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
