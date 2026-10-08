"""[NAKSHA STABILITY] long-session stress harness. Drives the real app.

Load -> Surface -> Shaded -> pan -> zoom -> switch modes, repeating, sampling
resource usage throughout. Needs a VISIBLE window for real GPU frames.
"""
import os
import time

os.environ.setdefault("NAKSHA_RENDER_BACKEND", "vulkan")
os.environ.setdefault("NAKSHA_VULKAN_DISABLE_ALL_VTK_RENDER", "1")
os.environ.setdefault("NAKSHA_TEST_LAS", "test_classified_highprecision.laz")

from PySide6.QtWidgets import QApplication

DURATION_S = int(os.environ.get("STRESS_MINUTES", "30")) * 60
DATASET = os.environ.get("NAKSHA_TEST_LAS", "test_classified_highprecision.laz")


def _pump(app, seconds):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.processEvents()
        time.sleep(0.005)


def _cam(win, what):
    try:
        ren = win.vtk_widget.renderer
        cam = ren.GetActiveCamera()
        fp, pos = cam.GetFocalPoint(), cam.GetPosition()
        if what == "zoom_in":
            cam.Zoom(0.6)
        elif what == "zoom_out":
            cam.Zoom(1.7)
        elif what == "pan":
            dx = (pos[0] - fp[0]) * 0.12
            dy = (pos[1] - fp[1]) * 0.12
            cam.SetFocalPoint(fp[0] + dx, fp[1] + dy, fp[2])
            cam.SetPosition(pos[0] + dx, pos[1] + dy, pos[2])
        ren.ResetCameraClippingRange()
        win.vtk_widget.render()
    except Exception as exc:
        print(f"cam({what}) failed: {exc!r}")


def _uploads(rb):
    try:
        return int(rb.vulkan_backend.get_surface_upload_count())
    except Exception:
        return 0


def main():
    app = QApplication.instance() or QApplication([])
    from gui.app_window import NakshaApp
    win = NakshaApp()
    win.show()
    win.resize(1280, 800)
    _pump(app, 1.0)

    t_load = time.perf_counter()
    win.open_file(DATASET)
    _pump(app, 3.0)
    while getattr(win, "_data_state", "") == "LOADING" and time.perf_counter() - t_load < 900:
        app.processEvents()
        time.sleep(0.2)
    print(f"[STRESS] loaded in {time.perf_counter() - t_load:,.1f}s "
          f"state={getattr(win, '_data_state', '?')}", flush=True)
    try:
        win.print_load_ui_state(DATASET, len(win.data.get("xyz") or []))
    except Exception:
        pass

    rb = getattr(win, "render_backend", None)
    if rb is None or not getattr(rb, "active", False):
        print("[STRESS] Vulkan not active")
        return 1

    peak_ram = peak_vram = 0.0
    uploads0 = _uploads(rb)
    errors, black_frames, popup_returns, cycles = 0, 0, 0, 0
    t0 = time.perf_counter()

    while time.perf_counter() - t0 < DURATION_S:
        cycles += 1
        for mode in ("surface", "shaded_class", "pointcloud"):
            try:
                win.set_display_mode(mode)
            except Exception as exc:
                print(f"[STRESS] set_display_mode({mode}) failed: {exc!r}")
                errors += 1
            _pump(app, 2.5)
            for act in ("zoom_in", "pan", "zoom_out"):
                _cam(win, act)
                _pump(app, 0.6)
            dlg = getattr(win, "_load_progress", None)
            if dlg is not None and dlg.isVisible():
                popup_returns += 1
                print(f"[STRESS] POPUP RETURNED cycle={cycles} mode={mode}")
            try:
                if int(rb.vulkan_backend.get_frame_stats()[0]) < 1:
                    black_frames += 1
            except Exception:
                pass
            try:
                peak_vram = max(peak_vram,
                                float(rb.vulkan_backend.get_vram_bytes()) / 1048576.0)
            except Exception:
                pass
            try:
                import psutil
                peak_ram = max(peak_ram, psutil.virtual_memory().used / 1048576.0)
            except Exception:
                pass
        print(f"[STRESS] cycle {cycles} @ "
              f"{(time.perf_counter() - t0) / 60:,.1f} min  "
              f"ram={peak_ram:,.0f} MB  vram={peak_vram:,.0f} MB  "
              f"popup_returns={popup_returns}  black={black_frames}", flush=True)

    uptime = time.perf_counter() - t0
    print("\n[NAKSHA STABILITY]", flush=True)
    print(f"  Runtime:        {uptime / 60:,.1f} min ({cycles} cycles)")
    print(f"  Peak RAM:       {peak_ram:,.0f} MB")
    print(f"  Peak VRAM:      {peak_vram:,.0f} MB")
    print(f"  Uploads:        {_uploads(rb)} (delta {_uploads(rb) - uploads0})")
    print(f"  Popup returns:  {popup_returns}")
    print(f"  Black frames:   {black_frames}")
    print(f"  Errors:         {errors}")
    print("  Crashes:        0 (process alive)", flush=True)
    print(rb.resource_monitor_report(), flush=True)

    print("[THREAD SHUTDOWN] starting", flush=True)
    stopped, stubborn = win._join_background_workers("stress-end", timeout_ms=8000)
    print(f"[THREAD SHUTDOWN] stopped={stopped or 'none'} "
          f"stubborn={stubborn or 'none'}", flush=True)
    print("STRESS DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
