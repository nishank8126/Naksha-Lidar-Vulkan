"""
gui/centerline_tool_dialog.py
────────────────────────────────
Centerline Tool: select two open line drawings and generate an interpolated,
open-ended centerline between them.
"""

from __future__ import annotations

import warnings

import numpy as np

try:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
        QSpinBox, QGroupBox, QFrame,
    )
except ImportError:
    from PyQt5.QtCore import Qt, QTimer
    from PyQt5.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
        QSpinBox, QGroupBox, QFrame,
    )

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


def _get_centerline_dialog_style():
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
QSpinBox {{
    background-color: {ThemeColors.get('bg_input')};
    color: {ThemeColors.get('text_primary')};
    border: 1px solid {ThemeColors.get('border_light')};
    border-radius: 6px;
    padding: 6px 10px;
    min-height: 28px;
    font-size: 13px;
}}
QSpinBox:hover {{
    border: 1px solid {ThemeColors.get('accent')};
    background-color: {ThemeColors.get('bg_button_hover')};
}}
QSpinBox::up-button, QSpinBox::down-button {{
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
"""


class CenterlineToolDialog(MinimizableDialogMixin, QDialog):
    """
    Dialog for the Centerline workflow:
      1. Select exactly two open line features
      2. Interpolate matching stations along both features
      3. Create one open centerline midway between them
    """

    DEFAULT_SAMPLE_COUNT = 96
    MAX_SAMPLE_COUNT = 1000
    CLOSED_TYPES = {"text", "circle", "rectangle", "polygon"}

    def __init__(self, digitizer, parent=None):
        super().__init__(None, Qt.Window)
        self._init_minimize_state()
        self.digitizer = digitizer
        self.app = digitizer.app
        self._selected_drawings = []
        self._selection_saved_props = {}
        self._select_mode = False
        self._old_left_press = None
        self._old_mouse_move = None
        self._hover_highlight_actor = None
        self._hover_orig_color = None
        self._hover_orig_width = None

        self.setWindowTitle("Centerline Tool")
        self.setWindowFlags(
            Qt.Dialog
            | Qt.WindowCloseButtonHint
            | Qt.WindowMaximizeButtonHint
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_QuitOnClose, False)
        self.setMinimumWidth(370)
        self.resize(370, 280)
        self.setStyleSheet(_get_centerline_dialog_style())
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 5, 18, 10)
        root.setSpacing(12)

        line_group = QGroupBox("Lines")
        line_layout = QVBoxLayout(line_group)
        line_layout.setContentsMargins(15, 10, 15, 10)
        line_layout.setSpacing(8)

        self.select_btn = QPushButton("Select 2 Lines")
        self.select_btn.setCursor(Qt.PointingHandCursor)
        self.select_btn.clicked.connect(self._on_select_lines)
        line_layout.addWidget(self.select_btn)

        self.lines_label = QLabel("No lines selected")
        self.lines_label.setWordWrap(True)
        self.lines_label.setStyleSheet(
            f"color: {ThemeColors.get('text_secondary')}; font-size: 11px; font-weight: 500; padding: 4px 0;"
        )
        line_layout.addWidget(self.lines_label)

        root.addWidget(line_group)

        options_group = QGroupBox("Interpolation")
        options_layout = QVBoxLayout(options_group)
        options_layout.setContentsMargins(15, 10, 15, 10)
        options_layout.setSpacing(8)

        samples_row = QHBoxLayout()
        samples_row.setSpacing(8)
        samples_label = QLabel("Samples:")
        samples_label.setStyleSheet(
            f"color: {ThemeColors.get('text_primary')}; font-size: 12px;"
        )
        samples_row.addWidget(samples_label)

        self.samples_spin = QSpinBox()
        self.samples_spin.setRange(2, self.MAX_SAMPLE_COUNT)
        self.samples_spin.setSingleStep(8)
        self.samples_spin.setValue(self.DEFAULT_SAMPLE_COUNT)
        samples_row.addWidget(self.samples_spin, 1)
        options_layout.addLayout(samples_row)

        hint = QLabel("Uses normalized distance along each selected line.")
        hint.setWordWrap(True)
        hint.setStyleSheet(
            f"color: {ThemeColors.get('text_secondary')}; font-size: 11px; font-weight: 500;"
        )
        options_layout.addWidget(hint)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        options_layout.addWidget(line)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)

        self.apply_btn = QPushButton("Generate")
        self.apply_btn.setCursor(Qt.PointingHandCursor)
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self._on_apply)
        action_row.addWidget(self.apply_btn, 1)

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setCursor(Qt.PointingHandCursor)
        self.clear_btn.clicked.connect(self._on_clear)
        action_row.addWidget(self.clear_btn, 1)

        options_layout.addLayout(action_row)
        root.addWidget(options_group)
        root.addStretch()

    def _on_select_lines(self):
        if self._select_mode:
            return
        if len(self._selected_drawings) >= 2:
            self._on_clear()

        self._select_mode = True
        self.select_btn.setText("Click open lines...")
        self.select_btn.clicked.disconnect(self._on_select_lines)
        self.select_btn.clicked.connect(self._on_finish_selection)
        QTimer.singleShot(150, self._install_selection_observer)

    def _install_selection_observer(self):
        if not self._select_mode:
            return
        self._remove_selection_observer()
        interactor = self.digitizer.interactor
        self._old_left_press = interactor.AddObserver(
            "LeftButtonPressEvent", self._on_line_picked, 1.0
        )
        self._old_mouse_move = interactor.AddObserver(
            "MouseMoveEvent", self._on_line_hovered, 1.0
        )
        try:
            interactor.SetFocus()
        except Exception:
            pass

    def _on_finish_selection(self):
        self._select_mode = False
        self._remove_selection_observer()
        self._clear_hover_highlight()
        self._reset_select_button()
        self._update_lines_label()
        self.raise_()
        self.activateWindow()

    def _on_line_picked(self, obj, evt):
        interactor = self.digitizer.interactor
        x, y = interactor.GetEventPosition()
        drawing = self._pick_drawing_at(x, y)
        self._remove_selection_observer()

        if not self._is_open_line_feature(drawing):
            self._clear_hover_highlight()
            self._show_message("Pick two open line features only.")
            QTimer.singleShot(100, self._install_selection_observer)
            return

        if any(d is drawing for d in self._selected_drawings):
            self._clear_hover_highlight()
            self._show_message("That line is already selected.")
            QTimer.singleShot(100, self._install_selection_observer)
            return

        self._selected_drawings.append(drawing)
        self._clear_hover_highlight()
        self._highlight_drawing(drawing)
        self._update_lines_label()
        self.apply_btn.setEnabled(len(self._selected_drawings) == 2)

        if len(self._selected_drawings) >= 2:
            self._on_finish_selection()
            return

        QTimer.singleShot(100, self._install_selection_observer)

    def _on_line_hovered(self, obj, evt):
        if not self._select_mode:
            return
        interactor = self.digitizer.interactor
        x, y = interactor.GetEventPosition()
        drawing = self._pick_drawing_at(x, y)
        if not self._is_open_line_feature(drawing):
            self._clear_hover_highlight()
            return
        self._highlight_hover_drawing(drawing)

    def _pick_drawing_at(self, x, y):
        try:
            actor = self.digitizer._pick_actor(x, y)
            if actor:
                for drawing in self._iter_pickable_drawings():
                    if drawing.get("actor") is actor:
                        return drawing
                    for key in ("start_marker", "end_marker"):
                        if drawing.get(key) is actor:
                            return drawing
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

    def _is_open_line_feature(self, drawing):
        if not isinstance(drawing, dict):
            return False
        dtype = str(drawing.get("type", "") or "").lower()
        if dtype in self.CLOSED_TYPES:
            return False
        coords = self._drawing_coords(drawing)
        if len(coords) < 2:
            return False
        if self._points_equal_xy(coords[0], coords[-1]):
            return False
        return True

    def _highlight_hover_drawing(self, drawing):
        actor = drawing.get("actor")
        if actor is None or not hasattr(actor, "GetProperty"):
            self._clear_hover_highlight()
            return
        if actor in self._selection_saved_props:
            self._clear_hover_highlight()
            return
        if actor is self._hover_highlight_actor:
            return

        self._clear_hover_highlight()
        prop = actor.GetProperty()
        self._hover_orig_color = prop.GetColor()
        self._hover_orig_width = prop.GetLineWidth()
        prop.SetColor(1.0, 0.55, 0.0)
        prop.SetLineWidth(max(self._hover_orig_width * 2, 4))
        self._hover_highlight_actor = actor
        self._render()

    def _clear_hover_highlight(self):
        actor = self._hover_highlight_actor
        if actor is not None and hasattr(actor, "GetProperty"):
            prop = actor.GetProperty()
            if self._hover_orig_color is not None:
                prop.SetColor(*self._hover_orig_color)
            if self._hover_orig_width is not None:
                prop.SetLineWidth(self._hover_orig_width)
            self._render()
        self._hover_highlight_actor = None
        self._hover_orig_color = None
        self._hover_orig_width = None

    def _highlight_drawing(self, drawing):
        actor = drawing.get("actor")
        if actor is None or not hasattr(actor, "GetProperty"):
            return
        if actor in self._selection_saved_props:
            return
        prop = actor.GetProperty()
        self._selection_saved_props[actor] = (prop.GetColor(), prop.GetLineWidth())
        prop.SetColor(0.0, 1.0, 1.0)
        prop.SetLineWidth(max(prop.GetLineWidth() * 2, 4))
        self._render()

    def _clear_highlight(self):
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
        self._render()

    def _update_lines_label(self):
        count = len(self._selected_drawings)
        if count == 0:
            self.lines_label.setText("No lines selected")
        elif count == 1:
            drawing = self._selected_drawings[0]
            dtype = drawing.get("type", "line")
            n = len(self._drawing_coords(drawing))
            self.lines_label.setText(f"Selected 1/2: {dtype} ({n} points)")
        else:
            parts = []
            for drawing in self._selected_drawings[:2]:
                dtype = drawing.get("type", "line")
                n = len(self._drawing_coords(drawing))
                parts.append(f"{dtype} ({n} pts)")
            self.lines_label.setText("Selected 2/2: " + " + ".join(parts))

    def _on_clear(self):
        self._remove_selection_observer()
        self._clear_highlight()
        self._selected_drawings.clear()
        self._select_mode = False
        self._reset_select_button()
        self.samples_spin.setValue(self.DEFAULT_SAMPLE_COUNT)
        self.apply_btn.setEnabled(False)
        self._update_lines_label()
        if not self.isVisible():
            self.show()
        self.raise_()
        self.activateWindow()

    def _reset_select_button(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                self.select_btn.clicked.disconnect(self._on_finish_selection)
            except Exception:
                pass
            try:
                self.select_btn.clicked.disconnect(self._on_select_lines)
            except Exception:
                pass
        self.select_btn.clicked.connect(self._on_select_lines)
        self.select_btn.setText("Select 2 Lines")
        self.select_btn.setEnabled(True)

    def _on_apply(self):
        if len(self._selected_drawings) != 2:
            return

        first, second = self._selected_drawings[:2]
        center_coords = self._compute_centerline_coords(
            self._drawing_coords(first),
            self._drawing_coords(second),
            self.samples_spin.value(),
        )
        if len(center_coords) < 2:
            self.lines_label.setText("Could not generate centerline - check geometry.")
            return

        self._create_centerline_drawing(center_coords, first, second)
        self._remove_selection_observer()
        self._clear_highlight()
        self._selected_drawings.clear()
        self.accept()

    @classmethod
    def _compute_centerline_coords(cls, first_coords, second_coords, sample_count):
        first = cls._clean_points(first_coords)
        second = cls._clean_points(second_coords)
        if len(first) < 2 or len(second) < 2:
            return []

        first_total = cls._polyline_total_length(first)
        second_total = cls._polyline_total_length(second)
        if first_total <= 1e-12 or second_total <= 1e-12:
            return []

        second = cls._align_second_line_direction(first, second)
        stations = cls._build_sample_stations(first, second, sample_count)

        result = []
        for fraction in stations:
            p1 = cls._sample_polyline_at_fraction(first, fraction)
            p2 = cls._sample_polyline_at_fraction(second, fraction)
            midpoint = (p1 + p2) * 0.5
            result.append((float(midpoint[0]), float(midpoint[1]), float(midpoint[2])))

        return cls._dedupe_points(result)

    def _get_centerline_style(self):
        """Use the dedicated Centerline tool style from draw settings."""
        style = dict(self.digitizer._get_draw_style("centerline") or {})
        color = style.get("color", (1.0, 0.0, 0.0))
        width = style.get("width", 2)
        line_style = style.get("style", "solid")

        try:
            color = tuple(float(v) for v in color[:3])
        except Exception:
            color = (1.0, 0.0, 0.0)

        try:
            width = max(float(width), 1.0)
        except Exception:
            width = 2.0

        return color, width, line_style

    @staticmethod
    def _as_xyz(point):
        x = float(point[0])
        y = float(point[1])
        z = float(point[2]) if len(point) > 2 else 0.0
        return np.array([x, y, z], dtype=np.float64)

    @classmethod
    def _clean_points(cls, coords):
        points = []
        for coord in coords or []:
            try:
                point = cls._as_xyz(coord)
            except Exception:
                continue
            if points and np.linalg.norm(point - points[-1]) <= 1e-12:
                continue
            points.append(point)
        return points

    @staticmethod
    def _segment_length(p1, p2):
        xy_len = float(np.linalg.norm((p2 - p1)[:2]))
        if xy_len > 1e-12:
            return xy_len
        return float(np.linalg.norm(p2 - p1))

    @classmethod
    def _polyline_lengths(cls, points):
        lengths = [0.0]
        total = 0.0
        for i in range(1, len(points)):
            total += cls._segment_length(points[i - 1], points[i])
            lengths.append(total)
        return lengths, total

    @classmethod
    def _polyline_total_length(cls, points):
        return cls._polyline_lengths(points)[1]

    @classmethod
    def _align_second_line_direction(cls, first, second):
        same = cls._segment_length(first[0], second[0]) + cls._segment_length(first[-1], second[-1])
        crossed = cls._segment_length(first[0], second[-1]) + cls._segment_length(first[-1], second[0])
        if crossed < same:
            return list(reversed(second))
        return second

    @classmethod
    def _build_sample_stations(cls, first, second, sample_count):
        count = max(2, min(int(sample_count), cls.MAX_SAMPLE_COUNT))
        stations = {float(v) for v in np.linspace(0.0, 1.0, count)}

        for points in (first, second):
            lengths, total = cls._polyline_lengths(points)
            if total <= 1e-12:
                continue
            for distance in lengths:
                stations.add(max(0.0, min(1.0, distance / total)))

        ordered = sorted(stations)
        if len(ordered) > cls.MAX_SAMPLE_COUNT:
            ordered = [float(v) for v in np.linspace(0.0, 1.0, cls.MAX_SAMPLE_COUNT)]
        return ordered

    @classmethod
    def _sample_polyline_at_fraction(cls, points, fraction):
        lengths, total = cls._polyline_lengths(points)
        if total <= 1e-12:
            return points[0]

        target = max(0.0, min(1.0, float(fraction))) * total
        if target <= 0.0:
            return points[0]
        if target >= total:
            return points[-1]

        for i in range(1, len(points)):
            prev_len = lengths[i - 1]
            curr_len = lengths[i]
            if target <= curr_len:
                seg_len = curr_len - prev_len
                if seg_len <= 1e-12:
                    return points[i]
                local_t = (target - prev_len) / seg_len
                return points[i - 1] + (points[i] - points[i - 1]) * local_t

        return points[-1]

    @classmethod
    def _dedupe_points(cls, coords):
        clean = []
        for coord in coords:
            point = cls._as_xyz(coord)
            if clean and np.linalg.norm(point - cls._as_xyz(clean[-1])) <= 1e-9:
                continue
            clean.append((float(point[0]), float(point[1]), float(point[2])))
        return clean

    @staticmethod
    def _points_equal_xy(p1, p2, tol=1e-6):
        try:
            return (
                abs(float(p1[0]) - float(p2[0])) < tol
                and abs(float(p1[1]) - float(p2[1])) < tol
            )
        except Exception:
            return False

    def _create_centerline_drawing(self, coords, first, second):
        color, width, line_style = self._get_centerline_style()
        source_layers = {
            str(first.get("layer", "") or "").strip(),
            str(second.get("layer", "") or "").strip(),
        }
        source_layers.discard("")
        source_layers.discard("DIGITIZER")
        layer_name = None
        if len(source_layers) == 1:
            layer_name = next(iter(source_layers))
        elif "CL" in source_layers:
            layer_name = "CL"

        actor = self.digitizer._make_polyline_actor(
            coords, color=color, width=width, line_style=line_style
        )
        actor.PickableOn()
        self.digitizer._add_actor_to_overlay(actor)

        drawing_entry = {
            "type": "centerline",
            "coords": list(coords),
            "actor": actor,
            "bounds": actor.GetBounds(),
            "original_color": color,
            "original_width": width,
            "original_style": line_style,
            "layer": layer_name,
            "snt_file": getattr(getattr(self.digitizer, "app", None), "active_snt_filename", None),
            "source_types": [
                first.get("type", "line"),
                second.get("type", "line"),
            ],
            "generated_from": "centerline_tool",
        }

        self.digitizer._save_state()
        self.digitizer.drawings.append(drawing_entry)
        self.digitizer._notify_selection_drawings_changed()
        self.digitizer._emit_drawing_finalized(drawing_entry)
        self.digitizer.renderer.Modified()
        self._render()

        print(
            f"Centerline created: {len(coords)} points "
            f"from {first.get('type', 'line')} + {second.get('type', 'line')}"
        )

    def _show_message(self, text, timeout=2500):
        self.lines_label.setText(text)
        try:
            self.app.statusBar().showMessage(text, timeout)
        except Exception:
            pass

    def _render(self):
        try:
            self.app.vtk_widget.render()
        except Exception:
            pass

    def closeEvent(self, event):
        self._cleanup_chip()
        self._remove_selection_observer()
        self._clear_highlight()
        self._selected_drawings.clear()
        self._select_mode = False
        super().closeEvent(event)

    def reject(self):
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
