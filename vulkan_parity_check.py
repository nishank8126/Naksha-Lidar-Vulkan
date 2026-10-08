"""vulkan_parity_check.py - visual-parity harness: native Vulkan vs VTK.

Compares the SAME scene, SAME camera and SAME display mode across both
renderers, per mode: rgb, class, intensity, elevation.

Why this exists
---------------
Screen grabs are not ground truth on Windows: QScreen.grabWindow() AND
PrintWindow() read through GDI, which returns a flat black image for a Vulkan
surface (measured: std=0.00 whatever the engine was presenting). So:

  * Vulkan side -> nkv_capture_frame(): the engine re-renders the current
    frame-slot UBO into a private colour image and reads it back on the CPU.
    Real engine pixels, always.
  * VTK side    -> pyvista's offscreen screenshot (VTK renders through its own
    FBO, so the screenshot path is valid there).

What "parity" means here
------------------------
Point rasterisation is deliberately NOT identical: VTK paints a fixed
point_size in pixels, the native renderer sizes points by footprint/depth
(density-correct, clamped to a band). A per-pixel diff would measure that
design choice, not correctness, so the harness compares what MUST match:
  * coverage  - fraction of non-background pixels (framing)
  * mean RGB  - average colour of the drawn points
  * histogram - 64-bin per-channel correlation (the real colour-match proxy)
and prints the raw per-pixel mean/p95 difference for information only.

Usage:
    python vulkan_parity_check.py [C:\\path\\file.las]     (default 123.las)
Exit code 0 = every mode matched, 1 = a mode mismatched, 3 = crashed.
Artifacts land in vulkan_parity_output/.
"""
from __future__ import annotations

import os
import sys
import time
import traceback

# ---- env BEFORE any gui import -------------------------------------------
os.environ["NAKSHA_RENDER_BACKEND"] = "vulkan"
# "" (not "1") so app_window takes install_as_main_viewport(); set to "" BEFORE
# importing vulkan_engine_test, which does setdefault("NAKSHA_VULKAN_PREVIEW","1").
os.environ["NAKSHA_VULKAN_PREVIEW"] = ""

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
OUT_DIR = os.path.join(PROJECT_ROOT, "vulkan_parity_output")
MODES = ("rgb", "class", "intensity", "elevation")

# Parity thresholds (documented; sizes differ by design, so coverage is loose
# and colour agreement is what must really hold).
MIN_COVERAGE_DELTA = 0.22     # percentage points of frame coverage
# Mean-RGB distance is bounded but loose on purpose: VTK's classification view
# runs its own GPU shader that bakes class colours/brightness differently than
# gui/pointcloud_display.compute_colors() does, while the native LUT bakes the
# latter (palette colour x app.class_weight). The per-channel histogram
# correlation below is the strict colour check; this one only catches gross
# brightness inversions.
MAX_MEAN_RGB_DIST = 55.0
MIN_HIST_CORR = 0.80          # 64-bin per-channel histogram correlation

_CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    ok = bool(ok)
    _CHECKS.append((name, ok, detail))
    print(f"[parity] {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""),
          flush=True)
    return ok


def report() -> int:
    failed = [c for c in _CHECKS if not c[1]]
    print("=" * 72, flush=True)
    print(f"[parity] {len(_CHECKS) - len(failed)}/{len(_CHECKS)} checks passed", flush=True)
    for name, _ok, detail in failed:
        print(f"[parity]   FAILED: {name}  ({detail})", flush=True)
    print(f"[parity] artifacts: {OUT_DIR}", flush=True)
    print("=" * 72, flush=True)
    return 0 if not failed else 1


def image_stats(arr: np.ndarray):
    """(coverage, mean RGB over drawn pixels, per-channel histograms)."""
    drawn = arr.sum(axis=2) > 20          # anything brighter than near-black
    cover = float(drawn.mean())
    mean = arr[drawn].mean(axis=0) if drawn.any() else np.zeros(3)
    hists = [np.histogram(arr[..., c], bins=64, range=(0, 256), density=True)[0]
             for c in range(3)]
    return cover, mean, hists


def hist_correlation(hists_a, hists_b) -> float:
    """Mean per-channel Pearson correlation of the normalised histograms."""
    total = 0.0
    for a, b in zip(hists_a, hists_b):
        a = np.asarray(a, dtype=np.float64)
        b = np.asarray(b, dtype=np.float64)
        if a.sum() <= 0 or b.sum() <= 0:
            continue
        a = a / max(a.sum(), 1e-9)
        b = b / max(b.sum(), 1e-9)
        da, db = a.std(), b.std()
        if da <= 0 or db <= 0:
            total += 1.0 if np.allclose(a, b) else 0.0
            continue
        total += float(np.corrcoef(a, b)[0, 1])
    return total / len(hists_a)


def vtk_screenshot(win):
    """Offscreen VTK render of the main view as an (h, w, 3) RGB array."""
    widget = win.vtk_widget
    for plotter in (widget, getattr(widget, "plotter", None)):
        if plotter is None:
            continue
        try:
            img = plotter.screenshot(return_img=True)
            if img is not None:
                return np.asarray(img)[..., :3]
        except Exception:
            continue
    return None


def top_colour_keys(arr, k: int = 8, levels: int = 16):
    """Dominant quantised colour keys of an (N, 3) uint8 array.

    Anti-aliasing and size differences mean exact RGB equality between two
    renderers is impossible, so colours are quantised to `levels` steps per
    channel (16 -> 16 levels) and compared as a SET of dominant buckets: if a
    renderer paints the same colour ramp, its dominant buckets match even
    though individual pixels differ.
    """
    a = np.asarray(arr, dtype=np.int64).reshape(-1, 3)
    if a.size == 0:
        return set()
    q = a // max(256 // levels, 1)
    keys = q[:, 0] * (levels * levels) + q[:, 1] * levels + q[:, 2]
    vals, counts = np.unique(keys, return_counts=True)
    order = np.argsort(-counts)[:k]
    return {int(vals[i]) for i in order}


def colour_overlap(ref_keys, img_keys) -> float:
    if not ref_keys or not img_keys:
        return 0.0
    return len(ref_keys & img_keys) / float(len(ref_keys))


def vtk_frame_is_flat(vtk_rgb, drawn_min: float = 0.005) -> bool:
    """True when the VTK frame is (nearly) a single flat colour over the drawn
    area - i.e. VTK has an actor on screen but no scalars bound (the app
    switches to a plain pyvista actor for some modes). Colour parity is then
    checked against compute_colors() instead of that placeholder frame.

    Robustness: a plain std over drawn pixels is too sensitive (anti-aliased
    edges are a few percent of them), so most drawn pixels must sit within a
    few units of the MEDIAN drawn colour."""
    if vtk_rgb is None:
        return True
    drawn = vtk_rgb[vtk_rgb.sum(axis=2) > 20]
    if drawn.size == 0 or float(drawn.mean()) < drawn_min:
        return True
    med = np.median(drawn.reshape(-1, 3), axis=0)
    near = np.abs(drawn - med).max(axis=1) < 8
    return float(near.mean()) > 0.90


def wait_for_vtk_content(app_qt, win, timeout: float = 15.0):
    """Poll until the VTK view actually shows something, then return
    (rgb, has_content).

    Several display modes rebuild their actor asynchronously (colour thread,
    GPU shader compile), so a fixed sleep can screenshot an empty view - which
    would then be compared against an equally empty native frame and report a
    meaningless "parity" of two black images. When VTK stays empty the mode is
    reported as inconclusive instead of a Vulkan failure.
    """
    end = time.monotonic() + timeout
    last = None
    while True:
        last = vtk_screenshot(win)
        if last is not None and float((last.sum(axis=2) > 20).mean()) > 0.005:
            return last, True
        if time.monotonic() > end:
            return last, False
        try:
            app_qt.processEvents()
        except Exception:
            pass
        time.sleep(0.3)

def set_reference_camera(win, rb) -> bool:
    """Frame the cloud identically in BOTH renderers before comparing.

    Mode switches legitimately move the VTK camera (2D-plan handling, shader
    rebuilds, ResetCamera calls). Both viewports then faithfully follow it into
    an empty view - which measures nothing. So pin a known pose on the VTK
    camera (the app's own source of truth) and push it through the very same
    adopt path the app uses: rb.resync_camera() -> rig -> nkv_set_camera_lookat.
    """
    try:
        plotter = win.vtk_widget                      # QtInteractor is a Plotter
        plotter.camera.parallel_projection = False
        plotter.reset_camera()                        # frames every visible prop
        cam = plotter.camera                          # pyvista.Camera wrapper
        cam.azimuth = -35.0
        cam.elevation = 28.0
        cam.up = (0.0, 1.0, 0.0)                      # engine's fixed world up
        plotter.reset_camera_clipping_range()
        win.vtk_widget.render()
        rb.resync_camera(present=True)                # VTK -> rig -> native camera
        return True
    except Exception as _err:
        print(f"[parity] reference camera failed: {_err!r}", flush=True)
        return False


def main() -> int:
    from PySide6.QtWidgets import QApplication
    from gui.app_window import NakshaApp
    from gui.pointcloud_display import update_pointcloud
    from vulkan_engine_test import _suppress_modal_dialogs, pump, save_rgb_png, wait_until

    print("=" * 72, flush=True)
    print("NAKSHA VULKAN <-> VTK PARITY CHECK", flush=True)
    print(f"  las file : {LAS_PATH}", flush=True)
    print(f"  modes    : {', '.join(MODES)}", flush=True)
    print("=" * 72, flush=True)
    if not os.path.isfile(LAS_PATH):
        print(f"[parity] LAS file not found: {LAS_PATH}", flush=True)
        return 1
    os.makedirs(OUT_DIR, exist_ok=True)

    _suppress_modal_dialogs()
    app_qt = QApplication.instance() or QApplication(sys.argv[:1])
    win = NakshaApp()
    win.resize(1400, 900)
    win.show()
    pump(app_qt, 1.0)

    rb = getattr(win, "render_backend", None)
    backend = getattr(rb, "vulkan_backend", None) if rb is not None else None
    check("Vulkan backend active", backend is not None and bool(getattr(rb, "active", False)),
          f"state={getattr(rb, 'state', None)}")
    if backend is None:
        return report()

    print(f"[parity] loading {os.path.basename(LAS_PATH)} ...", flush=True)
    win.open_file(filenames=[LAS_PATH], prompt_import=False)
    if not wait_until(app_qt, lambda: bool(getattr(win, "data", None)), timeout=240.0):
        check("LAS loaded", False, "timeout")
        return report()
    pump(app_qt, 1.0)
    backend.request_render()
    pump(app_qt, 0.6)
    n_points = int(win.data["xyz"].shape[0])
    check("LAS loaded", True, f"points={n_points:,}")

    summary = []
    for mode in MODES:
        t0 = time.monotonic()
        print(f"\n[parity] ---- mode: {mode} ----", flush=True)
        # 1. VTK renders the mode (this also rebuilds app.class_palette etc.)
        #    app.display_mode is the app's own source of truth for the mode
        #    (compute_colors reads it), and the menu/shortcut paths always set
        #    it before calling update_pointcloud - so the harness must too.
        try:
            win.display_mode = mode
            update_pointcloud(win, mode)
        except Exception as _err:
            check(f"{mode}: VTK update", False, repr(_err))
            continue
        pump(app_qt, 1.2)
        set_reference_camera(win, rb)
        pump(app_qt, 0.4)

        # 2. VTK first: wait until this mode really has content (actor rebuilds
        #    are async), otherwise both sides would be black and "parity" would
        #    be a vacuous comparison of two empty images.
        vtk_rgb, vtk_ok = wait_for_vtk_content(app_qt, win)
        check(f"{mode}: VTK view has content", vtk_ok,
              "" if vtk_ok else "VTK stayed empty - mode reported inconclusive")

        # Re-pin the camera AFTER the async rebuild settled (it can move the VTK
        # camera) and re-grab the VTK frame, so both sides are captured with one
        # agreed pose instead of racing the actor rebuild.
        set_reference_camera(win, rb)
        pump(app_qt, 0.4)
        if vtk_ok:
            vtk_rgb = vtk_screenshot(win)

        # 3. native side: push the same display state, then read pixels back
        try:
            rb.sync_point_shading(mode=mode, present=True)
        except Exception as _err:
            check(f"{mode}: Vulkan shading sync", False, repr(_err))
            continue
        pump(app_qt, 0.6)
        native = backend.capture_frame()
        check(f"{mode}: native capture non-empty", native is not None and native.size > 0,
              f"{None if native is None else native.shape}")
        if native is None or native.size == 0:
            continue
        n_rgb = native[..., :3]
        save_rgb_png(n_rgb, os.path.join(OUT_DIR, f"vulkan_{mode}.png"))
        if vtk_rgb is not None:
            save_rgb_png(vtk_rgb, os.path.join(OUT_DIR, f"vtk_{mode}.png"))

        n_cov, n_mean, n_hist = image_stats(n_rgb)

        # Colour reference: the app's OWN per-point colours for this mode. This
        # is the strict check for the GPU LUT path, and it stays meaningful even
        # when the VTK frame is a flat placeholder (actor without scalars).
        try:
            from gui.pointcloud_display import compute_colors
            ref = np.asarray(compute_colors(win, mask=None), dtype=np.uint8)
            drawn_native = n_rgb[n_rgb.sum(axis=2) > 20]
            print(f"[parity]   reference colours: mean={np.round(ref.reshape(-1, 3).mean(axis=0), 1)} "
                  f"lit={(ref.sum(axis=1) > 0).mean() * 100:.0f}%  "
                  f"drawn-native mean={np.round(drawn_native.mean(axis=0), 1) if drawn_native.size else None}",
                  flush=True)
            if ref.size and drawn_native.size:
                step = max(1, ref.shape[0] // 50000)
                overlap = colour_overlap(top_colour_keys(ref[::step]),
                                         top_colour_keys(drawn_native))
                check(f"{mode}: GPU colours match app compute_colors()", overlap >= 0.5,
                      f"{overlap * 100:.0f}% of the top-8 colour buckets match")
            else:
                check(f"{mode}: GPU colours match app compute_colors()", False,
                      "no reference colours or no drawn native pixels")
        except Exception as _err:
            check(f"{mode}: GPU colours match app compute_colors()", False, repr(_err))

        if not vtk_ok:
            print(f"[parity]   native coverage={n_cov * 100:.1f}% "
                  f"mean={np.round(n_mean, 1)} - VTK empty, parity INCONCLUSIVE",
                  flush=True)
            summary.append(f"{mode}: INCONCLUSIVE (VTK view empty; native coverage "
                           f"{n_cov * 100:.1f}%)")
            continue
        check(f"{mode}: native pixels are real (not flat/black)",
              float(n_rgb.std()) > 1.0 and len(np.unique(n_rgb.reshape(-1, 3), axis=0)) > 4,
              f"std={float(n_rgb.std()):.2f} coverage={n_cov * 100:.1f}%")

        v_cov, v_mean, v_hist = image_stats(vtk_rgb)
        cov_delta = abs(n_cov - v_cov)
        mean_dist = float(np.abs(n_mean - v_mean).mean())
        corr = hist_correlation(n_hist, v_hist)
        vtk_flat = vtk_frame_is_flat(vtk_rgb)
        if vtk_flat:
            print("[parity]   VTK frame is a flat placeholder (actor without "
                  "scalars) - its colours are not used for the comparison",
                  flush=True)
        if n_rgb.shape == vtk_rgb.shape:      # informational only, see docstring
            d = np.abs(n_rgb.astype(np.int16) - vtk_rgb.astype(np.int16))
            px_mean, px_p95 = float(d.mean()), float(np.percentile(d, 95))
        else:
            px_mean = px_p95 = float("nan")

        print(f"[parity]   coverage  vulkan={n_cov * 100:5.1f}%  vtk={v_cov * 100:5.1f}%  "
              f"delta={cov_delta * 100:.1f}pp", flush=True)
        print(f"[parity]   mean RGB  vulkan={np.round(n_mean, 1)}  vtk={np.round(v_mean, 1)}  "
              f"dist={mean_dist:.1f}", flush=True)
        print(f"[parity]   hist corr = {corr:.3f}   (per-pixel mean={px_mean:.1f} "
              f"p95={px_p95:.1f} - informational, rasterisation differs by design)",
              flush=True)
        check(f"{mode}: coverage matches VTK", cov_delta <= MIN_COVERAGE_DELTA,
              f"delta={cov_delta * 100:.1f}pp (limit {MIN_COVERAGE_DELTA * 100:.0f}pp)")
        check(f"{mode}: mean colour matches VTK",
              (vtk_flat or mean_dist <= MAX_MEAN_RGB_DIST),
              f"dist={mean_dist:.1f} (limit {MAX_MEAN_RGB_DIST:.0f})"
              + (" - VTK flat placeholder, colour checked against compute_colors()"
                 if vtk_flat else ""))
        check(f"{mode}: colour histogram matches VTK", corr >= MIN_HIST_CORR,
              f"corr={corr:.3f} (limit {MIN_HIST_CORR:.2f})")
        summary.append(f"{mode}: cov {n_cov * 100:.1f}%/{v_cov * 100:.1f}%  "
                       f"mean-dist {mean_dist:.1f}  corr {corr:.3f}")
        print(f"[parity]   ({time.monotonic() - t0:.1f}s)", flush=True)

    print("\n[parity] summary", flush=True)
    for line in summary:
        print(f"[parity]   {line}", flush=True)
    with open(os.path.join(OUT_DIR, "metrics.txt"), "w", encoding="utf-8") as fh:
        fh.write(f"las={LAS_PATH}\npoints={n_points}\n")
        for line in summary:
            fh.write(line + "\n")
    try:
        win.close()
        pump(app_qt, 0.5)
    except Exception:
        pass
    return report()


if __name__ == "__main__":
    try:
        _CODE = main()
    except BaseException:
        traceback.print_exc()
        _CODE = 3
    finally:
        sys.stdout.flush()
    raise SystemExit(_CODE)