"""shaded_normal_probe.py - STEP 2: does instant Shaded Class need a stored
NORMAL stream, and in which encoding?

Answers three questions on REAL committed cache blocks (no rebuild):

  Q1  Can points-only PCA normals replace the TIN vertex normals the current
      runtime shades with?  (If yes, NORMAL need NOT be a cache stream.)
  Q2  If normals ARE stored, which encoding is best: float32x3 (12 B/pt),
      snorm16x3 (6 B/pt), octahedral snorm16x2 (4 B/pt), oct8x2 (2 B/pt)?
  Q3  How many bytes do those encodings cost RAW and under NONE/LZ4/ZSTD-1/ZSTD-3?

Every reference normal comes from the SAME functions the runtime uses
(gui.shading_display._compute_face_normals / _compute_vertex_normals over
_do_triangulate), so the comparison cannot drift from production.  Neighbourhoods
come from a per-block cKDTree (the existing tile locality) - never O(N^2).

Run:
    venv\\Scripts\\python.exe shaded_normal_probe.py [--src ...] [--blocks 24]
"""
from __future__ import annotations

import argparse
import os
import random
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

for _n in ("stdout", "stderr"):
    _s = getattr(sys, _n, None)
    try:
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from scipy.spatial import cKDTree

from gui.naksha_cache import codecs
from gui.naksha_cache.reader import NakshaPointCacheReader
from compression_bench import REAL, MANDI, load_block
# The EXACT runtime normal math - imported, never re-implemented, so the
# reference normals cannot drift from what Shaded Class actually shades with.
from gui.shading_display import (_do_triangulate, _compute_face_normals,
                                 _compute_vertex_normals)

MB = 1024 ** 2
DEG = 180.0 / np.pi


def _unit(n):
    return n / (np.linalg.norm(n, axis=1, keepdims=True) + 1e-12)


def enc_f32x3(n):
    """12 bytes/point - exact float32 passthrough (the 'no encoding' ceiling)."""
    b = np.ascontiguousarray(n, dtype=np.float32).tobytes()
    d = np.frombuffer(b, dtype=np.float32).reshape(-1, 3).astype(np.float64)
    return b, _unit(d)


def enc_snorm16x3(n):
    """6 bytes/point - 3 x int16 normalised component."""
    q = np.clip(np.rint(n * 32767.0), -32767, 32767).astype("<i2")
    d = q.astype(np.float64) / 32767.0
    return q.tobytes(), _unit(d)


def _oct_encode(n):
    x, y, z = n[:, 0], n[:, 1], n[:, 2]
    inv = 1.0 / (np.abs(x) + np.abs(y) + np.abs(z) + 1e-12)
    px, py = x * inv, y * inv
    neg = z < 0.0
    ox = np.where(neg, (1.0 - np.abs(py)) * np.sign(px), px)
    oy = np.where(neg, (1.0 - np.abs(px)) * np.sign(py), py)
    return np.column_stack([ox, oy])


def _oct_decode(p):
    px, py = p[:, 0], p[:, 1]
    z = 1.0 - np.abs(px) - np.abs(py)
    neg = z < 0.0
    ox = np.where(neg, (1.0 - np.abs(py)) * np.sign(px), px)
    oy = np.where(neg, (1.0 - np.abs(px)) * np.sign(py), py)
    return _unit(np.column_stack([ox, oy, z]))


def enc_oct16x2(n):
    """4 bytes/point - octahedral projection, 2 x int16."""
    p = _oct_encode(n)
    q = np.clip(np.rint(p * 32767.0), -32767, 32767).astype("<i2")
    return q.tobytes(), _oct_decode(q.astype(np.float64) / 32767.0)


def enc_oct8x2(n):
    """2 bytes/point - octahedral projection, 2 x int8."""
    p = _oct_encode(n)
    q = np.clip(np.rint(p * 127.0), -127, 127).astype("<i1")
    return q.tobytes(), _oct_decode(q.astype(np.float64) / 127.0)


ENCODINGS = (
    ("f32x3 (12B)", 12, enc_f32x3),
    ("snorm16x3 (6B)", 6, enc_snorm16x3),
    ("oct16x2 (4B)", 4, enc_oct16x2),
    ("oct8x2 (2B)", 2, enc_oct8x2),
)


def ang_error_deg(a, b):
    """Angle between two unit normals, in degrees (a,b: (N,3))."""
    d = np.clip(np.sum(_unit(a) * _unit(b), axis=1), -1.0, 1.0)
    return np.arccos(d) * DEG


def ang_error_align(a, b):
    """Sign-aligned angular error: the intrinsic DIRECTION disagreement.

    Each pair is flipped onto the same hemisphere before measuring, so this
    isolates "do the two estimators describe the same surface plane" from
    "did they choose the same outward orientation".  Orientation is a separate,
    cheap decision (the production hemisphere rule), not a fidelity loss.
    """
    d = np.sum(_unit(a) * _unit(b), axis=1)
    bb = _unit(b).copy()
    bb[d < 0.0] *= -1.0
    return np.arccos(np.clip(np.abs(d), -1.0, 1.0)) * DEG


def _hemi_up(n):
    """Force the 'upward' hemisphere the hillshade actually consumes."""
    n = np.asarray(n, dtype=np.float64).copy()
    n[n[:, 2] < 0.0] *= -1.0
    return n


def pca_normals(pts, k):
    """Per-point PCA normal from the k nearest neighbours (tile-local KD-tree)."""
    n = pts.shape[0]
    if n < 3:
        return np.zeros((n, 3))
    k = min(k, n)
    tree = cKDTree(pts)
    idx = tree.query(pts, k=k, workers=-1)[1]
    if idx.ndim == 1:
        idx = idx[:, None]
    nb = pts[idx]
    c = nb - nb.mean(axis=1, keepdims=True)
    cov = np.einsum("nki,nkj->nij", c, c) / max(k, 1)
    _, v = np.linalg.eigh(cov)          # ascending eigenvalues
    return v[:, :, 0]                    # smallest-eigenvalue eigenvector


def pca_normals_radius(pts, radius, sample=4000):
    """Per-point PCA normal from a fixed radius (variable neighbour count)."""
    n = pts.shape[0]
    if n < 3:
        return np.zeros((n, 3))
    tree = cKDTree(pts)
    take = np.arange(0, n, max(1, n // sample))
    out = np.zeros((take.size, 3))
    for j, i in enumerate(take):
        ids = tree.query_ball_point(pts[i], radius)
        if len(ids) < 3:
            out[j] = (0.0, 0.0, 1.0)
            continue
        c = pts[ids] - pts[ids].mean(axis=0)
        _, v = np.linalg.eigh(c.T @ c / len(ids))
        out[j] = v[:, 0]
    return take, out


def _pick_lod0(r, want):
    """Dense leaf blocks (LOD 0) - real terrain, not scattered coarse samples.

    Coarse LOD levels hold spatially spread decimation samples whose Delaunay is
    meaningless, so normals must be measured on the leaf level the Shaded-Class
    TIN actually resembles.
    """
    blocks = r.index.blocks
    idx = [i for i in range(blocks.size)
           if int(blocks[i]["lod"]) == 0 and int(blocks[i]["point_count"]) > 0]
    random.Random(20240).shuffle(idx)
    return [(int(blocks[i]["node_id"]), 0) for i in idx[:want]]


def pct(vals, p):
    if len(vals) == 0:
        return float("nan")
    s = np.sort(np.asarray(vals, dtype=np.float64))
    return float(s[min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1))))])


def _row(name, errs):
    allv = np.concatenate([e for e in errs if len(e)]) if errs else np.zeros(0)
    print(f"{name:<22}{pct(allv,50):>9.3f}{pct(allv,95):>9.3f}"
          f"{(float(allv.max()) if allv.size else float('nan')):>9.3f}"
          f"{(float(allv.mean()) if allv.size else float('nan')):>9.3f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=REAL)
    ap.add_argument("--blocks", type=int, default=16)
    ap.add_argument("--max-pts", type=int, default=30000,
                    help="cap vertices per block for the PCA comparison")
    ap.add_argument("--ks", default="6,10,16,24")
    a = ap.parse_args()
    KS = [int(x) for x in a.ks.split(",") if x.strip()]

    print("=" * 78)
    print("[STEP 2] SHADED-CLASS NORMAL PROBE - real committed cache blocks")
    print("=" * 78)
    print(f"source        : {a.src}")
    print(f"blocks        : {a.blocks}   max-pts/block: {a.max_pts}")
    print(f"neighbour k   : {KS}")
    print(f"codec libs    : {codecs.versions()}")

    r = NakshaPointCacheReader(a.src, verify_crc=False, load_edits=False)
    picks = _pick_lod0(r, a.blocks)
    blocks = []
    for nid, lod in picks:
        b = load_block(r, nid, lod)
        if b and b["n"] >= 8:
            blocks.append(b)
    r.close()
    print(f"loaded        : {len(blocks)} blocks")

    err = {k: [] for k in KS}
    err_rad = []
    enc_err = {name: [] for name, _, _ in ENCODINGS}
    enc_raw = {name: 0 for name, _, _ in ENCODINGS}
    enc_comp = {name: {c: 0 for c in ("NONE", "LZ4", "ZSTD-1", "ZSTD-3")}
                for name, _, _ in ENCODINGS}
    face_vs_vert = []
    face_vs_vert_h = []
    err_hemi = {k: [] for k in KS}
    flat_err = {k: [] for k in KS}
    steep_err = {k: [] for k in KS}
    steep_frac = []
    total_pts = 0
    spacings = []

    for bi, b in enumerate(blocks):
        loc = np.asarray(b["xyz"], np.int64).reshape(-1, 3)
        w = (loc * np.asarray(b["scale"], np.float64)
             + np.asarray(b["origin"], np.float64))
        # dedup on the cache's exact tile-local quantisation, keep world z
        _, uidx = np.unique(loc[:, :2], axis=0, return_index=True)
        v = w[np.sort(uidx)]
        faces = _do_triangulate(v[:, :2])
        if faces.shape[0] < 4:
            continue
        fn = _compute_face_normals(v, faces)
        vn_raw = _compute_vertex_normals(v, faces, fn).astype(np.float64)
        vn = _hemi_up(vn_raw)
        total_pts += v.shape[0]

        fvc = fn.astype(np.float64)
        vn_face = vn_raw[faces].mean(axis=1)      # per-face smooth normal
        face_vs_vert.append(ang_error_align(fvc, vn_face))
        face_vs_vert_h.append(ang_error_deg(_hemi_up(fvc), _hemi_up(vn_face)))
        steep_frac.append(float(np.mean(np.abs(_unit(fvc)[:, 2]) < 0.30)))
        nfz = np.abs(_unit(fn)[:, 2])
        cnt = np.zeros(v.shape[0]); nfl = np.zeros(v.shape[0])
        for col in (0, 1, 2):
            np.add.at(cnt, faces[:, col], 1.0)
            np.add.at(nfl, faces[:, col], (nfz > 0.30).astype(np.float64))
        vflat = nfl > 0.5 * cnt          # majority of incident faces are flat
        sp = float(np.median(cKDTree(v[:, :2]).query(v[:, :2], k=2)[0][:, 1]))
        spacings.append(sp)

        nv = v.shape[0]
        sub = (np.arange(0, nv, max(1, nv // a.max_pts))
               if nv > a.max_pts else np.arange(nv))
        vsub, ref_sub = v[sub], vn[sub]
        fsub = vflat[sub]
        for k in KS:
            if vsub.shape[0] > k:
                pn = pca_normals(vsub, k)
                err[k].append(ang_error_align(ref_sub, pn))
                err_hemi[k].append(ang_error_deg(ref_sub, _hemi_up(pn)))
                if fsub.any():
                    flat_err[k].append(ang_error_align(ref_sub[fsub], pn[fsub]))
                if (~fsub).any():
                    steep_err[k].append(ang_error_align(ref_sub[~fsub], pn[~fsub]))
        take, pn_r = pca_normals_radius(vsub, max(2.0 * sp, 1e-6))
        if take.size:
            err_rad.append(ang_error_align(ref_sub[take], pn_r))

        for name, _, fn_enc in ENCODINGS:
            raw, dec = fn_enc(vn_raw)
            enc_err[name].append(ang_error_deg(vn_raw, dec))
            enc_raw[name] += len(raw)
            for cname, codec, lvl in (("NONE", codecs.CODEC_NONE, 0),
                                      ("LZ4", codecs.CODEC_LZ4, 0),
                                      ("ZSTD-1", codecs.CODEC_ZSTD, 1),
                                      ("ZSTD-3", codecs.CODEC_ZSTD, 3)):
                enc_comp[name][cname] += len(codecs.compress(raw, codec, lvl))
        print(f"  block {bi+1:>3}/{len(blocks)}  verts={nv:>7,} "
              f"tris={faces.shape[0]:>7,}  spacing={sp:.2f} m")

    report(a, KS, err, err_hemi, err_rad, enc_err, enc_raw, enc_comp,
           face_vs_vert, face_vs_vert_h, steep_frac, flat_err, steep_err,
           total_pts, spacings)
    return 0


def report(a, KS, err, err_hemi, err_rad, enc_err, enc_raw, enc_comp,
           face_vs_vert, face_vs_vert_h, steep_frac, flat_err, steep_err,
           total_pts, spacings):
    print()
    print("=" * 78)
    print("[smoothness] TIN face normal vs area-weighted vertex normal (deg)")
    print("=" * 78)
    print(f"{'method':<22}{'P50':>9}{'P95':>9}{'max':>9}{'mean':>9}")
    _row("face vs vertex dir", face_vs_vert)
    _row("face vs vertex hemi", face_vs_vert_h)
    print(f"steep faces (|nz|<0.30)  : {np.median(steep_frac)*100:.1f}%")

    print()
    print("=" * 78)
    print("[Q1] POINTS-ONLY PCA NORMAL vs TIN VERTEX NORMAL (deg)")
    print("=" * 78)
    print(f"{'method':<22}{'P50':>9}{'P95':>9}{'max':>9}{'mean':>9}")
    print("-- sign-aligned DIRECTION (can points describe the TIN plane?) --")
    for k in KS:
        _row(f"PCA k={k}", err[k])
    _row("PCA radius=2*spacing", err_rad)
    print("-- hemi-up ORIENTED (production hemisphere rule applied) --")
    for k in KS:
        _row(f"PCA k={k}", err_hemi[k])
    print(f"median tile spacing      : {pct(spacings,50):.2f} m")
    print("-- split by terrain flatness (majority incident faces |nz|>0.30) --")
    for k in KS:
        _row(f"PCA k={k} FLAT", flat_err[k])
    for k in KS:
        _row(f"PCA k={k} STEEP", steep_err[k])

    print()
    print("=" * 78)
    print("[Q2] NORMAL ENCODING ERROR vs REFERENCE  (angular error, deg)")
    print("=" * 78)
    print(f"{'encoding':<22}{'P50':>9}{'P95':>9}{'max':>9}{'mean':>9}")
    for name, _, _ in ENCODINGS:
        _row(name, enc_err[name])

    print()
    print("=" * 78)
    print(f"[Q3] NORMAL STREAM SIZE (raw + compressed), points {total_pts:,}")
    print("=" * 78)
    print(f"{'encoding':<16}{'B/pt':>7}{'NONE MB':>10}{'LZ4 MB':>9}"
          f"{'Z1 MB':>8}{'Z3 MB':>8}{'Z1 rat':>8}{'Z1 B/pt':>9}")
    for name, nominal, _ in ENCODINGS:
        raw = enc_raw[name]
        c1 = enc_comp[name]["ZSTD-1"] or 1
        print(f"{name:<16}{raw/total_pts:>7.2f}{enc_comp[name]['NONE']/MB:>10.2f}"
              f"{enc_comp[name]['LZ4']/MB:>9.2f}{enc_comp[name]['ZSTD-1']/MB:>8.2f}"
              f"{enc_comp[name]['ZSTD-3']/MB:>8.2f}{raw/c1:>8.2f}"
              f"{c1/total_pts:>9.2f}")


if __name__ == "__main__":
    raise SystemExit(main())