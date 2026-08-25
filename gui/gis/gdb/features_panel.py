# ─────────────────────────────────────────────────────────────────────────────
# gui/gis/gdb/features_panel.py — Create Features panel
#
# PySide6 dialog for creating new features in a GDB feature class.
# Shows a form with:
#   - Layer selector (all feature classes in the GDB)
#   - Subtype selector (for cascaded layers)
#   - Domain-aware field editors (QComboBox for coded values, range fields)
#   - Non-nullable fields with defaults from the engine
#   - Geometry input via coordinate text entry (one vertex per line: "X Y" or "X Y Z")
#   - or pickup from the most recent digitizer drawing
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import logging
import re

from PySide6.QtCore import Qt, QRegularExpression
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QComboBox, QTextEdit, QGroupBox, QFormLayout, QWidget, QScrollArea,
    QMessageBox, QCheckBox, QSizePolicy, QFrame, QSplitter,
    QRadioButton, QButtonGroup,
)
from PySide6.QtGui import QIntValidator, QDoubleValidator, QRegularExpressionValidator

from .engine import GDBEngine, get_engine
from . import reader as gdb_reader

log = logging.getLogger("GDBEngine")


class CreateFeaturesPanel(QDialog):
    """Dialog for creating new features in a GDB feature class.

    Parameters
    ----------
    app : QMainWindow
        Main app (for the digitizer and renderer).
    engine : GDBEngine
        Parsed GDB schema.
    gdb_path : str
        Path to the .gdb folder.
    """

    def __init__(self, app, engine: GDBEngine, gdb_path: str, parent=None):
        super().__init__(parent)
        self.app = app
        self.engine = engine
        self.gdb_path = gdb_path
        self._layer_names = []
        self._field_widgets = {}   # field_name_lower -> QWidget
        self._domain_map = {}      # field_name -> domain_name
        self._cascade = None       # current cascade data

        gdb_name = __import__("os").path.basename(gdb_path)
        self.setWindowTitle(f"Create Features — {gdb_name}")
        self.setMinimumSize(500, 620)
        self.resize(540, 700)
        try:
            from gui.theme_manager import get_dialog_stylesheet
            self.setStyleSheet(get_dialog_stylesheet())
        except Exception:
            pass
        self._setup_ui()
        self._populate_layers()

    # ── UI ────────────────────────────────────────────────────────────────

    def _setup_ui(self):
        main = QVBoxLayout(self)
        main.setContentsMargins(10, 10, 10, 10)
        main.setSpacing(8)

        # Title
        title = QLabel("Create Feature")
        title.setStyleSheet("font-size:14pt; font-weight:600; color:#e8eaed;")
        main.addWidget(title)

        # Layer selector
        layer_row = QHBoxLayout()
        layer_row.addWidget(QLabel("Target layer:"))
        self.cmb_layer = QComboBox()
        layer_row.addWidget(self.cmb_layer, 1)
        main.addLayout(layer_row)

        # Subtype selector (shown only for cascaded layers)
        self.subtype_group = QGroupBox("Subtype")
        self.subtype_group.setVisible(False)
        sf = QFormLayout(self.subtype_group)
        self.cmb_subtype = QComboBox()
        sf.addRow("Subtype:", self.cmb_subtype)
        self.lbl_subtype_desc = QLabel("")
        self.lbl_subtype_desc.setWordWrap(True)
        sf.addRow(self.lbl_subtype_desc)
        main.addWidget(self.subtype_group)

        # Fields scroll area
        fields_box = QGroupBox("Fields")
        fields_scroll = QScrollArea()
        fields_scroll.setWidgetResizable(True)
        fields_scroll.setFrameShape(QFrame.NoFrame)
        self._fields_widget = QWidget()
        self._fields_layout = QFormLayout(self._fields_widget)
        self._fields_layout.setSpacing(6)
        fields_scroll.setWidget(self._fields_widget)
        fields_v = QVBoxLayout(fields_box)
        fields_v.setContentsMargins(6, 6, 6, 6)
        fields_v.addWidget(fields_scroll, 1)
        main.addWidget(fields_box, 1)

        # Geometry input
        geom_box = QGroupBox("Geometry")
        gb = QVBoxLayout(geom_box)
        gb.setSpacing(6)

        # Mode selector
        mode_row = QHBoxLayout()
        self._mode_group = QButtonGroup()
        self.rb_manual = QRadioButton("Manual coordinates")
        self.rb_manual.setChecked(True)
        self.rb_pickup = QRadioButton("Pick from last drawing")
        self._mode_group.addButton(self.rb_manual, 0)
        self._mode_group.addButton(self.rb_pickup, 1)
        mode_row.addWidget(self.rb_manual)
        mode_row.addWidget(self.rb_pickup)
        mode_row.addStretch()
        gb.addLayout(mode_row)

        # Geometry type selector
        geom_type_row = QHBoxLayout()
        geom_type_row.addWidget(QLabel("Geometry type:"))
        self.cmb_geom_type = QComboBox()
        self.cmb_geom_type.addItems(["Polygon", "LineString", "Point"])
        geom_type_row.addWidget(self.cmb_geom_type)
        geom_type_row.addStretch()
        gb.addLayout(geom_type_row)

        # Manual coordinates editor
        self._coord_widget = QWidget()
        cw = QVBoxLayout(self._coord_widget)
        cw.setContentsMargins(0, 0, 0, 0)
        cw.setSpacing(4)
        cw.addWidget(QLabel(
            "Enter one vertex per line — format: X Y  or  X Y Z"))
        cw.addWidget(QLabel(
            "Polygon: last vertex auto-closes to first.  "
            "Leave empty for points."))
        self.txt_coords = QTextEdit()
        self.txt_coords.setPlaceholderText(
            "250000.0 3800000.0\n"
            "250100.0 3800000.0\n"
            "250100.0 3800100.0\n"
            "250000.0 3800100.0"
        )
        self.txt_coords.setLineWrapMode(QTextEdit.NoWrap)
        cw.addWidget(self.txt_coords, 1)
        gb.addWidget(self._coord_widget, 1)

        self._pickup_widget = QLabel(
            "Click Create to digitise on the 3D view, then click 'Create' again "
            "to save the geometry.")
        self._pickup_widget.setWordWrap(True)
        self._pickup_widget.setVisible(False)
        gb.addWidget(self._pickup_widget, 1)

        main.addWidget(geom_box, 1)

        # Create button
        btn_row = QHBoxLayout()
        self.btn_create = QPushButton("  Create feature")
        self.btn_create.setStyleSheet(
            "background:#3b5bdb; color:white; font-weight:600; padding:8px 20px; "
            "border:none; border-radius:6px; font-size:10pt;")
        self.btn_create.setCursor(Qt.PointingHandCursor)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.setCursor(Qt.PointingHandCursor)
        btn_row.addStretch()
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(self.btn_create)
        main.addLayout(btn_row)

        # Status
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color:#7f8794; font-size:8pt;")
        self.lbl_status.setWordWrap(True)
        main.addWidget(self.lbl_status)

        # Wire
        self.cmb_layer.currentIndexChanged.connect(self._on_layer_changed)
        self.cmb_subtype.currentIndexChanged.connect(self._on_subtype_changed)
        self.rb_manual.toggled.connect(self._on_mode_changed)
        self.btn_create.clicked.connect(self._do_create)
        btn_cancel.clicked.connect(self.close)

    # ── Layer population ────────────────────────────────────────────────

    def _populate_layers(self):
        self.cmb_layer.blockSignals(True)
        self.cmb_layer.clear()
        self._layer_names = [
            n for n in gdb_reader.list_layer_names(self.gdb_path)
            if gdb_reader.geometry_kind(
                self._get_geom_type(n)) != "none"
        ]
        for name in self._layer_names:
            kind = gdb_reader.geometry_kind(self._get_geom_type(name))
            cascaded = self.engine.is_cascaded_layer(name)
            label = name
            if cascaded:
                label += "  ⛓"
            if kind == "point":
                label = f"●  {label}"
            elif kind == "line":
                label = f"╱  {label}"
            elif kind == "polygon":
                label = f"▭  {label}"
            self.cmb_layer.addItem(label, name)
        self.cmb_layer.blockSignals(False)
        if self.cmb_layer.count() > 0:
            self.cmb_layer.setCurrentIndex(0)

    def _get_geom_type(self, layer_name: str) -> int:
        ds = gdb_reader.open_gdb_for_read(self.gdb_path)
        if not ds:
            return 0
        try:
            lyr = gdb_reader.get_layer(ds, layer_name)
            if lyr is None:
                return 0
            defn = lyr.GetLayerDefn()
            return defn.GetGeomType() if defn else 0
        finally:
            ds = None

    def _on_layer_changed(self, _idx):
        name = self.cmb_layer.currentData()
        if not name:
            return
        self._cascade = self.engine.get_cascade_data(name)
        self._populate_fields(name)
        self._populate_subtype(name)
        # Update geom type combo
        kind = gdb_reader.geometry_kind(self._get_geom_type(name))
        idx_map = {"polygon": 0, "line": 1, "point": 2}
        self.cmb_geom_type.setCurrentIndex(idx_map.get(kind, 0))

    # ── Subtype ────────────────────────────────────────────────────────

    def _populate_subtype(self, layer_name: str):
        if self._cascade:
            self.subtype_group.setVisible(True)
            self.cmb_subtype.blockSignals(True)
            self.cmb_subtype.clear()
            for code, label in self._cascade["subtype_domain"].items():
                self.cmb_subtype.addItem(f"{label} ({code})", code)
            self.cmb_subtype.blockSignals(False)
            if self.cmb_subtype.count() > 0:
                self.cmb_subtype.setCurrentIndex(0)
            self._on_subtype_changed()
        else:
            self.subtype_group.setVisible(False)
            self._cascade = None

    def _on_subtype_changed(self):
        if not self._cascade:
            return
        code = self.cmb_subtype.currentData()
        if code is None:
            return
        # Update dependent field combos
        dep_domains = self._cascade["dependent_field_domains"]
        for field_name, subtype_map in dep_domains.items():
            domain_name = subtype_map.get(code)
            if not domain_name:
                continue
            w = self._field_widgets.get(field_name.lower())
            if isinstance(w, QComboBox):
                w.clear()
                domain_vals = self.engine.all_domains.get(domain_name, {})
                for val, desc in domain_vals.items():
                    w.addItem(f"{desc} ({val})", val)
            elif isinstance(w, QLineEdit):
                # For non-domain fields that are dependent, show info
                pass
        # Apply subtype-specific default values, when present in GDB XML.
        subtype_defaults = self._cascade.get("subtype_default_values", {}) if self._cascade else {}
        for fname_lower, default_value in subtype_defaults.get(str(code), {}).items():
            w = self._field_widgets.get(fname_lower.lower())
            if isinstance(w, QLineEdit):
                w.setText(str(default_value))
            elif isinstance(w, QComboBox):
                idx = w.findData(default_value)
                if idx < 0:
                    idx = w.findData(str(default_value))
                if idx >= 0:
                    w.setCurrentIndex(idx)

        # Update description
        desc_parts = []
        for field_name, subtype_map in dep_domains.items():
            domain_name = subtype_map.get(code, "")
            if domain_name:
                desc_parts.append(f"  {field_name} → {domain_name}")
        self.lbl_subtype_desc.setText("\n".join(desc_parts) if desc_parts else "")

    # ── Fields ────────────────────────────────────────────────────────

    def _populate_fields(self, layer_name: str):
        # Clear old widgets
        while self._fields_layout.rowCount() > 0:
            self._fields_layout.removeRow(0)
        self._field_widgets.clear()
        self._domain_map.clear()

        if not layer_name:
            return

        # Get field list from OGR
        ds = gdb_reader.open_gdb_for_read(self.gdb_path)
        if not ds:
            return
        try:
            lyr = gdb_reader.get_layer(ds, layer_name)
            if lyr is None:
                return
            defn = lyr.GetLayerDefn()
            if defn is None:
                return
        finally:
            ds = None

        nn_fields = self.engine.get_non_nullable_fields(layer_name)
        field_types = self.engine.get_field_types(layer_name)

        for i in range(defn.GetFieldCount()):
            fdefn = defn.GetFieldDefn(i)
            fname = fdefn.GetName()
            fname_lower = fname.lower()
            # Skip system fields
            if fname_lower in ("objectid", "shape", "shape_length",
                               "shape_area", "globalid", "fid", "oid"):
                continue

            try:
                nullable = bool(fdefn.IsNullable())
            except Exception:
                nullable = True

            esri_type = field_types.get(fname_lower, "")
            domain_name = self.engine.get_domain_for_field(
                layer_name, layer_name, fname)
            self._domain_map[fname_lower] = domain_name

            # Build the right widget
            if domain_name:
                # Domain field: QComboBox
                combo = QComboBox()
                combo.setEditable(False)
                domain_vals = self.engine.all_domains.get(domain_name, {})
                for val, desc in domain_vals.items():
                    combo.addItem(f"{desc} ({val})", val)
                # Pre-select first value for non-nullable
                if not nullable and combo.count() > 0:
                    combo.setCurrentIndex(0)
                self._field_widgets[fname_lower] = combo
                label = fname
                if not nullable:
                    label += " *"
                self._fields_layout.addRow(label, combo)
            elif esri_type in ("esriFieldTypeString", "esriFieldTypeGUID",
                               "esriFieldTypeXML"):
                le = QLineEdit()
                le.setPlaceholderText(f"String ({esri_type})")
                self._field_widgets[fname_lower] = le
                label = fname
                if not nullable:
                    label += " *"
                self._fields_layout.addRow(label, le)
            elif esri_type in ("esriFieldTypeDouble", "esriFieldTypeSingle"):
                le = QLineEdit()
                le.setValidator(QDoubleValidator())
                le.setPlaceholderText(f"Number ({esri_type})")
                self._field_widgets[fname_lower] = le
                label = fname
                if not nullable:
                    label += " *"
                self._fields_layout.addRow(label, le)
            elif esri_type in ("esriFieldTypeInteger", "esriFieldTypeSmallInteger",
                               "esriFieldTypeOID"):
                le = QLineEdit()
                le.setValidator(QIntValidator())
                le.setPlaceholderText(f"Integer ({esri_type})")
                self._field_widgets[fname_lower] = le
                label = fname
                if not nullable:
                    label += " *"
                self._fields_layout.addRow(label, le)
            elif esri_type == "esriFieldTypeBigInteger":
                le = QLineEdit()
                le.setValidator(QRegularExpressionValidator(QRegularExpression(r"[-+]?\d{0,19}")))
                le.setPlaceholderText("64-bit integer / safe ArcGIS value")
                self._field_widgets[fname_lower] = le
                label = fname
                if not nullable:
                    label += " *"
                self._fields_layout.addRow(label, le)
            elif esri_type in ("esriFieldTypeDate", "esriFieldTypeDateOnly",
                               "esriFieldTypeTimeOnly", "esriFieldTypeTimestampOffset"):
                le = QLineEdit()
                if esri_type == "esriFieldTypeDateOnly":
                    le.setPlaceholderText("YYYY-MM-DD")
                elif esri_type == "esriFieldTypeTimeOnly":
                    le.setPlaceholderText("HH:MM:SS")
                elif esri_type == "esriFieldTypeTimestampOffset":
                    le.setPlaceholderText("YYYY-MM-DD HH:MM:SS+05:30")
                else:
                    le.setPlaceholderText("YYYY-MM-DD or YYYY-MM-DD HH:MM:SS")
                self._field_widgets[fname_lower] = le
                label = fname
                if not nullable:
                    label += " *"
                self._fields_layout.addRow(label, le)
            elif esri_type == "esriFieldTypeGeometry":
                continue  # handled separately
            else:
                le = QLineEdit()
                le.setPlaceholderText(esri_type or "Text")
                self._field_widgets[fname_lower] = le
                label = fname
                if not nullable:
                    label += " *"
                self._fields_layout.addRow(label, le)

        # Pre-fill layer-level schema defaults from the GDB XML.
        try:
            defaults = self.engine.get_field_defaults(layer_name)
        except Exception:
            defaults = {}
        for fname_lower, default_value in defaults.items():
            w = self._field_widgets.get(fname_lower)
            if isinstance(w, QLineEdit) and not w.text():
                w.setText(str(default_value))
            elif isinstance(w, QComboBox):
                idx = w.findData(default_value)
                if idx < 0:
                    idx = w.findData(str(default_value))
                if idx >= 0:
                    w.setCurrentIndex(idx)

    # ── Geometry mode ────────────────────────────────────────────────

    def _on_mode_changed(self, checked):
        is_manual = self.rb_manual.isChecked()
        self._coord_widget.setVisible(is_manual)
        self._pickup_widget.setVisible(not is_manual)

    # ── Create ────────────────────────────────────────────────────────

    def _do_create(self):
        layer_name = self.cmb_layer.currentData()
        if not layer_name:
            QMessageBox.warning(self, "Create", "Select a target layer first.")
            return

        # Collect attributes
        attributes = {}
        nn_defaults = self.engine.get_non_nullable_default_values(layer_name)
        for fname_lower, widget in self._field_widgets.items():
            if isinstance(widget, QComboBox):
                val = widget.currentData()
                if val is not None:
                    attributes[fname_lower] = str(val)
            elif isinstance(widget, QLineEdit):
                text = widget.text().strip()
                if text:
                    attributes[fname_lower] = text

        # Apply non-nullable defaults for empty fields
        nn_fields = self.engine.get_non_nullable_fields(layer_name)
        for fname_lower in nn_fields:
            if fname_lower not in attributes and fname_lower in nn_defaults:
                attributes[fname_lower] = nn_defaults[fname_lower]

        missing = []
        for fname_lower in self.engine.get_non_nullable_fields(layer_name):
            if fname_lower in attributes:
                continue
            w = self._field_widgets.get(fname_lower)
            if isinstance(w, QComboBox) and w.currentData() is not None:
                continue
            missing.append(fname_lower)
        if missing:
            QMessageBox.warning(
                self, "Create",
                "Fill required non-nullable field(s): " + ", ".join(sorted(missing)))
            return

        # Collect geometry
        geom_type_name = self.cmb_geom_type.currentText()
        geom_type_map = {
            "Polygon": 3,  # ogr.wkbPolygon
            "LineString": 2,  # ogr.wkbLineString
            "Point": 1,  # ogr.wkbPoint
        }
        ogr_geom_type = geom_type_map.get(geom_type_name, 3)

        coords = self._collect_coords()
        if coords is None:
            QMessageBox.warning(self, "Create",
                                "Enter at least one vertex coordinate.")
            return

        if ogr_geom_type == 3 and len(coords) < 3:
            QMessageBox.warning(self, "Create",
                                "Polygon needs at least 3 vertices.")
            return
        if ogr_geom_type == 2 and len(coords) < 2:
            QMessageBox.warning(self, "Create",
                                "Line needs at least 2 vertices.")
            return

        # Create
        try:
            fid = gdb_reader.create_feature(
                self.gdb_path, layer_name, attributes,
                coords, ogr_geom_type)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            QMessageBox.warning(self, "Create", f"Error:\n{exc}")
            return

        if fid < 0:
            QMessageBox.warning(self, "Create",
                                "Feature creation failed. Check non-nullable "
                                "fields and geometry coordinates.")
            return

        self.lbl_status.setText(f"Feature {fid} created in {layer_name}")

        # Refresh on-screen actor
        try:
            gdb_reader.reload_gdb_layer(self.app, self.gdb_path, layer_name)
        except Exception:
            pass

        QMessageBox.information(
            self, "Create",
            f"Feature {fid} created successfully in {layer_name}.")

    def _collect_coords(self) -> list | None:
        """Parse coordinate text into a list of (x, y) or (x, y, z)."""
        if self.rb_pickup.isChecked():
            return None  # will use digitizer in future
        text = self.txt_coords.toPlainText().strip()
        if not text:
            return []
        coords = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            parts = re.split(r'[\s,;\t]+', line)
            try:
                if len(parts) >= 3:
                    coords.append((float(parts[0]), float(parts[1]), float(parts[2])))
                elif len(parts) >= 2:
                    coords.append((float(parts[0]), float(parts[1]), 0.0))
            except (ValueError, IndexError):
                continue
        return coords
