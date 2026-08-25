"""
Auto-Backup Settings Dialog
Allows users to configure:
- Backup location (same folder or custom path)
- Backup interval (1-60 minutes)
- Backup file naming format
- Enable/disable auto-backup
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QLineEdit, QSpinBox, QFileDialog, QGroupBox, QCheckBox,
    QComboBox, QMessageBox, QRadioButton
)
from PySide6.QtCore import QSettings, Signal, Qt
from gui.popup_guard import InputPopupMixin


class BackupSettingsDialog(InputPopupMixin, QDialog):
    """
    Dialog for configuring auto-backup settings.
    """
    settings_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("BackupSettingsDialog")
        self.setProperty("themeStyledDialog", True)
        self.setWindowTitle("Auto-Backup Settings")
        self.setFixedSize(680, 440)

        # Load settings object
        self.settings = QSettings("NakshaAI", "LidarApp")

        # Apply theme-aware style
        self._apply_local_style()

        # Build UI
        self._create_ui()

        # Load saved values
        self._load_settings()

    # ------------------------------------------------------------------ #
    #  LOCAL STYLE (override global theme inside this dialog only)
    # ------------------------------------------------------------------ #
    def _apply_local_style(self):
        from gui.theme_manager import get_dialog_stylesheet

        self.setStyleSheet(get_dialog_stylesheet())

    # ------------------------------------------------------------------ #
    #  UI BUILD
    # ------------------------------------------------------------------ #
    def _create_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        # ============================================
        # Enable / Disable Auto-Backup
        # ============================================
        self.enable_checkbox = QCheckBox("Enable Auto-Backup")
        self.enable_checkbox.setStyleSheet(
            "QCheckBox { font-weight: bold; font-size: 11pt; }"
        )
        layout.addWidget(self.enable_checkbox)

        # ============================================
        # Backup Location Group
        # ============================================
        location_group = QGroupBox("Backup Location")
        location_layout = QVBoxLayout()
        location_layout.setContentsMargins(8, 8, 8, 8)
        location_layout.setSpacing(6)

        # Option 1: Same folder
        self.same_folder_radio = QRadioButton("Same folder as original file")
        # Option 2: Custom folder
        self.custom_folder_radio = QRadioButton("Custom backup folder:")
        location_layout.addWidget(self.same_folder_radio)
        location_layout.addWidget(self.custom_folder_radio)

        # Path selector row
        path_layout = QHBoxLayout()
        path_layout.setContentsMargins(18, 0, 0, 0)  # small indent under radios
        path_layout.setSpacing(8)

        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("Example: C:/Backups/LidarBackups")
        self.path_input.setMinimumWidth(380)

        self.browse_btn = QPushButton("Browse")
        self.browse_btn.setFixedWidth(90)
        self.browse_btn.setObjectName("secondaryBtn")
        self.browse_btn.setAutoDefault(False)
        self.browse_btn.setDefault(False)
        self.browse_btn.setFocusPolicy(Qt.NoFocus)
        self.browse_btn.clicked.connect(self._browse_folder)

        path_layout.addWidget(self.path_input, 1)
        path_layout.addWidget(self.browse_btn, 0)

        location_layout.addLayout(path_layout)
        location_group.setLayout(location_layout)
        layout.addWidget(location_group)

        # ============================================
        # Backup Interval Group
        # ============================================
        interval_group = QGroupBox("Backup Frequency")
        interval_layout = QHBoxLayout()
        interval_layout.setContentsMargins(8, 6, 8, 6)
        interval_layout.setSpacing(10)

        interval_layout.addWidget(QLabel("Auto-save every:"))

        self.interval_spinbox = QSpinBox()
        self.interval_spinbox.setRange(1, 60)
        self.interval_spinbox.setSuffix(" minutes")
        self.interval_spinbox.setValue(5)
        interval_layout.addWidget(self.interval_spinbox)

        interval_layout.addSpacing(10)
        interval_layout.addWidget(QLabel("Quick set:"))

        self.preset_combo = QComboBox()
        self.preset_combo.addItems(["1 min", "5 min", "10 min", "15 min", "30 min"])
        self.preset_combo.setCurrentIndex(1)
        self.preset_combo.currentTextChanged.connect(self._apply_preset)
        interval_layout.addWidget(self.preset_combo)

        interval_layout.addStretch()
        interval_group.setLayout(interval_layout)
        layout.addWidget(interval_group)

        # ============================================
        # Backup Naming Group
        # ============================================
        naming_group = QGroupBox("Backup File Naming")
        naming_layout = QHBoxLayout()
        naming_layout.setContentsMargins(8, 6, 8, 6)
        naming_layout.setSpacing(10)

        naming_layout.addWidget(QLabel("Format:"))

        self.naming_combo = QComboBox()
        self.naming_combo.addItems([
            "filename_backup.laz",
            "filename_YYYYMMDD_HHMMSS.laz",
            "filename_autosave.laz"
        ])
        naming_layout.addWidget(self.naming_combo, 1)
        naming_layout.addStretch()

        naming_group.setLayout(naming_layout)
        layout.addWidget(naming_group)

        layout.addStretch()

        # ============================================
        # Buttons row
        # ============================================
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setAutoDefault(False)
        self.cancel_btn.setDefault(False)
        self.cancel_btn.setFocusPolicy(Qt.NoFocus)
        self.cancel_btn.clicked.connect(self.reject)

        self.save_btn = QPushButton("Save Settings")
        self.save_btn.setObjectName("primaryBtn")
        self.save_btn.setAutoDefault(False)
        self.save_btn.setDefault(False)
        self.save_btn.setFocusPolicy(Qt.NoFocus)
        self.save_btn.clicked.connect(self._save_settings)

        btn_layout.addWidget(self.cancel_btn)
        btn_layout.addWidget(self.save_btn)

        layout.addLayout(btn_layout)

        # Connect logic
        self.same_folder_radio.toggled.connect(self._toggle_path_options)
        self.custom_folder_radio.toggled.connect(self._toggle_path_options)
        self.enable_checkbox.toggled.connect(self._toggle_all_options)

    # ------------------------------------------------------------------ #
    #  HELPERS
    # ------------------------------------------------------------------ #
    def _browse_folder(self):
        """Open folder browser dialog."""
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Backup Folder",
            self.path_input.text() or ""
        )
        if folder:
            self.path_input.setText(folder)

    def _apply_preset(self, text: str):
        """Apply quick preset interval."""
        if "1 min" in text:
            self.interval_spinbox.setValue(1)
        elif "5 min" in text:
            self.interval_spinbox.setValue(5)
        elif "10 min" in text:
            self.interval_spinbox.setValue(10)
        elif "15 min" in text:
            self.interval_spinbox.setValue(15)
        elif "30 min" in text:
            self.interval_spinbox.setValue(30)

    def _toggle_path_options(self):
        """Enable/disable path input based on radio selection."""
        custom_selected = self.custom_folder_radio.isChecked()
        self.path_input.setEnabled(custom_selected)
        self.browse_btn.setEnabled(custom_selected)

        # Ensure at least one is always selected
        if not self.same_folder_radio.isChecked() and not self.custom_folder_radio.isChecked():
            self.same_folder_radio.setChecked(True)

    def _toggle_all_options(self, enabled: bool):
        """Enable/disable all options based on main checkbox."""
        self.same_folder_radio.setEnabled(enabled)
        self.custom_folder_radio.setEnabled(enabled)
        self.interval_spinbox.setEnabled(enabled)
        self.preset_combo.setEnabled(enabled)
        self.naming_combo.setEnabled(enabled)

        if enabled and self.custom_folder_radio.isChecked():
            self.path_input.setEnabled(True)
            self.browse_btn.setEnabled(True)
        else:
            self.path_input.setEnabled(False)
            self.browse_btn.setEnabled(False)

    # ------------------------------------------------------------------ #
    #  SETTINGS LOAD / SAVE
    # ------------------------------------------------------------------ #
    def _load_settings(self):
        """Load saved settings from QSettings."""
        enabled = self.settings.value("backup_enabled", True, type=bool)
        self.enable_checkbox.setChecked(enabled)

        use_custom = self.settings.value("backup_use_custom_path", False, type=bool)
        custom_path = self.settings.value("backup_custom_path", "", type=str)

        if use_custom:
            self.custom_folder_radio.setChecked(True)
            self.path_input.setText(custom_path)
        else:
            self.same_folder_radio.setChecked(True)

        interval = self.settings.value("backup_interval_minutes", 5, type=int)
        self.interval_spinbox.setValue(interval)

        # Match preset combo
        if interval == 1:
            self.preset_combo.setCurrentIndex(0)
        elif interval == 5:
            self.preset_combo.setCurrentIndex(1)
        elif interval == 10:
            self.preset_combo.setCurrentIndex(2)
        elif interval == 15:
            self.preset_combo.setCurrentIndex(3)
        elif interval == 30:
            self.preset_combo.setCurrentIndex(4)
        else:
            self.preset_combo.setCurrentIndex(-1)

        naming_format = self.settings.value("backup_naming_format", 0, type=int)
        self.naming_combo.setCurrentIndex(naming_format)

        # Apply enabled/disabled state to controls
        self._toggle_all_options(enabled)

    def _save_settings(self):
        """Save settings and apply to application."""
        import os

        # Validate custom path if selected
        if self.custom_folder_radio.isChecked():
            custom_path = self.path_input.text().strip()
            if not custom_path:
                QMessageBox.warning(self, "Invalid Path", "Please select a backup folder.")
                return

            if not os.path.exists(custom_path):
                try:
                    os.makedirs(custom_path)
                    print(f"✅ Created backup folder: {custom_path}")
                except Exception as e:
                    QMessageBox.critical(self, "Error", f"Cannot create folder:\n{e}")
                    return

        # Save settings
        self.settings.setValue("backup_enabled", self.enable_checkbox.isChecked())
        self.settings.setValue("backup_use_custom_path", self.custom_folder_radio.isChecked())
        self.settings.setValue("backup_custom_path", self.path_input.text())
        self.settings.setValue("backup_interval_minutes", self.interval_spinbox.value())
        self.settings.setValue("backup_naming_format", self.naming_combo.currentIndex())
        self.settings.sync()

        print("✅ Backup settings saved:")
        print(f"   Enabled: {self.enable_checkbox.isChecked()}")
        print(f"   Interval: {self.interval_spinbox.value()} minutes")
        if self.custom_folder_radio.isChecked():
            print(f"   Location: {self.path_input.text()}")
        else:
            print("   Location: Same as original file")

        # Notify main app
        self.settings_changed.emit()

        QMessageBox.information(
            self,
            "Settings Saved",
            f"✅ Backup settings saved successfully!\n\n"
            f"Interval: {self.interval_spinbox.value()} minutes\n"
            f"Status: {'Enabled' if self.enable_checkbox.isChecked() else 'Disabled'}"
        )

        self.accept()
