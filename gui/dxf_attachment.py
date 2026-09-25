
"""
DXF Attachment System with Multiple File Support and Management

Features:
- Select multiple DXF files at once
- Auto-detect matching .PRJ files for each DXF
- Manage attached DXFs with remove capability
- Coordinate reprojection support
- Overlay/underlay modes
"""

import os
import numpy as np
from pathlib import Path
from typing import Set, Dict, List, Tuple, Optional
from gui.popup_guard import InputPopupMixin
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFileDialog, QMessageBox, QComboBox, QCheckBox, QGroupBox,
    QRadioButton, QButtonGroup, QSpinBox, QDoubleSpinBox,
    QListWidget, QListWidgetItem, QWidget, QScrollArea,
    QMenu, QInputDialog, QColorDialog, QLineEdit,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QFrame, QProgressDialog
)
from PySide6.QtCore import Qt, Signal, QCoreApplication, QEvent, QThread, QSize, QTimer
from PySide6.QtGui import QFont, QColor, QIcon, QPixmap, QPainter, QPen
from gui.theme_manager import get_dialog_stylesheet, get_progress_dialog_stylesheet, get_title_banner_style, get_file_item_row_style, get_badge_style, get_icon_button_style, get_notice_banner_style, ThemeColors
from gui.minimize_chip import MinimizableDialogMixin
try:
    import ezdxf
    from pyproj import CRS, Transformer
    DEPENDENCIES_AVAILABLE = True
except ImportError:
    DEPENDENCIES_AVAILABLE = False
    print("⚠️ Missing dependencies: pip install ezdxf pyproj")

_ACI_RGB = {
      1: (255,   0,   0),    2: (255, 255,   0),    3: (  0, 255,   0),
      4: (  0, 255, 255),    5: (  0,   0, 255),    6: (255,   0, 255),
      7: (255, 255, 255),    8: (128, 128, 128),    9: (192, 192, 192),
     10: (255,   0,   0),   20: (255, 127,   0),   30: (255, 191,   0),
     40: (255, 255,   0),   50: (127, 255,   0),   60: (  0, 255,   0),
     70: (  0, 255, 127),   80: (  0, 255, 255),   90: (  0, 127, 255),
    100: (  0,   0, 255),  110: (127,   0, 255),  120: (255,   0, 255),
    130: (255,   0, 127),  140: (255, 127, 127),  150: (255, 200, 127),
    160: (255, 255, 127),  170: (200, 255, 127),  180: (127, 255, 127),
    190: (127, 255, 200),  200: (127, 255, 255),  210: (127, 200, 255),
    220: (127, 127, 255),  230: (200, 127, 255),  240: (255, 127, 255),
    250: (  0,   0,   0),  251: ( 42,  42,  42),  252: ( 84,  84,  84),
    253: (127, 127, 127),  254: (170, 170, 170),  255: (255, 255, 255),
}

def _true_color_to_rgb(tc: int) -> Tuple[int, int, int]:
    """Decode a 24-bit true color integer (group code 420) into (r, g, b)."""
    return ((tc >> 16) & 0xFF, (tc >> 8) & 0xFF, tc & 0xFF)

from PySide6.QtCore import QThread, Signal
import traceback

# ============================================================================
# NEW: Background Worker Thread - Add this to your file
# ============================================================================

class DXFProcessWorker(QThread):
    """
    Background thread worker for processing DXF files
    Prevents UI freezing during heavy operations
    """
    progress = Signal(int, str)  # (percentage, status_message)
    finished = Signal(list)      # Emits list of processed attachments
    error = Signal(str)          # Emits error message
    
    def __init__(self, items, project_crs=None):
        super().__init__()
        self.items = items
        self.project_crs = project_crs
        self._is_cancelled = False
    
    def cancel(self):
        """Cancel the processing"""
        self._is_cancelled = True
    
    def run(self):
        """Run in background thread - DO NOT touch UI here!"""
        try:
            import ezdxf
            from pyproj import Transformer
            import numpy as np
            
            all_attachments = []
            total = len(self.items)
            
            for idx, item in enumerate(self.items):
                if self._is_cancelled:
                    return
                
                # Report progress
                progress_pct = int((idx / total) * 100)
                self.progress.emit(progress_pct, f"Processing {item.dxf_path.name}...")
                
                # Process the DXF file
                attachment_data = self._process_single_dxf(item)
                if attachment_data:
                    all_attachments.append(attachment_data)
            
            # Done!
            self.progress.emit(100, "Processing complete")
            self.finished.emit(all_attachments)
            
        except Exception as e:
            self.error.emit(f"Processing failed: {str(e)}\n{traceback.format_exc()}")
    
    def _process_single_dxf(self, item):
        """Process a single DXF file (runs in background thread)"""
        try:
            import ezdxf
            from pyproj import Transformer
            import numpy as np
            
            # Load DXF if not cached
            if item.cached_dxf_doc:
                dxf_doc = item.cached_dxf_doc
            else:
                dxf_doc = ezdxf.readfile(str(item.dxf_path))
                item.cached_dxf_doc = dxf_doc
            
            modelspace = dxf_doc.modelspace()
            
            # Get display options
            color_override = None
            if getattr(item, "override_enabled", False):
                color_override = getattr(item, "override_color", (255, 0, 0))
            
            display_mode = getattr(item, "display_mode", "overlay")
            
            # Setup transformer
            transformer = None
            if item.dxf_crs and self.project_crs:
                try:
                    transformer = Transformer.from_crs(
                        item.dxf_crs,
                        self.project_crs,
                        always_xy=True
                    )
                except Exception as e:
                    print(f"  ⚠️ Transformer failed: {e}")
            
            # Process entities - ✅ FIXED: Pass dxf_doc
            processed_entities = []
            for entity in modelspace:
                entity_data = self._process_entity(entity, transformer, color_override, dxf_doc)
                if entity_data:
                    processed_entities.append(entity_data)
            
            return {
                'filename': item.dxf_path.name,
                'full_path': str(item.dxf_path.resolve()),
                'mode': display_mode,
                'entities': processed_entities,
                'dxf_crs': item.dxf_crs.to_wkt() if item.dxf_crs else None,
                'project_crs': self.project_crs.to_wkt() if self.project_crs else None,
                'transformed': transformer is not None
            }
            
        except Exception as e:
            print(f"⚠️ Failed to process {item.dxf_path.name}: {e}")
            return None
        
    
    def _get_entity_color(self, entity):
        """Extract color from DXF entity — prefer true_color, fall back to ACI."""
        try:
            tc = getattr(entity.dxf, 'true_color', None)
            if tc is not None and int(tc) != 0:
                return _true_color_to_rgb(int(tc))
        except Exception:
            pass
        try:
            color_index = entity.dxf.color
            if color_index == 256:
                return (255, 255, 255)
            return _ACI_RGB.get(color_index, (255, 255, 255))
        except Exception:
            return (255, 255, 255)
        
class DXFDisplayOptionsDialog(QDialog):
    """Per-file display options (overlay / underlay + color override)."""

    def __init__(self, parent=None, mode="overlay", override_enabled=False, override_color=(255, 0, 0)):
        super().__init__(parent)
        self.setWindowTitle("DXF Display Options")
        self.setModal(True)
        self.resize(260, 160)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # Mode
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Display Mode:"))
        self.overlay_radio = QRadioButton("Overlay (on top)")
        self.underlay_radio = QRadioButton("Underlay (below)")
        if mode == "underlay":
            self.underlay_radio.setChecked(True)
        else:
            self.overlay_radio.setChecked(True)
        mode_row.addWidget(self.overlay_radio)
        mode_row.addWidget(self.underlay_radio)
        layout.addLayout(mode_row)

        # Color override
        color_row = QHBoxLayout()
        self.color_override_check = QCheckBox("Override color:")
        self.color_combo = QComboBox()
        self.color_combo.addItem("Red",    QColor(255, 0, 0))
        self.color_combo.addItem("Green",  QColor(0, 255, 0))
        self.color_combo.addItem("Blue",   QColor(0, 0, 255))
        self.color_combo.addItem("Yellow", QColor(255, 255, 0))
        self.color_combo.addItem("Cyan",   QColor(0, 255, 255))
        self.color_combo.addItem("Magenta",QColor(255, 0, 255))
        self.color_combo.addItem("White",  QColor(255, 255, 255))

        self.color_override_check.setChecked(override_enabled)
        self.color_combo.setEnabled(override_enabled)
        self.color_override_check.toggled.connect(self.color_combo.setEnabled)

        # Select initial color
        for i in range(self.color_combo.count()):
            q = self.color_combo.itemData(i)
            if (q.red(), q.green(), q.blue()) == override_color:
                self.color_combo.setCurrentIndex(i)
                break

        color_row.addWidget(self.color_override_check)
        color_row.addWidget(self.color_combo)
        layout.addLayout(color_row)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton("OK")
        cancel_btn = QPushButton("Cancel")
        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

class DXFFileItem(QWidget):
    """Widget representing a single DXF file with remove button"""

    remove_requested = Signal(object)

    def _find_attachment(self):
        parent_dlg = self._find_parent_dialog()
        if parent_dlg is None:
            return None
        fname = self.dxf_path.name
        for attachment in parent_dlg.app.dxf_attachments:
            if attachment.get("filename") == fname:
                return attachment
        return None

    def __init__(self, dxf_path, prj_exists, parent=None):
        super().__init__(parent)
        self.dxf_path = Path(dxf_path)
        self.prj_exists = prj_exists
        self.dxf_crs = None
        self.entity_count = 0
        
        self.cached_dxf_doc = None
        self.cached_entities = None
        self.actor_cache = {}
        self.entity_layers = {}
        
        self.display_mode = "overlay"
        self.override_enabled = False
        self.override_color = (255, 0, 0)
        self.selected_layers = None  # None = all layers, or set of layer names
        self.layer_stats_cache = None
        self._layer_selection_dlg = None
        self._highlight_layer_name = None
        self._highlight_actor_states = {}
        self._isolate_layer_name = None
        self._isolate_actor_states = {}

        self._link_to_app_state()
        self.init_ui()
    
    def _link_to_app_state(self):
        """Link this UI item to the persistent actor data in NakshaApp."""
        try:
            parent_dlg = self._find_parent_dialog()
            if not parent_dlg or not hasattr(parent_dlg.app, 'dxf_attachments'):
                return
            
            fname = self.dxf_path.name
            for attachment in parent_dlg.app.dxf_attachments:
                if attachment.get("filename") == fname:
                    self.actor_cache = attachment.setdefault("actor_cache_map", {})
                    self.selected_layers = attachment.get("selected_layers")
                    self.layer_stats_cache = attachment.get("layer_stats")
                    self.display_mode = attachment.get("mode", "overlay")
                    self.override_enabled = attachment.get("override_enabled", False)
                    self.override_color = attachment.get("override_color", (255, 0, 0))
                    self.cached_entities = attachment.get("entities")
                    attachment["_dxf_item"] = self
                    break
        except Exception as e:
            print(f"⚠️ Error linking DXF item to app state: {e}")

    def init_ui(self):
        self.setObjectName("dxfFileItemRow")
        self.setFixedHeight(44)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignVCenter)

        # 1. Classic checkbox (un-styled) + filename label
        self.checkbox = QCheckBox()
        self.checkbox.setChecked(True)
        self.checkbox.setCursor(Qt.PointingHandCursor)
        self.checkbox.stateChanged.connect(self.on_checkbox_changed)
        layout.addWidget(self.checkbox)

        self.name_label = QLabel(self.dxf_path.name)
        self.name_label.setObjectName("dxfNameLabel")
        layout.addWidget(self.name_label, 1)

        # 2. DXF Badge
        self.dxf_badge = QLabel("DXF")
        self.dxf_badge.setObjectName("dxfBadge")
        self.dxf_badge.setFixedSize(48, 30)
        self.dxf_badge.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.dxf_badge)

        # 3. Entity Count badge
        self.count_label = QLabel("...")
        self.count_label.setObjectName("dxfCountBadge")
        self.count_label.setFixedSize(110, 30)
        self.count_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.count_label)

        # 4. Layers button
        self.layers_btn = QPushButton("Layers")
        self.layers_btn.setObjectName("dxfRowBtn")
        self.layers_btn.setFixedSize(76, 30)
        self.layers_btn.setCursor(Qt.PointingHandCursor)
        self.layers_btn.setToolTip("Select layers/levels")
        self.layers_btn.clicked.connect(self.open_layer_selection)
        layout.addWidget(self.layers_btn)

        # 5. Display options button
        self.options_btn = QPushButton("Settings")
        self.options_btn.setObjectName("dxfRowBtn")
        self.options_btn.setFixedSize(84, 30)
        self.options_btn.setCursor(Qt.PointingHandCursor)
        self.options_btn.setToolTip("Display options")
        self.options_btn.clicked.connect(self.open_display_options)
        layout.addWidget(self.options_btn)

        # 6. Red remove button
        self.rm_btn = QPushButton("X")
        self.rm_btn.setObjectName("dxfRemoveBtn")
        self.rm_btn.setFixedSize(30, 30)
        self.rm_btn.setCursor(Qt.PointingHandCursor)
        self.rm_btn.setToolTip("Remove from list")
        self.rm_btn.clicked.connect(lambda: self.remove_requested.emit(self))
        layout.addWidget(self.rm_btn)

        self.refresh_theme()

    def refresh_theme(self):
        """Re-apply styles based on current theme."""
        c = ThemeColors
        accent = c.get('accent')

        self.setStyleSheet(f"""
            QWidget#dxfFileItemRow {{
                background: {c.get('bg_secondary')};
                border: 1px solid {c.get('border_light')};
                border-radius: 8px;
            }}
            QWidget#dxfFileItemRow:hover {{
                background: {c.get('bg_input')};
                border-color: {c.get('dialog_primary_border')};
            }}
            QCheckBox {{
                background: transparent;
            }}
            QCheckBox::indicator {{
                width: 16px;
                height: 16px;
                border-radius: 3px;
                border: 2px solid {c.get('border_light')};
                background-color: {c.get('bg_input')};
            }}
            QCheckBox::indicator:checked {{
                background-color: {accent};
                border-color: {accent};
                image: url(data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSJ3aGl0ZSIgc3Ryb2tlLXdpZHRoPSI0IiBzdHJva2UtbGluZWNhcD0icm91bmQiIHN0cm9rZS1saW5lam9pbj0icm91bmQiPjxwb2x5bGluZSBwb2ludHM9IjIwIDYgOSAxNyA0IDEyIi8+PC9zdmc+);
            }}
            QCheckBox::indicator:hover {{
                border-color: {accent};
            }}
            QLabel#dxfNameLabel {{
                color: {c.get('text_primary')};
                font-weight: 700;
                font-size: 12px;
                background: transparent;
                border: none;
            }}
            QLabel#dxfBadge {{
                background: {c.get('success')};
                color: white;
                border-radius: 6px;
                font-size: 12px;
                font-weight: 700;
            }}
            QLabel#dxfPrjBadgeActive {{
                background: {c.get('success')}22;
                color: {c.get('success')};
                border: 1px solid {c.get('success')};
                border-radius: 6px;
                font-size: 11px;
                font-weight: 700;
            }}
            QLabel#dxfPrjBadgeInactive {{
                background: {c.get('danger')}22;
                color: {c.get('danger')};
                border: 1px solid {c.get('danger')};
                border-radius: 6px;
                font-size: 11px;
                font-weight: 700;
            }}
            QLabel#dxfCountBadge {{
                background: {c.get('bg_input')};
                color: {c.get('text_primary')};
                border: 1px solid {c.get('border_light')};
                border-radius: 6px;
                font-size: 12px;
                font-weight: 700;
            }}
            QPushButton#dxfRowBtn {{
                background: {c.get('bg_input')};
                color: {c.get('text_primary')};
                border: 1px solid {c.get('border_light')};
                border-radius: 6px;
                font-size: 12px;
                font-weight: 700;
                padding: 0px;
            }}
            QPushButton#dxfRowBtn:hover {{
                border-color: {accent};
                color: {accent};
            }}
            QPushButton#dxfRemoveBtn {{
                background: {c.get('danger')};
                color: white;
                border: none;
                border-radius: 6px;
                font-size: 12px;
                font-weight: 700;
                padding: 0px;
            }}
            QPushButton#dxfRemoveBtn:hover {{
                background: {c.get('danger_hover')};
            }}
        """)

    def cache_dxf_data(self, dxf_doc, processed_entities):
        """Cache the parsed DXF data for fast re-use"""
        self.cached_dxf_doc = dxf_doc
        self.cached_entities = processed_entities        
    
    def update_entity_count(self, count):
        """Update the entity count display"""
        self.entity_count = count
        self.count_label.setText(f"{count:,} Entities")

    def is_checked(self) -> bool:
        """Return whether this DXF file is selected for attachment."""
        return self.checkbox.isChecked()
    
    def on_checkbox_changed(self, state):
        """Handle checkbox state changes - show/hide this DXF's actors"""
        try:
            self.clear_layer_focus(render=False)
            is_visible = (state == Qt.CheckState.Checked.value) or (state == 2)
            
            parent_dialog = self._find_parent_dialog()
            if not parent_dialog:
                return
            labels_hidden = getattr(parent_dialog, '_labels_hidden', False)
            
            if self.actor_cache:
                for layer_name, actors in self.actor_cache.items():
                    layer_ok = (self.selected_layers is None or layer_name in self.selected_layers)
                    vis = is_visible and layer_ok
                    for actor in actors:
                        if labels_hidden and _is_label_actor(actor):
                            actor.SetVisibility(0)
                        else:
                            actor.SetVisibility(1 if vis else 0)
            else:
                target_name = os.path.basename(str(self.dxf_path))
                for dxf_data in getattr(parent_dialog.app, 'dxf_actors', []):
                    stored_name = os.path.basename(str(dxf_data.get('filename', '')))
                    if stored_name == target_name:
                        for actor in dxf_data.get('actors', []):
                            if labels_hidden and _is_label_actor(actor):
                                actor.SetVisibility(0)
                            else:
                                actor.SetVisibility(1 if is_visible else 0)
            
            self._force_render(parent_dialog)
            
        except Exception as e:
            print(f"  ⚠️ Checkbox toggle failed: {e}")
            import traceback
            traceback.print_exc()

    def set_crs(self, crs):
        """Store the parsed CRS"""
        self.dxf_crs = crs

    def open_display_options(self):
        """Open per-file display options dialog and store result."""
        dlg = DXFDisplayOptionsDialog(
            self,
            mode=self.display_mode,
            override_enabled=self.override_enabled,
            override_color=self.override_color,
        )
        if dlg.exec() == QDialog.Accepted:
            old_mode = self.display_mode
            
            mode, ov_enabled, ov_color = dlg.get_values()
            self.display_mode = mode
            self.override_enabled = ov_enabled
            self.override_color = ov_color
            
            print(f"\n⚡ INSTANT: Updating display options")
            
            for layer_name, actors in self.actor_cache.items():
                for actor in actors:
                    if ov_enabled:
                        actor.GetProperty().SetColor([c/255.0 for c in ov_color])
                    elif hasattr(actor, '_original_color'):
                        actor.GetProperty().SetColor([c/255.0 for c in actor._original_color])
                    
                    if old_mode != mode:
                        opacity = 0.5 if mode == 'underlay' else 1.0
                        actor.GetProperty().SetOpacity(opacity)
            
            parent_dialog = self._find_parent_dialog()
            if parent_dialog and hasattr(parent_dialog.app, 'vtk_widget'):
                render_window = parent_dialog.app.vtk_widget.GetRenderWindow()
                if render_window:
                    render_window.Render()
            
            print(f"  ✅ Done instantly!")

    def open_layer_selection(self):
        """Open layer/level selection dialog"""
        try:
            if self._layer_selection_dlg is not None:
                try:
                    if self._layer_selection_dlg.isVisible() or self._layer_selection_dlg.isMinimized():
                        if self._layer_selection_dlg.isMinimized():
                            self._layer_selection_dlg.showNormal()
                        self._layer_selection_dlg.raise_()
                        self._layer_selection_dlg.activateWindow()
                        return
                except (RuntimeError, AttributeError):
                    self._layer_selection_dlg = None

            _app_ref = None
            try:
                _pdlg = self._find_parent_dialog()
                _app_ref = getattr(_pdlg, 'app', None)
            except Exception:
                _app_ref = None
           
            dlg = DXFLayerSelectionDialog(self.dxf_path, self, app=_app_ref)
            dlg.setModal(False)
            dlg.setWindowModality(Qt.NonModal)
            dlg.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            self._layer_selection_dlg = dlg

            def _cleanup_dialog(*_args):
                self.clear_layer_focus(render=False)
                if self._layer_selection_dlg is dlg:
                    self._layer_selection_dlg = None

            def _on_accept():
                selected = dlg.get_selected_layers()
                total_layers = dlg.table.rowCount()
                self._apply_layer_selection(selected, total_layers)

            dlg.accepted.connect(_on_accept)
            dlg.finished.connect(_cleanup_dialog)
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
        except Exception as exc:
            print(f"[error] Layer selection failed: {exc}")
            import traceback
            traceback.print_exc()

    def _apply_layer_selection(self, selected: Set[str], total_layers: int):
        self.clear_layer_focus(render=False)
        parent_dlg = self._find_parent_dialog()
        labels_hidden = getattr(parent_dlg, '_labels_hidden', False) if parent_dlg else False

        if len(selected) == 0:
            self.selected_layers = set()
            self.count_label.setText(f"{self.entity_count} entities (0 layers)")
            self.count_label.setStyleSheet(f"color:{ThemeColors.get('danger')}; font-size:7px; font-weight:bold;")
        elif len(selected) == total_layers:
            self.selected_layers = None
            self.count_label.setText(f"{self.entity_count} entities")
            self.count_label.setStyleSheet(f"color:{ThemeColors.get('accent')}; font-size:7px; font-weight:bold;")
        else:
            self.selected_layers = selected
            self.count_label.setText(
                f"{self.entity_count} entities ({len(selected)} layers)")
            self.count_label.setStyleSheet(f"color:{ThemeColors.get('text_secondary')}; font-size:7px; font-weight:bold;")

        if parent_dlg:
            fname = self.dxf_path.name
            for attachment in parent_dlg.app.dxf_attachments:
                if attachment.get("filename") == fname:
                    attachment["selected_layers"] = self.selected_layers
                    break

        if self.actor_cache and self.checkbox.isChecked():
            for layer_name, actors in self.actor_cache.items():
                vis = (
                    self.selected_layers is None
                    or layer_name in self.selected_layers
                )
                for actor in actors:
                    if labels_hidden and _is_label_actor(actor):
                        actor.SetVisibility(0)
                    else:
                        actor.SetVisibility(vis)

        if parent_dlg:
            self._force_render(parent_dlg)

    def _find_parent_dialog(self):
        p = self.parent()
        while p is not None and not isinstance(p, MultiDXFAttachmentDialog):
            p = p.parent()
        return p

    @staticmethod
    def _force_render(parent_dlg):
        try:
            rw = parent_dlg.app.vtk_widget.GetRenderWindow()
            if rw:
                rw.Render()
                return
        except Exception:
            pass
        try:
            parent_dlg.app.vtk_widget.render()
        except Exception:
            pass

    def _capture_actor_visual_state(self, actor):
        try:
            prop = actor.GetProperty() if hasattr(actor, "GetProperty") else None
            text_prop = actor.GetTextProperty() if hasattr(actor, "GetTextProperty") else None
            
            state = {
                "actor": actor,
                "visibility": int(actor.GetVisibility()) if hasattr(actor, "GetVisibility") else 1,
                "has_prop": prop is not None,
                "has_text_prop": text_prop is not None,
            }
            if prop is not None:
                state["color"] = tuple(prop.GetColor())
                state["opacity"] = float(prop.GetOpacity())
                state["line_width"] = float(prop.GetLineWidth())
                state["point_size"] = float(prop.GetPointSize())
            if text_prop is not None:
                state["text_color"] = tuple(text_prop.GetColor())
                state["text_opacity"] = float(text_prop.GetOpacity())
            return state
        except Exception:
            return None

    def _restore_actor_visual_state(self, state: Dict[str, object]) -> None:
        actor = state.get("actor")
        if actor is None:
            return
        try:
            if hasattr(actor, "SetVisibility"):
                actor.SetVisibility(int(state.get("visibility", 1)))
        except Exception:
            pass
            
        if bool(state.get("has_prop", False)):
            try:
                prop = actor.GetProperty()
                c = state.get("color")
                if c is not None:
                    prop.SetColor(float(c[0]), float(c[1]), float(c[2]))
                prop.SetOpacity(float(state.get("opacity", 1.0)))
                prop.SetLineWidth(float(state.get("line_width", 1.0)))
                prop.SetPointSize(float(state.get("point_size", 1.0)))
            except Exception:
                pass
                
        if bool(state.get("has_text_prop", False)):
            try:
                text_prop = actor.GetTextProperty()
                tc = state.get("text_color")
                if tc is not None:
                    text_prop.SetColor(float(tc[0]), float(tc[1]), float(tc[2]))
                text_prop.SetOpacity(float(state.get("text_opacity", 1.0)))
            except Exception:
                pass

    def clear_layer_focus(self, *, render: bool = True) -> None:
        had_focus = bool(self._highlight_actor_states or self._isolate_actor_states)
        self.clear_layer_highlight(render=False)
        self.clear_layer_isolation(render=False)
        if render and had_focus:
            parent_dlg = self._find_parent_dialog()
            if parent_dlg is not None:
                self._force_render(parent_dlg)

    def _get_dxf_layer_runtime_actors(self, layer_name: str):
        """Return saved DXF actors + live unsaved digitizer actors for a DXF level."""
        actors = []
        layer_key = str(layer_name or "").strip()
        if not layer_key:
            return actors

        cache = getattr(self, "actor_cache", None)
        if isinstance(cache, dict):
            for actor in list(cache.get(layer_key, []) or []):
                if actor is not None:
                    actors.append(actor)

        parent_dlg = self._find_parent_dialog()
        app = getattr(parent_dlg, "app", None) if parent_dlg is not None else None
        digitizer = getattr(app, "digitizer", None) if app is not None else None
        if digitizer is None:
            return actors

        target_file = os.path.basename(str(getattr(self, "dxf_path", Path("")).name or "")).lower()
        active_file = os.path.basename(str(getattr(app, "active_dxf_filename", "") or "")).lower()

        seen = {id(a) for a in actors if a is not None}
        for d in list(getattr(digitizer, "drawings", []) or []):
            if not isinstance(d, dict):
                continue

            dlayer = str(d.get("layer") or d.get("dxf_layer") or "").strip()
            if dlayer != layer_key:
                continue

            owner_file = os.path.basename(str(d.get("dxf_file", "") or "")).lower()
            if owner_file:
                if owner_file != target_file:
                    continue
            elif active_file and active_file != target_file:
                continue

            live_actors = []
            for key in ("actor", "text_actor", "preview_actor"):
                actor = d.get(key)
                if actor is not None:
                    live_actors.append(actor)

            for key in ("actors", "line_actors", "segment_actors", "marker_actors"):
                actor_list = d.get(key)
                if isinstance(actor_list, (list, tuple)):
                    live_actors.extend([a for a in actor_list if a is not None])

            for actor in live_actors:
                if actor is not None and id(actor) not in seen:
                    actors.append(actor)
                    seen.add(id(actor))

        return actors

    def _iter_all_dxf_runtime_actors(self):
        """Yield all saved DXF actors and all live unsaved DXF drawing actors for this file."""
        yielded = set()

        cache = getattr(self, "actor_cache", None)
        if isinstance(cache, dict):
            for actors in list(cache.values()):
                for actor in list(actors or []):
                    if actor is not None and id(actor) not in yielded:
                        yielded.add(id(actor))
                        yield actor

        parent_dlg = self._find_parent_dialog()
        app = getattr(parent_dlg, "app", None) if parent_dlg is not None else None
        digitizer = getattr(app, "digitizer", None) if app is not None else None
        if digitizer is None:
            return

        target_file = os.path.basename(str(getattr(self, "dxf_path", Path("")).name or "")).lower()
        active_file = os.path.basename(str(getattr(app, "active_dxf_filename", "") or "")).lower()

        for d in list(getattr(digitizer, "drawings", []) or []):
            if not isinstance(d, dict):
                continue

            owner_file = os.path.basename(str(d.get("dxf_file", "") or "")).lower()
            if owner_file:
                if owner_file != target_file:
                    continue
            elif active_file and active_file != target_file:
                continue

            for key in ("actor", "text_actor", "preview_actor"):
                actor = d.get(key)
                if actor is not None and id(actor) not in yielded:
                    yielded.add(id(actor))
                    yield actor

            for key in ("actors", "line_actors", "segment_actors", "marker_actors"):
                actor_list = d.get(key)
                if isinstance(actor_list, (list, tuple)):
                    for actor in actor_list:
                        if actor is not None and id(actor) not in yielded:
                            yielded.add(id(actor))
                            yield actor

    def highlight_layer(self, layer_name: str, *, layer_count: Optional[int] = None) -> None:
        layer_key = str(layer_name or "").strip()
        if not layer_key:
            return

        # Toggle behavior: if the same level is highlighted again, restore
        # original colours/widths and stop. This keeps all existing highlight
        # logic unchanged while giving a clean way to revert.
        if getattr(self, "_highlight_layer_name", None) == layer_key:
            self.clear_layer_highlight(render=True)
            return

        parent_dlg = self._find_parent_dialog()
        if parent_dlg is None:
            return

        actors = [a for a in self._get_dxf_layer_runtime_actors(layer_key) if a is not None]
        if not actors or (layer_count is not None and int(layer_count) <= 0):
            QMessageBox.information(
                self,
                "Layer Highlight",
                f"Layer '{layer_key}' has no drawable entities to highlight.",
            )
            return

        self.clear_layer_focus(render=False)
        labels_hidden = getattr(parent_dlg, "_labels_hidden", False)

        for actor in actors:
            state = self._capture_actor_visual_state(actor)
            if state is not None:
                self._highlight_actor_states[id(actor)] = state

            if labels_hidden and _is_label_actor(actor):
                continue

            try:
                if hasattr(actor, "SetVisibility"):
                    actor.SetVisibility(1)
            except Exception:
                pass

            try:
                prop = actor.GetProperty() if hasattr(actor, "GetProperty") else None
                text_prop = actor.GetTextProperty() if hasattr(actor, "GetTextProperty") else None
                if prop is not None:
                    prop.SetColor(1.0, 0.92, 0.18)
                    prop.SetOpacity(1.0)
                    try:
                        if prop.GetLineWidth() < 4.0:
                            prop.SetLineWidth(4.0)
                    except Exception:
                        pass
                    try:
                        if prop.GetPointSize() < 8.0:
                            prop.SetPointSize(8.0)
                    except Exception:
                        pass
                if text_prop is not None:
                    text_prop.SetColor(1.0, 0.92, 0.18)
                    text_prop.SetOpacity(1.0)
            except Exception:
                continue

        self._highlight_layer_name = layer_key
        self._force_render(parent_dlg)

    def isolate_layer(self, layer_name: str, *, layer_count: Optional[int] = None) -> None:
        layer_key = str(layer_name or "").strip()
        if not layer_key:
            return

        parent_dlg = self._find_parent_dialog()
        if parent_dlg is None:
            return

        target_actors = [a for a in self._get_dxf_layer_runtime_actors(layer_key) if a is not None]
        if not target_actors or (layer_count is not None and int(layer_count) <= 0):
            QMessageBox.information(
                self,
                "Layer Isolate",
                f"Layer '{layer_key}' has no drawable entities to isolate.",
            )
            return

        self.clear_layer_focus(render=False)
        labels_hidden = getattr(parent_dlg, "_labels_hidden", False)
        target_ids = {id(a) for a in target_actors}

        for actor in list(self._iter_all_dxf_runtime_actors()):
            state = self._capture_actor_visual_state(actor)
            if state is not None:
                self._isolate_actor_states[id(actor)] = state

            is_target_layer = id(actor) in target_ids

            if labels_hidden and _is_label_actor(actor):
                try:
                    actor.SetVisibility(0)
                except Exception:
                    pass
                continue

            try:
                if hasattr(actor, "SetVisibility"):
                    actor.SetVisibility(1)
            except Exception:
                pass

            try:
                prop = actor.GetProperty() if hasattr(actor, "GetProperty") else None
                text_prop = actor.GetTextProperty() if hasattr(actor, "GetTextProperty") else None
                if is_target_layer:
                    if prop is not None:
                        prop.SetColor(1.0, 0.92, 0.18)
                        prop.SetOpacity(1.0)
                        try:
                            if prop.GetLineWidth() < 4.0:
                                prop.SetLineWidth(4.0)
                        except Exception:
                            pass
                        try:
                            if prop.GetPointSize() < 8.0:
                                prop.SetPointSize(8.0)
                        except Exception:
                            pass
                    if text_prop is not None:
                        text_prop.SetColor(1.0, 0.92, 0.18)
                        text_prop.SetOpacity(1.0)
                else:
                    original_visible = int(state.get("visibility", 1)) if state else 1
                    if original_visible <= 0:
                        try:
                            actor.SetVisibility(0)
                        except Exception:
                            pass
                        continue
                    current_opacity = float(state.get("opacity", 1.0)) if state and state.get("has_prop") else 1.0
                    if text_prop is not None:
                        current_opacity = float(state.get("text_opacity", 1.0)) if state and state.get("has_text_prop") else current_opacity
                    dim_opacity = min(max(current_opacity * 0.18, 0.04), 0.20)
                    if prop is not None:
                        prop.SetOpacity(dim_opacity)
                    if text_prop is not None:
                        text_prop.SetOpacity(dim_opacity)
            except Exception:
                continue

        self._isolate_layer_name = layer_key
        self._force_render(parent_dlg)

    def clear_layer_highlight(self, *, render: bool = True) -> None:
        if not self._highlight_actor_states:
            self._highlight_layer_name = None
            return

        for state in list(self._highlight_actor_states.values()):
            try:
                self._restore_actor_visual_state(state)
            except Exception:
                continue

        self._highlight_actor_states.clear()
        self._highlight_layer_name = None

        if render:
            parent_dlg = self._find_parent_dialog()
            if parent_dlg is not None:
                self._force_render(parent_dlg)

    def clear_layer_isolation(self, *, render: bool = True) -> None:
        if not self._isolate_actor_states:
            self._isolate_layer_name = None
            return

        for state in list(self._isolate_actor_states.values()):
            try:
                self._restore_actor_visual_state(state)
            except Exception:
                continue

        self._isolate_actor_states.clear()
        self._isolate_layer_name = None

        if render:
            parent_dlg = self._find_parent_dialog()
            if parent_dlg is not None:
                self._force_render(parent_dlg)

    def delete_layer(
        self,
        layer_name: str,
        *,
        layer_count: Optional[int] = None,
        confirm: bool = True,
    ) -> bool:
        layer_key = str(layer_name or "").strip()
        if not layer_key:
            return False

        if confirm:
            cnt = max(0, int(layer_count or 0))
            reply = QMessageBox.warning(
                self,
                "Delete Layer",
                "Delete this layer from the current attached DXF session?\n\n"
                f"Layer: {layer_key}\n"
                f"Contains: {cnt:,} entities\n\n"
                "This action is not permanent in source file.\n"
                "Reattach the DXF file to restore it.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return False

        parent_dlg = self._find_parent_dialog()
        if parent_dlg is None:
            return False

        has_layer_in_cache = layer_key in (self.actor_cache or {})
        has_layer_in_stats = any((n == layer_key) for n, _c, _clr in (self.layer_stats_cache or []))
        has_layer_in_entities = False
        if self.cached_entities:
            try:
                has_layer_in_entities = any(str(e.get("layer", "0")) == layer_key for e in self.cached_entities)
            except Exception:
                has_layer_in_entities = False

        if not (has_layer_in_cache or has_layer_in_stats or has_layer_in_entities):
            QMessageBox.information(self, "Delete Layer", f"Layer '{layer_key}' was not found.")
            return False

        self.clear_layer_focus(render=False)

        removed_actor_ids: Set[int] = set()
        layer_actors = list((self.actor_cache or {}).pop(layer_key, []) or [])

        renderer = None
        try:
            renderer = parent_dlg.app.vtk_widget.renderer
        except Exception:
            renderer = None

        for actor in layer_actors:
            if actor is None:
                continue
            removed_actor_ids.add(id(actor))
            if renderer is not None:
                try:
                    renderer.RemoveActor(actor)
                except Exception:
                    pass

        target_name = os.path.basename(str(self.dxf_path.name or "")).lower()
        for store_name in ("snt_actors", "dxf_actors"):
            for snt_data in list(getattr(parent_dlg.app, store_name, []) or []):
                fname = os.path.basename(str(snt_data.get("filename", "") or "")).lower()
                if target_name and fname != target_name:
                    continue
                cur_actors = list(snt_data.get("actors", []) or [])
                kept: List = []
                for actor in cur_actors:
                    if actor is None:
                        continue
                    remove_this = False
                    if removed_actor_ids and id(actor) in removed_actor_ids:
                        remove_this = True
                    elif str(getattr(actor, "_naksha_snt_layer", "") or "").strip() == layer_key:
                        remove_this = True
                    if not remove_this:
                        kept.append(actor)
                snt_data["actors"] = kept

        if self.cached_entities is not None:
            self.cached_entities = [
                e for e in self.cached_entities if str(e.get("layer", "0")) != layer_key
            ]

        # Update dxf_attachments entities list
        for attachment in parent_dlg.app.dxf_attachments:
            if attachment.get("filename") == self.dxf_path.name:
                attachment["entities"] = self.cached_entities
                break

        # Also remove the on-screen digitizer drawings that belong to this layer
        try:
            digitizer = getattr(parent_dlg.app, "digitizer", None)
            if digitizer is not None:
                target_file = os.path.basename(str(self.dxf_path.name or "")).lower()
                active_file = os.path.basename(str(getattr(parent_dlg.app, "active_dxf_filename", "") or "")).lower()
                doomed = []
                for d in list(getattr(digitizer, "drawings", []) or []):
                    if not isinstance(d, dict):
                        continue
                    if str(d.get("layer", "")) != layer_key:
                        continue
                    owner_file = os.path.basename(str(d.get("snt_file", "") or "")).lower()
                    if owner_file:
                        if owner_file != target_file:
                            continue
                    elif active_file != target_file:
                        continue
                    doomed.append(d)
                for d in doomed:
                    try:
                        digitizer._remove_drawing(d)
                    except Exception:
                        pass
                try:
                    if hasattr(digitizer, "undo_stack"):
                        digitizer.undo_stack.clear()
                    if hasattr(digitizer, "redo_stack"):
                        digitizer.redo_stack.clear()
                except Exception:
                    pass
        except Exception:
            pass

        # Re-run rebuild without changing the current main-window zoom/pan.
        app = getattr(parent_dlg, "app", None)
        renderer = getattr(getattr(app, "vtk_widget", None), "renderer", None) if app is not None else None
        cam_state = _dxf_capture_camera_state(renderer)
        try:
            if renderer is not None:
                try:
                    setattr(renderer, "_skip_camera_reset", True)
                except Exception:
                    pass
            rebuild_dxf_attachment(app, self._find_attachment())
        except Exception as exc:
            print(f"Delete: rebuild failed: {exc}")
        finally:
            try:
                _dxf_restore_camera_state_delayed(app, renderer, cam_state)
            except Exception:
                pass
            if renderer is not None:
                try:
                    setattr(renderer, "_skip_camera_reset", False)
                except Exception:
                    pass

        return True


def _is_label_actor(actor) -> bool:
    """
    Return True when a VTK actor is a text/label actor created by create_text_actor.
    This is actor-level detection, so it works even on mixed geometry+label layers.
    """
    return bool(
        getattr(actor, 'is_grid_label', False)
        or getattr(actor, 'text_content', None) is not None
    )


_DEFAULT_COLOR = (0, 255, 200)


def _dxf_capture_camera_state(renderer):
    """Capture current VTK camera state so DXF updates do not zoom-fit the main view."""
    if renderer is None:
        return None
    try:
        cam = renderer.GetActiveCamera()
        if cam is None:
            return None
        return {
            "position": tuple(cam.GetPosition()),
            "focal_point": tuple(cam.GetFocalPoint()),
            "view_up": tuple(cam.GetViewUp()),
            "parallel_scale": float(cam.GetParallelScale()),
            "clipping_range": tuple(cam.GetClippingRange()),
        }
    except Exception:
        return None


def _dxf_restore_camera_state(app, renderer, cam_state):
    """Restore camera after a DXF refresh/rebuild without changing zoom/pan."""
    if renderer is None or cam_state is None:
        return
    try:
        cam = renderer.GetActiveCamera()
        if cam is None:
            return
        cam.SetPosition(*cam_state["position"])
        cam.SetFocalPoint(*cam_state["focal_point"])
        cam.SetViewUp(*cam_state["view_up"])
        cam.SetParallelScale(cam_state["parallel_scale"])
        cam.SetClippingRange(*cam_state["clipping_range"])
        try:
            renderer.ResetCameraClippingRange()
        except Exception:
            pass
        vtk_widget = getattr(app, "vtk_widget", None) if app is not None else None
        if vtk_widget is not None:
            try:
                vtk_widget.render()
            except Exception:
                try:
                    rw = vtk_widget.GetRenderWindow()
                    if rw is not None:
                        rw.Render()
                except Exception:
                    pass
    except Exception:
        pass


def _dxf_restore_camera_state_delayed(app, renderer, cam_state):
    """Restore now and again after delayed Qt/VTK renders."""
    _dxf_restore_camera_state(app, renderer, cam_state)
    try:
        QTimer.singleShot(0, lambda: _dxf_restore_camera_state(app, renderer, cam_state))
        QTimer.singleShot(50, lambda: _dxf_restore_camera_state(app, renderer, cam_state))
        QTimer.singleShot(150, lambda: _dxf_restore_camera_state(app, renderer, cam_state))
        QTimer.singleShot(300, lambda: _dxf_restore_camera_state(app, renderer, cam_state))
    except Exception:
        pass


def _extract_dxf_drawing_color(drawing: dict) -> Tuple[int, int, int]:
    color = (
        drawing.get("color")
        or drawing.get("original_color")
        or drawing.get("original_text_color")
        or _DEFAULT_COLOR
    )
    if not isinstance(color, (list, tuple)) or len(color) < 3:
        return _DEFAULT_COLOR
    try:
        values = [float(color[0]), float(color[1]), float(color[2])]
    except (TypeError, ValueError):
        return _DEFAULT_COLOR
    if max(abs(v) for v in values) <= 1.0:
        values = [v * 255.0 for v in values]
    return tuple(max(0, min(255, int(round(v)))) for v in values)

def _build_dxf_layer_stats_from_entities(entities, fallback_layers=None):
    stats = {}
    order = []

    for layer in list(fallback_layers or []):
        if not isinstance(layer, dict):
            continue
        lname = str(layer.get("name", "0") or "0")
        color = layer.get("color", _DEFAULT_COLOR)
        if not isinstance(color, (list, tuple)) or len(color) < 3:
            color = _DEFAULT_COLOR
        if lname not in stats:
            stats[lname] = {"count": 0, "color": tuple(color[:3])}
            order.append(lname)

    for ent in list(entities or []):
        if not isinstance(ent, dict):
            continue
        lname = str(ent.get("layer", "0") or "0")
        color = ent.get("color", _DEFAULT_COLOR)
        if not isinstance(color, (list, tuple)) or len(color) < 3:
            color = _DEFAULT_COLOR
        if lname not in stats:
            stats[lname] = {"count": 0, "color": tuple(color[:3])}
            order.append(lname)
        stats[lname]["count"] = int(stats[lname]["count"]) + 1

    return [(lname, int(stats[lname]["count"]), tuple(stats[lname]["color"])) for lname in order]



def _ensure_dxf_parsed(attachment: dict, item=None) -> dict:
    """Ensure DXF uses the same runtime model as SNT: parsed/layers/entities."""
    if not isinstance(attachment, dict):
        return {"layers": [], "entities": []}

    parsed = attachment.get("parsed")
    if not isinstance(parsed, dict):
        parsed = {"layers": [], "entities": []}
        attachment["parsed"] = parsed

    entities = parsed.get("entities")
    if not isinstance(entities, list):
        src_entities = attachment.get("entities")
        if not isinstance(src_entities, list) and item is not None:
            src_entities = getattr(item, "cached_entities", None)
        entities = src_entities if isinstance(src_entities, list) else []
        parsed["entities"] = entities

    attachment["entities"] = entities

    layers = parsed.get("layers")
    if not isinstance(layers, list):
        layers = []
        parsed["layers"] = layers

    known = {str(l.get("name", "")) for l in layers if isinstance(l, dict)}

    layer_stats = attachment.get("layer_stats")
    if not layer_stats and item is not None:
        layer_stats = getattr(item, "layer_stats_cache", None)
    for row in list(layer_stats or []):
        try:
            lname, _count, clr = row
        except Exception:
            continue
        lname = str(lname or "0")
        if lname and lname not in known:
            layers.append({"name": lname, "color": tuple(clr) if isinstance(clr, (list, tuple)) else _DEFAULT_COLOR})
            known.add(lname)

    for ent in list(entities or []):
        if not isinstance(ent, dict):
            continue
        lname = str(ent.get("layer", "0") or "0")
        if lname not in known:
            clr = ent.get("color", _DEFAULT_COLOR)
            layers.append({"name": lname, "color": tuple(clr) if isinstance(clr, (list, tuple)) else _DEFAULT_COLOR})
            known.add(lname)

    # Keep layer_stats always in sync, including empty user-created layers.
    attachment["layer_stats"] = _build_dxf_layer_stats_from_entities(entities, layers)

    return parsed


def _dxf_layer_color(parsed: dict, layer_name: str, fallback=_DEFAULT_COLOR) -> Tuple[int, int, int]:
    if isinstance(parsed, dict):
        for layer in list(parsed.get("layers", []) or []):
            if not isinstance(layer, dict):
                continue
            if str(layer.get("name", "")) == str(layer_name):
                c = layer.get("color")
                if isinstance(c, (list, tuple)) and len(c) >= 3:
                    try:
                        return (int(c[0]), int(c[1]), int(c[2]))
                    except Exception:
                        return fallback
    return fallback

def _dxf_layer_line_style(parsed: dict, layer_name: str, fallback="Solid") -> str:
    """Return saved DXF layer line style."""
    try:
        for layer in list((parsed or {}).get("layers", []) or []):
            if not isinstance(layer, dict):
                continue
            if str(layer.get("name", "")).strip() == str(layer_name or "").strip():
                style = str(layer.get("line_style", fallback) or fallback)
                return style if style in ("Solid", "Dashed", "Dotted", "Dash-Dot") else fallback
    except Exception:
        pass
    return fallback


def _dxf_layer_line_width(parsed: dict, layer_name: str, fallback=2.0) -> float:
    """Return saved DXF layer line width."""
    try:
        for layer in list((parsed or {}).get("layers", []) or []):
            if not isinstance(layer, dict):
                continue
            if str(layer.get("name", "")).strip() == str(layer_name or "").strip():
                return max(1.0, min(float(layer.get("line_width", fallback) or fallback), 20.0))
    except Exception:
        pass
    return float(fallback)


def _normalize_dxf_line_style(style) -> str:
    style = str(style or "Solid")
    return style if style in ("Solid", "Dashed", "Dotted", "Dash-Dot") else "Solid"


def _apply_dxf_actor_line_style(actor, line_width=2.0, line_style="Solid"):
    """Apply DXF line width/style metadata to a VTK actor without raising."""
    if actor is None:
        return
    try:
        width = max(1.0, min(float(line_width or 2.0), 20.0))
    except Exception:
        width = 2.0
    style = _normalize_dxf_line_style(line_style)
    try:
        prop = actor.GetProperty() if hasattr(actor, "GetProperty") else None
        if prop is not None:
            prop.SetLineWidth(width)
            # Best effort: some VTK/OpenGL builds ignore line stipple, but the metadata is kept.
            if hasattr(prop, "SetLineStipplePattern") and hasattr(prop, "SetLineStippleRepeatFactor"):
                if style == "Dashed":
                    prop.SetLineStipplePattern(0xF0F0)
                    prop.SetLineStippleRepeatFactor(1)
                elif style == "Dotted":
                    prop.SetLineStipplePattern(0xAAAA)
                    prop.SetLineStippleRepeatFactor(1)
                elif style == "Dash-Dot":
                    prop.SetLineStipplePattern(0xE4E4)
                    prop.SetLineStippleRepeatFactor(1)
                else:
                    prop.SetLineStipplePattern(0xFFFF)
                    prop.SetLineStippleRepeatFactor(1)
    except Exception:
        pass
    try:
        actor._dxf_line_width = width
        actor._dxf_line_style = style
    except Exception:
        pass


def _build_dxf_entity_from_drawing(drawing_entry: dict, active_layer: str, layer_color=None):
    coords = drawing_entry.get("coords") or drawing_entry.get("coordinates") or []
    if not coords:
        return None

    clean_coords = []
    for pt in coords:
        try:
            x = float(pt[0])
            y = float(pt[1])
            z = float(pt[2]) if len(pt) > 2 else 0.0
            clean_coords.append((x, y, z))
        except Exception:
            continue

    if not clean_coords:
        return None

    if layer_color is None:
        layer_color = _extract_dxf_drawing_color(drawing_entry)

    shape_type = str(drawing_entry.get("type", "polyline")).lower()
    if shape_type == "text":
        height = drawing_entry.get("height")
        if height is None:
            height = float(drawing_entry.get("font_size", 18.0) or 18.0) * 0.15
        ent = {
            "type": "TEXT",
            "layer": active_layer,
            "color": layer_color,
            "position": clean_coords[0],
            "text": drawing_entry.get("text", "Text"),
            "height": height,
            "rotation": 0.0,
            "font_size": drawing_entry.get("font_size", 18.0),
        }
        ent["line_width"] = max(1.0, min(float(drawing_entry.get("line_width", 2.0) or 2.0), 20.0))
        ent["line_style"] = _normalize_dxf_line_style(drawing_entry.get("line_style", "Solid"))
        return ent

    ent = {
        "type": "POLYLINE",
        "layer": active_layer,
        "color": layer_color,
        "vertices": clean_coords,
        "points": clean_coords,
        "closed": shape_type in ("polygon", "rectangle", "circle", "polyline", "freehand"),
    }
    ent["line_width"] = max(1.0, min(float(drawing_entry.get("line_width", 2.0) or 2.0), 20.0))
    ent["line_style"] = _normalize_dxf_line_style(drawing_entry.get("line_style", "Solid"))
    return ent


def _same_dxf_geometry(ent: dict, drawing_entry: dict, tol: float = 0.01) -> bool:
    if not isinstance(ent, dict) or not isinstance(drawing_entry, dict):
        return False
    coords = drawing_entry.get("coords") or drawing_entry.get("coordinates") or []
    if not coords:
        return False
    etype = str(ent.get("type", "")).upper()
    if etype == "LINE" and "start" in ent and "end" in ent:
        pts = [ent.get("start"), ent.get("end")]
    elif etype == "TEXT":
        pts = [ent.get("position")] if ent.get("position") is not None else []
    else:
        pts = ent.get("vertices") or ent.get("points") or []
    if len(pts) != len(coords):
        return False
    for a, b in zip(pts, coords):
        try:
            if abs(float(a[0]) - float(b[0])) > tol or abs(float(a[1]) - float(b[1])) > tol:
                return False
        except Exception:
            return False
    return True


def save_dxf_attachment_to_disk(attachment):
    """Write the parsed layers/entities back to the DXF file on disk.

    Mirrors save_snt_attachment_to_disk() but emits a .dxf directly (no SNT
    conversion). Returns True on success, False otherwise.
    """
    import tempfile
    import shutil
    from pathlib import Path
    import ezdxf
    from gui.vector_export import _emit_entity_to_dxf, _normalize_rgb_triplet
    from gui.snt_attachment import _rgb_to_aci

    filepath = attachment.get("full_path")
    if not filepath:
        return False

    parsed = attachment.get("parsed")
    if not parsed:
        return False

    temp_root = Path(tempfile.mkdtemp(prefix="dxf_save_"))
    temp_dxf = temp_root / "temp.dxf"

    try:
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()

        for layer in parsed.get("layers", []):
            layer_name = layer.get("name", "0")
            color_rgb = layer.get("color", (255, 255, 255))
            if not doc.layers.has_entry(layer_name):
                dxf_layer = doc.layers.new(layer_name)
                r, g, b = _normalize_rgb_triplet(color_rgb)
                dxf_layer.color = _rgb_to_aci((r, g, b))
                dxf_layer.rgb = (r, g, b)

        emitted = 0
        for ent in parsed.get("entities", []):
            etype = str(ent.get("type") or ent.get("entity_type") or "").upper()
            dxf_ent = {
                "entity_type": etype,
                "layer": ent.get("layer", "0"),
                "color": ent.get("color", (255, 255, 255)),
            }

            if etype == "POLYLINE":
                dxf_ent["points"] = ent.get("vertices") or ent.get("points") or []
                dxf_ent["closed"] = bool(ent.get("closed", False))
            elif etype == "POINT":
                dxf_ent["position"] = ent.get("position", (0.0, 0.0, 0.0))
            elif etype == "TEXT":
                dxf_ent["position"] = ent.get("position", (0.0, 0.0, 0.0))
                dxf_ent["text"] = ent.get("text", "")
                dxf_ent["height"] = ent.get("height", 2.5)
                dxf_ent["rotation"] = ent.get("rotation", 0.0)
            elif etype == "3DFACE":
                dxf_ent["vertices"] = ent.get("vertices", [])
            else:
                continue

            if _emit_entity_to_dxf(msp, doc, dxf_ent):
                emitted += 1

        if emitted == 0:
            msp.add_point((0, 0, 0))

        doc.saveas(temp_dxf)

        target = Path(filepath)
        temp_target = target.with_suffix(target.suffix + ".tmp_save")
        if temp_target.exists():
            temp_target.unlink()

        shutil.copy2(temp_dxf, temp_target)
        if target.exists():
            target.unlink()
        temp_target.replace(target)

        print(f"✅ DXF file saved to disk: {target}")
        return True
    except Exception as exc:
        print(f"❌ Failed to save DXF to disk: {exc}")
        return False
    finally:
        try:
            shutil.rmtree(temp_root, ignore_errors=True)
        except Exception:
            pass


def rebuild_dxf_attachment(app, attachment):
    if not isinstance(attachment, dict):
        return

    item = attachment.get("_dxf_item")
    if not item:
        return

    try:
        item.attachment = attachment
    except Exception:
        pass

    # Normalize actor cache first. Rebuild must never call .items() on a list.
    actor_cache = _normalize_dxf_actor_cache(item, attachment)

    renderer = getattr(app.vtk_widget, "renderer", None)
    if renderer:
        for _lname, actors in list(actor_cache.items()):
            for actor in list(actors or []):
                try:
                    renderer.RemoveActor(actor)
                except Exception:
                    pass

    actor_cache.clear()
    attachment["actor_cache_map"] = actor_cache

    parsed = _ensure_dxf_parsed(attachment, item)
    raw_entities = [ent for ent in list(parsed.get("entities", []) or []) if isinstance(ent, dict)]
    parsed["entities"] = raw_entities
    attachment["entities"] = raw_entities

    entity_count = len(raw_entities)
    item.update_entity_count(entity_count)
    attachment["entity_count"] = entity_count

    layers_meta = [layer for layer in list(parsed.get("layers", []) or []) if isinstance(layer, dict)]
    stats_map = {}
    for layer in layers_meta:
        lname = str(layer.get("name", "0") or "0")
        lcolor = layer.get("color", _DEFAULT_COLOR)
        stats_map[lname] = {"count": 0, "color": tuple(lcolor) if isinstance(lcolor, (list, tuple)) else _DEFAULT_COLOR}

    for ent in raw_entities:
        lname = str(ent.get("layer", "0") or "0")
        if lname not in stats_map:
            stats_map[lname] = {"count": 0, "color": tuple(ent.get("color", _DEFAULT_COLOR)) if isinstance(ent.get("color"), (list, tuple)) else _DEFAULT_COLOR}
        stats_map[lname]["count"] = int(stats_map[lname]["count"]) + 1

    layer_stats = [
        (lname, int(meta.get("count", 0)), tuple(meta.get("color", _DEFAULT_COLOR)))
        for lname, meta in stats_map.items()
    ]
    item.layer_stats_cache = list(layer_stats)
    attachment["layer_stats"] = layer_stats

    dlg = item._find_parent_dialog()
    if not dlg:
        dlg = getattr(app, "dxf_dialog", None)
    if not dlg:
        from gui.dxf_attachment import MultiDXFAttachmentDialog
        dlg = MultiDXFAttachmentDialog(app)

    if dlg:
        dlg.render_dxf_in_vtk(attachment)
        is_checked = item.checkbox.isChecked()
        labels_hidden = getattr(dlg, "_labels_hidden", False)

        for layer_name, actors in _normalize_dxf_actor_cache(item, attachment).items():
            layer_ok = (item.selected_layers is None or layer_name in item.selected_layers)
            vis = is_checked and layer_ok
            for actor in actors:
                if labels_hidden and _is_label_actor(actor):
                    actor.SetVisibility(0)
                else:
                    actor.SetVisibility(1 if vis else 0)

        item._force_render(dlg)

def _normalize_dxf_actor_cache(item, attachment=None):
    """Return a valid per-layer actor cache dict and repair bad runtime states.

    Older DXF refresh/delete paths sometimes leave item.actor_cache or
    attachment["actor_cache_map"] as a list. Every layer operation expects a
    dict: {layer_name: [actors...]}. This helper centralizes the guard so
    rebuild/delete/view cannot crash on .items().
    """
    if item is None:
        return {}

    cache = getattr(item, "actor_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        try:
            item.actor_cache = cache
        except Exception:
            pass

    if isinstance(attachment, dict):
        att_cache = attachment.get("actor_cache_map")
        if not isinstance(att_cache, dict):
            attachment["actor_cache_map"] = cache
        elif att_cache is not cache:
            # Prefer the live item cache because render_dxf_in_vtk writes to it.
            try:
                item.actor_cache = att_cache
                cache = att_cache
            except Exception:
                attachment["actor_cache_map"] = cache
    return cache

def get_dxf_entity_bounds(ent: dict):
    points = []
    if "points" in ent:
        points = ent["points"]
    elif "vertices" in ent:
        points = ent["vertices"]
    elif "start" in ent and "end" in ent:
        points = [ent["start"], ent["end"]]
    elif "position" in ent:
        pos = ent["position"]
        etype = str(ent.get("type", "")).upper()
        if etype == "TEXT":
            try:
                h = float(ent.get("height", 2.5))
            except (TypeError, ValueError):
                h = 2.5
            text_str = str(ent.get("text", "")).strip()
            num_chars = max(3, len(text_str))
            total_width = num_chars * (h * 0.7)
            pad_x = max(2.0, (total_width / 2.0) + (h * 2.0))
            pad_y = max(2.0, h * 2.0)
        else:
            pad_x = pad_y = 2.0
        z = float(pos[2]) if len(pos) > 2 else 0.0
        points = [
            [float(pos[0]) - pad_x, float(pos[1]) - pad_y, z],
            [float(pos[0]) + pad_x, float(pos[1]) + pad_y, z]
        ]
    elif "center" in ent:
        center = ent["center"]
        radius = ent.get("radius", 1.0)
        points = [
            [center[0] - radius, center[1] - radius, center[2] if len(center) > 2 else 0.0],
            [center[0] + radius, center[1] + radius, center[2] if len(center) > 2 else 0.0]
        ]
        
    if not points:
        return None
    try:
        xs = [float(p[0]) for p in points]
        ys = [float(p[1]) for p in points]
        zs = [float(p[2]) if len(p) > 2 else 0.0 for p in points]
        return (min(xs), max(xs), min(ys), max(ys), min(zs), max(zs))
    except Exception:
        return None


def _make_dxf_entity_highlight_actor(ent):
    try:
        import vtk
    except Exception:
        return None

    HIGHLIGHT_COLOR = (0.0, 1.0, 1.0)
    HIGHLIGHT_WIDTH = 7.0

    def _style(actor, mapper):
        prop = actor.GetProperty()
        prop.SetColor(*HIGHLIGHT_COLOR)
        prop.SetLineWidth(HIGHLIGHT_WIDTH)
        prop.SetPointSize(10.0)
        prop.SetLighting(False)
        actor.PickableOff()
        try:
            mapper.SetResolveCoincidentTopologyToPolygonOffset()
            mapper.SetRelativeCoincidentTopologyLineOffsetParameters(-2.0, -4.0)
            mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(-2.0, -4.0)
        except Exception:
            pass

    def _z(pt):
        try:
            return float(pt[2]) if len(pt) > 2 else 0.0
        except Exception:
            return 0.0

    etype = str(ent.get("type", "")).upper()

    if etype == "LINE":
        pts = vtk.vtkPoints()
        pts.InsertNextPoint(float(ent['start'][0]), float(ent['start'][1]), _z(ent['start']))
        pts.InsertNextPoint(float(ent['end'][0]), float(ent['end'][1]), _z(ent['end']))
        line = vtk.vtkLine()
        line.GetPointIds().SetId(0, 0)
        line.GetPointIds().SetId(1, 1)
        cells = vtk.vtkCellArray()
        cells.InsertNextCell(line)
        pd = vtk.vtkPolyData()
        pd.SetPoints(pts)
        pd.SetLines(cells)
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(pd)
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        _style(actor, mapper)
        return actor

    elif etype == "POLYLINE":
        verts = list(ent.get("points") or ent.get("vertices") or [])
        if len(verts) < 2:
            return None
        if ent.get("closed") and len(verts) >= 3:
            v0 = verts[0]
            vN = verts[-1]
            same = (
                abs(float(v0[0]) - float(vN[0])) <= 1e-6
                and abs(float(v0[1]) - float(vN[1])) <= 1e-6
                and abs(_z(v0) - _z(vN)) <= 1e-6
            )
            if not same:
                verts.append(verts[0])
        pts = vtk.vtkPoints()
        for v in verts:
            pts.InsertNextPoint(float(v[0]), float(v[1]), _z(v))
        line = vtk.vtkPolyLine()
        line.GetPointIds().SetNumberOfIds(len(verts))
        for i in range(len(verts)):
            line.GetPointIds().SetId(i, i)
        cells = vtk.vtkCellArray()
        cells.InsertNextCell(line)
        pd = vtk.vtkPolyData()
        pd.SetPoints(pts)
        pd.SetLines(cells)
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(pd)
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        _style(actor, mapper)
        return actor

    elif etype == "3DFACE":
        verts = ent.get("vertices") or []
        if len(verts) < 3:
            return None
        pts = vtk.vtkPoints()
        for v in verts:
            pts.InsertNextPoint(float(v[0]), float(v[1]), _z(v))
        n = len(verts)
        line = vtk.vtkPolyLine()
        line.GetPointIds().SetNumberOfIds(n + 1)
        for i in range(n):
            line.GetPointIds().SetId(i, i)
        line.GetPointIds().SetId(n, 0)
        cells = vtk.vtkCellArray()
        cells.InsertNextCell(line)
        pd = vtk.vtkPolyData()
        pd.SetPoints(pts)
        pd.SetLines(cells)
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(pd)
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        _style(actor, mapper)
        return actor

    elif etype in ("CIRCLE", "ARC"):
        center = ent.get("center", (0, 0, 0))
        radius = ent.get("radius", 1.0)
        src = vtk.vtkRegularPolygonSource()
        src.SetNumberOfSides(64)
        src.SetRadius(radius)
        src.SetCenter(center[0], center[1], center[2] if len(center) > 2 else 0.0)
        src.SetGeneratePolygon(False)
        src.SetGeneratePolyline(True)
        src.Update()
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(src.GetOutput())
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        _style(actor, mapper)
        return actor

    bounds = get_dxf_entity_bounds(ent)
    if not bounds:
        return None
    xmin, xmax, ymin, ymax, zmin, zmax = bounds
    if abs(xmax - xmin) < 1e-6:
        xmin -= 2.0
        xmax += 2.0
    if abs(ymax - ymin) < 1e-6:
        ymin -= 2.0
        ymax += 2.0
    z = (zmin + zmax) / 2.0
    pts = vtk.vtkPoints()
    for c in [(xmin, ymin, z), (xmax, ymin, z), (xmax, ymax, z), (xmin, ymax, z)]:
        pts.InsertNextPoint(*c)
    cells = vtk.vtkCellArray()
    for a, b in [(0, 1), (1, 2), (2, 3), (3, 0)]:
        ln = vtk.vtkLine()
        ln.GetPointIds().SetId(0, a)
        ln.GetPointIds().SetId(1, b)
        cells.InsertNextCell(ln)
    pd = vtk.vtkPolyData()
    pd.SetPoints(pts)
    pd.SetLines(cells)
    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputData(pd)
    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    _style(actor, mapper)
    return actor


def _delete_matching_digitizer_drawing(digitizer, ent):
    if digitizer is None or not hasattr(digitizer, "drawings") or not digitizer.drawings:
        return
    if not isinstance(ent, dict):
        return

    e_layer = str(ent.get("layer", "") or "").strip()
    e_type = str(ent.get("type", "") or "").upper()
    
    # Extract coordinates from entity
    e_pts = []
    if e_type == "LINE" and "start" in ent and "end" in ent:
        e_pts = [ent.get("start"), ent.get("end")]
    elif e_type == "TEXT":
        e_pos = ent.get("position")
        if e_pos:
            e_pts = [e_pos]
    else:
        e_pts = ent.get("vertices") or ent.get("points") or []
        
    if not e_pts:
        return

    matching_drawing = None
    for d in list(digitizer.drawings):
        if not isinstance(d, dict):
            continue
        
        # Match layer name
        d_layer = str(d.get("layer", "") or "").strip()
        if d_layer != e_layer:
            continue
            
        # Match type
        d_type = str(d.get("type", "") or "").lower()
        if e_type == "TEXT":
            if d_type != "text":
                continue
        else:
            if d_type == "text":
                continue
                
        # Match coordinates
        d_coords = d.get("coords") or []
        if not d_coords:
            continue
            
        if e_type == "TEXT":
            pt_d = d_coords[0]
            pt_e = e_pts[0]
            dx = abs(float(pt_d[0]) - float(pt_e[0]))
            dy = abs(float(pt_d[1]) - float(pt_e[1]))
            if dx < 0.01 and dy < 0.01:
                matching_drawing = d
                break
        else:
            if len(d_coords) != len(e_pts):
                continue
            match = True
            for pt_d, pt_e in zip(d_coords, e_pts):
                if not pt_d or not pt_e or len(pt_d) < 2 or len(pt_e) < 2:
                    match = False
                    break
                dx = abs(float(pt_d[0]) - float(pt_e[0]))
                dy = abs(float(pt_d[1]) - float(pt_e[1]))
                if dx > 0.01 or dy > 0.01:
                    match = False
                    break
            if match:
                matching_drawing = d
                break
                
    if matching_drawing is not None:
        print(f"🗑️ Found matching digitizer drawing for committed DXF entity. Removing from digitizer.")
        digitizer.delete_selected_drawing(matching_drawing)


def _find_dxf_connected_line_group(entities, start_idx):
    if not (0 <= start_idx < len(entities)):
        return {start_idx}
    start = entities[start_idx]
    etype = str(start.get("type", "")).upper()
    if etype not in ("LINE", "POLYLINE"):
        return {start_idx}
    layer = str(start.get("layer", "0"))

    def _key(pt):
        z = float(pt[2]) if len(pt) > 2 else 0.0
        return (round(float(pt[0]), 3), round(float(pt[1]), 3), round(z, 3))

    vertices_by_idx = {}
    for i, ent in enumerate(entities):
        curr_type = str(ent.get("type", "")).upper()
        if curr_type not in ("LINE", "POLYLINE"):
            continue
        if str(ent.get("layer", "0")) != layer:
            continue
        verts = []
        if curr_type == "LINE":
            verts = [ent.get("start"), ent.get("end")]
        elif curr_type == "POLYLINE":
            verts = ent.get("points") or ent.get("vertices") or []
        if len(verts) < 2:
            continue
        vertices_by_idx[i] = set(_key(v) for v in verts if v is not None)

    if start_idx not in vertices_by_idx:
        return {start_idx}

    visited = {start_idx}
    stack = [start_idx]
    while stack:
        cur = stack.pop()
        cur_verts = vertices_by_idx[cur]
        for j, j_verts in vertices_by_idx.items():
            if j in visited:
                continue
            if not cur_verts.isdisjoint(j_verts):
                visited.add(j)
                stack.append(j)
    return visited


class _DXFEntityHighlightMixin:
    _entity_highlight_actors: list = []
    _dimmed_actor_states: dict = {}


class _CenteredCheckBox(QWidget):
    """A larger, centered checkbox cell for use inside a QTableWidget cell.

    Self-paints a classic Windows-style checkbox (white square, blue ✓) via
    QPainter, so it is immune to QCheckBox stylesheet rules in the global
    theme. The whole cell is clickable.
    """

    stateChanged = Signal(int)
    toggled = Signal(bool)

    _BOX_SIZE = 18

    def __init__(self, parent=None, *, hit_padding: int = 10):
        super().__init__(parent)
        self._hit = hit_padding
        self._checked = False
        self._hover   = False
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(self._BOX_SIZE + self._hit)
        self.setMinimumWidth(self._BOX_SIZE + self._hit * 2)

    def sizeHint(self):
        return QSize(self._BOX_SIZE + self._hit * 2,
                     self._BOX_SIZE + self._hit)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, checked: bool) -> None:
        checked = bool(checked)
        if checked == self._checked:
            return
        self._checked = checked
        self.update()
        self.toggled.emit(self._checked)
        state_value = (
            Qt.CheckState.Checked.value
            if self._checked
            else Qt.CheckState.Unchecked.value
        )
        self.stateChanged.emit(int(state_value))

    def checkState(self):
        return Qt.CheckState.Checked if self._checked else Qt.CheckState.Unchecked

    def setCheckState(self, state) -> None:
        try:
            state_value = state.value if hasattr(state, "value") else state
            state_value = int(state_value)
        except Exception:
            state_value = int(Qt.CheckState.Unchecked.value)
        self.setChecked(state_value == int(Qt.CheckState.Checked.value))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.setChecked(not self._checked)
            event.accept()
            return
        super().mousePressEvent(event)

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)

        cx = self.width()  // 2
        cy = self.height() // 2
        x  = cx - self._BOX_SIZE // 2
        y  = cy - self._BOX_SIZE // 2
        s  = self._BOX_SIZE

        # Box
        border = QColor("#222222") if self._checked else QColor("#8a8a8a")
        if self._hover and not self._checked:
            border = QColor("#555555")
        p.fillRect(x, y, s, s, QColor("#ffffff"))
        pen = QPen(border)
        pen.setWidth(1)
        p.setPen(pen)
        p.drawRect(x, y, s - 1, s - 1)

        # Check mark
        if self._checked:
            check = QPen(QColor("#222222"))
            check.setWidth(2)
            check.setCapStyle(Qt.RoundCap)
            check.setJoinStyle(Qt.RoundJoin)
            p.setPen(check)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.drawLine(x + 3,  y + s // 2,      x + s // 2 - 1, y + s - 4)
            p.drawLine(x + s // 2 - 1, y + s - 4, x + s - 3,     y + 3)

        p.end()


    def _resolve_app(self):
        app = getattr(self, 'app', None)
        if app is not None:
            return app
        parent_item = getattr(self, 'parent_item', None)
        if parent_item is None:
            return None
        app = getattr(parent_item, 'app', None)
        if app is not None:
            return app
        try:
            _pdlg = parent_item._find_parent_dialog()
            return getattr(_pdlg, 'app', None)
        except Exception:
            return None

    def _vtk_renderer(self):
        try:
            app = self._resolve_app()
            if app is None:
                return None
            return app.vtk_widget.renderer
        except Exception:
            return None

    def _vtk_force_render(self):
        try:
            app = self._resolve_app()
            if app is None:
                return
            rw = app.vtk_widget.GetRenderWindow()
            if rw is not None:
                rw.Render()
                return
        except Exception:
            pass
        try:
            app = self._resolve_app()
            if app is not None:
                app.vtk_widget.render()
        except Exception:
            pass

    def _dim_all_existing_actors(self):
        if self._dimmed_actor_states:
            return
        item = self.parent_item
        if not item or not getattr(item, "actor_cache", None):
            return
        snapshot = {}
        for _layer, actors in _normalize_dxf_actor_cache(item, getattr(self, "attachment", None)).items():
            for actor in actors or []:
                if actor is None:
                    continue
                try:
                    prop = actor.GetProperty() if hasattr(actor, "GetProperty") else None
                except Exception:
                    prop = None
                if prop is None:
                    continue
                key = id(actor)
                if key in snapshot:
                    continue
                try:
                    snapshot[key] = {"actor": actor, "opacity": float(prop.GetOpacity())}
                    prop.SetOpacity(0.18)
                except Exception:
                    continue
        self._dimmed_actor_states = snapshot

    def _restore_dimmed_actors(self):
        states = list((self._dimmed_actor_states or {}).values())
        self._dimmed_actor_states = {}
        for s in states:
            actor = s.get("actor")
            if actor is None:
                continue
            try:
                prop = actor.GetProperty() if hasattr(actor, "GetProperty") else None
            except Exception:
                prop = None
            if prop is None:
                continue
            try:
                prop.SetOpacity(float(s.get("opacity", 1.0)))
            except Exception:
                continue

    def _clear_entity_highlight(self):
        actors = list(getattr(self, "_entity_highlight_actors", None) or [])
        self._entity_highlight_actors = []
        renderer = self._vtk_renderer()
        if renderer is not None:
            for a in actors:
                try:
                    renderer.RemoveActor(a)
                except Exception:
                    pass
        self._restore_dimmed_actors()

    def _isolate_entity_by_index(self, orig_idx):
        if not self.parent_item or not self._resolve_app():
            return
        if not self.attachment or "entities" not in self.attachment:
            return

        entities = self.attachment["entities"] or []
        if not (0 <= orig_idx < len(entities)):
            return

        group_indices = _find_dxf_connected_line_group(entities, orig_idx)
        group_ents = [entities[i] for i in group_indices if 0 <= i < len(entities)]
        if not group_ents:
            return

        is_all_text = all(str(e.get("type", "")).upper() == "TEXT" for e in group_ents)

        self._clear_entity_highlight()
        if not is_all_text:
            self._dim_all_existing_actors()

        renderer = self._vtk_renderer()
        new_actors = []
        if renderer is not None and not is_all_text:
            for e in group_ents:
                ha = _make_dxf_entity_highlight_actor(e)
                if ha is None:
                    continue
                try:
                    renderer.AddActor(ha)
                    new_actors.append(ha)
                except Exception:
                    continue
        self._entity_highlight_actors = new_actors

        overall = None
        for e in group_ents:
            b = None
            etype = str(e.get("type", "")).upper()
            if etype == "TEXT":
                txt = str(e.get("text", "")).strip()
                layer = str(e.get("layer", "0"))
                item = self.parent_item
                if item and getattr(item, "actor_cache", None):
                    for a in (item.actor_cache.get(layer) or []):
                        if getattr(a, "text_content", None) == txt:
                            try:
                                act_b = a.GetBounds()
                                if act_b and act_b[0] <= act_b[1]:
                                    b = act_b
                            except Exception:
                                pass
                            break
            if not b:
                b = get_dxf_entity_bounds(e)
            if not b:
                continue
            if overall is None:
                overall = list(b)
            else:
                overall[0] = min(overall[0], b[0])
                overall[1] = max(overall[1], b[1])
                overall[2] = min(overall[2], b[2])
                overall[3] = max(overall[3], b[3])
                overall[4] = min(overall[4], b[4])
                overall[5] = max(overall[5], b[5])

        if overall is not None and renderer is not None:
            try:
                xmin, xmax, ymin, ymax, zmin, zmax = overall
                dx = xmax - xmin
                dy = ymax - ymin
                dz = zmax - zmin
                pad_x = max(2.0, dx * 0.25)
                pad_y = max(2.0, dy * 0.25)
                pad_z = max(2.0, dz * 0.25)
                if not bool(getattr(renderer, "_skip_camera_reset", False)):
                    renderer.ResetCamera((
                    xmin - pad_x, xmax + pad_x,
                    ymin - pad_y, ymax + pad_y,
                    zmin - pad_z, zmax + pad_z,
                ))
                renderer.ResetCameraClippingRange()
            except Exception as exc:
                print(f"Camera fit failed: {exc}")

            self._vtk_force_render()


class DXFLayerDrawingsDialog(InputPopupMixin, _DXFEntityHighlightMixin, QDialog):
    def __init__(self, layer_name: str, parent_item, parent=None):
        super().__init__(parent)
        self.layer_name = layer_name
        self.parent_item = parent_item

        self.app = getattr(parent_item, "app", None) if parent_item is not None else None
        if self.app is None and parent_item is not None:
            try:
                _pdlg = parent_item._find_parent_dialog()
                self.app = getattr(_pdlg, "app", None)
            except Exception:
                self.app = None

        self.attachment = self._get_live_attachment()
        self.setWindowTitle(f"Manage Drawings - Level: {layer_name}")
        self.resize(600, 400)
        self._init_ui()
        self._load_drawings()

    def _resolve_app(self):
        app = getattr(self, "app", None)
        if app is not None:
            return app
        parent_item = getattr(self, "parent_item", None)
        if parent_item is None:
            return None
        app = getattr(parent_item, "app", None)
        if app is not None:
            self.app = app
            return app
        try:
            parent_dlg = parent_item._find_parent_dialog()
            app = getattr(parent_dlg, "app", None)
            if app is not None:
                self.app = app
            return app
        except Exception:
            return None

    def _get_live_attachment(self):
        parent_item = getattr(self, "parent_item", None)
        if parent_item is not None:
            att = getattr(parent_item, "attachment", None)
            if isinstance(att, dict):
                _ensure_dxf_parsed(att, parent_item)
                self.attachment = att
                return att

        app = self._resolve_app()
        if app is None or parent_item is None:
            return getattr(self, "attachment", None) if isinstance(getattr(self, "attachment", None), dict) else None

        target_name = ""
        target_full = ""
        try:
            target_name = os.path.basename(str(parent_item.dxf_path.name or "")).lower()
            target_full = str(parent_item.dxf_path.resolve()).lower()
        except Exception:
            pass

        for att in list(getattr(app, "dxf_attachments", []) or []):
            if not isinstance(att, dict):
                continue
            att_name = os.path.basename(str(att.get("filename", "") or "")).lower()
            att_full = str(att.get("full_path", "") or "").lower()
            if (target_name and att_name == target_name) or (target_full and att_full == target_full) or (target_name and att_full and os.path.basename(att_full) == target_name):
                _ensure_dxf_parsed(att, parent_item)
                self.attachment = att
                try:
                    parent_item.attachment = att
                    att["_dxf_item"] = parent_item
                    att["actor_cache_map"] = _normalize_dxf_actor_cache(parent_item, att)
                except Exception:
                    pass
                return att
        return None


    def _vtk_renderer(self):
        try:
            app = self._resolve_app()
            if app is None:
                return None
            return app.vtk_widget.renderer
        except Exception:
            return None

    def _vtk_force_render(self):
        try:
            app = self._resolve_app()
            if app is None:
                return
            rw = app.vtk_widget.GetRenderWindow()
            if rw is not None:
                rw.Render()
                return
        except Exception:
            pass
        try:
            app = self._resolve_app()
            if app is not None:
                app.vtk_widget.render()
        except Exception:
            pass

    def _dim_all_existing_actors(self):
        if getattr(self, "_dimmed_actor_states", None):
            return
        item = getattr(self, "parent_item", None)
        if item is None:
            return
        actor_cache = _normalize_dxf_actor_cache(item, getattr(self, "attachment", None))
        if not actor_cache:
            return
        snapshot = {}
        for _layer, actors in list(actor_cache.items()):
            for actor in list(actors or []):
                if actor is None:
                    continue
                try:
                    prop = actor.GetProperty() if hasattr(actor, "GetProperty") else None
                except Exception:
                    prop = None
                if prop is None:
                    continue
                key = id(actor)
                if key in snapshot:
                    continue
                try:
                    snapshot[key] = {"actor": actor, "opacity": float(prop.GetOpacity())}
                    prop.SetOpacity(0.18)
                except Exception:
                    continue
        self._dimmed_actor_states = snapshot

    def _restore_dimmed_actors(self):
        states = list((getattr(self, "_dimmed_actor_states", None) or {}).values())
        self._dimmed_actor_states = {}
        for s in states:
            actor = s.get("actor")
            if actor is None:
                continue
            try:
                prop = actor.GetProperty() if hasattr(actor, "GetProperty") else None
            except Exception:
                prop = None
            if prop is None:
                continue
            try:
                prop.SetOpacity(float(s.get("opacity", 1.0)))
            except Exception:
                continue

    def _clear_entity_highlight(self):
        actors = list(getattr(self, "_entity_highlight_actors", None) or [])
        self._entity_highlight_actors = []
        renderer = self._vtk_renderer()
        if renderer is not None:
            for actor in actors:
                try:
                    renderer.RemoveActor(actor)
                except Exception:
                    pass
        self._restore_dimmed_actors()

    def _isolate_entity_by_index(self, orig_idx):
        """Highlight one Manage Drawings row without crashing.

        Supports both saved DXF entities and live unsaved digitizer drawings
        shown by _load_drawings().
        """
        entities = list(getattr(self, "full_entities_list", None) or [])
        if not entities and isinstance(getattr(self, "attachment", None), dict):
            parsed = _ensure_dxf_parsed(self.attachment, self.parent_item)
            entities = [e for e in list(parsed.get("entities", []) or []) if isinstance(e, dict)]
        if not (0 <= int(orig_idx) < len(entities)):
            return

        try:
            group_indices = _find_dxf_connected_line_group(entities, int(orig_idx))
        except Exception:
            group_indices = {int(orig_idx)}
        group_ents = [entities[i] for i in group_indices if 0 <= i < len(entities)]
        if not group_ents:
            return

        self._clear_entity_highlight()
        self._dim_all_existing_actors()

        renderer = self._vtk_renderer()
        new_actors = []
        if renderer is not None:
            for ent in group_ents:
                actor = _make_dxf_entity_highlight_actor(ent)
                if actor is None:
                    continue
                try:
                    renderer.AddActor(actor)
                    new_actors.append(actor)
                except Exception:
                    continue
        self._entity_highlight_actors = new_actors

        overall = None
        for ent in group_ents:
            bounds = get_dxf_entity_bounds(ent)
            if not bounds:
                continue
            if overall is None:
                overall = list(bounds)
            else:
                overall[0] = min(overall[0], bounds[0])
                overall[1] = max(overall[1], bounds[1])
                overall[2] = min(overall[2], bounds[2])
                overall[3] = max(overall[3], bounds[3])
                overall[4] = min(overall[4], bounds[4])
                overall[5] = max(overall[5], bounds[5])

        if overall is not None and renderer is not None:
            try:
                xmin, xmax, ymin, ymax, zmin, zmax = overall
                dx = max(float(xmax) - float(xmin), 1.0)
                dy = max(float(ymax) - float(ymin), 1.0)
                dz = max(float(zmax) - float(zmin), 1.0)
                pad_x = max(2.0, dx * 0.25)
                pad_y = max(2.0, dy * 0.25)
                pad_z = max(2.0, dz * 0.25)
                if not bool(getattr(renderer, "_skip_camera_reset", False)):
                    renderer.ResetCamera((
                    xmin - pad_x, xmax + pad_x,
                    ymin - pad_y, ymax + pad_y,
                    zmin - pad_z, zmax + pad_z,
                ))
                renderer.ResetCameraClippingRange()
            except Exception:
                pass
        self._vtk_force_render()

    def _init_ui(self):
        from PySide6.QtWidgets import (
            QLineEdit, QTableWidget, QHeaderView, QAbstractItemView, QLabel, QPushButton, QHBoxLayout, QVBoxLayout, QWidget
        )
        from PySide6.QtCore import Qt
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        self.setStyleSheet("""
            QDialog {
                background-color: #121212;
                color: #e0e0e0;
                font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, Roboto, sans-serif;
            }
            QLabel#dialogTitle {
                color: #ffffff;
                font-size: 14px;
                font-weight: 600;
            }
            QLineEdit#searchBox {
                background-color: #181818;
                color: #e0e0e0;
                border: 1px solid #2d2d2d;
                border-radius: 6px;
                padding: 6px 10px;
                font-size: 11px;
            }
            QLineEdit#searchBox:focus {
                border-color: #007acc;
                background-color: #1a1a1a;
            }
            QTableWidget {
                background-color: #161616;
                alternate-background-color: #1d1d1d;
                color: #e0e0e0;
                gridline-color: #242424;
                border: 1px solid #2d2d2d;
                border-radius: 6px;
                font-size: 11px;
            }
            QTableWidget::item {
                padding: 6px;
            }
            QTableWidget::item:selected {
                background-color: #1a2a3a;
                color: #ffffff;
            }
            QHeaderView::section {
                background-color: #1a1a1a;
                color: #8c8c8c;
                padding: 6px;
                border: none;
                border-bottom: 1px solid #2d2d2d;
                font-weight: bold;
                font-size: 9px;
                text-transform: uppercase;
            }
            QScrollBar:vertical {
                border: none;
                background: #111111;
                width: 6px;
                margin: 0px;
            }
            QScrollBar::handle:vertical {
                background: #333333;
                min-height: 20px;
                border-radius: 3px;
            }
            QScrollBar::handle:vertical:hover {
                background: #444444;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                border: none;
                background: none;
            }
            QPushButton {
                background-color: #222222;
                color: #e0e0e0;
                border: 1px solid #333333;
                border-radius: 5px;
                padding: 4px 10px;
                font-size: 11px;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #2d2d2d;
                border-color: #444444;
            }
            QPushButton:pressed {
                background-color: #181818;
            }
            QPushButton#isoBtn {
                background-color: #13243f;
                color: #60a5fa;
                border: 1px solid #1c355e;
            }
            QPushButton#isoBtn:hover {
                background-color: #1b3259;
                border-color: #60a5fa;
            }
            QPushButton#deleteBtn {
                background-color: #2a1414;
                color: #f87171;
                border: 1px solid #3f1e1e;
            }
            QPushButton#deleteBtn:hover {
                background-color: #3d1b1b;
                border-color: #f87171;
            }
            QPushButton#closeBtn {
                background-color: #007acc;
                color: #ffffff;
                border: 1px solid #007acc;
                font-weight: 600;
                padding: 6px 16px;
            }
            QPushButton#closeBtn:hover {
                background-color: #0098ff;
                border-color: #0098ff;
            }
        """)

        title = QLabel(f"Drawings in Level: {self.layer_name}")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)

        self.search_box = QLineEdit()
        self.search_box.setObjectName("searchBox")
        self.search_box.setPlaceholderText("🔍 Filter drawings (type or text)...")
        self.search_box.textChanged.connect(self._load_drawings)
        layout.addWidget(self.search_box)

        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["No.", "Type", "Details", "Actions"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)

        footer = QHBoxLayout()
        footer.addStretch()
        save_btn = QPushButton("Save")
        save_btn.setObjectName("closeBtn")
        save_btn.clicked.connect(self._save_from_manage_drawings)
        footer.addWidget(save_btn)

        close_btn = QPushButton("Close")
        close_btn.setObjectName("closeBtn")
        close_btn.clicked.connect(self.accept)
        footer.addWidget(close_btn)
        layout.addLayout(footer)

    def _load_drawings(self):
        from PySide6.QtWidgets import QTableWidgetItem
        self.table.setRowCount(0)
        self.drawings_list = []
        self.full_entities_list = []

        if not isinstance(self.attachment, dict):
            return

        parsed = _ensure_dxf_parsed(self.attachment, self.parent_item)
        base_entities = [ent for ent in list(parsed.get("entities", []) or []) if isinstance(ent, dict)]
        entities = list(base_entities)

        # SNT-style behavior: Manage Drawings also shows live, unsaved active drawings.
        app = self._resolve_app()
        digitizer = getattr(app, "digitizer", None) if app is not None else None
        target_file = ""
        try:
            target_file = os.path.basename(str(self.parent_item.dxf_path.name or "")).lower()
        except Exception:
            target_file = os.path.basename(str(self.attachment.get("filename", "") or "")).lower()
        active_file = os.path.basename(str(getattr(app, "active_dxf_filename", "") or "")).lower() if app is not None else ""

        if digitizer is not None:
            for d in list(getattr(digitizer, "drawings", []) or []):
                if not isinstance(d, dict):
                    continue
                if d.get("dxf_committed"):
                    continue

                dlayer = str(d.get("layer", "") or "").strip()
                if not dlayer or dlayer == "DIGITIZER":
                    continue

                owner_file = os.path.basename(str(d.get("dxf_file", "") or "")).lower()
                if owner_file:
                    if owner_file != target_file:
                        continue
                elif active_file and active_file != target_file:
                    continue

                layer_color = _dxf_layer_color(parsed, dlayer, _extract_dxf_drawing_color(d))
                ent = _build_dxf_entity_from_drawing(d, dlayer, layer_color)
                if ent is None:
                    continue

                already_present = False
                for existing in base_entities:
                    if str(existing.get("layer", "")) == dlayer and _same_dxf_geometry(existing, d):
                        already_present = True
                        break
                if already_present:
                    continue

                ent["_digitizer_drawing"] = d
                entities.append(ent)

        self.full_entities_list = entities
        search_text = self.search_box.text().strip().lower()

        row_idx = 0
        for orig_idx, ent in enumerate(entities):
            if str(ent.get("layer", "0")) != self.layer_name:
                continue

            etype = str(ent.get("type", "")).upper()
            details = ""
            if etype in ("POLYLINE", "POLYGON", "LINE"):
                pts = ent.get("points") or ent.get("vertices") or []
                if etype == "LINE" and "start" in ent and "end" in ent:
                    pts = [ent["start"], ent["end"]]
                details = f"{len(pts)} points"
                if ent.get("closed"):
                    details += ", closed"
            elif etype == "3DFACE":
                verts = ent.get("vertices", [])
                details = f"{len(verts)} vertices"
            elif etype == "TEXT":
                details = f"Text: '{ent.get('text', '')}'"
            elif etype == "POINT":
                pos = ent.get("position", [0.0, 0.0, 0.0])
                details = f"Coords: ({pos[0]:.2f}, {pos[1]:.2f}, {pos[2] if len(pos) > 2 else 0.0:.2f})"
            elif etype == "CIRCLE":
                details = f"Radius: {ent.get('radius', 1.0):.2f}"
            else:
                details = "Entity drawing"

            if search_text and search_text not in etype.lower() and search_text not in details.lower():
                continue

            self.drawings_list.append((orig_idx, ent))
            self.table.insertRow(row_idx)

            no_item = QTableWidgetItem(f"#{row_idx + 1}")
            no_item.setFlags(no_item.flags() & ~Qt.ItemIsEditable)
            no_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row_idx, 0, no_item)

            type_item = QTableWidgetItem(etype)
            type_item.setFlags(type_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row_idx, 1, type_item)

            det_item = QTableWidgetItem(details)
            det_item.setFlags(det_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row_idx, 2, det_item)

            actions_widget = QWidget()
            actions_layout = QHBoxLayout(actions_widget)
            actions_layout.setContentsMargins(0, 0, 0, 0)
            actions_layout.setSpacing(4)
            actions_layout.setAlignment(Qt.AlignCenter)

            iso_btn = QPushButton("View")
            iso_btn.setObjectName("isoBtn")
            iso_btn.setMinimumSize(48, 24)
            iso_btn.setCursor(Qt.PointingHandCursor)
            iso_btn.setToolTip("Highlight this specific drawing in the main view and fit camera")
            iso_btn.clicked.connect(lambda checked=False, idx=orig_idx: self._isolate_entity_by_index(idx))
            actions_layout.addWidget(iso_btn)

            del_btn = QPushButton("Delete")
            del_btn.setObjectName("deleteBtn")
            del_btn.setMinimumSize(52, 24)
            del_btn.setCursor(Qt.PointingHandCursor)
            del_btn.setToolTip("Delete this drawing element from level")
            del_btn.clicked.connect(lambda checked=False, idx=orig_idx: self._delete_entity(idx))
            actions_layout.addWidget(del_btn)

            self.table.setCellWidget(row_idx, 3, actions_widget)
            row_idx += 1
    def _load_drawings_keep_scroll(self):
        """Reload Manage Drawings table without jumping the scroll position to top."""
        try:
            bar = self.table.verticalScrollBar()
            old_scroll = int(bar.value())
            old_row = int(self.table.currentRow())
        except Exception:
            bar = None
            old_scroll = 0
            old_row = -1

        self._load_drawings()

        def _restore_scroll():
            try:
                if bar is not None:
                    bar.setValue(min(old_scroll, bar.maximum()))
                if old_row >= 0 and old_row < self.table.rowCount():
                    self.table.setCurrentCell(old_row, 0)
            except Exception:
                pass

        try:
            QTimer.singleShot(0, _restore_scroll)
            QTimer.singleShot(50, _restore_scroll)
            QTimer.singleShot(150, _restore_scroll)
        except Exception:
            _restore_scroll()

    def _save_from_manage_drawings(self):
        parent = self.parent()
        save_fn = getattr(parent, "_save_layers_to_disk", None)
        if callable(save_fn):
            save_fn()
            self._load_drawings()
            return
        QMessageBox.information(self, "Save DXF", "Open the Level Manager and use Save to write changes.")

    def _refresh_after_entity_change(self, entities, affected_layers):
        if not self.parent_item:
            return
        _app = self._resolve_app()
        if _app is None or not isinstance(self.attachment, dict):
            return

        renderer = getattr(getattr(_app, "vtk_widget", None), "renderer", None)
        cam_state = None
        if renderer is not None:
            try:
                cam = renderer.GetActiveCamera()
                if cam is not None:
                    cam_state = {
                        "position": cam.GetPosition(),
                        "focal_point": cam.GetFocalPoint(),
                        "view_up": cam.GetViewUp(),
                        "parallel_scale": cam.GetParallelScale(),
                        "clipping_range": cam.GetClippingRange(),
                    }
                setattr(renderer, "_skip_camera_reset", True)
            except Exception:
                cam_state = None

        try:
            rebuild_dxf_attachment(_app, self.attachment)
            item = self.parent_item
            valid_names = {name for name, _c, _clr in (item.layer_stats_cache or [])}
            selected_now = set(valid_names) if item.selected_layers is None else {
                ln for ln in item.selected_layers if ln in valid_names
            }
            item._apply_layer_selection(selected_now, len(valid_names))
        finally:
            if renderer is not None:
                try:
                    if cam_state is not None:
                        cam = renderer.GetActiveCamera()
                        if cam is not None:
                            cam.SetPosition(*cam_state["position"])
                            cam.SetFocalPoint(*cam_state["focal_point"])
                            cam.SetViewUp(*cam_state["view_up"])
                            cam.SetParallelScale(cam_state["parallel_scale"])
                            cam.SetClippingRange(*cam_state["clipping_range"])
                    renderer.ResetCameraClippingRange()
                except Exception:
                    pass
                try:
                    setattr(renderer, "_skip_camera_reset", False)
                except Exception:
                    pass
            try:
                _app.vtk_widget.render()
            except Exception:
                pass
    def closeEvent(self, event):
        try:
            self._clear_entity_highlight()
            self._vtk_force_render()
        except Exception:
            pass
        super().closeEvent(event)

    def done(self, result):
        try:
            self._clear_entity_highlight()
            self._vtk_force_render()
        except Exception:
            pass
        super().done(result)

    def _delete_entity(self, orig_idx):
        if not isinstance(self.attachment, dict):
            return

        entities = list(getattr(self, "full_entities_list", []) or [])
        if not entities:
            parsed = _ensure_dxf_parsed(self.attachment, self.parent_item)
            entities = parsed.get("entities", []) or []

        if not (0 <= orig_idx < len(entities)):
            return

        ent = entities[orig_idx]
        digitizer_drawing = ent.get("_digitizer_drawing") if isinstance(ent, dict) else None

        if digitizer_drawing is not None:
            prompt = "Delete this active drawing from the level?"
        else:
            delete_indices = _find_dxf_connected_line_group(entities, orig_idx)
            delete_count = len(delete_indices)
            if delete_count > 1:
                prompt = (
                    f"This drawing is made of {delete_count} connected line segments.\n"
                    f"Delete the entire drawing from the level?"
                )
            else:
                prompt = "Are you sure you want to delete this specific drawing from the level?"

        reply = QMessageBox.warning(
            self,
            "Delete Drawing",
            prompt,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        try:
            self._clear_entity_highlight()
        except Exception:
            pass

        app = self._resolve_app()
        digitizer = getattr(app, "digitizer", None) if app else None

        if digitizer_drawing is not None:
            if digitizer is not None:
                try:
                    remove_fn = getattr(digitizer, "_remove_drawing", None)
                    if callable(remove_fn):
                        remove_fn(digitizer_drawing)
                    else:
                        digitizer.delete_selected_drawing(digitizer_drawing)
                except Exception:
                    try:
                        if digitizer_drawing in digitizer.drawings:
                            digitizer.drawings.remove(digitizer_drawing)
                    except Exception:
                        pass
            self._load_drawings_keep_scroll()
            try:
                parent = self.parent_item
                dlg = getattr(parent, "_layer_selection_dlg", None) if parent is not None else None
                if dlg is not None:
                    dlg._refresh_layers_on_draw(digitizer_drawing)
            except Exception:
                pass
            return

        parsed = _ensure_dxf_parsed(self.attachment, self.parent_item)
        stored_entities = parsed.get("entities", []) or []
        delete_indices = _find_dxf_connected_line_group(stored_entities, orig_idx)

        for idx in sorted(delete_indices, reverse=True):
            if 0 <= idx < len(stored_entities):
                ent_to_delete = stored_entities[idx]
                if digitizer is not None:
                    try:
                        _delete_matching_digitizer_drawing(digitizer, ent_to_delete)
                    except Exception as e:
                        print(f"⚠️ Failed to remove matching digitizer drawing: {e}")
                stored_entities.pop(idx)

        self.attachment["entities"] = stored_entities
        self._refresh_after_entity_change(stored_entities, [self.layer_name])
        self._load_drawings_keep_scroll()
class DXFMoveElementsDialog(_DXFEntityHighlightMixin, QDialog):
    def __init__(self, source_layer: str, parent_item, attachment, parent=None):
        super().__init__(parent)
        self.source_layer = str(source_layer or "")
        self.parent_item = parent_item
        self.attachment = attachment
        self.setWindowTitle(f"Move Elements — FROM '{self.source_layer}'")
        self.setModal(True)
        self.resize(820, 580)
        self.drawings_list: List[Tuple[int, Dict]] = []
        self._entity_highlight_actors = []
        self._init_ui()
        self._load_drawings()

    def _init_ui(self):
        from PySide6.QtWidgets import (
            QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView,
            QAbstractItemView, QComboBox, QCheckBox, QFrame, QLabel, QPushButton, QHBoxLayout, QVBoxLayout, QWidget
        )
        from PySide6.QtCore import Qt

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        self.setStyleSheet("""
            QDialog { background-color: #121212; color: #e0e0e0; font-family: 'Segoe UI', sans-serif; }
            QLabel#dialogTitle { color: #ffffff; font-size: 15px; font-weight: 600; }
            QLabel#subTitle { color: #9ca3af; font-size: 11px; }
            QFrame#fromCard {
                background-color: #1a1a1a;
                border: 1px solid #3f1e1e;
                border-left: 3px solid #f87171;
                border-radius: 8px;
                padding: 4px;
            }
            QFrame#toCard {
                background-color: #1a1a1a;
                border: 1px solid #1c355e;
                border-left: 3px solid #60a5fa;
                border-radius: 8px;
                padding: 4px;
            }
            QLabel#cardCaption {
                color: #9ca3af; font-size: 9px; font-weight: 700;
                text-transform: uppercase; letter-spacing: 1px;
            }
            QLabel#fromName { color: #f87171; font-size: 14px; font-weight: 700; }
            QLabel#fromMeta { color: #9ca3af; font-size: 10px; }
            QLabel#arrowLabel { color: #fbbf24; font-size: 26px; font-weight: 700; }
            QComboBox#targetBox {
                background-color: #0e1828; color: #ffffff;
                border: 1px solid #1c355e; border-radius: 6px;
                padding: 6px 10px; font-size: 13px; font-weight: 600;
            }
            QComboBox#targetBox:focus { border-color: #60a5fa; }
            QComboBox#sourceBox {
                background-color: #2a1414; color: #ffffff;
                border: 1px solid #3f1e1e; border-radius: 6px;
                padding: 6px 10px; font-size: 13px; font-weight: 600;
            }
            QComboBox#sourceBox:focus { border-color: #f87171; }
            QLineEdit#searchBox {
                background-color: #181818; color: #e0e0e0;
                border: 1px solid #2d2d2d; border-radius: 6px;
                padding: 6px 10px; font-size: 11px;
            }
            QLineEdit#searchBox:focus { border-color: #007acc; }
            QTableWidget {
                background-color: #161616; alternate-background-color: #1d1d1d;
                color: #e0e0e0; gridline-color: #242424;
                border: 1px solid #2d2d2d; border-radius: 6px; font-size: 11px;
            }
            QTableWidget::item { padding: 6px; }
            QHeaderView::section {
                background-color: #1a1a1a; color: #8c8c8c;
                padding: 6px; border: none; border-bottom: 1px solid #2d2d2d;
                font-weight: bold; font-size: 9px; text-transform: uppercase;
            }
            QPushButton {
                background-color: #222222; color: #e0e0e0;
                border: 1px solid #333333; border-radius: 5px;
                padding: 5px 12px; font-size: 11px; font-weight: 500;
            }
            QPushButton:hover { background-color: #2d2d2d; border-color: #444444; }
            QPushButton#isoBtn {
                background-color: #13243f; color: #60a5fa;
                border: 1px solid #1c355e;
            }
            QPushButton#isoBtn:hover { background-color: #1b3259; border-color: #60a5fa; }
            QPushButton#moveBtn {
                background-color: #1b3d2a; color: #34d399;
                border: 1px solid #1f5235; font-weight: 600;
                padding: 7px 18px; font-size: 12px;
            }
            QPushButton#moveBtn:hover { background-color: #244e36; border-color: #34d399; }
            QPushButton#moveBtn:disabled { color: #4b6357; border-color: #1b3d2a; background-color: #161616; }
            QPushButton#cancelBtn {
                background-color: #1f1f1f; color: #d0d0d0;
                padding: 7px 18px;
            }
        """)

        title = QLabel("Move Drawings Between Levels")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)
        subtitle = QLabel("Pick which drawings to move from one level to another. Use the 👁 button to confirm a drawing in the main view before moving.")
        subtitle.setObjectName("subTitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        cards_row = QHBoxLayout()
        cards_row.setSpacing(10)

        from_card = QFrame()
        from_card.setObjectName("fromCard")
        from_box = QVBoxLayout(from_card)
        from_box.setContentsMargins(12, 8, 12, 10)
        from_box.setSpacing(4)
        from_caption = QLabel("FROM (source)")
        from_caption.setObjectName("cardCaption")
        from_box.addWidget(from_caption)
        self.source_combo = QComboBox()
        self.source_combo.setObjectName("sourceBox")
        from_box.addWidget(self.source_combo)
        self.source_meta = QLabel("")
        self.source_meta.setObjectName("fromMeta")
        from_box.addWidget(self.source_meta)
        cards_row.addWidget(from_card, 1)

        arrow = QLabel("→")
        arrow.setObjectName("arrowLabel")
        arrow.setAlignment(Qt.AlignCenter)
        arrow.setFixedWidth(40)
        cards_row.addWidget(arrow)

        to_card = QFrame()
        to_card.setObjectName("toCard")
        to_box = QVBoxLayout(to_card)
        to_box.setContentsMargins(12, 8, 12, 10)
        to_box.setSpacing(4)
        to_caption = QLabel("TO (target)")
        to_caption.setObjectName("cardCaption")
        to_box.addWidget(to_caption)
        self.target_combo = QComboBox()
        self.target_combo.setObjectName("targetBox")
        to_box.addWidget(self.target_combo)
        self.target_meta = QLabel("")
        self.target_meta.setObjectName("fromMeta")
        to_box.addWidget(self.target_meta)
        cards_row.addWidget(to_card, 1)

        layout.addLayout(cards_row)

        self._populate_level_combos()
        self.source_combo.currentTextChanged.connect(self._on_source_changed)
        self.target_combo.currentIndexChanged.connect(self._update_target_meta)
        self._update_source_meta()
        self._update_target_meta()

        controls = QHBoxLayout()
        self.search_box = QLineEdit()
        self.search_box.setObjectName("searchBox")
        self.search_box.setPlaceholderText("🔍 Filter drawings (type or text)...")
        self.search_box.textChanged.connect(self._load_drawings)
        controls.addWidget(self.search_box, 1)

        select_all_btn = QPushButton("Select All")
        select_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        controls.addWidget(select_all_btn)
        deselect_btn = QPushButton("Deselect All")
        deselect_btn.clicked.connect(lambda: self._set_all_checked(False))
        controls.addWidget(deselect_btn)
        layout.addLayout(controls)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["", "No.", "Type", "Details", "Locate"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)

        footer = QHBoxLayout()
        self.count_label = QLabel("0 of 0 selected")
        self.count_label.setStyleSheet("color: #9ca3af; font-size: 11px;")
        footer.addWidget(self.count_label)
        footer.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("cancelBtn")
        cancel_btn.clicked.connect(self.reject)
        footer.addWidget(cancel_btn)
        self.move_btn = QPushButton("Move Selected →")
        self.move_btn.setObjectName("moveBtn")
        self.move_btn.clicked.connect(self._on_move_clicked)
        footer.addWidget(self.move_btn)
        layout.addLayout(footer)

        self._update_count_label()

    def _all_levels(self):
        out = []
        for n, count, _clr in (
            (self.parent_item.layer_stats_cache if self.parent_item else None) or []
        ):
            if n:
                try:
                    out.append((str(n), int(count)))
                except (TypeError, ValueError):
                    out.append((str(n), 0))
        out.sort(key=lambda x: x[0].lower())
        return out

    def _populate_level_combos(self):
        levels = self._all_levels()
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        for name, count in levels:
            self.source_combo.addItem(name, count)
        idx = self.source_combo.findText(self.source_layer)
        if idx >= 0:
            self.source_combo.setCurrentIndex(idx)
        self.source_combo.blockSignals(False)

        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        current_source = self.source_combo.currentText().strip()
        for name, count in levels:
            if name == current_source:
                continue
            self.target_combo.addItem(name, count)
        self.target_combo.blockSignals(False)

    def _on_source_changed(self, new_source):
        new_source = str(new_source or "").strip()
        if not new_source or new_source == self.source_layer:
            self._refresh_target_combo()
            return
        self.source_layer = new_source
        self.setWindowTitle(f"Move Elements — FROM '{self.source_layer}'")
        self._refresh_target_combo()
        self._update_source_meta()
        self._clear_entity_highlight()
        self._vtk_force_render()
        self._load_drawings()

    def _refresh_target_combo(self):
        levels = self._all_levels()
        current_source = self.source_combo.currentText().strip()
        previous_target = self.target_combo.currentText().strip()
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        for name, count in levels:
            if name == current_source:
                continue
            self.target_combo.addItem(name, count)
        if previous_target and previous_target != current_source:
            i = self.target_combo.findText(previous_target)
            if i >= 0:
                self.target_combo.setCurrentIndex(i)
        self.target_combo.blockSignals(False)
        self._update_target_meta()

    def _update_source_meta(self):
        if not hasattr(self, "source_meta"):
            return
        count = self.source_combo.currentData()
        try:
            count_int = int(count) if count is not None else 0
        except (TypeError, ValueError):
            count_int = 0
        self.source_meta.setText(f"{count_int} drawing(s) on this level")

    def _update_target_meta(self):
        if not hasattr(self, "target_meta"):
            return
        if self.target_combo.count() == 0:
            self.target_meta.setText("(no other levels available)")
            if hasattr(self, "move_btn"):
                self._update_count_label()
            return
        count = self.target_combo.currentData()
        try:
            count_int = int(count) if count is not None else 0
        except (TypeError, ValueError):
            count_int = 0
        self.target_meta.setText(f"{count_int} drawing(s) already on this level")
        if hasattr(self, "move_btn"):
            self._update_count_label()

    def _load_drawings(self):
        from PySide6.QtWidgets import QTableWidgetItem, QCheckBox
        self.table.setRowCount(0)
        self.drawings_list = []
        if not self.attachment or "entities" not in self.attachment:
            self._update_count_label()
            return

        entities = self.attachment["entities"] or []
        search_text = self.search_box.text().strip().lower()

        row_idx = 0
        for orig_idx, ent in enumerate(entities):
            if str(ent.get("layer", "0")) != self.source_layer:
                continue

            etype = str(ent.get("type", "")).upper()
            details = ""
            if etype in ("POLYLINE", "POLYGON", "LINE"):
                verts = ent.get("points") or ent.get("vertices") or []
                if etype == "LINE" and "start" in ent and "end" in ent:
                    verts = [ent["start"], ent["end"]]
                details = f"{len(verts)} points"
                if ent.get("closed"):
                    details += ", closed"
            elif etype == "3DFACE":
                details = f"{len(ent.get('vertices', []) or [])} vertices"
            elif etype == "TEXT":
                details = f"Text: '{ent.get('text', '')}'"
            elif etype == "POINT":
                pos = ent.get("position", [0.0, 0.0, 0.0])
                details = f"Coords: ({float(pos[0]):.2f}, {float(pos[1]):.2f}, {float(pos[2]) if len(pos) > 2 else 0.0:.2f})"
            else:
                details = "Entity drawing"

            if search_text and search_text not in etype.lower() and search_text not in details.lower():
                continue

            self.drawings_list.append((orig_idx, ent))
            self.table.insertRow(row_idx)

            chk = QCheckBox()
            chk.setStyleSheet("margin-left: 8px;")
            chk.stateChanged.connect(self._update_count_label)
            chk_holder = QWidget()
            h = QHBoxLayout(chk_holder)
            h.setContentsMargins(0, 0, 0, 0)
            h.setAlignment(Qt.AlignCenter)
            h.addWidget(chk)
            self.table.setCellWidget(row_idx, 0, chk_holder)

            no_item = QTableWidgetItem(f"#{row_idx + 1}")
            no_item.setFlags(no_item.flags() & ~Qt.ItemIsEditable)
            no_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row_idx, 1, no_item)

            type_item = QTableWidgetItem(etype)
            type_item.setFlags(type_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row_idx, 2, type_item)

            det_item = QTableWidgetItem(details)
            det_item.setFlags(det_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row_idx, 3, det_item)

            iso_btn = QPushButton("View")
            iso_btn.setObjectName("isoBtn")
            iso_btn.setFixedSize(28, 22)
            iso_btn.setCursor(Qt.PointingHandCursor)
            iso_btn.setToolTip("Highlight this drawing in the main view")
            iso_btn.clicked.connect(
                lambda checked=False, idx=orig_idx: self._isolate_entity_by_index(idx)
            )
            iso_holder = QWidget()
            ih = QHBoxLayout(iso_holder)
            ih.setContentsMargins(0, 0, 0, 0)
            ih.setAlignment(Qt.AlignCenter)
            ih.addWidget(iso_btn)
            self.table.setCellWidget(row_idx, 4, iso_holder)

            row_idx += 1

        self._update_count_label()

    def closeEvent(self, event):
        try:
            self._clear_entity_highlight()
            self._vtk_force_render()
        except Exception:
            pass
        super().closeEvent(event)

    def done(self, result):
        try:
            self._clear_entity_highlight()
            self._vtk_force_render()
        except Exception:
            pass
        super().done(result)

    def _row_checkbox(self, row):
        from PySide6.QtWidgets import QCheckBox
        holder = self.table.cellWidget(row, 0)
        if holder is None:
            return None
        return holder.findChild(QCheckBox)

    def _set_all_checked(self, checked: bool):
        for row in range(self.table.rowCount()):
            chk = self._row_checkbox(row)
            if chk is not None:
                chk.setChecked(checked)

    def _checked_orig_indices(self):
        out = []
        for row in range(self.table.rowCount()):
            chk = self._row_checkbox(row)
            if chk is not None and chk.isChecked():
                if 0 <= row < len(self.drawings_list):
                    out.append(self.drawings_list[row][0])
        return out

    def _update_count_label(self):
        selected = len(self._checked_orig_indices())
        total = self.table.rowCount()
        self.count_label.setText(f"{selected} of {total} selected")
        if hasattr(self, "move_btn"):
            self.move_btn.setEnabled(selected > 0 and self.target_combo.count() > 0)

    def _on_move_clicked(self):
        if not self.attachment or "entities" not in self.attachment:
            return
        if self.target_combo.count() == 0:
            QMessageBox.warning(self, "Move Elements", "No other levels are available to move into.")
            return

        target_layer = self.target_combo.currentText().strip()
        if not target_layer or target_layer == self.source_layer:
            return

        indices = self._checked_orig_indices()
        if not indices:
            QMessageBox.information(self, "Move Elements", "Select at least one drawing to move.")
            return

        reply = QMessageBox.question(
            self, "Confirm Move",
            f"Move {len(indices)} drawing(s)\n"
            f"FROM '{self.source_layer}'  →  TO '{target_layer}' ?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return

        target_color = None
        for n, _c, clr in (self.parent_item.layer_stats_cache or []):
            if n == target_layer:
                try:
                    target_color = (int(clr[0]), int(clr[1]), int(clr[2]))
                except (TypeError, ValueError, IndexError):
                    target_color = None
                break

        entities = self.attachment["entities"] or []
        full_indices = set()
        for idx in indices:
            full_indices.update(_find_dxf_connected_line_group(entities, idx))
            
        moved = 0
        for idx in full_indices:
            if 0 <= idx < len(entities):
                ent = entities[idx]
                ent["layer"] = target_layer
                if target_color is not None:
                    ent["color"] = target_color
                moved += 1

        try:
            if self.parent_item is not None and isinstance(
                getattr(self.parent_item, "selected_layers", None), set
            ):
                self.parent_item.selected_layers.add(target_layer)
        except Exception:
            pass

        try:
            _app = self._resolve_app()
            if _app is not None:
                rebuild_dxf_attachment(_app, self.attachment)
        except Exception as exc:
            print(f"Move: rebuild failed: {exc}")

        try:
            self._clear_entity_highlight()
            self._vtk_force_render()
        except Exception:
            pass

        QMessageBox.information(
            self, "Move Complete",
            f"Moved {moved} drawing(s)\n"
            f"FROM '{self.source_layer}'  →  TO '{target_layer}'."
        )
        self.accept()


def _is_label_actor(actor) -> bool:
    """
    Return True when a VTK actor is a text/label actor created by create_text_actor.
    This is actor-level detection, so it works even on mixed geometry+label layers.
    """
    return bool(
        getattr(actor, 'is_grid_label', False)
        or getattr(actor, 'text_content', None) is not None
    )


class MultiDXFAttachmentDialog(MinimizableDialogMixin, QDialog):
    """
    Dialog for attaching multiple DXF files with automatic PRJ detection
    
    Features:
    - Select multiple DXF files at once
    - Auto-detect .PRJ file for each DXF
    - Manage attached DXFs (view list, remove individual files)
    - Coordinate reprojection support
    - Overlay/underlay modes
    """
    
    dxf_attached = Signal(list)  # Emits list of attachment data
    

    def __init__(self, app, parent=None):
        # ✅ FIX 1: Robust Parent Finding
        # Ensures we attach to the actual window widget so minimize works
        from PySide6.QtWidgets import QWidget
        target_parent = None
        
        # Try to find the valid parent widget
        if parent and isinstance(parent, QWidget):
            target_parent = parent
        elif isinstance(app, QWidget):
            target_parent = app
        elif hasattr(app, 'window') and isinstance(app.window, QWidget):
            target_parent = app.window

        # Use parentless top-level window so native minimize goes to taskbar
        # (no floating restore chips/mini-bars above taskbar).
        super().__init__(None, Qt.Window)
        self._init_minimize_state()
        self.setAttribute(Qt.WA_QuitOnClose, False)

        self.setWindowModality(Qt.NonModal)
        self.app = app
        self.setProperty("themeStyledDialog", True)

        self.dxf_items = []  # List of DXFFileItem widgets
        self.project_crs = None
        self.load_worker = None
        self._close_requested_while_loading = False
        self._labels_hidden = False

        self.setWindowTitle("Attach Multiple DXF Files")
        self.setStyleSheet(get_dialog_stylesheet())
        self.setGeometry(150, 150, 700, 800)

        self.init_ui()
        # self.detect_project_crs()
    
    def init_ui(self):
        """Initialize the UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(12)
        
        # Title
        self.title_label = QLabel("Attach Multiple DXF Files")
        self.title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.title_label)

        self.info_label = QLabel(
            "Add one or more DXF overlays, review them in the list below, and attach the "
            "checked files to the active project."
        )
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)
        
        # Step 1: File Selection
        file_group = QGroupBox("Select DXF Files")
        file_layout = QVBoxLayout()
        
        self.select_btn = QPushButton("Browse and Add DXF Files...")
        self.select_btn.setObjectName("secondaryBtn")
        self.select_btn.setAutoDefault(False)
        self.select_btn.setDefault(False)
        self.select_btn.setFocusPolicy(Qt.NoFocus)
        self.select_btn.clicked.connect(self.select_dxf_files)
        file_layout.addWidget(self.select_btn)
        
        file_group.setLayout(file_layout)
        layout.addWidget(file_group)
        
        # Step 2: Selected Files List
        list_group = QGroupBox("Selected DXF Files (Click X to remove)")
        list_layout = QVBoxLayout()
        
        # Scroll area for file items
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(200)
        scroll.setMaximumHeight(300)
        
        self.file_list_widget = QWidget()
        self.file_list_layout = QVBoxLayout(self.file_list_widget)
        self.file_list_layout.setSpacing(5)
        self.file_list_layout.setContentsMargins(5, 5, 5, 5)
        self.file_list_layout.addStretch()
        
        scroll.setWidget(self.file_list_widget)
        list_layout.addWidget(scroll)
        
        # File count label
        self.file_count_label = QLabel("No files selected")
        self.file_count_label.setStyleSheet(f"color:{ThemeColors.get('text_muted')}; font-size:10px; padding:5px;")
        list_layout.addWidget(self.file_count_label)
        
        list_group.setLayout(list_layout)
        layout.addWidget(list_group)

        # Action buttons
        button_row = QHBoxLayout()
        
        clear_btn = QPushButton("Clear All")
        clear_btn.setObjectName("dangerBtn")
        clear_btn.setAutoDefault(False)
        clear_btn.setDefault(False)
        clear_btn.setFocusPolicy(Qt.NoFocus)
        clear_btn.clicked.connect(self.clear_all_files)
        button_row.addWidget(clear_btn)
        
        self.hide_labels_btn = QPushButton("Hide Labels")
        self.hide_labels_btn.setObjectName("secondaryBtn")
        self.hide_labels_btn.setAutoDefault(False)
        self.hide_labels_btn.setDefault(False)
        self.hide_labels_btn.setFocusPolicy(Qt.NoFocus)
        self.hide_labels_btn.clicked.connect(self._toggle_all_labels)
        button_row.addWidget(self.hide_labels_btn)
        
        button_row.addStretch()

        attach_btn = QPushButton("Attach All DXF Files")
        attach_btn.setObjectName("primaryBtn")
        attach_btn.setAutoDefault(False)
        attach_btn.setDefault(False)
        attach_btn.setFocusPolicy(Qt.NoFocus)
        attach_btn.clicked.connect(self.attach_all_dxf)
        button_row.addWidget(attach_btn)
        
        layout.addLayout(button_row)
        self.refresh_theme()

    def refresh_theme(self):
        """Re-apply styles to the entire dialog and all items."""
        self.setStyleSheet(get_dialog_stylesheet())
        self.title_label.setStyleSheet(get_title_banner_style())
        self.info_label.setStyleSheet(get_notice_banner_style("info"))
        _browse_style = f"""
            QPushButton {{
                background: {ThemeColors.get('bg_secondary')};
                color: {ThemeColors.get('accent')};
                border: 1px solid {ThemeColors.get('border_light')};
                border-radius: 8px;
                font-size: 13px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background: {ThemeColors.get('bg_input')};
                border: 1px solid {ThemeColors.get('accent')};
            }}
        """
        self.select_btn.setStyleSheet(_browse_style)
        self._refresh_hide_labels_button()
        for item in self.dxf_items:
            item.refresh_theme()

    def _toggle_all_labels(self):
        """
        Toggle all text/label actors across attached DXF files.
        Uses actor-level detection so labels are hidden even on mixed layers.
        """
        self._labels_hidden = not self._labels_hidden
        self._refresh_hide_labels_button()

        for item in self.dxf_items:
            is_checked = item.is_checked()
            for layer_name, actors in _normalize_dxf_actor_cache(item, attachment).items():
                layer_ok = (item.selected_layers is None or layer_name in item.selected_layers)
                for actor in actors:
                    if _is_label_actor(actor):
                        if self._labels_hidden:
                            actor.SetVisibility(0)
                        else:
                            actor.SetVisibility(1 if (is_checked and layer_ok) else 0)

        for store in ['snt_actors', 'dxf_actors']:
            for dxf_data in getattr(self.app, store, []):
                for actor in dxf_data.get("actors", []):
                    if _is_label_actor(actor) and self._labels_hidden:
                        actor.SetVisibility(0)

        try:
            rw = self.app.vtk_widget.GetRenderWindow()
            if rw:
                rw.Render()
                return
        except Exception:
            pass
        try:
            self.app.vtk_widget.render()
        except Exception:
            pass

    def _refresh_hide_labels_button(self):
        accent = ThemeColors.get('accent')
        if getattr(self, '_labels_hidden', False):
            self.hide_labels_btn.setText("Show Labels")
            self.hide_labels_btn.setStyleSheet(f"""
                QPushButton {{
                    background: {accent}22;
                    color: {accent};
                    border: 1px solid {accent}88;
                    border-radius: 8px;
                    font-weight: 700;
                }}
                QPushButton:hover {{
                    background: {accent}33;
                    border: 1px solid {accent};
                }}
            """)
        else:
            self.hide_labels_btn.setText("Hide Labels")
            self.hide_labels_btn.setStyleSheet("")

    def changeEvent(self, event):
        """Detect theme property changes and refresh."""
        if event.type() == QEvent.DynamicPropertyChange:
            if event.propertyName() == "themeStyledDialog":
                self.refresh_theme()
        QDialog.changeEvent(self, event)
   
    def select_dxf_files(self):
        """Open file dialog to select multiple DXF files"""
        if not DEPENDENCIES_AVAILABLE:
            QMessageBox.critical(
                self,
                "Missing Dependencies",
                "Required libraries not installed:\n\n"
                "pip install ezdxf pyproj"
            )
            return
        
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Select DXF Files",
            "",
            "DXF Files (*.dxf);;All Files (*)"
        )
        
        if not file_paths:
            return
        
        from PySide6.QtWidgets import QProgressDialog
        from PySide6.QtCore import QCoreApplication
        
        total_files = len(file_paths)
        
        # Create progress dialog
        if total_files == 1:
            progress = QProgressDialog(
                "Loading DXF file...",
                "Cancel",
                0,
                0,  # ✅ 0-0 range makes it indeterminate (pulsing)
                self
            )
        else:
            progress = QProgressDialog(
                "Loading DXF files...",
                "Cancel",
                0,
                total_files,
                self
            )
        
        progress.setWindowTitle("Loading DXF Files")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.setStyleSheet(get_progress_dialog_stylesheet())
        
        # Create worker thread
        self.load_worker = DXFLoadWorker(file_paths)
        worker = self.load_worker
        
        # Connect signals
        def on_progress(value, message, is_indeterminate):
            progress.setLabelText(message)
            if not is_indeterminate:
                progress.setValue(value)
        
        def on_file_loaded(item_data, _):
            # Create UI item in main thread
            dxf_path = item_data['dxf_path']
            prj_exists = item_data['prj_exists']
            
            # Check if already added
            for item in self.dxf_items:
                if item.dxf_path == dxf_path:
                    return
            
            # Create item widget
            item = DXFFileItem(dxf_path, prj_exists)
            item.remove_requested.connect(self.remove_dxf_file)
            
            # Add to layout
            self.file_list_layout.insertWidget(len(self.dxf_items), item)
            self.dxf_items.append(item)
            
            # Set loaded data
            if 'dxf_doc' in item_data:
                item.cached_dxf_doc = item_data['dxf_doc']
                item.update_entity_count(item_data['entity_count'])
                if item_data.get('crs'):
                    item.set_crs(item_data['crs'])
                print(f"✅ Added: {dxf_path.name} (PRJ: {prj_exists})")
            elif 'error' in item_data:
                item.count_label.setText("Error")
                item.count_label.setStyleSheet(f"color:{ThemeColors.get('danger')}; font-size:9px;")
        
        def on_finished():
            if self.load_worker is worker:
                self.load_worker = None
            if total_files > 1:
                progress.setValue(total_files)
            progress.close()
            self.update_file_count()
            print(f"✅ All {total_files} file(s) loaded")
            if self._close_requested_while_loading:
                self._close_requested_while_loading = False
                QTimer.singleShot(0, self.close)
        
        def on_error(error_msg):
            if self.load_worker is worker:
                self.load_worker = None
            progress.close()
            QMessageBox.critical(self, "Loading Failed", error_msg)
            if self._close_requested_while_loading:
                self._close_requested_while_loading = False
                QTimer.singleShot(0, self.close)
        
        def on_canceled():
            if worker is not None and worker.isRunning():
                worker.cancel()
                worker.wait(1000)
                if worker.isRunning():
                    progress.setLabelText("Cancelling DXF load...")
                    progress.setCancelButton(None)
                    print("⏳ Waiting for DXF load worker to stop cooperatively")
                print("❌ Loading canceled by user")
        
        worker.progress.connect(on_progress)
        worker.file_loaded.connect(on_file_loaded)
        worker.finished.connect(on_finished)
        worker.error.connect(on_error)
        progress.canceled.connect(on_canceled)
        
        # Show dialog and start worker
        progress.show()
        QCoreApplication.processEvents()
        worker.start()

    def closeEvent(self, event):
        worker = getattr(self, "load_worker", None)
        if worker is not None and worker.isRunning():
            self._close_requested_while_loading = True
            worker.cancel()
            worker.wait(3000)
            if worker.isRunning():
                print("⏳ DXF dialog close deferred until load worker stops")
                event.ignore()
                return
            self.load_worker = None
        super().closeEvent(event)
    
    
    def add_dxf_file(self, file_path, progress_callback=None):
        """Add a DXF file to the list"""
        dxf_path = Path(file_path)
        
        # Check if already added
        for item in self.dxf_items:
            if item.dxf_path == dxf_path:
                QMessageBox.information(self, "Already Added", f"{dxf_path.name} is already in the list")
                return
        
        # ❌ REMOVE THESE TWO LINES:
        
        # ❌ REMOVE THIS LINE:
        
        # Check for PRJ file
        prj_path = dxf_path.with_suffix('.prj')
        if not prj_path.exists():
            prj_path = dxf_path.with_suffix('.PRJ')
        prj_exists = prj_path.exists()
        
        # Create item widget
        item = DXFFileItem(dxf_path, prj_exists)
        item.remove_requested.connect(self.remove_dxf_file)
        
        # Add to layout (before the stretch)
        self.file_list_layout.insertWidget(len(self.dxf_items), item)
        self.dxf_items.append(item)
        
        # ❌ REMOVE THESE TWO LINES:
        
        # Load DXF to get entity count and CRS
        self.load_dxf_info(item, prj_path if prj_exists else None)
        
        # ❌ REMOVE THIS LINE:
        
        print(f"✅ Added: {dxf_path.name} (PRJ: {prj_exists})")
        
    
    def load_dxf_info(self, item, prj_path):
        """
        ✅ Load DXF info synchronously with UI updates
        """
        try:
            from PySide6.QtCore import QCoreApplication
            
            # ✅ FORCE UI UPDATE BEFORE STARTING
            QCoreApplication.processEvents()
            
            # Show loading indicator on item
            item.count_label.setText("Loading...")
            item.count_label.setStyleSheet(f"color:{ThemeColors.get('warning')}; font-size:9px;")
            
            # ✅ FORCE UI UPDATE TO SHOW "Loading..."
            QCoreApplication.processEvents()
            
            # Load DXF - THIS IS THE BLOCKING OPERATION
            dxf_doc = ezdxf.readfile(str(item.dxf_path))
            
            # ✅ FORCE UI UPDATE AFTER LOADING FILE
            QCoreApplication.processEvents()
            
            # Count entities
            modelspace = dxf_doc.modelspace()
            entity_count = len(list(modelspace))
            
            # ✅ FORCE UI UPDATE AFTER COUNTING
            QCoreApplication.processEvents()
            
            # ✅ Cache the document for later use
            item.cached_dxf_doc = dxf_doc
            item.update_entity_count(entity_count)
            
            # ✅ FORCE UI UPDATE AFTER CACHING
            QCoreApplication.processEvents()
            
            # Parse PRJ if exists
            if prj_path:
                try:
                    with open(prj_path, 'r') as f:
                        prj_content = f.read().strip()
                    crs = CRS.from_wkt(prj_content)
                    item.set_crs(crs)
                    print(f"  ✅ CRS: {crs.name}")
                    try:
                        from gui.crs_manager import ensure_canvas_crs, get_canvas_crs, log_dataset_crs
                        ensure_canvas_crs(self.app, crs, source="DXF adjacent WKT .prj",
                                          dataset=str(item.dxf_path))
                        log_dataset_crs(item.dxf_path.name, "DXF", crs, "DXF adjacent WKT .prj",
                                        canvas_crs=get_canvas_crs(self.app))
                    except Exception as ce:
                        print(f"  ⚠️ canvas CRS update failed: {ce}")
                except Exception as e:
                    print(f"  ⚠️ PRJ parse failed: {e}")
            
            # ✅ FINAL UI UPDATE
            QCoreApplication.processEvents()
            
        except Exception as e:
            print(f"  ⚠️ DXF load failed: {e}")
            item.count_label.setText("Error")
            item.count_label.setStyleSheet("color: #f44336; font-size: 9px;")
            QCoreApplication.processEvents()

    def remove_dxf_file(self, item):
        """Remove a DXF file from the list and from VTK display"""
        if item not in self.dxf_items:
            return
        try:
            filename = str(item.dxf_path.name)
            try:
                item.clear_layer_focus(render=False)
            except Exception:
                pass
            self.dxf_items.remove(item)
            self.file_list_layout.removeWidget(item)
            if hasattr(item, 'actor_cache'):
                item.actor_cache.clear()
            self.remove_dxf_from_vtk(filename)
            try:
                item.remove_requested.disconnect()
            except Exception:
                pass
            item.setParent(None)
            item.deleteLater()
            self.update_file_count()
            print(f"❌ Removed: {filename}")
        except Exception as exc:
            print(f"[warn] Error in remove_dxf_file: {exc}")
    
    def remove_dxf_from_vtk(self, filename):
        """Remove DXF/SNT actors matching filename from VTK renderer and detach from app lists."""
        import os
        try:
            target = os.path.basename(filename)

            renderer = None
            try:
                if (
                    hasattr(self.app, "vtk_widget")
                    and self.app.vtk_widget is not None
                    and hasattr(self.app.vtk_widget, "renderer")
                ):
                    renderer = self.app.vtk_widget.renderer
            except Exception:
                renderer = None

            # 1) Remove real SNT/DXF actors matching the removed DXF filename
            for store_name in ["snt_actors", "dxf_actors"]:
                if not hasattr(self.app, store_name):
                    continue

                store = getattr(self.app, store_name, [])
                indices_to_remove = []

                for i, data in enumerate(store):
                    if os.path.basename(data.get("filename", "")) == target:
                        indices_to_remove.append(i)

                        if renderer is not None:
                            for actor in data.get("actors", []):
                                try:
                                    if actor is not None:
                                        renderer.RemoveActor(actor)
                                except Exception:
                                    pass

                for i in reversed(indices_to_remove):
                    try:
                        store.pop(i)
                    except IndexError:
                        pass

            # 2) Remove DXF/SNT attachment metadata
            for list_name in ["snt_attachments", "dxf_attachments"]:
                if hasattr(self.app, list_name):
                    try:
                        current_list = getattr(self.app, list_name)
                        setattr(
                            self.app,
                            list_name,
                            [
                                a for a in current_list
                                if os.path.basename(a.get("filename", "")) != target
                            ],
                        )
                    except Exception:
                        pass

            # 3) Invalidate unified actor cache and refresh display
            try:
                from gui.unified_actor_manager import invalidate_unified_actor, sync_palette_to_gpu
                invalidate_unified_actor(self.app)
                sync_palette_to_gpu(self.app, 0, render=False)
            except Exception:
                pass

            # 4) Update point count
            if hasattr(self.app, "update_total_points_label"):
                self.app.update_total_points_label()

            # 5) Disable SNT/DXF layer pick tool if no SNT or DXF remains
            try:
                has_any_data = bool(getattr(self.app, "snt_attachments", []) or getattr(self.app, "dxf_attachments", []))
                if not has_any_data:
                    tool = getattr(self.app, "snt_layer_pick_tool", None)
                    if tool is not None and getattr(tool, "active", False):
                        tool.deactivate()

                    if hasattr(self.app, "_set_snt_layer_pick_footer_text"):
                        self.app._set_snt_layer_pick_footer_text(None, None, None)
            except Exception:
                pass

            # 6) Render once at end
            if renderer is not None:
                try:
                    rw = self.app.vtk_widget.GetRenderWindow()
                    if rw is not None:
                        rw.Render()
                except Exception:
                    pass
            try:
                from gui.crs_manager import reconcile_canvas_crs_after_content_change
                reconcile_canvas_crs_after_content_change(
                    self.app, reason="DXF attachment removed"
                )
            except Exception as exc:
                print(f"[CRS] reconciliation after DXF removal failed: {exc}")

        except Exception as e:
            print(f"  ⚠️ Failed to remove DXF '{filename}' from VTK: {e}")
            
            
    def refresh_dxf_display(self, item):
        """Refresh DXF display after layer selection changes"""
        try:
            print(f"\n🔄 Refreshing display for '{item.dxf_path.name}'...")
            
            # 1) Remove old actors from renderer
            self.remove_dxf_from_vtk(item.dxf_path.name)
            
            # 2) Clear cached entities so it will reprocess with new layers
            item.cached_entities = None
            
            # 3) Reprocess the DXF with new layer filter
            attachment_data = self.process_dxf_file(item)
            
            if not attachment_data:
                print(f"  ⚠️ No entities after filtering")
                return
            
            # 4) Update in app.dxf_attachments
            if hasattr(self.app, 'dxf_attachments'):
                # Remove old entry
                self.app.dxf_attachments = [
                    a for a in self.app.dxf_attachments
                    if a.get('filename') != item.dxf_path.name
                ]
                # Add new entry
                self.app.dxf_attachments.append(attachment_data)
            
            # 5) Render with new layer filter
            self.render_dxf_in_vtk(attachment_data)
            
            print(f"  ✅ Display refreshed with {len(attachment_data['entities'])} entities")
            
        except Exception as e:
            print(f"  ❌ Refresh failed: {e}")
            import traceback
            traceback.print_exc()       
        
    
    def clear_all_files(self):
        """Clear all DXF files and their cache"""
        if not self.dxf_items:
            return

        # Clear cache for all items
        for item in self.dxf_items:
            item.cached_dxf_doc = None
            item.cached_entities = None  # ✅ Clear cached entities
            item.checkbox.setChecked(False)

        self.update_file_count()
        print("🔘 All DXF files cleared (cache reset)")
    
    def update_file_count(self):
        """Update the file count label"""
        count = len(self.dxf_items)
        if count == 0:
            self.file_count_label.setText("No files selected")
            self.file_count_label.setStyleSheet(f"color:{ThemeColors.get('text_muted')}; font-size:10px; padding:5px;")
        else:
            total_entities = sum(item.entity_count for item in self.dxf_items)
            prj_count = sum(1 for item in self.dxf_items if item.prj_exists)
            self.file_count_label.setText(
                f"{count} file(s) selected | {total_entities:,} total Entities | "
                f"{prj_count} with PRJ"
            )
            self.file_count_label.setStyleSheet(f"color:{ThemeColors.get('accent')}; font-size:10px; font-weight:bold; padding:5px;")
    
    def detect_project_crs(self):
        """Refresh self.project_crs from the ONE authoritative canvas CRS
        (gui.crs_manager) so DXF entities get reprojected into whatever CRS
        SNT/LAZ/GIS already established - not whatever app.crs happened to
        hold when this dialog was opened."""
        try:
            from gui.crs_manager import get_canvas_crs
            crs = get_canvas_crs(self.app)
        except Exception:
            crs = getattr(self.app, 'crs', None)
        if crs is not None:
            self.project_crs = crs
            print(f"✅ Project CRS: {self.project_crs.name}")
        else:
            self.project_crs = None
            print("⚠️ Project CRS: Not detected")
    
    def attach_all_dxf(self):
        """
        ✅ ENHANCED: Auto-save current LAZ → Clear → Attach DXF → Load LAZ
        """
        from PySide6.QtWidgets import QProgressDialog, QMessageBox
        from PySide6.QtCore import Qt, QCoreApplication
        import os
        
        if not self.dxf_items:
            QMessageBox.warning(self, "No Files", "Please select DXF files first")
            return

        selected_items = [item for item in self.dxf_items if item.is_checked()]
        if not selected_items:
            QMessageBox.warning(
                self,
                "No Files Selected",
                "Please check at least one DXF file to attach."
            )
            return

        # ============================================================================
        # ✅ STEP 1: AUTO-SAVE CURRENT LAZ FILE (if exists)
        # ============================================================================
        print(f"\n{'='*60}")
        print(f"💾 AUTO-SAVING CURRENT FILE BEFORE DXF ATTACHMENT")
        print(f"{'='*60}")
        
        if hasattr(self.app, 'data') and self.app.data is not None:
            save_path = None
            
            # Determine save path
            if hasattr(self.app, 'last_save_path') and self.app.last_save_path:
                save_path = self.app.last_save_path
            elif hasattr(self.app, 'loaded_file') and self.app.loaded_file:
                save_path = self.app.loaded_file
            
            if save_path:
                try:
                    print(f"   Current file: {os.path.basename(save_path)}")
                    print(f"   Points: {len(self.app.data.get('xyz', [])):,}")
                    
                    from gui.save_pointcloud import save_pointcloud_quick
                    save_ok = save_pointcloud_quick(self.app, save_path)
                    if not save_ok:
                        raise RuntimeError("Quick-save returned False")
                    
                    print(f"✅ Current file saved successfully")
                    
                    if hasattr(self.app, "statusBar"):
                        self.app.statusBar().showMessage(f"💾 Saved: {os.path.basename(save_path)}", 2000)
                        QCoreApplication.processEvents()
                        
                except Exception as e:
                    print(f"⚠️ Failed to auto-save: {e}")
                    import traceback
                    traceback.print_exc()
                    
                    reply = QMessageBox.warning(
                        self,
                        "Save Failed",
                        f"Failed to auto-save current file:\n\n{e}\n\n"
                        "Continue with DXF attachment anyway?",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.No
                    )
                    if reply == QMessageBox.No:
                        return
            else:
                print("ℹ️ No save path - data won't be saved")
        else:
            print("ℹ️ No current data to save")

        # ============================================================================
        # ✅ STEP 1.5: RESOLVE MISSING CRS (QGIS-style prompt)
        # ============================================================================
        # Files without a .prj must not be silently dropped onto a georeferenced
        # canvas in their raw coordinates. Ask once for the whole batch.
        _canvas_crs = getattr(self, "project_crs", None)
        if _canvas_crs is None:
            try:
                from gui.crs_manager import get_canvas_crs
                _canvas_crs = get_canvas_crs(self.app)
            except Exception:
                _canvas_crs = None
            if _canvas_crs is not None:
                self.project_crs = _canvas_crs

        if _canvas_crs is not None:
            _no_crs = [it for it in selected_items if not it.dxf_crs]
            if _no_crs:
                try:
                    from gui.crs_selector_dialog import prompt_missing_source_crs
                    picked = prompt_missing_source_crs(
                        self,
                        f"{len(_no_crs)} DXF file(s) without a .prj",
                        "DXF",
                        _canvas_crs,
                    )
                except Exception as _pe:
                    print(f"  ⚠️ DXF CRS prompt failed: {_pe}")
                    picked = None
                if picked is None:
                    print("ℹ️ DXF attach cancelled — source CRS unresolved")
                    return
                for it in _no_crs:
                    it.set_crs(picked)
                print(f"  ✅ Assigned {picked.name} to {len(_no_crs)} DXF file(s)")

        # ============================================================================
        # ✅ STEP 2: CONFIRMATION DIALOG
        # ============================================================================
        msg = f"Attach {len(selected_items)} DXF file(s)?\n\n"
        
        if hasattr(self.app, 'data') and self.app.data is not None:
            msg += "⚠️ Current point cloud will be CLEARED\n"
            msg += "✅ You can load LAZ files after DXF attachment\n\n"
        
        if self.project_crs:
            prj_count = sum(1 for item in selected_items if item.dxf_crs)
            msg += f"📐 {prj_count} file(s) will be reprojected to project CRS\n"
            msg += f"📐 {len(selected_items) - prj_count} without PRJ will use original coordinates"

        reply = QMessageBox.question(
            self,
            "Confirm DXF Attachment",
            msg,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        # ============================================================================
        # ✅ STEP 3: CLEAR CURRENT POINT CLOUD (preserve nothing - DXF will be new)
        # ============================================================================
        print(f"\n🧹 CLEARING CURRENT PROJECT...")
        
        if hasattr(self.app, 'data') and self.app.data is not None:
            # Clear main viewer
            if hasattr(self.app, "vtk_widget") and self.app.vtk_widget:
                renderer = self.app.vtk_widget.renderer
                renderer.RemoveAllViewProps()
                
                if hasattr(self.app.vtk_widget, 'actors'):
                    self.app.vtk_widget.actors.clear()
                if hasattr(self.app.vtk_widget, '_actors'):
                    self.app.vtk_widget._actors.clear()
                
                self.app.vtk_widget.render()
                print(f"✅ Main viewer cleared")
            
            # Clear cross-section views
            if hasattr(self.app, 'section_vtks') and self.app.section_vtks:
                for view_idx, vtk_widget in self.app.section_vtks.items():
                    try:
                        vtk_widget.renderer.RemoveAllViewProps()
                        if hasattr(vtk_widget, 'actors'):
                            vtk_widget.actors.clear()
                        vtk_widget.render()
                    except Exception:
                        pass
            
            # Clear internal state
            self.app.data = None
            self.app.loaded_file = None
            self.app.last_save_path = None
            self.app.class_palette = {}
            
            if hasattr(self.app, "view_palettes"):
                self.app.view_palettes.clear()
            
            # Clear DXF actors list (we're replacing them)
            if hasattr(self.app, 'dxf_actors'):
                self.app.dxf_actors.clear()
            
            if hasattr(self.app, 'dxf_attachments'):
                self.app.dxf_attachments.clear()
            
            print(f"✅ Project cleared\n")
            
            QCoreApplication.processEvents()

        # ============================================================================
        # ✅ STEP 4: ATTACH DXF FILES (process and render)
        # ============================================================================
        print(f"📎 ATTACHING {len(selected_items)} DXF FILE(S)...")
        
        # Show progress dialog
        progress = QProgressDialog(
            "Processing DXF files...", 
            "Cancel", 
            0, 
            len(selected_items) * 2,
            self
        )
        progress.setWindowTitle("Attaching DXF Files")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.setAutoClose(True)
        progress.setAutoReset(True)
        progress.setStyleSheet(get_progress_dialog_stylesheet())
        progress.show()
        progress.forceShow()
        QCoreApplication.processEvents()
        
        try:
            all_attachments = []
            
            # Process all files
            for idx, item in enumerate(selected_items):
                if progress.wasCanceled():
                    return
                
                progress.setLabelText(f"Processing {item.dxf_path.name}...")
                progress.setValue(idx)
                QCoreApplication.processEvents()
                
                attachment_data = self.process_dxf_file(item)
                if attachment_data is not None:
                    # ✅ Store reference to item for actor caching
                    attachment_data['_dxf_item'] = item
                    all_attachments.append(attachment_data)
                else:
                    print(f"  ⚠️ Failed to process {item.dxf_path.name}")

            if not all_attachments:
                progress.close()
                QMessageBox.information(
                    self,
                    "No Files Processed",
                    "Could not process any of the selected DXF files."
                )
                return

            # Initialize storage
            if not hasattr(self.app, 'dxf_attachments'):
                self.app.dxf_attachments = []
            if not hasattr(self.app, 'dxf_actors'):
                self.app.dxf_actors = []

            self.app.dxf_attachments.extend(all_attachments)
            self.dxf_attached.emit(all_attachments)

            # Render all files
            for idx, attachment_data in enumerate(all_attachments):
                if progress.wasCanceled():
                    return
                
                progress.setLabelText(f"Rendering {attachment_data['filename']}...")
                progress.setValue(len(selected_items) + idx)
                QCoreApplication.processEvents()
                
                self.render_dxf_in_vtk(attachment_data)

            progress.setValue(len(selected_items) * 2)
            progress.close()

            total_entities = sum(len(a['entities']) for a in all_attachments)
            files_with_geometry = sum(1 for a in all_attachments if len(a['entities']) > 0)
            files_without_geometry = len(all_attachments) - files_with_geometry

            msg = f"✅ Successfully attached {len(all_attachments)} DXF file(s)\n"
            msg += f"📊 Total: {total_entities} entities\n"
            if files_without_geometry > 0:
                msg += f"⚠️ {files_without_geometry} file(s) have no visible geometry\n"
            msg += f"\n💡 You can now load LAZ files for the grids"

            QMessageBox.information(self, "DXF Attached", msg)
            print(f"✅ Attached {len(all_attachments)} DXF files: {total_entities} entities")
            
            # Update window title
            self.app._update_window_title(f"DXF Grid ({len(all_attachments)} files)", None)

        except Exception as e:
            progress.close()
            QMessageBox.critical(
                self,
                "Attachment Failed",
                f"Failed to attach DXF files:\n{str(e)}"
            )
            import traceback
            traceback.print_exc()

    def analyze_dxf_layers(self, dxf_path, dxf_doc):
        """Detailed analysis of what's in each layer"""
        print(f"\n🔬 DETAILED LAYER ANALYSIS: {dxf_path.name}")
        print("=" * 80)
        
        modelspace = dxf_doc.modelspace()
        
        # Collect stats by layer
        layer_data = {}
        
        for entity in modelspace:
            entity_type = entity.dxftype()
            layer = entity.dxf.layer if hasattr(entity.dxf, 'layer') else '0'
            
            if layer not in layer_data:
                layer_data[layer] = {}
            
            if entity_type not in layer_data[layer]:
                layer_data[layer][entity_type] = 0
            
            layer_data[layer][entity_type] += 1
        
        # Print layer breakdown
        for layer in sorted(layer_data.keys()):
            print(f"\n📁 Layer: '{layer}'")
            for entity_type in sorted(layer_data[layer].keys()):
                count = layer_data[layer][entity_type]
                print(f"   {entity_type:15s}: {count:5d}")
        
        print("=" * 80)
        
        
    def debug_insert_extraction(self, dxf_path, dxf_doc):
        """Debug: Show how many labels are being extracted from INSERT blocks"""
        print(f"\n{'='*80}")
        print(f"🔬 INSERT BLOCK EXTRACTION DEBUG")
        print(f"{'='*80}\n")
        
        modelspace = dxf_doc.modelspace()
        inserts = [e for e in modelspace if e.dxftype() == 'INSERT']
        
        print(f"Total INSERT blocks: {len(inserts)}")
        
        # Count extraction success
        labels_found = 0
        labels_failed = 0
        extraction_methods = {}
        
        for entity in inserts[:100]:  # Check first 100
            # Simulate extraction logic
            text_label = None
            method = "NONE"
            
            if hasattr(entity, 'attribs'):
                for idx, attrib in enumerate(entity.attribs):
                    if not hasattr(attrib.dxf, 'text'):
                        continue
                    
                    tag = attrib.dxf.tag if hasattr(attrib.dxf, 'tag') else 'NO_TAG'
                    attr_text = str(attrib.dxf.text).strip()
                    
                    skip_tags = {'LAYER', 'COLOR', 'LINETYPE', 'STYLE', 'LTYPE', 'WEIGHT'}
                    if tag.upper() in skip_tags or attr_text == 'FEATURE':
                        continue
                    
                    if attr_text and len(attr_text) >= 5:
                        if any(c.isdigit() for c in attr_text) or '_' in attr_text:
                            text_label = attr_text
                            method = f"ATTRIB_{tag}"
                            break
            
            if not text_label:
                block_name = entity.dxf.name
                if any(c.isdigit() for c in block_name) and len(block_name) >= 5:
                    text_label = block_name
                    method = "BLOCK_NAME"
            
            if text_label:
                labels_found += 1
                extraction_methods[method] = extraction_methods.get(method, 0) + 1
            else:
                labels_failed += 1
        
        print(f"\nSampled 100 blocks:")
        print(f"  ✅ Labels extracted: {labels_found}")
        print(f"  ❌ Labels failed: {labels_failed}")
        print(f"  Success rate: {labels_found/100*100:.1f}%")
        
        print(f"\nExtraction methods:")
        for method, count in sorted(extraction_methods.items(), key=lambda x: -x[1]):
            print(f"  {method}: {count}")
        
        # Extrapolate
        estimated_total = int((labels_found / 100) * len(inserts))
        print(f"\nEstimated total extractable labels: {estimated_total} / {len(inserts)}")
        print(f"{'='*80}\n")

    def process_dxf_file(self, item):
        """
        ✅ OPTIMIZED: Re-use cached DXF document
        Process geometry only once during attachment
        """
        try:
            # Refresh from the authoritative canvas CRS right before use (not
            # just once when the dialog opened) - SNT/LAZ/GIS loaded after
            # this dialog was constructed must still be reprojected against.
            self.detect_project_crs()
            # ✅ Check if we already processed this file
            if item.cached_entities:
                print(f"  ⚡ Using cached entities for {item.dxf_path.name}")
                cached_bounds = self._compute_2d_bounds(item.cached_entities)
                layer_stats = _build_dxf_layer_stats_from_entities(item.cached_entities)
                parsed = {
                    "layers": [{"name": n, "color": c} for n, _cnt, c in layer_stats],
                    "entities": item.cached_entities,
                }
                return {
                    'filename': item.dxf_path.name,
                    'full_path': str(item.dxf_path.resolve()),
                    'mode': item.display_mode,
                    'entities': item.cached_entities,
                    'parsed': parsed,
                    'layer_stats': layer_stats,
                    'actor_cache_map': item.actor_cache,
                    'bounds': cached_bounds,
                    'dxf_crs': item.dxf_crs.to_wkt() if item.dxf_crs else None,
                    'project_crs': self.project_crs.to_wkt() if self.project_crs else None,
                    'transformed': item.dxf_crs is not None and self.project_crs is not None
                }
            
            # ✅ Use cached document if available
            if item.cached_dxf_doc:
                dxf_doc = item.cached_dxf_doc
            else:
                dxf_doc = ezdxf.readfile(str(item.dxf_path))
                item.cached_dxf_doc = dxf_doc
            modelspace = dxf_doc.modelspace()

            offset_x = 0.0
            offset_y = 0.0
            offset_z = 0.0

            color_override = None
            if getattr(item, "override_enabled", False):
                color_override = getattr(item, "override_color", (255, 0, 0))

            display_mode = getattr(item, "display_mode", "overlay")

            # Setup transformer if CRS available
            transformer = None
            if item.dxf_crs and self.project_crs:
                try:
                    transformer = Transformer.from_crs(
                        item.dxf_crs,
                        self.project_crs,
                        always_xy=True
                    )
                    print(f"  ✅ Transformer: {item.dxf_crs.name} → {self.project_crs.name}")
                except Exception as e:
                    print(f"  ⚠️ Transformer failed: {e}")

            processed_entities = []
            entity_type_counts = {}
            processed_type_counts = {}
            skipped_by_layer = 0
            failed_processing = 0

            # ✅ PRODUCTION: Direct iteration (no list(modelspace) copy)
            import time as _time
            t0 = _time.perf_counter()
            total = 0
            
            for entity in modelspace:
                total += 1
                entity_type = entity.dxftype()
                entity_type_counts[entity_type] = entity_type_counts.get(entity_type, 0) + 1
                
                # Layer filter
                if item.selected_layers is not None:
                    entity_layer = entity.dxf.layer if hasattr(entity.dxf, 'layer') else '0'
                    if len(item.selected_layers) > 0 and entity_layer not in item.selected_layers:
                        skipped_by_layer += 1
                        continue
                
                entity_data = self.process_entity(
                    entity, transformer, offset_x, offset_y, offset_z, color_override, dxf_doc
                )
                
                if entity_data:
                    if isinstance(entity_data, list):
                        processed_entities.extend(entity_data)
                        processed_type_counts[entity_type] = processed_type_counts.get(entity_type, 0) + len(entity_data)
                    else:
                        processed_entities.append(entity_data)
                        processed_type_counts[entity_type] = processed_type_counts.get(entity_type, 0) + 1
                else:
                    failed_processing += 1
            
            parse_ms = (_time.perf_counter() - t0) * 1000
            print(f"  ⚡ Parsed {total} entities → {len(processed_entities)} in {parse_ms:.0f}ms")

            # ✅ Cache processed entities for future use
            validated_entities = []
            skipped_empty_text = 0

            for entity in processed_entities:
                if entity['type'] == 'text':
                    # Validate text content
                    text = entity.get('text', '')
                    if not text or not isinstance(text, str) or len(str(text).strip()) == 0:
                        skipped_empty_text += 1
                        continue  # Skip this entity
                
                validated_entities.append(entity)

            if skipped_empty_text > 0:
                print(f"  🧹 Filtered out {skipped_empty_text} empty text entities")

            # ✅ Cache validated entities
            item.cache_dxf_data(dxf_doc, validated_entities)
            entity_bounds = self._compute_2d_bounds(validated_entities)

            layer_stats = _build_dxf_layer_stats_from_entities(validated_entities)
            parsed = {
                "layers": [{"name": n, "color": c} for n, _cnt, c in layer_stats],
                "entities": validated_entities,
            }

            return {
                'filename': item.dxf_path.name,
                'full_path': str(item.dxf_path.resolve()),
                'mode': display_mode,
                'entities': validated_entities,
                'parsed': parsed,
                'layer_stats': layer_stats,
                'actor_cache_map': item.actor_cache,
                'bounds': entity_bounds,
                'dxf_crs': item.dxf_crs.to_wkt() if item.dxf_crs else None,
                'project_crs': self.project_crs.to_wkt() if self.project_crs else None,
                'transformed': transformer is not None
            }

        except Exception as e:
            print(f"⚠️ Failed to process {item.dxf_path.name}: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _compute_2d_bounds(self, entities):
        """
        Compute stable XY bounds from processed entities.
        Geometry is prioritized; text positions are used only as fallback.
        """
        if not entities:
            return None

        geom_min_x = float("inf")
        geom_min_y = float("inf")
        geom_max_x = float("-inf")
        geom_max_y = float("-inf")
        has_geom = False

        text_min_x = float("inf")
        text_min_y = float("inf")
        text_max_x = float("-inf")
        text_max_y = float("-inf")
        has_text = False

        def _update_xy(x_val, y_val, use_text_bucket=False):
            nonlocal geom_min_x, geom_min_y, geom_max_x, geom_max_y, has_geom
            nonlocal text_min_x, text_min_y, text_max_x, text_max_y, has_text
            try:
                x = float(x_val)
                y = float(y_val)
            except Exception:
                return
            if use_text_bucket:
                if x < text_min_x:
                    text_min_x = x
                if y < text_min_y:
                    text_min_y = y
                if x > text_max_x:
                    text_max_x = x
                if y > text_max_y:
                    text_max_y = y
                has_text = True
                return
            if x < geom_min_x:
                geom_min_x = x
            if y < geom_min_y:
                geom_min_y = y
            if x > geom_max_x:
                geom_max_x = x
            if y > geom_max_y:
                geom_max_y = y
            has_geom = True

        for ent in entities:
            etype = str(ent.get("type", "")).lower()
            if etype == "line":
                start = ent.get("start", [])
                end = ent.get("end", [])
                if len(start) >= 2:
                    _update_xy(start[0], start[1], use_text_bucket=False)
                if len(end) >= 2:
                    _update_xy(end[0], end[1], use_text_bucket=False)
            elif etype in ("polyline", "lwpolyline"):
                pts = ent.get("points") or ent.get("vertices") or []
                for pt in pts:
                    if len(pt) >= 2:
                        _update_xy(pt[0], pt[1], use_text_bucket=False)
            elif etype == "3dface":
                for pt in ent.get("vertices", []) or []:
                    if len(pt) >= 2:
                        _update_xy(pt[0], pt[1], use_text_bucket=False)
            elif etype == "point":
                pos = ent.get("position", [])
                if len(pos) >= 2:
                    _update_xy(pos[0], pos[1], use_text_bucket=False)
            elif etype in ("circle", "arc"):
                center = ent.get("center", [])
                radius = ent.get("radius", 0.0)
                if len(center) >= 2:
                    try:
                        r = abs(float(radius))
                    except Exception:
                        r = 0.0
                    cx = float(center[0])
                    cy = float(center[1])
                    _update_xy(cx - r, cy - r, use_text_bucket=False)
                    _update_xy(cx + r, cy + r, use_text_bucket=False)
            elif etype == "text":
                pos = ent.get("position", [])
                if len(pos) >= 2:
                    _update_xy(pos[0], pos[1], use_text_bucket=True)

        if has_geom:
            return (geom_min_x, geom_max_x, geom_min_y, geom_max_y)
        if has_text:
            return (text_min_x, text_max_x, text_min_y, text_max_y)
        return None


    
    def transform_block_point(self, point, insert_point, rotation, scale_x, scale_y, scale_z):
        """
        Transform a point from block coordinates to world coordinates
        
        Args:
            point: Point in block coordinate system [x, y, z]
            insert_point: Block insertion point [x, y, z]
            rotation: Rotation angle in degrees
            scale_x, scale_y, scale_z: Scale factors
        """
        import math
        
        pt = np.array(point)
        
        # Apply scale
        pt[0] *= scale_x
        pt[1] *= scale_y
        if len(pt) > 2:
            pt[2] *= scale_z
        
        # Apply rotation (around Z-axis)
        if rotation != 0:
            rad = math.radians(rotation)
            cos_r = math.cos(rad)
            sin_r = math.sin(rad)
            
            x_rot = pt[0] * cos_r - pt[1] * sin_r
            y_rot = pt[0] * sin_r + pt[1] * cos_r
            
            pt[0] = x_rot
            pt[1] = y_rot
        
        # Apply translation (insert point)
        pt[0] += insert_point[0]
        pt[1] += insert_point[1]
        if len(pt) > 2:
            pt[2] += insert_point[2]
        
        return pt
    
    
    def create_3dface_actor(self, entity):
        """
        Create VTK actor for 3DFACE (grid squares/polygons)
        Renders as crisp wireframe outline
        """
        import vtk
        
        points = vtk.vtkPoints()
        
        # Add all vertices
        for vertex in entity['vertices']:
            points.InsertNextPoint(vertex)
        
        # Create polygon or triangle
        if entity['is_triangle']:
            polygon = vtk.vtkTriangle()
            polygon.GetPointIds().SetId(0, 0)
            polygon.GetPointIds().SetId(1, 1)
            polygon.GetPointIds().SetId(2, 2)
        else:
            polygon = vtk.vtkQuad()
            polygon.GetPointIds().SetId(0, 0)
            polygon.GetPointIds().SetId(1, 1)
            polygon.GetPointIds().SetId(2, 2)
            polygon.GetPointIds().SetId(3, 3)
        
        polygons = vtk.vtkCellArray()
        polygons.InsertNextCell(polygon)
        
        polydata = vtk.vtkPolyData()
        polydata.SetPoints(points)
        polydata.SetPolys(polygons)
        
        # ✅ Extract edges for wireframe rendering
        edges = vtk.vtkExtractEdges()
        edges.SetInputData(polydata)
        edges.Update()
        
        # Create mapper
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(edges.GetOutputPort())
        
        # ✅ CRITICAL: Proper Z-fighting prevention
        mapper.SetResolveCoincidentTopologyToPolygonOffset()
        mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(0, -1)  # Changed from -2, -2
        
        # Create actor
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        
        # ✅ Set color
        actor.GetProperty().SetColor([c/255.0 for c in entity['color']])
        
        # ✅ ENHANCED: Better line visibility
        actor.GetProperty().SetLineWidth(2.0)  # Reduced from 3.0 for cleaner look
        actor.GetProperty().SetOpacity(1.0)
        
        # ✅ Lighting for better visibility
        actor.GetProperty().SetAmbient(0.5)  # Increased from 0.3
        actor.GetProperty().SetDiffuse(0.8)  # Increased from 0.7
        actor.GetProperty().SetSpecular(0.3)  # Add slight specular highlight
        
        # ✅ Ensure crisp rendering
        actor.GetProperty().SetRenderLinesAsTubes(False)  # Changed from True for sharper lines
        actor.GetProperty().EdgeVisibilityOn()
        
        return actor
   

    def process_entity(self, entity, transformer, offset_x, offset_y, offset_z, color_override, dxf_doc=None):
        """
        ✅ FIXED: Distinguishes between grid labels and feature labels by pattern
        """
        entity_data = None

        try:
            # === LINE ENTITY ===
            if entity.dxftype() == 'LINE':
                start = np.array(entity.dxf.start)
                end = np.array(entity.dxf.end)
                
                if transformer:
                    start[:2] = transformer.transform(start[0], start[1])
                    end[:2] = transformer.transform(end[0], end[1])
                
                start[0] += offset_x
                start[1] += offset_y
                if len(start) > 2:
                    start[2] += offset_z
                end[0] += offset_x
                end[1] += offset_y
                if len(end) > 2:
                    end[2] += offset_z
                
                entity_data = {
                    'type': 'line',
                    'start': start[:3].tolist(),
                    'end': end[:3].tolist(),
                    'color': color_override or self.get_entity_color(entity, dxf_doc),
                    'layer': getattr(entity.dxf, 'layer', '0')
                }
            
            # === INSERT ENTITY (BLOCKS) ===
            elif entity.dxftype() == 'INSERT':
                if not dxf_doc:
                    print(f"  ⚠️ Cannot process INSERT without dxf_doc")
                    return None
                    
                try:
                    insert_point = np.array(entity.dxf.insert)
                    block_name = entity.dxf.name
                    
                    # ✅ Extract text label
                    # ✅ FIX: Extract text label with PRIORITY to grid-like patterns
                    text_label = None
                    extraction_method = "NONE"
                    candidate_labels = []  # ✅ Collect all candidates

                    if hasattr(entity, 'attribs'):
                        for idx, attrib in enumerate(entity.attribs):
                            if not hasattr(attrib.dxf, 'text'):
                                continue
                                
                            tag = attrib.dxf.tag if hasattr(attrib.dxf, 'tag') else 'NO_TAG'
                            attr_text = str(attrib.dxf.text).strip()
                            
                            # Skip metadata
                            skip_tags = {'LAYER', 'COLOR', 'LINETYPE', 'STYLE', 'LTYPE', 'WEIGHT'}
                            if tag.upper() in skip_tags or attr_text == 'FEATURE':
                                continue
                            
                            # Accept if looks like a label
                            if attr_text and len(attr_text) >= 5:
                                if any(char.isdigit() for char in attr_text) or '_' in attr_text:
                                    # ✅ Store candidate with priority score
                                    priority = 0
                                    
                                    # ✅ HIGH PRIORITY: Grid pattern like "DW2039017_000347"
                                    if attr_text.startswith('DW') and '_' in attr_text:
                                        priority = 100
                                    # ✅ MEDIUM PRIORITY: Contains underscore and digits
                                    elif '_' in attr_text and sum(c.isdigit() for c in attr_text) >= 3:
                                        priority = 50
                                    # ✅ LOW PRIORITY: Just has digits
                                    else:
                                        priority = 10
                                    
                                    candidate_labels.append({
                                        'text': attr_text,
                                        'method': f"ATTRIB[{idx}]:{tag}",
                                        'priority': priority
                                    })

                    # Select BEST candidate (highest priority)
                    if candidate_labels:
                        best = max(candidate_labels, key=lambda x: x['priority'])
                        text_label = best['text']
                        extraction_method = best['method']

                    # Fallback to block name
                    if not text_label and block_name:
                        if any(char.isdigit() for char in block_name) and len(block_name) >= 5:
                            text_label = block_name
                            extraction_method = "BLOCK_NAME"
                    
                    # Get transformation parameters
                    rotation = entity.dxf.rotation if hasattr(entity.dxf, 'rotation') else 0.0
                    scale_x = entity.dxf.xscale if hasattr(entity.dxf, 'xscale') else 1.0
                    scale_y = entity.dxf.yscale if hasattr(entity.dxf, 'yscale') else 1.0
                    scale_z = entity.dxf.zscale if hasattr(entity.dxf, 'zscale') else 1.0
                    
                    # Transform insert point
                    if transformer:
                        insert_point[:2] = transformer.transform(insert_point[0], insert_point[1])
                    
                    insert_point[0] += offset_x
                    insert_point[1] += offset_y
                    if len(insert_point) > 2:
                        insert_point[2] += offset_z
                    
                    # Process block geometry
                    block_entities = []
                    
                    try:
                        block_layout = dxf_doc.blocks.get(block_name)
                        
                        for block_entity in block_layout:
                            block_entity_type = block_entity.dxftype()
                            
                            if block_entity_type == 'LINE':
                                start = np.array(block_entity.dxf.start)
                                end = np.array(block_entity.dxf.end)
                                
                                start_world = self.transform_block_point(
                                    start, insert_point, rotation, scale_x, scale_y, scale_z
                                )
                                end_world = self.transform_block_point(
                                    end, insert_point, rotation, scale_x, scale_y, scale_z
                                )
                                
                                block_entities.append({
                                    'type': 'line',
                                    'start': start_world[:3].tolist(),
                                    'end': end_world[:3].tolist(),
                                    'color': color_override or self.get_entity_color(block_entity, dxf_doc),
                                     'layer': getattr(entity.dxf, 'layer', '0')
                                })
                            
                            elif block_entity_type == 'LWPOLYLINE':
                                points = []
                                for point in block_entity.get_points():
                                    pt = np.array(point)
                                    pt_world = self.transform_block_point(
                                        pt, insert_point, rotation, scale_x, scale_y, scale_z
                                    )
                                    points.append(pt_world[:3].tolist())
                                
                                if len(points) >= 2:
                                    block_entities.append({
                                        'type': 'polyline',
                                        'points': points,
                                        'closed': block_entity.is_closed if hasattr(block_entity, 'is_closed') else False,
                                        'color': color_override or self.get_entity_color(block_entity, dxf_doc),
                                         'layer': getattr(entity.dxf, 'layer', '0')
                                    })
                            
                            elif block_entity_type == 'CIRCLE':
                                center = np.array(block_entity.dxf.center)
                                radius = block_entity.dxf.radius * scale_x
                                
                                center_world = self.transform_block_point(
                                    center, insert_point, rotation, scale_x, scale_y, scale_z
                                )
                                
                                block_entities.append({
                                    'type': 'circle',
                                    'center': center_world[:3].tolist(),
                                    'radius': radius,
                                    'color': color_override or self.get_entity_color(block_entity, dxf_doc),
                                     'layer': getattr(entity.dxf, 'layer', '0')
                                })
                            
                            elif block_entity_type == 'ARC':
                                center = np.array(block_entity.dxf.center)
                                radius = block_entity.dxf.radius * scale_x
                                
                                center_world = self.transform_block_point(
                                    center, insert_point, rotation, scale_x, scale_y, scale_z
                                )
                                
                                block_entities.append({
                                    'type': 'arc',
                                    'center': center_world[:3].tolist(),
                                    'radius': radius,
                                    'start_angle': block_entity.dxf.start_angle + rotation,
                                    'end_angle': block_entity.dxf.end_angle + rotation,
                                    'color': color_override or self.get_entity_color(block_entity, dxf_doc),
                                     'layer': getattr(entity.dxf, 'layer', '0')
                                })
                    
                    except Exception as e:
                        print(f"  ⚠️ Block geometry extraction failed: {e}")
                    
                    # Create result list
                    result_entities = []
                    result_entities.extend(block_entities)
                    
                    # ✅ Add text label with smart color selection
                    if text_label:
                        clean_text = str(text_label).strip()
                        
                        if clean_text and len(clean_text) > 0:
                            # ✅ SMART COLOR SELECTION based on label pattern
                            if 'DW' in clean_text.upper() and '_' in clean_text:
                                # Grid labels like "DW2039017_000347" → Keep cyan
                                label_color = (0, 255, 255)  # Cyan for grid IDs
                                label_height = 3.0  # Normal size
                            else:
                                # Feature labels like "FORNACE000015" → Yellow
                                label_color = (255, 255, 0)  # Yellow for feature names
                                label_height = 2.5  # Slightly smaller
                            
                            result_entities.append({
                                'type': 'text',
                                'text': clean_text,
                                'position': [insert_point[0], insert_point[1], insert_point[2]],
                                'height': label_height,
                                'rotation': rotation,
                                'color': label_color,
                                'extraction_method': extraction_method,
                                'layer': getattr(entity.dxf, 'layer', '0'),
                                'has_alignment': True
                            })
                        
                    return result_entities if result_entities else None
                    
                except Exception as e:
                    return None
            
            # === TEXT ENTITY ===
            elif entity.dxftype() in ('TEXT', 'MTEXT'):
                try:
                    text_content = entity.dxf.text if hasattr(entity.dxf, 'text') else ""
                    insert_point = np.array(entity.dxf.insert if hasattr(entity.dxf, 'insert') else [0, 0, 0])
                    height = entity.dxf.height if hasattr(entity.dxf, 'height') else 1.0
                    rotation = entity.dxf.rotation if hasattr(entity.dxf, 'rotation') else 0.0
                    entity.text_content = text_content  # ← Store text content
                    entity.is_grid_label = False    
                    
                    has_alignment = False
                    if entity.dxftype() == 'TEXT':
                        halign = entity.dxf.halign if hasattr(entity.dxf, 'halign') else 0
                        valign = entity.dxf.valign if hasattr(entity.dxf, 'valign') else 0
                        if (halign != 0 or valign != 0) and hasattr(entity.dxf, 'align_point'):
                            insert_point = np.array(entity.dxf.align_point)
                            has_alignment = True
                    elif entity.dxftype() == 'MTEXT':
                        attachment_point = entity.dxf.attachment_point if hasattr(entity.dxf, 'attachment_point') else 1
                        if attachment_point == 5:
                            has_alignment = True

                    if transformer:
                        insert_point[:2] = transformer.transform(insert_point[0], insert_point[1])
                    
                    if has_alignment:
                        pos_x = insert_point[0] + offset_x
                        pos_y = insert_point[1] + offset_y
                        pos_z = insert_point[2] + (offset_z if len(insert_point) > 2 else 0.0)
                    else:
                        pos_x = insert_point[0] + offset_x + 15.0 + height * 3
                        pos_y = insert_point[1] + offset_y - 2.5 + height * 1
                        pos_z = insert_point[2] + (offset_z if len(insert_point) > 2 else 0.0)

                    entity_data = {
                        'type': 'text',
                        'text': text_content,
                        'position': [pos_x, pos_y, pos_z],
                        'height': height,
                        'rotation': rotation,
                        'color': color_override or self.get_entity_color(entity, dxf_doc),
                        'layer': getattr(entity.dxf, 'layer', '0'),
                        'has_alignment': has_alignment
                    }
                    entity_data['text_content'] = text_content  # ← ADD THIS LINE
                    entity_data['is_grid_label'] = False  # ← ADD THIS LINE
                except Exception as e:
                    print(f"  ⚠️ Text entity error: {e}")
                    return None
            
            # === POINT ENTITY ===
            elif entity.dxftype() == 'POINT':
                try:
                    location = np.array(entity.dxf.location)
                    
                    if transformer:
                        location[:2] = transformer.transform(location[0], location[1])
                    
                    location[0] += offset_x
                    location[1] += offset_y
                    if len(location) > 2:
                        location[2] += offset_z
                    
                    entity_data = {
                        'type': 'point',
                        'position': location[:3].tolist(),
                        'color': color_override or self.get_entity_color(entity, dxf_doc),
                        'layer': getattr(entity.dxf, 'layer', '0')
                    }
                except Exception as e:
                    print(f"  ⚠️ Point entity error: {e}")
                    return None
            
            # === POLYLINE ENTITY ===
            elif entity.dxftype() in ('POLYLINE', 'LWPOLYLINE'):
                points = []
                
                if entity.dxftype() == 'LWPOLYLINE':
                    for point in entity.get_points():
                        pt = np.array(point)
                        if transformer:
                            pt[:2] = transformer.transform(pt[0], pt[1])
                        pt[0] += offset_x
                        pt[1] += offset_y
                        if len(pt) > 2:
                            pt[2] += offset_z
                        points.append(pt[:3].tolist())
                else:
                    for vertex in entity.vertices:
                        pt = np.array(vertex.dxf.location)
                        if transformer:
                            pt[:2] = transformer.transform(pt[0], pt[1])
                        pt[0] += offset_x
                        pt[1] += offset_y
                        if len(pt) > 2:
                            pt[2] += offset_z
                        points.append(pt[:3].tolist())
                
                if len(points) >= 2:
                    entity_data = {
                        'type': 'polyline',
                        'points': points,
                        'closed': entity.is_closed if hasattr(entity, 'is_closed') else False,
                        'color': color_override or self.get_entity_color(entity, dxf_doc),
                         'layer': getattr(entity.dxf, 'layer', '0')  # ✅ ADD THIS LINE
                    }
            
            # === CIRCLE ENTITY ===
            elif entity.dxftype() == 'CIRCLE':
                center = np.array(entity.dxf.center)
                radius = entity.dxf.radius
                
                if transformer:
                    center[:2] = transformer.transform(center[0], center[1])
                
                center[0] += offset_x
                center[1] += offset_y
                if len(center) > 2:
                    center[2] += offset_z
                
                entity_data = {
                    'type': 'circle',
                    'center': center[:3].tolist(),
                    'radius': radius,
                    'color': color_override or self.get_entity_color(entity, dxf_doc),
                    'layer': getattr(entity.dxf, 'layer', '0')
                }
            
            # === ARC ENTITY ===
            elif entity.dxftype() == 'ARC':
                center = np.array(entity.dxf.center)
                radius = entity.dxf.radius
                start_angle = entity.dxf.start_angle
                end_angle = entity.dxf.end_angle
                
                if transformer:
                    center[:2] = transformer.transform(center[0], center[1])
                
                center[0] += offset_x
                center[1] += offset_y
                if len(center) > 2:
                    center[2] += offset_z
                
                entity_data = {
                    'type': 'arc',
                    'center': center[:3].tolist(),
                    'radius': radius,
                    'start_angle': start_angle,
                    'end_angle': end_angle,
                    'color': color_override or self.get_entity_color(entity, dxf_doc),
                    'layer': getattr(entity.dxf, 'layer', '0')
                }
            
            # === 3DFACE ENTITY (GRID SQUARES) ===
            elif entity.dxftype() == '3DFACE':
                try:
                    vtx0 = np.array(entity.dxf.vtx0) if hasattr(entity.dxf, 'vtx0') else np.array([0, 0, 0])
                    vtx1 = np.array(entity.dxf.vtx1) if hasattr(entity.dxf, 'vtx1') else np.array([0, 0, 0])
                    vtx2 = np.array(entity.dxf.vtx2) if hasattr(entity.dxf, 'vtx2') else np.array([0, 0, 0])
                    vtx3 = np.array(entity.dxf.vtx3) if hasattr(entity.dxf, 'vtx3') else vtx2
                    
                    vertices = []
                    for vtx in [vtx0, vtx1, vtx2, vtx3]:
                        if transformer:
                            vtx[:2] = transformer.transform(vtx[0], vtx[1])
                        vtx[0] += offset_x
                        vtx[1] += offset_y
                        if len(vtx) > 2:
                            vtx[2] += offset_z
                        vertices.append(vtx[:3].tolist())
                    
                    is_triangle = np.allclose(vtx2, vtx3, atol=1e-6)
                    
                    entity_data = {
                        'type': '3dface',
                        'vertices': vertices[:3] if is_triangle else vertices,
                        'is_triangle': is_triangle,
                        'color': color_override or self.get_entity_color(entity, dxf_doc),
                         'layer': getattr(entity.dxf, 'layer', '0')
                    }
                    
                except Exception as e:
                    print(f"  ⚠️ 3DFACE entity error: {e}")
                    return None

        except Exception as e:
            print(f"  ⚠️ Entity processing error ({entity.dxftype()}): {e}")

        return entity_data


    def get_entity_color(self, entity, dxf_doc=None):
        """Extract color from DXF entity — prefer true_color, fall back to ACI."""
        try:
            tc = getattr(entity.dxf, 'true_color', None)
            if tc is not None and int(tc) != 0:
                return _true_color_to_rgb(int(tc))
        except Exception:
            pass
        try:
            color_index = entity.dxf.color
            if color_index == 256:
                # Resolve color from layer
                if dxf_doc and hasattr(dxf_doc, 'layers'):
                    layer_name = getattr(entity.dxf, 'layer', '0')
                    if layer_name in dxf_doc.layers:
                        layer = dxf_doc.layers.get(layer_name)
                        try:
                            layer_tc = getattr(layer.dxf, 'true_color', None)
                            if layer_tc is not None and int(layer_tc) != 0:
                                return _true_color_to_rgb(int(layer_tc))
                        except Exception:
                            pass
                        try:
                            layer_color = layer.dxf.color
                            if layer_color != 256 and layer_color != 0:
                                return _ACI_RGB.get(layer_color, (255, 255, 255))
                        except Exception:
                            pass
                return (255, 255, 255)
            return _ACI_RGB.get(color_index, (255, 255, 255))
        except Exception:
            return (255, 255, 255)
    
    def render_dxf_in_vtk(self, attachment_data):
        """
        Refined batched rendering with layer properties and actors tagged with _naksha_snt_layer.
        """
        try:
            import vtk
            from collections import defaultdict
            import time
            t0 = time.perf_counter()
            
            if not hasattr(self.app, 'vtk_widget') or not self.app.vtk_widget:
                print(f"  ⚠️ VTK widget not ready - cannot render {attachment_data['filename']}")
                return
            
            renderer = self.app.vtk_widget.renderer
            actors = []
            
            if not attachment_data['entities']:
                print(f"  ℹ️ No entities to render for {attachment_data['filename']}")
                if not hasattr(self.app, 'dxf_actors'):
                    self.app.dxf_actors = []
                self.app.dxf_actors = [
                    entry for entry in self.app.dxf_actors
                    if entry.get("filename") != attachment_data["filename"]
                ]
                self.app.dxf_actors.append({
                    'filename': attachment_data['filename'],
                    'full_path': attachment_data.get('full_path'),
                    'actors': [],
                    'bounds': attachment_data.get('bounds'),
                })
                return
            
            # Count entity types for logging
            entity_type_counts = {}
            for entity in attachment_data['entities']:
                etype = entity['type']
                entity_type_counts[etype] = entity_type_counts.get(etype, 0) + 1
            
            print(f"\n📊 Rendering {attachment_data['filename']}:")
            for etype, count in sorted(entity_type_counts.items()):
                print(f"  {etype:15s}: {count:4d} entities")
            
            # Pause rendering during batch build
            render_window = self.app.vtk_widget.GetRenderWindow()
            if render_window:
                render_window.SetDesiredUpdateRate(0.0001)
            
            # ═══════════════════════════════════════════════════════════════
            # NAKSHATECH BATCH: Group entities by (color, layer, type)
            # Each group becomes ONE VTK actor with merged geometry
            # ═══════════════════════════════════════════════════════════════
            line_groups = defaultdict(list)      # (color_tuple, layer, width, style) → [entity, ...]
            face_groups = defaultdict(list)      # (color_tuple, layer, width, style) → [entity, ...]
            circle_groups = defaultdict(list)    # (color_tuple, layer, width, style) → [entity, ...]
            text_entities = []                   # text rendered individually (3D labels)
            
            # Get DXF item for layer caching
            item = attachment_data.get('_dxf_item')
            
            for entity in attachment_data['entities']:
                etype = entity['type']
                color_key = tuple(entity.get('color', (255, 255, 255)))
                layer = entity.get('layer', '0')
                try:
                    line_width = max(1.0, min(float(entity.get('line_width', 2.0) or 2.0), 20.0))
                except Exception:
                    line_width = 2.0
                line_style = _normalize_dxf_line_style(entity.get('line_style', 'Solid'))
                group_key = (color_key, layer, line_width, line_style)
                
                if etype == 'line':
                    line_groups[group_key].append(entity)
                elif etype == 'polyline':
                    line_groups[group_key].append(entity)
                elif etype == '3dface':
                    face_groups[group_key].append(entity)
                elif etype in ('circle', 'arc'):
                    circle_groups[group_key].append(entity)
                elif etype == 'text':
                    if entity.get('text') and len(str(entity['text']).strip()) > 0:
                        text_entities.append(entity)
                elif etype == 'point':
                    line_groups[group_key].append(entity)  # points go in line batch
            
            mode = attachment_data.get('mode', 'overlay')
            
            # ── BATCH 1: Lines + Polylines (ONE actor per color/layer group) ──
            for (color, layer_name, line_width, line_style), group in line_groups.items():
                appender = vtk.vtkAppendPolyData()
                
                for e in group:
                    pts = vtk.vtkPoints()
                    cells = vtk.vtkCellArray()
                    
                    if e['type'] == 'line':
                        pts.InsertNextPoint(e['start'])
                        pts.InsertNextPoint(e['end'])
                        line_cell = vtk.vtkLine()
                        line_cell.GetPointIds().SetId(0, 0)
                        line_cell.GetPointIds().SetId(1, 1)
                        cells.InsertNextCell(line_cell)
                        pd = vtk.vtkPolyData()
                        pd.SetPoints(pts)
                        pd.SetLines(cells)
                    elif e['type'] == 'polyline':
                        for i, pt in enumerate(e['points']):
                            pts.InsertNextPoint(pt)
                        pline = vtk.vtkPolyLine()
                        n = pts.GetNumberOfPoints()
                        pline.GetPointIds().SetNumberOfIds(n)
                        for i in range(n):
                            pline.GetPointIds().SetId(i, i)
                        cells.InsertNextCell(pline)
                        if e.get('closed', False) and n > 2:
                            close = vtk.vtkLine()
                            close.GetPointIds().SetId(0, n - 1)
                            close.GetPointIds().SetId(1, 0)
                            cells.InsertNextCell(close)
                        pd = vtk.vtkPolyData()
                        pd.SetPoints(pts)
                        pd.SetLines(cells)
                    elif e['type'] == 'point':
                        pts.InsertNextPoint(e.get('position', (0, 0, 0)))
                        verts = vtk.vtkCellArray()
                        verts.InsertNextCell(1)
                        verts.InsertCellPoint(0)
                        pd = vtk.vtkPolyData()
                        pd.SetPoints(pts)
                        pd.SetVerts(verts)
                    else:
                        continue
                    
                    appender.AddInputData(pd)
                
                appender.Update()
                mapper = vtk.vtkPolyDataMapper()
                mapper.SetInputData(appender.GetOutput())
                mapper.SetResolveCoincidentTopologyToPolygonOffset()
                mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(-1.0, -1.0)
                
                actor = vtk.vtkActor()
                actor.SetMapper(mapper)
                actor.GetProperty().SetColor([c / 255.0 for c in color])
                _apply_dxf_actor_line_style(actor, line_width, line_style)
                actor.GetProperty().SetOpacity(1.0 if mode == 'overlay' else 0.6)
                actor.GetProperty().SetLighting(False)
                actor.GetProperty().SetAmbient(1.0)
                actor._original_color = color
                actor._naksha_snt_layer = layer_name
                actor._naksha_snt_filename = attachment_data['filename']
                
                renderer.AddActor(actor)
                actors.append(actor)
                if item:
                    item.actor_cache.setdefault(layer_name, []).append(actor)
            
            # ── BATCH 2: 3DFACE wireframes (ONE actor per color/layer group) ──
            for (color, layer_name, line_width, line_style), group in face_groups.items():
                appender = vtk.vtkAppendPolyData()
                
                for e in group:
                    pts = vtk.vtkPoints()
                    for v in e['vertices']:
                        pts.InsertNextPoint(v)
                    
                    polys = vtk.vtkCellArray()
                    if e.get('is_triangle', False):
                        tri = vtk.vtkTriangle()
                        for i in range(3):
                            tri.GetPointIds().SetId(i, i)
                        polys.InsertNextCell(tri)
                    else:
                        quad = vtk.vtkQuad()
                        for i in range(4):
                            quad.GetPointIds().SetId(i, i)
                        polys.InsertNextCell(quad)
                    
                    pd = vtk.vtkPolyData()
                    pd.SetPoints(pts)
                    pd.SetPolys(polys)
                    appender.AddInputData(pd)
                
                appender.Update()
                
                # Extract edges for wireframe
                edges = vtk.vtkExtractEdges()
                edges.SetInputData(appender.GetOutput())
                edges.Update()
                
                mapper = vtk.vtkPolyDataMapper()
                mapper.SetInputConnection(edges.GetOutputPort())
                mapper.SetResolveCoincidentTopologyToPolygonOffset()
                mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(-1.0, -1.0)
                
                actor = vtk.vtkActor()
                actor.SetMapper(mapper)
                actor.GetProperty().SetColor([c / 255.0 for c in color])
                _apply_dxf_actor_line_style(actor, line_width, line_style)
                actor.GetProperty().SetOpacity(1.0 if mode == 'overlay' else 0.6)
                actor.GetProperty().SetLighting(False)
                actor.GetProperty().SetAmbient(1.0)
                actor._original_color = color
                actor._naksha_snt_layer = layer_name
                actor._naksha_snt_filename = attachment_data['filename']
                
                renderer.AddActor(actor)
                actors.append(actor)
                if item:
                    item.actor_cache.setdefault(layer_name, []).append(actor)
            
            # ── BATCH 3: Circles/Arcs (ONE actor per color/layer group) ──
            for (color, layer_name, line_width, line_style), group in circle_groups.items():
                appender = vtk.vtkAppendPolyData()
                
                for e in group:
                    if e['type'] == 'circle':
                        src = vtk.vtkRegularPolygonSource()
                        src.SetNumberOfSides(64)
                        src.SetRadius(e.get('radius', 1.0))
                        src.SetCenter(e.get('center', (0, 0, 0)))
                        src.SetGeneratePolygon(False)
                        src.SetGeneratePolyline(True)
                        src.Update()
                        appender.AddInputData(src.GetOutput())
                    elif e['type'] == 'arc':
                        arc = vtk.vtkArcSource()
                        arc.SetCenter(e.get('center', (0, 0, 0)))
                        arc.SetPoint1(e.get('start', (1, 0, 0)))
                        arc.SetPoint2(e.get('end', (0, 1, 0)))
                        arc.SetResolution(32)
                        arc.Update()
                        appender.AddInputData(arc.GetOutput())
                
                appender.Update()
                mapper = vtk.vtkPolyDataMapper()
                mapper.SetInputData(appender.GetOutput())
                mapper.SetResolveCoincidentTopologyToPolygonOffset()
                mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(-1.0, -1.0)
                
                actor = vtk.vtkActor()
                actor.SetMapper(mapper)
                actor.GetProperty().SetColor([c / 255.0 for c in color])
                _apply_dxf_actor_line_style(actor, line_width, line_style)
                actor.GetProperty().SetOpacity(1.0 if mode == 'overlay' else 0.6)
                actor.GetProperty().SetLighting(False)
                actor.GetProperty().SetAmbient(1.0)
                actor._original_color = color
                actor._naksha_snt_layer = layer_name
                actor._naksha_snt_filename = attachment_data['filename']
                
                renderer.AddActor(actor)
                actors.append(actor)
                if item:
                    item.actor_cache.setdefault(layer_name, []).append(actor)
            
            # ── TEXT (individual actors — typically few, need 3D positioning) ──
            for entity in text_entities:
                try:
                    actor = self.create_text_actor(entity)
                    if actor:
                        if actor.GetMapper():
                            actor.GetMapper().SetResolveCoincidentTopologyToPolygonOffset()
                            actor.GetMapper().SetRelativeCoincidentTopologyPolygonOffsetParameters(-20.0, -20.0)
                        actor.GetProperty().SetOpacity(1.0)
                        actor._naksha_snt_layer = entity.get('layer', '0')
                        actor._naksha_snt_filename = attachment_data['filename']
                        actor._original_color = tuple(entity.get('color', (255, 255, 255)))
                        renderer.AddActor(actor)
                        actors.append(actor)
                        layer_name = entity.get('layer', '0')
                        if item:
                            item.actor_cache.setdefault(layer_name, []).append(actor)
                except Exception:
                    continue
            
            # ── Register actors ──
            if not hasattr(self.app, 'dxf_actors'):
                self.app.dxf_actors = []
            
            from pathlib import Path
            full_path = None
            for it in getattr(self, 'dxf_items', []):
                if it.dxf_path.name == attachment_data['filename']:
                    full_path = str(it.dxf_path.resolve())
                    break
            
            self.app.dxf_actors = [
                entry for entry in self.app.dxf_actors
                if entry.get("filename") != attachment_data["filename"]
            ]
            self.app.dxf_actors.append({
                'filename': attachment_data['filename'],
                'full_path': full_path,
                'actors': actors,
                'bounds': attachment_data.get('bounds'),
            })
            
            # Re-enable rendering
            if render_window:
                render_window.SetDesiredUpdateRate(30.0)
                if not bool(getattr(renderer, "_skip_camera_reset", False)):
                    renderer.ResetCamera()
                renderer.ResetCameraClippingRange()
                renderer.GetActiveCamera().Zoom(0.85)
                render_window.Render()
            
            # Invalidate unified actor cache
            try:
                from gui.unified_actor_manager import invalidate_unified_actor, sync_palette_to_gpu
                invalidate_unified_actor(self.app)
                sync_palette_to_gpu(self.app, 0, render=False)
            except Exception:
                pass
            
            elapsed = (time.perf_counter() - t0) * 1000
            total_entities = sum(entity_type_counts.values())
            print(f"\n  ✅ BATCHED: {total_entities} entities → {len(actors)} actors in {elapsed:.0f}ms")
            print(f"  📊 Actor breakdown: {len(line_groups)} line groups, "
                  f"{len(face_groups)} face groups, {len(circle_groups)} circle groups, "
                  f"{len(text_entities)} text labels")
            
        except Exception as e:
            print(f"  ⚠️ VTK rendering failed: {e}")
            import traceback
            traceback.print_exc()
    
    
    def create_text_actor(self, entity):
        """Create VTK actor for text labels - ADAPTIVE SCALING based on grid context"""
        import vtk
        
        # ✅ EMERGENCY FIX: Validate text before creating VTK object
        text_content = entity.get('text', '')
        
        if not text_content or not isinstance(text_content, str):
            print(f"  ⚠️ Skipping invalid text entity: {entity}")
            return None
        
        text_content = str(text_content).strip()
        
        if len(text_content) == 0:
            print(f"  ⚠️ Skipping empty text entity")
            return None
        
        # ✅ Now safe to create VTK text
        text_source = vtk.vtkVectorText()
        text_source.SetText(text_content)
        text_source.Update()  # CRITICAL: Compute bounds before scaling
       
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(text_source.GetOutputPort())
       
        actor = vtk.vtkFollower()
        actor.SetMapper(mapper)
    
        actor.text_content = text_content  # ← Store text for later retrieval
        actor.is_grid_label = True  
       
        # Get actual text geometry bounds
        bounds = text_source.GetOutput().GetBounds()
        text_width = bounds[1] - bounds[0]   # X extent
        text_height = bounds[3] - bounds[2]  # Y extent
       
        # ADAPTIVE: Detect coordinate system scale from position magnitude
        # Text should be about 5-10% of typical grid cell width
        pos_magnitude = abs(entity['position'][0]) + abs(entity['position'][1])
       
        if pos_magnitude > 100000:  # Large coordinate system (e.g., UTM coordinates)
            desired_width = 80.0   # Increased to fit long block names
            desired_height = 20.0
        elif pos_magnitude > 10000:  # Medium coordinate system
            desired_width = 40.0   # Increased to fit long block names
            desired_height = 10.0
        elif pos_magnitude > 1000:  # Small coordinate system
            desired_width = 16.0   # Increased to fit long block names
            desired_height = 4.0
        else:  # Very small coordinate system
            desired_width = 4.0    # Increased to fit long block names
            desired_height = 1.0
       
        # Calculate scale factor to fit within bounding box
        if text_width > 0 and text_height > 0:
            scale_x = desired_width / text_width
            scale_y = desired_height / text_height
            scale_factor = min(scale_x, scale_y)  # Use minimum to ensure text fits
        else:
            scale_factor = 1.0
       
        # Apply uniform scaling
        actor.SetScale(scale_factor, scale_factor, scale_factor)
       
        # Set appearance
        actor.GetProperty().SetColor([c/255.0 for c in entity['color']])
        actor.GetProperty().SetLineWidth(3.0)
        actor.GetProperty().SetOpacity(1.0)
        actor.GetProperty().SetAmbient(0.6)
        actor.GetProperty().SetDiffuse(0.9)
       
        # Store grid label metadata
        actor.grid_name = entity['text']
        actor.is_grid_label = True
        actor.PickableOn()
       
        # Set camera for billboard effect
        try:
            if hasattr(self, 'app') and hasattr(self.app, 'vtk_widget'):
                actor.SetCamera(self.app.vtk_widget.renderer.GetActiveCamera())
                
        except Exception:
            pass
            
        pos = entity['position']
        if entity.get('has_alignment', False):
            half_w = (text_width * scale_factor) / 2.0
            half_h = (text_height * scale_factor) / 2.0
            actor.SetPosition(pos[0] - half_w, pos[1] - half_h, pos[2] if len(pos) > 2 else 0.0)
        else:
            actor.SetPosition(pos)
       
        return actor
   

    def create_line_actor(self, entity):
        """Create VTK actor for line"""
        import vtk
        
        points = vtk.vtkPoints()
        points.InsertNextPoint(entity['start'])
        points.InsertNextPoint(entity['end'])
        
        line = vtk.vtkLine()
        line.GetPointIds().SetId(0, 0)
        line.GetPointIds().SetId(1, 1)
        
        lines = vtk.vtkCellArray()
        lines.InsertNextCell(line)
        
        polydata = vtk.vtkPolyData()
        polydata.SetPoints(points)
        polydata.SetLines(lines)
        
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(polydata)
        
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor([c/255.0 for c in entity['color']])
        actor.GetProperty().SetLineWidth(2)
        
        return actor
    
    def create_polyline_actor(self, entity):
        """
        ✅ ENHANCED: Better polyline rendering for grid lines
        """
        import vtk
        
        points = vtk.vtkPoints()
        points.SetNumberOfPoints(len(entity['points']))
        
        # Set all points
        for i, pt in enumerate(entity['points']):
            points.SetPoint(i, pt)
        
        # Create polyline
        polyline = vtk.vtkPolyLine()
        polyline.GetPointIds().SetNumberOfIds(len(entity['points']))
        for i in range(len(entity['points'])):
            polyline.GetPointIds().SetId(i, i)
        
        cells = vtk.vtkCellArray()
        cells.InsertNextCell(polyline)
        
        polydata = vtk.vtkPolyData()
        polydata.SetPoints(points)
        polydata.SetLines(cells)
        
        # ✅ Add closed line if needed
        if entity.get('closed', False) and len(entity['points']) > 2:
            closing_line = vtk.vtkLine()
            closing_line.GetPointIds().SetId(0, len(entity['points']) - 1)
            closing_line.GetPointIds().SetId(1, 0)
            cells.InsertNextCell(closing_line)
        
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(polydata)
        
        # ✅ CRITICAL: Anti-aliasing for smooth lines
        mapper.SetResolveCoincidentTopologyToPolygonOffset()
        mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(0, -1)
        
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        
        # ✅ Make lines VERY visible
        actor.GetProperty().SetColor([c/255.0 for c in entity['color']])
        actor.GetProperty().SetLineWidth(3.0)  # Thick lines
        actor.GetProperty().SetOpacity(1.0)
        actor.GetProperty().SetAmbient(0.6)
        actor.GetProperty().SetDiffuse(0.9)
        
        # ✅ Ensure lines render on top
        actor.GetProperty().SetRenderLinesAsTubes(True)
        
        return actor
        
    def create_circle_actor(self, entity):
        """Create VTK actor for circle"""
        import vtk
        
        polygon = vtk.vtkRegularPolygonSource()
        polygon.SetNumberOfSides(64)
        polygon.SetRadius(entity['radius'])
        polygon.SetCenter(entity['center'])
        polygon.GeneratePolygonOff()  # This makes it a circle outline, not filled
        
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(polygon.GetOutputPort())
        
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor([c/255.0 for c in entity['color']])
        
        # ✅ CRITICAL: Make circles much more visible
        actor.GetProperty().SetLineWidth(5.0)  # Increased from 2.0
        actor.GetProperty().SetOpacity(1.0)
        
        return actor
    
    def create_arc_actor(self, entity):
        """Create VTK actor for arc"""
        import vtk
        import math
        
        arc = vtk.vtkArcSource()
        arc.SetCenter(entity['center'])
        
        radius = entity['radius']
        start_rad = math.radians(entity['start_angle'])
        end_rad = math.radians(entity['end_angle'])
        
        start_pt = [
            entity['center'][0] + radius * math.cos(start_rad),
            entity['center'][1] + radius * math.sin(start_rad),
            entity['center'][2]
        ]
        end_pt = [
            entity['center'][0] + radius * math.cos(end_rad),
            entity['center'][1] + radius * math.sin(end_rad),
            entity['center'][2]
        ]
        
        arc.SetPoint1(start_pt)
        arc.SetPoint2(end_pt)
        arc.SetResolution(64)
        
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(arc.GetOutputPort())
        
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor([c/255.0 for c in entity['color']])
        actor.GetProperty().SetLineWidth(2)
        
        return actor
    
    def naksha_dark_theme(self):
        return """
        QWidget {
            background-color: #121212;
            color: #e0e0e0;
            font-family: "Segoe UI";
            font-size: 10pt;
        }
        QLabel { color: #e0e0e0; }
        QGroupBox {
            border: 1px solid #3a3a3a;
            border-radius: 5px;
            margin-top: 10px;
            padding-top: 10px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 5px;
        }
        QComboBox, QSpinBox, QDoubleSpinBox {
            background-color: #1e1e1e;
            border: 1px solid #3a3a3a;
            border-radius: 4px;
            padding: 5px;
            color: #eeeeee;
        }
        QPushButton {
            background-color: #333333;
            border: 1px solid #555555;
            border-radius: 5px;
            padding: 8px 12px;
            color: #dddddd;
        }
        QPushButton:hover {
            background-color: #444444;
            border-color: #007acc;
        }
        QRadioButton, QCheckBox {
            spacing: 6px;
            color: #cccccc;
        }
        QRadioButton::indicator, QCheckBox::indicator {
            width: 16px;
            height: 16px;
            border: 2px solid #555555;
            background: #1e1e1e;
            border-radius: 8px;
        }
        QRadioButton::indicator:checked, QCheckBox::indicator:checked {
            background-color: #007acc;
            border-color: #007acc;
        }
        """

def debug_dxf_contents(dxf_path):
        """Debug: Show what entity types are in the DXF file"""
        try:
            import ezdxf
            print(f"\n🔍 DEBUG: Analyzing DXF file: {dxf_path}")
            
            dxf_doc = ezdxf.readfile(str(dxf_path))
            modelspace = dxf_doc.modelspace()
            
            # Count entity types
            entity_counts = {}
            text_samples = []
            
            for entity in modelspace:
                entity_type = entity.dxftype()
                entity_counts[entity_type] = entity_counts.get(entity_type, 0) + 1
                
                # Collect text samples
                if entity_type in ('TEXT', 'MTEXT'):
                    text_content = entity.dxf.text if hasattr(entity.dxf, 'text') else "NO TEXT"
                    text_samples.append(text_content)
                    if len(text_samples) <= 5:  # Show first 5
                        print(f"  📝 Found TEXT: '{text_content}'")
            
            print(f"\n📊 Entity type summary:")
            for entity_type, count in sorted(entity_counts.items()):
                print(f"  {entity_type}: {count}")
            
            print(f"\n📝 Total TEXT/MTEXT entities: {entity_counts.get('TEXT', 0) + entity_counts.get('MTEXT', 0)}")
            
            if not text_samples:
                print("  ⚠️ WARNING: NO TEXT ENTITIES FOUND IN DXF!")
                print("  The grid labels might be in a different format (BLOCKS, ATTRIBUTES, etc.)")
            
        except Exception as e:
            print(f"❌ Debug failed: {e}")
            import traceback
            traceback.print_exc()

def show_multi_dxf_attachment_dialog(app):
    """Show the multi-DXF attachment dialog (persistent)."""
    if hasattr(app, '_dxf_dialog') and app._dxf_dialog is not None:
        try:
            dlg = app._dxf_dialog
            if dlg._is_minimized_to_chip:
                dlg._do_restore_from_chip()
            else:
                if dlg.windowState() & Qt.WindowMinimized:
                    dlg.showNormal()
                dlg.show()
                dlg.raise_()
                dlg.activateWindow()
            return dlg
        except Exception:
            app._dxf_dialog = None

    dialog = MultiDXFAttachmentDialog(app, parent=app)
    dialog.setModal(False)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    app._dxf_dialog = dialog

    def on_close(event):
        event.ignore()
        dialog.hide()
    dialog.closeEvent = on_close

    return dialog

# Backward compatibility
def show_dxf_attachment_dialog(app):
    return show_multi_dxf_attachment_dialog(app)

def inspect_dxf_text_entities(dxf_path):
        """Show first 20 text entities to verify content"""
        try:
            import ezdxf
            print(f"\n🔍 Inspecting TEXT entities in: {dxf_path}")
            
            dxf_doc = ezdxf.readfile(str(dxf_path))
            modelspace = dxf_doc.modelspace()
            
            text_count = 0
            for entity in modelspace:
                if entity.dxftype() in ('TEXT', 'MTEXT', 'INSERT'):
                    text_count += 1
                    
                    # Get text content
                    if entity.dxftype() == 'INSERT':
                        text = f"BLOCK: {entity.dxf.name}"
                        if hasattr(entity, 'attribs'):
                            for attrib in entity.attribs:
                                if hasattr(attrib.dxf, 'text'):
                                    text += f" | ATTRIB: {attrib.dxf.text}"
                    else:
                        text = entity.dxf.text if hasattr(entity.dxf, 'text') else "NO TEXT"
                    
                    position = entity.dxf.insert if hasattr(entity.dxf, 'insert') else entity.dxf.location
                    
                    if text_count <= 20:  # Show first 20
                        print(f"  [{text_count}] {entity.dxftype()}: '{text}' at {position[:2]}")
            
            print(f"\n📊 Total text entities: {text_count}")
            
        except Exception as e:
            print(f"❌ Inspection failed: {e}")
    
def debug_insert_blocks(dxf_path):
    """Debug: Show all INSERT blocks and their attributes"""
    try:
        import ezdxf
        print(f"\n🔍 DEBUG: Analyzing INSERT blocks in: {dxf_path}")
        
        dxf_doc = ezdxf.readfile(str(dxf_path))
        modelspace = dxf_doc.modelspace()
        
        insert_count = 0
        for entity in modelspace:
            if entity.dxftype() == 'INSERT':
                insert_count += 1
                print(f"\n📦 INSERT #{insert_count}:")
                print(f"   Block name: {entity.dxf.name}")
                print(f"   Position: {entity.dxf.insert}")
                
                if hasattr(entity, 'attribs'):
                    print(f"   Attributes ({len(entity.attribs)}):")
                    for idx, attrib in enumerate(entity.attribs):
                        tag = attrib.dxf.tag if hasattr(attrib.dxf, 'tag') else 'NO TAG'
                        text = attrib.dxf.text if hasattr(attrib.dxf, 'text') else 'NO TEXT'
                        print(f"     [{idx}] Tag: {tag}, Text: '{text}'")
                else:
                    print(f"   ⚠️ No attributes")
                
                # Check block definition
                try:
                    block = dxf_doc.blocks.get(entity.dxf.name)
                    print(f"   Block definition entities: {len(list(block))}")
                    for block_entity in block:
                        if block_entity.dxftype() in ('TEXT', 'MTEXT', 'ATTDEF'):
                            text = block_entity.dxf.text if hasattr(block_entity.dxf, 'text') else 'NO TEXT'
                            print(f"     - {block_entity.dxftype()}: '{text}'")
                except Exception:
                    print(f"   ⚠️ Could not read block definition")
        
        print(f"\n📊 Total INSERT blocks: {insert_count}")
        
    except Exception as e:
        print(f"❌ Debug failed: {e}")
        import traceback
        traceback.print_exc()
        
        
        
        
        
class DXFLayerSelectionDialog(QDialog):
    def __init__(self, dxf_path: Path, parent_item=None, app=None):
        super().__init__(None, Qt.Window)
        self.setAttribute(Qt.WA_QuitOnClose, False)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.dxf_path    = dxf_path
        self.parent_item = parent_item
        self.app         = app if app is not None else getattr(parent_item, 'app', None)
        if self.app is None and parent_item is not None:
            try:
                _pdlg = parent_item._find_parent_dialog()
                self.app = getattr(_pdlg, 'app', None)
            except Exception:
                self.app = None
        self.setWindowTitle(f"Layer Display & Level Manager - {dxf_path.name}")
        self.setModal(False)
        self.setWindowModality(Qt.NonModal)
        self.resize(550, 600)

        self.attachment = self._get_live_attachment()
        self._sync_runtime_attachment_state()

        self._init_ui()
        self._load_layers()
        # Apply dark title bar on Windows
        try:
            from gui.theme_manager import ThemeManager
            ThemeManager.apply_native_window_theme(self)
        except Exception:
            pass

    def _resolve_app(self):
        app = getattr(self, "app", None)
        if app is not None:
            return app

        parent_item = getattr(self, "parent_item", None)
        if parent_item is None:
            return None

        app = getattr(parent_item, "app", None)
        if app is not None:
            return app

        try:
            parent_dialog = parent_item._find_parent_dialog()
            return getattr(parent_dialog, "app", None)
        except Exception:
            return None

    def _get_live_attachment(self):
        app = self._resolve_app()
        if app is None:
            return getattr(self, "attachment", None) if isinstance(getattr(self, "attachment", None), dict) else None

        target_name = os.path.basename(str(self.dxf_path.name or "")).lower()
        try:
            target_full = str(self.dxf_path.resolve()).lower()
        except Exception:
            target_full = str(self.dxf_path).lower()

        parent_item = getattr(self, "parent_item", None)
        if parent_item is not None:
            att = getattr(parent_item, "attachment", None)
            if isinstance(att, dict):
                self.attachment = att
                return att
            finder = getattr(parent_item, "_find_attachment", None)
            if callable(finder):
                try:
                    att = finder()
                except Exception:
                    att = None
                if isinstance(att, dict):
                    self.attachment = att
                    try:
                        parent_item.attachment = att
                    except Exception:
                        pass
                    return att

        for att in list(getattr(app, "dxf_attachments", []) or []):
            if not isinstance(att, dict):
                continue
            att_name = os.path.basename(str(att.get("filename", "") or "")).lower()
            att_full = str(att.get("full_path", "") or "").lower()
            if (target_name and att_name == target_name) or (target_full and att_full == target_full) or (target_name and att_full and os.path.basename(att_full) == target_name):
                self.attachment = att
                if parent_item is not None:
                    try:
                        parent_item.attachment = att
                        att["_dxf_item"] = parent_item
                        att.setdefault("actor_cache_map", getattr(parent_item, "actor_cache", {}))
                    except Exception:
                        pass
                return att
        return None

    def _sync_runtime_attachment_state(self):
        att = self._get_live_attachment()
        if not isinstance(att, dict):
            return None
        parsed = _ensure_dxf_parsed(att, self.parent_item)
        att["parsed"] = parsed
        att["entities"] = parsed.get("entities", [])
        att.setdefault("layer_stats", _build_dxf_layer_stats_from_entities(att.get("entities", []), parsed.get("layers", [])))
        if self.parent_item is not None:
            try:
                self.parent_item.cached_entities = att.get("entities", [])
                self.parent_item.layer_stats_cache = list(att.get("layer_stats", []) or [])
                att["_dxf_item"] = self.parent_item
                att.setdefault("actor_cache_map", self.parent_item.actor_cache)
            except Exception:
                pass
        self.attachment = att
        return att

    def _init_ui(self):
        from PySide6.QtWidgets import (
            QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView,
            QAbstractItemView, QColorDialog, QInputDialog, QLabel, QPushButton, QHBoxLayout, QVBoxLayout, QWidget, QMenu
        )
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QPixmap, QIcon

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        from gui.theme_manager import ThemeColors
        c = ThemeColors
        accent = c.get("accent")
        self.setStyleSheet(get_dialog_stylesheet() + f"""
            QTableWidget {{
                background-color: {c.get('bg_input')};
                alternate-background-color: {c.get('bg_secondary')};
                color: {c.get('text_primary')};
                gridline-color: {c.get('border')};
                border: 1px solid {c.get('border_light')};
                border-radius: 6px;
                font-size: 11px;
            }}
            QTableWidget::item {{
                padding: 5px;
            }}
            QTableWidget::item:selected {{
                background-color: {c.get('dialog_selection')};
                color: {c.get('text_on_active')};
            }}
            QHeaderView::section {{
                background-color: {c.get('bg_secondary')};
                color: {c.get('text_primary')};
                padding: 8px;
                border: none;
                border-bottom: 2px solid {c.get('border_light')};
                font-weight: 700;
                font-size: 12px;
            }}
            QScrollBar:vertical {{
                border: none;
                background: {c.get('bg_primary')};
                width: 6px;
                margin: 0px;
            }}
            QScrollBar::handle:vertical {{
                background: {c.get('border')};
                min-height: 20px;
                border-radius: 3px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {c.get('border_light')};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                border: none;
                background: none;
            }}
            QPushButton#add_btn {{
                background-color: {c.get('bg_secondary')};
                color: {c.get('text_primary')};
                border: 1px solid {c.get('border')};
                font-weight: 600;
            }}
            QPushButton#add_btn:hover {{
                background-color: {c.get('bg_button_hover')};
                border: 1px solid {accent};
                color: {accent};
            }}
            QPushButton#import_btn {{
                background-color: {c.get('bg_secondary')};
                color: {c.get('text_primary')};
                border: 1px solid {c.get('border')};
                font-weight: 600;
            }}
            QPushButton#import_btn:hover {{
                background-color: {c.get('bg_button_hover')};
                border: 1px solid {accent};
                color: {accent};
            }}
            QPushButton#merge_btn {{
                background-color: {c.get('bg_secondary')};
                color: {c.get('text_primary')};
                border: 1px solid {c.get('border')};
                font-weight: 600;
            }}
            QPushButton#merge_btn:hover {{
                background-color: {c.get('bg_button_hover')};
                border: 1px solid {accent};
                color: {accent};
            }}
            QCheckBox {{
                spacing: 6px;
                background: transparent;
            }}
            QCheckBox::indicator {{
                width: 16px;
                height: 16px;
                border: 2px solid {c.get('border')};
                border-radius: 3px;
                background-color: {c.get('bg_input')};
            }}
            QCheckBox::indicator:checked {{
                background-color: {accent};
                border-color: {accent};
            }}
            QCheckBox::indicator:hover {{
                border-color: {accent};
            }}
        """)

        title = QLabel(f"Level Manager: {self.dxf_path.name}")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)

        search_layout = QHBoxLayout()
        self.search_box = QLineEdit()
        self.search_box.setObjectName("searchBox")
        self.search_box.setPlaceholderText("Search levels by name...")
        self.search_box.textChanged.connect(self._load_layers)
        search_layout.addWidget(self.search_box)
        layout.addLayout(search_layout)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["Sr.", "Active", "Show", "Color", "Level Name", "Entities"])
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 40)
        self.table.setColumnWidth(1, 60)
        self.table.setColumnWidth(2, 60)
        self.table.setColumnWidth(3, 50)
        self.table.setColumnWidth(5, 80)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignCenter)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(32)
        
        self.table.itemDoubleClicked.connect(self._on_table_double_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)
        self.table.cellChanged.connect(self._on_cell_changed)
        
        layout.addWidget(self.table)

        actions_bar = QHBoxLayout()
        
        add_btn = QPushButton("Add Level")
        add_btn.setObjectName("add_btn")
        add_btn.setAutoDefault(False)
        add_btn.setDefault(False)
        add_btn.setFocusPolicy(Qt.NoFocus)
        add_btn.clicked.connect(self._add_layer_dialog)
        actions_bar.addWidget(add_btn)

        save_btn = QPushButton("Save")
        save_btn.setObjectName("save_btn")
        save_btn.setToolTip("Save all levels and elements to the DXF file on disk")
        save_btn.setAutoDefault(False)
        save_btn.setDefault(False)
        save_btn.setFocusPolicy(Qt.NoFocus)
        save_btn.clicked.connect(self._save_layers_to_disk)
        actions_bar.addWidget(save_btn)

        merge_btn = QPushButton("Move Elements")
        merge_btn.setObjectName("merge_btn")
        merge_btn.setToolTip("Move all elements from selected level to another level")
        merge_btn.setAutoDefault(False)
        merge_btn.setDefault(False)
        merge_btn.setFocusPolicy(Qt.NoFocus)
        merge_btn.clicked.connect(lambda: self._merge_move_dialog())
        actions_bar.addWidget(merge_btn)
        
        layout.addLayout(actions_bar)

        toggles_bar = QHBoxLayout()
        self.toggle_all_btn = QPushButton("All On")
        self.toggle_all_btn.setAutoDefault(False)
        self.toggle_all_btn.setDefault(False)
        self.toggle_all_btn.setFocusPolicy(Qt.NoFocus)
        self.toggle_all_btn.clicked.connect(self._toggle_all)
        toggles_bar.addWidget(self.toggle_all_btn)

        invert_btn = QPushButton("Invert")
        invert_btn.setAutoDefault(False)
        invert_btn.setDefault(False)
        invert_btn.setFocusPolicy(Qt.NoFocus)
        invert_btn.clicked.connect(self._invert)
        toggles_bar.addWidget(invert_btn)
        
        toggles_bar.addStretch()
        
        close_btn = QPushButton("Close")
        close_btn.setObjectName("primaryBtn")
        close_btn.setAutoDefault(False)
        close_btn.setDefault(False)
        close_btn.setFocusPolicy(Qt.NoFocus)
        close_btn.clicked.connect(self.accept)
        toggles_bar.addWidget(close_btn)
        
        layout.addLayout(toggles_bar)

    def _load_layers(self):
        from PySide6.QtWidgets import QTableWidgetItem
        from PySide6.QtGui import QPixmap, QIcon
        from PySide6.QtCore import Qt

        try:
            layers_info = list(self._build_layers_info_with_live_counts())
        except Exception:
            layers_info = list(getattr(self.parent_item, "layer_stats_cache", None) or [])

        _app = self.app
        active_layer = getattr(_app, "active_dxf_layer", None)
        active_file = getattr(_app, "active_dxf_filename", None)
        is_this_file_active = (active_file == self.dxf_path.name)

        search_text = self.search_box.text().strip().lower()
        sorted_layers = sorted(layers_info, key=lambda x: str(x[0]).lower())

        self.table.setRowCount(0)
        self.table.blockSignals(True)
        row_idx = 0
        for name, count, color in sorted_layers:
            name = str(name)
            if search_text and search_text not in name.lower():
                continue

            self.table.insertRow(row_idx)

            sr_item = QTableWidgetItem(str(row_idx + 1))
            sr_item.setTextAlignment(Qt.AlignCenter)
            sr_item.setFlags(sr_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row_idx, 0, sr_item)

            is_active = is_this_file_active and (active_layer == name)
            active_box = _CenteredCheckBox(self.table, hit_padding=10)
            active_box.blockSignals(True)
            active_box.setChecked(is_active)
            active_box.blockSignals(False)
            active_box.setToolTip("Set as active drawing layer")
            active_box._layer_name = name
            active_box._layer_color = color
            active_box.toggled.connect(
                lambda checked, n=name, c=color: self._on_active_box_toggled(n, c, checked)
            )
            self.table.setCellWidget(row_idx, 1, active_box)

            chk_box = _CenteredCheckBox(self.table, hit_padding=10)
            current_sel = getattr(self.parent_item, "selected_layers", None)
            checked = (current_sel is None) or (name in current_sel)
            chk_box.blockSignals(True)
            chk_box.setChecked(checked)
            chk_box.blockSignals(False)
            chk_box.setToolTip("Toggle visibility for this level")
            chk_box._layer_name = name
            chk_box.toggled.connect(
                lambda checked, n=name: self._on_show_box_toggled(n, checked)
            )
            self.table.setCellWidget(row_idx, 2, chk_box)

            color_item = QTableWidgetItem("")
            color_item.setFlags(color_item.flags() & ~Qt.ItemIsEditable & ~Qt.ItemIsSelectable)
            r, g, b = color if color else _DEFAULT_COLOR
            color_item.setBackground(QColor(int(r), int(g), int(b)))
            color_item.setToolTip(f"RGB({int(r)}, {int(g)}, {int(b)})")
            self.table.setItem(row_idx, 3, color_item)

            name_item = QTableWidgetItem(name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
            name_item.setData(Qt.UserRole, name)
            self.table.setItem(row_idx, 4, name_item)

            count_item = QTableWidgetItem(f"{int(count):,}")
            count_item.setTextAlignment(Qt.AlignCenter)
            count_item.setFlags(count_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row_idx, 5, count_item)

            row_idx += 1

        self.table.blockSignals(False)
        self._update_toggle_all_btn_text()
    def _build_layers_info_with_live_counts(self):
        attachment = self._sync_runtime_attachment_state() or self._get_live_attachment()
        parsed = _ensure_dxf_parsed(attachment or {}, self.parent_item)
        stats = {}
        ordered_names = []

        for layer in list(parsed.get("layers", []) or []):
            if not isinstance(layer, dict):
                continue
            lname = str(layer.get("name", "0") or "0")
            if lname not in stats:
                stats[lname] = {
                    "count": 0,
                    "color": tuple(layer.get("color", _DEFAULT_COLOR)) if isinstance(layer.get("color"), (list, tuple)) else _DEFAULT_COLOR,
                }
                ordered_names.append(lname)

        for ent in list(parsed.get("entities", []) or []):
            if not isinstance(ent, dict):
                continue
            lname = str(ent.get("layer", "0") or "0")
            if lname not in stats:
                stats[lname] = {"count": 0, "color": tuple(ent.get("color", _DEFAULT_COLOR)) if isinstance(ent.get("color"), (list, tuple)) else _DEFAULT_COLOR}
                ordered_names.append(lname)
            stats[lname]["count"] = int(stats[lname]["count"]) + 1

        app = self.app
        digitizer = getattr(app, "digitizer", None) if app is not None else None
        target_file = os.path.basename(str(self.dxf_path.name or "")).lower()
        active_file = os.path.basename(str(getattr(app, "active_dxf_filename", "") or "")).lower() if app is not None else ""

        if digitizer is not None:
            for d in list(getattr(digitizer, "drawings", []) or []):
                if not isinstance(d, dict) or d.get("dxf_committed"):
                    continue
                lname = str(d.get("layer", "") or "").strip()
                if not lname or lname == "DIGITIZER":
                    continue
                owner_file = os.path.basename(str(d.get("dxf_file", "") or "")).lower()
                if owner_file:
                    if owner_file != target_file:
                        continue
                elif active_file and active_file != target_file:
                    continue
                elif not active_file and lname not in stats:
                    continue

                duplicate = False
                for ent in list(parsed.get("entities", []) or []):
                    if str(ent.get("layer", "")) == lname and _same_dxf_geometry(ent, d):
                        duplicate = True
                        break
                if duplicate:
                    continue

                if lname not in stats:
                    stats[lname] = {"count": 0, "color": _extract_dxf_drawing_color(d)}
                    ordered_names.append(lname)
                stats[lname]["count"] = int(stats[lname]["count"]) + 1

        layers_info = [
            (lname, int(stats[lname]["count"]), tuple(stats[lname].get("color", _DEFAULT_COLOR)))
            for lname in ordered_names
        ]

        if self.parent_item is not None:
            try:
                self.parent_item.layer_stats_cache = list(layers_info)
                self.parent_item.cached_entities = list(parsed.get("entities", []) or [])
            except Exception:
                pass
        if isinstance(self.attachment, dict):
            self.attachment["parsed"] = parsed
            self.attachment["entities"] = parsed.get("entities", [])
            self.attachment["layer_stats"] = list(layers_info)
        return layers_info

    def _refresh_layers_on_draw(self, drawing_entry=None):
        try:
            if isinstance(drawing_entry, dict):
                active_layer = getattr(self.app, "active_dxf_layer", None) if self.app else None
                active_file = getattr(self.app, "active_dxf_filename", None) if self.app else None
                if active_layer and active_file == self.dxf_path.name:
                    parsed = _ensure_dxf_parsed(self.attachment or {}, self.parent_item)
                    drawing_entry["dxf_file"] = self.dxf_path.name
                    drawing_entry["layer"] = active_layer
                    drawing_entry["color"] = _dxf_layer_color(parsed, active_layer, _extract_dxf_drawing_color(drawing_entry))
                    drawing_entry["line_width"] = max(1.0, min(float(getattr(self.app, "active_dxf_line_width", _dxf_layer_line_width(parsed, active_layer, 2.0)) or 2.0), 20.0))
                    drawing_entry["line_style"] = _normalize_dxf_line_style(getattr(self.app, "active_dxf_line_style", _dxf_layer_line_style(parsed, active_layer, "Solid")))
                    self._apply_layer_style_to_drawing_entry(drawing_entry, drawing_entry["line_width"], drawing_entry["line_style"])
            self._build_layers_info_with_live_counts()
            self._load_layers()
        except Exception:
            pass

    def _get_layer_draw_style(self, layer_name):
        """Return saved drawing line width/style for one DXF layer."""
        layer_key = str(layer_name or "").strip()
        attachment = None
        try:
            attachment = self._sync_runtime_attachment_state()
        except Exception:
            attachment = None
        if not isinstance(attachment, dict):
            attachment = getattr(self, "attachment", None)
        parsed = _ensure_dxf_parsed(attachment or {}, self.parent_item)
        return _dxf_layer_line_width(parsed, layer_key, 2.0), _dxf_layer_line_style(parsed, layer_key, "Solid")

    def _set_layer_draw_style(self, layer_name, line_width, line_style):
        """Save style to layer metadata and to all entities already on that layer."""
        layer_key = str(layer_name or "").strip()
        if not layer_key:
            return
        try:
            width = max(1.0, min(float(line_width or 2.0), 20.0))
        except Exception:
            width = 2.0
        style = _normalize_dxf_line_style(line_style)

        attachment = None
        try:
            attachment = self._sync_runtime_attachment_state()
        except Exception:
            attachment = None
        if not isinstance(attachment, dict):
            attachment = getattr(self, "attachment", None)
        if not isinstance(attachment, dict):
            return

        parsed = _ensure_dxf_parsed(attachment, self.parent_item)
        parsed.setdefault("layers", [])
        parsed.setdefault("entities", [])

        found = False
        for layer in parsed.get("layers", []) or []:
            if not isinstance(layer, dict):
                continue
            if str(layer.get("name", "")).strip() == layer_key:
                layer["line_width"] = width
                layer["line_style"] = style
                found = True
                break
        if not found:
            parsed["layers"].append({
                "name": layer_key,
                "color": _dxf_layer_color(parsed, layer_key, _DEFAULT_COLOR),
                "line_width": width,
                "line_style": style,
            })

        for ent in parsed.get("entities", []) or []:
            if isinstance(ent, dict) and str(ent.get("layer", "")).strip() == layer_key:
                ent["line_width"] = width
                ent["line_style"] = style

        attachment["parsed"] = parsed
        attachment["entities"] = parsed.get("entities", [])
        try:
            self.parent_item.cached_parsed = parsed
            self.parent_item.cached_entities = attachment["entities"]
        except Exception:
            pass

    def _push_active_dxf_draw_style(self, layer_name, color=None):
        """Push selected DXF layer style into app + digitizer state used by draw tools."""
        if not self.app:
            return
        width, style = self._get_layer_draw_style(layer_name)
        style = _normalize_dxf_line_style(style)

        # App-level names used by different draw-tool versions.
        for attr in (
            "active_dxf_line_width", "active_snt_line_width", "active_line_width",
            "current_line_width", "draw_line_width", "line_width", "pen_width",
            "current_pen_width", "selected_line_width"
        ):
            try:
                setattr(self.app, attr, width)
            except Exception:
                pass
        for attr in (
            "active_dxf_line_style", "active_snt_line_style", "active_line_style",
            "current_line_style", "draw_line_style", "line_style", "pen_style",
            "current_pen_style", "selected_line_style"
        ):
            try:
                setattr(self.app, attr, style)
            except Exception:
                pass
        if color is not None:
            for attr in ("active_dxf_color", "active_snt_color", "active_color", "current_color", "draw_color", "pen_color", "current_pen_color"):
                try:
                    setattr(self.app, attr, color)
                except Exception:
                    pass

        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is not None:
            for attr in (
                "active_dxf_line_width", "active_snt_line_width", "active_line_width",
                "current_line_width", "draw_line_width", "line_width", "pen_width",
                "current_pen_width", "selected_line_width"
            ):
                try:
                    setattr(digitizer, attr, width)
                except Exception:
                    pass
            for attr in (
                "active_dxf_line_style", "active_snt_line_style", "active_line_style",
                "current_line_style", "draw_line_style", "line_style", "pen_style",
                "current_pen_style", "selected_line_style"
            ):
                try:
                    setattr(digitizer, attr, style)
                except Exception:
                    pass
            if color is not None:
                for attr in ("active_dxf_color", "active_snt_color", "active_color", "current_color", "draw_color", "pen_color", "current_pen_color"):
                    try:
                        setattr(digitizer, attr, color)
                    except Exception:
                        pass
        print(f"🎨 Active DXF draw style: layer={layer_name}, width={width}, style={style}")

    def _apply_layer_style_to_drawing_entry(self, drawing_entry, line_width, line_style):
        """Apply style to all actors stored inside one digitizer drawing dict."""
        if not isinstance(drawing_entry, dict):
            return
        drawing_entry["line_width"] = float(line_width)
        drawing_entry["line_style"] = _normalize_dxf_line_style(line_style)
        live_actors = []
        for key in ("actor", "text_actor", "preview_actor"):
            actor = drawing_entry.get(key)
            if actor is not None:
                live_actors.append(actor)
        for key in ("actors", "line_actors", "segment_actors", "marker_actors"):
            actor_list = drawing_entry.get(key)
            if isinstance(actor_list, (list, tuple)):
                live_actors.extend([a for a in actor_list if a is not None])
        for actor in live_actors:
            _apply_dxf_actor_line_style(actor, line_width, line_style)

    def _apply_layer_style_to_live_drawings(self, layer_name, line_width, line_style):
        """Apply edited style to currently live unsaved digitizer drawings for this DXF layer."""
        app = getattr(self, "app", None)
        digitizer = getattr(app, "digitizer", None) if app is not None else None
        if digitizer is None:
            return
        layer_key = str(layer_name or "").strip()
        target_file = os.path.basename(str(self.dxf_path.name or "")).lower()
        active_file = os.path.basename(str(getattr(app, "active_dxf_filename", "") or "")).lower()
        for d in list(getattr(digitizer, "drawings", []) or []):
            if not isinstance(d, dict):
                continue
            dlayer = str(d.get("layer") or d.get("dxf_layer") or "").strip()
            if dlayer != layer_key:
                continue
            owner_file = os.path.basename(str(d.get("dxf_file", "") or "")).lower()
            if owner_file:
                if owner_file != target_file:
                    continue
            elif active_file and active_file != target_file:
                continue
            self._apply_layer_style_to_drawing_entry(d, line_width, line_style)

    def _edit_layer_style_dialog(self, layer_name):
        """Right-click Edit dialog for line width and line style."""
        layer_key = str(layer_name or "").strip()
        if not layer_key:
            return
        old_width, old_style = self._get_layer_draw_style(layer_key)

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Edit Layer Style - {layer_key}")
        dlg.setProperty("themeStyledDialog", True)
        try:
            dlg.setStyleSheet(get_dialog_stylesheet())
        except Exception:
            pass
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(f"Layer: {layer_key}"))

        width_row = QHBoxLayout()
        width_row.addWidget(QLabel("Line Width:"))
        width_spin = QDoubleSpinBox()
        width_spin.setRange(1.0, 20.0)
        width_spin.setSingleStep(0.5)
        width_spin.setDecimals(1)
        width_spin.setValue(float(old_width))
        width_row.addWidget(width_spin)
        layout.addLayout(width_row)

        style_row = QHBoxLayout()
        style_row.addWidget(QLabel("Line Style:"))
        style_box = QComboBox()
        style_box.addItems(["Solid", "Dashed", "Dotted", "Dash-Dot"])
        idx = style_box.findText(str(old_style))
        if idx >= 0:
            style_box.setCurrentIndex(idx)
        style_row.addWidget(style_box)
        layout.addLayout(style_row)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton("Apply")
        cancel_btn = QPushButton("Cancel")
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)
        ok_btn.clicked.connect(dlg.accept)
        cancel_btn.clicked.connect(dlg.reject)

        if dlg.exec() != QDialog.Accepted:
            return

        new_width = float(width_spin.value())
        new_style = _normalize_dxf_line_style(style_box.currentText())
        self._set_layer_draw_style(layer_key, new_width, new_style)
        self._apply_layer_style_to_live_drawings(layer_key, new_width, new_style)

        if (
            getattr(self.app, "active_dxf_layer", None) == layer_key
            and getattr(self.app, "active_dxf_filename", None) == self.dxf_path.name
        ):
            self._push_active_dxf_draw_style(layer_key)

        try:
            rebuild_dxf_attachment(self.app, self.attachment)
        except Exception:
            pass
        try:
            self._load_layers()
        except Exception:
            pass
        try:
            if self.app is not None and getattr(self.app, "vtk_widget", None) is not None:
                self.app.vtk_widget.render()
        except Exception:
            pass
    def _set_active_layer(self, name, color):
        if not self.app:
            return

        line_width, line_style = self._get_layer_draw_style(name)
        line_style = _normalize_dxf_line_style(line_style)

        self.app.active_dxf_layer = name
        self.app.active_snt_layer = name
        self.app.active_dxf_filename = self.dxf_path.name
        self.app.active_snt_filename = self.dxf_path.name
        self.app.active_dxf_color = color
        self.app.active_snt_color = color
        self.app.active_sbm_class = None

        self.app.active_dxf_line_width = line_width
        self.app.active_dxf_line_style = line_style
        self.app.active_snt_line_width = line_width
        self.app.active_snt_line_style = line_style
        self._push_active_dxf_draw_style(name, color)

        print(f"🎯 Active Drawing Layer set to: {name} ({self.dxf_path.name}) Color: {color} Width: {line_width} Style: {line_style}")

        digitizer = getattr(self.app, "digitizer", None)
        if digitizer:
            # DXF now follows SNT behavior: active drawings stay live until Save.
            old_cb = getattr(self, "_auto_store_cb", None)
            if old_cb:
                try:
                    digitizer.remove_drawing_finalized_callback(old_cb)
                except Exception:
                    pass
                self._auto_store_cb = None

            old_rm_cb = getattr(self, "_auto_remove_cb", None)
            if old_rm_cb:
                try:
                    digitizer.remove_drawing_removed_callback(old_rm_cb)
                except Exception:
                    pass
            self._auto_remove_cb = self._auto_remove_drawing_from_dxf
            try:
                digitizer.add_drawing_removed_callback(self._auto_remove_cb)
            except Exception:
                pass

            old_refresh = getattr(self, "_refresh_counts_cb", None)
            if old_refresh:
                try:
                    digitizer.remove_drawing_finalized_callback(old_refresh)
                    digitizer.remove_drawing_removed_callback(old_refresh)
                except Exception:
                    pass
            self._refresh_counts_cb = self._refresh_layers_on_draw
            try:
                digitizer.add_drawing_finalized_callback(self._refresh_counts_cb)
                digitizer.add_drawing_removed_callback(self._refresh_counts_cb)
            except Exception:
                pass

        self._load_layers()
    def _clear_active_layer(self, expected_name=None):
        if not self.app:
            return

        active_file = getattr(self.app, "active_dxf_filename", None)
        active_layer = getattr(self.app, "active_dxf_layer", None)
        if active_file != self.dxf_path.name:
            return
        if expected_name is not None and active_layer != expected_name:
            return

        self.app.active_dxf_layer = None
        self.app.active_snt_layer = None
        self.app.active_dxf_filename = None
        self.app.active_snt_filename = None
        self.app.active_dxf_color = None
        self.app.active_snt_color = None
        print(f"🎯 Active DXF Layer cleared ({self.dxf_path.name})")
        self._load_layers()

    def _auto_store_drawing_to_dxf(self, drawing_entry):
        """Deprecated: DXF now follows SNT-style live-until-Save behavior."""
        if isinstance(drawing_entry, dict) and self.app is not None:
            active_layer = getattr(self.app, "active_dxf_layer", None)
            active_file = getattr(self.app, "active_dxf_filename", None)
            if active_layer and active_file == self.dxf_path.name:
                drawing_entry["dxf_file"] = self.dxf_path.name
                drawing_entry["layer"] = active_layer
        self._refresh_layers_on_draw(drawing_entry)
    def _auto_remove_drawing_from_dxf(self, drawing_entry):
        if not self.app or not isinstance(self.attachment, dict):
            return
        if isinstance(drawing_entry, dict) and drawing_entry.get("dxf_committed"):
            return
        active_file = getattr(self.app, "active_dxf_filename", None)
        owner_file = os.path.basename(str(drawing_entry.get("dxf_file", "") or "")).lower() if isinstance(drawing_entry, dict) else ""
        target_file = os.path.basename(str(self.dxf_path.name or "")).lower()
        if owner_file:
            if owner_file != target_file:
                return
        elif active_file != self.dxf_path.name:
            return

        # Unsaved active DXF drawings are live only; normally there is nothing
        # to remove from parsed. This defensive cleanup removes a matching
        # pre-commit duplicate if old code had already inserted one.
        parsed = _ensure_dxf_parsed(self.attachment, self.parent_item)
        entities = parsed.get("entities", []) or []
        layer = drawing_entry.get("layer") if isinstance(drawing_entry, dict) else None
        for i in range(len(entities) - 1, -1, -1):
            ent = entities[i]
            if layer and ent.get("layer") != layer:
                continue
            if _same_dxf_geometry(ent, drawing_entry):
                try:
                    entities.pop(i)
                except Exception:
                    pass
                break
        self.attachment["entities"] = entities
        self._refresh_layers_on_draw(drawing_entry)
    def _on_active_box_toggled(self, name: str, color, checked: bool) -> None:
        if not checked:
            return
        self._set_active_layer(name, color or _DEFAULT_COLOR)

    def _on_show_box_toggled(self, name: str, checked: bool) -> None:
        self._on_visibility_toggled(name, checked)

    def _on_cell_changed(self, row, column):
        item = self.table.item(row, column)
        if item is None:
            return
        name = item.data(Qt.UserRole)
        if not name:
            return
        checked = item.checkState() == Qt.Checked

        if column == 1:
            if checked:
                color = item.data(Qt.UserRole + 1)
                self._set_active_layer(name, color or _DEFAULT_COLOR)
        elif column == 2:
            self._on_visibility_toggled(name, checked)

    def _on_visibility_toggled(self, name, checked):
        current_sel = getattr(self.parent_item, "selected_layers", None)
        if current_sel is None:
            all_layers = {x[0] for x in getattr(self.parent_item, "layer_stats_cache", [])}
            current_sel = set(all_layers)
        else:
            current_sel = set(current_sel)
            
        if checked:
            current_sel.add(name)
        else:
            current_sel.discard(name)
            
        total_layers = len(getattr(self.parent_item, "layer_stats_cache", []))
        self.parent_item._apply_layer_selection(current_sel, total_layers)
        self._update_toggle_all_btn_text()

    def _change_layer_color(self, name, color):
        from PySide6.QtWidgets import QColorDialog

        init_color = QColor(*color) if color else QColor(255, 255, 255)
        new_color = QColorDialog.getColor(init_color, self, f"Select Color for Layer: {name}")
        if not new_color.isValid():
            return

        r, g, b = new_color.red(), new_color.green(), new_color.blue()
        rgb_tuple = (r, g, b)
        name_key = str(name or "").strip()

        attachment = None
        try:
            attachment = self._sync_runtime_attachment_state()
        except Exception:
            attachment = None
        if not isinstance(attachment, dict):
            attachment = getattr(self, "attachment", None)
        if not isinstance(attachment, dict):
            return

        parsed = _ensure_dxf_parsed(attachment, self.parent_item)
        parsed.setdefault("layers", [])
        parsed.setdefault("entities", [])

        found_layer = False
        for layer in parsed.get("layers", []) or []:
            if not isinstance(layer, dict):
                continue
            if str(layer.get("name", "")).strip() == name_key:
                layer["color"] = rgb_tuple
                found_layer = True
                break

        if not found_layer:
            parsed["layers"].append({"name": name_key, "color": rgb_tuple})
            try:
                self.newly_created_layers.add(name_key)
            except Exception:
                pass

        for ent in parsed.get("entities", []) or []:
            if isinstance(ent, dict) and str(ent.get("layer", "")).strip() == name_key:
                ent["color"] = rgb_tuple

        attachment["parsed"] = parsed
        attachment["entities"] = parsed.get("entities", [])

        if self.parent_item is not None:
            try:
                self.parent_item.cached_entities = parsed.get("entities", [])
                self.parent_item.cached_parsed = parsed
            except Exception:
                pass

            try:
                if hasattr(self.parent_item, "_rebuild_layer_stats_cache_from_parsed"):
                    self.parent_item.layer_stats_cache = self.parent_item._rebuild_layer_stats_cache_from_parsed()
                else:
                    old_cache = list(getattr(self.parent_item, "layer_stats_cache", []) or [])
                    updated = False
                    new_cache = []
                    for lname, count, old_color in old_cache:
                        if str(lname).strip() == name_key:
                            new_cache.append((lname, count, rgb_tuple))
                            updated = True
                        else:
                            new_cache.append((lname, count, old_color))
                    if not updated:
                        new_cache.append((name_key, 0, rgb_tuple))
                    self.parent_item.layer_stats_cache = new_cache
            except Exception:
                pass

        # Update saved/rendered DXF actors immediately.
        actors = []
        try:
            actors = self.parent_item._get_dxf_layer_runtime_actors(name_key) if self.parent_item is not None else []
        except Exception:
            actors = []

        for actor in list(actors or []):
            try:
                if hasattr(actor, "GetProperty"):
                    actor.GetProperty().SetColor(r / 255.0, g / 255.0, b / 255.0)
                if hasattr(actor, "GetTextProperty"):
                    actor.GetTextProperty().SetColor(r / 255.0, g / 255.0, b / 255.0)
                actor._original_color = rgb_tuple
            except Exception:
                pass

        # Update live unsaved digitizer drawings on this DXF level immediately.
        _app = self._resolve_app()
        digitizer = getattr(_app, "digitizer", None) if _app is not None else None
        target_file = os.path.basename(str(self.dxf_path.name or "")).lower()
        active_file = os.path.basename(str(getattr(_app, "active_dxf_filename", "") or "")).lower() if _app is not None else ""

        if digitizer is not None:
            for d in list(getattr(digitizer, "drawings", []) or []):
                if not isinstance(d, dict):
                    continue
                dlayer = str(d.get("layer") or d.get("dxf_layer") or "").strip()
                if dlayer != name_key:
                    continue
                owner_file = os.path.basename(str(d.get("dxf_file", "") or "")).lower()
                if owner_file:
                    if owner_file != target_file:
                        continue
                elif active_file and active_file != target_file:
                    continue

                d["color"] = rgb_tuple
                d["original_color"] = (r / 255.0, g / 255.0, b / 255.0)
                d["dxf_file"] = self.dxf_path.name
                d["layer"] = name_key

                live_actors = []
                for key in ("actor", "text_actor", "preview_actor"):
                    actor = d.get(key)
                    if actor is not None:
                        live_actors.append(actor)
                for key in ("actors", "line_actors", "segment_actors", "marker_actors"):
                    actor_list = d.get(key)
                    if isinstance(actor_list, (list, tuple)):
                        live_actors.extend([a for a in actor_list if a is not None])

                for actor in live_actors:
                    try:
                        if hasattr(actor, "GetProperty"):
                            actor.GetProperty().SetColor(r / 255.0, g / 255.0, b / 255.0)
                        if hasattr(actor, "GetTextProperty"):
                            actor.GetTextProperty().SetColor(r / 255.0, g / 255.0, b / 255.0)
                        actor._original_color = rgb_tuple
                    except Exception:
                        pass

        if _app is not None:
            active_layer = getattr(_app, "active_dxf_layer", None)
            active_file_name = getattr(_app, "active_dxf_filename", None)
            if active_file_name == self.dxf_path.name and active_layer == name_key:
                _app.active_dxf_color = rgb_tuple
                _app.active_snt_color = rgb_tuple

        self._update_stats_cache_color(name_key, rgb_tuple)

        try:
            self._load_layers()
        except Exception:
            pass

        try:
            if _app is not None and getattr(_app, "vtk_widget", None) is not None:
                _app.vtk_widget.render()
        except Exception:
            pass

    def _update_stats_cache_color(self, name, new_color):
        if self.parent_item and hasattr(self.parent_item, "layer_stats_cache"):
            cache = list(self.parent_item.layer_stats_cache or [])
            updated = False
            for i, (lname, count, col) in enumerate(cache):
                if str(lname).strip() == str(name).strip():
                    cache[i] = (lname, count, new_color)
                    updated = True
                    break
            if not updated:
                cache.append((name, 0, new_color))
            self.parent_item.layer_stats_cache = cache

    def _on_table_double_clicked(self, item):
        if item.column() == 4:
            old_name = item.data(Qt.UserRole)
            self._rename_layer_dialog(old_name)

    def _set_dxf_rename_shortcut_guard(self, enabled: bool) -> None:
        try:
            app = self.app
            if app is None:
                return
            app._dxf_layer_rename_edit_active = bool(enabled)
        except Exception:
            pass

    def _rename_layer_dialog(self, old_name):
        from PySide6.QtWidgets import QInputDialog, QLineEdit

        rename_dlg = QInputDialog(self)
        rename_dlg.setWindowTitle("Rename Layer")
        rename_dlg.setMinimumWidth(380)
        rename_dlg.setLabelText(f"Rename layer '{old_name}' to:")
        rename_dlg.setTextValue(str(old_name or ""))
        rename_dlg.setOkButtonText("OK")
        rename_dlg.setCancelButtonText("Cancel")

        line_edit = rename_dlg.findChild(QLineEdit)
        if line_edit is not None:
            line_edit.selectAll()
            line_edit.setFocus(Qt.OtherFocusReason)

        self._set_dxf_rename_shortcut_guard(True)
        try:
            ok = bool(rename_dlg.exec())
        finally:
            self._set_dxf_rename_shortcut_guard(False)

        if not ok:
            return

        new_name = str(rename_dlg.textValue() or "").strip()
        if not new_name:
            return

        if new_name == old_name:
            return

        all_names = {x[0] for x in getattr(self.parent_item, "layer_stats_cache", [])}
        if new_name in all_names:
            QMessageBox.warning(self, "Rename Layer", f"Layer '{new_name}' already exists.")
            return

        entities = None
        if isinstance(self.attachment, dict):
            entities_candidate = self.attachment.get("entities")
            if isinstance(entities_candidate, list):
                entities = entities_candidate
        if entities is None and self.parent_item is not None:
            entities_candidate = getattr(self.parent_item, "cached_entities", None)
            if isinstance(entities_candidate, list):
                entities = entities_candidate

        if entities is None:
            QMessageBox.warning(self, "Rename Layer", "Layer data is unavailable for rename.")
            return

        for ent in entities:
            if ent.get("layer") == old_name:
                ent["layer"] = new_name

        actor_cache = getattr(self.parent_item, "actor_cache", None)
        if isinstance(actor_cache, dict) and old_name in actor_cache:
            actors = list(actor_cache.pop(old_name) or [])
            for actor in actors:
                actor._naksha_snt_layer = new_name
            existing = list(actor_cache.get(new_name, []) or [])
            actor_cache[new_name] = existing + actors

        current_sel = getattr(self.parent_item, "selected_layers", None)
        if current_sel is not None:
            if not isinstance(current_sel, set):
                try:
                    current_sel = set(current_sel)
                except Exception:
                    current_sel = set()
                self.parent_item.selected_layers = current_sel
            if old_name in current_sel:
                current_sel.discard(old_name)
                current_sel.add(new_name)

        app = self.app
        if app is not None:
            active_layer = getattr(app, "active_dxf_layer", None)
            active_file = getattr(app, "active_dxf_filename", None)
            if active_file == self.dxf_path.name and active_layer == old_name:
                app.active_dxf_layer = new_name
                app.active_snt_layer = new_name

        if hasattr(self.parent_item, "actor_cache"):
            self.attachment["actor_cache_map"] = self.parent_item.actor_cache

        rebuild_dxf_attachment(app, self.attachment)
        self._load_layers()

    def _commit_pending_drawings_to_dxf(self):
        attachment = self.attachment
        if not self.app or not isinstance(attachment, dict):
            return 0
        parsed = _ensure_dxf_parsed(attachment, self.parent_item)
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            return 0

        target_file = os.path.basename(str(self.dxf_path.name or "")).lower()
        layer_names = {
            str(l.get("name", ""))
            for l in parsed.get("layers", []) or []
            if isinstance(l, dict)
        }
        if "entities" not in parsed or not isinstance(parsed.get("entities"), list):
            parsed["entities"] = []

        committed = 0
        for d in list(getattr(digitizer, "drawings", []) or []):
            if not isinstance(d, dict):
                continue
            if d.get("dxf_committed"):
                continue
            owner_file = os.path.basename(str(d.get("dxf_file", "") or "")).lower()
            active_file = os.path.basename(str(getattr(self.app, "active_dxf_filename", "") or "")).lower()
            if owner_file:
                if owner_file != target_file:
                    continue
            elif active_file and active_file != target_file:
                continue

            layer = str(d.get("layer", "") or "").strip()
            if not layer or layer == "DIGITIZER":
                continue
            if layer_names and layer not in layer_names:
                continue

            duplicate = False
            for ent in parsed["entities"]:
                if str(ent.get("layer", "")) == layer and _same_dxf_geometry(ent, d):
                    duplicate = True
                    break
            if duplicate:
                d["dxf_committed"] = True
                d["dxf_stored"] = True
                d["dxf_file"] = self.dxf_path.name
                continue

            layer_color = _dxf_layer_color(parsed, layer, _extract_dxf_drawing_color(d))
            d["line_width"] = max(1.0, min(float(d.get("line_width", _dxf_layer_line_width(parsed, layer, 2.0)) or 2.0), 20.0))
            d["line_style"] = _normalize_dxf_line_style(d.get("line_style", _dxf_layer_line_style(parsed, layer, "Solid")))
            ent = _build_dxf_entity_from_drawing(d, layer, layer_color)
            if ent is None:
                continue
            parsed["entities"].append(ent)
            d["dxf_committed"] = True
            d["dxf_stored"] = True
            d["dxf_file"] = self.dxf_path.name
            committed += 1

        attachment["parsed"] = parsed
        attachment["entities"] = parsed.get("entities", [])
        try:
            self.parent_item.cached_entities = attachment["entities"]
        except Exception:
            pass
        self._build_layers_info_with_live_counts()
        return committed

    def _save_layers_to_disk(self):
        """Save all levels/elements to the DXF file on disk (manual Save button)."""
        try:
            from PySide6.QtWidgets import QMessageBox
        except ImportError:
            from PyQt5.QtWidgets import QMessageBox

        if not isinstance(self.attachment, dict):
            QMessageBox.warning(self, "Save DXF", "Could not resolve the live DXF attachment for this file.")
            return

        try:
            n = self._commit_pending_drawings_to_dxf()
            if n:
                print(f"💾 Committed {n} new drawing(s) to DXF before save")
        except Exception as exc:
            print(f"⚠️ Failed to commit drawings before save: {exc}")

        ok = False
        try:
            ok = save_dxf_attachment_to_disk(self.attachment)
        except Exception as exc:
            print(f"⚠️ Failed to save DXF: {exc}")
            ok = False

        path = ""
        try:
            path = self.attachment.get("full_path", "") or ""
        except Exception:
            path = ""

        if ok:
            digitizer = getattr(self.app, "digitizer", None)
            if digitizer is not None:
                try:
                    if hasattr(digitizer, "undo_stack"):
                        digitizer.undo_stack.clear()
                    if hasattr(digitizer, "redo_stack"):
                        digitizer.redo_stack.clear()
                except Exception:
                    pass
            QMessageBox.information(self, "Save DXF", f"Saved successfully.\n{path}")
        else:
            QMessageBox.warning(self, "Save DXF", "Could not save the DXF file.")
    def _add_layer_dialog(self):
        from PySide6.QtWidgets import QInputDialog, QColorDialog
        
        rename_dlg = QInputDialog(self)
        rename_dlg.setWindowTitle("Add Level")
        rename_dlg.setLabelText("Enter name for new level:")
        rename_dlg.setMinimumWidth(380)
        rename_dlg.setOkButtonText("OK")
        rename_dlg.setCancelButtonText("Cancel")
        
        self._set_dxf_rename_shortcut_guard(True)
        try:
            ok = bool(rename_dlg.exec())
            name = rename_dlg.textValue()
        finally:
            self._set_dxf_rename_shortcut_guard(False)
        if not ok or not name.strip():
            return
            
        name = name.strip()
        all_names = {x[0] for x in getattr(self.parent_item, "layer_stats_cache", [])}
        if name in all_names:
            QMessageBox.warning(self, "Add Level", f"Level '{name}' already exists.")
            return
            
        color = QColorDialog.getColor(QColor(255, 255, 255), self, "Select Level Color")
        if not color.isValid():
            return
            
        rgb_tuple = (color.red(), color.green(), color.blue())
        
        attachment = self._sync_runtime_attachment_state() or self._get_live_attachment()
        if isinstance(attachment, dict):
            parsed = _ensure_dxf_parsed(attachment, self.parent_item)
            layers = parsed.setdefault("layers", [])
            if not any(isinstance(l, dict) and l.get("name") == name for l in layers):
                layers.append({"name": name, "color": rgb_tuple})

            if hasattr(self.parent_item, "actor_cache"):
                self.parent_item.actor_cache.setdefault(name, [])
                attachment["actor_cache_map"] = _normalize_dxf_actor_cache(self.parent_item, attachment)

            if hasattr(self.parent_item, "layer_stats_cache"):
                cache = list(self.parent_item.layer_stats_cache or [])
                if not any(x[0] == name for x in cache):
                    cache.append((name, 0, rgb_tuple))
                self.parent_item.layer_stats_cache = cache

            current_sel = getattr(self.parent_item, "selected_layers", None)
            if current_sel is not None:
                current_sel.add(name)

            attachment["parsed"] = parsed
            attachment["entities"] = parsed.get("entities", [])
            attachment["layer_stats"] = _build_dxf_layer_stats_from_entities(attachment["entities"], parsed.get("layers", []))
            self.attachment = attachment
            self._load_layers()

    def _merge_move_dialog(self, source_layer=None):
        if not source_layer:
            curr_row = self.table.currentRow()
            if curr_row < 0:
                QMessageBox.warning(self, "Move Level Elements", "Please select a level first.")
                return
            item = self.table.item(curr_row, 4)
            if not item:
                return
            source_layer = item.data(Qt.UserRole)

        all_layers = [x[0] for x in getattr(self.parent_item, "layer_stats_cache", [])]
        other_layers = [x for x in all_layers if x != source_layer]
        if not other_layers:
            QMessageBox.warning(self, "Move Level Elements", "No other levels are available to move elements to.")
            return

        dlg = DXFMoveElementsDialog(
            source_layer=source_layer,
            parent_item=self.parent_item,
            attachment=self.attachment,
            parent=self,
        )
        if dlg.exec():
            self._load_layers()

    def _import_drawings_dialog(self):
        # --- collect digitizer drawings ---
        digitizer = getattr(self.app, "digitizer", None)
        drawings = list(getattr(digitizer, "drawings", []) or []) if digitizer else []

        # --- also collect visible SNT entities and convert them to drawing-like dicts ---
        try:
            from gui.vector_export import _collect_visible_snt_entities_for_export
            snt_entities = _collect_visible_snt_entities_for_export(self.app)
            for ent in snt_entities:
                # Normalise to the same shape digitize_tools uses
                drawing_dict = {
                    "type":        ent.get("entity_type", "polyline").lower(),
                    "coordinates": ent.get("points") or ent.get("coords") or [],
                    "color":       ent.get("color", (255, 255, 255)),
                    "layer":       ent.get("layer", "0"),
                    "closed":      ent.get("closed", False),
                }
                drawings.append(drawing_dict)
        except Exception as _exc:
            print(f"   ⚠️ Could not collect SNT entities for DXF import: {_exc}")

        if not drawings:
            QMessageBox.warning(
                self, "Import Drawings",
                "No digitized drawings found.\n"
                "Please draw some vectors/lines first using the digitizing tools."
            )
            return
            
        all_layers = sorted([x[0] for x in getattr(self.parent_item, "layer_stats_cache", [])])
        options = ["Use Drawing's Own Level"] + all_layers + ["Create New Level..."]
        
        item_dlg = QInputDialog(self)
        item_dlg.setWindowTitle("Import Drawings into DXF")
        item_dlg.setLabelText(f"Select target DXF Level for {len(drawings)} drawing(s):")
        item_dlg.setComboBoxItems(options)
        item_dlg.setComboBoxEditable(False)
        item_dlg.setMinimumWidth(380)
        item_dlg.setOkButtonText("OK")
        item_dlg.setCancelButtonText("Cancel")
        
        ok = bool(item_dlg.exec())
        target_opt = item_dlg.textValue()
        if not ok or not target_opt:
            return
            
        if target_opt == "Create New Level...":
            new_level_dlg = QInputDialog(self)
            new_level_dlg.setWindowTitle("Add Level")
            new_level_dlg.setLabelText("Enter name for new level:")
            new_level_dlg.setMinimumWidth(380)
            new_level_dlg.setOkButtonText("OK")
            new_level_dlg.setCancelButtonText("Cancel")
            
            self._set_dxf_rename_shortcut_guard(True)
            try:
                ok = bool(new_level_dlg.exec())
                new_name = new_level_dlg.textValue()
            finally:
                self._set_dxf_rename_shortcut_guard(False)
            if not ok or not new_name.strip():
                return
            new_name = new_name.strip()
            if new_name in all_layers:
                QMessageBox.warning(self, "Add Level", f"Level '{new_name}' already exists.")
                return
            color = QColorDialog.getColor(QColor(255, 255, 255), self, "Select Level Color")
            if not color.isValid():
                return
            rgb_tuple = (color.red(), color.green(), color.blue())
            
            if hasattr(self.parent_item, "layer_stats_cache"):
                self.parent_item.layer_stats_cache.append((new_name, 0, rgb_tuple))
            current_sel = getattr(self.parent_item, "selected_layers", None)
            if current_sel is not None:
                current_sel.add(new_name)
            chosen_layer = new_name
        else:
            chosen_layer = target_opt
            
        if self.attachment and "entities" in self.attachment:
            entities = self.attachment["entities"]
            
            def _lookup_layer_color(target_layer):
                target_lower = str(target_layer).strip().lower()
                for lname, count, clr in getattr(self.parent_item, "layer_stats_cache", []):
                    if str(lname).strip().lower() == target_lower:
                        return clr
                return None

            def _extract_drawing_color(drawing):
                c = drawing.get("color")
                if isinstance(c, (list, tuple)) and len(c) >= 3:
                    return (int(c[0]), int(c[1]), int(c[2]))
                return (255, 255, 255)

            entities_added = 0
            for drawing in list(drawings):
                layer_name = chosen_layer
                use_own_level = (chosen_layer == "Use Drawing's Own Level")
                if use_own_level:
                    layer_name = drawing.get("layer") or "DIGITIZER"
                    layer_exists = any(x[0] == layer_name for x in getattr(self.parent_item, "layer_stats_cache", []))
                    if not layer_exists:
                        extracted_color = _extract_drawing_color(drawing)
                        if hasattr(self.parent_item, "layer_stats_cache"):
                            self.parent_item.layer_stats_cache.append((layer_name, 0, extracted_color))

                layer_color = _lookup_layer_color(layer_name)
                color_val = layer_color if layer_color is not None else _extract_drawing_color(drawing)
                shape_type = str(drawing.get("type", "polyline")).lower()
                coords = drawing.get("coords") or drawing.get("coordinates") or []
                
                clean_coords = []
                for pt in coords:
                    x = float(pt[0])
                    y = float(pt[1])
                    z = float(pt[2]) if len(pt) > 2 else 0.0
                    clean_coords.append((x, y, z))
                    
                if not clean_coords:
                    continue
                    
                if shape_type == "text":
                    font_size = drawing.get("font_size")
                    if not font_size:
                        actor = drawing.get("actor")
                        if actor is not None:
                            try:
                                if hasattr(actor, "GetTextProperty"):
                                    font_size = float(actor.GetTextProperty().GetFontSize())
                                elif hasattr(actor, "_text_font_size"):
                                    font_size = float(actor._text_font_size)
                            except Exception:
                                font_size = None
                    try:
                        font_size = float(font_size) if font_size else 18.0
                    except (TypeError, ValueError):
                        font_size = 18.0
                    if font_size <= 0:
                        font_size = 18.0

                    height = drawing.get("height")
                    if height is not None:
                        try:
                            height = float(height)
                        except (TypeError, ValueError):
                            height = None
                    if height is None:
                        height = font_size * 0.15

                    ent = {
                        "type": "text",
                        "layer": layer_name,
                        "color": color_val,
                        "position": clean_coords[0],
                        "text": drawing.get("text", "Text"),
                        "height": height,
                        "rotation": 0.0,
                        "font_size": font_size,
                    }
                else:
                    closed = shape_type in ("polygon", "rectangle", "circle", "polyline")
                    ent = {
                        "type": "polyline",
                        "layer": layer_name,
                        "color": color_val,
                        "points": clean_coords,
                        "closed": closed
                    }
                    
                entities.append(ent)
                entities_added += 1
                
            rebuild_dxf_attachment(self.app, self.attachment)
            digitizer.clear_drawings()
            self._load_layers()
            
            QMessageBox.information(
                self, "Import Complete",
                f"Successfully imported {entities_added} drawings into DXF level(s).\n"
            )

    def _on_table_context_menu(self, pos):
        item = self.table.itemAt(pos)
        if item is None:
            return
        row = item.row()
        name_item = self.table.item(row, 4)
        if name_item is None:
            return
            
        layer_name = name_item.data(Qt.UserRole)
        layer_count = 0
        layer_color = (255, 255, 255)
        for name, count, color in getattr(self.parent_item, "layer_stats_cache", []):
            if name == layer_name:
                layer_count = count
                layer_color = color
                break
                
        menu = QMenu(self)
        
        act_action = menu.addAction(f"Set Active Layer: {layer_name}")
        menu.addSeparator()
        
        hl_action = menu.addAction(f"Highlight Layer: {layer_name}")
        iso_action = menu.addAction(f"Isolate Layer: {layer_name}")
        menu.addSeparator()
        
        rename_action = menu.addAction("Rename Layer...")
        color_action = menu.addAction("Change Color...")
        edit_action = menu.addAction("Edit...")
        move_action = menu.addAction("Move/Merge Elements...")
        manage_action = menu.addAction("Manage Drawings / Elements...")
        del_action = menu.addAction(f"Delete Layer: {layer_name}")
        menu.addSeparator()
        
        clear_action = menu.addAction("Clear Highlight / Restore Colors")
        
        if layer_count <= 0:
            hl_action.setEnabled(False)
            iso_action.setEnabled(False)
            manage_action.setEnabled(False)
            
        picked = menu.exec(self.table.viewport().mapToGlobal(pos))
        if picked == act_action:
            self._set_active_layer(layer_name, layer_color)
        elif picked == hl_action:
            self.parent_item.highlight_layer(layer_name, layer_count=layer_count)
        elif picked == iso_action:
            self.parent_item.isolate_layer(layer_name, layer_count=layer_count)
        elif picked == rename_action:
            self._rename_layer_dialog(layer_name)
        elif picked == color_action:
            self._change_layer_color(layer_name, layer_color)
        elif picked == edit_action:
            self._edit_layer_style_dialog(layer_name)
        elif picked == move_action:
            self._merge_move_dialog(layer_name)
        elif picked == manage_action:
            self._manage_drawings_dialog(layer_name)
        elif picked == del_action:
            self._delete_layer_with_confirmation(layer_name, layer_count)
        elif picked == clear_action:
            self.parent_item.clear_layer_focus()

    def _delete_layer_with_confirmation(self, layer_name: str, layer_count: int) -> None:
        if self.parent_item is None:
            return
        layer_key = str(layer_name or "").strip()
        if not layer_key:
            return

        count_text = f"{max(0, int(layer_count)):,} entities" if layer_count is not None else "entities"
        reply = QMessageBox.warning(
            self,
            "Delete Layer",
            "Delete this layer from the current attached DXF session?\n\n"
            f"Layer: {layer_key}\n"
            f"Contains: {count_text}\n\n"
            "This removes its vectors from main view permanently.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            self.parent_item.clear_layer_focus(render=False)
        except Exception:
            pass

        app = getattr(self, "app", None)
        renderer = getattr(getattr(app, "vtk_widget", None), "renderer", None) if app is not None else None
        cam_state = _dxf_capture_camera_state(renderer)

        ok = False
        try:
            if renderer is not None:
                try:
                    setattr(renderer, "_skip_camera_reset", True)
                except Exception:
                    pass
            ok = bool(self.parent_item.delete_layer(layer_key, layer_count=layer_count, confirm=False))
        except Exception as exc:
            QMessageBox.warning(self, "Delete Failed", f"Unable to delete layer '{layer_key}'.\n\n{exc}")
            self._load_layers()
            return
        finally:
            try:
                _dxf_restore_camera_state_delayed(app, renderer, cam_state)
            except Exception:
                pass
            if renderer is not None:
                try:
                    setattr(renderer, "_skip_camera_reset", False)
                except Exception:
                    pass

        if ok:
            try:
                self._clear_active_layer(expected_name=layer_key)
            except Exception:
                pass

        self._load_layers()
        try:
            _dxf_restore_camera_state_delayed(app, renderer, cam_state)
        except Exception:
            pass

    def _manage_drawings_dialog(self, layer_name):
        if self.parent_item is None:
            return

        layer_key = str(layer_name or "").strip()
        if not layer_key:
            return

        if not hasattr(self, "_manage_drawings_dialog_refs"):
            self._manage_drawings_dialog_refs = {}

        existing = self._manage_drawings_dialog_refs.get(layer_key)
        if existing is not None:
            try:
                existing.show()
                existing.raise_()
                existing.activateWindow()
                return
            except (RuntimeError, ReferenceError):
                self._manage_drawings_dialog_refs.pop(layer_key, None)

        dlg = DXFLayerDrawingsDialog(layer_key, self.parent_item, self)
        dlg.setModal(False)
        dlg.setWindowModality(Qt.NonModal)
        dlg.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        def _cleanup_dialog_ref(*_args):
            try:
                self._manage_drawings_dialog_refs.pop(layer_key, None)
            except Exception:
                pass
            try:
                self._load_layers()
            except Exception:
                pass

        try:
            dlg.finished.connect(_cleanup_dialog_ref)
        except Exception:
            pass

        self._manage_drawings_dialog_refs[layer_key] = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        dlg.raise_()
        dlg.activateWindow()

    def _toggle_all(self):
        # Determine if all levels are currently visible
        all_visible = True
        for i in range(self.table.rowCount()):
            w = self.table.cellWidget(i, 2)
            if w and hasattr(w, 'isChecked') and not w.isChecked():
                all_visible = False
                break
        
        # Toggle: if all are visible, turn all off. Otherwise, turn all on.
        target_state = not all_visible
        for i in range(self.table.rowCount()):
            w = self.table.cellWidget(i, 2)
            if w and hasattr(w, 'setChecked'):
                w.setChecked(target_state)
        
        self._update_toggle_all_btn_text()

    def _update_toggle_all_btn_text(self):
        all_visible = True
        for i in range(self.table.rowCount()):
            w = self.table.cellWidget(i, 2)
            if w and hasattr(w, 'isChecked') and not w.isChecked():
                all_visible = False
                break
        if hasattr(self, 'toggle_all_btn'):
            self.toggle_all_btn.setText("All Off" if all_visible else "All On")

    def _select_all(self):
        for i in range(self.table.rowCount()):
            w = self.table.cellWidget(i, 2)
            if w and hasattr(w, 'setChecked'):
                w.setChecked(True)

    def _deselect_all(self):
        for i in range(self.table.rowCount()):
            w = self.table.cellWidget(i, 2)
            if w and hasattr(w, 'setChecked'):
                w.setChecked(False)

    def _invert(self):
        for i in range(self.table.rowCount()):
            w = self.table.cellWidget(i, 2)
            if w and hasattr(w, 'isChecked'):
                w.setChecked(not w.isChecked())

    def get_selected_layers(self) -> Set[str]:
        selected = set()
        for i in range(self.table.rowCount()):
            w = self.table.cellWidget(i, 2)
            if w and hasattr(w, 'isChecked') and w.isChecked():
                name_item = self.table.item(i, 4)
                if name_item:
                    selected.add(name_item.data(Qt.UserRole))
        return selected


class DXFDisplayOptionsDialog(QDialog):
    """Per-file display options (overlay / underlay + color override)."""

    def __init__(self, parent=None, mode="overlay", override_enabled=False, override_color=(255, 0, 0)):
        super().__init__(parent)
        self.setWindowTitle("DXF Display Options")
        self.setModal(True)
        self.resize(260, 200)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # Mode
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Display Mode:"))
        self.overlay_radio = QRadioButton("Overlay (on top)")
        self.underlay_radio = QRadioButton("Underlay (below)")
        if mode == "underlay":
            self.underlay_radio.setChecked(True)
        else:
            self.overlay_radio.setChecked(True)
        mode_row.addWidget(self.overlay_radio)
        mode_row.addWidget(self.underlay_radio)
        layout.addLayout(mode_row)

        # Color override
        color_row = QHBoxLayout()
        self.color_override_check = QCheckBox("Override color:")
        self.color_combo = QComboBox()
        self.color_combo.addItem("Red",    QColor(255, 0, 0))
        self.color_combo.addItem("Green",  QColor(0, 255, 0))
        self.color_combo.addItem("Blue",   QColor(0, 0, 255))
        self.color_combo.addItem("Yellow", QColor(255, 255, 0))
        self.color_combo.addItem("Cyan",   QColor(0, 255, 255))
        self.color_combo.addItem("Magenta",QColor(255, 0, 255))
        self.color_combo.addItem("White",  QColor(255, 255, 255))

        self.color_override_check.setChecked(override_enabled)
        self.color_combo.setEnabled(override_enabled)
        self.color_override_check.toggled.connect(self.color_combo.setEnabled)

        # Select initial color
        for i in range(self.color_combo.count()):
            q = self.color_combo.itemData(i)
            if (q.red(), q.green(), q.blue()) == override_color:
                self.color_combo.setCurrentIndex(i)
                break

        color_row.addWidget(self.color_override_check)
        color_row.addWidget(self.color_combo)
        layout.addLayout(color_row)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton("OK")
        cancel_btn = QPushButton("Cancel")
        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def get_values(self):
        mode = "underlay" if self.underlay_radio.isChecked() else "overlay"
        override_enabled = self.color_override_check.isChecked()
        qcolor = self.color_combo.currentData()
        override_color = (qcolor.red(), qcolor.green(), qcolor.blue())
        return mode, override_enabled, override_color
    
    
class DXFLoadWorker(QThread):
    """Background thread for loading DXF files one at a time"""
    progress = Signal(int, str, bool)  # (current_value, status_message, is_indeterminate)
    file_loaded = Signal(object, object)  # (item_data, None)
    finished = Signal()
    error = Signal(str)
    
    def __init__(self, file_paths):
        super().__init__()
        self.file_paths = file_paths
        self._is_cancelled = False
    
    def cancel(self):
        self._is_cancelled = True
    
    def run(self):
        """Load files in background"""
        try:
            import ezdxf
            from pyproj import CRS
            
            total_files = len(self.file_paths)
            
            for idx, file_path in enumerate(self.file_paths):
                if self._is_cancelled:
                    return
                
                dxf_path = Path(file_path)
                filename = dxf_path.name
                
                if total_files == 1:
                    # Indeterminate progress (pulsing bar)
                    self.progress.emit(0, f"📂 Reading {filename}...", True)
                else:
                    # Regular progress
                    self.progress.emit(idx, f"📂 Loading {filename}... ({idx + 1}/{total_files})", False)
                
                # Check for PRJ file
                prj_path = dxf_path.with_suffix('.prj')
                if not prj_path.exists():
                    prj_path = dxf_path.with_suffix('.PRJ')
                prj_exists = prj_path.exists()
                
                item_data = {
                    'dxf_path': dxf_path,
                    'prj_exists': prj_exists,
                    'prj_path': prj_path if prj_exists else None
                }
                
                try:
                    # ✅ Show "Reading file..." for single file
                    if total_files == 1:
                        self.progress.emit(0, f"📂 Reading {filename}...", True)
                    
                    # Load DXF
                    dxf_doc = ezdxf.readfile(str(dxf_path))
                    
                    # ✅ Show "Counting entities..." for single file
                    if total_files == 1:
                        self.progress.emit(0, f"⏳ Counting entities in {filename}...", True)
                    
                    modelspace = dxf_doc.modelspace()
                    entity_count = len(list(modelspace))
                    
                    item_data['dxf_doc'] = dxf_doc
                    item_data['entity_count'] = entity_count
                    
                    # ✅ Show "Parsing CRS..." for single file
                    if total_files == 1 and prj_exists:
                        self.progress.emit(0, f"🗺️ Parsing coordinate system...", True)
                    
                    # Parse PRJ if exists
                    if prj_exists:
                        try:
                            with open(prj_path, 'r') as f:
                                prj_content = f.read().strip()
                            crs = CRS.from_wkt(prj_content)
                            item_data['crs'] = crs
                        except Exception as e:
                            print(f"  ⚠️ PRJ parse failed: {e}")
                            item_data['crs'] = None
                    else:
                        item_data['crs'] = None
                    
                    self.file_loaded.emit(item_data, None)
                    
                except Exception as e:
                    print(f"  ⚠️ Failed to load {filename}: {e}")
                    item_data['error'] = str(e)
                    self.file_loaded.emit(item_data, None)
            
            self.finished.emit()
            
        except Exception as e:
            self.error.emit(f"Loading failed: {str(e)}")
