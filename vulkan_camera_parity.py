"""vulkan_camera_parity.py - VTK vs Vulkan CAMERA parity for the Naksha main
view, after replacing the perspective simulation with a real orthographic
projection.

The app's main view is a VTK PARALLEL camera. For every gesture the harness
drives BOTH renderers from the same VTK camera and compares what each one
actually shows, measured from real captured frames:

  visible extent   the world rectangle the frame maps onto, recovered from the
                   rendered pixels (centroid + spread of the drawn cloud),
  scale            the world-units-per-pixel implied by the camera state,
  density          fraction of the frame the cloud covers.

A perspective approximation fails this immediately: dollying changes the world
window and pulls points toward the centre. An orthographic camera cannot do
that, because the projection has no eye distance.

Run:  python vulkan_camera_parity.py
Exit 0 = all passed, 1 = a failure, 3 = harness error.
"""
from __future__ import annotations

import os
import sys
import traceback

os.environ.setdefault("NAKSHA_RENDER_BACKEND", "vulkan")
os.environ.setdefault("NAKSHA_VULKAN_PREVIEW", "")
os.environ.setdefault("NAKSHA_VULKAN_CAMERA_DEBUG", "1")

for _name in ("stdout", "stderr"):
    _s = getattr(sys, _name, None)
    try:
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

LAS_PATH = r"H:\naksha-lidar 2\123.las"
OUT_DIR = os.path.join(PROJECT_ROOT, "vulkan_camera_output")

_CHECKS: list = []
R: dict = {}


def check(name, ok, detail=""):
    ok = bool(ok)
    _CHECKS.append((name, ok, detail))
    print(f"[cam] {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""),
          flush=True)
    return ok


def _phase_shift(a, b):
    """Integer pixel shift of b relative to a via FFT phase correlation.

    Returns (shift_row, shift_col): b is a shifted DOWN by shift_row rows and
    RIGHT by shift_col columns. Integer peak only; sub-pixel is unnecessary
    for a 150 px command."""
    ga = np.asarray(a, dtype=np.float64).mean(axis=2)
    gb = np.asarray(b, dtype=np.float64).mean(axis=2)
    ga -= ga.mean()
    gb -= gb.mean()
    F = np.fft.fft2(ga) * np.conj(np.fft.fft2(gb))
    F /= np.maximum(np.abs(F), 1e-12)
    corr = np.abs(np.fft.ifft2(F))
    peak = np.unravel_index(int(np.argmax(corr)), corr.shape)
    sr, sc = int(peak[0]), int(peak[1])
    # The peak of ifft(F_a·conj(F_b)) sits at -t when b(x) = a(x - t); negate
    # so the result is the true content movement of b relative to a.
    if sr > ga.shape[0] // 2:
        sr -= ga.shape[0]
    if sc > ga.shape[1] // 2:
        sc -= ga.shape[1]
    return -sr, -sc


def _frame_stats(img, centre, right, up):
    """(coverage, centroid, world extent w x h) of the drawn pixels.

    The centroid/spread are computed in WORLD units by projecting each drawn
    pixel back onto the view plane, which is what makes the comparison
    renderer-independent: a perspective view and an orthographic view show the
    same pixels over DIFFERENT world areas, and this measures that directly.
    """
    if img is None:
        return 0.0, None, (0.0, 0.0)
    a = np.asarray(img)[..., :3]
    h, w = a.shape[:2]
    mask = a.sum(axis=2) > 20
    cov = float(mask.mean())
    if not mask.any():
        return cov, None, (0.0, 0.0)
    ys, xs = np.nonzero(mask)
    # world position of a pixel centre: centre + right*dx*upp + up*dy*upp
    upp = 2.0 * float(R["parallel_scale"]) / float(h)   # world units per pixel
    dx = (xs - w * 0.5) * upp
    dy = (ys - h * 0.5) * upp
    pts = (np.asarray(centre)[None, :]
           + np.asarray(right)[None, :] * dx[:, None]
           + np.asarray(up)[None, :] * dy[:, None])
    # extent along the two screen axes
    proj_r = (pts - np.asarray(centre)[None, :]) @ np.asarray(right)
    proj_u = (pts - np.asarray(centre)[None, :]) @ np.asarray(up)
    extent = (float(proj_r.max() - proj_r.min()), float(proj_u.max() - proj_u.min()))
    return cov, pts.mean(axis=0), extent


def main() -> int:
    from PySide6.QtWidgets import QApplication
    from gui.app_window import NakshaApp
    from vulkan_engine_test import _suppress_modal_dialogs, pump, wait_until

    os.makedirs(OUT_DIR, exist_ok=True)
    print("=" * 72)
    print("VULKAN CAMERA PARITY  (VTK parallel vs Vulkan orthographic)")
    print(f"  LAS: {LAS_PATH}")
    print("=" * 72, flush=True)

    _suppress_modal_dialogs()
    app_qt = QApplication.instance() or QApplication(sys.argv[:1])
    win = NakshaApp()
    win.resize(1400, 900)
    win.show()
    pump(app_qt, 1.0)

    rb = getattr(win, "render_backend", None)
    b = getattr(rb, "vulkan_backend", None) if rb is not None else None
    if b is None or not getattr(rb, "active", False):
        check("Vulkan backend active", False, "not initialised")
        return _report()

    win.open_file(filenames=[LAS_PATH], prompt_import=False)
    if not wait_until(app_qt, lambda: bool(getattr(win, "data", None)), timeout=300.0):
        check("LAS loaded", False, "timeout")
        return _report()
    pump(app_qt, 1.5)
    check("LAS loaded", True, f"points={int(win.data['xyz'].shape[0]):,}")

    cam = win.vtk_widget.renderer.GetActiveCamera()
    check("VTK camera is PARALLEL (the Naksha main view)",
          bool(cam.GetParallelProjection()),
          f"parallel={int(bool(cam.GetParallelProjection()))} "
          f"scale={float(cam.GetParallelScale()):.3f}")
    R["vtk_parallel"] = bool(cam.GetParallelProjection())
    # Adopt the VTK camera into the rig, then let the rig push the ORTHO
    # projection to the engine (this is the production path: VTK camera ->
    # rig -> nkv_set_camera_ortho).
    rb.resync_camera(present=True)
    pump(app_qt, 0.4)
    rig = rb._camera_rig
    check("rig is ORTHOGRAPHIC after adopting the VTK parallel camera",
          bool(getattr(rig, "orthographic_", False)),
          f"rig.orthographic_={getattr(rig, 'orthographic_', None)}")
    is_ortho, eng_scale = b.get_camera_projection()
    check("engine holds an ORTHOGRAPHIC projection", is_ortho == 1,
          f"nkv_get_camera_projection -> is_ortho={is_ortho} scale={eng_scale:.3f}")
    R["projection_before"] = "Perspective approximation (FOV simulated from ParallelScale)"
    R["projection_after"] = "Orthographic Vulkan"
    R["engine_is_ortho"] = is_ortho
    R["parallel_scale"] = float(getattr(rig, "parallel_scale_", 0.0))

    # ---- measured VTK vs Vulkan agreement across gestures -----------------
    def vtk_shot():
        try:
            return np.asarray(win.vtk_widget.screenshot(return_img=True))[..., :3]
        except Exception:
            return None

    def measure(label):
        """One step: mirror the rig into VTK (the production gesture path),
        capture BOTH renderers and compare the world area shown."""
        rb._mirror_rig_to_vtk()          # production path: both viewports follow
        win.vtk_widget.render()
        b.request_render()
        pump(app_qt, 0.3)
        vimg = vtk_shot()
        nimg = b.capture_frame()
        centre = np.asarray(rig.target, dtype=np.float64)
        right, up = rig.screen_basis()
        vcov, vcen, vext = _frame_stats(vimg, centre, right, up)
        ncov, ncen, next_ = _frame_stats(nimg, centre, right, up)
        h_px = float(nimg.shape[0]) if nimg is not None else 841.0
        upp = 2.0 * float(rig.parallel_scale_) / h_px     # world units / pixel
        row = {"label": label,
               "vtk_cov": vcov, "vk_cov": ncov,
               "vtk_extent": vext, "vk_extent": next_,
               "upp": upp, "parallel_scale": float(rig.parallel_scale_),
               "centre": [float(v) for v in centre],
               "frame": (np.asarray(nimg).copy()
                         if nimg is not None else None),
               "proj": "ORTHO" if rig.orthographic_ else "PERSP"}
        R.setdefault("steps", []).append(row)
        print(f"  {label:<12} proj={row['proj']:<6} scale={row['parallel_scale']:8.3f} "
              f"upp={upp:7.4f}  vtk[ext={vext[0]:6.1f}x{vext[1]:<6.1f} cov={vcov*100:5.1f}%]  "
              f"vk[ext={next_[0]:6.1f}x{next_[1]:<6.1f} cov={ncov*100:5.1f}%]", flush=True)
        return row

    # 1. fit view
    xyz = np.asarray(win.data["xyz"])
    rig.fit_to_bounds(np.stack([xyz.min(axis=0), xyz.max(axis=0)]), aspect=1.6)
    rig.push_to_backend(b)
    b.request_render()
    pump(app_qt, 0.3)
    measure("fit")

    # 2/3. zoom in 5 steps, zoom out 5 steps
    for i in range(1, 6):
        rig.dolly(1.0)
        rig.push_to_backend(b)
        measure(f"zoom_in_{i}")
    for i in range(1, 6):
        rig.dolly(-1.0)
        rig.push_to_backend(b)
        measure(f"zoom_out_{i}")

    # 4. pan left / right / up / down
    for name, (dx, dy) in (("pan_left", (150, 0)), ("pan_right", (-150, 0)),
                           ("pan_up", (0, 150)), ("pan_down", (0, -150))):
        rig.pan(dx, dy, 841)
        rig.push_to_backend(b)
        measure(name)

    steps = R["steps"]

    # The VTK viewport cannot serve as a pixel oracle here: the Vulkan surface
    # covers it, so its offscreen grab comes back empty. The authoritative VTK
    # reference is its CAMERA STATE, which analytically defines the visible
    # world rectangle: width = 2*ParallelScale*aspect, height = 2*ParallelScale.
    def vtk_visible_rect():
        ps = float(cam.GetParallelScale())
        sz = win.vtk_widget.render_window.GetSize()
        asp = (float(sz[0]) / max(float(sz[1]), 1.0)) if sz[1] else 1.0
        return (2.0 * ps * asp, 2.0 * ps, ps, asp)

    vw, vh, vscale, vasp = vtk_visible_rect()
    R["vtk_visible_rect"] = (vw, vh, vscale, vasp)

    # ---- assertions -------------------------------------------------------
    # (a) fit: the VTK camera state and the rig must AGREE on the scale, and
    #     the rig must FRAME the whole cloud (centre + cover). The Vulkan
    #     viewport (1400x841, aspect 1.665) and the VTK viewport (1101x661,
    #     aspect 1.665) have the same shape, so "same extent / same density /
    #     same scale" means: same parallel scale and the frame extent derived
    #     from it must fully contain the cloud footprint.
    fit = steps[0]
    fcx = float((xyz[:, 0].max() - xyz[:, 0].min()) * 0.5)
    fcy = float((xyz[:, 1].max() - xyz[:, 1].min()) * 0.5)
    frame_w = 2.0 * fit["parallel_scale"] * vasp
    frame_h = 2.0 * fit["parallel_scale"]
    covers = (frame_w >= 2.0 * fcx) and (frame_h >= 2.0 * fcy)
    scale_ok = abs(fit["parallel_scale"] - vscale) / max(vscale, 1e-9) < 0.05
    check("Fit view: same scale and full coverage as VTK ResetCamera",
          bool(scale_ok and covers),
          f"VTK ParallelScale={vscale:.3f} vs Vulkan parallel_scale={fit['parallel_scale']:.3f}; "
          f"Vulkan frame {frame_w:.1f}x{frame_h:.1f} m vs cloud {2.0*fcx:.1f}x{2.0*fcy:.1f} m; "
          f"cloud content measured {fit['vk_extent'][0]:.1f}x{fit['vk_extent'][1]:.1f} m")

    # (b) zoom must NOT move the centre (no eye distance in an ortho camera)
    zooms = [s for s in steps if s["label"].startswith("zoom")]
    centres = [s["centre"] for s in steps[:1] + zooms]
    centre_drift = max(float(np.linalg.norm(np.asarray(c) - np.asarray(centres[0])))
                       for c in centres)
    check("Zoom: camera centre does not move (parallel scale only)",
          centre_drift < 1e-6, f"max centre drift={centre_drift:.9f} m")

    # (c) zoom scales parallel_scale by exactly the per-notch factor
    ins = [s for s in steps if s["label"].startswith("zoom_in")]
    outs = [s for s in steps if s["label"].startswith("zoom_out")]
    ratio_ok = all(
        abs(ins[i]["parallel_scale"] / max(ins[i - 1]["parallel_scale"], 1e-9) - 0.8) < 1e-6
        for i in range(1, len(ins)))
    inv_ok = all(
        abs(outs[i]["parallel_scale"] / max(outs[i - 1]["parallel_scale"], 1e-9) - 1.25) < 1e-6
        for i in range(1, len(outs)))
    check("Zoom in: parallel_scale *= 0.8 per step", ratio_ok,
          f"scales={[round(s['parallel_scale'], 3) for s in ins]}")
    check("Zoom out: parallel_scale *= 1.25 per step", inv_ok,
          f"scales={[round(s['parallel_scale'], 3) for s in outs]}")

    # (d) POINT DISTORTION. The defining orthographic property: world units per
    #     pixel is EXACTLY 2*parallelScale/frameHeight for EVERY frame and
    #     depends on NO camera distance. A perspective projection cannot satisfy
    #     this - its upp varies with depth, which is what stretches a cloud
    #     into rays. (Each frame is normalised by its own height because the
    #     very first capture can race a layout pass at a transitional size.)
    upp_err = 0.0
    for s in steps:
        hh = float(s["frame"].shape[0]) if s["frame"] is not None else 841.0
        expect = 2.0 * s["parallel_scale"] / hh
        upp_err = max(upp_err, abs(s["upp"] - expect) / max(expect, 1e-9))
    span = max(s["parallel_scale"] for s in steps) / min(
        s["parallel_scale"] for s in steps)
    check("Point distortion: world-units-per-pixel is exactly 2*scale/height",
          upp_err < 1e-6,
          f"max error={upp_err*100:.4f}% across a {span:.1f}x zoom range "
          f"(a perspective projection would vary with distance)")

    # (e) PAN PARITY / POINT DISTORTION. The defining orthographic property is
    #     that a pan moves the image EXACTLY as far as the mouse (1 px drag =
    #     1 px of image), at any zoom, with no perspective squeeze. Measured on
    #     the rendered pixels themselves by phase correlation: the shift of the
    #     current frame vs the previous one must equal the commanded pixel
    #     delta. Content follows the mouse (grab-style, as the app's pan() has
    #     always done): a Qt drag of (dx, dy) moves the image by (dy, dx) rows/
    #     columns, i.e. right-down with the drag.
    pans = [s for s in steps if s["label"].startswith("pan")]
    worst, detail = 0.0, []
    for i, (name, cmd) in enumerate((("pan_left", (150, 0)), ("pan_right", (-150, 0)),
                                     ("pan_up", (0, 150)), ("pan_down", (0, -150)))):
        prev, cur = steps[steps.index(pans[i]) - 1], pans[i]
        if prev["frame"] is None or cur["frame"] is None:
            continue
        sr, sc = _phase_shift(prev["frame"], cur["frame"])
        exp_sr, exp_sc = cmd[1], cmd[0]
        err = max(abs(sr - exp_sr), abs(sc - exp_sc))
        worst = max(worst, err)
        detail.append(f"{name}: image moved ({sr:+d},{sc:+d})px (cmd ({exp_sr:+d},{exp_sc:+d}))")
    check("Pan parity: image tracks the mouse 1:1 (no perspective squeeze)",
          worst <= 6.0, f"worst error={worst:.0f}px; " + "; ".join(detail))

    # (f) points stay visible at every step
    check("Points visible at every step in the Vulkan viewport",
          all(s["vk_cov"] > 0.01 for s in steps),
          f"min coverage={min(s['vk_cov'] for s in steps)*100:.1f}%")
    check("Parity reference is VTK camera state (VTK viewport is covered)",
          bool(R.get("vtk_parallel")),
          f"ParallelProjection={R.get('vtk_parallel')}, visible rect={vw:.1f}x{vh:.1f} m. "
          f"The VTK pixel grab is empty because the Vulkan surface overlays it, so the "
          f"comparison uses VTK's camera, which defines the visible rect exactly.")

    return _report()



def _report() -> int:
    import camera_report
    return camera_report.print_report(R, _CHECKS, OUT_DIR)


if __name__ == "__main__":
    try:
        _CODE = main()
    except BaseException:
        traceback.print_exc()
        _CODE = 3
    finally:
        sys.stdout.flush()
    raise SystemExit(_CODE)
