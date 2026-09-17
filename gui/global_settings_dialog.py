import json

from PySide6.QtCore import QSettings, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QColorDialog,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QHeaderView,
)

from PySide6.QtWidgets import QCheckBox

from gui.draw_settings_dialog import (
    DEFAULT_DRAW_STYLES,
    FILLABLE_TOOLS,
    TOOL_DISPLAY_NAMES,
    TOOL_ORDER,
    load_draw_settings,
    qcolor_to_vtk,
    save_draw_settings,
    vtk_color_to_qcolor,
)
from gui.shortcut_manager import ShortcutManager, TOOLS, SIMPLE_SHORTCUT_TOOLS


SHORTCUT_MODIFIERS = [
    "alt",
    "ctrl",
    "shift",
    "alt+shift",
    "ctrl+alt",
    "ctrl+shift",
    "ctrl+alt+shift",
    "none",
]

SHORTCUT_KEYS = [f"F{i}" for i in range(1, 13)] + [chr(i) for i in range(65, 91)] + ["Space"]

SHORTCUT_SIMPLE_TOOLS = set(SIMPLE_SHORTCUT_TOOLS)

class SettingsPreviewWidget(QWidget):
    """Preview widget for stroke + optional fill settings."""

    _STYLE_MAP = {
        "solid":        Qt.SolidLine,
        "dashed":       Qt.DashLine,
        "dotted":       Qt.DotLine,
        "dash-dot":     Qt.DashDotLine,
        "dash-dot-dot": Qt.DashDotDotLine,
    }

    def __init__(self, shape_mode=False, parent=None):
        super().__init__(parent)
        self._color = QColor("#ff00ff")
        self._width = 3
        self._style = "solid"
        self._fill_enabled = False
        self._fill_color = QColor("#ff00ff")
        self._fill_opacity = 0.3
        self._shape_mode = shape_mode
        self.setFixedHeight(72)
        self.setMinimumWidth(220)

    def set_state(self, color: QColor, width: int, style: str,
                  fill_enabled=False, fill_color=None, fill_opacity=0.3):
        self._color = color
        self._width = width
        self._style = style
        self._fill_enabled = fill_enabled
        self._fill_color = fill_color if fill_color is not None else color
        self._fill_opacity = fill_opacity
        self.update()

    def paintEvent(self, event):
        from gui.theme_manager import ThemeColors
        from PySide6.QtGui import QBrush
        from PySide6.QtCore import QRectF

        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(ThemeColors.get("bg_secondary")))
        painter.setRenderHint(QPainter.Antialiasing)

        pen = QPen(self._color)
        pen.setWidth(max(1, int(self._width)))
        pen.setStyle(self._STYLE_MAP.get(self._style, Qt.SolidLine))

        if self._shape_mode:
            # Draw a rounded rectangle showing fill + outline together
            margin = 10
            rect = QRectF(margin, margin, self.width() - margin * 2, self.height() - margin * 2)

            if self._fill_enabled:
                fill = QColor(self._fill_color)
                fill.setAlphaF(max(0.0, min(1.0, self._fill_opacity)))
                painter.setBrush(QBrush(fill))
            else:
                painter.setBrush(Qt.NoBrush)

            painter.setPen(pen)
            painter.drawRoundedRect(rect, 5, 5)

            # Opacity annotation when fill is on
            if self._fill_enabled:
                pct = int(self._fill_opacity * 100)
                painter.setPen(QColor(ThemeColors.get("text_secondary", "#888")))
                painter.setFont(painter.font())
                painter.drawText(self.rect().adjusted(0, 0, -8, -4),
                                 Qt.AlignRight | Qt.AlignBottom,
                                 f"fill {pct}%")
        else:
            # Simple horizontal line (used by cross-section preview)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(pen)
            y = self.height() // 2
            painter.drawLine(20, y, self.width() - 20, y)


class GlobalSettingsDialog(QDialog):
    """Category-based global settings window."""

    def __init__(self, app, parent=None):
        super().__init__(parent or app)
        self.app = app
        self.settings = QSettings("NakshaAI", "LidarApp")

        self._draw_styles = {}
        self._selected_draw_tool = TOOL_ORDER[0]
        self._loading_shortcuts = False
        self._dirty_categories = set()
        self._loading = False
        self._shortcuts_needs_reload = True

        self.setWindowTitle("Global Settings")
        self.setModal(False)
        self.resize(900, 620)

        self._init_colors()
        self._build_ui()

    def _init_colors(self):
        try:
            from gui.theme_manager import ThemeColors
            is_light = ThemeColors.is_light() if hasattr(ThemeColors, "is_light") else True
        except Exception:
            is_light = True

        if is_light:
            self.c_bg          = "#ffffff"
            self.c_bg_alt      = "#f5f5f5"
            self.c_bg_sidebar  = "#3a3a3a"
            self.c_sidebar_sel = "#4a4a4a"
            self.c_sidebar_txt = "#cccccc"
            self.c_sidebar_txt_sel = "#ffffff"
            self.c_border      = "#d0d5dd"
            self.c_text        = "#1b2838"
            self.c_text_muted  = "#6b7280"
            self.c_accent      = "#2c5f8a"
        else:
            self.c_bg          = "#1e1e2e"
            self.c_bg_alt      = "#2a2a3a"
            self.c_bg_sidebar  = "#181820"
            self.c_sidebar_sel = "#2a2a3a"
            self.c_sidebar_txt = "#aaaacc"
            self.c_sidebar_txt_sel = "#e0e0ff"
            self.c_border      = "#3c3c5c"
            self.c_text        = "#e0e0f0"
            self.c_text_muted  = "#888899"
            self.c_accent      = "#4a90d9"

    def _apply_style(self):
        self._title_bar.setStyleSheet(
            f"background:{self.c_accent}; color:#ffffff; font-weight:bold;"
            "font-size:11pt; padding-left:10px;"
        )
        self.category_list.setStyleSheet(f"""
            QListWidget#gsdSidebar {{
                background:{self.c_bg_sidebar};
                border:none; outline:none;
                font-size:9.5pt;
            }}
            QListWidget#gsdSidebar::item {{
                color:{self.c_sidebar_txt};
                padding:6px 4px;
                border-left:3px solid transparent;
            }}
            QListWidget#gsdSidebar::item:selected {{
                background:{self.c_sidebar_sel};
                color:{self.c_sidebar_txt_sel};
                border-left:3px solid {self.c_accent};
                font-weight:bold;
            }}
            QListWidget#gsdSidebar::item:hover:!selected {{
                background:#444444;
            }}
        """)
        self.setStyleSheet(f"""
            QDialog {{ background:{self.c_bg}; }}
            QWidget {{ background:{self.c_bg}; color:{self.c_text}; }}
            QGroupBox {{
                font-size:9.5pt; font-weight:bold;
                border:1px solid {self.c_border}; border-radius:4px;
                margin-top:10px; color:{self.c_text};
            }}
            QGroupBox::title {{
                subcontrol-origin:margin; left:10px; padding:0 4px;
            }}
            QLabel {{ background:transparent; color:{self.c_text}; }}
            QLabel#dialogCaption {{ color:{self.c_text_muted}; font-size:8.5pt; }}
            QComboBox, QDoubleSpinBox, QLineEdit {{
                font-size:9pt; min-height:22px; padding:2px 6px;
                border:1px solid {self.c_border}; border-radius:3px;
                background:{self.c_bg}; color:{self.c_text};
            }}
            QComboBox:focus, QDoubleSpinBox:focus, QLineEdit:focus {{
                border-color:{self.c_accent};
            }}
            QSlider::groove:horizontal {{
                height:4px; background:{self.c_border}; border-radius:2px;
            }}
            QSlider::handle:horizontal {{
                background:{self.c_accent}; width:14px; height:14px;
                margin:-5px 0; border-radius:7px;
            }}
            QPushButton {{
                font-size:9pt; padding:3px 12px; min-height:24px;
                border:1px solid {self.c_border}; border-radius:3px;
                background:{self.c_bg_alt}; color:{self.c_text};
            }}
            QPushButton:hover {{ background:{self.c_border}; }}
            QPushButton:disabled {{ color:{self.c_text_muted}; }}
            QPushButton#primaryBtn {{
                background:{self.c_accent}; color:#ffffff;
                border:none; font-weight:bold;
            }}
            QPushButton#primaryBtn:hover {{ background:{self.c_accent}; }}
            QPushButton#secondaryBtn {{
                background:{self.c_bg_alt}; color:{self.c_text};
            }}
            QTableWidget {{
                background:{self.c_bg}; alternate-background-color:{self.c_bg_alt};
                color:{self.c_text}; gridline-color:transparent;
                font-size:9pt; border:none; outline:none;
            }}
            QTableWidget::item:selected {{
                background:#dce8f5; color:{self.c_text};
            }}
            QHeaderView::section {{
                background:{self.c_bg_alt}; color:{self.c_text_muted};
                border:none; border-bottom:1px solid {self.c_border};
                padding:2px 4px; font-size:8.5pt;
            }}
            QFrame[frameShape="4"], QFrame[frameShape="5"] {{
                color:{self.c_border}; background:{self.c_border}; max-height:1px;
            }}
            QScrollBar:vertical {{
                width:8px; background:{self.c_bg_alt};
            }}
            QScrollBar::handle:vertical {{
                background:{self.c_border}; border-radius:4px; min-height:30px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
        """)

    def refresh_theme(self):
        self._init_colors()
        if hasattr(self, "_title_bar"):
            self._apply_style()

    def showEvent(self, event):
        self.refresh_theme()
        self._dirty_categories.clear()
        self._shortcuts_needs_reload = True
        self._load_values()
        super().showEvent(event)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Title bar
        self._title_bar = QLabel("  Global Settings")
        self._title_bar.setFixedHeight(28)
        root.addWidget(self._title_bar)

        # ── Body: sidebar | stack
        body = QSplitter(Qt.Horizontal)
        body.setHandleWidth(0)
        root.addWidget(body, 1)

        # Sidebar
        self.category_list = QListWidget()
        self.category_list.setObjectName("gsdSidebar")
        self.category_list.setFixedWidth(160)
        for label, icon in [
            ("Theme",         "◑"),
            ("Shortcuts",     "⌨"),
            ("Cross Section", "✂"),
            ("Draw",          "✏"),
            ("Navigation",    "⊙"),
        ]:
            item = QListWidgetItem(f"  {icon}  {label}")
            item.setSizeHint(QSize(160, 32))
            self.category_list.addItem(item)
        self.category_list.currentRowChanged.connect(self._on_category_changed)
        body.addWidget(self.category_list)

        # Right panel
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self.category_stack = QStackedWidget()
        self.category_stack.addWidget(self._build_theme_page())
        self.category_stack.addWidget(self._build_shortcuts_page())
        self.category_stack.addWidget(self._build_cross_section_page())
        self.category_stack.addWidget(self._build_draw_page())
        self.category_stack.addWidget(self._build_navigation_page())
        right_layout.addWidget(self.category_stack, 1)

        # Footer separator + bar
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        right_layout.addWidget(sep)

        footer_widget = QWidget()
        footer_widget.setFixedHeight(46)
        footer_layout = QHBoxLayout(footer_widget)
        footer_layout.setContentsMargins(14, 10, 14, 10)

        self.footer_note = QLabel("Save All applies only changed categories.")
        self.footer_note.setObjectName("dialogCaption")
        footer_layout.addWidget(self.footer_note)
        footer_layout.addStretch()

        self.save_all_btn = QPushButton("Save All")
        self.save_all_btn.setObjectName("primaryBtn")
        self.save_all_btn.setFixedWidth(82)
        self.save_all_btn.clicked.connect(self.save_all_settings)
        footer_layout.addWidget(self.save_all_btn)

        self.close_btn = QPushButton("Close")
        self.close_btn.setFixedWidth(70)
        self.close_btn.clicked.connect(self.close)
        footer_layout.addWidget(self.close_btn)

        right_layout.addWidget(footer_widget)
        body.addWidget(right)
        body.setSizes([160, 740])

        self._apply_style()
        self.category_list.setCurrentRow(0)
        self._connect_dirty_signals()

    def _build_theme_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        group = QGroupBox("Theme")
        form = QFormLayout(group)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)

        self.theme_combo = QComboBox()
        self.theme_combo.addItem("Dark", "dark")
        self.theme_combo.addItem("Light", "light")
        form.addRow("Application theme:", self.theme_combo)

        self.canvas_theme_combo = QComboBox()
        self.canvas_theme_combo.addItem("Black", "black")
        self.canvas_theme_combo.addItem("White", "white")
        form.addRow("Canvas theme:", self.canvas_theme_combo)

        self.theme_summary = QLabel()
        self.theme_summary.setObjectName("dialogCaption")
        self.theme_summary.setWordWrap(True)

        layout.addWidget(group)
        layout.addWidget(self.theme_summary)
        layout.addStretch()
        return page

    def _build_shortcuts_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        group = QGroupBox("Shortcuts")
        group_layout = QVBoxLayout(group)
        group_layout.setSpacing(8)

        self.shortcuts_summary = QLabel()
        self.shortcuts_summary.setObjectName("dialogCaption")
        self.shortcuts_summary.setWordWrap(True)
        group_layout.addWidget(self.shortcuts_summary)

        # ═══════════════════════════════════════════════════════════════
        # ✅ NEW: Enhanced search bar with keyboard capture mode
        # ═══════════════════════════════════════════════════════════════
        from PySide6.QtWidgets import QLineEdit, QCheckBox
        from PySide6.QtCore import Qt, QEvent
        from PySide6.QtGui import QKeyEvent

        search_layout = QHBoxLayout()
        search_layout.setSpacing(8)

        search_label = QLabel("Search:")
        search_layout.addWidget(search_label)

        # Custom QLineEdit that captures keyboard shortcuts
        class ShortcutCaptureLineEdit(QLineEdit):
            def __init__(self, parent=None):
                super().__init__(parent)
                self.capture_mode = False
                self.parent_dialog = parent
                self._pressed_modifiers = set()
                
            def keyPressEvent(self, event):
                if not self.capture_mode:
                    # Normal typing mode
                    super().keyPressEvent(event)
                    return
                    
                # Keyboard capture mode - build shortcut string
                key = event.key()
                
                # Track modifier keys being held
                if key == Qt.Key_Control:
                    self._pressed_modifiers.add("ctrl")
                elif key == Qt.Key_Alt:
                    self._pressed_modifiers.add("alt")
                elif key == Qt.Key_Shift:
                    self._pressed_modifiers.add("shift")
                
                # Build modifier list from currently pressed modifiers
                modifiers = []
                if event.modifiers() & Qt.ControlModifier or "ctrl" in self._pressed_modifiers:
                    if "ctrl" not in modifiers:
                        modifiers.append("ctrl")
                if event.modifiers() & Qt.AltModifier or "alt" in self._pressed_modifiers:
                    if "alt" not in modifiers:
                        modifiers.append("alt")
                if event.modifiers() & Qt.ShiftModifier or "shift" in self._pressed_modifiers:
                    if "shift" not in modifiers:
                        modifiers.append("shift")
                
                # Sort modifiers in standard order
                modifier_order = {"ctrl": 0, "alt": 1, "shift": 2}
                modifiers.sort(key=lambda x: modifier_order.get(x, 99))
                
                # Determine the actual key (non-modifier)
                key_text = ""
                is_modifier_only = False
                
                # Check if it's ONLY a modifier key press
                if key in (Qt.Key_Control, Qt.Key_Alt, Qt.Key_Shift):
                    is_modifier_only = True
                    # Show modifiers being held
                    if modifiers:
                        search_text = "+".join(modifiers)
                        self.setText(search_text)
                else:
                    # Function keys F1-F12
                    if Qt.Key_F1 <= key <= Qt.Key_F12:
                        key_text = f"F{key - Qt.Key_F1 + 1}"
                    # Letters A-Z
                    elif Qt.Key_A <= key <= Qt.Key_Z:
                        key_text = chr(key).upper()
                    # Space
                    elif key == Qt.Key_Space:
                        key_text = "Space"
                    # Numbers 0-9
                    elif Qt.Key_0 <= key <= Qt.Key_9:
                        key_text = chr(key)
                    # Escape, Enter, etc.
                    elif key == Qt.Key_Escape:
                        self.clear()
                        self._pressed_modifiers.clear()
                        event.accept()
                        return
                    else:
                        # Unknown key - ignore
                        event.accept()
                        return
                    
                    # Build complete search string with modifiers + key
                    if modifiers and key_text:
                        search_text = "+".join(modifiers) + "+" + key_text
                    elif key_text:
                        search_text = key_text
                    else:
                        event.accept()
                        return
                    
                    self.setText(search_text.lower())
                    self._pressed_modifiers.clear()  # Reset after capturing full combo
                
                event.accept()
            
            def keyReleaseEvent(self, event):
                if self.capture_mode:
                    # Track when modifiers are released
                    key = event.key()
                    if key == Qt.Key_Control:
                        self._pressed_modifiers.discard("ctrl")
                    elif key == Qt.Key_Alt:
                        self._pressed_modifiers.discard("alt")
                    elif key == Qt.Key_Shift:
                        self._pressed_modifiers.discard("shift")
                super().keyReleaseEvent(event)

        self.shortcuts_search = ShortcutCaptureLineEdit(self)
        self.shortcuts_search.setPlaceholderText("Type to search or enable capture mode to press keys...")
        self.shortcuts_search.setClearButtonEnabled(True)
        self.shortcuts_search.textChanged.connect(self._filter_shortcuts)
        search_layout.addWidget(self.shortcuts_search, stretch=1)

        # Capture mode toggle
        self.shortcuts_capture_mode = QCheckBox("Capture Keys")
        self.shortcuts_capture_mode.setToolTip(
            "Enable to press actual keyboard shortcuts (e.g., Shift+F1) instead of typing"
        )
        self.shortcuts_capture_mode.toggled.connect(self._toggle_capture_mode)
        search_layout.addWidget(self.shortcuts_capture_mode)

        group_layout.addLayout(search_layout)
        # ═══════════════════════════════════════════════════════════════

        self.shortcuts_table = QTableWidget(0, 4)
        self.shortcuts_table.setHorizontalHeaderLabels(["Modifier", "Key", "Tool", "Details"])
        self.shortcuts_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.shortcuts_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.shortcuts_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.shortcuts_table.verticalHeader().setVisible(False)
        header = self.shortcuts_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        group_layout.addWidget(self.shortcuts_table, 1)

        row_buttons = QHBoxLayout()
        self.shortcut_add_btn = QPushButton("Add Row")
        self.shortcut_add_btn.setObjectName("secondaryBtn")
        self.shortcut_add_btn.clicked.connect(self._add_empty_shortcut_row)
        row_buttons.addWidget(self.shortcut_add_btn)

        self.shortcut_remove_btn = QPushButton("Remove Row")
        self.shortcut_remove_btn.setObjectName("secondaryBtn")
        self.shortcut_remove_btn.clicked.connect(self._remove_selected_shortcut_row)
        row_buttons.addWidget(self.shortcut_remove_btn)

        self.shortcut_reload_btn = QPushButton("Reload Saved")
        self.shortcut_reload_btn.setObjectName("secondaryBtn")
        self.shortcut_reload_btn.clicked.connect(self._load_shortcuts)
        row_buttons.addWidget(self.shortcut_reload_btn)

        row_buttons.addStretch()
        group_layout.addLayout(row_buttons)

        layout.addWidget(group, 1)
        return page

    def _build_cross_section_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        group = QGroupBox("Cross Section")
        form = QVBoxLayout(group)
        form.setSpacing(10)

        size_form = QFormLayout()
        size_form.setHorizontalSpacing(12)
        size_form.setVerticalSpacing(10)

        self.cut_width_spin = QDoubleSpinBox()
        self.cut_width_spin.setDecimals(2)
        self.cut_width_spin.setRange(0.10, 1000.0)
        self.cut_width_spin.setSingleStep(0.25)
        self.cut_width_spin.setSuffix(" m")
        size_form.addRow("Default cut width:", self.cut_width_spin)

        self.cross_dock_layout_combo = QComboBox()
        self.cross_dock_layout_combo.addItem("Rows (Vertical)", "rows")
        self.cross_dock_layout_combo.addItem("Columns (Side by Side)", "columns")
        self.cross_dock_layout_combo.setToolTip(
            "Choose how multiple cross-section windows are arranged when they "
            "are attached to the right side of the application."
        )
        size_form.addRow("Multiple view layout:", self.cross_dock_layout_combo)
        form.addLayout(size_form)

        color_row = QHBoxLayout()
        color_row.addWidget(QLabel("Line color:"))
        self.cross_color_btn = QPushButton()
        self.cross_color_btn.setFixedSize(52, 30)
        self.cross_color_btn.clicked.connect(self._choose_cross_color)
        color_row.addWidget(self.cross_color_btn)
        color_row.addStretch()
        form.addLayout(color_row)

        width_row = QHBoxLayout()
        width_row.addWidget(QLabel("Line width:"))
        self.cross_width_slider = QSlider(Qt.Horizontal)
        self.cross_width_slider.setRange(1, 10)
        self.cross_width_slider.valueChanged.connect(self._on_cross_width_changed)
        width_row.addWidget(self.cross_width_slider)
        self.cross_width_label = QLabel("3 px")
        self.cross_width_label.setFixedWidth(42)
        width_row.addWidget(self.cross_width_label)
        form.addLayout(width_row)

        style_row = QHBoxLayout()
        style_row.addWidget(QLabel("Line style:"))
        self.cross_style_combo = QComboBox()
        self.cross_style_combo.addItems(["Solid", "Dashed", "Dotted", "Dash-Dot", "Dash-Dot-Dot"])
        self.cross_style_combo.currentTextChanged.connect(self._update_cross_preview)
        style_row.addWidget(self.cross_style_combo)
        style_row.addStretch()
        form.addLayout(style_row)

        form.addWidget(QLabel("Preview:"))
        self.cross_preview = SettingsPreviewWidget()
        form.addWidget(self.cross_preview)

        reset_row = QHBoxLayout()
        self.cross_reset_btn = QPushButton("Reset Cross Section")
        self.cross_reset_btn.setObjectName("secondaryBtn")
        self.cross_reset_btn.clicked.connect(self._reset_cross_section_controls)
        reset_row.addWidget(self.cross_reset_btn)
        reset_row.addStretch()
        form.addLayout(reset_row)

        self.cross_summary = QLabel()
        self.cross_summary.setObjectName("dialogCaption")
        self.cross_summary.setWordWrap(True)

        layout.addWidget(group)
        layout.addWidget(self.cross_summary)
        layout.addStretch()
        return page

    def _build_draw_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        group = QGroupBox("Draw")
        group_layout = QVBoxLayout(group)
        group_layout.setSpacing(10)

        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)

        self.draw_tool_combo = QComboBox()
        self.draw_tool_combo.addItem("All Tools (Global)", "__global__")  # always first
        for tool_key in TOOL_ORDER:
            self.draw_tool_combo.addItem(TOOL_DISPLAY_NAMES.get(tool_key, tool_key), tool_key)
        self.draw_tool_combo.currentIndexChanged.connect(self._on_draw_tool_changed)
        form.addRow("Tool:", self.draw_tool_combo)
        group_layout.addLayout(form)

        color_row = QHBoxLayout()
        color_row.addWidget(QLabel("Color:"))
        self.draw_color_btn = QPushButton()
        self.draw_color_btn.setFixedSize(52, 30)
        self.draw_color_btn.clicked.connect(self._choose_draw_color)
        color_row.addWidget(self.draw_color_btn)
        color_row.addStretch()
        group_layout.addLayout(color_row)

        width_row = QHBoxLayout()
        width_row.addWidget(QLabel("Width:"))
        self.draw_width_slider = QSlider(Qt.Horizontal)
        self.draw_width_slider.setRange(1, 10)
        self.draw_width_slider.valueChanged.connect(self._on_draw_width_changed)
        width_row.addWidget(self.draw_width_slider)
        self.draw_width_label = QLabel("2 px")
        self.draw_width_label.setFixedWidth(42)
        width_row.addWidget(self.draw_width_label)
        group_layout.addLayout(width_row)

        style_row = QHBoxLayout()
        style_row.addWidget(QLabel("Style:"))
        self.draw_style_combo = QComboBox()
        self.draw_style_combo.addItems(["Solid", "Dashed", "Dotted", "Dash-Dot", "Dash-Dot-Dot"])
        self.draw_style_combo.currentTextChanged.connect(self._update_draw_preview)
        style_row.addWidget(self.draw_style_combo)
        style_row.addStretch()
        group_layout.addLayout(style_row)

        # ── Fill group (shown only for closed-loop fillable tools) ──────────
        self.fill_group = QGroupBox("Fill")
        fill_layout = QVBoxLayout(self.fill_group)
        fill_layout.setSpacing(8)

        fill_cb_row = QHBoxLayout()
        self.draw_fill_enabled_cb = QCheckBox("Enable fill")
        self.draw_fill_enabled_cb.toggled.connect(self._on_fill_enabled_toggled)
        fill_cb_row.addWidget(self.draw_fill_enabled_cb)
        fill_cb_row.addStretch()
        fill_layout.addLayout(fill_cb_row)

        fill_color_row = QHBoxLayout()
        fill_color_row.addWidget(QLabel("Fill color:"))
        self.draw_fill_color_btn = QPushButton()
        self.draw_fill_color_btn.setFixedHeight(30)
        self.draw_fill_color_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.draw_fill_color_btn.clicked.connect(self._choose_fill_color)
        fill_color_row.addWidget(self.draw_fill_color_btn)
        fill_layout.addLayout(fill_color_row)

        fill_opacity_row = QHBoxLayout()
        fill_opacity_row.addWidget(QLabel("Opacity:"))
        self.draw_fill_opacity_slider = QSlider(Qt.Horizontal)
        self.draw_fill_opacity_slider.setRange(0, 100)
        self.draw_fill_opacity_slider.setValue(30)
        self.draw_fill_opacity_slider.valueChanged.connect(self._on_fill_opacity_changed)
        fill_opacity_row.addWidget(self.draw_fill_opacity_slider)
        self.draw_fill_opacity_label = QLabel("30%")
        self.draw_fill_opacity_label.setFixedWidth(42)
        fill_opacity_row.addWidget(self.draw_fill_opacity_label)
        fill_layout.addLayout(fill_opacity_row)

        group_layout.addWidget(self.fill_group)

        draw_buttons = QHBoxLayout()
        self.draw_reset_tool_btn = QPushButton("Reset Selected Tool")
        self.draw_reset_tool_btn.setObjectName("secondaryBtn")
        self.draw_reset_tool_btn.clicked.connect(self._reset_current_draw_tool)
        draw_buttons.addWidget(self.draw_reset_tool_btn)

        self.draw_reset_all_btn = QPushButton("Reset All Draw Styles")
        self.draw_reset_all_btn.setObjectName("secondaryBtn")
        self.draw_reset_all_btn.clicked.connect(self._reset_all_draw_tools)
        draw_buttons.addWidget(self.draw_reset_all_btn)

        draw_buttons.addStretch()
        group_layout.addLayout(draw_buttons)

        self.draw_summary = QLabel()
        self.draw_summary.setObjectName("dialogCaption")
        self.draw_summary.setWordWrap(True)

        layout.addWidget(group)
        layout.addWidget(self.draw_summary)
        layout.addStretch()
        return page

    def _build_navigation_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        group = QGroupBox("Navigation")
        form = QFormLayout(group)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)

        self.zoom_behavior_combo = QComboBox()
        self.zoom_behavior_combo.addItem("Zoom to Center", "center")
        self.zoom_behavior_combo.addItem("Zoom to Cursor", "cursor")
        self.zoom_behavior_combo.addItem("Zoom to Picked Point (MicroStation-like)", "picked_point")
        self.zoom_behavior_combo.currentIndexChanged.connect(self._update_navigation_summary)
        form.addRow("Mouse wheel zoom:", self.zoom_behavior_combo)

        note = QLabel(
            "Center keeps the current behavior. Cursor follows the live mouse position. "
            "Picked Point behaves closer to MicroStation zoom-and-recenter: click a model point first, then wheel zoom keeps bringing that point to the view center."
        )
        note.setObjectName("dialogCaption")
        note.setWordWrap(True)
        form.addRow("", note)

        # Panning button setting
        self.panning_button_combo = QComboBox()
        self.panning_button_combo.addItem("Scroll Button (Middle Click)", "scroll")
        self.panning_button_combo.addItem("Left Mouse Button", "left")
        self.panning_button_combo.addItem("Tap-Tap Pan", "tap")
        self.panning_button_combo.currentIndexChanged.connect(self._update_navigation_summary)
        form.addRow("Panning button:", self.panning_button_combo)

        note_pan = QLabel(
            "Scroll Button uses the scroll wheel press for panning. "
            "Left Mouse Button pans by left-dragging (MicroStation Pan-tool style). "
            "Tap-Tap Pan pans like MicroStation's dynamic pan: tap once to start "
            "panning, move the mouse without holding any button, and tap again to stop. "
            "Pan stays disabled while another tool owns the left click."
        )
        note_pan.setObjectName("dialogCaption")
        note_pan.setWordWrap(True)
        form.addRow("", note_pan)

        self.navigation_summary = QLabel()
        self.navigation_summary.setObjectName("dialogCaption")
        self.navigation_summary.setWordWrap(True)

        layout.addWidget(group)
        layout.addWidget(self.navigation_summary)
        layout.addStretch()
        return page

    def _on_category_changed(self, index):
        self.category_stack.setCurrentIndex(max(0, index))
        if index == 1:
            self._ensure_shortcuts_loaded()

    def _ensure_shortcuts_loaded(self):
        if self._shortcuts_needs_reload:
            self._load_shortcuts()
            self._shortcuts_needs_reload = False

    def _mark_dirty(self, category):
        if not self._loading:
            self._dirty_categories.add(category)

    def _connect_dirty_signals(self):
        self.theme_combo.currentIndexChanged.connect(lambda _: self._mark_dirty(0))
        self.canvas_theme_combo.currentIndexChanged.connect(lambda _: self._mark_dirty(0))
        self.cut_width_spin.valueChanged.connect(lambda _: self._mark_dirty(2))
        self.cross_dock_layout_combo.currentIndexChanged.connect(
            lambda _: self._mark_dirty(2)
        )
        self.cross_width_slider.valueChanged.connect(lambda _: self._mark_dirty(2))
        self.cross_style_combo.currentTextChanged.connect(lambda _: self._mark_dirty(2))
        self.draw_width_slider.valueChanged.connect(lambda _: self._mark_dirty(3))
        self.draw_style_combo.currentTextChanged.connect(lambda _: self._mark_dirty(3))
        self.draw_fill_opacity_slider.valueChanged.connect(lambda _: self._mark_dirty(3))
        self.zoom_behavior_combo.currentIndexChanged.connect(lambda _: self._mark_dirty(4))
        self.panning_button_combo.currentIndexChanged.connect(lambda _: self._mark_dirty(4))

    def _load_values(self):
        self._loading = True
        try:
            self._load_theme_settings()
            # Shortcuts loaded lazily when the tab is first clicked (_ensure_shortcuts_loaded)
            self._load_cross_section_settings()
            self._load_draw_settings_page()
            self._load_navigation_settings()
        finally:
            self._loading = False
        # If shortcuts tab is already active, load now so it isn't blank
        if self.category_list.currentRow() == 1:
            self._ensure_shortcuts_loaded()

    def _load_theme_settings(self):
        from gui.theme_manager import ThemeManager

        theme_name = ThemeManager.current() or ThemeManager.load_saved_theme()
        index = self.theme_combo.findData(theme_name)
        if index >= 0:
            self.theme_combo.setCurrentIndex(index)

        canvas_theme = ThemeManager.canvas_background_for_theme()
        canvas_index = self.canvas_theme_combo.findData(canvas_theme)
        if canvas_index >= 0:
            self.canvas_theme_combo.setCurrentIndex(canvas_index)

        self.theme_summary.setText(
            f"Current theme is {self.theme_combo.currentText().lower()}. "
            f"Canvas theme: {self.canvas_theme_combo.currentText().lower()}."
        )

    def _load_shortcuts(self):
        shortcuts_data = self.settings.value("shortcuts", None)
        self._loading_shortcuts = True
        self.shortcuts_table.setRowCount(0)

        shortcuts_list = []
        if shortcuts_data is not None:
            try:
                shortcuts_list = json.loads(shortcuts_data) if isinstance(shortcuts_data, str) else shortcuts_data
            except Exception:
                shortcuts_list = []

        for entry in shortcuts_list:
            self._add_shortcut_row(dict(entry))

        self._loading_shortcuts = False
        self._update_shortcuts_summary()

    def _load_cross_section_settings(self):
        cut_width = self.settings.value(
            "cut_section_width",
            getattr(self.app, "default_cut_width", 2.0),
            type=float,
        )
        self.cut_width_spin.setValue(cut_width)

        color_name = self.settings.value("cross_line_color", "#FF00FF", type=str)
        self._cross_color = QColor(color_name)
        self._update_color_button(self.cross_color_btn, self._cross_color)

        width = self.settings.value("cross_line_width", 3, type=int)
        self.cross_width_slider.setValue(width)

        style = self.settings.value("cross_line_style", "solid", type=str)
        self.cross_style_combo.setCurrentText(self._pretty_style_name(style))

        dock_layout = self.settings.value(
            "cross_section_dock_layout",
            getattr(self.app, "cross_section_dock_layout", "rows"),
            type=str,
        )
        layout_index = self.cross_dock_layout_combo.findData(dock_layout)
        self.cross_dock_layout_combo.setCurrentIndex(
            layout_index if layout_index >= 0 else 0
        )

        self._update_cross_preview()

    def _load_draw_settings_page(self):
        self._draw_styles = load_draw_settings()
        if self.draw_tool_combo.currentData() is None:
            self.draw_tool_combo.setCurrentIndex(0)
        self._selected_draw_tool = self.draw_tool_combo.currentData() or TOOL_ORDER[0]
        self._load_draw_editor_for_selection()
        self._update_draw_summary()

    def _load_navigation_settings(self):
        zoom_behavior = self.settings.value(
            "view_zoom_behavior",
            getattr(self.app, "zoom_behavior", "center"),
            type=str,
        )
        index = self.zoom_behavior_combo.findData(zoom_behavior)
        if index < 0:
            index = 0
        self.zoom_behavior_combo.setCurrentIndex(index)

        panning_button = self.settings.value(
            "panning_button",
            getattr(self.app, "panning_button", "scroll"),
            type=str,
        )
        index = self.panning_button_combo.findData(panning_button)
        if index < 0:
            index = 0
        self.panning_button_combo.setCurrentIndex(index)
        self._update_navigation_summary()

    def _add_empty_shortcut_row(self):
        entry = {
            "modifier": "alt",
            "key": "F1",
            "tool": "AboveLine",
            "from_classes": None,
            "to_class": None,
        }
        self._add_shortcut_row(entry)
        self.shortcuts_table.selectRow(self.shortcuts_table.rowCount() - 1)
        self._mark_dirty(1)
        self._update_shortcuts_summary()

    def _add_shortcut_row(self, entry):
        row = self.shortcuts_table.rowCount()
        self.shortcuts_table.insertRow(row)

        mod_combo = QComboBox()
        mod_combo.addItems(SHORTCUT_MODIFIERS)
        mod_combo.setCurrentText(entry.get("modifier", "alt"))
        mod_combo.currentTextChanged.connect(self._on_shortcut_editor_changed)
        self.shortcuts_table.setCellWidget(row, 0, mod_combo)

        key_combo = QComboBox()
        key_combo.addItems(SHORTCUT_KEYS)
        key_combo.setCurrentText(entry.get("key", "F1"))
        key_combo.currentTextChanged.connect(self._on_shortcut_editor_changed)
        self.shortcuts_table.setCellWidget(row, 1, key_combo)

        tool_combo = QComboBox()
        tool_combo.addItems(TOOLS)
        tool_combo.setCurrentText(entry.get("tool", "AboveLine"))
        tool_combo.currentTextChanged.connect(self._on_shortcut_editor_changed)
        self.shortcuts_table.setCellWidget(row, 2, tool_combo)

        details_item = QTableWidgetItem(self._shortcut_details_text(entry))
        details_item.setFlags(details_item.flags() & ~Qt.ItemIsEditable)
        details_item.setData(Qt.UserRole, dict(entry))
        self.shortcuts_table.setItem(row, 3, details_item)

    def _remove_selected_shortcut_row(self):
        row = self.shortcuts_table.currentRow()
        if row >= 0:
            self.shortcuts_table.removeRow(row)
            self._mark_dirty(1)
            self._update_shortcuts_summary()

    def _find_shortcut_row_for_widget(self, widget):
        for row in range(self.shortcuts_table.rowCount()):
            for col in range(3):
                if self.shortcuts_table.cellWidget(row, col) is widget:
                    return row
        return -1

    def _on_shortcut_editor_changed(self):
        if self._loading_shortcuts:
            return

        widget = self.sender()
        row = self._find_shortcut_row_for_widget(widget)
        if row < 0:
            return

        details_item = self.shortcuts_table.item(row, 3)
        if details_item is None:
            details_item = QTableWidgetItem()
            details_item.setFlags(details_item.flags() & ~Qt.ItemIsEditable)
            self.shortcuts_table.setItem(row, 3, details_item)

        entry = dict(details_item.data(Qt.UserRole) or {})
        tool = self.shortcuts_table.cellWidget(row, 2).currentText()
        entry = self._sanitize_shortcut_entry(entry, tool)
        entry["modifier"] = self.shortcuts_table.cellWidget(row, 0).currentText()
        entry["key"] = self.shortcuts_table.cellWidget(row, 1).currentText()
        entry["tool"] = tool
        details_item.setData(Qt.UserRole, entry)
        details_item.setText(self._shortcut_details_text(entry))
        self._mark_dirty(1)
        self._update_shortcuts_summary()

    def _shortcut_details_text(self, entry):
        tool = entry.get("tool", "")
        if tool == "DisplayMode":
            return entry.get("display_text") or "Display preset"
        if tool == "ShadingMode":
            return entry.get("shading_text") or "Shading preset"
        if tool == "DrawSettings":
            return entry.get("draw_text") or "Draw preset"

        from_classes = entry.get("from_classes")
        to_class = entry.get("to_class")
        if from_classes not in (None, "", []) or to_class not in (None, ""):
            return f"From: {self._format_class_value(from_classes)} → To: {self._format_class_value(to_class)}"

        return "Basic shortcut"

    def _format_class_value(self, value):
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        return str(value)

    def _sanitize_shortcut_entry(self, entry, tool):
        entry = dict(entry)

        if tool != "DisplayMode":
            entry.pop("display_preset", None)
            entry.pop("display_text", None)
        if tool != "ShadingMode":
            entry.pop("shading_preset", None)
            entry.pop("shading_text", None)
        if tool != "DrawSettings":
            entry.pop("draw_preset", None)
            entry.pop("draw_text", None)

        if tool in SHORTCUT_SIMPLE_TOOLS:
            entry["from_classes"] = None
            entry["to_class"] = None
        else:
            entry.setdefault("from_classes", None)
            entry.setdefault("to_class", None)

        return entry

    def _update_shortcuts_summary(self):
        count = self.shortcuts_table.rowCount()
        self.shortcuts_summary.setText(
            f"{count} shortcut row(s). "
            "Binding fields are editable here and saved directly in application settings. "
            "Existing advanced shortcut payloads are preserved."
        )

    def _filter_shortcuts(self, search_text):
        """Filter shortcuts table based on search text."""
        search_text = search_text.lower().strip()
        
        for row in range(self.shortcuts_table.rowCount()):
            # Get cell widgets and items
            mod_combo = self.shortcuts_table.cellWidget(row, 0)
            key_combo = self.shortcuts_table.cellWidget(row, 1)
            tool_combo = self.shortcuts_table.cellWidget(row, 2)
            details_item = self.shortcuts_table.item(row, 3)
            
            # Build searchable text from all columns
            searchable_parts = []
            
            if mod_combo:
                searchable_parts.append(mod_combo.currentText().lower())
            if key_combo:
                searchable_parts.append(key_combo.currentText().lower())
            if tool_combo:
                searchable_parts.append(tool_combo.currentText().lower())
            if details_item:
                searchable_parts.append(details_item.text().lower())
            
            searchable_text = " ".join(searchable_parts)
            
            # Show/hide row based on search match
            if search_text == "" or search_text in searchable_text:
                self.shortcuts_table.setRowHidden(row, False)
            else:
                self.shortcuts_table.setRowHidden(row, True)
        
        # Update summary with visible count
        visible_count = sum(
            1 for row in range(self.shortcuts_table.rowCount())
            if not self.shortcuts_table.isRowHidden(row)
        )
        total_count = self.shortcuts_table.rowCount()
        
        if search_text:
            self.shortcuts_summary.setText(
                f"Showing {visible_count} of {total_count} shortcut(s). "
                "Binding fields are editable here and saved directly in application settings."
            )
        else:
            self.shortcuts_summary.setText(
                f"{total_count} shortcut row(s). "
                "Binding fields are editable here and saved directly in application settings. "
                "Existing advanced shortcut payloads are preserved."
            )

    def _toggle_capture_mode(self, enabled):
        """Toggle between typing mode and keyboard capture mode."""
        self.shortcuts_search.capture_mode = enabled
        
        if enabled:
            self.shortcuts_search.setPlaceholderText("Press a keyboard shortcut (e.g., Shift+F1)...")
            self.shortcuts_search.setStyleSheet(
                "QLineEdit { background-color: #2d4a2e; border: 2px solid #4a7c4e; }"
            )
            self.shortcuts_search.clear()
            self.shortcuts_search.setFocus()
        else:
            self.shortcuts_search.setPlaceholderText("Type to search: modifier, key, tool, or details...")
            self.shortcuts_search.setStyleSheet("")

    def _choose_cross_color(self):
        color = QColorDialog.getColor(self._cross_color, self, "Choose Cross-Section Color")
        if color.isValid():
            self._cross_color = color
            self._update_color_button(self.cross_color_btn, color)
            self._mark_dirty(2)
            self._update_cross_preview()

    def _on_cross_width_changed(self, value):
        self.cross_width_label.setText(f"{value} px")
        self._update_cross_preview()

    def _reset_cross_section_controls(self):
        self.cut_width_spin.setValue(2.0)
        self._cross_color = QColor("#FF00FF")
        self._update_color_button(self.cross_color_btn, self._cross_color)
        self.cross_width_slider.setValue(3)
        self.cross_style_combo.setCurrentText("Solid")
        self.cross_dock_layout_combo.setCurrentIndex(0)
        self._update_cross_preview()

    def _update_cross_preview(self):
        style = self._style_key_from_text(self.cross_style_combo.currentText())
        self.cross_preview.set_state(self._cross_color, self.cross_width_slider.value(), style)
        dock_layout = self.cross_dock_layout_combo.currentText().lower()
        self.cross_summary.setText(
            f"Cut width: +/- {self.cut_width_spin.value():.2f} m | "
            f"Line: {self._cross_color.name()}, {self.cross_width_slider.value()} px, {style} | "
            f"Attached views: {dock_layout}"
        )

    def _on_draw_tool_changed(self):
        self._commit_draw_editor()
        self._selected_draw_tool = self.draw_tool_combo.currentData() or TOOL_ORDER[0]
        self._loading = True
        self._load_draw_editor_for_selection()
        self._loading = False
        self._update_draw_summary()

    def _load_draw_editor_for_selection(self):
        key = self._selected_draw_tool
        self._fill_color_manually_set = False
        if key == "__global__":
            style = dict(self._draw_styles.get(TOOL_ORDER[0], DEFAULT_DRAW_STYLES[TOOL_ORDER[0]]))
            # TOOL_ORDER[0] is non-fillable; pull fill data from first fillable tool
            first_fillable = next((t for t in TOOL_ORDER if t in FILLABLE_TOOLS), None)
            if first_fillable:
                fill_src = self._draw_styles.get(first_fillable, DEFAULT_DRAW_STYLES.get(first_fillable, {}))
                style["fill_enabled"] = fill_src.get("fill_enabled", False)
                style["fill_color"] = fill_src.get("fill_color", style.get("color", (1.0, 0.0, 0.0)))
                style["fill_opacity"] = fill_src.get("fill_opacity", 0.3)
        else:
            style = self._draw_styles.get(key, DEFAULT_DRAW_STYLES.get(key, DEFAULT_DRAW_STYLES[TOOL_ORDER[0]]))

        color = vtk_color_to_qcolor(style["color"])
        self._draw_color = color
        self._update_color_button(self.draw_color_btn, color)
        self.draw_width_slider.setValue(int(style["width"]))
        self.draw_width_label.setText(f"{int(style['width'])} px")
        self.draw_style_combo.setCurrentText(self._pretty_style_name(style["style"]))

        # Fill controls
        is_fillable = (key == "__global__" or key in FILLABLE_TOOLS)
        self.fill_group.setVisible(is_fillable)
        if is_fillable:
            fill_enabled = style.get("fill_enabled", False)
            self._draw_fill_color = vtk_color_to_qcolor(style.get("fill_color", style["color"]))
            fill_pct = int(style.get("fill_opacity", 0.3) * 100)
            self.draw_fill_enabled_cb.setChecked(fill_enabled)
            self.draw_fill_opacity_slider.setValue(fill_pct)
            self.draw_fill_opacity_label.setText(f"{fill_pct}%")
            self.draw_fill_color_btn.setEnabled(fill_enabled)
            self.draw_fill_opacity_slider.setEnabled(fill_enabled)
            self.draw_fill_opacity_label.setEnabled(fill_enabled)
            self._update_fill_preview_button()

        self._update_draw_summary()

    def _commit_draw_editor(self):
        if not self._draw_styles:
            return

        style = {
            "color": qcolor_to_vtk(getattr(self, "_draw_color", QColor("#ff0000"))),
            "width": int(self.draw_width_slider.value()),
            "style": self._style_key_from_text(self.draw_style_combo.currentText()),
        }

        if self._selected_draw_tool == "__global__":
            for tool_key in TOOL_ORDER:
                merged = dict(self._draw_styles.get(tool_key, style))
                merged.update(style)
                if tool_key in FILLABLE_TOOLS:
                    merged["fill_enabled"] = self.draw_fill_enabled_cb.isChecked()
                    merged["fill_color"] = qcolor_to_vtk(getattr(self, "_draw_fill_color", QColor("#ff0000")))
                    merged["fill_opacity"] = self.draw_fill_opacity_slider.value() / 100.0
                self._draw_styles[tool_key] = merged
        elif self._selected_draw_tool in TOOL_ORDER:
            merged = dict(self._draw_styles.get(self._selected_draw_tool, style))
            merged.update(style)
            if self._selected_draw_tool in FILLABLE_TOOLS:
                merged["fill_enabled"] = self.draw_fill_enabled_cb.isChecked()
                merged["fill_color"] = qcolor_to_vtk(getattr(self, "_draw_fill_color", QColor("#ff0000")))
                merged["fill_opacity"] = self.draw_fill_opacity_slider.value() / 100.0
            self._draw_styles[self._selected_draw_tool] = merged

    def _choose_draw_color(self):
        color = QColorDialog.getColor(getattr(self, "_draw_color", QColor("#ff0000")), self, "Choose Draw Color")
        if color.isValid():
            self._draw_color = color
            self._update_color_button(self.draw_color_btn, color)
            if not getattr(self, "_fill_color_manually_set", False):
                self._draw_fill_color = color
                self._update_fill_preview_button()
            self._mark_dirty(3)
            self._update_draw_summary()

    def _choose_fill_color(self):
        color = QColorDialog.getColor(getattr(self, "_draw_fill_color", QColor("#ff0000")), self, "Choose Fill Color")
        if color.isValid():
            self._draw_fill_color = color
            self._fill_color_manually_set = True
            self._update_fill_preview_button()
            self._mark_dirty(3)

    def _on_fill_enabled_toggled(self, checked):
        self.draw_fill_color_btn.setEnabled(checked)
        self.draw_fill_opacity_slider.setEnabled(checked)
        self.draw_fill_opacity_label.setEnabled(checked)
        self._mark_dirty(3)
        self._update_fill_preview_button()

    def _on_fill_opacity_changed(self, value):
        self.draw_fill_opacity_label.setText(f"{value}%")
        self._mark_dirty(3)
        self._update_fill_preview_button()

    def _on_draw_width_changed(self, value):
        self.draw_width_label.setText(f"{value} px")
        self._update_draw_preview()

    def _update_draw_preview(self):
        self._update_fill_preview_button()
        self._update_draw_summary()

    def _update_fill_preview_button(self):
        if not hasattr(self, "draw_fill_color_btn"):
            return
        color = getattr(self, "_draw_fill_color", QColor("#00aaff"))
        opacity = self.draw_fill_opacity_slider.value() / 100.0 if hasattr(self, "draw_fill_opacity_slider") else 0.3
        enabled = self.draw_fill_enabled_cb.isChecked() if hasattr(self, "draw_fill_enabled_cb") else False
        r, g, b = color.red(), color.green(), color.blue()
        alpha = int(opacity * 255) if enabled else 60
        self.draw_fill_color_btn.setStyleSheet(
            f"background-color: rgba({r},{g},{b},{alpha}); border: 1px solid #666; border-radius: 3px;"
        )

    def _update_draw_summary(self):
        key = self.draw_tool_combo.currentData() or TOOL_ORDER[0]
        if key == "__global__":
            self.draw_summary.setText("Global draw editing will apply the same style to every draw tool.")
        else:
            self.draw_summary.setText(
                f"Editing {TOOL_DISPLAY_NAMES.get(key, key)}. "
                "Save All will push these draw styles into persistent settings and the active digitizer."
            )

    def _update_navigation_summary(self):
        mode = self.zoom_behavior_combo.currentData() or "center"
        if mode == "cursor":
            zoom_text = "Mouse wheel zoom will stay anchored under the cursor in supported views."
        elif mode == "picked_point":
            zoom_text = "Click a point to set the zoom target, then mouse wheel zoom will recenter and zoom around that picked point."
        else:
            zoom_text = "Mouse wheel zoom will continue using the viewport center."

        panning = self.panning_button_combo.currentData() or "scroll"
        if panning == "left":
            panning_text = "Left-drag pans in the main view (when no tool owns left click); middle-click drag also pans."
        elif panning == "tap":
            panning_text = "Tap once to start panning, move to pan without holding, tap again to stop (MicroStation dynamic pan)."
        else:
            panning_text = "Scroll Button (Middle Click) is used for panning."

        self.navigation_summary.setText(f"{zoom_text}\n{panning_text}")

    def _reset_current_draw_tool(self):
        key = self.draw_tool_combo.currentData() or TOOL_ORDER[0]
        if key == "__global__":
            first_default = DEFAULT_DRAW_STYLES[TOOL_ORDER[0]]
            self._draw_color = vtk_color_to_qcolor(first_default["color"])
            self._update_color_button(self.draw_color_btn, self._draw_color)
            self.draw_width_slider.setValue(first_default["width"])
            self.draw_style_combo.setCurrentText(self._pretty_style_name(first_default["style"]))
            self._fill_color_manually_set = False
            self._draw_fill_color = self._draw_color
            self._update_fill_preview_button()
            self._update_draw_summary()
            return

        default = DEFAULT_DRAW_STYLES.get(key, DEFAULT_DRAW_STYLES[TOOL_ORDER[0]])
        self._draw_styles[key] = dict(default)
        self._load_draw_editor_for_selection()
        self._update_draw_summary()

    def _reset_all_draw_tools(self):
        self._draw_styles = {key: dict(value) for key, value in DEFAULT_DRAW_STYLES.items()}
        self._load_draw_editor_for_selection()
        self._update_draw_summary()

    def _save_theme_settings(self):
        from gui.theme_manager import ThemeManager

        theme_name = self.theme_combo.currentData()
        self.settings.setValue("ui_canvas_background", self.canvas_theme_combo.currentData())
        self.settings.sync()
        ThemeManager.apply_theme(self.app, theme_name)
        self.refresh_theme()
        if hasattr(self.app, "_update_theme_icon"):
            self.app._update_theme_icon()
        if hasattr(self.app, "_update_settings_icon"):
            self.app._update_settings_icon()

    def _save_shortcuts_settings(self):
        shortcuts_list = []
        seen = set()

        for row in range(self.shortcuts_table.rowCount()):
            modifier = self.shortcuts_table.cellWidget(row, 0).currentText()
            key = self.shortcuts_table.cellWidget(row, 1).currentText()
            tool = self.shortcuts_table.cellWidget(row, 2).currentText()
            combo_key = (modifier.lower(), key.upper())

            if combo_key in seen:
                self.category_list.setCurrentRow(1)
                raise ValueError(f"Duplicate shortcut detected for {modifier}+{key}.")
            seen.add(combo_key)

            details_item = self.shortcuts_table.item(row, 3)
            entry = dict(details_item.data(Qt.UserRole) or {})
            entry = self._sanitize_shortcut_entry(entry, tool)
            entry["modifier"] = modifier
            entry["key"] = key
            entry["tool"] = tool
            shortcuts_list.append(entry)

        self.settings.setValue("shortcuts", json.dumps(shortcuts_list))
        self.settings.sync()
        ShortcutManager.apply_shortcuts_from_settings(self.app)

    def _save_cross_section_settings(self):
        style = self._style_key_from_text(self.cross_style_combo.currentText())
        cut_width = self.cut_width_spin.value()

        self.app.default_cut_width = cut_width
        self.settings.setValue("cut_section_width", cut_width)

        self.app.cross_line_color = (
            self._cross_color.redF(),
            self._cross_color.greenF(),
            self._cross_color.blueF(),
        )
        self.app.cross_line_width = self.cross_width_slider.value()
        self.app.cross_line_style = style
        self.app.cross_section_dock_layout = (
            self.cross_dock_layout_combo.currentData() or "rows"
        )

        self.settings.setValue("cross_line_color", self._cross_color.name())
        self.settings.setValue("cross_line_width", self.cross_width_slider.value())
        self.settings.setValue("cross_line_style", style)
        self.settings.setValue(
            "cross_section_dock_layout", self.app.cross_section_dock_layout
        )

        arrange_docks = getattr(self.app, "_arrange_cross_section_docks", None)
        if callable(arrange_docks):
            arrange_docks()

        if hasattr(self.app, "section_controller"):
            sc = self.app.section_controller
            if hasattr(sc, "rubber_actor") and sc.rubber_actor:
                try:
                    prop = sc.rubber_actor.GetProperty()
                    prop.SetColor(*self.app.cross_line_color)
                    prop.SetLineWidth(self.app.cross_line_width)
                    if hasattr(sc, "update_rectangle_style"):
                        sc.update_rectangle_style()
                    self.app.vtk_widget.render()
                except Exception:
                    pass

    def _save_draw_settings(self):
        self._commit_draw_editor()
        save_draw_settings(self._draw_styles)

        digitizer = getattr(self.app, "digitizer", None)
        if digitizer and hasattr(digitizer, "draw_tool_styles"):
            digitizer.draw_tool_styles = {k: dict(v) for k, v in self._draw_styles.items()}

    def _save_navigation_settings(self):
        zoom_behavior = self.zoom_behavior_combo.currentData() or "center"
        self.settings.setValue("view_zoom_behavior", zoom_behavior)
        self.app.zoom_behavior = zoom_behavior

        panning_button = self.panning_button_combo.currentData() or "scroll"
        self.settings.setValue("panning_button", panning_button)
        self.app.panning_button = panning_button

    def save_all_settings(self):
        try:
            if not self._dirty_categories:
                if hasattr(self.app, "statusBar") and self.app.statusBar():
                    self.app.statusBar().showMessage("No changes to save.", 2500)
                return

            if 0 in self._dirty_categories:
                self._save_theme_settings()
            if 2 in self._dirty_categories:
                self._save_cross_section_settings()
            if 3 in self._dirty_categories:
                self._save_draw_settings()
            if 4 in self._dirty_categories:
                self._save_navigation_settings()
            if 1 in self._dirty_categories:
                self._save_shortcuts_settings()

            self.settings.sync()
            self._dirty_categories.clear()

            self._update_shortcuts_summary()
            self._update_cross_preview()
            self._update_draw_summary()
            self._update_navigation_summary()
            self._load_theme_settings()

            if hasattr(self.app, "statusBar") and self.app.statusBar():
                self.app.statusBar().showMessage("Settings saved successfully.", 2500)
        except Exception as exc:
            QMessageBox.warning(self, "Save Failed", str(exc))

    def refresh_summaries(self):
        """Compatibility hook used by the main window before showing the dialog."""
        self._load_values()

    def _update_color_button(self, button, color):
        button.setStyleSheet(
            f"background-color: {color.name()}; border: 1px solid rgba(0, 0, 0, 0.18); border-radius: 4px;"
        )

    def _pretty_style_name(self, style):
        return {
            "solid": "Solid",
            "dashed": "Dashed",
            "dotted": "Dotted",
            "dash-dot": "Dash-Dot",
            "dash-dot-dot": "Dash-Dot-Dot",
        }.get(style, "Solid")

    def _style_key_from_text(self, text):
        return text.strip().lower()
