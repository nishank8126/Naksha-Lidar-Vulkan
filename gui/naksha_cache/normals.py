"""normals.py - the .nakshanorm sidecar: format, writer, random-access reader.

WHY A SIDECAR
=============
The committed NKPC has no normal stream yet. Rather than destabilise it, the
stored oct16x2 normals live beside it in a versioned sidecar whose layout is
deliberately the SAME shape the future NKPC NORMAL stream will have, so it can
be folded in later without a format rewrite.

DESIGN CONSTRAINTS THIS FILE HONOURS
=====================================
* BLOCK-ALIGNED PAYLOAD. The directory maps (block_id -> offset, bytes, crc),
  so runtime is "need block 814 -> seek -> read exactly those normals". The
  whole normal file is NEVER loaded, which is what keeps a 1B-point dataset
  viable.
* RAW, NOT ZSTD. Measured gain was ~4.00 -> ~3.95 B/point (~1%), which does not
  pay for decompression CPU. codec is recorded as NONE so a future writer can
  change it without a reader rewrite.
* PAYLOAD ORDER == NKPC BLOCK POINT ORDER, exactly. That is what makes
  XYZ/CLASS/NORMAL alignment a property of the format rather than a hope.
* CANONICAL, NOT PER-LOD. A normal belongs to a STABLE SOURCE ID. LOD1/2/3/4
  blocks are decimated COPIES of the same source points, so they reuse the same
  canonical normal. Triangulating per LOD would give one surface a different
  normal at different zoom levels, which shows up as lighting popping on zoom.
"""

from __future__ import annotations

import hashlib
import os
import struct
import zlib
from pathlib import Path
from typing import Dict, Optional

import numpy as np

# Exactly 16 bytes so it fills the header's `16s` field with no implicit
# padding - a padded magic would compare unequal on read and every sidecar
# would look corrupt.
NORM_MAGIC = b"NKNORM01" + b"\x00" * 8
NORM_VERSION = 1
NORM_CODEC_NONE = 0
ENCODING_OCT16_X2 = 1
BYTES_PER_NORMAL = 4

# 160 bytes, fixed, so the header is one seek. Pinned with an assert below
# because a silent size change would misalign every directory entry.
#   magic | version | source_fp | idx_fp | source_file_count | source_point_count
#   stored_normal_point_count | block_count | encoding | codec | bytes_per_normal
#   directory_offset | payload_offset | payload_crc | directory_crc | pad
NORM_HEADER = struct.Struct("<16sI16s16sQQQQIIIQQQQ32s")
# PHASE 6C.4 - POINT-CACHE LAYOUT BINDING. The header always had a second,
# never-written 16-byte fingerprint slot (`idx_fp`). It now carries the point
# cache's LAYOUT FINGERPRINT (Phase 7A), marker-tagged so a sidecar written by
# an older builder - which stored the source fingerprint in that slot - is
# recognised as LEGACY and stays READABLE rather than being misread as a
# mismatching key and rejected.
LAYOUT_FP_MAGIC = b"NKLAYOUT"


def pack_layout_fingerprint(value: int) -> bytes:
    """16 bytes: marker + 8-byte little-endian fingerprint (never a hash)."""
    try:
        v = int(value) & 0xFFFFFFFFFFFFFFFF
    except Exception:
        v = 0
    return LAYOUT_FP_MAGIC + v.to_bytes(8, "little")


def unpack_layout_fingerprint(blob: bytes) -> int:
    """Fingerprint recorded in the slot, or 0 when nothing valid is recorded.

    0 means "not recorded" (legacy sidecar) and is deliberately NOT an error:
    the project rule is that a new version field may never hard-reject a cache
    that worked before. It is reported, not enforced.
    """
    try:
        raw = bytes(blob or b"")
    except Exception:
        return 0
    if len(raw) != 16 or not raw.startswith(LAYOUT_FP_MAGIC):
        return 0
    return int.from_bytes(raw[8:16], "little")
# Directory entry: lod | point_count | payload_offset | payload_bytes | crc | pad
# (36 bytes; block_id is the entry INDEX, so lookup needs no key table)
NORM_DIR_ENTRY = struct.Struct("<IQQQII")
assert NORM_HEADER.size == 160, NORM_HEADER.size


def oct_encode(normals: np.ndarray) -> np.ndarray:
    """(N,3) unit normals -> (N,2) int16 octahedral pairs.

    Standard separable octahedral mapping, L1-normalised, lower hemisphere
    folded onto the upper one. This is the EXACT inverse of instant.frag's
    octDecode(); tests assert the angular round-trip error, because a sign slip
    here mirrors every normal about the diagonal and looks like plausible but
    wrong lighting.
    """
    n = np.asarray(normals, dtype=np.float64)
    n = n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    inv = 1.0 / (np.abs(n[:, 0]) + np.abs(n[:, 1]) + np.abs(n[:, 2]))
    f = n * inv[:, None]
    x, y, z = f[:, 0], f[:, 1], f[:, 2]
    lower = z < 0.0
    xs = np.where(x >= 0.0, 1.0, -1.0)
    ys = np.where(y >= 0.0, 1.0, -1.0)
    # BOTH folded components must use the ORIGINAL x and y. Using the
    # already-folded x here is the classic oct bug: it is self-consistent-looking
    # but decodes up to ~90 degrees wrong on a large fraction of directions.
    xo = np.where(lower, (1.0 - np.abs(y)) * xs, x)
    yo = np.where(lower, (1.0 - np.abs(x)) * ys, y)
    return np.stack([np.rint(xo * 32767.0), np.rint(yo * 32767.0)],
                    axis=1).astype(np.int16)


def oct_decode(packed: np.ndarray) -> np.ndarray:
    """(N,2) int16 -> (N,3) unit normals. Mirrors instant.frag exactly."""
    p = np.asarray(packed, dtype=np.float64) / 32767.0
    x, y = p[:, 0], p[:, 1]
    z = 1.0 - np.abs(x) - np.abs(y)
    t = np.maximum(-z, 0.0)
    x = x + np.where(x >= 0.0, -t, t)
    y = y + np.where(y >= 0.0, -t, t)
    n = np.stack([x, y, z], axis=1)
    return n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)


def sidecar_path(dataset: str) -> Path:
    return Path(str(dataset) + ".nakshanorm")


def source_fingerprint(dataset: str) -> bytes:
    """16-byte fingerprint of the source dataset + its committed NKIDX.

    A normal cache is valid only for the point/block layout it was built from.
    If either changes the cache is STALE and must not be used silently - block
    offsets would point at the wrong points.

    Exactly 16 bytes so it fills the header 16s field unpadded; an 8-byte value
    is zero-padded on write and then never compares equal on read, which would
    make every freshly built cache look stale.

    """
    ds = Path(dataset)
    idx = Path(str(dataset) + ".nakshaidx")
    h = hashlib.blake2b(ds.name.encode("utf-8"), digest_size=16).digest()
    if idx.is_file():
        st = idx.stat()
        h = hashlib.blake2b(h + st.st_mtime_ns.to_bytes(8, "little"),
                            digest_size=16).digest()
        h = hashlib.blake2b(h + str(st.st_size).encode(),
                            digest_size=16).digest()
    return h


def _header_fields(**kw) -> tuple:
    base = dict(magic=NORM_MAGIC, version=NORM_VERSION,
                source_fp=b"\x00" * 16, idx_fp=b"\x00" * 16,
                source_file_count=0, source_point_count=0,
                stored_normal_point_count=0, block_count=0,
                encoding=ENCODING_OCT16_X2, codec=NORM_CODEC_NONE,
                bytes_per_normal=BYTES_PER_NORMAL,
                directory_offset=0, payload_offset=0,
                payload_crc=0, directory_crc=0)
    base.update(kw)
    return (base["magic"], base["version"], base["source_fp"], base["idx_fp"],
            base["source_file_count"], base["source_point_count"],
            base["stored_normal_point_count"], base["block_count"],
            base["encoding"], base["codec"], base["bytes_per_normal"],
            base["directory_offset"], base["payload_offset"],
            base["payload_crc"], base["directory_crc"], b"")



class NormalCacheWriter:
    """Atomic, block-aligned sidecar writer.

    Writes <dataset>.nakshanorm.tmp and renames on commit, so an interrupted
    build can NEVER appear valid. The directory is written last because it
    records every payload offset.
    """

    def __init__(self, dataset: str, source_file_count: int,
                 source_point_count: int, layout_fingerprint: int = 0):
        self.dataset = str(dataset)
        self.final_path = sidecar_path(self.dataset)
        self.tmp_path = Path(str(self.final_path) + ".tmp")
        self.source_file_count = int(source_file_count)
        self.source_point_count = int(source_point_count)
        # Phase 6C.4: the point-cache layout this sidecar's per-block offsets and
        # point ordering belong to. Stamped into the header so a rebuilt point
        # cache can never silently attach this payload.
        self.layout_fingerprint = int(layout_fingerprint or 0)
        self.entries: Dict[int, dict] = {}
        self.stored_points = 0
        self.payload_crc = 0
        if self.tmp_path.exists():
            self.tmp_path.unlink()
        self._fh = open(self.tmp_path, "wb")
        self._fh.write(NORM_HEADER.pack(*_header_fields()))
        self.payload_offset = self._fh.tell()

    def add_block(self, block_id: int, lod: int, packed: np.ndarray) -> None:
        """Append one block's normals in EXACT NKPC block point order.

        block_id MUST be the next contiguous id: the directory INDEX is the
        block_id (that is what makes random access a dict hit with no key
        table), so a gap would silently shift every later block's id. Real NKPC
        block ids are 0..N-1 contiguous; enforcing it here turns a corrupt
        cache into an error instead of wrong normals on the wrong points.
        """
        bid = int(block_id)
        if bid != len(self.entries):
            raise ValueError(
                f"block ids must be contiguous from 0: expected "
                f"{len(self.entries)}, got {bid}")
        arr = np.ascontiguousarray(packed, dtype=np.int16)
        if arr.ndim != 2 or arr.shape[1] != 2:
            raise ValueError("packed normals must be (N,2) int16")
        raw = arr.tobytes()
        off = self._fh.tell()
        self._fh.write(raw)
        self.payload_crc = zlib.crc32(raw, self.payload_crc)
        self.entries[bid] = {
            "lod": int(lod), "point_count": int(arr.shape[0]),
            "offset": off, "bytes": len(raw),
            "crc": zlib.crc32(raw) & 0xFFFFFFFF,
        }
        self.stored_points += int(arr.shape[0])

    def commit(self) -> Path:
        dir_offset = self._fh.tell()
        buf = bytearray()
        for block_id in sorted(self.entries):
            e = self.entries[block_id]
            buf += NORM_DIR_ENTRY.pack(e["lod"], e["point_count"],
                                       e["offset"], e["bytes"], e["crc"], 0)
        dir_bytes = bytes(buf)
        self._fh.write(dir_bytes)
        fp = source_fingerprint(self.dataset)
        self._fh.seek(0)
        self._fh.write(NORM_HEADER.pack(*_header_fields(
            source_fp=fp,
            idx_fp=pack_layout_fingerprint(self.layout_fingerprint),
            source_file_count=self.source_file_count,
            source_point_count=self.source_point_count,
            stored_normal_point_count=self.stored_points,
            block_count=len(self.entries),
            directory_offset=dir_offset,
            payload_offset=self.payload_offset,
            payload_crc=self.payload_crc & 0xFFFFFFFF,
            directory_crc=zlib.crc32(dir_bytes) & 0xFFFFFFFF)))
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        # Atomic publish: the sidecar only becomes visible once it is complete,
        # so an interrupted build can never be mistaken for a valid cache.
        os.replace(self.tmp_path, self.final_path)
        return self.final_path

    def abort(self) -> None:
        """Drop an unfinished build. Leaves no .nakshanorm behind."""
        try:
            self._fh.close()
        finally:
            if self.tmp_path.exists():
                self.tmp_path.unlink()


class NormalCacheReader:
    """Random-access O(1)-per-block normal reader.

    Opens with one file handle and never materialises the payload, so a
    110 MiB sidecar (27M points) - or a multi-GB one (1B) - costs no RAM beyond
    the page cache. read_block_normals() memcpy's into a fresh array, so it is
    safe to call from several stream worker threads at once.
    """

    def __init__(self, dataset: str, verify_source: bool = True,
                 expected_layout_fingerprint: Optional[int] = None):
        self.path = sidecar_path(dataset)
        if not self.path.is_file():
            raise FileNotFoundError(str(self.path))
        self._fh = open(self.path, "rb")
        raw = self._fh.read(NORM_HEADER.size)
        if len(raw) < NORM_HEADER.size:
            raise ValueError("truncated .nakshanorm header")
        f = NORM_HEADER.unpack(raw)
        if f[0] != NORM_MAGIC:
            raise ValueError(f"bad magic {f[0]!r}")
        if int(f[1]) != NORM_VERSION:
            raise ValueError(f"unsupported sidecar version {f[1]}")
        self.source_fp, self.idx_fp = f[2], f[3]
        # Phase 6C.4: the parent point cache's layout fingerprint, when one is
        # recorded. 0 == not recorded (older sidecar) -> readable, reported.
        self.layout_fingerprint = unpack_layout_fingerprint(self.idx_fp)
        self.layout_fingerprint_recorded = bool(self.layout_fingerprint)
        self.source_file_count = int(f[4])
        self.source_point_count = int(f[5])
        self.stored_normal_point_count = int(f[6])
        self.block_count = int(f[7])
        self.encoding = int(f[8])
        self.codec = int(f[9])
        self.bytes_per_normal = int(f[10])
        self.directory_offset = int(f[11])
        self.payload_offset = int(f[12])
        self.payload_crc = int(f[13])
        self.directory_crc = int(f[14])
        if self.encoding != ENCODING_OCT16_X2 or self.bytes_per_normal != 4:
            raise ValueError("unsupported normal encoding")
        self._index: Dict[int, dict] = {}
        self._load_directory()
        if verify_source:
            fp = source_fingerprint(dataset)
            if fp != self.source_fp:
                raise ValueError(
                    "normal cache is STALE: source/index fingerprint changed. "
                    "Rebuild - block offsets no longer match the point layout.")
        if expected_layout_fingerprint is not None:
            want = int(expected_layout_fingerprint or 0)
            got = int(self.layout_fingerprint or 0)
            if want and got and want != got:
                # The point cache was rebuilt with a different layout (block
                # ordering, depth, LOD ladder, attribute mask or bounds). Block
                # offsets would point at different points, so this payload is
                # refused instead of shading the wrong points.
                raise ValueError(
                    f"normal cache is STALE: layout fingerprint mismatch "
                    f"(sidecar 0x{got:016x}, point cache 0x{want:016x}). "
                    f"Rebuild - block offsets no longer match the point layout.")

    def _load_directory(self) -> None:
        self._fh.seek(self.directory_offset)
        need = self.block_count * NORM_DIR_ENTRY.size
        raw = self._fh.read(need)
        if len(raw) < need:
            raise ValueError("truncated normal directory")
        if (zlib.crc32(raw) & 0xFFFFFFFF) != self.directory_crc:
            raise ValueError("normal directory CRC mismatch")
        for i in range(self.block_count):
            lod, count, off, nbytes, ecrc, _p2 = \
                NORM_DIR_ENTRY.unpack_from(raw, i * NORM_DIR_ENTRY.size)
            # Entries are written sorted by block_id, so the directory INDEX IS
            # the block_id - lookup stays a dict hit with no key table.
            self._index[i] = {"lod": int(lod), "point_count": int(count),
                              "offset": int(off), "bytes": int(nbytes),
                              "crc": int(ecrc)}

    def has_block(self, block_id: int) -> bool:
        return int(block_id) in self._index

    def block_info(self, block_id: int) -> Optional[dict]:
        return self._index.get(int(block_id))

    def read_block_normals(self, block_id: int, verify_crc: bool = False) -> np.ndarray:
        """(N,2) int16 packed octahedral pairs - NO float expansion.

        This is what the streaming adapter hands to nkv_set_point_normals; the
        GPU decodes. Expanding to float32x3 here would triple the bytes and add a
        CPU pass for zero visual benefit.
        """
        e = self._index.get(int(block_id))
        if e is None:
            raise KeyError(f"no normals for block {block_id}")
        n = e["point_count"]
        self._fh.seek(e["offset"])
        raw = self._fh.read(e["bytes"])
        if len(raw) != e["bytes"] or len(raw) != n * 4:
            raise ValueError(f"block {block_id}: short read")
        if verify_crc and (zlib.crc32(raw) & 0xFFFFFFFF) != e["crc"]:
            raise ValueError(f"block {block_id}: normal payload CRC mismatch")
        return np.frombuffer(raw, dtype=np.int16).reshape(n, 2)

    def stored_bytes(self) -> int:
        return self.path.stat().st_size

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

        os.fsync(self._fh.fileno())
        self._fh.close()
        os.replace(self.tmp_path, self.final_path)
        return self.final_path

    def abort(self) -> None:
        try:
            self._fh.close()
        finally:
            if self.tmp_path.exists():
                self.tmp_path.unlink()
