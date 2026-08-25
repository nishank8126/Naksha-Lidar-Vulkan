"""
Depth Display Customization Dialog
Opens when user Shift+Clicks the Depth button in View ribbon
Similar to Elevation and Intensity settings
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QDoubleSpinBox, QGroupBox, QSlider, QComboBox, QFrame
)
from PySide6.QtGui import QPixmap, QPainter, QLinearGradient, QColor
from PySide6.QtCore import Qt, QSettings


class DepthSettingsDialog(QDialog):
    """
    Customize depth display settings (like MicroStation Display Depth).
    
    Controls:
    - Depth range (near/far percentiles)
    - Color scheme (grayscale, inverted, rainbow, heatmap)
    - Contrast/gamma adjustment
    - Preview gradient
    """
    
    def __init__(self, parent=None, app=None):
        super().__init__(parent)
        self.app = app
        self.setWindowTitle("Depth Display Settings")
        self.resize(500, 650)
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint)
        
        # Load current settings from app or defaults
        if app:
            self.clip_low = getattr(app, "depth_clip_low", 1.0)
            self.clip_high = getattr(app, "depth_clip_high", 99.0)
            self.color_scheme = getattr(app, "depth_color_scheme", "grayscale")
            self.gamma = getattr(app, "depth_gamma", 1.0)
        else:
            # Fallback defaults if no app
            settings = QSettings("NakshaAI", "LidarApp")
            self.clip_low = settings.value("depth_clip_low", 1.0, type=float)
            self.clip_high = settings.value("depth_clip_high", 99.0, type=float)
            self.color_scheme = settings.value("depth_color_scheme", "grayscale", type=str)
            self.gamma = settings.value("depth_gamma", 1.0, type=float)
        
        self._setup_ui()
        self._update_preview()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        
        # ✅ FIX 1: Set fixed dialog size
        self.setMinimumWidth(520)  # Prevent shrinking
        self.setMaximumWidth(520)  # Prevent expanding
        self.resize(520, 650)     # Initial size
        
        # ═══════════════════════════════════════════════════════════════
        # TITLE
        # ═══════════════════════════════════════════════════════════════
        title = QLabel("<h2>📏 Depth Display Settings</h2>")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)
        
        subtitle = QLabel(
            "Customize how depth (distance from camera) is displayed.<br>"
            "<i>Similar to MicroStation Display Depth controls</i>"
        )
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet("color: gray; font-size: 10pt;")
        layout.addWidget(subtitle)
        
        # ═══════════════════════════════════════════════════════════════
        # PREVIEW GRADIENT
        # ═══════════════════════════════════════════════════════════════
        preview_label = QLabel("<b>Preview (Near ← → Far):</b>")
        layout.addWidget(preview_label)
        
        self.preview = QLabel()
        self.preview.setFixedHeight(60)
        self.preview.setFrameStyle(QFrame.Box | QFrame.Sunken)
        self.preview.setStyleSheet("background: white; border: 2px solid #ccc;")
        layout.addWidget(self.preview)
        
        # ═══════════════════════════════════════════════════════════════
        # DEPTH RANGE (Percentile Clipping)
        # ═══════════════════════════════════════════════════════════════
        range_group = QGroupBox("Depth Range (Histogram Clipping)")
        range_layout = QVBoxLayout()
        
        # Near percentile
        near_row = QHBoxLayout()
        near_row.addWidget(QLabel("Near Clip (percentile):"))
        self.clip_low_spin = QDoubleSpinBox()
        self.clip_low_spin.setRange(0.0, 50.0)
        self.clip_low_spin.setSingleStep(0.5)
        self.clip_low_spin.setValue(self.clip_low)
        self.clip_low_spin.setDecimals(1)
        self.clip_low_spin.setSuffix("%")
        # ✅ FIX 2: Fixed width prevents resize
        self.clip_low_spin.setFixedWidth(90)
        self.clip_low_spin.setMaximumWidth(90)
        self.clip_low_spin.setMinimumWidth(90)
        self.clip_low_spin.valueChanged.connect(self._update_preview)
        near_row.addWidget(self.clip_low_spin)
        near_row.addStretch()
        range_layout.addLayout(near_row)
        
        # Far percentile
        far_row = QHBoxLayout()
        far_row.addWidget(QLabel("Far Clip (percentile):"))
        self.clip_high_spin = QDoubleSpinBox()
        self.clip_high_spin.setRange(50.0, 100.0)
        self.clip_high_spin.setSingleStep(0.5)
        self.clip_high_spin.setValue(self.clip_high)
        self.clip_high_spin.setDecimals(1)
        self.clip_high_spin.setSuffix("%")
        # ✅ FIX 3: Fixed width prevents resize
        self.clip_high_spin.setFixedWidth(90)
        self.clip_high_spin.setMaximumWidth(90)
        self.clip_high_spin.setMinimumWidth(90)
        self.clip_high_spin.valueChanged.connect(self._update_preview)
        far_row.addWidget(self.clip_high_spin)
        far_row.addStretch()
        range_layout.addLayout(far_row)
        
        range_group.setLayout(range_layout)
        layout.addWidget(range_group)
        
        # ═══════════════════════════════════════════════════════════════
        # COLOR SCHEME
        # ═══════════════════════════════════════════════════════════════
        color_group = QGroupBox("Color Scheme")
        color_layout = QVBoxLayout()
        
        self.scheme_combo = QComboBox()
        self.scheme_combo.addItems([
            "Grayscale (White=Near, Black=Far)",
            "Inverted Grayscale (Black=Near, White=Far)",
            "Rainbow (Blue=Near, Red=Far)",
            "Heat Map (Cold=Near, Hot=Far)"
        ])
        # ✅ FIX 4: Fixed width for consistency
        self.scheme_combo.setFixedWidth(280)
        
        # Set current selection
        scheme_map = {
            "grayscale": 0,
            "inverted": 1,
            "rainbow": 2,
            "heatmap": 3
        }
        self.scheme_combo.setCurrentIndex(scheme_map.get(self.color_scheme, 0))
        self.scheme_combo.currentIndexChanged.connect(self._on_scheme_changed)
        color_layout.addWidget(self.scheme_combo)
        
        color_group.setLayout(color_layout)
        layout.addWidget(color_group)
        
        # ═══════════════════════════════════════════════════════════════
        # CONTRAST/GAMMA ADJUSTMENT
        # ═══════════════════════════════════════════════════════════════
        gamma_group = QGroupBox("Contrast Adjustment")
        gamma_layout = QVBoxLayout()
        
        gamma_row = QHBoxLayout()
        gamma_row.addWidget(QLabel("Gamma:"))
        
        self.gamma_slider = QSlider(Qt.Horizontal)
        self.gamma_slider.setRange(50, 200)  # 0.5 to 2.0
        self.gamma_slider.setValue(int(self.gamma * 100))
        self.gamma_slider.setTickPosition(QSlider.TicksBelow)
        self.gamma_slider.setTickInterval(25)
        self.gamma_slider.valueChanged.connect(self._on_gamma_changed)
        gamma_row.addWidget(self.gamma_slider)
        
        self.gamma_label = QLabel(f"{self.gamma:.2f}")
        self.gamma_label.setFixedWidth(40)
        gamma_row.addWidget(self.gamma_label)
        
        gamma_layout.addLayout(gamma_row)
        
        gamma_hint = QLabel(
            "<i>Lower values = more contrast (darker shadows)<br>"
            "Higher values = less contrast (brighter overall)</i>"
        )
        gamma_hint.setStyleSheet("color: gray; font-size: 9pt;")
        gamma_layout.addWidget(gamma_hint)
        
        gamma_group.setLayout(gamma_layout)
        layout.addWidget(gamma_group)
        
        # ═══════════════════════════════════════════════════════════════
        # PRESETS
        # ═══════════════════════════════════════════════════════════════
        preset_group = QGroupBox("Presets")
        preset_layout = QHBoxLayout()
        
        self.default_btn = QPushButton("🔄 Reset to Default")
        self.default_btn.clicked.connect(self._preset_default)
        self.default_btn.setFixedWidth(140)  # ✅ FIX 5
        preset_layout.addWidget(self.default_btn)
        
        self.high_contrast_btn = QPushButton("🔆 High Contrast")
        self.high_contrast_btn.clicked.connect(self._preset_high_contrast)
        self.high_contrast_btn.setFixedWidth(140)  # ✅ FIX 6
        preset_layout.addWidget(self.high_contrast_btn)
        
        preset_group.setLayout(preset_layout)
        layout.addWidget(preset_group)
        
        layout.addStretch()
        
        # ═══════════════════════════════════════════════════════════════
        # APPLY/CANCEL BUTTONS
        # ═══════════════════════════════════════════════════════════════
        bottom_row = QHBoxLayout()
        bottom_row.addStretch()
        
        self.apply_btn = QPushButton("✅ Apply")
        self.cancel_btn = QPushButton("❌ Cancel")
        self.apply_btn.setFixedWidth(120)  # ✅ FIX 7
        self.cancel_btn.setFixedWidth(120)  # ✅ FIX 8
        
        self.apply_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)
        
        bottom_row.addWidget(self.apply_btn)
        bottom_row.addWidget(self.cancel_btn)
        layout.addLayout(bottom_row)
    
    def _update_preview(self):
        """Draw depth gradient preview."""
        width = max(self.preview.width(), 450)
        height = 60
        
        pixmap = QPixmap(width, height)
        gradient = QLinearGradient(0, 0, width, 0)
        
        # Get current scheme
        scheme_idx = self.scheme_combo.currentIndex()
        
        if scheme_idx == 0:  # Grayscale
            gradient.setColorAt(0.0, QColor(255, 255, 255))  # White (near)
            gradient.setColorAt(1.0, QColor(0, 0, 0))        # Black (far)
        elif scheme_idx == 1:  # Inverted
            gradient.setColorAt(0.0, QColor(0, 0, 0))        # Black (near)
            gradient.setColorAt(1.0, QColor(255, 255, 255))  # White (far)
        elif scheme_idx == 2:  # Rainbow
            gradient.setColorAt(0.00, QColor(0, 0, 255))     # Blue
            gradient.setColorAt(0.25, QColor(0, 255, 255))   # Cyan
            gradient.setColorAt(0.50, QColor(0, 255, 0))     # Green
            gradient.setColorAt(0.75, QColor(255, 255, 0))   # Yellow
            gradient.setColorAt(1.00, QColor(255, 0, 0))     # Red
        else:  # Heat map
            gradient.setColorAt(0.00, QColor(0, 0, 128))     # Dark Blue
            gradient.setColorAt(0.33, QColor(128, 0, 128))   # Purple
            gradient.setColorAt(0.67, QColor(255, 0, 0))     # Red
            gradient.setColorAt(1.00, QColor(255, 255, 0))   # Yellow
        
        painter = QPainter(pixmap)
        painter.fillRect(0, 0, width, height, gradient)
        
        # Draw labels
        painter.setPen(QColor(0, 0, 0, 180))
        painter.drawText(10, height - 10, f"NEAR ({self.clip_low_spin.value():.1f}%)")
        painter.drawText(width - 120, height - 10, f"FAR ({self.clip_high_spin.value():.1f}%)")
        
        painter.end()
        self.preview.setPixmap(pixmap)
    
    def _on_scheme_changed(self):
        """Color scheme dropdown changed."""
        self._update_preview()
    
    def _on_gamma_changed(self, value):
        """Gamma slider changed."""
        self.gamma = value / 100.0
        self.gamma_label.setText(f"{self.gamma:.2f}")
    
    def _preset_default(self):
        """Reset to MicroStation-like defaults."""
        self.clip_low_spin.setValue(1.0)
        self.clip_high_spin.setValue(99.0)
        self.scheme_combo.setCurrentIndex(0)  # Grayscale
        self.gamma_slider.setValue(100)  # 1.0
        self._update_preview()
    
    def _preset_high_contrast(self):
        """High contrast preset."""
        self.clip_low_spin.setValue(5.0)
        self.clip_high_spin.setValue(95.0)
        self.scheme_combo.setCurrentIndex(0)  # Grayscale
        self.gamma_slider.setValue(70)  # 0.7 = darker
        self._update_preview()
    
    def get_settings(self):
        """Return dict of settings to apply."""
        scheme_map = {
            0: "grayscale",
            1: "inverted",
            2: "rainbow",
            3: "heatmap"
        }
        
        return {
            "depth_clip_low": self.clip_low_spin.value(),
            "depth_clip_high": self.clip_high_spin.value(),
            "depth_color_scheme": scheme_map[self.scheme_combo.currentIndex()],
            "depth_gamma": self.gamma
        }