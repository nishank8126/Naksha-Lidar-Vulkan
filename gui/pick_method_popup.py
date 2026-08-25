from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QWidget, QFrame, QGroupBox
)
from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QIcon, QColor
import os

class PickMethodPopup(QDialog):
    """
    Mini popup for selecting selection method (Block vs Shape)
    Matches the NakshaAI premium aesthetic found in classification dialogs.
    """
    
    method_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Window | Qt.WindowCloseButtonHint | Qt.CustomizeWindowHint | Qt.WindowTitleHint)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.setWindowModality(Qt.NonModal)
        self.setWindowTitle("Pick Method")
        self.setFixedWidth(280)
        self._chosen_method = None
        self.init_ui()
        self.apply_styles()

    def prepare_for_show(self):
        self._chosen_method = None
        self.block_btn.setChecked(False)
        self.shape_btn.setChecked(False)

    def init_ui(self):
        # Main dialog layout
        dialog_layout = QVBoxLayout(self)
        dialog_layout.setContentsMargins(10, 10, 10, 10)
        dialog_layout.setSpacing(0)

        # Content Container (matches the look of the inner area of classification dialogs)
        self.container_box = QGroupBox("Pick Method")
        container_layout = QVBoxLayout(self.container_box)
        container_layout.setContentsMargins(20, 25, 20, 15) # Top margin large for title
        container_layout.setSpacing(15)

        # 1. Block Button
        self.block_btn = QPushButton("Block")
        self.block_btn.setObjectName("methodButton")
        self.block_btn.setCheckable(True)
        self.block_btn.setCursor(Qt.PointingHandCursor)
        container_layout.addWidget(self.block_btn)

        # 2. Shape Button
        self.shape_btn = QPushButton("Shape")
        self.shape_btn.setObjectName("methodButton")
        self.shape_btn.setCheckable(True)
        self.shape_btn.setCursor(Qt.PointingHandCursor)
        container_layout.addWidget(self.shape_btn)

        dialog_layout.addWidget(self.container_box)
        
        # Connect buttons to maintain exclusive selection (like radio buttons)
        self.block_btn.clicked.connect(self._on_block_clicked)
        self.shape_btn.clicked.connect(self._on_shape_clicked)

    def _on_block_clicked(self):
        self._chosen_method = "Block"
        self.block_btn.setChecked(True)
        self.shape_btn.setChecked(False)
        self.method_changed.emit("Block")
        self.accept() # Close popup after choice

    def _on_shape_clicked(self):
        self._chosen_method = "Shape"
        self.shape_btn.setChecked(True)
        self.block_btn.setChecked(False)
        self.method_changed.emit("Shape")
        self.accept() # Close popup after choice

    def apply_styles(self):
        from gui.theme_manager import ThemeColors as _TC
        
        accent = _TC.get('accent')
        bg_secondary = _TC.get('bg_secondary')
        bg_primary = _TC.get('bg_primary')
        border_light = _TC.get('border_light')
        text_primary = _TC.get('text_primary')
        bg_input = _TC.get('bg_input')
        
        # Consistent styling with the rest of the application (Classification Dialogs)
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {bg_secondary};
                border: 1px solid {border_light};
                border-radius: 12px;
            }}
            QGroupBox {{
                background-color: {bg_primary};
                border: 1px solid {border_light};
                border-radius: 10px;
                margin-top: 15px;
                font-weight: bold;
                color: {text_primary};
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 15px;
                padding: 0 5px;
                font-size: 11px;
                text-transform: capitalize;
                color: {accent}; /* Teal/Blue color */
            }}
            QPushButton#methodButton {{
                background-color: {bg_input};
                color: {text_primary};
                border: 1px solid {border_light};
                border-radius: 8px;
                padding: 12px 15px;
                font-size: 12px;
                font-weight: 500;
                text-align: left;
            }}
            QPushButton#methodButton:hover {{
                background-color: #333333;
                border-color: {accent};
            }}
            QPushButton#methodButton:checked {{
                background-color: {accent};
                color: #000000;
                border-color: {accent};
                font-weight: bold;
            }}
        """)
