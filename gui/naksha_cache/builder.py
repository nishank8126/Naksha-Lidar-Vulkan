"""NakshaPointCacheBuilder: LAS/LAZ -> spatially partitioned .nakshapc.

The pipeline is deliberately TWO passes, because the order is the whole point
(Part 5):

    PASS 1  external spatialization
            stream chunk -> coarse shard -> append -> release
    PASS 2  finalization
            shard -> adaptive ~256K leaves -> Morton within leaf -> blocks + LOD

Merging these into "sort the whole file by Morton and write it" is the mistake
already disproven: a global Morton sort of a flight-line-ordered file yields
plausible-looking data that is NOT spatially addressable, and it needs the whole
file in RAM.

Peak RAM is bounded by (source chunk + one shard + output buffers). It never
scales with total points. Shard count is chosen from the point total so each
shard comfortably fits in memory, and open shard handles are LRU-capped
(Part 7) so the build never needs millions of file handles.
"""
import os
import time

import numpy as np

from .format import (ATTR_CLASSIFICATION, ATTR_INTENSITY, ATTR_POINT_SOURCE_ID,
                     ATTR_RETURN_NUMBER, ATTR_RGB, ATTR_SOURCE_ID, ATTR_XYZ,
                     BlockWriter, BLOCK_ENTRY, BUILD_FINALIZED, LEAF_TARGET_POINTS,
                     MAX_OPEN_SHARDS, NODE_ENTRY, OVERVIEW_TARGET_POINTS,
                     SOURCE_ENTRY, BLOCK_READY, STREAMABLE, TEMP_OVERVIEW,
                     morton2d, quantize_xyz, rgb_bits_for, source_fingerprint,
                     voxel_representative_ids)
from .microcells import (MICROCELL_ENTRY, build_microcells,
                        local_spacing_estimate)
from .global_stats import GlobalStats
from .lod_ladder import (DEFAULT_LOD_LEVELS, DEFAULT_RATIO as DEFAULT_LOD_RATIO,
                         area_spacing, build_ladder, occupied_area)
from .overview_merge import BoundedOverviewReducer
from .source_catalog import CatalogError, build_catalog
# Attribute names copied from the laspy record, in the order the builder reads
# them. A source missing any of these simply gets no stream (Part 9 attribute
# masks: absent data consumes zero bytes).
_LAS_XYZ = ("x", "y", "z")

class ShardWriter:
    """Bounded set of temporary shard files with an LRU open-handle cap.

    Shards are COARSE spatial buckets, not final tiles. They exist only to break
    a flight-line-ordered file into pieces small enough to sort in memory. One
    file per final tile would mean millions of handles; here the count is fixed
    by the shard fan-out and only MAX_OPEN_SHARDS are open at once (Part 7).
    """

    def __init__(self, directory: str, n_shards: int, prefix: str = "shard", session=None):
        self.dir = directory
        self.n = int(n_shards)
        self.prefix = prefix
        self.paths = [os.path.join(directory, f"{prefix}_{i:05d}.bin")
                      for i in range(self.n)]
        self.counts = np.zeros(self.n, dtype=np.int64)
        self.bytes_written = 0
        self._open = {}          # shard -> file handle (insertion-ordered LRU)
        self.session = session
        self.itemsize = SHARD_ITEMSIZE
        self._dirty = set()
        os.makedirs(directory, exist_ok=True)

    def _fh(self, i):
        fh = self._open.get(i)
        if fh is not None:
            return fh
        if len(self._open) >= MAX_OPEN_SHARDS:
            # Evict the least-recently-used handle. Keeping every shard open is
            # exactly what Part 7 forbids.
            old = next(iter(self._open))
            self._open.pop(old).close()
        fh = open(self.paths[i], "ab")
        self._open[i] = fh
        return fh

    def append(self, i: int, payload: bytes, count: int):
        fh = self._fh(int(i))
        fh.write(payload)
        self.counts[int(i)] += count
        self.bytes_written += len(payload)
        self._dirty.add(int(i))
        if self.session:
            self.session.append_segment(int(i), payload)

    def flush_all(self):
        for fh in self._open.values():
            fh.flush()
        # Evicted LRU handles have flushed, but closing is not a durability
        # guarantee. Fsync every dirty shard at the checkpoint boundary.
        for i in self._dirty:
            if i in self._open:
                os.fsync(self._open[i].fileno())
            else:
                with open(self.paths[i], "r+b") as f:
                    os.fsync(f.fileno())
        self._dirty.clear()

    def close_all(self):
        for fh in self._open.values():
            try:
                fh.close()
            except Exception:
                pass
        self._open.clear()

    def used_shards(self):
        return int((self.counts > 0).sum())
# Shard record layout, written during PASS 1 and read back during PASS 2.
# Each shard is a flat sequence of fixed-width records so a shard can be
# np.memmap-ed and sliced without parsing.
#   x,y,z            float64 x3   (authoritative world, kept through the sort)
#   source_id        uint64       (stable identity: source base + point index)
#   classification   uint8
#   intensity        uint16
#   return_number    uint8
#   number_of_returns uint8
#   point_source_id  uint16
#   rgb              uint16 x3    (source width preserved - Part 6/12)
SHARD_DTYPE = np.dtype([
    ("x", "f8"), ("y", "f8"), ("z", "f8"),
    ("source_id", "u8"),
    ("classification", "u1"), ("intensity", "u2"),
    ("return_number", "u1"), ("number_of_returns", "u1"),
    ("point_source_id", "u2"),
    ("red", "u2"), ("green", "u2"), ("blue", "u2"),
    ("pad", "u1", 1),
])
SHARD_ITEMSIZE = SHARD_DTYPE.itemsize

def choose_shard_count(total_points: int, bytes_per_record: int = SHARD_ITEMSIZE,
                       target_shard_bytes: int = 96 << 20,
                       min_shards: int = 64, max_shards: int = 16384) -> int:
    """Pick a shard fan-out so ONE shard fits comfortably in RAM.

    Each shard must be sortable in memory during PASS 2, and small shards also
    subdivide MORE EVENLY in PASS 2. With a 384 MB target the 26.96M dataset
    produced only 16 shards, so each shard was subdivided into ~6 cells and the
    resulting leaves reached 1360 m across - far too coarse, which is why deep
    zoom stopped reducing the working set at x8. A 96 MB target yields ~64
    shards and keeps leaves spatially small.

    The count is derived from the actual point total and the measured record
    size, so peak RAM stays fixed whether the project has 3 million or 2.49
    billion points.
    """
    est = int(np.ceil(total_points * bytes_per_record / max(target_shard_bytes, 1)))
    n = int(np.clip(est, min_shards, max_shards))
    # Round up to a power of two so shard ids are a cheap bit-mask of the
    # Morton code rather than a division per point.
    return 1 << int(np.ceil(np.log2(max(n, 2))))

def shard_of(x, y, bounds_min, span, n_shards_bits):
    """Morton-based shard assignment: the PREFIX, i.e. a coarse spatial bucket.

    Morton rather than a linear grid because neighbouring shards then touch in
    memory too, which improves PASS 2 read locality for free.

    `morton2d(bits=k)` emits a 2k-bit code whose HIGH bits are the coarse
    position. The shard id therefore has to be the top `n_shards_bits` of that
    code. Masking with `code & (2**n_shards_bits - 1)` instead kept the LEAST
    significant bits, which are the fine position: a shard then collected every
    point on a 125 m-periodic lattice across the whole survey rather than one
    compact block. Measured on a uniform 1000 m test field, shard 0 held 122
    points spanning 15.6 m x 523.8 m instead of ~3125 points in a 125 m square
    - and because PASS 2 subdivides each shard independently, every resulting
    leaf inherited that whole-dataset footprint.
    """
    code = morton2d(x, y, float(bounds_min[0]), float(span[0]),
                    float(bounds_min[1]), float(span[1]), bits=n_shards_bits)
    side = np.uint64((1 << n_shards_bits) - 1)
    return (code >> np.uint64(n_shards_bits)) & side

class NakshaPointCacheBuilder:
    """Builds .nakshapc + .nakshaidx from LAS/LAZ sources with bounded RAM."""

    def __init__(self, project_file: str, chunk_points: int = 2_000_000,
                 leaf_target: int = LEAF_TARGET_POINTS,
                 overview_target: int = OVERVIEW_TARGET_POINTS,
                 max_leaf_extent: float = 160.0, min_leaf_points: int = 65536,
                 ordering: str = "XY16",
                 lod_levels: int = DEFAULT_LOD_LEVELS, temp_dir: str = None,
                 verbose=True,
                 lod_ratio: float = DEFAULT_LOD_RATIO,
                 output_path: str = None, scratch_path: str = None,
                 checkpoint_bytes: int = 256 << 20, cancel_check=None,
                 safety_hook=None):
        self.project = project_file
        self.chunk_points = chunk_points
        self.leaf_target = leaf_target
        self.overview_target = overview_target
        # No leaf may be wider than this (Part 5 spatial floor).
        self.max_leaf_extent = float(max_leaf_extent)
        # A leaf never falls below this, so the spatial floor cannot shred a
        # sparse region into uselessly small tiles.
        # Intra-page ordering. Production already used XY (Z is NOT in the key)
        # at 16 bits, so "XYZ16" is the experimental CONTROL, not the default.
        # The page-size sweep therefore varies exactly ONE variable.
        self.ordering = ordering
        self.min_leaf_points = int(min_leaf_points)
        self.lod_levels = lod_levels
        self.lod_ratio = float(lod_ratio)
        self.verbose = verbose
        from .index import project_paths
        self.idx_path, self.pc_path, self.edit_path, self.build_path = \
            project_paths(output_path or project_file)
        self.output_project = output_path or project_file
        self.temp_dir = os.path.abspath(scratch_path or temp_dir or
                                        (os.path.dirname(self.output_project) or "."))
        self.production_pc_path = self.pc_path
        self.checkpoint_bytes = max(1, int(checkpoint_bytes))
        self.cancel_check = cancel_check
        self.safety_hook = safety_hook
        self._session = None
        if self.chunk_points <= 0 or self.leaf_target <= 0 or self.max_leaf_extent <= 0:
            raise ValueError("build sizes/extents must be positive")
        if not 2 <= self.lod_levels <= len(np.zeros(1, NODE_ENTRY)[0]["lod_block"]):
            raise ValueError("LOD count outside node format capacity")
        if not 0 < self.lod_ratio < .9:
            raise ValueError("LOD ratio must be between 0 and .9")
        self.shard_dir = os.path.join(
            self.temp_dir, os.path.basename(project_file) + ".shards")
        self.sources = None
        self.nodes = None
        self.blocks = None
        self.microcell_rows = []   # Phase A render microcells
        self.stats = {}

    def _log(self, *a):
        if self.verbose:
            print(*a, flush=True)

    # ---------------------------------------------------------- header scan
    def scan_sources(self, paths):
        """Header-only pass: the canonical source catalog (source_catalog.py).

        Validates CRS / units / coordinate magnitude BEFORE anything is
        combined (raises CatalogError), assigns unique file ids and the dense
        global point base, and records LAS encoding metadata + fingerprints.
        """
        rows, rep = build_catalog(paths)
        self.sources = rows
        self.catalog_report = rep
        gmin = np.asarray(rep.bounds_min, float)
        gmax = np.asarray(rep.bounds_max, float)
        mask = int(np.bitwise_or.reduce(rows["attribute_mask"]))
        if not rep.rgb_all_sources and (mask & ATTR_RGB):
            # RGB is a per-POINT stream aligned with XYZ: it exists for the
            # whole dataset or not at all. A mix would make spatialize() read
            # `.red` from a source that has none.
            mask &= ~ATTR_RGB
            rep.warnings.append("RGB present in only some sources: the RGB "
                                "stream is disabled for this dataset")
        with_rgb = rows["attribute_mask"] & ATTR_RGB
        rgb_bits = int(rows["rgb_max_bits"][with_rgb != 0].max()) \
            if (with_rgb != 0).any() else 16
        self.gmin = gmin
        self.gmax = gmax
        self.total_points = int(rep.total_points)
        self.attribute_mask = mask
        self.rgb_bits = rgb_bits
        self.span = np.maximum(gmax - gmin, 1e-6)
        # Mergeable global statistics, accumulated in spatialize()'s chunk loop.
        self.stats_obj = GlobalStats(len(paths), float(gmin[2]), float(gmax[2]))
        self._log(f"  header scan: {len(paths)} file(s), {self.total_points:,} "
                  f"points, rgb_bits={rgb_bits}, mask=0x{mask:x}, "
                  f"{rep.summary()}")
        for w in rep.warnings:
            self._log(f"  catalog warning: {w}")
        return self.sources

    # ------------------------------------------------------ PASS 1 overview
    def build_overview(self, paths):
        """Distributed overview WITHOUT waiting for spatialization (Part 8/9).

        Raw LAZ is flight-line ordered, so taking the first N points would show
        one corner of the survey and nothing else. This walks EVERY source and
        EVERY chunk and feeds a strided subsample of each chunk to a
        `BoundedOverviewReducer`, whose pool never exceeds a fixed cap (the
        legacy list + np.concatenate grew with the dataset: ~228 M points at
        1.14 B). Retained points are real source points and keep their
        canonical global id.
        """
        import laspy
        t0 = time.perf_counter()
        have_rgb = bool(self.attribute_mask & ATTR_RGB)
        red = BoundedOverviewReducer(self.span[:2], self.overview_target,
                                     have_rgb=have_rgb)
        for fid, p in enumerate(paths):
            base = int(self.sources["point_base"][fid])
            local = 0
            with laspy.open(p) as f:
                for ch in f.chunk_iterator(self.chunk_points):
                    n = len(ch)
                    if n == 0:
                        continue
                    stride = max(1, -(-n // red.per_chunk))
                    sl = slice(0, n, stride)
                    xyz = np.column_stack((np.asarray(ch.x[sl]),
                                           np.asarray(ch.y[sl]),
                                           np.asarray(ch.z[sl])))
                    gid = (np.uint64(base + local)
                           + np.arange(0, n, stride, dtype=np.uint64))
                    rgb = (np.column_stack((np.asarray(ch.red[sl]),
                                            np.asarray(ch.green[sl]),
                                            np.asarray(ch.blue[sl])))
                           if have_rgb else None)
                    red.add(xyz, np.asarray(ch.classification[sl]), rgb, gid,
                            np.asarray(ch.intensity[sl]))
                    local += n
                    if self._session:
                        self._session.event("OVERVIEW_CHUNK")
        out = red.finish()
        if out is None:
            return None
        xyz, cls, rgb, gid, inten = out
        self.stats["overview_pool_peak_points"] = red.peak_points
        self.stats["overview_pool_cap"] = red.cap
        self.stats["overview_reductions"] = red.reductions
        self._log(f"  overview: {red.points_seen:,} seen, pool peak "
                  f"{red.peak_points:,} (cap {red.cap:,}) -> {xyz.shape[0]:,} "
                  f"pts in {time.perf_counter() - t0:.1f}s")
        self.overview_data = (xyz, cls, rgb)
        self.overview_ids = gid
        self.overview_intensity = inten
        return self.overview_data

    # ------------------------------------------------- PASS 1 spatialize
    def spatialize(self, paths):
        """PASS 1: stream every chunk into bounded spatial shards.

        Memory at any instant = one source chunk + shard write buffers. The
        source chunk is released as soon as it has been appended.
        """
        import laspy
        t0 = time.perf_counter()
        n_shards = choose_shard_count(self.total_points)
        bits = int(np.log2(n_shards))
        from .build_safety import BuildSafetyError
        session = self._session
        if session is None or not session.lock.held:
            raise BuildSafetyError("spatialize requires the owned build() lifecycle")
        sw = ShardWriter(self.shard_dir, n_shards, session=session)
        self.shards = sw
        if session.m["shards"]:
            sw.counts[:] = [r["record_count"] for r in session.m["shards"]]
        # Discard only uncheckpointed tails, after session.validate_work proved
        # all durable segments and exclusive ownership was acquired.
        for i, path in enumerate(sw.paths):
            size = int(sw.counts[i]) * SHARD_ITEMSIZE
            if os.path.exists(path):
                with open(path, "r+b") as f:
                    f.truncate(size)
        sw.bytes_written = int(sw.counts.sum()) * SHARD_ITEMSIZE
        have_rgb = bool(self.attribute_mask & ATTR_RGB)
        done = sum(r["processed_point_count"] for r in session.m["sources"])
        peak_chunk_bytes = 0
        stats = getattr(self, "stats_obj", None)
        for fid, p in enumerate(paths):
            progress = session.m["sources"][fid]
            if progress["status"] == "COMPLETE":
                continue
            # CANONICAL POINT IDENTITY: global id = point_base[file] + local
            # index. The legacy code reset the base to 0 for every file, so id
            # k existed once per file (proved on a two-file build).
            gbase = int(self.sources["point_base"][fid])
            expect = int(self.sources["point_count"][fid])
            base = progress["processed_point_count"]
            chunk_index = progress["chunk_index"]
            with laspy.open(p) as f:
                if base:
                    f.seek(base)
                for ch in f.chunk_iterator(self.chunk_points):
                    n = len(ch)
                    if n == 0:
                        continue
                    rec = np.zeros(n, dtype=SHARD_DTYPE)
                    rec["x"] = np.asarray(ch.x, np.float64)
                    rec["y"] = np.asarray(ch.y, np.float64)
                    rec["z"] = np.asarray(ch.z, np.float64)
                    rec["source_id"] = (np.uint64(gbase + base)
                                        + np.arange(0, n, dtype=np.uint64))
                    if hasattr(ch, "classification"):
                        rec["classification"] = np.asarray(ch.classification)
                    if hasattr(ch, "intensity"):
                        rec["intensity"] = np.asarray(ch.intensity)
                    if hasattr(ch, "return_number"):
                        rec["return_number"] = np.asarray(ch.return_number)
                    if hasattr(ch, "number_of_returns"):
                        rec["number_of_returns"] = np.asarray(ch.number_of_returns)
                    if hasattr(ch, "point_source_id"):
                        rec["point_source_id"] = np.asarray(ch.point_source_id)
                    if have_rgb:
                        # Preserve the SOURCE uint16 width (Part 6/12).
                        rec["red"] = np.asarray(ch.red, np.uint16)
                        rec["green"] = np.asarray(ch.green, np.uint16)
                        rec["blue"] = np.asarray(ch.blue, np.uint16)
                    if stats is not None:
                        # Mergeable global statistics ride the EXISTING pass:
                        # no second read of the source, constant memory.
                        stats.add_chunk(fid, rec["z"], rec["intensity"],
                                        rec["classification"])
                    shard = shard_of(rec["x"], rec["y"], self.gmin,
                                     self.span, bits)
                    peak_chunk_bytes = max(peak_chunk_bytes, rec.nbytes)
                    # Group by shard and append each group contiguously.
                    order = np.argsort(shard, kind="stable")
                    ss = shard[order]
                    bnd = np.flatnonzero(np.diff(ss)) + 1
                    starts = np.concatenate(([0], bnd))
                    ends = np.concatenate((bnd, [n]))
                    # Slice `rec` THROUGH `order`.
                    #
                    # The previous code sliced the flat `payload` at
                    # [a * ITEMSIZE : b * ITEMSIZE]. `a` and `b` are boundaries
                    # in the SORTED array, so that slice is a contiguous run of
                    # the ORIGINAL FILE ORDER: every shard file received points
                    # that did not belong to its bucket. PASS 2 then subdivided
                    # those file-order runs, which is why the published leaves
                    # were not tiles - on the real 123.las cache a "leaf"
                    # spanned the full 150 m survey with 60,060 points while
                    # its own shard was a 19 m square, and 53 of 73 leaves were
                    # wider than 30 m.
                    for a, b in zip(starts, ends):
                        sw.append(int(ss[a]), rec[order[a:b]].tobytes(),
                                  int(b - a))
                    del rec, order, ss
                    base += n
                    done += n
                    chunk_index += 1
                    session.checkpoint_source(sw, fid, base, chunk_index)
                    session.event("SOURCE_CHUNK")
            if base != expect:
                raise CatalogError(
                    f"{p}: read {base:,} points but the header declares "
                    f"{expect:,} (truncated or corrupt file)")
            session.checkpoint_source(sw, fid, base, chunk_index, complete=True)
            self._log(f"    spatialized {done:,}/{self.total_points:,} "
                      f"({time.perf_counter() - t0:.1f}s)")
        sw.flush_all()
        sw.close_all()
        self.shards = sw
        self.shard_bits = bits
        self.stats["pass1_seconds"] = time.perf_counter() - t0
        self.stats["shard_count"] = n_shards
        self.stats["shards_used"] = sw.used_shards()
        self.stats["temp_bytes"] = sw.bytes_written
        self.stats["peak_chunk_bytes"] = peak_chunk_bytes
        return sw

    # ------------------------------------------------ PASS 2 finalization
    def _write_block(self, writer, node_id, lod, xyz, cls, rgb, src_id,
                     ret, psi, inten):
        """Encode one leaf/tile as a .nakshapc block with SoA streams."""
        bmin = np.array([xyz[:, 0].min(), xyz[:, 1].min(), xyz[:, 2].min()])
        bmax = np.array([xyz[:, 0].max(), xyz[:, 1].max(), xyz[:, 2].max()])
        local, origin, scale = quantize_xyz(xyz[:, 0], xyz[:, 1], xyz[:, 2],
                                            bmin, bmax)
        streams = {
            ATTR_XYZ: local,
            ATTR_CLASSIFICATION: cls.astype(np.uint8),
            ATTR_INTENSITY: inten.astype(np.uint16),
            ATTR_RETURN_NUMBER: ret.astype(np.uint8),
            ATTR_POINT_SOURCE_ID: psi.astype(np.uint16),
            # Stable identity, its own stream so rendering never reads it.
            ATTR_SOURCE_ID: src_id.astype(np.uint64),
        }
        if rgb is not None:
            streams[ATTR_RGB] = rgb.astype(np.uint16 if self.rgb_bits == 16
                                          else np.uint8)
        mask = (ATTR_XYZ | ATTR_CLASSIFICATION | ATTR_INTENSITY
                | ATTR_RETURN_NUMBER | ATTR_POINT_SOURCE_ID | ATTR_SOURCE_ID)
        if rgb is not None:
            mask |= ATTR_RGB
        # Block ids must be unique across the WHOLE file; writer.next_id tracks
        # that across writer sessions. Using len(writer.blocks) restarts at 0
        # for a second writer and collides with the first detail block.
        bid = writer.next_id
        writer.add(bid, node_id, lod, streams, mask, self.rgb_bits,
                   origin, scale)
        writer.next_id = bid + 1
        # Phase A: derive render MICROCELLS from this Morton-ordered page.
        # They are contiguous sub-ranges, so no point data is duplicated.
        mc = build_microcells(bid, lod, local, origin, scale)
        self.microcell_rows.extend(list(mc))
        # Phase B: representative LOCAL spacing, not sqrt(AABB area / count).
        sp = local_spacing_estimate(local, origin, scale)
        return bid, len(local), bmin, bmax, sp

    def finalize(self):
        """PASS 2: shard -> adaptive leaves -> Morton inside leaf -> blocks+LOD."""
        from .format import BlockWriter
        from .build_safety import BuildSafetyError
        t0 = time.perf_counter()
        session = self._session
        if session is None or not session.lock.held:
            raise BuildSafetyError("finalize requires the owned build() lifecycle")
        node_rows, previous_blocks = self._restore_finalized()
        completed = {r["shard_id"] for r in session.m["finalized"]}
        if os.path.exists(self.pc_path):
            with open(self.pc_path, "r+b") as f:
                f.truncate(session.m["pc_offset"])
        writer = BlockWriter(self.pc_path, append=bool(session.m["pc_offset"]),
                             first_block_id=len(previous_blocks))
        self._active_writer = writer
        block_rows = []
        written_points = sum(int(r["point_count"]) for r in previous_blocks)
        processed_shards = 0
        for si, path in enumerate(self.shards.paths):
            if si in completed:
                continue
            cnt = int(self.shards.counts[si])
            if cnt == 0:
                continue
            processed_shards += 1
            rec = np.memmap(path, dtype=SHARD_DTYPE, mode="r",
                            shape=(cnt,))
            # FULL proof on original float64 shard records, retaining the
            # permanent invariant that caught the historic sorted-slice bug.
            membership = shard_of(rec["x"], rec["y"], self.gmin, self.span, self.shard_bits)
            if not (membership == si).all():
                rec._mmap.close()
                raise BuildSafetyError(f"VALIDATION FAILED: spatial shard membership {si}")
            ids = np.asarray(rec["source_id"])
            if len(np.unique(ids)) != cnt or int(ids.max()) >= self.total_points:
                rec._mmap.close()
                raise BuildSafetyError(f"VALIDATION FAILED: shard {si} source IDs")
            del ids, membership
            node_start, block_start, mc_start = len(node_rows), len(writer.blocks), len(self.microcell_rows)
            xyz = np.column_stack((rec["x"], rec["y"], rec["z"]))
            cls = np.asarray(rec["classification"])
            inten = np.asarray(rec["intensity"])
            ret = np.asarray(rec["return_number"])
            nret = np.asarray(rec["number_of_returns"])
            psi = np.asarray(rec["point_source_id"])
            sid = np.asarray(rec["source_id"])
            have_rgb = bool(self.attribute_mask & ATTR_RGB)
            if have_rgb:
                rgb = np.column_stack((rec["red"], rec["green"], rec["blue"]))
            else:
                rgb = None

            # ADAPTIVE subdivision (Part 5 + Part 19). DUAL condition:
            #   (a) enough cells that the average cell is near leaf_target, AND
            #   (b) enough cells that no leaf stays SPATIALLY enormous.
            # Condition (a) alone lets a sparse shard produce a leaf that is
            # under the point target but hundreds of metres across; such a leaf
            # overlaps a deep-zoom view and keeps being drawn, which is exactly
            # why x16 and x32 previously read the same 5.5M points.
            n = cnt
            spanx = max(float(xyz[:, 0].max() - xyz[:, 0].min()), 1e-6)
            spany = max(float(xyz[:, 1].max() - xyz[:, 1].min()), 1e-6)
            cells = int(np.clip(round(n / self.leaf_target), 1, 4096))
            # Spatial floor (Part 5): a leaf can be under the point target
            # and still be hundreds of metres across; such a leaf overlaps a
            # deep-zoom view and keeps being drawn, which is why x16 and x32
            # previously read the same 5.5M points.
            #
            # The floor is applied ONLY when it still leaves enough points per
            # leaf. Forcing it unconditionally in SPARSE areas produced 5,460
            # point leaves (median) instead of 256K - its own defect: a huge
            # block directory and more draw calls than the viewport needs.
            need_x = int(np.ceil(spanx / self.max_leaf_extent))
            need_y = int(np.ceil(spany / self.max_leaf_extent))
            cells_space = max(1, need_x * need_y)
            if n / float(cells_space) >= self.min_leaf_points:
                cells = int(np.clip(max(cells, cells_space), 1, 4096))
            aspect = spanx / spany
            ny = int(np.clip(round(np.sqrt(cells / aspect)), 1, 256))
            nx = int(np.clip(cells / ny, 1, 256))
            gx = np.clip(((xyz[:, 0] - xyz[:, 0].min()) / spanx * nx).astype(np.int64), 0, nx - 1)
            gy = np.clip(((xyz[:, 1] - xyz[:, 1].min()) / spany * ny).astype(np.int64), 0, ny - 1)
            cell = gy * nx + gx
            order = np.argsort(cell, kind="stable")
            cs = cell[order]
            bnd = np.flatnonzero(np.diff(cs)) + 1
            starts = np.concatenate(([0], bnd))
            ends = np.concatenate((bnd, [n]))

            for a, b in zip(starts, ends):
                idx = order[a:b]
                sub_xyz = xyz[idx]
                sub_cls = cls[idx]
                sub_inten = inten[idx]
                sub_ret = ret[idx]
                sub_psi = psi[idx]
                sub_sid = sid[idx]
                sub_rgb = rgb[idx] if rgb is not None else None
                # Morton ordering INSIDE the leaf only (Part 5/11): the leaf is
                # already a spatial partition; ordering within it tightens
                # sub-block locality without a global sort.
                mcode = morton2d(sub_xyz[:, 0], sub_xyz[:, 1],
                                 float(sub_xyz[:, 0].min()),
                                 max(float(sub_xyz[:, 0].max() - sub_xyz[:, 0].min()), 1e-6),
                                 float(sub_xyz[:, 1].min()),
                                 max(float(sub_xyz[:, 1].max() - sub_xyz[:, 1].min()), 1e-6),
                                 bits=16)
                mo = np.argsort(mcode, kind="stable")
                sub_xyz = sub_xyz[mo]; sub_cls = sub_cls[mo]
                sub_inten = sub_inten[mo]; sub_ret = sub_ret[mo]
                sub_psi = sub_psi[mo]; sub_sid = sub_sid[mo]
                if sub_rgb is not None:
                    sub_rgb = sub_rgb[mo]

                bmin = np.array([sub_xyz[:, 0].min(), sub_xyz[:, 1].min(), sub_xyz[:, 2].min()])
                bmax = np.array([sub_xyz[:, 0].max(), sub_xyz[:, 1].max(), sub_xyz[:, 2].max()])
                node_id = len(node_rows)
                # LOD0 = full detail block.
                bid0, npts, _, _, lod0_sp = self._write_block(
                    writer, node_id, 0, sub_xyz, sub_cls, sub_rgb, sub_sid,
                    sub_ret, sub_psi, sub_inten)
                block_rows.append(bid0)
                written_points += npts
                # LOD0 spacing: the sampling density of the raw source in
                # this leaf, estimated from point count over leaf area.
                # Stored so screen-space LOD selection can PROJECT it
                # instead of guessing a ratio from point counts.
                # Phase B: LOD0 spacing from the MEASURED local point
                # distribution (Morton-local neighbour distances), not
                # sqrt(AABB_area / count). The AABB form is dominated by empty
                # space and by an elongated flight-line box, so it OVERSTATES
                # coarseness and suppresses finer LOD rungs at runtime.
                # LOD_ALGO_VERSION 2: EVERY rung's spacing is the area-equivalent
                # sqrt(occupied_area / count) - one physical definition, so
                # apparent spacing is comparable across nodes and rungs (the
                # measured Morton distance at LOD0 vs voxel-cell size at LOD>=1
                # made equal-density rungs differ ~10x). See lod_ladder.py.
                _occ = occupied_area(sub_xyz)
                lod_spacing = [area_spacing(_occ, npts)]
                node_rows.append((node_id, 0, 0, bmin, bmax, npts, [bid0],
                                  [npts], STREAMABLE, lod_spacing))

                # Coarse LODs (Part 11): voxel representative, progressively
                # coarser, PERSISTED so no zoom ever resamples from source.
                cur = (sub_xyz, sub_cls, sub_rgb, sub_sid, sub_ret, sub_psi,
                       sub_inten)
                # LOD_ALGO_VERSION 2: a GEOMETRIC, NESTED ladder - each rung is a
                # fixed fraction (`lod_ratio`) of the previous rung's POINT
                # COUNT, chosen per leaf from its own measured spacing. The old
                # `leaf_cell * 2**lod` voxel schedule gave a dense leaf a 23x
                # cliff between LOD0 and LOD1 (62,036 -> ~2,700) with nothing in
                # between; see lod_ladder.py.
                _s0 = lod0_sp if lod0_sp > 0 else float(np.sqrt(
                    max(float(bmax[0] - bmin[0]) * float(bmax[1] - bmin[1]),
                        1e-9) / max(npts, 1)))
                _rungs = build_ladder(sub_xyz, sub_cls, _s0,
                                      max_rungs=self.lod_levels - 2,
                                      ratio=self.lod_ratio)
                for lod, (rep, cell_m) in enumerate(_rungs, start=1):
                    cxyz, ccls, crgb, csid, cret, cpsi, cint = cur
                    cur = (cxyz[rep], ccls[rep],
                           crgb[rep] if crgb is not None else None,
                           csid[rep], cret[rep], cpsi[rep], cint[rep])
                    cxyz, ccls, crgb, csid, cret, cpsi, cint = cur
                    bidl, nl, _, _, _sp = self._write_block(
                        writer, node_id, lod, cxyz, ccls, crgb, csid,
                        cret, cpsi, cint)
                    block_rows.append(bidl)
                    written_points += nl
                    # record the representative spacing for this LOD
                    node_rows[-1][9].append(max(area_spacing(_occ, nl), 1e-3))
                    node_rows[-1] = (node_id, 0, 0, bmin, bmax, npts,
                                     node_rows[-1][6] + [bidl],
                                     node_rows[-1][7] + [nl], STREAMABLE,
                                     node_rows[-1][9])
                session.event("FINALIZE_NODE")
            # Drop all memmap views before closing the Windows mapping.
            del cls, inten, ret, nret, psi, sid
            rec._mmap.close()
            del rec, xyz
            if rgb is not None:
                del rgb
            writer.fh.flush()
            os.fsync(writer.fh.fileno())
            self._finalize_nodes(node_rows[node_start:])
            metadata = session.write_npz(f"finalized-{si:05d}",
                nodes=self.nodes, blocks=np.array(writer.blocks[block_start:], dtype=BLOCK_ENTRY),
                microcells=np.array(self.microcell_rows[mc_start:], dtype=MICROCELL_ENTRY))
            session.m["finalized"].append({"shard_id": si, "record_count": cnt,
                "byte_size": cnt * SHARD_ITEMSIZE, "final_block_count": len(writer.blocks) - block_start,
                "status": "COMPLETE", "metadata": metadata})
            session.m["pc_offset"] = writer.offset
            session.save()
            session.event("SHARD_FINALIZED")
            if (processed_shards % 50) == 0:
                self._log(f"    finalized {processed_shards} shard(s), "
                          f"{written_points:,} pts written "
                          f"({time.perf_counter() - t0:.1f}s)")
        writer.close()
        self._active_writer = None
        new_blocks = writer.directory()
        self.blocks = np.concatenate((previous_blocks, new_blocks))
        self._finalize_nodes(node_rows)
        # Phase A: commit the render microcell table.
        self.microcells = (np.array(self.microcell_rows,
                                   dtype=MICROCELL_ENTRY)
                           if self.microcell_rows
                           else np.zeros(0, dtype=MICROCELL_ENTRY))
        self.stats["pass2_seconds"] = time.perf_counter() - t0
        self.stats["blocks"] = int(self.blocks.size)
        self.stats["nodes"] = int(self.nodes.size)
        self.stats["written_points"] = written_points
        return self.blocks

    def _finalize_nodes(self, rows):
        # Index positionally instead of nesting a tuple unpack. A nested
        # `for i, (a, b, c, ...) in rows:` reports a misleading arity error if
        # any row ever differs in shape, and the traceback points at the wrong
        # construct. Positional access is also cheaper for a million rows.
        nodes = np.zeros(len(rows), dtype=NODE_ENTRY)
        for i, r in enumerate(rows):
            nid, lvl, quad, bmin, bmax, npts = r[0], r[1], r[2], r[3], r[4], r[5]
            bids, ncnt = r[6], r[7]
            state = r[8]
            spac = r[9] if len(r) > 9 else []
            nodes[i]["node_id"] = nid
            nodes[i]["level"] = lvl
            nodes[i]["quad"] = quad
            nodes[i]["bounds_min"] = bmin
            nodes[i]["bounds_max"] = bmax
            nodes[i]["point_count"] = npts
            nodes[i]["first_child"] = -1
            lb = nodes[i]["lod_block"]
            ln = nodes[i]["lod_point_count"]
            lsp = nodes[i]["lod_spacing"]
            for j in range(min(len(bids), len(lb))):
                lb[j] = bids[j]
                ln[j] = ncnt[j] if j < len(ncnt) else 0
                # Representative world spacing per LOD, in metres. A zero means
                # "not recorded" and makes the runtime fall back to a density
                # estimate rather than trusting a bogus value.
                lsp[j] = float(spac[j]) if j < len(spac) else 0.0
            nodes[i]["state"] = state
        self.nodes = nodes

    def _restore_finalized(self):
        rows, blocks = [], []
        self.microcell_rows = []
        for entry in self._session.m["finalized"]:
            with np.load(entry["metadata"]["path"], allow_pickle=False) as z:
                blocks.extend(z["blocks"])
                self.microcell_rows.extend(z["microcells"])
                for node in z["nodes"]:
                    n = int((node["lod_point_count"] > 0).sum())
                    rows.append((int(node["node_id"]), int(node["level"]), int(node["quad"]),
                        node["bounds_min"].copy(), node["bounds_max"].copy(), int(node["point_count"]),
                        node["lod_block"][:n].tolist(), node["lod_point_count"][:n].tolist(),
                        int(node["state"]), node["lod_spacing"][:n].tolist()))
        return rows, np.array(blocks, dtype=BLOCK_ENTRY)

    def write_overview_block(self):
        """Write the live overview as a block so a cached open finds it too."""
        from .format import BlockWriter, ATTR_SOURCE_ID as _SID
        if getattr(self, "overview_data", None) is None:
            return None
        xyz, cls, rgb = self.overview_data
        n = xyz.shape[0]
        # Overview points are REAL source points (voxel representatives), so
        # they carry their canonical global id. The legacy code wrote id 0 for
        # all of them, colliding with the real point 0 (edits and normal
        # lookups of point 0 hit every overview point).
        ov_ids = getattr(self, "overview_ids", None)
        if ov_ids is not None and ov_ids.shape[0] == n:
            sid = np.asarray(ov_ids, dtype=np.uint64)
        else:
            from .format import SOURCE_ID_NONE
            sid = np.full(n, SOURCE_ID_NONE, dtype=np.uint64)
        ret = np.zeros(n, dtype=np.uint8)
        psi = np.zeros(n, dtype=np.uint16)
        ov_i = getattr(self, "overview_intensity", None)
        inten = (np.asarray(ov_i, dtype=np.uint16)
                 if ov_i is not None and ov_i.shape[0] == n
                 else np.zeros(n, dtype=np.uint16))
        writer = BlockWriter(self.pc_path, append=True,
                               first_block_id=len(self.blocks))
        self._active_writer = writer
        bid, npts, _, _, _sp = self._write_block(
            writer, 0, self.lod_levels - 1, xyz, cls, rgb, sid, ret, psi, inten)
        writer.close()
        self._active_writer = None
        newdir = writer.directory()
        # Append the overview entry to the existing block directory.
        self.blocks = (np.concatenate([self.blocks, newdir])
                        if self.blocks.size else newdir)
        self._session.m["overview_bid"] = bid
        self._session.m["overview_block"] = [r.tobytes().hex() for r in newdir]
        self._session.m["pc_offset"] = os.path.getsize(self.pc_path)
        self._session.save()
        self._log(f"  overview block {bid} written ({npts:,} pts)")
        return bid

    def commit_index(self, overview_bid=None):
        from .index import IndexWriter, EditFile
        from .build_safety import BuildSafetyError
        session = self._session
        if session is None or not session.lock.held:
            raise BuildSafetyError("commit_index requires the validated owned build() lifecycle")
        if not session.m["validation"]:
            raise BuildSafetyError("pre-commit output validation required")
        session.revalidate_sources()
        from .build_validation import validate_index
        final_pc = session.m["final_pc"]
        if self.pc_path != final_pc:
            os.replace(self.pc_path, final_pc)
            self.pc_path = final_pc
        session.event("POINT_CACHE_PUBLISHED")
        iw = IndexWriter(self.idx_path, dataset_uuid=bytes.fromhex(session.m["build_id"]))
        def validate_tmp(path):
            session.event("INDEX_TEMP_WRITTEN")
            validate_index(path, self, overview_bid, final_pc)
            session.revalidate_sources()
            session.event("INDEX_TEMP_VALIDATED")
        iw.commit(self.sources, self.nodes, self.blocks,
                  point_cache_name=self.pc_path,
                  pc_file_size=os.path.getsize(self.pc_path),
                  lod_count=self.lod_levels, max_depth=0,
                  shard_depth=self.shard_bits, attribute_mask=self.attribute_mask,
                  build_state=BUILD_FINALIZED,
                  overview_block_id=overview_bid,
                  microcells=getattr(self, "microcells", None),
                  crs_wkt=getattr(self.catalog_report, "crs_ref", ""),
                  crs_hash=getattr(self.catalog_report, "crs_hash", 0),
                  stats_blob=(self.stats_obj.to_bytes()
                              if getattr(self, "stats_obj", None) is not None
                              and self.stats_obj.n_points == self.total_points
                              else None), validator=validate_tmp)
        session.event("INDEX_PUBLISHED")
        self._publish_pc_alias()
        if not os.path.exists(self.edit_path):
            ed = EditFile(self.edit_path)
            ed.commit()          # preserve existing classification edits
        self._log(f"  index committed: {os.path.getsize(self.idx_path):,} B")
        return self.idx_path

    def _publish_pc_alias(self):
        """Compatibility name; readers use the index's immutable generation.

        A hard link costs no second full payload and leaves already-open
        readers on their original generation. The alias is not the commit.
        """
        alias = self.production_pc_path + ".alias.tmp"
        try:
            if os.path.exists(alias):
                os.unlink(alias)
            os.link(self.pc_path, alias)
            os.replace(alias, self.production_pc_path)
        except OSError as exc:
            self._log("  compatibility NKPC alias unavailable:", exc,
                      "; index references the valid immutable payload")

    def cleanup_shards(self):
        """Remove temporary shards once the cache is committed."""
        from .build_safety import BuildSafetyError
        if self._session is None or self._session.m["stages"]["COMMIT"] != "COMPLETE":
            raise BuildSafetyError("cannot delete resumable shards before successful commit")
        for p in getattr(self.shards, "paths", []):
            try:
                os.remove(p)
            except Exception:
                pass
        try:
            os.rmdir(self.shard_dir)
        except Exception:
            pass

    # --------------------------------------------------------------- driver
    def build(self, paths, with_overview=True, *, resume=False, recover_stale=False):
        from .build_safety import BuildSession
        from .disk_admission import admit_build, format_report
        from .build_validation import validate_output, validate_index
        from .index import IndexWriter, IndexReader
        t_start = time.perf_counter()
        paths = [os.path.abspath(os.fspath(p)) for p in paths]
        self.scan_sources(paths)
        t_header = time.perf_counter() - t_start
        admission = admit_build(self.total_points, len(paths), self.idx_path, self.temp_dir,
            shard_itemsize=SHARD_ITEMSIZE, lod_levels=self.lod_levels,
            overview_target=self.overview_target, rgb_bits=self.rgb_bits,
            have_rgb=bool(self.attribute_mask & ATTR_RGB))
        self.stats["disk_admission"] = admission
        self._log(format_report(admission))
        session = BuildSession(self, paths, with_overview, resume, recover_stale)
        self._session = session
        try:
            session.start()
            # A crash after index publication can leave COMMIT IN_PROGRESS.
            # The index UUID and immutable payload prove which generation won.
            published = False
            if resume and os.path.exists(self.idx_path):
                idx = IndexReader(self.idx_path)
                try:
                    published = (idx.uuid == bytes.fromhex(session.m["build_id"])
                                 and idx.point_cache_name == os.path.basename(session.m["final_pc"]))
                finally:
                    idx.close()
            if published:
                self.pc_path = session.m["final_pc"]
                self._publish_pc_alias()
                session.stage("COMMIT", "COMPLETE")
                self.stats["recovered_published_commit"] = True
                return self.stats
            session.event("BEFORE_SPATIALIZE")
            if with_overview:
                if session.m["overview"]:
                    with np.load(session.m["overview"]["path"], allow_pickle=False) as z:
                        self.overview_data = (z["xyz"], z["cls"], z["rgb"] if z["rgb"].size else None)
                        self.overview_ids, self.overview_intensity = z["gid"], z["intensity"]
                else:
                    session.stage("OVERVIEW", "IN_PROGRESS")
                    self.build_overview(paths)
                    xyz, cls, rgb = self.overview_data
                    session.m["overview"] = session.write_npz("overview", xyz=xyz, cls=cls,
                        rgb=rgb if rgb is not None else np.empty((0, 3), np.uint16),
                        gid=self.overview_ids, intensity=self.overview_intensity)
                    session.save()
                    session.event("OVERVIEW_BUILD_COMPLETE")
                self.stats["header_seconds"] = t_header
                self.stats["overview_seconds"] = time.perf_counter() - t_start - t_header
            if session.m["stages"]["SPATIALIZE"] != "COMPLETE":
                session.stage("SPATIALIZE", "IN_PROGRESS")
            self.spatialize(paths)
            session.stage("SPATIALIZE", "COMPLETE")
            session.stage("SHARDS", "COMPLETE")
            if session.m["stages"]["FINALIZE"] != "COMPLETE":
                session.stage("FINALIZE", "IN_PROGRESS")
                self.finalize()
                session.stage("FINALIZE", "COMPLETE")
            else:
                rows, self.blocks = self._restore_finalized()
                self._finalize_nodes(rows)
                self.microcells = np.array(self.microcell_rows, dtype=MICROCELL_ENTRY)
                with open(self.pc_path, "r+b") as f:
                    f.truncate(session.m["pc_offset"])
            if session.m["overview_bid"] is not None:
                rows = [np.frombuffer(bytes.fromhex(raw), BLOCK_ENTRY)[0]
                        for raw in session.m["overview_block"]]
                self.blocks = np.concatenate((self.blocks, np.array(rows, dtype=BLOCK_ENTRY)))
            elif with_overview:
                self.write_overview_block()
                session.event("OVERVIEW_BLOCK_WRITTEN")
            ov = session.m["overview_bid"]
            session.stage("OVERVIEW", "COMPLETE")
            session.stage("STATS", "IN_PROGRESS")
            if self.stats_obj.n_points != self.total_points:
                raise ValueError("statistics incomplete; refusing production commit")
            session.stage("STATS", "COMPLETE")
            session.revalidate_sources()
            session.stage("POINT_CACHE_VALIDATE", "IN_PROGRESS")
            session.m["validation"] = validate_output(self, ov)
            session.stage("POINT_CACHE_VALIDATE", "COMPLETE")
            session.stage("INDEX_VALIDATE", "IN_PROGRESS")
            candidate = os.path.join(session.m["work_dir"], "candidate.nakshaidx")
            iw = IndexWriter(candidate, dataset_uuid=bytes.fromhex(session.m["build_id"]))
            iw.commit(self.sources, self.nodes, self.blocks,
                point_cache_name=session.m["final_pc"], pc_file_size=os.path.getsize(self.pc_path),
                lod_count=self.lod_levels, shard_depth=self.shard_bits,
                attribute_mask=self.attribute_mask, build_state=BUILD_FINALIZED,
                overview_block_id=ov, microcells=self.microcells,
                crs_wkt=self.catalog_report.crs_ref, crs_hash=self.catalog_report.crs_hash,
                stats_blob=self.stats_obj.to_bytes(),
                validator=lambda p: validate_index(p, self, ov, session.m["final_pc"]))
            session.m["candidate_index"] = session.artifact(candidate)
            session.stage("INDEX_VALIDATE", "COMPLETE")
            session.stage("COMMIT", "IN_PROGRESS")
            session.event("PRE_COMMIT")
            self.commit_index(ov)
            session.stage("COMMIT", "COMPLETE")
            self.stats["validation"] = session.m["validation"]
        except Exception as exc:
            if session.m is not None and session.lock.held:
                session.m["last_error"] = f"{type(exc).__name__}: {exc}"
                for stage, state in session.m["stages"].items():
                    if state == "IN_PROGRESS":
                        session.m["stages"][stage] = "FAILED"
                session.save()
            raise
        finally:
            sw = getattr(self, "shards", None)
            if sw is not None:
                sw.close_all()
            writer = getattr(self, "_active_writer", None)
            if writer is not None and not writer.fh.closed:
                writer.close()
                self._active_writer = None
            session.lock.release()
            self.stats["checkpoint_ms"] = session.checkpoint_times
            self.stats["manifest_bytes"] = (os.path.getsize(session.manifest_path)
                                             if os.path.exists(session.manifest_path) else 0)
        self.stats["total_seconds"] = time.perf_counter() - t_start
        self.stats["pc_bytes"] = os.path.getsize(self.pc_path)
        self.stats["idx_bytes"] = os.path.getsize(self.idx_path)
        self.stats["first_visual_seconds"] = (self.stats.get("header_seconds", 0)
                                              + self.stats.get("overview_seconds", 0))
        return self.stats
