
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
    QDoubleSpinBox, QPushButton, QSlider, QRadioButton,
    QButtonGroup, QGroupBox, QFrame
)
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QColor, QPalette, QFont

try:
    from shiboken6 import isValid as _qt_object_is_valid
except ImportError:
    def _qt_object_is_valid(obj):
        return obj is not None


def _get_live_class_picker(app):
    """Return a live ClassPicker instance or None if the Qt wrapper is stale."""
    picker = getattr(app, "class_picker", None)
    if picker is None:
        return None
    try:
        if not _qt_object_is_valid(picker):
            app.class_picker = None
            return None
        picker.objectName()
        return picker
    except (RuntimeError, ReferenceError, AttributeError):
        app.class_picker = None
        return None
    except Exception:
        app.class_picker = None
        return None

class BrushSizeDialog(QDialog):
    """
    A modernized dialog to set brush size and shape.
    Updated with green accents and a highlighted outer border to match the specific UI theme.
    """
    
    def __init__(self, parent=None, current_size=1.0, current_shape="circle"):
        super().__init__(parent)
        self.setProperty("themeStyledDialog", True)
        self.setWindowTitle("Brush Settings")
        self.setModal(True)
        self.setMinimumWidth(400)
        
        # Remove standard window frame to better control the 'highlight border' look if needed,
        # but here we'll use a CSS-based border on the main dialog.
        
        self.brush_size = current_size
        self.brush_shape = current_shape
        self._settings_changed = False
        
        self._setup_ui()
        self._apply_styles()
        
        # Connect change tracking
        self.slider.valueChanged.connect(self._mark_changed)
        self.spinbox.valueChanged.connect(self._mark_changed)
        self.circle_radio.toggled.connect(self._mark_changed)
        self.rectangle_radio.toggled.connect(self._mark_changed)

    def _mark_changed(self):
        self._settings_changed = True

    def _apply_styles(self):
        """Apply theme-aware styles for the brush dialog."""
        from gui.theme_manager import get_dialog_stylesheet, ThemeColors
        c = ThemeColors
        self.setStyleSheet(get_dialog_stylesheet() + f"""
            QGroupBox {{
                color: {c.get('text_primary')};
                background-color: {c.get('bg_secondary')};
            }}
            QPushButton#okButton {{
                background-color: {c.get('dialog_primary_bg')};
                color: {c.get('dialog_primary_text')};
                border: 1px solid {c.get('dialog_primary_border')};
                padding: 8px 20px;
                font-weight: 600;
            }}
            QPushButton#okButton:hover {{
                background-color: {c.get('dialog_primary_hover')};
            }}
            #previewPanel {{
                background-color: {c.get('bg_input')};
                border: 1px solid {c.get('border_light')};
                border-radius: 8px;
                color: {c.get('text_secondary')};
                font-size: 10.5px;
                font-weight: 600;
            }}
            #presetContainer {{
                background-color: {c.get('bg_input')};
                border: 1px solid {c.get('border_light')};
                border-radius: 8px;
                padding: 5px;
            }}
            #presetLabel {{
                font-size: 9px;
                font-weight: 700;
                color: {c.get('text_muted')};
            }}
        """)

    def refresh_theme(self):
        self._apply_styles()

    def _setup_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(25, 25, 25, 25)
        layout.setSpacing(15)

        # --- Header ---
        header_layout = QVBoxLayout()
        header_layout.setSpacing(2)
        title = QLabel("Brush Configuration")
        from gui.theme_manager import ThemeColors
        title.setStyleSheet(f"font-weight: 700; font-size: 14px; color: {ThemeColors.get('text_primary')};")

        desc = QLabel("Adjust tool properties for point cloud classification.")
        desc.setStyleSheet(f"color: {ThemeColors.get('text_muted')}; font-size: 11px;")
        
        header_layout.addWidget(title)
        header_layout.addWidget(desc)
        layout.addLayout(header_layout)

        # --- Shape Selection ---
        shape_group = QGroupBox("Brush Shape")
        shape_layout = QHBoxLayout()
        shape_layout.setContentsMargins(15, 10, 15, 15)
        
        self.shape_button_group = QButtonGroup(self)
        self.circle_radio = QRadioButton("Circle")
        self.rectangle_radio = QRadioButton("Square")
        
        self.shape_button_group.addButton(self.circle_radio, 0)
        self.shape_button_group.addButton(self.rectangle_radio, 1)
        
        if self.brush_shape == "circle":
            self.circle_radio.setChecked(True)
        else:
            self.rectangle_radio.setChecked(True)
            
        shape_layout.addWidget(self.circle_radio)
        shape_layout.addSpacing(30)
        shape_layout.addWidget(self.rectangle_radio)
        shape_layout.addStretch()
        shape_group.setLayout(shape_layout)
        layout.addWidget(shape_group)

        # --- Size Controls ---
        size_group = QGroupBox("Size Settings")
        size_group_layout = QVBoxLayout()
        size_group_layout.setContentsMargins(15, 15, 15, 15)
        size_group_layout.setSpacing(12)
        
        # Slider
        slider_row = QHBoxLayout()
        slider_label = QLabel("Scale")
        slider_label.setFixedWidth(50)
        slider_label.setStyleSheet(f"color: {ThemeColors.get('text_secondary')};")
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setMinimum(1)
        self.slider.setMaximum(100)
        self.slider.setValue(int(self.brush_size * 10))
        slider_row.addWidget(slider_label)
        slider_row.addWidget(self.slider)
        size_group_layout.addLayout(slider_row)
        
        # Precise Input
        spin_row = QHBoxLayout()
        spin_label = QLabel("Radius")
        spin_label.setFixedWidth(50)
        spin_label.setStyleSheet(f"color: {ThemeColors.get('text_secondary')};")
        self.spinbox = QDoubleSpinBox()
        self.spinbox.setRange(0.1, 10.0)
        self.spinbox.setSingleStep(0.1)
        self.spinbox.setValue(self.brush_size)
        self.spinbox.setSuffix(" units")
        spin_row.addWidget(spin_label)
        spin_row.addStretch()
        spin_row.addWidget(self.spinbox)
        size_group_layout.addLayout(spin_row)
        
        size_group.setLayout(size_group_layout)
        layout.addWidget(size_group)

        # --- Presets ---
        preset_frame = QFrame()
        preset_frame.setObjectName("presetContainer")
        preset_layout = QHBoxLayout(preset_frame)
        preset_layout.setContentsMargins(10, 5, 10, 5)
        preset_layout.setSpacing(10)
        
        preset_label = QLabel("PRESETS:")
        preset_label.setObjectName("presetLabel")
        preset_layout.addWidget(preset_label)
        
        for size in [0.5, 1.0, 2.0, 5.0]:
            btn = QPushButton(f"{size:.1f}")
            btn.setFixedWidth(55)
            btn.clicked.connect(lambda checked, s=size: self.set_size(s))
            preset_layout.addWidget(btn)
        
        preset_layout.addStretch()
        layout.addWidget(preset_frame)

        # --- Live Preview Bar ---
        self.preview_label = QLabel()
        self.preview_label.setObjectName("previewPanel")
        self.preview_label.setFixedHeight(35)
        self.preview_label.setAlignment(Qt.AlignCenter)
        self._update_preview()
        layout.addWidget(self.preview_label)

        # --- Actions ---
        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)
        button_layout.setContentsMargins(0, 10, 0, 0)
        
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setFixedWidth(90)
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.setDefault(False)
        self.cancel_button.setFocusPolicy(Qt.NoFocus)
        self.cancel_button.clicked.connect(self.reject)
        
        self.ok_button = QPushButton("Apply Settings")
        self.ok_button.setObjectName("okButton")
        self.ok_button.setFixedWidth(130)
        self.ok_button.clicked.connect(self.accept)
        self.ok_button.setAutoDefault(False)
        self.ok_button.setDefault(False)
        self.ok_button.setFocusPolicy(Qt.NoFocus)
        
        button_layout.addStretch()
        button_layout.addWidget(self.cancel_button)
        button_layout.addWidget(self.ok_button)
        layout.addLayout(button_layout)

        self.setLayout(layout)

        # Logic connections
        self.slider.valueChanged.connect(self._on_slider_changed)
        self.spinbox.valueChanged.connect(self._on_spinbox_changed)
        self.circle_radio.toggled.connect(self._on_shape_changed)

    def _update_preview(self):
        shape_icon = "●" if self.brush_shape == "circle" else "■"
        self.preview_label.setText(
            f"{shape_icon} {self.brush_shape.upper()} PREVIEW | RADIUS: {self.brush_size:.1f} UNITS"
        )

    def _on_slider_changed(self, value):
        size = value / 10.0
        self.spinbox.blockSignals(True)
        self.spinbox.setValue(size)
        self.spinbox.blockSignals(False)
        self.brush_size = size
        self._update_preview()

    def _on_spinbox_changed(self, value):
        self.slider.blockSignals(True)
        self.slider.setValue(int(value * 10))
        self.slider.blockSignals(False)
        self.brush_size = value
        self._update_preview()

    def _on_shape_changed(self):
        self.brush_shape = "circle" if self.circle_radio.isChecked() else "rectangle"
        self._update_preview()

    def set_size(self, size):
        self.spinbox.setValue(size)

    def get_brush_size(self):
        return self.brush_size

    def get_brush_shape(self):
        return self.brush_shape

    def accept(self):
        if not self._settings_changed:
            pass 
        super().accept()

def show_brush_size_dialog(app):
    """
    Show the brush settings dialog and update app brush configuration.
    Returns: 'accepted', 'unchanged', or 'cancelled'
    
    Args:
        app: Your main application instance
        
    Returns:
        str: 'accepted' if user changed and clicked OK
             'unchanged' if user didn't change anything (auto-accepts)
             'cancelled' if user clicked cancel
    """
    current_size = getattr(app, "brush_radius", 1.0)
    current_shape = getattr(app, "brush_shape", "circle")
    
    existing_dialog = getattr(app, "_brush_size_dialog_instance", None)
    if existing_dialog is not None:
        try:
            if existing_dialog.isVisible():
                existing_dialog.raise_()
                existing_dialog.activateWindow()
                return "unchanged"
        except Exception:
            pass

    dialog = BrushSizeDialog(
        parent=app,
        current_size=current_size,
        current_shape=current_shape
    )
    app._brush_size_dialog_instance = dialog
    
    # ✅ CRITICAL: Track if user makes ANY changes
    settings_changed = [False]  # Use list so inner function can modify it
    
    def on_any_change():
        settings_changed[0] = True
    
    # Connect ALL input widgets to detect changes
    dialog.slider.valueChanged.connect(on_any_change)
    dialog.spinbox.valueChanged.connect(on_any_change)
    dialog.circle_radio.toggled.connect(on_any_change)
    dialog.rectangle_radio.toggled.connect(on_any_change)
    
    try:
        result = dialog.exec_()
    finally:
        if getattr(app, "_brush_size_dialog_instance", None) is dialog:
            app._brush_size_dialog_instance = None
    
    new_size = dialog.get_brush_size()
    new_shape = dialog.get_brush_shape()
    
    if result == QDialog.Accepted:
        if not settings_changed[0]:
            # User clicked OK but nothing changed - auto-accept
            print("ℹ️ No changes to brush settings - auto-accepting")
            return 'unchanged'
        
        # Settings changed and user clicked OK
        # ✅ CRITICAL FIX: Set BOTH world radius and pixel preview size
        app.brush_radius = new_size  # World units (for classification)
        
        # ✅ Convert world radius to pixel size for preview
        base_pixel_size = 20.0  # Base size for 1.0 unit
        app.brush_preview_px = new_size * base_pixel_size
        
        # Store shape
        app.brush_shape = new_shape
        
        shape_icon = "●" if new_shape == "circle" else "■"
        
        if hasattr(app, "statusBar"):
            app.statusBar().showMessage(
                f"🖌️ Brush: {shape_icon} {new_shape} - {new_size:.1f} units "
                f"({app.brush_preview_px:.0f}px preview)", 
                3000
            )
        
        print(f"✅ Brush updated: {new_shape} shape, {new_size:.1f} units, "
              f"{app.brush_preview_px:.0f}px preview")
        return 'accepted'
    else:
        # User clicked Cancel
        print("❌ Brush settings dialog cancelled")
        return 'cancelled'


def activate_brush_tool_with_dialog(app, show_settings=False):
    """
    Activate brush tool with smart behavior:
    - Normal activation: Use last settings (instant activation)
    - UI Shift+Left-click on Brush button: Show settings dialog first
    - First time ever: Auto-show settings dialog

    Args:
        app: main window/app instance
        show_settings: explicit request to open settings before activation
    """

    # Check if this is first time using brush
    first_time = not hasattr(app, "brush_radius")

    # Show dialog only when explicitly requested by UI interaction
    # (or on first-ever use to preserve onboarding behavior).
    if first_time or show_settings:
        result = show_brush_size_dialog(app)
        
        if result == 'cancelled':
            print("Brush tool activation cancelled by user")
            if hasattr(app, "statusBar"):
                app.statusBar().showMessage("❌ Brush cancelled", 2000)
            return False
    
    # Activate brush with current/last settings
    app.active_classify_tool = "brush"
    
    # Ensure defaults exist
    if not hasattr(app, "brush_radius"):
        app.brush_radius = 1.0
    if not hasattr(app, "brush_shape"):
        app.brush_shape = "circle"
    if not hasattr(app, "brush_preview_px"):
        app.brush_preview_px = 20.0
    
    radius = app.brush_radius
    shape = app.brush_shape
    shape_icon = "●" if shape == "circle" else "■"
    
    # Show status message
    if hasattr(app, "statusBar"):
        hint = " (Shift+Click for settings)" if not first_time else ""
        app.statusBar().showMessage(
            f"🖌️ Brush: {shape_icon} {shape}, {radius:.1f} units{hint}", 
            4000
        )
    
    print(f"✅ Brush activated: {shape} shape, radius {radius:.1f}")
    
    # Open class picker
    _open_class_picker_safely(app)
    
    return True


def _open_class_picker_safely(app):
    """
    Safely open class picker without stealing focus from main view.
    """
    from gui.class_picker import ClassPicker

    picker = _get_live_class_picker(app)
    if picker is None:
        app.class_picker = ClassPicker(app, parent=app)
        picker = app.class_picker
        picker.configure_for_background_mode()
        picker.ensure_visible()
    else:
        picker.sync_with_app()
        try:
            if not picker.isVisible():
                picker.ensure_visible()
        except (RuntimeError, ReferenceError, AttributeError):
            app.class_picker = None
        except Exception:
            pass
    
    # ✅ CRITICAL: Keep focus on main view
    if hasattr(app, 'vtk_widget'):
        app.vtk_widget.setFocus()