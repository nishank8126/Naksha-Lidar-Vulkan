"""Is INTRA-PAGE Morton ordering actually producing compact sub-ranges?

The microcell split assumes a fixed point count yields a spatially compact
range because the page is Morton-ordered. If that assumption is false, every
microcell stays large and deep zoom cannot improve. This measures it directly.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.naksha_cache import format as F          # noqa: E402
from gui.naksha_cache.reader import NakshaPointCacheReader   # noqa: E402
from gui.naksha_cache.readblock_new import read_block_once    # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "test_classified_highprecision.laz")


def main():
    r = NakshaPointCacheReader(SRC, verify_crc=False)
    idx = r.index
    blocks = idx.blocks
    row = int(np.argmax(blocks["point_count"]))
    e = blocks[row]
    hdr, streams, nb, raw = read_block_once(
        r.fh, e, only_attrs=(F.ATTR_XYZ,), verify_crc=False)
    local = streams[F.ATTR_XYZ]
    world = np.asarray(local, np.float64) * np.asarray(hdr["scale"]) \
        + np.asarray(hdr["origin"])
    n = world.shape[0]
    print("\n[MORTON INTRA-PAGE LOCALITY]")
    print("=" * 70)
    print(f"  page: {n:,} points, "
          f"{world[:, 0].max() - world[:, 0].min():.0f} m x "
          f"{world[:, 1].max() - world[:, 1].min():.0f} m")
    full_ext = float(np.hypot(world[:, 0].max() - world[:, 0].min(),
                              world[:, 1].max() - world[:, 1].min()))
    print(f"  full page extent      : {full_ext:.0f} m")
    step = max(1, n // 12)
    exts = []
    steps = []
    for i in range(0, n - step, step):
        sub = world[i:i + step]
        exts.append(float(np.hypot(sub[:, 0].max() - sub[:, 0].min(),
                                   sub[:, 1].max() - sub[:, 1].min())))
    for i in range(0, n - 1):
        d = float(np.hypot(*(world[i + 1, :2] - world[i, :2])))
        steps.append(d)
    exts = np.array(exts)
    steps = np.array(steps)
    print(f"  1/{len(exts)} of page       : median extent "
          f"{np.median(exts):.0f} m   (ideal ~{full_ext / np.sqrt(len(exts)):.0f} m)")
    print(f"  consecutive point step : median {np.median(steps):.2f} m  "
          f"p90 {np.percentile(steps, 90):.2f} m")
    ideal = full_ext / np.sqrt(len(exts))
    ratio = np.median(exts) / ideal
    print(f"\n  compactness ratio      : {ratio:.2f}x ideal")
    print("  VERDICT: " + ("Morton IS compact - sub-ranges shrink as expected"
                           if ratio < 2.5 else
                           "MORTON IS NOT COMPACT - sub-ranges stay large, "
                           "so microcells cannot shrink below the page"))
    r.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
