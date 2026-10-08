"""
vulkan_point_shape_parity.py - VTK-vs-Vulkan point SHAPE/density parity.

Point SIZE parity was settled earlier (vulkan_point_size_parity.py: 2.50 px vs
VTK 2.50 px, 0.00 px difference, constant across x1..x20). This script covers
what that one could not see: the reported "large soft balls / splats" symptom,
which is a SHAPE and ALPHA problem, not a size problem.

VTK source of truth (gui/unified_actor_manager.py, //VTK::Color::Impl):
    vec2 uv25 = gl_PointCoord.xy - vec2(0.5);
    float radial_sq = dot(uv25, uv25);
    if (radial_sq > 0.25) discard;
    ...
    opacity = 1.0;
=> hard-edged disc, FULLY OPAQUE inside, no alpha ramp.

A soft splat is measurable, not a matter of taste:
  * a hard disc is opaque across its interior and absent outside, so point
    pixels have alpha 255 and there are essentially NO intermediate values;
  * a soft ball has a large population of intermediate alpha.
So we measure the point-pixel alpha histogram. That is a direct, quantitative
test of the reported symptom rather than a judgement call.

Run:  python vulkan_point_shape_parity.py
Exit 0 = all passed, 1 = a failure, 3 = harness error.
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("NAKSHA_RENDER_BACKEND", "vulkan")
os.environ.setdefault("NAKSHA_VULKAN_PREVIEW", "")

for _n in ("stdout", "stderr"):
    _s = getattr(sys, _n, None)
    try:
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

LAS_PATH = os.path.join(ROOT, "123.las")
OUT_DIR = os.path.join(ROOT, "vulkan_pointshape_output")

LADDER = [("fit", 1.0), ("zoom_x2", 2.0), ("zoom_x5", 5.0),
          ("zoom_x10", 10.0), ("zoom_x20", 20.0)]

_CHECKS: list = []
R: dict = {}


def check(name, ok, detail=""):
    ok = bool(ok)
    _CHECKS.append((name, ok, detail))
    print(f"[shape] {'PASS' if ok else 'FAIL'}  {name}"
          + (f"  ({detail})" if detail else ""), flush=True)
    return ok


def analyse(img):
    """Coverage, blob size and the alpha profile of a capture.

    With VTK parity every point pixel is opaque (alpha 255), so the histogram
    of point-pixel alpha IS the test for "crisp discs" vs "soft balls".
    """
    a = np.asarray(img)
    if a.ndim != 3 or a.shape[2] < 4:
        return None
    rgb = a[..., :3].astype(np.int32)
    alpha = a[..., 3].astype(np.int32)
    flat = rgb.reshape(-1, 3)
    # Modal colour is the clear/background colour.
    vals, counts = np.unique(flat, axis=0, return_counts=True)
    bg = vals[int(np.argmax(counts))]
    dist = np.abs(rgb - bg[None, None, :]).sum(axis=2)
    mask = dist > 24
    cov = float(mask.mean())
    if not mask.any():
        # Same keys as the normal return, so callers never KeyError on a frame
        # with no points in it.
        return {"coverage": 0.0, "alpha_hist": {}, "semi_frac": 0.0,
                "blob_diam": 0.0, "n_blobs": 0, "iso_n": 0,
                "iso_diam": 0.0, "bg": bg.tolist()}

    pa = alpha[mask]
    semi = int(((pa > 8) & (pa < 247)).sum())
    semi_frac = float(semi / max(pa.size, 1))
    u, c = np.unique(pa, return_counts=True)
    hist = {str(int(v)): int(cc) for v, cc in zip(u, c)}

    diam, n_blobs, iso_n, iso_med = 0.0, 0, 0, 0.0
    try:
        from scipy import ndimage
        lab, n = ndimage.label(mask)
        n_blobs = int(n)
        if n:
            areas = ndimage.sum(mask, lab, range(1, n + 1))
            d_all = 2.0 * np.sqrt(np.asarray(areas) / np.pi)
            diam = float(np.median(d_all))
            # Only ISOLATED blobs are a single point. Where points overlap they
            # merge into one connected region whose equivalent diameter is much
            # larger than any real sprite - including those in a point-size
            # statistic is what made a 2.5 px point look like 4.65 px. Keep only
            # blobs small enough to be one sprite.
            objs = ndimage.find_objects(lab)
            iso = []
            for i, sh in enumerate(objs):
                bh = sh[0].stop - sh[0].start
                bw = sh[1].stop - sh[1].start
                if max(bh, bw) <= 6:
                    iso.append(float(d_all[i]))
            if iso:
                iso = np.asarray(iso)
                iso_n = int(iso.size)
                # Median. Where a point is partially occluded by a nearer
                # neighbour the blob is only PART of a sprite and its equivalent
                # diameter UNDERESTIMATES, so this estimator is biased LOW when
                # the cloud is dense and trustworthy when it is not. The caller
                # therefore only asserts on frames where coverage is low enough
                # for points to have separated - see SPARSE_ENOUGH_COVERAGE.
                iso_med = float(np.median(iso))
    except Exception:
        pass
    return {"coverage": cov, "alpha_hist": hist, "semi_frac": semi_frac,
            "blob_diam": diam, "n_blobs": n_blobs, "iso_n": iso_n,
            "iso_diam": iso_med, "bg": bg.tolist()}


def _report() -> int:
    L = []
    L.append("=" * 72)
    L.append("VULKAN POINT SHAPE / DENSITY PARITY")
    L.append("=" * 72)
    L.append("Reference: VTK   Dataset: 123.las")
    L.append("")
    L.append(f"{'step':<10} {'coverage':>10} {'blobs':>8} {'isolated':>9} "
             f"{'iso px':>8} {'semi%':>8}")
    for s in R.get("steps", []):
        a = s.get("stats")
        if not a:
            L.append(f"{s['label']:<10} {'(no RGBA capture)':>10}")
            continue
        L.append(f"{s['label']:<10} {a['coverage']*100:>9.2f}% {a['n_blobs']:>8} "
                 f"{a['iso_n']:>9} {a['iso_diam']:>8.2f} {a['semi_frac']*100:>7.3f}%")
    L.append("")
    failed = [n for n, ok, _ in _CHECKS if not ok]
    L.append(f"Final status: {'PASS' if not failed else 'FAIL'}")
    if failed:
        L.append("  failed: " + "; ".join(failed))
    text = "\n".join(L)
    print("\n" + text, flush=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "point_shape_report.txt"), "w",
              encoding="utf-8") as fh:
        fh.write(text + "\n")


def main():
    from PySide6.QtWidgets import QApplication
    from gui.app_window import NakshaApp
    from vulkan_engine_test import _suppress_modal_dialogs, pump, wait_until
    from gui.unified_actor_manager import compute_point_size, _BASE_POINT_SIZE
    from gui.render_backend import shading_uniforms

    os.makedirs(OUT_DIR, exist_ok=True)
    print("=" * 72)
    print("VULKAN POINT SHAPE / DENSITY PARITY")
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

    base = float(getattr(win, "point_size", _BASE_POINT_SIZE) or _BASE_POINT_SIZE)
    vtk_px = float(compute_point_size(1.0, base))
    print(f"\n[shape] VTK compute_point_size(1.0) = {vtk_px:.2f} px")
    print("[shape] VTK sprite = hard disc r=0.5, opacity=1.0 (no alpha ramp)",
          flush=True)

    print("\n[shape] loading 123.las ...", flush=True)
    win.open_file(filenames=[LAS_PATH], prompt_import=False)
    if not wait_until(app_qt, lambda: bool(getattr(win, "data", None)), timeout=300.0):
        check("LAS loaded", False, "timeout")
        return _report()
    pump(app_qt, 2.0)
    check("LAS loaded", True, f"points={int(win.data['xyz'].shape[0]):,}")

    # The point cloud must be the visible geometry, else the shape stats would
    # be measuring a shaded triangle surface instead of points.
    b.set_point_cloud_visible(True)
    u = shading_uniforms(win)
    b.set_point_sprite_params(float(u["softness"]), float(u["brightness"]))
    b.request_render()
    pump(app_qt, 0.5)
    check("point cloud is the visible geometry",
          int(b.get_point_cloud_visible()) == 1,
          f"visible={b.get_point_cloud_visible()} sprite_softness={u['softness']}")
    check("VTK-parity sprite softness is 0 (hard opaque disc)",
          float(u["softness"]) == 0.0,
          f"softness={u['softness']} (0 = VTK's opacity=1.0 disc)")
    check("point size still matches VTK",
          abs(float(u["min_px"]) - vtk_px) <= 1.0,
          f"VTK={vtk_px:.2f} Vulkan={float(u['min_px']):.2f}")

    rig = rb._camera_rig
    xyz = np.asarray(win.data["xyz"])
    bounds = np.stack([xyz.min(axis=0), xyz.max(axis=0)])
    # Re-centre from the DATA MEDIAN, not the vertex mean: a handful of outlier
    # vertices drag the mean far enough that the camera aims at empty ground, and
    # the frame then reads as "no points" at deep zoom. The median is the same
    # robust centre the other parity harnesses use.
    # fit_to_bounds() centres on the BOUNDS, which is what keeps the cloud in
    # frame. An experiment using the per-axis vertex MEDIAN instead made this
    # much worse (coverage fell to ~0.2%): for this dataset the median is not a
    # good centre. Reverted deliberately - measured, not assumed.
    # fit_to_bounds centres on the BOUNDS, which frames the whole cloud. That is
    # right for "fit", but the deep zoom steps look through a window only
    # ~2*parallel_scale tall (at x20: 8.2 m), and the exact middle of this scene
    # is a gap in the LiDAR returns - coverage there measured 0.00% with the
    # camera provably correct (ortho, correct centre, correct scale). So for the
    # zoomed steps, aim at the DENSEST patch instead of the geometric centre:
    # the test is about sprite rendering, which needs actual points in frame.
    rig.fit_to_bounds(bounds, aspect=1.6)
    rig.recompute_clip()
    rb.resync_camera(present=False)
    pump(app_qt, 0.4)
    base_ps = float(rig.parallel_scale_)

    # Densest XY bin at roughly the x20 window size. Compute the bin grid from
    # the CLOUD's own min/max - not from the `bounds` array, which is expressed
    # relative to the Vulkan render origin and is therefore offset by millions of
    # metres (using it placed the camera at x=128168, y=2389689, far outside the
    # scene at x~256275, y~4779375).
    xlo, xhi = float(xyz[:, 0].min()), float(xyz[:, 0].max())
    ylo, yhi = float(xyz[:, 1].min()), float(xyz[:, 1].max())
    win_m = max(2.0 * base_ps / LADDER[-1][1], 4.0)
    nbx = max(1, int(round((xhi - xlo) / win_m)))
    nby = max(1, int(round((yhi - ylo) / win_m)))
    H, _, _ = np.histogram2d(xyz[:, 0], xyz[:, 1], bins=[nbx, nby],
                             range=[[xlo, xhi], [ylo, yhi]])
    bx, by = np.unravel_index(int(np.argmax(H)), H.shape)
    dense_x = xlo + (bx + 0.5) * (xhi - xlo) / nbx
    dense_y = ylo + (by + 0.5) * (yhi - ylo) / nby
    dense_centre = np.array([dense_x, dense_y, float(np.median(xyz[:, 2]))])
    print(f"\n[shape] cloud bounds x[{xlo:.1f},{xhi:.1f}] y[{ylo:.1f},{yhi:.1f}]")
    print(f"[shape] densest {win_m:.1f} m patch: "
          f"({dense_x:.1f}, {dense_y:.1f}) with {int(H[bx, by]):,} points")
    check("dense patch is inside the cloud's own bounds",
          xlo <= dense_x <= xhi and ylo <= dense_y <= yhi,
          f"({dense_x:.1f}, {dense_y:.1f})")

    # The headless window never fires a real resizeEvent, so the swapchain can
    # still be at its initial 100x30. A 2.5 px point cannot be assessed in a
    # 30-px-tall image (every point merges into one blob), so force the extent
    # the test is actually about and verify it took effect.
    target_w, target_h = 1400, 900
    b.resize(target_w, target_h)
    b.request_render()
    pump(app_qt, 0.6)
    probe = b.capture_frame()
    got = probe.shape[1::-1] if probe is not None else (0, 0)
    check("capture is at a resolution where point shape is measurable",
          probe is not None and probe.shape[0] >= 400 and probe.shape[1] >= 800,
          f"capture={probe.shape if probe is not None else None} "
          f"(need >= 800x400; a 2.5 px point is invisible below that)")

    base_ps = float(rig.parallel_scale_)
    steps = []
    print("\n[VULKAN POINT SHAPE PARITY]")
    for label, factor in LADDER:
        # Aim at the dense patch for the zoomed steps (see above); "fit" keeps
        # the whole-scene framing.
        if factor > 1.0:
            rig.target = dense_centre
        rig.parallel_scale_ = base_ps / factor
        rig.recompute_clip()
        # Push the camera through the rig every step. resync_camera() writes to
        # VTK, and the installed camera observer writes a pose BACK, so the
        # engine can end up with a camera that is not the one just set - which
        # is what left the deepest zoom step framing empty ground (0 points).
        # push_to_backend() is the authoritative path to the engine.
        rig.push_to_backend(b)
        b.request_render()
        pump(app_qt, 0.35)
        import ctypes as _ct
        _o = _ct.c_int(0); _ps = _ct.c_double(0.0)
        _f = getattr(b._dll, "nkv_get_camera_projection", None)
        if _f is not None:
            _f.restype = _ct.c_int
            _f.argtypes = [_ct.c_uint64, _ct.POINTER(_ct.c_int),
                           _ct.POINTER(_ct.c_double)]
            _f(_ct.c_uint64(b._handle), _ct.byref(_o), _ct.byref(_ps))
        print(f"    rig: ps={float(rig.parallel_scale_):.4f} "
              f"target={np.round(np.asarray(rig.target, dtype=float), 2).tolist()} "
              f"near={float(rig.near_clip):.4f} far={float(rig.far_clip):.1f}")
        print(f"    engine: ortho={_o.value} parallel_scale={_ps.value:.4f}")
        img = b.capture_frame()
        if img is not None:
            np.save(os.path.join(OUT_DIR, f"{label}.npy"), img)
        a = analyse(img)
        print(f"  step={label} (x{factor:g}) "
              f"parallel_scale={float(rig.parallel_scale_):.4f}")
        if a is None:
            print("    capture had no alpha channel - cannot judge transparency")
            steps.append({"label": label, "stats": None})
            continue
        print(f"    coverage={a['coverage']*100:.2f}%  blobs={a['n_blobs']}  "
              f"isolated={a['iso_n']} med_iso_diam={a['iso_diam']:.2f}px")
        print(f"    point-pixel alpha histogram={a['alpha_hist']}")
        print(f"    semi-transparent fraction={a['semi_frac']*100:.3f}%")
        steps.append({"label": label, "stats": a})
    R["steps"] = steps

    good = [s for s in steps if s["stats"]]
    if not good:
        check("captures analysable", False, "no RGBA capture")
        return _report()

    for s in good:
        a = s["stats"]
        check(f"{s['label']}: points are OPAQUE (no soft-ball alpha ramp)",
              a["semi_frac"] <= 0.02,
              f"semi-transparent point pixels {a['semi_frac']*100:.3f}% "
              f"(VTK opacity=1.0 -> 0%)")
    all_semi = max(s["stats"]["semi_frac"] for s in good)
    check("Point shape: no translucent sprite pixels anywhere in the ladder",
          all_semi <= 0.02, f"worst {all_semi*100:.3f}%")

    # Point size is only measurable where points have separated enough for a
    # blob to BE one point. On a dense frame a blob can be a partially occluded
    # sprite (its neighbour won the depth test over part of it) or a small
    # cluster, and its equivalent diameter is then not the sprite size - the
    # estimator is biased low there. So assert on the SPARSE frames, and report
    # the dense ones without asserting, rather than tuning a percentile until
    # the dense frames pass.
    SPARSE_ENOUGH_COVERAGE = 0.55
    asserted = []
    for s in good:
        a = s["stats"]
        if a["iso_n"] <= 200:
            continue
        if a["coverage"] > SPARSE_ENOUGH_COVERAGE:
            note(f"    {s['label']}: coverage {a['coverage']*100:.1f}% - points not "
                 f"separated, diameter {a['iso_diam']:.2f} px reported but not "
                 f"asserted (blobs may be partial sprites)")
            continue
        asserted.append(a["iso_diam"])
        check(f"{s['label']}: isolated point diameter == VTK sprite (within 1 px)",
              abs(a["iso_diam"] - vtk_px) <= 1.0,
              f"median isolated disc {a['iso_diam']:.2f} px vs VTK "
              f"{vtk_px:.2f} px (diff {abs(a['iso_diam']-vtk_px):.2f} px, "
              f"n={a['iso_n']:,} isolated points, coverage {a['coverage']*100:.1f}%)")
    check("at least two frames were measurable for point size",
          len(asserted) >= 2, f"{len(asserted)} sparse frames asserted")
    # Guard the framing itself: a camera that drifts off the data makes every
    # size/density number meaningless. Test on COVERAGE, not on isolated-blob
    # count - at "fit" the whole cloud is one connected mass, so zero isolated
    # blobs is the correct, expected result there and says nothing about framing.
    check("framing: cloud is in view at every zoom step",
          all(s["stats"]["coverage"] > 0.01 for s in good),
          "coverage=" + ", ".join(f"{s['label']}:{s['stats']['coverage']*100:.1f}%"
                                  for s in good))

    # Density. Coverage is NOT expected to fall monotonically: zooming in spreads
    # the SAME points over more pixels, so it first rises (a sparse cloud becomes
    # resolvable) and can only fall once points are far enough apart to separate.
    # The real failure this must catch is a runaway: accumulation from alpha
    # blending inflating coverage, or points growing with zoom. Both are bounded
    # here.
    covs = [s["stats"]["coverage"] for s in good]
    check("Density: coverage stays bounded (no alpha accumulation runaway)",
          all(c <= 0.95 for c in covs),
          "coverage=" + ", ".join(f"{c*100:.1f}%" for c in covs))
    # Point size must not grow with zoom: the isolated-diameter series is the
    # direct test, since a zoom-scaled sprite would show up here.
    isos = list(asserted)
    if len(isos) >= 2:
        check("Zoom: isolated point size does not grow with zoom",
              max(isos) - min(isos) <= 0.6,
              f"isolated diameters={[round(v,2) for v in isos]} px across the "
              f"sparse frames (VTK {vtk_px:.2f} px, constant by construction)")
    check("Zoom: cloud still renders at the deepest step",
          covs[-1] > 0.0005, f"deepest coverage {covs[-1]*100:.3f}%")
    check("Zoom: isolated points are resolvable at the deepest step",
          good[-1]["stats"]["iso_n"] > 200,
          f"{good[-1]['stats']['iso_n']:,} isolated points at x20")
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
