"""shaded_tin_cost_probe.py - STEP 3 cost gate: how expensive is it to DERIVE the
shaded surface (Delaunay + face normals + vertex normals) at the sizes the instant
renderer would actually build per frame / per streamed tile?

If derivation is cheap enough to run on the render thread or async on tile load,
then even a tile-TIN architecture may NOT need a stored NORMAL stream - the normals
are regenerated from the resident positions.  This is a decisive lever for the
architecture decision, and unlike the GPU work it is fully measurable offline.

Uses the EXACT production functions (gui.shading_display) with numba fast paths.

Run:
    venv\\Scripts\\python.exe shaded_tin_cost_probe.py [--src ...] [--sizes 150000,1000000,3000000]
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time

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

from gui.naksha_cache.reader import NakshaPointCacheReader
from compression_bench import REAL, load_block
from gui.shading_display import (_do_triangulate, _compute_face_normals,
                                 _compute_vertex_normals, HAS_NUMBA)


def _lod0_ids(r):
    blocks = r.index.blocks
    idx = [i for i in range(blocks.size)
           if int(blocks[i]["lod"]) == 0 and int(blocks[i]["point_count"]) > 0]
    random.Random(20240).shuffle(idx)
    return [int(blocks[i]["node_id"]) for i in idx]


def gather_world(r, want):
    """Concatenate LOD-0 leaf blocks until we have >= `want` world-space points."""
    xyz = []
    got = 0
    for nid in _lod0_ids(r):
        b = load_block(r, nid, 0)
        if not b or b["n"] < 8:
            continue
        loc = np.asarray(b["xyz"], np.int64).reshape(-1, 3)
        w = loc * np.asarray(b["scale"], np.float64) + np.asarray(b["origin"], np.float64)
        _, uidx = np.unique(loc[:, :2], axis=0, return_index=True)
        xyz.append(w[np.sort(uidx)])
        got += len(xyz[-1])
        if got >= want:
            break
    if not xyz:
        return np.zeros((0, 3))
    return np.vstack(xyz)


def timeit(fn, *a, repeat=3):
    best = float("inf")
    for _ in range(repeat):
        t = time.perf_counter()
        out = fn(*a)
        best = min(best, time.perf_counter() - t)
    return best, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=REAL)
    ap.add_argument("--sizes", default="150000,1000000,3000000")
    ap.add_argument("--warm", type=int, default=20000)
    a = ap.parse_args()
    sizes = [int(x) for x in a.sizes.split(",") if x.strip()]

    print("=" * 78)
    print("[STEP 3] TIN DERIVATION COST - production numba functions")
    print("=" * 78)
    print(f"source        : {a.src}")
    print(f"HAS_NUMBA     : {HAS_NUMBA}")
    print(f"sizes         : {sizes}")

    r = NakshaPointCacheReader(a.src, verify_crc=False, load_edits=False)
    pool = gather_world(r, max(sizes))
    r.close()
    print(f"pool points   : {len(pool):,}  (LOD-0 leaves, world coords)")

    # numba warmup so the first timed call is not compile time
    w = pool[:max(3, a.warm)]
    fn0 = _compute_face_normals(w, _do_triangulate(w[:, :2]))
    _compute_vertex_normals(w, _do_triangulate(w[:, :2]), fn0)
    print()

    print(f"{'points':>10}{'tris':>12}{'delaunay':>11}{'face_n':>10}"
          f"{'vert_n':>10}{'total ms':>10}{'ms/1k pt':>10}{'pts/s':>12}")
    for P in sizes:
        if P > len(pool):
            print(f"{P:>10,}   (skipped - pool too small)")
            continue
        xyz = pool[:P]
        td, faces = timeit(_do_triangulate, xyz[:, :2])
        tf, fn = timeit(_compute_face_normals, xyz, faces)
        tv, _ = timeit(_compute_vertex_normals, xyz, faces, fn)
        tot = (td + tf + tv) * 1000.0
        pps = P / (td + tf + tv)
        print(f"{P:>10,}{faces.shape[0]:>12,}{td*1000:>10.1f}m"
              f"{tf*1000:>9.1f}m{tv*1000:>9.1f}m{tot:>10.1f}"
              f"{tot/(P/1000.0):>10.4f}{pps:>12,.0f}")

    print()
    print("Reading: 'total ms' is the full cost to turn resident POSITIONS into a")
    print("shaded surface for that many points (Delaunay + face + vertex normals).")
    print("If a 150K tile derives in a few ms, normals can be regenerated async on")
    print("load and NORMAL need not be a stored stream even for tile-TIN.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
