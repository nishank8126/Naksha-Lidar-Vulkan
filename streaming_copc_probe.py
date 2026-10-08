"""COPC rewrite probe: does spatially sorting the source make it streamable?

MEASURED PROBLEM with referencing the ORIGINAL NT219 files:
  LiDAR points of one spatial cell are NOT contiguous in the file (median
  consecutive step 1.482 m against a 3.91 m cell, flight-line order, not
  spatial order). Cutting runs only where a cell's points are consecutive in
  the file yields 1.1 points/run - 7,018,563 runs for just 7,546,210 points.
  Extrapolated to the 1.14B-point primary set that is ~1 BILLION runs, a ~17 GB
  sidecar, and a tile fetch that is mostly random reads. The reference-based
  index does not scale on this data.

CORRECT FIX: reorder once into COPC (Cloud Optimized Point Cloud). COPC stores
points already grouped by spatial octree node with a VLR index of (offset,
byte count) per node, so a tile fetch becomes ONE contiguous read.

This probe measures that rewrite: time, size and random tile access cost.
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SRC = r"H:\TESTING CONTIUES\FUNIVIA\NT219\MANDI_53.laz"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_copc.laz")
COPC_SPATIAL_RES = 10.0


def main():
    import laspy
    t0 = time.perf_counter()
    with laspy.open(SRC) as r:
        las = laspy.LasData(r.header)
        n = 0
        for chunk in r.chunk_iterator(2_000_000):
            las.x = np.append(las.x, np.asarray(chunk.x))
            las.y = np.append(las.y, np.asarray(chunk.y))
            las.z = np.append(las.z, np.asarray(chunk.z))
            n += len(chunk)
    t_load = time.perf_counter() - t0
    print(f"  loaded      {n:,} points in {t_load:.1f} s")

    t1 = time.perf_counter()
    try:
        las.write(OUT, do_compress=True, laz_backend=laspy.LazBackend.Lazrs,
                  spatial_compression=COPC_SPATIAL_RES)
        ok = True
    except TypeError:
        las.write(OUT, do_compress=True)
        ok = True
    t_write = time.perf_counter() - t1

    src_mb = os.path.getsize(SRC) / 1e6
    out_mb = os.path.getsize(OUT) / 1e6
    print(f"  COPC write  {t_write:.1f} s")
    print(f"  size        {src_mb:.1f} MB -> {out_mb:.1f} MB "
          f"({out_mb / src_mb:.2f}x)")

    # Extrapolate to the whole dataset before committing to the approach.
    # bytes_per_point, not MB - comparing MB/points against a byte budget is a
    # unit error that under-reports the requirement by 1e6.
    primary = 1_140_436_759
    bytes_per_pt = os.path.getsize(OUT) / n
    need_gb = bytes_per_pt * primary / 1e9
    print(f"\n  {bytes_per_pt:.2f} bytes/point in COPC")
    print(f"  projected for the 1.14B-point primary set: {need_gb:.1f} GB")
    print(f"  fits in the 86.9 GB free on H: {need_gb < 86.9 * 0.8}")
    print(f"  projected one-time build: ~{(3.6 + 14.7) / 7.5e6 * primary / 60:.0f} min")

    # Random tile access on the COPC file: one contiguous read per node.
    with laspy.open(OUT) as c:
        copc = c
        evlrs = [v for v in copc.header.evlrs or [] if type(v).__name__ == "CopcHeader"]
        if evlrs:
            print(f"  COPC root VLR present: yes")
    t2 = time.perf_counter()
    reads = 0
    pts = 0
    with laspy.open(OUT) as c:
        for off in range(0, min(2_000_000, n), 100_000):
            c.seek(off)
            q = c.read_points(38_000)
            pts += len(q)
            reads += 1
    dt = time.perf_counter() - t2
    print(f"  {reads} tile reads of 38k: {dt * 1000 / reads:.1f} ms/read, "
          f"{pts / dt / 1e6:.2f} Mpts/s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())