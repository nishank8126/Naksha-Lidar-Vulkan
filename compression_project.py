"""compression_project.py - project the measured stream results to cache totals.

Turns the representative-block benchmark into whole-cache numbers using the
bytes-per-point each combination actually achieved, then compares against the
committed codec-NONE cache and the 1.14B target.

Nothing here multiplies a whole-dataset figure by the file count. Every number
is bytes/point x points.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.naksha_cache.reader import NakshaPointCacheReader
from gui.naksha_cache.index import project_paths

GIB = 1024 ** 3

REAL = r"H:\naksha-lidar 2\test_classified_highprecision.laz"
MANDI = r"H:\TESTING CONTIUES\FUNIVIA\NT219\MANDI_56.laz"

SIX_POINTS = 1_140_436_759

# ---- measured bytes/source point per stream (100 real 26.96M blocks) -------
# XYZ measured 12.00 B/pt raw; class 1.00; intensity 2.00; sid 4.00 (uint64).
# rgb absent in this dataset (no RGB in the source), so it is excluded from the
# per-point total rather than guessed.
BPP = {
    #            none    lz4     zstd1   zstd3
    "xyz":     (12.00, 11.03,  9.99,   8.38),     # delta / delta+zigzag / +shuf
    "class":   ( 1.00,  0.28,  0.14,   0.14),
    "int":     ( 2.00,  2.01,  1.93,   1.93),
    "sid":     ( 4.00,  1.78,  1.15,   1.16),
}
LABELS = ("NONE", "LZ4", "ZSTD-1", "ZSTD-3")


def total_bpp(i):
    return sum(v[i] for v in BPP.values())


def main():
    print("=" * 76)
    print("[COMPRESSION PROJECTION] measured bytes/source point -> cache totals")
    print("=" * 76)
    print("streams in this dataset: XYZ, classification, intensity, stable id")
    print("(RGB is absent from the source, so nothing is charged for it)")
    print()
    hdr = (f"{'codec':<8}{'XYZ':>7}{'CLASS':>7}{'INTEN':>7}{'SID':>7}"
           f"{'TOTAL B/pt':>12}")
    print(hdr)
    print("-" * len(hdr))
    tot = []
    for i, lab in enumerate(LABELS):
        v = (BPP["xyz"][i], BPP["class"][i], BPP["int"][i], BPP["sid"][i])
        t = total_bpp(i)
        tot.append(t)
        print(f"{lab:<8}{v[0]:>7.2f}{v[1]:>7.2f}{v[2]:>7.2f}{v[3]:>7.2f}{t:>12.2f}")
    print()
    print(f"target <= 18.00 B/pt (50% of the ~36 B/pt NONE baseline)")
    for i, lab in enumerate(LABELS):
        ok = "PASS" if tot[i] <= 18.0 else "FAIL"
        print(f"  {lab:<8} {tot[i]:>6.2f} B/pt  -> {ok}")

    # ---- real caches ----
    print()
    print("=" * 76)
    print("REAL DATA")
    print("=" * 76)
    for label, path in (("26.96M", REAL), ("MANDI_56", MANDI)):
        if not os.path.exists(path):
            print(f"{label}: cache not present")
            continue
        r = NakshaPointCacheReader(path, verify_crc=False, load_edits=False)
        pts = int(r.index.total_points)
        pc = os.path.getsize(r.pc_path)
        idx = os.path.getsize(project_paths(path)[0])
        r.close()
        print()
        print(f"--- {label} ---")
        print(f"  points            : {pts:,}")
        print(f"  NKPC (NONE)       : {pc:,} B = {pc / GIB:.3f} GiB")
        print(f"  NKIDX             : {idx:,} B = {idx / 1e6:.2f} MB")
        print(f"  measured B/pt     : {pc / pts:.2f}")
        for i, lab in enumerate(LABELS):
            new = tot[i] * pts
            print(f"  {lab:<7} projected : {new:>16,.0f} B = {new / GIB:>7.3f} GiB"
                  f"   ({pc / new:.2f}x smaller)")
        z = tot[LABELS.index('ZSTD-1')]
        print(f"  ZSTD-1 reduction  : {(1 - (z * pts) / pc) * 100:.1f}%")

    # ---- 1.14B ----
    print()
    print("=" * 76)
    print("1.14B PROJECTION  (six files, NOT multiplied again)")
    print("=" * 76)
    src_bytes = 9_396_989_845
    print(f"  total points            : {SIX_POINTS:,}")
    print(f"  total source            : {src_bytes:,} B = {src_bytes / GIB:.3f} GiB")
    for i, lab in enumerate(LABELS):
        new = tot[i] * SIX_POINTS
        print(f"  {lab:<7} projected NKPC : {new:>16,.0f} B = {new / GIB:>7.2f} GiB")
    # NKIDX scales from the measured 26.96M index
    r = NakshaPointCacheReader(REAL, verify_crc=False, load_edits=False)
    p26 = int(r.index.total_points)
    idx26 = os.path.getsize(project_paths(REAL)[0])
    r.close()
    idx_proj = idx26 / p26 * SIX_POINTS
    print(f"  projected NKIDX         : {idx_proj:>16,.0f} B = {idx_proj / 1e6:.1f} MB")
    print(f"  projected normals       : not adopted (no measurement yet)")
    print(f"  projected NAKSHASURF    : not designed yet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())