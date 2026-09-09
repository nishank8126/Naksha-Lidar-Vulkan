# ─────────────────────────────────────────────────────────────────────────────
# gui/gis/gdb/reader.py — GDB → VTK actor pipeline + feature-write API
#
# Responsibilities:
#   1. Open an ESRI File Geodatabase in read (or update) mode via OGR
#   2. Iterate every feature in a feature class and build VTK polydata
#      (Point / LineString / Polygon / Multi* with Z / M support)
#   3. Register the resulting actor with the existing gis_layers.py
#      Overlay Control Center — same panel, same look, same ordering
#   4. Provide a write API for the features panel: create / update / delete
#      features using the engine's non-nullable-defaults + cascade logic
#   5. Re-import an edited feature class so the on-screen actor reflects
#      the saved changes immediately
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from osgeo import ogr, osr, gdal

log = logging.getLogger("GDBReader")

# Suppress OGR's stderr noise on geometry operations that "fail" (e.g. calling
# GetPointCount on a Multi Surface) — we work around them by recursing into
# sub-geometries, and the messages only confuse the user.  Set via env var
# (CPL_DEBUG=OFF) so the suppression is process-wide and persistent.
gdal.SetConfigOption("CPL_DEBUG", "OFF")
try:
    gdal.PushErrorHandler("CPLQuietErrorHandler")
except Exception:
    pass

# ─────────────────────────────────────────────────────────────────────────────
#  GDB file/folder resolution
# ─────────────────────────────────────────────────────────────────────────────

def _is_gdb_folder(path: str) -> bool:
    """True if `path` is a directory that looks like an Esri FileGDB.

    Normal FileGDBs are folders ending in .gdb and containing the small
    catalog file named ``gdb`` plus table files. Some export tools omit the
    extension casing or produce incomplete-but-openable folders, so this check
    is intentionally tolerant.
    """
    if not path:
        return False
    p = Path(path)
    if not p.is_dir():
        return False
    try:
        names = {e.name.lower() for e in p.iterdir()}
    except OSError:
        return False
    has_gdb = "gdb" in names
    has_table = any(n.endswith(".gdbtable") for n in names)
    # Accept a real catalog signature, but do not require .gdbtable because
    # damaged / partially-copied GDBs can still expose metadata through GDAL.
    return has_gdb or (p.name.lower().endswith(".gdb") and has_table)


def _is_zipped_gdb(path: str) -> bool:
    return str(path or "").lower().endswith(".gdb.zip")


def _is_direct_gdbtable(path: str) -> bool:
    return str(path or "").lower().endswith(".gdbtable")


def _resolve_gdb_path(path: str) -> str | None:
    """Resolve FileGDB inputs accepted by GDAL/ArcGIS-style workflows.

    Accepts:
      * a .gdb folder
      * a .gdb.zip archive for read-only imports
      * a single .gdbtable for diagnostics/read-only single-layer opening
      * a file inside a .gdb folder
      * a parent folder containing exactly one .gdb folder
    """
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None

    if p.is_file() and (_is_zipped_gdb(str(p)) or _is_direct_gdbtable(str(p))):
        return str(p)

    if p.is_dir():
        if _is_gdb_folder(str(p)):
            return str(p)
        try:
            subs = [c for c in p.iterdir() if c.is_dir() and _is_gdb_folder(str(c))]
        except OSError:
            subs = []
        if len(subs) == 1:
            return str(subs[0])
        return None

    cur = p.parent
    for _ in range(12):
        if _is_gdb_folder(str(cur)):
            return str(cur)
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def _gdb_lock_files(gdb_path: str) -> list[str]:
    """Return lock-file paths for diagnostics. Stale locks are not deleted."""
    try:
        p = Path(gdb_path)
        if not p.is_dir():
            return []
        return sorted(str(x) for x in p.glob("*.lock"))
    except Exception:
        return []


def _open_gdb_dataset(gdb_path: str, *, update: bool = False):
    """Open a FileGDB with explicit vector flags and driver fallback.

    ``ogr.Open`` usually works, but ``gdal.OpenEx`` lets us prefer the built-in
    OpenFileGDB driver and still fall back to the Esri FileGDB SDK driver when
    available. It also opens .gdb.zip archives, which OpenFileGDB documents as
    supported for read-only use.
    """
    flags = gdal.OF_VECTOR | (gdal.OF_UPDATE if update else gdal.OF_READONLY)
    drivers = [d for d in ("OpenFileGDB", "FileGDB") if gdal.GetDriverByName(d)]
    if not drivers:
        drivers = None
    try:
        ds = gdal.OpenEx(gdb_path, flags, allowed_drivers=drivers)
    except TypeError:
        ds = gdal.OpenEx(gdb_path, flags)
    if ds is not None:
        return ds
    # Last fallback for older GDAL Python bindings.
    return ogr.Open(gdb_path, 1 if update else 0)


# ─────────────────────────────────────────────────────────────────────────────
#  OGR feature-class introspection
# ─────────────────────────────────────────────────────────────────────────────

def open_gdb_for_read(path: str):
    """Open a FileGDB in read mode. Returns a GDAL/OGR dataset or None."""
    gdb = _resolve_gdb_path(path)
    if gdb is None:
        log.error("open_gdb_for_read: not a supported FileGDB input: %s", path)
        return None
    try:
        ds = _open_gdb_dataset(gdb, update=False)
        if ds is None:
            log.error("open_gdb_for_read: GDAL could not open %s", gdb)
        return ds
    except Exception as exc:
        log.error("open_gdb_for_read: open failed for %s: %s", gdb, exc)
        return None


def open_gdb_for_update(path: str):
    """Open a FileGDB in update mode for create/update/delete features."""
    gdb = _resolve_gdb_path(path)
    if gdb is None:
        log.error("open_gdb_for_update: not a supported FileGDB input: %s", path)
        return None
    if _is_zipped_gdb(gdb) or _is_direct_gdbtable(gdb):
        log.error("open_gdb_for_update: %s is read-only in this app", gdb)
        return None
    try:
        ds = _open_gdb_dataset(gdb, update=True)
        if ds is None:
            locks = _gdb_lock_files(gdb)
            if locks:
                log.error(
                    "open_gdb_for_update: GDAL could not acquire update access. "
                    "Lock files exist in the GDB folder: %s", locks[:5])
            else:
                log.error("open_gdb_for_update: GDAL could not open %s for update", gdb)
        return ds
    except Exception as exc:
        log.error("open_gdb_for_update: open failed for %s: %s", gdb, exc)
        return None


def list_layer_names(path: str) -> list[str]:
    """Return a sorted list of feature class names in the GDB."""
    ds = open_gdb_for_read(path)
    if ds is None:
        return []
    try:
        names = []
        for i in range(ds.GetLayerCount()):
            lyr = ds.GetLayerByIndex(i)
            if lyr is not None:
                names.append(lyr.GetName())
        return sorted(names, key=str.casefold)
    finally:
        ds = None


def _layer_key(name: str) -> str:
    if not name:
        return ""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _layer_name_variants(name: str) -> set[str]:
    raw = str(name or "").strip()
    if not raw:
        return set()
    variants = {raw.lower(), _layer_key(raw)}
    normalized = raw.replace("\\", "/").strip("/")
    if normalized:
        variants.add(normalized.lower())
        variants.add(_layer_key(normalized))
        tail = normalized.split("/")[-1].strip()
        if tail:
            variants.add(tail.lower())
            variants.add(_layer_key(tail))
    return {v for v in variants if v}


def get_layer(ds, name: str):
    """Get an OGR Layer by name (case-insensitive). Returns None on miss."""
    if ds is None:
        return None
    try:
        lyr = ds.GetLayerByName(name)
        if lyr is not None:
            return lyr
    except Exception:
        pass
    # Case-insensitive fallback
    try:
        for i in range(ds.GetLayerCount()):
            cand = ds.GetLayerByIndex(i)
            if cand.GetName().lower() == name.lower():
                return cand
    except Exception:
        pass
    # Driver-name fallback (dataset/layer vs layer-only naming differences).
    wanted = _layer_name_variants(name)
    if not wanted:
        return None
    try:
        for i in range(ds.GetLayerCount()):
            cand = ds.GetLayerByIndex(i)
            cand_name = cand.GetName() if cand is not None else ""
            if wanted.intersection(_layer_name_variants(cand_name)):
                return cand
    except Exception:
        pass
    return None


def feature_count(lyr) -> int:
    if lyr is None:
        return 0
    try:
        return int(lyr.GetFeatureCount())
    except Exception:
        return 0


# In OGR's Python bindings, the M flag is encoded as +0x7D0 (2000) in the
# "old" (pre-ISO WKB) layout, and as 0x40000000 in the ISO layout.  We use
# both names' M-bit to stay portable across GDAL versions.
_WKB_M_BIT_OLD = 0x7D0
_WKB_M_BIT_ISO = 0x40000000
_WKB_Z_BIT = 0x80000000


def _has_z(geom_type: int) -> bool:
    """True if the geometry type includes a Z dimension."""
    return bool(geom_type & _WKB_Z_BIT) or '25D' in geometry_type_name(geom_type)


def _has_m(geom_type: int) -> bool:
    """True if the geometry type includes an M (measure) dimension."""
    if geom_type & _WKB_M_BIT_OLD or geom_type & _WKB_M_BIT_ISO:
        return True
    return 'M' in geometry_type_name(geom_type)


def geometry_type_name(geom_type: int) -> str:
    """Raw OGR name (e.g. 'Measured Multi Polygon'), or 'Unknown'."""
    try:
        name = ogr.GeometryTypeToName(geom_type) or "Unknown"
    except Exception:
        name = "Unknown"
    return name


def geometry_type_label(geom_type: int) -> str:
    """Return a human-friendly name like 'Polygon', 'LineString (Z+M)'."""
    name = geometry_type_name(geom_type)
    has_z = _has_z(geom_type)
    has_m = _has_m(geom_type)
    suffix = []
    if has_z:
        suffix.append("Z")
    if has_m:
        suffix.append("M")
    if suffix:
        name = f"{name} ({'+'.join(suffix)})"
    return name


def geometry_kind(geom_type: int) -> str:
    """Return one of: 'point' | 'line' | 'polygon' | 'unknown'."""
    name = geometry_type_name(geom_type).lower().replace(" ", "")
    try:
        base = ogr.GT_Flatten(geom_type)
    except Exception:
        base = geom_type & ~(_WKB_M_BIT_OLD | _WKB_M_BIT_ISO | _WKB_Z_BIT)
    point_types = {
        ogr.wkbPoint,
        ogr.wkbMultiPoint,
    }
    line_types = {
        ogr.wkbLineString,
        ogr.wkbMultiLineString,
        getattr(ogr, "wkbCircularString", -1),
        getattr(ogr, "wkbCompoundCurve", -1),
        getattr(ogr, "wkbLinearRing", -1),
        getattr(ogr, "wkbMultiCurve", -1),
    }
    polygon_types = {
        ogr.wkbPolygon,
        ogr.wkbMultiPolygon,
        getattr(ogr, "wkbCurvePolygon", -1),
        getattr(ogr, "wkbMultiSurface", -1),
        getattr(ogr, "wkbPolyhedralSurface", -1),
        getattr(ogr, "wkbTIN", -1),
        getattr(ogr, "wkbTriangle", -1),
    }

    if base in point_types or name.endswith("point") or "multipoint" in name:
        return "point"
    if (base in polygon_types or "polygon" in name or "surface" in name
            or "triangle" in name or "tin" in name):
        return "polygon"
    if base in line_types or "curve" in name or "line" in name:
        return "line"
    if base == ogr.wkbNone or base == ogr.wkbUnknown:
        return "none"
    return "unknown"


def _flat_geom_type(geom) -> int:
    try:
        return ogr.GT_Flatten(geom.GetGeometryType())
    except Exception:
        try:
            return geom.GetGeometryType() & ~(_WKB_M_BIT_OLD | _WKB_M_BIT_ISO | _WKB_Z_BIT)
        except Exception:
            return ogr.wkbUnknown


def _geom_type_name(geom) -> str:
    try:
        return (ogr.GeometryTypeToName(geom.GetGeometryType()) or "").lower()
    except Exception:
        return ""


def _linearized_geom(geom):
    """Return a display-safe linear geometry for ArcGIS curve types."""
    if geom is None:
        return None
    name = _geom_type_name(geom)
    if "curve" not in name and "circular" not in name:
        return geom
    try:
        linear = geom.GetLinearGeometry()
        return linear if linear is not None else geom
    except Exception:
        return geom


def _finite_xyz(point):
    """Return a finite XYZ tuple or None; VTK must never receive NaN/Inf XY."""
    import math
    if point is None or len(point) < 2:
        return None
    try:
        x, y = float(point[0]), float(point[1])
        z = float(point[2]) if len(point) >= 3 else 0.0
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(x) or not math.isfinite(y):
        return None
    if not math.isfinite(z):
        z = 0.0
    return (x, y, z)


def _point_list_from_geom(geom) -> list[tuple[float, float, float]]:
    """Read direct vertices from Point/LineString/LinearRing/Triangle-like geometry."""
    geom = _linearized_geom(geom)
    if geom is None or geom.IsEmpty():
        return []
    # Bulk path: GetPoints() pulls every vertex in one C call instead of three
    # per-index GetX/GetY/GetZ SWIG calls - this is the difference between a
    # sub-millisecond read and a multi-second one on vertex-heavy features.
    # Only valid for Point/LineString/LinearRing; returns None otherwise, so
    # fall back to the per-index loop for anything GetPoints() can't handle.
    try:
        raw = geom.GetPoints()
    except Exception:
        raw = None
    if raw is not None:
        pts = []
        for point in raw:
            clean = _finite_xyz(point)
            if clean is not None:
                pts.append(clean)
        return pts

    pts = []
    try:
        n = geom.GetPointCount()
    except Exception:
        n = 0
    for i in range(n):
        try:
            x, y, z = geom.GetX(i), geom.GetY(i), geom.GetZ(i)
        except Exception:
            try:
                x, y, z = geom.GetX(i), geom.GetY(i), 0.0
            except Exception:
                continue
        clean = _finite_xyz((x, y, z))
        if clean is not None:
            pts.append(clean)
    return pts


def _iter_line_parts(geom) -> list[list[tuple[float, float, float]]]:
    """Return independent line parts without connecting multipart features."""
    if geom is None or geom.IsEmpty():
        return []
    geom = _linearized_geom(geom)
    flat = _flat_geom_type(geom)
    name = _geom_type_name(geom)

    line_types = {
        ogr.wkbLineString, getattr(ogr, "wkbLinearRing", -1),
        getattr(ogr, "wkbCircularString", -1), getattr(ogr, "wkbCompoundCurve", -1),
    }
    if flat in line_types or ("line" in name and "multi" not in name) or "curve" in name:
        pts = _sanitize_ring_points(_point_list_from_geom(geom))
        return [pts] if len(pts) >= 2 else []

    if (flat in (ogr.wkbMultiLineString, ogr.wkbGeometryCollection)
            or "multicurve" in name or "multiline" in name):
        parts = []
        try:
            n_sub = geom.GetGeometryCount()
        except Exception:
            n_sub = 0
        for i in range(n_sub):
            sub = geom.GetGeometryRef(i)
            if sub is not None:
                parts.extend(_iter_line_parts(sub))
        return parts

    pts = _sanitize_ring_points(list(_iter_xyz(geom)))
    return [pts] if len(pts) >= 2 else []


def _has_real_vertex_z(geom, tolerance: float = 1e-4) -> bool:
    vals = [z for _x, _y, z in _iter_xyz(geom)
            if z is not None and z == z and -1000000.0 <= z <= 1000000.0]
    if not vals:
        return False
    return (max(vals) - min(vals)) > tolerance or any(abs(z) > tolerance for z in vals)


# ─────────────────────────────────────────────────────────────────────────────
#  Coordinate extraction
# ─────────────────────────────────────────────────────────────────────────────

def _iter_xyz(geom) -> tuple:
    """Yield (x, y, z) for every vertex of an OGR geometry.

    Handles normal OGR types plus ArcGIS curve/multi-surface/multipatch
    geometries exposed through GDAL as GeometryCollection, TIN, Triangle,
    PolyhedralSurface, MultiSurface, and related 25D/M variants. M values are
    ignored because VTK needs display coordinates only.
    """
    if geom is None or geom.IsEmpty():
        return
    geom = _linearized_geom(geom)
    flat = _flat_geom_type(geom)
    name = _geom_type_name(geom)

    if flat == ogr.wkbPoint:
        for pt in _point_list_from_geom(geom):
            yield pt
        return

    container_types = {
        ogr.wkbPolygon, ogr.wkbMultiPolygon, ogr.wkbMultiPoint,
        ogr.wkbMultiLineString, ogr.wkbGeometryCollection,
        getattr(ogr, "wkbCurvePolygon", -1), getattr(ogr, "wkbCompoundCurve", -1),
        getattr(ogr, "wkbMultiCurve", -1), getattr(ogr, "wkbMultiSurface", -1),
        getattr(ogr, "wkbPolyhedralSurface", -1), getattr(ogr, "wkbTIN", -1),
    }
    if (flat in container_types or "polygon" in name or "surface" in name
            or "geometrycollection" in name or "tin" in name
            or "multicurve" in name or "multiline" in name):
        try:
            n_sub = geom.GetGeometryCount()
        except Exception:
            n_sub = 0
        if n_sub > 0:
            for i in range(n_sub):
                sub = geom.GetGeometryRef(i)
                if sub is not None:
                    for v in _iter_xyz(sub):
                        yield v
            return

    for pt in _point_list_from_geom(geom):
        yield pt


def _iter_rings(geom) -> list:
    """Return a list of rings (each ring is a list of (x, y, z)).

    Outer ring + all inner rings of a polygon. MultiPolygon nests them
    in order: outer_0, hole_0, hole_1, outer_1, hole_0, ...

    Tolerates ISO 19107 supertypes (MultiSurface, MultiCurve) which
    GDB layers sometimes expose instead of plain MultiPolygon / MultiLineString.
    """
    if geom is None or geom.IsEmpty():
        return []
    polygon_parts = _iter_polygon_parts(geom)
    if polygon_parts:
        return [ring for part in polygon_parts for ring in part]

    rings = []
    raw_type = geom.GetGeometryType()
    try:
        gtype = ogr.GT_Flatten(raw_type)
    except Exception:
        gtype = raw_type & 0xFFFFFFFC
    name = (ogr.GeometryTypeToName(raw_type) or "").lower()

    if gtype in (ogr.wkbLineString, ogr.wkbMultiLineString) or "curve" in name or "line" in name:
        pts = _sanitize_ring_points(list(_iter_xyz(geom)))
        if len(pts) >= 2:
            rings.append(pts)
        return rings

    # Unknown — try to recurse into sub-geometries anyway (e.g. GeometryCollection).
    for i in range(geom.GetGeometryCount()):
        sub = geom.GetGeometryRef(i)
        if sub is None:
            continue
        rings.extend(_iter_rings(sub))
    return rings


def _sanitize_ring_points(ring: list) -> list:
    """Drop non-finite and duplicate vertices plus the closing duplicate."""
    clean = []
    for raw_point in ring or []:
        point = _finite_xyz(raw_point)
        if point is None:
            continue
        if clean:
            px, py, _pz = clean[-1]
            if abs(px - point[0]) <= 1e-9 and abs(py - point[1]) <= 1e-9:
                continue
        clean.append(point)
    if len(clean) >= 2:
        x0, y0, _ = clean[0]
        x1, y1, _ = clean[-1]
        if abs(x0 - x1) <= 1e-9 and abs(y0 - y1) <= 1e-9:
            clean.pop()
    return clean


def _ring_signed_area_xy(ring: list) -> float:
    if len(ring) < 3:
        return 0.0
    area = 0.0
    for i, (x1, y1, _z1) in enumerate(ring):
        x2, y2, _z2 = ring[(i + 1) % len(ring)]
        area += (x1 * y2) - (x2 * y1)
    return 0.5 * area


def _normalize_ring_orientation(ring: list, clockwise: bool) -> list:
    """Force a ring to the orientation expected by vtkContourTriangulator."""
    if len(ring) < 3:
        return ring
    is_clockwise = _ring_signed_area_xy(ring) < 0.0
    if is_clockwise != clockwise:
        return list(reversed(ring))
    return ring


def _iter_polygon_parts(geom) -> list:
    """Return polygon parts as [[outer_ring, hole1, hole2], ...].

    The important ArcGIS case is Multipatch: GDAL can expose it as a
    GeometryCollection containing TIN/Triangle/PolyhedralSurface pieces mixed
    with MultiPolygon pieces. Treat triangles as one-ring polygon parts so they
    render instead of silently disappearing.
    """
    if geom is None or geom.IsEmpty():
        return []

    geom = _linearized_geom(geom)
    gtype = _flat_geom_type(geom)
    name = _geom_type_name(geom)

    polygon_types = {ogr.wkbPolygon, getattr(ogr, "wkbCurvePolygon", -1)}
    triangle_types = {getattr(ogr, "wkbTriangle", -1)}
    collection_types = {
        ogr.wkbMultiPolygon, ogr.wkbGeometryCollection,
        getattr(ogr, "wkbMultiSurface", -1),
        getattr(ogr, "wkbPolyhedralSurface", -1),
        getattr(ogr, "wkbTIN", -1),
    }

    if gtype in triangle_types or "triangle" in name:
        ring = _sanitize_ring_points(_point_list_from_geom(geom))
        return [[ring]] if len(ring) >= 3 else []

    if gtype in polygon_types:
        part = []
        try:
            n_rings = geom.GetGeometryCount()
        except Exception:
            n_rings = 0
        if n_rings == 0:
            ring = _sanitize_ring_points(_point_list_from_geom(geom))
            return [[ring]] if len(ring) >= 3 else []
        for ring_idx in range(n_rings):
            ring = geom.GetGeometryRef(ring_idx)
            if ring is None:
                continue
            pts = _sanitize_ring_points(list(_iter_xyz(ring)))
            if len(pts) >= 3:
                part.append(pts)
        return [part] if part else []

    if (gtype in collection_types or "surface" in name or "tin" in name
            or gtype == ogr.wkbGeometryCollection):
        parts = []
        try:
            n_sub = geom.GetGeometryCount()
        except Exception:
            n_sub = 0
        for i in range(n_sub):
            sub = geom.GetGeometryRef(i)
            if sub is not None:
                parts.extend(_iter_polygon_parts(sub))
        return parts

    return []


# ─────────────────────────────────────────────────────────────────────────────
#  VTK actor builders
# ─────────────────────────────────────────────────────────────────────────────

def _hex_to_rgb_floats(hex_color: str) -> tuple:
    """'#ff8800' -> (1.0, 0.533, 0.0)."""
    h = (hex_color or '').lstrip('#')
    if len(h) < 6:
        return (1.0, 0.0, 0.0)
    try:
        r = int(h[0:2], 16) / 255.0
        g = int(h[2:4], 16) / 255.0
        b = int(h[4:6], 16) / 255.0
        return (r, g, b)
    except ValueError:
        return (1.0, 0.0, 0.0)


def _clean_z(z: float, default_z: float) -> float:
    import math
    if z is None or math.isnan(z) or math.isinf(z):
        return default_z
    if z < -10000.0 or z > 10000.0:
        return default_z
    if abs(z) < 1e-4:
        return default_z
    return z


def _infer_scene_z(app) -> float:
    import numpy as np
    try:
        data = getattr(app, "data", None)
        if isinstance(data, dict):
            xyz = data.get("xyz")
            if xyz is not None:
                xyz = np.asarray(xyz)
                if xyz.ndim == 2 and xyz.shape[1] >= 3 and len(xyz) > 0:
                    return float(np.median(xyz[:, 2]))
    except Exception:
        pass
    try:
        renderer = getattr(getattr(app, "vtk_widget", None), "renderer", None)
        if renderer is not None:
            return float(renderer.GetActiveCamera().GetFocalPoint()[2])
    except Exception:
        pass
    return 0.0


def _determine_feature_flat_z(geom, default_z: float) -> float:
    z_values = []
    for x, y, z in _iter_xyz(geom):
        if z is not None and z == z and abs(z) > 1e-4 and -10000.0 <= z <= 10000.0:
            z_values.append(z)
    if len(z_values) > 0:
        import numpy as np
        return float(np.median(z_values))
    return default_z


def _build_point_polydata(geoms: list, scene_z: float) -> tuple:
    """Build a vtkPolyData containing one vertex per input geometry point.

    Returns (vtkPolyData, label) where label is unused but kept for
    symmetry with the line/polygon builders.
    """
    import vtk
    pts = vtk.vtkPoints()
    pts.SetDataType(vtk.VTK_DOUBLE)  # vtkPoints defaults to float32; UTM-scale coords need double
    for g in geoms:
        if g is None or g.IsEmpty():
            continue
        for x, y, z in _iter_xyz(g):
            pts.InsertNextPoint(x, y, _clean_z(z, scene_z))

    if pts.GetNumberOfPoints() == 0:
        return None

    verts = vtk.vtkCellArray()
    for i in range(pts.GetNumberOfPoints()):
        v = vtk.vtkVertex()
        v.GetPointIds().SetId(0, i)
        verts.InsertNextCell(v)

    pd = vtk.vtkPolyData()
    pd.SetPoints(pts)
    pd.SetVerts(verts)
    return pd


def _build_line_polydata(geoms: list, scene_z: float) -> tuple:
    """Build a vtkPolyData containing one polyline per independent line part."""
    import vtk
    pts = vtk.vtkPoints()
    pts.SetDataType(vtk.VTK_DOUBLE)  # vtkPoints defaults to float32; UTM-scale coords need double
    lines = vtk.vtkCellArray()
    for g in geoms:
        if g is None or g.IsEmpty():
            continue
        use_vertex_z = _has_real_vertex_z(g)
        flat_z = _determine_feature_flat_z(g, scene_z)
        for ring_pts in _iter_line_parts(g):
            if len(ring_pts) < 2:
                continue
            start = pts.GetNumberOfPoints()
            for x, y, z in ring_pts:
                pts.InsertNextPoint(x, y, _clean_z(z, flat_z) if use_vertex_z else flat_z)
            line = vtk.vtkPolyLine()
            line.GetPointIds().SetNumberOfIds(len(ring_pts))
            for i in range(len(ring_pts)):
                line.GetPointIds().SetId(i, start + i)
            lines.InsertNextCell(line)

    if pts.GetNumberOfPoints() == 0:
        return None

    pd = vtk.vtkPolyData()
    pd.SetPoints(pts)
    pd.SetLines(lines)
    return pd


def _triangulate_polygon_part(rings: list, flat_z: float, use_vertex_z: bool = False):
    """Triangulate one polygon part (outer ring + holes) into fill triangles."""
    import vtk

    contour_pts = vtk.vtkPoints()
    contour_pts.SetDataType(vtk.VTK_DOUBLE)  # vtkPoints defaults to float32; UTM-scale coords need double
    contour_lines = vtk.vtkCellArray()
    valid_rings = []

    for ring_idx, raw_ring in enumerate(rings or []):
        ring = _normalize_ring_orientation(
            _sanitize_ring_points(raw_ring),
            clockwise=bool(ring_idx),
        )
        if len(ring) < 3:
            continue
        valid_rings.append(ring)
        polyline = vtk.vtkPolyLine()
        polyline.GetPointIds().SetNumberOfIds(len(ring) + 1)
        start = contour_pts.GetNumberOfPoints()
        for i, (x, y, z) in enumerate(ring):
            pid = contour_pts.InsertNextPoint(x, y, _clean_z(z, flat_z) if use_vertex_z else flat_z)
            polyline.GetPointIds().SetId(i, pid)
        polyline.GetPointIds().SetId(len(ring), start)
        contour_lines.InsertNextCell(polyline)

    if not valid_rings:
        return None

    contours = vtk.vtkPolyData()
    contours.SetPoints(contour_pts)
    contours.SetLines(contour_lines)

    triangulator = vtk.vtkContourTriangulator()
    triangulator.SetInputData(contours)
    triangulator.Update()
    out = triangulator.GetOutput()

    if out is not None and out.GetNumberOfPolys() > 0:
        copy = vtk.vtkPolyData()
        copy.ShallowCopy(out)
        return copy

    # Fallback for simple single-ring polygons if VTK rejects the contours.
    if len(valid_rings) != 1:
        return None

    pts = vtk.vtkPoints()
    pts.SetDataType(vtk.VTK_DOUBLE)  # vtkPoints defaults to float32; UTM-scale coords need double
    polys = vtk.vtkCellArray()
    poly = vtk.vtkPolygon()
    poly.GetPointIds().SetNumberOfIds(len(valid_rings[0]))
    for i, (x, y, z) in enumerate(valid_rings[0]):
        pid = pts.InsertNextPoint(x, y, _clean_z(z, flat_z) if use_vertex_z else flat_z)
        poly.GetPointIds().SetId(i, pid)
    polys.InsertNextCell(poly)

    pd = vtk.vtkPolyData()
    pd.SetPoints(pts)
    pd.SetPolys(polys)

    tri_filter = vtk.vtkTriangleFilter()
    tri_filter.SetInputData(pd)
    tri_filter.Update()
    out = tri_filter.GetOutput()
    if out is None or out.GetNumberOfPolys() == 0:
        return None
    copy = vtk.vtkPolyData()
    copy.ShallowCopy(out)
    return copy


def _triangulate_simple_polygons_batch(entries: list):
    """Triangulate many independent single-ring (no-hole) polygons in ONE
    vtkTriangleFilter pass instead of one VTK filter call per polygon.

    vtkTriangleFilter triangulates each polygon cell independently - unlike
    vtkContourTriangulator, cells never interact with each other, so batching
    unrelated features' simple rings into one call is always correct
    regardless of whether their footprints overlap in world space. This is
    what makes it safe to batch across an entire layer: a layer with 27k
    simple building footprints pays one VTK call instead of 27k.
    """
    import vtk

    pts = vtk.vtkPoints()
    pts.SetDataType(vtk.VTK_DOUBLE)  # vtkPoints defaults to float32; UTM-scale coords need double
    polys = vtk.vtkCellArray()
    for ring, flat_z, use_vertex_z in entries:
        poly = vtk.vtkPolygon()
        poly.GetPointIds().SetNumberOfIds(len(ring))
        for i, (x, y, z) in enumerate(ring):
            pid = pts.InsertNextPoint(x, y, _clean_z(z, flat_z) if use_vertex_z else flat_z)
            poly.GetPointIds().SetId(i, pid)
        polys.InsertNextCell(poly)

    if polys.GetNumberOfCells() == 0:
        return None

    pd = vtk.vtkPolyData()
    pd.SetPoints(pts)
    pd.SetPolys(polys)

    tri_filter = vtk.vtkTriangleFilter()
    tri_filter.SetInputData(pd)
    tri_filter.Update()
    out = tri_filter.GetOutput()
    if out is None or out.GetNumberOfPolys() == 0:
        return None
    copy = vtk.vtkPolyData()
    copy.ShallowCopy(out)
    return copy


def _build_polygon_polydata(geoms: list, scene_z: float) -> tuple:
    """Build polygon fill + outline polydata from complex GDB geometries.

    Single-ring (no-hole) parts - the overwhelming majority in real GIS
    layers (buildings, most zoning, most water bodies) - are collected and
    triangulated in one batched vtkTriangleFilter call at the end instead of
    one vtkContourTriangulator call per part. Parts with holes keep the
    per-part vtkContourTriangulator path, since batching hole-bearing rings
    across features risks nesting ambiguity if two features' footprints
    overlap in world space (not safe to assume clean topology in real data).
    """
    import vtk
    fill_append = vtk.vtkAppendPolyData()
    outline_pts = vtk.vtkPoints()
    outline_pts.SetDataType(vtk.VTK_DOUBLE)  # vtkPoints defaults to float32; UTM-scale coords need double
    outline_lines = vtk.vtkCellArray()
    fill_inputs = 0
    simple_batch = []   # (ring, flat_z, use_vertex_z) for no-hole parts

    for g in geoms:
        if g is None or g.IsEmpty():
            continue

        flat_z = _determine_feature_flat_z(g, scene_z)
        use_vertex_z = _has_real_vertex_z(g)

        polygon_parts = _iter_polygon_parts(g)
        for part in polygon_parts:
            if not part:
                continue
            valid_rings = []
            if len(part) == 1:
                ring = _sanitize_ring_points(part[0])
                if len(ring) >= 3:
                    valid_rings.append(ring)
            else:
                for ring_idx, raw_ring in enumerate(part):
                    ring = _normalize_ring_orientation(
                        _sanitize_ring_points(raw_ring),
                        clockwise=bool(ring_idx),
                    )
                    if len(ring) >= 3:
                        valid_rings.append(ring)

            if len(valid_rings) == 1:
                simple_batch.append((valid_rings[0], flat_z, use_vertex_z))
            elif valid_rings:
                fill_pd = _triangulate_polygon_part(part, flat_z, use_vertex_z=use_vertex_z)
                if fill_pd is not None and fill_pd.GetNumberOfPolys() > 0:
                    fill_append.AddInputData(fill_pd)
                    fill_inputs += 1

            for ring in valid_rings:
                polyline = vtk.vtkPolyLine()
                polyline.GetPointIds().SetNumberOfIds(len(ring) + 1)
                start = outline_pts.GetNumberOfPoints()
                for i, (x, y, z) in enumerate(ring):
                    pid = outline_pts.InsertNextPoint(x, y, _clean_z(z, flat_z) if use_vertex_z else flat_z)
                    polyline.GetPointIds().SetId(i, pid)
                polyline.GetPointIds().SetId(len(ring), start)
                outline_lines.InsertNextCell(polyline)

    if simple_batch:
        batch_pd = _triangulate_simple_polygons_batch(simple_batch)
        if batch_pd is not None and batch_pd.GetNumberOfPolys() > 0:
            fill_append.AddInputData(batch_pd)
            fill_inputs += 1

    fill_pd = None
    if fill_inputs:
        fill_append.Update()
        out = fill_append.GetOutput()
        if out is not None and out.GetNumberOfPolys() > 0:
            fill_pd = vtk.vtkPolyData()
            fill_pd.ShallowCopy(out)

    outline_pd = None
    if outline_pts.GetNumberOfPoints() > 0:
        outline_pd = vtk.vtkPolyData()
        outline_pd.SetPoints(outline_pts)
        outline_pd.SetLines(outline_lines)

    if fill_pd is None and outline_pd is None:
        return (None, None)
    return (fill_pd, outline_pd)


# A pleasant QGIS-style palette for GDB layers (no two adjacent look the same).
_GDB_PALETTE = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
    "#008080", "#9a6324", "#46c2cb", "#ffd43b", "#e377c2",
    "#1abc9c", "#d62728", "#2c3e50", "#f39c12", "#27ae60",
]


def _pick_color(name: str, used: set) -> str:
    """
    Generate a deterministic, highly unique color based on name/hashing,
    ensuring it does not collide or get visually too close to any colors in 'used'.
    Uses golden-angle HSL rotation to distribute hues beautifully on the color wheel.
    """
    import colorsys
    clean_used = {u.lower() for u in used if u}
    
    # Deterministic starting seed based on name hash
    seed = abs(hash(name)) if name else 0
    golden_angle = 137.508
    
    for attempt in range(100):
        hue = ((seed + attempt) * golden_angle) % 360.0
        # alternate saturation and lightness slightly for visual variety
        saturation = 0.72 + 0.13 * (((seed + attempt) % 3) - 1)
        lightness = 0.48 + 0.07 * (((seed + attempt) % 2) * 2 - 1)
        
        r, g, b = colorsys.hls_to_rgb(hue / 360.0, lightness, saturation)
        hex_color = "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))
        
        # Verify distinctness against already used colors
        is_too_close = False
        rc, gc, bc = int(r * 255), int(g * 255), int(b * 255)
        for used_col in clean_used:
            try:
                hc = used_col.lstrip('#')
                if len(hc) == 6:
                    ur = int(hc[0:2], 16)
                    ug = int(hc[2:4], 16)
                    ub = int(hc[4:6], 16)
                    dist = ((rc - ur)**2 + (gc - ug)**2 + (bc - ub)**2)**0.5
                    if dist < 45:  # Ensure visual uniqueness
                        is_too_close = True
                        break
            except Exception:
                pass
                
        if not is_too_close:
            return hex_color
            
    # Fallback to deterministic sequence if all collide
    hue = (seed * golden_angle) % 360.0
    r, g, b = colorsys.hls_to_rgb(hue / 360.0, 0.5, 0.7)
    return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))


def _make_vtk_actor(polydata, kind: str, color_hex: str, *, outline: bool = False):
    """Create a styled actor for one imported GDB subtype."""
    if polydata is None:
        return None

    import vtk

    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputData(polydata)
    actor = vtk.vtkActor()
    actor.SetMapper(mapper)

    r, g, b = _hex_to_rgb_floats(color_hex)
    prop = actor.GetProperty()

    if kind == "point":
        mapper.SetResolveCoincidentTopologyToPolygonOffset()
        mapper.SetRelativeCoincidentTopologyPointOffsetParameter(-10.0)
        prop.SetColor(r, g, b)
        prop.SetPointSize(6.0)
        prop.SetRenderPointsAsSpheres(True)
        actor.PickableOn()
    elif kind == "line" or outline:
        if outline and kind == "polygon":
            r, g, b = (0.0, 0.0, 0.0)
            prop.SetOpacity(0.98)
            prop.SetLineWidth(1.35)
            actor.PickableOff()
        else:
            prop.SetOpacity(1.0)
            prop.SetLineWidth(2.0)
            actor.PickableOn()
        prop.SetColor(r, g, b)
    else:
        prop.SetColor(r, g, b)
        prop.SetOpacity(0.55)
        prop.EdgeVisibilityOff()
        actor.PickableOn()

    actor.VisibilityOn()
    return actor


# ─────────────────────────────────────────────────────────────────────────────
#  Public: read a feature class to VTK actors
# ─────────────────────────────────────────────────────────────────────────────

def _get_used_colors(app) -> set:
    """Return the set of color strings already used by registered GIS layers."""
    try:
        from gui.gis.gis_layers import _registry
        reg = _registry(app)
        return {e.get("color") for e in reg if e.get("color")}
    except Exception:
        return set()


def _register_gdb_entry(app, gdb_path: str, layer_name: str, *,
                        kind: str, geom: str, actors: list | None = None,
                        color: str | None = None, sub_layer_info: dict | None = None,
                        placeholder: bool = False,
                        placeholder_reason: str | None = None,
                        source_crs=None,
                        feature_count: int | None = None) -> dict | None:
    try:
        from gui.gis.gis_layers import register_gis_layer
        display_name = f"{Path(gdb_path).stem} · {layer_name}"
        entry = register_gis_layer(
            app, display_name, gdb_path, kind, "gdb", actors or [],
            allow_empty=placeholder or not actors,
            feature_count=feature_count,
            source_layer=layer_name,
        )
    except Exception as exc:
        log.error("import_gdb_layer: registry failed: %s", exc)
        return None

    if entry is not None:
        entry["geom"] = geom
        entry["color"] = color or entry.get("color")
        entry["gdb_layer_name"] = layer_name
        entry["gdb_path"] = gdb_path
        entry["_placement"] = "gdb-native"
        entry["sub_layers"] = sub_layer_info or {}
        entry["placeholder"] = bool(placeholder)
        if feature_count is not None:
            try:
                entry["feature_count"] = max(0, int(feature_count))
            except Exception:
                pass
        if placeholder_reason:
            entry["placeholder_reason"] = placeholder_reason
        try:
            from gui.gis.layer_model import GISLayerModel, attach_native_model
            from gui.crs_manager import get_canvas_crs
            source_wkt = source_crs.to_wkt() if source_crs is not None else None
            project_crs = get_canvas_crs(app)
            project_wkt = project_crs.to_wkt() if project_crs is not None else source_wkt
            attach_native_model(entry, GISLayerModel(
                path=str(gdb_path),
                layer_name=str(layer_name),
                driver="OpenFileGDB",
                kind="table" if kind == "table" else "vector",
                geometry_type=str(geom or ""),
                feature_count=int(feature_count) if feature_count is not None else -1,
                source_crs_wkt=source_wkt,
                project_crs_wkt=project_wkt,
                read_only=False,
            ))
        except Exception:
            pass
        try:
            from gui.gis.gis_layers import get_layer_epsg
            entry["epsg"] = get_layer_epsg(gdb_path, layer_name)
        except Exception:
            pass
        # Preserve the layer's OWN source CRS separately from the canvas CRS.
        # Prefer the CRS already resolved by import_gdb_layer() (it has a WKT
        # fallback for compound/vertical CRS such as "UTM 43N + EGM2008
        # height", which is common in real GDB deliveries and has no single
        # OGR/GDAL authority code - get_layer_epsg() above returns None for
        # those, so without this the registry would show an unknown source
        # CRS for a layer whose geometry was in fact correctly transformed.
        if source_crs is not None:
            entry["source_crs"] = source_crs
            try:
                _code = source_crs.to_epsg()
                if _code:
                    entry["source_epsg"] = int(_code)
                    if not entry.get("epsg"):
                        entry["epsg"] = f"EPSG:{_code}"
            except Exception:
                pass
            try:
                entry["source_wkt"] = source_crs.to_wkt()
            except Exception:
                pass
        else:
            try:
                if entry.get("source_crs") is None and entry.get("epsg"):
                    from pyproj import CRS as _CRS
                    _code = str(entry["epsg"]).split(":")[-1]
                    entry["source_crs"] = _CRS.from_epsg(int(_code))
                    entry["source_epsg"] = int(_code)
            except Exception:
                pass
    return entry



def _report_import_error(app, title: str, text: str) -> None:
    try:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.critical(app, title, text)
    except Exception:
        log.error("%s: %s", title, text)


def import_gdb_layer(app, gdb_path: str, layer_name: str,
                     color: str | None = None,
                     max_features: int = 0) -> dict | None:
    """Open `gdb_path`, read `layer_name`, build VTK actor(s), register the layer.

    Returns the registry entry on success, or None on failure.

    Parameters
    ----------
    app : QMainWindow
        The main app (used to look up renderers + register the layer).
    gdb_path : str
        Path to a .gdb folder, a file inside it, or its parent.
    layer_name : str
        The OGR feature class name.
    color : str, optional
        '#rrggbb' to use for the layer; if None, an unused palette color
        is picked automatically.
    max_features : int, optional
        If > 0, stop after reading this many features (used for huge layers).
    """
    _t0 = time.perf_counter()
    ds = open_gdb_for_read(gdb_path)
    log.warning("import_gdb_layer[%s]: open dataset %.3fs", layer_name, time.perf_counter() - _t0)
    if ds is None:
        return None
    try:
        lyr = get_layer(ds, layer_name)
        if lyr is None:
            log.error("import_gdb_layer: layer '%s' not found in %s",
                     layer_name, gdb_path)
            return None

        defn = lyr.GetLayerDefn()
        if defn is None:
            log.error("import_gdb_layer: layer '%s' has no definition", layer_name)
            return None

        # ---- CRS: resolve this feature class's OWN source CRS ----------------
        # Every feature class carries its own spatial reference; do NOT assume
        # the whole GDB shares one CRS. The source CRS is preserved on the
        # registry entry and geometries are transformed source_crs -> canvas_crs
        # before they become VTK points.
        src_srs = None
        src_crs = None
        try:
            src_srs = lyr.GetSpatialRef()
        except Exception:
            src_srs = None
        if src_srs is not None:
            try:
                src_srs = src_srs.Clone()
            except Exception:
                pass
            try:
                src_srs.AutoIdentifyEPSG()
            except Exception:
                pass
            try:
                from pyproj import CRS as _CRS
                code = src_srs.GetAuthorityCode(None)
                if code:
                    src_crs = _CRS.from_epsg(int(code))
                else:
                    src_crs = _CRS.from_wkt(src_srs.ExportToWkt())
            except Exception:
                try:
                    from pyproj import CRS as _CRS
                    src_crs = _CRS.from_wkt(src_srs.ExportToWkt())
                except Exception:
                    src_crs = None

        canvas_crs = None
        ogr_ct = None
        transform_required = False
        unknown_source_in_project = False
        try:
            from gui.crs_manager import get_canvas_crs, ensure_canvas_crs

            # First trustworthy georeferenced dataset establishes Project CRS.
            if src_crs is not None:
                ensure_canvas_crs(
                    app, src_crs, source="GDB feature class",
                    dataset=f"{os.path.basename(str(gdb_path))}:{layer_name}",
                )
            canvas_crs = get_canvas_crs(app)

            # Delay the unknown-CRS decision until feature iteration proves
            # that this item contains real geometry. Tables, empty feature
            # classes and rows with null geometry do not need map placement.
            unknown_source_in_project = canvas_crs is not None and src_srs is None

            if src_srs is not None and canvas_crs is not None:
                try:
                    same_crs = src_crs is not None and src_crs.equals(canvas_crs)
                except Exception:
                    same_crs = False
                if not same_crs:
                    transform_required = True
                    tgt = osr.SpatialReference()
                    rc = tgt.ImportFromWkt(canvas_crs.to_wkt())
                    if rc not in (None, 0):
                        raise RuntimeError("Could not construct the Project CRS spatial reference.")
                    tgt.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                    src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                    ogr_ct = osr.CreateCoordinateTransformation(src_srs, tgt)
                    if ogr_ct is None:
                        raise RuntimeError("GDAL could not create the source -> Project CRS transformation.")
        except Exception as e:
            log.error("import_gdb_layer[%s]: CRS setup failed: %s", layer_name, e)
            _report_import_error(
                app,
                "Coordinate Transformation Failed",
                f"Could not safely place FileGDB layer '{layer_name}' in the Naksha Project CRS.\n\n"
                f"{e}\n\nThe layer was NOT loaded.",
            )
            return None

        try:
            lyr.ResetReading()
        except Exception:
            pass

        try:
            geom_type = defn.GetGeomType()
        except Exception:
            geom_type = ogr.wkbUnknown
        kind = geometry_kind(geom_type)
        geom_label = geometry_type_label(geom_type)
        base_color = color or _pick_color(layer_name, _get_used_colors(app))

        if kind == "none":
            log.warning(
                "import_gdb_layer: '%s' is non-spatial (%s), registering as table",
                layer_name, geom_label,
            )
            return _register_gdb_entry(
                app, gdb_path, layer_name,
                kind="table", geom="table", actors=[],
                color=base_color, placeholder=True,
                placeholder_reason="table",
                feature_count=feature_count(lyr),
            )

        if kind == "unknown":
            log.warning(
                "import_gdb_layer: '%s' has no recognised geometry type '%s', skipping",
                layer_name, geom_label,
            )
            return _register_gdb_entry(
                app, gdb_path, layer_name,
                kind="vector", geom="unknown", actors=[],
                color=base_color, placeholder=True,
                placeholder_reason=f"unsupported:{geom_label}",
                feature_count=feature_count(lyr),
            )

        # Check for subtypes via GDBEngine
        _t1 = time.perf_counter()
        from gui.gis.gdb.engine import get_engine
        engine = get_engine(gdb_path)
        log.warning("import_gdb_layer[%s]: get_engine %.3fs", layer_name, time.perf_counter() - _t1)
        _t1 = time.perf_counter()
        cascade = engine.get_cascade_data(layer_name) if engine else None
        log.warning("import_gdb_layer[%s]: get_cascade_data %.3fs", layer_name, time.perf_counter() - _t1)
        
        subtype_field = None
        subtype_domain = {}
        if cascade and cascade.get("subtype_field"):
            subtype_field = cascade["subtype_field"]
            raw_subtype_domain = cascade.get("subtype_domain", {}) or {}
            subtype_domain = {
                str(code).strip(): (str(label).strip() or f"Subtype {code}")
                for code, label in raw_subtype_domain.items()
                if str(code).strip()
            }

        ovl = _get_overlay_renderer(app)
        used = _get_used_colors(app)
        if not color:
            color = _pick_color(layer_name, used)
        used.add(color)

        # Build subtype legend rows from the schema itself so empty cascaded
        # layers still expand like QGIS (for example ULU_Pnt with 0 features).
        sub_layer_info = {}
        for sub_code, sub_label in subtype_domain.items():
            sub_color = _pick_color(f"{layer_name}_{sub_label}", used)
            used.add(sub_color)
            sub_layer_info[sub_code] = {
                "actor": None,
                "actors": [],
                "color": sub_color,
                "label": sub_label,
                "feature_count": 0,
            }

        # Collect geometries in one pass (sparse iteration), grouped by subtype if available
        _t2 = time.perf_counter()
        geoms_by_subtype = {sub_code: [] for sub_code in subtype_domain}
        row_count = 0
        geom_count = 0
        
        # Determine total features for progress tracking
        try:
            total_features = lyr.GetFeatureCount()
        except Exception:
            total_features = 0
            
        progress = None
        if total_features > 1000:
            try:
                from PySide6.QtWidgets import QProgressDialog
                from PySide6.QtCore import Qt, QCoreApplication
                progress = QProgressDialog(
                    f"Loading features from '{layer_name}'...", "Cancel", 0, total_features, app
                )
                progress.setWindowTitle("Import Layer")
                progress.setAttribute(Qt.WA_DeleteOnClose, True)
                progress.setWindowModality(Qt.ApplicationModal)
                progress.setMinimumDuration(200)
                progress.setValue(0)
                QCoreApplication.processEvents()
            except Exception:
                progress = None

        lyr.ResetReading()
        feat = lyr.GetNextFeature()
        canceled = False
        transform_error = None
        subtype_feature_counts = {sub_code: 0 for sub_code in subtype_domain}
        while feat is not None:
            row_count += 1
            if progress is not None and row_count % 500 == 0:
                try:
                    from shiboken6 import isValid
                    if not isValid(progress):
                        progress = None
                except Exception:
                    pass
            if progress is not None and row_count % 500 == 0:
                try:
                    progress.setValue(min(row_count, max(total_features, row_count)))
                    QCoreApplication.processEvents()
                    if progress.wasCanceled():
                        log.warning("import_gdb_layer[%s]: loading canceled by user", layer_name)
                        canceled = True
                        break
                except RuntimeError:
                    # Dialog's native object was deleted (user closed it via
                    # the title bar mid-import) between the isValid() check
                    # above and this call - stop touching it, keep loading.
                    progress = None

            # Resolve subtype for every row, not only rows with geometry, so the
            # Layers panel reports real GIS feature counts rather than VTK cells.
            sub_code = None
            if subtype_field:
                try:
                    val = feat.GetField(subtype_field)
                    sub_code = str(val).strip() if val is not None else None
                except Exception:
                    sub_code = None
                if sub_code and sub_code not in subtype_domain:
                    try:
                        int_code = str(int(float(sub_code)))
                        if int_code in subtype_domain:
                            sub_code = int_code
                    except Exception:
                        pass
                if sub_code not in subtype_domain:
                    sub_code = "default"
                subtype_feature_counts[sub_code] = subtype_feature_counts.get(sub_code, 0) + 1

            try:
                g = feat.GetGeometryRef()
            except Exception:
                g = None
            if g is not None and not g.IsEmpty():
                geom_count += 1
                if sub_code not in geoms_by_subtype:
                    geoms_by_subtype[sub_code] = []
                _clone = g.Clone()   # Clone because feat is destroyed
                if ogr_ct is not None:
                    try:
                        rc = _clone.Transform(ogr_ct)
                        if rc not in (None, 0):
                            raise RuntimeError(f"OGR Transform returned error code {rc}")
                    except Exception as exc:
                        transform_error = f"Feature FID {feat.GetFID()}: {exc}"
                        feat = None
                        break
                geoms_by_subtype[sub_code].append(_clone)
            feat = lyr.GetNextFeature()
            if max_features:
                total_collected = sum(len(lst) for lst in geoms_by_subtype.values())
                if total_collected >= max_features:
                    break

        # Attach authoritative per-subtype feature counts.  These are OGR rows,
        # not VTK cells/triangles.
        for _code, _count in subtype_feature_counts.items():
            _key = _code if _code is not None else "default"
            if _key not in sub_layer_info:
                sub_layer_info[_key] = {
                    "actor": None, "actors": [], "color": color,
                    "label": ("Unclassified" if _key == "default" and subtype_field else layer_name),
                }
            sub_layer_info[_key]["feature_count"] = int(_count)

        if progress is not None:
            try:
                if not canceled:
                    progress.setValue(total_features)
                progress.close()
            except RuntimeError:
                pass

        if canceled:
            return None
        if unknown_source_in_project and geom_count > 0:
            geoms_by_subtype.clear()
            _report_import_error(
                app,
                "Unknown Layer CRS",
                f"The FileGDB layer '{layer_name}' contains geometry but has no declared "
                "coordinate reference system, while the Naksha project already has a Project CRS.\n\n"
                "Naksha will not place unknown raw coordinates into the Project CRS. "
                "Define/repair the layer CRS first, then load it again.",
            )
            return None

        if transform_error:
            geoms_by_subtype.clear()
            _report_import_error(
                app,
                "Coordinate Transformation Failed",
                f"A feature in FileGDB layer '{layer_name}' could not be transformed into the Project CRS.\n\n"
                f"{transform_error}\n\nThe layer was NOT loaded.",
            )
            return None

        log.warning(
            "import_gdb_layer[%s]: feature-collection loop %.3fs (rows=%d geoms=%d)",
            layer_name, time.perf_counter() - _t2, row_count, geom_count,
        )

        if not geoms_by_subtype or not any(geoms_by_subtype.values()):
            if row_count == 0:
                log.warning("import_gdb_layer: '%s' has no features", layer_name)
                reason = "empty"
            else:
                log.warning(
                    "import_gdb_layer: '%s' has %d row(s) but no non-empty %s geometry",
                    layer_name, row_count, kind,
                )
                reason = "no_non_empty_geometry"
            return _register_gdb_entry(
                app, gdb_path, layer_name,
                kind="vector", geom=kind, actors=[],
                color=base_color, sub_layer_info=sub_layer_info,
                placeholder=True,
                placeholder_reason=reason,
                source_crs=src_crs,
                feature_count=(total_features if total_features >= 0 else row_count),
            )

        # Build actors and polydata per subtype
        _t3 = time.perf_counter()
        actors = []

        # Infer scene_z to drape/align 2D reference layers and clean uninitialized coordinates
        scene_z = _infer_scene_z(app)

        for sub_code, sub_geoms in geoms_by_subtype.items():
            if not sub_geoms:
                continue

            outline_pd = None
            if kind == "point":
                pd = _build_point_polydata(sub_geoms, scene_z)
            elif kind == "line":
                pd = _build_line_polydata(sub_geoms, scene_z)
            else:  # polygon
                pd, outline_pd = _build_polygon_polydata(sub_geoms, scene_z)

            # Free the cloned geoms
            for g in sub_geoms:
                try:
                    g = None
                except Exception:
                    pass

            if pd is None and outline_pd is None:
                continue

            # Style
            entry_key = sub_code if sub_code is not None else "default"
            sub_info = sub_layer_info.get(entry_key)
            if sub_info is not None:
                sub_color = sub_info.get("color") or color
                sub_label = sub_info.get("label") or layer_name
            else:
                if entry_key == "default" and subtype_field:
                    sub_label = "Unclassified"
                    sub_color = _pick_color(f"{layer_name}_{sub_label}", used)
                    used.add(sub_color)
                elif entry_key == "default":
                    sub_label = layer_name
                    sub_color = color
                else:
                    sub_label = subtype_domain.get(entry_key, f"Subtype {entry_key}")
                    sub_color = _pick_color(f"{layer_name}_{sub_label}", used)
                    used.add(sub_color)

            actor_bundle = []
            if pd is not None:
                actor = _make_vtk_actor(pd, kind, sub_color, outline=False)
                if actor is not None:
                    actor_bundle.append(actor)

            if outline_pd is not None:
                outline_actor = _make_vtk_actor(outline_pd, kind, sub_color, outline=True)
                if outline_actor is not None:
                    actor_bundle.append(outline_actor)

            if not actor_bundle:
                continue

            if ovl is not None:
                for actor in actor_bundle:
                    try:
                        ovl.AddActor(actor)
                    except Exception:
                        pass

            actors.extend(actor_bundle)
            sub_layer_info[entry_key] = {
                "actor": actor_bundle[0] if actor_bundle else None,
                "actors": actor_bundle,
                "color": sub_color,
                "label": sub_label,
                "feature_count": int(subtype_feature_counts.get(sub_code, len(sub_geoms))),
            }

        if not actors:
            log.warning(
                "import_gdb_layer: '%s' read %d geometry feature(s) but produced no renderable %s actor",
                layer_name, geom_count, kind,
            )
            return _register_gdb_entry(
                app, gdb_path, layer_name,
                kind="vector", geom=kind, actors=[],
                color=color, sub_layer_info=sub_layer_info,
                placeholder=True,
                placeholder_reason="no_renderable_actor",
                source_crs=src_crs,
                feature_count=(total_features if total_features >= 0 else row_count),
            )

        log.warning(
            "import_gdb_layer[%s]: polydata+actor build %.3fs (%d actors)",
            layer_name, time.perf_counter() - _t3, len(actors),
        )

        # Refresh
        _t4 = time.perf_counter()
        _render(app)
        log.warning("import_gdb_layer[%s]: render %.3fs", layer_name, time.perf_counter() - _t4)

        entry = _register_gdb_entry(
            app, gdb_path, layer_name,
            kind="vector", geom=kind, actors=actors,
            color=color, sub_layer_info=sub_layer_info,
            source_crs=src_crs,
            feature_count=(total_features if total_features >= 0 else row_count),
        )
        if entry is not None:
            entry["loaded_feature_count"] = int(row_count)
            entry["rendered_geometry_feature_count"] = int(geom_count)
        try:
            from gui.crs_manager import log_dataset_crs, get_canvas_crs as _gcc
            _raw = lyr.GetExtent()  # xmin, xmax, ymin, ymax (OGR order)
            log_dataset_crs(
                f"{os.path.basename(str(gdb_path))}:{layer_name}", "GDB",
                src_crs, ("OGR SRS" if src_crs is not None else None),
                raw_bounds=(_raw[0], _raw[2], _raw[1], _raw[3]) if _raw else None,
                canvas_crs=_gcc(app),
            )
        except Exception:
            pass
        log.warning("import_gdb_layer[%s]: TOTAL %.3fs", layer_name, time.perf_counter() - _t0)
        if entry is not None:
            try:
                from gui.gis.gis_layers import zoom_to_gis_entries
                zoom_to_gis_entries(app, [entry])
            except Exception:
                pass

        return entry
    finally:
        ds = None


# ─────────────────────────────────────────────────────────────────────────────
#  Write API — used by features_panel.py
# ─────────────────────────────────────────────────────────────────────────────

def _to_ogr_geometry(coords, geom_type_hint: int = ogr.wkbUnknown):
    """Build an OGR geometry from user coordinates using the target type.

    The old implementation inferred polygons from ``list[tuple]`` and therefore
    broke LineString/Point creation from the UI. This version honours
    ``geom_type_hint`` first and only falls back to polygon detection when the
    hint is unknown.
    """
    if not coords:
        return None

    def as_point_tuple(pt):
        if pt is None or len(pt) < 2:
            return None
        z = float(pt[2]) if len(pt) >= 3 else 0.0
        return (float(pt[0]), float(pt[1]), z)

    flat_hint = geom_type_hint
    try:
        flat_hint = ogr.GT_Flatten(geom_type_hint)
    except Exception:
        pass

    # Normalise flat coordinate lists and polygon ring lists.
    first = coords[0]
    is_ring_list = (
        isinstance(first, (list, tuple)) and first and
        isinstance(first[0], (list, tuple))
    )

    if flat_hint == ogr.wkbPoint:
        pts = coords[0] if is_ring_list else coords
        pt = as_point_tuple(pts[0] if pts and isinstance(pts[0], (list, tuple)) else pts)
        if pt is None:
            return None
        g = ogr.Geometry(ogr.wkbPoint)
        g.AddPoint(pt[0], pt[1], pt[2])
        return g

    if flat_hint in (ogr.wkbLineString, getattr(ogr, "wkbCircularString", -999)):
        points = coords[0] if is_ring_list else coords
        if len(points) < 2:
            return None
        g = ogr.Geometry(ogr.wkbLineString)
        for raw in points:
            pt = as_point_tuple(raw)
            if pt is not None:
                g.AddPoint(pt[0], pt[1], pt[2])
        return g if not g.IsEmpty() and g.GetPointCount() >= 2 else None

    # Default/fallback: polygon. Accept either [pt, pt, pt] or [[ring], [hole]].
    rings = coords if is_ring_list else [coords]
    poly = ogr.Geometry(ogr.wkbPolygon)
    for ring in rings:
        if not ring or len(ring) < 3:
            continue
        r = ogr.Geometry(ogr.wkbLinearRing)
        for raw in ring:
            pt = as_point_tuple(raw)
            if pt is not None:
                r.AddPoint(pt[0], pt[1], pt[2])
        if r.GetPointCount() >= 3:
            r.CloseRings()
            poly.AddGeometry(r)
    if poly.IsEmpty():
        return None
    return poly


def _coerce_attribute_value(value, esri_type: str = ""):
    """Best-effort value conversion before OGR Feature.SetField()."""
    if value is None:
        return None
    et = (esri_type or "").lower()
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return None
    if et in {"esrifieldtypeinteger", "esrifieldtypesmallinteger",
              "esrifieldtypeoid", "esrifieldtypebiginteger"}:
        try:
            return int(float(value))
        except Exception:
            return value
    if et in {"esrifieldtypedouble", "esrifieldtypesingle"}:
        try:
            return float(value)
        except Exception:
            return value
    return value


def create_feature(gdb_path: str, layer_name: str, attributes: dict,
                   coords, geom_type: int = ogr.wkbPolygon) -> int:
    """Create a new feature in `layer_name`.

    Parameters
    ----------
    gdb_path : str
        Path to a GDB folder (or a file/parent inside it).
    layer_name : str
        OGR feature class name.
    attributes : dict
        {field_name (case-preserved): value} — non-nullable defaults
        are auto-filled from the engine.
    coords : list
        For polygons: list of rings (each a list of (x, y, z))
        For lines/points: list of (x, y, z) or (x, y)
    geom_type : int
        ogr.wkbPoint / ogr.wkbLineString / ogr.wkbPolygon / etc.

    Returns
    -------
    int
        The new feature id, or -1 on failure.
    """
    from .engine import get_engine

    eng = get_engine(gdb_path)
    ds = open_gdb_for_update(gdb_path)
    if ds is None:
        return -1
    try:
        lyr = get_layer(ds, layer_name)
        if lyr is None:
            log.error("create_feature: layer '%s' not found", layer_name)
            return -1
        defn = lyr.GetLayerDefn()
        if defn is None:
            return -1

        # Build geometry
        geom = _to_ogr_geometry(coords, geom_type)
        if geom is None:
            log.error("create_feature: empty geometry")
            return -1
        # Promote to the layer's actual 2D/3D/measured type if needed.
        try:
            target = defn.GetGeomType()
            if target != ogr.wkbUnknown and geom.GetGeometryType() != (target & 0xFFFFFFFC):
                # Force a flat-2D conversion (most GDBs are 2D) — VTK only uses XYZ.
                # We do NOT keep M values.
                pass
        except Exception:
            pass

        # Build the feature
        feat = ogr.Feature(defn)
        try:
            feat.SetGeometry(geom)
        except Exception as exc:
            log.error("create_feature: SetGeometry failed: %s", exc)
            feat = None
            return -1

        # Auto-fill non-nullable defaults
        try:
            nn_defaults = eng.get_non_nullable_default_values(layer_name) \
                if eng else {}
        except Exception:
            nn_defaults = {}
        try:
            field_types = eng.get_field_types(layer_name) if eng else {}
        except Exception:
            field_types = {}

        attrs = attributes or {}
        attrs_lower = {str(k).lower(): v for k, v in attrs.items()}

        # Set fields. The UI stores lower-case names, while OGR definitions keep
        # original casing, so always resolve both.
        for i in range(defn.GetFieldCount()):
            fdefn = defn.GetFieldDefn(i)
            fname = fdefn.GetName()
            fname_lower = fname.lower()
            if fname_lower in ('shape', 'objectid', 'globalid', 'fid', 'oid'):
                continue

            value = attrs.get(fname, attrs_lower.get(fname_lower, None))
            if value is None and fname_lower in nn_defaults:
                value = nn_defaults[fname_lower]
            if value is None:
                continue

            try:
                value = _coerce_attribute_value(value, field_types.get(fname_lower, ''))
                if value is not None:
                    feat.SetField(fname, value)
            except Exception as exc:
                log.debug("SetField(%s=%r) failed: %s", fname, value, exc)

        result = lyr.CreateFeature(feat)
        if result != ogr.OGRERR_NONE:
            log.error("create_feature: OGR error %s", result)
            feat = None
            return -1
        try:
            new_id = int(feat.GetFID())
        except Exception:
            new_id = -1
        feat = None
        # Force a re-read next time so any cache sees the new feature
        try:
            lyr.SyncToDisk()
        except Exception:
            pass
        return new_id
    finally:
        ds = None


def delete_feature(gdb_path: str, layer_name: str, fid: int) -> bool:
    """Delete a feature by id. Returns True on success."""
    ds = open_gdb_for_update(gdb_path)
    if ds is None:
        return False
    try:
        lyr = get_layer(ds, layer_name)
        if lyr is None:
            return False
        try:
            ok = lyr.DeleteFeature(int(fid))
        except Exception as exc:
            log.error("delete_feature: %s", exc)
            return False
        try:
            lyr.SyncToDisk()
        except Exception:
            pass
        return bool(ok == ogr.OGRERR_NONE)
    finally:
        ds = None


def update_feature_geometry(gdb_path: str, layer_name: str, fid: int,
                            coords, geom_type: int = ogr.wkbPolygon) -> bool:
    """Replace the geometry of an existing feature. Returns True on success."""
    ds = open_gdb_for_update(gdb_path)
    if ds is None:
        return False
    try:
        lyr = get_layer(ds, layer_name)
        if lyr is None:
            return False
        feat = lyr.GetFeature(int(fid))
        if feat is None:
            return False
        geom = _to_ogr_geometry(coords, geom_type)
        if geom is None:
            return False
        try:
            feat.SetGeometry(geom)
        except Exception as exc:
            log.error("update_feature_geometry: SetGeometry failed: %s", exc)
            return False
        try:
            ok = lyr.SetFeature(feat)
        except Exception:
            ok = ogr.OGRERR_FAILURE
        try:
            lyr.SyncToDisk()
        except Exception:
            pass
        return bool(ok == ogr.OGRERR_NONE)
    finally:
        ds = None


# ─────────────────────────────────────────────────────────────────────────────
#  Re-import helper — used after edits to refresh the on-screen actor
# ─────────────────────────────────────────────────────────────────────────────

def reload_gdb_layer(app, gdb_path: str, layer_name: str) -> dict | None:
    """Remove the existing on-screen actor for `layer_name` and re-read it.

    The registry entry's actor list is replaced; ordering and colour are
    preserved if a previous entry existed.
    """
    # Find the existing entry (if any) to preserve colour
    prev_color = None
    try:
        from gui.gis.gis_layers import _registry, _remove_layer
        for e in _registry(app):
            if (e.get("gdb_path") == gdb_path and
                    e.get("gdb_layer_name") == layer_name):
                prev_color = e.get("color")
                _remove_layer(app, e)
                break
    except Exception:
        pass

    return import_gdb_layer(app, gdb_path, layer_name, color=prev_color)


# ─────────────────────────────────────────────────────────────────────────────
#  App helpers (renderer lookup, refresh)
# ─────────────────────────────────────────────────────────────────────────────

def _get_overlay_renderer(app):
    try:
        dz = getattr(app, "digitizer", None)
        return getattr(dz, "overlay_renderer", None) if dz is not None else None
    except Exception:
        return None


def _render(app):
    """Render through the shared GIS batch gate."""
    try:
        from gui.gis.gis_layers import _render as render_gis
        return render_gis(app)
    except Exception:
        if getattr(app, "_gis_batch_update_depth", 0) > 0:
            app._gis_batch_render_pending = True
            return False
        try:
            app.vtk_widget.render()
            return True
        except Exception:
            try:
                app.vtk_widget.GetRenderWindow().Render()
                return True
            except Exception:
                return False
