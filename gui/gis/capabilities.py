"""Runtime GIS driver capability detection for Naksha.

This module intentionally asks the GDAL build that is actually bundled with the
application what it can do.  The UI should never promise FileGDB/GPKG/ECW/etc.
write or read support solely from a filename extension.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

try:
    from osgeo import gdal, ogr
except Exception:  # pragma: no cover - handled at runtime in packaged app
    gdal = None
    ogr = None


VECTOR_EXTENSIONS = {
    ".shp": "ESRI Shapefile",
    ".geojson": "GeoJSON",
    ".json": "GeoJSON",
    ".gpkg": "GPKG",
    ".kml": "KML",
    ".kmz": "KML",
    ".gpx": "GPX",
    ".csv": "CSV",
    ".gml": "GML",
    ".sqlite": "SQLite",
    ".db": "SQLite",
    ".fgb": "FlatGeobuf",
    ".dbf": "ESRI Shapefile",
}

RASTER_EXTENSIONS = {
    ".tif": "GTiff",
    ".tiff": "GTiff",
    ".btf": "GTiff",
    ".jp2": "JP2OpenJPEG",
    ".j2k": "JP2OpenJPEG",
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".ecw": "ECW",
    ".img": "HFA",
    ".vrt": "VRT",
    ".mbtiles": "MBTiles",
}

CONTAINER_EXTENSIONS = {".gdb", ".gpkg", ".sqlite", ".db", ".mbtiles"}


@dataclass(frozen=True)
class DriverCapability:
    name: str
    installed: bool
    vector: bool = False
    raster: bool = False
    open: bool = False
    create: bool = False
    create_copy: bool = False
    update: bool = False
    multiple_layers: bool = False
    field_domains: bool = False
    relationships: bool = False
    measured_geometries: bool = False
    z_geometries: bool = False
    virtual_io: bool = False
    long_name: str = ""
    help_topic: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _truthy(value) -> bool:
    return str(value or "").upper() in {"YES", "TRUE", "1"}


def gdal_version() -> str:
    if gdal is None:
        return "unavailable"
    try:
        return str(gdal.VersionInfo("RELEASE_NAME") or "unknown")
    except Exception:
        return "unknown"


def _metadata_bool(md: dict, key: str) -> bool:
    return _truthy((md or {}).get(key))


def get_driver_capability(driver_name: str) -> DriverCapability:
    if gdal is None:
        return DriverCapability(driver_name, False)
    try:
        drv = gdal.GetDriverByName(driver_name)
    except Exception:
        drv = None
    if drv is None:
        return DriverCapability(driver_name, False)

    try:
        md = drv.GetMetadata_Dict() or {}
    except Exception:
        try:
            md = drv.GetMetadata() or {}
        except Exception:
            md = {}

    # GDAL metadata keys have been stable for years, but use string fallbacks so
    # this remains compatible with older bundled bindings as well.
    def k(name: str, fallback: str) -> str:
        return getattr(gdal, name, fallback)

    return DriverCapability(
        name=driver_name,
        installed=True,
        vector=_metadata_bool(md, k("DCAP_VECTOR", "DCAP_VECTOR")),
        raster=_metadata_bool(md, k("DCAP_RASTER", "DCAP_RASTER")),
        open=_metadata_bool(md, k("DCAP_OPEN", "DCAP_OPEN")),
        create=_metadata_bool(md, k("DCAP_CREATE", "DCAP_CREATE")),
        create_copy=_metadata_bool(md, k("DCAP_CREATECOPY", "DCAP_CREATECOPY")),
        update=_metadata_bool(md, k("DCAP_UPDATE", "DCAP_UPDATE")),
        multiple_layers=_metadata_bool(md, k("DCAP_MULTIPLE_VECTOR_LAYERS", "DCAP_MULTIPLE_VECTOR_LAYERS")),
        field_domains=_metadata_bool(md, k("DCAP_FIELD_DOMAINS", "DCAP_FIELD_DOMAINS")),
        relationships=_metadata_bool(md, k("DCAP_RELATIONSHIPS", "DCAP_RELATIONSHIPS")),
        measured_geometries=_metadata_bool(md, k("DCAP_MEASURED_GEOMETRIES", "DCAP_MEASURED_GEOMETRIES")),
        z_geometries=_metadata_bool(md, k("DCAP_Z_GEOMETRIES", "DCAP_Z_GEOMETRIES")),
        virtual_io=_metadata_bool(md, k("DCAP_VIRTUALIO", "DCAP_VIRTUALIO")),
        long_name=str(md.get(getattr(gdal, "DMD_LONGNAME", "DMD_LONGNAME"), "") or ""),
        help_topic=str(md.get(getattr(gdal, "DMD_HELPTOPIC", "DMD_HELPTOPIC"), "") or ""),
    )


def priority_capabilities() -> List[DriverCapability]:
    names = [
        "OpenFileGDB", "FileGDB", "GPKG", "ESRI Shapefile", "GeoJSON",
        "KML", "LIBKML", "GPX", "CSV", "GML", "FlatGeobuf",
        "GTiff", "JP2OpenJPEG", "ECW", "HFA", "PNG", "JPEG", "MBTiles",
    ]
    seen = set()
    out = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        out.append(get_driver_capability(name))
    return out


def supported_drop_extensions() -> Tuple[str, ...]:
    """Extensions accepted by Naksha drag/drop without promising driver support."""
    return tuple(sorted(set(VECTOR_EXTENSIONS) | set(RASTER_EXTENSIONS) | {".gdb"}))


def preferred_driver_for_path(path: str) -> Optional[str]:
    p = Path(path)
    low = str(path).lower()
    if low.endswith(".gdb") or low.endswith(".gdb.zip"):
        if get_driver_capability("OpenFileGDB").installed:
            return "OpenFileGDB"
        if get_driver_capability("FileGDB").installed:
            return "FileGDB"
        return None
    ext = p.suffix.lower()
    requested = VECTOR_EXTENSIONS.get(ext) or RASTER_EXTENSIONS.get(ext)
    if requested == "KML":
        if get_driver_capability("LIBKML").installed:
            return "LIBKML"
    return requested


def runtime_summary() -> Dict[str, object]:
    return {
        "gdal_version": gdal_version(),
        "drivers": [cap.to_dict() for cap in priority_capabilities()],
    }


def capability_text(cap: DriverCapability) -> str:
    if not cap.installed:
        return "not installed"
    flags = []
    if cap.open:
        flags.append("read")
    if cap.create:
        flags.append("create")
    if cap.update:
        flags.append("update")
    if cap.vector:
        flags.append("vector")
    if cap.raster:
        flags.append("raster")
    return ", ".join(flags) or "installed"
