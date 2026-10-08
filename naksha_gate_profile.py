"""Consolidated pre-1B gate measurements: Parts 1-5, 8.

  [NKPC TILE READ PROFILE]  where the time goes
  [CRC VERIFICATION POLICY] single read + verify-once
  [NKPC LEAF DISTRIBUTION] are leaves even?
  [LOD CAMERA TEST]         does deep zoom actually reduce DRAWN points
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.naksha_cache.reader import NakshaPointCacheReader   # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "test_classified_highprecision.laz")


def pct(v, p):
    s = sorted(v)
    return float(s[min(len(s) - 1, int(len(s) * p / 100))])


def main():
    print("\n" + "=" * 76)
    print("[NKPC TILE READ PROFILE]  (after single-read + float32 fixes)")
    print("=" * 76)
    r = NakshaPointCacheReader(SRC, verify_crc=True)
    node = int(r.nodes_with_lod0()[0])
    tile0 = r.read_tile(node, 0, render_space=True)
    n_pts = tile0["point_count"]
    print(f"  tile: node {node}, {n_pts:,} pts, "
          f"{tile0['bytes_read'] / 1e6:.2f} MB of streams")
    for label, kw in [("CRC on admission", dict(render_space=True)),
                      ("CRC skipped (verified)", dict(render_space=True)),
                      ("float64 world (export path)", dict(render_space=False))]:
        rr = NakshaPointCacheReader(SRC, verify_crc=True)
        rr.read_tile(node, 0, render_space=True)     # pre-verify
        t0 = time.perf_counter()
        for _ in range(15):
            rr.read_tile(node, 0, **kw)
        ms = (time.perf_counter() - t0) / 15 * 1000
        print(f"  {label:<34}{ms:7.3f} ms/tile")
        rr.close()
    rr = NakshaPointCacheReader(SRC, verify_crc=True)
    t0 = time.perf_counter()
    for _ in range(15):
        rr.read_tile(node, 0, render_space=True)
    print(f"  {'CRC paid every read (old default)':<34}"
          f"{(time.perf_counter() - t0) / 15 * 1000:7.3f} ms/tile")
    rr.close()

    print("\n" + "=" * 76)
    print("[CRC VERIFICATION POLICY]")
    print("=" * 76)
    rr = NakshaPointCacheReader(SRC, verify_crc=True)
    nodes = rr.nodes_with_lod0()[:15]
    t0 = time.perf_counter()
    for n in nodes:
        rr.read_tile(n, 0, render_space=True)
    a = (time.perf_counter() - t0) / len(nodes) * 1000
    t0 = time.perf_counter()
    for n in nodes:
        rr.read_tile(n, 0, render_space=True)
    b = (time.perf_counter() - t0) / len(nodes) * 1000
    print(f"  first read  (CRC computed) {a:7.3f} ms/tile")
    print(f"  repeat read (CRC skipped)  {b:7.3f} ms/tile")
    print(f"  stats: {rr.stats}")
    rr.close()

    print("\n" + "=" * 76)
    print("[NKPC LEAF DISTRIBUTION]")
    print("=" * 76)
    idx = r.index
    nodes_arr = idx.nodes
    ids = np.asarray(nodes_arr["node_id"])
    pc = np.asarray(nodes_arr["point_count"], dtype=np.int64)
    bmin = np.asarray(nodes_arr["bounds_min"])
    bmax = np.asarray(nodes_arr["bounds_max"])
    ext = np.maximum(bmax - bmin, 0.0)
    diag = np.hypot(ext[:, 0], ext[:, 1])
    print(f"  leaves: {ids.size}")
    print(f"  {'points':>12}{'min':>12}{'P10':>12}{'median':>12}{'P90':>12}{'P95':>12}{'max':>12}{'mean':>12}")
    print(f"  {'':>12}{int(pc.min()):>12,}{int(pct(pc, 10)):>12,}"
          f"{int(np.median(pc)):>12,}{int(pct(pc, 90)):>12,}"
          f"{int(pct(pc, 95)):>12,}{int(pc.max()):>12,}"
          f"{int(pc.mean()):>12,}")
    print(f"  {'extent(m)':>12}{diag.min():>12.1f}{pct(diag, 10):>12.1f}"
          f"{float(np.median(diag)):>12.1f}{pct(diag, 90):>12.1f}"
          f"{pct(diag, 95):>12.1f}{diag.max():>12.1f}"
          f"{diag.mean():>12.1f}")
    ratio = pc.max() / max(1, int(np.median(pc)))
    print(f"\n  max/median point ratio: {ratio:.1f}x")
    if ratio > 4:
        print("  -> leaves are NOT even; adaptive subdivision is under-splitting")
    print(f"  leaves over 2x target (256K): {int((pc > 512000).sum())}")
    print(f"  leaves under 1/4 target:     {int((pc < 64000).sum())}")

    print("\n" + "=" * 76)
    print("[LOD CAMERA TEST]")
    print("=" * 76)
    gmin = bmin.min(0)
    gmax = bmax.max(0)
    span = gmax - gmin
    cx, cy = (gmin[0] + gmax[0]) * 0.5, (gmin[1] + gmax[1]) * 0.5
    detailed = r.nodes_with_lod0()
    total = int(sum(int(nodes_arr["point_count"][i]) for i in range(ids.size)
                    if int(ids[i]) in set(detailed)))
    print(f"  {'view':<12}{'nodes':>7}{'drawn pts':>14}{'% detail':>10}{'ms':>8}")
    for label, frac in [("fit", 1.0), ("x2", 0.5), ("x4", 0.25),
                        ("x8", 0.125), ("x16", 0.0625), ("x32", 0.03125)]:
        hx, hy = span[0] * 0.5 * frac, span[1] * 0.5 * frac
        sel = [n for n in detailed
               if bmin[n][0] <= cx + hx and bmax[n][0] >= cx - hx
               and bmin[n][1] <= cy + hy and bmax[n][1] >= cy - hy]
        t0 = time.perf_counter()
        pts = 0
        for n in sel:
            t = r.read_tile(n, 0, render_space=True)
            if t:
                pts += t["point_count"]
        ms = (time.perf_counter() - t0) * 1000
        print(f"  {label:<12}{len(sel):>7}{pts:>14,}"
              f"{pts / max(1, total) * 100:>9.1f}%{ms:>8.1f}")
    r.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
