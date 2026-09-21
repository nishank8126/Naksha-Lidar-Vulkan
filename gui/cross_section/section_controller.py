
import numpy as np
import pyvista as pv
import vtk
import os
from pyvistaqt import QtInteractor
from PySide6.QtWidgets import QWidget, QVBoxLayout, QDockWidget, QHBoxLayout, QPushButton, QLabel, QSpinBox
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPainter, QPen, QColor
from .interactor_classify import ClassificationInteractor
from .cut_section_controller import CutSectionController


class _SectionPreviewOverlay(QWidget):
    """
    Top-Level transparent Qt overlay for rubber-band and centerline preview.
    Uses OS compositing (DWM) to prevent ghosting on top of OpenGL contexts.
    Draws using QPainter — zero VTK render cost.
    """

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        # ✅ Make it a top-level transparent window so DWM clears the background properly!
        self.setWindowFlags(
            Qt.Window | Qt.FramelessWindowHint | 
            Qt.WindowTransparentForInput | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setStyleSheet("background: transparent;")
        
        self._corners: list = []
        self._line: tuple = ()
        self._color  = QColor(255, 0, 255)
        self._width  = 2
        self._style  = 'solid'
        
        self._sync_geometry()
        self.show()

    def _sync_geometry(self):
        if not self.parent():
            return
            
        rect = self.parent().rect()
        top_left = self.parent().mapToGlobal(rect.topLeft())
        new_geo = (top_left.x(), top_left.y(), rect.width(), rect.height())
        
        # Only update if the window actually moved or resized
        if getattr(self, '_last_geo', None) != new_geo:
            self.setGeometry(*new_geo)
            self._last_geo = new_geo

    # ------------------------------------------------------------------
    def set_rect(self, corners: list, color=(255, 0, 255), width: int = 2, style: str = 'solid'):
        self._sync_geometry()
        self._corners = list(corners)
        self._color   = QColor(*color)
        self._width   = width
        self._style   = style
        if not self.isVisible():
            self.show()
            self.raise_()
        self.update()

    def set_line(self, p1: tuple, p2: tuple, color=(255, 0, 255), width: int = 2, style: str = 'solid'):
        self._sync_geometry()
        self._line    = (p1[0], p1[1], p2[0], p2[1])
        self._color   = QColor(*color)
        self._width   = width
        self._style   = style
        if not self.isVisible():
            self.show()
            self.raise_()
        self.update()

    def set_preview(self, p1: tuple, p2: tuple, corners: list,
                    color=(255, 0, 255), width: int = 2,
                    style: str = 'solid'):
        """Update the complete rectangle preview in one composited frame."""
        self._sync_geometry()
        self._line = (p1[0], p1[1], p2[0], p2[1])
        self._corners = list(corners)
        self._color = QColor(*color)
        self._width = width
        self._style = style
        if not self.isVisible():
            self.show()
            self.raise_()
        # Coalesce high-frequency mouse events into the next Qt paint pass.
        self.update()


    def clear(self):
        self._corners = []
        self._line    = ()
        # Don't call hide() here - it causes flickering and "clashing" with the OS window manager
        # during rapid continuous actions. The empty paintEvent + repaint() is enough to make it
        # instantly invisible.
        self.repaint()  # Clear before section computation blocks the UI thread

    # ------------------------------------------------------------------
    def paintEvent(self, event):
        if not self._corners and not self._line:
            return
            
        painter = QPainter(self)
        pen = QPen(self._color, self._width)
        pen.setCosmetic(True)
        _style_map = {
            'solid':        Qt.SolidLine,
            'dashed':       Qt.DashLine,
            'dotted':       Qt.DotLine,
            'dash-dot':     Qt.DashDotLine,
            'dash-dot-dot': Qt.DashDotDotLine,
        }
        pen.setStyle(_style_map.get(self._style, Qt.SolidLine))
        painter.setPen(pen)

        painter.setRenderHint(QPainter.Antialiasing, False)

        # Draw centerline if set
        if self._line:
            x1, y1, x2, y2 = self._line
            painter.drawLine(int(x1), int(y1), int(x2), int(y2))

        # Draw rectangle box if set
        if self._corners:
            pts = self._corners + [self._corners[0]]
            for i in range(len(pts) - 1):
                painter.drawLine(int(pts[i][0]),   int(pts[i][1]),
                                 int(pts[i+1][0]), int(pts[i+1][1]))
        painter.end()


class SectionController:
    def __init__(self, app ,interactor=None):
        self.app = app
        self.app.cross_view_mode ="side"   # Added by bala for view
        self.P1 = None
        self.P2 = None
        self.half_width = None
        self.active_view = 0  # ✅ Track which view dock is active
        # self._view_section_actors = {}

        # rubberband actors
        self.rubber_points = None
        self.rubber_poly = None
        self.rubber_actor = None
        self.last_mask = None
        self.section_points = None
        self._last_update_time = 0
        self._update_interval = 16
        self._preview_render_pending = False
        self._preview_render_interval_ms = 16
        self._last_preview_render_ms = 0.0

        # section actor (per-view tracking)
        self._section_actor = None
        self._core_actor = None  # ✅ Add these for multi-view
        self._buffer_actor = None
        self.cut_controller = CutSectionController(self.app)
        self.is_cut_mode = False
        self._is_initial_section_plot = True
    # ---------------------------------------------------------------------------
        if interactor is not None:
            self._attach_observers(interactor)
        
        print("✅ SectionController initialized")
        # ------------------------------------------------------------------------------------------
               
    def is_classification_active(self):
        """Check if classification tool is active (blocks section drawing)"""
        return getattr(self.app, 'active_classify_tool', None) is not None
        
    def _should_throttle_update(self):
        """Check if we should skip this update for performance"""
        import time
        self._update_interval = self._adaptive_preview_interval_ms()
        current_time = time.time() * 1000  # milliseconds
        if current_time - self._last_update_time < self._update_interval:
            return True  # Skip this update
        self._last_update_time = current_time
        return False

    def _adaptive_preview_interval_ms(self) -> int:
        """
        Adaptive preview cadence:
        - default ~60 FPS (16 ms)
        - medium pressure ~45 FPS (22 ms)
        - high pressure ~30 FPS (33 ms)
        """
        try:
            mem = getattr(self.app, "_mem_guard", None)
            stats = mem.get_stats() if mem is not None else {}
            ram_pct = float(stats.get("ram_pct", 0.0) or 0.0)
        except Exception:
            ram_pct = 0.0

        try:
            n_points = len(getattr(self.app, "data", {}).get("xyz", []))
        except Exception:
            n_points = 0

        if ram_pct >= 9.0 or n_points >= 12_000_000:
            return 33
        if ram_pct >= 7.5 or n_points >= 6_000_000:
            return 22
        return 16

    def _section_point_budget(self) -> int:
        """
        Optional manual cap for points retained per cross-section view.
        Default behavior is uncapped (render all section points).
        Set env var NAKSHA_SECTION_POINT_BUDGET to enable a manual cap.
        """
        raw = str(os.getenv("NAKSHA_SECTION_POINT_BUDGET", "")).replace(",", "").strip()
        if not raw:
            return None
        try:
            env_cap = int(raw)
            if env_cap >= 100_000:
                return int(env_cap)
        except Exception:
            pass

        return None

    @staticmethod
    def _uniform_pick_indices(n_points: int, keep_count: int) -> np.ndarray:
        """Deterministic evenly spaced sampling indices."""
        if keep_count <= 0 or n_points <= 0:
            return np.empty((0,), dtype=np.int64)
        if keep_count >= n_points:
            return np.arange(n_points, dtype=np.int64)
        return np.linspace(0, n_points - 1, num=keep_count, dtype=np.int64)

    def _cap_section_points(
        self,
        core_points_local: np.ndarray,
        buffer_points_local: np.ndarray,
        core_indices: np.ndarray,
        buffer_indices: np.ndarray,
    ):
        """
        Cap section point volume while preserving core/buffer proportion.
        Returns potentially downsampled arrays plus stats dict.
        """
        core_n = int(len(core_points_local))
        buf_n = int(len(buffer_points_local))
        total_n = core_n + buf_n
        budget = self._section_point_budget()

        stats = {
            "budget": budget if budget is not None else total_n,
            "core_before": core_n,
            "buffer_before": buf_n,
            "total_before": total_n,
            "core_after": core_n,
            "buffer_after": buf_n,
            "total_after": total_n,
            "downsampled": False,
        }

        if budget is None or total_n <= 0 or total_n <= budget:
            return core_points_local, buffer_points_local, core_indices, buffer_indices, stats

        # Keep most of budget for core slice points, then fill remaining with buffer.
        core_target = min(core_n, max(1, int(round(budget * 0.8))))
        remaining = max(0, budget - core_target)
        buf_target = min(buf_n, remaining)

        if core_target < core_n and (core_target + buf_target) < budget:
            extra = min(core_n - core_target, budget - (core_target + buf_target))
            core_target += max(0, extra)

        if core_target <= 0 and core_n > 0:
            core_target = 1
        if (core_target + buf_target) > budget:
            overflow = (core_target + buf_target) - budget
            if buf_target >= overflow:
                buf_target -= overflow
            else:
                core_target = max(1 if core_n > 0 else 0, core_target - (overflow - buf_target))
                buf_target = 0

        core_sel = self._uniform_pick_indices(core_n, core_target)
        buf_sel = self._uniform_pick_indices(buf_n, buf_target)

        core_points_ds = core_points_local[core_sel] if core_sel.size else core_points_local[:0]
        buf_points_ds = buffer_points_local[buf_sel] if buf_sel.size else buffer_points_local[:0]
        core_indices_ds = core_indices[core_sel] if core_sel.size else core_indices[:0]
        buffer_indices_ds = buffer_indices[buf_sel] if buf_sel.size else buffer_indices[:0]

        stats["core_after"] = int(len(core_points_ds))
        stats["buffer_after"] = int(len(buf_points_ds))
        stats["total_after"] = int(len(core_points_ds) + len(buf_points_ds))
        stats["downsampled"] = True

        return core_points_ds, buf_points_ds, core_indices_ds, buffer_indices_ds, stats

    def _schedule_main_preview_render(self):
        """Coalesce preview renders to avoid render-on-every-mousemove stalls."""
        self._preview_render_interval_ms = self._adaptive_preview_interval_ms()
        if self._preview_render_pending:
            return
        self._preview_render_pending = True

        def _flush():
            self._preview_render_pending = False
            try:
                if hasattr(self.app, "vtk_widget") and self.app.vtk_widget is not None:
                    self.app.vtk_widget.render()
            except Exception:
                pass

        QTimer.singleShot(self._preview_render_interval_ms, _flush)
    
    
    def _attach_observers(self, interactor):
        """
        Attach event observers to interactor.
        Can be called later if interactor wasn't available during __init__.
        
        Args:
            interactor: vtk.vtkRenderWindowInteractor
        """
        # Store observer tags for later removal
        if not hasattr(self, '_observer_tags'):
            self._observer_tags = []
        
        # Add observers with HIGH priority (higher than measurement tool's -10.0)
        tag1 = interactor.AddObserver("LeftButtonPressEvent", self.on_left_press, 1.0)
        tag2 = interactor.AddObserver("MouseMoveEvent", self.on_mouse_move, 1.0)
        tag3 = interactor.AddObserver("RightButtonPressEvent", self.on_right_press, 1.0)
        
        self._observer_tags.extend([tag1, tag2, tag3])
        
        print("✅ SectionController observers attached")

    def on_left_press(self, obj, event):
        """Handle left button press."""
        if self.is_classification_active():
            print(f"⏭️ Classification active - section drawing disabled")
            return  # Let classification handle it

        # ✅ Check if measurement tool is active and should take priority
        if self._should_block_for_measurement():
            print("📏 Measurement tool active - cross-section blocked")
            return

        # MicroStation-style locate: click in section view while cross-section
        # tool is active → pan main view to that world XY and start rubber-band.
        if (
            getattr(self.app, 'cross_section_active', False)
            and getattr(self.app, 'section_locate_enabled', True)
        ):
            x, y = obj.GetEventPosition()
            self._do_section_locate(obj, x, y)
            return
        
    # ── MicroStation-style section locate ───────────────────────────────────

    def _do_section_locate(self, vtk_interactor, display_x, display_y):
        """
        Click in section view while cross-section tool is active:
        - Inverse-transform the clicked display position to real-world XY
        - Set cross_interactor.P1 so the main-view preview line starts there
        - Pan main view camera to that location
        - Store locate state so on_mouse_move can draw a rubber-band in section view
        """
        v_idx = self.active_view
        sP1 = getattr(self.app, f'section_{v_idx}_P1', None)
        sP2 = getattr(self.app, f'section_{v_idx}_P2', None)
        if sP1 is None or sP2 is None:
            print("⚠️ Section locate: no P1/P2 for view", v_idx)
            return

        # Display → section-local (along_c, across_c≈0, Z)
        # Camera looks along Y (across), so X = along, Z = elevation.
        try:
            ren = vtk_interactor.GetRenderWindow().GetRenderers().GetFirstRenderer()
            coord = vtk.vtkCoordinate()
            coord.SetCoordinateSystemToDisplay()
            coord.SetValue(float(display_x), float(display_y), 0.0)
            local_pt = coord.GetComputedWorldValue(ren)
            along = float(local_pt[0])
        except Exception as e:
            print(f"⚠️ Section locate: display→world failed: {e}")
            return

        # Inverse transform → real world XY
        v = np.asarray(sP2[:2], dtype=np.float64) - np.asarray(sP1[:2], dtype=np.float64)
        length = float(np.linalg.norm(v))
        if length < 1e-9:
            return
        dir_vec = v / length
        world_x = float(sP1[0]) + along * dir_vec[0]
        world_y = float(sP1[1]) + along * dir_vec[1]

        # Store for rubber-band drawing in on_mouse_move
        self.app._section_locate_display = (float(display_x), float(display_y))
        self.app._section_locate_view = v_idx

        # Arm cross_interactor so main-view preview line starts from this point
        cross = getattr(self.app, 'cross_interactor', None)
        if cross is not None:
            main_vtk = getattr(self.app, 'vtk_widget', None)
            if main_vtk is not None:
                focal_z = main_vtk.renderer.GetActiveCamera().GetFocalPoint()[2]
                cross.P1 = np.array([world_x, world_y, focal_z], dtype=np.float64)
                cross.slice_state = 1

        # Pan main view camera to (world_x, world_y)
        main_vtk = getattr(self.app, 'vtk_widget', None)
        if main_vtk is not None:
            cam = main_vtk.renderer.GetActiveCamera()
            focal = cam.GetFocalPoint()
            pos   = cam.GetPosition()
            dx_pan = world_x - focal[0]
            dy_pan = world_y - focal[1]
            cam.SetFocalPoint(world_x, world_y, focal[2])
            cam.SetPosition(pos[0] + dx_pan, pos[1] + dy_pan, pos[2])
            main_vtk.renderer.ResetCameraClippingRange()
            main_vtk.render()

        if hasattr(self.app, 'statusBar'):
            self.app.statusBar().showMessage(
                f"Locked ({world_x:.1f}, {world_y:.1f})  —  "
                "move to main view and draw cross-section",
                5000
            )
        print(f"📍 Section locate: ({world_x:.2f}, {world_y:.2f}), along={along:.2f}m")

    def _draw_locate_rubber_band(self, cursor_x, cursor_y):
        """
        Draw a rubber-band line in the section view from the locked locate point
        to the current cursor position (both in VTK display coords).
        Uses the same color as the cross-section preview (cross_line_color).
        """
        vtk_widget = self.app.section_vtks.get(self.active_view)
        if vtk_widget is None:
            return
        ren = vtk_widget.renderer
        locate_disp = getattr(self.app, '_section_locate_display', None)
        if locate_disp is None:
            return

        # Lazy-init 2D line actor (display-space, no world transform needed)
        if not hasattr(self, '_locate_rb_actor') or self._locate_rb_actor is None:
            pts = vtk.vtkPoints()
            pts.SetNumberOfPoints(2)
            lines = vtk.vtkCellArray()
            lines.InsertNextCell(2)
            lines.InsertCellPoint(0)
            lines.InsertCellPoint(1)
            poly = vtk.vtkPolyData()
            poly.SetPoints(pts)
            poly.SetLines(lines)
            dc = vtk.vtkCoordinate()
            dc.SetCoordinateSystemToDisplay()
            mapper = vtk.vtkPolyDataMapper2D()
            mapper.SetInputData(poly)
            mapper.SetTransformCoordinate(dc)
            actor = vtk.vtkActor2D()
            actor.SetMapper(mapper)
            self._locate_rb_pts   = pts
            self._locate_rb_poly  = poly
            self._locate_rb_actor = actor

        actor = self._locate_rb_actor
        if not ren.HasViewProp(actor):
            ren.AddActor2D(actor)
        actor.VisibilityOn()

        color = getattr(self.app, 'cross_line_color', (1.0, 0.0, 1.0))
        width = getattr(self.app, 'cross_line_width', 2)
        prop  = actor.GetProperty()
        prop.SetColor(*color)
        prop.SetLineWidth(max(2, width))
        prop.SetOpacity(1.0)

        self._locate_rb_pts.SetPoint(0, locate_disp[0], locate_disp[1], 0.0)
        self._locate_rb_pts.SetPoint(1, float(cursor_x), float(cursor_y), 0.0)
        self._locate_rb_pts.Modified()
        self._locate_rb_poly.Modified()
        vtk_widget.render()

    def clear_locate_state(self):
        """Remove locate rubber-band from renderer and clear the stored locate position."""
        try:
            actor = getattr(self, '_locate_rb_actor', None)
            if actor is not None:
                # Try the stored view first, but if _section_locate_view was already
                # cleared (e.g. by cross-section deactivation), iterate ALL views
                target_view = getattr(self.app, '_section_locate_view', None)
                removed = False
                if target_view is not None:
                    vtk_widget = self.app.section_vtks.get(target_view)
                    if vtk_widget is not None and hasattr(vtk_widget, 'renderer'):
                        try:
                            vtk_widget.renderer.RemoveActor2D(actor)
                            vtk_widget.render()
                            removed = True
                        except Exception:
                            pass
                if not removed:
                    for v_idx, vtk_widget in getattr(self.app, 'section_vtks', {}).items():
                        try:
                            if hasattr(vtk_widget, 'renderer') and vtk_widget.renderer.HasViewProp(actor):
                                vtk_widget.renderer.RemoveActor2D(actor)
                                vtk_widget.render()
                                removed = True
                        except Exception:
                            pass
                self._locate_rb_actor = None
        except Exception:
            pass
        try:
            cut_controller = getattr(self.app, "cut_section_controller", None)
            if cut_controller is not None and hasattr(cut_controller, "clear_cut_locate_rubber_band"):
                cut_controller.clear_cut_locate_rubber_band()
        except Exception:
            pass
        self.app._section_locate_display = None
        self.app._section_locate_view = None

    def set_section_locate_enabled(self, enabled: bool, clear_state: bool = True):
        """Enable/disable section-locate interaction without closing cross-section views."""
        try:
            self.app.section_locate_enabled = bool(enabled)
        except Exception:
            pass

        if not enabled and clear_state:
            self.clear_locate_state()

    # ── end locate ──────────────────────────────────────────────────────────

    def _display_to_world_no_snap(self, x, y, z_lock=None):
        """
        TRUE cursor-following conversion.
        No snapping, no picking, zoom-safe.
        """
        ren = self.app.vtk_widget.renderer

        coord = vtk.vtkCoordinate()
        coord.SetCoordinateSystemToDisplay()
        coord.SetValue(float(x), float(y), 0.0)

        world = coord.GetComputedWorldValue(ren)
        P = np.array(world, dtype=np.float64)

        if z_lock is not None:
            P[2] = z_lock

        return P    
        
    def on_mouse_move(self, obj, evt):
            """Draw preview as mouse moves - handles BOTH cross-section and cut section."""

            # ✅ THROTTLE FIRST - CRITICAL for smooth lines
            if self._should_throttle_update():
                return
            # Safety checks
            if not hasattr(self, 'app') or self.app is None:
                return

            # Locate rubber-band: draw preview from locked section point to cursor
            locate_disp = getattr(self.app, '_section_locate_display', None)
            locate_view = getattr(self.app, '_section_locate_view', None)
            if locate_disp is not None and locate_view == self.active_view:
                x, y = obj.GetEventPosition()
                self._draw_locate_rubber_band(x, y)
                return

            if not hasattr(self, 'interactor') or self.interactor is None:
                return

            tool = getattr(self.app, "active_classify_tool", None)
            if tool is None:
                return
            
            # Get current mouse position
            x, y = self.interactor.GetEventPosition()
            
            # Determine if we're in cut section
            vtk_widget = self._get_active_vtk_widget()
            is_cut_section = False
            if hasattr(self.app, 'cut_section_controller'):
                cut_vtk = getattr(self.app.cut_section_controller, 'cut_vtk', None)
                if cut_vtk and vtk_widget == cut_vtk:
                    is_cut_section = True
            
            # ═══════════════════════════════════════════════════════════════════
            # FREEHAND - Collect points while dragging
            # ═══════════════════════════════════════════════════════════════════
            if tool == "freehand" and getattr(self, 'is_drawing_freehand', False):
                try:
                    pt = self._pick_world_point(x, y)
                    P = self._get_view_coordinates(pt)
                    self.drawing_points.append(P)
                    
                    # Store display coordinates for cut section preview
                    if is_cut_section:
                        if not hasattr(self, 'drawing_points_display_cut'):
                            self.drawing_points_display_cut = []
                        self.drawing_points_display_cut.append((x, y))
                        self._draw_freehand_preview_cut()
                    else:
                        self._draw_freehand_preview()
                except Exception:
                    pass
                return
            
            # ═══════════════════════════════════════════════════════════════════
            # BRUSH / POINT - Draw circle at cursor
            # ═══════════════════════════════════════════════════════════════════
            if tool in ("brush", "point"):
                try:
                    pt = self._pick_world_point(x, y)
                    radius = getattr(self.app, "brush_radius", 1.0) if tool == "brush" else getattr(self.app, "point_radius", 0.5)
                    
                    if is_cut_section:
                        self._draw_brush_preview_cut(pt, radius)
                    else:
                        self._draw_brush_preview(pt, radius)
                except Exception:
                    pass
                return
            
            # ═══════════════════════════════════════════════════════════════════
            # LINE / RECTANGLE / CIRCLE - Need P1 and dragging
            # ═══════════════════════════════════════════════════════════════════
            # ✅ FIX: Don't snap during preview - use raw world coordinates
            try:
    
                P2 = self._display_to_world_no_snap(x, y, z_lock=self.P1[2])
                
            except Exception:
                return

            
            # ═══════════════════════════════════════════════════════════════════
            # ABOVE LINE / BELOW LINE
            # ═══════════════════════════════════════════════════════════════════
            if tool in ("above_line", "below_line", "parallel_line"):
                if is_cut_section:
                    print(f"🔧 Drawing line preview in CUT SECTION (P1={self.P1[:2]}, P2={P2[:2]})")
                    self._draw_line_preview_cut(self.P1, P2)
                else:
                    self._draw_line_preview(self.P1, P2)
            
            # ═══════════════════════════════════════════════════════════════════
            # RECTANGLE
            # ═══════════════════════════════════════════════════════════════════
            elif tool == "rectangle":
                if is_cut_section:
                    self._draw_rectangle_preview_cut(self.P1, P2)
                else:
                    self._draw_rectangle_preview(self.P1, P2)
            
            # ═══════════════════════════════════════════════════════════════════
            # CIRCLE
            # ═══════════════════════════════════════════════════════════════════
            elif tool == "circle":
                # Calculate center and radius
                center = np.array([
                    (self.P1[0] + P2[0]) / 2,
                    (self.P1[1] + P2[1]) / 2,
                    (self.P1[2] + P2[2]) / 2
                ])
                radius = np.linalg.norm(P2 - self.P1) / 2
                
                if is_cut_section:
                    self._draw_circle_preview_cut(center, radius)
                else:
                    self._draw_circle_preview(center, radius)

    def on_right_press(self, obj, event):
        """Handle right button press."""
        # ✅ Check if measurement tool is active and should take priority
        if self._should_block_for_measurement():
            return

        # Reactivate the last classification tool when right-clicking in a
        # cross-section window and no tool is currently active.
        if getattr(self.app, "active_classify_tool", None) is None:
            last_tool = getattr(self.app, "last_classify_tool", None)
            if last_tool and hasattr(self.app, "set_classify_tool"):
                try:
                    self.app.from_classes = getattr(self.app, "last_classify_from_classes", None)
                    self.app.to_class = getattr(self.app, "last_classify_to_class", None)
                    self.app.set_classify_tool(last_tool)
                except Exception as e:
                    print(f"⚠️ Right-click reactivate failed: {e}")

    def _get_view_palette(self, view_index):
        """
        Get the palette for a specific view from DisplayModeDialog.
        Returns view-specific palette.
        Fallback order:
          1) dialog.view_palettes[target_slot]
          2) app.view_palettes[target_slot]
          3) app.class_palette ONLY for main slot (0)
        """
        try:
            target_slot = view_index + 1  # View 1 = slot 1, View 2 = slot 2, etc.

            if hasattr(self.app, 'display_mode_dialog'):
                dialog = self.app.display_mode_dialog
                
                if hasattr(dialog, 'view_palettes') and target_slot in dialog.view_palettes:
                    palette = dialog.view_palettes[target_slot]
                    print(f"   📋 Using view_palettes[{target_slot}] for filtering")
                    return palette

            if hasattr(self.app, 'view_palettes') and isinstance(self.app.view_palettes, dict):
                slot_palette = self.app.view_palettes.get(target_slot)
                if slot_palette:
                    print(f"   📋 Using app.view_palettes[{target_slot}] for filtering")
                    return slot_palette

            # Never leak main palette into cross-section slots.
            if target_slot == 0:
                return getattr(self.app, 'class_palette', {})

            print(f"   ⚠️ No slot palette for view {view_index + 1}; using empty fallback")
            return {}
        except Exception as e:
            print(f"   ⚠️ Error getting view palette: {e}")
            return {}

    def _get_visible_classes_from_palette(self, palette):
        """
        Get list of class codes that are visible (show=True) in the palette.
        Returns None if no filtering should be applied (all visible).
        """
        if not palette:
            return None  # No filtering
        
        visible = [code for code, info in palette.items() if info.get('show', True)]
        
        if len(visible) == len(palette):
            return None
        
        return visible if visible else []

    def _make_colors_with_palette(self, points, classes, palette):
        """
        🚀 VECTORIZED COLOR MAPPING: 
        Builds colors using GLOBAL app.class_palette while respecting per-view visibility.
        ✅ SPEED: Processes millions of points in ~5ms.
        ✅ LOGIC: Keeps colors consistent with Main View but allows local hiding.
        """
        import numpy as np
        
        # 1. Initialize result array (default to middle gray)
        colors = np.full((points.shape[0], 3), 128, dtype=np.uint8)
        
        if classes.size == 0:
            return colors

        # 2. Get palettes
        global_palette = getattr(self.app, 'class_palette', {}) or {}
        view_palette = palette if palette else {}

        # 3. Create a Fast Lookup Table (LUT)
        # We find the highest class code to determine LUT size
        max_c = int(classes.max())
        lut = np.zeros((max_c + 1, 3), dtype=np.uint8)

        # 4. Fill the LUT
        # This is where we combine Global Color + View Visibility
        unique_codes = np.unique(classes)
        for code in unique_codes:
            code_int = int(code)
            if code_int > max_c: continue
            
            # Check visibility from the per-view palette
            vp = view_palette.get(code_int, {"show": True})
            is_visible = vp.get("show", True)

            if is_visible:
                # Get the "True" color from the global application palette
                global_entry = global_palette.get(code_int, {"color": (128, 128, 128)})
                lut[code_int] = global_entry.get("color", (128, 128, 128))
            else:
                lut[code_int] = (0, 0, 0)

        # 5. ⚡ THE MAGIC: One-shot vectorized mapping
        # Maps every point to its color based on its classification code
        colors = lut[classes.astype(int)]

        return colors
    
    def _clear_cross_section_point_actors(self, vtk_widget, view_index: int):
        """
        Remove ONLY point-cloud actors for a cross-section view.
        ✅ UNIFIED ACTOR: removes the unified section actor so it can be rebuilt.
        Also clears legacy class_* / section_* names for any leftover old-mode actors.
        NOTE: do NOT call this during a fast GPU refresh — only call on full rebuild.
        """
        if vtk_widget is None or not hasattr(vtk_widget, "actors"):
            return

        # Unified actor for this view (primary target on rebuild)
        unified_name = f"_section_{view_index}_unified"

        # Legacy per-class actor prefixes (safe to remove if they somehow exist)
        prefixes = (
            "class_",                           # class_{code}, class_{code}_border
            f"section_core_{view_index}",        # section_core_{view}_{code}
            f"section_buffer_{view_index}",      # section_buffer_{view}_{code}
        )

        for name in list(vtk_widget.actors.keys()):
            if name == unified_name or any(name.startswith(p) for p in prefixes):
                try:
                    vtk_widget.remove_actor(name, render=False)
                except Exception:
                    pass

    def set_active_view(self, view_index):
        """Don't switch view if cut is locked."""
        # ✅ BLOCK view switch if cut locked
        if hasattr(self.app, 'cut_section_controller'):
            if getattr(self.app.cut_section_controller, 'is_locked', False):
                print(f"🔒 CUT LOCKED - Cannot switch views")
                return
        
        self.active_view = view_index
        print(f"Active view: {view_index}")

    def store_section_data(self, section_index, P1, P2, half_width, core_points, buffer_points, core_mask, buffer_mask):
        """
        ✅ Store section data with view-specific keys
        """
        # Store per-view data
        setattr(self.app, f'section_{section_index}_P1', P1)
        setattr(self.app, f'section_{section_index}_P2', P2)
        setattr(self.app, f'section_{section_index}_half_width', half_width)
        setattr(self.app, f'section_{section_index}_core_points', core_points)
        setattr(self.app, f'section_{section_index}_buffer_points', buffer_points)
        setattr(self.app, f'section_{section_index}_core_mask', core_mask)
        setattr(self.app, f'section_{section_index}_buffer_mask', buffer_mask)
        
        print(f"✅ Stored section data for view {section_index + 1}: {len(core_points)} core, {len(buffer_points)} buffer")

    # ✅ NEW: Get the active VTK widget for current view
    def _get_qt_overlay(self) -> '_SectionPreviewOverlay | None':
        """Return (or lazily create) the zero-cost Qt preview overlay."""
        current_widget = self.app.vtk_widget
        
        if getattr(self, '_qt_overlay', None) is not None:
            try:
                # Re-use ONLY if the parent widget is still the same (user didn't switch views)
                if self._qt_overlay.parent() == current_widget:
                    return self._qt_overlay
                else:
                    # Target view changed! Hide and destroy the old overlay instantly.
                    self._qt_overlay.hide()
                    self._qt_overlay.deleteLater()
            except RuntimeError:
                pass
            self._qt_overlay = None

        try:
            overlay = _SectionPreviewOverlay(current_widget)
            color_tuple = getattr(self.app, 'cross_line_color', (1.0, 0.0, 1.0))
            overlay._color = QColor(
                int(color_tuple[0] * 255),
                int(color_tuple[1] * 255),
                int(color_tuple[2] * 255),
            )
            overlay._width = int(getattr(self.app, 'cross_line_width', 2))
            self._qt_overlay = overlay
            return overlay
        except Exception as e:
            print(f"⚠️ Failed to create Qt overlay: {e}")
            return None

    def _vtk_display_to_qt(self, x: float, y: float) -> tuple:
        """Convert VTK display coords (origin bottom-left) → Qt widget coords (origin top-left)."""
        try:
            h = self.app.vtk_widget.height()
        except Exception:
            try:
                h = self.app.vtk_widget.GetRenderWindow().GetSize()[1]
            except Exception:
                h = 800
        return (x, h - y - 1)

    def _get_active_vtk(self):
        """Return the VTK widget for the currently active view."""
        if not hasattr(self, 'active_view'):
            self.active_view = 0
        
        # Check if we have view-specific VTK widgets
        if hasattr(self.app, 'section_vtks') and self.active_view in self.app.section_vtks:
            vtk_widget = self.app.section_vtks[self.active_view]
            print(f"🎯 Using VTK widget for View {self.active_view + 1}")
            return vtk_widget
        
        # Fallback to default sec_vtk
        if hasattr(self.app, 'sec_vtk') and self.app.sec_vtk:
            print(f"⚠️ Falling back to default sec_vtk")
            return self.app.sec_vtk
        
        print(f"❌ No VTK widget found for view {self.active_view}")
        return None

    # ---------------- CLEANUP ----------------
    def clear(self):
        # Clear Qt overlay (instant)
        overlay = getattr(self, '_qt_overlay', None)
        if overlay is not None:
            try:
                overlay.clear()
            except Exception:
                pass

        if getattr(self.app, "_shutdown_in_progress", False):
            return

        renderer = getattr(getattr(self.app, "vtk_widget", None), "renderer", None)
        if renderer is None:
            return

        vtk_widget = self._get_active_vtk()
        if vtk_widget:
            vtk_widget.clear()
        if self.rubber_actor:
            renderer.RemoveActor(self.rubber_actor)
        self.rubber_actor = None
        self.rubber_points = None
        self.rubber_poly = None

        # Clear any VTK 2D actors that may have accumulated
        if getattr(self, '_rubber_actor_2d', None):
            renderer.RemoveActor2D(self._rubber_actor_2d)
            self._rubber_actor_2d = None
            self._rubber_points_2d = None
            self._rubber_poly_2d = None
        if getattr(self, '_centerline_actor_2d', None):
            renderer.RemoveActor2D(self._centerline_actor_2d)
            self._centerline_actor_2d = None
            self._centerline_points_2d = None
            self._centerline_poly_2d = None

        self._section_actor = None
        self._schedule_main_preview_render()

    def clear_preview(self, force_overlay_destroy: bool = False):
        """Clear only active preview elements, leaving finalized sections intact."""
        needs_render = False

        # Clear Qt overlay (Top-Level)
        overlay = getattr(self, '_qt_overlay', None)
        if overlay is not None:
            try:
                overlay.clear()
            except Exception:
                pass
            if force_overlay_destroy:
                try:
                    overlay.hide()
                except Exception:
                    pass
                try:
                    overlay.deleteLater()
                except Exception:
                    pass
                self._qt_overlay = None

        # Clear 3D rubber band
        if hasattr(self, "rubber_actor") and self.rubber_actor:
            self._remove_actor_from_all_renderers(self.rubber_actor, is_2d=False)
            self.rubber_actor = None
            self.rubber_points = None
            self.rubber_poly = None
            needs_render = True

        # Clear 2D rubber band — must use RemoveActor2D for vtkActor2D instances
        if hasattr(self, "_rubber_actor_2d") and self._rubber_actor_2d:
            self._remove_actor_from_all_renderers(self._rubber_actor_2d, is_2d=True)
            self._rubber_actor_2d = None
            self._rubber_points_2d = None
            self._rubber_poly_2d = None
            needs_render = True

        # Clear 2D centerline — must use RemoveActor2D for vtkActor2D instances
        if hasattr(self, "_centerline_actor_2d") and self._centerline_actor_2d:
            self._remove_actor_from_all_renderers(self._centerline_actor_2d, is_2d=True)
            self._centerline_actor_2d = None
            self._centerline_points_2d = None
            self._centerline_poly_2d = None
            needs_render = True

        if needs_render:
            self.app.vtk_widget.render()

    # ---------------- RECTANGLE INIT ----------------
    def _init_rectangle(self, npoints=5):
        """OPTIMIZED: Initialize rectangle once with reusable structures"""
        import vtk
        
        if hasattr(self, "rubber_actor") and self.rubber_actor is not None \
        and hasattr(self, "rubber_points") and self.rubber_points is not None \
        and self.rubber_points.GetNumberOfPoints() == npoints:
            print(f"Rectangle already initialized with {npoints} points, skipping")
            return
        
        if hasattr(self, "rubber_actor") and self.rubber_actor:
            try:
                self.app.vtk_widget.renderer.RemoveActor(self.rubber_actor)
            except Exception:
                pass
        
        # Create points array
        self.rubber_points = vtk.vtkPoints()
        self.rubber_points.SetNumberOfPoints(npoints)
        
        for i in range(npoints):
            self.rubber_points.SetPoint(i, 0.0, 0.0, 0.0)
        
        self.rubber_poly = vtk.vtkPolyData()
        self.rubber_poly.SetPoints(self.rubber_points)
        
        # Get user settings
        color = getattr(self.app, "cross_line_color", (1, 0, 1))
        width = getattr(self.app, "cross_line_width", 1)
        style = getattr(self.app, "cross_line_style", "solid")
        
        # Create INITIAL line topology (solid)
        lines = vtk.vtkCellArray()
        for i in range(npoints - 1):
            lines.InsertNextCell(2)
            lines.InsertCellPoint(i)
            lines.InsertCellPoint(i + 1)
        
        self.rubber_poly.SetLines(lines)
        
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(self.rubber_poly)
        mapper.SetResolveCoincidentTopologyToPolygonOffset()
        
        self.rubber_actor = vtk.vtkActor()
        self.rubber_actor.SetMapper(mapper)
        
        prop = self.rubber_actor.GetProperty()
        prop.SetColor(color)
        prop.SetLineWidth(width)
        prop.SetOpacity(1.0)
        
        self.app.vtk_widget.renderer.AddActor(self.rubber_actor)
        self._rubber_initialized = True
        print(f"✅ Rectangle geometry initialized with {npoints} points (style: {style})")
        
        # CRITICAL FIX: Apply the style immediately after initialization
        # This will NOT work here because points are all at (0,0,0) initially
        # Style must be applied AFTER points are set to actual positions

    def _init_centerline_2d(self):
        import vtk
 
        self._centerline_points_2d = vtk.vtkPoints()
        self._centerline_points_2d.SetNumberOfPoints(2)
 
        self._centerline_poly_2d = vtk.vtkPolyData()
        self._centerline_poly_2d.SetPoints(self._centerline_points_2d)
 
        lines = vtk.vtkCellArray()
        lines.InsertNextCell(2)
        lines.InsertCellPoint(0)
        lines.InsertCellPoint(1)
        self._centerline_poly_2d.SetLines(lines)
 
        mapper = vtk.vtkPolyDataMapper2D()
        mapper.SetInputData(self._centerline_poly_2d)
 
        actor = vtk.vtkActor2D()
        actor.SetMapper(mapper)
 
        prop = actor.GetProperty()
        prop.SetColor(*getattr(self.app, "cross_line_color", (1, 0, 1)))
        prop.SetLineWidth(getattr(self.app, "cross_line_width", 2))
 
        self.app.vtk_widget.renderer.AddActor2D(actor)
 
        self._centerline_actor_2d = actor    
       

    def update_rectangle_style(self):
        """Update rectangle line style by regenerating geometry for dashed/dotted lines"""
        import numpy as np
        import vtk
        
        if not hasattr(self, "rubber_actor") or self.rubber_actor is None:
            print("⚠️ No rubber_actor found")
            return
        
        if not hasattr(self, "rubber_points") or self.rubber_points is None:
            print("⚠️ No rubber_points found")
            return
        
        # Get current corner points
        npoints = self.rubber_points.GetNumberOfPoints()
        corners = []
        for i in range(npoints):
            pt = self.rubber_points.GetPoint(i)
            corners.append(pt)
        
        if len(corners) < 2:
            print("⚠️ Not enough corner points")
            return
        
        style = getattr(self.app, "cross_line_style", "solid")
        print(f"🎨 Applying style '{style}' to rectangle with {len(corners)} corners")
        
        if style == "solid":
            # Use normal continuous line - restore original topology
            lines = vtk.vtkCellArray()
            for i in range(len(corners) - 1):
                lines.InsertNextCell(2)
                lines.InsertCellPoint(i)
                lines.InsertCellPoint(i + 1)
            
            # CRITICAL: Use original points, not new ones
            self.rubber_poly.SetLines(lines)
            self.rubber_poly.Modified()
            print("✅ Solid line applied")
            return
            
        # ============================================================
        # Create dashed geometry by breaking lines into segments
        # ============================================================
        newpoints = vtk.vtkPoints()
        lines = vtk.vtkCellArray()
        
        # ✅ FIX: Use ABSOLUTE dash/gap lengths instead of ratios
        # This ensures visibility regardless of segment length
        if style == "dashed":
            dash_length = 10.0   # 10 units dash
            gap_length = 5.0     # 5 units gap
        elif style == "dotted":
            dash_length = 2.0    # 2 units (small dots)
            gap_length = 4.0     # 4 units gap
        elif style == "dash-dot":
            # Pattern: long dash, gap, dot, gap, repeat
            dash_pattern = [(10.0, 5.0), (2.0, 5.0)]  # (dash, gap) pairs
        elif style == "dash-dot-dot":
            # Pattern: long dash, gap, dot, gap, dot, gap, repeat
            dash_pattern = [(10.0, 5.0), (2.0, 4.0), (2.0, 5.0)]
        else:
            dash_length = 10.0
            gap_length = 5.0
        
        point_idx = 0
        total_segments = 0
        
        # Create dashed segments for each edge
        for edge_idx in range(len(corners) - 1):
            p1 = np.array(corners[edge_idx])
            p2 = np.array(corners[edge_idx + 1])
            segment_vec = p2 - p1
            segment_len = np.linalg.norm(segment_vec)
            
            if segment_len < 1e-6:
                continue
            
            direction = segment_vec / segment_len
            
            if style in ["dash-dot", "dash-dot-dot"]:
                # Complex pattern with multiple dash types
                t = 0.0
                pattern_idx = 0
                
                while t < segment_len:
                    current_dash, current_gap = dash_pattern[pattern_idx]
                    
                    # Start of dash
                    if t < segment_len:
                        dash_start = p1 + direction * t
                        newpoints.InsertNextPoint(dash_start[0], dash_start[1], dash_start[2])
                        start_idx = point_idx
                        point_idx += 1
                        
                        # End of dash
                        dash_end_t = min(t + current_dash, segment_len)
                        dash_end = p1 + direction * dash_end_t
                        newpoints.InsertNextPoint(dash_end[0], dash_end[1], dash_end[2])
                        end_idx = point_idx
                        point_idx += 1
                        
                        # Add this dash as a line segment
                        lines.InsertNextCell(2)
                        lines.InsertCellPoint(start_idx)
                        lines.InsertCellPoint(end_idx)
                        total_segments += 1
                        
                        # Move to next position
                        t = dash_end_t + current_gap
                    
                    # Move to next pattern element
                    pattern_idx = (pattern_idx + 1) % len(dash_pattern)
            else:
                # Simple pattern (dashed or dotted)
                t = 0.0
                while t < segment_len:
                    # Start of dash
                    dash_start = p1 + direction * t
                    newpoints.InsertNextPoint(dash_start[0], dash_start[1], dash_start[2])
                    start_idx = point_idx
                    point_idx += 1
                    
                    # End of dash
                    dash_end_t = min(t + dash_length, segment_len)
                    dash_end = p1 + direction * dash_end_t
                    newpoints.InsertNextPoint(dash_end[0], dash_end[1], dash_end[2])
                    end_idx = point_idx
                    point_idx += 1
                    
                    # Add this dash as a line segment
                    lines.InsertNextCell(2)
                    lines.InsertCellPoint(start_idx)
                    lines.InsertCellPoint(end_idx)
                    total_segments += 1
                    
                    # Move to next dash (skip gap)
                    t = dash_end_t + gap_length
        
        print(f"✅ Created {point_idx} points and {total_segments} dash segments for '{style}' style")
        
        # Update polydata with new dashed geometry
        self.rubber_poly.SetPoints(newpoints)
        self.rubber_poly.SetLines(lines)
        
        # Notify VTK pipeline that data changed
        self.rubber_poly.Modified()
        
        print(f"✅ Rectangle style updated to: {style}")

            
    def _create_dashed_line_geometry(self, corners, style='solid'):
        """
        Create line segments with gaps for dashed/dotted styles.
        Returns a vtkCellArray with the line segments.
        """
        import vtk
        
        lines = vtk.vtkCellArray()
        
        if style == 'solid':
            # Normal continuous line
            for i in range(len(corners) - 1):
                lines.InsertNextCell(2)
                lines.InsertCellPoint(i)
                lines.InsertCellPoint(i + 1)
            return lines
        
        new_points = vtk.vtkPoints()
        point_idx = 0
        
        # Define dash/gap ratios for each style
        if style == 'dashed':
            dash_ratio = 0.05  # 5% of segment length
            gap_ratio = 0.03   # 3% of segment length
        elif style == 'dotted':
            dash_ratio = 0.01  # 1% (tiny dashes = dots)
            gap_ratio = 0.02   # 2% gap
        elif style == 'dash-dot':
            dash_ratio = 0.05
            gap_ratio = 0.02
        elif style == 'dash-dot-dot':
            dash_ratio = 0.04
            gap_ratio = 0.015
        else:
            dash_ratio = 0.05
            gap_ratio = 0.03
        
        # Create dashed segments for each edge of the rectangle
        for i in range(len(corners) - 1):
            p1 = np.array(corners[i])
            p2 = np.array(corners[i + 1])
            
            segment_vec = p2 - p1
            segment_len = np.linalg.norm(segment_vec)
            
            if segment_len < 1e-6:
                continue
            
            direction = segment_vec / segment_len
            
            # Create dashes along this edge
            t = 0.0
            while t < segment_len:
                # Start of dash
                dash_start = p1 + direction * t
                new_points.InsertNextPoint(dash_start[0], dash_start[1], dash_start[2])
                start_idx = point_idx
                point_idx += 1
                
                # End of dash
                dash_length = dash_ratio * segment_len
                dash_end_t = min(t + dash_length, segment_len)
                dash_end = p1 + direction * dash_end_t
                new_points.InsertNextPoint(dash_end[0], dash_end[1], dash_end[2])
                end_idx = point_idx
                point_idx += 1
                
                # Add this dash as a line segment
                lines.InsertNextCell(2)

    # ---------------- CENTERLINE ----------------

    def draw_centerline(self, P1, P2):
        """Cursor-following centerline — drawn via Qt overlay (zero VTK render cost)."""
        renderer = self.app.vtk_widget.renderer
        coord = vtk.vtkCoordinate()
        coord.SetCoordinateSystemToWorld()

        coord.SetValue(P1[0], P1[1], P1[2])
        p1d = coord.GetComputedDisplayValue(renderer)

        coord.SetValue(P2[0], P2[1], P2[2])
        p2d = coord.GetComputedDisplayValue(renderer)

        # ── Qt overlay path: pure QPainter, zero VTK render cost ──────────
        overlay = self._get_qt_overlay()
        if overlay is not None:
            color_f = getattr(self.app, 'cross_line_color', (1.0, 0.0, 1.0))
            color   = tuple(int(v * 255) for v in color_f)
            overlay.set_line(
                self._vtk_display_to_qt(p1d[0], p1d[1]),
                self._vtk_display_to_qt(p2d[0], p2d[1]),
                color=color,
                width=getattr(self.app, 'cross_line_width', 3)
            )
            return

        # ✅ Throttle only the heavy VTK fallback path
        if self._should_throttle_update():
            return

        # --- VTK 2D Actor fallback ---
        if not hasattr(self, '_centerline_actor_2d') or self._centerline_actor_2d is None:
            self._init_centerline_2d()
        self._centerline_points_2d.SetPoint(0, p1d[0], p1d[1], 0.0)
        self._centerline_points_2d.SetPoint(1, p2d[0], p2d[1], 0.0)
        
        prop = self._centerline_actor_2d.GetProperty()
        color = getattr(self.app, 'cross_line_color', (1, 0, 1))
        width = getattr(self.app, 'cross_line_width', 3)
        prop.SetColor(*color)
        prop.SetLineWidth(width)
        
        style = getattr(self.app, 'cross_line_style', 'solid')
        if style == 'dashed':
            prop.SetLineStipplePattern(0xFF00)
        elif style == 'dotted':
            prop.SetLineStipplePattern(0xAAAA)
        elif style in ('dash-dot', 'dash-dot-dot'):
            prop.SetLineStipplePattern(0xE4E4)
        else:
            prop.SetLineStipplePattern(0xFFFF)
            
        self._centerline_points_2d.Modified()
        self._centerline_poly_2d.Modified()
        self._schedule_main_preview_render()
    

    def draw_rubber_rectangle(self, P1, P2, half_width):
        """✅ MICROSTATION METHOD: 2D overlay actor - always perfect rectangle"""
       
        # ============================================================
        # ✅ USE 2D OVERLAY ACTOR (screen space, not world space)
        # ============================================================
       
        # Convert P1, P2 from world to VTK display coordinates (screen pixels)
        renderer = self.app.vtk_widget.renderer
        coord = vtk.vtkCoordinate()
        coord.SetCoordinateSystemToWorld()

        coord.SetValue(P1[0], P1[1], P1[2])
        p1_display = coord.GetComputedDisplayValue(renderer)

        coord.SetValue(P2[0], P2[1], P2[2])
        p2_display = coord.GetComputedDisplayValue(renderer)

        p1_screen = np.array([p1_display[0], p1_display[1]], dtype=np.float64)
        p2_screen = np.array([p2_display[0], p2_display[1]], dtype=np.float64)

        vec = p2_screen - p1_screen
        length = np.linalg.norm(vec)
        if length < 1e-6:
            return

        dir_vec = vec / length
        perp = np.array([-dir_vec[1], dir_vec[0]], dtype=np.float64)

        world_dist = np.linalg.norm(P2[:2] - P1[:2])
        # ✅ FIX: Enforce a minimum screen-space half-width (e.g., 2 pixels) so 
        #    the box is always visible even when dragging very fast.
        hw_screen = float(half_width) * (length / world_dist) if world_dist > 1e-6 else 10.0
        hw_screen = max(hw_screen, 2.0) 
        
        offset = perp * hw_screen

        c1 = p1_screen + offset
        c2 = p2_screen + offset
        c3 = p2_screen - offset
        c4 = p1_screen - offset

        # ── Qt overlay path: pure QPainter, zero VTK render cost ──────────
        overlay = self._get_qt_overlay()
        if overlay is not None:
            color_f = getattr(self.app, 'cross_line_color', (1.0, 0.0, 1.0))
            color   = tuple(int(v * 255) for v in color_f)
            width   = int(getattr(self.app, 'cross_line_width', 2))
            style   = getattr(self.app, 'cross_line_style', 'solid')

            overlay.set_preview(
                self._vtk_display_to_qt(*p1_screen),
                self._vtk_display_to_qt(*p2_screen),
                [self._vtk_display_to_qt(*c1),
                 self._vtk_display_to_qt(*c2),
                 self._vtk_display_to_qt(*c3),
                 self._vtk_display_to_qt(*c4)],
                color=color, width=width, style=style,
            )
            self.P1, self.P2, self.half_width = P1, P2, half_width
            return

        # ✅ Throttle only the heavy VTK fallback path
        if self._should_throttle_update():
            return

        # ── VTK 2D actor fallback ───
        style = getattr(self.app, 'cross_line_style', 'solid')
        if not hasattr(self, '_rubber_actor_2d') or self._rubber_actor_2d is None:
            self._init_rectangle_2d()

        corners_display = [c1, c2, c3, c4, c1]
        for i, (x, y) in enumerate(corners_display):
            self._rubber_points_2d.SetPoint(i, float(x), float(y), 0.0)
            
        prop = self._rubber_actor_2d.GetProperty()
        color = getattr(self.app, 'cross_line_color', (1, 0, 1))
        width = getattr(self.app, 'cross_line_width', 3)
        prop.SetColor(*color)
        prop.SetLineWidth(width)
        
        if style == 'dashed':
            prop.SetLineStipplePattern(0xFF00)
        elif style == 'dotted':
            prop.SetLineStipplePattern(0xAAAA)
        elif style in ('dash-dot', 'dash-dot-dot'):
            prop.SetLineStipplePattern(0xE4E4)
        else:
            prop.SetLineStipplePattern(0xFFFF)
            
        self._rubber_points_2d.Modified()
        self._rubber_poly_2d.Modified()
        self._schedule_main_preview_render()

        self.P1, self.P2, self.half_width = P1, P2, half_width


    def _create_dashed_rectangle_2d(self, corners, style):
        """Create dashed/dotted geometry for 2D rectangle in screen space"""
        import vtk
        import numpy as np
        
        # Define dash/gap lengths in SCREEN PIXELS
        if style == 'dashed':
            dash_length = 10.0  # pixels
            gap_length = 5.0
        elif style == 'dotted':
            dash_length = 2.0
            gap_length = 4.0
        elif style == 'dash-dot':
            dash_pattern = [(10.0, 5.0), (2.0, 5.0)]
        elif style == 'dash-dot-dot':
            dash_pattern = [(10.0, 5.0), (2.0, 4.0), (2.0, 5.0)]
        else:
            dash_length = 10.0
            gap_length = 5.0
        
        new_points = vtk.vtkPoints()
        lines = vtk.vtkCellArray()
        point_idx = 0
        
        # Create dashed segments for each edge
        for edge_idx in range(len(corners) - 1):
            p1 = np.array(corners[edge_idx], dtype=np.float64)
            p2 = np.array(corners[edge_idx + 1], dtype=np.float64)
            
            edge_vec = p2 - p1
            edge_len = np.linalg.norm(edge_vec)
            
            if edge_len < 1e-6:
                continue
            
            direction = edge_vec / edge_len
            
            if style in ['dash-dot', 'dash-dot-dot']:
                # Complex pattern
                t = 0.0
                pattern_idx = 0
                while t < edge_len:
                    current_dash, current_gap = dash_pattern[pattern_idx]
                    
                    # Dash segment
                    dash_start = p1 + direction * t
                    new_points.InsertNextPoint(dash_start[0], dash_start[1], 0.0)
                    start_idx = point_idx
                    point_idx += 1
                    
                    dash_end_t = min(t + current_dash, edge_len)
                    dash_end = p1 + direction * dash_end_t
                    new_points.InsertNextPoint(dash_end[0], dash_end[1], 0.0)
                    end_idx = point_idx
                    point_idx += 1
                    
                    # Add line segment
                    lines.InsertNextCell(2)
                    lines.InsertCellPoint(start_idx)
                    lines.InsertCellPoint(end_idx)
                    
                    # Next position
                    t = dash_end_t + current_gap
                    pattern_idx = (pattern_idx + 1) % len(dash_pattern)
            else:
                # Simple dashed/dotted
                t = 0.0
                while t < edge_len:
                    # Dash segment
                    dash_start = p1 + direction * t
                    new_points.InsertNextPoint(dash_start[0], dash_start[1], 0.0)
                    start_idx = point_idx
                    point_idx += 1
                    
                    dash_end_t = min(t + dash_length, edge_len)
                    dash_end = p1 + direction * dash_end_t
                    new_points.InsertNextPoint(dash_end[0], dash_end[1], 0.0)
                    end_idx = point_idx
                    point_idx += 1
                    
                    # Add line segment
                    lines.InsertNextCell(2)
                    lines.InsertCellPoint(start_idx)
                    lines.InsertCellPoint(end_idx)
                    
                    # Next dash
                    t = dash_end_t + gap_length
        
        return new_points, lines


    def _init_rectangle_2d(self):
        """Initialize 2D overlay actor (screen space)"""
        import vtk
       
        # Remove old 3D actor if exists
        if hasattr(self, 'rubber_actor') and self.rubber_actor:
            try:
                self.app.vtk_widget.renderer.RemoveActor(self.rubber_actor)
            except Exception:
                pass
            self.rubber_actor = None
       
        # Create 2D points
        self._rubber_points_2d = vtk.vtkPoints()
        self._rubber_points_2d.SetNumberOfPoints(5)
        for i in range(5):
            self._rubber_points_2d.SetPoint(i, 0.0, 0.0, 0.0)
       
        # Create polydata
        self._rubber_poly_2d = vtk.vtkPolyData()
        self._rubber_poly_2d.SetPoints(self._rubber_points_2d)
       
        # Create line cells (initial solid topology)
        lines = vtk.vtkCellArray()
        for i in range(4):
            lines.InsertNextCell(2)
            lines.InsertCellPoint(i)
            lines.InsertCellPoint(i + 1)
        self._rubber_poly_2d.SetLines(lines)
       
        # Mapper for 2D
        mapper = vtk.vtkPolyDataMapper2D()
        mapper.SetInputData(self._rubber_poly_2d)
       
        # ✅ CRITICAL: Use Actor2D (renders in SCREEN SPACE)
        self._rubber_actor_2d = vtk.vtkActor2D()
        self._rubber_actor_2d.SetMapper(mapper)
       
        # Set color and width
        color = getattr(self.app, 'cross_line_color', (1, 0, 1))
        width = getattr(self.app, 'cross_line_width', 3)
       
        prop = self._rubber_actor_2d.GetProperty()
        prop.SetColor(*color)
        prop.SetLineWidth(width)
       
        # ✅ Add to renderer (2D overlay)
        self.app.vtk_widget.renderer.AddActor2D(self._rubber_actor_2d)
       
        print("✅ 2D overlay rectangle initialized")
 
    def _remove_actor_from_all_renderers(self, actor, is_2d=False):
        if not actor:
            return
 
        renderers = []
 
        # Main renderer
        if hasattr(self.app, "vtk_widget"):
            renderers.append(self.app.vtk_widget.renderer)
 
        # Active section view renderer
        try:
            vtk_widget = self._get_active_vtk()
            if vtk_widget:
                renderers.append(vtk_widget.renderer)
        except Exception:
            pass
 
        # Digitize / picked renderer (CRITICAL)
        if hasattr(self.app, "digitize_manager"):
            ren = getattr(self.app.digitize_manager, "renderer", None)
            if ren:
                renderers.append(ren)
 
        # Remove from all
        for ren in set(renderers):
            try:
                if is_2d:
                    ren.RemoveActor2D(actor)
                else:
                    ren.RemoveActor(actor)
            except Exception:
                pass
 
 
    #Added by bala
    def finalize_rectangle(self):
        """Remove preview rubber band (Qt overlay + any legacy VTK actors)."""

        # Clear the Qt overlay first (instant, no render needed)
        overlay = getattr(self, '_qt_overlay', None)
        if overlay is not None:
            try:
                overlay.clear()
            except Exception:
                pass

        # Remove 3D rubber actor (older code path)
        needs_render = False
        if hasattr(self, '_centerline_actor_2d') and self._centerline_actor_2d:
            self._remove_actor_from_all_renderers(self._centerline_actor_2d, is_2d=True)
            self._centerline_actor_2d = None
            needs_render = True

        if hasattr(self, '_rubber_actor_2d') and self._rubber_actor_2d:
            self._remove_actor_from_all_renderers(self._rubber_actor_2d, is_2d=True)
            self._rubber_actor_2d = None
            needs_render = True

        if self.rubber_actor:
            self._remove_actor_from_all_renderers(self.rubber_actor, is_2d=False)
            self.rubber_actor = None
            needs_render = True

        if needs_render:
            self.app.vtk_widget.render()
            
        # Force an instant repaint of the UI to make the Qt overlay vanish 
        # BEFORE the heavy cross-section processing blocks the main thread
        from PySide6.QtCore import QCoreApplication, QEventLoop
        QCoreApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
        
        print("✅ Rectangle finalized (removed from ALL renderers)")


    def _filter_points_by_visibility(self, points, point_indices, view_index):
        """
        ✅ FIXED: Filter points based on CURRENT visibility from Display Mode,
        not the palette state from when the cross-section was first created.
        
        Args:
            points: Section points array (N, 3)
            point_indices: Indices into main point cloud for these points
            view_index: Which cross-section view (0, 1, 2, ...)
        
        Returns:
            Filtered points array containing only visible class points
        """
        try:
            import numpy as np
            
            # ✅ CRITICAL FIX: Get CURRENT palette from Display Mode dialog
            # This ensures we use the up-to-date visibility settings, not stale data
            current_palette = None
            
            if hasattr(self.app, 'display_mode_dialog') and self.app.display_mode_dialog:
                dialog = self.app.display_mode_dialog
                
                # Get palette for THIS specific view (slot = view_index + 1)
                target_slot = view_index + 1  # View 0 = Slot 1, View 1 = Slot 2, etc.
                
                if hasattr(dialog, 'view_palettes') and target_slot in dialog.view_palettes:
                    current_palette = dialog.view_palettes[target_slot]
                    print(f"   🔍 Camera fit: Using Display Mode palette for View {view_index + 1}")
            
            # Fallback to app palette if dialog not available
            if current_palette is None:
                current_palette = self._get_view_palette(view_index)
            
            if not current_palette:
                # No palette = show all
                print(f"   📊 No palette filter for View {view_index + 1} - using all points")
                return points
            
            # Get CURRENTLY visible class codes
            visible_classes = [code for code, info in current_palette.items() if info.get('show', True)]
            
            if len(visible_classes) == len(current_palette):
                # All classes visible = no filtering needed
                print(f"   📊 All {len(current_palette)} classes visible - using all points")
                return points
            
            if len(visible_classes) == 0:
                # Nothing visible = return empty array
                print(f"   ⚠️ No visible classes - returning empty point set")
                return np.empty((0, 3), dtype=points.dtype)
            
            # Get classifications for these specific points
            if not hasattr(self.app, 'data') or 'classification' not in self.app.data:
                return points
            
            classifications = self.app.data['classification'][point_indices]
            
            # Create visibility mask
            visible_mask = np.isin(classifications, visible_classes)
            
            # Filter points
            filtered_points = points[visible_mask]
            
            hidden_count = len(points) - len(filtered_points)
            hidden_classes = [c for c in np.unique(classifications) if c not in visible_classes]
            
            if hidden_count > 0:
                print(f"   🔍 Camera fit: Excluding {hidden_count} points from hidden classes")
                print(f"      Visible classes: {visible_classes}")
            
            # Safety: if all points filtered out, return original
            if len(filtered_points) == 0:
                print(f"   ⚠️ All points filtered - using original points for camera fit")
                return points
            
            return filtered_points
            
        except Exception as e:
            print(f"   ⚠️ Visibility filter failed: {e} - using all points")
            import traceback
            traceback.print_exc()
            return points


    def finalize_section(self, P1, P2):
                """
                ✅ MICROSTATION METHOD:
                - Width = ONLY what user dragged (half_width)
                - Buffer extends LENGTH (along the line) only
                ✅ FIXED:
                - Stores section-local transformed coordinates (X=along, Y=across, Z=elev)
                    so CUT SECTION works again
                - Creates the selected cross-section dock on-demand
                - Keeps world-point copies for debugging / future needs
                """
    
                import numpy as np
    
                # ---------------- SAFETY ----------------
                if self.half_width is None:
                    print("⚠️ Half width not set")
                    return
    
                if not hasattr(self.app, 'data') or self.app.data is None or 'xyz' not in self.app.data:
                    print("❌ No point cloud data loaded!")
                    self.finalize_rectangle()
                    return
    
                xyz = self.app.data["xyz"]
                if xyz is None or len(xyz) == 0:
                    print("❌ Point cloud data is empty!")
                    self.finalize_rectangle()
                    return

                # New section geometry replaces the old one for this view — drop any
                # stale cross-section measurement overlays drawn against the previous
                # cut. No-op unless that (opt-in) tool has been used.
                cs_measure = getattr(self.app, "cross_section_measurement_tool", None)
                if cs_measure is not None:
                    try:
                        cs_measure.clear_view(self.active_view)
                    except Exception:
                        pass

                # Apply line style before finalizing rectangle
                style = getattr(self.app, "cross_line_style", "solid")
                if style != "solid":
                    print(f"🎨 Applying {style} style to rectangle before finalization")
                    if hasattr(self, 'update_rectangle_style'):
                        try:
                            self.update_rectangle_style()
                            self.app.vtk_widget.render()
                            print(f"✅ Style applied: {style}")
                        except Exception as e:
                            print(f"⚠️ Failed to apply style: {e}")
    
                # Remove preview rectangle
                self.finalize_rectangle()
    
                # ---------------- GEOMETRY ----------------
                v = P2[:2] - P1[:2]
                length = float(np.linalg.norm(v))
                if length < 1e-9:
                    print("❌ Invalid section line (zero length)")
                    return
    
                dir_vec = v / length
                perp = np.array([-dir_vec[1], dir_vec[0]], dtype=np.float64)
    
                buffer = float(getattr(self.app, "section_buffer", 2.0))
    
                print(f"✅ Cross-section computed:")
                print(f"   Line: P1={P1[:2]}, P2={P2[:2]}")
                print(f"   Length: {length:.2f}m")
                print(f"   Core width: ±{self.half_width:.2f}m (user drag)")
                print(f"   Buffer depth: {buffer:.2f}m (length extension only)")
    
                half_w   = float(self.half_width)
                n_points = len(xyz)

                # ── STEP 1: AABB pre-filter — one boolean pass on all N points (Memory Optimized) ──
                # Reduces 70M+ candidates to the tight section bounding box before
                # the expensive dot-product projections.
                margin_x = half_w * abs(perp[0]) + (length + buffer) * abs(dir_vec[0])
                margin_y = half_w * abs(perp[1]) + (length + buffer) * abs(dir_vec[1])
                bbox_min_x = min(P1[0], P2[0]) - margin_x
                bbox_max_x = max(P1[0], P2[0]) + margin_x
                bbox_min_y = min(P1[1], P2[1]) - margin_y
                bbox_max_y = max(P1[1], P2[1]) + margin_y

                # ✅ OPTIMIZATION: Filter in-place on xyz to avoid allocating large temp arrays (_x, _y)
                bbox_mask = (xyz[:, 0] >= bbox_min_x)
                bbox_mask &= (xyz[:, 0] <= bbox_max_x)
                bbox_mask &= (xyz[:, 1] >= bbox_min_y)
                bbox_mask &= (xyz[:, 1] <= bbox_max_y)
                
                candidate_idx = np.flatnonzero(bbox_mask)
                del bbox_mask

                print(f"   AABB filter: {len(candidate_idx):,} / {n_points:,} candidates "
                      f"({100.0 * len(candidate_idx) / max(n_points, 1):.1f}%)")

                if len(candidate_idx) == 0:
                    print("⚠️ No points found in cross-section")
                    return

                # ── STEP 2: Exact geometry — only on the candidate subset (float64 centering) ──
                # ✅ PERFORMANCE: Only perform float64 subtraction on the small candidate subset.
                # ✅ PRECISION: Center in float64 BEFORE casting to float32 to fix the 'scattering'.
                # Keep the inclusion test in float64. Reducing these centred
                # values and unit vectors to float32 can move points across the
                # section boundary. Only the final display coordinates are cast
                # to float32 below, preserving the existing rendering contract.
                cand_xy = np.asarray(xyz[candidate_idx, :2], dtype=np.float64)
                rel = cand_xy - np.asarray(P1[:2], dtype=np.float64)

                along_c = rel @ dir_vec
                across_c = rel @ perp
                del rel, cand_xy

                core_local = (
                    (along_c >= 0.0) & (along_c <= length) &
                    (np.abs(across_c) <= half_w)
                )
                buffer_local = (
                    (along_c >= -buffer) & (along_c <= length + buffer) &
                    (np.abs(across_c) <= half_w) &
                    (~core_local)
                )

                # Build index vectors directly from candidate subset.
                core_indices_full = candidate_idx[core_local]
                buffer_indices_full = candidate_idx[buffer_local]

                core_count  = int(len(core_indices_full))
                buf_count   = int(len(buffer_indices_full))
                total_count = core_count + buf_count

                print(f"   Core points: {core_count}")
                print(f"   Buffer points: {buf_count}")

                if total_count == 0:
                    del along_c, across_c
                    print("⚠️ No points found in cross-section")
                    return

                # ── BUILD SECTION-LOCAL POINTS (float32) ──
                # Coordinate system: X=along (0..length), Y=across (±half_w), Z=elev.
                # Extract filtered coords from candidate arrays, then free them
                _ca = along_c[core_local];   _cb = across_c[core_local]
                _ba = along_c[buffer_local]; _bb = across_c[buffer_local]
                del along_c, across_c, core_local, buffer_local, candidate_idx

                core_points_local = np.column_stack([
                    _ca, _cb, xyz[core_indices_full, 2]
                ]).astype(np.float32, copy=False)
                del _ca, _cb

                buffer_points_local = np.column_stack([
                    _ba, _bb, xyz[buffer_indices_full, 2]
                ]).astype(np.float32, copy=False)
                del _ba, _bb

                # Adaptive cap to prevent RAM/VRAM spikes on very large sections.
                (
                    core_points_local,
                    buffer_points_local,
                    core_indices,
                    buffer_indices,
                    cap_stats,
                ) = self._cap_section_points(
                    core_points_local,
                    buffer_points_local,
                    core_indices_full,
                    buffer_indices_full,
                )

                if cap_stats.get("downsampled", False):
                    print(
                        "   ⚡ Section cap applied: "
                        f"{cap_stats['total_before']:,} -> {cap_stats['total_after']:,} "
                        f"(budget={cap_stats['budget']:,}, "
                        f"core {cap_stats['core_before']:,}->{cap_stats['core_after']:,}, "
                        f"buffer {cap_stats['buffer_before']:,}->{cap_stats['buffer_after']:,})"
                    )
                    try:
                        if hasattr(self.app, "statusBar"):
                            self.app.statusBar().showMessage(
                                "Cross-section sampled for performance "
                                f"({cap_stats['total_after']:,}/{cap_stats['total_before']:,}). "
                                "Set NAKSHA_SECTION_POINT_BUDGET to increase.",
                                6000
                            )
                    except Exception:
                        pass

                # Build masks for retained subset.
                core_mask = np.zeros(n_points, dtype=bool)
                buffer_mask = np.zeros(n_points, dtype=bool)
                if len(core_indices) > 0:
                    core_mask[core_indices] = True
                if len(buffer_indices) > 0:
                    buffer_mask[buffer_indices] = True
                full_mask = core_mask | buffer_mask

                if len(buffer_points_local) > 0:
                    section_indices = np.concatenate([core_indices, buffer_indices])
                    all_points_local = np.vstack([core_points_local, buffer_points_local])
                else:
                    section_indices = core_indices
                    all_points_local = core_points_local

                # World-coordinate copies eliminated — use indices for on-demand access.
                # (xyz[section_indices] if ever needed downstream)

                # ---------------- STORE PER-VIEW DATA ----------------
                view_index = int(getattr(self, "active_view", 0))
    
                setattr(self.app, f'section_{view_index}_P1', P1)
                setattr(self.app, f'section_{view_index}_P2', P2)
                setattr(self.app, f'section_{view_index}_half_width', float(self.half_width))
    
                # ✅ Store LOCAL points (used for plotting + picking + cut-section workflow)
                setattr(self.app, f'section_{view_index}_core_points', core_points_local)
                setattr(self.app, f'section_{view_index}_buffer_points', buffer_points_local)
    
                setattr(self.app, f'section_{view_index}_core_mask', core_mask)
                setattr(self.app, f'section_{view_index}_buffer_mask', buffer_mask)
                setattr(self.app, f'section_{view_index}_core_indices', core_indices)
                setattr(self.app, f'section_{view_index}_buffer_indices', buffer_indices)
                setattr(self.app, f'section_{view_index}_indices', section_indices)
                # Keep original (pre-cap) index vectors for diagnostics/future tools.
                setattr(self.app, f'section_{view_index}_full_core_indices', core_indices_full)
                setattr(self.app, f'section_{view_index}_full_buffer_indices', buffer_indices_full)
    
                # ✅ These are what CutSectionController reads
                setattr(self.app, f'section_{view_index}_points_transformed', all_points_local)
                setattr(self.app, f'section_{view_index}_combined_mask', full_mask)
    
                print(f"💾 Stored section data for View {view_index + 1}")
                print(f"   ✅ Transformed coordinates stored for cut section ({len(all_points_local)} points)")
    
                # ---------------- STORE GLOBAL (BACKWARD COMPAT) ----------------
                # Many parts of your app re-use these globals
                self.app.section_core_points = core_points_local
                self.app.section_buffer_points = buffer_points_local
                self.app.section_core_mask = core_mask
                self.app.section_core_indices = core_indices
                self.app._last_drawn_view_idx = view_index   # ← track ownership
                self.app.section_indices = section_indices
    
                self.last_mask = full_mask
                self.app.section_points = all_points_local  # ✅ IMPORTANT: section_points should be LOCAL in cross-section context
    
                # ---------------- ENSURE DOCK EXISTS ----------------
                if not hasattr(self.app, 'section_vtks') or view_index not in self.app.section_vtks:
                    print(f"🔨 Creating View {view_index + 1} dock on-demand...")
                    if hasattr(self.app, '_open_specific_cross_section_view'):
                        self.app._open_specific_cross_section_view(view_index)
                        print(f"✅ View {view_index + 1} dock created")
                    else:
                        print("❌ Cannot create dock - _open_specific_cross_section_view not found")
                        return
                if view_index in self.app.section_docks:
                    dock = self.app.section_docks[view_index]
                
                    # Restore from minimized state
                    if dock.isMinimized():
                        dock.showNormal()
                
                    # Make visible if hidden
                    if not dock.isVisible():
                        dock.show()
                
                    # Bring to front and activate
                    dock.raise_()
                    dock.activateWindow()
                
                    print(f"✨ Auto-displayed Cross Section View {view_index + 1}")    
    
                # ---------------- PLOT (LOCAL POINTS) ----------------
                # ✅ CRITICAL FIX: Disable camera sync during initial rendering
                # This prevents the initial plot + zoom from triggering a sync cascade
                prev_syncing = getattr(self.app, '_syncing_camera', False)
                self.app._syncing_camera = True
    
                # Clear locate rubber-band before plotting the new section
                self.clear_locate_state()

                self._plot_section(
                    core_points_local,
                    buffer_points_local,
                    view=getattr(self.app, "cross_view_mode", "front")
                )
    
                # # ---------------- FORCE ZOOM TO CORE ----------------
                # # Fixes "view zoomed out/down" issue by ignoring buffer points for camera setup
                #         # 1. Calculate bounds of ONLY the core (user-selected) points
                #         xmin, ymin, zmin = core_points_local.min(axis=0)
                #         xmax, ymax, zmax = core_points_local.max(axis=0)

                #         # 2. Force 2D orthographic camera BEFORE ResetCamera
                #         # Cross section local coords: X=along, Y=across, Z=elevation
                #         # We look along the Y axis so we see X (along) vs Z (elevation)
                #         camera.ParallelProjectionOn()
                #         camera.SetPosition(xc, -y_dist, zc)
                #         camera.SetFocalPoint(xc, 0.0, zc)
                #         camera.SetViewUp(0.0, 0.0, 1.0)

                #         # 3. Reset camera to fit the XZ data bounds (along vs elevation)
                #         vtk_widget.renderer.ResetCamera(bounds)

                #         # 4. Zoom out slightly (5% margin) so points aren't touching edges
                #         camera.Zoom(0.95)

                #         vtk_widget.renderer.ResetCameraClippingRange()
                #         vtk_widget.render()

                # ---------------- FIT CAMERA ----------------
                try:
                    vtk_widget = self._get_active_vtk()
                    
                    # ✅ FIX: Filter points by CURRENT visibility before camera fit
                    # This ensures hidden classes (like Class 51) don't affect the initial view bounds
                    visible_points_for_fit = self._filter_points_by_visibility(
                        core_points_local, 
                        core_indices,
                        view_index
                    )
                    
                    if len(visible_points_for_fit) > 0:
                        self._fit_camera_to_section_points(vtk_widget, visible_points_for_fit)
                        print(f"   📷 Initial camera fit to {len(visible_points_for_fit)}/{len(core_points_local)} visible points")
                    else:
                        # Fallback if filter returned empty
                        self._fit_camera_to_section_points(vtk_widget, core_points_local)
                        print(f"   ⚠️ Filter returned empty - using all {len(core_points_local)} points")
                    
                except Exception as e:
                    print(f"   ⚠️ Camera fit failed: {e}")
                    import traceback
                    traceback.print_exc()
    
                # ════════════════════════════════════════════════════════════════════════════════════
                # ✅ AUTO-APPLY: Isolated palette to THIS VIEW ONLY (NOT main view)
                # ════════════════════════════════════════════════════════════════════════════════════
    
                try:
                    if hasattr(self.app, 'display_mode_dialog') and self.app.display_mode_dialog is not None:
                        dialog = self.app.display_mode_dialog
                        target_slot = view_index + 1  # View 0 = slot 1, View 1 = slot 2, etc.
                    
                        if hasattr(dialog, 'view_palettes') and target_slot in dialog.view_palettes:
                            view_palette = dialog.view_palettes[target_slot]
                        
                            print(f" 📋 Using view_palettes[{target_slot}] for View {view_index + 1}")
                            print(f" 📊 Palette has {len(view_palette)} classes")

                            if hasattr(self.app, "_debug_log_cross_section_palette_state"):
                                self.app._debug_log_cross_section_palette_state(
                                    context=f"pre-plot-auto-apply v{view_index + 1}",
                                    target_class=getattr(self.app, "_last_classified_to_class", None),
                                    gpu_slot=target_slot,
                                )
                        
                            # ✅ CRITICAL: Call ISOLATED apply (only affects THIS view, not main)
                            self._auto_apply_view_palette(view_index, view_palette)

                            if hasattr(self.app, "_debug_log_cross_section_palette_state"):
                                self.app._debug_log_cross_section_palette_state(
                                    context=f"post-plot-auto-apply v{view_index + 1}",
                                    target_class=getattr(self.app, "_last_classified_to_class", None),
                                    gpu_slot=target_slot,
                                )
                        
                            print(f" ✅ Auto-apply complete for View {view_index + 1}")
                        else:
                            print(f" ⚠️ No view-specific palette for slot {target_slot}")
                    else:
                        print(f" ⚠️ Display Mode dialog not open - skipping auto-apply")
                    
                except Exception as e:
                    print(f" ⚠️ Auto-apply failed: {e}")
                    import traceback
                    traceback.print_exc()
    
                finally:
                    print(f"{'='*60}\n")

                # ═══════════════════════════════════════════════════════════
                # View Context Invalidation — flush stale classifier state
                # (_plot_section already built the unified actor above;
                #  no second build needed here)
                # ═══════════════════════════════════════════════════════════
                
                try:
                    # Invalidate classify interactor coord cache for this view
                    if hasattr(self.app, 'classify_interactors'):
                        interactor = self.app.classify_interactors.get(view_index)
                        if interactor and hasattr(interactor, '_invalidate_coord_cache'):
                            interactor._invalidate_coord_cache()
                            print(f"   🔄 View {view_index + 1}: coord cache invalidated")

                    # No second sync needed here.
                except ImportError:
                    pass
                except Exception as e:
                    print(f"   ⚠️ View context invalidation failed: {e}")

                # ✅ FIX: Restore _syncing_camera flag (was never restored!)
                self.app._syncing_camera = prev_syncing

                # ✅ FIX: Force 2D camera as FINAL step
                try:
                    vtk_widget = self._get_active_vtk()
                    if vtk_widget:
                        self._force_section_camera_2d(vtk_widget, view_index)
                except Exception as e:
                    print(f"   ⚠️ Final 2D camera enforcement failed: {e}")

    ##NEWWW

    def _force_section_camera_2d(self, vtk_widget, view_index=None):
            """
            Force a cross-section view's camera into proper 2D orientation.
            Safe to call at any time; no actors are modified.
            """
            if vtk_widget is None:
                return
            try:
                # Install/refresh 2D camera lock on the camera
                try:
                    from .interactor_classify import install_camera_2d_lock
                    is_cut = (getattr(self.app, "active_mode", None) == "cut")
                    install_camera_2d_lock(vtk_widget, self.app, is_cut)
                except Exception as e:
                    print(f"   ⚠️ Lock install in _force_section_camera_2d failed: {e}")

                ren = vtk_widget.renderer
                if ren is None:
                    return
                cam = ren.GetActiveCamera()
                if cam is None:
                    return

                if not cam.GetParallelProjection():
                    cam.ParallelProjectionOn()
                    print(f"   🔧 Forced ParallelProjection ON for View {(view_index or 0) + 1}")

                up = cam.GetViewUp()
                if abs(up[0]) > 0.01 or abs(up[1]) > 0.01 or abs(up[2] - 1.0) > 0.01:
                    cam.SetViewUp(0.0, 0.0, 1.0)
                    print(f"   🔧 Fixed ViewUp drift {up} → (0, 0, 1) for View {(view_index or 0) + 1}")

                view_mode = getattr(self.app, 'cross_view_mode', 'side')
                pos = cam.GetPosition()
                fp = cam.GetFocalPoint()

                if view_mode == 'side':
                    if abs(pos[1] - fp[1]) < 1.0:
                        stand_off = max(abs(cam.GetParallelScale()) * 10.0, 100.0)
                        cam.SetPosition(fp[0], fp[1] - stand_off, fp[2])
                        print(f"   🔧 Fixed camera standoff for side view")
                else:
                    if abs(pos[0] - fp[0]) < 1.0:
                        stand_off = max(abs(cam.GetParallelScale()) * 10.0, 100.0)
                        cam.SetPosition(fp[0] - stand_off, fp[1], fp[2])
                        print(f"   🔧 Fixed camera standoff for front view")

                ren.ResetCameraClippingRange()

            except Exception as e:
                print(f"   ⚠️ _force_section_camera_2d failed: {e}")

    ##NEWWW
    def _fit_camera_to_section_points(self, vtk_widget, points, padding_fraction=0.05):
        """
        ✅ FIXED: Fits camera to include EVERY SINGLE POINT (no outlier filtering).
        
        Args:
            vtk_widget: The VTK widget to update
            points: Section points array (N, 3) - local coordinates
            padding_fraction: Extra margin around data (default 5% = 0.05)
        """
        if vtk_widget is None or points is None or len(points) == 0:
            return

        try:
            import numpy as np

            ren = vtk_widget.renderer
            cam = ren.GetActiveCamera()

            # ── 1. Axis mapping ──────────────────────────────────────────────
            # side  → displays X (col 0, along)  vs Z  — camera looks along -Y
            # front → displays Y (col 1, across) vs Z  — camera looks along -X
            view_mode = getattr(self.app, 'cross_view_mode', 'side')

            if view_mode == 'side':
                h_col   = 0          # X = along-section (0 → length)
                h_label = 'X'
            else:                    # front
                h_col   = 1          # Y = perpendicular (±half_width)
                h_label = 'Y'
                # front view camera looks along X, so we need the X midpoint
                x_mid = float((points[:, 0].min() + points[:, 0].max()) / 2.0)

            # ── 2. Window aspect ratio ───────────────────────────────────────
            try:
                win_w, win_h = vtk_widget.GetRenderWindow().GetSize()
            except Exception:
                win_w, win_h = 0, 0
            if win_w < 10 or win_h < 10:
                try:
                    win_w, win_h = vtk_widget.width(), vtk_widget.height()
                except Exception:
                    win_w, win_h = 900, 450
            if win_w < 10 or win_h < 10:
                win_w, win_h = 900, 450
            aspect = win_w / win_h

            # ═══════════════════════════════════════════════════════════════════
            # ✅ FIX: Use ACTUAL min/max to include ALL points (no percentile filtering)
            # ═══════════════════════════════════════════════════════════════════
            h_min = float(points[:, h_col].min())  # ✅ TRUE minimum
            h_max = float(points[:, h_col].max())  # ✅ TRUE maximum
            
            z_min = float(points[:, 2].min())      # ✅ TRUE minimum elevation
            z_max = float(points[:, 2].max())      # ✅ TRUE maximum elevation

            print(f"   📏 BOUNDS ({len(points)} points): {h_label}=[{h_min:.2f}, {h_max:.2f}] Z=[{z_min:.2f}, {z_max:.2f}]")

            # ── 3. Minimum range guard ───────────────────────────────────────
            h_range = max(h_max - h_min, 1.0)
            z_range = max(z_max - z_min, 1.0)

            # ── 4. Padding — configurable margin (default 5%) ────────────────
            padded_h = h_range * (1.0 + 2.0 * padding_fraction)
            padded_z = z_range * (1.0 + 2.0 * padding_fraction)

            # ── 5. Display centre ────────────────────────────────────────────
            h_center = (h_min + h_max) / 2.0
            z_center = (z_min + z_max) / 2.0

            # ── 6. Aspect-correct parallel scale ─────────────────────────────
            scale_from_h = (padded_h / 2.0) / aspect
            scale_from_z =  padded_z / 2.0
            parallel_scale = max(scale_from_h, scale_from_z)
            parallel_scale = max(parallel_scale, max(h_range, z_range) * 0.20)

            # ── 7. Camera placement ─────────────────────────────────────────
            stand_off = max(padded_h, padded_z) * 10.0 + 100.0

            cam.ParallelProjectionOn()
            cam.SetViewUp(0.0, 0.0, 1.0)
            cam.SetParallelScale(parallel_scale)

            if view_mode == 'side':
                # Looking along -Y → sees X (horizontal) vs Z (vertical)
                cam.SetFocalPoint(h_center, 0.0,        z_center)
                cam.SetPosition(  h_center, -stand_off, z_center)
            else:
                # Looking along -X → sees Y (horizontal) vs Z (vertical)
                cam.SetFocalPoint(x_mid,            h_center, z_center)
                cam.SetPosition(  x_mid - stand_off, h_center, z_center)

            ren.ResetCameraClippingRange()
            self._install_2d_camera_guard(vtk_widget, cam, ren)
            vtk_widget.render()

            print(
                f"   📷 Camera fit [{view_mode}]: "
                f"{h_label}[{h_min:.2f}~{h_max:.2f}] "
                f"Z[{z_min:.2f}~{z_max:.2f}] "
                f"scale={parallel_scale:.3f} aspect={aspect:.2f} "
                f"padding={padding_fraction*100:.0f}%"
            )

        except Exception as e:
            print(f"   ⚠️ _fit_camera_to_section_points failed: {e}")
            try:
                vtk_widget.reset_camera()
                vtk_widget.render()
            except Exception:
                pass

    def _install_2d_camera_guard(self, vtk_widget, cam, ren):
        """
        Install a lightweight camera ModifiedEvent observer that corrects any
        accidental 3D rotation while leaving pan and parallel-scale (zoom) intact.

        Captures the current view direction from cam (which is freshly set to a
        known-good 2D orientation by _fit_camera_to_section_points) and enforces
        it on every subsequent camera change.

        Safe to call repeatedly — removes the previous observer on the same
        vtk_widget before installing a new one.
        """
        import numpy as np

        # Remove any previous guard installed for this widget
        prev_id = getattr(vtk_widget, '_section_2d_guard_obs_id', None)
        if prev_id is not None:
            try:
                cam.RemoveObserver(prev_id)
            except Exception:
                pass
            vtk_widget._section_2d_guard_obs_id = None

        # Snapshot the correct 2D view direction from the just-positioned camera
        locked_dir = np.array(cam.GetDirectionOfProjection(), dtype=float)
        locked_up  = np.array([0.0, 0.0, 1.0])   # Z-up is always correct for cross-sections

        _busy = [False]

        def _guard(obj, event):
            if _busy[0]:
                return
            _busy[0] = True
            try:
                current_dir = np.array(cam.GetDirectionOfProjection(), dtype=float)
                # Pan: focal+position shift by same vector → direction unchanged → dot=1 → no-op
                # Zoom: parallel scale changes → direction unchanged → dot=1 → no-op
                # Rotation: direction changes → dot < 1 → correct immediately
                if np.dot(current_dir, locked_dir) < 0.9999:
                    focal = np.array(cam.GetFocalPoint(), dtype=float)
                    dist  = cam.GetDistance()
                    cam.SetPosition((focal - locked_dir * dist).tolist())
                    cam.SetViewUp(locked_up.tolist())
                    cam.OrthogonalizeViewUp()
                    cam.ParallelProjectionOn()
                    ren.ResetCameraClippingRange()
            except Exception:
                pass
            finally:
                _busy[0] = False

        obs_id = cam.AddObserver('ModifiedEvent', _guard)
        vtk_widget._section_2d_guard_obs_id = obs_id

    def refit_camera_to_visible_points(self, view_index, source_view_index=None):
        """
        ✅ FIXED: Refit camera based on SOURCE view's visibility when synced.
        
        When View 1 is synced to View 2:
        - View 1 shows View 2's section data
        - But camera fits to View 1's visibility settings (exclude hidden classes)
        
        Args:
            view_index: Target view to refit (0, 1, 2, ...)
            source_view_index: View providing the section data (None = use view_index)
        """
        try:
            import numpy as np
            
            # Get VTK widget for this view
            if not hasattr(self.app, 'section_vtks') or view_index not in self.app.section_vtks:
                print(f"   ⚠️ No VTK widget for view {view_index}")
                return
            
            vtk_widget = self.app.section_vtks[view_index]
            
            # ✅ CRITICAL FIX: When synced, get section data from SOURCE view
            # but use TARGET view's palette for visibility
            if source_view_index is not None:
                # Synced case: use source's data
                core_points = getattr(self.app, f"section_{source_view_index}_core_points", None)
                core_indices = getattr(self.app, f"section_{source_view_index}_core_indices", None)
                print(f"   🔗 Using section data from View {source_view_index + 1} for View {view_index + 1} camera fit")
            else:
                # Normal case: use own data
                core_points = getattr(self.app, f"section_{view_index}_core_points", None)
                core_indices = getattr(self.app, f"section_{view_index}_core_indices", None)
            
            if core_points is None or core_indices is None or len(core_points) == 0:
                print(f"   ⚠️ No section data for view {view_index}")
                return
            
            # ✅ ALWAYS use TARGET view's palette for visibility (not source)
            # This makes View 1 exclude Class 51 even when showing View 2's data
            view_palette = self._get_view_palette(view_index)
            if not view_palette:
                print(f"   ⚠️ No palette for view {view_index} - using all points")
                self._fit_camera_to_section_points(vtk_widget, core_points)
                return
            
            # Get CURRENTLY visible classes from TARGET view's palette
            visible_classes = [code for code, info in view_palette.items() if info.get('show', True)]
            
            if len(visible_classes) == len(view_palette):
                # All visible - no filtering needed
                print(f"   📊 All {len(view_palette)} classes visible - using full bounds")
                self._fit_camera_to_section_points(vtk_widget, core_points)
                return
            
            if len(visible_classes) == 0:
                print(f"   ⚠️ No visible classes - cannot refit")
                return
            
            # Get classifications for core points
            if not hasattr(self.app, 'data') or 'classification' not in self.app.data:
                return
            
            classifications = self.app.data['classification'][core_indices]
            
            # Filter to visible points only
            visible_mask = np.isin(classifications, visible_classes)
            visible_points = core_points[visible_mask]
            
            if len(visible_points) == 0:
                print(f"   ⚠️ No visible points after filtering")
                return
            
            hidden_count = len(core_points) - len(visible_points)
            hidden_classes = [c for c in np.unique(classifications) if c not in visible_classes]
            
            if hidden_count > 0:
                print(f"   🔍 REFIT VIEW {view_index + 1}: Excluding {hidden_count} points from classes {hidden_classes}")
                print(f"   📊 Camera refit: {len(visible_points)}/{len(core_points)} points visible")
            
            # Fit camera to visible points only
            self._fit_camera_to_section_points(vtk_widget, visible_points)
            
        except Exception as e:
            print(f"   ⚠️ Refit camera failed for view {view_index}: {e}")
            import traceback
            traceback.print_exc()

    def _auto_apply_view_palette(self, view_index: int, view_palette: dict):
        """
        ✅ AUTO-APPLY palette to ONLY this specific view (NOT main view)
        ✅ FIXED: When synced, uses SOURCE view's visibility for camera fit
    
        - Gets view-specific palette from Display Mode dialog
        - Re-renders ONLY that cross-section view
        - Does NOT touch main view
    
        Args:
            view_index: 0-based view index (0=View 1, 1=View 2, etc.)
            view_palette: The view-specific palette dict from dialog
        """
    
        print(f"\n{'='*60}")
        print(f"🎨 AUTO-APPLY PALETTE TO VIEW {view_index + 1}")
        print(f"{'='*60}")
    
        try:
            auto_apply_success = False
            prev_syncing = getattr(self.app, '_syncing_camera', False)
            sync_guard_set = False
            lock_set = False
            prev_lock = False

            # ✅ CRITICAL: Get the section data for THIS specific view
            core_points = getattr(self.app, f"section_{view_index}_core_points", None)
            buffer_points = getattr(self.app, f"section_{view_index}_buffer_points", None)
            core_mask = getattr(self.app, f"section_{view_index}_core_mask", None)
            buffer_mask = getattr(self.app, f"section_{view_index}_buffer_mask", None)
        
            if core_points is None or core_mask is None:
                print(f" ⚠️ No section data for View {view_index + 1}")
                print(f"{'='*60}\n")
                return
        
            # ✅ Get this view's VTK widget
            if view_index not in self.app.section_vtks:
                print(f" ⚠️ View {view_index + 1} not open")
                print(f"{'='*60}\n")
                return
        
            vtk_widget = self.app.section_vtks[view_index]
        
            # Get current classifications from MAIN data (not view-specific)
            current_classes = self.app.data.get("classification")
            if current_classes is None:
                print(f" ⚠️ No classification data")
                print(f"{'='*60}\n")
                return
        
            # ✅ Get VISIBLE classes from THIS VIEW's palette ONLY
            visible_classes = [c for c, info in view_palette.items() if info.get("show", True)]
        
            if not visible_classes:
                print(f" ⚠️ No visible classes in View {view_index + 1} palette")
                vtk_widget.clear()
                vtk_widget.render()
                print(f"{'='*60}\n")
                return
        
            print(f" 📋 Visible classes: {visible_classes}")
            print(f" 📊 View palette has {len(view_palette)} classes")
        
            # ✅ Re-render ONLY this view with the palette
            import numpy as np
            import pyvista as pv
        
            # Combine core + buffer
            if buffer_points is not None and buffer_mask is not None:
                all_points = np.vstack([core_points, buffer_points])
                all_classes = np.concatenate([
                    current_classes[core_mask],
                    current_classes[buffer_mask & ~core_mask]
                ])
            else:
                all_points = core_points
                all_classes = current_classes[core_mask]
        
            # Filter by visible classes
            visible_mask = np.isin(all_classes, visible_classes)
            filtered_points = all_points[visible_mask]
            filtered_classes = all_classes[visible_mask]
        
            print(f" 📊 Total points in section: {len(all_points)}:,")
            print(f" 📊 Visible points: {len(filtered_points)}:,")
            hidden_count = int(len(all_points) - len(filtered_points))
            if hidden_count > 0:
                hidden_pct = (100.0 * hidden_count / max(1, len(all_points)))
                print(
                    f" ⚠️ {hidden_count:,} points hidden by class visibility "
                    f"({hidden_pct:.2f}%)."
                )
                try:
                    if hasattr(self.app, "statusBar"):
                        self.app.statusBar().showMessage(
                            f"View {view_index + 1}: {hidden_count:,} points hidden by "
                            "Display Mode class visibility.",
                            5000
                        )
                except Exception:
                    pass
        
            if len(filtered_points) == 0:
                print(f" ⚠️ No visible points after filtering")
                vtk_widget.clear()
                vtk_widget.render()
                print(f"{'='*60}\n")
                return
        
            # Save camera
            try:
                cam = vtk_widget.renderer.GetActiveCamera()
                cam_state = {
                    "pos": cam.GetPosition(),
                    "fp": cam.GetFocalPoint(),
                    "up": cam.GetViewUp(),
                    "ps": cam.GetParallelScale(),
                    "pp": cam.GetParallelProjection(),
                }
            except Exception:
                cam_state = None
        
            # ✅ CRITICAL: Disable camera sync during this render to prevent blinking cascade
            # When we re-render with new palette, we don't want it to trigger sync to other views
            self.app._syncing_camera = True
            sync_guard_set = True

            # ✅ UNIFIED ACTOR: push palette change via GPU uniform — never rebuild actors
            try:
                from gui.unified_actor_manager import sync_palette_to_gpu, is_unified_actor_ready
                slot_idx = view_index + 1
                if is_unified_actor_ready(self.app) or \
                        f"_section_{view_index}_unified" in vtk_widget.actors:
                    border_pct = float(
                        (self.app.view_borders.get(slot_idx, 0) or 0.0)
                        if hasattr(self.app, "view_borders") else 0.0
                    )
                    sync_palette_to_gpu(self.app, slot_idx, view_palette, border_pct,
                                        render=False)
                    print(f"   ⚡ sync_palette_to_gpu fired for slot {slot_idx}")
                else:
                    # Unified actor not built yet — build it now
                    from gui.unified_actor_manager import build_section_unified_actor
                    build_section_unified_actor(
                        self.app, view_index,
                        view=getattr(self.app, 'cross_view_mode', 'front')
                    )
                    print(f"   🔨 Built unified section actor for View {view_index + 1}")
            except Exception as _ue:
                print(f"   ⚠️ Unified palette apply failed: {_ue}")
                import traceback
                traceback.print_exc()

            # ✅ Lock this view's palette so sync operations can't overwrite visibility
            if not hasattr(self.app, '_view_palette_locks'):
                self.app._view_palette_locks = {}
            prev_lock = bool(self.app._view_palette_locks.get(view_index, False))
            self.app._view_palette_locks[view_index] = True
            lock_set = True

            vtk_widget.render()
            print(f" 🔒 Palette locked for View {view_index + 1} (prevents sync overwrite)")
            print(f" ✅ View {view_index + 1} re-rendered via GPU uniform (ISOLATED)")

            # ✅ Re-apply this view's own previously-selected display mode
            # (Depth/Intensity/RGB/Elevation), if it had one. The sync above
            # only pushes classification colors — without this, drawing a
            # new section for a view that was e.g. on Elevation would
            # silently fall back to classification colors, losing the mode
            # the user had explicitly chosen for that view.
            try:
                _reapply_slot_idx = view_index + 1
                dlg = getattr(self.app, 'display_mode_dialog', None)
                remembered_modes = getattr(dlg, 'view_color_modes', None) if dlg is not None else None
                if isinstance(remembered_modes, dict):
                    _reapply_idx = remembered_modes.get(_reapply_slot_idx, 0)
                    if _reapply_idx in (1, 6):
                        from gui.cross_section.section_shaded_surface import (
                            build_section_shaded_surface_actor,
                        )
                        _mesh_mode = "shaded" if _reapply_idx == 1 else "surface"
                        reapplied = build_section_shaded_surface_actor(
                            self.app, view_index, _mesh_mode
                        )
                        if reapplied:
                            print(f"   🎨 Re-applied View {view_index + 1}'s own display mode: {_mesh_mode} (mesh cut)")
                    else:
                        _SECTION_MODE_BY_IDX = {2: "depth", 3: "intensity", 4: "rgb", 5: "elevation", 7: "line"}
                        remembered_mode = _SECTION_MODE_BY_IDX.get(_reapply_idx)
                        if remembered_mode:
                            _reapply_border = float(
                                (self.app.view_borders.get(_reapply_slot_idx, 0) or 0.0)
                                if hasattr(self.app, "view_borders") else 0.0
                            )
                            from gui.unified_actor_manager import refresh_section_after_weight_change
                            reapplied = refresh_section_after_weight_change(
                                self.app, view_index, view_palette, _reapply_border, remembered_mode
                            )
                            if reapplied:
                                print(f"   🎨 Re-applied View {view_index + 1}'s own display mode: {remembered_mode}")
            except Exception as _mode_reapply_err:
                print(f"   ⚠️ Re-applying View {view_index + 1}'s display mode skipped: {_mode_reapply_err}")

            # ═══════════════════════════════════════════════════════════════════
            # ✅ CRITICAL FIX: Refit camera based on SOURCE view's visibility when synced
            # ═══════════════════════════════════════════════════════════════════
            # Check if this view is synced to another view
            try:
                if hasattr(self, 'refit_camera_to_visible_points'):
                    # Determine which view's data is being displayed
                    source_view = None
                    
                    # Check if this view is synced (showing another view's data)
                    if hasattr(self.app, 'section_sync_map') and view_index in self.app.section_sync_map:
                        source_view = self.app.section_sync_map[view_index]
                        print(f" 🔗 View {view_index + 1} synced to View {source_view + 1}")
                        print(f"    Using View {source_view + 1}'s visibility for camera fit")
                    
                    # Call refit with source view index
                    self.refit_camera_to_visible_points(view_index, source_view_index=source_view)
                    print(f" 📷 Camera refitted to visible points only")
                else:
                    print(f" ⚠️ refit_camera_to_visible_points method not found")
            except Exception as e:
                print(f" ⚠️ Camera refit failed: {e}")
                import traceback
                traceback.print_exc()
            # ═══════════════════════════════════════════════════════════════════
            self._force_section_camera_2d(vtk_widget, view_index)            
            auto_apply_success = True
            print(f"{'='*60}\n")
        
        except Exception as e:
            print(f" ❌ Auto-apply failed: {e}")
            import traceback
            traceback.print_exc()
            print(f"{'='*60}\n")
        finally:
            try:
                if 'sync_guard_set' in locals() and sync_guard_set:
                    self.app._syncing_camera = prev_syncing
            except Exception:
                pass

            # If an exception occurs after lock=True, roll back to previous
            # lock state so this view cannot get stuck locked.
            try:
                if ('lock_set' in locals() and lock_set
                        and 'auto_apply_success' in locals()
                        and not auto_apply_success):
                    if not hasattr(self.app, '_view_palette_locks'):
                        self.app._view_palette_locks = {}
                    self.app._view_palette_locks[view_index] = prev_lock
                    print(
                        f"[PALETTE-LOCK] rollback view={view_index + 1} "
                        f"restored={prev_lock} reason=auto-apply-exception"
                    )
            except Exception:
                pass

    def refresh_colors(self):
        """
        ✅ UNIFIED ACTOR: Refresh cross-section view via fast_cross_section_update.
        Writes directly into _naksha_rgb_ptr — no per-class actor create/destroy.
        MicroStation equivalent: invalidate element display → single GPU redraw.
        """
        if self.active_view is None:
            print("⚠️ No active view to refresh")
            return

        view_idx = int(self.active_view)

        print(f"\n{'='*60}")
        print(f"🎨 REFRESHING COLORS (UNIFIED ACTOR): Cross-Section {view_idx + 1}")
        print(f"{'='*60}")

        vtk_widget = getattr(self.app, "section_vtks", {}).get(view_idx)
        if not vtk_widget:
            print(f"   ⚠️ No VTK widget for view {view_idx}")
            print(f"{'='*60}\n")
            return

        core_points = getattr(self.app, f"section_{view_idx}_core_points", None)
        core_mask   = getattr(self.app, f"section_{view_idx}_core_mask", None)
        if core_points is None or core_mask is None:
            print(f"   ⚠️ No section data for view {view_idx}")
            print(f"{'='*60}\n")
            return

        view_palette = self._get_view_palette(view_idx) or {}

        # ── UNIFIED ACTOR FAST PATH ──────────────────────────────────────
        try:
            from gui.unified_actor_manager import fast_cross_section_update
            changed_mask = getattr(self.app, '_last_changed_mask', None)
            fast_cross_section_update(self.app, view_idx, changed_mask,
                                      palette=view_palette,
                                      force_visibility_refresh=True)
            vtk_widget.render()
            print(f"   ✅ Unified fast update complete — View {view_idx + 1}")
            print(f"{'='*60}\n")
            return
        except Exception as e:
            print(f"   ⚠️ Unified fast path failed, using build fallback: {e}")

        # ── FALLBACK: unified actor not yet built — trigger build ────────
        try:
            from gui.unified_actor_manager import build_section_unified_actor
            border_pct = float(
                (self.app.view_borders.get(view_idx, 0) or 0.0)
                if hasattr(self.app, "view_borders") else 0.0
            )
            build_section_unified_actor(self.app, view_idx,
                                        view=getattr(self.app, 'cross_view_mode', 'front'))
            vtk_widget.render()
            print(f"   ✅ Unified actor built — View {view_idx + 1}")
        except Exception as e2:
            print(f"   ❌ Unified build fallback also failed: {e2}")
            import traceback
            traceback.print_exc()

        print(f"{'='*60}\n")



###################################################################################################################

    def refresh_colors_direct(self):
        """
        ✅ FIXED: Delegates to the unified actor refresh in app_window.py.
        """
        if not hasattr(self, "active_view") or self.active_view is None:
            print("⚠️ No active view to refresh")
            return

        view_idx = int(self.active_view)

        print(f"\n{'='*60}")
        print(f"🎨 DIRECT REFRESH (UNIFIED ACTOR): Cross-Section View {view_idx + 1}")
        print(f"{'='*60}")
        
        try:
            if hasattr(self.app, "_refresh_single_section_view"):
                # Use the new unified path in app_window.py
                self.app._refresh_single_section_view(view_idx)
            else:
                print(f"   ⚠️ _refresh_single_section_view not found!")
        except Exception as e:
            print(f"   ❌ Direct refresh failed: {e}")
            import traceback
            traceback.print_exc()
            print(f"{'='*60}\n")



    def _refresh_cut_section_if_active(self):
        """
        ✅ UNIFIED: Delegate to CutSectionController._refresh_cut_colors_fast().
        That method does a direct VTK pointer write — no actor destroy/create.
        """
        try:
            if not hasattr(self.app, 'cut_section_controller'):
                return
            cut_ctrl = self.app.cut_section_controller
            if not getattr(cut_ctrl, 'is_cut_view_active', False):
                return
            if cut_ctrl.cut_points is None or cut_ctrl._cut_index_map is None:
                return

            print(f"\n{'='*60}")
            print(f"🔄 AUTO-REFRESHING CUT SECTION (fast path)")
            print(f"{'='*60}")

            cut_ctrl._refresh_cut_colors_fast()

            print(f"   ✅ Cut section refreshed")
            print(f"{'='*60}\n")

        except Exception as e:
            print(f"❌ Failed to refresh cut section: {e}")
            import traceback
            traceback.print_exc()

    def _show_refresh_indicator(self):
        """
        Show a subtle indicator that auto-refresh happened.
        Makes the user aware their changes were applied.
        """
        if hasattr(self.app, 'statusBar'):
            active_view = getattr(self.app.section_controller, 'active_view', 0)
            self.app.statusBar().showMessage(
                f"✨ View {active_view + 1} updated", 
                1500  # Short duration - not intrusive
            )


        # ============================================
        # DIAGNOSTIC METHOD - Add this for debugging
        # ============================================

    def diagnose_view_state(self):
            """
            Diagnostic method to check which view is being updated.
            Call this before refresh to verify target view.
            """
            print(f"\n{'='*60}")
            print(f"🔍 SECTION CONTROLLER DIAGNOSTICS")
            print(f"{'='*60}")
            print(f"   active_view: {getattr(self, 'active_view', 'NOT SET')}")
            print(f"   Available section views: {list(getattr(self.app, 'section_vtks', {}).keys())}")
            print(f"   Main view display mode: {getattr(self.app, 'display_mode', 'UNKNOWN')}")
            
            if hasattr(self.app, 'class_palette'):
                visible = [c for c, i in self.app.class_palette.items() if i.get('show', False)]
                print(f"   Visible classes in palette: {visible}")
            
            print(f"{'='*60}\n")

    def _make_colors(self, points, classes=None):
        """
        ✅ UNIFIED: Assign colors using vectorized LUT (no per-class Python loop).
        Matches unified_actor_manager.ColorLUT.map_classes() for consistency.
        """
        mode = self.app.display_mode
        colors = np.full((points.shape[0], 3), 200, dtype=np.uint8)

        if mode == "rgb" and self.app.data.get("rgb") is not None:
            global_rgb = (self.app.data["rgb"] * 255).astype(np.uint8)
            n = min(points.shape[0], global_rgb.shape[0])
            colors[:n] = global_rgb[:n]

        elif mode == "intensity" and self.app.data.get("intensity") is not None:
            intens = self.app.data["intensity"][:points.shape[0]].astype(float)
            norm = (intens - intens.min()) / (intens.max() - intens.min() + 1e-6)
            colors = np.c_[norm * 255, norm * 255, norm * 255].astype(np.uint8)

        elif mode == "elevation":
            z = points[:, 2]
            norm = (z - z.min()) / (z.max() - z.min() + 1e-6)
            colors = np.c_[norm * 255, norm * 255, (1 - norm) * 255].astype(np.uint8)

        elif mode in ("class", "shaded_class") and classes is not None:
            # ✅ VECTORIZED: Build LUT once, apply to all points in one numpy op
            palette = {}
            # CUT mode should respect slot-5 palette when available.
            if getattr(self.app, "cross_view_mode", "") == "cut":
                cut_ctrl = getattr(self.app, "cut_section_controller", None)
                if cut_ctrl is not None and hasattr(cut_ctrl, "_get_cut_slot_palette"):
                    try:
                        palette = cut_ctrl._get_cut_slot_palette(ensure_seed=True) or {}
                    except Exception:
                        palette = {}
                if not palette and hasattr(self.app, "_get_cross_section_palette"):
                    try:
                        palette = self.app._get_cross_section_palette(
                            5,
                            allow_default_seed=True,
                            persist_seed=False,
                        ) or {}
                    except Exception:
                        palette = {}

            # Cross-section mode should respect view slot palette.
            if not palette and getattr(self, "active_view", None) is not None:
                try:
                    palette = self._get_view_palette(int(self.active_view)) or {}
                except Exception:
                    palette = {}

            # Main/legacy fallback.
            if not palette:
                palette = getattr(self.app, 'class_palette', {})
            max_c = max(int(classes.max()) + 1, 256) if len(classes) > 0 else 256
            lut = np.full((max_c, 3), 128, dtype=np.uint8)
            for code, info in palette.items():
                idx = int(code)
                if 0 <= idx < max_c:
                    if info.get("show", True):
                        lut[idx] = info.get("color", (200, 200, 200))
                    else:
                        lut[idx] = (0, 0, 0)
            colors = lut[classes.clip(0, max_c - 1).astype(np.intp)]

        return colors



    def _plot_section(self, core_points, buffer_points, view="side"):
        """
        Unified implementation. Always rebuilds geometry for new section coordinates.
        """
        view_idx   = self.active_view
        actor_name = f"_section_{view_idx}_unified"
        vtk_widget = self.app.section_vtks[view_idx]

        # Remove stale actor and any legacy actors before building fresh geometry
        for name in list(vtk_widget.actors.keys()):
            if name == actor_name or name.startswith("class_") \
                    or name.startswith("section_core_") \
                    or name.startswith("section_buffer_"):
                try:
                    vtk_widget.remove_actor(name, render=False)
                except Exception:
                    pass

        from gui.unified_actor_manager import build_section_unified_actor
        build_section_unified_actor(self.app, view_idx, view=view)

        # [CS-MESH-DISPLAY] section geometry reapply
        try:
            from .section_mesh_display import reapply_section_mesh_mode_after_geometry
            reapply_section_mesh_mode_after_geometry(self.app, view_idx)
        except Exception as _cs_mesh_reapply_err:
            print(
                f"SECTION_MESH view={view_idx + 1} status=geometry_reapply_failed "
                f"reason={_cs_mesh_reapply_err}"
            )

    # Other methods remain unchanged...

    # ---------------- DOCK ---------------

    def _init_dock(self):
        self.app.section_frame = QWidget()
        sec_layout = QVBoxLayout()
   
        # --- Buttons row ---
        btn_row = QHBoxLayout()
        self.side_btn = QPushButton("Side View")
        self.front_btn = QPushButton("Front View")
        for b in (self.side_btn, self.front_btn):
            b.setCheckable(True)
            btn_row.addWidget(b)
        sec_layout.addLayout(btn_row)

        self.front_btn.setChecked(True)
        self.side_btn.setChecked(False)

   
        # --- Buffer depth control (UPDATED LABEL) ---
        buffer_row = QHBoxLayout()
        buffer_row.addWidget(QLabel("Buffer Depth:"))  # ✅ Changed from "Width"
        self.buffer_spin = QSpinBox()
        self.buffer_spin.setRange(0, 50)   # allow 0–50 m
        self.buffer_spin.setValue(getattr(self.app, "section_buffer", 2))
        self.buffer_spin.setSuffix(" m")
        # ✅ Add tooltip explaining MicroStation behavior
        self.buffer_spin.setToolTip(
            "Extends section LENGTH (along the line) for context.\n"
            "Does NOT affect cross-section width.\n\n"
            "Width is determined only by your perpendicular drag."
        )
        buffer_row.addWidget(self.buffer_spin)
        sec_layout.addLayout(buffer_row)
   
        # --- Viewer ---
        self.app.sec_vtk = QtInteractor(self.app.section_frame)
        if hasattr(self.app, "_setup_interactor_swapper"):
            self.app._setup_interactor_swapper(
                self.app.sec_vtk.interactor,
                preserve_physical_middle_pan=True,
                honor_persistent_left_pan=True,
            )
        from gui.theme_manager import ThemeManager
        bg_color = "white" if ThemeManager.current() == "light" else "black"
        self.app.sec_vtk.set_background(bg_color)
        sec_layout.addWidget(self.app.sec_vtk.interactor)
        if hasattr(self.app, "_install_section_wheel_zoom"):
            self.app._install_section_wheel_zoom(self.app.sec_vtk)
   
        # ✅ Install shortcut filter here (now sec_vtk exists)
        if hasattr(self.app, "_shortcut_filter"):
            self.app.sec_vtk.interactor.installEventFilter(self.app._shortcut_filter)
   
        self.app.section_frame.setLayout(sec_layout)
        self.app.section_dock = QDockWidget("Cross Section", self.app)
        self.app.section_dock.setWidget(self.app.section_frame)
        self.app.addDockWidget(Qt.RightDockWidgetArea, self.app.section_dock)
   
        # ✅ Install global shortcut filter here too
        self.app.sec_vtk.interactor.installEventFilter(self.app._shortcut_filter)
   
        self.app.section_frame.setLayout(sec_layout)
        self.app.section_dock = QDockWidget("Cross Section", self.app)
        self.app.section_dock.setWidget(self.app.section_frame)
        self.app.addDockWidget(Qt.RightDockWidgetArea, self.app.section_dock)
   
        # Connect buttons
        # ✅ FIXED: Connect buttons to trigger refresh on view mode change
        self.side_btn.clicked.connect(lambda: self.set_cross_view_mode("side"))
        self.front_btn.clicked.connect(lambda: self.set_cross_view_mode("front"))
   
        # Connect buffer spin
        self.buffer_spin.valueChanged.connect(self._update_buffer)
   
        # self.app.cross_view_mode = "side"
        #Added by bala for view
        self.app.cross_view_mode = "front"


    def _update_buffer(self, val):
        """Triggered when spinbox changes."""
        self.app.section_buffer = val
        print(f"🔄 Buffer width set to {val} m")

        # Recompute section if a slice is active
        if self.P1 is not None and self.P2 is not None:
            self.finalize_section(self.P1, self.P2)

    def _plot_cut_section(self, cut_points):
        """Display perpendicular cut slice inside the same Cross Section dock,
        with full classification, color, and refresh support identical to normal section view."""
        if cut_points is None or len(cut_points) == 0:
            print("⚠️ No points to plot in cut section")
            return

        print("🔄 Rendering CUT section with TerraScan classification colors...")

        # --- Save state ---
        self.app.section_cut_points = cut_points
        self.app.cross_view_mode = "cut"

        # Remove previous actors
        if hasattr(self, "_cut_actor") and self._cut_actor is not None:
            self.app.sec_vtk.remove_actor(self._cut_actor, reset_camera=False)
            self._cut_actor = None

        # --- Compute accurate mask based on geometry (nearest match to source) ---
        try:
            all_xyz = self.app.data["xyz"]
            # Use persistent cached KDTree — built once per file load, not per call
            from gui.spatial_index import get_or_build_index
            _idx_obj = get_or_build_index(all_xyz)
            tree = _idx_obj.tree
            _, idx = tree.query(cut_points, k=1)
            cut_classes = self.app.data["classification"][idx]
            cut_colors = self._make_colors(cut_points, cut_classes)
        except Exception as e:
            print(f"⚠️ Failed to compute color mapping for cut-section: {e}")
            cut_colors = np.full((cut_points.shape[0], 3), 200, dtype=np.uint8)

        # --- Create and render the point cloud ---
        import pyvista as pv
        cloud = pv.PolyData(cut_points)
        cloud["RGB"] = cut_colors
        self._cut_actor = self.app.sec_vtk.add_points(
            cloud, scalars="RGB", rgb=True, point_size=3
        )

        # ✅ Register cut section state for classification
        try:
            # self.app.cut_section_active = True
            self.app.section_cut_points = cut_points
            all_xyz = self.app.data["xyz"]
            # Create a boolean mask marking those indices and store the indices for color lookups.
            if 'idx' in locals():
                cut_mask = np.zeros(len(all_xyz), dtype=bool)
                cut_mask[idx] = True
                self.app.cut_section_mask = cut_mask
                self.app.section_indices = idx
            else:
                # Fallback: no match indices available
                self.app.cut_section_mask = np.zeros(len(all_xyz), dtype=bool)
                self.app.section_indices = None
            print("🟢 Cut Section state registered for classification tools.")
        except Exception as e:
            print(f"⚠️ Could not register cut section state: {e}")

        # --- Camera setup (~80° rotated perpendicular view) ---
        cam = self.app.sec_vtk.renderer.GetActiveCamera()
        cam.ParallelProjectionOn()
        # self.app.sec_vtk.view_yz()
        # cam.Azimuth(80)
        # self.app.sec_vtk.renderer.ResetCamera()
        self.app.sec_vtk.render()

        # --- Reattach classification interactor ---
        from .interactor_classify import ClassificationInteractor
        iren = self.app.sec_vtk.interactor

        # ✅ Disable camera rotations in cut-section mode
        try:
            style = vtk.vtkInteractorStyleRubberBand2D()
  # completely user-driven style
            iren.SetInteractorStyle(style)
            print("🧭 Camera rotation/panning disabled for Cut Section view.")
        except Exception as e:
            print(f"⚠️ Failed to disable default interactor style: {e}")

        # ✅ Attach classification interactor for 2D interaction
        self.app.cut_section_active = True  
        self.is_locked = True
        wrapper = ClassificationInteractor(self.app, iren, mode="2d")
        iren.SetInteractorStyle(wrapper.style)
        self.app.classify_interactor = wrapper
        wrapper.is_cut_section_mode = True
        print("🟣 ClassificationInteractor attached (cut-section mode, locked to 2D).")

        # # --- Refresh colors support ---
        #     self.last_mask = np.ones(len(self.app.data["xyz"]), dtype=bool)
        #     self.app.section_points = cut_points
        #     self.app.section_controller.refresh_colors()

        # --- Handle shaded class / DSM modes ---
        if getattr(self.app, "display_mode", "") == "shaded_class":
            try:
                from ..pointcloud_display import update_pointcloud
                update_pointcloud(self.app, "shaded_class")
                print("✅ Shaded mode updated in Cut Section.")
            except Exception as e:
                print(f"⚠️ Shaded update failed: {e}")

        print("✅ Cut Section rendered with true classification colors.")

    # ------------------------------------------------------------------
    # ✅ Overlay indicator for Cut View
    # ------------------------------------------------------------------
    def show_cut_overlay(self):
        """Show 'CUT VIEW ACTIVE' overlay on the cross-section dock."""
        try:
            if not hasattr(self.app, "sec_vtk") or self.app.sec_vtk is None:
                return

            from PySide6.QtWidgets import QLabel
            from PySide6.QtGui import QFont

            label = QLabel("✂ CUT VIEW ACTIVE", self.app.sec_vtk.interactor)
            label.setStyleSheet("""
                QLabel {
                    background-color: rgba(0, 0, 0, 120);
                    color: rgb(255, 85, 85);
                    border: 1px solid rgba(255, 85, 85, 180);
                    border-radius: 5px;
                    padding: 3px 10px;
                }
            """)
            label.setFont(QFont("Segoe UI", 10, QFont.Bold))
            label.adjustSize()
            label.move(15, 15)
            label.show()
            label.raise_()
            self.cut_overlay_label = label
            print("🟢 Overlay shown: CUT VIEW ACTIVE")
        except Exception as e:
            print(f"⚠️ show_cut_overlay failed: {e}")


    def hide_cut_overlay(self):
        """Hide overlay label if visible."""
        try:
            if hasattr(self, "cut_overlay_label") and self.cut_overlay_label:
                self.cut_overlay_label.hide()
                self.cut_overlay_label.deleteLater()
                self.cut_overlay_label = None
                print("🔵 Overlay hidden.")
        except Exception as e:
            print(f"⚠️ hide_cut_overlay failed: {e}")

            # New functions for multi-view support
    def refresh_colors_for_view(self, view_index, palette=None):
        """
        🚀 MILLISECOND REFRESH: Updates GPU buffers for cross-sections.
        Eliminates the 3-4 second lag by avoiding actor re-creation.
        """
        if hasattr(self.app, 'cut_section_controller') and getattr(self.app.cut_section_controller, 'is_locked', False):
            return

        try:
            import numpy as np
            from vtkmodules.util import numpy_support

            # 1. Validation
            if not hasattr(self, 'view_vtks') or view_index not in self.view_vtks:
                return
            
            # 2. Identify the Actor
            # We look for the actor we created during the first _plot_section call
            actor_name = f"section_buffer_{view_index}"
            vtk_widget = self.view_vtks[view_index]
            
            actor = vtk_widget.actors.get(actor_name)
            
            # 🛑 FALLBACK: If actor doesn't exist yet, do a full plot once
            if actor is None or not hasattr(self, 'view_indices') or view_index not in self.view_indices:
                print(f"🔄 Initializing full plot for view {view_index}...")
                old_active = self.active_view
                self.active_view = view_index
                self.current_vtk = vtk_widget
                self._plot_section(
                    self.app.section_core_points,
                    self.app.section_buffer_points,
                    view=getattr(self, 'current_section_view', 'side')
                )
                self.active_view = old_active
                return

            # 3. 🚀 THE FAST PATH (Direct GPU update)
            # Get the point indices that belong to THIS specific cross-section view
            indices = self.view_indices.get(view_index)
            if indices is None: return

            # Get the classification data for these specific points
            classes = self.app.data["classification"][indices]
            
            # Use provided palette, otherwise resolve this view's slot palette.
            active_palette = palette or self._get_view_palette(view_index) or {}
            if not active_palette and hasattr(self.app, "_get_cross_section_palette"):
                try:
                    active_palette = self.app._get_cross_section_palette(
                        int(view_index) + 1,
                        allow_default_seed=True,
                        persist_seed=False,
                    ) or {}
                except Exception:
                    active_palette = {}

            # Last-resort safe seed: main colors/weights with independent visibility.
            if not active_palette:
                master = getattr(self.app, 'class_palette', {}) or {}
                seeded = {}
                for code, info in master.items():
                    if not isinstance(info, dict):
                        continue
                    try:
                        code_i = int(code)
                    except Exception:
                        continue
                    seeded[code_i] = {
                        "show": True,
                        "color": tuple(info.get("color", (128, 128, 128))),
                        "weight": float(info.get("weight", 1.0)),
                        "description": str(info.get("description", "")),
                    }
                active_palette = seeded

            # Access the VTK Color Buffer
            _m = actor.GetMapper()
            polydata = _m.GetInput() if _m else None
            vtk_colors = polydata.GetPointData().GetScalars() if polydata else None
            
            if vtk_colors:
                # Create a local Lookup Table for speed
                max_c = int(classes.max()) if classes.size > 0 else 0
                lut = np.zeros((max_c + 1, 3), dtype=np.uint8)
                
                for code, info in active_palette.items():
                    if code <= max_c:
                        lut[code] = info['color'] if info.get('show', True) else (0, 0, 0)

                # Vectorized mapping: Millions of points mapped in ~5-10ms
                new_rgb = lut[classes.astype(int)]
                
                # Zero-copy pointer access to GPU memory
                vtk_ptr = numpy_support.vtk_to_numpy(vtk_colors)
                np.copyto(vtk_ptr, new_rgb)
                
                # Notify VTK that data has changed so it re-renders
                vtk_colors.Modified()
                vtk_widget.render()
                
                print(f"🚀 Fast Sync: Section View {view_index+1} updated via GPU buffer")

        except Exception as e:
            print(f"⚠️ Fast refresh failed for view {view_index}: {e}")
            # Final safety fallback: run the original slow refresh
            self._plot_section(self.app.section_core_points, self.app.section_buffer_points)

    def refresh_colors_with_filter(self, view_index):
        """
        Refresh colors for a specific cross-section view with visibility filtering.
        Only shows points from classes that are checked in Display Mode.
        """
        try:
            if not hasattr(self, 'view_vtks') or view_index not in self.view_vtks:
                print(f"⚠️ View {view_index} not found")
                return
            
            vtk_widget = self.view_vtks[view_index]
            
            if not hasattr(self.app, 'section_core_points') or self.app.section_core_points is None:
                print(f"⚠️ No section data available")
                return
            
            # Get visibility filter (list of visible class codes)
            visible_classes = getattr(self, 'view_visibility_filter', None)
            
            # Clear the view
            vtk_widget.clear()
            
            # Get section data
            core_pts = self.app.section_core_points
            buffer_pts = self.app.section_buffer_points if hasattr(self.app, 'section_buffer_points') else None
            
            # ✅ Apply visibility filter if specified
            if visible_classes is not None and len(visible_classes) > 0:
                print(f"   🔍 Filtering to show only classes: {visible_classes}")
                
                # Get classifications for section points
                if hasattr(self.app, 'section_indices') and self.app.section_indices is not None:
                    section_classes = self.app.data['classification'][self.app.section_indices]
                    
                    # Create mask for visible classes
                    mask = np.isin(section_classes, visible_classes)
                    
                    # Filter core points
                    if core_pts is not None and len(core_pts) > 0:
                        core_mask = mask[:len(core_pts)]
                        core_pts = core_pts[core_mask]
                    
                    # Filter buffer points
                    if buffer_pts is not None and len(buffer_pts) > 0:
                        buffer_mask = mask[len(core_pts):] if len(mask) > len(core_pts) else np.zeros(len(buffer_pts), dtype=bool)
                        buffer_pts = buffer_pts[buffer_mask] if np.any(buffer_mask) else None
                    
                    print(f"   ✅ Filtered: {len(core_pts)} core points, {len(buffer_pts) if buffer_pts is not None else 0} buffer points")
            
            # Re-plot with filtered data
            if core_pts is not None and len(core_pts) > 0:
                self._plot_section_filtered(core_pts, buffer_pts, view_index)
            else:
                print(f"   ⚠️ No points to display after filtering")
            
        except Exception as e:
            print(f"⚠️ Error refreshing filtered view: {e}")
            import traceback
            traceback.print_exc()

    def _plot_section_filtered(self, core_pts, buffer_pts, view_index):
        """
        Plot section with filtered points and proper coloring.
        """
        try:
            vtk_widget = self.view_vtks[view_index]
            
            # Determine view orientation
            current_view = getattr(self, 'current_section_view', 'side')
            
            # Get button states to determine view
            if hasattr(self.app, 'section_view_buttons') and view_index in self.app.section_view_buttons:
                buttons = self.app.section_view_buttons[view_index]
                if buttons['side'].isChecked():
                    current_view = 'side'
                elif buttons['front'].isChecked():
                    current_view = 'front'
            
            # Transform points based on view
            if current_view == 'side':
                # Side view: X-Z (distance along section vs elevation)
                if core_pts is not None and len(core_pts) > 0:
                    display_pts = np.column_stack([core_pts[:, 0], core_pts[:, 2]])
                buffer_display = None
                if buffer_pts is not None and len(buffer_pts) > 0:
                    buffer_display = np.column_stack([buffer_pts[:, 0], buffer_pts[:, 2]])
            else:
                # Front view: Y-Z (perpendicular distance vs elevation)
                if core_pts is not None and len(core_pts) > 0:
                    display_pts = np.column_stack([core_pts[:, 1], core_pts[:, 2]])
                buffer_display = None
                if buffer_pts is not None and len(buffer_pts) > 0:
                    buffer_display = np.column_stack([buffer_pts[:, 1], buffer_pts[:, 2]])
            
            # Add Z coordinate (zero for 2D)
            if len(display_pts) > 0:
                display_pts = np.column_stack([display_pts, np.zeros(len(display_pts))])
            if buffer_display is not None and len(buffer_display) > 0:
                buffer_display = np.column_stack([buffer_display, np.zeros(len(buffer_display))])
            
            # Get colors based on current display mode
            colors = self._get_section_colors(core_pts, buffer_pts)
            
            # Plot core points
            if len(display_pts) > 0:
                import pyvista as pv
                cloud = pv.PolyData(display_pts)
                
                if colors is not None and len(colors) == len(display_pts):
                    vtk_widget.add_points(
                        cloud,
                        scalars=colors[:len(display_pts)],
                        rgb=True,
                        point_size=3,
                        render_points_as_spheres=True
                    )
                else:
                    vtk_widget.add_points(
                        cloud,
                        color='white',
                        point_size=3,
                        render_points_as_spheres=True
                    )
            
            # Plot buffer points (if any)
            if buffer_display is not None and len(buffer_display) > 0:
                buffer_cloud = pv.PolyData(buffer_display)
                buffer_colors = colors[len(display_pts):] if colors is not None and len(colors) > len(display_pts) else None
                
                if buffer_colors is not None:
                    vtk_widget.add_points(
                        buffer_cloud,
                        scalars=buffer_colors,
                        rgb=True,
                        point_size=2,
                        opacity=1.0,
                        render_points_as_spheres=True
                    )
                else:
                    vtk_widget.add_points(
                        buffer_cloud,
                        color='gray',
                        point_size=2,
                        opacity=1.0,
                        render_points_as_spheres=True
                    )
            
            # Reset camera and render
            vtk_widget.reset_camera()
            vtk_widget.render()
            
            print(f"✅ Plotted filtered section: {len(display_pts)} points")
            
        except Exception as e:
            print(f"⚠️ Error plotting filtered section: {e}")
            import traceback
            traceback.print_exc()


    def _get_section_colors(self, core_pts, buffer_pts):
        """
        Get colors for section points based on current display mode.
        """
        try:
            if not hasattr(self.app, 'section_indices') or self.app.section_indices is None:
                return None
            
            indices = self.app.section_indices
            
            # Get colors based on display mode
            if self.app.display_mode == "class":
                # Classification colors
                classes = self.app.data['classification'][indices]
                colors = np.zeros((len(classes), 3), dtype=np.uint8)

                # Resolve slot-aware palette (avoid slot-0 visibility/color bleed).
                palette = {}
                if getattr(self.app, "cross_view_mode", "") == "cut":
                    cut_ctrl = getattr(self.app, "cut_section_controller", None)
                    if cut_ctrl is not None and hasattr(cut_ctrl, "_get_cut_slot_palette"):
                        try:
                            palette = cut_ctrl._get_cut_slot_palette(ensure_seed=True) or {}
                        except Exception:
                            palette = {}
                    if not palette and hasattr(self.app, "_get_cross_section_palette"):
                        try:
                            palette = self.app._get_cross_section_palette(
                                5,
                                allow_default_seed=True,
                                persist_seed=False,
                            ) or {}
                        except Exception:
                            palette = {}
                else:
                    try:
                        if getattr(self, "active_view", None) is not None:
                            palette = self._get_view_palette(int(self.active_view)) or {}
                    except Exception:
                        palette = {}
                    if not palette and hasattr(self.app, "_get_cross_section_palette"):
                        try:
                            slot_idx = int(getattr(self, "active_view", 0)) + 1
                            palette = self.app._get_cross_section_palette(
                                slot_idx,
                                allow_default_seed=True,
                                persist_seed=False,
                            ) or {}
                        except Exception:
                            palette = {}

                if not palette:
                    palette = getattr(self.app, "class_palette", {}) or {}

                for code, info in palette.items():
                    mask = (classes == code)
                    if np.any(mask):
                        colors[mask] = info['color']
                
                return colors
                
            elif self.app.display_mode == "rgb" and 'rgb' in self.app.data:
                # Original RGB colors
                return self.app.data['rgb'][indices]
                
            elif self.app.display_mode == "intensity" and 'intensity' in self.app.data:
                # Intensity grayscale
                intensity = self.app.data['intensity'][indices]
                intensity_norm = ((intensity - intensity.min()) / (intensity.max() - intensity.min()) * 255).astype(np.uint8)
                return np.column_stack([intensity_norm] * 3)
                
            elif self.app.display_mode == "elevation":
                # Elevation color ramp
                z_vals = self.app.data['xyz'][indices, 2]
                z_norm = (z_vals - z_vals.min()) / (z_vals.max() - z_vals.min())
                
                # Create color ramp (blue -> green -> yellow -> red)
                colors = np.zeros((len(z_norm), 3), dtype=np.uint8)
                colors[:, 0] = (z_norm * 255).astype(np.uint8)  # Red
                colors[:, 1] = ((1 - np.abs(z_norm - 0.5) * 2) * 255).astype(np.uint8)  # Green
                colors[:, 2] = ((1 - z_norm) * 255).astype(np.uint8)  # Blue
                
                return colors
            
            return None
            
        except Exception as e:
            print(f"⚠️ Error getting section colors: {e}")
            return None
        

    def refresh_colors_isolated(self, view_index=None):
        """
        ✅ UNIFIED ACTOR: Refresh ONLY the specified cross-section view.
        Routes to fast_cross_section_update — no actor destroy/create.
        """
        if hasattr(self.app, 'cut_section_controller'):
            if getattr(self.app.cut_section_controller, 'is_locked', False):
                print("🔒 Cut section locked → BLOCKING cross-section refresh")
                return

        if view_index is None:
            view_index = self.active_view

        if view_index is None or view_index not in self.app.section_vtks:
            print("⚠️ No valid view to refresh")
            return

        print(f"🔄 Isolated refresh (unified): View {view_index + 1}")

        try:
            from gui.unified_actor_manager import fast_cross_section_update
            view_palette = self._get_view_palette(view_index) or {}
            changed_mask = getattr(self.app, '_last_changed_mask', None)
            fast_cross_section_update(self.app, view_index, changed_mask,
                                      palette=view_palette,
                                      force_visibility_refresh=True)
            vtk_widget = self.app.section_vtks[view_index]
            vtk_widget.render()
            print(f"✅ View {view_index + 1} isolated refresh complete")
        except Exception as e:
            print(f"⚠️ Isolated refresh failed: {e}")
            import traceback
            traceback.print_exc()


    
    def _refresh_all_section_colors(self, app=None):
            """
            Refresh colors in ALL open cross-section views.
            UNIFIED: Uses fast_cross_section_update for GPU-direct refresh.
            """
            app = app or self.app

            # Block refresh if cut section is locked
            if hasattr(app, 'cut_section_controller') and getattr(app.cut_section_controller, 'is_locked', False):
                print("Cut section locked: blocking _refresh_all_section_colors")
                return

            if not hasattr(app, "section_vtks") or not app.section_vtks:
                return

            try:
                from gui.unified_actor_manager import fast_cross_section_update
                changed_mask = getattr(app, '_last_changed_mask', None)

                for view_idx in sorted(app.section_vtks.keys()):
                    try:
                        view_palette = self._get_view_palette(view_idx) or {}
                        fast_cross_section_update(
                            app,
                            view_idx,
                            changed_mask,
                            palette=view_palette,
                            force_visibility_refresh=True,
                        )
                    except Exception as e:
                        print(f"Section {view_idx + 1} unified refresh failed: {e}")
            except ImportError:
                prev_active = getattr(self, "active_view", None)
                try:
                    for view_idx in sorted(app.section_vtks.keys()):
                        try:
                            self.active_view = view_idx
                            self.refresh_colors()
                        except Exception as e:
                            print(f"Fallback refresh View {view_idx + 1}: {e}")
                finally:
                    if prev_active is not None:
                        self.active_view = prev_active

    def refresh_cut_section_colors(self):
        """
        Refresh colors in the cut section view after classification changes.
        ✅ ISOLATED: Does NOT trigger cross-section or main view updates.
        """
        # Check if cut section is active
        if not hasattr(self.app, 'section_cut_points') or self.app.section_cut_points is None:
            print("⚠️ No active cut section to refresh")
            return
        
        if not hasattr(self.app, 'sec_vtk'):
            print("⚠️ No VTK widget for cut section")
            return
        
        print(f"\n{'='*60}")
        print(f"🔄 REFRESHING CUT SECTION COLORS")
        print(f"{'='*60}")
        
        try:
            import numpy as np
            import pyvista as pv
            cut_points = self.app.section_cut_points
            all_xyz = self.app.data["xyz"]

            # Use persistent cached KDTree — built once per file load, not per call
            from gui.spatial_index import get_or_build_index
            _idx_obj = get_or_build_index(all_xyz)
            _, idx = _idx_obj.tree.query(cut_points, k=1)
            
            # Get CURRENT classifications (after modification)
            current_classes = self.app.data["classification"][idx]
            
            # Resolve CUT slot palette (slot 5) when available.
            cut_palette = {}
            cut_ctrl = getattr(self.app, "cut_section_controller", None)
            if cut_ctrl is not None and hasattr(cut_ctrl, "_get_cut_slot_palette"):
                try:
                    cut_palette = cut_ctrl._get_cut_slot_palette(ensure_seed=True) or {}
                except Exception:
                    cut_palette = {}
            if not cut_palette and hasattr(self.app, "_get_cross_section_palette"):
                try:
                    cut_palette = self.app._get_cross_section_palette(
                        5,
                        allow_default_seed=True,
                        persist_seed=False,
                    ) or {}
                except Exception:
                    cut_palette = {}

            # Get visible classes from CUT palette (never slot-0 visibility).
            if cut_palette:
                visible_classes = [code for code, info in cut_palette.items() if info.get("show", True)]
                if len(visible_classes) == 0:
                    visible_classes = list(np.unique(current_classes))
            else:
                visible_classes = list(np.unique(current_classes))
            
            print(f"   📋 Visible classes: {visible_classes}")
            
            # Filter by visible classes
            visible_mask = np.isin(current_classes, visible_classes)
            filtered_points = cut_points[visible_mask]
            filtered_classes = current_classes[visible_mask]
            
            if len(filtered_points) == 0:
                print(f"   ⚠️ No visible points after filtering")
                self.app.sec_vtk.clear()
                self.app.sec_vtk.render()
                print(f"{'='*60}\n")
                return
            
            print(f"   📊 Total: {len(cut_points)} → Visible: {len(filtered_points)}")
            
            # ✅ VECTORIZED: Calculate colors using LUT (no per-point Python loop)
            palette = cut_palette or {}
            if palette:
                max_c = max(int(filtered_classes.max()) + 1, 256) if len(filtered_classes) > 0 else 256
                lut = np.full((max_c, 3), 128, dtype=np.uint8)
                for code, info in palette.items():
                    idx = int(code)
                    if 0 <= idx < max_c:
                        lut[idx] = info.get("color", (128, 128, 128))
                colors = lut[filtered_classes.clip(0, max_c - 1).astype(np.intp)]
            else:
                # Default color scheme
                default_palette = np.array([
                    [160, 160, 160], [255, 255, 255], [150, 100, 50],
                    [0, 255, 0], [0, 200, 0], [0, 150, 0],
                    [255, 0, 0], [0, 0, 255], [255, 255, 0],
                ], dtype=np.uint8)
                colors = default_palette[filtered_classes % len(default_palette)]
            
            # Debug color distribution
            unique = np.unique(filtered_classes)
            print(f"   🎨 Classes in view: {unique}")
            for cls in unique:
                count = np.sum(filtered_classes == cls)
                color = palette.get(int(cls), {}).get("color", (128, 128, 128))
                print(f"      Class {cls}: {count} pts, RGB={color}")
            
            # Save camera position
            camera_pos = self.app.sec_vtk.camera_position
            
            # Clear and redraw
            self.app.sec_vtk.clear()
            
            cloud = pv.PolyData(filtered_points)
            cloud["RGB"] = colors
            
            self.app.sec_vtk.add_points(
                cloud,
                scalars="RGB",
                rgb=True,
                point_size=3.0,
                render_points_as_spheres=True,
                name="cut_section_points"
            )
            
            # Restore camera
            self.app.sec_vtk.camera_position = camera_pos
            self.app.sec_vtk.render()
            
            print(f"   ✅ Cut section refreshed with {len(filtered_points)} points")
            print(f"{'='*60}\n")
            
        except Exception as e:
            print(f"❌ Failed to refresh cut section: {e}")
            import traceback
            traceback.print_exc()
            print(f"{'='*60}\n")

    def deactivate_for_measurement(self):
        """
        Temporarily deactivate cross-section when measurement tool starts.
        """
        print("🔄 Deactivating cross-section for measurement tool")
        
        # Reset drawing state
        self.P1 = None
        self.P2 = None
        self.half_width = None
        
        # Remove rubber band
        if self.rubber_actor:
            try:
                self.app.vtk_widget.renderer.RemoveActor(self.rubber_actor)
            except Exception:
                pass
            self.rubber_actor = None
        
        self.app.vtk_widget.render()


    def _should_block_for_measurement(self):
        """
        Check if measurement tool is active and should take priority.
        Returns True if cross-section should be blocked.
        """
        if not hasattr(self.app, 'digitizer') or not self.app.digitizer:
            return False
        
        if not hasattr(self.app.digitizer, 'measurement_tool') or not self.app.digitizer.measurement_tool:
            return False
        
        # Check if measurement tool is actively measuring
        measurement_tool = self.app.digitizer.measurement_tool
        
        if getattr(measurement_tool, 'is_measuring', False):
            return True
        
        if hasattr(measurement_tool, 'measurement_points') and len(measurement_tool.measurement_points) > 0:
            return True
        

    def update_section_colors_partial(self, changed_mask):
        """
        Update only the colors of the points whose classification changed.
        Uses the high-performance unified_actor_manager partial update.
        """
        if changed_mask is None:
            return

        if not isinstance(changed_mask, np.ndarray) or changed_mask.dtype != bool:
            m = np.zeros(len(self.app.data["xyz"]), dtype=bool)
            m[changed_mask] = True
            changed_mask = m

        if changed_mask.sum() == 0:
            return

        from gui.unified_actor_manager import fast_partial_cross_section_update
        
        # Update current active view
        view_idx = self.active_view
        success = fast_partial_cross_section_update(self.app, view_idx, changed_mask)
        
        if success:
            vtk_widget = self.app.section_vtks.get(view_idx)
            if vtk_widget:
                vtk_widget.render()
                
        # Also update other views if they are visible
        # (Classification in one view should reflect in others)
        for i, sw in self.app.section_vtks.items():
            if i != view_idx and sw and sw.isVisible():
                if fast_partial_cross_section_update(self.app, i, changed_mask):
                    sw.render()

    def _on_section_right_click(self, obj, event):
        """
        Observer callback: reactivate last classification tool on right-click
        in any cross-section view when no tool is currently active.
        """
        cs_measure = getattr(self.app, "cross_section_measurement_tool", None)
        if cs_measure is not None and getattr(cs_measure, "active", False):
            # Right-click ends an XS measurement chain. Do not immediately
            # reactivate classification underneath it, or shortcut routing
            # stops belonging to the measurement tool.
            return

        print(f"🖱️ Right-click detected in cross-section view")
        active_tool = getattr(self.app, "active_classify_tool", None)
        # A tool may be flagged active but only attached to the cut section (not cross-section
        # views). Detect that by checking whether classify_interactors has any cross-section
        # entry — if not, treat as inactive so right-click still reactivates.
        section_has_classifier = bool(
            getattr(self.app, "classify_interactors", None)
        )
        if active_tool is None or not section_has_classifier:
            last_tool = getattr(self.app, "last_classify_tool", None)
            print(f"   Last classify tool: {last_tool}")
            if last_tool and hasattr(self.app, "set_classify_tool"):
                try:
                    self.app.from_classes = getattr(self.app, "last_classify_from_classes", None)
                    self.app.to_class = getattr(self.app, "last_classify_to_class", None)
                    self.app._right_click_reactivating = True
                    try:
                        self.app.set_classify_tool(last_tool)
                        print(f"   ✅ Reactivated: {last_tool}")
                    finally:
                        self.app._right_click_reactivating = False
                except Exception as e:
                    print(f"   ⚠️ Right-click reactivate failed: {e}")
        else:
            print(f"   ℹ️ Tool already active in cross-section: {active_tool}")

    def unlock_after_classification(self):
        """
        Restore normal interaction after classification is complete.
        Called when user presses ESC or closes class picker.
        """
        try:
            print("🔓 Unlocking section controller after classification")

            if hasattr(self.app, 'section_vtks'):
                from vtkmodules.vtkInteractionStyle import vtkInteractorStyleImage

                # Ensure storage for right-click observer tags
                if not hasattr(self.app, '_section_right_click_observers'):
                    self.app._section_right_click_observers = {}

                for view_idx, vtk_widget in self.app.section_vtks.items():
                    try:
                        if vtk_widget and hasattr(vtk_widget, 'interactor'):
                            interactor = vtk_widget.interactor

                            # Remove old right-click observer if present
                            old_tag = self.app._section_right_click_observers.get(view_idx)
                            if old_tag is not None:
                                try:
                                    interactor.RemoveObserver(old_tag)
                                except Exception:
                                    pass

                            # Restore plain 2D pan/zoom style
                            if hasattr(self.app, "_apply_plain_section_2d_style"):
                                self.app._apply_plain_section_2d_style(interactor, vtk_widget)
                            else:
                                style = vtkInteractorStyleImage()
                                try:
                                    style.SetInteractionModeToImage2D()
                                except Exception:
                                    pass
                                interactor.SetInteractorStyle(style)

                            # Add right-click observer on the interactor directly
                            tag = interactor.AddObserver(
                                "RightButtonPressEvent", self._on_section_right_click, 1.0
                            )
                            self.app._section_right_click_observers[view_idx] = tag
                            print(f"   ✅ View {view_idx + 1}: Interactor restored + right-click observer added")
                    except Exception as e:
                        print(f"   ⚠️ View {view_idx + 1}: Failed - {e}")

            self._classification_active = False
            print("✅ Section controller unlocked")

        except Exception as e:
            print(f"⚠️ unlock_after_classification failed: {e}")

    def _get_current_view_mode(self):
        """
        Get the ACTUAL current view mode (front/side) from the active view's button state.
        ✅ FIXED: Checks button state instead of assuming app.cross_view_mode
        """
        if not hasattr(self, 'active_view') or self.active_view is None:
            return getattr(self.app, 'cross_view_mode', 'front')
        
        # Check if we have section docks with buttons
        if hasattr(self.app, 'section_view_buttons') and self.active_view in self.app.section_view_buttons:
            buttons = self.app.section_view_buttons[self.active_view]
            
            if buttons['side'].isChecked():
                return 'side'
            elif buttons['front'].isChecked():
                return 'front'
        
        # Fallback to app-level setting
        return getattr(self.app, 'cross_view_mode', 'front')

    # ✅ ADD THESE TWO NEW METHODS HERE:

    def set_cross_view_mode(self, mode):
        """
        Set view mode and trigger immediate refresh if changed.
        ✅ FIXED: Automatically refreshes the cross-section when switching views.
        """
        old_mode = getattr(self.app, 'cross_view_mode', 'side')
        
        if old_mode != mode:
            print(f"\n{'='*60}")
            print(f"🔄 VIEW MODE CHANGING: {old_mode} → {mode}")
            print(f"{'='*60}")
            
            # Set new mode
            self.app.cross_view_mode = mode
            
            # Get active view
            if not hasattr(self, 'active_view') or self.active_view is None:
                print("   ⚠️ No active view to refresh")
                print(f"{'='*60}\n")
                return
            
            # Mark for rebuild
            setattr(self.app, f'_force_rebuild_view_{self.active_view}', True)
            
            # Clear coordinate cache in classification interactor
            if hasattr(self.app, 'classify_interactor'):
                try:
                    self.app.classify_interactor._invalidate_coord_cache()
                    print("   ✅ Classification coordinate cache cleared")
                except Exception:
                    pass
            
            # Get section data for active view
            core_points = getattr(self.app, f"section_{self.active_view}_core_points", None)
            buffer_points = getattr(self.app, f"section_{self.active_view}_buffer_points", None)
            
            if core_points is None:
                print("   ⚠️ No section data for active view")
                print(f"{'='*60}\n")
                return
            
            print(f"   🔄 Refreshing Cross-Section View {self.active_view + 1}...")
            
            try:
                # Trigger immediate re-render with new view mode
                self._plot_section(core_points, buffer_points, view=mode)
                print(f"   ✅ View {self.active_view + 1} refreshed for {mode} mode")
            except Exception as e:
                print(f"   ❌ View refresh failed: {e}")
                import traceback
                traceback.print_exc()
            
            # Update button states
            self._update_view_buttons(mode)
            
            print(f"{'='*60}\n")
        else:
            # No change
            self.app.cross_view_mode = mode

    def _update_view_buttons(self, mode):
        """Update button states to reflect current view mode"""
        try:
            if hasattr(self.app, 'section_view_buttons') and self.active_view in self.app.section_view_buttons:
                buttons = self.app.section_view_buttons[self.active_view]
                
                # Block signals to prevent recursive calls
                buttons['side'].blockSignals(True)
                buttons['front'].blockSignals(True)
                
                # Update checked state
                buttons['side'].setChecked(mode == 'side')
                buttons['front'].setChecked(mode == 'front')
                
                # Unblock signals
                buttons['side'].blockSignals(False)
                buttons['front'].blockSignals(False)
                
                print(f"   ✅ Updated button states for {mode} mode")
        except Exception as e:
            print(f"   ⚠️ Failed to update button states: {e}")
