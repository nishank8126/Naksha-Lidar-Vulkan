"""Geometry integrity audit - Part 2: OVERVIEW + PER-LOD COVERAGE.

READ-ONLY. Establishes whether coarse LODs preserve the gross spatial
footprint, at 1m / 5m / 10m / 20m cell scales.
"""
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

SRC = os.path.join(ROOT, "test_classified_highprecision.laz")
SCALES = (1, 5, 10, 20)


def read_lod_xyz(reader, lod):
    """Reconstruct WORLD XYZ for every block at `lod`."""
    from gui.naksha_cache.format import ATTR_XYZ
    blocks = reader.index.blocks
    rows = blocks[np.asarray(blocks["lod"]) == lod]
    parts = []
    npts = 0
    for r in rows:
        t = reader.read_tile(int(r["node_id"]), int(lod),
                             only_attrs=(ATTR_XYZ,), apply_edits=False,
                             verify_crc=False, render_space=True)
        if t is None:
            continue
        # read_tile returns tile-LOCAL (decode_world_f32 drops origin), so the
        # world position must be rebuilt here. Same compensation _pack_block
        # performs in production.
        w = np.asarray(t["xyz"], np.float64) + np.asarray(t["origin"], np.float64)
        parts.append(w.astype(np.float32))
        npts += int(w.shape[0])
    if not parts:
        return None, 0, 0
    return np.concatenate(parts), len(rows), npts


def raster(x, y, bmin, span, cells):
    ix = np.clip(((x - bmin[0]) / span[0] * cells).astype(np.int32), 0, cells - 1)
    iy = np.clip(((y - bmin[1]) / span[1] * cells).astype(np.int32), 0, cells - 1)
    flat = np.zeros(cells * cells, dtype=bool)
    flat[iy.astype(np.int64) * cells + ix] = True
    return flat


def coverage(x, y, bmin, span, cells, src_occ):
    occ = raster(x, y, bmin, span, cells)
    src = raster(*(lambda a, b: (a, b))(  # placeholder, replaced below
        *_src_xy(bmin, span, cells)), bmin, span, cells)
    return occ, src


def _src_xy(bmin, span, cells):
    raise RuntimeError("unused")


def largest_missing_region(miss_occ, cells, cell_m):
    """Largest connected missing region (4-connectivity) + size in cells/m."""
    if not miss_occ.any():
        return 0, 0.0, 0.0
    try:
        from scipy import ndimage
        lab, n = ndimage.label(miss_occ.reshape(cells, cells))
        if n == 0:
            return 0, 0.0, 0.0
        sizes = ndimage.sum(miss_occ.reshape(cells, cells), lab, range(1, n + 1))
        k = int(np.argmax(sizes)) + 1
        ys, xs = np.where(lab == k)
        w = (int(xs.max() - xs.min()) + 1) * cell_m
        h = (int(ys.max() - ys.min()) + 1) * cell_m
        return int(sizes[k - 1]), w, h
    except Exception:
        return int(miss_occ.sum()), 0.0, 0.0

def main():
    import laspy
    from gui.naksha_cache.reader import NakshaPointCacheReader
    from PIL import Image

    with laspy.open(SRC) as f:
        h = f.header
        bmin = np.array([h.mins[0], h.mins[1]], np.float64)
        bmax = np.array([h.maxs[0], h.maxs[1]], np.float64)
    span = np.maximum(bmax - bmin, 1e-9)
    print(f"source XY bounds: {bmin.tolist()} -> {bmax.tolist()}  span={span.tolist()}")

    t0 = time.perf_counter()
    SX, SY = [], []
    with laspy.open(SRC) as f:
        for pts in f.chunk_iterator(1_000_000):
            SX.append(np.asarray(pts.x, np.float32))
            SY.append(np.asarray(pts.y, np.float32))
    SX = np.concatenate(SX); SY = np.concatenate(SY)
    print(f"source loaded: {SX.size:,}  ({sec(t0) if False else time.perf_counter()-t0:.1f}s)")

    reader = NakshaPointCacheReader(SRC, verify_crc=False, load_edits=False)

    # Source rasters at every scale.
    src_occ = {}
    for c in SCALES:
        src_occ[c] = raster(SX, SY, bmin, span, max(1, 1000 // c))
        print(f"source @{c:>2}m cells: {int(src_occ[c].sum()):>7,} / {src_occ[c].size:,}"
              f"  ({100.0*src_occ[c].sum()/src_occ[c].size:.2f}%)")

    print("\n" + "=" * 78)
    print("PER-LOD COVERAGE")
    print("=" * 78)
    hdr = f"{'LOD':<5}{'blocks':>7}{'points':>13}{'occupied1m':>12}"
    hdr += f"{'cov1m':>9}{'cov5m':>9}{'cov10m':>9}{'cov20m':>9}{'gap20m_m':>11}"
    print(hdr)
    results = {}
    for lod in (4, 3, 2, 1, 0):
        t0 = time.perf_counter()
        XYZ, nblocks, npts = read_lod_xyz(reader, lod)
        if XYZ is None:
            print(f"{lod:<5}{0:>7}{0:>13}  NO BLOCKS")
            continue
        row = {"points": npts, "blocks": nblocks}
        for c in SCALES:
            cells = max(1, 1000 // c)
            occ = raster(XYZ[:, 0], XYZ[:, 1], bmin, span, cells)
            src = src_occ[c]
            rep = int((src & occ).sum()) / max(1, int(src.sum()))
            row[f"cov{c}"] = rep * 100.0
            row[f"occ{c}"] = int(occ.sum())
            if c == 20:
                miss = src & ~occ
                n, w, hgt = largest_missing_region(miss, cells, 20)
                row["gap20_cells"] = n; row["gap20_w"] = w; row["gap20_h"] = hgt
        results[lod] = (row, XYZ)
        print(f"{lod:<5}{nblocks:>7}{npts:>13,}{row['occ1']:>12,}"
              f"{row['cov1']:>8.1f}%{row['cov5']:>8.1f}%{row['cov10']:>8.1f}%{row['cov20']:>8.1f}%"
              f"{row['gap20_w']:>8.0f}x{row['gap20_h']:<3.0f}")
        # PNGs at 1 m
        occ = raster(XYZ[:, 0], XYZ[:, 1], bmin, span, 1000)
        Image.fromarray((occ.reshape(1000, 1000) * 255).astype(np.uint8)).save(
            os.path.join(ROOT, f"lod{lod}_xy_occupancy.png"))
    print("=" * 78)

    # OVERVIEW block 1338 specifically
    print("\nOVERVIEW BLOCK 1338")
    ov = reader.index.blocks[np.asarray(reader.index.blocks["block_id"]) == 1338]
    print(f"  entry: node_id={int(ov['node_id'][0])} lod={int(ov['lod'][0])} "
          f"points={int(ov['point_count'][0]):,}")
    from gui.naksha_cache.format import ATTR_XYZ
    t = reader.read_tile(int(ov["node_id"][0]), int(ov["lod"][0]),
                         only_attrs=(ATTR_XYZ,), apply_edits=False,
                         verify_crc=False, render_space=True)
    W = np.asarray(t["xyz"], np.float64) + np.asarray(t["origin"], np.float64)
    print(f"  decoded points: {W.shape[0]:,}")
    print(f"  world X {W[:,0].min():.1f}..{W[:,0].max():.1f}  "
          f"Y {W[:,1].min():.1f}..{W[:,1].max():.1f}")
    for c in SCALES:
        cells = max(1, 1000 // c)
        occ = raster(W[:, 0], W[:, 1], bmin, span, cells)
        src = src_occ[c]
        rep = int((src & occ).sum()) / max(1, int(src.sum()))
        print(f"    cov@{c:>2}m = {rep*100:6.2f}%   occupied={int(occ.sum()):,}")
    occ1 = raster(W[:, 0], W[:, 1], bmin, span, 1000)
    Image.fromarray((occ1.reshape(1000, 1000) * 255).astype(np.uint8)).save(
        os.path.join(ROOT, "overview_xy_occupancy.png"))
    miss1 = src_occ[1] & ~occ1
    n, w, hgt = largest_missing_region(miss1, 1000 * 1000, 1.0)
    print(f"  largest missing 1m region: {n:,} cells  {w:.0f} x {hgt:.0f} m")
    src_occ_img = raster(SX, SY, bmin, span, 1000)
    Image.fromarray((src_occ_img.reshape(1000, 1000) * 255).astype(np.uint8)).save(
        os.path.join(ROOT, "source_laz_xy_occupancy.png"))
    print("\nPNGs written per LOD + overview + source")


if __name__ == "__main__":
    main()
