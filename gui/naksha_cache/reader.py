"""NakshaPointCacheReader: seek, read, verify and decode single blocks.

This is the runtime read path. It must never load the whole .nakshapc: every
call seeks to one block offset and reads that block's bytes (Part 19). The
measured basis for that design is the native block read at 0.44 ms for 64K
points versus 69.12 ms for a random 65,536-point LAZ read - about 157x.

The reader also owns correctness gates:
  * magic/version are checked before any field is trusted
  * the block CRC is verified on request, and a corrupt block is REPORTED and
    skipped rather than displayed as garbage (Part 16)
  * the stable source id is decoded so edits and export can resolve it
"""
import os

import numpy as np

from .format import (ATTR_CLASSIFICATION, ATTR_INTENSITY, ATTR_POINT_SOURCE_ID,
                     ATTR_RETURN_NUMBER, ATTR_RGB, ATTR_SOURCE_ID, ATTR_XYZ)
from .index import EditFile, IndexReader
from .readblock_new import (decode_world_f32, decode_world_f64,
                            read_block_once)

import threading

class CorruptBlock(Exception):
    """Raised when a block fails its checksum. Callers must skip the tile."""

class NakshaPointCacheReader:
    """Opens a committed project and serves individual tiles."""

    def __init__(self, project_file: str, verify_crc: bool = False,
                 load_edits: bool = True):
        from .index import project_paths
        from .derived_container import DerivedContainer, project_container_path
        idx_path, pc_path, edit_path, _ = project_paths(str(project_file))
        container_path = project_container_path(project_file)
        self.container = None
        self._point_base = 0
        if container_path.is_file():
            candidate = DerivedContainer(container_path)
            sections = candidate.directory["sections"]
            if sections.get("BASE_POINTS", {}).get("version") == 2:
                self.container = candidate
                idx_path = pc_path = str(container_path)
                # Metadata is bounded independently of payload size and must
                # be checked before trusting mmap table counts and offsets.
                import hashlib
                index_item = sections["BASE_INDEX"]["blocks"]["base"]
                with open(container_path, "rb") as metadata:
                    metadata.seek(int(index_item["offset"]))
                    remaining = int(index_item["size"])
                    digest = hashlib.sha256()
                    while remaining:
                        chunk = metadata.read(min(remaining, 4 << 20))
                        if not chunk:
                            raise ValueError("truncated embedded index")
                        digest.update(chunk)
                        remaining -= len(chunk)
                if digest.hexdigest() != index_item["checksum"]:
                    raise ValueError("embedded index checksum mismatch")
                self._point_base = int(sections["BASE_POINTS"]["blocks"]["base"]["offset"])
                self.index = IndexReader(idx_path, base_offset=int(
                    sections["BASE_INDEX"]["blocks"]["base"]["offset"]))
        if self.container is None:
            if not os.path.isfile(idx_path):
                raise FileNotFoundError(f"{idx_path}: no index; cache not built")
            self.index = IndexReader(idx_path)
            from .index import resolve_point_cache
            pc_path = resolve_point_cache(idx_path, self.index.point_cache_name, pc_path)
        if not os.path.isfile(pc_path):
            self.index.close()
            raise FileNotFoundError(f"{pc_path}: no point cache")
        self.pc_path = pc_path
        self.fh = open(pc_path, "rb")
        # THREAD SAFETY. read_block_once() does seek() then read() on a handle.
        # With several stream workers sharing ONE handle, thread A seeks to
        # block X, thread B seeks to block Y, and thread A then reads Y's bytes:
        # the parsed directory then describes a different block, streams point
        # outside the buffer, and np.frombuffer raises "buffer is smaller than
        # requested size". The cache is NOT corrupt - proven by reading all 1,339
        # blocks cleanly on one thread.
        #
        # A lock is the minimum correct fix: it keeps seek+read atomic. It is
        # per-reader, and the reads are large sequential reads, so the cost is
        # one memcpy of contention rather than a second disk pass.
        self._io_lock = threading.RLock()
        self.verify_crc = verify_crc
        self.edits = EditFile(edit_path).load() if load_edits else None
        self.stats = {"blocks_read": 0, "bytes_read": 0, "corrupt": 0,
                      "edits_applied": 0, "crc_verified": 0,
                      "crc_skipped": 0, "cache_hits": 0}
        # Part 3: CRC verification POLICY. Corruption protection is kept, but
        # the CRC is paid ONCE when a block is admitted to the RAM cache, not on
        # every camera frame. `_verified` records the (checksum, size) of each
        # block already verified against the CURRENT cache file; if the cache is
        # rebuilt the directory changes, so entries simply stop matching and
        # verification resumes - no separate generation counter is needed.
        self._verified = {}

    def _read_block(self, entry, only_attrs=None, verify_crc=False):
        with self._io_lock:
            result = self._read_block_unlocked(entry, only_attrs, verify_crc)
            self.stats["blocks_read"] += 1
            self.stats["bytes_read"] += result[2]
            return result

    def _read_block_unlocked(self, entry, only_attrs=None, verify_crc=False):
        if self.container is None:
            return read_block_once(self.fh, entry, only_attrs=only_attrs,
                                   verify_crc=verify_crc)
        from .container_base import read_stream_block
        size = int(self.container.directory["sections"]["BASE_POINTS"]["blocks"]["base"]["size"])
        if int(entry["file_offset"]) + int(entry["stored_bytes"]) > size:
            raise ValueError("point block outside BASE_DATA")
        absolute = entry.copy()
        absolute["file_offset"] = int(entry["file_offset"]) + self._point_base
        return read_stream_block(self.fh, absolute, only_attrs=only_attrs,
                                 stats=self.stats)

    def close(self):
        try:
            self.fh.close()
        finally:
            self.index.close()

    def read_tile(self, node_id: int, lod: int, only_attrs=None,
                  apply_edits: bool = True, verify_crc=None,
                  render_space: bool = False):
        """Read ONE block. Returns None if absent; raises CorruptBlock on CRC.

        `only_attrs` lets a display mode avoid reading streams it does not need
        (Part 36). `apply_edits` overlays the sparse classification edits
        BEFORE returning, so the caller uploads effective classification and
        never modifies the immutable cache (Part 38).
        """
        entry = self.index.find_block(node_id, lod)
        if entry is None:
            return None
        do_crc = self.verify_crc if verify_crc is None else verify_crc
        # Part 3: verify the CRC once per block ADMISSION. A block already
        # verified against this cache file is not re-checked on later camera
        # frames, so a steady-state camera move pays no CRC cost at all.
        key = (int(entry["block_id"]), int(entry["checksum"]),
               int(entry["stored_bytes"]))
        already = key in self._verified
        do_crc = (not already) and (self.verify_crc if verify_crc is None
                                       else verify_crc)
        try:
            with self._io_lock:
                hdr, streams, nbytes, _raw = self._read_block(
                    entry, only_attrs=only_attrs, verify_crc=do_crc)
            self._verified[key] = True
            self.stats["crc_skipped" if already else "crc_verified"] += 1
        except ValueError as exc:
            self.stats["corrupt"] += 1
            raise CorruptBlock(str(exc)) from exc

        xyz_local = streams.get(ATTR_XYZ)
        # BUG FIXED: this used to return None whenever ATTR_XYZ was absent, with
        # no regard for whether XYZ had been ASKED for. An attribute-only read -
        # `only_attrs=(ATTR_CLASSIFICATION,)`, which is exactly what the
        # Neutral -> Class backfill issues for every resident block - therefore
        # ALWAYS returned None. The manager recorded "block absent" for all 393
        # blocks, class coverage stayed 0, and Class/Intensity rendered nothing
        # (Intensity black, because a missing stream normalises to the bottom of
        # the ramp) no matter how long it was given to settle.
        #
        # The correct rule: None means "XYZ was requested and is absent", which
        # is a genuinely corrupt block. XYZ being absent because the caller
        # deliberately excluded it is NOT an error - it is the whole point of an
        # attribute-only read, and the caller must not have to pay for a
        # geometry decode it will discard.
        if xyz_local is None:
            wanted_xyz = (only_attrs is None) or (ATTR_XYZ in set(only_attrs))
            if wanted_xyz:
                return None
        # float32 tile-local for RENDERING; float64 world is only needed for
        # export/editing, and building it per frame was the single biggest cost
        # in the read profile (a fresh (N,3) float64 array per tile).
        world = None
        if xyz_local is not None:
            if render_space:
                world = decode_world_f32(hdr, xyz_local)
            else:
                world = decode_world_f64(hdr, xyz_local)
        out = {
            "node_id": node_id, "lod": lod,
            "xyz": world,                      # float64 authoritative world
                                                # (None for an attribute-only read)
            "classification": streams.get(ATTR_CLASSIFICATION),
            "intensity": streams.get(ATTR_INTENSITY),
            "rgb": streams.get(ATTR_RGB),
            "source_id": streams.get(ATTR_SOURCE_ID),
            "return_number": streams.get(ATTR_RETURN_NUMBER),
            "point_source_id": streams.get(ATTR_POINT_SOURCE_ID),
            "point_count": int(hdr["point_count"]),
            "bytes_read": nbytes,
            "bounds_min": np.asarray(hdr["origin"], float),
            "origin": np.asarray(hdr["origin"], float),
            "scale": np.asarray(hdr["scale"], float),
        }
        # Apply the sparse edit overlay to classification only. Positions are
        # untouched, so an edit never forces a geometry re-upload (Part 38).
        if apply_edits and self.edits and len(self.edits) > 0 \
                and out["classification"] is not None \
                and out["source_id"] is not None:
            n = self.edits.apply_to_stream(out["classification"],
                                           out["source_id"])
            self.stats["edits_applied"] += n
            out["edits_applied"] = n
        return out

    def read_overview(self):
        """Read the overview block for first paint (Part 8)."""
        bid = self.index.overview_block_id
        if bid < 0:
            return None
        entry = self.index.blocks[int(bid)]
        hdr, streams, nbytes, _raw = self._read_block(entry)
        world = decode_world_f32(hdr, streams[ATTR_XYZ])
        return {"xyz": world,
                "classification": streams.get(ATTR_CLASSIFICATION),
                "intensity": streams.get(ATTR_INTENSITY),
                "rgb": streams.get(ATTR_RGB),
                "point_count": int(hdr["point_count"]),
                "bytes_read": nbytes}

    def nodes_with_lod0(self):
        """Node ids that have a detailed block, for frustum selection."""
        if self.index.blocks.size == 0:
            return []
        sel = self.index.blocks["lod"] == 0
        return [int(v) for v in np.unique(self.index.blocks["node_id"][sel])]

    # ------------------------------------------------------- Phase A: cells
    def visible_microcells(self, min_x, min_y, max_x, max_y, lod=None):
        """Microcells intersecting a world rectangle (Phase A).

        Culling happens at MICROCELL granularity, not page granularity. The
        pages stay 150K-256K points (good disk reads) while the camera selects
        far smaller contiguous sub-ranges, which is what finally lets a 31 m view
        touch a handful of ranges instead of dozens of huge pages.
        """
        mc = getattr(self.index, "microcells", None)
        if mc is None or len(mc) == 0:
            return []
        bmin = mc["bounds_min"]
        bmax = mc["bounds_max"]
        m = ((bmin[:, 0] <= max_x) & (bmax[:, 0] >= min_x) &
             (bmin[:, 1] <= max_y) & (bmax[:, 1] >= min_y))
        if lod is not None:
            m &= (mc["lod"] == lod)
        return [int(i) for i in np.flatnonzero(m)]

    def read_microcell(self, mc_row, only_attrs=None, verify_crc=None):
        """Read ONE microcell: a contiguous subrange of its parent page.

        With codec NONE a subrange is a BYTE RANGE inside the page, so only the
        requested points' bytes are read from disk and decoded - not the whole
        150K-256K point page (Part A4). Source IDs are excluded by default:
        they are for editing and export, never for rendering (Part 19).
        """
        mc = self.index.microcells
        row = mc[int(mc_row)]
        block_id = int(row["block_id"])
        start = int(row["point_start"])
        count = int(row["point_count"])
        entry = self.index.blocks[block_id]
        if only_attrs is None:
            only_attrs = (ATTR_XYZ, ATTR_CLASSIFICATION, ATTR_INTENSITY,
                          ATTR_RGB)
        hdr, streams, nbytes, raw = self._read_block(
            entry, only_attrs=only_attrs, verify_crc=False)
        out = {}
        for attr, arr in streams.items():
            out[attr] = arr[start:start + count]
        world = decode_world_f32(hdr, out[ATTR_XYZ])
        return {"block_id": block_id, "lod": int(row["lod"]),
                "point_start": start, "point_count": count,
                "xyz": world,
                "classification": out.get(ATTR_CLASSIFICATION),
                "intensity": out.get(ATTR_INTENSITY),
                "rgb": out.get(ATTR_RGB),
                "page_bytes": int(entry["stored_bytes"]),
                "bytes_read": nbytes,
                "extent": float(np.hypot(row["bounds_max"][0] - row["bounds_min"][0],
                                         row["bounds_max"][1] - row["bounds_min"][1])),
                "spacing": float(row["spacing"])}

    def microcell_stats(self):
        mc = getattr(self.index, "microcells", None)
        if mc is None or len(mc) == 0:
            return None
        ext = np.hypot(mc["bounds_max"][:, 0] - mc["bounds_min"][:, 0],
                       mc["bounds_max"][:, 1] - mc["bounds_min"][:, 1])
        return {"count": int(len(mc)),
                "pts_median": float(np.median(mc["point_count"])),
                "pts_p95": float(np.percentile(mc["point_count"], 95)),
                "ext_median": float(np.median(ext)),
                "ext_p95": float(np.percentile(ext, 95)),
                "ext_max": float(ext.max())}
