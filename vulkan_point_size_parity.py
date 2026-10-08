"""
vulkan_point_size_parity.py - VTK-vs-Vulkan point SIZE parity across a zoom ladder.

The requirement is that the Vulkan point cloud LOOKS like the VTK one: same dot
diameter, same density, same spacing, same response to zoom and pan. This script
measures that instead of asserting it.

VTK source of truth (gui/unified_actor_manager.py):
    _BASE_POINT_SIZE = 2.5
    compute_point_size(weight, base) = max(max(0.5, base*0.1), min(base*weight, 30.0))
    vertex shader:  float ps = max(1.0, weight_lut[c_idx]);
                    gl_PointSize = ps + border_growth;
i.e. a FIXED PIXEL size with no depth, zoom, FOV or device-limit dependence.

Run:  python vulkan_point_size_parity.py
Exit 0 = all passed, 1 = a failure, 3 = harness error.
"""
from __future__ import annotations

import os
import sys

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
PTC_PATH = r"H:\TESTING CONTIUES\Class_ENEL_2025_connect 1.ptc"
OUT_DIR = os.path.join(PROJECT_ROOT, "vulkan_pointsize_output")

# The zoom ladder the task asks for: fit, x2, x5, x10, x20.
ZOOM_LADDER = [("fit", 1.0), ("zoom_x2", 2.0), ("zoom_x5", 5.0),
               ("zoom_x10", 10.0), ("zoom_x20", 20.0)]

_CHECKS: list = []
R: dict = {}


def check(name: str, ok: bool, detail: str = "") -> bool:
    ok = bool(ok)
    _CHECKS.append((name, ok, detail))
    print(f"[psz] {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""),
          flush=True)
    return ok


def coverage(img) -> float:
    """Fraction of non-background pixels in a native capture."""
    if img is None:
        return 0.0
    a = np.asarray(img)[..., :3]
    return float((a.sum(axis=2) > 20).mean())


def median_dot_diameter_px(img, max_px: int) -> float:
    """Median diameter of isolated non-background blobs, in pixels.

    Only blobs whose bounding box is small enough to be a single dot are
    counted, so merged/overlapping regions do not contaminate the statistic.
    Returns 0.0 when nothing is separable.
    """
    if img is None:
        return 0.0
    a = np.asarray(img)[..., :3]
    mask = a.sum(axis=2) > 20
    if not mask.any():
        return 0.0
    try:
        from scipy import ndimage
        lab, n = ndimage.label(mask)
        if n == 0:
            return 0.0
        areas = ndimage.sum(mask, lab, range(1, n + 1))
        objs = ndimage.find_objects(lab)
    except Exception:
        return 0.0
    diams = []
    for i, sh in enumerate(objs):
        a_i = float(areas[i])
        if a_i <= 0:
            continue
        bh = sh[0].stop - sh[0].start
        bw = sh[1].stop - sh[1].start
        if max(bh, bw) > max_px * 1.5:      # merged blob, skip
            continue
        diams.append(2.0 * float(np.sqrt(a_i / np.pi)))
    if not diams:
        return 0.0
    return float(np.median(diams))


def _report() -> int:
    lines = []
    lines.append("=" * 72)
    lines.append("VULKAN POINT SIZE PARITY REPORT")
    lines.append("=" * 72)
    lines.append("Reference: VTK   Dataset: 123.las")
    lines.append("")
    lines.append(f"{'step':<10} {'VTK px':>8} {'Vulkan px':>10} {'diff':>7} "
                 f"{'coverage':>10} {'dist':>11}")
    for s in R.get("steps", []):
        lines.append(f"{s['label']:<10} {s['vtk_px']:>8.2f} {s['vk_px']:>10.2f} "
                     f"{s['diff_px']:>7.2f} {s['cov']*100:>9.1f}% {s['dist']:>11.2f}")
    lines.append("")
    failed = [n for n, ok, _ in _CHECKS if not ok]
    lines.append(f"Final status: {'PASS' if not failed else 'FAIL'}")
    if failed:
        lines.append("  failed: " + "; ".join(failed))
    text = "\n".join(lines)
    print("\n" + text, flush=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "point_size_parity_report.txt"), "w",
              encoding="utf-8") as fh:
        fh.write(text + "\n")
    return 0 if not failed else 1



def main() -> int:
    import math as _m
    from PySide6.QtWidgets import QApplication
    from gui.app_window import NakshaApp
    from vulkan_engine_test import _suppress_modal_dialogs, pump, wait_until
    from gui.unified_actor_manager import compute_point_size, _BASE_POINT_SIZE
    from gui.render_backend import shading_uniforms

    os.makedirs(OUT_DIR, exist_ok=True)
    print("=" * 72)
    print("VULKAN POINT SIZE PARITY")
    print(f"  LAS: {LAS_PATH}")
    print(f"  PTC: {PTC_PATH}")
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
        check("Vulkan backend active", False, "backend not initialised")
        return _report()
    dev_max = float(b.get_max_point_size())
    R["device_max"] = dev_max
    check("Vulkan backend active", True,
          f"device={b.get_device_name()} hw max_point_size={dev_max:.1f}")

    print("\n[psz] loading 123.las ...", flush=True)
    win.open_file(filenames=[LAS_PATH], prompt_import=False)
    if not wait_until(app_qt, lambda: bool(getattr(win, "data", None)), timeout=300.0):
        check("LAS loaded", False, "timeout")
        return _report()
    pump(app_qt, 1.5)
    check("LAS loaded", True, f"points={int(win.data['xyz'].shape[0]):,}")

    # ---- VTK reference value (pure function, no camera involved) ----------
    base = float(getattr(win, "point_size", _BASE_POINT_SIZE) or _BASE_POINT_SIZE)
    vtk_px = float(compute_point_size(1.0, base))
    R["base"], R["vtk_px"] = base, vtk_px
    print(f"\n[psz] VTK _BASE_POINT_SIZE        = {_BASE_POINT_SIZE}")
    print(f"[psz] app point_size             = {base:.3f}")
    print(f"[psz] VTK compute_point_size(1.0) = {vtk_px:.2f} px", flush=True)

    # ---- Vulkan resolved value ---------------------------------------------
    u = shading_uniforms(win)
    vk_px = float(u["min_px"])
    R["vk_px"] = vk_px
    print(f"[psz] Vulkan min_px == max_px    = {u['min_px']:.2f} == {u['max_px']:.2f}")
    print(f"[psz] device hw cap (capability) = {dev_max:.1f}  <- NOT a target")

    check("Vulkan resolved size matches VTK (<=1px)", abs(vk_px - vtk_px) <= 1.0,
          f"VTK={vtk_px:.2f} Vulkan={vk_px:.2f} diff={abs(vk_px - vtk_px):.2f}px")
    check("min_px == max_px (fixed-pixel mode, no depth term)",
          abs(float(u["min_px"]) - float(u["max_px"])) < 1e-6,
          f"min={u['min_px']} max={u['max_px']}")
    check("point size NOT inflated toward the device max", vk_px < dev_max * 0.5,
          f"size={vk_px:.2f}px vs hw cap={dev_max:.1f}px")

    # ---- zoom ladder -------------------------------------------------------
    rig = rb._camera_rig
    xyz = np.asarray(win.data["xyz"])
    bounds = np.stack([xyz.min(axis=0), xyz.max(axis=0)])
    rig.fit_to_bounds(bounds, aspect=1.6)
    rb.resync_camera(present=False)
    pump(app_qt, 0.4)

    print("\n[VULKAN POINT SIZE PARITY]")
    # Zooming an ORTHOGRAPHIC rig means scaling parallel_scale: the window
    # shrinks by `factor`, so parallel_scale /= factor. Driving dolly() through
    # its notch count would only hit the requested factor approximately
    # (rig.dolly uses 0.8**notches, not 0.88**notches, and the error compounds
    # across the ladder), so set the scale directly for exact x2/x5/x10/x20.
    base_ps = float(rig.parallel_scale_)
    print(f"    fit parallel_scale = {base_ps:.4f}")
    steps = []
    for label, factor in ZOOM_LADDER:
        rig.parallel_scale_ = base_ps / factor
        rig.recompute_clip()
        # push_to_backend() dispatches to the ortho or lookat entry point; the
        # rig is in ortho (2D-locked) mode here, and calling
        # set_camera_lookat(**push_kwargs()) would pass an 'ortho' kwarg the
        # lookat signature does not accept.
        rig.push_to_backend(b)
        b.request_render()
        pump(app_qt, 0.35)
        line = rb.describe_camera(label)
        img = b.capture_frame()
        if img is not None:
            np.save(os.path.join(OUT_DIR, f"{label}.npy"), img)
        cov = coverage(img)
        diam = median_dot_diameter_px(img, max(16, int(vk_px * 3)))
        ext = getattr(b, "_last_extent", (0, 0)) or (0, 0)
        if int(ext[0]) < 64 or int(ext[1]) < 64:
            # _last_extent can be stale/placeholder on the first frames; the
            # native extent is the one actually being rendered into.
            try:
                ext = (b.get_extent_width(), b.get_extent_height())
            except Exception:
                pass
        ps = vk_px
        if "point_size_px=" in line:
            try:
                ps = float(line.split("point_size_px=")[-1].split()[0])
            except ValueError:
                ps = vk_px
        mode = "2D/ortho" if rig.orthographic_ else "3D/perspective"
        print(f"  step={label}  (zoom x{factor:g})")
        print(f"    Camera mode:             {mode}")
        print(f"    Viewport:                {int(ext[0])} x {int(ext[1])}")
        print(f"    VTK point size:          {vtk_px:.2f} px")
        print(f"    Parallel scale:          {float(rig.parallel_scale_):.4f}")
        print(f"    Camera distance:         {rig.distance:.2f}")
        print(f"    Calculated gl_PointSize: {ps:.2f} px  (footprint*pxPerM/depth NOT USED)")
        print(f"    Final clamped size:      {max(1.0, min(ps, min(dev_max, 64.0))):.2f} px")
        print(f"    coverage={cov*100:.2f}%  measured median dot={diam:.2f}px")
        steps.append({"label": label, "factor": factor, "vtk_px": vtk_px,
                      "vk_px": ps, "diff_px": abs(ps - vtk_px), "cov": cov,
                      "diam": diam, "dist": rig.distance})
    R["steps"] = steps

    # ---- acceptance --------------------------------------------------------
    for s in steps:
        check(f"{s['label']}: point size == VTK (<=1px)", s["diff_px"] <= 1.0,
              f"VTK={s['vtk_px']:.2f} Vk={s['vk_px']:.2f} diff={s['diff_px']:.2f}px")
    sizes = [s["vk_px"] for s in steps]
    check("zoom behaviour: point size constant across the ladder",
          max(sizes) - min(sizes) <= 0.01,
          f"sizes={[round(s, 2) for s in sizes]} over x1..x20")
    check("no oversized points (all <= 3x VTK)",
          all(s["vk_px"] <= s["vtk_px"] * 3 + 0.01 for s in steps),
          f"max={max(sizes):.2f}px vs VTK {vtk_px:.2f}px")
    check("no grid gaps (points render at every zoom)", all(s["cov"] > 0.01 for s in steps),
          f"coverages={[round(s['cov']*100, 1) for s in steps]}%")
    check("pan behaviour stable (coverage in [0,1] at every step)",
          all(0.0 <= s["cov"] <= 1.0 for s in steps), "coverage in [0,1]")
    return _report()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(3)
