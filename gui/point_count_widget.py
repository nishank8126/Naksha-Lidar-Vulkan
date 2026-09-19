from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QFrame, QPushButton
)
from PySide6.QtCore import Qt, QTimer, QEvent, QPoint
from PySide6.QtGui import QFont
import numpy as np

_POINT_STATS_DEBUG = False


def _debug_log(message: str) -> None:
    if _POINT_STATS_DEBUG:
        print(message)


class PointCountWidget(QWidget):
    """
    Simple, clean floating statistics widget.
    ✅ FIXED: Now properly displays LVL (Level/Class Name) from Display Mode
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.app = None
        self.update_timer = QTimer(self)
        self.update_timer.setSingleShot(True)
        self.update_timer.timeout.connect(self._delayed_update)
        self.is_expanded = False
        self.parent_window = parent  # ✅ Store parent for event filter
        self.init_ui()
        self.hide()  
        # Make it a normal widget (panel floats on its own)
        # self.setWindowFlags(Qt.Widget | Qt.FramelessWindowHint)
        # self.setAttribute(Qt.WA_TranslucentBackground)
        # self.setAttribute(Qt.WA_NoSystemBackground)
        # self.raise_()

    def eventFilter(self, obj, event):
        """No longer handle parent window resize events as we are not floating."""
        return super().eventFilter(obj, event)

    def position_in_ribbon_system(self, parent_window):
        """No longer actively position widget."""
        pass

    def init_ui(self):
        """Initialize the UI layout."""
        # Make the widget itself a movable tool window
        self.setWindowFlags(Qt.Tool | Qt.WindowTitleHint | Qt.WindowCloseButtonHint)
        self.setWindowTitle("Point Statistics")
        self.setMinimumSize(320, 420)  
       
        panel_layout = QVBoxLayout(self)
        panel_layout.setContentsMargins(10, 10, 10, 10)
        panel_layout.setSpacing(8)
       
        # Total count
        self.total_label = QLabel("Total: 0")
        self.total_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        panel_layout.addWidget(self.total_label)
       
        # Separator
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        panel_layout.addWidget(line)
       
        # Scrollable area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        
        # Professional small scrollbar style
        scroll.setStyleSheet("""
            QScrollBar:vertical {
                border: none;
                background: transparent;
                width: 6px;
                margin: 0px 0px 0px 0px;
            }
            QScrollBar::handle:vertical {
                background: rgba(150, 150, 150, 0.5);
                min-height: 20px;
                border-radius: 3px;
            }
            QScrollBar::handle:vertical:hover {
                background: rgba(150, 150, 150, 0.8);
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: none;
            }
        """)
       
        self.stats_container = QWidget()
        self.stats_layout = QVBoxLayout(self.stats_container)
        self.stats_layout.setContentsMargins(0, 0, 0, 0)
        self.stats_layout.setSpacing(4)
        self.stats_layout.setAlignment(Qt.AlignTop)
       
        scroll.setWidget(self.stats_container)
        panel_layout.addWidget(scroll)
       
        self.hide()  
    
    def toggle_panel(self, btn_widget=None):
        """Toggle the statistics panel. Optionally positions it near btn_widget."""
        if self.isHidden():
            if btn_widget:
                btn_global_pos = btn_widget.mapToGlobal(QPoint(0, 0))
                panel_width = self.width()
                
                # Position panel to the left of the button, vertically aligned
                panel_x = btn_global_pos.x() - panel_width - 5
                panel_y = btn_global_pos.y()
                
                # Ensure panel doesn't go off-screen to the left
                from PySide6.QtGui import QGuiApplication
                screen = QGuiApplication.screenAt(btn_global_pos)
                if screen:
                    screen_rect = screen.availableGeometry()
                    if panel_x < screen_rect.left():
                        panel_x = screen_rect.left() + 5
                
                self.move(panel_x, panel_y)
            self.show()
            self.raise_()
            self.activateWindow()
            self.update_statistics()
        else:
            self.hide()
    
    def set_app(self, app):
        """Set the main application reference and connect signals."""
        self.app = app
        
        if hasattr(app, 'display_dialog') and app.display_dialog:
            self.connect_to_display_mode(app.display_dialog)
        
        if hasattr(app, 'classification_changed'):
            try:
                app.classification_changed.connect(self.schedule_update, Qt.UniqueConnection)
            except Exception:
                pass
        
        if hasattr(app, 'classify_dialog') and app.classify_dialog:
            self.connect_to_classify_dialog(app.classify_dialog)
    
    def connect_to_display_mode(self, display_mode_dialog):
        """Connect to DisplayModeDialog to receive class updates."""
        try:
            if hasattr(display_mode_dialog, 'classes_loaded'):
                display_mode_dialog.classes_loaded.connect(self.schedule_update, Qt.UniqueConnection)
            
            if hasattr(display_mode_dialog, 'applied'):
                display_mode_dialog.applied.connect(self.schedule_update, Qt.UniqueConnection)
        except Exception as e:
            print(f"   ⚠️ Could not connect to display_mode: {e}")
    
    def connect_to_classify_dialog(self, classify_dialog):
        """Connect to ClassifyDialog for updates."""
        try:
            if hasattr(classify_dialog, 'classification_complete'):
                classify_dialog.classification_complete.connect(self.schedule_update, Qt.UniqueConnection)
        except Exception as e:
            print(f"   ⚠️ Could not connect to classify_dialog: {e}")
    
    def schedule_update(self):
        """Schedule an update with debouncing."""
        self.update_timer.stop()
        self.update_timer.start(200)
    
    def _delayed_update(self):
        """Delayed update after debouncing."""
        # Always update the footer total, even if panel is hidden
        if self.app and hasattr(self.app, 'total_points_label'):
            if self.app.data and self.app.data.get("xyz") is not None:
                total_points = len(self.app.data.get("xyz"))
                if hasattr(self.app, "update_total_points_label"):
                    self.app.update_total_points_label(total_points)
                else:
                    self.app.total_points_label.setText(f"Total Points: {total_points:,}")
            else:
                if hasattr(self.app, "update_total_points_label"):
                    self.app.update_total_points_label(0)
                else:
                    self.app.total_points_label.setText("Total Points: 0")
                
        if self.isVisible():
            self.update_statistics()

    def get_class_info_from_ptc(self):
        """
        ✅ FIXED: Properly reads and displays LVL (Level/Class Name)
        Priority: Display Mode table > class_palette > fallback
        """
        class_info = {}

        # ✅ PRIORITY 1: Get from Display Mode table (includes user changes)
        dialog = None
        if self.app:
            # Try display_mode_dialog FIRST (correct name)
            if hasattr(self.app, 'display_mode_dialog') and self.app.display_mode_dialog:
                dialog = self.app.display_mode_dialog
            # Fallback to display_dialog (old name)
            elif hasattr(self.app, 'display_dialog') and self.app.display_dialog:
                dialog = self.app.display_dialog
        
        if dialog and hasattr(dialog, 'table'):
            table = dialog.table
            
            _debug_log(f"   📋 Reading from Display Mode table ({table.rowCount()} rows)...")
            
            for row in range(table.rowCount()):
                    try:
                        # Column 1: Code
                        code_item = table.item(row, 1)
                        if not code_item:
                            continue
                        
                        code = int(code_item.text())
                        
                        # Column 2: Description
                        desc_item = table.item(row, 2)
                        desc = desc_item.text() if desc_item else f"Class {code}"
                        
                        # ✅ Column 4: LVL (Level/Class Name) - THIS IS THE KEY!
                        lvl_item = table.item(row, 4)
                        lvl = lvl_item.text() if lvl_item else ""
                        
                        # ✅ Clean up level value
                        if lvl in (None, "", " ", "Zero length ..."):
                            lvl = ""  # Empty = don't show
                        
                        # Column 5: Color
                        color_item = table.item(row, 5)
                        color = (160, 160, 160)  # default gray
                        if color_item:
                            try:
                                rgb = color_item.background().color().getRgb()[:3]
                                color = tuple(rgb)
                            except Exception:
                                pass
                        
                        # Column 0: Show (checkbox)
                        chk = table.cellWidget(row, 0)
                        show = chk.isChecked() if chk else True
                        
                        class_info[code] = {
                            "description": desc,
                            "color": color,
                            "show": show,
                            "lvl": lvl,  # ✅ Level from Display Mode - will be displayed!
                        }
                        
                        _debug_log(f"      ✅ Code {code}: lvl='{lvl}'")
                        
                    except Exception as e:
                        print(f"   ⚠️ Error reading row {row}: {e}")
                        continue

            if class_info:
                _debug_log(f"   ✅ Loaded {len(class_info)} classes from Display Mode")
                return class_info

        # ✅ PRIORITY 2: Get from class_palette (fallback when Display Mode not open)
        if self.app and hasattr(self.app, 'class_palette') and self.app.class_palette:
            _debug_log("   ℹ️ Using class_palette fallback")
            for code, info in self.app.class_palette.items():
                lvl = info.get("lvl", "")
                
                # ✅ Clean up level value
                if lvl in (None, "", " "):
                    lvl = ""
                
                class_info[code] = {
                    "description": info.get("description", f"Class {code}"),
                    "color": info.get("color", (160, 160, 160)),
                    "show": info.get("show", True),
                    "lvl": lvl,  # ✅ Level from palette
                }
            return class_info

        return class_info
    
    def update_statistics(self):
        """Update the statistics display."""
        # Clear existing
        while self.stats_layout.count():
            child = self.stats_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        
        if self.app is None or self.app.data is None:
            self.total_label.setText("Total: 0")
            return
        
        xyz = self.app.data.get("xyz")
        classification = self.app.data.get("classification")
        
        if xyz is None or len(xyz) == 0:
            self.total_label.setText("Total: 0")
            if self.app and hasattr(self.app, 'total_points_label'):
                if hasattr(self.app, "update_total_points_label"):
                    self.app.update_total_points_label(0)
                else:
                    self.app.total_points_label.setText("Total Points: 0")
            return
        
        total_points = len(xyz)
        self.total_label.setText(f"Total: {total_points:,}")
        if self.app and hasattr(self.app, 'total_points_label'):
            if hasattr(self.app, "update_total_points_label"):
                self.app.update_total_points_label(total_points)
            else:
                self.app.total_points_label.setText(f"Total Points: {total_points:,}")
        
        class_info = self.get_class_info_from_ptc()
        
        if classification is None:
            for class_code in sorted(class_info.keys()):
                info = class_info[class_code]
                widget = self._create_class_widget(
                    class_code,
                    info["description"],
                    0,
                    info["color"],
                    info["show"],
                    info["lvl"],  # ✅ Pass the LVL
                )
                self.stats_layout.addWidget(widget)
            return
        
        # Count points per class
        unique_classes, counts = np.unique(classification, return_counts=True)
        point_counts = {int(cls): int(cnt) for cls, cnt in zip(unique_classes, counts)}

        # Ensure classes present in data are still shown even if Display Mode table
        # does not currently contain a row for that class.
        palette_fallback = {}
        if self.app and hasattr(self.app, "class_palette") and isinstance(self.app.class_palette, dict):
            palette_fallback = self.app.class_palette

        for class_code in sorted(point_counts.keys()):
            if class_code in class_info:
                continue
            pinfo = palette_fallback.get(class_code, {})
            lvl = pinfo.get("lvl", "")
            if lvl in (None, "", " ", "Zero length ..."):
                lvl = ""
            color = pinfo.get("color", (160, 160, 160))
            try:
                color = tuple(int(v) for v in color[:3])
            except Exception:
                color = (160, 160, 160)
            class_info[class_code] = {
                "description": pinfo.get("description", f"Class {class_code}"),
                "color": color,
                "show": bool(pinfo.get("show", True)),
                "lvl": lvl,
            }
            _debug_log(f"   [STATS-REGISTRY] Added missing class {class_code} from data")
        
        for class_code in sorted(class_info.keys()):
            info = class_info[class_code]
            count = point_counts.get(class_code, 0)
            
            widget = self._create_class_widget(
                class_code,
                info["description"],
                count,
                info["color"],
                info["show"],
                info["lvl"],  # ✅ Pass the LVL
            )
            self.stats_layout.addWidget(widget)
    
    def _create_class_widget(self, code, description, count, color, visible, lvl):
        """
        ✅ FIXED: Properly displays LVL (Level/Class Name) prominently
        """
        widget = QFrame()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(10)
        
        r, g, b = color if color else (80, 80, 80)
        
        # Color bullet
        color_bullet = QLabel()
        color_bullet.setFixedSize(12, 12)
        color_bullet.setStyleSheet(f"background-color: rgb({r}, {g}, {b}); border-radius: 6px;")
        layout.addWidget(color_bullet)
        
        # Text details
        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)
        
        # Class identity is owned by the loaded PTC/runtime catalog.  Never
        # substitute semantic LAS/ENEL names here: doing so can make the
        # statistics panel disagree with Display Mode/ClassPicker.  If metadata
        # is genuinely unavailable, use a neutral code-only label.
        lvl_str = str(lvl).strip() if lvl else ""
        if not lvl_str or lvl_str == "-":
            lvl_str = f"Class {code}"
            
        desc_str = description.strip() if description else ""
        
        # Format exactly like Class Picker: "Code - Lvl" or "Code - Lvl (Desc)"
        if desc_str and desc_str != f"Class {code}":
            title_text = f"{code} - {lvl_str} ({desc_str})"
        else:
            title_text = f"{code} - {lvl_str}"
            
        title_lbl = QLabel(title_text)
        # User requested no bold inside the stats block
        text_layout.addWidget(title_lbl)
            
        layout.addLayout(text_layout)
        layout.addStretch()
        
        count_text = f"{count:,}" if count > 0 else "0"
        count_lbl = QLabel(count_text)
        layout.addWidget(count_lbl)
        
        # Styling
        if count == 0 or not visible:
            widget.setStyleSheet("QFrame { background-color: rgba(128, 128, 128, 0.05); border-radius: 4px; color: palette(mid); }")
            title_lbl.setStyleSheet("color: palette(mid);")
            count_lbl.setStyleSheet("color: palette(mid);")
        else:
            widget.setStyleSheet("QFrame { background-color: rgba(128, 128, 128, 0.1); border-radius: 4px; }")

        widget.setProperty('class_code', code)
        return widget


# ===== HELPER FUNCTIONS =====

def add_point_stats_dropdown(app, parent_widget):
    """
    Add the statistics widget using ribbon system positioning.
    Auto-repositions on window resize using event filter.
    """
    dropdown = PointCountWidget(parent_widget)
    dropdown.set_app(app)
    
    # Install event filter to catch resize events (ribbon system method)
    if parent_widget:
        parent_widget.installEventFilter(dropdown)
    
    # Initial positioning
    dropdown.position_in_ribbon_system(parent_widget)
    dropdown.show()
    
    # Store reference in app
    app.point_count_widget = dropdown
    
    print("✅ Point Stats widget added (ribbon system with LVL display)")
    return dropdown


def refresh_point_statistics(app):
    """Refresh the statistics display."""
    if hasattr(app, "point_count_widget") and app.point_count_widget:
        app.point_count_widget.schedule_update()

    # Keep an open "View Fields" table (Display > Fields) in sync — it holds
    # its own reference to app.data, which every load/reload replaces with a
    # brand-new dict rather than mutating the old one in place.
    fields_dialog = getattr(app, "_view_fields_table_dialog", None)
    if fields_dialog is not None and hasattr(fields_dialog, "refresh_data"):
        try:
            fields_dialog.refresh_data(getattr(app, "loaded_file", None), app.data)
        except Exception as exc:
            print(f"[FieldsTable] refresh failed: {exc}")
