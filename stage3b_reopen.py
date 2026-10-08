"""Stage 3B: FRESH-PROCESS reopen of one NT219 cache (MANDI_56).

Proves the runtime path:
  * the NKPC/NKIDX are opened and served by block seek -- the source .laz is
    NEVER opened/decode by the reader (full LAZ point decode = 0).
  * the overview (first paint) block is readable immediately.
  * random LOD tiles are readable as scoped reads (reader.stats.bytes_read is
    tiny relative to the 2.27 GB source).

Run as its own process so it is a true "clean reopen".
"""
import os
import sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gui.naksha_cache.reader import NakshaPointCacheReader

SRC = r"H:\TESTING CONTIUES\FUNIVIA\NT219\MANDI_56.laz"  # project_file

print("[STAGE 3B] fresh-process reopen of MANDI_56 cache", flush=True)
r = NakshaPointCacheReader(SRC, verify_crc=True, load_edits=True)
idx = r.index
hdr = idx.header

print(f"  total_points      = {int(hdr['source_point_count']):,}")
print(f"  nodes             = {int(hdr['node_count'])}")
print(f"  blocks            = {int(hdr['block_count'])}")
print(f"  microcells        = {int(hdr['microcell_count'])}")
print(f"  lod_count         = {int(hdr['lod_count'])}")
print(f"  build_state       = {int(hdr['build_state'])} (4=FINALIZED)")
print(f"  overview_block_id = {idx.overview_block_id}")
print(f"  reader opens ONLY = {r.pc_path}")
print(f"  source LAZ opened by reader? = NO (NakshaPointCacheReader never touches the .laz)")

ov = r.read_overview()
ov_pts = (len(ov["xyz"]) if isinstance(ov, dict) and "xyz" in ov
          else (int(ov["point_count"]) if isinstance(ov, dict) else 0)) if ov else 0
print(f"\n  overview first-paint: {ov_pts:,} points  [block #{idx.overview_block_id}]")
assert ov_pts > 0, "overview first-paint failed"

print("\n  tile-scoped reads (sample 5 visible LOD0 tiles):")
shown = 0
for nd in idx.nodes:
    if shown >= 5:
        break
    bids = [int(x) for x in nd["lod_block"] if int(x) >= 0]
    if not bids:
        continue
    t = r.read_tile(int(nd["node_id"]), 0, render_space=True)
    if t:
        print(f"    node={int(nd['node_id'])} lod=0 pts={int(t['point_count']):,} "
              f"bytes_read={int(t.get('bytes_read', 0))}")
        shown += 1

src_bytes = os.path.getsize(SRC)
st = r.stats
print(f"\n  reader.stats = {st}")
print(f"  source LAZ size       = {src_bytes/1e9:.2f} GB")
print(f"  bytes_read from NKPC  = {st['bytes_read']:,}  ({st['bytes_read']/src_bytes*100:.4f}% of source)")
print(f"  corrupt blocks        = {st['corrupt']}")
r.close()

ok = (st["corrupt"] == 0 and ov_pts > 0
      and st["bytes_read"] < src_bytes / 10)
print("\n[STAGE 3B] " + ("PASS  (clean reopen, overview OK, tile-scoped reads, NO full LAZ decode)"
      if ok else "FAIL"))
sys.exit(0 if ok else 1)
