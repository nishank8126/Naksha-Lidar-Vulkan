# gui/gis/gdb/__init__.py — ESRI File GDB support + universal catalog bridge
from __future__ import annotations


def open_gdb_in_panel(app, gdb_path: str = None):
    """Open a concrete FileGDB in the dedicated layer picker."""
    import os
    from PySide6.QtWidgets import QFileDialog

    if not gdb_path or not os.path.isdir(gdb_path):
        folder = QFileDialog.getExistingDirectory(app, "Select ESRI File Geodatabase (.gdb)")
        if not folder:
            return False
        gdb_path = folder

    from .gdb_picker import pick_and_import_gdb
    return pick_and_import_gdb(app, gdb_path)


def toggle_gdb_panel(app):
    """Compatibility entry point for the universal GIS Catalog.

    Double-clicking or adding a .gdb from the Catalog still opens the existing
    GDB layer picker, so established FileGDB import behavior is preserved.
    """
    from gui.gis.catalog_browser import toggle_catalog_panel
    return toggle_catalog_panel(app)


def open_domains_dialog(app, gdb_path: str):
    from .domains_dialog import WorkspaceDomainsDialog
    from .engine import get_engine
    engine = get_engine(gdb_path, refresh=True)
    if engine is None:
        raise RuntimeError(f"Could not initialize GDB engine: {gdb_path}")
    dlg = WorkspaceDomainsDialog(engine, parent=app)
    dlg.exec()
    return dlg


def create_file_gdb(path: str, overwrite: bool = False):
    from .writer import create_file_gdb as _create
    return _create(path, overwrite=overwrite)


def create_feature_class(gdb_path: str, name: str, **kwargs):
    from .writer import create_feature_class as _create
    return _create(gdb_path, name, **kwargs)


def create_table(gdb_path: str, name: str, **kwargs):
    from .writer import create_table as _create
    return _create(gdb_path, name, **kwargs)
