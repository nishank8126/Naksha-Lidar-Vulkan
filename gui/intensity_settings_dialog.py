"""
Intensity Display Customization Dialog
Opens when user Shift+Clicks the Intensity button in View Ribbon
Allows adjusting brightness/darkness of intensity display
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QSlider, QMessageBox, QFrame, QGroupBox
)
from PySide6.QtCore import Qt, QSettings


class IntensitySettingsDialog(QDialog):
    """
    Dialog for customizing intensity display appearance.
    Controls gamma correction (brightness/darkness) of intensity values.
    """
    
    def __init__(self, parent=None, app=None):
        super().__init__(parent)
        self.app = app
        self.setWindowTitle("Intensity Display Settings")
        self.resize(500, 450)
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint)
        
        # ✅ Load current settings from app
        if app:
            self.gamma = getattr(app, 'intensity_gamma', 1.65)
            self.clip_low = getattr(app, 'intensity_clip_low', 0.5)
            self.clip_high = getattr(app, 'intensity_clip_high', 99.8)
        else:
            self.gamma = 1.65
            self.clip_low = 0.5
            self.clip_high = 99.8
        
        self._setup_ui()
        self._load_current_values()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        
        # Title
        title = QLabel("<h2>⚡ Intensity Display Settings</h2>")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)
        
        subtitle = QLabel(
            "Adjust how LiDAR intensity values are displayed.<br>"
            "<i>Higher gamma = darker display (emphasizes strong returns)</i>"
        )
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet("color: gray; font-size: 10pt;")
        layout.addWidget(subtitle)
        
        # ══════════════════════════════════════════════════════════
        # GAMMA CONTROL (Brightness/Darkness)
        # ══════════════════════════════════════════════════════════
        gamma_group = QGroupBox("Display Brightness")
        gamma_layout = QVBoxLayout()
        
        # Slider label
        gamma_label_row = QHBoxLayout()
        gamma_label_row.addWidget(QLabel("<b>Gamma Correction:</b>"))
        self.gamma_value_label = QLabel(f"{self.gamma:.2f}")
        self.gamma_value_label.setStyleSheet("font-weight: bold; color: #0078D4;")
        gamma_label_row.addWidget(self.gamma_value_label)
        gamma_label_row.addStretch()
        gamma_layout.addLayout(gamma_label_row)
        
        # Gamma slider (0.5 to 3.0, default 1.65)
        slider_row = QHBoxLayout()
        slider_row.addWidget(QLabel("Bright"))
        
        self.gamma_slider = QSlider(Qt.Horizontal)
        self.gamma_slider.setMinimum(50)   # 0.5
        self.gamma_slider.setMaximum(300)  # 3.0
        self.gamma_slider.setValue(int(self.gamma * 100))
        self.gamma_slider.setTickPosition(QSlider.TicksBelow)
        self.gamma_slider.setTickInterval(25)
        self.gamma_slider.valueChanged.connect(self._on_gamma_changed)
        slider_row.addWidget(self.gamma_slider)
        
        slider_row.addWidget(QLabel("Dark"))
        gamma_layout.addLayout(slider_row)
        
        # Description
        gamma_desc = QLabel(
            "• <b>Lower</b> (0.5-1.0): Brighter display, shows weak returns<br>"
            "• <b>Default</b> (1.65): MicroStation-like balanced display<br>"
            "• <b>Higher</b> (2.0-3.0): Darker display, emphasizes strong returns"
        )
        gamma_desc.setWordWrap(True)
        gamma_desc.setStyleSheet("color: gray; font-size: 9pt; margin: 8px 0;")
        gamma_layout.addWidget(gamma_desc)
        
        gamma_group.setLayout(gamma_layout)
        layout.addWidget(gamma_group)
        
        # ══════════════════════════════════════════════════════════
        # HISTOGRAM CLIPPING (Advanced)
        # ══════════════════════════════════════════════════════════
        clip_group = QGroupBox("Histogram Clipping (Advanced)")
        clip_layout = QVBoxLayout()
        
        # Low percentile
        low_row = QHBoxLayout()
        low_row.addWidget(QLabel("Clip Low (%)"))
        self.low_clip_label = QLabel(f"{self.clip_low:.1f}%")
        low_row.addWidget(self.low_clip_label)
        low_row.addStretch()
        clip_layout.addLayout(low_row)
        
        self.low_clip_slider = QSlider(Qt.Horizontal)
        self.low_clip_slider.setMinimum(0)
        self.low_clip_slider.setMaximum(50)  # 0-5.0%
        self.low_clip_slider.setValue(int(self.clip_low * 10))
        self.low_clip_slider.valueChanged.connect(self._on_low_clip_changed)
        clip_layout.addWidget(self.low_clip_slider)
        
        # High percentile
        high_row = QHBoxLayout()
        high_row.addWidget(QLabel("Clip High (%)"))
        self.high_clip_label = QLabel(f"{self.clip_high:.1f}%")
        high_row.addWidget(self.high_clip_label)
        high_row.addStretch()
        clip_layout.addLayout(high_row)
        
        self.high_clip_slider = QSlider(Qt.Horizontal)
        self.high_clip_slider.setMinimum(950)  # 95.0%
        self.high_clip_slider.setMaximum(1000) # 100.0%
        self.high_clip_slider.setValue(int(self.clip_high * 10))
        self.high_clip_slider.valueChanged.connect(self._on_high_clip_changed)
        clip_layout.addWidget(self.high_clip_slider)
        
        clip_desc = QLabel(
            "Clips extreme intensity values to improve contrast.<br>"
            "Default: 0.5% - 99.8% (recommended)"
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
        self.normal_btn = QPushButton("📊 Normal (1.0)")
        self.bright_btn = QPushButton("☀️ Bright (0.8)")
        preset_row1.addWidget(self.normal_btn)
        preset_row1.addWidget(self.bright_btn)
        layout.addLayout(preset_row1)
        
        preset_row2 = QHBoxLayout()
        self.dark_btn = QPushButton("🌙 Dark (1.65)")
        self.contrast_btn = QPushButton("🔆 High Contrast (2.2)")
        preset_row2.addWidget(self.dark_btn)
        preset_row2.addWidget(self.contrast_btn)
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
        self.normal_btn.clicked.connect(lambda: self._apply_preset(1.0, 0.5, 99.8))
        self.bright_btn.clicked.connect(lambda: self._apply_preset(0.8, 0.5, 99.8))
        self.dark_btn.clicked.connect(lambda: self._apply_preset(1.65, 0.5, 99.8))
        self.contrast_btn.clicked.connect(lambda: self._apply_preset(2.2, 1.0, 99.5))
        
        self.apply_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)
    
    def _load_current_values(self):
        """Load current values into UI"""
        self.gamma_slider.setValue(int(self.gamma * 100))
        self.low_clip_slider.setValue(int(self.clip_low * 10))
        self.high_clip_slider.setValue(int(self.clip_high * 10))
        
        self.gamma_value_label.setText(f"{self.gamma:.2f}")
        self.low_clip_label.setText(f"{self.clip_low:.1f}%")
        self.high_clip_label.setText(f"{self.clip_high:.1f}%")
    
    def _on_gamma_changed(self, value):
        """Handle gamma slider changes"""
        self.gamma = value / 100.0
        self.gamma_value_label.setText(f"{self.gamma:.2f}")
    
    def _on_low_clip_changed(self, value):
        """Handle low clip slider changes"""
        self.clip_low = value / 10.0
        self.low_clip_label.setText(f"{self.clip_low:.1f}%")
    
    def _on_high_clip_changed(self, value):
        """Handle high clip slider changes"""
        self.clip_high = value / 10.0
        self.high_clip_label.setText(f"{self.clip_high:.1f}%")
    
    def _apply_preset(self, gamma, clip_low, clip_high):
        """Apply a preset configuration"""
        self.gamma = gamma
        self.clip_low = clip_low
        self.clip_high = clip_high
        self._load_current_values()
    
    def _reset_to_default(self):
        """Reset to MicroStation-like defaults"""
        self._apply_preset(1.65, 0.5, 99.8)
        QMessageBox.information(
            self, 
            "Reset Complete", 
            "Settings reset to default MicroStation-like values."
        )
    
    def get_settings(self):
        """Return current settings as dict"""
        return {
            'gamma': self.gamma,
            'clip_low': self.clip_low,
            'clip_high': self.clip_high
        }
