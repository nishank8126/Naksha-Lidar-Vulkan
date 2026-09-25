import json
import os
import re
from pathlib import Path
import numpy as np
import vtk
from PySide6.QtCore import QEvent, QPoint, Qt, Signal, QTimer
from PySide6.QtGui import QValidator, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QComboBox,
    QApplication,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


_TITLE_BAR_HEIGHT = 36
_CORNER_RADIUS = 20


class _BlockTitleBar(QWidget):
    """Drag-capable custom title bar for the frameless BlockCreationSettingsDialog."""

    def __init__(self, parent: QDialog):
        super().__init__(parent)
        self.setFixedHeight(_TITLE_BAR_HEIGHT)
        self.setCursor(Qt.ArrowCursor)
        self._drag_pos: QPoint | None = None

        from gui.theme_manager import ThemeColors as _TC
        text = _TC.get("text_primary")
        hover = _TC.get("bg_button_hover")

        self.setStyleSheet(f"""
            _BlockTitleBar {{ background: transparent; }}
            QPushButton#titleBarBtn {{
                background: transparent;
                border: none;
                color: {text};
                font-size: 13px;
                min-width: 32px;
                max-width: 32px;
                min-height: {_TITLE_BAR_HEIGHT}px;
                max-height: {_TITLE_BAR_HEIGHT}px;
                border-radius: 0px;
                padding: 0px;
            }}
            QPushButton#titleBarBtn:hover {{ background-color: {hover}; }}
            QPushButton#titleBarCloseBtn {{
                background: transparent;
                border: none;
                color: {text};
                font-size: 13px;
                min-width: 32px;
                max-width: 32px;
                min-height: {_TITLE_BAR_HEIGHT}px;
                max-height: {_TITLE_BAR_HEIGHT}px;
                border-top-right-radius: {_CORNER_RADIUS}px;
                padding: 0px;
            }}
            QPushButton#titleBarCloseBtn:hover {{ background-color: #c42b1c; color: white; }}
        """)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 0, 0)
        lay.setSpacing(0)

        self._title_lbl = QLabel("Block Creation Settings")
        self._title_lbl.setStyleSheet(
            f"color: {text}; font-size: 13px; font-weight: bold; background: transparent;"
        )
        lay.addWidget(self._title_lbl)
        lay.addStretch()

        min_btn = QPushButton("−")
        min_btn.setObjectName("titleBarBtn")
        min_btn.setToolTip("Minimize")
        min_btn.clicked.connect(parent.showMinimized)
        lay.addWidget(min_btn)

        close_btn = QPushButton("✕")
        close_btn.setObjectName("titleBarCloseBtn")
        close_btn.setToolTip("Close")
        close_btn.clicked.connect(parent.close)
        lay.addWidget(close_btn)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() == Qt.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)


def _get_block_dialog_style():
    from gui.theme_manager import ThemeColors as _TC

    return f"""
QDialog {{
    background-color: {_TC.get('bg_secondary')};
}}
QWidget#dialogContent {{
    background-color: {_TC.get('bg_secondary')};
    color: {_TC.get('text_primary')};
}}
QGroupBox {{
    background-color: {_TC.get('bg_primary')};
    border: 1px solid {_TC.get('border_light')};
    border-radius: 6px;
    margin-top: 12px;
    padding-top: 12px;
    padding-bottom: 6px;
    font-weight: bold;
    color: {_TC.get('text_primary')};
    font-size: 9.5pt;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
    font-size: 9.5pt;
    letter-spacing: 0.3px;
    color: {_TC.get('accent')};
}}
QLabel {{
    color: {_TC.get('text_primary')};
    font-size: 9pt;
    font-weight: 500;
}}
QComboBox, QDoubleSpinBox, QLineEdit, QListWidget {{
    background-color: {_TC.get('bg_input')};
    color: {_TC.get('text_primary')};
    border: 1px solid {_TC.get('border_light')};
    border-radius: 4px;
    padding: 2px 8px;
    min-height: 22px;
    max-height: 22px;
    font-size: 9pt;
}}
QListWidget {{
    max-height: none;
}}
QComboBox:hover, QDoubleSpinBox:hover, QLineEdit:hover, QListWidget:hover {{
    border: 1px solid {_TC.get('accent')};
    background-color: {_TC.get('bg_button_hover')};
}}
QLineEdit:read-only {{
    color: {_TC.get('text_secondary')};
}}
QListWidget::item {{
    padding: 3px 4px;
}}
QComboBox::drop-down {{
    border: none;
    width: 18px;
}}
QPushButton {{
    background-color: {_TC.get('bg_button')};
    color: {_TC.get('text_primary')};
    border: 1px solid {_TC.get('border_light')};
    border-radius: 4px;
    padding: 2px 12px;
    font-weight: bold;
    font-size: 9pt;
    min-height: 22px;
    max-height: 22px;
}}
QPushButton:hover {{
    background-color: {_TC.get('bg_button_hover')};
    border-color: {_TC.get('accent')};
}}
QPushButton:pressed {{
    background-color: {_TC.get('accent')};
    color: {_TC.get('text_on_active')};
}}
QPushButton:checked {{
    background-color: {_TC.get('accent')};
    border-color: {_TC.get('accent_hover')};
    color: {_TC.get('text_on_active')};
}}
QPushButton#okButton {{
    background-color: {_TC.get('accent')};
    border-color: {_TC.get('accent')};
    color: {_TC.get('text_on_active')};
}}
QPushButton#okButton:hover {{
    background-color: {_TC.get('accent_hover')};
}}
QPushButton#splitButton {{
    background-color: #1a1a00;
    border: 1px solid #ccff00;
    color: #ccff00;
    font-weight: bold;
}}
QPushButton#splitButton:hover {{
    background-color: #2e2e00;
    border-color: #eeff44;
    color: #eeff44;
}}
QPushButton#splitButton:pressed {{
    background-color: #ccff00;
    color: #000000;
}}
"""


class CLSelectionPopup(QDialog):
    selection_confirmed = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setModal(False)
        self.selected_lines = []
        self._build_ui()

    def _build_ui(self):
        from gui.theme_manager import ThemeColors as _TC

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        self.setStyleSheet(f"""
QDialog {{
    background-color: {_TC.get('bg_secondary')};
    border: 1px solid {_TC.get('border_light')};
    border-radius: 6px;
}}
QListWidget {{
    background-color: {_TC.get('bg_input')};
    color: {_TC.get('text_primary')};
    border: 1px solid {_TC.get('border_light')};
    border-radius: 4px;
    padding: 2px;
    font-size: 9pt;
}}
QListWidget::item {{
    padding: 3px 6px;
}}
QPushButton {{
    background-color: {_TC.get('bg_button')};
    color: {_TC.get('text_primary')};
    border: 1px solid {_TC.get('border_light')};
    border-radius: 4px;
    padding: 3px 12px;
    font-weight: bold;
    font-size: 9pt;
    min-height: 20px;
    max-height: 20px;
}}
QPushButton:hover {{
    background-color: {_TC.get('bg_button_hover')};
    border-color: {_TC.get('accent')};
}}
QPushButton#okButton {{
    background-color: {_TC.get('accent')};
    color: {_TC.get('text_on_active')};
}}
QPushButton#okButton:hover {{
    background-color: {_TC.get('accent_hover')};
}}
""")

        small_font = QFont(self.font().family(), 8)

        self.list_widget = QListWidget()
        self.list_widget.setFixedWidth(150)
        self.list_widget.setFixedHeight(160)
        self.list_widget.setFont(small_font)
        layout.addWidget(self.list_widget)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.done_btn = QPushButton("Done")
        self.done_btn.setObjectName("okButton")
        self.done_btn.setFont(small_font)
        self.done_btn.setFixedSize(56, 22)
        self.done_btn.clicked.connect(self._on_done)
        btn_layout.addWidget(self.done_btn)

        layout.addLayout(btn_layout)

    def update_selection(self, lines):
        self.selected_lines = lines
        self.list_widget.clear()
        for idx, line in enumerate(lines, 1):
            item = QListWidgetItem(f"Line {idx}")
            self.list_widget.addItem(item)

    def _on_done(self):
        self.selection_confirmed.emit(self.selected_lines)
        self.hide()


class DistanceSpinBox(QDoubleSpinBox):
    """Accepts 'km' suffix and auto-converts to meters."""

    def textFromValue(self, value):
        return f"{self.locale().toString(value, 'f', self.decimals())} m"

    def valueFromText(self, text):
        t = text.strip().lower()
        if t.endswith('km'):
            try:
                return float(t[:-2].strip()) * 1000.0
            except ValueError:
                pass
        elif t.endswith('m'):
            try:
                return float(t[:-1].strip())
            except ValueError:
                pass
        try:
            return float(t)
        except ValueError:
            return self.value()

    def validate(self, text, pos):
        t = text.strip().lower()
        if t in ('', '-', '.', '-.'):
            return QValidator.Intermediate, text, pos
        pat = r'^-?(\d+\.?\d*|\.\d+)\s*(km|m)?$'
        if re.match(pat, t):
            return QValidator.Acceptable, text, pos
        pat2 = r'^-?\d*\.?\d*\s*(k|m|km)?$'
        if re.match(pat2, t):
            return QValidator.Intermediate, text, pos
        return QValidator.Invalid, text, pos


class BlockSplitSaveDialog(QDialog):
    def __init__(self, app, blocks, parent=None):
        super().__init__(parent or app)
        self.app = app
        self.blocks = sorted(blocks or [], key=lambda item: item.get("priority", 0))
        self.source_path = self._get_source_path()
        self.source_stem = self.source_path.stem if self.source_path else "pointcloud"
        self.output_ext = self._get_output_ext()
        self.parent_dir = self._default_output_parent()
        self.setWindowTitle("Save Split Blocks As")
        self.setModal(True)
        self.resize(520, 430)
        self.setStyleSheet(_get_block_dialog_style())
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 5, 18, 10)
        layout.setSpacing(12)

        blocks_group = QGroupBox("Blocks")
        blocks_layout = QVBoxLayout(blocks_group)
        blocks_layout.setContentsMargins(15, 10, 15, 10)
        blocks_layout.setSpacing(8)
        self.block_list = QListWidget()
        for block in self.blocks:
            self.block_list.addItem(self._block_file_stem(block) + self.output_ext)
        blocks_layout.addWidget(self.block_list, 1)
        layout.addWidget(blocks_group, 1)

        location_group = QGroupBox("Save Location")
        location_form = QFormLayout(location_group)
        location_form.setLabelAlignment(Qt.AlignRight)
        location_form.setHorizontalSpacing(12)
        location_form.setVerticalSpacing(8)
        location_form.setContentsMargins(15, 10, 15, 10)

        self.folder_name_edit = QLineEdit(f"{self.source_stem}_Split_Files")
        # Runtime shortcuts remain available everywhere else in this dialog.
        # Only literal filename entry owns alphabet keys while it has focus.
        self.folder_name_edit.setProperty("nakshaStrictTextInput", True)
        self.folder_name_edit.textChanged.connect(self._refresh_destination_path)
        location_form.addRow("Folder name:", self.folder_name_edit)

        location_row = QWidget()
        location_row_layout = QHBoxLayout(location_row)
        location_row_layout.setContentsMargins(0, 0, 0, 0)
        location_row_layout.setSpacing(8)
        self.parent_dir_edit = QLineEdit(str(self.parent_dir))
        self.parent_dir_edit.setReadOnly(True)
        location_row_layout.addWidget(self.parent_dir_edit, 1)
        self.browse_btn = QPushButton("Browse...")
        self.browse_btn.clicked.connect(self._browse_parent_dir)
        location_row_layout.addWidget(self.browse_btn)
        location_form.addRow("Save in:", location_row)

        self.destination_label = QLabel("")
        self.destination_label.setWordWrap(True)
        location_form.addRow("Destination:", self.destination_label)
        layout.addWidget(location_group)
        self._refresh_destination_path()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.save_btn = QPushButton("Save")
        self.save_btn.setObjectName("okButton")
        self.save_btn.setDefault(True)
        self.cancel_btn = QPushButton("Cancel")
        self.save_btn.clicked.connect(self._save_split_blocks)
        self.folder_name_edit.returnPressed.connect(self._save_split_blocks)
        self.cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.save_btn)
        btn_row.addWidget(self.cancel_btn)
        layout.addLayout(btn_row)

    def _get_source_path(self):
        for attr in ("loaded_file", "current_file_path", "last_save_path"):
            value = getattr(self.app, attr, None)
            if value:
                try:
                    return Path(value)
                except Exception:
                    pass
        data = getattr(self.app, "data", None)
        if isinstance(data, dict):
            value = data.get("_source_file_path") or data.get("filename")
            if value:
                try:
                    return Path(value)
                except Exception:
                    pass
        return None

    def _get_output_ext(self):
        if self.source_path and self.source_path.suffix.lower() in (".las", ".laz"):
            return self.source_path.suffix.lower()
        return ".laz"

    def _default_output_parent(self):
        if self.source_path and self.source_path.parent:
            return self.source_path.parent
        return Path(os.getcwd())

    def _output_parent(self):
        return self.parent_dir

    def _output_dir(self):
        folder_name = self.folder_name_edit.text().strip()
        return self._output_parent() / folder_name if folder_name else self._output_parent()

    def _refresh_destination_path(self):
        if hasattr(self, "destination_label"):
            self.destination_label.setText(f"Destination: {self._output_dir()}")

    def _browse_parent_dir(self):
        selected = QFileDialog.getExistingDirectory(
            self,
            "Select Save Location",
            str(self.parent_dir),
        )
        if not selected:
            return
        self.parent_dir = Path(selected)
        self.parent_dir_edit.setText(str(self.parent_dir))
        self._refresh_destination_path()

    @staticmethod
    def _distance_token(distance):
        distance = float(distance or 0.0)
        if abs(distance - round(distance)) < 1e-6:
            return str(int(round(distance)))
        return f"{distance:.4f}".rstrip("0").rstrip(".")

    def _block_file_stem(self, block):
        serial = int(block.get("priority", 0) or 0)
        distance_token = self._distance_token(block.get("distance", 0.0))
        return f"{self.source_stem}-{distance_token}-{serial:02d}"

    def _save_split_blocks(self):
        folder_name = self.folder_name_edit.text().strip()
        if not folder_name:
            QMessageBox.warning(self, "Split Blocks", "Enter a folder name.")
            return

        output_dir = self._output_dir()
        progress = QProgressDialog("Saving split blocks...", None, 0, 0, self)
        progress.setWindowTitle("Split Blocks")
        progress.setWindowModality(Qt.ApplicationModal)
        progress.setCancelButton(None)
        progress.setMinimumDuration(0)
        progress.show()
        QApplication.processEvents()
        self.save_btn.setEnabled(False)

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            saved, skipped, saved_blocks = self._write_block_files(output_dir)
            if saved_blocks:
                progress.setLabelText("Creating SNT and PRJ files...")
                QApplication.processEvents()
                saved_blocks = self._prepare_manifest_blocks(saved_blocks)
                manifest_base = output_dir.name or self.source_stem
                self._write_prj_manifest(output_dir / f"{manifest_base}.prj", saved_blocks)
                self._write_snt_manifest(output_dir / f"{manifest_base}.snt", saved_blocks)
        except Exception as exc:
            progress.close()
            self.save_btn.setEnabled(True)
            QMessageBox.critical(self, "Split Blocks", f"Failed to save split files:\n{exc}")
            return
        finally:
            progress.close()

        if skipped:
            manifest_message = (
                f"Created {output_dir.name}.snt and {output_dir.name}.prj.\n\n"
                if saved_blocks else ""
            )
            QMessageBox.information(
                self,
                "Split Blocks",
                f"Saved {saved} block file(s) to:\n{output_dir}\n\n"
                f"{manifest_message}"
                f"{skipped} block(s) had no points and were skipped.",
            )
        else:
            manifest_message = (
                f"\n\nCreated {output_dir.name}.snt and {output_dir.name}.prj."
                if saved_blocks else ""
            )
            QMessageBox.information(
                self,
                "Split Blocks",
                f"Saved {saved} block file(s) to:\n{output_dir}{manifest_message}",
            )
        self.accept()

    def _write_block_files(self, output_dir):
        data = getattr(self.app, "data", None)
        if not isinstance(data, dict) or data.get("xyz") is None:
            raise RuntimeError("No point cloud data is loaded.")

        xyz = np.asarray(data["xyz"])
        if xyz.ndim != 2 or xyz.shape[1] < 3 or xyz.shape[0] == 0:
            raise RuntimeError("Loaded point cloud data is empty or invalid.")

        assigned = np.zeros(xyz.shape[0], dtype=bool)
        saved = 0
        skipped = 0
        saved_blocks = []

        for block in self.blocks:
            polygon = np.asarray(block.get("polygon"), dtype=np.float64)
            if polygon.ndim != 2 or polygon.shape[0] < 3:
                skipped += 1
                continue

            inside = self._points_in_polygon(xyz[:, 0], xyz[:, 1], polygon)
            mask = inside & ~assigned
            if not np.any(mask):
                skipped += 1
                continue

            output_path = output_dir / (self._block_file_stem(block) + self.output_ext)
            self._write_las_subset(output_path, data, mask, block)
            assigned[mask] = True
            saved += 1
            saved_blocks.append({
                "block": block,
                "file_name": output_path.name,
                "label": output_path.stem,
                "polygon": polygon,
                "point_count": int(np.count_nonzero(mask)),
            })

        return saved, skipped, saved_blocks

    def _write_prj_manifest(self, output_path, saved_blocks):
        distance = 0.0
        for item in saved_blocks:
            try:
                distance = max(distance, float(item["block"].get("distance", 0.0) or 0.0))
            except Exception:
                pass

        block_size = int(round(distance)) if distance > 0 else 0
        lines = [
            "[Nakshatech project]",
            "Scanner=AirborneLidar",
            f"Storage={self.output_ext.lstrip('.').upper()}1.2",
            "StoreTime=1",
            "StoreColor=0",
            "StoreEchoLen=0",
            "StoreParam=0",
            "StoreReflectance=0",
            "StoreDeviation=0",
            "StoreReliability=0",
            "StoreDistance=0",
            "StoreGroup=0",
            "StoreNormal=0",
            "StoreImage=0",
            "RequireLock=0",
            "Description=",
            "FirstPointId=1",
            "BlockRounded=1",
            f"BlockSize={block_size}",
            "BlockGroupCount=1000000",
            "BlockNaming=0",
            "BlockPrefix=pt",
            "",
        ]

        for index, item in enumerate(saved_blocks, start=1):
            lines.append(f"Block {item['file_name']}")
            lines.append(f"GroupFirst={index * 1000000}")
            lines.append("GroupCount=1000000")
            rings = item.get("export_rings") or [self._closed_polygon_xy(item["polygon"])]
            for x, y in rings[0]:
                lines.append(f" {x:.4f} {y:.4f}")
            lines.append("")

        output_path.write_text("\n".join(lines), encoding="utf-8")

    def _write_snt_manifest(self, output_path, saved_blocks):
        import shutil
        import tempfile

        try:
            import ezdxf
            import snt_core
        except Exception as exc:
            raise RuntimeError(f"SNT export backend is unavailable: {exc}") from exc

        temp_root = Path(tempfile.mkdtemp(prefix="naksha_split_snt_"))
        temp_dxf = temp_root / f"{output_path.stem}.dxf"
        temp_snt = temp_root / f"{output_path.stem}.snt"

        try:
            doc = ezdxf.new("R2010")
            for layer_name, color in (("BL", 4), ("CL", 1), ("FileNames", 2)):
                if not doc.layers.has_entry(layer_name):
                    layer = doc.layers.new(layer_name)
                else:
                    layer = doc.layers.get(layer_name)
                layer.color = color

            msp = doc.modelspace()
            for item in saved_blocks:
                cl_points = self._polyline_xyz(item["block"].get("cl_pts"))
                if len(cl_points) >= 2:
                    msp.add_polyline3d(
                        cl_points,
                        dxfattribs={"layer": "CL", "color": 1},
                    )

                bl_line_chains = self._block_bl_line_chains(item)
                rings = item.get("export_rings") or [self._closed_polygon_xy(item["polygon"])]
                rings = [ring for ring in rings if len(ring) >= 4]
                if not bl_line_chains and not rings:
                    continue
                if bl_line_chains:
                    for chain in bl_line_chains:
                        msp.add_polyline3d(
                            chain,
                            dxfattribs={"layer": "BL", "color": 4},
                        )
                # Always write the closed ring so the SNT block-click PIP test works.
                # bl_line_chains are open polylines (visual only); the closed lwpolyline
                # is what build_snt_block_polygons needs to form a hit-testable polygon.
                for polygon in rings:
                    msp.add_lwpolyline(
                        polygon[:-1],
                        format="xy",
                        close=True,
                        dxfattribs={"layer": "BL", "color": 4},
                    )

                label_pos = item.get("label_position") or self._block_label_position(item["block"], item["polygon"])
                label_polygon = rings[0] if rings else self._closed_polygon_xy(item["polygon"])
                label_height = self._block_label_height(label_polygon, item["label"])
                text = msp.add_text(
                    item["label"],
                    dxfattribs={
                        "layer": "FileNames",
                        "color": 2,
                        "height": label_height,
                    },
                )
                try:
                    from ezdxf.enums import TextEntityAlignment
                    text.set_placement(label_pos, align=TextEntityAlignment.MIDDLE_CENTER)
                except Exception:
                    text.dxf.insert = label_pos

            doc.saveas(temp_dxf)

            result = None
            try:
                result = snt_core.convert(
                    dxf_path=temp_dxf,
                    snt_path=temp_snt,
                    description=output_path.stem,
                )
            except TypeError as exc:
                if "snt_path" not in str(exc):
                    raise
                result = snt_core.convert(
                    dxf_path=temp_dxf,
                    description=output_path.stem,
                )

            produced = Path(getattr(result, "snt_path", temp_snt))
            if not produced.exists():
                fallback = temp_dxf.with_suffix(".snt")
                if fallback.exists():
                    produced = fallback
            if not produced.exists():
                raise RuntimeError(f"SNT conversion did not produce output file: {produced}")

            shutil.copy2(produced, output_path)
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def _prepare_manifest_blocks(self, saved_blocks):
        try:
            from shapely.geometry import Polygon
            from shapely.ops import unary_union
        except Exception:
            return [
                {
                    **item,
                    "export_rings": [self._closed_polygon_xy(item["polygon"])],
                    "label_position": self._block_label_position(item["block"], item["polygon"]),
                }
                for item in saved_blocks
            ]

        prepared = []
        higher_priority_geoms = []

        for item in saved_blocks:
            original_ring = self._closed_polygon_xy(item["polygon"])
            try:
                geom = Polygon(original_ring)
                if not geom.is_valid:
                    geom = geom.buffer(0)

                if higher_priority_geoms and not geom.is_empty:
                    # Only reshape the exported ring when it actually needs to be
                    # clipped against a higher-priority block. Reshaping unconditionally
                    # (e.g. shapely's buffer(0) repair of a self-intersecting ring from a
                    # sharp CL bend) can produce a polygon that no longer matches the
                    # bl_line_chains boundary already drawn on screen, so the reloaded
                    # SNT shows both the real boundary and a stray shapely-repaired sliver.
                    export_geom = geom.difference(unary_union(higher_priority_geoms))
                    if export_geom.is_empty:
                        export_geom = geom
                    rings = self._rings_from_geometry(export_geom)
                    label_position = self._label_position_from_geometry(export_geom, item)
                else:
                    rings = [original_ring]
                    label_position = self._block_label_position(item["block"], item["polygon"])

                if not rings:
                    rings = [original_ring]

                prepared.append({
                    **item,
                    "export_rings": rings,
                    "label_position": label_position,
                })

                if not geom.is_empty:
                    higher_priority_geoms.append(geom)
            except Exception:
                prepared.append({
                    **item,
                    "export_rings": [original_ring],
                    "label_position": self._block_label_position(item["block"], item["polygon"]),
                })

        return prepared

    @classmethod
    def _rings_from_geometry(cls, geom):
        if geom is None or geom.is_empty:
            return []

        geoms = []
        geom_type = getattr(geom, "geom_type", "")
        if geom_type == "Polygon":
            geoms = [geom]
        elif geom_type == "MultiPolygon":
            geoms = list(geom.geoms)
        else:
            return []

        rings = []
        for poly in geoms:
            if poly.is_empty or float(poly.area) <= 1e-9:
                continue
            ring = [(float(x), float(y)) for x, y in list(poly.exterior.coords)]
            if len(ring) >= 4:
                rings.append(ring)
        rings.sort(key=cls._ring_area_abs, reverse=True)
        return rings

    @staticmethod
    def _ring_area_abs(ring):
        area = 0.0
        pts = list(ring or [])
        for idx in range(len(pts) - 1):
            area += pts[idx][0] * pts[idx + 1][1] - pts[idx + 1][0] * pts[idx][1]
        return abs(area) * 0.5

    def _label_position_from_geometry(self, geom, item):
        z = self._block_label_position(item["block"], item["polygon"])[2]
        try:
            if geom is None or geom.is_empty:
                raise ValueError("empty geometry")
            point = geom.centroid
            if not geom.contains(point):
                point = geom.representative_point()
            return (float(point.x), float(point.y), float(z))
        except Exception:
            return self._block_label_position(item["block"], item["polygon"])

    @staticmethod
    def _closed_polygon_xy(polygon):
        pts = []
        for point in np.asarray(polygon, dtype=np.float64):
            if len(point) < 2:
                continue
            pts.append((float(point[0]), float(point[1])))
        if pts and (abs(pts[0][0] - pts[-1][0]) > 1e-9 or abs(pts[0][1] - pts[-1][1]) > 1e-9):
            pts.append(pts[0])
        return pts

    @staticmethod
    def _polyline_xyz(points):
        out = []
        for point in list(points or []):
            try:
                if point is None or len(point) < 2:
                    continue
                x = float(point[0])
                y = float(point[1])
                z = float(point[2]) if len(point) > 2 else 0.0
                if np.isfinite(x) and np.isfinite(y) and np.isfinite(z):
                    out.append((x, y, z))
            except Exception:
                continue
        return out

    @classmethod
    def _block_bl_line_chains(cls, item):
        chains = []
        block = item.get("block") if isinstance(item, dict) else None
        for drawing in (block or {}).get("drawings") or []:
            chain = cls._polyline_xyz(drawing.get("coords") or drawing.get("points"))
            if len(chain) >= 2:
                chains.append(chain)
        return chains

    @staticmethod
    def _block_label_position(block, polygon):
        above = list(block.get("above_pts") or [])
        below = list(block.get("below_pts") or [])
        if above and below:
            idx = min(len(above), len(below)) // 2
            idx = max(0, min(idx, len(above) - 1, len(below) - 1))
            a = above[idx]
            b = below[idx]
            z = ((float(a[2]) if len(a) > 2 else 0.0) + (float(b[2]) if len(b) > 2 else 0.0)) * 0.5
            return ((float(a[0]) + float(b[0])) * 0.5, (float(a[1]) + float(b[1])) * 0.5, z)

        pts = np.asarray(polygon, dtype=np.float64)
        return (float(np.mean(pts[:, 0])), float(np.mean(pts[:, 1])), 0.0)

    @staticmethod
    def _block_label_height(polygon, label):
        pts = np.asarray(polygon, dtype=np.float64)
        span_x = float(np.max(pts[:, 0]) - np.min(pts[:, 0])) if pts.size else 0.0
        span_y = float(np.max(pts[:, 1]) - np.min(pts[:, 1])) if pts.size else 0.0
        block_span = max(span_x, span_y, 1.0)
        by_name = block_span / max(len(str(label or "")) * 0.75, 1.0)
        return max(1.0, min(25.0, by_name))

    @staticmethod
    def _points_in_polygon(x, y, polygon):
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        inside = np.zeros(x.shape[0], dtype=bool)
        j = polygon.shape[0] - 1
        for i in range(polygon.shape[0]):
            xi, yi = polygon[i]
            xj, yj = polygon[j]
            crosses = ((yi > y) != (yj > y)) & (
                x < (xj - xi) * (y - yi) / (yj - yi + 1e-300) + xi
            )
            inside ^= crosses
            j = i
        return inside

    def _write_las_subset(self, output_path, data, mask, block):
        import laspy
        from gui.save_pointcloud import (
            _choose_point_format,
            _intensity_to_uint16,
            _rgb_to_las16,
        )

        xyz = np.asarray(data["xyz"])[mask]
        n_points = xyz.shape[0]

        classes = data.get("classification")
        if classes is None or np.asarray(classes).shape[0] != mask.shape[0]:
            classes = np.zeros(n_points, dtype=np.uint8)
        else:
            classes = np.asarray(classes)[mask].astype(np.uint8, copy=False)

        rgb = data.get("rgb")
        rgb_subset = None
        if rgb is not None and np.asarray(rgb).shape[0] == mask.shape[0]:
            rgb_subset = np.asarray(rgb)[mask]

        intensity = data.get("intensity")
        intensity_subset = None
        if intensity is not None and np.asarray(intensity).shape[0] == mask.shape[0]:
            intensity_subset = np.asarray(intensity)[mask]

        las_version = self._infer_las_version(data, classes)
        major, minor = map(int, las_version.split("."))
        rgb16 = _rgb_to_las16(rgb_subset)
        point_format_id = _choose_point_format(las_version, has_rgb=(rgb16 is not None))
        header = laspy.LasHeader(point_format=point_format_id, version=f"{major}.{minor}")

        try:
            import pyproj
            if getattr(self.app, "project_crs_wkt", None):
                header.parse_crs(pyproj.CRS.from_wkt(self.app.project_crs_wkt))
            elif getattr(self.app, "project_crs_epsg", None):
                header.parse_crs(pyproj.CRS.from_epsg(self.app.project_crs_epsg))
        except Exception as exc:
            print(f"Failed to embed split-file CRS: {exc}")

        las = laspy.LasData(header)
        las.x, las.y, las.z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
        las.classification = classes
        if rgb16 is not None:
            las.red, las.green, las.blue = rgb16[:, 0], rgb16[:, 1], rgb16[:, 2]
        intensity16 = _intensity_to_uint16(intensity_subset, n_points=n_points)
        if intensity16 is not None:
            las.intensity = intensity16

        drawing_data = self._serialize_block_drawings(block)
        if drawing_data:
            las.vlrs.append(laspy.VLR(
                user_id="NakshaAI",
                record_id=1002,
                description="Digitized drawings (lines, polygons, annotations)",
                record_data=drawing_data,
            ))

        las.write(str(output_path))

    @staticmethod
    def _infer_las_version(data, classes):
        version = data.get("input_format_version") or data.get("las_version") or data.get("version")
        if isinstance(version, tuple) and len(version) >= 2:
            las_version = f"{int(version[0])}.{int(version[1])}"
        else:
            las_version = str(version or "1.4")
        if las_version not in ("1.2", "1.4"):
            las_version = "1.4"
        max_cls = int(np.asarray(classes).max()) if np.asarray(classes).size else 0
        if max_cls > 31 and las_version == "1.2":
            las_version = "1.4"
        return las_version

    @staticmethod
    def _serialize_block_drawings(block):
        serializable = []
        for drawing in block.get("drawings", []):
            coords = drawing.get("coords")
            if coords is None:
                coords = drawing.get("points")
            if coords is None:
                continue
            try:
                points = np.asarray(coords, dtype=float).tolist()
            except Exception:
                continue
            if not points:
                continue
            serializable.append({
                "type": drawing.get("type", "smartline"),
                "points": points,
                "color": [float(c) for c in drawing.get("original_color", (0.0, 0.7, 1.0))],
                "width": float(drawing.get("original_width", 3)),
            })
        if not serializable:
            return b""
        return json.dumps(serializable, separators=(",", ":")).encode("utf-8")


class BlockCreationSettingsDialog(QDialog):
    def __init__(self, app, parent=None):
        _parent = parent if isinstance(parent, QWidget) else (app if isinstance(app, QWidget) else None)
        super().__init__(_parent)
        self.app = app
        self.setAttribute(Qt.WA_QuitOnClose, False)
        self.selected_cl_lines = []
        self._hovered_cl_line = None
        self._cl_select_active = False
        self._snt_cl_pseudo_drawings = []
        self._snt_hover_actor = None       # transient VTK actor for hovered SNT CL line
        self._snt_selected_actors = {}     # id(drawing) -> VTK actor for selected SNT CL lines
        self._chip: QWidget | None = None
        self._saved_geometry = None
        self._generated_block_drawings = []
        self._generated_blocks_metadata = []
        self._generated_block_pickability = []
        self._generated_block_snap_state = None
        self._generated_block_edit_scope = False
        self._edit_action_buttons = []
        self._value_editors = []
        self.setWindowTitle("Block Creation Settings")
        self.setModal(False)
        self.setWindowFlags(Qt.Dialog | Qt.WindowMinimizeButtonHint | Qt.WindowCloseButtonHint)
        self.setMinimumSize(400, 380)
        self.resize(400, 460)

        self.setStyleSheet(_get_block_dialog_style())
        self._build_ui()
        self._refresh_mode_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(0)

        content = QWidget()
        content.setObjectName("dialogContent")
        content_lay = QVBoxLayout(content)
        content_lay.setContentsMargins(2, 2, 2, 2)
        content_lay.setSpacing(8)
        root.addWidget(content)

        # All existing widgets go into content_lay; alias as root for the rest of this method.
        root = content_lay

        cl_group = QGroupBox("CL")
        cl_layout = QHBoxLayout(cl_group)
        cl_layout.setContentsMargins(10, 6, 10, 6)
        cl_layout.setSpacing(8)

        self.select_cl_btn = QPushButton("Select CL")
        self.select_cl_btn.setCheckable(True)
        self.select_cl_btn.setAutoDefault(False)
        self.select_cl_btn.setDefault(False)
        self.select_cl_btn.setCursor(Qt.PointingHandCursor)
        self.select_cl_btn.clicked.connect(self._toggle_cl_selection)
        cl_layout.addWidget(self.select_cl_btn, 0, Qt.AlignLeft)

        self.cl_selection_label = QLabel("No CL selected")
        self.cl_selection_label.setVisible(False)
        cl_layout.addWidget(self.cl_selection_label)
        cl_layout.addStretch()
        root.addWidget(cl_group)

        self.cl_popup = CLSelectionPopup(self)
        self.cl_popup.selection_confirmed.connect(self._on_cl_popup_done)

        settings_group = QGroupBox("Block Settings")
        form = QFormLayout(settings_group)
        form.setLabelAlignment(Qt.AlignRight)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)
        form.setContentsMargins(10, 6, 10, 6)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Symmetric (total width)", "symmetric")
        self.mode_combo.addItem("Asymmetric (above / below)", "asymmetric")
        self.mode_combo.currentIndexChanged.connect(self._refresh_mode_ui)
        form.addRow("Mode:", self.mode_combo)

        self.total_width_spin = QDoubleSpinBox()
        self.total_width_spin.setDecimals(4)
        self.total_width_spin.setRange(0.0, 999999.0)
        self.total_width_spin.setSingleStep(0.05)
        self.total_width_spin.setSuffix(" m")
        self.total_width_spin.setValue(2.0)
        self.total_width_spin.valueChanged.connect(self._refresh_summary)
        self._register_value_editor(self.total_width_spin)
        form.addRow("Total width:", self.total_width_spin)

        self.tol_above_spin = QDoubleSpinBox()
        self.tol_above_spin.setDecimals(4)
        self.tol_above_spin.setRange(0.0, 999999.0)
        self.tol_above_spin.setSingleStep(0.05)
        self.tol_above_spin.setSuffix(" m")
        self.tol_above_spin.setValue(1.0)
        self.tol_above_spin.valueChanged.connect(self._refresh_summary)
        self._register_value_editor(self.tol_above_spin)
        form.addRow("Tolerance above:", self.tol_above_spin)

        self.tol_below_spin = QDoubleSpinBox()
        self.tol_below_spin.setDecimals(4)
        self.tol_below_spin.setRange(0.0, 999999.0)
        self.tol_below_spin.setSingleStep(0.05)
        self.tol_below_spin.setSuffix(" m")
        self.tol_below_spin.setValue(1.0)
        self.tol_below_spin.valueChanged.connect(self._refresh_summary)
        self._register_value_editor(self.tol_below_spin)
        form.addRow("Tolerance below:", self.tol_below_spin)

        self.distance_spin = DistanceSpinBox()
        self.distance_spin.setDecimals(4)
        self.distance_spin.setRange(0.0, 999999.0)
        self.distance_spin.setSingleStep(0.05)
        self.distance_spin.setValue(0.0)
        self.distance_spin.valueChanged.connect(self._refresh_summary)
        self._register_value_editor(self.distance_spin)
        form.addRow("Distance:", self.distance_spin)

        root.addWidget(settings_group)

        self.edit_group = QGroupBox("Edit Before Split")
        self.edit_group.setVisible(False)
        edit_layout = QVBoxLayout(self.edit_group)
        edit_layout.setContentsMargins(10, 6, 10, 6)
        edit_layout.setSpacing(6)

        self.edit_status_label = QLabel(
            "After Create, select the cyan edge you want to change."
        )
        self.edit_status_label.setWordWrap(True)
        edit_layout.addWidget(self.edit_status_label)

        edit_row_one = QHBoxLayout()
        edit_row_one.setSpacing(8)

        self.edit_select_btn = QPushButton("Select")
        self.edit_select_btn.setAutoDefault(False)
        self.edit_select_btn.setDefault(False)
        self.edit_select_btn.setCursor(Qt.PointingHandCursor)
        self.edit_select_btn.clicked.connect(self._on_edit_select_clicked)
        edit_row_one.addWidget(self.edit_select_btn)

        self.edit_move_btn = QPushButton("Move")
        self.edit_move_btn.setAutoDefault(False)
        self.edit_move_btn.setDefault(False)
        self.edit_move_btn.setCursor(Qt.PointingHandCursor)
        self.edit_move_btn.clicked.connect(self._on_edit_move_clicked)
        edit_row_one.addWidget(self.edit_move_btn)

        self.edit_vertex_btn = QPushButton("Move Vertex")
        self.edit_vertex_btn.setAutoDefault(False)
        self.edit_vertex_btn.setDefault(False)
        self.edit_vertex_btn.setCursor(Qt.PointingHandCursor)
        self.edit_vertex_btn.clicked.connect(self._on_edit_move_vertex_clicked)
        edit_row_one.addWidget(self.edit_vertex_btn)
        edit_layout.addLayout(edit_row_one)

        edit_row_two = QHBoxLayout()
        edit_row_two.setSpacing(8)

        self.edit_extend_btn = QPushButton("Extend Edge")
        self.edit_extend_btn.setAutoDefault(False)
        self.edit_extend_btn.setDefault(False)
        self.edit_extend_btn.setCursor(Qt.PointingHandCursor)
        self.edit_extend_btn.clicked.connect(self._on_edit_extend_clicked)
        edit_row_two.addWidget(self.edit_extend_btn)

        self.edit_delete_btn = QPushButton("Delete Edge")
        self.edit_delete_btn.setAutoDefault(False)
        self.edit_delete_btn.setDefault(False)
        self.edit_delete_btn.setCursor(Qt.PointingHandCursor)
        self.edit_delete_btn.clicked.connect(self._on_edit_delete_clicked)
        edit_row_two.addWidget(self.edit_delete_btn)

        self.edit_done_btn = QPushButton("Done Edit")
        self.edit_done_btn.setAutoDefault(False)
        self.edit_done_btn.setDefault(False)
        self.edit_done_btn.setCursor(Qt.PointingHandCursor)
        self.edit_done_btn.clicked.connect(self._on_edit_done_clicked)
        edit_row_two.addWidget(self.edit_done_btn)
        edit_layout.addLayout(edit_row_two)

        self._edit_action_buttons = [
            self.edit_select_btn,
            self.edit_move_btn,
            self.edit_vertex_btn,
            self.edit_extend_btn,
            self.edit_delete_btn,
            self.edit_done_btn,
        ]

        root.addWidget(self.edit_group)

        btn_row = QHBoxLayout()

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setAutoDefault(False)
        self.clear_btn.setDefault(False)
        self.clear_btn.setCursor(Qt.PointingHandCursor)
        self.clear_btn.clicked.connect(self._clear_generated_blocks_and_reset)
        btn_row.addWidget(self.clear_btn, 0, Qt.AlignLeft)

        self.split_btn = QPushButton("Split")
        self.split_btn.setObjectName("splitButton")
        self.split_btn.setAutoDefault(False)
        self.split_btn.setDefault(False)
        self.split_btn.setCursor(Qt.PointingHandCursor)
        self.split_btn.clicked.connect(self._on_split_clicked)
        self.split_btn.setVisible(False)
        btn_row.addWidget(self.split_btn, 0, Qt.AlignLeft)

        btn_row.addStretch()

        self.create_btn = QPushButton("Create")
        self.create_btn.setObjectName("okButton")
        self.create_btn.setAutoDefault(False)
        self.create_btn.setDefault(False)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setAutoDefault(False)
        cancel_btn.setDefault(False)
        self.create_btn.clicked.connect(self._on_create_clicked)
        cancel_btn.clicked.connect(self._on_cancel_clicked)

        btn_row.addWidget(self.create_btn)
        btn_row.addWidget(cancel_btn)
        root.addLayout(btn_row)

        self._set_edit_controls_visible(False)

    def _register_value_editor(self, spin_box):
        spin_box.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self._value_editors.append(spin_box)
        spin_box.installEventFilter(self)
        line_edit = spin_box.lineEdit()
        if line_edit is not None:
            self._value_editors.append(line_edit)
            line_edit.installEventFilter(self)

    def _refresh_mode_ui(self):
        mode = self.mode_combo.currentData()
        is_symmetric = mode == "symmetric"
        self.total_width_spin.setEnabled(is_symmetric)
        self.tol_above_spin.setEnabled(not is_symmetric)
        self.tol_below_spin.setEnabled(not is_symmetric)
        if is_symmetric:
            half = float(self.total_width_spin.value()) * 0.5
            self.tol_above_spin.setValue(half)
            self.tol_below_spin.setValue(half)
        self._refresh_summary()

    def _refresh_summary(self):
        mode = self.mode_combo.currentData()
        if mode == "symmetric":
            width = float(self.total_width_spin.value())
            half = width * 0.5
            self.tol_above_spin.blockSignals(True)
            self.tol_below_spin.blockSignals(True)
            self.tol_above_spin.setValue(half)
            self.tol_below_spin.setValue(half)
            self.tol_above_spin.blockSignals(False)
            self.tol_below_spin.blockSignals(False)
            return

        return

    def _build_snt_cl_pseudo_drawings(self):
        """Return lightweight drawing-like dicts for CL-layer polylines in loaded SNT files.

        These are transient; they are rebuilt each time Select CL is armed and discarded
        when it is disarmed, so they hold no VTK resources and cannot leak.
        """
        result = []
        for attachment in getattr(self.app, "snt_attachments", []) or []:
            parsed = attachment.get("parsed") or {}
            entities = parsed.get("entities") or []
            fname = str(attachment.get("filename") or "SNT")
            for ent in entities:
                if not isinstance(ent, dict):
                    continue
                layer = str(ent.get("layer", "")).strip().upper()
                if layer != "CL":
                    continue
                etype = str(ent.get("type", "")).strip().upper()
                if etype not in ("POLYLINE", "LWPOLYLINE", "LINE"):
                    continue
                vertices = ent.get("vertices") or ent.get("coords") or ent.get("points") or []
                if len(vertices) < 2:
                    continue
                coords = [
                    (float(v[0]), float(v[1]), float(v[2]) if len(v) > 2 else 0.0)
                    for v in vertices
                ]
                result.append({
                    "type": "smartline",
                    "source": "snt_cl",
                    "layer": "CL",
                    "snt_filename": fname,
                    "coords": coords,
                })
        return result

    # ── SNT CL highlight helpers ──────────────────────────────────────────────

    def _make_snt_cl_actor(self, drawing, color, width):
        """Build a transient VTK overlay actor for an SNT CL pseudo-drawing."""
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            return None
        coords = drawing.get("coords", [])
        if len(coords) < 2:
            return None
        try:
            poly = digitizer._build_styled_polydata_world(coords, line_style="solid")
            origin = getattr(poly, "_world_origin", None)
            if origin is None:
                origin = np.zeros(3, dtype=np.float64)
            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputData(poly)
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            prop = actor.GetProperty()
            prop.SetColor(float(color[0]), float(color[1]), float(color[2]))
            prop.SetLineWidth(float(width))
            prop.SetOpacity(1.0)
            actor.PickableOff()
            actor.SetPosition(float(origin[0]), float(origin[1]), float(origin[2]))
            digitizer._add_actor_to_overlay(actor)
            return actor
        except Exception:
            return None

    def _remove_snt_cl_actor(self, actor):
        """Remove a transient SNT CL overlay actor from the renderer."""
        if actor is None:
            return
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            return
        try:
            digitizer._remove_actor_from_overlay(actor)
        except Exception:
            pass

    def _highlight_snt_cl_line(self, drawing):
        """Show a cyan highlight actor over a hovered SNT CL pseudo-drawing."""
        self._snt_hover_actor = self._make_snt_cl_actor(drawing, (0.0, 1.0, 1.0), 5)
        if self._snt_hover_actor is not None:
            self._render_view()

    def _unhighlight_snt_cl_line(self):
        """Remove the hover highlight actor for SNT CL lines."""
        if self._snt_hover_actor is not None:
            self._remove_snt_cl_actor(self._snt_hover_actor)
            self._snt_hover_actor = None
            self._render_view()

    def _select_snt_cl_line(self, drawing):
        """Add a persistent cyan selection actor for a selected SNT CL pseudo-drawing."""
        key = id(drawing)
        if key in self._snt_selected_actors:
            return
        actor = self._make_snt_cl_actor(drawing, (0.0, 1.0, 1.0), 5)
        if actor is not None:
            self._snt_selected_actors[key] = actor

    def _deselect_snt_cl_line(self, drawing):
        """Remove the selection actor for an SNT CL pseudo-drawing."""
        key = id(drawing)
        actor = self._snt_selected_actors.pop(key, None)
        self._remove_snt_cl_actor(actor)

    def _clear_all_snt_cl_actors(self):
        """Remove every transient SNT CL actor (hover + all selected)."""
        self._unhighlight_snt_cl_line()
        for actor in list(self._snt_selected_actors.values()):
            self._remove_snt_cl_actor(actor)
        self._snt_selected_actors.clear()

    # ─────────────────────────────────────────────────────────────────────────

    def _toggle_cl_selection(self, checked):
        self._cl_select_active = bool(checked)
        self.select_cl_btn.blockSignals(True)
        self.select_cl_btn.setChecked(self._cl_select_active)
        self.select_cl_btn.blockSignals(False)
        self.cl_selection_label.setVisible(bool(self.selected_cl_lines) and not self._cl_select_active)

        vtk_widget = getattr(self.app, "vtk_widget", None)
        if vtk_widget is not None:
            if self._cl_select_active:
                self._snt_cl_pseudo_drawings = self._build_snt_cl_pseudo_drawings()
                vtk_widget.installEventFilter(self)
                vtk_widget.setCursor(Qt.PointingHandCursor)
                if hasattr(self.app, "statusBar"):
                    n_snt = len(self._snt_cl_pseudo_drawings)
                    snt_hint = f" or a CL line from SNT ({n_snt} available)" if n_snt else ""
                    self.app.statusBar().showMessage(
                        f"Select CL active: click a smart line{snt_hint}, "
                        "or Shift+click to select multiple",
                        0,
                    )
            else:
                self._snt_cl_pseudo_drawings = []
                vtk_widget.removeEventFilter(self)
                vtk_widget.setCursor(Qt.ArrowCursor)
                self._clear_hover_cl_line()
                if hasattr(self.app, "statusBar"):
                    self.app.statusBar().showMessage("Select CL inactive", 2000)
                self._refresh_cl_selection_label()

    def eventFilter(self, obj, event):
        if any(editor is obj for editor in self._value_editors) and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                spin_box = obj if isinstance(obj, QDoubleSpinBox) else obj.parent()
                if isinstance(spin_box, QDoubleSpinBox):
                    spin_box.interpretText()
                    spin_box.selectAll()
                    return True

        if not self._cl_select_active or obj is not getattr(self.app, "vtk_widget", None):
            return super().eventFilter(obj, event)

        event_type = event.type()
        if event_type == QEvent.MouseMove:
            self._update_hover_cl_line(event)
            return False

        if event_type == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            self._select_hovered_cl_line(event)
            return True

        if event_type == QEvent.KeyPress and event.key() == Qt.Key_Escape:
            self._toggle_cl_selection(False)
            return True

        return False

    def _event_display_pos(self, event):
        pos = event.position() if hasattr(event, "position") else event.pos()
        x = int(pos.x())
        y = int(pos.y())
        vtk_widget = getattr(self.app, "vtk_widget", None)
        height = vtk_widget.height() if vtk_widget is not None else 0
        return x, max(0, height - y)

    def _find_smartline_at_event(self, event):
        drawings = self._find_smartlines_at_event(event)
        return drawings[0] if drawings else None

    def _is_selectable_cl_line(self, drawing):
        if not drawing:
            return False
        source = drawing.get("source", "")
        if source == "snt_cl":
            return True
        return (
            drawing.get("type") in ("smartline", "smart_line")
            and source != "block_creation"
        )

    def _has_selected_cl_line(self, drawing):
        return any(selected is drawing for selected in self.selected_cl_lines)

    def _is_shift_selection(self, event):
        try:
            if event is not None and event.modifiers() & Qt.ShiftModifier:
                return True
        except Exception:
            pass
        return bool(QApplication.keyboardModifiers() & Qt.ShiftModifier)

    def _find_smartlines_at_event(self, event, tolerance=25.0):
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            return []

        renderer = getattr(digitizer, "renderer", None)
        distance_fn = getattr(digitizer, "_distance_point_to_segment_2d", None)
        if renderer is None or distance_fn is None:
            x, y = self._event_display_pos(event)
            drawing = digitizer._get_drawing_under_cursor(x, y, tolerance=tolerance)
            return [drawing] if self._is_selectable_cl_line(drawing) else []

        x, y = self._event_display_pos(event)
        mouse_p = np.array([x, y])
        hits = []

        all_candidates = list(getattr(digitizer, "drawings", [])) + self._snt_cl_pseudo_drawings
        for drawing in all_candidates:
            if not self._is_selectable_cl_line(drawing):
                continue

            coords = drawing.get("coords", [])
            if len(coords) < 2:
                continue

            min_dist = None
            for idx in range(len(coords) - 1):
                p1_world = coords[idx]
                p2_world = coords[idx + 1]

                renderer.SetWorldPoint(p1_world[0], p1_world[1], p1_world[2], 1.0)
                renderer.WorldToDisplay()
                s1 = np.array(renderer.GetDisplayPoint()[:2])

                renderer.SetWorldPoint(p2_world[0], p2_world[1], p2_world[2], 1.0)
                renderer.WorldToDisplay()
                s2 = np.array(renderer.GetDisplayPoint()[:2])

                dist = float(distance_fn(mouse_p, s1, s2))
                if min_dist is None or dist < min_dist:
                    min_dist = dist

            if min_dist is not None and min_dist <= tolerance:
                hits.append((min_dist, drawing))

        hits.sort(key=lambda item: item[0])
        return [drawing for _, drawing in hits]

    def _update_hover_cl_line(self, event):
        candidates = self._find_smartlines_at_event(event)
        drawing = next((d for d in candidates if not self._has_selected_cl_line(d)), None)
        if drawing is self._hovered_cl_line:
            return

        self._clear_hover_cl_line()
        if drawing is None:
            return

        self._hovered_cl_line = drawing
        if drawing.get("source") == "snt_cl":
            self._highlight_snt_cl_line(drawing)
        else:
            digitizer = getattr(self.app, "digitizer", None)
            if digitizer is not None and hasattr(digitizer, "_highlight_line"):
                digitizer._highlight_line(drawing)
                self._render_view()

    def _select_hovered_cl_line(self, event=None):
        if self._is_shift_selection(event):
            drawings = self._find_smartlines_at_event(event)
        else:
            drawings = [self._hovered_cl_line]

        drawings = [drawing for drawing in drawings if self._is_selectable_cl_line(drawing)]
        if not drawings:
            return

        # Remove the hover actor before promoting to selection actor.
        self._unhighlight_snt_cl_line()

        for drawing in drawings:
            if not self._has_selected_cl_line(drawing):
                self.selected_cl_lines.append(drawing)
                if drawing.get("source") == "snt_cl":
                    self._select_snt_cl_line(drawing)
                else:
                    digitizer = getattr(self.app, "digitizer", None)
                    if digitizer is not None and hasattr(digitizer, "_highlight_line"):
                        digitizer._highlight_line(drawing)

        self._hovered_cl_line = None
        self._refresh_cl_selection_label()
        self._render_view()

    def _refresh_cl_selection_label(self):
        if not self.selected_cl_lines:
            self.cl_selection_label.setText("No CL selected")
            self.cl_selection_label.setVisible(False)
            self.cl_popup.hide()
            return

        labels = []
        for idx, d in enumerate(self.selected_cl_lines, 1):
            if d.get("source") == "snt_cl":
                fname = d.get("snt_filename", "SNT")
                labels.append(f"Line {idx} (SNT:{fname})")
            else:
                labels.append(f"Line {idx}")
        self.cl_selection_label.setText(", ".join(labels))
        self.cl_selection_label.setVisible(not self._cl_select_active)

        if not self._cl_select_active:
            self.cl_popup.hide()
            return

        self.cl_popup.update_selection(self.selected_cl_lines)
        self._position_cl_popup()
        self.cl_popup.show()
        # Keep the main dialog in front of the app window after the popup appears.
        # On Windows, showing a Qt.Tool child can push the parentless Qt.Window dialog
        # behind the main application window; raise_() restores the correct z-order.
        self.raise_()
        self.activateWindow()

    def _position_cl_popup(self):
        if not hasattr(self, "cl_popup") or self.cl_popup is None:
            return
        # mapToGlobal is unreliable when the dialog is not yet shown; ensure it is visible.
        if not self.isVisible():
            self.show()
        popup_pos = self.select_cl_btn.mapToGlobal(
            QPoint(self.select_cl_btn.width() + 5, 0)
        )
        self.cl_popup.move(popup_pos)

    def moveEvent(self, event):
        super().moveEvent(event)
        if hasattr(self, "cl_popup") and self.cl_popup.isVisible():
            self._position_cl_popup()

    def _on_cl_popup_done(self, lines):
        self.selected_cl_lines = lines
        self._toggle_cl_selection(False)

    def _clear_hover_cl_line(self):
        drawing = self._hovered_cl_line
        if drawing is None:
            return

        self._hovered_cl_line = None
        if drawing.get("source") == "snt_cl":
            self._unhighlight_snt_cl_line()
        else:
            if not self._has_selected_cl_line(drawing):
                digitizer = getattr(self.app, "digitizer", None)
                if digitizer is not None and hasattr(digitizer, "_unhighlight_line"):
                    digitizer._unhighlight_line(drawing)
                    self._render_view()

    def _render_view(self):
        vtk_widget = getattr(self.app, "vtk_widget", None)
        if vtk_widget is not None and hasattr(vtk_widget, "render"):
            vtk_widget.render()

    def _clear_selected_cl_lines(self):
        digitizer = getattr(self.app, "digitizer", None)
        for drawing in list(self.selected_cl_lines):
            if drawing.get("source") == "snt_cl":
                self._deselect_snt_cl_line(drawing)
            elif digitizer is not None and hasattr(digitizer, "_unhighlight_line"):
                try:
                    digitizer._unhighlight_line(drawing)
                except Exception:
                    pass

        self._clear_all_snt_cl_actors()
        self.selected_cl_lines = []
        self._hovered_cl_line = None
        self._snt_cl_pseudo_drawings = []
        if hasattr(self, "cl_popup"):
            self.cl_popup.hide()
        self._refresh_cl_selection_label()

    def _reset_settings_fields(self):
        self.mode_combo.setCurrentIndex(0)
        self.total_width_spin.setValue(2.0)
        self.tol_above_spin.setValue(1.0)
        self.tol_below_spin.setValue(1.0)
        self.distance_spin.setValue(0.0)
        self._refresh_mode_ui()

    def _clear_generated_blocks_and_reset(self):
        self._stop_generated_block_editing()
        self._remove_generated_block_drawings()
        self._clear_selected_cl_lines()
        self._reset_settings_fields()
        self._set_edit_controls_visible(False)
        self._set_edit_status(
            "After Create, select the cyan edge you want to change."
        )
        self._render_view()
        if hasattr(self, "split_btn"):
            self.split_btn.setVisible(False)
            self.split_btn.setEnabled(False)

    def _on_cancel_clicked(self):
        self._clear_generated_blocks_and_reset()
        self.reject()

    def _remove_generated_block_drawings(self):
        self._stop_generated_block_editing()
        self._toggle_cl_selection(False)

        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is not None:
            for drawing in list(self._generated_block_drawings):
                try:
                    if any(existing is drawing for existing in getattr(digitizer, "drawings", [])):
                        if hasattr(digitizer, "_remove_drawing"):
                            digitizer._remove_drawing(drawing)
                        else:
                            actor = drawing.get("actor")
                            if actor is not None and hasattr(digitizer, "_remove_actor_from_overlay"):
                                digitizer._remove_actor_from_overlay(actor)
                            digitizer.drawings.remove(drawing)
                except Exception as e:
                    print(f"⚠️ Failed to clear generated block drawing: {e}")

        self._generated_block_drawings = []
        self._generated_blocks_metadata = []
        setattr(self.app, "block_creation_generated_blocks", [])
        setattr(self.app, "block_creation_generated_source", None)

    def _set_edit_controls_visible(self, visible):
        if hasattr(self, "edit_group") and self.edit_group is not None:
            self.edit_group.setVisible(bool(visible))
        for button in list(getattr(self, "_edit_action_buttons", []) or []):
            try:
                button.setEnabled(bool(visible))
            except Exception:
                pass

    def _set_edit_status(self, message):
        if hasattr(self, "edit_status_label") and self.edit_status_label is not None:
            self.edit_status_label.setText(str(message or ""))

    def _generated_block_live_drawings(self):
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            return []

        live_ids = {id(d) for d in list(getattr(digitizer, "drawings", []) or [])}
        return [drawing for drawing in list(self._generated_block_drawings) if id(drawing) in live_ids]

    def _restore_generated_block_pickability(self):
        for actor, was_pickable in list(self._generated_block_pickability):
            if actor is None or was_pickable is None:
                continue
            try:
                if was_pickable:
                    actor.PickableOn()
                else:
                    actor.PickableOff()
            except Exception:
                pass
        self._generated_block_pickability = []

    def _activate_generated_block_edit_scope(self, clear_selection=False):
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            return False

        live_drawings = self._generated_block_live_drawings()
        if not live_drawings:
            QMessageBox.warning(
                self,
                "Edit Blocks",
                "Create the cyan preview edges first before using the edit tools.",
            )
            return False

        self._restore_generated_block_pickability()

        if self._generated_block_snap_state is None:
            self._generated_block_snap_state = (
                bool(getattr(digitizer, "snap_enabled", False)),
                getattr(digitizer, "snap_mode", None),
            )

        try:
            digitizer.snap_enabled = True
            if not getattr(digitizer, "snap_mode", None):
                digitizer.snap_mode = "nearby"
        except Exception:
            pass

        live_ids = {id(drawing) for drawing in live_drawings}
        setattr(digitizer, "_move_vertex_allowed_drawing_ids", set(live_ids))

        for drawing in list(getattr(digitizer, "drawings", []) or []):
            actor = drawing.get("actor") if isinstance(drawing, dict) else None
            if actor is None or not hasattr(actor, "GetPickable"):
                continue
            try:
                was_pickable = bool(actor.GetPickable())
            except Exception:
                was_pickable = None
            self._generated_block_pickability.append((actor, was_pickable))
            try:
                if id(drawing) in live_ids:
                    actor.PickableOn()
                else:
                    actor.PickableOff()
            except Exception:
                pass

        if clear_selection:
            try:
                digitizer.selection_manager.clear()
            except Exception:
                pass

        self._generated_block_edit_scope = True
        try:
            self.app.vtk_widget.render()
        except Exception:
            pass
        return True

    def _stop_generated_block_editing(self):
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            self._restore_generated_block_pickability()
            self._generated_block_edit_scope = False
            self._generated_block_snap_state = None
            return

        try:
            if getattr(digitizer, "active_tool", None) == "movevertex":
                if hasattr(digitizer, "_deactivate_active_tool_keep_drawings"):
                    digitizer._deactivate_active_tool_keep_drawings()
                elif hasattr(digitizer, "_deactivate_move_vertex_mode"):
                    digitizer._deactivate_move_vertex_mode()
        except Exception:
            pass

        try:
            if hasattr(digitizer, "deactivate_element_select_tool"):
                digitizer.deactivate_element_select_tool()
        except Exception:
            pass

        try:
            digitizer.selection_manager.clear()
        except Exception:
            pass

        if hasattr(digitizer, "_move_vertex_allowed_drawing_ids"):
            try:
                delattr(digitizer, "_move_vertex_allowed_drawing_ids")
            except Exception:
                setattr(digitizer, "_move_vertex_allowed_drawing_ids", None)
        self._restore_generated_block_pickability()

        if self._generated_block_snap_state is not None:
            try:
                old_enabled, old_mode = self._generated_block_snap_state
                digitizer.snap_enabled = bool(old_enabled)
                digitizer.snap_mode = old_mode
                if not old_enabled and hasattr(digitizer, "_hide_snap_marker_now"):
                    digitizer._hide_snap_marker_now()
            except Exception:
                pass
            self._generated_block_snap_state = None

        self._generated_block_edit_scope = False
        try:
            self.app.vtk_widget.render()
        except Exception:
            pass

    @staticmethod
    def _clean_block_edit_chain(points):
        cleaned = []
        for point in list(points or []):
            try:
                xyz = (
                    float(point[0]),
                    float(point[1]),
                    float(point[2]) if len(point) > 2 else 0.0,
                )
            except (TypeError, ValueError, IndexError):
                continue
            if not cleaned or np.hypot(xyz[0] - cleaned[-1][0], xyz[1] - cleaned[-1][1]) > 1e-9:
                cleaned.append(xyz)
        return cleaned

    def _tag_block_vertex_groups(self, drawings):
        """Remember which preview vertices are one logical block corner."""
        groups = []
        for drawing in list(drawings or []):
            original = self._clean_block_edit_chain(drawing.get("coords"))
            drawing["block_creation_original_coords"] = [tuple(point) for point in original]
            vertex_groups = [None] * len(original)
            drawing["block_creation_vertex_groups"] = vertex_groups

            for vertex_index, point in enumerate(original):
                group = next(
                    (
                        candidate
                        for candidate in groups
                        if np.hypot(point[0] - candidate["point"][0], point[1] - candidate["point"][1])
                        <= 1e-6
                    ),
                    None,
                )
                if group is None:
                    group = {"point": point, "members": []}
                    groups.append(group)
                group["members"].append((drawing, vertex_index))

        for group_index, group in enumerate(groups, 1):
            if len(group["members"]) < 2:
                continue
            group_id = f"block-junction-{group_index}"
            for drawing, vertex_index in group["members"]:
                drawing["block_creation_vertex_groups"][vertex_index] = group_id

    @staticmethod
    def _same_block_vertex(first, second, tolerance=1e-6):
        return np.hypot(float(first[0]) - float(second[0]), float(first[1]) - float(second[1])) <= tolerance

    def _normalised_block_drawings(self, drawings):
        """Return copied drawings with duplicated block corners made coherent."""
        normalised = []
        junctions = {}
        for drawing in list(drawings or []):
            copied = dict(drawing)
            copied["coords"] = self._clean_block_edit_chain(drawing.get("coords"))
            normalised.append(copied)

            original = drawing.get("block_creation_original_coords") or []
            vertex_groups = drawing.get("block_creation_vertex_groups") or []
            for vertex_index, junction_id in enumerate(vertex_groups):
                if (
                    junction_id is None
                    or vertex_index >= len(copied["coords"])
                    or vertex_index >= len(original)
                ):
                    continue
                junctions.setdefault(junction_id, []).append(
                    (copied, vertex_index, original[vertex_index])
                )

        conflicts = []
        for junction_id, members in junctions.items():
            moved = [
                copied["coords"][vertex_index]
                for copied, vertex_index, original in members
                if not self._same_block_vertex(copied["coords"][vertex_index], original)
            ]
            target = moved[0] if moved else members[0][0]["coords"][members[0][1]]
            if any(not self._same_block_vertex(point, target, tolerance=0.02) for point in moved[1:]):
                conflicts.append(junction_id)
                continue
            for copied, vertex_index, _original in members:
                copied["coords"][vertex_index] = tuple(target)

        # Also remove tiny gaps created by independent edits at two endpoints.
        endpoints = []
        for copied in normalised:
            coords = copied["coords"]
            if len(coords) < 2:
                continue
            endpoints.append((copied, 0))
            endpoints.append((copied, len(coords) - 1))

        visited = set()
        for start_index, (copied, vertex_index) in enumerate(endpoints):
            if start_index in visited:
                continue
            cluster = [start_index]
            visited.add(start_index)
            queue = [start_index]
            while queue:
                current_index = queue.pop()
                current_drawing, current_vertex = endpoints[current_index]
                current_point = current_drawing["coords"][current_vertex]
                for candidate_index, (candidate_drawing, candidate_vertex) in enumerate(endpoints):
                    if candidate_index in visited:
                        continue
                    candidate_point = candidate_drawing["coords"][candidate_vertex]
                    if self._same_block_vertex(current_point, candidate_point, tolerance=0.02):
                        visited.add(candidate_index)
                        queue.append(candidate_index)
                        cluster.append(candidate_index)
            if len(cluster) < 2:
                continue
            points = [endpoints[index][0]["coords"][endpoints[index][1]] for index in cluster]
            target = tuple(float(np.mean([point[axis] for point in points])) for axis in range(3))
            for endpoint_index in cluster:
                endpoint_drawing, endpoint_vertex = endpoints[endpoint_index]
                endpoint_drawing["coords"][endpoint_vertex] = target

        return normalised, conflicts

    def _cached_block_polygon(self, block):
        try:
            raw_polygon = np.asarray(block.get("polygon"), dtype=np.float64)
        except (TypeError, ValueError):
            return None
        if raw_polygon.ndim != 2 or raw_polygon.shape[0] < 3:
            return None

        ring = []
        for point in raw_polygon:
            xy = (float(point[0]), float(point[1]))
            if not ring or not self._same_block_vertex(xy, ring[-1]):
                ring.append(xy)
        if len(ring) < 3:
            return None
        if not self._same_block_vertex(ring[0], ring[-1]):
            ring.append(ring[0])
        return np.asarray(ring, dtype=np.float64)

    def _patched_cached_block_polygon(self, block, drawings):
        """Apply edited preview vertices to the original full block ring."""
        cached = self._cached_block_polygon(block)
        if cached is None:
            return None
        ring = np.array(cached[:-1], dtype=np.float64, copy=True)
        changed = False

        for drawing in list(drawings or []):
            original = drawing.get("block_creation_original_coords") or []
            current = self._clean_block_edit_chain(drawing.get("coords"))
            if len(original) != len(current):
                continue
            for original_point, current_point in zip(original, current):
                if self._same_block_vertex(original_point, current_point):
                    continue
                distances = np.hypot(ring[:, 0] - original_point[0], ring[:, 1] - original_point[1])
                for ring_index in np.flatnonzero(distances <= 1e-5):
                    ring[ring_index] = (float(current_point[0]), float(current_point[1]))
                    changed = True

        if not changed:
            return None
        patched = np.vstack((ring, ring[0]))
        try:
            from shapely.geometry import Polygon

            polygon = Polygon(patched)
            if polygon.is_empty or not polygon.is_valid or float(polygon.area) <= 1e-9:
                return None
        except Exception:
            return None
        return patched

    def _block_drawings_changed(self, block, live_drawings):
        original_drawings = list(block.get("drawings", []) or [])
        live_ids = {id(drawing) for drawing in live_drawings}
        if any(id(drawing) not in live_ids for drawing in original_drawings):
            return True

        for drawing in live_drawings:
            original = drawing.get("block_creation_original_coords")
            if original is None:
                continue
            current = self._clean_block_edit_chain(drawing.get("coords"))
            if len(current) != len(original):
                return True
            if any(
                not self._same_block_vertex(current_point, original_point)
                for current_point, original_point in zip(current, original)
            ):
                return True
        return False

    def _polygon_from_block_drawings(self, drawings):
        chains = []
        for drawing in list(drawings or []):
            coords = self._clean_block_edit_chain(
                drawing.get("coords") if isinstance(drawing, dict) else None
            )
            if len(coords) >= 2:
                chains.append(coords)

        if not chains:
            return None

        try:
            from shapely.geometry import LineString, Polygon
            from shapely.ops import polygonize, unary_union
        except Exception:
            return None

        try:
            line_geoms = []
            for chain in chains:
                xy = [(float(p[0]), float(p[1])) for p in chain]
                if len(xy) >= 2:
                    line_geoms.append(LineString(xy))
            if not line_geoms:
                return None

            merged = unary_union(line_geoms)
            polygons = [poly for poly in polygonize(merged) if not poly.is_empty and float(poly.area) > 1e-9]

            if not polygons:
                for chain in chains:
                    xy = [(float(p[0]), float(p[1])) for p in chain]
                    if len(xy) >= 4 and abs(xy[0][0] - xy[-1][0]) <= 1e-9 and abs(xy[0][1] - xy[-1][1]) <= 1e-9:
                        poly = Polygon(xy)
                        if not poly.is_empty and float(poly.area) > 1e-9:
                            polygons.append(poly)

            if not polygons:
                return None

            poly = max(polygons, key=lambda item: float(item.area))
            ring = [(float(x), float(y)) for x, y in list(poly.exterior.coords)]
            return np.array(ring, dtype=np.float64) if len(ring) >= 4 else None
        except Exception as exc:
            print(f"Block edit polygon rebuild failed: {exc}")
            return None

    def _refresh_generated_blocks_metadata_from_drawings(self):
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            return self._generated_blocks_metadata

        current_blocks = list(
            self._generated_blocks_metadata
            or getattr(self.app, "block_creation_generated_blocks", [])
            or []
        )
        if not current_blocks:
            return []

        live_ids = {id(d) for d in list(getattr(digitizer, "drawings", []) or [])}
        refreshed = []
        invalid_blocks = []

        for block in current_blocks:
            original_drawings = list(block.get("drawings", []) or [])
            live_drawings = [
                drawing for drawing in original_drawings
                if id(drawing) in live_ids
            ]
            if not live_drawings:
                invalid_blocks.append(int(block.get("priority", len(refreshed) + 1)))
                continue

            has_deleted_drawing = len(live_drawings) != len(original_drawings)
            changed = self._block_drawings_changed(block, live_drawings)
            normalised_drawings, conflicts = self._normalised_block_drawings(live_drawings)
            if conflicts:
                invalid_blocks.append(int(block.get("priority", len(refreshed) + 1)))
                continue

            if not changed:
                # Overlap clipping deliberately leaves some visible edge groups open.
                # Their cached block polygon is still the authoritative valid shape.
                polygon = self._cached_block_polygon(block)
            else:
                # A vertex move normally maps directly back to the full original ring.
                # This keeps clipped helper edges from turning a valid block into an open one.
                polygon = None if has_deleted_drawing else self._patched_cached_block_polygon(
                    block, normalised_drawings
                )
                if polygon is None:
                    polygon = self._polygon_from_block_drawings(normalised_drawings)
            if polygon is None or len(polygon) < 4:
                invalid_blocks.append(int(block.get("priority", len(refreshed) + 1)))
                continue

            updated = dict(block)
            updated["drawings"] = live_drawings
            updated["polygon"] = polygon
            updated["above_pts"] = []
            updated["below_pts"] = []
            refreshed.append(updated)

        if invalid_blocks:
            labels = ", ".join(str(item) for item in invalid_blocks)
            QMessageBox.warning(
                self,
                "Split Blocks",
                "Some edited blocks no longer form a closed polygon. "
                f"Fix or undo the edits for block priority: {labels}.",
            )
            return None

        self._generated_blocks_metadata = refreshed
        setattr(self.app, "block_creation_generated_blocks", refreshed)
        setattr(self.app, "block_creation_generated_source", self._current_source_path())
        return refreshed

    def _on_edit_select_clicked(self):
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None or not self._activate_generated_block_edit_scope(clear_selection=True):
            return
        digitizer.activate_element_select_tool(method="individual")
        self._set_edit_status(
            "Select the cyan edge you want to adjust."
        )

    def _on_edit_move_clicked(self):
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None or not self._activate_generated_block_edit_scope(clear_selection=False):
            return
        tool = digitizer.activate_element_select_tool(method="individual")
        if tool is not None and hasattr(tool, "enter_move_mode"):
            tool.enter_move_mode()
        self._set_edit_status(
            "Move the selected cyan edge."
        )

    def _on_edit_move_vertex_clicked(self):
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None or not self._activate_generated_block_edit_scope(clear_selection=False):
            return
        digitizer.vertex_move_constraint_mode = "free"
        digitizer.vertex_move_constraint_label = "Free 360 Move"
        digitizer.set_tool("movevertex")
        self._set_edit_status(
            "Move a cyan vertex, then click again to finish."
        )

    def _on_edit_extend_clicked(self):
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None or not self._activate_generated_block_edit_scope(clear_selection=False):
            return
        digitizer.extend_selected_element_distance_command()
        self._set_edit_status(
            "Extend the selected cyan edge."
        )

    def _on_edit_delete_clicked(self):
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None or not self._activate_generated_block_edit_scope(clear_selection=False):
            return
        deleted = digitizer.delete_selection()
        if deleted:
            self._set_edit_status(f"Deleted {deleted} selected cyan edge(s).")
        else:
            self._set_edit_status("Nothing deleted. Select a cyan edge first.")

    def _on_edit_done_clicked(self):
        self._stop_generated_block_editing()
        self._set_edit_status(
            "Editing finished. Split now or select again to continue."
        )

    def _on_split_clicked(self):
        live_preview_drawings = self._generated_block_live_drawings()
        blocks = self._generated_blocks_metadata or getattr(
            self.app, "block_creation_generated_blocks", []
        )
        had_existing_preview = bool(live_preview_drawings or blocks)
        generated_source = getattr(self.app, "block_creation_generated_source", None)
        current_source = self._current_source_path()
        if (
            had_existing_preview
            and generated_source
            and current_source
            and generated_source != current_source
        ):
            QMessageBox.warning(
                self,
                "Split Blocks",
                "The current cyan preview belongs to a different source file. Create the preview again before splitting.",
            )
            return

        if had_existing_preview:
            refreshed_blocks = self._refresh_generated_blocks_metadata_from_drawings()
            if refreshed_blocks is None:
                return
            blocks = refreshed_blocks
            if not blocks:
                QMessageBox.warning(
                    self,
                    "Split Blocks",
                    "No valid edited cyan preview remains to split. Rebuild the preview or undo the last edit.",
                )
                return
            self._stop_generated_block_editing()
        else:
            blocks = self._generate_block_lines()
            if blocks:
                self._stop_generated_block_editing()
        if not blocks:
            QMessageBox.warning(
                self,
                "Split Blocks",
                "Generate a preview first by selecting CL lines and creating the cyan block edges.",
            )
            return

        dialog = BlockSplitSaveDialog(self.app, blocks, parent=self)
        dialog.exec()
        if dialog.result() == QDialog.Accepted:
            self._clear_generated_blocks_and_reset()

    def _current_source_path(self):
        for attr in ("loaded_file", "current_file_path", "last_save_path"):
            value = getattr(self.app, attr, None)
            if value:
                return str(value)
        data = getattr(self.app, "data", None)
        if isinstance(data, dict):
            value = data.get("_source_file_path") or data.get("filename")
            if value:
                return str(value)
        return None

    def _split_polyline_by_distance(self, pts, max_distance):
        if len(pts) < 2 or max_distance <= 1e-9:
            return [pts]

        chunks = []
        current = [pts[0]]
        current_len = 0.0
        prev = pts[0]

        for idx in range(1, len(pts)):
            seg_start = prev
            seg_end = pts[idx]
            seg_vec = seg_end - seg_start
            seg_len = float(np.hypot(seg_vec[0], seg_vec[1]))

            if seg_len <= 1e-12:
                current.append(seg_end)
                prev = seg_end
                continue

            segment_cursor = seg_start
            remaining_seg_len = seg_len

            while current_len + remaining_seg_len > max_distance + 1e-9:
                needed = max_distance - current_len
                if needed <= 1e-9:
                    if len(current) >= 2:
                        chunks.append(np.array(current, dtype=np.float64))
                    current = [segment_cursor]
                    current_len = 0.0
                    continue

                t = needed / remaining_seg_len
                split_pt = segment_cursor + t * (seg_end - segment_cursor)
                current.append(split_pt)

                if len(current) >= 2:
                    chunks.append(np.array(current, dtype=np.float64))

                current = [split_pt]
                segment_cursor = split_pt
                remaining_seg_len = float(np.hypot(
                    seg_end[0] - segment_cursor[0],
                    seg_end[1] - segment_cursor[1],
                ))
                current_len = 0.0

                if remaining_seg_len <= 1e-12:
                    break

            if remaining_seg_len > 1e-12:
                current.append(seg_end)
                current_len += remaining_seg_len

            prev = seg_end

        if len(current) >= 2:
            chunks.append(np.array(current, dtype=np.float64))

        return chunks

    def _generate_block_lines(self):
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            print("No digitizer found")
            return []
        if not self.selected_cl_lines:
            print("No CL lines selected")
            return []

        self._remove_generated_block_drawings()

        mode = self.mode_combo.currentData()
        distance = float(self.distance_spin.value())

        if mode == "symmetric":
            total_width = float(self.total_width_spin.value())
            tol_above = total_width * 0.5
            tol_below = total_width * 0.5
        else:
            tol_above = float(self.tol_above_spin.value())
            tol_below = float(self.tol_below_spin.value())

        block_color = (0.0, 0.7, 1.0)
        count = 0
        block_records = []

        print(f"CL count={len(self.selected_cl_lines)}")

        # Compute a shared world-coordinate origin so that all block geometry math
        # (normal offsets, polygon clipping, point-in-polygon ray casting) operates on
        # small numbers.  Large survey coordinates (e.g. 600 000 m) cause catastrophic
        # cancellation in the floating-point arithmetic used by the clipping helpers,
        # producing wrong inside/outside results and distorted block shapes for SNT CL
        # lines.  Subtracting this origin here and adding it back when creating VTK
        # actors (which already handle large coords via _build_styled_polydata_world)
        # eliminates the cancellation without touching any other code path.
        first_coords = next(
            (d.get("coords", []) for d in self.selected_cl_lines if d.get("coords")),
            [],
        )
        if first_coords:
            _ox = float(first_coords[0][0])
            _oy = float(first_coords[0][1])
            _oz = float(first_coords[0][2]) if len(first_coords[0]) > 2 else 0.0
        else:
            _ox = _oy = _oz = 0.0

        def _to_local(coords_list):
            """Shift a list of (x,y,z) tuples to local coords."""
            return [
                (float(p[0]) - _ox, float(p[1]) - _oy,
                 float(p[2]) - _oz if len(p) > 2 else -_oz)
                for p in coords_list
            ]

        def _to_world(coords_list):
            """Shift a list of (x,y,z) tuples back to world coords."""
            return [
                (float(p[0]) + _ox, float(p[1]) + _oy, float(p[2]) + _oz)
                for p in coords_list
            ]

        # ---- Pass 1: compute block geometry for every CL line in selection-priority order ----
        all_block_data = []   # list-of-lists: one inner list per CL line

        for cl_priority, cl_drawing in enumerate(self.selected_cl_lines):
            coords = cl_drawing.get("coords", [])
            print(f"CL#{cl_priority} coords len={len(coords)}")
            if len(coords) < 2:
                continue
            pts = np.array(_to_local(coords), dtype=np.float64)
            chunks = self._split_polyline_by_distance(pts, distance) if distance > 0 else [pts]

            cl_blocks = []
            for idx, chunk_pts in enumerate(chunks):
                block_data = self._compute_block_geometry(
                    chunk_pts, tol_above, tol_below,
                    is_first_block=(idx == 0),
                    is_last_block=(idx == len(chunks) - 1),
                )
                if block_data is not None:
                    block_data["cl_pts"] = [tuple(p) for p in chunk_pts]
                    cl_blocks.append(block_data)
            all_block_data.append(cl_blocks)

        # ---- Pass 2: draw, clipping lower-priority blocks against higher-priority polygons ----
        # All geometry in this pass is in local (origin-subtracted) coordinates so that
        # the polygon clipping math operates on small numbers (no catastrophic cancellation).
        # Chains are converted back to world coords before being passed to _make_polyline_actor
        # and stored in records.
        priority_polygons = []  # closed 2-D polygon arrays, highest priority first (local coords)

        for cl_priority, cl_blocks in enumerate(all_block_data):
            for block_data in cl_blocks:
                above_pts       = block_data["above_pts"]
                below_pts       = block_data["below_pts"]
                start_cap_above = block_data["start_cap_above"]
                start_cap_below = block_data["start_cap_below"]
                end_cap_above   = block_data["end_cap_above"]
                end_cap_below   = block_data["end_cap_below"]

                line_sets = [
                    (above_pts,                          "above"),
                    (below_pts,                          "below"),
                    ([start_cap_above, start_cap_below], "start_cap"),
                    ([end_cap_above,   end_cap_below],   "end_cap"),
                ]

                drawing_group = []
                for seg_pts, seg_name in line_sets:
                    if len(seg_pts) < 2:
                        continue

                    # Clip this polyline against every higher-priority block polygon
                    # (clipping uses local coords — correct, no large-number precision issue)
                    surviving_chains = [seg_pts]
                    for higher_poly in priority_polygons:
                        next_chains = []
                        for chain in surviving_chains:
                            next_chains.extend(
                                self._clip_polyline_outside_polygon(chain, higher_poly)
                            )
                        surviving_chains = next_chains

                    for chain in surviving_chains:
                        if len(chain) < 2:
                            continue
                        try:
                            # Convert chain from local back to world coords for the actor.
                            world_chain = _to_world(chain)
                            actor = digitizer._make_polyline_actor(
                                world_chain, color=block_color, width=3, line_style="solid"
                            )
                            if actor is None:
                                continue
                            actor.PickableOn()
                            digitizer._add_actor_to_overlay(actor)
                            drawing = {
                                "type": "smartline",
                                "coords": world_chain,
                                "actor": actor,
                                "bounds": actor.GetBounds(),
                                "original_color": block_color,
                                "original_width": 3,
                                "original_style": "solid",
                                "source": "block_creation",
                            }
                            digitizer.drawings.append(drawing)
                            self._generated_block_drawings.append(drawing)
                            drawing_group.append(drawing)
                            count += 1
                        except Exception as e:
                            print(f"Failed to create {seg_name}: {e}")

                # Register this block polygon (local coords) for clipping subsequent blocks.
                # Build a world-coord polygon separately for export and point-assignment.
                poly_local = self._build_block_polygon(above_pts, below_pts)
                if poly_local is not None:
                    self._tag_block_vertex_groups(drawing_group)
                    priority_polygons.append(poly_local)
                    # Shift the polygon back to world coords for downstream use
                    poly_world = poly_local + np.array([_ox, _oy], dtype=np.float64)
                    block_records.append({
                        "priority": len(block_records) + 1,
                        "cl_priority": cl_priority + 1,
                        "distance": distance,
                        "source_path": self._current_source_path(),
                        "polygon": poly_world,
                        "cl_pts": _to_world(list(block_data.get("cl_pts", []))),
                        "above_pts": _to_world(list(above_pts)),
                        "below_pts": _to_world(list(below_pts)),
                        "drawings": drawing_group,
                    })

        print(f"Created {count} block lines")
        if count > 0:
            self.app.vtk_widget.render()
        self._generated_blocks_metadata = block_records
        setattr(self.app, "block_creation_generated_blocks", block_records)
        setattr(self.app, "block_creation_generated_source", self._current_source_path())
        return block_records

    # -------------------------------------------------------------------------
    #  Block geometry helpers
    # -------------------------------------------------------------------------

    def _compute_block_geometry(self, pts, tol_above, tol_below, is_first_block, is_last_block):
        """Compute offset boundary lists and cap endpoints for one chunk."""
        above_pts = []
        below_pts = []

        for i in range(len(pts)):
            if i == 0:
                direction = pts[1] - pts[0]
                length = np.hypot(direction[0], direction[1])
                if length < 1e-12:
                    above_pts.append(tuple(pts[i]))
                    below_pts.append(tuple(pts[i]))
                    continue
                d = direction[:2] / length
                n = np.array([-d[1], d[0]])
                miter_factor = 1.0
            elif i == len(pts) - 1:
                direction = pts[i] - pts[i - 1]
                length = np.hypot(direction[0], direction[1])
                if length < 1e-12:
                    above_pts.append(tuple(pts[i]))
                    below_pts.append(tuple(pts[i]))
                    continue
                d = direction[:2] / length
                n = np.array([-d[1], d[0]])
                miter_factor = 1.0
            else:
                p_prev = pts[i - 1][:2]
                p_curr = pts[i][:2]
                p_next = pts[i + 1][:2]

                v1 = p_curr - p_prev
                v1_len = np.hypot(v1[0], v1[1])
                v2 = p_next - p_curr
                v2_len = np.hypot(v2[0], v2[1])

                if v1_len > 1e-12 and v2_len > 1e-12:
                    v1_u = v1 / v1_len
                    v2_u = v2 / v2_len

                    n1 = np.array([-v1_u[1], v1_u[0]])
                    n2 = np.array([-v2_u[1], v2_u[0]])

                    n_sum = n1 + n2
                    n_sum_len = np.hypot(n_sum[0], n_sum[1])

                    if n_sum_len > 1e-6:
                        n = n_sum / n_sum_len
                        cos_half = float(np.dot(n, n1))
                        if cos_half > 1e-6:
                            miter_factor = 1.0 / cos_half
                            # Cap the miter factor to achieve a balance between line and vertex
                            miter_factor = min(miter_factor, 1.5)
                        else:
                            miter_factor = 1.0
                    else:
                        n = n1
                        miter_factor = 1.0
                else:
                    direction = (pts[i + 1] - pts[i - 1]) * 0.5
                    length = np.hypot(direction[0], direction[1])
                    if length < 1e-12:
                        above_pts.append(tuple(pts[i]))
                        below_pts.append(tuple(pts[i]))
                        continue
                    d = direction[:2] / length
                    n = np.array([-d[1], d[0]])
                    miter_factor = 1.0

            z = float(pts[i][2]) if len(pts[i]) > 2 else 0.0
            above_pts.append((
                float(pts[i][0] + n[0] * tol_above * miter_factor),
                float(pts[i][1] + n[1] * tol_above * miter_factor),
                z,
            ))
            below_pts.append((
                float(pts[i][0] - n[0] * tol_below * miter_factor),
                float(pts[i][1] - n[1] * tol_below * miter_factor),
                z,
            ))

        if len(above_pts) < 2 or len(below_pts) < 2:
            return None

        d_start = (pts[1] - pts[0])[:2]
        d_start_len = np.hypot(d_start[0], d_start[1])
        d_start = d_start / d_start_len if d_start_len > 1e-12 else np.array([1.0, 0.0])

        d_end = (pts[-1] - pts[-2])[:2]
        d_end_len = np.hypot(d_end[0], d_end[1])
        d_end = d_end / d_end_len if d_end_len > 1e-12 else np.array([1.0, 0.0])

        n_start = np.array([-d_start[1], d_start[0]])
        n_end   = np.array([-d_end[1],   d_end[0]])

        z0    = float(pts[0][2])  if len(pts[0])  > 2 else 0.0
        z_end = float(pts[-1][2]) if len(pts[-1]) > 2 else 0.0

        if is_first_block:
            start_cap_above = (
                float(pts[0][0] - d_start[0] * tol_above + n_start[0] * tol_above),
                float(pts[0][1] - d_start[1] * tol_above + n_start[1] * tol_above),
                z0,
            )
            start_cap_below = (
                float(pts[0][0] - d_start[0] * tol_below - n_start[0] * tol_below),
                float(pts[0][1] - d_start[1] * tol_below - n_start[1] * tol_below),
                z0,
            )
            above_pts.insert(0, start_cap_above)
            below_pts.insert(0, start_cap_below)
        else:
            start_cap_above = above_pts[0]
            start_cap_below = below_pts[0]

        if is_last_block:
            end_cap_above = (
                float(pts[-1][0] + d_end[0] * tol_above + n_end[0] * tol_above),
                float(pts[-1][1] + d_end[1] * tol_above + n_end[1] * tol_above),
                z_end,
            )
            end_cap_below = (
                float(pts[-1][0] + d_end[0] * tol_below - n_end[0] * tol_below),
                float(pts[-1][1] + d_end[1] * tol_below - n_end[1] * tol_below),
                z_end,
            )
            above_pts.append(end_cap_above)
            below_pts.append(end_cap_below)
        else:
            end_cap_above = above_pts[-1]
            end_cap_below = below_pts[-1]

        return {
            "above_pts":       above_pts,
            "below_pts":       below_pts,
            "start_cap_above": start_cap_above,
            "start_cap_below": start_cap_below,
            "end_cap_above":   end_cap_above,
            "end_cap_below":   end_cap_below,
        }

    def _build_block_polygon(self, above_pts, below_pts):
        """Return a closed 2-D polygon (Nx2 numpy array) from block boundary lists."""
        try:
            ring = []
            for p in above_pts:
                ring.append((float(p[0]), float(p[1])))
            for p in reversed(below_pts):
                ring.append((float(p[0]), float(p[1])))
            if len(ring) < 3:
                return None
            return np.array(ring, dtype=np.float64)
        except Exception:
            return None

    # -------------------------------------------------------------------------
    #  Polyline-vs-polygon clipping helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _point_in_polygon_2d(px, py, polygon):
        """Ray-casting point-in-polygon test (2-D). polygon is Nx2 numpy array."""
        n = len(polygon)
        inside = False
        j = n - 1
        for i in range(n):
            xi, yi = polygon[i]
            xj, yj = polygon[j]
            if ((yi > py) != (yj > py)) and (
                px < (xj - xi) * (py - yi) / (yj - yi + 1e-300) + xi
            ):
                inside = not inside
            j = i
        return inside

    @staticmethod
    def _segment_polygon_intersections(p1, p2, polygon):
        """
        Find all t in (0,1) where segment p1->p2 crosses a polygon edge.
        Returns sorted list of (t, (ix, iy)).
        """
        x1, y1 = float(p1[0]), float(p1[1])
        x2, y2 = float(p2[0]), float(p2[1])
        dx, dy = x2 - x1, y2 - y1

        hits = []
        n = len(polygon)
        for i in range(n):
            ax, ay = polygon[i]
            bx, by = polygon[(i + 1) % n]
            ex, ey = bx - ax, by - ay

            denom = dx * ey - dy * ex
            if abs(denom) < 1e-12:
                continue

            t = ((ax - x1) * ey - (ay - y1) * ex) / denom
            u = ((ax - x1) * dy - (ay - y1) * dx) / denom

            if 1e-9 < t < 1.0 - 1e-9 and 0.0 <= u <= 1.0:
                hits.append((t, (x1 + t * dx, y1 + t * dy)))

        hits.sort(key=lambda h: h[0])
        return hits

    def _clip_polyline_outside_polygon(self, polyline_pts, polygon):
        """
        Clip polyline_pts retaining only the portions that lie OUTSIDE the polygon.
        Returns a list of sub-chains (each is a list of (x,y,z) tuples).
        """
        if polygon is None or len(polygon) < 3 or len(polyline_pts) < 2:
            return [polyline_pts]

        def z_of(p):
            return float(p[2]) if len(p) > 2 else 0.0

        def get_pt(p1, p2, t):
            x = float(p1[0]) + t * (float(p2[0]) - float(p1[0]))
            y = float(p1[1]) + t * (float(p2[1]) - float(p1[1]))
            z = z_of(p1) + t * (z_of(p2) - z_of(p1))
            return (x, y, z)

        # Collect all kept sub-segments
        kept_segments = []

        for seg_idx in range(len(polyline_pts) - 1):
            p1 = polyline_pts[seg_idx]
            p2 = polyline_pts[seg_idx + 1]

            # Find intersections in (0, 1)
            hits = self._segment_polygon_intersections(p1, p2, polygon)
            t_vals = [0.0]
            for t, _ in hits:
                if t > 1e-9 and t < 1.0 - 1e-9:
                    t_vals.append(t)
            t_vals.append(1.0)
            t_vals.sort()

            for idx in range(len(t_vals) - 1):
                ta = t_vals[idx]
                tb = t_vals[idx + 1]
                if tb - ta < 1e-9:
                    continue

                # Check the midpoint of this sub-segment
                t_mid = (ta + tb) * 0.5
                mid_pt = get_pt(p1, p2, t_mid)

                if not self._point_in_polygon_2d(mid_pt[0], mid_pt[1], polygon):
                    # Outside! Keep this sub-segment
                    sub_start = get_pt(p1, p2, ta)
                    sub_end = get_pt(p1, p2, tb)
                    kept_segments.append((sub_start, sub_end))

        # Assemble the kept segments into continuous chains
        chains = []
        for start_pt, end_pt in kept_segments:
            if chains and np.hypot(chains[-1][-1][0] - start_pt[0], chains[-1][-1][1] - start_pt[1]) < 1e-6:
                chains[-1].append(end_pt)
            else:
                chains.append([start_pt, end_pt])

        return [c for c in chains if len(c) >= 2]

    def _legacy_translucent_paint_event(self, event):
        """Retained for source compatibility; native window painting is now used."""
        return

        # The old frameless implementation below is intentionally unreachable.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        from gui.theme_manager import ThemeColors as _TC
        w, h, r = self.width(), self.height(), _CORNER_RADIUS

        path = QPainterPath()
        path.addRoundedRect(1, 1, w - 2, h - 2, r, r)

        # Base fill
        painter.fillPath(path, QColor(_TC.get("bg_secondary")))

        # Top highlight — thin bright line gives the "raised lid" 3D feel.
        highlight = QLinearGradient(0, 0, 0, r * 2)
        highlight.setColorAt(0.0, QColor(255, 255, 255, 35))
        highlight.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillPath(path, highlight)

        # Outer border
        painter.setPen(QColor(_TC.get("border_light")))
        painter.drawPath(path)

        # Bottom shadow line — dark edge gives depth.
        shadow_path = QPainterPath()
        shadow_path.addRoundedRect(2, 2, w - 4, h - 4, max(0, r - 1), max(0, r - 1))
        painter.setPen(QColor(0, 0, 0, 60))
        painter.drawPath(shadow_path)

    def _on_create_clicked(self):
        blocks = self._generate_block_lines()
        has_blocks = bool(blocks)
        self.split_btn.setVisible(has_blocks)
        self.split_btn.setEnabled(has_blocks)
        self._set_edit_controls_visible(has_blocks)
        if has_blocks:
            self._set_edit_status(
                "Cyan block edges created. You can now edit individual edges before Split."
            )
        else:
            self._set_edit_status("No cyan block edges were created.")

    def accept(self):
        super().accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "cl_popup") and self.cl_popup.isVisible():
            self._position_cl_popup()

    def changeEvent(self, event):
        super().changeEvent(event)

    def _status_bar_for_chip(self):
        app = getattr(self, "app", None)
        status_bar = getattr(app, "status", None)
        if status_bar is None and hasattr(app, "statusBar"):
            try:
                status_bar = app.statusBar()
            except Exception:
                status_bar = None
        return status_bar

    def _do_minimize_to_chip(self):
        """Hide dialog and show a restore chip in the app's status bar."""
        if self._chip is not None:
            return  # already minimized to chip

        status_bar = self._status_bar_for_chip()
        if status_bar is None:
            self.hide()
            return

        from gui.theme_manager import ThemeColors as _TC
        chip = QWidget()
        chip.setObjectName("blockChip")
        chip.setFixedHeight(22)
        chip.setCursor(Qt.PointingHandCursor)
        chip.setStyleSheet(f"""
            QWidget#blockChip {{
                background-color: {_TC.get('bg_secondary')};
                border: 1px solid {_TC.get('accent')};
                border-radius: 4px;
            }}
            QLabel {{ color: {_TC.get('accent')}; font-size: 10px;
                      font-weight: bold; background: transparent; }}
            QPushButton {{
                background: transparent; border: none;
                color: {_TC.get('accent')}; font-size: 10px;
                font-weight: bold; padding: 0 4px; min-width: 0;
            }}
            QPushButton:hover {{ color: {_TC.get('accent_hover')}; }}
        """)
        lay = QHBoxLayout(chip)
        lay.setContentsMargins(5, 0, 5, 0)
        lay.setSpacing(4)
        lay.addWidget(QLabel("◼ Block Settings"))
        restore_btn = QPushButton("▲")
        restore_btn.setToolTip("Restore Block Creation Settings")
        lay.addWidget(restore_btn)

        def _restore():
            self._do_restore_from_chip()

        restore_btn.clicked.connect(_restore)
        chip.mousePressEvent = lambda e: _restore()

        self._chip = chip
        status_bar.addPermanentWidget(chip)
        chip.show()
        self.hide()

    def _do_restore_from_chip(self):
        """Remove status-bar chip and restore the dialog."""
        chip = self._chip
        self._chip = None
        if chip is not None:
            status_bar = self._status_bar_for_chip()
            if status_bar is not None:
                try:
                    status_bar.removeWidget(chip)
                except Exception:
                    pass
            try:
                chip.hide()
                chip.deleteLater()
            except Exception:
                pass

        self.show()
        self.raise_()
        self.activateWindow()

    @property
    def _is_minimized_to_chip(self):
        return self._chip is not None and not self.isVisible()

    def closeEvent(self, event):
        self._clear_generated_blocks_and_reset()
        self._toggle_cl_selection(False)
        self._clear_all_snt_cl_actors()
        if hasattr(self, "cl_popup"):
            self.cl_popup.hide()
        if self._chip is not None:
            status_bar = self._status_bar_for_chip()
            if status_bar is not None:
                try:
                    status_bar.removeWidget(self._chip)
                except Exception:
                    pass
            try:
                self._chip.hide()
                self._chip.deleteLater()
            except Exception:
                pass
            self._chip = None
        super().closeEvent(event)

    def blink(self):
        """Bring the dialog to the front without disturbing interaction state."""
        from PySide6.QtWidgets import QApplication

        try:
            self.setWindowOpacity(1.0)
        except Exception:
            pass
        self.show()
        self.raise_()
        self.activateWindow()
        QApplication.alert(self)


def show_block_creation_settings_dialog(app):
    dialog = BlockCreationSettingsDialog(app)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
