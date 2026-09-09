"""Universal GIS package initialization.

The wheel builds of GDAL/OGR and Rasterio bundle separate PROJ databases.  On
Windows they share the process-wide PROJ runtime, so whichever library imports
first can otherwise point the other at an incompatible database layout.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path


def _configure_proj_database() -> str | None:
    """Choose one process-wide PROJ database before importing GDAL or Rasterio."""
    configured = os.environ.get("PROJ_DATA") or os.environ.get("PROJ_LIB")
    if configured and (Path(configured) / "proj.db").is_file():
        os.environ.setdefault("PROJ_DATA", configured)
        os.environ.setdefault("PROJ_LIB", configured)
        return configured

    try:
        spec = importlib.util.find_spec("rasterio")
        package_dir = Path(spec.origin).resolve().parent if spec and spec.origin else None
        candidate = package_dir / "proj_data" if package_dir else None
    except Exception:
        candidate = None

    if candidate is not None and (candidate / "proj.db").is_file():
        value = str(candidate)
        os.environ["PROJ_DATA"] = value
        os.environ["PROJ_LIB"] = value
        return value
    return None


PROJ_DATABASE_PATH = _configure_proj_database()

from gui.gis.gdb import open_gdb_in_panel, toggle_gdb_panel, open_domains_dialog


def show_catalog_panel(app, folder=None):
    from gui.gis.catalog_browser import show_catalog_panel as _show
    return _show(app, folder=folder)


def toggle_catalog_panel(app):
    from gui.gis.catalog_browser import toggle_catalog_panel as _toggle
    return _toggle(app)
