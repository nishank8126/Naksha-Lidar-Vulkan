"""[MICROCELL CAMERA TEST] - Phase A/C measurement on 26.96M.

The question this answers: with storage granularity and render granularity
separated, does a SMALL camera region now touch a small spatial working set?

Old (page-granular culling):  x32 -> 39 physical pages, ~17.7% of source.
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.naksha_cache.reader import NakshaPointCacheReader   # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "test_classified_highprecision.laz")
TOTAL = 26_960_750


def main():
    print("\n" + "=" * 76)
    print("[POINT SPATIAL GRANULARITY]")
    print("=" * 76)
    r = NakshaPointCacheReader(SRC, verify_crc=True)
    n = r.index.nodes
    pc = np.asarray(n["point_count"])
    ext = np.hypot(np.asarray(n["bounds_max"])[:, 0] -
                   np.asarray(n["bounds_min"])[:, 0],
                   np.asarray(n["bounds_max"])[:, 1] -
                   np.asarray(n["bounds_min"])[:, 1])
    print(f"  physical pages : {n.size}")
    print(f"    median points {int(np.median(pc)):,}   "
          f"median extent {np.median(ext):.0f} m")
    ms = r.microcell_stats()
    if ms is None:
        print("  render microcells: NONE")
        r.close()
        return 1
    print(f"  render microcells: {ms['count']:,}")
    print(f"    median points {ms['pts_median']:,.0f}   p95 {ms['pts_p95']:,.0f}")
    print(f"    median extent {ms['ext_median']:.0f} m   "
          f"p95 {ms['ext_p95']:.0f} m   max {ms['ext_max']:.0f} m")
    print(f"  index size     : {os.path.getsize(r.index.path):,} B "
          f"(+{os.path.getsize(r.index.path) / 431636:.2f}x with microcells)")

    gmin = np.asarray(n["bounds_min"]).min(0)
    gmax = np.asarray(n["bounds_max"]).max(0)
    span = gmax - gmin
    cx, cy = (gmin[0] + gmax[0]) * 0.5, (gmin[1] + gmax[1]) * 0.5

    print("\n" + "=" * 76)
    print("[MICROCELL CAMERA TEST]  LOD0")
    print("=" * 76)
    print(f"  {'zoom':<7}{'view m':>9}{'pages':>7}{'microcells':>12}"
          f"{'pts drawn':>13}{'% src':>9}{'decode ms':>11}")
    for label, frac in [("fit", 1.0), ("x2", 0.5), ("x4", 0.25),
                        ("x8", 0.125), ("x16", 0.0625),
                        ("x32", 0.03125)]:
        hw = span[0] * 0.5 * frac
        hh = span[1] * 0.5 * frac
        x0, x1 = cx - hw, cx + hw
        y0, y1 = cy - hh, cy + hh
        nb = np.asarray(n["bounds_min"])
        xb = np.asarray(n["bounds_max"])
        pages = [i for i in range(n.size)
                 if nb[i][0] <= x1 and xb[i][0] >= x0
                 and nb[i][1] <= y1 and xb[i][1] >= y0]
        cells = r.visible_microcells(x0, y0, x1, y1, lod=0)
        t0 = time.perf_counter()
        drawn = 0
        for ci in cells:
            t = r.read_microcell(ci)
            if t:
                drawn += t["point_count"]
        ms = (time.perf_counter() - t0) * 1000
        print(f"  {label:<7}{hw * 2:>9.0f}{len(pages):>7}{len(cells):>12}"
              f"{drawn:>13,}{drawn / TOTAL * 100:>8.2f}%{ms:>11.1f}")

    # x32 detail: the case that used to be the blocker.
    hw = span[0] * 0.5 * 0.03125
    hh = span[1] * 0.5 * 0.03125
    cells = r.visible_microcells(cx - hw, cy - hh, cx + hw, cy + hh, lod=0)
    nb = np.asarray(n["bounds_min"])
    xb = np.asarray(n["bounds_max"])
    pages = [i for i in range(n.size)
             if nb[i][0] <= cx + hw and xb[i][0] >= cx - hw
             and nb[i][1] <= cy + hh and xb[i][1] >= cy - hh]
    print("\n" + "=" * 76)
    print("[x32 31 m VIEW]")
    print("=" * 76)
    print(f"  physical pages intersected : {len(pages)}")
    print(f"  microcells intersected     : {len(cells)}")
    tot = sum(int(r.index.microcells[c]['point_count']) for c in cells)
    print(f"  points decoded             : {tot:,} "
          f"({tot / TOTAL * 100:.3f}% of source)")
    if cells:
        ex = [float(r.index.microcells[c]['spacing']) for c in cells[:5]]
        print(f"  sample microcell spacing   : "
              f"{[round(v, 2) for v in ex]} m")
    r.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
