"""ArcGIS-style coordinate reference system chooser for Naksha.

The chooser is backed by PROJ's installed ``proj.db`` (EPSG + ESRI + other
registered authorities) instead of a hard-coded EPSG list.  It is shared by the
project CRS control and GIS schema dialogs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QSettings, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QSplitter, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from pyproj import CRS

from gui.projection_engine import (
    CRSRecord, crs_identifier, describe_crs, parse_crs, proj_database_metadata,
    search_crs, suggest_utm_crs,
)
from gui.gis.gis_style import (
    apply_gis_dialog_style, compact_layout, compact_view, configure_header,
)


_CATEGORIES = [
    ("Favorites", "favorites"),
    ("Recent", "recent"),
    ("UTM", "utm"),
    ("Projected Coordinate Systems", "projected"),
    ("Geographic Coordinate Systems", "geographic"),
    ("Vertical Coordinate Systems", "vertical"),
    ("Compound Coordinate Systems", "compound"),
    ("Engineering / Local", "engineering"),
    ("All Coordinate Systems", "all"),
]


def _theme(dialog):
    apply_gis_dialog_style(dialog)


def _read_json_setting(settings: QSettings, key: str, default):
    raw = settings.value(key, "", type=str)
    if not raw:
        return default
    try:
        value = json.loads(raw)
        return value
    except Exception:
        return default


def _write_json_setting(settings: QSettings, key: str, value):
    try:
        settings.setValue(key, json.dumps(value, ensure_ascii=False))
    except Exception:
        pass


def _record_from_crs(crs: CRS) -> CRSRecord:
    d = describe_crs(crs)
    area = d.get("area") or {}
    auth = d.get("authority") or "CUSTOM"
    code = d.get("code") or "WKT"
    op = d.get("coordinate_operation") or {}
    return CRSRecord(
        authority=str(auth), code=str(code), name=str(d.get("name") or "Custom CRS"),
        crs_type=str(d.get("type") or "Other"), deprecated=False,
        projection_method=op.get("method"),
        west=area.get("west"), south=area.get("south"), east=area.get("east"), north=area.get("north"),
    )


class CRSSelectorDialog(QDialog):
    def __init__(self, parent=None, initial_crs=None, area_of_interest=None, title="Select Coordinate Reference System"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(980, 610)
        self.setMinimumSize(820, 540)
        _theme(self)

        self._settings = QSettings("NakshaAI", "LidarApp")
        self._area_of_interest = area_of_interest
        self._selected_crs: Optional[CRS] = parse_crs(initial_crs)
        self._selected_record: Optional[CRSRecord] = None
        self._rows: list[CRSRecord] = []
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(180)
        self._refresh_timer.timeout.connect(self._refresh_results)

        root = QVBoxLayout(self)
        compact_layout(root)

        top = QHBoxLayout()
        top.addWidget(QLabel("Search:"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("EPSG:32643, 32643, WGS 84 UTM 43N, Transverse Mercator...")
        self.search.setClearButtonEnabled(True)
        top.addWidget(self.search, 1)
        self.authority = QComboBox()
        self.authority.addItems(["All authorities", "EPSG", "ESRI"])
        top.addWidget(self.authority)
        self.extent_only = QCheckBox("Current project extent only")
        self.extent_only.setEnabled(self._area_of_interest is not None)
        top.addWidget(self.extent_only)
        root.addLayout(top)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.addWidget(QLabel("Coordinate systems"))
        self.categories = QListWidget()
        for label, key in _CATEGORIES:
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, key)
            self.categories.addItem(item)
        last_category = self._settings.value("projection/last_category", "utm", type=str)
        category_row = next(
            (i for i in range(self.categories.count())
             if self.categories.item(i).data(Qt.UserRole) == last_category),
            2,
        )
        self.categories.setCurrentRow(category_row)
        left_lay.addWidget(self.categories, 1)
        splitter.addWidget(left)

        center = QWidget()
        center_lay = QVBoxLayout(center)
        center_lay.setContentsMargins(0, 0, 0, 0)
        self.result_label = QLabel("")
        center_lay.addWidget(self.result_label)
        self.results = QTableWidget(0, 4)
        self.results.setHorizontalHeaderLabels(["Name", "Authority", "Code", "Type"])
        self.results.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results.setSelectionMode(QAbstractItemView.SingleSelection)
        self.results.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.results.verticalHeader().setVisible(False)
        compact_view(self.results)
        configure_header(self.results.horizontalHeader(), stretch_column=0)
        self.results.setColumnWidth(1, 82)
        self.results.setColumnWidth(2, 76)
        self.results.setColumnWidth(3, 145)
        center_lay.addWidget(self.results, 1)
        splitter.addWidget(center)

        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.addWidget(QLabel("Spatial reference details"))
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        right_lay.addWidget(self.details, 1)
        self.favorite_btn = QPushButton("☆ Add to Favorites")
        self.favorite_btn.clicked.connect(self._toggle_favorite)
        right_lay.addWidget(self.favorite_btn)
        splitter.addWidget(right)
        splitter.setSizes([215, 515, 290])

        tools = QHBoxLayout()
        self.import_dataset_btn = QPushButton("Import from Dataset...")
        self.import_prj_btn = QPushButton("Import .PRJ...")
        self.suggest_utm_btn = QPushButton("Suggest UTM from Project")
        self.suggest_utm_btn.setEnabled(self._area_of_interest is not None)
        tools.addWidget(self.import_dataset_btn)
        tools.addWidget(self.import_prj_btn)
        tools.addWidget(self.suggest_utm_btn)
        tools.addStretch(1)
        meta = proj_database_metadata()
        epsg_ver = meta.get("EPSG.VERSION") or "?"
        esri_ver = meta.get("ESRI.VERSION") or "?"
        tools.addWidget(QLabel(f"PROJ catalog · EPSG {epsg_ver} · ESRI {esri_ver}"))
        root.addLayout(tools)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_btn = buttons.button(QDialogButtonBox.Ok)
        self.ok_btn.setText("Select")
        self.ok_btn.setEnabled(self._selected_crs is not None)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.search.textChanged.connect(lambda _t: self._schedule_refresh())
        self.search.returnPressed.connect(self._search_entered)
        self.authority.currentIndexChanged.connect(lambda _i: self._refresh_results())
        self.extent_only.toggled.connect(lambda _v: self._refresh_results())
        self.categories.currentRowChanged.connect(lambda _i: self._refresh_results())
        self.results.itemSelectionChanged.connect(self._selection_changed)
        self.results.doubleClicked.connect(lambda _idx: self._accept())
        self.import_dataset_btn.clicked.connect(self._import_dataset)
        self.import_prj_btn.clicked.connect(self._import_prj)
        self.suggest_utm_btn.clicked.connect(self._suggest_utm)

        self._refresh_results()
        if self._selected_crs is not None:
            self._show_crs(self._selected_crs)
        geometry = self._settings.value("projection/selector_geometry")
        if geometry:
            self.restoreGeometry(geometry)

    @property
    def selected_crs(self) -> Optional[CRS]:
        return self._selected_crs

    def _schedule_refresh(self):
        self._refresh_timer.start()

    def _search_entered(self):
        self._refresh_timer.stop()
        self._refresh_results()
        if self.results.rowCount():
            self.results.selectRow(0)
            self.results.setFocus()

    def _favorites(self):
        vals = _read_json_setting(self._settings, "projection/favorites", ["EPSG:4326", "EPSG:3857", "EPSG:32643", "EPSG:32644"])
        return [str(v) for v in vals if v]

    def _recent(self):
        vals = _read_json_setting(self._settings, "projection/recent", [])
        return [str(v) for v in vals if v]

    def _special_rows(self, keys):
        rows = []
        for key in keys:
            try:
                crs = parse_crs(key)
                if crs is not None:
                    rows.append(_record_from_crs(crs))
            except Exception:
                pass
        return rows

    def _current_category(self):
        item = self.categories.currentItem()
        return str(item.data(Qt.UserRole) if item else "all")

    def _refresh_results(self):
        category = self._current_category()
        query = self.search.text().strip()
        auth_text = self.authority.currentText()
        authority = None if auth_text.startswith("All") else auth_text
        aoi = self._area_of_interest if self.extent_only.isChecked() else None

        if category == "favorites":
            rows = self._special_rows(self._favorites())
            if query:
                q = query.casefold()
                rows = [r for r in rows if q in f"{r.auth_code} {r.name}".casefold()]
        elif category == "recent":
            rows = self._special_rows(self._recent())
            if query:
                q = query.casefold()
                rows = [r for r in rows if q in f"{r.auth_code} {r.name}".casefold()]
        else:
            rows = search_crs(query, category=category, authority=authority,
                              area_of_interest=aoi, limit=2000)

        self._rows = rows
        self.results.setRowCount(len(rows))
        for r, rec in enumerate(rows):
            values = [rec.name, rec.authority, rec.code, rec.crs_type.replace("_", " ").title()]
            for c, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.UserRole, r)
                self.results.setItem(r, c, item)
        self.result_label.setText(f"{len(rows):,} coordinate system(s)" + (" matching current extent" if aoi is not None else ""))

        if rows:
            # Prefer the initial CRS when it appears, otherwise do not surprise
            # the user by silently accepting row 0.
            initial_id = crs_identifier(self._selected_crs) if self._selected_crs is not None else None
            match_row = None
            if initial_id:
                for i, rec in enumerate(rows):
                    if rec.auth_code.casefold() == initial_id.casefold():
                        match_row = i
                        break
            if match_row is not None:
                self.results.selectRow(match_row)

    def _selection_changed(self):
        rows = self.results.selectionModel().selectedRows()
        if not rows:
            return
        idx = rows[0].row()
        if idx < 0 or idx >= len(self._rows):
            return
        rec = self._rows[idx]
        try:
            crs = CRS.from_user_input(rec.auth_code)
        except Exception as exc:
            self.details.setPlainText(f"Could not open {rec.auth_code}: {exc}")
            return
        self._selected_record = rec
        self._selected_crs = crs
        self.ok_btn.setEnabled(True)
        self._show_crs(crs)

    def _show_crs(self, crs):
        d = describe_crs(crs)
        lines = [
            d.get("name") or "",
            d.get("identifier") or "",
            f"Type: {d.get('type') or '-'}",
        ]
        if d.get("datum"):
            lines.append(f"Datum: {d['datum']}")
        op = d.get("coordinate_operation") or {}
        if op:
            if op.get("method"):
                lines.append(f"Projection/Method: {op['method']}")
            elif op.get("name"):
                lines.append(f"Operation: {op['name']}")
        axis = d.get("axis") or []
        if axis:
            lines.append("Axes:")
            for a in axis:
                unit = f" [{a.get('unit')}]" if a.get("unit") else ""
                lines.append(f"  {a.get('abbrev') or a.get('name')} · {a.get('direction')}{unit}")
        area = d.get("area") or {}
        if area and area.get("name"):
            lines.append("")
            lines.append(f"Area of use: {area.get('name')}")
            if all(area.get(k) is not None for k in ("west", "south", "east", "north")):
                lines.append(f"Extent: {area['west']:.6g}, {area['south']:.6g} to {area['east']:.6g}, {area['north']:.6g}")
        subs = d.get("sub_crs") or []
        if subs:
            lines.append("")
            lines.append("Compound components:")
            for sub in subs:
                lines.append(f"  {sub['type']}: {sub['name']} ({sub['identifier']})")
        self.details.setPlainText("\n".join(lines))
        ident = crs_identifier(crs)
        self.favorite_btn.setText("★ Remove from Favorites" if ident in self._favorites() else "☆ Add to Favorites")

    def _toggle_favorite(self):
        if self._selected_crs is None:
            return
        ident = crs_identifier(self._selected_crs)
        vals = self._favorites()
        if ident in vals:
            vals = [v for v in vals if v != ident]
        else:
            vals.insert(0, ident)
        _write_json_setting(self._settings, "projection/favorites", vals[:100])
        self._show_crs(self._selected_crs)
        if self._current_category() == "favorites":
            self._refresh_results()

    def _remember_recent(self, crs):
        ident = crs_identifier(crs)
        vals = [v for v in self._recent() if v != ident]
        vals.insert(0, ident)
        _write_json_setting(self._settings, "projection/recent", vals[:30])

    def _accept(self):
        if self._selected_crs is None:
            QMessageBox.warning(self, "Coordinate System", "Select a coordinate reference system first.")
            return
        self._remember_recent(self._selected_crs)
        self._settings.setValue("projection/last_category", self._current_category())
        self.accept()

    def closeEvent(self, event):
        self._settings.setValue("projection/selector_geometry", self.saveGeometry())
        self._settings.setValue("projection/last_category", self._current_category())
        super().closeEvent(event)

    def _import_prj(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import Coordinate System from PRJ", "", "Projection files (*.prj *.PRJ);;All files (*)")
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore").strip()
            crs = CRS.from_user_input(text)
        except Exception as exc:
            QMessageBox.warning(self, "Import PRJ", f"Could not read the coordinate system:\n\n{exc}")
            return
        self._selected_crs = crs
        self._selected_record = _record_from_crs(crs)
        self.ok_btn.setEnabled(True)
        self._show_crs(crs)

    def _import_dataset(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Coordinate System from Dataset", "",
            "GIS / Survey data (*.shp *.geojson *.json *.gpkg *.tif *.tiff *.las *.laz *.snt *.dxf *.dwg *.prj);;All files (*)"
        )
        if not path:
            return
        crs = None
        label = None
        try:
            from gui.crs_manager import resolve_point_cloud_crs, resolve_snt_crs, resolve_gis_crs, resolve_prj_crs
            ext = Path(path).suffix.lower()
            if ext in {".las", ".laz"}:
                crs, label = resolve_point_cloud_crs(path)
            elif ext in {".snt", ".dgn"}:
                crs, label = resolve_snt_crs(path)
            elif ext == ".prj":
                crs, label = resolve_prj_crs(path)
            else:
                crs, label = resolve_gis_crs(path)
        except Exception as exc:
            QMessageBox.warning(self, "Import Coordinate System", str(exc))
            return
        if crs is None:
            QMessageBox.warning(self, "Import Coordinate System", "No machine-readable coordinate system was found in that dataset.")
            return
        self._selected_crs = crs
        self._selected_record = _record_from_crs(crs)
        self.ok_btn.setEnabled(True)
        self._show_crs(crs)
        self.result_label.setText(f"Imported from dataset: {label or os.path.basename(path)}")

    def _suggest_utm(self):
        aoi = self._area_of_interest
        if aoi is None:
            return
        lon = (aoi.west_lon_degree + aoi.east_lon_degree) * 0.5
        lat = (aoi.south_lat_degree + aoi.north_lat_degree) * 0.5
        rows = suggest_utm_crs(lon, lat)
        if not rows:
            QMessageBox.information(self, "UTM Suggestion", "No UTM CRS was found for the current project extent.")
            return
        self.categories.setCurrentRow(2)
        self.search.setText(rows[0].auth_code)
        self._refresh_results()
        if self._rows:
            self.results.selectRow(0)

    @staticmethod
    def select_crs(parent=None, initial_crs=None, area_of_interest=None, title="Select Coordinate Reference System") -> Optional[CRS]:
        dlg = CRSSelectorDialog(parent, initial_crs=initial_crs, area_of_interest=area_of_interest, title=title)
        if dlg.exec() == QDialog.Accepted:
            return dlg.selected_crs
        return None


def choose_crs(parent=None, initial_crs=None, area_of_interest=None, title="Select Coordinate Reference System") -> Optional[CRS]:
    return CRSSelectorDialog.select_crs(parent, initial_crs=initial_crs, area_of_interest=area_of_interest, title=title)
