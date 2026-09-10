# """
# PRJ Block Identifier Dialog
# Loads PRJ file and allows identification/highlighting of DXF blocks
# """

# from gui.minimize_chip import MinimizableDialogMixin
# import os
# from typing import Optional, Set
# from pathlib import Path
# from gui.theme_manager import (
#     get_dialog_stylesheet,
#     ThemeColors
# )

# from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton,
#                                QTableWidget, QTableWidgetItem, QFileDialog,
#                                QLabel, QMessageBox, QHeaderView, QWidget,
#                                QApplication, QLineEdit)
# from PySide6.QtCore import Qt, QTimer, QEvent, Signal, QRect
# from PySide6.QtGui import QColor, QPainter, QPen, QBrush
# from gui.popup_guard import InputPopupMixin


# def _point_in_polygon_xy(px, py, polygon_xy, eps: float = 1e-9):
#     """Return True when a point lies inside or on the edge of a polygon."""
#     pts = [(float(x), float(y)) for x, y in polygon_xy]
#     n = len(pts)
#     if n < 3:
#         return False

#     inside = False
#     j = n - 1
#     for i in range(n):
#         xi, yi = pts[i]
#         xj, yj = pts[j]

#         dx = xj - xi
#         dy = yj - yi
#         seg_len2 = dx * dx + dy * dy
#         if seg_len2 > 0.0:
#             cross = (px - xi) * dy - (py - yi) * dx
#             if abs(cross) <= eps * (abs(dx) + abs(dy) + 1.0):
#                 dot = (px - xi) * dx + (py - yi) * dy
#                 if -eps <= dot <= seg_len2 + eps:
#                     return True

#         if ((yi > py) != (yj > py)):
#             safe_dy = dy if abs(dy) > 1e-20 else (1e-20 if dy >= 0 else -1e-20)
#             x_hit = (dx * (py - yi) / safe_dy) + xi
#             if px <= x_hit + eps:
#                 inside = not inside
#         j = i
#     return inside

# class _SortableHeader(QHeaderView):
#     """
#     Custom horizontal header that draws a small sort-cycle button
#     on the LEFT side of each section label.
#     Clicking that zone cycles: none → asc → desc → none.
#     Columns 1 and 2 sort numerically; column 0 sorts alphabetically.
#     """
#     ICON_W = 18   # px width of the sort-button zone inside each section

#     sort_changed = Signal(int, str)   # col_index, "asc" | "desc" | "none"

#     def __init__(self, orientation, parent=None):
#         super().__init__(orientation, parent)
#         self._sort_states = {}        # col_index -> "none" | "asc" | "desc"
#         self.setSectionsClickable(True)
#         self.sectionClicked.connect(self._on_section_clicked)
#         self.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)

#     def _next_state(self, col):
#         current = self._sort_states.get(col, "none")
#         return {"none": "asc", "asc": "desc", "desc": "none"}[current]

#     def _on_section_clicked(self, logical_index):
#         new_state = self._next_state(logical_index)
#         # Clear other columns
#         self._sort_states = {logical_index: new_state}
#         self.sort_changed.emit(logical_index, new_state)
#         self.viewport().update()

#     # REPLACE the entire paintSection method
#     def paintSection(self, painter: QPainter, rect: QRect, logical_index: int):
#         painter.save()
#         super().paintSection(painter, rect, logical_index)
#         painter.restore()

#         state = self._sort_states.get(logical_index, "none")

#         icon_rect = QRect(rect.right() - self.ICON_W - 4, rect.y(), self.ICON_W, rect.height())
#         mid_x = icon_rect.center().x()
#         mid_y = icon_rect.center().y()

#         painter.save()
#         painter.setClipRect(rect)

#         # Theme-aware colours — pure white would be invisible on a light-theme header
#         from gui.theme_manager import ThemeColors as _TC
#         active_color = QColor(_TC.get("accent"))
#         pill_color = QColor(_TC.get("accent"))
#         pill_color.setAlpha(35)
#         dim_color = QColor(_TC.get("text_secondary"))

#         # Background pill when active
#         if state != "none":
#             painter.setBrush(QBrush(pill_color))
#             painter.setPen(Qt.NoPen)
#             painter.drawRoundedRect(icon_rect.adjusted(1, 3, -1, -3), 3, 3)

#         # Single arrow: dim ▼ when unsorted, bright ▲ for asc, bright ▼ for desc
#         if state == "asc":
#             color = active_color
#             # ▲  tip at top
#             pts = [(mid_x - 4, mid_y + 3), (mid_x, mid_y - 3), (mid_x + 4, mid_y + 3)]
#         elif state == "desc":
#             color = active_color
#             # ▼  tip at bottom
#             pts = [(mid_x - 4, mid_y - 3), (mid_x, mid_y + 3), (mid_x + 4, mid_y - 3)]
#         else:
#             color = dim_color
#             # dim ▼  (hint that column is sortable)
#             pts = [(mid_x - 4, mid_y - 3), (mid_x, mid_y + 3), (mid_x + 4, mid_y - 3)]

#         pen = QPen(color, 1.8)
#         pen.setCapStyle(Qt.RoundCap)
#         pen.setJoinStyle(Qt.RoundJoin)
#         painter.setPen(pen)
#         painter.drawLine(pts[0][0], pts[0][1], pts[1][0], pts[1][1])
#         painter.drawLine(pts[1][0], pts[1][1], pts[2][0], pts[2][1])

#         painter.restore()

# # ============================================================================
# # NEW: MINIMIZED CHIP WIDGET (Premium Bottom-Left Docking)
# # ============================================================================
# class _MinimizedPRJChip(QWidget):
#     """
#     A small floating chip shown at the bottom-left of the screen
#     when the PRJ identifier dialog is minimized.
#     """
#     restore_requested = Signal()

#     def __init__(self, title: str):
#         super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
#         self.setAttribute(Qt.WA_ShowWithoutActivating)
#         self.setAttribute(Qt.WA_DeleteOnClose)
#         self.setObjectName("minimizedPRJChip")
        
#         # Apply premium styling
#         from gui.theme_manager import ThemeColors as _TC
#         self.setStyleSheet(f"""
#             QWidget#minimizedPRJChip {{
#                 background-color: {_TC.get('bg_secondary')};
#                 border: 1px solid {_TC.get('accent')};
#                 border-radius: 6px;
#             }}
#             QLabel#chipLabel {{
#                 color: {_TC.get('accent')};
#                 font-size: 10px;
#                 font-weight: bold;
#                 padding: 2px 6px;
#             }}
#             QPushButton#chipRestoreBtn {{
#                 background-color: {_TC.get('bg_button')};
#                 border: none;
#                 border-radius: 4px;
#                 color: {_TC.get('accent')};
#                 font-size: 10px;
#                 font-weight: bold;
#                 padding: 2px 8px;
#                 min-width: 0;
#             }}
#             QPushButton#chipRestoreBtn:hover {{
#                 background-color: {_TC.get('bg_button_hover')};
#             }}
#         """)
#         self.setCursor(Qt.PointingHandCursor)

#         lay = QHBoxLayout(self)
#         lay.setContentsMargins(8, 6, 8, 6)
#         lay.setSpacing(10)

#         # Icon or prefix
#         icon_lbl = QLabel("🔍")
#         icon_lbl.setObjectName("chipLabel")
#         lay.addWidget(icon_lbl)

#         # Title
#         display_title = title if len(title) < 25 else title[:22] + "..."
#         title_lbl = QLabel(display_title)
#         title_lbl.setObjectName("chipLabel")
#         lay.addWidget(title_lbl)

#         # Restore button
#         restore_btn = QPushButton("▲ Restore")
#         restore_btn.setObjectName("chipRestoreBtn")
#         restore_btn.clicked.connect(self.restore_requested.emit)
#         lay.addWidget(restore_btn)

#         self.adjustSize()
#         self.place_bottom_left()

#     def place_bottom_left(self):
#         """Anchor to the bottom-left of the available screen geometry."""
#         screen = QApplication.primaryScreen().availableGeometry()
#         margin = 10
#         self.move(screen.left() + margin, screen.bottom() - self.height() - margin)

#     def mousePressEvent(self, event):
#         if event.button() == Qt.LeftButton:
#             self.restore_requested.emit()
#         super().mousePressEvent(event)


# class AddBlocksByBoundariesDialog(InputPopupMixin, QDialog):
#     """Naming dialog shown after drawing one or more boundary rectangles.

#     Mirrors MicroStation's "Add Blocks by Boundaries" dialog: a file prefix,
#     a numbering mode, and a starting number.  Block names are built as
#     f"{prefix}{number:06d}" — the same zero-padded style already used by
#     the existing PRJ blocks in this app.
#     """

#     def __init__(self, count: int, default_prefix: str = "", default_first_number: int = 1, parent=None):
#         super().__init__(parent)
#         from PySide6.QtWidgets import (
#             QFormLayout, QComboBox, QSpinBox, QDialogButtonBox
#         )

#         self.setWindowTitle("Add Blocks by Boundaries")
#         self.setModal(True)
#         self.setMinimumWidth(320)
#         self.setStyleSheet(get_dialog_stylesheet())

#         layout = QVBoxLayout(self)
#         info = QLabel(f"{count} boundary{'ies' if count != 1 else 'y'} drawn — name the new block(s):")
#         info.setWordWrap(True)
#         layout.addWidget(info)

#         form = QFormLayout()
#         self.prefix_edit = QLineEdit(default_prefix)
#         self.prefix_edit.setPlaceholderText("e.g. FORNACE")
#         form.addRow("File prefix:", self.prefix_edit)

#         self.numbering_combo = QComboBox()
#         self.numbering_combo.addItem("Selection order", "selection_order")
#         form.addRow("Numbering:", self.numbering_combo)

#         self.first_number_spin = QSpinBox()
#         self.first_number_spin.setRange(1, 999999)
#         self.first_number_spin.setValue(max(1, default_first_number))
#         form.addRow("First number:", self.first_number_spin)

#         layout.addLayout(form)

#         buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
#         buttons.accepted.connect(self.accept)
#         buttons.rejected.connect(self.reject)
#         layout.addWidget(buttons)

#     def get_values(self):
#         """Returns (prefix: str, first_number: int)."""
#         return self.prefix_edit.text().strip(), self.first_number_spin.value()


# # ── Project Information helpers ───────────────────────────────────────────────

# class _AttributesToStoreDialog(InputPopupMixin, QDialog):
#     """Sub-dialog opened by the 'Attributes...' button in Project Information."""

#     TIME_MODES = [
#         "GPS seconds-of-week",
#         "GPS week time",
#         "GPS adjusted standard time",
#         "Unix time",
#     ]

#     # (display label, prj key, default_checked, xyz_disabled)
#     _LEFT = [
#         ("Xyz",          "AttrXyz",    True,  True),
#         ("Class",        "AttrClass",  True,  False),
#         ("Line",         "AttrLine",   True,  False),
#         ("Echo",         "AttrEcho",   True,  False),
#         ("Distance",     "AttrDist",   False, False),
#         ("Amplitude",    "AttrAmpl",   False, False),
#         ("Group",        "AttrGroup",  False, False),
#         ("Image number", "AttrImgNum", False, False),
#         ("Normal vector","AttrNormal", False, False),
#         ("Intensity",    "AttrIntens", True,  False),
#     ]
#     _RIGHT = [
#         ("Scanner",        "AttrScanner",  True,  False),
#         ("Angle",          "AttrAngle",    True,  False),
#         ("Color",          "AttrColor",    True,  False),
#         ("Time",           "AttrTime",     True,  False),   # has dropdown
#         ("Echo length",    "AttrEchoLen",  False, False),
#         ("Echo normality", "AttrEchoNorm", False, False),
#         ("Echo position",  "AttrEchoPos",  False, False),
#         ("Reflectance",    "AttrRefl",     False, False),
#         ("Deviation",      "AttrDev",      False, False),
#         ("Reliability",    "AttrReli",     False, False),
#     ]

#     def __init__(self, fields: dict, parent=None):
#         super().__init__(parent)
#         from PySide6.QtWidgets import QDialogButtonBox, QCheckBox, QComboBox, QGridLayout, QGroupBox
#         self.setWindowTitle("Attributes to store")
#         self.setModal(True)
#         self.setMinimumWidth(460)
#         self.setStyleSheet(get_dialog_stylesheet())

#         root = QVBoxLayout(self)

#         grid = QGridLayout()
#         grid.setColumnStretch(0, 0)
#         grid.setColumnStretch(1, 0)
#         grid.setColumnStretch(2, 1)
#         grid.setHorizontalSpacing(24)
#         grid.setVerticalSpacing(4)

#         self._checks: dict = {}
#         self._time_combo: QComboBox = None

#         # Left column (col 0)
#         for row, (label, key, default, disabled) in enumerate(self._LEFT):
#             cb = QCheckBox(label)
#             cb.setChecked(bool(int(fields.get(key, "1" if default else "0"))))
#             if disabled:
#                 cb.setEnabled(False)
#             self._checks[key] = cb
#             grid.addWidget(cb, row, 0)

#         # Right column (col 1-2)
#         for row, (label, key, default, _disabled) in enumerate(self._RIGHT):
#             cb = QCheckBox(label)
#             cb.setChecked(bool(int(fields.get(key, "1" if default else "0"))))
#             self._checks[key] = cb
#             grid.addWidget(cb, row, 1)
#             if label == "Time":
#                 combo = QComboBox()
#                 for m in self.TIME_MODES:
#                     combo.addItem(m)
#                 saved_mode = int(fields.get("AttrTimeMode", "0"))
#                 combo.setCurrentIndex(max(0, min(saved_mode, len(self.TIME_MODES) - 1)))
#                 self._time_combo = combo
#                 grid.addWidget(combo, row, 2)

#         root.addLayout(grid)

#         buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
#         buttons.accepted.connect(self.accept)
#         buttons.rejected.connect(self.reject)
#         root.addWidget(buttons)

#     def get_fields(self) -> dict:
#         out = {}
#         for key, cb in self._checks.items():
#             out[key] = "1" if cb.isChecked() else "0"
#         if self._time_combo is not None:
#             out["AttrTimeMode"] = str(self._time_combo.currentIndex())
#         return out


# class ProjectInformationDialog(InputPopupMixin, QDialog):
#     """MicroStation-style Project Information dialog.

#     Used for both 'New project' (defaults) and 'Edit project information'
#     (pre-populated from existing PRJ header).  On OK, returns a fields dict
#     that the caller writes into the PRJ header.
#     """

#     CLOUD_TYPES = [
#         ("Airborne lidar",       "0"),
#         ("Mobile lidar",         "1"),
#         ("Aerial camera",        "2"),
#         ("Terrestrial scanner",  "3"),
#     ]
#     STORAGE_FORMATS = [
#         ("Fast binary",          "Fast binary"),
#         ("Scan binary 8 bit",    "Scan binary 8 bit"),
#         ("Scan binary 16 bit",   "Scan binary 16 bit"),
#         ("LAS 1.0",              "LAS 1.0"),
#         ("LAS 1.1",              "LAS 1.1"),
#         ("LAS 1.2",              "LAS 1.2"),
#         ("LAS 1.4",              "LAS 1.4"),
#         ("LAZ 0.1",              "LAZ 0.1"),
#         ("LAZ 1.1",              "LAZ 1.1"),
#         ("LAZ 1.2",              "LAZ 1.2"),
#         ("LAZ 1.3",              "LAZ 1.3"),
#         ("LAZ 1.4",              "LAZ 1.4"),
#         ("GeoTIFF",              "GeoTIFF"),
#     ]
#     DATA_IN = [
#         ("Project file directory", "0"),
#         ("Defined directory",      "1"),
#     ]
#     NEIGHBOUR_AREAS = [
#         ("Rounded corners",    "0"),
#         ("Rectangle corners",  "1"),
#     ]
#     BLOCK_NAMING = [
#         ("Number",     "0"),
#         ("Characters", "1"),
#     ]

#     _DEFAULTS = {
#         "CloudType":          "0",
#         "Description":        "",
#         "FirstPointId":       "1",
#         "Storage":            "Fast binary",
#         "RequireFileLocking": "0",
#         "Projection":         "",
#         "DataIn":             "0",
#         "Directory":          "",
#         "LoadClassFile":      "0",
#         "ClassFile":          "",
#         "LoadColorFile":      "0",
#         "ColorFile":          "",
#         "LoadTrajectories":   "0",
#         "TrajDirectory":      "",
#         "ReferenceProject":   "0",
#         "ReferenceFile":      "",
#         "NeighbourArea":      "0",
#         "BlockSize":          "1000",
#         "BlockPrefix":        "pt",
#         "GroupCount":         "1000000",
#         "BlockNaming":        "0",
#         # Attribute defaults
#         "AttrXyz":      "1", "AttrClass":    "1", "AttrLine":    "1",
#         "AttrEcho":     "1", "AttrDist":     "0", "AttrAmpl":    "0",
#         "AttrGroup":    "0", "AttrImgNum":   "0", "AttrNormal":  "0",
#         "AttrIntens":   "1", "AttrScanner":  "1", "AttrAngle":   "1",
#         "AttrColor":    "1", "AttrTime":     "1", "AttrTimeMode":"0",
#         "AttrEchoLen":  "0", "AttrEchoNorm": "0", "AttrEchoPos": "0",
#         "AttrRefl":     "0", "AttrDev":      "0", "AttrReli":    "0",
#     }

#     def __init__(self, fields: dict = None, parent=None):
#         super().__init__(parent)
#         from PySide6.QtWidgets import (
#             QDialogButtonBox, QCheckBox, QComboBox, QSpinBox,
#             QFormLayout, QScrollArea, QFrame, QGridLayout,
#         )
#         self.setWindowTitle("Project Information")
#         self.setModal(True)
#         self.setMinimumWidth(560)
#         self.setMinimumHeight(520)
#         self.setStyleSheet(get_dialog_stylesheet())

#         # Solid-background override for combo popups — prevents form content
#         # bleeding through the dropdown on Windows 11 with border-radius.
#         _bg = ThemeColors.get("bg_input")
#         _fg = ThemeColors.get("text_primary")
#         _sel = ThemeColors.get("dialog_selection")
#         _bdr = ThemeColors.get("border")
#         _combo_popup_ss = (
#             f"QComboBox QAbstractItemView {{"
#             f"  background-color: {_bg};"
#             f"  color: {_fg};"
#             f"  selection-background-color: {_sel};"
#             f"  selection-color: {_fg};"
#             f"  border: 1px solid {_bdr};"
#             f"  border-radius: 0px;"
#             f"  padding: 2px 0px;"
#             f"  outline: none;"
#             f"}}"
#         )

#         # Merge supplied fields with defaults
#         self._fields = dict(self._DEFAULTS)
#         if fields:
#             self._fields.update({k: v for k, v in fields.items() if v is not None})

#         # ── Root: scroll area (content) + fixed button bar ──────────────────
#         root = QVBoxLayout(self)
#         root.setContentsMargins(0, 0, 0, 0)
#         root.setSpacing(0)

#         scroll = QScrollArea()
#         scroll.setWidgetResizable(True)
#         scroll.setFrameShape(QFrame.NoFrame)
#         scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
#         scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

#         content = QWidget()
#         content.setStyleSheet("background: transparent;")
#         main = QVBoxLayout(content)
#         main.setContentsMargins(14, 12, 14, 10)
#         main.setSpacing(8)

#         def _rl(text):
#             lbl = QLabel(text)
#             lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
#             return lbl

#         # ── Section 1: Core project settings ───────────────────────────────
#         form1 = QFormLayout()
#         form1.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
#         form1.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
#         form1.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
#         form1.setSpacing(5)
#         form1.setContentsMargins(0, 0, 0, 0)

#         self._cloud_combo = QComboBox()
#         self._cloud_combo.setMaxVisibleItems(8)
#         self._cloud_combo.setStyleSheet(_combo_popup_ss)
#         for lbl, code in self.CLOUD_TYPES:
#             self._cloud_combo.addItem(lbl, code)
#         self._set_combo(self._cloud_combo, self._fields["CloudType"])
#         form1.addRow("Cloud type:", self._cloud_combo)

#         self._desc_edit = QLineEdit(self._fields["Description"])
#         form1.addRow("Description:", self._desc_edit)

#         self._first_pt_spin = QSpinBox()
#         self._first_pt_spin.setRange(1, 2_147_483_647)
#         try:
#             self._first_pt_spin.setValue(int(self._fields["FirstPointId"]))
#         except (ValueError, OverflowError):
#             self._first_pt_spin.setValue(1)
#         form1.addRow("First point id:", self._first_pt_spin)

#         self._storage_combo = QComboBox()
#         self._storage_combo.setMaxVisibleItems(8)
#         self._storage_combo.setStyleSheet(_combo_popup_ss)
#         for lbl, code in self.STORAGE_FORMATS:
#             self._storage_combo.addItem(lbl, code)
#         self._set_combo(self._storage_combo, self._fields["Storage"], by_data=True)
#         _stor_row = QHBoxLayout()
#         _stor_row.setSpacing(6)
#         _stor_row.addWidget(self._storage_combo, 1)
#         _attr_btn = QPushButton("Attributes...")
#         _attr_btn.clicked.connect(self._open_attributes)
#         _stor_row.addWidget(_attr_btn)
#         form1.addRow("Storage:", _stor_row)

#         _lock_row = QHBoxLayout()
#         _lock_row.setContentsMargins(0, 0, 0, 0)
#         self._lock_cb = QCheckBox("Require file locking")
#         self._lock_cb.setChecked(self._fields["RequireFileLocking"] == "1")
#         _lock_row.addWidget(self._lock_cb)
#         _lock_row.addStretch()
#         form1.addRow("", _lock_row)

#         self._proj_edit = QLineEdit(self._fields["Projection"])
#         _proj_row = QHBoxLayout()
#         _proj_row.setSpacing(6)
#         _proj_row.addWidget(self._proj_edit, 1)
#         _proj_browse = QPushButton("Browse...")
#         _proj_browse.clicked.connect(
#             lambda: self._browse_file(self._proj_edit, "Projection Files (*.prj *.txt);;All Files (*.*)")
#         )
#         _proj_row.addWidget(_proj_browse)
#         form1.addRow("Projection:", _proj_row)

#         main.addLayout(form1)
#         main.addWidget(self._make_sep())

#         # ── Section 2: Data in / Directory ─────────────────────────────────
#         form2 = QFormLayout()
#         form2.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
#         form2.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
#         form2.setSpacing(5)
#         form2.setContentsMargins(0, 0, 0, 0)

#         self._datain_combo = QComboBox()
#         self._datain_combo.setMaxVisibleItems(6)
#         self._datain_combo.setStyleSheet(_combo_popup_ss)
#         for lbl, code in self.DATA_IN:
#             self._datain_combo.addItem(lbl, code)
#         self._set_combo(self._datain_combo, self._fields["DataIn"])
#         form2.addRow("Data in:", self._datain_combo)

#         self._dir_edit = QLineEdit(self._fields["Directory"])
#         _dir_browse = QPushButton("Browse...")
#         _dir_browse.clicked.connect(lambda: self._browse_dir(self._dir_edit))
#         _dir_row = QHBoxLayout()
#         _dir_row.setSpacing(6)
#         _dir_row.addWidget(self._dir_edit, 1)
#         _dir_row.addWidget(_dir_browse)
#         form2.addRow("Directory:", _dir_row)

#         _dir_defined = self._fields["DataIn"] == "1"
#         self._dir_edit.setEnabled(_dir_defined)
#         _dir_browse.setEnabled(_dir_defined)

#         def _on_datain_changed():
#             _en = self._datain_combo.currentData() == "1"
#             self._dir_edit.setEnabled(_en)
#             _dir_browse.setEnabled(_en)

#         self._datain_combo.currentIndexChanged.connect(_on_datain_changed)

#         main.addLayout(form2)
#         main.addWidget(self._make_sep())

#         # ── Section 3: Optional file / directory rows ───────────────────────
#         opt_vbox = QVBoxLayout()
#         opt_vbox.setSpacing(2)
#         opt_vbox.setContentsMargins(0, 0, 0, 0)

#         self._class_cb, self._class_edit = self._make_opt_file_row(
#             opt_vbox, "Load class list automatically",
#             "LoadClassFile", "ClassFile",
#             "Class Files (*.ptc *.txt);;All Files (*.*)"
#         )
#         self._color_cb, self._color_edit = self._make_opt_file_row(
#             opt_vbox, "Load line colors automatically",
#             "LoadColorFile", "ColorFile",
#             "Color Files (*.clr *.txt);;All Files (*.*)"
#         )
#         self._traj_cb, self._traj_edit = self._make_opt_dir_row(
#             opt_vbox, "Load trajectories automatically",
#             "LoadTrajectories", "TrajDirectory"
#         )
#         self._ref_cb, self._ref_edit = self._make_opt_file_row(
#             opt_vbox, "Reference project exists",
#             "ReferenceProject", "ReferenceFile",
#             "PRJ Files (*.prj);;All Files (*.*)"
#         )

#         main.addLayout(opt_vbox)
#         main.addWidget(self._make_sep())

#         # ── Section 4: Default block values ────────────────────────────────
#         _blk_hdr = QLabel("Default block values")
#         _blk_hdr.setStyleSheet("font-weight: bold; font-size: 10pt;")
#         main.addWidget(_blk_hdr)

#         block_grid = QGridLayout()
#         block_grid.setHorizontalSpacing(8)
#         block_grid.setVerticalSpacing(5)
#         block_grid.setColumnStretch(1, 1)
#         block_grid.setColumnStretch(3, 1)
#         block_grid.setContentsMargins(0, 4, 0, 4)

#         # Row 0: Neighbour area | Block prefix
#         block_grid.addWidget(_rl("Neighbour area:"), 0, 0)
#         self._neighbour_combo = QComboBox()
#         self._neighbour_combo.setMaxVisibleItems(6)
#         self._neighbour_combo.setStyleSheet(_combo_popup_ss)
#         for lbl, code in self.NEIGHBOUR_AREAS:
#             self._neighbour_combo.addItem(lbl, code)
#         self._set_combo(self._neighbour_combo, self._fields["NeighbourArea"])
#         block_grid.addWidget(self._neighbour_combo, 0, 1)
#         block_grid.addWidget(_rl("Block prefix:"), 0, 2)
#         self._prefix_edit = QLineEdit(self._fields["BlockPrefix"])
#         self._prefix_edit.setMaximumWidth(80)
#         block_grid.addWidget(self._prefix_edit, 0, 3)

#         # Row 1: Block size | Block naming
#         block_grid.addWidget(_rl("Block size:"), 1, 0)
#         self._block_size_spin = QSpinBox()
#         self._block_size_spin.setRange(1, 999_999)
#         try:
#             self._block_size_spin.setValue(int(self._fields["BlockSize"]))
#         except (ValueError, OverflowError):
#             self._block_size_spin.setValue(1000)
#         _bsz_w = QWidget()
#         _bsz_h = QHBoxLayout(_bsz_w)
#         _bsz_h.setContentsMargins(0, 0, 0, 0)
#         _bsz_h.setSpacing(4)
#         _bsz_h.addWidget(self._block_size_spin)
#         _bsz_h.addWidget(QLabel("m"))
#         _bsz_h.addStretch()
#         block_grid.addWidget(_bsz_w, 1, 1)
#         block_grid.addWidget(_rl("Block naming:"), 1, 2)
#         self._naming_combo = QComboBox()
#         self._naming_combo.setMaxVisibleItems(6)
#         self._naming_combo.setStyleSheet(_combo_popup_ss)
#         for lbl, code in self.BLOCK_NAMING:
#             self._naming_combo.addItem(lbl, code)
#         self._set_combo(self._naming_combo, self._fields["BlockNaming"])
#         block_grid.addWidget(self._naming_combo, 1, 3)

#         # Row 2: Group count
#         block_grid.addWidget(_rl("Group count:"), 2, 0)
#         self._group_count_spin = QSpinBox()
#         self._group_count_spin.setRange(1, 2_147_483_647)
#         try:
#             self._group_count_spin.setValue(int(self._fields["GroupCount"]))
#         except (ValueError, OverflowError):
#             self._group_count_spin.setValue(1_000_000)
#         block_grid.addWidget(self._group_count_spin, 2, 1)

#         main.addLayout(block_grid)
#         main.addStretch()

#         scroll.setWidget(content)
#         root.addWidget(scroll, 1)

#         # ── Button bar (outside scroll — always visible) ────────────────────
#         root.addWidget(self._make_sep())
#         _btn_bar = QWidget()
#         _btn_h = QHBoxLayout(_btn_bar)
#         _btn_h.setContentsMargins(14, 6, 14, 10)
#         buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
#         buttons.accepted.connect(self.accept)
#         buttons.rejected.connect(self.reject)
#         _btn_h.addWidget(buttons)
#         root.addWidget(_btn_bar)

#     # ── Helpers ───────────────────────────────────────────────────────────────

#     @staticmethod
#     def _set_combo(combo, value: str, by_data: bool = False):
#         """Select the combo item whose data (by_data=True) or text (by_data=False) matches value."""
#         for i in range(combo.count()):
#             cmp = combo.itemData(i) if by_data else combo.itemText(i)
#             if cmp == value:
#                 combo.setCurrentIndex(i)
#                 return
#         # Fall back: search both data and text so a value stored either way still matches
#         for i in range(combo.count()):
#             if combo.itemData(i) == value or combo.itemText(i) == value:
#                 combo.setCurrentIndex(i)
#                 return
#         combo.setCurrentIndex(0)

#     def _make_sep(self) -> QWidget:
#         sep = QWidget()
#         sep.setFixedHeight(1)
#         sep.setStyleSheet(f"background:{ThemeColors.get('border')};")
#         return sep

#     def _make_opt_file_row(self, vbox, label, flag_key, path_key, file_filter):
#         from PySide6.QtWidgets import QCheckBox
#         cb = QCheckBox(label)
#         cb.setChecked(self._fields.get(flag_key, "0") == "1")
#         vbox.addWidget(cb)

#         path_w = QWidget()
#         path_h = QHBoxLayout(path_w)
#         path_h.setContentsMargins(20, 0, 0, 2)
#         path_h.setSpacing(6)
#         edit = QLineEdit(self._fields.get(path_key, ""))
#         edit.setEnabled(cb.isChecked())
#         path_h.addWidget(edit, 1)
#         btn = QPushButton("Browse...")
#         btn.setEnabled(cb.isChecked())
#         ff = file_filter

#         def _browse():
#             path, _ = QFileDialog.getOpenFileName(self, "Select File", edit.text() or "", ff)
#             if path:
#                 edit.setText(path)

#         btn.clicked.connect(_browse)
#         path_h.addWidget(btn)
#         vbox.addWidget(path_w)

#         def _toggle(checked):
#             edit.setEnabled(checked)
#             btn.setEnabled(checked)

#         cb.toggled.connect(_toggle)
#         return cb, edit

#     def _make_opt_dir_row(self, vbox, label, flag_key, path_key):
#         from PySide6.QtWidgets import QCheckBox
#         cb = QCheckBox(label)
#         cb.setChecked(self._fields.get(flag_key, "0") == "1")
#         vbox.addWidget(cb)

#         path_w = QWidget()
#         path_h = QHBoxLayout(path_w)
#         path_h.setContentsMargins(20, 0, 0, 2)
#         path_h.setSpacing(6)
#         edit = QLineEdit(self._fields.get(path_key, ""))
#         edit.setEnabled(cb.isChecked())
#         path_h.addWidget(edit, 1)
#         btn = QPushButton("Browse...")
#         btn.setEnabled(cb.isChecked())

#         def _browse():
#             d = QFileDialog.getExistingDirectory(self, "Select Directory", edit.text() or "")
#             if d:
#                 edit.setText(d)

#         btn.clicked.connect(_browse)
#         path_h.addWidget(btn)
#         vbox.addWidget(path_w)

#         def _toggle(checked):
#             edit.setEnabled(checked)
#             btn.setEnabled(checked)

#         cb.toggled.connect(_toggle)
#         return cb, edit

#     def _browse_file(self, edit, file_filter):
#         path, _ = QFileDialog.getOpenFileName(self, "Select File", edit.text() or "", file_filter)
#         if path:
#             edit.setText(path)

#     def _browse_dir(self, edit):
#         d = QFileDialog.getExistingDirectory(self, "Select Directory", edit.text() or "")
#         if d:
#             edit.setText(d)

#     def _open_attributes(self):
#         dlg = _AttributesToStoreDialog(self._fields, parent=self)
#         if dlg.exec() == QDialog.Accepted:
#             self._fields.update(dlg.get_fields())

#     def get_fields(self) -> dict:
#         """Return the complete fields dict after user edits."""
#         out = dict(self._fields)  # carries attribute flags updated via sub-dialog
#         out["CloudType"]         = self._cloud_combo.currentData() or "0"
#         out["Description"]       = self._desc_edit.text().strip()
#         out["FirstPointId"]      = str(self._first_pt_spin.value())
#         out["Storage"]           = self._storage_combo.currentData() or "Fast binary"
#         out["RequireFileLocking"]= "1" if self._lock_cb.isChecked() else "0"
#         out["Projection"]        = self._proj_edit.text().strip()
#         out["DataIn"]            = self._datain_combo.currentData() or "0"
#         out["Directory"]         = self._dir_edit.text().strip()
#         out["LoadClassFile"]     = "1" if self._class_cb.isChecked() else "0"
#         out["ClassFile"]         = self._class_edit.text().strip()
#         out["LoadColorFile"]     = "1" if self._color_cb.isChecked() else "0"
#         out["ColorFile"]         = self._color_edit.text().strip()
#         out["LoadTrajectories"]  = "1" if self._traj_cb.isChecked() else "0"
#         out["TrajDirectory"]     = self._traj_edit.text().strip()
#         out["ReferenceProject"]  = "1" if self._ref_cb.isChecked() else "0"
#         out["ReferenceFile"]     = self._ref_edit.text().strip()
#         out["NeighbourArea"]     = self._neighbour_combo.currentData() or "0"
#         out["BlockSize"]         = str(self._block_size_spin.value())
#         out["BlockPrefix"]       = self._prefix_edit.text().strip() or "pt"
#         out["GroupCount"]        = str(self._group_count_spin.value())
#         out["BlockNaming"]       = self._naming_combo.currentData() or "0"
#         return out

#     @staticmethod
#     def fields_to_header_text(fields: dict) -> str:
#         """Serialise fields dict → PRJ header text (before any Block sections)."""
#         # Field order matches MicroStation's layout for readability
#         ordered_keys = [
#             "Description", "CloudType", "FirstPointId", "Storage",
#             "RequireFileLocking", "Projection", "DataIn", "Directory",
#             "LoadClassFile", "ClassFile", "LoadColorFile", "ColorFile",
#             "LoadTrajectories", "TrajDirectory", "ReferenceProject", "ReferenceFile",
#             "NeighbourArea", "BlockSize", "BlockPrefix", "GroupCount", "BlockNaming",
#             "AttrXyz", "AttrClass", "AttrLine", "AttrEcho", "AttrDist",
#             "AttrAmpl", "AttrGroup", "AttrImgNum", "AttrNormal", "AttrIntens",
#             "AttrScanner", "AttrAngle", "AttrColor", "AttrTime", "AttrTimeMode",
#             "AttrEchoLen", "AttrEchoNorm", "AttrEchoPos", "AttrRefl",
#             "AttrDev", "AttrReli",
#         ]
#         lines = ["[TerraScan project]"]
#         written = set()
#         for key in ordered_keys:
#             if key in fields:
#                 lines.append(f"{key}={fields[key]}")
#                 written.add(key)
#         # Preserve any unknown keys from original (e.g. added by TerraScan itself)
#         for key, val in fields.items():
#             if key not in written:
#                 lines.append(f"{key}={val}")
#         lines.append("")  # trailing newline
#         return "\n".join(lines)

#     @staticmethod
#     def parse_header_fields(header_text: str) -> dict:
#         """Extract key=value pairs from PRJ header text."""
#         fields = {}
#         for line in header_text.splitlines():
#             line = line.strip()
#             if not line or line.startswith("[") or not ("=" in line):
#                 continue
#             key, _, val = line.partition("=")
#             fields[key.strip()] = val.strip()
#         return fields


# class PRJBlockIdentifierDialog(MinimizableDialogMixin, QDialog):
#     """Dialog to load PRJ file and identify DXF blocks"""

#     def __init__(self, app, parent=None):
#         if parent is None:
#             if isinstance(app, QWidget):
#                 parent = app
#             elif hasattr(app, "window") and isinstance(app.window, QWidget):
#                 parent = app.window

#         super().__init__(None, Qt.Window)
#         self.setAttribute(Qt.WA_QuitOnClose, False)

#         self.app = app
#         self.prj_data = []
#         self.current_dxf_path = None
#         self.current_prj_path = None
#         self.setProperty("themeStyledDialog", True)

#         self._init_minimize_state()

#         self.setWindowTitle("PRJ Block Identifier")
#         self.setMinimumSize(400, 300)
#         self.resize(820, 440)

#         self._missing_blocks_hidden = False
#         self._hide_missing_btn: Optional[QPushButton] = None
#         self._all_row_data = []
#         self._last_hide_missing_labels: Set[str] = set()

#         self._snt_filter_saved_visibility: dict = {}
#         self._snt_filter_overlay_actors: list = []

#         self._highlight_identify_id = 0
#         self._highlight_auto_clear_timer = None

#         # Fence-selection state
#         self._fence_active = False
#         self._fence_start_screen = None   # (vtk_x, vtk_y) on left-button down
#         self._fence_current_screen = None
#         self._fence_actor = None          # vtkActor2D rubberband overlay
#         self._fence_action = None            # assigned in setup_ui
#         self._fence_highlight_actors: list = []   # 3D boundary-polygon highlight actors
#         self._fence_mouse_grabbed = False    # True while vtk_widget.grabMouse() is active

#         # Add-by-Boundaries state
#         self._addb_active = False
#         self._addb_start_screen = None    # (vtk_x, vtk_y) on left-button down
#         self._addb_drag_actor = None      # vtkActor2D live rubberband overlay
#         self._addb_pending_rects: list = []   # world (minx, miny, maxx, maxy) tuples
#         self._addb_pending_snt: list = []     # (grid_name, pts_2d) from SNT polygon detection
#         self._addb_pending_actors: list = []  # persistent 3D outline actors (orange)
#         self._addb_action = None             # assigned in setup_ui
#         self._addb_mouse_grabbed = False     # True while vtk_widget.grabMouse() is active

#         # Flags used by GlobalShortcutFilter to suppress runtime tool shortcuts
#         # (alphabet keys etc.) while PRJ text entry is in progress.
#         self._addb_dialog_active = False     # Add Blocks by Boundaries popup open
#         self._prj_search_focus = False       # PRJ block-label search box has focus

#         # Edit Definition state (single-shot boundary redraw)
#         self._editdef_active = False
#         self._editdef_start_screen = None
#         self._editdef_drag_actor = None       # vtkActor2D live rubberband overlay
#         self._editdef_target_prj_idx = None   # which block is being edited
#         self._editdef_action = None              # assigned in setup_ui
#         self._editdef_mouse_grabbed = False   # True while vtk_widget.grabMouse() is active

#         self._apply_dark_theme()
#         self.setup_ui()
#         self.detect_current_dxf()
#         self.current_directory = None

#     def resizeEvent(self, event):
#         super().resizeEvent(event)
#         if not hasattr(self, '_sort_header') or self._sort_header is None:
#             return
#         total = event.size().width()
#         if total <= 0:
#             return
#         col0 = max(150, int(total * 0.45))
#         col1 = max(100, int(total * 0.28))
#         col2 = max(100, total - col0 - col1)
#         self.table.setColumnWidth(0, col0)
#         self.table.setColumnWidth(1, col1)
#         self.table.setColumnWidth(2, col2)

#     def _apply_dark_theme(self):
#         """Apply the shared application dialog theme, plus a premium polish
#         layer scoped to this dialog only (table, header card, search field)."""
#         self.setStyleSheet(get_dialog_stylesheet() + self._premium_polish_stylesheet())
#         self._update_hide_btn_style()

#     def _premium_polish_stylesheet(self) -> str:
#         """Extra styling layered on top of the shared dialog theme — elevates
#         the table, header card, and search field without touching the global
#         theme file (so other dialogs are unaffected)."""
#         c = ThemeColors
#         bg_secondary = c.get('bg_secondary')
#         bg_input = c.get('bg_input')
#         border_light = c.get('border_light')
#         text_primary = c.get('text_primary')
#         accent = c.get('accent')
#         bg_hover = c.get('bg_button_hover')
#         selection_bg = c.get('dialog_selection')
#         selection_text = c.get('text_primary') if c.is_light() else c.get('text_on_active')
#         return f"""
#             QTableWidget#prjBlockTable {{
#                 background-color: {bg_secondary};
#                 alternate-background-color: {bg_input};
#                 border: 1px solid {border_light};
#                 border-radius: 10px;
#                 gridline-color: transparent;
#                 selection-background-color: {selection_bg};
#                 padding: 2px;
#             }}
#             QTableWidget#prjBlockTable::item {{
#                 padding: 8px 10px;
#                 border: none;
#                 border-bottom: 1px solid {border_light};
#             }}
#             QTableWidget#prjBlockTable::item:selected {{
#                 background-color: {selection_bg};
#                 color: {selection_text};
#             }}
#             QTableWidget#prjBlockTable::item:hover {{
#                 background-color: {bg_hover};
#             }}
#             QTableWidget#prjBlockTable QHeaderView::section {{
#                 background-color: {bg_input};
#                 color: {text_primary};
#                 border: none;
#                 border-bottom: 2px solid {accent};
#                 padding: 9px 10px;
#                 font-weight: 700;
#                 font-size: 10.5pt;
#             }}
#             QFrame#prjInfoCard {{
#                 background-color: {bg_secondary};
#                 border: 1px solid {border_light};
#                 border-radius: 8px;
#             }}
#             QLabel#prjInfoLabel {{
#                 color: {text_primary};
#                 font-weight: 700;
#                 font-size: 10.5pt;
#                 background: transparent;
#             }}
#             QLineEdit#prjSearchBox {{
#                 background-color: {bg_input};
#                 border: 1px solid {border_light};
#                 border-radius: 13px;
#                 padding: 4px 14px;
#                 font-size: 10pt;
#             }}
#             QLineEdit#prjSearchBox:focus {{
#                 border: 1px solid {accent};
#             }}
#         """

#     def _style_prj_button(self, btn, object_name, width=140, height=50):
#         """Keep PRJ dialog buttons visually consistent without changing behavior."""
#         btn.setObjectName(object_name)
#         btn.setFixedSize(width, height)
#         btn.setStyleSheet(f"""
#         QPushButton#{object_name} {{
#             min-width: 0px;
#             max-width: {width}px;
#             min-height: 20px;
#             max-height: {height}px;
#             padding: 2px 6px;
#             font-size: 12px;
#             border-radius: 4px;
#         }}
#         """)

#     def setup_ui(self):
#         layout = QVBoxLayout(self)
#         layout.setContentsMargins(10, 8, 16, 16)
#         layout.setSpacing(10)

#         # ── Menu bar — mirrors MicroStation's "Project:" window (File / Block) ──
#         from PySide6.QtWidgets import QMenuBar
#         self.menu_bar = QMenuBar(self)
#         self.menu_bar.setNativeMenuBar(False)
#         bg = ThemeColors.get('bg_secondary')
#         bg_hover = ThemeColors.get('bg_button_hover')
#         text = ThemeColors.get('text_primary')
#         accent = ThemeColors.get('accent')
#         self.menu_bar.setStyleSheet(f"""
#             QMenuBar {{
#                 background-color: {bg};
#                 color: {text};
#                 border-bottom: 1px solid {accent};
#                 padding: 2px;
#             }}
#             QMenuBar::item {{
#                 background: transparent;
#                 padding: 4px 10px;
#                 border-radius: 4px;
#             }}
#             QMenuBar::item:selected {{
#                 background-color: {bg_hover};
#             }}
#             QMenu {{
#                 background-color: {bg};
#                 color: {text};
#                 border: 1px solid {accent};
#             }}
#             QMenu::item {{
#                 padding: 6px 24px 6px 16px;
#             }}
#             QMenu::item:selected {{
#                 background-color: {bg_hover};
#             }}
#             QMenu::separator {{
#                 height: 1px;
#                 background: {accent};
#                 margin: 4px 8px;
#             }}
#         """)
#         layout.setMenuBar(self.menu_bar)

#         file_menu = self.menu_bar.addMenu("File")
#         act_new = file_menu.addAction("New project...")
#         act_new.setToolTip("Create a brand-new, empty PRJ file and load it.")
#         act_new.triggered.connect(self._new_project)
#         act_open = file_menu.addAction("Open project...")
#         act_open.setToolTip("Load a PRJ file")
#         act_open.triggered.connect(self.load_prj_file)
#         act_remove = file_menu.addAction("Remove project")
#         act_remove.triggered.connect(self.remove_prj_file)
#         file_menu.addSeparator()

#         act_save = file_menu.addAction("Save project")
#         act_save.setToolTip(
#             "Commit pending edits to the real PRJ file.\n"
#             "Edits are buffered in a backend working copy until you save here.\n"
#             "A fresh backup snapshot (.prj.bak) is created first."
#         )
#         act_save.triggered.connect(self._save_project)

#         act_save_as = file_menu.addAction("Save project As...")
#         act_save_as.setToolTip("Copy the current PRJ to a new file and switch to it.")
#         act_save_as.triggered.connect(self._save_project_as)
#         file_menu.addSeparator()

#         act_edit_info = file_menu.addAction("Edit project information...")
#         act_edit_info.setToolTip("Edit the PRJ header text (metadata before any Block definitions).")
#         act_edit_info.triggered.connect(self._edit_project_information)
#         file_menu.addSeparator()

#         act_close = file_menu.addAction("Close")
#         act_close.triggered.connect(self.hide)

#         block_menu = self.menu_bar.addMenu("Block")

#         act_add_files = block_menu.addAction("Add using files...")
#         act_add_files.setToolTip(
#             "Add new block definition(s) from one or more LAZ/LAS files.\n"
#             "Each boundary polygon is auto-read from its file header."
#         )
#         act_add_files.triggered.connect(self.add_block_definition)

#         self._addb_action = block_menu.addAction("Add by boundaries...")
#         self._addb_action.setCheckable(True)
#         self._addb_action.setToolTip(
#             "Draw a rectangle on the viewport. If SNT blocks are loaded, all SNT "
#             "block polygons inside the rectangle are detected and added as individual "
#             "blocks. Without SNT data, the rectangle itself becomes one new block."
#         )
#         self._addb_action.toggled.connect(self._toggle_addb_mode)

#         act_add_snt = block_menu.addAction("Add all SNT blocks...")
#         act_add_snt.setToolTip(
#             "Add every loaded SNT block polygon to this PRJ in one step.\n"
#             "Skips any blocks already present. Matches each SNT polygon exactly."
#         )
#         act_add_snt.triggered.connect(self._add_blocks_from_snt)

#         self._editdef_action = block_menu.addAction("Edit definition...")
#         self._editdef_action.setCheckable(True)
#         self._editdef_action.setToolTip(
#             "Select exactly ONE block, then redraw its boundary on the viewport."
#         )
#         self._editdef_action.toggled.connect(self._toggle_editdef_mode)

#         act_rename = block_menu.addAction("Rename definition...")
#         act_rename.triggered.connect(self.rename_block_definition)

#         act_delete = block_menu.addAction("Delete definition")
#         act_delete.triggered.connect(self.delete_block_definition)

#         act_export = block_menu.addAction("Export Sub-PRJ...")
#         act_export.triggered.connect(self.export_selected_blocks_prj)

#         act_merge = block_menu.addAction("Merge blocks")
#         act_merge.setToolTip("Combine 2+ selected blocks into one (union of their boundaries).")
#         act_merge.triggered.connect(self._merge_selected_blocks)

#         act_lock = block_menu.addAction("Lock selected")
#         act_lock.setToolTip(
#             "Lock the selected block(s) against Delete / Rename / Edit Definition\n"
#             "for this app session. Not written to the PRJ file."
#         )
#         act_lock.triggered.connect(self._lock_selected_blocks)

#         act_unlock = block_menu.addAction("Release lock")
#         act_unlock.triggered.connect(self._release_selected_locks)

#         block_menu.addSeparator()

#         self._fence_action = block_menu.addAction("Select by Fence")
#         self._fence_action.setCheckable(True)
#         self._fence_action.setToolTip(
#             "Drag a rectangle on the viewport to spatially select PRJ blocks."
#         )
#         self._fence_action.toggled.connect(self._toggle_fence_mode)

#         act_select_all = block_menu.addAction("Select all")
#         act_select_all.triggered.connect(lambda: self.table.selectAll())

#         act_deselect_all = block_menu.addAction("Deselect all")
#         act_deselect_all.triggered.connect(self._deselect_all)

#         block_menu.addSeparator()

#         act_identify = block_menu.addAction("Identify")
#         act_identify.triggered.connect(self.identify_selected_block)

#         act_hide_missing = block_menu.addAction("Hide Missing Blocks")
#         act_hide_missing.triggered.connect(self._toggle_missing_blocks_visibility)

#         # ── Search row ───────────────────────────────────────────────────────
#         top_row = QHBoxLayout()
#         top_row.setContentsMargins(0, 0, 0, 0)
#         top_row.setSpacing(8)

#         self.search_box = QLineEdit()
#         self.search_box.setObjectName("prjSearchBox")
#         self.search_box.setProperty("nakshaStrictTextInput", True)
#         self.search_box.setPlaceholderText("🔍  Search block label…")
#         self.search_box.setClearButtonEnabled(True)
#         self.search_box.textChanged.connect(self._on_search_changed)
#         self.search_box.installEventFilter(self)

#         self.search_box.setFixedHeight(28)
#         self.search_box.setMinimumWidth(260)
#         self.search_box.setMaximumWidth(360)
#         top_row.addWidget(self.search_box, 0, Qt.AlignLeft | Qt.AlignTop)

#         top_row.addStretch()
#         layout.addLayout(top_row)

#         from PySide6.QtWidgets import QFrame
#         info_card = QFrame()
#         info_card.setObjectName("prjInfoCard")
#         info_layout = QHBoxLayout(info_card)
#         info_layout.setContentsMargins(12, 8, 12, 8)
#         self.dxf_label = QLabel("📁  No PRJ loaded")
#         self.dxf_label.setObjectName("prjInfoLabel")
#         info_layout.addWidget(self.dxf_label)
#         info_layout.addStretch()
#         layout.addWidget(info_card)

#         self.table = QTableWidget()
#         self.table.setObjectName("prjBlockTable")
#         self.table.setColumnCount(3)
#         self.table.setHorizontalHeaderLabels(["Block Label", "Total Points", "Area (m²)"])

#         self._sort_header = _SortableHeader(Qt.Horizontal, self.table)
#         self._sort_header.sort_changed.connect(self._on_sort_changed)
#         self.table.setHorizontalHeader(self._sort_header)
#         self.table.setHorizontalHeaderLabels(["Block Label", "Total Points", "Area (m²)"])

#         self._sort_header.setSectionResizeMode(0, QHeaderView.Interactive)
#         self._sort_header.setSectionResizeMode(1, QHeaderView.Interactive)
#         self._sort_header.setSectionResizeMode(2, QHeaderView.Interactive)
#         self.table.setColumnWidth(0, 300)
#         self.table.setColumnWidth(1, 160)
#         self.table.setColumnWidth(2, 140)
#         self._sort_header.setStretchLastSection(True)

#         self.table.setSelectionBehavior(QTableWidget.SelectRows)
#         self.table.setSelectionMode(QTableWidget.ExtendedSelection)
#         self.table.setAlternatingRowColors(True)
#         self.table.setMouseTracking(True)
#         self.table.setToolTip("")
#         self.table.setSortingEnabled(False)
#         self.table.itemDoubleClicked.connect(self.identify_selected_block)
#         self.table.itemClicked.connect(self.on_table_item_clicked)
#         self.table.setContextMenuPolicy(Qt.CustomContextMenu)
#         self.table.customContextMenuRequested.connect(self._on_table_context_menu)
#         layout.addWidget(self.table)

#         self._all_row_data = []

#         btn_layout = QHBoxLayout()
#         btn_layout.addStretch()

#         identify_btn = QPushButton("🔍  Identify Selected Block")
#         self._style_prj_button(identify_btn, "primaryBtn", 160, 60)
#         identify_btn.clicked.connect(self.identify_selected_block)
#         btn_layout.addWidget(identify_btn)

#         self._hide_missing_btn = QPushButton("👁  Hide Missing Blocks")
#         self._style_prj_button(self._hide_missing_btn, "secondaryBtn", 160, 60)
#         self._hide_missing_btn.setToolTip(
#             "Toggle visibility of SNT block rectangles whose LAZ file was not found"
#         )
#         self._hide_missing_btn.clicked.connect(self._toggle_missing_blocks_visibility)
#         btn_layout.addWidget(self._hide_missing_btn)

#         delete_def_btn = QPushButton("🗑  Delete Definition")
#         self._style_prj_button(delete_def_btn, "dangerBtn", 160, 60)
#         delete_def_btn.setToolTip(
#             "Permanently remove the selected block definition(s) from the PRJ file.\n"
#             "The LAZ/LAS files on disk are NOT deleted.\n"
#             "Changes are buffered in a backend working copy and only written to the\n"
#             "real PRJ when you press 'Save project' (which snapshots .prj.bak first)."
#         )
#         delete_def_btn.clicked.connect(self.delete_block_definition)
#         btn_layout.addWidget(delete_def_btn)

#         remove_btn = QPushButton("📂  Remove File")
#         self._style_prj_button(remove_btn, "dangerBtn", 160, 60)
#         remove_btn.clicked.connect(self.remove_prj_file)
#         btn_layout.addWidget(remove_btn)

#         close_btn = QPushButton("✕  Close")
#         self._style_prj_button(close_btn, "closeBtn", 130, 60)
#         close_btn.clicked.connect(self.hide)
#         btn_layout.addWidget(close_btn)

#         layout.addLayout(btn_layout)
#     def _deselect_all(self):
#         """Clear table selection and fence-selection boundary highlights."""
#         self.table.clearSelection()
#         self._fence_highlight_clear()

#     # ── Lock / Release lock ──────────────────────────────────────────────────
#     # Lock state is session-only (not written to the .prj file) — the exact
#     # TerraScan format for a lock flag is unverified, and inventing one risks
#     # breaking compatibility if the file is later opened in real MicroStation.
#     # It still fully blocks Delete / Rename / Edit Definition in this app.

#     def _resolve_selected_prj_indices(self):
#         """Return the list of valid prj_idx values for the current table selection."""
#         indices = []
#         for idx in self.table.selectionModel().selectedRows():
#             item = self.table.item(idx.row(), 0)
#             if item is None:
#                 continue
#             prj_idx = item.data(Qt.UserRole)
#             if prj_idx is None:
#                 continue
#             try:
#                 prj_idx = int(prj_idx)
#             except (TypeError, ValueError):
#                 continue
#             if 0 <= prj_idx < len(self.prj_data) and prj_idx not in indices:
#                 indices.append(prj_idx)
#         return indices

#     def _lock_selected_blocks(self):
#         """Lock the selected block(s) against Delete / Rename / Edit Definition."""
#         indices = self._resolve_selected_prj_indices()
#         if not indices:
#             QMessageBox.warning(self, "No Selection", "Please select one or more blocks to lock.")
#             return

#         for prj_idx in indices:
#             self.prj_data[prj_idx]["locked"] = True

#         self._repopulate_table(self._all_row_data)
#         self._on_search_changed(self.search_box.text())

#         n = len(indices)
#         if hasattr(self.app, "statusBar"):
#             self.app.statusBar().showMessage(f"Locked {n} block{'s' if n != 1 else ''}", 3000)

#     def _release_selected_locks(self):
#         """Release the lock on the selected block(s)."""
#         indices = self._resolve_selected_prj_indices()
#         if not indices:
#             QMessageBox.warning(self, "No Selection", "Please select one or more blocks to unlock.")
#             return

#         n_unlocked = 0
#         for prj_idx in indices:
#             if self.prj_data[prj_idx].get("locked", False):
#                 self.prj_data[prj_idx]["locked"] = False
#                 n_unlocked += 1

#         self._repopulate_table(self._all_row_data)
#         self._on_search_changed(self.search_box.text())

#         if hasattr(self.app, "statusBar"):
#             self.app.statusBar().showMessage(
#                 f"Released lock on {n_unlocked} block{'s' if n_unlocked != 1 else ''}", 3000
#             )

#     # ── Merge Blocks ─────────────────────────────────────────────────────────

#     def _merge_selected_blocks(self):
#         """Combine 2+ selected blocks into one via shapely union of their boundaries.

#         The first selected block (by table order) keeps its label/filename and
#         absorbs the others; absorbed blocks are removed from the .prj file.
#         If the union isn't a single contiguous polygon (blocks don't touch or
#         overlap), falls back to the convex hull after explicit confirmation.
#         """
#         if not self.current_prj_path:
#             QMessageBox.warning(self, "No PRJ Loaded", "Please load a PRJ file first.")
#             return
#         if not os.path.isfile(self.current_prj_path):
#             QMessageBox.critical(
#                 self, "PRJ File Not Found",
#                 f"The PRJ file no longer exists on disk:\n\n{self.current_prj_path}"
#             )
#             return

#         indices = self._resolve_selected_prj_indices()
#         if len(indices) < 2:
#             QMessageBox.warning(
#                 self, "Select Two or More Blocks",
#                 "Please select at least 2 blocks in the table to merge."
#             )
#             return

#         locked = [
#             self.prj_data[i].get("label", "?") for i in indices
#             if self.prj_data[i].get("locked", False)
#         ]
#         if locked:
#             QMessageBox.warning(
#                 self, "Block(s) Locked",
#                 "The following selected block(s) are locked and cannot be merged:\n\n"
#                 + "\n".join(f"• {lbl}" for lbl in locked)
#                 + "\n\nUse Block → Release lock first."
#             )
#             return

#         blocks = [self.prj_data[i] for i in indices]
#         labels = [b.get("label", "?") for b in blocks]

#         # ── Build merged geometry ────────────────────────────────────────────
#         try:
#             from shapely.geometry import Polygon
#             from shapely.ops import unary_union
#             polys = []
#             for b in blocks:
#                 coords = b.get("boundary_coords", [])
#                 if len(coords) >= 3:
#                     polys.append(Polygon(coords))
#             if len(polys) < 2:
#                 QMessageBox.warning(
#                     self, "Cannot Merge",
#                     "At least 2 of the selected blocks must have a valid (3+ point) boundary."
#                 )
#                 return
#             merged = unary_union(polys)
#         except Exception as exc:
#             QMessageBox.critical(self, "Merge Failed", f"Could not compute merged geometry:\n\nError: {exc}")
#             return

#         used_hull_fallback = False
#         if merged.geom_type != "Polygon":
#             reply = QMessageBox.question(
#                 self, "Blocks Don't Overlap",
#                 "The selected blocks don't touch or overlap, so their union isn't\n"
#                 "a single contiguous shape.\n\n"
#                 "Use the convex hull (bounding shape around all of them) instead?",
#                 QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
#             )
#             if reply != QMessageBox.Yes:
#                 return
#             merged = merged.convex_hull
#             used_hull_fallback = True

#         # Guard: convex_hull of degenerate input (collinear/coincident points) can
#         # collapse to a Point or LineString, neither of which has .exterior — would
#         # otherwise raise an uncaught AttributeError and crash the dialog.
#         if merged.is_empty or merged.geom_type != "Polygon":
#             QMessageBox.critical(
#                 self, "Merge Failed",
#                 "The merged geometry is degenerate (collapses to a point or line) "
#                 "and cannot be used as a block boundary."
#             )
#             return

#         new_coords = list(merged.exterior.coords)[:-1]   # drop closing duplicate
#         if len(new_coords) < 3:
#             QMessageBox.critical(self, "Merge Failed", "The merged geometry is degenerate.")
#             return

#         old_total_area = sum(self._shoelace_area(b.get("boundary_coords", [])) for b in blocks)
#         new_area = merged.area

#         primary_idx = indices[0]
#         primary_label = blocks[0].get("label", "?")
#         absorbed_labels = labels[1:]

#         shape_note = " (convex hull)" if used_hull_fallback else ""
#         reply = QMessageBox.question(
#             self, "Confirm Merge",
#             (
#                 f"Merge {len(indices)} blocks into '{primary_label}'{shape_note}?\n\n"
#                 "Absorbed (will be deleted):\n"
#                 + "\n".join(f"  • {lbl}" for lbl in absorbed_labels) + "\n\n"
#                 f"Old combined area: {old_total_area:.2f} m²\n"
#                 f"New merged area:   {new_area:.2f} m²\n\n"
#                 "Changes are buffered in a backend working copy and only written\n"
#                 "to the real PRJ when you press 'Save project'. This cannot be undone."
#             ),
#             QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
#         )
#         if reply != QMessageBox.Yes:
#             return

#         # ── Read, transform, write (single atomic write) ──────────────────────
#         try:
#             with open(self.current_prj_path, "r", encoding="utf-8") as fh:
#                 content = fh.read()
#         except Exception as exc:
#             QMessageBox.critical(self, "Read Error", f"Could not read the PRJ file:\n\nError: {exc}")
#             return

#         try:
#             content = self._replace_block_boundary_in_prj_text(content, primary_label, new_coords)
#             absorbed_upper = {lbl.strip().upper() for lbl in absorbed_labels}
#             content = self._remove_blocks_from_prj_text(content, absorbed_upper)
#         except Exception as exc:
#             QMessageBox.critical(self, "Parse Error", f"Failed to build the merged PRJ content:\n\nError: {exc}")
#             return

#         # ── Write to backend working copy (real .prj untouched until Save) ────
#         try:
#             self._write_prj_to_working(content)
#             print(f"  ✅ Merged {len(indices)} blocks into '{primary_label}' (backend working copy)")
#         except Exception as exc:
#             QMessageBox.critical(
#                 self, "Write Error",
#                 f"Failed to buffer the merged PRJ changes:\n\nError: {exc}\n\n"
#                 "The original file has NOT been modified."
#             )
#             return

#         # ── Update in-memory state ─────────────────────────────────────────────
#         try:
#             cx, cy = merged.centroid.x, merged.centroid.y
#             self.prj_data[primary_idx]["boundary_coords"] = new_coords
#             self.prj_data[primary_idx]["easting"] = f"{cx:.2f}"
#             self.prj_data[primary_idx]["northing"] = f"{cy:.2f}"

#             absorbed_upper = {lbl.strip().upper() for lbl in absorbed_labels}
#             primary_upper = primary_label.strip().upper()

#             new_prj_data = [
#                 entry for entry in self.prj_data
#                 if entry.get("label", "").strip().upper() not in absorbed_upper
#             ]
#             kept_rows = [
#                 row for row in self._all_row_data
#                 if row[0].strip().upper() not in absorbed_upper
#             ]
#             new_idx_map = {
#                 entry["label"].strip().upper(): i
#                 for i, entry in enumerate(new_prj_data)
#             }
#             reindexed = []
#             for row in kept_rows:
#                 norm = row[0].strip().upper()
#                 new_idx = new_idx_map.get(norm, 0)
#                 if norm == primary_upper:
#                     reindexed.append((row[0], row[1], f"{new_area:.2f}", row[3], row[4], new_idx))
#                 else:
#                     reindexed.append((row[0], row[1], row[2], row[3], row[4], new_idx))

#             self.prj_data = new_prj_data
#             self._all_row_data = reindexed

#             self._sort_header._sort_states = {}
#             self._sort_header.viewport().update()
#             self._repopulate_table(self._all_row_data)
#             self._on_search_changed(self.search_box.text())
#             print(f"  ✅ In-memory state updated: {len(self.prj_data)} block(s) remain")
#         except Exception as exc:
#             print(f"  ⚠️ Failed to refresh table after merge: {exc}")
#             QMessageBox.warning(
#                 self, "Display Refresh Issue",
#                 "The blocks were merged on disk successfully, but the table\n"
#                 "could not refresh automatically.\n\n"
#                 "Please close and reopen the PRJ Block Identifier to see the change."
#             )
#             return

#         if hasattr(self.app, "statusBar"):
#             self.app.statusBar().showMessage(
#                 f"Merged {len(indices)} blocks into '{primary_label}' — area: {new_area:.2f} m²", 6000
#             )

#     # ── Search ────────────────────────────────────────────────────────────────

#     def _on_search_changed(self, text: str):
#         query = text.strip().lower()
#         for row in range(self.table.rowCount()):
#             item = self.table.item(row, 0)
#             label = item.text().lower() if item else ""
#             self.table.setRowHidden(row, bool(query and query not in label))

#     def _prj_search_focus_set(self, on: bool):
#         # Guard for GlobalShortcutFilter — keep alphabet keys for search entry.
#         self._prj_search_focus = bool(on)
#         from gui.popup_guard import set_input_popup_open
#         set_input_popup_open(self.app, bool(on))


#     # ── Sort ──────────────────────────────────────────────────────────────────

#     # REPLACE _on_sort_changed
#     def _on_sort_changed(self, col: int, state: str):
#         if not self._all_row_data:
#             return

#         if state == "none":
#             ordered = list(self._all_row_data)
#         else:
#             reverse = (state == "desc")

#             def _sort_key(row_tuple):
#                 val = row_tuple[col]          # col 0=label, 1=points, 2=area
#                 if col in (1, 2):
#                     try:
#                         return float(val.replace(",", ""))
#                     except (ValueError, AttributeError):
#                         return float("inf") * (-1 if reverse else 1)
#                 return val.lower()

#             ordered = sorted(self._all_row_data, key=_sort_key, reverse=reverse)

#         self._repopulate_table(ordered)
#         self._on_search_changed(self.search_box.text())

#     # REPLACE _repopulate_table
#     def _repopulate_table(self, rows):
#         """Write rows (sorted/filtered) back into QTableWidget, preserving prj_data links."""
#         self.table.setSortingEnabled(False)
#         self.table.setRowCount(len(rows))
#         for r, (c0, c1, c2, col1_fg, col2_fg, prj_idx) in enumerate(rows):
#             label_item = QTableWidgetItem(c0)
#             label_item.setData(Qt.UserRole, prj_idx)   # ← critical: keep index alive

#             # Lock visual indicator — label TEXT is left untouched (other code
#             # matches on it) so we only style colour/font/tooltip here.
#             is_locked = (
#                 prj_idx is not None and 0 <= prj_idx < len(self.prj_data)
#                 and self.prj_data[prj_idx].get("locked", False)
#             )
#             if is_locked:
#                 label_item.setForeground(QColor(ThemeColors.get("warning")))
#                 f = label_item.font()
#                 f.setItalic(True)
#                 label_item.setFont(f)
#                 label_item.setToolTip("🔒 Locked — Delete / Rename / Edit Definition disabled")

#             self.table.setItem(r, 0, label_item)

#             item1 = QTableWidgetItem(c1)
#             item2 = QTableWidgetItem(c2)
#             if col1_fg:
#                 item1.setForeground(col1_fg)
#             if col2_fg:
#                 item2.setForeground(col2_fg)
#             self.table.setItem(r, 1, item1)
#             self.table.setItem(r, 2, item2)

#     def _safe_render_main_view(self):
#         """Render main VTK view only when the widget/window is still usable."""
#         try:
#             vtk_widget = getattr(self.app, 'vtk_widget', None)
#             if vtk_widget is None:
#                 return
#             if hasattr(vtk_widget, 'isVisible') and not vtk_widget.isVisible():
#                 return
#             window = vtk_widget.window() if hasattr(vtk_widget, 'window') else None
#             if window is not None and hasattr(window, 'isVisible') and not window.isVisible():
#                 return
#             render_window = vtk_widget.GetRenderWindow()
#             if render_window is not None:
#                 render_window.Render()
#         except Exception:
#             pass

#     def detect_current_dxf(self):
#         """Detect currently loaded DXF file"""
#         try:
#             if hasattr(self.app, 'dxf_layers') and self.app.dxf_layers:
#                 # Get first DXF path from loaded layers
#                 first_layer = next(iter(self.app.dxf_layers.values()))
#                 if hasattr(first_layer, 'dxf_path'):
#                     self.current_dxf_path = first_layer.dxf_path
#                     dxf_name = os.path.basename(self.current_dxf_path)
#                     self.dxf_label.setText(f"📁  DXF: {dxf_name}")
#                     self.auto_load_prj()
#                     return
            
            
#         except Exception as e:
#             print(f"⚠️ DXF detection failed: {e}")
            
#     def auto_load_prj(self):
#         """Automatically load PRJ file if it exists next to DXF"""
#         if not self.current_dxf_path:
#             return
            
#         try:
#             # Look for .prj file with same name as DXF
#             prj_path = os.path.splitext(self.current_dxf_path)[0] + ".prj"
            
#             if os.path.exists(prj_path):
#                 print(f"✅ Auto-loading PRJ: {prj_path}")
#                 self.parse_prj_file(prj_path)
#             else:
#                 print(f"ℹ️ No PRJ file found at: {prj_path}")
#         except Exception as e:
#             print(f"⚠️ Auto-load PRJ failed: {e}")
            
#     def load_prj_file(self):
#         """Open file dialog to select PRJ file"""
#         file_path, _ = QFileDialog.getOpenFileName(
#             self,
#             "Select PRJ File",
#             "",
#             "PRJ Files (*.prj);;All Files (*.*)"
#         )
        
#         if file_path:
#             self.parse_prj_file(file_path)
            
#     def parse_prj_file(self, file_path):
#         """Parse TerraScan PRJ file and populate table"""
#         try:
#             # ✅ Store the PRJ file path (prevents deletion)
#             self.current_prj_path = file_path
#             self.current_directory = os.path.dirname(file_path)
#             # Any previous backend working copy is stale once a fresh file loads
#             self._discard_working()
#             prj_filename = os.path.basename(file_path)
#             self.dxf_label.setText(f"📁  PRJ: {prj_filename}")
                        
#             print(f"✅ Stored PRJ path: {file_path}")
#             # Clean up any active fence / add-by-boundaries / edit-definition state
#             self._fence_deactivate(cancel=False)
#             self._fence_highlight_clear()
#             self._addb_deactivate(cancel=True)
#             self._editdef_deactivate(cancel=True)
#             self.prj_data = []
#             self._all_row_data = []
#             self.table.setRowCount(0)
#             # Reset sort arrows back to neutral on every fresh load
#             self._sort_header._sort_states = {}
#             self._sort_header.viewport().update()
            
#             with open(file_path, 'r', encoding='utf-8') as f:
#                 lines = f.readlines()
            
#             print(f"\n📄 Parsing PRJ file: {file_path}")
#             print(f"📄 Total lines in file: {len(lines)}")
            
#             # Parse block entries: "Block DX5013255_000001.laz"
#             i = 0
#             while i < len(lines):
#                 line = lines[i].strip()
                
#                 if line.startswith('Block '):
#                     # Extract block filename
#                     block_file = line.replace('Block ', '').strip()
#                     block_label = block_file.replace('.laz', '').replace('.las', '')
                    
#                     print(f"\n  🔍 Found block: {block_label} at line {i}")
                    
#                     # ✅ Read ALL lines until next Block or EOF — no arbitrary cap.
#                     # Previous <=10 limit was cutting off polygons with many vertices.
#                     coords = []
#                     j = i + 1
                    
#                     while j < len(lines):
#                         coord_line = lines[j].strip()
                        
#                         # Stop at next block header
#                         if coord_line.startswith('Block '):
#                             print(f"    🛑 Hit next block at line {j}, stopping")
#                             break
                        
#                         # Skip empty lines
#                         if coord_line == '':
#                             j += 1
#                             continue
                        
#                         # ✅ Skip TerraScan metadata lines (GroupFirst=, GroupCount=, etc.)
#                         if '=' in coord_line:
#                             print(f"    ⏭️ Metadata: '{coord_line}', skipping")
#                             j += 1
#                             continue
                        
#                         # Try to parse as "X Y" coordinate pair
#                         parts = coord_line.split()
#                         if len(parts) >= 2:
#                             try:
#                                 x = float(parts[0])
#                                 y = float(parts[1])
#                                 coords.append((x, y))
#                                 print(f"    ✅ Coord line {j}: ({x}, {y})")
#                             except ValueError:
#                                 print(f"    ⚠️ Non-numeric line {j}: '{coord_line}', skipping")
#                         else:
#                             print(f"    ⚠️ Unexpected line {j}: '{coord_line}', skipping")
                        
#                         j += 1
                    
#                     # ✅ Remove duplicate closing point if polygon is explicitly closed
#                     if len(coords) >= 2 and coords[0] == coords[-1]:
#                         coords = coords[:-1]
#                         print(f"    ♻️ Removed duplicate closing point")
                    
#                     print(f"    📊 Total unique boundary coords: {len(coords)}")
                    
#                     if len(coords) >= 2:
#                         # Use a polygon-safe target point instead of a raw
#                         # vertex average, which can drift outside concave shapes.
#                         avg_x, avg_y = self._polygon_target_xy(coords)
                        
#                         block_data = {
#                             'label': block_label,
#                             'easting': f"{avg_x:.2f}",
#                             'northing': f"{avg_y:.2f}",
#                             'description': f"Boundary: {len(coords)} points",
#                             'boundary_coords': coords,  # ✅ stored for Shoelace area
#                             'target_xy': (avg_x, avg_y),
#                         }
#                         self.prj_data.append(block_data)
#                         print(f"    ✅ Added: {block_label} at ({avg_x:.2f}, {avg_y:.2f})")
#                     else:
#                         print(f"    ⚠️ Not enough coordinates for {block_label}, skipping")
                    
#                     # Resume outer loop from where inner loop ended
#                     i = j
#                 else:
#                     i += 1
            
#             # ── Collect LAZ search directories from loaded SNT attachments ──────
#             # The LAZ files live in the same folder as the .snt file, NOT in the
#             # PRJ file's own directory.  We gather every unique parent folder from
#             # all currently loaded SNT attachments so cross-area PRJs correctly
#             # show "No file in path" for blocks that aren't in the active dataset.
#             laz_search_dirs = self._get_snt_laz_directories()

#             # Fallback: if no SNT loaded at all, use the PRJ's own directory
#             if not laz_search_dirs:
#                 laz_search_dirs = [self.current_directory]
#                 print("  ℹ️ No SNT loaded — falling back to PRJ directory for LAZ lookup")
#             else:
#                 print(f"  🗂️ LAZ search dirs from SNT: {laz_search_dirs}")

#             # Populate table
#             # Populate table + master sort list
#             print(f"\n📊 Total blocks found: {len(self.prj_data)}")
#             self.table.setRowCount(len(self.prj_data))
#             self._all_row_data = []   # ← cleared fresh every load
#             files_found = 0
#             files_missing = 0

#             for row, data in enumerate(self.prj_data):
#                 block_label = data['label']
#                 candidate_filenames = [f"{block_label}.laz", f"{block_label}.las"]

#                 found_laz_path = None
#                 for search_dir in laz_search_dirs:
#                     for candidate_name in candidate_filenames:
#                         candidate = os.path.join(search_dir, candidate_name)
#                         if os.path.exists(candidate):
#                             found_laz_path = candidate
#                             break
#                     if found_laz_path:
#                         break

#                 # Column 0: Block Label  — store original prj_data index in UserRole
#                 label_item = QTableWidgetItem(block_label)
#                 label_item.setData(Qt.UserRole, row)   # ← keeps sort↔prj_data in sync
#                 self.table.setItem(row, 0, label_item)

#                 if found_laz_path:
#                     # Keep the exact resolved source beside the PRJ geometry.
#                     # The table previously retained only the point-count text,
#                     # so Identify could find a LAS but had no path to load it.
#                     data['las_path'] = os.path.normpath(found_laz_path)
#                     point_count = self.get_laz_point_count(found_laz_path)
#                     area = self.calculate_grid_area_from_prj(data)

#                     pts_str  = f"{point_count:,}" if point_count > 0 else "N/A"
#                     area_str = f"{area:.2f}"      if area > 0        else "N/A"

#                     self.table.setItem(row, 1, QTableWidgetItem(pts_str))
#                     self.table.setItem(row, 2, QTableWidgetItem(area_str))

#                     # master list tuple: (label, points, area, col1_fg, col2_fg, prj_idx)
#                     self._all_row_data.append((block_label, pts_str, area_str, None, None, row))
#                     files_found += 1
#                 else:
#                     red = QColor(ThemeColors.get("danger"))
#                     item1 = QTableWidgetItem("No file in path")
#                     item2 = QTableWidgetItem("No file in path")
#                     item1.setForeground(red)
#                     item2.setForeground(red)
#                     self.table.setItem(row, 1, item1)
#                     self.table.setItem(row, 2, item2)

#                     self._all_row_data.append((block_label, "No file in path", "No file in path", red, red, row))
#                     files_missing += 1

#             print(f"✅ Files found: {files_found}, ❌ Files missing: {files_missing}")

#             # Reset sort state so header arrows go back to neutral on new load
#             self._sort_header._sort_states = {}
#             self._sort_header.viewport().update()

#             if len(self.prj_data) > 0:
#                 if hasattr(self.app, "statusBar"):
#                     self.app.statusBar().showMessage(
#                         f"Loaded {len(self.prj_data)} blocks from {os.path.basename(file_path)}", 5000
#                     )
#             else:
#                 QMessageBox.warning(
#                     self,
#                     "⚠️ No Blocks Found",
#                     f"The PRJ file was read but no block entries were found.\n\nFile: {os.path.basename(file_path)}"
#                 )
            
#         except Exception as e:
#             QMessageBox.critical(
#                 self,
#                 "❌ Load Failed",
#                 f"Failed to load PRJ file:\n{str(e)}"
#             )
#             import traceback
#             traceback.print_exc()
            
            
#     def clear_highlight_for_block(self, block_label):
#         """Clear highlight for a specific block when clicked."""
#         if not hasattr(self, '_highlighted_blocks') or block_label not in self._highlighted_blocks:
#             return
        
#         try:
#             import vtk
            
#             block_info = self._highlighted_blocks[block_label]
#             actor = block_info['actor']
#             highlight_actor = block_info['highlight_actor']
            
#             if hasattr(self.app, 'vtk_widget') and self.app.vtk_widget:
#                 renderer = self.app.vtk_widget.renderer
                
#                 # Find and restore original text properties
#                 if hasattr(self, '_highlighted_labels'):
#                     for label_actor, orig_color, orig_scale in self._highlighted_labels:
#                         if label_actor == actor:
#                             actor.GetProperty().SetColor(orig_color)
#                             actor.SetScale(orig_scale)
#                             self._highlighted_labels.remove((label_actor, orig_color, orig_scale))
#                             break
                
#                 # Remove highlight circle
#                 if highlight_actor and highlight_actor in self._highlight_actors:
#                     renderer.RemoveActor(highlight_actor)
#                     self._highlight_actors.remove(highlight_actor)
                
#                 # Remove from tracking
#                 del self._highlighted_blocks[block_label]
                
#                 # Render
#                 self.app.vtk_widget.GetRenderWindow().Render()
                
#                 print(f"✅ Cleared highlight for: {block_label}")
        
#         except Exception as e:
#             print(f"⚠️ Failed to clear highlight for {block_label}: {e}")


#     def _auto_clear_highlights(self):
#         """Auto-clear all highlight circles and restore text colors after timeout."""
#         if not hasattr(self, '_highlight_actors') or not self._highlight_actors:
#             return

#         try:
#             if hasattr(self.app, 'vtk_widget') and self.app.vtk_widget:
#                 renderer = self.app.vtk_widget.renderer

#                 # Remove highlight circles
#                 for highlight_actor in list(self._highlight_actors):
#                     try:
#                         renderer.RemoveActor(highlight_actor)
#                     except Exception:
#                         pass

#                 # Restore text label colors
#                 for label_actor, orig_color, orig_scale in list(self._highlighted_labels):
#                     try:
#                         label_actor.GetProperty().SetColor(orig_color)
#                         label_actor.SetScale(orig_scale)
#                     except Exception:
#                         pass

#                 self.app.vtk_widget.GetRenderWindow().Render()

#             self._highlight_actors = []
#             self._highlighted_labels = []
#             self._highlighted_blocks = {}

#             print("🕐 Auto-cleared all block highlights")
#         except Exception as e:
#             print(f"⚠️ Auto-clear highlights failed: {e}")
            
            
#     def on_table_item_clicked(self, item):
#         """Handle single-click on table item - clear highlight if exists."""
#         if item is None:
#             return
#         row = item.row()

#         # Clear fence-selection boundary highlights when user manually interacts
#         self._fence_highlight_clear()

#         if not self.prj_data:
#             return

#         # ← CHANGED: read original prj_data index from UserRole (survives sorting)
#         label_item = self.table.item(row, 0)
#         prj_idx = label_item.data(Qt.UserRole) if label_item is not None else row
#         if prj_idx is None or prj_idx < 0 or prj_idx >= len(self.prj_data):
#             return

#         block_label = self.prj_data[prj_idx].get('label', '')
#         if not block_label:
#             return

#         # Check if this block is currently highlighted
#         if hasattr(self, '_highlighted_blocks') and block_label in self._highlighted_blocks:
#             print(f"🖱️ Clicked highlighted block: {block_label} - clearing highlight")
#             self.clear_highlight_for_block(block_label)
                    
#     def identify_selected_block(self):
#         """Identify selected blocks from their PRJ boundary geometry."""
#         selected_rows = self.table.selectionModel().selectedRows()
#         if not selected_rows:
#             QMessageBox.warning(self, "No Selection", "Please select a block to identify")
#             return

#         # Get unique visual row indices
#         rows = list(set([index.row() for index in selected_rows]))

#         print(f"\n🔍 Searching for {len(rows)} selected blocks...")

#         # Clear previous highlight actors and restore text colors before starting fresh
#         if hasattr(self.app, 'vtk_widget') and self.app.vtk_widget:
#             renderer = self.app.vtk_widget.renderer
#             for old_actor in getattr(self, '_highlight_actors', []):
#                 try:
#                     renderer.RemoveActor(old_actor)
#                 except Exception:
#                     pass
#         for label_actor, orig_color, orig_scale in getattr(self, '_highlighted_labels', []):
#             try:
#                 label_actor.GetProperty().SetColor(orig_color)
#                 label_actor.SetScale(orig_scale)
#             except Exception:
#                 pass

#         self._highlight_actors = []
#         self._highlighted_labels = []
#         self._highlighted_blocks = {}

#         matches_found = []
#         selected_grid_blocks = []
#         prj_geometry_blocks = []

#         for row in rows:
#             # ← CHANGED: read original prj_data index from UserRole (survives sorting)
#             label_item = self.table.item(row, 0)
#             prj_idx = label_item.data(Qt.UserRole) if label_item is not None else row
#             if prj_idx is None or prj_idx >= len(self.prj_data):
#                 continue

#             block_data = self.prj_data[prj_idx]   # ← was: self.prj_data[row]
#             block_label = block_data['label']
#             print(f"\n🔍 Searching for block: '{block_label}'")

#             # The loaded SNT polygon is authoritative for viewport location.
#             # It exists independently of whether a matching LAZ/LAS file is
#             # present or whether its text label received a rendered actor.
#             snt_polygon = self._find_loaded_snt_block_polygon(
#                 block_label,
#                 block_data.get('boundary_coords') or [],
#             )
#             identify_data = dict(block_data)
#             if snt_polygon is not None:
#                 identify_data['boundary_coords'] = snt_polygon
#                 identify_data['identify_source'] = 'SNT'
#                 print(
#                     f"  ✅ MATCH (SNT polygon): '{block_label}' "
#                     f"({len(snt_polygon)} vertices)"
#                 )
#             else:
#                 prj_geometry_blocks.append(block_label)
#                 identify_data['identify_source'] = 'PRJ'
#                 print(
#                     f"  ✅ MATCH (PRJ geometry): '{block_label}'; "
#                     "navigating independently of LAZ/LAS availability"
#                 )
#             selected_grid_blocks.append(identify_data)

#             try:
#                 block_found = False

#                 if not block_found and hasattr(self.app, 'dxf_actors'):
#                     for dxf_data in self.app.dxf_actors:
#                         for actor in dxf_data.get('actors', []):
#                             if (
#                                 getattr(actor, 'is_grid_label', False)
#                                 or getattr(actor, 'is_block_polygon', False)
#                             ):
#                                 grid_name = getattr(actor, 'grid_name', '')
#                                 grid_key = "".join(ch.lower() for ch in str(grid_name) if ch.isalnum())
#                                 block_key = "".join(ch.lower() for ch in str(block_label) if ch.isalnum())
#                                 if grid_key == block_key:
#                                     print(f"  ✅ MATCH (DXF): '{grid_name}' ~ '{block_label}'")
#                                     matches_found.append((actor, block_data))
#                                     block_found = True
#                                     break
#                         if block_found:
#                             break

#                 # Also search SNT actors (block labels from SNT attachments)
#                 if not block_found and hasattr(self.app, 'snt_actors'):
#                     for snt_data in self.app.snt_actors:
#                         for actor in snt_data.get('actors', []):
#                             if (
#                                 getattr(actor, 'is_grid_label', False)
#                                 or getattr(actor, 'is_block_polygon', False)
#                             ):
#                                 grid_name = getattr(actor, 'grid_name', '')
#                                 grid_key = "".join(ch.lower() for ch in str(grid_name) if ch.isalnum())
#                                 block_key = "".join(ch.lower() for ch in str(block_label) if ch.isalnum())
#                                 if grid_key == block_key:
#                                     print(f"  ✅ MATCH (SNT): '{grid_name}' ~ '{block_label}'")
#                                     matches_found.append((actor, block_data))
#                                     block_found = True
#                                     break
#                         if block_found:
#                             break

#                 if not block_found:
#                     if snt_polygon is not None:
#                         print(
#                             f"  ℹ️ No rendered label actor for '{block_label}'; "
#                             "using its loaded SNT polygon"
#                         )
#                     else:
#                         print(
#                             f"  ℹ️ No rendered SNT label actor for "
#                             f"'{block_label}'; using its exact PRJ geometry"
#                         )

#             except Exception as e:
#                 print(f"  ❌ Error searching for '{block_label}': {e}")

#         # Highlight all matched blocks simultaneously
#         if matches_found:
#             print(f"\n🎨 Highlighting {len(matches_found)} blocks simultaneously...")
#             try:
#                 import vtk

#                 if hasattr(self.app, 'vtk_widget') and self.app.vtk_widget:
#                     renderer = self.app.vtk_widget.renderer
#                     highlighted_labels = []

#                     for actor, block_data in matches_found:
#                         # Use the ACTUAL actor position from SNT/DXF
#                         # NOT the PRJ boundary centroid — they differ!
#                         try:
#                             b = actor.GetBounds()
#                             x = (b[0] + b[1]) / 2.0
#                             y = (b[2] + b[3]) / 2.0
#                         except Exception:
#                             x = float(block_data['easting'])
#                             y = float(block_data['northing'])

#                         circle = vtk.vtkRegularPolygonSource()
#                         circle.SetNumberOfSides(50)
#                         circle.SetRadius(30)
#                         circle.SetCenter(x, y, 1)
#                         circle.GeneratePolygonOff()

#                         mapper = vtk.vtkPolyDataMapper()
#                         mapper.SetInputConnection(circle.GetOutputPort())

#                         highlight_actor = vtk.vtkActor()
#                         highlight_actor.SetMapper(mapper)
#                         highlight_actor.GetProperty().SetColor(1.0, 0.0, 0.0)
#                         highlight_actor.GetProperty().SetLineWidth(5)
#                         highlight_actor.GetProperty().SetOpacity(1.0)

#                         renderer.AddActor(highlight_actor)
#                         self._highlight_actors.append(highlight_actor)

#                         if hasattr(actor, 'GetProperty'):
#                             original_color = actor.GetProperty().GetColor()
#                             original_scale = actor.GetScale()
#                             actor.GetProperty().SetColor(1.0, 1.0, 0.0)
#                             highlighted_labels.append((actor, original_color, original_scale))

#                         print(f"   ✅ Highlighted: {block_data['label']} at ({x:.2f}, {y:.2f})")

#                     self.app.vtk_widget.GetRenderWindow().Render()

#                 self._highlighted_labels.extend(highlighted_labels)

#                 for i, (actor, block_data) in enumerate(matches_found):
#                     block_label = block_data['label']
#                     self._highlighted_blocks[block_label] = {
#                         'actor': actor,
#                         'block_data': block_data,
#                         'highlight_actor': self._highlight_actors[i] if i < len(self._highlight_actors) else None
#                     }

#                 # Cancel any pending auto-clear from previous identify
#                 self._highlight_identify_id += 1
#                 current_id = self._highlight_identify_id

#                 # Auto-clear highlights after 20 seconds (only if no new identify happened)
#                 def _guarded_auto_clear():
#                     if self._highlight_identify_id == current_id:
#                         self._auto_clear_highlights()

#                 self._highlight_auto_clear_timer = QTimer.singleShot(20000, _guarded_auto_clear)

#                 if hasattr(self.app, "statusBar"):
#                     block_names = [bd['label'] for _, bd in matches_found]
#                     self.app.statusBar().showMessage(
#                         f"Highlighted {len(matches_found)} block(s) — auto-clears in 20 s", 20000
#                     )

#             except Exception as e:
#                 print(f"❌ Highlighting failed: {e}")
#                 import traceback
#                 traceback.print_exc()

#         # Frame SNT geometry when present; otherwise frame the exact PRJ grid
#         # even when it lies outside the loaded SNT coverage.
#         grid_bounds = self._highlight_prj_boundaries(selected_grid_blocks)
#         if grid_bounds:
#             min_x, min_y, max_x, max_y = grid_bounds
#             center_x = 0.5 * (min_x + max_x)
#             center_y = 0.5 * (min_y + max_y)
#             span = max(max_x - min_x, max_y - min_y)
#             target_scale = max(50.0, span * 0.58 if span > 0 else 250.0)
#             self.fly_to_location(center_x, center_y, target_scale=target_scale)

#             self._highlight_identify_id += 1
#             current_id = self._highlight_identify_id

#             def _guarded_prj_auto_clear():
#                 if self._highlight_identify_id == current_id:
#                     self._auto_clear_highlights()

#             self._highlight_auto_clear_timer = QTimer.singleShot(
#                 20000, _guarded_prj_auto_clear
#             )
#             if hasattr(self.app, "statusBar"):
#                 snt_count = sum(
#                     block.get('identify_source') == 'SNT'
#                     for block in selected_grid_blocks
#                 )
#                 self.app.statusBar().showMessage(
#                     f"Identified {len(selected_grid_blocks)} block(s): "
#                     f"{snt_count} from loaded SNT geometry, "
#                     f"{len(selected_grid_blocks) - snt_count} from PRJ fallback"
#                     + (
#                         f"; {len(prj_geometry_blocks)} from PRJ geometry"
#                         if prj_geometry_blocks else ""
#                     ),
#                     5000,
#                 )
#         else:
#             QMessageBox.warning(
#                 self,
#                 "Invalid PRJ Geometry",
#                 "The selected block has no valid boundary coordinates in the PRJ file."
#             )

#     def _find_loaded_snt_block_polygon(self, block_label, prj_boundary=None):
#         """Return a loaded-SNT polygon by label, then by PRJ spatial overlap."""
#         def _key(value):
#             stem = os.path.splitext(os.path.basename(str(value or '').strip()))[0]
#             return ''.join(ch.lower() for ch in stem if ch.isalnum())

#         wanted = _key(block_label)
#         if not wanted:
#             return None

#         entries = getattr(self.app, 'snt_block_polygons', []) or []
#         for entry in entries:
#             names = [entry.get('grid_name'), entry.get('block_file')]
#             names.extend(entry.get('alt_names') or [])
#             if not any(_key(name) == wanted for name in names):
#                 continue

#             points = entry.get('points_2d') or []
#             try:
#                 polygon = [(float(point[0]), float(point[1])) for point in points]
#             except (TypeError, ValueError, IndexError):
#                 continue
#             if len(polygon) >= 3:
#                 if polygon[0] == polygon[-1]:
#                     polygon = polygon[:-1]
#                 return polygon

#         # Some SNT formats contain valid block boundaries without a usable
#         # label actor/name association. Match those boundaries spatially to
#         # the selected PRJ grid, independent of LAZ/LAS file availability.
#         try:
#             prj_polygon = [
#                 (float(point[0]), float(point[1]))
#                 for point in (prj_boundary or [])
#             ]
#         except (TypeError, ValueError, IndexError):
#             prj_polygon = []
#         if len(prj_polygon) < 3:
#             return None

#         prj_x = [point[0] for point in prj_polygon]
#         prj_y = [point[1] for point in prj_polygon]
#         pmin_x, pmax_x = min(prj_x), max(prj_x)
#         pmin_y, pmax_y = min(prj_y), max(prj_y)
#         prj_bbox_area = max(1e-9, (pmax_x - pmin_x) * (pmax_y - pmin_y))
#         best_polygon = None
#         best_score = 0.0

#         for entry in entries:
#             points = entry.get('points_2d') or []
#             try:
#                 polygon = [(float(point[0]), float(point[1])) for point in points]
#             except (TypeError, ValueError, IndexError):
#                 continue
#             if len(polygon) < 3:
#                 continue
#             if polygon[0] == polygon[-1]:
#                 polygon = polygon[:-1]

#             xs = [point[0] for point in polygon]
#             ys = [point[1] for point in polygon]
#             overlap_w = max(0.0, min(pmax_x, max(xs)) - max(pmin_x, min(xs)))
#             overlap_h = max(0.0, min(pmax_y, max(ys)) - max(pmin_y, min(ys)))
#             bbox_overlap = overlap_w * overlap_h
#             if bbox_overlap <= 0.0:
#                 continue

#             center_x, center_y = self._polygon_target_xy(polygon)
#             center_inside = _point_in_polygon_xy(
#                 center_x, center_y, prj_polygon
#             )
#             score = bbox_overlap / prj_bbox_area
#             if center_inside:
#                 score += 1.0
#             if score > best_score:
#                 best_score = score
#                 best_polygon = polygon

#         if best_polygon is not None and best_score >= 0.25:
#             print(
#                 f"  ✅ MATCH (SNT spatial geometry): '{block_label}' "
#                 f"(score={best_score:.3f})"
#             )
#             return best_polygon
#         return None
#         return None

#     def _highlight_prj_boundaries(self, blocks):
#         """Draw selected PRJ polygons and return their combined XY bounds."""
#         vtk_widget = getattr(self.app, "vtk_widget", None)
#         renderer = getattr(vtk_widget, "renderer", None)
#         if renderer is None:
#             return None

#         try:
#             import vtk

#             all_x = []
#             all_y = []
#             for block in blocks:
#                 coords = block.get("boundary_coords") or []
#                 if len(coords) < 3:
#                     continue

#                 ring = [(float(x), float(y)) for x, y in coords]
#                 if ring[0] != ring[-1]:
#                     ring.append(ring[0])

#                 points = vtk.vtkPoints()
#                 for x, y in ring:
#                     points.InsertNextPoint(x, y, 2.0)
#                     all_x.append(x)
#                     all_y.append(y)

#                 polyline = vtk.vtkPolyLine()
#                 polyline.GetPointIds().SetNumberOfIds(len(ring))
#                 for index in range(len(ring)):
#                     polyline.GetPointIds().SetId(index, index)

#                 cells = vtk.vtkCellArray()
#                 cells.InsertNextCell(polyline)
#                 polydata = vtk.vtkPolyData()
#                 polydata.SetPoints(points)
#                 polydata.SetLines(cells)

#                 mapper = vtk.vtkPolyDataMapper()
#                 mapper.SetInputData(polydata)
#                 boundary_actor = vtk.vtkActor()
#                 boundary_actor.SetMapper(mapper)
#                 boundary_actor.GetProperty().SetColor(1.0, 0.15, 0.0)
#                 boundary_actor.GetProperty().SetLineWidth(6.0)
#                 boundary_actor.GetProperty().SetOpacity(1.0)
#                 boundary_actor.PickableOff()
#                 renderer.AddActor(boundary_actor)
#                 self._highlight_actors.append(boundary_actor)

#                 print(
#                     f"   ✅ {block.get('identify_source', 'PRJ')} boundary: "
#                     f"{block.get('label', '')} ({len(coords)} vertices)"
#                 )

#             if not all_x:
#                 return None
#             vtk_widget.GetRenderWindow().Render()
#             return min(all_x), min(all_y), max(all_x), max(all_y)
#         except Exception as exc:
#             print(f"❌ PRJ boundary highlight failed: {exc}")
#             return None
    
    
#     def fly_to_location(self, x, y, duration=2000, target_scale=None):
#         """
#         Smoothly pan the 2D orthographic camera to the target XY location.

#         ROOT-CAUSE FIX (grid disappears after identify):
#         ─────────────────────────────────────────────────
#         The old implementation set camera Z-position to 200 and called
#         camera.Zoom(2.0).  In a parallel-projection (2D) view this does two
#         harmful things:

#           1. SetPosition([x, y, 200]) moves the camera ABOVE the data plane
#              but ONLY translates the focal-point to Z=0.  This implicitly
#              switches the view direction, which — combined with
#              ResetCameraClippingRange() — produces a clipping frustum that
#              excludes DXF grid actors sitting at Z ≈ 0.  The actors are still
#              in the renderer, but they fall outside the near/far clip planes,
#              so VTK does not draw them.  Pressing Shift+F calls fit_view()
#              which resets the camera properly and brings them back.

#           2. camera.Zoom(2.0) is a MULTIPLICATIVE scale operation — calling
#              it once per "identify" compounded the parallel-scale, causing
#              progressive zoom drift on repeated identifies.

#         CORRECT 2D APPROACH:
#           • Keep ParallelProjection ON throughout.
#           • Animate ONLY the focal-point (XY pan) and the camera position
#             offset by the same delta — the camera always stays directly
#             above the focal point at its current Z height (view-up = Y).
#           • Animate the parallel-scale from its current value to a
#             target_scale that gives a comfortable (~500 m) view window.
#           • After animation: call _ensure_overlay_actors() so that any
#             DXF/SNT actors that were removed by an intermediate render
#             call are guaranteed to be back in the renderer.
#         """
#         try:
#             from PySide6.QtCore import QTimer

#             if not hasattr(self.app, 'vtk_widget') or not self.app.vtk_widget:
#                 return

#             renderer   = self.app.vtk_widget.renderer
#             camera     = renderer.GetActiveCamera()
#             render_win = self.app.vtk_widget.GetRenderWindow()

#             # Own the camera for the complete transaction.  A large grid load
#             # can queue wheel/touchpad momentum events; MainWheelZoomEventFilter
#             # consumes them while this flag is set instead of letting them
#             # overwrite the fly-to scale between animation frames.
#             fly_generation = int(getattr(self.app, "_prj_fly_generation", 0)) + 1
#             self.app._prj_fly_generation = fly_generation
#             self.app._prj_fly_camera_active = True
#             cancel_zoom = getattr(self.app, "_cancel_smooth_zoom_for_pan", None)
#             if callable(cancel_zoom):
#                 cancel_zoom()

#             # ── Snapshot current camera state ─────────────────────────
#             start_focal = list(camera.GetFocalPoint())   # (fx, fy, fz)
#             start_pos   = list(camera.GetPosition())     # (px, py, pz)
#             start_scale = camera.GetParallelScale()      # half-height in world units

#             # A PRJ identify is a strict 2D operation.  Do not carry an XY
#             # camera offset (tilt) or a stale focal Z forward from a previous
#             # SNT/DXF view: either one makes correctly georeferenced points at
#             # a different elevation project away from the requested PRJ XY.
#             # Preserve only a safe positive camera-to-focal distance.
#             camera_distance = abs(float(start_pos[2]) - float(start_focal[2]))
#             if camera_distance < 1.0:
#                 camera_distance = 1000.0

#             target_z = float(start_focal[2])
#             try:
#                 xyz = (getattr(self.app, "data", None) or {}).get("xyz")
#                 if xyz is not None and len(xyz):
#                     # Mid-range is stable and avoids an O(N) mean during UI work.
#                     target_z = 0.5 * (
#                         float(xyz[:, 2].min()) + float(xyz[:, 2].max())
#                     )
#             except Exception:
#                 pass

#             # ── Target values ──────────────────────────────────────────
#             target_focal = [x, y, target_z]
#             target_pos   = [x, y, target_z + camera_distance]

#             # Target parallel-scale: ~250 m half-height gives a comfortable
#             # 500 m wide view.  Clamp so we never zoom in tighter than 50 m
#             # or wider than 2× the current scale.
#             if target_scale is None:
#                 TARGET_HALF_HEIGHT_M = 250.0
#                 target_scale = max(
#                     50.0,
#                     min(TARGET_HALF_HEIGHT_M, start_scale * 2.0)
#                 )
#             else:
#                 target_scale = max(50.0, float(target_scale))

#             # ── Ensure parallel projection is active ───────────────────
#             camera.ParallelProjectionOn()
#             camera.SetViewUp(0.0, 1.0, 0.0)

#             # ── Animation ─────────────────────────────────────────────
#             try:
#                 loaded_count = len((getattr(self.app, "data", None) or {}).get("xyz", ()))
#             except Exception:
#                 loaded_count = 0
#             # Forty full renders of a 13M-point actor make a nominal two-second
#             # animation take far longer and enlarge the input-race window.
#             # Large production clouds land on the target on the next event-loop
#             # turn; lightweight scenes retain the existing smooth animation.
#             STEPS = 1 if loaded_count >= 1_000_000 else 40
#             step_duration = 1 if STEPS == 1 else max(1, duration // STEPS)
#             current_step  = [0]

#             def _eased(t: float) -> float:
#                 """Smooth-step (ease-in-out cubic)."""
#                 return t * t * (3.0 - 2.0 * t)

#             def animate_step():
#                 if int(getattr(self.app, "_prj_fly_generation", 0)) != fly_generation:
#                     return
#                 step = current_step[0]

#                 if step >= STEPS:
#                     # ── Final frame: land exactly on target ───────────
#                     camera.SetFocalPoint(target_focal)
#                     camera.SetPosition(target_pos)
#                     camera.SetParallelScale(target_scale)
#                     camera.SetViewUp(0.0, 1.0, 0.0)
#                     renderer.ResetCameraClippingRange()
#                     render_win.Render()

#                     # ── CRITICAL: restore any DXF/SNT actors that an
#                     #    intermediate render might have dropped ─────────
#                     try:
#                         if hasattr(self.app, '_ensure_overlay_actors'):
#                             self.app._ensure_overlay_actors()
#                     except Exception:
#                         pass

#                     print(
#                         f"✈️  Fly-to complete → ({x:.2f}, {y:.2f}) "
#                         f"focal={tuple(round(v, 3) for v in camera.GetFocalPoint())} "
#                         f"scale={camera.GetParallelScale():.3f}"
#                     )

#                     # Keep the guard for one short event-loop drain so queued
#                     # momentum generated during the blocking LAS load cannot
#                     # execute immediately after the final frame.
#                     def _release_fly_camera():
#                         if int(getattr(self.app, "_prj_fly_generation", 0)) == fly_generation:
#                             self.app._prj_fly_camera_active = False
#                             commit = getattr(self.app, "_commit_main_view_history", None)
#                             if callable(commit):
#                                 commit("prj_fly_to")

#                     QTimer.singleShot(250, _release_fly_camera)
#                     return

#                 t = _eased(step / STEPS)

#                 # Interpolate focal point (XY pan only)
#                 new_focal = [
#                     start_focal[0] + (target_focal[0] - start_focal[0]) * t,
#                     start_focal[1] + (target_focal[1] - start_focal[1]) * t,
#                     start_focal[2] + (target_focal[2] - start_focal[2]) * t,
#                 ]
#                 # Converge to a true top-down camera as well as the target XY.
#                 new_pos = [
#                     start_pos[0] + (target_pos[0] - start_pos[0]) * t,
#                     start_pos[1] + (target_pos[1] - start_pos[1]) * t,
#                     start_pos[2] + (target_pos[2] - start_pos[2]) * t,
#                 ]
#                 # Animate parallel scale
#                 new_scale = start_scale + (target_scale - start_scale) * t

#                 camera.SetFocalPoint(new_focal)
#                 camera.SetPosition(new_pos)
#                 camera.SetParallelScale(new_scale)
#                 renderer.ResetCameraClippingRange()
#                 render_win.Render()

#                 current_step[0] += 1
#                 QTimer.singleShot(step_duration, animate_step)

#             # Kick off animation
#             animate_step()
#             print(f"✈️  Flying to ({x:.2f}, {y:.2f})  target_scale={target_scale:.1f}")

#         except Exception as e:
#             try:
#                 self.app._prj_fly_camera_active = False
#             except Exception:
#                 pass
#             print(f"⚠️ Fly-to animation failed: {e}")
#             import traceback
#             traceback.print_exc()        
            
#     def zoom_to_block(self, block, block_data):
#         """Zoom and highlight a DXF block"""
#         try:
#             # Get block position (from PRJ coordinates or block data)
#             x = float(block_data['easting'])
#             y = float(block_data['northing'])
            
#             # Zoom to block location
#             if hasattr(self.app, 'viewer') and self.app.viewer:
#                 # Create temporary highlight
#                 self.highlight_location(x, y)
                
#                 # Zoom to location with some padding
#                 padding = 50  # meters
#                 self.app.viewer.zoom_to_bounds(
#                     x - padding, y - padding,
#                     x + padding, y + padding
#                 )
                
#         except Exception as e:
#             print(f"⚠️ Zoom failed: {e}")
            
            
#     def zoom_to_entity(self, actor, block_data):
#         """
#         Pan the 2D camera to the entity location and create a visible highlight
#         marker.  Preserves parallel projection so DXF grid actors stay visible.
#         """
#         try:
#             import vtk

#             x = float(block_data['easting'])
#             y = float(block_data['northing'])

#             print(f"🎯 Zooming to block at ({x}, {y})")

#             if not (hasattr(self.app, 'vtk_widget') and self.app.vtk_widget):
#                 print("⚠️ VTK widget not available")
#                 return

#             renderer   = self.app.vtk_widget.renderer
#             render_win = self.app.vtk_widget.GetRenderWindow()
#             camera     = renderer.GetActiveCamera()

#             # ── Remove old single-entity highlight ────────────────────
#             if hasattr(self, '_highlight_actor') and self._highlight_actor:
#                 try:
#                     renderer.RemoveActor(self._highlight_actor)
#                 except Exception:
#                     pass
#                 self._highlight_actor = None

#             # ── Create RED CIRCLE highlight marker ────────────────────
#             circle = vtk.vtkRegularPolygonSource()
#             circle.SetNumberOfSides(50)
#             circle.SetRadius(30)
#             circle.SetCenter(x, y, 1)        # slightly above data plane
#             circle.GeneratePolygonOff()

#             mapper = vtk.vtkPolyDataMapper()
#             mapper.SetInputConnection(circle.GetOutputPort())

#             highlight_actor = vtk.vtkActor()
#             highlight_actor.SetMapper(mapper)
#             highlight_actor.GetProperty().SetColor(1.0, 0.0, 0.0)
#             highlight_actor.GetProperty().SetLineWidth(5)
#             highlight_actor.GetProperty().SetOpacity(1.0)

#             renderer.AddActor(highlight_actor)
#             self._highlight_actor = highlight_actor

#             # ── Move camera in 2D — DO NOT break parallel projection ──
#             # Keep the camera at its current Z height above the scene and
#             # simply pan the focal point to (x, y).  This keeps every DXF/
#             # SNT actor inside the clipping frustum.
#             current_focal = list(camera.GetFocalPoint())
#             current_pos   = list(camera.GetPosition())
#             cam_offset_z  = current_pos[2] - current_focal[2]   # camera Z above focal

#             camera.ParallelProjectionOn()
#             camera.SetViewUp(0.0, 1.0, 0.0)
#             camera.SetFocalPoint(x, y, current_focal[2])
#             camera.SetPosition(x, y, current_focal[2] + cam_offset_z)

#             # Zoom in: halve the parallel scale for a closer look (minimum 50 m)
#             new_scale = max(50.0, camera.GetParallelScale() * 0.5)
#             camera.SetParallelScale(new_scale)

#             renderer.ResetCameraClippingRange()
#             render_win.Render()

#             # ── Ensure all overlay actors are still in the renderer ───
#             try:
#                 if hasattr(self.app, '_ensure_overlay_actors'):
#                     self.app._ensure_overlay_actors()
#             except Exception:
#                 pass

#             # ── Temporarily highlight the text actor ──────────────────
#             if hasattr(actor, 'GetProperty'):
#                 original_color = tuple(actor.GetProperty().GetColor())
#                 original_scale = tuple(actor.GetScale())

#                 actor.GetProperty().SetColor(1.0, 1.0, 0.0)   # yellow
#                 render_win.Render()

#                 def reset_highlight():
#                     try:
#                         actor.GetProperty().SetColor(original_color)
#                         actor.SetScale(original_scale)
#                         renderer.RemoveActor(highlight_actor)
#                         self._highlight_actor = None
#                         render_win.Render()
#                     except Exception:
#                         pass

#                 from PySide6.QtCore import QTimer
#                 QTimer.singleShot(3000, reset_highlight)

#             print(f"✅ Zoomed to block with RED CIRCLE highlight (30 m radius)")

#             if hasattr(self.app, "statusBar"):
#                 self.app.statusBar().showMessage(
#                     f"Zoomed to block: {block_data['label']}  ({x:.2f}, {y:.2f})", 4000
#                 )

#         except Exception as e:
#             print(f"⚠️ Zoom failed: {e}")
#             import traceback
#             traceback.print_exc()
            
#     def highlight_location(self, x, y):
#         """Create temporary highlight at location"""
#         try:
#             # Add temporary marker/circle at location
#             if hasattr(self.app, 'viewer') and self.app.viewer:
#                 # You can implement this based on your viewer's API
#                 print(f"🎯 Highlighting location: ({x}, {y})")
                
#                 # Example: Create a temporary highlight layer
#                 # self.app.viewer.add_highlight_marker(x, y)
                
#         except Exception as e:
#             print(f"⚠️ Highlight failed: {e}")
            
#     def debug_dxf_text_labels(self):
#         """Debug: Print all text labels found in loaded DXF"""
#         print("\n🔍 DEBUG: Scanning DXF for text labels...")
        
#         try:
#             if hasattr(self.app, 'dxf_actors') and self.app.dxf_actors:
#                 for dxf_data in self.app.dxf_actors:
#                     print(f"\n📄 DXF File: {dxf_data.get('filename', 'Unknown')}")
                    
#                     text_count = 0
#                     for actor in dxf_data.get('actors', []):
#                         if hasattr(actor, 'is_grid_label') and actor.is_grid_label:
#                             grid_name = getattr(actor, 'grid_name', 'NO_NAME')
#                             print(f"  📝 Text label: '{grid_name}'")
#                             text_count += 1
                    
#                     print(f"  Total text labels found: {text_count}")
#             else:
#                 print("  ⚠️ No DXF actors found in app")
                
#         except Exception as e:
#             print(f"  ❌ Debug failed: {e}")
#             import traceback
#             traceback.print_exc()
            
            
#     # ── Table right-click context menu ───────────────────────────────────────

#     def _on_table_context_menu(self, pos):
#         """Right-click context menu on the block table (MicroStation-style).

#         Shows block-management actions for the currently selected row(s).
#         Only available when at least one row is selected.
#         """
#         selected_indexes = self.table.selectionModel().selectedRows()
#         if not selected_indexes:
#             return

#         from PySide6.QtWidgets import QMenu

#         n = len({idx.row() for idx in selected_indexes})
#         menu = QMenu(self)

#         act_identify = menu.addAction(
#             "Identify Block" if n == 1 else f"Identify {n} Blocks"
#         )
#         menu.addSeparator()
#         act_rename = menu.addAction("Rename Definition…")
#         act_rename.setEnabled(n == 1)  # rename only makes sense for a single block
#         act_delete = menu.addAction("Delete Definition…")
#         menu.addSeparator()
#         act_export = menu.addAction("Export Selected to Sub-PRJ…")

#         chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
#         if chosen is None:
#             return
#         if chosen is act_identify:
#             self.identify_selected_block()
#         elif chosen is act_rename:
#             self.rename_block_definition()
#         elif chosen is act_delete:
#             self.delete_block_definition()
#         elif chosen is act_export:
#             self.export_selected_blocks_prj()

#     # ── Add Definition ────────────────────────────────────────────────────────

#     def add_block_definition(self):
#         """Add new block definition(s) to the loaded PRJ from LAZ/LAS file(s).

#         Matches MicroStation's "Add using files..." — multiple files can be
#         selected at once.  Each file's header is read to get the XY bounding
#         box automatically (no points are loaded).  The bounding rectangle of
#         each file becomes its block polygon.  All blocks are written in a
#         single backup + atomic write.  A .prj.bak backup is created before
#         the file is modified.
#         """
#         # ── Guard: PRJ must be loaded ─────────────────────────────────────────
#         if not self.current_prj_path:
#             QMessageBox.warning(
#                 self,
#                 "No PRJ Loaded",
#                 "Please load a PRJ file before adding a block definition.",
#             )
#             return
#         if not os.path.isfile(self.current_prj_path):
#             QMessageBox.critical(
#                 self,
#                 "PRJ File Not Found",
#                 f"The PRJ file no longer exists on disk:\n\n{self.current_prj_path}",
#             )
#             return

#         # ── Browse for one or more LAZ / LAS files ─────────────────────────────
#         file_paths, _ = QFileDialog.getOpenFileNames(
#             self,
#             "Select LAZ / LAS File(s) to Add",
#             self.current_directory or "",
#             "Point Cloud Files (*.laz *.las);;All Files (*.*)",
#         )
#         if not file_paths:
#             return  # user cancelled

#         # ── Read headers, validate, build candidate entries ────────────────────
#         existing_upper = {e.get("label", "").strip().upper() for e in self.prj_data}
#         candidates = []     # (filename, stem, bounds, polygon)
#         skipped_dupes = []
#         failed_reads = []

#         for file_path in file_paths:
#             filename = os.path.basename(file_path)
#             stem = filename
#             for ext in (".laz", ".las"):
#                 if stem.lower().endswith(ext):
#                     stem = stem[: len(stem) - len(ext)]
#                     break

#             if stem.strip().upper() in existing_upper:
#                 skipped_dupes.append(filename)
#                 continue

#             try:
#                 bounds = self._read_laz_header_bounds(file_path)
#             except Exception as exc:
#                 failed_reads.append((filename, str(exc)))
#                 continue

#             polygon = [
#                 (bounds["min_x"], bounds["min_y"]),
#                 (bounds["max_x"], bounds["min_y"]),
#                 (bounds["max_x"], bounds["max_y"]),
#                 (bounds["min_x"], bounds["max_y"]),
#                 (bounds["min_x"], bounds["min_y"]),  # close ring
#             ]
#             candidates.append((filename, stem, bounds, polygon))
#             existing_upper.add(stem.strip().upper())   # guard within this batch too

#         if not candidates:
#             msg = "No blocks could be added.\n"
#             if skipped_dupes:
#                 msg += f"\nAlready exist ({len(skipped_dupes)}): " + ", ".join(skipped_dupes[:10])
#             if failed_reads:
#                 msg += f"\nHeader read failed ({len(failed_reads)}): " + ", ".join(f[0] for f in failed_reads[:10])
#             QMessageBox.warning(self, "Nothing to Add", msg)
#             return

#         # ── Confirm with summary of all files ───────────────────────────────────
#         total_area = sum(
#             (b["max_x"] - b["min_x"]) * (b["max_y"] - b["min_y"]) for _, _, b, _ in candidates
#         )
#         total_pts = sum(b["point_count"] for _, _, b, _ in candidates)
#         summary_lines = "\n".join(
#             f"  • {fn}  ({b['point_count']:,} pts)" for fn, _, b, _ in candidates[:15]
#         )
#         if len(candidates) > 15:
#             summary_lines += f"\n  ...and {len(candidates) - 15} more"
#         warn_lines = ""
#         if skipped_dupes:
#             warn_lines += f"\n\n{len(skipped_dupes)} file(s) skipped (already exist): " + ", ".join(skipped_dupes[:5])
#         if failed_reads:
#             warn_lines += f"\n\n{len(failed_reads)} file(s) skipped (header read failed): " + ", ".join(f[0] for f in failed_reads[:5])

#         reply = QMessageBox.question(
#             self,
#             "Add Block Definitions",
#             (
#                 f"Add {len(candidates)} block(s) to the PRJ?\n\n"
#                 f"{summary_lines}\n\n"
#                 f"Total points: {total_pts:,}\n"
#                 f"Total area:   {total_area:.2f} m²\n\n"
#                 "Each file's header bounding box will be used as its block polygon."
#                 f"{warn_lines}"
#             ),
#             QMessageBox.Yes | QMessageBox.No,
#             QMessageBox.Yes,
#         )
#         if reply != QMessageBox.Yes:
#             return

#         # ── Read PRJ file ─────────────────────────────────────────────────────
#         try:
#             with open(self.current_prj_path, "r", encoding="utf-8") as fh:
#                 prj_content = fh.read()
#         except Exception as exc:
#             QMessageBox.critical(
#                 self,
#                 "Read Error",
#                 f"Could not read the PRJ file:\n\nError: {exc}",
#             )
#             return

#         # ── Build all new block sections ────────────────────────────────────────
#         group_first = self._get_next_group_first(prj_content)
#         if prj_content and not prj_content.endswith("\n"):
#             prj_content += "\n"

#         new_sections = [
#             self._build_block_section_text(filename, polygon, group_first + i * 1_000_000)
#             for i, (filename, _, _, polygon) in enumerate(candidates)
#         ]
#         new_content = prj_content + "".join(new_sections)

#         # ── Write to backend working copy (real .prj untouched until Save) ────
#         try:
#             self._write_prj_to_working(new_content)
#             print(f"  ✅ {len(candidates)} block(s) appended to backend working copy")
#         except Exception as exc:
#             QMessageBox.critical(
#                 self,
#                 "Write Error",
#                 f"Failed to buffer the PRJ changes:\n\nError: {exc}\n\n"
#                 "The original file has NOT been modified.",
#             )
#             return

#         # ── Update in-memory state ────────────────────────────────────────────
#         try:
#             for filename, stem, bounds, polygon in candidates:
#                 avg_x = (bounds["min_x"] + bounds["max_x"]) / 2.0
#                 avg_y = (bounds["min_y"] + bounds["max_y"]) / 2.0
#                 area_m2 = (bounds["max_x"] - bounds["min_x"]) * (bounds["max_y"] - bounds["min_y"])
#                 new_entry = {
#                     "label": stem,
#                     "easting": f"{avg_x:.2f}",
#                     "northing": f"{avg_y:.2f}",
#                     "description": "Boundary: 4 points",
#                     "boundary_coords": [(x, y) for x, y in polygon[:-1]],
#                 }
#                 new_prj_idx = len(self.prj_data)
#                 self.prj_data.append(new_entry)
#                 self._all_row_data.append(
#                     (stem, f"{bounds['point_count']:,}", f"{area_m2:.2f}", None, None, new_prj_idx)
#                 )

#             self._sort_header._sort_states = {}
#             self._sort_header.viewport().update()
#             self._repopulate_table(self._all_row_data)
#             self._on_search_changed(self.search_box.text())
#             print(f"  ✅ In-memory state updated: {len(self.prj_data)} blocks")
#         except Exception as exc:
#             print(f"  ⚠️ Failed to refresh table after add: {exc}")
#             QMessageBox.warning(
#                 self,
#                 "Display Refresh Issue",
#                 "The block(s) were added to the PRJ file successfully, but the table\n"
#                 "could not refresh automatically.\n\n"
#                 "Please close and reopen the PRJ Block Identifier to see the change.",
#             )
#             return

#         result_msg = (
#             f"{len(candidates)} block definition(s) added successfully.\n\n"
#             f"Total points: {total_pts:,}\n"
#             f"Total area:   {total_area:.2f} m²\n\n"
#             f"Backup saved as:  {os.path.basename(bak_path)}"
#         )
#         if skipped_dupes or failed_reads:
#             result_msg += "\n\nSkipped:"
#             if skipped_dupes:
#                 result_msg += f"\n  • {len(skipped_dupes)} duplicate(s)"
#             if failed_reads:
#                 result_msg += f"\n  • {len(failed_reads)} header read failure(s)"
#         if hasattr(self.app, "statusBar"):
#             self.app.statusBar().showMessage(result_msg.replace("\n", "  "), 5000)

#     # ── Rename Definition ─────────────────────────────────────────────────────

#     def rename_block_definition(self):
#         """Rename a single block definition in the .prj file on disk.

#         Only the 'Block <filename>' line in the .prj manifest is changed.
#         The LAZ/LAS file on disk is NOT renamed.  A .prj.bak backup is
#         written before any change is saved.
#         """
#         # ── Guard: PRJ must be loaded ─────────────────────────────────────────
#         if not self.current_prj_path:
#             QMessageBox.warning(
#                 self, "No PRJ Loaded",
#                 "Please load a PRJ file before renaming a block definition.",
#             )
#             return
#         if not os.path.isfile(self.current_prj_path):
#             QMessageBox.critical(
#                 self, "PRJ File Not Found",
#                 f"The PRJ file no longer exists:\n\n{self.current_prj_path}",
#             )
#             return

#         # ── Guard: exactly one row must be selected ───────────────────────────
#         selected_indexes = self.table.selectionModel().selectedRows()
#         visual_rows = {idx.row() for idx in selected_indexes}
#         if len(visual_rows) != 1:
#             QMessageBox.warning(
#                 self,
#                 "Single Selection Required",
#                 "Please select exactly one block to rename.",
#             )
#             return

#         vrow = next(iter(visual_rows))
#         label_item = self.table.item(vrow, 0)
#         if label_item is None:
#             return
#         prj_idx = label_item.data(Qt.UserRole)
#         if prj_idx is None:
#             return
#         try:
#             prj_idx = int(prj_idx)
#         except (TypeError, ValueError):
#             return
#         if prj_idx < 0 or prj_idx >= len(self.prj_data):
#             return
#         old_label = self.prj_data[prj_idx].get("label", "").strip()
#         if not old_label:
#             return

#         # ── Guard: block must not be locked ───────────────────────────────────
#         if self.prj_data[prj_idx].get("locked", False):
#             QMessageBox.warning(
#                 self, "Block Locked",
#                 f"'{old_label}' is locked.\n\nUse Block → Release lock before renaming it."
#             )
#             return

#         # ── Read PRJ file ─────────────────────────────────────────────────────
#         try:
#             with open(self.current_prj_path, "r", encoding="utf-8") as fh:
#                 prj_content = fh.read()
#         except Exception as exc:
#             QMessageBox.critical(
#                 self, "Read Error",
#                 f"Could not read the PRJ file:\n\nError: {exc}",
#             )
#             return

#         # Detect which extension the existing block line uses
#         old_ext = ".laz"
#         for ext in (".laz", ".las"):
#             if (
#                 f"Block {old_label}{ext}" in prj_content
#                 or f"Block {old_label}{ext.upper()}" in prj_content
#             ):
#                 old_ext = ext
#                 break

#         # ── Prompt for new name ───────────────────────────────────────────────
#         from PySide6.QtWidgets import QInputDialog
#         new_stem, ok = QInputDialog.getText(
#             self,
#             "Rename Block Definition",
#             (
#                 f"Current name:  {old_label}\n\n"
#                 "Enter the new block name (stem only, no file extension):"
#             ),
#             text=old_label,
#         )
#         if not ok:
#             return
#         new_stem = new_stem.strip()

#         if not new_stem:
#             QMessageBox.warning(self, "Invalid Name", "Block name cannot be empty.")
#             return
#         if new_stem == old_label:
#             return  # no change requested
#         if any(ch in new_stem for ch in r'/\:*?"<>|'):
#             QMessageBox.warning(
#                 self, "Invalid Name",
#                 f"Block name contains invalid characters:\n  {new_stem}",
#             )
#             return
#         if "." in new_stem:
#             QMessageBox.warning(
#                 self, "Invalid Name",
#                 "Enter just the stem — do not include a file extension.",
#             )
#             return

#         # ── Guard: no duplicates ──────────────────────────────────────────────
#         existing_upper = {e.get("label", "").strip().upper() for e in self.prj_data}
#         if new_stem.strip().upper() in existing_upper:
#             QMessageBox.warning(
#                 self, "Duplicate Name",
#                 f"A block named '{new_stem}' already exists in this PRJ.",
#             )
#             return

#         new_filename = new_stem + old_ext

#         # ── Build modified content ────────────────────────────────────────────
#         try:
#             new_content = self._rename_block_in_prj_text(
#                 prj_content, old_label, new_filename
#             )
#         except Exception as exc:
#             QMessageBox.critical(
#                 self, "Parse Error",
#                 f"Failed to process PRJ content:\n\nError: {exc}",
#             )
#             return

#         if new_content == prj_content:
#             QMessageBox.information(
#                 self, "No Changes",
#                 "The old block name was not found in the PRJ file text.\n"
#                 "No changes were written.",
#             )
#             return

#         # ── Write to backend working copy (real .prj untouched until Save) ────
#         try:
#             self._write_prj_to_working(new_content)
#             print(f"  ✅ Block renamed in backend working copy: {old_label} → {new_stem}")
#         except Exception as exc:
#             QMessageBox.critical(
#                 self, "Write Error",
#                 f"Failed to buffer the PRJ changes:\n\nError: {exc}\n\n"
#                 "The original file has NOT been modified.",
#             )
#             return

#         # ── Update in-memory state ────────────────────────────────────────────
#         try:
#             self.prj_data[prj_idx]["label"] = new_stem
#             for i, row in enumerate(self._all_row_data):
#                 if row[0].strip().upper() == old_label.upper():
#                     self._all_row_data[i] = (
#                         new_stem, row[1], row[2], row[3], row[4], row[5]
#                     )
#                     break

#             self._sort_header._sort_states = {}
#             self._sort_header.viewport().update()
#             self._repopulate_table(self._all_row_data)
#             self._on_search_changed(self.search_box.text())
#             print(f"  ✅ Table refreshed after rename")
#         except Exception as exc:
#             print(f"  ⚠️ Failed to refresh table after rename: {exc}")
#             QMessageBox.warning(
#                 self, "Display Refresh Issue",
#                 "The PRJ was saved, but the table could not refresh automatically.\n\n"
#                 "Please reload the PRJ file to see the updated name.",
#             )
#             return

#         if hasattr(self.app, "statusBar"):
#             self.app.statusBar().showMessage(
#                 f"Renamed '{old_label}' → '{new_stem}'", 5000
#             )

#     # ── Export Sub-PRJ ────────────────────────────────────────────────────────

#     def export_selected_blocks_prj(self):
#         """Export selected block definitions to a new standalone PRJ file.

#         The original PRJ header (scanner settings, storage format, etc.) is
#         preserved.  Only the selected block sections are written to the new
#         file.  LAZ/LAS files on disk are never copied or modified.
#         """
#         # ── Guard: PRJ must be loaded ─────────────────────────────────────────
#         if not self.current_prj_path:
#             QMessageBox.warning(
#                 self, "No PRJ Loaded",
#                 "Please load a PRJ file before exporting block definitions.",
#             )
#             return
#         if not os.path.isfile(self.current_prj_path):
#             QMessageBox.critical(
#                 self, "PRJ File Not Found",
#                 f"The PRJ file no longer exists:\n\n{self.current_prj_path}",
#             )
#             return

#         # ── Guard: selection ──────────────────────────────────────────────────
#         selected_indexes = self.table.selectionModel().selectedRows()
#         if not selected_indexes:
#             QMessageBox.warning(
#                 self, "No Selection",
#                 "Select one or more blocks to export.",
#             )
#             return

#         # ── Collect labels ────────────────────────────────────────────────────
#         visual_rows = sorted({idx.row() for idx in selected_indexes})
#         labels_to_export = []
#         for vrow in visual_rows:
#             label_item = self.table.item(vrow, 0)
#             if label_item is None:
#                 continue
#             prj_idx = label_item.data(Qt.UserRole)
#             if prj_idx is None:
#                 continue
#             try:
#                 prj_idx = int(prj_idx)
#             except (TypeError, ValueError):
#                 continue
#             if 0 <= prj_idx < len(self.prj_data):
#                 lbl = self.prj_data[prj_idx].get("label", "").strip()
#                 if lbl and lbl not in labels_to_export:
#                     labels_to_export.append(lbl)

#         if not labels_to_export:
#             QMessageBox.warning(
#                 self, "Nothing to Export",
#                 "Could not resolve any block labels from the current selection.\n"
#                 "The table may be out of sync — try reloading the PRJ file.",
#             )
#             return

#         # ── File-save dialog ──────────────────────────────────────────────────
#         base = os.path.splitext(os.path.basename(self.current_prj_path))[0]
#         suggested = os.path.join(
#             os.path.dirname(self.current_prj_path),
#             f"{base}_sub_{len(labels_to_export)}blocks.prj",
#         )
#         save_path, _ = QFileDialog.getSaveFileName(
#             self,
#             "Export Sub-PRJ — Save As",
#             suggested,
#             "PRJ Files (*.prj);;All Files (*.*)",
#         )
#         if not save_path:
#             return  # user cancelled

#         # ── Read source PRJ ───────────────────────────────────────────────────
#         try:
#             with open(self.current_prj_path, "r", encoding="utf-8") as fh:
#                 prj_content = fh.read()
#         except Exception as exc:
#             QMessageBox.critical(
#                 self, "Read Error",
#                 f"Could not read the source PRJ:\n\nError: {exc}",
#             )
#             return

#         # ── Build new PRJ content: header + selected blocks ───────────────────
#         try:
#             labels_upper = {lbl.strip().upper() for lbl in labels_to_export}
#             header_text = self._extract_header_from_prj_text(prj_content)
#             blocks_text = self._extract_blocks_from_prj_text(prj_content, labels_upper)
#             new_content = header_text + blocks_text
#         except Exception as exc:
#             QMessageBox.critical(
#                 self, "Parse Error",
#                 f"Failed to process PRJ content:\n\nError: {exc}",
#             )
#             return

#         if not blocks_text.strip():
#             QMessageBox.warning(
#                 self, "Nothing Exported",
#                 "The selected block label(s) were not found as 'Block' entries "
#                 "in the PRJ text.\nNo file was written.",
#             )
#             return

#         # ── Write new PRJ file ────────────────────────────────────────────────
#         try:
#             with open(save_path, "w", encoding="utf-8", newline="\n") as fh:
#                 fh.write(new_content)
#             print(f"  ✅ Sub-PRJ written: {save_path}")
#         except Exception as exc:
#             QMessageBox.critical(
#                 self, "Write Error",
#                 f"Could not write the sub-PRJ file:\n\n{save_path}\n\nError: {exc}",
#             )
#             return

#         if hasattr(self.app, "statusBar"):
#             self.app.statusBar().showMessage(
#                 f"Exported {len(labels_to_export)} block(s) to {os.path.basename(save_path)}", 5000
#             )

#     # ── Static helpers shared by Add / Rename / Export / Delete ──────────────

#     @staticmethod
#     def _read_laz_header_bounds(file_path):
#         """Read XY extents and point count from a LAZ/LAS file header only.

#         Uses laspy header read — no points are loaded into memory.
#         Works even on billion-point files in under a second.
#         """
#         import laspy
#         with laspy.open(file_path) as las:
#             h = las.header
#             return {
#                 "min_x": float(h.mins[0]),
#                 "max_x": float(h.maxs[0]),
#                 "min_y": float(h.mins[1]),
#                 "max_y": float(h.maxs[1]),
#                 "point_count": int(h.point_count),
#             }

#     @staticmethod
#     def _get_next_group_first(prj_content):
#         """Return the next available GroupFirst value (max existing + 1 000 000).

#         Falls back to 1 000 000 when no existing GroupFirst lines are found.
#         """
#         import re
#         matches = re.findall(r"^GroupFirst=(\d+)", prj_content, re.MULTILINE)
#         if not matches:
#             return 1_000_000
#         return max(int(m) for m in matches) + 1_000_000

#     @staticmethod
#     def _build_block_section_text(filename, polygon_coords, group_first,
#                                   group_count=1_000_000):
#         """Build the text for one block section to append to a .prj file."""
#         lines = [
#             f"Block {filename}",
#             f"GroupFirst={group_first}",
#             f"GroupCount={group_count}",
#         ]
#         for x, y in polygon_coords:
#             lines.append(f" {x:.4f} {y:.4f}")
#         lines.append("")  # trailing blank line required by TerraScan parser
#         return "\n".join(lines) + "\n"

#     @staticmethod
#     def _rename_block_in_prj_text(content, old_label, new_filename):
#         """Replace the 'Block <old_label>.<ext>' line with 'Block <new_filename>'.

#         Matches case-insensitively on the extension (.laz/.las) while keeping
#         the rest of the file byte-for-byte identical.
#         """
#         import re
#         # Primary: match "Block <label>.laz" or "Block <label>.las" (any case)
#         pattern = re.compile(
#             r"^(Block[ \t]+)"
#             + re.escape(old_label)
#             + r"\.[lL][aA][zZsS][ \t]*$",
#             re.MULTILINE,
#         )
#         result, n = pattern.subn(f"Block {new_filename}", content)
#         if n == 0:
#             # Fallback: block line with no extension at all
#             pattern2 = re.compile(
#                 r"^Block[ \t]+" + re.escape(old_label) + r"[ \t]*$",
#                 re.MULTILINE,
#             )
#             result, _ = pattern2.subn(f"Block {new_filename}", content)
#         return result

#     @staticmethod
#     def _extract_header_from_prj_text(content):
#         """Return every line before the first 'Block ' entry (the file header)."""
#         lines = content.splitlines(keepends=True)
#         header = []
#         for line in lines:
#             if line.strip().startswith("Block "):
#                 break
#             header.append(line)
#         return "".join(header)

#     @staticmethod
#     def _extract_blocks_from_prj_text(content, labels_upper):
#         """Return only the block sections whose labels are in labels_upper.

#         Args:
#             content:       Full .prj file text.
#             labels_upper:  Uppercase label set (stems without file extension).

#         Returns:
#             Text containing only the matching block sections.
#         """
#         if not labels_upper:
#             return ""
#         lines = content.splitlines(keepends=True)
#         result = []
#         keeping = False
#         for line in lines:
#             stripped = line.strip()
#             if stripped.startswith("Block "):
#                 fname = stripped[6:].strip()
#                 stem = fname
#                 for ext in (".laz", ".las"):
#                     if stem.lower().endswith(ext):
#                         stem = stem[: len(stem) - len(ext)]
#                         break
#                 keeping = stem.strip().upper() in labels_upper
#             if keeping:
#                 result.append(line)
#         return "".join(result)

#     @staticmethod
#     def _polygon_target_xy(points_xy):
#         """Return a stable target point for PRJ block navigation.

#         The average of boundary vertices can sit outside a concave polygon, so
#         this prefers a true polygon centroid and falls back to an interior-ish
#         probe when necessary.
#         """
#         pts = [(float(x), float(y)) for x, y in points_xy if x is not None and y is not None]
#         if not pts:
#             return (0.0, 0.0)
#         if len(pts) == 1:
#             return pts[0]
#         if len(pts) == 2:
#             return ((pts[0][0] + pts[1][0]) / 2.0, (pts[0][1] + pts[1][1]) / 2.0)

#         closed = list(pts)
#         if closed[0] != closed[-1]:
#             closed.append(closed[0])

#         twice_area = 0.0
#         cx = 0.0
#         cy = 0.0
#         for i in range(len(closed) - 1):
#             x0, y0 = closed[i]
#             x1, y1 = closed[i + 1]
#             cross = x0 * y1 - x1 * y0
#             twice_area += cross
#             cx += (x0 + x1) * cross
#             cy += (y0 + y1) * cross

#         if abs(twice_area) > 1e-12:
#             centroid = (cx / (3.0 * twice_area), cy / (3.0 * twice_area))
#             if _point_in_polygon_xy(centroid[0], centroid[1], pts):
#                 return centroid

#         min_x = min(x for x, _ in pts)
#         max_x = max(x for x, _ in pts)
#         min_y = min(y for _, y in pts)
#         max_y = max(y for _, y in pts)

#         probes = [
#             ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0),
#             (min_x + (max_x - min_x) * 0.5, min_y + (max_y - min_y) * 0.5),
#             (min_x + (max_x - min_x) * 0.25, min_y + (max_y - min_y) * 0.25),
#             (min_x + (max_x - min_x) * 0.75, min_y + (max_y - min_y) * 0.75),
#             (min_x + (max_x - min_x) * 0.1, min_y + (max_y - min_y) * 0.9),
#         ]
#         for probe in probes:
#             if _point_in_polygon_xy(probe[0], probe[1], pts):
#                 return probe
#         return probes[0]

#     # ── Delete Definition ─────────────────────────────────────────────────────

#     def delete_block_definition(self):
#         """Permanently remove selected block definition(s) from the .prj file on disk.

#         Only the .prj manifest text is modified — the actual LAZ/LAS files are
#         never touched.  A .prj.bak backup is written before any change is saved.
#         The in-memory block table is updated immediately after a successful write
#         so the user sees the result without having to reload.
#         """
#         # ── Guard: a PRJ file must be loaded ─────────────────────────────────
#         if not self.current_prj_path:
#             QMessageBox.warning(
#                 self,
#                 "No PRJ Loaded",
#                 "Please load a PRJ file before attempting to delete block definitions.",
#             )
#             return

#         if not os.path.isfile(self.current_prj_path):
#             QMessageBox.critical(
#                 self,
#                 "PRJ File Not Found",
#                 f"The PRJ file no longer exists on disk:\n\n"
#                 f"{self.current_prj_path}\n\n"
#                 "Please reload the file.",
#             )
#             return

#         # ── Guard: at least one row must be selected ──────────────────────────
#         selected_indexes = self.table.selectionModel().selectedRows()
#         if not selected_indexes:
#             QMessageBox.warning(
#                 self,
#                 "No Selection",
#                 "Please select one or more blocks in the list\n"
#                 "before clicking Delete Definition.",
#             )
#             return

#         # ── Resolve selected visual rows → unique block labels ────────────────
#         visual_rows = sorted({idx.row() for idx in selected_indexes})
#         labels_to_delete = []
#         for vrow in visual_rows:
#             label_item = self.table.item(vrow, 0)
#             if label_item is None:
#                 continue
#             prj_idx = label_item.data(Qt.UserRole)
#             if prj_idx is None:
#                 continue
#             try:
#                 prj_idx = int(prj_idx)
#             except (TypeError, ValueError):
#                 continue
#             if prj_idx < 0 or prj_idx >= len(self.prj_data):
#                 continue
#             label = self.prj_data[prj_idx].get("label", "").strip()
#             if label and label not in labels_to_delete:
#                 labels_to_delete.append(label)

#         if not labels_to_delete:
#             QMessageBox.warning(
#                 self,
#                 "Nothing to Delete",
#                 "Could not resolve any valid block labels from the selection.\n"
#                 "The table may be out of sync — try reloading the PRJ file.",
#             )
#             return

#         # ── Guard: refuse if any selected block is locked ─────────────────────
#         locked_labels = [
#             block.get("label", "").strip()
#             for block in self.prj_data
#             if block.get("label", "").strip() in labels_to_delete and block.get("locked", False)
#         ]
#         if locked_labels:
#             QMessageBox.warning(
#                 self, "Block(s) Locked",
#                 "The following selected block(s) are locked and cannot be deleted:\n\n"
#                 + "\n".join(f"• {lbl}" for lbl in locked_labels)
#                 + "\n\nUse Block → Release lock first."
#             )
#             return

#         # ── Confirmation dialog ───────────────────────────────────────────────
#         prj_name = os.path.basename(self.current_prj_path)
#         preview_lines = "\n".join(f"  • {lbl}" for lbl in labels_to_delete[:12])
#         if len(labels_to_delete) > 12:
#             preview_lines += f"\n  … and {len(labels_to_delete) - 12} more"

#         reply = QMessageBox.warning(
#             self,
#             "Delete Block Definition(s)",
#             (
#                 f"This will permanently remove {len(labels_to_delete)} block "
#                 f"definition(s) from:\n\n  {prj_name}\n\n"
#                 f"{preview_lines}\n\n"
#                 "The LAZ/LAS files on disk are NOT deleted.\n"
#                 "Changes are buffered in a backend working copy and only written\n"
#                 "to the real PRJ when you press 'Save project'.\n\n"
#                 "This action cannot be undone.  Continue?"
#             ),
#             QMessageBox.Yes | QMessageBox.No,
#             QMessageBox.No,
#         )
#         if reply != QMessageBox.Yes:
#             return

#         # ── Read the current .prj text ────────────────────────────────────────
#         try:
#             with open(self.current_prj_path, "r", encoding="utf-8") as fh:
#                 original_content = fh.read()
#         except Exception as exc:
#             QMessageBox.critical(
#                 self,
#                 "Read Error",
#                 f"Could not read the PRJ file:\n\n{self.current_prj_path}\n\n"
#                 f"Error: {exc}",
#             )
#             return

#         # ── Build the modified text ───────────────────────────────────────────
#         try:
#             labels_set_upper = {lbl.strip().upper() for lbl in labels_to_delete}
#             new_content = self._remove_blocks_from_prj_text(original_content, labels_set_upper)
#         except Exception as exc:
#             QMessageBox.critical(
#                 self,
#                 "Parse Error",
#                 f"Failed to parse the PRJ content:\n\nError: {exc}",
#             )
#             return

#         # Sanity-check: did anything actually change?
#         if new_content == original_content:
#             QMessageBox.information(
#                 self,
#                 "No Changes Written",
#                 "The selected block label(s) were not found as 'Block' entries in the "
#                 "PRJ file.\nNo changes were made to disk.",
#             )
#             return

#         # ── Write to backend working copy (real .prj untouched until Save) ────
#         try:
#             self._write_prj_to_working(new_content)
#             print(f"  ✅ Deleted block(s) written to backend working copy")
#         except Exception as exc:
#             QMessageBox.critical(
#                 self,
#                 "Write Error",
#                 f"Failed to buffer the updated PRJ file:\n\nError: {exc}\n\n"
#                 "The original file has NOT been modified.",
#             )
#             return

#         # ── Clear VTK highlights for deleted blocks ───────────────────────────
#         for lbl in labels_to_delete:
#             try:
#                 if (
#                     hasattr(self, "_highlighted_blocks")
#                     and isinstance(self._highlighted_blocks, dict)
#                     and lbl in self._highlighted_blocks
#                 ):
#                     self.clear_highlight_for_block(lbl)
#             except Exception:
#                 pass  # highlight cleanup is non-critical

#         # ── Update the in-memory block table ──────────────────────────────────
#         try:
#             labels_set_norm = {lbl.strip().upper() for lbl in labels_to_delete}

#             # Filter prj_data and _all_row_data, keeping only blocks not deleted
#             new_prj_data = [
#                 entry for entry in self.prj_data
#                 if entry.get("label", "").strip().upper() not in labels_set_norm
#             ]
#             kept_rows = [
#                 row for row in self._all_row_data
#                 if row[0].strip().upper() not in labels_set_norm
#             ]

#             # Re-build the prj_idx (column 5 in each row tuple) so every kept row
#             # points to the correct index in the new (shorter) prj_data list.
#             new_idx_map = {
#                 entry["label"].strip().upper(): i
#                 for i, entry in enumerate(new_prj_data)
#             }
#             reindexed = []
#             for row in kept_rows:
#                 norm = row[0].strip().upper()
#                 new_idx = new_idx_map.get(norm, 0)
#                 reindexed.append((row[0], row[1], row[2], row[3], row[4], new_idx))

#             self.prj_data = new_prj_data
#             self._all_row_data = reindexed

#             # Reset sort arrows to neutral and repopulate the table
#             self._sort_header._sort_states = {}
#             self._sort_header.viewport().update()
#             self._repopulate_table(self._all_row_data)

#             # Re-apply any active search text
#             self._on_search_changed(self.search_box.text())

#             print(
#                 f"  ✅ In-memory PRJ state updated: "
#                 f"{len(self.prj_data)} block(s) remain"
#             )
#         except Exception as exc:
#             # The disk write succeeded, so this is non-fatal.
#             # Ask the user to reload to see the updated state.
#             print(f"  ⚠️ Failed to refresh in-memory PRJ state: {exc}")
#             QMessageBox.warning(
#                 self,
#                 "Display Refresh Issue",
#                 "The PRJ file was saved successfully, but the block list could not "
#                 "be refreshed automatically.\n\n"
#                 "Please close and reopen the PRJ Block Identifier to see the changes.",
#             )
#             return

#         n = len(labels_to_delete)
#         remaining = len(self.prj_data)
#         if hasattr(self.app, "statusBar"):
#             self.app.statusBar().showMessage(
#                 f"Deleted {n} block(s) — {remaining} remaining in project", 5000
#             )

#     @staticmethod
#     def _remove_blocks_from_prj_text(content: str, labels_upper: set) -> str:
#         """Return new .prj file content with the named block sections removed.

#         Processes the file line-by-line.  When a 'Block <filename>' line is
#         encountered the label (filename without extension) is compared against
#         labels_upper (pre-normalised to uppercase).  If it matches, that line
#         and every subsequent line belonging to that block section (GroupFirst=,
#         GroupCount=, coordinate pairs, trailing blank line) are omitted from
#         the output.  Skipping stops as soon as the next 'Block ' header is
#         reached, at which point the new block is evaluated independently.

#         The file header ([TerraScan project] and all key=value settings) is
#         never touched.

#         Args:
#             content:       Full UTF-8 text of the .prj file.
#             labels_upper:  Set of uppercase label strings (filename without
#                            extension) that should be removed.

#         Returns:
#             Modified file text as a single string.
#         """
#         if not labels_upper:
#             return content

#         lines = content.splitlines(keepends=True)
#         result = []
#         skipping = False  # True while inside a block section to be removed

#         for line in lines:
#             stripped = line.strip()

#             if stripped.startswith("Block "):
#                 # Derive the label: everything after "Block ", strip extension
#                 fname = stripped[6:].strip()
#                 stem = fname
#                 for ext in (".laz", ".las"):
#                     if stem.lower().endswith(ext):
#                         stem = stem[: len(stem) - len(ext)]
#                         break
#                 skipping = stem.strip().upper() in labels_upper
#                 # Fall through: line is appended below only when skipping=False

#             if not skipping:
#                 result.append(line)

#         return "".join(result)

#     @staticmethod
#     def _replace_block_boundary_in_prj_text(content: str, label: str, new_polygon) -> str:
#         """Return new .prj content with one block's coordinate lines replaced.

#         The 'Block <filename>' line and its GroupFirst=/GroupCount= metadata
#         lines are left untouched — only the polygon coordinate lines that
#         follow are swapped for new_polygon (a list of (x, y) tuples, open
#         ring — the first point is NOT repeated at the end).

#         Args:
#             content:     Full UTF-8 text of the .prj file.
#             label:       Block label (filename without extension) to edit.
#             new_polygon: List of (x, y) tuples for the new boundary.

#         Returns:
#             Modified file text as a single string.
#         """
#         label_upper = label.strip().upper()
#         lines = content.splitlines(keepends=True)
#         result = []
#         in_target_block = False
#         skipping_old_coords = False

#         for line in lines:
#             stripped = line.strip()

#             if stripped.startswith("Block "):
#                 fname = stripped[6:].strip()
#                 stem = fname
#                 for ext in (".laz", ".las"):
#                     if stem.lower().endswith(ext):
#                         stem = stem[: len(stem) - len(ext)]
#                         break
#                 in_target_block = (stem.strip().upper() == label_upper)
#                 skipping_old_coords = False
#                 result.append(line)
#                 continue

#             if in_target_block and (stripped.startswith("GroupFirst=") or stripped.startswith("GroupCount=")):
#                 result.append(line)
#                 continue

#             if in_target_block and not skipping_old_coords:
#                 # First coordinate line after the metadata — write the new ring here
#                 skipping_old_coords = True
#                 for x, y in new_polygon:
#                     result.append(f" {x:.4f} {y:.4f}\n")
#                 # the closing point (repeat of first) to match existing file convention
#                 if new_polygon:
#                     fx, fy = new_polygon[0]
#                     result.append(f" {fx:.4f} {fy:.4f}\n")
#                 continue   # do not append the old coordinate line being replaced

#             if in_target_block and skipping_old_coords:
#                 continue   # skip remaining old coordinate lines for this block

#             result.append(line)

#         return "".join(result)

#     # ── Select by Fence ───────────────────────────────────────────────────────

#     def _fence_highlight_clear(self):
#         """Remove all fence-selection boundary-polygon highlights from the viewport."""
#         if not self._fence_highlight_actors:
#             return
#         try:
#             vtk_widget = getattr(self.app, "vtk_widget", None)
#             renderer = getattr(vtk_widget, "renderer", None)
#             for actor in self._fence_highlight_actors:
#                 try:
#                     if renderer is not None:
#                         renderer.RemoveActor(actor)
#                 except Exception:
#                     pass
#             self._fence_highlight_actors = []
#             if vtk_widget is not None:
#                 vtk_widget.GetRenderWindow().Render()
#         except Exception:
#             self._fence_highlight_actors = []

#     def _highlight_fence_selected_blocks(self, labels_upper: Set[str]):
#         """Draw bright boundary-polygon outlines for selected blocks (MicroStation-style).

#         Uses the boundary_coords already stored in prj_data — no DXF/SNT actor
#         lookup required.  Each selected block gets a cyan polyline at z=1 so it
#         renders on top of the existing SNT grid rectangles.
#         """
#         self._fence_highlight_clear()
#         if not labels_upper:
#             return
#         vtk_widget = getattr(self.app, "vtk_widget", None)
#         renderer = getattr(vtk_widget, "renderer", None)
#         if renderer is None:
#             return
#         try:
#             import vtk
#             for block in self.prj_data:
#                 lbl = block.get("label", "").strip().upper()
#                 if lbl not in labels_upper:
#                     continue
#                 coords = block.get("boundary_coords", [])
#                 if len(coords) < 2:
#                     continue

#                 # Build closed polyline in world coords at z=1 (above SNT grid)
#                 all_pts = list(coords)
#                 if all_pts[0] != all_pts[-1]:
#                     all_pts.append(all_pts[0])   # close the ring

#                 pts = vtk.vtkPoints()
#                 for x, y in all_pts:
#                     pts.InsertNextPoint(float(x), float(y), 1.0)

#                 n = pts.GetNumberOfPoints()
#                 poly_line = vtk.vtkPolyLine()
#                 poly_line.GetPointIds().SetNumberOfIds(n)
#                 for i in range(n):
#                     poly_line.GetPointIds().SetId(i, i)
#                 cells = vtk.vtkCellArray()
#                 cells.InsertNextCell(poly_line)

#                 pd = vtk.vtkPolyData()
#                 pd.SetPoints(pts)
#                 pd.SetLines(cells)

#                 mapper = vtk.vtkPolyDataMapper()
#                 mapper.SetInputData(pd)

#                 actor = vtk.vtkActor()
#                 actor.SetMapper(mapper)
#                 actor.GetProperty().SetColor(0.0, 1.0, 1.0)   # cyan — MicroStation selection colour
#                 actor.GetProperty().SetLineWidth(3.0)
#                 actor.GetProperty().SetOpacity(1.0)
#                 try:
#                     actor.PickableOff()
#                 except Exception:
#                     pass

#                 renderer.AddActor(actor)
#                 self._fence_highlight_actors.append(actor)

#             if self._fence_highlight_actors:
#                 vtk_widget.GetRenderWindow().Render()
#         except Exception as e:
#             print(f"⚠️ Fence boundary highlight failed: {e}")

#     def _toggle_fence_mode(self, checked: bool):
#         if checked:
#             self._fence_activate()
#         else:
#             self._fence_deactivate(cancel=True)

#     def _fence_activate(self):
#         if self._addb_active:
#             self._addb_deactivate(cancel=True)
#         if self._editdef_active:
#             self._editdef_deactivate(cancel=True)
#         vtk_widget = getattr(self.app, "vtk_widget", None)
#         if vtk_widget is None:
#             self._fence_action.blockSignals(True)
#             self._fence_action.setChecked(False)
#             self._fence_action.blockSignals(False)
#             return
#         self._fence_active = True
#         self._fence_start_screen = None
#         self._fence_current_screen = None
#         self._fence_actor = None
#         vtk_widget.installEventFilter(self)
#         vtk_widget.setCursor(Qt.CrossCursor)
#         if hasattr(self.app, "statusBar"):
#             self.app.statusBar().showMessage(
#                 "Select by Fence: drag a rectangle to select blocks — Esc or right-click to cancel",
#                 0,
#             )

#     def _fence_deactivate(self, *, cancel: bool = False):
#         if not self._fence_active:
#             self._fence_action.blockSignals(True)
#             self._fence_action.setChecked(False)
#             self._fence_action.blockSignals(False)
#             return
#         self._fence_active = False
#         self._fence_start_screen = None
#         self._fence_current_screen = None
#         if self._fence_mouse_grabbed:
#             try:
#                 vtk_widget = getattr(self.app, "vtk_widget", None)
#                 if vtk_widget is not None:
#                     vtk_widget.releaseMouse()
#             except Exception:
#                 pass
#             self._fence_mouse_grabbed = False
#         self._fence_remove_overlay()
#         vtk_widget = getattr(self.app, "vtk_widget", None)
#         if vtk_widget is not None:
#             vtk_widget.removeEventFilter(self)
#             vtk_widget.setCursor(Qt.ArrowCursor)
#         if hasattr(self.app, "statusBar"):
#             if cancel:
#                 self.app.statusBar().showMessage("Select by Fence cancelled", 2000)
#             else:
#                 self.app.statusBar().clearMessage()
#         self._fence_action.blockSignals(True)
#         self._fence_action.setChecked(False)
#         self._fence_action.blockSignals(False)

#     def _fence_remove_overlay(self):
#         if self._fence_actor is None:
#             return
#         try:
#             vtk_widget = getattr(self.app, "vtk_widget", None)
#             renderer = getattr(vtk_widget, "renderer", None)
#             if renderer is not None:
#                 renderer.RemoveActor2D(self._fence_actor)
#             if vtk_widget is not None:
#                 vtk_widget.GetRenderWindow().Render()
#         except Exception:
#             pass
#         self._fence_actor = None

#     def _fence_update_overlay(self, p0_vtk, p1_vtk):
#         """Draw/refresh a cyan rubberband rectangle in VTK display coords.

#         Removes any existing overlay actor and adds a fresh one in a single
#         render pass to avoid the stutter of two back-to-back renders.
#         """
#         try:
#             import vtk
#             vtk_widget = getattr(self.app, "vtk_widget", None)
#             renderer = getattr(vtk_widget, "renderer", None)
#             if renderer is None:
#                 return

#             # Remove old actor inline (no render yet)
#             if self._fence_actor is not None:
#                 try:
#                     renderer.RemoveActor2D(self._fence_actor)
#                 except Exception:
#                     pass
#                 self._fence_actor = None

#             x0, y0 = p0_vtk
#             x1, y1 = p1_vtk
#             # 5-point closed rectangle (same winding as _make_screen_polyline)
#             pts_screen = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
#             pts = vtk.vtkPoints()
#             for sx, sy in pts_screen:
#                 pts.InsertNextPoint(float(sx), float(sy), 0.0)
#             # Single vtkPolyLine — matches the pattern used by _make_screen_polyline
#             poly_line = vtk.vtkPolyLine()
#             poly_line.GetPointIds().SetNumberOfIds(len(pts_screen))
#             for i in range(len(pts_screen)):
#                 poly_line.GetPointIds().SetId(i, i)
#             cells = vtk.vtkCellArray()
#             cells.InsertNextCell(poly_line)
#             pd = vtk.vtkPolyData()
#             pd.SetPoints(pts)
#             pd.SetLines(cells)
#             coord = vtk.vtkCoordinate()
#             coord.SetCoordinateSystemToDisplay()
#             mapper = vtk.vtkPolyDataMapper2D()
#             mapper.SetInputData(pd)
#             mapper.SetTransformCoordinate(coord)
#             actor = vtk.vtkActor2D()
#             actor.SetMapper(mapper)
#             actor.GetProperty().SetColor(0.2, 0.8, 1.0)   # cyan
#             actor.GetProperty().SetLineWidth(2.0)
#             renderer.AddActor2D(actor)
#             self._fence_actor = actor
#             # Single render — remove + add in one pass
#             vtk_widget.GetRenderWindow().Render()
#         except Exception:
#             pass

#     def _fence_screen_to_world_xy(self, vtk_x: float, vtk_y: float):
#         """Convert a VTK display-coord point to world (x, y).  Returns None on error."""
#         try:
#             import vtk
#             renderer = getattr(getattr(self.app, "vtk_widget", None), "renderer", None)
#             if renderer is None:
#                 return None
#             coord = vtk.vtkCoordinate()
#             coord.SetCoordinateSystemToDisplay()
#             coord.SetValue(float(vtk_x), float(vtk_y), 0.0)
#             w = coord.GetComputedWorldValue(renderer)
#             return float(w[0]), float(w[1])
#         except Exception:
#             return None

#     def _fence_commit(self, p0_vtk, p1_vtk):
#         """Select PRJ blocks whose polygons overlap the drawn fence rectangle."""
#         w0 = self._fence_screen_to_world_xy(*p0_vtk)
#         w1 = self._fence_screen_to_world_xy(*p1_vtk)
#         if w0 is None or w1 is None:
#             return

#         wmin_x, wmax_x = min(w0[0], w1[0]), max(w0[0], w1[0])
#         wmin_y, wmax_y = min(w0[1], w1[1]), max(w0[1], w1[1])

#         # Ignore degenerate (single click with no drag)
#         if (wmax_x - wmin_x) < 1e-6 or (wmax_y - wmin_y) < 1e-6:
#             return

#         # Overlap mode: block hits if any vertex is inside fence OR bboxes overlap
#         matched: Set[str] = set()
#         for block in self.prj_data:
#             coords = block.get("boundary_coords", [])
#             if not coords:
#                 continue
#             hit = any(
#                 wmin_x <= bx <= wmax_x and wmin_y <= by <= wmax_y
#                 for bx, by in coords
#             )
#             if not hit:
#                 bxs = [c[0] for c in coords]
#                 bys = [c[1] for c in coords]
#                 hit = (
#                     min(bxs) <= wmax_x and max(bxs) >= wmin_x
#                     and min(bys) <= wmax_y and max(bys) >= wmin_y
#                 )
#             if hit:
#                 lbl = block.get("label", "").strip().upper()
#                 if lbl:
#                     matched.add(lbl)

#         if not matched:
#             if hasattr(self.app, "statusBar"):
#                 self.app.statusBar().showMessage(
#                     "Select by Fence: no blocks found in selected area", 3000
#                 )
#             return

#         # Apply selection to the table
#         from PySide6.QtCore import QItemSelectionModel
#         self.table.clearSelection()
#         sel_model = self.table.selectionModel()
#         first_idx = None
#         for trow in range(self.table.rowCount()):
#             item = self.table.item(trow, 0)
#             if item and item.text().strip().upper() in matched:
#                 idx = self.table.model().index(trow, 0)
#                 sel_model.select(idx, QItemSelectionModel.Select | QItemSelectionModel.Rows)
#                 if first_idx is None:
#                     first_idx = idx
#         if first_idx is not None:
#             self.table.scrollTo(first_idx)

#         # Highlight block boundary polygons in the viewport (MicroStation-style)
#         self._highlight_fence_selected_blocks(matched)

#         n = len(matched)
#         if hasattr(self.app, "statusBar"):
#             self.app.statusBar().showMessage(
#                 f"Select by Fence: {n} block{'s' if n != 1 else ''} selected", 4000
#             )

#     def eventFilter(self, obj, event):
#         if obj is getattr(self, "search_box", None):
#             if event.type() == QEvent.FocusIn:
#                 self._prj_search_focus_set(True)
#             elif event.type() == QEvent.FocusOut:
#                 self._prj_search_focus_set(False)
#             # Never consume the event. QLineEdit must receive its native focus,
#             # caret, selection, clear-button, and keyboard handling.
#             return False

#         vtk_widget = getattr(self.app, "vtk_widget", None)

#         if self._fence_active and obj is vtk_widget:
#             et = event.type()

#             if et == QEvent.KeyPress and event.key() == Qt.Key_Escape:
#                 self._fence_deactivate(cancel=True)
#                 return True

#             if et == QEvent.MouseButtonPress:
#                 if event.button() == Qt.LeftButton:
#                     pos = event.position() if hasattr(event, "position") else event.pos()
#                     qt_x, qt_y = int(pos.x()), int(pos.y())
#                     vtk_y = vtk_widget.height() - 1 - qt_y
#                     self._fence_start_screen = (qt_x, vtk_y)
#                     self._fence_current_screen = (qt_x, vtk_y)
#                     # Grab mouse so release is received even if cursor moves over dialog
#                     vtk_widget.grabMouse()
#                     self._fence_mouse_grabbed = True
#                     return True
#                 if event.button() == Qt.RightButton:
#                     self._fence_deactivate(cancel=True)
#                     return True

#             if et == QEvent.MouseMove and self._fence_start_screen is not None:
#                 pos = event.position() if hasattr(event, "position") else event.pos()
#                 qt_x, qt_y = int(pos.x()), int(pos.y())
#                 vtk_y = vtk_widget.height() - 1 - qt_y
#                 self._fence_current_screen = (qt_x, vtk_y)
#                 self._fence_update_overlay(self._fence_start_screen, self._fence_current_screen)
#                 return True  # consume: prevent VTK camera orbit during drag

#             if et == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
#                 if self._fence_mouse_grabbed:
#                     vtk_widget.releaseMouse()
#                     self._fence_mouse_grabbed = False
#                 if self._fence_start_screen is not None:
#                     pos = event.position() if hasattr(event, "position") else event.pos()
#                     qt_x, qt_y = int(pos.x()), int(pos.y())
#                     vtk_y = vtk_widget.height() - 1 - qt_y
#                     end = (qt_x, vtk_y)
#                     self._fence_remove_overlay()
#                     self._fence_commit(self._fence_start_screen, end)
#                     self._fence_deactivate(cancel=False)
#                     return True

#             return False

#         if self._addb_active and obj is vtk_widget:
#             et = event.type()

#             if et == QEvent.KeyPress and event.key() == Qt.Key_Escape:
#                 self._addb_finish_drawing()
#                 return True

#             if et == QEvent.MouseButtonPress:
#                 if event.button() == Qt.LeftButton:
#                     pos = event.position() if hasattr(event, "position") else event.pos()
#                     qt_x, qt_y = int(pos.x()), int(pos.y())
#                     vtk_y = vtk_widget.height() - 1 - qt_y
#                     self._addb_start_screen = (qt_x, vtk_y)
#                     # Grab mouse so release is received even if cursor drifts over the
#                     # floating PRJ dialog — prevents silently losing a drawn rectangle
#                     vtk_widget.grabMouse()
#                     self._addb_mouse_grabbed = True
#                     return True
#                 if event.button() == Qt.RightButton:
#                     self._addb_finish_drawing()
#                     return True

#             if et == QEvent.MouseMove and self._addb_start_screen is not None:
#                 pos = event.position() if hasattr(event, "position") else event.pos()
#                 qt_x, qt_y = int(pos.x()), int(pos.y())
#                 vtk_y = vtk_widget.height() - 1 - qt_y
#                 self._addb_update_drag_overlay(self._addb_start_screen, (qt_x, vtk_y))
#                 return True  # consume: prevent VTK camera orbit during drag

#             if et == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
#                 if self._addb_mouse_grabbed:
#                     vtk_widget.releaseMouse()
#                     self._addb_mouse_grabbed = False
#                 if self._addb_start_screen is not None:
#                     pos = event.position() if hasattr(event, "position") else event.pos()
#                     qt_x, qt_y = int(pos.x()), int(pos.y())
#                     vtk_y = vtk_widget.height() - 1 - qt_y
#                     end = (qt_x, vtk_y)
#                     self._addb_remove_drag_overlay()
#                     self._addb_commit_rect(self._addb_start_screen, end)
#                     self._addb_start_screen = None
#                     return True

#             return False

#         if self._editdef_active and obj is vtk_widget:
#             et = event.type()

#             if et == QEvent.KeyPress and event.key() == Qt.Key_Escape:
#                 self._editdef_deactivate(cancel=True)
#                 return True

#             if et == QEvent.MouseButtonPress:
#                 if event.button() == Qt.LeftButton:
#                     pos = event.position() if hasattr(event, "position") else event.pos()
#                     qt_x, qt_y = int(pos.x()), int(pos.y())
#                     vtk_y = vtk_widget.height() - 1 - qt_y
#                     self._editdef_start_screen = (qt_x, vtk_y)
#                     # Grab mouse so release is received even if cursor moves over dialog
#                     vtk_widget.grabMouse()
#                     self._editdef_mouse_grabbed = True
#                     return True
#                 if event.button() == Qt.RightButton:
#                     self._editdef_deactivate(cancel=True)
#                     return True

#             if et == QEvent.MouseMove and self._editdef_start_screen is not None:
#                 pos = event.position() if hasattr(event, "position") else event.pos()
#                 qt_x, qt_y = int(pos.x()), int(pos.y())
#                 vtk_y = vtk_widget.height() - 1 - qt_y
#                 self._editdef_update_drag_overlay(self._editdef_start_screen, (qt_x, vtk_y))
#                 return True  # consume: prevent VTK camera orbit during drag

#             if et == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
#                 if self._editdef_mouse_grabbed:
#                     vtk_widget.releaseMouse()
#                     self._editdef_mouse_grabbed = False
#                 if self._editdef_start_screen is not None:
#                     pos = event.position() if hasattr(event, "position") else event.pos()
#                     qt_x, qt_y = int(pos.x()), int(pos.y())
#                     vtk_y = vtk_widget.height() - 1 - qt_y
#                     end = (qt_x, vtk_y)
#                     self._editdef_remove_drag_overlay()
#                     self._editdef_commit(self._editdef_start_screen, end)
#                     self._editdef_start_screen = None
#                     self._editdef_deactivate(cancel=False)
#                     return True

#             return False

#         return super().eventFilter(obj, event)

#     # ── Add by Boundaries ────────────────────────────────────────────────────

#     def _toggle_addb_mode(self, checked: bool):
#         if checked:
#             self._addb_activate()
#         else:
#             self._addb_finish_drawing()

#     def _addb_activate(self):
#         if self._fence_active:
#             self._fence_deactivate(cancel=True)
#         if self._editdef_active:
#             self._editdef_deactivate(cancel=True)
#         vtk_widget = getattr(self.app, "vtk_widget", None)
#         if vtk_widget is None:
#             self._addb_action.blockSignals(True)
#             self._addb_action.setChecked(False)
#             self._addb_action.blockSignals(False)
#             return
#         self._addb_active = True
#         self._addb_start_screen = None
#         self._addb_drag_actor = None
#         vtk_widget.installEventFilter(self)
#         vtk_widget.setCursor(Qt.CrossCursor)
#         if hasattr(self.app, "statusBar"):
#             self.app.statusBar().showMessage(
#                 "Add by Boundaries: drag rectangle(s) to define new blocks — "
#                 "Esc or right-click to finish",
#                 0,
#             )

#     def _addb_get_visible_snt_polygons(self):
#         """SNT block polygons restricted to layers currently checked ON in the
#         SNT/SBM attachment panel.

#         app.snt_block_polygons holds every BL/GRID/BBOX polygon parsed from
#         every loaded attachment, regardless of which layers the user has
#         toggled visible. Add-by-Boundaries must only capture blocks from the
#         layer(s) the user actually turned on (e.g. just the block layer),
#         not also GRID/BBOX outlines that happen to still be loaded but hidden.
#         """
#         all_polys = getattr(self.app, "snt_block_polygons", None) or []
#         if not all_polys:
#             return []
#         dlg = getattr(self.app, "snt_dialog", None)
#         items = getattr(dlg, "snt_items", None) if dlg is not None else None
#         if not items:
#             # No layer-visibility info available (dialog never opened) —
#             # fall back to the full list rather than silently dropping blocks.
#             return all_polys

#         by_filename = {}
#         for item in items:
#             try:
#                 by_filename[item.snt_path.name] = item
#             except Exception:
#                 continue

#         visible = []
#         for blk in all_polys:
#             fname = os.path.basename(blk.get("snt_filename") or "")
#             item = by_filename.get(fname)
#             if item is None:
#                 continue  # attachment no longer tracked/loaded — skip
#             try:
#                 if not item.is_checked():
#                     continue  # whole file toggled off
#             except Exception:
#                 pass
#             sel = getattr(item, "selected_layers", None)
#             if sel is not None and blk.get("poly_layer") not in sel:
#                 continue  # this specific layer toggled off
#             visible.append(blk)
#         return visible

#     def _add_blocks_from_snt(self):
#         """Add every loaded SNT block polygon to the PRJ in one step, skipping
#         any block whose grid_name already exists as a PRJ label."""
#         snt_polygons = self._addb_get_visible_snt_polygons()
#         if not snt_polygons:
#             QMessageBox.information(
#                 self, "No SNT Data",
#                 "No SNT block polygons are loaded.\n\n"
#                 "Load an SNT file first using File → Load SNT."
#             )
#             return
#         if not self.current_prj_path:
#             QMessageBox.warning(
#                 self, "No PRJ Loaded",
#                 "Please load a PRJ file before adding blocks."
#             )
#             return
#         if not os.path.isfile(self.current_prj_path):
#             QMessageBox.critical(
#                 self, "PRJ File Not Found",
#                 f"The PRJ file no longer exists on disk:\n\n{self.current_prj_path}"
#             )
#             return

#         existing_upper = {e.get("label", "").strip().upper() for e in self.prj_data}
#         candidates = []
#         for blk in snt_polygons:
#             g = (blk.get("grid_name") or "").strip()
#             pts = blk.get("points_2d") or []
#             if not pts:
#                 continue
#             if g.upper() in existing_upper:
#                 continue  # already in PRJ
#             candidates.append((g, pts))

#         if not candidates:
#             QMessageBox.information(
#                 self, "Nothing to Add",
#                 "All SNT blocks are already present in the PRJ file."
#             )
#             return

#         reply = QMessageBox.question(
#             self, "Add All SNT Blocks",
#             f"Add {len(candidates)} SNT block(s) to the PRJ file?",
#             QMessageBox.Yes | QMessageBox.No,
#         )
#         if reply != QMessageBox.Yes:
#             return

#         suggested_prefix, suggested_first_number = self._detect_prefix_and_next_number()
#         self._addb_commit_new_blocks(
#             rects=[], prefix=suggested_prefix,
#             first_number=suggested_first_number, snt_blocks=candidates
#         )

#     def _addb_deactivate(self, *, cancel: bool = False):
#         """Stop drawing mode WITHOUT touching pending rectangles (caller decides)."""
#         if not self._addb_active:
#             self._addb_action.blockSignals(True)
#             self._addb_action.setChecked(False)
#             self._addb_action.blockSignals(False)
#             return
#         self._addb_active = False
#         self._addb_start_screen = None
#         if self._addb_mouse_grabbed:
#             try:
#                 vtk_widget = getattr(self.app, "vtk_widget", None)
#                 if vtk_widget is not None:
#                     vtk_widget.releaseMouse()
#             except Exception:
#                 pass
#             self._addb_mouse_grabbed = False
#         self._addb_remove_drag_overlay()
#         vtk_widget = getattr(self.app, "vtk_widget", None)
#         if vtk_widget is not None:
#             vtk_widget.removeEventFilter(self)
#             vtk_widget.setCursor(Qt.ArrowCursor)
#         if hasattr(self.app, "statusBar"):
#             if cancel:
#                 self.app.statusBar().showMessage("Add by Boundaries cancelled", 2000)
#             else:
#                 self.app.statusBar().clearMessage()
#         self._addb_action.blockSignals(True)
#         self._addb_action.setChecked(False)
#         self._addb_action.blockSignals(False)
#         if cancel:
#             self._addb_clear_pending()

#     def _addb_remove_drag_overlay(self):
#         if self._addb_drag_actor is None:
#             return
#         try:
#             vtk_widget = getattr(self.app, "vtk_widget", None)
#             renderer = getattr(vtk_widget, "renderer", None)
#             if renderer is not None:
#                 renderer.RemoveActor2D(self._addb_drag_actor)
#             if vtk_widget is not None:
#                 vtk_widget.GetRenderWindow().Render()
#         except Exception:
#             pass
#         self._addb_drag_actor = None

#     def _addb_update_drag_overlay(self, p0_vtk, p1_vtk):
#         """Live rubberband rectangle while dragging — orange, distinct from fence cyan."""
#         try:
#             import vtk
#             vtk_widget = getattr(self.app, "vtk_widget", None)
#             renderer = getattr(vtk_widget, "renderer", None)
#             if renderer is None:
#                 return
#             if self._addb_drag_actor is not None:
#                 try:
#                     renderer.RemoveActor2D(self._addb_drag_actor)
#                 except Exception:
#                     pass
#                 self._addb_drag_actor = None

#             x0, y0 = p0_vtk
#             x1, y1 = p1_vtk
#             pts_screen = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
#             pts = vtk.vtkPoints()
#             for sx, sy in pts_screen:
#                 pts.InsertNextPoint(float(sx), float(sy), 0.0)
#             poly_line = vtk.vtkPolyLine()
#             poly_line.GetPointIds().SetNumberOfIds(len(pts_screen))
#             for i in range(len(pts_screen)):
#                 poly_line.GetPointIds().SetId(i, i)
#             cells = vtk.vtkCellArray()
#             cells.InsertNextCell(poly_line)
#             pd = vtk.vtkPolyData()
#             pd.SetPoints(pts)
#             pd.SetLines(cells)
#             coord = vtk.vtkCoordinate()
#             coord.SetCoordinateSystemToDisplay()
#             mapper = vtk.vtkPolyDataMapper2D()
#             mapper.SetInputData(pd)
#             mapper.SetTransformCoordinate(coord)
#             actor = vtk.vtkActor2D()
#             actor.SetMapper(mapper)
#             actor.GetProperty().SetColor(1.0, 0.6, 0.0)   # orange — distinct from fence cyan
#             actor.GetProperty().SetLineWidth(2.0)
#             renderer.AddActor2D(actor)
#             self._addb_drag_actor = actor
#             vtk_widget.GetRenderWindow().Render()
#         except Exception:
#             pass

#     def _addb_draw_polygon_outline(self, pts_2d, renderer):
#         """Draw a closed orange polygon outline in the VTK renderer and return the actor.

#         Returns None (and adds nothing to the renderer) if pts_2d is empty.
#         """
#         if not pts_2d:
#             return None
#         import vtk
#         ring = list(pts_2d)
#         if ring[0] != ring[-1]:
#             ring.append(ring[0])
#         vpts = vtk.vtkPoints()
#         for x, y in ring:
#             vpts.InsertNextPoint(float(x), float(y), 1.0)
#         n = vpts.GetNumberOfPoints()
#         poly_line = vtk.vtkPolyLine()
#         poly_line.GetPointIds().SetNumberOfIds(n)
#         for i in range(n):
#             poly_line.GetPointIds().SetId(i, i)
#         cells = vtk.vtkCellArray()
#         cells.InsertNextCell(poly_line)
#         pd = vtk.vtkPolyData()
#         pd.SetPoints(vpts)
#         pd.SetLines(cells)
#         mapper = vtk.vtkPolyDataMapper()
#         mapper.SetInputData(pd)
#         actor = vtk.vtkActor()
#         actor.SetMapper(mapper)
#         actor.GetProperty().SetColor(1.0, 0.6, 0.0)
#         actor.GetProperty().SetLineWidth(3.0)
#         actor.GetProperty().SetOpacity(1.0)
#         try:
#             actor.PickableOff()
#         except Exception:
#             pass
#         renderer.AddActor(actor)
#         return actor

#     def _addb_commit_rect(self, p0_vtk, p1_vtk):
#         """Convert the just-drawn screen rectangle to world coords.

#         If SNT block polygons are loaded, detects every SNT block whose interior
#         falls within the rectangle and creates one pending entry per SNT block
#         (with its exact polygon shape).  If no SNT data is present, stores the
#         rectangle itself as one pending boundary (original behaviour).
#         """
#         w0 = self._fence_screen_to_world_xy(*p0_vtk)
#         w1 = self._fence_screen_to_world_xy(*p1_vtk)
#         if w0 is None or w1 is None:
#             return
#         wmin_x, wmax_x = min(w0[0], w1[0]), max(w0[0], w1[0])
#         wmin_y, wmax_y = min(w0[1], w1[1]), max(w0[1], w1[1])
#         if (wmax_x - wmin_x) < 1e-6 or (wmax_y - wmin_y) < 1e-6:
#             return  # degenerate (click without drag) — ignore

#         snt_polygons = self._addb_get_visible_snt_polygons()

#         vtk_widget = getattr(self.app, "vtk_widget", None)
#         renderer = getattr(vtk_widget, "renderer", None)

#         if snt_polygons:
#             try:
#                 from gui.snt_attachment import _snt_polygon_interior_point
#             except Exception:
#                 _snt_polygon_interior_point = None

#             matched = []
#             for blk in snt_polygons:
#                 pts_2d = blk.get("points_2d") or []
#                 if not pts_2d:
#                     continue
#                 try:
#                     if _snt_polygon_interior_point is not None:
#                         ix, iy = _snt_polygon_interior_point(pts_2d)
#                     else:
#                         ix = sum(p[0] for p in pts_2d) / len(pts_2d)
#                         iy = sum(p[1] for p in pts_2d) / len(pts_2d)
#                 except Exception:
#                     continue
#                 if wmin_x <= ix <= wmax_x and wmin_y <= iy <= wmax_y:
#                     matched.append((blk.get("grid_name", ""), pts_2d))

#             if matched:
#                 # Avoid duplicates with already-pending SNT blocks
#                 pending_keys = {(g, tuple(tuple(p) for p in pts))
#                                 for g, pts in self._addb_pending_snt}
#                 added = 0
#                 for grid_name, pts_2d in matched:
#                     key = (grid_name, tuple(tuple(p) for p in pts_2d))
#                     if key in pending_keys:
#                         continue
#                     pending_keys.add(key)
#                     self._addb_pending_snt.append((grid_name, pts_2d))
#                     added += 1
#                     if renderer is not None:
#                         try:
#                             actor = self._addb_draw_polygon_outline(pts_2d, renderer)
#                             if actor is not None:
#                                 self._addb_pending_actors.append(actor)
#                         except Exception as e:
#                             print(f"⚠️ Add-by-Boundaries SNT outline failed: {e}")

#                 if renderer is not None and vtk_widget is not None:
#                     try:
#                         vtk_widget.GetRenderWindow().Render()
#                     except Exception:
#                         pass

#                 total = len(self._addb_pending_rects) + len(self._addb_pending_snt)
#                 if hasattr(self.app, "statusBar"):
#                     self.app.statusBar().showMessage(
#                         f"Add by Boundaries: {total} SNT block(s) detected "
#                         f"({added} new in this fence) — draw more or Esc/right-click to finish",
#                         0,
#                     )
#                 return   # SNT path handled — do not also store plain rectangle

#         # No SNT data (or no matches) — fall back to original rectangle behaviour
#         self._addb_pending_rects.append((wmin_x, wmin_y, wmax_x, wmax_y))

#         try:
#             if renderer is not None:
#                 ring = [
#                     (wmin_x, wmin_y), (wmax_x, wmin_y), (wmax_x, wmax_y),
#                     (wmin_x, wmax_y), (wmin_x, wmin_y),
#                 ]
#                 actor = self._addb_draw_polygon_outline(ring, renderer)
#                 if actor is not None:
#                     self._addb_pending_actors.append(actor)
#                 if vtk_widget is not None:
#                     vtk_widget.GetRenderWindow().Render()
#         except Exception as e:
#             print(f"⚠️ Add-by-Boundaries pending outline failed: {e}")

#         if hasattr(self.app, "statusBar"):
#             n = len(self._addb_pending_rects)
#             self.app.statusBar().showMessage(
#                 f"Add by Boundaries: {n} boundary{'ies' if n != 1 else 'y'} drawn — "
#                 "drag more, or Esc/right-click to finish",
#                 0,
#             )

#     def _addb_clear_pending(self):
#         """Remove all persistent pending-boundary outlines and clear the lists."""
#         self._addb_pending_rects = []
#         self._addb_pending_snt = []
#         if not self._addb_pending_actors:
#             return
#         try:
#             vtk_widget = getattr(self.app, "vtk_widget", None)
#             renderer = getattr(vtk_widget, "renderer", None)
#             for actor in self._addb_pending_actors:
#                 try:
#                     if renderer is not None:
#                         renderer.RemoveActor(actor)
#                 except Exception:
#                     pass
#             self._addb_pending_actors = []
#             if vtk_widget is not None:
#                 vtk_widget.GetRenderWindow().Render()
#         except Exception:
#             self._addb_pending_actors = []

#     def _detect_prefix_and_next_number(self):
#         """Inspect existing PRJ block labels to suggest a prefix + next number,
#         matching MicroStation's auto-suggest in 'Add by boundaries' — e.g. if
#         'drjh RADAR +000009/10/11' already exist, suggests prefix
#         'drjh RADAR +' and first number 12.

#         Returns (prefix: str, next_number: int).  Falls back to ("", 1) when
#         no existing label ends in a trailing number.
#         """
#         import re
#         pattern = re.compile(r"^(.*?)(\d+)$")
#         prefix_counts: dict = {}
#         prefix_max_num: dict = {}
#         for block in self.prj_data:
#             label = block.get("label", "").strip()
#             m = pattern.match(label)
#             if not m:
#                 continue
#             prefix, num_str = m.group(1), m.group(2)
#             try:
#                 num = int(num_str)
#             except ValueError:
#                 continue
#             prefix_counts[prefix] = prefix_counts.get(prefix, 0) + 1
#             prefix_max_num[prefix] = max(prefix_max_num.get(prefix, 0), num)

#         if not prefix_counts:
#             return "", 1

#         best_prefix = max(prefix_counts, key=lambda p: prefix_counts[p])
#         return best_prefix, prefix_max_num[best_prefix] + 1

#     def _addb_finish_drawing(self):
#         """Esc / right-click / un-toggling the button.

#         If at least one boundary was drawn, opens the naming dialog and
#         creates the new block(s) on accept.  Always stops drawing mode.
#         """
#         pending_rects = list(self._addb_pending_rects)
#         pending_snt = list(self._addb_pending_snt)
#         self._addb_deactivate(cancel=False)   # stop listening, keep pending outlines for now

#         total_pending = len(pending_rects) + len(pending_snt)
#         if not total_pending:
#             self._addb_clear_pending()
#             return

#         if not self.current_prj_path:
#             QMessageBox.warning(
#                 self, "No PRJ Loaded",
#                 "Please load a PRJ file before adding blocks by boundaries."
#             )
#             self._addb_clear_pending()
#             return
#         if not os.path.isfile(self.current_prj_path):
#             QMessageBox.critical(
#                 self, "PRJ File Not Found",
#                 f"The PRJ file no longer exists on disk:\n\n{self.current_prj_path}"
#             )
#             self._addb_clear_pending()
#             return

#         suggested_prefix, suggested_first_number = self._detect_prefix_and_next_number()

#         dlg = AddBlocksByBoundariesDialog(
#             total_pending, default_prefix=suggested_prefix,
#             default_first_number=suggested_first_number, parent=self
#         )
#         self._addb_dialog_active = True
#         try:
#             if dlg.exec() != QDialog.Accepted:
#                 self._addb_clear_pending()
#                 return
#         finally:
#             self._addb_dialog_active = False

#         prefix, first_number = dlg.get_values()
#         if not prefix:
#             QMessageBox.warning(self, "Missing Prefix", "Please enter a file prefix.")
#             self._addb_clear_pending()
#             return

#         self._addb_commit_new_blocks(
#             pending_rects, prefix, first_number, snt_blocks=pending_snt
#         )
#         self._addb_clear_pending()

#     def _addb_commit_new_blocks(self, rects, prefix, first_number, snt_blocks=None):
#         """Write one new Block section per drawn rectangle / SNT polygon to the .prj
#         file in a single backup + atomic write (not one write per block).

#         snt_blocks — list of (grid_name, pts_2d) from SNT polygon detection.
#         """
#         existing_upper = {e.get("label", "").strip().upper() for e in self.prj_data}
#         new_blocks = []   # (label, filename, polygon)
#         num = first_number

#         # Rectangular boundaries (no SNT data involved)
#         for rect in rects:
#             minx, miny, maxx, maxy = rect
#             if (maxx - minx) < 1e-6 or (maxy - miny) < 1e-6:
#                 continue
#             while True:
#                 label = f"{prefix}{num:06d}"
#                 if label.strip().upper() not in existing_upper:
#                     break
#                 num += 1
#             existing_upper.add(label.strip().upper())
#             filename = f"{label}.laz"
#             polygon = [
#                 (minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny)
#             ]
#             new_blocks.append((label, filename, polygon))
#             num += 1

#         # SNT polygon blocks — use grid_name as label when available
#         for grid_name, pts_2d in (snt_blocks or []):
#             if not pts_2d:
#                 continue
#             snt_label = grid_name.strip() if grid_name and grid_name.strip() else ""
#             if snt_label and snt_label.upper() not in existing_upper:
#                 label = snt_label
#             else:
#                 # SNT name conflicts or empty — generate a prefixed name
#                 while True:
#                     label = f"{prefix}{num:06d}"
#                     if label.strip().upper() not in existing_upper:
#                         break
#                     num += 1
#                 num += 1
#             existing_upper.add(label.upper())
#             filename = f"{label}.laz"
#             # Close the polygon ring if needed
#             polygon = list(pts_2d)
#             if polygon and polygon[0] != polygon[-1]:
#                 polygon.append(polygon[0])
#             new_blocks.append((label, filename, polygon))

#         if not new_blocks:
#             QMessageBox.warning(
#                 self, "Nothing to Add",
#                 "No valid (non-degenerate) boundaries were drawn."
#             )
#             return

#         try:
#             with open(self.current_prj_path, "r", encoding="utf-8") as fh:
#                 prj_content = fh.read()
#         except Exception as exc:
#             QMessageBox.critical(
#                 self, "Read Error", f"Could not read the PRJ file:\n\nError: {exc}"
#             )
#             return

#         group_first = self._get_next_group_first(prj_content)
#         if prj_content and not prj_content.endswith("\n"):
#             prj_content += "\n"

#         new_sections = [
#             self._build_block_section_text(filename, polygon, group_first + i * 1_000_000)
#             for i, (_, filename, polygon) in enumerate(new_blocks)
#         ]
#         new_content = prj_content + "".join(new_sections)

#         # ── Write to backend working copy (real .prj untouched until Save) ────
#         try:
#             self._write_prj_to_working(new_content)
#             print(f"  ✅ {len(new_blocks)} block(s) appended to backend working copy via Add by Boundaries")
#         except Exception as exc:
#             QMessageBox.critical(
#                 self, "Write Error",
#                 f"Failed to buffer the PRJ changes:\n\nError: {exc}\n\n"
#                 "The original file has NOT been modified."
#             )
#             return

#         try:
#             for label, filename, polygon in new_blocks:
#                 minx = min(p[0] for p in polygon)
#                 maxx = max(p[0] for p in polygon)
#                 miny = min(p[1] for p in polygon)
#                 maxy = max(p[1] for p in polygon)
#                 avg_x = (minx + maxx) / 2.0
#                 avg_y = (miny + maxy) / 2.0
#                 area_m2 = (maxx - minx) * (maxy - miny)
#                 coord_count = len(polygon) - 1 if (polygon and polygon[0] == polygon[-1]) else len(polygon)
#                 new_entry = {
#                     "label": label,
#                     "easting": f"{avg_x:.2f}",
#                     "northing": f"{avg_y:.2f}",
#                     "description": f"Boundary: {coord_count} points",
#                     "boundary_coords": [(x, y) for x, y in polygon[:-1]],
#                 }
#                 new_prj_idx = len(self.prj_data)
#                 self.prj_data.append(new_entry)
#                 self._all_row_data.append(
#                     (label, "No file in path", f"{area_m2:.2f}", None, None, new_prj_idx)
#                 )
#             self._sort_header._sort_states = {}
#             self._sort_header.viewport().update()
#             self._repopulate_table(self._all_row_data)
#             # Clear search box so newly added blocks are visible (search text may not
#             # match the new generated names, which would hide them from the table)
#             self.search_box.setText("")
#             self.table.scrollToBottom()
#             print(f"  ✅ In-memory state updated: {len(self.prj_data)} blocks")
#         except Exception as exc:
#             print(f"  ⚠️ Failed to refresh table after add-by-boundaries: {exc}")
#             QMessageBox.warning(
#                 self, "Display Refresh Issue",
#                 "Blocks were added to the PRJ file successfully, but the table\n"
#                 "could not refresh automatically.\n\n"
#                 "Please close and reopen the PRJ Block Identifier to see the change."
#             )
#             return

#         if hasattr(self.app, "statusBar"):
#             self.app.statusBar().showMessage(
#                 f"Add by Boundaries: {len(new_blocks)} block(s) added to PRJ", 5000
#             )

#     # ── Edit Definition ──────────────────────────────────────────────────────

#     def _toggle_editdef_mode(self, checked: bool):
#         if checked:
#             self._editdef_activate()
#         else:
#             self._editdef_deactivate(cancel=True)

#     def _editdef_activate(self):
#         selected_rows = self.table.selectionModel().selectedRows()
#         if len(selected_rows) != 1:
#             QMessageBox.warning(
#                 self, "Select One Block",
#                 "Please select exactly one block in the table before editing its definition."
#             )
#             self._editdef_action.blockSignals(True)
#             self._editdef_action.setChecked(False)
#             self._editdef_action.blockSignals(False)
#             return

#         item = self.table.item(selected_rows[0].row(), 0)
#         prj_idx = item.data(Qt.UserRole) if item is not None else None
#         if prj_idx is None or prj_idx < 0 or prj_idx >= len(self.prj_data):
#             self._editdef_action.blockSignals(True)
#             self._editdef_action.setChecked(False)
#             self._editdef_action.blockSignals(False)
#             return

#         if self.prj_data[prj_idx].get("locked", False):
#             label = self.prj_data[prj_idx].get("label", "?")
#             QMessageBox.warning(
#                 self, "Block Locked",
#                 f"'{label}' is locked.\n\nUse Block → Release lock before editing its definition."
#             )
#             self._editdef_action.blockSignals(True)
#             self._editdef_action.setChecked(False)
#             self._editdef_action.blockSignals(False)
#             return

#         if self._fence_active:
#             self._fence_deactivate(cancel=True)
#         if self._addb_active:
#             self._addb_deactivate(cancel=True)

#         vtk_widget = getattr(self.app, "vtk_widget", None)
#         if vtk_widget is None:
#             self._editdef_action.blockSignals(True)
#             self._editdef_action.setChecked(False)
#             self._editdef_action.blockSignals(False)
#             return

#         self._editdef_target_prj_idx = prj_idx
#         self._editdef_active = True
#         self._editdef_start_screen = None
#         self._editdef_drag_actor = None
#         vtk_widget.installEventFilter(self)
#         vtk_widget.setCursor(Qt.CrossCursor)

#         label = self.prj_data[prj_idx].get("label", "?")
#         if hasattr(self.app, "statusBar"):
#             self.app.statusBar().showMessage(
#                 f"Edit Definition: drag a new boundary for '{label}' — Esc/right-click to cancel",
#                 0,
#             )

#     def _editdef_deactivate(self, *, cancel: bool = False):
#         if not self._editdef_active:
#             self._editdef_action.blockSignals(True)
#             self._editdef_action.setChecked(False)
#             self._editdef_action.blockSignals(False)
#             return
#         self._editdef_active = False
#         self._editdef_start_screen = None
#         self._editdef_target_prj_idx = None
#         if self._editdef_mouse_grabbed:
#             try:
#                 vtk_widget = getattr(self.app, "vtk_widget", None)
#                 if vtk_widget is not None:
#                     vtk_widget.releaseMouse()
#             except Exception:
#                 pass
#             self._editdef_mouse_grabbed = False
#         self._editdef_remove_drag_overlay()
#         vtk_widget = getattr(self.app, "vtk_widget", None)
#         if vtk_widget is not None:
#             vtk_widget.removeEventFilter(self)
#             vtk_widget.setCursor(Qt.ArrowCursor)
#         if hasattr(self.app, "statusBar"):
#             if cancel:
#                 self.app.statusBar().showMessage("Edit Definition cancelled", 2000)
#             else:
#                 self.app.statusBar().clearMessage()
#         self._editdef_action.blockSignals(True)
#         self._editdef_action.setChecked(False)
#         self._editdef_action.blockSignals(False)

#     def _editdef_remove_drag_overlay(self):
#         if self._editdef_drag_actor is None:
#             return
#         try:
#             vtk_widget = getattr(self.app, "vtk_widget", None)
#             renderer = getattr(vtk_widget, "renderer", None)
#             if renderer is not None:
#                 renderer.RemoveActor2D(self._editdef_drag_actor)
#             if vtk_widget is not None:
#                 vtk_widget.GetRenderWindow().Render()
#         except Exception:
#             pass
#         self._editdef_drag_actor = None

#     def _editdef_update_drag_overlay(self, p0_vtk, p1_vtk):
#         """Live rubberband rectangle while dragging — magenta, distinct from
#         fence cyan and add-by-boundaries orange."""
#         try:
#             import vtk
#             vtk_widget = getattr(self.app, "vtk_widget", None)
#             renderer = getattr(vtk_widget, "renderer", None)
#             if renderer is None:
#                 return
#             if self._editdef_drag_actor is not None:
#                 try:
#                     renderer.RemoveActor2D(self._editdef_drag_actor)
#                 except Exception:
#                     pass
#                 self._editdef_drag_actor = None

#             x0, y0 = p0_vtk
#             x1, y1 = p1_vtk
#             pts_screen = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
#             pts = vtk.vtkPoints()
#             for sx, sy in pts_screen:
#                 pts.InsertNextPoint(float(sx), float(sy), 0.0)
#             poly_line = vtk.vtkPolyLine()
#             poly_line.GetPointIds().SetNumberOfIds(len(pts_screen))
#             for i in range(len(pts_screen)):
#                 poly_line.GetPointIds().SetId(i, i)
#             cells = vtk.vtkCellArray()
#             cells.InsertNextCell(poly_line)
#             pd = vtk.vtkPolyData()
#             pd.SetPoints(pts)
#             pd.SetLines(cells)
#             coord = vtk.vtkCoordinate()
#             coord.SetCoordinateSystemToDisplay()
#             mapper = vtk.vtkPolyDataMapper2D()
#             mapper.SetInputData(pd)
#             mapper.SetTransformCoordinate(coord)
#             actor = vtk.vtkActor2D()
#             actor.SetMapper(mapper)
#             actor.GetProperty().SetColor(1.0, 0.0, 1.0)   # magenta
#             actor.GetProperty().SetLineWidth(2.0)
#             renderer.AddActor2D(actor)
#             self._editdef_drag_actor = actor
#             vtk_widget.GetRenderWindow().Render()
#         except Exception:
#             pass

#     @staticmethod
#     def _shoelace_area(coords) -> float:
#         if len(coords) < 3:
#             return 0.0
#         n = len(coords)
#         s = 0.0
#         for i in range(n):
#             x1, y1 = coords[i]
#             x2, y2 = coords[(i + 1) % n]
#             s += x1 * y2 - x2 * y1
#         return abs(s) / 2.0

#     def _editdef_commit(self, p0_vtk, p1_vtk):
#         """Replace the target block's boundary with the just-drawn rectangle,
#         after explicit user confirmation."""
#         prj_idx = self._editdef_target_prj_idx
#         if prj_idx is None or prj_idx < 0 or prj_idx >= len(self.prj_data):
#             return

#         w0 = self._fence_screen_to_world_xy(*p0_vtk)
#         w1 = self._fence_screen_to_world_xy(*p1_vtk)
#         if w0 is None or w1 is None:
#             return
#         wmin_x, wmax_x = min(w0[0], w1[0]), max(w0[0], w1[0])
#         wmin_y, wmax_y = min(w0[1], w1[1]), max(w0[1], w1[1])
#         if (wmax_x - wmin_x) < 1e-6 or (wmax_y - wmin_y) < 1e-6:
#             return  # degenerate — ignore silently

#         block = self.prj_data[prj_idx]
#         label = block.get("label", "")
#         old_area = self._shoelace_area(block.get("boundary_coords", []))
#         new_area = (wmax_x - wmin_x) * (wmax_y - wmin_y)

#         reply = QMessageBox.question(
#             self, "Confirm Boundary Edit",
#             (
#                 f"Replace the boundary for block '{label}'?\n\n"
#                 f"  Old area: {old_area:.2f} m²\n"
#                 f"  New area: {new_area:.2f} m²\n\n"
#                 "This permanently rewrites the polygon in the PRJ file.\n"
#                 "Changes are buffered in a backend working copy and only written\n"
#                 "to the real PRJ when you press 'Save project'."
#             ),
#             QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
#         )
#         if reply != QMessageBox.Yes:
#             return

#         if not self.current_prj_path or not os.path.isfile(self.current_prj_path):
#             QMessageBox.critical(
#                 self, "PRJ File Not Found",
#                 "The PRJ file no longer exists on disk — cannot save the edit."
#             )
#             return

#         new_polygon = [
#             (wmin_x, wmin_y), (wmax_x, wmin_y), (wmax_x, wmax_y), (wmin_x, wmax_y)
#         ]

#         try:
#             with open(self.current_prj_path, "r", encoding="utf-8") as fh:
#                 prj_content = fh.read()
#         except Exception as exc:
#             QMessageBox.critical(self, "Read Error", f"Could not read the PRJ file:\n\nError: {exc}")
#             return

#         new_content = self._replace_block_boundary_in_prj_text(prj_content, label, new_polygon)

#         # ── Write to backend working copy (real .prj untouched until Save) ────
#         try:
#             self._write_prj_to_working(new_content)
#             print(f"  ✅ Boundary replaced for: {label} (backend working copy)")
#         except Exception as exc:
#             QMessageBox.critical(
#                 self, "Write Error",
#                 f"Failed to buffer the PRJ changes:\n\nError: {exc}\n\n"
#                 "The original file has NOT been modified."
#             )
#             return

#         try:
#             avg_x = (wmin_x + wmax_x) / 2.0
#             avg_y = (wmin_y + wmax_y) / 2.0
#             self.prj_data[prj_idx]["boundary_coords"] = new_polygon
#             self.prj_data[prj_idx]["easting"] = f"{avg_x:.2f}"
#             self.prj_data[prj_idx]["northing"] = f"{avg_y:.2f}"

#             for i, row in enumerate(self._all_row_data):
#                 if row[5] == prj_idx:
#                     self._all_row_data[i] = (row[0], row[1], f"{new_area:.2f}", row[3], row[4], prj_idx)
#                     break

#             self._sort_header._sort_states = {}
#             self._sort_header.viewport().update()
#             self._repopulate_table(self._all_row_data)
#             self._on_search_changed(self.search_box.text())
#             print(f"  ✅ In-memory boundary updated for: {label}")
#         except Exception as exc:
#             print(f"  ⚠️ Failed to refresh table after edit: {exc}")
#             QMessageBox.warning(
#                 self, "Display Refresh Issue",
#                 "The boundary was updated in the PRJ file successfully, but the table\n"
#                 "could not refresh automatically.\n\n"
#                 "Please close and reopen the PRJ Block Identifier to see the change."
#             )
#             return

#         if hasattr(self.app, "statusBar"):
#             self.app.statusBar().showMessage(
#                 f"Boundary updated for '{label}' — new area: {new_area:.2f} m²", 5000
#             )

#     # ── File menu: New / Save / Save As / Edit project information ─────────────

#     def _new_project(self):
#         """'New project...' — Project Information dialog first, then save location."""
#         # 1. Show Project Information dialog with all defaults — matches MicroStation
#         #    workflow where the form appears immediately on "New project".
#         info_dlg = ProjectInformationDialog(fields=None, parent=self)
#         info_dlg.setWindowTitle("Project Information — New Project")
#         if info_dlg.exec() != QDialog.Accepted:
#             return

#         fields = info_dlg.get_fields()

#         # 2. Now ask where to save the file
#         new_path, _ = QFileDialog.getSaveFileName(
#             self, "Save New PRJ File",
#             self.current_directory or "",
#             "PRJ Files (*.prj)"
#         )
#         if not new_path:
#             return
#         if not new_path.lower().endswith(".prj"):
#             new_path += ".prj"

#         header_text = ProjectInformationDialog.fields_to_header_text(fields)

#         # 3. Write file atomically
#         tmp_path = new_path + ".tmp"
#         try:
#             with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
#                 fh.write(header_text)
#             os.replace(tmp_path, new_path)
#         except Exception as exc:
#             try:
#                 if os.path.exists(tmp_path):
#                     os.remove(tmp_path)
#             except Exception:
#                 pass
#             QMessageBox.critical(
#                 self, "Create Failed",
#                 f"Could not create the new project file:\n\nError: {exc}"
#             )
#             return

#         self.parse_prj_file(new_path)

#     # ── Backend working-copy persistence ─────────────────────────────────────
#     # Edits (Add/Delete/Rename/Merge/Boundary/Info) are written to a backend
#     # side-file ("<name>.prj.working") instead of mutating the real .prj on
#     # disk. The original .prj is only ever replaced when the user explicitly
#     # presses "Save project" (which snapshots it to .prj.bak first). This
#     # prevents accidental edits from destroying the source project and lets
#     # the user discard pending changes by simply not saving.

#     @property
#     def _prj_working_path(self):
#         if not self.current_prj_path:
#             return None
#         return self.current_prj_path + ".working"

#     def _has_unsaved_working(self):
#         """True if there is a backend working copy with pending (unsaved) edits."""
#         wp = self._prj_working_path
#         return bool(wp and os.path.isfile(wp))

#     def _write_prj_to_working(self, new_content):
#         """Atomically write the new PRJ text to the backend working side-file.

#         Returns True on success. The real .prj is NEVER touched here.
#         """
#         wp = self._prj_working_path
#         if not wp:
#             return False
#         tmp_path = wp + ".tmp"
#         try:
#             with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
#                 fh.write(new_content)
#             os.replace(tmp_path, wp)
#             return True
#         except Exception as exc:
#             try:
#                 if os.path.exists(tmp_path):
#                     os.remove(tmp_path)
#             except Exception:
#                 pass
#             raise

#     def _commit_working_to_real(self):
#         """Replace the real .prj with the backend working copy.

#         The current real .prj is first snapshotted to .prj.bak (matching the
#         previous "Save" behaviour). Returns True on success. If there is no
#         working copy, simply refreshes the .bak snapshot so "Save" still works
#         as before for an already-clean project.
#         """
#         wp = self._prj_working_path
#         real = self.current_prj_path
#         if not real:
#             return False

#         # Snapshot the current real .prj to .prj.bak (keep existing behaviour)
#         bak_path = real + ".bak"
#         try:
#             import shutil as _shutil
#             _shutil.copy2(real, bak_path)
#             print(f"  ✅ PRJ backup written: {bak_path}")
#         except Exception as exc:
#             print(f"  ⚠️ PRJ backup failed (non-fatal): {exc}")

#         if self._has_unsaved_working():
#             try:
#                 os.replace(wp, real)
#                 print(f"  ✅ PRJ file updated: {real}")
#             except Exception as exc:
#                 QMessageBox.critical(
#                     self, "Write Error",
#                     f"Failed to save the project file:\n\n{real}\n\nError: {exc}",
#                 )
#                 return False
#         else:
#             print(f"  ✅ PRJ file unchanged (no pending edits): {real}")
#         return True

#     def _discard_working(self):
#         """Drop any pending backend working copy (call when reloading/closing)."""
#         wp = self._prj_working_path
#         if wp and os.path.isfile(wp):
#             try:
#                 os.remove(wp)
#             except Exception:
#                 pass

#     def _save_project(self):
#         """'Save project' — commit all pending backend edits to the real .prj.

#         Edits made via Add/Delete/Rename/Merge/Boundary/Info are buffered in a
#         backend working side-file (".prj.working") so the original .prj is not
#         modified until the user explicitly saves. This action snapshots the
#         current .prj to .prj.bak first, then replaces it with the working copy.
#         """
#         if not self.current_prj_path:
#             QMessageBox.warning(self, "No PRJ Loaded", "Please load a PRJ file first.")
#             return
#         if not os.path.isfile(self.current_prj_path):
#             QMessageBox.critical(
#                 self, "PRJ File Not Found",
#                 f"The PRJ file no longer exists on disk:\n\n{self.current_prj_path}"
#             )
#             return

#         committed = self._commit_working_to_real()
#         if not committed:
#             return

#         if hasattr(self.app, "statusBar"):
#             self.app.statusBar().showMessage("Project saved", 5000)

#     def _save_project_as(self):
#         """'Save project As...' — copy the current .prj file byte-for-byte
#         (preserving every field, including GroupFirst/GroupCount which are
#         not tracked in memory) to a new location, then switch the dialog
#         to point at the new copy.
#         """
#         if not self.current_prj_path or not os.path.isfile(self.current_prj_path):
#             QMessageBox.warning(self, "No PRJ Loaded", "Please load a PRJ file first.")
#             return

#         default_name = os.path.basename(self.current_prj_path)
#         new_path, _ = QFileDialog.getSaveFileName(
#             self, "Save Project As",
#             os.path.join(self.current_directory or "", default_name),
#             "PRJ Files (*.prj)"
#         )
#         if not new_path:
#             return
#         if not new_path.lower().endswith(".prj"):
#             new_path += ".prj"
#         if os.path.abspath(new_path) == os.path.abspath(self.current_prj_path):
#             QMessageBox.warning(self, "Same File", "Choose a different file name or location.")
#             return

#         try:
#             import shutil as _shutil
#             # Prefer the backend working copy (which holds any pending edits);
#             # fall back to the real .prj when there are no unsaved changes.
#             src = self._prj_working_path if self._has_unsaved_working() else self.current_prj_path
#             _shutil.copy2(src, new_path)
#         except Exception as exc:
#             QMessageBox.critical(self, "Save As Failed", f"Could not save project:\n\nError: {exc}")
#             return

#         if hasattr(self.app, "statusBar"):
#             self.app.statusBar().showMessage(f"Saved as: {os.path.basename(new_path)}", 5000)
#         self.parse_prj_file(new_path)

#     def _edit_project_information(self):
#         """'Edit project information...' — structured Project Information dialog.

#         Reads the existing header, pre-populates all fields, writes back on OK.
#         Block definitions are never touched.
#         """
#         if not self.current_prj_path or not os.path.isfile(self.current_prj_path):
#             QMessageBox.warning(self, "No PRJ Loaded", "Please load a PRJ file first.")
#             return

#         try:
#             with open(self.current_prj_path, "r", encoding="utf-8") as fh:
#                 content = fh.read()
#         except Exception as exc:
#             QMessageBox.critical(self, "Read Error", f"Could not read the PRJ file:\n\nError: {exc}")
#             return

#         header = self._extract_header_from_prj_text(content)
#         existing_fields = ProjectInformationDialog.parse_header_fields(header)

#         info_dlg = ProjectInformationDialog(fields=existing_fields, parent=self)
#         info_dlg.setWindowTitle(
#             f"Project Information — {os.path.basename(self.current_prj_path)}"
#         )
#         if info_dlg.exec() != QDialog.Accepted:
#             return

#         fields = info_dlg.get_fields()
#         new_header = ProjectInformationDialog.fields_to_header_text(fields)
#         if not new_header.endswith("\n"):
#             new_header += "\n"

#         # Re-read fresh to pick up any concurrent block changes
#         try:
#             with open(self.current_prj_path, "r", encoding="utf-8") as fh:
#                 fresh_content = fh.read()
#         except Exception as exc:
#             QMessageBox.critical(self, "Read Error", f"Could not re-read the PRJ file:\n\nError: {exc}")
#             return

#         lines = fresh_content.splitlines(keepends=True)
#         block_start_idx = None
#         for i, line in enumerate(lines):
#             if line.strip().startswith("Block "):
#                 block_start_idx = i
#                 break
#         blocks_text = "".join(lines[block_start_idx:]) if block_start_idx is not None else ""
#         new_content = new_header + blocks_text

#         # ── Write to backend working copy (real .prj untouched until Save) ────
#         try:
#             self._write_prj_to_working(new_content)
#             print("  ✅ Project information updated (backend working copy)")
#         except Exception as exc:
#             QMessageBox.critical(
#                 self, "Write Error",
#                 f"Failed to buffer project information:\n\nError: {exc}\n\n"
#                 "The original file has NOT been modified."
#             )
#             return

#         if hasattr(self.app, "statusBar"):
#             self.app.statusBar().showMessage("Project information updated (unsaved)", 4000)

#     def remove_prj_file(self):
#         """Remove currently loaded PRJ file from the dialog."""
#         if not self.current_prj_path:
#             QMessageBox.warning(
#                 self,
#                 "No File Loaded",
#                 "No PRJ file is currently loaded."
#             )
#             return
        
#         reply = QMessageBox.question(
#             self,
#             "Remove PRJ File",
#             f"Remove loaded file:\n{os.path.basename(self.current_prj_path)}\n\n"
#             "This will clear the block list but NOT delete the file from disk.",
#             QMessageBox.Yes | QMessageBox.No,
#             QMessageBox.No
#         )
        
#         if reply == QMessageBox.Yes:
#             # Clean up fence / add-by-boundaries / edit-definition state before clearing data
#             self._fence_deactivate(cancel=False)
#             self._fence_highlight_clear()
#             self._addb_deactivate(cancel=True)
#             self._editdef_deactivate(cancel=True)
#             # Clear the table
#             self.table.setRowCount(0)
#             self.prj_data.clear()
            
#             # Clear the stored path
#             filename = os.path.basename(self.current_prj_path)
#             self._discard_working()
#             self.current_prj_path = None
            
#             # Update status label
#             self.dxf_label.setText("📁  No DXF loaded")
#             # ✅ Clear all highlights
#             if hasattr(self, '_highlight_actors') and self._highlight_actors:
#                 if hasattr(self.app, 'vtk_widget') and self.app.vtk_widget:
#                     renderer = self.app.vtk_widget.renderer
#                     for highlight_actor in self._highlight_actors:
#                         try:
#                             renderer.RemoveActor(highlight_actor)
#                         except Exception:
#                             pass
#                     self.app.vtk_widget.GetRenderWindow().Render()
            
#             # Clear tracking data
#             self._highlight_actors = []
#             self._highlighted_labels = []
#             self._highlighted_blocks = {}

#             # Cancel pending auto-clear timer
#             self._highlight_identify_id += 1
            
#             # Update status label
#             self.dxf_label.setText("📁  No DXF loaded")

#             self._missing_blocks_hidden = False
#             if self._hide_missing_btn is not None:
#                 self._hide_missing_btn.setText("👁  Hide Missing Blocks")
#                 self._hide_missing_btn.setStyleSheet("")

#             self._restore_bl_polygon_actors()

#             print(f"✅ Removed PRJ file: {filename}")
#             if hasattr(self.app, "statusBar"):
#                 self.app.statusBar().showMessage(f"PRJ file '{filename}' removed", 4000)

#     def _toggle_missing_blocks_visibility(self):
#         """Hide/show SNT block polygons + labels for missing-LAZ blocks."""
#         missing_labels: Set[str] = {
#             row[0]
#             for row in self._all_row_data
#             if row[1] == "No file in path"
#         }

#         if not missing_labels:
#             QMessageBox.information(
#                 self, "No Missing Blocks",
#                 "All blocks in the table have associated LAZ files."
#             )
#             return

#         self._missing_blocks_hidden = not self._missing_blocks_hidden

#         if self._missing_blocks_hidden:
#             for lbl in list(missing_labels):
#                 self.clear_highlight_for_block(lbl)
#             self._last_hide_missing_labels = set(missing_labels)
#             present_labels = {
#                 row[0]
#                 for row in self._all_row_data
#                 if row[1] != "No file in path"
#             }
#             self._filter_bl_polygon_actors(missing_labels, present_labels)
#         else:
#             self._last_hide_missing_labels = set()
#             self._restore_bl_polygon_actors()

#         self._update_hide_btn_style()

#     def _restore_bl_polygon_actors(self):
#         """
#         Restore hidden original SNT block actors and remove PRJ-created
#         Hide Missing Blocks overlay actors.

#         Important:
#         Hide Missing Blocks creates separate overlay actors with:
#             filename = "_prj_boundary_overlay"
#             cache layer = "_PRJ_BOUNDARY"

#         Normal SNT remove will not catch those actors, so this function removes
#         both tracked and orphan overlay actors safely.
#         """
#         # 1) Restore original actors that were hidden by Hide Missing Blocks
#         for _aid, saved in list(getattr(self, "_snt_filter_saved_visibility", {}).items()):
#             try:
#                 actor, vis = saved
#                 if actor is not None:
#                     actor.SetVisibility(vis)
#             except Exception:
#                 pass

#         self._snt_filter_saved_visibility.clear()

#         # 2) Get renderer safely
#         renderer = None
#         try:
#             if getattr(self.app, "vtk_widget", None) is not None:
#                 renderer = self.app.vtk_widget.renderer
#         except Exception:
#             renderer = None

#         # 3) Collect tracked overlay actors
#         overlay_actors = []
#         overlay_ids = set()

#         for actor in list(getattr(self, "_snt_filter_overlay_actors", []) or []):
#             if actor is None:
#                 continue
#             overlay_actors.append(actor)
#             overlay_ids.add(id(actor))

#         # 4) Also collect orphan overlay actors from app stores
#         #    This covers cases where the tracking list is stale/empty.
#         for store_name in ("snt_actors", "dxf_actors"):
#             for entry in list(getattr(self.app, store_name, []) or []):
#                 try:
#                     filename = str(entry.get("filename", "") or "")
#                     actors = entry.get("actors", []) or []

#                     if filename == "_prj_boundary_overlay":
#                         for actor in actors:
#                             if actor is not None:
#                                 overlay_actors.append(actor)
#                                 overlay_ids.add(id(actor))
#                         continue

#                     for actor in actors:
#                         if actor is not None and getattr(actor, "_is_filter_overlay", False):
#                             overlay_actors.append(actor)
#                             overlay_ids.add(id(actor))
#                 except Exception:
#                     pass

#         # 5) Remove overlay actors from renderer
#         if renderer is not None:
#             seen_actor_ids = set()
#             for actor in overlay_actors:
#                 if actor is None:
#                     continue
#                 aid = id(actor)
#                 if aid in seen_actor_ids:
#                     continue
#                 seen_actor_ids.add(aid)

#                 try:
#                     renderer.RemoveActor(actor)
#                 except Exception:
#                     pass

#         # 6) Remove overlay entries from app.snt_actors and app.dxf_actors
#         for store_name in ("snt_actors", "dxf_actors"):
#             store = getattr(self.app, store_name, None)
#             if store is None:
#                 continue

#             cleaned_store = []
#             for entry in list(store):
#                 try:
#                     filename = str(entry.get("filename", "") or "")
#                     actors = entry.get("actors", []) or []

#                     # Drop the dedicated PRJ overlay entry completely
#                     if filename == "_prj_boundary_overlay":
#                         continue

#                     # Remove only overlay actors if they somehow got mixed into an entry
#                     filtered_actors = [
#                         actor for actor in actors
#                         if actor is not None
#                         and id(actor) not in overlay_ids
#                         and not getattr(actor, "_is_filter_overlay", False)
#                     ]

#                     if len(filtered_actors) != len(actors):
#                         entry = dict(entry)
#                         entry["actors"] = filtered_actors

#                     cleaned_store.append(entry)
#                 except Exception:
#                     cleaned_store.append(entry)

#             try:
#                 setattr(self.app, store_name, cleaned_store)
#             except Exception:
#                 pass

#         # 7) Remove overlay cache layer from SNT item actor_cache
#         _PRJ_CACHE_LAYER = "_PRJ_BOUNDARY"
#         snt_dlg = getattr(self.app, "snt_dialog", None)
#         if snt_dlg is not None:
#             for si in getattr(snt_dlg, "snt_items", []):
#                 try:
#                     actor_cache = getattr(si, "actor_cache", None)
#                     if actor_cache and _PRJ_CACHE_LAYER in actor_cache:
#                         del actor_cache[_PRJ_CACHE_LAYER]
#                 except Exception:
#                     pass

#         # 8) Clear PRJ overlay tracking list
#         self._snt_filter_overlay_actors.clear()

#         # 9) Render
#         self._safe_render_main_view()

#     def clear_snt_filter_overlays_on_snt_removed(self):
#         """
#         Called from SNT remove / Clear All.

#         Clears only PRJ Hide Missing Blocks overlay lines.
#         Does NOT remove PRJ file.
#         Does NOT clear PRJ table.
#         """
#         try:
#             self._restore_bl_polygon_actors()

#             self._missing_blocks_hidden = False
#             self._last_hide_missing_labels = set()
#             self._snt_filter_saved_visibility.clear()
#             self._snt_filter_overlay_actors.clear()

#             self._update_hide_btn_style()
#             self._safe_render_main_view()

#             print("✅ PRJ hide-missing overlays cleared after SNT remove")

#         except Exception as exc:
#             print(f"⚠️ Failed to clear PRJ hide-missing overlays after SNT remove: {exc}")

#     def _update_hide_btn_style(self):
#         """Sync button label + colour to current hide state. Safe to call after theme reload."""
#         if self._hide_missing_btn is None:
#             return
#         if self._missing_blocks_hidden:
#             self._hide_missing_btn.setText("👁  Show Missing Blocks")
#             # Use object-name-specific rule so it wins over app stylesheet
#             warning = ThemeColors.get("warning")
#             self._hide_missing_btn.setStyleSheet(
#                 f"QPushButton#secondaryBtn {{ color: {warning}; border: 1px solid {warning}; }}"
#             )
#         else:
#             self._hide_missing_btn.setText("👁  Hide Missing Blocks")
#             self._hide_missing_btn.setStyleSheet("")

#     def reapply_hide_state(self):
#         """
#         Call this after any grid / LAZ load completes.
#         The load restores all DXF actors to visible=1, so we re-hide
#         the missing-block actors if the button was active.
#         """
#         if not self._missing_blocks_hidden or not self._last_hide_missing_labels:
#             return
#         self._snt_filter_saved_visibility.clear()
#         self._restore_bl_polygon_actors()
#         present_labels = {
#             row[0]
#             for row in self._all_row_data
#             if row[1] != "No file in path"
#         }
#         self._filter_bl_polygon_actors(self._last_hide_missing_labels, present_labels)
#         self._update_hide_btn_style()
#         print("  🔵 PRJ hide-state reapplied after grid load")

#     def should_keep_actor_hidden(self, actor) -> bool:
#         """
#         Return True when PRJ Hide Missing Blocks currently owns this actor's
#         visibility and it must not be revived by generic SNT/DXF restore code.
#         """
#         if actor is None or not self._missing_blocks_hidden:
#             return False
#         try:
#             return id(actor) in getattr(self, "_snt_filter_saved_visibility", {})
#         except Exception:
#             return False

#     def _filter_bl_polygon_actors(self, missing_labels, present_labels: set = None):
#         """
#         Hide missing-block labels and boundary rectangles.
#         """
#         from gui.snt_attachment import _is_block_label_layer, _snt_point_in_polygon_2d

#         if not getattr(self.app, 'vtk_widget', None):
#             return

#         renderer = self.app.vtk_widget.renderer
#         seen_bl: set = set()
#         hidden_boundary_labels: set = set()
#         snt_dlg = getattr(self.app, 'snt_dialog', None)

#         if present_labels is None:
#             present_labels = {
#                 row[0]
#                 for row in self._all_row_data
#                 if row[1] != "No file in path"
#             }

#         def _normalise_label(name: str) -> str:
#             return str(name or "").replace(".laz", "").replace(".las", "").strip().upper()

#         def _match_missing_label(name: str, label_pool=None):
#             gn = _normalise_label(name)
#             if not gn:
#                 return None
#             pool = label_pool if label_pool is not None else missing_labels
#             for lbl in pool:
#                 lb = _normalise_label(lbl)
#                 if gn == lb or lb in gn or gn in lb:
#                     return lbl
#             return None

#         missing_poly_by_label: dict = {}
#         for bd in self.prj_data or []:
#             lbl = bd.get("label", "")
#             coords = bd.get("boundary_coords", [])
#             if lbl in missing_labels and len(coords) >= 3:
#                 missing_poly_by_label[lbl] = coords

#         missing_poly_bounds: dict = {}
#         for lbl, pts in missing_poly_by_label.items():
#             try:
#                 xs = [float(p[0]) for p in pts]
#                 ys = [float(p[1]) for p in pts]
#                 missing_poly_bounds[lbl] = (
#                     min(xs), max(xs), min(ys), max(ys)
#                 )
#             except Exception:
#                 continue

#         def _find_missing_label_for_point(cx, cy, label_pool=None):
#             pool = label_pool if label_pool is not None else missing_poly_by_label.keys()
#             for lbl in pool:
#                 pts = missing_poly_by_label.get(lbl)
#                 if not pts:
#                     continue
#                 if _snt_point_in_polygon_2d(cx, cy, pts):
#                     return lbl
#             return None

#         def _point_on_segment_2d(px, py, x1, y1, x2, y2, eps=0.10):
#             dx = x2 - x1
#             dy = y2 - y1
#             seg_len2 = dx * dx + dy * dy
#             if seg_len2 <= 1e-12:
#                 qx = px - x1
#                 qy = py - y1
#                 return (qx * qx + qy * qy) <= (eps * eps)

#             t = ((px - x1) * dx + (py - y1) * dy) / seg_len2
#             if t < 0.0:
#                 cx, cy = x1, y1
#             elif t > 1.0:
#                 cx, cy = x2, y2
#             else:
#                 cx = x1 + t * dx
#                 cy = y1 + t * dy

#             ex = px - cx
#             ey = py - cy
#             return (ex * ex + ey * ey) <= (eps * eps)

#         def _point_in_or_on_polygon(px, py, poly):
#             if _snt_point_in_polygon_2d(px, py, poly):
#                 return True
#             if not poly:
#                 return False
#             try:
#                 xs = [p[0] for p in poly]
#                 ys = [p[1] for p in poly]
#                 diag = ((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2) ** 0.5
#                 edge_eps = max(0.10, diag * 1e-5)
#             except Exception:
#                 edge_eps = 0.10

#             n = len(poly)
#             for i in range(n):
#                 x1, y1 = poly[i][0], poly[i][1]
#                 x2, y2 = poly[(i + 1) % n][0], poly[(i + 1) % n][1]
#                 if _point_on_segment_2d(px, py, x1, y1, x2, y2, edge_eps):
#                     return True
#             return False

#         def _actor_centroid(a):
#             try:
#                 b = a.GetBounds()
#                 return (b[0] + b[1]) / 2.0, (b[2] + b[3]) / 2.0
#             except Exception:
#                 return None, None

#         def _actor_bounds(a):
#             try:
#                 b = a.GetBounds()
#                 return float(b[0]), float(b[1]), float(b[2]), float(b[3])
#             except Exception:
#                 return None

#         def _bbox_overlap_ratio(bounds_a, bounds_b):
#             if not bounds_a or not bounds_b:
#                 return 0.0
#             ax0, ax1, ay0, ay1 = bounds_a
#             bx0, bx1, by0, by1 = bounds_b
#             ix0 = max(ax0, bx0)
#             ix1 = min(ax1, bx1)
#             iy0 = max(ay0, by0)
#             iy1 = min(ay1, by1)
#             if ix1 <= ix0 or iy1 <= iy0:
#                 return 0.0
#             inter = (ix1 - ix0) * (iy1 - iy0)
#             aa = max((ax1 - ax0) * (ay1 - ay0), 1e-12)
#             ab = max((bx1 - bx0) * (by1 - by0), 1e-12)
#             return inter / min(aa, ab)

#         def _extract_actor_points_2d(a, sample_limit=96):
#             try:
#                 mapper = a.GetMapper()
#                 if mapper is None:
#                     return []
#                 pd = mapper.GetInput()
#                 if pd is None:
#                     return []
#                 pts = pd.GetPoints()
#                 if pts is None:
#                     return []

#                 npts = int(pts.GetNumberOfPoints())
#                 if npts <= 0:
#                     return []

#                 step = max(1, npts // max(1, int(sample_limit)))
#                 out = []
#                 idx = 0
#                 while idx < npts:
#                     p = pts.GetPoint(idx)
#                     out.append((float(p[0]), float(p[1])))
#                     idx += step
#                 if out and (npts - 1) % step != 0:
#                     p = pts.GetPoint(npts - 1)
#                     out.append((float(p[0]), float(p[1])))
#                 return out
#             except Exception:
#                 return []

#         def _find_missing_label_for_actor(a, label_pool=None):
#             pool = list(label_pool if label_pool is not None else missing_poly_by_label.keys())
#             if not pool:
#                 return None

#             # Fast path: centroid test.
#             cx, cy = _actor_centroid(a)
#             if cx is not None:
#                 matched = _find_missing_label_for_point(cx, cy, pool)
#                 if matched:
#                     return matched

#             bounds = _actor_bounds(a)
#             sample_pts = _extract_actor_points_2d(a)

#             for lbl in pool:
#                 poly = missing_poly_by_label.get(lbl)
#                 if not poly:
#                     continue

#                 # Strong signal: actor bounds mostly overlap this PRJ polygon bounds.
#                 if _bbox_overlap_ratio(bounds, missing_poly_bounds.get(lbl)) >= 0.80:
#                     return lbl

#                 # Robust signal for concave/long boundaries: actor poly points are
#                 # on/inside the missing PRJ boundary.
#                 if sample_pts:
#                     hits = 0
#                     for sx, sy in sample_pts:
#                         if _point_in_or_on_polygon(sx, sy, poly):
#                             hits += 1
#                             if hits >= 2:
#                                 return lbl
#             return None

#         def _hide_actor(actor, matched_label=None):
#             aid = id(actor)
#             if aid in self._snt_filter_saved_visibility:
#                 return
#             self._snt_filter_saved_visibility[aid] = (actor, int(actor.GetVisibility()))
#             actor.SetVisibility(0)
#             seen_bl.add(aid)
#             if matched_label and not getattr(actor, "is_grid_label", False):
#                 hidden_boundary_labels.add(matched_label)

#         # ── Phase A: direct grid_name match ─────────────────────────────────
#         def _process_direct(a):
#             aid = id(a)
#             if aid in seen_bl or aid in self._snt_filter_saved_visibility:
#                 return
#             gn = getattr(a, 'grid_name', None)
#             matched_label = _match_missing_label(gn)
#             if matched_label:
#                 _hide_actor(a, matched_label)

#         for store in ('snt_actors', 'dxf_actors'):
#             for sd in getattr(self.app, store, []):
#                 for a in sd.get('actors', []):
#                     _process_direct(a)

#         if snt_dlg:
#             for si in getattr(snt_dlg, 'snt_items', []):
#                 for ln, actors in getattr(si, 'actor_cache', {}).items():
#                     for a in actors:
#                         _process_direct(a)

#         print(
#             f"  🔵 Phase A (grid_name direct): {len(seen_bl)} actors hidden "
#             f"across {len(hidden_boundary_labels)} boundary label(s)"
#         )

#         # ── Phase A.5: block-label layer spatial check ───────────────────────
#         if missing_poly_by_label:
#             def _process_blk_layer(a, layer_name=""):
#                 aid = id(a)
#                 if aid in seen_bl or aid in self._snt_filter_saved_visibility:
#                     return
#                 if getattr(a, 'is_grid_label', False):
#                     return
#                 lyr = layer_name or getattr(a, '_naksha_snt_layer', '')
#                 if not _is_block_label_layer(lyr):
#                     return
#                 pending_labels = {lbl for lbl in missing_poly_by_label if lbl not in hidden_boundary_labels}
#                 if not pending_labels:
#                     return
#                 matched_label = _find_missing_label_for_actor(a, pending_labels)
#                 if matched_label:
#                     _hide_actor(a, matched_label)

#             for store in ('snt_actors', 'dxf_actors'):
#                 for sd in getattr(self.app, store, []):
#                     for a in sd.get('actors', []):
#                         _process_blk_layer(a)

#             if snt_dlg:
#                 for si in getattr(snt_dlg, 'snt_items', []):
#                     for ln, actors_in_cache in getattr(si, 'actor_cache', {}).items():
#                         for a in actors_in_cache:
#                             _process_blk_layer(a, ln)

#             print(
#                 f"  🔵 Phase A.5 (block-label layer spatial): {len(seen_bl)} actors hidden "
#                 f"across {len(hidden_boundary_labels)} boundary label(s)"
#             )

#         # ── Phase B: spatial + layer fallback ───────────────────────────────
#         # Only runs for boundary labels that remain unresolved after earlier phases.
#         # Only hides actors whose centroid is inside a MISSING block's PRJ polygon,
#         # AND whose layer is a recognised block-boundary layer.
#         pending_boundary_labels = {
#             lbl for lbl in missing_poly_by_label.keys()
#             if lbl not in hidden_boundary_labels
#         }
#         if pending_boundary_labels and self.prj_data:
#             print("  🔵 Unresolved boundary labels remain → spatial+layer fallback")

#             _PRJ_BB_LAYERS = {
#                 "bl", "bbox", "bboxes", "blocks", "block",
#                 "block_boundary", "blocks_boundary",
#                 "grid", "grids", "block_boxes", "blockbox",
#             }

#             if not missing_poly_by_label:
#                 print("  ℹ️ No missing-block PRJ polygons — skipping fallback")
#             else:
#                 block_boundary_layers: set = set()
#                 for pi in getattr(self.app, 'snt_block_polygons', []):
#                     lyr = pi.get('poly_layer', '') or pi.get('layer', '')
#                     if lyr:
#                         block_boundary_layers.add(lyr)
#                 for store in ('snt_actors', 'dxf_actors'):
#                     for sd in getattr(self.app, store, []):
#                         for a in sd.get('actors', []):
#                             lyr = getattr(a, '_naksha_snt_layer', '')
#                             if lyr and lyr.lower() in _PRJ_BB_LAYERS:
#                                 block_boundary_layers.add(lyr)
#                 if snt_dlg:
#                     for si in getattr(snt_dlg, 'snt_items', []):
#                         for ln in getattr(si, 'actor_cache', {}):
#                             if ln.lower() in _PRJ_BB_LAYERS:
#                                 block_boundary_layers.add(ln)

#                 def _process_spatial(a):
#                     aid = id(a)
#                     if aid in seen_bl or aid in self._snt_filter_saved_visibility:
#                         return
#                     if getattr(a, 'is_grid_label', False):
#                         return
#                     if getattr(a, 'grid_name', None) is not None:
#                         return  # handled (or not missing) in Phase A
#                     lyr = getattr(a, '_naksha_snt_layer', '')
#                     # Layer-matched actors: standard check
#                     if lyr in block_boundary_layers or lyr.lower() in _PRJ_BB_LAYERS:
#                         matched_label = _find_missing_label_for_actor(a, pending_boundary_labels)
#                         if matched_label:
#                             _hide_actor(a, matched_label)
#                         return
#                     # No layer match — check if actor bounds are FULLY inside a missing PRJ polygon.
#                     # This catches merged/batched geometry on large files where Phase 0 was skipped.
#                     try:
#                         b = a.GetBounds()
#                         # Check all 4 corners of actor bounds — if all inside, it's the block rectangle
#                         corners = [
#                             (b[0], b[2]), (b[1], b[2]),
#                             (b[0], b[3]), (b[1], b[3]),
#                         ]
#                         for lbl in list(pending_boundary_labels):
#                             pts = missing_poly_by_label.get(lbl)
#                             if not pts:
#                                 continue
#                             if all(_snt_point_in_polygon_2d(cx2, cy2, pts) for cx2, cy2 in corners):
#                                 _hide_actor(a, lbl)
#                                 return
#                     except Exception:
#                         pass

#                 for store in ('snt_actors', 'dxf_actors'):
#                     for sd in getattr(self.app, store, []):
#                         for a in sd.get('actors', []):
#                             _process_spatial(a)

#                 if snt_dlg:
#                     for si in getattr(snt_dlg, 'snt_items', []):
#                         for ln, actors in getattr(si, 'actor_cache', {}).items():
#                             for a in actors:
#                                 _process_spatial(a)

#                 print(
#                     f"  🔵 Phase B (spatial) result: {len(seen_bl)} actors hidden "
#                     f"across {len(hidden_boundary_labels)} boundary label(s)"
#                 )

#         # ════════════════════════════════════════════════════════════════════
#         # PHASE C: Rebuild block-boundary outlines from PRJ data
#         # Creates new VTK actors for ALL blocks using PRJ boundary_coords.
#         # Present block outlines are visible; missing block outlines hidden.
#         # Registered in actor_cache + snt_actors/dxf_actors for layer-system
#         # compatibility.
#         # ════════════════════════════════════════════════════════════════════
#         if self.prj_data:
#             visible_polys: list = []
#             hidden_poly_count = 0

#             for bd in self.prj_data:
#                 lbl = bd.get('label', '')
#                 coords = bd.get('boundary_coords', [])
#                 if len(coords) < 3:
#                     continue
#                 if lbl in present_labels:
#                     visible_polys.append(coords)
#                 elif lbl in missing_labels:
#                     hidden_poly_count += 1
#                 else:
#                     visible_polys.append(coords)

#             print(f"  🔵 Phase C: {len(visible_polys)} visible, "
#                   f"{hidden_poly_count} hidden (missing LAZ)")

#             if visible_polys:
#                 try:
#                     import vtk
#                     import numpy as np
#                     try:
#                         from vtkmodules.util import numpy_support
#                     except ImportError:
#                         # Some vtk packaging variants don't expose vtkmodules.util.
#                         # Avoid importing vtk.util directly (some linters/ENVs
#                         # can't resolve it). Provide minimal compatible helpers
#                         # that emulate numpy_support.numpy_to_vtk and
#                         # numpy_to_vtkIdTypeArray using pure-VTK APIs.
#                         numpy_support = None

#                     all_pts: list = []
#                     cell_data: list = []
#                     pt_offset = 0

#                     for poly in visible_polys:
#                         ring = list(poly) + [poly[0]]
#                         n = len(ring)
#                         for x, y in ring:
#                             all_pts.append([float(x), float(y), 0.0])
#                         cell_data.append(n)
#                         cell_data.extend(range(pt_offset, pt_offset + n))
#                         pt_offset += n

#                     pts_np = np.array(all_pts, dtype=np.float32)
#                     cells_np = np.array(cell_data, dtype=np.int64)

#                     vtk_pts = vtk.vtkPoints()
#                     vtk_pts.SetData(numpy_support.numpy_to_vtk(pts_np, deep=True))

#                     vtk_cells = vtk.vtkCellArray()
#                     vtk_cells.ImportLegacyFormat(
#                         numpy_support.numpy_to_vtkIdTypeArray(cells_np))

#                     pd = vtk.vtkPolyData()
#                     pd.SetPoints(vtk_pts)
#                     pd.SetLines(vtk_cells)

#                     mapper = vtk.vtkPolyDataMapper()
#                     mapper.SetInputData(pd)
#                     mapper.SetResolveCoincidentTopologyToPolygonOffset()
#                     mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(-10000.0, -10000.0)

#                     new_actor = vtk.vtkActor()
#                     new_actor.SetMapper(mapper)
#                     new_actor.GetProperty().SetColor(0.6, 0.4, 0.8)
#                     new_actor.GetProperty().SetLineWidth(4.0)
#                     new_actor.GetProperty().SetLighting(False)
#                     new_actor.GetProperty().SetAmbient(1.0)
#                     new_actor.GetProperty().SetOpacity(1.0)
#                     new_actor._is_filter_overlay = True
#                     new_actor._original_color = (153, 102, 204)

#                     renderer.AddActor(new_actor)
#                     self._snt_filter_overlay_actors.append(new_actor)

#                     # Register in snt_actors/dxf_actors so _ensure_overlay_actors finds it
#                     overlay_entry = {
#                         "filename": "_prj_boundary_overlay",
#                         "full_path": "",
#                         "actors": [new_actor],
#                         "bounds": None,
#                     }
#                     self.app.snt_actors.append(overlay_entry)
#                     self.app.dxf_actors.append(overlay_entry)

#                     # Register in actor_cache under dedicated layer name
#                     _PRJ_CACHE_LAYER = "_PRJ_BOUNDARY"
#                     if snt_dlg:
#                         for si in getattr(snt_dlg, 'snt_items', []):
#                             si.actor_cache.setdefault(_PRJ_CACHE_LAYER, []).append(new_actor)

#                 except Exception as e:
#                     print(f"  ⚠️ Phase C rebuild failed: {e}")
#                     import traceback
#                     traceback.print_exc()

#         unresolved_labels = [
#             lbl for lbl in missing_poly_by_label
#             if lbl not in hidden_boundary_labels
#         ]
#         if unresolved_labels:
#             print(
#                 f"  ⚠️ Unresolved missing block boundaries still visible: {unresolved_labels}"
#             )

#         if not seen_bl and not unresolved_labels:
#             print("  ℹ️ No block actors found to hide")

#         self._safe_render_main_view()

#     def _get_snt_laz_directories(self) -> list:
#         """
#         Return a deduplicated list of absolute directory paths that contain the
#         LAZ files for the currently loaded SNT attachments.

#         Logic:
#           - Each SNT attachment in app.snt_attachments has a "full_path" key
#             that holds the absolute path to the .snt file.
#           - The LAZ files live in the SAME folder as the .snt file.
#           - We collect the parent directory of every loaded SNT, deduplicate,
#             and return the list so the table-population code can search only
#             those directories for block LAZ files.

#         This ensures:
#           • If you loaded Dogana SNT  → only Dogana LAZ files are "found".
#           • Blocks from a different area PRJ show "No file in path" correctly.
#           • If a block is genuinely missing from the Dogana folder it also
#             shows "No file in path".
#         """
#         dirs = []
#         try:
#             attachments = getattr(self.app, 'snt_attachments', []) or []
#             for attachment in attachments:
#                 # "full_path" is set by SNTAttachmentDialog when the SNT is loaded
#                 full_path = attachment.get('full_path', '')
#                 if full_path:
#                     parent = os.path.dirname(os.path.abspath(full_path))
#                     if parent and parent not in dirs:
#                         dirs.append(parent)
#                         print(f"  📂 SNT LAZ dir: {parent}")
#         except Exception as e:
#             print(f"  ⚠️ _get_snt_laz_directories error: {e}")
#         return dirs

#     def get_laz_point_count(self, laz_path):
#         """
#         Get the total point count from a LAZ/LAS file.
#         Returns point count or 0 if file not found/error.
#         """
#         try:
#             from pathlib import Path
            
#             file_path = Path(laz_path)
            
#             # Check if file exists
#             if not file_path.exists():
#                 print(f"  ⚠️ File not found: {laz_path}")
#                 return 0
            
#             # Try to read point count using laspy
#             try:
#                 import laspy
#                 with laspy.open(str(file_path)) as las_file:
#                     point_count = las_file.header.point_count
#                     print(f"  ✅ {file_path.name}: {point_count:,} points")
#                     return point_count
#             except ImportError:
#                 print("  ⚠️ laspy not installed - cannot read point count")
#                 return 0
#             except Exception as e:
#                 print(f"  ⚠️ Failed to read {file_path.name}: {e}")
#                 return 0
                
#         except Exception as e:
#             print(f"  ❌ Error getting point count: {e}")
#             return 0        
            
            
#     def calculate_grid_area(self, block_name):
#         """
#         Calculate approximate area of a grid block from DXF geometry.
#         Returns area in square meters or 0 if cannot calculate.
#         """
#         try:
#             # Get the block's polyline/rectangle from DXF
#             # This is a simple approach - measure bounding box
            
#             if not hasattr(self, 'dxf_blocks') or block_name not in self.dxf_blocks:
#                 return 0
            
#             block_data = self.dxf_blocks[block_name]
            
#             # Get all line/polyline coordinates for this block
#             all_x = []
#             all_y = []
            
#             for entity in block_data.get('entities', []):
#                 if entity['type'] == 'line':
#                     all_x.extend([entity['start'][0], entity['end'][0]])
#                     all_y.extend([entity['start'][1], entity['end'][1]])
#                 elif entity['type'] == 'polyline':
#                     for pt in entity['points']:
#                         all_x.append(pt[0])
#                         all_y.append(pt[1])
            
#             if not all_x or not all_y:
#                 return 0
            
#             # Calculate bounding box area
#             width = max(all_x) - min(all_x)
#             height = max(all_y) - min(all_y)
#             area = width * height
            
#             return area
            
#         except Exception as e:
#             print(f"  ⚠️ Error calculating area: {e}")
#             return 0        
#     def calculate_grid_area_from_prj(self, block_data):
#         """
#         Calculate exact polygon area from PRJ boundary coordinates using the
#         Shoelace (Gauss's area) formula. Returns area in square metres.
#         """
#         try:
#             coords = block_data.get('boundary_coords', [])
#             if len(coords) < 3:
#                 print(f"  ⚠️ Not enough coords for area ({len(coords)} found), need >= 3")
#                 return 0

#             # Shoelace formula: A = 0.5 * |sum(x_i * y_{i+1} - x_{i+1} * y_i)|
#             n = len(coords)
#             area = 0.0
#             for k in range(n):
#                 x_i, y_i = coords[k]
#                 x_j, y_j = coords[(k + 1) % n]
#                 area += (x_i * y_j) - (x_j * y_i)

#             area = abs(area) / 2.0
#             print(f"  📐 Shoelace area for '{block_data.get('label','?')}': {area:.2f} m² ({n} vertices)")
#             return area

#         except Exception as e:
#             print(f"  ⚠️ Error calculating area: {e}")
#             return 0

#     def closeEvent(self, event):
#         """
#         Hide the dialog instead of destroying it so the loaded PRJ state
#         (blocks table, file path, polygon data) is preserved across open/close cycles.
        
#         The window X button now behaves the same as the Close button — both hide.
#         The dialog is only truly destroyed when the main app shuts down.
        
#         NOTE: this is the ONLY closeEvent — the duplicate below was removed.
#         """
#         # ✅ FIX: intercept the close and hide instead, preserving all PRJ state.
#         # Only allow true destruction when the app is quitting.
#         app = getattr(self, 'app', None)
#         app_is_closing = (app is not None and getattr(app, '_is_closing', False))

#         if not app_is_closing:
#             # Deactivate fence / add-by-boundaries / edit-definition modes so
#             # the event filter is removed before hiding
#             self._fence_deactivate(cancel=False)
#             self._fence_highlight_clear()
#             self._addb_deactivate(cancel=True)
#             self._editdef_deactivate(cancel=True)
#             event.ignore()
#             self.hide()
#             return

#         # App is shutting down — allow true close and clean up references.
#         try:
#             if self._chip is not None:
#                 try:
#                     self._chip.restore_requested.disconnect()
#                     self._chip.hide()
#                     self._chip.deleteLater()
#                 except Exception:
#                     pass
#                 self._chip = None

#             # Only clear the app reference on true shutdown, not on every hide.
#             if app is not None and hasattr(app, 'block_identifier_dialog') \
#                     and app.block_identifier_dialog is self:
#                 app.block_identifier_dialog = None

#         except Exception:
#             pass
#         super().closeEvent(event)

# def show_block_identifier_dialog(app):
#     """Show the PRJ block identifier dialog (persistent reference).
    
#     The dialog is never destroyed on close — it is hidden so that the loaded
#     PRJ state (blocks table, polygon data, file path) survives across open/close
#     cycles. Calling this function always brings the existing dialog back.
#     """
#     if hasattr(app, 'block_identifier_dialog') and app.block_identifier_dialog:
#         try:
#             dlg = app.block_identifier_dialog
#             if dlg._is_minimized_to_chip:
#                 dlg._do_restore_from_chip()
#             else:
#                 if dlg.windowState() & Qt.WindowMinimized:
#                     dlg.showNormal()
#                 # ✅ FIX: show() works whether the dialog was hidden or just obscured.
#                 # Previously only raise_/activateWindow were called, which fail when
#                 # the dialog was hidden via hide() from the Close button.
#                 dlg.show()
#                 dlg.raise_()
#                 dlg.activateWindow()
#             return dlg
#         except Exception:
#             pass

#     # First open — create and store the persistent dialog.
#     dialog = PRJBlockIdentifierDialog(app, parent=app)
#     app.block_identifier_dialog = dialog
#     dialog.show()
#     return dialog



"""
PRJ Block Identifier Dialog
Loads PRJ file and allows identification/highlighting of DXF blocks
"""

from gui.minimize_chip import MinimizableDialogMixin
import os
from typing import Optional, Set
from pathlib import Path
from gui.theme_manager import (
    get_dialog_stylesheet,
    ThemeColors
)

from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton,
                               QTableWidget, QTableWidgetItem, QFileDialog,
                               QLabel, QMessageBox, QHeaderView, QWidget,
                               QApplication, QLineEdit)
from PySide6.QtCore import Qt, QTimer, QEvent, Signal, QRect
from PySide6.QtGui import QColor, QPainter, QPen, QBrush
from gui.popup_guard import InputPopupMixin


def _lidar_block_stem(value):
    """Return a block filename without only a terminal LAS/LAZ suffix.

    ``os.path.splitext`` cannot be used for extensionless TerraScan block
    labels such as ``S. PIETRO000025``: it interprets everything from the
    embedded dot onward as an extension and returns just ``S``.
    """
    name = os.path.basename(str(value or '').strip())
    lower_name = name.lower()
    for suffix in ('.laz', '.las'):
        if lower_name.endswith(suffix):
            return name[:-len(suffix)]
    return name


def _lidar_block_key(value):
    """Return a case/punctuation-insensitive key for a LAS/LAZ block."""
    return ''.join(ch.lower() for ch in _lidar_block_stem(value) if ch.isalnum())


def _point_in_polygon_xy(px, py, polygon_xy, eps: float = 1e-9):
    """Return True when a point lies inside or on the edge of a polygon."""
    pts = [(float(x), float(y)) for x, y in polygon_xy]
    n = len(pts)
    if n < 3:
        return False

    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]

        dx = xj - xi
        dy = yj - yi
        seg_len2 = dx * dx + dy * dy
        if seg_len2 > 0.0:
            cross = (px - xi) * dy - (py - yi) * dx
            if abs(cross) <= eps * (abs(dx) + abs(dy) + 1.0):
                dot = (px - xi) * dx + (py - yi) * dy
                if -eps <= dot <= seg_len2 + eps:
                    return True

        if ((yi > py) != (yj > py)):
            safe_dy = dy if abs(dy) > 1e-20 else (1e-20 if dy >= 0 else -1e-20)
            x_hit = (dx * (py - yi) / safe_dy) + xi
            if px <= x_hit + eps:
                inside = not inside
        j = i
    return inside

class _SortableHeader(QHeaderView):
    """
    Custom horizontal header that draws a small sort-cycle button
    on the LEFT side of each section label.
    Clicking that zone cycles: none → asc → desc → none.
    Columns 1 and 2 sort numerically; column 0 sorts alphabetically.
    """
    ICON_W = 18   # px width of the sort-button zone inside each section

    sort_changed = Signal(int, str)   # col_index, "asc" | "desc" | "none"

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self._sort_states = {}        # col_index -> "none" | "asc" | "desc"
        self.setSectionsClickable(True)
        self.sectionClicked.connect(self._on_section_clicked)
        self.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)

    def _next_state(self, col):
        current = self._sort_states.get(col, "none")
        return {"none": "asc", "asc": "desc", "desc": "none"}[current]

    def _on_section_clicked(self, logical_index):
        new_state = self._next_state(logical_index)
        # Clear other columns
        self._sort_states = {logical_index: new_state}
        self.sort_changed.emit(logical_index, new_state)
        self.viewport().update()

    # REPLACE the entire paintSection method
    def paintSection(self, painter: QPainter, rect: QRect, logical_index: int):
        painter.save()
        super().paintSection(painter, rect, logical_index)
        painter.restore()

        state = self._sort_states.get(logical_index, "none")

        icon_rect = QRect(rect.right() - self.ICON_W - 4, rect.y(), self.ICON_W, rect.height())
        mid_x = icon_rect.center().x()
        mid_y = icon_rect.center().y()

        painter.save()
        painter.setClipRect(rect)

        # Theme-aware colours — pure white would be invisible on a light-theme header
        from gui.theme_manager import ThemeColors as _TC
        active_color = QColor(_TC.get("accent"))
        pill_color = QColor(_TC.get("accent"))
        pill_color.setAlpha(35)
        dim_color = QColor(_TC.get("text_secondary"))

        # Background pill when active
        if state != "none":
            painter.setBrush(QBrush(pill_color))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(icon_rect.adjusted(1, 3, -1, -3), 3, 3)

        # Single arrow: dim ▼ when unsorted, bright ▲ for asc, bright ▼ for desc
        if state == "asc":
            color = active_color
            # ▲  tip at top
            pts = [(mid_x - 4, mid_y + 3), (mid_x, mid_y - 3), (mid_x + 4, mid_y + 3)]
        elif state == "desc":
            color = active_color
            # ▼  tip at bottom
            pts = [(mid_x - 4, mid_y - 3), (mid_x, mid_y + 3), (mid_x + 4, mid_y - 3)]
        else:
            color = dim_color
            # dim ▼  (hint that column is sortable)
            pts = [(mid_x - 4, mid_y - 3), (mid_x, mid_y + 3), (mid_x + 4, mid_y - 3)]

        pen = QPen(color, 1.8)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.drawLine(pts[0][0], pts[0][1], pts[1][0], pts[1][1])
        painter.drawLine(pts[1][0], pts[1][1], pts[2][0], pts[2][1])

        painter.restore()

# ============================================================================
# NEW: MINIMIZED CHIP WIDGET (Premium Bottom-Left Docking)
# ============================================================================
class _MinimizedPRJChip(QWidget):
    """
    A small floating chip shown at the bottom-left of the screen
    when the PRJ identifier dialog is minimized.
    """
    restore_requested = Signal()

    def __init__(self, title: str):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setObjectName("minimizedPRJChip")
        
        # Apply premium styling
        from gui.theme_manager import ThemeColors as _TC
        self.setStyleSheet(f"""
            QWidget#minimizedPRJChip {{
                background-color: {_TC.get('bg_secondary')};
                border: 1px solid {_TC.get('accent')};
                border-radius: 6px;
            }}
            QLabel#chipLabel {{
                color: {_TC.get('accent')};
                font-size: 10px;
                font-weight: bold;
                padding: 2px 6px;
            }}
            QPushButton#chipRestoreBtn {{
                background-color: {_TC.get('bg_button')};
                border: none;
                border-radius: 4px;
                color: {_TC.get('accent')};
                font-size: 10px;
                font-weight: bold;
                padding: 2px 8px;
                min-width: 0;
            }}
            QPushButton#chipRestoreBtn:hover {{
                background-color: {_TC.get('bg_button_hover')};
            }}
        """)
        self.setCursor(Qt.PointingHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(10)

        # Icon or prefix
        icon_lbl = QLabel("🔍")
        icon_lbl.setObjectName("chipLabel")
        lay.addWidget(icon_lbl)

        # Title
        display_title = title if len(title) < 25 else title[:22] + "..."
        title_lbl = QLabel(display_title)
        title_lbl.setObjectName("chipLabel")
        lay.addWidget(title_lbl)

        # Restore button
        restore_btn = QPushButton("▲ Restore")
        restore_btn.setObjectName("chipRestoreBtn")
        restore_btn.clicked.connect(self.restore_requested.emit)
        lay.addWidget(restore_btn)

        self.adjustSize()
        self.place_bottom_left()

    def place_bottom_left(self):
        """Anchor to the bottom-left of the available screen geometry."""
        screen = QApplication.primaryScreen().availableGeometry()
        margin = 10
        self.move(screen.left() + margin, screen.bottom() - self.height() - margin)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.restore_requested.emit()
        super().mousePressEvent(event)


class AddBlocksByBoundariesDialog(InputPopupMixin, QDialog):
    """Naming dialog shown after drawing one or more boundary rectangles.

    Mirrors MicroStation's "Add Blocks by Boundaries" dialog: a file prefix,
    a numbering mode, and a starting number.  Block names are built as
    f"{prefix}{number:06d}" — the same zero-padded style already used by
    the existing PRJ blocks in this app.
    """

    def __init__(self, count: int, default_prefix: str = "", default_first_number: int = 1, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import (
            QFormLayout, QComboBox, QSpinBox, QDialogButtonBox
        )

        self.setWindowTitle("Add Blocks by Boundaries")
        self.setModal(True)
        self.setMinimumWidth(320)
        self.setStyleSheet(get_dialog_stylesheet())

        layout = QVBoxLayout(self)
        info = QLabel(f"{count} boundary{'ies' if count != 1 else 'y'} drawn — name the new block(s):")
        info.setWordWrap(True)
        layout.addWidget(info)

        form = QFormLayout()
        self.prefix_edit = QLineEdit(default_prefix)
        self.prefix_edit.setPlaceholderText("e.g. FORNACE")
        form.addRow("File prefix:", self.prefix_edit)

        self.numbering_combo = QComboBox()
        self.numbering_combo.addItem("Selection order", "selection_order")
        form.addRow("Numbering:", self.numbering_combo)

        self.first_number_spin = QSpinBox()
        self.first_number_spin.setRange(1, 999999)
        self.first_number_spin.setValue(max(1, default_first_number))
        form.addRow("First number:", self.first_number_spin)

        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_values(self):
        """Returns (prefix: str, first_number: int)."""
        return self.prefix_edit.text().strip(), self.first_number_spin.value()


# ── Project Information helpers ───────────────────────────────────────────────

class _AttributesToStoreDialog(InputPopupMixin, QDialog):
    """Sub-dialog opened by the 'Attributes...' button in Project Information."""

    TIME_MODES = [
        "GPS seconds-of-week",
        "GPS week time",
        "GPS adjusted standard time",
        "Unix time",
    ]

    # (display label, prj key, default_checked, xyz_disabled)
    _LEFT = [
        ("Xyz",          "AttrXyz",    True,  True),
        ("Class",        "AttrClass",  True,  False),
        ("Line",         "AttrLine",   True,  False),
        ("Echo",         "AttrEcho",   True,  False),
        ("Distance",     "AttrDist",   False, False),
        ("Amplitude",    "AttrAmpl",   False, False),
        ("Group",        "AttrGroup",  False, False),
        ("Image number", "AttrImgNum", False, False),
        ("Normal vector","AttrNormal", False, False),
        ("Intensity",    "AttrIntens", True,  False),
    ]
    _RIGHT = [
        ("Scanner",        "AttrScanner",  True,  False),
        ("Angle",          "AttrAngle",    True,  False),
        ("Color",          "AttrColor",    True,  False),
        ("Time",           "AttrTime",     True,  False),   # has dropdown
        ("Echo length",    "AttrEchoLen",  False, False),
        ("Echo normality", "AttrEchoNorm", False, False),
        ("Echo position",  "AttrEchoPos",  False, False),
        ("Reflectance",    "AttrRefl",     False, False),
        ("Deviation",      "AttrDev",      False, False),
        ("Reliability",    "AttrReli",     False, False),
    ]

    def __init__(self, fields: dict, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QDialogButtonBox, QCheckBox, QComboBox, QGridLayout, QGroupBox
        self.setWindowTitle("Attributes to store")
        self.setModal(True)
        self.setMinimumWidth(460)
        self.setStyleSheet(get_dialog_stylesheet())

        root = QVBoxLayout(self)

        grid = QGridLayout()
        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 0)
        grid.setColumnStretch(2, 1)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(4)

        self._checks: dict = {}
        self._time_combo: QComboBox = None

        # Left column (col 0)
        for row, (label, key, default, disabled) in enumerate(self._LEFT):
            cb = QCheckBox(label)
            cb.setChecked(bool(int(fields.get(key, "1" if default else "0"))))
            if disabled:
                cb.setEnabled(False)
            self._checks[key] = cb
            grid.addWidget(cb, row, 0)

        # Right column (col 1-2)
        for row, (label, key, default, _disabled) in enumerate(self._RIGHT):
            cb = QCheckBox(label)
            cb.setChecked(bool(int(fields.get(key, "1" if default else "0"))))
            self._checks[key] = cb
            grid.addWidget(cb, row, 1)
            if label == "Time":
                combo = QComboBox()
                for m in self.TIME_MODES:
                    combo.addItem(m)
                saved_mode = int(fields.get("AttrTimeMode", "0"))
                combo.setCurrentIndex(max(0, min(saved_mode, len(self.TIME_MODES) - 1)))
                self._time_combo = combo
                grid.addWidget(combo, row, 2)

        root.addLayout(grid)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def get_fields(self) -> dict:
        out = {}
        for key, cb in self._checks.items():
            out[key] = "1" if cb.isChecked() else "0"
        if self._time_combo is not None:
            out["AttrTimeMode"] = str(self._time_combo.currentIndex())
        return out


class ProjectInformationDialog(InputPopupMixin, QDialog):
    """MicroStation-style Project Information dialog.

    Used for both 'New project' (defaults) and 'Edit project information'
    (pre-populated from existing PRJ header).  On OK, returns a fields dict
    that the caller writes into the PRJ header.
    """

    CLOUD_TYPES = [
        ("Airborne lidar",       "0"),
        ("Mobile lidar",         "1"),
        ("Aerial camera",        "2"),
        ("Terrestrial scanner",  "3"),
    ]
    STORAGE_FORMATS = [
        ("Fast binary",          "Fast binary"),
        ("Scan binary 8 bit",    "Scan binary 8 bit"),
        ("Scan binary 16 bit",   "Scan binary 16 bit"),
        ("LAS 1.0",              "LAS 1.0"),
        ("LAS 1.1",              "LAS 1.1"),
        ("LAS 1.2",              "LAS 1.2"),
        ("LAS 1.4",              "LAS 1.4"),
        ("LAZ 0.1",              "LAZ 0.1"),
        ("LAZ 1.1",              "LAZ 1.1"),
        ("LAZ 1.2",              "LAZ 1.2"),
        ("LAZ 1.3",              "LAZ 1.3"),
        ("LAZ 1.4",              "LAZ 1.4"),
        ("GeoTIFF",              "GeoTIFF"),
    ]
    DATA_IN = [
        ("Project file directory", "0"),
        ("Defined directory",      "1"),
    ]
    NEIGHBOUR_AREAS = [
        ("Rounded corners",    "0"),
        ("Rectangle corners",  "1"),
    ]
    BLOCK_NAMING = [
        ("Number",     "0"),
        ("Characters", "1"),
    ]

    _DEFAULTS = {
        "CloudType":          "0",
        "Description":        "",
        "FirstPointId":       "1",
        "Storage":            "Fast binary",
        "RequireFileLocking": "0",
        "Projection":         "",
        "DataIn":             "0",
        "Directory":          "",
        "LoadClassFile":      "0",
        "ClassFile":          "",
        "LoadColorFile":      "0",
        "ColorFile":          "",
        "LoadTrajectories":   "0",
        "TrajDirectory":      "",
        "ReferenceProject":   "0",
        "ReferenceFile":      "",
        "NeighbourArea":      "0",
        "BlockSize":          "1000",
        "BlockPrefix":        "pt",
        "GroupCount":         "1000000",
        "BlockNaming":        "0",
        # Attribute defaults
        "AttrXyz":      "1", "AttrClass":    "1", "AttrLine":    "1",
        "AttrEcho":     "1", "AttrDist":     "0", "AttrAmpl":    "0",
        "AttrGroup":    "0", "AttrImgNum":   "0", "AttrNormal":  "0",
        "AttrIntens":   "1", "AttrScanner":  "1", "AttrAngle":   "1",
        "AttrColor":    "1", "AttrTime":     "1", "AttrTimeMode":"0",
        "AttrEchoLen":  "0", "AttrEchoNorm": "0", "AttrEchoPos": "0",
        "AttrRefl":     "0", "AttrDev":      "0", "AttrReli":    "0",
    }

    def __init__(self, fields: dict = None, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import (
            QDialogButtonBox, QCheckBox, QComboBox, QSpinBox,
            QFormLayout, QScrollArea, QFrame, QGridLayout,
        )
        self.setWindowTitle("Project Information")
        self.setModal(True)
        self.setMinimumWidth(560)
        self.setMinimumHeight(520)
        self.setStyleSheet(get_dialog_stylesheet())

        # Solid-background override for combo popups — prevents form content
        # bleeding through the dropdown on Windows 11 with border-radius.
        _bg = ThemeColors.get("bg_input")
        _fg = ThemeColors.get("text_primary")
        _sel = ThemeColors.get("dialog_selection")
        _bdr = ThemeColors.get("border")
        _combo_popup_ss = (
            f"QComboBox QAbstractItemView {{"
            f"  background-color: {_bg};"
            f"  color: {_fg};"
            f"  selection-background-color: {_sel};"
            f"  selection-color: {_fg};"
            f"  border: 1px solid {_bdr};"
            f"  border-radius: 0px;"
            f"  padding: 2px 0px;"
            f"  outline: none;"
            f"}}"
        )

        # Merge supplied fields with defaults
        self._fields = dict(self._DEFAULTS)
        if fields:
            self._fields.update({k: v for k, v in fields.items() if v is not None})

        # ── Root: scroll area (content) + fixed button bar ──────────────────
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        content = QWidget()
        content.setStyleSheet("background: transparent;")
        main = QVBoxLayout(content)
        main.setContentsMargins(14, 12, 14, 10)
        main.setSpacing(8)

        def _rl(text):
            lbl = QLabel(text)
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            return lbl

        # ── Section 1: Core project settings ───────────────────────────────
        form1 = QFormLayout()
        form1.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form1.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        form1.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        form1.setSpacing(5)
        form1.setContentsMargins(0, 0, 0, 0)

        self._cloud_combo = QComboBox()
        self._cloud_combo.setMaxVisibleItems(8)
        self._cloud_combo.setStyleSheet(_combo_popup_ss)
        for lbl, code in self.CLOUD_TYPES:
            self._cloud_combo.addItem(lbl, code)
        self._set_combo(self._cloud_combo, self._fields["CloudType"])
        form1.addRow("Cloud type:", self._cloud_combo)

        self._desc_edit = QLineEdit(self._fields["Description"])
        form1.addRow("Description:", self._desc_edit)

        self._first_pt_spin = QSpinBox()
        self._first_pt_spin.setRange(1, 2_147_483_647)
        try:
            self._first_pt_spin.setValue(int(self._fields["FirstPointId"]))
        except (ValueError, OverflowError):
            self._first_pt_spin.setValue(1)
        form1.addRow("First point id:", self._first_pt_spin)

        self._storage_combo = QComboBox()
        self._storage_combo.setMaxVisibleItems(8)
        self._storage_combo.setStyleSheet(_combo_popup_ss)
        for lbl, code in self.STORAGE_FORMATS:
            self._storage_combo.addItem(lbl, code)
        self._set_combo(self._storage_combo, self._fields["Storage"], by_data=True)
        _stor_row = QHBoxLayout()
        _stor_row.setSpacing(6)
        _stor_row.addWidget(self._storage_combo, 1)
        _attr_btn = QPushButton("Attributes...")
        _attr_btn.clicked.connect(self._open_attributes)
        _stor_row.addWidget(_attr_btn)
        form1.addRow("Storage:", _stor_row)

        _lock_row = QHBoxLayout()
        _lock_row.setContentsMargins(0, 0, 0, 0)
        self._lock_cb = QCheckBox("Require file locking")
        self._lock_cb.setChecked(self._fields["RequireFileLocking"] == "1")
        _lock_row.addWidget(self._lock_cb)
        _lock_row.addStretch()
        form1.addRow("", _lock_row)

        self._proj_edit = QLineEdit(self._fields["Projection"])
        _proj_row = QHBoxLayout()
        _proj_row.setSpacing(6)
        _proj_row.addWidget(self._proj_edit, 1)
        _proj_browse = QPushButton("Browse...")
        _proj_browse.clicked.connect(
            lambda: self._browse_file(self._proj_edit, "Projection Files (*.prj *.txt);;All Files (*.*)")
        )
        _proj_row.addWidget(_proj_browse)
        form1.addRow("Projection:", _proj_row)

        main.addLayout(form1)
        main.addWidget(self._make_sep())

        # ── Section 2: Data in / Directory ─────────────────────────────────
        form2 = QFormLayout()
        form2.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form2.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        form2.setSpacing(5)
        form2.setContentsMargins(0, 0, 0, 0)

        self._datain_combo = QComboBox()
        self._datain_combo.setMaxVisibleItems(6)
        self._datain_combo.setStyleSheet(_combo_popup_ss)
        for lbl, code in self.DATA_IN:
            self._datain_combo.addItem(lbl, code)
        self._set_combo(self._datain_combo, self._fields["DataIn"])
        form2.addRow("Data in:", self._datain_combo)

        self._dir_edit = QLineEdit(self._fields["Directory"])
        _dir_browse = QPushButton("Browse...")
        _dir_browse.clicked.connect(lambda: self._browse_dir(self._dir_edit))
        _dir_row = QHBoxLayout()
        _dir_row.setSpacing(6)
        _dir_row.addWidget(self._dir_edit, 1)
        _dir_row.addWidget(_dir_browse)
        form2.addRow("Directory:", _dir_row)

        _dir_defined = self._fields["DataIn"] == "1"
        self._dir_edit.setEnabled(_dir_defined)
        _dir_browse.setEnabled(_dir_defined)

        def _on_datain_changed():
            _en = self._datain_combo.currentData() == "1"
            self._dir_edit.setEnabled(_en)
            _dir_browse.setEnabled(_en)

        self._datain_combo.currentIndexChanged.connect(_on_datain_changed)

        main.addLayout(form2)
        main.addWidget(self._make_sep())

        # ── Section 3: Optional file / directory rows ───────────────────────
        opt_vbox = QVBoxLayout()
        opt_vbox.setSpacing(2)
        opt_vbox.setContentsMargins(0, 0, 0, 0)

        self._class_cb, self._class_edit = self._make_opt_file_row(
            opt_vbox, "Load class list automatically",
            "LoadClassFile", "ClassFile",
            "Class Files (*.ptc *.txt);;All Files (*.*)"
        )
        self._color_cb, self._color_edit = self._make_opt_file_row(
            opt_vbox, "Load line colors automatically",
            "LoadColorFile", "ColorFile",
            "Color Files (*.clr *.txt);;All Files (*.*)"
        )
        self._traj_cb, self._traj_edit = self._make_opt_dir_row(
            opt_vbox, "Load trajectories automatically",
            "LoadTrajectories", "TrajDirectory"
        )
        self._ref_cb, self._ref_edit = self._make_opt_file_row(
            opt_vbox, "Reference project exists",
            "ReferenceProject", "ReferenceFile",
            "PRJ Files (*.prj);;All Files (*.*)"
        )

        main.addLayout(opt_vbox)
        main.addWidget(self._make_sep())

        # ── Section 4: Default block values ────────────────────────────────
        _blk_hdr = QLabel("Default block values")
        _blk_hdr.setStyleSheet("font-weight: bold; font-size: 10pt;")
        main.addWidget(_blk_hdr)

        block_grid = QGridLayout()
        block_grid.setHorizontalSpacing(8)
        block_grid.setVerticalSpacing(5)
        block_grid.setColumnStretch(1, 1)
        block_grid.setColumnStretch(3, 1)
        block_grid.setContentsMargins(0, 4, 0, 4)

        # Row 0: Neighbour area | Block prefix
        block_grid.addWidget(_rl("Neighbour area:"), 0, 0)
        self._neighbour_combo = QComboBox()
        self._neighbour_combo.setMaxVisibleItems(6)
        self._neighbour_combo.setStyleSheet(_combo_popup_ss)
        for lbl, code in self.NEIGHBOUR_AREAS:
            self._neighbour_combo.addItem(lbl, code)
        self._set_combo(self._neighbour_combo, self._fields["NeighbourArea"])
        block_grid.addWidget(self._neighbour_combo, 0, 1)
        block_grid.addWidget(_rl("Block prefix:"), 0, 2)
        self._prefix_edit = QLineEdit(self._fields["BlockPrefix"])
        self._prefix_edit.setMaximumWidth(80)
        block_grid.addWidget(self._prefix_edit, 0, 3)

        # Row 1: Block size | Block naming
        block_grid.addWidget(_rl("Block size:"), 1, 0)
        self._block_size_spin = QSpinBox()
        self._block_size_spin.setRange(1, 999_999)
        try:
            self._block_size_spin.setValue(int(self._fields["BlockSize"]))
        except (ValueError, OverflowError):
            self._block_size_spin.setValue(1000)
        _bsz_w = QWidget()
        _bsz_h = QHBoxLayout(_bsz_w)
        _bsz_h.setContentsMargins(0, 0, 0, 0)
        _bsz_h.setSpacing(4)
        _bsz_h.addWidget(self._block_size_spin)
        _bsz_h.addWidget(QLabel("m"))
        _bsz_h.addStretch()
        block_grid.addWidget(_bsz_w, 1, 1)
        block_grid.addWidget(_rl("Block naming:"), 1, 2)
        self._naming_combo = QComboBox()
        self._naming_combo.setMaxVisibleItems(6)
        self._naming_combo.setStyleSheet(_combo_popup_ss)
        for lbl, code in self.BLOCK_NAMING:
            self._naming_combo.addItem(lbl, code)
        self._set_combo(self._naming_combo, self._fields["BlockNaming"])
        block_grid.addWidget(self._naming_combo, 1, 3)

        # Row 2: Group count
        block_grid.addWidget(_rl("Group count:"), 2, 0)
        self._group_count_spin = QSpinBox()
        self._group_count_spin.setRange(1, 2_147_483_647)
        try:
            self._group_count_spin.setValue(int(self._fields["GroupCount"]))
        except (ValueError, OverflowError):
            self._group_count_spin.setValue(1_000_000)
        block_grid.addWidget(self._group_count_spin, 2, 1)

        main.addLayout(block_grid)
        main.addStretch()

        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        # ── Button bar (outside scroll — always visible) ────────────────────
        root.addWidget(self._make_sep())
        _btn_bar = QWidget()
        _btn_h = QHBoxLayout(_btn_bar)
        _btn_h.setContentsMargins(14, 6, 14, 10)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        _btn_h.addWidget(buttons)
        root.addWidget(_btn_bar)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _set_combo(combo, value: str, by_data: bool = False):
        """Select the combo item whose data (by_data=True) or text (by_data=False) matches value."""
        for i in range(combo.count()):
            cmp = combo.itemData(i) if by_data else combo.itemText(i)
            if cmp == value:
                combo.setCurrentIndex(i)
                return
        # Fall back: search both data and text so a value stored either way still matches
        for i in range(combo.count()):
            if combo.itemData(i) == value or combo.itemText(i) == value:
                combo.setCurrentIndex(i)
                return
        combo.setCurrentIndex(0)

    def _make_sep(self) -> QWidget:
        sep = QWidget()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background:{ThemeColors.get('border')};")
        return sep

    def _make_opt_file_row(self, vbox, label, flag_key, path_key, file_filter):
        from PySide6.QtWidgets import QCheckBox
        cb = QCheckBox(label)
        cb.setChecked(self._fields.get(flag_key, "0") == "1")
        vbox.addWidget(cb)

        path_w = QWidget()
        path_h = QHBoxLayout(path_w)
        path_h.setContentsMargins(20, 0, 0, 2)
        path_h.setSpacing(6)
        edit = QLineEdit(self._fields.get(path_key, ""))
        edit.setEnabled(cb.isChecked())
        path_h.addWidget(edit, 1)
        btn = QPushButton("Browse...")
        btn.setEnabled(cb.isChecked())
        ff = file_filter

        def _browse():
            path, _ = QFileDialog.getOpenFileName(self, "Select File", edit.text() or "", ff)
            if path:
                edit.setText(path)

        btn.clicked.connect(_browse)
        path_h.addWidget(btn)
        vbox.addWidget(path_w)

        def _toggle(checked):
            edit.setEnabled(checked)
            btn.setEnabled(checked)

        cb.toggled.connect(_toggle)
        return cb, edit

    def _make_opt_dir_row(self, vbox, label, flag_key, path_key):
        from PySide6.QtWidgets import QCheckBox
        cb = QCheckBox(label)
        cb.setChecked(self._fields.get(flag_key, "0") == "1")
        vbox.addWidget(cb)

        path_w = QWidget()
        path_h = QHBoxLayout(path_w)
        path_h.setContentsMargins(20, 0, 0, 2)
        path_h.setSpacing(6)
        edit = QLineEdit(self._fields.get(path_key, ""))
        edit.setEnabled(cb.isChecked())
        path_h.addWidget(edit, 1)
        btn = QPushButton("Browse...")
        btn.setEnabled(cb.isChecked())

        def _browse():
            d = QFileDialog.getExistingDirectory(self, "Select Directory", edit.text() or "")
            if d:
                edit.setText(d)

        btn.clicked.connect(_browse)
        path_h.addWidget(btn)
        vbox.addWidget(path_w)

        def _toggle(checked):
            edit.setEnabled(checked)
            btn.setEnabled(checked)

        cb.toggled.connect(_toggle)
        return cb, edit

    def _browse_file(self, edit, file_filter):
        path, _ = QFileDialog.getOpenFileName(self, "Select File", edit.text() or "", file_filter)
        if path:
            edit.setText(path)

    def _browse_dir(self, edit):
        d = QFileDialog.getExistingDirectory(self, "Select Directory", edit.text() or "")
        if d:
            edit.setText(d)

    def _open_attributes(self):
        dlg = _AttributesToStoreDialog(self._fields, parent=self)
        if dlg.exec() == QDialog.Accepted:
            self._fields.update(dlg.get_fields())

    def get_fields(self) -> dict:
        """Return the complete fields dict after user edits."""
        out = dict(self._fields)  # carries attribute flags updated via sub-dialog
        out["CloudType"]         = self._cloud_combo.currentData() or "0"
        out["Description"]       = self._desc_edit.text().strip()
        out["FirstPointId"]      = str(self._first_pt_spin.value())
        out["Storage"]           = self._storage_combo.currentData() or "Fast binary"
        out["RequireFileLocking"]= "1" if self._lock_cb.isChecked() else "0"
        out["Projection"]        = self._proj_edit.text().strip()
        out["DataIn"]            = self._datain_combo.currentData() or "0"
        out["Directory"]         = self._dir_edit.text().strip()
        out["LoadClassFile"]     = "1" if self._class_cb.isChecked() else "0"
        out["ClassFile"]         = self._class_edit.text().strip()
        out["LoadColorFile"]     = "1" if self._color_cb.isChecked() else "0"
        out["ColorFile"]         = self._color_edit.text().strip()
        out["LoadTrajectories"]  = "1" if self._traj_cb.isChecked() else "0"
        out["TrajDirectory"]     = self._traj_edit.text().strip()
        out["ReferenceProject"]  = "1" if self._ref_cb.isChecked() else "0"
        out["ReferenceFile"]     = self._ref_edit.text().strip()
        out["NeighbourArea"]     = self._neighbour_combo.currentData() or "0"
        out["BlockSize"]         = str(self._block_size_spin.value())
        out["BlockPrefix"]       = self._prefix_edit.text().strip() or "pt"
        out["GroupCount"]        = str(self._group_count_spin.value())
        out["BlockNaming"]       = self._naming_combo.currentData() or "0"
        return out

    @staticmethod
    def fields_to_header_text(fields: dict) -> str:
        """Serialise fields dict → PRJ header text (before any Block sections)."""
        # Field order matches MicroStation's layout for readability
        ordered_keys = [
            "Description", "CloudType", "FirstPointId", "Storage",
            "RequireFileLocking", "Projection", "DataIn", "Directory",
            "LoadClassFile", "ClassFile", "LoadColorFile", "ColorFile",
            "LoadTrajectories", "TrajDirectory", "ReferenceProject", "ReferenceFile",
            "NeighbourArea", "BlockSize", "BlockPrefix", "GroupCount", "BlockNaming",
            "AttrXyz", "AttrClass", "AttrLine", "AttrEcho", "AttrDist",
            "AttrAmpl", "AttrGroup", "AttrImgNum", "AttrNormal", "AttrIntens",
            "AttrScanner", "AttrAngle", "AttrColor", "AttrTime", "AttrTimeMode",
            "AttrEchoLen", "AttrEchoNorm", "AttrEchoPos", "AttrRefl",
            "AttrDev", "AttrReli",
        ]
        lines = ["[TerraScan project]"]
        written = set()
        for key in ordered_keys:
            if key in fields:
                lines.append(f"{key}={fields[key]}")
                written.add(key)
        # Preserve any unknown keys from original (e.g. added by TerraScan itself)
        for key, val in fields.items():
            if key not in written:
                lines.append(f"{key}={val}")
        lines.append("")  # trailing newline
        return "\n".join(lines)

    @staticmethod
    def parse_header_fields(header_text: str) -> dict:
        """Extract key=value pairs from PRJ header text."""
        fields = {}
        for line in header_text.splitlines():
            line = line.strip()
            if not line or line.startswith("[") or not ("=" in line):
                continue
            key, _, val = line.partition("=")
            fields[key.strip()] = val.strip()
        return fields


class PRJBlockIdentifierDialog(MinimizableDialogMixin, QDialog):
    """Dialog to load PRJ file and identify DXF blocks"""

    def __init__(self, app, parent=None):
        if parent is None:
            if isinstance(app, QWidget):
                parent = app
            elif hasattr(app, "window") and isinstance(app.window, QWidget):
                parent = app.window

        super().__init__(None, Qt.Window)
        self.setAttribute(Qt.WA_QuitOnClose, False)

        self.app = app
        self.prj_data = []
        self.current_dxf_path = None
        self.current_prj_path = None
        self.setProperty("themeStyledDialog", True)

        self._init_minimize_state()

        self.setWindowTitle("PRJ Block Identifier")
        self.setMinimumSize(400, 300)
        self.resize(820, 440)

        self._missing_blocks_hidden = False
        self._hide_missing_btn: Optional[QPushButton] = None
        self._all_row_data = []
        self._last_hide_missing_labels: Set[str] = set()

        self._snt_filter_saved_visibility: dict = {}
        self._snt_filter_overlay_actors: list = []

        self._highlight_identify_id = 0
        self._highlight_auto_clear_timer = None

        # Fence-selection state
        self._fence_active = False
        self._fence_start_screen = None   # (vtk_x, vtk_y) on left-button down
        self._fence_current_screen = None
        self._fence_actor = None          # vtkActor2D rubberband overlay
        self._fence_action = None            # assigned in setup_ui
        self._fence_highlight_actors: list = []   # 3D boundary-polygon highlight actors
        self._fence_mouse_grabbed = False    # True while vtk_widget.grabMouse() is active

        # Add-by-Boundaries state
        self._addb_active = False
        self._addb_start_screen = None    # (vtk_x, vtk_y) on left-button down
        self._addb_drag_actor = None      # vtkActor2D live rubberband overlay
        self._addb_pending_rects: list = []   # world (minx, miny, maxx, maxy) tuples
        self._addb_pending_snt: list = []     # (grid_name, pts_2d) from SNT polygon detection
        self._addb_pending_actors: list = []  # persistent 3D outline actors (orange)
        self._addb_action = None             # assigned in setup_ui
        self._addb_mouse_grabbed = False     # True while vtk_widget.grabMouse() is active

        # Flags used by GlobalShortcutFilter to suppress runtime tool shortcuts
        # (alphabet keys etc.) while PRJ text entry is in progress.
        self._addb_dialog_active = False     # Add Blocks by Boundaries popup open
        self._prj_search_focus = False       # PRJ block-label search box has focus

        # Edit Definition state (single-shot boundary redraw)
        self._editdef_active = False
        self._editdef_start_screen = None
        self._editdef_drag_actor = None       # vtkActor2D live rubberband overlay
        self._editdef_target_prj_idx = None   # which block is being edited
        self._editdef_action = None              # assigned in setup_ui
        self._editdef_mouse_grabbed = False   # True while vtk_widget.grabMouse() is active

        self._apply_dark_theme()
        self.setup_ui()
        self.detect_current_dxf()
        self.current_directory = None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not hasattr(self, '_sort_header') or self._sort_header is None:
            return
        total = event.size().width()
        if total <= 0:
            return
        col0 = max(150, int(total * 0.45))
        col1 = max(100, int(total * 0.28))
        col2 = max(100, total - col0 - col1)
        self.table.setColumnWidth(0, col0)
        self.table.setColumnWidth(1, col1)
        self.table.setColumnWidth(2, col2)

    def _apply_dark_theme(self):
        """Apply the shared application dialog theme, plus a premium polish
        layer scoped to this dialog only (table, header card, search field)."""
        self.setStyleSheet(get_dialog_stylesheet() + self._premium_polish_stylesheet())
        self._update_hide_btn_style()

    def _premium_polish_stylesheet(self) -> str:
        """Extra styling layered on top of the shared dialog theme — elevates
        the table, header card, and search field without touching the global
        theme file (so other dialogs are unaffected)."""
        c = ThemeColors
        bg_secondary = c.get('bg_secondary')
        bg_input = c.get('bg_input')
        border_light = c.get('border_light')
        text_primary = c.get('text_primary')
        accent = c.get('accent')
        bg_hover = c.get('bg_button_hover')
        selection_bg = c.get('dialog_selection')
        selection_text = c.get('text_primary') if c.is_light() else c.get('text_on_active')
        return f"""
            QTableWidget#prjBlockTable {{
                background-color: {bg_secondary};
                alternate-background-color: {bg_input};
                border: 1px solid {border_light};
                border-radius: 10px;
                gridline-color: transparent;
                selection-background-color: {selection_bg};
                padding: 2px;
            }}
            QTableWidget#prjBlockTable::item {{
                padding: 8px 10px;
                border: none;
                border-bottom: 1px solid {border_light};
            }}
            QTableWidget#prjBlockTable::item:selected {{
                background-color: {selection_bg};
                color: {selection_text};
            }}
            QTableWidget#prjBlockTable::item:hover {{
                background-color: {bg_hover};
            }}
            QTableWidget#prjBlockTable QHeaderView::section {{
                background-color: {bg_input};
                color: {text_primary};
                border: none;
                border-bottom: 2px solid {accent};
                padding: 9px 10px;
                font-weight: 700;
                font-size: 10.5pt;
            }}
            QFrame#prjInfoCard {{
                background-color: {bg_secondary};
                border: 1px solid {border_light};
                border-radius: 8px;
            }}
            QLabel#prjInfoLabel {{
                color: {text_primary};
                font-weight: 700;
                font-size: 10.5pt;
                background: transparent;
            }}
            QLineEdit#prjSearchBox {{
                background-color: {bg_input};
                border: 1px solid {border_light};
                border-radius: 13px;
                padding: 4px 14px;
                font-size: 10pt;
            }}
            QLineEdit#prjSearchBox:focus {{
                border: 1px solid {accent};
            }}
        """

    def _style_prj_button(self, btn, object_name, width=140, height=50):
        """Keep PRJ dialog buttons visually consistent without changing behavior."""
        btn.setObjectName(object_name)
        btn.setFixedSize(width, height)
        btn.setStyleSheet(f"""
        QPushButton#{object_name} {{
            min-width: 0px;
            max-width: {width}px;
            min-height: 20px;
            max-height: {height}px;
            padding: 2px 6px;
            font-size: 12px;
            border-radius: 4px;
        }}
        """)

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 16, 16)
        layout.setSpacing(10)

        # ── Menu bar — mirrors MicroStation's "Project:" window (File / Block) ──
        from PySide6.QtWidgets import QMenuBar
        self.menu_bar = QMenuBar(self)
        self.menu_bar.setNativeMenuBar(False)
        bg = ThemeColors.get('bg_secondary')
        bg_hover = ThemeColors.get('bg_button_hover')
        text = ThemeColors.get('text_primary')
        accent = ThemeColors.get('accent')
        self.menu_bar.setStyleSheet(f"""
            QMenuBar {{
                background-color: {bg};
                color: {text};
                border-bottom: 1px solid {accent};
                padding: 2px;
            }}
            QMenuBar::item {{
                background: transparent;
                padding: 4px 10px;
                border-radius: 4px;
            }}
            QMenuBar::item:selected {{
                background-color: {bg_hover};
            }}
            QMenu {{
                background-color: {bg};
                color: {text};
                border: 1px solid {accent};
            }}
            QMenu::item {{
                padding: 6px 24px 6px 16px;
            }}
            QMenu::item:selected {{
                background-color: {bg_hover};
            }}
            QMenu::separator {{
                height: 1px;
                background: {accent};
                margin: 4px 8px;
            }}
        """)
        layout.setMenuBar(self.menu_bar)

        file_menu = self.menu_bar.addMenu("File")
        act_new = file_menu.addAction("New project...")
        act_new.setToolTip("Create a brand-new, empty PRJ file and load it.")
        act_new.triggered.connect(self._new_project)
        act_open = file_menu.addAction("Open project...")
        act_open.setToolTip("Load a PRJ file")
        act_open.triggered.connect(self.load_prj_file)
        act_remove = file_menu.addAction("Remove project")
        act_remove.triggered.connect(self.remove_prj_file)
        file_menu.addSeparator()

        act_save = file_menu.addAction("Save project")
        act_save.setToolTip(
            "Commit pending edits to the real PRJ file.\n"
            "Edits are buffered in a backend working copy until you save here.\n"
            "A fresh backup snapshot (.prj.bak) is created first."
        )
        act_save.triggered.connect(self._save_project)

        act_save_as = file_menu.addAction("Save project As...")
        act_save_as.setToolTip("Copy the current PRJ to a new file and switch to it.")
        act_save_as.triggered.connect(self._save_project_as)
        file_menu.addSeparator()

        act_edit_info = file_menu.addAction("Edit project information...")
        act_edit_info.setToolTip("Edit the PRJ header text (metadata before any Block definitions).")
        act_edit_info.triggered.connect(self._edit_project_information)
        file_menu.addSeparator()

        act_close = file_menu.addAction("Close")
        act_close.triggered.connect(self.hide)

        block_menu = self.menu_bar.addMenu("Block")

        act_add_files = block_menu.addAction("Add using files...")
        act_add_files.setToolTip(
            "Add new block definition(s) from one or more LAZ/LAS files.\n"
            "Each boundary polygon is auto-read from its file header."
        )
        act_add_files.triggered.connect(self.add_block_definition)

        self._addb_action = block_menu.addAction("Add by boundaries...")
        self._addb_action.setCheckable(True)
        self._addb_action.setToolTip(
            "Draw a rectangle on the viewport. If SNT blocks are loaded, all SNT "
            "block polygons inside the rectangle are detected and added as individual "
            "blocks. Without SNT data, the rectangle itself becomes one new block."
        )
        self._addb_action.toggled.connect(self._toggle_addb_mode)

        act_add_snt = block_menu.addAction("Add all SNT blocks...")
        act_add_snt.setToolTip(
            "Add every loaded SNT block polygon to this PRJ in one step.\n"
            "Skips any blocks already present. Matches each SNT polygon exactly."
        )
        act_add_snt.triggered.connect(self._add_blocks_from_snt)

        self._editdef_action = block_menu.addAction("Edit definition...")
        self._editdef_action.setCheckable(True)
        self._editdef_action.setToolTip(
            "Select exactly ONE block, then redraw its boundary on the viewport."
        )
        self._editdef_action.toggled.connect(self._toggle_editdef_mode)

        act_rename = block_menu.addAction("Rename definition...")
        act_rename.triggered.connect(self.rename_block_definition)

        act_delete = block_menu.addAction("Delete definition")
        act_delete.triggered.connect(self.delete_block_definition)

        act_export = block_menu.addAction("Export Sub-PRJ...")
        act_export.triggered.connect(self.export_selected_blocks_prj)

        act_merge = block_menu.addAction("Merge blocks")
        act_merge.setToolTip("Combine 2+ selected blocks into one (union of their boundaries).")
        act_merge.triggered.connect(self._merge_selected_blocks)

        act_lock = block_menu.addAction("Lock selected")
        act_lock.setToolTip(
            "Lock the selected block(s) against Delete / Rename / Edit Definition\n"
            "for this app session. Not written to the PRJ file."
        )
        act_lock.triggered.connect(self._lock_selected_blocks)

        act_unlock = block_menu.addAction("Release lock")
        act_unlock.triggered.connect(self._release_selected_locks)

        block_menu.addSeparator()

        self._fence_action = block_menu.addAction("Select by Fence")
        self._fence_action.setCheckable(True)
        self._fence_action.setToolTip(
            "Drag a rectangle on the viewport to spatially select PRJ blocks."
        )
        self._fence_action.toggled.connect(self._toggle_fence_mode)

        act_select_all = block_menu.addAction("Select all")
        act_select_all.triggered.connect(lambda: self.table.selectAll())

        act_deselect_all = block_menu.addAction("Deselect all")
        act_deselect_all.triggered.connect(self._deselect_all)

        block_menu.addSeparator()

        act_identify = block_menu.addAction("Identify")
        act_identify.triggered.connect(self.identify_selected_block)

        act_hide_missing = block_menu.addAction("Hide Missing Blocks")
        act_hide_missing.triggered.connect(self._toggle_missing_blocks_visibility)

        # ── Search row ───────────────────────────────────────────────────────
        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(8)

        self.search_box = QLineEdit()
        self.search_box.setObjectName("prjSearchBox")
        self.search_box.setProperty("nakshaStrictTextInput", True)
        self.search_box.setPlaceholderText("🔍  Search block label…")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self._on_search_changed)
        self.search_box.installEventFilter(self)

        self.search_box.setFixedHeight(28)
        self.search_box.setMinimumWidth(260)
        self.search_box.setMaximumWidth(360)
        top_row.addWidget(self.search_box, 0, Qt.AlignLeft | Qt.AlignTop)

        top_row.addStretch()
        layout.addLayout(top_row)

        from PySide6.QtWidgets import QFrame
        info_card = QFrame()
        info_card.setObjectName("prjInfoCard")
        info_layout = QHBoxLayout(info_card)
        info_layout.setContentsMargins(12, 8, 12, 8)
        self.dxf_label = QLabel("📁  No PRJ loaded")
        self.dxf_label.setObjectName("prjInfoLabel")
        info_layout.addWidget(self.dxf_label)
        info_layout.addStretch()
        layout.addWidget(info_card)

        self.table = QTableWidget()
        self.table.setObjectName("prjBlockTable")
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Block Label", "Total Points", "Area (m²)"])

        self._sort_header = _SortableHeader(Qt.Horizontal, self.table)
        self._sort_header.sort_changed.connect(self._on_sort_changed)
        self.table.setHorizontalHeader(self._sort_header)
        self.table.setHorizontalHeaderLabels(["Block Label", "Total Points", "Area (m²)"])

        self._sort_header.setSectionResizeMode(0, QHeaderView.Interactive)
        self._sort_header.setSectionResizeMode(1, QHeaderView.Interactive)
        self._sort_header.setSectionResizeMode(2, QHeaderView.Interactive)
        self.table.setColumnWidth(0, 300)
        self.table.setColumnWidth(1, 160)
        self.table.setColumnWidth(2, 140)
        self._sort_header.setStretchLastSection(True)

        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setMouseTracking(True)
        self.table.setToolTip("")
        self.table.setSortingEnabled(False)
        self.table.itemDoubleClicked.connect(self.identify_selected_block)
        self.table.itemClicked.connect(self.on_table_item_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)
        layout.addWidget(self.table)

        self._all_row_data = []

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        identify_btn = QPushButton("🔍  Identify Selected Block")
        self._style_prj_button(identify_btn, "primaryBtn", 160, 60)
        identify_btn.clicked.connect(self.identify_selected_block)
        btn_layout.addWidget(identify_btn)

        self._hide_missing_btn = QPushButton("👁  Hide Missing Blocks")
        self._style_prj_button(self._hide_missing_btn, "secondaryBtn", 160, 60)
        self._hide_missing_btn.setToolTip(
            "Toggle visibility of SNT block rectangles whose LAZ file was not found"
        )
        self._hide_missing_btn.clicked.connect(self._toggle_missing_blocks_visibility)
        btn_layout.addWidget(self._hide_missing_btn)

        delete_def_btn = QPushButton("🗑  Delete Definition")
        self._style_prj_button(delete_def_btn, "dangerBtn", 160, 60)
        delete_def_btn.setToolTip(
            "Permanently remove the selected block definition(s) from the PRJ file.\n"
            "The LAZ/LAS files on disk are NOT deleted.\n"
            "Changes are buffered in a backend working copy and only written to the\n"
            "real PRJ when you press 'Save project' (which snapshots .prj.bak first)."
        )
        delete_def_btn.clicked.connect(self.delete_block_definition)
        btn_layout.addWidget(delete_def_btn)

        remove_btn = QPushButton("📂  Remove File")
        self._style_prj_button(remove_btn, "dangerBtn", 160, 60)
        remove_btn.clicked.connect(self.remove_prj_file)
        btn_layout.addWidget(remove_btn)

        close_btn = QPushButton("✕  Close")
        self._style_prj_button(close_btn, "closeBtn", 130, 60)
        close_btn.clicked.connect(self.hide)
        btn_layout.addWidget(close_btn)

        layout.addLayout(btn_layout)
    def _deselect_all(self):
        """Clear table selection and fence-selection boundary highlights."""
        self.table.clearSelection()
        self._fence_highlight_clear()

    # ── Lock / Release lock ──────────────────────────────────────────────────
    # Lock state is session-only (not written to the .prj file) — the exact
    # TerraScan format for a lock flag is unverified, and inventing one risks
    # breaking compatibility if the file is later opened in real MicroStation.
    # It still fully blocks Delete / Rename / Edit Definition in this app.

    def _resolve_selected_prj_indices(self):
        """Return the list of valid prj_idx values for the current table selection."""
        indices = []
        for idx in self.table.selectionModel().selectedRows():
            item = self.table.item(idx.row(), 0)
            if item is None:
                continue
            prj_idx = item.data(Qt.UserRole)
            if prj_idx is None:
                continue
            try:
                prj_idx = int(prj_idx)
            except (TypeError, ValueError):
                continue
            if 0 <= prj_idx < len(self.prj_data) and prj_idx not in indices:
                indices.append(prj_idx)
        return indices

    def _lock_selected_blocks(self):
        """Lock the selected block(s) against Delete / Rename / Edit Definition."""
        indices = self._resolve_selected_prj_indices()
        if not indices:
            QMessageBox.warning(self, "No Selection", "Please select one or more blocks to lock.")
            return

        for prj_idx in indices:
            self.prj_data[prj_idx]["locked"] = True

        self._repopulate_table(self._all_row_data)
        self._on_search_changed(self.search_box.text())

        n = len(indices)
        if hasattr(self.app, "statusBar"):
            self.app.statusBar().showMessage(f"Locked {n} block{'s' if n != 1 else ''}", 3000)

    def _release_selected_locks(self):
        """Release the lock on the selected block(s)."""
        indices = self._resolve_selected_prj_indices()
        if not indices:
            QMessageBox.warning(self, "No Selection", "Please select one or more blocks to unlock.")
            return

        n_unlocked = 0
        for prj_idx in indices:
            if self.prj_data[prj_idx].get("locked", False):
                self.prj_data[prj_idx]["locked"] = False
                n_unlocked += 1

        self._repopulate_table(self._all_row_data)
        self._on_search_changed(self.search_box.text())

        if hasattr(self.app, "statusBar"):
            self.app.statusBar().showMessage(
                f"Released lock on {n_unlocked} block{'s' if n_unlocked != 1 else ''}", 3000
            )

    # ── Merge Blocks ─────────────────────────────────────────────────────────

    def _merge_selected_blocks(self):
        """Combine 2+ selected blocks into one via shapely union of their boundaries.

        The first selected block (by table order) keeps its label/filename and
        absorbs the others; absorbed blocks are removed from the .prj file.
        If the union isn't a single contiguous polygon (blocks don't touch or
        overlap), falls back to the convex hull after explicit confirmation.
        """
        if not self.current_prj_path:
            QMessageBox.warning(self, "No PRJ Loaded", "Please load a PRJ file first.")
            return
        if not os.path.isfile(self.current_prj_path):
            QMessageBox.critical(
                self, "PRJ File Not Found",
                f"The PRJ file no longer exists on disk:\n\n{self.current_prj_path}"
            )
            return

        indices = self._resolve_selected_prj_indices()
        if len(indices) < 2:
            QMessageBox.warning(
                self, "Select Two or More Blocks",
                "Please select at least 2 blocks in the table to merge."
            )
            return

        locked = [
            self.prj_data[i].get("label", "?") for i in indices
            if self.prj_data[i].get("locked", False)
        ]
        if locked:
            QMessageBox.warning(
                self, "Block(s) Locked",
                "The following selected block(s) are locked and cannot be merged:\n\n"
                + "\n".join(f"• {lbl}" for lbl in locked)
                + "\n\nUse Block → Release lock first."
            )
            return

        blocks = [self.prj_data[i] for i in indices]
        labels = [b.get("label", "?") for b in blocks]

        # ── Build merged geometry ────────────────────────────────────────────
        try:
            from shapely.geometry import Polygon
            from shapely.ops import unary_union
            polys = []
            for b in blocks:
                coords = b.get("boundary_coords", [])
                if len(coords) >= 3:
                    polys.append(Polygon(coords))
            if len(polys) < 2:
                QMessageBox.warning(
                    self, "Cannot Merge",
                    "At least 2 of the selected blocks must have a valid (3+ point) boundary."
                )
                return
            merged = unary_union(polys)
        except Exception as exc:
            QMessageBox.critical(self, "Merge Failed", f"Could not compute merged geometry:\n\nError: {exc}")
            return

        used_hull_fallback = False
        if merged.geom_type != "Polygon":
            reply = QMessageBox.question(
                self, "Blocks Don't Overlap",
                "The selected blocks don't touch or overlap, so their union isn't\n"
                "a single contiguous shape.\n\n"
                "Use the convex hull (bounding shape around all of them) instead?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            merged = merged.convex_hull
            used_hull_fallback = True

        # Guard: convex_hull of degenerate input (collinear/coincident points) can
        # collapse to a Point or LineString, neither of which has .exterior — would
        # otherwise raise an uncaught AttributeError and crash the dialog.
        if merged.is_empty or merged.geom_type != "Polygon":
            QMessageBox.critical(
                self, "Merge Failed",
                "The merged geometry is degenerate (collapses to a point or line) "
                "and cannot be used as a block boundary."
            )
            return

        new_coords = list(merged.exterior.coords)[:-1]   # drop closing duplicate
        if len(new_coords) < 3:
            QMessageBox.critical(self, "Merge Failed", "The merged geometry is degenerate.")
            return

        old_total_area = sum(self._shoelace_area(b.get("boundary_coords", [])) for b in blocks)
        new_area = merged.area

        primary_idx = indices[0]
        primary_label = blocks[0].get("label", "?")
        absorbed_labels = labels[1:]

        shape_note = " (convex hull)" if used_hull_fallback else ""
        reply = QMessageBox.question(
            self, "Confirm Merge",
            (
                f"Merge {len(indices)} blocks into '{primary_label}'{shape_note}?\n\n"
                "Absorbed (will be deleted):\n"
                + "\n".join(f"  • {lbl}" for lbl in absorbed_labels) + "\n\n"
                f"Old combined area: {old_total_area:.2f} m²\n"
                f"New merged area:   {new_area:.2f} m²\n\n"
                "Changes are buffered in a backend working copy and only written\n"
                "to the real PRJ when you press 'Save project'. This cannot be undone."
            ),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        # ── Read, transform, write (single atomic write) ──────────────────────
        try:
            with open(self.current_prj_path, "r", encoding="utf-8") as fh:
                content = fh.read()
        except Exception as exc:
            QMessageBox.critical(self, "Read Error", f"Could not read the PRJ file:\n\nError: {exc}")
            return

        try:
            content = self._replace_block_boundary_in_prj_text(content, primary_label, new_coords)
            absorbed_upper = {lbl.strip().upper() for lbl in absorbed_labels}
            content = self._remove_blocks_from_prj_text(content, absorbed_upper)
        except Exception as exc:
            QMessageBox.critical(self, "Parse Error", f"Failed to build the merged PRJ content:\n\nError: {exc}")
            return

        # ── Write to backend working copy (real .prj untouched until Save) ────
        try:
            self._write_prj_to_working(content)
            print(f"  ✅ Merged {len(indices)} blocks into '{primary_label}' (backend working copy)")
        except Exception as exc:
            QMessageBox.critical(
                self, "Write Error",
                f"Failed to buffer the merged PRJ changes:\n\nError: {exc}\n\n"
                "The original file has NOT been modified."
            )
            return

        # ── Update in-memory state ─────────────────────────────────────────────
        try:
            cx, cy = merged.centroid.x, merged.centroid.y
            self.prj_data[primary_idx]["boundary_coords"] = new_coords
            self.prj_data[primary_idx]["easting"] = f"{cx:.2f}"
            self.prj_data[primary_idx]["northing"] = f"{cy:.2f}"

            absorbed_upper = {lbl.strip().upper() for lbl in absorbed_labels}
            primary_upper = primary_label.strip().upper()

            new_prj_data = [
                entry for entry in self.prj_data
                if entry.get("label", "").strip().upper() not in absorbed_upper
            ]
            kept_rows = [
                row for row in self._all_row_data
                if row[0].strip().upper() not in absorbed_upper
            ]
            new_idx_map = {
                entry["label"].strip().upper(): i
                for i, entry in enumerate(new_prj_data)
            }
            reindexed = []
            for row in kept_rows:
                norm = row[0].strip().upper()
                new_idx = new_idx_map.get(norm, 0)
                if norm == primary_upper:
                    reindexed.append((row[0], row[1], f"{new_area:.2f}", row[3], row[4], new_idx))
                else:
                    reindexed.append((row[0], row[1], row[2], row[3], row[4], new_idx))

            self.prj_data = new_prj_data
            self._all_row_data = reindexed

            self._sort_header._sort_states = {}
            self._sort_header.viewport().update()
            self._repopulate_table(self._all_row_data)
            self._on_search_changed(self.search_box.text())
            print(f"  ✅ In-memory state updated: {len(self.prj_data)} block(s) remain")
        except Exception as exc:
            print(f"  ⚠️ Failed to refresh table after merge: {exc}")
            QMessageBox.warning(
                self, "Display Refresh Issue",
                "The blocks were merged on disk successfully, but the table\n"
                "could not refresh automatically.\n\n"
                "Please close and reopen the PRJ Block Identifier to see the change."
            )
            return

        if hasattr(self.app, "statusBar"):
            self.app.statusBar().showMessage(
                f"Merged {len(indices)} blocks into '{primary_label}' — area: {new_area:.2f} m²", 6000
            )

    # ── Search ────────────────────────────────────────────────────────────────

    def _on_search_changed(self, text: str):
        query = text.strip().lower()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            label = item.text().lower() if item else ""
            self.table.setRowHidden(row, bool(query and query not in label))

    def _prj_search_focus_set(self, on: bool):
        # Guard for GlobalShortcutFilter — keep alphabet keys for search entry.
        self._prj_search_focus = bool(on)
        from gui.popup_guard import set_input_popup_open
        set_input_popup_open(self.app, bool(on))


    # ── Sort ──────────────────────────────────────────────────────────────────

    # REPLACE _on_sort_changed
    def _on_sort_changed(self, col: int, state: str):
        if not self._all_row_data:
            return

        if state == "none":
            ordered = list(self._all_row_data)
        else:
            reverse = (state == "desc")

            def _sort_key(row_tuple):
                val = row_tuple[col]          # col 0=label, 1=points, 2=area
                if col in (1, 2):
                    try:
                        return float(val.replace(",", ""))
                    except (ValueError, AttributeError):
                        return float("inf") * (-1 if reverse else 1)
                return val.lower()

            ordered = sorted(self._all_row_data, key=_sort_key, reverse=reverse)

        self._repopulate_table(ordered)
        self._on_search_changed(self.search_box.text())

    # REPLACE _repopulate_table
    def _repopulate_table(self, rows):
        """Write rows (sorted/filtered) back into QTableWidget, preserving prj_data links."""
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for r, (c0, c1, c2, col1_fg, col2_fg, prj_idx) in enumerate(rows):
            label_item = QTableWidgetItem(c0)
            label_item.setData(Qt.UserRole, prj_idx)   # ← critical: keep index alive

            # Lock visual indicator — label TEXT is left untouched (other code
            # matches on it) so we only style colour/font/tooltip here.
            is_locked = (
                prj_idx is not None and 0 <= prj_idx < len(self.prj_data)
                and self.prj_data[prj_idx].get("locked", False)
            )
            if is_locked:
                label_item.setForeground(QColor(ThemeColors.get("warning")))
                f = label_item.font()
                f.setItalic(True)
                label_item.setFont(f)
                label_item.setToolTip("🔒 Locked — Delete / Rename / Edit Definition disabled")

            self.table.setItem(r, 0, label_item)

            item1 = QTableWidgetItem(c1)
            item2 = QTableWidgetItem(c2)
            if col1_fg:
                item1.setForeground(col1_fg)
            if col2_fg:
                item2.setForeground(col2_fg)
            self.table.setItem(r, 1, item1)
            self.table.setItem(r, 2, item2)

    def _safe_render_main_view(self):
        """Render main VTK view only when the widget/window is still usable."""
        try:
            vtk_widget = getattr(self.app, 'vtk_widget', None)
            if vtk_widget is None:
                return
            if hasattr(vtk_widget, 'isVisible') and not vtk_widget.isVisible():
                return
            window = vtk_widget.window() if hasattr(vtk_widget, 'window') else None
            if window is not None and hasattr(window, 'isVisible') and not window.isVisible():
                return
            render_window = vtk_widget.GetRenderWindow()
            if render_window is not None:
                render_window.Render()
        except Exception:
            pass

    def detect_current_dxf(self):
        """Detect currently loaded DXF file"""
        try:
            if hasattr(self.app, 'dxf_layers') and self.app.dxf_layers:
                # Get first DXF path from loaded layers
                first_layer = next(iter(self.app.dxf_layers.values()))
                if hasattr(first_layer, 'dxf_path'):
                    self.current_dxf_path = first_layer.dxf_path
                    dxf_name = os.path.basename(self.current_dxf_path)
                    self.dxf_label.setText(f"📁  DXF: {dxf_name}")
                    self.auto_load_prj()
                    return
            
            
        except Exception as e:
            print(f"⚠️ DXF detection failed: {e}")
            
    def auto_load_prj(self):
        """Automatically load PRJ file if it exists next to DXF"""
        if not self.current_dxf_path:
            return
            
        try:
            # Look for .prj file with same name as DXF
            prj_path = os.path.splitext(self.current_dxf_path)[0] + ".prj"
            
            if os.path.exists(prj_path):
                print(f"✅ Auto-loading PRJ: {prj_path}")
                self.parse_prj_file(prj_path)
            else:
                print(f"ℹ️ No PRJ file found at: {prj_path}")
        except Exception as e:
            print(f"⚠️ Auto-load PRJ failed: {e}")
            
    def load_prj_file(self):
        """Open file dialog to select PRJ file"""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select PRJ File",
            "",
            "PRJ Files (*.prj);;All Files (*.*)"
        )
        
        if file_path:
            self.parse_prj_file(file_path)
            
    def parse_prj_file(self, file_path):
        """Parse TerraScan PRJ file and populate table"""
        try:
            # ✅ Store the PRJ file path (prevents deletion)
            self.current_prj_path = file_path
            self.current_directory = os.path.dirname(file_path)
            # Any previous backend working copy is stale once a fresh file loads
            self._discard_working()
            prj_filename = os.path.basename(file_path)
            self.dxf_label.setText(f"📁  PRJ: {prj_filename}")
                        
            print(f"✅ Stored PRJ path: {file_path}")
            # Clean up any active fence / add-by-boundaries / edit-definition state
            self._fence_deactivate(cancel=False)
            self._fence_highlight_clear()
            self._addb_deactivate(cancel=True)
            self._editdef_deactivate(cancel=True)
            self.prj_data = []
            self._all_row_data = []
            self.table.setRowCount(0)
            # Reset sort arrows back to neutral on every fresh load
            self._sort_header._sort_states = {}
            self._sort_header.viewport().update()
            
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            print(f"\n📄 Parsing PRJ file: {file_path}")
            print(f"📄 Total lines in file: {len(lines)}")
            
            # Parse block entries: "Block DX5013255_000001.laz"
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                
                if line.startswith('Block '):
                    # Extract block filename
                    block_file = line.replace('Block ', '').strip()
                    block_label = block_file.replace('.laz', '').replace('.las', '')
                    
                    print(f"\n  🔍 Found block: {block_label} at line {i}")
                    
                    # ✅ Read ALL lines until next Block or EOF — no arbitrary cap.
                    # Previous <=10 limit was cutting off polygons with many vertices.
                    coords = []
                    j = i + 1
                    
                    while j < len(lines):
                        coord_line = lines[j].strip()
                        
                        # Stop at next block header
                        if coord_line.startswith('Block '):
                            print(f"    🛑 Hit next block at line {j}, stopping")
                            break
                        
                        # Skip empty lines
                        if coord_line == '':
                            j += 1
                            continue
                        
                        # ✅ Skip TerraScan metadata lines (GroupFirst=, GroupCount=, etc.)
                        if '=' in coord_line:
                            print(f"    ⏭️ Metadata: '{coord_line}', skipping")
                            j += 1
                            continue
                        
                        # Try to parse as "X Y" coordinate pair
                        parts = coord_line.split()
                        if len(parts) >= 2:
                            try:
                                x = float(parts[0])
                                y = float(parts[1])
                                coords.append((x, y))
                                print(f"    ✅ Coord line {j}: ({x}, {y})")
                            except ValueError:
                                print(f"    ⚠️ Non-numeric line {j}: '{coord_line}', skipping")
                        else:
                            print(f"    ⚠️ Unexpected line {j}: '{coord_line}', skipping")
                        
                        j += 1
                    
                    # ✅ Remove duplicate closing point if polygon is explicitly closed
                    if len(coords) >= 2 and coords[0] == coords[-1]:
                        coords = coords[:-1]
                        print(f"    ♻️ Removed duplicate closing point")
                    
                    print(f"    📊 Total unique boundary coords: {len(coords)}")
                    
                    if len(coords) >= 2:
                        # Use a polygon-safe target point instead of a raw
                        # vertex average, which can drift outside concave shapes.
                        avg_x, avg_y = self._polygon_target_xy(coords)
                        
                        block_data = {
                            'label': block_label,
                            'easting': f"{avg_x:.2f}",
                            'northing': f"{avg_y:.2f}",
                            'description': f"Boundary: {len(coords)} points",
                            'boundary_coords': coords,  # ✅ stored for Shoelace area
                            'target_xy': (avg_x, avg_y),
                        }
                        self.prj_data.append(block_data)
                        print(f"    ✅ Added: {block_label} at ({avg_x:.2f}, {avg_y:.2f})")
                    else:
                        print(f"    ⚠️ Not enough coordinates for {block_label}, skipping")
                    
                    # Resume outer loop from where inner loop ended
                    i = j
                else:
                    i += 1
            
            # ── PRJ-authoritative LAZ/LAS discovery ───────────────────────────
            # PRJ Block Identifier is intentionally independent from SNT attachment
            # paths.  The currently loaded PRJ file owns the point-cloud lookup.
            #
            # Priority inside the PRJ tree:
            #   1) file beside the PRJ (direct child)
            #   2) same-stem file in a PRJ subfolder
            #
            # Loaded SNT folders are NEVER consulted here.  SNT remains useful for
            # viewport geometry/highlighting, but cannot change which point-cloud
            # files the PRJ table reports as available.
            prj_lidar_index = {}
            prj_root = None
            if self.current_directory:
                try:
                    prj_root = Path(self.current_directory).resolve()
                    print(f"  📁 PRJ-authoritative point-cloud root: {prj_root}")

                    # Direct children first so they always win over a duplicate
                    # filename found deeper in the PRJ directory tree.
                    try:
                        for fp in prj_root.iterdir():
                            if fp.is_file() and fp.suffix.lower() in ('.laz', '.las'):
                                prj_lidar_index.setdefault(_lidar_block_key(fp), str(fp))
                    except OSError as exc:
                        print(f"  ⚠️ Could not scan PRJ directory directly: {exc}")

                    # Then allow project-local subfolders without ever escaping to
                    # an SNT directory or a previously loaded project directory.
                    try:
                        for fp in prj_root.rglob('*'):
                            if not fp.is_file() or fp.suffix.lower() not in ('.laz', '.las'):
                                continue
                            prj_lidar_index.setdefault(_lidar_block_key(fp), str(fp))
                    except OSError as exc:
                        print(f"  ⚠️ Could not recursively scan PRJ directory: {exc}")

                    print(
                        f"  ✅ PRJ-local point-cloud index: "
                        f"{len(prj_lidar_index)} unique LAZ/LAS stem(s)"
                    )
                except Exception as exc:
                    print(f"  ⚠️ PRJ point-cloud discovery failed: {exc}")
            else:
                print("  ⚠️ PRJ directory unavailable; point-cloud lookup disabled")

            # Populate table
            # Populate table + master sort list
            print(f"\n📊 Total blocks found: {len(self.prj_data)}")
            self.table.setRowCount(len(self.prj_data))
            self._all_row_data = []   # ← cleared fresh every load
            files_found = 0
            files_missing = 0

            for row, data in enumerate(self.prj_data):
                block_label = data['label']
                # Resolve strictly from the currently loaded PRJ's own
                # directory tree.  This deliberately ignores loaded SNT folders.
                block_key = _lidar_block_key(block_label)
                found_laz_path = prj_lidar_index.get(block_key)

                # Column 0: Block Label  — store original prj_data index in UserRole
                label_item = QTableWidgetItem(block_label)
                label_item.setData(Qt.UserRole, row)   # ← keeps sort↔prj_data in sync
                self.table.setItem(row, 0, label_item)

                if found_laz_path:
                    # Keep the exact resolved source beside the PRJ geometry.
                    # The table previously retained only the point-count text,
                    # so Identify could find a LAS but had no path to load it.
                    data['las_path'] = os.path.normpath(found_laz_path)
                    point_count = self.get_laz_point_count(found_laz_path)
                    area = self.calculate_grid_area_from_prj(data)

                    pts_str  = f"{point_count:,}" if point_count > 0 else "N/A"
                    area_str = f"{area:.2f}"      if area > 0        else "N/A"

                    self.table.setItem(row, 1, QTableWidgetItem(pts_str))
                    self.table.setItem(row, 2, QTableWidgetItem(area_str))

                    # master list tuple: (label, points, area, col1_fg, col2_fg, prj_idx)
                    self._all_row_data.append((block_label, pts_str, area_str, None, None, row))
                    files_found += 1
                else:
                    red = QColor(ThemeColors.get("danger"))
                    item1 = QTableWidgetItem("No file in path")
                    item2 = QTableWidgetItem("No file in path")
                    item1.setForeground(red)
                    item2.setForeground(red)
                    self.table.setItem(row, 1, item1)
                    self.table.setItem(row, 2, item2)

                    self._all_row_data.append((block_label, "No file in path", "No file in path", red, red, row))
                    files_missing += 1

            print(f"✅ Files found: {files_found}, ❌ Files missing: {files_missing}")

            # Reset sort state so header arrows go back to neutral on new load
            self._sort_header._sort_states = {}
            self._sort_header.viewport().update()

            if len(self.prj_data) > 0:
                if hasattr(self.app, "statusBar"):
                    self.app.statusBar().showMessage(
                        f"Loaded {len(self.prj_data)} blocks from {os.path.basename(file_path)}", 5000
                    )
            else:
                QMessageBox.warning(
                    self,
                    "⚠️ No Blocks Found",
                    f"The PRJ file was read but no block entries were found.\n\nFile: {os.path.basename(file_path)}"
                )
            
        except Exception as e:
            QMessageBox.critical(
                self,
                "❌ Load Failed",
                f"Failed to load PRJ file:\n{str(e)}"
            )
            import traceback
            traceback.print_exc()
            
            
    def clear_highlight_for_block(self, block_label):
        """Clear highlight for a specific block when clicked."""
        if not hasattr(self, '_highlighted_blocks') or block_label not in self._highlighted_blocks:
            return
        
        try:
            import vtk
            
            block_info = self._highlighted_blocks[block_label]
            actor = block_info['actor']
            highlight_actor = block_info['highlight_actor']
            
            if hasattr(self.app, 'vtk_widget') and self.app.vtk_widget:
                renderer = self.app.vtk_widget.renderer
                
                # Find and restore original text properties
                if hasattr(self, '_highlighted_labels'):
                    for label_actor, orig_color, orig_scale in self._highlighted_labels:
                        if label_actor == actor:
                            actor.GetProperty().SetColor(orig_color)
                            actor.SetScale(orig_scale)
                            self._highlighted_labels.remove((label_actor, orig_color, orig_scale))
                            break
                
                # Remove highlight circle
                if highlight_actor and highlight_actor in self._highlight_actors:
                    renderer.RemoveActor(highlight_actor)
                    self._highlight_actors.remove(highlight_actor)
                
                # Remove from tracking
                del self._highlighted_blocks[block_label]
                
                # Render
                self.app.vtk_widget.GetRenderWindow().Render()
                
                print(f"✅ Cleared highlight for: {block_label}")
        
        except Exception as e:
            print(f"⚠️ Failed to clear highlight for {block_label}: {e}")


    def _auto_clear_highlights(self):
        """Auto-clear all highlight circles and restore text colors after timeout."""
        if not hasattr(self, '_highlight_actors') or not self._highlight_actors:
            return

        try:
            if hasattr(self.app, 'vtk_widget') and self.app.vtk_widget:
                renderer = self.app.vtk_widget.renderer

                # Remove highlight circles
                for highlight_actor in list(self._highlight_actors):
                    try:
                        renderer.RemoveActor(highlight_actor)
                    except Exception:
                        pass

                # Restore text label colors
                for label_actor, orig_color, orig_scale in list(self._highlighted_labels):
                    try:
                        label_actor.GetProperty().SetColor(orig_color)
                        label_actor.SetScale(orig_scale)
                    except Exception:
                        pass

                self.app.vtk_widget.GetRenderWindow().Render()

            self._highlight_actors = []
            self._highlighted_labels = []
            self._highlighted_blocks = {}
            self._identified_prj_block_labels = set()

            print("🕐 Auto-cleared all block highlights")
        except Exception as e:
            print(f"⚠️ Auto-clear highlights failed: {e}")
            
            
    def on_table_item_clicked(self, item):
        """Handle single-click on table item - clear highlight if exists."""
        if item is None:
            return
        row = item.row()

        # Clear fence-selection boundary highlights when user manually interacts
        self._fence_highlight_clear()

        if not self.prj_data:
            return

        # ← CHANGED: read original prj_data index from UserRole (survives sorting)
        label_item = self.table.item(row, 0)
        prj_idx = label_item.data(Qt.UserRole) if label_item is not None else row
        if prj_idx is None or prj_idx < 0 or prj_idx >= len(self.prj_data):
            return

        block_label = self.prj_data[prj_idx].get('label', '')
        if not block_label:
            return

        # Check if this block is currently highlighted
        if hasattr(self, '_highlighted_blocks') and block_label in self._highlighted_blocks:
            print(f"🖱️ Clicked highlighted block: {block_label} - clearing highlight")
            self.clear_highlight_for_block(block_label)
                    
    def identify_selected_block(self):
        """Identify selected blocks from their PRJ boundary geometry."""
        selected_rows = self.table.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.warning(self, "No Selection", "Please select a block to identify")
            return

        # Get unique visual row indices
        rows = list(set([index.row() for index in selected_rows]))

        print(f"\n🔍 Searching for {len(rows)} selected blocks...")

        # Clear previous highlight actors and restore text colors before starting fresh
        if hasattr(self.app, 'vtk_widget') and self.app.vtk_widget:
            renderer = self.app.vtk_widget.renderer
            for old_actor in getattr(self, '_highlight_actors', []):
                try:
                    renderer.RemoveActor(old_actor)
                except Exception:
                    pass
        for label_actor, orig_color, orig_scale in getattr(self, '_highlighted_labels', []):
            try:
                label_actor.GetProperty().SetColor(orig_color)
                label_actor.SetScale(orig_scale)
            except Exception:
                pass

        self._highlight_actors = []
        self._highlighted_labels = []
        self._highlighted_blocks = {}
        self._identified_prj_block_labels = set()

        matches_found = []
        selected_grid_blocks = []
        prj_geometry_blocks = []

        for row in rows:
            # ← CHANGED: read original prj_data index from UserRole (survives sorting)
            label_item = self.table.item(row, 0)
            prj_idx = label_item.data(Qt.UserRole) if label_item is not None else row
            if prj_idx is None or prj_idx >= len(self.prj_data):
                continue

            block_data = self.prj_data[prj_idx]   # ← was: self.prj_data[row]
            block_label = block_data['label']
            print(f"\n🔍 Searching for block: '{block_label}'")

            # The loaded SNT polygon is authoritative for viewport location.
            # It exists independently of whether a matching LAZ/LAS file is
            # present or whether its text label received a rendered actor.
            snt_polygon = self._find_loaded_snt_block_polygon(
                block_label,
                block_data.get('boundary_coords') or [],
            )
            identify_data = dict(block_data)
            if snt_polygon is not None:
                identify_data['boundary_coords'] = snt_polygon
                identify_data['identify_source'] = 'SNT'
                print(
                    f"  ✅ MATCH (SNT polygon): '{block_label}' "
                    f"({len(snt_polygon)} vertices)"
                )
            else:
                prj_geometry_blocks.append(block_label)
                identify_data['identify_source'] = 'PRJ'
                print(
                    f"  ✅ MATCH (PRJ geometry): '{block_label}'; "
                    "navigating independently of LAZ/LAS availability"
                )
            selected_grid_blocks.append(identify_data)

            try:
                block_found = False

                if not block_found and hasattr(self.app, 'dxf_actors'):
                    for dxf_data in self.app.dxf_actors:
                        for actor in dxf_data.get('actors', []):
                            if (
                                getattr(actor, 'is_grid_label', False)
                                or getattr(actor, 'is_block_polygon', False)
                            ):
                                grid_name = getattr(actor, 'grid_name', '')
                                grid_key = "".join(ch.lower() for ch in str(grid_name) if ch.isalnum())
                                block_key = "".join(ch.lower() for ch in str(block_label) if ch.isalnum())
                                if grid_key == block_key:
                                    print(f"  ✅ MATCH (DXF): '{grid_name}' ~ '{block_label}'")
                                    matches_found.append((actor, block_data))
                                    block_found = True
                                    break
                        if block_found:
                            break

                # Also search SNT actors (block labels from SNT attachments)
                if not block_found and hasattr(self.app, 'snt_actors'):
                    for snt_data in self.app.snt_actors:
                        for actor in snt_data.get('actors', []):
                            if (
                                getattr(actor, 'is_grid_label', False)
                                or getattr(actor, 'is_block_polygon', False)
                            ):
                                grid_name = getattr(actor, 'grid_name', '')
                                grid_key = "".join(ch.lower() for ch in str(grid_name) if ch.isalnum())
                                block_key = "".join(ch.lower() for ch in str(block_label) if ch.isalnum())
                                if grid_key == block_key:
                                    print(f"  ✅ MATCH (SNT): '{grid_name}' ~ '{block_label}'")
                                    matches_found.append((actor, block_data))
                                    block_found = True
                                    break
                        if block_found:
                            break

                # ── Broader fallback: scan ALL rendered actors for text match ──
                # Some SNT/DGN actors have the grid name as their rendered
                # text but lack the is_grid_label / is_block_polygon flag.
                if not block_found:
                    block_key = "".join(
                        ch.lower() for ch in str(block_label) if ch.isalnum()
                    )
                    for actor_list_attr in ('snt_actors', 'dxf_actors'):
                        if block_found:
                            break
                        for data_set in getattr(self.app, actor_list_attr, []):
                            if block_found:
                                break
                            for actor in data_set.get('actors', []):
                                actor_text = getattr(actor, 'grid_name', '') or ''
                                if not actor_text:
                                    # Try to read from vtkTextActor3D input
                                    try:
                                        actor_text = actor.GetInput() or ''
                                    except (AttributeError, TypeError):
                                        pass
                                if not actor_text:
                                    continue
                                actor_key = "".join(
                                    ch.lower()
                                    for ch in str(actor_text)
                                    if ch.isalnum()
                                )
                                if actor_key == block_key:
                                    print(
                                        f"  ✅ MATCH (text fallback): "
                                        f"'{actor_text}' ~ '{block_label}'"
                                    )
                                    matches_found.append((actor, block_data))
                                    block_found = True
                                    break

                if not block_found:
                    if snt_polygon is not None:
                        print(
                            f"  ℹ️ No rendered label actor for '{block_label}'; "
                            "using its loaded SNT polygon"
                        )
                    else:
                        print(
                            f"  ℹ️ No rendered SNT label actor for "
                            f"'{block_label}'; using its exact PRJ geometry"
                        )

            except Exception as e:
                print(f"  ❌ Error searching for '{block_label}': {e}")

        # Persist the set of currently-identified block labels so the
        # viewport right-click handler (grid_label_system._get_prj_block_polygons)
        # can restrict its hit-test to ONLY these blocks, not every block in
        # the .prj file. Without this, right-clicking anywhere inside any
        # (un-highlighted, possibly invisible) neighboring block's boundary
        # would also show the load-options context menu.
        self._identified_prj_block_labels = {
            str(block.get('label') or '').strip()
            for block in selected_grid_blocks
            if block.get('label')
        }

        # ── Build a lookup of matched-actor rendered positions ─────────
        # The DXF/SNT actor position reflects where the grid label is
        # ACTUALLY rendered in the viewport.  PRJ boundary_coords are in
        # the PRJ's real-world projection which can differ from the DGN
        # coordinate space used by the renderer (e.g. MicroStation UORs).
        # When an actor match exists we must navigate to its viewport
        # position — not to the raw PRJ coordinate — and translate the
        # PRJ boundary polygon so it frames the label the user can see.
        _actor_xy_by_label = {}   # block_label → (actor_cx, actor_cy)

        # Highlight all matched blocks simultaneously
        if matches_found:
            print(f"\n🎨 Highlighting {len(matches_found)} blocks simultaneously...")
            try:
                import vtk

                if hasattr(self.app, 'vtk_widget') and self.app.vtk_widget:
                    renderer = self.app.vtk_widget.renderer
                    highlighted_labels = []

                    for actor, block_data in matches_found:
                        # Use the ACTUAL actor position from SNT/DXF
                        # NOT the PRJ boundary centroid — they differ!
                        try:
                            b = actor.GetBounds()
                            x = (b[0] + b[1]) / 2.0
                            y = (b[2] + b[3]) / 2.0
                        except Exception:
                            x = float(block_data['easting'])
                            y = float(block_data['northing'])

                        _actor_xy_by_label[block_data['label']] = (x, y)

                        # ✅ Removed: red identify circle. Its center came from
                        # the label actor's bounding-box midpoint, which for
                        # elongated/diagonal blocks can sit well outside the
                        # actual polygon. The block boundary outline (drawn
                        # separately below) already highlights the match, so
                        # the circle was redundant and sometimes misleading.
                        highlight_actor = None

                        if hasattr(actor, 'GetProperty'):
                            original_color = actor.GetProperty().GetColor()
                            original_scale = actor.GetScale()
                            actor.GetProperty().SetColor(1.0, 1.0, 0.0)
                            highlighted_labels.append((actor, original_color, original_scale))

                        print(f"   ✅ Highlighted: {block_data['label']} at ({x:.2f}, {y:.2f})")

                    self.app.vtk_widget.GetRenderWindow().Render()

                self._highlighted_labels.extend(highlighted_labels)

                for i, (actor, block_data) in enumerate(matches_found):
                    block_label = block_data['label']
                    self._highlighted_blocks[block_label] = {
                        'actor': actor,
                        'block_data': block_data,
                        'highlight_actor': None,  # circle marker removed
                    }

                # Cancel any pending auto-clear from previous identify
                self._highlight_identify_id += 1
                current_id = self._highlight_identify_id

                # Auto-clear highlights after 20 seconds (only if no new identify happened)
                def _guarded_auto_clear():
                    if self._highlight_identify_id == current_id:
                        self._auto_clear_highlights()

                self._highlight_auto_clear_timer = QTimer.singleShot(20000, _guarded_auto_clear)

                if hasattr(self.app, "statusBar"):
                    block_names = [bd['label'] for _, bd in matches_found]
                    self.app.statusBar().showMessage(
                        f"Highlighted {len(matches_found)} block(s) — auto-clears in 20 s", 20000
                    )

            except Exception as e:
                print(f"❌ Highlighting failed: {e}")
                import traceback
                traceback.print_exc()

        # ── Coordinate authority for Identify navigation ───────────────
        # SNT-sourced polygons are already in viewport/world coordinates.
        # PRJ-sourced polygons are authoritative project coordinates and must
        # never be translated from rendered text/label actor positions.
        #
        # The former PRJ→viewport offset workaround was required only while
        # legacy DGN/SNT conversions could land in a different coordinate
        # system. Applying that correction after the converter fix introduces
        # false offsets from text bounds/anchor placement (for example ~204 m).
        # Keep actor matching exclusively for visual highlighting; navigation
        # and boundary framing use the selected SNT/PRJ geometry unchanged.
        for block in selected_grid_blocks:
            source = str(block.get('identify_source') or 'PRJ').upper()
            label = block.get('label', '')
            coords = block.get('boundary_coords') or []
            if len(coords) < 3:
                continue

            if source == 'PRJ':
                cx, cy = self._polygon_target_xy(coords)
                print(
                    f"   ✅ PRJ navigation authority for '{label}': "
                    f"target=({cx:.2f}, {cy:.2f}); actor offsets ignored"
                )
            elif source == 'SNT':
                cx, cy = self._polygon_target_xy(coords)
                print(
                    f"   ✅ SNT navigation geometry for '{label}': "
                    f"target=({cx:.2f}, {cy:.2f})"
                )

        # Frame SNT geometry when present; otherwise frame the exact PRJ grid
        # even when it lies outside the loaded SNT coverage.
        grid_bounds = self._highlight_prj_boundaries(selected_grid_blocks)
        if grid_bounds:
            min_x, min_y, max_x, max_y = grid_bounds
            center_x = 0.5 * (min_x + max_x)
            center_y = 0.5 * (min_y + max_y)
            span = max(max_x - min_x, max_y - min_y)
            target_scale = max(50.0, span * 0.58 if span > 0 else 250.0)
            self.fly_to_location(center_x, center_y, target_scale=target_scale)

            self._highlight_identify_id += 1
            current_id = self._highlight_identify_id

            def _guarded_prj_auto_clear():
                if self._highlight_identify_id == current_id:
                    self._auto_clear_highlights()

            self._highlight_auto_clear_timer = QTimer.singleShot(
                20000, _guarded_prj_auto_clear
            )
            if hasattr(self.app, "statusBar"):
                snt_count = sum(
                    block.get('identify_source') == 'SNT'
                    for block in selected_grid_blocks
                )
                self.app.statusBar().showMessage(
                    f"Identified {len(selected_grid_blocks)} block(s): "
                    f"{snt_count} from loaded SNT geometry, "
                    f"{len(selected_grid_blocks) - snt_count} from PRJ fallback"
                    + (
                        f"; {len(prj_geometry_blocks)} from PRJ geometry"
                        if prj_geometry_blocks else ""
                    ),
                    5000,
                )
        else:
            QMessageBox.warning(
                self,
                "Invalid PRJ Geometry",
                "The selected block has no valid boundary coordinates in the PRJ file."
            )

    def _find_loaded_snt_block_polygon(self, block_label, prj_boundary=None):
        """Return a loaded-SNT polygon by label, then by PRJ spatial overlap."""
        wanted = _lidar_block_key(block_label)
        if not wanted:
            return None

        entries = getattr(self.app, 'snt_block_polygons', []) or []
        for entry in entries:
            names = [entry.get('grid_name'), entry.get('block_file')]
            names.extend(entry.get('alt_names') or [])
            if not any(_lidar_block_key(name) == wanted for name in names):
                continue

            points = entry.get('points_2d') or []
            try:
                polygon = [(float(point[0]), float(point[1])) for point in points]
            except (TypeError, ValueError, IndexError):
                continue
            if len(polygon) >= 3:
                if polygon[0] == polygon[-1]:
                    polygon = polygon[:-1]
                return polygon

        # Some SNT formats contain valid block boundaries without a usable
        # label actor/name association. Match those boundaries spatially to
        # the selected PRJ grid, independent of LAZ/LAS file availability.
        try:
            prj_polygon = [
                (float(point[0]), float(point[1]))
                for point in (prj_boundary or [])
            ]
        except (TypeError, ValueError, IndexError):
            prj_polygon = []
        if len(prj_polygon) < 3:
            return None

        prj_x = [point[0] for point in prj_polygon]
        prj_y = [point[1] for point in prj_polygon]
        pmin_x, pmax_x = min(prj_x), max(prj_x)
        pmin_y, pmax_y = min(prj_y), max(prj_y)
        prj_bbox_area = max(1e-9, (pmax_x - pmin_x) * (pmax_y - pmin_y))
        best_polygon = None
        best_score = 0.0

        for entry in entries:
            points = entry.get('points_2d') or []
            try:
                polygon = [(float(point[0]), float(point[1])) for point in points]
            except (TypeError, ValueError, IndexError):
                continue
            if len(polygon) < 3:
                continue
            if polygon[0] == polygon[-1]:
                polygon = polygon[:-1]

            xs = [point[0] for point in polygon]
            ys = [point[1] for point in polygon]
            overlap_w = max(0.0, min(pmax_x, max(xs)) - max(pmin_x, min(xs)))
            overlap_h = max(0.0, min(pmax_y, max(ys)) - max(pmin_y, min(ys)))
            bbox_overlap = overlap_w * overlap_h
            if bbox_overlap <= 0.0:
                continue

            center_x, center_y = self._polygon_target_xy(polygon)
            center_inside = _point_in_polygon_xy(
                center_x, center_y, prj_polygon
            )
            score = bbox_overlap / prj_bbox_area
            if center_inside:
                score += 1.0
            if score > best_score:
                best_score = score
                best_polygon = polygon

        if best_polygon is not None and best_score >= 0.25:
            print(
                f"  ✅ MATCH (SNT spatial geometry): '{block_label}' "
                f"(score={best_score:.3f})"
            )
            return best_polygon
        return None
        return None

    def _highlight_prj_boundaries(self, blocks):
        """Draw selected PRJ polygons and return their combined XY bounds."""
        vtk_widget = getattr(self.app, "vtk_widget", None)
        renderer = getattr(vtk_widget, "renderer", None)
        if renderer is None:
            return None

        try:
            import vtk

            all_x = []
            all_y = []
            for block in blocks:
                coords = block.get("boundary_coords") or []
                if len(coords) < 3:
                    continue

                ring = [(float(x), float(y)) for x, y in coords]
                if ring[0] != ring[-1]:
                    ring.append(ring[0])

                points = vtk.vtkPoints()
                for x, y in ring:
                    points.InsertNextPoint(x, y, 2.0)
                    all_x.append(x)
                    all_y.append(y)

                polyline = vtk.vtkPolyLine()
                polyline.GetPointIds().SetNumberOfIds(len(ring))
                for index in range(len(ring)):
                    polyline.GetPointIds().SetId(index, index)

                cells = vtk.vtkCellArray()
                cells.InsertNextCell(polyline)
                polydata = vtk.vtkPolyData()
                polydata.SetPoints(points)
                polydata.SetLines(cells)

                mapper = vtk.vtkPolyDataMapper()
                mapper.SetInputData(polydata)
                boundary_actor = vtk.vtkActor()
                boundary_actor.SetMapper(mapper)
                boundary_actor.GetProperty().SetColor(1.0, 0.15, 0.0)
                boundary_actor.GetProperty().SetLineWidth(6.0)
                boundary_actor.GetProperty().SetOpacity(1.0)
                boundary_actor.PickableOff()
                renderer.AddActor(boundary_actor)
                self._highlight_actors.append(boundary_actor)

                print(
                    f"   ✅ {block.get('identify_source', 'PRJ')} boundary: "
                    f"{block.get('label', '')} ({len(coords)} vertices)"
                )

            if not all_x:
                return None
            vtk_widget.GetRenderWindow().Render()
            return min(all_x), min(all_y), max(all_x), max(all_y)
        except Exception as exc:
            print(f"❌ PRJ boundary highlight failed: {exc}")
            return None
    
    
    def fly_to_location(self, x, y, duration=2000, target_scale=None):
        """
        Smoothly pan the 2D orthographic camera to the target XY location.

        ROOT-CAUSE FIX (grid disappears after identify):
        ─────────────────────────────────────────────────
        The old implementation set camera Z-position to 200 and called
        camera.Zoom(2.0).  In a parallel-projection (2D) view this does two
        harmful things:

          1. SetPosition([x, y, 200]) moves the camera ABOVE the data plane
             but ONLY translates the focal-point to Z=0.  This implicitly
             switches the view direction, which — combined with
             ResetCameraClippingRange() — produces a clipping frustum that
             excludes DXF grid actors sitting at Z ≈ 0.  The actors are still
             in the renderer, but they fall outside the near/far clip planes,
             so VTK does not draw them.  Pressing Shift+F calls fit_view()
             which resets the camera properly and brings them back.

          2. camera.Zoom(2.0) is a MULTIPLICATIVE scale operation — calling
             it once per "identify" compounded the parallel-scale, causing
             progressive zoom drift on repeated identifies.

        CORRECT 2D APPROACH:
          • Keep ParallelProjection ON throughout.
          • Animate ONLY the focal-point (XY pan) and the camera position
            offset by the same delta — the camera always stays directly
            above the focal point at its current Z height (view-up = Y).
          • Animate the parallel-scale from its current value to a
            target_scale that gives a comfortable (~500 m) view window.
          • After animation: call _ensure_overlay_actors() so that any
            DXF/SNT actors that were removed by an intermediate render
            call are guaranteed to be back in the renderer.
        """
        try:
            from PySide6.QtCore import QTimer

            if not hasattr(self.app, 'vtk_widget') or not self.app.vtk_widget:
                return

            renderer   = self.app.vtk_widget.renderer
            camera     = renderer.GetActiveCamera()
            render_win = self.app.vtk_widget.GetRenderWindow()

            # Own the camera for the complete transaction.  A large grid load
            # can queue wheel/touchpad momentum events; MainWheelZoomEventFilter
            # consumes them while this flag is set instead of letting them
            # overwrite the fly-to scale between animation frames.
            fly_generation = int(getattr(self.app, "_prj_fly_generation", 0)) + 1
            self.app._prj_fly_generation = fly_generation
            self.app._prj_fly_camera_active = True
            cancel_zoom = getattr(self.app, "_cancel_smooth_zoom_for_pan", None)
            if callable(cancel_zoom):
                cancel_zoom()

            # ── Snapshot current camera state ─────────────────────────
            start_focal = list(camera.GetFocalPoint())   # (fx, fy, fz)
            start_pos   = list(camera.GetPosition())     # (px, py, pz)
            start_scale = camera.GetParallelScale()      # half-height in world units

            # A PRJ identify is a strict 2D operation.  Do not carry an XY
            # camera offset (tilt) or a stale focal Z forward from a previous
            # SNT/DXF view: either one makes correctly georeferenced points at
            # a different elevation project away from the requested PRJ XY.
            # Preserve only a safe positive camera-to-focal distance.
            camera_distance = abs(float(start_pos[2]) - float(start_focal[2]))
            if camera_distance < 1.0:
                camera_distance = 1000.0

            target_z = float(start_focal[2])
            try:
                xyz = (getattr(self.app, "data", None) or {}).get("xyz")
                if xyz is not None and len(xyz):
                    # Mid-range is stable and avoids an O(N) mean during UI work.
                    target_z = 0.5 * (
                        float(xyz[:, 2].min()) + float(xyz[:, 2].max())
                    )
            except Exception:
                pass

            # ── Target values ──────────────────────────────────────────
            target_focal = [x, y, target_z]
            target_pos   = [x, y, target_z + camera_distance]

            # Target parallel-scale: ~250 m half-height gives a comfortable
            # 500 m wide view.  Clamp so we never zoom in tighter than 50 m
            # or wider than 2× the current scale.
            if target_scale is None:
                TARGET_HALF_HEIGHT_M = 250.0
                target_scale = max(
                    50.0,
                    min(TARGET_HALF_HEIGHT_M, start_scale * 2.0)
                )
            else:
                target_scale = max(50.0, float(target_scale))

            # ── Ensure parallel projection is active ───────────────────
            camera.ParallelProjectionOn()
            camera.SetViewUp(0.0, 1.0, 0.0)

            # ── Animation ─────────────────────────────────────────────
            try:
                loaded_count = len((getattr(self.app, "data", None) or {}).get("xyz", ()))
            except Exception:
                loaded_count = 0
            # Forty full renders of a 13M-point actor make a nominal two-second
            # animation take far longer and enlarge the input-race window.
            # Large production clouds land on the target on the next event-loop
            # turn; lightweight scenes retain the existing smooth animation.
            STEPS = 1 if loaded_count >= 1_000_000 else 40
            step_duration = 1 if STEPS == 1 else max(1, duration // STEPS)
            current_step  = [0]

            def _eased(t: float) -> float:
                """Smooth-step (ease-in-out cubic)."""
                return t * t * (3.0 - 2.0 * t)

            def animate_step():
                if int(getattr(self.app, "_prj_fly_generation", 0)) != fly_generation:
                    return
                step = current_step[0]

                if step >= STEPS:
                    # ── Final frame: land exactly on target ───────────
                    camera.SetFocalPoint(target_focal)
                    camera.SetPosition(target_pos)
                    camera.SetParallelScale(target_scale)
                    camera.SetViewUp(0.0, 1.0, 0.0)
                    renderer.ResetCameraClippingRange()
                    render_win.Render()

                    # ── CRITICAL: restore any DXF/SNT actors that an
                    #    intermediate render might have dropped ─────────
                    try:
                        if hasattr(self.app, '_ensure_overlay_actors'):
                            self.app._ensure_overlay_actors()
                    except Exception:
                        pass

                    print(
                        f"✈️  Fly-to complete → ({x:.2f}, {y:.2f}) "
                        f"focal={tuple(round(v, 3) for v in camera.GetFocalPoint())} "
                        f"scale={camera.GetParallelScale():.3f}"
                    )

                    # Keep the guard for one short event-loop drain so queued
                    # momentum generated during the blocking LAS load cannot
                    # execute immediately after the final frame.
                    def _release_fly_camera():
                        if int(getattr(self.app, "_prj_fly_generation", 0)) == fly_generation:
                            self.app._prj_fly_camera_active = False
                            commit = getattr(self.app, "_commit_main_view_history", None)
                            if callable(commit):
                                commit("prj_fly_to")

                    QTimer.singleShot(250, _release_fly_camera)
                    return

                t = _eased(step / STEPS)

                # Interpolate focal point (XY pan only)
                new_focal = [
                    start_focal[0] + (target_focal[0] - start_focal[0]) * t,
                    start_focal[1] + (target_focal[1] - start_focal[1]) * t,
                    start_focal[2] + (target_focal[2] - start_focal[2]) * t,
                ]
                # Converge to a true top-down camera as well as the target XY.
                new_pos = [
                    start_pos[0] + (target_pos[0] - start_pos[0]) * t,
                    start_pos[1] + (target_pos[1] - start_pos[1]) * t,
                    start_pos[2] + (target_pos[2] - start_pos[2]) * t,
                ]
                # Animate parallel scale
                new_scale = start_scale + (target_scale - start_scale) * t

                camera.SetFocalPoint(new_focal)
                camera.SetPosition(new_pos)
                camera.SetParallelScale(new_scale)
                renderer.ResetCameraClippingRange()
                render_win.Render()

                current_step[0] += 1
                QTimer.singleShot(step_duration, animate_step)

            # Kick off animation
            animate_step()
            print(f"✈️  Flying to ({x:.2f}, {y:.2f})  target_scale={target_scale:.1f}")

        except Exception as e:
            try:
                self.app._prj_fly_camera_active = False
            except Exception:
                pass
            print(f"⚠️ Fly-to animation failed: {e}")
            import traceback
            traceback.print_exc()        
            
    def zoom_to_block(self, block, block_data):
        """Zoom and highlight a DXF block"""
        try:
            # Get block position (from PRJ coordinates or block data)
            x = float(block_data['easting'])
            y = float(block_data['northing'])
            
            # Zoom to block location
            if hasattr(self.app, 'viewer') and self.app.viewer:
                # Create temporary highlight
                self.highlight_location(x, y)
                
                # Zoom to location with some padding
                padding = 50  # meters
                self.app.viewer.zoom_to_bounds(
                    x - padding, y - padding,
                    x + padding, y + padding
                )
                
        except Exception as e:
            print(f"⚠️ Zoom failed: {e}")
            
            
    def zoom_to_entity(self, actor, block_data):
        """
        Pan the 2D camera to the entity location and create a visible highlight
        marker.  Preserves parallel projection so DXF grid actors stay visible.
        """
        try:
            import vtk

            x = float(block_data['easting'])
            y = float(block_data['northing'])

            print(f"🎯 Zooming to block at ({x}, {y})")

            if not (hasattr(self.app, 'vtk_widget') and self.app.vtk_widget):
                print("⚠️ VTK widget not available")
                return

            renderer   = self.app.vtk_widget.renderer
            render_win = self.app.vtk_widget.GetRenderWindow()
            camera     = renderer.GetActiveCamera()

            # ── Remove old single-entity highlight ────────────────────
            if hasattr(self, '_highlight_actor') and self._highlight_actor:
                try:
                    renderer.RemoveActor(self._highlight_actor)
                except Exception:
                    pass
                self._highlight_actor = None

            # ── Create RED CIRCLE highlight marker ────────────────────
            circle = vtk.vtkRegularPolygonSource()
            circle.SetNumberOfSides(50)
            circle.SetRadius(30)
            circle.SetCenter(x, y, 1)        # slightly above data plane
            circle.GeneratePolygonOff()

            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputConnection(circle.GetOutputPort())

            highlight_actor = vtk.vtkActor()
            highlight_actor.SetMapper(mapper)
            highlight_actor.GetProperty().SetColor(1.0, 0.0, 0.0)
            highlight_actor.GetProperty().SetLineWidth(5)
            highlight_actor.GetProperty().SetOpacity(1.0)

            renderer.AddActor(highlight_actor)
            self._highlight_actor = highlight_actor

            # ── Move camera in 2D — DO NOT break parallel projection ──
            # Keep the camera at its current Z height above the scene and
            # simply pan the focal point to (x, y).  This keeps every DXF/
            # SNT actor inside the clipping frustum.
            current_focal = list(camera.GetFocalPoint())
            current_pos   = list(camera.GetPosition())
            cam_offset_z  = current_pos[2] - current_focal[2]   # camera Z above focal

            camera.ParallelProjectionOn()
            camera.SetViewUp(0.0, 1.0, 0.0)
            camera.SetFocalPoint(x, y, current_focal[2])
            camera.SetPosition(x, y, current_focal[2] + cam_offset_z)

            # Zoom in: halve the parallel scale for a closer look (minimum 50 m)
            new_scale = max(50.0, camera.GetParallelScale() * 0.5)
            camera.SetParallelScale(new_scale)

            renderer.ResetCameraClippingRange()
            render_win.Render()

            # ── Ensure all overlay actors are still in the renderer ───
            try:
                if hasattr(self.app, '_ensure_overlay_actors'):
                    self.app._ensure_overlay_actors()
            except Exception:
                pass

            # ── Temporarily highlight the text actor ──────────────────
            if hasattr(actor, 'GetProperty'):
                original_color = tuple(actor.GetProperty().GetColor())
                original_scale = tuple(actor.GetScale())

                actor.GetProperty().SetColor(1.0, 1.0, 0.0)   # yellow
                render_win.Render()

                def reset_highlight():
                    try:
                        actor.GetProperty().SetColor(original_color)
                        actor.SetScale(original_scale)
                        renderer.RemoveActor(highlight_actor)
                        self._highlight_actor = None
                        render_win.Render()
                    except Exception:
                        pass

                from PySide6.QtCore import QTimer
                QTimer.singleShot(3000, reset_highlight)

            print(f"✅ Zoomed to block with RED CIRCLE highlight (30 m radius)")

            if hasattr(self.app, "statusBar"):
                self.app.statusBar().showMessage(
                    f"Zoomed to block: {block_data['label']}  ({x:.2f}, {y:.2f})", 4000
                )

        except Exception as e:
            print(f"⚠️ Zoom failed: {e}")
            import traceback
            traceback.print_exc()
            
    def highlight_location(self, x, y):
        """Create temporary highlight at location"""
        try:
            # Add temporary marker/circle at location
            if hasattr(self.app, 'viewer') and self.app.viewer:
                # You can implement this based on your viewer's API
                print(f"🎯 Highlighting location: ({x}, {y})")
                
                # Example: Create a temporary highlight layer
                # self.app.viewer.add_highlight_marker(x, y)
                
        except Exception as e:
            print(f"⚠️ Highlight failed: {e}")
            
    def debug_dxf_text_labels(self):
        """Debug: Print all text labels found in loaded DXF"""
        print("\n🔍 DEBUG: Scanning DXF for text labels...")
        
        try:
            if hasattr(self.app, 'dxf_actors') and self.app.dxf_actors:
                for dxf_data in self.app.dxf_actors:
                    print(f"\n📄 DXF File: {dxf_data.get('filename', 'Unknown')}")
                    
                    text_count = 0
                    for actor in dxf_data.get('actors', []):
                        if hasattr(actor, 'is_grid_label') and actor.is_grid_label:
                            grid_name = getattr(actor, 'grid_name', 'NO_NAME')
                            print(f"  📝 Text label: '{grid_name}'")
                            text_count += 1
                    
                    print(f"  Total text labels found: {text_count}")
            else:
                print("  ⚠️ No DXF actors found in app")
                
        except Exception as e:
            print(f"  ❌ Debug failed: {e}")
            import traceback
            traceback.print_exc()
            
            
    # ── Table right-click context menu ───────────────────────────────────────

    def _on_table_context_menu(self, pos):
        """Right-click context menu on the block table (MicroStation-style).

        Shows block-management actions for the currently selected row(s).
        Only available when at least one row is selected.
        """
        selected_indexes = self.table.selectionModel().selectedRows()
        if not selected_indexes:
            return

        from PySide6.QtWidgets import QMenu

        n = len({idx.row() for idx in selected_indexes})
        menu = QMenu(self)

        act_identify = menu.addAction(
            "Identify Block" if n == 1 else f"Identify {n} Blocks"
        )
        menu.addSeparator()
        act_rename = menu.addAction("Rename Definition…")
        act_rename.setEnabled(n == 1)  # rename only makes sense for a single block
        act_delete = menu.addAction("Delete Definition…")
        menu.addSeparator()
        act_export = menu.addAction("Export Selected to Sub-PRJ…")

        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_identify:
            self.identify_selected_block()
        elif chosen is act_rename:
            self.rename_block_definition()
        elif chosen is act_delete:
            self.delete_block_definition()
        elif chosen is act_export:
            self.export_selected_blocks_prj()

    # ── Add Definition ────────────────────────────────────────────────────────

    def add_block_definition(self):
        """Add new block definition(s) to the loaded PRJ from LAZ/LAS file(s).

        Matches MicroStation's "Add using files..." — multiple files can be
        selected at once.  Each file's header is read to get the XY bounding
        box automatically (no points are loaded).  The bounding rectangle of
        each file becomes its block polygon.  All blocks are written in a
        single backup + atomic write.  A .prj.bak backup is created before
        the file is modified.
        """
        # ── Guard: PRJ must be loaded ─────────────────────────────────────────
        if not self.current_prj_path:
            QMessageBox.warning(
                self,
                "No PRJ Loaded",
                "Please load a PRJ file before adding a block definition.",
            )
            return
        if not os.path.isfile(self.current_prj_path):
            QMessageBox.critical(
                self,
                "PRJ File Not Found",
                f"The PRJ file no longer exists on disk:\n\n{self.current_prj_path}",
            )
            return

        # ── Browse for one or more LAZ / LAS files ─────────────────────────────
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Select LAZ / LAS File(s) to Add",
            self.current_directory or "",
            "Point Cloud Files (*.laz *.las);;All Files (*.*)",
        )
        if not file_paths:
            return  # user cancelled

        # ── Read headers, validate, build candidate entries ────────────────────
        existing_upper = {e.get("label", "").strip().upper() for e in self.prj_data}
        candidates = []     # (filename, stem, bounds, polygon)
        skipped_dupes = []
        failed_reads = []

        for file_path in file_paths:
            filename = os.path.basename(file_path)
            stem = filename
            for ext in (".laz", ".las"):
                if stem.lower().endswith(ext):
                    stem = stem[: len(stem) - len(ext)]
                    break

            if stem.strip().upper() in existing_upper:
                skipped_dupes.append(filename)
                continue

            try:
                bounds = self._read_laz_header_bounds(file_path)
            except Exception as exc:
                failed_reads.append((filename, str(exc)))
                continue

            polygon = [
                (bounds["min_x"], bounds["min_y"]),
                (bounds["max_x"], bounds["min_y"]),
                (bounds["max_x"], bounds["max_y"]),
                (bounds["min_x"], bounds["max_y"]),
                (bounds["min_x"], bounds["min_y"]),  # close ring
            ]
            candidates.append((filename, stem, bounds, polygon))
            existing_upper.add(stem.strip().upper())   # guard within this batch too

        if not candidates:
            msg = "No blocks could be added.\n"
            if skipped_dupes:
                msg += f"\nAlready exist ({len(skipped_dupes)}): " + ", ".join(skipped_dupes[:10])
            if failed_reads:
                msg += f"\nHeader read failed ({len(failed_reads)}): " + ", ".join(f[0] for f in failed_reads[:10])
            QMessageBox.warning(self, "Nothing to Add", msg)
            return

        # ── Confirm with summary of all files ───────────────────────────────────
        total_area = sum(
            (b["max_x"] - b["min_x"]) * (b["max_y"] - b["min_y"]) for _, _, b, _ in candidates
        )
        total_pts = sum(b["point_count"] for _, _, b, _ in candidates)
        summary_lines = "\n".join(
            f"  • {fn}  ({b['point_count']:,} pts)" for fn, _, b, _ in candidates[:15]
        )
        if len(candidates) > 15:
            summary_lines += f"\n  ...and {len(candidates) - 15} more"
        warn_lines = ""
        if skipped_dupes:
            warn_lines += f"\n\n{len(skipped_dupes)} file(s) skipped (already exist): " + ", ".join(skipped_dupes[:5])
        if failed_reads:
            warn_lines += f"\n\n{len(failed_reads)} file(s) skipped (header read failed): " + ", ".join(f[0] for f in failed_reads[:5])

        reply = QMessageBox.question(
            self,
            "Add Block Definitions",
            (
                f"Add {len(candidates)} block(s) to the PRJ?\n\n"
                f"{summary_lines}\n\n"
                f"Total points: {total_pts:,}\n"
                f"Total area:   {total_area:.2f} m²\n\n"
                "Each file's header bounding box will be used as its block polygon."
                f"{warn_lines}"
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return

        # ── Read PRJ file ─────────────────────────────────────────────────────
        try:
            with open(self.current_prj_path, "r", encoding="utf-8") as fh:
                prj_content = fh.read()
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Read Error",
                f"Could not read the PRJ file:\n\nError: {exc}",
            )
            return

        # ── Build all new block sections ────────────────────────────────────────
        group_first = self._get_next_group_first(prj_content)
        if prj_content and not prj_content.endswith("\n"):
            prj_content += "\n"

        new_sections = [
            self._build_block_section_text(filename, polygon, group_first + i * 1_000_000)
            for i, (filename, _, _, polygon) in enumerate(candidates)
        ]
        new_content = prj_content + "".join(new_sections)

        # ── Write to backend working copy (real .prj untouched until Save) ────
        try:
            self._write_prj_to_working(new_content)
            print(f"  ✅ {len(candidates)} block(s) appended to backend working copy")
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Write Error",
                f"Failed to buffer the PRJ changes:\n\nError: {exc}\n\n"
                "The original file has NOT been modified.",
            )
            return

        # ── Update in-memory state ────────────────────────────────────────────
        try:
            for filename, stem, bounds, polygon in candidates:
                avg_x = (bounds["min_x"] + bounds["max_x"]) / 2.0
                avg_y = (bounds["min_y"] + bounds["max_y"]) / 2.0
                area_m2 = (bounds["max_x"] - bounds["min_x"]) * (bounds["max_y"] - bounds["min_y"])
                new_entry = {
                    "label": stem,
                    "easting": f"{avg_x:.2f}",
                    "northing": f"{avg_y:.2f}",
                    "description": "Boundary: 4 points",
                    "boundary_coords": [(x, y) for x, y in polygon[:-1]],
                }
                new_prj_idx = len(self.prj_data)
                self.prj_data.append(new_entry)
                self._all_row_data.append(
                    (stem, f"{bounds['point_count']:,}", f"{area_m2:.2f}", None, None, new_prj_idx)
                )

            self._sort_header._sort_states = {}
            self._sort_header.viewport().update()
            self._repopulate_table(self._all_row_data)
            self._on_search_changed(self.search_box.text())
            print(f"  ✅ In-memory state updated: {len(self.prj_data)} blocks")
        except Exception as exc:
            print(f"  ⚠️ Failed to refresh table after add: {exc}")
            QMessageBox.warning(
                self,
                "Display Refresh Issue",
                "The block(s) were added to the PRJ file successfully, but the table\n"
                "could not refresh automatically.\n\n"
                "Please close and reopen the PRJ Block Identifier to see the change.",
            )
            return

        result_msg = (
            f"{len(candidates)} block definition(s) added successfully.\n\n"
            f"Total points: {total_pts:,}\n"
            f"Total area:   {total_area:.2f} m²\n\n"
            f"Backup saved as:  {os.path.basename(bak_path)}"
        )
        if skipped_dupes or failed_reads:
            result_msg += "\n\nSkipped:"
            if skipped_dupes:
                result_msg += f"\n  • {len(skipped_dupes)} duplicate(s)"
            if failed_reads:
                result_msg += f"\n  • {len(failed_reads)} header read failure(s)"
        if hasattr(self.app, "statusBar"):
            self.app.statusBar().showMessage(result_msg.replace("\n", "  "), 5000)

    # ── Rename Definition ─────────────────────────────────────────────────────

    def rename_block_definition(self):
        """Rename a single block definition in the .prj file on disk.

        Only the 'Block <filename>' line in the .prj manifest is changed.
        The LAZ/LAS file on disk is NOT renamed.  A .prj.bak backup is
        written before any change is saved.
        """
        # ── Guard: PRJ must be loaded ─────────────────────────────────────────
        if not self.current_prj_path:
            QMessageBox.warning(
                self, "No PRJ Loaded",
                "Please load a PRJ file before renaming a block definition.",
            )
            return
        if not os.path.isfile(self.current_prj_path):
            QMessageBox.critical(
                self, "PRJ File Not Found",
                f"The PRJ file no longer exists:\n\n{self.current_prj_path}",
            )
            return

        # ── Guard: exactly one row must be selected ───────────────────────────
        selected_indexes = self.table.selectionModel().selectedRows()
        visual_rows = {idx.row() for idx in selected_indexes}
        if len(visual_rows) != 1:
            QMessageBox.warning(
                self,
                "Single Selection Required",
                "Please select exactly one block to rename.",
            )
            return

        vrow = next(iter(visual_rows))
        label_item = self.table.item(vrow, 0)
        if label_item is None:
            return
        prj_idx = label_item.data(Qt.UserRole)
        if prj_idx is None:
            return
        try:
            prj_idx = int(prj_idx)
        except (TypeError, ValueError):
            return
        if prj_idx < 0 or prj_idx >= len(self.prj_data):
            return
        old_label = self.prj_data[prj_idx].get("label", "").strip()
        if not old_label:
            return

        # ── Guard: block must not be locked ───────────────────────────────────
        if self.prj_data[prj_idx].get("locked", False):
            QMessageBox.warning(
                self, "Block Locked",
                f"'{old_label}' is locked.\n\nUse Block → Release lock before renaming it."
            )
            return

        # ── Read PRJ file ─────────────────────────────────────────────────────
        try:
            with open(self.current_prj_path, "r", encoding="utf-8") as fh:
                prj_content = fh.read()
        except Exception as exc:
            QMessageBox.critical(
                self, "Read Error",
                f"Could not read the PRJ file:\n\nError: {exc}",
            )
            return

        # Detect which extension the existing block line uses
        old_ext = ".laz"
        for ext in (".laz", ".las"):
            if (
                f"Block {old_label}{ext}" in prj_content
                or f"Block {old_label}{ext.upper()}" in prj_content
            ):
                old_ext = ext
                break

        # ── Prompt for new name ───────────────────────────────────────────────
        from PySide6.QtWidgets import QInputDialog
        new_stem, ok = QInputDialog.getText(
            self,
            "Rename Block Definition",
            (
                f"Current name:  {old_label}\n\n"
                "Enter the new block name (stem only, no file extension):"
            ),
            text=old_label,
        )
        if not ok:
            return
        new_stem = new_stem.strip()

        if not new_stem:
            QMessageBox.warning(self, "Invalid Name", "Block name cannot be empty.")
            return
        if new_stem == old_label:
            return  # no change requested
        if any(ch in new_stem for ch in r'/\:*?"<>|'):
            QMessageBox.warning(
                self, "Invalid Name",
                f"Block name contains invalid characters:\n  {new_stem}",
            )
            return
        if "." in new_stem:
            QMessageBox.warning(
                self, "Invalid Name",
                "Enter just the stem — do not include a file extension.",
            )
            return

        # ── Guard: no duplicates ──────────────────────────────────────────────
        existing_upper = {e.get("label", "").strip().upper() for e in self.prj_data}
        if new_stem.strip().upper() in existing_upper:
            QMessageBox.warning(
                self, "Duplicate Name",
                f"A block named '{new_stem}' already exists in this PRJ.",
            )
            return

        new_filename = new_stem + old_ext

        # ── Build modified content ────────────────────────────────────────────
        try:
            new_content = self._rename_block_in_prj_text(
                prj_content, old_label, new_filename
            )
        except Exception as exc:
            QMessageBox.critical(
                self, "Parse Error",
                f"Failed to process PRJ content:\n\nError: {exc}",
            )
            return

        if new_content == prj_content:
            QMessageBox.information(
                self, "No Changes",
                "The old block name was not found in the PRJ file text.\n"
                "No changes were written.",
            )
            return

        # ── Write to backend working copy (real .prj untouched until Save) ────
        try:
            self._write_prj_to_working(new_content)
            print(f"  ✅ Block renamed in backend working copy: {old_label} → {new_stem}")
        except Exception as exc:
            QMessageBox.critical(
                self, "Write Error",
                f"Failed to buffer the PRJ changes:\n\nError: {exc}\n\n"
                "The original file has NOT been modified.",
            )
            return

        # ── Update in-memory state ────────────────────────────────────────────
        try:
            self.prj_data[prj_idx]["label"] = new_stem
            for i, row in enumerate(self._all_row_data):
                if row[0].strip().upper() == old_label.upper():
                    self._all_row_data[i] = (
                        new_stem, row[1], row[2], row[3], row[4], row[5]
                    )
                    break

            self._sort_header._sort_states = {}
            self._sort_header.viewport().update()
            self._repopulate_table(self._all_row_data)
            self._on_search_changed(self.search_box.text())
            print(f"  ✅ Table refreshed after rename")
        except Exception as exc:
            print(f"  ⚠️ Failed to refresh table after rename: {exc}")
            QMessageBox.warning(
                self, "Display Refresh Issue",
                "The PRJ was saved, but the table could not refresh automatically.\n\n"
                "Please reload the PRJ file to see the updated name.",
            )
            return

        if hasattr(self.app, "statusBar"):
            self.app.statusBar().showMessage(
                f"Renamed '{old_label}' → '{new_stem}'", 5000
            )

    # ── Export Sub-PRJ ────────────────────────────────────────────────────────

    def export_selected_blocks_prj(self):
        """Export selected block definitions to a new standalone PRJ file.

        The original PRJ header (scanner settings, storage format, etc.) is
        preserved.  Only the selected block sections are written to the new
        file.  LAZ/LAS files on disk are never copied or modified.
        """
        # ── Guard: PRJ must be loaded ─────────────────────────────────────────
        if not self.current_prj_path:
            QMessageBox.warning(
                self, "No PRJ Loaded",
                "Please load a PRJ file before exporting block definitions.",
            )
            return
        if not os.path.isfile(self.current_prj_path):
            QMessageBox.critical(
                self, "PRJ File Not Found",
                f"The PRJ file no longer exists:\n\n{self.current_prj_path}",
            )
            return

        # ── Guard: selection ──────────────────────────────────────────────────
        selected_indexes = self.table.selectionModel().selectedRows()
        if not selected_indexes:
            QMessageBox.warning(
                self, "No Selection",
                "Select one or more blocks to export.",
            )
            return

        # ── Collect labels ────────────────────────────────────────────────────
        visual_rows = sorted({idx.row() for idx in selected_indexes})
        labels_to_export = []
        for vrow in visual_rows:
            label_item = self.table.item(vrow, 0)
            if label_item is None:
                continue
            prj_idx = label_item.data(Qt.UserRole)
            if prj_idx is None:
                continue
            try:
                prj_idx = int(prj_idx)
            except (TypeError, ValueError):
                continue
            if 0 <= prj_idx < len(self.prj_data):
                lbl = self.prj_data[prj_idx].get("label", "").strip()
                if lbl and lbl not in labels_to_export:
                    labels_to_export.append(lbl)

        if not labels_to_export:
            QMessageBox.warning(
                self, "Nothing to Export",
                "Could not resolve any block labels from the current selection.\n"
                "The table may be out of sync — try reloading the PRJ file.",
            )
            return

        # ── File-save dialog ──────────────────────────────────────────────────
        base = os.path.splitext(os.path.basename(self.current_prj_path))[0]
        suggested = os.path.join(
            os.path.dirname(self.current_prj_path),
            f"{base}_sub_{len(labels_to_export)}blocks.prj",
        )
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Sub-PRJ — Save As",
            suggested,
            "PRJ Files (*.prj);;All Files (*.*)",
        )
        if not save_path:
            return  # user cancelled

        # ── Read source PRJ ───────────────────────────────────────────────────
        try:
            with open(self.current_prj_path, "r", encoding="utf-8") as fh:
                prj_content = fh.read()
        except Exception as exc:
            QMessageBox.critical(
                self, "Read Error",
                f"Could not read the source PRJ:\n\nError: {exc}",
            )
            return

        # ── Build new PRJ content: header + selected blocks ───────────────────
        try:
            labels_upper = {lbl.strip().upper() for lbl in labels_to_export}
            header_text = self._extract_header_from_prj_text(prj_content)
            blocks_text = self._extract_blocks_from_prj_text(prj_content, labels_upper)
            new_content = header_text + blocks_text
        except Exception as exc:
            QMessageBox.critical(
                self, "Parse Error",
                f"Failed to process PRJ content:\n\nError: {exc}",
            )
            return

        if not blocks_text.strip():
            QMessageBox.warning(
                self, "Nothing Exported",
                "The selected block label(s) were not found as 'Block' entries "
                "in the PRJ text.\nNo file was written.",
            )
            return

        # ── Write new PRJ file ────────────────────────────────────────────────
        try:
            with open(save_path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(new_content)
            print(f"  ✅ Sub-PRJ written: {save_path}")
        except Exception as exc:
            QMessageBox.critical(
                self, "Write Error",
                f"Could not write the sub-PRJ file:\n\n{save_path}\n\nError: {exc}",
            )
            return

        if hasattr(self.app, "statusBar"):
            self.app.statusBar().showMessage(
                f"Exported {len(labels_to_export)} block(s) to {os.path.basename(save_path)}", 5000
            )

    # ── Static helpers shared by Add / Rename / Export / Delete ──────────────

    @staticmethod
    def _read_laz_header_bounds(file_path):
        """Read XY extents and point count from a LAZ/LAS file header only.

        Uses laspy header read — no points are loaded into memory.
        Works even on billion-point files in under a second.
        """
        import laspy
        with laspy.open(file_path) as las:
            h = las.header
            return {
                "min_x": float(h.mins[0]),
                "max_x": float(h.maxs[0]),
                "min_y": float(h.mins[1]),
                "max_y": float(h.maxs[1]),
                "point_count": int(h.point_count),
            }

    @staticmethod
    def _get_next_group_first(prj_content):
        """Return the next available GroupFirst value (max existing + 1 000 000).

        Falls back to 1 000 000 when no existing GroupFirst lines are found.
        """
        import re
        matches = re.findall(r"^GroupFirst=(\d+)", prj_content, re.MULTILINE)
        if not matches:
            return 1_000_000
        return max(int(m) for m in matches) + 1_000_000

    @staticmethod
    def _build_block_section_text(filename, polygon_coords, group_first,
                                  group_count=1_000_000):
        """Build the text for one block section to append to a .prj file."""
        lines = [
            f"Block {filename}",
            f"GroupFirst={group_first}",
            f"GroupCount={group_count}",
        ]
        for x, y in polygon_coords:
            lines.append(f" {x:.4f} {y:.4f}")
        lines.append("")  # trailing blank line required by TerraScan parser
        return "\n".join(lines) + "\n"

    @staticmethod
    def _rename_block_in_prj_text(content, old_label, new_filename):
        """Replace the 'Block <old_label>.<ext>' line with 'Block <new_filename>'.

        Matches case-insensitively on the extension (.laz/.las) while keeping
        the rest of the file byte-for-byte identical.
        """
        import re
        # Primary: match "Block <label>.laz" or "Block <label>.las" (any case)
        pattern = re.compile(
            r"^(Block[ \t]+)"
            + re.escape(old_label)
            + r"\.[lL][aA][zZsS][ \t]*$",
            re.MULTILINE,
        )
        result, n = pattern.subn(f"Block {new_filename}", content)
        if n == 0:
            # Fallback: block line with no extension at all
            pattern2 = re.compile(
                r"^Block[ \t]+" + re.escape(old_label) + r"[ \t]*$",
                re.MULTILINE,
            )
            result, _ = pattern2.subn(f"Block {new_filename}", content)
        return result

    @staticmethod
    def _extract_header_from_prj_text(content):
        """Return every line before the first 'Block ' entry (the file header)."""
        lines = content.splitlines(keepends=True)
        header = []
        for line in lines:
            if line.strip().startswith("Block "):
                break
            header.append(line)
        return "".join(header)

    @staticmethod
    def _extract_blocks_from_prj_text(content, labels_upper):
        """Return only the block sections whose labels are in labels_upper.

        Args:
            content:       Full .prj file text.
            labels_upper:  Uppercase label set (stems without file extension).

        Returns:
            Text containing only the matching block sections.
        """
        if not labels_upper:
            return ""
        lines = content.splitlines(keepends=True)
        result = []
        keeping = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("Block "):
                fname = stripped[6:].strip()
                stem = fname
                for ext in (".laz", ".las"):
                    if stem.lower().endswith(ext):
                        stem = stem[: len(stem) - len(ext)]
                        break
                keeping = stem.strip().upper() in labels_upper
            if keeping:
                result.append(line)
        return "".join(result)

    def _derive_prj_viewport_offset(self):
        """Compute PRJ→viewport coordinate offsets from all grid label actors.

        Scans every rendered grid-label actor, looks up the matching PRJ
        block, and computes the XY delta between the actor's rendered
        position and the PRJ centroid.  Returns a list of (dx, dy) pairs.
        A consistent non-zero offset means the DGN/SNT coordinate system
        differs from the PRJ projection — common when a DGN stores
        MicroStation UOR values rather than real-world projected metres.
        """
        offsets = []
        try:
            if not self.prj_data:
                return offsets

            # Build a quick PRJ centroid lookup
            prj_lookup = {}
            for block in self.prj_data:
                label = block.get('label', '')
                coords = block.get('boundary_coords') or []
                if label and len(coords) >= 3:
                    cx, cy = self._polygon_target_xy(coords)
                    prj_lookup[
                        "".join(ch.lower() for ch in label if ch.isalnum())
                    ] = (cx, cy)

            if not prj_lookup:
                return offsets

            # Scan rendered actors
            for actor_list_attr in ('snt_actors', 'dxf_actors'):
                for data_set in getattr(self.app, actor_list_attr, []):
                    for actor in data_set.get('actors', []):
                        grid_name = getattr(actor, 'grid_name', '')
                        if not grid_name:
                            continue
                        actor_key = "".join(
                            ch.lower() for ch in str(grid_name) if ch.isalnum()
                        )
                        if actor_key not in prj_lookup:
                            continue
                        try:
                            b = actor.GetBounds()
                            ax = (b[0] + b[1]) / 2.0
                            ay = (b[2] + b[3]) / 2.0
                        except Exception:
                            continue
                        px, py = prj_lookup[actor_key]
                        dx = ax - px
                        dy = ay - py
                        if abs(dx) > 1.0 or abs(dy) > 1.0:
                            offsets.append((dx, dy))
                        if len(offsets) >= 10:
                            return offsets   # enough samples
        except Exception as e:
            print(f"  ⚠️ _derive_prj_viewport_offset error: {e}")
        return offsets

    @staticmethod
    def _polygon_target_xy(points_xy):
        """Return a stable target point for PRJ block navigation.

        The average of boundary vertices can sit outside a concave polygon, so
        this prefers a true polygon centroid and falls back to an interior-ish
        probe when necessary.
        """
        pts = [(float(x), float(y)) for x, y in points_xy if x is not None and y is not None]
        if not pts:
            return (0.0, 0.0)
        if len(pts) == 1:
            return pts[0]
        if len(pts) == 2:
            return ((pts[0][0] + pts[1][0]) / 2.0, (pts[0][1] + pts[1][1]) / 2.0)

        closed = list(pts)
        if closed[0] != closed[-1]:
            closed.append(closed[0])

        twice_area = 0.0
        cx = 0.0
        cy = 0.0
        for i in range(len(closed) - 1):
            x0, y0 = closed[i]
            x1, y1 = closed[i + 1]
            cross = x0 * y1 - x1 * y0
            twice_area += cross
            cx += (x0 + x1) * cross
            cy += (y0 + y1) * cross

        if abs(twice_area) > 1e-12:
            centroid = (cx / (3.0 * twice_area), cy / (3.0 * twice_area))
            if _point_in_polygon_xy(centroid[0], centroid[1], pts):
                return centroid

        min_x = min(x for x, _ in pts)
        max_x = max(x for x, _ in pts)
        min_y = min(y for _, y in pts)
        max_y = max(y for _, y in pts)

        probes = [
            ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0),
            (min_x + (max_x - min_x) * 0.5, min_y + (max_y - min_y) * 0.5),
            (min_x + (max_x - min_x) * 0.25, min_y + (max_y - min_y) * 0.25),
            (min_x + (max_x - min_x) * 0.75, min_y + (max_y - min_y) * 0.75),
            (min_x + (max_x - min_x) * 0.1, min_y + (max_y - min_y) * 0.9),
        ]
        for probe in probes:
            if _point_in_polygon_xy(probe[0], probe[1], pts):
                return probe
        return probes[0]

    # ── Delete Definition ─────────────────────────────────────────────────────

    def delete_block_definition(self):
        """Permanently remove selected block definition(s) from the .prj file on disk.

        Only the .prj manifest text is modified — the actual LAZ/LAS files are
        never touched.  A .prj.bak backup is written before any change is saved.
        The in-memory block table is updated immediately after a successful write
        so the user sees the result without having to reload.
        """
        # ── Guard: a PRJ file must be loaded ─────────────────────────────────
        if not self.current_prj_path:
            QMessageBox.warning(
                self,
                "No PRJ Loaded",
                "Please load a PRJ file before attempting to delete block definitions.",
            )
            return

        if not os.path.isfile(self.current_prj_path):
            QMessageBox.critical(
                self,
                "PRJ File Not Found",
                f"The PRJ file no longer exists on disk:\n\n"
                f"{self.current_prj_path}\n\n"
                "Please reload the file.",
            )
            return

        # ── Guard: at least one row must be selected ──────────────────────────
        selected_indexes = self.table.selectionModel().selectedRows()
        if not selected_indexes:
            QMessageBox.warning(
                self,
                "No Selection",
                "Please select one or more blocks in the list\n"
                "before clicking Delete Definition.",
            )
            return

        # ── Resolve selected visual rows → unique block labels ────────────────
        visual_rows = sorted({idx.row() for idx in selected_indexes})
        labels_to_delete = []
        for vrow in visual_rows:
            label_item = self.table.item(vrow, 0)
            if label_item is None:
                continue
            prj_idx = label_item.data(Qt.UserRole)
            if prj_idx is None:
                continue
            try:
                prj_idx = int(prj_idx)
            except (TypeError, ValueError):
                continue
            if prj_idx < 0 or prj_idx >= len(self.prj_data):
                continue
            label = self.prj_data[prj_idx].get("label", "").strip()
            if label and label not in labels_to_delete:
                labels_to_delete.append(label)

        if not labels_to_delete:
            QMessageBox.warning(
                self,
                "Nothing to Delete",
                "Could not resolve any valid block labels from the selection.\n"
                "The table may be out of sync — try reloading the PRJ file.",
            )
            return

        # ── Guard: refuse if any selected block is locked ─────────────────────
        locked_labels = [
            block.get("label", "").strip()
            for block in self.prj_data
            if block.get("label", "").strip() in labels_to_delete and block.get("locked", False)
        ]
        if locked_labels:
            QMessageBox.warning(
                self, "Block(s) Locked",
                "The following selected block(s) are locked and cannot be deleted:\n\n"
                + "\n".join(f"• {lbl}" for lbl in locked_labels)
                + "\n\nUse Block → Release lock first."
            )
            return

        # ── Confirmation dialog ───────────────────────────────────────────────
        prj_name = os.path.basename(self.current_prj_path)
        preview_lines = "\n".join(f"  • {lbl}" for lbl in labels_to_delete[:12])
        if len(labels_to_delete) > 12:
            preview_lines += f"\n  … and {len(labels_to_delete) - 12} more"

        reply = QMessageBox.warning(
            self,
            "Delete Block Definition(s)",
            (
                f"This will permanently remove {len(labels_to_delete)} block "
                f"definition(s) from:\n\n  {prj_name}\n\n"
                f"{preview_lines}\n\n"
                "The LAZ/LAS files on disk are NOT deleted.\n"
                "Changes are buffered in a backend working copy and only written\n"
                "to the real PRJ when you press 'Save project'.\n\n"
                "This action cannot be undone.  Continue?"
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        # ── Read the current .prj text ────────────────────────────────────────
        try:
            with open(self.current_prj_path, "r", encoding="utf-8") as fh:
                original_content = fh.read()
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Read Error",
                f"Could not read the PRJ file:\n\n{self.current_prj_path}\n\n"
                f"Error: {exc}",
            )
            return

        # ── Build the modified text ───────────────────────────────────────────
        try:
            labels_set_upper = {lbl.strip().upper() for lbl in labels_to_delete}
            new_content = self._remove_blocks_from_prj_text(original_content, labels_set_upper)
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Parse Error",
                f"Failed to parse the PRJ content:\n\nError: {exc}",
            )
            return

        # Sanity-check: did anything actually change?
        if new_content == original_content:
            QMessageBox.information(
                self,
                "No Changes Written",
                "The selected block label(s) were not found as 'Block' entries in the "
                "PRJ file.\nNo changes were made to disk.",
            )
            return

        # ── Write to backend working copy (real .prj untouched until Save) ────
        try:
            self._write_prj_to_working(new_content)
            print(f"  ✅ Deleted block(s) written to backend working copy")
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Write Error",
                f"Failed to buffer the updated PRJ file:\n\nError: {exc}\n\n"
                "The original file has NOT been modified.",
            )
            return

        # ── Clear VTK highlights for deleted blocks ───────────────────────────
        for lbl in labels_to_delete:
            try:
                if (
                    hasattr(self, "_highlighted_blocks")
                    and isinstance(self._highlighted_blocks, dict)
                    and lbl in self._highlighted_blocks
                ):
                    self.clear_highlight_for_block(lbl)
            except Exception:
                pass  # highlight cleanup is non-critical

        # ── Update the in-memory block table ──────────────────────────────────
        try:
            labels_set_norm = {lbl.strip().upper() for lbl in labels_to_delete}

            # Filter prj_data and _all_row_data, keeping only blocks not deleted
            new_prj_data = [
                entry for entry in self.prj_data
                if entry.get("label", "").strip().upper() not in labels_set_norm
            ]
            kept_rows = [
                row for row in self._all_row_data
                if row[0].strip().upper() not in labels_set_norm
            ]

            # Re-build the prj_idx (column 5 in each row tuple) so every kept row
            # points to the correct index in the new (shorter) prj_data list.
            new_idx_map = {
                entry["label"].strip().upper(): i
                for i, entry in enumerate(new_prj_data)
            }
            reindexed = []
            for row in kept_rows:
                norm = row[0].strip().upper()
                new_idx = new_idx_map.get(norm, 0)
                reindexed.append((row[0], row[1], row[2], row[3], row[4], new_idx))

            self.prj_data = new_prj_data
            self._all_row_data = reindexed

            # Reset sort arrows to neutral and repopulate the table
            self._sort_header._sort_states = {}
            self._sort_header.viewport().update()
            self._repopulate_table(self._all_row_data)

            # Re-apply any active search text
            self._on_search_changed(self.search_box.text())

            print(
                f"  ✅ In-memory PRJ state updated: "
                f"{len(self.prj_data)} block(s) remain"
            )
        except Exception as exc:
            # The disk write succeeded, so this is non-fatal.
            # Ask the user to reload to see the updated state.
            print(f"  ⚠️ Failed to refresh in-memory PRJ state: {exc}")
            QMessageBox.warning(
                self,
                "Display Refresh Issue",
                "The PRJ file was saved successfully, but the block list could not "
                "be refreshed automatically.\n\n"
                "Please close and reopen the PRJ Block Identifier to see the changes.",
            )
            return

        n = len(labels_to_delete)
        remaining = len(self.prj_data)
        if hasattr(self.app, "statusBar"):
            self.app.statusBar().showMessage(
                f"Deleted {n} block(s) — {remaining} remaining in project", 5000
            )

    @staticmethod
    def _remove_blocks_from_prj_text(content: str, labels_upper: set) -> str:
        """Return new .prj file content with the named block sections removed.

        Processes the file line-by-line.  When a 'Block <filename>' line is
        encountered the label (filename without extension) is compared against
        labels_upper (pre-normalised to uppercase).  If it matches, that line
        and every subsequent line belonging to that block section (GroupFirst=,
        GroupCount=, coordinate pairs, trailing blank line) are omitted from
        the output.  Skipping stops as soon as the next 'Block ' header is
        reached, at which point the new block is evaluated independently.

        The file header ([TerraScan project] and all key=value settings) is
        never touched.

        Args:
            content:       Full UTF-8 text of the .prj file.
            labels_upper:  Set of uppercase label strings (filename without
                           extension) that should be removed.

        Returns:
            Modified file text as a single string.
        """
        if not labels_upper:
            return content

        lines = content.splitlines(keepends=True)
        result = []
        skipping = False  # True while inside a block section to be removed

        for line in lines:
            stripped = line.strip()

            if stripped.startswith("Block "):
                # Derive the label: everything after "Block ", strip extension
                fname = stripped[6:].strip()
                stem = fname
                for ext in (".laz", ".las"):
                    if stem.lower().endswith(ext):
                        stem = stem[: len(stem) - len(ext)]
                        break
                skipping = stem.strip().upper() in labels_upper
                # Fall through: line is appended below only when skipping=False

            if not skipping:
                result.append(line)

        return "".join(result)

    @staticmethod
    def _replace_block_boundary_in_prj_text(content: str, label: str, new_polygon) -> str:
        """Return new .prj content with one block's coordinate lines replaced.

        The 'Block <filename>' line and its GroupFirst=/GroupCount= metadata
        lines are left untouched — only the polygon coordinate lines that
        follow are swapped for new_polygon (a list of (x, y) tuples, open
        ring — the first point is NOT repeated at the end).

        Args:
            content:     Full UTF-8 text of the .prj file.
            label:       Block label (filename without extension) to edit.
            new_polygon: List of (x, y) tuples for the new boundary.

        Returns:
            Modified file text as a single string.
        """
        label_upper = label.strip().upper()
        lines = content.splitlines(keepends=True)
        result = []
        in_target_block = False
        skipping_old_coords = False

        for line in lines:
            stripped = line.strip()

            if stripped.startswith("Block "):
                fname = stripped[6:].strip()
                stem = fname
                for ext in (".laz", ".las"):
                    if stem.lower().endswith(ext):
                        stem = stem[: len(stem) - len(ext)]
                        break
                in_target_block = (stem.strip().upper() == label_upper)
                skipping_old_coords = False
                result.append(line)
                continue

            if in_target_block and (stripped.startswith("GroupFirst=") or stripped.startswith("GroupCount=")):
                result.append(line)
                continue

            if in_target_block and not skipping_old_coords:
                # First coordinate line after the metadata — write the new ring here
                skipping_old_coords = True
                for x, y in new_polygon:
                    result.append(f" {x:.4f} {y:.4f}\n")
                # the closing point (repeat of first) to match existing file convention
                if new_polygon:
                    fx, fy = new_polygon[0]
                    result.append(f" {fx:.4f} {fy:.4f}\n")
                continue   # do not append the old coordinate line being replaced

            if in_target_block and skipping_old_coords:
                continue   # skip remaining old coordinate lines for this block

            result.append(line)

        return "".join(result)

    # ── Select by Fence ───────────────────────────────────────────────────────

    def _fence_highlight_clear(self):
        """Remove all fence-selection boundary-polygon highlights from the viewport."""
        if not self._fence_highlight_actors:
            return
        try:
            vtk_widget = getattr(self.app, "vtk_widget", None)
            renderer = getattr(vtk_widget, "renderer", None)
            for actor in self._fence_highlight_actors:
                try:
                    if renderer is not None:
                        renderer.RemoveActor(actor)
                except Exception:
                    pass
            self._fence_highlight_actors = []
            if vtk_widget is not None:
                vtk_widget.GetRenderWindow().Render()
        except Exception:
            self._fence_highlight_actors = []

    def _highlight_fence_selected_blocks(self, labels_upper: Set[str]):
        """Draw bright boundary-polygon outlines for selected blocks (MicroStation-style).

        Uses the boundary_coords already stored in prj_data — no DXF/SNT actor
        lookup required.  Each selected block gets a cyan polyline at z=1 so it
        renders on top of the existing SNT grid rectangles.
        """
        self._fence_highlight_clear()
        if not labels_upper:
            return
        vtk_widget = getattr(self.app, "vtk_widget", None)
        renderer = getattr(vtk_widget, "renderer", None)
        if renderer is None:
            return
        try:
            import vtk
            for block in self.prj_data:
                lbl = block.get("label", "").strip().upper()
                if lbl not in labels_upper:
                    continue
                coords = block.get("boundary_coords", [])
                if len(coords) < 2:
                    continue

                # Build closed polyline in world coords at z=1 (above SNT grid)
                all_pts = list(coords)
                if all_pts[0] != all_pts[-1]:
                    all_pts.append(all_pts[0])   # close the ring

                pts = vtk.vtkPoints()
                for x, y in all_pts:
                    pts.InsertNextPoint(float(x), float(y), 1.0)

                n = pts.GetNumberOfPoints()
                poly_line = vtk.vtkPolyLine()
                poly_line.GetPointIds().SetNumberOfIds(n)
                for i in range(n):
                    poly_line.GetPointIds().SetId(i, i)
                cells = vtk.vtkCellArray()
                cells.InsertNextCell(poly_line)

                pd = vtk.vtkPolyData()
                pd.SetPoints(pts)
                pd.SetLines(cells)

                mapper = vtk.vtkPolyDataMapper()
                mapper.SetInputData(pd)

                actor = vtk.vtkActor()
                actor.SetMapper(mapper)
                actor.GetProperty().SetColor(0.0, 1.0, 1.0)   # cyan — MicroStation selection colour
                actor.GetProperty().SetLineWidth(3.0)
                actor.GetProperty().SetOpacity(1.0)
                try:
                    actor.PickableOff()
                except Exception:
                    pass

                renderer.AddActor(actor)
                self._fence_highlight_actors.append(actor)

            if self._fence_highlight_actors:
                vtk_widget.GetRenderWindow().Render()
        except Exception as e:
            print(f"⚠️ Fence boundary highlight failed: {e}")

    def _toggle_fence_mode(self, checked: bool):
        if checked:
            self._fence_activate()
        else:
            self._fence_deactivate(cancel=True)

    def _fence_activate(self):
        if self._addb_active:
            self._addb_deactivate(cancel=True)
        if self._editdef_active:
            self._editdef_deactivate(cancel=True)
        vtk_widget = getattr(self.app, "vtk_widget", None)
        if vtk_widget is None:
            self._fence_action.blockSignals(True)
            self._fence_action.setChecked(False)
            self._fence_action.blockSignals(False)
            return
        self._fence_active = True
        self._fence_start_screen = None
        self._fence_current_screen = None
        self._fence_actor = None
        vtk_widget.installEventFilter(self)
        vtk_widget.setCursor(Qt.CrossCursor)
        if hasattr(self.app, "statusBar"):
            self.app.statusBar().showMessage(
                "Select by Fence: drag a rectangle to select blocks — Esc or right-click to cancel",
                0,
            )

    def _fence_deactivate(self, *, cancel: bool = False):
        if not self._fence_active:
            self._fence_action.blockSignals(True)
            self._fence_action.setChecked(False)
            self._fence_action.blockSignals(False)
            return
        self._fence_active = False
        self._fence_start_screen = None
        self._fence_current_screen = None
        if self._fence_mouse_grabbed:
            try:
                vtk_widget = getattr(self.app, "vtk_widget", None)
                if vtk_widget is not None:
                    vtk_widget.releaseMouse()
            except Exception:
                pass
            self._fence_mouse_grabbed = False
        self._fence_remove_overlay()
        vtk_widget = getattr(self.app, "vtk_widget", None)
        if vtk_widget is not None:
            vtk_widget.removeEventFilter(self)
            vtk_widget.setCursor(Qt.ArrowCursor)
        if hasattr(self.app, "statusBar"):
            if cancel:
                self.app.statusBar().showMessage("Select by Fence cancelled", 2000)
            else:
                self.app.statusBar().clearMessage()
        self._fence_action.blockSignals(True)
        self._fence_action.setChecked(False)
        self._fence_action.blockSignals(False)

    def _fence_remove_overlay(self):
        if self._fence_actor is None:
            return
        try:
            vtk_widget = getattr(self.app, "vtk_widget", None)
            renderer = getattr(vtk_widget, "renderer", None)
            if renderer is not None:
                renderer.RemoveActor2D(self._fence_actor)
            if vtk_widget is not None:
                vtk_widget.GetRenderWindow().Render()
        except Exception:
            pass
        self._fence_actor = None

    def _fence_update_overlay(self, p0_vtk, p1_vtk):
        """Draw/refresh a cyan rubberband rectangle in VTK display coords.

        Removes any existing overlay actor and adds a fresh one in a single
        render pass to avoid the stutter of two back-to-back renders.
        """
        try:
            import vtk
            vtk_widget = getattr(self.app, "vtk_widget", None)
            renderer = getattr(vtk_widget, "renderer", None)
            if renderer is None:
                return

            # Remove old actor inline (no render yet)
            if self._fence_actor is not None:
                try:
                    renderer.RemoveActor2D(self._fence_actor)
                except Exception:
                    pass
                self._fence_actor = None

            x0, y0 = p0_vtk
            x1, y1 = p1_vtk
            # 5-point closed rectangle (same winding as _make_screen_polyline)
            pts_screen = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
            pts = vtk.vtkPoints()
            for sx, sy in pts_screen:
                pts.InsertNextPoint(float(sx), float(sy), 0.0)
            # Single vtkPolyLine — matches the pattern used by _make_screen_polyline
            poly_line = vtk.vtkPolyLine()
            poly_line.GetPointIds().SetNumberOfIds(len(pts_screen))
            for i in range(len(pts_screen)):
                poly_line.GetPointIds().SetId(i, i)
            cells = vtk.vtkCellArray()
            cells.InsertNextCell(poly_line)
            pd = vtk.vtkPolyData()
            pd.SetPoints(pts)
            pd.SetLines(cells)
            coord = vtk.vtkCoordinate()
            coord.SetCoordinateSystemToDisplay()
            mapper = vtk.vtkPolyDataMapper2D()
            mapper.SetInputData(pd)
            mapper.SetTransformCoordinate(coord)
            actor = vtk.vtkActor2D()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(0.2, 0.8, 1.0)   # cyan
            actor.GetProperty().SetLineWidth(2.0)
            renderer.AddActor2D(actor)
            self._fence_actor = actor
            # Single render — remove + add in one pass
            vtk_widget.GetRenderWindow().Render()
        except Exception:
            pass

    def _fence_screen_to_world_xy(self, vtk_x: float, vtk_y: float):
        """Convert a VTK display-coord point to world (x, y).  Returns None on error."""
        try:
            import vtk
            renderer = getattr(getattr(self.app, "vtk_widget", None), "renderer", None)
            if renderer is None:
                return None
            coord = vtk.vtkCoordinate()
            coord.SetCoordinateSystemToDisplay()
            coord.SetValue(float(vtk_x), float(vtk_y), 0.0)
            w = coord.GetComputedWorldValue(renderer)
            return float(w[0]), float(w[1])
        except Exception:
            return None

    def _fence_commit(self, p0_vtk, p1_vtk):
        """Select PRJ blocks whose polygons overlap the drawn fence rectangle."""
        w0 = self._fence_screen_to_world_xy(*p0_vtk)
        w1 = self._fence_screen_to_world_xy(*p1_vtk)
        if w0 is None or w1 is None:
            return

        wmin_x, wmax_x = min(w0[0], w1[0]), max(w0[0], w1[0])
        wmin_y, wmax_y = min(w0[1], w1[1]), max(w0[1], w1[1])

        # Ignore degenerate (single click with no drag)
        if (wmax_x - wmin_x) < 1e-6 or (wmax_y - wmin_y) < 1e-6:
            return

        # Overlap mode: block hits if any vertex is inside fence OR bboxes overlap
        matched: Set[str] = set()
        for block in self.prj_data:
            coords = block.get("boundary_coords", [])
            if not coords:
                continue
            hit = any(
                wmin_x <= bx <= wmax_x and wmin_y <= by <= wmax_y
                for bx, by in coords
            )
            if not hit:
                bxs = [c[0] for c in coords]
                bys = [c[1] for c in coords]
                hit = (
                    min(bxs) <= wmax_x and max(bxs) >= wmin_x
                    and min(bys) <= wmax_y and max(bys) >= wmin_y
                )
            if hit:
                lbl = block.get("label", "").strip().upper()
                if lbl:
                    matched.add(lbl)

        if not matched:
            if hasattr(self.app, "statusBar"):
                self.app.statusBar().showMessage(
                    "Select by Fence: no blocks found in selected area", 3000
                )
            return

        # Apply selection to the table
        from PySide6.QtCore import QItemSelectionModel
        self.table.clearSelection()
        sel_model = self.table.selectionModel()
        first_idx = None
        for trow in range(self.table.rowCount()):
            item = self.table.item(trow, 0)
            if item and item.text().strip().upper() in matched:
                idx = self.table.model().index(trow, 0)
                sel_model.select(idx, QItemSelectionModel.Select | QItemSelectionModel.Rows)
                if first_idx is None:
                    first_idx = idx
        if first_idx is not None:
            self.table.scrollTo(first_idx)

        # Highlight block boundary polygons in the viewport (MicroStation-style)
        self._highlight_fence_selected_blocks(matched)

        n = len(matched)
        if hasattr(self.app, "statusBar"):
            self.app.statusBar().showMessage(
                f"Select by Fence: {n} block{'s' if n != 1 else ''} selected", 4000
            )

    def eventFilter(self, obj, event):
        if obj is getattr(self, "search_box", None):
            if event.type() == QEvent.FocusIn:
                self._prj_search_focus_set(True)
            elif event.type() == QEvent.FocusOut:
                self._prj_search_focus_set(False)
            # Never consume the event. QLineEdit must receive its native focus,
            # caret, selection, clear-button, and keyboard handling.
            return False

        vtk_widget = getattr(self.app, "vtk_widget", None)

        if self._fence_active and obj is vtk_widget:
            et = event.type()

            if et == QEvent.KeyPress and event.key() == Qt.Key_Escape:
                self._fence_deactivate(cancel=True)
                return True

            if et == QEvent.MouseButtonPress:
                if event.button() == Qt.LeftButton:
                    pos = event.position() if hasattr(event, "position") else event.pos()
                    qt_x, qt_y = int(pos.x()), int(pos.y())
                    vtk_y = vtk_widget.height() - 1 - qt_y
                    self._fence_start_screen = (qt_x, vtk_y)
                    self._fence_current_screen = (qt_x, vtk_y)
                    # Grab mouse so release is received even if cursor moves over dialog
                    vtk_widget.grabMouse()
                    self._fence_mouse_grabbed = True
                    return True
                if event.button() == Qt.RightButton:
                    self._fence_deactivate(cancel=True)
                    return True

            if et == QEvent.MouseMove and self._fence_start_screen is not None:
                pos = event.position() if hasattr(event, "position") else event.pos()
                qt_x, qt_y = int(pos.x()), int(pos.y())
                vtk_y = vtk_widget.height() - 1 - qt_y
                self._fence_current_screen = (qt_x, vtk_y)
                self._fence_update_overlay(self._fence_start_screen, self._fence_current_screen)
                return True  # consume: prevent VTK camera orbit during drag

            if et == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                if self._fence_mouse_grabbed:
                    vtk_widget.releaseMouse()
                    self._fence_mouse_grabbed = False
                if self._fence_start_screen is not None:
                    pos = event.position() if hasattr(event, "position") else event.pos()
                    qt_x, qt_y = int(pos.x()), int(pos.y())
                    vtk_y = vtk_widget.height() - 1 - qt_y
                    end = (qt_x, vtk_y)
                    self._fence_remove_overlay()
                    self._fence_commit(self._fence_start_screen, end)
                    self._fence_deactivate(cancel=False)
                    return True

            return False

        if self._addb_active and obj is vtk_widget:
            et = event.type()

            if et == QEvent.KeyPress and event.key() == Qt.Key_Escape:
                self._addb_finish_drawing()
                return True

            if et == QEvent.MouseButtonPress:
                if event.button() == Qt.LeftButton:
                    pos = event.position() if hasattr(event, "position") else event.pos()
                    qt_x, qt_y = int(pos.x()), int(pos.y())
                    vtk_y = vtk_widget.height() - 1 - qt_y
                    self._addb_start_screen = (qt_x, vtk_y)
                    # Grab mouse so release is received even if cursor drifts over the
                    # floating PRJ dialog — prevents silently losing a drawn rectangle
                    vtk_widget.grabMouse()
                    self._addb_mouse_grabbed = True
                    return True
                if event.button() == Qt.RightButton:
                    self._addb_finish_drawing()
                    return True

            if et == QEvent.MouseMove and self._addb_start_screen is not None:
                pos = event.position() if hasattr(event, "position") else event.pos()
                qt_x, qt_y = int(pos.x()), int(pos.y())
                vtk_y = vtk_widget.height() - 1 - qt_y
                self._addb_update_drag_overlay(self._addb_start_screen, (qt_x, vtk_y))
                return True  # consume: prevent VTK camera orbit during drag

            if et == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                if self._addb_mouse_grabbed:
                    vtk_widget.releaseMouse()
                    self._addb_mouse_grabbed = False
                if self._addb_start_screen is not None:
                    pos = event.position() if hasattr(event, "position") else event.pos()
                    qt_x, qt_y = int(pos.x()), int(pos.y())
                    vtk_y = vtk_widget.height() - 1 - qt_y
                    end = (qt_x, vtk_y)
                    self._addb_remove_drag_overlay()
                    self._addb_commit_rect(self._addb_start_screen, end)
                    self._addb_start_screen = None
                    return True

            return False

        if self._editdef_active and obj is vtk_widget:
            et = event.type()

            if et == QEvent.KeyPress and event.key() == Qt.Key_Escape:
                self._editdef_deactivate(cancel=True)
                return True

            if et == QEvent.MouseButtonPress:
                if event.button() == Qt.LeftButton:
                    pos = event.position() if hasattr(event, "position") else event.pos()
                    qt_x, qt_y = int(pos.x()), int(pos.y())
                    vtk_y = vtk_widget.height() - 1 - qt_y
                    self._editdef_start_screen = (qt_x, vtk_y)
                    # Grab mouse so release is received even if cursor moves over dialog
                    vtk_widget.grabMouse()
                    self._editdef_mouse_grabbed = True
                    return True
                if event.button() == Qt.RightButton:
                    self._editdef_deactivate(cancel=True)
                    return True

            if et == QEvent.MouseMove and self._editdef_start_screen is not None:
                pos = event.position() if hasattr(event, "position") else event.pos()
                qt_x, qt_y = int(pos.x()), int(pos.y())
                vtk_y = vtk_widget.height() - 1 - qt_y
                self._editdef_update_drag_overlay(self._editdef_start_screen, (qt_x, vtk_y))
                return True  # consume: prevent VTK camera orbit during drag

            if et == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                if self._editdef_mouse_grabbed:
                    vtk_widget.releaseMouse()
                    self._editdef_mouse_grabbed = False
                if self._editdef_start_screen is not None:
                    pos = event.position() if hasattr(event, "position") else event.pos()
                    qt_x, qt_y = int(pos.x()), int(pos.y())
                    vtk_y = vtk_widget.height() - 1 - qt_y
                    end = (qt_x, vtk_y)
                    self._editdef_remove_drag_overlay()
                    self._editdef_commit(self._editdef_start_screen, end)
                    self._editdef_start_screen = None
                    self._editdef_deactivate(cancel=False)
                    return True

            return False

        return super().eventFilter(obj, event)

    # ── Add by Boundaries ────────────────────────────────────────────────────

    def _toggle_addb_mode(self, checked: bool):
        if checked:
            self._addb_activate()
        else:
            self._addb_finish_drawing()

    def _addb_activate(self):
        if self._fence_active:
            self._fence_deactivate(cancel=True)
        if self._editdef_active:
            self._editdef_deactivate(cancel=True)
        vtk_widget = getattr(self.app, "vtk_widget", None)
        if vtk_widget is None:
            self._addb_action.blockSignals(True)
            self._addb_action.setChecked(False)
            self._addb_action.blockSignals(False)
            return
        self._addb_active = True
        self._addb_start_screen = None
        self._addb_drag_actor = None
        vtk_widget.installEventFilter(self)
        vtk_widget.setCursor(Qt.CrossCursor)
        if hasattr(self.app, "statusBar"):
            self.app.statusBar().showMessage(
                "Add by Boundaries: drag rectangle(s) to define new blocks — "
                "Esc or right-click to finish",
                0,
            )

    def _addb_get_visible_snt_polygons(self):
        """SNT block polygons restricted to layers currently checked ON in the
        SNT/SBM attachment panel.

        app.snt_block_polygons holds every BL/GRID/BBOX polygon parsed from
        every loaded attachment, regardless of which layers the user has
        toggled visible. Add-by-Boundaries must only capture blocks from the
        layer(s) the user actually turned on (e.g. just the block layer),
        not also GRID/BBOX outlines that happen to still be loaded but hidden.
        """
        all_polys = getattr(self.app, "snt_block_polygons", None) or []
        if not all_polys:
            return []
        dlg = getattr(self.app, "snt_dialog", None)
        items = getattr(dlg, "snt_items", None) if dlg is not None else None
        if not items:
            # No layer-visibility info available (dialog never opened) —
            # fall back to the full list rather than silently dropping blocks.
            return all_polys

        by_filename = {}
        for item in items:
            try:
                by_filename[item.snt_path.name] = item
            except Exception:
                continue

        visible = []
        for blk in all_polys:
            fname = os.path.basename(blk.get("snt_filename") or "")
            item = by_filename.get(fname)
            if item is None:
                continue  # attachment no longer tracked/loaded — skip
            try:
                if not item.is_checked():
                    continue  # whole file toggled off
            except Exception:
                pass
            sel = getattr(item, "selected_layers", None)
            if sel is not None and blk.get("poly_layer") not in sel:
                continue  # this specific layer toggled off
            visible.append(blk)
        return visible

    def _add_blocks_from_snt(self):
        """Add every loaded SNT block polygon to the PRJ in one step, skipping
        any block whose grid_name already exists as a PRJ label."""
        snt_polygons = self._addb_get_visible_snt_polygons()
        if not snt_polygons:
            QMessageBox.information(
                self, "No SNT Data",
                "No SNT block polygons are loaded.\n\n"
                "Load an SNT file first using File → Load SNT."
            )
            return
        if not self.current_prj_path:
            QMessageBox.warning(
                self, "No PRJ Loaded",
                "Please load a PRJ file before adding blocks."
            )
            return
        if not os.path.isfile(self.current_prj_path):
            QMessageBox.critical(
                self, "PRJ File Not Found",
                f"The PRJ file no longer exists on disk:\n\n{self.current_prj_path}"
            )
            return

        existing_upper = {e.get("label", "").strip().upper() for e in self.prj_data}
        candidates = []
        for blk in snt_polygons:
            g = (blk.get("grid_name") or "").strip()
            pts = blk.get("points_2d") or []
            if not pts:
                continue
            if g.upper() in existing_upper:
                continue  # already in PRJ
            candidates.append((g, pts))

        if not candidates:
            QMessageBox.information(
                self, "Nothing to Add",
                "All SNT blocks are already present in the PRJ file."
            )
            return

        reply = QMessageBox.question(
            self, "Add All SNT Blocks",
            f"Add {len(candidates)} SNT block(s) to the PRJ file?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        suggested_prefix, suggested_first_number = self._detect_prefix_and_next_number()
        self._addb_commit_new_blocks(
            rects=[], prefix=suggested_prefix,
            first_number=suggested_first_number, snt_blocks=candidates
        )

    def _addb_deactivate(self, *, cancel: bool = False):
        """Stop drawing mode WITHOUT touching pending rectangles (caller decides)."""
        if not self._addb_active:
            self._addb_action.blockSignals(True)
            self._addb_action.setChecked(False)
            self._addb_action.blockSignals(False)
            return
        self._addb_active = False
        self._addb_start_screen = None
        if self._addb_mouse_grabbed:
            try:
                vtk_widget = getattr(self.app, "vtk_widget", None)
                if vtk_widget is not None:
                    vtk_widget.releaseMouse()
            except Exception:
                pass
            self._addb_mouse_grabbed = False
        self._addb_remove_drag_overlay()
        vtk_widget = getattr(self.app, "vtk_widget", None)
        if vtk_widget is not None:
            vtk_widget.removeEventFilter(self)
            vtk_widget.setCursor(Qt.ArrowCursor)
        if hasattr(self.app, "statusBar"):
            if cancel:
                self.app.statusBar().showMessage("Add by Boundaries cancelled", 2000)
            else:
                self.app.statusBar().clearMessage()
        self._addb_action.blockSignals(True)
        self._addb_action.setChecked(False)
        self._addb_action.blockSignals(False)
        if cancel:
            self._addb_clear_pending()

    def _addb_remove_drag_overlay(self):
        if self._addb_drag_actor is None:
            return
        try:
            vtk_widget = getattr(self.app, "vtk_widget", None)
            renderer = getattr(vtk_widget, "renderer", None)
            if renderer is not None:
                renderer.RemoveActor2D(self._addb_drag_actor)
            if vtk_widget is not None:
                vtk_widget.GetRenderWindow().Render()
        except Exception:
            pass
        self._addb_drag_actor = None

    def _addb_update_drag_overlay(self, p0_vtk, p1_vtk):
        """Live rubberband rectangle while dragging — orange, distinct from fence cyan."""
        try:
            import vtk
            vtk_widget = getattr(self.app, "vtk_widget", None)
            renderer = getattr(vtk_widget, "renderer", None)
            if renderer is None:
                return
            if self._addb_drag_actor is not None:
                try:
                    renderer.RemoveActor2D(self._addb_drag_actor)
                except Exception:
                    pass
                self._addb_drag_actor = None

            x0, y0 = p0_vtk
            x1, y1 = p1_vtk
            pts_screen = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
            pts = vtk.vtkPoints()
            for sx, sy in pts_screen:
                pts.InsertNextPoint(float(sx), float(sy), 0.0)
            poly_line = vtk.vtkPolyLine()
            poly_line.GetPointIds().SetNumberOfIds(len(pts_screen))
            for i in range(len(pts_screen)):
                poly_line.GetPointIds().SetId(i, i)
            cells = vtk.vtkCellArray()
            cells.InsertNextCell(poly_line)
            pd = vtk.vtkPolyData()
            pd.SetPoints(pts)
            pd.SetLines(cells)
            coord = vtk.vtkCoordinate()
            coord.SetCoordinateSystemToDisplay()
            mapper = vtk.vtkPolyDataMapper2D()
            mapper.SetInputData(pd)
            mapper.SetTransformCoordinate(coord)
            actor = vtk.vtkActor2D()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(1.0, 0.6, 0.0)   # orange — distinct from fence cyan
            actor.GetProperty().SetLineWidth(2.0)
            renderer.AddActor2D(actor)
            self._addb_drag_actor = actor
            vtk_widget.GetRenderWindow().Render()
        except Exception:
            pass

    def _addb_draw_polygon_outline(self, pts_2d, renderer):
        """Draw a closed orange polygon outline in the VTK renderer and return the actor.

        Returns None (and adds nothing to the renderer) if pts_2d is empty.
        """
        if not pts_2d:
            return None
        import vtk
        ring = list(pts_2d)
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        vpts = vtk.vtkPoints()
        for x, y in ring:
            vpts.InsertNextPoint(float(x), float(y), 1.0)
        n = vpts.GetNumberOfPoints()
        poly_line = vtk.vtkPolyLine()
        poly_line.GetPointIds().SetNumberOfIds(n)
        for i in range(n):
            poly_line.GetPointIds().SetId(i, i)
        cells = vtk.vtkCellArray()
        cells.InsertNextCell(poly_line)
        pd = vtk.vtkPolyData()
        pd.SetPoints(vpts)
        pd.SetLines(cells)
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(pd)
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(1.0, 0.6, 0.0)
        actor.GetProperty().SetLineWidth(3.0)
        actor.GetProperty().SetOpacity(1.0)
        try:
            actor.PickableOff()
        except Exception:
            pass
        renderer.AddActor(actor)
        return actor

    def _addb_commit_rect(self, p0_vtk, p1_vtk):
        """Convert the just-drawn screen rectangle to world coords.

        If SNT block polygons are loaded, detects every SNT block whose interior
        falls within the rectangle and creates one pending entry per SNT block
        (with its exact polygon shape).  If no SNT data is present, stores the
        rectangle itself as one pending boundary (original behaviour).
        """
        w0 = self._fence_screen_to_world_xy(*p0_vtk)
        w1 = self._fence_screen_to_world_xy(*p1_vtk)
        if w0 is None or w1 is None:
            return
        wmin_x, wmax_x = min(w0[0], w1[0]), max(w0[0], w1[0])
        wmin_y, wmax_y = min(w0[1], w1[1]), max(w0[1], w1[1])
        if (wmax_x - wmin_x) < 1e-6 or (wmax_y - wmin_y) < 1e-6:
            return  # degenerate (click without drag) — ignore

        snt_polygons = self._addb_get_visible_snt_polygons()

        vtk_widget = getattr(self.app, "vtk_widget", None)
        renderer = getattr(vtk_widget, "renderer", None)

        if snt_polygons:
            try:
                from gui.snt_attachment import _snt_polygon_interior_point
            except Exception:
                _snt_polygon_interior_point = None

            matched = []
            for blk in snt_polygons:
                pts_2d = blk.get("points_2d") or []
                if not pts_2d:
                    continue
                try:
                    if _snt_polygon_interior_point is not None:
                        ix, iy = _snt_polygon_interior_point(pts_2d)
                    else:
                        ix = sum(p[0] for p in pts_2d) / len(pts_2d)
                        iy = sum(p[1] for p in pts_2d) / len(pts_2d)
                except Exception:
                    continue
                if wmin_x <= ix <= wmax_x and wmin_y <= iy <= wmax_y:
                    matched.append((blk.get("grid_name", ""), pts_2d))

            if matched:
                # Avoid duplicates with already-pending SNT blocks
                pending_keys = {(g, tuple(tuple(p) for p in pts))
                                for g, pts in self._addb_pending_snt}
                added = 0
                for grid_name, pts_2d in matched:
                    key = (grid_name, tuple(tuple(p) for p in pts_2d))
                    if key in pending_keys:
                        continue
                    pending_keys.add(key)
                    self._addb_pending_snt.append((grid_name, pts_2d))
                    added += 1
                    if renderer is not None:
                        try:
                            actor = self._addb_draw_polygon_outline(pts_2d, renderer)
                            if actor is not None:
                                self._addb_pending_actors.append(actor)
                        except Exception as e:
                            print(f"⚠️ Add-by-Boundaries SNT outline failed: {e}")

                if renderer is not None and vtk_widget is not None:
                    try:
                        vtk_widget.GetRenderWindow().Render()
                    except Exception:
                        pass

                total = len(self._addb_pending_rects) + len(self._addb_pending_snt)
                if hasattr(self.app, "statusBar"):
                    self.app.statusBar().showMessage(
                        f"Add by Boundaries: {total} SNT block(s) detected "
                        f"({added} new in this fence) — draw more or Esc/right-click to finish",
                        0,
                    )
                return   # SNT path handled — do not also store plain rectangle

        # No SNT data (or no matches) — fall back to original rectangle behaviour
        self._addb_pending_rects.append((wmin_x, wmin_y, wmax_x, wmax_y))

        try:
            if renderer is not None:
                ring = [
                    (wmin_x, wmin_y), (wmax_x, wmin_y), (wmax_x, wmax_y),
                    (wmin_x, wmax_y), (wmin_x, wmin_y),
                ]
                actor = self._addb_draw_polygon_outline(ring, renderer)
                if actor is not None:
                    self._addb_pending_actors.append(actor)
                if vtk_widget is not None:
                    vtk_widget.GetRenderWindow().Render()
        except Exception as e:
            print(f"⚠️ Add-by-Boundaries pending outline failed: {e}")

        if hasattr(self.app, "statusBar"):
            n = len(self._addb_pending_rects)
            self.app.statusBar().showMessage(
                f"Add by Boundaries: {n} boundary{'ies' if n != 1 else 'y'} drawn — "
                "drag more, or Esc/right-click to finish",
                0,
            )

    def _addb_clear_pending(self):
        """Remove all persistent pending-boundary outlines and clear the lists."""
        self._addb_pending_rects = []
        self._addb_pending_snt = []
        if not self._addb_pending_actors:
            return
        try:
            vtk_widget = getattr(self.app, "vtk_widget", None)
            renderer = getattr(vtk_widget, "renderer", None)
            for actor in self._addb_pending_actors:
                try:
                    if renderer is not None:
                        renderer.RemoveActor(actor)
                except Exception:
                    pass
            self._addb_pending_actors = []
            if vtk_widget is not None:
                vtk_widget.GetRenderWindow().Render()
        except Exception:
            self._addb_pending_actors = []

    def _detect_prefix_and_next_number(self):
        """Inspect existing PRJ block labels to suggest a prefix + next number,
        matching MicroStation's auto-suggest in 'Add by boundaries' — e.g. if
        'drjh RADAR +000009/10/11' already exist, suggests prefix
        'drjh RADAR +' and first number 12.

        Returns (prefix: str, next_number: int).  Falls back to ("", 1) when
        no existing label ends in a trailing number.
        """
        import re
        pattern = re.compile(r"^(.*?)(\d+)$")
        prefix_counts: dict = {}
        prefix_max_num: dict = {}
        for block in self.prj_data:
            label = block.get("label", "").strip()
            m = pattern.match(label)
            if not m:
                continue
            prefix, num_str = m.group(1), m.group(2)
            try:
                num = int(num_str)
            except ValueError:
                continue
            prefix_counts[prefix] = prefix_counts.get(prefix, 0) + 1
            prefix_max_num[prefix] = max(prefix_max_num.get(prefix, 0), num)

        if not prefix_counts:
            return "", 1

        best_prefix = max(prefix_counts, key=lambda p: prefix_counts[p])
        return best_prefix, prefix_max_num[best_prefix] + 1

    def _addb_finish_drawing(self):
        """Esc / right-click / un-toggling the button.

        If at least one boundary was drawn, opens the naming dialog and
        creates the new block(s) on accept.  Always stops drawing mode.
        """
        pending_rects = list(self._addb_pending_rects)
        pending_snt = list(self._addb_pending_snt)
        self._addb_deactivate(cancel=False)   # stop listening, keep pending outlines for now

        total_pending = len(pending_rects) + len(pending_snt)
        if not total_pending:
            self._addb_clear_pending()
            return

        if not self.current_prj_path:
            QMessageBox.warning(
                self, "No PRJ Loaded",
                "Please load a PRJ file before adding blocks by boundaries."
            )
            self._addb_clear_pending()
            return
        if not os.path.isfile(self.current_prj_path):
            QMessageBox.critical(
                self, "PRJ File Not Found",
                f"The PRJ file no longer exists on disk:\n\n{self.current_prj_path}"
            )
            self._addb_clear_pending()
            return

        suggested_prefix, suggested_first_number = self._detect_prefix_and_next_number()

        dlg = AddBlocksByBoundariesDialog(
            total_pending, default_prefix=suggested_prefix,
            default_first_number=suggested_first_number, parent=self
        )
        self._addb_dialog_active = True
        try:
            if dlg.exec() != QDialog.Accepted:
                self._addb_clear_pending()
                return
        finally:
            self._addb_dialog_active = False

        prefix, first_number = dlg.get_values()
        if not prefix:
            QMessageBox.warning(self, "Missing Prefix", "Please enter a file prefix.")
            self._addb_clear_pending()
            return

        self._addb_commit_new_blocks(
            pending_rects, prefix, first_number, snt_blocks=pending_snt
        )
        self._addb_clear_pending()

    def _addb_commit_new_blocks(self, rects, prefix, first_number, snt_blocks=None):
        """Write one new Block section per drawn rectangle / SNT polygon to the .prj
        file in a single backup + atomic write (not one write per block).

        snt_blocks — list of (grid_name, pts_2d) from SNT polygon detection.
        """
        existing_upper = {e.get("label", "").strip().upper() for e in self.prj_data}
        new_blocks = []   # (label, filename, polygon)
        num = first_number

        # Rectangular boundaries (no SNT data involved)
        for rect in rects:
            minx, miny, maxx, maxy = rect
            if (maxx - minx) < 1e-6 or (maxy - miny) < 1e-6:
                continue
            while True:
                label = f"{prefix}{num:06d}"
                if label.strip().upper() not in existing_upper:
                    break
                num += 1
            existing_upper.add(label.strip().upper())
            filename = f"{label}.laz"
            polygon = [
                (minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny)
            ]
            new_blocks.append((label, filename, polygon))
            num += 1

        # SNT polygon blocks — use grid_name as label when available
        for grid_name, pts_2d in (snt_blocks or []):
            if not pts_2d:
                continue
            snt_label = grid_name.strip() if grid_name and grid_name.strip() else ""
            if snt_label and snt_label.upper() not in existing_upper:
                label = snt_label
            else:
                # SNT name conflicts or empty — generate a prefixed name
                while True:
                    label = f"{prefix}{num:06d}"
                    if label.strip().upper() not in existing_upper:
                        break
                    num += 1
                num += 1
            existing_upper.add(label.upper())
            filename = f"{label}.laz"
            # Close the polygon ring if needed
            polygon = list(pts_2d)
            if polygon and polygon[0] != polygon[-1]:
                polygon.append(polygon[0])
            new_blocks.append((label, filename, polygon))

        if not new_blocks:
            QMessageBox.warning(
                self, "Nothing to Add",
                "No valid (non-degenerate) boundaries were drawn."
            )
            return

        try:
            with open(self.current_prj_path, "r", encoding="utf-8") as fh:
                prj_content = fh.read()
        except Exception as exc:
            QMessageBox.critical(
                self, "Read Error", f"Could not read the PRJ file:\n\nError: {exc}"
            )
            return

        group_first = self._get_next_group_first(prj_content)
        if prj_content and not prj_content.endswith("\n"):
            prj_content += "\n"

        new_sections = [
            self._build_block_section_text(filename, polygon, group_first + i * 1_000_000)
            for i, (_, filename, polygon) in enumerate(new_blocks)
        ]
        new_content = prj_content + "".join(new_sections)

        # ── Write to backend working copy (real .prj untouched until Save) ────
        try:
            self._write_prj_to_working(new_content)
            print(f"  ✅ {len(new_blocks)} block(s) appended to backend working copy via Add by Boundaries")
        except Exception as exc:
            QMessageBox.critical(
                self, "Write Error",
                f"Failed to buffer the PRJ changes:\n\nError: {exc}\n\n"
                "The original file has NOT been modified."
            )
            return

        try:
            for label, filename, polygon in new_blocks:
                minx = min(p[0] for p in polygon)
                maxx = max(p[0] for p in polygon)
                miny = min(p[1] for p in polygon)
                maxy = max(p[1] for p in polygon)
                avg_x = (minx + maxx) / 2.0
                avg_y = (miny + maxy) / 2.0
                area_m2 = (maxx - minx) * (maxy - miny)
                coord_count = len(polygon) - 1 if (polygon and polygon[0] == polygon[-1]) else len(polygon)
                new_entry = {
                    "label": label,
                    "easting": f"{avg_x:.2f}",
                    "northing": f"{avg_y:.2f}",
                    "description": f"Boundary: {coord_count} points",
                    "boundary_coords": [(x, y) for x, y in polygon[:-1]],
                }
                new_prj_idx = len(self.prj_data)
                self.prj_data.append(new_entry)
                self._all_row_data.append(
                    (label, "No file in path", f"{area_m2:.2f}", None, None, new_prj_idx)
                )
            self._sort_header._sort_states = {}
            self._sort_header.viewport().update()
            self._repopulate_table(self._all_row_data)
            # Clear search box so newly added blocks are visible (search text may not
            # match the new generated names, which would hide them from the table)
            self.search_box.setText("")
            self.table.scrollToBottom()
            print(f"  ✅ In-memory state updated: {len(self.prj_data)} blocks")
        except Exception as exc:
            print(f"  ⚠️ Failed to refresh table after add-by-boundaries: {exc}")
            QMessageBox.warning(
                self, "Display Refresh Issue",
                "Blocks were added to the PRJ file successfully, but the table\n"
                "could not refresh automatically.\n\n"
                "Please close and reopen the PRJ Block Identifier to see the change."
            )
            return

        if hasattr(self.app, "statusBar"):
            self.app.statusBar().showMessage(
                f"Add by Boundaries: {len(new_blocks)} block(s) added to PRJ", 5000
            )

    # ── Edit Definition ──────────────────────────────────────────────────────

    def _toggle_editdef_mode(self, checked: bool):
        if checked:
            self._editdef_activate()
        else:
            self._editdef_deactivate(cancel=True)

    def _editdef_activate(self):
        selected_rows = self.table.selectionModel().selectedRows()
        if len(selected_rows) != 1:
            QMessageBox.warning(
                self, "Select One Block",
                "Please select exactly one block in the table before editing its definition."
            )
            self._editdef_action.blockSignals(True)
            self._editdef_action.setChecked(False)
            self._editdef_action.blockSignals(False)
            return

        item = self.table.item(selected_rows[0].row(), 0)
        prj_idx = item.data(Qt.UserRole) if item is not None else None
        if prj_idx is None or prj_idx < 0 or prj_idx >= len(self.prj_data):
            self._editdef_action.blockSignals(True)
            self._editdef_action.setChecked(False)
            self._editdef_action.blockSignals(False)
            return

        if self.prj_data[prj_idx].get("locked", False):
            label = self.prj_data[prj_idx].get("label", "?")
            QMessageBox.warning(
                self, "Block Locked",
                f"'{label}' is locked.\n\nUse Block → Release lock before editing its definition."
            )
            self._editdef_action.blockSignals(True)
            self._editdef_action.setChecked(False)
            self._editdef_action.blockSignals(False)
            return

        if self._fence_active:
            self._fence_deactivate(cancel=True)
        if self._addb_active:
            self._addb_deactivate(cancel=True)

        vtk_widget = getattr(self.app, "vtk_widget", None)
        if vtk_widget is None:
            self._editdef_action.blockSignals(True)
            self._editdef_action.setChecked(False)
            self._editdef_action.blockSignals(False)
            return

        self._editdef_target_prj_idx = prj_idx
        self._editdef_active = True
        self._editdef_start_screen = None
        self._editdef_drag_actor = None
        vtk_widget.installEventFilter(self)
        vtk_widget.setCursor(Qt.CrossCursor)

        label = self.prj_data[prj_idx].get("label", "?")
        if hasattr(self.app, "statusBar"):
            self.app.statusBar().showMessage(
                f"Edit Definition: drag a new boundary for '{label}' — Esc/right-click to cancel",
                0,
            )

    def _editdef_deactivate(self, *, cancel: bool = False):
        if not self._editdef_active:
            self._editdef_action.blockSignals(True)
            self._editdef_action.setChecked(False)
            self._editdef_action.blockSignals(False)
            return
        self._editdef_active = False
        self._editdef_start_screen = None
        self._editdef_target_prj_idx = None
        if self._editdef_mouse_grabbed:
            try:
                vtk_widget = getattr(self.app, "vtk_widget", None)
                if vtk_widget is not None:
                    vtk_widget.releaseMouse()
            except Exception:
                pass
            self._editdef_mouse_grabbed = False
        self._editdef_remove_drag_overlay()
        vtk_widget = getattr(self.app, "vtk_widget", None)
        if vtk_widget is not None:
            vtk_widget.removeEventFilter(self)
            vtk_widget.setCursor(Qt.ArrowCursor)
        if hasattr(self.app, "statusBar"):
            if cancel:
                self.app.statusBar().showMessage("Edit Definition cancelled", 2000)
            else:
                self.app.statusBar().clearMessage()
        self._editdef_action.blockSignals(True)
        self._editdef_action.setChecked(False)
        self._editdef_action.blockSignals(False)

    def _editdef_remove_drag_overlay(self):
        if self._editdef_drag_actor is None:
            return
        try:
            vtk_widget = getattr(self.app, "vtk_widget", None)
            renderer = getattr(vtk_widget, "renderer", None)
            if renderer is not None:
                renderer.RemoveActor2D(self._editdef_drag_actor)
            if vtk_widget is not None:
                vtk_widget.GetRenderWindow().Render()
        except Exception:
            pass
        self._editdef_drag_actor = None

    def _editdef_update_drag_overlay(self, p0_vtk, p1_vtk):
        """Live rubberband rectangle while dragging — magenta, distinct from
        fence cyan and add-by-boundaries orange."""
        try:
            import vtk
            vtk_widget = getattr(self.app, "vtk_widget", None)
            renderer = getattr(vtk_widget, "renderer", None)
            if renderer is None:
                return
            if self._editdef_drag_actor is not None:
                try:
                    renderer.RemoveActor2D(self._editdef_drag_actor)
                except Exception:
                    pass
                self._editdef_drag_actor = None

            x0, y0 = p0_vtk
            x1, y1 = p1_vtk
            pts_screen = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
            pts = vtk.vtkPoints()
            for sx, sy in pts_screen:
                pts.InsertNextPoint(float(sx), float(sy), 0.0)
            poly_line = vtk.vtkPolyLine()
            poly_line.GetPointIds().SetNumberOfIds(len(pts_screen))
            for i in range(len(pts_screen)):
                poly_line.GetPointIds().SetId(i, i)
            cells = vtk.vtkCellArray()
            cells.InsertNextCell(poly_line)
            pd = vtk.vtkPolyData()
            pd.SetPoints(pts)
            pd.SetLines(cells)
            coord = vtk.vtkCoordinate()
            coord.SetCoordinateSystemToDisplay()
            mapper = vtk.vtkPolyDataMapper2D()
            mapper.SetInputData(pd)
            mapper.SetTransformCoordinate(coord)
            actor = vtk.vtkActor2D()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(1.0, 0.0, 1.0)   # magenta
            actor.GetProperty().SetLineWidth(2.0)
            renderer.AddActor2D(actor)
            self._editdef_drag_actor = actor
            vtk_widget.GetRenderWindow().Render()
        except Exception:
            pass

    @staticmethod
    def _shoelace_area(coords) -> float:
        if len(coords) < 3:
            return 0.0
        n = len(coords)
        s = 0.0
        for i in range(n):
            x1, y1 = coords[i]
            x2, y2 = coords[(i + 1) % n]
            s += x1 * y2 - x2 * y1
        return abs(s) / 2.0

    def _editdef_commit(self, p0_vtk, p1_vtk):
        """Replace the target block's boundary with the just-drawn rectangle,
        after explicit user confirmation."""
        prj_idx = self._editdef_target_prj_idx
        if prj_idx is None or prj_idx < 0 or prj_idx >= len(self.prj_data):
            return

        w0 = self._fence_screen_to_world_xy(*p0_vtk)
        w1 = self._fence_screen_to_world_xy(*p1_vtk)
        if w0 is None or w1 is None:
            return
        wmin_x, wmax_x = min(w0[0], w1[0]), max(w0[0], w1[0])
        wmin_y, wmax_y = min(w0[1], w1[1]), max(w0[1], w1[1])
        if (wmax_x - wmin_x) < 1e-6 or (wmax_y - wmin_y) < 1e-6:
            return  # degenerate — ignore silently

        block = self.prj_data[prj_idx]
        label = block.get("label", "")
        old_area = self._shoelace_area(block.get("boundary_coords", []))
        new_area = (wmax_x - wmin_x) * (wmax_y - wmin_y)

        reply = QMessageBox.question(
            self, "Confirm Boundary Edit",
            (
                f"Replace the boundary for block '{label}'?\n\n"
                f"  Old area: {old_area:.2f} m²\n"
                f"  New area: {new_area:.2f} m²\n\n"
                "This permanently rewrites the polygon in the PRJ file.\n"
                "Changes are buffered in a backend working copy and only written\n"
                "to the real PRJ when you press 'Save project'."
            ),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if reply != QMessageBox.Yes:
            return

        if not self.current_prj_path or not os.path.isfile(self.current_prj_path):
            QMessageBox.critical(
                self, "PRJ File Not Found",
                "The PRJ file no longer exists on disk — cannot save the edit."
            )
            return

        new_polygon = [
            (wmin_x, wmin_y), (wmax_x, wmin_y), (wmax_x, wmax_y), (wmin_x, wmax_y)
        ]

        try:
            with open(self.current_prj_path, "r", encoding="utf-8") as fh:
                prj_content = fh.read()
        except Exception as exc:
            QMessageBox.critical(self, "Read Error", f"Could not read the PRJ file:\n\nError: {exc}")
            return

        new_content = self._replace_block_boundary_in_prj_text(prj_content, label, new_polygon)

        # ── Write to backend working copy (real .prj untouched until Save) ────
        try:
            self._write_prj_to_working(new_content)
            print(f"  ✅ Boundary replaced for: {label} (backend working copy)")
        except Exception as exc:
            QMessageBox.critical(
                self, "Write Error",
                f"Failed to buffer the PRJ changes:\n\nError: {exc}\n\n"
                "The original file has NOT been modified."
            )
            return

        try:
            avg_x = (wmin_x + wmax_x) / 2.0
            avg_y = (wmin_y + wmax_y) / 2.0
            self.prj_data[prj_idx]["boundary_coords"] = new_polygon
            self.prj_data[prj_idx]["easting"] = f"{avg_x:.2f}"
            self.prj_data[prj_idx]["northing"] = f"{avg_y:.2f}"

            for i, row in enumerate(self._all_row_data):
                if row[5] == prj_idx:
                    self._all_row_data[i] = (row[0], row[1], f"{new_area:.2f}", row[3], row[4], prj_idx)
                    break

            self._sort_header._sort_states = {}
            self._sort_header.viewport().update()
            self._repopulate_table(self._all_row_data)
            self._on_search_changed(self.search_box.text())
            print(f"  ✅ In-memory boundary updated for: {label}")
        except Exception as exc:
            print(f"  ⚠️ Failed to refresh table after edit: {exc}")
            QMessageBox.warning(
                self, "Display Refresh Issue",
                "The boundary was updated in the PRJ file successfully, but the table\n"
                "could not refresh automatically.\n\n"
                "Please close and reopen the PRJ Block Identifier to see the change."
            )
            return

        if hasattr(self.app, "statusBar"):
            self.app.statusBar().showMessage(
                f"Boundary updated for '{label}' — new area: {new_area:.2f} m²", 5000
            )

    # ── File menu: New / Save / Save As / Edit project information ─────────────

    def _new_project(self):
        """'New project...' — Project Information dialog first, then save location."""
        # 1. Show Project Information dialog with all defaults — matches MicroStation
        #    workflow where the form appears immediately on "New project".
        info_dlg = ProjectInformationDialog(fields=None, parent=self)
        info_dlg.setWindowTitle("Project Information — New Project")
        if info_dlg.exec() != QDialog.Accepted:
            return

        fields = info_dlg.get_fields()

        # 2. Now ask where to save the file
        new_path, _ = QFileDialog.getSaveFileName(
            self, "Save New PRJ File",
            self.current_directory or "",
            "PRJ Files (*.prj)"
        )
        if not new_path:
            return
        if not new_path.lower().endswith(".prj"):
            new_path += ".prj"

        header_text = ProjectInformationDialog.fields_to_header_text(fields)

        # 3. Write file atomically
        tmp_path = new_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(header_text)
            os.replace(tmp_path, new_path)
        except Exception as exc:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            QMessageBox.critical(
                self, "Create Failed",
                f"Could not create the new project file:\n\nError: {exc}"
            )
            return

        self.parse_prj_file(new_path)

    # ── Backend working-copy persistence ─────────────────────────────────────
    # Edits (Add/Delete/Rename/Merge/Boundary/Info) are written to a backend
    # side-file ("<name>.prj.working") instead of mutating the real .prj on
    # disk. The original .prj is only ever replaced when the user explicitly
    # presses "Save project" (which snapshots it to .prj.bak first). This
    # prevents accidental edits from destroying the source project and lets
    # the user discard pending changes by simply not saving.

    @property
    def _prj_working_path(self):
        if not self.current_prj_path:
            return None
        return self.current_prj_path + ".working"

    def _has_unsaved_working(self):
        """True if there is a backend working copy with pending (unsaved) edits."""
        wp = self._prj_working_path
        return bool(wp and os.path.isfile(wp))

    def _write_prj_to_working(self, new_content):
        """Atomically write the new PRJ text to the backend working side-file.

        Returns True on success. The real .prj is NEVER touched here.
        """
        wp = self._prj_working_path
        if not wp:
            return False
        tmp_path = wp + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(new_content)
            os.replace(tmp_path, wp)
            return True
        except Exception as exc:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            raise

    def _commit_working_to_real(self):
        """Replace the real .prj with the backend working copy.

        The current real .prj is first snapshotted to .prj.bak (matching the
        previous "Save" behaviour). Returns True on success. If there is no
        working copy, simply refreshes the .bak snapshot so "Save" still works
        as before for an already-clean project.
        """
        wp = self._prj_working_path
        real = self.current_prj_path
        if not real:
            return False

        # Snapshot the current real .prj to .prj.bak (keep existing behaviour)
        bak_path = real + ".bak"
        try:
            import shutil as _shutil
            _shutil.copy2(real, bak_path)
            print(f"  ✅ PRJ backup written: {bak_path}")
        except Exception as exc:
            print(f"  ⚠️ PRJ backup failed (non-fatal): {exc}")

        if self._has_unsaved_working():
            try:
                os.replace(wp, real)
                print(f"  ✅ PRJ file updated: {real}")
            except Exception as exc:
                QMessageBox.critical(
                    self, "Write Error",
                    f"Failed to save the project file:\n\n{real}\n\nError: {exc}",
                )
                return False
        else:
            print(f"  ✅ PRJ file unchanged (no pending edits): {real}")
        return True

    def _discard_working(self):
        """Drop any pending backend working copy (call when reloading/closing)."""
        wp = self._prj_working_path
        if wp and os.path.isfile(wp):
            try:
                os.remove(wp)
            except Exception:
                pass

    def _save_project(self):
        """'Save project' — commit all pending backend edits to the real .prj.

        Edits made via Add/Delete/Rename/Merge/Boundary/Info are buffered in a
        backend working side-file (".prj.working") so the original .prj is not
        modified until the user explicitly saves. This action snapshots the
        current .prj to .prj.bak first, then replaces it with the working copy.
        """
        if not self.current_prj_path:
            QMessageBox.warning(self, "No PRJ Loaded", "Please load a PRJ file first.")
            return
        if not os.path.isfile(self.current_prj_path):
            QMessageBox.critical(
                self, "PRJ File Not Found",
                f"The PRJ file no longer exists on disk:\n\n{self.current_prj_path}"
            )
            return

        committed = self._commit_working_to_real()
        if not committed:
            return

        if hasattr(self.app, "statusBar"):
            self.app.statusBar().showMessage("Project saved", 5000)

    def _save_project_as(self):
        """'Save project As...' — copy the current .prj file byte-for-byte
        (preserving every field, including GroupFirst/GroupCount which are
        not tracked in memory) to a new location, then switch the dialog
        to point at the new copy.
        """
        if not self.current_prj_path or not os.path.isfile(self.current_prj_path):
            QMessageBox.warning(self, "No PRJ Loaded", "Please load a PRJ file first.")
            return

        default_name = os.path.basename(self.current_prj_path)
        new_path, _ = QFileDialog.getSaveFileName(
            self, "Save Project As",
            os.path.join(self.current_directory or "", default_name),
            "PRJ Files (*.prj)"
        )
        if not new_path:
            return
        if not new_path.lower().endswith(".prj"):
            new_path += ".prj"
        if os.path.abspath(new_path) == os.path.abspath(self.current_prj_path):
            QMessageBox.warning(self, "Same File", "Choose a different file name or location.")
            return

        try:
            import shutil as _shutil
            # Prefer the backend working copy (which holds any pending edits);
            # fall back to the real .prj when there are no unsaved changes.
            src = self._prj_working_path if self._has_unsaved_working() else self.current_prj_path
            _shutil.copy2(src, new_path)
        except Exception as exc:
            QMessageBox.critical(self, "Save As Failed", f"Could not save project:\n\nError: {exc}")
            return

        if hasattr(self.app, "statusBar"):
            self.app.statusBar().showMessage(f"Saved as: {os.path.basename(new_path)}", 5000)
        self.parse_prj_file(new_path)

    def _edit_project_information(self):
        """'Edit project information...' — structured Project Information dialog.

        Reads the existing header, pre-populates all fields, writes back on OK.
        Block definitions are never touched.
        """
        if not self.current_prj_path or not os.path.isfile(self.current_prj_path):
            QMessageBox.warning(self, "No PRJ Loaded", "Please load a PRJ file first.")
            return

        try:
            with open(self.current_prj_path, "r", encoding="utf-8") as fh:
                content = fh.read()
        except Exception as exc:
            QMessageBox.critical(self, "Read Error", f"Could not read the PRJ file:\n\nError: {exc}")
            return

        header = self._extract_header_from_prj_text(content)
        existing_fields = ProjectInformationDialog.parse_header_fields(header)

        info_dlg = ProjectInformationDialog(fields=existing_fields, parent=self)
        info_dlg.setWindowTitle(
            f"Project Information — {os.path.basename(self.current_prj_path)}"
        )
        if info_dlg.exec() != QDialog.Accepted:
            return

        fields = info_dlg.get_fields()
        new_header = ProjectInformationDialog.fields_to_header_text(fields)
        if not new_header.endswith("\n"):
            new_header += "\n"

        # Re-read fresh to pick up any concurrent block changes
        try:
            with open(self.current_prj_path, "r", encoding="utf-8") as fh:
                fresh_content = fh.read()
        except Exception as exc:
            QMessageBox.critical(self, "Read Error", f"Could not re-read the PRJ file:\n\nError: {exc}")
            return

        lines = fresh_content.splitlines(keepends=True)
        block_start_idx = None
        for i, line in enumerate(lines):
            if line.strip().startswith("Block "):
                block_start_idx = i
                break
        blocks_text = "".join(lines[block_start_idx:]) if block_start_idx is not None else ""
        new_content = new_header + blocks_text

        # ── Write to backend working copy (real .prj untouched until Save) ────
        try:
            self._write_prj_to_working(new_content)
            print("  ✅ Project information updated (backend working copy)")
        except Exception as exc:
            QMessageBox.critical(
                self, "Write Error",
                f"Failed to buffer project information:\n\nError: {exc}\n\n"
                "The original file has NOT been modified."
            )
            return

        if hasattr(self.app, "statusBar"):
            self.app.statusBar().showMessage("Project information updated (unsaved)", 4000)

    def remove_prj_file(self):
        """Remove currently loaded PRJ file from the dialog."""
        if not self.current_prj_path:
            QMessageBox.warning(
                self,
                "No File Loaded",
                "No PRJ file is currently loaded."
            )
            return
        
        reply = QMessageBox.question(
            self,
            "Remove PRJ File",
            f"Remove loaded file:\n{os.path.basename(self.current_prj_path)}\n\n"
            "This will clear the block list but NOT delete the file from disk.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            # Clean up fence / add-by-boundaries / edit-definition state before clearing data
            self._fence_deactivate(cancel=False)
            self._fence_highlight_clear()
            self._addb_deactivate(cancel=True)
            self._editdef_deactivate(cancel=True)
            # Clear the table
            self.table.setRowCount(0)
            self.prj_data.clear()
            
            # Clear the stored path
            filename = os.path.basename(self.current_prj_path)
            self._discard_working()
            self.current_prj_path = None
            
            # Update status label
            self.dxf_label.setText("📁  No DXF loaded")
            # ✅ Clear all highlights
            if hasattr(self, '_highlight_actors') and self._highlight_actors:
                if hasattr(self.app, 'vtk_widget') and self.app.vtk_widget:
                    renderer = self.app.vtk_widget.renderer
                    for highlight_actor in self._highlight_actors:
                        try:
                            renderer.RemoveActor(highlight_actor)
                        except Exception:
                            pass
                    self.app.vtk_widget.GetRenderWindow().Render()
            
            # Clear tracking data
            self._highlight_actors = []
            self._highlighted_labels = []
            self._highlighted_blocks = {}

            # Cancel pending auto-clear timer
            self._highlight_identify_id += 1
            
            # Update status label
            self.dxf_label.setText("📁  No DXF loaded")

            self._missing_blocks_hidden = False
            if self._hide_missing_btn is not None:
                self._hide_missing_btn.setText("👁  Hide Missing Blocks")
                self._hide_missing_btn.setStyleSheet("")

            self._restore_bl_polygon_actors()

            print(f"✅ Removed PRJ file: {filename}")
            if hasattr(self.app, "statusBar"):
                self.app.statusBar().showMessage(f"PRJ file '{filename}' removed", 4000)

    def _toggle_missing_blocks_visibility(self):
        """Hide/show SNT block polygons + labels for missing-LAZ blocks."""
        missing_labels: Set[str] = {
            row[0]
            for row in self._all_row_data
            if row[1] == "No file in path"
        }

        if not missing_labels:
            QMessageBox.information(
                self, "No Missing Blocks",
                "All blocks in the table have associated LAZ files."
            )
            return

        self._missing_blocks_hidden = not self._missing_blocks_hidden

        if self._missing_blocks_hidden:
            for lbl in list(missing_labels):
                self.clear_highlight_for_block(lbl)
            self._last_hide_missing_labels = set(missing_labels)
            present_labels = {
                row[0]
                for row in self._all_row_data
                if row[1] != "No file in path"
            }
            self._filter_bl_polygon_actors(missing_labels, present_labels)
        else:
            self._last_hide_missing_labels = set()
            self._restore_bl_polygon_actors()

        self._update_hide_btn_style()

    def _restore_bl_polygon_actors(self):
        """
        Restore hidden original SNT block actors and remove PRJ-created
        Hide Missing Blocks overlay actors.

        Important:
        Hide Missing Blocks creates separate overlay actors with:
            filename = "_prj_boundary_overlay"
            cache layer = "_PRJ_BOUNDARY"

        Normal SNT remove will not catch those actors, so this function removes
        both tracked and orphan overlay actors safely.
        """
        # 1) Restore original actors that were hidden by Hide Missing Blocks
        for _aid, saved in list(getattr(self, "_snt_filter_saved_visibility", {}).items()):
            try:
                actor, vis = saved
                if actor is not None:
                    actor.SetVisibility(vis)
            except Exception:
                pass

        self._snt_filter_saved_visibility.clear()

        # 2) Get renderer safely
        renderer = None
        try:
            if getattr(self.app, "vtk_widget", None) is not None:
                renderer = self.app.vtk_widget.renderer
        except Exception:
            renderer = None

        # 3) Collect tracked overlay actors
        overlay_actors = []
        overlay_ids = set()

        for actor in list(getattr(self, "_snt_filter_overlay_actors", []) or []):
            if actor is None:
                continue
            overlay_actors.append(actor)
            overlay_ids.add(id(actor))

        # 4) Also collect orphan overlay actors from app stores
        #    This covers cases where the tracking list is stale/empty.
        for store_name in ("snt_actors", "dxf_actors"):
            for entry in list(getattr(self.app, store_name, []) or []):
                try:
                    filename = str(entry.get("filename", "") or "")
                    actors = entry.get("actors", []) or []

                    if filename == "_prj_boundary_overlay":
                        for actor in actors:
                            if actor is not None:
                                overlay_actors.append(actor)
                                overlay_ids.add(id(actor))
                        continue

                    for actor in actors:
                        if actor is not None and getattr(actor, "_is_filter_overlay", False):
                            overlay_actors.append(actor)
                            overlay_ids.add(id(actor))
                except Exception:
                    pass

        # 5) Remove overlay actors from renderer
        if renderer is not None:
            seen_actor_ids = set()
            for actor in overlay_actors:
                if actor is None:
                    continue
                aid = id(actor)
                if aid in seen_actor_ids:
                    continue
                seen_actor_ids.add(aid)

                try:
                    renderer.RemoveActor(actor)
                except Exception:
                    pass

        # 6) Remove overlay entries from app.snt_actors and app.dxf_actors
        for store_name in ("snt_actors", "dxf_actors"):
            store = getattr(self.app, store_name, None)
            if store is None:
                continue

            cleaned_store = []
            for entry in list(store):
                try:
                    filename = str(entry.get("filename", "") or "")
                    actors = entry.get("actors", []) or []

                    # Drop the dedicated PRJ overlay entry completely
                    if filename == "_prj_boundary_overlay":
                        continue

                    # Remove only overlay actors if they somehow got mixed into an entry
                    filtered_actors = [
                        actor for actor in actors
                        if actor is not None
                        and id(actor) not in overlay_ids
                        and not getattr(actor, "_is_filter_overlay", False)
                    ]

                    if len(filtered_actors) != len(actors):
                        entry = dict(entry)
                        entry["actors"] = filtered_actors

                    cleaned_store.append(entry)
                except Exception:
                    cleaned_store.append(entry)

            try:
                setattr(self.app, store_name, cleaned_store)
            except Exception:
                pass

        # 7) Remove overlay cache layer from SNT item actor_cache
        _PRJ_CACHE_LAYER = "_PRJ_BOUNDARY"
        snt_dlg = getattr(self.app, "snt_dialog", None)
        if snt_dlg is not None:
            for si in getattr(snt_dlg, "snt_items", []):
                try:
                    actor_cache = getattr(si, "actor_cache", None)
                    if actor_cache and _PRJ_CACHE_LAYER in actor_cache:
                        del actor_cache[_PRJ_CACHE_LAYER]
                except Exception:
                    pass

        # 8) Clear PRJ overlay tracking list
        self._snt_filter_overlay_actors.clear()

        # 9) Render
        self._safe_render_main_view()

    def clear_snt_filter_overlays_on_snt_removed(self):
        """
        Called from SNT remove / Clear All.

        Clears only PRJ Hide Missing Blocks overlay lines.
        Does NOT remove PRJ file.
        Does NOT clear PRJ table.
        """
        try:
            self._restore_bl_polygon_actors()

            self._missing_blocks_hidden = False
            self._last_hide_missing_labels = set()
            self._snt_filter_saved_visibility.clear()
            self._snt_filter_overlay_actors.clear()

            self._update_hide_btn_style()
            self._safe_render_main_view()

            print("✅ PRJ hide-missing overlays cleared after SNT remove")

        except Exception as exc:
            print(f"⚠️ Failed to clear PRJ hide-missing overlays after SNT remove: {exc}")

    def _update_hide_btn_style(self):
        """Sync button label + colour to current hide state. Safe to call after theme reload."""
        if self._hide_missing_btn is None:
            return
        if self._missing_blocks_hidden:
            self._hide_missing_btn.setText("👁  Show Missing Blocks")
            # Use object-name-specific rule so it wins over app stylesheet
            warning = ThemeColors.get("warning")
            self._hide_missing_btn.setStyleSheet(
                f"QPushButton#secondaryBtn {{ color: {warning}; border: 1px solid {warning}; }}"
            )
        else:
            self._hide_missing_btn.setText("👁  Hide Missing Blocks")
            self._hide_missing_btn.setStyleSheet("")

    def reapply_hide_state(self):
        """
        Call this after any grid / LAZ load completes.
        The load restores all DXF actors to visible=1, so we re-hide
        the missing-block actors if the button was active.
        """
        if not self._missing_blocks_hidden or not self._last_hide_missing_labels:
            return
        self._snt_filter_saved_visibility.clear()
        self._restore_bl_polygon_actors()
        present_labels = {
            row[0]
            for row in self._all_row_data
            if row[1] != "No file in path"
        }
        self._filter_bl_polygon_actors(self._last_hide_missing_labels, present_labels)
        self._update_hide_btn_style()
        print("  🔵 PRJ hide-state reapplied after grid load")

    def should_keep_actor_hidden(self, actor) -> bool:
        """
        Return True when PRJ Hide Missing Blocks currently owns this actor's
        visibility and it must not be revived by generic SNT/DXF restore code.
        """
        if actor is None or not self._missing_blocks_hidden:
            return False
        try:
            return id(actor) in getattr(self, "_snt_filter_saved_visibility", {})
        except Exception:
            return False

    def _filter_bl_polygon_actors(self, missing_labels, present_labels: set = None):
        """
        Hide missing-block labels and boundary rectangles.
        """
        from gui.snt_attachment import _is_block_label_layer, _snt_point_in_polygon_2d

        if not getattr(self.app, 'vtk_widget', None):
            return

        renderer = self.app.vtk_widget.renderer
        seen_bl: set = set()
        hidden_boundary_labels: set = set()
        snt_dlg = getattr(self.app, 'snt_dialog', None)

        if present_labels is None:
            present_labels = {
                row[0]
                for row in self._all_row_data
                if row[1] != "No file in path"
            }

        def _normalise_label(name: str) -> str:
            return str(name or "").replace(".laz", "").replace(".las", "").strip().upper()

        def _match_missing_label(name: str, label_pool=None):
            gn = _normalise_label(name)
            if not gn:
                return None
            pool = label_pool if label_pool is not None else missing_labels
            for lbl in pool:
                lb = _normalise_label(lbl)
                if gn == lb or lb in gn or gn in lb:
                    return lbl
            return None

        missing_poly_by_label: dict = {}
        for bd in self.prj_data or []:
            lbl = bd.get("label", "")
            coords = bd.get("boundary_coords", [])
            if lbl in missing_labels and len(coords) >= 3:
                missing_poly_by_label[lbl] = coords

        missing_poly_bounds: dict = {}
        for lbl, pts in missing_poly_by_label.items():
            try:
                xs = [float(p[0]) for p in pts]
                ys = [float(p[1]) for p in pts]
                missing_poly_bounds[lbl] = (
                    min(xs), max(xs), min(ys), max(ys)
                )
            except Exception:
                continue

        def _find_missing_label_for_point(cx, cy, label_pool=None):
            pool = label_pool if label_pool is not None else missing_poly_by_label.keys()
            for lbl in pool:
                pts = missing_poly_by_label.get(lbl)
                if not pts:
                    continue
                if _snt_point_in_polygon_2d(cx, cy, pts):
                    return lbl
            return None

        def _point_on_segment_2d(px, py, x1, y1, x2, y2, eps=0.10):
            dx = x2 - x1
            dy = y2 - y1
            seg_len2 = dx * dx + dy * dy
            if seg_len2 <= 1e-12:
                qx = px - x1
                qy = py - y1
                return (qx * qx + qy * qy) <= (eps * eps)

            t = ((px - x1) * dx + (py - y1) * dy) / seg_len2
            if t < 0.0:
                cx, cy = x1, y1
            elif t > 1.0:
                cx, cy = x2, y2
            else:
                cx = x1 + t * dx
                cy = y1 + t * dy

            ex = px - cx
            ey = py - cy
            return (ex * ex + ey * ey) <= (eps * eps)

        def _point_in_or_on_polygon(px, py, poly):
            if _snt_point_in_polygon_2d(px, py, poly):
                return True
            if not poly:
                return False
            try:
                xs = [p[0] for p in poly]
                ys = [p[1] for p in poly]
                diag = ((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2) ** 0.5
                edge_eps = max(0.10, diag * 1e-5)
            except Exception:
                edge_eps = 0.10

            n = len(poly)
            for i in range(n):
                x1, y1 = poly[i][0], poly[i][1]
                x2, y2 = poly[(i + 1) % n][0], poly[(i + 1) % n][1]
                if _point_on_segment_2d(px, py, x1, y1, x2, y2, edge_eps):
                    return True
            return False

        def _actor_centroid(a):
            try:
                b = a.GetBounds()
                return (b[0] + b[1]) / 2.0, (b[2] + b[3]) / 2.0
            except Exception:
                return None, None

        def _actor_bounds(a):
            try:
                b = a.GetBounds()
                return float(b[0]), float(b[1]), float(b[2]), float(b[3])
            except Exception:
                return None

        def _bbox_overlap_ratio(bounds_a, bounds_b):
            if not bounds_a or not bounds_b:
                return 0.0
            ax0, ax1, ay0, ay1 = bounds_a
            bx0, bx1, by0, by1 = bounds_b
            ix0 = max(ax0, bx0)
            ix1 = min(ax1, bx1)
            iy0 = max(ay0, by0)
            iy1 = min(ay1, by1)
            if ix1 <= ix0 or iy1 <= iy0:
                return 0.0
            inter = (ix1 - ix0) * (iy1 - iy0)
            aa = max((ax1 - ax0) * (ay1 - ay0), 1e-12)
            ab = max((bx1 - bx0) * (by1 - by0), 1e-12)
            return inter / min(aa, ab)

        def _extract_actor_points_2d(a, sample_limit=96):
            try:
                mapper = a.GetMapper()
                if mapper is None:
                    return []
                pd = mapper.GetInput()
                if pd is None:
                    return []
                pts = pd.GetPoints()
                if pts is None:
                    return []

                npts = int(pts.GetNumberOfPoints())
                if npts <= 0:
                    return []

                step = max(1, npts // max(1, int(sample_limit)))
                out = []
                idx = 0
                while idx < npts:
                    p = pts.GetPoint(idx)
                    out.append((float(p[0]), float(p[1])))
                    idx += step
                if out and (npts - 1) % step != 0:
                    p = pts.GetPoint(npts - 1)
                    out.append((float(p[0]), float(p[1])))
                return out
            except Exception:
                return []

        def _find_missing_label_for_actor(a, label_pool=None):
            pool = list(label_pool if label_pool is not None else missing_poly_by_label.keys())
            if not pool:
                return None

            # Fast path: centroid test.
            cx, cy = _actor_centroid(a)
            if cx is not None:
                matched = _find_missing_label_for_point(cx, cy, pool)
                if matched:
                    return matched

            bounds = _actor_bounds(a)
            sample_pts = _extract_actor_points_2d(a)

            for lbl in pool:
                poly = missing_poly_by_label.get(lbl)
                if not poly:
                    continue

                # Strong signal: actor bounds mostly overlap this PRJ polygon bounds.
                if _bbox_overlap_ratio(bounds, missing_poly_bounds.get(lbl)) >= 0.80:
                    return lbl

                # Robust signal for concave/long boundaries: actor poly points are
                # on/inside the missing PRJ boundary.
                if sample_pts:
                    hits = 0
                    for sx, sy in sample_pts:
                        if _point_in_or_on_polygon(sx, sy, poly):
                            hits += 1
                            if hits >= 2:
                                return lbl
            return None

        def _hide_actor(actor, matched_label=None):
            aid = id(actor)
            if aid in self._snt_filter_saved_visibility:
                return
            self._snt_filter_saved_visibility[aid] = (actor, int(actor.GetVisibility()))
            actor.SetVisibility(0)
            seen_bl.add(aid)
            if matched_label and not getattr(actor, "is_grid_label", False):
                hidden_boundary_labels.add(matched_label)

        # ── Phase A: direct grid_name match ─────────────────────────────────
        def _process_direct(a):
            aid = id(a)
            if aid in seen_bl or aid in self._snt_filter_saved_visibility:
                return
            gn = getattr(a, 'grid_name', None)
            matched_label = _match_missing_label(gn)
            if matched_label:
                _hide_actor(a, matched_label)

        for store in ('snt_actors', 'dxf_actors'):
            for sd in getattr(self.app, store, []):
                for a in sd.get('actors', []):
                    _process_direct(a)

        if snt_dlg:
            for si in getattr(snt_dlg, 'snt_items', []):
                for ln, actors in getattr(si, 'actor_cache', {}).items():
                    for a in actors:
                        _process_direct(a)

        print(
            f"  🔵 Phase A (grid_name direct): {len(seen_bl)} actors hidden "
            f"across {len(hidden_boundary_labels)} boundary label(s)"
        )

        # ── Phase A.5: block-label layer spatial check ───────────────────────
        if missing_poly_by_label:
            def _process_blk_layer(a, layer_name=""):
                aid = id(a)
                if aid in seen_bl or aid in self._snt_filter_saved_visibility:
                    return
                if getattr(a, 'is_grid_label', False):
                    return
                lyr = layer_name or getattr(a, '_naksha_snt_layer', '')
                if not _is_block_label_layer(lyr):
                    return
                pending_labels = {lbl for lbl in missing_poly_by_label if lbl not in hidden_boundary_labels}
                if not pending_labels:
                    return
                matched_label = _find_missing_label_for_actor(a, pending_labels)
                if matched_label:
                    _hide_actor(a, matched_label)

            for store in ('snt_actors', 'dxf_actors'):
                for sd in getattr(self.app, store, []):
                    for a in sd.get('actors', []):
                        _process_blk_layer(a)

            if snt_dlg:
                for si in getattr(snt_dlg, 'snt_items', []):
                    for ln, actors_in_cache in getattr(si, 'actor_cache', {}).items():
                        for a in actors_in_cache:
                            _process_blk_layer(a, ln)

            print(
                f"  🔵 Phase A.5 (block-label layer spatial): {len(seen_bl)} actors hidden "
                f"across {len(hidden_boundary_labels)} boundary label(s)"
            )

        # ── Phase B: spatial + layer fallback ───────────────────────────────
        # Only runs for boundary labels that remain unresolved after earlier phases.
        # Only hides actors whose centroid is inside a MISSING block's PRJ polygon,
        # AND whose layer is a recognised block-boundary layer.
        pending_boundary_labels = {
            lbl for lbl in missing_poly_by_label.keys()
            if lbl not in hidden_boundary_labels
        }
        if pending_boundary_labels and self.prj_data:
            print("  🔵 Unresolved boundary labels remain → spatial+layer fallback")

            _PRJ_BB_LAYERS = {
                "bl", "bbox", "bboxes", "blocks", "block",
                "block_boundary", "blocks_boundary",
                "grid", "grids", "block_boxes", "blockbox",
            }

            if not missing_poly_by_label:
                print("  ℹ️ No missing-block PRJ polygons — skipping fallback")
            else:
                block_boundary_layers: set = set()
                for pi in getattr(self.app, 'snt_block_polygons', []):
                    lyr = pi.get('poly_layer', '') or pi.get('layer', '')
                    if lyr:
                        block_boundary_layers.add(lyr)
                for store in ('snt_actors', 'dxf_actors'):
                    for sd in getattr(self.app, store, []):
                        for a in sd.get('actors', []):
                            lyr = getattr(a, '_naksha_snt_layer', '')
                            if lyr and lyr.lower() in _PRJ_BB_LAYERS:
                                block_boundary_layers.add(lyr)
                if snt_dlg:
                    for si in getattr(snt_dlg, 'snt_items', []):
                        for ln in getattr(si, 'actor_cache', {}):
                            if ln.lower() in _PRJ_BB_LAYERS:
                                block_boundary_layers.add(ln)

                def _process_spatial(a):
                    aid = id(a)
                    if aid in seen_bl or aid in self._snt_filter_saved_visibility:
                        return
                    if getattr(a, 'is_grid_label', False):
                        return
                    if getattr(a, 'grid_name', None) is not None:
                        return  # handled (or not missing) in Phase A
                    lyr = getattr(a, '_naksha_snt_layer', '')
                    # Layer-matched actors: standard check
                    if lyr in block_boundary_layers or lyr.lower() in _PRJ_BB_LAYERS:
                        matched_label = _find_missing_label_for_actor(a, pending_boundary_labels)
                        if matched_label:
                            _hide_actor(a, matched_label)
                        return
                    # No layer match — check if actor bounds are FULLY inside a missing PRJ polygon.
                    # This catches merged/batched geometry on large files where Phase 0 was skipped.
                    try:
                        b = a.GetBounds()
                        # Check all 4 corners of actor bounds — if all inside, it's the block rectangle
                        corners = [
                            (b[0], b[2]), (b[1], b[2]),
                            (b[0], b[3]), (b[1], b[3]),
                        ]
                        for lbl in list(pending_boundary_labels):
                            pts = missing_poly_by_label.get(lbl)
                            if not pts:
                                continue
                            if all(_snt_point_in_polygon_2d(cx2, cy2, pts) for cx2, cy2 in corners):
                                _hide_actor(a, lbl)
                                return
                    except Exception:
                        pass

                for store in ('snt_actors', 'dxf_actors'):
                    for sd in getattr(self.app, store, []):
                        for a in sd.get('actors', []):
                            _process_spatial(a)

                if snt_dlg:
                    for si in getattr(snt_dlg, 'snt_items', []):
                        for ln, actors in getattr(si, 'actor_cache', {}).items():
                            for a in actors:
                                _process_spatial(a)

                print(
                    f"  🔵 Phase B (spatial) result: {len(seen_bl)} actors hidden "
                    f"across {len(hidden_boundary_labels)} boundary label(s)"
                )

        # ════════════════════════════════════════════════════════════════════
        # PHASE C: Rebuild block-boundary outlines from PRJ data
        # Creates new VTK actors for ALL blocks using PRJ boundary_coords.
        # Present block outlines are visible; missing block outlines hidden.
        # Registered in actor_cache + snt_actors/dxf_actors for layer-system
        # compatibility.
        # ════════════════════════════════════════════════════════════════════
        if self.prj_data:
            visible_polys: list = []
            hidden_poly_count = 0

            for bd in self.prj_data:
                lbl = bd.get('label', '')
                coords = bd.get('boundary_coords', [])
                if len(coords) < 3:
                    continue
                if lbl in present_labels:
                    visible_polys.append(coords)
                elif lbl in missing_labels:
                    hidden_poly_count += 1
                else:
                    visible_polys.append(coords)

            print(f"  🔵 Phase C: {len(visible_polys)} visible, "
                  f"{hidden_poly_count} hidden (missing LAZ)")

            if visible_polys:
                try:
                    import vtk
                    import numpy as np
                    try:
                        from vtkmodules.util import numpy_support
                    except ImportError:
                        # Some vtk packaging variants don't expose vtkmodules.util.
                        # Avoid importing vtk.util directly (some linters/ENVs
                        # can't resolve it). Provide minimal compatible helpers
                        # that emulate numpy_support.numpy_to_vtk and
                        # numpy_to_vtkIdTypeArray using pure-VTK APIs.
                        numpy_support = None

                    all_pts: list = []
                    cell_data: list = []
                    pt_offset = 0

                    for poly in visible_polys:
                        ring = list(poly) + [poly[0]]
                        n = len(ring)
                        for x, y in ring:
                            all_pts.append([float(x), float(y), 0.0])
                        cell_data.append(n)
                        cell_data.extend(range(pt_offset, pt_offset + n))
                        pt_offset += n

                    pts_np = np.array(all_pts, dtype=np.float32)
                    cells_np = np.array(cell_data, dtype=np.int64)

                    vtk_pts = vtk.vtkPoints()
                    vtk_pts.SetData(numpy_support.numpy_to_vtk(pts_np, deep=True))

                    vtk_cells = vtk.vtkCellArray()
                    vtk_cells.ImportLegacyFormat(
                        numpy_support.numpy_to_vtkIdTypeArray(cells_np))

                    pd = vtk.vtkPolyData()
                    pd.SetPoints(vtk_pts)
                    pd.SetLines(vtk_cells)

                    mapper = vtk.vtkPolyDataMapper()
                    mapper.SetInputData(pd)
                    mapper.SetResolveCoincidentTopologyToPolygonOffset()
                    mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(-10000.0, -10000.0)

                    new_actor = vtk.vtkActor()
                    new_actor.SetMapper(mapper)
                    new_actor.GetProperty().SetColor(0.6, 0.4, 0.8)
                    new_actor.GetProperty().SetLineWidth(4.0)
                    new_actor.GetProperty().SetLighting(False)
                    new_actor.GetProperty().SetAmbient(1.0)
                    new_actor.GetProperty().SetOpacity(1.0)
                    new_actor._is_filter_overlay = True
                    new_actor._original_color = (153, 102, 204)

                    renderer.AddActor(new_actor)
                    self._snt_filter_overlay_actors.append(new_actor)

                    # Register in snt_actors/dxf_actors so _ensure_overlay_actors finds it
                    overlay_entry = {
                        "filename": "_prj_boundary_overlay",
                        "full_path": "",
                        "actors": [new_actor],
                        "bounds": None,
                    }
                    self.app.snt_actors.append(overlay_entry)
                    self.app.dxf_actors.append(overlay_entry)

                    # Register in actor_cache under dedicated layer name
                    _PRJ_CACHE_LAYER = "_PRJ_BOUNDARY"
                    if snt_dlg:
                        for si in getattr(snt_dlg, 'snt_items', []):
                            si.actor_cache.setdefault(_PRJ_CACHE_LAYER, []).append(new_actor)

                except Exception as e:
                    print(f"  ⚠️ Phase C rebuild failed: {e}")
                    import traceback
                    traceback.print_exc()

        unresolved_labels = [
            lbl for lbl in missing_poly_by_label
            if lbl not in hidden_boundary_labels
        ]
        if unresolved_labels:
            print(
                f"  ⚠️ Unresolved missing block boundaries still visible: {unresolved_labels}"
            )

        if not seen_bl and not unresolved_labels:
            print("  ℹ️ No block actors found to hide")

        self._safe_render_main_view()

    def _get_snt_laz_directories(self) -> list:
        """
        Return a deduplicated list of absolute directory paths that contain the
        LAZ files for the currently loaded SNT attachments.

        Logic:
          - Each SNT attachment in app.snt_attachments has a "full_path" key
            that holds the absolute path to the .snt file.
          - The LAZ files live in the SAME folder as the .snt file.
          - We collect the parent directory of every loaded SNT, deduplicate,
            and return the list so the table-population code can search only
            those directories for block LAZ files.

        This ensures:
          • If you loaded Dogana SNT  → only Dogana LAZ files are "found".
          • Blocks from a different area PRJ show "No file in path" correctly.
          • If a block is genuinely missing from the Dogana folder it also
            shows "No file in path".
        """
        dirs = []
        try:
            attachments = getattr(self.app, 'snt_attachments', []) or []
            for attachment in attachments:
                # "full_path" is set by SNTAttachmentDialog when the SNT is loaded
                full_path = attachment.get('full_path', '')
                if full_path:
                    parent = os.path.dirname(os.path.abspath(full_path))
                    if parent and parent not in dirs:
                        dirs.append(parent)
                        print(f"  📂 SNT LAZ dir: {parent}")
        except Exception as e:
            print(f"  ⚠️ _get_snt_laz_directories error: {e}")
        return dirs

    def get_laz_point_count(self, laz_path):
        """
        Get the total point count from a LAZ/LAS file.
        Returns point count or 0 if file not found/error.
        """
        try:
            from pathlib import Path
            
            file_path = Path(laz_path)
            
            # Check if file exists
            if not file_path.exists():
                print(f"  ⚠️ File not found: {laz_path}")
                return 0
            
            # Try to read point count using laspy
            try:
                import laspy
                with laspy.open(str(file_path)) as las_file:
                    point_count = las_file.header.point_count
                    print(f"  ✅ {file_path.name}: {point_count:,} points")
                    return point_count
            except ImportError:
                print("  ⚠️ laspy not installed - cannot read point count")
                return 0
            except Exception as e:
                print(f"  ⚠️ Failed to read {file_path.name}: {e}")
                return 0
                
        except Exception as e:
            print(f"  ❌ Error getting point count: {e}")
            return 0        
                        
    def calculate_grid_area(self, block_name):
        """
        Calculate approximate area of a grid block from DXF geometry.
        Returns area in square meters or 0 if cannot calculate.
        """
        try:
            # Get the block's polyline/rectangle from DXF
            # This is a simple approach - measure bounding box
            
            if not hasattr(self, 'dxf_blocks') or block_name not in self.dxf_blocks:
                return 0
            
            block_data = self.dxf_blocks[block_name]
            
            # Get all line/polyline coordinates for this block
            all_x = []
            all_y = []
            
            for entity in block_data.get('entities', []):
                if entity['type'] == 'line':
                    all_x.extend([entity['start'][0], entity['end'][0]])
                    all_y.extend([entity['start'][1], entity['end'][1]])
                elif entity['type'] == 'polyline':
                    for pt in entity['points']:
                        all_x.append(pt[0])
                        all_y.append(pt[1])
            
            if not all_x or not all_y:
                return 0
            
            # Calculate bounding box area
            width = max(all_x) - min(all_x)
            height = max(all_y) - min(all_y)
            area = width * height
            
            return area
            
        except Exception as e:
            print(f"  ⚠️ Error calculating area: {e}")
            return 0        
        
    def calculate_grid_area_from_prj(self, block_data):
        """
        Calculate exact polygon area from PRJ boundary coordinates using the
        Shoelace (Gauss's area) formula. Returns area in square metres.
        """
        try:
            coords = block_data.get('boundary_coords', [])
            if len(coords) < 3:
                print(f"  ⚠️ Not enough coords for area ({len(coords)} found), need >= 3")
                return 0

            # Shoelace formula: A = 0.5 * |sum(x_i * y_{i+1} - x_{i+1} * y_i)|
            n = len(coords)
            area = 0.0
            for k in range(n):
                x_i, y_i = coords[k]
                x_j, y_j = coords[(k + 1) % n]
                area += (x_i * y_j) - (x_j * y_i)

            area = abs(area) / 2.0
            print(f"  📐 Shoelace area for '{block_data.get('label','?')}': {area:.2f} m² ({n} vertices)")
            return area

        except Exception as e:
            print(f"  ⚠️ Error calculating area: {e}")
            return 0

    def closeEvent(self, event):
        """
        Hide the dialog instead of destroying it so the loaded PRJ state
        (blocks table, file path, polygon data) is preserved across open/close cycles.
        
        The window X button now behaves the same as the Close button — both hide.
        The dialog is only truly destroyed when the main app shuts down.
        
        NOTE: this is the ONLY closeEvent — the duplicate below was removed.
        """
        # ✅ FIX: intercept the close and hide instead, preserving all PRJ state.
        # Only allow true destruction when the app is quitting.
        app = getattr(self, 'app', None)
        app_is_closing = (app is not None and getattr(app, '_is_closing', False))

        if not app_is_closing:
            # Deactivate fence / add-by-boundaries / edit-definition modes so
            # the event filter is removed before hiding
            self._fence_deactivate(cancel=False)
            self._fence_highlight_clear()
            self._addb_deactivate(cancel=True)
            self._editdef_deactivate(cancel=True)
            event.ignore()
            self.hide()
            return

        # App is shutting down — allow true close and clean up references.
        try:
            if self._chip is not None:
                try:
                    self._chip.restore_requested.disconnect()
                    self._chip.hide()
                    self._chip.deleteLater()
                except Exception:
                    pass
                self._chip = None

            # Only clear the app reference on true shutdown, not on every hide.
            if app is not None and hasattr(app, 'block_identifier_dialog') \
                    and app.block_identifier_dialog is self:
                app.block_identifier_dialog = None

        except Exception:
            pass
        super().closeEvent(event)

def show_block_identifier_dialog(app):
    """Show the PRJ block identifier dialog (persistent reference).
    
    The dialog is never destroyed on close — it is hidden so that the loaded
    PRJ state (blocks table, polygon data, file path) survives across open/close
    cycles. Calling this function always brings the existing dialog back.
    """
    if hasattr(app, 'block_identifier_dialog') and app.block_identifier_dialog:
        try:
            dlg = app.block_identifier_dialog
            if dlg._is_minimized_to_chip:
                dlg._do_restore_from_chip()
            else:
                if dlg.windowState() & Qt.WindowMinimized:
                    dlg.showNormal()
                # ✅ FIX: show() works whether the dialog was hidden or just obscured.
                # Previously only raise_/activateWindow were called, which fail when
                # the dialog was hidden via hide() from the Close button.
                dlg.show()
                dlg.raise_()
                dlg.activateWindow()
            return dlg
        except Exception:
            pass

    # First open — create and store the persistent dialog.
    dialog = PRJBlockIdentifierDialog(app, parent=app)
    app.block_identifier_dialog = dialog
    dialog.show()
    return dialog
