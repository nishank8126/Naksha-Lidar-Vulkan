"""View Fields dialog — lists point-cloud attribute fields available in the
currently loaded LAS/LAZ file (MicroStation-style "View Fields" panel).

Only the header is reopened (no point data is read), so this stays cheap
even on very large files.
"""

import os

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
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
        self.setWindowFlags(
            Qt.Dialog
            | Qt.WindowTitleHint
            | Qt.WindowSystemMenuHint
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowMaximizeButtonHint
            | Qt.WindowCloseButtonHint
        )
        icon = get_icon("fields")
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.resize(340, 420)
        self.setMinimumSize(0, 0)
        self.setSizeGripEnabled(True)

        self.setStyleSheet(get_dialog_stylesheet() + self._extra_stylesheet())

        self._build_ui()
        self._populate_fields()

    def _extra_stylesheet(self):
        c = ThemeColors
        return f"""
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
        root = QVBoxLayout(self)
        root.setSizeConstraint(QLayout.SetNoConstraint)
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
        ok_btn = QPushButton("OK")
        ok_btn.setObjectName("primaryBtn")
        ok_btn.setDefault(True)
        ok_btn.setAutoDefault(True)

        for button in (all_btn, clear_btn, ok_btn):
            button.setMinimumWidth(0)
            button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)

        actions.addWidget(all_btn)
        actions.addStretch()
        actions.addWidget(clear_btn)
        actions.addWidget(ok_btn)
        root.addLayout(actions)

        all_btn.clicked.connect(self._select_all)
        clear_btn.clicked.connect(self._clear_all)
        ok_btn.clicked.connect(self._on_accept)

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
