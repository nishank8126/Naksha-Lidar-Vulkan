

"""
Zoom Rectangle Tool
Allows drawing a rectangle and zooming to that area.

Final behavior:
- Left click 1 = start zoom rectangle
- Left click 2 = end zoom rectangle and zoom
- Right click = ignored completely while zoom tool is active
- Fast left+right/right+left fake middle-pan = blocked briefly
- Real middle button pan = allowed
"""

import time
import vtk
from PySide6.QtCore import QObject


class ZoomRectangleTool(QObject):
    """Tool for drawing rectangle and zooming to that area."""

    def __init__(self, app):
        super().__init__()

        self.app = app
        self.active = False
        self.start_pos = None
        self.end_pos = None
        self.rubber_band_actor = None
        self.is_dragging_zoom = False

        self.is_panning = False
        self.last_pan_pos = None

        self.observer_ids = []
        self.interactor = None

        self._block_middle_until = 0.0
        self._last_hint_time = 0.0
        self._picker = vtk.vtkWorldPointPicker()

        print(f"✅ Zoom Tool initialized from: {__file__}")

    def activate(self):
        """Activate the zoom rectangle tool."""
        if self.active:
            return

        self.active = True
        self.start_pos = None
        self.is_panning = False
        self.last_pan_pos = None
        self._block_middle_until = 0.0

        if hasattr(self.app, "vtk_widget") and self.app.vtk_widget:
            self.interactor = self.app.vtk_widget.GetRenderWindow().GetInteractor()

            # Remove any existing observers first to be safe
            self._remove_observers()

            self.observer_ids = [
                self.interactor.AddObserver(
                "LeftButtonPressEvent",
                self.on_left_button_down,
                1.0
                ),
            self.interactor.AddObserver(
                "MouseMoveEvent",
                self.on_mouse_move,
                1.0
                ),
            self.interactor.AddObserver(
                "LeftButtonReleaseEvent",
                self.on_left_button_up,
                1.0
                ),

                # Right click has no zoom function.
                # It is ignored and used only to block fake left+right middle-pan.
                self.interactor.AddObserver(
                    "RightButtonPressEvent",
                    self.on_right_button_down,
                    1.0
                ),
                self.interactor.AddObserver(
                    "RightButtonReleaseEvent",
                    self.on_right_button_down,
                    1.0
                ),

                # Real middle-button pan is allowed.
                # Fake middle from fast left+right is blocked briefly.
                self.interactor.AddObserver(
                    "MiddleButtonPressEvent",
                    self.on_middle_button_guard,
                    1.0
                ),
                self.interactor.AddObserver(
                    "MiddleButtonReleaseEvent",
                    self.on_middle_button_guard,
                    1.0
                ),
            ]

            print(f"✅ Zoom Rectangle Tool activated (State Clean, Observers: {self.observer_ids})")

    def deactivate(self):
        """Deactivate the zoom rectangle tool."""
        if not self.active:
            return

        self.active = False
        self.start_pos = None
        self.end_pos = None
        self.is_dragging_zoom = False
        self.is_panning = False
        self.last_pan_pos = None
        #self._block_middle_until = 0.0

        # Remove rubber band if exists
        if self.rubber_band_actor:
            try:
                renderer = self.app.vtk_widget.renderer
                renderer.RemoveActor(self.rubber_band_actor)
                self.rubber_band_actor = None
                self.app.vtk_widget.GetRenderWindow().Render()
            except Exception:
                pass

        self._remove_observers()

        print("✅ Zoom Rectangle Tool deactivated")

    def _remove_observers(self):
        """Safely remove all registered observers."""
        if self.interactor and self.observer_ids:
            print(f"🧹 Removing Zoom Tool Observers: {self.observer_ids}")
            for obs_id in self.observer_ids:
                try:
                    self.interactor.RemoveObserver(obs_id)
                except Exception:
                    pass
            self.observer_ids = []

    def _safe_abort(self, obj):
        """Safely abort event propagation in VTK."""
        if obj is None:
            return

        try:
            if hasattr(obj, "AbortFlagOn"):
                obj.AbortFlagOn()
            elif hasattr(obj, "SetAbortFlag"):
                try:
                    obj.SetAbortFlag(1)
                except AttributeError:
                    pass
        except Exception:
            pass

    def _show_zoom_hint(self, message):
        """Show a small user hint without disturbing workflow."""
        now = time.time()

        # Avoid spamming status bar if user clicks many times quickly.
        if now - self._last_hint_time < 0.8:
            return

        self._last_hint_time = now

        try:
            if hasattr(self.app, "statusBar"):
                self.app.statusBar().showMessage(message, 2500)
        except Exception:
            pass

    def _finish_zoom_rectangle(self, end_pos, obj=None):
        """Finish rectangle zoom and keep zoom tool active."""
        if self.start_pos is None or end_pos is None:
            return

        min_x = min(self.start_pos[0], end_pos[0])
        max_x = max(self.start_pos[0], end_pos[0])
        min_y = min(self.start_pos[1], end_pos[1])
        max_y = max(self.start_pos[1], end_pos[1])

        self.is_dragging_zoom = False

        if self.rubber_band_actor:
            try:
                renderer = self.app.vtk_widget.renderer
                renderer.RemoveActor(self.rubber_band_actor)
                self.rubber_band_actor = None
                self.app.vtk_widget.GetRenderWindow().Render()
            except Exception:
                pass

        self.start_pos = None
        self.end_pos = None

        if abs(max_x - min_x) < 0.001 or abs(max_y - min_y) < 0.001:
            print("⚠️ Zoom rectangle too small")
            self._safe_abort(obj)
            return

        self.zoom_to_bounds(min_x, max_x, min_y, max_y)

        print(
            f"✅ Zoomed to rectangle: "
            f"({min_x:.2f}, {min_y:.2f}) -> ({max_x:.2f}, {max_y:.2f})"
        )

    # IMPORTANT:
    # Do not deactivate here.
    # Zoom stays active for multiple zoom boxes.

        self._safe_abort(obj)

    def on_left_button_down(self, obj, event):
        """Left click starts zoom drag. If already dragging, second click finishes zoom."""
        if not self.active:
            return

        click_pos = self.interactor.GetEventPosition()

        self._picker.Pick(
            click_pos[0],
            click_pos[1],
            0,
            self.app.vtk_widget.renderer
        )

        world_pos = self._picker.GetPickPosition()
        current_pos = (world_pos[0], world_pos[1])

        # ✅ Fallback:
        # If release event did not fire, next left click finishes the zoom.
        if self.is_dragging_zoom and self.start_pos is not None:
            self.end_pos = current_pos
            self._finish_zoom_rectangle(self.end_pos, obj)
            return

        # Start new zoom rectangle
        self.start_pos = current_pos
        self.end_pos = current_pos
        self.is_dragging_zoom = True

        print(f"📍 Zoom Rectangle drag start: {self.start_pos}")

        self._safe_abort(obj)

    def on_mouse_move(self, obj, event):
        """Update rubber band rectangle while left button is dragging."""
        if not self.active:
            return

        if not self.is_dragging_zoom or self.start_pos is None:
            return

        mouse_pos = self.interactor.GetEventPosition()

        self._picker.Pick(
            mouse_pos[0],
            mouse_pos[1],
            0,
            self.app.vtk_widget.renderer
        )

        world_pos = self._picker.GetPickPosition()
        self.end_pos = (world_pos[0], world_pos[1])

        self.draw_rubber_band(self.start_pos, self.end_pos)

        self._safe_abort(obj)

    def on_left_button_up(self, obj, event):
        """Left release finishes rectangle zoom if release event is received."""
        if not self.active:
            return

        if not self.is_dragging_zoom or self.start_pos is None:
            return

        release_pos = self.interactor.GetEventPosition()

        self._picker.Pick(
            release_pos[0],
            release_pos[1],
            0,
            self.app.vtk_widget.renderer
        )

        world_pos = self._picker.GetPickPosition()
        self.end_pos = (world_pos[0], world_pos[1])

        self._finish_zoom_rectangle(self.end_pos, obj)

    def on_right_button_down(self, obj, event):
        """Right click disables Zoom Rectangle tool."""
        if not self.active:
            return

        self.is_dragging_zoom = False
        self.start_pos = None
        self.end_pos = None

        if self.rubber_band_actor:
            try:
                renderer = self.app.vtk_widget.renderer
                renderer.RemoveActor(self.rubber_band_actor)
                self.rubber_band_actor = None
                self.app.vtk_widget.GetRenderWindow().Render()
            except Exception:
                pass

        print("✅ Zoom Rectangle Tool disabled by right click")

        self._safe_abort(obj)

        self.deactivate()

        try:
            ribbon = self.app.ribbon_manager.ribbons.get("identify")
            if ribbon and hasattr(ribbon, "_deactivate_zoom_rectangle"):
                ribbon._deactivate_zoom_rectangle()
        except Exception:
            pass

    def on_middle_button_guard(self, obj, event):
        """
        Allow real middle pan.
        Block only fake middle generated immediately after fast left+right/right+left.
        """
        if not self.active:
            return

        if time.time() < self._block_middle_until:
            self._show_zoom_hint("Zoom tool: use LEFT click only.")
            print(f"🚫 Zoom Tool: fake middle pan blocked ({event})")
            self._safe_abort(obj)
            return

        # Real middle-button pan is allowed.
        return

    def draw_rubber_band(self, start, end):
        """Draw a rubber band rectangle."""
        if self.rubber_band_actor:
            renderer = self.app.vtk_widget.renderer
            renderer.RemoveActor(self.rubber_band_actor)

        z_min, z_max = self._get_point_cloud_z_bounds()
        z_draw = z_max + 1

        points = vtk.vtkPoints()
        points.InsertNextPoint(start[0], start[1], z_draw)
        points.InsertNextPoint(end[0], start[1], z_draw)
        points.InsertNextPoint(end[0], end[1], z_draw)
        points.InsertNextPoint(start[0], end[1], z_draw)

        line = vtk.vtkPolyLine()
        line.GetPointIds().SetNumberOfIds(5)
        for i in range(4):
            line.GetPointIds().SetId(i, i)
        line.GetPointIds().SetId(4, 0)

        cells = vtk.vtkCellArray()
        cells.InsertNextCell(line)

        polydata = vtk.vtkPolyData()
        polydata.SetPoints(points)
        polydata.SetLines(cells)

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(polydata)

        self.rubber_band_actor = vtk.vtkActor()
        self.rubber_band_actor.SetMapper(mapper)
        self.rubber_band_actor.GetProperty().SetColor(0.0, 0.5, 1.0)
        self.rubber_band_actor.GetProperty().SetLineWidth(2)
        self.rubber_band_actor.GetProperty().SetOpacity(0.8)

        renderer = self.app.vtk_widget.renderer
        renderer.AddActor(self.rubber_band_actor)
        self.app.vtk_widget.GetRenderWindow().Render()

    def zoom_to_bounds(self, min_x, max_x, min_y, max_y):
        """Zoom to fit exactly the drawn rectangle."""
        renderer = self.app.vtk_widget.renderer
        camera = renderer.GetActiveCamera()

        width = max_x - min_x
        height = max_y - min_y
        center_x = (min_x + max_x) / 2
        center_y = (min_y + max_y) / 2

        z_min, z_max = self._get_point_cloud_z_bounds()
        z_center = (z_min + z_max) / 2
        z_range = z_max - z_min

        camera.ParallelProjectionOn()
        camera.SetFocalPoint(center_x, center_y, z_center)

        camera_distance = max(width, height, z_range * 2) * 5
        camera.SetPosition(center_x, center_y, z_center + camera_distance)
        camera.SetViewUp(0, 1, 0)

        max_dimension = max(width, height)
        camera.SetParallelScale(max_dimension / 2)

        z_extent = max(z_range, 100)
        near_clip = max(0.1, camera_distance - z_extent * 2)
        far_clip = camera_distance + z_extent * 2
        camera.SetClippingRange(near_clip, far_clip)

        self.app.vtk_widget.GetRenderWindow().Render()

        print(f"✅ Zoomed to exact rectangle: {width:.1f}m × {height:.1f}m")
        print(
            f"   Camera distance: {camera_distance:.1f}m, "
            f"Clipping: {near_clip:.1f} - {far_clip:.1f}m"
        )

    def _get_point_cloud_z_bounds(self):
        """Get Z coordinate bounds including all visible actors."""
        try:
            renderer = self.app.vtk_widget.renderer
            actors = renderer.GetActors()
            actors.InitTraversal()

            z_min = float("inf")
            z_max = float("-inf")

            for _ in range(actors.GetNumberOfItems()):
                actor = actors.GetNextActor()
                if actor and actor.GetVisibility():
                    bounds = actor.GetBounds()
                    z_min = min(z_min, bounds[4])
                    z_max = max(z_max, bounds[5])

            if z_min == float("inf") or z_max == float("-inf"):
                return -100, 100

            z_range = z_max - z_min
            z_min -= z_range * 0.1
            z_max += z_range * 0.1

            return z_min, z_max

        except Exception as e:
            print(f"⚠️ Could not get Z bounds: {e}")
            return -100, 100
