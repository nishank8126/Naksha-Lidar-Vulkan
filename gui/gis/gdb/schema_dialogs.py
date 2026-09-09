"""ArcCatalog-style FileGDB creation dialogs for Naksha.

These dialogs are intentionally thin: all schema work is delegated to
``gui.gis.gdb.writer`` so the same code can be reused by the Catalog Browser,
other ribbon commands and automated tests.
"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .writer import FieldSpec, create_feature_class, create_feature_dataset, create_file_gdb, create_table
from gui.gis.catalog_events import notify_created
from gui.gis.gis_style import apply_gis_dialog_style, compact_layout, compact_view, configure_header


def _parent_project_crs(parent):
    try:
        from gui.crs_manager import get_canvas_crs
        app = parent
        # Dialogs are often parented directly to NakshaApp; walk one level if needed.
        for _ in range(4):
            crs = get_canvas_crs(app)
            if crs is not None:
                return crs
            app = app.parent() if hasattr(app, "parent") and callable(app.parent) else None
            if app is None:
                break
    except Exception:
        pass
    return None


def _feature_dataset_crs(gdb_path: str, feature_dataset: str | None):
    if not feature_dataset:
        return None

    # OpenFileGDB may not persist an *empty* Feature Dataset.  During the
    # current Naksha session the writer keeps an explicit pending descriptor so
    # the first Feature Class can still inherit the intended CRS correctly.
    try:
        from .pending import get_pending
        from gui.projection_engine import parse_crs
        pending = get_pending(gdb_path, feature_dataset)
        if pending is not None and pending.get("crs"):
            return parse_crs(pending.get("crs"))
    except Exception:
        pass

    try:
        from gui.gis.catalog_inspector import inspect_source
        from gui.projection_engine import parse_crs
        meta = inspect_source(gdb_path, include_fields=False)
        for layer in meta.get("layers") or []:
            if str(layer.get("group") or "").casefold() != feature_dataset.casefold():
                continue
            crs = layer.get("crs") or {}
            value = layer.get("native_crs_wkt") or crs.get("wkt")
            if value:
                return parse_crs(value)
    except Exception:
        pass
    return None


class _CRSPicker(QWidget):
    """Compact reusable CRS row backed by Naksha's PROJ database selector."""
    def __init__(self, parent=None, initial_crs=None):
        super().__init__(parent)
        self._crs = initial_crs
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.label = QLabel()
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.button = QPushButton("Select CRS...")
        self.button.clicked.connect(self._pick)
        lay.addWidget(self.label, 1)
        lay.addWidget(self.button)
        self._refresh()

    def _pick(self):
        try:
            from gui.crs_selector_dialog import choose_crs
            picked = choose_crs(self, initial_crs=self._crs, title="Select Coordinate Reference System")
            if picked is not None:
                self._crs = picked
                self._refresh()
        except Exception as exc:
            QMessageBox.warning(self, "Coordinate System", str(exc))

    def _refresh(self):
        if self._crs is None:
            self.label.setText("Not set")
            self.label.setToolTip("No coordinate system selected")
            return
        try:
            from gui.projection_engine import crs_identifier
            self.label.setText(f"{self._crs.name}  ({crs_identifier(self._crs)})")
            self.label.setToolTip(self._crs.to_wkt())
        except Exception:
            self.label.setText(str(self._crs))

    def crs(self):
        return self._crs

    def user_input(self):
        if self._crs is None:
            return None
        try:
            return self._crs.to_wkt()
        except Exception:
            return str(self._crs)


def _theme(dialog):
    apply_gis_dialog_style(dialog)


def _message_result(parent, title, result):
    if result.success:
        extra = ""
        if result.warnings:
            extra = "\n\nWarnings:\n- " + "\n- ".join(result.warnings)
        if bool(getattr(result, "pending", False)):
            QMessageBox.information(
                parent,
                title,
                "Feature Dataset is pending for this Naksha session.\n\n"
                f"{result.path}\n{result.name}\n\n"
                "The installed OpenFileGDB runtime does not persist an empty "
                "Feature Dataset. Naksha will materialize it on disk when its "
                "first Feature Class is created."
                f"{extra}",
            )
        else:
            QMessageBox.information(parent, title, f"Created successfully:\n{result.path}\n{result.name}{extra}")
        return True
    text = result.error or "Operation failed."
    if result.warnings:
        text += "\n\nWarnings:\n- " + "\n- ".join(result.warnings)
    QMessageBox.warning(parent, title, text)
    return False


def create_file_gdb_dialog(parent, folder: str | None = None):
    if not folder or not os.path.isdir(folder):
        folder = QFileDialog.getExistingDirectory(parent, "Choose Folder for File Geodatabase")
        if not folder:
            return None
    name, ok = QInputDialog.getText(parent, "New File Geodatabase", "Name:", text="New File Geodatabase")
    if not ok or not name.strip():
        return None
    name = name.strip()
    if not name.lower().endswith(".gdb"):
        name += ".gdb"
    result = create_file_gdb(os.path.join(folder, name), overwrite=False)
    if _message_result(parent, "New File Geodatabase", result):
        notify_created(result, kind="datasource")
    return result


class FeatureDatasetDialog(QDialog):
    def __init__(self, parent, gdb_path: str, feature_dataset: str | None = None):
        super().__init__(parent)
        self.gdb_path = gdb_path
        self.feature_dataset = feature_dataset
        self.result = None
        self.setWindowTitle("New Feature Dataset")
        self.setMinimumWidth(440)
        _theme(self)

        root = QVBoxLayout(self)
        compact_layout(root)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("UTILITIES")
        inherited_crs = _feature_dataset_crs(gdb_path, feature_dataset)
        self.crs_picker = _CRSPicker(
            self, inherited_crs or _parent_project_crs(parent)
        )
        if feature_dataset:
            self.crs_picker.button.setEnabled(False)
            self.crs_picker.button.setToolTip(
                "Feature classes inside a Feature Dataset inherit its coordinate system."
            )
        form.addRow("Name:", self.name_edit)
        form.addRow("Coordinate system:", self.crs_picker)
        root.addLayout(form)

        note = QLabel("All feature classes in this dataset inherit this coordinate system.")
        note.setWordWrap(True)
        note.setProperty("secondary", True)
        root.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._create)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _create(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "New Feature Dataset", "Enter a feature dataset name.")
            return
        crs = self.crs_picker.user_input()
        if crs is None:
            QMessageBox.warning(self, "New Feature Dataset", "Select a coordinate reference system.")
            return
        self.result = create_feature_dataset(self.gdb_path, name, crs=crs)
        if _message_result(self, "New Feature Dataset", self.result):
            notify_created(self.result, kind="feature_dataset", container=self.gdb_path)
            self.accept()


def create_feature_dataset_dialog(parent, gdb_path: str, feature_dataset: str | None = None):
    dlg = FeatureDatasetDialog(parent, gdb_path, feature_dataset)
    dlg.exec()
    return dlg.result


class FeatureClassDialog(QDialog):
    def __init__(self, parent, gdb_path: str, feature_dataset: str | None = None):
        super().__init__(parent)
        self.gdb_path = gdb_path
        self.result = None
        self.setWindowTitle("New Feature Class")
        self.resize(700, 475)
        _theme(self)

        root = QVBoxLayout(self)
        compact_layout(root)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.alias_edit = QLineEdit()
        self.geometry_combo = QComboBox()
        self.geometry_combo.addItems(["Point", "MultiPoint", "Polyline", "Polygon", "MultiLineString", "MultiPolygon"])
        inherited_crs = _feature_dataset_crs(gdb_path, feature_dataset)
        self.crs_picker = _CRSPicker(self, inherited_crs or _parent_project_crs(parent))
        self.fd_edit = QLineEdit(feature_dataset or "")
        self.fd_edit.setReadOnly(bool(feature_dataset))
        if feature_dataset and inherited_crs is not None:
            self.crs_picker.button.setEnabled(False)
            self.crs_picker.button.setToolTip(
                f"CRS is inherited from Feature Dataset '{feature_dataset}' and cannot be overridden."
            )
        self.z_check = QCheckBox("Enable Z")
        self.m_check = QCheckBox("Enable M")
        dim_box = QWidget()
        dim_lay = QHBoxLayout(dim_box)
        dim_lay.setContentsMargins(0, 0, 0, 0)
        dim_lay.addWidget(self.z_check)
        dim_lay.addWidget(self.m_check)
        dim_lay.addStretch(1)

        form.addRow("Name:", self.name_edit)
        form.addRow("Alias:", self.alias_edit)
        form.addRow("Location:", QLabel(
            os.path.basename(gdb_path) + (f"\\{feature_dataset}" if feature_dataset else " (root)")
        ))
        form.addRow("Geometry:", self.geometry_combo)
        form.addRow("Coordinate system:", self.crs_picker)
        if feature_dataset and inherited_crs is not None:
            inherited_note = QLabel(f"Inherited from Feature Dataset: {feature_dataset}")
            inherited_note.setProperty("secondary", True)
            form.addRow("", inherited_note)
        form.addRow("Feature Dataset:", self.fd_edit)
        form.addRow("Dimensions:", dim_box)
        root.addLayout(form)

        root.addWidget(QLabel("Fields"))
        self.fields = QTableWidget(0, 7)
        self.fields.setHorizontalHeaderLabels(["Name", "Type", "Length", "Precision", "Nullable", "Default", "Domain"])
        self.fields.horizontalHeader().setStretchLastSection(True)
        compact_view(self.fields)
        configure_header(self.fields.horizontalHeader(), stretch_column=0)
        root.addWidget(self.fields, 1)

        field_buttons = QHBoxLayout()
        add_btn = QPushButton("Add Field")
        del_btn = QPushButton("Remove Field")
        add_btn.clicked.connect(self._add_field)
        del_btn.clicked.connect(self._remove_field)
        field_buttons.addWidget(add_btn)
        field_buttons.addWidget(del_btn)
        field_buttons.addStretch(1)
        root.addLayout(field_buttons)

        self.add_to_map = QCheckBox("Add newly created feature class to map")
        self.add_to_map.setChecked(True)
        root.addWidget(self.add_to_map)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._create)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)


    def _add_field(self, name="", field_type="String", width=""):
        r = self.fields.rowCount()
        self.fields.insertRow(r)
        defaults = [name, field_type, width, "0", "Yes", "", ""]
        for c, value in enumerate(defaults):
            self.fields.setItem(r, c, QTableWidgetItem(str(value)))

    def _remove_field(self):
        rows = sorted({x.row() for x in self.fields.selectedIndexes()}, reverse=True)
        for r in rows:
            self.fields.removeRow(r)

    def _collect_fields(self):
        out = []
        for r in range(self.fields.rowCount()):
            def text(c):
                item = self.fields.item(r, c)
                return item.text().strip() if item else ""
            name = text(0)
            if not name:
                continue
            try:
                width = int(text(2) or 0)
            except Exception:
                width = 0
            try:
                precision = int(text(3) or 0)
            except Exception:
                precision = 0
            nullable = text(4).lower() not in {"no", "false", "0", "n"}
            default = text(5) or None
            out.append(FieldSpec(
                name=name,
                type=text(1) or "String",
                width=width,
                precision=precision,
                nullable=nullable,
                default=default,
                domain=text(6),
            ))
        return out

    def _create(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "New Feature Class", "Enter a feature class name.")
            return
        dataset_name = self.fd_edit.text().strip()
        if dataset_name:
            # A Feature Dataset owns the coordinate system. Never trust a project
            # default or an independently selected CRS for a child feature class.
            selected_crs = _feature_dataset_crs(self.gdb_path, dataset_name)
            if selected_crs is None:
                QMessageBox.warning(
                    self, "New Feature Class",
                    f"Naksha could not resolve the coordinate system of Feature Dataset '{dataset_name}'. "
                    "The feature class was NOT created because child classes must inherit the dataset CRS.",
                )
                return
        else:
            selected_crs = self.crs_picker.user_input()
        self.result = create_feature_class(
            self.gdb_path,
            name,
            geometry=self.geometry_combo.currentText(),
            crs=selected_crs,
            feature_dataset=dataset_name or None,
            alias=self.alias_edit.text().strip() or None,
            has_z=self.z_check.isChecked(),
            has_m=self.m_check.isChecked(),
            fields=self._collect_fields(),
        )
        if _message_result(self, "New Feature Class", self.result):
            dataset = self.fd_edit.text().strip()
            notify_created(
                self.result, kind="layer", container=self.gdb_path,
                parent_dataset=dataset,
            )
            if self.add_to_map.isChecked():
                owner = self.parent()
                for _ in range(5):
                    if owner is None or hasattr(owner, "gis_layers"):
                        break
                    owner = owner.parent() if hasattr(owner, "parent") else None
                if owner is not None:
                    try:
                        from .reader import import_gdb_layer
                        import_gdb_layer(owner, self.gdb_path, name)
                        from gui.gis.gis_layers import show_gis_layers_panel
                        show_gis_layers_panel(owner)
                    except Exception as exc:
                        QMessageBox.warning(
                            self, "Add Feature Class to Map",
                            f"The feature class was created, but could not be added to the map.\n\n{exc}",
                        )
            self.accept()


def create_feature_class_dialog(parent, gdb_path: str, feature_dataset: str | None = None):
    dlg = FeatureClassDialog(parent, gdb_path, feature_dataset)
    dlg.exec()
    return dlg.result


class TableDialog(QDialog):
    def __init__(self, parent, gdb_path: str):
        super().__init__(parent)
        self.gdb_path = gdb_path
        self.result = None
        self.setWindowTitle("New Table")
        self.resize(620, 390)
        _theme(self)
        root = QVBoxLayout(self)
        compact_layout(root)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.alias_edit = QLineEdit()
        form.addRow("Name:", self.name_edit)
        form.addRow("Alias:", self.alias_edit)
        root.addLayout(form)
        self.fields = QTableWidget(0, 5)
        self.fields.setHorizontalHeaderLabels(["Name", "Type", "Length", "Nullable", "Domain"])
        self.fields.horizontalHeader().setStretchLastSection(True)
        compact_view(self.fields)
        configure_header(self.fields.horizontalHeader(), stretch_column=0)
        root.addWidget(self.fields, 1)
        row = QHBoxLayout()
        add = QPushButton("Add Field")
        remove = QPushButton("Remove Field")
        add.clicked.connect(self._add)
        remove.clicked.connect(self._remove)
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch(1)
        root.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._create)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._add("Name", "String", "100")

    def _add(self, name="", field_type="String", width=""):
        r = self.fields.rowCount(); self.fields.insertRow(r)
        for c, value in enumerate([name, field_type, width, "Yes", ""]):
            self.fields.setItem(r, c, QTableWidgetItem(str(value)))

    def _remove(self):
        for r in sorted({x.row() for x in self.fields.selectedIndexes()}, reverse=True):
            self.fields.removeRow(r)

    def _create(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "New Table", "Enter a table name.")
            return
        specs = []
        for r in range(self.fields.rowCount()):
            vals = []
            for c in range(self.fields.columnCount()):
                it = self.fields.item(r, c); vals.append(it.text().strip() if it else "")
            if not vals[0]:
                continue
            try: width = int(vals[2] or 0)
            except Exception: width = 0
            specs.append(FieldSpec(name=vals[0], type=vals[1] or "String", width=width,
                                   nullable=vals[3].lower() not in {"no","false","0"}, domain=vals[4]))
        self.result = create_table(self.gdb_path, name, alias=self.alias_edit.text().strip() or None, fields=specs)
        if _message_result(self, "New Table", self.result):
            notify_created(self.result, kind="table", container=self.gdb_path)
            self.accept()


def create_table_dialog(parent, gdb_path: str):
    dlg = TableDialog(parent, gdb_path)
    dlg.exec()
    return dlg.result
