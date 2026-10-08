"""source_catalog.py - the canonical multi-file source catalog (header-only).

A multi-file dataset is ONE virtual dataset made of N sources. Everything that
must stay true across files lives here, and nothing here reads a point body:

  * unique `file_id` per source (== row index == SOURCE_ENTRY["source_id"]),
  * a dense global point base, so a source point has ONE canonical uint64 id

        CanonicalPointID = point_base[file_id] + file_local_index
        (file_id, index) <-> id   is a bijection, `resolve_global_ids` inverts it

    (the legacy builder stored the file-LOCAL index, so id 0 existed once per
    file and edits / normals / selection of file B silently hit file A),
  * CRS / units / coordinate-magnitude validation BEFORE any file is combined:
    sources are never silently merged on an assumption of matching coordinates,
  * LAS encoding metadata and a cheap-but-sensitive content signature.

Validation failures raise `CatalogError` with a precise message.
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field

import numpy as np

from .format import (
    ATTR_CLASSIFICATION, ATTR_FLAGS, ATTR_GPS_TIME, ATTR_INTENSITY,
    ATTR_NUMBER_OF_RETURNS, ATTR_POINT_SOURCE_ID, ATTR_RETURN_NUMBER, ATTR_RGB,
    ATTR_SCAN_ANGLE, ATTR_USER_DATA, ATTR_XYZ, SOURCE_ENTRY, UNIT_DEGREE,
    UNIT_INTL_FOOT, UNIT_METRE, UNIT_OTHER, UNIT_UNKNOWN, UNIT_US_FOOT,
    rgb_bits_for, source_fingerprint,
)

MAX_POINT_FORMAT = 10
# Projected coordinates beyond this union span are not one coordinate system.
MAX_UNION_SPAN = 2.0e7


class CatalogError(ValueError):
    """The sources cannot be combined (or one is unusable)."""


@dataclass
class CatalogReport:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    overlaps: list = field(default_factory=list)      # (file_a, file_b, area_m2)
    crs_status: str = "UNKNOWN"                       # PRESENT | UNKNOWN
    crs_hash: int = 0
    crs_ref: str = ""
    total_points: int = 0
    bounds_min: tuple = (0.0, 0.0, 0.0)
    bounds_max: tuple = (0.0, 0.0, 0.0)
    rgb_all_sources: bool = False

    def summary(self) -> str:
        return (f"{self.total_points:,} pts, crs={self.crs_status}, "
                f"{len(self.warnings)} warning(s), "
                f"{len(self.overlaps)} overlapping pair(s)")


# --------------------------------------------------------------------------- #
# small helpers                                                                #
# --------------------------------------------------------------------------- #
def fnv64(data: bytes, h: int = 1469598103934665603) -> int:
    for b in data:
        h ^= b
        h = (h * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h


def _unit_code(name: str, is_angular: bool) -> int:
    n = (name or "").lower()
    if is_angular or "degree" in n:
        return UNIT_DEGREE
    if "us survey" in n or "us_survey" in n or "u.s." in n:
        return UNIT_US_FOOT
    if "foot" in n or "feet" in n:
        return UNIT_INTL_FOOT
    if "metre" in n or "meter" in n:
        return UNIT_METRE
    return UNIT_OTHER if n else UNIT_UNKNOWN


def _crs_info(header):
    """-> dict(hash, epsg, wkt, xy_unit, z_unit, xy_factor, z_factor, obj) | None

    Never raises: a CRS that cannot be parsed is reported as unknown, and the
    validator then decides whether that is acceptable.
    """
    try:
        crs = header.parse_crs()
    except Exception:
        crs = None
    if crs is None:
        return None
    try:
        epsg = crs.to_epsg() or 0
    except Exception:
        epsg = 0
    try:
        wkt = crs.to_wkt()
    except Exception:
        wkt = str(crs)
    key = f"EPSG:{epsg}" if epsg else wkt
    horiz, vert = crs, None
    try:
        if crs.is_compound and crs.sub_crs_list:
            horiz = crs.sub_crs_list[0]
            vert = crs.sub_crs_list[1] if len(crs.sub_crs_list) > 1 else None
    except Exception:
        pass
    xy_unit, xy_factor = UNIT_UNKNOWN, 0.0
    z_unit, z_factor = UNIT_UNKNOWN, 0.0
    try:
        ax = horiz.axis_info[0]
        ang = bool(getattr(horiz, "is_geographic", False))
        xy_unit = _unit_code(ax.unit_name, ang)
        xy_factor = float(ax.unit_conversion_factor)
    except Exception:
        pass
    try:
        src = vert if vert is not None else horiz
        axes = src.axis_info
        za = axes[-1] if vert is not None else (axes[2] if len(axes) > 2
                                                else None)
        if za is not None:
            z_unit = _unit_code(za.unit_name, False)
            z_factor = float(za.unit_conversion_factor)
    except Exception:
        pass
    return {"hash": fnv64(key.encode("utf-8")), "epsg": int(epsg), "wkt": wkt,
            "xy_unit": xy_unit, "z_unit": z_unit, "xy_factor": xy_factor,
            "z_factor": z_factor, "obj": crs}


def _crs_same(a, b) -> bool:
    if a["hash"] == b["hash"]:
        return True
    try:
        return bool(a["obj"].equals(b["obj"], ignore_axis_order=True))
    except Exception:
        return False


def _raw_header(path):
    with open(path, "rb") as fh:
        raw = fh.read(375)
        size = os.path.getsize(path)
        if len(raw) < 227 or raw[:4] != b"LASF":
            raise CatalogError(f"{path}: not a LAS/LAZ file (bad signature)")
        hsize = struct.unpack_from("<H", raw, 94)[0]
        off = struct.unpack_from("<I", raw, 96)[0]
        head = b""
        tail = b""
        if size > off + 4096:
            fh.seek(off)
            head = fh.read(4096)
            fh.seek(max(size - 4096, 0))
            tail = fh.read(4096)
    return raw, hsize, off, size, head, tail


def _dims_mask(dims) -> int:
    m = ATTR_XYZ
    for name, bit in (("classification", ATTR_CLASSIFICATION),
                      ("intensity", ATTR_INTENSITY),
                      ("return_number", ATTR_RETURN_NUMBER),
                      ("number_of_returns", ATTR_NUMBER_OF_RETURNS),
                      ("scan_angle_rank", ATTR_SCAN_ANGLE),
                      ("scan_angle", ATTR_SCAN_ANGLE),
                      ("user_data", ATTR_USER_DATA),
                      ("point_source_id", ATTR_POINT_SOURCE_ID),
                      ("gps_time", ATTR_GPS_TIME),
                      ("red", ATTR_RGB),
                      ("edge_of_flight_line", ATTR_FLAGS)):
        if name in dims:
            m |= bit
    return m


def _geographic_like(mn, mx) -> bool:
    return (max(abs(mn[0]), abs(mx[0])) <= 360.0
            and max(abs(mn[1]), abs(mx[1])) <= 90.0
            and (mx[0] - mn[0]) <= 360.0)


# --------------------------------------------------------------------------- #
# build                                                                        #
# --------------------------------------------------------------------------- #
def build_catalog(paths, *, allow_unknown_crs: bool = True):
    """Header-only catalog for `paths`. Returns (SOURCE_ENTRY[n], CatalogReport).

    Raises `CatalogError` when the sources cannot form one dataset.
    `allow_unknown_crs=True` lets a dataset whose files ALL lack a CRS through
    (flagged UNKNOWN in the report); a MIX of files with and without a CRS is
    always rejected - that is exactly the "silently assume matching" case.
    """
    import laspy

    rep = CatalogReport()
    paths = [os.fspath(p) for p in paths]
    if not paths:
        raise CatalogError("no source files given")
    seen = {}
    for p in paths:
        if not os.path.isfile(p):
            raise CatalogError(f"source file missing: {p}")
        key = os.path.normcase(os.path.abspath(p))
        if key in seen:
            raise CatalogError(f"source listed twice: {p}")
        seen[key] = p

    rows = np.zeros(len(paths), dtype=SOURCE_ENTRY)
    crs = []
    base = 0
    rgb_flags = []
    for fid, p in enumerate(paths):
        try:
            raw, hsize, _off, size, head, tail = _raw_header(p)
            with laspy.open(p) as f:
                h = f.header
                n = int(h.point_count)
                mn = np.asarray(h.mins, float)
                mx = np.asarray(h.maxs, float)
                dims = {str(d.name).lower() for d in h.point_format.dimensions}
                pf = int(h.point_format.id)
                rgb_bits = 16
                if "red" in dims and n > 0:
                    rgb_bits = rgb_bits_for(np.asarray(f.read_points(1).red).dtype)
                info = _crs_info(h)
                ver = int(h.version.major) * 10 + int(h.version.minor)
                scales = [float(v) for v in h.scales]
                offsets = [float(v) for v in h.offsets]
        except CatalogError:
            raise
        except Exception as exc:                                  # noqa: BLE001
            raise CatalogError(f"{p}: unreadable LAS/LAZ header "
                               f"({type(exc).__name__}: {exc})") from exc
        if n <= 0:
            raise CatalogError(f"{p}: contains no points")
        if pf > MAX_POINT_FORMAT:
            raise CatalogError(f"{p}: unsupported point format {pf}")
        if not (np.isfinite(mn).all() and np.isfinite(mx).all()) \
                or (mx < mn).any():
            raise CatalogError(f"{p}: header bounds are not finite/ordered "
                               f"(min={mn.tolist()} max={mx.tolist()})")
        r = rows[fid]
        r["source_id"] = fid
        r["path"] = os.path.abspath(p).encode("utf-8", "replace")
        r["file_size"] = size
        r["mtime"] = os.path.getmtime(p)
        r["point_count"] = n
        r["point_base"] = base
        r["las_version"] = ver
        r["point_format"] = pf
        r["scale"] = scales
        r["offset"] = offsets
        r["bounds_min"] = mn
        r["bounds_max"] = mx
        am = _dims_mask(dims)
        r["available_dims"] = am
        # The STREAM mask the builder has always used (what blocks carry); the
        # richer `available_dims` above says what the SOURCE has.
        r["attribute_mask"] = (ATTR_XYZ | ATTR_CLASSIFICATION | ATTR_INTENSITY
                               | ATTR_RETURN_NUMBER | ATTR_POINT_SOURCE_ID
                               | (ATTR_RGB if "red" in dims else 0))
        r["fingerprint"] = source_fingerprint(p, n)
        r["rgb_max_bits"] = rgb_bits
        r["compressed"] = 1 if (raw[104] & 0x80) else 0
        r["global_encoding"] = struct.unpack_from("<H", raw, 6)[0]
        r["record_length"] = struct.unpack_from("<H", raw, 105)[0]
        r["header_size"] = hsize
        r["project_uuid"] = np.frombuffer(raw[8:24], np.uint8)
        r["content_sig"] = fnv64(head + tail, fnv64(raw[:hsize or 375]))
        if info is not None:
            r["crs_hash"] = info["hash"]
            r["crs_epsg"] = info["epsg"]
            r["xy_unit"] = info["xy_unit"]
            r["z_unit"] = info["z_unit"]
            r["xy_unit_factor"] = info["xy_factor"]
            r["z_unit_factor"] = info["z_factor"]
            r["crs_status"] = 1
        crs.append(info)
        rgb_flags.append("red" in dims)
        base += n

    rep.total_points = base
    rep.bounds_min = tuple(rows["bounds_min"].min(axis=0))
    rep.bounds_max = tuple(rows["bounds_max"].max(axis=0))
    rep.rgb_all_sources = all(rgb_flags)
    _validate(paths, rows, crs, rep, allow_unknown_crs)
    if rep.errors:
        raise CatalogError("; ".join(rep.errors))
    return rows, rep


def _validate(paths, rows, crs, rep, allow_unknown_crs):
    names = [os.path.basename(p) for p in paths]
    have = [c is not None for c in crs]
    if any(have) and not all(have):
        miss = [names[i] for i, h in enumerate(have) if not h]
        pres = [names[i] for i, h in enumerate(have) if h]
        rep.errors.append(
            f"CRS present in {pres} but missing in {miss}; refusing to assume "
            f"matching coordinates")
    elif not any(have):
        rep.crs_status = "UNKNOWN"
        if not allow_unknown_crs:
            rep.errors.append("no source declares a CRS")
        else:
            rep.warnings.append("no source declares a CRS; coordinates are "
                                "assumed to share one system (UNKNOWN)")
    else:
        rep.crs_status = "PRESENT"
        ref = crs[0]
        for i in range(1, len(crs)):
            if not _crs_same(ref, crs[i]):
                rep.errors.append(
                    f"CRS mismatch: {names[0]} is EPSG:{ref['epsg'] or '?'} "
                    f"but {names[i]} is EPSG:{crs[i]['epsg'] or '?'}; transform "
                    f"to one CRS first or load them separately")
            else:
                a, b = ref, crs[i]
                for k, label in (("xy_factor", "horizontal"),
                                 ("z_factor", "vertical")):
                    fa, fb = a[k], b[k]
                    if fa > 0 and fb > 0 and abs(fa - fb) > 1e-9 * max(fa, fb):
                        rep.errors.append(
                            f"{label} unit mismatch: {names[0]} {fa} vs "
                            f"{names[i]} {fb} (metres per unit)")
        rep.crs_hash = int(ref["hash"])
        # A SHORT, VALID reference - never a truncated WKT (a 255-char cut of a
        # WKT is not parseable and would be worse than empty). The full CRS is
        # recoverable from the sources; equality is carried by `crs_hash`.
        rep.crs_ref = f"EPSG:{ref['epsg']}" if ref["epsg"] else ""
    # ---- coordinate magnitude -------------------------------------------
    geo = [_geographic_like(rows["bounds_min"][i], rows["bounds_max"][i])
           for i in range(len(rows))]
    if any(geo) and not all(geo):
        rep.errors.append(
            "coordinate magnitude mismatch: "
            f"{[names[i] for i, g in enumerate(geo) if g]} look like degrees, "
            f"{[names[i] for i, g in enumerate(geo) if not g]} look projected")
    span = np.asarray(rep.bounds_max[:2]) - np.asarray(rep.bounds_min[:2])
    if not all(geo) and (span > MAX_UNION_SPAN).any():
        rep.errors.append(
            f"combined extent {span.tolist()} m exceeds {MAX_UNION_SPAN:g}: "
            f"the sources are not in one projected coordinate system")
    # ---- informational ---------------------------------------------------
    for i in range(len(rows)):
        if float(rows["scale"][i].max()) > 1.0:
            rep.warnings.append(f"{names[i]}: coarse scale "
                                f"{rows['scale'][i].tolist()}")
    sigs = rows["content_sig"]
    if np.unique(sigs).size != sigs.size:
        rep.warnings.append("two sources have an identical content signature "
                            "(duplicate files?)")
    # Overlap is DIAGNOSTIC only: source files are authoritative and nothing is
    # ever de-duplicated (Part 57).
    bmn, bmx = rows["bounds_min"], rows["bounds_max"]
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            ox = min(bmx[i][0], bmx[j][0]) - max(bmn[i][0], bmn[j][0])
            oy = min(bmx[i][1], bmx[j][1]) - max(bmn[i][1], bmn[j][1])
            if ox > 0 and oy > 0:
                rep.overlaps.append((i, j, float(ox * oy)))


# --------------------------------------------------------------------------- #
# canonical point identity                                                     #
# --------------------------------------------------------------------------- #
def verify_dense_bases(sources) -> bool:
    """point_base[i+1] == point_base[i] + point_count[i], starting at 0."""
    if sources.size == 0:
        return True
    b = sources["point_base"].astype(np.uint64)
    c = sources["point_count"].astype(np.uint64)
    exp = np.concatenate(([np.uint64(0)], np.cumsum(c)[:-1].astype(np.uint64)))
    return bool(np.array_equal(b, exp))


def global_point_ids(sources, file_id: int, local_index) -> np.ndarray:
    """uint64 canonical ids for `local_index` of file `file_id`."""
    return (np.uint64(int(sources["point_base"][file_id]))
            + np.asarray(local_index, dtype=np.uint64))


def resolve_global_ids(sources, gids):
    """-> (file_ids int64, local_index int64). Raises on an id out of range."""
    g = np.asarray(gids, dtype=np.uint64)
    base = sources["point_base"].astype(np.uint64)
    total = int(sources["point_count"].astype(np.uint64).sum())
    if g.size and int(g.max()) >= total:
        raise ValueError(f"canonical point id {int(g.max())} >= total points "
                         f"{total}")
    fid = np.searchsorted(base, g, side="right").astype(np.int64) - 1
    local = (g - base[fid]).astype(np.int64)
    return fid, local
