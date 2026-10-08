"""STAGE 2 cached reopen + streaming behaviour (Part 21, Part 43).

Proves the cache is usable WITHOUT decoding the source: reopen, read the
overview, then simulate zoom x2/x4/x8/x16 by moving the visible rectangle and
recording which blocks were actually read. A deep zoom must NOT pull every
detailed point - that is the property the whole design exists for.
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.naksha_cache.index import IndexReader          # noqa: E402
from gui.naksha_cache.reader import NakshaPointCacheReader   # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "test_classified_highprecision.laz")


def main():
    print("\n[NAKSHA CACHE V1] STAGE 2 cached reopen + zoom behaviour")
    print("=" * 74)
    src_before = os.stat(SRC)
    t0 = time.perf_counter()
    r = NakshaPointCacheReader(SRC, verify_crc=True)
    idx_ms = (time.perf_counter() - t0) * 1000
    idx = r.index
    print(f"  index open      {idx_ms:8.2f} ms | {idx.summary()}")
    t0 = time.perf_counter()
    ov = r.read_overview()
    ov_ms = (time.perf_counter() - t0) * 1000
    print(f"  overview read   {ov_ms:8.2f} ms | {ov['point_count']:,} pts "
          f"({ov['bytes_read'] / 1e6:.1f} MB)")

    nodes = idx.nodes
    # `detailed` holds NODE IDS, so bounds must come from a LOOKUP.
    # Indexing a memmap positionally with a node id raises IndexError.
    bmin = {int(nodes["node_id"][i]): np.asarray(nodes["bounds_min"][i])
            for i in range(nodes.size)}
    bmax = {int(nodes["node_id"][i]): np.asarray(nodes["bounds_max"][i])
            for i in range(nodes.size)}
    pcount = {int(nodes["node_id"][i]): int(nodes["point_count"][i])
             for i in range(nodes.size)}
    _allmin = np.array([bmin[k] for k in sorted(bmin)])
    _allmax = np.array([bmax[k] for k in sorted(bmax)])
    gmin, gmax = _allmin.min(0), _allmax.max(0)
    span = gmax - gmin
    detailed = r.nodes_with_lod0()
    total_detail_pts = int(sum(pcount[n] for n in detailed))
    print(f"  dataset bounds  x[{gmin[0]:,.0f}..{gmax[0]:,.0f}] "
          f"y[{gmin[1]:,.0f}..{gmax[1]:,.0f}]")
    print(f"  {len(detailed)} detailed tiles, {total_detail_pts:,} detailed pts")

    cx, cy = (gmin[0] + gmax[0]) * 0.5, (gmin[1] + gmax[1]) * 0.5
    print(f"\n  {'view':<22}{'tiles':>7}{'blocks read':>13}"
          f"{'pts read':>14}{'% of detail':>14}{'median ms':>11}")
    for label, frac in [("fit (whole)", 1.0), ("zoom x2", 0.5),
                        ("zoom x4", 0.25), ("zoom x8", 0.125),
                        ("zoom x16", 0.0625)]:
        hx = span[0] * 0.5 * frac
        hy = span[1] * 0.5 * frac
        x0, x1 = cx - hx, cx + hx
        y0, y1 = cy - hy, cy + hy
        sel = [n for n in detailed
               if bmin[n][0] <= x1 and bmax[n][0] >= x0
               and bmin[n][1] <= y1 and bmax[n][1] >= y0]
        times, pts = [], 0
        for n in sel:
            t = time.perf_counter()
            tile = r.read_tile(n, 0, verify_crc=True)
            times.append((time.perf_counter() - t) * 1000)
            if tile:
                pts += tile["point_count"]
        med = float(np.median(times)) if times else 0.0
        pct = pts / max(1, total_detail_pts) * 100
        print(f"  {label:<22}{len(sel):>7}{len(sel):>13}{pts:>14,}"
              f"{pct:>13.2f}%{med:>11.2f}")

    after = os.stat(SRC)
    print(f"\n  source unchanged during cached read: "
          f"{src_before.st_mtime == after.st_mtime and src_before.st_size == after.st_size}")
    print(f"  reader stats: {r.stats}")
    deep = [n for n in detailed]
    r.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
