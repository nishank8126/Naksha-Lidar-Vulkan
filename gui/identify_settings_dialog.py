# gui/identify_settings_dialog.py
# Identify Tool Settings window — per-tool appearance (color / width) for the
# Identify ribbon's rectangle tools. Mirrors the layout of the Draw Tool
# Settings dialog (and gui/classify_settings_dialog.py).

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QSlider, QColorDialog, QListWidget, QListWidgetItem, QGroupBox,
)
from PySide6.QtCore import Qt, QSettings, QTimer

from gui.draw_settings_dialog import (
    DrawPreviewWidget, vtk_color_to_qcolor, qcolor_to_vtk,
)

_ORG, _APP = "NakshaAI", "LidarApp"

# Identify tools exposed in this window: key -> label
IDENTIFY_TOOLS = {
    "zoom_rect": "Zoom",
    "select_rect": "Select",
}
TOOL_ORDER = list(IDENTIFY_TOOLS)

# Defaults match the previous hard-coded rubber-band look for each tool.
DEFAULT_IDENTIFY_STYLES = {
    "zoom_rect": {"color": (0.0, 0.5, 1.0), "width": 2},
    "select_rect": {"color": (1.0, 0.5, 0.0), "width": 3},
}

_style_cache = {}


def load_identify_style(tool_key):
    """Return {'color': (r, g, b) 0-1, 'width': int} for an identify tool."""
    cached = _style_cache.get(tool_key)
    if cached is not None:
        return cached
    default = DEFAULT_IDENTIFY_STYLES.get(tool_key)
    if default is None:
        return None
    from PySide6.QtGui import QColor
    settings = QSettings(_ORG, _APP)
    color_name = settings.value(f"identify_style/{tool_key}/color", None)
    if color_name:
        qc = QColor(color_name)
        color = (qc.redF(), qc.greenF(), qc.blueF()) if qc.isValid() else default["color"]
    else:
        color = default["color"]
    try:
        width = int(settings.value(f"identify_style/{tool_key}/width", default["width"]))
    except (TypeError, ValueError):
        width = default["width"]
    style = {"color": color, "width": max(1, width)}
    _style_cache[tool_key] = style
    return style


def save_identify_style(tool_key, style):
    settings = QSettings(_ORG, _APP)
    settings.setValue(
        f"identify_style/{tool_key}/color", vtk_color_to_qcolor(style["color"]).name()
    )
    settings.setValue(f"identify_style/{tool_key}/width", int(style["width"]))
    _style_cache[tool_key] = {"color": tuple(style["color"]), "width": int(style["width"])}


def apply_identify_style_to_actor(actor, tool_key, default_color=(1.0, 0.5, 0.0),
                                  default_width=2):
    """Colour/width a preview actor for `tool_key`; unknown tools get the default."""
    try:
        style = load_identify_style(tool_key)
        prop = actor.GetProperty()
        if style is not None:
            prop.SetColor(*style["color"])
            prop.SetLineWidth(style["width"])
        else:
            prop.SetColor(*default_color)
            prop.SetLineWidth(default_width)
    except Exception:
        pass


class IdentifyToolSettingsDialog(QDialog):
    """Identify Tool Settings window: colour and width for Zoom and Select."""

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.setWindowTitle("Identify Tool Settings")
        self.setModal(False)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setMinimumSize(520, 420)

        self.selected_tool_key = TOOL_ORDER[0]
        self._working_styles = {
            k: dict(load_identify_style(k)) for k in TOOL_ORDER
        }

        s = self._working_styles[self.selected_tool_key]
        self.current_color = vtk_color_to_qcolor(s["color"])
        self.current_width = s["width"]
        # DrawPreviewWidget reads this to pick the pen style.
        self.current_style = "solid"

        self._build_ui()
        self._apply_dark_theme()

    # ── UI ──────────────────────────────────────────────────────────────
    def _build_ui(self):
        root_layout = QHBoxLayout(self)
        root_layout.setSpacing(12)

        left_box = QVBoxLayout()
        left_box.addWidget(QLabel("Select Tool:"))
        self.tool_list = QListWidget()
        self.tool_list.setFixedWidth(170)
        for key in TOOL_ORDER:
            item = QListWidgetItem(IDENTIFY_TOOLS[key])
            item.setData(Qt.UserRole, key)
            self.tool_list.addItem(item)
        self.tool_list.setCurrentRow(0)
        self.tool_list.currentItemChanged.connect(self._on_tool_selected)
        left_box.addWidget(self.tool_list)
        root_layout.addLayout(left_box)

        right_box = QVBoxLayout()
        self.tool_title = QLabel(IDENTIFY_TOOLS[self.selected_tool_key])
        self.tool_title.setStyleSheet("font-size: 15px; font-weight: bold; margin-bottom: 4px;")
        right_box.addWidget(self.tool_title)

        group = QGroupBox("Appearance")
        form = QVBoxLayout()
        form.setSpacing(10)

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

        group.setLayout(form)
        right_box.addWidget(group)

        right_box.addWidget(QLabel("Preview:"))
        self.preview_widget = DrawPreviewWidget(self)
        right_box.addWidget(self.preview_widget)
        right_box.addStretch()

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
    def _load_tool_into_ui(self, key):
        s = self._working_styles[key]
        self.current_color = vtk_color_to_qcolor(s["color"])
        self.current_width = s["width"]
        self._update_color_button()
        self.width_slider.blockSignals(True)
        self.width_slider.setValue(self.current_width)
        self.width_slider.blockSignals(False)
        self.width_label.setText(f"{self.current_width} px")
        self.tool_title.setText(IDENTIFY_TOOLS[key])
        self.preview_widget.update()

    def _on_tool_selected(self, current, previous):
        if current is None:
            return
        self._commit_current_to_working()
        self.selected_tool_key = current.data(Qt.UserRole)
        self._load_tool_into_ui(self.selected_tool_key)

    def _choose_color(self):
        color = QColorDialog.getColor(self.current_color, self, "Choose Rectangle Color")
        if color.isValid():
            self.current_color = color
            self._update_color_button()
            self.preview_widget.update()

    def _update_color_button(self):
        self.color_button.setStyleSheet(
            f"background-color: {self.current_color.name()}; border: 2px solid #fff; border-radius: 4px;"
        )

    def _on_width_changed(self, value):
        self.current_width = value
        self.width_label.setText(f"{value} px")
        self.preview_widget.update()

    # ── Commit / Apply / Reset ──────────────────────────────────────────
    def _commit_current_to_working(self):
        self._working_styles[self.selected_tool_key] = {
            "color": qcolor_to_vtk(self.current_color),
            "width": self.current_width,
        }

    def _apply_settings(self):
        self._commit_current_to_working()
        for key, style in self._working_styles.items():
            save_identify_style(key, style)
        print(
            f"✅ Identify settings saved: "
            f"{IDENTIFY_TOOLS[self.selected_tool_key]} color={self.current_color.name()}, "
            f"width={self.current_width}px"
        )
        try:
            self.app.statusBar().showMessage("✅ Identify tool settings saved", 2000)
        except Exception:
            pass

    def _reset_settings(self):
        self._working_styles = {
            k: dict(DEFAULT_IDENTIFY_STYLES[k]) for k in TOOL_ORDER
        }
        self._load_tool_into_ui(self.selected_tool_key)

    # ── Theme / already-open highlight ──────────────────────────────────
    def _apply_dark_theme(self):
        from gui.theme_manager import get_dialog_stylesheet
        self._base_stylesheet = get_dialog_stylesheet()
        self.setStyleSheet(self._base_stylesheet)

    def highlight_already_open(self):
        try:
            self.app.statusBar().showMessage("Identify Tool Settings is already open", 2000)
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
