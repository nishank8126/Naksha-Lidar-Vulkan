"""Stage 3: HEADER-ONLY discovery of NT219 primaries + Part 18 disk admission."""
import os
import shutil

NT219 = r"H:\TESTING CONTIUES\FUNIVIA\NT219"

free = shutil.disk_usage("H:").free
print(f"[DISK] free H: = {free/1e9:.2f} GB ({free:,} bytes)")

import laspy

prim = []
for f in sorted(os.listdir(NT219)):
    if not f.lower().endswith(".laz"):
        continue
    low = f.lower()
    if "backup" in low or "deleted" in low or "removed" in low:
        continue
    prim.append(f)

tot = 0
sz = 0
print("\n[STAGE 3 - NT219 primaries, header only]")
for f in prim:
    p = os.path.join(NT219, f)
    s = os.path.getsize(p)
    sz += s
    rd = laspy.open(p)
    h = rd.header
    rd.close()
    n = int(h.point_count)
    tot += n
    pf = h.point_format
    dims = set(str(d).lower() for d in pf.dimension_names)
    has_rgb = "rgb" in dims or ("red" in dims)
    has_cls = "classification" in dims
    vf = h.version
    try:
        vstr = ".".join(str(x) for x in vf)
    except Exception:
        vstr = str(vf)
    bmin = tuple(float(v) for v in h.mins)
    bmax = tuple(float(v) for v in h.maxs)
    print(f"  {f}: pts={n:,} sz={s/1e9:.3f}GB pf={pf.id} v={vstr} "
          f"rgb={has_rgb} cls={has_cls} "
          f"bounds=({bmin[0]:.1f},{bmin[1]:.1f},{bmin[2]:.1f})-"
          f"({bmax[0]:.1f},{bmax[1]:.1f},{bmax[2]:.1f})")

print(f"\n[TOTAL primaries] files={len(prim)} points={tot:,} "
      f"({tot/1e9:.3f} B) bytes={sz/1e9:.2f}GB")

# Part 18 - REAL 1.14B DISK ADMISSION (scaled from measured 26.96M cache)
NKPC_BYTES_26 = 750_976_278
NKIDX_BYTES_26 = 598_844
PTS_26 = 26_960_750
nkpc_per_pt = NKPC_BYTES_26 / PTS_26
idx_per_pt = NKIDX_BYTES_26 / PTS_26

est_nkpc = tot * nkpc_per_pt
est_idx = tot * idx_per_pt
est_temp = est_nkpc * 1.2
checkpoint = 2.0 * 1e9
safety = 15.0 * 1e9
needed = est_nkpc + est_idx + est_temp + checkpoint + safety

print("\n[REAL 1.14B DISK ADMISSION]")
print(f"  Free disk            = {free/1e9:.2f} GB")
print(f"  Est final NKPC       = {est_nkpc/1e9:.1f} GB  (ratio {nkpc_per_pt:.2f} B/pt)")
print(f"  Est final NKIDX      = {est_idx/1e6:.1f} MB")
print(f"  Est temp peak        = {est_temp/1e9:.1f} GB  (1.2x NKPC, conservative)")
print(f"  Build checkpoint     = {checkpoint/1e9:.1f} GB")
print(f"  Safety reserve       = {safety/1e9:.1f} GB")
print(f"  Total needed         = {needed/1e9:.1f} GB")
print(f"  Result               = {'ACCEPT' if needed <= free else 'REFUSE'} "
      f"(free {free/1e9:.1f} GB vs needed {needed/1e9:.1f} GB)")
