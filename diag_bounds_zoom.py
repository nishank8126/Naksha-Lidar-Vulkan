"""Why does zooming in not refine LOD?

Measures, on the real 27M cache:
  * node AABB geometry vs the dataset extent (are bounds inflated?)
  * how many nodes the production visibility test returns at each zoom
  * the PRE-budget "ideal" LOD vs the POST-budget chosen LOD per zoom
  * stored lod_spacing per rung, to see if the ladder has usable rungs

If the visible set is identical at FIT and x16, the budget share is
scale-invariant and zoom cannot change the answer -- that is the defect.
"""
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "test_classified_highprecision.laz")
sys.path.insert(0, ROOT)

from accept_stored_normal_shaded import _build                # noqa: E402
from gui.naksha_cache.reader import NakshaPointCacheReader    # noqa: E402
from gui.naksha_cache.normal_streaming import (               # noqa: E402
    ScreenDensityBudget, open_normal_cache)
from naksha_lod_gate import Camera2D, ScreenSpaceLOD          # noqa: E402

LADDER = [("FIT", 1.0), ("x2", 0.5), ("x4", 0.25), ("x8", 0.125),
          ("x16", 0.0625), ("x32", 0.03125)]


def wmean(sel):
    n = sum(int(o["points"]) for o in sel)
    if not n:
        return 0.0
    return sum(int(o["lod"]) * int(o["points"]) for o in sel) / n


def ideal_wmean(lod, index, cam, vis):
    """Run only the PRE-budget pass by asking for an enormous budget."""
    keep = lod.budget
    lod.budget = 10 ** 12
    sel = lod.select(index, cam, vis)
    lod.budget = keep
    return wmean(sel), sel


def main():
    reader = NakshaPointCacheReader(SRC, verify_crc=False, load_edits=False)
    nreader, nrep = open_normal_cache(SRC, verify_source=False,
                                      expected_source_points=26960750)
    mgr = _build(reader, nreader, nrep)
    nodes = mgr.idx.nodes
    bmin = np.asarray(nodes["bounds_min"], float)
    bmax = np.asarray(nodes["bounds_max"], float)

    hdr = reader.index.header
    gmin = np.asarray(hdr["bounds_min"], float)
    gmax = np.asarray(hdr["bounds_max"], float)
    ds_area = float((gmax[0] - gmin[0]) * (gmax[1] - gmin[1]))
    node_area = float(np.sum((bmax[:, 0] - bmin[:, 0]) *
                             (bmax[:, 1] - bmin[:, 1])))
    w = bmax[:, 0] - bmin[:, 0]
    h = bmax[:, 1] - bmin[:, 1]

    print("[BOUNDS]")
    print(f"  nodes                : {nodes.size}")
    print(f"  dataset XY area      : {ds_area:,.0f} m^2")
    print(f"  sum(node XY area)    : {node_area:,.0f} m^2  "
          f"= {node_area / max(ds_area, 1e-9):.2f}x dataset")
    print(f"  node width  min/p50/max : {w.min():.3f} / "
          f"{np.median(w):.3f} / {w.max():.3f} m")
    print(f"  node height min/p50/max : {h.min():.3f} / "
          f"{np.median(h):.3f} / {h.max():.3f} m")
    # pairwise AABB overlap (393 nodes -> 77k pairs, fine)
    ox = np.maximum(0.0, np.minimum(bmax[:, None, 0], bmax[None, :, 0])
                    - np.maximum(bmin[:, None, 0], bmin[None, :, 0]))
    oy = np.maximum(0.0, np.minimum(bmax[:, None, 1], bmax[None, :, 1])
                    - np.maximum(bmin[:, None, 1], bmin[None, :, 1]))
    ov = (ox > 1e-9) & (oy > 1e-9)
    np.fill_diagonal(ov, False)
    print(f"  overlapping node pairs: {int(ov.sum())}")

    cx, cy = (gmin[0] + gmax[0]) * .5, (gmin[1] + gmax[1]) * .5
    span_x = float(gmax[0] - gmin[0])

    print("\n[FOOTPRINT vs SCREEN]")
    for label, frac in LADDER:
        cam = Camera2D(cx, cy, span_x * .5 * frac, 1920, 848)
        vis = mgr._visible_node_rows(cam, None)
        wpx = w * cam.px_per_m()
        hpx = h * cam.px_per_m()
        proj = wpx * hpx
        screen = 1920 * 848
        print(f"  {label:<5} vis={len(vis):>4}  "
              f"sum_proj/viewport={proj.sum() / screen:>9.1f}x  "
              f"max_node_proj={proj.max() / screen:>8.1f}x")

    print("\n[LOD RESPONSE TO ZOOM]  (1920x848 IDLE)")
    print(f"  {'zoom':<6}{'vis':>5}{'ideal_w':>10}{'final_w':>10}"
          f"{'final_pts':>12}{'target':>12}{'LOD dist'}")
    for label, frac in LADDER:
        cam = Camera2D(cx, cy, span_x * .5 * frac, 1920, 848)
        vis = mgr._visible_node_rows(cam, None)
        lod = ScreenSpaceLOD()
        ScreenDensityBudget().apply(lod, 1920, 848, moving=False)
        iw, isel = ideal_wmean(lod, mgr.idx, cam, vis)
        lod2 = ScreenSpaceLOD()
        ScreenDensityBudget().apply(lod2, 1920, 848, moving=False)
        sel = lod2.select(mgr.idx, cam, vis)
        dist = {}
        for o in sel:
            dist[o["lod"]] = dist.get(o["lod"], 0) + 1
        ds = " ".join(f"L{k}:{v}" for k, v in sorted(dist.items()))
        print(f"  {label:<6}{len(vis):>5}{iw:>10.1f}{wmean(sel):>10.1f}"
              f"{sum(int(o['points']) for o in sel):>12,}"
              f"{lod2.budget:>12,}  {ds}")

    print("\n[LADDER RUNGS] first 4 nodes")
    for i in range(4):
        nc = nodes["lod_point_count"][i]
        sp = nodes["lod_spacing"][i] if "lod_spacing" in nodes.dtype.names \
            else [0] * len(nc)
        rungs = [(l, int(c), round(float(s), 4))
                 for l, (c, s) in enumerate(zip(nc, sp)) if int(c) > 0]
        print(f"  node {int(nodes['node_id'][i]):>4} "
              f"bounds={tuple(round(v, 1) for v in bmin[i][:2])}"
              f"..{tuple(round(v, 1) for v in bmax[i][:2])}")
        for l, c, s in rungs:
            print(f"      LOD{l}: {c:>9,} pts  spacing {s} m")
    reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
