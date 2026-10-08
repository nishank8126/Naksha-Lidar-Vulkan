"""build_v2_27m.py - build the CORRECTED (index v2) cache of the real 27M source
into ./v2cache, leaving the existing (old, pre-Morton-fix) cache untouched.

The project file lives in v2cache/ so the cache + index land there; the source is
read from its original location. Reports timing and (if psutil is present) RSS.
"""
import os
import shutil
import sys
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from gui.naksha_cache.builder import NakshaPointCacheBuilder      # noqa: E402

SRC = os.path.join(ROOT, "test_classified_highprecision.laz")
# usage: py build_v2_27m.py [output_dir_name]   (default v2cache_geo)
OUT = os.path.join(ROOT, sys.argv[1] if len(sys.argv) > 1 else "v2cache_geo")
PROJ = os.path.join(OUT, "test_classified_highprecision.laz")

os.makedirs(OUT, exist_ok=True)
peak = {"rss": 0}


def _watch():
    try:
        import psutil
        p = psutil.Process()
    except Exception:
        return
    while peak.get("run", True):
        try:
            peak["rss"] = max(peak["rss"], p.memory_info().rss)
        except Exception:
            pass
        time.sleep(0.5)


threading.Thread(target=_watch, daemon=True).start()
t0 = time.perf_counter()
#         py build_v2_27m.py <dir> <lod_ratio>   (default ratio: lod_ladder.DEFAULT_RATIO)
#         py build_v2_27m.py <dir> <lod_ratio> <lod_levels>   (levels incl. overview)
_kw = {"lod_ratio": float(sys.argv[2])} if len(sys.argv) > 2 else {}
if len(sys.argv) > 3:
    _kw["lod_levels"] = int(sys.argv[3])
b = NakshaPointCacheBuilder(PROJ, chunk_points=2_000_000, **_kw)
stats = b.build([SRC], with_overview=True)
b.cleanup_shards()
shutil.rmtree(PROJ + ".shards", ignore_errors=True)
peak["run"] = False
print(f"V2 BUILD DONE build_s={time.perf_counter() - t0:.1f} "
      f"pc={os.path.getsize(PROJ + '.nakshapc') / 1e6:.0f}MB "
      f"idx={os.path.getsize(PROJ + '.nakshaidx') / 1024:.0f}KB "
      f"peak_rss={peak['rss'] / 1e6:.0f}MB", flush=True)
print({k: (round(v, 2) if isinstance(v, float) else v)
       for k, v in stats.items()}, flush=True)
