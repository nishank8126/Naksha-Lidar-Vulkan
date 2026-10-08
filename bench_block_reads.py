"""bench_block_reads.py - PHASE 5 / PARTS 16, 58, 59: block read latency, batch
and ordering effects, and read-worker scaling on a REAL .nakshapc.

    py bench_block_reads.py [project_path]   (default: v2cache_geo2 build)

All timings are OS-FILE-CACHE WARM (the file cache cannot be dropped without
admin rights); read it as "decode + parse + lock + syscall cost", not as disk
seek latency. A cold-disk run needs a reboot or `RAMMap -> Empty Standby`.
"""
import concurrent.futures
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from gui.naksha_cache.reader import NakshaPointCacheReader          # noqa: E402

DEFAULT = os.path.join(ROOT, "v2cache_geo2", "test_classified_highprecision.laz")


def timed(fn, *a):
    t = time.perf_counter()
    out = fn(*a)
    return (time.perf_counter() - t) * 1000.0, out


def main():
    proj = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    r = NakshaPointCacheReader(proj, verify_crc=False, load_edits=False)
    idx = r.index
    blocks = idx.blocks
    rng = np.random.default_rng(7)
    ov = int(idx.overview_block_id)
    lod0 = [b for b in blocks if int(b["lod"]) == 0 and int(b["block_id"]) != ov]
    pick = [lod0[i] for i in rng.permutation(len(lod0))[:64]]
    keys = [(int(b["node_id"]), 0) for b in pick]
    sizes = np.array([int(b["stored_bytes"]) for b in pick])
    print(f"cache: {os.path.getsize(proj + '.nakshapc') / 1e6:.0f} MB, "
          f"{len(blocks)} blocks, LOD0 leaf block ~{sizes.mean() / 1e6:.2f} MB "
          f"({int(np.mean([b['point_count'] for b in pick])):,} pts)")
    for k in keys[:8]:                                   # touch once
        r.read_tile(*k)

    # ---- single-block latency -------------------------------------------------
    lat = [timed(r.read_tile, *k)[0] for k in keys]
    print(f"\n[SINGLE BLOCK LOD0]  p50 {np.median(lat):.3f} ms  p95 "
          f"{np.percentile(lat, 95):.3f}  max {max(lat):.3f}   "
          f"-> {sizes.mean() / 1e6 / (np.median(lat) / 1000):.0f} MB/s")
    # a coarse rung (the rung the camera requests first)
    coarse = [(int(b["node_id"]), int(b["lod"])) for b in blocks
              if int(b["lod"]) == 3 and int(b["block_id"]) != ov][:64]
    lat3 = [timed(r.read_tile, *k)[0] for k in coarse]
    print(f"[SINGLE BLOCK LOD3]  p50 {np.median(lat3):.3f} ms  p95 "
          f"{np.percentile(lat3, 95):.3f}  (the coverage-floor request)")

    # ---- batches: per-block cost vs batch size -----------------------------
    print("\n[BATCH, sequential, random order]")
    for n in (1, 4, 16, 64):
        ts = []
        for rep in range(5):
            sub = [keys[i] for i in rng.permutation(64)[:n]]
            t = time.perf_counter()
            for k in sub:
                r.read_tile(*k)
            ts.append((time.perf_counter() - t) * 1000.0)
        print(f"  {n:>3} blocks: {np.median(ts):8.2f} ms total  "
              f"{np.median(ts) / n:6.3f} ms/block")

    # ---- ordering: random vs file-offset order (coalescing potential) --------
    print("\n[ORDERING, 64 blocks]  (Part 16: does physical order matter?)")
    off = {int(b["block_id"]): int(b["file_offset"]) for b in pick}
    by_off = sorted(pick, key=lambda b: int(b["file_offset"]))
    keys_off = [(int(b["node_id"]), 0) for b in by_off]
    for label, ks in (("random order", keys), ("file-offset order", keys_off)):
        ts = []
        for _ in range(7):
            t = time.perf_counter()
            for k in ks:
                r.read_tile(*k)
            ts.append((time.perf_counter() - t) * 1000.0)
        print(f"  {label:<18}: {np.median(ts):8.2f} ms")
    gaps = np.diff([int(b["file_offset"]) for b in by_off])
    contiguous = int((gaps <= sizes.max() * 1.5).sum())
    print(f"  of 63 neighbours in offset order, {contiguous} lie within 1.5 block "
          f"sizes of each other (physical adjacency available to coalesce)")

    # ---- worker scaling ------------------------------------------------------
    print("\n[WORKER SCALING, 64 LOD0 blocks, shared reader (seek+read locked)]")
    base = None
    for w in (1, 2, 4, 8):
        ts = []
        for _ in range(5):
            with concurrent.futures.ThreadPoolExecutor(w) as ex:
                t = time.perf_counter()
                list(ex.map(lambda k: r.read_tile(*k), keys))
                ts.append((time.perf_counter() - t) * 1000.0)
        m = float(np.median(ts))
        base = base or m
        print(f"  {w} worker(s): {m:8.2f} ms   speed-up x{base / m:.2f}   "
              f"{64 * sizes.mean() / 1e6 / (m / 1000):.0f} MB/s")
    r.close()


if __name__ == "__main__":
    main()
