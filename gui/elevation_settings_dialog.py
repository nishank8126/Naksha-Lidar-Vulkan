"""
Elevation Color Ramp Customization Dialog
Opens when user Shift+Clicks the Elevation button
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QListWidget, QListWidgetItem, QColorDialog, QDoubleSpinBox,
    QMessageBox, QFrame
)
from PySide6.QtGui import QColor, QPixmap, QPainter, QLinearGradient
from PySide6.QtCore import Qt


class ElevationSettingsDialog(QDialog):
    """
    Dialog for customizing elevation color gradient.
    Default: MicroStation 5-color rainbow (Blue→Cyan→Green→Yellow→Red)
    """
    
    def __init__(self, parent=None, app=None, ramp_attr="elevation_color_ramp", title="Elevation", subtitle="Customize how elevation (Z-height) is colored.", default_label="Default: MicroStation Rainbow (Blue→Cyan→Green→Yellow→Red)"):
        super().__init__(parent)
        self.setWindowTitle(f"{title} Color Gradient Settings")
        self.resize(550, 650)
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint)
        
        # Default MicroStation 5-color ramp
        self.color_stops = [
            (0.00, (0, 0, 255)),      # Blue
            (0.25, (0, 255, 255)),    # Cyan
            (0.50, (0, 255, 0)),      # Green
            (0.75, (255, 255, 0)),    # Yellow
            (1.00, (255, 0, 0))       # Red
        ]
        
        # use it as the starting point instead of the default.
        if app is not None and hasattr(app, ramp_attr) and getattr(app, ramp_attr):
            self.color_stops = list(getattr(app, ramp_attr))
        
        self._title_text = title
        self._subtitle_text = subtitle
        self._default_label_text = default_label
        self._setup_ui()
        self._load_current_ramp()
        self._update_preview()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        
        # Title
        title = QLabel(f"<h2>🎨 {self._title_text} Color Gradient</h2>")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)
        
        subtitle = QLabel(
            f"{self._subtitle_text}<br>"
            f"<i>{self._default_label_text}</i>"
        )
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet("color: gray; font-size: 10pt;")
        layout.addWidget(subtitle)
        
        # Preview gradient
        preview_label = QLabel("<b>Preview:</b>")
        layout.addWidget(preview_label)
        
        self.preview = QLabel()
        self.preview.setFixedHeight(80)
        self.preview.setFrameStyle(QFrame.Box | QFrame.Sunken)
        self.preview.setStyleSheet("background: white; border: 2px solid #ccc;")
        layout.addWidget(self.preview)
        
        # Color stops list
        stops_label = QLabel("<b>Color Stops:</b>")
        layout.addWidget(stops_label)
        
        self.stops_list = QListWidget()
        self.stops_list.setMaximumHeight(200)
        self.stops_list.setAlternatingRowColors(True)
        layout.addWidget(self.stops_list)
        
        # Add/Edit/Remove buttons
        btn_row = QHBoxLayout()
        self.add_btn = QPushButton("➕ Add Stop")
        self.edit_btn = QPushButton("✏️ Edit Stop")
        self.remove_btn = QPushButton("🗑️ Remove Stop")
        btn_row.addWidget(self.add_btn)
        btn_row.addWidget(self.edit_btn)
        btn_row.addWidget(self.remove_btn)
        layout.addLayout(btn_row)
        
        # Preset buttons
        preset_label = QLabel("<b>Presets:</b>")
        layout.addWidget(preset_label)
        
        preset_row1 = QHBoxLayout()
        self.rainbow_btn = QPushButton("🌈 Rainbow (Default)")
        self.grayscale_btn = QPushButton("⬛ Grayscale")
        preset_row1.addWidget(self.rainbow_btn)
        preset_row1.addWidget(self.grayscale_btn)
        layout.addLayout(preset_row1)
        
        preset_row2 = QHBoxLayout()
        self.terrain_btn = QPushButton("🏔️ Terrain")
        self.heatmap_btn = QPushButton("🔥 Heat Map")
        preset_row2.addWidget(self.terrain_btn)
        preset_row2.addWidget(self.heatmap_btn)
        layout.addLayout(preset_row2)
        
        layout.addStretch()
        
        # Apply/Cancel buttons
        bottom_row = QHBoxLayout()
        bottom_row.addStretch()
        self.apply_btn = QPushButton("✅ Apply")
        self.cancel_btn = QPushButton("❌ Cancel")
        self.apply_btn.setFixedWidth(120)
        self.cancel_btn.setFixedWidth(120)
        bottom_row.addWidget(self.apply_btn)
        bottom_row.addWidget(self.cancel_btn)
        layout.addLayout(bottom_row)
        
        # Connect signals
        self.add_btn.clicked.connect(self._add_stop)
        self.edit_btn.clicked.connect(self._edit_stop)
        self.remove_btn.clicked.connect(self._remove_stop)
        self.rainbow_btn.clicked.connect(self._preset_rainbow)
        self.grayscale_btn.clicked.connect(self._preset_grayscale)
        self.terrain_btn.clicked.connect(self._preset_terrain)
        self.heatmap_btn.clicked.connect(self._preset_heatmap)
        self.apply_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)
    
    def _load_current_ramp(self):
        """Populate list widget with current color stops."""
        self.stops_list.clear()
        for pos, (r, g, b) in sorted(self.color_stops):
            item = QListWidgetItem(f"{pos:.2f}  →  RGB({r:3d}, {g:3d}, {b:3d})")
            
            # Color swatch icon
            pixmap = QPixmap(24, 24)
            pixmap.fill(QColor(r, g, b))
            item.setIcon(pixmap)
            
            item.setData(Qt.UserRole, (pos, (r, g, b)))
            self.stops_list.addItem(item)
    
    def _update_preview(self):
        """Draw gradient preview bar."""
        width = max(self.preview.width(), 500)
        height = 80
        
        pixmap = QPixmap(width, height)
        painter = QPainter(pixmap)
        
        # Draw gradient
        gradient = QLinearGradient(0, 0, width, 0)
        for pos, (r, g, b) in sorted(self.color_stops):
            gradient.setColorAt(pos, QColor(r, g, b))
        
        painter.fillRect(0, 0, width, height, gradient)
        
        # Draw position markers
        painter.setPen(QColor(0, 0, 0, 200))
        for pos, _ in sorted(self.color_stops):
            x = int(pos * width)
            painter.drawLine(x, 0, x, height)
            painter.drawText(x - 15, height - 5, f"{pos:.2f}")
        
        painter.end()
        self.preview.setPixmap(pixmap)
    
    def _add_stop(self):
        """Add new color stop."""
        dialog = ColorStopDialog(self)
        if dialog.exec() == QDialog.Accepted:
            pos, color = dialog.get_stop()
            self.color_stops.append((pos, color))
            self._load_current_ramp()
            self._update_preview()
    
    def _edit_stop(self):
        """Edit selected color stop."""
        current = self.stops_list.currentItem()
        if not current:
            QMessageBox.warning(self, "No Selection", "Please select a color stop to edit.")
            return
        
        pos, color = current.data(Qt.UserRole)
        dialog = ColorStopDialog(self, pos, color)
        if dialog.exec() == QDialog.Accepted:
            new_pos, new_color = dialog.get_stop()
            
            # Remove old, add new
            self.color_stops = [s for s in self.color_stops if s[0] != pos]
            self.color_stops.append((new_pos, new_color))
            
            self._load_current_ramp()
            self._update_preview()
    
    def _remove_stop(self):
        """Remove selected color stop."""
        current = self.stops_list.currentItem()
        if not current:
            QMessageBox.warning(self, "No Selection", "Please select a color stop to remove.")
            return
        
        if len(self.color_stops) <= 2:
            QMessageBox.warning(self, "Minimum Stops", "Gradient must have at least 2 color stops.")
            return
        
        pos, _ = current.data(Qt.UserRole)
        self.color_stops = [s for s in self.color_stops if s[0] != pos]
        
        self._load_current_ramp()
        self._update_preview()
    
    def _preset_rainbow(self):
        """MicroStation default rainbow (5 colors)."""
        self.color_stops = [
            (0.00, (0, 0, 255)),      # Blue
            (0.25, (0, 255, 255)),    # Cyan
            (0.50, (0, 255, 0)),      # Green
            (0.75, (255, 255, 0)),    # Yellow
            (1.00, (255, 0, 0))       # Red
        ]
        self._load_current_ramp()
        self._update_preview()
    
    def _preset_grayscale(self):
        """Black to white."""
        self.color_stops = [
            (0.0, (0, 0, 0)),         # Black
            (1.0, (255, 255, 255))    # White
        ]
        self._load_current_ramp()
        self._update_preview()
    
    def _preset_terrain(self):
        """Earth tones (water→vegetation→earth→snow)."""
        self.color_stops = [
            (0.00, (0, 0, 128)),      # Deep Blue
            (0.25, (34, 139, 34)),    # Forest Green
            (0.50, (139, 69, 19)),    # Brown
            (0.75, (210, 180, 140)),  # Tan
            (1.00, (255, 255, 255))   # White
        ]
        self._load_current_ramp()
        self._update_preview()
    
    def _preset_heatmap(self):
        """Black→Purple→Red→Orange→Yellow."""
        self.color_stops = [
            (0.00, (0, 0, 0)),        # Black
            (0.25, (128, 0, 128)),    # Purple
            (0.50, (255, 0, 0)),      # Red
            (0.75, (255, 165, 0)),    # Orange
            (1.00, (255, 255, 0))     # Yellow
        ]
        self._load_current_ramp()
        self._update_preview()
    
    def get_color_ramp(self):
        """Return sorted list of (position, (r,g,b)) tuples."""
        return sorted(self.color_stops)


class ColorStopDialog(QDialog):
    """Dialog to add/edit a single color stop."""
    
    def __init__(self, parent=None, position=0.5, color=(128, 128, 128)):
        super().__init__(parent)
        self.setWindowTitle("Color Stop")
        self.color = QColor(*color)
        
        layout = QVBoxLayout(self)
        
        # Position input
        pos_row = QHBoxLayout()
        pos_row.addWidget(QLabel("Position (0.0 = low, 1.0 = high):"))
        self.pos_spin = QDoubleSpinBox()
        self.pos_spin.setRange(0.0, 1.0)
        self.pos_spin.setSingleStep(0.05)
        self.pos_spin.setValue(position)
        self.pos_spin.setDecimals(2)
        pos_row.addWidget(self.pos_spin)
        layout.addLayout(pos_row)
        
        # Color picker
        self.color_btn = QPushButton("Pick Color")
        self.color_btn.setFixedHeight(50)
        self.color_btn.clicked.connect(self._pick_color)
        self._update_color_preview()
        layout.addWidget(self.color_btn)
        
        # OK/Cancel
        btn_row = QHBoxLayout()
        ok_btn = QPushButton("OK")
        cancel_btn = QPushButton("Cancel")
        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)
    
    def _pick_color(self):
        """Open color picker."""
        color = QColorDialog.getColor(self.color, self, "Pick Color")
        if color.isValid():
            self.color = color
            self._update_color_preview()
    
    def _update_color_preview(self):
        """Update button to show current color."""
        r, g, b = self.color.red(), self.color.green(), self.color.blue()
        text_color = 'white' if (r + g + b) < 384 else 'black'
        self.color_btn.setStyleSheet(
            f"background-color: rgb({r}, {g}, {b}); "
            f"color: {text_color}; "
            f"font-weight: bold; font-size: 12pt;"
        )
        self.color_btn.setText(f"RGB({r}, {g}, {b})")
    
    def get_stop(self):
        """Return (position, (r, g, b))."""
        pos = self.pos_spin.value()
        color = (self.color.red(), self.color.green(), self.color.blue())
        return pos, color
