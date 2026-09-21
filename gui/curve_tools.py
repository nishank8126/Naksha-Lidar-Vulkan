import numpy as np
import vtk
import time
from PySide6.QtCore import Qt, QEvent, QObject # ✅ ADD QObject
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QColorDialog

class CurveTool(QObject):  # ✅ INHERIT FROM QObject
    """
    MicroStation-style Curve Point Tool
    
    Workflow:
    1. Click "Curve Point" button → Tool activates
    2. Click on canvas → Add point 1
    3. Click on canvas → Add point 2 (line preview)
    4. Click on canvas → Add point 3 (curve preview appears)
    5. Continue clicking → Curve updates in real-time
    6. Press ENTER or right-click → Finalize curve
    7. Press ESC → Cancel
    """
    
    def __init__(self, app):
        super().__init__()  # ✅ CALL QObject.__init__()
        self.app = app
        self.active = False
        self._select_mode = False          # ◀◀◀ ADD THIS
        self.points = []  # List of [x, y, z] clicked points
        
        # VTK actors for visualization
        self.preview_actor = None      # Real-time curve preview (blue)
        self.point_actors = []         # Point markers (red dots)
        self.finalized_actors = []  
        self.dynamic_line_actor = None  # Completed curves (green)
        
        
        # Undo/Redo stacks for point-by-point
        self.undo_stack = []  # Stores removed points
        self.redo_stack = []  # Stores re-added points

        # Undo/Redo stacks for completed curves (whole-operation undo after right-click)
        self.history_stack = []       # List of curve_data after each finalization
        self.history_redo_stack = []  # For redo after undoing a completed curve
        self.max_curve_history = 200

        # Selection
        self.selected_curve = None     # Currently selected curve actor
        self.selected_curve_data = None # Store curve info for editing
        
        from gui.curve_settings_dialog import load_curve_settings
        self.curve_style = load_curve_settings()
        
        self.tension = 0.5             # Catmull-Rom tension (0 = tight, 1 = loose)
        self.samples = 100             # Number of interpolation points

        # ── Endpoint snapping ──────────────────────────────────────────
        self.snap_enabled = True
        self.snap_tolerance_pixels = 14  # screen-space snap radius in pixels
        self.current_snap_target = None  # world-space [x,y,z] of active snap target
        self._snap_indicator_actor = None  # small circle/crosshair drawn at snap point
        self._snap_indicator_target = None
        self._suspended_state = None

        # ── Intercept-snap throttle ─────────────────────────────────
        # Intercept snap is O(n^2) over all segments — too slow for every frame.
        # We cache the last result and only recompute when the cursor has moved
        # more than half a snap-tolerance OR >= _INTERCEPT_INTERVAL_MS have passed.
        self._intercept_cache = None       # cached result: list[3] or None
        self._intercept_last_pos = None    # world pos at last full recompute
        self._intercept_last_ms = 0        # monotonic ms at last full recompute
        self._INTERCEPT_INTERVAL_MS = 80   # ≈12 Hz ceiling for intercept calc
        # ──────────────────────────────────────────────────────────────

        # Live preview must feel like Polyline: cheap, quiet, and bounded.
        # Clicks still use the full world-pick path; mouse-move preview uses
        # this cached plane/Z path and refreshes at most about 60 FPS.
        self._preview_last_screen_pos = None
        self._preview_last_ms = 0
        self._PREVIEW_INTERVAL_MS = 16
        self._cached_fallback_z = None
        self._fallback_z_source_id = None
        self._cached_world_px = None
        self._cached_world_px_camera_mtime = None

        # Plan-view draw plane for Curve.
        # All points in one curve must stay on this single Z plane,
        # exactly like Line / SmartLine. Without this, VTK picking can
        # return mixed Z values from point cloud/SNT/empty space and the
        # curve visually dives below the SNT.
        self._curve_draw_z = None

        self.app.vtk_widget.installEventFilter(self)
        
        print("✅ CurveTool initialized")

    def _get_renderer(self):
        """Get the active renderer, prioritizing digitizer's overlay renderer if available."""
        digitizer = getattr(self.app, 'digitizer', None)
        if digitizer and hasattr(digitizer, 'overlay_renderer') and digitizer.overlay_renderer:
            return digitizer.overlay_renderer
        return self.app.vtk_widget.renderer

    def _remove_prop_safe(self, actor):
        """Safely remove a prop/actor from main, overlay, and digitizer renderers."""
        if actor is None:
            return

        digitizer = getattr(self.app, 'digitizer', None)

        # First try the Digitizer overlay remover, but still continue with
        # RemoveViewProp/RemoveActor/RemoveActor2D below because Curve preview
        # may be a vtkActor2D added through AddViewProp.
        if digitizer is not None and hasattr(digitizer, '_remove_actor_from_overlay'):
            try:
                digitizer._remove_actor_from_overlay(actor)
            except Exception:
                pass

        renderers = []

        try:
            renderers.append(self.app.vtk_widget.renderer)
        except Exception:
            pass

        if digitizer is not None:
            renderers.append(getattr(digitizer, 'overlay_renderer', None))
            renderers.append(getattr(digitizer, 'renderer', None))
            renderers.append(getattr(digitizer, 'text_overlay_renderer', None))

        try:
            renderers.append(self._get_renderer())
        except Exception:
            pass

        seen = set()
        for renderer in renderers:
            if renderer is None:
                continue
            rid = id(renderer)
            if rid in seen:
                continue
            seen.add(rid)

            for method_name in ("RemoveViewProp", "RemoveActor", "RemoveActor2D"):
                method = getattr(renderer, method_name, None)
                if callable(method):
                    try:
                        method(actor)
                    except Exception:
                        pass

    def _add_curve_actor_to_overlay(self, actor):
        """
        Add curve actor using the same overlay path as normal Draw tools.
        This keeps Curve visible above SNT / point cloud.
        """
        if actor is None:
            return

        # Actor2D must be added as a ViewProp.
        # This is required for empty-canvas curves after Clear All.
        if isinstance(actor, vtk.vtkActor2D):
            self._get_renderer().AddViewProp(actor)
            return

        digitizer = getattr(self.app, 'digitizer', None)
        if digitizer is not None and hasattr(digitizer, '_add_actor_to_overlay'):
            try:
                digitizer._add_actor_to_overlay(actor)
                return
            except Exception as e:
                print(f"⚠️ Curve overlay add fallback: {e}")

        self._get_renderer().AddViewProp(actor)
    
    def clear_history(self):
        """
        Clear completed curve history stacks.
        Called when switching to another tool context to prevent
        stale curve undo/redo from intercepting Ctrl+Z/Y.
        """
        if self.history_stack or self.history_redo_stack:
            print(f"   🎨 Clearing curve history: {len(self.history_stack)} items")
        self.history_stack = []
        self.history_redo_stack = []

    def _trim_curve_history(self):
        """Bound completed-curve undo/redo history to prevent long-run growth."""
        max_hist = int(getattr(self, "max_curve_history", 200))
        while len(self.history_stack) > max_hist:
            self.history_stack.pop(0)
        while len(self.history_redo_stack) > max_hist:
            self.history_redo_stack.pop(0)

    def activate(self):
        """Activate the curve drawing tool"""
        if self.active:
            return
        
        self.active = True
        self.points = []
        self._finalizing = False  # ✅ Add flag to prevent double-finalization
        self.current_snap_target = None   # reset snap on each activation
        self._curve_draw_z = None       # reset plan-view Z for the new curve
        self._preview_last_screen_pos = None
        self._preview_last_ms = 0
        # Reset intercept cache so a fresh curve starts clean
        self._intercept_cache = None
        self._intercept_last_pos = None
        self._intercept_last_ms = 0
        
        # ✅ Enable mouse tracking for live preview
        self.app.vtk_widget.setMouseTracking(True)
        
        if hasattr(self.app, 'set_cross_cursor_active'):
            self.app.set_cross_cursor_active(True, "curve")
        
        print("🎯 Curve Point tool ACTIVATED - Click to add points")
        
        if hasattr(self.app, 'statusBar'):
            self.app.statusBar().showMessage(
                "🔮 Curve Point: Click to add points | ENTER = Apply | ESC = Cancel | Right-click = Finish",
                0  # Persistent message
            )

    def resume(self):
        """Resume an in-progress curve without discarding the collected points."""
        if self.active:
            return

        if getattr(self, "_suspended_state", None):
            state = self._suspended_state
            self.points = list(state.get("points", self.points))
            self.redo_stack = list(state.get("redo_stack", self.redo_stack))
            self.current_snap_target = state.get("current_snap_target")
            self._curve_draw_z = state.get("curve_draw_z", self._curve_draw_z)
            self._suspended_state = None

        self.active = True
        self.current_snap_target = None
        self._preview_last_screen_pos = None
        self._preview_last_ms = 0
        self._intercept_cache = None
        self._intercept_last_pos = None
        self._intercept_last_ms = 0

        self.app.vtk_widget.setMouseTracking(True)

        if hasattr(self.app, 'set_cross_cursor_active'):
            self.app.set_cross_cursor_active(True, "curve")

        self._update_preview()
        print("🎯 Curve Point tool RESUMED - continuing in-progress curve")

        if hasattr(self.app, 'statusBar'):
            self.app.statusBar().showMessage(
                f"🔮 Curve Point resumed: {len(self.points)} points | ENTER = Apply | ESC = Cancel | Right-click = Finish",
                0
            )

    def suspend(self):
        """Temporarily pause curve drawing without losing in-progress points."""
        if not self.active and not getattr(self, '_select_mode', False):
            return

        self._suspended_state = {
            "points": list(self.points),
            "redo_stack": list(getattr(self, "redo_stack", [])),
            "current_snap_target": self.current_snap_target,
            "curve_draw_z": getattr(self, "_curve_draw_z", None),
        }

        self.active = False
        self._select_mode = False
        self.current_snap_target = None

        digitizer = getattr(self.app, 'digitizer', None)
        if digitizer is not None:
            try:
                digitizer._hide_snap_marker_now()
            except Exception:
                pass

        if not (digitizer and getattr(digitizer, 'enabled', False)):
            self.app.vtk_widget.setMouseTracking(False)

        if hasattr(self.app, 'set_cross_cursor_active'):
            self.app.set_cross_cursor_active(False, "curve")

        # Preserve the frozen curve preview so the user can see the unfinished
        # shape while temporarily using cross-section or another tool.
        # Only remove the transient cursor-linked bits.
        if self.dynamic_line_actor:
            self._remove_prop_safe(self.dynamic_line_actor)
            self.dynamic_line_actor = None
        self._clear_snap_indicator()
        print("⏸️ Curve Point tool SUSPENDED (in-progress curve preserved)")
            
            
    def _enable_line_stippling(self):
        """Enable OpenGL line stippling for dotted lines"""
        try:
            # Get the render window
            render_window = self.app.vtk_widget.GetRenderWindow()
            
            # Enable line smoothing for better appearance
            render_window.LineSmoothingOn()
            
            print("   ✅ Line stippling enabled")
        except Exception as e:
            print(f"   ⚠️ Could not enable line stippling: {e}")
    
    def deactivate(self, clear_history=False):
        """Deactivate the curve drawing tool"""
        if not self.active:
            return
        
        self.active = False
        self._select_mode = False
        self.current_snap_target = None

        # Hide the digitizer's AccuSnap sphere so it doesn't linger after
        # the curve tool exits.
        digitizer = getattr(self.app, 'digitizer', None)
        if digitizer is not None:
            try:
                digitizer._hide_snap_marker_now()
            except Exception:
                pass

        # ✅ Disable mouse tracking
        digitizer = getattr(self.app, 'digitizer', None)
        if not (digitizer and getattr(digitizer, 'enabled', False)):
            self.app.vtk_widget.setMouseTracking(False)
    
        if hasattr(self.app, 'set_cross_cursor_active'):
            self.app.set_cross_cursor_active(False, "curve")
        
        # Clear preview
        self._clear_preview()
        
        # ✅ Clear history if switching to another tool context
        if clear_history:
            self.clear_history()
        
        print("⏹️ Curve Point tool DEACTIVATED" + 
              (" (history cleared)" if clear_history else " (selection still enabled)"))
        
        if hasattr(self.app, 'statusBar'):
            self.app.statusBar().showMessage(
                "💡 Right-click curves to select | Delete to remove | Shift+E to edit color",
                3000
            )
        
    def eventFilter(self, obj, event):
        """
        Qt-level event filter.
        
        CRITICAL: This runs BEFORE VTK interactor observers (digitizer, etc).
        When no tool is active, we check for grid labels on right-click and
        show the menu directly — preventing the digitizer from swallowing it.
        """
        
        # ══════════════════════════════════════════════════════════════
        # STATE: NO TOOL ACTIVE — grid labels should work
        # ══════════════════════════════════════════════════════════════
        if not self.active and not getattr(self, '_select_mode', False):
            
            # ── RIGHT-CLICK: Grid label check (MUST run before VTK) ──
            if (event.type() == QEvent.MouseButtonPress 
                    and event.button() == Qt.RightButton):
                
                grid_name = self._check_grid_label_at_click(event)
                if grid_name:
                    print(f"   🏷️ Grid label found: '{grid_name}' → showing menu")
                    if hasattr(self.app, 'grid_label_manager'):
                        self.app.grid_label_manager.show_grid_label_menu(grid_name)
                    return True   # ← BLOCK digitizer from seeing this click
                
                # No grid label → let VTK / digitizer handle normally
                return False
            
            # ── Delete/Edit key for previously selected curves ──
            if event.type() == QEvent.KeyPress and self.selected_curve_data is not None:
                if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
                    self._delete_selected_curve()
                    return True
                elif event.key() == Qt.Key_E and event.modifiers() == Qt.ShiftModifier:
                    self._edit_selected_curve_color()
                    return True
            
            return False   # ← everything else passes through
        
        # ══════════════════════════════════════════════════════════════
        # STATE: SELECT MODE — curve selection clicks
        # ══════════════════════════════════════════════════════════════
        if getattr(self, '_select_mode', False) and not self.active:
            
            if event.type() == QEvent.MouseButtonPress:
                if event.button() == Qt.LeftButton:
                    self._select_curve_at_click(event)
                    return True
                elif event.button() == Qt.RightButton:
                    if self.selected_curve_data:
                        self._deselect_curve()
                    return True
            
            if event.type() == QEvent.KeyPress:
                if event.key() == Qt.Key_Escape:
                    self.deactivate_select_mode()
                    return True
                if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
                    if self.selected_curve_data:
                        self._delete_selected_curve()
                        return True
                if event.key() == Qt.Key_E and event.modifiers() == Qt.ShiftModifier:
                    if self.selected_curve_data:
                        self._edit_selected_curve_color()
                        return True
            
            return False
        
        # ══════════════════════════════════════════════════════════════
        # STATE: DRAWING ACTIVE — full curve drawing mode
        # ══════════════════════════════════════════════════════════════
        
        if event.type() == QEvent.MouseMove:
            if len(self.points) > 0:
                self._update_dynamic_preview(event)
            return False
        
        if event.type() == QEvent.MouseButtonPress:
            if event.button() == Qt.LeftButton:
                self._on_left_click(event)
                return True
            elif event.button() == Qt.RightButton:
                self._finalize_curve()
                return True
        
        if event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                self._finalize_curve()
                return True
            elif event.key() == Qt.Key_Escape:
                self._cancel_curve()
                return True
            elif event.key() in (Qt.Key_Backspace, Qt.Key_Delete):
                self._undo_last_point()
                return True
            elif event.key() == Qt.Key_Z and event.modifiers() == Qt.ControlModifier:
                self._undo_last_point()
                return True
            elif event.key() == Qt.Key_Y and event.modifiers() == Qt.ControlModifier:
                self._redo_last_point()
                return True
        
        return False

    def activate_select_mode(self):
        """
        Enter selection mode — user can click curves to select/delete them.
        Activated from the "Select Drawing" button in Draw ribbon.
        """
        # Deactivate drawing mode if active
        if self.active:
            self._cancel_curve()
        
        self._select_mode = True
        self.app.vtk_widget.setCursor(Qt.PointingHandCursor)
        
        print("🎯 Select Drawing mode ACTIVATED — Click curves to select, ESC to exit")
        
        if hasattr(self.app, 'statusBar'):
            self.app.statusBar().showMessage(
                "🎯 Select Drawing: Click to select | Delete to remove | "
                "Shift+E = color | ESC = exit",
                0
            )

    def deactivate_select_mode(self):
        """Exit selection mode — right-clicks now go to grid labels."""
        self._select_mode = False
        
        # Deselect any selected curve
        self._deselect_curve()
        
        # Restore cursor
        self.app.vtk_widget.setCursor(Qt.ArrowCursor)
        
        print("⏹️ Select Drawing mode DEACTIVATED — Grid labels now respond to right-click")
        
        if hasattr(self.app, 'statusBar'):
            self.app.statusBar().showMessage(
                "✅ Selection mode off — Right-click grid labels to load data",
                3000
            )

    def is_any_mode_active(self) -> bool:
        """Check if curve tool has any active mode."""
        return self.active or getattr(self, '_select_mode', False) 

    def _update_dynamic_preview(self, event):
        """Update the dotted line from last point to cursor (Actor2D)"""
        if not self.points:
            return

        pos = event.pos()
        now_ms = int(time.monotonic() * 1000)
        screen_pos = (pos.x(), pos.y())
        if (
            self._preview_last_screen_pos == screen_pos
            and now_ms - self._preview_last_ms < self._PREVIEW_INTERVAL_MS
        ):
            return
        if (
            self._preview_last_screen_pos is not None
            and now_ms - self._preview_last_ms < self._PREVIEW_INTERVAL_MS
        ):
            dx = abs(screen_pos[0] - self._preview_last_screen_pos[0])
            dy = abs(screen_pos[1] - self._preview_last_screen_pos[1])
            if dx <= 1 and dy <= 1:
                return
        self._preview_last_screen_pos = screen_pos
        self._preview_last_ms = now_ms
        
        # Get cursor position
        cursor_world = self._screen_to_world(pos.x(), pos.y(), fast_preview=True)
        
        if cursor_world is None:
            return

        # ── Apply snapping ─────────────────────────────────────────────
        snapped_world = self._apply_snapping(cursor_world)
        snapped_world = self._force_curve_plan_z(snapped_world)
        if snapped_world is None:
            return
        # Show yellow circle only for curve-own endpoint snap.
        # When digitizer AccuSnap is active it shows its own green sphere;
        # _apply_snapping() already called _clear_snap_indicator() in that path.
        if self.current_snap_target is not None and not self._digitizer_snap_active():
            self._draw_snap_indicator(self.current_snap_target)
        elif self._digitizer_snap_active():
            self._clear_snap_indicator()   # green sphere is enough
        else:
            self._clear_snap_indicator()
        # ──────────────────────────────────────────────────────────────
        
        # Create line geometry
        points = vtk.vtkPoints()
        points.SetDataTypeToDouble()
        points.InsertNextPoint(self.points[-1])  # Last clicked
        points.InsertNextPoint(snapped_world)    # Cursor (possibly snapped)
        
        line = vtk.vtkLine()
        line.GetPointIds().SetId(0, 0)
        line.GetPointIds().SetId(1, 1)
        
        cells = vtk.vtkCellArray()
        cells.InsertNextCell(line)
        
        polydata = vtk.vtkPolyData()
        polydata.SetPoints(points)
        polydata.SetLines(cells)
        
        if self.dynamic_line_actor is None:
            mapper = vtk.vtkPolyDataMapper2D()
            coord = vtk.vtkCoordinate()
            coord.SetCoordinateSystemToWorld()
            mapper.SetTransformCoordinate(coord)

            self.dynamic_line_actor = vtk.vtkActor2D()
            self.dynamic_line_actor.SetMapper(mapper)
            self.dynamic_line_actor.GetProperty().SetDisplayLocationToForeground()
            self._get_renderer().AddViewProp(self.dynamic_line_actor)
        else:
            mapper = self.dynamic_line_actor.GetMapper()

        mapper.SetInputData(polydata)

        # Use yellow line when snapping is active, grey otherwise
        if self.current_snap_target is not None:
            self.dynamic_line_actor.GetProperty().SetColor(1.0, 1.0, 0.0)
            self.dynamic_line_actor.GetProperty().SetLineWidth(2)
        else:
            self.dynamic_line_actor.GetProperty().SetColor(0.7, 0.7, 0.7)
            self.dynamic_line_actor.GetProperty().SetLineWidth(1)
        self.app.vtk_widget.render()
    
    def _on_left_click(self, event):
        """Handle left mouse click - add point to curve"""
        # Get 3D coordinates from click position
        pos = event.pos()
        x, y = pos.x(), pos.y()
        
        # Convert screen coordinates to 3D world coordinates
        world_pos = self._screen_to_world(x, y)
        
        if world_pos is None:
            print("⚠️ Could not get 3D coordinates")
            return

        # ── Apply snapping ─────────────────────────────────────────────
        world_pos = self._apply_snapping(world_pos)
        world_pos = self._force_curve_plan_z(world_pos)
        if world_pos is None:
            print("⚠️ Could not get curve plan coordinates")
            return
        snapped = self.current_snap_target is not None
        # Clear snap indicator immediately after the point is committed
        self._clear_snap_indicator()
        self.current_snap_target = None
        # ──────────────────────────────────────────────────────────────
        
        # Add point
        self.points.append(world_pos)
        
        snap_tag = " [SNAPPED]" if snapped else ""
        print(f"📍 Point {len(self.points)} added: ({world_pos[0]:.2f}, {world_pos[1]:.2f}, {world_pos[2]:.2f}){snap_tag}")
        
        # Update visualization
        self._update_preview()
        
        # Update status
        if hasattr(self.app, 'statusBar'):
            self.app.statusBar().showMessage(
                f"🔮 Curve Point: {len(self.points)} points | ENTER = Apply | ESC = Cancel",
                0
            )
    
    def _has_snt_grid_context(self):
        """
        True when an SNT/DXF/DWG grid/attachment is visible in the viewer.
        In this case Curve must draw on the attachment/grid plane, not on
        the loaded LAZ point-cloud surface.
        """
        app = getattr(self, 'app', None)
        if app is None:
            return False

        for attr in (
            'snt_actors', 'dxf_actors', 'dwg_actors',
            'snt_attachments', 'dxf_attachments', 'dwg_attachments',
        ):
            try:
                value = getattr(app, attr, None)
                if value:
                    return True
            except Exception:
                pass
        return False

    def _get_snt_grid_plane_z(self):
        """
        Return the displayed SNT/DXF/DWG grid plane Z.

        Important:
        Do NOT use point-cloud XYZ here. If LAZ is loaded, the cloud picker
        returns LAZ elevation, which makes Curve sit on the point cloud.
        For Curve-over-SNT we first read attachment/grid actors and only then
        fall back to attachment entity coordinates.
        """
        app = getattr(self, 'app', None)
        if app is None:
            return None

        z_vals = []

        # 1) Prefer the currently displayed attachment actors.
        # This matches the visible SNT/DXF/DWG grid plane.
        for store_name in ('snt_actors', 'dxf_actors', 'dwg_actors'):
            try:
                for data in list(getattr(app, store_name, []) or []):
                    if isinstance(data, dict):
                        actors = list(data.get('actors', []) or [])
                    elif isinstance(data, (list, tuple, set)):
                        actors = list(data)
                    else:
                        actors = [data]

                    for actor in actors:
                        if actor is None:
                            continue
                        try:
                            if hasattr(actor, 'GetVisibility') and not actor.GetVisibility():
                                continue
                        except Exception:
                            pass
                        try:
                            bounds = actor.GetBounds()
                        except Exception:
                            bounds = None
                        if not bounds or len(bounds) < 6:
                            continue
                        z0, z1 = bounds[4], bounds[5]
                        try:
                            z0 = float(z0)
                            z1 = float(z1)
                        except Exception:
                            continue
                        if z0 < 1e9 and z1 > -1e9:
                            z_vals.append((z0 + z1) * 0.5)
            except Exception:
                pass

        if z_vals:
            return float(np.median(z_vals))

        # 2) Fallback to attachment entity vertices if actor bounds are not useful.
        z_vals = []
        for store_name in ('snt_attachments', 'dxf_attachments', 'dwg_attachments'):
            try:
                for att in list(getattr(app, store_name, []) or []):
                    if not isinstance(att, dict):
                        continue
                    for ent in list(att.get('entities', []) or []):
                        if not isinstance(ent, dict):
                            continue
                        pts = (
                            ent.get('points')
                            or ent.get('vertices')
                            or ent.get('coords')
                            or ent.get('coordinates')
                            or []
                        )
                        for pt in pts:
                            try:
                                if pt is not None and len(pt) > 2:
                                    z_vals.append(float(pt[2]))
                            except Exception:
                                pass
            except Exception:
                pass

        if z_vals:
            return float(np.median(z_vals))

        return None

    def _get_fallback_z(self):
        """
        Curve draw plane Z.

        If SNT/DXF/DWG is loaded, Curve must stay on that grid/attachment
        plane even when a LAZ point cloud is loaded below/above it. Only when
        no attachment grid exists do we fall back to the normal digitizer / LAZ
        fallback height.
        """
        if self._has_snt_grid_context():
            snt_z = self._get_snt_grid_plane_z()
            if snt_z is not None:
                return float(snt_z)

            # SNT exists but no valid Z is available. Most SNT/DXF linework is
            # drawn on a 2D plan plane, so use 0 instead of point-cloud Z.
            return 0.0

        digitizer = getattr(self.app, 'digitizer', None)
        if digitizer is not None and hasattr(digitizer, '_get_fallback_z_height'):
            try:
                return float(digitizer._get_fallback_z_height())
            except Exception:
                pass

        data = getattr(self.app, 'data', None)
        xyz = data.get('xyz') if isinstance(data, dict) else None
        source_id = id(xyz) if xyz is not None else None

        if self._fallback_z_source_id == source_id and self._cached_fallback_z is not None:
            return self._cached_fallback_z

        avg_z = 0.0
        if xyz is not None:
            try:
                avg_z = float(np.nanmean(xyz[:, 2]))
            except Exception:
                avg_z = 0.0

        self._fallback_z_source_id = source_id
        self._cached_fallback_z = avg_z
        return avg_z

    def _force_curve_plan_z(self, world_pos):
        """
        Keep all Curve points on one plan-view Z plane.

        Line/SmartLine already do this in DigitizeManager by forcing Z to
        the first point/fallback height. Curve must do the same, otherwise
        vtkPropPicker may return point-cloud Z for one click, SNT Z for
        another click, and 0 for empty space. That mixed Z makes the curve
        appear to go down below the SNT.
        """
        if world_pos is None:
            return None

        pt = list(world_pos)
        if len(pt) < 3:
            pt = [float(pt[0]), float(pt[1]), float(self._get_fallback_z())]

        if getattr(self.app, 'is_3d_mode', False):
            return pt

        if getattr(self, '_curve_draw_z', None) is None:
            if self.points and len(self.points[0]) >= 3:
                self._curve_draw_z = float(self.points[0][2])
            else:
                self._curve_draw_z = float(self._get_fallback_z())

        pt[2] = float(self._curve_draw_z)
        return pt

    def _screen_to_world(self, screen_x, screen_y, fast_preview=False):
        """
        Convert screen coordinates to world coordinates.

        When SNT/DXF/DWG grid is loaded, Curve must draw on the visible grid
        plane. So we intentionally DO NOT pick the LAZ/point-cloud actor in
        that mode. We project the mouse position to the SNT grid plane instead.
        This is what prevents Curve from appearing on the LAZ surface.
        """
        try:
            renderer = self.app.vtk_widget.renderer
            render_window = self.app.vtk_widget.GetRenderWindow()
            window_size = render_window.GetSize()

            # VTK uses bottom-left origin, Qt uses top-left.
            vtk_y = window_size[1] - screen_y

            # SNT/DXF/DWG mode: always use attachment-grid plane, not cloud pick.
            if self._has_snt_grid_context() and not getattr(self.app, 'is_3d_mode', False):
                world_pos = self._project_to_plane(
                    screen_x,
                    vtk_y,
                    self._get_fallback_z(),
                    renderer,
                )
                return self._force_curve_plan_z(world_pos)

            # Normal mode without SNT/DXF/DWG: keep old behavior.
            if fast_preview:
                return self._force_curve_plan_z(self._project_to_plane(
                    screen_x, vtk_y, self._get_fallback_z(), renderer
                ))

            picker = vtk.vtkPropPicker()
            picker.Pick(screen_x, vtk_y, 0, renderer)
            world_pos = picker.GetPickPosition()

            if world_pos == (0, 0, 0):
                world_pos = self._project_to_plane(
                    screen_x,
                    vtk_y,
                    self._get_fallback_z(),
                    renderer,
                )

            return self._force_curve_plan_z(world_pos)

        except Exception as e:
            print(f"❌ Screen to world conversion failed: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _project_to_plane(self, screen_x, screen_y, z_plane, renderer):
        """Project screen coordinates onto a horizontal plane at given Z"""
        # Get camera
        camera = renderer.GetActiveCamera()
        
        # Create coordinate converter
        coord = vtk.vtkCoordinate()
        coord.SetCoordinateSystemToDisplay()
        coord.SetValue(screen_x, screen_y, 0)
        
        # Convert to world coordinates
        world_pos = coord.GetComputedWorldValue(renderer)
        
        # Adjust Z to plane
        return (world_pos[0], world_pos[1], z_plane)
    
    def _update_preview(self):
        """Update the real-time curve preview (NOT the dynamic line to cursor)"""
        # Clear old CURVE preview only (keep dynamic line separate)
        if self.preview_actor:
            self._remove_prop_safe(self.preview_actor)
            self.preview_actor = None
        
        # Clear point markers
        for actor in self.point_actors:
            self._remove_prop_safe(actor)
        self.point_actors = []
        
        num_points = len(self.points)
        
        if num_points == 0:
            return
        
        # Point markers are no longer drawn per user request
        
        # ✅ ONLY draw the smooth curve if we have 3+ points
        # Do NOT draw line preview for 2 points - let dynamic line handle that
        if num_points >= 2:
            self._draw_curve_preview()
        
        # Render
        self.app.vtk_widget.render()
    
    # Point marker logic removed per user request
   
    def _draw_curve_preview(self):
        """Draw smooth Catmull-Rom spline preview (Actor2D)"""
        curve_points = self._interpolate_catmull_rom(self.points)
        
        if curve_points is None or len(curve_points) < 2:
            return
        
        # Create VTK points
        vtk_points = vtk.vtkPoints()
        vtk_points.SetDataTypeToDouble()
        for pt in curve_points:
            vtk_points.InsertNextPoint(pt)
        
        # Create polyline
        polyline = vtk.vtkPolyLine()
        polyline.GetPointIds().SetNumberOfIds(len(curve_points))
        for i in range(len(curve_points)):
            polyline.GetPointIds().SetId(i, i)
        
        cells = vtk.vtkCellArray()
        cells.InsertNextCell(polyline)
        
        polydata = vtk.vtkPolyData()
        polydata.SetPoints(vtk_points)
        polydata.SetLines(cells)
        
        # Mapper 2D with World Coordinates
        mapper = vtk.vtkPolyDataMapper2D()
        mapper.SetInputData(polydata)
        
        coord = vtk.vtkCoordinate()
        coord.SetCoordinateSystemToWorld()
        mapper.SetTransformCoordinate(coord)
        
        self.preview_actor = vtk.vtkActor2D()
        self.preview_actor.SetMapper(mapper)
        
        # solid curve, render on top using user settings
        color = self.curve_style.get('color', (0, 1, 1))
        width = self.curve_style.get('width', 2)
        self.preview_actor.GetProperty().SetColor(*color) 
        self.preview_actor.GetProperty().SetLineWidth(width)
        self.preview_actor.GetProperty().SetDisplayLocationToForeground()
        
        # Use same overlay path as normal Draw tools
        self._add_curve_actor_to_overlay(self.preview_actor)
    
    def _interpolate_catmull_rom(self, control_points):
        """
        Interpolate smooth Catmull-Rom spline through control points
        Returns array of interpolated points
        """
        if len(control_points) < 2:
            return None
        
        control_points = np.array(control_points)
        num_segments = len(control_points) - 1
        points_per_segment = 20  # constant per segment (smooth)        
        interpolated = []
        
        for i in range(num_segments):
            # Get control points for this segment
            p0 = control_points[max(0, i - 1)]
            p1 = control_points[i]
            p2 = control_points[i + 1]
            p3 = control_points[min(len(control_points) - 1, i + 2)]
            
            # Interpolate this segment
            for j in range(points_per_segment):
                t = j / points_per_segment
                pt = self._catmull_rom_point(p0, p1, p2, p3, t)
                interpolated.append(pt)
        
        # Add final point
        interpolated.append(control_points[-1])
        
        return np.array(interpolated)
    
    def _catmull_rom_point(self, p0, p1, p2, p3, t):
        """Calculate single point on Catmull-Rom spline"""
        t2 = t * t
        t3 = t2 * t
        
        # Catmull-Rom basis matrix
        result = 0.5 * (
            (2 * p1) +
            (-p0 + p2) * t +
            (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2 +
            (-p0 + 3 * p1 - 3 * p2 + p3) * t3
        )
        
        return result
    
    def _clear_preview(self):
        """Remove all preview actors using RemoveViewProp"""
        if self.preview_actor:
            self._remove_prop_safe(self.preview_actor)
            self.preview_actor = None
        
        if self.dynamic_line_actor:
            self._remove_prop_safe(self.dynamic_line_actor)
            self.dynamic_line_actor = None
        
        for actor in self.point_actors:
            self._remove_prop_safe(actor)
        self.point_actors = []

        # Clear snap indicator
        self._clear_snap_indicator()
    
    # ══════════════════════════════════════════════════════════════════
    # SNAPPING HELPERS
    # ══════════════════════════════════════════════════════════════════
    #
    # Priority order when the curve tool is active:
    #
    #   1. Digitizer AccuSnap  (nearby / center / keypoint / midpoint /
    #      intercept) — delegates directly to digitizer._snap_point()
    #      when the digitizer has snap_enabled=True and a snap_mode set.
    #      The digitizer's own green sphere marker is shown by _snap_point
    #      automatically; we hide our yellow circle in this path.
    #
    #   2. Curve-own endpoint snap  (fallback when digitizer snap is OFF
    #      or not available) — snaps to self.points[0] when ≥ 3 points
    #      exist (closed-curve helper), then to any other non-last control
    #      point.  Shows our yellow circle.
    #
    #   3. Raw cursor coordinates  (no snap active).
    # ══════════════════════════════════════════════════════════════════

    def _digitizer_snap_active(self):
        """Return True when the digitizer's AccuSnap is enabled and has a mode set."""
        digitizer = getattr(self.app, 'digitizer', None)
        if digitizer is None:
            return False
        return (
            getattr(digitizer, 'snap_enabled', False)
            and getattr(digitizer, 'snap_mode', None) is not None
        )

    def _apply_digitizer_snap(self, world_pos):
        """
        Delegate to digitizer._snap_point() so the curve tool respects the
        same Nearby / Center / Key Point / Mid Point / Intercept logic that
        all other draw tools use.

        Special handling for 'intercept' mode:
        ──────────────────────────────────────
        Intercept snap is O(n²) over all drawing segments — with many drawings
        on screen this makes every mouse-move stutter.  We apply two guards:

          1. TIME THROTTLE  — recompute at most every _INTERCEPT_INTERVAL_MS ms
             (≈ 12 Hz).  Between recomputes we return the cached result.

          2. SPATIAL PRE-FILTER (inside the throttle window) — if the cursor
             hasn't moved more than half the snap tolerance since the last full
             recompute we reuse the cache without even starting a new one.

        For all other snap modes (nearby / center / keypoint / midpoint) we
        delegate to the digitizer directly on every frame — they are O(n·k)
        with small constants and are not the bottleneck.
        """
        digitizer = getattr(self.app, 'digitizer', None)
        if digitizer is None:
            return world_pos

        snap_mode = getattr(digitizer, 'snap_mode', None)

        # ── Fast path: intercept snap with throttle ────────────────────
        if snap_mode == 'intercept':
            return self._apply_intercept_snap_throttled(world_pos, digitizer)

        # ── All other modes: run every frame (fast enough) ────────────
        try:
            pos_arr = np.array(world_pos, dtype=np.float64)
            snapped = digitizer._snap_point(pos_arr)
            if snapped is not None:
                return list(snapped)
        except Exception as e:
            print(f"   ⚠️ Curve digitizer snap error: {e}")
        return world_pos

    def _apply_intercept_snap_throttled(self, world_pos, digitizer):
        """
        Throttled intercept-snap computation for the curve tool.

        Algorithm
        ─────────
        • Compute a world-space move-threshold = half the snap tolerance.
        • If the cursor moved less than that threshold since the last full
          compute → return cached result immediately (0 ms).
        • Otherwise check elapsed wall-clock time.  If < _INTERCEPT_INTERVAL_MS
          and we already have a cache → return cached result.
        • Otherwise run a spatially pre-filtered intercept search:
            - Only segments whose bounding box is within (snap_tol * 3) of the
              cursor are included.  This trims the pair-wise set from O(all²)
              to O(local²), which is usually ≤ 5–10 segments even in dense files.
          Update the cache and timestamp.

        The digitizer's green sphere marker is updated after each real compute.
        """
        import time as _time

        pos_arr = np.array(world_pos, dtype=np.float64)

        # ── Move threshold (lazy, computed once per zoom level) ───────
        try:
            tol_world = digitizer._screen_to_world_tolerance(
                getattr(digitizer, '_snap_screen_tol_px', 25)
            )
        except Exception:
            tol_world = 1.0
        move_thr = max(tol_world * 0.5, 1e-3)

        # ── Check spatial delta ───────────────────────────────────────
        if self._intercept_last_pos is not None:
            delta = np.linalg.norm(pos_arr[:2] - np.array(self._intercept_last_pos[:2]))
            if delta < move_thr:
                # Cursor barely moved — reuse cache, update marker visibility only
                self._update_intercept_marker(self._intercept_cache, digitizer, tol_world)
                return list(self._intercept_cache) if self._intercept_cache is not None else world_pos

        # ── Check time throttle ───────────────────────────────────────
        now_ms = int(_time.monotonic() * 1000)
        elapsed = now_ms - self._intercept_last_ms
        if elapsed < self._INTERCEPT_INTERVAL_MS and self._intercept_last_pos is not None:
            self._update_intercept_marker(self._intercept_cache, digitizer, tol_world)
            return list(self._intercept_cache) if self._intercept_cache is not None else world_pos

        # ── Full recompute with spatial pre-filter ────────────────────
        snap_tol_search = tol_world * 3.0   # generous AABB search radius

        # Build segment list: only segments whose AABB touches the search box
        all_segments = []
        px, py = float(pos_arr[0]), float(pos_arr[1])
        for d in getattr(digitizer, 'drawings', []):
            coords = d.get('coords') or []
            for i in range(len(coords) - 1):
                ax, ay = float(coords[i][0]),     float(coords[i][1])
                bx, by = float(coords[i + 1][0]), float(coords[i + 1][1])
                # AABB check — skip segments far from the cursor
                if (max(ax, bx) < px - snap_tol_search or
                        min(ax, bx) > px + snap_tol_search or
                        max(ay, by) < py - snap_tol_search or
                        min(ay, by) > py + snap_tol_search):
                    continue
                all_segments.append((
                    np.array(coords[i],     dtype=np.float64),
                    np.array(coords[i + 1], dtype=np.float64),
                ))

        # O(local²) intersection search
        best_ix_dist = None
        ix_target    = None
        screen_tol   = getattr(digitizer, '_snap_screen_tol_px', 25)
        n = len(all_segments)
        for a in range(n):
            p1, p2 = all_segments[a]
            for b in range(a + 1, n):
                p3, p4 = all_segments[b]
                try:
                    ix = digitizer._segment_intersection_2d(p1, p2, p3, p4)
                except Exception:
                    continue
                if ix is None:
                    continue
                try:
                    screen_dist = digitizer._world_to_screen_distance(ix, pos_arr)
                except Exception:
                    screen_dist = np.linalg.norm(ix[:2] - pos_arr[:2])
                if screen_dist >= screen_tol:
                    continue
                world_dist = np.linalg.norm(ix[:2] - pos_arr[:2])
                if best_ix_dist is None or world_dist < best_ix_dist:
                    best_ix_dist = world_dist
                    ix_target    = ix

        result = list(ix_target) if ix_target is not None else None

        # ── Update cache and timestamp ────────────────────────────────
        self._intercept_cache    = result
        self._intercept_last_pos = list(pos_arr)
        self._intercept_last_ms  = now_ms

        self._update_intercept_marker(result, digitizer, tol_world)
        return result if result is not None else world_pos

    def _update_intercept_marker(self, snap_target, digitizer, marker_world_r):
        """Show or hide the digitizer's green sphere at snap_target."""
        marker = getattr(digitizer, '_snap_marker', None)
        if marker is None:
            return
        try:
            if snap_target is not None:
                r = marker_world_r * 0.6
                marker.SetPosition(float(snap_target[0]),
                                   float(snap_target[1]),
                                   float(snap_target[2]))
                marker.SetScale(r, r, r)
                marker.SetVisibility(True)
            else:
                marker.SetVisibility(False)
        except Exception:
            pass

    def _world_snap_tolerance(self):
        """
        Convert snap_tolerance_pixels to a world-space distance.
        Used only by the curve-own endpoint-snap fallback path.
        Falls back to inf (= snap never fires) when the renderer is not ready.
        """
        try:
            digitizer = getattr(self.app, 'digitizer', None)
            if digitizer is not None and hasattr(digitizer, '_screen_to_world_tolerance'):
                return digitizer._screen_to_world_tolerance(self.snap_tolerance_pixels)

            px_world = self._world_units_per_screen_pixel()
            return px_world * self.snap_tolerance_pixels if px_world > 0 else float('inf')
        except Exception:
            return float('inf')

    def _world_units_per_screen_pixel(self):
        try:
            renderer = self.app.vtk_widget.renderer
            camera = renderer.GetActiveCamera()
            camera_mtime = camera.GetMTime() if camera is not None else None
            if (
                self._cached_world_px is not None
                and self._cached_world_px_camera_mtime == camera_mtime
            ):
                return self._cached_world_px

            render_window = self.app.vtk_widget.GetRenderWindow()
            w, h = render_window.GetSize()
            if w == 0 or h == 0:
                return 0.0
            cx, cy = w // 2, h // 2
            z = self._get_fallback_z()
            p1 = self._project_to_plane(cx, cy, z, renderer)
            p2 = self._project_to_plane(cx + 1, cy, z, renderer)
            px_world = float(np.linalg.norm(np.array(p2) - np.array(p1)))
            self._cached_world_px = px_world
            self._cached_world_px_camera_mtime = camera_mtime
            return px_world
        except Exception:
            return 0.0

    def _get_endpoint_snap_target(self, world_pos):
        """
        Curve-own endpoint snap (fallback path — used when digitizer snap is OFF).

        Priority:
          1. self.points[0] when ≥ 3 points exist  →  clean closed curves.
          2. Any other non-last control point.

        Returns a list[3] target or None.
        """
        if not self.snap_enabled:
            return None
        if not self.points or world_pos is None:
            return None

        tol = self._world_snap_tolerance()
        if tol == float('inf'):
            return None

        wp = np.array(world_pos[:2])

        # Priority 1 — first point (closed-curve snap)
        if len(self.points) >= 3:
            p0 = np.array(self.points[0][:2])
            if np.linalg.norm(wp - p0) <= tol:
                return list(self.points[0])

        # Priority 2 — any other non-last control point
        last_idx = len(self.points) - 1
        best_dist = tol
        best_pt = None
        for idx, pt in enumerate(self.points):
            if idx == last_idx:
                continue
            d = np.linalg.norm(wp - np.array(pt[:2]))
            if d < best_dist:
                best_dist = d
                best_pt = list(pt)
        return best_pt

    def _apply_snapping(self, world_pos):
        """
        Master snap entry point called by both _update_dynamic_preview and
        _on_left_click.

        1. If the digitizer AccuSnap is active → delegate to it entirely.
           current_snap_target is set to the snapped pos when it moved, or
           None when digitizer returned the same pos unchanged.
        2. Otherwise → use the curve-own endpoint snap.

        Always updates self.current_snap_target and returns the final coord.
        """
        if self._digitizer_snap_active():
            snapped = self._apply_digitizer_snap(world_pos)
            # Determine whether a real snap occurred (position changed)
            moved = (
                np.linalg.norm(
                    np.array(snapped[:2]) - np.array(world_pos[:2])
                ) > 1e-9
            )
            self.current_snap_target = snapped if moved else None
            # Hide our own yellow circle — digitizer shows its green sphere
            self._clear_snap_indicator()
            return snapped

        # Fallback: curve-own endpoint snap
        snap = self._get_endpoint_snap_target(world_pos)
        self.current_snap_target = snap
        return snap if snap is not None else world_pos

    # ── Snap indicator (yellow circle, shown only in fallback path) ─────

    def _clear_snap_indicator(self):
        """Remove the snap-indicator actor from the renderer."""
        if self._snap_indicator_actor is not None:
            self._remove_prop_safe(self._snap_indicator_actor)
            self._snap_indicator_actor = None
        self._snap_indicator_target = None

    def _draw_snap_indicator(self, world_pos):
        """
        Draw a small bright yellow circle around *world_pos* to signal the
        curve-own endpoint snap is active.  Not shown when the digitizer's
        green sphere is already visible.
        """
        if world_pos is None:
            self._clear_snap_indicator()
            return

        target_key = tuple(round(float(v), 6) for v in world_pos[:3])
        if self._snap_indicator_actor is not None and self._snap_indicator_target == target_key:
            return

        self._clear_snap_indicator()

        cx, cy, cz = world_pos[0], world_pos[1], world_pos[2]

        radius = max(self._world_units_per_screen_pixel() * 8.0, 1e-6)

        n = 16
        vtk_pts = vtk.vtkPoints()
        vtk_pts.SetDataTypeToDouble()
        for i in range(n):
            angle = 2.0 * np.pi * i / n
            vtk_pts.InsertNextPoint(cx + radius * np.cos(angle),
                                    cy + radius * np.sin(angle), cz)
        vtk_pts.InsertNextPoint(*vtk_pts.GetPoint(0))

        polyline = vtk.vtkPolyLine()
        polyline.GetPointIds().SetNumberOfIds(n + 1)
        for i in range(n + 1):
            polyline.GetPointIds().SetId(i, i)

        cells = vtk.vtkCellArray()
        cells.InsertNextCell(polyline)

        poly = vtk.vtkPolyData()
        poly.SetPoints(vtk_pts)
        poly.SetLines(cells)

        mapper = vtk.vtkPolyDataMapper2D()
        mapper.SetInputData(poly)
        coord = vtk.vtkCoordinate()
        coord.SetCoordinateSystemToWorld()
        mapper.SetTransformCoordinate(coord)

        actor = vtk.vtkActor2D()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(1.0, 1.0, 0.0)   # bright yellow
        actor.GetProperty().SetLineWidth(2)
        actor.GetProperty().SetDisplayLocationToForeground()

        self._snap_indicator_actor = actor
        self._snap_indicator_target = target_key
        self._get_renderer().AddViewProp(actor)

    # ══════════════════════════════════════════════════════════════════

    def _finalize_curve(self):
        """Finalize the curve (ENTER or right-click)"""
        # ✅ Prevent double-finalization from multiple events
        if hasattr(self, '_finalizing') and self._finalizing:
            return
        
        # ✅ Guard against insufficient points
        if len(self.points) < 2:
            if len(self.points) > 0:
                print("⚠️ Need at least 2 points to create a curve")
            return
        
        # ✅ Set flag immediately
        self._finalizing = True
        
        print(f"✅ Finalizing curve with {len(self.points)} points")
        
        # ✅ Save points before clearing
        points_to_save = self.points.copy()
        
        # ✅ Clear points IMMEDIATELY to prevent re-entry
        self.points = []
        
        # Create final curve actor using settings
        curve_points = self._interpolate_catmull_rom(points_to_save)
        
        if curve_points is not None:
            color = self.curve_style.get('color', (0, 1, 0))
            width = self.curve_style.get('width', 2)
            actor = self._create_curve_actor(curve_points, color=color, width=width)

            # ✅ Store curve data with actor for later editing
            curve_data = {
                'actor': actor,
                'control_points': points_to_save.copy(),
                'interpolated': curve_points.copy(),
                'coords': [tuple(pt) for pt in curve_points],
                'type': 'curve',
                'source': 'curve_tool',
                'color': color,  # Default or custom color
                'original_color': color,
                'original_width': width,
            }

            # Commit to digitizer's unified undo stack BEFORE adding the actor so
            # Ctrl+Z on any draw tool correctly reverts the canvas to pre-curve state.
            digitizer = getattr(self.app, 'digitizer', None)
            if digitizer and hasattr(digitizer, '_save_state'):
                digitizer._save_state()

            self.finalized_actors.append(curve_data)
            if digitizer:
                digitizer.drawings.append(curve_data)
                if hasattr(digitizer, '_emit_drawing_finalized'):
                    digitizer._emit_drawing_finalized(curve_data)
            self._add_curve_actor_to_overlay(actor)

        # Save to history for whole-operation undo/redo after completion
        if curve_points is not None:
            self.history_stack.append(curve_data)
            self.history_redo_stack.clear()
            self._trim_curve_history()
        
        # Clear preview and reset
        self._clear_preview()
        self.current_snap_target = None
        self._curve_draw_z = None

        # Hide digitizer AccuSnap sphere — it may have been visible during drawing
        digitizer_ref = getattr(self.app, 'digitizer', None)
        if digitizer_ref is not None:
            try:
                digitizer_ref._hide_snap_marker_now()
            except Exception:
                pass
        
        # Render
        # Render
        self.app.vtk_widget.render()

        # ✅ Reset flag after a short delay
        from PySide6.QtCore import QTimer
        QTimer.singleShot(100, lambda: setattr(self, '_finalizing', False))

        # Keep tool active for persistence (do not deactivate)
        if hasattr(self.app, 'statusBar'):
            self.app.statusBar().showMessage(
                "🔮 Curve created! Click to start next curve | ENTER = Apply | ESC = Cancel",
                0
            )
    
    def _cancel_curve(self):
        """Cancel current curve and deactivate tool (ESC)"""
        print("❌ Curve cancelled - deactivating tool")
        
        self._clear_preview()
        self.points = []
        self._curve_draw_z = None
        
        # ✅ Deactivate the tool completely
        self.deactivate()
        
        self.app.vtk_widget.render()
        
        if hasattr(self.app, 'statusBar'):
            self.app.statusBar().showMessage(
                "❌ Curve tool deactivated",
                2000
            )
        
    def _undo_last_point(self):
        """Remove the last added point (Backspace/Delete)"""
        if not self.points:
            return

        removed = self.points.pop()
        self.redo_stack.append(removed)  # Save for redo
        # Cap redo stack (point entries are small, but prevent unbounded growth)
        if len(self.redo_stack) > 200:
            self.redo_stack.pop(0)

        print(f"↶ Undo point: ({removed[0]:.2f}, {removed[1]:.2f}, {removed[2]:.2f})")

        self._update_preview()

        if hasattr(self.app, 'statusBar'):
            self.app.statusBar().showMessage(
                f"↶ Undo: {len(self.points)} points | Ctrl+Y to redo",
                2000
            )

    def _redo_last_point(self):
        """Re-add the last removed point (Ctrl+Y)"""
        if not self.redo_stack:
            print("Nothing to redo")
            return

        restored = self.redo_stack.pop()
        self.points.append(restored)

        print(f"↷ Redo point: ({restored[0]:.2f}, {restored[1]:.2f}, {restored[2]:.2f})")

        self._update_preview()

        if hasattr(self.app, 'statusBar'):
            self.app.statusBar().showMessage(
                f"↷ Redo: {len(self.points)} points",
                2000
            )
               
    def _select_curve_at_click(self, event):
        """Select a curve by clicking on it"""
        pos = event.pos()
        x, y = pos.x(), pos.y()

        print(f"🔍 Curve tool: Checking for curve at ({x}, {y})")

        render_window = self.app.vtk_widget.GetRenderWindow()
        window_size = render_window.GetSize()
        vtk_y = window_size[1] - y

        print(f"   📐 Window size: {window_size}, VTK Y: {vtk_y}")

        # Try vtkCellPicker first (best for lines)
        cell_picker = vtk.vtkCellPicker()
        cell_picker.SetTolerance(0.02)
        success = cell_picker.Pick(x, vtk_y, 0, self._get_renderer())
        picked_actor = cell_picker.GetActor()

        # ──────────────────────────────────────────────────────────────
        # 🚫  SKIP GRID LABELS  (robust — survives VTK wrapper mismatch)
        # ──────────────────────────────────────────────────────────────
        if picked_actor:
            # Direct attribute check
            if hasattr(picked_actor, 'is_grid_label') and picked_actor.is_grid_label:
                print("   🔵 Clicked grid label (direct) - skipping curve tool")
                self.selected_curve_data = None
                return

            # Position-based match against known grid labels
            picked_pos    = picked_actor.GetPosition()
            picked_bounds = picked_actor.GetBounds()
            for store_name in ('snt_actors', 'dxf_actors'):
                for data in getattr(self.app, store_name, []):
                    for actor in data.get('actors', []):
                        if hasattr(actor, 'is_grid_label') and actor.is_grid_label:
                            try:
                                if (actor.GetPosition() == picked_pos and
                                        actor.GetBounds() == picked_bounds):
                                    print("   🔵 Clicked grid label (matched) - skipping curve tool")
                                    self.selected_curve_data = None
                                    return
                            except Exception:
                                pass

        if not picked_actor:
            print("   🔄 Trying PropPicker as fallback...")
            prop_picker = vtk.vtkPropPicker()
            prop_picker.Pick(x, vtk_y, 0, self._get_renderer())
            picked_actor = prop_picker.GetActor()
            print(f"   🎯 PropPicker actor: {picked_actor}")

        if not picked_actor:
            print("   ⚪ No actor picked - allowing digitizer to handle")
            self.selected_curve_data = None
            return

        print(f"   📋 Total finalized curves: {len(self.finalized_actors)}")

        # Check if picked actor is one of our curves
        for i, curve_data in enumerate(self.finalized_actors):
            actor = curve_data['actor'] if isinstance(curve_data, dict) else curve_data
            if actor is picked_actor:
                print(f"   ✅ MATCH! Selecting curve {i}")
                self._select_curve(curve_data)
                return

        print("   ⚪ Picked actor is not a curve - allowing digitizer to handle")
        self.selected_curve_data = None

    def _check_grid_label_at_click(self, event):
        """
        Check if a grid label exists at the click position.
        Returns grid_name string if found, None otherwise.
        
        Uses multiple picker strategies to handle VTK's Python wrapper
        mismatch (where GetActor() returns a different wrapper that lost
        custom attributes like is_grid_label).
        """
        try:
            x, y = event.pos().x(), event.pos().y()
            render_window = self.app.vtk_widget.GetRenderWindow()
            window_size = render_window.GetSize()
            vtk_y = window_size[1] - y
            renderer = self.app.vtk_widget.renderer

            # ── Method 1: AreaPicker (returns original Python objects) ──
            area_picker = vtk.vtkAreaPicker()
            area_picker.AreaPick(
                x - 12, vtk_y - 12, x + 12, vtk_y + 12, renderer
            )

            for prop in area_picker.GetProp3Ds():
                if hasattr(prop, 'is_grid_label') and prop.is_grid_label:
                    grid_name = getattr(prop, 'grid_name', '')
                    if grid_name:
                        return grid_name

            # ── Method 2: PropPicker + match against known actors ──────
            prop_picker = vtk.vtkPropPicker()
            prop_picker.Pick(x, vtk_y, 0, renderer)
            picked = prop_picker.GetActor()

            if picked is None:
                return None

            # Direct attribute check
            if hasattr(picked, 'is_grid_label') and picked.is_grid_label:
                return getattr(picked, 'grid_name', '')

            # ── Method 3: Match by position/bounds with all known labels ─
            try:
                picked_pos = picked.GetPosition()
                picked_bounds = picked.GetBounds()
            except Exception:
                return None

            for store_name in ('snt_actors', 'dxf_actors'):
                for data in getattr(self.app, store_name, []):
                    for actor in data.get('actors', []):
                        if not (hasattr(actor, 'is_grid_label') and actor.is_grid_label):
                            continue
                        try:
                            if (actor.GetPosition() == picked_pos
                                    and actor.GetBounds() == picked_bounds):
                                gn = getattr(actor, 'grid_name', '')
                                if gn:
                                    return gn
                        except Exception:
                            pass

        except Exception as exc:
            print(f"   ⚠️ Grid label check failed: {exc}")

        return None

    def _select_curve(self, curve_data):
        """Highlight selected curve"""
        # Deselect previous
        self._deselect_curve()
        
        # Select new
        self.selected_curve_data = curve_data
        actor = curve_data['actor'] if isinstance(curve_data, dict) else curve_data
        self.selected_curve = actor
        
        # Highlight with yellow color
        actor.GetProperty().SetColor(1, 1, 0)  # Yellow
        actor.GetProperty().SetLineWidth(4)    # Thicker (increased from 3)
        
        self.app.vtk_widget.render()
        
        # ✅ Show status message
        if hasattr(self.app, 'statusBar'):
            self.app.statusBar().showMessage(
                "🎯 Curve selected | Delete to remove | Shift+E to change color | Right-click again to deselect",
                5000
            )
        
        print("✅ Curve selected - Press Delete to remove, Shift+E to change color")

    def _deselect_curve(self):
        """Remove selection highlight"""
        if self.selected_curve and self.selected_curve_data:
            # Restore original color
            if isinstance(self.selected_curve_data, dict):
                color = self.selected_curve_data.get('color', (0, 1, 0))
                self.selected_curve.GetProperty().SetColor(*color)
                self.selected_curve.GetProperty().SetLineWidth(2)
            
            self.app.vtk_widget.render()
        
        self.selected_curve = None
        self.selected_curve_data = None
        
    def _delete_selected_curve(self):
        """Delete the currently selected curve"""
        if not self.selected_curve_data:
            print("⚠️ No curve selected")
            return
        
        try:
            # Save state to undo stack before deleting
            digitizer = getattr(self.app, 'digitizer', None)
            if digitizer and hasattr(digitizer, '_save_state'):
                digitizer._save_state()

            # Remove actor from renderer
            actor = self.selected_curve_data['actor']
            self._remove_curve_actor(actor)
            
            # Remove from finalized list
            self.finalized_actors.remove(self.selected_curve_data)
            
            # Also remove from digitizer drawings
            if digitizer and self.selected_curve_data in digitizer.drawings:
                digitizer.drawings.remove(self.selected_curve_data)
                if hasattr(digitizer, '_emit_drawing_removed'):
                    digitizer._emit_drawing_removed(self.selected_curve_data)

            # Clear selection
            self.selected_curve = None
            self.selected_curve_data = None
            
            # Render
            self.app.vtk_widget.render()
            
            if hasattr(self.app, 'statusBar'):
                self.app.statusBar().showMessage("🗑️ Curve deleted", 2000)
                
        except Exception as e:
            print(f"❌ Failed to delete curve: {e}")
            import traceback
            traceback.print_exc()
        
    def _edit_selected_curve_color(self):
        """Open color picker to change selected curve color"""
        if not self.selected_curve or not self.selected_curve_data:
            print("⚠️ No curve selected - Right-click on a curve first")
            return
        
        # Get current color
        current_color = self.selected_curve_data.get('color', (0, 1, 0))
        qcolor = QColor.fromRgbF(current_color[0], current_color[1], current_color[2])
        
        # Show color picker
        new_color = QColorDialog.getColor(qcolor, self.app, "Choose Curve Color")
        
        if new_color.isValid():
            # Convert to RGB tuple (0-1 range)
            rgb = (new_color.redF(), new_color.greenF(), new_color.blueF())
            
            # Update curve color
            self.selected_curve.GetProperty().SetColor(*rgb)
            self.selected_curve_data['color'] = rgb
            
            self.app.vtk_widget.render()
            
            print(f"🎨 Curve color changed to RGB({rgb[0]:.2f}, {rgb[1]:.2f}, {rgb[2]:.2f})")
            
            if hasattr(self.app, 'statusBar'):
                self.app.statusBar().showMessage("🎨 Curve color updated", 2000)

    def _has_curve_scene_reference(self):
        """
        True when there is a real scene/grid/point cloud behind the drawing.

        If no LAZ/SNT/DXF/DWG is loaded, final Curve should stay as Actor2D,
        because 3D overlay actors can disappear when the scene has no active
        reference geometry/camera bounds.
        """
        try:
            data = getattr(self.app, "data", None)
            if isinstance(data, dict) and data.get("xyz") is not None:
                return True
        except Exception:
            pass

        for attr_name in (
            "snt_actors",
            "dxf_actors",
            "dwg_actors",
            "snt_block_polygons",
        ):
            try:
                value = getattr(self.app, attr_name, None)
                if value:
                    return True
            except Exception:
                pass

        return False
        
    def _create_curve_actor(self, points, color=(0, 1, 0), width=2):
        """
        Create final curve actor.

        With LAZ/SNT/DXF/DWG loaded:
            use Digitizer's normal 3D overlay line system.

        With nothing loaded:
            use Actor2D foreground, same as preview, so the curve does not
            disappear after finalizing on an empty canvas.
        """
        if points is None or len(points) < 2:
            return None

        clean_points = [tuple(float(v) for v in pt[:3]) for pt in points]

        digitizer = getattr(self.app, 'digitizer', None)

        # Use digitizer 3D overlay only when a real scene/grid/point-cloud exists.
        if self._has_curve_scene_reference():
            if digitizer is not None and hasattr(digitizer, '_make_polyline_actor'):
                actor = digitizer._make_polyline_actor(
                    clean_points,
                    color=color,
                    width=width,
                    line_style='solid'
                )
                if actor is not None:
                    try:
                        actor.PickableOff()
                        actor.SetPickable(0)
                    except Exception:
                        pass
                    return actor

        # Empty canvas fallback:
        # Keep the final curve as Actor2D foreground, same as preview.
        vtk_points = vtk.vtkPoints()
        vtk_points.SetDataTypeToDouble()
        for pt in clean_points:
            vtk_points.InsertNextPoint(pt)

        polyline = vtk.vtkPolyLine()
        polyline.GetPointIds().SetNumberOfIds(len(clean_points))
        for i in range(len(clean_points)):
            polyline.GetPointIds().SetId(i, i)

        cells = vtk.vtkCellArray()
        cells.InsertNextCell(polyline)

        polydata = vtk.vtkPolyData()
        polydata.SetPoints(vtk_points)
        polydata.SetLines(cells)

        mapper = vtk.vtkPolyDataMapper2D()
        mapper.SetInputData(polydata)

        coord = vtk.vtkCoordinate()
        coord.SetCoordinateSystemToWorld()
        mapper.SetTransformCoordinate(coord)

        actor = vtk.vtkActor2D()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(*color)
        actor.GetProperty().SetLineWidth(width)
        actor.GetProperty().SetDisplayLocationToForeground()

        return actor
    
    def get_curves_as_fences(self):
        """
        Return finalized curves in a format compatible with FenceSelectorWidget.
        Each curve is returned as a dict similar to digitizer drawings:
        {
            'type': 'curve',
            'coords': [[x,y,z], ...],  # interpolated points
            'source': 'curve_tool',
            'curve_data': <original curve_data dict>,
            'curve_index': <int>  # index in finalized_actors
        }
        """
        fences = []
        for idx, curve_data in enumerate(self.finalized_actors):
            if isinstance(curve_data, dict) and 'interpolated' in curve_data:
                interpolated = curve_data['interpolated']
                # Convert numpy array to list of [x, y, z]
                if hasattr(interpolated, 'tolist'):
                    coords = interpolated.tolist()
                else:
                    coords = [list(pt) for pt in interpolated]

                curve_data['type'] = 'curve'
                curve_data['coords'] = [tuple(pt) for pt in coords]
                curve_data['source'] = 'curve_tool'
                curve_data['curve_index'] = idx
                
                fences.append(curve_data)
        return fences


    def is_curve_fence(self, fence_data):
        """Check if a fence dict came from the curve tool."""
        return isinstance(fence_data, dict) and fence_data.get('source') == 'curve_tool'


    def get_curve_actor_for_fence(self, fence_data):
        """Get the VTK actor for a curve fence (for highlighting)."""
        if not self.is_curve_fence(fence_data):
            return None
        curve_data = fence_data.get('curve_data')
        if curve_data and isinstance(curve_data, dict):
            return curve_data.get('actor')
        return None

    def _remove_curve_actor(self, actor):
        """Remove a curve actor from any renderer layer it may live in."""
        if actor is None:
            return

        digitizer = getattr(self.app, "digitizer", None)

        # Try digitizer overlay remover, but do NOT return immediately.
        # Empty-canvas curves may be vtkActor2D added through AddViewProp,
        # so we must also try RemoveViewProp / RemoveActor2D below.
        if digitizer is not None and hasattr(digitizer, "_remove_actor_from_overlay"):
            try:
                digitizer._remove_actor_from_overlay(actor)
            except Exception:
                pass

        renderers = []
        vtk_widget = getattr(self.app, "vtk_widget", None)

        if vtk_widget is not None:
            renderers.append(getattr(vtk_widget, "renderer", None))

        if digitizer is not None:
            renderers.append(getattr(digitizer, "overlay_renderer", None))
            renderers.append(getattr(digitizer, "renderer", None))
            renderers.append(getattr(digitizer, "text_overlay_renderer", None))

        try:
            renderers.append(self._get_renderer())
        except Exception:
            pass

        seen = set()
        for renderer in renderers:
            if renderer is None:
                continue

            rid = id(renderer)
            if rid in seen:
                continue
            seen.add(rid)

            for method_name in ("RemoveViewProp", "RemoveActor", "RemoveActor2D"):
                method = getattr(renderer, method_name, None)
                if callable(method):
                    try:
                        method(actor)
                    except Exception:
                        pass

    def clear_all_curves(self):
        """Clear EVERYTHING related to curves (final + preview + dynamic)"""

        print("🧹 Clearing all curve data...")

        # ✅ 1. Remove finalized curves (skip classified fences — dialog handles those)
        classified_to_keep = []
        for curve_data in self.finalized_actors:
            if isinstance(curve_data, dict) and curve_data.get('classified_fence', False):
                classified_to_keep.append(curve_data)
                continue
            actor = curve_data['actor'] if isinstance(curve_data, dict) else curve_data
            self._remove_curve_actor(actor)

        self.finalized_actors = classified_to_keep

        # ✅ 2. Clear preview (CRITICAL FIX)
        self._clear_preview()

        # ✅ 3. Reset state
        self.points = []
        self.undo_stack = []
        self.redo_stack = []
        self.history_stack = []
        self.history_redo_stack = []

        # ✅ 4. Clear curve entries from digitizer (if any left) to ensure NO LINK in undo
        if hasattr(self.app, 'digitizer'):
            # Modify in-place with slice assignment so the DrawingList instance
            # (and its automatic layer/snt_file tagging in .append) is preserved.
            # A full replacement (drawings = [...]) would swap it for a plain list,
            # breaking layer tagging for every subsequent draw after a Clear.
            # Classified fence curves are preserved here — the Clear Classified
            # Fences dialog handles their deletion explicitly.
            self.app.digitizer.drawings[:] = [
                d for d in self.app.digitizer.drawings
                if d.get('type') != 'curve' or d.get('classified_fence', False)
            ]
            # Tell every registered finalize listener that the drawing list just changed
            for callback in list(getattr(self.app.digitizer, "_drawing_finalized_callbacks", []) or []):
                try:
                    callback(None)
                except Exception:
                    pass

        # ✅ 5. Clear selection
        self.selected_curve = None
        self.selected_curve_data = None

        # ✅ 6. Force render refresh
        self.app.vtk_widget.render()

        print("✅ All curves + previews cleared")


    def undo_curve(self):
        """Undo the last completed curve (whole-operation undo after right-click)."""
        digitizer = getattr(self.app, 'digitizer', None)
        if digitizer and hasattr(digitizer, 'undo'):
            print("🎨 Delegating curve undo to unified digitizer undo")
            digitizer.undo()
            return

        if not self.history_stack:
            print("⚠️ No completed curve to undo")
            return
 
        curve_data = self.history_stack.pop()
 
        # Save to redo stack
        self.history_redo_stack.append(curve_data)
        self._trim_curve_history()
 
        # Remove actor from renderer and finalized list
        try:
            self.finalized_actors.remove(curve_data)
        except ValueError:
            pass
        self._remove_curve_actor(curve_data['actor'])

        # Also remove from digitizer drawings
        if digitizer and curve_data in digitizer.drawings:
            digitizer.drawings.remove(curve_data)
            if hasattr(digitizer, '_emit_drawing_removed'):
                digitizer._emit_drawing_removed(curve_data)
 
        self.app.vtk_widget.render()
        print(f"↶ Undo completed curve (history: {len(self.history_stack)})")
 
        if hasattr(self.app, 'statusBar'):
            self.app.statusBar().showMessage("↶ Curve undone | Ctrl+Y to redo", 2000)

    def redo_curve(self):
        """Redo the last undone completed curve."""
        digitizer = getattr(self.app, 'digitizer', None)
        if digitizer and hasattr(digitizer, 'redo'):
            print("🎨 Delegating curve redo to unified digitizer redo")
            digitizer.redo()
            return

        if not self.history_redo_stack:
            print("⚠️ Nothing to redo")
            return
 
        curve_data = self.history_redo_stack.pop()
 
        # Push back to history
        self.history_stack.append(curve_data)
        self._trim_curve_history()
 
        # Re-add actor and finalized entry
        self.finalized_actors.append(curve_data)
        self._add_curve_actor_to_overlay(curve_data['actor'])

        # Re-add to digitizer drawings
        if digitizer:
            digitizer.drawings.append(curve_data)
            if hasattr(digitizer, '_emit_drawing_finalized'):
                digitizer._emit_drawing_finalized(curve_data)
 
        self.app.vtk_widget.render()
        print(f"↷ Redo completed curve (history: {len(self.history_stack)})")
 
        if hasattr(self.app, 'statusBar'):
            self.app.statusBar().showMessage("↷ Curve redone", 2000)
