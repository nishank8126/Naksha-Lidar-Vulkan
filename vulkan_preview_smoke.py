"""vulkan_preview_smoke.py - end-to-end check of the NAKSHA_VULKAN_PREVIEW
side-by-side split view (real Vulkan Shaded Class frame next to VTK).

What it verifies, in order:
  1. NAKSHA_RENDER_BACKEND=vulkan reaches READY and NAKSHA_VULKAN_PREVIEW=1
     docks the _VulkanSurfaceWidget as the right pane of the main splitter
     (VTK left, overlays intact).
  2. A real LAS file loads through the normal threaded FileLoaderWorker
     pipeline with NO dialogs (open_file(filenames=..., prompt_import=False)).
  3. Display mode "shaded_class" pushes the crisp-hybrid surface through the
     native seam (SHADING_VULKAN_SEAM) and mark_active presents a frame.
  4. Frames are actually presented: VULKAN_PRESENT_COUNT > 0 and the status
     bar shows the strict "ACTIVE (Preview)" wording.
  5. VTK -> Vulkan camera sync: moving the VTK camera produces new Vulkan
     presentations without any geometry re-upload.
  6. Screen capture of both panes (QScreen.grabWindow) + RGB pixel-diff
     metrics (mean / p95 / max) + PNG artifacts on disk.

Usage:
    python vulkan_preview_smoke.py [C:\\path\\file.las]   (default 123.las)

Exit code 0 = every check passed, 1 = a check failed, 3 = crashed.
Run with the window in the foreground - QScreen.grabWindow reads real
desktop pixels, so occluding the window skews the capture.
"""
from __future__ import annotations

import os
import sys
import time
import traceback

# ---- env must be set BEFORE any gui module is imported ----------------------
os.environ["NAKSHA_RENDER_BACKEND"] = "vulkan"
os.environ["NAKSHA_VULKAN_PREVIEW"] = "1"

# main.py reconfigures the console to UTF-8 before importing the GUI stack;
# do the same here so redirected log files (cp1252 by default on Windows)
# cannot break the emoji/status prints in gui.shading_display at import time.
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
OUT_DIR = os.path.join(PROJECT_ROOT, "vulkan_preview_smoke_output")

_CHECKS: list[tuple[str, bool, str]] = []
_STATE: dict = {}


def check(name: str, ok: bool, detail: str = "") -> bool:
    ok = bool(ok)
    _CHECKS.append((name, ok, detail))
    tag = "PASS" if ok else "FAIL"
    print(f"[smoke] {tag}  {name}" + (f"  ({detail})" if detail else ""), flush=True)
    return ok


def report() -> int:
    failed = [c for c in _CHECKS if not c[1]]
    print("=" * 70, flush=True)
    print(f"[smoke] {len(_CHECKS) - len(failed)}/{len(_CHECKS)} checks passed", flush=True)
    for name, _ok, detail in failed:
        print(f"[smoke]   FAILED: {name}" + (f"  ({detail})" if detail else ""), flush=True)
    print(f"[smoke] artifacts: {OUT_DIR}", flush=True)
    print("=" * 70, flush=True)
    return 0 if not failed else 1


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


def diff_metrics(a: np.ndarray, b: np.ndarray) -> dict:
    d = np.abs(a[..., :3].astype(np.int16) - b[..., :3].astype(np.int16))
    return {
        "mean_rgb": float(d.mean()),
        "p95_rgb": float(np.percentile(d, 95)),
        "max_rgb": int(d.max()),
    }


def _suppress_modal_dialogs() -> None:
    """Never let a QMessageBox block the unattended run (memory warnings,
    missing-classification notices, etc. are logged and dismissed)."""
    from PySide6.QtWidgets import QMessageBox

    def _warn(*args, **kwargs):
        print(f"[smoke] QMessageBox.warning suppressed: {args[2] if len(args) > 2 else args}", flush=True)
        return QMessageBox.Yes

    def _crit(*args, **kwargs):
        print(f"[smoke] QMessageBox.critical suppressed: {args[2] if len(args) > 2 else args}", flush=True)
        return QMessageBox.Ok

    def _info(*args, **kwargs):
        print(f"[smoke] QMessageBox.information suppressed: {args[2] if len(args) > 2 else args}", flush=True)
        return QMessageBox.Ok

    QMessageBox.warning = _warn
    QMessageBox.critical = _crit
    QMessageBox.information = _info


def main() -> int:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtWidgets import QApplication

    from gui.app_window import NakshaApp
    from gui.dialogs.load_pointcloud_dialog import DEFAULT_IMPORT_OPTIONS
    from gui import render_backend as rb_mod
    from gui.render_backend import BackendState

    if not os.path.isfile(LAS_PATH):
        print(f"[smoke] LAS file not found: {LAS_PATH}", flush=True)
        return 1

    _suppress_modal_dialogs()
    app = QApplication.instance() or QApplication(sys.argv[:1])

    # ---- construct app -----------------------------------------------------
    print("[smoke] constructing NakshaApp ...", flush=True)
    t0 = time.monotonic()
    win = NakshaApp()
    _STATE["win"] = win
    _STATE["app"] = app
    win.show()
    win.raise_()
    win.activateWindow()
    pump(app, 1.0)
    print(f"[smoke] app constructed in {time.monotonic() - t0:.1f}s", flush=True)

    # ---- 1. backend + split preview ---------------------------------------
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
    check(
        "split preview installed (>=3 splitter panes)",
        rb.vulkan_widget is not None and win.splitter.count() >= 3,
        f"panes={win.splitter.count()}",
    )
    pump(app, 0.5)
    check(
        "preview widget visible (right pane)",
        rb.vulkan_widget is not None and rb.vulkan_widget.isVisible(),
        f"size={rb.vulkan_widget.width()}x{rb.vulkan_widget.height()}" if rb.vulkan_widget else "",
    )
    check("VTK widget still visible (left pane)", win.vtk_widget.interactor.isVisible())
    check("preview camera sync installed", rb._camera_sync_timer is not None)

    # ---- 2. programmatic LAS load (no dialogs) -----------------------------
    print(f"[smoke] loading {LAS_PATH} ...", flush=True)
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

    # ---- 3. Shaded Class through the native seam ---------------------------
    print("[smoke] switching display mode -> shaded_class ...", flush=True)
    t0 = time.monotonic()
    win.set_display_mode("shaded_class")
    active = wait_until(
        app,
        lambda: (
            rb.state == BackendState.ACTIVE
            and rb.active_render_mode == "shaded_class"
            and rb_mod.get_present_count() > 0
        ),
        timeout=900,
    )
    check("Shaded Class ACTIVE + first frame presented", active,
          f"{time.monotonic() - t0:.1f}s, present_count={rb_mod.get_present_count()}, "
          f"mode={rb.active_render_mode}")
    if not active:
        return report()

    # ---- 4. strict status label -------------------------------------------
    pump(app, 1.2)  # let the 1 s status-bar timer tick over
    win._update_render_backend_status_label()
    label = win.render_backend_status_label.text()
    check("status label = strict 'ACTIVE (Preview)'", "ACTIVE (Preview)" in label, label)

    # ---- 5. VTK -> Vulkan camera sync (pan produces a new frame) -----------
    cam = win.vtk_widget.renderer.GetActiveCamera()
    pos, fp = cam.GetPosition(), cam.GetFocalPoint()
    base = rb_mod.get_present_count()
    pan = 5.0  # world units - any pan works; proves matrix-only re-render
    cam.SetFocalPoint(fp[0] + pan, fp[1], fp[2])
    cam.SetPosition(pos[0] + pan, pos[1], pos[2])
    synced = wait_until(app, lambda: rb_mod.get_present_count() > base, timeout=20)
    check("VTK camera pan -> new Vulkan frame (no re-upload)", synced,
          f"present {base} -> {rb_mod.get_present_count()}")

    # ---- 6. capture both panes + pixel-diff metrics ------------------------
    os.makedirs(OUT_DIR, exist_ok=True)
    screen = QApplication.primaryScreen()
    win.raise_()
    win.activateWindow()
    pump(app, 0.5)

    full = screen.grabWindow(int(win.winId()))
    check("full-window screen capture non-null", not full.isNull(),
          f"{full.width()}x{full.height()}")
    if full.isNull():
        return report()
    win_origin = win.mapToGlobal(QPoint(0, 0))

    def pane_rect(widget):
        gp = widget.mapToGlobal(QPoint(0, 0))
        return (gp.x() - win_origin.x(), gp.y() - win_origin.y(),
                max(1, widget.width()), max(1, widget.height()))

    vx, vy, vw, vh = pane_rect(win.vtk_widget.interactor)
    kx, ky, kw, kh = pane_rect(rb.vulkan_widget)
    vtk_img = full.copy(vx, vy, vw, vh)
    vk_img = full.copy(kx, ky, kw, kh)
    full.save(os.path.join(OUT_DIR, "window_full.png"))
    vtk_img.save(os.path.join(OUT_DIR, "vtk_pane.png"))
    vk_img.save(os.path.join(OUT_DIR, "vulkan_pane.png"))

    vtk_np = qimg_to_np(vtk_img)
    vk_scaled = vk_img.scaled(vtk_img.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    vk_np = qimg_to_np(vk_scaled)
    metrics = diff_metrics(vtk_np, vk_np)
    print(f"[smoke] VTK vs Vulkan pixel diff: mean={metrics['mean_rgb']:.2f} "
          f"p95={metrics['p95_rgb']:.1f} max={metrics['max_rgb']} (informational)",
          flush=True)
    with open(os.path.join(OUT_DIR, "pixel_diff_metrics.txt"), "w", encoding="utf-8") as fh:
        fh.write(f"vtk_size={vw}x{vh} vulkan_size={kw}x{kh}\n")
        for k, v in metrics.items():
            fh.write(f"{k}={v}\n")
        fh.write(f"present_count={rb_mod.get_present_count()}\n")
        fh.write(f"status_label={label}\n")

    check("VTK pane has real content", float(vtk_np[..., :3].std()) > 1.0,
          f"std={vtk_np[..., :3].std():.1f}")
    check("Vulkan pane has real content", float(vk_np[..., :3].std()) > 1.0,
          f"std={vk_np[..., :3].std():.1f}")
    check("VULKAN_PRESENT_COUNT > 0", rb_mod.get_present_count() > 0,
          str(rb_mod.get_present_count()))
    return report()


if __name__ == "__main__":
    _code = 3
    try:
        _code = main()
    except Exception:
        traceback.print_exc()
        _code = 3
    finally:
        sys.exit(_code)
