# from __future__ import annotations

# import warnings
# import numpy as np

# try:
#     from PySide6.QtCore import Qt, QTimer
#     from PySide6.QtGui import QColor, QIcon, QPixmap
#     from PySide6.QtWidgets import (
#         QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
#         QDoubleSpinBox, QGroupBox, QFrame, QApplication, QSizePolicy,
#         QButtonGroup, QComboBox,
#     )
# except ImportError:
#     from PyQt5.QtCore import Qt, QTimer
#     from PyQt5.QtGui import QColor, QIcon, QPixmap
#     from PyQt5.QtWidgets import (
#         QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
#         QDoubleSpinBox, QGroupBox, QFrame, QApplication, QSizePolicy,
#         QButtonGroup, QComboBox,
#     )

# try:
#     from gui.classification_tools import _apply_classification
# except Exception:
#     _apply_classification = None

# try:
#     from gui.theme_manager import ThemeColors
# except Exception:
#     class ThemeColors:
#         @staticmethod
#         def get(k):
#             return ""

# try:
#     from gui.minimize_chip import MinimizableDialogMixin
# except Exception:
#     class MinimizableDialogMixin:
#         def _init_minimize_state(self): pass
#         def _cleanup_chip(self): pass


# def _get_parallel_dialog_style():
#     return f"""
# QDialog {{
#     background-color: {ThemeColors.get('bg_secondary')};
#     color: {ThemeColors.get('text_primary')};
#     border: 1px solid {ThemeColors.get('border_light')};
#     border-radius: 12px;
# }}
# QGroupBox {{
#     background-color: {ThemeColors.get('bg_primary')};
#     border: 1px solid {ThemeColors.get('border_light')};
#     border-radius: 10px;
#     margin-top: 15px;
#     padding-top: 18px;
#     padding-bottom: 10px;
#     font-weight: bold;
#     color: {ThemeColors.get('text_primary')};
#     font-size: 13px;
# }}
# QGroupBox::title {{
#     subcontrol-origin: margin;
#     left: 10px;
#     padding: 0 5px;
#     font-size: 13px;
#     text-transform: uppercase;
#     letter-spacing: 0.5px;
#     color: {ThemeColors.get('accent')};
# }}
# QLabel {{
#     color: {ThemeColors.get('text_primary')};
#     font-size: 13px;
#     font-weight: 500;
# }}
# QDoubleSpinBox {{
#     background-color: {ThemeColors.get('bg_input')};
#     color: {ThemeColors.get('text_primary')};
#     border: 1px solid {ThemeColors.get('border_light')};
#     border-radius: 6px;
#     padding: 6px 10px;
#     min-height: 28px;
#     font-size: 13px;
# }}
# QDoubleSpinBox:hover {{
#     border: 1px solid {ThemeColors.get('accent')};
#     background-color: {ThemeColors.get('bg_button_hover')};
# }}
# QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
#     width: 0px;
#     border: none;
# }}
# QPushButton {{
#     background-color: {ThemeColors.get('bg_button')};
#     color: {ThemeColors.get('text_primary')};
#     border: 1px solid {ThemeColors.get('border_light')};
#     border-radius: 6px;
#     padding: 8px 16px;
#     font-weight: bold;
#     font-size: 13px;
#     min-height: 28px;
# }}
# QPushButton:hover {{
#     background-color: {ThemeColors.get('bg_button_hover')};
#     border-color: {ThemeColors.get('accent')};
# }}
# QPushButton:pressed {{
#     background-color: {ThemeColors.get('accent')};
#     color: {ThemeColors.get('text_on_active')};
# }}
# QPushButton:disabled {{
#     color: {ThemeColors.get('text_primary')};
#     background-color: {ThemeColors.get('bg_button')};
#     border-color: {ThemeColors.get('border_light')};
# }}
# QFrame {{
#     color: {ThemeColors.get('border_light')};
# }}
# QPushButton[sideBtn="true"] {{
#     background-color: {ThemeColors.get('bg_button')};
#     color: {ThemeColors.get('text_primary')};
#     border: 1px solid {ThemeColors.get('border_light')};
#     border-radius: 6px;
#     padding: 6px 4px;
#     font-size: 12px;
#     font-weight: bold;
#     min-width: 58px;
#     min-height: 28px;
# }}
# QPushButton[sideBtn="true"]:hover {{
#     background-color: {ThemeColors.get('bg_button_hover')};
#     border-color: {ThemeColors.get('accent')};
# }}
# QPushButton[sideBtn="true"][checked="true"] {{
#     background-color: {ThemeColors.get('accent')};
#     color: {ThemeColors.get('text_on_active')};
#     border-color: {ThemeColors.get('accent')};
# }}
# """


# class ParallelToolDialog(MinimizableDialogMixin, QDialog):
#     """
#     Dialog for the Parallel tool workflow:
#       1. Click 'Select Figure' → pick a drawn figure on the canvas
#       2. Enter distance (m)
#       3. Click 'Apply' → parallel figure is generated
#     """

#     def __init__(self, digitizer, parent=None):
#         super().__init__(None, Qt.Window)
#         self._init_minimize_state()
#         self.digitizer = digitizer
#         self.app = digitizer.app
#         self._selected_drawings = []        # list of selected drawing dicts
#         self._selection_saved_props = {}    # actor → (orig_color, orig_width)
#         self._select_mode = False
#         self._old_left_press = None
#         self._old_mouse_move = None
#         self._hover_highlight_actor = None
#         self._hover_orig_color = None
#         self._hover_orig_width = None
#         self._selected_side = "left"   # default offset side
#         self._selected_drawing = None
#         self._parallel_left_tolerance = 0.20
#         self._parallel_right_tolerance = 0.20

#         self.setWindowTitle("Parallel Tool")
#         self.setWindowFlags(
#             Qt.Dialog
#             | Qt.WindowCloseButtonHint
#             | Qt.WindowMaximizeButtonHint
#             | Qt.WindowMinimizeButtonHint
#             | Qt.WindowStaysOnTopHint
#         )
#         self.setAttribute(Qt.WA_QuitOnClose, False)
#         self.setMinimumWidth(360)
#         self.resize(420, 540)
#         self.setStyleSheet(_get_parallel_dialog_style())
#         self._build_ui()

#     # ── UI ──────────────────────────────────────────────────────────────
#     def _build_ui(self):
#         root = QVBoxLayout(self)
#         root.setContentsMargins(18, 5, 18, 10)
#         root.setSpacing(12)

#         # ── Section 1: Select Figure ────────────────────────────────────
#         sec1 = QGroupBox("Figure")
#         sec1_layout = QVBoxLayout(sec1)
#         sec1_layout.setContentsMargins(15, 10, 15, 10)
#         sec1_layout.setSpacing(8)

#         self.select_btn = QPushButton("Select Figure")
#         self.select_btn.setCursor(Qt.PointingHandCursor)
#         self.select_btn.clicked.connect(self._on_select_figure)
#         sec1_layout.addWidget(self.select_btn)

#         self.figure_label = QLabel("No figure selected")
#         self.figure_label.setWordWrap(True)
#         self.figure_label.setStyleSheet(
#             f"color: {ThemeColors.get('text_secondary')}; font-size: 11px; font-weight: 500; padding: 4px 0;"
#         )
#         sec1_layout.addWidget(self.figure_label)

#         root.addWidget(sec1)

#         # ── Section 2: Side Selection ───────────────────────────────────
#         sec_side = QGroupBox("Side")
#         sec_side_layout = QVBoxLayout(sec_side)
#         sec_side_layout.setContentsMargins(15, 10, 15, 10)
#         sec_side_layout.setSpacing(6)

#         # Top row: just "Top" centred
#         top_row = QHBoxLayout()
#         top_row.setSpacing(6)
#         self._btn_top = self._make_side_btn("▲  Top")
#         top_row.addStretch()
#         top_row.addWidget(self._btn_top)
#         top_row.addStretch()
#         sec_side_layout.addLayout(top_row)

#         # Middle row: Left  [diagram]  Right
#         mid_row = QHBoxLayout()
#         mid_row.setSpacing(6)
#         self._btn_left  = self._make_side_btn("◀  Left")
#         self._btn_right = self._make_side_btn("Right  ▶")

#         # Small visual indicator in the centre
#         centre_lbl = QLabel("┼")
#         centre_lbl.setAlignment(Qt.AlignCenter)
#         centre_lbl.setStyleSheet(
#             f"color: {ThemeColors.get('text_secondary')}; font-size: 18px; min-width: 28px;"
#         )
#         mid_row.addWidget(self._btn_left)
#         mid_row.addStretch()
#         mid_row.addWidget(centre_lbl)
#         mid_row.addStretch()
#         mid_row.addWidget(self._btn_right)
#         sec_side_layout.addLayout(mid_row)

#         # Bottom row: just "Bottom" centred
#         bot_row = QHBoxLayout()
#         bot_row.setSpacing(6)
#         self._btn_bottom = self._make_side_btn("▼  Bottom")
#         bot_row.addStretch()
#         bot_row.addWidget(self._btn_bottom)
#         bot_row.addStretch()
#         sec_side_layout.addLayout(bot_row)

#         # Hint label
#         self._side_hint = QLabel("Offset side: Left  (for closed shapes: outward)")
#         self._side_hint.setWordWrap(True)
#         self._side_hint.setStyleSheet(
#             f"color: {ThemeColors.get('text_secondary')}; font-size: 11px; padding: 2px 0;"
#         )
#         sec_side_layout.addWidget(self._side_hint)

#         # Button group for mutual exclusion
#         self._side_group = QButtonGroup(self)
#         self._side_group.setExclusive(True)
#         for btn in (self._btn_left, self._btn_right, self._btn_top, self._btn_bottom):
#             self._side_group.addButton(btn)
#             btn.clicked.connect(self._on_side_selected)

#         # Default: Left
#         self._set_side_active(self._btn_left, "left")

#         root.addWidget(sec_side)
#         # Keep the workflow compact; the old side pad is now collapsed into
#         # the compact side dropdown inside the classify block.
#         sec_side.hide()

#         # ── Section 3: Distance + Apply ─────────────────────────────────
#         sec2 = QGroupBox("Offset")
#         sec2_layout = QVBoxLayout(sec2)
#         sec2_layout.setContentsMargins(15, 10, 15, 10)
#         sec2_layout.setSpacing(8)

#         dist_row = QHBoxLayout()
#         dist_row.setSpacing(8)
#         dist_label = QLabel("Distance:")
#         dist_label.setStyleSheet(
#             f"color: {ThemeColors.get('text_primary')}; font-size: 12px;"
#         )
#         dist_row.addWidget(dist_label)

#         self.distance_spin = QDoubleSpinBox()
#         self.distance_spin.setDecimals(2)
#         self.distance_spin.setRange(0.01, 9999.99)
#         self.distance_spin.setSingleStep(10)
#         self.distance_spin.setValue(10)
#         self.distance_spin.setSuffix(" m")
#         dist_row.addWidget(self.distance_spin, 1)
#         sec2_layout.addLayout(dist_row)

#         # Separator
#         line = QFrame()
#         line.setFrameShape(QFrame.HLine)
#         sec2_layout.addWidget(line)

#         action_row = QHBoxLayout()
#         action_row.setSpacing(8)

#         # Apply button
#         self.apply_btn = QPushButton("Apply")
#         self.apply_btn.setCursor(Qt.PointingHandCursor)
#         self.apply_btn.setEnabled(False)
#         self.apply_btn.clicked.connect(self._on_apply)
#         action_row.addWidget(self.apply_btn, 1)

#         self.clear_btn = QPushButton("Clear")
#         self.clear_btn.setCursor(Qt.PointingHandCursor)
#         self.clear_btn.clicked.connect(self._on_clear)
#         action_row.addWidget(self.clear_btn, 1)

#         sec2_layout.addLayout(action_row)

#         root.addWidget(sec2)

#         # â”€â”€ Section 4: Classify Band â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
#         sec_class = QGroupBox("Classify Band")
#         sec_class_layout = QVBoxLayout(sec_class)
#         sec_class_layout.setContentsMargins(15, 10, 15, 10)
#         sec_class_layout.setSpacing(8)

#         band_note = QLabel("Use the selected line/polyline to classify points in a world-space XY band.")
#         band_note.setWordWrap(True)
#         band_note.setStyleSheet(
#             f"color: {ThemeColors.get('text_secondary')}; font-size: 11px; padding: 2px 0;"
#         )
#         sec_class_layout.addWidget(band_note)

#         mode_row = QHBoxLayout()
#         mode_row.setSpacing(8)
#         mode_row.addWidget(QLabel("Mode:"))
#         self.band_mode_combo = QComboBox()
#         self.band_mode_combo.addItem("Symmetric (total width)", "symmetric")
#         self.band_mode_combo.addItem("Asymmetric (above / below)", "asymmetric")
#         self.band_mode_combo.currentIndexChanged.connect(self._sync_band_mode_ui)
#         mode_row.addWidget(self.band_mode_combo, 1)
#         sec_class_layout.addLayout(mode_row)

#         side_row = QHBoxLayout()
#         side_row.setSpacing(8)
#         side_row.addWidget(QLabel("Side:"))
#         self.band_side_combo = QComboBox()
#         self.band_side_combo.addItem("Left", "left")
#         self.band_side_combo.addItem("Right", "right")
#         self.band_side_combo.addItem("Top", "top")
#         self.band_side_combo.addItem("Bottom", "bottom")
#         side_row.addWidget(self.band_side_combo, 1)
#         sec_class_layout.addLayout(side_row)

#         tol_grid = QHBoxLayout()
#         tol_grid.setSpacing(8)
#         self.left_tol_label = QLabel("Left tolerance:")
#         tol_grid.addWidget(self.left_tol_label)
#         self.left_tol_spin = QDoubleSpinBox()
#         self.left_tol_spin.setDecimals(3)
#         self.left_tol_spin.setRange(0.0, 9999.0)
#         self.left_tol_spin.setSingleStep(0.05)
#         self.left_tol_spin.setSuffix(" m")
#         self.left_tol_spin.setValue(0.20)
#         tol_grid.addWidget(self.left_tol_spin, 1)
#         sec_class_layout.addLayout(tol_grid)

#         tol_grid2 = QHBoxLayout()
#         tol_grid2.setSpacing(8)
#         self.right_tol_label = QLabel("Right tolerance:")
#         tol_grid2.addWidget(self.right_tol_label)
#         self.right_tol_spin = QDoubleSpinBox()
#         self.right_tol_spin.setDecimals(3)
#         self.right_tol_spin.setRange(0.0, 9999.0)
#         self.right_tol_spin.setSingleStep(0.05)
#         self.right_tol_spin.setSuffix(" m")
#         self.right_tol_spin.setValue(0.20)
#         tol_grid2.addWidget(self.right_tol_spin, 1)
#         sec_class_layout.addLayout(tol_grid2)

#         class_row = QHBoxLayout()
#         class_row.setSpacing(8)
#         self.from_class_combo = QComboBox()
#         self.to_class_combo = QComboBox()
#         class_row.addWidget(QLabel("From class:"))
#         class_row.addWidget(self.from_class_combo, 1)
#         sec_class_layout.addLayout(class_row)

#         class_row2 = QHBoxLayout()
#         class_row2.setSpacing(8)
#         class_row2.addWidget(QLabel("To class:"))
#         class_row2.addWidget(self.to_class_combo, 1)
#         sec_class_layout.addLayout(class_row2)

#         self.classify_btn = QPushButton("Classify Points")
#         self.classify_btn.setCursor(Qt.PointingHandCursor)
#         self.classify_btn.clicked.connect(self._on_classify_band)
#         sec_class_layout.addWidget(self.classify_btn)

#         self.classify_status = QLabel("No drawing selected")
#         self.classify_status.setWordWrap(True)
#         self.classify_status.setStyleSheet(
#             f"color: {ThemeColors.get('text_secondary')}; font-size: 11px; padding: 2px 0;"
#         )
#         sec_class_layout.addWidget(self.classify_status)

#         self._populate_class_dropdowns()
#         self._load_parallel_settings()
#         self._sync_band_mode_ui()
#         self._sync_band_side_ui()

#         root.addWidget(sec_class)
#         root.addStretch()

#     # ── Side Selection helpers ───────────────────────────────────────────
#     def _make_side_btn(self, label: str) -> QPushButton:
#         btn = QPushButton(label)
#         btn.setProperty("sideBtn", "true")
#         btn.setProperty("checked", "false")
#         btn.setCursor(Qt.PointingHandCursor)
#         btn.setCheckable(False)   # we manage visuals manually via property
#         return btn

#     def _set_side_active(self, active_btn: QPushButton, side: str):
#         """Mark one button as active and refresh all side-button styles."""
#         self._selected_side = side
#         for btn, s in (
#             (self._btn_left,   "left"),
#             (self._btn_right,  "right"),
#             (self._btn_top,    "top"),
#             (self._btn_bottom, "bottom"),
#         ):
#             is_active = (s == side)
#             btn.setProperty("checked", "true" if is_active else "false")
#             # Force Qt to re-evaluate the stylesheet
#             btn.style().unpolish(btn)
#             btn.style().polish(btn)

#         # Update hint text
#         hints = {
#             "left":   "Offset side: Left  (for closed shapes: outward)",
#             "right":  "Offset side: Right  (for closed shapes: inward)",
#             "top":    "Offset side: Top / Up  (for closed shapes: outward)",
#             "bottom": "Offset side: Bottom / Down  (for closed shapes: outward)",
#         }
#         self._side_hint.setText(hints.get(side, ""))

#     def _on_side_selected(self):
#         """Called when any side button is clicked."""
#         sender = self.sender()
#         mapping = {
#             id(self._btn_left):   "left",
#             id(self._btn_right):  "right",
#             id(self._btn_top):    "top",
#             id(self._btn_bottom): "bottom",
#         }
#         side = mapping.get(id(sender), "left")
#         self._set_side_active(sender, side)

#     # ── Select Figure ───────────────────────────────────────────────────
#     def _on_select_figure(self):
#         """Enter multi-selection mode; each canvas click adds one figure."""
#         if self._select_mode:
#             return

#         self._select_mode = True
#         self.select_btn.setText("Click figures … (press button again to finish)")
#         self.select_btn.clicked.disconnect(self._on_select_figure)
#         self.select_btn.clicked.connect(self._on_finish_selection)

#         # Give VTK a moment to regain focus, then install the observer
#         QTimer.singleShot(150, self._install_selection_observer)

#     def _install_selection_observer(self):
#         """Install a one-shot LeftButtonPress observer for figure picking."""
#         if not self._select_mode:
#             return
#         self._remove_selection_observer()
#         interactor = self.digitizer.interactor
#         self._old_left_press = interactor.AddObserver(
#             "LeftButtonPressEvent", self._on_figure_picked, 1.0
#         )
#         try:
#             interactor.SetFocus()
#         except Exception:
#             pass

#     def _on_finish_selection(self):
#         """User pressed the button again — exit selection mode."""
#         self._select_mode = False
#         self._remove_selection_observer()
#         self._clear_hover_highlight()
#         try:
#             self.select_btn.clicked.disconnect(self._on_finish_selection)
#         except Exception:
#             pass
#         self.select_btn.clicked.connect(self._on_select_figure)
#         self.select_btn.setText("Select Figure")
#         self._update_figure_label()
#         self.raise_()
#         self.activateWindow()

#     def _on_figure_picked(self, obj, evt):
#         """Add the clicked figure to the selection; stay in selection mode."""
#         interactor = self.digitizer.interactor
#         x, y = interactor.GetEventPosition()
#         drawing = self._pick_drawing_at(x, y)
#         self._remove_selection_observer()

#         if drawing is None or drawing.get("type") == "text":
#             # Nothing useful — stay in selection mode
#             self._clear_hover_highlight()
#             QTimer.singleShot(100, self._install_selection_observer)
#             return

#         if any(d is drawing for d in self._selected_drawings):
#             # Already selected — skip, stay in selection mode
#             self._clear_hover_highlight()
#             QTimer.singleShot(100, self._install_selection_observer)
#             return

#         self._selected_drawings.append(drawing)
#         self._selected_drawing = drawing
#         self._clear_hover_highlight()
#         self._highlight_drawing(drawing)
#         self._update_figure_label()
#         self.apply_btn.setEnabled(True)
#         self.classify_btn.setEnabled(True)

#         # Reinstall observer to accept the next pick
#         QTimer.singleShot(100, self._install_selection_observer)

#     def _pick_drawing_at(self, x, y):
#         """Return the drawing under the cursor, or None if no drawing is hit."""
#         try:
#             actor = self.digitizer._pick_actor(x, y)
#             if actor:
#                 for d in self._iter_pickable_drawings():
#                     if d.get("actor") is actor:
#                         return d
#                     for key in ("start_marker", "end_marker"):
#                         if d.get(key) is actor:
#                             return d
#         except Exception:
#             pass

#         try:
#             return self.digitizer._get_drawing_under_cursor(x, y, tolerance=25.0)
#         except Exception:
#             return None

#     def _drawing_coords(self, drawing):
#         if hasattr(self.digitizer, "_get_drawing_coords"):
#             return self.digitizer._get_drawing_coords(drawing)
#         if isinstance(drawing, dict):
#             coords = drawing.get("coords")
#             if coords is not None:
#                 return coords
#             coords = drawing.get("coordinates")
#             if coords is not None:
#                 return coords
#             coords = drawing.get("interpolated")
#             if coords is not None:
#                 return coords
#         return []

#     def _iter_pickable_drawings(self):
#         drawings = list(getattr(self.digitizer, "drawings", []) or [])
#         curve_tool = getattr(self.app, "curve_tool", None)
#         finalized = getattr(curve_tool, "finalized_actors", None)
#         if finalized:
#             for curve_data in list(finalized or []):
#                 coords = self._drawing_coords(curve_data)
#                 if coords is not None and len(coords) > 0:
#                     if not any(curve_data is drawing for drawing in drawings):
#                         drawings.append(curve_data)
#         return drawings

#     def _highlight_hover_drawing(self, drawing):
#         actor = drawing.get("actor")
#         if actor is None or not hasattr(actor, "GetProperty"):
#             self._clear_hover_highlight()
#             return

#         # Don't hover-paint over an already-selected figure
#         if actor in self._selection_saved_props:
#             self._clear_hover_highlight()
#             return

#         if actor is self._hover_highlight_actor:
#             return

#         self._clear_hover_highlight()
#         prop = actor.GetProperty()
#         self._hover_orig_color = prop.GetColor()
#         self._hover_orig_width = prop.GetLineWidth()
#         prop.SetColor(0.0, 0.75, 1.0)
#         prop.SetLineWidth(max(self._hover_orig_width * 2, 4))
#         self._hover_highlight_actor = actor
#         try:
#             self.app.vtk_widget.render()
#         except Exception:
#             pass

#     def _clear_hover_highlight(self):
#         actor = self._hover_highlight_actor
#         if actor is not None and hasattr(actor, "GetProperty"):
#             prop = actor.GetProperty()
#             if self._hover_orig_color is not None:
#                 prop.SetColor(*self._hover_orig_color)
#             if self._hover_orig_width is not None:
#                 prop.SetLineWidth(self._hover_orig_width)
#             try:
#                 self.app.vtk_widget.render()
#             except Exception:
#                 pass
#         self._hover_highlight_actor = None
#         self._hover_orig_color = None
#         self._hover_orig_width = None

#     def _highlight_drawing(self, drawing):
#         """Cyan-highlight one newly selected drawing; idempotent per actor."""
#         actor = drawing.get("actor")
#         if actor is None or not hasattr(actor, "GetProperty"):
#             return
#         if actor in self._selection_saved_props:
#             return  # already highlighted
#         prop = actor.GetProperty()
#         self._selection_saved_props[actor] = (prop.GetColor(), prop.GetLineWidth())
#         prop.SetColor(0.0, 1.0, 1.0)
#         prop.SetLineWidth(max(prop.GetLineWidth() * 2, 4))
#         try:
#             self.app.vtk_widget.render()
#         except Exception:
#             pass

#     def _clear_highlight(self):
#         """Restore original appearance for every highlighted drawing."""
#         self._clear_hover_highlight()
#         for actor, (color, width) in list(self._selection_saved_props.items()):
#             try:
#                 if hasattr(actor, "GetProperty"):
#                     prop = actor.GetProperty()
#                     prop.SetColor(*color)
#                     prop.SetLineWidth(width)
#             except Exception:
#                 pass
#         self._selection_saved_props.clear()
#         try:
#             self.app.vtk_widget.render()
#         except Exception:
#             pass

#     def _update_figure_label(self):
#         count = len(self._selected_drawings)
#         if count == 0:
#             self.figure_label.setText("No figure selected")
#         elif count == 1:
#             d = self._selected_drawings[0]
#             dtype = d.get("type", "unknown")
#             n = len(self._drawing_coords(d))
#             self.figure_label.setText(f"Selected: {dtype.capitalize()} ({n} points)")
#         else:
#             types = ", ".join(
#                 d.get("type", "unknown").capitalize() for d in self._selected_drawings
#             )
#             self.figure_label.setText(f"Selected {count} figures: {types}")

#     def _on_clear(self):
#         """Reset the dialog state without deleting completed drawings."""
#         self._remove_selection_observer()
#         self._clear_highlight()
#         self._selected_drawings.clear()
#         self._selected_drawing = None
#         self._select_mode = False
#         self._reset_select_button()
#         self.distance_spin.setValue(0.10)
#         self.apply_btn.setEnabled(False)
#         self.classify_btn.setEnabled(False)
#         self._update_figure_label()
#         if not self.isVisible():
#             self.show()
#         self.raise_()
#         self.activateWindow()

#     def _reset_select_button(self):
#         """Restore the Select Figure button to its default state."""
#         with warnings.catch_warnings():
#             warnings.simplefilter("ignore")
#             try:
#                 self.select_btn.clicked.disconnect(self._on_finish_selection)
#             except Exception:
#                 pass
#             try:
#                 self.select_btn.clicked.disconnect(self._on_select_figure)
#             except Exception:
#                 pass
#         self.select_btn.clicked.connect(self._on_select_figure)
#         self.select_btn.setText("Select Figure")
#         self.select_btn.setEnabled(True)

#     def _build_parallel_band_mask_xy(self, xyz, polyline_xy, left_tol, right_tol):
#         if xyz is None or polyline_xy is None:
#             return np.zeros(0, dtype=bool)
#         pts = np.asarray(xyz, dtype=np.float64)
#         if pts.ndim != 2 or pts.shape[0] == 0:
#             return np.zeros(0, dtype=bool)
#         line = np.asarray(polyline_xy, dtype=np.float64)
#         if line.ndim != 2 or line.shape[0] < 2:
#             return np.zeros(len(pts), dtype=bool)

#         points_xy = pts[:, :2]
#         mask = np.zeros(len(points_xy), dtype=bool)
#         left_tol = max(0.0, float(left_tol))
#         right_tol = max(0.0, float(right_tol))

#         for p0, p1 in zip(line[:-1], line[1:]):
#             v = p1 - p0
#             seg_len_sq = float(np.dot(v, v))
#             if seg_len_sq <= 1e-12:
#                 continue
#             seg_len = float(np.sqrt(seg_len_sq))
#             rel = points_xy - p0
#             t = (rel @ v) / seg_len_sq
#             in_seg = (t >= 0.0) & (t <= 1.0)
#             cross = v[0] * rel[:, 1] - v[1] * rel[:, 0]
#             signed_distance = cross / seg_len
#             in_band = (signed_distance >= -right_tol) & (signed_distance <= left_tol)
#             mask |= in_seg & in_band
#         return mask

#     def _band_mode(self):
#         data = self.band_mode_combo.currentData()
#         return str(data or "symmetric").lower()

#     def _sync_band_mode_ui(self):
#         # Symmetric mode keeps a single total width control, while asymmetric
#         # mode exposes independent above/below widths through the two spins.
#         symmetric = self._band_mode() != "asymmetric"
#         self.left_tol_label.setText("Total width:" if symmetric else "Tolerance above:")
#         self.right_tol_label.setText("Tolerance below:")
#         if symmetric:
#             total_width = float(self.left_tol_spin.value())
#             half_width = max(0.0, total_width / 2.0)
#             self.right_tol_spin.blockSignals(True)
#             try:
#                 self.right_tol_spin.setValue(half_width)
#             finally:
#                 self.right_tol_spin.blockSignals(False)
#             self.right_tol_spin.setEnabled(False)
#             self.left_tol_spin.setToolTip("Total band width measured across both sides of the line.")
#             self.right_tol_spin.setToolTip("Auto-derived as half of the total width.")
#         else:
#             self.right_tol_spin.setEnabled(True)
#             self.left_tol_spin.setToolTip("Distance above / left of the line.")
#             self.right_tol_spin.setToolTip("Distance below / right of the line.")

#     def _sync_band_side_ui(self):
#         try:
#             idx = self.band_side_combo.findData(self._selected_side)
#             if idx >= 0:
#                 self.band_side_combo.setCurrentIndex(idx)
#         except Exception:
#             pass

#     def _set_status(self, text):
#         self.classify_status.setText(text)
#         try:
#             if hasattr(self.app, "statusBar") and self.app.statusBar():
#                 self.app.statusBar().showMessage(text, 3500)
#         except Exception:
#             pass

#     def _refresh_class_lists_from_active_palette(self):
#         # Pull the latest PTC/class palette right before classification so the
#         # dropdowns follow whatever DisplayMode last applied, but keep the
#         # user's live UI edits intact.
#         try:
#             current_from = self.from_class_combo.currentData()
#             current_to = self.to_class_combo.currentData()
#             current_mode = self.band_mode_combo.currentData()
#             current_side = self.band_side_combo.currentData()
#             current_left = float(self.left_tol_spin.value())
#             current_right = float(self.right_tol_spin.value())
#             self._populate_class_dropdowns()
#             if current_from is not None:
#                 idx = self.from_class_combo.findData(current_from)
#                 if idx >= 0:
#                     self.from_class_combo.setCurrentIndex(idx)
#             if current_to is not None:
#                 idx = self.to_class_combo.findData(current_to)
#                 if idx >= 0:
#                     self.to_class_combo.setCurrentIndex(idx)
#             if current_mode is not None:
#                 idx = self.band_mode_combo.findData(current_mode)
#                 if idx >= 0:
#                     self.band_mode_combo.setCurrentIndex(idx)
#             if current_side is not None:
#                 idx = self.band_side_combo.findData(current_side)
#                 if idx >= 0:
#                     self.band_side_combo.setCurrentIndex(idx)
#             self.left_tol_spin.setValue(current_left)
#             self.right_tol_spin.setValue(current_right)
#             self._sync_band_mode_ui()
#             self._sync_band_side_ui()
#         except Exception:
#             pass

#     def _refresh_main_view_after_parallel_classify(self):
#         app = self.app
#         display_mode = str(getattr(app, "display_mode", "class") or "class").lower()
#         rendered = False
#         try:
#             # Keep the current loaded palette/rendering intact by using the
#             # smallest safe redraw hook for the active mode.
#             if display_mode == "surface":
#                 try:
#                     from gui.surface_mode import refresh_surface_after_classification
#                     refresh_surface_after_classification(
#                         app,
#                         changed_mask=getattr(app, "_last_changed_mask", None),
#                         operation="classification",
#                     )
#                     rendered = True
#                 except Exception:
#                     pass
#             elif display_mode == "shaded_class":
#                 try:
#                     from gui.shading_display import refresh_shaded_after_classification_fast
#                     refresh_shaded_after_classification_fast(
#                         app,
#                         changed_mask=getattr(app, "_last_changed_mask", None),
#                     )
#                     rendered = True
#                 except Exception:
#                     try:
#                         from gui.shading_display import update_shaded_class
#                         update_shaded_class(app, force_rebuild=True)
#                         rendered = True
#                     except Exception:
#                         pass
#             else:
#                 if hasattr(app, "_sync_classification_to_gpu"):
#                     app._sync_classification_to_gpu()
#                     rendered = True
#         except Exception:
#             pass
#         if not rendered:
#             try:
#                 if hasattr(app, "vtk_widget") and app.vtk_widget:
#                     app.vtk_widget.render()
#             except Exception:
#                 pass
#         try:
#             if hasattr(app, "statusBar") and app.statusBar():
#                 app.statusBar().showMessage("Parallel band classification applied", 2500)
#         except Exception:
#             pass

#     def _on_classify_band(self):
#         self._refresh_class_lists_from_active_palette()
#         if self._selected_drawing is None and not self._selected_drawings:
#             self._set_status("Select a line/polyline first.")
#             return
#         if self._selected_drawing is None and self._selected_drawings:
#             self._selected_drawing = self._selected_drawings[-1]

#         drawing = self._selected_drawing
#         coords = np.asarray(self._drawing_coords(drawing), dtype=np.float64)
#         if coords.ndim != 2 or coords.shape[0] < 2:
#             self._set_status("Selected drawing has no usable coordinates.")
#             return

#         xyz = getattr(self.app, "data", {}).get("xyz") if getattr(self.app, "data", None) else None
#         classes = getattr(self.app, "data", {}).get("classification") if getattr(self.app, "data", None) else None
#         if xyz is None or classes is None:
#             self._set_status("No point cloud loaded.")
#             return

#         if self._band_mode() == "symmetric":
#             left_tol = max(0.0, float(self.left_tol_spin.value()) / 2.0)
#             right_tol = left_tol
#         else:
#             left_tol = float(self.left_tol_spin.value())
#             right_tol = float(self.right_tol_spin.value())
#         from_class = self.from_class_combo.currentData()
#         to_class = self.to_class_combo.currentData()
#         if to_class is None:
#             self._set_status("Choose a target class.")
#             return

#         band_mask = self._build_parallel_band_mask_xy(xyz, coords[:, :2], left_tol, right_tol)
#         band_points = int(np.count_nonzero(band_mask))
#         if band_points == 0:
#             self._set_status("No points found inside selected parallel band.")
#             return

#         if from_class is not None:
#             band_mask &= (classes == int(from_class))
#         band_mask &= (classes != int(to_class))

#         changed_points = int(np.count_nonzero(band_mask))
#         print(
#             f"[PARALLEL-CLASSIFY] selected_drawing={drawing.get('type', 'unknown')} "
#             f"segments={max(0, len(coords) - 1)} left_tol={left_tol:.3f} "
#             f"right_tol={right_tol:.3f} from_class={from_class} to_class={to_class} "
#             f"band_points={band_points} changed_points={changed_points}"
#         )

#         if changed_points == 0:
#             self._set_status("No points matched the selected class filter.")
#             return

#         try:
#             self._selected_side = str(self.band_side_combo.currentData() or "left")
#         except Exception:
#             self._selected_side = "left"

#         if _apply_classification is None:
#             self._set_status("Classification engine unavailable.")
#             return

#         try:
#             ok = _apply_classification(self.app, band_mask, [int(from_class)] if from_class is not None else None, int(to_class))
#         except Exception as e:
#             self._set_status(f"Classification failed: {e}")
#             return

#         if not ok:
#             self._set_status("Classification made no changes.")
#             return

#         self._save_parallel_settings()
#         self._refresh_main_view_after_parallel_classify()
#         self._set_status(f"[PARALLEL-CLASSIFY] band_points={band_points} changed_points={changed_points}")

#     def _class_palette_codes(self):
#         palette = getattr(self.app, "class_palette", {}) or {}
#         codes = []
#         try:
#             codes = sorted(int(c) for c in palette.keys())
#         except Exception:
#             codes = []
#         if not codes:
#             data = getattr(self.app, "data", None) or {}
#             classes = data.get("classification")
#             if classes is not None:
#                 try:
#                     codes = sorted(int(c) for c in np.unique(classes))
#                 except Exception:
#                     codes = []
#         return codes

#     def _class_item_icon(self, color):
#         try:
#             if isinstance(color, QColor):
#                 qcolor = color
#             else:
#                 rgb = tuple(int(c) for c in (color or (128, 128, 128))[:3])
#                 qcolor = QColor(*rgb)
#             pix = QPixmap(12, 12)
#             pix.fill(qcolor)
#             return QIcon(pix)
#         except Exception:
#             return QIcon()

#     def _populate_class_dropdowns(self):
#         from_palette = getattr(self.app, "class_palette", {}) or {}
#         codes = self._class_palette_codes()
#         self.from_class_combo.blockSignals(True)
#         self.to_class_combo.blockSignals(True)
#         try:
#             self.from_class_combo.clear()
#             self.to_class_combo.clear()
#             self.from_class_combo.addItem("Any class", None)
#             for code in codes:
#                 info = from_palette.get(code, {}) or {}
#                 desc = (
#                     info.get("description")
#                     or info.get("lvl")
#                     or info.get("draw")
#                     or f"Class {code}"
#                 )
#                 label = f"{code} - {desc}"
#                 icon = self._class_item_icon(info.get("color", (128, 128, 128)))
#                 self.from_class_combo.addItem(icon, label, code)
#                 self.to_class_combo.addItem(icon, label, code)
#         finally:
#             self.from_class_combo.blockSignals(False)
#             self.to_class_combo.blockSignals(False)

#     def _load_parallel_settings(self):
#         try:
#             from PySide6.QtCore import QSettings
#             s = QSettings("NakshaAI", "LidarApp")
#             mode = str(s.value("parallel_tool/band_mode", "symmetric"))
#             idx = self.band_mode_combo.findData(mode)
#             if idx >= 0:
#                 self.band_mode_combo.setCurrentIndex(idx)
#             side = str(s.value("parallel_tool/side", "left"))
#             idx = self.band_side_combo.findData(side)
#             if idx >= 0:
#                 self.band_side_combo.setCurrentIndex(idx)
#             self.left_tol_spin.setValue(float(s.value("parallel_tool/left_tolerance", 0.20)))
#             self.right_tol_spin.setValue(float(s.value("parallel_tool/right_tolerance", 0.20)))
#             self._selected_side = str(s.value("parallel_tool/side", "left"))
#             from_code = s.value("parallel_tool/from_class", None)
#             to_code = s.value("parallel_tool/to_class", None)
#             if from_code is not None:
#                 idx = self.from_class_combo.findData(int(from_code))
#                 if idx >= 0:
#                     self.from_class_combo.setCurrentIndex(idx)
#             if to_code is not None:
#                 idx = self.to_class_combo.findData(int(to_code))
#                 if idx >= 0:
#                     self.to_class_combo.setCurrentIndex(idx)
#         except Exception:
#             pass

#     def _save_parallel_settings(self):
#         try:
#             from PySide6.QtCore import QSettings
#             s = QSettings("NakshaAI", "LidarApp")
#             s.setValue("parallel_tool/band_mode", self.band_mode_combo.currentData())
#             s.setValue("parallel_tool/left_tolerance", float(self.left_tol_spin.value()))
#             s.setValue("parallel_tool/right_tolerance", float(self.right_tol_spin.value()))
#             s.setValue("parallel_tool/side", self.band_side_combo.currentData())
#             s.setValue("parallel_tool/from_class", self.from_class_combo.currentData())
#             s.setValue("parallel_tool/to_class", self.to_class_combo.currentData())
#         except Exception:
#             pass

#     # ── Apply ───────────────────────────────────────────────────────────
#     def _on_apply(self):
#         """Generate a parallel figure for every selected drawing, then close."""
#         if not self._selected_drawings:
#             return

#         distance_m = self.distance_spin.value()
#         try:
#             self._selected_side = str(self.band_side_combo.currentData() or "left")
#         except Exception:
#             self._selected_side = "left"

#         failed = 0
#         for drawing in list(self._selected_drawings):
#             coords = list(self._drawing_coords(drawing))
#             dtype = drawing.get("type", "")

#             if len(coords) < 2:
#                 failed += 1
#                 continue

#             new_coords = self._compute_parallel_coords(coords, dtype, distance_m, self._selected_side)
#             if not new_coords or len(new_coords) < 2:
#                 failed += 1
#                 continue

#             self._create_parallel_drawing(new_coords, dtype, drawing)

#         if failed:
#             self.figure_label.setText(
#                 f"{failed} figure(s) could not be offset — check geometry."
#             )

#         # Clean up and close
#         self._remove_selection_observer()
#         self._clear_highlight()
#         self._selected_drawings.clear()
#         self.accept()

#     def _compute_parallel_coords(self, coords, dtype, distance_m, side="left"):
#         """Compute offset coordinates for the parallel figure.

#         ``side`` controls the direction of the offset for **open** polylines:
#           - "left"   → offset to the left of the direction of travel  (+normal)
#           - "right"  → offset to the right                            (-normal)
#           - "top"    → offset upward  in world-Y                      (+Y bias)
#           - "bottom" → offset downward in world-Y                     (-Y bias)

#         For **closed** figures the sign flips inward/outward:
#           - "left" / "top" / "bottom" → outward  (positive distance)
#           - "right"                   → inward    (negative distance)
#         """
#         is_closed = self._is_closed_figure(coords, dtype)

#         # --- Signed distance based on side ---
#         if is_closed:
#             # right = inward for closed shapes; everything else = outward
#             signed_dist = -distance_m if side == "right" else distance_m
#         else:
#             # For open lines the normal is the left-perpendicular of travel.
#             # "right"  flips it; "top"/"bottom" we handle via _world_normal_sign.
#             signed_dist = distance_m  # adjusted below for open lines

#         if dtype == "circle":
#             return self._parallel_circle(coords, signed_dist, is_closed)

#         if is_closed:
#             return self._parallel_closed_polygon(coords, signed_dist)

#         # --- Open polyline: resolve effective sign ---
#         # _segment_normal returns the left-hand perpendicular.
#         # left  → use as-is  (+1)
#         # right → flip       (-1)
#         # top   → we want the component that pushes up; post-process below
#         # bottom→ push down; post-process below
#         if side in ("left", "right"):
#             sign = 1.0 if side == "left" else -1.0
#             return self._parallel_open_polyline(coords, distance_m * sign)

#         # Top / Bottom still use the true parallel offset path so the result
#         # remains geometrically parallel to the source line in world XY.
#         # We keep the dropdown labels for UX, but the geometry stays exact.
#         sign = 1.0 if side == "top" else -1.0
#         return self._parallel_open_polyline(coords, distance_m * sign)

#     def _resolve_source_style(self, drawing):
#         """Use the source drawing's persisted style so generated geometry restores correctly."""
#         style = dict(self.digitizer._get_draw_style("line") or {})
#         color = drawing.get("original_color")
#         width = drawing.get("original_width")
#         line_style = drawing.get("original_style")

#         actor = drawing.get("actor")
#         if actor is not None and hasattr(actor, "GetProperty"):
#             try:
#                 prop = actor.GetProperty()
#                 if color is None:
#                     color = tuple(float(v) for v in prop.GetColor())
#                 if width is None:
#                     width = float(prop.GetLineWidth())
#             except Exception:
#                 pass

#         if color is None:
#             color = style.get("color", (1.0, 0.0, 0.0))
#         else:
#             color = tuple(float(v) for v in color[:3])

#         if width is None:
#             width = style.get("width", 2)
#         try:
#             width = max(float(width), 1.0)
#         except Exception:
#             width = float(style.get("width", 2))

#         if not line_style:
#             line_style = style.get("style", "solid")

#         return color, width, line_style

#     # ── Geometry helpers ────────────────────────────────────────────────

#     @staticmethod
#     def _is_closed_figure(coords, dtype):
#         if dtype in ("rectangle", "polygon", "circle"):
#             return True
#         if len(coords) < 3:
#             return False
#         p0 = np.array(coords[0][:2], dtype=np.float64)
#         pn = np.array(coords[-1][:2], dtype=np.float64)
#         return np.linalg.norm(p0 - pn) < 1e-6

#     @staticmethod
#     def _segment_normal(p1, p2):
#         """Return the unit left normal for p1 to p2."""
#         d = np.array(p2[:2], dtype=np.float64) - np.array(p1[:2], dtype=np.float64)
#         length = np.linalg.norm(d)
#         if length < 1e-12:
#             return np.array([0.0, 0.0])
#         n = np.array([-d[1], d[0]], dtype=np.float64) / length
#         return n

#     @staticmethod
#     def _line_intersection_xy(a1, a2, b1, b2):
#         """Return XY intersection of two infinite 2D lines, or None if parallel."""
#         p = np.array(a1[:2], dtype=np.float64)
#         r = np.array(a2[:2], dtype=np.float64) - p
#         q = np.array(b1[:2], dtype=np.float64)
#         s = np.array(b2[:2], dtype=np.float64) - q
#         denom = r[0] * s[1] - r[1] * s[0]
#         if abs(float(denom)) < 1e-12:
#             return None
#         qp = q - p
#         t = (qp[0] * s[1] - qp[1] * s[0]) / denom
#         return p + t * r

#     @staticmethod
#     def _signed_area_xy(coords):
#         area = 0.0
#         for i in range(len(coords)):
#             x1, y1 = coords[i][0], coords[i][1]
#             x2, y2 = coords[(i + 1) % len(coords)][0], coords[(i + 1) % len(coords)][1]
#             area += float(x1) * float(y2) - float(x2) * float(y1)
#         return area * 0.5

#     @staticmethod
#     def _offset_point(point, normal, distance_m):
#         p = np.array(point[:2], dtype=np.float64) + normal * distance_m
#         z = float(point[2]) if len(point) > 2 else 0.0
#         return (float(p[0]), float(p[1]), z)

#     def _parallel_open_polyline(self, coords, distance_m):
#         """Offset an open polyline by exactly distance_m from each segment."""
#         if len(coords) < 2:
#             return []

#         normals = [
#             self._segment_normal(coords[i], coords[i + 1])
#             for i in range(len(coords) - 1)
#         ]

#         result = [self._offset_point(coords[0], normals[0], distance_m)]
#         for i in range(1, len(coords) - 1):
#             n_prev = normals[i - 1]
#             n_next = normals[i]
#             prev_a = self._offset_point(coords[i - 1], n_prev, distance_m)
#             prev_b = self._offset_point(coords[i], n_prev, distance_m)
#             next_a = self._offset_point(coords[i], n_next, distance_m)
#             next_b = self._offset_point(coords[i + 1], n_next, distance_m)
#             xy = self._line_intersection_xy(prev_a, prev_b, next_a, next_b)
#             if xy is None:
#                 avg = n_prev + n_next
#                 norm = np.linalg.norm(avg)
#                 normal = avg / norm if norm > 1e-12 else n_prev
#                 result.append(self._offset_point(coords[i], normal, distance_m))
#             else:
#                 z = float(coords[i][2]) if len(coords[i]) > 2 else 0.0
#                 result.append((float(xy[0]), float(xy[1]), z))

#         result.append(self._offset_point(coords[-1], normals[-1], distance_m))
#         return result

#     def _parallel_open_polyline_world_y(self, coords, distance_m, up: bool = True):
#         """Offset an open polyline by ``distance_m`` purely in world-Y direction.

#         Used for the Top / Bottom side selections so the user gets an intuitive
#         vertical offset regardless of the line's direction of travel.
#         """
#         dy = distance_m if up else -distance_m
#         result = []
#         for pt in coords:
#             x = float(pt[0])
#             y = float(pt[1]) + dy
#             z = float(pt[2]) if len(pt) > 2 else 0.0
#             result.append((x, y, z))
#         return result

#     def _parallel_closed_polygon(self, coords, distance_m):
#         # Use only the unique vertices (remove closing duplicate if present)
#         if self._points_equal(coords[0], coords[-1]):
#             verts = coords[:-1]
#         else:
#             verts = coords
#         nv = len(verts)
#         if nv < 3:
#             return self._parallel_open_polyline(verts, distance_m)

#         normal_sign = -1.0 if self._signed_area_xy(verts) > 0.0 else 1.0
#         normals = [
#             self._segment_normal(verts[i], verts[(i + 1) % nv]) * normal_sign
#             for i in range(nv)
#         ]
#         result = []
#         for i in range(nv):
#             prev_i = (i - 1) % nv
#             next_i = (i + 1) % nv
#             n_prev = normals[prev_i]
#             n_curr = normals[i]
#             prev_a = self._offset_point(verts[prev_i], n_prev, distance_m)
#             prev_b = self._offset_point(verts[i], n_prev, distance_m)
#             curr_a = self._offset_point(verts[i], n_curr, distance_m)
#             curr_b = self._offset_point(verts[next_i], n_curr, distance_m)
#             xy = self._line_intersection_xy(prev_a, prev_b, curr_a, curr_b)
#             if xy is None:
#                 avg = n_prev + n_curr
#                 norm = np.linalg.norm(avg)
#                 normal = avg / norm if norm > 1e-12 else n_curr
#                 result.append(self._offset_point(verts[i], normal, distance_m))
#             else:
#                 z = float(verts[i][2]) if len(verts[i]) > 2 else 0.0
#                 result.append((float(xy[0]), float(xy[1]), z))

#         # Close the polygon
#         result.append(result[0])
#         return result

#     def _parallel_circle(self, coords, distance_m, is_closed):
#         """Offset a circle by adjusting its radius."""
#         if len(coords) < 2:
#             return []
#         if len(coords) > 2 and self._points_equal(coords[0], coords[-1]):
#             coords = coords[:-1]
#         # Compute center and radius from the coordinates
#         pts = np.array([c[:2] for c in coords], dtype=np.float64)
#         cx = float(np.mean(pts[:, 0]))
#         cy = float(np.mean(pts[:, 1]))

#         # Compute average radius
#         radii = np.sqrt((pts[:, 0] - cx) ** 2 + (pts[:, 1] - cy) ** 2)
#         avg_radius = float(np.mean(radii))

#         new_radius = avg_radius + distance_m
#         if new_radius <= 0:
#             return []

#         # Generate the new circle using the original angular samples.
#         result = []
#         for coord in coords:
#             vec = np.array(coord[:2], dtype=np.float64) - np.array([cx, cy], dtype=np.float64)
#             length = np.linalg.norm(vec)
#             if length < 1e-12:
#                 continue
#             unit = vec / length
#             x = float(cx + new_radius * unit[0])
#             y = float(cy + new_radius * unit[1])
#             z = float(coord[2]) if len(coord) > 2 else 0.0
#             result.append((x, y, z))
#         if len(result) < 2:
#             return []
#         result.append(result[0])  # close
#         return result

#     @staticmethod
#     def _points_equal(p1, p2, tol=1e-6):
#         return (
#             abs(float(p1[0]) - float(p2[0])) < tol
#             and abs(float(p1[1]) - float(p2[1])) < tol
#         )

#     # ── Create the drawing ──────────────────────────────────────────────

#     def _create_parallel_drawing(self, new_coords, orig_dtype, orig_drawing):
#         """Create a new drawing entry for the parallel figure."""
#         import vtk

#         color, width, line_style = self._resolve_source_style(orig_drawing)

#         # Build the VTK actor
#         actor = self.digitizer._make_polyline_actor(
#             new_coords, color=color, width=width, line_style=line_style
#         )
#         self.digitizer._add_actor_to_overlay(actor)

#         # Determine the type label
#         if orig_dtype == "rectangle":
#             new_type = "polygon"
#         elif orig_dtype in ("line_segment", "smartline", "curve"):
#             new_type = "parallel_line"
#         else:
#             new_type = "parallel_" + orig_dtype

#         drawing_entry = {
#             "type": new_type,
#             "coords": list(new_coords),
#             "actor": actor,
#             "bounds": actor.GetBounds(),
#             "original_color": color,
#             "original_width": width,
#             "original_style": line_style,
#             "generated_from": orig_dtype,
#         }

#         self.digitizer._save_state()
#         self.digitizer.drawings.append(drawing_entry)
#         self.digitizer._notify_selection_drawings_changed()
#         self.digitizer._emit_drawing_finalized(drawing_entry)
#         self.digitizer.renderer.Modified()
#         self.app.vtk_widget.render()

#         print(
#             f"✅ Parallel figure created: {new_type} "
#             f"({len(new_coords)} points, offset={self.distance_spin.value()} m)"
#         )

#     # ── Cleanup on close ────────────────────────────────────────────────
#     def closeEvent(self, event):
#         """Clean up observer and highlight when dialog is closed."""
#         try:
#             self._save_parallel_settings()
#         except Exception:
#             pass
#         self._cleanup_chip()
#         self._remove_selection_observer()
#         self._clear_highlight()
#         self._selected_drawings.clear()
#         self._select_mode = False
#         super().closeEvent(event)

#     def reject(self):
#         """Clean up when Escape is pressed."""
#         self._remove_selection_observer()
#         self._clear_highlight()
#         self._selected_drawings.clear()
#         self._select_mode = False
#         super().reject()

#     def _remove_selection_observer(self):
#         interactor = self.digitizer.interactor
#         if self._old_left_press is not None:
#             try:
#                 interactor.RemoveObserver(self._old_left_press)
#             except Exception:
#                 pass
#             self._old_left_press = None
#         if self._old_mouse_move is not None:
#             try:
#                 interactor.RemoveObserver(self._old_mouse_move)
#             except Exception:
#                 pass
#             self._old_mouse_move = None

from __future__ import annotations

import warnings
import numpy as np

try:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QColor, QIcon, QPixmap
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
        QDoubleSpinBox, QGroupBox, QFrame, QApplication, QSizePolicy,
        QButtonGroup, QComboBox,
    )
except ImportError:
    from PyQt5.QtCore import Qt, QTimer
    from PyQt5.QtGui import QColor, QIcon, QPixmap
    from PyQt5.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
        QDoubleSpinBox, QGroupBox, QFrame, QApplication, QSizePolicy,
        QButtonGroup, QComboBox,
    )

try:
    from gui.classification_tools import _apply_classification
except Exception:
    _apply_classification = None

try:
    from gui.theme_manager import ThemeColors
except Exception:
    class ThemeColors:
        @staticmethod
        def get(k):
            return ""

try:
    from gui.minimize_chip import MinimizableDialogMixin
except Exception:
    class MinimizableDialogMixin:
        def _init_minimize_state(self): pass
        def _cleanup_chip(self): pass


def _get_parallel_dialog_style():
    return f"""
QDialog {{
    background-color: {ThemeColors.get('bg_secondary')};
    color: {ThemeColors.get('text_primary')};
    border: 1px solid {ThemeColors.get('border_light')};
    border-radius: 12px;
}}
QGroupBox {{
    background-color: {ThemeColors.get('bg_primary')};
    border: 1px solid {ThemeColors.get('border_light')};
    border-radius: 10px;
    margin-top: 15px;
    padding-top: 18px;
    padding-bottom: 10px;
    font-weight: bold;
    color: {ThemeColors.get('text_primary')};
    font-size: 13px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: {ThemeColors.get('accent')};
}}
QLabel {{
    color: {ThemeColors.get('text_primary')};
    font-size: 13px;
    font-weight: 500;
}}
QDoubleSpinBox {{
    background-color: {ThemeColors.get('bg_input')};
    color: {ThemeColors.get('text_primary')};
    border: 1px solid {ThemeColors.get('border_light')};
    border-radius: 6px;
    padding: 6px 10px;
    min-height: 28px;
    font-size: 13px;
}}
QDoubleSpinBox:hover {{
    border: 1px solid {ThemeColors.get('accent')};
    background-color: {ThemeColors.get('bg_button_hover')};
}}
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    width: 0px;
    border: none;
}}
QPushButton {{
    background-color: {ThemeColors.get('bg_button')};
    color: {ThemeColors.get('text_primary')};
    border: 1px solid {ThemeColors.get('border_light')};
    border-radius: 6px;
    padding: 8px 16px;
    font-weight: bold;
    font-size: 13px;
    min-height: 28px;
}}
QPushButton:hover {{
    background-color: {ThemeColors.get('bg_button_hover')};
    border-color: {ThemeColors.get('accent')};
}}
QPushButton:pressed {{
    background-color: {ThemeColors.get('accent')};
    color: {ThemeColors.get('text_on_active')};
}}
QPushButton:disabled {{
    color: {ThemeColors.get('text_primary')};
    background-color: {ThemeColors.get('bg_button')};
    border-color: {ThemeColors.get('border_light')};
}}
QFrame {{
    color: {ThemeColors.get('border_light')};
}}
QPushButton[sideBtn="true"] {{
    background-color: {ThemeColors.get('bg_button')};
    color: {ThemeColors.get('text_primary')};
    border: 1px solid {ThemeColors.get('border_light')};
    border-radius: 6px;
    padding: 6px 4px;
    font-size: 12px;
    font-weight: bold;
    min-width: 58px;
    min-height: 28px;
}}
QPushButton[sideBtn="true"]:hover {{
    background-color: {ThemeColors.get('bg_button_hover')};
    border-color: {ThemeColors.get('accent')};
}}
QPushButton[sideBtn="true"][checked="true"] {{
    background-color: {ThemeColors.get('accent')};
    color: {ThemeColors.get('text_on_active')};
    border-color: {ThemeColors.get('accent')};
}}
"""


class ParallelToolDialog(MinimizableDialogMixin, QDialog):
    """
    Dialog for the Parallel tool workflow:
      1. Click 'Select Figure' → pick a drawn figure on the canvas
      2. Enter distance (m)
      3. Click 'Apply' → parallel figure is generated
    """

    def __init__(self, digitizer, parent=None):
        super().__init__(None, Qt.Window)
        self._init_minimize_state()
        self.digitizer = digitizer
        self.app = digitizer.app
        self._selected_drawings = []        # list of selected drawing dicts
        self._selection_saved_props = {}    # actor → (orig_color, orig_width)
        self._select_mode = False
        self._old_left_press = None
        self._old_mouse_move = None
        self._hover_highlight_actor = None
        self._hover_orig_color = None
        self._hover_orig_width = None
        self._selected_side = "left"   # default offset side
        self._selected_drawing = None
        self._parallel_left_tolerance = 0.20
        self._parallel_right_tolerance = 0.20

        self.setWindowTitle("Parallel Tool")
        self.setWindowFlags(
            Qt.Dialog
            | Qt.WindowCloseButtonHint
            | Qt.WindowMaximizeButtonHint
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_QuitOnClose, False)
        self.setMinimumWidth(360)
        self.resize(420, 540)
        self.setStyleSheet(_get_parallel_dialog_style())
        self._build_ui()

    # ── UI ──────────────────────────────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 5, 18, 10)
        root.setSpacing(12)

        # ── Section 1: Select Figure ────────────────────────────────────
        sec1 = QGroupBox("Figure")
        sec1_layout = QVBoxLayout(sec1)
        sec1_layout.setContentsMargins(15, 10, 15, 10)
        sec1_layout.setSpacing(8)

        self.select_btn = QPushButton("Select Figure")
        self.select_btn.setCursor(Qt.PointingHandCursor)
        self.select_btn.clicked.connect(self._on_select_figure)
        sec1_layout.addWidget(self.select_btn)

        self.figure_label = QLabel("No figure selected")
        self.figure_label.setWordWrap(True)
        self.figure_label.setStyleSheet(
            f"color: {ThemeColors.get('text_secondary')}; font-size: 11px; font-weight: 500; padding: 4px 0;"
        )
        sec1_layout.addWidget(self.figure_label)

        root.addWidget(sec1)

        # ── Section 2: Side Selection ───────────────────────────────────
        sec_side = QGroupBox("Side")
        sec_side_layout = QVBoxLayout(sec_side)
        sec_side_layout.setContentsMargins(15, 10, 15, 10)
        sec_side_layout.setSpacing(6)

        # Top row: just "Top" centred
        top_row = QHBoxLayout()
        top_row.setSpacing(6)
        self._btn_top = self._make_side_btn("▲  Top")
        top_row.addStretch()
        top_row.addWidget(self._btn_top)
        top_row.addStretch()
        sec_side_layout.addLayout(top_row)

        # Middle row: Left  [diagram]  Right
        mid_row = QHBoxLayout()
        mid_row.setSpacing(6)
        self._btn_left  = self._make_side_btn("◀  Left")
        self._btn_right = self._make_side_btn("Right  ▶")

        # Small visual indicator in the centre
        centre_lbl = QLabel("┼")
        centre_lbl.setAlignment(Qt.AlignCenter)
        centre_lbl.setStyleSheet(
            f"color: {ThemeColors.get('text_secondary')}; font-size: 18px; min-width: 28px;"
        )
        mid_row.addWidget(self._btn_left)
        mid_row.addStretch()
        mid_row.addWidget(centre_lbl)
        mid_row.addStretch()
        mid_row.addWidget(self._btn_right)
        sec_side_layout.addLayout(mid_row)

        # Bottom row: just "Bottom" centred
        bot_row = QHBoxLayout()
        bot_row.setSpacing(6)
        self._btn_bottom = self._make_side_btn("▼  Bottom")
        bot_row.addStretch()
        bot_row.addWidget(self._btn_bottom)
        bot_row.addStretch()
        sec_side_layout.addLayout(bot_row)

        # Hint label
        self._side_hint = QLabel("Offset side: Left  (for closed shapes: outward)")
        self._side_hint.setWordWrap(True)
        self._side_hint.setStyleSheet(
            f"color: {ThemeColors.get('text_secondary')}; font-size: 11px; padding: 2px 0;"
        )
        sec_side_layout.addWidget(self._side_hint)

        # Button group for mutual exclusion
        self._side_group = QButtonGroup(self)
        self._side_group.setExclusive(True)
        for btn in (self._btn_left, self._btn_right, self._btn_top, self._btn_bottom):
            self._side_group.addButton(btn)
            btn.clicked.connect(self._on_side_selected)

        # Default: Left
        self._set_side_active(self._btn_left, "left")

        root.addWidget(sec_side)
        # Keep the workflow compact; the old side pad is now collapsed into
        # the compact side dropdown inside the classify block.
        sec_side.hide()

        # ── Section 3: Distance + Apply ─────────────────────────────────
        sec2 = QGroupBox("Offset")
        sec2_layout = QVBoxLayout(sec2)
        sec2_layout.setContentsMargins(15, 10, 15, 10)
        sec2_layout.setSpacing(8)

        dist_row = QHBoxLayout()
        dist_row.setSpacing(8)
        dist_label = QLabel("Distance:")
        dist_label.setStyleSheet(
            f"color: {ThemeColors.get('text_primary')}; font-size: 12px;"
        )
        dist_row.addWidget(dist_label)

        self.distance_spin = QDoubleSpinBox()
        self.distance_spin.setDecimals(2)
        self.distance_spin.setRange(0.01, 9999.99)
        self.distance_spin.setSingleStep(10)
        self.distance_spin.setValue(10)
        self.distance_spin.setSuffix(" m")
        dist_row.addWidget(self.distance_spin, 1)
        sec2_layout.addLayout(dist_row)

        # Separator
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        sec2_layout.addWidget(line)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)

        # Apply button
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.setCursor(Qt.PointingHandCursor)
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self._on_apply)
        action_row.addWidget(self.apply_btn, 1)

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setCursor(Qt.PointingHandCursor)
        self.clear_btn.clicked.connect(self._on_clear)
        action_row.addWidget(self.clear_btn, 1)

        sec2_layout.addLayout(action_row)

        root.addWidget(sec2)

        # â”€â”€ Section 4: Classify Band â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        sec_class = QGroupBox("Classify Band")
        sec_class_layout = QVBoxLayout(sec_class)
        sec_class_layout.setContentsMargins(15, 10, 15, 10)
        sec_class_layout.setSpacing(8)

        band_note = QLabel("Use the selected line/polyline to classify points in a world-space XY band.")
        band_note.setWordWrap(True)
        band_note.setStyleSheet(
            f"color: {ThemeColors.get('text_secondary')}; font-size: 11px; padding: 2px 0;"
        )
        sec_class_layout.addWidget(band_note)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        mode_row.addWidget(QLabel("Mode:"))
        self.band_mode_combo = QComboBox()
        self.band_mode_combo.addItem("Symmetric (total width)", "symmetric")
        self.band_mode_combo.addItem("Asymmetric (above / below)", "asymmetric")
        self.band_mode_combo.currentIndexChanged.connect(self._sync_band_mode_ui)
        mode_row.addWidget(self.band_mode_combo, 1)
        sec_class_layout.addLayout(mode_row)

        side_row = QHBoxLayout()
        side_row.setSpacing(8)
        side_row.addWidget(QLabel("Side:"))
        self.band_side_combo = QComboBox()
        self.band_side_combo.addItem("Left", "left")
        self.band_side_combo.addItem("Right", "right")
        self.band_side_combo.addItem("Top", "top")
        self.band_side_combo.addItem("Bottom", "bottom")
        side_row.addWidget(self.band_side_combo, 1)
        sec_class_layout.addLayout(side_row)

        tol_grid = QHBoxLayout()
        tol_grid.setSpacing(8)
        self.left_tol_label = QLabel("Left tolerance:")
        tol_grid.addWidget(self.left_tol_label)
        self.left_tol_spin = QDoubleSpinBox()
        self.left_tol_spin.setDecimals(3)
        self.left_tol_spin.setRange(0.0, 9999.0)
        self.left_tol_spin.setSingleStep(0.05)
        self.left_tol_spin.setSuffix(" m")
        self.left_tol_spin.setValue(0.20)
        tol_grid.addWidget(self.left_tol_spin, 1)
        sec_class_layout.addLayout(tol_grid)

        tol_grid2 = QHBoxLayout()
        tol_grid2.setSpacing(8)
        self.right_tol_label = QLabel("Right tolerance:")
        tol_grid2.addWidget(self.right_tol_label)
        self.right_tol_spin = QDoubleSpinBox()
        self.right_tol_spin.setDecimals(3)
        self.right_tol_spin.setRange(0.0, 9999.0)
        self.right_tol_spin.setSingleStep(0.05)
        self.right_tol_spin.setSuffix(" m")
        self.right_tol_spin.setValue(0.20)
        tol_grid2.addWidget(self.right_tol_spin, 1)
        sec_class_layout.addLayout(tol_grid2)

        class_row = QHBoxLayout()
        class_row.setSpacing(8)
        self.from_class_combo = QComboBox()
        self.to_class_combo = QComboBox()
        class_row.addWidget(QLabel("From class:"))
        class_row.addWidget(self.from_class_combo, 1)
        sec_class_layout.addLayout(class_row)

        class_row2 = QHBoxLayout()
        class_row2.setSpacing(8)
        class_row2.addWidget(QLabel("To class:"))
        class_row2.addWidget(self.to_class_combo, 1)
        sec_class_layout.addLayout(class_row2)

        self.classify_btn = QPushButton("Classify Points")
        self.classify_btn.setCursor(Qt.PointingHandCursor)
        self.classify_btn.clicked.connect(self._on_classify_band)
        sec_class_layout.addWidget(self.classify_btn)

        self.classify_status = QLabel("No drawing selected")
        self.classify_status.setWordWrap(True)
        self.classify_status.setStyleSheet(
            f"color: {ThemeColors.get('text_secondary')}; font-size: 11px; padding: 2px 0;"
        )
        sec_class_layout.addWidget(self.classify_status)

        self._populate_class_dropdowns()
        self._load_parallel_settings()
        self._sync_band_mode_ui()
        self._sync_band_side_ui()

        root.addWidget(sec_class)
        root.addStretch()

    # ── Side Selection helpers ───────────────────────────────────────────
    def _make_side_btn(self, label: str) -> QPushButton:
        btn = QPushButton(label)
        btn.setProperty("sideBtn", "true")
        btn.setProperty("checked", "false")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setCheckable(False)   # we manage visuals manually via property
        return btn

    def _set_side_active(self, active_btn: QPushButton, side: str):
        """Mark one button as active and refresh all side-button styles."""
        self._selected_side = side
        for btn, s in (
            (self._btn_left,   "left"),
            (self._btn_right,  "right"),
            (self._btn_top,    "top"),
            (self._btn_bottom, "bottom"),
        ):
            is_active = (s == side)
            btn.setProperty("checked", "true" if is_active else "false")
            # Force Qt to re-evaluate the stylesheet
            btn.style().unpolish(btn)
            btn.style().polish(btn)

        # Update hint text
        hints = {
            "left":   "Offset side: Left  (for closed shapes: outward)",
            "right":  "Offset side: Right  (for closed shapes: inward)",
            "top":    "Offset side: Top / Up  (for closed shapes: outward)",
            "bottom": "Offset side: Bottom / Down  (for closed shapes: outward)",
        }
        self._side_hint.setText(hints.get(side, ""))

    def _on_side_selected(self):
        """Called when any side button is clicked."""
        sender = self.sender()
        mapping = {
            id(self._btn_left):   "left",
            id(self._btn_right):  "right",
            id(self._btn_top):    "top",
            id(self._btn_bottom): "bottom",
        }
        side = mapping.get(id(sender), "left")
        self._set_side_active(sender, side)

    # ── Select Figure ───────────────────────────────────────────────────
    def _on_select_figure(self):
        """Enter multi-selection mode; each canvas click adds one figure."""
        if self._select_mode:
            return

        self._select_mode = True
        self.select_btn.setText("Click figures … (press button again to finish)")
        self.select_btn.clicked.disconnect(self._on_select_figure)
        self.select_btn.clicked.connect(self._on_finish_selection)

        # Give VTK a moment to regain focus, then install the observer
        QTimer.singleShot(150, self._install_selection_observer)

    def _install_selection_observer(self):
        """Install a one-shot LeftButtonPress observer for figure picking."""
        if not self._select_mode:
            return
        self._remove_selection_observer()
        interactor = self.digitizer.interactor
        self._old_left_press = interactor.AddObserver(
            "LeftButtonPressEvent", self._on_figure_picked, 1.0
        )
        try:
            interactor.SetFocus()
        except Exception:
            pass

    def _on_finish_selection(self):
        """User pressed the button again — exit selection mode."""
        self._select_mode = False
        self._remove_selection_observer()
        self._clear_hover_highlight()
        try:
            self.select_btn.clicked.disconnect(self._on_finish_selection)
        except Exception:
            pass
        self.select_btn.clicked.connect(self._on_select_figure)
        self.select_btn.setText("Select Figure")
        self._update_figure_label()
        self.raise_()
        self.activateWindow()

    def _on_figure_picked(self, obj, evt):
        """Add the clicked figure to the selection; stay in selection mode."""
        interactor = self.digitizer.interactor
        x, y = interactor.GetEventPosition()
        drawing = self._pick_drawing_at(x, y)
        self._remove_selection_observer()

        if drawing is None or drawing.get("type") == "text":
            # Nothing useful — stay in selection mode
            self._clear_hover_highlight()
            QTimer.singleShot(100, self._install_selection_observer)
            return

        if any(d is drawing for d in self._selected_drawings):
            # Already selected — skip, stay in selection mode
            self._clear_hover_highlight()
            QTimer.singleShot(100, self._install_selection_observer)
            return

        self._selected_drawings.append(drawing)
        self._selected_drawing = drawing
        self._clear_hover_highlight()
        self._highlight_drawing(drawing)
        self._update_figure_label()
        self.apply_btn.setEnabled(True)
        self.classify_btn.setEnabled(True)

        # Reinstall observer to accept the next pick
        QTimer.singleShot(100, self._install_selection_observer)

    def _pick_drawing_at(self, x, y):
        """Return the drawing under the cursor, or None if no drawing is hit."""
        try:
            actor = self.digitizer._pick_actor(x, y)
            if actor:
                for d in self._iter_pickable_drawings():
                    if d.get("actor") is actor:
                        return d
                    for key in ("start_marker", "end_marker"):
                        if d.get(key) is actor:
                            return d
        except Exception:
            pass

        try:
            return self.digitizer._get_drawing_under_cursor(x, y, tolerance=25.0)
        except Exception:
            return None

    def _drawing_coords(self, drawing):
        if hasattr(self.digitizer, "_get_drawing_coords"):
            return self.digitizer._get_drawing_coords(drawing)
        if isinstance(drawing, dict):
            coords = drawing.get("coords")
            if coords is not None:
                return coords
            coords = drawing.get("coordinates")
            if coords is not None:
                return coords
            coords = drawing.get("interpolated")
            if coords is not None:
                return coords
        return []

    def _iter_pickable_drawings(self):
        drawings = list(getattr(self.digitizer, "drawings", []) or [])
        curve_tool = getattr(self.app, "curve_tool", None)
        finalized = getattr(curve_tool, "finalized_actors", None)
        if finalized:
            for curve_data in list(finalized or []):
                coords = self._drawing_coords(curve_data)
                if coords is not None and len(coords) > 0:
                    if not any(curve_data is drawing for drawing in drawings):
                        drawings.append(curve_data)
        return drawings

    def _highlight_hover_drawing(self, drawing):
        actor = drawing.get("actor")
        if actor is None or not hasattr(actor, "GetProperty"):
            self._clear_hover_highlight()
            return

        # Don't hover-paint over an already-selected figure
        if actor in self._selection_saved_props:
            self._clear_hover_highlight()
            return

        if actor is self._hover_highlight_actor:
            return

        self._clear_hover_highlight()
        prop = actor.GetProperty()
        self._hover_orig_color = prop.GetColor()
        self._hover_orig_width = prop.GetLineWidth()
        prop.SetColor(0.0, 0.75, 1.0)
        prop.SetLineWidth(max(self._hover_orig_width * 2, 4))
        self._hover_highlight_actor = actor
        try:
            self.app.vtk_widget.render()
        except Exception:
            pass

    def _clear_hover_highlight(self):
        actor = self._hover_highlight_actor
        if actor is not None and hasattr(actor, "GetProperty"):
            prop = actor.GetProperty()
            if self._hover_orig_color is not None:
                prop.SetColor(*self._hover_orig_color)
            if self._hover_orig_width is not None:
                prop.SetLineWidth(self._hover_orig_width)
            try:
                self.app.vtk_widget.render()
            except Exception:
                pass
        self._hover_highlight_actor = None
        self._hover_orig_color = None
        self._hover_orig_width = None

    def _highlight_drawing(self, drawing):
        """Cyan-highlight one newly selected drawing; idempotent per actor."""
        actor = drawing.get("actor")
        if actor is None or not hasattr(actor, "GetProperty"):
            return
        if actor in self._selection_saved_props:
            return  # already highlighted
        prop = actor.GetProperty()
        self._selection_saved_props[actor] = (prop.GetColor(), prop.GetLineWidth())
        prop.SetColor(0.0, 1.0, 1.0)
        prop.SetLineWidth(max(prop.GetLineWidth() * 2, 4))
        try:
            self.app.vtk_widget.render()
        except Exception:
            pass

    def _clear_highlight(self):
        """Restore original appearance for every highlighted drawing."""
        self._clear_hover_highlight()
        for actor, (color, width) in list(self._selection_saved_props.items()):
            try:
                if hasattr(actor, "GetProperty"):
                    prop = actor.GetProperty()
                    prop.SetColor(*color)
                    prop.SetLineWidth(width)
            except Exception:
                pass
        self._selection_saved_props.clear()
        try:
            self.app.vtk_widget.render()
        except Exception:
            pass

    def _update_figure_label(self):
        count = len(self._selected_drawings)
        if count == 0:
            self.figure_label.setText("No figure selected")
        elif count == 1:
            d = self._selected_drawings[0]
            dtype = d.get("type", "unknown")
            n = len(self._drawing_coords(d))
            self.figure_label.setText(f"Selected: {dtype.capitalize()} ({n} points)")
        else:
            types = ", ".join(
                d.get("type", "unknown").capitalize() for d in self._selected_drawings
            )
            self.figure_label.setText(f"Selected {count} figures: {types}")

    def _on_clear(self):
        """Reset the dialog state without deleting completed drawings."""
        self._remove_selection_observer()
        self._clear_highlight()
        self._selected_drawings.clear()
        self._selected_drawing = None
        self._select_mode = False
        self._reset_select_button()
        self.distance_spin.setValue(0.10)
        self.apply_btn.setEnabled(False)
        self.classify_btn.setEnabled(False)
        self._update_figure_label()
        if not self.isVisible():
            self.show()
        self.raise_()
        self.activateWindow()

    def _reset_select_button(self):
        """Restore the Select Figure button to its default state."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                self.select_btn.clicked.disconnect(self._on_finish_selection)
            except Exception:
                pass
            try:
                self.select_btn.clicked.disconnect(self._on_select_figure)
            except Exception:
                pass
        self.select_btn.clicked.connect(self._on_select_figure)
        self.select_btn.setText("Select Figure")
        self.select_btn.setEnabled(True)

    def _build_parallel_band_mask_xy(self, xyz, polyline_xy, left_tol, right_tol):
        if xyz is None or polyline_xy is None:
            return np.zeros(0, dtype=bool)
        pts = np.asarray(xyz, dtype=np.float64)
        if pts.ndim != 2 or pts.shape[0] == 0:
            return np.zeros(0, dtype=bool)
        line = np.asarray(polyline_xy, dtype=np.float64)
        if line.ndim != 2 or line.shape[0] < 2:
            return np.zeros(len(pts), dtype=bool)

        points_xy = pts[:, :2]
        mask = np.zeros(len(points_xy), dtype=bool)
        left_tol = max(0.0, float(left_tol))
        right_tol = max(0.0, float(right_tol))

        for p0, p1 in zip(line[:-1], line[1:]):
            v = p1 - p0
            seg_len_sq = float(np.dot(v, v))
            if seg_len_sq <= 1e-12:
                continue
            seg_len = float(np.sqrt(seg_len_sq))
            rel = points_xy - p0
            t = (rel @ v) / seg_len_sq
            in_seg = (t >= 0.0) & (t <= 1.0)
            cross = v[0] * rel[:, 1] - v[1] * rel[:, 0]
            signed_distance = cross / seg_len
            in_band = (signed_distance >= -right_tol) & (signed_distance <= left_tol)
            mask |= in_seg & in_band
        return mask

    def _band_mode(self):
        data = self.band_mode_combo.currentData()
        return str(data or "symmetric").lower()

    def _sync_band_mode_ui(self):
        # Symmetric mode keeps a single total width control, while asymmetric
        # mode exposes independent above/below widths through the two spins.
        symmetric = self._band_mode() != "asymmetric"
        self.left_tol_label.setText("Total width:" if symmetric else "Tolerance above:")
        self.right_tol_label.setText("Tolerance below:")
        if symmetric:
            total_width = float(self.left_tol_spin.value())
            half_width = max(0.0, total_width / 2.0)
            self.right_tol_spin.blockSignals(True)
            try:
                self.right_tol_spin.setValue(half_width)
            finally:
                self.right_tol_spin.blockSignals(False)
            self.right_tol_spin.setEnabled(False)
            self.left_tol_spin.setToolTip("Total band width measured across both sides of the line.")
            self.right_tol_spin.setToolTip("Auto-derived as half of the total width.")
        else:
            self.right_tol_spin.setEnabled(True)
            self.left_tol_spin.setToolTip("Distance above / left of the line.")
            self.right_tol_spin.setToolTip("Distance below / right of the line.")

    def _sync_band_side_ui(self):
        try:
            idx = self.band_side_combo.findData(self._selected_side)
            if idx >= 0:
                self.band_side_combo.setCurrentIndex(idx)
        except Exception:
            pass

    def _set_status(self, text):
        self.classify_status.setText(text)
        try:
            if hasattr(self.app, "statusBar") and self.app.statusBar():
                self.app.statusBar().showMessage(text, 3500)
        except Exception:
            pass

    def _refresh_class_lists_from_active_palette(self):
        # Pull the latest PTC/class palette right before classification so the
        # dropdowns follow whatever DisplayMode last applied, but keep the
        # user's live UI edits intact.
        try:
            current_from = self.from_class_combo.currentData()
            current_to = self.to_class_combo.currentData()
            current_mode = self.band_mode_combo.currentData()
            current_side = self.band_side_combo.currentData()
            current_left = float(self.left_tol_spin.value())
            current_right = float(self.right_tol_spin.value())
            self._populate_class_dropdowns()
            if current_from is not None:
                idx = self.from_class_combo.findData(current_from)
                if idx >= 0:
                    self.from_class_combo.setCurrentIndex(idx)
            if current_to is not None:
                idx = self.to_class_combo.findData(current_to)
                if idx >= 0:
                    self.to_class_combo.setCurrentIndex(idx)
            if current_mode is not None:
                idx = self.band_mode_combo.findData(current_mode)
                if idx >= 0:
                    self.band_mode_combo.setCurrentIndex(idx)
            if current_side is not None:
                idx = self.band_side_combo.findData(current_side)
                if idx >= 0:
                    self.band_side_combo.setCurrentIndex(idx)
            self.left_tol_spin.setValue(current_left)
            self.right_tol_spin.setValue(current_right)
            self._sync_band_mode_ui()
            self._sync_band_side_ui()
        except Exception:
            pass

    def _refresh_main_view_after_parallel_classify(self):
        app = self.app
        display_mode = str(getattr(app, "display_mode", "class") or "class").lower()
        rendered = False
        try:
            # Keep the current loaded palette/rendering intact by using the
            # smallest safe redraw hook for the active mode.
            if display_mode == "surface":
                try:
                    from gui.surface_mode import refresh_surface_after_classification
                    refresh_surface_after_classification(
                        app,
                        changed_mask=getattr(app, "_last_changed_mask", None),
                        operation="classification",
                    )
                    rendered = True
                except Exception:
                    pass
            elif display_mode == "shaded_class":
                try:
                    from gui.shading_display import refresh_shaded_after_classification_fast
                    refresh_shaded_after_classification_fast(
                        app,
                        changed_mask=getattr(app, "_last_changed_mask", None),
                    )
                    rendered = True
                except Exception:
                    try:
                        from gui.shading_display import update_shaded_class
                        update_shaded_class(app, force_rebuild=True)
                        rendered = True
                    except Exception:
                        pass
            else:
                if hasattr(app, "_sync_classification_to_gpu"):
                    app._sync_classification_to_gpu()
                    rendered = True
        except Exception:
            pass
        if not rendered:
            try:
                if hasattr(app, "vtk_widget") and app.vtk_widget:
                    app.vtk_widget.render()
            except Exception:
                pass
        try:
            if hasattr(app, "statusBar") and app.statusBar():
                app.statusBar().showMessage("Parallel band classification applied", 2500)
        except Exception:
            pass

    def _on_classify_band(self):
        self._refresh_class_lists_from_active_palette()
        if self._selected_drawing is None and not self._selected_drawings:
            self._set_status("Select a line/polyline first.")
            return
        if self._selected_drawing is None and self._selected_drawings:
            self._selected_drawing = self._selected_drawings[-1]

        drawing = self._selected_drawing
        coords = np.asarray(self._drawing_coords(drawing), dtype=np.float64)
        if coords.ndim != 2 or coords.shape[0] < 2:
            self._set_status("Selected drawing has no usable coordinates.")
            return

        xyz = getattr(self.app, "data", {}).get("xyz") if getattr(self.app, "data", None) else None
        classes = getattr(self.app, "data", {}).get("classification") if getattr(self.app, "data", None) else None
        if xyz is None or classes is None:
            self._set_status("No point cloud loaded.")
            return

        if self._band_mode() == "symmetric":
            left_tol = max(0.0, float(self.left_tol_spin.value()) / 2.0)
            right_tol = left_tol
        else:
            left_tol = float(self.left_tol_spin.value())
            right_tol = float(self.right_tol_spin.value())
        from_class = self.from_class_combo.currentData()
        to_class = self.to_class_combo.currentData()
        if to_class is None:
            self._set_status("Choose a target class.")
            return

        band_mask = self._build_parallel_band_mask_xy(xyz, coords[:, :2], left_tol, right_tol)
        band_points = int(np.count_nonzero(band_mask))
        if band_points == 0:
            self._set_status("No points found inside selected parallel band.")
            return

        if from_class is not None:
            band_mask &= (classes == int(from_class))
        band_mask &= (classes != int(to_class))

        changed_points = int(np.count_nonzero(band_mask))
        print(
            f"[PARALLEL-CLASSIFY] selected_drawing={drawing.get('type', 'unknown')} "
            f"segments={max(0, len(coords) - 1)} left_tol={left_tol:.3f} "
            f"right_tol={right_tol:.3f} from_class={from_class} to_class={to_class} "
            f"band_points={band_points} changed_points={changed_points}"
        )

        if changed_points == 0:
            self._set_status("No points matched the selected class filter.")
            return

        try:
            self._selected_side = str(self.band_side_combo.currentData() or "left")
        except Exception:
            self._selected_side = "left"

        if _apply_classification is None:
            self._set_status("Classification engine unavailable.")
            return

        # Parallel classification is created and selected in the MAIN view.
        # Force the classification engine to apply slot-0 visibility rules,
        # even when cross-section docks exist or retain an active_view value.
        _missing = object()
        _previous_slot_override = getattr(
            self.app,
            "_classification_visibility_slot_override",
            _missing,
        )
        self.app._classification_visibility_slot_override = 0
        try:
            ok = _apply_classification(
                self.app,
                band_mask,
                [int(from_class)] if from_class is not None else None,
                int(to_class),
            )
        except Exception as e:
            self._set_status(f"Classification failed: {e}")
            return
        finally:
            if _previous_slot_override is _missing:
                try:
                    delattr(self.app, "_classification_visibility_slot_override")
                except AttributeError:
                    pass
            else:
                self.app._classification_visibility_slot_override = _previous_slot_override

        if not ok:
            self._set_status("Classification made no changes.")
            return

        self._save_parallel_settings()
        self._refresh_main_view_after_parallel_classify()
        self._set_status(f"[PARALLEL-CLASSIFY] band_points={band_points} changed_points={changed_points}")

    def _class_palette_codes(self):
        palette = getattr(self.app, "class_palette", {}) or {}
        codes = []
        try:
            codes = sorted(int(c) for c in palette.keys())
        except Exception:
            codes = []
        if not codes:
            data = getattr(self.app, "data", None) or {}
            classes = data.get("classification")
            if classes is not None:
                try:
                    codes = sorted(int(c) for c in np.unique(classes))
                except Exception:
                    codes = []
        return codes

    def _class_item_icon(self, color):
        try:
            if isinstance(color, QColor):
                qcolor = color
            else:
                rgb = tuple(int(c) for c in (color or (128, 128, 128))[:3])
                qcolor = QColor(*rgb)
            pix = QPixmap(12, 12)
            pix.fill(qcolor)
            return QIcon(pix)
        except Exception:
            return QIcon()

    def _populate_class_dropdowns(self):
        from_palette = getattr(self.app, "class_palette", {}) or {}
        codes = self._class_palette_codes()
        self.from_class_combo.blockSignals(True)
        self.to_class_combo.blockSignals(True)
        try:
            self.from_class_combo.clear()
            self.to_class_combo.clear()
            self.from_class_combo.addItem("Any class", None)
            for code in codes:
                info = from_palette.get(code, {}) or {}
                desc = (
                    info.get("description")
                    or info.get("lvl")
                    or info.get("draw")
                    or f"Class {code}"
                )
                label = f"{code} - {desc}"
                icon = self._class_item_icon(info.get("color", (128, 128, 128)))
                self.from_class_combo.addItem(icon, label, code)
                self.to_class_combo.addItem(icon, label, code)
        finally:
            self.from_class_combo.blockSignals(False)
            self.to_class_combo.blockSignals(False)

    def _load_parallel_settings(self):
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("NakshaAI", "LidarApp")
            mode = str(s.value("parallel_tool/band_mode", "symmetric"))
            idx = self.band_mode_combo.findData(mode)
            if idx >= 0:
                self.band_mode_combo.setCurrentIndex(idx)
            side = str(s.value("parallel_tool/side", "left"))
            idx = self.band_side_combo.findData(side)
            if idx >= 0:
                self.band_side_combo.setCurrentIndex(idx)
            self.left_tol_spin.setValue(float(s.value("parallel_tool/left_tolerance", 0.20)))
            self.right_tol_spin.setValue(float(s.value("parallel_tool/right_tolerance", 0.20)))
            self._selected_side = str(s.value("parallel_tool/side", "left"))
            from_code = s.value("parallel_tool/from_class", None)
            to_code = s.value("parallel_tool/to_class", None)
            if from_code is not None:
                idx = self.from_class_combo.findData(int(from_code))
                if idx >= 0:
                    self.from_class_combo.setCurrentIndex(idx)
            if to_code is not None:
                idx = self.to_class_combo.findData(int(to_code))
                if idx >= 0:
                    self.to_class_combo.setCurrentIndex(idx)
        except Exception:
            pass

    def _save_parallel_settings(self):
        try:
            from PySide6.QtCore import QSettings
            s = QSettings("NakshaAI", "LidarApp")
            s.setValue("parallel_tool/band_mode", self.band_mode_combo.currentData())
            s.setValue("parallel_tool/left_tolerance", float(self.left_tol_spin.value()))
            s.setValue("parallel_tool/right_tolerance", float(self.right_tol_spin.value()))
            s.setValue("parallel_tool/side", self.band_side_combo.currentData())
            s.setValue("parallel_tool/from_class", self.from_class_combo.currentData())
            s.setValue("parallel_tool/to_class", self.to_class_combo.currentData())
        except Exception:
            pass

    # ── Apply ───────────────────────────────────────────────────────────
    def _on_apply(self):
        """Generate a parallel figure for every selected drawing, then close."""
        if not self._selected_drawings:
            return

        distance_m = self.distance_spin.value()
        try:
            self._selected_side = str(self.band_side_combo.currentData() or "left")
        except Exception:
            self._selected_side = "left"

        failed = 0
        for drawing in list(self._selected_drawings):
            coords = list(self._drawing_coords(drawing))
            dtype = drawing.get("type", "")

            if len(coords) < 2:
                failed += 1
                continue

            new_coords = self._compute_parallel_coords(coords, dtype, distance_m, self._selected_side)
            if not new_coords or len(new_coords) < 2:
                failed += 1
                continue

            self._create_parallel_drawing(new_coords, dtype, drawing)

        if failed:
            self.figure_label.setText(
                f"{failed} figure(s) could not be offset — check geometry."
            )

        # Clean up and close
        self._remove_selection_observer()
        self._clear_highlight()
        self._selected_drawings.clear()
        self.accept()

    def _compute_parallel_coords(self, coords, dtype, distance_m, side="left"):
        """Compute offset coordinates for the parallel figure.

        ``side`` controls the direction of the offset for **open** polylines:
          - "left"   → offset to the left of the direction of travel  (+normal)
          - "right"  → offset to the right                            (-normal)
          - "top"    → offset upward  in world-Y                      (+Y bias)
          - "bottom" → offset downward in world-Y                     (-Y bias)

        For **closed** figures the sign flips inward/outward:
          - "left" / "top" / "bottom" → outward  (positive distance)
          - "right"                   → inward    (negative distance)
        """
        is_closed = self._is_closed_figure(coords, dtype)

        # --- Signed distance based on side ---
        if is_closed:
            # right = inward for closed shapes; everything else = outward
            signed_dist = -distance_m if side == "right" else distance_m
        else:
            # For open lines the normal is the left-perpendicular of travel.
            # "right"  flips it; "top"/"bottom" we handle via _world_normal_sign.
            signed_dist = distance_m  # adjusted below for open lines

        if dtype == "circle":
            return self._parallel_circle(coords, signed_dist, is_closed)

        if is_closed:
            return self._parallel_closed_polygon(coords, signed_dist)

        # --- Open polyline: resolve effective sign ---
        # _segment_normal returns the left-hand perpendicular.
        # left  → use as-is  (+1)
        # right → flip       (-1)
        # top   → we want the component that pushes up; post-process below
        # bottom→ push down; post-process below
        if side in ("left", "right"):
            sign = 1.0 if side == "left" else -1.0
            return self._parallel_open_polyline(coords, distance_m * sign)

        # Top / Bottom still use the true parallel offset path so the result
        # remains geometrically parallel to the source line in world XY.
        # We keep the dropdown labels for UX, but the geometry stays exact.
        sign = 1.0 if side == "top" else -1.0
        return self._parallel_open_polyline(coords, distance_m * sign)

    def _resolve_source_style(self, drawing):
        """Use the source drawing's persisted style so generated geometry restores correctly."""
        style = dict(self.digitizer._get_draw_style("line") or {})
        color = drawing.get("original_color")
        width = drawing.get("original_width")
        line_style = drawing.get("original_style")

        actor = drawing.get("actor")
        if actor is not None and hasattr(actor, "GetProperty"):
            try:
                prop = actor.GetProperty()
                if color is None:
                    color = tuple(float(v) for v in prop.GetColor())
                if width is None:
                    width = float(prop.GetLineWidth())
            except Exception:
                pass

        if color is None:
            color = style.get("color", (1.0, 0.0, 0.0))
        else:
            color = tuple(float(v) for v in color[:3])

        if width is None:
            width = style.get("width", 2)
        try:
            width = max(float(width), 1.0)
        except Exception:
            width = float(style.get("width", 2))

        if not line_style:
            line_style = style.get("style", "solid")

        return color, width, line_style

    # ── Geometry helpers ────────────────────────────────────────────────

    @staticmethod
    def _is_closed_figure(coords, dtype):
        if dtype in ("rectangle", "polygon", "circle"):
            return True
        if len(coords) < 3:
            return False
        p0 = np.array(coords[0][:2], dtype=np.float64)
        pn = np.array(coords[-1][:2], dtype=np.float64)
        return np.linalg.norm(p0 - pn) < 1e-6

    @staticmethod
    def _segment_normal(p1, p2):
        """Return the unit left normal for p1 to p2."""
        d = np.array(p2[:2], dtype=np.float64) - np.array(p1[:2], dtype=np.float64)
        length = np.linalg.norm(d)
        if length < 1e-12:
            return np.array([0.0, 0.0])
        n = np.array([-d[1], d[0]], dtype=np.float64) / length
        return n

    @staticmethod
    def _line_intersection_xy(a1, a2, b1, b2):
        """Return XY intersection of two infinite 2D lines, or None if parallel."""
        p = np.array(a1[:2], dtype=np.float64)
        r = np.array(a2[:2], dtype=np.float64) - p
        q = np.array(b1[:2], dtype=np.float64)
        s = np.array(b2[:2], dtype=np.float64) - q
        denom = r[0] * s[1] - r[1] * s[0]
        if abs(float(denom)) < 1e-12:
            return None
        qp = q - p
        t = (qp[0] * s[1] - qp[1] * s[0]) / denom
        return p + t * r

    @staticmethod
    def _signed_area_xy(coords):
        area = 0.0
        for i in range(len(coords)):
            x1, y1 = coords[i][0], coords[i][1]
            x2, y2 = coords[(i + 1) % len(coords)][0], coords[(i + 1) % len(coords)][1]
            area += float(x1) * float(y2) - float(x2) * float(y1)
        return area * 0.5

    @staticmethod
    def _offset_point(point, normal, distance_m):
        p = np.array(point[:2], dtype=np.float64) + normal * distance_m
        z = float(point[2]) if len(point) > 2 else 0.0
        return (float(p[0]), float(p[1]), z)

    def _parallel_open_polyline(self, coords, distance_m):
        """Offset an open polyline by exactly distance_m from each segment."""
        if len(coords) < 2:
            return []

        normals = [
            self._segment_normal(coords[i], coords[i + 1])
            for i in range(len(coords) - 1)
        ]

        result = [self._offset_point(coords[0], normals[0], distance_m)]
        for i in range(1, len(coords) - 1):
            n_prev = normals[i - 1]
            n_next = normals[i]
            prev_a = self._offset_point(coords[i - 1], n_prev, distance_m)
            prev_b = self._offset_point(coords[i], n_prev, distance_m)
            next_a = self._offset_point(coords[i], n_next, distance_m)
            next_b = self._offset_point(coords[i + 1], n_next, distance_m)
            xy = self._line_intersection_xy(prev_a, prev_b, next_a, next_b)
            if xy is None:
                avg = n_prev + n_next
                norm = np.linalg.norm(avg)
                normal = avg / norm if norm > 1e-12 else n_prev
                result.append(self._offset_point(coords[i], normal, distance_m))
            else:
                z = float(coords[i][2]) if len(coords[i]) > 2 else 0.0
                result.append((float(xy[0]), float(xy[1]), z))

        result.append(self._offset_point(coords[-1], normals[-1], distance_m))
        return result

    def _parallel_open_polyline_world_y(self, coords, distance_m, up: bool = True):
        """Offset an open polyline by ``distance_m`` purely in world-Y direction.

        Used for the Top / Bottom side selections so the user gets an intuitive
        vertical offset regardless of the line's direction of travel.
        """
        dy = distance_m if up else -distance_m
        result = []
        for pt in coords:
            x = float(pt[0])
            y = float(pt[1]) + dy
            z = float(pt[2]) if len(pt) > 2 else 0.0
            result.append((x, y, z))
        return result

    def _parallel_closed_polygon(self, coords, distance_m):
        # Use only the unique vertices (remove closing duplicate if present)
        if self._points_equal(coords[0], coords[-1]):
            verts = coords[:-1]
        else:
            verts = coords
        nv = len(verts)
        if nv < 3:
            return self._parallel_open_polyline(verts, distance_m)

        normal_sign = -1.0 if self._signed_area_xy(verts) > 0.0 else 1.0
        normals = [
            self._segment_normal(verts[i], verts[(i + 1) % nv]) * normal_sign
            for i in range(nv)
        ]
        result = []
        for i in range(nv):
            prev_i = (i - 1) % nv
            next_i = (i + 1) % nv
            n_prev = normals[prev_i]
            n_curr = normals[i]
            prev_a = self._offset_point(verts[prev_i], n_prev, distance_m)
            prev_b = self._offset_point(verts[i], n_prev, distance_m)
            curr_a = self._offset_point(verts[i], n_curr, distance_m)
            curr_b = self._offset_point(verts[next_i], n_curr, distance_m)
            xy = self._line_intersection_xy(prev_a, prev_b, curr_a, curr_b)
            if xy is None:
                avg = n_prev + n_curr
                norm = np.linalg.norm(avg)
                normal = avg / norm if norm > 1e-12 else n_curr
                result.append(self._offset_point(verts[i], normal, distance_m))
            else:
                z = float(verts[i][2]) if len(verts[i]) > 2 else 0.0
                result.append((float(xy[0]), float(xy[1]), z))

        # Close the polygon
        result.append(result[0])
        return result

    def _parallel_circle(self, coords, distance_m, is_closed):
        """Offset a circle by adjusting its radius."""
        if len(coords) < 2:
            return []
        if len(coords) > 2 and self._points_equal(coords[0], coords[-1]):
            coords = coords[:-1]
        # Compute center and radius from the coordinates
        pts = np.array([c[:2] for c in coords], dtype=np.float64)
        cx = float(np.mean(pts[:, 0]))
        cy = float(np.mean(pts[:, 1]))

        # Compute average radius
        radii = np.sqrt((pts[:, 0] - cx) ** 2 + (pts[:, 1] - cy) ** 2)
        avg_radius = float(np.mean(radii))

        new_radius = avg_radius + distance_m
        if new_radius <= 0:
            return []

        # Generate the new circle using the original angular samples.
        result = []
        for coord in coords:
            vec = np.array(coord[:2], dtype=np.float64) - np.array([cx, cy], dtype=np.float64)
            length = np.linalg.norm(vec)
            if length < 1e-12:
                continue
            unit = vec / length
            x = float(cx + new_radius * unit[0])
            y = float(cy + new_radius * unit[1])
            z = float(coord[2]) if len(coord) > 2 else 0.0
            result.append((x, y, z))
        if len(result) < 2:
            return []
        result.append(result[0])  # close
        return result

    @staticmethod
    def _points_equal(p1, p2, tol=1e-6):
        return (
            abs(float(p1[0]) - float(p2[0])) < tol
            and abs(float(p1[1]) - float(p2[1])) < tol
        )

    # ── Create the drawing ──────────────────────────────────────────────

    def _create_parallel_drawing(self, new_coords, orig_dtype, orig_drawing):
        """Create a new drawing entry for the parallel figure."""
        import vtk

        color, width, line_style = self._resolve_source_style(orig_drawing)

        # Build the VTK actor
        actor = self.digitizer._make_polyline_actor(
            new_coords, color=color, width=width, line_style=line_style
        )
        self.digitizer._add_actor_to_overlay(actor)

        # Determine the type label
        if orig_dtype == "rectangle":
            new_type = "polygon"
        elif orig_dtype in ("line_segment", "smartline", "curve"):
            new_type = "parallel_line"
        else:
            new_type = "parallel_" + orig_dtype

        drawing_entry = {
            "type": new_type,
            "coords": list(new_coords),
            "actor": actor,
            "bounds": actor.GetBounds(),
            "original_color": color,
            "original_width": width,
            "original_style": line_style,
            "generated_from": orig_dtype,
        }

        self.digitizer._save_state()
        self.digitizer.drawings.append(drawing_entry)
        self.digitizer._notify_selection_drawings_changed()
        self.digitizer._emit_drawing_finalized(drawing_entry)
        self.digitizer.renderer.Modified()
        self.app.vtk_widget.render()

        print(
            f"✅ Parallel figure created: {new_type} "
            f"({len(new_coords)} points, offset={self.distance_spin.value()} m)"
        )

    # ── Cleanup on close ────────────────────────────────────────────────
    def closeEvent(self, event):
        """Clean up observer and highlight when dialog is closed."""
        try:
            self._save_parallel_settings()
        except Exception:
            pass
        self._cleanup_chip()
        self._remove_selection_observer()
        self._clear_highlight()
        self._selected_drawings.clear()
        self._select_mode = False
        super().closeEvent(event)

    def reject(self):
        """Clean up when Escape is pressed."""
        self._remove_selection_observer()
        self._clear_highlight()
        self._selected_drawings.clear()
        self._select_mode = False
        super().reject()

    def _remove_selection_observer(self):
        interactor = self.digitizer.interactor
        if self._old_left_press is not None:
            try:
                interactor.RemoveObserver(self._old_left_press)
            except Exception:
                pass
            self._old_left_press = None
        if self._old_mouse_move is not None:
            try:
                interactor.RemoveObserver(self._old_mouse_move)
            except Exception:
                pass
            self._old_mouse_move = None
