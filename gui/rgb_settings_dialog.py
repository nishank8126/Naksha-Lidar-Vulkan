"""
RGB Display Customization Dialog
Opens when user Shift+Clicks the RGB button in View Ribbon
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QSlider, QCheckBox, QGroupBox
)
from PySide6.QtCore import Qt


class RGBSettingsDialog(QDialog):
    """
    Dialog for customizing RGB display appearance.
    Controls auto-stretch, gamma, and black/white points.
    """
    
    def __init__(self, parent=None, app=None):
        super().__init__(parent)
        self.app = app
        self.setWindowTitle("RGB Display Settings")
        self.resize(500, 500)
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint)
        
        # Load current settings from app
        if app:
            self.auto_stretch = getattr(app, 'rgb_auto_stretch', False)
            self.gamma = getattr(app, 'rgb_gamma', 1.0)
            self.black_point = getattr(app, 'rgb_black_point', 0.0)
            self.white_point = getattr(app, 'rgb_white_point', 100.0)
        else:
            self.auto_stretch = True
            self.gamma = 1.1
            self.black_point = 2.0
            self.white_point = 98.0
        
        self._setup_ui()
        self._load_current_values()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        
        # Title
        title = QLabel("<h2>🎨 RGB Display Settings</h2>")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)
        
        subtitle = QLabel(
            "Adjust how RGB colors are displayed for photo-realistic rendering.<br>"
            "<i>Auto-stretch improves contrast, gamma brightens midtones</i>"
        )
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet("color: gray; font-size: 10pt;")
        layout.addWidget(subtitle)
        
        # ══════════════════════════════════════════════════════════
        # AUTO-STRETCH TOGGLE
        # ══════════════════════════════════════════════════════════
        stretch_group = QGroupBox("Histogram Stretching")
        stretch_layout = QVBoxLayout()
        
        self.auto_stretch_checkbox = QCheckBox("Enable auto-stretch (recommended)")
        self.auto_stretch_checkbox.setChecked(self.auto_stretch)
        self.auto_stretch_checkbox.toggled.connect(self._on_auto_stretch_toggled)
        stretch_layout.addWidget(self.auto_stretch_checkbox)
        
        stretch_desc = QLabel(
            "Automatically adjusts contrast by stretching each color channel's histogram.<br>"
            "This makes dark scans brighter and enhances color vibrancy."
        )
        stretch_desc.setWordWrap(True)
        stretch_desc.setStyleSheet("color: gray; font-size: 9pt; margin: 8px 0;")
        stretch_layout.addWidget(stretch_desc)
        
        stretch_group.setLayout(stretch_layout)
        layout.addWidget(stretch_group)
        
        # ══════════════════════════════════════════════════════════
        # GAMMA CONTROL
        # ══════════════════════════════════════════════════════════
        gamma_group = QGroupBox("Brightness (Gamma)")
        gamma_layout = QVBoxLayout()
        
        gamma_label_row = QHBoxLayout()
        gamma_label_row.addWidget(QLabel("<b>Gamma Correction:</b>"))
        self.gamma_value_label = QLabel(f"{self.gamma:.2f}")
        self.gamma_value_label.setStyleSheet("font-weight: bold; color: #0078D4;")
        gamma_label_row.addWidget(self.gamma_value_label)
        gamma_label_row.addStretch()
        gamma_layout.addLayout(gamma_label_row)
        
        slider_row = QHBoxLayout()
        slider_row.addWidget(QLabel("Dark"))
        
        self.gamma_slider = QSlider(Qt.Horizontal)
        self.gamma_slider.setMinimum(80)   # 0.8
        self.gamma_slider.setMaximum(140)  # 1.4
        self.gamma_slider.setValue(int(self.gamma * 100))
        self.gamma_slider.setTickPosition(QSlider.TicksBelow)
        self.gamma_slider.setTickInterval(10)
        self.gamma_slider.valueChanged.connect(self._on_gamma_changed)
        slider_row.addWidget(self.gamma_slider)
        
        slider_row.addWidget(QLabel("Bright"))
        gamma_layout.addLayout(slider_row)
        
        gamma_desc = QLabel(
            "• <b>Lower</b> (0.8-1.0): Darker, preserves original tone<br>"
            "• <b>Default</b> (1.1): Slightly brighter midtones (natural)<br>"
            "• <b>Higher</b> (1.2-1.4): Brighter, good for dark scans"
        )
        gamma_desc.setWordWrap(True)
        gamma_desc.setStyleSheet("color: gray; font-size: 9pt; margin: 8px 0;")
        gamma_layout.addWidget(gamma_desc)
        
        gamma_group.setLayout(gamma_layout)
        layout.addWidget(gamma_group)
        
        # ══════════════════════════════════════════════════════════
        # CLIPPING CONTROLS
        # ══════════════════════════════════════════════════════════
        clip_group = QGroupBox("Histogram Clipping (Advanced)")
        clip_layout = QVBoxLayout()
        
        # Black point
        black_row = QHBoxLayout()
        black_row.addWidget(QLabel("Black Point (%)"))
        self.black_point_label = QLabel(f"{self.black_point:.1f}%")
        black_row.addWidget(self.black_point_label)
        black_row.addStretch()
        clip_layout.addLayout(black_row)
        
        self.black_point_slider = QSlider(Qt.Horizontal)
        self.black_point_slider.setMinimum(0)
        self.black_point_slider.setMaximum(100)  # 0-10%
        self.black_point_slider.setValue(int(self.black_point * 10))
        self.black_point_slider.valueChanged.connect(self._on_black_point_changed)
        clip_layout.addWidget(self.black_point_slider)
        
        # White point
        white_row = QHBoxLayout()
        white_row.addWidget(QLabel("White Point (%)"))
        self.white_point_label = QLabel(f"{self.white_point:.1f}%")
        white_row.addWidget(self.white_point_label)
        white_row.addStretch()
        clip_layout.addLayout(white_row)
        
        self.white_point_slider = QSlider(Qt.Horizontal)
        self.white_point_slider.setMinimum(900)  # 90%
        self.white_point_slider.setMaximum(1000) # 100%
        self.white_point_slider.setValue(int(self.white_point * 10))
        self.white_point_slider.valueChanged.connect(self._on_white_point_changed)
        clip_layout.addWidget(self.white_point_slider)
        
        clip_desc = QLabel(
            "Clips extreme dark/bright pixels to improve overall contrast.<br>"
            "Default: 2% - 98% (recommended for most scans)"
        )
        clip_desc.setWordWrap(True)
        clip_desc.setStyleSheet("color: gray; font-size: 9pt; margin: 8px 0;")
        clip_layout.addWidget(clip_desc)
        
        clip_group.setLayout(clip_layout)
        layout.addWidget(clip_group)
        
        # ══════════════════════════════════════════════════════════
        # PRESETS
        # ══════════════════════════════════════════════════════════
        preset_label = QLabel("<b>Quick Presets:</b>")
        layout.addWidget(preset_label)
        
        preset_row1 = QHBoxLayout()
        self.natural_btn = QPushButton("📷 Natural (Default)")
        self.vivid_btn = QPushButton("🌈 Vivid Colors")
        preset_row1.addWidget(self.natural_btn)
        preset_row1.addWidget(self.vivid_btn)
        layout.addLayout(preset_row1)
        
        preset_row2 = QHBoxLayout()
        self.bright_btn = QPushButton("☀️ Bright")
        self.raw_btn = QPushButton("🎞️ Raw (No Processing)")
        preset_row2.addWidget(self.bright_btn)
        preset_row2.addWidget(self.raw_btn)
        layout.addLayout(preset_row2)
        
        layout.addStretch()
        
        # ══════════════════════════════════════════════════════════
        # BUTTONS
        # ══════════════════════════════════════════════════════════
        bottom_row = QHBoxLayout()
        
        self.reset_btn = QPushButton("🔄 Reset to Default")
        self.reset_btn.clicked.connect(self._reset_to_default)
        bottom_row.addWidget(self.reset_btn)
        
        bottom_row.addStretch()
        
        self.apply_btn = QPushButton("✅ Apply")
        self.cancel_btn = QPushButton("❌ Cancel")
        self.apply_btn.setFixedWidth(120)
        self.cancel_btn.setFixedWidth(120)
        bottom_row.addWidget(self.apply_btn)
        bottom_row.addWidget(self.cancel_btn)
        layout.addLayout(bottom_row)
        
        # Connect signals
        self.natural_btn.clicked.connect(lambda: self._apply_preset(True, 1.1, 2.0, 98.0))
        self.vivid_btn.clicked.connect(lambda: self._apply_preset(True, 1.2, 1.0, 99.0))
        self.bright_btn.clicked.connect(lambda: self._apply_preset(True, 1.3, 3.0, 97.0))
        self.raw_btn.clicked.connect(lambda: self._apply_preset(False, 1.0, 0.0, 100.0))
        
        self.apply_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)
    
    def _load_current_values(self):
        """Load current values into UI"""
        self.auto_stretch_checkbox.setChecked(self.auto_stretch)
        self.gamma_slider.setValue(int(self.gamma * 100))
        self.black_point_slider.setValue(int(self.black_point * 10))
        self.white_point_slider.setValue(int(self.white_point * 10))
        
        self.gamma_value_label.setText(f"{self.gamma:.2f}")
        self.black_point_label.setText(f"{self.black_point:.1f}%")
        self.white_point_label.setText(f"{self.white_point:.1f}%")
    
    def _on_auto_stretch_toggled(self, checked):
        """Handle auto-stretch toggle"""
        self.auto_stretch = checked
    
    def _on_gamma_changed(self, value):
        """Handle gamma slider changes"""
        self.gamma = value / 100.0
        self.gamma_value_label.setText(f"{self.gamma:.2f}")
    
    def _on_black_point_changed(self, value):
        """Handle black point slider changes"""
        self.black_point = value / 10.0
        self.black_point_label.setText(f"{self.black_point:.1f}%")
    
    def _on_white_point_changed(self, value):
        """Handle white point slider changes"""
        self.white_point = value / 10.0
        self.white_point_label.setText(f"{self.white_point:.1f}%")
    
    def _apply_preset(self, auto_stretch, gamma, black_point, white_point):
        """Apply a preset configuration"""
        self.auto_stretch = auto_stretch
        self.gamma = gamma
        self.black_point = black_point
        self.white_point = white_point
        self._load_current_values()
    
    def _reset_to_default(self):
        """Reset to MicroStation-like defaults"""
        self._apply_preset(True, 1.1, 2.0, 98.0)
    
    def get_settings(self):
        """Return current settings as dict"""
        return {
            'auto_stretch': self.auto_stretch,
            'gamma': self.gamma,
            'black_point': self.black_point,
            'white_point': self.white_point
        }
