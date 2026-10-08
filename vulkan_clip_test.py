"""vulkan_clip_test.py - camera CLIP BOX regression test (no window, no GPU).

Why this exists: the 3D leg of vulkan_resize_test.py failed with an all-black
viewport - engine readback AND real screen grab both at std 0.25 - while every
pose number looked perfect. describe_camera() showed the reason:

    projection=PERSPECTIVE dist=5000.000 near=5000.0000 far=5001.00
    depth=[4946.59,5075.90] pts_clipped_near=62600 pts_clipped_far=136311

A one-metre frustum around a 129-metre cloud: 99.5% of the sampled points sat
outside near/far. The cause was resync_camera() assigning VTK's clipping range
over the rig's own freshly derived box, and VTK's range in the main viewport is
a stale/degenerate [5000, 5001] because _mirror_rig_to_vtk() had written almost
that same box back into it - source and sink of the same bad value.

These checks pin the contract that fix installed, on the pure camera rig, so it
is covered in milliseconds without Qt, a swapchain or a 3M-point LAS:

  1. VTK's degenerate range is REJECTED and the rig's own box is kept.
  2. Whatever the pose, near/far always BRACKET the actual points.
  3. A genuinely wider VTK range is still adopted (the rig does not hog).
  4. Zooming, orbiting, panning and switching 2D <-> 3D never break 2.
  5. fit_to_bounds keeps working for both projections.

Usage:  python vulkan_clip_test.py        (exit 0 = all checks pass)
"""
from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import numpy as np

from gui.render_backend import _VulkanCameraRig

_CHECKS: list[tuple[str, bool, str]] = []

# The real scene from the failing run: 123.las near (256275, 4779375, 294),
# a ~400 x 400 x 130 m box, viewed from 5000 m out.
CENTRE = np.array([256475.96, 4779553.44, 440.09], dtype=np.float64)
HALF = np.array([200.0, 200.0, 65.0], dtype=np.float64)


def check(name: str, ok: bool, detail: str = "") -> bool:
    ok = bool(ok)
    _CHECKS.append((name, ok, detail))
    print(f"[clip] {'PASS' if ok else 'FAIL'}  {name}"
          + (f"  ({detail})" if detail else ""), flush=True)
    return ok


def make_cloud(n: int = 20000) -> np.ndarray:
    rng = np.random.default_rng(7)
    lo, hi = CENTRE - HALF, CENTRE + HALF
    return rng.uniform(lo, hi, size=(n, 3))


def depth_span(rig: _VulkanCameraRig, pts: np.ndarray) -> tuple[float, float]:
    """(nearest, farthest) point depth in view space - exactly the depth
    gl_Position.w carries, and the same measurement describe_camera() does."""
    eye, target = rig.eye(), np.asarray(rig.target, dtype=np.float64)
    fwd = target - eye
    fwd = fwd / np.linalg.norm(fwd)
    d = (pts.astype(np.float64) - eye) @ fwd
    return float(d.min()), float(d.max())


def ortho_depth_span(rig: _VulkanCameraRig, pts: np.ndarray) -> tuple[float, float]:
    """For an ortho camera the eye position is arbitrary; only the distance
    along view_dir measured from the CENTRE matters, because the engine builds
    the box around the focal point at the nominal distance."""
    d = np.asarray(rig.view_dir_, dtype=np.float64)
    d = d / np.linalg.norm(d)
    proj = (pts.astype(np.float64) - np.asarray(rig.target, dtype=np.float64)) @ d
    # Looking ALONG view_dir, so a point behind the centre (proj negative) is
    # the one nearer the eye. Ascending order = (nearest, farthest).
    return float(rig.distance + proj.min()), float(rig.distance + proj.max())


def brackets(rig: _VulkanCameraRig, pts: np.ndarray) -> tuple[bool, str]:
    if rig.orthographic_:
        lo, hi = ortho_depth_span(rig, pts)
    else:
        lo, hi = depth_span(rig, pts)
    ok = rig.near_clip < lo and rig.far_clip > hi and rig.far_clip > rig.near_clip
    return ok, (f"near={rig.near_clip:.2f} far={rig.far_clip:.2f} "
                f"data=[{lo:.2f},{hi:.2f}] src={rig.clip_source_}")


def plan_rig(pts: np.ndarray) -> _VulkanCameraRig:
    """The rig as the APP leaves it in 2D: a parallel VTK camera adopted at a
    5000 m standoff - which is what resync_camera() does, not fit_to_bounds()."""
    rig = _VulkanCameraRig()
    rig.set_data_extent(CENTRE - HALF, CENTRE + HALF)
    view_dir = np.array([0.0, 0.0, -1.0])
    eye = CENTRE - view_dir * 5000.0            # eye 5000 m above, looking down
    rig.set_eye_target(eye, CENTRE, 30.0, parallel_scale=52.7965)
    return rig


def main() -> int:
    pts = make_cloud()

    # ---- 1. the exact regression -----------------------------------------
    rig = plan_rig(pts)
    rejected = not rig.adopt_vtk_clip(5000.0, 5001.0)
    check("degenerate VTK range [5000,5001] is rejected", rejected,
          f"near={rig.near_clip:.2f} far={rig.far_clip:.2f} src={rig.clip_source_}")
    ok, det = brackets(rig, pts)
    check("2D after rejection: near/far bracket the cloud", ok, det)

    # ---- 2. the 3D leg: Shift+P flips the SAME rig to perspective ---------
    eye3d = CENTRE + np.array([-3339.11, 1104.30, 3554.00])
    rig.set_eye_target(eye3d, CENTRE, 30.0)     # no parallel_scale => PERSP
    check("projection flipped to perspective", not rig.orthographic_)
    rejected = not rig.adopt_vtk_clip(5000.0, 5001.0)
    check("3D: stale VTK range rejected after the flip", rejected,
          f"near={rig.near_clip:.2f} far={rig.far_clip:.2f} src={rig.clip_source_}")
    ok, det = brackets(rig, pts)
    check("3D: near/far bracket the cloud (the std=0.25 black viewport)", ok, det)

    # ---- 3. a trustworthy VTK range is still honoured ---------------------
    before = (rig.near_clip, rig.far_clip)
    wide = rig.adopt_vtk_clip(rig.distance - 4000.0, rig.distance + 4000.0)
    check("wide VTK range is adopted", wide,
          f"near={rig.near_clip:.2f} far={rig.far_clip:.2f} src={rig.clip_source_}")
    check("adoption actually changed the box", (rig.near_clip, rig.far_clip) != before)
    ok, det = brackets(rig, pts)
    check("adopted box still brackets the cloud", ok, det)

    # ---- 4. gestures never strand the data outside the box ----------------
    worst = ""
    for notch in range(-14, 15):
        r = plan_rig(pts)
        r.adopt_vtk_clip(5000.0, 5001.0)        # the bad candidate, rejected
        r.dolly(notch)
        ok, det = brackets(r, pts)
        if not ok:
            worst = f"dolly {notch:+d}: {det}"
            break
    check("zoom +-14 notches keeps the cloud inside", not worst, worst)

    rig = _VulkanCameraRig()
    rig.set_data_extent(CENTRE - HALF, CENTRE + HALF)
    rig.set_eye_target(CENTRE + np.array([0.0, 0.0, 5000.0]), CENTRE, 30.0)
    ok, det = True, ""
    for step in range(1, 40):
        rig.orbit(step * 6.0, -step * 4.0)
        ok, det = brackets(rig, pts)
        if not ok:
            break
    check("orbit keeps the cloud inside near/far", ok, det)
    for step in range(1, 40):
        rig.pan(step * 60.0, -step * 45.0, 900)
        ok, det = brackets(rig, pts)
        if not ok:
            break
    check("pan far away keeps the cloud inside near/far", ok, det)

    rig = plan_rig(pts)                          # 2D: ortho pan moves CENTRE
    rig.pan(9000.0, 9000.0, 900)
    ok, det = brackets(rig, pts)
    check("2D pan far away keeps the cloud inside near/far", ok, det)

    # ---- 5. fit_to_bounds on both projections -----------------------------
    lo, hi = CENTRE - HALF, CENTRE + HALF
    r = _VulkanCameraRig()
    r.set_eye_target(CENTRE + np.array([0.0, 0.0, 5000.0]), CENTRE, 30.0,
                     parallel_scale=52.0)
    r.fit_to_bounds(np.stack([lo, hi]), aspect=1.6)
    ok, det = brackets(r, pts)
    check("fit_to_bounds (ortho) brackets the cloud", ok, det)
    check("fit_to_bounds records the data centre",
          r.scene_center_ is not None
          and float(np.linalg.norm(r.scene_center_ - CENTRE)) < 1e-6,
          f"centre={None if r.scene_center_ is None else r.scene_center_.round(2)}")
    r = _VulkanCameraRig()
    r.orthographic_ = False
    r.fit_to_bounds(np.stack([lo, hi]), aspect=1.6)
    ok, det = brackets(r, pts)
    check("fit_to_bounds (persp) brackets the cloud", ok, det)

    # ---- 6. garbage in, sane box out --------------------------------------
    bad_detail = ""
    for bad in [(0.0, 0.0), (-5.0, -1.0), (5000.0, 4000.0), (float("nan"), 1e9),
                (None, None), (5000.0, 5000.0)]:
        r = plan_rig(pts)
        r.adopt_vtk_clip(*bad)
        if not (r.far_clip > r.near_clip > 0.0):
            bad_detail = f"{bad} -> near={r.near_clip} far={r.far_clip}"
            break
    check("bad/garbage VTK ranges always fall back to an ordered box",
          not bad_detail, bad_detail)

    # ---- 7. nothing loaded yet must not produce a degenerate box ----------
    r = _VulkanCameraRig()
    r.set_eye_target(CENTRE + np.array([0.0, 0.0, 5000.0]), CENTRE, 30.0,
                     parallel_scale=52.7965)
    check("empty scene: ortho box is not near==far", r.far_clip > r.near_clip,
          f"near={r.near_clip:.2f} far={r.far_clip:.2f}")
    r.set_eye_target(CENTRE + np.array([0.0, 0.0, 5000.0]), CENTRE, 30.0)
    check("empty scene: perspective box straddles the data",
          r.far_clip > r.near_clip and r.near_clip < 5000.0 < r.far_clip,
          f"near={r.near_clip:.2f} far={r.far_clip:.2f}")

    failed = [c for c in _CHECKS if not c[1]]
    print(f"[clip] {len(_CHECKS) - len(failed)}/{len(_CHECKS)} checks passed",
          flush=True)
    for name, _ok, detail in failed:
        print(f"[clip]   FAILED: {name}" + (f"  ({detail})" if detail else ""),
              flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

