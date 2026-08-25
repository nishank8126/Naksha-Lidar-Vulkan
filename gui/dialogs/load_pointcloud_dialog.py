from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

import os
import re

import laspy
from laspy.vlrs.known import WktCoordinateSystemVlr
from pyproj import CRS

from gui.theme_manager import ThemeColors, get_dialog_stylesheet


CLASS_NAMES = {
    0: "Created, never classified",
    1: "Unclassified",
    2: "Ground",
    3: "Low vegetation",
    4: "Medium vegetation",
    5: "High vegetation",
    6: "Building",
    7: "Low point (noise)",
    8: "Model key-point",
    9: "Water",
    10: "Rail",
    11: "Road surface",
    12: "Overlap points",
    13: "Wire guard",
    14: "Wire conductor",
    15: "Transmission tower",
    16: "Wire connector",
    17: "Bridge deck",
    18: "High noise",
}

DEFAULT_IMPORT_OPTIONS = {
    "cloud_type": "Airborne lidar",
    "system": "Other",
    "input_proj": "",
    "active_proj": "",
    "only_every": False,
    "nth_point": 10,
    "only_class": False,
    "class_codes": [],        # list of selected class codes (replaces single class_code)
    "line_mode": "Use from file",
    "scanner_mode": "File -- scanner byte",
    "attributes": {
        "XYZ": True,
        "Amplitude": False,
        "Angle": False,
        "Normal vector": False,
        "Reflectance": False,
        "Line": False,
        "Group": False,
        "Time": False,
        "Deviation": False,
        "Color": True,
        "Intensity": True,
        "Distance": False,
        "Scanner": False,
    },
}


def _make_color_icon(rgb):
    """Return a 16×16 filled square QIcon for the given (r,g,b) tuple."""
    px = QPixmap(16, 16)
    px.fill(QColor(*rgb))
    return QIcon(px)


class LoadPointCloudDialog(QDialog):
    def __init__(self, filename, parent=None, disabled_attrs=None, initial_options=None):
        super().__init__(parent)
        self.setProperty("themeStyledDialog", True)
        self.filename = filename
        self.disabled_attrs = set(disabled_attrs or [])
        self._metadata = self._read_header_metadata(filename)
        self._options = self._merge_options(initial_options)
        self._app_window = parent

        self.setWindowTitle(f"Read points - {os.path.basename(filename)}")
        self.resize(760, 900)
        self.setMinimumSize(600, 520)
        self.refresh_theme()

        self._build_ui()
        self._connect_signals()
        self._apply_initial_state()

    # ------------------------------------------------------------------
    # Stylesheet / theme
    # ------------------------------------------------------------------

    def _build_stylesheet(self):
        c = ThemeColors
        return (
            get_dialog_stylesheet()
            + f"""
            QFrame#dialogSheet {{
                background-color: {c.get('bg_secondary')};
                border: 1px solid {c.get('border_light')};
                border-radius: 14px;
            }}
            QFrame#summaryStrip {{
                background-color: {c.get('bg_input')};
                border: 1px solid {c.get('border_light')};
                border-radius: 10px;
            }}
            QFrame#sheetDivider {{
                min-height: 1px;
                max-height: 1px;
                border: none;
                background-color: {c.get('border_light')};
            }}
            QLabel#metaLabel {{
                color: {c.get('text_secondary')};
                font-size: 8.5pt;
                font-weight: 600;
            }}
            QLabel#metaValue {{
                color: {c.get('text_primary')};
                font-size: 10.5pt;
                font-weight: 700;
            }}
            QLabel#sheetSectionTitle {{
                color: {c.get('text_primary')};
                font-size: 10pt;
                font-weight: 700;
            }}
            QLabel#sheetSectionHint {{
                color: {c.get('text_secondary')};
                font-size: 8.5pt;
            }}
            QLabel#fieldLabel {{
                color: {c.get('text_primary')};
                font-weight: 600;
                font-size: 9pt;
            }}
            QLineEdit#rangeField {{
                padding-top: 4px;
                padding-bottom: 4px;
            }}
            QListWidget#classListWidget::item {{
                padding: 2px 4px;
                color: {c.get('text_primary')};
                font-size: 8.5pt;
            }}
            QLabel#metaValue {{
                color: {c.get('text_primary')};
                font-size: 9pt;
                font-weight: 700;
            }}
            QLabel#sheetSectionTitle {{
                color: {c.get('text_primary')};
                font-size: 9pt;
                font-weight: 700;
            }}
            QLabel#sheetSectionHint {{
                color: {c.get('text_secondary')};
                font-size: 7.5pt;
            }}
            QLabel#fieldLabel {{
                color: {c.get('text_primary')};
                font-weight: 600;
                font-size: 8.5pt;
            }}
            QLineEdit#rangeField {{
                padding-top: 3px;
                padding-bottom: 3px;
            }}
            QListWidget#classListWidget {{
                background-color: {c.get('bg_input')};
                border: 1px solid {c.get('border_light')};
                border-radius: 6px;
                outline: none;
            }}
            QListWidget#classListWidget::item {{
                padding: 1px 4px;
                color: {c.get('text_primary')};
                font-size: 8pt;
            }}
            QListWidget#classListWidget::item:selected {{
                background-color: {c.get('accent', '#2979ff')};
                color: #ffffff;
            }}
            QListWidget#classListWidget::item:hover:!selected {{
                background-color: {c.get('bg_hover', '#2a2d3a')};
            }}
            """
        )

    def refresh_theme(self):
        self.setStyleSheet(self._build_stylesheet())

    # ------------------------------------------------------------------
    # Options helpers
    # ------------------------------------------------------------------

    def _merge_options(self, initial_options):
        merged = {
            key: value
            for key, value in DEFAULT_IMPORT_OPTIONS.items()
            if key != "attributes"
        }
        merged["attributes"] = dict(DEFAULT_IMPORT_OPTIONS["attributes"])
        merged["class_codes"] = list(DEFAULT_IMPORT_OPTIONS["class_codes"])
        if not initial_options:
            return merged

        for key, value in initial_options.items():
            if key == "attributes" and isinstance(value, dict):
                merged["attributes"].update(value)
            elif key == "class_codes":
                merged["class_codes"] = list(value) if value else []
            elif key == "class_code":
                # Back-compat: old single-code key → wrap in list
                try:
                    merged["class_codes"] = [int(value)]
                except (TypeError, ValueError):
                    pass
            else:
                merged[key] = value
        return merged

    # ------------------------------------------------------------------
    # Header metadata
    # ------------------------------------------------------------------

    def _read_header_metadata(self, filename):
        metadata = {
            "point_count": 0,
            "format": "Unknown",
            "coords_text": "",
            "crs_epsg": None,
        }

        try:
            if filename.lower().endswith((".las", ".laz")):
                with laspy.open(filename) as las_file:
                    header = las_file.header
                    metadata["point_count"] = header.point_count
                    ext = os.path.splitext(filename)[1].lower()
                    fmt_prefix = "LAZ" if ext == ".laz" else "LAS"
                    metadata["format"] = f"{fmt_prefix} {header.version.major}.{header.version.minor}"
                    metadata["coords_text"] = f"{header.mins}  ->  {header.maxs}"

                    for vlr in header.vlrs:
                        if isinstance(vlr, WktCoordinateSystemVlr):
                            try:
                                metadata["crs_epsg"] = CRS.from_wkt(vlr.string).to_epsg()
                            except Exception:
                                match = re.search(r"EPSG[\"']?,?\s?(\d+)", vlr.string, re.IGNORECASE)
                                if match:
                                    metadata["crs_epsg"] = int(match.group(1))
                            break

            if metadata["crs_epsg"] is None:
                prj_file = os.path.splitext(filename)[0] + ".prj"
                if os.path.exists(prj_file):
                    with open(prj_file, "r", encoding="utf-8", errors="ignore") as file_obj:
                        prj_text = file_obj.read().strip()
                    try:
                        metadata["crs_epsg"] = CRS.from_wkt(prj_text).to_epsg()
                    except Exception:
                        metadata["crs_epsg"] = None
        except Exception:
            pass

        return metadata

    # ------------------------------------------------------------------
    # UI build
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        # ── Header ────────────────────────────────────────────────────
        header_row = QHBoxLayout()
        header_row.setSpacing(10)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)

        title = QLabel("Reading block data")
        title.setObjectName("dialogTitle")
        title_col.addWidget(title)

        subtitle = QLabel(os.path.basename(self.filename))
        subtitle.setObjectName("dialogSubtitle")
        title_col.addWidget(subtitle)

        header_row.addLayout(title_col, 1)
        header_row.addStretch()
        header_row.addWidget(self._make_value_pill(self._metadata["format"]))
        header_row.addWidget(self._make_value_pill(f"{self._metadata['point_count']:,} points"))
        root.addLayout(header_row)

        # ── Summary strip ─────────────────────────────────────────────
        summary_strip = QFrame()
        summary_strip.setObjectName("summaryStrip")
        summary_layout = QGridLayout(summary_strip)
        summary_layout.setContentsMargins(10, 6, 10, 6)
        summary_layout.setHorizontalSpacing(14)
        summary_layout.setVerticalSpacing(2)

        summary_layout.addWidget(self._make_meta_block("Format", self._metadata["format"]), 0, 0)
        summary_layout.addWidget(
            self._make_meta_block("Points", f"{self._metadata['point_count']:,}"), 0, 1
        )
        summary_layout.addWidget(
            self._make_meta_block(
                "EPSG",
                str(self._metadata["crs_epsg"]) if self._metadata["crs_epsg"] else "Unknown",
            ),
            0, 2,
        )
        root.addWidget(summary_strip)

        # ── Main sheet ────────────────────────────────────────────────
        sheet = QFrame()
        sheet.setObjectName("dialogSheet")
        sheet_layout = QVBoxLayout(sheet)
        sheet_layout.setContentsMargins(12, 10, 12, 10)
        sheet_layout.setSpacing(8)

        sheet_layout.addLayout(
            self._make_section_header(
                "Source and coordinate setup",
                "Set cloud context, projection values, and the import filters you want to apply.",
            )
        )

        form_grid = QGridLayout()
        form_grid.setHorizontalSpacing(10)
        form_grid.setVerticalSpacing(8)

        from PySide6.QtWidgets import QComboBox as _QCB
        self.cloud_type = _QCB()
        self.cloud_type.addItems(["Airborne lidar", "Terrestrial lidar", "Mobile lidar", "Other"])

        self.system_box = _QCB()
        self.system_box.addItems(["Other", "Optech", "Leica", "Riegl"])

        self.format_box = self._make_readonly_field(self._metadata["format"])
        self.points_box = self._make_readonly_field(f"{self._metadata['point_count']:,}")
        self.input_proj = QLineEdit()
        self.active_proj = QLineEdit()
        self.coordinates_box = self._make_readonly_field(self._metadata["coords_text"])
        self.coordinates_box.setObjectName("rangeField")
        self.coordinates_box.setToolTip(self._metadata["coords_text"])

        form_grid.addWidget(self._make_field_label("Cloud type"), 0, 0)
        form_grid.addWidget(self.cloud_type, 0, 1)
        form_grid.addWidget(self._make_field_label("System"), 0, 2)
        form_grid.addWidget(self.system_box, 0, 3)

        form_grid.addWidget(self._make_field_label("Format"), 1, 0)
        form_grid.addWidget(self.format_box, 1, 1)
        form_grid.addWidget(self._make_field_label("Points"), 1, 2)
        form_grid.addWidget(self.points_box, 1, 3)

        form_grid.addWidget(self._make_field_label("Input projection (EPSG)"), 2, 0)
        form_grid.addWidget(self.input_proj, 2, 1)
        form_grid.addWidget(self._make_field_label("Active projection (EPSG)"), 2, 2)
        form_grid.addWidget(self.active_proj, 2, 3)

        form_grid.addWidget(self._make_field_label("Coordinate range"), 3, 0)
        form_grid.addWidget(self.coordinates_box, 3, 1, 1, 3)
        sheet_layout.addLayout(form_grid)

        sheet_layout.addWidget(self._make_divider())

        # ── Import filters ────────────────────────────────────────────
        options_grid = QGridLayout()
        options_grid.setHorizontalSpacing(10)
        options_grid.setVerticalSpacing(8)

        options_grid.addLayout(
            self._make_section_header(
                "Import filters", "Keep the load decision focused and predictable."
            ),
            0, 0, 1, 4,
        )

        self.only_every = QCheckBox("Load every Nth point")
        self.nth_spin = QSpinBox()
        self.nth_spin.setRange(1, 1000)
        self.nth_spin.setFixedWidth(96)

        self.only_class = QCheckBox("Load selected classifications")

        self.line_mode = _QCB()
        self.line_mode.addItems(["Use from file", "Generate"])

        self.scanner_mode = _QCB()
        self.scanner_mode.addItems(["File -- scanner byte", "Auto assign"])

        options_grid.addWidget(self.only_every, 1, 0)
        options_grid.addWidget(self.nth_spin, 1, 1)
        options_grid.addWidget(self._make_field_label("Line numbers"), 1, 2)
        options_grid.addWidget(self.line_mode, 1, 3)

        options_grid.addWidget(self.only_class, 2, 0)
        options_grid.addWidget(self._make_field_label("Scanner numbers"), 2, 2)
        options_grid.addWidget(self.scanner_mode, 2, 3)
        sheet_layout.addLayout(options_grid)

        # ── Class list (multi-select with color icons) ─────────────────
        self._class_list_wrapper = QWidget()
        cl_layout = QVBoxLayout(self._class_list_wrapper)
        cl_layout.setContentsMargins(0, 2, 0, 0)
        cl_layout.setSpacing(2)

        hint_lbl = QLabel("Ctrl+click to select multiple classes. Selected classes will be visible after load.")
        hint_lbl.setObjectName("sheetSectionHint")
        hint_lbl.setWordWrap(True)
        cl_layout.addWidget(hint_lbl)

        self.class_list = QListWidget()
        self.class_list.setObjectName("classListWidget")
        self.class_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.class_list.setIconSize(QSize(14, 14))
        self.class_list.setFixedHeight(100)
        cl_layout.addWidget(self.class_list)
        sheet_layout.addWidget(self._class_list_wrapper)

        sheet_layout.addWidget(self._make_divider())

        # ── Attributes ────────────────────────────────────────────────
        sheet_layout.addLayout(
            self._make_section_header(
                "Attributes to carry into the session",
                "Leave rarely used channels off unless the next tool really needs them.",
            )
        )

        attr_grid = QGridLayout()
        attr_grid.setHorizontalSpacing(20)
        attr_grid.setVerticalSpacing(6)

        self.attr_checks = {}
        attrs = [
            "XYZ", "Amplitude", "Angle", "Normal vector",
            "Reflectance", "Line", "Group", "Time",
            "Deviation", "Color", "Intensity", "Distance", "Scanner",
        ]

        for index, attr_name in enumerate(attrs):
            checkbox = QCheckBox(attr_name)
            checkbox.setProperty("primaryAttr", attr_name in {"XYZ", "Color", "Intensity"})
            if attr_name in self.disabled_attrs:
                checkbox.setChecked(False)
                checkbox.setEnabled(False)
            self.attr_checks[attr_name] = checkbox
            attr_grid.addWidget(checkbox, index // 3, index % 3)
        sheet_layout.addLayout(attr_grid)

        root.addWidget(sheet, 1)

        hint = QLabel(
            "Review the file metadata, adjust any sampling or class filter, then start the import."
        )
        hint.setObjectName("dialogCaption")
        hint.setWordWrap(True)
        root.addWidget(hint)

        # ── Buttons ───────────────────────────────────────────────────
        actions = QHBoxLayout()
        actions.addStretch()

        cancel_btn = QPushButton("Cancel")
        load_btn = QPushButton("Load Points")
        load_btn.setObjectName("primaryBtn")
        load_btn.setMinimumWidth(128)
        load_btn.setDefault(True)
        load_btn.setAutoDefault(True)

        actions.addWidget(cancel_btn)
        actions.addWidget(load_btn)
        root.addLayout(actions)

        cancel_btn.clicked.connect(self.reject)
        load_btn.clicked.connect(self.accept)

    # ------------------------------------------------------------------
    # Widget helpers
    # ------------------------------------------------------------------

    def _make_value_pill(self, text):
        label = QLabel(text)
        label.setObjectName("valuePill")
        label.setAlignment(Qt.AlignCenter)
        return label

    def _make_meta_block(self, label_text, value_text):
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)
        label = QLabel(label_text)
        label.setObjectName("metaLabel")
        layout.addWidget(label)
        value = QLabel(value_text)
        value.setObjectName("metaValue")
        value.setWordWrap(True)
        layout.addWidget(value)
        return wrapper

    def _make_section_header(self, title_text, hint_text):
        layout = QVBoxLayout()
        layout.setSpacing(1)
        title = QLabel(title_text)
        title.setObjectName("sheetSectionTitle")
        layout.addWidget(title)
        hint = QLabel(hint_text)
        hint.setObjectName("sheetSectionHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return layout

    def _make_field_label(self, text):
        label = QLabel(text)
        label.setObjectName("fieldLabel")
        return label

    def _make_readonly_field(self, text):
        field = QLineEdit(text)
        field.setReadOnly(True)
        field.setCursorPosition(0)
        return field

    def _make_divider(self):
        divider = QFrame()
        divider.setObjectName("sheetDivider")
        divider.setFrameShape(QFrame.HLine)
        divider.setFrameShadow(QFrame.Plain)
        return divider

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    def _connect_signals(self):
        self.only_every.toggled.connect(self.nth_spin.setEnabled)
        self.only_class.toggled.connect(self._on_only_class_toggled)

    # ------------------------------------------------------------------
    # PTC / palette helpers
    # ------------------------------------------------------------------

    def _ptc_is_loaded(self):
        app = self._app_window
        if app is None:
            print("[LoadDialog] _ptc_is_loaded: no app window → False")
            return False
        dlg = getattr(app, "display_mode_dialog", None)
        ptc_path = getattr(dlg, "current_ptc_path", None) if dlg is not None else None
        if ptc_path:
            print(f"[LoadDialog] _ptc_is_loaded: True (ptc_path={ptc_path})")
            return True
        vp = getattr(app, "view_palettes", None)
        if vp and isinstance(vp, dict):
            slot0 = vp.get(0)
            if isinstance(slot0, dict) and slot0:
                print(f"[LoadDialog] _ptc_is_loaded: True (view_palettes[0] has {len(slot0)} classes)")
                return True
        cp = getattr(app, "class_palette", None)
        if cp:
            print(f"[LoadDialog] _ptc_is_loaded: True (class_palette has {len(cp)} classes)")
            return True
        print(f"[LoadDialog] _ptc_is_loaded: False (dlg={dlg}, ptc_path={ptc_path}, vp={bool(vp)})")
        return False

    def _get_active_palette(self):
        """Return the current slot-0 palette dict, or None."""
        app = self._app_window
        if app is None:
            return None
        dlg = getattr(app, "display_mode_dialog", None)
        if dlg is not None and hasattr(dlg, "view_palettes"):
            slot0 = (dlg.view_palettes or {}).get(0)
            if isinstance(slot0, dict) and slot0:
                return slot0
        if isinstance(getattr(app, "view_palettes", None), dict):
            slot0 = app.view_palettes.get(0)
            if isinstance(slot0, dict) and slot0:
                return slot0
        cp = getattr(app, "class_palette", None)
        if cp:
            return cp
        return None

    def _populate_class_list(self, selected_codes=None):
        """
        Fill self.class_list from the active PTC palette with color swatches.
        Names come from 'lvl' first (TerraScan level name), then 'description',
        then the LAS standard CLASS_NAMES table, then 'Class <code>'.
        selected_codes: set/list of int codes to pre-select.
        """
        self.class_list.blockSignals(True)
        self.class_list.clear()

        palette = self._get_active_palette()
        selected_set = set(int(c) for c in (selected_codes or []))

        if palette:
            for code in sorted(palette.keys()):
                info = palette[code]
                # Name priority: lvl → description → CLASS_NAMES → "Class <n>"
                name = (
                    str(info.get("lvl") or "").strip()
                    or str(info.get("description") or "").strip()
                    or CLASS_NAMES.get(code, "")
                    or f"Class {code}"
                )
                rgb = info.get("color", (128, 128, 128))
                item = QListWidgetItem(_make_color_icon(rgb), f"{code} - {name}")
                item.setData(Qt.UserRole, code)
                self.class_list.addItem(item)
                if code in selected_set:
                    item.setSelected(True)
        else:
            # No PTC loaded — show LAS standard classes with a grey swatch
            for code in range(19):
                name = CLASS_NAMES.get(code, f"Class {code}")
                item = QListWidgetItem(_make_color_icon((128, 128, 128)), f"{code} - {name}")
                item.setData(Qt.UserRole, code)
                self.class_list.addItem(item)
                if code in selected_set:
                    item.setSelected(True)

        self.class_list.blockSignals(False)

    # ------------------------------------------------------------------
    # Checkbox handler
    # ------------------------------------------------------------------

    def _on_only_class_toggled(self, checked):
        ptc_loaded = self._ptc_is_loaded()
        print(f"[LoadDialog] _on_only_class_toggled: checked={checked}, ptc_loaded={ptc_loaded}")
        if checked and not ptc_loaded:
            self.only_class.blockSignals(True)
            self.only_class.setChecked(False)
            self.only_class.blockSignals(False)
            self._class_list_wrapper.setVisible(False)
            QMessageBox.warning(
                self,
                "PTC Not Loaded",
                "You must load a PTC (class table) file in the Display Mode first "
                "to use this feature. Open Display Mode → File → Open… and load a .ptc file.",
            )
            return
        if checked:
            # Refresh list from current PTC, restoring any prior selection
            current_codes = self._selected_class_codes()
            self._populate_class_list(selected_codes=current_codes)
        self._class_list_wrapper.setVisible(checked)

    # ------------------------------------------------------------------
    # Initial state
    # ------------------------------------------------------------------

    def _apply_initial_state(self):
        self.cloud_type.setCurrentText(self._options["cloud_type"])
        self.system_box.setCurrentText(self._options["system"])
        self.input_proj.setText(
            self._options["input_proj"]
            or (str(self._metadata["crs_epsg"]) if self._metadata["crs_epsg"] else "")
        )
        self.active_proj.setText(
            self._options["active_proj"]
            or (str(self._metadata["crs_epsg"]) if self._metadata["crs_epsg"] else "")
        )
        self.only_every.setChecked(bool(self._options["only_every"]))
        self.nth_spin.setValue(max(1, int(self._options["nth_point"])))

        init_codes = list(self._options.get("class_codes") or [])
        self._populate_class_list(selected_codes=init_codes)

        only_class_checked = bool(self._options["only_class"])
        self.only_class.setChecked(only_class_checked)
        self._class_list_wrapper.setVisible(only_class_checked)

        self.line_mode.setCurrentText(self._options["line_mode"])
        self.scanner_mode.setCurrentText(self._options["scanner_mode"])

        self.nth_spin.setEnabled(self.only_every.isChecked())

        for attr_name, checkbox in self.attr_checks.items():
            if attr_name in self.disabled_attrs:
                checkbox.setChecked(False)
                checkbox.setEnabled(False)
                continue
            checkbox.setChecked(bool(self._options["attributes"].get(attr_name, False)))

    # ------------------------------------------------------------------
    # Result
    # ------------------------------------------------------------------

    def _selected_class_codes(self):
        """Return sorted list of int codes currently selected in the list widget."""
        return sorted(
            int(item.data(Qt.UserRole))
            for item in self.class_list.selectedItems()
        )

    def get_import_options(self):
        _only = self.only_class.isChecked()
        _codes = self._selected_class_codes()
        print(f"[LoadDialog] get_import_options: only_class={_only}, class_codes={_codes}")
        return {
            "cloud_type": self.cloud_type.currentText(),
            "system": self.system_box.currentText(),
            "input_proj": self.input_proj.text().strip(),
            "active_proj": self.active_proj.text().strip(),
            "only_every": self.only_every.isChecked(),
            "nth_point": int(self.nth_spin.value()),
            "only_class": _only,
            "class_codes": _codes,
            "line_mode": self.line_mode.currentText(),
            "scanner_mode": self.scanner_mode.currentText(),
            "attributes": {
                name: checkbox.isChecked()
                for name, checkbox in self.attr_checks.items()
            },
        }
