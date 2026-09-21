"""FileGDB creation and schema-write helpers for Naksha.

Targets the built-in GDAL OpenFileGDB writer (GDAL >= 3.6).  The functions are
kept UI-independent so ArcCatalog-like wizards, batch conversion and automated
tests can all use the same backend.
"""
from __future__ import annotations

import os
import re
import shutil
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from osgeo import gdal, ogr, osr
except Exception:
    gdal = None
    ogr = None
    osr = None


@dataclass
class FieldSpec:
    name: str
    type: str = "String"
    width: int = 0
    precision: int = 0
    nullable: bool = True
    default: Any = None
    domain: str = ""
    alias: str = ""


@dataclass
class WriteResult:
    success: bool
    path: str = ""
    name: str = ""
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None
    pending: bool = False

    def to_dict(self):
        return asdict(self)


FIELD_TYPES = {
    "smallinteger": "OFTInteger",
    "short": "OFTInteger",
    "integer": "OFTInteger",
    "long": "OFTInteger",
    "longinteger": "OFTInteger",
    "integer64": "OFTInteger64",
    "biginteger": "OFTInteger64",
    "single": "OFTReal",
    "float": "OFTReal",
    "double": "OFTReal",
    "real": "OFTReal",
    "string": "OFTString",
    "text": "OFTString",
    "date": "OFTDateTime",
    "datetime": "OFTDateTime",
    "dateonly": "OFTDate",
    "timeonly": "OFTTime",
    "binary": "OFTBinary",
    "guid": "OFTString",
    "globalid": "OFTString",
}

GEOMETRY_TYPES = {
    "point": "wkbPoint",
    "multipoint": "wkbMultiPoint",
    "polyline": "wkbLineString",
    "line": "wkbLineString",
    "linestring": "wkbLineString",
    "multiline": "wkbMultiLineString",
    "multilinestring": "wkbMultiLineString",
    "polygon": "wkbPolygon",
    "multipolygon": "wkbMultiPolygon",
    "none": "wkbNone",
    "table": "wkbNone",
}


def _require():
    if gdal is None or ogr is None:
        raise RuntimeError("GDAL Python bindings (osgeo) are required for FileGDB creation.")


def _driver():
    _require()
    drv = gdal.GetDriverByName("OpenFileGDB") or gdal.GetDriverByName("FileGDB")
    if drv is None:
        raise RuntimeError("Neither OpenFileGDB nor FileGDB GDAL driver is installed.")
    return drv


def _version_tuple():
    _require()
    try:
        parts = str(gdal.VersionInfo("RELEASE_NAME")).split(".")
        return tuple(int(x) for x in parts[:3])
    except Exception:
        return (0, 0, 0)


def ensure_write_capable():
    ver = _version_tuple()
    if ver < (3, 6, 0):
        raise RuntimeError(f"FileGDB creation requires GDAL >= 3.6. Installed: {gdal.VersionInfo('RELEASE_NAME')}")
    return True


def sanitize_name(name: str, max_len: int = 160) -> str:
    raw = str(name or "").strip()
    clean = re.sub(r"[^A-Za-z0-9_]", "_", raw)
    clean = re.sub(r"_+", "_", clean).strip("_")
    if not clean:
        clean = "item"
    if clean[0].isdigit():
        clean = "N_" + clean
    return clean[:max_len]


def _open_update(path: str):
    _require()
    drivers = [x for x in ("OpenFileGDB", "FileGDB") if gdal.GetDriverByName(x)]
    try:
        ds = gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_UPDATE, allowed_drivers=drivers or None)
    except TypeError:
        ds = gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_UPDATE)
    if ds is None:
        raise RuntimeError("FileGDB could not be opened for update. Check locks and driver capabilities.")
    return ds


def _spatial_ref(value):
    if value in (None, "") or osr is None:
        return None
    if hasattr(value, "ExportToWkt"):
        try:
            return value.Clone()
        except Exception:
            return value
    srs = osr.SpatialReference()
    text = str(value).strip()
    if isinstance(value, int) or text.isdigit():
        if srs.ImportFromEPSG(int(value)) != 0:
            raise ValueError(f"Invalid EPSG: {value}")
        return srs
    if text.upper().startswith("EPSG:"):
        if srs.ImportFromEPSG(int(text.split(":", 1)[1])) != 0:
            raise ValueError(f"Invalid CRS: {value}")
        return srs
    if hasattr(srs, "SetFromUserInput") and srs.SetFromUserInput(text) == 0:
        return srs
    if srs.ImportFromWkt(text) == 0:
        return srs
    raise ValueError(f"Could not parse CRS: {value}")


def _geom_type(name: str, has_z: bool = False, has_m: bool = False):
    _require()
    attr = GEOMETRY_TYPES.get(str(name or "").replace(" ", "").lower(), "wkbUnknown")
    gt = getattr(ogr, attr, ogr.wkbUnknown)
    if has_z:
        try:
            gt = ogr.GT_SetZ(gt)
        except Exception:
            gt = gt | getattr(ogr, "wkb25DBit", 0)
    if has_m:
        try:
            gt = ogr.GT_SetM(gt)
        except Exception:
            gt = gt | getattr(ogr, "wkbMBit", 0)
    return gt


def _field_type(name: str):
    _require()
    attr = FIELD_TYPES.get(str(name or "String").replace(" ", "").lower(), "OFTString")
    return getattr(ogr, attr, ogr.OFTString)


def _make_field(spec: FieldSpec):
    fd = ogr.FieldDefn(sanitize_name(spec.name, 64), _field_type(spec.type))
    if spec.width:
        try:
            fd.SetWidth(int(spec.width))
        except Exception:
            pass
    if spec.precision:
        try:
            fd.SetPrecision(int(spec.precision))
        except Exception:
            pass
    try:
        fd.SetNullable(bool(spec.nullable))
    except Exception:
        pass
    if spec.default not in (None, ""):
        try:
            fd.SetDefault(str(spec.default))
        except Exception:
            pass
    if spec.domain:
        try:
            fd.SetDomainName(str(spec.domain))
        except Exception:
            pass
    if spec.alias:
        for method in ("SetAlternativeName", "SetAlternativeNameRef"):
            try:
                getattr(fd, method)(str(spec.alias))
                break
            except Exception:
                pass
    return fd


def create_file_gdb(path: str, *, overwrite: bool = False) -> WriteResult:
    try:
        ensure_write_capable()
        if not str(path).lower().endswith(".gdb"):
            path = str(path) + ".gdb"
        path = os.path.abspath(path)
        if os.path.exists(path):
            if not overwrite:
                return WriteResult(False, path=path, error="A geodatabase with this name already exists.")
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        drv = _driver()
        ds = None
        if hasattr(drv, "CreateVector"):
            try:
                ds = drv.CreateVector(path)
            except Exception:
                ds = None
        if ds is None:
            ds = drv.Create(path, 0, 0, 0, gdal.GDT_Unknown)
        if ds is None:
            return WriteResult(False, path=path, error="GDAL could not create the FileGDB.")
        ds = None
        return WriteResult(True, path=path, name=os.path.basename(path))
    except Exception as exc:
        return WriteResult(False, path=str(path), error=str(exc))


def create_feature_class(gdb_path: str, name: str, *, geometry: str = "Polygon",
                         crs=None, feature_dataset: Optional[str] = None,
                         alias: Optional[str] = None,
                         has_z: bool = False, has_m: bool = False,
                         fields: Optional[Sequence[FieldSpec | dict]] = None,
                         target_arcgis_version: str = "ALL",
                         create_shape_fields: bool = True,
                         geometry_nullable: bool = True,
                         xy_tolerance: Optional[float] = None,
                         z_tolerance: Optional[float] = None,
                         m_tolerance: Optional[float] = None) -> WriteResult:
    warnings: List[str] = []
    try:
        ensure_write_capable()
        ds = _open_update(gdb_path)
        transaction = False
        try:
            transaction = (ds.StartTransaction(force=True) == 0)
        except Exception:
            transaction = False
        try:
            layer_name = sanitize_name(name, 160)
            if ds.GetLayerByName(layer_name) is not None:
                raise RuntimeError(f"Layer '{layer_name}' already exists.")
            opts = [f"TARGET_ARCGIS_VERSION={target_arcgis_version or 'ALL'}"]
            if feature_dataset:
                opts.append(f"FEATURE_DATASET={sanitize_name(feature_dataset, 160)}")
            if alias:
                opts.append(f"LAYER_ALIAS={alias}")
            opts.append(f"GEOMETRY_NULLABLE={'YES' if geometry_nullable else 'NO'}")
            if create_shape_fields:
                opts.append("CREATE_SHAPE_AREA_AND_LENGTH_FIELDS=YES")
            if xy_tolerance is not None:
                opts.append(f"XYTOLERANCE={float(xy_tolerance)}")
            if z_tolerance is not None:
                opts.append(f"ZTOLERANCE={float(z_tolerance)}")
            if m_tolerance is not None:
                opts.append(f"MTOLERANCE={float(m_tolerance)}")
            layer = ds.CreateLayer(layer_name, srs=_spatial_ref(crs),
                                   geom_type=_geom_type(geometry, has_z, has_m),
                                   options=opts)
            if layer is None:
                raise RuntimeError(f"Could not create feature class '{layer_name}'.")
            for raw in list(fields or []):
                spec = raw if isinstance(raw, FieldSpec) else FieldSpec(**raw)
                if layer.CreateField(_make_field(spec)) != 0:
                    warnings.append(f"Field '{spec.name}' could not be created.")
            try:
                layer.SyncToDisk()
            except Exception:
                pass
            if transaction and ds.CommitTransaction() != 0:
                raise RuntimeError("Could not commit FileGDB schema transaction.")
            transaction = False
            try:
                ds.FlushCache()
            except Exception:
                pass
            if feature_dataset:
                try:
                    from .pending import clear_pending
                    clear_pending(gdb_path, sanitize_name(feature_dataset, 160))
                except Exception:
                    pass
            return WriteResult(True, path=gdb_path, name=layer_name, warnings=warnings)
        except Exception:
            if transaction:
                try:
                    ds.RollbackTransaction()
                except Exception:
                    pass
            raise
        finally:
            ds = None
    except Exception as exc:
        return WriteResult(False, path=gdb_path, name=name, warnings=warnings, error=str(exc))


def create_table(gdb_path: str, name: str, *, alias: Optional[str] = None,
                 fields: Optional[Sequence[FieldSpec | dict]] = None,
                 target_arcgis_version: str = "ALL") -> WriteResult:
    warnings: List[str] = []
    try:
        ds = _open_update(gdb_path)
        table_name = sanitize_name(name, 160)
        if ds.GetLayerByName(table_name) is not None:
            return WriteResult(False, gdb_path, table_name, error="Table already exists.")
        opts = [f"TARGET_ARCGIS_VERSION={target_arcgis_version or 'ALL'}"]
        if alias:
            opts.append(f"LAYER_ALIAS={alias}")
        layer = ds.CreateLayer(table_name, srs=None, geom_type=ogr.wkbNone, options=opts)
        if layer is None:
            return WriteResult(False, gdb_path, table_name, error="Could not create table.")
        for raw in list(fields or []):
            spec = raw if isinstance(raw, FieldSpec) else FieldSpec(**raw)
            if layer.CreateField(_make_field(spec)) != 0:
                warnings.append(f"Field '{spec.name}' could not be created.")
        try:
            ds.FlushCache()
        except Exception:
            pass
        ds = None
        return WriteResult(True, gdb_path, table_name, warnings=warnings)
    except Exception as exc:
        return WriteResult(False, gdb_path, name, warnings=warnings, error=str(exc))


def create_feature_dataset(gdb_path: str, name: str, *, crs=None) -> WriteResult:
    """Create an empty FileGDB feature dataset when supported by the driver.

    OpenFileGDB materializes feature datasets through the FEATURE_DATASET layer
    creation option.  To support ArcCatalog's 'empty Feature Dataset' workflow,
    Naksha creates a temporary point feature class inside the dataset, deletes it,
    and then verifies that the hierarchy still exposes the feature dataset.
    If the installed GDAL build removes the now-empty group, the function returns
    a clear warning instead of pretending parity.
    """
    warnings: List[str] = []
    fd_name = sanitize_name(name, 160)
    temp_name = f"__NAKSHA_FD_INIT_{uuid.uuid4().hex[:10]}"
    try:
        ds = _open_update(gdb_path)
        layer = ds.CreateLayer(temp_name, srs=_spatial_ref(crs), geom_type=ogr.wkbPoint,
                               options=[f"FEATURE_DATASET={fd_name}"])
        if layer is None:
            ds = None
            return WriteResult(False, gdb_path, fd_name, error="GDAL could not create the feature dataset container.")
        layer = None
        # Delete the temporary class. Prefer name-based DeleteLayer in newer bindings.
        deleted = False
        try:
            deleted = (ds.DeleteLayer(temp_name) == 0)
        except Exception:
            for i in range(ds.GetLayerCount()):
                lyr = ds.GetLayerByIndex(i)
                if lyr is not None and lyr.GetName() == temp_name:
                    deleted = (ds.DeleteLayer(i) == 0)
                    break
        try:
            ds.FlushCache()
        except Exception:
            pass
        ds = None

        # Verify hierarchy when the Python binding exposes GDALGroup.
        persisted = None
        try:
            check = _open_update(gdb_path)
            root = check.GetRootGroup() if hasattr(check, "GetRootGroup") else None
            if root is not None:
                persisted = fd_name in set(root.GetGroupNames() or [])
            check = None
        except Exception:
            persisted = None

        if not deleted:
            warnings.append(f"Temporary initializer layer '{temp_name}' could not be removed automatically.")
        if persisted is False:
            warnings.append(
                "This GDAL/OpenFileGDB build does not persist an empty Feature Dataset. "
                "Naksha will show it as PENDING for this session and materialize it when the first feature class is created."
            )
            try:
                from .pending import register_pending
                register_pending(gdb_path, fd_name, crs)
            except Exception:
                pass
            return WriteResult(True, gdb_path, fd_name, warnings=warnings, pending=True)
        if persisted is None:
            warnings.append("Feature dataset was created through OpenFileGDB, but this GDAL binding cannot verify empty-group persistence.")
        try:
            from .pending import clear_pending
            clear_pending(gdb_path, fd_name)
        except Exception:
            pass
        return WriteResult(True, gdb_path, fd_name, warnings=warnings)
    except Exception as exc:
        return WriteResult(False, gdb_path, fd_name, warnings=warnings, error=str(exc))


def recompute_extent(gdb_path: str, layer_name: str) -> WriteResult:
    try:
        ds = _open_update(gdb_path)
        sql = f"RECOMPUTE EXTENT ON {layer_name}"
        result = ds.ExecuteSQL(sql)
        if result is not None:
            try:
                ds.ReleaseResultSet(result)
            except Exception:
                pass
        try:
            ds.FlushCache()
        except Exception:
            pass
        ds = None
        return WriteResult(True, gdb_path, layer_name)
    except Exception as exc:
        return WriteResult(False, gdb_path, layer_name, error=str(exc))


def repack(gdb_path: str) -> WriteResult:
    try:
        ds = _open_update(gdb_path)
        result = ds.ExecuteSQL("REPACK")
        if result is not None:
            try:
                ds.ReleaseResultSet(result)
            except Exception:
                pass
        ds = None
        return WriteResult(True, gdb_path, "REPACK")
    except Exception as exc:
        return WriteResult(False, gdb_path, "REPACK", error=str(exc))
