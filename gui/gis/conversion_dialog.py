"""Batch GIS conversion dialog backed by ``conversion_engine``."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox, QProgressDialog,
    QPushButton, QVBoxLayout, QAbstractItemView,
)

from .conversion_engine import SourceLayer, convert_vector_sources
from .catalog_events import catalog_events
from .gis_style import apply_gis_dialog_style, compact_layout, compact_view


_VECTOR_FILTER = (
    "Vector GIS (*.shp *.geojson *.json *.gpkg *.gdb *.kml *.kmz *.gpx *.gml *.fgb *.sqlite *.db *.csv);;"
    "Shapefile (*.shp);;GeoJSON (*.geojson *.json);;GeoPackage (*.gpkg);;"
    "KML/KMZ (*.kml *.kmz);;All files (*)"
)


class _ConversionWorker(QThread):
    progress = Signal(int, int, str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, specs, target, options, parent=None):
        super().__init__(parent)
        self.specs = list(specs)
        self.target = str(target)
        self.options = dict(options)
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            report = convert_vector_sources(
                self.specs,
                self.target,
                progress=lambda done, total, label: self.progress.emit(int(done), int(total), str(label or "")),
                cancel_check=lambda: self._cancelled,
                **self.options,
            )
            self.completed.emit(report)
        except Exception as exc:
            self.failed.emit(str(exc))


class BatchConversionDialog(QDialog):
    def __init__(self, parent=None, *, sources=None, target_path=None, feature_dataset=None):
        super().__init__(parent)
        self.report = None
        self._worker = None
        self._progress = None
        qt_app = QApplication.instance()
        if qt_app is not None:
            qt_app.aboutToQuit.connect(self._shutdown_worker)
        self.setWindowTitle("Import / Convert GIS Data")
        self.resize(760, 500)
        apply_gis_dialog_style(self)

        root = QVBoxLayout(self)
        compact_layout(root)
        root.addWidget(QLabel("Source datasets / layers"))
        self.sources = QListWidget()
        compact_view(self.sources)
        root.addWidget(self.sources, 1)
        row = QHBoxLayout()
        add = QPushButton("Add Files")
        add_dataset = QPushButton("Add Dataset")
        remove = QPushButton("Remove")
        clear = QPushButton("Clear")
        add.clicked.connect(self._add_files)
        add_dataset.clicked.connect(self._add_dataset)
        remove.clicked.connect(self._remove)
        clear.clicked.connect(self.sources.clear)
        row.addWidget(add); row.addWidget(add_dataset); row.addWidget(remove); row.addWidget(clear); row.addStretch(1)
        root.addLayout(row)

        form = QFormLayout()
        self.target_edit = QLineEdit(target_path or "")
        browse = QPushButton("Browse...")
        target_row = QHBoxLayout(); target_row.addWidget(self.target_edit, 1); target_row.addWidget(browse)
        browse.clicked.connect(self._browse_target)
        form.addRow("Target GDB/GPKG:", target_row)

        self.fd_edit = QLineEdit(feature_dataset or "")
        self.fd_edit.setPlaceholderText("Optional - FileGDB only, e.g. UTILITIES")
        form.addRow("Feature Dataset:", self.fd_edit)
        self._target_crs = None
        self.crs_edit = QLineEdit()
        self.crs_edit.setReadOnly(True)
        self.crs_edit.setPlaceholderText("Keep each source CRS unless a target CRS is selected")
        crs_pick = QPushButton("Select CRS...")
        crs_clear = QPushButton("Clear")
        crs_pick.clicked.connect(self._select_target_crs)
        crs_clear.clicked.connect(self._clear_target_crs)
        crs_row = QHBoxLayout(); crs_row.addWidget(self.crs_edit, 1); crs_row.addWidget(crs_pick); crs_row.addWidget(crs_clear)
        form.addRow("Reproject to:", crs_row)

        self.overwrite_layers = QCheckBox("Replace target layer when the same name exists")
        self.shape_fields = QCheckBox("Create Shape_Length / Shape_Area for FileGDB")
        self.shape_fields.setChecked(True)
        form.addRow("Options:", self.overwrite_layers)
        form.addRow("", self.shape_fields)
        root.addLayout(form)

        note = QLabel("Native geometry, fields, Z/M values and CRS metadata are preserved unless reprojection is selected.")
        note.setWordWrap(True)
        note.setProperty("secondary", True)
        root.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.button_box = buttons
        ok = buttons.button(QDialogButtonBox.Ok)
        if ok: ok.setText("Convert")
        buttons.accepted.connect(self._run)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        for src in sources or []:
            if isinstance(src, SourceLayer):
                label = src.path + (f"  [{src.layer_name}]" if src.layer_name else "")
                self.sources.addItem(label); self.sources.item(self.sources.count()-1).setData(Qt.UserRole, src)
            else:
                self._append_source(str(src))

    def _append_source(self, path, layer_name=None):
        spec = SourceLayer(path=str(path), layer_name=layer_name)
        label = str(path) + (f"  [{layer_name}]" if layer_name else "  [all public layers]")
        self.sources.addItem(label)
        self.sources.item(self.sources.count()-1).setData(Qt.UserRole, spec)

    def _select_dataset_layers(self, path):
        """Return None for cancel, [] for none, or selected public layer names."""
        try:
            from gui.gis.native_vector import list_public_layers
            descriptors = list_public_layers(path)
        except Exception as exc:
            QMessageBox.warning(self, "Select Dataset Layers", str(exc))
            return None
        if not descriptors:
            return []
        if len(descriptors) == 1:
            return [descriptors[0]["name"]]

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Select Layers - {Path(path).name}")
        dlg.resize(520, 430)
        apply_gis_dialog_style(dlg)
        lay = QVBoxLayout(dlg); compact_layout(lay)
        lay.addWidget(QLabel("Select layers/tables to convert. All are selected by default."))
        lw = QListWidget(); compact_view(lw)
        lw.setSelectionMode(QAbstractItemView.MultiSelection)
        for d in descriptors:
            count = d.get("feature_count", -1)
            suffix = f" ({count:,})" if isinstance(count, int) and count >= 0 else ""
            item = lw.addItem(f"{d['name']}  -  {d.get('geometry_type') or d.get('kind')}{suffix}")
            qitem = lw.item(lw.count()-1)
            qitem.setData(Qt.UserRole, d["name"])
            qitem.setSelected(True)
        lay.addWidget(lw, 1)
        actions = QHBoxLayout()
        all_btn = QPushButton("Select All"); none_btn = QPushButton("Select None")
        all_btn.clicked.connect(lambda: [lw.item(i).setSelected(True) for i in range(lw.count())])
        none_btn.clicked.connect(lambda: lw.clearSelection())
        actions.addWidget(all_btn); actions.addWidget(none_btn); actions.addStretch(1)
        lay.addLayout(actions)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept); buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)
        if dlg.exec() != QDialog.Accepted:
            return None
        return [str(x.data(Qt.UserRole)) for x in lw.selectedItems() if x.data(Qt.UserRole)]

    def _append_dataset(self, path):
        selected = self._select_dataset_layers(path)
        if selected is None:
            return
        if not selected:
            QMessageBox.information(self, "Select Dataset Layers", "No layers were selected.")
            return
        try:
            from gui.gis.native_vector import list_public_layers
            all_names = [d["name"] for d in list_public_layers(path)]
        except Exception:
            all_names = []
        # Keep an all-layers SourceLayer when every public layer is selected. This
        # preserves one datasource handle and gives relationship copy the complete
        # source layer-name map.
        if all_names and set(selected) == set(all_names):
            self._append_source(path, None)
        else:
            for name in selected:
                self._append_source(path, name)

    def _add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Select GIS Vector Sources", str(Path.home()), _VECTOR_FILTER)
        for p in paths:
            if str(p).lower().endswith((".gpkg", ".sqlite", ".db")):
                self._append_dataset(p)
            else:
                self._append_source(p, None)

    def _add_dataset(self):
        path = QFileDialog.getExistingDirectory(
            self, "Select File Geodatabase", str(Path.home())
        )
        if path and path.lower().endswith(".gdb"):
            self._append_dataset(path)
        elif path:
            QMessageBox.information(
                self, "Add Dataset",
                "Select a .gdb folder. GeoPackage/SQLite files can be selected with Add Files.",
            )

    def _remove(self):
        for item in list(self.sources.selectedItems()):
            self.sources.takeItem(self.sources.row(item))

    def _browse_target(self):
        path, selected = QFileDialog.getSaveFileName(
            self, "Target GIS Container", self.target_edit.text() or str(Path.home() / "output.gdb"),
            "File Geodatabase (*.gdb);;GeoPackage (*.gpkg)"
        )
        if path:
            if "GeoPackage" in selected and not path.lower().endswith(".gpkg"):
                path += ".gpkg"
            elif "Geodatabase" in selected and not path.lower().endswith(".gdb"):
                path += ".gdb"
            self.target_edit.setText(path)

    def _select_target_crs(self):
        try:
            from gui.crs_selector_dialog import choose_crs
            initial = self._target_crs
            if initial is None:
                owner = getattr(self.parent(), "app", None) or self.parent()
                initial = getattr(owner, "canvas_crs", None)
            crs = choose_crs(self, initial_crs=initial, title="Target Coordinate Reference System")
            if crs is None:
                return
            self._target_crs = crs
            try:
                from gui.projection_engine import crs_identifier
                self.crs_edit.setText(f"{crs_identifier(crs)} - {crs.name}")
            except Exception:
                self.crs_edit.setText(crs.name or "Selected CRS")
        except Exception as exc:
            QMessageBox.warning(self, "Target CRS", str(exc))

    def _clear_target_crs(self):
        self._target_crs = None
        self.crs_edit.clear()

    def _run(self):
        specs = []
        for i in range(self.sources.count()):
            item = self.sources.item(i)
            spec = item.data(Qt.UserRole)
            if not isinstance(spec, SourceLayer):
                spec = SourceLayer(path=item.text())
            specs.append(spec)
        target = self.target_edit.text().strip()
        if not specs:
            QMessageBox.warning(self, "Convert GIS Data", "Add at least one source dataset.")
            return
        if not target:
            QMessageBox.warning(self, "Convert GIS Data", "Choose a target .gdb or .gpkg.")
            return
        if not target.lower().endswith((".gdb", ".gpkg")):
            QMessageBox.warning(self, "Convert GIS Data", "Target must currently be a .gdb or .gpkg container.")
            return
        if getattr(self, "_worker", None) is not None and self._worker.isRunning():
            return

        feature_dataset = self.fd_edit.text().strip() or None
        effective_target_crs = self._target_crs

        # A FileGDB Feature Dataset owns one coordinate system.  Existing (or
        # session-pending) datasets therefore override any independent target
        # CRS choice.  A brand-new Feature Dataset requires an explicit target
        # CRS so sources with different CRSs cannot accidentally create an
        # inconsistent container.
        if target.lower().endswith(".gdb") and feature_dataset:
            inherited = None
            try:
                from gui.gis.gdb.schema_dialogs import _feature_dataset_crs
                inherited = _feature_dataset_crs(target, feature_dataset)
            except Exception:
                inherited = None

            if inherited is not None:
                if effective_target_crs is not None:
                    try:
                        differs = not effective_target_crs.equals(inherited)
                    except Exception:
                        differs = str(effective_target_crs) != str(inherited)
                    if differs:
                        QMessageBox.information(
                            self,
                            "Feature Dataset CRS",
                            f"'{feature_dataset}' already owns its coordinate system. "
                            "The conversion will use the Feature Dataset CRS and ignore "
                            "the independently selected target CRS.",
                        )
                effective_target_crs = inherited
            else:
                existing_fd = False
                try:
                    from gui.gis.catalog_inspector import inspect_source
                    if os.path.exists(target):
                        meta = inspect_source(target, include_fields=False)
                        existing_fd = feature_dataset.casefold() in {
                            str(x).casefold() for x in (meta.get("feature_datasets") or [])
                        }
                except Exception:
                    pass
                try:
                    from gui.gis.gdb.pending import get_pending
                    existing_fd = existing_fd or get_pending(target, feature_dataset) is not None
                except Exception:
                    pass

                if existing_fd:
                    QMessageBox.warning(
                        self,
                        "Feature Dataset CRS",
                        f"Naksha could not resolve the coordinate system of Feature Dataset "
                        f"'{feature_dataset}'. Conversion was NOT started because child "
                        "feature classes must inherit the dataset CRS.",
                    )
                    return
                if effective_target_crs is None:
                    QMessageBox.warning(
                        self,
                        "Feature Dataset CRS Required",
                        f"'{feature_dataset}' does not yet exist. Select one target CRS first. "
                        "Naksha will reproject every imported feature class to that CRS when "
                        "materializing the new Feature Dataset.",
                    )
                    return

        self._progress = QProgressDialog("Converting GIS data...", "Cancel", 0, 100, self)
        self._progress.setAttribute(Qt.WA_DeleteOnClose, True)
        self._progress.setWindowModality(Qt.WindowModal)
        self._progress.setMinimumDuration(0)
        self._progress.setValue(0)

        options = dict(
            overwrite_dataset=False,
            overwrite_layers=self.overwrite_layers.isChecked(),
            feature_dataset=feature_dataset,
            target_crs=effective_target_crs.to_wkt() if effective_target_crs is not None else None,
            create_shape_fields=self.shape_fields.isChecked(),
        )
        self._worker = _ConversionWorker(specs, target, options, self)
        self._worker.progress.connect(self._on_worker_progress)
        self._worker.completed.connect(self._on_worker_completed)
        self._worker.failed.connect(self._on_worker_failed)
        self._worker.finished.connect(self._on_worker_finished)
        self._progress.canceled.connect(self._worker.cancel)
        self.button_box.setEnabled(False)
        self._worker.start()

    def _on_worker_progress(self, done, total, label):
        progress = getattr(self, "_progress", None)
        if progress is None:
            return
        try:
            from shiboken6 import isValid
            if not isValid(progress):
                return
        except Exception:
            pass
        if total and total > 0:
            if progress.maximum() == 0:
                progress.setRange(0, 100)
            progress.setValue(min(99, int(done * 100 / max(1, total))))
        else:
            # Unknown feature count: keep the dialog active without inventing a percentage.
            progress.setRange(0, 0)
        progress.setLabelText(label or "Converting GIS data...")

    def _finish_worker_ui(self):
        progress = getattr(self, "_progress", None)
        if progress is not None:
            try:
                if progress.maximum() != 0:
                    progress.setValue(100)
                progress.close()
            except Exception:
                pass
        self._progress = None
        try:
            self.button_box.setEnabled(True)
        except Exception:
            pass

    def _on_worker_failed(self, message):
        self._finish_worker_ui()
        QMessageBox.critical(self, "Convert GIS Data", str(message))

    def _on_worker_completed(self, report):
        self._finish_worker_ui()
        self.report = report

        if self.report.error and "canceled" in str(self.report.error).lower():
            QMessageBox.information(
                self, "GIS Conversion Canceled",
                "Conversion was canceled. Any active datasource transaction was rolled back where the driver supports it."
            )
            return

        succeeded = [x for x in self.report.layers if x.success]
        failed = [x for x in self.report.layers if not x.success]
        lines = [
            f"Target: {self.report.output_path}",
            f"Successful layers: {len(succeeded)}",
            f"Failed layers: {len(failed)}",
        ]
        if self.report.warnings:
            lines += ["", "Warnings:"] + [f"- {x}" for x in self.report.warnings]
        if failed:
            lines += ["", "Failures:"] + [f"- {x.source_layer}: {x.error or 'failed'}" for x in failed[:20]]
        if self.report.error:
            lines += ["", f"Error: {self.report.error}"]
        if succeeded:
            catalog_events().schemaChanged.emit(os.path.abspath(self.report.output_path))
        if self.report.success and not failed:
            QMessageBox.information(self, "GIS Conversion Complete", "\n".join(lines))
            self.accept()
        elif succeeded:
            QMessageBox.warning(self, "GIS Conversion Partially Complete", "\n".join(lines))
            self.accept()
        else:
            QMessageBox.critical(self, "GIS Conversion Failed", "\n".join(lines))


    def _on_worker_finished(self):
        worker = self.sender()
        if worker is self._worker:
            self._worker = None
        if worker is not None:
            worker.deleteLater()

    def _shutdown_worker(self):
        worker = getattr(self, "_worker", None)
        if worker is None or not worker.isRunning():
            return
        worker.cancel()
        worker.requestInterruption()
        # A running QThread must never be destroyed with its parent dialog.
        # The converter checks cancellation between feature operations.
        worker.wait()

    def reject(self):
        worker = getattr(self, "_worker", None)
        if worker is not None and worker.isRunning():
            worker.cancel()
            QMessageBox.information(
                self, "GIS Conversion",
                "Cancellation was requested. Wait for the current GDAL feature operation to stop safely."
            )
            return
        super().reject()


def show_batch_conversion_dialog(parent=None, **kwargs):
    dlg = BatchConversionDialog(parent, **kwargs)
    dlg.exec()
    return dlg.report
