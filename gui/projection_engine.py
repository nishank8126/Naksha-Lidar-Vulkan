"""Naksha unified projection engine.

This module is the single projection/coordinate-operation core used by LiDAR,
SNT/CAD and GIS adapters.  It intentionally has **no Qt dependency** so it can
be used by file-loader workers and tests.

Design rules
------------
* One project/display CRS (``app.canvas_crs``) describes Naksha world XY.
* Every source keeps its own CRS metadata.  Geometry is transformed *for the
  project*, not silently re-labelled.
* Traditional GIS axis order is used consistently: X/Y = lon/lat for geographic
  CRS and easting/northing for projected CRS (``always_xy=True``).
* PROJ's database (``proj.db``) is the catalogue.  Nothing is hard-coded to one
  country, UTM zone or datum.
* Coordinate operations are chosen with PROJ/pyproj's late-binding engine.
  Area-of-interest and grid availability are surfaced in diagnostics.
* Z is transformed only through the coordinate operation selected by PROJ.
  If no vertical operation exists, PROJ normally passes Z through; Naksha never
  invents a vertical datum shift.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from functools import lru_cache
import json
import math
import os
import threading
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import numpy as np
from pyproj import CRS
from pyproj.aoi import AreaOfInterest
from pyproj.enums import PJType
from pyproj.transformer import Transformer, TransformerGroup

try:
    from pyproj import database as _proj_db
except Exception:  # pragma: no cover - old pyproj fallback
    _proj_db = None


@dataclass(frozen=True)
class CRSRecord:
    authority: str
    code: str
    name: str
    crs_type: str
    deprecated: bool = False
    projection_method: Optional[str] = None
    west: Optional[float] = None
    south: Optional[float] = None
    east: Optional[float] = None
    north: Optional[float] = None

    @property
    def auth_code(self) -> str:
        return f"{self.authority}:{self.code}"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TransformReport:
    source: str
    target: str
    operation: Optional[str] = None
    accuracy_m: Optional[float] = None
    best_available: bool = True
    ballpark: bool = False
    missing_grids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


def parse_crs(value: Any) -> Optional[CRS]:
    """Return a pyproj.CRS from any supported user/database representation."""
    if value is None or value == "":
        return None
    if isinstance(value, CRS):
        return value
    if isinstance(value, (int, np.integer)):
        return CRS.from_epsg(int(value))
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return CRS.from_epsg(int(text))
    return CRS.from_user_input(text)


def crs_authority(crs: Any) -> tuple[Optional[str], Optional[str]]:
    obj = parse_crs(crs)
    if obj is None:
        return None, None
    try:
        auth = obj.to_authority(min_confidence=25)
    except TypeError:
        auth = obj.to_authority()
    except Exception:
        auth = None
    if auth:
        return str(auth[0]), str(auth[1])
    try:
        code = obj.to_epsg(min_confidence=25)
        if code:
            return "EPSG", str(code)
    except Exception:
        pass
    return None, None


def crs_identifier(crs: Any) -> str:
    obj = parse_crs(crs)
    if obj is None:
        return "UNRESOLVED"
    auth, code = crs_authority(obj)
    if auth and code:
        return f"{auth}:{code}"
    return obj.name or "Custom CRS"


def crs_kind(crs: Any) -> str:
    obj = parse_crs(crs)
    if obj is None:
        return "Unknown"
    if getattr(obj, "is_compound", False):
        return "Compound"
    if getattr(obj, "is_vertical", False):
        return "Vertical"
    if getattr(obj, "is_projected", False):
        return "Projected"
    if getattr(obj, "is_geographic", False):
        return "Geographic"
    if getattr(obj, "is_engineering", False):
        return "Engineering"
    if getattr(obj, "is_geocentric", False):
        return "Geocentric"
    return str(getattr(obj, "type_name", "Other") or "Other")


def describe_crs(crs: Any) -> dict:
    obj = parse_crs(crs)
    if obj is None:
        return {
            "name": "Unresolved",
            "identifier": "UNRESOLVED",
            "type": "Unknown",
        }
    auth, code = crs_authority(obj)
    area = getattr(obj, "area_of_use", None)
    axis = []
    try:
        for info in obj.axis_info:
            axis.append({
                "name": info.name,
                "abbrev": info.abbrev,
                "direction": info.direction,
                "unit": info.unit_name,
                "unit_conversion_factor": info.unit_conversion_factor,
            })
    except Exception:
        pass
    operation = None
    try:
        op = obj.coordinate_operation
        if op is not None:
            operation = {
                "name": op.name,
                "method": getattr(op, "method_name", None),
                "accuracy": getattr(op, "accuracy", None),
            }
    except Exception:
        pass
    datum_name = None
    try:
        datum_name = obj.datum.name if obj.datum is not None else None
    except Exception:
        pass
    sub_crs = []
    try:
        for sub in obj.sub_crs_list or []:
            sub_crs.append({
                "name": sub.name,
                "identifier": crs_identifier(sub),
                "type": crs_kind(sub),
            })
    except Exception:
        pass
    return {
        "name": obj.name,
        "authority": auth,
        "code": code,
        "identifier": f"{auth}:{code}" if auth and code else "Custom",
        "type": crs_kind(obj),
        "datum": datum_name,
        "axis": axis,
        "area": {
            "name": getattr(area, "name", None),
            "west": getattr(area, "west", None),
            "south": getattr(area, "south", None),
            "east": getattr(area, "east", None),
            "north": getattr(area, "north", None),
        } if area is not None else None,
        "coordinate_operation": operation,
        "sub_crs": sub_crs,
        "wkt": obj.to_wkt(),
    }


def _pj_types_for_category(category: Optional[str]):
    category = str(category or "all").strip().lower()
    if category in {"projected", "projection", "pcs"}:
        return [PJType.PROJECTED_CRS]
    if category in {"geographic", "gcs"}:
        return [PJType.GEOGRAPHIC_2D_CRS, PJType.GEOGRAPHIC_3D_CRS]
    if category in {"vertical", "vcs"}:
        return [PJType.VERTICAL_CRS]
    if category in {"compound"}:
        return [PJType.COMPOUND_CRS]
    if category in {"engineering", "local"}:
        return [PJType.ENGINEERING_CRS]
    if category in {"geocentric"}:
        return [PJType.GEOCENTRIC_CRS]
    return None


@lru_cache(maxsize=16)
def _catalog_rows_cached(category: str, authority: str, allow_deprecated: bool):
    if _proj_db is None:
        return tuple()
    pj_types = _pj_types_for_category(category)
    auth = authority or None
    try:
        infos = _proj_db.query_crs_info(
            auth_name=auth,
            pj_types=pj_types,
            allow_deprecated=bool(allow_deprecated),
        )
    except Exception:
        return tuple()
    rows = []
    for info in infos:
        area = getattr(info, "area_of_use", None)
        rows.append(CRSRecord(
            authority=str(info.auth_name),
            code=str(info.code),
            name=str(info.name),
            crs_type=str(getattr(info.type, "name", info.type)),
            deprecated=bool(info.deprecated),
            projection_method=getattr(info, "projection_method_name", None),
            west=getattr(area, "west", None),
            south=getattr(area, "south", None),
            east=getattr(area, "east", None),
            north=getattr(area, "north", None),
        ))
    return tuple(rows)


def _record_intersects_aoi(row: CRSRecord, aoi: Optional[AreaOfInterest], contains: bool) -> bool:
    if aoi is None:
        return True
    if None in (row.west, row.south, row.east, row.north):
        return False
    if contains:
        return (
            row.west <= aoi.west_lon_degree and
            row.east >= aoi.east_lon_degree and
            row.south <= aoi.south_lat_degree and
            row.north >= aoi.north_lat_degree
        )
    return not (
        row.east < aoi.west_lon_degree or
        row.west > aoi.east_lon_degree or
        row.north < aoi.south_lat_degree or
        row.south > aoi.north_lat_degree
    )


def search_crs(
    query: str = "",
    *,
    category: str = "all",
    authority: Optional[str] = None,
    area_of_interest: Optional[AreaOfInterest] = None,
    contains: bool = False,
    allow_deprecated: bool = False,
    limit: int = 1500,
) -> list[CRSRecord]:
    """Search the installed PROJ CRS catalogue.

    Matching is deliberately tolerant: authority code, complete name,
    projection method and individual query tokens are considered.
    """
    q = str(query or "").strip().casefold()
    rows = list(_catalog_rows_cached(
        str(category or "all").lower(),
        str(authority or ""),
        bool(allow_deprecated),
    ))

    # UTM is a user-facing category, even though PROJ stores it as projected CRS.
    if str(category).lower() == "utm":
        rows = [r for r in _catalog_rows_cached("projected", str(authority or "EPSG"), bool(allow_deprecated))
                if "utm" in r.name.casefold() or "utm" in str(r.projection_method or "").casefold()]

    if area_of_interest is not None:
        rows = [r for r in rows if _record_intersects_aoi(r, area_of_interest, contains)]

    if q:
        tokens = [t for t in q.replace("/", " ").replace("_", " ").split() if t]
        def score(row: CRSRecord):
            ident = row.auth_code.casefold()
            name = row.name.casefold()
            method = str(row.projection_method or "").casefold()
            hay = f"{ident} {name} {method}"
            if q == ident or q == row.code.casefold():
                return (0, len(name))
            if name == q:
                return (1, len(name))
            if name.startswith(q):
                return (2, len(name))
            if q in name:
                return (3, len(name))
            if all(t in hay for t in tokens):
                return (4, len(name))
            return None
        ranked = []
        for row in rows:
            s = score(row)
            if s is not None:
                ranked.append((s, row))
        ranked.sort(key=lambda item: (item[0], item[1].authority, item[1].code))
        rows = [r for _, r in ranked]
    else:
        rows.sort(key=lambda r: (r.name.casefold(), r.authority, r.code))

    return rows[: max(1, int(limit))]


def suggest_utm_crs(lon: float, lat: float, datum_name: str = "WGS 84") -> list[CRSRecord]:
    """Return UTM CRSs suitable for a WGS84 lon/lat location."""
    if _proj_db is None:
        return []
    lon = float(lon)
    lat = float(lat)
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return []
    # A tiny AOI is better than hard-coding EPSG:326xx/327xx, and also works
    # for non-WGS84 datums when requested.
    aoi = AreaOfInterest(lon - 1e-8, lat - 1e-8, lon + 1e-8, lat + 1e-8)
    try:
        infos = _proj_db.query_utm_crs_info(datum_name=datum_name, area_of_interest=aoi)
    except Exception:
        return []
    out = []
    for info in infos:
        area = getattr(info, "area_of_use", None)
        out.append(CRSRecord(
            authority=str(info.auth_name), code=str(info.code), name=str(info.name),
            crs_type=str(getattr(info.type, "name", info.type)),
            deprecated=bool(info.deprecated),
            projection_method=getattr(info, "projection_method_name", None),
            west=getattr(area, "west", None), south=getattr(area, "south", None),
            east=getattr(area, "east", None), north=getattr(area, "north", None),
        ))
    return out


def _collect_missing_grids(group: TransformerGroup) -> tuple[str, ...]:
    names = []
    try:
        for op in group.unavailable_operations:
            for grid in getattr(op, "grids", []) or []:
                name = getattr(grid, "short_name", None) or getattr(grid, "full_name", None) or getattr(grid, "url", None)
                if name and name not in names:
                    names.append(str(name))
    except Exception:
        pass
    return tuple(names)


_TRANSFORMER_CACHE_LIMIT = 64
_TRANSFORMER_CACHE_STATE = threading.local()


def _transformer_cache() -> dict:
    """Per-thread transformer cache.

    ``TransformerGroup`` operation selection is expensive (several milliseconds
    per call, mostly PROJ database work) and callers routinely re-resolve the
    same CRS pair for every feature in a dataset.  Caching is per thread because
    a pyproj ``Transformer``/PROJ context must not be shared across threads.
    """
    cache = getattr(_TRANSFORMER_CACHE_STATE, "cache", None)
    if cache is None:
        cache = {}
        _TRANSFORMER_CACHE_STATE.cache = cache
    return cache


def _area_of_interest_key(area_of_interest: Optional[AreaOfInterest]):
    if area_of_interest is None:
        return None
    try:
        return (
            round(float(area_of_interest.west), 9),
            round(float(area_of_interest.south), 9),
            round(float(area_of_interest.east), 9),
            round(float(area_of_interest.north), 9),
        )
    except Exception:
        return repr(area_of_interest)


def _transformer_cache_key(src: CRS, dst: CRS,
                           area_of_interest: Optional[AreaOfInterest],
                           allow_ballpark: bool,
                           accuracy: Optional[float]):
    try:
        src_key = src.to_wkt()
    except Exception:
        src_key = str(src)
    try:
        dst_key = dst.to_wkt()
    except Exception:
        dst_key = str(dst)
    return (
        src_key,
        dst_key,
        _area_of_interest_key(area_of_interest),
        bool(allow_ballpark),
        None if accuracy is None else float(accuracy),
    )


def build_transformer(
    source_crs: Any,
    target_crs: Any,
    *,
    area_of_interest: Optional[AreaOfInterest] = None,
    allow_ballpark: bool = True,
    accuracy: Optional[float] = None,
) -> tuple[Optional[Transformer], TransformReport]:
    src = parse_crs(source_crs)
    dst = parse_crs(target_crs)
    src_id = crs_identifier(src)
    dst_id = crs_identifier(dst)
    if src is None or dst is None:
        return None, TransformReport(src_id, dst_id, warnings=("Source or target CRS is unresolved.",))
    try:
        if src.equals(dst):
            return None, TransformReport(src_id, dst_id, operation="Identity", accuracy_m=0.0)
    except Exception:
        pass

    cache = _transformer_cache()
    cache_key = _transformer_cache_key(src, dst, area_of_interest, allow_ballpark, accuracy)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    warnings = []
    try:
        group = TransformerGroup(
            src,
            dst,
            always_xy=True,
            area_of_interest=area_of_interest,
            accuracy=accuracy,
            allow_ballpark=allow_ballpark,
        )
        transformer = group.transformers[0] if group.transformers else None
        missing = _collect_missing_grids(group)
        if not group.best_available:
            warnings.append("PROJ reports that the best coordinate operation is unavailable.")
        if missing:
            warnings.append("One or more higher-accuracy transformation grids are not installed.")
        if transformer is None:
            result = (None, TransformReport(
                src_id, dst_id, best_available=bool(group.best_available),
                missing_grids=missing,
                warnings=tuple(warnings + ["No coordinate operation is available."]),
            ))
        else:
            desc = str(getattr(transformer, "description", None) or getattr(transformer, "name", None) or "PROJ transformation")
            acc = getattr(transformer, "accuracy", None)
            try:
                acc = float(acc) if acc is not None and float(acc) >= 0 else None
            except Exception:
                acc = None
            ballpark = "ballpark" in desc.casefold()
            if ballpark:
                warnings.append("The selected operation is a ballpark transformation.")
            result = (transformer, TransformReport(
                src_id,
                dst_id,
                operation=desc,
                accuracy_m=acc,
                best_available=bool(group.best_available),
                ballpark=ballpark,
                missing_grids=missing,
                warnings=tuple(warnings),
            ))
    except Exception as exc:
        # Last-resort pyproj transformer. This preserves compatibility with
        # older PROJ builds while still reporting that operation selection was
        # degraded.
        try:
            t = Transformer.from_crs(src, dst, always_xy=True)
            result = (t, TransformReport(
                src_id, dst_id,
                operation=str(getattr(t, "description", None) or "PROJ transformation"),
                warnings=(f"TransformerGroup selection failed: {exc}",),
            ))
        except Exception as exc2:
            result = (None, TransformReport(src_id, dst_id, warnings=(str(exc2),)))

    if len(cache) >= _TRANSFORMER_CACHE_LIMIT:
        cache.clear()
    cache[cache_key] = result
    return result


def transform_xy(
    xs: Any,
    ys: Any,
    source_crs: Any,
    target_crs: Any,
    *,
    area_of_interest: Optional[AreaOfInterest] = None,
) -> tuple[Any, Any, TransformReport]:
    transformer, report = build_transformer(source_crs, target_crs, area_of_interest=area_of_interest)
    if transformer is None:
        if report.operation == "Identity":
            return xs, ys, report
        detail = "; ".join(report.warnings or ()) or "No coordinate operation is available."
        if report.missing_grids:
            detail += " Missing grids: " + ", ".join(report.missing_grids)
        raise RuntimeError(f"Cannot transform {report.source} -> {report.target}: {detail}")
    ax = np.asarray(xs, dtype=np.float64)
    ay = np.asarray(ys, dtype=np.float64)
    nx, ny = transformer.transform(ax, ay)
    return np.asarray(nx, dtype=np.float64), np.asarray(ny, dtype=np.float64), report


def transform_xyz(
    xs: Any,
    ys: Any,
    zs: Any,
    source_crs: Any,
    target_crs: Any,
    *,
    area_of_interest: Optional[AreaOfInterest] = None,
) -> tuple[Any, Any, Any, TransformReport]:
    transformer, report = build_transformer(source_crs, target_crs, area_of_interest=area_of_interest)
    if transformer is None:
        if report.operation == "Identity":
            return xs, ys, zs, report
        detail = "; ".join(report.warnings or ()) or "No coordinate operation is available."
        if report.missing_grids:
            detail += " Missing grids: " + ", ".join(report.missing_grids)
        raise RuntimeError(f"Cannot transform {report.source} -> {report.target}: {detail}")
    ax = np.asarray(xs, dtype=np.float64)
    ay = np.asarray(ys, dtype=np.float64)
    az = np.asarray(zs, dtype=np.float64)
    nx, ny, nz = transformer.transform(ax, ay, az)
    return (
        np.asarray(nx, dtype=np.float64),
        np.asarray(ny, dtype=np.float64),
        np.asarray(nz, dtype=np.float64),
        report,
    )


def transform_points(
    points: Any,
    source_crs: Any,
    target_crs: Any,
    *,
    copy: bool = True,
    chunk_size: int = 1_000_000,
    area_of_interest: Optional[AreaOfInterest] = None,
) -> tuple[np.ndarray, TransformReport]:
    """Transform an Nx2/Nx3 array with bounded memory use.

    The returned array is float64.  ``copy=False`` updates the supplied float64
    ndarray in place when possible; otherwise a float64 copy is made.
    """
    arr0 = np.asarray(points)
    if arr0.ndim != 2 or arr0.shape[1] < 2:
        raise ValueError("points must be an Nx2 or Nx3 array")
    src = parse_crs(source_crs)
    dst = parse_crs(target_crs)
    if src is None or dst is None:
        raise ValueError(
            f"Cannot transform points with unresolved CRS: "
            f"{crs_identifier(src)} -> {crs_identifier(dst)}"
        )
    try:
        if src.equals(dst):
            return (np.array(arr0, dtype=np.float64, copy=copy),
                    TransformReport(crs_identifier(src), crs_identifier(dst), operation="Identity", accuracy_m=0.0))
    except Exception:
        pass

    transformer, report = build_transformer(src, dst, area_of_interest=area_of_interest)
    if transformer is None:
        detail = "; ".join(report.warnings or ()) or "No coordinate operation is available."
        if report.missing_grids:
            detail += " Missing grids: " + ", ".join(report.missing_grids)
        raise RuntimeError(f"Cannot transform {report.source} -> {report.target}: {detail}")

    if not copy and isinstance(points, np.ndarray) and points.dtype == np.float64 and points.flags.writeable:
        out = points
    else:
        out = np.asarray(points, dtype=np.float64).copy()

    n = len(out)
    step = max(1, int(chunk_size))
    has_z = out.shape[1] >= 3
    for start in range(0, n, step):
        end = min(n, start + step)
        if has_z:
            x, y, z = transformer.transform(out[start:end, 0], out[start:end, 1], out[start:end, 2])
            out[start:end, 0] = x
            out[start:end, 1] = y
            out[start:end, 2] = z
        else:
            x, y = transformer.transform(out[start:end, 0], out[start:end, 1])
            out[start:end, 0] = x
            out[start:end, 1] = y
    return out, report


def transform_point_sequence(
    points: Sequence[Sequence[float]],
    source_crs: Any,
    target_crs: Any,
) -> list[tuple[float, ...]]:
    if not points:
        return []
    arr = np.asarray(points, dtype=np.float64)
    if arr.ndim != 2 or arr.shape[1] < 2:
        return [tuple(p) for p in points]
    out, _ = transform_points(arr, source_crs, target_crs, copy=True, chunk_size=max(1, len(arr)))
    return [tuple(float(v) for v in row) for row in out]


def area_of_interest_from_bounds(bounds: Sequence[float], bounds_crs: Any) -> Optional[AreaOfInterest]:
    """Convert XY bounds to a WGS84 AOI suitable for PROJ operation ranking."""
    if bounds is None or len(bounds) < 4:
        return None
    src = parse_crs(bounds_crs)
    if src is None:
        return None
    try:
        x0, x1, y0, y1 = map(float, bounds[:4])
        xs = np.asarray([x0, x0, x1, x1], dtype=np.float64)
        ys = np.asarray([y0, y1, y0, y1], dtype=np.float64)
        lon, lat, _ = transform_xy(xs, ys, src, CRS.from_epsg(4326))
        west = float(np.nanmin(lon)); east = float(np.nanmax(lon))
        south = float(np.nanmin(lat)); north = float(np.nanmax(lat))
        if not all(map(math.isfinite, (west, south, east, north))):
            return None
        return AreaOfInterest(west, south, east, north)
    except Exception:
        return None


def write_prj_sidecar(dataset_path: str, crs: Any, *, esri_compatible: bool = True) -> Optional[str]:
    """Write a same-stem .prj sidecar and return its path.

    WKT1_ESRI is preferred for interoperability with ArcMap/ArcCatalog.  WKT2 is
    retained as a safe fallback for CRSs WKT1 cannot represent.
    """
    obj = parse_crs(crs)
    if obj is None:
        return None
    path = Path(str(dataset_path)).with_suffix(".prj")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = None
    if esri_compatible:
        try:
            text = obj.to_wkt(version="WKT1_ESRI")
        except Exception:
            text = None
    if not text:
        try:
            text = obj.to_wkt(version="WKT2_2019")
        except Exception:
            text = obj.to_wkt()
    path.write_text(text, encoding="utf-8")
    return str(path)


def proj_database_metadata() -> dict:
    out = {}
    if _proj_db is None:
        return out
    keys = [
        "EPSG.VERSION", "EPSG.DATE", "ESRI.VERSION", "ESRI.DATE",
        "PROJ.VERSION", "PROJ_DATA.VERSION",
    ]
    for key in keys:
        try:
            out[key] = _proj_db.get_database_metadata(key)
        except Exception:
            out[key] = None
    return out


class ProjectionEngine:
    """App-facing facade around the stateless functions above."""

    def __init__(self, app=None):
        self.app = app
        self.sources: dict[str, dict] = {}
        self.last_report: Optional[TransformReport] = None

    @property
    def project_crs(self) -> Optional[CRS]:
        if self.app is None:
            return None
        return parse_crs(getattr(self.app, "canvas_crs", None) or getattr(self.app, "crs", None))

    def register_source(self, source_id: str, crs: Any, **metadata) -> dict:
        obj = parse_crs(crs)
        entry = {
            "id": str(source_id),
            "crs": obj,
            "crs_wkt": obj.to_wkt() if obj is not None else None,
            "identifier": crs_identifier(obj),
            **metadata,
        }
        self.sources[str(source_id)] = entry
        return entry

    def to_project(self, points: Any, source_crs: Any, *, copy=True, chunk_size=1_000_000):
        dst = self.project_crs
        out, report = transform_points(points, source_crs, dst, copy=copy, chunk_size=chunk_size)
        self.last_report = report
        return out, report

    def from_project(self, points: Any, target_crs: Any, *, copy=True, chunk_size=1_000_000):
        src = self.project_crs
        out, report = transform_points(points, src, target_crs, copy=copy, chunk_size=chunk_size)
        self.last_report = report
        return out, report


def get_projection_engine(app) -> ProjectionEngine:
    eng = getattr(app, "projection_engine", None) if app is not None else None
    if isinstance(eng, ProjectionEngine):
        return eng
    eng = ProjectionEngine(app)
    if app is not None:
        try:
            app.projection_engine = eng
        except Exception:
            pass
    return eng
