"""Fail-closed real-GUI Vulkan gate before tile-artifact diagnosis.

Run with venv/Scripts/python.exe. No LOD, cache, or camera policy changes.
Native offscreen readback supplies the actual renderer/swapchain extent only
after visible Vulkan takeover; no artifact images are saved by this check.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parent
REPORT = ROOT / "diagnostics" / "swapchain_stability.json"


def pump(app, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(min(0.005, max(0, end - time.monotonic())))


def native_stats(rb):
    """Do not accept the backend wrapper's unavailable-counter zero fallback."""
    b = rb.vulkan_backend
    values = [ctypes.c_uint64() for _ in range(3)]
    if not b._handle or not b._dll.nkv_get_frame_stats(
            ctypes.c_uint64(b._handle), *(ctypes.byref(v) for v in values)):
        raise RuntimeError("native frame statistics unavailable")
    return tuple(v.value for v in values)


def snapshot(rb):
    w = rb.vulkan_widget
    rendered, skipped, recreates = native_stats(rb)
    actors = rb._vtk_lidar_actor_list()
    return {
        "lidar_backend": "VULKAN" if rb.vulkan_owns_lidar_viewport() else "VTK",
        "vulkan_visible": bool(rb.is_displayed),
        "vtk_lidar_actor_count": len(actors),
        "vtk_visible_lidar_actor_count": sum(bool(a.GetVisibility()) for _, a in actors),
        "viewport_extent": [w.width(), w.height()] if w else [0, 0],
        "swapchain_requested_extent": list(rb.vulkan_backend._last_extent),
        "rendered": rendered, "skipped": skipped,
        "swapchain_recreates": recreates,
        "gpu_point_count": rb.vulkan_backend.get_point_count(),
        "loading_overlay_visible": rb._loading_overlay_visible(),
        "data_state": rb._data_state_for_report(),
        "camera_center": list(rb.main_camera.center),
        "camera_scale": rb.main_camera.parallel_scale,
        "camera_generation": rb.main_camera.generation,
    }


def takeover_complete(s):
    return (s["lidar_backend"] == "VULKAN" and s["vulkan_visible"]
            and s["vtk_visible_lidar_actor_count"] == 0
            and not s["loading_overlay_visible"] and s["rendered"] > 0
            and s["gpu_point_count"] > 0
            and s["data_state"] in ("READY", "STREAMING_READY", "LOADED")
            and min(s["viewport_extent"]) > 0)


def authoritative_snapshot(rb):
    s = snapshot(rb)
    if not takeover_complete(s):
        raise RuntimeError("FINAL VISIBLE LIDAR BACKEND is not ready VULKAN")
    # Native CaptureOffscreenRGBA8 uses Renderer::extent_, the live swapchain
    # extent. The Python _last_extent is only a requested size, never proof.
    frame = rb.vulkan_backend.capture_frame()
    if frame is None:
        raise RuntimeError("native swapchain extent readback unavailable")
    s["swapchain_extent"] = [int(frame.shape[1]), int(frame.shape[0])]
    if s["swapchain_extent"] != s["swapchain_requested_extent"]:
        raise RuntimeError("actual swapchain extent differs from requested extent")
    return s


def print_state(s):
    print("FINAL VISIBLE LIDAR BACKEND = " + s["lidar_backend"], flush=True)
    for key in ("vulkan_visible", "vtk_lidar_actor_count",
                "vtk_visible_lidar_actor_count", "viewport_extent",
                "swapchain_extent", "swapchain_recreates", "data_state"):
        print(f"{key}: {s[key]}", flush=True)


def send_mouse(app, w, kind, position, button, buttons):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    event = QMouseEvent(kind, QPointF(position), QPointF(w.mapToGlobal(position.toPoint())),
                        button, buttons, Qt.KeyboardModifier.NoModifier)
    app.sendEvent(w, event)


def send_wheel(app, w, delta):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    p = w.rect().center()
    app.sendEvent(w, QWheelEvent(QPointF(p), QPointF(w.mapToGlobal(p)),
                                QPoint(), QPoint(0, delta),
                                Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                Qt.ScrollPhase.NoScrollPhase, False))


def validate_sample(before, current):
    if current["swapchain_recreates"] != before["swapchain_recreates"]:
        raise RuntimeError("STOP LOD tuning: swapchain recreated during camera-only movement; fix resize/embedding first")
    if not takeover_complete(current):
        raise RuntimeError("visible Vulkan takeover lost during camera movement")
    for key in ("viewport_extent", "swapchain_requested_extent"):
        if current[key] != before[key]:
            raise RuntimeError(f"STOP LOD tuning: {key} changed during camera-only movement; fix resize/embedding first")


def run_motion(app, rb, name, duration, pan=False, zoom=False):
    from PySide6.QtCore import QEvent, QPointF, Qt
    before = authoritative_snapshot(rb)
    w = rb.vulkan_widget
    p = QPointF(w.rect().center())
    no = Qt.MouseButton.NoButton
    middle = Qt.MouseButton.MiddleButton
    if pan:
        send_mouse(app, w, QEvent.Type.MouseButtonPress, p, middle, middle)
    start = time.monotonic()
    steps = 0
    pan_changed = zoom_changed = False
    try:
        while time.monotonic() - start < duration:
            if pan:
                # Small bounded zigzag keeps injected positions within viewport.
                p += QPointF(1 if (steps // 120) % 2 == 0 else -1, 0)
                send_mouse(app, w, QEvent.Type.MouseMove, p, no, middle)
            if zoom and steps % 3 == 0:
                send_wheel(app, w, 120 if (steps // 30) % 2 == 0 else -120)
            pump(app, 1 / 60)
            current = snapshot(rb)
            validate_sample(before, current)
            pan_changed |= current["camera_center"] != before["camera_center"]
            zoom_changed |= current["camera_scale"] != before["camera_scale"]
            steps += 1
    finally:
        if pan:
            send_mouse(app, w, QEvent.Type.MouseButtonRelease, p, middle, no)
    elapsed = time.monotonic() - start
    # Include coalesced camera/resize notifications after the final event.
    for _ in range(30):
        pump(app, 0.05)
        validate_sample(before, snapshot(rb))
    after = authoritative_snapshot(rb)
    validate_sample(before, after)
    if after["swapchain_extent"] != before["swapchain_extent"]:
        raise RuntimeError("actual swapchain extent changed during camera movement")
    if after["rendered"] <= before["rendered"]:
        raise RuntimeError("no native frames presented during movement")
    if after["camera_generation"] <= before["camera_generation"]:
        raise RuntimeError("injected events did not move the camera")
    if pan and not pan_changed:
        raise RuntimeError("pan did not change camera center")
    if zoom and not zoom_changed:
        raise RuntimeError("wheel events did not change camera scale")
    result = {"scenario": name, "elapsed_seconds": elapsed, "input_steps": steps,
              "swapchain_recreations": after["swapchain_recreates"] - before["swapchain_recreates"],
              "presented_frames": after["rendered"] - before["rendered"],
              "camera_generations": after["camera_generation"] - before["camera_generation"],
              "before": before, "after": after, "passed": True}
    print(json.dumps(result), flush=True)
    return result


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    os.environ["NAKSHA_RENDER_BACKEND"] = "vulkan"
    os.environ["NAKSHA_VULKAN_MAIN_VIEWPORT"] = "1"
    os.environ["NAKSHA_VULKAN_PREVIEW"] = ""
    from PySide6.QtWidgets import QApplication
    from gui.app_window import NakshaApp
    from gui.dialogs.load_pointcloud_dialog import DEFAULT_IMPORT_OPTIONS
    app = QApplication.instance() or QApplication(sys.argv[:1])
    report = {"passed": False, "scenarios": [], "artifact_captures_permitted": False}
    win = None
    try:
        dataset = ROOT / "test_classified_highprecision.laz"
        if not dataset.is_file():
            raise RuntimeError("required test_classified_highprecision.laz missing")
        # This check uses existing caches only; it must never start a builder.
        for suffix in (".nakshaidx", ".nakshapc"):
            if not Path(str(dataset) + suffix).is_file():
                raise RuntimeError("required existing cache missing: " + suffix)
        report["dataset"] = str(dataset)
        win = NakshaApp()
        win.resize(1400, 900)
        win.show()
        pump(app, 1)
        win.open_file(filenames=[str(dataset)], import_options=dict(DEFAULT_IMPORT_OPTIONS),
                      prompt_import=False)
        rb = win.render_backend
        deadline = time.monotonic() + 180
        stable_since = None
        last_key = None
        while time.monotonic() < deadline:
            pump(app, 0.1)
            s = snapshot(rb)
            key = (s["swapchain_recreates"], tuple(s["viewport_extent"]),
                   tuple(s["swapchain_requested_extent"]), s["camera_generation"])
            if takeover_complete(s) and getattr(win, "_file_loader_worker", None) is None:
                if stable_since is None or key != last_key:
                    stable_since = time.monotonic()
                if time.monotonic() - stable_since >= 2:
                    break
            else:
                stable_since = None
            last_key = key
        else:
            report["last_state"] = s
            raise RuntimeError("Vulkan takeover did not complete and settle within 180s")
        report["final_authoritative_state"] = authoritative_snapshot(rb)
        print_state(report["final_authoritative_state"])
        for name, duration, pan, zoom in (("slow_pan", 10, True, False),
                                          ("rapid_zoom", 3, False, True),
                                          ("pan_zoom", 5, True, True)):
            report["active_scenario"] = name
            report["scenarios"].append(run_motion(app, rb, name, duration, pan, zoom))
        report["post_motion_authoritative_state"] = authoritative_snapshot(rb)
        print_state(report["post_motion_authoritative_state"])
        report["passed"] = True
        report["artifact_captures_permitted"] = True
        print("PASS: all camera-only swapchain recreation deltas = 0", flush=True)
        return 0
    except Exception as exc:
        report["failure"] = str(exc)
        print("FAIL: " + str(exc), flush=True)
        traceback.print_exc()
        return 1
    finally:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
        if win is not None:
            win.close()
            app.processEvents()


if __name__ == "__main__":
    sys.exit(main())
