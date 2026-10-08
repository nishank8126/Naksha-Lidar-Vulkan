"""Geometry integrity audit - Part 1/2: SOURCE ORACLE + POINT CONSERVATION.

READ-ONLY. Nothing here writes, rebuilds or modifies any cache or LAZ.

Establishes:
  - the original LAZ is complete and self-consistent
  - the NAKSHA cache conserves every source point exactly once
  - SOURCE_ID -> XYZ association is correct
"""
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

SRC = os.path.join(ROOT, "test_classified_highprecision.laz")
GRID = 1000


def sec(t):
    return f"{time.perf_counter() - t:.1f}s"


# --------------------------------------------------------------------------- #
# PART 1/2/3 - the original LAZ is the oracle                                  #
# --------------------------------------------------------------------------- #
def audit_source_laz():
    import laspy
    print("=" * 72)
    print("PART 1-3  SOURCE LAZ ORACLE")
    print("=" * 72)
    sz = os.path.getsize(SRC)
    with laspy.open(SRC) as f:
        hdr = f.header
        n_hdr = int(hdr.point_count)
        hmin = np.asarray(hdr.mins, float)
        hmax = np.asarray(hdr.maxs, float)
    print(f"source file          : {os.path.basename(SRC)}")
    print(f"file size            : {sz:,} bytes")
    print(f"header point count   : {n_hdr:,}")
    if n_hdr != 26_960_750:
        print(f"  !! EXPECTED 26,960,750, header says {n_hdr:,}")

    t0 = time.perf_counter()
    xs, ys, zs = [], [], []
    n_dec = 0
    bad = 0
    with laspy.open(SRC) as f:
        for pts in f.chunk_iterator(1_000_000):
            x = np.asarray(pts.x, dtype=np.float64)
            y = np.asarray(pts.y, dtype=np.float64)
            z = np.asarray(pts.z, dtype=np.float64)
            finite = np.isfinite(x) & np.isfinite(y) & np.isfinite(z)
            bad += int((~finite).sum())
            xs.append(x[finite].astype(np.float32))
            ys.append(y[finite].astype(np.float32))
            zs.append(z[finite].astype(np.float32))
            n_dec += int(len(x))
    X = np.concatenate(xs)
    Y = np.concatenate(ys)
    Z = np.concatenate(zs)
    del xs, ys, zs
    print(f"actual decoded count : {n_dec:,}  ({sec(t0)})")
    print(f"header == decoded    : {'YES' if n_dec == n_hdr else 'NO'}")
    print(f"non-finite XYZ       : {bad:,}")
    print(f"X  min/max           : {X.min():.3f} / {X.max():.3f}")
    print(f"Y  min/max           : {Y.min():.3f} / {Y.max():.3f}")
    print(f"Z  min/max           : {float(np.min(Z)):.3f} / {Z.max():.3f}")
    print(f"XY span              : {X.max() - X.min():.3f} x "
          f"{Y.max() - Y.min():.3f}")
    print(f"Z span               : {Z.max() - float(np.min(Z)):.3f}")
    print(f"header bounds        : {np.round(hmin, 3).tolist()} -> "
          f"{np.round(hmax, 3).tolist()}")

    # ---- occupancy oracle (XY only, no RGB/classification) --------------
    t0 = time.perf_counter()
    bmin = np.array([X.min(), Y.min()], np.float64)
    bmax = np.array([X.max(), Y.max()], np.float64)
    span = np.maximum(bmax - bmin, 1e-9)
    def raster(x, y):
        ix = np.clip(((x - bmin[0]) / span[0] * GRID).astype(np.int32),
                     0, GRID - 1)
        iy = np.clip(((y - bmin[1]) / span[1] * GRID).astype(np.int32),
                     0, GRID - 1)
        flat = np.zeros(GRID * GRID, dtype=bool)
        flat[iy.astype(np.int64) * GRID + ix] = True
        return flat
    src_occ = raster(X, Y)
    occ = int(src_occ.sum())
    print(f"\nsource occupied cells: {occ:,} / {GRID * GRID:,} "
          f"({100.0 * occ / (GRID * GRID):.2f}%)  ({sec(t0)})")
    return dict(X=X, Y=Y, Z=Z, bmin=bmin, bmax=bmax, raster=raster,
                src_occ=src_occ, n_hdr=n_hdr, n_dec=n_dec, bad=bad)


# --------------------------------------------------------------------------- #
# PART 4/5/12 - cache point conservation + SOURCE_ID -> XYZ fidelity            #
# --------------------------------------------------------------------------- #
def audit_cache(s):
    from gui.naksha_cache.reader import NakshaPointCacheReader
    from gui.naksha_cache.format import ATTR_XYZ, ATTR_SOURCE_ID
    print("\n" + "=" * 72)
    print("PART 4/5/12  CACHE POINT CONSERVATION + XYZ FIDELITY")
    print("=" * 72)
    reader = NakshaPointCacheReader(SRC, verify_crc=False, load_edits=False)
    blocks = reader.index.blocks
    lods = np.asarray(blocks["lod"])
    # Source-detail blocks are LOD0 ONLY: every finer-coarse block is a
    # decimated COPY, and the overview is a duplicate representation.
    lod0 = blocks[lods == 0]
    print(f"blocks total         : {blocks.size}")
    print(f"LOD0 blocks          : {lod0.size}")
    print(f"LOD0 point_count sum : {int(np.sum(lod0['point_count'])):,}")

    seen = np.zeros(s["n_hdr"], dtype=bool)
    dup = 0
    oor = 0
    cx_parts, cy_parts, sid_parts = [], [], []
    t0 = time.perf_counter()
    for r in lod0:
        t = reader.read_tile(int(r["node_id"]), 0,
                             only_attrs=(ATTR_XYZ, ATTR_SOURCE_ID),
                             apply_edits=False, verify_crc=False,
                             render_space=True)
        if t is None:
            continue
        sid = np.asarray(t["source_id"], dtype=np.int64)
        oor += int(((sid < 0) | (sid >= s["n_hdr"])).sum())
        ok = (sid >= 0) & (sid < s["n_hdr"])
        sids = sid[ok]
        dup += int(seen[sids].sum())
        seen[sids] = True
        local = np.asarray(t["xyz"], np.float64)[ok] + np.asarray(t["origin"], np.float64)
        cx_parts.append(local.astype(np.float32))
        sid_parts.append(sids.astype(np.int32))
    CX = np.concatenate(cx_parts)
    CSID = np.concatenate(sid_parts)
    del cx_parts, sid_parts
    missing = int((~seen).sum())
    print(f"\nunique source points : {int(seen.sum()):,}   ({sec(t0)})")
    print(f"missing source IDs   : {missing:,}   {'OK' if missing == 0 else 'FAIL'}")
    print(f"duplicate source IDs : {dup:,}   {'OK' if dup == 0 else 'FAIL'}")
    print(f"out-of-range IDs     : {oor:,}   {'OK' if oor == 0 else 'FAIL'}")
    return reader, blocks, CX, CSID, seen

if __name__ == "__main__":
    st = audit_source_laz()
    _rd, _bl, CX, CSID, _seen = audit_cache(st)

    print("\n" + "=" * 72)
    print("PART 5/12  XYZ FIDELITY  (cached world XYZ vs original LAZ XYZ)")
    print("=" * 72)
    rng = np.random.default_rng(12345)
    n = CX.shape[0]
    idx = rng.choice(n, size=min(1_000_000, n), replace=False)
    idx.sort()
    dx = CX[idx, 0].astype(np.float64) - st["X"][CSID[idx]]
    dy = CX[idx, 1].astype(np.float64) - st["Y"][CSID[idx]]
    dz = CX[idx, 2].astype(np.float64) - st["Z"][CSID[idx]]
    for nm, d in (("dx", dx), ("dy", dy), ("dz", dz)):
        ad = np.abs(d)
        print(f"{nm}: max={ad.max():.6f} mean={ad.mean():.6f} P95={np.percentile(ad, 95):.6f} P99={np.percentile(ad, 99):.6f}")
    print(f"sampled {len(idx):,} points")
    print("\nfirst 10 SOURCE_ID -> (cached XYZ | original XYZ):")
    for i in idx[:10]:
        sid = int(CSID[i])
        print(f"  sid={sid:>9,} cache=({CX[i,0]:.3f},{CX[i,1]:.3f},{CX[i,2]:.3f}) src=({st['X'][sid]:.3f},{st['Y'][sid]:.3f},{st['Z'][sid]:.3f})")

    print("\n" + "=" * 72)
    print("PART 6  XY COVERAGE  (1000x1000, source vs LOD0)")
    print("=" * 72)
    cache_occ = st["raster"](CX[:, 0], CX[:, 1])
    s_occ = st["src_occ"]
    miss = int((s_occ & ~cache_occ).sum())
    extra = int((~s_occ & cache_occ).sum())
    print(f"source occupied cells : {int(s_occ.sum()):,}")
    print(f"cache LOD0 occupied   : {int(cache_occ.sum()):,}")
    print(f"source occupied but cache EMPTY : {miss:,}")
    print(f"cache occupied but source EMPTY : {extra:,}")
    tot = int(s_occ.sum())
    print(f"source footprint covered by cache: {100.0 * (tot - miss) / tot:.4f}%")
    print(f"XOR mismatch cells    : {int((s_occ ^ cache_occ).sum()):,}")
    try:
        from PIL import Image
        Image.fromarray((s_occ.reshape(GRID, GRID) * 255).astype(np.uint8)).save(os.path.join(ROOT, "source_laz_xy_occupancy.png"))
        Image.fromarray((cache_occ.reshape(GRID, GRID) * 255).astype(np.uint8)).save(os.path.join(ROOT, "cache_lod0_xy_occupancy.png"))
        diff = (s_occ ^ cache_occ).reshape(GRID, GRID)
        Image.fromarray(((~diff) * 255).astype(np.uint8)).save(os.path.join(ROOT, "coverage_diff.png"))
        print("\nPNGs written: source_laz_xy_occupancy.png, cache_lod0_xy_occupancy.png, coverage_diff.png")
    except Exception as exc:
        print(f"PNG write skipped: {exc!r}")
    print("=" * 72)
