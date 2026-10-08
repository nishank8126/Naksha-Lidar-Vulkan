"""stage2_app_run.py - NAKSHA STAGE 2 real-application streaming harness.

Launches the REAL application (PySide6 + Vulkan), drives a real cache-first open
through AppWindow.open_file(), then records the view ladder, a 30-second pan and
the position-reupload test into a JSONL telemetry file.

This is NOT a headless stub: it constructs the real AppWindow, calls the REAL
loader on the REAL source, and lets the StreamManager attach to the real VTK
camera. GPU timing comes from the engine's own frame-timing accessors, never
from the UI timer.

Usage:
    venv\\Scripts\\python.exe stage2_app_run.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "test_classified_highprecision.laz")
TE_PATH = os.path.join(HERE, "stage2_viewport_telemetry.jsonl")


def _print_instructions():
    print("\n" + "=" * 74)
    print("NAKSHA STAGE 2 - REAL APP STREAMING ACCEPTANCE")
    print("=" * 74)
    print(f"Source   : {SRC}")
    print("Cache    : committed .nakshaidx + .nakshapc")
    print("Mode     : DatasetMode.STREAMING (cache-first open)")
    print(f"Telemetry: {TE_PATH}")
    print("=" * 74)
    print("""
The app opens the REAL source automatically, then drives:
  FIT -> x2 -> x4 -> x8 -> x16 -> x32, a 30 s pan, and the position-reupload
  test. Watch the console for [STAGE2 LADDER] / [POSITION REUPLOAD TEST].
Leave the window open to interact manually; close it when finished.

Then run:  venv\\Scripts\\python.exe analyze_stage2.py
""")


def main():
    _print_instructions()
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer

    try:
        from gui.app_window import AppWindow
    except Exception as e:
        print(f"Cannot import the real AppWindow here: {e!r}")
        print("Run this on the T400 workstation so PySide6 + Vulkan load.")
        return 2

    app = QApplication.instance() or QApplication(sys.argv)
    win = AppWindow()
    win.show()
    app.processEvents()

    def _do_open():
        try:
            t0 = time.perf_counter()
            # The REAL production loader. install_streaming() runs inside
            # open_file() when the committed cache validates.
            win.open_file([SRC], import_options={}, prompt_import=False)
            print(f"[stage2] open_file() returned in "
                  f"{(time.perf_counter() - t0) * 1000:.0f} ms")
            print(f"[stage2] DatasetMode = {getattr(win, 'dataset_mode', '?')}")
            _report_open(win)
            _start_ladder(win)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[stage2] open failed: {e!r}")

    QTimer.singleShot(600, _do_open)
    return int(app.exec())


def _report_open(win):
    """Print the acceptance counters read from the LIVE app object."""
    d = getattr(win, "data", None) or {}
    mgr = getattr(win, "naksha_stream", None)
    n_app = 0
    v = d.get("xyz")
    if v is not None:
        try:
            n_app = len(v)
        except Exception:
            pass
    print("\n[STAGE 2 OPEN RESULT]")
    print(f"  DatasetMode                : {getattr(win, 'dataset_mode', '?')}")
    print(f"  legacy_loader_called       : "
          f"{int(bool(mgr and mgr.legacy_loader_called))}")
    print(f"  app.data keys              : {sorted(d.keys())}")
    print(f"  app.data point count       : {n_app:,}  (must be 0)")
    print(f"  monolithic upload attempts : "
          f"{int(getattr(win, '_streaming_full_point_upload_attempts', 0))}")
    if mgr is not None:
        print(f"  total_points (metadata)    : {mgr.total_points:,}")
        print(f"  RAM bytes resident         : {mgr.ram_bytes:,}")
        print(f"  resident tiles             : {len(mgr.resident)}")
    print("[/STAGE 2 OPEN RESULT]\n")


def _apply_zoom(win, mgr, frac, gen):
    """Set the real VTK camera to a zoom fraction of the extent and push ONE
    camera_changed through the real stream manager. Returns the measured
    CAMERA HOT PATH time in ms."""
    try:
        cam = win.vtk_widget.GetRenderWindow().GetRenderers() \
            .GetFirstRenderer().GetActiveCamera()
        bmin, bmax = win.data_bounds
        cx = (bmin[0] + bmax[0]) * 0.5
        cy = (bmin[1] + bmax[1]) * 0.5
        half = max((bmax[0] - bmin[0]) * 0.5 * frac, 1.0)
        sz = win.vtk_widget.size()
        cam.SetParallelProjection(1)
        cam.SetFocalPoint(cx, cy, bmax[2])
        cam.SetPosition(cx, cy - half * 2.0, bmax[2])
        cam.SetViewUp(0.0, 1.0, 0.0)
        cam.SetParallelScale(half * float(sz.height()) / float(sz.width()))
        win.vtk_widget.GetRenderWindow().Render()
        from gui.naksha_cache.app_streaming import _viewport
        camera, viewport = _viewport(win)
        t0 = time.perf_counter()
        mgr.on_camera_changed(camera, viewport, gen)
        return (time.perf_counter() - t0) * 1000.0
    except Exception as e:
        print(f"    (zoom failed: {e!r})")
        return float("nan")
def _start_ladder(win):
    """Drive FIT/x2/x4/x8/x16/x32 through the REAL camera + stream manager."""
    from PySide6.QtCore import QTimer
    mgr = getattr(win, "naksha_stream", None)
    if mgr is None:
        print("[stage2] no stream manager; skipping ladder")
        return
    steps = [("FIT", 1.0), ("x2", 0.5), ("x4", 0.25), ("x8", 0.125),
             ("x16", 0.0625), ("x32", 0.03125)]
    state = {"i": 0, "gen": 1000}

    def step():
        if state["i"] >= len(steps):
            _start_pan(win, mgr)
            return
        label, frac = steps[state["i"]]
        state["i"] += 1
        state["gen"] += 1
        try:
            ms = _apply_zoom(win, mgr, frac, state["gen"])
            drawn = sum(v.count for v in mgr.resident.values())
            print(f"[STAGE2 LADDER] {label:<4} frac={frac:<8} "
                  f"camera_hot_ms={ms:6.2f}  "
                  f"background_ms={mgr.bg_ms_last:7.1f}  "
                  f"tiles={len(mgr.resident):3d}  "
                  f"drawn={drawn:,}  "
                  f"uploads={mgr.adapter.uploads}  "
                  f"pos_reuploads={mgr.position_reuploads}")
        except Exception as e:
            print(f"[STAGE2 LADDER] {label} failed: {e!r}")
        QTimer.singleShot(2500, step)

    QTimer.singleShot(1500, step)


def _start_pan(win, mgr):
    """30-second continuous pan, sampling the real camera path."""
    from PySide6.QtCore import QTimer
    print("\n[STAGE2 PAN] starting 30 s continuous pan")
    state = {"t0": time.perf_counter(), "n": 0, "gen": 5000}

    def tick():
        elapsed = time.perf_counter() - state["t0"]
        if elapsed >= 30.0:
            print(f"[STAGE2 PAN] complete: {state['n']} camera updates "
                  f"over {elapsed:.1f}s")
            _start_reupload_test(win, mgr)
            return
        state["n"] += 1
        state["gen"] += 1
        bmin, bmax = win.data_bounds
        cx = (bmin[0] + bmax[0]) * 0.5 + 40.0 * state["n"]
        cy = (bmin[1] + bmax[1]) * 0.5
        half = max((bmax[0] - bmin[0]) * 0.125, 1.0)
        try:
            cam = win.vtk_widget.GetRenderWindow().GetRenderers() \
                .GetFirstRenderer().GetActiveCamera()
            sz = win.vtk_widget.size()
            cam.SetFocalPoint(cx, cy, bmax[2])
            cam.SetPosition(cx, cy - half * 2.0, bmax[2])
            cam.SetParallelScale(half * float(sz.height()) / float(sz.width()))
            from gui.naksha_cache.app_streaming import _viewport
            camera, viewport = _viewport(win)
            mgr.on_camera_changed(camera, viewport, state["gen"])
        except Exception as e:
            print(f"    (pan tick failed: {e!r})")
            return
        QTimer.singleShot(60, tick)

    QTimer.singleShot(100, tick)


def _start_reupload_test(win, mgr):
    """POSITION REUPLOAD TEST across FIT/x8/x16/x32 (spec: must be 0)."""
    from PySide6.QtCore import QTimer
    print("\n[POSITION REUPLOAD TEST]")
    steps = [("FIT", 1.0), ("x8", 0.125), ("x16", 0.0625), ("x32", 0.03125)]
    state = {"i": 0, "gen": 9000}

    def step():
        if state["i"] >= len(steps):
            _finish(win)
            return
        label, frac = steps[state["i"]]
        state["i"] += 1
        state["gen"] += 1
        _apply_zoom(win, mgr, frac, state["gen"])
        ad = mgr.adapter
        print(f"  {label:<4} uploads={ad.uploads} "
              f"position_reuploads={mgr.position_reuploads} "
              f"full_attempts={mgr.streaming_full_point_upload_attempts}")
        QTimer.singleShot(2500, step)

    QTimer.singleShot(500, step)


def _finish(win):
    """Print the closing summary and flush telemetry WITHOUT truncating it."""
    mgr = getattr(win, "naksha_stream", None)
    print("\n[STAGE 2 CLOSE SUMMARY]")
    if mgr is not None:
        print(f"  position_reuploads        : {mgr.position_reuploads}")
        print(f"  full upload attempts      : "
              f"{mgr.streaming_full_point_upload_attempts}")
        print(f"  RAM bytes                 : {mgr.ram_bytes:,}")
        print(f"  GPU bytes                 : {mgr.gpu_bytes:,}")
        print(f"  evictions                 : {mgr.evictions}")
        print(f"  stale cancellations       : {mgr.cancelled_stale}")
        print(f"  ram hits / misses         : {mgr.ram_hits} / {mgr.ram_misses}")
        if mgr.timing["hot_ms"]:
            h = sorted(mgr.timing["hot_ms"])
            p95 = h[int(len(h) * 0.95) - 1] if len(h) >= 20 else h[-1]
            print(f"  camera hot path ms        : "
                  f"p50={h[len(h) // 2]:.2f} p95={p95:.2f} max={h[-1]:.2f}")
    # APPEND the closing event. StreamTelemetry.__init__ TRUNCATES the file, so
    # the close marker must reuse the manager's OWN telemetry instance; making a
    # new StreamTelemetry here would erase everything recorded during the run.
    try:
        if mgr is not None and getattr(mgr, "te", None) is not None:
            mgr.te.event("app_closed")
            print(f"  telemetry appended -> "
                  f"{getattr(win, '_stream_telemetry_path', '?')}")
    except Exception as e:
        print(f"  telemetry close marker failed: {e!r}")
    print("[/STAGE 2 CLOSE SUMMARY]")
    print("\nNow run: venv\\Scripts\\python.exe analyze_stage2.py")


if __name__ == "__main__":
    raise SystemExit(main())