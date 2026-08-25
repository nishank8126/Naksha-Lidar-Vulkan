"""
Temporary Fence tool — draw a throwaway polygon fence on the MAIN viewer and
instantly run any By Class operation inside it.

Flow (matches agreed spec):
  1. Classify ribbon → Points → "Fence" activates this tool.
  2. Left-click adds vertices, rubber-band preview follows the mouse
     (same feel as the digitizer Polygon tool).
  3. Right-click / Enter / double-click finalizes (min 3 vertices).
     Esc cancels an in-progress draw, or clears a finalized fence + popup.
  4. On finalize a popup lists the 7 By Class tools (everything EXCEPT
     Convert): Close, Height, Fence, Low Points, Isolated, Ground, Surface.
  5. Picking a tool opens that By Class dialog with the temp fence already
     injected as the selected fence — no fence/selection prompt.
  6. Lifetime rules:
       - Drawing a new fence tears down the previous popup + fence first.
       - When the chosen By Class conversion completes, the fence disappears.
       - ✕ on the popup keeps the fence; Esc clears fence + popup.
       - Switching to any other tool only stands down the draw; a finalized
         fence + popup stay available until replaced / used / Esc.

Safety:
  - The fence NEVER enters digitizer.drawings — it is temporary, never saved,
    never exported, invisible to Clear-Fence lists.
  - No existing tool's logic is modified; every new path is exception-wrapped
    so a failure can never crash or block other workflows.
"""

import numpy as np

import vtk

from PySide6.QtCore import Qt, QTimer, QPoint
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

_builtin_print = print

# Premium theming: base app dialog stylesheet + popup-specific button styling.
try:
    from gui.theme_manager import get_dialog_stylesheet as _theme_dialog_qss
except Exception:
    def _theme_dialog_qss():
        return ""

# The 7 By Class tools offered in the popup (Convert deliberately excluded).
BY_CLASS_TOOLS = [
    # (key, label, icon, ribbon_method_name, ribbon_dialog_attr)
    ("close",      "Close",      "📍", "open_closed_convert_dialog",  "closed_by_class_dialog"),
    ("height",     "Height",     "📏", "open_height_convert_dialog",  "height_convert_dialog"),
    ("fence",      "Fence",      "🔷", "open_inside_fence_dialog",    "inside_fence_dialog"),
    ("low_points", "Low Points", "⬇️", "open_low_points_dialog",      "low_points_dialog"),
    ("isolated",   "Isolated",   "🔴", "open_isolated_dialog",        "isolated_dialog"),
    ("ground",     "Ground",     "🏔️", "open_ground_dialog",          "ground_dialog"),
    ("surface",    "Surface",    "📐", "open_below_surface_dialog",   "below_surface_dialog"),
]

_POPUP_QSS = """
QDialog { background-color: palette(base); border: 1px solid #3c3c3c; border-radius: 10px; }
QLabel#tfTitle { color: palette(text); font-size: 11px; font-weight: bold; letter-spacing: 1px; padding: 4px; background: transparent; }
QPushButton#tfToolBtn {
    background-color: palette(button); color: palette(button-text); border: 1px solid palette(mid);
    border-radius: 6px; padding: 8px 12px; font-size: 11px; font-weight: 500; text-align: left;
}
QPushButton#tfToolBtn:hover { background-color: palette(highlight); border-color: palette(highlight); color: palette(highlighted-text); }
QPushButton#tfToolBtn:pressed { background-color: palette(dark); }
QToolButton#tfCloseBtn { background: transparent; color: palette(mid); border: none; font-size: 13px; border-radius: 4px; padding: 2px 6px; }
QToolButton#tfCloseBtn:hover { color: palette(text); background-color: palette(mid); }
"""


def _safe_status(app, message, timeout=3000):
    """Best-effort status-bar message; never raises."""
    try:
        sb = getattr(app, "statusBar", None)
        if callable(sb):
            sb().showMessage(message, timeout)
    except Exception:
        pass


class TempFencePopup(QDialog):
    """Small frameless popup listing the 7 By Class tools (no Convert)."""

    def __init__(self, tool, parent=None):
        super().__init__(parent)
        self._tool = tool
        self._tool_picked = False
        self.setWindowTitle("By Class — Temp Fence")
        # ✅ Qt.Tool + parent → hides when the app minimizes and restores with
        # it (same lifecycle as every other app popup). No WindowStaysOnTopHint
        # so it never floats above OTHER applications.
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint)
        try:
            self.setStyleSheet(_theme_dialog_qss() + _POPUP_QSS)
        except Exception:
            self.setStyleSheet(_POPUP_QSS)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        # Premium soft shadow around the frameless popup.
        try:
            from PySide6.QtWidgets import QGraphicsDropShadowEffect
            _shadow = QGraphicsDropShadowEffect(self)
            _shadow.setBlurRadius(28)
            _shadow.setOffset(0, 4)
            _shadow.setColor(QColor(0, 0, 0, 140))
            self.setGraphicsEffect(_shadow)
        except Exception:
            pass
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 10)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("BY CLASS — pick a tool for this fence")
        title.setObjectName("tfTitle")
        header.addWidget(title)
        header.addStretch()
        close_btn = QToolButton()
        close_btn.setObjectName("tfCloseBtn")
        close_btn.setText("✕")
        close_btn.setToolTip("Dismiss popup and remove the fence")
        close_btn.clicked.connect(self._dismiss)
        header.addWidget(close_btn)
        layout.addLayout(header)

        grid = QGridLayout()
        grid.setSpacing(6)
        for idx, (key, label, icon, _method, _attr) in enumerate(BY_CLASS_TOOLS):
            btn = QPushButton(f"{icon}  {label}")
            btn.setObjectName("tfToolBtn")
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, k=key: self._pick(k))
            grid.addWidget(btn, idx // 2, idx % 2)
        layout.addLayout(grid)

    def _dismiss(self):
        """✕ button: close popup AND remove the fence."""
        self._tool_picked = True
        if self._tool is not None:
            try:
                self._tool.remove_fence()
            except Exception:
                pass
        try:
            self.close()
        except Exception:
            pass

    def closeEvent(self, event):
        """When popup closes without a tool being picked, remove the fence too."""
        if not self._tool_picked and self._tool is not None:
            try:
                self._tool.remove_fence()
            except Exception:
                pass
        super().closeEvent(event)

    def _pick(self, key):
        self._tool_picked = True
        try:
            self.close()
        except Exception:
            pass
        try:
            if self._tool is not None:
                self._tool.run_by_class_tool(key)
        except Exception as e:
            _builtin_print(f"⚠️ TempFencePopup: tool dispatch failed ({key}): {e}")

    def open_near(self, world_xyz, renderer, vtk_widget):
        """Position near the fence centroid (clamped to the screen) and show."""
        try:
            self.show()  # realize first so size() is meaningful
            pos = None
            if world_xyz is not None and renderer is not None:
                try:
                    ren = renderer
                    ren.SetWorldPoint(float(world_xyz[0]), float(world_xyz[1]),
                                      float(world_xyz[2]), 1.0)
                    ren.WorldToDisplay()
                    dp = ren.GetDisplayPoint()
                    pos = QPoint(int(dp[0]), int(dp[1]))
                    if vtk_widget is not None:
                        pos = vtk_widget.mapToGlobal(pos)
                except Exception:
                    pos = None
            if pos is None and vtk_widget is not None:
                pos = vtk_widget.mapToGlobal(vtk_widget.rect().center())

            if pos is not None:
                screen = None
                try:
                    if vtk_widget is not None:
                        screen = vtk_widget.screen()
                except Exception:
                    screen = None
                if screen is not None:
                    avail = screen.availableGeometry()
                    x = min(max(pos.x(), avail.left() + 8),
                            avail.right() - self.width() - 8)
                    y = min(max(pos.y(), avail.top() + 8),
                            avail.bottom() - self.height() - 8)
                    self.move(x, y)
        except Exception as e:
            _builtin_print(f"⚠️ TempFencePopup: positioning failed: {e}")
        try:
            self.show()
            self.raise_()
        except Exception:
            pass


class TempFenceTool:
    """Standalone temporary polygon fence drawer for the MAIN viewer."""

    def __init__(self, app):
        self.app = app
        self.active = False
        self._drawing = False
        self._points = []          # list of (x, y, z) tuples
        self._plane_z = None       # drawing plane elevation
        self._observed_interactor = None
        self._obs_ids = []
        self._preview_actor = None
        self._fence_actor = None   # finalized fence outline (stays visible)
        self._fence = None         # finalized fence dict (mirrored on app.temp_fence)
        self._popup = None
        self._watch_timer = None   # conversion-completion poller
        self._prev_cursor = None
        self._redo_stack = []      # vertices removed by Ctrl+Z (for Ctrl+Y)

    # ------------------------------------------------------------------
    # Widget / renderer resolution
    # ------------------------------------------------------------------
    def _vtk_widget(self):
        return getattr(self.app, "vtk_widget", None)

    def _interactor(self):
        w = self._vtk_widget()
        if w is None:
            return None
        try:
            return w.interactor
        except Exception:
            pass
        try:
            return w.GetRenderWindow().GetInteractor()
        except Exception:
            return None

    def _renderer(self):
        w = self._vtk_widget()
        if w is None:
            return None
        try:
            return w.renderer
        except Exception:
            pass
        try:
            return w.GetRenderWindow().GetRenderers().GetFirstRenderer()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Activation / deactivation
    # ------------------------------------------------------------------
    def activate(self):
        """Start (or restart) the tool. Idempotent — safe to call repeatedly."""
        inter = self._interactor()
        if inter is None:
            _safe_status(self.app, "⚠️ Temp Fence: no active 3D view")
            return
        _builtin_print(f"🩺 [tempfence] activate: interactor={type(inter).__name__} already_active={self.active}")
        if self.active and self._observed_interactor is inter:
            _safe_status(self.app, "🔲 Temp Fence: click to place fence vertices")
            return

        self._remove_observers()

        events = (
            ("LeftButtonPressEvent", self._on_left_press),
            ("LeftButtonDoubleClickEvent", self._on_double_click),
            ("RightButtonPressEvent", self._on_right_press),
            ("MouseMoveEvent", self._on_mouse_move),
            ("KeyPressEvent", self._on_key_press),
        )
        for name, cb in events:
            try:
                self._obs_ids.append(inter.AddObserver(name, cb))
            except Exception as e:
                _builtin_print(f"⚠️ TempFenceTool: observer {name} failed: {e}")
        self._observed_interactor = inter
        self.active = True
        _builtin_print(f"🗪 [tempfence] activate DONE: {len(self._obs_ids)} observers installed")

        w = self._vtk_widget()
        try:
            self._prev_cursor = w.cursor()
            w.setCursor(Qt.CrossCursor)
        except Exception:
            self._prev_cursor = None

        _safe_status(self.app, "🔲 Temp Fence: left-click vertices · Ctrl+Z undo · Ctrl+Y redo · right-click / Enter / double-click to finish · Esc to cancel")

    def deactivate(self):
        """Stand the tool down: cancel any in-progress draw.

        A finalized fence + popup stay alive (they are still usable until
        replaced, used, or Esc'd) — this only stops vertex capture.
        """
        _builtin_print(f"🩺 [tempfence] deactivate: drawing={self._drawing} pts={len(self._points)} fence={self._fence is not None}")
        self._cancel_drawing()
        self._remove_observers()
        self.active = False
        w = self._vtk_widget()
        if w is not None and self._prev_cursor is not None:
            try:
                w.setCursor(self._prev_cursor)
            except Exception:
                pass
            self._prev_cursor = None

    def shutdown(self):
        """Full teardown (app close / project clear): everything goes."""
        try:
            self.deactivate()
        except Exception:
            pass
        self.remove_fence()

    def _remove_observers(self):
        inter = self._observed_interactor
        if inter is None:
            return
        for tag in self._obs_ids:
            try:
                inter.RemoveObserver(tag)
            except Exception:
                pass
        self._obs_ids = []
        self._observed_interactor = None

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------
    def _consume(self, obj):
        """Stop the default VTK interactor style from also processing the event."""
        try:
            if hasattr(obj, "AbortFlagOn"):
                obj.AbortFlagOn()
            elif hasattr(obj, "SetAbortFlag"):
                try:
                    obj.SetAbortFlag(1)
                except TypeError:
                    obj.SetAbortFlag(True)
        except Exception:
            pass

    def _event_pos(self, obj):
        try:
            return obj.GetEventPosition()
        except Exception:
            return (0, 0)

    def _on_left_press(self, obj, evt):
        try:
            self._consume(obj)
            if not self.active:
                _builtin_print("🩺 [tempfence] left press: tool not active, ignoring")
                return
            x, y = self._event_pos(obj)
            _builtin_print(f"🩺 [tempfence] left press: screen=({x:.0f},{y:.0f}) active={self.active} drawing={self._drawing} pts={len(self._points)}")
            world = self._display_to_plane(x, y)
            _builtin_print(f"🩺 [tempfence] world={world} plane_z={self._plane_z}")
            if world is None:
                _safe_status(self.app, "⚠️ Temp Fence: cannot place a vertex — use a plan/top view")
                return
            if not self._drawing:
                self._drawing = True
                self._points = []
                self._plane_z = self._resolve_plane_z()
            # Dedupe: a double-click's second press lands on the same spot —
            # skip it so double-click-to-finalize doesn't add a junk vertex.
            if self._points:
                lx, ly, lz = self._points[-1]
                if (world[0] - lx) ** 2 + (world[1] - ly) ** 2 < 1e-12:
                    _builtin_print("🩺 [tempfence] deduped vertex")
                    return
            self._points.append((float(world[0]), float(world[1]), float(self._plane_z)))
            _builtin_print(f"🩺 [tempfence] vertex #{len(self._points)}: {self._points[-1]}")
            self._redo_stack.clear()  # new vertex invalidates redo history
            self._rebuild_preview()
        except Exception as e:
            _builtin_print(f"⚠️ TempFenceTool: left press failed: {e}")
            import traceback; traceback.print_exc()

    def _on_double_click(self, obj, evt):
        try:
            self._consume(obj)
            self._finalize()
        except Exception as e:
            _builtin_print(f"⚠️ TempFenceTool: double-click failed: {e}")

    def _on_right_press(self, obj, evt):
        try:
            self._consume(obj)
            _builtin_print(f"🩺 [tempfence] right press: drawing={self._drawing} pts={len(self._points)}")
            self._finalize()
        except Exception as e:
            _builtin_print(f"⚠️ TempFenceTool: right-click failed: {e}")
            import traceback; traceback.print_exc()

    def _on_mouse_move(self, obj, evt):
        try:
            if not self._drawing or not self._points:
                return
            x, y = self._event_pos(obj)
            world = self._display_to_plane(x, y)
            if world is None:
                return
            self._rebuild_preview(cursor_xy=(world[0], world[1]))
        except Exception:
            pass

    def _on_key_press(self, obj, evt):
        try:
            self._consume(obj)
            sym = ""
            try:
                sym = obj.GetKeySym() or ""
            except Exception:
                pass
            ctrl = False
            try:
                ctrl = bool(obj.GetControlKey())
            except Exception:
                pass
            if ctrl and sym in ("z", "Z"):
                if self.undo_vertex():
                    self._consume(obj)
                return
            if ctrl and sym in ("y", "Y"):
                if self.redo_vertex():
                    self._consume(obj)
                return
            if sym in ("Escape",):
                if self._drawing:
                    self._cancel_drawing()
                    _safe_status(self.app, "🔲 Temp Fence: draw cancelled")
                elif self._fence is not None or self._popup is not None:
                    self.remove_fence()
                    _safe_status(self.app, "🔲 Temp Fence: cleared")
            elif sym in ("Return", "Enter"):
                self._finalize()
        except Exception as e:
            _builtin_print(f"⚠️ TempFenceTool: key press failed: {e}")

    def undo_vertex(self):
        """Ctrl+Z while drawing: remove the last vertex.
        Returns True if the undo was consumed here."""
        try:
            if self._drawing and self._points:
                removed = self._points.pop()
                self._redo_stack.append(removed)
                _builtin_print(f"🩺 [tempfence] undo vertex → {len(self._points)} left")
                if self._points:
                    self._rebuild_preview()
                else:
                    self._cancel_drawing()
                    _safe_status(self.app, "🔲 Temp Fence: all vertices undone — click to start again")
                return True
        except Exception as e:
            _builtin_print(f"⚠️ TempFenceTool: undo vertex failed: {e}")
        return False

    def redo_vertex(self):
        """Ctrl+Y while drawing: re-add the last undone vertex.
        Returns True if the redo was consumed here."""
        try:
            if self._drawing and self._redo_stack:
                pt = self._redo_stack.pop()
                self._points.append(pt)
                _builtin_print(f"🩺 [tempfence] redo vertex → {len(self._points)} total")
                self._rebuild_preview()
                return True
        except Exception as e:
            _builtin_print(f"⚠️ TempFenceTool: redo vertex failed: {e}")
        return False

    # ------------------------------------------------------------------
    # Geometry helpers
    # ------------------------------------------------------------------
    def _resolve_plane_z(self):
        """Elevation of the drawing plane (digitizer-compatible fallback)."""
        # If a fence is being redrawn, keep drawing at the same elevation.
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is not None:
            try:
                z = digitizer._get_fallback_z_height()
                if z is not None:
                    return float(z)
            except Exception:
                pass
        try:
            data = getattr(self.app, "data", None)
            xyz = data.get("xyz", None) if isinstance(data, dict) else None
            if xyz is not None and len(xyz) > 0:
                step = max(1, len(xyz) // 100000)
                return float(np.median(xyz[::step, 2]))
        except Exception:
            pass
        return 0.0

    def _display_to_plane(self, x, y):
        """Intersect the camera ray through display (x, y) with the z-plane."""
        ren = self._renderer()
        if ren is None:
            return None
        z0 = self._plane_z
        if z0 is None:
            z0 = self._plane_z = self._resolve_plane_z()
        try:
            ren.SetDisplayPoint(float(x), float(y), 0.0)
            ren.DisplayToWorld()
            # ✅ FIX: GetWorldPoint (not GetDisplayPoint) — after DisplayToWorld
            # the world coordinates are in GetWorldPoint, not GetDisplayPoint.
            # In VTK 9.5 Python bindings GetWorldPoint() RETURNS the tuple
            # (it takes no out-array argument).
            wp = ren.GetWorldPoint()
            w = wp[3] if len(wp) > 3 else 1.0
            if abs(w) < 1e-12:
                return None
            wx, wy, wz = wp[0] / w, wp[1] / w, wp[2] / w

            cam = ren.GetActiveCamera()
            cx, cy, cz = cam.GetPosition()
            dx, dy, dz = wx - cx, wy - cy, wz - cz
            if abs(dz) < 1e-9:
                return None
            t = (z0 - cz) / dz
            if t <= 0:
                return None
            return (cx + dx * t, cy + dy * t, z0)
        except Exception as e:
            _builtin_print(f"⚠️ TempFenceTool: display→world failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Actors
    # ------------------------------------------------------------------
    def _make_line_actor(self, pts, color=(1.0, 1.0, 0.0), width=2.0):
        """Build a polyline + vertex-points actor from a list of (x, y, z)."""
        if not pts:
            return None
        vpoints = vtk.vtkPoints()
        for p in pts:
            vpoints.InsertNextPoint(float(p[0]), float(p[1]), float(p[2]))

        poly = vtk.vtkPolyData()
        poly.SetPoints(vpoints)

        lines = vtk.vtkCellArray()
        if len(pts) >= 2:
            line = vtk.vtkPolyLine()
            line.GetPointIds().SetNumberOfIds(len(pts))
            for i in range(len(pts)):
                line.GetPointIds().SetId(i, i)
            lines.InsertNextCell(line)
        poly.SetLines(lines)

        verts = vtk.vtkCellArray()
        for i in range(len(pts)):
            cell = vtk.vtkVertex()
            cell.GetPointIds().SetId(0, i)
            verts.InsertNextCell(cell)
        poly.SetVerts(verts)

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(poly)
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(*color)
        prop.SetLineWidth(width)
        prop.SetPointSize(6)
        prop.SetLighting(False)
        return actor

    def _add_actor(self, actor):
        ren = self._renderer()
        if ren is None or actor is None:
            return
        try:
            ren.AddActor(actor)
        except Exception:
            pass

    def _remove_actor(self, actor):
        ren = self._renderer()
        if ren is None or actor is None:
            return
        for fn in ("RemoveActor", "RemoveActor2D", "RemoveViewProp"):
            try:
                getattr(ren, fn)(actor)
            except Exception:
                pass

    def _render(self):
        w = self._vtk_widget()
        try:
            if hasattr(w, "render"):
                w.render()
            else:
                w.GetRenderWindow().Render()
        except Exception:
            pass

    def _rebuild_preview(self, cursor_xy=None):
        """Redraw the in-progress outline (rubber band to the cursor included)."""
        try:
            if self._preview_actor is not None:
                self._remove_actor(self._preview_actor)
                self._preview_actor = None
            pts = list(self._points)
            if cursor_xy is not None and pts:
                z = self._plane_z or 0.0
                pts.append((float(cursor_xy[0]), float(cursor_xy[1]), float(z)))
            if not pts:
                return
            self._preview_actor = self._make_line_actor(pts, color=(0.4, 0.9, 1.0), width=2.0)
            self._add_actor(self._preview_actor)
            self._render()
        except Exception as e:
            _builtin_print(f"⚠️ TempFenceTool: preview rebuild failed: {e}")

    def _cancel_drawing(self):
        self._drawing = False
        self._points = []
        self._redo_stack = []
        if self._preview_actor is not None:
            self._remove_actor(self._preview_actor)
            self._preview_actor = None
            self._render()

    # ------------------------------------------------------------------
    # Finalize → fence + popup
    # ------------------------------------------------------------------
    def _finalize(self):
        try:
            if not self._drawing or len(self._points) < 3:
                if self._drawing:
                    _safe_status(self.app, "⚠️ Temp Fence: need at least 3 vertices — keep clicking")
                return

            coords = [(float(p[0]), float(p[1]), float(p[2])) for p in self._points]
            # Close the visible outline (coords list itself stays open, like
            # the digitizer polygon — masks treat polygons as closed).
            outline = list(coords) + [coords[0]]

            # Replace rule: new fence kills the old popup + old fence first.
            self._teardown_previous()

            self._fence_actor = self._make_line_actor(outline, color=(1.0, 1.0, 0.0), width=2.5)
            self._add_actor(self._fence_actor)

            self._fence = {
                "type": "polygon",
                "coords": coords,
                "source": "temp_fence",
                "_temp_fence": True,
            }
            try:
                self.app.temp_fence = self._fence
            except Exception as e:
                _builtin_print(f"⚠️ TempFenceTool: could not store app.temp_fence: {e}")

            self._cancel_drawing()  # removes preview only
            self._render()

            centroid = (
                sum(c[0] for c in coords) / len(coords),
                sum(c[1] for c in coords) / len(coords),
                self._plane_z or 0.0,
            )
            self._show_popup(centroid)
            _safe_status(self.app, "✅ Temp Fence finalized — pick a By Class tool")
            _builtin_print("🔲 Temp Fence finalized with", len(coords), "vertices")
        except Exception as e:
            _builtin_print(f"⚠️ TempFenceTool: finalize failed: {e}")
            import traceback
            traceback.print_exc()

    def _teardown_previous(self):
        """New draw replaces everything: old popup + old fence are removed."""
        self._close_popup()
        self._stop_watch()
        if self._fence_actor is not None:
            self._remove_actor(self._fence_actor)
            self._fence_actor = None
        self._fence = None
        try:
            self.app.temp_fence = None
        except Exception:
            pass

    def remove_fence(self):
        """Remove the fence outline + popup + app.temp_fence (fence 'used up')."""
        self._close_popup()
        self._stop_watch()
        if self._fence_actor is not None:
            self._remove_actor(self._fence_actor)
            self._fence_actor = None
            self._render()
        self._fence = None
        try:
            self.app.temp_fence = None
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Popup
    # ------------------------------------------------------------------
    def _show_popup(self, world_xyz):
        try:
            self._close_popup()
            # ✅ Parent to the app window so the popup minimizes/restores with
            # the app exactly like every other dialog (ClassPicker, By Class, …)
            # and is discovered by _handle_app_minimized's ownership sweep.
            parent = self.app if isinstance(self.app, QWidget) else None
            self._popup = TempFencePopup(self, parent=parent)
            self._popup.open_near(world_xyz, self._renderer(), self._vtk_widget())
        except Exception as e:
            _builtin_print(f"⚠️ TempFenceTool: popup failed: {e}")

    def _close_popup(self):
        popup = self._popup
        self._popup = None
        if popup is None:
            return
        try:
            popup.close()
        except RuntimeError:
            pass
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Bridge → By Class dialogs
    # ------------------------------------------------------------------
    def run_by_class_tool(self, key):
        """Open the chosen By Class dialog with the temp fence pre-injected."""
        try:
            fence = getattr(self.app, "temp_fence", None)
            if fence is None:
                _safe_status(self.app, "⚠️ Temp Fence: no active temp fence")
                return

            entry = next((e for e in BY_CLASS_TOOLS if e[0] == key), None)
            if entry is None:
                _builtin_print(f"⚠️ TempFenceTool: unknown By Class tool '{key}'")
                return
            _key, label, _icon, method_name, dialog_attr = entry

            rm = getattr(self.app, "ribbon_manager", None)
            ribbons = getattr(rm, "ribbons", None) if rm is not None else None
            ribbon = ribbons.get("by_class") if isinstance(ribbons, dict) else None
            if ribbon is None or not hasattr(ribbon, method_name):
                _safe_status(self.app, "⚠️ By Class ribbon not available")
                return

            getattr(ribbon, method_name)()
            dlg = getattr(ribbon, dialog_attr, None)
            if dlg is None:
                _builtin_print(f"⚠️ TempFenceTool: dialog attr '{dialog_attr}' not set after open")
                return

            self._inject_fence(dlg, fence)
            self._watch_completion(dlg)

            self._close_popup()
            _safe_status(self.app, "✅ Temp fence applied — fence clears when the conversion completes")
            _builtin_print(f"🔲 Temp fence injected into By Class dialog: {label}")
        except Exception as e:
            _builtin_print(f"⚠️ TempFenceTool: run_by_class_tool({key}) failed: {e}")
            import traceback
            traceback.print_exc()

    def _inject_fence(self, dlg, fence):
        """Set the temp fence as the dialog's selected fence — no picker prompt."""
        fs = getattr(dlg, "_fence_sel", None)  # lidar_classification_tools dialogs
        if fs is not None:
            try:
                fs.selected_fences = [fence]
                lbl = getattr(fs, "status_lbl", None)
                if lbl is not None:
                    lbl.setText("🔲 1 temp fence selected")
                    lbl.setStyleSheet(
                        "color:#4fc3f7; font-size:10px; padding:2px 0; font-weight:bold;"
                    )
                _builtin_print("🔲 Temp fence set on dialog fence selector")
                return
            except Exception as e:
                _builtin_print(f"⚠️ TempFenceTool: _fence_sel injection failed: {e}")

        # menu_sidebar_system dialogs (Closed / Height / InsideFence)
        try:
            dlg.selected_fences = [fence]
        except Exception as e:
            _builtin_print(f"⚠️ TempFenceTool: selected_fences injection failed: {e}")
            return
        try:
            if hasattr(dlg, "_restore_highlights_from_data"):
                dlg._restore_highlights_from_data()
        except Exception as e:
            _builtin_print(f"⚠️ TempFenceTool: highlight restore failed: {e}")
        try:
            lbl = getattr(dlg, "fence_status", None)
            if lbl is not None:
                lbl.setText("🔲 1 temp fence selected")
                try:
                    lbl.setStyleSheet("color:#4fc3f7; font-weight:bold;")
                except Exception:
                    pass
        except Exception:
            pass
        _builtin_print("🔲 Temp fence set on By Class dialog")

    def _watch_completion(self, dlg):
        """Poll the dialog; when its conversion completes, remove the fence."""
        self._stop_watch()
        timer = QTimer()
        timer.setInterval(400)

        def _check():
            try:
                fs = getattr(dlg, "_fence_sel", None)
                if fs is not None:
                    done = bool(getattr(fs, "_conversion_completed", False))
                else:
                    done = bool(getattr(dlg, "_conversion_completed", False))
                if done:
                    timer.stop()
                    _builtin_print("🔲 By Class conversion completed — asking user about the temp fence")
                    self._ask_keep_or_delete()
            except RuntimeError:
                # Dialog was destroyed (C++ object deleted) — stop watching.
                timer.stop()
                self._stop_watch()
            except Exception:
                pass

        try:
            timer.timeout.connect(_check)
            timer.start()
        except Exception:
            return
        self._watch_timer = timer

    def _stop_watch(self):
        timer = self._watch_timer
        self._watch_timer = None
        if timer is None:
            return
        try:
            timer.stop()
        except Exception:
            pass

    def _ask_keep_or_delete(self):
        """After a conversion completes, ask whether to keep or delete the fence."""
        try:
            from PySide6.QtWidgets import QMessageBox
            mb = QMessageBox(self.app)
            mb.setWindowTitle("Temp Fence")
            mb.setText("✅ Conversion complete.\n\nKeep the temp fence or delete it?")
            keep_btn = mb.addButton("🔲 Keep Fence", QMessageBox.AcceptRole)
            del_btn = mb.addButton("🗑 Delete Fence", QMessageBox.DestructiveRole)
            mb.setDefaultButton(keep_btn)
            mb.exec()
            if mb.clickedButton() is del_btn:
                self.remove_fence()
                _safe_status(self.app, "🗑 Temp fence deleted")
            else:
                _safe_status(self.app, "🔲 Temp fence kept — Esc or draw a new fence to clear it")
        except Exception as e:
            _builtin_print(f"⚠️ TempFenceTool: keep/delete prompt failed: {e}")
            self.remove_fence()
