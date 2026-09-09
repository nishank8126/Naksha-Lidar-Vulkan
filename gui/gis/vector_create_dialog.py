"""Catalog dialog for creating standalone vector GIS files."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout,
)

from .catalog_events import catalog_events
from .gdb.schema_dialogs import _CRSPicker, _parent_project_crs
from .gdb.writer import FieldSpec
from .gis_style import apply_gis_dialog_style, compact_layout, compact_view, configure_header
from .vector_writer import VECTOR_FORMATS, create_vector_dataset


class NewVectorDatasetDialog(QDialog):
    def __init__(self, parent, folder: str, format_key: str):
        super().__init__(parent)
        self.folder = os.path.abspath(folder)
        self.format_key = format_key
        self.result = None
        self.is_spatial = format_key != "dbase"
        fmt = VECTOR_FORMATS[format_key]
        self.setWindowTitle(f"New {fmt.label}")
        self.resize(650, 450)
        apply_gis_dialog_style(self)

        root = QVBoxLayout(self)
        compact_layout(root)
        form = QFormLayout()

        self.folder_label = QLabel(self.folder)
        self.folder_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.name_edit = QLineEdit(f"New_Layer{fmt.extension}")
        self.geometry_combo = QComboBox()
        self.geometry_combo.addItems([
            "Point", "MultiPoint", "Polyline", "MultiLineString",
            "Polygon", "MultiPolygon",
        ])
        if not self.is_spatial:
            self.geometry_combo.clear()
            self.geometry_combo.addItem("None (attribute table)")
            self.geometry_combo.setEnabled(False)
        self.crs_picker = _CRSPicker(self, _parent_project_crs(parent))
        if not self.is_spatial:
            self.crs_picker.setEnabled(False)

        form.addRow("Folder:", self.folder_label)
        form.addRow("File name:", self.name_edit)
        form.addRow("Format:", QLabel(fmt.label))
        form.addRow("Geometry type:", self.geometry_combo)
        form.addRow("Coordinate system:", self.crs_picker)
        root.addLayout(form)

        note = QLabel(
            "Select the real coordinate system for this dataset. Use the CRS "
            "browser for UTM, projected, geographic, EPSG/ESRI, PRJ, or an "
            "existing dataset's coordinate system."
            if self.is_spatial else
            "dBASE tables store attributes only and do not have a coordinate system."
        )
        note.setWordWrap(True)
        note.setProperty("secondary", True)
        root.addWidget(note)

        root.addWidget(QLabel("Attribute fields:"))
        self.fields = QTableWidget(0, 3)
        self.fields.setHorizontalHeaderLabels(["Name", "Type", "Length"])
        compact_view(self.fields)
        configure_header(self.fields.horizontalHeader(), stretch_column=0)
        root.addWidget(self.fields, 1)

        field_actions = QHBoxLayout()
        add_field = QPushButton("Add Field")
        remove_field = QPushButton("Remove Field")
        add_field.clicked.connect(self._add_field)
        remove_field.clicked.connect(self._remove_fields)
        field_actions.addWidget(add_field)
        field_actions.addWidget(remove_field)
        field_actions.addStretch(1)
        root.addLayout(field_actions)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Create")
        buttons.accepted.connect(self._create)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _add_field(self, name="", field_type="String", width=""):
        row = self.fields.rowCount()
        self.fields.insertRow(row)
        for column, value in enumerate((name, field_type, width)):
            self.fields.setItem(row, column, QTableWidgetItem(str(value)))

    def _remove_fields(self):
        rows = sorted({index.row() for index in self.fields.selectedIndexes()}, reverse=True)
        for row in rows:
            self.fields.removeRow(row)

    def _field_specs(self):
        allowed = {"String", "Integer", "Integer64", "Real", "Date", "DateTime"}
        specs = []
        names = set()
        for row in range(self.fields.rowCount()):
            values = []
            for column in range(3):
                item = self.fields.item(row, column)
                values.append(item.text().strip() if item else "")
            name, field_type, width_text = values
            if not name:
                continue
            folded = name.casefold()
            if folded in names:
                raise ValueError(f"Duplicate field name: {name}")
            names.add(folded)
            normalized_type = next((x for x in allowed if x.casefold() == field_type.casefold()), None)
            if normalized_type is None:
                raise ValueError(
                    f"Unsupported field type '{field_type}'. Use: " + ", ".join(sorted(allowed))
                )
            try:
                width = max(0, int(width_text or 0))
            except ValueError:
                raise ValueError(f"Field '{name}' has an invalid length.")
            specs.append(FieldSpec(name=name, type=normalized_type, width=width))
        return specs

    def _create(self):
        fmt = VECTOR_FORMATS[self.format_key]
        filename = self.name_edit.text().strip()
        if not filename:
            QMessageBox.warning(self, self.windowTitle(), "Enter a file name.")
            return
        if Path(filename).name != filename:
            QMessageBox.warning(self, self.windowTitle(), "Enter a file name without a folder path.")
            return
        if not filename.casefold().endswith(fmt.extension):
            filename += fmt.extension
        try:
            fields = self._field_specs()
        except ValueError as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        crs = self.crs_picker.crs() if self.is_spatial else None
        if self.is_spatial and crs is None:
            QMessageBox.warning(self, self.windowTitle(), "Select a coordinate reference system.")
            return

        self.result = create_vector_dataset(
            os.path.join(self.folder, filename),
            format_key=self.format_key,
            layer_name=Path(filename).stem,
            geometry=self.geometry_combo.currentText() if self.is_spatial else "None",
            crs=crs,
            fields=fields,
        )
        if not self.result.success:
            QMessageBox.warning(self, self.windowTitle(), self.result.error or "Creation failed.")
            return

        warning_text = ""
        if self.result.warnings:
            warning_text = "\n\nWarnings:\n- " + "\n- ".join(self.result.warnings)
        QMessageBox.information(
            self,
            self.windowTitle(),
            f"Created successfully:\n{self.result.path}{warning_text}",
        )
        catalog_events().datasourceCreated.emit(self.result.path)
        self.accept()


def create_vector_dataset_dialog(parent, folder: str, format_key: str):
    dialog = NewVectorDatasetDialog(parent, folder, format_key)
    dialog.exec()
    return dialog.result