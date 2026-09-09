"""Universal GIS catalog inspection used by the Naksha Catalog Browser.

The inspector never renders features.  It opens a datasource just long enough to
report its driver, schema, CRS, Z/M flags, feature counts, layer hierarchy and
raster metadata.  Keeping inspection separate from rendering is important for
large GDB/GPKG/SHP sources.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .capabilities import (
    VECTOR_EXTENSIONS, RASTER_EXTENSIONS, preferred_driver_for_path,
    get_driver_capability, gdal_version,
)

try:
    from osgeo import gdal, ogr, osr
except Exception:  # runtime error will be reported as catalog metadata
    gdal = None
    ogr = None
    osr = None


SYSTEM_LAYER_PREFIXES = ("gdb_", "sqlite_", "gpkg_")


def _has_z(geom_type: int) -> bool:
    if ogr is None:
        return False
    try:
        return bool(ogr.GT_HasZ(geom_type))
    except Exception:
        try:
            return bool(geom_type & getattr(ogr, "wkb25DBit", 0))
        except Exception:
            return False


def _has_m(geom_type: int) -> bool:
    if ogr is None:
        return False
    try:
        return bool(ogr.GT_HasM(geom_type))
    except Exception:
        try:
            return bool(geom_type & getattr(ogr, "wkbMBit", 0))
        except Exception:
            return False


def _flatten_geom_type(geom_type: int) -> int:
    if ogr is None:
        return geom_type
    try:
        return int(ogr.GT_Flatten(geom_type))
    except Exception:
        return int(geom_type)


def _geom_name(geom_type: int) -> str:
    if ogr is None:
        return "Unknown"
    try:
        return str(ogr.GeometryTypeToName(geom_type) or "Unknown")
    except Exception:
        return "Unknown"


def _geom_kind(geom_type: int) -> str:
    if ogr is None:
        return "unknown"
    flat = _flatten_geom_type(geom_type)
    point_types = {
        getattr(ogr, "wkbPoint", -1), getattr(ogr, "wkbMultiPoint", -2),
    }
    line_types = {
        getattr(ogr, "wkbLineString", -3), getattr(ogr, "wkbMultiLineString", -4),
        getattr(ogr, "wkbCircularString", -5), getattr(ogr, "wkbCompoundCurve", -6),
        getattr(ogr, "wkbMultiCurve", -7),
    }
    polygon_types = {
        getattr(ogr, "wkbPolygon", -8), getattr(ogr, "wkbMultiPolygon", -9),
        getattr(ogr, "wkbCurvePolygon", -10), getattr(ogr, "wkbMultiSurface", -11),
        getattr(ogr, "wkbTriangle", -12), getattr(ogr, "wkbTIN", -13),
        getattr(ogr, "wkbPolyhedralSurface", -14),
    }
    if flat in point_types:
        return "point"
    if flat in line_types:
        return "line"
    if flat in polygon_types:
        return "polygon"
    if flat == getattr(ogr, "wkbNone", 100):
        return "table"
    return "geometry"


def _srs_metadata(srs) -> Dict[str, Any]:
    if srs is None:
        return {"wkt": None, "epsg": None, "name": None, "is_projected": None}
    try:
        s = srs.Clone()
    except Exception:
        s = srs
    epsg = None
    try:
        s.AutoIdentifyEPSG()
        code = s.GetAuthorityCode(None)
        if not code and s.IsProjected():
            code = s.GetAuthorityCode("PROJCS")
        if not code:
            code = s.GetAuthorityCode("GEOGCS")
        epsg = int(code) if code else None
    except Exception:
        epsg = None
    try:
        wkt = s.ExportToWkt()
    except Exception:
        wkt = None
    try:
        name = s.GetName()
    except Exception:
        name = None
    try:
        is_projected = bool(s.IsProjected())
    except Exception:
        is_projected = None
    return {"wkt": wkt, "epsg": epsg, "name": name, "is_projected": is_projected}


def _safe_extent(layer, force: bool = False):
    try:
        ext = layer.GetExtent(force=1 if force else 0)
    except TypeError:
        try:
            ext = layer.GetExtent(1 if force else 0)
        except Exception:
            ext = None
    except Exception:
        ext = None
    if not ext:
        return None
    try:
        minx, maxx, miny, maxy = map(float, ext)
        return (minx, miny, maxx, maxy)
    except Exception:
        return None


def _field_info(field_defn) -> Dict[str, Any]:
    out = {
        "name": field_defn.GetNameRef(),
        "type": field_defn.GetFieldTypeName(field_defn.GetType()),
        "type_code": int(field_defn.GetType()),
        "width": int(field_defn.GetWidth()),
        "precision": int(field_defn.GetPrecision()),
    }
    for key, meth, default in (
        ("nullable", "IsNullable", True),
        ("unique", "IsUnique", False),
        ("default", "GetDefault", None),
        ("domain", "GetDomainName", ""),
        ("alias", "GetAlternativeNameRef", ""),
    ):
        try:
            fn = getattr(field_defn, meth)
            out[key] = fn()
        except Exception:
            out[key] = default
    return out


def _relationship_summary(ds) -> List[Dict[str, Any]]:
    if ds is None or not hasattr(ds, "GetRelationshipNames"):
        return []
    out = []
    try:
        names = list(ds.GetRelationshipNames() or [])
    except Exception:
        names = []
    for name in names:
        item = {"name": str(name)}
        try:
            rel = ds.GetRelationship(name)
            if rel is not None:
                for key, meth in (
                    ("left_table", "GetLeftTableName"),
                    ("right_table", "GetRightTableName"),
                    ("mapping_table", "GetMappingTableName"),
                    ("cardinality", "GetCardinality"),
                    ("type", "GetType"),
                ):
                    try:
                        item[key] = getattr(rel, meth)()
                    except Exception:
                        pass
        except Exception:
            pass
        out.append(item)
    return out


def _domain_summary(ds) -> List[Dict[str, Any]]:
    if ds is None or not hasattr(ds, "GetFieldDomainNames"):
        return []
    out = []
    try:
        names = list(ds.GetFieldDomainNames() or [])
    except Exception:
        names = []
    for name in names:
        item = {"name": str(name)}
        try:
            dom = ds.GetFieldDomain(name)
        except Exception:
            dom = None
        if dom is not None:
            for key, meth in (
                ("description", "GetDescription"),
                ("domain_type", "GetDomainType"),
                ("field_type", "GetFieldType"),
                ("field_subtype", "GetFieldSubType"),
                ("split_policy", "GetSplitPolicy"),
                ("merge_policy", "GetMergePolicy"),
            ):
                try:
                    item[key] = getattr(dom, meth)()
                except Exception:
                    pass
            # GetEnumeration() is Coded-only and GetMinAsDouble/GetMaxAsDouble are
            # Range-only - GDAL logs a CPLError ("should be called with a
            # range/coded field domain object") to stderr for the wrong type, even
            # though the bare except below swallows it. Gate by the domain's own
            # type so a GDB with many domains (e.g. a 124-domain FileGDB) doesn't
            # flood the log with spurious errors on every inspect.
            dtype = item.get("domain_type")
            if dtype == getattr(ogr, "OFDT_CODED", 0):
                try:
                    enum = dom.GetEnumeration()
                    if enum is not None:
                        item["coded_values"] = dict(enum)
                except Exception:
                    pass
            elif dtype == getattr(ogr, "OFDT_RANGE", 1):
                for key, meth in (("min", "GetMinAsDouble"), ("max", "GetMaxAsDouble")):
                    try:
                        item[key] = getattr(dom, meth)()
                    except Exception:
                        pass
        out.append(item)
    return out


def _open_vector(path: str, list_all_tables: bool = False):
    if gdal is None:
        return None
    options = ["LIST_ALL_TABLES=YES"] if list_all_tables and str(path).lower().endswith(".gdb") else None
    drivers = None
    if str(path).lower().endswith(".gdb"):
        drivers = [d for d in ("OpenFileGDB", "FileGDB") if gdal.GetDriverByName(d)]
    try:
        return gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_READONLY,
                           allowed_drivers=drivers or None,
                           open_options=options)
    except TypeError:
        return gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_READONLY)


def _gdb_engine(path: str):
    try:
        from .gdb.engine import get_engine
        return get_engine(path)
    except Exception:
        return None


def inspect_vector_source(path: str, *, include_fields: bool = True,
                          include_internal: bool = False,
                          verify_extents: bool = False) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "path": os.path.abspath(path),
        "kind": "vector",
        "gdal_version": gdal_version(),
        "layers": [],
        "domains": [],
        "relationships": [],
        "warnings": [],
    }
    if gdal is None:
        result["error"] = "GDAL Python bindings (osgeo) are not available."
        return result

    ds = _open_vector(path, list_all_tables=include_internal)
    if ds is None:
        # A just-created FileGDB can contain only its system/catalog tables and
        # some GDAL builds refuse to return a vector dataset until the first
        # user feature class/table exists.  That is an EMPTY database, not a
        # failed/corrupt source. Keep schema creation available in Catalog.
        low = str(path).lower()
        if low.endswith(".gdb") and os.path.isdir(path):
            try:
                entries = os.listdir(path)
            except Exception:
                entries = []
            looks_like_filegdb = any(
                name.lower().endswith((".gdbtable", ".gdbtablx", ".freelist"))
                or name.lower().startswith("a000000")
                for name in entries
            )
            if looks_like_filegdb or not entries:
                result["driver"] = "OpenFileGDB" if gdal.GetDriverByName("OpenFileGDB") else "FileGDB"
                if result["driver"]:
                    try:
                        result["capabilities"] = get_driver_capability(result["driver"]).to_dict()
                    except Exception:
                        result["capabilities"] = None
                result["empty"] = True
                result["feature_datasets"] = []
                result["warnings"].append(
                    "Empty File Geodatabase: no user feature classes/tables have been created yet."
                )
                return result
        result["error"] = "GDAL could not open this source as vector data."
        return result

    try:
        drv = ds.GetDriver()
        driver_name = drv.GetDescription() if drv else preferred_driver_for_path(path)
        result["driver"] = driver_name
        result["capabilities"] = get_driver_capability(driver_name).to_dict() if driver_name else None

        engine = _gdb_engine(path) if str(path).lower().endswith(".gdb") else None
        for i in range(ds.GetLayerCount()):
            layer = ds.GetLayerByIndex(i)
            if layer is None:
                continue
            name = str(layer.GetName() or f"layer_{i}")
            geom_type = int(layer.GetLayerDefn().GetGeomType())
            kind = _geom_kind(geom_type)
            internal = False
            if engine is not None:
                try:
                    internal = bool(engine.is_internal_layer(name))
                except Exception:
                    internal = False
            elif name.lower().startswith(SYSTEM_LAYER_PREFIXES):
                internal = True
            if internal and not include_internal:
                continue

            try:
                count = int(layer.GetFeatureCount(0))
                if count < 0:
                    count = int(layer.GetFeatureCount(1))
            except Exception:
                count = -1

            srs = layer.GetSpatialRef()
            sr_meta = _srs_metadata(srs)
            info = {
                "name": name,
                "index": i,
                "kind": kind,
                "geometry_type": _geom_name(geom_type),
                "geometry_type_code": geom_type,
                "has_z": _has_z(geom_type),
                "has_m": _has_m(geom_type),
                "feature_count": count,
                "crs": sr_meta,
                "extent": _safe_extent(layer, force=verify_extents),
                "internal": internal,
            }
            try:
                info["alias"] = layer.GetMetadataItem("ALIAS_NAME") or ""
            except Exception:
                info["alias"] = ""
            if engine is not None:
                try:
                    info["group"] = engine.get_layer_group(name)
                    info["catalog_path"] = engine.get_layer_catalog_path(name)
                    info["cascaded"] = bool(engine.is_cascaded_layer(name))
                    native_wkt = engine.get_layer_crs_wkt(name)
                    if native_wkt and (not info["crs"].get("wkt") or len(native_wkt) > len(info["crs"]["wkt"])):
                        info["native_crs_wkt"] = native_wkt
                except Exception:
                    pass
            if include_fields:
                fields = []
                defn = layer.GetLayerDefn()
                for fi in range(defn.GetFieldCount()):
                    fields.append(_field_info(defn.GetFieldDefn(fi)))
                info["fields"] = fields
            result["layers"].append(info)

        result["domains"] = _domain_summary(ds)
        result["relationships"] = _relationship_summary(ds)
        if engine is not None:
            try:
                result["feature_datasets"] = sorted({
                    v for v in (engine.get_layer_group(x["name"]) for x in result["layers"]) if v
                })
                # engine XML parser may see domains that the installed GDAL binding
                # does not expose through GetFieldDomainNames(). Prefer the richer set.
                engine_domains = list(engine.export_domains_meta() or [])
                if len(engine_domains) > len(result.get("domains", [])):
                    result["domains"] = engine_domains
            except Exception:
                pass
    finally:
        ds = None
    return result


def inspect_raster_source(path: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "path": os.path.abspath(path),
        "kind": "raster",
        "gdal_version": gdal_version(),
        "warnings": [],
    }
    if gdal is None:
        result["error"] = "GDAL Python bindings (osgeo) are not available."
        return result
    try:
        ds = gdal.OpenEx(path, gdal.OF_RASTER | gdal.OF_READONLY)
    except Exception:
        ds = None
    if ds is None:
        result["error"] = "GDAL could not open this source as raster data."
        return result
    try:
        drv = ds.GetDriver()
        driver_name = drv.GetDescription() if drv else preferred_driver_for_path(path)
        result["driver"] = driver_name
        result["capabilities"] = get_driver_capability(driver_name).to_dict() if driver_name else None
        result["width"] = int(ds.RasterXSize)
        result["height"] = int(ds.RasterYSize)
        result["bands"] = int(ds.RasterCount)
        try:
            result["geotransform"] = tuple(ds.GetGeoTransform(can_return_null=True) or ()) or None
        except TypeError:
            try:
                result["geotransform"] = tuple(ds.GetGeoTransform() or ())
            except Exception:
                result["geotransform"] = None
        try:
            srs = ds.GetSpatialRef()
        except Exception:
            srs = None
        if srs is None:
            try:
                wkt = ds.GetProjectionRef()
                srs = osr.SpatialReference(wkt=wkt) if wkt and osr is not None else None
            except Exception:
                srs = None
        result["crs"] = _srs_metadata(srs)
        try:
            result["subdatasets"] = [x[0] for x in (ds.GetSubDatasets() or [])]
        except Exception:
            result["subdatasets"] = []
    finally:
        ds = None
    return result


def inspect_source(path: str, *, include_fields: bool = True,
                   include_internal: bool = False,
                   verify_extents: bool = False) -> Dict[str, Any]:
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return {"path": path, "kind": "missing", "error": "Path does not exist."}
    low = path.lower()
    if low.endswith(".gdb") or low.endswith(".gdb.zip"):
        return inspect_vector_source(path, include_fields=include_fields,
                                     include_internal=include_internal,
                                     verify_extents=verify_extents)
    ext = Path(path).suffix.lower()
    if ext in VECTOR_EXTENSIONS:
        result = inspect_vector_source(path, include_fields=include_fields,
                                       include_internal=include_internal,
                                       verify_extents=verify_extents)
        if not result.get("error"):
            return result
    if ext in RASTER_EXTENSIONS:
        return inspect_raster_source(path)
    # Last chance: ask GDAL which model can open it.
    if gdal is not None:
        try:
            ds = gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_READONLY)
            if ds is not None:
                ds = None
                return inspect_vector_source(path, include_fields=include_fields,
                                             include_internal=include_internal,
                                             verify_extents=verify_extents)
        except Exception:
            pass
        try:
            ds = gdal.OpenEx(path, gdal.OF_RASTER | gdal.OF_READONLY)
            if ds is not None:
                ds = None
                return inspect_raster_source(path)
        except Exception:
            pass
    return {"path": path, "kind": "unknown", "error": "No supported GDAL driver recognized this source."}


def list_folder_items(folder: str) -> List[Dict[str, Any]]:
    out = []
    try:
        names = sorted(os.listdir(folder), key=lambda x: (not os.path.isdir(os.path.join(folder, x)), x.casefold()))
    except Exception:
        return out
    for name in names:
        full = os.path.join(folder, name)
        if os.path.isdir(full):
            is_gdb = name.lower().endswith(".gdb")
            out.append({
                "name": name,
                "path": full,
                "kind": "gdb" if is_gdb else "folder",
                "expandable": True,
            })
            continue
        ext = Path(name).suffix.lower()
        if ext in VECTOR_EXTENSIONS:
            out.append({"name": name, "path": full, "kind": "vector", "expandable": ext in {".gpkg", ".sqlite", ".db"}})
        elif ext in RASTER_EXTENSIONS:
            out.append({"name": name, "path": full, "kind": "raster", "expandable": ext in {".mbtiles"}})
    return out
