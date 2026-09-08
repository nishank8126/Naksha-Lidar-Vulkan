# gui/measure_settings_dialog.py
# Measurement Tool Settings Dialog
# Three-section layout: Line · Path · Block  — styled to match BlockCreationSettingsDialog.

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QPushButton, QLabel, QSpinBox, QColorDialog,
    QGroupBox, QComboBox, QWidget, QMessageBox, QScrollArea,
)
from PySide6.QtCore import Qt, QSettings, QTimer
from PySide6.QtGui import QColor


# ──────────────────────────────────────────────────────────────────
#  Defaults
# ──────────────────────────────────────────────────────────────────

DEFAULT_MEASURE_STYLE = {
    'line': {
        'color': (1.0, 1.0, 0.0),   # Yellow (VTK 0-1 float)
        'width': 4,
        'label_font_size': 16,
        'unit': 'm',
    },
    'path': {
        'color': (1.0, 0.65, 0.0),  # Orange
        'width': 4,
        'label_font_size': 16,
        'unit': 'm',
    },
    'cross_section': {
        'color': (1.0, 0.55, 0.0),
        'width': 3,
        'line_style': 'solid',
        'label_font_size': 16,
        'unit': 'm',
    },
    'block': {
        'unit': 'm',
        'label_font_size': 24,
    },
    'grid': {
        'unit': 'm',
        'label_font_size': 24,
    },
}

_UNIT_OPTIONS = [
    ("Meters (m)",       "m"),
    ("Kilometers (km)",  "km"),
]

_LINE_STYLE_OPTIONS = [
    ("Solid", "solid"),
    ("Dashed", "dashed"),
    ("Dotted", "dotted"),
]


# ──────────────────────────────────────────────────────────────────
#  Colour helpers
# ──────────────────────────────────────────────────────────────────

def _vtk_to_qcolor(vtk_color):
    """Convert VTK float (0-1) RGB tuple → QColor."""
    return QColor(
        max(0, min(255, int(vtk_color[0] * 255))),
        max(0, min(255, int(vtk_color[1] * 255))),
        max(0, min(255, int(vtk_color[2] * 255))),
    )


def _qcolor_to_vtk(qcolor):
    """Convert QColor → VTK float (0-1) RGB tuple."""
    return (qcolor.redF(), qcolor.greenF(), qcolor.blueF())


# ──────────────────────────────────────────────────────────────────
#  Persistence
# ──────────────────────────────────────────────────────────────────

def load_measure_settings():
    """Load persisted settings from QSettings, filling missing keys with defaults."""
    s = QSettings("NakshaAI", "LidarApp")
    out = {}

    for section in ('line', 'path', 'cross_section'):
        dflt = DEFAULT_MEASURE_STYLE[section]
        color_name = s.value(f"measure_style/{section}/color", None)
        if color_name:
            qc = QColor(color_name)
            color = (qc.redF(), qc.greenF(), qc.blueF())
        else:
            color = dflt['color']
        out[section] = {
            'color':           color,
            'width':           int(s.value(f"measure_style/{section}/width",           dflt['width'])),
            'line_style':      s.value(f"measure_style/{section}/line_style",          dflt.get('line_style', 'solid')),
            'label_font_size': int(s.value(f"measure_style/{section}/label_font_size", dflt['label_font_size'])),
            'unit':            s.value(f"measure_style/{section}/unit",                dflt['unit']),
        }

    dflt_block = DEFAULT_MEASURE_STYLE['block']
    out['block'] = {
        'unit': s.value("measure_style/block/unit", dflt_block['unit']),
        'label_font_size': int(s.value("measure_style/block/label_font_size", dflt_block['label_font_size'])),
    }

    dflt_grid = DEFAULT_MEASURE_STYLE['grid']
    out['grid'] = {
        'unit': s.value("measure_style/grid/unit", dflt_grid['unit']),
        'label_font_size': int(s.value("measure_style/grid/label_font_size", dflt_grid['label_font_size'])),
    }

    return out


def save_measure_settings(style):
    """Persist style dict to QSettings."""
    s = QSettings("NakshaAI", "LidarApp")

    for section in ('line', 'path', 'cross_section'):
        sec = style.get(section, {})
        if 'color' in sec:
            s.setValue(f"measure_style/{section}/color", _vtk_to_qcolor(sec['color']).name())
        if 'width' in sec:
            s.setValue(f"measure_style/{section}/width", int(sec['width']))
        if 'line_style' in sec:
            s.setValue(f"measure_style/{section}/line_style", sec['line_style'])
        if 'label_font_size' in sec:
            s.setValue(f"measure_style/{section}/label_font_size", int(sec['label_font_size']))
        if 'unit' in sec:
            s.setValue(f"measure_style/{section}/unit", sec['unit'])

    block = style.get('block', {})
    if 'unit' in block:
        s.setValue("measure_style/block/unit", block['unit'])
    if 'label_font_size' in block:
        s.setValue("measure_style/block/label_font_size", int(block['label_font_size']))

    grid = style.get('grid', {})
    if 'unit' in grid:
        s.setValue("measure_style/grid/unit", grid['unit'])
    if 'label_font_size' in grid:
        s.setValue("measure_style/grid/label_font_size", int(grid['label_font_size']))


# ──────────────────────────────────────────────────────────────────
#  Stylesheet (mirrors _get_block_dialog_style)
# ──────────────────────────────────────────────────────────────────

def _get_measure_dialog_style():
    from gui.theme_manager import ThemeColors as _TC

    return f"""
QDialog {{
    background-color: {_TC.get('bg_secondary')};
    color: {_TC.get('text_primary')};
    border: 1px solid {_TC.get('border_light')};
    border-radius: 12px;
}}
QGroupBox {{
    background-color: {_TC.get('bg_primary')};
    border: 1px solid {_TC.get('border_light')};
    border-radius: 10px;
    margin-top: 15px;
    padding-top: 18px;
    padding-bottom: 10px;
    font-weight: bold;
    color: {_TC.get('text_primary')};
    font-size: 13px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: {_TC.get('accent')};
}}
QLabel {{
    color: {_TC.get('text_primary')};
    font-size: 13px;
    font-weight: 500;
}}
QComboBox, QSpinBox {{
    background-color: {_TC.get('bg_input')};
    color: {_TC.get('text_primary')};
    border: 1px solid {_TC.get('border_light')};
    border-radius: 6px;
    padding: 6px 10px;
    min-height: 28px;
    font-size: 13px;
}}
QComboBox:hover, QSpinBox:hover {{
    border: 1px solid {_TC.get('accent')};
    background-color: {_TC.get('bg_button_hover')};
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QPushButton {{
    background-color: {_TC.get('bg_button')};
    color: {_TC.get('text_primary')};
    border: 1px solid {_TC.get('border_light')};
    border-radius: 6px;
    padding: 8px 16px;
    font-weight: bold;
    font-size: 13px;
    min-height: 28px;
}}
QPushButton:hover {{
    background-color: {_TC.get('bg_button_hover')};
    border-color: {_TC.get('accent')};
}}
QPushButton:pressed {{
    background-color: {_TC.get('accent')};
    color: {_TC.get('text_on_active')};
}}
QPushButton#applyButton {{
    background-color: {_TC.get('accent')};
    border-color: {_TC.get('accent')};
    color: {_TC.get('text_on_active')};
}}
QPushButton#applyButton:hover {{
    background-color: {_TC.get('accent_hover')};
}}
"""


# ──────────────────────────────────────────────────────────────────
#  Section widget (Line / Path)
# ──────────────────────────────────────────────────────────────────

class _MeasureSectionWidget(QGroupBox):
    """
    A single titled group-box section containing:
        Line Color  |  color swatch button
        Line Width  |  QSpinBox  (1-10 px)
        Label Size  |  QSpinBox  (8-32 pt)
        Unit        |  QComboBox (m / km)
    """

    def __init__(self, title: str, section_key: str, initial: dict, parent=None, *, allow_line_style=False):
        super().__init__(title, parent)
        self._key = section_key
        self._allow_line_style = allow_line_style
        self._color = _vtk_to_qcolor(initial['color'])
        self._build(initial)

    # ------------------------------------------------------------------
    def _build(self, initial: dict):
        form = QFormLayout(self)
        form.setLabelAlignment(Qt.AlignRight)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        form.setContentsMargins(15, 10, 15, 12)

        # ── Line Color ───────────────────────────────────────────────
        self._color_btn = QPushButton()
        self._color_btn.setFixedSize(56, 30)
        self._color_btn.setCursor(Qt.PointingHandCursor)
        self._color_btn.setToolTip("Click to pick a line colour")
        # Override the global QPushButton stylesheet for the swatch
        self._color_btn.setObjectName("colorSwatch")
        self._refresh_color_btn()
        self._color_btn.clicked.connect(self._choose_color)
        form.addRow("Line Color:", self._color_btn)

        # ── Line Width ───────────────────────────────────────────────
        self._width_spin = QSpinBox()
        self._width_spin.setRange(1, 10)
        self._width_spin.setValue(int(initial['width']))
        self._width_spin.setSuffix(" px")
        self._width_spin.setToolTip("Measurement line thickness in pixels")
        form.addRow("Line Width:", self._width_spin)

        if self._allow_line_style:
            self._line_style_combo = QComboBox()
            for label, value in _LINE_STYLE_OPTIONS:
                self._line_style_combo.addItem(label, value)
            selected_style = initial.get('line_style', 'solid')
            self._line_style_combo.setCurrentIndex(
                next((i for i, (_, value) in enumerate(_LINE_STYLE_OPTIONS) if value == selected_style), 0)
            )
            form.addRow("Line Design:", self._line_style_combo)

        # ── Label Font Size ──────────────────────────────────────────
        self._font_spin = QSpinBox()
        self._font_spin.setRange(8, 32)
        self._font_spin.setValue(int(initial['label_font_size']))
        self._font_spin.setSuffix(" pt")
        self._font_spin.setToolTip("Distance label font size in points")
        form.addRow("Label Size:", self._font_spin)

        # ── Measurement Unit ─────────────────────────────────────────
        self._unit_combo = QComboBox()
        for label, value in _UNIT_OPTIONS:
            self._unit_combo.addItem(label, value)
        # Select current unit
        for i, (_, v) in enumerate(_UNIT_OPTIONS):
            if v == initial.get('unit', 'm'):
                self._unit_combo.setCurrentIndex(i)
                break
        self._unit_combo.setToolTip("Unit used when displaying distances")
        form.addRow("Unit:", self._unit_combo)

    # ------------------------------------------------------------------
    def _choose_color(self):
        color = QColorDialog.getColor(self._color, self.window(), "Choose Line Colour")
        if color.isValid():
            self._color = color
            self._refresh_color_btn()

    def _refresh_color_btn(self):
        # Inline style so it overrides the global QPushButton rule
        self._color_btn.setStyleSheet(
            f"QPushButton#colorSwatch {{"
            f"  background-color: {self._color.name()};"
            f"  border: 2px solid rgba(255,255,255,0.6);"
            f"  border-radius: 5px;"
            f"}}"
        )

    # ------------------------------------------------------------------
    def get_style(self) -> dict:
        """Return the current control values as a style dict."""
        style = {
            'color':           _qcolor_to_vtk(self._color),
            'width':           self._width_spin.value(),
            'label_font_size': self._font_spin.value(),
            'unit':            self._unit_combo.currentData(),
        }
        if self._allow_line_style:
            style['line_style'] = self._line_style_combo.currentData()
        return style

    def reset_to_defaults(self):
        """Restore controls to DEFAULT_MEASURE_STYLE for this section."""
        dflt = DEFAULT_MEASURE_STYLE[self._key]
        self._color = _vtk_to_qcolor(dflt['color'])
        self._refresh_color_btn()
        self._width_spin.setValue(dflt['width'])
        if self._allow_line_style:
            selected_style = dflt.get('line_style', 'solid')
            self._line_style_combo.setCurrentIndex(
                next((i for i, (_, value) in enumerate(_LINE_STYLE_OPTIONS) if value == selected_style), 0)
            )
        self._font_spin.setValue(dflt['label_font_size'])
        for i, (_, v) in enumerate(_UNIT_OPTIONS):
            if v == dflt.get('unit', 'm'):
                self._unit_combo.setCurrentIndex(i)
                break


# ──────────────────────────────────────────────────────────────────
#  Block section (unit only)
# ──────────────────────────────────────────────────────────────────

class _BlockSectionWidget(QGroupBox):
    """Minimal section for Block: only Measurement Unit."""

    def __init__(self, initial: dict, parent=None, *, title="Block", section_key="block"):
        super().__init__(title, parent)
        self._section_key = section_key
        self._build(initial)

    def _build(self, initial: dict):
        form = QFormLayout(self)
        form.setLabelAlignment(Qt.AlignRight)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        form.setContentsMargins(15, 10, 15, 12)

        self._unit_combo = QComboBox()
        for label, value in _UNIT_OPTIONS:
            self._unit_combo.addItem(label, value)
        for i, (_, v) in enumerate(_UNIT_OPTIONS):
            if v == initial.get('unit', 'm'):
                self._unit_combo.setCurrentIndex(i)
                break
        self._unit_combo.setToolTip("Unit used when displaying measured areas")
        form.addRow("Unit:", self._unit_combo)

        # ── Label Font Size ──────────────────────────────────────────
        self._font_spin = QSpinBox()
        self._font_spin.setRange(8, 64)
        self._font_spin.setValue(int(initial.get('label_font_size', 24)))
        self._font_spin.setSuffix(" pt")
        self._font_spin.setToolTip("Area label font size in points")
        form.addRow("Label Size:", self._font_spin)

    def get_style(self) -> dict:
        return {
            'unit': self._unit_combo.currentData(),
            'label_font_size': self._font_spin.value()
        }

    def reset_to_defaults(self):
        dflt = DEFAULT_MEASURE_STYLE[self._section_key]
        for i, (_, v) in enumerate(_UNIT_OPTIONS):
            if v == dflt.get('unit', 'm'):
                self._unit_combo.setCurrentIndex(i)
                break
        self._font_spin.setValue(dflt.get('label_font_size', 24))


# ──────────────────────────────────────────────────────────────────
#  Main dialog
# ──────────────────────────────────────────────────────────────────

class MeasureSettingsDialog(QDialog):
    """
    Measurement Tool Settings — three sections: Line, Path, Block.
    Styled to match BlockCreationSettingsDialog.
    """

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.setWindowTitle("Measurement Settings")
        self.setModal(False)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setMinimumWidth(400)

        loaded = load_measure_settings()

        self.setStyleSheet(_get_measure_dialog_style())

        self._build_ui(loaded)

    # ------------------------------------------------------------------
    def _build_ui(self, loaded: dict):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 12, 18, 12)
        root.setSpacing(10)

        # Keep all setting groups reachable on short screens while leaving
        # the action buttons visible at the bottom of the dialog.
        scroll_area = QScrollArea(self)
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll_content = QWidget(scroll_area)
        settings_layout = QVBoxLayout(scroll_content)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setSpacing(10)
        scroll_area.setWidget(scroll_content)
        root.addWidget(scroll_area, 1)

        # ── Line section ─────────────────────────────────────────────
        self._line_sec = _MeasureSectionWidget(
            "Line", "line", loaded['line'], parent=scroll_content
        )
        settings_layout.addWidget(self._line_sec)

        # ── Path section ─────────────────────────────────────────────
        self._path_sec = _MeasureSectionWidget(
            "Path", "path", loaded['path'], parent=scroll_content
        )
        settings_layout.addWidget(self._path_sec)

        self._cross_section_sec = _MeasureSectionWidget(
            "Cross Section", "cross_section", loaded['cross_section'], parent=scroll_content
        )
        settings_layout.addWidget(self._cross_section_sec)

        # ── Block section ────────────────────────────────────────────
        self._block_sec = _BlockSectionWidget(loaded['block'], parent=scroll_content)
        settings_layout.addWidget(self._block_sec)

        self._grid_sec = _BlockSectionWidget(
            loaded['grid'],
            parent=scroll_content,
            title="Grid",
            section_key="grid",
        )
        settings_layout.addWidget(self._grid_sec)

        settings_layout.addStretch()

        # ── Action buttons ───────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self._apply_btn = QPushButton("Apply")
        self._apply_btn.setObjectName("applyButton")
        self._apply_btn.setToolTip("Save and apply settings to the measurement tool")
        self._apply_btn.clicked.connect(self._apply_settings)
        btn_row.addWidget(self._apply_btn)

        reset_btn = QPushButton("Reset")
        reset_btn.setToolTip("Restore all sections to default values")
        reset_btn.clicked.connect(self._reset_settings)
        btn_row.addWidget(reset_btn)

        close_btn = QPushButton("Close")
        close_btn.setToolTip("Close this dialog")
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)

        root.addLayout(btn_row)

    # ------------------------------------------------------------------
    def _build_style_dict(self) -> dict:
        return {
            'line':  self._line_sec.get_style(),
            'path':  self._path_sec.get_style(),
            'cross_section': self._cross_section_sec.get_style(),
            'block': self._block_sec.get_style(),
            'grid':  self._grid_sec.get_style(),
        }

    def _apply_settings(self):
        """Persist settings and push them to the live measurement tool."""
        style = self._build_style_dict()
        save_measure_settings(style)

        # Resolve app window (dialog may be parented to a ribbon widget)
        app = self.app
        if app is None or not hasattr(app, 'measurement_tool'):
            widget = self.parent()
            while widget is not None:
                if hasattr(widget, 'measurement_tool'):
                    app = widget
                    break
                widget = widget.parent()

        if app is not None:
            mt = getattr(app, 'measurement_tool', None)
            if mt is not None:
                # Store style on the tool and apply it
                mt._measure_style = style
                if hasattr(mt, 'apply_style'):
                    mt.apply_style(style)
            cs_measure = getattr(app, 'cross_section_measurement_tool', None)
            if cs_measure is not None and hasattr(cs_measure, 'apply_style'):
                cs_measure.apply_style(style)
                    
            try:
                # Show status bar message
                app.statusBar().showMessage(
                    "✅ Measurement settings applied properly",
                    4000,
                )
                
                # Also show a popup dialog as requested by the user
                QMessageBox.information(
                    self,
                    "Success",
                    "Measurement changes successfully applied!"
                )
            except Exception:
                pass

    def _reset_settings(self):
        """Restore all section controls to default values (does not persist until Apply)."""
        self._line_sec.reset_to_defaults()
        self._path_sec.reset_to_defaults()
        self._cross_section_sec.reset_to_defaults()
        self._block_sec.reset_to_defaults()
        self._grid_sec.reset_to_defaults()

    # ------------------------------------------------------------------
    def highlight_already_open(self):
        """Flash the window border to signal it is already open."""
        original = self.styleSheet()
        try:
            from gui.theme_manager import ThemeColors
            accent = ThemeColors.get("accent", "#0078d4")
        except Exception:
            accent = "#0078d4"
        self.setStyleSheet(original + f"QDialog {{ border: 2px solid {accent}; }}")
        QTimer.singleShot(500, lambda: self.setStyleSheet(original))
