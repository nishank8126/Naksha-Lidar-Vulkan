"""STAGE 2: build the native cache for test_classified_highprecision.laz.

26.96M points. Measures build time, peak RAM, cache size, tile count and the
LIVE-FIRST-VISUAL timing (Part 8/21): the overview must be usable long before
spatialization finishes.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.naksha_cache.builder import NakshaPointCacheBuilder   # noqa: E402
from gui.naksha_cache.index import project_paths               # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "test_classified_highprecision.laz")


def rss_mb():
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1e6
    except Exception:
        return 0.0


def main():
    print("\n[NAKSHA CACHE V1] STAGE 2 build: test_classified_highprecision.laz")
    print("=" * 74)
    for p in project_paths(SRC):
        if os.path.exists(p):
            os.remove(p)
    t0 = time.perf_counter()
    b = NakshaPointCacheBuilder(SRC, chunk_points=2_000_000,
                                leaf_target=256_000)
    peak = [rss_mb()]
    stats = b.build([SRC], with_overview=True)
    peak.append(rss_mb())
    total = time.perf_counter() - t0
    print(f"\n  BUILD {total:.1f}s")
    for k in ("header_seconds", "overview_seconds", "first_visual_seconds",
              "pass1_seconds", "pass2_seconds", "shard_count", "shards_used",
              "blocks", "nodes", "written_points", "temp_bytes", "pc_bytes",
              "idx_bytes"):
        if k in stats:
            v = stats[k]
            print(f"    {k:<22}{v:,.4f}" if isinstance(v, float)
                  else f"    {k:<22}{v:,}")
    print(f"    {'peak_rss_mb':<22}{max(peak):,.1f}")
    print(f"    {'source_mb':<22}"
          f"{os.path.getsize(SRC) / 1e6:,.1f}")
    if stats.get("written_points"):
        print(f"    {'build_Mpts_s':<22}"
              f"{stats['written_points'] / total / 1e6:,.2f}")
    print(f"    {'first_visual_vs_build':<22}"
          f"{stats['first_visual_seconds']:.1f}s / {total:.1f}s "
          f"= {stats['first_visual_seconds'] / total * 100:.0f}%")
    b.cleanup_shards()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
