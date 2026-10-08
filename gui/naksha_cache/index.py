"""NKIDX001 index + NKEDIT001 edit overlay: writer and mmap reader.

The index is three packed numpy tables after a fixed header:

    [IDX_HEADER][SOURCE_ENTRY x n][NODE_ENTRY x n][BLOCK_ENTRY x n]

That layout is the whole point. A reader memory-maps the file and indexes the
tables directly, so opening a project with millions of nodes costs three mmap
calls and NO Python objects per node. Unpacking into a list of dicts would
allocate gigabytes and take minutes at 1.14B points - the failure this format
exists to prevent.

Writes are crash-safe (Part 17): everything lands in a .tmp file which is
fsync'd and atomically renamed, so the older cache is never touched until the
replacement is committed.
"""
import os

import numpy as np

from .microcells import MICROCELL_ENTRY
from .format import (ATTR_XYZ, BLOCK_ENTRY, BLOCK_PAYLOAD_VERSION,
                     BUILD_FINALIZED, CACHE_INCOMPATIBLE, EDIT_HEADER,
                     EDIT_MAGIC, EDIT_RECORD, EDIT_VERSION, IDENTITY_VERSION,
                     IDX_HEADER, IDX_HEADER_V1, IDX_HEADER_V2, IDX_MAGIC,
                     IDX_VERSION, IDX_VERSIONS_READABLE, LOD_ALGO_VERSION,
                     LOD_SPACING_VERSION, MORTON_VERSION, NODE_ENTRY,
                     OVERVIEW_ALGO_VERSION, RECORD_ORDER_VERSION, SEMANTIC_VERSIONS,
                     SHARD_VERSION, SOURCE_ENTRY, SOURCE_ENTRY_V1,
                     SPATIAL_LAYOUT_VERSION, STATS_VERSION, crc32_bytes,
                     evaluate_cache_status, layout_fingerprint,
                     new_dataset_uuid, now_unix, read_semantic_versions)


def project_paths(project_file: str):
    """project.las -> (.nakshaidx, .nakshapc, .nakshaedit, .nakshabuild)."""
    return (project_file + ".nakshaidx",
            project_file + ".nakshapc",
            project_file + ".nakshaedit",
            project_file + ".nakshabuild")


class IndexWriter:
    """Serialises the index tables into a .tmp, then atomically commits."""

    def __init__(self, idx_path: str, dataset_uuid=None):
        self.path = idx_path
        self.tmp = idx_path + ".tmp"
        self.uuid = dataset_uuid or new_dataset_uuid()

    def commit(self, sources, nodes, blocks, *, point_cache_name: str,
               pc_file_size: int = 0, crs_wkt: str = "", build_state=BUILD_FINALIZED,
               lod_count: int = 5, max_depth: int = 0, shard_depth: int = 0,
               attribute_mask: int = ATTR_XYZ, overview_block_id=None,
               microcells=None, stats_blob: bytes = None, crs_hash: int = 0,
               validator=None):
        hdr = np.zeros(1, dtype=IDX_HEADER)
        hdr["magic"] = IDX_MAGIC
        hdr["version"] = IDX_VERSION
        hdr["endianness"] = 0x01020304        # marker; this build is LE
        hdr["header_bytes"] = IDX_HEADER.itemsize
        hdr["dataset_uuid"] = np.frombuffer(self.uuid, dtype=np.uint8)
        hdr["created_unix"] = now_unix()
        hdr["source_point_count"] = int(sources["point_count"].sum()) \
            if sources.size else 0
        if sources.size:
            hdr["bounds_min"] = sources["bounds_min"].min(axis=0)
            hdr["bounds_max"] = sources["bounds_max"].max(axis=0)
        hdr["crs_wkt"] = crs_wkt.encode("utf-8", "replace")[:255]
        hdr["source_count"] = int(sources.size)
        hdr["node_count"] = int(nodes.size)
        hdr["block_count"] = int(blocks.size)
        hdr["microcell_count"] = (0 if microcells is None
                                 else int(len(microcells)))
        hdr["lod_count"] = lod_count
        hdr["attribute_mask"] = attribute_mask
        hdr["max_depth"] = max_depth
        hdr["shard_depth"] = shard_depth
        hdr["point_cache_name"] = os.path.basename(
            point_cache_name).encode("utf-8", "replace")[:63]
        hdr["pc_file_size"] = pc_file_size
        hdr["build_state"] = build_state
        # The overview block is recorded so a cached open knows exactly where to
        # read for first paint. It must be selected by the LARGEST point count
        # at the coarsest LOD, not merely the first block found: the
        # .nakshapc is written detail-first, so "first block at lod ==
        # lod_count-1" resolves to whichever leaf happened to emit that LOD
        # first (8,057 points instead of 299,715) - a silent wrong-data bug.
        # The overview block id is supplied EXPLICITLY by the builder, which knows
        # exactly which block it just appended. Inferring it here as "the first
        # block at the coarsest LOD" is wrong: leaf tiles also emit coarse LODs,
        # and .nakshapc is written detail-first, so that heuristic returned an
        # 8,057-point leaf instead of the 299,715-point overview - a silent
        # wrong-data bug on first paint.
        if overview_block_id is not None:
            hdr["overview_block_id"] = int(overview_block_id)
        else:
            hdr["overview_block_id"] = -1
        # ---- v2: semantic versions + global statistics slot ---------------
        if sources.dtype != SOURCE_ENTRY:
            raise ValueError("IndexWriter needs a v2 SOURCE_ENTRY table "
                             "(build it with source_catalog.build_catalog)")
        hdr["identity_version"] = IDENTITY_VERSION
        hdr["lod_algo_version"] = LOD_ALGO_VERSION
        hdr["overview_algo_version"] = OVERVIEW_ALGO_VERSION
        # ---- v3: one INDEPENDENT version per semantic subsystem -----------
        # These were previously unversioned, so a builder change silently kept
        # an old cache "current". See format.SEMANTIC_VERSIONS.
        hdr["spatial_layout_version"] = SPATIAL_LAYOUT_VERSION
        hdr["morton_version"] = MORTON_VERSION
        hdr["shard_version"] = SHARD_VERSION
        hdr["record_order_version"] = RECORD_ORDER_VERSION
        hdr["lod_spacing_version"] = LOD_SPACING_VERSION
        hdr["block_payload_version"] = BLOCK_PAYLOAD_VERSION
        # The fingerprint derived sidecars (.nakshanorm, future Surface cache)
        # bind to, so they can never be attached to a different cache layout
        # whose byte offsets happen to line up.
        hdr["layout_fingerprint"] = layout_fingerprint(
            versions={name: value for name, value, _label in SEMANTIC_VERSIONS},
            shard_depth=shard_depth,
            max_depth=max_depth,
            lod_count=lod_count,
            node_count=int(nodes.size),
            block_count=int(blocks.size),
            attribute_mask=int(attribute_mask),
            bounds_min=hdr["bounds_min"],
            bounds_max=hdr["bounds_max"])
        hdr["crs_hash"] = int(crs_hash)
        n_mc = 0 if microcells is None else int(len(microcells))
        stats_off = (IDX_HEADER.itemsize + sources.size * SOURCE_ENTRY.itemsize
                     + nodes.size * NODE_ENTRY.itemsize
                     + blocks.size * BLOCK_ENTRY.itemsize
                     + n_mc * MICROCELL_ENTRY.itemsize)
        if stats_blob:
            hdr["stats_version"] = STATS_VERSION
            hdr["stats_offset"] = stats_off
            hdr["stats_bytes"] = len(stats_blob)
        hdr["header_crc"] = 0
        hdr["header_crc"] = crc32_bytes(hdr.tobytes())

        with open(self.tmp, "wb") as f:
            hdr.tofile(f)
            sources.tofile(f)
            nodes.tofile(f)
            blocks.tofile(f)
            if microcells is not None and len(microcells):
                microcells.tofile(f)
            if stats_blob:
                f.write(stats_blob)
            f.flush()
            os.fsync(f.fileno())
        if validator is not None:
            validator(self.tmp)
        os.replace(self.tmp, self.path)       # atomic commit
        return self.path

    def write_checkpoint(self, state: dict):
        """Persist resumable builder state (Part 18)."""
        import json
        p = self.path + ".buildstate"
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)


def resolve_point_cache(idx_path, recorded_name, legacy_path):
    """The atomic index is the authority for its immutable payload generation."""
    if not recorded_name:
        return legacy_path
    if os.path.basename(recorded_name) != recorded_name or recorded_name in (".", ".."):
        raise ValueError("index point-cache name must be a local basename")
    return os.path.join(os.path.dirname(os.path.abspath(idx_path)), recorded_name)

class IndexReader:
    """mmap-backed reader. Opening costs three memmaps and zero Python objects.

    The tables are returned as numpy views straight over the mapped bytes, so
    `nodes["point_count"]` on a million-node project is an array operation, not
    a million attribute lookups.
    """

    def __init__(self, idx_path: str):
        self.path = idx_path
        if not os.path.isfile(idx_path):
            raise FileNotFoundError(idx_path)
        self.fh = open(idx_path, "rb")
        # magic (16) + version (4) are the same in every header layout, so the
        # version is read FIRST and decides which layout the rest is.
        prefix = self.fh.read(20)
        if len(prefix) < 20:
            raise ValueError(f"{idx_path}: truncated index header")
        magic = prefix[:16].rstrip(b"\x00")
        if magic != IDX_MAGIC.rstrip(b"\x00"):
            raise ValueError(f"{idx_path}: bad magic {magic!r}")
        version = int(np.frombuffer(prefix[16:20], dtype="<u4")[0])
        if version not in IDX_VERSIONS_READABLE:
            raise ValueError(f"{idx_path}: unsupported index version "
                             f"{version}, expected one of "
                             f"{IDX_VERSIONS_READABLE}")
        # THREE layouts: reading v2 bytes with the v3 dtype would reinterpret
        # the source table as header fields, so each version reads its own.
        self._hdr_dtype = (IDX_HEADER if version >= 3 else
                           (IDX_HEADER_V2 if version == 2 else IDX_HEADER_V1))
        self._src_dtype = SOURCE_ENTRY if version >= 2 else SOURCE_ENTRY_V1
        self.fh.seek(0)
        raw = self.fh.read(self._hdr_dtype.itemsize)
        if len(raw) < self._hdr_dtype.itemsize:
            raise ValueError(f"{idx_path}: truncated index header")
        hdr = np.frombuffer(raw, dtype=self._hdr_dtype, count=1)[0]
        self.version = version
        if version >= 2:
            self.identity_version = int(hdr["identity_version"])
            self.lod_algo_version = int(hdr["lod_algo_version"])
            self.overview_algo_version = int(hdr["overview_algo_version"])
            self.stats_version = int(hdr["stats_version"])
        else:
            self.identity_version = 1       # per-file point index (legacy)
            # A v1 file predates the version fields: it was built with the
            # ORIGINAL voxel schedule (algo 1). It stays readable (single file),
            # but it is reported honestly as the old ladder, not as current.
            self.lod_algo_version = 1
            self.overview_algo_version = 1
            self.stats_version = 0
        # Never interpret old bytes under a new schema. Version-1 bytes are the
        # SAME identity as version 2 only when there is a single source
        # (file-local index == global index). With several sources the legacy
        # ids collide across files, so the cache must be rebuilt.
        if self.identity_version != IDENTITY_VERSION:
            if int(hdr["source_count"]) > 1:
                raise ValueError(
                    f"{idx_path}: unsupported index version {version}: "
                    f"multi-file cache with legacy per-file point ids "
                    f"(identity v{self.identity_version}, need "
                    f"v{IDENTITY_VERSION}); rebuild required")
        if version >= 2 and (self.lod_algo_version != LOD_ALGO_VERSION):
            raise ValueError(
                f"{idx_path}: unsupported index version {version}: LOD "
                f"algorithm v{self.lod_algo_version} != v{LOD_ALGO_VERSION}; "
                f"rebuild required")
        self.header = hdr
        # ---- Phase 7A.2: READABLE is not PRODUCTION_VALID -------------------
        # A cache that predates a semantic field, or records a value this build
        # does not produce, is STALE: still openable for regression/debugging,
        # reported honestly, and never silently used as a current spatial
        # reference. A semantic version NEWER than this build is refused.
        self.semantic_versions = read_semantic_versions(hdr)
        self.cache_status, self.cache_status_detail = evaluate_cache_status(
            version, self.semantic_versions)
        self.layout_fingerprint = (int(hdr["layout_fingerprint"])
                                   if "layout_fingerprint" in hdr.dtype.names
                                   else 0)
        if self.cache_status == CACHE_INCOMPATIBLE:
            raise ValueError(
                f"{idx_path}: cache is INCOMPATIBLE: "
                f"{self.cache_status_detail.get('reason', 'unsupported')}")
        self.uuid = bytes(hdr["dataset_uuid"])
        self.total_points = int(hdr["source_point_count"])
        self.build_state = int(hdr["build_state"])
        self.overview_block_id = int(hdr["overview_block_id"])
        self.point_cache_name = bytes(hdr["point_cache_name"]).rstrip(b"\x00").decode()
        self.bounds_min = np.asarray(hdr["bounds_min"], dtype=np.float64)
        self.bounds_max = np.asarray(hdr["bounds_max"], dtype=np.float64)

        n_src = int(hdr["source_count"])
        n_node = int(hdr["node_count"])
        n_blk = int(hdr["block_count"])
        base = self._hdr_dtype.itemsize
        self._map_sources(base, n_src)
        base += n_src * self._src_dtype.itemsize
        self._map_nodes(base, n_node)
        base += n_node * NODE_ENTRY.itemsize
        self._map_blocks(base, n_blk)
        base += n_blk * BLOCK_ENTRY.itemsize
        self._map_microcells(base, int(hdr["microcell_count"]))
        # A sorted key list for (node_id, lod) -> block lookup. Built once and
        # queried with searchsorted, so a lookup is O(log n), never a scan.
        self._build_lookup()

    def _map_sources(self, offset, count):
        if count <= 0:
            self.sources = np.zeros(0, dtype=self._src_dtype)
            return
        self.sources = np.memmap(self.path, dtype=self._src_dtype, mode="r",
                                 offset=offset, shape=(count,))

    @property
    def stats(self):
        """Global statistics (GlobalStats) or None for a cache without them."""
        if getattr(self, "_stats_loaded", False):
            return self._stats
        self._stats_loaded = True
        self._stats = None
        if self.version >= 2 and int(self.header["stats_bytes"]) > 0:
            from .global_stats import GlobalStats
            self.fh.seek(int(self.header["stats_offset"]))
            blob = self.fh.read(int(self.header["stats_bytes"]))
            self._stats = GlobalStats.from_bytes(blob)
        return self._stats

    def _map_nodes(self, offset, count):
        if count <= 0:
            self.nodes = np.zeros(0, dtype=NODE_ENTRY)
            return
        self.nodes = np.memmap(self.path, dtype=NODE_ENTRY, mode="r",
                               offset=offset, shape=(count,))

    def _map_blocks(self, offset, count):
        if count <= 0:
            self.blocks = np.zeros(0, dtype=BLOCK_ENTRY)
            return
        self.blocks = np.memmap(self.path, dtype=BLOCK_ENTRY, mode="r",
                                offset=offset, shape=(count,))

    def _map_microcells(self, offset, count):
        self.microcells = (np.memmap(self.path, dtype=MICROCELL_ENTRY,
                                     mode="r", offset=offset,
                                     shape=(count,)) if count > 0
                       else np.zeros(0, dtype=MICROCELL_ENTRY))

    def _build_lookup(self):
        if self.blocks.size == 0:
            self._lk_keys = np.zeros(0, dtype=np.int64)
            self._lk_rows = np.zeros(0, dtype=np.int64)
            return
        key = (self.blocks["node_id"].astype(np.int64) << 8) \
            | self.blocks["lod"].astype(np.int64)
        order = np.argsort(key, kind="stable")
        self._lk_keys = key[order]
        self._lk_rows = order.astype(np.int64)

    def find_block(self, node_id: int, lod: int):
        """Return the BLOCK_ENTRY for (node_id, lod), or None. O(log n)."""
        key = (int(node_id) << 8) | int(lod)
        i = np.searchsorted(self._lk_keys, key)
        if i < self._lk_keys.size and int(self._lk_keys[i]) == key:
            return self.blocks[int(self._lk_rows[i])]
        return None

    def node_row(self, node_id: int):
        """Return the NODE_ENTRY row for a node id, or None."""
        if self.nodes.size == 0:
            return None
        rows = np.flatnonzero(self.nodes["node_id"] == int(node_id))
        if rows.size == 0:
            return None
        return self.nodes[int(rows[0])]

    def blocks_for_node(self, node_id: int):
        if self.blocks.size == 0:
            return []
        sel = self.blocks["node_id"] == int(node_id)
        return self.blocks[sel]

    def lods_present(self, node_id: int):
        return sorted(int(b["lod"]) for b in self.blocks_for_node(node_id))

    def summary(self) -> str:
        return (f"{self.total_points:,} pts | {self.nodes.size:,} nodes | "
                f"{self.blocks.size:,} blocks | build_state="
                f"{self.build_state} | pc={self.point_cache_name}")

    def close(self):
        for a in ("sources", "nodes", "blocks"):
            m = getattr(self, a, None)
            if isinstance(m, np.memmap):
                m._mmap.close() if hasattr(m, "_mmap") else None
        if getattr(self, "fh", None):
            self.fh.close()


# ==========================================================================
# NKEDIT001 - sparse classification overlay (Part 3, Part 37, Part 38)
#
# The point cache is IMMUTABLE. A classification change never rewrites a block;
# it appends a record keyed by the STABLE source id. When a detailed tile
# loads, its base classification is overlaid with the matching records BEFORE
# upload. That keeps editing O(edited points) instead of O(dataset), and keeps
# undo/redo a matter of manipulating this map.
# ==========================================================================


class EditFile:
    """Append-only sparse edit overlay, keyed by stable source id."""

    def __init__(self, path: str, dataset_uuid: bytes = None):
        self.path = path
        self.tmp = path + ".tmp"
        self.uuid = dataset_uuid
        self.records = {}       # source_point_id -> classification
        self.revision = 0

    def load(self):
        if not os.path.isfile(self.path):
            return self
        with open(self.path, "rb") as f:
            raw = f.read(EDIT_HEADER.itemsize)
            if len(raw) < EDIT_HEADER.itemsize:
                return self
            hdr = np.frombuffer(raw, dtype=EDIT_HEADER, count=1)[0]
            magic = bytes(hdr["magic"]).rstrip(b"\x00")
            if magic != EDIT_MAGIC.rstrip(b"\x00"):
                raise ValueError(f"{self.path}: bad edit magic {magic!r}")
            if int(hdr["version"]) != EDIT_VERSION:
                raise ValueError(f"{self.path}: unsupported edit version "
                                 f"{int(hdr['version'])}")
            self.uuid = bytes(hdr["dataset_uuid"])
            self.revision = int(hdr["revision"])
            n = int(hdr["record_count"])
            if n > 0:
                body = f.read(EDIT_RECORD.itemsize * n)
                recs = np.frombuffer(body, dtype=EDIT_RECORD, count=n)
                self.records = {int(r["source_point_id"]): int(r["classification"])
                                for r in recs}
        return self

    def set_class(self, source_point_id: int, classification: int):
        self.records[int(source_point_id)] = int(classification) & 0xFF
        self.revision += 1

    def get_class(self, source_point_id: int, default: int) -> int:
        return self.records.get(int(source_point_id), default)

    def apply_to_stream(self, classification: np.ndarray,
                        source_ids: np.ndarray) -> int:
        """Overlay edits onto a freshly decoded tile's classification, in place.

        Cost is O(points in THIS TILE), never O(dataset). Returns how many
        points actually changed so a caller can skip a GPU upload when nothing
        did (Part 38).
        """
        if not self.records:
            return 0
        # Vectorised lookup instead of a Python loop over points.
        ids = np.fromiter(self.records.keys(), dtype=np.uint64,
                          count=len(self.records))
        vals = np.fromiter(self.records.values(), dtype=np.uint8,
                           count=len(self.records))
        order = np.argsort(ids, kind="stable")
        ids = ids[order]
        vals = vals[order]
        pos = np.searchsorted(ids, source_ids)
        pos_clipped = np.clip(pos, 0, max(0, ids.size - 1))
        hit = (ids.size > 0) & (pos_clipped < ids.size) \
            & (ids[pos_clipped] == source_ids)
        n = int(hit.sum())
        if n:
            classification[:] = np.where(
                hit, vals[pos_clipped].astype(np.uint8), classification)
        return n

    def commit(self):
        hdr = np.zeros(1, dtype=EDIT_HEADER)
        hdr["magic"] = EDIT_MAGIC
        hdr["version"] = EDIT_VERSION
        hdr["endianness"] = 0x01020304
        hdr["header_bytes"] = EDIT_HEADER.itemsize
        hdr["dataset_uuid"] = np.frombuffer(self.uuid or b"\x00" * 16, dtype=np.uint8)
        hdr["record_count"] = len(self.records)
        hdr["revision"] = self.revision
        hdr["created_unix"] = now_unix()
        recs = np.zeros(max(0, len(self.records)), dtype=EDIT_RECORD)
        for i, (k, v) in enumerate(self.records.items()):
            recs[i]["source_point_id"] = k
            recs[i]["classification"] = v
            recs[i]["revision"] = self.revision
        with open(self.tmp, "wb") as f:
            hdr.tofile(f)
            if recs.size:
                recs.tofile(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(self.tmp, self.path)
        return self.path

    def __len__(self):
        return len(self.records)
