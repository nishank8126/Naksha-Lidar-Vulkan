from __future__ import annotations

import logging
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

log = logging.getLogger("GDBEngine")

_ROLE_INFO = Qt.UserRole
_ROLE_LEAF = Qt.UserRole + 1


def _colors():
    try:
        from gui.theme_manager import ThemeColors as C

        def g(key, fallback):
            try:
                return C.get(key) or fallback
            except Exception:
                return fallback

        light = False
        try:
            light = bool(C.is_light())
        except Exception:
            light = False

        return {
            "panel": g("bg_secondary", "#f5f7fa" if light else "#1b1d23"),
            "surface": g("bg_primary", "#ffffff" if light else "#16181d"),
            "surface_alt": g("bg_input", "#f8fafc" if light else "#20242d"),
            "text": g("text_primary", "#1f2937" if light else "#e8eaed"),
            "muted": g("text_muted", "#64748b" if light else "#7f8794"),
            "secondary": g("text_secondary", "#475569" if light else "#aeb4bf"),
            "border": g("border_light", "#d7dde5" if light else "#2a2d35"),
            "accent": g("accent", "#0b84d8"),
            "accent_hover": g("accent_hover", "#1493ee"),
            "btn": g("bg_button", "#eef2f7" if light else "#262a33"),
            "btn_hover": g("bg_button_hover", "#e3eaf3" if light else "#313641"),
            "sel": g("bg_button_hover", "#e9f4fd" if light else "#2a3442"),
            "on_accent": "#ffffff",
        }
    except Exception:
        return {
            "panel": "#f5f7fa",
            "surface": "#ffffff",
            "surface_alt": "#f8fafc",
            "text": "#1f2937",
            "muted": "#64748b",
            "secondary": "#475569",
            "border": "#d7dde5",
            "accent": "#0b84d8",
            "accent_hover": "#1493ee",
            "btn": "#eef2f7",
            "btn_hover": "#e3eaf3",
            "sel": "#e9f4fd",
            "on_accent": "#ffffff",
        }


def _style(c):
    return f"""
    QDialog#gdbPicker {{
        background:{c['panel']};
        color:{c['text']};
    }}
    QDialog#gdbPicker QLabel {{
        background:transparent;
        color:{c['text']};
    }}
    QDialog#gdbPicker QLabel#pickerPath {{
        color:{c['accent']};
        font-size:8.5pt;
        padding-bottom:2px;
    }}
    QDialog#gdbPicker QLabel#pickerStatus {{
        color:{c['muted']};
        font-size:8.5pt;
        min-height:18px;
    }}
    QDialog#gdbPicker QLabel#pickerError {{
        color:#c62828;
        font-size:8.5pt;
        min-height:18px;
    }}
    QDialog#gdbPicker QLineEdit#pickerSearch {{
        min-height:28px;
        padding:4px 8px;
        border:1px solid {c['border']};
        border-radius:4px;
        background:{c['surface']};
        color:{c['text']};
    }}
    QDialog#gdbPicker QLineEdit#pickerSearch:focus {{
        border:1px solid {c['accent']};
    }}
    QDialog#gdbPicker QTreeWidget#pickerTree {{
        background:{c['surface']};
        alternate-background-color:{c['surface_alt']};
        border:1px solid {c['border']};
        border-radius:4px;
        color:{c['text']};
        outline:none;
    }}
    QDialog#gdbPicker QTreeWidget#pickerTree::item {{
        min-height:22px;
        padding:2px 0;
    }}
    QDialog#gdbPicker QTreeWidget#pickerTree::item:selected {{
        background:{c['sel']};
        color:{c['text']};
    }}
    QDialog#gdbPicker QHeaderView::section {{
        background:{c['surface_alt']};
        color:{c['secondary']};
        border:none;
        border-bottom:1px solid {c['border']};
        padding:4px 6px;
        font-size:8.5pt;
        font-weight:600;
    }}
    QDialog#gdbPicker QPushButton#pickerSecondary {{
        background:{c['btn']};
        color:{c['text']};
        border:1px solid {c['border']};
        border-radius:4px;
        padding:5px 12px;
        min-height:28px;
    }}
    QDialog#gdbPicker QPushButton#pickerSecondary:hover {{
        background:{c['btn_hover']};
    }}
    QDialog#gdbPicker QPushButton#pickerPrimary {{
        background:{c['accent']};
        color:{c['on_accent']};
        border:none;
        border-radius:4px;
        padding:5px 16px;
        min-height:28px;
        font-weight:600;
    }}
    QDialog#gdbPicker QPushButton#pickerPrimary:hover {{
        background:{c['accent_hover']};
    }}
    QDialog#gdbPicker QFrame#pickerOptions {{
        background:{c['surface']};
        border:1px solid {c['border']};
        border-radius:4px;
    }}
    QDialog#gdbPicker QToolButton#pickerOptionsToggle {{
        border:none;
        background:transparent;
        color:{c['text']};
        font-weight:600;
        padding:0;
    }}
    QDialog#gdbPicker QCheckBox {{
        color:{c['text']};
        spacing:6px;
    }}
    """


def _icon(name: str, color_hex: str, size: int = 16):
    try:
        from gui.gis.icons import icon as gicon
        return gicon(name, color_hex, size)
    except Exception:
        from PySide6.QtGui import QIcon
        return QIcon()


def _geom_icon_name(kind: str) -> str:
    return {
        "point": "vector-point",
        "line": "vector-line",
        "polygon": "vector-polygon",
        "none": "table",
    }.get(kind, "table")


def _geom_color(kind: str, colors: dict) -> str:
    return {
        "point": "#2563eb",
        "line": "#0f766e",
        "polygon": "#ca8a04",
        "none": colors["muted"],
    }.get(kind, colors["muted"])


def _description_text(info: dict) -> str:
    count = int(info.get("count") or 0)
    label = info.get("label") or "Layer"
    suffix = f"{label} ({count:,})"
    if info.get("cascaded"):
        suffix += "   cascade"
    return suffix


def pick_and_import_gdb(app, gdb_path: str):
    """Show a compact QGIS-style tree picker and import the checked layers."""
    from .engine import get_engine
    from .reader import (
        feature_count,
        geometry_kind,
        geometry_type_label,
        get_layer,
        import_gdb_layer,
        open_gdb_for_read,
    )
    from gui.gis.gis_layers import show_gis_layers_panel, zoom_to_gis_entries

    engine = get_engine(gdb_path)
    if engine is None:
        QMessageBox.warning(None, "GDB", f"Could not read GDB:\n{gdb_path}")
        return False

    ds = open_gdb_for_read(gdb_path)
    if ds is None:
        QMessageBox.warning(None, "GDB", f"Could not open GDB:\n{gdb_path}")
        return False

    layers = []
    for i in range(ds.GetLayerCount()):
        lyr = ds.GetLayerByIndex(i)
        if lyr is None:
            continue
        name = lyr.GetName()
        defn = lyr.GetLayerDefn()
        gt = defn.GetGeomType() if defn else 0
        kind = geometry_kind(gt)
        count = feature_count(lyr)
        layers.append({
            "name": name,
            "kind": kind,
            "count": count,
            "label": "Table" if kind == "none" else geometry_type_label(gt),
            "cascaded": engine.is_cascaded_layer(name),
            "group": engine.get_layer_group(name),
            "catalog_path": engine.get_layer_catalog_path(name),
            "internal": engine.is_internal_layer(name) or kind == "none",
        })
    ds = None

    if not layers:
        QMessageBox.information(None, "GDB", "No layers or tables found.")
        return False

    grouped_layers = sorted(
        layers,
        key=lambda item: (
            (item.get("group") or "").casefold(),
            item["name"].casefold(),
        ),
    )

    colors = _colors()
    dlg = QDialog(None)
    dlg.setObjectName("gdbPicker")
    dlg.setWindowTitle(f"Select Items to Add | {os.path.splitext(os.path.basename(gdb_path))[0]}")
    dlg.setMinimumSize(650, 470)
    dlg.resize(700, 520)
    dlg.setStyleSheet(_style(colors))
    dlg.setModal(True)

    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(12, 12, 12, 10)
    layout.setSpacing(8)

    title = QLabel(f"Select Items to Add | {os.path.splitext(os.path.basename(gdb_path))[0]}")
    title.setStyleSheet("font-size:11pt; font-weight:600;")
    layout.addWidget(title)

    path_label = QLabel(gdb_path)
    path_label.setObjectName("pickerPath")
    path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    layout.addWidget(path_label)

    search_box = QLineEdit()
    search_box.setObjectName("pickerSearch")
    search_box.setPlaceholderText("Search...")
    layout.addWidget(search_box)

    tree = QTreeWidget()
    tree.setObjectName("pickerTree")
    tree.setColumnCount(2)
    tree.setHeaderLabels(["Item", "Description"])
    tree.setRootIsDecorated(True)
    tree.setAlternatingRowColors(True)
    tree.setUniformRowHeights(True)
    tree.setSelectionMode(QAbstractItemView.SingleSelection)
    tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
    tree.header().setStretchLastSection(False)
    tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
    tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
    layout.addWidget(tree, 1)

    options_toggle = QToolButton()
    options_toggle.setObjectName("pickerOptionsToggle")
    options_toggle.setText("Options")
    options_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
    options_toggle.setArrowType(Qt.RightArrow)
    options_toggle.setCheckable(True)
    options_toggle.setChecked(False)
    layout.addWidget(options_toggle)

    options_frame = QFrame()
    options_frame.setObjectName("pickerOptions")
    options_frame.setVisible(False)
    options_layout = QVBoxLayout(options_frame)
    options_layout.setContentsMargins(10, 8, 10, 8)
    options_layout.setSpacing(6)

    opt_show_tables = QCheckBox("Show system and internal tables")
    opt_show_tables.setChecked(False)
    options_layout.addWidget(opt_show_tables)

    opt_show_empty = QCheckBox("Show empty vector layers")
    opt_show_empty.setChecked(True)
    options_layout.addWidget(opt_show_empty)

    layout.addWidget(options_frame)

    status_label = QLabel("")
    status_label.setObjectName("pickerStatus")
    layout.addWidget(status_label)

    buttons = QHBoxLayout()
    buttons.setSpacing(8)

    btn_select_all = QPushButton("Select All")
    btn_select_all.setObjectName("pickerSecondary")
    btn_deselect_all = QPushButton("Deselect All")
    btn_deselect_all.setObjectName("pickerSecondary")
    buttons.addWidget(btn_select_all)
    buttons.addWidget(btn_deselect_all)
    buttons.addStretch()

    btn_add = QPushButton("Add Layers")
    btn_add.setObjectName("pickerPrimary")
    btn_cancel = QPushButton("Cancel")
    btn_cancel.setObjectName("pickerSecondary")
    buttons.addWidget(btn_add)
    buttons.addWidget(btn_cancel)
    layout.addLayout(buttons)

    group_items: dict[str, QTreeWidgetItem] = {}
    leaf_items: list[tuple[QTreeWidgetItem, dict]] = []

    def add_leaf(parent, info: dict):
        item = QTreeWidgetItem(parent)
        item.setFlags(
            Qt.ItemIsEnabled | Qt.ItemIsSelectable |
            Qt.ItemIsUserCheckable
        )
        item.setCheckState(0, Qt.Unchecked)
        item.setText(0, info["name"])
        item.setText(1, _description_text(info))
        item.setData(0, _ROLE_INFO, info)
        item.setData(0, _ROLE_LEAF, True)
        item.setIcon(0, _icon(_geom_icon_name(info["kind"]), _geom_color(info["kind"], colors), 14))
        if (info.get("count") or 0) == 0:
            muted_brush = QBrush(QColor(colors["muted"]))
            item.setForeground(0, muted_brush)
            item.setForeground(1, muted_brush)
        leaf_items.append((item, info))

    for info in grouped_layers:
        group_name = (info.get("group") or "").strip()
        if group_name:
            group_item = group_items.get(group_name)
            if group_item is None:
                group_item = QTreeWidgetItem(tree)
                group_item.setFlags(Qt.ItemIsEnabled)
                group_item.setText(0, group_name)
                group_item.setIcon(0, _icon("folder", colors["muted"], 14))
                group_item.setFirstColumnSpanned(False)
                group_item.setExpanded(True)
                group_items[group_name] = group_item
            add_leaf(group_item, info)
        else:
            add_leaf(tree, info)

    def set_status(text: str = "", *, error: bool = False):
        status_label.setText(text)
        status_label.setObjectName("pickerError" if error else "pickerStatus")
        dlg.style().unpolish(status_label)
        dlg.style().polish(status_label)

    def row_is_visible(info: dict, query: str) -> bool:
        if info["kind"] == "none" and not opt_show_tables.isChecked():
            return False
        if info["kind"] != "none" and (info.get("count") or 0) == 0 and not opt_show_empty.isChecked():
            return False
        if not query:
            return True
        hay = " ".join([
            info.get("name", ""),
            info.get("label", ""),
            info.get("group", "") or "",
            info.get("catalog_path", "") or "",
        ]).lower()
        return query in hay

    def apply_filters():
        query = search_box.text().strip().lower()
        visible_count = 0
        for item, info in leaf_items:
            visible = row_is_visible(info, query)
            item.setHidden(not visible)
            if visible:
                visible_count += 1
        for group_item in group_items.values():
            any_child_visible = any(
                not group_item.child(i).isHidden()
                for i in range(group_item.childCount())
            )
            group_item.setHidden(not any_child_visible)
            if any_child_visible:
                group_item.setExpanded(True)
        set_status(f"{visible_count} of {len(leaf_items)} item(s) shown")

    def set_checked_visible(checked: bool):
        for item, _info in leaf_items:
            if item.isHidden():
                continue
            item.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)

    def toggle_options(opened: bool):
        options_frame.setVisible(opened)
        options_toggle.setArrowType(Qt.DownArrow if opened else Qt.RightArrow)

    def add_layers():
        selected = [
            info for item, info in leaf_items
            if not item.isHidden() and item.checkState(0) == Qt.Checked
        ]
        if not selected:
            set_status("Select at least one layer or table to add.", error=True)
            return

        btn_add.setEnabled(False)
        btn_add.setText("Adding…")
        imported_entries = []
        fail_count = 0
        from gui.gis.gis_layers import suspend_layer_panel_refresh, resume_layer_panel_refresh
        suspend_layer_panel_refresh(app)
        try:
            for info in selected:
                try:
                    entry = import_gdb_layer(app, gdb_path, info["name"])
                    if entry is not None:
                        imported_entries.append(entry)
                    else:
                        fail_count += 1
                except Exception as exc:
                    fail_count += 1
                    log.error("Import %s failed: %s", info["name"], exc)
        finally:
            resume_layer_panel_refresh(app)

        btn_add.setEnabled(True)
        btn_add.setText("Add Layers")

        if not imported_entries:
            set_status("Could not add the selected items. Check the log for details.", error=True)
            return

        try:
            zoom_to_gis_entries(app, imported_entries)
        except Exception:
            pass
        try:
            show_gis_layers_panel(app)
        except Exception:
            pass

        dlg._imported_ok = True
        dlg.accept()

    def on_item_activated(item: QTreeWidgetItem, _column: int):
        if not item or not item.data(0, _ROLE_LEAF):
            return
        item.setCheckState(0, Qt.Unchecked if item.checkState(0) == Qt.Checked else Qt.Checked)

    dlg._imported_ok = False
    search_box.textChanged.connect(lambda _text: apply_filters())
    opt_show_tables.toggled.connect(lambda _on: apply_filters())
    opt_show_empty.toggled.connect(lambda _on: apply_filters())
    btn_select_all.clicked.connect(lambda: set_checked_visible(True))
    btn_deselect_all.clicked.connect(lambda: set_checked_visible(False))
    btn_cancel.clicked.connect(dlg.reject)
    btn_add.clicked.connect(add_layers)
    options_toggle.toggled.connect(toggle_options)
    tree.itemDoubleClicked.connect(on_item_activated)

    apply_filters()
    dlg.exec()
    return bool(getattr(dlg, "_imported_ok", False))
