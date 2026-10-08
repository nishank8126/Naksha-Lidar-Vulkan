src_pts = 268_809_706
src_gib = 2.115
nkpc_gib = 9.11
nkkdx_mb = 4.78
peak_temp_gib = 12.37
total_pts = 1_140_436_759
ratio = total_pts / src_pts
proj_nkpc = nkpc_gib * ratio
proj_idx_gib = nkkdx_mb * ratio / 1024
proj_temp_single = peak_temp_gib * ratio
proj_temp_6 = proj_temp_single * 6
checkpoint = 1.0
safety = max(proj_nkpc * 0.1, 20.0)
best = proj_nkpc * 6 + proj_temp_single + checkpoint + safety
free = 75.27
print("[REAL 1.14B DISK ADMISSION]  (measured MANDI_56 build ratios)")
print(f"  Source already present:        {8.752:.3f} GiB  ({total_pts:,} pts exact)")
print(f"  Free working disk:             {free:.2f} GiB")
print(f"  Point scale ratio (1.14B/56):  {ratio:.3f}")
print(f"  Projected final NKPC (6 files): {proj_nkpc*6:.1f} GiB  ({proj_nkpc:.2f} GiB/file)")
print(f"  Projected index (6 files):      {proj_idx_gib*6:.3f} GiB")
print(f"  Projected temp peak (1 build):  {proj_temp_single:.1f} GiB  (shards concurrent w/ NKPC)")
print(f"  Projected temp (6 concurrent):  {proj_temp_6:.1f} GiB  (worst case)")
print(f"  Checkpoint:                     {checkpoint:.1f} GiB")
print(f"  Safety reserve:                 {safety:.1f} GiB")
print(f"  Additional required (SEQUENTIAL): {best:.1f} GiB")
print(f"  Available:                      {free:.2f} GiB")
if best <= free:
    print(f"  RESULT: PASS  (need {best:.1f} GiB <= {free:.2f} available)")
else:
    print(f"  RESULT: FAIL  (need {best:.1f} GiB > {free:.2f} available; free {best-free:.1f} GiB more)")