"""Creation backend for standalone vector GIS datasets."""
from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

try:
    from osgeo import ogr, osr
except Exception:
    ogr = None
    osr = None

from .gdb.writer import FieldSpec


@dataclass(frozen=True)
class VectorFormat:
    key: str
    label: str
    driver: str
    extension: str


@dataclass
class VectorCreateResult:
    success: bool
    path: str
    layer_name: str = ""
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


VECTOR_FORMATS = {
    "shapefile": VectorFormat("shapefile", "ESRI Shapefile", "ESRI Shapefile", ".shp"),
    "geojson": VectorFormat("geojson", "GeoJSON", "GeoJSON", ".geojson"),
    "geopackage": VectorFormat("geopackage", "GeoPackage", "GPKG", ".gpkg"),
    "flatgeobuf": VectorFormat("flatgeobuf", "FlatGeobuf", "FlatGeobuf", ".fgb"),
    "dbase": VectorFormat("dbase", "dBASE Table", "ESRI Shapefile", ".dbf"),
}

_GEOMETRY_TYPES = {
    "point": "wkbPoint",
    "multipoint": "wkbMultiPoint",
    "polyline": "wkbLineString",
    "linestring": "wkbLineString",
    "multilinestring": "wkbMultiLineString",
    "polygon": "wkbPolygon",
    "multipolygon": "wkbMultiPolygon",
    "none": "wkbNone",
}

def schema_sidecar_path(path: str) -> str:
    """Private schema metadata for formats (GeoJSON) that cannot store an empty schema."""
    return os.path.abspath(str(path)) + ".naksha-schema"


def load_vector_schema(path: str) -> dict:
    sidecar = schema_sidecar_path(path)
    try:
        with open(sidecar, "r", encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_vector_schema(path: str, layer_name: str, geometry: str, crs, fields) -> None:
    payload = {
        "version": 1,
        "layer_name": str(layer_name),
        "geometry": str(geometry),
        "crs_wkt": crs.to_wkt() if hasattr(crs, "to_wkt") else str(crs or ""),
        "fields": [
            {
                "name": str(spec.name), "type": str(spec.type),
                "width": int(spec.width or 0), "precision": int(spec.precision or 0),
                "nullable": bool(spec.nullable), "default": spec.default,
                "domain": str(spec.domain or ""), "alias": str(spec.alias or ""),
            }
            for spec in list(fields or []) if str(spec.name or "").strip()
        ],
    }
    with open(schema_sidecar_path(path), "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)

_FIELD_TYPES = {
    "string": "OFTString",
    "integer": "OFTInteger",
    "integer64": "OFTInteger64",
    "real": "OFTReal",
    "date": "OFTDate",
    "datetime": "OFTDateTime",
}


def available_vector_formats() -> list[VectorFormat]:
    if ogr is None:
        return []
    return [fmt for fmt in VECTOR_FORMATS.values() if ogr.GetDriverByName(fmt.driver) is not None]


def _spatial_ref(crs):
    if crs is None:
        raise ValueError("Select a coordinate reference system.")
    text = crs.to_wkt() if hasattr(crs, "to_wkt") else str(crs)
    srs = osr.SpatialReference()
    if srs.ImportFromWkt(text) != 0:
        if srs.SetFromUserInput(text) != 0:
            raise ValueError("The selected coordinate reference system could not be parsed.")
    try:
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except Exception:
        pass
    return srs


def _field_definition(spec: FieldSpec):
    type_name = _FIELD_TYPES.get(str(spec.type or "String").casefold(), "OFTString")
    definition = ogr.FieldDefn(spec.name, getattr(ogr, type_name))
    if spec.width:
        definition.SetWidth(max(0, int(spec.width)))
    if spec.precision and hasattr(definition, "SetPrecision"):
        definition.SetPrecision(max(0, int(spec.precision)))
    return definition


def create_vector_dataset(
    path: str,
    *,
    format_key: str,
    layer_name: str,
    geometry: str,
    crs,
    fields: Sequence[FieldSpec] | None = None,
) -> VectorCreateResult:
    fmt = VECTOR_FORMATS.get(str(format_key).casefold())
    if fmt is None:
        return VectorCreateResult(False, path, error=f"Unsupported vector format: {format_key}")
    if ogr is None or osr is None:
        return VectorCreateResult(False, path, error="GDAL/OGR Python bindings are required.")

    target = os.path.abspath(path)
    if not target.casefold().endswith(fmt.extension):
        target += fmt.extension
    if os.path.exists(target):
        return VectorCreateResult(False, target, error="A dataset with this name already exists.")

    driver = ogr.GetDriverByName(fmt.driver)
    if driver is None:
        return VectorCreateResult(False, target, error=f"The {fmt.label} driver is not installed.")

    geom_name = str(geometry or "Polygon").replace(" ", "").casefold()
    ogr_geom = getattr(ogr, _GEOMETRY_TYPES.get(geom_name, "wkbPolygon"))
    spatial_ref = None if geom_name == "none" else _spatial_ref(crs)
    safe_layer_name = str(layer_name or Path(target).stem).strip()
    warnings = []
    datasource = None
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        datasource = driver.CreateDataSource(target)
        if datasource is None:
            raise RuntimeError(f"GDAL could not create the {fmt.label} dataset.")
        options = ["ENCODING=UTF-8"] if fmt.key == "shapefile" else []
        layer = datasource.CreateLayer(
            safe_layer_name,
            srs=spatial_ref,
            geom_type=ogr_geom,
            options=options,
        )
        if layer is None:
            raise RuntimeError("GDAL could not create the vector layer.")
        for spec in fields or []:
            if not str(spec.name or "").strip():
                continue
            if layer.CreateField(_field_definition(spec)) != 0:
                warnings.append(f"Field '{spec.name}' could not be created.")
        try:
            layer.SyncToDisk()
            datasource.FlushCache()
        except Exception:
            pass
        # Shapefile derives its public OGR layer name from the filename.
        actual_layer_name = str(layer.GetName() or safe_layer_name)
        layer = None
        datasource = None
        if fmt.key == "geojson":
            _write_vector_schema(target, actual_layer_name, geometry, crs, fields)
        return VectorCreateResult(True, target, actual_layer_name, warnings)
    except Exception as exc:
        datasource = None
        try:
            if os.path.exists(target):
                driver.DeleteDataSource(target)
            sidecar = schema_sidecar_path(target)
            if os.path.exists(sidecar):
                os.remove(sidecar)
        except Exception:
            pass
        return VectorCreateResult(False, target, safe_layer_name, warnings, str(exc))