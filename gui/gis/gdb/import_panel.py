# ─────────────────────────────────────────────────────────────────────────────
# gui/gis/gdb/import_panel.py — PySide6 dock for browsing + importing GDBs
#
# Mirrors the look/feel of gui/gis/gis_layers.py:
#   - QDockWidget, theme-aware stylesheet (dark + light)
#   - standalone top-level window when floated (own taskbar button)
#   - same icon set (Lucide via gui.gis.icons)
#
# Layout:
#   [Open GDB]   [path/folder line]   [Import selected]
#   [Tree: one row per feature class]
#      • checkbox  • icon (point/line/polygon)  • name  • type  • n features
#      • cascade badge  • non-nullable warning
#   [Workspace Domains…]   [Create Features…]   [Reload]   [Read-only?]
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QPen
from PySide6.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTreeWidget, QTreeWidgetItem, QFileDialog, QMessageBox, QSizePolicy,
    QAbstractItemView, QMenu, QToolButton, QLineEdit, QCheckBox,
    QHeaderView,
)

from .engine import (
    get_engine, list_layer_names, has_filegdb_driver, has_openfilegdb_driver,
    check_gdal_version,
)
from . import reader as gdb_reader


# ─────────────────────────────────────────────────────────────────────────────
#  Theme / icons  (shareable with the existing OCC stylesheet)
# ─────────────────────────────────────────────────────────────────────────────

def _occ_colors():
    try:
        from gui.theme_manager import ThemeColors as C
        def g(k, fb):
            try:
                v = C.get(k)
                return v or fb
            except Exception:
                return fb
        light = False
        try:
            light = bool(C.is_light())
        except Exception:
            light = False
        return {
            "panel": g("bg_secondary", "#1b1d23"),
            "surface": g("bg_primary", "#16181d"),
            "card_hover": g("bg_button", "#21242c"),
            "btn": g("bg_button", "#262a33"),
            "btn_hover": g("bg_button_hover", "#313641"),
            "text": g("text_primary", "#e8eaed"),
            "muted": g("text_muted", "#7f8794"),
            "secondary": g("text_secondary", "#aeb4bf"),
            "border": g("border_light", "#2a2d35"),
            "accent": g("accent", "#3b5bdb"),
            "accent_hover": g("accent_hover", "#4c6ef5"),
            "danger": g("danger", "#d32f2f"),
            "on_accent": "#ffffff",
            "warn": "#f5a623" if not light else "#d97706",
            "is_light": light,
        }
    except Exception:
        return {
            "panel": "#1b1d23", "surface": "#16181d", "card_hover": "#21242c",
            "btn": "#262a33", "btn_hover": "#313641", "text": "#e8eaed",
            "muted": "#7f8794", "secondary": "#aeb4bf", "border": "#2a2d35",
            "accent": "#3b5bdb", "accent_hover": "#4c6ef5",
            "danger": "#d32f2f", "on_accent": "#ffffff",
            "warn": "#f5a623", "is_light": False,
        }


_UI_FONT = '"Segoe UI", "Inter", "Roboto", Arial, sans-serif'


def _glyph(name: str, color_hex: str, size: int = 16) -> QPixmap:
    dpr = 2
    pm = QPixmap(size * dpr, size * dpr)
    pm.fill(Qt.transparent)
    pm.setDevicePixelRatio(dpr)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.scale(dpr, dpr)
    col = QColor(color_hex)
    sw = max(1.3, size * 0.083)
    pen = QPen(col, sw)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    s = size

    def line(x1, y1, x2, y2):
        from PySide6.QtCore import QPointF
        p.drawLine(QPointF(s * x1, s * y1), QPointF(s * x2, s * y2))

    def poly(pts):
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPainterPath
        path = QPainterPath()
        path.moveTo(s * pts[0][0], s * pts[0][1])
        for x, y in pts[1:]:
            path.lineTo(s * x, s * y)
        p.drawPath(path)

    if name == "gdb":
        # Database/cylinder: a top ellipse + two vertical sides + a curved bottom.
        from PySide6.QtCore import QRectF
        p.drawEllipse(QRectF(s * 0.18, s * 0.20, s * 0.64, s * 0.18))
        line(0.18, 0.29, 0.18, 0.74)
        line(0.82, 0.29, 0.82, 0.74)
        # Two "shelf" lines for the cylinder body
        p.drawArc(QRectF(s * 0.18, s * 0.55, s * 0.64, s * 0.36), 0, -180 * 16)
    elif name == "import":
        line(0.50, 0.18, 0.50, 0.62)
        poly([(0.34, 0.46), (0.50, 0.62), (0.66, 0.46)])
        line(0.18, 0.78, 0.82, 0.78)
    elif name == "point":
        from PySide6.QtCore import QPointF
        p.setBrush(col)
        p.drawEllipse(QPointF(s * 0.5, s * 0.5), s * 0.18, s * 0.18)
    elif name == "line":
        poly([(0.18, 0.78), (0.50, 0.40), (0.82, 0.78)])
    elif name == "polygon":
        from PySide6.QtCore import QRectF
        p.drawRoundedRect(QRectF(s * 0.20, s * 0.26, s * 0.60, s * 0.50),
                          s * 0.06, s * 0.06)
    elif name == "table":
        from PySide6.QtCore import QRectF
        p.drawRect(QRectF(s * 0.18, s * 0.28, s * 0.64, s * 0.44))
        line(0.18, 0.42, 0.82, 0.42)
        line(0.18, 0.56, 0.82, 0.56)
        line(0.18, 0.70, 0.82, 0.70)
    elif name == "cascade":
        # Linked-rings icon for cascaded subtype.
        from PySide6.QtCore import QRectF
        p.drawEllipse(QRectF(s * 0.20, s * 0.36, s * 0.30, s * 0.30))
        p.drawEllipse(QRectF(s * 0.50, s * 0.36, s * 0.30, s * 0.30))
    elif name == "warn":
        from PySide6.QtCore import QPointF
        poly([(0.50, 0.18), (0.86, 0.82), (0.14, 0.82)])
        line(0.50, 0.42, 0.50, 0.62)
        # Dot at the bottom of the !
        p.setBrush(col)
        p.drawEllipse(QPointF(s * 0.50, s * 0.72), s * 0.05, s * 0.05)
    elif name == "refresh":
        # A circular arrow.
        from PySide6.QtGui import QPainterPath
        from PySide6.QtCore import QRectF
        path = QPainterPath()
        path.arcMoveTo(QRectF(s * 0.16, s * 0.16, s * 0.68, s * 0.68), 30)
        path.arcTo(QRectF(s * 0.16, s * 0.16, s * 0.68, s * 0.68), 30, 300)
        p.drawPath(path)
        # Arrow head
        poly([(0.66, 0.18), (0.84, 0.16), (0.82, 0.34)])
    p.end()
    return pm


def _icon(name: str, color_hex: str, size: int = 16) -> QIcon:
    return QIcon(_glyph(name, color_hex, size))


# ─────────────────────────────────────────────────────────────────────────────
#  Stylesheet
# ─────────────────────────────────────────────────────────────────────────────

def _build_panel_style() -> str:
    c = _occ_colors()
    return f"""
QDockWidget#GdbImportDock {{ color:{c['text']}; font-family:{_UI_FONT}; font-size:10pt; }}
QDockWidget#GdbImportDock::title {{
    background:{c['surface']}; color:{c['text']}; padding:9px 12px;
    text-align:left; border-bottom:1px solid {c['border']};
}}
#GdbImportPanel {{ background:{c['panel']}; font-family:{_UI_FONT}; font-size:9pt; }}
#GdbImportPanel QLabel {{ background:transparent; font-family:{_UI_FONT}; }}
#GdbImportPanel QLabel#gdbTitle {{ color:{c['text']}; font-size:12pt; font-weight:600; }}
#GdbImportPanel QLabel#gdbPath {{ color:{c['secondary']}; font-size:8pt; }}
#GdbImportPanel QLabel#gdbHint {{ color:{c['muted']}; font-size:8pt; }}
#GdbImportPanel QLabel#gdbWarn {{ color:{c['warn']}; font-size:8pt; font-weight:600; }}
#GdbImportPanel QPushButton#gdbOpen, #GdbImportPanel QPushButton#gdbImport {{
    background:{c['accent']}; color:{c['on_accent']}; border:none; border-radius:6px;
    padding:7px 12px; font-size:9pt; font-weight:600;
}}
#GdbImportPanel QPushButton#gdbOpen:hover, #GdbImportPanel QPushButton#gdbImport:hover
    {{ background:{c['accent_hover']}; }}
#GdbImportPanel QPushButton#gdbSecondary {{
    background:{c['btn']}; color:{c['text']}; border:none; border-radius:6px;
    padding:6px 10px; font-size:9pt;
}}
#GdbImportPanel QPushButton#gdbSecondary:hover {{ background:{c['btn_hover']}; }}
#GdbImportPanel QLineEdit#gdbFilter {{
    background:{c['surface']}; color:{c['text']}; border:1px solid {c['border']};
    border-radius:6px; padding:4px 8px; font-size:9pt;
}}
#GdbImportPanel QLineEdit#gdbFilter:focus {{ border:1px solid {c['accent']}; }}
#GdbImportPanel QTreeWidget#gdbTree {{
    background:{c['surface']}; border:1px solid {c['border']}; border-radius:8px;
    padding:3px; outline:0;
}}
#GdbImportPanel QTreeWidget#gdbTree::item {{
    padding:3px 1px; border-radius:4px; min-height:22px;
}}
#GdbImportPanel QTreeWidget#gdbTree::item:hover {{ background:{c['card_hover']}; }}
#GdbImportPanel QTreeWidget#gdbTree::item:selected {{ background:{c['accent']}; color:{c['on_accent']}; }}
#GdbImportPanel QTreeWidget#gdbTree::indicator {{ width:16px; height:16px; }}
"""


# ─────────────────────────────────────────────────────────────────────────────
#  The dock
# ─────────────────────────────────────────────────────────────────────────────

class GdbImportDock:
    """Lazy factory + holder for the GDB Import QDockWidget."""

    @staticmethod
    def create(app):
        cols = _occ_colors()

        dock = QDockWidget("GDB Import", app)
        dock.setObjectName("GdbImportDock")
        dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        dock.setFeatures(
            QDockWidget.DockWidgetClosable
            | QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
        )
        dock.setMinimumWidth(320)
        dock.setMinimumHeight(380)

        # When floated, become a standalone window with its own taskbar button.
        def _make_standalone():
            try:
                pos = dock.frameGeometry().topLeft()
                w = max(380, dock.width())
                h = max(500, dock.height())
                dock.setParent(None)
                dock.setWindowFlags(
                    Qt.Window
                    | Qt.CustomizeWindowHint
                    | Qt.WindowTitleHint
                    | Qt.WindowSystemMenuHint
                    | Qt.WindowMinimizeButtonHint
                    | Qt.WindowMaximizeButtonHint
                    | Qt.WindowCloseButtonHint
                )
                dock.setWindowTitle("GDB Import")
                dock.resize(w, h)
                dock.move(pos)
                dock.show()
            except Exception as exc:
                print(f"⚠️ float→window failed: {exc}")

        def _on_float(floating):
            if floating:
                QTimer.singleShot(0, _make_standalone)
        dock.topLevelChanged.connect(_on_float)

        root = QWidget()
        root.setObjectName("GdbImportPanel")
        dock.setWidget(root)
        dock.setStyleSheet(_build_panel_style())

        v = QVBoxLayout(root)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(8)

        # ── Header: title + path ────────────────────────────────────────────
        title = QLabel("GDB Import")
        title.setObjectName("gdbTitle")
        path_lbl = QLabel("(no GDB opened)")
        path_lbl.setObjectName("gdbPath")
        path_lbl.setWordWrap(True)
        warn_lbl = QLabel("")
        warn_lbl.setObjectName("gdbWarn")
        warn_lbl.setWordWrap(True)
        warn_lbl.hide()
        v.addWidget(title)
        v.addWidget(path_lbl)
        v.addWidget(warn_lbl)

        # ── Action buttons row ─────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_open = QPushButton("  Open GDB…")
        btn_open.setObjectName("gdbOpen")
        btn_open.setIcon(_icon("gdb", cols["on_accent"], 14))
        btn_open.setIconSize(Qt.QSize(14, 14))
        btn_open.setCursor(Qt.PointingHandCursor)
        btn_import = QPushButton("  Import selected")
        btn_import.setObjectName("gdbImport")
        btn_import.setIcon(_icon("import", cols["on_accent"], 14))
        btn_import.setIconSize(Qt.QSize(14, 14))
        btn_import.setCursor(Qt.PointingHandCursor)
        btn_row.addWidget(btn_open)
        btn_row.addWidget(btn_import)
        v.addLayout(btn_row)

        # ── Search box + select-all ────────────────────────────────────────
        filter_row = QHBoxLayout()
        filter_box = QLineEdit()
        filter_box.setObjectName("gdbFilter")
        filter_box.setPlaceholderText("Filter feature classes…")
        filter_box.setClearButtonEnabled(True)
        filter_row.addWidget(filter_box, 1)
        btn_all = QPushButton("All")
        btn_all.setObjectName("gdbSecondary")
        btn_none = QPushButton("None")
        btn_none.setObjectName("gdbSecondary")
        for b in (btn_all, btn_none):
            b.setCursor(Qt.PointingHandCursor)
            filter_row.addWidget(b)
        v.addLayout(filter_row)

        # ── Tree of feature classes ────────────────────────────────────────
        tree = QTreeWidget()
        tree.setObjectName("gdbTree")
        tree.setHeaderLabels(["Feature class", "Geometry", "Features"])
        tree.setRootIsDecorated(False)
        tree.setUniformRowHeights(False)
        tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        tree.setAlternatingRowColors(False)
        tree.setColumnWidth(0, 200)
        tree.setColumnWidth(1, 110)
        tree.header().setStretchLastSection(True)
        v.addWidget(tree, 1)

        # ── Footer: Workspace Domains / Create Features / Reload ───────────
        footer = QHBoxLayout()
        btn_domains = QPushButton("Workspace Domains…")
        btn_domains.setObjectName("gdbSecondary")
        btn_features = QPushButton("Create Features…")
        btn_features.setObjectName("gdbSecondary")
        btn_reload = QPushButton("Reload")
        btn_reload.setObjectName("gdbSecondary")
        for b in (btn_domains, btn_features, btn_reload):
            b.setCursor(Qt.PointingHandCursor)
            footer.addWidget(b)
        v.addLayout(footer)

        # ── Hint ───────────────────────────────────────────────────────────
        hint = QLabel("Tip: drag a .gdb folder onto the scene to open it here.")
        hint.setObjectName("gdbHint")
        hint.setWordWrap(True)
        v.addWidget(hint)

        # ── State held on the dock ─────────────────────────────────────────
        state = {
            "gdb_path": None,
            "engine": None,
        }

        def _set_gdb_path(path: str):
            if not path:
                return
            state["gdb_path"] = path
            state["engine"] = get_engine(path)
            try:
                path_lbl.setText(self._format_path_label(path))
            except Exception:
                path_lbl.setText(path)

            # GDAL driver warning
            warn = ""
            ok, ver = check_gdal_version()
            if not ok:
                if has_openfilegdb_driver() and not has_filegdb_driver():
                    warn = f"GDAL {ver} — OpenFileGDB driver is read-only. Upgrade GDAL to ≥ 3.6 for write support."
                else:
                    warn = f"GDAL {ver} — upgrade to ≥ 3.6 for GDB write support."
            elif not has_filegdb_driver() and not has_openfilegdb_driver():
                warn = "Neither FileGDB nor OpenFileGDB driver is available."
            if warn:
                warn_lbl.setText("⚠ " + warn)
                warn_lbl.show()
            else:
                warn_lbl.hide()

            _populate_tree()

        def _populate_tree():
            tree.blockSignals(True)
            tree.clear()
            path = state["gdb_path"]
            if not path:
                tree.blockSignals(False)
                return
            try:
                names = list_layer_names(path)
            except Exception as exc:
                names = []
                QMessageBox.warning(dock, "GDB", f"Could not list layers:\n{exc}")
            ds = gdb_reader.open_gdb_for_read(path)
            try:
                for name in names:
                    lyr = gdb_reader.get_layer(ds, name)
                    if lyr is None:
                        continue
                    defn = lyr.GetLayerDefn()
                    try:
                        gt = defn.GetGeomType() if defn else 0
                    except Exception:
                        gt = 0
                    try:
                        fc = lyr.GetFeatureCount() if lyr else 0
                    except Exception:
                        fc = 0
                    kind = gdb_reader.geometry_kind(gt)
                    is_cascaded = (state["engine"] is not None and
                                   state["engine"].is_cascaded_layer(name))
                    nn_count = (len(state["engine"].get_non_nullable_fields(name))
                                if state["engine"] else 0)

                    label = name
                    if is_cascaded:
                        label = f"  {name}  ⛓"
                    icon_name = kind if kind in ("point", "line", "polygon") else "table"
                    it = QTreeWidgetItem([
                        label,
                        gdb_reader.geometry_type_label(gt),
                        f"{fc:,}" if fc else "—",
                    ])
                    it.setIcon(0, _icon(icon_name, _geom_color(kind, cols), 16))
                    it.setData(0, Qt.UserRole, {
                        "name": name,
                        "kind": kind,
                        "geom_type": gt,
                        "feature_count": fc,
                        "is_cascaded": is_cascaded,
                        "non_nullable_count": nn_count,
                    })
                    it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable
                                | Qt.ItemIsUserCheckable)
                    it.setCheckState(0, Qt.Unchecked)
                    tooltip = (
                        f"{name}\n"
                        f"Geometry: {gdb_reader.geometry_type_label(gt)}\n"
                        f"Features: {fc:,}\n"
                    )
                    if is_cascaded:
                        tooltip += "Cascaded subtype: yes (domain values depend on subtype)\n"
                    if nn_count:
                        tooltip += f"Non-nullable fields: {nn_count}\n"
                    it.setToolTip(0, tooltip)
                    tree.addTopLevelItem(it)
            finally:
                ds = None
            tree.blockSignals(False)

        def _apply_filter(text: str):
            t = (text or "").strip().lower()
            for i in range(tree.topLevelItemCount()):
                it = tree.topLevelItem(i)
                it.setHidden(bool(t) and t not in it.text(0).lower())

        def _checked_layers() -> list:
            out = []
            for i in range(tree.topLevelItemCount()):
                it = tree.topLevelItem(i)
                if it.checkState(0) == Qt.Checked:
                    data = it.data(0, Qt.UserRole)
                    if data:
                        out.append(data)
            return out

        def _do_open():
            start = state["gdb_path"] or str(Path.home())
            d = QFileDialog.getExistingDirectory(
                app, "Open ESRI File Geodatabase folder", start)
            if not d:
                f, _ = QFileDialog.getOpenFileName(
                    app, "Open zipped File Geodatabase", start,
                    "File Geodatabase ZIP (*.gdb.zip);;GDB table (*.gdbtable);;All files (*)")
                d = f
            if d:
                _set_gdb_path(d)

        def _do_import():
            path = state["gdb_path"]
            if not path:
                QMessageBox.information(dock, "GDB",
                                        "Open a GDB first (Open GDB… button).")
                return
            selected = _checked_layers()
            if not selected:
                QMessageBox.information(dock, "GDB",
                                        "Check at least one feature class to import.")
                return
            ok_count = 0
            imported_entries = []
            fail = []
            from gui.gis.gis_layers import suspend_layer_panel_refresh, resume_layer_panel_refresh
            suspend_layer_panel_refresh(app)
            try:
                for data in selected:
                    name = data["name"]
                    try:
                        entry = gdb_reader.import_gdb_layer(app, path, name)
                        if entry:
                            imported_entries.append(entry)
                            ok_count += 1
                        else:
                            fail.append(name)
                    except Exception as exc:
                        fail.append(f"{name}: {exc}")
            finally:
                resume_layer_panel_refresh(app)
            if fail:
                QMessageBox.warning(
                    dock, "Import errors",
                    f"Imported {ok_count} of {len(selected)} layer(s).\n\n"
                    f"Failed:\n  " + "\n  ".join(fail))
            # Show the OCC panel
            try:
                from gui.gis.gis_layers import show_gis_layers_panel, zoom_to_gis_entries
                if imported_entries:
                    zoom_to_gis_entries(app, imported_entries)
                show_gis_layers_panel(app)
            except Exception:
                pass

        def _do_domains():
            path = state["gdb_path"]
            if not path:
                QMessageBox.information(dock, "GDB",
                                        "Open a GDB first.")
                return
            try:
                from .domains_dialog import WorkspaceDomainsDialog
                eng = state["engine"] or get_engine(path)
                dlg = WorkspaceDomainsDialog(eng, parent=app)
                dlg.setStyleSheet(_build_panel_style())
                dlg.show()
                dock._domains_dialog = dlg   # keep a reference
            except Exception as exc:
                import traceback
                traceback.print_exc()
                QMessageBox.warning(dock, "Domains", f"Could not open:\n{exc}")

        def _do_features():
            path = state["gdb_path"]
            if not path:
                QMessageBox.information(dock, "GDB",
                                        "Open a GDB first.")
                return
            try:
                from .features_panel import CreateFeaturesPanel
                eng = state["engine"] or get_engine(path)
                dlg = CreateFeaturesPanel(app, eng, path)
                dlg.setStyleSheet(_build_panel_style())
                dlg.show()
                dock._features_dialog = dlg
            except Exception as exc:
                import traceback
                traceback.print_exc()
                QMessageBox.warning(dock, "Create Features",
                                    f"Could not open:\n{exc}")

        def _do_reload():
            if not state["gdb_path"]:
                return
            # Clear engine cache so the schema is re-parsed.
            from .engine import clear_engine_cache
            clear_engine_cache(state["gdb_path"])
            state["engine"] = get_engine(state["gdb_path"])
            _populate_tree()
            # Refresh any on-screen layers from this GDB
            try:
                from gui.gis.gis_layers import _registry
                for entry in list(_registry(app)):
                    if entry.get("gdb_path") == state["gdb_path"]:
                        nm = entry.get("gdb_layer_name")
                        if nm:
                            gdb_reader.reload_gdb_layer(app, state["gdb_path"], nm)
            except Exception:
                pass

        def _import_and_focus(layer_name: str):
            entry = gdb_reader.import_gdb_layer(app, state["gdb_path"], layer_name)
            if entry:
                try:
                    from gui.gis.gis_layers import show_gis_layers_panel, zoom_to_gis_entries
                    zoom_to_gis_entries(app, [entry])
                    show_gis_layers_panel(app)
                except Exception:
                    pass
            return entry

        def _context_menu(pos):
            it = tree.itemAt(pos)
            if it is not None:
                tree.setCurrentItem(it)
            data = tree.currentItem().data(0, Qt.UserRole) if tree.currentItem() else None
            if not data:
                return
            menu = QMenu(tree)
            menu.addAction("✓ Import this layer",
                           lambda: gdb_reader.import_gdb_layer(
                               app, state["gdb_path"], data["name"]))
            menu.addAction("🔄 Reload this layer",
                           lambda: gdb_reader.reload_gdb_layer(
                               app, state["gdb_path"], data["name"]))
            menu.addSeparator()
            menu.addAction("📋 Copy layer name",
                           lambda: QApplication_clipboard_set(app, data["name"]))
            menu.exec(tree.viewport().mapToGlobal(pos))

        def _select_all(state_yes: bool):
            for i in range(tree.topLevelItemCount()):
                tree.topLevelItem(i).setCheckState(
                    0, Qt.Checked if state_yes else Qt.Unchecked)

        # Wire signals
        btn_open.clicked.connect(_do_open)
        btn_import.clicked.connect(_do_import)
        btn_domains.clicked.connect(_do_domains)
        btn_features.clicked.connect(_do_features)
        btn_reload.clicked.connect(_do_reload)
        btn_all.clicked.connect(lambda: _select_all(True))
        btn_none.clicked.connect(lambda: _select_all(False))
        filter_box.textChanged.connect(_apply_filter)
        tree.customContextMenuRequested.connect(_context_menu)
        tree.setContextMenuPolicy(Qt.CustomContextMenu)
        tree.itemDoubleClicked.connect(
            lambda it, _c: gdb_reader.import_gdb_layer(
                app, state["gdb_path"], it.data(0, Qt.UserRole)["name"]))

        dock._gdb_state = state
        dock.set_gdb_path = _set_gdb_path
        dock.refresh_tree = _populate_tree
        dock.get_state = lambda: state
        return dock

    @staticmethod
    def _format_path_label(path: str) -> str:
        """Make long Windows paths readable in a QLabel (parent dir + basename)."""
        if not path:
            return ""
        p = Path(path)
        parent = p.parent.name
        if parent:
            return f"…{os.sep}{parent}{os.sep}{p.name}"
        return path


def _geom_color(kind: str, cols: dict) -> str:
    if kind == "point":
        return cols.get("secondary", "#aeb4bf")
    if kind == "line":
        return cols.get("accent_hover", "#4c6ef5")
    if kind == "polygon":
        return "#37b24d" if not cols.get("is_light") else "#2f9e44"
    return cols.get("muted", "#7f8794")


def QApplication_clipboard_set(app, text: str):
    try:
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(text)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  Public: show / toggle the panel
# ─────────────────────────────────────────────────────────────────────────────

def show_gdb_import_panel(app) -> "QDockWidget":
    from PySide6.QtCore import Qt
    dock = getattr(app, "_gdb_import_dock", None)
    if dock is None:
        dock = GdbImportDock.create(app)
        setattr(app, "_gdb_import_dock", dock)
        try:
            app.addDockWidget(Qt.LeftDockWidgetArea, dock)
        except Exception:
            pass
    else:
        try:
            dock.refresh_tree()
        except Exception:
            pass
    try:
        dock.setStyleSheet(_build_panel_style())
    except Exception:
        pass
    dock.show()
    try:
        if dock.isFloating():
            dock.raise_()
    except Exception:
        pass
    return dock


def toggle_gdb_import_panel(app):
    dock = getattr(app, "_gdb_import_dock", None)
    if dock is not None and dock.isVisible():
        dock.hide()
        return dock
    return show_gdb_import_panel(app)


def open_gdb_in_panel(app, gdb_path: str):
    """Open the GDB import panel and pre-load a .gdb folder path."""
    dock = show_gdb_import_panel(app)
    try:
        dock.set_gdb_path(gdb_path)
    except Exception:
        pass
    return dock
