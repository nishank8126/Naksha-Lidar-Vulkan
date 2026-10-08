"""What shape are the leaves actually?

Prints the leaf AABB population of the real 27M index so the next step is
chosen from evidence, not from a story about the builder.
"""
import os
import sys
from collections import Counter

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from gui.naksha_cache.reader import NakshaPointCacheReader   # noqa: E402

SRC = os.path.join(ROOT, "test_classified_highprecision.laz")


def main():
    r = NakshaPointCacheReader(SRC, verify_crc=False, load_edits=False)
    n = r.index.nodes
    bmin = np.asarray(n["bounds_min"], float)
    bmax = np.asarray(n["bounds_max"], float)
    w = bmax[:, 0] - bmin[:, 0]
    h = bmax[:, 1] - bmin[:, 1]
    pc = np.asarray(n["point_count"], float)

    hdr = r.index.header
    gmin = np.asarray(hdr["bounds_min"], float)
    gmax = np.asarray(hdr["bounds_max"], float)
    print("[DATASET]")
    print(f"  bounds {tuple(round(v, 2) for v in gmin)} .. "
          f"{tuple(round(v, 2) for v in gmax)}")
    names = hdr.dtype.names or ()
    for key in ("source_point_count", "total_points", "point_count"):
        if key in names:
            print(f"  points {int(hdr[key]):,}  ({key})")
            break
    print(f"  nodes  {n.size}")

    print("\n[LEAF WIDTH BUCKETS]")
    buckets = Counter()
    for wi, hi in zip(w, h):
        buckets[(round(wi), round(hi))] += 1
    for (wi, hi), c in buckets.most_common(12):
        print(f"  {wi:>5} x {hi:<5} m : {c:>4} leaves")

    print("\n[WIDTH HISTOGRAM]")
    for lo, hi in [(0, 5), (5, 20), (20, 50), (50, 100), (100, 200),
                   (200, 500), (500, 2000)]:
        m = (w >= lo) & (w < hi)
        if m.any():
            print(f"  {lo:>4}-{hi:<5} m : {int(m.sum()):>4} leaves  "
                  f"pts p50={np.median(pc[m]):>10,.0f}")

    print("\n[FIRST 8 LEAVES]")
    for i in range(8):
        print(f"  node {int(n['node_id'][i]):>4}  "
              f"({bmin[i][0]:.1f},{bmin[i][1]:.1f}).."
              f"({bmax[i][0]:.1f},{bmax[i][1]:.1f})  "
              f"{w[i]:.1f}x{h[i]:.1f} m  {int(pc[i]):,} pts")

    # Do the leaves tile the dataset, or do many leaves cover the same ground?
    xs0, xs1 = bmin[:, 0], bmax[:, 0]
    order = np.argsort(xs0)
    cover = np.zeros(200, float)
    lo, hi = gmin[0], gmax[0]
    for i in order:
        a = int(np.clip((xs0[i] - lo) / (hi - lo) * 200, 0, 199))
        b = int(np.clip((xs1[i] - lo) / (hi - lo) * 200, 0, 199))
        cover[a:b + 1] += 1
    print("\n[X-AXIS COVERAGE DEPTH] (how many leaves cover each 1/200 slice)")
    print(f"  min={cover.min():.0f} p50={np.median(cover):.0f} "
          f"max={cover.max():.0f}")

    print("\n[POINT COUNT]")
    print(f"  total {int(pc.sum()):,}  min={int(pc.min()):,}  "
          f"p50={np.median(pc):,.0f}  max={int(pc.max()):,}")
    r.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
