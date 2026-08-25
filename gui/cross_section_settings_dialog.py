# New file: gui/cross_section_settings_dialog.py

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, 
    QLabel, QSlider, QComboBox, QColorDialog,QWidget
)
from PySide6.QtCore import Qt, QSettings
from PySide6.QtGui import QColor, QPainter, QPen

class CrossSectionSettingsDialog(QDialog):
    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.setProperty("themeStyledDialog", True)
        self.app = app
        self.setWindowTitle("Cross-Section Line Settings")
        self.setModal(False)  # Non-blocking
        
        # Load current settings (or defaults)
        # Load current color (handle both tuple and QColor)
        color_value = getattr(app, 'cross_line_color', (255, 0, 255))
        if isinstance(color_value, tuple):
            # Convert normalized float tuple to QColor
            if all(0 <= v <= 1 for v in color_value):
                self.current_color = QColor(int(color_value[0] * 255), int(color_value[1] * 255), int(color_value[2] * 255))
            else:
                # Already 0-255 range
                self.current_color = QColor(int(color_value[0]), int(color_value[1]), int(color_value[2]))
        else:
            self.current_color = color_value
        self.current_width = getattr(app, 'cross_line_width', 3)
        self.current_style = getattr(app, 'cross_line_style', 'solid')
        
        self.build_ui()
        self.apply_dark_theme()
        
    def build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        # 1. Color Picker
        color_row = QHBoxLayout()
        color_row.setSpacing(10)
        color_row.addWidget(QLabel("Color:"))
        self.color_button = QPushButton()
        self.color_button.setFixedSize(50, 30)
        self.update_color_button()
        self.color_button.clicked.connect(self.choose_color)
        color_row.addWidget(self.color_button)
        color_row.addStretch()
        layout.addLayout(color_row)

        layout.addSpacing(6)

        # 2. Width Slider
        width_row = QHBoxLayout()
        width_row.setSpacing(10)
        width_row.addWidget(QLabel("Width:"))
        self.width_slider = QSlider(Qt.Horizontal)
        self.width_slider.setRange(1, 10)
        self.width_slider.setValue(self.current_width)
        self.width_slider.valueChanged.connect(self.on_width_changed)
        width_row.addWidget(self.width_slider)
        self.width_label = QLabel(f"{self.current_width} px")
        self.width_label.setMinimumWidth(36)
        width_row.addWidget(self.width_label)
        layout.addLayout(width_row)

        layout.addSpacing(6)

        # 3. Line Style Dropdown
        style_row = QHBoxLayout()
        style_row.setSpacing(10)
        style_row.addWidget(QLabel("Style:"))
        self.style_combo = QComboBox()
        self.style_combo.addItems([
            "Solid",
            "Dashed", 
            "Dotted",
            "Dash-Dot",
            "Dash-Dot-Dot"
        ])
        self.style_combo.setCurrentText(self.current_style.capitalize())
        self.style_combo.currentTextChanged.connect(self.on_style_changed)
        style_row.addWidget(self.style_combo)
        style_row.addStretch()
        layout.addLayout(style_row)

        layout.addSpacing(6)

        # 4. Preview Canvas
        layout.addWidget(QLabel("Preview:"))
        self.preview_widget = LinePreviewWidget(self)
        self.preview_widget.setFixedHeight(60)
        layout.addWidget(self.preview_widget)

        layout.addSpacing(8)

        # 5. Buttons
        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(self.apply_settings)
        button_row.addWidget(apply_btn)

        reset_btn = QPushButton("Reset")
        reset_btn.clicked.connect(self.reset_settings)
        button_row.addWidget(reset_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        button_row.addWidget(close_btn)

        layout.addLayout(button_row)


    def apply_dark_theme(self):
        from gui.theme_manager import get_dialog_stylesheet
        self.setStyleSheet(get_dialog_stylesheet())

    def showEvent(self, event):
        """Re-sync all widgets from current app state when the dialog is re-shown.
        Fixes Qt QSlider visibility bug that occurs when reusing a cached dialog instance."""
        super().showEvent(event)

        # Reload current values from app (in case they changed since last open)
        color_value = getattr(self.app, 'cross_line_color', (255, 0, 255))
        if isinstance(color_value, tuple):
            if all(0 <= v <= 1 for v in color_value):
                self.current_color = QColor(
                    int(color_value[0] * 255),
                    int(color_value[1] * 255),
                    int(color_value[2] * 255)
                )
            else:
                self.current_color = QColor(
                    int(color_value[0]),
                    int(color_value[1]),
                    int(color_value[2])
                )
        else:
            self.current_color = color_value

        self.current_width = getattr(self.app, 'cross_line_width', 3)
        self.current_style  = getattr(self.app, 'cross_line_style', 'solid')

        # Push refreshed values back into widgets
        self.update_color_button()
        self.width_slider.setValue(self.current_width)
        self.width_label.setText(f"{self.current_width} px")
        self.style_combo.setCurrentText(self.current_style.capitalize())
        self.preview_widget.update()

        # Force Qt to recalculate layout and repaint all children
        # (fixes QSlider disappearing after hide/show cycle on cached dialogs)
        self.width_slider.show()
        self.adjustSize()
        self.update()


    def choose_color(self):
        color = QColorDialog.getColor(self.current_color, self)
        if color.isValid():
            self.current_color = color
            self.update_color_button()
            self.preview_widget.update()
    
    def update_color_button(self):
        self.color_button.setStyleSheet(
            f"background-color: {self.current_color.name()};"
        )
    
    def on_width_changed(self, value):
        self.current_width = value
        self.width_label.setText(f"{value} px")
        self.preview_widget.update()
    
    def on_style_changed(self, text):
        self.current_style = text.lower()
        self.preview_widget.update()
    

    def apply_settings(self):
        # Save to app
        self.app.cross_line_color = (
            self.current_color.redF(),
            self.current_color.greenF(),
            self.current_color.blueF()
        )
        self.app.cross_line_width = self.current_width
        self.app.cross_line_style = self.current_style

        # Save to QSettings (persist across sessions)
        settings = QSettings("NakshaAI", "LidarApp")
        settings.setValue("cross_line_color", self.current_color.name())
        settings.setValue("cross_line_width", self.current_width)
        settings.setValue("cross_line_style", self.current_style)

        print(f"✅ Cross-section settings saved:")
        print(f"   Color: {self.current_color.name()}")
        print(f"   Width: {self.current_width}px")
        print(f"   Style: {self.current_style}")

        self.app.statusBar().showMessage(
            "✅ Cross-section line settings saved", 2000
        )

        # Update existing rubber band actor instead of resetting
        if hasattr(self.app, 'section_controller'):
            sc = self.app.section_controller
            if hasattr(sc, 'rubber_actor') and sc.rubber_actor:
                try:
                    # Update color and width
                    prop = sc.rubber_actor.GetProperty()
                    prop.SetColor(
                        self.current_color.redF(),
                        self.current_color.greenF(),
                        self.current_color.blueF()
                    )
                    prop.SetLineWidth(self.current_width)

                    # Update line style geometry (for dashed/dotted lines)
                    if hasattr(sc, 'update_rectangle_style'):
                        sc.update_rectangle_style()
                    else:
                        # Fallback: force reinit if update method doesn't exist yet
                        self.app.vtk_widget.renderer.RemoveActor(sc.rubber_actor)
                        sc.rubber_actor = None
                        sc.rubber_points = None
                        sc.rubber_poly = None
                        sc._rubber_initialized = False
                        print("⚠️ Rectangle reset - update_rectangle_style() not found")

                    # Render the changes
                    self.app.vtk_widget.render()
                    print(f"🔄 Cross-section line updated to: {self.current_style}")

                except Exception as e:
                    print(f"❌ Error updating rubber actor: {e}")
                    # Fallback to reset on error
                    try:
                        self.app.vtk_widget.renderer.RemoveActor(sc.rubber_actor)
                    except Exception:
                        pass
                    sc.rubber_actor = None
                    sc.rubber_points = None
                    sc.rubber_poly = None
                    sc._rubber_initialized = False
                    print("🔄 Rectangle reset due to error")

        self.accept()   # or self.close()

    def reset_settings(self):
        self.current_color = QColor(255, 0, 255)  # Magenta
        self.current_width = 3
        self.current_style = 'solid'
        
        self.update_color_button()
        self.width_slider.setValue(3)
        self.style_combo.setCurrentText("Solid")
        self.preview_widget.update()

class LinePreviewWidget(QWidget):
    """Shows a live preview of the line appearance"""
    
    def __init__(self, dialog):
        super().__init__()
        self.dialog = dialog
        
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        # Draw preview line
        pen = QPen(self.dialog.current_color)
        pen.setWidth(self.dialog.current_width)
        
        # Set line style
        style_map = {
            'solid': Qt.SolidLine,
            'dashed': Qt.DashLine,
            'dotted': Qt.DotLine,
            'dash-dot': Qt.DashDotLine,
            'dash-dot-dot': Qt.DashDotDotLine
        }
        
        print(f"Preview painting with style: {self.dialog.current_style}")
        pen.setStyle(style_map.get(self.dialog.current_style, Qt.SolidLine))
        
        painter.setPen(pen)
        
        # Draw horizontal line centered
        y = self.height() // 2
        painter.drawLine(20, y, self.width() - 20, y)
        
    def paintEvent(self, event):
        painter = QPainter(self)
        from gui.theme_manager import ThemeColors
        painter.fillRect(self.rect(), QColor(ThemeColors.get("bg_secondary")))
        painter.setRenderHint(QPainter.Antialiasing)

        pen = QPen(self.dialog.current_color)
        pen.setWidth(self.dialog.current_width)

        style_map = {
            'solid': Qt.SolidLine,
            'dashed': Qt.DashLine,
            'dotted': Qt.DotLine,
            'dash-dot': Qt.DashDotLine,
            'dash-dot-dot': Qt.DashDotDotLine
        }

        pen.setStyle(style_map.get(self.dialog.current_style, Qt.SolidLine))
        painter.setPen(pen)

        y = self.height() // 2
        painter.drawLine(20, y, self.width() - 20, y)
