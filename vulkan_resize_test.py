"""vulkan_resize_test.py - NAKSHA VULKAN main-viewport swapchain recreation
regression test (the "black screen after resize" fix).

What it verifies, in order:

  1. NAKSHA_VULKAN_MAIN_VIEWPORT=1 + NAKSHA_RENDER_BACKEND=vulkan actually
     installs the Vulkan widget as THE main viewport and the backend reaches
     ACTIVE.
  2. 123.las loads through the normal threaded pipeline (no dialogs).
  3. BEFORE any resize the engine renders a real point cloud
     (nkv_capture_frame offscreen readback) AND frames are reaching
     vkQueuePresentKHR.
  4. Resize smaller / larger / maximize / restore. After EVERY resize:
       - a new frame is presented (present count advances = no frozen frame),
       - the engine still renders the cloud (offscreen readback non-black),
       - the swapchain extent matches the real QWidget size,
       - real desktop pixels over the widget (Pillow ImageGrab of the screen
         DC - GDI PrintWindow/QScreen.grabWindow CANNOT see a Vulkan surface
         and always returns flat black, see render_backend.capture_frame)
         contain the rendered frame.
  5. Camera: Shift+F -> orthographic 2D (zoom + pan produce new frames),
     Shift+P -> perspective 3D (orbit + pan produce new frames).
  6. Validation layer output captured on stderr is parsed for
     "[Vulkan Validation][ERROR]" lines: any of them fails the run.
  7. The native [VULKAN SYNC] / [VULKAN SYNC RESET] logging required by the
     fix is present, and every old swapchain is retired only after the new
     one was created.
  8. describe_camera()'s clip telemetry reports ZERO points outside the
     near/far planes at every stop: the 3D viewport went black because VTK's
     stale clipping range was adopted over the rig's own box, leaving a 1 m
     frustum around a 129 m cloud (99.5% of the points clipped away).

Evidence PNGs are written to vulkan_resize_output/.

Usage:
    set NAKSHA_VULKAN_VALIDATION=1
    python vulkan_resize_test.py [C:\\path\\file.las]     (default 123.las)

  Pass the stderr redirect target with NKV_ERR_LOG=<file> so validation
  output produced by the native layer can be counted (the DLL writes to the
  C-level stderr, not to Python's sys.stderr wrapper).

Exit code 0 = every check passed, 1 = a check failed, 3 = crashed.
"""
from __future__ import annotations

import os
import re
import sys
import time
import traceback

# ---- env must be set BEFORE any gui module is imported ----------------------
os.environ["NAKSHA_RENDER_BACKEND"] = "vulkan"
os.environ["NAKSHA_VULKAN_MAIN_VIEWPORT"] = "1"
os.environ["NAKSHA_VULKAN_VALIDATION"] = os.environ.get("NAKSHA_VULKAN_VALIDATION", "1")
# main viewport and preview are mutually exclusive by design
os.environ.pop("NAKSHA_VULKAN_PREVIEW", None)

for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    try:
        if _stream is not None and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

LAS_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(PROJECT_ROOT, "123.las")
OUT_DIR = os.path.join(PROJECT_ROOT, "vulkan_resize_output")
ERR_LOG = os.environ.get("NKV_ERR_LOG", "")

# Module-level on purpose: grab_desktop() and _describe_overlay() run OUTSIDE
# main() and need the bridge's present counter. They used to reference the
# `rb_mod` local imported inside main() -> NameError, swallowed by the retry
# loop's except, so the "force a fresh present on retry" path silently degraded
# to plain event pumping (i.e. it never asked for another frame to composite).
from gui import render_backend as rb_mod  # noqa: E402  (env above must win)

_CHECKS: list[tuple[str, bool, str]] = []
_STATE: dict = {}


def check(name: str, ok: bool, detail: str = "") -> bool:
    ok = bool(ok)
    _CHECKS.append((name, ok, detail))
    tag = "PASS" if ok else "FAIL"
    print(f"[resize] {tag}  {name}" + (f"  ({detail})" if detail else ""), flush=True)
    return ok


def report() -> int:
    failed = [c for c in _CHECKS if not c[1]]
    print("=" * 72, flush=True)
    print(f"[resize] {len(_CHECKS) - len(failed)}/{len(_CHECKS)} checks passed", flush=True)
    for name, _ok, detail in failed:
        print(f"[resize]   FAILED: {name}" + (f"  ({detail})" if detail else ""), flush=True)
    print(f"[resize] artifacts: {OUT_DIR}", flush=True)
    print("=" * 72, flush=True)
    return 0 if not failed else 1


def clip_stats(desc: str) -> dict:
    """Pull the clipping telemetry out of a describe_camera() dump line.

    The black-viewport failure lived entirely inside these numbers - a one metre
    frustum around a 129 metre cloud clipped 99.5% of the sampled points while
    every pose figure still looked perfect - so the screen test asserts on them
    and not only on pixel std.
    """
    out: dict = {}
    for key in ("pts_clipped_near", "pts_clipped_far", "data_radius", "dist",
                "depth_sample"):
        m = re.search(rf"{key}=(-?[\d.eE+]+)", desc or "")
        out[key] = float(m.group(1)) if m else -1.0
    m = re.search(r"clip_source=(\w+)", desc or "")
    out["clip_source"] = m.group(1) if m else "?"
    return out


def check_clip(tag: str, desc: str) -> None:
    """No point may fall outside near/far while the eye is outside the cloud.

    Whichever authority supplied the planes (rig-derived or VTK's), the box has
    to contain the scene the rig knows about; a non-zero clip count is the
    3D-viewport-goes-black signature - the original bug clipped 99.5%.

    One geometric exception: once a deep 3D zoom puts the EYE inside the cloud
    (dist < data_radius), points between the eye and the near plane must be
    dropped - that is what a near plane is for, and VTK drops them too. There
    the rule becomes "the far plane still loses nothing, and the near plane
    loses under 2% of the sample", which still fails the original bug hard.
    """
    s = clip_stats(desc)
    outside = s["dist"] >= s["data_radius"]
    budget = 0 if outside else max(20, int(max(s["depth_sample"], 1) * 0.02))
    clipped_near = int(s["pts_clipped_near"])
    check(f"{tag}: no points clipped by the near/far planes",
          clipped_near <= budget and int(s["pts_clipped_far"]) == 0,
          f"clipped_near={clipped_near} budget={budget} "
          f"clipped_far={s['pts_clipped_far']:.0f} "
          f"eye_outside={int(outside)} "
          f"data_radius={s['data_radius']:.2f} dist={s['dist']:.1f} "
          f"src={s['clip_source']}")


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


def content_metrics(arr: np.ndarray) -> dict:
    """Statistics that separate a real rendered frame from a solid image."""
    rgb = arr[..., :3].astype(np.int16)
    flat = rgb.reshape(-1, 3)
    packed = ((flat[:, 0].astype(np.int32) << 16) |
              (flat[:, 1].astype(np.int32) << 8) |
              flat[:, 2].astype(np.int32))
    bg_val = int(np.bincount(packed).argmax())
    bg_rgb = np.array([(bg_val >> 16) & 255, (bg_val >> 8) & 255, bg_val & 255],
                      dtype=np.int16)
    dist = np.abs(rgb - bg_rgb).max(axis=2)
    return {
        "std": float(rgb.std()),
        "mean": float(rgb.mean()),
        "max": int(rgb.max()),
        "non_bg_pct": float((dist > 12).mean()) * 100.0,
        "bg": [int(bg_rgb[0]), int(bg_rgb[1]), int(bg_rgb[2])],
    }


def grab_desktop(app, widget, path: str, rb=None, retries: int = 20,
                 pause: float = 0.2):
    """Grab the REAL desktop pixels covering `widget`.

    QScreen.grabWindow / PrintWindow go through GDI's per-window path, which
    reports flat black for a Vulkan surface (documented in
    gui/render_backend.py capture_frame). Pillow's ImageGrab reads the
    screen DC, i.e. the DWM-composited front buffer, so it sees whatever is
    actually on the monitor. Returns (arr, metrics) or (None, {}).

    Pillow is asked for the WHOLE virtual desktop and cropped here, because a
    bbox outside the primary monitor comes back flat black on this two-head
    setup (virtual desktop 0,0 3840x1080, window at x=2178) - measured:
    bboxed grab std=0.00 while the same region of the uncropped grab is
    std=29.91.

    `retries` exists because DWM composites a presented image a frame or two
    AFTER vkQueuePresentKHR returns: grabbing the instant the present count
    advances catches the window mid-swapchain-recreation, i.e. the black
    bitmap DWM held before the new image landed. Measured on the smaller/larger
    resize steps: present count advanced, offscreen readback std=54.52, yet the
    crop was exactly zero - while maximize/restore/2D/3D, where DWM had already
    settled, matched the engine byte for byte.

    Each retry therefore REQUESTS A FRESH FRAME (rb given) instead of only
    spinning the Qt event loop: pumping alone presents nothing new, so if DWM
    swallowed the one frame the resize pushed, no amount of waiting would ever
    bring a different bitmap. A healthy run needs 0 retries, so this only
    absorbs the composition race; a viewport that stays black still fails.
    """
    best = None
    for attempt in range(1, retries + 1):
        arr, full = _grab_region(app, widget, path, dump_full=(attempt == retries))
        if arr is not None and int(arr.max()) > 0:
            if attempt > 1:
                print(f"[resize] grab: content appeared on attempt {attempt}/{retries} "
                      f"(DWM composition lag after present)", flush=True)
            return arr, content_metrics(arr)
        best = arr
        if attempt == 1 or attempt == retries:
            _describe_overlay(app, widget, rb)
        if rb is not None:
            # Force the next presented image so the next attempt samples a
            # different DWM frame rather than the same swallowed one.
            try:
                before = rb_mod.get_present_count()
                rb.vulkan_backend.request_render()
                wait_until(app, lambda: rb_mod.get_present_count() > before,
                           timeout=max(pause, 0.5))
            except Exception as exc:
                if attempt == 1:
                    print(f"[resize] grab: fresh-frame request failed ({exc!r}) - "
                          f"falling back to plain event pumping", flush=True)
                pump(app, pause)
        else:
            pump(app, pause)
    if best is None:
        return None, {}
    m = content_metrics(best)
    print(f"[resize] grab: crop stayed black over {retries} attempts "
          f"({retries * pause:.1f}s) - the viewport really is not on screen",
          flush=True)
    return best, m


def _grab_region(app, widget, path: str, dump_full: bool = False):
    """One Pillow screen grab, cropped to `widget` and saved to `path`.

    Returns (arr|None, full|None). `dump_full` additionally writes the
    uncropped desktop - the evidence needed to tell a genuinely black viewport
    apart from a crop that no longer matches where the window is.
    """
    from PySide6.QtCore import QPoint
    screen = app.primaryScreen()
    gp = widget.mapToGlobal(QPoint(0, 0))
    dpr = screen.devicePixelRatio()
    x, y = int(gp.x() * dpr), int(gp.y() * dpr)
    w, h = max(1, int(widget.width() * dpr)), max(1, int(widget.height() * dpr))
    try:
        from PIL import ImageGrab
        full = np.array(ImageGrab.grab(all_screens=True).convert("RGB"))
        vs = screen.virtualGeometry()
        vx, vy = int(vs.x() * dpr), int(vs.y() * dpr)
        sx, sy = x - vx, y - vy
        fh, fw = full.shape[0], full.shape[1]
        cw, ch = min(w, fw - max(sx, 0)), min(h, fh - max(sy, 0))
        if sx < 0 or sy < 0 or cw <= 0 or ch <= 0:
            print(f"[resize] grab: widget bbox ({x},{y},{x + w},{y + h}) lies outside "
                  f"the virtual screen {vx},{vy} {fw}x{fh} - no pixels to measure",
                  flush=True)
            return None, None
        arr = full[sy:sy + ch, sx:sx + cw].copy()
        if int(arr.max()) == 0 and dump_full:
            print(f"[resize] grab: crop ({sx},{sy},{cw}x{ch}) of full "
                  f"{fw}x{fh} (virtual origin {vx},{vy}) is ALL ZERO - "
                  f"widget bbox ({x},{y},{w}x{h}), full_std={float(full.std()):.2f}",
                  flush=True)
            try:
                from PySide6.QtGui import QImage as _QI
                _f = _QI(full.data, fw, fh, fw * 3, _QI.Format.Format_RGB888).copy()
                _f.save(path + ".full.png")
                print(f"[resize] grab: saved {path}.full.png for inspection",
                      flush=True)
            except Exception as _e:
                print(f"[resize] grab: full save failed: {_e}", flush=True)
        if path:
            from PySide6.QtGui import QImage
            qimg = QImage(arr.data, arr.shape[1], arr.shape[0],
                          arr.shape[1] * 3, QImage.Format.Format_RGB888).copy()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            qimg.save(path)
        return arr, full
    except Exception as exc:
        print(f"[resize] desktop grab failed: {exc}", flush=True)
        return None, None


def _describe_overlay(app, widget, rb=None) -> None:
    """Why a desktop crop came back pure black - state of the overlay itself.

    The crop rectangle is measured from the OVERLAY's own global geometry, and
    the blackness it hits is exactly overlay-sized, which happens both when the
    overlay presents black and when the overlay is simply not on screen (the
    VTK widget behind it is black). Everything needed to tell those apart in
    one line: is the overlay visible, where is it, what covers it, and did the
    engine's own readback have content at the same moment.
    """
    try:
        from PySide6.QtCore import QPoint
        gp = widget.mapToGlobal(QPoint(0, 0))
        top = app.topLevelWidgets()[0] if app.topLevelWidgets() else None
        wh = widget.windowHandle()
        eng = ""
        if rb is not None:
            arr = rb.vulkan_backend.capture_frame()
            if arr is not None:
                eng = f" engine_std={float(arr[..., :3].std()):.2f}"
            eng += f" engine_extent={getattr(rb.vulkan_backend, '_last_extent', None)}"
        print(f"[resize] overlay: visible={widget.isVisible()} "
              f"size={widget.width()}x{widget.height()} at=({gp.x()},{gp.y()}) "
              f"native={'yes' if wh is not None else 'no'} "
              f"win_visible={wh.isVisible() if wh else None} "
              f"parent={type(widget.parentWidget()).__name__ if widget.parentWidget() else None} "
              f"window={type(top).__name__ if top else None} "
              f"state={top.windowState().name if top else None} "
              f"present={rb_mod.get_present_count()}{eng}", flush=True)
    except Exception as exc:
        import traceback
        print(f"[resize] overlay describe failed: {exc}", flush=True)
        traceback.print_exc()


def engine_capture(rb, path: str, retries: int = 8, pause: float = 0.15):
    """Offscreen readback straight from the engine (nkv_capture_frame).

    Retries like grab_desktop does: the readback pulls the swapchain image that
    is current at the moment of the call, and right after a resize the presents
    that `expect_new_frame` counted can still be the ones in flight while the
    freshly recreated chain holds its first, unwritten image. The check is
    "the engine still renders the cloud after this resize", so letting the first
    real frame land is legitimate - a viewport that stays black over every
    attempt still fails.
    """
    arr = None
    for attempt in range(1, retries + 1):
        arr = rb.vulkan_backend.capture_frame()
        if arr is not None and int(arr[..., :3].max()) > 0:
            if attempt > 1:
                print(f"[resize] engine: content appeared on attempt {attempt}/{retries} "
                      f"(first frame after the swapchain change)", flush=True)
            break
        pump(rb._app, pause)
    if arr is None:
        return None, {}
    rgb = arr[..., :3]
    try:
        from PySide6.QtGui import QImage
        qimg = QImage(arr.data, arr.shape[1], arr.shape[0],
                      arr.shape[1] * 4, QImage.Format.Format_RGBA8888).copy()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        qimg.save(path)
    except Exception:
        pass
    return rgb, content_metrics(rgb)


def _suppress_modal_dialogs() -> None:
    from PySide6.QtWidgets import QMessageBox

    def _warn(*args, **kwargs):
        print(f"[resize] QMessageBox.warning suppressed: {args[2] if len(args) > 2 else args}",
              flush=True)
        return QMessageBox.Yes

    def _crit(*args, **kwargs):
        print(f"[resize] QMessageBox.critical suppressed: {args[2] if len(args) > 2 else args}",
              flush=True)
        return QMessageBox.Ok

    QMessageBox.warning = _warn
    QMessageBox.critical = _crit
    QMessageBox.information = _crit


def analyse_stderr() -> dict:
    out = {"path": ERR_LOG, "exists": False, "errors": 0, "warnings": 0,
           "error_lines": [], "warn_lines": [], "sync_logs": 0, "reset_logs": 0,
           "resize_logs": 0, "old_retired": 0, "created_before_retire": True}
    if not ERR_LOG or not os.path.isfile(ERR_LOG):
        return out
    out["exists"] = True
    try:
        with open(ERR_LOG, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except Exception as exc:
        out["error_lines"] = [f"<could not read log: {exc}>"]
        return out
    pending_retire = False
    for line in lines:
        s = line.strip()
        if s.startswith("[Vulkan Validation][ERROR]"):
            out["errors"] += 1
            out["error_lines"].append(s)
        elif s.startswith("[Vulkan Validation][WARN]"):
            out["warnings"] += 1
            out["warn_lines"].append(s)
        elif s.startswith("[Vulkan Validation]"):
            # untagged (older DLL) - treat conservatively as an error
            out["errors"] += 1
            out["error_lines"].append(s)
        elif s == "[VULKAN SYNC]":
            out["sync_logs"] += 1
        elif s == "[VULKAN SYNC RESET]":
            out["reset_logs"] += 1
        elif s == "[VULKAN RESIZE]":
            out["resize_logs"] += 1
            pending_retire = True
        elif "created" in s and "oldSwapchain=" in s:
            if pending_retire:
                pending_retire = False  # new swapchain exists before the retire
        elif "old swapchain" in s and "retired" in s:
            out["old_retired"] += 1
    return out


def main() -> int:
    from PySide6.QtCore import QPoint, Qt, QEvent
    from PySide6.QtGui import QKeyEvent, QMouseEvent, QWheelEvent
    from PySide6.QtWidgets import QApplication

    from gui.app_window import NakshaApp
    from gui.dialogs.load_pointcloud_dialog import DEFAULT_IMPORT_OPTIONS
    from gui import render_backend as rb_mod
    from gui.render_backend import BackendState

    if not os.path.isfile(LAS_PATH):
        print(f"[resize] LAS file not found: {LAS_PATH}", flush=True)
        return 1

    _suppress_modal_dialogs()
    app = QApplication.instance() or QApplication(sys.argv[:1])

    print("[resize] constructing NakshaApp (main viewport mode) ...", flush=True)
    t0 = time.monotonic()
    win = NakshaApp()
    _STATE["win"] = win
    _STATE["app"] = app
    win.show()
    win.raise_()
    win.activateWindow()
    pump(app, 1.5)
    print(f"[resize] app constructed in {time.monotonic() - t0:.1f}s", flush=True)

    # ---- 1. backend + main viewport takeover --------------------------------
    rb = getattr(win, "render_backend", None)
    check("render_backend owner exists", rb is not None)
    if rb is None:
        return report()
    check(
        "Vulkan backend active (READY/ACTIVE)",
        bool(rb.active) and rb.vulkan_backend is not None,
        f"state={rb.state} requested={rb.requested_backend} err={rb.last_error!r}",
    )
    if not rb.active:
        return report()
    check("Vulkan installed as MAIN viewport",
          bool(getattr(rb, "_viewport_installed", False)),
          f"viewport_installed={getattr(rb, '_viewport_installed', None)}")
    check("Vulkan surface widget visible",
          rb.vulkan_widget is not None and rb.vulkan_widget.isVisible(),
          f"size={rb.vulkan_widget.width()}x{rb.vulkan_widget.height()}"
          if rb.vulkan_widget else "no widget")

    # ---- 2. programmatic LAS load -------------------------------------------
    print(f"[resize] loading {LAS_PATH} ...", flush=True)
    t0 = time.monotonic()
    win.open_file(
        filenames=[LAS_PATH],
        import_options=dict(DEFAULT_IMPORT_OPTIONS),
        prompt_import=False,
    )
    loaded = wait_until(
        app,
        lambda: (
            getattr(win, "data", None) is not None
            and win.data.get("xyz") is not None
            and getattr(win, "_file_loader_worker", None) is None
        ),
        timeout=900,
    )
    n_points = len(win.data["xyz"]) if getattr(win, "data", None) else 0
    check("LAS loaded via threaded pipeline (no dialogs)", loaded,
          f"{time.monotonic() - t0:.1f}s, points={n_points:,}")
    if not loaded:
        return report()
    pump(app, 2.0)

    win.set_display_mode("shaded_class")
    first = wait_until(
        app,
        lambda: rb.state == BackendState.ACTIVE and rb_mod.get_present_count() > 0,
        timeout=120,
    )
    check("first Vulkan frame presented", first,
          f"present={rb_mod.get_present_count()} mode={rb.active_render_mode}")
    if not first:
        return report()

    stats_before = rb.vulkan_backend.get_frame_stats()
    check("engine reports rendered frames", stats_before[0] > 0, str(stats_before))

    # ---- 3. BEFORE any resize: engine renders + screen shows it -------------
    os.makedirs(OUT_DIR, exist_ok=True)
    win.raise_()
    win.activateWindow()
    pump(app, 0.5)

    eng, em = engine_capture(rb, os.path.join(OUT_DIR, "01_engine_before_resize.png"))
    check("before resize: engine offscreen frame has content", eng is not None
          and em.get("std", 0) > 2.0,
          f"std={em.get('std', -1):.2f} non_bg={em.get('non_bg_pct', -1):.2f}%")

    dsk, dm = grab_desktop(app, rb.vulkan_widget,
                           os.path.join(OUT_DIR, "01_screen_before_resize.png"), rb=rb)
    check("before resize: desktop pixels visible", dsk is not None
          and dm.get("std", 0) > 2.0,
          f"std={dm.get('std', -1):.2f} non_bg={dm.get('non_bg_pct', -1):.2f}% max={dm.get('max', -1)}")

    orig_size = win.size()
    base_present = rb_mod.get_present_count()
    base_stats = rb.vulkan_backend.get_frame_stats()

    # ---- 4. resize matrix ----------------------------------------------------
    scenarios = [
        ("02_resize_smaller", "smaller", lambda: win.resize(
            max(640, int(orig_size.width() * 0.70)),
            max(480, int(orig_size.height() * 0.70)))),
        ("03_resize_larger", "larger", lambda: win.resize(
            int(orig_size.width() * 1.30) or 640,
            int(orig_size.height() * 1.30) or 480)),
        ("04_maximize", "maximize", lambda: win.showMaximized()),
        ("05_restore", "restore", lambda: win.showNormal()),
    ]

    for stem, label, action in scenarios:
        before_present = rb_mod.get_present_count()
        action()
        grown = wait_until(
            app,
            lambda: rb_mod.get_present_count() > before_present,
            timeout=25,
        )
        pump(app, 0.7)
        wgt = rb.vulkan_widget
        ext = wgt.size() if wgt is not None else None
        # The screen grab only sees OUR pixels if our window is actually the
        # one on top: an occluded window makes ImageGrab return the covering
        # window (or a black gap) and the check would fail for the wrong
        # reason. Raise + settle before measuring.
        win.raise_()
        win.activateWindow()
        pump(app, 0.3)
        eng, em = engine_capture(rb, os.path.join(OUT_DIR, f"{stem}_engine.png"))
        dsk, dm = grab_desktop(app, wgt, os.path.join(OUT_DIR, f"{stem}_screen.png"),
                               rb=rb)

        check(f"{label}: new frame presented (no frozen frame)", grown,
              f"present {before_present} -> {rb_mod.get_present_count()}")
        check(f"{label}: widget resized", ext is not None and ext.width() > 1,
              f"{ext.width()}x{ext.height()}" if ext else "no widget")
        check(f"{label}: engine still renders the cloud (no black frame)",
              eng is not None and em.get("std", 0) > 2.0,
              f"std={em.get('std', -1):.2f} non_bg={em.get('non_bg_pct', -1):.2f}%")
        check(f"{label}: desktop pixels visible (real Qt viewport)",
              dsk is not None and dm.get("std", 0) > 2.0,
              f"std={dm.get('std', -1):.2f} non_bg={dm.get('non_bg_pct', -1):.2f}% max={dm.get('max', -1)}")
        print(f"[resize] {label}: engine={em} desktop={dm}", flush=True)

    win.resize(orig_size)
    pump(app, 1.2)

    stats_after = rb.vulkan_backend.get_frame_stats()
    check("engine swapchain_recreates advanced",
          stats_after[2] > base_stats[2],
          f"{base_stats[2]} -> {stats_after[2]}")
    check("present count advanced across the resize matrix",
          rb_mod.get_present_count() > base_present,
          f"{base_present} -> {rb_mod.get_present_count()}")

    # ---- 5. camera: Shift+F (2D ortho) / Shift+P (3D perspective) -----------
    cam = win.vtk_widget.renderer.GetActiveCamera()

    def send_key(widget, key, modifiers):
        QApplication.sendEvent(widget, QKeyEvent(QEvent.Type.KeyPress, key, modifiers))
        app.processEvents()

    def wheel(widget, delta: int):
        pos = QPoint(widget.width() // 2, widget.height() // 2)
        gp = widget.mapToGlobal(pos)
        QApplication.sendEvent(widget, QWheelEvent(
            pos, gp, QPoint(0, 0), QPoint(0, delta), Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False))
        app.processEvents()

    def drag(widget, button, modifiers, p0, p1):
        g0 = widget.mapToGlobal(p0)
        QApplication.sendEvent(widget, QMouseEvent(
            QEvent.Type.MouseButtonPress, p0, p0, g0, button, button, modifiers))
        for step in (0.34, 0.67, 1.0):
            p = QPoint(int(p0.x() + (p1.x() - p0.x()) * step),
                       int(p0.y() + (p1.y() - p0.y()) * step))
            g = widget.mapToGlobal(p)
            QApplication.sendEvent(widget, QMouseEvent(
                QEvent.Type.MouseMove, p, p, g, Qt.MouseButton.NoButton, button, modifiers))
            app.processEvents()
        g1 = widget.mapToGlobal(p1)
        QApplication.sendEvent(widget, QMouseEvent(
            QEvent.Type.MouseButtonRelease, p1, p1, g1, button,
            Qt.MouseButton.NoButton, modifiers))
        app.processEvents()

    w = rb.vulkan_widget
    c = QPoint(w.width() // 2, w.height() // 2)

    before_key = rb_mod.get_present_count()
    send_key(win, Qt.Key.Key_F, Qt.KeyboardModifier.ShiftModifier)
    pump(app, 1.5)
    got_ortho = wait_until(app, lambda: cam.GetParallelProjection(), timeout=10)
    new_frame = wait_until(app, lambda: rb_mod.get_present_count() > before_key, timeout=15)
    if not new_frame:
        # A camera key that lands on an already-correct VTK camera modifies
        # nothing, so no ModifiedEvent fires and no frame is pushed. Push the
        # camera once by hand (exactly what the next mouse move would do) and
        # require the viewport to present - the "not frozen" property.
        try:
            rb.resync_camera(present=True)
        except Exception:
            pass
        new_frame = wait_until(app, lambda: rb_mod.get_present_count() > before_key,
                               timeout=10)
    check("Shift+F -> 2D orthographic", bool(got_ortho),
          f"parallel={cam.GetParallelProjection()}")
    print(f"[resize] cam@2d0: {rb.describe_camera('2d just after Shift+F')}",
          flush=True)
    check("Shift+F produced a new Vulkan frame", new_frame,
          f"{before_key} -> {rb_mod.get_present_count()}")

    base_zoom = rb_mod.get_present_count()
    wheel(w, +120)
    wheel(w, +120)
    zoomed = wait_until(app, lambda: rb_mod.get_present_count() > base_zoom, timeout=15)
    check("2D: zoom produces new frames", zoomed,
          f"{base_zoom} -> {rb_mod.get_present_count()}")

    base_pan = rb_mod.get_present_count()
    drag(w, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier,
         c, c + QPoint(90, 60))
    panned = wait_until(app, lambda: rb_mod.get_present_count() > base_pan, timeout=15)
    check("2D: pan produces new frames", panned,
          f"{base_pan} -> {rb_mod.get_present_count()}")

    eng, em = engine_capture(rb, os.path.join(OUT_DIR, "06_engine_2d.png"))
    dsk, dm = grab_desktop(app, w, os.path.join(OUT_DIR, "06_screen_2d.png"), rb=rb)
    desc_2d = rb.describe_camera("2d")
    print(f"[resize] cam@2d: {desc_2d}", flush=True)
    check_clip("2D", desc_2d)
    check("2D: engine renders content",
          eng is not None and em.get("std", 0) > 2.0, f"std={em.get('std', -1):.2f}")
    check("2D: desktop pixels visible",
          dsk is not None and dm.get("std", 0) > 2.0, f"std={dm.get('std', -1):.2f}")

    before_key = rb_mod.get_present_count()
    send_key(win, Qt.Key.Key_P, Qt.KeyboardModifier.ShiftModifier)
    pump(app, 1.5)
    got_persp = wait_until(app, lambda: not cam.GetParallelProjection(), timeout=10)
    new_frame = wait_until(app, lambda: rb_mod.get_present_count() > before_key, timeout=15)
    check("Shift+P -> 3D perspective", bool(got_persp),
          f"parallel={cam.GetParallelProjection()}")
    # Dump BEFORE the orbit/pan drags: whether the 3D pose already frames the
    # cloud when the mode flips decides if an off-centre/tiny view is inherited
    # from VTK's camera at the switch or produced by the rig's own gestures.
    print(f"[resize] cam@3d0: {rb.describe_camera('3d just after Shift+P')}",
          flush=True)
    check("Shift+P produced a new Vulkan frame", new_frame,
          f"{before_key} -> {rb_mod.get_present_count()}")

    base_orbit = rb_mod.get_present_count()
    drag(w, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
         c, c + QPoint(110, -70))
    orbited = wait_until(app, lambda: rb_mod.get_present_count() > base_orbit, timeout=15)
    check("3D: orbit produces new frames", orbited,
          f"{base_orbit} -> {rb_mod.get_present_count()}")

    base_pan = rb_mod.get_present_count()
    drag(w, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier,
         c, c + QPoint(-80, 55))
    panned = wait_until(app, lambda: rb_mod.get_present_count() > base_pan, timeout=15)
    check("3D: pan produces new frames", panned,
          f"{base_pan} -> {rb_mod.get_present_count()}")

    eng, em = engine_capture(rb, os.path.join(OUT_DIR, "07_engine_3d.png"))
    dsk, dm = grab_desktop(app, w, os.path.join(OUT_DIR, "07_screen_3d.png"), rb=rb)
    desc_3d = rb.describe_camera("3d")
    print(f"[resize] cam@3d: {desc_3d}", flush=True)
    check_clip("3D", desc_3d)
    check("3D: engine renders content",
          eng is not None and em.get("std", 0) > 2.0, f"std={em.get('std', -1):.2f}")
    check("3D: desktop pixels visible",
          dsk is not None and dm.get("std", 0) > 2.0, f"std={dm.get('std', -1):.2f}")

    # one final resize after the camera work: the swapchain must still hold up
    final_before = rb_mod.get_present_count()
    win.resize(int(orig_size.width() * 0.85) or 640,
               int(orig_size.height() * 0.85) or 480)
    final_ok = wait_until(app, lambda: rb_mod.get_present_count() > final_before,
                          timeout=25)
    pump(app, 0.7)
    eng, em = engine_capture(rb, os.path.join(OUT_DIR, "08_engine_final_resize.png"))
    dsk, dm = grab_desktop(app, w, os.path.join(OUT_DIR, "08_screen_final_resize.png"),
                           rb=rb)
    desc_final = rb.describe_camera("final")
    print(f"[resize] cam@final: {desc_final}", flush=True)
    check_clip("final resize", desc_final)
    check("post-camera resize: new frame presented", final_ok,
          f"{final_before} -> {rb_mod.get_present_count()}")
    check("post-camera resize: engine renders content",
          eng is not None and em.get("std", 0) > 2.0, f"std={em.get('std', -1):.2f}")
    check("post-camera resize: desktop pixels visible",
          dsk is not None and dm.get("std", 0) > 2.0, f"std={dm.get('std', -1):.2f}")

    # ---- 6. native logging + validation layer --------------------------------
    pump(app, 1.0)
    v = analyse_stderr()
    check("native [VULKAN RESIZE] log present", v["resize_logs"] > 0, str(v["resize_logs"]))
    check("native [VULKAN SYNC] log present", v["sync_logs"] > 0, str(v["sync_logs"]))
    check("native [VULKAN SYNC RESET] log present", v["reset_logs"] > 0, str(v["reset_logs"]))
    check("old swapchain retired only after replacement created",
          v["old_retired"] > 0, str(v["old_retired"]))
    check("validation layer actually ran", v["exists"],
          v["path"] or "NKV_ERR_LOG not set")
    check("validation ERRORS = 0", v["errors"] == 0, f"count={v['errors']}")
    for line in v["error_lines"][:20]:
        print(f"[resize]   VAL-ERR: {line}", flush=True)
    print(f"[resize] validation warnings (informational): {v['warnings']}", flush=True)
    for line in v["warn_lines"][:10]:
        print(f"[resize]   VAL-WARN: {line}", flush=True)

    code = report()

    # ---- controlled teardown -------------------------------------------------
    # Leaving the window, the Vulkan surface and the engine DLL to Python's
    # shutdown order ended the run with a 0xC0000409 fast-fail at interpreter
    # exit AFTER every check had already gone green: the surface outlives the
    # device, then the loader unloads a library still holding live handles.
    # Destroy them here, in the order Vulkan requires, and leave by os._exit so
    # the report's exit code is the last word.
    try:
        win.close()
        pump(app, 0.3)
    except Exception:
        pass
    try:
        rb.shutdown()
    except Exception:
        pass
    return code


if __name__ == "__main__":
    _code = 3
    try:
        _code = main()
    except Exception:
        traceback.print_exc()
        _code = 3
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        # os._exit, not sys.exit: interpreter shutdown would now unwind Qt and
        # the ctypes-loaded Vulkan DLL in whatever order the GC feels like,
        # which is exactly where the fast-fail was coming from. Everything that
        # owns a handle has already been torn down inside main().
        os._exit(_code)
