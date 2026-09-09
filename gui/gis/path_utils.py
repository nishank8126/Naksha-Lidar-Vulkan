"""Filesystem path helpers shared by GIS importers."""

from __future__ import annotations

import os


def normalize_gis_path(path) -> str:
    """Canonicalize Qt's forward-slash UNC spelling for Windows GIS drivers.

    A drop event can provide ``//server/share/dataset``. GDAL-family readers
    may parse it as a URL and discard ``server``; their Windows-safe spelling
    is ``\\server\share\dataset``.
    """
    value = os.fspath(path)
    if os.name == "nt" and value.startswith("//") and not value.startswith("///"):
        return "\\\\" + value[2:].replace("/", "\\")
    return value

