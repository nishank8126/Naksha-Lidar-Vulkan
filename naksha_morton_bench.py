"""[MORTON LOCALITY BENCHMARK] - pick the intra-page ordering by measurement.

Real points from the 26.96M source, cut to one representative physical page
(Part 4: bounded dataset, reorder ONLY intra-page). Everything else - page
partitioning, point targets, SoA layout, LOD architecture - is held fixed.

Metrics (Part 5/6): consecutive point step, subrange compactness at 1/4 .. 1/32,
and microcell quality including the pathological ~1414 m outliers.
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.naksha_cache import morton as MO        # noqa: E402
from gui.naksha_cache.microcells import build_microcells   # noqa: E402

SRC = r"H:\TESTING CONTIUES\FUNIVIA\NT219\MANDI_53.laz"
VARIANTS = ["XYZ16", "XYZ21", "XY16", "XY21", "XY24", "XY28"]
FRACTIONS = [4, 8, 12, 16, 32]


def load_page(n=2_000_000):
    """One representative page-sized region of real LiDAR."""
    import laspy
    X, Y, Z = [], [], []
    got = 0
    with laspy.open(SRC) as f:
        while got < n:
            c = f.read_points(min(500_000, n - got))
            if len(c) == 0:
                break
            X.append(np.asarray(c.x, np.float64))
            Y.append(np.asarray(c.y, np.float64))
            Z.append(np.asarray(c.z, np.float64))
            got += len(X[-1])
    return np.concatenate(X), np.concatenate(Y), np.concatenate(Z)

def local_of(order, x, y, z, origin, scale):
    """LOCAL int32 coordinates relative to the page origin/scale.

    Passing WORLD coords here dequantised to nonsense - the benchmark reported
    5572 m microcells and collapsed every variant to a single cell.
    """
    return np.column_stack(((x[order] - origin[0]) / scale[0],
                            (y[order] - origin[1]) / scale[1],
                            (z[order] - origin[2]) / scale[2])).astype(np.int32)

def metrics(x, y, order):
    xs, ys = x[order], y[order]
    d = np.hypot(np.diff(xs), np.diff(ys))
    step_med = float(np.median(d))
    step_p95 = float(np.percentile(d, 95))
    n = xs.size
    full = float(np.hypot(xs.max() - xs.min(), ys.max() - ys.min()))
    comp = {}
    for f in FRACTIONS:
        step = n // f
        exts = []
        for i in range(0, n - step, step):
            s = slice(i, i + step)
            exts.append(float(np.hypot(xs[s].max() - xs[s].min(),
                                       ys[s].max() - ys[s].min())))
        ideal = full / np.sqrt(f)
        comp[f] = (float(np.median(exts)), float(np.percentile(exts, 95)),
                   float(np.median(exts)) / ideal)
    return step_med, step_p95, full, comp


def microcells_for(order, x, y, z, origin, scale, local):
    rows = build_microcells(0, 0, local, origin, scale,
                            target_points=12000, max_extent=45.0,
                            min_points=2000)
    if len(rows) == 0:
        return None
    ext = np.hypot(rows["bounds_max"][:, 0] - rows["bounds_min"][:, 0],
                   rows["bounds_max"][:, 1] - rows["bounds_min"][:, 1])
    return {"count": int(len(rows)),
            "pts_med": float(np.median(rows["point_count"])),
            "ext_med": float(np.median(ext)),
            "ext_p95": float(np.percentile(ext, 95)),
            "ext_max": float(ext.max()),
            "gt64": int((ext > 64).sum()), "gt128": int((ext > 128).sum()),
            "gt256": int((ext > 256).sum()), "gt500": int((ext > 500).sum()),
            "gt1000": int((ext > 1000).sum())}


def main():
    x, y, z = load_page()
    print("\n" + "=" * 78)
    print("[MORTON LOCALITY BENCHMARK]")
    print("=" * 78)
    print(f"  page sample: {x.size:,} real points from {os.path.basename(SRC)}")
    print(f"  extent: {x.max() - x.min():.0f} m x {y.max() - y.min():.0f} m, "
          f"z {z.max() - z.min():.0f} m")
    # Page-local quantisation anchor.
    origin = (float(x.min()), float(y.min()), float(z.min()))
    scale = (float(x.max() - x.min()) or 1.0, float(y.max() - y.min()) or 1.0,
             float(z.max() - z.min()) or 1.0)

    print(f"\n  {'variant':<8}{'sort s':>9}{'step med':>10}{'step p95':>10}"
          f"{'1/8 ext':>10}{'1/8 ideal':>11}{'compact':>9}"
          f"{'mc med ext':>12}{'mc max ext':>12}")
    print("  " + "-" * 76)
    results = {}
    for v in VARIANTS:
        t0 = time.perf_counter()
        order = MO.sort_order(x, y, z, v)
        ts = time.perf_counter() - t0
        smed, sp95, full, comp = metrics(x, y, order)
        loc = local_of(order, x, y, z, origin, scale)
        mc = microcells_for(order, x, y, z, origin, scale, loc)
        results[v] = {"step": smed, "p95": sp95, "comp8": comp[8][2],
                      "mc": mc, "sort_s": ts}
        print(f"  {v:<8}{ts:>9.2f}{smed:>10.2f}{sp95:>10.2f}"
              f"{comp[8][0]:>10.0f}{full / np.sqrt(8):>11.0f}"
              f"{comp[8][2]:>9.2f}"
              f"{mc['ext_med'] if mc else 0:>12.0f}"
              f"{mc['ext_max'] if mc else 0:>12.0f}")

    print("\n" + "=" * 78)
    print("[SUBRANGE COMPACTNESS]  actual / ideal")
    print("=" * 78)
    hdr = f"  {'variant':<8}" + "".join(f"{'1/' + str(f):>9}" for f in FRACTIONS)
    print(hdr)
    for v in VARIANTS:
        order = MO.sort_order(x, y, z, v)
        _s, _p, _f, comp = metrics(x, y, order)
        print(f"  {v:<8}" + "".join(f"{comp[f][2]:>9.2f}" for f in FRACTIONS))

    print("\n" + "=" * 78)
    print("[MICROCELL QUALITY]")
    print("=" * 78)
    print(f"  {'variant':<8}{'count':>8}{'pts med':>10}{'ext med':>9}"
          f"{'ext p95':>9}{'ext max':>9}{'>64m':>7}{'>128m':>7}{'>256m':>7}"
          f"{'>500m':>7}{'>1km':>7}")
    for v in VARIANTS:
        order = MO.sort_order(x, y, z, v)
        loc = local_of(order, x, y, z, origin, scale)
        mc = microcells_for(order, x, y, z, origin, scale, loc)
        if not mc:
            continue
        print(f"  {v:<8}{mc['count']:>8}{mc['pts_med']:>10,.0f}"
              f"{mc['ext_med']:>9.0f}{mc['ext_p95']:>9.0f}{mc['ext_max']:>9.0f}"
              f"{mc['gt64']:>7}{mc['gt128']:>7}{mc['gt256']:>7}"
              f"{mc['gt500']:>7}{mc['gt1000']:>7}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
