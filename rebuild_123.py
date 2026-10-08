"""Rebuild 123.las (the 2.95M dataset in the bug report) with the fixed
Morton key + shard prefix, then measure the resulting leaf geometry.

Preserves the .nakshaedit sidecar so any user edits survive the rebuild.
"""
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.naksha_cache.builder import NakshaPointCacheBuilder     # noqa: E402
from gui.naksha_cache.format import LEAF_TARGET_POINTS           # noqa: E402
from gui.naksha_cache.index import project_paths                 # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "123.las")


def main():
    idx_path, pc_path, edit_path, build_path = project_paths(SRC)
    backup = edit_path + ".rebuild_bak"
    if os.path.exists(edit_path):
        shutil.copy2(edit_path, backup)

    for p in (idx_path, pc_path, build_path):
        if os.path.exists(p):
            os.remove(p)
    shutil.rmtree(SRC + ".shards", ignore_errors=True)

    t0 = time.perf_counter()
    b = NakshaPointCacheBuilder(SRC, chunk_points=2_000_000,
                                leaf_target=LEAF_TARGET_POINTS,
                                ordering="XY16")
    b.scan_sources([SRC])
    stats = b.build([SRC], with_overview=True)
    b.cleanup_shards()
    shutil.rmtree(SRC + ".shards", ignore_errors=True)
    total = time.perf_counter() - t0

    if os.path.exists(backup):
        shutil.move(backup, edit_path)

    print(f"BUILD {total:.1f}s")
    for k in ("pass1_seconds", "pass2_seconds", "shard_count", "shards_used",
              "blocks", "nodes", "written_points", "pc_bytes", "idx_bytes"):
        if k in stats:
            v = stats[k]
            print(f"  {k:<18}{v:,.3f}" if isinstance(v, float)
                  else f"  {k:<18}{v:,}")

    import numpy as np
    from gui.naksha_cache.reader import NakshaPointCacheReader
    r = NakshaPointCacheReader(SRC, verify_crc=False, load_edits=False)
    n = r.index.nodes
    hdr = r.index.header
    bmin = np.asarray(n["bounds_min"], float)
    bmax = np.asarray(n["bounds_max"], float)
    w = bmax[:, 0] - bmin[:, 0]
    h = bmax[:, 1] - bmin[:, 1]
    gmin = np.asarray(hdr["bounds_min"], float)
    gmax = np.asarray(hdr["bounds_max"], float)
    da = float((gmax[0] - gmin[0]) * (gmax[1] - gmin[1]))
    area = float(np.sum(w * h))

    from collections import Counter
    buckets = Counter((round(float(wi)), round(float(hi)))
                      for wi, hi in zip(w, h))

    print("\n[LEAF GEOMETRY AFTER FIX]")
    print(f"  nodes            : {n.size}")
    print(f"  sum(area)/dataset: {area / max(da, 1e-9):.3f}x   "
          f"(was 4.93x with the broken key)")
    print(f"  width  min/p50/max: {w.min():.1f} / {np.median(w):.1f} / "
          f"{w.max():.1f} m")
    print(f"  height min/p50/max: {h.min():.1f} / {np.median(h):.1f} / "
          f"{h.max():.1f} m")

    # X-slice coverage depth: a tiling has depth ~1 everywhere.
    cover = np.zeros(200, float)
    lo, hi_ = gmin[0], gmax[0]
    for i in np.argsort(bmin[:, 0]):
        a = int(np.clip((bmin[i][0] - lo) / (hi_ - lo) * 200, 0, 199))
        bnd = int(np.clip((bmax[i][0] - lo) / (hi_ - lo) * 200, 0, 199))
        cover[a:bnd + 1] += 1
    print(f"  x-slice leaf depth: min={cover.min():.0f} "
          f"p50={np.median(cover):.0f} max={cover.max():.0f}  "
          f"(was 1..5-ish before)")

    # pairwise XY overlap
    ox = np.maximum(0.0, np.minimum(bmax[:, None, 0], bmax[None, :, 0])
                    - np.maximum(bmin[:, None, 0], bmin[None, :, 0]))
    oy = np.maximum(0.0, np.minimum(bmax[:, None, 1], bmax[None, :, 1])
                    - np.maximum(bmin[:, None, 1], bmin[None, :, 1]))
    ov = (ox > 1e-9) & (oy > 1e-9)
    np.fill_diagonal(ov, False)
    print(f"  overlapping pairs : {int(ov.sum())} of "
          f"{n.size * (n.size - 1)}")

    print("\n  width buckets:")
    for (wi, hi2), c in buckets.most_common(8):
        print(f"    {wi:>5} x {hi2:<5} m : {c:>4}")
    r.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
