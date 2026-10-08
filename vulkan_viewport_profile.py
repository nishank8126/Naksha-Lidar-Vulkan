"""[VULKAN VIEWPORT PERFORMANCE] measurement harness. Observation only.

Drives the real app: load dataset -> Surface mode -> fit / zoom in / pan /
zoom out, sampling the engine's existing per-frame counters at each step.
No rendering code is modified.
"""
import os
import sys
import time

os.environ.setdefault("NAKSHA_RENDER_BACKEND", "vulkan")
os.environ.setdefault("NAKSHA_VULKAN_DISABLE_ALL_VTK_RENDER", "1")
os.environ.setdefault("NAKSHA_TEST_LAS", "test_classified_highprecision.laz")

import numpy as np
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

import laspy

STEP_FRAMES = 24


def _pump(app, seconds):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.processEvents()
        time.sleep(0.005)


def _sample(app, rb, label, seconds=1.2):
    """Let frames flow, then take the median of several report samples."""
    _pump(app, seconds)
    rows = []
    for _ in range(7):
        app.processEvents()
        try:
            t = rb.vulkan_backend.get_frame_timing()
            if t and t.get("valid"):
                rows.append(float(t["gpuRenderMs"]))
        except Exception:
            pass
        time.sleep(0.05)
    gpu = sorted(rows)[len(rows) // 2] if rows else None
    print(f"\n{'=' * 68}\n### {label}\n{'=' * 68}")
    if gpu is not None:
        print(f"  (median GPU frame over {len(rows)} samples: {gpu:,.2f} ms"
              f"  -> {1000.0 / gpu:,.1f} fps)")
    print(rb.viewport_performance_report(), flush=True)
    print(rb.interaction_performance_report(label), flush=True)
    print(rb.classify_bottleneck(), flush=True)
    return gpu


def _validate_paths(raw):
    """[PROFILE DATASET] - normalize and validate before loading.

    A bare str is ONE file, never a sequence. Single-character entries are
    rejected unless they are real files, which is the signature of the
    char-splitting bug this guard exists to catch.
    """
    if isinstance(raw, (str, os.PathLike)):
        files = [str(raw)]
    else:
        files = [str(p) for p in raw]

    print("[PROFILE DATASET]", flush=True)
    print(f"  Files: {len(files)}")
    bad = []
    for i, f in enumerate(files, 1):
        ap = os.path.abspath(f)
        exists = os.path.isfile(f)
        ext = os.path.splitext(f)[1].lower()
        if ext not in (".las", ".laz"):
            bad.append((f, f"invalid extension {ext or '(none)'}"))
        if not exists:
            bad.append((f, "does not exist"))
        print(f"  {i}. full path:  {ap}")
        print(f"     exists:      {'YES' if exists else 'NO'}")
        print(f"     extension:   {'LAS' if ext == '.las' else ('LAZ' if ext == '.laz' else ext or 'NONE')}")

    if not files:
        print("  REJECT: no dataset path supplied")
        return []
    if bad:
        for f, why in bad:
            print(f"  REJECT: {os.path.basename(f)!r} - {why}")
        return []
    # Catch the char-split signature explicitly: one-char entries that are not files.
    chars = [f for f in files if len(os.path.basename(f)) == 1 and not os.path.isfile(f)]
    if chars:
        print(f"  REJECT: {len(chars)} single-character path(s) - "
              f"a string was iterated as a sequence")
        return []
    return files


def main():
    raw = os.environ.get("NAKSHA_TEST_LAS", "test_classified_highprecision.laz")
    files = _validate_paths(raw)
    if not files:
        return 1
    path = files[0]

    app = QApplication.instance() or QApplication([])

    from gui.app_window import NakshaApp
    win = NakshaApp()
    win.show()
    win.resize(1280, 800)
    _pump(app, 1.0)

    print(f"loading {path} ...", flush=True)
    t0 = time.perf_counter()
    win.open_file(path)
    _pump(app, 2.0)
    while (getattr(win, "_data_state", "") == "LOADING") and time.perf_counter() - t0 < 900:
        app.processEvents()
        time.sleep(0.2)
    print(f"data ready in {time.perf_counter() - t0:,.1f}s  "
          f"state={getattr(win, '_data_state', '?')}", flush=True)

    rb = getattr(win, "render_backend", None)
    if rb is None or not getattr(rb, "active", False):
        print("Vulkan backend NOT active - cannot profile")
        return 1
    print(f"device={rb.vulkan_backend.get_device_name()}")

    _sample(app, rb, "1. POINT CLOUD (no surface)")

    print("\n>>> enabling Surface ...", flush=True)
    t_s = time.perf_counter()
    try:
        win.set_display_mode("surface")
    except Exception as exc:
        print(f"set_display_mode failed: {exc!r}")
    # Wait for the final surface to land.
    while time.perf_counter() - t_s < 600:
        app.processEvents()
        time.sleep(0.2)
        try:
            if rb.vulkan_backend.get_surface_upload_count() > 0:
                break
        except Exception:
            break
    print(f"surface ready in {time.perf_counter() - t_s:,.1f}s "
          f"uploads={rb.vulkan_backend.get_surface_upload_count()}", flush=True)
    # Settle the anomaly: record the quality mode actually used and the
    # vertex:triangle ratio. A clean triangulation has ~0.5x as many
    # vertices as triangles; we measured 3x, which is not clean geometry.
    try:
        _q = getattr(win, "surface_quality", "?")
        _sv = rb.vulkan_backend.get_surface_vertex_count()
        _si = rb.vulkan_backend.get_surface_index_count()
        _tri = _si // 3
        print(f"[SURFACE DIAG] quality={_q} "
              f"vertices={_sv:,} indices={_si:,} triangles={_tri:,} "
              f"verts_per_tri={(_sv / max(_tri, 1)):.2f}", flush=True)
    except Exception as exc:
        print(f"[SURFACE DIAG] unavailable: {exc!r}", flush=True)

    _sample(app, rb, "2. SURFACE ENABLED - fit view")

    print("\n>>> enabling Shaded Class ...", flush=True)
    try:
        win.set_display_mode("shaded_class")
    except Exception as exc:
        print(f"set_display_mode(shaded_class) failed: {exc!r}")
    _pump(app, 30.0)
    print("shaded settle complete", flush=True)

    rw = win
    for label, fn in (
        ("3. ZOOM IN", lambda: _zoom(rw, 0.25)),
        ("4. PAN", lambda: _pan(rw, 0.25)),
        ("5. ZOOM OUT", lambda: _zoom(rw, 4.0)),
    ):
        fn()
        _sample(app, rb, label)

    print("\nDONE", flush=True)
    return 0


def _zoom(win, factor):
    try:
        ren = win.vtk_widget.renderer
        cam = ren.GetActiveCamera()
        cam.Zoom(factor)
        ren.ResetCameraClippingRange()
        win.vtk_widget.render()
    except Exception as exc:
        print(f"zoom failed: {exc!r}")


def _pan(win, frac):
    try:
        ren = win.vtk_widget.renderer
        cam = ren.GetActiveCamera()
        fp = cam.GetFocalPoint()
        pos = cam.GetPosition()
        dx = (pos[0] - fp[0]) * frac
        dy = (pos[1] - fp[1]) * frac
        cam.SetFocalPoint(fp[0] + dx, fp[1] + dy, fp[2])
        cam.SetPosition(pos[0] + dx, pos[1] + dy, pos[2])
        ren.ResetCameraClippingRange()
        win.vtk_widget.render()
    except Exception as exc:
        print(f"pan failed: {exc!r}")


if __name__ == "__main__":
    raise SystemExit(main())
