"""NAKSHA NATIVE POINT CACHE V1 - on-disk format definitions.

Three files, three magics, one contract:

    PROJECT.nakshaidx   NKIDX001   compact, mmap-friendly spatial index
    PROJECT.nakshapc    NKPC001    independently seekable SoA point blocks
    PROJECT.nakshaeedit NKEDIT001  sparse classification overlay

Everything is little-endian and every structure is a PACKED fixed-width numpy
dtype, so the reader can memory-map a table and index it directly without
constructing a Python object per entry. At 1.14B points that difference is the
whole design: an object-per-node index would cost gigabytes of RAM at open and
minutes of startup.

Design rules enforced here:

  * A reader MUST reject an unknown magic or version rather than guess. A
    silently misread cache on a billion-point dataset is worse than no cache.
  * Coordinates are stored as int32 LOCAL values plus a float64 tile origin and
    a per-axis scale. world = origin + local * scale. float64 authoritative
    geometry is never stored per point (Part 7).
  * RGB keeps the SOURCE dtype. NT219 is LAS 1.2 format 3, whose RGB is
    uint16; casting it to uint8 is the exact bug this format exists to prevent.
  * The source id stream is separate from the render streams, so normal
    rendering never has to read or upload it (Part 10).
"""
import os
import os
import struct
import time
import uuid

import numpy as np

# ---- magics -------------------------------------------------------------
# The magic fields are S16, NOT S8. numpy's fixed-width bytes fields reserve
# one byte for a NUL terminator, so an S8 field silently TRUNCATES an
# 8-character value to 7: "NKEDIT001" is stored as "NKEDIT00". Every magic in
# this format is exactly 8 characters, so S8 corrupts all three of them. S16
# stores the full value and NUL-pads on read, which the readers strip.
IDX_MAGIC = b"NKIDX001"
PC_MAGIC = b"NKPC001"
EDIT_MAGIC = b"NKEDIT001"
# IDX_VERSION 3 (this change): the header records ONE INDEPENDENT version per
# semantic subsystem, so a cache can no longer be "version 2" while carrying
# stale spacing/layout semantics. The v2 defect this closes, measured on a real
# 27M cache: a cache whose spatial layout was perfect and whose every recorded
# version matched the current build, but whose LOD rung spacing (a 2.15x step)
# exceeded the selector's own coherence limit (2.0x) and so could never be drawn
# coherently - accepted silently, because no field described spacing semantics.
#
# Versions 1 and 2 stay READABLE and are classed (see `evaluate_cache_status`):
#   v1 single-file  -> LEGACY_READABLE (pre-semantic-version cache)
#   v1 MULTI-file   -> rejected by the reader (source ids collide across files)
#   v2              -> STALE: readable and openable, but it cannot describe the
#                      semantics added in v3, so it is not production-valid.
# A version NEWER than this build is rejected outright - never "best effort".
IDX_VERSION = 3
IDX_VERSIONS_READABLE = (1, 2, 3)
# Semantic versions recorded in the header, each INDEPENDENT of the others.
#   IDENTITY_VERSION  1 = per-file point index (legacy; ids collide across files)
#                     2 = GLOBAL dense uint64: point_base[file] + file-local index
#   LOD_ALGO_VERSION  1 = voxel/highest-z reps, cell = leaf_cell * 2**lod
#                         (a 23x LOD0->LOD1 cliff on dense leaves)
#                     2 = nested GEOMETRIC ladder: each rung a fixed fraction
#                         of the previous rung's point count (lod_ladder.py)
#   OVERVIEW_ALGO     1 = unbounded pool + bisection (legacy)
#                     2 = bounded deterministic grid reduction, real source ids
#   STATS_VERSION     1 = exact u16 intensity histogram, 8192-bin Z histogram,
#                         256-bin class counts (global + per source)
IDENTITY_VERSION = 2
LOD_ALGO_VERSION = 2
OVERVIEW_ALGO_VERSION = 2
STATS_VERSION = 1
# ---- v3: semantics that previously had NO version of their own -------------
# Each of these was a real silent-mismatch risk: the code changed, the recorded
# version did not, and an old cache kept being accepted.
#   SPATIAL_LAYOUT_VERSION  1 = leaves are a TILE PARTITION of the survey
#                               (legacy overlapping leaves measured at
#                               overlap_ratio 30.4-39.9, i.e. a mosaic source)
#   MORTON_VERSION          1 = true interleaving of masked bits, corrected
#                               prefix shard selection
#   SHARD_VERSION           1 = coarse-prefix shard assignment matching the
#                               physical shard a record is written to
#   RECORD_ORDER_VERSION    1 = spatialize writes records in shard order
#                               (sorted boundaries + unsorted payload was Bug C)
#   LOD_SPACING_VERSION     1 = every rung stores sqrt(occupied_area / count),
#                               ONE physical quantity, comparable across nodes
#   BLOCK_PAYLOAD_VERSION   1 = block payload header/stream layout
SPATIAL_LAYOUT_VERSION = 1
MORTON_VERSION = 1
SHARD_VERSION = 1
RECORD_ORDER_VERSION = 1
LOD_SPACING_VERSION = 1
BLOCK_PAYLOAD_VERSION = 1

# (header field, current value, human label). ONE list, so the writer, the
# reader and the tests can never disagree about what is versioned.
SEMANTIC_VERSIONS = (
    ("identity_version", IDENTITY_VERSION, "point identity"),
    ("spatial_layout_version", SPATIAL_LAYOUT_VERSION, "leaf spatial layout"),
    ("morton_version", MORTON_VERSION, "Morton encoding"),
    ("shard_version", SHARD_VERSION, "shard prefix assignment"),
    ("record_order_version", RECORD_ORDER_VERSION, "spatialize record order"),
    ("lod_algo_version", LOD_ALGO_VERSION, "LOD ladder"),
    ("lod_spacing_version", LOD_SPACING_VERSION, "LOD spacing metadata"),
    ("overview_algo_version", OVERVIEW_ALGO_VERSION, "overview reduction"),
    ("stats_version", STATS_VERSION, "global statistics"),
    ("block_payload_version", BLOCK_PAYLOAD_VERSION, "block payload layout"),
)

# Subsystems that may be legitimately ABSENT from a cache (no statistics blob
# was written) rather than mismatched. Reported in the status detail, but an
# absent optional subsystem does not by itself make a cache STALE.
OPTIONAL_SEMANTIC_VERSIONS = ("stats_version",)

# ---- cache validity vs readability (Phase 7A.2) ---------------------------
# READABLE is not the same thing as PRODUCTION_VALID. An old cache may stay
# openable for regression and debugging while being reported as not spatially
# trustworthy, instead of being silently treated as current.
CACHE_CURRENT_VALID = "CURRENT_VALID"
CACHE_LEGACY_READABLE = "LEGACY_READABLE"
CACHE_STALE = "STALE"
CACHE_INCOMPATIBLE = "INCOMPATIBLE"
CACHE_CORRUPT = "CORRUPT"
CACHE_STATUSES = (CACHE_CURRENT_VALID, CACHE_LEGACY_READABLE, CACHE_STALE,
                  CACHE_INCOMPATIBLE, CACHE_CORRUPT)
# Statuses that must never be used as a spatial/performance reference.
CACHE_NOT_PRODUCTION_VALID = (CACHE_LEGACY_READABLE, CACHE_STALE,
                              CACHE_INCOMPATIBLE, CACHE_CORRUPT)


def evaluate_cache_status(fmt_version: int, recorded: dict) -> tuple:
    """(status, detail) for a cache read off disk. Pure function, no I/O.

    Rules, in order:
      * a format NEWER than this build -> INCOMPATIBLE (rejected, never
        interpreted under an older schema);
      * v1 -> LEGACY_READABLE (it predates the semantic set entirely);
      * v2 -> STALE (readable, but it cannot describe the v3 semantics);
      * v3 with a semantic version NEWER than this build -> INCOMPATIBLE;
      * v3 otherwise: CURRENT_VALID only when every subsystem matches. Any
        absent or differing version is STALE - readable, reported, not
        silently trusted.
    """
    try:
        fmt = int(fmt_version)
    except Exception:
        return CACHE_CORRUPT, {"reason": f"unreadable format version {fmt_version!r}"}
    if fmt > IDX_VERSION:
        return CACHE_INCOMPATIBLE, {
            "reason": f"cache format v{fmt} is newer than this build "
                      f"(v{IDX_VERSION})"}
    if fmt < 2:
        return CACHE_LEGACY_READABLE, {
            "reason": "index v1: predates the semantic version set"}
    if fmt < IDX_VERSION:
        return CACHE_STALE, {
            "reason": f"index v{fmt}: predates the independent semantic "
                      f"version set added in v{IDX_VERSION}",
            "unversioned": [name for name, _v, _l in SEMANTIC_VERSIONS]}
    missing, mismatched, future, absent_optional = [], {}, [], []
    for name, expected, label in SEMANTIC_VERSIONS:
        got = int(recorded.get(name, 0) or 0)
        if got == 0:
            if name in OPTIONAL_SEMANTIC_VERSIONS:
                # An absent optional subsystem (no stats blob) is a documented
                # property of the cache, not a semantic mismatch.
                absent_optional.append(name)
            else:
                missing.append(name)
        elif got > int(expected):
            future.append({"field": name, "recorded": got,
                           "build": int(expected), "label": label})
        elif got != int(expected):
            mismatched[name] = {"recorded": got, "build": int(expected),
                               "label": label}
    if future:
        return CACHE_INCOMPATIBLE, {
            "reason": "cache records semantics newer than this build",
            "future": future}
    if missing or mismatched:
        return CACHE_STALE, {"missing": missing, "mismatched": mismatched,
                            "absent_optional": absent_optional}
    return CACHE_CURRENT_VALID, ({"absent_optional": absent_optional}
                                 if absent_optional else {})
# Reserved: "no source point" (never a valid global id).
SOURCE_ID_NONE = 0xFFFFFFFFFFFFFFFF
PC_VERSION = 1
EDIT_VERSION = 1
MAGIC_FIELD = "S16"

# ---- attribute schema flags --------------------------------------------
# A bit means "this attribute stream is present". Absent attributes consume
# ZERO block bytes (Part 9 attribute masks), which is why an RGB-less dataset
# does not pay for an RGB stream.
ATTR_XYZ = 1 << 0
ATTR_CLASSIFICATION = 1 << 1
ATTR_INTENSITY = 1 << 2
ATTR_RGB = 1 << 3
ATTR_SOURCE_ID = 1 << 4
ATTR_RETURN_NUMBER = 1 << 5
ATTR_NUMBER_OF_RETURNS = 1 << 6
ATTR_SCAN_ANGLE = 1 << 7
ATTR_POINT_SOURCE_ID = 1 << 8
ATTR_GPS_TIME = 1 << 9
ATTR_USER_DATA = 1 << 10
ATTR_FLAGS = 1 << 11

ATTR_NAMES = {
    ATTR_XYZ: "xyz", ATTR_CLASSIFICATION: "classification",
    ATTR_INTENSITY: "intensity", ATTR_RGB: "rgb",
    ATTR_SOURCE_ID: "source_id", ATTR_RETURN_NUMBER: "return_number",
    ATTR_NUMBER_OF_RETURNS: "number_of_returns", ATTR_SCAN_ANGLE: "scan_angle",
    ATTR_POINT_SOURCE_ID: "point_source_id", ATTR_GPS_TIME: "gps_time",
    ATTR_USER_DATA: "user_data", ATTR_FLAGS: "flags",
}

# ---- codecs (Part 24) ---------------------------------------------------
CODEC_NONE = 0
CODEC_LZ4 = 1
CODEC_ZSTD = 2
CODEC_NAMES = {CODEC_NONE: "none", CODEC_LZ4: "lz4", CODEC_ZSTD: "zstd"}

# ---- block residency states (Part 22) -----------------------------------
TEMP_OVERVIEW = 0
BUILDING = 1
BLOCK_READY = 2
STREAMABLE = 3
FINALIZED = 4
STATE_NAMES = {TEMP_OVERVIEW: "TEMP_OVERVIEW", BUILDING: "BUILDING",
               BLOCK_READY: "BLOCK_READY", STREAMABLE: "STREAMABLE",
               FINALIZED: "FINALIZED"}

# ---- build states (Part 20) --------------------------------------------
BUILD_NOT_STARTED = 0
BUILD_RUNNING = 1
BUILD_FINALIZED = 2
BUILD_FAILED = 3

# ---- locked format decisions (do not re-derive) -------------------------
# LEAF_TARGET_POINTS tuned by the PHYSICAL PAGE GRANULARITY EXPERIMENT
# (256K->128K->64K sweep, XY16 ordering held constant, x32 on 26.96M pts):
#   256K: 16.06% decoded, 192x amplification
#   128K: 9.76% decoded, 117x amplification  (+39% pages, +0.8% cache)
#    64K: 5.76% decoded,  69x amplification  (+155% pages, +3.1% cache, faster build)
# 64K is the smallest target that clears the >=2x decoded-reduction bar vs baseline
# while keeping cache growth <3% and build time DOWN; returns are still not
# plateauing. Index stays ~25MB projected at 1.14B. PAGE SIZE IS A REAL LEVER.
LEAF_TARGET_POINTS = 64_000
OVERVIEW_TARGET_POINTS = 300_000
OVERVIEW_MIN_POINTS = 100_000
OVERVIEW_MAX_POINTS = 500_000
# Part 18: a strict cap on simultaneously open temporary shard files. Windows
# caps process handles; opening one file per final tile does not scale to
# millions of tiles.
MAX_OPEN_SHARDS = 24
# Part 17: the source chunk that is resident at once during a build pass.
BUILD_CHUNK_POINTS = 2_000_000
# Shard fan-out for the external sort (Part 18). Shards are coarse spatial
# buckets, not final tiles.
SHARD_DEPTH = 6          # 64 x 64 buckets over the dataset footprint

# ------------------------------------------------------------------------
# .nakshaidx structures
# ------------------------------------------------------------------------
IDX_HEADER_V1 = np.dtype([
    ("magic", "S16"), ("version", "u4"), ("endianness", "u4"),
    ("header_bytes", "u4"), ("flags", "u4"),
    ("dataset_uuid", "u1", 16),
    ("created_unix", "u8"),
    ("source_point_count", "u8"),
    ("bounds_min", "f8", 3), ("bounds_max", "f8", 3),
    ("crs_wkt", "S256"),
    ("source_count", "u4"), ("node_count", "u8"), ("block_count", "u8"),
    # Phase A: number of render microcells appended after the block table.
    ("microcell_count", "u8"),
    ("lod_count", "u4"), ("attribute_mask", "u4"),
    ("max_depth", "u4"), ("shard_depth", "u4"),
    ("point_cache_name", "S64"),
    ("pc_file_size", "u8"),
    ("build_state", "u4"),
    ("overview_block_id", "i8"),
    ("header_crc", "u4"),
])

# v2 header layout, kept VERBATIM as its own dtype so an existing v2 cache is
# still read correctly after the v3 fields were added. Reading v2 bytes with the
# v3 dtype would silently reinterpret the source table as header fields.
_IDX_HEADER_V2_FIELDS = [
    ("magic", "S16"), ("version", "u4"), ("endianness", "u4"),
    ("header_bytes", "u4"), ("flags", "u4"),
    ("dataset_uuid", "u1", 16),
    ("created_unix", "u8"),
    ("source_point_count", "u8"),
    ("bounds_min", "f8", 3), ("bounds_max", "f8", 3),
    ("crs_wkt", "S256"),
    ("source_count", "u4"), ("node_count", "u8"), ("block_count", "u8"),
    ("microcell_count", "u8"),
    ("lod_count", "u4"), ("attribute_mask", "u4"),
    ("max_depth", "u4"), ("shard_depth", "u4"),
    ("point_cache_name", "S64"),
    ("pc_file_size", "u8"),
    ("build_state", "u4"),
    ("overview_block_id", "i8"),
    # ---- v2 -----------------------------------------------------------
    ("identity_version", "u4"), ("lod_algo_version", "u4"),
    ("stats_version", "u4"), ("overview_algo_version", "u4"),
    ("crs_hash", "u8"),
    ("stats_offset", "u8"), ("stats_bytes", "u8"),
    ("header_crc", "u4"),
]
IDX_HEADER_V2 = np.dtype(_IDX_HEADER_V2_FIELDS)

# v3 header layout: the v2 fields, then ONE field per semantic subsystem that
# previously had no version of its own, then the layout fingerprint that derived
# sidecars bind to. `header_crc` stays LAST so it can always be located.
IDX_HEADER = np.dtype(_IDX_HEADER_V2_FIELDS[:-1] + [
    ("spatial_layout_version", "u4"),
    ("morton_version", "u4"),
    ("shard_version", "u4"),
    ("record_order_version", "u4"),
    ("lod_spacing_version", "u4"),
    ("block_payload_version", "u4"),
    ("layout_fingerprint", "u8"),
] + [_IDX_HEADER_V2_FIELDS[-1]])

# The semantic-version header field names, in header order.
VERSION_FIELD_NAMES = tuple(name for name, _v, _l in SEMANTIC_VERSIONS)


def read_semantic_versions(hdr) -> dict:
    """Every semantic version a header CARRIES, 0 for one it predates.

    0 is deliberate: it is not a valid version, so a field the layout does not
    have is reported as absent rather than as "version 1", which would let an
    old cache look like it matches.
    """
    names = set(getattr(hdr, "dtype", hdr).names or ())

    def _scalar(field):
        # A header may arrive as a (1,) structured array or as a single record,
        # so reduce to one element rather than letting int() see an array (which
        # errors on numpy 2 and warns on 1.25+).
        arr = np.asarray(hdr[field]).reshape(-1)
        return int(arr[0]) if arr.size else 0

    out = {}
    for name, _expected, _label in SEMANTIC_VERSIONS:
        out[name] = _scalar(name) if name in names else 0
    return out


def layout_fingerprint(*, versions, shard_depth, max_depth, lod_count,
                       node_count, block_count, attribute_mask,
                       bounds_min, bounds_max) -> int:
    """Stable 64-bit fingerprint of the POINT CACHE LAYOUT (Phase 7A.3).

    A derived sidecar (.nakshanorm, the future Surface cache, any AI-derived
    file) is a function of the CACHE LAYOUT it was derived from, not of the
    source file alone: one source produces a different cache after a builder
    change, and normal offsets would still line up by accident while meaning a
    different point. So the fingerprint covers the semantics that decide what a
    byte offset MEANS - every subsystem version, the shard/depth geometry, the
    counts, the attribute mask and the quantised bounds - and nothing
    incidental (no mtime, no uuid, no paths), so rebuilding the same data the
    same way reproduces the same fingerprint.

    Dependency-free and deterministic: two CRC32 passes over a canonical text
    form, giving 64 bits. This is a change-detector, not a cryptographic hash.
    """
    import zlib
    v = dict(versions or {})
    parts = ["naksha-layout-v1"]
    for name, _expected, _label in SEMANTIC_VERSIONS:
        parts.append(f"{name}={int(v.get(name, 0) or 0)}")
    parts.append(f"shard_depth={int(shard_depth)}")
    parts.append(f"max_depth={int(max_depth)}")
    parts.append(f"lod_count={int(lod_count)}")
    parts.append(f"node_count={int(node_count)}")
    parts.append(f"block_count={int(block_count)}")
    parts.append(f"attribute_mask={int(attribute_mask)}")
    # Quantised so float noise cannot flip the fingerprint; 1e-4 m is far below
    # any positioning precision the cache stores. The bounds arrive either as a
    # (3,) vector or as the (1,3) row of a one-row structured array, so flatten
    # rather than index: float() on a length-1 ndarray raises on numpy 2.
    bmin = np.asarray(bounds_min, dtype=np.float64).reshape(-1)
    bmax = np.asarray(bounds_max, dtype=np.float64).reshape(-1)
    if bmin.size < 3 or bmax.size < 3:
        raise ValueError("layout_fingerprint needs 3 bounds components")
    for axis in range(3):
        parts.append(f"bmin{axis}={round(float(bmin[axis]), 4):.4f}")
        parts.append(f"bmax{axis}={round(float(bmax[axis]), 4):.4f}")
    blob = "|".join(parts).encode("utf-8")
    lo = zlib.crc32(blob) & 0xFFFFFFFF
    hi = zlib.crc32(blob[::-1], 0x9E3779B9) & 0xFFFFFFFF
    return int((hi << 32) | lo)

# Unit codes used by the catalog (see source_catalog.py).
UNIT_UNKNOWN, UNIT_METRE, UNIT_US_FOOT, UNIT_INTL_FOOT, UNIT_DEGREE, \
    UNIT_OTHER = 0, 1, 2, 3, 4, 9

SOURCE_ENTRY_V1 = np.dtype([
    ("source_id", "u4"),
    ("path", "S512"),
    ("file_size", "u8"),
    ("mtime", "f8"),
    ("point_count", "u8"),
    ("point_base", "u8"),          # cumulative base for the stable source id
    ("las_version", "u2"), ("point_format", "u2"),
    ("scale", "f8", 3), ("offset", "f8", 3),
    ("bounds_min", "f8", 3), ("bounds_max", "f8", 3),
    ("attribute_mask", "u4"),
    ("fingerprint", "u8"),
    ("rgb_max_bits", "u2"),        # 8 or 16 - drives the RGB stream dtype
    ("reserved", "u2"),
])

# v2 catalog row. `source_id` IS the unique file id (== row index) and
# `point_base` the global base: canonical point id = point_base + file-local
# index, so (file_id, index) <-> uint64 id is a bijection (source_catalog.py).
SOURCE_ENTRY = np.dtype([
    ("source_id", "u4"),
    ("path", "S512"),
    ("file_size", "u8"),
    ("mtime", "f8"),
    ("point_count", "u8"),
    ("point_base", "u8"),
    ("las_version", "u2"), ("point_format", "u2"),
    ("scale", "f8", 3), ("offset", "f8", 3),
    ("bounds_min", "f8", 3), ("bounds_max", "f8", 3),
    ("attribute_mask", "u4"),
    ("fingerprint", "u8"),
    ("rgb_max_bits", "u2"),
    ("reserved", "u2"),
    # ---- v2 catalog -----------------------------------------------------
    ("crs_hash", "u8"),             # FNV-1a of the normalised CRS WKT; 0 = none
    ("crs_epsg", "i4"),             # 0 = none / not an EPSG code
    ("xy_unit", "u1"), ("z_unit", "u1"),            # UNIT_* codes
    ("compressed", "u1"), ("crs_status", "u1"),     # LAZ?; 1 = CRS present
    ("xy_unit_factor", "f8"), ("z_unit_factor", "f8"),   # metres per unit
    ("global_encoding", "u2"), ("record_length", "u2"),
    ("header_size", "u2"), ("reserved2", "u2"),
    ("available_dims", "u4"),       # ATTR_* bits the SOURCE has (incl. GPS time)
    ("content_sig", "u8"),          # LAS header bytes + head/tail sample hash
    ("project_uuid", "u1", 16),     # LAS header GUID
])

NODE_ENTRY = np.dtype([
    ("node_id", "u4"),
    ("level", "u2"), ("quad", "u2"),
    ("bounds_min", "f8", 3), ("bounds_max", "f8", 3),
    ("point_count", "u8"),
    ("first_child", "i4", 4),
    ("lod_block", "i4", 8),       # block id per LOD, -1 when absent
    ("lod_point_count", "u8", 8),
    # Representative WORLD SPACING in metres for each LOD. Screen-space LOD
    # selection projects this into pixels and compares it against an error
    # target, which is far more robust than inferring LOD ratios from point
    # counts. Stored so the runtime never has to guess.
    ("lod_spacing", "f8", 8),
    ("state", "u4"),
])

# ------------------------------------------------------------------------
# .nakshapc block payload
# ------------------------------------------------------------------------
# Each block is self-describing: a fixed header, then SoA substreams whose
# (offset, bytes) are recorded in the header. A reader that only needs
# classification never touches the RGB bytes - that is the whole point of SoA
# (Part 5), and it is why attribute streams are separate rather than
# interleaved.
PC_BLOCK_HEADER = np.dtype([
    ("magic", "S16"), ("version", "u4"), ("codec", "u2"), ("lod", "u2"),
    ("block_id", "u4"), ("node_id", "u4"),
    ("point_count", "u8"),
    ("origin", "f8", 3),        # tile-local world origin (authoritative anchor)
    ("scale", "f8", 3),         # world metres per local integer step
    ("attribute_mask", "u4"),
    ("rgb_bits", "u2"), ("xyz_bits", "u2"),
    ("stream_count", "u2"), ("reserved", "u2"),
    ("payload_crc", "u4"), ("header_crc", "u4"),
])

# One entry per SoA stream, in a fixed order so no name table is needed.
PC_STREAM = np.dtype([
    ("attr", "u4"),
    ("offset", "u8"),          # relative to the end of the block header
    ("bytes", "u8"),
    ("encoding", "u2"), ("elem_bits", "u2"),
    ("codec", "u2"), ("reserved", "u2"),
])

# Order of streams in every block. Index == STREAM_INDEX_OF_ATTR.
STREAM_ORDER = (
    ATTR_XYZ, ATTR_CLASSIFICATION, ATTR_INTENSITY, ATTR_RGB,
    ATTR_RETURN_NUMBER, ATTR_NUMBER_OF_RETURNS, ATTR_SCAN_ANGLE,
    ATTR_POINT_SOURCE_ID, ATTR_GPS_TIME, ATTR_USER_DATA, ATTR_FLAGS,
    ATTR_SOURCE_ID,
)
STREAM_INDEX_OF_ATTR = {a: i for i, a in enumerate(STREAM_ORDER)}

# The directory entry that makes random block access possible (Part 4):
# read(node=1058, lod=2) seeks straight to file_offset and reads stored_bytes.
# No preceding block is parsed or decompressed.
BLOCK_ENTRY = np.dtype([
    ("block_id", "u4"),
    ("node_id", "u4"),
    ("lod", "u2"), ("codec", "u2"),
    ("file_offset", "u8"),
    ("stored_bytes", "u8"),
    ("uncompressed_bytes", "u8"),
    ("point_count", "u8"),
    ("attribute_mask", "u4"),
    ("stream_count", "u2"), ("reserved", "u2"),
    ("checksum", "u4"),
])

# Canonical stream order inside every block. A fixed order means no name table
# is needed and the reader can compute stream offsets from the directory alone.
STREAM_ORDER = (
    ATTR_XYZ, ATTR_CLASSIFICATION, ATTR_INTENSITY, ATTR_RGB,
    ATTR_RETURN_NUMBER, ATTR_NUMBER_OF_RETURNS, ATTR_SCAN_ANGLE,
    ATTR_POINT_SOURCE_ID, ATTR_GPS_TIME, ATTR_USER_DATA, ATTR_FLAGS,
    ATTR_SOURCE_ID,
)
STREAM_INDEX_OF_ATTR = {a: i for i, a in enumerate(STREAM_ORDER)}

# numpy dtype per attribute stream. RGB width follows the SOURCE format, which
# is the whole point of Part 6.
STREAM_DTYPE = {
    ATTR_XYZ: np.int32,               # 3 x int32 LOCAL coordinates
    ATTR_CLASSIFICATION: np.uint8,
    ATTR_INTENSITY: np.uint16,
    ATTR_RGB: None,                   # resolved from the block's rgb_bits
    ATTR_RETURN_NUMBER: np.uint8,
    ATTR_NUMBER_OF_RETURNS: np.uint8,
    ATTR_SCAN_ANGLE: np.int16,
    ATTR_POINT_SOURCE_ID: np.uint16,
    ATTR_GPS_TIME: np.float64,
    ATTR_USER_DATA: np.uint8,
    ATTR_FLAGS: np.uint8,
    ATTR_SOURCE_ID: np.uint64,        # stable global identity (Part 10)
}
# Attributes that are 3-component vectors.
STREAM_COMPONENTS = {ATTR_XYZ: 3, ATTR_RGB: 3}


def rgb_dtype(bits: int):
    return np.uint8 if bits == 8 else np.uint16


def stream_component_count(attr: int) -> int:
    return STREAM_COMPONENTS.get(attr, 1)


def stream_itemsize(attr: int, rgb_bits: int = 16) -> int:
    """Bytes per point for one stream, all components included."""
    comps = stream_component_count(attr)
    if attr == ATTR_RGB:
        return comps * (1 if rgb_bits == 8 else 2)
    return comps * np.dtype(STREAM_DTYPE[attr]).itemsize


# ==========================================================================
# .nakshaedit (Part 37)
# ==========================================================================
EDIT_HEADER = np.dtype([
    ("magic", "S16"), ("version", "u4"), ("endianness", "u4"),
    ("header_bytes", "u4"),
    ("dataset_uuid", "u1", 16),
    ("record_count", "u8"),
    ("revision", "u8"),
    ("created_unix", "u8"),
])

# One sparse classification override keyed by the STABLE source id.
EDIT_RECORD = np.dtype([
    ("source_point_id", "u8"),
    ("classification", "u1"), ("reserved", "u1", 7),
    ("revision", "u8"),
])


def new_dataset_uuid() -> bytes:
    return uuid.uuid4().bytes


def now_unix() -> int:
    return int(time.time())


def crc32_bytes(data: bytes) -> int:
    import zlib
    return zlib.crc32(data) & 0xFFFFFFFF


def crc32_of(arr: np.ndarray) -> int:
    return crc32_bytes(np.ascontiguousarray(arr).tobytes())


def source_fingerprint(path: str, point_count: int) -> int:
    """Cheap metadata fingerprint for stale-cache detection (Part 3).

    Deliberately NOT a content hash. NT219 is 16.39 GB across 14 files; hashing
    that on every launch would cost far more than opening the cache itself.
    Size + whole-second mtime + header point count catches every realistic
    edit, and the point count comes free with the header read.
    """
    st = os.stat(path)
    h = 1469598103934665603          # FNV-1a 64
    for v in (st.st_size, int(st.st_mtime), point_count):
        for b in int(v).to_bytes(8, "little"):
            h ^= b
            h = (h * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h

def rgb_bits_for(source_dtype) -> int:
    """8 or 16, taken from the SOURCE dtype - never guessed (Part 6).

    LAS 1.2 point format 3 (NT219) stores RGB as uint16. Casting that to uint8
    takes it modulo 256: 49920 becomes 0, and an entire real-colour cloud
    renders BLACK. The cache stores the source width and the reader widens only
    at display time.
    """
    return 8 if np.dtype(source_dtype).itemsize == 1 else 16


# ==========================================================================
# Morton / Z-order (Part 9)
#
# The earlier benchmark used int64 and overflowed SILENTLY at 32 bits/axis,
# producing garbage ordering and NaN diagnostics instead of an error. This
# implementation uses uint64 (so every shift up to 2*32 is defined), validates
# bits-per-axis up front, and casts to unsigned BEFORE shifting so numpy cannot
# wrap through a signed intermediate.
# ==========================================================================
MAX_MORTON_BITS = 32          # 2 * 32 = 64 bits, fits uint64 exactly


def morton_check_bits(bits: int):
    if not (1 <= bits <= MAX_MORTON_BITS):
        raise ValueError(
            f"morton bits per axis must be 1..{MAX_MORTON_BITS}, got {bits}")


def morton2d(x, y, x_min, span, y_min, yspan, bits: int = 21) -> np.ndarray:
    """Interleave X and Y into a uint64 Morton code.

    bits=21 per axis yields a 42-bit code over a 2^21 x 2^21 grid; at 4 km that
    resolves ~2 mm, finer than any point spacing, so the code never merges
    distinct points by truncation.
    """
    morton_check_bits(bits)
    side = (1 << bits) - 1
    span = span if span > 0 else 1.0
    yspan = yspan if yspan > 0 else 1.0
    ix = np.floor((np.asarray(x, dtype=np.float64) - x_min) / span * side)
    iy = np.floor((np.asarray(y, dtype=np.float64) - y_min) / yspan * side)
    # Clip BEFORE casting: a negative float to uint64 is undefined behaviour.
    ix = np.clip(ix, 0, side).astype(np.uint64)
    iy = np.clip(iy, 0, side).astype(np.uint64)
    code = np.zeros(ix.shape, dtype=np.uint64)
    # Interleave ONE BIT PER STEP. The previous loop shifted the whole
    # remaining value (`ix >> b` instead of `(ix >> b) & 1`) and OR-ed the
    # overlapping ranges together, so a 64x64 grid collapsed to 144 distinct
    # codes with 3952 collisions - cells far apart on the map produced the
    # same key. Anything keyed on that value (the PASS 1 shard partition, the
    # intra-leaf point order) lost its spatial meaning, which is why the
    # resulting leaves were not tiles: measured on the real 27M index, the
    # leaves summed to 30.4x the dataset area with 43-102 of them covering
    # every X slice.
    one = np.uint64(1)
    for b in range(bits):
        code |= ((ix >> np.uint64(b)) & one) << np.uint64(2 * b)
        code |= ((iy >> np.uint64(b)) & one) << np.uint64(2 * b + 1)
    return code


def morton_within_tile(x, y, bounds_min, bounds_max, bits: int = 16) -> np.ndarray:
    """Morton code local to a tile, for intra-tile ordering (Part 9)."""
    morton_check_bits(bits)
    return morton2d(x, y, float(bounds_min[0]),
                    float(bounds_max[0] - bounds_min[0]),
                    float(bounds_min[1]),
                    float(bounds_max[1] - bounds_min[1]), bits=bits)


def quantize_xyz(x, y, z, bounds_min, bounds_max):
    """int32 LOCAL coordinates plus the (origin, scale) that invert them.

        world = origin + local * scale

    The scale maps the tile extent exactly onto int32's range, so the
    reconstruction error is bounded by half a step and cannot silently exceed
    the source LAS quantisation (Part 8).
    """
    bmin = np.asarray(bounds_min, dtype=np.float64)
    bmax = np.asarray(bounds_max, dtype=np.float64)
    ext = np.maximum(bmax - bmin, 1e-12)
    scale = ext / (2.0 ** 31 - 1)
    local = np.empty((len(x), 3), dtype=np.int32)
    for k, arr in enumerate((x, y, z)):
        v = (np.asarray(arr, dtype=np.float64) - bmin[k]) / scale[k]
        v = np.clip(v, -2.0 ** 31 + 1, 2.0 ** 31 - 1)
        local[:, k] = np.rint(v).astype(np.int32)
    return local, bmin, scale


def dequantize_xyz(local: np.ndarray, origin, scale) -> np.ndarray:
    """Invert quantize_xyz back to world float64."""
    o = np.asarray(origin, dtype=np.float64)
    s = np.asarray(scale, dtype=np.float64)
    return np.asarray(local, dtype=np.float64) * s + o


def voxel_representative_ids(xyz, cell_size: float, class_ids=None):
    """Voxel representative sampling (Part 11). NOT points[::N].

    A stride samples in FILE order, so a small dense building can vanish
    completely at a coarse LOD while a dense ground swath is resampled over and
    over. One representative per occupied voxel ties coverage to occupied SPACE
    instead of file position.

    When class_ids is supplied (Part 12) the voxel key also mixes in the class,
    so two classes sharing a voxel each survive - rare but important classes
    (wires, poles) are not silently eliminated by a dominant neighbour.
    """
    xyz = np.asarray(xyz)
    ix = np.floor(xyz[:, 0] / cell_size).astype(np.int64)
    iy = np.floor(xyz[:, 1] / cell_size).astype(np.int64)
    iz = np.floor(xyz[:, 2] / cell_size).astype(np.int64)
    key = (ix * 73856093) ^ (iy * 19349663) ^ (iz * 83492791)
    if class_ids is not None:
        key = key ^ (np.asarray(class_ids, dtype=np.int64) * 2654435761)
    # Within a voxel prefer the HIGHEST z: that is the surface a viewer sees,
    # and it keeps building tops and wires present.
    order = np.lexsort((-xyz[:, 2], key))
    ks = key[order]
    first = np.ones(order.size, dtype=bool)
    first[1:] = ks[1:] != ks[:-1]
    return order[first]

# ==========================================================================
# Block codec (Part 4, Part 5, Part 24)
#
# A block is: [PC_BLOCK_HEADER][stream directory][stream payloads...]
#
# Each payload is a separate SoA substream. A reader needing only classification
# seeks to exactly that payload and never reads the RGB bytes - the property
# the SoA decision bought, preserved here rather than assumed.
# ==========================================================================


def encode_block_with_geometry(block_id, node_id, lod, streams, attribute_mask,
                               rgb_bits, origin, scale, codec=CODEC_NONE):
    """Serialise one block, with the tile geometry anchor in the header.

        world = origin + local * scale

    The XYZ stream holds int32 LOCAL values, so float64 world coordinates are
    never stored per point (Part 7).
    """
    n = len(next(iter(streams.values())))
    payloads = []
    for attr, arr in streams.items():
        idx = STREAM_INDEX_OF_ATTR[attr]
        a = np.ascontiguousarray(arr)
        if a.ndim == 1:
            a = a.reshape(-1, 1)
        payloads.append((idx, attr, a.tobytes()))
    payloads.sort(key=lambda kv: kv[0])

    body = bytearray()
    directory = np.zeros(len(STREAM_ORDER), dtype=PC_STREAM)
    # Stream offsets are relative to the START OF THE BLOCK, i.e. they include
    # the header and the directory themselves. The reader computes body_start
    # the same way, so the two must agree exactly. Making the writer's offsets
    # body-relative while the reader treats them as block-relative shifts every
    # stream by exactly PC_BLOCK_HEADER.itemsize bytes: the data is present and
    # plausible, but every value is misaligned, which is far harder to notice
    # than a hard failure.
    offset = PC_BLOCK_HEADER.itemsize + PC_STREAM.itemsize * len(STREAM_ORDER)
    for idx, attr, payload in payloads:
        directory[idx] = (attr, offset, len(payload), 0,
                          stream_component_count(attr) * 8, CODEC_NONE, 0)
        body += payload
        offset += len(payload)

    hdr = np.zeros(1, dtype=PC_BLOCK_HEADER)
    hdr["magic"] = PC_MAGIC
    hdr["version"] = PC_VERSION
    hdr["codec"] = codec
    hdr["lod"] = lod
    hdr["block_id"] = block_id
    hdr["node_id"] = node_id
    hdr["point_count"] = n
    hdr["origin"] = origin
    hdr["scale"] = scale
    hdr["attribute_mask"] = attribute_mask
    hdr["rgb_bits"] = rgb_bits
    hdr["xyz_bits"] = 32
    hdr["stream_count"] = len(payloads)
    hdr["payload_crc"] = crc32_bytes(bytes(body))

    head = hdr.copy()
    head["header_crc"] = crc32_bytes(hdr.tobytes())
    return head.tobytes() + directory.tobytes() + bytes(body)


class BlockWriter:
    """Append-only writer for a .nakshapc, recording the block directory.

    Append-only so a crash mid-build never corrupts an already-finalised block:
    a directory entry exists only after the block bytes are on disk, and the
    index is committed separately at the end (Part 21).
    """
    def __init__(self, path: str, append: bool = False, first_block_id: int = 0):
        # first_block_id makes block ids unique across the WHOLE file, not just
        # this writer session. Without it, a second writer appended to an
        # existing .nakshapc restarts numbering at 0 and produces TWO blocks with
        # the same id at different offsets - so a lookup finds the wrong one
        # and silently returns the wrong point set (Part 4 / Part 16).
        self.path = path
        exists = os.path.exists(path)
        self.fh = open(path, "r+b" if (append and exists) else "wb")
        if append and exists:
            self.fh.seek(0, os.SEEK_END)
        self.offset = self.fh.tell()
        self.next_id = int(first_block_id)
        self.blocks = []
    def add(self, block_id, node_id, lod, streams, attribute_mask, rgb_bits,
            origin, scale, codec=CODEC_NONE) -> int:
        raw = encode_block_with_geometry(block_id, node_id, lod, streams,
                                         attribute_mask, rgb_bits, origin,
                                         scale, codec)
        pos = self.offset
        self.fh.write(raw)
        self.offset += len(raw)
        b = np.zeros(1, dtype=BLOCK_ENTRY)
        b["block_id"] = block_id
        b["node_id"] = node_id
        b["lod"] = lod
        b["codec"] = codec
        b["file_offset"] = pos
        b["stored_bytes"] = len(raw)
        b["uncompressed_bytes"] = len(raw)
        b["point_count"] = len(next(iter(streams.values())))
        b["attribute_mask"] = attribute_mask
        b["stream_count"] = len(streams)
        b["checksum"] = crc32_bytes(raw)
        self.blocks.append(b[0])
        return pos

    def close(self):
        self.fh.flush()
        os.fsync(self.fh.fileno())
        self.fh.close()

    def directory(self) -> np.ndarray:
        if not self.blocks:
            return np.zeros(0, dtype=BLOCK_ENTRY)
        return np.array(self.blocks, dtype=BLOCK_ENTRY)

# ==========================================================================
# Block reader (Part 28)
# ==========================================================================


def read_block_header(fh, file_offset: int):
    fh.seek(file_offset)
    raw = fh.read(PC_BLOCK_HEADER.itemsize)
    if len(raw) < PC_BLOCK_HEADER.itemsize:
        raise ValueError("truncated block header")
    hdr = np.frombuffer(raw, dtype=PC_BLOCK_HEADER, count=1)[0]
    # Reject an unknown magic or version rather than guessing: a misread block
    # on a billion-point dataset is worse than a clean failure.
    magic = bytes(hdr["magic"])
    if bytes(hdr["magic"]).rstrip(b"\x00") != PC_MAGIC:
        raise ValueError(f"bad block magic {bytes(hdr['magic'])!r}")
    if int(hdr["version"]) != PC_VERSION:
        raise ValueError(f"unsupported block version {int(hdr['version'])}, "
                         f"expected {PC_VERSION}")
    return hdr


def read_block_directory(fh, file_offset: int):
    """Read just the stream directory: one small seek + read."""
    base = file_offset + PC_BLOCK_HEADER.itemsize
    fh.seek(base)
    raw = fh.read(PC_STREAM.itemsize * len(STREAM_ORDER))
    if len(raw) < PC_STREAM.itemsize * len(STREAM_ORDER):
        raise ValueError("truncated stream directory")
    return np.frombuffer(raw, dtype=PC_STREAM,
                         count=len(STREAM_ORDER)).copy()


def read_block(fh, entry, only_attrs=None, verify_crc=False):
    """Decode one block, optionally reading ONLY the requested streams.

    `only_attrs` is the SoA payoff made concrete: passing
    (ATTR_XYZ, ATTR_CLASSIFICATION) seeks to exactly those two payloads and
    never reads the RGB bytes. The returned `bytes_read` lets a test prove it.
    """
    off = int(entry["file_offset"])
    hdr = read_block_header(fh, off)
    directory = read_block_directory(fh, off)
    body_start = off   # stream offsets are relative to the block start

    want = None if only_attrs is None else set(only_attrs)
    out = {}
    bytes_read = 0
    for i in range(len(STREAM_ORDER)):
        s = directory[i]
        attr = int(s["attr"])
        if attr == 0 or int(s["bytes"]) == 0:
            continue
        if want is not None and attr not in want:
            continue          # skip this stream: its bytes are never read
        fh.seek(body_start + int(s["offset"]))
        raw = fh.read(int(s["bytes"]))
        bytes_read += len(raw)
        comps = stream_component_count(attr)
        if attr == ATTR_RGB:
            # np.dtype(dt).itemsize, NOT dt.itemsize: dt is a dtype CLASS here and a class attribute access returns an unbound descriptor, not a size.
            dt = np.dtype(rgb_dtype(int(hdr["rgb_bits"])))
            out[attr] = np.frombuffer(
                raw, dtype=dt, count=len(raw) // dt.itemsize).reshape(-1, comps)
        else:
            dt = np.dtype(STREAM_DTYPE[attr])
            arr = np.frombuffer(raw, dtype=dt, count=len(raw) // dt.itemsize)
            out[attr] = arr.reshape(-1, comps) if comps > 1 else arr

    if verify_crc:
        # The payload starts AFTER the header and directory. body_start is the
        # BLOCK start because stream offsets are block-relative, so reusing it
        # here would CRC the header and directory too - a different byte range
        # than the writer signed, which reports every block as corrupt.
        payload_start = off + PC_BLOCK_HEADER.itemsize + PC_STREAM.itemsize * len(STREAM_ORDER)
        fh.seek(payload_start)
        body = fh.read(int(entry["stored_bytes"]) - PC_BLOCK_HEADER.itemsize
                       - PC_STREAM.itemsize * len(STREAM_ORDER))
        if crc32_bytes(body) != int(hdr["payload_crc"]):
            raise ValueError(f"block {int(entry['block_id'])} payload CRC mismatch")

    return hdr, out, bytes_read


def block_world_xyz(hdr, local):
    """Reconstruct float64 world coordinates from the stored anchor (Part 7).

        world = origin + local * scale
    """
    return dequantize_xyz(local, hdr["origin"], hdr["scale"])
