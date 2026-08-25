# ─────────────────────────────────────────────────────────────────────────────
# gui/gis/gdb/domains_dialog.py — Workspace Domains viewer & editor
#
# PySide6 port of the QGIS dialog (gdb_engine/domains_dialog.py).
# Lets the user:
#   - browse all CodedValue + Range domains in the GDB
#   - add / remove / rename codes (and ranges)
#   - see which fields / subtypes use each domain
#   - apply changes to the engine (in-memory; persisted via the engine's
#     save_project_domain_overrides)
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import copy
import logging
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QSplitter, QFrame,
    QTableWidget, QTableWidgetItem, QLabel, QPushButton, QListWidget,
    QListWidgetItem, QGroupBox, QFormLayout, QLineEdit, QComboBox,
    QMessageBox, QHeaderView, QAbstractItemView, QWidget, QCheckBox,
    QTextEdit,
)

from .engine import GDBEngine

log = logging.getLogger("GDBEngine")


FIELD_TYPES = [
    "Double", "Float", "Integer", "Long", "Short", "SmallInteger",
    "BigInteger", "String", "Date", "DateOnly", "TimeOnly",
    "TimestampOffset", "OID", "GUID", "GlobalID", "Geometry", "Blob",
    "Raster", "XML",
]
DOMAIN_TYPES = ["CodedValue", "Range"]
SPLIT_POLICIES = ["DefaultValue", "Duplicate", "GeometryRatio"]
MERGE_POLICIES = ["DefaultValue", "SumValues", "AreaWeighted"]


class WorkspaceDomainsDialog(QDialog):
    """Dialog for browsing + editing the GDB's coded-value and range domains."""

    dirty_changed = Signal(bool)

    def __init__(self, engine: GDBEngine, parent=None, select_domain: str | None = None):
        super().__init__(parent)
        self.engine = engine
        self._domains = [self._decorate(d) for d in engine.export_domains_meta()]
        self._dirty = False
        self._current_name: str | None = None

        gdb_name = os.path.basename(engine.gdb_path or "")
        self.setWindowTitle(f"Workspace Domains — {gdb_name}")
        self.setMinimumSize(850, 580)
        self.resize(960, 640)
        self.setWindowFlags(self.windowFlags() | Qt.Tool)
        
        try:
            from gui.theme_manager import get_dialog_stylesheet, ThemeColors
            self.setStyleSheet(get_dialog_stylesheet())
            self.bg_sec = ThemeColors.get("bg_secondary") or "#ffffff"
            self.bg_input = ThemeColors.get("bg_input") or "#f7f8fa"
            self.text_prim = ThemeColors.get("text_primary") or "#1b2838"
            self.border_col = ThemeColors.get("border") or "#d0d5dd"
        except Exception:
            self.bg_sec, self.bg_input, self.text_prim, self.border_col = "#ffffff", "#f7f8fa", "#1b2838", "#d0d5dd"

        self._setup_ui(gdb_name)
        self._load_domain_list()
        
        selected_row = 0
        if select_domain:
            for r in range(self.domain_table.rowCount()):
                item = self.domain_table.item(r, 0)
                if item and item.text().lower() == select_domain.lower():
                    selected_row = r
                    break
        if self.domain_table.rowCount() > 0:
            self.domain_table.setCurrentCell(selected_row, 0)

    def select_domain(self, domain_name: str):
        if not domain_name:
            return
        for r in range(self.domain_table.rowCount()):
            item = self.domain_table.item(r, 0)
            if item and item.text().lower() == domain_name.lower():
                self.domain_table.setCurrentCell(r, 0)
                break

    # ── UI setup ──────────────────────────────────────────────────────────

    def _setup_ui(self, gdb_name: str):
        main = QVBoxLayout(self)
        main.setContentsMargins(6, 6, 6, 6)
        main.setSpacing(4)

        # Title bar
        title = QLabel(f"  Domains  —  {gdb_name}")
        title.setStyleSheet(
            "background:#2c5f8a; color:white; padding:4px 8px; "
            "font-weight:bold; font-size:12px; border-radius:2px;"
        )
        main.addWidget(title)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(4)
        main.addWidget(splitter, 1)

        # ── Left: domain list table ─────────────────────────────────────────
        left = QFrame()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(4)
        
        lbl_domains = QLabel("DOMAINS")
        lbl_domains.setStyleSheet("font-weight: bold; font-size: 9pt;")
        ll.addWidget(lbl_domains)
        
        self.domain_table = QTableWidget(0, 1)
        self.domain_table.setHorizontalHeaderLabels(["Domain Name"])
        self.domain_table.verticalHeader().setVisible(False)
        self.domain_table.verticalHeader().setDefaultSectionSize(20)
        self.domain_table.horizontalHeader().setMinimumHeight(20)
        self.domain_table.horizontalHeader().setMaximumHeight(20)
        self.domain_table.setAlternatingRowColors(True)
        self.domain_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.domain_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.domain_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.domain_table.currentItemChanged.connect(self._on_select)
        ll.addWidget(self.domain_table, 1)

        left_btns = QHBoxLayout()
        self.btn_add = QPushButton("+ Add Domain")
        self.btn_remove = QPushButton("- Delete Domain")
        left_btns.addWidget(self.btn_add)
        left_btns.addWidget(self.btn_remove)
        ll.addLayout(left_btns)
        splitter.addWidget(left)

        # ── Right: editor ──────────────────────────────────────────────────
        right = QFrame()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)

        # Properties group
        props = QGroupBox("Domain Properties")
        pf = QFormLayout(props)
        pf.setContentsMargins(6, 6, 6, 6)
        pf.setSpacing(4)
        
        self.ed_name = QLineEdit()
        self.ed_description = QLineEdit()
        self.cmb_type = QComboBox()
        self.cmb_type.addItems(DOMAIN_TYPES)
        self.cmb_field_type = QComboBox()
        self.cmb_field_type.addItems(FIELD_TYPES)
        self.cmb_split = QComboBox()
        self.cmb_split.addItems(SPLIT_POLICIES)
        self.cmb_merge = QComboBox()
        self.cmb_merge.addItems(MERGE_POLICIES)
        
        pf.addRow("Name:", self.ed_name)
        pf.addRow("Description:", self.ed_description)
        pf.addRow("Field Type:", self.cmb_field_type)
        pf.addRow("Domain Type:", self.cmb_type)
        pf.addRow("Split policy:", self.cmb_split)
        pf.addRow("Merge policy:", self.cmb_merge)
        rl.addWidget(props)

        # Coded values container (can be hidden/shown)
        self.coded_container = QWidget()
        coded_layout = QVBoxLayout(self.coded_container)
        coded_layout.setContentsMargins(0, 4, 0, 0)
        coded_layout.setSpacing(4)

        # Coded values header bar
        coded_header = QHBoxLayout()
        self.coded_lbl = QLabel("Coded Values:")
        self.coded_lbl.setStyleSheet("font-weight: bold;")
        self.btn_add_code = QPushButton("+ Add")
        self.btn_remove_code = QPushButton("- Delete")
        coded_header.addWidget(self.coded_lbl)
        coded_header.addStretch()
        coded_header.addWidget(self.btn_add_code)
        coded_header.addWidget(self.btn_remove_code)
        coded_layout.addLayout(coded_header)

        # Coded values table
        self.coded_table = QTableWidget(0, 2)
        self.coded_table.setHorizontalHeaderLabels(["Code", "Description"])
        self.coded_table.verticalHeader().setVisible(False)
        self.coded_table.verticalHeader().setDefaultSectionSize(20)
        self.coded_table.horizontalHeader().setMinimumHeight(20)
        self.coded_table.horizontalHeader().setMaximumHeight(20)
        self.coded_table.setAlternatingRowColors(True)
        self.coded_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.coded_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        self.coded_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.coded_table.setColumnWidth(0, 120)
        coded_layout.addWidget(self.coded_table, 1)

        rl.addWidget(self.coded_container, 1)

        # Range box container (can be hidden/shown)
        self.range_box = QGroupBox("Range Properties")
        rf = QFormLayout(self.range_box)
        rf.setContentsMargins(6, 6, 6, 6)
        rf.setSpacing(4)
        self.ed_min = QLineEdit()
        self.ed_max = QLineEdit()
        rf.addRow("Min value:", self.ed_min)
        rf.addRow("Max value:", self.ed_max)
        rl.addWidget(self.range_box)

        # Apply general compact stylesheet stylesheet
        table_style = f"""
            QTableWidget {{
                background-color: {self.bg_sec};
                alternate-background-color: {self.bg_input};
                color: {self.text_prim};
                gridline-color: {self.border_col};
                font-size: 9pt;
                border: 1px solid {self.border_col};
                border-radius: 4px;
            }}
            QTableWidget::item {{
                padding: 1px 4px;
            }}
            QHeaderView::section {{
                background-color: {self.bg_input};
                color: {self.text_prim};
                border: 1px solid {self.border_col};
                padding: 2px 4px;
                font-size: 9pt;
                font-weight: bold;
            }}
        """
        self.domain_table.setStyleSheet(table_style)
        self.coded_table.setStyleSheet(table_style)

        self.setStyleSheet(self.styleSheet() + f"""
            QLabel {{ font-size: 9pt; }}
            QLineEdit, QComboBox {{
                font-size: 9pt;
                min-height: 20px;
                max-height: 20px;
                padding: 1px 3px;
                border: 1px solid {self.border_col};
                border-radius: 3px;
                background-color: {self.bg_sec};
                color: {self.text_prim};
            }}
            QGroupBox {{
                font-size: 9.5pt;
                font-weight: bold;
                border: 1px solid {self.border_col};
                margin-top: 6px;
                padding-top: 6px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 7px;
                padding: 0px 3px;
            }}
            QPushButton {{
                font-size: 9pt;
                padding: 2px 8px;
                border: 1px solid {self.border_col};
                border-radius: 3px;
                background-color: {self.bg_input};
                color: {self.text_prim};
            }}
            QPushButton:hover {{
                background-color: {self.border_col};
            }}
        """)

        # Usage preview
        self.usage_lbl = QLabel("")
        self.usage_lbl.setWordWrap(True)
        self.usage_lbl.setStyleSheet(
            "background:rgba(0,0,0,25); color:#aeb4bf; padding:4px 6px; border-radius:2px; font-size:8.5pt;")
        rl.addWidget(self.usage_lbl)

        splitter.addWidget(right)
        splitter.setSizes([200, 760])

        # ── Footer ─────────────────────────────────────────────────────────
        footer = QHBoxLayout()
        footer.setContentsMargins(2, 2, 2, 2)
        self.dirty_lbl = QLabel("")
        self.dirty_lbl.setStyleSheet("color:#d97706; font-weight:600; font-size:8.5pt;")
        footer.addWidget(self.dirty_lbl)
        footer.addStretch()
        
        self.btn_apply = QPushButton("Apply changes")
        self.btn_apply.setStyleSheet(
            "background:#3b5bdb; color:white; font-weight:600; padding:4px 12px; "
            "border:none; border-radius:3px; font-size:9pt;")
        self.btn_reset = QPushButton("Reset")
        self.btn_revert = QPushButton("Revert to GDB schema")
        
        for btn in (self.btn_reset, self.btn_revert):
            btn.setStyleSheet("padding:4px 10px; font-size:9pt;")
            
        footer.addWidget(self.btn_reset)
        footer.addWidget(self.btn_revert)
        footer.addWidget(self.btn_apply)
        main.addLayout(footer)

        # ── Wire signals ────────────────────────────────────────────────────
        self.btn_add.clicked.connect(self._on_new_domain)
        self.btn_remove.clicked.connect(self._on_remove_domain)
        self.btn_add_code.clicked.connect(self._on_add_code)
        self.btn_remove_code.clicked.connect(self._on_remove_code)
        self.btn_apply.clicked.connect(self._on_apply)
        self.btn_reset.clicked.connect(self._on_reset)
        self.btn_revert.clicked.connect(self._on_revert)

        for w in (self.ed_name, self.ed_description):
            w.textChanged.connect(self._mark_dirty)
        for c in (self.cmb_type, self.cmb_field_type, self.cmb_split, self.cmb_merge):
            c.currentIndexChanged.connect(self._mark_dirty)
        self.coded_table.itemChanged.connect(self._on_code_edited)
        self.ed_min.textChanged.connect(self._mark_dirty)
        self.ed_max.textChanged.connect(self._mark_dirty)
        self.cmb_type.currentIndexChanged.connect(self._on_type_changed)

    # ── List population ──────────────────────────────────────────────────

    def _update_type_visibility(self, dtype: str):
        is_range = (dtype == "Range")
        self.range_box.setVisible(is_range)
        self.coded_container.setVisible(not is_range)

    def _load_domain_list(self):
        self.domain_table.blockSignals(True)
        self.domain_table.setRowCount(0)
        for d in self._domains:
            r = self.domain_table.rowCount()
            self.domain_table.insertRow(r)
            
            name_item = QTableWidgetItem(d["name"])
            name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
            
            tooltip = (
                f"{d['name']}  ({d['domain_type']})\n"
                f"field_type={d['field_type']}\n"
                f"{d['description']}"
            )
            if d["domain_type"] == "CodedValue":
                tooltip += f"\ncodes: {len(d['coded_values'])}"
            else:
                tooltip += f"\nrange: {d['min_value']} – {d['max_value']}"
            name_item.setToolTip(tooltip)
            
            self.domain_table.setItem(r, 0, name_item)
            
        self.domain_table.blockSignals(False)
        self._update_dirty_lbl()

    def _on_select(self, current, _previous):
        if current is None:
            return
        row = current.row()
        name_item = self.domain_table.item(row, 0)
        if name_item:
            self._select_domain(name_item)

    def _select_domain(self, item: QTableWidgetItem):
        name = item.text()
        self._current_name = name
        domain = self._find_domain(name)
        if not domain:
            return
        # Disconnect itemChanged while we populate the table to avoid spurious dirty.
        self.coded_table.blockSignals(True)
        for w in (self.ed_name, self.ed_description, self.ed_min, self.ed_max):
            w.blockSignals(True)
        for c in (self.cmb_type, self.cmb_field_type, self.cmb_split, self.cmb_merge):
            c.blockSignals(True)
        try:
            self.ed_name.setText(domain["name"])
            self.ed_description.setText(domain["description"])
            self.cmb_type.setCurrentText(domain["domain_type"])
            self.cmb_field_type.setCurrentText(domain["field_type"])
            self.cmb_split.setCurrentText(domain["split"])
            self.cmb_merge.setCurrentText(domain["merge"])
            self.ed_min.setText(str(domain.get("min_value") or ""))
            self.ed_max.setText(str(domain.get("max_value") or ""))
            
            self._update_type_visibility(domain["domain_type"])
            
            # Coded values
            self.coded_table.setRowCount(0)
            if domain["domain_type"] == "CodedValue":
                for cv in domain["coded_values"]:
                    r = self.coded_table.rowCount()
                    self.coded_table.insertRow(r)
                    self.coded_table.setItem(r, 0, QTableWidgetItem(str(cv["code"])))
                    self.coded_table.setItem(r, 1, QTableWidgetItem(str(cv["description"])))
        finally:
            self.coded_table.blockSignals(False)
            for w in (self.ed_name, self.ed_description, self.ed_min, self.ed_max):
                w.blockSignals(False)
            for c in (self.cmb_type, self.cmb_field_type, self.cmb_split, self.cmb_merge):
                c.blockSignals(False)
        self._update_usage()
        self._update_dirty_lbl()

    # ── Mutators ──────────────────────────────────────────────────────────

    def _on_new_domain(self):
        base = "New_Domain"
        existing = {d["name"] for d in self._domains}
        name = base
        i = 1
        while name in existing:
            i += 1
            name = f"{base}_{i}"
        self._domains.append({
            "name": name,
            "description": "",
            "field_type": "String",
            "domain_type": "CodedValue",
            "split": "DefaultValue",
            "merge": "DefaultValue",
            "min_value": None,
            "max_value": None,
            "coded_values": [],
        })
        self._load_domain_list()
        # Select the new domain
        for r in range(self.domain_table.rowCount()):
            item = self.domain_table.item(r, 0)
            if item and item.text() == name:
                self.domain_table.setCurrentCell(r, 0)
                break
        self._mark_dirty()

    def _on_remove_domain(self):
        if not self._current_name:
            return
        domain = self._find_domain(self._current_name)
        if not domain:
            return
        usage = self.engine.domain_usage(self._current_name)
        n_use = len(usage["fields"]) + len(usage["subtypes"])
        msg = f"Remove domain '{self._current_name}'?"
        if n_use:
            msg += f"\n\n⚠ {n_use} field(s) reference this domain. " \
                   "Removing it will leave those fields with no domain."
        if QMessageBox.question(self, "Remove domain", msg) != QMessageBox.Yes:
            return
        self._domains = [d for d in self._domains if d["name"] != self._current_name]
        self._current_name = None
        self._load_domain_list()
        self.coded_table.setRowCount(0)
        for w in (self.ed_name, self.ed_description, self.ed_min, self.ed_max):
            w.clear()
        self._mark_dirty()

    def _on_add_code(self):
        if not self._current_name:
            return
        domain = self._find_domain(self._current_name)
        if not domain or domain["domain_type"] != "CodedValue":
            return
        # Find an unused numeric code
        existing = {str(cv["code"]) for cv in domain["coded_values"]}
        i = 1
        while str(i) in existing:
            i += 1
        r = self.coded_table.rowCount()
        self.coded_table.insertRow(r)
        self.coded_table.setItem(r, 0, QTableWidgetItem(str(i)))
        self.coded_table.setItem(r, 1, QTableWidgetItem("New value"))
        self._mark_dirty()

    def _on_remove_code(self):
        rows = sorted({i.row() for i in self.coded_table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.coded_table.removeRow(r)
        self._mark_dirty()

    def _on_code_edited(self, _item):
        if self.coded_table.signalsBlocked():
            return
        self._mark_dirty()

    def _on_type_changed(self, _idx):
        dtype = self.cmb_type.currentText()
        self._update_type_visibility(dtype)
        self._mark_dirty()

    def _mark_dirty(self):
        self._dirty = True
        self._update_dirty_lbl()

    def _update_dirty_lbl(self):
        if self._dirty:
            self.dirty_lbl.setText("● Unsaved changes")
        else:
            self.dirty_lbl.setText("")

    def _update_usage(self):
        if not self._current_name:
            self.usage_lbl.setText("")
            return
        usage = self.engine.domain_usage(self._current_name)
        if not usage["fields"] and not usage["subtypes"]:
            self.usage_lbl.setText(f"Domain '{self._current_name}' is not used by any field.")
            return
        lines = [f"<b>Used by:</b>"]
        for layer, field in usage["fields"]:
            lines.append(f"  • {layer}  ·  {field}")
        for layer, field, code in usage["subtypes"]:
            lines.append(f"  • {layer}  ·  {field}  (when subtype = {code})")
        self.usage_lbl.setText("<br>".join(lines))

    def _on_reset(self):
        # Discard edits to the currently-shown domain
        if not self._current_name:
            return
        for i, d in enumerate(self._domains):
            if d["name"] == self._current_name:
                self._domains[i] = self._decorate(
                    self.engine.get_domain_meta(self._current_name) or {})
                break
        self._load_domain_list()
        for r in range(self.domain_table.rowCount()):
            item = self.domain_table.item(r, 0)
            if item and item.text() == self._current_name:
                self.domain_table.setCurrentCell(r, 0)
                break
        self._dirty = False
        self._update_dirty_lbl()

    def _on_revert(self):
        if QMessageBox.question(
                self, "Revert all",
                "Discard ALL pending domain edits and reload from the GDB?") != QMessageBox.Yes:
            return
        self._domains = [self._decorate(d) for d in self.engine.export_domains_meta()]
        self._load_domain_list()
        if self.domain_table.rowCount() > 0:
            self.domain_table.setCurrentCell(0, 0)
        self._dirty = False
        self._update_dirty_lbl()

    def _on_apply(self):
        # Commit the visible domain's edits to self._domains
        if not self._current_name:
            return
        domain = self._find_domain(self._current_name)
        if not domain:
            return
        # Handle rename
        new_name = self.ed_name.text().strip()
        if not new_name:
            QMessageBox.warning(self, "Apply", "Domain name cannot be empty.")
            return
        if new_name != self._current_name:
            existing = {d["name"] for d in self._domains if d["name"] != self._current_name}
            if new_name in existing:
                QMessageBox.warning(self, "Apply", f"Domain '{new_name}' already exists.")
                return
            self.engine.rename_domain_references(self._current_name, new_name)
            domain["name"] = new_name
            self._current_name = new_name
        domain["description"] = self.ed_description.text().strip()
        domain["domain_type"] = self.cmb_type.currentText()
        domain["field_type"] = self.cmb_field_type.currentText()
        domain["split"] = self.cmb_split.currentText()
        domain["merge"] = self.cmb_merge.currentText()
        if domain["domain_type"] == "CodedValue":
            codes = []
            for r in range(self.coded_table.rowCount()):
                code = (self.coded_table.item(r, 0).text() if self.coded_table.item(r, 0) else "").strip()
                desc = (self.coded_table.item(r, 1).text() if self.coded_table.item(r, 1) else "").strip()
                if code:
                    codes.append({"code": code, "description": desc})
            domain["coded_values"] = codes
            domain["min_value"] = None
            domain["max_value"] = None
        else:
            domain["coded_values"] = []
            domain["min_value"] = self.ed_min.text().strip() or None
            domain["max_value"] = self.ed_max.text().strip() or None

        # Push to engine
        try:
            self.engine.set_domains_meta(self._domains)
            from .engine import save_project_domain_overrides
            save_project_domain_overrides(self.engine.gdb_path, self.engine.export_domains_meta())
        except Exception as exc:
            QMessageBox.warning(self, "Apply", f"Could not save:\n{exc}")
            return

        self._dirty = False
        self._load_domain_list()
        for r in range(self.domain_table.rowCount()):
            item = self.domain_table.item(r, 0)
            if item and item.text() == self._current_name:
                self.domain_table.setCurrentCell(r, 0)
                break
        QMessageBox.information(self, "Apply", "Domain changes saved.")

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _decorate(meta: dict) -> dict:
        meta = copy.deepcopy(meta)
        meta.setdefault("name", "")
        meta.setdefault("description", "")
        meta.setdefault("field_type", "String")
        meta.setdefault("domain_type", "CodedValue")
        meta.setdefault("split", "DefaultValue")
        meta.setdefault("merge", "DefaultValue")
        meta.setdefault("min_value", None)
        meta.setdefault("max_value", None)
        meta.setdefault("coded_values", [])
        return meta

    def _find_domain(self, name: str):
        for d in self._domains:
            if d["name"] == name:
                return d
        return None

    # ── Lifecycle ────────────────────────────────────────────────────────

    def closeEvent(self, event):
        if self._dirty:
            res = QMessageBox.question(
                self, "Unsaved changes",
                "There are unsaved domain edits. Apply them now?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if res == QMessageBox.Yes:
                self._on_apply()
            elif res == QMessageBox.Cancel:
                event.ignore()
                return
        super().closeEvent(event)
