# gui/draw_settings_dialog.py
# Draw Tool Settings Dialog — per-tool style customization

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QSlider, QComboBox, QColorDialog, QWidget,
    QListWidget, QListWidgetItem, QGroupBox, QSplitter
)
from PySide6.QtCore import Qt, QSettings, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QIcon, QPainterPath


# Tools that support polygon fill (closed-loop shapes)
FILLABLE_TOOLS = {'rectangle', 'circle', 'freehand', 'polyline', 'orthopolygon', 'smartline', 'hatcharea'}

# ── Default styles per tool ─────────────────────────────────────────────────
DEFAULT_DRAW_STYLES = {
    'smartline':    {'color': (1.0, 0.0, 0.0), 'width': 2, 'style': 'solid',
                     'fill_enabled': False, 'fill_color': (1.0, 0.0, 0.0), 'fill_opacity': 0.3},
    'line':         {'color': (1.0, 0.0, 0.0), 'width': 2, 'style': 'solid'},
    'centerline':   {'color': (1.0, 0.0, 0.0), 'width': 2, 'style': 'solid'},
    'orthopolygon': {'color': (1.0, 0.0, 0.0), 'width': 2, 'style': 'solid',
                     'fill_enabled': False, 'fill_color': (1.0, 0.0, 0.0), 'fill_opacity': 0.3},
    'polyline':     {'color': (1.0, 0.0, 0.0), 'width': 2, 'style': 'solid',
                     'fill_enabled': False, 'fill_color': (1.0, 0.0, 0.0), 'fill_opacity': 0.3},
    'rectangle':    {'color': (1.0, 0.0, 0.0), 'width': 2, 'style': 'solid',
                     'fill_enabled': False, 'fill_color': (1.0, 0.0, 0.0), 'fill_opacity': 0.3},
    'circle':       {'color': (0.0, 1.0, 1.0), 'width': 2, 'style': 'solid',
                     'fill_enabled': False, 'fill_color': (0.0, 1.0, 1.0), 'fill_opacity': 0.3},
    'freehand':     {'color': (1.0, 0.0, 1.0), 'width': 2, 'style': 'solid',
                     'fill_enabled': False, 'fill_color': (1.0, 0.0, 1.0), 'fill_opacity': 0.3},
    'curve':        {'color': (0.0, 1.0, 0.0), 'width': 2, 'style': 'solid'},
    'hatcharea':    {'color': (1.0, 1.0, 0.0), 'width': 1, 'style': 'solid',
                     'fill_enabled': False, 'fill_color': (1.0, 1.0, 0.0), 'fill_opacity': 0.3},
    'accudraw':     {'color': (0.0, 1.0, 1.0), 'width': 2, 'style': 'solid'},
}


# Display names for the tool list
TOOL_DISPLAY_NAMES = {
    'orthopolygon': 'Ortho',
    'smartline': '🔮  Smart Line',
    'line':      '📏  Line',
    'centerline': '≋  Centerline',
    'polyline':  '⬡  Polyline',
    'rectangle': '⬜  Rectangle',
    'circle':    '⭕  Circle',
    'freehand':  '✏️  Freehand',
    'curve':     '〰️  Curve',
    'hatcharea': '▦  Hatch Area',
    'accudraw':  '📐  AccuDraw',
}

TOOL_ORDER = ['smartline', 'line', 'centerline', 'orthopolygon', 'polyline', 'rectangle', 'circle', 'freehand', 'curve', 'hatcharea', 'accudraw']


def _plain_tool_display_name(tool_key):
    """Return a dialog-safe tool label without any leading icon glyphs."""
    label = TOOL_DISPLAY_NAMES.get(tool_key, tool_key)
    parts = label.split("  ", 1)
    return parts[1] if len(parts) == 2 else label


def vtk_color_to_qcolor(vtk_color):
    """Convert VTK float (0-1) color tuple to QColor."""
    return QColor(
        int(vtk_color[0] * 255),
        int(vtk_color[1] * 255),
        int(vtk_color[2] * 255),
    )


def qcolor_to_vtk(qcolor):
    """Convert QColor to VTK float (0-1) tuple."""
    return (qcolor.redF(), qcolor.greenF(), qcolor.blueF())


def load_draw_settings():
    """Load persisted draw tool styles from QSettings, falling back to defaults."""
    settings = QSettings("NakshaAI", "LidarApp")
    styles = {}
    for tool_key in TOOL_ORDER:
        default = DEFAULT_DRAW_STYLES[tool_key]
        color_name = settings.value(f"draw_style/{tool_key}/color", None)
        if color_name:
            qc = QColor(color_name)
            color = (qc.redF(), qc.greenF(), qc.blueF())
        else:
            color = default['color']
        width = int(settings.value(f"draw_style/{tool_key}/width", default['width']))
        style = settings.value(f"draw_style/{tool_key}/style", default['style'])
        tool_style = {'color': color, 'width': width, 'style': style}

        if tool_key in FILLABLE_TOOLS:
            tool_style['fill_enabled'] = settings.value(
                f"draw_style/{tool_key}/fill_enabled", default.get('fill_enabled', False), type=bool)
            fill_color_name = settings.value(f"draw_style/{tool_key}/fill_color", None)
            if fill_color_name:
                fc = QColor(fill_color_name)
                tool_style['fill_color'] = (fc.redF(), fc.greenF(), fc.blueF())
            else:
                tool_style['fill_color'] = default.get('fill_color', color)
            tool_style['fill_opacity'] = float(settings.value(
                f"draw_style/{tool_key}/fill_opacity", default.get('fill_opacity', 0.3)))

        styles[tool_key] = tool_style
    return styles


def save_draw_settings(styles):
    """Persist draw tool styles to QSettings."""
    settings = QSettings("NakshaAI", "LidarApp")
    for tool_key, props in styles.items():
        qc = vtk_color_to_qcolor(props['color'])
        settings.setValue(f"draw_style/{tool_key}/color", qc.name())
        settings.setValue(f"draw_style/{tool_key}/width", props['width'])
        settings.setValue(f"draw_style/{tool_key}/style", props['style'])
        if tool_key in FILLABLE_TOOLS:
            settings.setValue(f"draw_style/{tool_key}/fill_enabled", props.get('fill_enabled', False))
            fc = vtk_color_to_qcolor(props.get('fill_color', props['color']))
            settings.setValue(f"draw_style/{tool_key}/fill_color", fc.name())
            settings.setValue(f"draw_style/{tool_key}/fill_opacity", props.get('fill_opacity', 0.3))


# ── Line style helpers (VTK stipple) ────────────────────────────────────────
VTK_STIPPLE_MAP = {
    'solid':        0xFFFF,
    'dashed':       0x00FF,
    'dotted':       0xAAAA,
    'dash-dot':     0xFF18,
    'dash-dot-dot': 0xFF24,
}


def apply_line_style_to_actor(actor, style_name):
    """Apply line stipple pattern to a VTK actor based on style name."""
    prop = actor.GetProperty()
    if style_name == 'solid':
        prop.SetLineStipplePattern(0xFFFF)
    else:
        pattern = VTK_STIPPLE_MAP.get(style_name, 0xFFFF)
        prop.SetLineStipplePattern(pattern)
        prop.SetLineStippleRepeatFactor(1)


# ── Curve style persistence ─────────────────────────────────────────────────
def load_curve_settings():
    """Load persisted curve tool style from QSettings, falling back to defaults."""
    settings = QSettings("NakshaAI", "LidarApp")
    default = DEFAULT_DRAW_STYLES['curve']
    color_name = settings.value("curve_style/color", None)
    if color_name:
        qc = QColor(color_name)
        color = (qc.redF(), qc.greenF(), qc.blueF())
    else:
        color = default['color']
    width = int(settings.value("curve_style/width", default['width']))
    return {'color': color, 'width': width, 'style': 'solid'}


def save_curve_settings(style):
    """Persist curve tool style to QSettings."""
    settings = QSettings("NakshaAI", "LidarApp")
    qc = vtk_color_to_qcolor(style['color'])
    settings.setValue("curve_style/color", qc.name())
    settings.setValue("curve_style/width", style['width'])


# ── Preview Widget ──────────────────────────────────────────────────────────
class DrawPreviewWidget(QWidget):
    """Shows a live preview of the line appearance."""

    def __init__(self, parent_dialog):
        super().__init__()
        self.dialog = parent_dialog
        self.setFixedHeight(60)
        self.setMinimumWidth(200)

    def paintEvent(self, event):
        painter = QPainter(self)
        from gui.theme_manager import ThemeColors
        painter.fillRect(self.rect(), QColor(ThemeColors.get("bg_secondary")))
        painter.setRenderHint(QPainter.Antialiasing)

        pen = QPen(self.dialog.current_color)
        pen.setWidth(self.dialog.current_width)

        style_map = {
            'solid':        Qt.SolidLine,
            'dashed':       Qt.DashLine,
            'dotted':       Qt.DotLine,
            'dash-dot':     Qt.DashDotLine,
            'dash-dot-dot': Qt.DashDotDotLine,
        }
        pen.setStyle(style_map.get(self.dialog.current_style, Qt.SolidLine))
        painter.setPen(pen)

        y = self.height() // 2
        painter.drawLine(20, y, self.width() - 20, y)


# ── Curve Preview Widget ────────────────────────────────────────────────────
class CurvePreviewWidget(QWidget):
    """Shows a live preview of the curve appearance."""

    def __init__(self, parent_dialog):
        super().__init__()
        self.dialog = parent_dialog
        self.setFixedHeight(100)
        self.setMinimumWidth(250)

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            from gui.theme_manager import ThemeColors
            painter.fillRect(self.rect(), QColor(ThemeColors.get("bg_secondary")))
        except Exception:
            painter.fillRect(self.rect(), QColor("#1e1e1e"))
        painter.setRenderHint(QPainter.Antialiasing)

        pen = QPen(self.dialog.current_color)
        pen.setWidth(self.dialog.current_width)
        pen.setStyle(Qt.SolidLine)
        painter.setPen(pen)

        path = QPainterPath()
        y = self.height() // 2
        path.moveTo(20, y + 20)
        path.cubicTo(self.width() // 3, y - 40, 2 * self.width() // 3, y + 40, self.width() - 20, y - 20)
        painter.drawPath(path)


# ── Main Dialog ─────────────────────────────────────────────────────────────
class DrawToolSettingsDialog(QDialog):
    """Settings dialog for customizing draw tool appearance (per-tool or global)."""

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.setWindowTitle("Draw Tool Settings")
        self.setModal(False)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setMinimumSize(520, 420)

        # Current tool being edited
        self.selected_tool_key = TOOL_ORDER[0]

        # Working copy of styles (so we can cancel without saving)
        if hasattr(app, 'digitizer') and hasattr(app.digitizer, 'draw_tool_styles'):
            self._working_styles = {
                k: dict(v) for k, v in app.digitizer.draw_tool_styles.items()
            }
        else:
            self._working_styles = {
                k: dict(v) for k, v in DEFAULT_DRAW_STYLES.items()
            }

        # Ensure curve key exists in working styles
        if 'curve' not in self._working_styles:
            self._working_styles['curve'] = dict(DEFAULT_DRAW_STYLES['curve'])

        # Load curve settings from curve_tool if available
        curve_tool = getattr(app, 'curve_tool', None)
        if curve_tool and hasattr(curve_tool, 'curve_style'):
            self._working_styles['curve'] = {
                'color': curve_tool.curve_style.get('color', DEFAULT_DRAW_STYLES['curve']['color']),
                'width': curve_tool.curve_style.get('width', DEFAULT_DRAW_STYLES['curve']['width']),
                'style': 'solid',
            }

        # Current editing values (synced from working styles)
        s = self._working_styles[self.selected_tool_key]
        self.current_color = vtk_color_to_qcolor(s['color'])
        self.current_width = s['width']
        self.current_style = s['style']

        self._build_ui()
        self._apply_dark_theme()

    # ── UI Construction ─────────────────────────────────────────────────
    def _build_ui(self):
        root_layout = QHBoxLayout(self)
        root_layout.setSpacing(12)

        # ── Left: Tool list ─────────────────────────────────────────────
        left_box = QVBoxLayout()
        left_box.addWidget(QLabel("Select Tool:"))

        self.tool_list = QListWidget()
        self.tool_list.setFixedWidth(170)

        # Add per-tool entries
        for key in TOOL_ORDER:
            item = QListWidgetItem(_plain_tool_display_name(key))
            item.setData(Qt.UserRole, key)
            self.tool_list.addItem(item)

        # Add "All Tools (Global)" entry
        global_item = QListWidgetItem("All Tools (Global)")
        global_item.setData(Qt.UserRole, "__global__")
        self.tool_list.addItem(global_item)

        self.tool_list.setCurrentRow(0)
        self.tool_list.currentItemChanged.connect(self._on_tool_selected)
        left_box.addWidget(self.tool_list)
        root_layout.addLayout(left_box)

        # ── Right: Settings panel ───────────────────────────────────────
        right_box = QVBoxLayout()

        self.tool_title = QLabel(_plain_tool_display_name(self.selected_tool_key))
        self.tool_title.setStyleSheet("font-size: 15px; font-weight: bold; margin-bottom: 4px;")
        right_box.addWidget(self.tool_title)

        settings_group = QGroupBox("Appearance")
        form = QVBoxLayout()
        form.setSpacing(10)

        # Color row
        color_row = QHBoxLayout()
        color_row.addWidget(QLabel("Color:"))
        self.color_button = QPushButton()
        self.color_button.setFixedSize(50, 30)
        self.color_button.setCursor(Qt.PointingHandCursor)
        self._update_color_button()
        self.color_button.clicked.connect(self._choose_color)
        color_row.addWidget(self.color_button)
        color_row.addStretch()
        form.addLayout(color_row)

        # Width row
        width_row = QHBoxLayout()
        width_row.addWidget(QLabel("Width:"))
        self.width_slider = QSlider(Qt.Horizontal)
        self.width_slider.setRange(1, 10)
        self.width_slider.setValue(self.current_width)
        self.width_slider.valueChanged.connect(self._on_width_changed)
        width_row.addWidget(self.width_slider)
        self.width_label = QLabel(f"{self.current_width} px")
        self.width_label.setFixedWidth(36)
        width_row.addWidget(self.width_label)
        form.addLayout(width_row)

        # Style row
        style_row = QHBoxLayout()
        style_row.addWidget(QLabel("Style:"))
        self.style_combo = QComboBox()
        self.style_combo.addItems(["Solid", "Dashed", "Dotted", "Dash-Dot", "Dash-Dot-Dot"])
        self.style_combo.setCurrentText(self.current_style.capitalize() if '-' not in self.current_style
                                        else self.current_style.title())
        self.style_combo.currentTextChanged.connect(self._on_style_changed)
        self._style_row = style_row
        style_row.addWidget(self.style_combo)
        style_row.addStretch()
        form.addLayout(style_row)

        settings_group.setLayout(form)
        right_box.addWidget(settings_group)

        # Preview
        right_box.addWidget(QLabel("Preview:"))
        self.preview_widget = DrawPreviewWidget(self)
        self.curve_preview_widget = CurvePreviewWidget(self)
        right_box.addWidget(self.preview_widget)
        right_box.addWidget(self.curve_preview_widget)
        self.curve_preview_widget.hide()
        right_box.addStretch()

        # Buttons
        btn_row = QHBoxLayout()
        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(self._apply_settings)
        btn_row.addWidget(apply_btn)

        reset_btn = QPushButton("Reset")
        reset_btn.clicked.connect(self._reset_settings)
        btn_row.addWidget(reset_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)

        right_box.addLayout(btn_row)
        root_layout.addLayout(right_box, 1)

    # ── Callbacks ───────────────────────────────────────────────────────
    def _on_tool_selected(self, current, previous):
        if current is None:
            return
        # Save edits for previous tool before switching
        self._commit_current_to_working()

        key = current.data(Qt.UserRole)
        if key == "__global__":
            self.selected_tool_key = "__global__"
            self.tool_title.setText("All Tools (Global)")
            # Show first tool's settings as a starting point
            s = self._working_styles[TOOL_ORDER[0]]
        else:
            self.selected_tool_key = key
            self.tool_title.setText(_plain_tool_display_name(key))
            s = self._working_styles[key]

        self.current_color = vtk_color_to_qcolor(s['color'])
        self.current_width = s['width']
        self.current_style = s['style']

        self._update_color_button()
        self.width_slider.blockSignals(True)
        self.width_slider.setValue(self.current_width)
        self.width_slider.blockSignals(False)
        self.width_label.setText(f"{self.current_width} px")

        # Map style text for combo box
        style_text = self.current_style.replace('-', '-').title()
        if style_text == 'Dash-Dot-Dot':
            style_text = 'Dash-Dot-Dot'
        elif style_text == 'Dash-Dot':
            style_text = 'Dash-Dot'
        self.style_combo.blockSignals(True)
        self.style_combo.setCurrentText(style_text)
        self.style_combo.blockSignals(False)

        # Handle curve tool - hide style combo and show curve preview
        is_curve = (key == 'curve')
        self.style_combo.setVisible(not is_curve)
        # Find and hide the style label
        for i in range(self._style_row.count()):
            widget = self._style_row.itemAt(i).widget()
            if widget and isinstance(widget, QLabel) and widget.text() == "Style:":
                widget.setVisible(not is_curve)
                break
        self.preview_widget.setVisible(not is_curve)
        self.curve_preview_widget.setVisible(is_curve)

        if is_curve:
            self.tool_title.setText("🔮 Curve Tool")

        self.preview_widget.update()
        self.curve_preview_widget.update()

    def _choose_color(self):
        title = "Choose Curve Color" if self.selected_tool_key == 'curve' else "Choose Line Color"
        color = QColorDialog.getColor(self.current_color, self, title)
        if color.isValid():
            self.current_color = color
            self._update_color_button()
            self.preview_widget.update()
            self.curve_preview_widget.update()

    def _update_color_button(self):
        self.color_button.setStyleSheet(
            f"background-color: {self.current_color.name()}; border: 2px solid #fff; border-radius: 4px;"
        )

    def _on_width_changed(self, value):
        self.current_width = value
        self.width_label.setText(f"{value} px")
        self.preview_widget.update()
        self.curve_preview_widget.update()

    def _on_style_changed(self, text):
        self.current_style = text.lower()
        self.preview_widget.update()
        self.curve_preview_widget.update()

    # ── Commit / Apply / Reset ──────────────────────────────────────────
    def _commit_current_to_working(self):
        """Write current UI values into the working dict for the active tool."""
        vtk_c = qcolor_to_vtk(self.current_color)
        base = {
            "color": vtk_c,
            "width": self.current_width,
            "style": self.current_style,
        }
        if self.selected_tool_key == "__global__":
            for key in TOOL_ORDER:
                prev = dict(self._working_styles.get(key, {}))
                # Keep any fill / extra keys that the dialog doesn't expose.
                prev.update(base)
                self._working_styles[key] = prev
        else:
            prev = dict(self._working_styles.get(self.selected_tool_key, {}))
            prev.update(base)
            self._working_styles[self.selected_tool_key] = prev

    def _apply_settings(self):
        """Apply settings to the digitizer/curve_tool and persist."""
        self._commit_current_to_working()

        # Handle curve tool separately
        if self.selected_tool_key == 'curve':
            curve_tool = getattr(self.app, 'curve_tool', None)
            if not curve_tool:
                widget = self.parent()
                while widget:
                    if hasattr(widget, 'curve_tool'):
                        curve_tool = widget.curve_tool
                        self.app = widget
                        break
                    widget = widget.parent()

            if curve_tool:
                curve_tool.curve_style = dict(self._working_styles['curve'])
                print(f"✅ Pushed styles to curve_tool.curve_style")

            # Persist to QSettings
            save_curve_settings(self._working_styles['curve'])
            print(f"✅ Curve settings saved: color={self.current_color.name()}, width={self.current_width}px")

            # Also push the (updated) curve style into the digitizer's
            # draw_tool_styles so that update_all_drawings_style() can pick it
            # up for drawings whose "source" is "curve_tool".
            digitizer = None
            if hasattr(self.app, 'digitizer'):
                digitizer = self.app.digitizer
            else:
                widget = self.parent()
                while widget:
                    if hasattr(widget, 'digitizer'):
                        digitizer = widget.digitizer
                        self.app = widget
                        break
                    widget = widget.parent()
            if digitizer is not None and hasattr(digitizer, 'draw_tool_styles'):
                working = dict(self._working_styles)
                working.setdefault('curve', dict(self._working_styles.get('curve', {})))
                digitizer.draw_tool_styles = working
                try:
                    if hasattr(digitizer, 'update_all_drawings_style'):
                        digitizer.update_all_drawings_style()
                        print(f"✅ Updated existing curve drawings to new curve style")
                except Exception as e:
                    print(f"⚠️ Curve style update failed: {e}")

            try:
                if hasattr(self.app, 'statusBar'):
                    self.app.statusBar().showMessage("✅ Curve settings saved", 2000)
            except Exception:
                pass
            return

        # Handle other draw tools
        # Robustly find the digitizer
        digitizer = None
        if hasattr(self.app, 'digitizer'):
            digitizer = self.app.digitizer
        else:
            # Walk parents to find the app with digitizer
            widget = self.parent()
            while widget:
                if hasattr(widget, 'digitizer'):
                    digitizer = widget.digitizer
                    self.app = widget  # Fix for future calls
                    print(f"🔧 Found digitizer via parent walk: {type(widget).__name__}")
                    break
                widget = widget.parent()

        if digitizer and hasattr(digitizer, 'draw_tool_styles'):
            # Push all non-curve tools to digitizer
            draw_styles = {k: dict(v) for k, v in self._working_styles.items() if k != 'curve'}
            digitizer.draw_tool_styles = draw_styles
            print(f"✅ Pushed styles to digitizer.draw_tool_styles")
            try:
                if hasattr(digitizer, "update_all_drawings_style"):
                    digitizer.update_all_drawings_style()
                    print(f"✅ Updated {len(getattr(digitizer, 'drawings', []))} existing drawings to new styles")
                if hasattr(digitizer, "_deferred_preview_update"):
                    digitizer._deferred_preview_update()
            except Exception as e:
                print(f"⚠️ Style update failed: {e}")
        else:
            print(f"⚠️ Could not find digitizer! app={type(self.app).__name__}, has_digitizer={hasattr(self.app, 'digitizer')}")

        # Persist to QSettings (always works regardless of digitizer ref)
        save_draw_settings(self._working_styles)

        tool_label = "All Tools" if self.selected_tool_key == "__global__" else TOOL_DISPLAY_NAMES.get(self.selected_tool_key, self.selected_tool_key)
        print(f"✅ Draw settings saved for {tool_label}")
        for k, v in self._working_styles.items():
            if k == 'curve':
                continue
            qc = vtk_color_to_qcolor(v['color'])
            print(f"   {TOOL_DISPLAY_NAMES.get(k, k)}: color={qc.name()}, width={v['width']}px, style={v['style']}")

        try:
            self.app.statusBar().showMessage("✅ Draw tool settings saved", 2000)
        except Exception:
            pass

    def _reset_settings(self):
        """Reset all tools to their factory defaults."""
        self._working_styles = {k: dict(v) for k, v in DEFAULT_DRAW_STYLES.items()}

        # Reload UI for current selection
        if self.selected_tool_key == "__global__":
            s = self._working_styles[TOOL_ORDER[0]]
        else:
            s = self._working_styles.get(self.selected_tool_key, DEFAULT_DRAW_STYLES[TOOL_ORDER[0]])

        self.current_color = vtk_color_to_qcolor(s['color'])
        self.current_width = s['width']
        self.current_style = s['style']

        self._update_color_button()
        self.width_slider.setValue(self.current_width)
        self.width_label.setText(f"{self.current_width} px")
        self.style_combo.setCurrentText("Solid")
        self.preview_widget.update()
        self.curve_preview_widget.update()

        print("🔄 Draw settings reset to defaults")

    # ── Dark Theme ──────────────────────────────────────────────────────
    def _apply_dark_theme(self):
        from gui.theme_manager import get_dialog_stylesheet
        self._base_stylesheet = get_dialog_stylesheet()
        self.setStyleSheet(self._base_stylesheet)

    def highlight_already_open(self):
        """Briefly highlight this dialog when the Settings button is clicked again."""
        try:
            self.app.statusBar().showMessage("Draw Tool Settings is already open", 2000)
        except Exception:
            pass

        highlight = self._base_stylesheet + """
            QDialog {
                border: 2px solid #ffb74d;
            }
        """

        def set_highlighted(enabled):
            try:
                self.setStyleSheet(highlight if enabled else self._base_stylesheet)
            except RuntimeError:
                pass

        for index, enabled in enumerate((True, False, True, False)):
            QTimer.singleShot(index * 180, lambda enabled=enabled: set_highlighted(enabled))
