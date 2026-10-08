"""[PHYSICAL PAGE GRANULARITY EXPERIMENT] - controlled page-target sweep.

ORDERING IS CONTROLLED (matrix A..D) so page size is not confounded with it:
    A  baseline page target + production ordering (XYZ16, current default)
    B  baseline page target + XY16              -> isolates ORDERING effect
    C  128K page target   + XY16
    D   64K page target   + XY16

x32 WORLD BOUNDS ARE FIXED AND RECORDED, and reused for every candidate, so
each rebuild cannot silently shift the viewport.

IDEAL SPATIAL POINTS are computed directly from the source XY, so
amplification = decoded / ideal is a true measure of how much EXTRA data the
cache organisation forces us to read. Codec stays NONE throughout: compression
would be a third variable.
"""
import os
import shutil
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.naksha_cache.builder import NakshaPointCacheBuilder    # noqa: E402
from gui.naksha_cache.index import project_paths                # noqa: E402
from gui.naksha_cache.reader import NakshaPointCacheReader     # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "test_classified_highprecision.laz")
WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "_pagetest.laz")
TOTAL = 26_960_750
# x32 of the 1 km dataset: a 31.25 m window centred on the dataset.
X32_HALF = 15.625


def hdr_bounds(path):
    import laspy
    with laspy.open(path) as f:
        h = f.header
        return (float(h.mins[0]), float(h.mins[1]), float(h.maxs[0]),
                float(h.maxs[1]))


def ideal_points(path, x0, y0, x1, y1):
    """Points whose XY falls inside the exact x32 viewport (offline only)."""
    import laspy
    n = 0
    with laspy.open(path) as f:
        for ch in f.chunk_iterator(2_000_000):
            x = np.asarray(ch.x)
            y = np.asarray(ch.y)
            n += int(((x >= x0) & (x <= x1) & (y >= y0) & (y <= y1)).sum())
    return n


def build(tag, page_target, xy16, x0, y0, x1, y1):
    """Build a cache candidate. Returns a metrics dict."""
    for p in project_paths(WORK):
        if os.path.exists(p):
            os.remove(p)
    shutil.rmtree(WORK + ".shards", ignore_errors=True)
    t0 = time.perf_counter()
    b = NakshaPointCacheBuilder(WORK, chunk_points=2_000_000,
                                leaf_target=page_target, ordering="XY16" if xy16 else "XYZ16")
    peak = [0.0]
    try:
        import psutil
        proc = psutil.Process()
        orig = b.finalize

        def finalize():
            peak[0] = max(peak[0], proc.memory_info().rss / 1e6)
            return orig()
        b.finalize = finalize
    except Exception:
        pass
    b.scan_sources([SRC])
    peak[0] = max(peak[0], 0.0)
    stats = b.build([SRC], with_overview=False)
    build_s = time.perf_counter() - t0
    b.cleanup_shards()

    r = NakshaPointCacheReader(WORK, verify_crc=False)
    n = r.index.nodes
    bmin = np.asarray(n["bounds_min"])
    bmax = np.asarray(n["bounds_max"])
    w = bmax[:, 0] - bmin[:, 0]
    h = bmax[:, 1] - bmin[:, 1]
    # WORLD-SPACE extent statistics (never normalised ints / render floats)
    extent = np.maximum(w, h)
    area = w * h
    aspect = np.maximum(w, h) / np.maximum(np.minimum(w, h), 1e-6)
    pc = np.asarray(n["point_count"])

    cells = r.visible_microcells(x0, y0, x1, y1, lod=0)
    pages = [i for i in range(n.size)
             if bmin[i][0] <= x1 and bmax[i][0] >= x0
             and bmin[i][1] <= y1 and bmax[i][1] >= y0]
    mc = r.index.microcells
    decoded = sum(int(mc[c]["point_count"]) for c in cells) if len(mc) else 0
    r.close()

    def d(a, p):
        return float(np.percentile(a, p)) if len(a) else 0.0

    return {
        "tag": tag, "page_target": page_target, "ordering":
            "XY16" if xy16 else "XYZ16",
        "pages": int(n.size), "pc": pc, "extent": extent, "aspect": aspect,
        "cache_mb": os.path.getsize(WORK + ".nakshapc") / 1e6,
        "idx_kb": os.path.getsize(WORK + ".nakshaidx") / 1e3,
        "build_s": build_s, "peak_mb": peak[0],
        "microcells": int(len(cells)),
        "pages_touched": len(pages),
        "decoded": decoded,
        "p25": d(pc, 25), "med": d(pc, 50), "p75": d(pc, 75),
        "p95": d(pc, 95),
        "e_med": d(extent, 50), "e_p95": d(extent, 95), "e_max": float(extent.max()),
        "a_med": d(aspect, 50), "a_p95": d(aspect, 95),
        "a_max": float(aspect.max()),
        "worst_extent": [(int(n["node_id"][i]), int(pc[i]), float(w[i]),
                          float(h[i]), float(area[i]), float(aspect[i]))
                         for i in np.argsort(-extent)[:5]],
        "worst_aspect": [(int(n["node_id"][i]), int(pc[i]), float(w[i]),
                          float(h[i]), float(area[i]), float(aspect[i]))
                         for i in np.argsort(-aspect)[:5]],
    }


def main():
    print("\n" + "=" * 78)
    print("[PHYSICAL PAGE GRANULARITY EXPERIMENT]")
    print("=" * 78)
    x0d, y0d, x1d, y1d = hdr_bounds(SRC)
    cx, cy = (x0d + x1d) / 2.0, (y0d + y1d) / 2.0
    x0, x1 = cx - X32_HALF, cx + X32_HALF
    y0, y1 = cy - X32_HALF, cy + X32_HALF
    print(f"  dataset bounds  x[{x0d:,.0f}..{x1d:,.0f}] "
          f"y[{y0d:,.0f}..{y1d:,.0f}]")
    print("  EXACT x32 WORLD BOUNDS (fixed for every candidate):")
    print(f"    X min {x0:,.3f}   X max {x1:,.3f}")
    print(f"    Y min {y0:,.3f}   Y max {y1:,.3f}")
    print(f"    width {x1 - x0:.3f} m")

    t0 = time.perf_counter()
    ideal = ideal_points(SRC, x0, y0, x1, y1)
    print(f"\n  IDEAL spatial points in that viewport : {ideal:,} "
          f"(scanned in {time.perf_counter() - t0:.1f}s)")
    print(f"  = {ideal / TOTAL * 100:.4f}% of source")

    cands = [("A baseline", 256_000, False),
             ("C 128K", 128_000, True),
             ("D 64K", 64_000, True)]
    res = []
    for tag, target, xy in cands:
        print(f"\n  building {tag} (target {target:,}, "
              f"{'XY16' if xy else 'XYZ16'}) ...", flush=True)
        m = build(tag, target, xy, x0, y0, x1, y1)
        m["amplification"] = m["decoded"] / max(ideal, 1)
        res.append(m)
        print(f"    pages {m['pages']:,}  median pts {m['med']:,.0f}  "
              f"median extent {m['e_med']:.0f} m  p95 {m['e_p95']:.0f} m")
        print(f"    x32: pages {m['pages_touched']}  microcells "
              f"{m['microcells']}  decoded {m['decoded']:,} "
              f"({m['decoded'] / TOTAL * 100:.2f}%)  "
              f"amplification {m['amplification']:.1f}x")

    base = res[0]
    print("\n" + "=" * 78)
    print("[COMPARISON]")
    print("=" * 78)
    print(f"  {'candidate':<12}{'pages':>7}{'med pts':>10}{'med ext':>9}"
          f"{'p95 ext':>9}{'cache MB':>10}{'build s':>9}{'x32 pg':>8}"
          f"{'x32 dec':>12}{'x32 %':>8}{'amplif':>9}")
    for m in res:
        print(f"  {m['tag']:<12}{m['pages']:>7}{m['med']:>10,.0f}"
              f"{m['e_med']:>9.0f}{m['e_p95']:>9.0f}{m['cache_mb']:>10.0f}"
              f"{m['build_s']:>9.1f}{m['pages_touched']:>8}"
              f"{m['decoded']:>12,}{m['decoded'] / TOTAL * 100:>7.2f}%"
              f"{m['amplification']:>9.1f}")

    print("\n  RELATIVE TO BASELINE")
    for m in res[1:]:
        print(f"  {m['tag']}:")
        print(f"    x32 decoded reduction : "
              f"{(base['decoded'] - m['decoded']) / base['decoded'] * 100:+.1f}%")
        print(f"    cache-size change     : "
              f"{(m['cache_mb'] - base['cache_mb']) / base['cache_mb'] * 100:+.1f}%")
        print(f"    build-time change     : "
              f"{(m['build_s'] - base['build_s']) / base['build_s'] * 100:+.1f}%")
        print(f"    page-count change     : "
              f"{(m['pages'] - base['pages']) / base['pages'] * 100:+.1f}%")

    print("\n  ASPECT RATIO (world space):")
    for m in res:
        print(f"    {m['tag']:<12} median {m['a_med']:.2f}  "
              f"p95 {m['a_p95']:.2f}  max {m['a_max']:.2f}")
    print("\n  WORST PAGES BY EXTENT (baseline):")
    for pid, n_, w_, h_, a_, asp in base["worst_extent"]:
        print(f"    page {pid:>4}  {n_:>8,} pts  {w_:>7.0f} x {h_:>7.0f} m  "
              f"area {a_:>10.0f}  aspect {asp:>6.2f}")
    print("\n  WORST PAGES BY ASPECT (baseline):")
    for pid, n_, w_, h_, a_, asp in base["worst_aspect"]:
        print(f"    page {pid:>4}  {n_:>8,} pts  {w_:>7.0f} x {h_:>7.0f} m  "
              f"area {a_:>10.0f}  aspect {asp:>6.2f}")

    print("\n  INDEX SCALING (informational only)")
    for m in res:
        bpp = m["idx_kb"] * 1024 / m["pages"]
        print(f"    {m['tag']:<12} {m['pages']:>7,} pages  "
              f"{m['idx_kb'] / 1024:.2f} MB  {bpp:>6.0f} B/page  "
              f"proj 1.14B {bpp * 1_140_436_759 / 1e9:.2f} GB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
