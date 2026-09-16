"""Creation helpers for non-FileGDB GIS containers."""
from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from osgeo import gdal
except Exception:
    gdal = None


@dataclass
class ContainerResult:
    success: bool
    path: str
    error: str | None = None


def create_geopackage(path: str, overwrite: bool = False) -> ContainerResult:
    if gdal is None:
        return ContainerResult(False, path, "GDAL Python bindings are required.")
    if not path.lower().endswith(".gpkg"):
        path += ".gpkg"
    path = os.path.abspath(path)
    if os.path.exists(path):
        if not overwrite:
            return ContainerResult(False, path, "A GeoPackage with this name already exists.")
        try:
            os.remove(path)
        except Exception as exc:
            return ContainerResult(False, path, str(exc))
    drv = gdal.GetDriverByName("GPKG")
    if drv is None:
        return ContainerResult(False, path, "GDAL GPKG driver is not installed.")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ds = None
    try:
        if hasattr(drv, "CreateVector"):
            try:
                ds = drv.CreateVector(path)
            except Exception:
                ds = None
        if ds is None:
            ds = drv.Create(path, 0, 0, 0, gdal.GDT_Unknown)
        if ds is None:
            return ContainerResult(False, path, "GDAL could not create the GeoPackage.")
        ds = None
        return ContainerResult(True, path)
    except Exception as exc:
        return ContainerResult(False, path, str(exc))
    finally:
        ds = None
