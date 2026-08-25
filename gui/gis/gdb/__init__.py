# gui/gis/gdb/__init__.py — ESRI File GDB support
#
# Public API:
#   open_gdb_in_panel(app, gdb_path)  — show the GDB layer picker dialog
#   toggle_gdb_panel(app)             — alias (same as open, for shortcut compat)
#   open_domains_dialog(app, gdb_path)— open the Workspace Domains editor

from __future__ import annotations


def open_gdb_in_panel(app, gdb_path: str = None):
    """Show the GDB layer picker dialog for the given .gdb folder.
    If *gdb_path* is None, opens a folder browser first."""
    import os
    from PySide6.QtWidgets import QFileDialog

    if not gdb_path or not os.path.isdir(gdb_path):
        folder = QFileDialog.getExistingDirectory(
            app, "Select ESRI File Geodatabase (.gdb)")
        if not folder:
            return False
        gdb_path = folder

    from .gdb_picker import pick_and_import_gdb
    return pick_and_import_gdb(app, gdb_path)


def toggle_gdb_panel(app):
    """Open the GDB layer picker (same as open_gdb_in_panel)."""
    return open_gdb_in_panel(app)


def open_domains_dialog(app, gdb_path: str):
    """Open the Workspace Domains editor for a GDB."""
    from .domains_dialog import WorkspaceDomainsDialog
    dlg = WorkspaceDomainsDialog(app, gdb_path)
    dlg.exec()
