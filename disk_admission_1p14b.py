"""disk_admission_1p14b.py - CORRECTED real 1.14B six-file disk admission.

Replaces the arithmetic double-counting in phase16.log. Every figure is derived
from MEASURED RAW BYTES on MANDI_56, never by multiplying a whole-dataset
projection by the file count a second time.

    bytes_per_source_point = measured_bytes / measured_source_points
    projected_total        = bytes_per_source_point * TOTAL_points

The old error: 9.11 GiB was scaled by 4.243 to ~38.65 GiB for the ENTIRE
1.14B dataset, then treated as PER FILE and multiplied by six -> 231.9 GiB.
That number is never used again.

TEMP LIFECYCLE is read from gui/naksha_cache/builder.py, not assumed:
  build(paths) = scan_sources -> build_overview -> spatialize(paths)
                 -> finalize() -> write_overview_block -> commit_index
  cleanup_shards() is NOT called inside build(); the driver calls it after.
  spatialize() streams EVERY path into ONE shard set that lives until the
  driver cleans it, and finalize() reads those shards while the NKPC grows.
  => one shared six-file project is MONOLITHIC temp (CASE A), by construction.
"""
from __future__ import annotations

import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.naksha_cache.builder import SHARD_ITEMSIZE

GIB = 1024 ** 3
GB = 10 ** 9

# ---- MEASURED (MANDI_56, production build, codec NONE, 64K leaf, XY16) ----
M56_DIR = r"H:\TESTING CONTIUES\FUNIVIA\NT219"
M56 = "MANDI_56.laz"
M56_SOURCE_BYTES = 2_271_422_114        # os.path.getsize
M56_SOURCE_POINTS = 268_809_706          # laspy header
M56_NKPC_BYTES = 9_113_670_032          # os.path.getsize .nakshapc
M56_IDX_BYTES = 4_780_540               # os.path.getsize .nakshaidx
M56_TEMP_BYTES = 12_365_246_476         # builder stats['temp_bytes']
M56_WRITTEN_POINTS = 284_330_057        # includes LOD parents

SIX = ["MANDI_46.laz", "MANDI_47.laz", "MANDI_53.laz",
       "MANDI_54.laz", "MANDI_55.laz", "MANDI_56.laz"]

CHECKPOINT_GIB = 1.0
SAFETY_GIB = 20.0
RECOMMENDED_EXTRA_GIB = 25.0
def measure_six():
    """Exact per-file raw bytes and point counts from disk + LAZ headers."""
    import laspy
    rows, tot_b, tot_p = [], 0, 0
    for n in SIX:
        p = os.path.join(M56_DIR, n)
        b = os.path.getsize(p)
        with laspy.open(p) as f:
            pc = int(f.header.point_count)
        rows.append((n, b, pc))
        tot_b += b
        tot_p += pc
    return rows, tot_b, tot_p


def main():
    rows, SRC_BYTES, SRC_POINTS = measure_six()
    free = shutil.disk_usage("H:\\").free

    bpp_pc = M56_NKPC_BYTES / M56_SOURCE_POINTS
    bpp_idx = M56_IDX_BYTES / M56_SOURCE_POINTS
    bpp_tmp = M56_TEMP_BYTES / M56_SOURCE_POINTS
    lod_over = M56_WRITTEN_POINTS / M56_SOURCE_POINTS
    cache_ratio = M56_NKPC_BYTES / M56_SOURCE_BYTES

    PC_TOTAL = bpp_pc * SRC_POINTS
    IDX_TOTAL = bpp_idx * SRC_POINTS
    TMP_TOTAL = bpp_tmp * SRC_POINTS

    print("=" * 74)
    print("[CORRECTED REAL 1.14B DISK ADMISSION]")
    print("=" * 74)
    print(f"Exact source points        : {SRC_POINTS:,}")
    print(f"Exact source GiB           : {SRC_BYTES:,} B = {SRC_BYTES / GIB:.4f} GiB")
    print()
    print("--- measured basis (MANDI_56, one production build) ---")
    print(f"  source points            : {M56_SOURCE_POINTS:,}")
    print(f"  source bytes             : {M56_SOURCE_BYTES:,} B "
          f"= {M56_SOURCE_BYTES / GIB:.4f} GiB")
    print(f"  NKPC bytes               : {M56_NKPC_BYTES:,} B "
          f"= {M56_NKPC_BYTES / GIB:.4f} GiB")
    print(f"  NKIDX bytes              : {M56_IDX_BYTES:,} B "
          f"= {M56_IDX_BYTES / 1e6:.3f} MB")
    print(f"  temp shard bytes         : {M56_TEMP_BYTES:,} B "
          f"= {M56_TEMP_BYTES / GIB:.4f} GiB")
    print(f"  written_points (w/ LOD)  : {M56_WRITTEN_POINTS:,} "
          f"(LOD overhead x{lod_over:.4f})")
    print(f"  measured cache/source    : x{cache_ratio:.4f}")
    print()
    print("--- bytes per source point (the only scaling unit) ---")
    print(f"  NKPC  bytes/source_point : {bpp_pc:.6f}")
    print(f"  NKIDX bytes/source_point : {bpp_idx:.8f}")
    print(f"  TEMP  bytes/source_point : {bpp_tmp:.6f}   "
          f"(SHARD_ITEMSIZE={SHARD_ITEMSIZE})")
    print()
    print("--- projected TOTAL for the whole 1.14B dataset ---")
    print(f"  projected TOTAL NKPC     : {PC_TOTAL:,.0f} B "
          f"= {PC_TOTAL / GIB:.2f} GiB   <-- TOTAL, not per file")
    print(f"  projected TOTAL NKIDX    : {IDX_TOTAL:,.0f} B "
          f"= {IDX_TOTAL / 1e6:.2f} MB = {IDX_TOTAL / GIB:.5f} GiB")
    print(f"  projected TOTAL temp     : {TMP_TOTAL:,.0f} B "
          f"= {TMP_TOTAL / GIB:.2f} GiB")
    print()
    print("--- independent cross-check (source bytes x measured cache ratio) ---")
    xc = SRC_BYTES * cache_ratio
    print(f"  {SRC_BYTES:,} x {cache_ratio:.4f} = {xc:,.0f} B = {xc / GIB:.2f} GiB")
    print(f"  delta vs bytes/point projection: "
          f"{abs(xc - PC_TOTAL) / PC_TOTAL * 100:.2f}%  (independent agreement)")
    print()
    print("--- ACTUAL builder temp lifecycle (read from builder.py) ---")
    print("  build(paths): scan_sources -> build_overview -> spatialize(paths)")
    print("               -> finalize() -> write_overview_block -> commit_index")
    print("  cleanup_shards() is NOT inside build(); the driver calls it after.")
    print("  spatialize() streams EVERY path into ONE shard set held on disk;")
    print("  finalize() reads those shards WHILE the NKPC grows to full size.")
    print("  => ONE shared six-file project is MONOLITHIC: CASE A.")
    print()

    peakA = TMP_TOTAL + PC_TOTAL
    addA = peakA + IDX_TOTAL + CHECKPOINT_GIB * GIB + SAFETY_GIB * GIB
    headA = free - addA

    order = sorted(rows, key=lambda r: -r[2])
    print("--- SCENARIO B per-file order (largest first minimises the peak) ---")
    committed = 0.0
    peakB = 0.0
    peak_at = ""
    peak_rows = []
    for name, b, pc in order:
        shards = bpp_tmp * pc / GIB
        pcfile = bpp_pc * pc / GIB
        cur = committed + shards + pcfile
        peak_rows.append([name, pc, shards, pcfile, committed, cur, False])
        if cur > peakB:
            peakB = cur
            peak_at = name
        committed += pcfile
    for r in peak_rows:
        r[6] = (r[0] == peak_at)
    print(f"  {'file':<16}{'points':>14}{'shards GiB':>13}{'own NKPC GiB':>14}"
          f"{'committed GiB':>15}{'peak GiB':>10}")
    for name, pc, shards, pcfile, com, cur, isp in peak_rows:
        print(f"  {name:<16}{pc:>14,}{shards:>13.2f}{pcfile:>14.2f}"
              f"{com:>15.2f}{cur:>10.2f}{'  <-- PEAK' if isp else ''}")
    print(f"  peak occurs while building: {peak_at}")
    addB = peakB * GIB + IDX_TOTAL + CHECKPOINT_GIB * GIB + SAFETY_GIB * GIB
    headB = free - addB

    print("=" * 74)
    print("SCENARIO A - MONOLITHIC TEMP (actual for one shared project)")
    print("=" * 74)
    print(f"  Final NKPC               : {PC_TOTAL / GIB:>10.2f} GiB")
    print(f"  NKIDX                    : {IDX_TOTAL / GIB:>10.5f} GiB")
    print(f"  Peak temp (shards)       : {TMP_TOTAL / GIB:>10.2f} GiB")
    print(f"  PEAK simultaneous temp   : {peakA / GIB:>10.2f} GiB "
          f"(shards AND NKPC coexist during finalize)")
    print(f"  Checkpoint               : {CHECKPOINT_GIB:>10.2f} GiB")
    print(f"  Safety reserve           : {SAFETY_GIB:>10.2f} GiB")
    print(f"  Total additional required: {addA / GIB:>10.2f} GiB")
    print(f"  Free disk                : {free / GIB:>10.2f} GiB")
    print(f"  Headroom after build     : {headA / GIB:>10.2f} GiB")
    print(f"  RESULT: {'FAIL (short by %.2f GiB)' % (abs(headA) / GIB) if headA < 0 else 'PASS'}")
    print()

    print("=" * 74)
    print("SCENARIO B - TRUE SEQUENTIAL TEMP (six SEPARATE projects only)")
    print("=" * 74)
    print(f"  Final NKPC (all six)     : {PC_TOTAL / GIB:>10.2f} GiB")
    print(f"  NKIDX (all six)          : {IDX_TOTAL / GIB:>10.5f} GiB")
    print(f"  Peak simultaneous temp   : {peakB:>10.2f} GiB "
          f"(peak while building {peak_at})")
    print(f"  Checkpoint               : {CHECKPOINT_GIB:>10.2f} GiB")
    print(f"  Safety reserve           : {SAFETY_GIB:>10.2f} GiB")
    print(f"  Total additional required: {addB / GIB:>10.2f} GiB")
    print(f"  Free disk                : {free / GIB:>10.2f} GiB")
    print(f"  Headroom after build     : {headB / GIB:>10.2f} GiB")
    resB = ("PASS" if headB >= RECOMMENDED_EXTRA_GIB * GIB else
            ("PASS-BUT-LOW-HEADROOM" if headB >= 0 else "FAIL"))
    print(f"  RESULT: {resB}")
    print()

    print("=" * 74)
    print("SAFETY RULE")
    print("=" * 74)
    need = RECOMMENDED_EXTRA_GIB
    if headB < 0:
        print(f"  Need {abs(headB) / GIB:.2f} GiB just to fit, plus "
              f"{need:.0f} GiB recommended headroom")
        print(f"  -> free at least {abs(headB) / GIB + need:.2f} GiB additional")
    elif headB < need * GIB:
        short = need - headB / GIB
        print(f"  Passes with {headB / GIB:.2f} GiB headroom, but the rule asks "
              f"for {need:.0f} GiB.")
        print(f"  Shortfall {short:.2f} GiB -> free {short:.2f} GiB MORE "
              f"before starting.")
    else:
        print(f"  Headroom {headB / GIB:.2f} GiB already exceeds the "
              f"{need:.0f} GiB recommendation.")
    print()
    print("  DO NOT START PHASE 17 / the 1.14B build on this result.")
    print("  Codec stays NONE (production baseline); no LZ4/ZSTD change.")
    print()
    print("  CORRECTED: final cache is ~%.1f GiB TOTAL (not 231.9 GiB)."
          % (PC_TOTAL / GIB))
    print("  CORRECTED: source is %.3f GiB TOTAL (not ~940 GB)."
          % (SRC_BYTES / GIB))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())