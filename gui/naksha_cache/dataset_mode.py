"""DatasetMode: the single authoritative owner of how a dataset is rendered.

Stage 3C contract (spec: "Authoritative Dataset Mode"):
    EMPTY      - no dataset loaded
    IN_MEMORY  - legacy path: FileLoaderWorker decodes the WHOLE source into a
                 single app.data[...] numpy block, then rb.upload_point_cloud()
                 pushes the full cloud to the GPU in one monolithic call.
    STREAMING  - cache-first: a committed .nakshaidx + .nakshapc is opened by
                 NakshaPointCacheReader; a StreamManager serves tiles on demand.
                 The source .laz point BODY is never decoded for rendering.

Helpers: is_in_memory_dataset(app), is_streaming_dataset(app).

cache_first_open(): validates NKIDX+NKPC presence, magic/version/FINALIZED, and
that the source still matches the fingerprint stored at build time -- via a
HEADER-ONLY LAZ probe that reads ZERO point-body bytes.
"""
from __future__ import annotations
import os
from dataclasses import dataclass
from enum import Enum


class DatasetMode(str, Enum):
    EMPTY = "empty"
    IN_MEMORY = "in_memory"
    STREAMING = "streaming"


def is_in_memory_dataset(app) -> bool:
    return getattr(app, "dataset_mode", None) == DatasetMode.IN_MEMORY


def is_streaming_dataset(app) -> bool:
    return getattr(app, "dataset_mode", None) == DatasetMode.STREAMING


@dataclass
class CacheValidity:
    ok: bool
    reason: str = ""
    source_path: str = ""
    index_path: str = ""
    pc_path: str = ""
    edit_path: str = ""
    total_points: int = 0
    lod_count: int = 0
    overview_block_id: int = -1
    bounds_min: tuple = (0.0, 0.0, 0.0)
    bounds_max: tuple = (0.0, 0.0, 0.0)
    crs_wkt: str = ""
    pc_bytes: int = 0
    ram_bytes: int = 0
    source_point_count: int = 0
    source_file_size: int = 0
    source_fingerprint: int = 0


def _source_probe(path: str):
    """Header-only LAZ probe. NEVER decodes the point body.

    Uses laspy.open() which opens the HEADER READER ONLY; the compressed
    point body is never decompressed. This is the explicit instrumentation
    that proves the source is touched only for fingerprint/stat.
    Returns (point_count, bounds_min, bounds_max, file_size, mtime, fingerprint).
    """
    sz = os.path.getsize(path)
    mt = int(os.path.getmtime(path))
    import laspy
    from gui.naksha_cache.format import source_fingerprint
    with laspy.open(path) as f:
        h = f.header
        pc = int(h.point_count)
        bmin = tuple(float(v) for v in h.mins)
        bmax = tuple(float(v) for v in h.maxs)
        fp = int(source_fingerprint(path, pc))
    return pc, bmin, bmax, sz, mt, fp


def _to_int(v):
    """Coerce a table field to int: int, or raw bytes via little-endian decode."""
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        pass
    if hasattr(v, "tobytes"):
        try:
            return int.from_bytes(v.tobytes(), "little")
        except Exception:
            return None
    if isinstance(v, (bytes, bytearray)):
        try:
            return int.from_bytes(v, "little")
        except Exception:
            return None
    return None


def _idx_source_row(idx):
    """Pull the (single) source row out of the mapped source table tolerantly."""
    src = getattr(idx, "sources", None)
    if src is None or src.size == 0:
        return {}
    row = src[0]
    names = row.dtype.names
    def _get(name, default=None):
        if name in names:
            v = row[name]
            if hasattr(v, "tobytes") and v.dtype.kind in ("S", "U", "O"):
                try:
                    return v.tobytes().rstrip(b"\x00").decode("utf-8", "replace")
                except Exception:
                    return str(v)
            try:
                return int(v)
            except Exception:
                return v
        return default
    return {
        "point_count": _to_int(row["point_count"]) if "point_count" in names else None,
        "bounds_min": _get("bounds_min"),
        "bounds_max": _get("bounds_max"),
        "file_size": _to_int(row["file_size"]) if "file_size" in names else
                   (_to_int(row["size"]) if "size" in names else None),
        "fingerprint": _to_int(row["fingerprint"]) if "fingerprint" in names else None,
        "path": _get("path"),
    }


def cache_first_open(source_path: str) -> CacheValidity:
    """Validate that a committed NAKSHA cache exists for ``source_path``.

    Returns CacheValidity(ok=True) only when the cache is present, FINALIZED,
    and the source matches the fingerprint stored at build time (header-only
    probe -- zero point-body bytes read).
    """
    from gui.naksha_cache.index import project_paths, IndexReader
    from gui.naksha_cache.format import BUILD_FINALIZED
    idx_path, pc_path, edit_path, _ = project_paths(source_path)
    bad = CacheValidity(False, "", source_path=source_path, index_path=idx_path,
                       pc_path=pc_path, edit_path=edit_path)
    if not os.path.isfile(source_path):
        return CacheValidity(False, f"source LAZ not found: {source_path}",
                             source_path=source_path)
    if not os.path.isfile(idx_path):
        return CacheValidity(False, "no index; cache not built",
                             source_path=source_path, index_path=idx_path,
                             pc_path=pc_path, edit_path=edit_path)
    idx = IndexReader(idx_path)
    from gui.naksha_cache.index import resolve_point_cache
    pc_path = resolve_point_cache(idx_path, idx.point_cache_name, pc_path)
    if not os.path.isfile(pc_path):
        idx.close()
        return CacheValidity(False, "index present but point cache missing",
                             source_path=source_path, index_path=idx_path,
                             pc_path=pc_path, edit_path=edit_path)
    hdr = idx.header
    magic = bytes(hdr["magic"]).rstrip(b"\x00")
    if magic != b"NKIDX001":
        return CacheValidity(False, f"bad index magic {magic!r}")
    # IndexReader already refused unknown versions / incompatible identity; this
    # only guards a reader built from a different format module.
    from gui.naksha_cache.format import IDX_VERSIONS_READABLE
    if int(hdr["version"]) not in IDX_VERSIONS_READABLE:
        return CacheValidity(False, f"unsupported index version {int(hdr['version'])}")
    if int(hdr["build_state"]) != BUILD_FINALIZED:
        return CacheValidity(False,
                             f"build_state={int(hdr['build_state'])} != FINALIZED(4)")
    pc_bytes = os.path.getsize(pc_path)
    total = int(hdr["source_point_count"])
    hmin = tuple(float(v) for v in hdr["bounds_min"])
    hmax = tuple(float(v) for v in hdr["bounds_max"])
    crs = bytes(hdr["crs_wkt"]).rstrip(b"\x00").decode("utf-8", "replace")
    # ---- stale-cache gate: header-only source probe (no point-body decode) ----
    try:
        pc, bmin, bmax, sz, mt, fp = _source_probe(source_path)
    except Exception as e:
        return CacheValidity(False, f"source probe failed: {e}")
    src = _idx_source_row(idx)
    stored_fp = src.get("fingerprint")
    if stored_fp is not None and int(stored_fp) != fp:
        return CacheValidity(False,
                             f"source fingerprint changed (stale cache); "
                             f"stored={stored_fp} current={fp}")
    stored_sz = src.get("file_size")
    if stored_sz is not None and int(stored_sz) != sz:
        return CacheValidity(False,
                             f"source file size changed (stale cache); "
                             f"stored={stored_sz} current={sz}")
    if total != pc:
        return CacheValidity(False,
                             f"source point count changed; index={total} current={pc}")
    if hmin != bmin or hmax != bmax:
        return CacheValidity(False,
                             f"source bounds changed (stale cache); index_min={hmin} src_min={bmin}")
    # cache the reader onto the validity result so open_file can reuse it
    bad_ok = CacheValidity(
        ok=True, reason="cache valid; source fingerprint match (header-only probe)",
        source_path=source_path, index_path=idx_path, pc_path=pc_path, edit_path=edit_path,
        total_points=total, lod_count=int(hdr["lod_count"]),
        overview_block_id=int(hdr["overview_block_id"]),
        bounds_min=hmin, bounds_max=hmax, crs_wkt=crs,
        pc_bytes=pc_bytes, ram_bytes=pc_bytes,
        source_point_count=pc, source_file_size=sz, source_fingerprint=fp)
    return bad_ok
