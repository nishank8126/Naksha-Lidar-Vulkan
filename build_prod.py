"""Rebuild the real 26.96M production cache at the new 64K default, overview ON."""
import os, sys, time, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gui.naksha_cache.builder import NakshaPointCacheBuilder
from gui.naksha_cache.index import project_paths
from gui.naksha_cache.format import LEAF_TARGET_POINTS

SRC = "test_classified_highprecision.laz"
print("LEAF_TARGET_POINTS default in use:", LEAF_TARGET_POINTS, flush=True)
for p in project_paths(SRC):
    try:
        if os.path.exists(p):
            os.remove(p)
    except PermissionError:
        pass
shutil.rmtree(SRC + ".shards", ignore_errors=True)
t0 = time.perf_counter()
b = NakshaPointCacheBuilder(SRC, chunk_points=2_000_000,
                            leaf_target=LEAF_TARGET_POINTS, ordering="XY16")
b.scan_sources([SRC])
b.build([SRC], with_overview=True)
b.cleanup_shards()
shutil.rmtree(SRC + ".shards", ignore_errors=True)
print("PROD BUILD DONE  build_s=%.1f  pc=%dMB  idx=%dKB"
      % (time.perf_counter() - t0,
         os.path.getsize(SRC + ".nakshapc") / 1e6,
         os.path.getsize(SRC + ".nakshaidx") / 1024), flush=True)
