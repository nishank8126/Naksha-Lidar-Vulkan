"""normal_builder.py - canonical per-source-point normals, offline.

ONE canonical normal per ORIGINAL source point. LOD1/2/3/4 blocks are
decimated COPIES of the same source points, so they all reuse that single
normal. Triangulating per LOD would give one surface a different normal at
different zoom levels (lighting pops while zooming) and cost 5x the work.

WHY TILE-LOCAL + HALO
=====================
A global Delaunay over 26.96M points is unaffordable and is exactly what this
architecture exists to avoid. Each LOD0 spatial block is triangulated
independently over itself plus a thin read-only halo from neighbouring blocks.
The halo exists ONLY so points near a tile edge get a correct normal - their
true triangulation neighbours live across the boundary. Only the CENTRAL tile's
normals are kept, so no duplicate is ever written.

Halo width is DERIVED from measured local point spacing, not a magic constant.

NORMAL SEMANTICS
================
Face normal = cross(b-a, c-a) with the winding forced to POSITIVE Z, then
area-weighted accumulation and normalisation. This is the standard terrain
convention the legacy TIN hillshade used (its light vector has a +Z component,
so an up-facing ground normal is brightest). Deliberately NOT a PCA normal: PCA
is a different convention and would change already-accepted shading.

Memory: a memory-mapped int16 (N,2) array - 4 bytes per source point, ~103 MiB
for 27M - never float32x3 (309 MiB, and it would have to stay resident).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from .format import ATTR_SOURCE_ID, ATTR_XYZ
from .index import IndexReader, project_paths
from .normals import (NormalCacheWriter, oct_encode, source_fingerprint)

BUILDER_VERSION = 3

# Halo bounds in metres. Bounded on purpose: a giant halo would turn 393
# independent tiles back into one global mesh.
# MEASURED, not assumed: nearest-neighbour spacing on the real 27M dataset is
# ~0.50 m median / ~0.98 m P95. The NKIDX lod_spacing column reads ~6.05 m but
# that is a node/block-scale quantity, NOT point spacing - deriving the halo from
# it gave a 20 m halo (40x the real spacing) and a measured 204% halo overhead,
# i.e. double the triangulation work for no accuracy gain. So the spacing FACTOR
# governs; the floor is only a degenerate-geometry guard.
HALO_MIN_M = 4.0
HALO_MAX_M = 120.0
HALO_SPACING_FACTOR = 2.5

REQUIRED_FOR_SCORED = (ATTR_XYZ, ATTR_SOURCE_ID)
UP = np.array([0.0, 0.0, 1.0])


def estimate_spacing(xyz: np.ndarray, max_samples: int = 4000,
                     seed: int = 12345) -> Tuple[float, float]:
    """(median, p95) nearest-neighbour spacing from a bounded random sample.

    Sampled, not exact: an all-pairs query on 68k points per tile would dominate
    the build, and 4000 samples pin the median to well under a percent - which
    is all the halo rule needs.
    """
    n = xyz.shape[0]
    if n < 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    idx = rng.choice(n, size=min(max_samples, n), replace=False)
    q = xyz[idx][:, :2]
    d = np.sqrt(((q[:, None, :2] - q[None, :, :2]) ** 2).sum(axis=2))
    np.fill_diagonal(d, np.inf)
    nn = d.min(axis=1)
    nn = nn[np.isfinite(nn) & (nn > 0)]
    if nn.size == 0:
        return float("nan"), float("nan")
    return float(np.median(nn)), float(np.percentile(nn, 95))


def halo_width_m(spacing_median: float, spacing_p95: float) -> float:
    """Halo in metres from the measured spacing, clamped to a sane band."""
    s = spacing_p95 if np.isfinite(spacing_p95) else spacing_median
    if not np.isfinite(s) or s <= 0.0:
        s = 6.0                      # measured dataset median, last resort
    return float(np.clip(HALO_SPACING_FACTOR * s, HALO_MIN_M, HALO_MAX_M))


def build_neighbour_index(nodes, lod0_node_ids: List[int],
                         halo_m: float) -> Dict[int, List[int]]:
    """node -> LOD0 nodes whose bounds intersect its bounds grown by the halo.

    Bounds-only on the NKIDX node table, computed once: no per-tile scan of all
    blocks, which is what keeps neighbour discovery cheap.
    """
    ids = np.asarray(lod0_node_ids, dtype=np.int64)
    if ids.size == 0:
        return {}
    bmin = np.asarray(nodes["bounds_min"], dtype=np.float64)[ids]
    bmax = np.asarray(nodes["bounds_max"], dtype=np.float64)[ids]
    grown_min = bmin - halo_m
    grown_max = bmax + halo_m
    overlap = np.all((grown_min[:, None, :] <= bmax[None, :, :]) &
                     (grown_max[:, None, :] >= bmin[None, :, :]), axis=2)
    return {int(nid): [int(ids[j]) for j in np.flatnonzero(overlap[i])]
            for i, nid in enumerate(ids)}


def tile_normals(central_xyz: np.ndarray,
                 halo_xyz: np.ndarray) -> Tuple[np.ndarray, dict]:
    """Canonical normals for the CENTRAL points only.

    halo_xyz are appended AFTER the central points and exist purely so
    triangulation near the tile edge is correct; their normals are discarded.
    """
    from scipy.spatial import Delaunay

    n_central = central_xyz.shape[0]
    stats = {"triangles": 0, "duplicate_xy": 0, "degenerate_triangles": 0,
             "fallback_points": 0, "delaunay_ms": 0.0, "normal_ms": 0.0}

    pts = (np.concatenate([central_xyz, halo_xyz], axis=0)
           if halo_xyz.size else central_xyz)

    # Duplicate XY collapses Delaunay simplices; drop exact duplicates first so
    # the triangulation is well defined and the count stays observable.
    xy_uniq, uniq_idx = np.unique(pts[:, :2], axis=0, return_index=True)
    stats["duplicate_xy"] = int(pts.shape[0] - xy_uniq.shape[0])
    if xy_uniq.shape[0] < 3:
        stats["fallback_points"] = n_central
        return np.tile(UP, (n_central, 1)), stats

    t0 = time.perf_counter()
    simplices = Delaunay(xy_uniq).simplices        # indices into xy_uniq
    stats["delaunay_ms"] = (time.perf_counter() - t0) * 1000.0
    stats["triangles"] = int(simplices.shape[0])

    t1 = time.perf_counter()
    tri_pts = pts[uniq_idx]                        # back to ORIGINAL points
    a = tri_pts[simplices[:, 0]]
    b = tri_pts[simplices[:, 1]]
    c = tri_pts[simplices[:, 2]]
    cross = np.cross(b - a, c - a)
    flip = cross[:, 2] < 0.0
    if np.any(flip):
        b2 = b.copy()
        b[flip] = c[flip]
        c[flip] = b2[flip]
        cross = np.cross(b - a, c - a)
    area2 = np.linalg.norm(cross, axis=1)         # 2 * triangle area
    good = np.isfinite(area2) & (area2 > 1e-12)
    stats["degenerate_triangles"] = int((~good).sum())
    if not np.any(good):
        stats["fallback_points"] = n_central
        return np.tile(UP, (n_central, 1)), stats

    # cross already carries 2*area, so summing it IS area weighting.
    acc = np.zeros((tri_pts.shape[0], 3), dtype=np.float64)
    for col in range(3):
        np.add.at(acc, simplices[good, col], cross[good])

    out = np.zeros((pts.shape[0], 3), dtype=np.float64)
    np.add.at(out, uniq_idx, acc)                  # -> original point slots
    normals = out[:n_central]
    lengths = np.linalg.norm(normals, axis=1)
    bad = ~np.isfinite(lengths) | (lengths <= 1e-9)
    stats["fallback_points"] = int(bad.sum())
    if np.any(bad):
        normals[bad] = UP                          # documented degenerate case
        lengths[bad] = 1.0
    normals /= lengths[:, None]
    down = normals[:, 2] < 0.0
    if np.any(down):
        normals[down] = -normals[down]
    stats["normal_ms"] = (time.perf_counter() - t1) * 1000.0
    return normals, stats


class CanonicalNormalStore:
    """Memory-mapped packed oct16 canonical normals indexed by SOURCE ID.

    Single-source fast path: this dataset is one .laz, so the source id IS the
    index. The (source_file_id, source_point_index) identity is kept explicit in
    the sidecar header and in the phase-2 writer, so a future multi-source layout
    is a mapping change rather than a format rewrite.
    """

    def __init__(self, path: Path, source_point_count: int, mode: str = "w+"):
        self.path = Path(path)
        self.n = int(source_point_count)
        self.normals = np.memmap(self.path, dtype=np.int16, mode=mode,
                                 shape=(self.n, 2))
        self.mask_path = Path(str(path) + ".mask")
        self.mask = np.memmap(self.mask_path, dtype=np.uint8, mode=mode,
                              shape=(self.n,))

    def put(self, source_ids, packed) -> None:
        ids = np.asarray(source_ids, dtype=np.int64)
        self.normals[ids] = packed
        self.mask[ids] = 1

    def missing_count(self) -> int:
        return int((np.asarray(self.mask) == 0).sum())

    def flush(self) -> None:
        self.normals.flush()
        self.mask.flush()

    def close(self) -> None:
        if self.normals._mmap.closed and self.mask._mmap.closed:
            return
        try:
            self.flush()
        finally:
            for mapped in (self.normals, self.mask):
                if not mapped._mmap.closed:
                    mapped._mmap.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def checkpoint_path(tmp_path: Path) -> Path:
    return Path(str(tmp_path) + ".ckpt")


def save_checkpoint(path: Path, fp: bytes, done) -> None:
    """Resume record: which LOD0 tiles finished, plus the builder version and
    the source fingerprint, so stale work is never resumed."""
    import pickle
    payload = {"version": BUILDER_VERSION, "fingerprint": fp,
               "done": sorted(int(d) for d in done)}
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "wb") as fh:
        pickle.dump(payload, fh, protocol=4)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def normal_work_fingerprint(dataset: str, layout_fingerprint: int) -> bytes:
    """Bind resumable work to both source identity and the point-cache layout."""
    import hashlib
    return hashlib.blake2b(
        source_fingerprint(dataset) + int(layout_fingerprint).to_bytes(8, "little"),
        digest_size=16).digest()


def load_checkpoint(path: Path, fp: bytes) -> set:
    import pickle
    if not Path(path).is_file():
        return set()
    try:
        with open(path, "rb") as fh:
            p = pickle.load(fh)
    except Exception:
        return set()
    if p.get("version") != BUILDER_VERSION or p.get("fingerprint") != fp:
        return set()
    return set(int(d) for d in p.get("done", []))


def _phase1(idx, lod0_blocks, reader, store, fp, ckpt, done, progress_every,
            cancel=None, progress=None, already_done=0, total_tiles=0,
            on_block_ready=None, dataset=""):
    """PHASE 1: per LOD0 tile, tile-local Delaunay over central + halo.

    Bounded RAM by construction - one tile at a time, never the dataset.
    """
    lod0_nodes = [int(n) for n in np.asarray(lod0_blocks["node_id"])]
    first = reader.read_tile(lod0_nodes[0], 0, only_attrs=REQUIRED_FOR_SCORED,
                             apply_edits=False, verify_crc=False,
                             render_space=True)
    sp_med, sp_p95 = estimate_spacing(np.asarray(first["xyz"], float))
    halo_m = halo_width_m(sp_med, sp_p95)
    neighbours = build_neighbour_index(idx.nodes, lod0_nodes, halo_m)

    totals = {"central_points": 0, "halo_points": 0, "triangles": 0,
              "delaunay_ms": 0.0, "normal_ms": 0.0, "encode_ms": 0.0,
              "duplicate_xy": 0, "degenerate_triangles": 0,
              "fallback_points": 0, "halo_overheads": [], "tiles": 0}
    t_start = time.perf_counter()
    def deliver_node(nid):
        if on_block_ready is None:
            return
        # Every LOD for a node samples the same stable source identities. Send
        # exact packed normals, never an independently recomputed LOD normal.
        for lod in range(int(idx.header["lod_count"])):
            block = idx.find_block(nid, lod)
            if block is None:
                continue
            tile = reader.read_tile(nid, lod,
                                    only_attrs=(ATTR_XYZ, ATTR_SOURCE_ID),
                                    apply_edits=False, render_space=True)
            if tile is None:
                continue
            ids = np.asarray(tile["source_id"], dtype=np.int64)
            if np.any(store.mask[ids] == 0):
                continue  # overview may cover unbuilt nodes
            on_block_ready(str(dataset), int(idx.layout_fingerprint), nid, lod,
                           int(block["block_id"]), np.asarray(store.normals[ids]).copy())
    print("")
    print("[NORMAL BUILD]")
    print(f"halo_width_m: {halo_m:.1f}  (spacing median {sp_med:.3f} m, "
          f"P95 {sp_p95:.3f} m)  LOD0_tiles: {len(lod0_blocks)}")

    for i, blk in enumerate(lod0_blocks):
        # PHASE 6D: cancel BETWEEN tiles. The work files are left exactly as
        # they are, so a restart resumes instead of recomputing - that is what
        # "cancel-safe" means here, and it is why the loop never unwinds the
        # canonical store.
        if cancel is not None and cancel.is_set():
            print("[NORMAL BUILD] cancelled between tiles; work files kept "
                  "for resume", flush=True)
            store.flush()
            return {"cancelled": True, "totals": totals, "halo_m": halo_m,
                    "seconds": time.perf_counter() - t_start}
        nid = int(blk["node_id"])
        if nid in done:
            deliver_node(nid)
            continue
        t = reader.read_tile(nid, 0, only_attrs=REQUIRED_FOR_SCORED,
                             apply_edits=False, verify_crc=False,
                             render_space=True)
        xyz = np.asarray(t["xyz"], dtype=np.float64)
        sid = np.asarray(t["source_id"], dtype=np.int64)
        if (len(sid) != len(xyz) or np.any(sid < 0) or np.any(sid >= store.n)
                or len(np.unique(sid)) != len(sid)):
            raise ValueError(f"POINT CACHE IDENTITY ERROR: node {nid} has duplicate, "
                             "misaligned or out-of-range source IDs; rebuild the point cache")
        bmin, bmax = xyz.min(axis=0), xyz.max(axis=0)

        halo_parts = []
        for other in neighbours.get(nid, [nid]):
            if other == nid:
                continue
            nt = reader.read_tile(other, 0, only_attrs=(ATTR_XYZ,),
                                  apply_edits=False, verify_crc=False,
                                  render_space=True)
            if nt is None:
                continue
            nx = np.asarray(nt["xyz"], dtype=np.float64)
            # A TRUE RING, not the whole overlapping neighbour block: keep points
            # inside the grown box but OUTSIDE the central tile box, i.e. within
            # halo_m of the tile boundary. Selecting on the grown box alone kept
            # ~90% of each neighbour block (they are similar in size), which made
            # the halo overhead grow as the halo shrank - the opposite of what a
            # halo is for.
            in_grown = np.all((nx >= bmin - halo_m) & (nx <= bmax + halo_m), axis=1)
            in_central = np.all((nx >= bmin) & (nx <= bmax), axis=1)
            sel = in_grown & ~in_central
            if np.any(sel):
                halo_parts.append(nx[sel])
        halo_xyz = (np.concatenate(halo_parts, axis=0) if halo_parts
                    else np.empty((0, 3), dtype=np.float64))

        normals, st = tile_normals(xyz, halo_xyz)
        t_enc = time.perf_counter()
        store.put(sid, oct_encode(normals))
        deliver_node(nid)

        totals["central_points"] += int(xyz.shape[0])
        totals["halo_points"] += int(halo_xyz.shape[0])
        totals["halo_overheads"].append(halo_xyz.shape[0] / max(xyz.shape[0], 1))
        for k in ("triangles", "delaunay_ms", "normal_ms", "duplicate_xy",
                  "degenerate_triangles", "fallback_points"):
            totals[k] += st[k]
        totals["encode_ms"] += (time.perf_counter() - t_enc) * 1000.0
        totals["tiles"] += 1
        done.add(nid)
        if progress is not None:
            progress(int(already_done) + len(done), int(total_tiles)
                     or len(lod0_blocks), f"tile {len(done)}/{len(lod0_blocks)}")
        if (i + 1) % progress_every == 0 or i + 1 == len(lod0_blocks):
            save_checkpoint(ckpt, fp, done)
            el = time.perf_counter() - t_start
            eta = el / max(totals["tiles"], 1) * (len(lod0_blocks) - i - 1)
            print(f"tile: {i + 1} / {len(lod0_blocks)}   "
                  f"central: {totals['central_points']:,}   "
                  f"halo: {totals['halo_points']:,}   "
                  f"tri: {totals['triangles']:,}   "
                  f"del: {totals['delaunay_ms'] / 1000.0:.1f}s   "
                  f"nrm: {totals['normal_ms'] / 1000.0:.1f}s   "
                  f"enc: {totals['encode_ms'] / 1000.0:.1f}s   "
                  f"elapsed: {el / 60.0:.1f}m   ETA: {eta / 60.0:.1f}m")
    store.flush()
    ov = (np.asarray(totals["halo_overheads"])
          if totals["halo_overheads"] else np.zeros(1))
    print(f"halo overhead: median {float(np.median(ov)):.3f}  "
          f"P95 {float(np.percentile(ov, 95)):.3f}")
    return {"totals": totals, "halo_m": halo_m,
            "seconds": time.perf_counter() - t_start}


def build_normal_cache(dataset: str, *, resume: bool = True,
                       progress_every: int = 25, max_tiles=None,
                       write_sidecar: bool = True, priority_nodes=None,
                       cancel=None, progress=None, on_block_ready=None) -> dict:
    """Generate canonical normals, then write the .nakshanorm sidecar.

    PHASE 1  per LOD0 tile: tile-local Delaunay over central + halo,
             accumulate, oct16-encode the CENTRAL points into the canonical
             store. Bounded RAM: one tile at a time, never the dataset.

    PHASE 2  per NKPC block (ALL LODs) in block-id order: look each point's
             ATTR_SOURCE_ID up in the canonical store and append its packed
             normal. No triangulation here - LOD1..LOD4 are decimated COPIES and
             reuse the SAME canonical normal, which is what keeps lighting
             stable across zoom levels.
    """
    idx_path, _pc, _edit, _ = project_paths(str(dataset))
    idx = IndexReader(idx_path)
    blocks = idx.blocks
    n_blocks = int(blocks.size)
    # The sidecar directory index IS the block id (O(1) lookup, no key table).
    if not np.array_equal(np.asarray(blocks["block_id"], dtype=np.int64),
                          np.arange(n_blocks, dtype=np.int64)):
        raise ValueError("NKPC block ids are not contiguous 0..N-1")
    src_points = sum(int(s["point_count"]) for s in idx.sources)
    fp = normal_work_fingerprint(str(dataset), int(idx.layout_fingerprint))
    work = Path(str(dataset) + ".nakshanorm.work")
    work.parent.mkdir(parents=True, exist_ok=True)

    lod0_blocks = blocks[np.asarray(blocks["lod"]) == 0]
    lod0_blocks = lod0_blocks[np.argsort(np.asarray(lod0_blocks["block_id"]))]
    # PHASE 6D - VISIBLE-FIRST. The canonical normals a VISIBLE tile needs come
    # from its own LOD0 tile, so building the visible LOD0 nodes first is what
    # lets Shading appear long before the dataset is finished. The remaining
    # tiles keep their block order, so the build is still deterministic and a
    # resumed run produces the same bytes. This is an ORDER, not a subset: the
    # sidecar format requires every block, so nothing is skipped.
    if priority_nodes:
        prio = [int(n) for n in priority_nodes]
        rank = {n: i for i, n in enumerate(prio)}
        order = sorted(range(lod0_blocks.size),
                       key=lambda j: (rank.get(int(lod0_blocks[j]["node_id"]),
                                               len(prio) + j), j))
        lod0_blocks = lod0_blocks[np.asarray(order, dtype=np.int64)]
        print(f"[NORMAL BUILD] visible-first: {len(set(prio))} priority tile(s) "
              f"first, {lod0_blocks.size} total", flush=True)
    if max_tiles:
        lod0_blocks = lod0_blocks[:int(max_tiles)]

    ckpt = checkpoint_path(work)
    done = load_checkpoint(ckpt, fp) if resume else set()
    # Stale node IDs must not reuse an old mask or normal payload, even when
    # the unchanged LAS/LAZ now has a rebuilt spatial cache.
    store = CanonicalNormalStore(
        work, src_points, mode="r+" if (resume and work.exists() and done) else "w+")

    from .reader import NakshaPointCacheReader
    reader = NakshaPointCacheReader(str(dataset), verify_crc=False,
                                    load_edits=False)
    print("")
    print("[NORMAL BUILD]")
    print(f"source_points: {src_points:,}   blocks: {n_blocks}   "
          f"LOD0_tiles: {len(lod0_blocks)}")
    # STEP 9: a complete checkpoint must SKIP phase 1 outright. Recomputing the
    # halo here would re-read the first tile and then print halo statistics as if
    # they had just been measured, which is exactly the kind of fabricated
    # telemetry this project has already been bitten by.
    remaining = [int(n) for n in np.asarray(lod0_blocks["node_id"])
                 if int(n) not in done]
    if not remaining:
        print("Phase 1: COMPLETE FROM CHECKPOINT")
        print("canonical generation skipped")
        print("halo statistics: not recomputed on resumed run")
        p1 = {"totals": {"central_points": 0, "halo_points": 0,
                          "triangles": 0, "delaunay_ms": 0.0,
                          "normal_ms": 0.0, "encode_ms": 0.0,
                          "duplicate_xy": 0, "degenerate_triangles": 0,
                          "fallback_points": 0, "halo_overheads": [],
                          "tiles": 0},
              "halo_m": float("nan"), "seconds": 0.0}
    else:
        p1 = _phase1(idx, lod0_blocks, reader, store, fp, ckpt, done,
                     progress_every, cancel=cancel, progress=progress,
                     already_done=len(done), total_tiles=len(lod0_blocks),
                     on_block_ready=on_block_ready, dataset=dataset)
        print(f"phase1_seconds: {p1['seconds']:.1f}")

    result = {"source_points": src_points, "halo_m": p1["halo_m"],
              "totals": p1["totals"], "phase1_seconds": p1["seconds"],
              "sidecar": None, "stored_normals": 0, "bytes": 0,
              "cancelled": bool(p1.get("cancelled"))}
    if p1.get("cancelled"):
        # Cancelled: keep `<dataset>.nakshanorm.work` and `.ckpt` so the next
        # run resumes. Nothing is published, so no partial sidecar can exist.
        print("[NORMAL BUILD] cancelled: work + checkpoint kept for resume; "
              "no sidecar published", flush=True)
        store.close()
        return result
    if not write_sidecar:
        store.close()
        return result

    # ---- PHASE 2 ---------------------------------------------------------
    missing = store.missing_count()
    if missing:
        print(f"[NORMAL BUILD] FAIL BUILD: {missing:,} source points have no "
              f"canonical normal")
        result["missing"] = missing
        result["reason"] = (f"POINT CACHE IDENTITY ERROR: {missing:,} source IDs have no "
                            "canonical normal; rebuild the point cache")
        store.close()
        return result

    t_w = time.perf_counter()
    # Phase 6C.4: stamp the POINT CACHE's layout fingerprint into the sidecar
    # header (0 when the point cache predates Phase 7A versioning, which is
    # reported as "unbound, readable" rather than rejected).
    layout_fp = 0
    try:
        layout_fp = int(getattr(idx, "layout_fingerprint", 0) or 0)
    except Exception:
        layout_fp = 0
    writer = NormalCacheWriter(str(dataset), source_file_count=len(idx.sources),
                               source_point_count=src_points,
                               layout_fingerprint=layout_fp)
    normals_mm = store.normals
    mask_mm = store.mask
    # PHASE 2 read shape - THIS is why the first attempt returned None:
    # reader.read_tile() returns None whenever ATTR_XYZ is not among the
    # decoded streams:
    #       xyz_local = streams.get(ATTR_XYZ)
    #       if xyz_local is None: return None
    # so only_attrs=(ATTR_SOURCE_ID,) makes it bail before the caller ever
    # sees the dict. (ATTR_XYZ, ATTR_SOURCE_ID) is the exact shape Phase 1
    # proved on all 393 LOD0 blocks, and XYZ is the dequantisation anchor for
    # every block.
    sid_attrs = (ATTR_XYZ, ATTR_SOURCE_ID)
    for bid in range(n_blocks):
        b = blocks[bid]
        sid = read_block_source_ids(reader, b, src_points, mask_mm)
        writer.add_block(bid, int(b["lod"]), np.asarray(normals_mm[sid]))
        if (bid + 1) % 200 == 0 or bid + 1 == n_blocks:
            print(f"  block {bid + 1} / {n_blocks}  "
                  f"normals={writer.stored_points:,}")
    path = writer.commit()
    store.close()
    result.update({"sidecar": str(path), "stored_normals": writer.stored_points,
                   "bytes": path.stat().st_size,
                   "write_seconds": time.perf_counter() - t_w})
    print(f"sidecar: {path}  ({result['bytes'] / 1048576:.1f} MiB, "
          f"{writer.stored_points:,} normals)")
    print("")
    return result


def main(argv=None) -> int:
    import argparse
    root = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(description="build the .nakshanorm cache")
    ap.add_argument("dataset", nargs="?",
                    default=str(root / "test_classified_highprecision.laz"))
    ap.add_argument("--max-tiles", type=int, default=None)
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--no-sidecar", action="store_true")
    args = ap.parse_args(argv)
    r = build_normal_cache(args.dataset, resume=not args.no_resume,
                          max_tiles=args.max_tiles,
                          write_sidecar=not args.no_sidecar)
    if r.get("missing"):
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())


# ---------------------------------------------------------------------------
# Phase 2 block read (extracted so it is unit-testable and so the None case
# can never degrade back into a TypeError).
# ---------------------------------------------------------------------------
SID_ATTRS = (ATTR_XYZ, ATTR_SOURCE_ID)


def read_block_source_ids(reader, b, src_points: int, mask,
                          sid_attrs=SID_ATTRS) -> np.ndarray:
    """Read and fully validate ONE block's stable source ids.

    Returns int64 source ids, exactly ``point_count`` of them, all inside
    ``[0, src_points)`` and all marked present in the canonical store.

    Why the explicit attrs: ``NakshaPointCacheReader.read_tile`` returns None
    as soon as ``ATTR_XYZ`` is missing from the decoded streams::

        xyz_local = streams.get(ATTR_XYZ)
        if xyz_local is None:
            return None

    so ``only_attrs=(ATTR_SOURCE_ID,)`` makes it bail before the caller ever
    sees the dict - which is exactly the TypeError this function now replaces
    with a message that names the block, the LOD, the requested attrs and the
    expected count.
    """
    bid = int(b["block_id"])
    lod = int(b["lod"])
    expected = int(b["point_count"])
    t = reader.read_tile(int(b["node_id"]), lod, only_attrs=sid_attrs,
                         apply_edits=False, verify_crc=False, render_space=True)
    if t is None:
        raise RuntimeError(
            "[NORMAL SIDECAR PHASE2 READ ERROR]\n"
            f"block_id: {bid}\n"
            f"node_id: {int(b['node_id'])}\n"
            f"lod: {lod}\n"
            f"expected_point_count: {expected}\n"
            f"requested_attrs: {sid_attrs}\n"
            "reader_function: NakshaPointCacheReader.read_tile\n"
            "reader_return_type: NoneType\n"
            "reader_return_value: None\n"
            f"block_metadata: file_offset={int(b['file_offset'])} "
            f"stored_bytes={int(b['stored_bytes'])} "
            f"checksum={int(b['checksum'])}\n"
            "cause: read_tile returns None when ATTR_XYZ is not among the "
            "decoded streams, or the (node_id, lod) pair has no block.")
    raw_sid = t.get("source_id")
    if raw_sid is None:
        raise RuntimeError(
            f"[NORMAL SIDECAR PHASE2 READ ERROR] block_id={bid} lod={lod} "
            f"expected_point_count={expected} requested_attrs={sid_attrs}: "
            "block decoded but carries no ATTR_SOURCE_ID stream")
    sid = np.asarray(raw_sid, dtype=np.int64)
    if sid.shape[0] != expected:
        raise RuntimeError(
            f"[NORMAL SIDECAR PHASE2 COUNT MISMATCH] block_id={bid} lod={lod}: "
            f"read {sid.shape[0]} source ids, NKPC directory says {expected}")
    lo, hi = int(sid.min()), int(sid.max())
    if lo < 0 or hi >= src_points:
        oor = int(((sid < 0) | (sid >= src_points)).sum())
        raise RuntimeError(
            f"[NORMAL SIDECAR PHASE2 SOURCE ID RANGE] block_id={bid} lod={lod}: "
            f"min={lo} max={hi} out_of_range={oor} valid_range=[0,{src_points})")
    missing = int((np.asarray(mask[sid]) == 0).sum())
    if missing:
        raise RuntimeError(
            f"[NORMAL SIDECAR PHASE2 MISSING CANONICAL] block_id={bid} "
            f"lod={lod}: {missing} of {sid.shape[0]} source ids have no "
            "canonical normal")
    return sid
