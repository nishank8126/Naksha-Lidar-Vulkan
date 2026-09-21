from ast import Pass
import os
import uuid
import vtk
import numpy as np
import math
import time
try:
    from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                                    QLineEdit, QSpinBox, QFontComboBox, QComboBox,
                                    QCheckBox, QPushButton, QGroupBox, QColorDialog,QRadioButton, QMenu,
                                    QMessageBox)
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QCursor
except ImportError:
    try:
        from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                                       QLineEdit, QSpinBox, QFontComboBox, QComboBox,
                                       QCheckBox, QPushButton, QGroupBox, QColorDialog, QMenu,
                                       QMessageBox)
        from PyQt5.QtCore import Qt
        from PyQt5.QtGui import QColor, QCursor
    except ImportError:
        print("⚠️ Qt library not found - text editing will be limited")

try:
    from gui.theme_manager import get_dialog_stylesheet, ThemeColors as _TC
except Exception:
    def get_dialog_stylesheet(): return ""
    class _TC:
        @staticmethod
        def get(k): return ""

class DrawingList(list):
    def __init__(self, parent_tool, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.parent_tool = parent_tool

    def _app(self):
        return getattr(self.parent_tool, "app", None)

    @staticmethod
    def _sbm_layer_name(active_sbm_class):
        explicit_layer = str(active_sbm_class.get("layer") or "").strip()
        if explicit_layer:
            return explicit_layer
        category = str(active_sbm_class.get("category") or "").strip()
        lv = str(active_sbm_class.get("lv") or active_sbm_class.get("label") or "").strip()
        if category and lv:
            return f"{category} - {lv}"
        return lv or "SBM"

    def append(self, item):
        if isinstance(item, dict):
            # ✅ PERF: stable per-drawing id, stamped once at the single choke
            # point every drawing dict passes through. Lets undo/redo diff
            # "what actually changed" instead of rebuilding everything.
            if "_uid" not in item:
                item["_uid"] = uuid.uuid4().hex
            app = self._app()
            active_sbm_class = getattr(app, "active_sbm_class", None)
            sbm_active = isinstance(active_sbm_class, dict) and bool(active_sbm_class.get("lv"))
            # A drawing that ALREADY carries a layer is not a fresh draw — it is
            # being re-added by undo/redo restore (or already tagged elsewhere).
            # Such drawings must keep their own layer + colour and must NOT be
            # retagged/recoloured to the CURRENT active layer (that was the
            # "everything turns yellow after undo" bug).
            had_layer = "layer" in item
            if sbm_active:
                if "sbm_class" not in item:
                    item["sbm_class"] = dict(active_sbm_class)
                if "layer" not in item:
                    item["layer"] = self._sbm_layer_name(active_sbm_class)
            if "layer" not in item:
                item["layer"] = getattr(app, "active_snt_layer", None) or getattr(app, "active_dxf_layer", None) or "DIGITIZER"
            active_attachment_file = getattr(app, "active_snt_filename", None) or getattr(app, "active_dxf_filename", None)
            # Defensive fallback: if no file is explicitly active but exactly
            # one SNT attachment is loaded, use that one. Otherwise drawings
            # would never get their `snt_file` tagged and the Level Manager
            # would silently show 0 entities for the layer they were drawn
            # on (the post-Clear "silent failure").
            if not had_layer and not active_attachment_file and "snt_file" not in item and "dxf_file" not in item:
                try:
                    snt_attachments = list(getattr(app, "snt_attachments", []) or [])
                    if len(snt_attachments) == 1:
                        only = snt_attachments[0]
                        if isinstance(only, dict):
                            only_full = only.get("full_path") or only.get("filename")
                            if only_full:
                                active_attachment_file = os.path.basename(str(only_full))
                except Exception:
                    pass
            if not had_layer and active_attachment_file:
                if getattr(app, "active_dxf_filename", None) and "dxf_file" not in item:
                    item["dxf_file"] = active_attachment_file
                elif "snt_file" not in item:
                    item["snt_file"] = active_attachment_file
            active_color = getattr(app, "active_snt_color", None)
            if active_color is None:
                active_color = getattr(app, "active_dxf_color", None)

            active_width = None
            for attr in (
                "active_dxf_line_width", "active_snt_line_width", "active_line_width",
                "current_line_width", "draw_line_width", "line_width",
            ):
                try:
                    value = getattr(app, attr, None)
                    if value is not None:
                        active_width = float(value)
                        break
                except Exception:
                    pass

            active_style = None
            for attr in (
                "active_dxf_line_style", "active_snt_line_style", "active_line_style",
                "current_line_style", "draw_line_style", "line_style",
            ):
                try:
                    value = getattr(app, attr, None)
                    if value:
                        active_style = str(value).strip()
                        break
                except Exception:
                    pass

            if active_style:
                style_key = active_style.lower().replace("_", "-").replace(" ", "-")
                if style_key in ("dashdot", "dash-dot"):
                    active_style = "dash-dot"
                elif style_key in ("dashdotdot", "dash-dot-dot"):
                    active_style = "dash-dot-dot"
                elif style_key in ("dashed", "dotted"):
                    active_style = style_key
                else:
                    active_style = "solid"

            # Apply the active SNT/DXF layer colour/style to fresh drawings only.
            # Restored undo/redo drawings keep their own saved style.
            if not sbm_active and not had_layer:
                if active_color is not None:
                    layer_color_01 = (active_color[0] / 255.0, active_color[1] / 255.0, active_color[2] / 255.0)
                    item["original_color"] = layer_color_01
                    item["color"] = layer_color_01

                if active_width is not None:
                    item["original_width"] = active_width
                    item["line_width"] = active_width

                if active_style:
                    item["original_style"] = active_style
                    item["line_style"] = active_style

                actor = item.get("actor")
                if actor is not None:
                    try:
                        if active_color is not None:
                            layer_color_01 = (active_color[0] / 255.0, active_color[1] / 255.0, active_color[2] / 255.0)
                            if hasattr(actor, "GetProperty"):
                                actor.GetProperty().SetColor(*layer_color_01)
                            elif hasattr(actor, "GetTextProperty"):
                                actor.GetTextProperty().SetColor(*layer_color_01)
                        if active_width is not None and hasattr(actor, "GetProperty"):
                            actor.GetProperty().SetLineWidth(float(active_width))
                        if active_style:
                            actor._dxf_line_style = active_style
                            actor._snt_line_style = active_style
                    except Exception:
                        pass
        super().append(item)


class DigitizeCornerRotateTool:
    """
    Integrated corner-box rotate tool for DigitizeManager.

    Flow:
        Select drawing -> Rotate -> Corner Drag
        Cyan box appears
        Click/drag any cyan corner
        Left click or mouse release confirms
        Esc / right-click cancels
    """

    def __init__(self, digitizer):
        self.digitizer = digitizer
        self.app = getattr(digitizer, "app", None)
        self.renderer = getattr(digitizer, "renderer", None)
        self.overlay_renderer = getattr(digitizer, "overlay_renderer", None)
        if self.overlay_renderer is None:
            self.overlay_renderer = self.renderer
        self.interactor = getattr(digitizer, "interactor", None)

        self.active = False
        self.dragging = False
        self.has_moved = False
        self.drawings = []
        self.center = None
        self.start_angle = 0.0
        self.original_coords = {}
        self.box_actors = []
        self.corner_centers = []
        self.observer_ids = []

    def activate(self, drawings=None):
        if self.digitizer is None or self.interactor is None:
            self._status("Corner Rotate unavailable: digitizer not ready", 3000)
            return False

        if drawings is None:
            drawings = self._get_selection()

        drawings = [d for d in list(drawings or []) if isinstance(d, dict) and d.get("coords")]
        if not drawings:
            self._status("Select a drawing first, then use Rotate", 3000)
            return False

        center = self._selection_center(drawings)
        if center is None:
            self._status("Selected drawing has no transformable points", 3000)
            return False

        try:
            if hasattr(self.digitizer, "_prepare_corner_rotate_modal_state"):
                self.digitizer._prepare_corner_rotate_modal_state()
            else:
                self.digitizer.temp_points = []
                self.digitizer.active_tool = "cornerrotate"
        except Exception as e:
            print(f"⚠️ Corner rotate modal prep warning: {e}")

        self.deactivate(cancel=False, silent=True)

        try:
            if hasattr(self.digitizer, "_save_state"):
                self.digitizer._save_state()
        except Exception:
            pass

        self.active = True
        self.dragging = False
        self.has_moved = False
        self.drawings = drawings
        self.center = np.asarray(center, dtype=np.float64)
        self.start_angle = 0.0
        self.original_coords = {
            id(drawing): [tuple(pt) for pt in drawing.get("coords", []) or []]
            for drawing in drawings
        }

        self._install_observers()
        self._show_box()

        self._status(
            "Corner Rotate: drag any cyan corner. Left click/release confirms. Esc cancels.",
            6000,
        )
        return True

    def deactivate(self, cancel=False, silent=False):
        if cancel:
            self._restore_original()

        self._remove_observers()
        self._remove_box()

        self.active = False
        self.dragging = False
        self.has_moved = False
        self.drawings = []
        self.center = None
        self.start_angle = 0.0
        self.original_coords = {}

        try:
            if getattr(self.digitizer, "active_tool", None) == "cornerrotate":
                self.digitizer.active_tool = None
        except Exception:
            pass

        if not silent:
            self._render()

    def _get_selection(self):
        digitizer = self.digitizer
        if digitizer is None:
            return []

        if hasattr(digitizer, "_get_transform_selection"):
            try:
                selected = list(digitizer._get_transform_selection() or [])
                if selected:
                    return selected
            except Exception:
                pass

        out = []
        seen = set()

        def add(d):
            if isinstance(d, dict) and d.get("coords") and id(d) not in seen:
                seen.add(id(d))
                out.append(d)

        for d in getattr(digitizer, "multi_selected", []) or []:
            add(d)
        add(getattr(digitizer, "selected_drawing", None))
        add(getattr(digitizer, "selected", None))

        sel_mgr = getattr(digitizer, "_selection_manager", None)
        if sel_mgr is not None:
            for name in ("get", "selected", "items", "get_selected"):
                method = getattr(sel_mgr, name, None)
                if callable(method):
                    try:
                        for d in list(method() or []):
                            add(d)
                    except Exception:
                        pass
                    break

        element_tool = getattr(digitizer, "_element_select_tool", None)
        if element_tool is not None:
            for attr in ("selected_drawings", "selected_items", "selected_elements", "selection", "targets"):
                value = getattr(element_tool, attr, None)
                if isinstance(value, dict):
                    add(value)
                elif isinstance(value, (list, tuple, set)):
                    for d in value:
                        add(d)

        return out

    def _selection_center(self, drawings):
        if hasattr(self.digitizer, "_transform_selection_center"):
            try:
                center = self.digitizer._transform_selection_center(drawings)
                if center is not None:
                    return center
            except Exception:
                pass

        pts = []
        for drawing in drawings:
            for pt in drawing.get("coords", []) or []:
                if len(pt) >= 2:
                    z = float(pt[2]) if len(pt) > 2 else 0.0
                    pts.append((float(pt[0]), float(pt[1]), z))

        if not pts:
            return None

        arr = np.asarray(pts, dtype=np.float64)
        return (arr.min(axis=0) + arr.max(axis=0)) * 0.5

    def _install_observers(self):
        self._remove_observers()
        self.observer_ids = [
            self.interactor.AddObserver("LeftButtonPressEvent", self._on_left_press, 100.0),
            self.interactor.AddObserver("MouseMoveEvent", self._on_mouse_move, 100.0),
            self.interactor.AddObserver("LeftButtonReleaseEvent", self._on_left_release, 100.0),
            self.interactor.AddObserver("RightButtonPressEvent", self._on_right_press, 100.0),
            self.interactor.AddObserver("KeyPressEvent", self._on_key_press, 100.0),
        ]

    def _remove_observers(self):
        for oid in list(self.observer_ids):
            try:
                self.interactor.RemoveObserver(oid)
            except Exception:
                pass
        self.observer_ids = []

    def _selected_points(self):
        pts = []
        for drawing in self.drawings:
            for pt in drawing.get("coords", []) or []:
                if len(pt) >= 2:
                    z = float(pt[2]) if len(pt) > 2 else 0.0
                    pts.append((float(pt[0]), float(pt[1]), z))
        return pts

    def _world_to_screen(self, point):
        try:
            self.renderer.SetWorldPoint(float(point[0]), float(point[1]), float(point[2]), 1.0)
            self.renderer.WorldToDisplay()
            dp = self.renderer.GetDisplayPoint()
            return (float(dp[0]), float(dp[1]))
        except Exception:
            return None

    def _selected_screen_points(self):
        pts = []
        for point in self._selected_points():
            screen_pt = self._world_to_screen(point)
            if screen_pt is not None:
                pts.append(screen_pt)
        return pts

    def _bbox_corners(self):
        pts = self._selected_screen_points()
        if not pts:
            return []

        arr = np.asarray(pts, dtype=np.float64)
        mn = arr.min(axis=0)
        mx = arr.max(axis=0)
        return [
            (float(mn[0]), float(mn[1])),
            (float(mx[0]), float(mn[1])),
            (float(mx[0]), float(mx[1])),
            (float(mn[0]), float(mx[1])),
        ]

    def _world_tolerance(self, pixels=18.0):
        try:
            if hasattr(self.digitizer, "_screen_to_world_tolerance"):
                return float(self.digitizer._screen_to_world_tolerance(pixels))
        except Exception:
            pass

        pts = self._selected_points()
        if pts:
            arr = np.asarray(pts, dtype=np.float64)
            diag = np.linalg.norm(arr.max(axis=0)[:2] - arr.min(axis=0)[:2])
            return max(float(diag) * 0.04, 1e-6)

        return 1.0

    def _mouse_world(self):
        try:
            p = self.digitizer._get_mouse_world_no_snap()
        except Exception:
            p = self.digitizer._get_mouse_world()
        return np.asarray(p, dtype=np.float64)

    def _angle_from_center_to_mouse(self):
        p = self._mouse_world()
        c = self.center
        return math.atan2(float(p[1]) - float(c[1]), float(p[0]) - float(c[0]))

    def _near_any_corner(self):
        if not self.corner_centers:
            return False
        x, y = self.interactor.GetEventPosition()
        tol = 24.0
        for c in self.corner_centers:
            if math.hypot(float(x) - float(c[0]), float(y) - float(c[1])) <= tol:
                return True
        return False

    def _make_line_actor(self, points, color=(0.0, 1.0, 1.0), width=1.5):
        vtk_points = vtk.vtkPoints()
        for p in points:
            vtk_points.InsertNextPoint(float(p[0]), float(p[1]), 0.0)

        lines = vtk.vtkCellArray()
        lines.InsertNextCell(len(points))
        for i in range(len(points)):
            lines.InsertCellPoint(i)

        poly = vtk.vtkPolyData()
        poly.SetPoints(vtk_points)
        poly.SetLines(lines)

        coord = vtk.vtkCoordinate()
        coord.SetCoordinateSystemToDisplay()

        mapper = vtk.vtkPolyDataMapper2D()
        mapper.SetInputData(poly)
        mapper.SetTransformCoordinate(coord)

        actor = vtk.vtkActor2D()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(*color)
        actor.GetProperty().SetLineWidth(width)
        try:
            actor.PickableOff()
            actor.SetPickable(0)
        except Exception:
            pass
        return actor

    def _make_grip_actor(self, center):
        h = 9.0
        cx, cy = float(center[0]), float(center[1])
        pts = [
            (cx - h, cy - h),
            (cx + h, cy - h),
            (cx + h, cy + h),
            (cx - h, cy + h),
            (cx - h, cy - h),
        ]
        return self._make_line_actor(pts, color=(0.0, 1.0, 1.0), width=3.0)

    def _show_box(self):
        self._remove_box()

        corners = self._bbox_corners()
        if not corners:
            self._render()
            return

        self.corner_centers = corners

        box_points = [corners[0], corners[1], corners[2], corners[3], corners[0]]
        self.box_actors.append(self._make_line_actor(box_points, color=(0.0, 0.8, 0.9), width=1.2))

        for corner in corners:
            self.box_actors.append(self._make_grip_actor(corner))

        for actor in self.box_actors:
            try:
                self.overlay_renderer.AddActor2D(actor)
            except Exception:
                try:
                    self.renderer.AddActor2D(actor)
                except Exception:
                    pass

        self._render()

    def _remove_box(self):
        for actor in list(self.box_actors):
            for renderer in (self.overlay_renderer, self.renderer):
                try:
                    renderer.RemoveActor2D(actor)
                except Exception:
                    pass
                try:
                    renderer.RemoveActor(actor)
                except Exception:
                    pass
                try:
                    renderer.RemoveViewProp(actor)
                except Exception:
                    pass
        self.box_actors = []
        self.corner_centers = []

    def _apply_preview(self):
        if not self.active or not self.dragging or self.center is None:
            return False

        current = self._angle_from_center_to_mouse()
        delta = current - self.start_angle
        cos_a = math.cos(delta)
        sin_a = math.sin(delta)

        for drawing in list(self.drawings):
            original = self.original_coords.get(id(drawing), [])
            if not original:
                continue

            new_coords = [
                self.digitizer._rotate_point_xy(pt, self.center, cos_a, sin_a)
                if hasattr(self.digitizer, "_rotate_point_xy")
                else self._rotate_point_xy_fallback(pt, self.center, cos_a, sin_a)
                for pt in original
            ]

            self._set_coords(drawing, new_coords)
            self._rebuild(drawing)

            try:
                if hasattr(self.digitizer, "_highlight_line"):
                    self.digitizer._highlight_line(drawing)
            except Exception:
                pass

        try:
            if len(self.drawings) == 1:
                self.digitizer.selected_drawing = self.drawings[0]
            elif len(self.drawings) > 1:
                self.digitizer.multi_selected = list(self.drawings)
        except Exception:
            pass

        self._show_box()
        self._render()
        return True

    def _rotate_point_xy_fallback(self, point, center, cos_a, sin_a):
        x = float(point[0]) - float(center[0])
        y = float(point[1]) - float(center[1])
        z = float(point[2]) if len(point) > 2 else float(center[2])
        rx = (x * cos_a) - (y * sin_a) + float(center[0])
        ry = (x * sin_a) + (y * cos_a) + float(center[1])
        return (rx, ry, z)

    def _set_coords(self, drawing, coords):
        if hasattr(self.digitizer, "_set_transformed_coords"):
            try:
                self.digitizer._set_transformed_coords(drawing, coords)
                return
            except Exception:
                pass
        drawing["coords"] = [tuple(p) for p in coords]

    def _rebuild(self, drawing):
        if hasattr(self.digitizer, "_rebuild_transformed_drawing"):
            try:
                self.digitizer._rebuild_transformed_drawing(drawing)
                return
            except Exception:
                pass
        if hasattr(self.digitizer, "_rebuild_drawing_actor"):
            try:
                self.digitizer._rebuild_drawing_actor(drawing)
                return
            except Exception:
                pass

    def _restore_original(self):
        for drawing in list(self.drawings):
            original = self.original_coords.get(id(drawing), [])
            if not original:
                continue
            self._set_coords(drawing, original)
            self._rebuild(drawing)
            try:
                if hasattr(self.digitizer, "_highlight_line"):
                    self.digitizer._highlight_line(drawing)
            except Exception:
                pass

    def _confirm(self, obj=None):
        drawings = list(self.drawings)

        self.active = False
        self.dragging = False
        self.has_moved = False

        self._remove_observers()
        self._remove_box()

        self.drawings = []
        self.center = None
        self.original_coords = {}

        try:
            if getattr(self.digitizer, "active_tool", None) == "cornerrotate":
                self.digitizer.active_tool = None
        except Exception:
            pass

        try:
            if hasattr(self.digitizer, "_finish_transform_refresh"):
                self.digitizer._finish_transform_refresh(drawings)
        except Exception:
            pass

        try:
            if hasattr(self.digitizer, "_restore_element_select_after_corner_rotate"):
                self.digitizer._restore_element_select_after_corner_rotate()
        except Exception as e:
            print(f"⚠️ Corner rotate restore warning: {e}")

        self._status("Corner rotation confirmed", 1500)
        self._consume(obj)
        self._render()

    def _cancel(self, obj=None):
        self._restore_original()
        self.deactivate(cancel=False, silent=True)
        try:
            if getattr(self.digitizer, "active_tool", None) == "cornerrotate":
                self.digitizer.active_tool = None
        except Exception:
            pass
        try:
            if hasattr(self.digitizer, "_restore_element_select_after_corner_rotate"):
                self.digitizer._restore_element_select_after_corner_rotate()
        except Exception as e:
            print(f"⚠️ Corner rotate restore warning: {e}")
        self._status("Corner rotation cancelled", 1500)
        self._consume(obj)
        self._render()

    def _on_left_press(self, obj, evt):
        if not self.active:
            return

        self._consume(obj)

        if self.dragging:
            self._confirm(obj)
            return

        if not self._near_any_corner():
            self._status("Corner Rotate: drag from a cyan corner to rotate", 1200)

        self.start_angle = self._angle_from_center_to_mouse()
        self.dragging = True
        self.has_moved = False

    def _on_mouse_move(self, obj, evt):
        if not self.active:
            return

        self._consume(obj)

        if self.dragging:
            self.has_moved = True
            self._apply_preview()

    def _on_left_release(self, obj, evt):
        if not self.active:
            return

        self._consume(obj)

        if self.dragging and self.has_moved:
            self._confirm(obj)

    def _on_right_press(self, obj, evt):
        if self.active:
            self._cancel(obj)

    def _on_key_press(self, obj, evt):
        if not self.active:
            return

        key = ""
        try:
            key = self.interactor.GetKeySym().lower()
        except Exception:
            pass

        if key == "escape":
            self._cancel(obj)

    def _consume(self, obj):
        try:
            if hasattr(self.digitizer, "_consume_vtk_event"):
                self.digitizer._consume_vtk_event(obj)
        except Exception:
            pass
        try:
            if hasattr(obj, "AbortFlagOn"):
                obj.AbortFlagOn()
        except Exception:
            pass

    def _status(self, message, timeout=3000):
        try:
            self.app.statusBar().showMessage(message, timeout)
        except Exception:
            print(message)

    def _render(self):
        try:
            if self.renderer is not None:
                self.renderer.Modified()
            if self.overlay_renderer is not None:
                self.overlay_renderer.Modified()
            self.app.vtk_widget.render()
        except Exception:
            try:
                if hasattr(self.digitizer, "_force_render"):
                    self.digitizer._force_render()
            except Exception:
                pass


class DigitizeManager:
    """
    Full-featured geo-referenced digitizing system for Naksha Plan View.
    Supports: line (continuous smartline), rectangle, circle, polygon, text, freehand.
    """

    def __init__(self, app, renderer, interactor):
        self.enabled = True
        self._event_forward = True

        self.app = app
        self.renderer = renderer
        self.interactor = interactor

        # Data storage
        self.drawings = DrawingList(self)
        # Undo/Redo system
        self.undo_stack = []     # Stack of previous states
        self.redo_stack = []     # Stack of undone states
        self.max_undo_levels = 50  # Limit undo history
        # list of {type, coords, actor, text}
        self.active_tool = None
        self.temp_points = []
        # ✅ All tools permanent by default
        self.polyline_permanent_mode = True
        self.rectangle_permanent_mode = True
        self.circle_permanent_mode = True
        self.freehand_permanent_mode = True
        self.smartline_permanent_mode = True
        self.line_permanent_mode = True

        # --- Smart line additions ---
        self._continuous_line_actor = None
        self._preview_line_actor = None

        # Editing
        self.selected = None
        self.move_mode = False
        self._last_pos = None
        self.left_down = False
        self.middle_down = False 
        self._is_panning = False 
        # Prevent Move Vertex from reacting when middle mouse pan generates
        # stray mouse move / left / right events.
        self._ignore_move_vertex_finish_until = 0.0
        self._move_vertex_pan_block_until = 0.0
        self._move_vertex_observer_ids = []

        self.coord_labels = []   # stores floating coordinate labels
        self.selected_drawing = None  # ✅
        self._is_panning = False
        self._pan_start_pos = None
        self._middle_press_processed = False
        self._blocked_fake_middle = False
        self._pan_press_monotonic = 0.0
        self._pan_button_miss_count = 0
        self._temp_vertex_stack = []
        self._temp_redo_stack = []

        # MicroStation-style selection set (Phase 1 — beside legacy selected_drawing)
        self._selection_manager = None
        self._element_select_tool = None
        self._corner_rotate_selection_restore = None

        # Track our own VTK observer IDs so we can remove only ours
        # (not grid_label_system's or other tools' observers)
        self._draw_observer_ids = []
        # Companion observers (key / middle-button / wheel) installed below.
        # Tracked so _reinstall_all_observers() can remove-then-readd them
        # instead of stacking duplicates.
        self._aux_observer_ids = []
        # Text UI/session state
        self._text_dialog = None
        self._text_ui_editing = False
        self._text_context_menu = None
        self._text_context_menu_timer = None

        # Draw-tool text visibility.
        # This only controls text stored in self.drawings with type == "text".
        # It does NOT affect loaded SNT attachment labels/text.
        self.draw_text_visible = True

        self._drawing_finalized_callbacks = []
        self._drawing_removed_callbacks = []

        # Suspended drawing state (preserved when section tool activated mid-draw)
        self._suspended_state = None   # {"tool": str, "temp_points": list, "markers": list, "marker_attr": str}
        self._suspended_preview_actor = None

        self.snap_enabled = False  # ✅ Snap disabled by default
        self.snap_mode = None
        self._snap_hide_seq = 0    # ✅ used to safely hide snap marker with timer
        self.vertex_move_mode = False
        self.dragging_vertex = None
        self.vertex_hover_marker = None
        self.vertex_drag_marker = None
        self._shift_blocked = False  # Blocks shift key after Shift+Esc deactivation
        self.vertex_auto_drag = False

        # Move Vertex constraint mode.
        # free     = existing 360 movement
        # previous = move only along previous connected edge direction
        # next     = move only along next connected edge direction
        self.vertex_move_constraint_mode = "free"
        self.vertex_move_constraint_label = "Free 360 Move"

        # Corner-drag rotate mode
        self._corner_rotate_waiting = False
        self._corner_rotate_dragging = False
        self._corner_rotate_drawings = []
        self._corner_rotate_center_world = None
        self._corner_rotate_center_display = None
        self._corner_rotate_start_angle = 0.0
        self._corner_rotate_original_coords = {}
        self._corner_rotate_actors = []
        self._corner_rotate_tool = None
        # Vertex-pivot rotate mode
        self._rotate_vertex_pivot_observer_id = None
        self._rotate_vertex_pivot_right_observer_id = None
        self._rotate_vertex_pivot_pending_degrees = None
        self._rotate_vertex_pivot_drawings = []
        self._rotate_vertex_pivot_showing_vertices = False
        self.picker = vtk.vtkWorldPointPicker()
        self.interactor.SetPicker(self.picker)

        # Disable 3D rotation
        try:
            style = vtk.vtkInteractorStyleTrackballCamera()
            self.interactor.SetInteractorStyle(style)

            print("🎯 Digitizer interactor style locked (no 3D rotation)")
        except Exception as e:
            print("⚠️ Failed to set interactor style:", e)

        # Event bindings — store IDs so we can remove only our observers later
        self._ensure_plan_view_interaction("digitizer init")
        self._draw_observer_ids.append(self.interactor.AddObserver("LeftButtonPressEvent", self._on_left_press))
        self._draw_observer_ids.append(self.interactor.AddObserver("MouseMoveEvent", self._on_mouse_move))
        self._draw_observer_ids.append(self.interactor.AddObserver("LeftButtonReleaseEvent", self._on_left_release))
        self._draw_observer_ids.append(self.interactor.AddObserver("RightButtonPressEvent", self._on_right_press))
        self._aux_observer_ids.append(
            self.interactor.AddObserver("KeyPressEvent", self._on_key_press)
        )
        # Middle mouse button for panning
        self._aux_observer_ids.append(
            self.interactor.AddObserver("MiddleButtonPressEvent", self._on_middle_press, 20.0)
        )
        self._aux_observer_ids.append(
            self.interactor.AddObserver("MiddleButtonReleaseEvent", self._on_middle_release, 20.0)
        )
        # After existing interactor.AddObserver calls (~line 91):
        self._aux_observer_ids.append(
            self.interactor.AddObserver("MouseWheelForwardEvent", self._on_zoom, 1.0)
        )
        self._aux_observer_ids.append(
            self.interactor.AddObserver("MouseWheelBackwardEvent", self._on_zoom, 1.0)
        )
                # Ensure interactor focus for key handling
        try:
            self.interactor.EnableRenderOn()
            print("🎹 Key focus ensured for digitizer interactor")
        except Exception:
            pass

        self.picker = vtk.vtkWorldPointPicker()
        self.interactor.SetPicker(self.picker)
        
        # Native optimized pickers
        self._cell_picker = vtk.vtkCellPicker()
        self._cell_picker.SetTolerance(0.005)
        self._prop_picker = vtk.vtkPropPicker()

        # ── OVERLAY RENDERER (always draws on top of point cloud) ──────────────────
        from gui.scene_render_pipeline import (
            OVERLAY_LAYER, TEXT_LAYER, NUMBER_OF_LAYERS,
            ensure_scene_render_pipeline,
        )
        render_window = self.interactor.GetRenderWindow()
        self.overlay_renderer = vtk.vtkRenderer()
        self.overlay_renderer.SetLayer(OVERLAY_LAYER)
        self.overlay_renderer.SetInteractive(0)    # Don't intercept mouse events
        self.overlay_renderer.SetBackgroundAlpha(0.0)
        # Preserve lower-layer colors but start with a fresh depth buffer. Drawings
        # then composite above LiDAR without depending on either actor's world Z.
        self.overlay_renderer.SetErase(1)
        self.overlay_renderer.SetPreserveColorBuffer(True)
        self.overlay_renderer.SetPreserveDepthBuffer(False)
        render_window.SetNumberOfLayers(NUMBER_OF_LAYERS)
        render_window.AddRenderer(self.overlay_renderer)
        # Share the EXACT same camera — pan/zoom stays in sync automatically
        self.overlay_renderer.SetActiveCamera(self.renderer.GetActiveCamera())
        print("✅ Overlay renderer created (layer 2, isolated depth)")
        # ───────────────────────────────────────────────────────────────────────────

        # ── TEXT OVERLAY RENDERER (Layer 3) ───────────────────────────────────────
        # Text has its own compositor pass so it cannot inherit vector/LiDAR depth.
        self.text_overlay_renderer = vtk.vtkRenderer()
        self.text_overlay_renderer.SetLayer(TEXT_LAYER)
        self.text_overlay_renderer.SetInteractive(0)
        self.text_overlay_renderer.SetBackground(0, 0, 0)
        self.text_overlay_renderer.SetBackgroundAlpha(0.0)
        self.text_overlay_renderer.SetErase(1)      # clears depth so text always wins
        try:
            self.text_overlay_renderer.SetPreserveColorBuffer(True)  # keep layers 0+1 visible
        except AttributeError:
            pass  # VTK < 8.2: fall back; text may still occlude but won't blank the scene
        render_window.SetNumberOfLayers(NUMBER_OF_LAYERS)
        render_window.AddRenderer(self.text_overlay_renderer)
        self.text_overlay_renderer.SetActiveCamera(self.renderer.GetActiveCamera())
        ensure_scene_render_pipeline(
            self.app,
            overlay_renderer=self.overlay_renderer,
            text_renderer=self.text_overlay_renderer,
        )
        print("✅ Text overlay renderer created (layer 3, isolated depth)")

        # ── Draw tool style settings (per-tool color/width/style) ─────────────────
        from gui.draw_settings_dialog import load_draw_settings, DEFAULT_DRAW_STYLES
        self.default_draw_tool_styles = {k: dict(v) for k, v in DEFAULT_DRAW_STYLES.items()}
        try:
            loaded_styles = load_draw_settings()
            self.draw_tool_styles = {
                key: {
                    **self.default_draw_tool_styles.get(key, {}),
                    **dict(loaded_styles.get(key, {})),
                }
                for key in self.default_draw_tool_styles
            }
        except Exception:
            self.draw_tool_styles = {
                k: dict(v) for k, v in self.default_draw_tool_styles.items()
            }
        print("✅ Draw tool styles loaded")

        # ── Separate AccuDraw XYZ/Angle tool ───────────────────────────────
        try:
            from gui.accudraw_tool import AccuDrawTool
            self.accudraw_tool = AccuDrawTool(self)
            print("✅ AccuDraw tool initialized")
        except Exception as e:
            self.accudraw_tool = None
            print(f"⚠️ AccuDraw tool init failed: {e}")
        # ───────────────────────────────────────────────────────────────────────────

    def _check_and_update_renderers(self):
        """Self-healing method to ensure the digitizer uses the correct active main renderer
        and that the overlay and text overlay renderers are correctly registered in the VTK
        render window with the correct layers, cameras, and properties.
        """
        try:
            if not self.interactor or not self.app or not hasattr(self.app, 'vtk_widget') or not self.app.vtk_widget:
                return

            active_renderer = self.app.vtk_widget.renderer
            active_interactor = self.app.vtk_widget.interactor

            renderer_changed = (self.renderer != active_renderer)
            interactor_changed = (self.interactor != active_interactor)

            if renderer_changed:
                print(f"🔄 DigitizeManager: Main renderer changed from {self.renderer} to {active_renderer}")
                self.renderer = active_renderer
            if interactor_changed:
                print(f"🔄 DigitizeManager: Interactor changed from {self.interactor} to {active_interactor}")
                self.interactor = active_interactor
                if hasattr(self, 'picker') and self.picker:
                    self.interactor.SetPicker(self.picker)

            render_window = self.interactor.GetRenderWindow()
            if not render_window:
                return

            from gui.scene_render_pipeline import ensure_scene_render_pipeline
            ensure_scene_render_pipeline(
                self.app,
                overlay_renderer=self.overlay_renderer,
                text_renderer=self.text_overlay_renderer,
            )

            renderers = render_window.GetRenderers()
            renderers.InitTraversal()
            r = renderers.GetNextItem()
            found_overlay = False
            found_text_overlay = False
            while r is not None:
                if r == self.overlay_renderer:
                    found_overlay = True
                elif r == self.text_overlay_renderer:
                    found_text_overlay = True
                r = renderers.GetNextItem()

            needs_restore = renderer_changed or interactor_changed or not found_overlay or not found_text_overlay

            if needs_restore:
                print("🛠️ Restoring DigitizeManager overlay renderers and layers...")
                from gui.scene_render_pipeline import (
                    OVERLAY_LAYER, TEXT_LAYER, NUMBER_OF_LAYERS,
                )
                render_window.SetNumberOfLayers(NUMBER_OF_LAYERS)

                if not found_overlay:
                    render_window.AddRenderer(self.overlay_renderer)
                self.overlay_renderer.SetLayer(OVERLAY_LAYER)
                self.overlay_renderer.SetInteractive(0)
                self.overlay_renderer.SetBackgroundAlpha(0.0)
                self.overlay_renderer.SetErase(1)
                self.overlay_renderer.SetPreserveColorBuffer(True)
                self.overlay_renderer.SetPreserveDepthBuffer(False)
                self.overlay_renderer.SetActiveCamera(self.renderer.GetActiveCamera())

                if not found_text_overlay:
                    render_window.AddRenderer(self.text_overlay_renderer)
                self.text_overlay_renderer.SetLayer(TEXT_LAYER)
                self.text_overlay_renderer.SetInteractive(0)
                self.text_overlay_renderer.SetBackground(0, 0, 0)
                self.text_overlay_renderer.SetBackgroundAlpha(0.0)
                self.text_overlay_renderer.SetErase(1)
                try:
                    self.text_overlay_renderer.SetPreserveColorBuffer(True)
                except AttributeError:
                    pass
                self.text_overlay_renderer.SetActiveCamera(self.renderer.GetActiveCamera())

                if hasattr(self, '_element_select_tool') and self._element_select_tool:
                    self._element_select_tool.renderer = self.renderer
                    self._element_select_tool.overlay_renderer = self.overlay_renderer
                    self._element_select_tool.interactor = self.interactor
                if hasattr(self, '_ortho_polygon_tool') and self._ortho_polygon_tool:
                    self._ortho_polygon_tool.renderer = self.renderer
                    self._ortho_polygon_tool.overlay_renderer = self.overlay_renderer
                    self._ortho_polygon_tool.interactor = self.interactor
                if hasattr(self.app, 'measurement_tool') and self.app.measurement_tool:
                    self.app.measurement_tool.renderer = self.renderer
                    self.app.measurement_tool.overlay_renderer = self.overlay_renderer
                    self.app.measurement_tool.interactor = self.interactor
                    if hasattr(self.app.measurement_tool, 'text_overlay_renderer'):
                        self.app.measurement_tool.text_overlay_renderer = self.text_overlay_renderer

                print("✅ DigitizeManager overlays and layers successfully healed!")

        except Exception as e:
            print(f"⚠️ Error in _check_and_update_renderers: {e}")
            import traceback
            traceback.print_exc()

    def _get_fallback_z_height(self):
        """Get the best fallback Z height for drawing when the point cloud is empty/cleared.
        Aligns with SNT/DXF/DWG actors currently in the viewer.
        """
        app = self.app
        if hasattr(app, 'data') and app.data is not None and 'xyz' in app.data:
            try:
                from gui.snt_attachment import _get_snt_z_offset
                offset = _get_snt_z_offset(app)
                if offset is not None and offset != 0.0:
                    return float(offset)
            except Exception:
                pass
            try:
                xyz = app.data['xyz']
                if len(xyz) > 0:
                    return float(np.median(xyz[:, 2]))
            except Exception:
                pass

        try:
            actors = self.renderer.GetActors()
            actors.InitTraversal()
            actor = actors.GetNextActor()
            z_coords = []
            while actor is not None:
                if actor.GetVisibility():
                    bounds = actor.GetBounds()
                    if bounds and bounds[4] < 1e9 and bounds[5] > -1e9:
                        if abs(bounds[4]) > 0.001 or abs(bounds[5]) > 0.001:
                            z_coords.append((bounds[4] + bounds[5]) * 0.5)
                actor = actors.GetNextActor()
            if z_coords:
                return float(np.median(z_coords))
        except Exception as e:
            print(f"⚠️ Fallback Z from actors failed: {e}")

        if hasattr(app, 'snt_attachments') and app.snt_attachments:
            try:
                for att in app.snt_attachments:
                    entities = att.get('entities', [])
                    if entities:
                        z_vals = []
                        for ent in entities:
                            pts = ent.get('points') or ent.get('vertices') or []
                            for pt in pts:
                                if pt is not None and len(pt) > 2:
                                    z_vals.append(float(pt[2]))
                        if z_vals:
                            return float(np.median(z_vals))
            except Exception:
                pass

        return 0.0


    # ------------------------------------------------------------------
    # Transform tools: Rotate / Mirror
    # ------------------------------------------------------------------
    def _get_transform_selection(self):
        selected_items = []
        seen = set()

        def _add_candidate(candidate):
            if not isinstance(candidate, dict):
                return
            if not candidate.get("coords"):
                return
            key = id(candidate)
            if key in seen:
                return
            seen.add(key)
            selected_items.append(candidate)

        sel_mgr = getattr(self, "_selection_manager", None)
        if sel_mgr is not None:
            try:
                if hasattr(sel_mgr, "get"):
                    for drawing in list(sel_mgr.get() or []):
                        _add_candidate(drawing)
            except Exception as e:
                print(f"⚠️ Transform selection manager read failed: {e}")

        element_tool = getattr(self, "_element_select_tool", None)
        if element_tool is not None:
            for attr in (
                "selected_drawings", "selected_drawing", "selected_items",
                "selected_elements", "selection", "targets",
            ):
                try:
                    value = getattr(element_tool, attr, None)
                except Exception:
                    value = None
                if isinstance(value, dict):
                    _add_candidate(value)
                elif isinstance(value, (list, tuple, set)):
                    for drawing in value:
                        _add_candidate(drawing)

        for drawing in getattr(self, "multi_selected", []) or []:
            _add_candidate(drawing)

        _add_candidate(getattr(self, "selected_drawing", None))
        _add_candidate(getattr(self, "selected", None))

        return selected_items
    
    def _iter_pickable_drawings(self):
        """
        Return only current active digitizer drawings.

        IMPORTANT:
        Do not re-add curve_tool.finalized_actors here.
        After Clear All Drawings, curve_tool may still have old finalized data
        for a moment, and Element Select / Vertex Display can pick those stale
        cleared curves again.
        """
        return [
            d for d in list(getattr(self, "drawings", []) or [])
            if not self._is_hidden_draw_tool_text_drawing(d)
        ]



    def _get_drawing_coords(self, drawing, include_interpolated=True):
        if not isinstance(drawing, dict):
            return []

        for key in ("coords", "coordinates"):
            coords = drawing.get(key)
            if coords is None:
                continue
            try:
                if len(coords) > 0:
                    return coords
            except TypeError:
                return coords

        if include_interpolated:
            coords = drawing.get("interpolated")
            if coords is not None:
                try:
                    if len(coords) > 0:
                        return coords
                except TypeError:
                    return coords

        return []

    def _show_transform_status(self, message, timeout=3000):
        try:
            self.app.statusBar().showMessage(message, timeout)
        except Exception:
            print(message)

    def _transform_selection_center(self, drawings):
        if not drawings:
            return None

        pts = []
        for drawing in drawings:
            coords = drawing.get("coords") or []
            for pt in coords:
                if len(pt) >= 2:
                    z = float(pt[2]) if len(pt) > 2 else 0.0
                    pts.append((float(pt[0]), float(pt[1]), z))

            if drawing.get("center") is not None:
                c = drawing.get("center")
                if len(c) >= 2:
                    z = float(c[2]) if len(c) > 2 else 0.0
                    pts.append((float(c[0]), float(c[1]), z))

        if not pts:
            return None

        arr = np.asarray(pts, dtype=np.float64)
        min_xyz = arr.min(axis=0)
        max_xyz = arr.max(axis=0)
        return (min_xyz + max_xyz) * 0.5

    def _rotate_point_xy(self, point, center, cos_a, sin_a):
        x = float(point[0]) - float(center[0])
        y = float(point[1]) - float(center[1])
        z = float(point[2]) if len(point) > 2 else float(center[2])
        rx = (x * cos_a) - (y * sin_a) + float(center[0])
        ry = (x * sin_a) + (y * cos_a) + float(center[1])
        return (rx, ry, z)

    def _mirror_point_axis_xy(self, point, center, axis_degrees):
        theta = math.radians(float(axis_degrees))
        ux = math.cos(theta)
        uy = math.sin(theta)

        x = float(point[0]) - float(center[0])
        y = float(point[1]) - float(center[1])
        z = float(point[2]) if len(point) > 2 else float(center[2])

        dot = (x * ux) + (y * uy)
        mx = (2.0 * dot * ux) - x + float(center[0])
        my = (2.0 * dot * uy) - y + float(center[1])
        return (mx, my, z)

    def _set_transformed_coords(self, drawing, coords):
        drawing["coords"] = [tuple(p) for p in coords]

        if drawing.get("source") == "curve_tool":
            drawing["interpolated"] = [tuple(p) for p in coords]

        if drawing.get("type") == "circle":
            arr = np.asarray(drawing["coords"], dtype=np.float64)
            if arr.ndim == 2 and arr.shape[0] > 0:
                center = arr[:, :3].mean(axis=0)
                drawing["center"] = tuple(center.tolist())
                try:
                    drawing["radius"] = float(
                        np.linalg.norm(arr[0, :2] - np.asarray(drawing["center"][:2], dtype=np.float64))
                    )
                except Exception:
                    pass


    def _clear_transformed_drawing_markers(self, drawing):
        """Remove transient per-vertex/end markers before transform rebuilds.

        Corner-drag rotate previews rebuild the drawing repeatedly. If we let
        _rebuild_drawing_actor preserve stored vertex markers, hidden
        construction grips can come back as visible yellow dots after rotate.
        """
        if not isinstance(drawing, dict):
            return

        for key in ("start_marker", "end_marker"):
            actor = drawing.get(key)
            if actor is not None:
                try:
                    self._remove_actor_from_overlay(actor)
                except Exception:
                    try:
                        self.renderer.RemoveActor(actor)
                    except Exception:
                        pass
                drawing[key] = None

        markers = drawing.get("vertex_markers") or []
        if markers:
            for marker in list(markers):
                try:
                    self._remove_actor_from_overlay(marker)
                except Exception:
                    try:
                        self.renderer.RemoveActor(marker)
                    except Exception:
                        pass
            drawing["vertex_markers"] = []

    def _rebuild_transformed_drawing(self, drawing):
        try:
            dtype = drawing.get("type")

            if dtype == "text":
                actor = drawing.get("actor")
                coords = drawing.get("coords") or []
                if actor is not None and coords:
                    pt = coords[0]
                    if hasattr(actor, "SetPosition"):
                        actor.SetPosition(float(pt[0]), float(pt[1]), float(pt[2]) if len(pt) > 2 else 0.0)
                    actor.Modified()
                return
            self._clear_transformed_drawing_markers(drawing)

            if hasattr(self, "_rebuild_drawing_actor"):
                    self._rebuild_drawing_actor(drawing)
            else:
                old_actor = drawing.get("actor")
                if old_actor is not None:
                    self._remove_actor_from_overlay(old_actor)
                color = drawing.get("original_color", drawing.get("color", (1, 0, 0)))
                width = drawing.get("original_width", 2)
                style = drawing.get("original_style", "solid")
                if drawing.get("source") == "curve_tool":
                    curve_tool = getattr(getattr(self, "app", None), "curve_tool", None)
                    if curve_tool is not None and hasattr(curve_tool, "_create_curve_actor"):
                        actor = curve_tool._create_curve_actor(drawing["coords"], color=color, width=width)
                    else:
                        actor = self._make_polyline_actor(drawing["coords"], color=color, width=width, line_style=style)
                else:
                    actor = self._make_polyline_actor(drawing["coords"], color=color, width=width, line_style=style)
                self._add_actor_to_overlay(actor)
                drawing["actor"] = actor
                drawing["bounds"] = actor.GetBounds()
        except Exception as e:
            print(f"⚠️ Transform rebuild failed: {e}")

    def _finish_transform_refresh(self, drawings):
        drawings = list(drawings or [])
        self._pending_transform_deselect_on_right_click = bool(drawings)

        try:
            had_vertex_display = bool(getattr(self, "coord_labels", None))
        except Exception:
            had_vertex_display = False

        try:
            self.clear_coordinate_labels()
        except Exception:
            pass

        try:
            if len(drawings) == 1 and getattr(self, "selected_drawing", None) is None:
                self.selected_drawing = drawings[0]
            elif len(drawings) > 1:
                self.multi_selected = list(drawings)
        except Exception:
            pass

        for drawing in drawings:
            try:
                if hasattr(self, "_highlight_line"):
                    self._highlight_line(drawing)
            except Exception as e:
                print(f"⚠️ Transform highlight restore failed: {e}")

        if had_vertex_display:
            transformed_points = []
            for drawing in drawings:
                if drawing is None or drawing.get("type") == "text":
                    continue
                transformed_points.extend(list(drawing.get("coords") or []))
            if transformed_points:
                try:
                    self.show_vertex_coordinates(transformed_points)
                except Exception:
                    pass

        try:
            if self._selection_manager is not None:
                self._selection_manager.notify_drawings_changed()
        except Exception:
            pass

        try:
            self.renderer.Modified()
            self.app.vtk_widget.render()
        except Exception:
            self._force_render()


    def extend_selected_element_distance_command(self):
        """
        Element Selection -> Extend / distance scaling.

        Robust version for updated Line tool:
        - Does NOT require line_group_id / line_segment_index.
        - Uses real endpoint connectivity to move downstream attached line_segment drawings.
        - Prevents Z-line disconnection after Extend.

        Final behavior:
        - Select Line / SmartLine / line_segment.
        - Click Extend.
        - Enter distance only.
        - No Start/End popup.
        - No new point.
        - No new edge.
        """
        drawings = []
        seen = set()

        def _add_extend_candidate(candidate):
            if not isinstance(candidate, dict):
                return

            coords = candidate.get("coords") or []
            if len(coords) < 2:
                return

            key = id(candidate)
            if key in seen:
                return

            seen.add(key)
            drawings.append(candidate)

        # Prefer SelectionManager.
        # Do not merge stale selected_drawing / multi_selected when SelectionManager has active items.
        try:
            mgr_items = list(self.selection_manager.get() or [])
        except Exception:
            mgr_items = []

        if mgr_items:
            for item in mgr_items:
                _add_extend_candidate(item)
        else:
            _add_extend_candidate(getattr(self, "selected_drawing", None))
            _add_extend_candidate(getattr(self, "selected", None))

            if not drawings:
                for item in list(getattr(self, "multi_selected", []) or []):
                    _add_extend_candidate(item)

        valid_types = {"smartline", "line", "line_segment"}

        drawings = [
            d for d in drawings
            if isinstance(d, dict)
            and d.get("type") in valid_types
            and len(d.get("coords") or []) >= 2
        ]

        if not drawings:
            self._show_transform_status(
                "Select Line / SmartLine first",
                2500,
            )
            return False

        try:
            from PySide6.QtWidgets import QInputDialog
        except Exception:
            from PyQt5.QtWidgets import QInputDialog

        parent = getattr(self, "app", None)

        distance_m, ok = QInputDialog.getDouble(
            parent,
            "Extend",
            "Enter extend distance in meters:",
            1.0,
            -1000000000.0,
            1000000000.0,
            3,
        )

        if not ok:
            return False

        try:
            distance_m = float(distance_m)
        except Exception:
            self._show_transform_status("Invalid extend distance", 2500)
            return False

        if abs(distance_m) <= 1e-12:
            return False

        def _same_xy(a, b, tol=1e-7):
            try:
                return (
                    abs(float(a[0]) - float(b[0])) <= tol
                    and abs(float(a[1]) - float(b[1])) <= tol
                )
            except Exception:
                return False

        def _point_key(pt, precision=7):
            return (
                round(float(pt[0]), precision),
                round(float(pt[1]), precision),
            )

        def _extended_endpoint(endpoint, neighbor):
            endpoint_np = np.array(endpoint, dtype=np.float64)
            neighbor_np = np.array(neighbor, dtype=np.float64)

            vec = endpoint_np - neighbor_np
            length_xy = float(np.linalg.norm(vec[:2]))

            if length_xy <= 1e-12:
                return None

            direction_xy = vec[:2] / length_xy

            return (
                float(endpoint_np[0] + direction_xy[0] * distance_m),
                float(endpoint_np[1] + direction_xy[1] * distance_m),
                float(endpoint_np[2]) if len(endpoint_np) > 2 else 0.0,
            )

        def _translate_segment(drawing, delta):
            coords = list(drawing.get("coords") or [])
            if len(coords) < 2:
                return False

            new_coords = []
            for pt in coords:
                z = float(pt[2]) if len(pt) > 2 else 0.0
                new_coords.append(
                    (
                        float(pt[0]) + float(delta[0]),
                        float(pt[1]) + float(delta[1]),
                        z,
                    )
                )

            self._set_transformed_coords(drawing, new_coords)
            self._rebuild_transformed_drawing(drawing)
            return True

        def _mark_changed(drawing):
            key = id(drawing)
            if key not in changed_ids:
                changed_ids.add(key)
                changed_drawings.append(drawing)

        def _mark_highlight(drawing):
            key = id(drawing)
            if key not in highlight_ids:
                highlight_ids.add(key)
                highlight_drawings.append(drawing)

        def _all_line_segments():
            return [
                d for d in list(getattr(self, "drawings", []) or [])
                if isinstance(d, dict)
                and d.get("type") == "line_segment"
                and len(d.get("coords") or []) >= 2
            ]

        def _touches_point(segment, point):
            coords = list(segment.get("coords") or [])
            if len(coords) < 2:
                return False
            return _same_xy(coords[0], point) or _same_xy(coords[-1], point)

        def _other_endpoint(segment, shared_point):
            coords = list(segment.get("coords") or [])
            if len(coords) < 2:
                return None

            if _same_xy(coords[0], shared_point):
                return coords[-1]

            if _same_xy(coords[-1], shared_point):
                return coords[0]

            return None

        def _connected_downstream_segments(start_point, blocked_segment):
            """
            Find all connected line_segment drawings on the far side of start_point.

            This is the important fix:
            updated Line tool does not store line_group_id, so we walk connectivity
            by shared endpoint coordinates instead.
            """
            segments = _all_line_segments()
            downstream = []
            downstream_ids = set()

            queue = [_point_key(start_point)]
            visited_points = set()
            blocked_id = id(blocked_segment)

            while queue:
                point_key = queue.pop(0)
                if point_key in visited_points:
                    continue
                visited_points.add(point_key)

                probe_point = (float(point_key[0]), float(point_key[1]), 0.0)

                for seg in segments:
                    if id(seg) == blocked_id:
                        continue
                    if id(seg) in downstream_ids:
                        continue

                    if not _touches_point(seg, probe_point):
                        continue

                    downstream_ids.add(id(seg))
                    downstream.append(seg)

                    other = _other_endpoint(seg, probe_point)
                    if other is not None:
                        other_key = _point_key(other)
                        if other_key not in visited_points:
                            queue.append(other_key)

            return downstream

        self._save_state()

        changed_drawings = []
        changed_ids = set()

        # Only selected target should stay highlighted.
        # Downstream translated segments should move but should not look selected.
        highlight_drawings = []
        highlight_ids = set()

        # 1) SmartLine / full line:
        # Extend only last vertex.
        normal_drawings = [
            d for d in drawings
            if d.get("type") in {"smartline", "line"}
        ]

        for drawing in normal_drawings:
            coords = list(drawing.get("coords") or [])
            if len(coords) < 2:
                continue

            old_endpoint = coords[-1]
            neighbor = coords[-2]

            new_endpoint = _extended_endpoint(old_endpoint, neighbor)
            if new_endpoint is None:
                continue

            coords[-1] = tuple(new_endpoint)

            self._set_transformed_coords(drawing, coords)
            self._rebuild_transformed_drawing(drawing)
            _mark_changed(drawing)
            _mark_highlight(drawing)

        # 2) Updated Line tool line_segment:
        # The updated Line tool creates independent line_segment records with no group id.
        # So extend selected segment's end point and translate all geometrically connected
        # downstream segments from that endpoint.
        selected_line_segments = [
            d for d in drawings
            if d.get("type") == "line_segment"
        ]

        for target_segment in selected_line_segments:
            target_coords = list(target_segment.get("coords") or [])
            if len(target_coords) < 2:
                continue

            # Use the segment's stored draw direction:
            # coords[0] stays fixed, coords[-1] grows.
            # This matches how _finalize_line creates each segment from p1 -> p2.
            old_endpoint = target_coords[-1]
            neighbor = target_coords[-2]

            new_endpoint = _extended_endpoint(old_endpoint, neighbor)
            if new_endpoint is None:
                continue

            delta = np.array(new_endpoint, dtype=np.float64) - np.array(
                old_endpoint,
                dtype=np.float64,
            )

            downstream_segments = _connected_downstream_segments(
                old_endpoint,
                target_segment,
            )

            # Move selected segment endpoint.
            target_coords[-1] = tuple(new_endpoint)
            self._set_transformed_coords(target_segment, target_coords)
            self._rebuild_transformed_drawing(target_segment)
            _mark_changed(target_segment)
            _mark_highlight(target_segment)

            # Move all downstream connected segments by same delta.
            # This keeps Z-line joints attached and preserves downstream angle.
            for downstream_segment in downstream_segments:
                if downstream_segment is target_segment:
                    continue

                if _translate_segment(downstream_segment, delta):
                    _mark_changed(downstream_segment)

        if not changed_drawings:
            self._show_transform_status(
                "Selected element could not be extended",
                2500,
            )
            return False

        try:
            if self._selection_manager is not None:
                self._selection_manager.notify_drawings_changed()
        except Exception:
            pass

        # Only selected target remains highlighted.
        self._finish_transform_refresh(highlight_drawings or changed_drawings)

        self._show_transform_status(
            f"Extended selected line by {distance_m:g} m",
            2500,
        )

        return True


    def rotate_selected_element(self, degrees=None, pivot=None):
        drawings = self._get_transform_selection()
        if not drawings:
            self._show_transform_status("Select a drawing first, then use Rotate", 3000)
            return False

        if degrees is None:
            return self.show_rotate_dialog()

        try:
            degrees = float(degrees)
        except Exception:
            self._show_transform_status("Invalid rotation angle", 3000)
            return False

        if pivot is None:
            center = self._transform_selection_center(drawings)
        else:
            center = pivot

        if center is None:
            self._show_transform_status("Selected drawing has no transformable points", 3000)
            return False

        self._save_state()

        # Positive user angle means CLOCKWISE.
        theta = math.radians(-degrees)
        cos_a = math.cos(theta)
        sin_a = math.sin(theta)

        for drawing in drawings:
            coords = drawing.get("coords") or []
            new_coords = [self._rotate_point_xy(pt, center, cos_a, sin_a) for pt in coords]
            self._set_transformed_coords(drawing, new_coords)
            self._rebuild_transformed_drawing(drawing)

        self._finish_transform_refresh(drawings)

        if pivot is None:
            self._show_transform_status(
                f"Rotated {len(drawings)} drawing(s) clockwise by {degrees:g}°",
                2500
            )
        else:
            self._show_transform_status(
                f"Rotated {len(drawings)} drawing(s) clockwise by {degrees:g}° from selected vertex",
                2500
            )

        return True

    def show_rotate_dialog(self):
        try:
            from PySide6.QtWidgets import QInputDialog
        except Exception:
            from PyQt5.QtWidgets import QInputDialog

        drawings = self._get_transform_selection()
        if not drawings:
            self._show_transform_status("Select a drawing first, then use Rotate", 3000)
            return False

        parent = getattr(self, "app", None)
        option, ok = QInputDialog.getItem(
            parent,
            "Rotate",
            "Choose rotation method:",
            ["Enter Angle", "Vertex Pivot", "Corner Drag"],
            0,
            False,
        )
        if not ok:
            return False

        option_text = str(option).strip().lower()

        if option_text == "corner drag":
            return self.start_corner_drag_rotate()

        degrees, ok = QInputDialog.getDouble(
            parent,
            "Rotate",
            "Rotation angle in degrees (clockwise):",
            0.0,
            -3600.0,
            3600.0,
            2,
        )
        if not ok:
            return False

        if option_text == "vertex pivot":
            return self.start_vertex_pivot_rotate(degrees)

        return self.rotate_selected_element(degrees)
    def _cancel_vertex_pivot_rotate(self):
        """Cancel pending vertex-pivot rotate mode."""
        try:
            if self._rotate_vertex_pivot_observer_id is not None:
                self.interactor.RemoveObserver(self._rotate_vertex_pivot_observer_id)
        except Exception:
            pass

        try:
            if self._rotate_vertex_pivot_right_observer_id is not None:
                self.interactor.RemoveObserver(self._rotate_vertex_pivot_right_observer_id)
        except Exception:
            pass

        self._rotate_vertex_pivot_observer_id = None
        self._rotate_vertex_pivot_right_observer_id = None
        self._rotate_vertex_pivot_pending_degrees = None
        self._rotate_vertex_pivot_drawings = []
        self._clear_rotate_vertex_pivot_vertices()

        try:
            if getattr(self, "active_tool", None) == "rotatepivot":
                self.active_tool = None
        except Exception:
            pass

        try:
            if hasattr(self, "_restore_element_select_after_corner_rotate"):
                self._restore_element_select_after_corner_rotate()
        except Exception as e:
            print(f"⚠️ Vertex pivot restore selection warning: {e}")

    def _nearest_selected_vertex(self, drawings, world_pt, tolerance_px=24.0):
        """Return nearest vertex from selected drawings near clicked world point."""
        if world_pt is None:
            return None

        try:
            tolerance = float(self._screen_to_world_tolerance(tolerance_px))
        except Exception:
            pts = []
            for drawing in drawings:
                for pt in drawing.get("coords", []) or []:
                    if len(pt) >= 2:
                        pts.append((float(pt[0]), float(pt[1])))
            if pts:
                arr = np.asarray(pts, dtype=np.float64)
                diag = np.linalg.norm(arr.max(axis=0) - arr.min(axis=0))
                tolerance = max(diag * 0.04, 1e-6)
            else:
                tolerance = 1.0

        wx = float(world_pt[0])
        wy = float(world_pt[1])

        best_pt = None
        best_dist = None

        for drawing in drawings:
            for pt in drawing.get("coords", []) or []:
                if len(pt) < 2:
                    continue

                dx = float(pt[0]) - wx
                dy = float(pt[1]) - wy
                dist = (dx * dx + dy * dy) ** 0.5

                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    z = float(pt[2]) if len(pt) > 2 else 0.0
                    best_pt = (float(pt[0]), float(pt[1]), z)

        if best_pt is not None and best_dist is not None and best_dist <= tolerance:
            return best_pt

        return None

    def _collect_transform_vertex_points(self, drawings):
        """Collect unique drawing vertices for temporary transform overlays."""
        points = []
        seen = set()

        for drawing in list(drawings or []):
            if not isinstance(drawing, dict) or drawing.get("type") == "text":
                continue

            for pt in self._get_drawing_coords(drawing):
                if pt is None or len(pt) < 2:
                    continue

                vertex = (
                    float(pt[0]),
                    float(pt[1]),
                    float(pt[2]) if len(pt) > 2 else 0.0,
                )
                if vertex in seen:
                    continue

                seen.add(vertex)
                points.append(vertex)

        return points

    def _show_rotate_vertex_pivot_vertices(self, drawings):
        """Display temporary vertices while vertex-pivot rotate is active."""
        points = self._collect_transform_vertex_points(drawings)
        self._rotate_vertex_pivot_showing_vertices = False

        if not points:
            try:
                self.clear_coordinate_labels()
            except Exception:
                pass
            return False

        self.show_vertex_coordinates(points)
        self._rotate_vertex_pivot_showing_vertices = True
        return True

    def _clear_rotate_vertex_pivot_vertices(self):
        """Hide temporary rotate-pivot vertex markers."""
        if not getattr(self, "_rotate_vertex_pivot_showing_vertices", False):
            return

        self._rotate_vertex_pivot_showing_vertices = False
        try:
            self.clear_coordinate_labels()
        except Exception:
            pass

        try:
            self.app.vtk_widget.render()
        except Exception:
            pass

    def start_vertex_pivot_rotate(self, degrees):
        """
        Start rotate mode where user clicks a drawing vertex.
        The clicked vertex becomes the rotation pivot.
        """
        drawings = self._get_transform_selection()
        if not drawings:
            self._show_transform_status("Select a drawing first, then use Rotate", 3000)
            return False

        try:
            degrees = float(degrees)
        except Exception:
            self._show_transform_status("Invalid rotation angle", 3000)
            return False

        self._cancel_vertex_pivot_rotate()
        selection_suspended = False

        try:
            selection_suspended = self._suspend_element_select_for_corner_rotate()

            self._rotate_vertex_pivot_pending_degrees = degrees
            self._rotate_vertex_pivot_drawings = list(drawings)
            self.active_tool = "rotatepivot"

            self._rotate_vertex_pivot_observer_id = self.interactor.AddObserver(
                "LeftButtonPressEvent",
                self._on_vertex_pivot_rotate_click,
                100.0
            )

            self._rotate_vertex_pivot_right_observer_id = self.interactor.AddObserver(
                "RightButtonPressEvent",
                self._on_vertex_pivot_rotate_right_click,
                100.0
            )

        except Exception as e:
            self._cancel_vertex_pivot_rotate()
            self._show_transform_status(f"Vertex Pivot Rotate failed: {e}", 3000)
            return False

        try:
            self._show_rotate_vertex_pivot_vertices(drawings)
        except Exception as e:
            print(f"⚠️ Vertex Pivot Rotate marker refresh failed: {e}")

        self._show_transform_status(
            "Vertex Pivot Rotate: click a vertex on the selected drawing. Right-click to stop.",
            5000
        )

        return True

    def _on_vertex_pivot_rotate_click(self, obj, evt):
        """Apply vertex-pivot rotate on every left click until right click cancels."""
        self._consume_vtk_event(obj)

        drawings = list(getattr(self, "_rotate_vertex_pivot_drawings", []) or [])
        degrees = getattr(self, "_rotate_vertex_pivot_pending_degrees", None)

        if not drawings or degrees is None:
            self._cancel_vertex_pivot_rotate()
            return

        try:
            try:
                world_pt = self._get_mouse_world_no_snap()
            except Exception:
                world_pt = self._get_mouse_world()

            pivot = self._nearest_selected_vertex(drawings, world_pt)
            if pivot is None:
                self._show_transform_status(
                    "Click closer to a selected drawing vertex. Right-click to stop.",
                    2500
                )
                return

            # IMPORTANT:
            # Do NOT cancel vertex-pivot mode here.
            # Each left-click applies the same angle again.
            self.rotate_selected_element(degrees, pivot=pivot)

            # Keep latest selected drawings alive after rebuild/refresh.
            refreshed = self._get_transform_selection()
            if refreshed:
                self._rotate_vertex_pivot_drawings = list(refreshed)
            else:
                self._rotate_vertex_pivot_drawings = drawings

            self.active_tool = "rotatepivot"

            try:
                self._show_rotate_vertex_pivot_vertices(self._rotate_vertex_pivot_drawings)
            except Exception as marker_error:
                print(f"⚠️ Vertex Pivot Rotate marker refresh failed: {marker_error}")

            self._show_transform_status(
                f"Rotated {len(drawings)} drawing(s) {degrees:g}°. "
                "Click another vertex to rotate again, or right-click to stop.",
                3000
            )

        except Exception as e:
            self._show_transform_status(f"Vertex Pivot Rotate failed: {e}", 3000)
    
    def _on_vertex_pivot_rotate_right_click(self, obj, evt):
        """Stop vertex-pivot rotate mode on right click."""
        self._consume_vtk_event(obj)
        self._cancel_vertex_pivot_rotate()
        self._show_transform_status("Vertex Pivot Rotate stopped", 2000)

    def mirror_selected_element(self, axis="x"):
        drawings = self._get_transform_selection()
        if not drawings:
            self._show_transform_status("Select a drawing first, then use Mirror", 3000)
            return False

        center = self._transform_selection_center(drawings)
        if center is None:
            self._show_transform_status("Selected drawing has no transformable points", 3000)
            return False

        axis_label = str(axis).strip().lower()
        if axis_label in ("x", "horizontal"):
            axis_degrees = 0.0
            label = "X"
        elif axis_label in ("y", "vertical"):
            axis_degrees = 90.0
            label = "Y"
        else:
            try:
                axis_degrees = float(axis)
                label = f"{axis_degrees:g}°"
            except Exception:
                self._show_transform_status("Invalid mirror axis", 3000)
                return False

        self._save_state()

        for drawing in drawings:
            coords = drawing.get("coords") or []
            new_coords = [self._mirror_point_axis_xy(pt, center, axis_degrees) for pt in coords]
            if drawing.get("source") == "curve_tool" and drawing.get("control_points"):
                drawing["control_points"] = [
                    self._mirror_point_axis_xy(pt, center, axis_degrees)
                    for pt in drawing.get("control_points", [])
                ]
            self._set_transformed_coords(drawing, new_coords)
            self._rebuild_transformed_drawing(drawing)

        self._finish_transform_refresh(drawings)
        self._show_transform_status(f"Mirrored {len(drawings)} drawing(s) on {label} axis", 2500)
        return True

    def show_mirror_menu(self, button=None):
        try:
            from PySide6.QtWidgets import QMenu, QInputDialog
        except Exception:
            from PyQt5.QtWidgets import QMenu, QInputDialog

        menu = QMenu(getattr(self, "app", None))
        mirror_x_action = menu.addAction("Mirror X")
        mirror_y_action = menu.addAction("Mirror Y")
        custom_action = menu.addAction("Mirror Axis...")

        mirror_x_action.triggered.connect(lambda: self.mirror_selected_element("x"))
        mirror_y_action.triggered.connect(lambda: self.mirror_selected_element("y"))

        def _custom_axis():
            parent = getattr(self, "app", None)
            degrees, ok = QInputDialog.getDouble(
                parent,
                "Mirror Axis",
                "Mirror axis angle in degrees:",
                0.0,
                -3600.0,
                3600.0,
                2,
            )
            if ok:
                self.mirror_selected_element(degrees)

        custom_action.triggered.connect(_custom_axis)

        try:
            if button is not None:
                menu.exec(button.mapToGlobal(button.rect().bottomLeft()))
            else:
                menu.exec(QCursor.pos())
        except AttributeError:
            if button is not None:
                menu.exec_(button.mapToGlobal(button.rect().bottomLeft()))
            else:
                menu.exec_(QCursor.pos())
        return True

    def _prepare_corner_rotate_modal_state(self):
        """
        Enter modal Corner Rotate mode safely.

        Clears unfinished Smart/Line/Ortho/Polyline/Rect/Circle previews before
        rotating, so old draw tools cannot create extra lines or rectangles.
        """
        self.temp_points = []
        self.left_down = False
        self.dragging_vertex = None
        self.vertex_move_mode = False

        try:
            if getattr(self, "_placing_text", False) or getattr(self, "active_tool", None) == "text":
                self._cleanup_text_tool()
        except Exception:
            pass

        try:
            old_ortho = getattr(self, "_ortho_polygon_tool", None)
            if old_ortho is not None:
                if hasattr(old_ortho, "_cancel"):
                    old_ortho._cancel()
                if hasattr(old_ortho, "deactivate"):
                    old_ortho.deactivate()
                self._ortho_polygon_tool = None
        except Exception:
            pass

        try:
            self._clear_suspended_preview_actor()
        except Exception:
            pass

        try:
            self._clear_live_preview_actors()
        except Exception:
            for attr in (
                "_preview_line_actor",
                "_continuous_line_actor",
                "_rectangle_preview_actor",
                "_circle_preview_actor",
                "_circle_preview_actor_2d",
                "_preview_actor",
                "_freehand_preview_actor_2d",
                "_suspended_preview_actor",
            ):
                try:
                    self._remove_preview_actor_2d(attr)
                except Exception:
                    actor = getattr(self, attr, None)
                    if actor is not None:
                        for renderer in (getattr(self, "overlay_renderer", None), getattr(self, "renderer", None)):
                            if renderer is None:
                                continue
                            try:
                                renderer.RemoveActor(actor)
                            except Exception:
                                pass
                            try:
                                renderer.RemoveActor2D(actor)
                            except Exception:
                                pass
                            try:
                                renderer.RemoveViewProp(actor)
                            except Exception:
                                pass
                    try:
                        setattr(self, attr, None)
                    except Exception:
                        pass

        for attr_name in (
            "_smartline_vertex_markers",
            "_line_vertex_markers",
            "_polyline_vertex_markers",
        ):
            markers = getattr(self, attr_name, None)
            if isinstance(markers, (list, tuple, set)):
                for marker in list(markers):
                    try:
                        self._remove_actor_from_overlay(marker)
                    except Exception:
                        for renderer in (getattr(self, "overlay_renderer", None), getattr(self, "renderer", None)):
                            if renderer is None:
                                continue
                            try:
                                renderer.RemoveActor(marker)
                            except Exception:
                                pass
                            try:
                                renderer.RemoveViewProp(marker)
                            except Exception:
                                pass
            try:
                setattr(self, attr_name, [])
            except Exception:
                pass

        try:
            self.clear_coordinate_labels()
        except Exception:
            pass

        try:
            self._hide_snap_marker_now()
        except Exception:
            pass

        self.active_tool = "cornerrotate"

        try:
            if self.overlay_renderer is not None:
                self.overlay_renderer.Modified()
            if self.renderer is not None:
                self.renderer.Modified()
            self.app.vtk_widget.render()
        except Exception:
            try:
                self._force_render()
            except Exception:
                pass

    def _suspend_element_select_for_corner_rotate(self):
        """Disable live element-pick observers while corner-drag rotate is active."""
        tool = getattr(self, "_element_select_tool", None)
        if tool is None or not tool.is_active():
            self._corner_rotate_selection_restore = None
            return False

        self._corner_rotate_selection_restore = {
            "method": getattr(tool, "_method", "individual"),
            "enclose_mode": getattr(tool, "_enclose_mode", "overlap"),
        }

        dialog = getattr(self.app, "_element_selection_dialog", None)
        if dialog is not None and hasattr(dialog, "suspend_pick_interaction"):
            try:
                dialog.suspend_pick_interaction()
                return True
            except Exception as e:
                print(f"⚠️ Corner rotate suspend dialog warning: {e}")

        try:
            self.deactivate_element_select_tool()
        except Exception as e:
            print(f"⚠️ Corner rotate suspend selection warning: {e}")
        return True

    def _restore_element_select_after_corner_rotate(self):
        """Restore element selection if its control dialog is still open."""
        restore_state = getattr(self, "_corner_rotate_selection_restore", None)
        self._corner_rotate_selection_restore = None
        if not restore_state:
            return

        dialog = getattr(self.app, "_element_selection_dialog", None)
        if dialog is None:
            return
        try:
            if not dialog.isVisible():
                return
            if dialog.windowState() & Qt.WindowMinimized:
                return
        except Exception:
            return

        if hasattr(dialog, "restore_pick_interaction"):
            try:
                dialog.restore_pick_interaction()
                return
            except Exception as e:
                print(f"⚠️ Corner rotate restore dialog warning: {e}")

        try:
            self.activate_element_select_tool(
                method=restore_state.get("method", "individual"),
                delete_on_click=False,
            )
            tool = getattr(self, "_element_select_tool", None)
            if tool is not None and hasattr(tool, "set_enclose_mode"):
                tool.set_enclose_mode(restore_state.get("enclose_mode", "overlap"))
        except Exception as e:
            print(f"⚠️ Corner rotate restore selection warning: {e}")

    def start_corner_drag_rotate(self):
        """
        Start box/corner drag rotation from inside digitize_tools.py.

        This keeps Rotate / Mirror optimized in one file and avoids importing
        gui.corner_rotate_tool.
        """
        drawings = self._get_transform_selection()
        if not drawings:
            self._show_transform_status("Select a drawing first, then use Rotate", 3000)
            return False

        selection_suspended = False
        try:
            selection_suspended = self._suspend_element_select_for_corner_rotate()
            if not hasattr(self, "_corner_rotate_tool") or self._corner_rotate_tool is None:
                self._corner_rotate_tool = DigitizeCornerRotateTool(self)

            started = self._corner_rotate_tool.activate(drawings)
            if not started and selection_suspended:
                self._restore_element_select_after_corner_rotate()
            return started
        except Exception as e:
            print(f"❌ Corner Rotate tool failed: {e}")
            if selection_suspended:
                self._restore_element_select_after_corner_rotate()
            self._show_transform_status("Corner Rotate failed to start", 3000)
            return False

    def _on_zoom(self, obj, evt):
        """Schedule preview update AFTER VTK finishes processing zoom."""
        if not self.temp_points:
            return
        # ✅ VTK hasn't zoomed the camera yet — defer to next event loop
        try:
            from PySide6.QtCore import QTimer
        except ImportError:
            from PyQt5.QtCore import QTimer
        QTimer.singleShot(0, self._deferred_preview_update)

    def _normalize_line_style(self, line_style):
        """Normalize UI/database line style names to digitizer geometry style keys."""
        style = str(line_style or "solid").strip().lower()
        style = style.replace("_", "-").replace(" ", "-")

        aliases = {
            "solid": "solid",
            "continuous": "solid",
            "normal": "solid",
            "dashed": "dashed",
            "dash": "dashed",
            "dotted": "dotted",
            "dot": "dotted",
            "dash-dot": "dash-dot",
            "dashdot": "dash-dot",
            "dash-dot-dot": "dash-dot-dot",
            "dashdotdot": "dash-dot-dot",
        }
        return aliases.get(style, "solid")

    def _get_active_layer_draw_style_override(self):
        """Read active DXF/SNT layer drawing style pushed by dxf_attachment.py/snt_attachment.py."""
        app = getattr(self, "app", None)
        if app is None:
            return {}

        override = {}

        active_color = getattr(app, "active_snt_color", None)
        if active_color is None:
            active_color = getattr(app, "active_dxf_color", None)
        if active_color is not None:
            try:
                override["color"] = (
                    float(active_color[0]) / 255.0,
                    float(active_color[1]) / 255.0,
                    float(active_color[2]) / 255.0,
                )
            except Exception:
                pass

        for attr in (
            "active_dxf_line_width", "active_snt_line_width", "active_line_width",
            "current_line_width", "draw_line_width", "line_width",
        ):
            try:
                value = getattr(app, attr, None)
                if value is not None:
                    override["width"] = max(1.0, float(value))
                    break
            except Exception:
                pass

        for attr in (
            "active_dxf_line_style", "active_snt_line_style", "active_line_style",
            "current_line_style", "draw_line_style", "line_style",
        ):
            try:
                value = getattr(app, attr, None)
                if value:
                    override["style"] = self._normalize_line_style(value)
                    break
            except Exception:
                pass

        return override

    def _get_draw_style(self, tool_key):
        """Return a draw style merged with defaults so previews use configured colors immediately.

        If an SBM class is active (app.active_sbm_class), its CO colour and WT
        weight override the per-tool saved style so every tool draws in the
        class colour automatically.

        If no SBM class is active, active SNT/DXF layer color + line width +
        line style override the saved draw-tool defaults.
        """
        default_style = getattr(self, "default_draw_tool_styles", {}).get(
            tool_key, {'color': (1.0, 0.0, 0.0), 'width': 2, 'style': 'solid'}
        )
        style = dict(default_style)
        style.update((getattr(self, "draw_tool_styles", {}) or {}).get(tool_key, {}) or {})
        style["style"] = self._normalize_line_style(style.get("style", "solid"))

        active_sbm = getattr(getattr(self, "app", None), "active_sbm_class", None)
        if active_sbm:
            r, g, b = active_sbm["rgb"]
            style["color"] = (r / 255.0, g / 255.0, b / 255.0)
            if active_sbm.get("wt"):
                style["width"] = int(active_sbm["wt"])
            style["style"] = self._normalize_line_style(style.get("style", "solid"))
            return style

        layer_override = self._get_active_layer_draw_style_override()
        if layer_override:
            style.update(layer_override)
            style["style"] = self._normalize_line_style(style.get("style", "solid"))

        return style



    def _tag_sbm(self, drawing_entry: dict) -> dict:
        """Attach active SBM class metadata to a drawing entry (in-place).

        Stores ``sbm_class`` dict on the entry so downstream export / SNT
        writers know which layer/class this geometry belongs to.
        """
        active_sbm = getattr(getattr(self, "app", None), "active_sbm_class", None)
        if active_sbm:
            drawing_entry["sbm_class"] = dict(active_sbm)
        return drawing_entry



    # ------------------------------------------------------------------
    # MicroStation-style selection set (lazy — created on first access)
    # ------------------------------------------------------------------
    @property
    def selection_manager(self):
        if self._selection_manager is None:
            try:
                from gui.selection_manager import SelectionManager
                self._selection_manager = SelectionManager(self)
            except Exception as e:
                print(f"⚠️ SelectionManager init failed: {e}")
        return self._selection_manager

    def _notify_selection_drawings_changed(self):
        """Tell the selection manager the drawings list mutated. Safe no-op
        if the manager was never instantiated."""
        if self._selection_manager is not None:
            try:
                self._selection_manager.notify_drawings_changed()
            except Exception as e:
                print(f"⚠️ SelectionManager notify failed: {e}")

    def _deferred_preview_update(self):
        """Reproject all 2D preview actors AFTER camera has finished moving."""
        if not self.temp_points or not self.active_tool:
            return

        mouse_x, mouse_y = self.interactor.GetEventPosition()

        if self.active_tool in ("smartline", "line"):
            style = self._get_draw_style(self.active_tool)
            color = style['color']
            width = style['width']
            line_style = style['style']
            if len(self.temp_points) >= 2:
                self._update_continuous_preview(color=color, width=width, line_style=line_style)
            if len(self.temp_points) >= 1:
                self._update_cursor_preview(color=color, width=width, line_style=line_style)

        elif self.active_tool == "polyline":
            style = self._get_draw_style('polyline')
            color = style['color']
            width = style['width']
            line_style = style['style']
            if len(self.temp_points) >= 2:
                self._update_continuous_preview(color=color, width=width, line_style=line_style)
            if len(self.temp_points) >= 1:
                self._update_cursor_preview(color=color, width=width, line_style='dotted', close_loop=True)

        elif self.active_tool == "rectangle" and len(self.temp_points) == 1:
            style = self._get_draw_style('rectangle')
            self._update_rectangle_preview(color=style['color'], width=style['width'], line_style=style['style'])

        elif self.active_tool == "circle" and len(self.temp_points) == 1:
            style = self._get_draw_style('circle')
            self._update_circle_preview_world(color=style['color'], width=style['width'], line_style=style['style'])

        elif self.active_tool == "freehand" and len(self.temp_points) >= 2:
            fh_style = self._get_draw_style('freehand')
            self._update_freehand_preview_world(
                color=fh_style['color'],
                width=fh_style['width'],
                line_style=fh_style['style'],
            )

        elif self.active_tool == "hatcharea":
            style = self._get_draw_style('hatcharea')
            if len(self.temp_points) >= 2:
                self._update_continuous_preview(
                    color=style['color'],
                    width=style['width'],
                    line_style=style['style'],
                )
            if len(self.temp_points) >= 1:
                self._update_cursor_preview(
                    color=style['color'],
                    width=style['width'],
                    line_style='dotted',
                    close_loop=True,
                )

        self.app.vtk_widget.render()

    def _update_continuous_preview(self, color, width=3, line_style='solid'):
        """Keep placed preview vertices in world space so pan/zoom cannot detach them."""
        if len(self.temp_points) < 2:
            self._remove_preview_actor_2d('_continuous_line_actor')
            return

        self._update_continuous_line_world(
            '_continuous_line_actor',
            list(self.temp_points),
            color=color,
            width=width,
            line_style=line_style,
        )

    def _update_cursor_preview(self, color, width=2, line_style='solid', close_loop=False, current_world=None):
        """Keep the live rubber-band preview in world space during pan/zoom."""
        if len(self.temp_points) < 1:
            self._remove_preview_actor_2d('_preview_line_actor')
            return

        if current_world is None:
            current_world = self._get_mouse_world()
        preview_points = [tuple(self.temp_points[-1]), tuple(current_world)]
        if close_loop and self.temp_points:
            preview_points.append(tuple(self.temp_points[0]))

        self._update_continuous_line_world(
            '_preview_line_actor',
            preview_points,
            color=color,
            width=width,
            line_style=line_style,
        )

    def _build_rectangle_preview_points(self, p1, p2):
        """Build rectangle preview coordinates in world space."""
        x1, y1, z1 = p1
        x2, y2, z2 = p2
        return [
            (x1, y1, z1),
            (x2, y1, z1),
            (x2, y2, z2),
            (x1, y2, z2),
            (x1, y1, z1),
        ]

    def _build_circle_preview_points(self, center, edge, n=None):
        """Build circle preview coordinates in world space."""
        radius = np.sqrt((edge[0] - center[0]) ** 2 + (edge[1] - center[1]) ** 2)
        if radius <= 0:
            return []

        if n is None:
            n = self._get_circle_segment_count(center, edge)

        thetas = np.linspace(0, 2 * np.pi, n, endpoint=False)
        coords = [
            (center[0] + radius * np.cos(t), center[1] + radius * np.sin(t), center[2])
            for t in thetas
        ]
        coords.append(coords[0])
        return coords

    def _get_circle_segment_count(self, center, edge, min_segments=128, max_segments=1440):
        """Choose circle resolution from current on-screen size so zoomed previews stay smooth."""
        try:
            screen_radius = float(self._world_to_screen_distance(center, edge))
        except Exception:
            screen_radius = 0.0

        if screen_radius > 0.0:
            circumference_px = 2.0 * np.pi * screen_radius
            segments = int(np.ceil(circumference_px / 2.0))  # about one segment per ~2 px
        else:
            world_radius = float(np.sqrt((edge[0] - center[0]) ** 2 + (edge[1] - center[1]) ** 2))
            segments = 128 if world_radius < 50 else 180

        segments = max(min_segments, min(max_segments, segments))
        return int(np.ceil(segments / 8.0) * 8)

    def _update_rectangle_preview(self, color, width=2, line_style='solid'):
        """Keep rectangle preview in world space so it stays aligned while panning."""
        if len(self.temp_points) != 1:
            self._remove_preview_actor_2d('_rectangle_preview_actor')
            return

        world_pos = self._get_mouse_world_no_snap()
        coords = self._build_rectangle_preview_points(self.temp_points[0], world_pos)
        self._update_continuous_line_world(
            '_rectangle_preview_actor',
            coords,
            color=color,
            width=width,
            line_style=line_style,
        )

    def _update_circle_preview_world(self, color, width=2, line_style='solid'):
        """Keep circle preview in world space so it stays aligned while panning."""
        if len(self.temp_points) != 1:
            self._remove_preview_actor_2d('_circle_preview_actor')
            self._remove_preview_actor_2d('_circle_preview_actor_2d')
            return

        self._remove_preview_actor_2d('_circle_preview_actor_2d')
        world_pos = self._get_mouse_world_no_snap()
        coords = self._build_circle_preview_points(self.temp_points[0], world_pos)
        if coords:
            self._update_continuous_line_world(
                '_circle_preview_actor',
                coords,
                color=color,
                width=width,
                line_style=line_style,
            )
            actor = getattr(self, '_circle_preview_actor', None)
            if actor is not None:
                try:
                    actor.GetProperty().RenderLinesAsTubesOn()
                except Exception:
                    pass
        else:
            self._remove_preview_actor_2d('_circle_preview_actor')

    def _update_freehand_preview_world(self, color, width=2, line_style='solid'):
        """Keep freehand preview in world space so it stays aligned while panning."""
        if len(self.temp_points) < 2:
            self._remove_preview_actor_2d('_preview_actor')
            self._remove_preview_actor_2d('_freehand_preview_actor_2d')
            return

        self._update_continuous_line_world(
            '_preview_actor',
            list(self.temp_points),
            color=color,
            width=width,
            line_style=line_style,
        )

    def _get_line_dash_pattern(self, line_style):
        """Return the visible dash pattern in screen-pixel units."""
        line_style = self._normalize_line_style(line_style)
        if line_style == 'dashed':
            return [(10.0, 6.0)]
        if line_style == 'dotted':
            return [(2.0, 6.0)]
        if line_style == 'dash-dot':
            return [(10.0, 6.0), (2.0, 6.0)]
        if line_style == 'dash-dot-dot':
            return [(10.0, 6.0), (2.0, 4.0), (2.0, 6.0)]
        return None

    def _build_styled_polydata_world(self, world_points, line_style='solid'):
        """
        Build world-space polydata with visible line styles using explicit segments.

        ✅ ZOOM FIX: Re-centres all geometry around world_points[0] before
        inserting into vtkPoints.  Large coordinate values (e.g. 456789.123)
        lose sub-millimetre precision when passed through VTK's float32
        pipeline stages, causing visible shape distortion at high zoom.
        Keeping values near zero preserves full float64 precision.
        The returned poly carries a _world_origin attribute so callers can
        shift the actor back to the correct world location via SetPosition().
        """
        poly = vtk.vtkPolyData()
        pts  = vtk.vtkPoints()
        pts.SetDataTypeToDouble()
        lines = vtk.vtkCellArray()

        # Always tag origin so callers never have to guard against missing attr
        if not world_points or len(world_points) < 2:
            poly.SetPoints(pts)
            poly.SetLines(lines)
            poly.Modified()
            poly._world_origin = np.zeros(3, dtype=np.float64)
            return poly

        # ── Re-centre around the first vertex ──────────────────────────────
        origin = np.array(
            [float(world_points[0][0]),
             float(world_points[0][1]),
             float(world_points[0][2])],
            dtype=np.float64,
        )
        centred = [
            np.array([float(p[0]), float(p[1]), float(p[2])], dtype=np.float64) - origin
            for p in world_points
        ]
        # ───────────────────────────────────────────────────────────────────

        dash_pattern = self._get_line_dash_pattern(line_style)

        if not dash_pattern:
            # Solid line — single polyline cell using centred coords
            for p in centred:
                pts.InsertNextPoint(float(p[0]), float(p[1]), float(p[2]))

            n = pts.GetNumberOfPoints()
            lines.InsertNextCell(n)
            for i in range(n):
                lines.InsertCellPoint(i)

            poly.SetPoints(pts)
            poly.SetLines(lines)
            poly.Modified()
            poly._world_origin = origin
            return poly

        # Dashed / dotted — explicit segment pairs using centred coords
        point_idx = 0
        for seg_idx in range(len(centred) - 1):
            p1 = centred[seg_idx]
            p2 = centred[seg_idx + 1]

            world_vec = p2 - p1
            world_len = np.linalg.norm(world_vec)
            if world_len < 1e-9:
                continue

            # Screen-length must use original world coords for correct projection
            screen_len = float(self._world_to_screen_distance(
                world_points[seg_idx], world_points[seg_idx + 1]))
            if screen_len < 1e-6:
                screen_len = world_len

            direction = world_vec / world_len
            t_px      = 0.0
            pattern_idx = 0

            while t_px < screen_len:
                dash_length, gap_length = dash_pattern[pattern_idx]
                dash_end_px = min(t_px + dash_length, screen_len)
                if dash_end_px - t_px <= 1e-6:
                    break

                start_ratio = t_px       / screen_len
                end_ratio   = dash_end_px / screen_len
                dash_start  = p1 + direction * (world_len * start_ratio)
                dash_end    = p1 + direction * (world_len * end_ratio)

                pts.InsertNextPoint(
                    float(dash_start[0]), float(dash_start[1]), float(dash_start[2]))
                start_idx  = point_idx
                point_idx += 1

                pts.InsertNextPoint(
                    float(dash_end[0]), float(dash_end[1]), float(dash_end[2]))
                end_idx    = point_idx
                point_idx += 1

                lines.InsertNextCell(2)
                lines.InsertCellPoint(start_idx)
                lines.InsertCellPoint(end_idx)

                t_px        = dash_end_px + gap_length
                pattern_idx = (pattern_idx + 1) % len(dash_pattern)

        poly.SetPoints(pts)
        poly.SetLines(lines)
        poly.Modified()
        poly._world_origin = origin
        return poly

    def _apply_world_line_style(self, prop, line_style='solid'):
        """World actors use explicit segmented geometry, so keep the property itself solid."""
        try:
            prop.SetLineStippleRepeatFactor(1)
            prop.SetLineStipplePattern(0xFFFF)
        except Exception:
            pass

    def _update_continuous_line_world(self, attr_name, world_points, color=(0,1,0), width=3, line_style='solid'):
        """
        World-space preview geometry — zooms/pans perfectly with no lag.
        Uses vtkPolyDataMapper (3D) instead of 2D screen-space mapper.

        ✅ ZOOM FIX: _build_styled_polydata_world now re-centres geometry and
        tags poly._world_origin.  We apply that offset via actor.SetPosition()
        so the actor lands in the correct world location at any zoom level.
        """
        if not world_points or len(world_points) < 2:
            actor = getattr(self, attr_name, None)
            if actor:
                self.overlay_renderer.RemoveActor(actor)
                setattr(self, attr_name, None)
            return

        poly = self._build_styled_polydata_world(world_points, line_style=line_style)

        # ✅ Retrieve the re-centring origin written by _build_styled_polydata_world
        origin = getattr(poly, '_world_origin', np.zeros(3, dtype=np.float64))

        actor = getattr(self, attr_name, None)
        if actor is not None and actor.IsA("vtkActor2D"):
            self._remove_preview_actor_2d(attr_name)
            actor = None

        if actor is None:
            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputData(poly)
            mapper.SetResolveCoincidentTopologyToPolygonOffset()
            mapper.SetRelativeCoincidentTopologyLineOffsetParameters(-3, -3)
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(float(color[0]), float(color[1]), float(color[2]))
            actor.GetProperty().SetLineWidth(float(width))
            actor.GetProperty().SetOpacity(1.0)
            self._apply_world_line_style(actor.GetProperty(), line_style=line_style)
            actor.PickableOff()
            actor.SetVisibility(1)
            # ✅ Shift actor back to true world position
            actor.SetPosition(float(origin[0]), float(origin[1]), float(origin[2]))
            self._add_actor_to_overlay(actor)
            setattr(self, attr_name, actor)
        else:
            _dm = actor.GetMapper()
            if _dm is not None:
                _dm.SetInputData(poly)
                _dm.Modified()
            actor.GetProperty().SetColor(float(color[0]), float(color[1]), float(color[2]))
            actor.GetProperty().SetLineWidth(float(width))
            self._apply_world_line_style(actor.GetProperty(), line_style=line_style)
            # ✅ Always update position — origin may differ between calls
            actor.SetPosition(float(origin[0]), float(origin[1]), float(origin[2]))
            actor.SetVisibility(1)

    def _restore_shared_interactor_observers(self):
        """Re-install non-digitizer observers that may be removed during tool switches."""
        grid_manager = getattr(self.app, 'grid_label_manager', None)
        if not grid_manager:
            return

        try:
            if hasattr(grid_manager, 'ensure_interactor_observers'):
                grid_manager.ensure_interactor_observers()
            else:
                grid_manager.setup_interactor()
        except Exception as e:
            print(f"⚠️ Failed to restore grid-label observers: {e}")

    def is_text_ui_editing_active(self) -> bool:
        """True while a Digitizer text edit/input dialog is active."""
        dlg = getattr(self, "_text_dialog", None)
        if dlg is not None:
            try:
                if dlg.isVisible():
                    return True
            except Exception:
                pass
        return bool(getattr(self, "_text_ui_editing", False))

    def _rebind_primary_draw_observers(self, include_left_release: bool = True):
        """
        Rebind only digitizer-owned primary draw observers.
        Never removes global/shared observers installed by other systems.
        """
        for oid in list(getattr(self, "_draw_observer_ids", []) or []):
            try:
                self.interactor.RemoveObserver(oid)
            except Exception:
                pass
        self._draw_observer_ids = []

        self._draw_observer_ids.append(
            self.interactor.AddObserver("LeftButtonPressEvent", self._on_left_press, 1.0)
        )
        self._draw_observer_ids.append(
            self.interactor.AddObserver("MouseMoveEvent", self._on_mouse_move, 1.0)
        )
        if include_left_release:
            self._draw_observer_ids.append(
                self.interactor.AddObserver("LeftButtonReleaseEvent", self._on_left_release, 1.0)
            )
        self._draw_observer_ids.append(
            self.interactor.AddObserver("RightButtonPressEvent", self._on_right_press, 1.0)
        )

    def _reinstall_all_observers(self):
        """Re-install every VTK interactor observer owned by the digitizer.

        Idempotent: safe after ObserverRegistry.release_all(), after the
        interactor is recreated, and after project / point-cloud / grid clears.
        Call _check_and_update_renderers() FIRST so self.interactor points at
        the live interactor before observers are attached.
        """
        if self.interactor is None:
            return

        # 1) digitizer-owned primary draw observers (self-removing → idempotent)
        self._rebind_primary_draw_observers(include_left_release=True)

        # 2) companion observers that __init__ installs (key/middle/wheel).
        #    Remove our previously-tracked IDs first so repeated calls do not
        #    stack duplicates (which would cause double pan / double zoom).
        for oid in list(getattr(self, "_aux_observer_ids", []) or []):
            try:
                self.interactor.RemoveObserver(oid)
            except Exception:
                pass
        self._aux_observer_ids = [
            self.interactor.AddObserver("KeyPressEvent", self._on_key_press),
            self.interactor.AddObserver("MiddleButtonPressEvent", self._on_middle_press, 20.0),
            self.interactor.AddObserver("MiddleButtonReleaseEvent", self._on_middle_release, 20.0),
            self.interactor.AddObserver("MouseWheelForwardEvent", self._on_zoom, 1.0),
            self.interactor.AddObserver("MouseWheelBackwardEvent", self._on_zoom, 1.0),
        ]

        # 3) shared, non-digitizer observers (grid-label right-click + hover)
        self._restore_shared_interactor_observers()

        self._force_render()

    def _ensure_plan_view_interaction(self, reason="digitizer"):
        """Keep the main viewer in 2D plan interaction while digitizing."""
        if getattr(self.app, 'is_3d_mode', False):
            return

        try:
            if hasattr(self.app, 'ensure_main_view_2d_interaction'):
                self.app.ensure_main_view_2d_interaction(
                    preserve_camera=True,
                    reason=reason,
                )
                return
        except Exception as e:
            print(f"⚠️ Digitizer failed to request 2D interaction: {e}")

        try:
            from vtkmodules.vtkInteractionStyle import vtkInteractorStyleImage

            style = self.interactor.GetInteractorStyle()
            style_name = style.GetClassName() if style is not None else "None"
            if style_name != "vtkInteractorStyleImage":
                style_2d = vtkInteractorStyleImage()
                try:
                    style_2d.SetInteractionModeToImageSlicing()
                except Exception:
                    pass
                self.interactor.SetInteractorStyle(style_2d)

            camera = self.renderer.GetActiveCamera()
            if camera is not None:
                camera.ParallelProjectionOn()
                self.renderer.ResetCameraClippingRange()

            self.app.vtk_widget.render()
            print(f"🔒 Digitizer forced 2D plan interaction ({reason})")

        except Exception as e:
            print(f"⚠️ Digitizer could not enforce 2D interaction: {e}")

    def _consume_vtk_event(self, obj):
        """Stop the default VTK interactor style from also processing our draw events."""
        if obj is None:
            return

        try:
            if hasattr(obj, 'AbortFlagOn'):
                obj.AbortFlagOn()
            elif hasattr(obj, 'SetAbortFlag'):
                try:
                    obj.SetAbortFlag(1)
                except TypeError:
                    obj.SetAbortFlag(True)
        except Exception:
            pass

    def _clear_suspended_preview_actor(self):
        actor = getattr(self, '_suspended_preview_actor', None)
        if actor is None:
            return

        try:
            self.overlay_renderer.RemoveActor(actor)
        except Exception:
            try:
                self.renderer.RemoveActor(actor)
            except Exception:
                pass

        self._suspended_preview_actor = None

    def _clear_live_preview_actors(self):
        """Remove transient preview actors so suspended drawings do not freeze on-screen."""
        for attr in (
            '_preview_line_actor',
            '_continuous_line_actor',
            '_rectangle_preview_actor',
            '_preview_actor',
            '_circle_preview_actor_2d',
            '_circle_preview_actor',
            '_freehand_preview_actor_2d',
        ):
            self._remove_preview_actor_2d(attr)

    def _show_suspended_preview(self, tool_name, temp_points):
        """Show a world-space placeholder while a draw tool is suspended."""
        self._clear_suspended_preview_actor()
        self._clear_live_preview_actors()

        if not tool_name or not temp_points:
            self.app.vtk_widget.render()
            return

        world_points = None
        if tool_name in ("smartline", "line", "freehand") and len(temp_points) >= 2:
            world_points = list(temp_points)
        elif tool_name == "polyline" and len(temp_points) >= 2:
            world_points = list(temp_points)
            world_points.append(world_points[0])
        elif tool_name == "hatcharea" and len(temp_points) >= 2:
            world_points = list(temp_points)

        if not world_points or len(world_points) < 2:
            self.app.vtk_widget.render()
            return

        style = self._get_draw_style(tool_name)
        self._update_continuous_line_world(
            '_suspended_preview_actor',
            world_points,
            color=style.get('color', (1, 0, 0)),
            width=style.get('width', 3),
        )
        self.app.vtk_widget.render()

    def _get_display_coords_from_world(self, world_points):
        """
        TRASH REMOVAL: Converts 3D world points to 2D display points for overlay rendering.
        """
        if not world_points:
            return None

        renderer = self.renderer
        coord = vtk.vtkCoordinate()
        coord.SetCoordinateSystemToWorld()

        display_points = vtk.vtkPoints()
        
        for p in world_points:
            coord.SetValue(p[0], p[1], p[2])
            d = coord.GetComputedDisplayValue(renderer)
            display_points.InsertNextPoint(d[0], d[1], 0.0)

        return display_points
    
    def _update_circle_preview_2d(self, center, radius, segments=None):
        """
        Generates a smooth 2D overlay for the circle preview. 
        NO Z-FIGHTING allowed.
        """
        # 1. Clean up old 3D preview if it exists (legacy trash)
        if hasattr(self, "_circle_preview_actor") and self._circle_preview_actor:
            self.renderer.RemoveActor(self._circle_preview_actor)
            self._circle_preview_actor = None

        # 2. Generate World Points (Resolution matters)
        n = segments or 128
        thetas = np.linspace(0, 2 * np.pi, n, endpoint=True)
        # Assume Z is constant at center for the preview ring
        world_coords = [
            (center[0] + radius * np.cos(t), center[1] + radius * np.sin(t), center[2])
            for t in thetas
        ]

        # 3. Convert to Display Space
        display_pts = self._get_display_coords_from_world(world_coords)

        # 4. Create 2D PolyData
        lines = vtk.vtkCellArray()
        lines.InsertNextCell(n)
        for i in range(n):
            lines.InsertCellPoint(i)

        poly = vtk.vtkPolyData()
        poly.SetPoints(display_pts)
        poly.SetLines(lines)

        # 5. Mapper & Actor 2D
        mapper = vtk.vtkPolyDataMapper2D()
        mapper.SetInputData(poly)
        
        # Coordinate system must be Display
        c = vtk.vtkCoordinate()
        c.SetCoordinateSystemToDisplay()
        mapper.SetTransformCoordinate(c)

        if not hasattr(self, "_circle_preview_actor_2d") or self._circle_preview_actor_2d is None:
            self._circle_preview_actor_2d = vtk.vtkActor2D()
            self.renderer.AddActor2D(self._circle_preview_actor_2d)

        self._circle_preview_actor_2d.SetMapper(mapper)
        
        # Style: Use configured tool settings
        _ci = self._get_draw_style('circle')
        prop = self._circle_preview_actor_2d.GetProperty()
        prop.SetColor(*_ci['color'])
        prop.SetLineWidth(_ci['width'])
        prop.SetOpacity(1.0)

    ####newww
    def _world_to_display_pt(self, world_point):
        """Convert one world point to screen pixel coords."""
        self.renderer.SetWorldPoint(
            float(world_point[0]), float(world_point[1]), float(world_point[2]), 1.0
        )
        self.renderer.WorldToDisplay()
        d = self.renderer.GetDisplayPoint()
        return (d[0], d[1])

    def _make_preview_actor_screen(self, screen_points, color=(0, 1, 0), width=3):
        """
        Pure screen-space 2D line. Points are already in display pixels.
        No world transform — no Z picking — no offset ever.
        """
        if not screen_points or len(screen_points) < 2:
            return None

        pts = vtk.vtkPoints()
        for p in screen_points:
            pts.InsertNextPoint(float(p[0]), float(p[1]), 0.0)

        n = pts.GetNumberOfPoints()
        cell = vtk.vtkCellArray()
        cell.InsertNextCell(n)
        for i in range(n):
            cell.InsertCellPoint(i)

        poly = vtk.vtkPolyData()
        poly.SetPoints(pts)
        poly.SetLines(cell)

        mapper = vtk.vtkPolyDataMapper2D()
        mapper.SetInputData(poly)
        # ✅ No SetTransformCoordinate — points are raw pixels, no conversion needed

        actor = vtk.vtkActor2D()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(float(color[0]), float(color[1]), float(color[2]))
        prop.SetLineWidth(float(width))
        prop.SetOpacity(1.0)
        prop.SetDisplayLocationToForeground()
        return actor  ###

        
    def _update_freehand_preview_2d(self, points):
        """
        Updates freehand trace using 2D Actor.
        """
        # 1. Clean up old 3D preview
        if hasattr(self, "_preview_actor") and self._preview_actor:
            self.renderer.RemoveActor(self._preview_actor)
            self._preview_actor = None
            
        if len(points) < 2:
            return

        # 2. Convert to Display Space
        display_pts = self._get_display_coords_from_world(points)

        # 3. Create Lines
        n = display_pts.GetNumberOfPoints()
        lines = vtk.vtkCellArray()
        lines.InsertNextCell(n)
        for i in range(n):
            lines.InsertCellPoint(i)

        poly = vtk.vtkPolyData()
        poly.SetPoints(display_pts)
        poly.SetLines(lines)

        # 4. Mapper 2D
        mapper = vtk.vtkPolyDataMapper2D()
        mapper.SetInputData(poly)
        
        c = vtk.vtkCoordinate()
        c.SetCoordinateSystemToDisplay()
        mapper.SetTransformCoordinate(c)

        # 5. Actor 2D
        if not hasattr(self, "_freehand_preview_actor_2d") or self._freehand_preview_actor_2d is None:
            self._freehand_preview_actor_2d = vtk.vtkActor2D()
            self.renderer.AddActor2D(self._freehand_preview_actor_2d)
            
        self._freehand_preview_actor_2d.SetMapper(mapper)
        
        fh_style = self._get_draw_style('freehand')
        prop = self._freehand_preview_actor_2d.GetProperty()
        prop.SetColor(*fh_style['color'])
        prop.SetLineWidth(float(fh_style.get('width', 2)))
        
    # ---------------- PUBLIC ----------------
    def enable(self, state: bool = True):
        self.enabled = state
        print("✏️ Digitizer ENABLED" if state else "🚫 Digitizer DISABLED")    
        
    def _is_draw_tool_text_drawing(self, drawing):
        """
        Return True only for text created/loaded through Draw tools.

        SNT/DXF attachment labels are not stored as Draw Tool text drawings.
        """

        return (
            isinstance(drawing, dict)
            and drawing.get("type") == "text"
            and drawing.get("actor") is not None
        )

    def _is_hidden_draw_tool_text_drawing(self, drawing):
        """
        Return True when a Draw Tool text drawing is intentionally hidden.

        Self-contained on purpose:
        Do NOT call _is_draw_tool_text_drawing() here. This method runs inside
        right-click picking, so it must never crash if a helper is missed during
        manual patching.
        """

        return (
            isinstance(drawing, dict)
            and drawing.get("type") == "text"
            and drawing.get("_draw_text_visible") is False
        )

    def _is_draw_tool_text_drawing(self, drawing):
        """
        Return True only for text created/loaded through Draw tools.

        SNT attachment labels are not stored in self.drawings, so this will not
        affect loaded SNT labels or SNT Hide Labels logic.
        """

        return isinstance(drawing, dict) and drawing.get("type") == "text" and drawing.get("actor") is not None

    def set_draw_text_visibility(self, visible: bool):
        """
        Show/hide only Draw-tool text actors.

        This does not remove actors and does not touch SNT attachment labels.
        Hidden Draw Text remains in self.drawings, but it must not stay selected
        or keep a context menu alive.
        """

        visible = bool(visible)
        self.draw_text_visible = visible

        changed_count = 0

        if not visible:
            try:
                if self._is_draw_tool_text_drawing(getattr(self, "selected_drawing", None)):
                    self.selected_drawing = None
                if self._is_draw_tool_text_drawing(getattr(self, "selected", None)):
                    self.selected = None
                self.multi_selected = [
                    d for d in list(getattr(self, "multi_selected", []) or [])
                    if not self._is_draw_tool_text_drawing(d)
                ]
            except Exception:
                pass

            try:
                menu = getattr(self, "_text_context_menu", None)
                if menu is not None:
                    menu.close()
                    menu.deleteLater()
                self._text_context_menu = None
                self._pending_text_menu_drawing = None
            except Exception:
                pass

        for drawing in list(getattr(self, "drawings", []) or []):
            if not self._is_draw_tool_text_drawing(drawing):
                continue

            actor = drawing.get("actor")
            if actor is None:
                continue

            try:
                actor.SetVisibility(1 if visible else 0)
                drawing["_draw_text_visible"] = visible
                changed_count += 1
            except Exception:
                try:
                    if visible:
                        actor.VisibilityOn()
                    else:
                        actor.VisibilityOff()
                    drawing["_draw_text_visible"] = visible
                    changed_count += 1
                except Exception:
                    pass

        try:
            if hasattr(self, "text_overlay_renderer") and self.text_overlay_renderer:
                self.text_overlay_renderer.Modified()
            if hasattr(self, "renderer") and self.renderer:
                self.renderer.Modified()
            if hasattr(self, "app") and self.app and hasattr(self.app, "vtk_widget"):
                self.app.vtk_widget.render()
        except Exception:
            try:
                self._force_render()
            except Exception:
                pass

        status = "shown" if visible else "hidden"
        message = f"Draw text {status}: {changed_count} item(s)"

        try:
            self.app.statusBar().showMessage(message, 2500)
        except Exception:
            print(message)

        print(message)
        return changed_count

    def toggle_draw_text_visibility(self):
        return self.set_draw_text_visibility(
            not bool(getattr(self, "draw_text_visible", True))
        )

    def _has_hidden_texts(self):
        """Return True if any Draw-tool text drawings are currently hidden."""
        if getattr(self, "draw_text_visible", True):
            return False
        for d in list(getattr(self, "drawings", []) or []):
            if self._is_hidden_draw_tool_text_drawing(d):
                return True
        return False

    def show_draw_text_visibility_dialog(self, parent=None):

        """
        Shift + Draw Text popup.
        Controls only text created/loaded through Draw tools.
        """

        dialog_parent = parent or getattr(self, "app", None)

        dlg = QDialog(dialog_parent)
        dlg.setWindowTitle("Draw Text Visibility")
        dlg.setModal(True)
        dlg.setMinimumWidth(340)

        try:
            dlg.setStyleSheet(get_dialog_stylesheet())
        except Exception:
            pass

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        title = QLabel("Draw Tool Text")
        title.setStyleSheet("font-weight: bold; font-size: 13px;")
        layout.addWidget(title)

        info = QLabel(
            "Show or hide only text created/loaded from the Draw tool.\n"
            "Loaded SNT labels are not affected."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        show_checkbox = QCheckBox("Show Draw Text")
        show_checkbox.setChecked(bool(getattr(self, "draw_text_visible", True)))
        layout.addWidget(show_checkbox)

        button_row = QHBoxLayout()

        show_btn = QPushButton("Show")
        hide_btn = QPushButton("Hide")
        close_btn = QPushButton("Close")

        button_row.addWidget(show_btn)
        button_row.addWidget(hide_btn)
        button_row.addStretch()
        button_row.addWidget(close_btn)

        layout.addLayout(button_row)

        def apply_visible(value):
            show_checkbox.blockSignals(True)
            show_checkbox.setChecked(bool(value))
            show_checkbox.blockSignals(False)
            self.set_draw_text_visibility(bool(value))

        show_checkbox.toggled.connect(self.set_draw_text_visibility)
        show_btn.clicked.connect(lambda: apply_visible(True))
        hide_btn.clicked.connect(lambda: apply_visible(False))
        close_btn.clicked.connect(dlg.accept)

        try:
            dlg.exec()
        except AttributeError:
            dlg.exec_()

    def add_drawing_finalized_callback(self, callback):
        if callback is None:
            return
        if not hasattr(self, "_drawing_finalized_callbacks"):
            self._drawing_finalized_callbacks = []
        if callback not in self._drawing_finalized_callbacks:
            self._drawing_finalized_callbacks.append(callback)

    def remove_drawing_finalized_callback(self, callback):
        callbacks = getattr(self, "_drawing_finalized_callbacks", None) or []
        if callback in callbacks:
            callbacks.remove(callback)

    def _emit_drawing_finalized(self, drawing_entry):
        callbacks = list(getattr(self, "_drawing_finalized_callbacks", None) or [])
        for callback in callbacks:
            try:
                callback(drawing_entry)
            except Exception as e:
                print(f"⚠️ drawing finalized callback failed: {e}")
 
    def add_drawing_removed_callback(self, callback):
        if callback is None:
            return
        if not hasattr(self, "_drawing_removed_callbacks"):
            self._drawing_removed_callbacks = []
        if callback not in self._drawing_removed_callbacks:
            self._drawing_removed_callbacks.append(callback)
 
    def remove_drawing_removed_callback(self, callback):
        callbacks = getattr(self, "_drawing_removed_callbacks", None) or []
        if callback in callbacks:
            callbacks.remove(callback)
 
    def _emit_drawing_removed(self, drawing_entry):
        callbacks = list(getattr(self, "_drawing_removed_callbacks", None) or [])
        for callback in callbacks:
            try:
                callback(drawing_entry)
            except Exception as e:
                print(f"⚠️ drawing removed callback failed: {e}")

    def _auto_select_last_drawing(self):
        # """Automatically selects the newly created drawing."""
        # self.clear_coordinate_labels()
        # self._unhighlight_all_lines()
        # self.selected_drawing = self.drawings[-1]
        # self._highlight_line(self.selected_drawing)
        #     self.show_vertex_coordinates(self.selected_drawing["coords"])
        # self._force_render()
        pass

    def _on_right_press(self, obj, evt):
        """
        Handle right-click.
        Priority Order:
        1. FINALIZE Drawing
        2. SELECT Drawing
        """
        if getattr(self, "active_tool", None) == "rotatepivot":
            self._consume_vtk_event(obj)
            self._cancel_vertex_pivot_rotate()
            self._show_transform_status("Vertex Pivot Rotate stopped", 2000)
            return
        if not self.enabled: return

        # Delete Vertex:
        # Left click selects/highlights the vertex.
        # Right click confirms/finalizes deletion.
        if self.active_tool == "deletevertex":
            self._consume_vtk_event(obj)

            if getattr(self, "selected_vertex_idx", None) is not None:
                if self._delete_selected_vertex():
                    print("✅ Delete Vertex finalized by right-click")
                else:
                    print("ℹ️ Delete Vertex: selected vertex could not be deleted")
            else:
                print("ℹ️ Delete Vertex: no vertex selected")

            return

        # Move Vertex:
        # While Move Vertex is active, right-click must be completely idle.
        # It should not finalize, not print, not open SNT/Grid menu.
        # After left-click finalize exits Move Vertex, right-click works normally again.
        if self.active_tool == "movevertex":
            self._consume_vtk_event(obj)
            return
        # =====================================================================
        # 1. PRIORITY: FINALIZE ACTIVE DRAWING TOOLS
        # =====================================================================
        if self.active_tool:
            self._consume_vtk_event(obj)
            if self.active_tool == "rectangle" and len(self.temp_points) == 1:
                pos = self._get_mouse_world_no_snap()
                self.temp_points.append(pos)
                # REPLACE WITH:
                self._finalize_rectangle()
                if not getattr(self, 'rectangle_permanent_mode', True):
                    self.active_tool = None
                return
            
            if self.active_tool == "circle" and len(self.temp_points) == 1:
                pos = self._get_mouse_world_no_snap()
                self.temp_points.append(pos)
                # REPLACE WITH:
                self._finalize_circle()
                self._auto_select_last_drawing()
                if not getattr(self, 'circle_permanent_mode', True):
                    self.active_tool = None
                return 
            
            if self.active_tool == "polyline":
                if len(self.temp_points) >= 3:
                    self._finalize_polyline()
                    if getattr(self, 'polyline_permanent_mode', False):
                        self.temp_points = []
                        self.clear_coordinate_labels()
                        self._unhighlight_all_lines()
                        self.selected_drawing = None
                    else:
                        self.active_tool = None
                elif len(self.temp_points) >= 1:
                    # 1 or 2 points placed — not enough to finalize; cancel cleanly to remove dangling vertex markers
                    if hasattr(self, '_polyline_vertex_markers'):
                        for m in self._polyline_vertex_markers:
                            try:
                                self._remove_actor_from_overlay(m)
                            except Exception:
                                pass
                        self._polyline_vertex_markers = []
                    self._remove_preview_actor_2d('_preview_line_actor')
                    self._remove_preview_actor_2d('_continuous_line_actor')
                    self._hide_snap_marker_now()
                    self.temp_points = []
                    self._force_render()
                # 0 points: nothing was placed, nothing to clean up
                return
            # ✅ BULLETPROOF FREEHAND FINALIZATION
            if self.active_tool == "freehand":
                self.left_down = False
                self.is_drawing_freehand = False
                
                if hasattr(self, "temp_points") and len(self.temp_points) > 1:
                    # Auto-close loop if the start and end points aren't exactly the same
                    start_pt = np.array(self.temp_points[0])
                    end_pt = np.array(self.temp_points[-1])
                    
                    if np.linalg.norm(end_pt - start_pt) > 0.01:
                        self.temp_points.append(tuple(start_pt))
                        print("🔗 Freehand auto-closed on Right-Click")
                        
                    self._finalize_freehand()
                else:
                    print("❌ Freehand cancelled (not enough points)")
                    self._hide_snap_marker_now()
                    if hasattr(self, "_preview_actor") and self._preview_actor:
                        try: self.renderer.RemoveViewProp(self._preview_actor)
                        except Exception:
                            pass
                        self._preview_actor = None
                                
                self.temp_points = []
                if not getattr(self, 'freehand_permanent_mode', True):
                    self.active_tool = None
                self._force_render()
                return
                
            if self.active_tool in ("smartline", "line"):
                if len(self.temp_points) >= 2:
                    current = self.active_tool
                    func = self._finalize_smart_line if current == "smartline" else self._finalize_line
                    func()
                    flag = 'smartline_permanent_mode' if current == "smartline" else 'line_permanent_mode'
                    if not getattr(self, flag, True):
                        self.active_tool = None
                elif len(self.temp_points) == 1:
                    # Exactly 1 point placed then right-click — cancel cleanly to remove the dangling vertex marker
                    self._cancel_smart_line()
                # 0 points: nothing was placed, nothing to clean up — fall through
                return

            if self.active_tool == "hatcharea":
                if len(self.temp_points) >= 3:
                    self._finalize_hatcharea()
                else:
                    n_had = len(self.temp_points)
                    if hasattr(self, '_hatch_vertex_markers'):
                        for m in self._hatch_vertex_markers:
                            try: self._remove_actor_from_overlay(m)
                            except Exception: pass
                        self._hatch_vertex_markers = []
                    self._remove_preview_actor_2d('_preview_line_actor')
                    self._remove_preview_actor_2d('_continuous_line_actor')
                    self.temp_points = []
                    self._force_render()
                    print(f"🔴 Hatch right-click cancel: had {n_had} point(s), need ≥3 — boundary cleared. Keep left-clicking to add points, then right-click.")
                    if hasattr(self, '_hatch_dialog') and self._hatch_dialog:
                        try: self._hatch_dialog.set_status(
                            f"Right-click cancelled {n_had} point(s).\n"
                            "Left-click to add points (need ≥3),\nthen right-click to apply hatch."
                        )
                        except Exception: pass
                return

            self.temp_points = []
            
            if self.active_tool == "text" and getattr(self, "_placing_text", False):
                # Right-click during text placement cancels (does NOT place text).
                # Remove the preview actor before cleanup.
                temp = getattr(self, "_temp_text_actor", None)
                if temp is not None:
                    try:
                        self._remove_actor_from_overlay(temp)
                    except Exception:
                        pass
                    self._temp_text_actor = None
                self._cleanup_text_tool()
                # Restore primary observers so other tools keep working.
                self._rebind_primary_draw_observers(include_left_release=True)
                self._restore_shared_interactor_observers()
                if hasattr(self.app, 'set_cross_cursor_active'):
                    self.app.set_cross_cursor_active(False, "draw")
                self._force_render()
                print("❌ Text placement cancelled by right-click")
                return
            
            self._force_render()
            return 

        # =====================================================================
        # 2. SELECTION MODE (IDLE) 
        # =====================================================================
        x, y = self.interactor.GetEventPosition()
        picked_drawing = None
        
        # A. Hardware picker
        actor = self._pick_actor(x, y)
        if actor:
            for d in self.drawings:
                if self._is_hidden_draw_tool_text_drawing(d):
                    continue
                if d.get("actor") is actor:
                    picked_drawing = d
                    break

        
        # B. Math picker fallback
        if not picked_drawing:
            picked_drawing = self._get_drawing_under_cursor(x, y, tolerance=25.0)
        
        if picked_drawing:
            if picked_drawing["type"] == "text":
                if self._is_hidden_draw_tool_text_drawing(picked_drawing):
                    self._consume_vtk_event(obj)
                    return

                self.clear_coordinate_labels()
                self._unhighlight_all_lines()
                self.multi_selected = []
                self.selected_drawing = picked_drawing

                self._show_text_context_menu(picked_drawing)

                self._force_render()
                return
            
            # Shape selection
            shift_held = self.interactor.GetShiftKey()
            if getattr(self, "selected_drawing", None) is picked_drawing and not shift_held:
                # Toggle Off
                self._cleanup_polygon_overlays()
                self._unhighlight_line(picked_drawing)
                self.clear_coordinate_labels()
                self.selected_drawing = None
            elif shift_held:
                # Multi-Select
                if not hasattr(self, "multi_selected"): self.multi_selected = []
                
                # ✅ FIX: Use identity check to avoid numpy array comparison
                if not any(d is picked_drawing for d in self.multi_selected):
                    self.multi_selected.append(picked_drawing)
                    self._highlight_line(picked_drawing)
                else:
                    # Remove using identity check
                    self.multi_selected = [d for d in self.multi_selected if d is not picked_drawing]
                    self._unhighlight_line(picked_drawing)
                self.selected_drawing = None
                self._expand_legacy_selection_to_polygons()
            else:
                # Single Select
                self.clear_coordinate_labels()
                self._unhighlight_all_lines()
                self.multi_selected = []
                self.selected_drawing = picked_drawing
                self._highlight_line(picked_drawing)
                self._expand_legacy_selection_to_polygons()
                
                if "coords" in picked_drawing:
                    self.show_vertex_coordinates(picked_drawing["coords"])
            
            self._force_render()
            return  

        # C. Deselect All
        print("⚪ Clicked empty space - clearing selection")
        self.clear_coordinate_labels()
        self.selected_drawing = None
        self._unhighlight_all_lines()
        self._force_render()
        
    def _force_render(self):
        """Force an immediate main-view refresh for digitize actions."""
        try:
            if hasattr(self.app, "_force_render_main_view"):
                self.app._force_render_main_view()
                return True
        except Exception:
            pass

        try:
            if hasattr(self.app, "vtk_widget") and self.app.vtk_widget is not None:
                self.app.vtk_widget.render()
                return True
        except Exception:
            pass

        try:
            if self.renderer is not None:
                self.renderer.Modified()
            if self.overlay_renderer is not None:
                self.overlay_renderer.Modified()
            if hasattr(self.app, "vtk_widget") and self.app.vtk_widget is not None:
                self.app.vtk_widget.render()
                return True
        except Exception:
            pass

        return False
    def activate_accudraw_tool(self):
        """Activate separate AccuDraw XYZ/Angle drawing tool."""
        try:
            if getattr(self, "accudraw_tool", None) is None:
                from gui.accudraw_tool import AccuDrawTool
                self.accudraw_tool = AccuDrawTool(self)
            return self.accudraw_tool.activate()
        except Exception as e:
            print(f"⚠️ AccuDraw activation failed: {e}")
            return False

    def deactivate_accudraw_tool(self, cancel=False):
        """Deactivate AccuDraw if active."""
        try:
            tool = getattr(self, "accudraw_tool", None)
            if tool is not None and getattr(tool, "active", False):
                tool.deactivate(cancel=cancel)
                return True
        except Exception as e:
            print(f"⚠️ AccuDraw deactivate failed: {e}")
        return False

    def set_tool(self, tool, suspend_only=False):
        """Activate drawing tool and handle tool-specific setup.

        suspend_only=True  →  only pause input handling, do NOT cancel/clear
                            any in-progress drawing (preview actors + temp_points
                            are preserved so the user can resume later).
        """
        if tool is not None:
            self._check_and_update_renderers()
        # Deactivate any active plugins if we are switching to a non-null digitizer tool
        if tool is not None and hasattr(self.app, "plugin_manager"):
            for name, info in self.app.plugin_manager.loaded_plugins.items():
                instance = info.get("instance")
                if instance and hasattr(instance, "deactivate") and getattr(instance, "active", False):
                    try:
                        instance.deactivate()
                    except Exception as e:
                        print(f"Error deactivating plugin {name}: {e}")

        # Stop any stuck/manual pan before switching draw tools.
        if getattr(self, "_is_panning", False) or getattr(self, "middle_down", False):
            self._reset_pan_state()

        # ============================================================
        # ✅ FIX: Clean up text tool BEFORE active_tool is reassigned.
        # Text tool sets _placing_text, installs its own observers,
        # and leaves temp_points dirty. Must clean NOW while
        # active_tool still == 'text', before it gets overwritten.
        # ============================================================
        if getattr(self, 'active_tool', None) == 'text' or getattr(self, '_placing_text', False):
            self._cleanup_text_tool()

        # Clear the shared canvas tool cursor if no draw tool is selected
        if tool is None and hasattr(self.app, 'set_cross_cursor_active'):
            self.app.set_cross_cursor_active(False, "draw")

        # Remove only OUR observers (not grid_label_system's etc.)
        for oid in list(self._draw_observer_ids):
            try:
                self.interactor.RemoveObserver(oid)
            except Exception:
                pass
        self._draw_observer_ids = []
        # Re-add our right-press observer
        self._draw_observer_ids.append(
            self.interactor.AddObserver("RightButtonPressEvent", self._on_right_press, 1.0)
        )

        # ✅ Deactivate measurement tool FIRST (only if it was actually active)
        if hasattr(self.app, 'measurement_tool') and self.app.measurement_tool:
            if getattr(self.app.measurement_tool, 'active', False):
                self.app.measurement_tool.deactivate()
                print("📏 Measurement tool deactivated before drawing")

        # When suspend_only=True we are just parking the tool temporarily
        if not suspend_only:
            # Cancel any unfinished SmartLine before switching
            if getattr(self, "active_tool", None) == "smartline" and self.temp_points:
                self._cancel_smart_line()

            # Cancel any unfinished Line before switching
            if getattr(self, "active_tool", None) == "line" and self.temp_points:
                if hasattr(self, '_line_vertex_markers'):
                    for m in self._line_vertex_markers:
                        try: self._remove_actor_from_overlay(m)
                        except Exception:
                            pass
                    self._line_vertex_markers = []
                self._remove_preview_actor_2d('_preview_line_actor')
                self._remove_preview_actor_2d('_continuous_line_actor')
                self.temp_points = []
                print("🧹 Cancelled unfinished Line on tool switch")

            # Cancel any unfinished Polyline before switching
            if getattr(self, "active_tool", None) == "polyline" and self.temp_points:
                if hasattr(self, '_polyline_vertex_markers'):
                    for m in self._polyline_vertex_markers:
                        try: self._remove_actor_from_overlay(m)
                        except Exception:
                            pass
                    self._polyline_vertex_markers = []
                self._remove_preview_actor_2d('_preview_line_actor')
                self._remove_preview_actor_2d('_continuous_line_actor')
                self.temp_points = []
                print("🧹 Cancelled unfinished Polyline on tool switch")

            # Cancel any unfinished HatchArea before switching (also cleans cursor for permanent mode)
            if getattr(self, "active_tool", None) == "hatcharea":
                if self.temp_points:
                    if hasattr(self, '_hatch_vertex_markers'):
                        for m in self._hatch_vertex_markers:
                            try: self._remove_actor_from_overlay(m)
                            except Exception: pass
                        self._hatch_vertex_markers = []
                    self._remove_preview_actor_2d('_preview_line_actor')
                    self._remove_preview_actor_2d('_continuous_line_actor')
                    self.temp_points = []
                    print("🧹 Cancelled unfinished HatchArea on tool switch")
                # Always remove "hatcharea" from the cursor active-tools set when leaving
                if hasattr(self.app, 'set_cross_cursor_active'):
                    self.app.set_cross_cursor_active(False, "hatcharea")

            # Cancel any active OrthogonalPolygon before switching
            if getattr(self, "active_tool", None) == "orthopolygon":
                old_inst = getattr(self, '_ortho_polygon_tool', None)
                if old_inst:
                    old_inst._cancel()
                    old_inst.deactivate()
                    self._ortho_polygon_tool = None
                print("🧹 Cancelled OrthogonalPolygon on tool switch")

            # Cancel any unfinished Freehand before switching
            if getattr(self, "active_tool", None) == "freehand":
                if self.temp_points or getattr(self, "is_drawing_freehand", False):
                    self._remove_preview_actor_2d('_freehand_preview_actor_2d')
                    self._remove_preview_actor_2d('_preview_actor')
                    self.is_drawing_freehand = False
                    self.left_down = False
                    self.temp_points = []
                    print("🧹 Cancelled unfinished Freehand on tool switch")

            # Cancel any unfinished Circle before switching
            if getattr(self, "active_tool", None) == "circle" and self.temp_points:
                self._remove_preview_actor_2d('_circle_preview_actor_2d')
                self._remove_preview_actor_2d('_circle_preview_actor')
                self.temp_points = []
                print("🧹 Cancelled unfinished Circle on tool switch")

            # Cancel any unfinished Rectangle before switching
            if getattr(self, "active_tool", None) == "rectangle" and self.temp_points:
                self._remove_preview_actor_2d('_rectangle_preview_actor')
                self.temp_points = []
                print("🧹 Cancelled unfinished Rectangle on tool switch")

            # Cancel any unfinished Polygon before switching
            if getattr(self, "active_tool", None) == "polygon" and self.temp_points:
                if hasattr(self, '_polyline_vertex_markers'):
                    for m in self._polyline_vertex_markers:
                        try:
                            self._remove_actor_from_overlay(m)
                        except Exception:
                            pass
                    self._polyline_vertex_markers = []
                self._remove_preview_actor_2d('_preview_line_actor')
                self._remove_preview_actor_2d('_continuous_line_actor')
                self.temp_points = []
                print("🧹 Cancelled unfinished Polygon on tool switch")

        # Normalize tool name
        if tool:
            tool = tool.lower().replace(" ", "").replace("_", "")

        if tool == "select":
            active_tools = getattr(self.app, "_active_tools", set())
            classify_tool = getattr(self.app, "active_classify_tool", None)
            if classify_tool and classify_tool not in ("cross_section", "cut_section"):
                msg = f"Selection is disabled while {classify_tool} tool is active"
                print(f"🛑 {msg}")
                try:
                    self.app.statusBar().showMessage(msg, 2500)
                except Exception:
                    pass
                try:
                    self.app.set_cross_cursor_active(False, "draw")
                except Exception:
                    pass
                return
            if getattr(self.app, "cross_section_active", False) or "cross_section" in active_tools:
                try:
                    print("ðŸ›‘ Draw select requested - deactivating cross-section tool")
                    self.app.deactivate_cross_section_tool()
                except Exception:
                    try:
                        self.app.set_cross_cursor_active(False, "cross_section")
                    except Exception:
                        pass
            if getattr(self.app, "cut_section_mode_on", False) or "cut_section" in active_tools:
                try:
                    print("ðŸ›‘ Draw select requested - deactivating cut-section tool")
                    self.app._deactivate_pending_cut_section_tool("switching to select")
                except Exception:
                    try:
                        self.app.set_cross_cursor_active(False, "cut_section")
                    except Exception:
                        pass

        if tool and self._element_select_tool is not None:
            try:
                if self._element_select_tool.is_active():
                    self._element_select_tool.deactivate()
            except Exception as e:
                print(f"Element Select deactivate before drawing failed: {e}")

        # ✅ Capture requested tool BEFORE self.active_tool is changed
        # This is used later for observer attachment logic fix
        _requested_tool = tool

        # Guard: skip reset if same tool is re-activated while actively mid-draw.
        # This prevents phantom VTK events (fired during render inside this call)
        # from clearing temp_points and corrupting in-progress geometry.
        if (not suspend_only and tool is not None):
            _norm = tool.lower().replace(" ", "").replace("_", "")
            if (_norm == getattr(self, "active_tool", None)
                    and (self.temp_points or getattr(self, "is_drawing_freehand", False))):
                print(f"⚠️ set_tool({tool}) called mid-draw on same tool — skipping reset to preserve state")
                return

        # Draw tool selection must immediately shut down classification session
        if tool and getattr(self.app, 'active_classify_tool', None):
            try:
                print(f"🛑 Draw tool '{tool}' selected — deactivating classification tool")
                self.app.deactivate_classification_tool(preserve_cross_section=True)
            except Exception as e:
                print(f"⚠️ Failed to deactivate classification before drawing: {e}")

        if tool:
            if getattr(self.app, 'cross_section_active', False):
                try:
                    print(f"🛑 Draw tool '{tool}' selected — deactivating cross-section tool")
                    self.app.deactivate_cross_section_tool()
                except Exception as e:
                    print(f"⚠️ Failed to deactivate cross-section before drawing: {e}")
            if getattr(self.app, 'cut_section_mode_on', False):
                try:
                    print(f"🛑 Draw tool '{tool}' selected — deactivating cut-section tool")
                    self.app.cut_section_controller.cancel_cut_section()
                    self.app.cut_section_mode_on = False
                except Exception as e:
                    print(f"⚠️ Failed to deactivate cut-section before drawing: {e}")

        if tool:
            self._ensure_plan_view_interaction(f"draw tool: {tool}")

        # --- Handle suspend / resume logic ---
        _resumed = False

        if suspend_only and tool is None and getattr(self, 'active_tool', None):
            # SUSPENDING: save current drawing state
            markers = []
            marker_attr = None
            if getattr(self, 'active_tool', None) == 'smartline':
                marker_attr = '_smartline_vertex_markers'
            elif getattr(self, 'active_tool', None) == 'line':
                marker_attr = '_line_vertex_markers'
            elif getattr(self, 'active_tool', None) == 'polyline':
                marker_attr = '_polyline_vertex_markers'
            elif getattr(self, 'active_tool', None) == 'hatcharea':
                marker_attr = '_hatch_vertex_markers'

            if marker_attr and hasattr(self, marker_attr):
                markers = list(getattr(self, marker_attr) or [])
            self._suspended_state = {
                "tool": self.active_tool,
                "temp_points": list(self.temp_points) if self.temp_points else [],
                "markers": markers,
                "marker_attr": marker_attr,
            }
            self._show_suspended_preview(self.active_tool, self.temp_points)
            print(f"💾 Suspended drawing state: tool={self.active_tool}, points={len(self.temp_points)}")

        elif not suspend_only and self._suspended_state:
            suspended_tool = self._suspended_state["tool"]
            normalized = tool.lower().replace(" ", "").replace("_", "") if tool else ""
            if normalized == suspended_tool:
                # RESUMING same tool → restore temp_points
                self.temp_points = self._suspended_state["temp_points"]
                marker_attr = self._suspended_state.get("marker_attr")
                if marker_attr and hasattr(self, marker_attr):
                    setattr(self, marker_attr, list(self._suspended_state.get("markers", [])))
                _resumed = True
                self._clear_suspended_preview_actor()
                print(f"♻️ Resumed drawing: tool={tool}, restored {len(self.temp_points)} points")
            else:
                # Different tool → cancel the old suspended drawing
                old_pts = self._suspended_state.get("temp_points", [])
                old_markers = self._suspended_state.get("markers", [])
                for m in old_markers:
                    try: self._remove_actor_from_overlay(m)
                    except Exception:
                        pass
                self._clear_suspended_preview_actor()
                if old_pts:
                    self._clear_live_preview_actors()
                    print(f"🧹 Cleared old suspended drawing ({suspended_tool}, {len(old_pts)} pts)")
            self._suspended_state = None

        elif not suspend_only:
            self._clear_suspended_preview_actor()

        self.active_tool = tool
        self._shift_blocked = False  # Clear shift block when switching tools
        if self.active_tool != "deletevertex":
            self.selected_vertex_idx = None
            self._clear_vertex_delete_highlight()
            self.clear_coordinate_labels()
            if hasattr(self, 'vertex_hover_marker') and self.vertex_hover_marker:
                self._remove_actor_from_overlay(self.vertex_hover_marker)
                self.vertex_hover_marker = None

        # Clear temp_points only on normal activation (not suspend, not resume)
        if not suspend_only and not _resumed:
            self.temp_points = []
            self._clear_temp_vertex_history()

        # ✅ NEW: Handle AccuDraw tool
        if self.active_tool == "accudraw":
            print("📐 AccuDraw mode activated")
            self.activate_accudraw_tool()
            return

        # ✅ NEW: Handle Move Vertex tool
        if self.active_tool == "movevertex":
            print("🔄 Move Vertex mode activated")
            self._activate_move_vertex_mode()
            return

        # Show all vertices for Delete Vertex mode
        if self.active_tool == "deletevertex":
            self.moving_vertex_data = None
            self.dragging_vertex = None
            if self.vertex_drag_marker:
                self._remove_actor_from_overlay(self.vertex_drag_marker)
                self.vertex_drag_marker = None
            self._show_all_vertices_for_delete_mode()

        # Special handling for vertex insertion tool
        if self.active_tool == "vertex":
            print("🔵 Vertex insertion mode activated - click on any line to add a vertex")
        else:
            print(f"🖊️ Tool activated: {self.active_tool or 'select'}")

        # ── Hatch Area tool ────────────────────────────────────────────────────
        if tool == 'hatcharea':
            from gui.hatch_area_tool import HatchAreaDialog
            if not hasattr(self, '_hatch_dialog') or self._hatch_dialog is None:
                self._hatch_dialog = HatchAreaDialog(parent=self.app)
                # Re-activate hatch tool when user clicks a method button while another tool is active
                self._hatch_dialog.method_changed.connect(self._on_hatch_method_clicked)
            if not hasattr(self, '_hatch_method_id'):
                self._hatch_method_id = 2  # "Select by Points" — matches dialog's default checked button
            try:
                self._hatch_dialog.show()
                self._hatch_dialog.raise_()
                self._hatch_dialog.set_status("Click inside a shape to hatch it,\nor click empty space to define boundary points\n(right-click when ≥3 points to apply).")
            except Exception as e:
                print(f"⚠️ HatchAreaDialog open error: {e}")
            if not _resumed:
                self._hatch_vertex_markers = []
            if hasattr(self.app, 'set_cross_cursor_active'):
                self.app.set_cross_cursor_active(True, "hatcharea")
            print("▦ Hatch Area tool activated — click boundary points, right-click to finish")
        # ──────────────────────────────────────────────────────────────────────

        # ── Ortho-Polygon tool ─────────────────────────────────────────────────
        if tool == 'orthopolygon':
            from gui.orthogonal_polygon_tool import OrthogonalPolygonTool
            old = getattr(self, '_ortho_polygon_tool', None)
            if old:
                old.deactivate()
            inst = OrthogonalPolygonTool(self)
            inst.activate()
            self._ortho_polygon_tool = inst
            return
        # ──────────────────────────────────────────────────────────────────────

        # ============================================================
        # ✅ FIX: Use _requested_tool (captured BEFORE self.active_tool
        # was set) instead of self.active_tool.
        #
        # ORIGINAL BUG: "if self.active_tool and self.active_tool not in
        # ("text", "movevertex")" — by this point self.active_tool is
        # already the NEW tool, so switching FROM text TO polyline would
        # check "polyline" not in ("text","movevertex") = True and attach
        # observers, but text cleanup had already been skipped because
        # active_tool was overwritten before the check ran.
        #
        # text cleanup already ran at the very top of this method.
        # ============================================================
        if _requested_tool and _requested_tool not in ("text", "movevertex"):
            # Remove only OUR old observers
            for oid in list(self._draw_observer_ids):
                try:
                    self.interactor.RemoveObserver(oid)
                except Exception:
                    pass
            self._draw_observer_ids = []

            # Add fresh observers and track their IDs
            self._draw_observer_ids.append(
                self.interactor.AddObserver(
                    "LeftButtonPressEvent", self._on_left_press, 1.0))
            self._draw_observer_ids.append(
                self.interactor.AddObserver(
                    "MouseMoveEvent", self._on_mouse_move, 1.0))
            self._draw_observer_ids.append(
                self.interactor.AddObserver(
                    "RightButtonPressEvent", self._on_right_press, 1.0))

            print(f"✅ Event observers attached for {_requested_tool}")

        # Handle polyline modes
        if self.active_tool == "polyline":
            if getattr(self, 'polyline_permanent_mode', False):
                try:
                    self.app.statusBar().showMessage(
                        "🔄 Permanent Polyline Mode - Draw multiple polylines (Shift+Esc to exit)"
                    )
                except Exception:
                    pass

        if tool and "polyline" in tool.lower():
            self.active_tool = "polyline"
            print("🔄 Permanent Polyline mode activated" if self.polyline_permanent_mode else "🖊️ Polyline activated")

        # ✅ Start text placement if text tool
        if self.active_tool == "text":
            self._start_text_label()

        # Activate the shared canvas tool cursor after interactor changes
        if self.active_tool and hasattr(self.app, 'set_cross_cursor_active'):
            self.app.set_cross_cursor_active(True, "draw")
            print("Canvas tool cursor activated for drawing")

        if _resumed and self.active_tool and self.temp_points:
            self._deferred_preview_update()

        self._restore_shared_interactor_observers()

    # ---------------- INTERNAL HELPERS ----------------
    def _pick_hit_background(self, x, y):
        """True if the display point (x, y) has no pickable geometry under it.

        vtkWorldPointPicker always returns *a* point by sampling the depth
        buffer -- on a miss (e.g. after the point cloud has been cleared and
        nothing else is under the cursor) that point lands on the far
        clipping plane, which can be far below ground level. Use a cheap
        vtkPropPicker as a hit/miss test so callers can reproject onto a
        sensible working plane instead of trusting that stray point.
        """
        try:
            prop_picker = vtk.vtkPropPicker()
            hit = prop_picker.Pick(x, y, 0, self.renderer)
            return not bool(hit)
        except Exception:
            return False

    def _hatch_flat_z(self):
        """Best flat Z for the Hatch tool when there's no point cloud to pick
        against. Reads the elevation straight off whatever grid/SNT/DXF
        actors are already in the scene (they carry the real offset baked
        into their position via `_snt_z_offset`) so the hatch lands at grid
        level instead of guessing from a generic bounds scan.
        """
        try:
            actors = self.renderer.GetActors()
            actors.InitTraversal()
            actor = actors.GetNextActor()
            while actor is not None:
                if actor.GetVisibility() and (getattr(actor, 'is_grid_label', False)
                                               or hasattr(actor, '_snt_z_offset')):
                    z = getattr(actor, '_snt_z_offset', None)
                    if z is not None:
                        return float(z)
                    pos = actor.GetPosition()
                    if pos and len(pos) > 2:
                        return float(pos[2])
                actor = actors.GetNextActor()
        except Exception:
            pass
        return self._get_fallback_z_height()

    def _project_to_z_plane(self, x, y, z_plane):
        """Cast the camera ray through display point (x, y) and intersect it
        with the horizontal plane Z = z_plane. Used when the world-point
        picker misses all geometry, so we still get a correct X/Y (not just
        the far-clip-plane X/Y) at a sensible elevation."""
        try:
            self.renderer.SetDisplayPoint(float(x), float(y), 0.0)
            self.renderer.DisplayToWorld()
            near = self.renderer.GetWorldPoint()

            self.renderer.SetDisplayPoint(float(x), float(y), 1.0)
            self.renderer.DisplayToWorld()
            far = self.renderer.GetWorldPoint()

            if (near is not None and far is not None
                    and len(near) >= 4 and len(far) >= 4
                    and abs(near[3]) > 1e-9 and abs(far[3]) > 1e-9):
                n = np.array([near[0]/near[3], near[1]/near[3], near[2]/near[3]])
                f = np.array([far[0]/far[3], far[1]/far[3], far[2]/far[3]])
                direction = f - n
                dz = direction[2]
                if abs(dz) > 1e-10:
                    t = (z_plane - n[2]) / dz
                    return n + t * direction
                return np.array([n[0], n[1], z_plane])
        except Exception:
            pass
        return np.array([0.0, 0.0, z_plane])

    def _get_mouse_world(self):
        x, y = self.interactor.GetEventPosition()
        self.picker.Pick(x, y, 0, self.renderer)
        pos = np.array(self.picker.GetPickPosition())

        if self.active_tool == "hatcharea" and self._pick_hit_background(x, y):
            pos = self._project_to_z_plane(x, y, self._hatch_flat_z())

        # Always snap for line and smartline tools regardless of snap_enabled flag
        if self.active_tool in ("line", "smartline"):
            pos = self._snap_point(pos)
        elif self.active_tool and self.snap_enabled:
            pos = self._snap_point(pos)

        # ✅ FIX: Plan View Z-Consistency (MicroStation style)
        # If we are in 2D Plan View and already have a starting point or are dragging a vertex,
        # force the Z-coordinate to be consistent. This prevents "jagged" figures in 3D.
        if not getattr(self.app, 'is_3d_mode', False):
            if self.temp_points:
                pos[2] = float(self.temp_points[0][2])
            elif getattr(self, 'dragging_vertex', None):
                drawing = self.dragging_vertex.get('drawing')
                if drawing and 'coords' in drawing and drawing['coords']:
                    pos[2] = float(drawing['coords'][0][2])
            else:
                pos[2] = self._get_fallback_z_height()

        return pos

    def _get_mouse_world_no_snap(self):
        """Get mouse position WITHOUT snapping - for smooth freehand/circle drawing."""
        x, y = self.interactor.GetEventPosition()
        self.picker.Pick(x, y, 0, self.renderer)
        pos = np.array(self.picker.GetPickPosition())

        if self.active_tool == "hatcharea" and self._pick_hit_background(x, y):
            pos = self._project_to_z_plane(x, y, self._hatch_flat_z())


        # ✅ FIX: Plan View Z-Consistency for unsnapped previews
        if not getattr(self.app, 'is_3d_mode', False):
            if self.temp_points:
                pos[2] = float(self.temp_points[0][2])
            elif getattr(self, 'dragging_vertex', None):
                drawing = self.dragging_vertex.get('drawing')
                if drawing and 'coords' in drawing and drawing['coords']:
                    pos[2] = float(drawing['coords'][0][2])
            else:
                pos[2] = self._get_fallback_z_height()

        return pos  # No snapping applied

    # CURRENT: (no such method — fixed world-space tolerance everywhere)

    # AFTER: (new method — converts screen pixels → world units at current zoom)
    def _screen_to_world_tolerance(self, screen_pixels=15):
        """
        Convert a fixed screen-pixel snap radius into world-space units
        at the current camera zoom level.  This makes snap feel identical
        regardless of how far in or out the user is zoomed — exactly like
        MicroStation's AccuSnap.
        """
        try:
            camera = self.renderer.GetActiveCamera()
            win_size = self.interactor.GetRenderWindow().GetSize()
            win_h = win_size[1] if win_size[1] > 0 else 1

            if camera.GetParallelProjection():
                # Orthographic: world_per_pixel is trivially derived from parallel scale
                world_per_px = (2.0 * camera.GetParallelScale()) / win_h
            else:
                # Perspective
                focal   = np.array(camera.GetFocalPoint())
                cam_pos = np.array(camera.GetPosition())
                dist    = np.linalg.norm(cam_pos - focal)
                fov_rad = np.radians(camera.GetViewAngle())
                world_per_px = (2.0 * dist * np.tan(fov_rad / 2.0)) / win_h

            return max(world_per_px * screen_pixels, 1e-6)
        except Exception:
            return 0.5          # safe fallback

    def _snap_point(self, pos, screen_tol_px=25):
        """
        Snap to the nearest existing vertex within `screen_tol_px` screen pixels.
        Nearby Snap mode also snaps to the nearest point on completed drawing
        segments, so a new vertex can land directly on an existing figure edge.
        The tolerance is converted to world units per frame so it stays constant
        at every zoom level — identical to MicroStation AccuSnap behaviour.
        """
        world_tol = self._screen_to_world_tolerance(screen_tol_px)

        snap_target = None
        best_world  = world_tol

        _intercept_mode = getattr(self, "snap_mode", None) == "intercept"
        if not _intercept_mode:
            for d in self.drawings:
                for c in d["coords"]:
                    # Use 2-D XY distance for plan-view snapping (ignore Z drift in point cloud)
                    dx = float(c[0]) - float(pos[0])
                    dy = float(c[1]) - float(pos[1])
                    dist = np.sqrt(dx * dx + dy * dy)
                    if dist < best_world:
                        best_world  = dist
                        snap_target = c

        if getattr(self, "snap_mode", None) == "nearby":
            for d in self.drawings:
                coords = d.get("coords") or []
                for i in range(len(coords) - 1):
                    p1 = np.array(coords[i], dtype=np.float64)
                    p2 = np.array(coords[i + 1], dtype=np.float64)
                    point_on_segment, _ = self._closest_point_on_segment(
                        np.array(pos, dtype=np.float64), p1, p2
                    )
                    screen_dist = self._world_to_screen_distance(point_on_segment, pos)
                    if screen_dist < screen_tol_px:
                        world_dist = np.linalg.norm(
                            np.array(point_on_segment[:2], dtype=np.float64)
                            - np.array(pos[:2], dtype=np.float64)
                        )
                        if world_dist < best_world:
                            best_world = world_dist
                            snap_target = point_on_segment

        if getattr(self, "snap_mode", None) == "center":
            best_center_dist = None
            best_center_target = None
            mx, my = self.interactor.GetEventPosition()
            for d in self.drawings:
                coords = d.get("coords") or []
                dtype  = d.get("type", "")
                if len(coords) < 3:
                    continue
                # A figure is closed if it is a polygon/rectangle/circle type,
                # or if the first and last vertices coincide (smartline/polyline closure).
                p0 = np.array(coords[0][:2], dtype=np.float64)
                pn = np.array(coords[-1][:2], dtype=np.float64)
                is_closed = (
                    dtype in ("polygon", "rectangle", "circle")
                    or np.linalg.norm(p0 - pn) < 1e-6
                )
                if not is_closed:
                    continue
                
                # Project world coordinates to screen coords
                screen_coords = [self._world_to_screen(pt) for pt in coords]
                
                is_near = self._point_in_polygon((mx, my), screen_coords)
                if not is_near:
                    # Check screen distance to segments of the boundary in screen space
                    for i in range(len(screen_coords) - 1):
                        p1 = np.array(screen_coords[i], dtype=np.float64)
                        p2 = np.array(screen_coords[i + 1], dtype=np.float64)
                        point_on_segment, _ = self._closest_point_on_segment(
                            np.array([mx, my], dtype=np.float64), p1, p2
                        )
                        dx = point_on_segment[0] - mx
                        dy = point_on_segment[1] - my
                        screen_dist = np.sqrt(dx * dx + dy * dy)
                        if screen_dist < screen_tol_px:
                            is_near = True
                            break
                if not is_near:
                    continue

                # Use proper polygon centroid or stored circle center
                if dtype == "circle" and d.get("center") is not None:
                    c_pt = d.get("center")
                    cx, cy = float(c_pt[0]), float(c_pt[1])
                    cz = float(c_pt[2]) if len(c_pt) > 2 else float(coords[0][2])
                else:
                    cx, cy = self._polygon_centroid_2d(coords)
                    cz = float(coords[0][2]) if len(coords[0]) > 2 else 0.0

                center_target = (cx, cy, cz)
                cx_screen, cy_screen = self._world_to_screen(center_target)
                dx = cx_screen - mx
                dy = cy_screen - my
                dist_to_center_screen = np.sqrt(dx * dx + dy * dy)
                if best_center_dist is None or dist_to_center_screen < best_center_dist:
                    best_center_dist = dist_to_center_screen
                    best_center_target = center_target
            if best_center_target is not None:
                snap_target = best_center_target

        if getattr(self, "snap_mode", None) == "keypoint":
            # For each segment of every drawing that is within screen_tol_px of the
            # cursor, snap to whichever of the segment's two endpoints is closer to
            # the cursor.  This lets the user click anywhere along a line and land
            # exactly on the nearest key point (vertex) of that figure.
            best_kp_dist = None
            kp_target    = None
            pos_2d = np.array(pos[:2], dtype=np.float64)
            for d in self.drawings:
                coords = d.get("coords") or []
                for i in range(len(coords) - 1):
                    p1 = np.array(coords[i],     dtype=np.float64)
                    p2 = np.array(coords[i + 1], dtype=np.float64)
                    point_on_seg, _ = self._closest_point_on_segment(
                        np.array(pos, dtype=np.float64), p1, p2
                    )
                    screen_dist = self._world_to_screen_distance(point_on_seg, pos)
                    if screen_dist < screen_tol_px:
                        # Cursor is near this segment — pick the closer endpoint
                        d1 = np.linalg.norm(p1[:2] - pos_2d)
                        d2 = np.linalg.norm(p2[:2] - pos_2d)
                        if d1 <= d2:
                            candidate, cdist = coords[i],     d1
                        else:
                            candidate, cdist = coords[i + 1], d2
                        if best_kp_dist is None or cdist < best_kp_dist:
                            best_kp_dist = cdist
                            kp_target    = candidate
            if kp_target is not None:
                snap_target = kp_target

        if getattr(self, "snap_mode", None) == "midpoint":
            # For each segment of every drawing that is within screen_tol_px of
            # the cursor, compute the exact midpoint of that segment and treat it
            # as a snap candidate.  Among all qualifying segments, the one whose
            # midpoint is closest to the cursor position wins.
            best_mp_dist = None
            mp_target    = None
            for d in self.drawings:
                coords = d.get("coords") or []
                for i in range(len(coords) - 1):
                    p1 = np.array(coords[i],     dtype=np.float64)
                    p2 = np.array(coords[i + 1], dtype=np.float64)
                    point_on_seg, _ = self._closest_point_on_segment(
                        np.array(pos, dtype=np.float64), p1, p2
                    )
                    screen_dist = self._world_to_screen_distance(point_on_seg, pos)
                    if screen_dist < screen_tol_px:
                        midpoint = (p1 + p2) * 0.5
                        mp_dist  = np.linalg.norm(midpoint[:2] - np.array(pos[:2], dtype=np.float64))
                        if best_mp_dist is None or mp_dist < best_mp_dist:
                            best_mp_dist = mp_dist
                            mp_target    = midpoint
            if mp_target is not None:
                snap_target = tuple(mp_target)

        if getattr(self, "snap_mode", None) == "intercept":
            # Collect all segments from all drawings, then test every pair for
            # intersection.  Only true interior crossings qualify — endpoints/
            # vertices are excluded.  Among all intersection candidates, the one
            # whose screen distance to the cursor is within screen_tol_px and
            # whose world distance to the cursor is smallest wins.
            best_ix_dist = None
            ix_target    = None
            pos_2d       = np.array(pos[:2], dtype=np.float64)

            # Build flat segment list: (p1_3d, p2_3d)
            all_segments = []
            for d in self.drawings:
                coords = d.get("coords") or []
                for i in range(len(coords) - 1):
                    all_segments.append((
                        np.array(coords[i],     dtype=np.float64),
                        np.array(coords[i + 1], dtype=np.float64),
                    ))

            n_segs = len(all_segments)
            for a in range(n_segs):
                p1, p2 = all_segments[a]
                for b in range(a + 1, n_segs):
                    p3, p4 = all_segments[b]
                    ix = self._segment_intersection_2d(p1, p2, p3, p4)
                    if ix is None:
                        continue
                    # Check cursor is close enough on screen
                    screen_dist = self._world_to_screen_distance(ix, pos)
                    if screen_dist >= screen_tol_px:
                        continue
                    world_dist = np.linalg.norm(ix[:2] - pos_2d)
                    if best_ix_dist is None or world_dist < best_ix_dist:
                        best_ix_dist = world_dist
                        ix_target    = ix

            if ix_target is not None:
                snap_target = tuple(ix_target)

        # ── Also snap to the current in-progress line's own vertices ─────────
        if not _intercept_mode:
            for c in self.temp_points:
                dx = float(c[0]) - float(pos[0])
                dy = float(c[1]) - float(pos[1])
                dist = np.sqrt(dx * dx + dy * dy)
                if dist < best_world:
                    best_world  = dist
                    snap_target = c

        # ── Snap marker (green sphere, scales with zoom like a fixed-pixel dot) ──
        if not hasattr(self, "_snap_marker") or self._snap_marker is None:
            sphere = vtk.vtkSphereSource()
            sphere.SetRadius(1.0)           # visual size — will be hidden most of the time
            sphere.SetThetaResolution(16)
            sphere.SetPhiResolution(16)
            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputConnection(sphere.GetOutputPort())
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(0.0, 1.0, 0.0)   # bright green
            actor.GetProperty().SetOpacity(0.9)
            actor.SetVisibility(False)
            actor.PickableOff()
            self._snap_marker = actor
            self._add_actor_to_overlay(actor)

        if snap_target is not None:
            # Scale the marker so it is always ~10 px regardless of zoom
            marker_world_r = self._screen_to_world_tolerance(6)
            self._snap_marker.SetPosition(float(snap_target[0]),
                                        float(snap_target[1]),
                                        float(snap_target[2]))
            # vtkSphereSource radius can't be changed on-the-fly without rebuilding,
            # so scale the actor instead
            self._snap_marker.SetScale(marker_world_r, marker_world_r, marker_world_r)
            self._snap_marker.SetVisibility(True)
        else:
            self._snap_marker.SetVisibility(False)

        return np.array(snap_target) if snap_target is not None else pos
    

    def _hide_snap_marker_now(self):
        """Hide the AccuSnap marker immediately."""
        marker = getattr(self, "_snap_marker", None)
        if marker is not None:
            try:
                marker.SetVisibility(False)
            except Exception:
                pass

    def _schedule_hide_snap_marker(self, delay_ms=500):
        """
        Hide snap marker after delay. Safe against multiple drawings:
        an older timer won't hide a new snap marker (sequence guard).
        """
        marker = getattr(self, "_snap_marker", None)
        if marker is None:
            return

        # Only schedule if currently visible
        try:
            if not marker.GetVisibility():
                return
        except Exception:
            return

        try:
            from PySide6.QtCore import QTimer
        except ImportError:
            from PyQt5.QtCore import QTimer

        self._snap_hide_seq += 1
        seq = self._snap_hide_seq

        def _do_hide():
            if seq != getattr(self, "_snap_hide_seq", 0):
                return
            self._hide_snap_marker_now()
            try:
                self.app.vtk_widget.render()
            except Exception:
                pass

        QTimer.singleShot(delay_ms, _do_hide)
    
    
    def keyPressEvent(self, event):
        """Handle keyboard events - check digitizer first!"""
        
        # ✅ CRITICAL: Let digitizer handle Ctrl+Z/Ctrl+Y if it's active
        if hasattr(self, 'digitizer') and self.digitizer.enabled:
            key = event.key()
            modifiers = event.modifiers()
            
            from PySide6.QtCore import Qt
            
            # Ctrl+Z - Undo (digitizer priority)
            if key == Qt.Key_Z and modifiers == Qt.ControlModifier:
                self.digitizer.undo()
                event.accept()
                return
            
            # Ctrl+Y - Redo (digitizer priority)
            if key == Qt.Key_Y and modifiers == Qt.ControlModifier:
                self.digitizer.redo()
                event.accept()
                return  
        super().keyPressEvent(event)
        

    def _on_left_press(self, obj, evt):
        """Handle left mouse press - add vertices continuously."""
        if not self.enabled:
            return

         # Vertex-pivot rotate owns the next left click.
        # Prevent normal draw/select tools from also processing this click.
        if getattr(self, "active_tool", None) == "rotatepivot":
            self._consume_vtk_event(obj)
            return
        
        # ── TEXT TOOL: placement is handled entirely by _start_text_label observers ──
        # We must NOT consume the event here — doing so blocks middle-mouse panning
        # because VTK's interactor style never receives the button-press.
        # The text tool installs its own high-priority observer in _start_text_label,
        # so returning here is safe and correct.
        if self.active_tool == "text":
            return

        # Modal corner-rotate tool: block every normal draw/resize/select path.
        # The integrated DigitizeCornerRotateTool has its own high-priority
        # observers, so this handler must not draw anything while cornerrotate is active.
        if getattr(self, "active_tool", None) == "cornerrotate":
            self._consume_vtk_event(obj)
            self.left_down = True
            return

        self._consume_vtk_event(obj)
        self.left_down = True
        pos = self._get_mouse_world()
        x, y = self.interactor.GetEventPosition()
        world_pos = self._display_to_world(x, y)

        # Shift+Click vertex drag
        if self.interactor.GetShiftKey() and not self.active_tool and not getattr(self, '_shift_blocked', False):
            nearest_vertex, nearest_drawing, vertex_idx = self._find_nearest_vertex_at_position(x, y, tolerance=15.0)
            if nearest_vertex is not None and nearest_drawing:
                self._save_state()
                self.dragging_vertex = {
                    'drawing': nearest_drawing,
                    'vertex_index': vertex_idx,
                    'original_pos': nearest_vertex
                }
                if self.vertex_drag_marker:
                    self._remove_actor_from_overlay(self.vertex_drag_marker)
                self.vertex_drag_marker = self._add_endpoint_sphere(nearest_vertex, color=(1, 1, 0), radius=0.12)
                self.app.vtk_widget.render()
                return

        # Vertex insertion tool
        if self.active_tool == "vertex":
            render_window = self.app.vtk_widget.GetRenderWindow()
            window_height = render_window.GetSize()[1]
            qt_y = window_height - y
            picked_drawing, insertion_point = self._find_line_at_position(x, qt_y)
            if picked_drawing and insertion_point is not None:
                self._insert_vertex_at_point(picked_drawing, insertion_point)
            else:
                print("⚪ No line found at cursor position")
            return

        # Selection mode
        if not self.active_tool:
            actor = self._pick_actor(x, y)
            if actor:
                self._select_actor(actor)
            return

        # ============ GIS POINT ============
        if self.active_tool == "gispoint":
            self._finalize_gis_point(pos)
            return
        # ============ FREEHAND ============
        if self.active_tool == "freehand":
            if not self.temp_points:
                self.temp_points = [world_pos]
                print("🖊️ Freehand STARTED")
            else:
                self.temp_points.append(world_pos)
                if len(self.temp_points) >= 2:
                    fh_style = self._get_draw_style('freehand')
                    self._update_freehand_preview_world(
                        color=fh_style['color'],
                        width=fh_style['width'],
                        line_style=fh_style['style'],
                    )
                self.app.vtk_widget.render()
                print("🖊️ Freehand RESUMED")
            self.is_drawing_freehand = True
            return

        # ============ SMARTLINE ============
        if self.active_tool == "smartline":
            sl_style = self._get_draw_style('smartline')
            self._push_temp_vertex_history()

            if len(self.temp_points) == 0:
                snap_target   = None
                snap_distance = float('inf')
                world_tol     = self._screen_to_world_tolerance(15)
                for drawing in self.drawings:
                    if 'coords' in drawing:
                        for vertex in drawing['coords']:
                            vertex_arr = np.array(vertex)
                            dx = float(vertex_arr[0]) - float(pos[0])
                            dy = float(vertex_arr[1]) - float(pos[1])
                            dist = np.sqrt(dx * dx + dy * dy)
                            if dist < world_tol and dist < snap_distance:
                                snap_distance = dist
                                snap_target   = vertex_arr

                if getattr(self, "snap_mode", None) == "nearby":
                    for drawing in self.drawings:
                        coords = drawing.get("coords") or []
                        for i in range(len(coords) - 1):
                            p1 = np.array(coords[i], dtype=np.float64)
                            p2 = np.array(coords[i + 1], dtype=np.float64)
                            point_on_seg, _ = self._closest_point_on_segment(
                                np.array(pos, dtype=np.float64), p1, p2
                            )
                            screen_dist = self._world_to_screen_distance(point_on_seg, pos)
                            if screen_dist < 25:
                                world_dist = np.linalg.norm(
                                    np.array(point_on_seg[:2], dtype=np.float64)
                                    - np.array(pos[:2], dtype=np.float64)
                                )
                                if world_dist < snap_distance:
                                    snap_distance = world_dist
                                    snap_target   = point_on_seg

                if snap_target is not None:
                    pos = snap_target

            self.temp_points.append(pos)

            if not hasattr(self, '_smartline_vertex_markers'):
                self._smartline_vertex_markers = []

            if len(self.temp_points) == 1:
                sphere = self._add_endpoint_sphere(pos, color=(0, 1, 0), radius=0.20)
            else:
                sphere = self._add_endpoint_sphere(pos, color=(1, 1, 0), radius=0.20)
                if len(self._smartline_vertex_markers) > 1:
                    self._smartline_vertex_markers[-1].GetProperty().SetColor(1, 1, 0)
            self._smartline_vertex_markers.append(sphere)

            if len(self.temp_points) >= 2:
                self._update_continuous_preview(
                    color=sl_style['color'],
                    width=sl_style['width'],
                    line_style=sl_style['style'],
                )
                self._update_cursor_preview(
                    color=sl_style['color'],
                    width=sl_style['width'],
                    line_style=sl_style['style'],
                    current_world=pos,
                )
                self.app.vtk_widget.render()
            return

        # ============ LINE ============
        if self.active_tool == "line":
            ln_style = self._get_draw_style('line')
            self._push_temp_vertex_history()

            self.temp_points.append(pos)

            if not hasattr(self, '_line_vertex_markers'):
                self._line_vertex_markers = []

            if len(self.temp_points) == 1:
                sphere = self._add_endpoint_sphere(pos, color=(0, 1, 0), radius=0.05)
                sphere.GetProperty().SetOpacity(0.8)
            else:
                sphere = self._add_endpoint_sphere(pos, color=(1, 1, 0), radius=0.05)
                sphere.GetProperty().SetOpacity(0.8)
                if len(self._line_vertex_markers) > 1:
                    self._line_vertex_markers[-1].GetProperty().SetColor(1, 1, 0)
            self._line_vertex_markers.append(sphere)

            if len(self.temp_points) >= 2:
                self._update_continuous_preview(
                    color=ln_style['color'],
                    width=ln_style['width'],
                    line_style=ln_style['style'],
                )
                self._update_cursor_preview(
                    color=ln_style['color'],
                    width=ln_style['width'],
                    line_style=ln_style['style'],
                    current_world=pos,
                )
                self.app.vtk_widget.render()
            return

        # ============ RECTANGLE ============
        if self.active_tool == "rectangle":
            if not self.temp_points:
                self.temp_points = [pos]
                return
            if len(self.temp_points) == 1:
                self.temp_points.append(pos)
                self._remove_preview_actor_2d('_rectangle_preview_actor')
                self._finalize_rectangle()
                return

        # ============ CIRCLE ============
        if self.active_tool == "circle":
            if not self.temp_points:
                self.temp_points = [pos]
                return
            if len(self.temp_points) == 1:
                self.temp_points.append(pos)
                self._finalize_circle()
                return

        # ============ POLYLINE ============
        if self.active_tool == "polyline":
            pl_style = self._get_draw_style('polyline')
            self._push_temp_vertex_history()

            self.temp_points.append(pos)

            if not hasattr(self, '_polyline_vertex_markers'):
                self._polyline_vertex_markers = []

            if len(self.temp_points) == 1:
                sphere = self._add_endpoint_sphere(pos, color=(0, 1, 0), radius=0.05)
                sphere.GetProperty().SetOpacity(0.8)
            else:
                sphere = self._add_endpoint_sphere(pos, color=(1, 1, 0), radius=0.05)
                sphere.GetProperty().SetOpacity(0.8)
                if len(self._polyline_vertex_markers) > 1:
                    self._polyline_vertex_markers[-1].GetProperty().SetColor(1, 1, 0)
            self._polyline_vertex_markers.append(sphere)

            if len(self.temp_points) >= 2:
                self._update_continuous_preview(
                    color=pl_style['color'],
                    width=pl_style['width'],
                    line_style=pl_style['style'],
                )
                self._update_cursor_preview(
                    color=pl_style['color'],
                    width=pl_style['width'],
                    line_style='dotted',
                    close_loop=True,
                    current_world=pos,
                )
                self.app.vtk_widget.render()
            return

    # ============================================================================
    # VERTEX INSERTION METHODS
    # ============================================================================
    
    def _find_line_at_position(self, x, y, tolerance=50.0):  # ✅ Increased from 15 to 50
        """
        Find which line/polyline is near the clicked position and 
        determine the closest point on that line for vertex insertion.
        
        Returns:
            (drawing, insertion_point) if found, (None, None) otherwise
        """
        try:
            # ✅ CRITICAL: Get VTK Y coordinate (flip from Qt coordinate system)
            render_window = self.app.vtk_widget.GetRenderWindow()
            window_height = render_window.GetSize()[1]
            vtk_y = window_height - y

            # Get world coordinates of click
            picker = vtk.vtkWorldPointPicker()
            picker.Pick(x, vtk_y, 0, self.renderer)  # ✅ Use vtk_y not y
            click_world = np.array(picker.GetPickPosition())

            closest_drawing = None
            closest_point = None
            min_distance = float('inf')

            # Search through all drawings
            for drawing in self._iter_pickable_drawings():
                # Only consider line-based drawings
                if drawing.get('type', '') not in ['line_segment', 'smartline', 'centerline', 'polyline', 'freehand', 'rectangle', 'polygon', 'circle', 'curve']:
                    continue

                coords = drawing.get('coords') or drawing.get('interpolated') or []
                if len(coords) < 2:
                    continue

                # Check each line segment
                for i in range(len(coords) - 1):
                    p1 = np.array(coords[i])
                    p2 = np.array(coords[i + 1])

                    # Find closest point on this segment to click position
                    point_on_segment, dist = self._closest_point_on_segment(click_world, p1, p2)

                    # Convert to screen space for accurate distance check
                    screen_dist = self._world_to_screen_distance(point_on_segment, click_world)

                    if screen_dist < tolerance and screen_dist < min_distance:
                        min_distance = screen_dist
                        closest_drawing = drawing
                        closest_point = {
                            'coords': point_on_segment.tolist(),
                            'segment_index': i,
                            'distance': screen_dist
                        }
            
            if closest_drawing and closest_point:
                print(f"✅ Found {closest_drawing['type']} at distance {min_distance:.1f}px, segment {closest_point['segment_index']}")
                return closest_drawing, closest_point
            else:
                print(f"⚪ No line found within {tolerance}px tolerance")
            
            return None, None
            
        except Exception as e:
            print(f"⚠️ Error finding line: {e}")
            import traceback
            traceback.print_exc()
            return None, None
      
    def _point_in_polygon(self, point, polygon_coords):
        """Ray-casting point-in-polygon test (2-D XY plane)."""
        x, y = float(point[0]), float(point[1])
        n = len(polygon_coords)
        inside = False
        j = n - 1
        for i in range(n):
            xi, yi = float(polygon_coords[i][0]), float(polygon_coords[i][1])
            xj, yj = float(polygon_coords[j][0]), float(polygon_coords[j][1])
            if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
                inside = not inside
            j = i
        return inside

    def _polygon_centroid_2d(self, polygon_coords):
        """
        Compute the 2-D centroid of a polygon using the signed-area formula.
        Falls back to simple vertex average for degenerate (zero-area) cases.
        """
        coords = polygon_coords
        # Remove duplicate closing vertex if present
        if len(coords) > 1:
            p0 = coords[0]
            pn = coords[-1]
            if abs(float(p0[0]) - float(pn[0])) < 1e-9 and abs(float(p0[1]) - float(pn[1])) < 1e-9:
                coords = coords[:-1]
        n = len(coords)
        if n == 0:
            return 0.0, 0.0
        area = 0.0
        cx = 0.0
        cy = 0.0
        for i in range(n):
            x0, y0 = float(coords[i][0]), float(coords[i][1])
            x1, y1 = float(coords[(i + 1) % n][0]), float(coords[(i + 1) % n][1])
            cross = x0 * y1 - x1 * y0
            area += cross
            cx += (x0 + x1) * cross
            cy += (y0 + y1) * cross
        area *= 0.5
        if abs(area) < 1e-12:
            # Degenerate polygon — simple average
            cx = sum(float(c[0]) for c in coords) / n
            cy = sum(float(c[1]) for c in coords) / n
            return cx, cy
        factor = 1.0 / (6.0 * area)
        return cx * factor, cy * factor

    def _closest_point_on_segment(self, point, seg_start, seg_end):
        """
        Find the closest point on a line segment to a given point.
        
        Returns:
            (closest_point, distance)
        """
        # Vector from seg_start to seg_end
        segment = seg_end - seg_start
        segment_length_sq = np.dot(segment, segment)
        
        if segment_length_sq == 0:
            # Degenerate segment (point)
            return seg_start, np.linalg.norm(point - seg_start)
        
        # Project point onto line segment
        t = max(0, min(1, np.dot(point - seg_start, segment) / segment_length_sq))
        
        # Closest point on segment
        closest = seg_start + t * segment
        distance = np.linalg.norm(point - closest)
        
        return closest, distance
    
    
    def _world_to_screen(self, world_pos):
        """Convert a 3D world coordinate to 2D screen display coordinate."""
        try:
            z = world_pos[2] if len(world_pos) > 2 else 0.0
            self.renderer.SetWorldPoint(float(world_pos[0]), float(world_pos[1]), float(z), 1.0)
            self.renderer.WorldToDisplay()
            screen = self.renderer.GetDisplayPoint()
            return float(screen[0]), float(screen[1])
        except Exception:
            return 0.0, 0.0

    def _world_to_screen_distance(self, world_pos1, world_pos2):
        """
        Calculate distance between two world points in screen pixels.
        More accurate for user interaction than world-space distance.
        """
        try:
            # Convert first point to screen
            self.renderer.SetWorldPoint(world_pos1[0], world_pos1[1], world_pos1[2], 1.0)
            self.renderer.WorldToDisplay()
            screen1 = self.renderer.GetDisplayPoint()
            
            # Convert second point to screen
            self.renderer.SetWorldPoint(world_pos2[0], world_pos2[1], world_pos2[2], 1.0)
            self.renderer.WorldToDisplay()
            screen2 = self.renderer.GetDisplayPoint()
            
            # Calculate 2D screen distance
            dx = screen2[0] - screen1[0]
            dy = screen2[1] - screen1[1]
            return np.sqrt(dx*dx + dy*dy)
            
        except Exception:
            # Fallback to world distance
            return np.linalg.norm(np.array(world_pos1) - np.array(world_pos2))
    
    
    def _segment_intersection_2d(self, p1, p2, p3, p4):
        """Return the 3-D intersection point of segments (p1,p2) and (p3,p4) in XY,
        or None if they are parallel, collinear, or only meet at a shared endpoint.

        Uses parametric line intersection. Both parameters t and u must be in
        (0, 1) exclusive so that purely vertex-to-vertex touches are excluded —
        only true interior crossings qualify as intercept-snap candidates.
        Z is linearly interpolated along the first segment at the crossing t.
        """
        d1 = p2[:2] - p1[:2]
        d2 = p4[:2] - p3[:2]
        cross = d1[0] * d2[1] - d1[1] * d2[0]
        if abs(cross) < 1e-10:
            return None  # parallel or collinear
        delta = p3[:2] - p1[:2]
        t = (delta[0] * d2[1] - delta[1] * d2[0]) / cross
        u = (delta[0] * d1[1] - delta[1] * d1[0]) / cross
        _EPS = 1e-9
        if not (_EPS < t < 1.0 - _EPS and _EPS < u < 1.0 - _EPS):
            return None  # intersection outside segment interiors (endpoint touch)
        ix = p1[:2] + t * d1
        z  = float(p1[2]) + t * (float(p2[2]) - float(p1[2])) if len(p1) > 2 else 0.0
        return np.array([ix[0], ix[1], z], dtype=np.float64)

    def _insert_vertex_at_point(self, drawing, insertion_point):
        """
        Insert a new vertex into an existing line, splitting it into two segments.
        Optionally enables dragging for the new vertex based on vertex_auto_drag setting.
        Args:
            drawing: The drawing dict to modify
            insertion_point: Dict with 'coords', 'segment_index', 'distance'
        """
        try:
            self._save_state() 
            new_vertex = insertion_point['coords']
            segment_idx = insertion_point['segment_index']
            
            print(f"📍 Inserting vertex at segment {segment_idx}: {new_vertex}")
            
            coords = drawing['coords']
            drawing_type = drawing.get('type')
            was_selected = getattr(self, "selected_drawing", None) is drawing
            was_multi_selected = any(
                selected is drawing for selected in (getattr(self, "multi_selected", []) or [])
            )
            
            new_vertex_tuple = tuple(new_vertex)
            target_drawing = None
            target_vertex_idx = None
            
            # ✅ HANDLE LINE_SEGMENT: Split into TWO independent segments
            if drawing_type == 'line_segment':
                # Get the original segment endpoints
                p1 = coords[0]
                p2 = coords[-1]
                orig_color = drawing.get('original_color', (1, 0, 0))
                orig_width = drawing.get('original_width', 2)
                
                # ✅ ENHANCED CLEANUP: Remove the original segment completely
                if 'actor' in drawing and drawing['actor']:
                    try:
                        self._remove_actor_from_overlay(drawing['actor'])
                        drawing['actor'].VisibilityOff()
                    except Exception:
                        pass
                    drawing['actor'] = None
                
                # Remove old markers
                for marker_key in ['start_marker', 'end_marker']:
                    if marker_key in drawing and drawing[marker_key]:
                        try:
                            self._remove_actor_from_overlay(drawing[marker_key])
                            drawing[marker_key].VisibilityOff()
                        except Exception:
                            pass
                        drawing[marker_key] = None
                
                # Remove from drawings list using identity check
                try:
                    self.drawings[:] = [d for d in self.drawings if d is not drawing]
                    print("  🗑️ Removed original segment from drawings list")
                except Exception as e:
                    print(f"  ⚠️ Failed to remove segment: {e}")
                
                # ✅ FORCE RENDER before creating new segments
                self.renderer.Modified()
                self.app.vtk_widget.render()
                
                # ✅ CREATE SEGMENT 1: p1 → new_vertex
                coords_1 = [p1, new_vertex_tuple]
                actor_1 = self._make_polyline_actor(coords_1, color=orig_color, width=orig_width)
                actor_1.PickableOn()
                actor_1.SetPickable(1)
                self._add_actor_to_overlay(actor_1)
                
                start_marker_1 = self._add_endpoint_sphere(coords_1[0], color=(0, 1, 0), radius=0.05)
                end_marker_1 = self._add_endpoint_sphere(coords_1[1], color=(1, 1, 0), radius=0.05)  # Yellow (shared)
                
                segment_1 = {
                    "type": "line_segment",
                    "coords": coords_1,
                    "actor": actor_1,
                    "bounds": actor_1.GetBounds(),
                    "start_marker": start_marker_1,
                    "end_marker": end_marker_1,
                    "original_color": orig_color,
                    "original_width": orig_width
                }
                self.drawings.append(segment_1)
                
                # ✅ CREATE SEGMENT 2: new_vertex → p2
                coords_2 = [new_vertex_tuple, p2]
                actor_2 = self._make_polyline_actor(coords_2, color=orig_color, width=orig_width)
                actor_2.PickableOn()
                actor_2.SetPickable(1)
                self._add_actor_to_overlay(actor_2)
                
                start_marker_2 = self._add_endpoint_sphere(coords_2[0], color=(1, 1, 0), radius=0.05)  # Yellow (shared)
                end_marker_2 = self._add_endpoint_sphere(coords_2[1], color=(1, 0, 0), radius=0.05)
                
                segment_2 = {
                    "type": "line_segment",
                    "coords": coords_2,
                    "actor": actor_2,
                    "bounds": actor_2.GetBounds(),
                    "start_marker": start_marker_2,
                    "end_marker": end_marker_2,
                    "original_color": orig_color,
                    "original_width": orig_width
                }
                self.drawings.append(segment_2)
                
                print(f"✅ Line segment SPLIT into 2 segments:")
                print(f"   Segment 1: {coords_1}")
                print(f"   Segment 2: {coords_2}")
                
                # Store reference for optional drag mode
                target_drawing = segment_1
                target_vertex_idx = 1  # The end point of segment_1 (the new vertex)
            
            # ✅ HANDLE SMARTLINE/POLYLINE/FREEHAND/RECTANGLE: Add vertex to existing multi-vertex line
            elif drawing_type == 'curve':
                control_points = [tuple(pt) for pt in (drawing.get('control_points') or [])]
                if len(control_points) < 2:
                    print("Curve has no editable control points")
                    return

                total_curve_segments = max(len(coords) - 1, 1)
                total_control_segments = max(len(control_points) - 1, 1)
                points_per_control_segment = max(
                    1, int(round(total_curve_segments / total_control_segments))
                )
                control_segment_idx = min(
                    total_control_segments - 1,
                    max(0, int(segment_idx // points_per_control_segment)),
                )
                new_control_idx = control_segment_idx + 1
                control_points.insert(new_control_idx, new_vertex_tuple)

                curve_tool = getattr(getattr(self, 'app', None), 'curve_tool', None)
                if curve_tool is None or not hasattr(curve_tool, '_interpolate_catmull_rom'):
                    print("Curve tool unavailable for vertex insertion")
                    return

                new_curve_points = curve_tool._interpolate_catmull_rom(control_points)
                if new_curve_points is None or len(new_curve_points) < 2:
                    print("Curve rebuild failed after vertex insertion")
                    return

                if 'actor' in drawing and drawing['actor']:
                    try:
                        if hasattr(curve_tool, '_remove_curve_actor'):
                            curve_tool._remove_curve_actor(drawing['actor'])
                        else:
                            self._remove_actor_from_overlay(drawing['actor'])
                        drawing['actor'].VisibilityOff()
                    except Exception as e:
                        print(f"Failed to remove curve actor: {e}")
                    drawing['actor'] = None

                if 'vertex_markers' in drawing and drawing['vertex_markers']:
                    for marker in drawing['vertex_markers']:
                        try:
                            self._remove_actor_from_overlay(marker)
                            marker.VisibilityOff()
                        except Exception:
                            pass
                    drawing['vertex_markers'] = []

                drawing['control_points'] = [tuple(pt) for pt in control_points]
                drawing['interpolated'] = [tuple(pt) for pt in new_curve_points]
                drawing['coords'] = [tuple(pt) for pt in new_curve_points]

                self._rebuild_drawing_actor(drawing)
                if drawing.get('actor'):
                    drawing['bounds'] = drawing['actor'].GetBounds()

                if self._selection_manager is not None and self._selection_manager.contains(drawing):
                    try:
                        self._highlight_line(drawing)
                    except Exception:
                        pass

                print(f"Inserted curve vertex. Control points: {len(control_points)}")

                target_drawing = drawing
                target_vertex_idx = None

            elif drawing_type in ['smartline', 'centerline', 'line', 'polyline', 'freehand', 'rectangle', 'polygon']:
                # Insert new vertex into coordinates list
                new_vertex_idx = segment_idx + 1
                coords.insert(new_vertex_idx, new_vertex_tuple)
                
                # ✅ ENHANCED CLEANUP: Remove ALL old geometry systematically
                # 1. Remove main actor
                if 'actor' in drawing and drawing['actor']:
                    try:
                        self._remove_actor_from_overlay(drawing['actor'])
                        drawing['actor'].VisibilityOff()
                    except Exception as e:
                        print(f"  ⚠️ Failed to remove main actor: {e}")
                    drawing['actor'] = None
                
                # 2. Remove ALL vertex markers
                if 'vertex_markers' in drawing and drawing['vertex_markers']:
                    for marker in drawing['vertex_markers']:
                        try:
                            self._remove_actor_from_overlay(marker)
                            marker.VisibilityOff()
                        except Exception:
                            pass
                    drawing['vertex_markers'] = []
                
                # 3. Remove arrows if present
                if 'arrow_actor' in drawing and drawing['arrow_actor']:
                    arrows = drawing['arrow_actor']
                    if not isinstance(arrows, list):
                        arrows = [arrows]
                    for arr in arrows:
                        try:
                            self.renderer.RemoveActor2D(arr)
                            arr.VisibilityOff()
                        except Exception:
                            pass
                    drawing['arrow_actor'] = None
                
                # ✅ FORCE RENDER PIPELINE FLUSH before creating new geometry
                self.renderer.Modified()
                self.app.vtk_widget.render()
                
                # Recreate actor with new geometry
                color = drawing.get('original_color', (1, 0, 0))
                width = drawing.get('original_width', 2)
                new_actor = self._make_polyline_actor(coords, color=color, width=width)
                
                # ✅ CRITICAL: Ensure new actor is pickable and visible
                new_actor.PickableOn()
                new_actor.VisibilityOn()
                new_actor.SetPickable(1)
                
                # Update drawing
                drawing['actor'] = new_actor
                drawing['coords'] = coords
                drawing['bounds'] = new_actor.GetBounds()
                
                # Add new actor to renderer
                self._add_actor_to_overlay(new_actor)
                
                # CREATE PERMANENT VERTEX MARKERS
                drawing['vertex_markers'] = []
                
                for i, pt in enumerate(coords):
                    if i == 0:
                        marker_color = (0, 1, 0)  # Green start
                    elif i == len(coords) - 1:
                        marker_color = (1, 0, 0)  # Red end
                    else:
                        marker_color = (1, 1, 0)  # Yellow middle
                    
                    marker = self._add_endpoint_sphere(pt, color=marker_color, radius=0.05)
                    marker.GetProperty().SetOpacity(0.8)
                    drawing['vertex_markers'].append(marker)
                
                print(f"✅ Vertex inserted! Line now has {len(coords)} points")
                
                # Store reference for optional drag mode
                target_drawing = drawing
                target_vertex_idx = new_vertex_idx
            
            # ✅ FORCE COMPLETE RENDER before optional drag mode
            self.renderer.Modified()
            self.app.vtk_widget.interactor.GetRenderWindow().Render()
            self.app.vtk_widget.render()
            
            # ✅ CONDITIONAL: Enable drag mode ONLY if setting is enabled
            try:
                if target_drawing is not None and getattr(self, "_rotate_vertex_pivot_showing_vertices", False):
                    drawings_for_overlay = self._get_transform_selection() or [target_drawing]
                    self._show_rotate_vertex_pivot_vertices(drawings_for_overlay)
                elif target_drawing is not None and (was_selected or was_multi_selected):
                    if was_selected:
                        self.selected_drawing = target_drawing
                    points_to_show = self._collect_transform_vertex_points([target_drawing])
                    if points_to_show:
                        self.show_vertex_coordinates(points_to_show)
                    self._highlight_line(target_drawing)
                    self.app.vtk_widget.render()
            except Exception as marker_error:
                print(f"⚠️ Vertex visibility refresh failed after insertion: {marker_error}")

            if getattr(self, 'vertex_auto_drag', False) and target_drawing and target_vertex_idx is not None:
                self.dragging_vertex = {
                    'drawing': target_drawing,
                    'vertex_index': target_vertex_idx,
                    'original_pos': new_vertex_tuple
                }
                
                # Create drag marker
                if hasattr(self, 'vertex_drag_marker') and self.vertex_drag_marker:
                    try:
                        self._remove_actor_from_overlay(self.vertex_drag_marker)
                    except Exception:
                        pass
                
                self.vertex_drag_marker = self._add_endpoint_sphere(
                    new_vertex_tuple, color=(1, 1, 0), radius=0.12
                )
                
                print("🖱️ Drag mode enabled - move mouse to reposition, click to place")
            else:
                print("✅ Vertex inserted (insert-only mode)")
            
        except Exception as e:
            print(f"❌ Failed to insert vertex: {e}")
            import traceback
            traceback.print_exc()
        
        
    def _reset_stale_zoom_mouse_state(self):
        """Clear stale mouse state, including VTK's right-drag Dolly state."""
        style = None
        try:
            style = self.interactor.GetInteractorStyle()
            if style:
                if hasattr(style, "OnLeftButtonUp"):
                    style.OnLeftButtonUp()
                if hasattr(style, "OnRightButtonUp"):
                    style.OnRightButtonUp()
                if hasattr(style, "OnMiddleButtonUp"):
                    style.OnMiddleButtonUp()
        except Exception:
            pass

        # Grid labels are opened by right-click. A long synchronous grid load
        # can prevent VTK from receiving the physical release, leaving the
        # interactor in VTKIS_DOLLY. Button-up callbacks alone are not reliable
        # once that release was lost, so explicitly terminate the style state.
        try:
            if style is not None and hasattr(style, "GetState"):
                if int(style.GetState()) != 0 and hasattr(style, "StopState"):
                    style.StopState()
        except Exception:
            pass

        try:
            if hasattr(self.interactor, "SetControlKey"):
                self.interactor.SetControlKey(0)
            if hasattr(self.interactor, "SetShiftKey"):
                self.interactor.SetShiftKey(0)
            if hasattr(self.interactor, "SetAltKey"):
                self.interactor.SetAltKey(0)
            if hasattr(self.interactor, "ReleaseFocus"):
                self.interactor.ReleaseFocus()
        except Exception:
            pass

        self.left_down = False
        self.middle_down = False
        self._is_panning = False
        self._pan_start_pos = None
        self._last_pos = None
        self._blocked_fake_middle = False

        if hasattr(self, "_middle_button_down"):
            self._middle_button_down = False

        print("Stale zoom mouse state reset")

    def _is_real_middle_button_down(self):
        """Return True only while the physical middle mouse button is pressed."""
        try:
            from PySide6.QtWidgets import QApplication
            from PySide6.QtCore import Qt as _Qt
        except Exception:
            try:
                from PyQt5.QtWidgets import QApplication
                from PyQt5.QtCore import Qt as _Qt
            except Exception:
                return True

        try:
            panning_button = getattr(self.app, "panning_button", "scroll") if self.app else "scroll"
            buttons = QApplication.mouseButtons()
            return bool(buttons & _Qt.MiddleButton)
        except Exception:
            return True

    def _begin_manual_pan_rendering(self):
        """Route manual pan repaints through the main interaction scheduler."""
        manager = getattr(self.app, "gpu_render_manager", None) if self.app else None
        if manager is None or not hasattr(manager, "begin_pan_interaction"):
            return False
        try:
            data = getattr(self.app, "data", None)
            point_count = len(data.get("xyz", ())) if isinstance(data, dict) else 0
            return bool(manager.begin_pan_interaction(point_count=point_count))
        except Exception:
            return False

    def _request_manual_pan_render(self):
        """Coalesce costly full-cloud repaints while camera motion stays immediate."""
        vtk_widget = getattr(self.app, "vtk_widget", None) if self.app else None
        manager = getattr(self.app, "gpu_render_manager", None) if self.app else None
        if vtk_widget is None:
            return
        if manager is not None and hasattr(manager, "request_render"):
            try:
                manager.request_render(vtk_widget)
                return
            except Exception:
                pass
        try:
            vtk_widget.render()
        except Exception:
            pass

    def _finish_manual_pan_rendering(self):
        """Settle clipping and draw one final frame after manual pan."""
        try:
            self.renderer.ResetCameraClippingRange()
        except Exception:
            pass

        manager = getattr(self.app, "gpu_render_manager", None) if self.app else None
        if manager is not None and hasattr(manager, "finish_pan_interaction"):
            try:
                if manager.finish_pan_interaction():
                    return
            except Exception:
                pass

        vtk_widget = getattr(self.app, "vtk_widget", None) if self.app else None
        try:
            if vtk_widget is not None:
                vtk_widget.render()
        except Exception:
            pass

    def _on_middle_press(self, obj, evt):
        # Middle mouse is for pan only. Do not allow it to finalize Move Vertex.
        try:
            import time
            self._ignore_move_vertex_finish_until = time.monotonic() + 0.40
        except Exception:
            self._ignore_move_vertex_finish_until = 999999999.0
        """Start manual middle-button pan and ignore fake middle events."""
        # Skip when classification tool is active — ClassificationInteractor
        # handles its own custom pan (_on_safe_pan_start / _do_safe_pan) and
        # forwarding to style.OnMiddleButtonDown() would conflict with it.
        if getattr(self.app, "active_classify_tool", None) is not None:
            return
        zoom_tool = getattr(self.app, "zoom_rectangle_tool", None)

        if zoom_tool is not None:
            zoom_active = getattr(zoom_tool, "active", False)
            zoom_started = getattr(zoom_tool, "start_pos", None) is not None

            if zoom_active and zoom_started:
                print("Fake/middle pan ignored during zoom")

                self._blocked_fake_middle = True
                self.middle_down = False
                self._is_panning = False
                self._pan_start_pos = None
                self._last_pos = None

                try:
                    self.app.statusBar().showMessage(
                        "Zoom tool: finish with LEFT click only.",
                        2000,
                    )
                except Exception:
                    pass

                try:
                    if obj is not None and hasattr(obj, "AbortFlagOn"):
                        obj.AbortFlagOn()
                except Exception:
                    pass

                return

        if not self._is_real_middle_button_down():
            print("Fake middle press ignored - real middle button is not down")

            self._blocked_fake_middle = True
            self.middle_down = False
            self._is_panning = False
            self._pan_start_pos = None
            self._last_pos = None

            try:
                if obj is not None and hasattr(obj, "AbortFlagOn"):
                    obj.AbortFlagOn()
            except Exception:
                pass

            return
        print("🖱️ Middle button PRESSED - starting pan")

        # Move Vertex special case:
        # The main middle-pan handler has higher priority than the Move Vertex
        # middle observer, so protect/cancel the pending move here.
        if getattr(self, "active_tool", None) == "movevertex":
            try:
                import time
                self._middle_button_down = True
                self._middle_button_down = True
                self.middle_down = True
                self._is_panning = True
                self._move_vertex_pan_block_until = 0.0
                self._ignore_move_vertex_finish_until = 0.0
            except Exception:
                self._middle_button_down = True
                self._move_vertex_pan_block_until = 999999999.0
                self._ignore_move_vertex_finish_until = 999999999.0

            #self._cancel_move_vertex_pending_for_pan()

        # ── CRITICAL: Do NOT consume this event.
        # _consume_vtk_event blocks vtkInteractorStyleImage from receiving
        # the button press, so its internal pan state never initializes.
        # We set our flags and delegate to the style — that's all.
        self._blocked_fake_middle = False
        self.middle_down = True
        self._is_panning = True
        self._pan_press_monotonic = time.monotonic()
        self._pan_button_miss_count = 0

        pos = self.interactor.GetEventPosition()
        self._pan_start_pos = pos
        self._last_pos = pos

        try:
            rw = self.interactor.GetRenderWindow()
            if rw is not None:
                rw.SetDesiredUpdateRate(30.0)
        except Exception:
            pass

        self._begin_manual_pan_rendering()

        # Let VTK's built-in pan handler do the actual work
        try:
            if obj is not None and hasattr(obj, "AbortFlagOn"):
                obj.AbortFlagOn()
        except Exception:
            pass


    def _on_mouse_move(self, obj, evt):
        """Show rubber-band preview line as you move mouse."""
        if not self.enabled:
            if not getattr(self, "active_tool", None) or \
            not getattr(self, "is_drawing_freehand", False):
                return
            return

        # ══════════════════════════════════════════════════════════════════
        # PANNING: Absolute top priority — never block middle-mouse pan.
        # Check this BEFORE any _consume_vtk_event call so the interactor
        # style always receives MouseMoveEvent during pan.
        # ══════════════════════════════════════════════════════════════════
        if self._is_panning:
            if not self.middle_down:
                # Middle button was released without us catching it — reset
                self._is_panning = False
                self._pan_start_pos = None
                self.app.vtk_widget.render()
                return
            # Forward to interactor style — this is what moves the camera
            style = self.interactor.GetInteractorStyle()
            if style and hasattr(style, "OnMouseMove"):
                style.OnMouseMove()
            self.app.vtk_widget.render()
            return

        # ══════════════════════════════════════════════════════════════════
        # Vertex dragging (Shift+Click drag mode)
        # ══════════════════════════════════════════════════════════════════
        if self.dragging_vertex:
            self._consume_vtk_event(obj)
            new_pos = self._get_mouse_world()
            drawing = self.dragging_vertex['drawing']
            vertex_idx = self.dragging_vertex['vertex_index']
            coords = drawing['coords']
            coords[vertex_idx] = tuple(new_pos)
            if self.vertex_drag_marker:
                self.vertex_drag_marker.SetPosition(*new_pos)
            self._rebuild_drawing_actor(drawing)
            if self.selected_drawing is drawing:
                self.clear_coordinate_labels()
                self.show_vertex_coordinates(coords)
            self.app.vtk_widget.render()
            return

        # ══════════════════════════════════════════════════════════════════
        # TEXT TOOL: Update preview position WITHOUT consuming the event.
        # Consuming here would block panning even though _is_panning=False,
        # because the middle press sets _is_panning=True only AFTER the
        # style receives OnMiddleButtonDown — there is a 1-frame gap where
        # a consumed MouseMoveEvent breaks the pan initialization sequence.
        # ══════════════════════════════════════════════════════════════════
        if self.active_tool == "text" and getattr(self, "_placing_text", False):
            try:
                world_pos = self._get_mouse_world_no_snap()
                # Lift text above the point cloud surface
                z_lift = self._text_z_offset()
                if hasattr(self, "_temp_text_actor") and self._temp_text_actor:
                    self._temp_text_actor.SetPosition(
                        world_pos[0], world_pos[1], world_pos[2] + z_lift
                    )
                    self.app.vtk_widget.render()
            except Exception:
                pass
            return

        # ══════════════════════════════════════════════════════════════════
        # All remaining active tools consume the event to prevent the
        # interactor style from also processing it (avoids double-handling).
        # ══════════════════════════════════════════════════════════════════
        if self.active_tool:
            self._consume_vtk_event(obj)

        # --- DELETE VERTEX: suppress all mouse-move effects ---
        if self.active_tool == "deletevertex":
            return

        # --- FREEHAND CONTINUOUS DRAWING ---
        if self.active_tool == "freehand" and \
        getattr(self, "left_down", False) and \
        getattr(self, "is_drawing_freehand", False):
            world_pos = self._get_mouse_world_no_snap()
            dist_px = 999
            if self.temp_points:
                dist_px = self._world_to_screen_distance(world_pos, self.temp_points[-1])
            if len(self.temp_points) == 0 or dist_px > 2.0:
                self.temp_points.append(world_pos)
                if len(self.temp_points) >= 2:
                    fh_style = self._get_draw_style('freehand')
                    self._update_freehand_preview_world(
                        color=fh_style['color'],
                        width=fh_style['width'],
                        line_style=fh_style['style'],
                    )
                self.app.vtk_widget.render()
            return

        # --- SMARTLINE ---
        if self.active_tool == "smartline" and len(self.temp_points) >= 1:
            sl_style = self._get_draw_style('smartline')
            if self.snap_enabled:
                raw_pos = self._get_mouse_world_no_snap()
                self._snap_point(raw_pos)
            if len(self.temp_points) >= 2:
                self._update_continuous_preview(
                    color=sl_style['color'],
                    width=sl_style['width'],
                    line_style=sl_style['style'],
                )
            self._update_cursor_preview(
                color=sl_style['color'],
                width=sl_style['width'],
                line_style=sl_style['style'],
            )
            self.app.vtk_widget.render()
            return

        # --- LINE ---
        if self.active_tool == "line" and len(self.temp_points) >= 1:
            ln_style = self._get_draw_style('line')
            raw_pos = self._get_mouse_world_no_snap()
            self._snap_point(raw_pos)
            if len(self.temp_points) >= 2:
                self._update_continuous_preview(
                    color=ln_style['color'],
                    width=ln_style['width'],
                    line_style=ln_style['style'],
                )
            self._update_cursor_preview(
                color=ln_style['color'],
                width=ln_style['width'],
                line_style=ln_style['style'],
            )
            self.app.vtk_widget.render()
            return

        # --- RECTANGLE PREVIEW ---
        if self.active_tool == "rectangle" and len(self.temp_points) == 1:
            rc_style = self._get_draw_style('rectangle')
            self._update_rectangle_preview(
                color=rc_style['color'],
                width=rc_style['width'],
                line_style=rc_style['style'],
            )
            self.app.vtk_widget.render()
            return

        # --- CIRCLE PREVIEW ---
        if self.active_tool == "circle" and len(self.temp_points) == 1:
            ci_style = self._get_draw_style('circle')
            self._update_circle_preview_world(
                color=ci_style['color'],
                width=ci_style['width'],
                line_style=ci_style['style'],
            )
            self.app.vtk_widget.render()
            return

        # --- POLYLINE ---
        if self.active_tool == "polyline" and len(self.temp_points) >= 1:
            pl_style = self._get_draw_style('polyline')
            if len(self.temp_points) >= 2:
                self._update_continuous_preview(
                    color=pl_style['color'],
                    width=pl_style['width'],
                    line_style=pl_style['style'],
                )
            self._update_cursor_preview(
                color=pl_style['color'],
                width=pl_style['width'],
                line_style='dotted',
                close_loop=True,
            )
            self.app.vtk_widget.render()
            return

        # --- HATCH AREA PREVIEW ---
        if self.active_tool == "hatcharea" and len(self.temp_points) >= 1:
            ha_style = self._get_draw_style('hatcharea')
            if len(self.temp_points) >= 2:
                self._update_continuous_preview(
                    color=ha_style['color'],
                    width=ha_style['width'],
                    line_style=ha_style['style'],
                )
            self._update_cursor_preview(
                color=ha_style['color'],
                width=ha_style['width'],
                line_style='dotted',
                close_loop=True,
            )
            self.app.vtk_widget.render()
            return

        # --- MOVE SELECTED ACTOR ---
        if getattr(self, "move_mode", False) and \
        getattr(self, "selected", None) and \
        self.left_down and not self._is_panning:
            pos = self._get_mouse_world()
            if not hasattr(self, "_last_pos") or self._last_pos is None:
                self._last_pos = pos
            delta = np.array(pos) - np.array(self._last_pos)
            self._last_pos = pos
            self._translate_selected(delta)
            self.app.vtk_widget.render()
            return


    def _on_left_press(self, obj, evt):
        """Handle left mouse press - add vertices continuously."""
        if not self.enabled:
            return
        if getattr(self, "active_tool", None) in ("rotatepivot", "cornerrotate"):
            self._consume_vtk_event(obj)
            return

        # ══════════════════════════════════════════════════════════════════
        # TEXT TOOL: Placement is handled entirely by _start_text_label's
        # own high-priority observer (_finalize_text_drag at priority 2.0).
        # We must NOT consume the event here — doing so would prevent
        # middle-mouse pan from working because VTK's style never sees
        # the button state change.
        # ══════════════════════════════════════════════════════════════════
        if self.active_tool == "text":
            return

        self._consume_vtk_event(obj)
        self.left_down = True

        # Clear shift block on regular click (without shift)
        if not self.interactor.GetShiftKey():
            self._shift_blocked = False

        pos = self._get_mouse_world()
        x, y = self.interactor.GetEventPosition()
        world_pos = self._display_to_world(x, y)

        # Shift+Click vertex drag
        if self.interactor.GetShiftKey() and not self.active_tool and not getattr(self, '_shift_blocked', False):
            nearest_vertex, nearest_drawing, vertex_idx = \
                self._find_nearest_vertex_at_position(x, y, tolerance=15.0)
            if nearest_vertex is not None and nearest_drawing:
                self._save_state()
                self.dragging_vertex = {
                    'drawing': nearest_drawing,
                    'vertex_index': vertex_idx,
                    'original_pos': nearest_vertex
                }
                if self.vertex_drag_marker:
                    self._remove_actor_from_overlay(self.vertex_drag_marker)
                self.vertex_drag_marker = self._add_endpoint_sphere(
                    nearest_vertex, color=(1, 1, 0), radius=0.12
                )
                self.app.vtk_widget.render()
                return

        # Vertex insertion tool
        if self.active_tool == "vertex":
            render_window = self.app.vtk_widget.GetRenderWindow()
            window_height = render_window.GetSize()[1]
            qt_y = window_height - y
            picked_drawing, insertion_point = self._find_line_at_position(x, qt_y)
            if picked_drawing and insertion_point is not None:
                self._insert_vertex_at_point(picked_drawing, insertion_point)
            else:
                print("⚪ No line found at cursor position")
            return

        if self.active_tool == "deletevertex":
            self._select_vertex_for_delete_at_position(x, y)
            return

        # ============ HATCH AREA ============
        if self.active_tool == "hatcharea":
            hatch_method = getattr(self, '_hatch_method_id', 2)

            # ── Block-select mode (method 6): find SNT/DXF/PRJ block at click ──
            if hatch_method == 6:
                # Ray-plane projection: near+far → intersect at Z=0 plane.
                # NOT vtkWorldPointPicker (picks nearest prop, wrong in empty space).
                world_for_block = None
                try:
                    # Near plane (z=0 in display)
                    self.renderer.SetDisplayPoint(float(x), float(y), 0.0)
                    self.renderer.DisplayToWorld()
                    near = self.renderer.GetWorldPoint()

                    # Far plane (z=1 in display)
                    self.renderer.SetDisplayPoint(float(x), float(y), 1.0)
                    self.renderer.DisplayToWorld()
                    far = self.renderer.GetWorldPoint()

                    if (near is not None and far is not None
                            and len(near) >= 4 and len(far) >= 4
                            and abs(near[3]) > 1e-9 and abs(far[3]) > 1e-9):
                        # Dehomogenize
                        n = np.array([near[0]/near[3], near[1]/near[3], near[2]/near[3]])
                        f = np.array([far[0]/far[3], far[1]/far[3], far[2]/far[3]])
                        direction = f - n
                        dz = direction[2]
                        if abs(dz) > 1e-10:
                            t = -n[2] / dz  # intersect Z=0 plane
                            world_for_block = n + t * direction
                        else:
                            world_for_block = n  # parallel to plane
                except Exception:
                    pass
                if world_for_block is None:
                    world_for_block = pos  # last resort
                block_hit = self._hatch_find_block_at(world_for_block)
                if block_hit is not None:
                    label = block_hit.get('label', '?')
                    area_val = block_hit.get('area', 0)
                    print(f"▦ Hatch block-select: found '{label}' area={area_val:.2f}")
                    if hasattr(self, '_hatch_dialog') and self._hatch_dialog:
                        try:
                            self._hatch_dialog.set_status(
                                f"Block '{label}' selected — applying hatch…"
                            )
                        except Exception:
                            pass
                    self.temp_hatch_block_label = label
                    self.temp_points = list(block_hit['points'])
                    self._finalize_hatcharea()
                    return
                else:
                    if hasattr(self, '_hatch_dialog') and self._hatch_dialog:
                        try:
                            self._hatch_dialog.set_status(
                                "No SNT/DXF/PRJ block found at click location.\n"
                                "Click inside a block boundary to hatch it."
                            )
                        except Exception:
                            pass
                    print("▦ Hatch block-select: no block found at click position")
                    return

            # ── Element-click mode: if click is inside an existing drawing, hatch it ──
            hit = self._hatch_find_element_at(pos)
            if hit is not None:
                coords = hit.get('coords', [])
                print(f"▦ Hatch element-click: found '{hit.get('type','?')}' with {len(coords)} pts")
                if hasattr(self, '_hatch_dialog') and self._hatch_dialog:
                    try: self._hatch_dialog.set_status("Element selected — applying hatch…")
                    except Exception: pass
                self.temp_points = list(coords)
                self._finalize_hatcharea()
                return

            # ── Manual boundary-point mode: click adds a vertex ──
            ha_style = self._get_draw_style('hatcharea')
            self._push_temp_vertex_history()
            self.temp_points.append(pos)
            n = len(self.temp_points)
            print(f"▦ Hatch boundary point {n}: {pos}")
            if not hasattr(self, '_hatch_vertex_markers'):
                self._hatch_vertex_markers = []
            sphere_color = (0, 1, 0) if n == 1 else (1, 1, 0)
            try:
                sphere = self._add_endpoint_sphere(pos, color=sphere_color)
                if sphere is not None:
                    self._hatch_vertex_markers.append(sphere)
            except Exception as e:
                print(f"⚠️ Hatch sphere error: {e}")
            if n >= 2:
                self._update_continuous_preview(
                    color=ha_style['color'],
                    width=ha_style['width'],
                    line_style=ha_style['style'],
                )
                self._update_cursor_preview(
                    color=ha_style['color'],
                    width=ha_style['width'],
                    line_style='dotted',
                    close_loop=True,
                    current_world=pos,
                )
            self.app.vtk_widget.render()
            if hasattr(self, '_hatch_dialog') and self._hatch_dialog:
                try:
                    self._hatch_dialog.set_status(
                        f"{n} point{'s' if n != 1 else ''} placed.\nRight-click to apply hatch.\n"
                        "(Or click inside an existing shape to hatch it.)"
                    )
                except Exception:
                    pass
            return

        # Selection mode (no active tool)
        if not self.active_tool:
            actor = self._pick_actor(x, y)
            if actor:
                self._select_actor(actor)
            return

        # ============ FREEHAND ============
        if self.active_tool == "freehand":
            if not self.temp_points:
                self.temp_points = [world_pos]
                print("🖊️ Freehand STARTED")
            else:
                self.temp_points.append(world_pos)
                if len(self.temp_points) >= 2:
                    fh_style = self._get_draw_style('freehand')
                    self._update_freehand_preview_world(
                        color=fh_style['color'],
                        width=fh_style['width'],
                        line_style=fh_style['style'],
                    )
                self.app.vtk_widget.render()
                print("🖊️ Freehand RESUMED")
            self.is_drawing_freehand = True
            return

        # ============ SMARTLINE ============
        if self.active_tool == "smartline":
            sl_style = self._get_draw_style('smartline')
            self._push_temp_vertex_history()

            if len(self.temp_points) == 0:
                snap_target = None
                snap_distance = float('inf')
                world_tol = self._screen_to_world_tolerance(15)
                for drawing in self.drawings:
                    if 'coords' in drawing:
                        for vertex in drawing['coords']:
                            vertex_arr = np.array(vertex)
                            dx = float(vertex_arr[0]) - float(pos[0])
                            dy = float(vertex_arr[1]) - float(pos[1])
                            dist = np.sqrt(dx * dx + dy * dy)
                            if dist < world_tol and dist < snap_distance:
                                snap_distance = dist
                                snap_target = vertex_arr
                if snap_target is not None:
                    pos = snap_target

            self.temp_points.append(pos)

            if not hasattr(self, '_smartline_vertex_markers'):
                self._smartline_vertex_markers = []

            if len(self.temp_points) == 1:
                sphere = self._add_endpoint_sphere(pos, color=(0, 1, 0), radius=0.20)
            else:
                sphere = self._add_endpoint_sphere(pos, color=(1, 1, 0), radius=0.20)
                if len(self._smartline_vertex_markers) > 1:
                    self._smartline_vertex_markers[-1].GetProperty().SetColor(1, 1, 0)
            self._smartline_vertex_markers.append(sphere)

            if len(self.temp_points) >= 2:
                self._update_continuous_preview(
                    color=sl_style['color'],
                    width=sl_style['width'],
                    line_style=sl_style['style'],
                )
                self._update_cursor_preview(
                    color=sl_style['color'],
                    width=sl_style['width'],
                    line_style=sl_style['style'],
                    current_world=pos,
                )
                self.app.vtk_widget.render()
            return

        # ============ LINE ============
        if self.active_tool == "line":
            ln_style = self._get_draw_style('line')
            self._push_temp_vertex_history()

            self.temp_points.append(pos)

            if not hasattr(self, '_line_vertex_markers'):
                self._line_vertex_markers = []

            if len(self.temp_points) == 1:
                sphere = self._add_endpoint_sphere(pos, color=(0, 1, 0), radius=0.05)
                sphere.GetProperty().SetOpacity(0.8)
            else:
                sphere = self._add_endpoint_sphere(pos, color=(1, 1, 0), radius=0.05)
                sphere.GetProperty().SetOpacity(0.8)
                if len(self._line_vertex_markers) > 1:
                    self._line_vertex_markers[-1].GetProperty().SetColor(1, 1, 0)
            self._line_vertex_markers.append(sphere)

            if len(self.temp_points) >= 2:
                self._update_continuous_preview(
                    color=ln_style['color'],
                    width=ln_style['width'],
                    line_style=ln_style['style'],
                )
                self._update_cursor_preview(
                    color=ln_style['color'],
                    width=ln_style['width'],
                    line_style=ln_style['style'],
                    current_world=pos,
                )
                self.app.vtk_widget.render()
            return

        # ============ RECTANGLE ============
        if self.active_tool == "rectangle":
            if not self.temp_points:
                # First click: store the anchor corner
                self.temp_points = [pos]
                print(f"🟥 Rectangle: first corner set at {pos}")
                return
            if len(self.temp_points) == 1:
                # Second click: finalize
                self.temp_points.append(pos)
                self._remove_preview_actor_2d('_rectangle_preview_actor')
                self._finalize_rectangle()
                return

        # ============ CIRCLE ============
        if self.active_tool == "circle":
            if not self.temp_points:
                # First click: store the center
                self.temp_points = [pos]
                print(f"⭕ Circle: center set at {pos}")
                return
            if len(self.temp_points) == 1:
                # Second click: finalize
                self.temp_points.append(pos)
                self._finalize_circle()
                return

        # ============ POLYLINE ============
        if self.active_tool == "polyline":
            pl_style = self._get_draw_style('polyline')
            self._push_temp_vertex_history()

            # Shift+Click with no in-progress points: reopen the nearest polyline for continued drawing
            if self.interactor.GetShiftKey() and not self.temp_points and not getattr(self, '_shift_blocked', False):
                world_tol = self._screen_to_world_tolerance(20)
                best_dist = float('inf')
                found_drawing = None
                for d in self.drawings:
                    if d.get("type") != "polyline":
                        continue
                    for pt in d.get("coords") or []:
                        dx = float(np.array(pt)[0]) - float(np.array(pos)[0])
                        dy = float(np.array(pt)[1]) - float(np.array(pos)[1])
                        dist = (dx * dx + dy * dy) ** 0.5
                        if dist < best_dist:
                            best_dist = dist
                            found_drawing = d
                # Always consume Shift+Click — never fall through to normal vertex placement
                if found_drawing is None or best_dist > world_tol:
                    return
                if found_drawing is not None and best_dist <= world_tol:
                    self._save_state()
                    self.clear_coordinate_labels()
                    self._clear_temp_vertex_history()
                    # Strip the auto-added closing duplicate vertex
                    coords = list(found_drawing["coords"])
                    if len(coords) >= 2 and np.allclose(np.array(coords[0], dtype=float), np.array(coords[-1], dtype=float)):
                        coords = coords[:-1]
                    # Remove the finalized actor and all its markers from the scene
                    try:
                        self._remove_actor_from_overlay(found_drawing["actor"])
                    except Exception:
                        pass
                    if "fill_actor" in found_drawing:
                        try:
                            self._remove_actor_from_overlay(found_drawing["fill_actor"])
                        except Exception:
                            pass
                    for m in found_drawing.get("vertex_markers", []):
                        try:
                            m.SetVisibility(False)
                            self._remove_actor_from_overlay(m)
                        except Exception:
                            pass
                    # Remove by identity to avoid numpy array comparison issues
                    for _i, _d in enumerate(self.drawings):
                        if _d is found_drawing:
                            self.drawings.pop(_i)
                            break
                    # Load vertices back into in-progress state
                    self.temp_points = coords
                    self._polyline_vertex_markers = []
                    for i, pt in enumerate(self.temp_points):
                        color = (0, 1, 0) if i == 0 else (1, 1, 0)
                        sphere = self._add_endpoint_sphere(pt, color=color, radius=0.05)
                        sphere.GetProperty().SetOpacity(0.8)
                        self._polyline_vertex_markers.append(sphere)
                    if len(self.temp_points) >= 2:
                        self._update_continuous_preview(
                            color=pl_style['color'],
                            width=pl_style['width'],
                            line_style=pl_style['style'],
                        )
                    self.app.vtk_widget.render()
                    return

            self.temp_points.append(pos)

            if not hasattr(self, '_polyline_vertex_markers'):
                self._polyline_vertex_markers = []

            if len(self.temp_points) == 1:
                sphere = self._add_endpoint_sphere(pos, color=(0, 1, 0), radius=0.05)
                sphere.GetProperty().SetOpacity(0.8)
            else:
                sphere = self._add_endpoint_sphere(pos, color=(1, 1, 0), radius=0.05)
                sphere.GetProperty().SetOpacity(0.8)
                if len(self._polyline_vertex_markers) > 1:
                    self._polyline_vertex_markers[-1].GetProperty().SetColor(1, 1, 0)
            self._polyline_vertex_markers.append(sphere)

            if len(self.temp_points) >= 2:
                self._update_continuous_preview(
                    color=pl_style['color'],
                    width=pl_style['width'],
                    line_style=pl_style['style'],
                )
                self._update_cursor_preview(
                    color=pl_style['color'],
                    width=pl_style['width'],
                    line_style='dotted',
                    close_loop=True,
                    current_world=pos,
                )
                self.app.vtk_widget.render()
            return


    def _reset_pan_state(self):
        """Emergency reset for stuck pan state."""
        self.left_down = False
        self.middle_down = False
        self._is_panning = False
        self._pan_start_pos = None
        self._last_pos = None
        self._blocked_fake_middle = False

        if hasattr(self, "_middle_button_down"):
            self._middle_button_down = False

        try:
            rw = self.interactor.GetRenderWindow()
            if rw is not None:
                rw.SetDesiredUpdateRate(0.001)
        except Exception:
            pass

        print("Pan state forcibly reset")

    def _on_middle_release(self, obj, evt):
        # Physical middle button released:
        # Move Vertex must resume immediately, but do not clear _is_panning here.
        # Let the normal release branches below finish pan cleanup.
        self._middle_button_down = False
        self._move_vertex_pan_block_until = 0.0
        self._ignore_move_vertex_finish_until = 0.0
        """Stop panning IMMEDIATELY when button is released."""
        # Skip when classification tool is active — classification handles
        # its own pan release (_on_safe_pan_stop) and consuming the event here
        # would prevent the style from ever receiving MiddleButtonReleaseEvent,
        # leaving _is_panning stuck at True on the classification interactor.
        if getattr(self.app, "active_classify_tool", None) is not None:
            return
        if getattr(self, "_blocked_fake_middle", False):
            print("Fake middle release ignored after zoom combo")

            self._blocked_fake_middle = False
            self.middle_down = False
            self._is_panning = False
            self._pan_start_pos = None
            self._last_pos = None
            self._middle_button_down = False
            self._move_vertex_pan_block_until = 0.0
            self._ignore_move_vertex_finish_until = 0.0

            try:
                if obj is not None and hasattr(obj, "AbortFlagOn"):
                    obj.AbortFlagOn()
            except Exception:
                pass

            return

        if not self._is_panning:
            self._middle_button_down = False
            self.middle_down = False
            self._is_panning = False
            self._move_vertex_pan_block_until = 0.0
            self._ignore_move_vertex_finish_until = 0.0
            self._pan_start_pos = None
            self._last_pos = None
            return

        print("🛑 Middle button RELEASED - stopping pan NOW")

        self.middle_down = False
        self._is_panning = False
        self._pan_start_pos = None
        self._last_pos = None

        if hasattr(self, "_middle_button_down"):
            self._middle_button_down = False

        self._pan_press_monotonic = 0.0
        self._pan_button_miss_count = 0

        # ── Restore still-render quality after pan ends.
        # final still frame. Force one render so the user sees crisp result.
        try:
            rw = self.interactor.GetRenderWindow()
            if rw is not None:
                rw.SetDesiredUpdateRate(0.001)  # still render = full quality
        except Exception:
            pass

        self._finish_manual_pan_rendering()

        # ✅ Refresh preview lines after pan moved camera
        if self.temp_points and self.active_tool:
            try:
                from PySide6.QtCore import QTimer
            except ImportError:
                from PyQt5.QtCore import QTimer
            QTimer.singleShot(0, self._deferred_preview_update)

        try:
            if obj is not None and hasattr(obj, "AbortFlagOn"):
                obj.AbortFlagOn()
        except Exception:
            pass

        print("✅ Pan stopped")
            
    def debug_pan_state(self):
        """Debug helper to check pan state - call from console if needed"""
        print(f"🔍 Pan State Debug:")
        print(f"   middle_down: {self.middle_down}")
        print(f"   _is_panning: {self._is_panning}")
        print(f"   _pan_start_pos: {self._pan_start_pos}")
        print(f"   active_tool: {self.active_tool}")
        
        # Try auto-fix if stuck
        if self._is_panning and not self.middle_down:
            print("   ⚠️ STUCK STATE DETECTED - Auto-fixing...")
            self._reset_pan_state()

    def _on_mouse_move(self, obj, evt):
        """Show rubber-band preview line as you move mouse."""
        if not self.enabled:
            if not getattr(self, "active_tool", None) or not getattr(self, "is_drawing_freehand", False):
                return
            return

        # ── PANNING takes absolute priority — never block middle-mouse pan ──────
        if self._is_panning:
            # Don't consume the event during pan — let VTK style handle it
            if self._is_real_middle_button_down():
                self._pan_button_miss_count = 0
            else:
                # Qt can briefly report no buttons while translating a VTK
                # press. Require two misses outside a short grace period before
                # repairing a genuinely lost release.
                self._pan_button_miss_count += 1
                press_age = time.monotonic() - float(
                    getattr(self, "_pan_press_monotonic", 0.0) or 0.0
                )
                if press_age >= 0.12 and self._pan_button_miss_count >= 2:
                    print("Manual pan auto-stopped - middle release was missed")

                    self.middle_down = False
                    self._is_panning = False
                    self._pan_start_pos = None
                    self._last_pos = None
                    self._blocked_fake_middle = False
                    self._pan_press_monotonic = 0.0
                    self._pan_button_miss_count = 0

                    if hasattr(self, "_middle_button_down"):
                        self._middle_button_down = False

                    try:
                        rw = self.interactor.GetRenderWindow()
                        if rw is not None:
                            rw.SetDesiredUpdateRate(0.001)
                    except Exception:
                        pass

                    self._finish_manual_pan_rendering()

                    try:
                        if obj is not None and hasattr(obj, "AbortFlagOn"):
                            obj.AbortFlagOn()
                    except Exception:
                        pass
                    return

            if not self.middle_down:
                self._is_panning = False
                self._pan_start_pos = None
                self._last_pos = None
                self._pan_press_monotonic = 0.0
                self._pan_button_miss_count = 0
                self._finish_manual_pan_rendering()
                return

            current_pos = self.interactor.GetEventPosition()

            if self._last_pos is None:
                self._last_pos = current_pos
                return

            dx = current_pos[0] - self._last_pos[0]
            dy = current_pos[1] - self._last_pos[1]
            self._last_pos = current_pos

            try:
                camera = self.renderer.GetActiveCamera()
                rw = self.interactor.GetRenderWindow()
                win_w, win_h = rw.GetSize()

                if win_h <= 0:
                    win_h = 1

                world_per_pixel = (2.0 * camera.GetParallelScale()) / float(win_h)

                move_x = -dx * world_per_pixel
                move_y = -dy * world_per_pixel

                pos = camera.GetPosition()
                focal = camera.GetFocalPoint()

                camera.SetPosition(
                    pos[0] + move_x,
                    pos[1] + move_y,
                    pos[2],
                )
                camera.SetFocalPoint(
                    focal[0] + move_x,
                    focal[1] + move_y,
                    focal[2],
                )

                self._request_manual_pan_render()
            except Exception as e:
                print(f"Manual pan failed: {e}")

            try:
                if obj is not None and hasattr(obj, "AbortFlagOn"):
                    obj.AbortFlagOn()
            except Exception:
                pass

            return

        # Only consume event for non-pan, non-text active tools
        if self.active_tool and self.active_tool != "text":
            self._consume_vtk_event(obj)
        elif self.dragging_vertex:
            self._consume_vtk_event(obj)

        # Vertex dragging
        if self.dragging_vertex:
            new_pos = self._get_mouse_world()
            drawing = self.dragging_vertex['drawing']
            vertex_idx = self.dragging_vertex['vertex_index']
            coords = drawing.get('coords')
            coord_len = 0 if coords is None else len(coords)
            if coords is None or coord_len == 0 or vertex_idx < 0 or vertex_idx >= coord_len:
                print(f"⚠️ Vertex drag index {vertex_idx} out of range (coords len={coord_len}) — aborting drag")
                self.dragging_vertex = None
                if self.vertex_drag_marker:
                    self._remove_actor_from_overlay(self.vertex_drag_marker)
                    self.vertex_drag_marker = None
                self.app.vtk_widget.render()
                return
            coords[vertex_idx] = tuple(new_pos)
            if self.vertex_drag_marker:
                self.vertex_drag_marker.SetPosition(*new_pos)
            self._rebuild_drawing_actor(drawing)
            if self.selected_drawing is drawing:
                self.clear_coordinate_labels()
                self.show_vertex_coordinates(coords)
            self.app.vtk_widget.render()
            return

        # --- TEXT TOOL: text follows mouse, panning must still work ---
        if self.active_tool == "text" and getattr(self, "_placing_text", False):
            # Update text position to follow mouse WITHOUT consuming event
            # This allows middle-mouse pan to still function
            try:
                world_pos = self._get_mouse_world_no_snap()
                self._update_text_preview_position(world_pos)
                self.app.vtk_widget.render()
            except Exception:
                pass
            return

        # --- FREEHAND CONTINUOUS DRAWING ---
        if self.active_tool == "freehand" and getattr(self, "left_down", False) and getattr(self, "is_drawing_freehand", False):
            world_pos = self._get_mouse_world_no_snap()
            dist_px = 999
            if self.temp_points:
                dist_px = self._world_to_screen_distance(world_pos, self.temp_points[-1])
            if len(self.temp_points) == 0 or dist_px > 2.0:
                self.temp_points.append(world_pos)
                if len(self.temp_points) >= 2:
                    fh_style = self._get_draw_style('freehand')
                    self._update_freehand_preview_world(
                        color=fh_style['color'],
                        width=fh_style['width'],
                        line_style=fh_style['style'],
                    )
                self.app.vtk_widget.render()
            return

        # --- SMARTLINE ---
        if self.active_tool == "smartline" and len(self.temp_points) >= 1:
            sl_style = self._get_draw_style('smartline')
            if self.snap_enabled:
                raw_pos = self._get_mouse_world_no_snap()
                self._snap_point(raw_pos)
            if len(self.temp_points) >= 2:
                self._update_continuous_preview(
                    color=sl_style['color'],
                    width=sl_style['width'],
                    line_style=sl_style['style'],
                )
            self._update_cursor_preview(
                color=sl_style['color'],
                width=sl_style['width'],
                line_style=sl_style['style'],
            )
            self.app.vtk_widget.render()
            return

        # --- LINE ---
        if self.active_tool == "line" and len(self.temp_points) >= 1:
            ln_style = self._get_draw_style('line')
            raw_pos = self._get_mouse_world_no_snap()
            self._snap_point(raw_pos)
            if len(self.temp_points) >= 2:
                self._update_continuous_preview(
                    color=ln_style['color'],
                    width=ln_style['width'],
                    line_style=ln_style['style'],
                )
            self._update_cursor_preview(
                color=ln_style['color'],
                width=ln_style['width'],
                line_style=ln_style['style'],
            )
            self.app.vtk_widget.render()
            return

        # --- RECTANGLE PREVIEW ---
        if self.active_tool == "rectangle" and len(self.temp_points) == 1:
            rc_style = self._get_draw_style('rectangle')
            self._update_rectangle_preview(
                color=rc_style['color'],
                width=rc_style['width'],
                line_style=rc_style['style'],
            )
            self.app.vtk_widget.render()
            return

        # --- CIRCLE PREVIEW ---
        if self.active_tool == "circle" and len(self.temp_points) == 1:
            ci_style = self._get_draw_style('circle')
            self._update_circle_preview_world(
                color=ci_style['color'],
                width=ci_style['width'],
                line_style=ci_style['style'],
            )
            self.app.vtk_widget.render()
            return

        # --- POLYLINE ---
        if self.active_tool == "polyline" and len(self.temp_points) >= 1:
            pl_style = self._get_draw_style('polyline')
            if len(self.temp_points) >= 2:
                self._update_continuous_preview(
                    color=pl_style['color'],
                    width=pl_style['width'],
                    line_style=pl_style['style'],
                )
            self._update_cursor_preview(
                color=pl_style['color'],
                width=pl_style['width'],
                line_style='dotted',
                close_loop=True,
            )
            self.app.vtk_widget.render()
            return

        # --- HATCH AREA PREVIEW ---
        if self.active_tool == "hatcharea" and len(self.temp_points) >= 1:
            ha_style = self._get_draw_style('hatcharea')
            if len(self.temp_points) >= 2:
                self._update_continuous_preview(
                    color=ha_style['color'],
                    width=ha_style['width'],
                    line_style=ha_style['style'],
                )
            self._update_cursor_preview(
                color=ha_style['color'],
                width=ha_style['width'],
                line_style='dotted',
                close_loop=True,
            )
            self.app.vtk_widget.render()
            return

        # --- MOVE SELECTED ACTOR ---
        if getattr(self, "move_mode", False) and getattr(self, "selected", None) and self.left_down and not self._is_panning:
            pos = self._get_mouse_world()
            if not hasattr(self, "_last_pos") or self._last_pos is None:
                self._last_pos = pos
            delta = np.array(pos) - np.array(self._last_pos)
            self._last_pos = pos
            self._translate_selected(delta)
            self.app.vtk_widget.render()
            return



    def _on_left_release(self, obj, evt):
        """Finalize freehand and deactivate cleanly."""
        if getattr(self, "_is_panning", False):
            self._is_panning = False
            self.middle_down = False
            self._pan_start_pos = None
            self._consume_vtk_event(obj)
            if self.interactor:
                self.interactor.InvokeEvent("MiddleButtonReleaseEvent")
            self.app.vtk_widget.render()
            return

        if not self.enabled:
            return
        
        # ✅ NEW: Finish vertex dragging from INSERT mode
        self._consume_vtk_event(obj)
        if self.dragging_vertex and self.active_tool == "vertex":
            drawing = self.dragging_vertex['drawing']
            vertex_idx = self.dragging_vertex['vertex_index']
            coords = drawing.get('coords')
            if not coords or vertex_idx < 0 or vertex_idx >= len(coords):
                if self.vertex_drag_marker:
                    self._remove_actor_from_overlay(self.vertex_drag_marker)
                    self.vertex_drag_marker = None
                self.dragging_vertex = None
                self.left_down = False
                return
            new_pos = coords[vertex_idx]
            
            print(f"✅ Vertex {vertex_idx} placed at {new_pos} (auto-drag mode)")
            
            if self.vertex_drag_marker:
                self._remove_actor_from_overlay(self.vertex_drag_marker)
                self.vertex_drag_marker = None
            
            self._rebuild_drawing_actor(drawing)
            self.dragging_vertex = None
            self.app.vtk_widget.render()
            
            self.left_down = False
            self._last_pos = None 
            return
        
        # ✅ Existing: Finish vertex dragging from SHIFT+CLICK mode
        if self.dragging_vertex:
            drawing = self.dragging_vertex['drawing']
            vertex_idx = self.dragging_vertex['vertex_index']
            coords = drawing.get('coords')
            if not coords or vertex_idx < 0 or vertex_idx >= len(coords):
                print(f"⚠️ Drag vertex index {vertex_idx} out of range — aborting")
                if self.vertex_drag_marker:
                    self._remove_actor_from_overlay(self.vertex_drag_marker)
                    self.vertex_drag_marker = None
                self.dragging_vertex = None
                self.left_down = False
                return
            old_pos = self.dragging_vertex.get('original_pos', coords[vertex_idx])
            new_pos = coords[vertex_idx]
            
            print(f"✅ Vertex {vertex_idx} moved to {new_pos}")
            
            if self.vertex_drag_marker:
                self._remove_actor_from_overlay(self.vertex_drag_marker)
                self.vertex_drag_marker = None
            
            self._rebuild_drawing_actor(drawing)
            self._update_shared_vertices(old_pos, new_pos)
            self.dragging_vertex = None
            self.app.vtk_widget.render()
            
            self.left_down = False
            return
            
        # Reset left mouse button state
        self.left_down = False

        # 3. FREEHAND: PAUSE LOGIC (Replaced the old Finalization logic)
        # ======================= FREEHAND PAUSE (DO NOT FINISH) =======================
        if self.active_tool == "freehand" and getattr(self, "is_drawing_freehand", False):
            # Just pause recording. Do NOT clear points. Do NOT finalize.
            self.is_drawing_freehand = False
            print("⏸️ Freehand PAUSED. (Left-drag to continue, Right-click to finish)")
            return

        # -------------------- Other Tools (unchanged) --------------------
        if self.active_tool == "rectangle" and len(self.temp_points) == 2:
            self._finalize_rectangle()
            return
        if self.active_tool == "circle" and len(self.temp_points) == 2:
            self._finalize_circle()
            return
        if self.active_tool == "polygon" and len(self.temp_points) >= 3:
            print("ℹ️ Polygon still active (awaiting right-click to finalize).")
            return
        
    def test_pan_functionality(self):
        """
        Quick test to verify pan works.
        Call this after activating a drawing tool.
        """
        print("\n" + "="*60)
        print("🧪 PAN FUNCTIONALITY TEST")
        print("="*60)
        print(f"Active tool: {self.active_tool}")
        print(f"Interactor style: {self.interactor.GetInteractorStyle()}")
        print(f"Ctrl key check works: {self.interactor.GetControlKey()}")
        print("\n📋 INSTRUCTIONS:")
        print("1. Hold Ctrl key")
        print("2. Click and drag with left mouse")
        print("3. View should pan without adding points")
        print("4. Release Ctrl")
        print("5. Click again - point should be added")
    
   

    def _handle_right_click_selection(self):
        """
        Robust Selection Handler.
        """
        x, y = self.interactor.GetEventPosition()
        shift_held = self.interactor.GetShiftKey()

        picked_drawing = None

        # --- STEP 1: Try Hardware Picking (Fast, good for text/labels) ---
        # A. Hardware picker
        actor = self._pick_actor(x, y)
        if actor:
            for d in self.drawings:
                if self._is_hidden_draw_tool_text_drawing(d):
                    continue
                if d.get("actor") is actor:
                    picked_drawing = d
                    break
        
        # --- STEP 2: Force Math Picker if Hardware failed ---
        if not picked_drawing:
            picked_drawing = self._get_drawing_under_cursor(x, y, tolerance=25.0)

        # --- STEP 3: Apply Selection ---
        if picked_drawing:
            # TEXT EDITING (Selection only, Edit is in right-click Context Menu)
            if picked_drawing["type"] == "text":
                if self._is_hidden_draw_tool_text_drawing(picked_drawing):
                    return

                self.clear_coordinate_labels()
                self._unhighlight_all_lines()
                self.multi_selected = []
                self.selected_drawing = picked_drawing
                self.renderer.Modified()
                self.app.vtk_widget.render()
                return


            # SHAPE SELECTION
            if self.selected_drawing is picked_drawing and not shift_held:
                # Toggle Off
                self._unhighlight_line(picked_drawing)
                self.clear_coordinate_labels()
                self.selected_drawing = None
            elif shift_held:
                # Multi-Select
                # ✅ FIX: Use identity check to avoid numpy array comparison
                if not any(d is picked_drawing for d in self.multi_selected):
                    self.multi_selected.append(picked_drawing)
                    self._highlight_line(picked_drawing)
                else:
                    # Remove using identity check
                    self.multi_selected = [d for d in self.multi_selected if d is not picked_drawing]
                    self._unhighlight_line(picked_drawing)
                self.selected_drawing = None
                self._expand_legacy_selection_to_polygons()
            else:
                # Single Select
                self.clear_coordinate_labels()
                self._unhighlight_all_lines()
                self.multi_selected = []
                self.selected_drawing = picked_drawing
                self._highlight_line(picked_drawing)
                
                # Show vertices if available
                if "coords" in picked_drawing:
                    self.show_vertex_coordinates(picked_drawing["coords"])
            
            self.renderer.Modified()
            self.app.vtk_widget.render()
            return

        # --- STEP 4: Deselect All (Clicked Empty Space) ---
        self.clear_coordinate_labels()
        self.selected_drawing = None
        self._unhighlight_all_lines()
        self.app.vtk_widget.render()
        

    def _on_key_press(self, obj, evt):
        """Handle key presses: delete, move, copy/paste, cancel, etc."""
        if not self.enabled:
            return

        key = self.interactor.GetKeySym().lower()
        
        # --- UNDO (Ctrl+Z) ---
        if key == "z" and self.interactor.GetControlKey():
            if getattr(self, "active_tool", None):
                self.undo()
                return

        # --- REDO (Ctrl+Y) ---
        elif key == "y" and self.interactor.GetControlKey():
            if getattr(self, "active_tool", None):
                self.redo()
                return

        # --- SHIFT+ESC: Exit permanent modes or active tools ---
        if key == "escape" and self.interactor.GetShiftKey():
            if self._deactivate_active_tool_keep_drawings():
                return

            # No active digitize tool: treat Shift+ESC as selection clear only.
            self.clear_coordinate_labels()
            self.selected_drawing = None
            self.selected_vertex_idx = None
            if hasattr(self, "multi_selected"):
                self.multi_selected = []
            if hasattr(self, "_vertex_highlight") and self._vertex_highlight:
                self._remove_actor_from_overlay(self._vertex_highlight)
                self._vertex_highlight = None
            self._unhighlight_all_lines()
            print("⚪ All selections cleared")
            self.app.vtk_widget.render()
            return

        # --- S KEY: Toggle snapping ---
        elif key == "s":
            self.snap_enabled = not self.snap_enabled
            state = "ON" if self.snap_enabled else "OFF"
            print(f"🧲 Snapping: {state}")

        # --- DELETE / BACKSPACE ---
        elif key in ("delete", "backspace"):
            self._save_state()
            pos = self._get_mouse_world()

            # === TEXT LABEL DELETE (single selection) ===
            if getattr(self, "selected_drawing", None) and self.selected_drawing.get("type") == "text":
                print(f"🗑️ Deleting text label: {self.selected_drawing.get('text', 'N/A')}")
                self._remove_drawing(self.selected_drawing)
                self.selected_drawing = None
                self.renderer.Modified()
                self.app.vtk_widget.render()
                print("✅ Text label deleted")
                return

            # === LINE SEGMENT DELETE ===
            if getattr(self, "selected_drawing", None) and self.selected_drawing.get("type") == "line_segment":
                d = self.selected_drawing
                print(f"🗑️ Deleting selected line segment: {d['coords']}")
                self._remove_drawing(d)
                self.selected_drawing = None
                self.clear_coordinate_labels()
                self.renderer.Modified()
                self.app.vtk_widget.render()
                print("✅ Line segment deleted")
                return

            # === FREEHAND DELETE ===
            if getattr(self, "selected_drawing", None) and self.selected_drawing.get("type") == "freehand":
                d = self.selected_drawing
                print(f"🗑️ Deleting selected freehand drawing ({len(d['coords'])} points)")
                self._remove_drawing(d)
                self.selected_drawing = None
                self.clear_coordinate_labels()
                self.renderer.Modified()
                self.app.vtk_widget.render()
                print("✅ Freehand drawing deleted")
                return

            # === POLYLINE DELETE ===
            if getattr(self, "selected_drawing", None) and self.selected_drawing.get("type") == "polyline":
                d = self.selected_drawing
                print(f"🗑️ Deleting selected polyline drawing ({len(d['coords'])} points)")
                self._remove_drawing(d)
                self.selected_drawing = None
                self.clear_coordinate_labels()
                self.renderer.Modified()
                self.app.vtk_widget.render()
                print("✅ Polyline drawing deleted")
                return

            # === MULTI-SELECTION DELETE ===
            if getattr(self, "multi_selected", []):
                print(f"🗑️ Multi-delete: {len(self.multi_selected)} selected drawings")

                for d in list(self.multi_selected):
                    if "coords" not in d:
                        continue

                    # Freehand: remove directly
                    if d["type"] == "freehand":
                        print("🗑️ Removing freehand from multi-selection")
                        self._remove_drawing(d)
                        continue

                    coords = d["coords"]
                    idx, dist = self._find_nearest_vertex(pos, coords, threshold=5.0)

                    if idx is not None:
                        print(f"✅ Removing vertex {idx} ({dist:.2f}) from {d['type']}")
                        del coords[idx]

                        if len(coords) < 2:
                            self._remove_drawing(d)
                            continue

                        # Rebuild actor
                        self._remove_actor_from_overlay(d["actor"])
                        new_actor = self._make_polyline_actor(coords)
                        self._add_actor_to_overlay(new_actor)
                        d["actor"] = new_actor
                        d["coords"] = coords
                        d["bounds"] = new_actor.GetBounds()

                        if d["type"] == "line":
                            for mk in ("start_marker", "end_marker"):
                                if mk in d and d[mk]:
                                    try:
                                        self._remove_actor_from_overlay(d[mk])
                                    except Exception:
                                        pass
                                    d[mk] = None
                            start_pt, end_pt = coords[0], coords[-1]
                            d["start_marker"] = self._add_endpoint_sphere(start_pt, color=(0, 1, 0))
                            d["end_marker"] = self._add_endpoint_sphere(end_pt, color=(1, 0, 0))

                    else:
                        print(f"⚪ No nearby vertex — removing full {d['type']}")
                        self._remove_drawing(d)

                self.multi_selected = []
                self.clear_coordinate_labels()
                self.renderer.Modified()
                self.app.vtk_widget.render()
                print("✅ Multi-delete complete")
                return

            # === SINGLE SELECTION VERTEX DELETE ===
            if getattr(self, "selected_vertex_idx", None) is not None and self.selected_drawing:
                idx = self.selected_vertex_idx
                d = self.selected_drawing
                coords = d["coords"]

                print(f"🗑️ Deleting selected vertex {idx} from {d['type']}")
                del coords[idx]

                if len(coords) < 2:
                    self._remove_drawing(d)
                    self.selected_drawing = None
                    self.selected_vertex_idx = None
                    print("⚠️ Line too short, entire drawing removed.")
                else:
                    self._remove_actor_from_overlay(d["actor"])
                    new_actor = self._make_polyline_actor(coords)
                    self._add_actor_to_overlay(new_actor)
                    d["actor"] = new_actor
                    d["coords"] = coords
                    d["bounds"] = new_actor.GetBounds()

                    if d["type"] == "line":
                        for mk in ("start_marker", "end_marker"):
                            if mk in d and d[mk]:
                                try:
                                    self._remove_actor_from_overlay(d[mk])
                                except Exception:
                                    pass
                                d[mk] = None
                        start_pt, end_pt = coords[0], coords[-1]
                        d["start_marker"] = self._add_endpoint_sphere(start_pt, color=(0, 1, 0))
                        d["end_marker"] = self._add_endpoint_sphere(end_pt, color=(1, 0, 0))

                    print("✅ Vertex deleted and line rebuilt.")

                self.clear_coordinate_labels()
                if d and len(coords) >= 2:
                    self.show_vertex_coordinates(coords)
                self.selected_vertex_idx = None
                self.renderer.Modified()
                self.app.vtk_widget.render()
                return

            # === FALLBACK: DELETE ENTIRE SELECTED DRAWING ===
            if getattr(self, "selected_drawing", None):
                d = self.selected_drawing
                print(f"🗑️ Removing full {d['type']} drawing")
                self._remove_drawing(d)
                self.selected_drawing = None
                self.clear_coordinate_labels()
                self.renderer.Modified()
                self.app.vtk_widget.render()
                print("✅ Entire drawing deleted")
                return

            print("⚪ Nothing selected to delete")
            return

        # --- MOVE MODE TOGGLE ---
        elif key == "m":
            self.move_mode = not getattr(self, "move_mode", False)
            print("🔄 Move mode:", self.move_mode)

        # --- COPY / PASTE ---
        elif key == "c" and self.interactor.GetControlKey():
            if hasattr(self, "_copy_selected"):
                self._copy_selected()
            else:
                print("⚠️ Copy not implemented")
        elif key == "v" and self.interactor.GetControlKey():
            if hasattr(self, "_paste_selected"):
                self._paste_selected()
            else:
                print("⚠️ Paste not implemented")

        # --- ESC: Exit move vertex mode, deactivate active tool, or clear selections ---
        elif key == "escape":
            # Check if in move vertex mode
            if getattr(self, 'vertex_moving', False):
                self._deactivate_move_vertex_mode()
                self.active_tool = None
                try:
                    self.app.statusBar().showMessage("Move Vertex mode exited")
                except Exception:
                    pass
                return

            # If a draw tool is active, deactivate it (keeps drawings, blocks shift)
            if self._deactivate_active_tool_keep_drawings():
                return
            
            # Normal selection clearing
            self.clear_coordinate_labels()
            self.selected_drawing = None
            self.selected_vertex_idx = None
            if hasattr(self, "multi_selected"):
                self.multi_selected = []
            if hasattr(self, "_vertex_highlight") and self._vertex_highlight:
                self._remove_actor_from_overlay(self._vertex_highlight)
                self._vertex_highlight = None
            self._unhighlight_all_lines()
            print("⚪ All selections cleared")

        # --- E KEY: Edit selected drawing (text or line) ---
        elif key == "e":
            if getattr(self, "selected_drawing", None):
                drawing_type = self.selected_drawing.get("type")
                
                # Text editing
                if drawing_type == "text":
                    self._edit_text_label(self.selected_drawing)
                    return
                
                # Line/SmartLine/Polyline/Freehand editing
                elif drawing_type in ("line_segment", "smartline", "centerline", "polyline", "freehand", "rectangle", "circle", "polygon"):
                    self._edit_line_properties(self.selected_drawing)
                    return
                
                else:
                    print(f"ℹ️ Editing not supported for {drawing_type}")
            else:
                print("⚠️ No drawing selected - right-click on a line first")


    # ---------------- SELECTION HIGHLIGHTING ----------------
    def _highlight_line(self, drawing):
        """Highlight selected drawing — works for lines, polylines, rectangles AND text."""
        if "actor" not in drawing:
            return

        actor = drawing["actor"]
        if actor is None:
            return

        dtype = drawing.get("type", "")

        # ── TEXT actors use vtkTextActor3D → GetTextProperty() ───────────────
        if dtype == "text":
            if not hasattr(actor, "GetTextProperty"):
                return
            try:
                text_prop = actor.GetTextProperty()
                if "original_color" not in drawing:
                    c = text_prop.GetColor()
                    drawing["original_color"] = (float(c[0]), float(c[1]), float(c[2]))
                text_prop.SetColor(0.0, 1.0, 1.0)
                actor.Modified()
            except Exception as e:
                print(f"⚠️ Text highlight failed: {e}")
            return

        # ── All other actors (vtkActor / vtkActor2D) → GetProperty() ─────────
        if not hasattr(actor, "GetProperty"):
            return
        try:
            prop = actor.GetProperty()
            if "original_color" not in drawing:
                c = prop.GetColor()
                drawing["original_color"] = (float(c[0]), float(c[1]), float(c[2]))
            if "original_width" not in drawing:
                try:
                    drawing["original_width"] = float(prop.GetLineWidth())
                except Exception:
                    drawing["original_width"] = 2.0
            prop.SetColor(0.0, 1.0, 1.0)
            try:
                prop.SetLineWidth(5)
            except Exception:
                pass
            actor.Modified()
        except Exception as e:
            print(f"⚠️ Highlight failed: {e}")

    def _unhighlight_line(self, drawing=None):
        """Restore a drawing to its original appearance — works for lines AND text."""
        target = drawing if drawing is not None else getattr(self, "selected_drawing", None)

        if target is None or "actor" not in target:
            return

        actor = target["actor"]
        if actor is None:
            return

        dtype = target.get("type", "")

        # ── TEXT actors ───────────────────────────────────────────────────────
        if dtype == "text":
            if not hasattr(actor, "GetTextProperty"):
                return
            try:
                text_prop = actor.GetTextProperty()
                orig = (
                    target.get("original_color")
                    or target.get("original_text_color")
                    or (1.0, 1.0, 1.0)
                )
                # Ensure it's a plain 3-tuple of floats — numpy arrays crash SetColor
                r, g, b = float(orig[0]), float(orig[1]), float(orig[2])
                text_prop.SetColor(r, g, b)
                actor.Modified()
            except Exception as e:
                print(f"⚠️ Text unhighlight failed: {e}")
            return

        # ── All other actors ──────────────────────────────────────────────────
        if not hasattr(actor, "GetProperty"):
            return
        try:
            prop = actor.GetProperty()
            orig = target.get("original_color", (1.0, 0.0, 0.0))
            r, g, b = float(orig[0]), float(orig[1]), float(orig[2])
            prop.SetColor(r, g, b)
            try:
                prop.SetLineWidth(float(target.get("original_width", 2)))
            except Exception:
                pass
            actor.Modified()
        except Exception as e:
            print(f"⚠️ Unhighlight failed: {e}")

    def _remove_drawing(self, drawing):
        """
        ✅ BULLETPROOF: Completely remove a drawing and ALL associated geometry.
        Includes state-desync prevention to guarantee zero memory leaks.
        """
        try:
            print(f"🧹 Commencing full deletion of: {drawing.get('type', 'unknown')}")
            
            # 🛑 0️⃣ STATE DESYNC PREVENTION (THE FIX) 🛑
            if getattr(self, "selected_drawing", None) is drawing:
                self.clear_coordinate_labels()
                self.selected_drawing = None

            # ✅ FIX: Use identity check instead of 'in' to avoid numpy array comparison
            if hasattr(self, "multi_selected"):
                try:
                    # Remove by identity, not equality
                    self.multi_selected = [d for d in self.multi_selected if d is not drawing]
                except Exception as e:
                    print(f"⚠️ multi_selected cleanup failed: {e}")
                
            if getattr(self, "selected", None) is drawing:
                self._clear_selection()
                self.selected = None

            if drawing.get("source") == "curve_tool":
                curve_tool = getattr(getattr(self, "app", None), "curve_tool", None)
                if curve_tool is not None:
                    try:
                        if getattr(curve_tool, "selected_curve_data", None) is drawing:
                            if hasattr(curve_tool, "_deselect_curve"):
                                curve_tool._deselect_curve()
                        if hasattr(curve_tool, "finalized_actors"):
                            curve_tool.finalized_actors = [curve for curve in curve_tool.finalized_actors if curve is not drawing]
                    except Exception:
                        pass

            # --- 1️⃣ Remove the main actor ---
            if "actor" in drawing and drawing["actor"]:
                if drawing.get("type") == "text":
                    if drawing.get("scalable", False):
                        # Scalable text lives in overlay renderer
                        self._remove_actor_from_overlay(drawing["actor"])
                    else:
                        # Legacy billboard text
                        try: self.renderer.RemoveViewProp(drawing["actor"])
                        except Exception:
                            pass
                    # Safety net
                    try: self._remove_actor_from_overlay(drawing["actor"])
                    except Exception:
                        pass
                else:
                    self._remove_actor_from_overlay(drawing["actor"])

            # --- 1b: Remove fill actor if present ---
            if drawing.get("fill_actor"):
                try:
                    self._remove_actor_from_overlay(drawing["fill_actor"])
                except Exception:
                    pass
                drawing["fill_actor"] = None

            # --- 1c: Remove hatch line actors if present ---
            for ha in drawing.get("hatch_actors", []):
                try:
                    self._remove_actor_from_overlay(ha)
                except Exception:
                    pass

                for _renderer in (
                    getattr(self, "text_overlay_renderer", None),
                    getattr(self, "overlay_renderer", None),
                    getattr(self, "renderer", None),
                ):
                    if _renderer is None:
                        continue
                    try:
                        _renderer.RemoveActor(ha)
                    except Exception:
                        pass
                    try:
                        _renderer.RemoveActor2D(ha)
                    except Exception:
                        pass
                    try:
                        _renderer.RemoveViewProp(ha)
                    except Exception:
                        pass

            if "hatch_actors" in drawing:
                drawing["hatch_actors"] = []

            # --- 2️⃣ Remove standard markers ---
            for key in ("start_marker", "end_marker"):
                if key in drawing and drawing[key]:
                    try: self._remove_actor_from_overlay(drawing[key])
                    except Exception:
                        pass
                    drawing[key] = None

            # --- 3️⃣ Remove explicitly linked vertex markers ---
            if "vertex_markers" in drawing and drawing["vertex_markers"]:
                for marker in drawing["vertex_markers"]:
                    try: self._remove_actor_from_overlay(marker)
                    except Exception:
                        pass
                drawing["vertex_markers"] = []
            
            # --- 4️⃣ Remove 2D arrow overlays ---
            if "arrow_actor" in drawing and drawing["arrow_actor"]:
                arrows = drawing["arrow_actor"]
                if not isinstance(arrows, list): arrows = [arrows]
                for arr in arrows:
                    try: self.renderer.RemoveActor2D(arr)
                    except Exception:
                        pass
                drawing["arrow_actor"] = None

            # --- 5️⃣ Remove from list ---
            # ✅ FIX: Use identity check to avoid numpy array comparison
            try:
                self.drawings[:] = [d for d in self.drawings if d is not drawing]
            except Exception as e:
                print(f"⚠️ Failed to remove drawing from list: {e}")

            # --- 6️⃣ Rebuild remaining segment connections ---
            if hasattr(self, '_refresh_segment_markers'):
                self._refresh_segment_markers()

            # Keep MicroStation-style selection set in sync (Phase 1)
            if self._selection_manager is not None:
                try:
                    self._selection_manager.notify_drawing_removed(drawing)
                except Exception as _sel_err:
                    print(f"⚠️ SelectionManager remove sync warning: {_sel_err}")

            # Keep fence-dialog selections/highlights in sync with removed drawing.
            try:
                self._sync_fence_dialogs_after_drawing_change(
                    removed_drawings=[drawing],
                    clear_all=False,
                )
            except Exception as _sync_err:
                print(f"⚠️ Fence dialog sync warning: {_sync_err}")
                
            self.renderer.Modified()
            self._emit_drawing_removed(drawing)
            print(f"✅ Drawing and all associated geometry purged.")

        except Exception as e:
            print(f"⚠️ Error while removing drawing: {e}")
            import traceback
            traceback.print_exc()

    def _hard_cleanup_drawing_runtime_actors(self):
        """
        Remove all live drawing-related VTK actors before undo/redo restore.

        This prevents stale residue actors after Rotate/Mirror + Undo/Redo.
        """
        renderers = []
        for renderer in (
            getattr(self, "overlay_renderer", None),
            getattr(self, "renderer", None),
        ):
            if renderer is not None and renderer not in renderers:
                renderers.append(renderer)

        def _remove_actor(actor):
            if actor is None:
                return
            for renderer in renderers:
                for method_name in ("RemoveActor", "RemoveActor2D", "RemoveViewProp"):
                    try:
                        getattr(renderer, method_name)(actor)
                    except Exception:
                        pass

        for drawing in list(getattr(self, "drawings", []) or []):
            if not isinstance(drawing, dict):
                continue

            for key in (
                "actor",
                "arrow_actor",
                "start_marker",
                "end_marker",
                "marker",
                "label_actor",
                "text_actor",
                "fill_actor",
            ):
                value = drawing.get(key)
                if isinstance(value, (list, tuple, set)):
                    for actor in value:
                        _remove_actor(actor)
                else:
                    _remove_actor(value)

            for key in (
                "actors",
                "markers",
                "vertex_markers",
                "coordinate_labels",
                "highlight_actors",
            ):
                value = drawing.get(key)
                if isinstance(value, (list, tuple, set)):
                    for actor in value:
                        _remove_actor(actor)

        for attr_name in (
            "_continuous_line_actor",
            "_preview_line_actor",
            "_rectangle_preview_actor",
            "_circle_preview_actor",
            "_circle_preview_actor_2d",
            "_preview_actor",
            "_freehand_preview_actor_2d",
            "_suspended_preview_actor",
            "vertex_hover_marker",
            "vertex_drag_marker",
        ):
            _remove_actor(getattr(self, attr_name, None))
            try:
                setattr(self, attr_name, None)
            except Exception:
                pass

        for attr_name in (
            "_smartline_vertex_markers",
            "_line_vertex_markers",
            "_polyline_vertex_markers",
            "_hatch_vertex_markers",
        ):
            markers = getattr(self, attr_name, None)
            if isinstance(markers, (list, tuple, set)):
                for actor in markers:
                    _remove_actor(actor)
            try:
                setattr(self, attr_name, [])
            except Exception:
                pass

        try:
            self.clear_coordinate_labels()
        except Exception:
            labels = getattr(self, "coord_labels", None)
            if isinstance(labels, (list, tuple, set)):
                for actor in labels:
                    _remove_actor(actor)
            try:
                self.coord_labels = []
            except Exception:
                pass

        try:
            tool = getattr(self, "_corner_rotate_tool", None)
            if tool is not None:
                tool.deactivate(cancel=False, silent=True)
        except Exception:
            pass

        try:
            if hasattr(self, "_corner_rotate_remove_grips"):
                self._corner_rotate_remove_grips()
        except Exception:
            pass

        try:
            if hasattr(self, "_unhighlight_all_lines"):
                self._unhighlight_all_lines()
        except Exception:
            pass

        self.selected = None
        self.selected_drawing = None
        self.selected_vertex_idx = None
        self.multi_selected = []
        self.dragging_vertex = None
        self.vertex_move_mode = False
        self.left_down = False

        try:
            manager = getattr(self, "_selection_manager", None)
            if manager is not None:
                for method_name in ("clear", "clear_selection", "deselect_all", "clear_all"):
                    method = getattr(manager, method_name, None)
                    if callable(method):
                        method()
                        break
        except Exception:
            pass

        try:
            if self.overlay_renderer is not None:
                self.overlay_renderer.Modified()
            if self.renderer is not None:
                self.renderer.Modified()
        except Exception:
            pass

    @staticmethod
    def _snapshot_entry_for(d):
        """
        Build the plain-value snapshot dict for one live drawing.

        Shared by _capture_state_snapshot() (undo/redo history) and the
        incremental restore's "did this drawing actually change?" comparison.
        Only plain types (str/int/float/bool/tuple/list/None) are stored here
        — never numpy arrays or VTK actors (except the curve 'actor'/'curve_data'
        identity fields, which are handled as pass-through references) — so
        this dict is always safe to compare with `==`.
        """
        drawing_copy = {
            '_uid': d.get('_uid'),
            'type': d['type'],
            'coords': list(d['coords']),
            'text': d.get('text', ''),
            'original_color': d.get('original_color', (1, 0, 0)),
            'original_width': d.get('original_width', 2),
            'original_style': d.get('original_style', 'solid'),
            'original_text_color': d.get('original_text_color', (1, 1, 1)),
            'font_size': d.get('font_size', 75),
            'bold': d.get('bold', True),
            'italic': d.get('italic', False),
            'font_family': d.get('font_family', 'Arial'),
            'justify': d.get('justify', 'center'),
            'scalable': d.get('scalable', False),
            'center': d.get('center', None),
            'radius': d.get('radius', None),
            'has_arrow': bool(d.get('arrow_actor')),
            'source_types': list(d.get('source_types', [])),
            'generated_from': d.get('generated_from', None),
            'original_fill_color': d.get('original_fill_color', None),
            'original_fill_opacity': d.get('original_fill_opacity', 0.3),
        }
        # Preserve the drawing's identity metadata so undo/redo does NOT
        # recolour it to the current active layer, re-tag its layer, or lose
        # its "already saved" state (which would re-commit it as a duplicate
        # and let Clear strip it from the saved SNT).
        if d.get('layer') is not None:
            drawing_copy['layer'] = d.get('layer')
        if d.get('sbm_class') is not None:
            drawing_copy['sbm_class'] = d.get('sbm_class')
        if d.get('color') is not None:
            drawing_copy['color'] = d.get('color')
        if d.get('snt_committed'):
            drawing_copy['snt_committed'] = True
        if d.get('snt_stored'):
            drawing_copy['snt_stored'] = True
        if d.get('snt_file') is not None:
            drawing_copy['snt_file'] = d.get('snt_file')
        if d.get('classified_fence'):
            drawing_copy['classified_fence'] = True
        if d.get('type') == 'curve':
            drawing_copy['curve_data'] = d
            drawing_copy['actor'] = d.get('actor')
            drawing_copy['control_points'] = d.get('control_points')
        if d.get('type') == 'hatcharea':
            drawing_copy['hatch_spacing'] = d.get('hatch_spacing', 800.0)
            drawing_copy['hatch_angle'] = d.get('hatch_angle', 0.0)
            drawing_copy['block_label'] = d.get('block_label', None)
        return drawing_copy

    def _capture_state_snapshot(self):
        """Capture drawings with the properties needed for undo/redo restoration."""
        state = []
        for d in self.drawings:
            drawing_copy = self._snapshot_entry_for(d)
            state.append(drawing_copy)

        
        # Also capture curve tool actors for unified undo/redo
        curve_tool = getattr(self.app, 'curve_tool', None)
        if curve_tool and hasattr(curve_tool, 'finalized_actors'):
            for curve_data in curve_tool.finalized_actors:
                if isinstance(curve_data, dict) and 'actor' in curve_data:
                    state.append({
                        'type': 'curve',
                        'curve_data': curve_data,
                        '_uid': curve_data.get('_uid'),
                    })
        
        return state

    @staticmethod
    def _restore_layer_metadata(drawing_entry, snapshot_entry):
        """Restore layer/SBM/commit metadata without retagging from the current active layer."""
        if 'layer' in snapshot_entry:
            drawing_entry['layer'] = snapshot_entry.get('layer')
        if 'sbm_class' in snapshot_entry:
            sbm_class = snapshot_entry.get('sbm_class')
            drawing_entry['sbm_class'] = dict(sbm_class) if isinstance(sbm_class, dict) else sbm_class
        if 'color' in snapshot_entry:
            drawing_entry['color'] = snapshot_entry.get('color')
        # Keep "already saved" markers so a restored drawing stays locked: it is
        # not re-committed (no duplicate) and Clear/auto-remove won't strip it.
        if snapshot_entry.get('snt_committed'):
            drawing_entry['snt_committed'] = True
        if snapshot_entry.get('snt_stored'):
            drawing_entry['snt_stored'] = True
        if snapshot_entry.get('snt_file') is not None:
            drawing_entry['snt_file'] = snapshot_entry.get('snt_file')
        if snapshot_entry.get('classified_fence'):
            drawing_entry['classified_fence'] = True

    def _save_state(self):
        """Save current state to undo stack with full properties."""
        self.undo_stack.append(self._capture_state_snapshot())
        
        if len(self.undo_stack) > self.max_undo_levels:
            self.undo_stack.pop(0)
        
        self.redo_stack = []
        print(f"💾 State saved (undo stack: {len(self.undo_stack)})")            

    def _get_classified_fences_removed_by_undo(self):
        """Check which classified fences in current drawings will be lost on undo.
        
        Compares current drawings against the previous state snapshot (top of
        undo_stack) and returns a list of drawing dicts whose ``classified_fence``
        flag is True but that do NOT exist in the previous snapshot.
        """
        if not self.undo_stack:
            return []
        previous_state = self.undo_stack[-1]  # peek, do not pop
        # Build a set of coordinate keys from the previous state
        prev_keys = set()
        for d in previous_state:
            if d.get('type') == 'curve':
                continue
            coords = d.get('coords', [])
            if coords:
                try:
                    key = (d.get('type'), tuple(tuple(c) for c in coords))
                    prev_keys.add(key)
                except Exception:
                    pass
        # Find classified fences that won't exist in the previous state
        removed = []
        for d in self.drawings:
            if not d.get('classified_fence'):
                continue
            coords = d.get('coords', [])
            if coords:
                try:
                    key = (d.get('type'), tuple(tuple(c) for c in coords))
                    if key not in prev_keys:
                        removed.append(d)
                except Exception:
                    pass
        return removed

    def _push_temp_vertex_history(self):
        """Store the current in-progress vertex state before adding a new point."""
        if not hasattr(self, "_temp_vertex_stack"):
            self._temp_vertex_stack = []
        if not hasattr(self, "_temp_redo_stack"):
            self._temp_redo_stack = []
        self._temp_vertex_stack.append(list(self.temp_points))
        # Cap temp stacks to prevent unbounded growth during long drawing sessions
        if len(self._temp_vertex_stack) > self.max_undo_levels:
            self._temp_vertex_stack.pop(0)
        self._temp_redo_stack = []

    def _clear_temp_vertex_history(self):
        """Drop any in-progress undo/redo history for active line-based tools."""
        self._temp_vertex_stack = []
        self._temp_redo_stack = []

    def _get_active_vertex_marker_attr(self):
        return {
            "smartline": "_smartline_vertex_markers",
            "line": "_line_vertex_markers",
            "polyline": "_polyline_vertex_markers",
            "hatcharea": "_hatch_vertex_markers",
        }.get(self.active_tool)

    def _rebuild_active_vertex_markers(self):
        """Recreate in-progress vertex markers after temp undo/redo changes."""
        marker_attr = self._get_active_vertex_marker_attr()
        if not marker_attr:
            return

        markers = getattr(self, marker_attr, [])
        for marker in markers:
            try:
                self._remove_actor_from_overlay(marker)
            except Exception:
                pass

        rebuilt_markers = []
        radius = 0.20 if self.active_tool == "smartline" else 0.05
        for idx, pt in enumerate(self.temp_points):
            color = (0, 1, 0) if idx == 0 else (1, 1, 0)
            marker = self._add_endpoint_sphere(pt, color=color, radius=radius)
            try:
                marker.GetProperty().SetOpacity(0.8)
            except Exception:
                pass
            rebuilt_markers.append(marker)

        setattr(self, marker_attr, rebuilt_markers)

    def _remove_drawing_actors(self, d):
        """
        Remove every live VTK actor belonging to ONE drawing dict.

        Extracted verbatim from the per-drawing removal body that used to be
        inline in _restore_state's "Remove existing drawing actors" loop, so
        it can be called either for every drawing (full restore) or for just
        the one drawing that was actually removed (incremental restore).
        """
        try:
            if "actor" in d and d["actor"]:
                if d.get("type") == "text":
                    if d.get("scalable", False):
                        self._remove_actor_from_overlay(d["actor"])
                    else:
                        try:
                            self.renderer.RemoveViewProp(d["actor"])
                        except Exception:
                            pass
                        try:
                            self._remove_actor_from_overlay(d["actor"])
                        except Exception:
                            pass
                else:
                    self._remove_actor_from_overlay(d["actor"])

            for key in ("start_marker", "end_marker"):
                if key in d and d[key]:
                    try:
                        self._remove_actor_from_overlay(d[key])
                    except Exception:
                        pass

            if "vertex_markers" in d and d["vertex_markers"]:
                for marker in d["vertex_markers"]:
                    try:
                        self._remove_actor_from_overlay(marker)
                    except Exception:
                        pass

            if "arrow_actor" in d and d["arrow_actor"]:
                arrows = d["arrow_actor"]
                if not isinstance(arrows, list):
                    arrows = [arrows]
                for arr in arrows:
                    try:
                        self.renderer.RemoveActor2D(arr)
                    except Exception:
                        pass

            if d.get("fill_actor"):
                try:
                    self._remove_actor_from_overlay(d["fill_actor"])
                except Exception:
                    pass

            for ha in d.get("hatch_actors", []):
                try:
                    self._remove_actor_from_overlay(ha)
                except Exception:
                    pass
        except Exception:
            pass

    def _rebuild_single_drawing_from_snapshot(self, d):
        """
        Recreate ONE drawing (actors + dict entry) from a saved-state snapshot
        entry and append it to self.drawings.

        Extracted verbatim from the "Recreate ALL drawings from saved state"
        loop that used to be inline in _restore_state, so it can be called
        either for every snapshot entry (full restore) or for just the one
        entry that was actually added (incremental restore).
        """
        curve_tool = getattr(self.app, 'curve_tool', None)

        # Handle curve entries - restore to curve_tool
        if d.get('type') == 'curve':
            curve_data = d.get('curve_data')

            # Reconstruct fallback if curve_data is missing or lost
            if not curve_data or not isinstance(curve_data, dict) or 'actor' not in curve_data:
                if curve_tool:
                    color = d.get("original_color", (0, 1, 0))
                    width = d.get("original_width", 2)
                    coords = d.get("coords", [])
                    control_points = d.get("control_points", coords)
                    if coords:
                        try:
                            actor = curve_tool._create_curve_actor(coords, color=color, width=width)
                            curve_data = {
                                'actor': actor,
                                'control_points': control_points,
                                'interpolated': coords,
                                'coords': coords,
                                'type': 'curve',
                                'source': 'curve_tool',
                                'color': color,
                                'original_color': color,
                                'original_width': width,
                                '_uid': d.get('_uid'),
                            }
                        except Exception:
                            curve_data = None
                    else:
                        curve_data = None
                else:
                    curve_data = None

            if curve_data and isinstance(curve_data, dict) and 'actor' in curve_data:
                try:
                    renderer = curve_tool._get_renderer() if curve_tool else self.app.vtk_widget.renderer
                    renderer.AddViewProp(curve_data['actor'])
                    if curve_tool and curve_data not in curve_tool.finalized_actors:
                        curve_tool.finalized_actors.append(curve_data)
                    if curve_data not in self.drawings:
                        self.drawings.append(curve_data)
                except Exception as e:
                    print(f"⚠️ Error restoring curve: {e}")
            return

        coords = d["coords"]
        color = d.get("original_color", (1, 0, 0))
        width = d.get("original_width", 2)
        line_style = d.get("original_style", "solid")
        dtype = d["type"]

        if dtype == "text":
            pos = coords[0]
            text = d.get("text", "Text")
            t_color = d.get("original_text_color", d.get("original_color", (1, 1, 1)))
            t_font_size = d.get("font_size", 75)
            t_bold = d.get("bold", True)
            t_italic = d.get("italic", False)
            t_font_family = d.get("font_family", "Arial")
            t_justify = d.get("justify", "center")

            actor = self._make_scalable_text_actor(
                text=text,
                position=pos,
                color=t_color,
                font_size=t_font_size,
                bold=t_bold,
                italic=t_italic,
                font_family=t_font_family,
                justify=t_justify,
            )

            if not bool(getattr(self, "draw_text_visible", True)):
                actor.SetVisibility(0)

            self._add_actor_to_overlay(actor)

            drawing_entry = {
                "type": dtype,
                "coords": coords,
                "actor": actor,
                "text": text,
                "bounds": actor.GetBounds(),
                "original_text_color": t_color,
                "font_size": t_font_size,
                "bold": t_bold,
                "italic": t_italic,
                "font_family": t_font_family,
                "justify": t_justify,
                "scalable": True,
                "_draw_text_visible": bool(getattr(self, "draw_text_visible", True)),
                "_uid": d.get("_uid"),
            }
            self._restore_layer_metadata(drawing_entry, d)
            self.drawings.append(drawing_entry)

        elif dtype == "line_segment":
            # ✅ Line segments get endpoint markers
            if len(coords) >= 2:
                actor = self._make_polyline_actor(coords, color=color, width=width, line_style=line_style)
                actor.PickableOn()
                self._add_actor_to_overlay(actor)
                arrow_actor = self._add_arrow_to_line(coords) if d.get("has_arrow", False) else None

                start_marker = self._add_endpoint_sphere(coords[0], color=(0, 1, 0), radius=0.05)
                end_marker = self._add_endpoint_sphere(coords[-1], color=(1, 0, 0), radius=0.05)

                drawing_entry = {
                    "type": dtype,
                    "coords": list(coords),
                    "actor": actor,
                    "bounds": actor.GetBounds(),
                    "start_marker": start_marker,
                    "end_marker": end_marker,
                    "arrow_actor": arrow_actor,
                    "original_color": color,
                    "original_width": width,
                    "original_style": line_style,
                    "_uid": d.get("_uid"),
                }
                self._restore_layer_metadata(drawing_entry, d)
                self.drawings.append(drawing_entry)

        elif dtype == "hatcharea":
            # Restore boundary outline + hatch lines
            from gui.hatch_area_tool import generate_hatch_lines
            if len(coords) >= 3:
                boundary_actor = self._make_polyline_actor(coords, color=color, width=width, line_style=line_style)
                if boundary_actor:
                    boundary_actor.PickableOn()
                    self._add_hatch_actor(boundary_actor)
                spacing = d.get("hatch_spacing", 800.0)
                angle = d.get("hatch_angle", 0.0)
                hatch_segments = generate_hatch_lines(coords, spacing, angle)
                hatch_actors = []
                for sa in self._make_hatch_segments_actors(hatch_segments, color, width, line_style):
                    self._add_hatch_actor(sa)
                    hatch_actors.append(sa)
                drawing_entry = {
                    "type": "hatcharea",
                    "coords": list(coords),
                    "actor": boundary_actor,
                    "hatch_actors": hatch_actors,
                    "hatch_spacing": spacing,
                    "hatch_angle": angle,
                    "block_label": d.get("block_label", None),
                    "bounds": boundary_actor.GetBounds() if boundary_actor else (0,0,0,0,0,0),
                    "original_color": color,
                    "original_width": width,
                    "original_style": line_style,
                    "_uid": d.get("_uid"),
                }
                # Unsaved generated hatch should remain clearable after undo/redo.
                # Saved/committed hatch can keep its metadata.
                if not d.get("snt_committed", False):
                    drawing_entry["layer"] = "DIGITIZER"
                    drawing_entry.pop("snt_file", None)
                    drawing_entry.pop("dxf_file", None)
                    drawing_entry.pop("sbm_class", None)
                else:
                    self._restore_layer_metadata(drawing_entry, d)

                self.drawings.append(drawing_entry)
                if hasattr(self, '_hatch_dialog') and self._hatch_dialog:
                    try: self._hatch_dialog.set_status(
                        f"Undo restored hatch ({len(hatch_segments)} lines).\n"
                        f"Click inside a shape to hatch again."
                    )
                    except Exception: pass

        elif dtype in ("smartline", "centerline", "line", "polyline", "freehand", "rectangle", "polygon", "circle"):
            # ✅ All geometry types get recreated with vertex markers
            if len(coords) >= 2:
                actor = self._make_polyline_actor(coords, color=color, width=width, line_style=line_style)
                actor.PickableOn()
                self._add_actor_to_overlay(actor)
                arrow_actor = None
                if dtype in ("smartline", "line") and d.get("has_arrow", False):
                    arrow_actor = self._add_arrow_to_line(coords)

                # Create vertex markers
                vertex_markers = []
                for i, pt in enumerate(coords):
                    if i == 0:
                        marker_color = (0, 1, 0)   # Green start
                    elif i == len(coords) - 1:
                        marker_color = (1, 0, 0)    # Red end
                    else:
                        marker_color = (1, 1, 0)    # Yellow middle

                    marker = self._add_endpoint_sphere(pt, color=marker_color, radius=0.05)
                    marker.GetProperty().SetOpacity(0.8)
                    vertex_markers.append(marker)

                drawing_entry = {
                    "type": dtype,
                    "coords": list(coords),
                    "actor": actor,
                    "bounds": actor.GetBounds(),
                    "vertex_markers": vertex_markers,
                    "arrow_actor": arrow_actor,
                    "original_color": color,
                    "original_width": width,
                    "original_style": line_style,
                    "_uid": d.get("_uid"),
                }

                # Restore fill actor for fillable types
                fill_color = d.get("original_fill_color")
                if fill_color is not None and dtype in ("freehand", "rectangle", "circle", "polyline", "polygon", "smartline"):
                    fill_opacity = d.get("original_fill_opacity", 0.3)
                    fill_actor = self._make_fill_actor(coords, fill_color, fill_opacity)
                    if fill_actor is not None:
                        self._add_actor_to_overlay(fill_actor)
                        drawing_entry["fill_actor"] = fill_actor
                        drawing_entry["original_fill_color"] = fill_color
                        drawing_entry["original_fill_opacity"] = fill_opacity

                # Preserve extra properties for circles
                if dtype == "circle" and "center" in d:
                    drawing_entry["center"] = d["center"]
                    drawing_entry["radius"] = d["radius"]
                if d.get("source_types"):
                    drawing_entry["source_types"] = list(d.get("source_types", []))
                if d.get("generated_from"):
                    drawing_entry["generated_from"] = d.get("generated_from")
                self._restore_layer_metadata(drawing_entry, d)

                self.drawings.append(drawing_entry)

        else:
            # ✅ Fallback for any unknown type
            if len(coords) >= 2:
                actor = self._make_polyline_actor(coords, color=color, width=width)
                actor.PickableOn()
                self._add_actor_to_overlay(actor)

                drawing_entry = {
                    "type": dtype,
                    "coords": list(coords),
                    "actor": actor,
                    "bounds": actor.GetBounds(),
                    "original_color": color,
                    "original_width": width,
                    "original_style": line_style,
                    "_uid": d.get("_uid"),
                }
                self._restore_layer_metadata(drawing_entry, d)
                self.drawings.append(drawing_entry)

    def _sync_curve_finalized_actors(self):
        """Keep CurveTool.finalized_actors in lockstep with self.drawings
        after any undo/redo.

        curve_tool.undo_curve()/redo_curve() delegate straight to this
        class's own undo()/redo() (the unified undo stack), which correctly
        adds/removes the curve's dict from self.drawings - but never touches
        curve_tool.finalized_actors, its own separate bookkeeping list of
        every curve ever finished. Left stale, an already-undone curve stays
        in finalized_actors forever, and Element Select's
        _iter_pickable_drawings() (element_select_tool.py) merges
        finalized_actors into what it treats as selectable - resurrecting
        the undone curve into Block/Individual-select (and, once selected,
        onto the canvas again via Move) even though it is correctly gone
        from self.drawings. Reconcile by `_uid` (stable across a "rebuilt"
        drawing getting a fresh dict/actor) rather than by object identity.
        """
        curve_tool = getattr(self.app, "curve_tool", None)
        finalized = getattr(curve_tool, "finalized_actors", None)
        if not finalized:
            return
        try:
            live_by_uid = {
                d.get('_uid'): d for d in self.drawings
                if isinstance(d, dict) and d.get('_uid')
            }
            kept = []
            for curve_data in finalized:
                uid = curve_data.get('_uid') if isinstance(curve_data, dict) else None
                if uid is None:
                    kept.append(curve_data)
                elif uid in live_by_uid:
                    kept.append(live_by_uid[uid])
            curve_tool.finalized_actors = kept
        except Exception as e:
            print(f"⚠️ Failed to sync curve finalized_actors after undo/redo: {e}")

    def _restore_state(self, state):
        """
        Undo/redo entry point.

        Diffs `state` (the snapshot we're restoring to) against the currently
        live self.drawings by each drawing's stable `_uid`, and only tears
        down / rebuilds the drawings that actually differ — instead of every
        drawing in the project. This is what makes rapid Undo/Redo not freeze
        the app on large projects.

        Safety net: if anything about the diff can't be trusted (missing
        uid, duplicate uid, any unexpected error), this falls straight back
        to _restore_state_legacy_full(state) — today's exact, already-working
        full-rebuild behavior. So the fast path can only ever be faster, never
        behave differently.
        """
        try:
            live_by_uid = {}
            for d in self.drawings:
                uid = d.get('_uid') if isinstance(d, dict) else None
                if not uid or uid in live_by_uid:
                    raise ValueError("live drawing missing/duplicate _uid")
                live_by_uid[uid] = d

            target_by_uid = {}
            for entry in state:
                uid = entry.get('_uid') if isinstance(entry, dict) else None
                if not uid or uid in target_by_uid:
                    raise ValueError("snapshot entry missing/duplicate _uid")
                target_by_uid[uid] = entry

            live_uids = set(live_by_uid.keys())
            target_uids = set(target_by_uid.keys())

            removed_uids = live_uids - target_uids
            added_uids = target_uids - live_uids
            common_uids = live_uids & target_uids

            touched_drawings = []

            # --- Removed: tear down actors for just these, drop from list ---
            for uid in removed_uids:
                self._remove_drawing_actors(live_by_uid[uid])
            if removed_uids:
                self.drawings[:] = [d for d in self.drawings if d.get('_uid') not in removed_uids]

            # --- Unchanged vs changed (same uid): skip untouched, else reuse
            #     the existing single-drawing rebuild (Move Vertex/Rotate/
            #     Translate already rely on this same function). ---
            for uid in common_uids:
                live_d = live_by_uid[uid]
                target_entry = target_by_uid[uid]
                if self._snapshot_entry_for(live_d) == target_entry:
                    continue  # nothing about this drawing changed — zero cost

                if live_d.get('type') == 'text':
                    # _rebuild_drawing_actor() intentionally no-ops for text
                    # (vtkTextActor3D isn't rebuilt from coords like a
                    # polyline), so a changed text drawing needs its own
                    # in-place actor swap instead.
                    self._remove_drawing_actors(live_d)
                    coords = target_entry.get('coords') or live_d.get('coords') or [(0, 0, 0)]
                    pos = coords[0]
                    text_val = target_entry.get('text', live_d.get('text', 'Text'))
                    t_color = target_entry.get(
                        'original_text_color', target_entry.get('original_color', (1, 1, 1))
                    )
                    font_size = target_entry.get('font_size', 75)
                    bold = target_entry.get('bold', True)
                    italic = target_entry.get('italic', False)
                    font_family = target_entry.get('font_family', 'Arial')
                    justify = target_entry.get('justify', 'center')
                    new_actor = self._make_scalable_text_actor(
                        text=text_val, position=pos, color=t_color,
                        font_size=font_size, bold=bold, italic=italic,
                        font_family=font_family, justify=justify,
                    )
                    if not bool(getattr(self, 'draw_text_visible', True)):
                        new_actor.SetVisibility(0)
                    self._add_actor_to_overlay(new_actor)
                    live_d['actor'] = new_actor
                    live_d['coords'] = list(coords)
                    live_d['text'] = text_val
                    live_d['original_text_color'] = t_color
                    live_d['font_size'] = font_size
                    live_d['bold'] = bold
                    live_d['italic'] = italic
                    live_d['font_family'] = font_family
                    live_d['justify'] = justify
                    live_d['bounds'] = new_actor.GetBounds()
                    self._restore_layer_metadata(live_d, target_entry)
                    touched_drawings.append(live_d)
                    continue

                live_d['coords'] = list(target_entry.get('coords', live_d.get('coords', [])))
                for field in (
                    'original_color', 'original_width', 'original_style',
                    'original_text_color', 'font_size', 'bold', 'italic',
                    'font_family', 'justify', 'center', 'radius',
                    'original_fill_color', 'original_fill_opacity', 'text',
                ):
                    if field in target_entry:
                        live_d[field] = target_entry[field]
                self._restore_layer_metadata(live_d, target_entry)
                self._rebuild_drawing_actor(live_d)
                touched_drawings.append(live_d)

            # --- Added: recreate from snapshot using the existing per-type
            #     reconstruction logic (identical to the full-rebuild path). ---
            for uid in added_uids:
                before = len(self.drawings)
                self._rebuild_single_drawing_from_snapshot(target_by_uid[uid])
                touched_drawings.extend(self.drawings[before:])

            # --- Shared finishing steps (same as full rebuild) ---
            if any(d['type'] == 'line_segment' for d in self.drawings):
                try:
                    self._refresh_segment_markers()
                except Exception:
                    pass
            try:
                self._hide_drawing_markers_now(touched_drawings or None)
            except Exception as e:
                print(f"⚠️ Undo/Redo marker-hide warning: {e}")

            self.renderer.Modified()
            self.app.vtk_widget.render()
            self._notify_selection_drawings_changed()
            try:
                self._sync_fence_dialogs_after_drawing_change(clear_all=True)
            except Exception as e:
                print(f"⚠️ Failed to sync fence dialogs after undo/redo: {e}")

            print(
                f"✅ State restored (fast path): {len(self.drawings)} drawings total "
                f"— {len(added_uids)} added, {len(removed_uids)} removed, "
                f"{len(touched_drawings)} rebuilt, rest unchanged"
            )
        except Exception as e:
            print(f"ℹ️ Incremental restore unavailable ({e}) — using full rebuild")
            self._restore_state_legacy_full(state)

    def _restore_state_legacy_full(self, state):
        """
        Restore drawings from a saved state - recreates ALL drawing types.

        This is the original, fully-tested full-rebuild path, kept intact as
        the guaranteed-safe fallback used by _restore_state() whenever the
        incremental (uid-diff) fast path can't be trusted for a given state.
        """
        # Hard cleanup before rebuilding snapshot; fixes stale yellow/residue
        # drawing actors after Rotate/Mirror + Undo/Redo.
        try:
            self._hard_cleanup_drawing_runtime_actors()
        except Exception as e:
            print(f"⚠️ Undo/Redo cleanup warning: {e}")

        # ✅ FIX: Remove 2D preview actors
        self._remove_preview_actor_2d('_continuous_line_actor')
        self._remove_preview_actor_2d('_preview_line_actor')
        self._remove_preview_actor_2d('_rectangle_preview_actor')
        self._remove_preview_actor_2d('_preview_actor')

        # Reset temp drawing state
        self.temp_points = []
        self.selected = None
        self.selected_drawing = None
        self._last_pos = None
        self.left_down = False
        self._clear_temp_vertex_history()

        # ✅ Clear all active in-progress vertex markers
        for attr in ['_smartline_vertex_markers', '_polyline_vertex_markers', '_line_vertex_markers', '_hatch_vertex_markers']:
            markers = getattr(self, attr, [])
            for marker in markers:
                try:
                    self._remove_actor_from_overlay(marker)
                except Exception:
                    pass
            setattr(self, attr, [])

        # --- Remove existing drawing actors ---
        for d in list(self.drawings):
            self._remove_drawing_actors(d)
        self.drawings.clear()

        # --- Also clear existing curve actors ---
        curve_tool = getattr(self.app, 'curve_tool', None)
        if curve_tool and hasattr(curve_tool, 'finalized_actors'):
            for curve_data in curve_tool.finalized_actors:
                try:
                    actor = curve_data['actor'] if isinstance(curve_data, dict) else curve_data
                    curve_tool._remove_curve_actor(actor)
                except Exception:
                    pass
            curve_tool.finalized_actors = []

        # --- ✅ FIX: Recreate ALL drawings from saved state ---
        for d in state:
            self._rebuild_single_drawing_from_snapshot(d)
        self._finish_restore_state()

    def _finish_restore_state(self):
        """Shared tail of both the legacy-full and incremental restore paths."""
        # ✅ Refresh segment markers for line_segments
        if any(d['type'] == 'line_segment' for d in self.drawings):
            try:
                self._refresh_segment_markers()
            except Exception:
                pass

        # Match the finished-drawing look: a freshly finalized drawing hides its
        # vertex / endpoint markers (via _hide_drawing_markers_now). Restore
        # recreates those markers visible, which made undo/redo show spheres on
        # every vertex (dotted circles/freehand). Hide them so a restored drawing
        # looks exactly like when it was finished — geometry is untouched.
        try:
            self._hide_drawing_markers_now()
        except Exception as e:
            print(f"⚠️ Undo/Redo marker-hide warning: {e}")

        self.renderer.Modified()
        self.app.vtk_widget.render()
        # Undo/redo may have replaced some drawing dicts with fresh objects
        # (added/changed) and dropped others (removed) — refresh the
        # MicroStation-style selection set so it only tracks ids still live.
        self._notify_selection_drawings_changed()
        
        # ✅ Sync and clear By Class fence dialogs on undo/redo to prevent stale reference leaks
        try:
            self._sync_fence_dialogs_after_drawing_change(clear_all=True)
        except Exception as e:
            print(f"⚠️ Failed to sync fence dialogs after undo/redo: {e}")
            
        print(f"✅ State restored: {len(self.drawings)} drawings recreated")

    def undo(self):
        """Undo last drawing operation (Ctrl+Z)."""
        if getattr(self, '_undo_in_progress', False):
            print("⏳ Undo already in progress, ignoring re-entrant call")
            return
        self._undo_in_progress = True
        try:
            # If Ctrl+Z is pressed during Move Vertex, cancel the active
            # runtime move before restoring state.
            try:
                self._cancel_active_vertex_move_runtime()
            except Exception:
                pass

            try:
                tool = getattr(self, "_corner_rotate_tool", None)
                if tool is not None and getattr(tool, "active", False):
                    tool.deactivate(cancel=False, silent=True)
            except Exception:
                pass

            # AccuDraw must handle Ctrl+Z internally while a draft is active.
            # Do not let normal Digitizer undo restore stale drawing snapshots,
            # because AccuDraw also keeps live points in accudraw_tool.points.
            if self.active_tool == "accudraw":
                accudraw_tool = getattr(self, "accudraw_tool", None)
                if accudraw_tool is not None and getattr(accudraw_tool, "active", False):
                    try:
                        if hasattr(accudraw_tool, "handle_global_undo"):
                            if accudraw_tool.handle_global_undo():
                                return
                        elif getattr(accudraw_tool, "points", None):
                            accudraw_tool.undo_last_point()
                            return
                    except Exception as e:
                        print(f"⚠️ AccuDraw internal undo failed: {e}")

            if self.active_tool == "orthopolygon":
                ortho_tool = getattr(self, "_ortho_polygon_tool", None)
                if ortho_tool and ortho_tool.undo():
                    return

            # Per-vertex undo during active drawing
            if self.active_tool in ("smartline", "line", "polyline", "hatcharea") and self.temp_points:
                if not hasattr(self, '_temp_redo_stack'):
                    self._temp_redo_stack = []
                self._temp_redo_stack.append(list(self.temp_points))
                # Cap temp redo stack
                if len(self._temp_redo_stack) > self.max_undo_levels:
                    self._temp_redo_stack.pop(0)

                if hasattr(self, '_temp_vertex_stack') and self._temp_vertex_stack:
                    self.temp_points = list(self._temp_vertex_stack.pop())
                else:
                    self.temp_points = []

                self._rebuild_active_vertex_markers()
                self._rebuild_active_preview()
                return

            # Drawing-level undo
            if not self.undo_stack:
                print("⚠️ Nothing to undo")
                return

            # Check if any classified fences will be removed by this undo
            classified_to_remove = self._get_classified_fences_removed_by_undo()
            if classified_to_remove:
                count = len(classified_to_remove)
                if count == 1:
                    msg = ("Undo operation on the fence which has been classified "
                           "is being carried out. This will delete the fence. Is it OK?")
                else:
                    msg = (f"Undo operation on {count} fences which have been classified "
                           "is being carried out. This will delete the fences. Is it OK?")
                reply = QMessageBox.question(
                    None, "Confirm Undo", msg,
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No
                )
                if reply != QMessageBox.Yes:
                    print("🚫 Undo cancelled by user — classified fence preserved")
                    return
                # Classified fence is being permanently removed — clear the redo
                # stack so the fence drawing cannot be restored via Ctrl+Y.
                # The classification itself can still be redone via the separate
                # classification redo stack (app.redo_stack).
                self.redo_stack.clear()
                print("🗑️ Redo stack cleared — classified fence will not be restorable")

            self.redo_stack.append(self._capture_state_snapshot())
            # Cap redo stack to same limit as undo
            if len(self.redo_stack) > self.max_undo_levels:
                self.redo_stack.pop(0)

            previous_state = self.undo_stack.pop()
            self._restore_state(previous_state)
            self._sync_curve_finalized_actors()
            self.clear_coordinate_labels()
            self._clear_vertex_delete_highlight()
            print(f"↶ Undo (undo stack: {len(self.undo_stack)}, redo stack: {len(self.redo_stack)})")

            # Update hatch dialog if hatch tool is active in permanent mode
            if getattr(self, 'active_tool', None) == 'hatcharea':
                has_hatch = any(d.get('type') == 'hatcharea' for d in self.drawings)
                if hasattr(self, '_hatch_dialog') and self._hatch_dialog:
                    try:
                        if not has_hatch:
                            self._hatch_dialog.set_status(
                                "Hatch undone — ready.\n"
                                "Click inside a shape to hatch it,\n"
                                "or adjust settings and click again."
                            )
                    except Exception:
                        pass
        finally:
            self._undo_in_progress = False

    def redo(self):
        """Redo previously undone operation (Ctrl+Y)."""
        # Safety: do not allow redo while Move Vertex is half-active.
        try:
            self._cancel_active_vertex_move_runtime()
        except Exception:
            pass

        try:
            tool = getattr(self, "_corner_rotate_tool", None)
            if tool is not None and getattr(tool, "active", False):
                tool.deactivate(cancel=False, silent=True)
        except Exception:
            pass

        if self.active_tool == "orthopolygon":
            ortho_tool = getattr(self, "_ortho_polygon_tool", None)
            if ortho_tool and ortho_tool.redo():
                return
        if self.active_tool in ("smartline", "line", "polyline", "hatcharea") and getattr(self, "_temp_redo_stack", None):
            if not hasattr(self, "_temp_vertex_stack"):
                self._temp_vertex_stack = []
            self._temp_vertex_stack.append(list(self.temp_points))
            self.temp_points = list(self._temp_redo_stack.pop())
            self._rebuild_active_vertex_markers()
            self._rebuild_active_preview()
            return
        if not self.redo_stack:
            print("⚠️ Nothing to redo")
            return

        self.undo_stack.append(self._capture_state_snapshot())
        # Cap undo stack
        if len(self.undo_stack) > self.max_undo_levels:
            self.undo_stack.pop(0)

        next_state = self.redo_stack.pop()
        self._restore_state(next_state)
        self._sync_curve_finalized_actors()
        self.clear_coordinate_labels()
        self._clear_vertex_delete_highlight()

        print(f"↷ Redo (undo stack: {len(self.undo_stack)}, redo stack: {len(self.redo_stack)})")



    def _unhighlight_all_lines(self):
        """Restore all highlighted lines to normal appearance."""
        self._cleanup_polygon_overlays()
        if not hasattr(self, "multi_selected"):
            return
        for d in self.multi_selected:
            if "actor" in d:
                prop = d["actor"].GetProperty()
                prop.SetColor(1, 0, 0)
                prop.SetLineWidth(2)
            if d["type"] == "line":
                if "start_marker" in d:
                    d["start_marker"].GetProperty().SetColor(0, 1, 0)
                if "end_marker" in d:
                    d["end_marker"].GetProperty().SetColor(1, 0, 0)
        self.multi_selected = []

    # ------------------------------------------------------------------
    # GIS-style polygon-closing expansion (legacy selection path)
    # ------------------------------------------------------------------
    _POLY_SNAP_PRECISION = 6

    @staticmethod
    def _poly_snap(coord):
        return (round(coord[0], DigitizeManager._POLY_SNAP_PRECISION),
                round(coord[1], DigitizeManager._POLY_SNAP_PRECISION),
                round(coord[2] if len(coord) > 2 else 0, DigitizeManager._POLY_SNAP_PRECISION))

    def _cleanup_polygon_overlays(self):
        """Remove temporary overlay actors created for polygon closing."""
        ov = getattr(self, '_polygon_overlay_actors', None)
        if not ov:
            return
        for actor in ov:
            try:
                if actor:
                    self._remove_actor_from_overlay(actor)
            except Exception:
                pass
        self._polygon_overlay_actors = []

    def _create_segment_overlay(self, p1_world, p2_world):
        """Create a highlight overlay actor for a single line segment."""
        import vtk
        points = vtk.vtkPoints()
        points.InsertNextPoint(float(p1_world[0]), float(p1_world[1]),
                               float(p1_world[2] if len(p1_world) > 2 else 0))
        points.InsertNextPoint(float(p2_world[0]), float(p2_world[1]),
                               float(p2_world[2] if len(p2_world) > 2 else 0))
        poly = vtk.vtkPolyLine()
        poly.GetPointIds().SetNumberOfIds(2)
        poly.GetPointIds().SetId(0, 0)
        poly.GetPointIds().SetId(1, 1)
        ca = vtk.vtkCellArray()
        ca.InsertNextCell(poly)
        pd = vtk.vtkPolyData()
        pd.SetPoints(points)
        pd.SetLines(ca)
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(pd)
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(0.0, 1.0, 1.0)
        actor.GetProperty().SetLineWidth(5)
        actor.PickableOff()
        self._add_actor_to_overlay(actor)
        return actor

    def _expand_legacy_selection_to_polygons(self):
        """Expand legacy selection to include segments that close open polygons.

        Operates on self.selected_drawing / self.multi_selected (the
        legacy right-click selection path).
        For multi-segment drawings (e.g. smartline rectangle) only a visual
        overlay is created for the shared segment - the parent drawing is
        NOT added to the selection.
        """
        # Clean up overlays from previous expansion
        self._cleanup_polygon_overlays()

        from collections import defaultdict as _dd

        selected = list(getattr(self, 'multi_selected', []))
        if getattr(self, 'selected_drawing', None) is not None:
            selected.append(self.selected_drawing)
        if len(selected) < 2:
            return

        selected_ids = {id(d) for d in selected}

        # segments[idx] = (snapped_p1, snapped_p2, original_p1, original_p2)
        segments = []
        seg_to_drawing = {}
        graph = _dd(list)

        for d in self.drawings:
            coords = d.get('coords') or []
            if len(coords) < 2:
                continue
            for i in range(len(coords) - 1):
                p1 = self._poly_snap(coords[i])
                p2 = self._poly_snap(coords[i + 1])
                if p1 == p2:
                    continue
                idx = len(segments)
                segments.append((p1, p2, coords[i], coords[i + 1]))
                seg_to_drawing[idx] = d
                graph[p1].append((p2, idx))
                graph[p2].append((p1, idx))

        if not segments:
            return

        selected_seg = set()
        for idx, d in seg_to_drawing.items():
            if id(d) in selected_ids:
                selected_seg.add(idx)
        if not selected_seg:
            return

        visited_segs = set()
        additional_drawings = []
        overlay_segments = []

        for start_idx in selected_seg:
            if start_idx in visited_segs:
                continue
            component = set()
            queue = [start_idx]
            while queue:
                idx = queue.pop(0)
                if idx in component:
                    continue
                component.add(idx)
                visited_segs.add(idx)
                p1, p2, _, _ = segments[idx]
                for _, nidx in graph[p1]:
                    if nidx in selected_seg and nidx not in component:
                        queue.append(nidx)
                for _, nidx in graph[p2]:
                    if nidx in selected_seg and nidx not in component:
                        queue.append(nidx)

            degree = _dd(int)
            for idx in component:
                p1, p2, _, _ = segments[idx]
                degree[p1] += 1
                degree[p2] += 1

            open_endpoints = [p for p, deg in degree.items() if deg == 1]
            if len(open_endpoints) != 2:
                continue

            ep1, ep2 = open_endpoints
            bfs_vis = {ep1}
            bfs_q = [(ep1, [], 0)]
            found = None
            MAX_DEPTH = 50

            while bfs_q and found is None:
                cur, path, depth = bfs_q.pop(0)
                if depth >= MAX_DEPTH:
                    continue
                for nbr, sidx in graph[cur]:
                    if sidx in selected_seg:
                        continue
                    new_path = path + [sidx]
                    if nbr == ep2:
                        found = new_path
                        break
                    if nbr not in bfs_vis:
                        bfs_vis.add(nbr)
                        bfs_q.append((nbr, new_path, depth + 1))

            if found is None:
                continue

            for sidx in found:
                d = seg_to_drawing[sidx]
                if id(d) in selected_ids:
                    continue
                coords = d.get('coords') or []
                if len(coords) <= 2:
                    additional_drawings.append(d)
                else:
                    _, _, p1_orig, p2_orig = segments[sidx]
                    overlay_segments.append((p1_orig, p2_orig))

        # Apply: select drawings
        for d in additional_drawings:
            if not any(d is s for s in (getattr(self, 'multi_selected', []) + [getattr(self, 'selected_drawing', None)])):
                if not hasattr(self, 'multi_selected'):
                    self.multi_selected = []
                if self.selected_drawing is not None:
                    self.multi_selected.append(self.selected_drawing)
                    self.selected_drawing = None
                self.multi_selected.append(d)
                self._highlight_line(d)

        # Apply: create visual overlays for shared segments
        if overlay_segments:
            actors = []
            for p1, p2 in overlay_segments:
                actor = self._create_segment_overlay(p1, p2)
                if actor is not None:
                    actors.append(actor)
            if actors:
                if not hasattr(self, '_polygon_overlay_actors'):
                    self._polygon_overlay_actors = []
                self._polygon_overlay_actors.extend(actors)




    def _find_nearest_vertex(self, pos, coords, threshold=10.0):
        """
        Finds the nearest vertex to the mouse click in screen space (2D), 
        ignoring Z depth — so works in 2D top view or 3D perspective reliably.
        """
        if not coords or not self.renderer or not self.interactor:
            return None, None

        min_dist = float("inf")
        nearest_idx = None

        # Get current display-space mouse position
        mouse_x, mouse_y = self.interactor.GetEventPosition()

        # Loop through vertices and project to display space
        for i, c in enumerate(coords):
            self.renderer.SetWorldPoint(c[0], c[1], c[2], 1.0)
            self.renderer.WorldToDisplay()
            disp_x, disp_y, _ = self.renderer.GetDisplayPoint()

            # Compare only X/Y (2D distance)
            dist = ((mouse_x - disp_x) ** 2 + (mouse_y - disp_y) ** 2) ** 0.5

            if dist < min_dist and dist < threshold:
                min_dist = dist
                nearest_idx = i

        if nearest_idx is not None:
            return nearest_idx, min_dist
        return None, None

    def _show_all_vertices_for_delete_mode(self):
        """Show vertex markers for ALL drawings so the user can pick one to delete."""
        self.clear_coordinate_labels()
        self.coord_labels = []
        for drawing in self.drawings:
            if drawing.get("type") == "text" or "coords" not in drawing:
                continue
            coords = drawing.get("coords")
            if coords is None or len(coords) < 2:
                continue
            for pt in coords:
                marker = self._add_endpoint_sphere(pt, color=(1.0, 1.0, 0.0), radius=None)
                self.coord_labels.append(marker)
        self.app.vtk_widget.render()
        print(f"📍 Delete-vertex mode: displayed {len(self.coord_labels)} vertex markers across {len(self.drawings)} drawings.")

    def _clear_vertex_delete_highlight(self):
        marker = getattr(self, "_vertex_highlight", None)
        if marker:
            try:
                self._remove_actor_from_overlay(marker)
            except Exception:
                pass
        self._vertex_highlight = None

    def _select_vertex_for_delete_at_position(self, x, y, tolerance=15.0):
        """Select and highlight the nearest editable drawing vertex."""
        nearest_vertex = None
        nearest_drawing = None
        nearest_idx = None
        min_dist = tolerance

        for drawing in self.drawings:
            if drawing.get("type") == "text" or "coords" not in drawing:
                continue

            coords = drawing.get("coords")
            if coords is None or len(coords) < 2:
                continue

            for idx, vertex in enumerate(coords):
                try:
                    self.renderer.SetWorldPoint(vertex[0], vertex[1], vertex[2], 1.0)
                    self.renderer.WorldToDisplay()
                    sx, sy, _ = self.renderer.GetDisplayPoint()
                except Exception:
                    continue

                dist = ((sx - x) ** 2 + (sy - y) ** 2) ** 0.5
                if dist < min_dist:
                    min_dist = dist
                    nearest_vertex = vertex
                    nearest_drawing = drawing
                    nearest_idx = idx

        self._clear_vertex_delete_highlight()

        if nearest_drawing is None:
            self.selected_vertex_idx = None
            self.selected_drawing = None
            self._unhighlight_all_lines()
            self._show_all_vertices_for_delete_mode()
            print("No nearby vertex selected")
            self.app.vtk_widget.render()
            return

        self._unhighlight_all_lines()
        self.multi_selected = []
        self.selected_drawing = nearest_drawing
        self.selected_vertex_idx = nearest_idx
        self._show_all_vertices_for_delete_mode()
        self._vertex_highlight = self._add_endpoint_sphere(
            nearest_vertex, color=(0, 1, 1), radius=0.14
        )

        print(f"Selected vertex {nearest_idx} for deletion")
        self.renderer.Modified()
        self.app.vtk_widget.render()

    def _delete_selected_vertex(self):
        """Delete the currently selected vertex in Delete Vertex mode."""
        idx = getattr(self, "selected_vertex_idx", None)
        drawing = getattr(self, "selected_drawing", None)

        if idx is None or not drawing or "coords" not in drawing:
            print("No vertex selected to delete")
            return False

        coords = drawing["coords"]
        if idx < 0 or idx >= len(coords):
            self.selected_vertex_idx = None
            self._clear_vertex_delete_highlight()
            print("Selected vertex is no longer valid")
            return False

        self._save_state()
        del coords[idx]
        self._clear_vertex_delete_highlight()
        self.selected_vertex_idx = None

        if len(coords) < 2:
            self._remove_drawing(drawing)
            self.selected_drawing = None
            self._show_all_vertices_for_delete_mode()
            print("Drawing removed because it has fewer than two vertices")
        else:
            drawing["coords"] = coords
            self._rebuild_drawing_actor(drawing)
            self._unhighlight_all_lines()
            self.selected_drawing = None
            self._show_all_vertices_for_delete_mode()
            print(f"Deleted vertex {idx}")


        self.renderer.Modified()
        self.app.vtk_widget.render()
        return True

     # ---------------- SMARTLINE HELPERS ----------------
    def _finalize_smart_line(self):
        self._save_state()
        if len(self.temp_points) < 2:
            return

        end_point     = np.array(self.temp_points[-1])
        snap_target   = None
        snap_distance = float('inf')
        # ✅ FIX: Use a FIXED small snap tolerance at finalization only.
        # so small that neighbouring legitimate vertices snap to each other,
        # collapsing a 5-vertex polygon into 2-3 vertices.
        # We use 8 pixels (half the live-snap radius) so only the cursor
        # position that is essentially ON an existing vertex gets snapped.
        world_tol = self._screen_to_world_tolerance(8)

        # ── Snap end point to an existing drawing vertex ─────────────────
        for drawing in self.drawings:
            if 'coords' in drawing:
                for vertex in drawing["coords"]:
                    vertex_arr = np.array(vertex)
                    dx = float(vertex_arr[0]) - float(end_point[0])
                    dy = float(vertex_arr[1]) - float(end_point[1])
                    dist = np.sqrt(dx * dx + dy * dy)
                    if dist < world_tol and dist < snap_distance:
                        snap_distance = dist
                        snap_target   = vertex_arr

        # ── Snap end point to own start (close the polygon) ──────────────
        # Only attempt if we have at least 3 unique interior vertices so
        # that closing does not collapse the shape.
        if len(self.temp_points) >= 4:          # 4 pts = triangle + close
            start_point = np.array(self.temp_points[0])
            dx = float(start_point[0]) - float(end_point[0])
            dy = float(start_point[1]) - float(end_point[1])
            dist_to_start = np.sqrt(dx * dx + dy * dy)
            if dist_to_start < world_tol and dist_to_start < snap_distance:
                snap_target   = start_point
                snap_distance = dist_to_start

        if snap_target is not None:
            # Only REPLACE the last point — never append
            self.temp_points[-1] = tuple(snap_target)
            if hasattr(self, '_smartline_vertex_markers') and self._smartline_vertex_markers:
                self._smartline_vertex_markers[-1].SetPosition(snap_target)

        if hasattr(self, '_smartline_vertex_markers') and self._smartline_vertex_markers:
            self._smartline_vertex_markers[-1].GetProperty().SetColor(1, 0, 0)

        # Remove preview actors
        self._remove_preview_actor_2d('_preview_line_actor')
        self._remove_preview_actor_2d('_continuous_line_actor')

        # ✅ FIX: Deduplicate ONLY truly identical consecutive points.
        # Use exact equality (atol=0) so that vertices which are merely
        # close but distinct (common at high zoom) are never merged.
        # This prevents a 5-vertex polygon from becoming 3 vertices.
        clean_points = [self.temp_points[0]]
        for pt in self.temp_points[1:]:
            prev = np.array(clean_points[-1], dtype=np.float64)
            curr = np.array(pt,              dtype=np.float64)
            if not np.array_equal(prev, curr):
                clean_points.append(pt)

        if len(clean_points) < 2:
            print("⚠️ SmartLine cancelled — not enough unique points after dedup")
            self.temp_points = []
            self._smartline_vertex_markers = []
            self._clear_temp_vertex_history()
            self.app.vtk_widget.render()
            return

        sl_style  = self._get_draw_style('smartline')
        sl_color  = sl_style['color']
        sl_width  = sl_style['width']
        sl_lstyle = sl_style['style']

        actor = self._make_polyline_actor(
            clean_points, color=sl_color, width=sl_width, line_style=sl_lstyle)
        actor.PickableOn()
        self._add_actor_to_overlay(actor)

        arrow_actor = None
        if getattr(self, 'smartline_arrow_mode', False):
            arrow_actor = self._add_arrow_to_line(clean_points)

        drawing_entry = {
            "type":           "smartline",
            "coords":         list(clean_points),
            "actor":          actor,
            "bounds":         actor.GetBounds(),
            "vertex_markers": list(self._smartline_vertex_markers)
                              if hasattr(self, '_smartline_vertex_markers') else [],
            "arrow_actor":    arrow_actor,
            "original_color": sl_color,
            "original_width": sl_width,
            "original_style": sl_lstyle,
        }

        # Fill only when the smartline forms a closed loop
        is_closed = (
            len(clean_points) >= 3
            and np.allclose(
                np.array(clean_points[0][:2], dtype=np.float64),
                np.array(clean_points[-1][:2], dtype=np.float64),
                atol=1e-6,
            )
        )
        if is_closed and sl_style.get("fill_enabled", False):
            fill_color = sl_style.get("fill_color", sl_color)
            fill_opacity = sl_style.get("fill_opacity", 0.3)
            fill_actor = self._make_fill_actor(clean_points, fill_color, fill_opacity)
            if fill_actor is not None:
                self._add_actor_to_overlay(fill_actor)
                drawing_entry["fill_actor"] = fill_actor
                drawing_entry["original_fill_color"] = fill_color
                drawing_entry["original_fill_opacity"] = fill_opacity

        # Hatch is generated Draw geometry. Keep it under DIGITIZER so Draw > Clear
        # can remove it even when an SNT/DXF/SBM layer is active.
        drawing_entry["layer"] = "DIGITIZER"
        drawing_entry.pop("snt_file", None)
        drawing_entry.pop("dxf_file", None)
        drawing_entry.pop("sbm_class", None)

        self.drawings.append(drawing_entry)
        self._emit_drawing_finalized(drawing_entry)

        print(f"✅ SmartLine finalized: {len(clean_points)} vertices")

        self._hide_drawing_markers_now(drawing_entry)
        self._hide_snap_marker_now()

        self.temp_points = []
        self._smartline_vertex_markers = []
        self._clear_temp_vertex_history()
        if not getattr(self, 'smartline_permanent_mode', True):
            self.active_tool = None
        self.renderer.Modified()
        self.app.vtk_widget.render()


    def _hide_snap_marker_now(self):
        """Hide the green snap marker immediately."""
        self._snap_hide_seq = getattr(self, "_snap_hide_seq", 0) + 1

        marker = getattr(self, "_snap_marker", None)
        if marker is not None:
            try:
                marker.SetVisibility(False)
            except Exception:
                pass


    def _schedule_hide_snap_marker(self, delay_ms=500):
        """Hide snap marker after delay, safely."""
        marker = getattr(self, "_snap_marker", None)
        if marker is None:
            return

        try:
            from PySide6.QtCore import QTimer
        except ImportError:
            from PyQt5.QtCore import QTimer

        self._snap_hide_seq = getattr(self, "_snap_hide_seq", 0) + 1
        seq = self._snap_hide_seq

        def _hide():
            if seq != getattr(self, "_snap_hide_seq", 0):
                return

            marker = getattr(self, "_snap_marker", None)
            if marker is not None:
                try:
                    marker.SetVisibility(False)
                except Exception:
                    pass

            try:
                self.app.vtk_widget.render()
            except Exception:
                pass

        QTimer.singleShot(delay_ms, _hide)


    def _hide_drawing_markers_now(self, drawing_entries=None):
        """
        Hide completed drawing markers:
        - smartline/polyline vertex_markers
        - line segment start_marker/end_marker
        """
        if drawing_entries is None:
            drawing_entries = list(getattr(self, "drawings", []))

        if isinstance(drawing_entries, dict):
            drawing_entries = [drawing_entries]

        any_hidden = False

        for d in drawing_entries:
            if not isinstance(d, dict):
                continue

            # smartline / polyline markers
            for marker in d.get("vertex_markers", []) or []:
                try:
                    if marker and marker.GetVisibility():
                        marker.SetVisibility(False)
                        any_hidden = True
                except Exception:
                    pass

            # line segment endpoint markers
            for key in ("start_marker", "end_marker"):
                marker = d.get(key)
                if marker is not None:
                    try:
                        if marker.GetVisibility():
                            marker.SetVisibility(False)
                            any_hidden = True
                    except Exception:
                        pass

        if any_hidden:
            try:
                self.app.vtk_widget.render()
            except Exception:
                pass


    def _schedule_vertex_marker_hide(self, drawing_entries, delay_ms=500):
        """
        Hide completed drawing vertex/end markers after delay.
        This handles:
        - smartline/polyline: vertex_markers
        - line: start_marker/end_marker
        Also hides snap marker after same delay.
        """
        try:
            from PySide6.QtCore import QTimer
        except ImportError:
            from PyQt5.QtCore import QTimer

        def _hide_markers():
            self._hide_drawing_markers_now(drawing_entries)
            self._hide_snap_marker_now()
            try:
                self.app.vtk_widget.render()
            except Exception:
                pass

        QTimer.singleShot(delay_ms, _hide_markers)
            
        
    def _finalize_line(self):
        if len(self.temp_points) < 2:
            return
        self._save_state()

        if hasattr(self, '_line_vertex_markers') and self._line_vertex_markers:
            self._line_vertex_markers[-1].GetProperty().SetColor(1, 0, 0)

        all_points = list(self.temp_points)
        self.temp_points = []

        # ✅ FIX: Remove 2D preview actors
        self._remove_preview_actor_2d('_preview_line_actor')
        self._remove_preview_actor_2d('_continuous_line_actor')

        ln_style = self._get_draw_style('line')
        ln_color = ln_style['color']
        ln_width = ln_style['width']
        ln_lstyle = ln_style['style']

        arrow_enabled = getattr(self, 'line_arrow_mode', False)

        created_line_entries = []

        for i in range(len(all_points) - 1):
            p1, p2 = all_points[i], all_points[i + 1]
            coords = [p1, p2]
            actor = self._make_polyline_actor(coords, color=ln_color, width=ln_width, line_style=ln_lstyle)
            self._add_actor_to_overlay(actor)

            arrow_actor = None
            if arrow_enabled:
                arrow_actor = self._add_arrow_to_line(coords)

            start_marker = self._line_vertex_markers[i] if i < len(self._line_vertex_markers) else None
            end_marker = self._line_vertex_markers[i + 1] if (i + 1) < len(self._line_vertex_markers) else None

            drawing_entry = {
                "type": "line_segment",
                "coords": coords,
                "actor": actor,
                "bounds": actor.GetBounds(),
                "start_marker": start_marker,
                "end_marker": end_marker,
                "arrow_actor": arrow_actor,
                "original_color": ln_color,
                "original_width": ln_width,
                "original_style": ln_lstyle,
            }
            self._tag_sbm(drawing_entry)
            self.drawings.append(drawing_entry)
            self._emit_drawing_finalized(drawing_entry)
            created_line_entries.append(drawing_entry)

        # Hide the endpoint spheres immediately so the normal Line tool
        # behaves like SmartLine/Polyline finalization in the main view.
        # The delayed timer remains as a safety net in case another callback
        # briefly rebinds the markers during the same event cycle.
        self._hide_drawing_markers_now(created_line_entries)

        # ✅ Hide line endpoint markers after 3 seconds
        self._schedule_vertex_marker_hide(created_line_entries, 500)

        # REPLACE WITH:
        self._line_vertex_markers = []
        self._clear_temp_vertex_history()
        if not getattr(self, 'line_permanent_mode', True):
            self.active_tool = None
        self.renderer.Modified()
        self.app.vtk_widget.render()
        print(f"✅ Created {len(all_points) - 1} independent line segments.")

    def _cancel_smart_line(self):
        # ✅ Hide snap marker immediately on cancel/deactivate
        self._hide_snap_marker_now()

        # ✅ FIX: All preview actors are now 2D
        self._remove_preview_actor_2d('_preview_line_actor')
        self._remove_preview_actor_2d('_continuous_line_actor')

        if hasattr(self, "_line_start_marker") and self._line_start_marker:
            try:
                self._remove_actor_from_overlay(self._line_start_marker)
            except Exception:
                pass
            self._line_start_marker = None

        # Clear all in-progress vertex markers
        for attr in ['_smartline_vertex_markers', '_line_vertex_markers', '_polyline_vertex_markers']:
            markers = getattr(self, attr, [])
            for m in markers:
                try:
                    self._remove_actor_from_overlay(m)
                except Exception:
                    pass
            setattr(self, attr, [])

        self.temp_points = []
        self._clear_temp_vertex_history()
        self.app.vtk_widget.render()
        print("❌ Line/SmartLine cancelled")


    def _finalize_polyline(self):
        if len(self.temp_points) < 3:
            print("⚠️ Polyline needs at least 3 points")
            return
        self._save_state()

        if hasattr(self, '_polyline_vertex_markers') and self._polyline_vertex_markers:
            self._polyline_vertex_markers[-1].GetProperty().SetColor(1, 0, 0)

        all_points = list(self.temp_points)
        if not np.array_equal(all_points[0], all_points[-1]):
            all_points.append(all_points[0])
        self.temp_points = []

        # ✅ FIX: Remove 2D preview actors
        self._remove_preview_actor_2d('_preview_line_actor')
        self._remove_preview_actor_2d('_continuous_line_actor')

        pl_style = self._get_draw_style('polyline')
        pl_color = pl_style['color']
        pl_width = pl_style['width']
        pl_lstyle = pl_style['style']

        actor = self._make_polyline_actor(all_points, color=pl_color, width=pl_width, line_style=pl_lstyle)
        self._add_actor_to_overlay(actor)

        drawing_entry = {
            "type": "polyline",
            "coords": all_points,
            "actor": actor,
            "bounds": actor.GetBounds(),
            "vertex_markers": list(self._polyline_vertex_markers) if hasattr(self, '_polyline_vertex_markers') else [],
            "original_color": pl_color,
            "original_width": pl_width,
            "original_style": pl_lstyle,
        }

        if pl_style.get("fill_enabled", False):
            fill_color = pl_style.get("fill_color", pl_color)
            fill_opacity = pl_style.get("fill_opacity", 0.3)
            fill_actor = self._make_fill_actor(all_points, fill_color, fill_opacity)
            if fill_actor is not None:
                self._add_actor_to_overlay(fill_actor)
                drawing_entry["fill_actor"] = fill_actor
                drawing_entry["original_fill_color"] = fill_color
                drawing_entry["original_fill_opacity"] = fill_opacity

        self._tag_sbm(drawing_entry)
        self.drawings.append(drawing_entry)
        self._emit_drawing_finalized(drawing_entry)

        # Hide vertex markers immediately on finalization.
        for m in drawing_entry["vertex_markers"]:
            try:
                m.VisibilityOff()
            except Exception:
                pass

        self._schedule_hide_snap_marker(500)

        self._polyline_vertex_markers = []
        self._clear_temp_vertex_history()
        self.renderer.Modified()
        self.app.vtk_widget.render()
        print(f"✅ Polyline finalized: {len(all_points)} vertices (closed polygon)")

        if not getattr(self, 'polyline_permanent_mode', False):
            self.active_tool = None


    # ---------------- SELECTION / EDITING ----------------
    def _pick_actor(self, x, y):
        """
        Hybrid picker: supports both geometric and billboard/text actors.
        """
        actor = None
        
        # 1️⃣ Try PropPicker FIRST for text or billboard props
        self._prop_picker.Pick(x, y, 0, self.renderer)
        prop_actor = self._prop_picker.GetViewProp()
        if prop_actor and isinstance(prop_actor, vtk.vtkBillboardTextActor3D):
            return prop_actor
            
        # 2️⃣ Try CellPicker for geometric shapes
        self._cell_picker.Pick(x, y, 0, self.renderer)
        actor = self._cell_picker.GetActor()

        return actor


    def _select_actor(self, actor):
        self._clear_selection()
        self.selected = next((d for d in self.drawings if d["actor"] == actor), None)
        if not self.selected:
            return

        # Highlight line (only geometry has GetProperty, text has GetTextProperty and doesn't need line width changes)
        if hasattr(self.selected["actor"], "GetProperty"):
            prop = self.selected["actor"].GetProperty()
            if hasattr(prop, "SetLineWidth"):
                prop.SetLineWidth(5)
                prop.SetColor(0, 1, 1)

        self.selection_vertices = []

        for pt in self.selected["coords"]:
            marker = self._add_endpoint_sphere(pt, color=(1, 1, 0), radius=4.0)
            self.selection_vertices.append(marker)
        
        self.app.vtk_widget.render()

    def _clear_selection(self):
        if self.selected:
            prop = self.selected["actor"].GetProperty() if hasattr(self.selected["actor"], "GetProperty") else None
            if prop:
                prop.SetColor(1, 0, 0)
                prop.SetLineWidth(2)
        for v in getattr(self, "selection_vertices", []):
            try:
                self._remove_actor_from_overlay(v)
            except Exception:
                pass
        self.selection_vertices = []
        self.app.vtk_widget.render()


    def _translate_selected(self, delta):
        coords = [tuple(np.array(c) + delta) for c in self.selected["coords"]]
        self.selected["coords"] = coords
        self._remove_actor_from_overlay(self.selected["actor"])
        self.selected["actor"] = self._make_polyline_actor(coords)
        self._add_actor_to_overlay(self.selected["actor"])
        self.app.vtk_widget.render()

    def _delete_selected(self):
        if not self.selected:
            return
        try:
            self._remove_drawing(self.selected)
            print("🗑️ Deleted selected vector")
        except Exception as e:
            print(f"⚠️ Delete failed: {e}")
        self.selected = None
        self.app.vtk_widget.render()

    def _copy_selected(self):
        if not self.selected:
            return
        new_coords = [tuple(np.array(c) + np.array([1, 1, 0])) for c in self.selected["coords"]]
        new_actor = self._make_polyline_actor(new_coords)
        self._add_actor_to_overlay(new_actor)
        copy = {"type": self.selected["type"], "coords": new_coords, "actor": new_actor}
        self.drawings.append(copy)
        self.app.vtk_widget.render()
        print("📄 Copied selected")
    

    def _make_polyline_actor(self, points, color=(1, 0, 0), width=2, line_style='solid'):
        """
        ✅ BULLETPROOF 3D LINE
        Uses 3D mapping so it pans correctly, but relies on OpenGL LineWidth
        so thickness never scales with zoom.

        ✅ ZOOM FIX: _build_styled_polydata_world re-centres geometry around
        points[0] and tags poly._world_origin.  We apply that offset via
        actor.SetPosition() so the actor lands in the correct world location
        at any zoom level — no shape distortion at maximum zoom.
        """
        if len(points) < 2:
            return None

        # 1. Geometry — re-centred for float64 precision at high zoom
        polydata = self._build_styled_polydata_world(points, line_style=line_style)

        # ✅ Retrieve the re-centring origin written by _build_styled_polydata_world
        origin = getattr(polydata, '_world_origin', np.zeros(3, dtype=np.float64))

        # 2. 3D Mapper
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(polydata)
        mapper.SetResolveCoincidentTopologyToPolygonOffset()
        mapper.SetRelativeCoincidentTopologyLineOffsetParameters(-3, -3)

        # 3. 3D Actor
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(color)
        actor.GetProperty().SetLineWidth(width)
        line_style = self._normalize_line_style(line_style)
        self._apply_world_line_style(actor.GetProperty(), line_style=line_style)
        actor._dxf_line_width = float(width)
        actor._dxf_line_style = line_style
        actor._snt_line_width = float(width)
        actor._snt_line_style = line_style

        # ✅ Shift actor back to true world position
        actor.SetPosition(float(origin[0]), float(origin[1]), float(origin[2]))
        actor._digitize_overlay = True
        return actor

    def _make_hatch_segments_actors(self, segments, color, width, line_style='solid'):
        """Build actor(s) for a batch of disconnected hatch fill-line segments.

        Solid-style hatch (the common case) is merged into a SINGLE actor
        instead of one actor per segment. Large areas (e.g. a whole SNT/DXF
        grid cell picked via the Hatch Block-select method) can generate
        hundreds of fill lines -- building/adding hundreds of separate VTK
        actors is what was causing the noticeable delay when finalizing or
        rebuilding those hatches. Non-solid styles keep the old per-segment
        path since dash/dot patterns are generated per-line.
        """
        if not segments:
            return []

        normalized_style = self._normalize_line_style(line_style)
        if normalized_style != 'solid':
            actors = []
            for seg in segments:
                sa = self._make_polyline_actor([seg[0], seg[1]], color=color, width=width, line_style=line_style)
                if sa:
                    actors.append(sa)
            return actors

        try:
            origin = np.array(
                [float(segments[0][0][0]), float(segments[0][0][1]),
                 float(segments[0][0][2]) if len(segments[0][0]) > 2 else 0.0],
                dtype=np.float64,
            )
            points = vtk.vtkPoints()
            lines = vtk.vtkCellArray()
            idx = 0
            for seg in segments:
                for pt in (seg[0], seg[1]):
                    z = float(pt[2]) if len(pt) > 2 else 0.0
                    points.InsertNextPoint(float(pt[0]) - origin[0], float(pt[1]) - origin[1], z - origin[2])
                line = vtk.vtkLine()
                line.GetPointIds().SetId(0, idx)
                line.GetPointIds().SetId(1, idx + 1)
                lines.InsertNextCell(line)
                idx += 2

            polydata = vtk.vtkPolyData()
            polydata.SetPoints(points)
            polydata.SetLines(lines)

            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputData(polydata)
            mapper.SetResolveCoincidentTopologyToPolygonOffset()
            mapper.SetRelativeCoincidentTopologyLineOffsetParameters(-3, -3)

            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(color)
            actor.GetProperty().SetLineWidth(width)
            self._apply_world_line_style(actor.GetProperty(), line_style=normalized_style)
            actor._dxf_line_width = float(width)
            actor._dxf_line_style = normalized_style
            actor._snt_line_width = float(width)
            actor._snt_line_style = normalized_style
            actor.SetPosition(float(origin[0]), float(origin[1]), float(origin[2]))
            actor._digitize_overlay = True
            return [actor]
        except Exception as e:
            print(f"Hatch merged-actor build failed, falling back to per-segment: {e}")
            actors = []
            for seg in segments:
                sa = self._make_polyline_actor([seg[0], seg[1]], color=color, width=width, line_style=line_style)
                if sa:
                    actors.append(sa)
            return actors

    def _make_fill_actor(self, coords, fill_color, fill_opacity):
        """Create a filled polygon VTK actor from closed world-space coordinates."""
        pts = list(coords)
        if len(pts) > 1:
            p0 = np.array(pts[0][:2], dtype=np.float64)
            pn = np.array(pts[-1][:2], dtype=np.float64)
            if np.linalg.norm(p0 - pn) < 1e-9:
                pts = pts[:-1]
        if len(pts) < 3:
            return None

        center = np.mean([[float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 0.0] for p in pts], axis=0)

        points = vtk.vtkPoints()
        polygon = vtk.vtkPolygon()
        polygon.GetPointIds().SetNumberOfIds(len(pts))
        for i, pt in enumerate(pts):
            points.InsertNextPoint(
                float(pt[0]) - center[0],
                float(pt[1]) - center[1],
                (float(pt[2]) if len(pt) > 2 else 0.0) - center[2],
            )
            polygon.GetPointIds().SetId(i, i)

        cells = vtk.vtkCellArray()
        cells.InsertNextCell(polygon)

        poly = vtk.vtkPolyData()
        poly.SetPoints(points)
        poly.SetPolys(cells)

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(poly)
        mapper.SetResolveCoincidentTopologyToPolygonOffset()
        mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(1, 1)

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        # Pre-multiply color against the black canvas background so overlapping
        # fills of the same color always look identical — no alpha-stacking.
        opacity = float(fill_opacity)
        prop.SetColor(
            float(fill_color[0]) * opacity,
            float(fill_color[1]) * opacity,
            float(fill_color[2]) * opacity,
        )
        prop.SetOpacity(1.0)
        prop.SetRepresentationToSurface()
        prop.SetAmbient(1.0)
        prop.SetDiffuse(0.0)
        prop.SetSpecular(0.0)
        prop.LightingOff()
        actor.PickableOff()
        actor.SetPosition(float(center[0]), float(center[1]), float(center[2]))
        actor._digitize_overlay = True
        return actor

    # ============================================================
    # ✅ THE FIX: Single source of truth for actor routing
    # ALL drawing actors go through overlay_renderer ONLY.
    # ============================================================

    def _add_actor_to_overlay(self, actor):
        """
        Route drawing geometry to vector layer 2 and 3D text to text layer 3.

        Each pass preserves the lower layers' color and clears their depth, so the
        scene order is determined centrally rather than by per-actor Z patches.
        """
        # vtkTextActor3D has no depth-test property; use the dedicated text pass.
        if isinstance(actor, vtk.vtkTextActor3D):
            if hasattr(self, 'text_overlay_renderer') and self.text_overlay_renderer:
                self.text_overlay_renderer.AddActor(actor)
                return
            # fallback: add to overlay and hope Z-offset is enough
            if hasattr(self, 'overlay_renderer') and self.overlay_renderer:
                self.overlay_renderer.AddActor(actor)
            else:
                self.renderer.AddActor(actor)
            return

        if hasattr(self, 'overlay_renderer') and self.overlay_renderer:
            # Keep legacy depth disabling for coincident vector geometry within
            # this pass; cross-role ordering is handled by the compositor.
            try:
                actor.GetProperty().SetDepthTestingEnabled(False)
            except AttributeError:
                # VTK < 8.2 fallback: use a very aggressive polygon/line offset so
                # the actor pushes to the very front of the depth range.  This is
                # less reliable than disabling the test outright but better than
                # nothing on older VTK builds.
                try:
                    mapper = actor.GetMapper()
                    if mapper is not None:
                        mapper.SetResolveCoincidentTopologyToPolygonOffset()
                        mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(
                            -1e5, -1e5)
                        try:
                            mapper.SetRelativeCoincidentTopologyLineOffsetParameters(
                                -1e5, -1e5)
                        except Exception:
                            pass
                except Exception:
                    pass
            self.overlay_renderer.AddActor(actor)
        else:
            self.renderer.AddActor(actor)   # fallback

    def _add_hatch_actor(self, actor):
        """Add a hatch actor to text layer 3 so it always renders above labels.

        Layer 3 clears depth before rendering,
        guaranteeing hatch lines are never occluded by point cloud or grid label
        geometry -- regardless of world-Z elevation.
        """
        if isinstance(actor, vtk.vtkTextActor3D):
            if hasattr(self, 'text_overlay_renderer') and self.text_overlay_renderer:
                self.text_overlay_renderer.AddActor(actor)
                return
            if hasattr(self, 'overlay_renderer') and self.overlay_renderer:
                self.overlay_renderer.AddActor(actor)
            else:
                self.renderer.AddActor(actor)
            return

        if hasattr(self, 'text_overlay_renderer') and self.text_overlay_renderer:
            try:
                actor.GetProperty().SetDepthTestingEnabled(False)
            except AttributeError:
                pass
            self.text_overlay_renderer.AddActor(actor)
        elif hasattr(self, 'overlay_renderer') and self.overlay_renderer:
            try:
                actor.GetProperty().SetDepthTestingEnabled(False)
            except AttributeError:
                pass
            self.overlay_renderer.AddActor(actor)
        else:
            self.renderer.AddActor(actor)

    def _remove_actor_from_overlay(self, actor):
        """Remove digitize actor from overlay renderer."""
        if hasattr(self, 'overlay_renderer') and self.overlay_renderer:
            self.overlay_renderer.RemoveActor(actor)
        if hasattr(self, 'text_overlay_renderer') and self.text_overlay_renderer:
            try:
                self.text_overlay_renderer.RemoveActor(actor)
            except Exception:
                pass
        # Also try base renderer as safety net (handles legacy actors)
        try:
            self.renderer.RemoveActor(actor)
        except Exception:
            pass

    def _remove_preview_actor_2d(self, attr_name):
        """Safely remove a transient preview actor from any renderer layer."""
        actor = getattr(self, attr_name, None)
        if actor is not None:
            try:
                actor.VisibilityOff()
            except Exception:
                pass
            try:
                if hasattr(self, 'overlay_renderer') and self.overlay_renderer:
                    self.overlay_renderer.RemoveActor(actor)
            except Exception:
                pass
            try:
                if hasattr(self, 'text_overlay_renderer') and self.text_overlay_renderer:
                    self.text_overlay_renderer.RemoveActor(actor)
            except Exception:
                pass
            try:
                self.renderer.RemoveActor2D(actor)
            except Exception:
                pass
            try:
                self.renderer.RemoveActor(actor)
            except Exception:
                pass
            try:
                self.renderer.RemoveViewProp(actor)
            except Exception:
                pass
            setattr(self, attr_name, None) 

    def deactivate_all(self):
        """Exit the current digitize tool while preserving completed drawings."""

        # AccuDraw can remain active even when active_tool is None.
        # So ESC/deactivate_all must check accudraw_tool.active directly.
        accudraw_tool = getattr(self, "accudraw_tool", None)
        if accudraw_tool is not None and getattr(accudraw_tool, "active", False):
            try:
                if hasattr(accudraw_tool, "finish_for_tool_switch"):
                    accudraw_tool.finish_for_tool_switch("ESC")
                else:
                    has_unfinished = bool(
                        getattr(accudraw_tool, "points", None)
                        or getattr(accudraw_tool, "drawing", None) is not None
                        or getattr(accudraw_tool, "preview_actor", None) is not None
                    )
                    accudraw_tool.deactivate(cancel=has_unfinished)

                self.active_tool = None
                self.temp_points = []
                self.left_down = False
                self.middle_down = False

                try:
                    if hasattr(self, "clear_coordinate_labels"):
                        self.clear_coordinate_labels()
                except Exception:
                    pass

                try:
                    if hasattr(self, "_hide_snap_marker_now"):
                        self._hide_snap_marker_now()
                except Exception:
                    pass

                try:
                    if hasattr(self, "_hide_drawing_markers_now"):
                        self._hide_drawing_markers_now()
                except Exception:
                    pass

                try:
                    if hasattr(self.app, "set_cross_cursor_active"):
                        self.app.set_cross_cursor_active(False, "draw")
                except Exception:
                    pass

                try:
                    self.app.vtk_widget.render()
                except Exception:
                    pass

                print("✅ AccuDraw exited by ESC — tool deactivated")
                return True

            except Exception as e:
                print(f"⚠️ AccuDraw ESC deactivate failed: {e}")

        if self._element_select_tool is not None:
            try:
                if self._element_select_tool.is_active():
                    self._element_select_tool.deactivate()
                    return True
            except Exception as e:
                print(f"Element Select deactivate failed: {e}")

        if self._deactivate_active_tool_keep_drawings():
            return True

        self._clear_live_preview_actors()
        self._clear_suspended_preview_actor()

        # ✅ Hide any remaining snap / vertex markers
        self._hide_snap_marker_now()
        self._hide_drawing_markers_now()

        self.temp_points = []
        self.left_down = False
        self.middle_down = False
        self._clear_temp_vertex_history()

        if hasattr(self.app, 'set_cross_cursor_active'):
            self.app.set_cross_cursor_active(False, "draw")

        try:
            self.app.vtk_widget.render()
        except Exception:
            pass

        print("Digitizer already idle")
        return False

    def disable_all_tools(self):
        """Disable digitizer interaction entirely, e.g. while the app is in 3D view."""
        self.deactivate_all()

        if hasattr(self.app, 'measurement_tool') and self.app.measurement_tool:
            try:
                self.app.measurement_tool.deactivate()
            except Exception as e:
                print(f"Failed to deactivate measurement tool while disabling digitizer: {e}")

        self.enabled = False
        if hasattr(self.app, 'set_cross_cursor_active'):
            self.app.set_cross_cursor_active(False, "draw")
        print("Digitizer DISABLED")

    def _build_preview_polydata_screen(self, screen_points, line_style='solid'):
        """Build screen-space preview geometry, including real dashed/dotted segments."""
        poly = vtk.vtkPolyData()
        pts = vtk.vtkPoints()
        lines = vtk.vtkCellArray()

        if line_style == 'solid':
            for p in screen_points:
                pts.InsertNextPoint(float(p[0]), float(p[1]), 0.0)

            n = pts.GetNumberOfPoints()
            lines.InsertNextCell(n)
            for i in range(n):
                lines.InsertCellPoint(i)

            poly.SetPoints(pts)
            poly.SetLines(lines)
            poly.Modified()
            return poly

        if line_style == 'dashed':
            dash_pattern = [(10.0, 6.0)]
        elif line_style == 'dotted':
            dash_pattern = [(2.0, 6.0)]
        elif line_style == 'dash-dot':
            dash_pattern = [(10.0, 6.0), (2.0, 6.0)]
        elif line_style == 'dash-dot-dot':
            dash_pattern = [(10.0, 6.0), (2.0, 4.0), (2.0, 6.0)]
        else:
            dash_pattern = [(10.0, 6.0)]

        point_idx = 0
        for seg_idx in range(len(screen_points) - 1):
            p1 = np.array(screen_points[seg_idx], dtype=np.float64)
            p2 = np.array(screen_points[seg_idx + 1], dtype=np.float64)

            edge_vec = p2 - p1
            edge_len = np.linalg.norm(edge_vec)
            if edge_len < 1e-6:
                continue

            direction = edge_vec / edge_len
            t = 0.0
            pattern_idx = 0

            while t < edge_len:
                dash_length, gap_length = dash_pattern[pattern_idx]
                dash_end_t = min(t + dash_length, edge_len)
                if dash_end_t - t <= 1e-6:
                    break

                dash_start = p1 + direction * t
                dash_end = p1 + direction * dash_end_t

                pts.InsertNextPoint(float(dash_start[0]), float(dash_start[1]), 0.0)
                start_idx = point_idx
                point_idx += 1

                pts.InsertNextPoint(float(dash_end[0]), float(dash_end[1]), 0.0)
                end_idx = point_idx
                point_idx += 1

                lines.InsertNextCell(2)
                lines.InsertCellPoint(start_idx)
                lines.InsertCellPoint(end_idx)

                t = dash_end_t + gap_length
                pattern_idx = (pattern_idx + 1) % len(dash_pattern)

        poly.SetPoints(pts)
        poly.SetLines(lines)
        poly.Modified()
        return poly

    def _update_preview_actor_screen(self, attr_name, screen_points, color=(0,1,0), width=3, line_style='solid'):
        """
        ANTI-BLINK: Reuse existing actor, just swap polydata in-place.
        No Remove+Add per frame = zero flicker.
        """
        if not screen_points or len(screen_points) < 2:
            self._remove_preview_actor_2d(attr_name)
            return

        poly = self._build_preview_polydata_screen(screen_points, line_style=line_style)

        actor = getattr(self, attr_name, None)

        if actor is None:
            mapper = vtk.vtkPolyDataMapper2D()
            mapper.SetInputData(poly)

            actor = vtk.vtkActor2D()
            actor.SetMapper(mapper)
            prop = actor.GetProperty()
            prop.SetColor(float(color[0]), float(color[1]), float(color[2]))
            prop.SetLineWidth(float(width))
            prop.SetOpacity(1.0)
            prop.SetDisplayLocationToForeground()

            self.renderer.AddActor2D(actor)
            setattr(self, attr_name, actor)
        else:
            mapper = actor.GetMapper()
            mapper.SetInputData(poly)
            mapper.Modified()
            prop = actor.GetProperty()
            prop.SetColor(float(color[0]), float(color[1]), float(color[2]))
            prop.SetLineWidth(float(width))
            actor.SetVisibility(1)
        return

        pts = vtk.vtkPoints()
        for p in screen_points:
            pts.InsertNextPoint(float(p[0]), float(p[1]), 0.0)

        n = pts.GetNumberOfPoints()
        cell = vtk.vtkCellArray()
        cell.InsertNextCell(n)
        for i in range(n):
            cell.InsertCellPoint(i)

        poly = vtk.vtkPolyData()
        poly.SetPoints(pts)
        poly.SetLines(cell)
        # ✅ CRITICAL: must call Modified() so VTK pipeline knows data changed
        poly.Modified()

        actor = getattr(self, attr_name, None)

        if actor is None:
            mapper = vtk.vtkPolyDataMapper2D()
            mapper.SetInputData(poly)

            actor = vtk.vtkActor2D()
            actor.SetMapper(mapper)
            prop = actor.GetProperty()
            prop.SetColor(float(color[0]), float(color[1]), float(color[2]))
            prop.SetLineWidth(float(width))
            prop.SetOpacity(1.0)
            prop.SetDisplayLocationToForeground()

            self.renderer.AddActor2D(actor)
            setattr(self, attr_name, actor)
        else:
            # ✅ Swap data in-place — actor never leaves renderer
            mapper = actor.GetMapper()
            mapper.SetInputData(poly)
            # ✅ CRITICAL: force mapper to re-read new polydata
            mapper.Modified()
            actor.GetProperty().SetColor(float(color[0]), float(color[1]), float(color[2]))
            actor.GetProperty().SetLineWidth(float(width))
            actor.SetVisibility(1)###
    
    def _add_endpoint_sphere(self, position, color=(1, 1, 0), radius=None):
        """
        ✅ TRUE BULLETPROOF SCREEN VERTEX
        Uses native VTK 3D points. PointSize is fixed in screen pixels 
        and mathematically ignores camera zoom natively.
        """
        pts = vtk.vtkPoints()
        pts.SetDataTypeToDouble()
        pts.InsertNextPoint(position[0], position[1], position[2])

        verts = vtk.vtkCellArray()
        verts.InsertNextCell(1)
        verts.InsertCellPoint(0)

        poly = vtk.vtkPolyData()
        poly.SetPoints(pts)
        poly.SetVerts(verts)

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(poly)
        
        mapper.SetResolveCoincidentTopologyToPolygonOffset()
        mapper.SetRelativeCoincidentTopologyPointOffsetParameter(-4)

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)

        prop = actor.GetProperty()
        prop.SetColor(color)
        prop.SetOpacity(1.0)
        prop.SetPointSize(12.0)
        prop.SetRenderPointsAsSpheres(True)

        # Route vertex markers to text layer 3. It clears depth before rendering,
        # so markers stay visible over the shaded mesh at identical world coordinates.
        if hasattr(self, 'text_overlay_renderer') and self.text_overlay_renderer:
            actor.PickableOff()
            self.text_overlay_renderer.AddActor(actor)
        else:
            self._add_actor_to_overlay(actor)
        return actor

    # ============================================================
    # PATCH FIX 4: Enhanced _make_scalable_text_actor
    # ============================================================
    def _resolve_font_file(self, font_family, bold=False, italic=False):
        """
        Resolve selected UI font family to the real Windows font file.

        First uses the Windows installed-font registry, so fonts shown in the
        dropdown such as Bauhaus 93, Corbel, Candara, Consolas, etc. can map
        to their real .ttf/.otf/.ttc file instead of falling back to Arial.
        """
        family = (font_family or "Arial").strip()
        family_lower = family.lower()
        fonts_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")

        def _norm(value):
            return "".join(ch for ch in str(value).lower() if ch.isalnum())

        def _clean_registry_name(value_name):
            name = str(value_name)
            for token in (
                "(TrueType)", "(OpenType)", "(Type 1)", "(Raster)",
                "TrueType", "OpenType", "Regular", "Bold Italic",
                "Bold Oblique", "Bold", "Italic", "Oblique"
            ):
                name = name.replace(token, "")
            return " ".join(name.split()).strip()

        def _style_rank(value_name):
            n = str(value_name).lower()
            has_bold = "bold" in n
            has_italic = "italic" in n or "oblique" in n

            if bold and italic:
                if has_bold and has_italic:
                    return 0
                if has_italic:
                    return 1
                if has_bold:
                    return 2
                return 3

            if bold:
                if has_bold and not has_italic:
                    return 0
                if has_bold:
                    return 1
                return 2

            if italic:
                if has_italic and not has_bold:
                    return 0
                if has_italic:
                    return 1
                return 2

            if not has_bold and not has_italic:
                return 0
            return 5

        def _candidate_path(filename):
            if not filename:
                return None
            filename = str(filename).strip().strip("\x00")
            if not filename:
                return None

            if os.path.isabs(filename):
                path = filename
            else:
                path = os.path.join(fonts_dir, filename)

            if os.path.exists(path) and path.lower().endswith((".ttf", ".ttc", ".otf")):
                return path
            return None

        # 1) Windows registry: source of truth for installed font family -> file.
        try:
            import winreg

            registry_roots = [
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
            ]

            matches = []
            requested_norm = _norm(family)

            for root, subkey in registry_roots:
                try:
                    with winreg.OpenKey(root, subkey) as key:
                        count = winreg.QueryInfoKey(key)[1]
                        for i in range(count):
                            try:
                                value_name, value_data, _ = winreg.EnumValue(key, i)
                            except OSError:
                                continue

                            display_name = _clean_registry_name(value_name)
                            display_norm = _norm(display_name)
                            raw_norm = _norm(value_name)

                            if (
                                display_norm == requested_norm
                                or raw_norm == requested_norm
                                or requested_norm in display_norm
                                or requested_norm in raw_norm
                                or display_norm in requested_norm
                            ):
                                path = _candidate_path(value_data)
                                if path:
                                    matches.append((_style_rank(value_name), display_name, path, value_name))
                except Exception:
                    continue

            if matches:
                matches.sort(key=lambda item: item[0])
                return matches[0][2]
        except Exception:
            pass

        # 2) Known Windows filename mapping fallback.
        font_files = {
            "arial": {
                "regular": ["arial.ttf"],
                "bold": ["arialbd.ttf"],
                "italic": ["ariali.ttf"],
                "bold_italic": ["arialbi.ttf"],
            },
            "arial black": {
                "regular": ["ariblk.ttf"],
                "bold": ["ariblk.ttf"],
                "italic": ["ariblk.ttf"],
                "bold_italic": ["ariblk.ttf"],
            },
            "arial narrow": {
                "regular": ["arialn.ttf"],
                "bold": ["arialnb.ttf", "arialn.ttf"],
                "italic": ["arialni.ttf", "arialn.ttf"],
                "bold_italic": ["arialnbi.ttf", "arialnb.ttf", "arialn.ttf"],
            },
            "times new roman": {
                "regular": ["times.ttf"],
                "bold": ["timesbd.ttf"],
                "italic": ["timesi.ttf"],
                "bold_italic": ["timesbi.ttf"],
            },
            "times": {
                "regular": ["times.ttf"],
                "bold": ["timesbd.ttf"],
                "italic": ["timesi.ttf"],
                "bold_italic": ["timesbi.ttf"],
            },
            "georgia": {
                "regular": ["georgia.ttf"],
                "bold": ["georgiab.ttf"],
                "italic": ["georgiai.ttf"],
                "bold_italic": ["georgiaz.ttf"],
            },
            "palatino linotype": {
                "regular": ["pala.ttf"],
                "bold": ["palab.ttf"],
                "italic": ["palai.ttf"],
                "bold_italic": ["palabi.ttf"],
            },
            "courier new": {
                "regular": ["cour.ttf"],
                "bold": ["courbd.ttf"],
                "italic": ["couri.ttf"],
                "bold_italic": ["courbi.ttf"],
            },
            "courier": {
                "regular": ["cour.ttf"],
                "bold": ["courbd.ttf"],
                "italic": ["couri.ttf"],
                "bold_italic": ["courbi.ttf"],
            },
            "lucida console": {
                "regular": ["lucon.ttf"],
                "bold": ["lucon.ttf"],
                "italic": ["lucon.ttf"],
                "bold_italic": ["lucon.ttf"],
            },
            "verdana": {
                "regular": ["verdana.ttf"],
                "bold": ["verdanab.ttf"],
                "italic": ["verdanai.ttf"],
                "bold_italic": ["verdanaz.ttf"],
            },
            "tahoma": {
                "regular": ["tahoma.ttf"],
                "bold": ["tahomabd.ttf"],
                "italic": ["tahoma.ttf"],
                "bold_italic": ["tahomabd.ttf"],
            },
            "trebuchet ms": {
                "regular": ["trebuc.ttf"],
                "bold": ["trebucbd.ttf"],
                "italic": ["trebucit.ttf"],
                "bold_italic": ["trebucbi.ttf"],
            },
            "impact": {
                "regular": ["impact.ttf"],
                "bold": ["impact.ttf"],
                "italic": ["impact.ttf"],
                "bold_italic": ["impact.ttf"],
            },
            "comic sans ms": {
                "regular": ["comic.ttf"],
                "bold": ["comicbd.ttf"],
                "italic": ["comici.ttf", "comic.ttf"],
                "bold_italic": ["comicz.ttf", "comicbd.ttf"],
            },
            "calibri": {
                "regular": ["calibri.ttf"],
                "bold": ["calibrib.ttf"],
                "italic": ["calibrii.ttf"],
                "bold_italic": ["calibriz.ttf"],
            },
            "cambria": {
                "regular": ["cambria.ttc", "cambria.ttf"],
                "bold": ["cambriab.ttf", "cambria.ttc"],
                "italic": ["cambriai.ttf", "cambria.ttc"],
                "bold_italic": ["cambriaz.ttf", "cambria.ttc"],
            },
            "segoe ui": {
                "regular": ["segoeui.ttf"],
                "bold": ["segoeuib.ttf"],
                "italic": ["segoeuii.ttf"],
                "bold_italic": ["segoeuiz.ttf"],
            },
            "helvetica": {
                "regular": ["arial.ttf"],
                "bold": ["arialbd.ttf"],
                "italic": ["ariali.ttf"],
                "bold_italic": ["arialbi.ttf"],
            },
            "century gothic": {
                "regular": ["gothic.ttf"],
                "bold": ["gothicb.ttf"],
                "italic": ["gothici.ttf"],
                "bold_italic": ["gothicz.ttf"],
            },
            "franklin gothic medium": {
                "regular": ["framd.ttf"],
                "bold": ["framd.ttf"],
                "italic": ["framdit.ttf"],
                "bold_italic": ["framdit.ttf"],
            },
        }

        if bold and italic:
            style_key = "bold_italic"
        elif italic:
            style_key = "italic"
        elif bold:
            style_key = "bold"
        else:
            style_key = "regular"

        family_entry = font_files.get(family_lower, {})
        candidates = []
        candidates.extend(family_entry.get(style_key, []))
        if style_key != "regular":
            candidates.extend(family_entry.get("regular", []))

        for filename in candidates:
            path = _candidate_path(filename)
            if path:
                return path

        # 3) Last-resort fuzzy search in Fonts folder.
        try:
            compact_family = _norm(family)
            fuzzy_aliases = {
                "arial narrow": ["arialn", "arialnb", "arialni", "arialnbi"],
                "franklin gothic medium": ["framd", "framdit"],
                "century gothic": ["gothic", "gothicb", "gothici", "gothicz"],
                "palatino linotype": ["pala", "palab", "palai", "palabi"],
                "trebuchet ms": ["trebuc", "trebucbd", "trebucit", "trebucbi"],
                "comic sans ms": ["comic", "comicbd", "comici", "comicz"],
                "segoe ui": ["segoeui", "segoeuib", "segoeuii", "segoeuiz"],
                "bauhaus 93": ["bauhs93"],
                "corbel": ["corbel", "corbelb", "corbeli", "corbelz"],
                "candara": ["candara", "candarab", "candarai", "candaraz"],
                "constantia": ["constan", "constanb", "constani", "constanz"],
                "consolas": ["consola", "consolab", "consolai", "consolaz"],
            }
            search_keys = [compact_family] + fuzzy_aliases.get(family_lower, [])

            for filename in os.listdir(fonts_dir):
                lower_name = filename.lower()
                if not lower_name.endswith((".ttf", ".ttc", ".otf")):
                    continue
                compact_name = _norm(lower_name)
                if any(key and key in compact_name for key in search_keys):
                    return os.path.join(fonts_dir, filename)
        except Exception:
            pass

        return None


    def _apply_vtk_font_family(self, text_prop, font_family, bold=False, italic=False):
        """
        Apply selected font to VTK text property using the exact installed font file.
        """
        family = (font_family or "Arial").strip()
        family_lower = family.lower()
        font_file = self._resolve_font_file(family, bold=bold, italic=italic)

        try:
            text_prop.SetBold(1 if bold else 0)
            text_prop.SetItalic(1 if italic else 0)
        except Exception:
            pass

        if font_file and hasattr(text_prop, "SetFontFile"):
            try:
                vtk_font_file_enum = getattr(vtk, "VTK_FONT_FILE", 4)

                try:
                    text_prop.SetFontFamily(vtk_font_file_enum)
                except Exception:
                    pass

                if hasattr(text_prop, "SetFontFamilyAsString"):
                    try:
                        text_prop.SetFontFamilyAsString("File")
                    except Exception:
                        pass

                text_prop.SetFontFile(font_file)

                try:
                    text_prop.SetFontFamily(vtk_font_file_enum)
                except Exception:
                    pass

                try:
                    text_prop.Modified()
                except Exception:
                    pass

                try:
                    enum_now = text_prop.GetFontFamily()
                except Exception:
                    enum_now = "unknown"

                print(
                    f"🔤 Text font applied: family='{family}', "
                    f"bold={bold}, italic={italic}, enum={enum_now}, file='{font_file}'"
                )
                return
            except Exception as e:
                print(f"⚠️ Font file apply failed: family='{family}', file='{font_file}', error={e}")

        if "courier" in family_lower or "mono" in family_lower or "consolas" in family_lower:
            text_prop.SetFontFamilyToCourier()
        elif "times" in family_lower or "serif" in family_lower:
            text_prop.SetFontFamilyToTimes()
        else:
            text_prop.SetFontFamilyToArial()

        try:
            text_prop.Modified()
        except Exception:
            pass

        print(
            f"⚠️ Text font fallback applied: family='{family}', "
            f"bold={bold}, italic={italic}, file_not_found_or_unusable"
        )


    def _make_scalable_text_actor(
        self,
        text,
        position,
        color=(1, 1, 1),
        font_size=75,
        bold=True,
        italic=False,
        font_family="Arial",
        justify="center",
    ):
        """
        Creates a vtkTextActor3D that lives in WORLD SPACE.
        Supports selected Windows fonts using SetFontFile().
        """
        font_size = max(0, min(999, int(75 if font_size is None else font_size)))
        font_family = (font_family or "Arial").strip()

        actor = vtk.vtkTextActor3D()
        actor.SetInput(text)
        actor.SetPosition(position[0], position[1], position[2])

        prop = actor.GetTextProperty()
        prop.SetColor(color[0], color[1], color[2])
        prop.SetFontSize(font_size)
        prop.SetOpacity(1.0)

        if bold:
            prop.BoldOn()
        else:
            prop.BoldOff()

        if italic:
            prop.ItalicOn()
        else:
            prop.ItalicOff()

        self._apply_vtk_font_family(prop, font_family, bold=bold, italic=italic)
        try:
            prop.Modified()
            actor.Modified()
        except Exception:
            pass

        if justify == "left":
            prop.SetJustificationToLeft()
        elif justify == "right":
            prop.SetJustificationToRight()
        else:
            prop.SetJustificationToCentered()

        prop.SetVerticalJustificationToCentered()

        camera = self.renderer.GetActiveCamera()
        if camera.GetParallelProjection():
            parallel_scale = camera.GetParallelScale()
        else:
            focal = np.array(camera.GetFocalPoint())
            cam_pos = np.array(camera.GetPosition())
            parallel_scale = np.linalg.norm(cam_pos - focal) * 0.1

        if parallel_scale < 0.001:
            parallel_scale = 100.0

        # Keep text comfortably legible in the main CAD viewport. The old
        # 0.05 coefficient made even 50 pt labels appear very small.
        base_scale = (parallel_scale / 100.0) * (font_size / 18.0) * 0.25
        actor.SetScale(base_scale, base_scale, base_scale)

        actor._text_scale_base = base_scale
        actor._text_font_size = font_size
        actor._text_font_family = font_family
        actor._text_parallel_scale_ref = parallel_scale

        actor.PickableOn()
        actor._digitize_overlay = True

        return actor

    # ---------------- FINALIZE EXISTING SHAPES ----------------
    def _finalize_gis_point(self, point):
        """Create one point drawing; the GIS bridge persists it to the active layer."""
        self._save_state()
        style = self._get_draw_style("smartline")
        points = vtk.vtkPoints()
        points.SetDataType(vtk.VTK_DOUBLE)
        points.InsertNextPoint(float(point[0]), float(point[1]), float(point[2]))
        vertices = vtk.vtkCellArray()
        vertices.InsertNextCell(1)
        vertices.InsertCellPoint(0)
        polydata = vtk.vtkPolyData()
        polydata.SetPoints(points)
        polydata.SetVerts(vertices)
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(polydata)
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(*style["color"])
        actor.GetProperty().SetPointSize(8.0)
        actor.GetProperty().SetRenderPointsAsSpheres(True)
        actor.PickableOn()
        self._add_actor_to_overlay(actor)
        drawing_entry = {
            "type": "gispoint",
            "coords": [tuple(point)],
            "actor": actor,
            "bounds": actor.GetBounds(),
            "original_color": style["color"],
            "original_width": 8.0,
        }
        self.drawings.append(drawing_entry)
        self._emit_drawing_finalized(drawing_entry)
        self.renderer.Modified()
        self.app.vtk_widget.render()

    def _finalize_rectangle(self):
        """Finalize rectangle drawing immediately."""
        self._save_state()
        if len(self.temp_points) != 2:
            print("⚠️ Rectangle needs exactly 2 points")
            return
        
        self._remove_preview_actor_2d('_rectangle_preview_actor')
        
        p1, p2 = self.temp_points
        x1, y1, z1 = p1
        x2, y2, z2 = p2
        
        coords = [
            (x1, y1, z1), 
            (x2, y1, z1), 
            (x2, y2, z2), 
            (x1, y2, z2), 
            (x1, y1, z1)
        ]
        
        rc_style = self._get_draw_style('rectangle')
        rc_color = rc_style['color']
        rc_width = rc_style['width']
        rc_lstyle = rc_style['style']

        actor = self._make_polyline_actor(coords, color=rc_color, width=rc_width, line_style=rc_lstyle)
        actor.PickableOn()
        self._add_actor_to_overlay(actor)

        drawing_entry = {
            "type": "rectangle",
            "coords": coords,
            "actor": actor,
            "bounds": actor.GetBounds(),
            "original_color": rc_color,
            "original_width": rc_width,
            "original_style": rc_lstyle,
        }

        if rc_style.get("fill_enabled", False):
            fill_color = rc_style.get("fill_color", rc_color)
            fill_opacity = rc_style.get("fill_opacity", 0.3)
            fill_actor = self._make_fill_actor(coords, fill_color, fill_opacity)
            if fill_actor is not None:
                self._add_actor_to_overlay(fill_actor)
                drawing_entry["fill_actor"] = fill_actor
                drawing_entry["original_fill_color"] = fill_color
                drawing_entry["original_fill_opacity"] = fill_opacity

        self._tag_sbm(drawing_entry)
        self.drawings.append(drawing_entry)
        self._emit_drawing_finalized(drawing_entry)

        self.temp_points = []
        if getattr(self, 'rectangle_permanent_mode', False):
            self.active_tool = "rectangle"
            print(f"✅ Rectangle finalized (Permanent - ready for next)")
        else:
            self.active_tool = None
            print(f"✅ Rectangle finalized")
        self.renderer.Modified()
        self.app.vtk_widget.render()

    def _finalize_circle(self, n=None):
        self._save_state()
        if len(self.temp_points) != 2: return
        
        self._remove_preview_actor_2d('_circle_preview_actor_2d')
        self._remove_preview_actor_2d('_circle_preview_actor')
        
        center, edge = self.temp_points
        radius = np.sqrt((edge[0]-center[0])**2 + (edge[1]-center[1])**2)
        
        if n is None:
            n = self._get_circle_segment_count(center, edge)
        
        thetas = np.linspace(0, 2 * np.pi, n, endpoint=False)
        coords = [(center[0] + radius * np.cos(t), center[1] + radius * np.sin(t), center[2]) for t in thetas]
        coords.append(coords[0]) 
        
        ci_style = self._get_draw_style('circle')
        ci_color = ci_style['color']
        ci_width = ci_style['width']
        ci_lstyle = ci_style['style']

        actor = self._make_polyline_actor(coords, color=ci_color, width=ci_width, line_style=ci_lstyle)
        self._add_actor_to_overlay(actor)

        drawing_entry = {
            'type': 'circle', 'coords': coords, 'actor': actor,
            'bounds': actor.GetBounds(), 'center': center, 'radius': radius,
            'original_color': ci_color, 'original_width': ci_width,
            'original_style': ci_lstyle,
        }

        if ci_style.get("fill_enabled", False):
            fill_color = ci_style.get("fill_color", ci_color)
            fill_opacity = ci_style.get("fill_opacity", 0.3)
            fill_actor = self._make_fill_actor(coords, fill_color, fill_opacity)
            if fill_actor is not None:
                self._add_actor_to_overlay(fill_actor)
                drawing_entry["fill_actor"] = fill_actor
                drawing_entry["original_fill_color"] = fill_color
                drawing_entry["original_fill_opacity"] = fill_opacity

        self._tag_sbm(drawing_entry)
        self.drawings.append(drawing_entry)
        self._emit_drawing_finalized(drawing_entry)
        
        self.temp_points = []
        if getattr(self, 'circle_permanent_mode', False):
            self.active_tool = "circle"
            print(f"✅ Circle finalized (Permanent - ready for next, Radius={radius:.3f})")
        else:
            self.active_tool = None
            print(f"✅ Circle finalized (Radius={radius:.3f})")
        self.renderer.Modified()
        self.app.vtk_widget.render()

    def _finalize_polygon(self):
        self._save_state()
        coords = self.temp_points + [self.temp_points[0]]
        actor = self._make_polyline_actor(coords)
        self._add_actor_to_overlay(actor)
        drawing_entry = {
            "type": "polygon", 
            "coords": coords, 
            "actor": actor,
            "bounds": actor.GetBounds(),
            "original_color": (1, 0, 0),
            "original_width": 2,
        }
        self.drawings.append(drawing_entry)
        self._emit_drawing_finalized(drawing_entry)
        self.temp_points = []
        self.app.vtk_widget.render()
    
    
    
    def _finalize_freehand(self):
        """
        ✅ FIX: Commit freehand drawing using overlay renderer (consistent with all other tools).
        Previously used self.renderer.AddActor2D() which bypassed overlay_renderer.
        """
        self._hide_snap_marker_now()
        self._save_state()

        self._remove_preview_actor_2d('_preview_actor')
        self._remove_preview_actor_2d('_freehand_preview_actor_2d')

        if len(self.temp_points) < 2:
            self.temp_points = []
            return

        # 2. FORCE AUTO-CLOSE
        start_pt = self.temp_points[0]
        end_pt = self.temp_points[-1]
        
        dist = np.linalg.norm(np.array(start_pt) - np.array(end_pt))
        
        if dist > 0.001:
            self.temp_points.append(start_pt)
            print("🔗 Freehand loop Auto-Closed (Forced)")
        else:
            print("🔗 Freehand loop already closed")

        # 3. Create Final Actor — use 3D polyline actor like all other tools
        fh_style = self._get_draw_style('freehand')
        fh_color = fh_style['color']
        fh_width = fh_style['width']
        fh_lstyle = fh_style['style']

        final_actor = self._make_polyline_actor(self.temp_points, color=fh_color, width=fh_width, line_style=fh_lstyle)
        
        # 4. Add via overlay renderer (THE FIX — was AddActor2D before)
        self._add_actor_to_overlay(final_actor)
        
        # 5. Store Drawing
        drawing_entry = {
            "type": "freehand",
            "coords": list(self.temp_points),
            "actor": final_actor,
            "bounds": final_actor.GetBounds(),
            "original_color": fh_color,
            "original_width": fh_width,
            "original_style": fh_lstyle,
        }

        if fh_style.get("fill_enabled", False):
            fill_color = fh_style.get("fill_color", fh_color)
            fill_opacity = fh_style.get("fill_opacity", 0.3)
            fill_actor = self._make_fill_actor(list(self.temp_points), fill_color, fill_opacity)
            if fill_actor is not None:
                self._add_actor_to_overlay(fill_actor)
                drawing_entry["fill_actor"] = fill_actor
                drawing_entry["original_fill_color"] = fill_color
                drawing_entry["original_fill_opacity"] = fill_opacity

        self.drawings.append(drawing_entry)
        self._emit_drawing_finalized(drawing_entry)
        
        self.temp_points = []
        self.is_drawing_freehand = False
        if getattr(self, 'freehand_permanent_mode', False):
            self.active_tool = "freehand"
            print(f"✅ Freehand finalized (Permanent - ready for next)")
        else:
            self.active_tool = None
        self.renderer.Modified()
        self.app.vtk_widget.render()

    # Drawing types that form a closed area and can be hatched
    _HATCHABLE_TYPES = frozenset({
        'rectangle', 'circle', 'polyline', 'polygon',
        'freehand', 'smartline', 'orthopolygon',
    })

    def _on_hatch_method_clicked(self, method_id):
        """Called when user clicks a method button in the floating hatch dialog.
        Re-activates the hatch tool if another tool is currently active."""
        try:
            if getattr(self, 'active_tool', None) != 'hatcharea':
                self.set_tool('hatcharea')
            self._hatch_method_id = method_id
            if hasattr(self, '_hatch_dialog') and self._hatch_dialog:
                try:
                    _msgs = {
                        0: "Click inside an existing shape to hatch it.",
                        1: "Select by Fence: draw a fence, then right-click to apply.",
                        2: "Click boundary points, right-click when ≥3 to apply hatch.",
                        3: "Drag a rectangle to define the hatch boundary.",
                        4: "Freehand: drag to paint the hatch boundary.",
                        5: "Union mode: click multiple shapes to merge and hatch.",
                        6: "Click a block (SNT/DXF/PRJ grid) to hatch it.",
                    }
                    self._hatch_dialog.set_status(_msgs.get(method_id,
                        "Click inside a shape to hatch it,\nor click empty space to define boundary points."))
                except Exception:
                    pass
        except Exception as _e:
            print(f"⚠️ _on_hatch_method_clicked: {_e}")

    def _hatch_find_element_at(self, pos):
        """Return the topmost hatchable drawing whose closed boundary contains pos."""
        try:
            from gui.hatch_area_tool import _point_in_polygon_2d
            for d in reversed(self.drawings):
                dtype = d.get('type', '')
                if dtype not in self._HATCHABLE_TYPES:
                    continue
                coords = d.get('coords', [])
                if len(coords) < 3:
                    continue
                if _point_in_polygon_2d(pos, coords):
                    return d
        except Exception as e:
            print(f"⚠️ _hatch_find_element_at: {e}")
        return None

    def _hatch_find_block_by_name(self, grid_name, _preloaded_mt_blocks=None):
        """Find a block polygon by its grid/label name. Returns 3D-point dict or None.

        `_preloaded_mt_blocks`: pass an already-fetched `measurement_tool
        ._load_block_boundaries()` list to avoid recomputing it -- that call
        re-parses SNT/digitizer geometry on every invocation and was being
        triggered twice per click from `_hatch_find_block_at`, which was the
        main source of the Hatch Block-select tool's noticeable delay.
        """
        if not grid_name:
            return None
        name_lower = grid_name.lower().strip()

        # 1) Search snt_block_polygons
        for entry in (getattr(self.app, 'snt_block_polygons', None) or []):
            entry_name = (
                entry.get("grid_name")
                or entry.get("block_name")
                or entry.get("label")
                or ""
            )
            if not entry_name:
                continue
            if entry_name.lower().strip() == name_lower or name_lower in entry_name.lower():
                coords = (
                    entry.get("points_2d")
                    or entry.get("points")
                    or entry.get("vertices")
                    or entry.get("polygon")
                    or []
                )
                if len(coords) < 3:
                    continue
                return self._hatch_build_block_result(entry_name, coords, "snt")

        # 2) Search measurement_tool block boundaries
        try:
            if _preloaded_mt_blocks is not None:
                mt_blocks = _preloaded_mt_blocks
            else:
                mt = getattr(self.app, 'measurement_tool', None)
                mt_blocks = mt._load_block_boundaries() if (mt is not None and hasattr(mt, '_load_block_boundaries')) else []
            for blk in mt_blocks:
                blk_label = blk.get("label", "")
                if not blk_label:
                    continue
                if blk_label.lower().strip() == name_lower or name_lower in blk_label.lower():
                    return self._hatch_build_block_result(
                        blk_label, blk.get("points", []), blk.get("source_type", "prj")
                    )
        except Exception:
            pass

        # 3) Search PRJ block identifier dialog
        try:
            prj_dlg = getattr(self.app, 'block_identifier_dialog', None)
            if prj_dlg is not None:
                for entry in getattr(prj_dlg, 'prj_data', []) or []:
                    entry_label = entry.get('label', '') or entry.get('block_file', '')
                    if not entry_label:
                        continue
                    if entry_label.lower().strip() == name_lower or name_lower in entry_label.lower():
                        coords = entry.get('boundary_coords', [])
                        if len(coords) >= 3:
                            return self._hatch_build_block_result(entry_label, coords, "prj_grid")
        except Exception:
            pass

        return None

    def _hatch_build_block_result(self, label, coords_2d, source):
        """Build a hatch block dict from label + 2D coords. Returns dict with 3D points."""
        if len(coords_2d) < 3:
            return None
        try:
            pts2d = [(float(p[0]), float(p[1])) for p in coords_2d]
        except (TypeError, IndexError, ValueError):
            return None

        n = len(pts2d)
        area = 0.0
        cx_val, cy_val = 0.0, 0.0
        for i in range(n):
            x0, y0 = pts2d[i]
            x1, y1 = pts2d[(i + 1) % n]
            cross = x0 * y1 - x1 * y0
            area += cross
            cx_val += (x0 + x1) * cross
            cy_val += (y0 + y1) * cross
        area = abs(area) / 2.0
        if area > 1e-12:
            cx_val /= (6.0 * area)
            cy_val /= (6.0 * area)

        # Z from the grid's real elevation (matches grid labels), not the
        # camera focal point -- the focal point can drift far from grid level
        # once the point cloud is cleared, dropping the hatch below the grid.
        z_val = self._hatch_flat_z()

        return {
            "label": label,
            "points": [(float(p[0]), float(p[1]), z_val) for p in pts2d],
            "area": area,
            "centroid": (cx_val, cy_val),
            "source": source,
        }

    def _hatch_find_block_at(self, world_pos):
        """Find an SNT/DXF/PRJ block boundary at the clicked world position.
        Returns dict with 'points', 'label', 'area', 'centroid' keys or None."""
        # Fetch measurement_tool block boundaries ONCE and reuse in both Step 0
        # (name lookup) and Step 1 (coordinate fallback) below -- this call
        # re-parses SNT/digitizer geometry and was previously invoked twice
        # per click, which was the main source of the Block-select delay.
        mt = getattr(self.app, 'measurement_tool', None)
        mt_blocks = []
        try:
            if mt is not None and hasattr(mt, '_load_block_boundaries'):
                mt_blocks = mt._load_block_boundaries()
        except Exception:
            mt_blocks = []
        # ── Step 0: VTK pick grid label actor → lookup block by name ──
        # This is the most reliable method for SNT grid cells.
        try:
            x_screen, y_screen = self.interactor.GetEventPosition()
            vtk_y = y_screen

            # Area pick around cursor to catch grid label actors
            area_picker = vtk.vtkAreaPicker()
            area_picker.AreaPick(x_screen - 15, vtk_y - 15, x_screen + 15, vtk_y + 15, self.renderer)
            grid_name = None
            for prop in area_picker.GetProp3Ds():
                if getattr(prop, "is_grid_label", False):
                    grid_name = getattr(prop, "grid_name", "")
                    if grid_name:
                        break

            # Fallback: single prop pick
            if not grid_name:
                prop_picker = vtk.vtkPropPicker()
                prop_picker.Pick(x_screen, vtk_y, 0, self.renderer)
                picked = prop_picker.GetActor()
                if picked is not None and getattr(picked, "is_grid_label", False):
                    grid_name = getattr(picked, "grid_name", "")

            # Also check overlay renderer (SNT labels may live here)
            if not grid_name and getattr(self, '_overlay_renderer', None) is not None:
                prop_picker2 = vtk.vtkPropPicker()
                prop_picker2.Pick(x_screen, vtk_y, 0, self._overlay_renderer)
                picked2 = prop_picker2.GetActor()
                if picked2 is not None and getattr(picked2, "is_grid_label", False):
                    grid_name = getattr(picked2, "grid_name", "")

            if grid_name:
                result = self._hatch_find_block_by_name(grid_name, mt_blocks)
                if result is not None:
                    return result
                print(f"⚠️ Hatch grid pick: found label '{grid_name}' but no matching block polygon")
            else:
                # Debug: check what was picked
                debug_picks = []
                for prop in area_picker.GetProp3Ds():
                    debug_picks.append(getattr(prop, '__class__', type(prop)).__name__)
                if debug_picks:
                    print(f"⚠️ Hatch grid pick: picked {len(debug_picks)} props but none are grid labels: {debug_picks[:5]}")
        except Exception as e:
            print(f"⚠️ _hatch_find_block_at grid pick: {e}")

        # ── Step 1: Coordinate-based polygon search (fallback) ──
        if world_pos is None:
            return None
        try:
            x, y = float(world_pos[0]), float(world_pos[1])
        except (TypeError, IndexError):
            return None

        blocks = []

        # 1) SNT block polygons (app.snt_block_polygons)
        for entry in (getattr(self.app, 'snt_block_polygons', None) or []):
            coords = (
                entry.get("points_2d")
                or entry.get("points")
                or entry.get("vertices")
                or entry.get("polygon")
                or []
            )
            if len(coords) < 3:
                continue
            label = entry.get("grid_name") or entry.get("block_name") or entry.get("label") or ""
            blocks.append({"label": label, "points": coords, "source": entry.get("source", "snt")})

        # 2) PRJ block boundaries via measurement_tool (reuse the list fetched above)
        try:
            for blk in mt_blocks:
                src = blk.get("source_type", "")
                if src in ("prj", "snt", "snt_grid", "prj_grid"):
                    blk_copy = dict(blk)
                    blk_copy["source"] = src
                    blocks.append(blk_copy)
        except Exception:
            pass

        # 2b) PRJ block identifier dialog grid blocks (boundary_coords)
        try:
            prj_dlg = getattr(self.app, 'block_identifier_dialog', None)
            if prj_dlg is not None:
                for entry in getattr(prj_dlg, 'prj_data', []) or []:
                    coords = entry.get('boundary_coords', [])
                    if len(coords) < 3:
                        continue
                    label = entry.get('label', '') or entry.get('block_file', '') or 'PRJ grid block'
                    blocks.append({"label": label, "points": coords, "source": "prj_grid"})
        except Exception:
            pass

        # 3) DXF closed entities (polylines, polygons from dxf_actors)
        for dxf_data in (getattr(self.app, 'dxf_actors', None) or []):
            for ent in dxf_data.get('entities', []):
                etype = ent.get('type', '')
                if etype not in ('lwpolyline', 'polyline', 'polygon', 'circle', 'ellipse'):
                    continue
                coords = ent.get('points') or ent.get('coords') or []
                if etype == 'circle':
                    import math
                    cx, cy = ent.get('center', (0, 0))
                    r = ent.get('radius', 0)
                    if r > 0:
                        n_pts = 36
                        coords = [
                            (cx + r * math.cos(2 * math.pi * i / n_pts),
                             cy + r * math.sin(2 * math.pi * i / n_pts))
                            for i in range(n_pts)
                        ]
                if len(coords) < 3:
                    continue
                label = ent.get('layer', '') or ent.get('name', '') or "DXF block"
                blocks.append({"label": label, "points": coords, "source": "dxf"})

        # 4) Digitizer drawings (closed polygons)
        for idx, d in enumerate(getattr(self, 'drawings', []) or []):
            dtype = d.get('type', '')
            if dtype not in ('polygon', 'rectangle', 'polyline', 'freehand', 'orthopolygon'):
                continue
            coords = d.get('coords', [])
            if len(coords) < 3:
                continue
            layer = d.get('layer', '')
            label = f"{dtype.capitalize()} #{idx + 1}" + (f" [{layer}]" if layer else "")
            blocks.append({"label": label, "points": coords, "source": "digitizer"})

        # Find block containing the click point
        from gui.hatch_area_tool import _point_in_polygon_2d
        containing_blocks = []
        fallback_candidates = []

        # Get screen click coordinates and camera parallel scale
        click_x, click_y = 0.0, 0.0
        has_display = False
        if hasattr(self, 'interactor') and self.interactor:
            click_x, click_y = self.interactor.GetEventPosition()
            has_display = True

        parallel_scale = 1e9
        try:
            camera = self.renderer.GetActiveCamera()
            if camera is not None and hasattr(camera, 'GetParallelScale'):
                parallel_scale = camera.GetParallelScale()
        except Exception:
            pass

        def _get_display_dist(cx, cy):
            if not has_display:
                return float('inf')
            try:
                self.renderer.SetWorldPoint(float(cx), float(cy), 0.0, 1.0)
                self.renderer.WorldToDisplay()
                dp = self.renderer.GetDisplayPoint()
                return ((click_x - dp[0]) ** 2 + (click_y - dp[1]) ** 2) ** 0.5
            except Exception:
                return float('inf')

        for blk in blocks:
            pts = blk['points']
            try:
                pts2d = [(float(p[0]), float(p[1])) for p in pts]
            except (TypeError, IndexError, ValueError):
                continue
            if len(pts2d) < 3:
                continue
            try:
                xmin = min(p[0] for p in pts2d)
                xmax = max(p[0] for p in pts2d)
                ymin = min(p[1] for p in pts2d)
                ymax = max(p[1] for p in pts2d)

                # Compute centroid for distance fallback/sorting
                n = len(pts2d)
                cx_tmp = sum(p[0] for p in pts2d) / n
                cy_tmp = sum(p[1] for p in pts2d) / n
                dist_sq = (x - cx_tmp) ** 2 + (y - cy_tmp) ** 2

                # Check containment
                if x >= xmin and x <= xmax and y >= ymin and y <= ymax:
                    if _point_in_polygon_2d((x, y), pts2d):
                        containing_blocks.append((blk, dist_sq, pts2d))

                # Collect fallback candidates in screen space
                if (dist_sq ** 0.5) < 1.5 * parallel_scale:
                    dist_disp = _get_display_dist(cx_tmp, cy_tmp)
                    if dist_disp < 40.0:
                        fallback_candidates.append((blk, dist_disp, pts2d))
            except Exception:
                continue

        if containing_blocks:
            # Prioritize non-grid blocks over grid blocks, then sort by distance to centroid (resolves overlaps)
            containing_blocks.sort(key=lambda item: (1 if item[0].get("source") in ("snt_grid", "prj_grid") else 0, item[1]))
            best_blk, _, best_pts2d = containing_blocks[0]
            # world_pos.z is deliberately forced to 0 by the caller's Z=0-plane
            # ray intersection (used only for XY containment above) -- use the
            # grid's real elevation here so the hatch lands at grid level.
            z_val = self._hatch_flat_z()
            n = len(best_pts2d)
            area = 0.0
            cx_val, cy_val = 0.0, 0.0
            for i in range(n):
                x0, y0 = best_pts2d[i]
                x1, y1 = best_pts2d[(i + 1) % n]
                cross = x0 * y1 - x1 * y0
                area += cross
                cx_val += (x0 + x1) * cross
                cy_val += (y0 + y1) * cross
            area = abs(area) / 2.0
            if area > 1e-12:
                cx_val /= (6.0 * area)
                cy_val /= (6.0 * area)
            # CRITICAL: return a COPY — never mutate the cached source dict
            result = dict(best_blk)
            result['area'] = area
            result['centroid'] = (cx_val, cy_val)
            result['points'] = [(float(p[0]), float(p[1]), z_val) for p in best_pts2d]
            return result

        # Fallback: nearest centroid in display space within 40 pixels (handles coord precision at zoom out)
        if fallback_candidates:
            # Prioritize non-grid blocks over grid blocks, then sort by display distance (in pixels)
            fallback_candidates.sort(key=lambda item: (1 if item[0].get("source") in ("snt_grid", "prj_grid") else 0, item[1]))
            best_blk, dist_disp, best_pts2d = fallback_candidates[0]
            # world_pos.z is deliberately forced to 0 by the caller's Z=0-plane
            # ray intersection (used only for XY containment above) -- use the
            # grid's real elevation here so the hatch lands at grid level.
            z_val = self._hatch_flat_z()
            n = len(best_pts2d)
            area = 0.0
            cx_val, cy_val = 0.0, 0.0
            for i in range(n):
                x0, y0 = best_pts2d[i]
                x1, y1 = best_pts2d[(i + 1) % n]
                cross = x0 * y1 - x1 * y0
                area += cross
                cx_val += (x0 + x1) * cross
                cy_val += (y0 + y1) * cross
            area = abs(area) / 2.0
            if area > 1e-12:
                cx_val /= (6.0 * area)
                cy_val /= (6.0 * area)
            # CRITICAL: return a COPY — never mutate the cached source dict
            result = dict(best_blk)
            result['area'] = area
            result['centroid'] = (cx_val, cy_val)
            result['points'] = [(float(p[0]), float(p[1]), z_val) for p in best_pts2d]
            label_name = result.get('label', '?')
            print(f"▦ Hatch fallback (display space): '{label_name}' (dist={dist_disp:.1f}px)")
            return result

        return None

    @staticmethod
    def _hatch_coords_match(coords1, coords2, tol=1e-9):
        """Return True if two boundary coord lists describe the same polygon (same pts in order)."""
        if len(coords1) != len(coords2):
            return False
        try:
            for a, b in zip(coords1, coords2):
                if abs(float(a[0]) - float(b[0])) > tol or abs(float(a[1]) - float(b[1])) > tol:
                    return False
            return True
        except Exception:
            return False

    def _finalize_hatcharea(self):
        """Finalize hatch area: draw boundary + fill with parallel hatch lines."""
        from gui.hatch_area_tool import generate_hatch_lines

        self._save_state()

        # Remove preview actors
        self._remove_preview_actor_2d('_preview_line_actor')
        self._remove_preview_actor_2d('_continuous_line_actor')

        # Remove vertex markers
        for m in getattr(self, '_hatch_vertex_markers', []):
            try: self._remove_actor_from_overlay(m)
            except Exception: pass
        self._hatch_vertex_markers = []

        boundary = list(self.temp_points)
        if len(boundary) < 3:
            self.temp_points = []
            return

        # Close the boundary
        if np.linalg.norm(np.array(boundary[0]) - np.array(boundary[-1])) > 1e-9:
            boundary.append(boundary[0])

        # ── Toggle off/Deactivate mode: if there are existing hatches on this exact boundary or matching block label,
        # remove them and return immediately. This acts as a toggle to deactivate hatching.
        try:
            block_label = getattr(self, 'temp_hatch_block_label', None)
            _old_hatches = [
                d for d in self.drawings
                if d.get('type') == 'hatcharea'
                and (
                    self._hatch_coords_match(d.get('coords', []), boundary)
                    or (block_label and d.get('block_label') == block_label)
                )
            ]
            if _old_hatches:
                for _old in _old_hatches:
                    for _ha in _old.get('hatch_actors', []):
                        try: self._remove_actor_from_overlay(_ha)
                        except Exception: pass
                    if _old.get('actor'):
                        try: self._remove_actor_from_overlay(_old['actor'])
                        except Exception: pass
                    try: self.drawings.remove(_old)
                    except ValueError: pass
                print(f"▦ Deactivated/Removed {len(_old_hatches)} existing hatch(es) on this boundary/block")
                self.temp_points = []
                self.temp_hatch_block_label = None
                try:
                    self.app.vtk_widget.GetRenderWindow().Render()
                except Exception:
                    self.app.vtk_widget.render()
                if hasattr(self, '_hatch_dialog') and self._hatch_dialog:
                    try:
                        self._hatch_dialog.set_status("Hatch removed from block.")
                    except Exception:
                        pass
                return
        except Exception as _re:
            print(f"⚠️ Hatch deactivation failed: {_re}")

        ha_style = self._get_draw_style('hatcharea')
        ha_color = ha_style['color']
        ha_width = ha_style['width']
        ha_lstyle = ha_style['style']

        # Draw boundary outline
        boundary_actor = self._make_polyline_actor(boundary, color=ha_color, width=ha_width, line_style=ha_lstyle)
        if boundary_actor:
            boundary_actor.PickableOn()
            self._add_hatch_actor(boundary_actor)

        # Get hatch parameters from dialog (fallback to defaults)
        spacing = 800.0
        angle = 0.0
        if hasattr(self, '_hatch_dialog') and self._hatch_dialog:
            try:
                spacing = self._hatch_dialog.spacing
                angle = self._hatch_dialog.angle
            except Exception:
                pass

        # Auto-adjust spacing when it's too large for the polygon extent.
        # This happens when the user has a spacing saved from a different-scale project.
        try:
            _pts2d = np.array([(float(p[0]), float(p[1])) for p in boundary[:-1]])
            _ar = np.radians(float(angle))
            _nv = np.array([-np.sin(_ar), np.cos(_ar)])
            _projs = _pts2d @ _nv
            _extent = float(_projs.max() - _projs.min())
            if _extent > 1e-9 and spacing >= _extent:
                spacing = _extent / 10.0
                print(f"⚠️ Hatch auto-spacing: polygon extent={_extent:.6f}, adjusted spacing to {spacing:.6f}")
                if hasattr(self, '_hatch_dialog') and self._hatch_dialog:
                    try:
                        self._hatch_dialog.spacing_spin.setValue(spacing)
                    except Exception:
                        pass
        except Exception as _ae:
            print(f"⚠️ Hatch auto-spacing calc failed: {_ae}")

        # Generate hatch line segments
        hatch_segments = generate_hatch_lines(boundary, spacing, angle)

        # Build actor(s) for all hatch line segments (merged into one actor
        # for solid style -- see _make_hatch_segments_actors)
        hatch_actors = []
        for seg_actor in self._make_hatch_segments_actors(hatch_segments, ha_color, ha_width, ha_lstyle):
            self._add_hatch_actor(seg_actor)
            hatch_actors.append(seg_actor)

        drawing_entry = {
            "type": "hatcharea",
            "coords": boundary,
            "actor": boundary_actor,
            "hatch_actors": hatch_actors,
            "hatch_spacing": spacing,
            "hatch_angle": angle,
            "block_label": block_label,
            "bounds": boundary_actor.GetBounds() if boundary_actor else (0, 0, 0, 0, 0, 0),
            "original_color": ha_color,
            "original_width": ha_width,
            "original_style": ha_lstyle,
        }

        # Hatch is generated Draw geometry. Keep it under DIGITIZER so Draw > Clear
        # can remove it even when an SNT/DXF/SBM layer is active.
        drawing_entry["layer"] = "DIGITIZER"
        drawing_entry.pop("snt_file", None)
        drawing_entry.pop("dxf_file", None)
        drawing_entry.pop("sbm_class", None)

        self.drawings.append(drawing_entry)
        self._emit_drawing_finalized(drawing_entry)
        self.temp_hatch_block_label = None

        self.temp_points = []
        # ── Permanent mode: keep tool active so undo→re-hatch works immediately ──
        # active_tool stays "hatcharea"; cursor stays active.
        # User can switch away via toolbar; then the cancel-on-switch block handles cleanup.

        n_lines = len(hatch_segments)
        if hasattr(self, '_hatch_dialog') and self._hatch_dialog:
            try:
                if n_lines == 0:
                    self._hatch_dialog.set_status(
                        f"0 lines — spacing {spacing:.4f} may be too large.\n"
                        f"Adjust spacing/angle and click inside a shape."
                    )
                else:
                    self._hatch_dialog.set_status(
                        f"✓ {n_lines} lines applied (spacing={spacing:.4f}).\n"
                        f"Click inside a shape to hatch again, or\n"
                        f"click empty space to draw a custom boundary."
                    )
            except Exception:
                pass

        print(f"✅ Hatch Area finalized: boundary={len(boundary)} pts, lines={n_lines}, spacing={spacing:.6f}, angle={angle}°")
        self.renderer.Modified()
        self.app.vtk_widget.render()

    def _finalize_text(self, text):
        """
        ✅ Text uses renderer directly (not overlay) because vtkTextActor is 2D.
        This is intentional and separate from the overlay system.
        """
        self._save_state()
        if not self.temp_points:
            return
            
        pos = self.temp_points[-1]
        
        actor = vtk.vtkTextActor()
        actor.SetInput(text)
        
        prop = actor.GetTextProperty()
        prop.SetColor(1, 1, 0) # Yellow
        prop.BoldOn()
        prop.SetFontSize(18)
        prop.SetJustificationToCentered()
        prop.SetVerticalJustificationToCentered()
        
        coord = actor.GetActualPositionCoordinate()
        coord.SetCoordinateSystemToWorld()
        coord.SetValue(pos[0], pos[1], pos[2])
        
        actor.GetProperty().SetDisplayLocationToForeground()

        if not bool(getattr(self, "draw_text_visible", True)):
            actor.SetVisibility(0)

        # TEXT actors must use renderer.AddActor2D (they are inherently 2D)
        self.renderer.AddActor2D(actor)
        
        self.drawings.append({
            "type": "text", 
            "coords": [tuple(pos)], 
            "actor": actor, 
            "text": text,
            "original_text_color": (1, 1, 0),
            "_draw_text_visible": bool(getattr(self, "draw_text_visible", True)),
        })
        
        self.temp_points = []
        self.renderer.Modified()
        self.app.vtk_widget.render()
        print(f"✅ Zoom-immune 2D Text placed at {pos}")

    def clear_drawings(self, clear_classified=False, record_undo=False, preserve_committed=False, clear_text=True):
        """
        ✅ FIX: Clear all drawings consistently.
        All non-text actors live in overlay_renderer.
        Text actors live in self.renderer (they are 2D by nature).

        preserve_committed=True keeps drawings already saved to an SNT layer
        (snt_committed) on screen — the user-facing Clear button uses this so a
        Clear never wipes drawings that were already saved.

        clear_text=False preserves all text drawings (both visible and hidden)
        while clearing everything else.
        """
        print("🧹 Clearing all drawings and previews (safe mode)...")

        # AccuDraw cleanup before clearing normal drawings.
        try:
            acc = getattr(self, "accudraw_tool", None)
            if acc is not None:
                acc.reset_after_external_clear()
        except Exception as e:
            print(f"⚠️ AccuDraw clear reset warning: {e}")

        # If Clear is pressed while Move Vertex is active, cancel it first.
        # Otherwise a stale circle/polyline actor can remain visible after clear.
        try:
            self._cancel_active_vertex_move_runtime()
        except Exception:
            pass
        
        # Element Selection > Intersect replaces live drawing actors with its
        # own overlay actors while active. Clear must discard those overlays
        # first, otherwise the drawing list is empty but split previews remain
        # visible until Intersect is manually deactivated.
        try:
            element_tool = getattr(self, "_element_select_tool", None)
            if element_tool is not None:
                if getattr(element_tool, "_intersect_mode", False):
                    element_tool._cancel_intersect_mode()
                elif hasattr(element_tool, "_clear_intersect_overlays"):
                    element_tool._clear_intersect_overlays()
        except Exception as e:
            print(f"⚠️ Intersect clear cleanup warning: {e}")

        def _is_protected(d):
            if not clear_classified and d.get('classified_fence', False):
                return True

            # Hatch is generated Draw geometry.
            # Even when an SNT/DXF/SBM layer is active, unsaved hatch must be
            # removable by Draw > Clear.
            if d.get("type") == "hatcharea" and not d.get("snt_committed", False):
                return False

            if preserve_committed and d.get('snt_committed'):
                return True
            # Protect drawings that were created while an SNT Level Manager layer
            # was active (tagged with snt_file + a real layer name, not DIGITIZER).
            # These live only in the digitizer until the user clicks Save in the
            # Level Manager, so the Draw-ribbon Clear must not remove them.
            # Deleting these entities is handled exclusively via the Level Manager.
            if preserve_committed and d.get('snt_file') and \
                    d.get('layer') and str(d.get('layer')).upper() != 'DIGITIZER':
                return True
            return False


        if record_undo:
            has_clearable_drawings = any(
                not _is_protected(d) for d in self.drawings
            )
            if has_clearable_drawings:
                self._save_state()

        self._clear_suspended_preview_actor()
        self._clear_live_preview_actors()
        self._suspended_state = None

        # Clean up any active Ortho-Polygon tool state
        ortho_tool = getattr(self, '_ortho_polygon_tool', None)
        if ortho_tool is not None:
            try:
                ortho_tool._cancel()
            except Exception as e:
                print(f"⚠️ Failed to cancel ortho tool: {e}")

        # Clean up any active Text tool state
        try:
            self._cleanup_text_tool()
        except Exception as e:
            print(f"⚠️ Failed to clean up text tool: {e}")

        # ✅ Clear the temp fence (if any) alongside Draw > Clear. Without
        # this, a yellow temp fence outline remains on the canvas after the
        # user explicitly clears drawings, which is confusing because every
        # other drawn element was removed.
        try:
            temp_fence_tool = getattr(self.app, "temp_fence_tool", None)
            if temp_fence_tool is not None and hasattr(temp_fence_tool, "remove_fence"):
                temp_fence_tool.remove_fence()
        except Exception as e:
            print(f"⚠️ Failed to clear temp fence during Draw > Clear: {e}")


        # currently selected drawing do not survive the global clear.
        try:
            self._clear_selection()
        except Exception:
            pass
        self.selected = None
        self.selected_drawing = None
        self.multi_selected = []

        def _safe_remove(actor, is_text=False, is_scalable=False):
            """Route removal to the correct renderer."""
            if actor is None:
                return
            if is_text:
                if is_scalable:
                    # Scalable text (vtkTextActor3D) lives in text layer 3.
                    try:
                        if hasattr(self, 'text_overlay_renderer') and self.text_overlay_renderer:
                            self.text_overlay_renderer.RemoveActor(actor)
                    except Exception:
                        pass
                    # Safety net: also try overlay_renderer for actors placed before the fix
                    # Scalable text lives in overlay
                    try:
                        if hasattr(self, 'overlay_renderer') and self.overlay_renderer:
                            self.overlay_renderer.RemoveActor(actor)
                    except Exception:
                        pass
                else:
                    # Legacy billboard text
                    try: self.renderer.RemoveViewProp(actor)
                    except Exception:
                        pass
                    try: self.renderer.RemoveActor2D(actor)
                    except Exception:
                        pass
                # Safety net
                try: self._remove_actor_from_overlay(actor)
                except Exception:
                    pass
            else:
                # Endpoint/vertex sphere actors live in text layer 3 so they
                # always appear over the shaded mesh — remove
                # from all three renderers defensively.
                try:
                    if hasattr(self, 'text_overlay_renderer') and self.text_overlay_renderer:
                        self.text_overlay_renderer.RemoveActor(actor)
                except Exception:
                    pass
                try:
                    if hasattr(self, 'overlay_renderer') and self.overlay_renderer:
                        self.overlay_renderer.RemoveActor(actor)
                except Exception:
                    pass
                try: self.renderer.RemoveActor(actor)
                except Exception:
                    pass
        for d in list(self.drawings):
            try:
                # Hatch generated by Draw > Hatch must clear even if the current
                # active SNT/DXF layer tagged it accidentally.
                if d.get("type") == "hatcharea" and not d.get("snt_committed", False):
                    try:
                        self._remove_drawing(d)
                    except Exception as e:
                        print(f"⚠️ Failed to remove hatcharea during clear: {e}")
                    continue

                # ✅ Skip cyan classified fences unless Shift+Clear, and skip
                # already-saved drawings when preserve_committed is set.
                if _is_protected(d):
                    continue                # Skip text drawings when clear_text=False (user chose to
                # preserve hidden texts on the warning prompt).
                if not clear_text and d.get('type') == 'text':
                    continue
                is_text = d.get('type') == 'text'
                is_scalable = d.get('scalable', False)

                
                if d.get('actor') is not None:
                    _safe_remove(d['actor'], is_text=is_text, is_scalable=is_scalable)
                    try: d['actor'].VisibilityOff()
                    except Exception:
                        pass
                # Endpoint markers (always in overlay)
                _safe_remove(d.get('start_marker'), is_text=False)
                _safe_remove(d.get('end_marker'), is_text=False)

                # Vertex markers (always in overlay)
                for marker in d.get('vertex_markers', []):
                    _safe_remove(marker, is_text=False)

                # Arrow actors (2D, in self.renderer)
                arrows = d.get('arrow_actor')
                if arrows is not None:
                    if not isinstance(arrows, list):
                        arrows = [arrows]
                    for arr in arrows:
                        try: self.renderer.RemoveActor2D(arr)
                        except Exception:
                            pass

                # Fill polygon actor
                if d.get('fill_actor') is not None:
                    _safe_remove(d['fill_actor'], is_text=False)

                # Only emit drawing-removed for drawings that were COMMITTED
                # to an SNT (via the Save button) — non-committed drawings
                # are not in any SNT so listeners have nothing to update.
                # This prevents the SNT layer dialog's
                # `_auto_remove_drawing_from_snt` from being called for every
                # non-committed drawing on a canvas Clear, which (a) wastes
                # work and (b) could accidentally delete the wrong saved
                # entity on a coordinate near-collision.
                if d.get('snt_committed'):
                    self._emit_drawing_removed(d)
            except Exception as e:
                print(f"⚠️ Failed to remove drawing: {e}")


        # Keep exactly the drawings that were protected from this clear
        # (classified fences and/or already-saved committed drawings).
        # When clear_text=False, also preserve all text drawings.
        if clear_classified and not preserve_committed and clear_text:
            self.drawings.clear()
        else:
            self.drawings[:] = [
                d for d in self.drawings
                if _is_protected(d) or (not clear_text and d.get('type') == 'text')
            ]

        # ── 2. Remove preview line actors (in self.renderer — they are temp) ────
        for attr in ['_preview_line_actor', '_continuous_line_actor', '_line_start_marker',
                    '_rectangle_preview_actor', '_ortho_edge_preview', '_ortho_poly_preview']:
            self._remove_preview_actor_2d(attr)

        # ── 3. Remove circle / freehand preview actors ───────────────────────────
        for attr in ['_circle_preview_actor_2d', '_circle_preview_actor',
                    '_freehand_preview_actor_2d']:
            self._remove_preview_actor_2d(attr)

        # Freehand in-progress preview (now in overlay)
        if hasattr(self, '_preview_actor') and self._preview_actor:
            _safe_remove(self._preview_actor, is_text=False)
            self._preview_actor = None

        # ── 4. Remove in-progress vertex marker arrays (all in overlay) ──────────
        for attr in ['_smartline_vertex_markers', '_polyline_vertex_markers', '_line_vertex_markers', '_hatch_vertex_markers']:
            markers = getattr(self, attr, [])
            for marker in markers:
                _safe_remove(marker, is_text=False)
            setattr(self, attr, [])

        # ── 5. Clear coordinate labels ───────────────────────────────────────────
        self.clear_coordinate_labels()

        # ── 6. Reset drawing state ───────────────────────────────────────────────
        self.temp_points = []
        self.selected = None
        self.selected_drawing = None
        self.left_down = False
        self.is_drawing_freehand = False
        self.multi_selected = []
        self._clear_temp_vertex_history()
        # self.active_tool = None

        # ── 7. Force full re-render ──────────────────────────────────────────────
        if hasattr(self, 'overlay_renderer') and self.overlay_renderer:
            self.overlay_renderer.Modified()
        self.renderer.Modified()
        try:
            self.interactor.GetRenderWindow().Render()
        except Exception:
            pass
        try:
            self.app.vtk_widget.render()
        except Exception:
            pass
        
        try:
            self._sync_fence_dialogs_after_drawing_change(
                removed_drawings=None,
                clear_all=bool(clear_classified),
            )
        except Exception as e:
            print(f"⚠️ Could not synchronize fence dialogs: {e}")

        # Purge selection set of any stale handles after the wholesale clear.
        self._notify_selection_drawings_changed()

        # Tell every registered finalize listener that the drawing list just
        # changed (so the Level Manager entity counts get recomputed). The
        # emit-drawing-removed path is now committed-only — without this extra
        # finalize emission the Level Manager would keep showing stale counts
        # after a canvas Clear until the user happens to draw or move a layer.
        try:
            callbacks = list(getattr(self, "_drawing_finalized_callbacks", None) or [])
        except Exception:
            callbacks = []
        for callback in callbacks:
            try:
                callback(None)
            except Exception as e:
                print(f"⚠️ post-clear finalize callback failed: {e}")
        # Clear CurveTool stale finalized list also.
        # This prevents cleared curves from coming back during
        # Element Select / Select Vertex Display.
        try:
            curve_tool = getattr(self.app, "curve_tool", None)
            if curve_tool is not None:
                curve_tool.clear_all_curves()
                curve_tool.finalized_actors = []
                curve_tool.selected_curve = None
                curve_tool.selected_curve_data = None
                curve_tool.points = []
                curve_tool.history_stack = []
                curve_tool.history_redo_stack = []
        except Exception:
            pass
                
        print("✅ Cleared all drawn vectors, previews, and arrows - point cloud preserved.")

        # Clear stale drawing/vertex selections after Clear All.
        # Without this, Element Select / Select Vertex Display can still show
        # vertices from drawings that were already cleared.
        try:
            self.selected = None
            self.selected_drawing = None
            self.multi_selected = []
            self.dragging_vertex = None
            self.vertex_moving = False
            self.moving_vertex_data = None
            self.selected_vertex_idx = None
        except Exception:
            pass

        # Clear element/block selection tool cached targets.
        try:
            element_tool = getattr(self, "_element_select_tool", None)
            if element_tool is not None:
                for attr in (
                    "selected_drawings",
                    "selected_items",
                    "selected_elements",
                    "selection",
                    "targets",
                    "selected_targets",
                    "picked_drawings",
                    "picked_items",
                ):
                    if hasattr(element_tool, attr):
                        value = getattr(element_tool, attr)
                        if isinstance(value, dict):
                            setattr(element_tool, attr, {})
                        elif isinstance(value, set):
                            setattr(element_tool, attr, set())
                        else:
                            setattr(element_tool, attr, [])
        except Exception:
            pass

        # Clear any generic selection manager cache if present.
        try:
            selection_manager = getattr(self, "_selection_manager", None)
            if selection_manager is not None:
                if hasattr(selection_manager, "clear"):
                    selection_manager.clear()
                elif hasattr(selection_manager, "clear_selection"):
                    selection_manager.clear_selection()
                elif hasattr(selection_manager, "selected"):
                    selection_manager.selected = []
        except Exception:
            pass

        # Clear any coordinate/vertex labels still visible.
        try:
            self.clear_coordinate_labels()
        except Exception:
            pass


    # ==================================================================
    # MicroStation-style delete operations (Phase 3)
    # ==================================================================
    def delete_selection(self, include_classified=False):
        """Delete every drawing currently in the SelectionManager.

        Records ONE undo snapshot before the batch so a single Ctrl+Z
        restores them all. Refuses classified fences by default to mirror
        the existing Shift+C behavior.
        """
        if self._selection_manager is None or self._selection_manager.is_empty():
            print("⚠️ delete_selection: nothing selected")
            try:
                self.app.statusBar().showMessage("Nothing selected to delete.", 2000)
            except Exception:
                pass
            return 0

        targets = list(self._selection_manager.get())
        # Already-saved (committed) drawings are locked: a Save freezes them so
        # select+delete leaves them intact, matching the classified-fence guard.
        committed_skipped = [d for d in targets if d.get("snt_committed")]
        targets = [d for d in targets if not d.get("snt_committed")]
        if not include_classified:
            skipped = [d for d in targets if d.get("classified_fence", False)]
            targets = [d for d in targets if not d.get("classified_fence", False)]
        else:
            skipped = []

        if not targets:
            extra = " saved" if committed_skipped and not skipped else ""
            try:
                self.app.statusBar().showMessage(
                    f"Selection contained only{extra} locked elements — refused.", 2500
                )
            except Exception:
                pass
            return 0

        try:
            self._save_state()
        except Exception as e:
            print(f"⚠️ delete_selection: _save_state failed: {e}")

        for d in targets:
            try:
                self._remove_drawing(d)
            except Exception as e:
                print(f"⚠️ delete_selection: _remove_drawing failed: {e}")

        # _remove_drawing already notifies the selection manager per-item.
        msg = f"Deleted {len(targets)} element(s)"
        if skipped:
            msg += f" (skipped {len(skipped)} classified)"
        try:
            self.app.statusBar().showMessage(msg, 2500)
        except Exception:
            pass
        print(f"🗑️ {msg}")
        # Force a final overlay-renderer commit so batched removals are flushed
        # in one render rather than relying on per-item Modified() calls.
        try:
            if hasattr(self, "overlay_renderer") and self.overlay_renderer:
                self.overlay_renderer.Modified()
        except Exception:
            pass
        try:
            self.renderer.Modified()
        except Exception:
            pass
        try:
            self.interactor.GetRenderWindow().Render()
        except Exception:
            pass
        try:
            self.app.vtk_widget.render()
        except Exception:
            pass
        return len(targets)

    def move_selection(self, dx, dy, save_undo=True):
        """Translate every selected drawing by (dx, dy) in world units.

        Rebuilds each drawing's actor in-place so VTK overlay stays consistent.
        save_undo=False skips the undo snapshot — used during interactive drag
        where the caller saves exactly one snapshot before the drag starts.
        """
        if self._selection_manager is None or self._selection_manager.is_empty():
            try:
                self.app.statusBar().showMessage("Nothing selected to move.", 2000)
            except Exception:
                pass
            return 0

        targets = [d for d in self._selection_manager.get()
                   if not d.get("classified_fence", False)]
        if not targets:
            try:
                self.app.statusBar().showMessage(
                    "Selection contained only classified fences — refused.", 2500
                )
            except Exception:
                pass
            return 0

        if save_undo:
            try:
                self._save_state()
            except Exception as e:
                print(f"⚠️ move_selection: _save_state failed: {e}")

        offset = np.array([dx, dy, 0.0], dtype=np.float64)
        moved = 0
        for d in targets:
            try:
                old_coords = self._get_drawing_coords(d)
                new_coords = [tuple(np.array(c, dtype=np.float64) + offset) for c in old_coords]
                self._set_transformed_coords(d, new_coords)

                # Rebuild main actor
                if "actor" in d and d["actor"]:
                    self._remove_actor_from_overlay(d["actor"])
                    d["actor"] = None

                dtype = d.get("type", "")
                if d.get("source") == "curve_tool":
                    old_points = d.get("control_points") or []
                    if old_points:
                        d["control_points"] = [tuple(np.array(c, dtype=np.float64) + offset) for c in old_points]
                    color = d.get("original_color", d.get("color", (0, 1, 0)))
                    width = d.get("original_width", 2)
                    curve_tool = getattr(getattr(self, "app", None), "curve_tool", None)
                    if curve_tool is not None and hasattr(curve_tool, "_create_curve_actor"):
                        actor = curve_tool._create_curve_actor(new_coords, color=color, width=width)
                    else:
                        actor = self._make_polyline_actor(new_coords, color=color, width=width, line_style="solid")
                    if actor is not None:
                        self._add_actor_to_overlay(actor)
                        d["actor"] = actor
                elif dtype == "text":
                    pos = new_coords[0] if new_coords else (0, 0, 0)
                    actor = self._make_scalable_text_actor(
                        text=d.get("text", ""),
                        position=pos,
                        color=d.get("original_text_color", d.get("original_color", (1, 1, 1))),
                        font_size=d.get("font_size", 75),
                        bold=d.get("bold", True),
                        italic=d.get("italic", False),
                        font_family=d.get("font_family", "Arial"),
                        justify=d.get("justify", "center"),
                    )
                    self._add_actor_to_overlay(actor)
                    d["actor"] = actor
                    d["bounds"] = actor.GetBounds()
                else:
                    color = d.get("original_color", (1, 0, 0))
                    width = d.get("original_width", 2)
                    style = d.get("original_style", "solid")
                    actor = self._make_polyline_actor(new_coords, color=color, width=width, line_style=style)
                    if actor is not None:
                        self._add_actor_to_overlay(actor)
                        d["actor"] = actor

                # Remove stale vertex markers
                for key in ("start_marker", "end_marker"):
                    if d.get(key):
                        try:
                            self._remove_actor_from_overlay(d[key])
                        except Exception:
                            pass
                        d[key] = None
                if d.get("vertex_markers"):
                    for m in d["vertex_markers"]:
                        try:
                            self._remove_actor_from_overlay(m)
                        except Exception:
                            pass
                    d["vertex_markers"] = []

                # Re-apply selection highlight on the new actor so the
                # SelectionManager's visual state stays consistent after
                # the actor rebuild.
                if self._selection_manager is not None and \
                        self._selection_manager.contains(d):
                    try:
                        self._highlight_line(d)
                    except Exception:
                        pass

                moved += 1
            except Exception as e:
                print(f"⚠️ move_selection: failed on element: {e}")

        try:
            self.app.statusBar().showMessage(f"Moved {moved} element(s).", 2500)
        except Exception:
            pass
        try:
            self.interactor.GetRenderWindow().Render()
        except Exception:
            pass
        try:
            self.app.vtk_widget.render()
        except Exception:
            pass
        return moved

    def copy_selection(self, dx=0.0, dy=0.0):
        """Duplicate every selected drawing, offset by (dx, dy) in world units.

        Adds copies to the drawing list, clears the old selection, then selects
        only the new copies so the user can immediately move them again.
        Records one undo snapshot before the batch.
        """
        if self._selection_manager is None or self._selection_manager.is_empty():
            try:
                self.app.statusBar().showMessage("Nothing selected to copy.", 2000)
            except Exception:
                pass
            return 0

        targets = [d for d in self._selection_manager.get()
                   if not d.get("classified_fence", False)]
        if not targets:
            try:
                self.app.statusBar().showMessage(
                    "Selection contained only classified fences — refused.", 2500
                )
            except Exception:
                pass
            return 0

        try:
            self._save_state()
        except Exception as e:
            print(f"⚠️ copy_selection: _save_state failed: {e}")

        offset = np.array([dx, dy, 0.0], dtype=np.float64)
        new_drawings = []
        for d in targets:
            try:
                old_coords = self._get_drawing_coords(d)
                new_coords = [tuple(np.array(c, dtype=np.float64) + offset) for c in old_coords]

                dtype = d.get("type", "")
                if d.get("source") == "curve_tool":
                    color = d.get("original_color", d.get("color", (0, 1, 0)))
                    width = d.get("original_width", 2)
                    curve_tool = getattr(getattr(self, "app", None), "curve_tool", None)
                    if curve_tool is not None and hasattr(curve_tool, "_create_curve_actor"):
                        actor = curve_tool._create_curve_actor(new_coords, color=color, width=width)
                    else:
                        actor = self._make_polyline_actor(new_coords, color=color, width=width, line_style="solid")
                    if actor is None:
                        continue
                    self._add_actor_to_overlay(actor)
                    entry = dict(d)
                    self._set_transformed_coords(entry, new_coords)
                    if entry.get("control_points"):
                        entry["control_points"] = [tuple(np.array(c, dtype=np.float64) + offset) for c in entry.get("control_points", [])]
                    entry["actor"] = actor
                    entry["original_color"] = color
                    entry["original_width"] = width
                    entry["type"] = "curve"
                    entry["source"] = "curve_tool"
                    new_drawings.append(entry)
                    if getattr(getattr(self, "app", None), "curve_tool", None) is not None:
                        try:
                            self.app.curve_tool.finalized_actors.append(entry)
                        except Exception:
                            pass
                    continue
                elif dtype == "text":
                    pos = new_coords[0] if new_coords else (0, 0, 0)
                    actor = self._make_scalable_text_actor(
                        text=d.get("text", ""),
                        position=pos,
                        color=d.get("original_text_color", d.get("original_color", (1, 1, 1))),
                        font_size=d.get("font_size", 75),
                        bold=d.get("bold", True),
                        italic=d.get("italic", False),
                        font_family=d.get("font_family", "Arial"),
                        justify=d.get("justify", "center"),
                    )
                    self._add_actor_to_overlay(actor)
                    entry = {
                        "type": dtype,
                        "coords": new_coords,
                        "actor": actor,
                        "text": d.get("text", ""),
                        "bounds": actor.GetBounds(),
                        "original_text_color": d.get("original_text_color", d.get("original_color", (1, 1, 1))),
                        "font_size": d.get("font_size", 75),
                        "bold": d.get("bold", True),
                        "italic": d.get("italic", False),
                        "font_family": d.get("font_family", "Arial"),
                        "justify": d.get("justify", "center"),
                        "scalable": True,
                    }
                else:
                    color = d.get("original_color", (1, 0, 0))
                    width = d.get("original_width", 2)
                    style = d.get("original_style", "solid")
                    actor = self._make_polyline_actor(new_coords, color=color, width=width, line_style=style)
                    if actor is None:
                        continue
                    self._add_actor_to_overlay(actor)
                    entry = {
                        "type": dtype,
                        "coords": new_coords,
                        "actor": actor,
                        "original_color": color,
                        "original_width": width,
                        "original_style": style,
                        "vertex_markers": [],
                    }
                    if d.get("center") is not None:
                        entry["center"] = tuple(np.array(d["center"], dtype=np.float64) + offset)
                    if d.get("radius") is not None:
                        entry["radius"] = d["radius"]

                self.drawings.append(entry)
                new_drawings.append(entry)
            except Exception as e:
                print(f"⚠️ copy_selection: failed on element: {e}")

        if new_drawings and self._selection_manager is not None:
            try:
                self._selection_manager.clear()
                for entry in new_drawings:
                    self._selection_manager.apply([entry], "add")
            except Exception as e:
                print(f"⚠️ copy_selection: selection update failed: {e}")

        try:
            self.app.statusBar().showMessage(f"Copied {len(new_drawings)} element(s).", 2500)
        except Exception:
            pass
        try:
            self.interactor.GetRenderWindow().Render()
        except Exception:
            pass
        try:
            self.app.vtk_widget.render()
        except Exception:
            pass
        return len(new_drawings)

    def delete_by_fence(self, fence_drawing, mode="inside", include_classified=False):
        """Delete drawings spatially related to an existing fence drawing.

        mode:
          'inside'   — drawings fully inside the fence
          'outside'  — drawings fully outside the fence
          'crossing' — drawings whose segments cross the fence boundary
        """
        if fence_drawing is None:
            return 0
        coords = fence_drawing.get("coords") or []
        if len(coords) < 3:
            print("⚠️ delete_by_fence: fence has < 3 points")
            return 0

        poly_xy = [(c[0], c[1]) for c in coords if c and len(c) >= 2]
        if len(poly_xy) < 3:
            return 0

        def pt_in_poly(px, py):
            inside = False
            n = len(poly_xy)
            j = n - 1
            for i in range(n):
                xi, yi = poly_xy[i]
                xj, yj = poly_xy[j]
                if ((yi > py) != (yj > py)):
                    denom = (yj - yi)
                    if denom != 0.0:
                        if px < (xj - xi) * (py - yi) / denom + xi:
                            inside = not inside
                j = i
            return inside

        def seg_intersect(p1, p2, p3, p4):
            def ccw(a, b, c):
                return (c[1] - a[1]) * (b[0] - a[0]) > (b[1] - a[1]) * (c[0] - a[0])
            return (ccw(p1, p3, p4) != ccw(p2, p3, p4) and
                    ccw(p1, p2, p3) != ccw(p1, p2, p4))

        def fence_edges():
            for i in range(len(poly_xy)):
                yield poly_xy[i], poly_xy[(i + 1) % len(poly_xy)]

        targets = []
        for d in list(self.drawings):
            if d is fence_drawing:
                continue
            if not include_classified and d.get("classified_fence", False):
                continue
            dcoords = d.get("coords") or []
            d_xy = [(c[0], c[1]) for c in dcoords if c and len(c) >= 2]
            if not d_xy:
                continue

            all_inside = all(pt_in_poly(px, py) for (px, py) in d_xy)
            any_inside = any(pt_in_poly(px, py) for (px, py) in d_xy)

            crosses = False
            if len(d_xy) >= 2 and not all_inside:
                for i in range(len(d_xy) - 1):
                    for (e0, e1) in fence_edges():
                        if seg_intersect(d_xy[i], d_xy[i + 1], e0, e1):
                            crosses = True
                            break
                    if crosses:
                        break

            keep = False
            if mode == "inside" and all_inside:
                keep = True
            elif mode == "outside" and (not any_inside) and not crosses:
                keep = True
            elif mode == "crossing" and crosses:
                keep = True

            if keep:
                targets.append(d)

        if not targets:
            try:
                self.app.statusBar().showMessage(
                    f"Fence ({mode}): no elements matched.", 2000
                )
            except Exception:
                pass
            return 0

        try:
            self._save_state()
        except Exception:
            pass
        for d in targets:
            try:
                self._remove_drawing(d)
            except Exception as e:
                print(f"⚠️ delete_by_fence: _remove_drawing failed: {e}")

        msg = f"Fence ({mode}) deleted {len(targets)} element(s)"
        try:
            self.app.statusBar().showMessage(msg, 2500)
        except Exception:
            pass
        print(f"🗑️ {msg}")
        try:
            self.app.vtk_widget.render()
        except Exception:
            pass
        return len(targets)

    def activate_element_select_tool(self, method="individual", delete_on_click=False):
        """Bring the MicroStation-style element selection tool online.

        method: 'individual' | 'block' | 'shape' | 'cline'
        delete_on_click=True turns it into 'Delete Element' mode.
        """
        app = getattr(self, "app", None)
        active_tools = getattr(app, "_active_tools", set()) if app is not None else set()

        if getattr(app, "cross_section_active", False) or "cross_section" in active_tools:
            try:
                print("🛑 Switching from cross-section to element selection")
                app.deactivate_cross_section_tool()
            except Exception:
                try:
                    app.set_cross_cursor_active(False, "cross_section")
                except Exception:
                    pass

        if getattr(app, "cut_section_mode_on", False) or "cut_section" in active_tools:
            try:
                print("🛑 Switching from cut-section to element selection")
                app._deactivate_pending_cut_section_tool("switching to element selection")
            except Exception:
                try:
                    app.set_cross_cursor_active(False, "cut_section")
                except Exception:
                    pass

        # Stand down classification tool the same way cross/cut-section are stood down.
        # Same call path used by _enter_measure_tab_mode / _enter_identify_tab_mode.
        classify_tool = getattr(app, "active_classify_tool", None)
        if classify_tool and classify_tool not in ("cross_section", "cut_section"):
            try:
                print(f"🛑 Switching from {classify_tool} classification to element selection")
                app.deactivate_classification_tool(preserve_cross_section=True)
            except Exception as e:
                print(f"⚠️ Failed to deactivate classification before element select: {e}")

        # Stand down identification tool so its click observer doesn't fight the
        # element-select observer. Use the ribbon path so the ribbon button state
        # resets too — matches _enter_measure_tab_mode / _enter_identify_tab_mode.
        # All access is defensive because ribbon_manager.ribbons may not exist
        # during app startup/shutdown.
        identify_tool = getattr(app, "identification_tool", None)
        if identify_tool is not None and getattr(identify_tool, "active", False):
            try:
                rm = getattr(app, "ribbon_manager", None)
                ribbons = getattr(rm, "ribbons", None) if rm is not None else None
                identify_ribbon = ribbons.get("identify") if isinstance(ribbons, dict) else None
                if identify_ribbon is not None and hasattr(identify_ribbon, "deactivate_all_tools"):
                    identify_ribbon.deactivate_all_tools()
                else:
                    identify_tool.deactivate()
                print("🛑 Identification tool deactivated for element selection")
            except Exception as e:
                print(f"⚠️ Failed to deactivate identification before element select: {e}")

        if self._element_select_tool is None:
            try:
                from gui.element_select_tool import ElementSelectTool
                self._element_select_tool = ElementSelectTool(self.app, self)
            except Exception as e:
                print(f"⚠️ ElementSelectTool init failed: {e}")
                return None
        tool = self._element_select_tool
        tool.set_delete_on_click(delete_on_click)
        tool.activate(method=method)
        return tool

    def deactivate_element_select_tool(self):
        if self._element_select_tool is not None:
            try:
                self._element_select_tool.deactivate()
            except Exception as e:
                print(f"⚠️ ElementSelectTool deactivate failed: {e}")
    # ==================================================================
    # End Phase 3
    # ==================================================================

    def _sync_fence_dialogs_after_drawing_change(self, removed_drawings=None, clear_all=False):
        """
        Keep all fence-related dialogs aligned with digitizer drawing changes.
        Prevents stale fence references from recreating ghost highlight actors.
        """
        app = getattr(self, 'app', None)
        if app is None:
            return

        removed_drawings = list(removed_drawings or [])

        def _coords_key(shape):
            try:
                return tuple(tuple(c[:2]) for c in (shape.get('coords', []) or []))
            except Exception:
                return ()

        removed_ids = {id(d) for d in removed_drawings}
        removed_actor_ids = {
            id(d.get('actor')) for d in removed_drawings
            if isinstance(d, dict) and d.get('actor') is not None
        }
        removed_keys = {_coords_key(d) for d in removed_drawings}

        dialog_attrs = (
            'inside_fence_dialog',
            'by_class_dialog',
            'closed_by_class_dialog',
            'height_convert_dialog',
        )

        by_class_ribbon = None
        if hasattr(app, 'ribbon_manager') and app.ribbon_manager:
            by_class_ribbon = getattr(app.ribbon_manager, 'ribbons', {}).get('by_class')

        for attr in dialog_attrs:
            dlg = None
            if by_class_ribbon:
                dlg = getattr(by_class_ribbon, attr, None)
            if dlg is None:
                dlg = getattr(app, attr, None)
            if dlg is None:
                continue

            try:
                selected = list(getattr(dlg, 'selected_fences', []) or [])
            except Exception:
                selected = []

            selected_changed = False

            if clear_all:
                if hasattr(dlg, 'selected_fences') and selected:
                    dlg.selected_fences = []
                    selected_changed = True
            elif selected:
                kept = []
                for fence in selected:
                    drop = False
                    if id(fence) in removed_ids:
                        drop = True
                    elif _coords_key(fence) in removed_keys:
                        drop = True
                    elif (
                        isinstance(fence, dict)
                        and fence.get('actor') is not None
                        and id(fence.get('actor')) in removed_actor_ids
                    ):
                        drop = True

                    if not drop:
                        kept.append(fence)

                if len(kept) != len(selected):
                    dlg.selected_fences = kept
                    selected_changed = True

            if clear_all:
                if hasattr(dlg, '_clear_fence_highlights'):
                    try:
                        dlg._clear_fence_highlights()
                    except Exception:
                        pass
                if hasattr(dlg, 'update_fence_display'):
                    try:
                        dlg.update_fence_display()
                    except Exception:
                        pass
                if hasattr(dlg, 'fence_status') and dlg.fence_status:
                    try:
                        dlg.fence_status.setText("❌ No fence selected")
                        dlg.fence_status.setStyleSheet("""
                            QLabel {
                                padding: 6px;
                                background-color: #2c2c2c;
                                border-radius: 3px;
                                color: #f44336;
                            }
                        """)
                    except Exception:
                        pass
            elif selected_changed:
                if hasattr(dlg, '_clear_fence_highlights'):
                    try:
                        dlg._clear_fence_highlights()
                    except Exception:
                        pass
                if getattr(dlg, 'selected_fences', None) and hasattr(dlg, '_restore_highlights_from_data'):
                    try:
                        dlg._restore_highlights_from_data()
                    except Exception:
                        pass
                elif hasattr(dlg, '_prune_stale_fences'):
                    try:
                        dlg._prune_stale_fences()
                    except Exception:
                        pass

                if hasattr(dlg, 'update_fence_display'):
                    try:
                        dlg.update_fence_display()
                    except Exception:
                        pass

                if not getattr(dlg, 'selected_fences', None) and hasattr(dlg, 'fence_status') and dlg.fence_status:
                    try:
                        dlg.fence_status.setText("❌ No fence selected")
                        dlg.fence_status.setStyleSheet("""
                            QLabel {
                                padding: 6px;
                                background-color: #2c2c2c;
                                border-radius: 3px;
                                color: #f44336;
                            }
                        """)
                    except Exception:
                        pass
                elif getattr(dlg, 'selected_fences', None) and hasattr(dlg, 'fence_status') and dlg.fence_status:
                    try:
                        fences = dlg.selected_fences
                        shape_fc = sum(1 for f in fences if f.get('source') != 'curve_tool')
                        curve_fc = sum(1 for f in fences if f.get('source') == 'curve_tool')
                        total_pts = sum(len(f.get('coords', [])) for f in fences)
                        mode_text = "🔄 PERMANENT" if getattr(dlg, 'permanent_fence_mode', False) else "TEMP"
                        parts = []
                        if shape_fc: parts.append(f"{shape_fc} shape(s)")
                        if curve_fc: parts.append(f"{curve_fc} curve(s)")
                        dlg.fence_status.setText(f"✅ {' + '.join(parts)} selected ({total_pts} pts) - {mode_text}")
                        dlg.fence_status.setStyleSheet("QLabel { padding: 6px; background-color: #1b5e20; border-radius: 3px; color: #4caf50; font-weight: bold; }")
                    except Exception:
                        pass

            if hasattr(dlg, 'update_fence_display'):
                try:
                    dlg.update_fence_display()
                except Exception:
                    pass

        # Also clear temporary AI fence-run preview actors if dialog still exists.
        ai_dlg = getattr(app, '_ai_fence_dialog', None)
        if ai_dlg is not None:
            try:
                if hasattr(ai_dlg, '_clear_hover_actor'):
                    ai_dlg._clear_hover_actor()
                if hasattr(ai_dlg, '_clear_selected_actors'):
                    ai_dlg._clear_selected_actors()
            except Exception:
                pass

    # ---------------- COORDINATE LABELS ----------------
    def show_vertex_coordinates(self, points):
        """Display constant-size sphere markers at each vertex."""
        self.clear_coordinate_labels()
        self.coord_labels = []

        for pt in points:
            marker = self._add_endpoint_sphere(pt, color=(1.0, 1.0, 0.0), radius=None)
            self.coord_labels.append(marker)

        self.app.vtk_widget.render()
        print(f"📍 Displayed {len(points)} vertex markers (constant size).")

    def clear_coordinate_labels(self):
        """Remove all coordinate labels and markers without leaking memory."""
        if not hasattr(self, "coord_labels"):
            self.coord_labels = []
            return
        
        print(f"🧹 Clearing {len(self.coord_labels)} coordinate labels...")
        
        for lbl in self.coord_labels:
            try:
                if hasattr(self, 'text_overlay_renderer') and self.text_overlay_renderer:
                    self.text_overlay_renderer.RemoveActor(lbl)
                if hasattr(self, 'overlay_renderer') and self.overlay_renderer:
                    self.overlay_renderer.RemoveActor(lbl)
                self.renderer.RemoveActor(lbl)  # harmless fallback
            except Exception as e:
                print(f"⚠️ Failed to remove marker: {e}")
        
        self.coord_labels = []
        self.renderer.Modified()
        print("✅ Coordinate labels cleared")

    # ============================================================
    # PATCH 12: Update rebind_drawings for scalable text
    # ============================================================

    def rebind_drawings(self):
        """Restores all 3D actors AND 2D overlays after a renderer clear."""
        if not hasattr(self, "drawings") or not self.drawings:
            return
            
        print(f"🔄 Rebinding {len(self.drawings)} drawings to renderer...")
        
        for d in self.drawings:
            is_text = d.get('type') == 'text'
            is_scalable = d.get('scalable', False)
            
            if "actor" in d and d["actor"]:
                if is_text:
                    if is_scalable:
                        # Scalable text goes to overlay
                        self._add_actor_to_overlay(d["actor"])
                    else:
                        # Legacy billboard text
                        self.renderer.AddViewProp(d["actor"])
                else:
                    self._add_actor_to_overlay(d["actor"])
                
            if d.get("start_marker"): 
                self._add_actor_to_overlay(d["start_marker"])
            if d.get("end_marker"): 
                self._add_actor_to_overlay(d["end_marker"])
            
            if d.get("vertex_markers"):
                for marker in d["vertex_markers"]:
                    self._add_actor_to_overlay(marker)
                    
            if d.get("arrow_actor"):
                arrows = d["arrow_actor"]
                if not isinstance(arrows, list): arrows = [arrows]
                for arr in arrows:
                    self.renderer.AddActor2D(arr)
                    
        self.renderer.Modified()
        try: 
            self.app.vtk_widget.render()
        except Exception:
            pass
        print("✅ Rebind complete.")


    def debug_test_sphere(self):
        if self.drawings and self.drawings[0]["coords"]:
            x, y, z = self.drawings[0]["coords"][0]
        else:
            x, y, z = 0, 0, 0
        sphere = vtk.vtkSphereSource()
        sphere.SetRadius(1.0)
        sphere.SetCenter(x, y, z)
        sphere.SetThetaResolution(32)
        sphere.SetPhiResolution(32)
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(sphere.GetOutputPort())
        marker = vtk.vtkActor()
        marker.SetMapper(mapper)
        marker.GetProperty().SetColor(1, 0, 1)
        marker.GetProperty().SetOpacity(1.0)
        self.renderer.AddActor(marker)
        self.app.vtk_widget.interactor.GetRenderWindow().Render()
        print(f"DEBUG: drew sphere at ({x},{y},{z}) with radius 5.0")

    def _focus_camera_on_points(self, points):
        pts = np.array(points)
        if pts.shape[0] == 0:
            return
        min_xyz = pts.min(axis=0)
        max_xyz = pts.max(axis=0)
        center = (min_xyz + max_xyz) / 2
        radius = np.linalg.norm(max_xyz - min_xyz) * 1.5 + 5

        camera = self.renderer.GetActiveCamera()
        camera.SetFocalPoint(*center)
        camera.SetPosition(center[0], center[1] - radius, center[2] + radius)
        camera.SetViewUp(0, 0, 1)
        camera.SetClippingRange(0.01, radius * 100)
        self.renderer.ResetCameraClippingRange()
        self.app.vtk_widget.render()

    def set_vertex_move_mode(self, enabled=True):
        """Toggle vertex move mode on/off."""
        self.vertex_move_mode = enabled
        
        if enabled:
            print("🔵 Vertex Move Mode ON - Click and drag vertices")
            self.active_tool = None
            
            self.interactor.RemoveObservers("MouseMoveEvent")
            self.interactor.RemoveObservers("LeftButtonPressEvent")
            self.interactor.RemoveObservers("LeftButtonReleaseEvent")
            
            self.interactor.AddObserver("MouseMoveEvent", self._on_vertex_move_hover, 1.0)
            self.interactor.AddObserver("LeftButtonPressEvent", self._on_vertex_move_start, 1.0)
            self.interactor.AddObserver("LeftButtonReleaseEvent", self._on_vertex_move_end, 1.0)
        else:
            print("⚪ Vertex Move Mode OFF")
            self.dragging_vertex = None
            
            if self.vertex_hover_marker:
                self._remove_actor_from_overlay(self.vertex_hover_marker)
                self.vertex_hover_marker = None
            
            if self.vertex_drag_marker:
                self._remove_actor_from_overlay(self.vertex_drag_marker)
                self.vertex_drag_marker = None
            
            self.interactor.RemoveObservers("MouseMoveEvent")
            self.interactor.RemoveObservers("LeftButtonPressEvent")
            self.interactor.RemoveObservers("LeftButtonReleaseEvent")
            
            self.interactor.AddObserver("LeftButtonPressEvent", self._on_left_press, 1.0)
            self.interactor.AddObserver("MouseMoveEvent", self._on_mouse_move, 1.0)
            self.interactor.AddObserver("LeftButtonReleaseEvent", self._on_left_release, 1.0)
            
            
    def _rebuild_drawing_actor(self, drawing):
        """
        Rebuild the visual actor for a drawing after coordinates changed.
        Preserves color, width, arrows, and markers.

        ✅ ZOOM FIX: _make_polyline_actor now sets actor.SetPosition(origin)
        for float64 precision at high zoom.  We must call _add_actor_to_overlay
        BEFORE reading GetBounds() so VTK's pipeline has applied the position
        transform — otherwise bounds are stale/wrong at maximum zoom.
        """
        try:
            # Text drawings use vtkTextActor3D placed at a world position —
            # they are not built from coords via _make_polyline_actor.
            # Attempting to rebuild them through the polyline path yields None
            # and crashes on GetBounds().  The actor is already in the renderer
            # from the original placement call; nothing to rebuild here.
            if drawing.get("type") == "text":
                return

            coords = drawing['coords']
            color = drawing.get('original_color', drawing.get('color', (1, 0, 0)))
            width = drawing.get('original_width', 2)
            line_style = drawing.get('original_style', 'solid')

            if 'actor' in drawing and drawing['actor']:
                self._remove_actor_from_overlay(drawing['actor'])

            if drawing.get("source") == "curve_tool":
                curve_tool = getattr(getattr(self, "app", None), "curve_tool", None)
                if curve_tool is not None and hasattr(curve_tool, "_create_curve_actor"):
                    new_actor = curve_tool._create_curve_actor(coords, color=color, width=width)
                else:
                    new_actor = self._make_polyline_actor(
                        coords, color=color, width=width, line_style=line_style
                    )
            else:
                new_actor = self._make_polyline_actor(
                    coords, color=color, width=width, line_style=line_style)

            # ✅ Add to renderer FIRST so VTK pipeline applies SetPosition()
            # transform before we read GetBounds() — critical at high zoom.
            self._add_actor_to_overlay(new_actor)

            drawing['actor'] = new_actor
            # ✅ Read bounds AFTER actor is in the renderer
            drawing['bounds'] = new_actor.GetBounds()

            # Rebuild fill actor if drawing had one
            if drawing.get("original_fill_color") is not None:
                if drawing.get("fill_actor"):
                    try:
                        self._remove_actor_from_overlay(drawing["fill_actor"])
                    except Exception:
                        pass
                new_fill = self._make_fill_actor(
                    coords,
                    drawing["original_fill_color"],
                    drawing.get("original_fill_opacity", 0.3),
                )
                if new_fill is not None:
                    self._add_actor_to_overlay(new_fill)
                drawing["fill_actor"] = new_fill

            # Rebuild hatch actors if this is a hatcharea drawing
            if drawing.get("type") == "hatcharea":
                from gui.hatch_area_tool import generate_hatch_lines
                for ha in drawing.get("hatch_actors", []):
                    try: self._remove_actor_from_overlay(ha)
                    except Exception: pass
                spacing = drawing.get("hatch_spacing", 800.0)
                angle = drawing.get("hatch_angle", 0.0)
                segs = generate_hatch_lines(coords, spacing, angle)
                new_hatch = []
                for sa in self._make_hatch_segments_actors(segs, color, width, line_style):
                    self._add_actor_to_overlay(sa)
                    new_hatch.append(sa)
                drawing["hatch_actors"] = new_hatch

            if 'vertex_markers' in drawing and drawing['vertex_markers']:
                for marker in drawing['vertex_markers']:
                    try:
                        self._remove_actor_from_overlay(marker)
                    except Exception:
                        pass

                drawing['vertex_markers'] = []
                for i, pt in enumerate(coords):
                    if i == 0:
                        marker_color = (0, 1, 0)
                    elif i == len(coords) - 1:
                        marker_color = (1, 0, 0)
                    else:
                        marker_color = (1, 1, 0)

                    marker = self._add_endpoint_sphere(pt, color=marker_color, radius=0.05)
                    marker.GetProperty().SetOpacity(0.8)
                    drawing['vertex_markers'].append(marker)

            # Rebuild start_marker / end_marker for line_segment drawings
            if drawing.get('type') == 'line_segment':
                for mk_key, coord_idx in (('start_marker', 0), ('end_marker', -1)):
                    old_mk = drawing.get(mk_key)
                    mk_color = (0, 1, 0) if coord_idx == 0 else (1, 0, 0)
                    if old_mk is not None:
                        try:
                            c = old_mk.GetProperty().GetColor()
                            mk_color = (c[0], c[1], c[2])
                            self._remove_actor_from_overlay(old_mk)
                        except Exception:
                            pass
                    drawing[mk_key] = self._add_endpoint_sphere(
                        coords[coord_idx], color=mk_color, radius=0.05
                    )

            if 'arrow_actor' in drawing and drawing['arrow_actor']:
                arrows = drawing['arrow_actor']
                if not isinstance(arrows, list):
                    arrows = [arrows]
                for arr in arrows:
                    try:
                        self.renderer.RemoveActor2D(arr)
                    except Exception:
                        pass

                if getattr(self, f"{drawing['type']}_arrow_mode", False):
                    drawing['arrow_actor'] = self._add_arrow_to_line(coords)
                else:
                    drawing['arrow_actor'] = None

            self.renderer.Modified()

        except Exception as e:
            print(f"⚠️ Failed to rebuild drawing actor: {e}")
            import traceback
            traceback.print_exc()
    
    def _on_vertex_move_hover(self, obj, evt):
        """Show cyan marker when hovering over a vertex."""
        if self.dragging_vertex:
            self._on_vertex_drag(obj, evt)
            return
        
        x, y = self.interactor.GetEventPosition()
        nearest_vertex, nearest_drawing, vertex_idx = self._find_nearest_vertex_at_position(x, y, tolerance=15.0)
        
        if nearest_vertex is not None:
            if not self.vertex_hover_marker:
                self.vertex_hover_marker = self._add_endpoint_sphere(
                    nearest_vertex, color=(0, 1, 1), radius=0.08
                )
            else:
                self.vertex_hover_marker.SetPosition(*nearest_vertex)
                self.vertex_hover_marker.SetVisibility(True)
            self.app.vtk_widget.render()
        else:
            if self.vertex_hover_marker:
                self.vertex_hover_marker.SetVisibility(False)
                self.app.vtk_widget.render()
    
    def _on_vertex_move_start(self, obj, evt):
        """Click on vertex to start dragging."""
        x, y = self.interactor.GetEventPosition()
        nearest_vertex, nearest_drawing, vertex_idx = self._find_nearest_vertex_at_position(x, y, tolerance=15.0)
        
        if nearest_vertex and nearest_drawing:
            self._save_state()
            
            self.dragging_vertex = {
                'drawing': nearest_drawing,
                'vertex_index': vertex_idx,
                'original_pos': nearest_vertex
            }
            
            if self.vertex_drag_marker:
                self._remove_actor_from_overlay(self.vertex_drag_marker)
            
            self.vertex_drag_marker = self._add_endpoint_sphere(
                nearest_vertex, color=(1, 1, 0), radius=0.12
            )
            
            if self.vertex_hover_marker:
                self.vertex_hover_marker.SetVisibility(False)
            
            print(f"🔵 Dragging vertex {vertex_idx}")
            self.app.vtk_widget.render()
    
    def _on_vertex_drag(self, obj, evt):
        """Update vertex position while dragging."""
        if not self.dragging_vertex:
            return
        
        new_pos = self._get_mouse_world()
        
        drawing = self.dragging_vertex['drawing']
        vertex_idx = self.dragging_vertex['vertex_index']
        
        coords = drawing['coords']
        coords[vertex_idx] = tuple(new_pos)
        
        if self.vertex_drag_marker:
            self.vertex_drag_marker.SetPosition(*new_pos)
        
        self._rebuild_drawing_actor(drawing)
        
        if self.selected_drawing is drawing:
            self.clear_coordinate_labels()
            self.show_vertex_coordinates(coords)
        
        self.app.vtk_widget.render()
    
    def _on_vertex_move_end(self, obj, evt):
        """Release to finish dragging."""
        if not self.dragging_vertex:
            return
        
        drawing = self.dragging_vertex['drawing']
        vertex_idx = self.dragging_vertex['vertex_index']
        new_pos = drawing['coords'][vertex_idx]
        
        print(f"✅ Vertex {vertex_idx} moved to {new_pos}")
        
        if self.vertex_drag_marker:
            self._remove_actor_from_overlay(self.vertex_drag_marker)
            self.vertex_drag_marker = None
        
        self._rebuild_drawing_actor(drawing)
        self.dragging_vertex = None
        self.app.vtk_widget.render()
    
    def _find_nearest_vertex_at_position(self, x, y, tolerance=15.0):
        """Find nearest vertex to screen position."""
        min_dist = tolerance
        nearest_vertex = None
        nearest_drawing = None
        nearest_idx = None
        
        for drawing in self.drawings:
            if 'coords' not in drawing:
                continue
            
            coords = drawing['coords']
            for i, vertex in enumerate(coords):
                self.renderer.SetWorldPoint(vertex[0], vertex[1], vertex[2], 1.0)
                self.renderer.WorldToDisplay()
                screen_pos = self.renderer.GetDisplayPoint()
                
                dx = screen_pos[0] - x
                dy = screen_pos[1] - y
                dist = (dx*dx + dy*dy) ** 0.5
                
                if dist < min_dist:
                    min_dist = dist
                    nearest_vertex = vertex
                    nearest_drawing = drawing
                    nearest_idx = i
        
        return nearest_vertex, nearest_drawing, nearest_idx
    
    def save_drawings_to_file(self, filepath):
        """Save all drawings to a JSON file."""
        import json
        import os
        
        try:
            drawings_data = []
            for d in self.drawings:
                drawing_info = {
                    "type": d["type"],
                    "coords": d["coords"],
                }
                
                if "text" in d:
                    drawing_info["text"] = d["text"]
                
                # Save text-specific properties
                if d["type"] == "text":
                    if "original_text_color" in d:
                        drawing_info["color"] = list(d["original_text_color"])
                    if "font_size" in d:
                        drawing_info["font_size"] = d["font_size"]
                    if "bold" in d:
                        drawing_info["bold"] = d["bold"]
                    if "font_family" in d:
                        drawing_info["font_family"] = d["font_family"]
                    if "scalable" in d:
                        drawing_info["scalable"] = d["scalable"]
                
                drawings_data.append(drawing_info)
            
            if not filepath.endswith('.json'):
                base_name = os.path.splitext(filepath)[0]
                json_path = base_name + "_drawings.json"
            else:
                json_path = filepath
            
            with open(json_path, 'w') as f:
                json.dump(drawings_data, f, indent=2)
            
            print(f"✅ Saved {len(drawings_data)} drawings to: {json_path}")
            return json_path
            
        except Exception as e:
            print(f"⚠️ Failed to save drawings: {e}")
            import traceback
            traceback.print_exc()
            return None

    def load_drawings_from_file(self, filepath):
        """Load drawings from a JSON file and recreate them in the scene."""
        import json
        import os
        
        try:
            if not filepath.endswith('.json'):
                base_name = os.path.splitext(filepath)[0]
                json_path = base_name + "_drawings.json"
            else:
                json_path = filepath
            
            if not os.path.exists(json_path):
                print(f"ℹ️ No drawings file found: {json_path}")
                return False
            
            with open(json_path, 'r') as f:
                drawings_data = json.load(f)
            
            for d in list(self.drawings):
                try:
                    if "actor" in d and d["actor"] is not None:
                        if d.get('type') == 'text':
                            self.renderer.RemoveActor(d["actor"])
                        else:
                            self._remove_actor_from_overlay(d["actor"])
                except Exception:
                    pass
            self.drawings.clear()
            
            for d in drawings_data:
                drawing_type = d["type"]
                coords = [tuple(c) for c in d["coords"]]
                
                if drawing_type in ("smartline", "centerline", "line", "freehand", "rectangle", "polygon", "circle"):
                    actor = self._make_polyline_actor(coords)

                    if "color" in d:
                        actor.GetProperty().SetColor(*d["color"])
                    if "width" in d:
                        actor.GetProperty().SetLineWidth(d["width"])

                    # ✅ ZOOM FIX: Add to renderer BEFORE reading GetBounds()
                    # so VTK pipeline applies actor.SetPosition(origin) transform.
                    self._add_actor_to_overlay(actor)

                    drawing_entry = {
                        "type": drawing_type,
                        "coords": coords,
                        "actor": actor,
                        "bounds": actor.GetBounds()  # correct now — actor is in renderer
                    }
                    
                    if "color" in d:
                        drawing_entry["original_color"] = tuple(d["color"])
                    if "width" in d:
                        drawing_entry["original_width"] = d["width"]
                    
                    self.drawings.append(drawing_entry)
                    
                elif drawing_type == "text":
                    pos = coords[0]
                    text = d.get("text", "Text")
                    t_color = d.get("color", d.get("original_text_color", (0, 0, 1)))
                    if isinstance(t_color, list):
                        t_color = tuple(t_color)
                    # Normalize color to 0-1 range
                    if t_color and all(c <= 1.0 for c in t_color):
                        pass
                    elif t_color:
                        t_color = tuple(c / 255.0 for c in t_color)
                    else:
                        t_color = (0, 0, 1)
                    
                    t_font_size = d.get("font_size", 75)
                    t_bold = d.get("bold", True)
                    t_italic = d.get("italic", False)
                    t_font_family = d.get("font_family", "Arial")
                    t_justify = d.get("justify", "center")
                    
                    # Create scalable text actor
                    t = self._make_scalable_text_actor(
                        text=text,
                        position=pos,
                        color=t_color,
                        font_size=t_font_size,
                        bold=t_bold,
                        italic=t_italic,
                        font_family=t_font_family,
                        justify=t_justify,
                    )

                    if not bool(getattr(self, "draw_text_visible", True)):
                        t.SetVisibility(0)

                    self._add_actor_to_overlay(t)

                    
                    self.drawings.append({
                        "type": "text",
                        "coords": [tuple(pos)],
                        "actor": t,
                        "text": text,
                        "bounds": t.GetBounds(),
                        "original_text_color": t_color,
                        "font_size": t_font_size,
                        "bold": t_bold,
                        "italic": t_italic,
                        "font_family": t_font_family,
                        "justify": t_justify,
                        "scalable": True,
                        "_draw_text_visible": bool(getattr(self, "draw_text_visible", True)),
                    })

            
            self.app.vtk_widget.render()
            
            print(f"✅ Loaded {len(drawings_data)} drawings from: {json_path}")
            return True
            
        except Exception as e:
            print(f"⚠️ Failed to load drawings: {e}")
            import traceback
            traceback.print_exc()
            return False

    def auto_save_drawings(self, las_filepath):
        if not self.drawings:
            print("ℹ️ No drawings to save")
            return None
        path = self.save_drawings_to_file(las_filepath)
        if path:
            print(f"💾 Auto-saved drawings to: {path}")
        return path

    def auto_load_drawings(self, las_filepath):
        success = self.load_drawings_from_file(las_filepath)
        if success:
            try:
                self.renderer.Modified()
                self.app.vtk_widget.interactor.GetRenderWindow().Render()
                self.app.vtk_widget.render()
            except Exception:
                pass
        return success


    # ----------Text Label Helper----------
    def _apply_text_properties(self, text_prop, font_family, font_size, bold, italic, color):
        """Apply text properties to VTK text safely."""
        font_size = max(0, min(999, int(75 if font_size is None else font_size)))
        font_family = (font_family or "Arial").strip()

        text_prop.SetColor(*color)
        text_prop.SetBold(1 if bold else 0)
        text_prop.SetItalic(1 if italic else 0)
        text_prop.SetFontSize(font_size)

        self._apply_vtk_font_family(text_prop, font_family, bold=bold, italic=italic)
        print(f"🔧 Text font applied: {font_family} -> VTK Enum {text_prop.GetFontFamily()}")


    def _cleanup_text_tool(self):
        """
        Fully reset all text tool state before switching to another tool.
        Called at the TOP of set_tool() whenever active_tool == 'text'.
        """
        print("🧹 Cleaning up text tool state before switch")

        # 1. Cancel any in-progress text placement
        if getattr(self, '_placing_text', False):
            try:
                dlg = getattr(self, '_text_dialog', None)
                if dlg is not None:
                    try:
                        dlg.reject()
                    except Exception:
                        pass
                self._text_dialog = None
            except Exception as e:
                print(f"  ⚠️ Could not close text dialog: {e}")

        self._text_dialog = None
        self._text_ui_editing = False

        # Close any open text context menu/timer
        try:
            timer = getattr(self, "_text_context_menu_timer", None)
            if timer is not None and timer.isActive():
                timer.stop()
        except Exception:
            pass
        try:
            menu = getattr(self, "_text_context_menu", None)
            if menu is not None:
                try:
                    menu.close()
                except Exception:
                    pass
                try:
                    menu.deleteLater()
                except Exception:
                    pass
            self._text_context_menu = None
        except Exception:
            pass

        # 2. Reset ALL text-specific state flags
        self._placing_text = False
        self._text_drag_start = None
        self._text_placement_pos = None
        self._text_drag_middle_button_down = False
        self._text_drag_pan_start = None

        # 3. Remove any lingering text preview / cursor actors
        for attr in (
            '_text_preview_actor',
            '_text_cursor_actor',
            '_text_placement_actor',
            '_text_drag_actor',
            '_text_anchor_marker',
            '_temp_text_actor',
        ):
            actor = getattr(self, attr, None)
            if actor is not None:
                if hasattr(self, 'text_overlay_renderer') and self.text_overlay_renderer:
                    try:
                        self.text_overlay_renderer.RemoveActor(actor)
                    except Exception:
                        pass
                try:
                    self.overlay_renderer.RemoveActor(actor)
                except Exception:
                    pass
                try:
                    self.renderer.RemoveActor(actor)
                except Exception:
                    pass
                try:
                    self.renderer.RemoveActor2D(actor)
                except Exception:
                    pass
                try:
                    self.renderer.RemoveViewProp(actor)
                except Exception:
                    pass
                setattr(self, attr, None)

        # ============================================================
        # ✅ FIX: Include ALL observers that _start_text_label() creates
        # The original list was missing middle button observers!
        # ============================================================
        for attr in (
            '_text_press_observer',           # ✅ left button press
            '_text_move_observer',            # ✅ mouse move
            '_text_release_observer',         # legacy (might not exist)
            '_text_observer_id',              # legacy (might not exist)
            '_text_click_observer',           # legacy (might not exist)
            '_text_left_press_observer',      # legacy (might not exist)
            '_text_middle_press_observer',    # ✅ FIX: was missing!
            '_text_middle_release_observer',  # ✅ FIX: was missing!
        ):
            oid = getattr(self, attr, None)
            if oid is not None:
                try:
                    self.interactor.RemoveObserver(oid)
                    print(f"  ✅ Removed text observer: {attr}")
                except Exception:
                    pass
                setattr(self, attr, None)

        # 5. Clear temp_points — text tool leaves ghost points
        self.temp_points = []
        self._clear_temp_vertex_history()

        print("✅ Text tool cleanup complete")

    def _text_z_offset(self):
        """
        Compute a world-space Z offset so that text actors appear visually
        above the point cloud surface (just like other annotation labels).
        The offset is proportional to the current camera scale so it stays
        consistent at any zoom level.
        """
        try:
            camera = self.renderer.GetActiveCamera()
            if camera.GetParallelProjection():
                scale = camera.GetParallelScale()
            else:
                focal = np.array(camera.GetFocalPoint())
                cam_pos = np.array(camera.GetPosition())
                scale = np.linalg.norm(cam_pos - focal) * 0.1
            # Use ~1 % of the visible scene height as the lift
            return max(scale * 0.01, 0.05)
        except Exception:
            return 0.5

    def _start_text_label(self):
        """Opens text input dialog, then enters placement mode with scalable text."""

        # Remove any previous text-specific observers (prevent stacking)
        for attr in (
            '_text_press_observer',
            '_text_move_observer',
            '_text_release_observer',
            '_text_click_observer',
            '_text_left_press_observer',
            '_text_middle_press_observer',
            '_text_middle_release_observer',
        ):
            oid = getattr(self, attr, None)
            if oid is not None:
                try:
                    self.interactor.RemoveObserver(oid)
                except Exception:
                    pass
                setattr(self, attr, None)

        dialog = TextEditDialog(
            current_text="New Text",
            current_size=75,
            current_font="Arial",
            current_bold=True,
            current_italic=False,
            current_color=(1, 0, 0),
            parent=self.app,
        )

        self._text_dialog = dialog
        self._text_ui_editing = True
        try:
            try:
                from PySide6.QtWidgets import QDialog
                result = dialog.exec()
            except ImportError:
                from PyQt5.QtWidgets import QDialog
                result = dialog.exec_()
        finally:
            self._text_ui_editing = False
            self._text_dialog = None

        if result == QDialog.Accepted:
            values = dialog.get_values()

            if not values['text'].strip():
                print("⚠️ Empty text - cancelled")
                self.active_tool = None
                return

            self._pending_text_config = values
            self._placing_text = True

            pos = self._get_mouse_world_no_snap()
            # Lift text above the point cloud surface
            pos = pos.copy()
            pos[2] += self._text_z_offset()

            # Create scalable preview actor
            self._temp_text_actor = self._make_scalable_text_actor(
                text=values['text'],
                position=pos,
                color=(1, 1, 0),
                font_size=values.get('font_size', 75),
                bold=values.get('bold', True),
                italic=values.get('italic', False),
                font_family=values.get('font_family', 'Arial'),
                justify='center',
            )
            self._temp_text_actor.GetTextProperty().SetOpacity(0.6)
            self._add_actor_to_overlay(self._temp_text_actor)
            self._temp_text_color = values['color']

            # ══════════════════════════════════════════════════════════════
            # CRITICAL: Remove draw-tool observers by stored ID only.
            # NEVER call RemoveObservers() globally — it destroys observers
            #
            # We remove the current _draw_observer_ids (left press, mouse
            # move, right press from set_tool) and replace them with
            # text-specific ones.  Middle button observers are left
            # completely untouched so pan always works.
            # ══════════════════════════════════════════════════════════════
            for oid in list(self._draw_observer_ids):
                try:
                    self.interactor.RemoveObserver(oid)
                except Exception:
                    pass
            self._draw_observer_ids = []

            # Text placement needs only these two:
            self._text_move_observer = self.interactor.AddObserver(
                "MouseMoveEvent", self._on_text_drag, 2.0
            )
            self._text_press_observer = self.interactor.AddObserver(
                "LeftButtonPressEvent", self._finalize_text_drag, 2.0
            )

            # Keep right-press so user can cancel via right-click
            oid = self.interactor.AddObserver(
                "RightButtonPressEvent", self._on_right_press, 1.0
            )
            self._draw_observer_ids.append(oid)

            self.app.vtk_widget.render()
            print(
                f"📝 Scalable text ready for placement: "
                f"'{values['text']}' — click to place"
            )
        else:
            print("❌ Text tool cancelled")
            self.active_tool = None
            if hasattr(self.app, 'set_cross_cursor_active'):
                self.app.set_cross_cursor_active(False, "draw")

    def _finalize_text_drag(self, obj, evt):
        """Finalize scalable text position."""
        if not getattr(self, "_placing_text", False):
            return

        # Do not let other active tools/styles consume the same click.
        self._consume_vtk_event(obj)

        self._save_state()

        pos = self._get_mouse_world_no_snap()
        # Lift text above the point cloud surface so it renders above other data
        pos = pos.copy()
        pos[2] += self._text_z_offset()
        config = getattr(self, '_pending_text_config', {})

        # Remove preview actor (only if creating new text)
        if getattr(self, '_temp_text_actor', None) and \
        not hasattr(self, "_dragging_text_drawing"):
            self._remove_actor_from_overlay(self._temp_text_actor)
            self._temp_text_actor = None

        # Handle repositioning existing text
        if hasattr(self, "_dragging_text_drawing") and self._dragging_text_drawing:
            drawing = self._dragging_text_drawing
            actor = drawing['actor']

            actor.SetPosition(pos[0], pos[1], pos[2])

            text_prop = actor.GetTextProperty()
            if 'original_text_color' in drawing:
                text_prop.SetColor(*drawing['original_text_color'])
            text_prop.SetOpacity(1.0)

            drawing['coords'] = [tuple(pos)]
            drawing['bounds'] = actor.GetBounds()

            print(f"✅ Text repositioned at "
                f"({pos[0]:.2f}, {pos[1]:.2f}, {pos[2]:.2f})")

            self._placing_text = False
            self._dragging_text_drawing = None
            self._temp_text_actor = None

        else:
            # Creating new text
            if getattr(self, '_temp_text_actor', None):
                self._remove_actor_from_overlay(self._temp_text_actor)
                self._temp_text_actor = None

            final_color = getattr(
                self, "_temp_text_color", config.get('color', (0, 0, 1))
            )

            text_actor = self._make_scalable_text_actor(
                text=config.get('text', 'Text'),
                position=pos,
                color=final_color,
                font_size=config.get('font_size', 75),
                bold=config.get('bold', True),
                italic=config.get('italic', False),
                font_family=config.get('font_family', 'Arial'),
                justify='center',
            )

            text_actor.GetTextProperty().SetOpacity(1.0)
            self._add_actor_to_overlay(text_actor)

            drawing_entry = {
                "type": "text",
                "coords": [tuple(pos)],
                "actor": text_actor,
                "text": config.get('text', 'Text'),
                "bounds": text_actor.GetBounds(),
                "original_text_color": final_color,
                "font_size": config.get('font_size', 75),
                "bold": config.get('bold', True),
                "italic": config.get('italic', False),
                "font_family": config.get('font_family', 'Arial'),
                "justify": 'center',
                "scalable": True,
            }
            self.drawings.append(drawing_entry)

            print(f"✅ Scalable text placed at "
                f"({pos[0]:.2f}, {pos[1]:.2f}, {pos[2]:.2f})")

        # ── Cleanup all state ──────────────────────────────────────────────
        self._placing_text = False
        self._pending_text_config = None
        self._text_drag_middle_button_down = False
        self._text_drag_pan_start = None

        if hasattr(self, "_temp_text_color"):
            delattr(self, "_temp_text_color")
        if hasattr(self, "_dragging_text_drawing"):
            self._dragging_text_drawing = None

        # ══════════════════════════════════════════════════════════════════
        # CRITICAL FIX: Remove ONLY the text-specific observers by their
        # stored IDs — never call RemoveObservers() globally.
        # Global RemoveObservers() wipes ALL observers for that event type
        # including ones installed by grid_label_manager and other systems,
        # leaving the interactor in a broken state for subsequent tools.
        # ══════════════════════════════════════════════════════════════════
        for attr in (
            '_text_press_observer',
            '_text_move_observer',
            '_text_middle_press_observer',
            '_text_middle_release_observer',
        ):
            oid = getattr(self, attr, None)
            if oid is not None:
                try:
                    self.interactor.RemoveObserver(oid)
                except Exception:
                    pass
                setattr(self, attr, None)

        # Reinstall only digitizer-owned primary observers.
        self._rebind_primary_draw_observers(include_left_release=True)

        # ── Reset tool state ───────────────────────────────────────────────
        self.temp_points = []
        self._clear_temp_vertex_history()
        self.active_tool = None

        if hasattr(self.app, 'set_cross_cursor_active'):
            self.app.set_cross_cursor_active(False, "draw")

        self._restore_shared_interactor_observers()
        self._force_render()

    # ============================================================
    # PATCH: Text Context Menu and Related Methods for Scalable Text
    # ============================================================

    def _show_text_context_menu(self, drawing):
        """Show context menu for visible Draw Tool text objects only."""

        if self._is_hidden_draw_tool_text_drawing(drawing):
            return

        try:
            from PySide6.QtCore import QTimer
            from PySide6.QtWidgets import QMenu
            from PySide6.QtGui import QCursor
        except ImportError:
            from PyQt5.QtCore import QTimer
            from PyQt5.QtWidgets import QMenu
            from PyQt5.QtGui import QCursor

        # If user right-clicks repeatedly, keep only the latest request.
        self._pending_text_menu_drawing = drawing

        timer = getattr(self, "_text_context_menu_timer", None)
        if timer is None:
            timer = QTimer(self.app)
            timer.setSingleShot(True)
            self._text_context_menu_timer = timer

        try:
            if timer.isActive():
                timer.stop()
        except Exception:
            pass

        try:
            old_menu = getattr(self, "_text_context_menu", None)
            if old_menu is not None:
                old_menu.close()
                old_menu.deleteLater()
        except Exception:
            pass
        self._text_context_menu = None

        def _deferred_menu():
            target = getattr(self, "_pending_text_menu_drawing", None)
            if not target:
                return
            if self._is_hidden_draw_tool_text_drawing(target):
                return

            menu = QMenu(self.app)

            self._text_context_menu = menu
            menu.setStyleSheet(f"""
                QMenu {{
                    background-color: {_TC.get('bg_secondary')};
                    color: {_TC.get('text_primary')};
                    border: 1px solid {_TC.get('border')};
                    padding: 5px;
                }}
                QMenu::item {{
                    padding: 8px 30px;
                    border-radius: 3px;
                }}
                QMenu::item:selected {{
                    background-color: {_TC.get('bg_button_hover')};
                    color: {_TC.get('text_primary')};
                }}
            """)

            edit_action = menu.addAction("✏️ Edit Text")
            move_action = menu.addAction("🔄 Move Text")
            menu.addSeparator()
            copy_action = menu.addAction("📋 Copy Text")
            delete_action = menu.addAction("🗑️ Delete Text")

            try:
                action = menu.exec(QCursor.pos())
            except AttributeError:
                action = menu.exec_(QCursor.pos())

            # Clear tracked menu ref after close
            if self._text_context_menu is menu:
                self._text_context_menu = None

            if action == edit_action:
                self._edit_text_label(target)
            elif action == move_action:
                self._enable_text_dragging(target)
            elif action == copy_action:
                self._copy_text_label(target)
            elif action == delete_action:
                self._delete_text_label(target)

        try:
            timer.timeout.disconnect()
        except Exception:
            pass
        timer.timeout.connect(_deferred_menu)
        timer.start(10)
        
    def _on_text_drag_middle_press(self, obj, evt):
        """Handle middle button press during text drag - enable pan."""
        self._text_drag_middle_button_down = True
        self._text_drag_pan_start = self.interactor.GetEventPosition()
        
        # Forward to interactor style for pan
        try:
            style = self.interactor.GetInteractorStyle()
            if style and hasattr(style, 'OnMiddleButtonDown'):
                style.OnMiddleButtonDown()
        except Exception:
            pass
        
        print("🖱️ Pan started during text drag")


    def _on_text_drag_middle_release(self, obj, evt):
        """Handle middle button release during text drag - stop pan."""
        self._text_drag_middle_button_down = False
        self._text_drag_pan_start = None
        
        # Forward to interactor style
        try:
            style = self.interactor.GetInteractorStyle()
            if style and hasattr(style, 'OnMiddleButtonUp'):
                style.OnMiddleButtonUp()
        except Exception:
            pass
        
        print("🛑 Pan stopped during text drag")


    def _on_text_drag_with_pan(self, obj, evt):
        """Update text position while dragging, with pan support."""
        if not getattr(self, "_placing_text", False):
            return
        
        # ✅ If middle button is held, handle pan instead of text drag
        if getattr(self, '_text_drag_middle_button_down', False):
            # Let the interactor style handle the pan
            try:
                style = self.interactor.GetInteractorStyle()
                if style and hasattr(style, 'OnMouseMove'):
                    style.OnMouseMove()
            except Exception:
                pass
            
            # Also manually update camera for smooth panning
            if hasattr(self, '_text_drag_pan_start') and self._text_drag_pan_start:
                current_pos = self.interactor.GetEventPosition()
                dx = current_pos[0] - self._text_drag_pan_start[0]
                dy = current_pos[1] - self._text_drag_pan_start[1]
                
                if abs(dx) > 0 or abs(dy) > 0:
                    camera = self.renderer.GetActiveCamera()
                    focal = camera.GetFocalPoint()
                    cam_pos = camera.GetPosition()
                    
                    self.renderer.SetWorldPoint(focal[0], focal[1], focal[2], 1.0)
                    self.renderer.WorldToDisplay()
                    display_focal = self.renderer.GetDisplayPoint()
                    
                    self.renderer.SetDisplayPoint(
                        display_focal[0] - dx, 
                        display_focal[1] - dy, 
                        display_focal[2]
                    )
                    self.renderer.DisplayToWorld()
                    world_focal = self.renderer.GetWorldPoint()
                    
                    delta_x = world_focal[0] / world_focal[3] - focal[0]
                    delta_y = world_focal[1] / world_focal[3] - focal[1]
                    delta_z = world_focal[2] / world_focal[3] - focal[2]
                    
                    camera.SetFocalPoint(
                        focal[0] + delta_x, 
                        focal[1] + delta_y, 
                        focal[2] + delta_z
                    )
                    camera.SetPosition(
                        cam_pos[0] + delta_x, 
                        cam_pos[1] + delta_y, 
                        cam_pos[2] + delta_z
                    )
                    
                    self._text_drag_pan_start = current_pos
            
            self.app.vtk_widget.render()
            return
        
        # Normal text dragging (when middle button is NOT held)
        pos = self._get_mouse_world_no_snap()
        # Keep text lifted above the point cloud surface
        pos = pos.copy()
        pos[2] += self._text_z_offset()
        
        if hasattr(self, "_temp_text_actor") and self._temp_text_actor:
            self._temp_text_actor.SetPosition(pos[0], pos[1], pos[2])
            self.app.vtk_widget.render()


    def _on_text_drag(self, obj, evt):
        """Update scalable text position while dragging."""
        if not getattr(self, "_placing_text", False):
            return

        # Keep text-drag exclusive while active.
        self._consume_vtk_event(obj)
        
        pos = self._get_mouse_world_no_snap()
        # Keep text lifted above the point cloud surface
        pos = pos.copy()
        pos[2] += self._text_z_offset()
        
        if hasattr(self, "_temp_text_actor") and self._temp_text_actor:
            self._temp_text_actor.SetPosition(pos[0], pos[1], pos[2])
            self.app.vtk_widget.render()

    def _delete_text_label(self, drawing):
        """Delete a text label from the scene."""
        try:
            self._save_state()
            
            actor = drawing.get('actor')
            if actor:
                if drawing.get('scalable', False):
                    self._remove_actor_from_overlay(actor)
                else:
                    try:
                        self.renderer.RemoveViewProp(actor)
                    except Exception:
                        pass
                    try:
                        self._remove_actor_from_overlay(actor)
                    except Exception:
                        pass
            
            # ✅ FIX: Use identity check to avoid numpy array comparison
            try:
                self.drawings[:] = [d for d in self.drawings if d is not drawing]
            except Exception as e:
                print(f"⚠️ Failed to remove drawing from list: {e}")
            
            # Clear selection if this was selected
            if getattr(self, 'selected_drawing', None) is drawing:
                self.selected_drawing = None
            
            self._force_render()
            print(f"🗑️ Text label deleted: '{drawing.get('text', 'N/A')}'")
            
        except Exception as e:
            print(f"❌ Failed to delete text: {e}")
            import traceback
            traceback.print_exc()

    def _copy_text_label(self, drawing):
        """Duplicate a scalable text label at the current mouse position."""
        try:
            self._save_state()
            old_actor = drawing["actor"]
            
            new_pos = self._get_mouse_world()
            
            # Get properties from original
            original_color = drawing.get("original_text_color", (1, 1, 1))
            font_size = drawing.get("font_size", 75)
            bold = drawing.get("bold", True)
            italic = drawing.get("italic", False)
            font_family = drawing.get("font_family", "Arial")
            text = drawing.get("text", old_actor.GetInput() if hasattr(old_actor, 'GetInput') else "Text")
            
            # Create new scalable text actor
            new_actor = self._make_scalable_text_actor(
                text=text,
                position=new_pos,
                color=original_color,
                font_size=font_size,
                bold=bold,
                italic=italic,
                font_family=font_family,
                justify='center',
            )
            self._add_actor_to_overlay(new_actor)
            
            entry = {
                "type": "text",
                "coords": [tuple(new_pos)],
                "actor": new_actor,
                "text": text,
                "bounds": new_actor.GetBounds(),
                "original_text_color": original_color,
                "font_size": font_size,
                "bold": bold,
                "italic": italic,
                "font_family": font_family,
                "scalable": True,
            }
            
            self.drawings.append(entry)
            self.app.vtk_widget.render()
            
            self.clear_coordinate_labels()
            self._unhighlight_all_lines()
            self.multi_selected = []
            self.selected_drawing = entry
            
            self.app.vtk_widget.setFocus()
            
            print(f"✅ Scalable text copied.")
            self._enable_text_dragging(entry)
        except Exception as e:
            print(f"❌ Failed to copy text: {e}")
            import traceback
            traceback.print_exc()

    def _edit_text_label(self, drawing):
        """Edit text label with full property support for scalable text."""
        try:
            self._save_state()
            actor = drawing["actor"]
            
            # Get current properties
            if hasattr(actor, 'GetTextProperty'):
                text_prop = actor.GetTextProperty()
                old_text = actor.GetInput()
                old_size = drawing.get('font_size', text_prop.GetFontSize())
                current_color = drawing.get('original_text_color', text_prop.GetColor())
                
                font_family = drawing.get('font_family', 'Arial')
                if not font_family:
                    font_enum = text_prop.GetFontFamily()
                    if font_enum == vtk.VTK_TIMES: font_family = "Times"
                    elif font_enum == vtk.VTK_COURIER: font_family = "Courier"
                    else: font_family = "Arial"
            else:
                old_text = drawing.get('text', 'Text')
                old_size = drawing.get('font_size', 75)
                current_color = drawing.get('original_text_color', (1, 1, 1))
                font_family = drawing.get('font_family', 'Arial')
                
            dialog = TextEditDialog(
                current_text=old_text,
                current_size=old_size,
                current_font=font_family,
                current_bold=drawing.get('bold', True),
                current_italic=drawing.get('italic', False),
                current_color=current_color,
                parent=self.app,
            )
            
            self._text_dialog = dialog
            self._text_ui_editing = True
            try:
                try:
                    from PySide6.QtWidgets import QDialog
                    result = dialog.exec()
                except ImportError:
                    from PyQt5.QtWidgets import QDialog
                    result = dialog.exec_()
            finally:
                self._text_ui_editing = False
                self._text_dialog = None
            
            if result == QDialog.Accepted:
                values = dialog.get_values()
                
                # Get current position
                pos = actor.GetPosition()
                
                # Remove old actor
                if drawing.get('scalable', False):
                    self._remove_actor_from_overlay(actor)
                else:
                    try: self.renderer.RemoveViewProp(actor)
                    except Exception:
                        pass
                    try: self._remove_actor_from_overlay(actor)
                    except Exception:
                        pass
                # Create new scalable text actor
                new_actor = self._make_scalable_text_actor(
                    text=values['text'],
                    position=pos,
                    color=values['color'],
                    font_size=values.get('font_size', 75),
                    bold=values.get('bold', True),
                    italic=values.get('italic', False),
                    font_family=values.get('font_family', 'Arial'),
                    justify='center',
                )
                self._add_actor_to_overlay(new_actor)
                
                # Update drawing entry
                drawing['actor'] = new_actor
                drawing['text'] = values['text']
                drawing['original_text_color'] = values['color']
                drawing['font_size'] = values.get('font_size', 75)
                drawing['bold'] = values.get('bold', True)
                drawing['italic'] = values.get('italic', False)
                drawing['font_family'] = values.get('font_family', 'Arial')
                drawing['scalable'] = True
                drawing['bounds'] = new_actor.GetBounds()
                
                self._force_render()
                print(f"✅ Text updated: '{values['text']}'")
            else:
                print("❌ Text edit cancelled")
                
        except Exception as e:
            print(f"⚠️ Text edit error: {e}")
            import traceback
            traceback.print_exc()
            
    def _edit_line_properties(self, drawing):
        """Open dialog to edit line color and thickness."""
        try:
            self._save_state()
            
            actor = drawing["actor"]
            prop = actor.GetProperty()
            
            old_color = drawing.get("original_color", prop.GetColor())
            old_width = drawing.get("original_width", prop.GetLineWidth())
            
            dialog = LineEditDialog(
                current_color=old_color,
                current_width=int(old_width)
            )
            
            try:
                from PySide6.QtWidgets import QDialog
                result = dialog.exec()
            except ImportError:
                from PyQt5.QtWidgets import QDialog
                result = dialog.exec_()
            
            if result == QDialog.Accepted:
                values = dialog.get_values()
                
                prop.SetColor(*values['color'])
                prop.SetLineWidth(values['width'])
                
                drawing['original_color'] = values['color']
                drawing['original_width'] = values['width']
                
                self.app.vtk_widget.render()
                print(f"🎨 Line updated: color=RGB{values['color']}, width={values['width']}px")
                
        except Exception as e:
            print(f"⚠️ Line edit failed: {e}")
            import traceback
            traceback.print_exc()        
                
   # ============================================================
    # PATCH FIX 3: Corrected _enable_text_dragging for vtkTextActor3D
    # ============================================================

    def _enable_text_dragging(self, text_drawing, suppress_color_change=False):
        """Enable dragging mode for a scalable text label."""
        try:
            self._placing_text = True
            self._temp_text_actor = text_drawing["actor"]
            self._dragging_text_drawing = text_drawing
            self._text_drag_middle_button_down = False
            self._text_drag_pan_start = None
            
            # Get text property (works for both vtkTextActor3D and vtkBillboardTextActor3D)
            text_prop = text_drawing["actor"].GetTextProperty()
            
            if 'original_text_color' not in text_drawing:
                text_drawing['original_text_color'] = text_prop.GetColor()
            
            # Set yellow color while dragging
            if not suppress_color_change:
                text_prop.SetColor(1, 1, 0)
            
            # ✅ FIX: Use GetTextProperty() for opacity
            text_prop.SetOpacity(0.6)
            
            # Remove only digitizer-owned observers (never global RemoveObservers)
            for oid in list(getattr(self, "_draw_observer_ids", []) or []):
                try:
                    self.interactor.RemoveObserver(oid)
                except Exception:
                    pass
            self._draw_observer_ids = []

            for attr in (
                '_text_press_observer',
                '_text_move_observer',
                '_text_middle_press_observer',
                '_text_middle_release_observer',
            ):
                oid = getattr(self, attr, None)
                if oid is not None:
                    try:
                        self.interactor.RemoveObserver(oid)
                    except Exception:
                        pass
                    setattr(self, attr, None)

            # Move Text must only move the text actor, never the camera/view.
            self._text_move_observer = self.interactor.AddObserver(
                "MouseMoveEvent", self._on_text_drag, 2.0
            )
            self._text_press_observer = self.interactor.AddObserver(
                "LeftButtonPressEvent", self._finalize_text_drag, 2.0
            )

            # Keep right-click selection/cancel path active while dragging text
            self._draw_observer_ids.append(
                self.interactor.AddObserver("RightButtonPressEvent", self._on_right_press, 1.0)
            )
            
            self.app.vtk_widget.render()
            print("✏️ Text drag enabled - move mouse to reposition, click to place")
            
        except Exception as e:
            print(f"⚠️ Failed to enable text dragging: {e}")
            import traceback
            traceback.print_exc()

    def _refresh_segment_markers(self):
        """Ensure correct coloring for all segment endpoints after deletion, and detect closed loops.

        Also performs a defensive sweep so that when ZERO line_segments remain,
        any leftover endpoint spheres from the in-progress _line_vertex_markers
        bookkeeping get removed from the overlay. Without this, batch-delete
        of all line segments leaves orphan green/red dots behind because shared
        marker references between adjacent segments can lose the actor pointer
        before the last segment is reached.
        """
        from collections import Counter, defaultdict

        segment_points = []
        adjacency = defaultdict(set)
        line_segments = [d for d in self.drawings if d.get("type") == "line_segment"]
        for d in line_segments:
            p1, p2 = tuple(d["coords"][0]), tuple(d["coords"][-1])
            segment_points.extend([p1, p2])
            adjacency[p1].add(p2)
            adjacency[p2].add(p1)

        vertex_counts = Counter(segment_points)

        is_closed_loop = all(len(neighbors) == 2 for neighbors in adjacency.values()) and len(adjacency) > 2

        for d in line_segments:
            start = tuple(d["coords"][0])
            end = tuple(d["coords"][-1])

            for key in ("start_marker", "end_marker"):
                if key in d and d[key]:
                    try:
                        self._remove_actor_from_overlay(d[key])
                    except Exception:
                        pass
                    d[key] = None

            if is_closed_loop:
                color = (1, 1, 0)
                d["start_marker"] = self._add_endpoint_sphere(start, color=color)
                d["end_marker"] = self._add_endpoint_sphere(end, color=color)
            else:
                start_color = (1, 1, 0) if vertex_counts[start] > 1 else (0, 1, 0)
                end_color = (1, 1, 0) if vertex_counts[end] > 1 else (1, 0, 0)
                d["start_marker"] = self._add_endpoint_sphere(start, color=start_color)
                d["end_marker"] = self._add_endpoint_sphere(end, color=end_color)

        # Defensive sweep: drain any in-progress sphere markers held on the
        # manager that no live drawing references. Covers the orphan case where
        # a Line tool batch-delete loses shared marker pointers between
        # adjacent segments before the last segment cleans up.
        leftover = list(getattr(self, "_line_vertex_markers", []) or [])
        if leftover and not line_segments:
            for marker in leftover:
                try:
                    self._remove_actor_from_overlay(marker)
                except Exception:
                    pass
            self._line_vertex_markers = []

        # Force overlay renderer to commit removals; the existing code only
        # called Modified() on the main renderer, which can leave overlay
        # actors visible until the next unrelated render.
        try:
            if hasattr(self, "overlay_renderer") and self.overlay_renderer:
                self.overlay_renderer.Modified()
        except Exception:
            pass
        self.renderer.Modified()
        try:
            self.interactor.GetRenderWindow().Render()
        except Exception:
            pass
        try:
            self.app.vtk_widget.render()
        except Exception:
            pass

        if line_segments:
            if is_closed_loop:
                print("🔁 Closed ring detected — all endpoints set to yellow")
            else:
                print("🎨 Endpoint markers refreshed (Green=start, Red=end, Yellow=shared)")

    def _display_to_world(self, x, y):
        """Convert display (x, y) coordinates to world coordinates using the renderer."""
        picker = vtk.vtkWorldPointPicker()
        picker.Pick(x, y, 0, self.renderer)
        world_pos = np.array(picker.GetPickPosition())

        if self.active_tool == "hatcharea" and self._pick_hit_background(x, y):
            world_pos = self._project_to_z_plane(x, y, self._hatch_flat_z())

        # ✅ FIX: Plan View Z-Consistency for initial point placement and freehand
        if not getattr(self.app, 'is_3d_mode', False):
            if self.temp_points:
                world_pos[2] = float(self.temp_points[0][2])
            elif getattr(self, 'dragging_vertex', None):
                drawing = self.dragging_vertex.get('drawing')
                if drawing and 'coords' in drawing and drawing['coords']:
                    world_pos[2] = float(drawing['coords'][0][2])
            else:
                world_pos[2] = self._get_fallback_z_height()

        return world_pos


    def add_drawing_from_data(self, drawing_data: dict):
        """Add a drawing from imported data (DXF, GeoJSON, Shapefile)."""
        try:
            shape_type = drawing_data.get('type')
            coords = drawing_data.get('coordinates') or drawing_data.get('coords', [])
            color = drawing_data.get('color', (255, 255, 255))
            
            if not coords:
                print(f"⚠️ No coordinates for {shape_type}")
                return False
            
            print(f"🔍 Importing {shape_type} with {len(coords)} points")
            
            if isinstance(color, (list, tuple)) and len(color) == 3:
                if all(c <= 1.0 for c in color):
                    color_vtk = color
                else:
                    color_vtk = tuple(c / 255.0 for c in color)
            else:
                color_vtk = (1.0, 0.0, 0.0)
            
            actor = None
            
            if shape_type == 'circle':
                if len(coords) == 1:
                    center = coords[0]
                    radius = drawing_data.get('radius', 5.0)
                    
                    cx, cy, cz = center[0], center[1], center[2] if len(center) > 2 else 0
                    
                    if radius < 10:
                        n = 64
                    elif radius < 50:
                        n = 128
                    else:
                        n = 180
                    
                    thetas = np.linspace(0, 2 * np.pi, n, endpoint=False)
                    circle_coords = [
                        (cx + radius * np.cos(t), cy + radius * np.sin(t), cz) 
                        for t in thetas
                    ]
                    circle_coords.append(circle_coords[0])
                    coords = circle_coords
                    
                elif len(coords) >= 3:
                    if coords[0] != coords[-1]:
                        coords = list(coords) + [coords[0]]
                else:
                    print(f"⚠️ Circle needs either 1 point (center) or 3+ points (outline)")
                    return False
                
                actor = self._make_polyline_actor(coords, color=color_vtk, width=3)
            
            elif shape_type == 'rectangle':
                if len(coords) == 5:
                    actor = self._make_polyline_actor(coords, color=color_vtk, width=3)
                elif len(coords) >= 2:
                    p1, p2 = coords[0], coords[1]
                    rect_coords = [
                        (p1[0], p1[1], p1[2] if len(p1) > 2 else 0),
                        (p2[0], p1[1], p1[2] if len(p1) > 2 else 0),
                        (p2[0], p2[1], p2[2] if len(p2) > 2 else 0),
                        (p1[0], p2[1], p2[2] if len(p2) > 2 else 0),
                        (p1[0], p1[1], p1[2] if len(p1) > 2 else 0)
                    ]
                    coords = rect_coords
                    actor = self._make_polyline_actor(rect_coords, color=color_vtk, width=3)
                else:
                    return False
            
            # ✅ Add 'smartline' alongside 'line' and 'line_segment'
            elif shape_type in ['line', 'line_segment', 'smartline', 'centerline']:
                if len(coords) >= 2:
                    actor = self._make_polyline_actor(coords, color=color_vtk, width=3)
                else:
                    return False

            # ✅ Add 'freehand' alongside 'polyline' and 'polygon'
            elif shape_type in ['polyline', 'polygon', 'freehand']:
                if len(coords) >= 3:
                    if coords[0] != coords[-1]:
                        coords = list(coords) + [coords[0]]
                    actor = self._make_polyline_actor(coords, color=color_vtk, width=3)
                else:
                    return False

            elif shape_type == 'text':
                if len(coords) >= 1:
                    pos = coords[0]
                    text_value = str(drawing_data.get('text', '') or 'Text')

                    actor = vtk.vtkTextActor()
                    actor.SetInput(text_value)

                    prop = actor.GetTextProperty()
                    prop.SetColor(*color_vtk)
                    prop.BoldOn()
                    prop.SetFontSize(18)
                    prop.SetJustificationToCentered()
                    prop.SetVerticalJustificationToCentered()

                    coord = actor.GetActualPositionCoordinate()
                    coord.SetCoordinateSystemToWorld()
                    coord.SetValue(pos[0], pos[1], pos[2] if len(pos) > 2 else 0)

                    actor.GetProperty().SetDisplayLocationToForeground()
                else:
                    return False
            
            else:
                if len(coords) >= 2:
                    actor = self._make_polyline_actor(coords, color=color_vtk, width=3)
                else:
                    return False
            
            if actor:
                if hasattr(actor, 'PickableOn'):
                    actor.PickableOn()
                if hasattr(actor, 'VisibilityOn'):
                    actor.VisibilityOn()

                if shape_type == 'text' and not bool(getattr(self, "draw_text_visible", True)):
                    try:
                        actor.SetVisibility(0)
                    except Exception:
                        actor.VisibilityOff()

                if shape_type == 'text':
                    self.renderer.AddActor2D(actor)
                else:
                    self._add_actor_to_overlay(actor)

                try:
                    bounds = actor.GetBounds()
                except Exception:
                    bounds = None
                
                drawing = {
                    'type': shape_type,
                    'coords': coords,
                    'coordinates': coords,
                    'actor': actor,
                    'bounds': bounds,
                    'original_color': color_vtk,
                    'original_width': 3,
                    'color': color,
                    'text': drawing_data.get('text', ''),
                    'radius': drawing_data.get('radius', 0)
                }

                # Preserve GIS source identity/schema metadata on the display/edit
                # drawing. The VTK actor is only a render representation; these
                # fields let higher-level GIS tools keep attributes and source
                # provenance instead of reducing imported GIS features to lines.
                for _meta_key in (
                    'source_attributes', 'source_fid', 'source_layer', 'source_path',
                    'source_driver', 'source_crs_wkt', 'source_geometry_type',
                    'native_wkb', 'source_has_z', 'source_has_m',
                ):
                    if _meta_key in drawing_data:
                        drawing[_meta_key] = drawing_data[_meta_key]

                if shape_type == 'text':
                    drawing['original_text_color'] = color_vtk
                    drawing['_draw_text_visible'] = bool(getattr(self, "draw_text_visible", True))
                
                self.drawings.append(drawing)
                print(f"✅ Added {shape_type} to scene\n")
                return True
            else:
                print(f"⚠️ Failed to create actor for {shape_type}\n")
                return False
                
        except Exception as e:
            print(f"❌ Failed to add drawing from data: {e}")
            import traceback
            traceback.print_exc()
            return False

    def select_drawing_at_position(self, x: float, y: float):
        """Select a drawing near the clicked position."""
        try:
            picker = vtk.vtkPropPicker()
            picker.Pick(x, y, 0, self.app.vtk_widget.renderer)
            
            picked_prop = picker.GetViewProp()
            
            if picked_prop:
                for drawing in self.drawings:
                    if drawing.get('actor') == picked_prop:
                        print(f"✅ Selected {drawing['type']}")
                        return drawing
            
            return None
            
        except Exception as e:
            print(f"⚠️ Selection failed: {e}")
            return None

    def delete_selected_drawing(self, drawing: dict):
        """Delete a drawing from the scene."""
        try:
            self._remove_drawing(drawing)
            self.app.vtk_widget.render()
            return True
        except Exception as e:
            print(f"❌ Delete failed: {e}")
            return False

    def get_drawing_info(self, drawing: dict) -> str:
        """Get human-readable info about a drawing."""
        shape_type = drawing.get('type', 'unknown')
        coords = drawing.get('coordinates', [])
        
        if shape_type == 'line':
            return f"Line: 2 points"
        elif shape_type in ['polyline', 'freehand']:
            return f"Polyline: {len(coords)} points"
        elif shape_type == 'polygon':
            return f"Polygon: {len(coords)} vertices"
        elif shape_type == 'circle':
            radius = drawing.get('radius', 0)
            return f"Circle: radius={radius:.2f}m"
        elif shape_type == 'rectangle':
            return f"Rectangle"
        elif shape_type == 'text':
            text = drawing.get('text', '')
            return f"Text: '{text}'"
        else:
            return f"Unknown: {shape_type}"
        
    def _deactivate_active_tool_keep_drawings(self):
        """
        ESC behavior: Exit/deactivate current tool.
        Cancel ONLY in-progress preview. KEEP all already drawn lines.
        """
        
        # ============================================================
        # ✅ FIX: Clean up text tool state when deactivating via ESC
        # This must happen BEFORE tool checks below, while active_tool
        # is still 'text', so _cleanup_text_tool() can find and remove
        # all text-specific observers.
        # ============================================================
        text_session_active = bool(getattr(self, "_placing_text", False))
        if getattr(self, 'active_tool', None) == 'text' or text_session_active:
            self._cleanup_text_tool()
        # ============================================================
        
        tool = getattr(self, "active_tool", None)
        if (not tool or tool == "none") and text_session_active:
            tool = "text"
        if not tool or tool == "none":
            return False

        if tool in ("smartline", "line"):
            self._cancel_smart_line()

        elif tool == "movevertex":
            self._deactivate_move_vertex_mode()
            self.interactor.RemoveObservers("RightButtonPressEvent")
            self.interactor.RemoveObservers("MiddleButtonPressEvent")
            self.interactor.RemoveObservers("MiddleButtonReleaseEvent")
            self.interactor.AddObserver("RightButtonPressEvent", self._on_right_press, 1.0)
            self.interactor.AddObserver("MiddleButtonPressEvent", self._on_middle_press, 20.0)
            self.interactor.AddObserver("MiddleButtonReleaseEvent", self._on_middle_release, 20.0)

        elif tool == "freehand":
            self._remove_preview_actor_2d('_freehand_preview_actor_2d')
            self._remove_preview_actor_2d('_preview_actor')
            self.is_drawing_freehand = False
            self.left_down = False
            self.temp_points = []

        elif tool == "circle":
            self._remove_preview_actor_2d('_circle_preview_actor_2d')
            self._remove_preview_actor_2d('_circle_preview_actor')
            self.temp_points = []

        elif tool == 'orthopolygon':
            inst = getattr(self, '_ortho_polygon_tool', None)
            if inst:
                inst._cancel()
                inst.deactivate()
                self._ortho_polygon_tool = None

        elif tool == "rectangle":
            self._remove_preview_actor_2d('_rectangle_preview_actor')
            self.temp_points = []

        elif tool == "polyline":
            self._remove_preview_actor_2d('_preview_line_actor')
            self._remove_preview_actor_2d('_continuous_line_actor')
            self.temp_points = []

        elif tool == "text":
            # ✅ Text cleanup already happened at top of method
            # This block just handles visual restoration of dragged text
            dragging_text = getattr(self, "_dragging_text_drawing", None)
            if dragging_text and dragging_text.get("actor") is not None:
                try:
                    text_prop = dragging_text["actor"].GetTextProperty()
                    if 'original_text_color' in dragging_text:
                        text_prop.SetColor(*dragging_text['original_text_color'])
                    text_prop.SetOpacity(1.0)
                except Exception:
                    pass

            if getattr(self, '_temp_text_actor', None) and not dragging_text:
                try:
                    self._remove_actor_from_overlay(self._temp_text_actor)
                except Exception:
                    pass
            self._temp_text_actor = None
            self._placing_text = False
            self._pending_text_config = None
            if hasattr(self, "_temp_text_color"):
                delattr(self, "_temp_text_color")
            if hasattr(self, "_dragging_text_drawing"):
                self._dragging_text_drawing = None

            # Restore only digitizer-owned primary observers by ID.
            # Never use broad RemoveObservers() here — that can remove
            # shared observers owned by other systems.
            self._rebind_primary_draw_observers(include_left_release=True)

        elif tool == "deletevertex":
            self._clear_vertex_delete_highlight()
            self.clear_coordinate_labels()
            if hasattr(self, 'vertex_hover_marker') and self.vertex_hover_marker:
                self._remove_actor_from_overlay(self.vertex_hover_marker)
                self.vertex_hover_marker = None
            self.selected_vertex_idx = None
            self.selected_drawing = None
            self._unhighlight_all_lines()

        elif tool == "hatcharea":
            self._remove_preview_actor_2d('_preview_line_actor')
            self._remove_preview_actor_2d('_continuous_line_actor')
            for m in getattr(self, '_hatch_vertex_markers', []):
                try: self._remove_actor_from_overlay(m)
                except Exception: pass
            self._hatch_vertex_markers = []
            self.temp_points = []
            if hasattr(self, '_hatch_dialog') and self._hatch_dialog:
                try: self._hatch_dialog.set_status(
                    "Click inside a shape to hatch it,\nor click empty space to define boundary points\n(right-click when ≥3 points to apply)."
                )
                except Exception: pass
            # Remove "hatcharea" from cursor active-tools set (the generic end only removes "draw")
            if hasattr(self.app, 'set_cross_cursor_active'):
                self.app.set_cross_cursor_active(False, "hatcharea")

        else:
            self._remove_preview_actor_2d('_preview_line_actor')
            self._remove_preview_actor_2d('_continuous_line_actor')
            self._remove_preview_actor_2d('_rectangle_preview_actor')
            self.temp_points = []

        for attr in ('_smartline_vertex_markers', '_line_vertex_markers', '_polyline_vertex_markers'):
            markers = getattr(self, attr, [])
            for marker in markers:
                try:
                    self._remove_actor_from_overlay(marker)
                except Exception:
                    pass
            setattr(self, attr, [])

        self.left_down = False
        self.middle_down = False
        self._clear_temp_vertex_history()
        self.active_tool = None
        if hasattr(self.app, 'set_cross_cursor_active'):
            self.app.set_cross_cursor_active(False, "draw")
        # ✅ If user deactivates before 3-sec timer, hide markers immediately
        self._hide_snap_marker_now()
        self._hide_drawing_markers_now()

        self._restore_shared_interactor_observers()
        self.app.vtk_widget.render()
        print(f"✅ {tool} exited — tool deactivated (drawings kept)")
        self._shift_blocked = True
        return True

        
    def _cancel_active_tool(self):
        """Cancel/deactivate the currently active digitize tool (if any)."""
        tool = getattr(self, "active_tool", None)
        if not tool or tool == "none":
            return False

        if tool in ("smartline", "line"):
            self._cancel_smart_line()

        elif tool == "freehand":
            self._remove_preview_actor_2d('_freehand_preview_actor_2d')
            self._remove_preview_actor_2d('_preview_actor')
                
            self.is_drawing_freehand = False
            self.left_down = False
            self.temp_points = []

        elif tool == "circle":
            self._remove_preview_actor_2d('_circle_preview_actor_2d')
            self._remove_preview_actor_2d('_circle_preview_actor')
            
            self.temp_points = []

        else:
            self._remove_preview_actor_2d('_preview_line_actor')
            self.temp_points = []

        self._clear_temp_vertex_history()
        self.active_tool = None
        try:
            self.app.statusBar().showMessage(f"{tool} cancelled")
        except Exception:
            pass
        self.app.vtk_widget.render()
        print(f"❌ {tool} cancelled — tool deactivated")
        return True     
        

    def _rebuild_active_preview(self):
        """Rebuild screen-space preview after undo — immediate visual update, no blink."""

        if len(self.temp_points) < 2:
            # Hide actors instead of removing them
            for attr in ('_continuous_line_actor', '_preview_line_actor'):
                actor = getattr(self, attr, None)
                if actor is not None:
                    actor.SetVisibility(0)
            self.app.vtk_widget.render()
            return

        if self.active_tool == "smartline":
            s = self._get_draw_style('smartline')
            color = s['color']
        elif self.active_tool == "line":
            s = self._get_draw_style('line')
            color = s['color']
        elif self.active_tool == "polyline":
            s = self._get_draw_style('polyline')
            color = s['color']
        elif self.active_tool == "hatcharea":
            s = self._get_draw_style('hatcharea')
            color = s['color']
        else:
            return

        self._update_continuous_preview(color=color, width=s['width'], line_style=s['style'])

        if self.active_tool in ("polyline", "hatcharea"):
            self._update_cursor_preview(color=color, width=s['width'], line_style='dotted', close_loop=True)
        else:
            self._update_cursor_preview(color=color, width=s['width'], line_style=s['style'])

        self.app.vtk_widget.render()
            
        
    def _get_drawing_under_cursor(self, mouse_x, mouse_y, tolerance=25.0):
        """
        Mathematic Picker with Debugging.
        Projects every line segment to the screen and checks distance.
        """
        best_drawing = None
        min_dist = tolerance

        for d_idx, d in enumerate(self._iter_pickable_drawings()):
            if self._is_hidden_draw_tool_text_drawing(d):
                continue

            coords = self._get_drawing_coords(d)
            dtype = d.get('type', 'unknown')
            
            if len(coords) == 0:
                continue
                
            # ✅ Handle 1-point objects like TEXT
            if dtype == 'text' and len(coords) == 1:
                actor = d.get('actor')
                hit = False

                # Try bounding box test first (works when zoomed in and anchor is off-screen)
                if actor:
                    try:
                        bounds = actor.GetBounds()  # (xmin,xmax,ymin,ymax,zmin,zmax)
                        corners = [
                            (bounds[0], bounds[2], bounds[4]),
                            (bounds[1], bounds[2], bounds[4]),
                            (bounds[0], bounds[3], bounds[4]),
                            (bounds[1], bounds[3], bounds[4]),
                        ]
                        sx_vals, sy_vals = [], []
                        for c in corners:
                            self.renderer.SetWorldPoint(c[0], c[1], c[2], 1.0)
                            self.renderer.WorldToDisplay()
                            dp = self.renderer.GetDisplayPoint()
                            sx_vals.append(dp[0])
                            sy_vals.append(dp[1])
                        bx_min, bx_max = min(sx_vals), max(sx_vals)
                        by_min, by_max = min(sy_vals), max(sy_vals)
                        # Expand by tolerance
                        bx_min -= tolerance; bx_max += tolerance
                        by_min -= tolerance; by_max += tolerance
                        if bx_min <= mouse_x <= bx_max and by_min <= mouse_y <= by_max:
                            dist = 0.0  # inside box = direct hit
                            hit = True
                    except Exception:
                        pass

                # Fallback: anchor point distance (original logic)
                if not hit:
                    p_world = coords[0]
                    self.renderer.SetWorldPoint(p_world[0], p_world[1], p_world[2], 1.0)
                    self.renderer.WorldToDisplay()
                    display_p = self.renderer.GetDisplayPoint()
                    screen_p = np.array(display_p[:2])
                    mouse_p = np.array([mouse_x, mouse_y])
                    dist = np.linalg.norm(mouse_p - screen_p)
                    hit = dist < (tolerance * 5)

                if hit and dist < min_dist:
                    min_dist = dist
                    best_drawing = d
                continue

            if len(coords) < 2: continue
            
            for i in range(len(coords) - 1):
                p1_world = coords[i]
                p2_world = coords[i+1]

                self.renderer.SetWorldPoint(p1_world[0], p1_world[1], p1_world[2], 1.0)
                self.renderer.WorldToDisplay()
                display_p1 = self.renderer.GetDisplayPoint()
                s1 = np.array(display_p1[:2]) 

                self.renderer.SetWorldPoint(p2_world[0], p2_world[1], p2_world[2], 1.0)
                self.renderer.WorldToDisplay()
                display_p2 = self.renderer.GetDisplayPoint()
                s2 = np.array(display_p2[:2])

                dist = self._distance_point_to_segment_2d(np.array([mouse_x, mouse_y]), s1, s2)

                if dist < min_dist:
                    min_dist = dist
                    best_drawing = d
        
        if best_drawing:
            pass  # selection found — caller handles feedback
        
        return best_drawing

    def _distance_point_to_segment_2d(self, p, a, b):
        """Calculates closest distance from point P to segment A-B in 2D."""
        ab = b - a
        len_sq = np.dot(ab, ab)
        
        if len_sq == 0:
            return np.linalg.norm(p - a)

        t = np.dot(p - a, ab) / len_sq
        t = max(0, min(1, t))

        projection = a + t * ab
        return np.linalg.norm(p - projection)

    def _add_arrow_to_line(self, coords):
        """Add 2D screen-space arrows to a line."""
        if len(coords) < 2:
            return None
        
        arrow_actors = []
        
        try:
            renderer = self.renderer
            
            for i in range(len(coords) - 1):
                p1_world = np.array(coords[i])
                p2_world = np.array(coords[i+1])
                midpoint = (p1_world + p2_world) / 2.0
                
                renderer.SetWorldPoint(p1_world[0], p1_world[1], p1_world[2], 1.0)
                renderer.WorldToDisplay()
                d1 = np.array(renderer.GetDisplayPoint()[:2])
                
                renderer.SetWorldPoint(p2_world[0], p2_world[1], p2_world[2], 1.0)
                renderer.WorldToDisplay()
                d2 = np.array(renderer.GetDisplayPoint()[:2])
                
                dx, dy = d2[0] - d1[0], d2[1] - d1[1]
                
                if np.hypot(dx, dy) < 20.0: 
                    continue 
                    
                angle_deg = np.degrees(np.arctan2(dy, dx))
                
                arrow_src = vtk.vtkGlyphSource2D()
                arrow_src.SetGlyphTypeToArrow()
                arrow_src.SetScale(15.0)
                arrow_src.SetFilled(True)
                arrow_src.Update()
                
                transform = vtk.vtkTransform()
                transform.RotateZ(angle_deg)
                transform.Translate(-7.5, 0, 0)
                
                tf = vtk.vtkTransformPolyDataFilter()
                tf.SetInputConnection(arrow_src.GetOutputPort())
                tf.SetTransform(transform)
                tf.Update()
                
                mapper = vtk.vtkPolyDataMapper2D()
                mapper.SetInputConnection(tf.GetOutputPort())
                
                actor = vtk.vtkActor2D()
                actor.SetMapper(mapper)
                
                actor.GetPositionCoordinate().SetCoordinateSystemToWorld()
                actor.GetPositionCoordinate().SetValue(midpoint[0], midpoint[1], midpoint[2])
                
                actor.GetProperty().SetColor(1, 0, 0)
                
                renderer.AddActor2D(actor)
                arrow_actors.append(actor)
                
            print(f"  ➡️ Added {len(arrow_actors)} fixed-size 2D direction arrows")
            return arrow_actors
            
        except Exception as e:
            print(f"⚠️ Failed to create 2D arrows: {e}")
            return None
        
    def _remove_move_vertex_observers(self):
        """Remove only Move Vertex temporary observers to avoid duplicate callbacks."""
        for oid in list(getattr(self, "_move_vertex_observer_ids", []) or []):
            try:
                self.interactor.RemoveObserver(oid)
            except Exception:
                pass
        self._move_vertex_observer_ids = []

    def _cancel_move_vertex_pending_for_pan(self):
        """
        Middle mouse pan must NOT finalize, cancel, restore, or clear Move Vertex.

        Earlier version restored original_pos and cleared moving_vertex_data.
        That made the vertex jump/go back when middle button was used.

        Correct behavior:
        - Keep selected vertex active.
        - Keep current preview position.
        - Only freeze Move Vertex reactions during pan.
        - User can continue moving after pan.
        - Right-click is still the only finish action.
        """
        data = getattr(self, "moving_vertex_data", None)
        if not isinstance(data, dict):
            return False

        try:
            drawing = data.get("drawing")
            vertex_idx = data.get("vertex_index")

            if isinstance(drawing, dict):
                coords = drawing.get("coords") or []
                if vertex_idx is not None and 0 <= int(vertex_idx) < len(coords):
                    # Store current live preview position only.
                    # Do NOT restore original_pos.
                    data["pan_freeze_pos"] = tuple(coords[int(vertex_idx)])
        except Exception as e:
            print(f"⚠️ Move Vertex pan freeze warning: {e}")

        print("ℹ️ Move Vertex frozen for middle-button pan")
        return True

    def _move_vertex_pan_guard_active(self):
        """
        True only while the physical middle mouse button is actually held.

        This is stronger than checking stale flags only.
        VTK can fire repeated MiddleButtonPressEvent without a matching release,
        leaving middle_down / _is_panning stuck True. Qt mouseButtons() tells
        the real current button state, so Move Vertex resumes immediately after
        the middle button is physically released.
        """
        qt_checked = False
        physical_middle_down = None

        try:
            try:
                from PySide6.QtWidgets import QApplication
                from PySide6.QtCore import Qt
            except Exception:
                from PyQt5.QtWidgets import QApplication
                from PyQt5.QtCore import Qt

            buttons = QApplication.mouseButtons()

            try:
                middle_button = Qt.MouseButton.MiddleButton
                left_button = Qt.MouseButton.LeftButton
            except Exception:
                middle_button = Qt.MiddleButton
                left_button = Qt.LeftButton

            panning_button = getattr(self.app, "panning_button", "scroll") if self.app else "scroll"
            physical_middle_down = bool(buttons & middle_button)
            qt_checked = True
        except Exception:
            qt_checked = False

        if qt_checked:
            if physical_middle_down:
                return True

            # Physical middle button is released.
            # Clear any stale VTK pan flags immediately so Move Vertex resumes.
            self._middle_button_down = False
            self.middle_down = False
            self._is_panning = False
            self._move_vertex_pan_block_until = 0.0
            self._ignore_move_vertex_finish_until = 0.0
            return False

        # Fallback only if Qt check is unavailable.
        try:
            return bool(
                getattr(self, "_middle_button_down", False)
                or getattr(self, "middle_down", False)
                or getattr(self, "_is_panning", False)
            )
        except Exception:
            return False

    def _activate_move_vertex_mode(self):
        """Activate vertex move mode.
        ✅ FIX: Preserves middle-button pan by NOT removing MouseMoveEvent
        and by tracking middle button state.
        """
        self._remove_move_vertex_observers()

        self.vertex_moving = True
        self.moving_vertex_data = None
        self._middle_button_down = False
        self._move_vertex_pan_block_until = 0.0

        # Clear any vertex display markers left from Vertex Display button.
        self.clear_coordinate_labels()

        mode = getattr(self, 'vertex_move_mode_type', 'click')
        
        if mode == 'drag':
            print("🖱️ Drag mode: Click and hold to move vertices")
        else:
            print("🖱️ Click mode: Left-click vertex, move mouse, left-click again to finish")
        
        # ✅ Only remove LEFT button observers — never touch middle button or mouse move
        self.interactor.RemoveObservers("LeftButtonPressEvent")
        self.interactor.RemoveObservers("LeftButtonReleaseEvent")
        
        # Add Move Vertex observers and store IDs so repeated activation
        # does not stack duplicate callbacks.
        self._move_vertex_observer_ids = []
        self._move_vertex_observer_ids.append(
            self.interactor.AddObserver("LeftButtonPressEvent", self._on_move_vertex_press, 1.0)
        )
        self._move_vertex_observer_ids.append(
            self.interactor.AddObserver("LeftButtonReleaseEvent", self._on_move_vertex_release, 1.0)
        )

        # Mouse move must pause during middle-button pan.
        self._move_vertex_observer_ids.append(
            self.interactor.AddObserver("MouseMoveEvent", self._on_move_vertex_hover, 1.0)
        )

        # Track middle button only for Move Vertex pan guard.
        self._move_vertex_observer_ids.append(
            self.interactor.AddObserver("MiddleButtonPressEvent", self._on_move_middle_press, 10.0)
        )
        self._move_vertex_observer_ids.append(
            self.interactor.AddObserver("MiddleButtonReleaseEvent", self._on_move_middle_release, 10.0)
        )
        
        try:
            constraint_label = getattr(
                self,
                "vertex_move_constraint_label",
                "Free 360 Move",
            )
            self.app.statusBar().showMessage(
                f"🔄 Move Vertex Mode [{constraint_label}] - Left click vertex, move mouse, left-click again to finish (ESC to exit)"
            )
        except Exception:
            pass

    def _on_move_middle_press(self, obj, evt):
        """Middle button is pan only. Pause Move Vertex only while physically held."""
        self._middle_button_down = True

        # Do not force middle_down / _is_panning here.
        # Main _on_middle_press handles actual pan state.
        # This avoids stale flags if VTK emits duplicate middle press events.
        self._move_vertex_pan_block_until = 0.0
        self._ignore_move_vertex_finish_until = 0.0

        print("ℹ️ Move Vertex paused for middle-button pan")

        # Do NOT consume event. Let VTK/app pan handle it.

    def _on_move_middle_release(self, obj, evt):
        """Middle button released. Resume Move Vertex immediately."""
        self._middle_button_down = False
        self.middle_down = False
        self._is_panning = False
        self._move_vertex_pan_block_until = 0.0
        self._ignore_move_vertex_finish_until = 0.0

        print("ℹ️ Move Vertex resumed after middle-button pan")

        # Do NOT consume event. Let VTK/app pan handle it.


    def _on_move_vertex_hover(self, obj, evt):
        if self.active_tool != "movevertex" or not getattr(self, "vertex_moving", False):
            self.moving_vertex_data = None
            return

        """Show cyan highlight when hovering over vertices.
        ✅ FIX: When middle button is held, forward to style for pan."""
        
        # If middle button pan is active/recent, Move Vertex must not hover or drag.
        if self._move_vertex_pan_guard_active():
            return
        
        if self.moving_vertex_data:
            self._on_move_vertex_drag(obj, evt)
            return
        
        x, y = self.interactor.GetEventPosition()
        nearest_vertex, nearest_drawing, vertex_idx = self._find_nearest_vertex_at_position(
            x, y, tolerance=15.0)
        
        if nearest_vertex is not None:
            # Always remove and recreate — SetPosition does not move the
            # geometry since position is baked into vtkPoints, not the actor.
            if hasattr(self, 'vertex_hover_marker') and self.vertex_hover_marker:
                self._remove_actor_from_overlay(self.vertex_hover_marker)
            self.vertex_hover_marker = self._add_endpoint_sphere(
                nearest_vertex, color=(0, 1, 1), radius=0.12
            )
            self.app.vtk_widget.render()
        else:
            if hasattr(self, 'vertex_hover_marker') and self.vertex_hover_marker:
                self._remove_actor_from_overlay(self.vertex_hover_marker)
                self.vertex_hover_marker = None
                self.app.vtk_widget.render()



    def _move_vertex_supports_edge_constraint(self, drawing):
        """
        Previous/Next edge constraint should work only for real edge-based drawings.

        Supported:
            line, line_segment, smartline, polyline, polygon, rectangle, orthopolygon

        Not supported:
            circle, freehand, curve, text
        """
        if not isinstance(drawing, dict):
            return False

        drawing_type = str(drawing.get("type", "")).lower().strip()
        drawing_source = str(drawing.get("source", "")).lower().strip()

        unsupported_types = {
            "circle",
            "freehand",
            "free",
            "curve",
            "text",
        }

        if drawing_type in unsupported_types:
            return False

        if drawing_source == "curve_tool":
            return False

        return True


    def _cancel_active_vertex_move_runtime(self):
        """
        Hard-cancel any in-progress Move Vertex operation.

        This prevents stale/orphan actors when user presses Ctrl+Z, Clear,
        Redo, or switches tools while a vertex move is still active.
        """
        def _remove_any_actor(actor):
            if actor is None:
                return

            if isinstance(actor, (list, tuple, set)):
                for item in actor:
                    _remove_any_actor(item)
                return

            try:
                self._remove_actor_from_overlay(actor)
            except Exception:
                pass

            for renderer in (
                getattr(self, "overlay_renderer", None),
                getattr(self, "text_overlay_renderer", None),
                getattr(self, "renderer", None),
            ):
                if renderer is None:
                    continue

                try:
                    renderer.RemoveActor(actor)
                except Exception:
                    pass
                try:
                    renderer.RemoveActor2D(actor)
                except Exception:
                    pass
                try:
                    renderer.RemoveViewProp(actor)
                except Exception:
                    pass

        try:
            data = getattr(self, "moving_vertex_data", None)

            if isinstance(data, dict):
                drawing = data.get("drawing")
                if isinstance(drawing, dict):
                    for key in (
                        "actor",
                        "fill_actor",
                        "arrow_actor",
                        "start_marker",
                        "end_marker",
                        "marker",
                        "label_actor",
                        "text_actor",
                    ):
                        _remove_any_actor(drawing.get(key))

                    for key in (
                        "actors",
                        "markers",
                        "vertex_markers",
                        "highlight_actors",
                    ):
                        _remove_any_actor(drawing.get(key))

            _remove_any_actor(getattr(self, "vertex_drag_marker", None))
            self.vertex_drag_marker = None

            _remove_any_actor(getattr(self, "vertex_hover_marker", None))
            self.vertex_hover_marker = None

        except Exception as e:
            print(f"⚠️ Move Vertex runtime cleanup warning: {e}")

        self.moving_vertex_data = None
        self.vertex_moving = False
        self.dragging_vertex = None
        self.left_down = False

    def _point_to_np3(self, point):
        """Convert any point-like value to xyz numpy array."""
        arr = np.asarray(point, dtype=np.float64).reshape(-1)

        out = np.zeros(3, dtype=np.float64)
        if arr.size >= 1:
            out[0] = arr[0]
        if arr.size >= 2:
            out[1] = arr[1]
        if arr.size >= 3:
            out[2] = arr[2]
        return out


    def _move_vertex_is_closed_coords(self, coords, tolerance=1e-9):
        """Return True if first and last vertices represent the same closed point."""
        if coords is None or len(coords) < 3:
            return False

        try:
            first = self._point_to_np3(coords[0])
            last = self._point_to_np3(coords[-1])
            return float(np.linalg.norm(first[:2] - last[:2])) <= float(tolerance)
        except Exception:
            return False


    def _move_vertex_neighbor_index(self, coords, vertex_idx, preferred_mode):
        """
        Get the neighbor vertex that defines the movement direction.

        previous = preserve previous connected edge direction
        next     = preserve next connected edge direction
        """
        if coords is None or len(coords) == 0:
            return None

        n = len(coords)
        if n < 2 or vertex_idx < 0 or vertex_idx >= n:
            return None

        closed = self._move_vertex_is_closed_coords(coords)

        prev_idx = None
        next_idx = None

        if vertex_idx > 0:
            prev_idx = vertex_idx - 1
        elif closed and n >= 3:
            prev_idx = n - 2

        if vertex_idx < n - 1:
            next_idx = vertex_idx + 1
        elif closed and n >= 3:
            next_idx = 1

        if closed and vertex_idx == n - 1 and n >= 3:
            next_idx = 1

        if closed and next_idx == n - 1 and vertex_idx != n - 1:
            next_idx = 0

        if preferred_mode == "previous":
            candidates = [prev_idx, next_idx]
        else:
            candidates = [next_idx, prev_idx]

        for idx in candidates:
            if idx is None:
                continue
            if idx < 0 or idx >= n:
                continue
            if idx == vertex_idx:
                continue
            return idx

        return None


    def _constrain_move_vertex_position(self, raw_pos, drawing, vertex_idx):
        """
        Apply Move Vertex constraint.

        free     -> no change, existing 360 movement
        previous -> project clicked/mouse point onto previous edge direction
        next     -> project clicked/mouse point onto next edge direction
        """
        mode = getattr(self, "vertex_move_constraint_mode", "free")
        if mode not in ("previous", "next"):
            return tuple(raw_pos)

        if not isinstance(drawing, dict):
            return tuple(raw_pos)

        # Circle / Freehand / Curve / Text must always remain Free 360.
        # Previous/Next edge mode is meaningful only for real edge-based tools.
        if not self._move_vertex_supports_edge_constraint(drawing):
            return tuple(raw_pos)

        coords = drawing.get("coords")
        coord_len = 0 if coords is None else len(coords)
        if coords is None or coord_len == 0 or vertex_idx < 0 or vertex_idx >= coord_len:
            return tuple(raw_pos)

        neighbor_idx = self._move_vertex_neighbor_index(coords, vertex_idx, mode)
        if neighbor_idx is None:
            return tuple(raw_pos)

        try:
            raw = self._point_to_np3(raw_pos)

            original_pos = None
            if getattr(self, "moving_vertex_data", None):
                original_pos = self.moving_vertex_data.get("original_pos")

            base = self._point_to_np3(
                original_pos if original_pos is not None else coords[vertex_idx]
            )
            neighbor = self._point_to_np3(coords[neighbor_idx])

            direction = base[:2] - neighbor[:2]
            length_sq = float(np.dot(direction, direction))

            if length_sq <= 1e-18:
                return tuple(raw_pos)

            # Project mouse/click point onto the selected edge direction.
            t = float(np.dot(raw[:2] - base[:2], direction) / length_sq)
            projected_xy = base[:2] + (t * direction)

            return (
                float(projected_xy[0]),
                float(projected_xy[1]),
                float(base[2]),
            )

        except Exception as e:
            print(f"⚠️ Move Vertex constraint failed, using free move: {e}")
            return tuple(raw_pos)

    def _on_move_vertex_press(self, obj, evt):
        if self.active_tool != "movevertex" or not getattr(self, "vertex_moving", False):
            self.moving_vertex_data = None
            return

        """Handle vertex selection for moving"""

        mode = getattr(self, 'vertex_move_mode_type', 'click')

        # Ignore left-click events generated during or just after middle-button pan.
        if self._move_vertex_pan_guard_active():
            print("ℹ️ Move Vertex left-click ignored during middle-button pan")
            return

        # In click mode:
        # First left click selects vertex.
        # Mouse move previews/live-moves it.
        # Second left click finishes/places it.
        # Keep Move Vertex active so user can move another vertex.
        # Exit only by ESC or by activating another tool.
        if mode == 'click' and self.moving_vertex_data:
            self._consume_vtk_event(obj)

            if self._move_vertex_pan_guard_active():
                return

            new_pos = self._get_mouse_world()
            self._finalize_vertex_move(new_pos)

            # Do NOT deactivate Move Vertex here.
            # _finalize_vertex_move() already clears only the current moving vertex.
            self.active_tool = "movevertex"
            self.vertex_moving = True

            try:
                constraint_label = getattr(
                    self,
                    "vertex_move_constraint_label",
                    "Free 360 Move",
                )
                self.app.statusBar().showMessage(
                    f"🔄 Move Vertex Mode [{constraint_label}] - Left click another vertex, move mouse, left-click again to finish (ESC to exit)"
                )
            except Exception:
                pass

            return
        x, y = self.interactor.GetEventPosition()
        nearest_vertex, nearest_drawing, vertex_idx = self._find_nearest_vertex_at_position(
            x,
            y,
            tolerance=15.0,
        )

        if nearest_vertex is None or nearest_drawing is None:
            print("⚪ No vertex found near click")
            return

        self._save_state()

        self.moving_vertex_data = {
            'drawing': nearest_drawing,
            'vertex_index': vertex_idx,
            'original_pos': tuple(nearest_vertex),
            'constraint_mode': getattr(self, "vertex_move_constraint_mode", "free"),
        }

        if hasattr(self, 'vertex_drag_marker') and self.vertex_drag_marker:
            self._remove_actor_from_overlay(self.vertex_drag_marker)

        self.vertex_drag_marker = self._add_endpoint_sphere(
            nearest_vertex, color=(1, 1, 0), radius=0.14
        )

        if hasattr(self, 'vertex_hover_marker') and self.vertex_hover_marker:
            self._remove_actor_from_overlay(self.vertex_hover_marker)
            self.vertex_hover_marker = None

        constraint_label = getattr(self, "vertex_move_constraint_label", "Free 360 Move")

        if (
            getattr(self, "vertex_move_constraint_mode", "free") in ("previous", "next")
            and not self._move_vertex_supports_edge_constraint(nearest_drawing)
        ):
            constraint_label = "Free 360 Move"
            print(
                f"ℹ️ {nearest_drawing.get('type')} does not support Previous/Next edge constraint. "
                "Using Free 360 Move."
            )

        print(f"🔵 Moving vertex {vertex_idx} of {nearest_drawing['type']} [{constraint_label}]")
        self.app.vtk_widget.render()

    def _on_move_vertex_drag(self, obj, evt):
        if self.active_tool != "movevertex" or not getattr(self, "vertex_moving", False):
            self.moving_vertex_data = None
            return

        """Update vertex position while moving"""
        if not self.moving_vertex_data:
            return

        # Critical fix:
        # Middle-button pan also fires MouseMoveEvent. Do not let that move the vertex.
        if self._move_vertex_pan_guard_active():
            return

        raw_pos = self._get_mouse_world()

        drawing = self.moving_vertex_data['drawing']
        vertex_idx = self.moving_vertex_data['vertex_index']

        # If undo/clear already removed or replaced this drawing,
        # do not compare drawing dictionaries with == because coords may contain numpy arrays.
        # Use identity check only.
        active_drawings = list(getattr(self, "drawings", []) or [])
        if not any(d is drawing for d in active_drawings):
            print("⚠️ Move Vertex ignored: drawing is no longer active")
            self._cancel_active_vertex_move_runtime()
            return

        coords = drawing.get('coords')
        coord_len = 0 if coords is None else len(coords)
        if coords is None or coord_len == 0 or vertex_idx < 0 or vertex_idx >= coord_len:
            print(f"⚠️ Vertex index {vertex_idx} out of range (coords len={coord_len}) — aborting drag")
            self.moving_vertex_data = None
            if hasattr(self, 'vertex_drag_marker') and self.vertex_drag_marker:
                self._remove_actor_from_overlay(self.vertex_drag_marker)
                self.vertex_drag_marker = None
            return

        new_pos = self._constrain_move_vertex_position(raw_pos, drawing, vertex_idx)
        coords[vertex_idx] = tuple(new_pos)

        # Remove and recreate — SetPosition does not move vtkPoints geometry.
        if hasattr(self, 'vertex_drag_marker') and self.vertex_drag_marker:
            self._remove_actor_from_overlay(self.vertex_drag_marker)
        self.vertex_drag_marker = self._add_endpoint_sphere(
            new_pos, color=(1, 1, 0), radius=0.14
        )

        self._rebuild_drawing_actor(drawing)
        self.app.vtk_widget.render()

    def _on_move_vertex_release(self, obj, evt):
        if self.active_tool != "movevertex" or not getattr(self, "vertex_moving", False):
            self.moving_vertex_data = None
            return

        """Handle mouse release for drag mode"""
        if self._move_vertex_pan_guard_active():
            print("ℹ️ Move Vertex release ignored during middle-button pan")
            return

        mode = getattr(self, 'vertex_move_mode_type', 'click')
        
        if mode == 'drag' and self.moving_vertex_data:
            new_pos = self._get_mouse_world()
            self._finalize_vertex_move(new_pos)


    def _finalize_vertex_move(self, new_pos):
        """Complete the vertex move operation"""
        if not self.moving_vertex_data:
            return

        drawing = self.moving_vertex_data['drawing']
        vertex_idx = self.moving_vertex_data['vertex_index']

        # If undo/clear already removed or replaced this drawing,
        # do not finalize against a stale drawing reference.
        active_drawings = list(getattr(self, "drawings", []) or [])
        if not any(d is drawing for d in active_drawings):
            print("⚠️ Move Vertex finalize ignored: drawing is no longer active")
            self._cancel_active_vertex_move_runtime()
            return

        coords = drawing.get('coords')
        coord_len = 0 if coords is None else len(coords)
        if coords is None or coord_len == 0 or vertex_idx < 0 or vertex_idx >= coord_len:
            print(f"⚠️ Vertex index {vertex_idx} out of range (coords len={coord_len}) — aborting move")
            self.moving_vertex_data = None
            if hasattr(self, 'vertex_drag_marker') and self.vertex_drag_marker:
                self._remove_actor_from_overlay(self.vertex_drag_marker)
                self.vertex_drag_marker = None
            return

        old_pos = tuple(
            self.moving_vertex_data.get(
                'original_pos',
                coords[vertex_idx],
            )
        )

        new_pos = self._constrain_move_vertex_position(new_pos, drawing, vertex_idx)
        coords[vertex_idx] = tuple(new_pos)

        constraint_label = getattr(self, "vertex_move_constraint_label", "Free 360 Move")
        print(f"✅ Vertex {vertex_idx} moved to {new_pos} [{constraint_label}]")

        if hasattr(self, 'vertex_drag_marker') and self.vertex_drag_marker:
            self._remove_actor_from_overlay(self.vertex_drag_marker)
            self.vertex_drag_marker = None

        self._rebuild_drawing_actor(drawing)
        self._update_shared_vertices(old_pos, new_pos)
        self.moving_vertex_data = None
        self.app.vtk_widget.render()

    def _update_shared_vertices(self, old_pos, new_pos, tolerance=0.01):
        """Update vertices in OTHER drawings that share the same position as the moved vertex."""
        import numpy as np
        old_arr = np.array(old_pos[:3] if len(old_pos) >= 3 else old_pos, dtype=np.float64)
        new_tuple = tuple(new_pos[:3] if len(new_pos) >= 3 else new_pos)

        updated = 0
        for d in self.drawings:
            if 'coords' not in d:
                continue
            changed = False
            for i, v in enumerate(d['coords']):
                v_arr = np.array(v[:3] if len(v) >= 3 else v, dtype=np.float64)
                if np.linalg.norm(v_arr - old_arr) < tolerance:
                    d['coords'][i] = new_tuple
                    changed = True
            if changed:
                self._rebuild_drawing_actor(d)
                updated += 1
        if updated > 0:
            print(f"🔗 Propagated vertex move to {updated} shared drawing(s)")


    def _deactivate_move_vertex_mode(self):
        """Exit move vertex mode.
        ✅ FIX: Clean up middle button observers too."""
        self.vertex_moving = False
        self.moving_vertex_data = None
        self._middle_button_down = False
        
        if hasattr(self, 'vertex_hover_marker') and self.vertex_hover_marker:
            self._remove_actor_from_overlay(self.vertex_hover_marker)
            self.vertex_hover_marker = None
        
        if hasattr(self, 'vertex_drag_marker') and self.vertex_drag_marker:
            self._remove_actor_from_overlay(self.vertex_drag_marker)
            self.vertex_drag_marker = None
        
        # ✅ Remove only our observers
        self.interactor.RemoveObservers("LeftButtonPressEvent")
        self.interactor.RemoveObservers("LeftButtonReleaseEvent")
        self.interactor.RemoveObservers("MouseMoveEvent")
        self.interactor.RemoveObservers("MiddleButtonPressEvent")
        self.interactor.RemoveObservers("MiddleButtonReleaseEvent")
        
        # Restore normal drawing observers
        self.interactor.AddObserver("LeftButtonPressEvent", self._on_left_press, 1.0)
        self.interactor.AddObserver("MouseMoveEvent", self._on_mouse_move, 1.0)
        self.interactor.AddObserver("LeftButtonReleaseEvent", self._on_left_release, 1.0)
        self.interactor.AddObserver("MiddleButtonPressEvent", self._on_middle_press, 20.0)
        self.interactor.AddObserver("MiddleButtonReleaseEvent", self._on_middle_release, 20.0)
        self._restore_shared_interactor_observers()
        
        print("⚪ Move Vertex mode deactivated")
        self.app.vtk_widget.render()
        
class LineEditDialog(QDialog):
    """Custom dialog for editing line properties (color and thickness)."""
    
    def __init__(self, current_color=(1, 0, 0), current_width=2, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Line Properties")
        self.setModal(True)
        self.resize(350, 200)
        self.setProperty("themeStyledDialog", True)
        self.setStyleSheet(get_dialog_stylesheet())
        
        layout = QVBoxLayout()
        
        props_group = QGroupBox("Line Properties")
        props_layout = QVBoxLayout()
        
        width_layout = QHBoxLayout()
        width_layout.addWidget(QLabel("Thickness:"))
        self.width_spin = QSpinBox()
        self.width_spin.setRange(1, 20)
        self.width_spin.setValue(current_width)
        self.width_spin.setSuffix(" px")
        width_layout.addWidget(self.width_spin)
        width_layout.addStretch()
        props_layout.addLayout(width_layout)
        
        color_layout = QHBoxLayout()
        color_layout.addWidget(QLabel("Color:"))
        
        self.color_button = QPushButton()
        self.color_button.setFixedSize(80, 30)
        self.current_color = (
            int(current_color[0] * 255),
            int(current_color[1] * 255),
            int(current_color[2] * 255)
        )
        self._update_color_button()
        self.color_button.clicked.connect(self._pick_color)
        color_layout.addWidget(self.color_button)
        color_layout.addStretch()
        props_layout.addLayout(color_layout)
        
        props_group.setLayout(props_layout)
        layout.addWidget(props_group)
        
        button_layout = QHBoxLayout()
        
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("cancel_btn")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addStretch()
        button_layout.addWidget(ok_btn)
        button_layout.addWidget(cancel_btn)
        layout.addLayout(button_layout)
        
        self.setLayout(layout)
    
    def _update_color_button(self):
        r, g, b = self.current_color
        self.color_button.setStyleSheet(f"""
            QPushButton {{
                background-color: rgb({r}, {g}, {b});
                border: 2px solid {_TC.get('border_light')};
                border-radius: 3px;
            }}
        """)
    
    def _pick_color(self):
        try:
            from PySide6.QtWidgets import QColorDialog
            from PySide6.QtGui import QColor
        except ImportError:
            from PyQt5.QtWidgets import QColorDialog
            from PyQt5.QtGui import QColor
        
        initial_color = QColor(*self.current_color)
        color = QColorDialog.getColor(initial_color, self, "Choose Line Color")
        
        if color.isValid():
            self.current_color = (color.red(), color.green(), color.blue())
            self._update_color_button()
    
    def get_values(self):
        vtk_color = (
            self.current_color[0] / 255.0,
            self.current_color[1] / 255.0,
            self.current_color[2] / 255.0
        )
        
        return {
            'color': vtk_color,
            'width': self.width_spin.value()
        }
        
    def enable_pan_while_drawing(self):
        try:
            from vtkmodules.vtkInteractionStyle import vtkInteractorStyleUser
            current_style = self.interactor.GetInteractorStyle()
            if hasattr(current_style, 'SetMiddleButtonPressEvent'):
                print("✅ Pan while drawing enabled (middle mouse button)")
            else:
                print("⚠️ Current interactor doesn't support pan while drawing")
        except Exception as e:
            print(f"⚠️ Failed to enable pan while drawing: {e}")      
        

class _ClipboardSafeLineEdit(QLineEdit):
    """QLineEdit that always handles Ctrl+C/V/X/A itself.

    Some app-wide QApplication event filters (global tool shortcuts, AccuDraw,
    etc.) can intercept Ctrl+C/V/X/A before Qt's normal focus-widget shortcut
    handling runs. This widget explicitly performs the clipboard operation on
    every Ctrl+C/V/X/A keypress it receives and consumes the event, so text
    editing works the same as any OS text field regardless of what else is
    installed on the application.
    """

    def keyPressEvent(self, event):
        if event.modifiers() & Qt.ControlModifier and not (
            event.modifiers() & (Qt.AltModifier | Qt.MetaModifier)
        ):
            key = event.key()
            if key == Qt.Key_C:
                self.copy()
                event.accept()
                return
            if key == Qt.Key_X:
                self.cut()
                event.accept()
                return
            if key == Qt.Key_V:
                self.paste()
                event.accept()
                return
            if key == Qt.Key_A:
                self.selectAll()
                event.accept()
                return
        super().keyPressEvent(event)


class TextEditDialog(QDialog):
    """Custom dialog for editing text labels with stable font-family selection."""

    def __init__(self, current_text="", current_size=75, current_font="Arial",
                 current_bold=False, current_italic=False, current_color=(1, 1, 0), parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Text Label")
        self.setModal(True)
        self.resize(460, 340)
        self.setProperty("themeStyledDialog", True)
        self.setStyleSheet(get_dialog_stylesheet())

        self._selected_font_family = (current_font or "Arial").strip() or "Arial"

        layout = QVBoxLayout()

        text_group = QGroupBox("Text Content")
        text_layout = QVBoxLayout()
        self.text_input = _ClipboardSafeLineEdit(current_text)
        self.text_input.setPlaceholderText("Enter text here...")
        # Always let this field own its keystrokes (select-all, copy, paste,
        # undo, etc.) even if the same key combo is mapped to a runtime tool
        # shortcut elsewhere in the app — see the nakshaStrictTextInput
        # opt-out check in gui/global_shortcuts.py's GlobalShortcutFilter.
        self.text_input.setProperty("nakshaStrictTextInput", True)
        text_layout.addWidget(self.text_input)
        text_group.setLayout(text_layout)
        layout.addWidget(text_group)

        font_group = QGroupBox("Font Settings")
        font_layout = QVBoxLayout()

        font_family_layout = QHBoxLayout()
        font_family_layout.addWidget(QLabel("Font:"))

        # Microsoft Word-style font dropdown.
        # Use QComboBox instead of QFontComboBox because QFontComboBox.currentFont()
        # can be disturbed when we change the widget preview font for Bold/Italic.
        self.font_combo = QComboBox()
        self.font_combo.setEditable(True)
        try:
            self.font_combo.lineEdit().setReadOnly(True)
            self.font_combo.lineEdit().setCursorPosition(0)
        except Exception:
            pass
        self._populate_font_combo(self._selected_font_family)
        self.font_combo.currentIndexChanged.connect(self._on_font_index_changed)
        self.font_combo.setMinimumWidth(260)
        try:
            self.font_combo.view().setMinimumWidth(340)
        except Exception:
            pass

        font_family_layout.addWidget(self.font_combo)
        font_layout.addLayout(font_family_layout)

        font_size_layout = QHBoxLayout()
        font_size_layout.addWidget(QLabel("Size:"))
        self.size_spin = QSpinBox()
        self.size_spin.setRange(0, 999)
        self.size_spin.setValue(int(75 if current_size is None else current_size))
        self.size_spin.setSuffix(" pt")
        font_size_layout.addWidget(self.size_spin)
        font_size_layout.addStretch()
        font_layout.addLayout(font_size_layout)

        style_layout = QHBoxLayout()
        self.bold_check = QCheckBox("Bold")
        self.bold_check.setChecked(current_bold)
        self.italic_check = QCheckBox("Italic")
        self.italic_check.setChecked(current_italic)

        self.bold_check.toggled.connect(self._apply_selected_font_preview)
        self.italic_check.toggled.connect(self._apply_selected_font_preview)
        self.size_spin.valueChanged.connect(self._apply_selected_font_preview)

        style_layout.addWidget(self.bold_check)
        style_layout.addWidget(self.italic_check)
        style_layout.addStretch()
        font_layout.addLayout(style_layout)

        color_layout = QHBoxLayout()
        color_layout.addWidget(QLabel("Color:"))

        self.color_button = QPushButton()
        self.color_button.setFixedSize(60, 30)
        self.current_color = (
            int(current_color[0] * 255),
            int(current_color[1] * 255),
            int(current_color[2] * 255)
        )
        self._update_color_button()
        self.color_button.clicked.connect(self._pick_color)
        color_layout.addWidget(self.color_button)
        color_layout.addStretch()
        font_layout.addLayout(color_layout)

        font_group.setLayout(font_layout)
        layout.addWidget(font_group)

        button_layout = QHBoxLayout()
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("cancel_btn")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addStretch()
        button_layout.addWidget(ok_btn)
        button_layout.addWidget(cancel_btn)
        layout.addLayout(button_layout)

        self.setLayout(layout)
        self._apply_selected_font_preview()
        self.text_input.selectAll()

    def _font_classes(self):
        try:
            from PySide6.QtGui import QFont, QFontDatabase
        except ImportError:
            from PyQt5.QtGui import QFont, QFontDatabase
        return QFont, QFontDatabase

    def _populate_font_combo(self, current_font):
        """
        Every dropdown row is displayed using its own font.
        The selected family is stored separately in Qt.UserRole.
        """
        QFont, QFontDatabase = self._font_classes()

        try:
            families = list(QFontDatabase.families())
        except Exception:
            families = []

        fallback_fonts = [
            "Arial", "Arial Black", "Arial Narrow",
            "Times New Roman", "Georgia", "Palatino Linotype",
            "Courier New", "Courier", "Lucida Console",
            "Verdana", "Tahoma", "Trebuchet MS",
            "Impact", "Comic Sans MS",
            "Calibri", "Cambria", "Segoe UI",
            "Helvetica", "Century Gothic", "Franklin Gothic Medium",
        ]

        for font_name in fallback_fonts:
            if font_name not in families:
                families.append(font_name)

        families = sorted(set(families), key=lambda x: x.lower())

        self.font_combo.blockSignals(True)
        self.font_combo.clear()

        selected_index = -1
        current_norm = (current_font or "Arial").strip().lower()

        for family in families:
            self.font_combo.addItem(family)
            idx = self.font_combo.count() - 1
            self.font_combo.setItemData(idx, family, Qt.UserRole)

            try:
                self.font_combo.setItemData(idx, QFont(family, 11), Qt.FontRole)
            except Exception:
                pass

            if family.strip().lower() == current_norm:
                selected_index = idx

        if selected_index < 0:
            selected_index = self.font_combo.findText("Arial", Qt.MatchFixedString)

        if selected_index >= 0:
            self.font_combo.setCurrentIndex(selected_index)
            family = self.font_combo.itemData(selected_index, Qt.UserRole)
            if family:
                self._selected_font_family = family

        self.font_combo.blockSignals(False)

    def _on_font_index_changed(self, index):
        family = self.font_combo.itemData(index, Qt.UserRole)
        if not family:
            family = self.font_combo.currentText()
        if family:
            self._selected_font_family = str(family).strip()
        self._apply_selected_font_preview()

    def _current_font_family(self):
        family = getattr(self, "_selected_font_family", None)
        if family:
            return str(family).strip()

        try:
            family = self.font_combo.itemData(self.font_combo.currentIndex(), Qt.UserRole)
            if family:
                self._selected_font_family = str(family).strip()
                return self._selected_font_family
        except Exception:
            pass

        try:
            self._selected_font_family = self.font_combo.currentText() or "Arial"
        except Exception:
            self._selected_font_family = "Arial"

        return self._selected_font_family

    def _apply_selected_font_preview(self):
        """Update preview without changing selected family."""
        QFont, _ = self._font_classes()

        family = self._current_font_family()
        bold = self.bold_check.isChecked()
        italic = self.italic_check.isChecked()
        size = int(self.size_spin.value() or 75)

        try:
            combo_font = QFont(family, 11)
            combo_font.setBold(bold)
            combo_font.setItalic(italic)
            old_block_state = self.font_combo.blockSignals(True)
            self.font_combo.setFont(combo_font)
            try:
                safe_family = str(family).replace("\\", "\\\\").replace('"', '\\"')
                font_weight = "700" if bold else "400"
                font_style = "italic" if italic else "normal"
                self.font_combo.setStyleSheet(
                    f'QComboBox {{ font-family: "{safe_family}"; font-size: 11pt; '
                    f'font-weight: {font_weight}; font-style: {font_style}; }}'
                )
            except Exception:
                pass
            try:
                line_edit = self.font_combo.lineEdit()
                if line_edit is not None:
                    line_edit.setFont(combo_font)
                    line_edit.setText(family)
                    line_edit.setCursorPosition(0)
            except Exception:
                pass
            self.font_combo.blockSignals(old_block_state)
        except Exception:
            try:
                self.font_combo.blockSignals(False)
            except Exception:
                pass

    def _update_color_button(self):
        r, g, b = self.current_color
        self.color_button.setStyleSheet(f"""
            QPushButton {{
                background-color: rgb({r}, {g}, {b});
                border: 2px solid {_TC.get('border_light')};
                border-radius: 3px;
            }}
        """)

    def _pick_color(self):
        try:
            from PySide6.QtWidgets import QColorDialog
            from PySide6.QtGui import QColor
        except ImportError:
            from PyQt5.QtWidgets import QColorDialog
            from PyQt5.QtGui import QColor

        initial_color = QColor(*self.current_color)
        color = QColorDialog.getColor(initial_color, self, "Choose Text Color")

        if color.isValid():
            self.current_color = (color.red(), color.green(), color.blue())
            self._update_color_button()

    def get_values(self):
        vtk_color = (
            self.current_color[0] / 255.0,
            self.current_color[1] / 255.0,
            self.current_color[2] / 255.0
        )

        family = self._current_font_family()
        bold = self.bold_check.isChecked()
        italic = self.italic_check.isChecked()

        print(
            f"🧾 Text dialog OK: family='{family}', "
            f"bold={bold}, italic={italic}, size={self.size_spin.value()}"
        )

        return {
            'text': self.text_input.text(),
            'font_family': family,
            'font_size': self.size_spin.value(),
            'bold': bold,
            'italic': italic,
            'color': vtk_color
        }


class PolylineSettingsDialog(QDialog):
    """Settings dialog for Polyline drawing mode"""
    
    def __init__(self, current_permanent=True, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Polyline Settings")
        self.setModal(True)
        self.resize(300, 150)
        
        layout = QVBoxLayout()
        
        mode_group = QGroupBox("Drawing Mode")
        mode_layout = QVBoxLayout()
        
        self.temp_radio = QRadioButton("Temporary (finishes on right-click)")
        self.permanent_radio = QRadioButton("Permanent (stays active)")
        
        if current_permanent or True:   # always default to permanent
            self.permanent_radio.setChecked(True)
        else:
            self.temp_radio.setChecked(True)
        
        mode_layout.addWidget(self.temp_radio)
        mode_layout.addWidget(self.permanent_radio)
        mode_group.setLayout(mode_layout)
        layout.addWidget(mode_group)
        
        button_layout = QHBoxLayout()
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addStretch()
        button_layout.addWidget(ok_btn)
        button_layout.addWidget(cancel_btn)
        layout.addLayout(button_layout)
        
        self.setLayout(layout)
    
    def get_permanent_mode(self):
        return self.permanent_radio.isChecked()
    
    
class LineArrowSettingsDialog(QDialog):
    """Settings dialog for Line/SmartLine arrow direction"""
    
    def __init__(self, tool_name="Line", current_arrow=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{tool_name} Arrow Settings")
        self.setModal(True)
        self.resize(350, 180)
        self.setProperty("themeStyledDialog", True)
        self.setStyleSheet(get_dialog_stylesheet())
        
        layout = QVBoxLayout()
        
        arrow_group = QGroupBox("Direction Arrow")
        arrow_layout = QVBoxLayout()
        
        self.no_arrow_radio = QRadioButton("No arrow (simple line)")
        self.with_arrow_radio = QRadioButton("With arrow (shows drawing direction)")
        
        if current_arrow:
            self.with_arrow_radio.setChecked(True)
        else:
            self.no_arrow_radio.setChecked(True)
        
        arrow_layout.addWidget(self.no_arrow_radio)
        arrow_layout.addWidget(self.with_arrow_radio)
        arrow_group.setLayout(arrow_layout)
        layout.addWidget(arrow_group)
        
        button_layout = QHBoxLayout()
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("cancel_btn")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addStretch()
        button_layout.addWidget(ok_btn)
        button_layout.addWidget(cancel_btn)
        layout.addLayout(button_layout)
        
        self.setLayout(layout)
    
    def get_arrow_mode(self):
        return self.with_arrow_radio.isChecked()

    ##newww
    def _make_preview_actor_2d(self, points, color=(0, 1, 0), width=3):
        """
        Pixel-perfect 2D preview line.
        Converts world→display coords explicitly to avoid vtkMapper2D projection bugs.
        """
        if not points or len(points) < 2:
            return None

        # ✅ Convert world coords to display pixels directly — no Z projection issues
        coord = vtk.vtkCoordinate()
        coord.SetCoordinateSystemToWorld()

        display_pts = vtk.vtkPoints()
        for p in points:
            coord.SetValue(float(p[0]), float(p[1]), float(p[2]))
            d = coord.GetComputedDisplayValue(self.renderer)
            display_pts.InsertNextPoint(float(d[0]), float(d[1]), 0.0)

        n = display_pts.GetNumberOfPoints()
        cell = vtk.vtkCellArray()
        cell.InsertNextCell(n)
        for i in range(n):
            cell.InsertCellPoint(i)

        poly = vtk.vtkPolyData()
        poly.SetPoints(display_pts)
        poly.SetLines(cell)

        # Use Display system — coordinates are already in pixels
        coord2 = vtk.vtkCoordinate()
        coord2.SetCoordinateSystemToDisplay()

        mapper = vtk.vtkPolyDataMapper2D()
        mapper.SetInputData(poly)
        mapper.SetTransformCoordinate(coord2)

        actor = vtk.vtkActor2D()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(float(color[0]), float(color[1]), float(color[2]))
        prop.SetLineWidth(float(width))
        prop.SetOpacity(1.0)
        prop.SetDisplayLocationToForeground()

        return actor

    def _remove_preview_actor_2d(self, attr_name):
        """Safely remove a named 2D preview actor from renderer."""
        actor = getattr(self, attr_name, None)
        if actor is not None:
            try:
                self.renderer.RemoveActor2D(actor)
            except Exception:
                pass
            setattr(self, attr_name, None)
