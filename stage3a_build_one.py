"""Stage 3A: build ONE representative NT219 cache (64K / XY16 / codec NONE).

Representative file: MANDI_56.laz  (268.81M pts, dense urban, LAS 1.2 pf3, RGB+cls).
Mirrors build_prod.py exactly; adds the Stage-3A telemetry the spec requires:
header time, spatialization time, total build time, Mpts/sec, peak RAM, peak
temp disk, final NKPC/NKIDX, pages/microcells/LOD-block counts.
"""
import os
import sys
import time
import shutil
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.naksha_cache.builder import NakshaPointCacheBuilder
from gui.naksha_cache.index import project_paths, IndexReader
from gui.naksha_cache.format import LEAF_TARGET_POINTS

# ---- peak-RAM sampler (psutil with ctypes fallback) ---------------------
try:
    import psutil

    def rss_mb():
        return psutil.Process(os.getpid()).memory_info().rss / 1e6
    print("[RAM] psutil available", flush=True)
except Exception as exc:
    import ctypes
    import ctypes.wintypes as w

    def rss_mb():
        k32 = ctypes.windll.kernel32
        proc = k32.GetCurrentProcess()
        pmc = w.PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(pmc)
        k32.GetProcessMemoryInfo(proc, ctypes.byref(pmc), ctypes.sizeof(pmc))
        return pmc.WorkingSetSize / 1e6
    print(f"[RAM] psutil unavailable ({exc}); ctypes fallback", flush=True)


SRC = r"H:\TESTING CONTIUES\FUNIVIA\NT219\MANDI_56.laz"
print("[STAGE 3A] build one NT219 cache  file=MANDI_56.laz  64K/XY16/codec=NONE",
      flush=True)

# remove stale cache + shards
for p in project_paths(SRC):
    if os.path.exists(p):
        os.remove(p)
shutil.rmtree(SRC + ".shards", ignore_errors=True)

stop = [False]
peak = [rss_mb()]


def sampler():
    while not stop[0]:
        peak[0] = max(peak[0], rss_mb())
        time.sleep(0.5)


threading.Thread(target=sampler, daemon=True).start()
t0 = time.perf_counter()

b = NakshaPointCacheBuilder(SRC, chunk_points=2_000_000,
                            leaf_target=LEAF_TARGET_POINTS, ordering="XY16")
b.scan_sources([SRC])
stats = b.build([SRC], with_overview=True)
dur = time.perf_counter() - t0
stop[0] = True

# peak temp disk (shards) BEFORE cleanup
shd = SRC + ".shards"
peak_temp = 0
if os.path.isdir(shd):
    for root, _, files in os.walk(shd):
        for fn in files:
            try:
                peak_temp += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
b.cleanup_shards()
shutil.rmtree(shd, ignore_errors=True)

print("\n[STAGE 3A BUILD DONE]", flush=True)
print(f"  header_scan      = {stats.get('header_seconds'):.1f} s")
print(f"  overview         = {stats.get('overview_seconds'):.1f} s")
print(f"  spatialize pass1 = {stats.get('pass1_seconds'):.1f} s")
print(f"  finalize pass2   = {stats.get('pass2_seconds'):.1f} s")
print(f"  total            = {dur:.1f} s")
print(f"  Mpts/sec         = {b.total_points / dur / 1e6:.3f}")
print(f"  peak RAM         = {peak[0]:.0f} MB")
print(f"  peak temp disk   = {peak_temp / 1e9:.2f} GB  (stat temp_bytes={stats.get('temp_bytes'):,})")
print(f"  final NKPC       = {stats.get('pc_bytes') / 1e9:.2f} GB")
print(f"  final NKIDX      = {stats.get('idx_bytes') / 1e6:.2f} MB")
print(f"  written_points   = {int(stats.get('written_points')):,}")
print(f"  nodes            = {int(stats.get('nodes'))}  blocks={int(stats.get('blocks'))}")

# post-build index breakdown by LOD
idxp = project_paths(SRC)[0]
idx = IndexReader(idxp)
bl = idx.blocks
header = idx.header
print(f"\n[INDEX] node_count={int(header['node_count'])} block_count={int(header['block_count'])} "
      f"microcell_count={int(header['microcell_count'])} lod_count={int(header['lod_count'])}",
      flush=True)
ov = int(idx.overview_block_id)
print(f"  overview_block_id = {ov}")
for l in range(int(header["lod_count"])):
    bl_l = bl[bl["lod"] == l]
    tag = "  (overview / first-paint)" if l == int(header["lod_count"]) - 1 else ""
    if bl_l.size:
        print(f"  LOD{l}: blocks={bl_l.size} points={int(bl_l['point_count'].sum()):,} "
              f"maxblock={int(bl_l['point_count'].max()):,} median={int(np.median(bl_l['point_count'])):,}{tag}")
    else:
        print(f"  LOD{l}: blocks=0{tag}")
l0 = bl[bl["lod"] == 0]
if l0.size:
    print(f"\n  PHYSICAL LEAF PAGES (LOD0): n={l0.size} "
          f"min={int(l0['point_count'].min()):,} max={int(l0['point_count'].max()):,} "
          f"median={int(np.median(l0['point_count'])):,}  target=64,000")
mc = getattr(idx, "microcells", None)
if mc is not None and len(mc):
    print(f"\n  MICROCELLS: n={len(mc)} median={float(np.median(mc['point_count'])):.0f} "
          f"p95={float(np.percentile(mc['point_count'], 95)):.0f}")
idx.close()
print("[STAGE 3A] report complete.", flush=True)
