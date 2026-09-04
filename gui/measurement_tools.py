
"""
Measurement Tools for NakshaAI
Vertex-to-vertex distance measurement with live display
"""

import time
from pathlib import Path

import vtk
import numpy as np


from gui.measure_settings_dialog import load_measure_settings


class MeasurementTool:
    """
    Live measurement tool for vertex-to-vertex distances.
    Shows distance labels as you draw measurement lines.
    """
    
    def __init__(self, digitizer):
        self.digitizer = digitizer
        self.app = digitizer.app
        self.renderer = digitizer.renderer
        self.interactor = digitizer.interactor

        self._measure_style = load_measure_settings()


        self.original_interactor_style = self.interactor.GetInteractorStyle()
        
        # Storage
        # Storage
        self.measurements = []
        self.active_measurement = None
        self.temp_line_actor = None
        self.distance_labels = []
        self.vertex_markers = []
        self.continuous_line_actor = None
        # Track every actor created by MeasurementTool.
        # Fixes orphan actors when user switches Line -> Path -> Line without finalizing.
        self._measurement_actors = []
        self._measurement_actor_ids = set()
        # Screen-space measurement line actor caches. 2D/class-filtered loads can
        # bury world-space line actors behind the point cloud; these caches let us
        # redraw line geometry in display coordinates on the overlay layer.
        self._line_actor_world_points = {}
        self._line_actor_display_points = {}
        self._line_actor_polydata = {}
        # ✅ Overlay renderer for always-on-top measurement lines
        self._overlay_renderer = getattr(digitizer, "overlay_renderer", None)
        # ⚡ Throttle + reuse state for fast mouse-move preview (Microstation-style)
        self._render_timer = None
        self._last_z = 0.0
        self._last_preview_time = 0.0          # epoch time of last preview render
        self._preview_interval = 0.0            # no manual cap — Qt update() coalesces renders
        self._preview_pts = None               # reusable vtkPoints (2-pt line)
        self._preview_polydata = None          # reusable vtkPolyData for preview
        self._preview_actor_in_scene = False   # track whether actor was added
        self._last_cursor_pos = None           # last known world cursor position
        self._label_offset_pixels = 12.0
        # Lightweight snap-to-vertex state. Keep this cheap: candidates are cached
        # at tool activation and no heavy DXF/SNT actor polydata is scanned on mouse move.
        self._snap_tolerance_pixels = 14.0
        self._snap_candidate_cache = []
        self._snap_cache_valid = False
        self._last_snap_check_time = 0.0
        self._last_snap_screen_pos = None
        self._last_snap_world_pos = None
        self._last_snap_result = None
        # Measurement drawing plane. In 3D, keep measurement geometry on the
        # same Z level as the grid labels instead of falling below the grid.
        self._measurement_level_z = None
        # Keep measurement vertex dots visible while drawing/finalized, like the
        # original Measurement Tool behavior. Lines remain stable draw-style.
        self._measurement_show_vertex_markers = True
        self._render_observer_tag = None
        self._refreshing_label_positions = False

        
        # Selection
        # Selection
        self.selected_measurement_index = None
        self.selected_segment_index = None  # Track individual segment
        self.original_colors = {}  # Store original colors for unhighlighting
        
        # Mouse state
        self.mode = "measure_line" # Default
        self.active = False
        self.is_measuring = False
        self.is_panning = False 
        self._suspended_pan_button = None
        self.measurement_points = []
        self._observer_tags = [] 
        # Undo/Redo state
        self.undo_stack = []
        self.redo_stack = []
        self.max_undo_levels = 50
        self._temp_vertex_stack = []
        self._block_boundary_cache = {}
        self._block_boundary_index = []

        self._ensure_render_observer()

    def _suspend_left_click_pan(self):
        """Give an active measurement session exclusive ownership of left click.

        The application has both Qt-level and VTK-level left-to-middle pan
        routing.  Merely aborting the measurement observer is too late for
        some of those paths, so temporarily disable the global mapping while
        measuring.  Physical middle-button panning remains available.
        """
        if self.app is None or self._suspended_pan_button is not None:
            return
        current = getattr(self.app, "panning_button", "scroll")
        if current == "left":
            self._suspended_pan_button = current
            self.app.panning_button = "scroll"
            # Also terminate a pan that may already have claimed the press.
            try:
                handler = getattr(self.app, "_handle_fast_main_pan_release", None)
                if callable(handler):
                    handler()
            except Exception:
                pass
            try:
                style = self.interactor.GetInteractorStyle()
                if style is not None:
                    style.OnMiddleButtonUp()
                    style.OnLeftButtonUp()
            except Exception:
                pass
            print("Measurement active: left-click pan suspended; middle-click pan remains available")

    def _restore_left_click_pan(self):
        """Restore the pan button temporarily suspended by measurement mode."""
        if self.app is None or self._suspended_pan_button is None:
            return
        self.app.panning_button = self._suspended_pan_button
        self._suspended_pan_button = None
        print("Measurement inactive: left-click pan restored")

    def _ensure_overlay_renderer(self):
        """Return the pipeline-owned vector overlay renderer."""
        from gui.scene_render_pipeline import ROLE_OVERLAY, ensure_scene_render_pipeline

        pipeline = ensure_scene_render_pipeline(
            self.app,
            overlay_renderer=getattr(self.digitizer, "overlay_renderer", None),
            text_renderer=getattr(self.digitizer, "text_overlay_renderer", None),
        )
        ren = pipeline.get(ROLE_OVERLAY)
        if ren is not None:
            self._overlay_renderer = ren
        return ren

    def _prepare_overlay_actor(self, actor):
        """
        Apply front-most overlay settings before adding a prop to vector layer 2.
        """
        if actor is None:
            return

        try:
            if actor.IsA("vtkActor2D"):
                return
        except Exception:
            pass

        try:
            prop = actor.GetProperty()
        except Exception:
            prop = None

        if prop is not None:
            try:
                prop.SetDepthTestingEnabled(False)
                return
            except Exception:
                pass

        try:
            mapper = actor.GetMapper()
        except Exception:
            mapper = None

        if mapper is None:
            return

        try:
            mapper.SetResolveCoincidentTopologyToPolygonOffset()
            mapper.SetResolveCoincidentTopologyPolygonOffsetParameters(-1e5, -1e5)
        except Exception:
            pass

        try:
            mapper.SetResolveCoincidentTopologyLineOffsetParameters(-1e5, -1e5)
        except Exception:
            pass

    def _track_measurement_actor(self, actor):
        """Track measurement actors so Clear can remove orphan active actors also."""
        if actor is None:
            return

        actor_id = id(actor)
        if actor_id in self._measurement_actor_ids:
            return

        self._measurement_actor_ids.add(actor_id)
        self._measurement_actors.append(actor)

    def _untrack_measurement_actor(self, actor):
        """Remove actor from measurement registry."""
        if actor is None:
            return

        actor_id = id(actor)
        if actor_id not in self._measurement_actor_ids:
            return

        self._measurement_actor_ids.discard(actor_id)
        self._measurement_actors = [
            a for a in self._measurement_actors
            if id(a) != actor_id
        ]

    def _remove_all_tracked_measurement_actors(self):
        """
        Hard cleanup for all MeasurementTool actors.
        Removes only actors created by MeasurementTool._scene_add().
        Does not remove SNT/DXF/draw-tool actors.
        """
        for actor in list(self._measurement_actors):
            try:
                self._scene_remove(actor)
            except Exception:
                pass

        self._measurement_actors = []
        self._measurement_actor_ids.clear()
        self._preview_actor_in_scene = False

    def _scene_add(self, actor):
        """Add a measurement actor to the overlay renderer (always on top) and track it."""
        if actor is None:
            return

        ren = self._ensure_overlay_renderer()
        self._prepare_overlay_actor(actor)

        # Never leave measurement props in layer-0 point-cloud renderer.
        try:
            if actor.IsA("vtkActor2D"):
                self.renderer.RemoveActor2D(actor)
            else:
                self.renderer.RemoveActor(actor)
        except Exception:
            pass
        try:
            self.renderer.RemoveViewProp(actor)
        except Exception:
            pass

        try:
            if actor.IsA("vtkActor2D"):
                if not ren.HasViewProp(actor):
                    ren.AddActor2D(actor)
                self._track_measurement_actor(actor)
                return
        except Exception:
            pass

        try:
            if not ren.HasViewProp(actor):
                ren.AddActor(actor)
        except Exception:
            ren.AddActor(actor)

        self._track_measurement_actor(actor)

    def _scene_remove(self, actor):
        """Remove a measurement actor from the overlay renderer and registry."""
        if actor is None:
            return

        actor_key = id(actor)
        for cache_name in (
            "_line_actor_world_points",
            "_line_actor_display_points",
            "_line_actor_polydata",
        ):
            try:
                getattr(self, cache_name, {}).pop(actor_key, None)
            except Exception:
                pass

        is_actor_2d = False
        try:
            is_actor_2d = actor.IsA("vtkActor2D")
        except Exception:
            pass

        if self._overlay_renderer is not None:
            try:
                if is_actor_2d:
                    self._overlay_renderer.RemoveActor2D(actor)
                else:
                    self._overlay_renderer.RemoveActor(actor)
            except Exception:
                pass
            try:
                self._overlay_renderer.RemoveViewProp(actor)
            except Exception:
                pass

        # Fallback: also try main renderer in case actor ended up there.
        try:
            if is_actor_2d:
                self.renderer.RemoveActor2D(actor)
            else:
                self.renderer.RemoveActor(actor)
        except Exception:
            pass
        try:
            self.renderer.RemoveViewProp(actor)
        except Exception:
            pass

        self._untrack_measurement_actor(actor)

        key = id(actor)
        self._line_actor_world_points.pop(key, None)
        self._line_actor_display_points.pop(key, None)
        self._line_actor_polydata.pop(key, None)

    def _render_overlay_only(self):
        """Render all layers so the layer-1 measurement overlay is repainted."""
        try:
            self._ensure_overlay_renderer()
            self._refresh_all_measurement_line_positions()
        except Exception:
            pass
        try:
            self.app.vtk_widget.GetRenderWindow().Render()
        except Exception:
            try:
                self.app.vtk_widget.render()
            except Exception:
                pass

    def _ensure_render_observer(self):
        """
        🚀 SENIOR REFACTOR: Use Camera ModifiedEvent instead of Window StartEvent.
        This ensures we only recompute label offsets when the camera actually moves,
        saving significant CPU cycles during idle or classification.
        """
        if self._render_observer_tag is not None:
            return
        try:
            cam = self.renderer.GetActiveCamera()
            if cam is not None:
                # ModifiedEvent fires when camera panned/zoomed/rotated
                self._render_observer_tag = cam.AddObserver("ModifiedEvent", self._on_render_update_labels)
        except Exception:
            self._render_observer_tag = None


    def _on_render_update_labels(self, obj, evt):
        """Refresh 2D measurement line/label positions when camera moves."""
        if self._refreshing_label_positions:
            return
        if (not self.measurements and not self.distance_labels
                and self.temp_line_actor is None and self.continuous_line_actor is None):
            return

        now = time.time()
        if (now - getattr(self, '_last_label_update', 0)) < 0.016:  # 60fps cap
            return
        self._last_label_update = now

        self._refreshing_label_positions = True
        try:
            self._refresh_all_measurement_line_positions()
            self._refresh_label_positions(self.distance_labels)
            for measurement in self.measurements:
                self._refresh_label_positions(measurement.get('labels', []))
        finally:
            self._refreshing_label_positions = False

    def _world_to_display_point(self, world_point):
        """Project a world point into display coordinates."""
        helper = getattr(self.app, "_world_to_display", None)
        if callable(helper):
            try:
                return helper(self.renderer, world_point)
            except Exception:
                pass

        try:
            self.renderer.SetWorldPoint(world_point[0], world_point[1], world_point[2], 1.0)
            self.renderer.WorldToDisplay()
            display_point = self.renderer.GetDisplayPoint()
            if display_point is None or len(display_point) < 3:
                return None
            return (
                float(display_point[0]),
                float(display_point[1]),
                float(display_point[2]),
            )
        except Exception:
            return None

    def _build_display_line_polydata(self, world_points):
        """Build screen-space polyline geometry from world-space measurement points."""
        if not world_points or len(world_points) < 2:
            return None, None

        pts = vtk.vtkPoints()
        pts.SetDataTypeToDouble()

        clean_world = []
        for wp in world_points:
            if not self._is_valid_world_point(wp):
                return None, None
            d = self._world_to_display_point(wp)
            if d is None:
                return None, None
            pts.InsertNextPoint(float(d[0]), float(d[1]), 0.0)
            clean_world.append((float(wp[0]), float(wp[1]), float(wp[2])))

        line = vtk.vtkPolyLine()
        line.GetPointIds().SetNumberOfIds(len(clean_world))
        for i in range(len(clean_world)):
            line.GetPointIds().SetId(i, i)

        cells = vtk.vtkCellArray()
        cells.InsertNextCell(line)

        polydata = vtk.vtkPolyData()
        polydata.SetPoints(pts)
        polydata.SetLines(cells)
        return polydata, pts

    def _refresh_line_actor_2d(self, actor):
        """Keep an Actor2D measurement line locked to its saved world points."""
        if actor is None:
            return
        key = id(actor)
        world_points = self._line_actor_world_points.get(key)
        display_pts = self._line_actor_display_points.get(key)
        polydata = self._line_actor_polydata.get(key)
        if not world_points or display_pts is None or polydata is None:
            return

        for i, wp in enumerate(world_points):
            d = self._world_to_display_point(wp)
            if d is None:
                continue
            display_pts.SetPoint(i, float(d[0]), float(d[1]), 0.0)
        display_pts.Modified()
        polydata.Modified()
        try:
            actor.Modified()
        except Exception:
            pass

    def _refresh_measurement_line_positions(self):
        """Compatibility wrapper: refresh screen-space measurement line actors."""
        self._refresh_all_measurement_line_positions()

    def _display_to_world_point(self, display_x, display_y, display_z):
        """Project display coordinates back into world coordinates."""
        helper = getattr(self.app, "_display_to_world", None)
        if callable(helper):
            try:
                return helper(self.renderer, display_x, display_y, display_z)
            except Exception:
                pass

        try:
            self.renderer.SetDisplayPoint(float(display_x), float(display_y), float(display_z))
            self.renderer.DisplayToWorld()
            world_point = self.renderer.GetWorldPoint()
            if world_point is None or len(world_point) < 4:
                return None
            w = world_point[3]
            if abs(w) < 1e-9:
                return None
            return (
                float(world_point[0] / w),
                float(world_point[1] / w),
                float(world_point[2] / w),
            )
        except Exception:
            return None

    def _get_label_position_above_segment(self, p1, p2):
        """Return a world-space anchor a few screen pixels above a segment."""
        if not self._is_valid_world_point(p1) or not self._is_valid_world_point(p2):
            return p2 if self._is_valid_world_point(p2) else p1

        p1_arr = np.asarray(p1, dtype=np.float64)[:3]
        p2_arr = np.asarray(p2, dtype=np.float64)[:3]
        midpoint = ((p1_arr + p2_arr) * 0.5).tolist()

        d1 = self._world_to_display_point(p1_arr)
        d2 = self._world_to_display_point(p2_arr)
        dmid = self._world_to_display_point(midpoint)
        if d1 is None or d2 is None or dmid is None:
            return (float(midpoint[0]), float(midpoint[1]), float(midpoint[2]))

        tangent = np.array([d2[0] - d1[0], d2[1] - d1[1]], dtype=np.float64)
        tangent_len = np.linalg.norm(tangent)
        if tangent_len < 1e-6:
            normal = np.array([0.0, 1.0], dtype=np.float64)
        else:
            normal = np.array([-tangent[1], tangent[0]], dtype=np.float64) / tangent_len
            if normal[1] < 0.0:
                normal *= -1.0

        world_anchor = self._display_to_world_point(
            dmid[0] + normal[0] * self._label_offset_pixels,
            dmid[1] + normal[1] * self._label_offset_pixels,
            dmid[2],
        )
        if self._is_valid_world_point(world_anchor):
            return (
                float(world_anchor[0]),
                float(world_anchor[1]),
                float(world_anchor[2]),
            )
        return (float(midpoint[0]), float(midpoint[1]), float(midpoint[2]))

    def _set_label_actor_position(self, actor, world_position):
        """Update a 2D text actor's world anchor."""
        if actor is None or not self._is_valid_world_point(world_position):
            return
        try:
            if not actor.IsA("vtkActor2D"):
                return
            coord = actor.GetActualPositionCoordinate()
            coord.SetCoordinateSystemToWorld()
            coord.SetValue(world_position[0], world_position[1], world_position[2])
            actor.Modified()
        except Exception:
            pass

    def _refresh_label_positions(self, label_entries):
        """Recompute segment label anchors for the current camera."""
        for label_data in label_entries or []:
            actor = label_data.get('label')
            if actor is None:
                continue

            p1 = label_data.get('p1')
            p2 = label_data.get('p2')
            if self._is_valid_world_point(p1) and self._is_valid_world_point(p2):
                p1_arr = np.asarray(p1, dtype=np.float64)[:3]
                p2_arr = np.asarray(p2, dtype=np.float64)[:3]
                if np.linalg.norm(p2_arr - p1_arr) > 1e-9:
                    self._set_label_actor_position(actor, self._get_label_position_above_segment(p1, p2))
                    continue

            if self._is_valid_world_point(p1):
                self._set_label_actor_position(actor, (float(p1[0]), float(p1[1]), float(p1[2])))

    def _project_world_to_measure_display(self, world_point):
        """Project a world point to display pixels for screen-space measurement lines."""
        if not self._is_valid_world_point(world_point):
            return None
        try:
            active_ren = getattr(getattr(self.app, "vtk_widget", None), "renderer", None) or self.renderer
            active_ren.SetWorldPoint(float(world_point[0]), float(world_point[1]), float(world_point[2]), 1.0)
            active_ren.WorldToDisplay()
            d = active_ren.GetDisplayPoint()
            if d is None or len(d) < 2:
                return None
            return (float(d[0]), float(d[1]), 0.0)
        except Exception:
            return None

    def _make_screen_line_polydata(self, world_points):
        """Build vtkPolyData in display coordinates, like draw-tool overlay geometry."""
        display_points = []
        for p in world_points or []:
            d = self._project_world_to_measure_display(p)
            if d is None:
                return None, None
            display_points.append(d)

        if len(display_points) < 2:
            return None, None

        pts = vtk.vtkPoints()
        pts.SetDataTypeToDouble()
        for x, y, z in display_points:
            pts.InsertNextPoint(float(x), float(y), float(z))

        polyline = vtk.vtkPolyLine()
        polyline.GetPointIds().SetNumberOfIds(len(display_points))
        for i in range(len(display_points)):
            polyline.GetPointIds().SetId(i, i)

        cells = vtk.vtkCellArray()
        cells.InsertNextCell(polyline)

        polydata = vtk.vtkPolyData()
        polydata.SetPoints(pts)
        polydata.SetLines(cells)
        return polydata, display_points

    def _create_screen_line_actor(self, world_points, color=(1, 1, 0), width=3, pickable=True):
        """
        Create a measurement line as vtkActor2D in display coordinates.
        This matches draw-tool overlay behaviour: always visible in 2D, not buried
        under filtered point-cloud/classification actors.
        """
        polydata, display_points = self._make_screen_line_polydata(world_points)
        if polydata is None:
            return None

        mapper = vtk.vtkPolyDataMapper2D()
        mapper.SetInputData(polydata)
        coord = vtk.vtkCoordinate()
        coord.SetCoordinateSystemToDisplay()
        mapper.SetTransformCoordinate(coord)

        actor = vtk.vtkActor2D()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(float(color[0]), float(color[1]), float(color[2]))
        prop.SetLineWidth(float(width))
        prop.SetOpacity(1.0)
        try:
            prop.SetDisplayLocationToForeground()
        except Exception:
            pass
        if pickable:
            actor.PickableOn()
        else:
            actor.PickableOff()

        key = id(actor)
        self._line_actor_world_points[key] = [tuple(p) for p in world_points]
        self._line_actor_display_points[key] = display_points
        self._line_actor_polydata[key] = polydata
        return actor

    def _refresh_screen_line_actor(self, actor):
        """Reproject one cached vtkActor2D measurement line after camera/view change."""
        if actor is None:
            return
        key = id(actor)
        world_points = self._line_actor_world_points.get(key)
        polydata = self._line_actor_polydata.get(key)
        if not world_points or polydata is None:
            return
        new_polydata, display_points = self._make_screen_line_polydata(world_points)
        if new_polydata is None:
            return
        try:
            polydata.DeepCopy(new_polydata)
            polydata.Modified()
            self._line_actor_display_points[key] = display_points
            actor.Modified()
        except Exception:
            pass

    def _refresh_all_measurement_line_positions(self):
        """Reproject all measurement line actors so 2D/3D switch keeps them visible."""
        try:
            if self.temp_line_actor is not None:
                self._refresh_screen_line_actor(self.temp_line_actor)
            if self.continuous_line_actor is not None:
                self._refresh_screen_line_actor(self.continuous_line_actor)
            for label_data in self.distance_labels:
                self._refresh_screen_line_actor(label_data.get('line'))
            for measurement in self.measurements:
                self._refresh_screen_line_actor(measurement.get('continuous_line'))
                for label_data in measurement.get('labels', []):
                    self._refresh_screen_line_actor(label_data.get('line'))
        except Exception:
            pass

    def _update_preview_line(self, p1, p2):
        """
        Update the rubber-band preview line as a stable world-space overlay.
        This matches draw-tool behaviour better in 3D: no screen-space
        reprojection jitter, no camera-move shake, and no heavy per-frame rebuild.
        """
        p1 = self._force_measurement_level(p1) if hasattr(self, "_force_measurement_level") else p1
        p2 = self._force_measurement_level(p2) if hasattr(self, "_force_measurement_level") else p2

        style = getattr(self, '_measure_style', None)
        color = (0, 1, 1)
        width = 2
        if style:
            mode_key = 'line' if getattr(self, 'mode', '') == 'measure_line' else 'path'
            sec = style.get(mode_key, {})
            color = sec.get('color', color)
            width = sec.get('width', width)

        if self._preview_polydata is None or self.temp_line_actor is None:
            pts = vtk.vtkPoints()
            pts.SetDataTypeToDouble()
            pts.SetNumberOfPoints(2)
            pts.SetPoint(0, 0.0, 0.0, 0.0)
            pts.SetPoint(
                1,
                float(p2[0]) - float(p1[0]),
                float(p2[1]) - float(p1[1]),
                float(p2[2]) - float(p1[2]),
            )

            line = vtk.vtkLine()
            line.GetPointIds().SetId(0, 0)
            line.GetPointIds().SetId(1, 1)

            cells = vtk.vtkCellArray()
            cells.InsertNextCell(line)

            polydata = vtk.vtkPolyData()
            polydata.SetPoints(pts)
            polydata.SetLines(cells)

            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputData(polydata)
            try:
                mapper.SetResolveCoincidentTopologyToPolygonOffset()
                mapper.SetResolveCoincidentTopologyPolygonOffsetParameters(-1e5, -1e5)
            except Exception:
                pass

            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            prop = actor.GetProperty()
            prop.SetColor(float(color[0]), float(color[1]), float(color[2]))
            prop.SetLineWidth(float(width))
            prop.SetOpacity(1.0)
            prop.LightingOff()
            try:
                prop.SetDepthTestingEnabled(False)
            except Exception:
                pass
            try:
                prop.RenderLinesAsTubesOn()
            except Exception:
                pass
            actor.PickableOff()
            actor.SetPosition(float(p1[0]), float(p1[1]), float(p1[2]))

            self._preview_pts = pts
            self._preview_polydata = polydata
            self.temp_line_actor = actor
            self._scene_add(actor)
            self._preview_actor_in_scene = True
        else:
            self._preview_pts.SetPoint(0, 0.0, 0.0, 0.0)
            self._preview_pts.SetPoint(
                1,
                float(p2[0]) - float(p1[0]),
                float(p2[1]) - float(p1[1]),
                float(p2[2]) - float(p1[2]),
            )
            self._preview_pts.Modified()
            self._preview_polydata.Modified()
            self.temp_line_actor.SetPosition(float(p1[0]), float(p1[1]), float(p1[2]))
            prop = self.temp_line_actor.GetProperty()
            prop.SetColor(float(color[0]), float(color[1]), float(color[2]))
            prop.SetLineWidth(float(width))

        try:
            self.temp_line_actor.VisibilityOn()
        except Exception:
            pass
        try:
            ren = self._ensure_overlay_renderer()
            if not ren.HasViewProp(self.temp_line_actor):
                self._scene_add(self.temp_line_actor)
        except Exception:
            pass
        self._preview_actor_in_scene = True

    def _screen_to_world_fast(self):
        """
        Project screen cursor to world XY at the measurement drawing level.
        The drawing level follows grid label Z in 3D, so measurement lines do
        not fall below the grid-label plane.
        """
        x, y = self.interactor.GetEventPosition()
        ren = self.renderer
        target_z = self._get_measurement_level_z()

        ren.SetDisplayPoint(x, y, 0.0)
        ren.DisplayToWorld()
        nh = ren.GetWorldPoint()
        w = nh[3] if nh[3] != 0.0 else 1.0
        near = np.array([nh[0]/w, nh[1]/w, nh[2]/w])

        ren.SetDisplayPoint(x, y, 1.0)
        ren.DisplayToWorld()
        fh = ren.GetWorldPoint()
        w = fh[3] if fh[3] != 0.0 else 1.0
        far = np.array([fh[0]/w, fh[1]/w, fh[2]/w])

        direction = far - near
        dz = direction[2]
        if abs(dz) > 1e-10:
            t = (target_z - near[2]) / dz
            return (float(near[0] + t * direction[0]),
                    float(near[1] + t * direction[1]),
                    float(target_z))
        # Fallback: ray parallel to target plane — return near point at target Z
        return (float(near[0]), float(near[1]), float(target_z))

    def _is_valid_world_point(self, pos):
        """Return True when a picked world position is usable for measurement."""
        if pos is None:
            return False
        try:
            arr = np.asarray(pos, dtype=np.float64).reshape(-1)
        except Exception:
            return False
        return arr.size >= 3 and np.all(np.isfinite(arr[:3]))

    def _compute_grid_label_level_z(self):
        """Return the Z level used by visible grid labels / attached grid overlays."""
        z_values = []

        def _add_actor_z(actor):
            if actor is None:
                return
            try:
                if not (getattr(actor, "is_grid_label", False) or getattr(actor, "grid_name", None)):
                    return
            except Exception:
                return
            try:
                pos = actor.GetPosition()
                if pos is not None and len(pos) >= 3 and np.isfinite(float(pos[2])):
                    z_values.append(float(pos[2]))
                    return
            except Exception:
                pass
            try:
                bounds = actor.GetBounds()
                if bounds is not None and len(bounds) >= 6:
                    z_mid = (float(bounds[4]) + float(bounds[5])) * 0.5
                    if np.isfinite(z_mid):
                        z_values.append(z_mid)
            except Exception:
                pass

        for store_name in ("snt_actors", "dxf_actors", "dwg_actors"):
            for entry in getattr(self.app, store_name, []) or []:
                actors = []
                if isinstance(entry, dict):
                    actors = entry.get("actors", []) or []
                elif isinstance(entry, (list, tuple)):
                    actors = entry
                else:
                    actors = [entry]
                for actor in actors:
                    _add_actor_z(actor)

        for attr_name in ("grid_label_actors", "grid_labels", "label_actors"):
            for actor in getattr(self.app, attr_name, []) or []:
                _add_actor_z(actor)

        if z_values:
            # Use the top-most label plane so the measurement never appears below it in 3D.
            return float(max(z_values))
        return None

    def _get_measurement_level_z(self):
        """Use grid-label Z when available, otherwise keep the current cached Z."""
        z_level = getattr(self, "_measurement_level_z", None)
        if z_level is None or not np.isfinite(float(z_level)):
            z_level = self._compute_grid_label_level_z()
            self._measurement_level_z = z_level
        if z_level is not None and np.isfinite(float(z_level)):
            return float(z_level)
        return float(getattr(self, "_last_z", 0.0))

    def _force_measurement_level(self, pos):
        """Keep measurement points on the grid-label plane while preserving XY."""
        if not self._is_valid_world_point(pos):
            return pos
        try:
            return (float(pos[0]), float(pos[1]), self._get_measurement_level_z())
        except Exception:
            return pos

    def _invalidate_measure_snap_cache(self):
        """Mark the lightweight vertex-snap cache dirty."""
        self._snap_cache_valid = False
        self._snap_candidate_cache = []
        self._last_snap_screen_pos = None
        self._last_snap_world_pos = None
        self._last_snap_result = None

    def _collect_static_measure_snap_candidates(self):
        """
        Collect stable snap vertices once, not on every mouse move.
        This keeps measurement as smooth as draw tools: no heavy actor-polydata
        scan during mouse movement.
        """
        candidates = []
        seen = set()

        def add_point(point):
            if not self._is_valid_world_point(point):
                return
            try:
                p = tuple(float(v) for v in point[:3])
            except Exception:
                return
            try:
                p = self._force_measurement_level(p)
            except Exception:
                pass
            key = (round(p[0], 4), round(p[1], 4), round(p[2], 4))
            if key in seen:
                return
            seen.add(key)
            candidates.append(p)

        # NOTE: Measurement-tool snapping must stay scoped to the measurement
        # tools' own geometry only. Draw-tool figures (digitizer.drawings) are
        # intentionally excluded here so Line/Path measurement snapping never
        # locks onto vertices from unrelated draw-tool shapes.
        for measurement in getattr(self, 'measurements', []) or []:
            for p in measurement.get('points', []) or []:
                add_point(p)
            for label_data in measurement.get('labels', []) or []:
                add_point(label_data.get('p1'))
                add_point(label_data.get('p2'))

        return candidates

    def _get_static_measure_snap_candidates(self):
        """Return cached snap candidates for the active measurement session."""
        if not getattr(self, '_snap_cache_valid', False):
            self._snap_candidate_cache = self._collect_static_measure_snap_candidates()
            self._snap_cache_valid = True
        return self._snap_candidate_cache

    def _find_nearest_measure_snap_point(self, display_pos=None, fallback_world=None, force=False):
        """Find nearest snap vertex within pixel tolerance using cached candidates."""
        try:
            if display_pos is None:
                display_pos = self.interactor.GetEventPosition()
            sx, sy = int(display_pos[0]), int(display_pos[1])
        except Exception:
            return fallback_world

        now = time.monotonic()
        if not force:
            if self._last_snap_screen_pos == (sx, sy):
                return self._last_snap_result or fallback_world
            if (now - getattr(self, '_last_snap_check_time', 0.0)) < 0.025:
                return self._last_snap_result or fallback_world

        self._last_snap_check_time = now
        self._last_snap_screen_pos = (sx, sy)
        self._last_snap_world_pos = fallback_world

        tolerance = float(getattr(self, '_snap_tolerance_pixels', 14.0))
        tolerance_sq = tolerance * tolerance
        best_point = None
        best_dist_sq = tolerance_sq

        candidates = []
        for p in getattr(self, 'measurement_points', []) or []:
            if self._is_valid_world_point(p):
                try:
                    candidates.append(self._force_measurement_level(tuple(float(v) for v in p[:3])))
                except Exception:
                    candidates.append(tuple(float(v) for v in p[:3]))
        candidates.extend(self._get_static_measure_snap_candidates())

        for point in candidates:
            d = self._project_world_to_measure_display(point)
            if d is None:
                continue
            dx = float(d[0]) - sx
            dy = float(d[1]) - sy
            dist_sq = dx * dx + dy * dy
            if dist_sq <= best_dist_sq:
                best_dist_sq = dist_sq
                best_point = point
                if dist_sq <= 4.0:
                    break

        self._last_snap_result = best_point
        return best_point or fallback_world

    def _get_snapped_measurement_point(self, display_pos=None, fallback_world=None, force=False):
        """Return snapped point when near a known vertex, otherwise fallback world point."""
        try:
            return self._find_nearest_measure_snap_point(
                display_pos=display_pos,
                fallback_world=fallback_world,
                force=force,
            )
        except Exception:
            return fallback_world

    def _normalize_block_label(self, label):
        """Normalize labels so PRJ block names and DXF text labels can be matched."""
        return "".join(ch.lower() for ch in str(label or "") if ch.isalnum())

    def _normalize_block_polygon(self, coords):
        """Drop duplicate closing vertices and malformed points from a block polygon."""
        normalized = []
        for coord in coords or []:
            try:
                x = float(coord[0])
                y = float(coord[1])
            except Exception:
                continue
            if normalized and abs(normalized[-1][0] - x) < 1e-9 and abs(normalized[-1][1] - y) < 1e-9:
                continue
            normalized.append((x, y))

        if len(normalized) >= 2:
            first_x, first_y = normalized[0]
            last_x, last_y = normalized[-1]
            if abs(first_x - last_x) < 1e-9 and abs(first_y - last_y) < 1e-9:
                normalized.pop()
        return normalized



    def _calculate_polygon_centroid_2d(self, points):
        """Return a stable centroid for a 2D polygon."""
        if not points:
            return (0.0, 0.0)
        if len(points) < 3:
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            return (float(sum(xs) / len(xs)), float(sum(ys) / len(ys)))

        signed_area = 0.0
        cx = 0.0
        cy = 0.0
        for i in range(len(points)):
            j = (i + 1) % len(points)
            cross = points[i][0] * points[j][1] - points[j][0] * points[i][1]
            signed_area += cross
            cx += (points[i][0] + points[j][0]) * cross
            cy += (points[i][1] + points[j][1]) * cross

        if abs(signed_area) < 1e-9:
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            return (float(sum(xs) / len(xs)), float(sum(ys) / len(ys)))

        signed_area *= 0.5
        scale = 1.0 / (6.0 * signed_area)
        return (float(cx * scale), float(cy * scale))

    def _calculate_polygon_area_2d(self, points):
        """
        Backward-compatible 2D area helper.
        Keeps legacy block-area call sites working while delegating to the
        shared Shoelace implementation.
        """
        return float(self._calculate_polygon_area(points))

    def _point_in_polygon_2d(self, point_xy, polygon):
        """Return True when the XY point lies inside or very near the polygon."""
        if len(polygon) < 3:
            return False

        x = float(point_xy[0])
        y = float(point_xy[1])
        inside = False
        n = len(polygon)
        for i in range(n):
            x1, y1 = polygon[i]
            x2, y2 = polygon[(i + 1) % n]

            edge_dx = x2 - x1
            edge_dy = y2 - y1
            edge_len_sq = edge_dx * edge_dx + edge_dy * edge_dy
            if edge_len_sq > 1e-12:
                t = ((x - x1) * edge_dx + (y - y1) * edge_dy) / edge_len_sq
                t = max(0.0, min(1.0, t))
                nearest_x = x1 + t * edge_dx
                nearest_y = y1 + t * edge_dy
                if (nearest_x - x) ** 2 + (nearest_y - y) ** 2 < 1e-6:
                    return True

            intersects = ((y1 > y) != (y2 > y))
            if intersects:
                cross_x = (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-12) + x1
                if x <= cross_x:
                    inside = not inside
        return inside

    def _iter_block_project_files(self):
        """Yield PRJ files that can describe block boundaries for attached overlays."""
        candidates = []
        seen = set()

        loaded_file = getattr(self.app, "loaded_file", None)
        if loaded_file:
            loaded_path = Path(str(loaded_file))
            if loaded_path.suffix.lower() == ".prj" and loaded_path.exists():
                key = str(loaded_path.resolve()).lower()
                if key not in seen:
                    seen.add(key)
                    candidates.append(loaded_path)

        for store_name in ("dxf_actors", "dwg_actors"):
            for overlay in getattr(self.app, store_name, []) or []:
                full_path = overlay.get("full_path") or overlay.get("filename")
                if not full_path:
                    continue
                base_path = Path(str(full_path))
                for suffix in (".prj", ".PRJ"):
                    prj_path = base_path.with_suffix(suffix)
                    if not prj_path.exists():
                        continue
                    try:
                        key = str(prj_path.resolve()).lower()
                    except Exception:
                        key = str(prj_path).lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append(prj_path)

        return candidates

    def _parse_terrascan_prj_blocks(self, prj_path):
        """Parse TerraScan PRJ block boundaries into searchable polygons."""
        blocks = []
        try:
            lines = Path(prj_path).read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception as exc:
            print(f"⚠️ Failed to read PRJ block file {prj_path}: {exc}")
            return blocks

        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line.startswith("Block "):
                i += 1
                continue

            block_file = line.replace("Block ", "", 1).strip()
            block_label = Path(block_file).stem or block_file
            coords = []
            j = i + 1
            while j < len(lines):
                coord_line = lines[j].strip()
                if not coord_line:
                    j += 1
                    continue
                if coord_line.startswith("Block "):
                    break

                parts = coord_line.split()
                if len(parts) >= 2:
                    try:
                        coords.append((float(parts[0]), float(parts[1])))
                    except ValueError:
                        pass
                j += 1

            polygon = self._normalize_block_polygon(coords)
            if len(polygon) >= 3:
                area = self._calculate_polygon_area_2d(polygon)
                centroid = self._calculate_polygon_centroid_2d(polygon)
                xs = [pt[0] for pt in polygon]
                ys = [pt[1] for pt in polygon]
                blocks.append({
                    "label": block_label,
                    "label_key": self._normalize_block_label(block_label),
                    "points": polygon,
                    "area": area,
                    "centroid": centroid,
                    "bbox": (min(xs), max(xs), min(ys), max(ys)),
                    "source_type": "prj",
                    "source_prj": str(prj_path),
                })

            i = j

        return blocks

    def _parse_attached_snt_blocks(self):
        """Convert attached SNT block polygons into the measurement block index.

        Includes legacy PRJ matches and current PRJ-authority records.
        Raw grid outlines (snt_grid) and unnamed BL-layer polygons (snt_text) are
        excluded — they are reference geometry, not actual file blocks.
        """
        blocks = []
        for idx, entry in enumerate(getattr(self.app, "snt_block_polygons", []) or []):
            # Only PRJ-matched blocks have file definitions — skip all others.
            if entry.get("source") not in {"prj", "prj_authority"}:
                continue

            coords = (
                entry.get("points_2d")
                or entry.get("points")
                or entry.get("vertices")
                or entry.get("polygon")
                or []
            )
            polygon = self._normalize_block_polygon(coords)
            if len(polygon) < 3:
                continue

            source_snt = (
                entry.get("snt_filename")
                or entry.get("source_snt")
                or entry.get("filename")
                or ""
            )
            label = (
                entry.get("grid_name")
                or entry.get("block_name")
                or entry.get("label")
                or ""
            )
            if not label:
                source_name = Path(str(source_snt)).stem if source_snt else "SNT block"
                label = f"{source_name} #{idx + 1}"

            area = self._calculate_polygon_area_2d(polygon)
            centroid = self._calculate_polygon_centroid_2d(polygon)
            xs = [pt[0] for pt in polygon]
            ys = [pt[1] for pt in polygon]
            blocks.append({
                "label": str(label),
                "label_key": self._normalize_block_label(label),
                "points": polygon,
                "area": area,
                "centroid": centroid,
                "bbox": (min(xs), max(xs), min(ys), max(ys)),
                "source_type": "snt",
                "source_snt": str(source_snt) if source_snt else None,
                "source_prj": entry.get("prj_path"),
            })

        return blocks

    def _parse_attached_snt_grids(self):
        """Adapt the live SNT grid index into measurement entries.

        Current SNT attachments replace raw ``snt_grid`` cells with
        ``prj_authority`` records whenever an adjacent PRJ supplies the exact,
        authoritative boundary.  Files without PRJ data continue to expose
        ``snt_grid`` records.  Both are grid cells for measurement purposes.
        """
        grids = []
        for idx, entry in enumerate(getattr(self.app, "snt_block_polygons", []) or []):
            index_source = entry.get("source")
            if index_source not in {"snt_grid", "prj_authority"}:
                continue

            coords = (
                entry.get("points_2d")
                or entry.get("points")
                or entry.get("vertices")
                or entry.get("polygon")
                or []
            )
            polygon = self._normalize_block_polygon(coords)
            if len(polygon) < 3:
                continue

            source_snt = entry.get("snt_filename") or entry.get("source_snt") or ""
            label = entry.get("grid_name") or entry.get("label") or f"Grid #{idx + 1}"
            xs = [point[0] for point in polygon]
            ys = [point[1] for point in polygon]
            grids.append({
                "label": str(label),
                "label_key": self._normalize_block_label(label),
                "points": polygon,
                "area": self._calculate_polygon_area_2d(polygon),
                "centroid": self._calculate_polygon_centroid_2d(polygon),
                "bbox": (min(xs), max(xs), min(ys), max(ys)),
                "source_type": "snt_grid",
                "source_snt": str(source_snt) if source_snt else None,
                "source_layer": entry.get("poly_layer") or entry.get("layer"),
                "index_source": index_source,
                "source_prj": entry.get("prj_path"),
            })
        return grids

    def _load_grid_boundaries(self):
        """Return live grid geometry without changing the Block boundary cache."""
        return self._parse_attached_snt_grids()

    def _parse_digitizer_drawings(self):
        """Convert digitizer drawn closed shapes into block-like polygon entries."""
        blocks = []
        digitizer = getattr(self.app, 'digitizer', None)
        if digitizer is None:
            return blocks

        CLOSED_TYPES = {'polygon', 'polyline', 'rectangle', 'freehand', 'orthopolygon'}

        for idx, drawing in enumerate(getattr(digitizer, 'drawings', []) or []):
            draw_type = drawing.get('type', '')
            if draw_type not in CLOSED_TYPES:
                continue

            coords = drawing.get('coords', [])
            polygon_2d = []
            for c in coords:
                try:
                    polygon_2d.append((float(c[0]), float(c[1])))
                except (IndexError, TypeError, ValueError):
                    continue

            polygon_2d = self._normalize_block_polygon(polygon_2d)
            if len(polygon_2d) < 3:
                continue

            area = self._calculate_polygon_area_2d(polygon_2d)
            centroid = self._calculate_polygon_centroid_2d(polygon_2d)
            xs = [pt[0] for pt in polygon_2d]
            ys = [pt[1] for pt in polygon_2d]

            layer = drawing.get('layer') or ''
            label = f"{draw_type.capitalize()} #{idx + 1}" + (f" [{layer}]" if layer else "")

            blocks.append({
                'label': label,
                'label_key': self._normalize_block_label(label),
                'points': polygon_2d,
                'area': area,
                'centroid': centroid,
                'bbox': (min(xs), max(xs), min(ys), max(ys)),
                'source_type': 'digitizer',
                'drawing_type': draw_type,
            })

        return blocks

    def _load_block_boundaries(self, force=False):
        """Load block polygons from PRJ files, SNT overlays, and digitizer drawings."""
        if force:
            self._block_boundary_cache = {}
            self._block_boundary_index = []

        blocks = []
        for prj_path in self._iter_block_project_files():
            cache_key = str(prj_path)
            if cache_key not in self._block_boundary_cache:
                self._block_boundary_cache[cache_key] = self._parse_terrascan_prj_blocks(prj_path)
            blocks.extend(self._block_boundary_cache.get(cache_key, []))

        blocks.extend(self._parse_attached_snt_blocks())
        blocks.extend(self._parse_digitizer_drawings())

        self._block_boundary_index = blocks
        return self._block_boundary_index

    def _find_block_by_label(self, label):
        """Find a parsed block whose label matches the clicked grid label."""
        label_key = self._normalize_block_label(label)
        if not label_key:
            return None

        for block in self._load_block_boundaries():
            block_key = block.get("label_key", "")
            if not block_key:
                continue
            if block_key == label_key or block_key in label_key or label_key in block_key:
                return block
        return None

    def _find_block_at_world_point(self, world_point):
        """Find the block polygon containing the clicked world XY location."""
        if not self._is_valid_world_point(world_point):
            return None

        x = float(world_point[0])
        y = float(world_point[1])

        for block in self._load_block_boundaries():
            xmin, xmax, ymin, ymax = block["bbox"]
            if x < xmin or x > xmax or y < ymin or y > ymax:
                continue
            if self._point_in_polygon_2d((x, y), block["points"]):
                return block
        return None

    def _find_grid_by_label(self, label):
        """Resolve a grid label without mixing in PRJ/block definitions."""
        label_key = self._normalize_block_label(label)
        if not label_key:
            return None
        for grid in self._load_grid_boundaries():
            grid_key = grid.get("label_key", "")
            if grid_key and (
                grid_key == label_key
                or grid_key in label_key
                or label_key in grid_key
            ):
                return grid
        return None

    def _find_grid_at_world_point(self, world_point):
        """Return the smallest grid cell containing the clicked world XY."""
        if not self._is_valid_world_point(world_point):
            return None
        x, y = float(world_point[0]), float(world_point[1])
        candidates = []
        for grid in self._load_grid_boundaries():
            xmin, xmax, ymin, ymax = grid["bbox"]
            if xmin <= x <= xmax and ymin <= y <= ymax:
                if self._point_in_polygon_2d((x, y), grid["points"]):
                    candidates.append(grid)
        if not candidates:
            return None
        return min(candidates, key=lambda item: float(item.get("area", float("inf"))))

    def _pick_grid_label_name(self, display_pos=None):
        """Return the clicked DXF/SNT grid label when the cursor is over one."""
        if display_pos is None:
            display_pos = self.interactor.GetEventPosition()
        x, y = display_pos

        try:
            render_window = self.app.vtk_widget.GetRenderWindow()
            window_size = render_window.GetSize()
            vtk_y = window_size[1] - y
        except Exception:
            return None

        try:
            area_picker = vtk.vtkAreaPicker()
            area_picker.AreaPick(x - 12, vtk_y - 12, x + 12, vtk_y + 12, self.renderer)
            for prop in area_picker.GetProp3Ds():
                if getattr(prop, "is_grid_label", False):
                    grid_name = getattr(prop, "grid_name", "")
                    if grid_name:
                        return grid_name
        except Exception:
            pass

        try:
            prop_picker = vtk.vtkPropPicker()
            prop_picker.Pick(x, vtk_y, 0, self.renderer)
            picked = prop_picker.GetActor()
        except Exception:
            picked = None

        if picked is None:
            return None

        if getattr(picked, "is_grid_label", False):
            grid_name = getattr(picked, "grid_name", "")
            if grid_name:
                return grid_name

        try:
            picked_pos = picked.GetPosition()
            picked_bounds = picked.GetBounds()
        except Exception:
            return None

        for store_name in ("snt_actors", "dxf_actors"):
            for data in getattr(self.app, store_name, []) or []:
                for actor in data.get("actors", []):
                    if not getattr(actor, "is_grid_label", False):
                        continue
                    try:
                        if actor.GetPosition() == picked_pos and actor.GetBounds() == picked_bounds:
                            grid_name = getattr(actor, "grid_name", "")
                            if grid_name:
                                return grid_name
                    except Exception:
                        pass
        return None

    def _create_block_outline_actor(self, polygon_points, z_value):
        """Create a highlighted outline actor for the selected block polygon."""
        if len(polygon_points) < 3:
            return None

        points = vtk.vtkPoints()
        for x, y in polygon_points:
            points.InsertNextPoint(float(x), float(y), float(z_value))
        points.InsertNextPoint(float(polygon_points[0][0]), float(polygon_points[0][1]), float(z_value))

        polyline = vtk.vtkPolyLine()
        polyline.GetPointIds().SetNumberOfIds(points.GetNumberOfPoints())
        for idx in range(points.GetNumberOfPoints()):
            polyline.GetPointIds().SetId(idx, idx)

        cells = vtk.vtkCellArray()
        cells.InsertNextCell(polyline)

        polydata = vtk.vtkPolyData()
        polydata.SetPoints(points)
        polydata.SetLines(cells)

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(polydata)

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(1.0, 0.55, 0.0)
        actor.GetProperty().SetLineWidth(4.0)
        actor.GetProperty().SetOpacity(1.0)
        actor.GetProperty().SetLighting(False)
        return actor

    def _measure_block_area_at_click(self, display_pos=None):
        """Resolve the clicked block and add an area label measurement (toggle on/off)."""
        blocks = self._load_block_boundaries()
        if not blocks:
            self.app.statusBar().showMessage("⚠️ No PRJ/SNT/Draw block boundaries found for the attached overlays", 4000)
            return

        grid_name = self._pick_grid_label_name(display_pos)
        block = self._find_block_by_label(grid_name) if grid_name else None

        world_point = self._get_measurement_world_point(display_pos)
        if world_point is None:
            try:
                world_point = self._screen_to_world_fast()
            except Exception:
                world_point = None

        if block is None and world_point is not None:
            block = self._find_block_at_world_point(world_point)

        if block is None:
            self.app.statusBar().showMessage("⚠️ No block found at this location", 2500)
            return

        # Toggle off: if this block already has a measurement, remove it
        block_key = self._normalize_block_label(block['label'])
        for idx, m in enumerate(self.measurements):
            if m.get('type') != 'measure_block_area':
                continue
            if self._normalize_block_label(m.get('block_name', '')) == block_key:
                self._save_state()
                self._remove_entire_measurement(idx)
                self.app.statusBar().showMessage(f"▦ {block['label']} deselected", 2000)
                try:
                    self.app.vtk_widget.GetRenderWindow().Render()
                except Exception:
                    self.app.vtk_widget.render()
                return

        z_value = float(world_point[2]) if self._is_valid_world_point(world_point) else float(self._last_z)
        centroid_x, centroid_y = block["centroid"]
        label_position = (float(centroid_x), float(centroid_y), z_value)

        outline_actor = self._create_block_outline_actor(block["points"], z_value)
        label_actor = self._create_area_label(label_position, block["area"])

        self._scene_add(outline_actor)
        self._scene_add(label_actor)

        self.measurements.append({
            "type": "measure_block_area",
            "block_name": block["label"],
            "points": [(float(x), float(y), z_value) for x, y in block["points"]],
            "labels": [{
                "line": outline_actor,
                "label": label_actor,
                "p1": label_position,
                "p2": label_position,
                "distance": 0.0,
            }],
            "vertices": [],
            "continuous_line": None,
            "total_distance": 0.0,
            "area": float(block["area"]),
            "source_type": block.get("source_type", "prj"),
            "source_prj": block.get("source_prj"),
            "source_snt": block.get("source_snt"),
        })

        self.app.statusBar().showMessage(
            f"▦ {block['label']} area: {block['area']:.2f} m²",
            5000,
        )
        try:
            self.app.vtk_widget.GetRenderWindow().Render()
        except Exception:
            self.app.vtk_widget.render()

    def _measure_grid_area_at_click(self, display_pos=None):
        """Resolve one clicked SNT grid cell and display its exact polygon area."""
        grids = self._load_grid_boundaries()
        if not grids:
            self.app.statusBar().showMessage(
                "No measurable SNT grid boundaries are attached",
                4000,
            )
            return

        grid_name = self._pick_grid_label_name(display_pos)
        grid = self._find_grid_by_label(grid_name) if grid_name else None

        world_point = self._get_measurement_world_point(display_pos)
        if world_point is None:
            try:
                world_point = self._screen_to_world_fast()
            except Exception:
                world_point = None
        if grid is None and world_point is not None:
            grid = self._find_grid_at_world_point(world_point)
        if grid is None:
            self.app.statusBar().showMessage("No grid found at this location", 2500)
            return

        grid_key = self._normalize_block_label(grid["label"])
        for idx, measurement in enumerate(self.measurements):
            if measurement.get("type") != "measure_grid_area":
                continue
            if self._normalize_block_label(measurement.get("block_name", "")) == grid_key:
                self._save_state()
                self._remove_entire_measurement(idx)
                self.app.statusBar().showMessage(f"Grid {grid['label']} deselected", 2000)
                try:
                    self.app.vtk_widget.GetRenderWindow().Render()
                except Exception:
                    self.app.vtk_widget.render()
                return

        self._save_state()
        z_value = (
            float(world_point[2])
            if self._is_valid_world_point(world_point)
            else float(self._last_z)
        )
        centroid_x, centroid_y = grid["centroid"]
        label_position = (float(centroid_x), float(centroid_y), z_value)
        outline_actor = self._create_block_outline_actor(grid["points"], z_value)
        label_actor = self._create_area_label(label_position, grid["area"], style_key="grid")
        self._scene_add(outline_actor)
        self._scene_add(label_actor)
        self.measurements.append({
            "type": "measure_grid_area",
            "block_name": grid["label"],
            "points": [(float(x), float(y), z_value) for x, y in grid["points"]],
            "labels": [{
                "line": outline_actor,
                "label": label_actor,
                "p1": label_position,
                "p2": label_position,
                "distance": 0.0,
            }],
            "vertices": [],
            "continuous_line": None,
            "total_distance": 0.0,
            "area": float(grid["area"]),
            "source_type": "snt_grid",
            "source_snt": grid.get("source_snt"),
            "source_layer": grid.get("source_layer"),
        })
        self.app.statusBar().showMessage(
            f"Grid {grid['label']} area: {grid['area']:.2f} m²",
            5000,
        )
        try:
            self.app.vtk_widget.GetRenderWindow().Render()
        except Exception:
            self.app.vtk_widget.render()

    def _get_measurement_world_point(self, display_pos=None, allow_focal_fallback=True):
        """
        Resolve the clicked world point robustly across point-cloud and shading modes.
        Measurements must stay on one stable view plane. If point-cloud prop
        hits are preferred, clicks inside loaded data inherit arbitrary cloud
        depth while clicks outside the data use the view plane, producing
        inconsistent metre values for the same plan distance.
        """
        if display_pos is None:
            display_pos = self.interactor.GetEventPosition()
        x, y = display_pos

        if allow_focal_fallback:
            focal_pick = getattr(self.app, "_display_to_world_on_focal_plane", None)
            if callable(focal_pick):
                try:
                    pos = focal_pick(self.renderer, x, y)
                    if self._is_valid_world_point(pos):
                        arr = np.asarray(pos, dtype=np.float64)
                        return self._force_measurement_level((float(arr[0]), float(arr[1]), float(arr[2])))
                except Exception:
                    pass

        # ── Fast gate: PropPicker uses bounding-boxes only (no geometry traversal).
        # Run it FIRST.  If nothing is hit we are outside the loaded data → skip
        # ALL expensive pickers (including app._pick_world_point which runs three
        # the caller falls back to _screen_to_world_fast().  This is the dominant
        # cause of click latency outside the data.
        try:
            prop_picker = vtk.vtkPropPicker()
            prop_hit = prop_picker.Pick(float(x), float(y), 0.0, self.renderer)
            if prop_hit and prop_picker.GetViewProp() is not None:
                pos = prop_picker.GetPickPosition()
                if self._is_valid_world_point(pos):
                    arr = np.asarray(pos, dtype=np.float64)
                    return self._force_measurement_level((float(arr[0]), float(arr[1]), float(arr[2])))
                # Prop was hit but pick position invalid — fall through to precise pickers
                picker_specs = []
                try:
                    point_picker = vtk.vtkPointPicker()
                    point_picker.SetTolerance(0.01)
                    picker_specs.append((point_picker, lambda p: p.GetPointId() >= 0))
                except Exception:
                    pass
                try:
                    cell_picker = vtk.vtkCellPicker()
                    cell_picker.SetTolerance(0.001)
                    picker_specs.append((cell_picker, lambda p: p.GetCellId() >= 0))
                except Exception:
                    pass
                for picker, is_valid in picker_specs:
                    try:
                        if picker.Pick(float(x), float(y), 0.0, self.renderer) and is_valid(picker):
                            pos = picker.GetPickPosition()
                            if self._is_valid_world_point(pos):
                                arr = np.asarray(pos, dtype=np.float64)
                                return self._force_measurement_level((float(arr[0]), float(arr[1]), float(arr[2])))
                    except Exception:
                        continue
            # No prop under cursor — return None immediately so caller uses fast fallback.
            # Avoids O(n) point/cell traversal over the entire point cloud for empty picks.
            else:
                pass
        except Exception:
            pass

        return None

    def _hide_preview_line(self):
        """Hide preview actor from scene without deleting reusable VTK objects."""
        if self.temp_line_actor is not None:
            try:
                self.temp_line_actor.VisibilityOff()
            except Exception:
                pass
        if self._preview_actor_in_scene and self._overlay_renderer is not None:
            try:
                if self.temp_line_actor is not None and self.temp_line_actor.IsA("vtkActor2D"):
                    self._overlay_renderer.RemoveActor2D(self.temp_line_actor)
                else:
                    self._overlay_renderer.RemoveActor(self.temp_line_actor)
            except Exception:
                pass
            try:
                self._overlay_renderer.RemoveViewProp(self.temp_line_actor)
            except Exception:
                pass
            self._preview_actor_in_scene = False

    def _capture_state(self):
        """Capture measurement data (without VTK actors) for undo/redo."""
        state = []
        for measurement in self.measurements:
            entry = {
                'type': measurement.get('type', 'measure_line'),
                'points': [tuple(p) for p in measurement.get('points', [])],
            }
            if measurement.get('type') in ('measure_block_area', 'measure_grid_area'):
                entry['block_name'] = measurement.get('block_name')
                entry['area'] = float(measurement.get('area', 0.0))
                entry['source_type'] = measurement.get('source_type')
                entry['source_prj'] = measurement.get('source_prj')
                entry['source_snt'] = measurement.get('source_snt')
                entry['source_layer'] = measurement.get('source_layer')
            state.append(entry)
        return state

    def _save_state(self):
        """Save current state to undo stack and clear redo stack."""
        self.undo_stack.append(self._capture_state())
        if len(self.undo_stack) > self.max_undo_levels:
            self.undo_stack.pop(0)
        self.redo_stack = []
        print(f"💾 Measurement state saved (undo stack: {len(self.undo_stack)})")

    def _remove_all_measurement_visuals(self):
        """Remove all finalized measurement actors without touching undo/redo."""
        for measurement in self.measurements:
            for label_data in measurement.get('labels', []):
                try:
                    self._scene_remove(label_data.get('line'))
                except Exception:
                    pass
                try:
                    self._scene_remove(label_data.get('label'))
                except Exception:
                    pass

            for vertex in measurement.get('vertices', []):
                try:
                    self._scene_remove(vertex)
                except Exception:
                    pass

            try:
                self._scene_remove(measurement.get('continuous_line'))
            except Exception:
                pass

    def _clear_active_drawing_visuals(self):
        """Remove in-progress measurement actors."""
        # Hide reusable preview actor without destroying it
        self._hide_preview_line()
        if self.temp_line_actor and not self._preview_polydata:
            # Only destroy if it is NOT the reusable preview actor
            try:
                self._scene_remove(self.temp_line_actor)
            except Exception:
                pass
            self.temp_line_actor = None

        if self.continuous_line_actor:
            try:
                self._scene_remove(self.continuous_line_actor)
            except Exception:
                pass
            self.continuous_line_actor = None

        for label_data in self.distance_labels:
            try:
                self._scene_remove(label_data.get('line'))
            except Exception:
                pass
            try:
                self._scene_remove(label_data.get('label'))
            except Exception:
                pass

        for vertex in self.vertex_markers:
            try:
                self._scene_remove(vertex)
            except Exception:
                pass

        self.distance_labels = []
        self.vertex_markers = []

    def _rebuild_active_measurement(self, points, render=True):
        """Rebuild current in-progress measurement from point list."""
        self._clear_active_drawing_visuals()
        self.measurement_points = []

        for point in points:
            pos = tuple(point)
            self.measurement_points.append(pos)

            if len(self.measurement_points) == 1:
                sphere = self._create_vertex_marker(pos, color=(0, 1, 0), radius=0.02)
            else:
                sphere = self._create_vertex_marker(pos, color=(1, 1, 0), radius=0.02)
                if len(self.vertex_markers) > 1:
                    self.vertex_markers[-1].GetProperty().SetColor(1, 1, 0)
                    self.vertex_markers[-1].sphere_source.SetRadius(0.02)

            self._scene_add(sphere)
            self.vertex_markers.append(sphere)

            if len(self.measurement_points) >= 2 and self.mode != "measure_polygon":
                self._update_continuous_line()
                self._create_distance_line()

        # Restore preview line immediately if cursor position is known (e.g. after undo)
        if render and self.measurement_points and self._last_cursor_pos is not None:
            self._update_preview_line(self.measurement_points[-1], self._last_cursor_pos)

        if render:
            self.app.vtk_widget.GetRenderWindow().Render()

    def _recreate_measurement_from_state(self, measurement_state):
        """Recreate one finalized measurement from captured state."""
        points = measurement_state.get('points', [])
        measurement_type = measurement_state.get('type', 'measure_line')

        if measurement_type in ('measure_block_area', 'measure_grid_area'):
            if len(points) < 3:
                return

            polygon_points = [(float(p[0]), float(p[1])) for p in points]
            z_value = float(points[0][2]) if points and len(points[0]) >= 3 else float(self._last_z)
            area = float(measurement_state.get('area', self._calculate_polygon_area_2d(polygon_points)))
            centroid_x, centroid_y = self._calculate_polygon_centroid_2d(polygon_points)
            label_position = (float(centroid_x), float(centroid_y), z_value)

            outline_actor = self._create_block_outline_actor(polygon_points, z_value)
            label_actor = self._create_area_label(
                label_position,
                area,
                style_key="grid" if measurement_type == "measure_grid_area" else "block",
            )
            self._scene_add(outline_actor)
            self._scene_add(label_actor)

            self.measurements.append({
                'type': measurement_type,
                'block_name': measurement_state.get('block_name'),
                'points': [(float(x), float(y), z_value) for x, y in polygon_points],
                'labels': [{
                    'line': outline_actor,
                    'label': label_actor,
                    'p1': label_position,
                    'p2': label_position,
                    'distance': 0.0,
                }],
                'vertices': [],
                'continuous_line': None,
                'total_distance': 0.0,
                'area': area,
                'source_type': measurement_state.get('source_type'),
                'source_prj': measurement_state.get('source_prj'),
                'source_snt': measurement_state.get('source_snt'),
                'source_layer': measurement_state.get('source_layer'),
            })
            return

        if len(points) < 2:
            return

        prev_mode = getattr(self, "mode", "measure_line")
        self.mode = measurement_type
        self._rebuild_active_measurement(points, render=False)
        self._finalize_measurement(push_undo=False)
        self.mode = prev_mode

    def _restore_state(self, state):
        """Restore finalized measurements from captured state."""
        prev_mode = getattr(self, "mode", "measure_line")

        self._clear_selection()
        self._remove_all_measurement_visuals()
        self.measurements = []

        self._clear_active_drawing_visuals()
        self.measurement_points = []
        self._temp_vertex_stack = []

        for measurement_state in state:
            self._recreate_measurement_from_state(measurement_state)

        self.mode = prev_mode
                # ⚡ FIX: must repaint all layers so overlay actors update
        try:
            self.app.vtk_widget.GetRenderWindow().Render()
        except Exception:
            self.app.vtk_widget.render()
        print(f"✅ Measurement state restored: {len(self.measurements)} measurements")

    def undo(self):
        """Undo last measurement operation (Ctrl+Z)."""
        if self.measurement_points:
            if self._temp_vertex_stack:
                prev_points = self._temp_vertex_stack.pop()
                self._rebuild_active_measurement(prev_points)
            else:
                self._rebuild_active_measurement([])
            return

        if not self.undo_stack:
            print("⚠️ Nothing to undo")
            return

        current_state = self._capture_state()
        self.redo_stack.append(current_state)
        # Cap redo stack to same limit as undo
        if len(self.redo_stack) > self.max_undo_levels:
            self.redo_stack.pop(0)
        previous_state = self.undo_stack.pop()
        self._restore_state(previous_state)
        print(f"↶ Undo (undo stack: {len(self.undo_stack)}, redo stack: {len(self.redo_stack)})")

    def redo(self):
        """Redo previously undone measurement operation (Ctrl+Y)."""
        if not self.redo_stack:
            print("⚠️ Nothing to redo")
            return

        current_state = self._capture_state()
        self.undo_stack.append(current_state)
        if len(self.undo_stack) > self.max_undo_levels:
            self.undo_stack.pop(0)

        next_state = self.redo_stack.pop()
        self._restore_state(next_state)
        print(f"↷ Redo (undo stack: {len(self.undo_stack)}, redo stack: {len(self.redo_stack)})")

    def apply_style(self, style: dict):
        """Apply new style to all existing measurements and update units."""
        self._measure_style = style
        if not self.measurements and not self.distance_labels:
            return
            
        print("🎨 Applying updated styles to existing measurements...")
        
        # 1. Update finalized measurements
        for measurement in self.measurements:
            m_type = measurement.get('type')
            mode_key = 'line' if m_type == 'measure_line' else 'path'
            if m_type == 'measure_block_area':
                mode_key = 'block'
            elif m_type == 'measure_grid_area':
                mode_key = 'grid'
                
            sec = style.get(mode_key, {})
            color = sec.get('color')
            width = sec.get('width')
            font_size = sec.get('label_font_size')
            unit = sec.get('unit', 'm')
            
            # Update labels and lines
            for label_data in measurement.get('labels', []):
                # Update line color/width (if applicable)
                if color and 'line' in label_data:
                    line = label_data['line']
                    try:
                        if m_type not in ('measure_block_area', 'measure_grid_area'):
                            line.GetProperty().SetColor(*color)
                        if width:
                            line.GetProperty().SetLineWidth(width)
                    except Exception:
                        pass
                            
                # Update label text and font size
                if 'label' in label_data:
                    lbl = label_data['label']
                    try:
                        if font_size:
                            lbl.GetTextProperty().SetFontSize(font_size)
                        
                        # Update text for unit change
                        dist = label_data.get('distance', 0.0)
                        if m_type in ('measure_block_area', 'measure_grid_area'):
                            area = measurement.get('area', 0.0)
                            if unit == 'km':
                                text = f"Area: {area/1000000.0:.4f} km²"
                            else:
                                if area >= 1: text = f"Area: {area:.2f} m²"
                                else: text = f"Area: {area*10000:.1f} cm²"
                            lbl.SetInput(text)
                        else:
                            # ── Check if this is a summary label (no line) ─
                            if label_data.get('line') is None:
                                # Standard segments have distance > 0, summary might have distance 0 or total
                                # If it's a polygon/path summary, it might have area too
                                if m_type in ('measure_polygon', 'measure_path'):
                                    # We need to re-calculate total distance and area for the label
                                    pts = measurement.get('points', [])
                                    if len(pts) >= 2:
                                        arr = np.asarray(pts)
                                        total_dist = float(np.sum(np.linalg.norm(np.diff(arr, axis=0), axis=1)))
                                        # Handle Area for both Polygon and Path
                                        calculate_area = False
                                        if m_type == 'measure_polygon' and len(pts) >= 3:
                                            total_dist += float(np.linalg.norm(arr[-1] - arr[0]))
                                            calculate_area = True
                                        elif m_type == 'measure_path' and len(pts) >= 3:
                                            calculate_area = True
                                            
                                        if calculate_area:
                                            area = self._calculate_polygon_area(pts)
                                            if unit == 'km':
                                                text = f"▸ {total_dist/1000.0:.3f} km  ◇  {area/1000000.0:.4f} km²"
                                            else:
                                                text = f"▸ {total_dist:.1f} m  ◇  {area:.2f} m²"
                                        else:
                                            if unit == 'km':
                                                text = f"Total: {total_dist/1000.0:.3f} km"
                                            else:
                                                text = f"Total: {total_dist:.2f} m"
                                        lbl.SetInput(text)
                                    else:
                                        pass
                                else:
                                    # Fallback for simple labels
                                    if unit == 'km':
                                        lbl.SetInput(f"↔ {dist/1000.0:.3f}km")
                                    else:
                                        lbl.SetInput(f"↔ {dist:.2f}m")
                            else:
                                # Regular segment label
                                if unit == 'km':
                                    text = f"↔ {dist/1000.0:.3f}km"
                                else:
                                    text = f"↔ {dist:.2f}m"
                                lbl.SetInput(text)
                    except Exception:
                        pass
            
            # Update continuous line if it exists
            cont_line = measurement.get('continuous_line')
            if cont_line:
                try:
                    if color: cont_line.GetProperty().SetColor(*color)
                    if width: cont_line.GetProperty().SetLineWidth(max(1, width - 1))
                except Exception:
                    pass
                
        # 2. Update current in-progress measurement visuals if tool is active
        if self.active:
             # Refresh active labels
             mode_key = 'line' if self.mode == 'measure_line' else 'path'
             sec = style.get(mode_key, {})
             color = sec.get('color')
             width = sec.get('width')
             font_size = sec.get('label_font_size')
             unit = sec.get('unit', 'm')

             for label_data in self.distance_labels:
                 try:
                     if color and 'line' in label_data:
                         label_data['line'].GetProperty().SetColor(*color)
                     if width and 'line' in label_data:
                         label_data['line'].GetProperty().SetLineWidth(width)
                     
                     if 'label' in label_data:
                         lbl = label_data['label']
                         if font_size: lbl.GetTextProperty().SetFontSize(font_size)
                         dist = label_data.get('distance', 0.0)
                         if unit == 'km': lbl.SetInput(f"↔ {dist/1000.0:.3f}km")
                         else: lbl.SetInput(f"↔ {dist:.2f}m")
                 except Exception:
                     pass
             
             if self.continuous_line_actor:
                 try:
                     if color: self.continuous_line_actor.GetProperty().SetColor(*color)
                     if width: self.continuous_line_actor.GetProperty().SetLineWidth(max(1, width-1))
                 except Exception:
                     pass

        try:
            self._render_overlay_only()
        except Exception:
            pass

    def add_cross_section_measurement(self, world_points, source_view=None, show_line=True):
        """
        Mirror a measurement taken in a cross-section view into this (main) view.

        This is an additive, self-contained entry point used by
        CrossSectionMeasurementTool: it reuses the same rendering building
        blocks (_create_screen_line_actor, _create_distance_label,
        _create_vertex_marker, _scene_add) that finalized main-view
        measurements already use, so a mirrored measurement looks and behaves
        like a normal one — but it never reads or mutates any interactive
        drawing state (self.mode, self.measurement_points, undo/redo stacks,
        snap cache), so it cannot interfere with an in-progress main-view
        measurement or any other tool.

        A cross-section measurement is a vertical/profile distance — its two
        endpoints usually sit almost on top of each other in a top-down main
        view, so drawing a full line between them there is misleading rather
        than informative. With show_line=False (the default caller usage),
        only a single value-only label is placed at the segment's world
        midpoint; no line, no per-point vertex markers.
        """
        try:
            pts = [tuple(float(v) for v in p[:3]) for p in world_points]
        except Exception:
            return None
        if len(pts) < 2:
            return None

        color = (1.0, 0.55, 0.0)  # distinguish cross-section-sourced measurements
        labels = []
        total_distance = 0.0

        for i in range(len(pts) - 1):
            p1, p2 = pts[i], pts[i + 1]
            distance = float(np.linalg.norm(np.asarray(p2) - np.asarray(p1)))
            total_distance += distance

            line_actor = None
            if show_line:
                line_actor = self._create_screen_line_actor([p1, p2], color=color, width=3)
                if line_actor:
                    self._scene_add(line_actor)

            midpoint = tuple((a + b) / 2.0 for a, b in zip(p1, p2))
            label_position = midpoint if not show_line else self._get_label_position_above_segment(p1, p2)
            label_actor = self._create_distance_label(label_position, distance)
            if label_actor:
                self._scene_add(label_actor)

            labels.append({
                'line': line_actor,
                'label': label_actor,
                'p1': p1,
                'p2': p2,
                'distance': distance,
            })

        vertices = []
        if show_line:
            for p in pts:
                marker = self._create_vertex_marker(p, color=color)
                if marker:
                    self._scene_add(marker)
                    vertices.append(marker)

        measurement_entry = {
            'type': 'cross_section_line',
            'points': pts,
            'labels': labels,
            'vertices': vertices,
            'continuous_line': None,
            'total_distance': total_distance,
            'area': 0.0,
            'source': 'cross_section',
            'source_view': source_view,
        }
        self.measurements.append(measurement_entry)
        self._invalidate_measure_snap_cache()
        self._render_overlay_only()
        print(f"📏 Cross-section measurement mirrored into main view: {total_distance:.3f} m")
        return measurement_entry

    def remove_measurement_entry(self, measurement_entry):
        """
        Remove one previously finalized measurement (its actors + list entry).

        Additive counterpart to add_cross_section_measurement(), used by
        CrossSectionMeasurementTool's undo. Does not touch the interactive
        undo/redo stack used by main-view drawing (self.undo_stack) — those
        are unrelated histories.
        """
        if measurement_entry is None or measurement_entry not in self.measurements:
            return False

        for label_data in measurement_entry.get('labels', []) or []:
            try:
                self._scene_remove(label_data.get('line'))
            except Exception:
                pass
            try:
                self._scene_remove(label_data.get('label'))
            except Exception:
                pass

        for vertex in measurement_entry.get('vertices', []) or []:
            try:
                self._scene_remove(vertex)
            except Exception:
                pass

        try:
            self._scene_remove(measurement_entry.get('continuous_line'))
        except Exception:
            pass

        try:
            self.measurements.remove(measurement_entry)
        except ValueError:
            pass

        self._invalidate_measure_snap_cache()
        self._render_overlay_only()
        return True

    def activate(self, mode="measure_line"):
        """
        Activate measurement mode with proper tool coordination.
        Keeps existing cleanup/orphan-actor protection and adds snap/grid-Z refresh.
        """
        # CRITICAL FIX:
        # If user switches Line -> Path -> Line without finalizing,
        # remove the old unfinished actors before resetting state.
        if (
            self.measurement_points
            or self.distance_labels
            or self.vertex_markers
            or self.continuous_line_actor is not None
            or self.temp_line_actor is not None
        ):
            self._clear_active_drawing_visuals()
            self.measurement_points = []
            self.distance_labels = []
            self.vertex_markers = []
            self.continuous_line_actor = None
            self._temp_vertex_stack = []

        # Ensure latest settings are loaded when activating.
        self._measure_style = load_measure_settings()

        self.mode = mode
        self.active = True
        self.is_measuring = True
        self.is_panning = False
        self._suspend_left_click_pan()
        self.measurement_points = []
        self.distance_labels = []
        self.vertex_markers = []
        self.continuous_line_actor = None
        self._temp_vertex_stack = []
        self._last_cursor_pos = None
        self._invalidate_measure_snap_cache()

        # Seed _last_z from the camera focal point so the first click outside
        # the point cloud projects onto a sensible world plane instead of Z=0.
        try:
            focal = self.renderer.GetActiveCamera().GetFocalPoint()
            self._last_z = float(focal[2])
        except Exception:
            pass

        # Recompute on every activation because grid attachments / labels can change.
        self._measurement_level_z = self._compute_grid_label_level_z()
        if self._measurement_level_z is not None:
            self._last_z = float(self._measurement_level_z)

        # ✅ CRITICAL FIX: Only remove OUR specific observers, never wipe global events.
        if hasattr(self, '_observer_tags') and self._observer_tags:
            for tag in self._observer_tags:
                try:
                    self.interactor.RemoveObserver(tag)
                except Exception:
                    pass
        self._observer_tags = []

        # ---------------------------------------------------------
        # 🔥 HIGH PRIORITY (2.0): Measurement tool observers
        # ---------------------------------------------------------
        # NOTE: We DO NOT call RemoveObservers("EventType") anymore.
        # This preserves default camera controls (rotate/orbit).
        # ---------------------------------------------------------

        self._tag_left_button = self.interactor.AddObserver("LeftButtonPressEvent", self._on_click, 2.0)
        self._tag_mouse_move  = self.interactor.AddObserver("MouseMoveEvent", self._on_mouse_move, 2.0)
        tag3 = self.interactor.AddObserver("RightButtonPressEvent", self._on_right_click_select, 2.0)
        tag4 = self.interactor.AddObserver("KeyPressEvent", self._on_key_press, 2.0)
        tag5 = self.interactor.AddObserver("MiddleButtonPressEvent", self._on_middle_press, 2.0)
        tag6 = self.interactor.AddObserver("MiddleButtonReleaseEvent", self._on_middle_release, 2.0)
        tag_left_release = self.interactor.AddObserver("LeftButtonReleaseEvent", self._on_left_release, 2.0)
        self._observer_tags.extend([self._tag_left_button, self._tag_mouse_move, tag3, tag4, tag5, tag6, tag_left_release])

        print(f"📏 Measurement tool activated: {mode} (priority 2.0)")
        self.app.statusBar().showMessage(
            "📏 Left-click: measure | Middle-click: pan | Right-click: finish/select | ESC: cancel",
            5000
        )


    def deactivate(self):
        """Deactivate measurement tool but keep selection/deletion active."""
        self.active = False
        self.is_measuring = False
        self.is_panning = False
        self._restore_left_click_pan()

        # Keep observers installed for right-click selection/delete behavior,
        # but remove any unfinished active measurement visuals.
        self._clear_active_drawing_visuals()

        self.measurement_points = []
        self.distance_labels = []
        self.vertex_markers = []
        self.continuous_line_actor = None
        self._temp_vertex_stack = []
        self._last_cursor_pos = None
        self._invalidate_measure_snap_cache()

        try:
            self.app.vtk_widget.GetRenderWindow().Render()
        except Exception:
            self.app.vtk_widget.render()

        print("📏 Measurement drawing deactivated (selection/deletion still active)")
    
    def _on_click(self, _obj, _evt):  # noqa: unused-args — VTK callback signature
        """Handle left click to add measurement point."""
        self.is_panning = False  # Safety reset: left-click always ends any panning state
        if self.interactor.GetShiftKey():
            return  # Allow shift+click for camera
        if not self.is_measuring:
            return
        if self._should_block_measurement():
            return
        # Allow ctrl/alt for camera controls
        if self.interactor.GetControlKey() or self.interactor.GetAltKey():
            return

        # Consume the click so the interactor style does NOT enter rotate state.
        self._safe_abort(_obj)

        if self.mode == "measure_block_area":
            self._measure_block_area_at_click()
            return
        if self.mode == "measure_grid_area":
            self._measure_grid_area_at_click()
            return

        self._temp_vertex_stack.append(list(self.measurement_points))

        pos = self._get_measurement_world_point()
        if pos is None:
            # Fallback: project screen position onto the cached Z plane (same math as preview).
            # This lets the user measure in empty space outside the loaded point cloud.
            try:
                pos = self._screen_to_world_fast()
            except Exception:
                pos = None
        if pos is None:
            self.app.statusBar().showMessage("⚠️ Unable to pick a measurement point here", 2000)
            return
        pos = self._get_snapped_measurement_point(fallback_world=pos, force=True) or pos
        pos = self._force_measurement_level(pos)
        self._last_z = pos[2]  # ← cache Z for fast preview
        self.measurement_points.append(pos)
        
        # Add vertex marker with color coding
        # First point = GREEN, Last point = RED (will update as we add more)
        if len(self.measurement_points) == 1:
            # First point - GREEN
            sphere = self._create_vertex_marker(pos, color=(0, 1, 0), radius=0.02)
        else:
            # Middle points - YELLOW
            sphere = self._create_vertex_marker(pos, color=(1, 1, 0), radius=0.02)
            
            # Update previous "end" vertex from RED back to YELLOW (if it exists and isn't the first)
            if len(self.vertex_markers) > 1:
                self.vertex_markers[-1].GetProperty().SetColor(1, 1, 0)  # Yellow
                self.vertex_markers[-1].sphere_source.SetRadius(0.02)
        
        self._scene_add(sphere)
        self.vertex_markers.append(sphere)
        
        # (verbose per-click prints removed to avoid Python overhead in hot path)
        
        # Update continuous line through all points
       # Update continuous line through all points
        # Update continuous line through all points
        if len(self.measurement_points) >= 2:
            if self.mode != "measure_polygon":
                self._update_continuous_line()
                self._create_distance_line()
            
            if self.mode == "measure_polygon" and len(self.measurement_points) >= 3:
                self._update_polygon_closing_line()
        
        self._render_overlay_only()
    
    def _on_mouse_move(self, obj, evt):
        """Show preview line as mouse moves — throttled to 40 fps, zero-alloc hot path."""
        if self._allow_camera_pan(obj, evt):
            return   # Middle button: allow panning
        if not self.is_measuring or not self.measurement_points:
            return   # Not measuring: allow normal camera rotation

        # Consume the mouse-move event during active measurement so the
        # interactor style does not rotate/pan the camera.
        # Skip abort while panning so the camera style can still execute Pan().
        if not self.is_panning:
            self._safe_abort(obj)

        # ⚡ Frame-rate cap: skip render if last preview was < 25 ms ago
        now = time.monotonic()
        if now - self._last_preview_time < self._preview_interval:
            return
        self._last_preview_time = now

        if self._should_block_measurement():
            return

        try:
            pos = self._screen_to_world_fast()   # no picker — pure camera math
        except Exception:
            return
        pos = self._get_snapped_measurement_point(fallback_world=pos, force=False) or pos
        pos = self._force_measurement_level(pos)

        last_point = self.measurement_points[-1]
        self._last_cursor_pos = pos             # cache for post-undo restore

        # ⚡ Reuse preview actor — no VTK object allocation per frame
        self._update_preview_line(last_point, pos)

        # Distance calculations — use pre-built numpy array to avoid repeated casting
        pts = self.measurement_points
        lp = np.asarray(last_point)
        cp = np.asarray(pos)
        next_distance = float(np.linalg.norm(cp - lp))

        if self.mode == "measure_polygon" and len(pts) >= 3:
            arr = np.asarray(pts)
            current_total = float(np.sum(np.linalg.norm(np.diff(arr, axis=0), axis=1)))
            closing_distance = float(np.linalg.norm(cp - arr[0]))
            total_if_closed = current_total + next_distance + closing_distance
            self.app.statusBar().showMessage(
                f"📏 Current: {current_total:.2f} m | Next: {next_distance:.2f} m | "
                f"Close: {closing_distance:.2f} m | Total: {total_if_closed:.2f} m", 100
            )
        elif self.mode == "measure_path" and len(pts) >= 2:
            arr = np.asarray(pts)
            current_total = float(np.sum(np.linalg.norm(np.diff(arr, axis=0), axis=1)))
            self.app.statusBar().showMessage(
                f"📏 Current: {current_total:.2f} m | Next: {next_distance:.2f} m | "
                f"Total: {current_total + next_distance:.2f} m", 100
            )
        else:
            self.app.statusBar().showMessage(f"📏 Distance: {next_distance:.2f} m", 100)

        self._render_overlay_only()
    
    def _on_right_click_select(self, obj, evt):
        """Right click to select measurement or finalize current drawing."""
        if self.measurement_points and len(self.measurement_points) >= 2:
            self._finalize_measurement()
            # Keep the tool armed so the user can immediately start the next
            # measurement without reopening the menu/ribbon.
            if self.mode in ['measure_line', 'measure_path', 'measure_polygon']:
                self._prepare_next_measurement()
                self.app.statusBar().showMessage(
                    "✅ Measurement completed. Click to start the next one.",
                    3000
                )
            return
            
        # Otherwise, select a measurement
        # Try cell picker first
        self._select_measurement_at_cursor()
        
        if self.selected_measurement_index is None:
            print("🔄 Trying alternative selection method...")
            self._select_measurement_at_cursor_alternative()
            
            if self.selected_measurement_index is None:
                print("🔄 Trying alternative selection method...")
                self._select_measurement_at_cursor_alternative()
        
    def _create_distance_line(self):
        """Create a line with distance label between last two points."""
        if len(self.measurement_points) < 2:
            return
        
        p1 = self.measurement_points[-2]
        p2 = self.measurement_points[-1]
        
        # Calculate distance
        distance = np.linalg.norm(np.array(p2) - np.array(p1))
        
        # Create line actor (make it easier to pick by increasing width)
        line_actor = self._create_line_actor([p1, p2], color=(1, 1, 0), width=4)
        if line_actor:
            self._scene_add(line_actor)
        # Create distance label at midpoint
        if self.mode == "measure_line":
            label_actor = None
        else:
            label_position = self._get_label_position_above_segment(p1, p2)
            label_actor = self._create_distance_label(label_position, distance)
            self._scene_add(label_actor)
        
        # Store for later removal
       # Store for later removal
        label_entry = {
            'line': line_actor,
            'label': label_actor,
            'p1': p1,
            'p2': p2,
            'distance': distance
        }
        self.distance_labels.append(label_entry)
        
        print(f"📏 Distance: {distance:.3f} m (stored {len(self.distance_labels)} labels)")
    
    def _finalize_measurement(self, push_undo=True):
        """Finalize the current measurement."""
        if push_undo and len(self.measurement_points) >= 2:
            self._save_state()

        # Hide preview line — use hide (not destroy) to keep reusable actor for next session
        self._hide_preview_line()
        
        total_distance = 0.0
        area = 0.0
        
        # Calculate total distance for all modes
        if len(self.measurement_points) >= 2:
            for i in range(len(self.measurement_points) - 1):
                p1 = self.measurement_points[i]
                p2 = self.measurement_points[i + 1]
                total_distance += np.linalg.norm(np.array(p2) - np.array(p1))
        
        # Handle polygon mode - close the shape and calculate area
        if self.mode == "measure_polygon" and len(self.measurement_points) >= 3:
            # Add closing segment from last point to first point
            first_point = self.measurement_points[0]
            last_point = self.measurement_points[-1]
            
            # Calculate closing distance
            closing_distance = np.linalg.norm(np.array(first_point) - np.array(last_point))
            total_distance += closing_distance
            
            # Create closing line with distance label
            closing_line_actor = self._create_line_actor([last_point, first_point], color=(1, 0.5, 0), width=4)
            if closing_line_actor:
                self._scene_add(closing_line_actor)
            
            # Create distance label for closing segment
            label_position = self._get_label_position_above_segment(last_point, first_point)
            closing_label_actor = self._create_distance_label(label_position, closing_distance)
            if closing_label_actor:
                self._scene_add(closing_label_actor)
            
            # Store closing segment
            self.distance_labels.append({
                'line': closing_line_actor,
                'label': closing_label_actor,
                'p1': last_point,
                'p2': first_point,
                'distance': closing_distance
            })
            
            # Calculate area
            area = self._calculate_polygon_area(self.measurement_points)
            
            print(f"📏 Polygon perimeter: {total_distance:.3f} m")
            print(f"📐 Polygon area: {area:.3f} m²")
            
            # Create combined summary label at centroid
            centroid = np.mean(self.measurement_points, axis=0)
            summary_label = self._create_polygon_summary_label(centroid, total_distance, area)
            if summary_label:
                self._scene_add(summary_label)
                print(f"  ✅ Created summary label at centroid: {centroid}")
                # Store the summary label so it can be deleted later
                self.distance_labels.append({
                    'line': None,
                    'label': summary_label,
                    'p1': centroid,
                    'p2': centroid,
                    'distance': 0
                })
            else:
                print(f"  ⚠️ Failed to create summary label")
            
            self.app.statusBar().showMessage(
                f"📏 Perimeter: {total_distance:.2f} m | Area: {area:.2f} m²", 
                5000
            )
        
        elif self.mode == "measure_path" and len(self.measurement_points) > 2:
            # Calculate area (treating path as if it were closed)
            area = self._calculate_polygon_area(self.measurement_points)
            
            print(f"📏 Total path distance: {total_distance:.3f} m")
            print(f"📐 Enclosed area: {area:.3f} m²")
            
            # Create combined label at the centroid
            centroid = np.mean(self.measurement_points, axis=0)
            combined_label = self._create_path_summary_label(centroid, total_distance, area)
            if combined_label:
                self._scene_add(combined_label)
                print(f"  ✅ Created summary label at centroid: {centroid}")
                # Store the label so it can be deleted later
                self.distance_labels.append({
                    'line': None,
                    'label': combined_label,
                    'p1': centroid,
                    'p2': centroid,
                    'distance': 0
                })
            else:
                print(f"  ⚠️ Failed to create summary label")
            
            self.app.statusBar().showMessage(
                f"📏 Total: {total_distance:.2f} m | Area: {area:.2f} m²", 
                5000
            )
        
        elif self.mode == "measure_line" and len(self.measurement_points) >= 2:
            print(f"📏 Line distance: {total_distance:.3f} m")
            self.app.statusBar().showMessage(f"📏 Total Distance: {total_distance:.2f} m", 5000)
            last_pt = self.measurement_points[-1]
            second_last_pt = self.measurement_points[-2]
            label_position = self._get_label_position_above_segment(second_last_pt, last_pt)
            total_label = self._create_distance_label(label_position, total_distance)
            if total_label:
                self._scene_add(total_label)
                self.distance_labels.append({
                    'line': None,
                    'label': total_label,
                    'p1': second_last_pt,
                    'p2': last_pt,
                    'distance': total_distance
                })
        if len(self.vertex_markers) > 0:
            if self.mode == "measure_line":
                if len(self.vertex_markers) >= 2:
                    self.vertex_markers[-1].GetProperty().SetColor(1, 0, 0)  # Red
                    self.vertex_markers[-1].sphere_source.SetRadius(0.02)
            elif self.mode in ["measure_path", "measure_polygon"]:
                if len(self.vertex_markers) >= 2:
                    self.vertex_markers[-1].GetProperty().SetColor(1, 0, 0)  # Red
                    self.vertex_markers[-1].sphere_source.SetRadius(0.02)
        
        # Store measurement with all visual elements
        self.measurements.append({
            'type': self.mode,
            'points': list(self.measurement_points),
            'labels': list(self.distance_labels),
            'vertices': list(self.vertex_markers),
            'continuous_line': self.continuous_line_actor,
            'total_distance': total_distance,
            'area': area  # Store area for all modes
        })
        
        
        # Reset for next measurement
        self.measurement_points = []
        self.distance_labels = []
        self.vertex_markers = []
        self.continuous_line_actor = None
        self._temp_vertex_stack = []
        
        self._invalidate_measure_snap_cache()
        self._render_overlay_only()
        print(f"✅ Measurement finalized ({self.mode})")
    
    def clear_all_measurements(self):
        """Remove all finalized and unfinished measurement lines, labels, and points."""
        if self.measurements:
            self._save_state()

        # Clear any selection first.
        self._clear_selection()

        # CRITICAL FIX:
        # Clear unfinished active measurement also.
        # Fixes: draw Line/Path without finalize -> press Clear -> points/lines stay.
        self._clear_active_drawing_visuals()

        # Remove finalized measurement visuals.
        for measurement in self.measurements:
            for label_data in measurement.get('labels', []):
                try:
                    self._scene_remove(label_data.get('line'))
                except Exception:
                    pass
                try:
                    self._scene_remove(label_data.get('label'))
                except Exception:
                    pass

            for vertex in measurement.get('vertices', []):
                try:
                    self._scene_remove(vertex)
                except Exception:
                    pass

            try:
                self._scene_remove(measurement.get('continuous_line'))
            except Exception:
                pass

        # Final safety net:
        # Remove any orphan MeasurementTool actors created before a mode switch/finalize.
        self._remove_all_tracked_measurement_actors()

        self.measurements.clear()
        self._invalidate_measure_snap_cache()
        self.measurement_points = []
        self.distance_labels = []
        self.vertex_markers = []
        self.continuous_line_actor = None
        self._temp_vertex_stack = []

        # Clear undo/redo stacks so history does not bleed after a full clear.
        self.undo_stack.clear()
        self.redo_stack.clear()

        # Reset reusable preview cache cleanly.
        self._hide_preview_line()
        self.temp_line_actor = None
        self._preview_pts = None
        self._preview_polydata = None
        self._preview_actor_in_scene = False

        # ⚡ FIX: vtk_widget.render() only repaints layer 0 (point cloud).
        # Measurement actors live in overlay renderer at layer 1.
        # Must call GetRenderWindow().Render() to repaint ALL layers.
        try:
            self.app.vtk_widget.GetRenderWindow().Render()
        except Exception:
            self.app.vtk_widget.render()

        print("🗑️ All measurements cleared")
    # ============================================================
    # HELPER METHODS
    # ============================================================
    
    def _create_vertex_marker(self, position, color=(1, 1, 0), radius=0.03):
        """
        Create a fixed screen-size vertex marker.
        Draw-tool style default: keep measurement vertex dots hidden so 3D view
        stays clean and non-distracting. The actor is still returned so existing
        undo/delete logic does not need to change.
        """
        pts = vtk.vtkPoints()
        pts.SetDataTypeToDouble()
        pts.InsertNextPoint(float(position[0]), float(position[1]), float(position[2]))

        verts = vtk.vtkCellArray()
        verts.InsertNextCell(1)
        verts.InsertCellPoint(0)

        poly = vtk.vtkPolyData()
        poly.SetPoints(pts)
        poly.SetVerts(verts)

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(poly)
        try:
            mapper.SetResolveCoincidentTopologyToPolygonOffset()
            mapper.SetResolveCoincidentTopologyPolygonOffsetParameters(-4, -4)
        except Exception:
            pass

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)

        prop = actor.GetProperty()
        prop.SetColor(float(color[0]), float(color[1]), float(color[2]))
        prop.SetOpacity(1.0)
        prop.SetPointSize(10.0)
        try:
            prop.SetRenderPointsAsSpheres(True)
        except Exception:
            pass
        try:
            prop.SetDepthTestingEnabled(False)
        except Exception:
            pass

        actor.PickableOff()

        if not bool(getattr(self, "_measurement_show_vertex_markers", False)):
            actor.VisibilityOff()

        class _DummySphere:
            def SetRadius(self, r):
                pass
        actor.sphere_source = _DummySphere()

        return actor

    def _create_line_actor(self, points, color=(1, 1, 0), width=3):
        """
        Create a stable draw-tool-style world-space measurement line.
        The line is kept on the grid-label Z plane and rendered in the overlay
        renderer with depth testing disabled. This avoids 3D shaking caused by
        screen-space Actor2D reprojection while keeping the line visible.
        """
        # --- Apply settings from MeasureSettingsDialog ---
        style = getattr(self, '_measure_style', None)
        if style:
            mode_key = 'line' if getattr(self, 'mode', '') == 'measure_line' else 'path'
            sec = style.get(mode_key, {})
            if color == (1, 1, 0) or color == (1, 0.5, 0) or color == (1.0, 0.65, 0.0):
                color = sec.get('color', color)
            if width in (2, 3, 4):
                width = sec.get('width', width)
        # ------------------------------------------------
        if len(points) < 2:
            return None

        try:
            leveled_points = []
            for p in points:
                if hasattr(self, "_force_measurement_level"):
                    p = self._force_measurement_level(p)
                leveled_points.append((float(p[0]), float(p[1]), float(p[2])))

            origin = np.array(leveled_points[0], dtype=np.float64)

            vtk_points = vtk.vtkPoints()
            vtk_points.SetDataTypeToDouble()
            for p in leveled_points:
                vtk_points.InsertNextPoint(
                    float(p[0]) - origin[0],
                    float(p[1]) - origin[1],
                    float(p[2]) - origin[2],
                )

            line = vtk.vtkPolyLine()
            line.GetPointIds().SetNumberOfIds(len(leveled_points))
            for i in range(len(leveled_points)):
                line.GetPointIds().SetId(i, i)

            cells = vtk.vtkCellArray()
            cells.InsertNextCell(line)

            polydata = vtk.vtkPolyData()
            polydata.SetPoints(vtk_points)
            polydata.SetLines(cells)

            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputData(polydata)
            try:
                mapper.SetResolveCoincidentTopologyToPolygonOffset()
                mapper.SetResolveCoincidentTopologyPolygonOffsetParameters(-1e5, -1e5)
            except Exception:
                pass

            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            prop = actor.GetProperty()
            prop.SetColor(float(color[0]), float(color[1]), float(color[2]))
            prop.SetLineWidth(float(width))
            prop.SetOpacity(1.0)
            prop.LightingOff()
            try:
                prop.SetDepthTestingEnabled(False)
            except Exception:
                pass
            try:
                prop.RenderLinesAsTubesOn()
            except Exception:
                pass
            actor.PickableOn()
            actor.SetPosition(float(origin[0]), float(origin[1]), float(origin[2]))
            return actor

        except Exception as e:
            print(f"  ⚠️ Error creating stable measurement line actor: {e}")
            return None

    def _create_distance_label(self, position, distance):
        """Create a beautiful modern distance label."""
        # --- Apply settings from MeasureSettingsDialog ---
        style = getattr(self, '_measure_style', None)
        font_size = 16
        unit = 'm'
        if style:
            mode_key = 'line' if getattr(self, 'mode', '') == 'measure_line' else 'path'
            sec = style.get(mode_key, {})
            font_size = sec.get('label_font_size', 16)
            unit = sec.get('unit', 'm')
        
        if unit == 'km':
            text = f"↔ {distance/1000.0:.3f}km"
        else:
            text = f"↔ {distance:.2f}m"
        # ------------------------------------------------

        # Create billboard text (always faces camera)
        text_actor = vtk.vtkBillboardTextActor3D()
        text_actor.SetInput(text)
        text_actor.SetPosition(position)
        
        # Style the text - sleek minimal style
        prop = text_actor.GetTextProperty()
        prop.SetColor(1.0, 1.0, 1.0)  # Pure white
        prop.SetFontSize(font_size)
        prop.BoldOn()
        prop.SetBackgroundOpacity(0.0)  # Fully transparent - no box
        prop.SetJustificationToCentered()
        prop.SetVerticalJustificationToCentered()
        prop.SetFrame(False)            # No border
        
        text_actor.PickableOn()
        
        return text_actor
    
    def export_measurements(self):
        """Export measurements to text format."""
        if not self.measurements:
            print("⚠️ No measurements to export")
            return None
        
        output = []
        output.append("="*60)
        output.append("MEASUREMENT REPORT")
        output.append("="*60)
        
        for i, measurement in enumerate(self.measurements, 1):
            output.append(f"\nMeasurement {i} ({measurement['type']}):")
            output.append("-" * 40)

            if measurement.get('type') in ('measure_block_area', 'measure_grid_area'):
                if measurement.get('block_name'):
                    entity_name = "Grid" if measurement.get('type') == 'measure_grid_area' else "Block"
                    output.append(f"  {entity_name}: {measurement['block_name']}")
                if measurement.get('area', 0) > 0:
                    area_name = "Grid Area" if measurement.get('type') == 'measure_grid_area' else "Block Area"
                    output.append(f"  {area_name}: {measurement['area']:.3f} m²")
                if measurement.get('source_prj'):
                    output.append(f"  Source PRJ: {measurement['source_prj']}")
                if measurement.get('source_snt'):
                    output.append(f"  Source SNT: {measurement['source_snt']}")
                continue
            
            for j, label_data in enumerate(measurement['labels'], 1):
                p1 = label_data['p1']
                p2 = label_data['p2']
                dist = label_data['distance']
                
                output.append(f"  Segment {j}:")
                output.append(f"    From: ({p1[0]:.2f}, {p1[1]:.2f}, {p1[2]:.2f})")
                output.append(f"    To:   ({p2[0]:.2f}, {p2[1]:.2f}, {p2[2]:.2f})")
                output.append(f"    Distance: {dist:.3f} m")
            
            # Add total distance and area if available
            if 'total_distance' in measurement and measurement['total_distance'] > 0:
                output.append(f"\n  Total Distance: {measurement['total_distance']:.3f} m")
            
            if 'area' in measurement and measurement['area'] > 0:
                output.append(f"  Total Area: {measurement['area']:.3f} m²")
        
        output.append("\n" + "="*60)
        
        report = "\n".join(output)
        print(report)
        return report
    

    def _update_continuous_line(self):
        """Update the continuous line connecting all measurement points."""
        # Remove old continuous line
        if self.continuous_line_actor:
            try:
                self._scene_remove(self.continuous_line_actor)
            except:
                pass
        
        # Create new continuous line through all points
        # Create new continuous line through all points
        if len(self.measurement_points) >= 2:
            self.continuous_line_actor = self._create_line_actor(
                self.measurement_points, 
                color=(1, 1, 0), 
                width=2
            )
            # Make continuous line non-pickable so only segments can be selected
            self.continuous_line_actor.PickableOff()
            self._scene_add(self.continuous_line_actor)
    
    def _on_key_press(self, obj, evt):
        """Handle key press for deleting measurements."""
        key = self.interactor.GetKeySym()
        key_lower = key.lower() if isinstance(key, str) else ""

        # ═══════════════════════════════════════════════════════════════════
        # ✅ CRITICAL: Measurement undo/redo has EXCLUSIVE priority
        # When measurement tool has any measurements, Ctrl+Z/Y ONLY
        # affects measurements - classification is completely blocked
        # ═══════════════════════════════════════════════════════════════════
        if self.interactor.GetControlKey() and key_lower == "z":
            self.undo()
            return  # Don't fall through to other handlers
        if self.interactor.GetControlKey() and key_lower == "y":
            self.redo()
            return  # Don't fall through to other handlers
        
        if key == "Delete" or key == "BackSpace":
            if self.is_measuring and self.measurement_points:
                print("↶ BackSpace/Delete → Undo last point")
                self.undo()
            elif self.selected_measurement_index is not None:
                self._delete_selected_measurement()
            else:
                print("⚠️ No point to undo or measurement selected. Right-click on a line to select it first.")
        elif key == "Escape":
            if self.is_measuring:
                # Cancel active drawing (works for block mode too, which has no points)
                self._clear_active_drawing_visuals()
                self.measurement_points = []
                self._temp_vertex_stack = []
                self.stop_drawing()
                self.app.statusBar().showMessage("Measurement cancelled (ESC)", 2000)
            else:
                self._clear_selection()
        
    def _delete_selected_measurement(self):
        """Delete the currently selected segment and rebuild the measurement."""
        if self.selected_measurement_index is None or self.selected_segment_index is None:
            print("⚠️ No segment selected")
            return
        
        if self.selected_measurement_index >= len(self.measurements):
            print("⚠️ Invalid selection")
            self.selected_measurement_index = None
            self.selected_segment_index = None
            return
        
        measurement = self.measurements[self.selected_measurement_index]
        
        if self.selected_segment_index >= len(measurement['labels']):
            print("⚠️ Invalid segment")
            return
        
        self._save_state()

        if measurement['type'] == 'measure_line':
            self._remove_entire_measurement(self.selected_measurement_index)
            self.selected_measurement_index = None
            self.selected_segment_index = None
            self.original_colors.clear()
            self.app.vtk_widget.render()
            print("🗑️ Line measurement deleted")
            return
        
        points = measurement.get('points', [])
        
        if not points or len(points) <= 2:
            self._remove_entire_measurement(self.selected_measurement_index)
            self.selected_measurement_index = None
            self.selected_segment_index = None
            self.original_colors.clear()
            self.app.vtk_widget.render()
            return
        
        # Remove the point at selected_segment_index + 1 
        # (because segment i connects point i to point i+1)
        if self.selected_segment_index + 1 < len(points):
            points.pop(self.selected_segment_index + 1)
            print(f"  🗑️ Removed point at index {self.selected_segment_index + 1}")
        elif self.selected_segment_index == len(points) - 1:
            points.pop(-1)
            print(f"  🗑️ Removed last point (closing segment)")
        
        # Clear all visual elements for this measurement
        for label_data in measurement['labels']:
            try:
                if label_data['line']:
                    self._scene_remove(label_data['line'])
                self._scene_remove(label_data['label'])
            except:
                pass
        
        for vertex in measurement.get('vertices', []):
            try:
                self._scene_remove(vertex)
            except:
                pass
        
        if 'continuous_line' in measurement and measurement['continuous_line']:
            try:
                self._scene_remove(measurement['continuous_line'])
            except:
                pass
        
        # Rebuild the entire measurement from remaining points
        measurement['labels'] = []
        measurement['vertices'] = []
        measurement['points'] = points
        
        # Create new vertex markers
        # Create new vertex markers with color coding
        for i, point in enumerate(points):
            if i == 0:
                # First point - GREEN
                sphere = self._create_vertex_marker(point, color=(0, 1, 0), radius=0.02)
            elif i == len(points) - 1:
                # Last point - RED
                sphere = self._create_vertex_marker(point, color=(1, 0, 0), radius=0.02)
            else:
                # Middle points - YELLOW
                sphere = self._create_vertex_marker(point, color=(1, 1, 0), radius=0.02)
            
            self._scene_add(sphere)
            measurement['vertices'].append(sphere)
        
        # Create new segments
        # Create new segments
        total_distance = 0.0
        for i in range(len(points) - 1):
            p1 = points[i]
            p2 = points[i + 1]
            distance = np.linalg.norm(np.array(p2) - np.array(p1))
            total_distance += distance
            
            # Create line
            line_actor = self._create_line_actor([p1, p2], color=(1, 1, 0), width=4)
            self._scene_add(line_actor)
            
            # Create label
            midpoint = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2, (p1[2] + p2[2]) / 2)
            label_actor = self._create_distance_label(midpoint, distance)
            self._scene_add(label_actor)
            
            measurement['labels'].append({
                'line': line_actor,
                'label': label_actor,
                'p1': p1,
                'p2': p2,
                'distance': distance
            })
        
        # CHECK: If no valid segments were created, delete entire measurement
        if len(measurement['labels']) == 0:
            print("⚠️ No valid segments remaining after deletion")
            self._remove_entire_measurement(self.selected_measurement_index)
            self.selected_measurement_index = None
            self.selected_segment_index = None
            self.original_colors.clear()
            self.app.vtk_widget.render()
            return
        
        if measurement['type'] == 'measure_polygon' and len(points) >= 3:
            p1 = points[-1]
            p2 = points[0]
            distance = np.linalg.norm(np.array(p2) - np.array(p1))
            total_distance += distance
            
            line_actor = self._create_line_actor([p1, p2], color=(1, 0.5, 0), width=4)
            self._scene_add(line_actor)
            
            midpoint = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2, (p1[2] + p2[2]) / 2)
            label_actor = self._create_distance_label(midpoint, distance)
            self._scene_add(label_actor)
            
            measurement['labels'].append({
                'line': line_actor,
                'label': label_actor,
                'p1': p1,
                'p2': p2,
                'distance': distance
            })
        elif measurement['type'] == 'measure_polygon' and len(points) < 3:
            # Polygon needs at least 3 points, delete if not enough
            print("⚠️ Polygon needs at least 3 points, deleting measurement")
            self._remove_entire_measurement(self.selected_measurement_index)
            self.selected_measurement_index = None
            self.selected_segment_index = None
            self.original_colors.clear()
            self.app.vtk_widget.render()
            return
        
        # Rebuild continuous line
        if len(points) >= 2 and measurement['type'] in ['measure_path', 'measure_polygon']:
            continuous_line_actor = self._create_line_actor(points, color=(1, 1, 0), width=2)
            continuous_line_actor.PickableOff()
            self._scene_add(continuous_line_actor)
            measurement['continuous_line'] = continuous_line_actor
        
        # Recalculate area and create summary
        # Recalculate area and create summary
        area = 0.0
        if len(points) >= 3:
            area = self._calculate_polygon_area(points)
        
        measurement['total_distance'] = total_distance
        measurement['area'] = area
        
        # Only create summary label if measurement is valid (has actual segments)
        if total_distance > 0.001 and len(points) >= 2:
            # Create new summary label
            centroid = np.mean(points, axis=0)
            if measurement['type'] == 'measure_polygon':
                summary_label = self._create_polygon_summary_label(centroid, total_distance, area)
            elif measurement['type'] == 'measure_path':
                summary_label = self._create_path_summary_label(centroid, total_distance, area)
            else:
                summary_label = None
            
            if summary_label:
                self._scene_add(summary_label)
                measurement['labels'].append({
                    'line': None,
                    'label': summary_label,
                    'p1': centroid,
                    'p2': centroid,
                    'distance': 0
                })
        else:
            # No valid measurement, delete it
            print("⚠️ No valid measurement remaining (total_distance = 0)")
            self._remove_entire_measurement(self.selected_measurement_index)
            self.selected_measurement_index = None
            self.selected_segment_index = None
            self.original_colors.clear()
            self.app.vtk_widget.render()
            return
        
        print(f"✅ Measurement rebuilt: {len(points)} points, {total_distance:.2f} m, {area:.2f} m²")
        
        # Clear selection
        self.selected_measurement_index = None
        self.selected_segment_index = None
        self.original_colors.clear()
        
        self.app.vtk_widget.render()


    def _remove_entire_measurement(self, measurement_index):
        """Helper to remove an entire measurement."""
        if measurement_index >= len(self.measurements):
            return
            
        measurement = self.measurements[measurement_index]
        
        for label_data in measurement['labels']:
            try:
                if label_data['line']:
                    self._scene_remove(label_data['line'])
                self._scene_remove(label_data['label'])
            except:
                pass
        
        for vertex in measurement.get('vertices', []):
            try:
                self._scene_remove(vertex)
            except:
                pass
        
        if 'continuous_line' in measurement and measurement['continuous_line']:
            try:
                self._scene_remove(measurement['continuous_line'])
            except:
                pass
        
        self.measurements.pop(measurement_index)
        self._invalidate_measure_snap_cache()
        print(f"🗑️ Entire measurement removed")

    def _select_measurement_at_cursor(self):
        """Select the individual segment under the cursor."""
        # Use CellPicker for better line picking
        picker = vtk.vtkCellPicker()
        picker.SetTolerance(0.01)  # Increase tolerance for easier picking
        
        pos = self.interactor.GetEventPosition()
        picker_renderer = self._overlay_renderer if self._overlay_renderer is not None else self.renderer
        picker.Pick(pos[0], pos[1], 0, picker_renderer)
        picked_actor = picker.GetActor()
        
        if not picked_actor:
            print("⚠️ No measurement at cursor position")
            return
        
        print(f"🔍 Picked actor: {picked_actor}")
        
        # Clear previous selection
        self._clear_selection()
        
        # Find which specific segment this actor belongs to
        for i, measurement in enumerate(self.measurements):
            for j, label_data in enumerate(measurement['labels']):
                if picked_actor == label_data['line']:
                    # Select this specific segment
                    self.selected_measurement_index = i
                    self.selected_segment_index = j
                    self._highlight_segment(i, j)
                    print(f"✅ Segment {j+1} of Measurement {i+1} selected (Press Delete to remove)")
                    self.app.statusBar().showMessage(f"✅ Segment {j+1} selected (Press Delete to remove)", 3000)
                    return
            
            # Also check continuous line
            # Also check continuous line - if clicked, select the closest segment
            if 'continuous_line' in measurement and measurement['continuous_line']:
                if picked_actor == measurement['continuous_line']:
                    print(f"📍 Clicked continuous line - finding closest segment...")
                    # Use alternative method to find closest segment
                    self._select_closest_segment_to_click(i)
                    return
        
        print("⚠️ Clicked on a non-selectable element")
    def _highlight_measurement(self, index):
        """Highlight the selected measurement in blue."""
        if index >= len(self.measurements):
            return
        
        measurement = self.measurements[index]
        
        # Store original colors and highlight lines
        for label_data in measurement['labels']:
            line_actor = label_data['line']
            # Store original color
            original_color = line_actor.GetProperty().GetColor()
            self.original_colors[id(line_actor)] = original_color
            # Set to blue
            line_actor.GetProperty().SetColor(0, 0.5, 1)  # Bright blue
            line_actor.GetProperty().SetLineWidth(5)  # Make thicker
        
        # Highlight continuous line
        if 'continuous_line' in measurement and measurement['continuous_line']:
            line_actor = measurement['continuous_line']
            original_color = line_actor.GetProperty().GetColor()
            self.original_colors[id(line_actor)] = original_color
            line_actor.GetProperty().SetColor(0, 0.5, 1)  # Bright blue
            line_actor.GetProperty().SetLineWidth(4)  # Make thicker
        
        # Highlight vertices
        for vertex in measurement.get('vertices', []):
            original_color = vertex.GetProperty().GetColor()
            self.original_colors[id(vertex)] = original_color
            vertex.GetProperty().SetColor(0, 0.5, 1)  # Bright blue
        
        self.app.vtk_widget.render()
    
    def _clear_selection(self):
        """Clear the current selection and restore original colors."""
        if self.selected_measurement_index is None:
            return
        
        if self.selected_measurement_index >= len(self.measurements):
            self.selected_measurement_index = None
            self.selected_segment_index = None
            self.original_colors.clear()
            return
        
        measurement = self.measurements[self.selected_measurement_index]
        
        # Restore original colors for all actors that were highlighted
        for actor_id, original_color in self.original_colors.items():
            # Find the actor by ID
            for label_data in measurement['labels']:
                if id(label_data['line']) == actor_id:
                    label_data['line'].GetProperty().SetColor(original_color)
                    label_data['line'].GetProperty().SetLineWidth(3)
            
            for vertex in measurement.get('vertices', []):
                if id(vertex) == actor_id:
                    vertex.GetProperty().SetColor(original_color)
        
        self.selected_measurement_index = None
        self.selected_segment_index = None
        self.original_colors.clear()
        
        self.app.vtk_widget.render()


    def _highlight_segment(self, measurement_index, segment_index):
        """Highlight only the selected segment (one line + its two vertices)."""
        if measurement_index >= len(self.measurements):
            return
        
        measurement = self.measurements[measurement_index]
        
        if segment_index >= len(measurement['labels']):
            return
        
        label_data = measurement['labels'][segment_index]
        
        # Highlight the line segment
        line_actor = label_data['line']
        original_color = line_actor.GetProperty().GetColor()
        self.original_colors[id(line_actor)] = original_color
        line_actor.GetProperty().SetColor(0, 0.5, 1)  # Bright blue
        line_actor.GetProperty().SetLineWidth(5)  # Make thicker
        
        # Highlight the two vertices of this segment
        # Get the points for this segment
        p1 = label_data['p1']
        p2 = label_data['p2']
        
        # Find and highlight the vertices at these positions
        for vertex in measurement.get('vertices', []):
            vertex_pos = vertex.GetMapper().GetInput().GetCenter()
            
            # Check if this vertex is at p1 or p2 (with small tolerance)
            if (abs(vertex_pos[0] - p1[0]) < 0.01 and 
                abs(vertex_pos[1] - p1[1]) < 0.01 and 
                abs(vertex_pos[2] - p1[2]) < 0.01) or \
               (abs(vertex_pos[0] - p2[0]) < 0.01 and 
                abs(vertex_pos[1] - p2[1]) < 0.01 and 
                abs(vertex_pos[2] - p2[2]) < 0.01):
                
                original_color = vertex.GetProperty().GetColor()
                self.original_colors[id(vertex)] = original_color
                vertex.GetProperty().SetColor(0, 0.5, 1)  # Bright blue
        
        self.app.vtk_widget.render()


    def _update_continuous_line_for_measurement(self, measurement_index):
        """Rebuild the continuous line for a measurement after segment deletion."""
        if measurement_index >= len(self.measurements):
            return
        
        measurement = self.measurements[measurement_index]
        
        # Remove old continuous line
        if 'continuous_line' in measurement and measurement['continuous_line']:
            try:
                self._scene_remove(measurement['continuous_line'])
            except:
                pass
        
        # Rebuild points list from remaining segments (excluding summary labels)
        points = []
        if measurement['labels']:
            # Only process actual segments (not summary labels where distance = 0)
            valid_segments = [label_data for label_data in measurement['labels'] 
                            if label_data['distance'] > 0]
            
            if valid_segments:
                # Add first point
                points.append(valid_segments[0]['p1'])
                # Add all second points
                for label_data in valid_segments:
                    points.append(label_data['p2'])
        
        # Create new continuous line only if we have valid points
        if len(points) >= 2:
            continuous_line_actor = self._create_line_actor(points, color=(1, 1, 0), width=2)
            continuous_line_actor.PickableOff()  # Make non-pickable
            self._scene_add(continuous_line_actor)
            measurement['continuous_line'] = continuous_line_actor
        else:
            measurement['continuous_line'] = None


    def _select_measurement_at_cursor_alternative(self):
        """Alternative selection using world coordinates."""
        # Get the 3D world position of the click
        click_pos = self._get_measurement_world_point(allow_focal_fallback=False)
        
        if click_pos is None:
            print("⚠️ Could not get world position")
            return
        
        print(f"🔍 Click position: {click_pos}")
        
        # Clear previous selection
        self._clear_selection()
        
        # Find the closest segment to the click position
        min_distance = float('inf')
        closest_measurement_idx = None
        closest_segment_idx = None
        
        for i, measurement in enumerate(self.measurements):
            for j, label_data in enumerate(measurement['labels']):
                p1 = np.array(label_data['p1'])
                p2 = np.array(label_data['p2'])
                click = np.array(click_pos)
                
                # Calculate distance from click point to line segment
                line_vec = p2 - p1
                line_len = np.linalg.norm(line_vec)
                
                if line_len < 0.001:  # Degenerate segment
                    continue
                
                line_unitvec = line_vec / line_len
                
                # Project click point onto the line
                point_vec = click - p1
                projection_length = np.dot(point_vec, line_unitvec)
                
                # Clamp to segment
                projection_length = max(0, min(line_len, projection_length))
                
                # Get closest point on segment
                closest_point = p1 + line_unitvec * projection_length
                
                # Calculate distance
                distance = np.linalg.norm(click - closest_point)
                
                if distance < min_distance:
                    min_distance = distance
                    closest_measurement_idx = i
                    closest_segment_idx = j
        
        if closest_measurement_idx is not None and min_distance < 1.0:
            self.selected_measurement_index = closest_measurement_idx
            self.selected_segment_index = closest_segment_idx
            self._highlight_segment(closest_measurement_idx, closest_segment_idx)
            print(f"✅ Segment {closest_segment_idx+1} of Measurement {closest_measurement_idx+1} selected (distance: {min_distance:.2f}m)")
            self.app.statusBar().showMessage(f"✅ Segment {closest_segment_idx+1} selected (Press Delete to remove)", 3000)
        else:
            print(f"⚠️ No segment found within 1m (closest was {min_distance:.2f}m away)")


    def _select_closest_segment_to_click(self, measurement_index):
        """Select the segment closest to the click position."""
        click_pos = self._get_measurement_world_point(allow_focal_fallback=False)
        
        if click_pos is None:
            print("⚠️ Could not get world position")
            return
        
        measurement = self.measurements[measurement_index]
        
        # Find the closest segment
        min_distance = float('inf')
        closest_segment_idx = None
        
        for j, label_data in enumerate(measurement['labels']):
            p1 = np.array(label_data['p1'])
            p2 = np.array(label_data['p2'])
            click = np.array(click_pos)
            
            # Calculate distance from click point to line segment
            line_vec = p2 - p1
            line_len = np.linalg.norm(line_vec)
            
            if line_len < 0.001:  # Degenerate segment
                continue
            
            line_unitvec = line_vec / line_len
            
            # Project click point onto the line
            point_vec = click - p1
            projection_length = np.dot(point_vec, line_unitvec)
            
            # Clamp to segment
            projection_length = max(0, min(line_len, projection_length))
            
            # Get closest point on segment
            closest_point = p1 + line_unitvec * projection_length
            
            # Calculate distance
            distance = np.linalg.norm(click - closest_point)
            
            if distance < min_distance:
                min_distance = distance
                closest_segment_idx = j
        
        if closest_segment_idx is not None:
            self.selected_measurement_index = measurement_index
            self.selected_segment_index = closest_segment_idx
            self._highlight_segment(measurement_index, closest_segment_idx)
            print(f"✅ Segment {closest_segment_idx+1} selected (closest to click, {min_distance:.2f}m away)")
            self.app.statusBar().showMessage(f"✅ Segment {closest_segment_idx+1} selected (Press Delete to remove)", 3000)
        else:
            print("⚠️ No valid segment found")

    def get_selected_measurement_total(self):
        """
        Return the total distance of the currently selected measurement(s).

        Backward-compatible behavior:
        - If the tool only has the existing single-segment selection state,
          return that measurement's full stored total_distance.
        - If multiple refs are attached, sum each measurement's full
          total_distance once.
        """
        total = 0.0
        seen = set()

        # Existing single-selection path.
        if self.selected_measurement_index is not None and self.selected_segment_index is not None:
            try:
                measurement = self.measurements[self.selected_measurement_index]
                total += float(measurement.get("total_distance", 0.0))
                seen.add(self.selected_measurement_index)
            except Exception:
                pass

        # Optional future path: multi-selected measurement refs may be attached
        # by the element selection layer or other UI paths.
        selected_refs = getattr(self, "_selected_measurement_refs", None) or []
        for ref in selected_refs:
            try:
                m_idx = int(ref.get("measurement_index"))
            except Exception:
                continue
            key = m_idx
            if key in seen:
                continue
            try:
                measurement = self.measurements[m_idx]
                total += float(measurement.get("total_distance", 0.0))
                seen.add(key)
            except Exception:
                continue

        return total
    
    def _calculate_polygon_area(self, points):
        """
        Calculate the area of a polygon using the Shoelace formula (2D projection).
        Implements Gauss's area formula as specified by the user.
        """
        if len(points) < 3:
            return 0.0
        
        # 1. Use a local relative coordinate system to maintain high precision
        # (Subtract first point from all points to avoid large coordinate issues)
        origin = np.array(points[0])
        local_pts = [(p[0] - origin[0], p[1] - origin[1]) for p in points]
        
        # 2. Apply Shoelace formula: A = 0.5 * |sum(x_i*y_{i+1} - x_{i+1}*y_i)|
        area = 0.0
        n = len(local_pts)
        
        for i in range(n):
            j = (i + 1) % n
            # (x_i * y_{j}) - (x_j * y_i)
            area += local_pts[i][0] * local_pts[j][1]
            area -= local_pts[j][0] * local_pts[i][1]
        
        return abs(area) / 2.0
    
    def _create_world_text_label(
        self,
        text,
        position,
        color=(1.0, 1.0, 1.0),
        font_size=16,
        bold=True,
        background_color=None,
        background_opacity=0.0,
        frame_color=None,
        frame_width=1,
    ):
        """Create fixed-size overlay text anchored to a world position."""
        text_actor = vtk.vtkTextActor()
        text_actor.SetInput(text)

        prop = text_actor.GetTextProperty()
        prop.SetColor(*color)
        prop.SetFontSize(font_size)
        if bold:
            prop.BoldOn()
        else:
            prop.BoldOff()
        prop.SetJustificationToCentered()
        prop.SetVerticalJustificationToCentered()

        if background_color is not None:
            prop.SetBackgroundColor(*background_color)
        prop.SetBackgroundOpacity(background_opacity)

        if frame_color is not None:
            prop.SetFrame(True)
            prop.SetFrameColor(*frame_color)
            prop.SetFrameWidth(frame_width)
        else:
            prop.SetFrame(False)

        coord = text_actor.GetActualPositionCoordinate()
        coord.SetCoordinateSystemToWorld()
        coord.SetValue(
            float(position[0]),
            float(position[1]),
            float(position[2] if len(position) > 2 else 0.0),
        )

        text_actor.GetProperty().SetDisplayLocationToForeground()
        text_actor.PickableOff()
        return text_actor



    def _create_area_label(self, position, area, *, style_key="block"):
        """Create a fixed-size text label showing area."""
        style = getattr(self, '_measure_style', None)
        sec = style.get(style_key, {}) if style else {}
        font_size = sec.get('label_font_size', 24)
        unit = sec.get('unit', 'm')

        if unit == 'km':
            text = f"Area: {area/1000000.0:.4f} km²"
        else:
            if area >= 1:
                text = f"Area: {area:.2f} m²"
            else:
                text = f"Area: {area*10000:.1f} cm²"

        return self._create_world_text_label(
            text=text,
            position=position,
            color=(0.0, 1.0, 0.5),
            font_size=font_size,
            bold=True,
            background_color=(0.0, 0.0, 0.0),
            background_opacity=0.8,
        )
        # Format area string
        if area >= 1000000:
            text = f"Area: {area/1000000:.2f} km²"
        elif area >= 1:
            text = f"Area: {area:.2f} m²"
        else:
            text = f"Area: {area*10000:.1f} cm²"
        
        # Create billboard text (always faces camera)
        text_actor = vtk.vtkBillboardTextActor3D()
        text_actor.SetInput(text)
        text_actor.SetPosition(position)
        
        # Style the text - make it larger and different color
        prop = text_actor.GetTextProperty()
        prop.SetColor(0, 1, 0.5)  # Cyan/green for area
        prop.SetFontSize(24)
        prop.BoldOn()
        prop.SetBackgroundColor(0, 0, 0)
        prop.SetBackgroundOpacity(0.8)
        
        text_actor.PickableOn()  # Make pickable for deletion
        
        return text_actor
    


    def _create_polygon_summary_label(self, position, perimeter, area):
        """Create the summary label for polygon measurements."""
        style = getattr(self, '_measure_style', None)
        sec = style.get('path', {}) if style else {} # Polygon uses path settings
        font_size = sec.get('label_font_size', 12)
        unit = sec.get('unit', 'm')

        if unit == 'km':
            text = f"{perimeter/1000.0:.3f} km | {area/1000000.0:.4f} km²"
        else:
            text = f"{perimeter:.1f} m | {area:.2f} m²"

        return self._create_world_text_label(
            text=text,
            position=position,
            color=(0.2, 1.0, 0.8),
            font_size=font_size,
            bold=True,
            background_color=(0.0, 0.05, 0.1),
            background_opacity=0.9,
            frame_color=(0.0, 0.8, 1.0),
            frame_width=3,
        )
        # Format with consistent meters
        perim_text = f"{perimeter:.1f}"
        area_text = f"{area:.2f}"
        
        # Premium format with gradient symbols
        text = f"◆ {perim_text}m  ◇  {area_text}m²"
        
        # Create billboard text (always faces camera)
        text_actor = vtk.vtkBillboardTextActor3D()
        text_actor.SetInput(text)
        text_actor.SetPosition(position)
        
        # Style - premium neon style
        prop = text_actor.GetTextProperty()
        prop.SetColor(0.2, 1.0, 0.8)  # Neon cyan
        prop.SetFontSize(12)
        prop.BoldOn()
        prop.SetBackgroundColor(0.0, 0.05, 0.1)  # Almost black with blue tint
        prop.SetBackgroundOpacity(0.9)
        prop.SetJustificationToCentered()
        prop.SetVerticalJustificationToCentered()
        prop.SetFrameColor(0.0, 0.8, 1.0)  # Electric blue border
        prop.SetFrame(True)
        prop.SetFrameWidth(3)
        
        text_actor.PickableOn()
        
        return text_actor


    def _create_path_summary_label(self, position, total_distance, area):
        """Create the summary label for path measurements."""
        style = getattr(self, '_measure_style', None)
        sec = style.get('path', {}) if style else {}
        font_size = sec.get('label_font_size', 13)
        unit = sec.get('unit', 'm')

        if unit == 'km':
            text = f"{total_distance/1000.0:.3f} km  ◇  {area/1000000.0:.4f} km²"
        else:
            text = f"{total_distance:.1f} m  ◇  {area:.2f} m²"

        return self._create_world_text_label(
            text=text,
            position=position,
            color=(1.0, 0.9, 0.4), # Premium Gold
            font_size=font_size,
            bold=True,
            background_color=(0.2, 0.12, 0.05), # Dark warm background
            background_opacity=0.85,
            frame_color=(1.0, 0.7, 0.2), # Orange-gold border
            frame_width=2,
        )


    def _create_distance_label(self, position, distance):
        """Create a distance label that stays anchored during zoom."""
        style = getattr(self, '_measure_style', None)
        mode_key = 'line' if getattr(self, 'mode', '') == 'measure_line' else 'path'
        sec = style.get(mode_key, {}) if style else {}
        
        font_size = sec.get('label_font_size', 16)
        unit = sec.get('unit', 'm')
        
        if unit == 'km':
            text = f"{distance/1000.0:.3f} km"
        else:
            text = f"{distance:.2f} m"
        return self._create_world_text_label(
            text=text,
            position=position,
            color=(1.0, 1.0, 1.0),
            font_size=font_size,
            bold=True,
            background_color=(0.0, 0.0, 0.0),
            background_opacity=0.75,
            frame_color=(1.0, 1.0, 1.0),
            frame_width=2,
        )
        # Sleek minimal format
        text = f"— {distance:.2f}m —"
        
        # Create billboard text (always faces camera)
        text_actor = vtk.vtkBillboardTextActor3D()
        text_actor.SetInput(text)
        text_actor.SetPosition(position)
        
        # Style - sleek white-blue
        prop = text_actor.GetTextProperty()
        prop.SetColor(1.0, 1.0, 1.0)  # Pure white
        prop.SetFontSize(16)
        prop.BoldOn()
        prop.SetBackgroundOpacity(0.0)  # No background box
        prop.SetJustificationToCentered()
        prop.SetVerticalJustificationToCentered()
        prop.SetFrame(False)            # No border
        
        text_actor.PickableOn()
        
        return text_actor


    def _create_total_distance_label(self, position, total_distance):
        """Create a fixed-size text label showing total distance/perimeter."""
        style = getattr(self, '_measure_style', None)
        mode_key = 'line' if getattr(self, 'mode', '') == 'measure_line' else 'path'
        sec = style.get(mode_key, {}) if style else {}
        
        font_size = sec.get('label_font_size', 28)
        unit = sec.get('unit', 'm')

        if unit == 'km':
            text = f"Total: {total_distance/1000.0:.3f} km"
        else:
            if total_distance >= 1:
                text = f"Total: {total_distance:.2f} m"
            else:
                text = f"Total: {total_distance*100:.1f} cm"

        return self._create_world_text_label(
            text=text,
            position=position,
            color=(1.0, 0.5, 0.0),
            font_size=font_size,
            bold=True,
            background_color=(0.0, 0.0, 0.0),
            background_opacity=0.9,
        )
        # Format distance string
        if total_distance >= 1000:
            text = f"Total: {total_distance/1000:.2f} km"
        elif total_distance >= 1:
            text = f"Total: {total_distance:.2f} m"
        else:
            text = f"Total: {total_distance*100:.1f} cm"
        
        # Create billboard text (always faces camera)
        text_actor = vtk.vtkBillboardTextActor3D()
        text_actor.SetInput(text)
        text_actor.SetPosition(position)
        
        # Style the text - make it large and prominent
        prop = text_actor.GetTextProperty()
        prop.SetColor(1, 0.5, 0)  # Orange for total
        prop.SetFontSize(28)  # Large font
        prop.BoldOn()
        prop.SetBackgroundColor(0, 0, 0)
        prop.SetBackgroundOpacity(0.9)
        prop.SetJustificationToCentered()
        prop.SetVerticalJustificationToCentered()
        
        text_actor.PickableOn()  # Make pickable for deletion
        
        return text_actor

    def _recalculate_measurement_summary(self, measurement_index):

        """Recalculate and update the summary label after segment deletion."""
        if measurement_index >= len(self.measurements):
            return
        
        measurement = self.measurements[measurement_index]
        
        # Remove old summary label (the one without a line, usually the last one)
        for i in range(len(measurement['labels']) - 1, -1, -1):
            label_data = measurement['labels'][i]
            if label_data['line'] is None:  # This is a summary label
                try:
                    self._scene_remove(label_data['label'])
                    measurement['labels'].pop(i)
                    print("  🗑️ Removed old summary label")
                except:
                    pass
                break
        
        # Recalculate total distance
        total_distance = 0.0
        for label_data in measurement['labels']:
            if label_data['distance'] > 0:  # Skip summary labels
                total_distance += label_data['distance']
        
        # Recalculate points from remaining segments
        points = []
        if measurement['labels']:
            points.append(measurement['labels'][0]['p1'])
            for label_data in measurement['labels']:
                if label_data['distance'] > 0:  # Skip summary labels
                    points.append(label_data['p2'])
        
        # Recalculate area
        area = 0.0
        if len(points) >= 3:
            area = self._calculate_polygon_area(points)
        
        # Update stored values
        measurement['total_distance'] = total_distance
        measurement['area'] = area
        measurement['points'] = points
        
        # Create new summary label at centroid
        if len(points) >= 2:
            centroid = np.mean(points, axis=0)
            
            # Choose the right label type based on measurement type
            if measurement['type'] == 'measure_polygon':
                summary_label = self._create_polygon_summary_label(centroid, total_distance, area)
            elif measurement['type'] == 'measure_path':
                summary_label = self._create_path_summary_label(centroid, total_distance, area)
            else:
                summary_label = self._create_distance_label(centroid, total_distance)
            
            if summary_label:
                self._scene_add(summary_label)
                # Store the new summary label
                measurement['labels'].append({
                    'line': None,
                    'label': summary_label,
                    'p1': centroid,
                    'p2': centroid,
                    'distance': 0
                })
                print(f"  ✅ Created new summary: Total: {total_distance:.2f} m, Area: {area:.2f} m²")


    def stop_drawing(self):
        """Stop drawing mode but keep selection/deletion active."""
        self.is_measuring = False
        self._restore_left_click_pan()

        for attr in ('_tag_left_button', '_tag_mouse_move'):
            tag = getattr(self, attr, None)
            if tag is not None:
                try:
                    self.interactor.RemoveObserver(tag)
                except Exception:
                    pass
                setattr(self, attr, None)

        # CRITICAL FIX:
        # Remove unfinished active visual actors when drawing stops.
        self._clear_active_drawing_visuals()

        self.measurement_points = []
        self.distance_labels = []
        self.vertex_markers = []
        self.continuous_line_actor = None
        self._temp_vertex_stack = []
        self._last_cursor_pos = None
        self._invalidate_measure_snap_cache()

        print("📏 Drawing stopped (selection/deletion still active)")

    def _prepare_next_measurement(self):
        """
        Reset the transient drawing state after a finalize while keeping the
        measurement tool active and ready for the next measurement.
        """
        self.active = True
        self.is_measuring = True
        self.is_panning = False
        self._suspend_left_click_pan()
        self.measurement_points = []
        self.distance_labels = []
        self.vertex_markers = []
        self.continuous_line_actor = None
        self._temp_vertex_stack = []
        self._last_cursor_pos = None
        self._clear_active_drawing_visuals()
        self._invalidate_measure_snap_cache()
        self._render_overlay_only()
        print("📏 Measurement tool armed for next measurement")


    def deactivate_completely(self):
        """
        Completely deactivate measurement tool and remove ALL observers.
        Call this when switching to other tools.
        """
        self.active = False
        self.is_measuring = False
        self.is_panning = False
        self._restore_left_click_pan()

        # ✅ Remove ONLY our stored observer tags
        if hasattr(self, '_observer_tags') and self._observer_tags:
            for tag in self._observer_tags:
                try:
                    self.interactor.RemoveObserver(tag)
                except Exception:
                    pass
            self._observer_tags = []

        # ✅ FIX: Remove camera observer to prevent crash on destruction
        if self._render_observer_tag is not None:
            try:
                cam = self.renderer.GetActiveCamera()
                if cam:
                    cam.RemoveObserver(self._render_observer_tag)
            except Exception:
                pass
            self._render_observer_tag = None

        # Remove unfinished active measurement visuals completely.
        self._clear_active_drawing_visuals()

        self.measurement_points = []
        self.distance_labels = []
        self.vertex_markers = []
        self.continuous_line_actor = None
        self._temp_vertex_stack = []
        self._last_cursor_pos = None
        self._invalidate_measure_snap_cache()
        print("📏 Measurement tool fully deactivated")

    def pause_without_clearing(self):
        """
        Temporarily suspend measurement interaction without deleting the
        current measurement geometry or labels.

        Use this when another tool needs to take over briefly, but the user
        should be able to resume the same measurement session afterward.
        """
        self.active = False
        self.is_measuring = False
        self.is_panning = False
        self._restore_left_click_pan()

        if hasattr(self, '_observer_tags') and self._observer_tags:
            for tag in self._observer_tags:
                try:
                    self.interactor.RemoveObserver(tag)
                except Exception:
                    pass
            self._observer_tags = []

        if self._render_observer_tag is not None:
            try:
                cam = self.renderer.GetActiveCamera()
                if cam:
                    cam.RemoveObserver(self._render_observer_tag)
            except Exception:
                pass
            self._render_observer_tag = None

        print("📏 Measurement tool paused (state preserved)")

    def reinstall_observers(self):
        """Re-register VTK observers without resetting any measurement state.

        Called after another tool (e.g. ElementSelectTool) temporarily removed
        our observers via deactivate_completely().  Restores interactivity for
        selection/deletion without starting a new measurement session.
        """
        # Remove any stale tags first (safety)
        if hasattr(self, '_observer_tags') and self._observer_tags:
            for tag in self._observer_tags:
                try:
                    self.interactor.RemoveObserver(tag)
                except Exception:
                    pass
        self._observer_tags = []

        self._tag_left_button = self.interactor.AddObserver("LeftButtonPressEvent", self._on_click, 2.0)
        self._tag_mouse_move  = self.interactor.AddObserver("MouseMoveEvent", self._on_mouse_move, 2.0)
        tag3 = self.interactor.AddObserver("RightButtonPressEvent", self._on_right_click_select, 2.0)
        tag4 = self.interactor.AddObserver("KeyPressEvent", self._on_key_press, 2.0)
        tag5 = self.interactor.AddObserver("MiddleButtonPressEvent", self._on_middle_press, 2.0)
        tag6 = self.interactor.AddObserver("MiddleButtonReleaseEvent", self._on_middle_release, 2.0)
        tag_left_release = self.interactor.AddObserver("LeftButtonReleaseEvent", self._on_left_release, 2.0)
        self._observer_tags.extend([self._tag_left_button, self._tag_mouse_move, tag3, tag4, tag5, tag6, tag_left_release])
        self._ensure_render_observer()
        print("📏 Measurement tool observers reinstalled (state preserved)")

    def _on_left_release(self, obj, evt):
        if getattr(self, "is_panning", False):
            self.is_panning = False
            self._safe_abort(obj)
            self.interactor.InvokeEvent("MiddleButtonReleaseEvent")
            self.app.vtk_widget.render()

    def _on_middle_press(self, obj, evt):
        """✅ NEW: Handle middle click start (Pan)."""
        self.is_panning = True
        # We do NOT abort the event, so the default interactor style will handle the actual panning
        
    def _on_middle_release(self, obj, evt):
        """Handle middle click end (Pan)."""
        self.is_panning = False
        self.app.vtk_widget.render()

    def _should_block_measurement(self):
        """
        Check if measurement tool should be blocked by other active tools.
        Returns True if another tool should take precedence.
        
        ✅ REMOVED cross-section blocking - tools should coexist peacefully
        """
        # Check digitizer tools (drawing tools like line, rectangle, etc.)
        if hasattr(self.app, 'digitizer'):
            if hasattr(self.app.digitizer, 'active_tool') and self.app.digitizer.active_tool is not None:
                return True
        
        # Check cut section (perpendicular cuts)
        if hasattr(self.app, 'cut_section_controller'):
            if getattr(self.app.cut_section_controller, 'is_drawing', False):
                return True
        
        # ✅ NOTE: Cross-section is NOT blocked anymore - they can coexist
        return False
    
    def _allow_camera_pan(self, obj, evt):
        if evt == "MiddleButtonPressEvent":
            return True
        if self.interactor.GetShiftKey() and evt == "MouseMoveEvent":
            return True
        return False

    def _safe_abort(self, obj):
        aborted = False
        for target in [obj, getattr(self, "interactor", None), getattr(obj, "GetInteractorStyle", lambda: None)()]:
            if target is not None:
                try:
                    if hasattr(target, "AbortFlagOn"):
                        target.AbortFlagOn()
                        aborted = True
                    elif hasattr(target, "SetAbortFlag"):
                        target.SetAbortFlag(1)
                        aborted = True
                except Exception:
                    pass
        return aborted
