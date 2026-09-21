"""View Fields dialog — lists point-cloud attribute fields available in the
currently loaded LAS/LAZ file (MicroStation-style "View Fields" panel).

Only the header is reopened (no point data is read), so this stays cheap
even on very large files.
"""

import os

from PySide6.QtCore import QRectF, QSettings, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QRegion
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from gui.icon_provider import get_icon
from gui.theme_manager import ThemeColors, get_dialog_stylesheet


IMAGE_FIELD_LABEL = "Image"
IMAGE_SOURCE_KEY = "__image_related__"

_IMAGE_DIMENSION_PRIORITY = (
    "imageid",
    "imagenumber",
    "imagenum",
    "imageno",
    "imageindex",
    "image",
    "photoid",
    "photonumber",
    "photoindex",
    "photo",
    "cameraimageid",
    "cameraid",
    "frameid",
    "framenumber",
    "frameindex",
)


def _normalise_dimension_name(name):
    return "".join(ch for ch in str(name).casefold() if ch.isalnum())


def find_image_dimension_name(dimension_names):
    """Return the best image-related per-point dimension name, if present."""
    candidates = []
    for name in dimension_names or ():
        text = str(name).strip()
        if not text or text.startswith("_"):
            continue
        normalised = _normalise_dimension_name(text)
        if normalised:
            candidates.append((name, normalised))

    by_normalised = {}
    for original, normalised in candidates:
        by_normalised.setdefault(normalised, original)

    for preferred in _IMAGE_DIMENSION_PRIORITY:
        if preferred in by_normalised:
            return by_normalised[preferred]

    # Vendor extra-byte names vary. Image/photo terms are specific enough to
    # accept directly; camera/frame names require an identifier-like suffix.
    for original, normalised in candidates:
        if "image" in normalised or "photo" in normalised:
            return original
        for prefix in ("camera", "frame"):
            if normalised.startswith(prefix) and normalised[len(prefix):] in {
                "id", "number", "num", "no", "index",
            }:
                return original
    return None

# (display label, required LAS dimension names, always available)
FIELD_SPECS = [
    ("Class", ("classification",), True),
    ("Description", ("classification",), True),
    ("Line", ("point_source_id",), False),
    (IMAGE_FIELD_LABEL, (), False),
    ("Time", ("gps_time",), False),
    ("Date", ("gps_time",), False),
    ("Echo", ("return_number",), False),
    ("Easting", ("x",), True),
    ("Northing", ("y",), True),
    ("Elevation", ("z",), True),
    ("Amplitude", ("amplitude",), False),
    ("Intensity", ("intensity",), False),
    ("Reflectance", ("reflectance",), False),
    ("Deviation", ("deviation",), False),
    ("Reliability", ("reliability",), False),
    ("Color RGB", ("red", "green", "blue"), False),
]

DEFAULT_CHECKED = {"Class", "Description", "Easting", "Northing", "Elevation"}

_SETTINGS_KEY = "view_fields/checked"


def _read_dimension_names(filename):
    """Read only the point-format dimension names from a LAS/LAZ header."""
    if not filename or not os.path.isfile(filename):
        return set()
    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".las", ".laz"):
        return set()
    try:
        import laspy
        with laspy.open(filename) as las_file:
            return {str(d).lower() for d in las_file.header.point_format.dimension_names}
    except Exception:
        return set()


class _TitleCloseButton(QWidget):
    """Self-painted close button for the frameless title bar.

    Painted by hand (no QPushButton) so the app-wide button theme cannot add
    a box/border to it; only a red hover fill that follows the window's
    rounded top-right corner is drawn.
    """

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(40, 28)
        self.setCursor(Qt.ArrowCursor)
        self.setToolTip("Close")
        self._hover = False

    def enterEvent(self, event):
        self._hover = True
        self.update()

    def leaveEvent(self, event):
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        if self._hover:
            r = 9.0
            path = QPainterPath()
            path.moveTo(0, 0)
            path.lineTo(w - r, 0)
            path.quadTo(w, 0, w, r)
            path.lineTo(w, h)
            path.lineTo(0, h)
            path.closeSubpath()
            p.fillPath(path, QColor("#c42b1c"))
        pen = QPen(QColor("#ffffff") if self._hover else QColor(ThemeColors.get("text_primary")))
        pen.setWidthF(1.3)
        p.setPen(pen)
        cx, cy, d = w / 2.0, h / 2.0, 5.0
        p.drawLine(QRectF(cx - d, cy - d, 0, 0).topLeft(), QRectF(cx + d, cy + d, 0, 0).topLeft())
        p.drawLine(QRectF(cx + d, cy - d, 0, 0).topLeft(), QRectF(cx - d, cy + d, 0, 0).topLeft())


class ViewFieldsDialog(QDialog):
    """Checklist of point-cloud fields available in the loaded file."""

    def __init__(self, filename=None, parent=None, available_dimensions=None):
        super().__init__(parent)
        self.setProperty("themeStyledDialog", True)
        self.filename = filename
        self._settings = QSettings("NakshaAI", "LidarApp")
        self._available_dims = _read_dimension_names(filename)
        self._available_dims.update(
            str(name).casefold() for name in (available_dimensions or ())
        )

        self.setWindowTitle("View Fields")
        # Frameless + translucent so we can draw our own rounded frame and a
        # title bar with only a Close button.
        self.setObjectName("viewFieldsDialog")
        self.setWindowFlags(
            Qt.Dialog
            | Qt.FramelessWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._drag_pos = None
        icon = get_icon("fields")
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.resize(340, 520)
        self.setMinimumSize(300, 420)
        self.setSizeGripEnabled(True)

        self.setStyleSheet(get_dialog_stylesheet() + self._extra_stylesheet())

        self._build_ui()
        self._populate_fields()

    def _extra_stylesheet(self):
        c = ThemeColors
        return f"""
        QDialog#viewFieldsDialog {{
            background: transparent;
        }}
        QFrame#vfContainer {{
            background-color: {c.get('bg_primary')};
            border: 1px solid {c.get('border_light')};
            border-radius: 10px;
        }}
        QFrame#vfTitleBar {{
            background-color: {c.get('bg_secondary')};
            border: none;
            border-top-left-radius: 9px;
            border-top-right-radius: 9px;
            border-bottom-left-radius: 0px;
            border-bottom-right-radius: 0px;
        }}
        QLabel#vfTitleText {{
            background: transparent;
            color: {c.get('text_primary')};
            font-size: 12px;
        }}
        QListWidget#fieldsList {{
            background-color: {c.get('bg_input')};
            border: 1px solid {c.get('border_light')};
            border-radius: 6px;
            outline: none;
        }}
        QListWidget#fieldsList::item {{
            padding: 3px 4px;
            color: {c.get('text_primary')};
        }}
        QListWidget#fieldsList::item:disabled {{
            color: {c.get('text_muted')};
        }}
        """

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        container = QFrame()
        container.setObjectName("vfContainer")
        outer.addWidget(container)
        container_lay = QVBoxLayout(container)
        container_lay.setContentsMargins(1, 1, 1, 1)
        container_lay.setSpacing(0)

        # --- custom title bar: icon, title, Close ------------------------
        self._title_bar = QFrame()
        self._title_bar.setObjectName("vfTitleBar")
        bar = QHBoxLayout(self._title_bar)
        bar.setContentsMargins(8, 0, 0, 0)
        bar.setSpacing(6)

        icon = get_icon("fields")
        if not icon.isNull():
            icon_lbl = QLabel()
            icon_lbl.setPixmap(icon.pixmap(14, 14))
            icon_lbl.setStyleSheet("background: transparent;")
            bar.addWidget(icon_lbl)
        title_text = QLabel("View Fields")
        title_text.setObjectName("vfTitleText")
        bar.addWidget(title_text)
        bar.addStretch()

        close_btn = _TitleCloseButton()
        close_btn.clicked.connect(self.reject)
        bar.addWidget(close_btn)
        container_lay.addWidget(self._title_bar)

        body = QWidget()
        body.setStyleSheet("background: transparent;")
        container_lay.addWidget(body, 1)
        root = QVBoxLayout(body)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        title = QLabel("View Fields")
        title.setObjectName("dialogTitle")
        root.addWidget(title)

        if self.filename:
            subtitle = QLabel(os.path.basename(self.filename))
            subtitle.setObjectName("dialogSubtitle")
            root.addWidget(subtitle)

        self.fields_list = QListWidget()
        self.fields_list.setObjectName("fieldsList")
        self.fields_list.setMinimumSize(0, 0)
        self.fields_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        root.addWidget(self.fields_list, 1)

        actions = QHBoxLayout()

        all_btn = QPushButton("All")
        all_btn.setToolTip("Select all available fields")
        clear_btn = QPushButton("Clear")
        clear_btn.setToolTip("Clear all selections")
        ok_btn = QPushButton("Apply")
        ok_btn.setObjectName("primaryBtn")
        ok_btn.setDefault(True)
        ok_btn.setAutoDefault(True)

        # Ignored made the buttons collapse to zero width next to the
        # addStretch() spacer, so they were never visible. Keep them
        # shrinkable (min width 0) but let them claim their natural width.
        for button in (all_btn, clear_btn, ok_btn):
            button.setMinimumWidth(0)
            button.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)

        actions.addWidget(all_btn)
        actions.addStretch()
        actions.addWidget(clear_btn)
        actions.addWidget(ok_btn)
        root.addLayout(actions)

        all_btn.clicked.connect(self._select_all)
        clear_btn.clicked.connect(self._clear_all)
        ok_btn.clicked.connect(self._on_accept)

    _CORNER_RADIUS = 10

    def _apply_rounded_mask(self):
        """Clip the window itself to a rounded rectangle.

        The theme's dialog background can still paint an opaque rect behind
        the translucent frame, leaving square black corners; a window mask
        guarantees the corners are really cut off whatever paints below.
        """
        path = QPainterPath()
        path.addRoundedRect(
            QRectF(self.rect()), self._CORNER_RADIUS, self._CORNER_RADIUS
        )
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_rounded_mask()

    def showEvent(self, event):
        super().showEvent(event)
        self._apply_rounded_mask()

    # -- drag the frameless window by its title bar ------------------------
    def mousePressEvent(self, event):
        if (
            event.button() == Qt.LeftButton
            and self._title_bar.geometry().contains(
                self._title_bar.parentWidget().mapFrom(self, event.position().toPoint())
            )
        ):
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    def _iter_available_items(self):
        for i in range(self.fields_list.count()):
            item = self.fields_list.item(i)
            # Skip disabled items (fields not present in the loaded file)
            if item.flags() & Qt.ItemIsEnabled:
                yield item

    def _select_all(self):
        for item in self._iter_available_items():
            item.setCheckState(Qt.Checked)

    def _clear_all(self):
        for item in self._iter_available_items():
            item.setCheckState(Qt.Unchecked)

    def _populate_fields(self):
        saved = self._settings.value(_SETTINGS_KEY)
        saved_checked = set(saved) if isinstance(saved, (list, tuple, set)) else None

        image_dimension = find_image_dimension_name(self._available_dims)

        for label, dims, always_available in FIELD_SPECS:
            if label == IMAGE_FIELD_LABEL:
                available = image_dimension is not None
            else:
                available = always_available or any(
                    d in self._available_dims for d in dims
                )

            item = QListWidgetItem(label)
            item.setFlags(Qt.ItemIsUserCheckable | (Qt.ItemIsEnabled | Qt.ItemIsSelectable if available else Qt.NoItemFlags))

            if saved_checked is not None:
                checked = available and label in saved_checked
            else:
                checked = available and label in DEFAULT_CHECKED
            item.setCheckState(Qt.Checked if checked else Qt.Unchecked)

            if label == IMAGE_FIELD_LABEL and available:
                item.setToolTip("Source dimension: %s" % image_dimension)
            elif not available:
                item.setToolTip("Not present in the loaded file")

            self.fields_list.addItem(item)

    def selected_fields(self):
        return [
            self.fields_list.item(i).text()
            for i in range(self.fields_list.count())
            if self.fields_list.item(i).checkState() == Qt.Checked
        ]

    def _on_accept(self):
        self._settings.setValue(_SETTINGS_KEY, self.selected_fields())
        self.accept()
