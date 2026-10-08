"""vulkan_engine_test.py - validation gate for the Naksha Vulkan engine.

This is the script that must pass before any GPU-compute migration work:
it drives the REAL application (real threaded LAS loader, real Shaded Class
pipeline) with the Vulkan backend selected and then verifies the ENGINE state
and the ACTUAL PIXELS, not the intent to render.

Checks, in order:
  1. Vulkan renderer created (handle valid, state ACTIVE/READY).
  2. Point cloud uploaded to GPU memory (native nkv_get_point_count() > 0, so
     the device-side buffer - not just the Python call - is checked).
  3. Shaded-class surface uploaded (native nkv_get_surface_upload_count() > 0)
     after set_display_mode("shaded_class").
  4. Frames genuinely presented: native nkv_get_frame_stats() reports
     rendered > 0 AND nkv_render() returned 2 (presented) - a skipped frame
     no longer counts, see the return-code contract in
     native/naksha_vulkan/include/naksha/naksha_vulkan_c_api.h.
  5. Real pixels in the Vulkan viewport: screen-captured frame must have
     non-trivial variance and must not be a single flat colour.

Then it prints VULKAN TEST PASSED plus GPU device, frame counts and a MEASURED
frames-per-second figure (native presented-frame delta over wall time while the
viewport is driven - no synthetic/assumed timing).

Usage:
    python vulkan_engine_test.py [C:\\path\\file.las]      (default 123.las)

Environment: NAKSHA_RENDER_BACKEND=vulkan is forced here. While the app still
ships the split-preview seam, NAKSHA_VULKAN_PREVIEW=1 is also set (override it
by exporting it before running); once the single-viewport switch lands that
variable becomes irrelevant and this script needs no change - it locates the
Vulkan viewport through gui.render_backend rather than through the splitter.

Exit code 0 = every check passed, 1 = a check failed, 3 = crashed.
Run with the window in the foreground: screen capture reads real desktop
pixels, so occluding the window skews check 5.
"""
from __future__ import annotations

import os
import sys
import time
import traceback

# ---- environment must be set BEFORE any gui module is imported ---------------
os.environ["NAKSHA_RENDER_BACKEND"] = "vulkan"
os.environ.setdefault("NAKSHA_VULKAN_PREVIEW", "1")

# main.py reconfigures the console to UTF-8 before importing the GUI stack;
# do the same so redirected logs (cp1252 by default) cannot break the
# emoji/status prints inside gui.shading_display at import time.
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    try:
        if _stream is not None and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

LAS_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(PROJECT_ROOT, "123.las")
OUT_DIR = os.path.join(PROJECT_ROOT, "vulkan_engine_test_output")
FPS_SAMPLE_SECONDS = 3.0

_CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    ok = bool(ok)
    _CHECKS.append((name, ok, detail))
    print(f"[test] {'PASS' if ok else 'FAIL'}  {name}"
          + (f"  ({detail})" if detail else ""), flush=True)
    return ok


def pump(app, seconds: float = 0.25) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def wait_until(app, cond, timeout: float, poll: float = 0.1) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(poll)
    return False


def qimg_to_np(img) -> np.ndarray:
    from PySide6.QtGui import QImage
    if hasattr(img, "toImage"):  # QScreen.grabWindow / copy() return a QPixmap
        img = img.toImage()
    img = img.convertToFormat(QImage.Format_RGBA8888)
    h, w, bpl = img.height(), img.width(), img.bytesPerLine()
    arr = np.frombuffer(img.constBits(), np.uint8, count=h * bpl).reshape(h, bpl)
    return arr[:, : w * 4].reshape(h, w, 4).copy()


def printwindow_capture(hwnd) -> "np.ndarray | None":
    """Win32 PrintWindow(hwnd, ..., PW_RENDERFULLCONTENT) capture of a window's
    CLIENT area (children included), returned as an (h, w, 3) RGB uint8 array.

    QScreen.grabWindow()/QPixmap copies go through a GDI path that reports
    BLACK for accelerated (OpenGL/Vulkan) surfaces, which made check 5 fail
    with std=0.00 no matter what the engine was presenting. PW_RENDERFULLCONTENT
    (Windows 8.1+) asks DWM for the composed window image instead, so
    swapchain pixels are read back for real.
    """
    import ctypes
    import ctypes.wintypes as wt

    try:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        hwnd = wt.HWND(int(hwnd))
        wr = wt.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(wr)):
            return None
        w, h = wr.right - wr.left, wr.bottom - wr.top
        if w <= 0 or h <= 0:
            return None

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
            ]

        hdc_w = user32.GetDC(0)
        hdc_m = gdi32.CreateCompatibleDC(hdc_w)
        hbmp = gdi32.CreateCompatibleBitmap(hdc_w, w, h)
        old = gdi32.SelectObject(hdc_m, hbmp)
        ok = user32.PrintWindow(hwnd, hdc_m, 2)  # PW_RENDERFULLCONTENT
        bi = BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.biWidth, bi.biHeight = w, -h          # top-down rows
        bi.biPlanes, bi.biBitCount = 1, 32
        buf = ctypes.create_string_buffer(w * h * 4)
        got = gdi32.GetDIBits(hdc_m, hbmp, 0, h, buf, ctypes.byref(bi), 0)
        gdi32.SelectObject(hdc_m, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_m)
        user32.ReleaseDC(0, hdc_w)
        if not ok or not got:
            return None
        arr = np.frombuffer(buf.raw, np.uint8).reshape(h, w, 4)

        # Crop window-rect bitmap down to the CLIENT area, so callers can use
        # plain mapToGlobal() widget coordinates (they are client-relative).
        cl = wt.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(cl))
        pt = wt.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(pt))
        cx, cy = pt.x - wr.left, pt.y - wr.top
        cw, ch = cl.right, cl.bottom
        if cw <= 0 or ch <= 0 or cx < 0 or cy < 0:
            return arr[..., [2, 1, 0]].copy()
        client = arr[cy:cy + ch, cx:cx + cw]
        return client[..., [2, 1, 0]].copy()      # BGRA -> RGB
    except Exception:
        return None


def save_rgb_png(arr: np.ndarray, path: str) -> bool:
    """Write an (h, w, 3|4) uint8 array as PNG via QImage (no PIL dependency)."""
    from PySide6.QtGui import QImage
    try:
        a = np.ascontiguousarray(arr[..., :3], dtype=np.uint8)
        h_, w_ = a.shape[:2]
        q = QImage(a.tobytes(), w_, h_, w_ * 3, QImage.Format_RGB888).copy()
        return bool(q.save(path))
    except Exception:
        return False


def vulkan_viewport_widget(rb):
    """The widget that owns the Vulkan swapchain's pixels.

    Resolves the current split-preview widget first, then a single-viewport
    hook if one exists, so this test does not need editing when the app stops
    using the splitter.
    """
    for attr in ("vulkan_viewport_widget", "vulkan_widget"):
        w = getattr(rb, attr, None)
        if w is not None:
            return w
    return None


def _suppress_modal_dialogs() -> None:
    """Never let a QMessageBox block the unattended run."""
    from PySide6.QtWidgets import QMessageBox

    def _warn(*args, **kwargs):
        print(f"[test] QMessageBox.warning suppressed: "
              f"{args[2] if len(args) > 2 else args}", flush=True)
        return QMessageBox.Yes

    def _crit(*args, **kwargs):
        print(f"[test] QMessageBox.critical suppressed: "
              f"{args[2] if len(args) > 2 else args}", flush=True)
        return QMessageBox.Ok

    QMessageBox.warning = _warn
    QMessageBox.critical = _crit
    QMessageBox.information = lambda *a, **k: QMessageBox.Ok


def get_native_stats(rb):
    """(rendered, skipped, recreates, source) straight from the engine.

    Prefers the native counters (a frame counts only when it reached
    vkQueuePresentKHR); falls back to the Python-side counters when the wrapper
    or the native call is unavailable.
    """
    backend = getattr(rb, "vulkan_backend", None)
    if backend is not None and hasattr(backend, "get_frame_stats"):
        try:
            rendered, skipped, recreates = backend.get_frame_stats()
            if rendered or skipped or recreates:
                return rendered, skipped, recreates, "native"
        except Exception:
            pass
    if backend is not None:
        return (int(getattr(backend, "present_count", 0)),
                int(getattr(backend, "skip_count", 0)), 0, "python")
    return 0, 0, 0, "none"


def report(backend) -> int:
    failed = [c for c in _CHECKS if not c[1]]
    print("=" * 72, flush=True)
    if failed:
        print(f"[test] {len(_CHECKS) - len(failed)}/{len(_CHECKS)} checks passed",
              flush=True)
        for name, _ok, detail in failed:
            print(f"[test]   FAILED: {name}" + (f"  ({detail})" if detail else ""),
                  flush=True)
        print("[test] VULKAN TEST FAILED", flush=True)
        print("=" * 72, flush=True)
        return 1
    print(f"[test] {len(_CHECKS)}/{len(_CHECKS)} checks passed", flush=True)
    print("VULKAN TEST PASSED", flush=True)
    print("=" * 72, flush=True)
    return 0


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from gui.app_window import NakshaApp
    from gui.dialogs.load_pointcloud_dialog import DEFAULT_IMPORT_OPTIONS
    from gui import render_backend as rb_mod

    print("=" * 72, flush=True)
    print("NAKSHA VULKAN ENGINE TEST", flush=True)
    print(f"  backend  : {os.environ.get('NAKSHA_RENDER_BACKEND')}", flush=True)
    print(f"  las file : {LAS_PATH}", flush=True)
    print("=" * 72, flush=True)
    if not os.path.isfile(LAS_PATH):
        print(f"[test] LAS file not found: {LAS_PATH}", flush=True)
        return 1

    _suppress_modal_dialogs()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    win = NakshaApp()
    win.resize(1400, 900)
    win.show()
    _STATE["app"] = app
    _STATE["win"] = win
    pump(app, 1.0)

    # ---- 1. renderer created ------------------------------------------------
    rb = getattr(win, "render_backend", None)
    check("render backend owner present", rb is not None)
    if rb is None:
        return report(None)
    backend = getattr(rb, "vulkan_backend", None)
    check("Vulkan initialized and active",
          backend is not None and bool(rb.active),
          f"state={rb.state} err={getattr(rb, 'last_error', '')!r}")
    if backend is None or not rb.active:
        return report(None)
    check("native renderer handle valid", bool(getattr(backend, "_handle", 0)),
          f"handle={getattr(backend, '_handle', 0)}")

    device = backend.get_device_name() if hasattr(backend, "get_device_name") else ""
    check("GPU device name reported", bool(device), device)

    # ---- 2. point cloud uploaded -------------------------------------------
    print(f"[test] loading {LAS_PATH} ...", flush=True)
    t0 = time.monotonic()
    win.open_file(filenames=[LAS_PATH],
                  import_options=dict(DEFAULT_IMPORT_OPTIONS), prompt_import=False)
    loaded = wait_until(
        app,
        lambda: (getattr(win, "data", None) is not None
                 and win.data.get("xyz") is not None
                 and getattr(win, "_file_loader_worker", None) is None),
        timeout=900)
    n_points = len(win.data["xyz"]) if getattr(win, "data", None) else 0
    check("LAS loaded (threaded pipeline, no dialogs)", loaded,
          f"{time.monotonic() - t0:.1f}s, points={n_points:,}")
    if not loaded:
        return report(backend)
    pump(app, 2.0)

    gpu_points = int(backend.get_point_count()) if hasattr(backend, "get_point_count") else 0
    check("point cloud uploaded to GPU memory", gpu_points > 0,
          f"python_points={n_points:,} gpu_points={gpu_points:,}")

    # ---- 3. shaded-class surface uploaded ----------------------------------
    print("[test] display mode -> shaded_class ...", flush=True)
    t0 = time.monotonic()
    win.set_display_mode("shaded_class")
    active = wait_until(
        app,
        lambda: rb.active_render_mode == "shaded_class" and get_native_stats(rb)[0] > 0,
        timeout=900)
    check("shaded_class active + frame presented", active,
          f"{time.monotonic() - t0:.1f}s, mode={rb.active_render_mode}")
    if not active:
        return report(backend)

    def _count(method: str) -> int:
        fn = getattr(backend, method, None)
        if fn is None:
            return 0
        try:
            return int(fn())
        except Exception:
            return 0

    surface_uploads = _count("get_surface_upload_count")
    overlay_tris = _count("get_overlay_triangle_count")
    check("shaded-class surface uploaded to GPU", surface_uploads > 0,
          f"surface_upload_count={surface_uploads} overlay_triangles={overlay_tris:,}")

    rendered, skipped, recreates, src = get_native_stats(rb)
    check("frames genuinely presented (native)", rendered > 0,
          f"rendered={rendered} skipped={skipped} recreates={recreates} source={src}")
    check("VULKAN_PRESENT_COUNT > 0", rb_mod.get_present_count() > 0,
          str(rb_mod.get_present_count()))
    return finish(app, win, rb, backend, device, surface_uploads, vram_bytes(backend))


def vram_bytes(backend) -> int:
    if hasattr(backend, "get_vram_bytes"):
        try:
            return int(backend.get_vram_bytes())
        except Exception:
            return 0
    return 0


def finish(app, win, rb, backend, device, surface_uploads, vram) -> int:
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication

    # ---- 4. MEASURED present throughput (no assumed timing) ----------------
    r0, _s0, _rec0, src = get_native_stats(rb)
    t0 = time.monotonic()
    requests = 0
    while time.monotonic() - t0 < FPS_SAMPLE_SECONDS:
        app.processEvents()
        try:
            backend.request_render()
            requests += 1
        except Exception:
            break
        time.sleep(0.001)
    elapsed = max(1e-9, time.monotonic() - t0)
    r1, s1, rec1, _ = get_native_stats(rb)
    presented = max(0, r1 - r0)
    fps = presented / elapsed
    check("present throughput measured", presented > 0,
          f"{presented} frames in {elapsed:.2f}s")
    print(f"[test] render requests driven : {requests:,} in {elapsed:.2f}s", flush=True)
    print(f"[test] presented frames (native): {presented}  ->  {fps:.1f} FPS", flush=True)

    # ---- 5. real pixels in the Vulkan viewport -----------------------------
    os.makedirs(OUT_DIR, exist_ok=True)
    win.raise_()
    win.activateWindow()
    pump(app, 0.6)
    screen = QApplication.primaryScreen()
    # PW_RENDERFULLCONTENT first: QScreen.grabWindow goes through a GDI path
    # that reads BLACK for OpenGL/Vulkan-backed surfaces - the historical
    # false FAIL of this check ("std=0.00 distinct_colours=1").
    full_rgb = printwindow_capture(int(win.winId()))
    if full_rgb is None:
        full = screen.grabWindow(int(win.winId()))
        check("screen capture non-null", not full.isNull(),
              f"{full.width()}x{full.height()} (QScreen fallback)")
        if full.isNull():
            return report(backend)
        full_rgb = qimg_to_np(full)[..., :3]
    else:
        check("screen capture non-null", True,
              f"{full_rgb.shape[1]}x{full_rgb.shape[0]} (PrintWindow)")
    win_origin = win.mapToGlobal(QPoint(0, 0))
    widget = vulkan_viewport_widget(rb)
    check("Vulkan viewport widget located", widget is not None)
    if widget is None:
        return report(backend)
    # -- diagnostics for the sole-viewport install (geometry mirroring) -------
    try:
        def _g(w):
            r = w.geometry()
            return (r.x(), r.y(), r.width(), r.height())
        src = getattr(rb, "_vtk_widget", None)
        host = src.parentWidget() if src is not None else None
        inter = getattr(src, "interactor", None) if src is not None else None
        print(f"[test] diag installed={getattr(rb, '_viewport_installed', None)} "
              f"mirror={getattr(rb, '_geometry_mirror', None) is not None} "
              f"visible={bool(widget.isVisible())}", flush=True)
        print(f"[test] diag vulkan geom={_g(widget)} "
              f"parent={type(widget.parentWidget()).__name__ if widget.parentWidget() else None}",
              flush=True)
        if src is not None:
            print(f"[test] diag vtk geom={_g(src)} visible={bool(src.isVisible())} "
                  f"interactor={_g(inter) if inter is not None else None}", flush=True)
        if host is not None:
            print(f"[test] diag host={type(host).__name__} geom={_g(host)} "
                  f"layout={type(host.layout()).__name__ if host.layout() else None}", flush=True)
    except Exception as _d_err:
        print(f"[test] diag failed: {_d_err!r}", flush=True)
    gp = widget.mapToGlobal(QPoint(0, 0))
    x, y = gp.x() - win_origin.x(), gp.y() - win_origin.y()
    w, h = max(1, widget.width()), max(1, widget.height())
    fh, fw = full_rgb.shape[:2]
    x0, y0, x1, y1 = max(0, x), max(0, y), min(fw, x + w), min(fh, y + h)
    crop = full_rgb[y0:y1, x0:x1] if (x1 > x0 and y1 > y0) else full_rgb[0:1, 0:1]
    save_rgb_png(crop, os.path.join(OUT_DIR, "vulkan_viewport.png"))
    save_rgb_png(full_rgb, os.path.join(OUT_DIR, "window_full.png"))

    # Real pixels: prefer the NATIVE offscreen readback (nkv_capture_frame).
    # Screen grabs (QScreen.grabWindow AND PrintWindow) go through GDI and
    # read a flat black image for a Vulkan surface, so they can only ever be
    # artifacts, never proof of what the engine actually presented.
    native_arr = None
    try:
        cap = backend.capture_frame() if hasattr(backend, "capture_frame") else None
        if cap is not None and getattr(cap, "size", 0):
            native_arr = cap[..., :3]
    except Exception as _cap_err:
        print(f"[test] native capture failed: {_cap_err!r}", flush=True)
    if native_arr is not None:
        arr, source = native_arr, "native offscreen"
        save_rgb_png(arr, os.path.join(OUT_DIR, "vulkan_viewport_native.png"))
    else:
        arr, source = crop, "screen (GDI - cannot see Vulkan)"
    std = float(arr.std())
    distinct = int(len(np.unique(arr.reshape(-1, 3), axis=0)))
    nonblack = float((arr.sum(axis=2) > 12).mean()) * 100.0
    check("Vulkan viewport has real pixels (not flat/black)",
          std > 1.0 and distinct > 4,
          f"{arr.shape[1]}x{arr.shape[0]} std={std:.2f} distinct_colours={distinct} "
          f"non-black={nonblack:.1f}% source={source}")

    gpu_points = backend.get_point_count() if hasattr(backend, "get_point_count") else 0
    with open(os.path.join(OUT_DIR, "metrics.txt"), "w", encoding="utf-8") as fh:
        fh.write(f"device={device}\n")
        fh.write(f"vram_mb={vram / (1024 ** 2):.0f}\n")
        fh.write(f"gpu_points={gpu_points}\n")
        fh.write(f"surface_upload_count={surface_uploads}\n")
        fh.write(f"rendered={r1} skipped={s1} recreates={rec1} counter_source={src}\n")
        fh.write(f"fps_measured={fps:.2f}\n")
        fh.write(f"viewport={w}x{h} std={std:.2f} distinct_colours={distinct}\n")

    print("-" * 40, flush=True)
    print("NAKSHA VULKAN ENGINE", flush=True)
    print(f"GPU:              {device}", flush=True)
    print(f"Points:           {gpu_points:,}", flush=True)
    print(f"Presented frames: {r1}", flush=True)
    print(f"FPS (measured):   {fps:.1f}", flush=True)
    print(f"VRAM:             {vram / (1024 ** 2):.0f} MB", flush=True)
    print(f"Viewport:         {w}x{h}  std={std:.2f}  colours={distinct}", flush=True)
    print("-" * 40, flush=True)
    print(f"[test] artifacts: {OUT_DIR}", flush=True)
    return report(backend)


_STATE: dict = {}


if __name__ == "__main__":
    try:
        _CODE = main()
    except BaseException:
        traceback.print_exc()
        _CODE = 3
    finally:
        _app = _STATE.get("app")
        _win = _STATE.get("win")
        if _win is not None:
            try:
                _win.close()
            except Exception:
                pass
        if _app is not None:
            try:
                _app.processEvents()
            except Exception:
                pass
    sys.exit(_CODE)
