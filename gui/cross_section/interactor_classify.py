# from json import tool
# from vtkmodules.vtkInteractionStyle import vtkInteractorStyleImage, vtkInteractorStyleTrackballCamera
# from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper, vtkCoordinate
# from vtkmodules.vtkCommonDataModel import vtkPolyData, vtkCellArray
# from vtkmodules.vtkCommonCore import vtkPoints
# import vtk
# import numpy as np
# import time
# from builtins import print as _builtin_print
# from gui.vtk_utils import force_vtk_pipeline_update
# # ✅ Shared helper — avoids triggering shading rebuild when no mesh exists yet
# from gui.classification_tools import _shading_mesh_exists
          
# from PySide6.QtWidgets import (
#     QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
#     QDoubleSpinBox, QPushButton, QSlider, QWidget
# )
# from PySide6.QtCore import QTimer, QObject, QEvent
# from PySide6.QtCore import Qt, QPoint, QPointF, QLineF
# from PySide6.QtGui import QPainter, QColor, QPen
# import pyvista as pv

# _INTERACTOR_DEBUG = False


# def print(*args, **kwargs):  # type: ignore[override]
#     if _INTERACTOR_DEBUG:
#         _builtin_print(*args, **kwargs)


# class Camera2DLock:
#     def __init__(self, camera, app, is_cut_section=False):
#         self.camera = camera
#         self.app = app
#         self.is_cut_section = is_cut_section
#         self._in_sync = False

#     def __call__(self, obj, event):
#         if self._in_sync:
#             return
#         self._in_sync = True
#         try:
#             cam = self.camera
#             if cam is None:
#                 return

#             # 1. Enforce Parallel Projection (2D)
#             if not cam.GetParallelProjection():
#                 cam.ParallelProjectionOn()

#             # 2. Enforce ViewUp to be strictly (0, 0, 1)
#             up = cam.GetViewUp()
#             if abs(up[0]) > 1e-4 or abs(up[1]) > 1e-4 or abs(up[2] - 1.0) > 1e-4:
#                 cam.SetViewUp(0.0, 0.0, 1.0)

#             # 3. Enforce Direction of Projection (align Position and Focal Point)
#             view_mode = getattr(self.app, 'cross_view_mode', 'side')
#             pos = list(cam.GetPosition())
#             fp = list(cam.GetFocalPoint())

#             if view_mode == 'side':
#                 # Force X and Z to match between position and focal point
#                 if abs(pos[0] - fp[0]) > 1e-4 or abs(pos[2] - fp[2]) > 1e-4:
#                     pos[0] = fp[0]
#                     pos[2] = fp[2]
#                     cam.SetPosition(pos)
#             else:
#                 # Force Y and Z to match between position and focal point
#                 if abs(pos[1] - fp[1]) > 1e-4 or abs(pos[2] - fp[2]) > 1e-4:
#                     pos[1] = fp[1]
#                     pos[2] = fp[2]
#                     cam.SetPosition(pos)

#         except Exception:
#             pass
#         finally:
#             self._in_sync = False


# def install_camera_2d_lock(vtk_widget, app, is_cut_section=False):
#     """
#     Ensure the active camera of the given VTK widget is locked to 2D.
#     Prevents any rotation or 3D drift at the camera level, while fully
#     preserving panning and zooming.
#     """
#     if vtk_widget is None:
#         return
#     # Guard: do not install lock on the main view
#     if hasattr(app, "vtk_widget") and vtk_widget is app.vtk_widget:
#         # If there was a lock on it, remove it
#         if hasattr(vtk_widget, "_camera_2d_lock_id"):
#             old_cam = getattr(vtk_widget, "_locked_camera", None)
#             if old_cam is not None:
#                 try:
#                     old_cam.RemoveObserver(vtk_widget._camera_2d_lock_id)
#                 except Exception:
#                     pass
#             try:
#                 delattr(vtk_widget, "_camera_2d_lock_id")
#                 delattr(vtk_widget, "_locked_camera")
#             except Exception:
#                 pass
#         return
#     try:
#         renderer = getattr(vtk_widget, "renderer", None)
#         if renderer is None:
#             return
#         camera = renderer.GetActiveCamera()
#         if camera is None:
#             return

#         # Avoid duplicate lock
#         if getattr(vtk_widget, "_locked_camera", None) is camera:
#             # Already locked to this camera, check if observer is active
#             if hasattr(vtk_widget, "_camera_2d_lock_id"):
#                 return

#         # If there was an old lock on a different camera, remove it
#         if hasattr(vtk_widget, "_camera_2d_lock_id"):
#             old_cam = getattr(vtk_widget, "_locked_camera", None)
#             if old_cam is not None:
#                 try:
#                     old_cam.RemoveObserver(vtk_widget._camera_2d_lock_id)
#                 except Exception:
#                     pass
#             try:
#                 delattr(vtk_widget, "_camera_2d_lock_id")
#                 delattr(vtk_widget, "_locked_camera")
#             except Exception:
#                 pass

#         # Create and install new lock callback
#         lock_callback = Camera2DLock(camera, app, is_cut_section)
#         obs_id = camera.AddObserver("ModifiedEvent", lock_callback)
#         vtk_widget._camera_2d_lock_id = obs_id
#         vtk_widget._locked_camera = camera

#         # Enforce initial lock state immediately
#         lock_callback(camera, "ModifiedEvent")
#     except Exception as e:
#         print(f"⚠️ Failed to install 2D Camera Lock: {e}")


# class SpatialGridIndex:
#     """
#     Ultra-fast 2D grid index for point clouds (Main View or Section View).
#     Reduces brush classification time from O(N) to O(1) cell lookup.
#     Built in ~200ms for 15M points; queried in <1ms.
#     """
#     def __init__(self, pts2d):
#         self.valid = False
#         if pts2d is None or len(pts2d) == 0:
#             return
            
#         try:
#             self.pts2d = pts2d
#             self.min_u, self.min_z = pts2d.min(axis=0)
#             max_u, max_z = pts2d.max(axis=0)
            
#             range_u = max(max_u - self.min_u, 1e-6)
#             range_z = max(max_z - self.min_z, 1e-6)
            
#             # Heuristic for grid resolution: target ~150 points per cell
#             n = len(pts2d)
#             res = int(np.sqrt(n / 150))
#             self.grid_res = max(20, min(res, 400)) # Cap resolution
            
#             self.cell_u = range_u / self.grid_res
#             self.cell_z = range_z / self.grid_res
            
#             # Assign points to cells
#             u_idx = ((pts2d[:, 0] - self.min_u) / self.cell_u).astype(np.int32)
#             z_idx = ((pts2d[:, 1] - self.min_z) / self.cell_z).astype(np.int32)
            
#             np.clip(u_idx, 0, self.grid_res - 1, out=u_idx)
#             np.clip(z_idx, 0, self.grid_res - 1, out=z_idx)
            
#             # Flattened grid index
#             flat_idx = u_idx * self.grid_res + z_idx
            
#             # Sort points by grid cell for fast slicing
#             self.sort_perm = np.argsort(flat_idx)
#             self.sorted_flat_idx = flat_idx[self.sort_perm]
            
#             # Find start/end of each cell in the sorted array
#             all_cell_ids = np.arange(self.grid_res * self.grid_res + 1)
#             self.cell_boundaries = np.searchsorted(self.sorted_flat_idx, all_cell_ids)
#             self.valid = True
#         except Exception as e:
#             print(f"⚠️ SpatialGridIndex build failed: {e}")

#     def query_rectangle_minmax(self, u_min, z_min, u_max, z_max):
#         """Returns indices of points within the specified bounding box."""
#         if not self.valid:
#             return np.array([], dtype=np.int32)

#         u0 = max(0, int((u_min - self.min_u) / self.cell_u))
#         u1 = min(self.grid_res - 1, int((u_max - self.min_u) / self.cell_u))
#         z0 = max(0, int((z_min - self.min_z) / self.cell_z))
#         z1 = min(self.grid_res - 1, int((z_max - self.min_z) / self.cell_z))

#         if u0 > u1 or z0 > z1:
#             return np.array([], dtype=np.int32)

#         # Vectorized cell-ID computation — eliminates the Python nested for-loop.
#         iu = np.arange(u0, u1 + 1, dtype=np.int32)
#         iz = np.arange(z0, z1 + 1, dtype=np.int32)
#         cell_ids = (iu[:, None] * self.grid_res + iz[None, :]).ravel()

#         starts = self.cell_boundaries[cell_ids]
#         ends   = self.cell_boundaries[cell_ids + 1]
#         nonempty = ends > starts
#         if not nonempty.any():
#             return np.array([], dtype=np.int32)

#         slices = [self.sort_perm[s:e] for s, e in zip(starts[nonempty], ends[nonempty])]
#         indices = np.concatenate(slices) if len(slices) > 1 else slices[0]

#         # Exact box filter on the candidates
#         sub_pts = self.pts2d[indices]
#         mask = ((sub_pts[:, 0] >= u_min) & (sub_pts[:, 0] <= u_max) &
#                 (sub_pts[:, 1] >= z_min) & (sub_pts[:, 1] <= z_max))
#         return indices[mask]

#     def query_radius(self, u, z, radius):
#         """Legacy helper for circular queries."""
#         return self.query_rectangle_minmax(u-radius, z-radius, u+radius, z+radius)

#     def query_rectangle(self, u, z, radius):
#         """Legacy helper for square queries."""
#         return self.query_rectangle_minmax(u-radius, z-radius, u+radius, z+radius)

# class PreviewOverlay(QWidget):
#     def __init__(self, parent=None):
#         super().__init__(parent)
#         # Allow mouse events to pass through to the VTK interactor
#         self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
#         # Allow a translucent background so we can draw overlays if needed
#         self.setAttribute(Qt.WA_TranslucentBackground, True)
#         # Start hidden; _init_preview_overlay will show and size it.
#         self._shapes = []  # placeholder for future use
#         self.hide()

#     def set_shapes(self, shapes):
#         self._shapes = list(shapes or [])
#         if self._shapes:
#             self.show()
#             self.raise_()
#         else:
#             self.hide()
#         self.update()

#     def clear_shapes(self):
#         self.set_shapes([])

#     def translate_shapes(self, dx, dy):
#         if not self._shapes or (dx == 0 and dy == 0):
#             return

#         shifted_shapes = []
#         for shape in self._shapes:
#             shifted_segments = []
#             for p0, p1 in shape.get("segments", []):
#                 shifted_segments.append((
#                     (float(p0[0]) + dx, float(p0[1]) + dy),
#                     (float(p1[0]) + dx, float(p1[1]) + dy),
#                 ))

#             shifted_shape = dict(shape)
#             shifted_shape["segments"] = shifted_segments
#             shifted_shapes.append(shifted_shape)

#         self._shapes = shifted_shapes
#         self.update()

#     def paintEvent(self, event):
#         painter = QPainter(self)
#         painter.setRenderHint(QPainter.Antialiasing)
#         for shape in self._shapes:
#             color = shape.get("color", (255, 255, 0, 220))
#             width = float(shape.get("width", 2.0))
#             pen = QPen(QColor(*color))
#             pen.setWidthF(max(width, 1.0))
#             pen.setCosmetic(True)
#             painter.setPen(pen)
#             painter.setBrush(Qt.NoBrush)

#             for p0, p1 in shape.get("segments", []):
#                 painter.drawLine(
#                     QLineF(
#                         QPointF(float(p0[0]), float(p0[1])),
#                         QPointF(float(p1[0]), float(p1[1])),
#                     )
#                 )
#         painter.end()


# class ClassificationDoubleClickFilter(QObject):
#     def __init__(self, parent=None):
#         super().__init__(parent)
#         self.ignore_until = 0.0

#     def eventFilter(self, obj, event):
#         if event is not None:
#             et = event.type()
#             if et in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease, QEvent.MouseButtonDblClick):
#                 try:
#                     import time
#                     now = time.time()
                    
#                     if now < self.ignore_until:
#                         # Keep extending the block window slightly if they click continuously
#                         self.ignore_until = max(self.ignore_until, now + 0.3)
#                         event.accept()
#                         return True
                        
#                     if et == QEvent.MouseButtonDblClick:
#                         # Block the double click and start the 500ms ignore window
#                         self.ignore_until = now + 0.5
#                         event.accept()
#                         return True
#                 except Exception:
#                     if et == QEvent.MouseButtonDblClick:
#                         try:
#                             event.accept()
#                         except Exception:
#                             pass
#                         return True
#         return False


# class ClassificationInteractor:
#     """
#     Hybrid interactor for classification in cross-section dock.
#     Left mouse → classification tools.
#     Middle mouse → pan (while tools remain active).
#     """

#     def __init__(self, app, interactor, mode="2d"):
#         """
#         ✅ FIXED: Set self.app FIRST before any operations that might access it
#         """
#         # ✅ CRITICAL: Set instance variables FIRST
#         self.app = app
#         self.interactor = interactor
#         self.mode = mode
#         self.guide_actors = []  # Temporary visual guides (rubber-band lines)
#         self.section_actors = [] 

#         # added by bala
#         # SAFE MULTI-CLASS SYNC (read-only, not modifying app)
#         self.from_classes = getattr(app, "from_classes", None)
#         self.to_class = getattr(app, "to_class", None)

#          # ✅ NEW: Qt-based preview overlay (ultra-fast)
#         self._preview_overlay = None
#         self._last_update_time = 0
#         self._last_render_time = 0 
#         self._update_interval = 16  # ~60 FPS throttle (milliseconds)
#         self._cached_renderer = None
#         self._widget_height = 0
                
#         # ✅ NEW FIX: Deactivate cut section tool if it's in WAITING state
#         if hasattr(app, 'cut_section_controller'):
#             cut_ctrl = app.cut_section_controller
#             if hasattr(cut_ctrl, 'deactivate_if_waiting'):
#                 try:
#                     cut_ctrl.deactivate_if_waiting()
#                     print("✅ Checked and handled cut section tool state")
#                 except Exception as e:
#                     print(f"⚠️ Failed to deactivate cut section: {e}")

#         # Cancel any active cut section mode (legacy code - kept for backwards compatibility)
#         if hasattr(app, 'section_controller') and hasattr(app.section_controller, "_cut_active"):
#             if app.section_controller._cut_active:
#                 app.section_controller.cancel_cut_section()

#         # base style
#         if mode == "2d":
#             self.style = vtkInteractorStyleImage()
#             # Lock to 2D: prevents VTK's default Image3D mode from allowing camera
#             # rotation during rapid click sequences (the root cause of 2D→3D drift).
#             self.style.SetInteractionModeToImage2D()
#         else:
#             self.style = vtkInteractorStyleTrackballCamera()

#         self.interactor.SetInteractorStyle(self.style)

#         # Block Qt-level double clicks to prevent propagation to QDockWidget (which causes deactivation)
#         self._dblclick_filter = ClassificationDoubleClickFilter(self.interactor)
#         self.interactor.installEventFilter(self._dblclick_filter)

#         # disable double-click reset and abort event
#         self.style.AddObserver("LeftButtonDoubleClickEvent", lambda o, e: o.AbortFlagOn() if hasattr(o, "AbortFlagOn") else None)

#         # attach events
#         self.style.AddObserver("LeftButtonPressEvent", self.on_left_press)
#         self.style.AddObserver("LeftButtonReleaseEvent", self.on_left_release)
#         self.style.AddObserver("KeyPressEvent", self.on_key_press)

#         # ✅ CRITICAL FIX: Use custom mouse move handler that ALWAYS shows previews
#         self.style.AddObserver("MouseMoveEvent", self._on_mouse_move_with_preview)

#         # Install 2D camera lock immediately for 2D mode
#         if mode == "2d":
#             try:
#                 vtk_widget = self._get_active_vtk_widget()
#                 is_cut = (getattr(self.app, "active_mode", None) == "cut") or getattr(self, "is_cut_section_mode", False)
#                 install_camera_2d_lock(vtk_widget, self.app, is_cut)
#             except Exception as e:
#                 print(f"⚠️ Initial lock install failed: {e}")

#         # ✅ CUSTOM PAN: Do NOT forward to style.OnMiddleButtonDown/Up — those call
#         # VTK's internal C++ Render() which segfaults on stale render windows.
#         # Instead, implement pan manually with safe deferred rendering.
#         self._is_panning = False
#         self._last_pan_pos = (0, 0)
#         self._pan_render_pending = False
#         self._preview_hidden_for_pan = False
#         self._pan_hidden_actor_names = []
#         self._deferred_left_release_timer = QTimer()
#         self._deferred_left_release_timer.setInterval(16)
#         self._deferred_left_release_timer.timeout.connect(self._check_deferred_left_release)
#         self.style.AddObserver("MiddleButtonPressEvent", self._on_safe_pan_start)
#         self.style.AddObserver("MiddleButtonReleaseEvent", self._on_safe_pan_stop)

#         # Restore scroll-wheel zoom for 2D mode.
#         # SetInteractionModeToImage2D() changes VTK's scroll handler to "Slice"
#         # (steps through image slices) instead of Dolly.  Override it here so
#         # scroll always zooms via ParallelScale regardless of interaction mode.
#         if mode == "2d":
#             self.style.AddObserver("MouseWheelForwardEvent", self._on_scroll_zoom_in)
#             self.style.AddObserver("MouseWheelBackwardEvent", self._on_scroll_zoom_out)

#         # state
#         self.P1 = None
#         self.is_dragging = False 
#         self._gesture_tool = None
#         self.line_actor = None
#         self.dotted_actor = None
#         self.rect_actor = None
#         self.circle_actor = None
#         self.poly_actor = None
#         self.poly_actor_cut = None
#         self.brush_actor = None
#         self.freehand_actor = None

#         # polygon / freehand classification state
#         self.drawing_points = []
#         self.is_drawing_freehand = False
        
#         self._brush_grid_size = 50  # Grid cells for spatial lookup
#         self._brush_spatial_index      = None
#         self._brush_spatial_index_xyz_id = None  # id()-based cache key; rebuild only on new file
#         self._brush_sort_order         = None
#         self._brush_grid_params        = None
#         self._main_grid_index          = None   # SpatialGridIndex for main-view brush
#         self._main_points_2d           = None   # 2-D projection cache for main-view
#         self._prebuild_thread          = None   # background pre-warm thread
#         import threading
#         self._build_index_lock         = threading.Lock()  # guards _build_spatial_index_for_brush


#         # ✅ BRUSH OPTIMIZATION: Smooth interpolation
#         self._brush_interpolation_step = 0.5  # Smaller = smoother
#         self._brush_min_move_distance = 0.1  # Minimum movement to register

#         # ✅ BRUSH OPTIMIZATION: Render throttling
#         self._brush_render_counter = 0
#         self._brush_render_every = 3

#         # Brush stroke state (initialized here so cleanup() is always safe)
#         self._brush_accumulated_mask = None
#         self._brush_old_classes = {}
#         self._brush_frame_chunks = []
#         self._brush_stroke_positions = []
#         self._last_brush_center = None
#         self._brush_needs_render = False
#         self._brush_last_render_time = 0.0
#         self._brush_current_to_class = None
#         self._brush_visible_classes = None

#         # Background worker (kept alive across strokes, stopped in cleanup())
#         self._brush_worker = None
#         self._brush_worker_active = False
#         self._roi_preview = None
        
#         self.click_to_finalize = False
#         self._press_pos = None
#         self._line_press_max_move_px = 0.0
#         self._last_active_tool = getattr(app, "active_classify_tool", None)
#         self._ignore_next_left_release = False
#         self._suppress_release_observer = False

#         print("✅ ClassificationInteractor initialized with live previews")
#         self.style.AddObserver("RightButtonPressEvent", self.on_right_press)

#     def _check_tool_changed(self):
#         """Reset drawing state if the active tool has changed since the last event."""
#         if not hasattr(self, 'app') or self.app is None:
#             return
#         current_tool = getattr(self.app, "active_classify_tool", None)
#         if getattr(self, "_last_active_tool", None) != current_tool:
#             self.click_to_finalize = False
#             self.P1 = None
#             self.is_dragging = False
#             self._ignore_next_left_release = False
#             self._suppress_release_observer = False
#             self.drawing_points = []
#             self.is_drawing_freehand = False
#             self._clear_all_previews()
#             self._last_active_tool = current_tool

#     def _notify_style_left_button_up(self):
#         """
#         Safely forward a synthetic left-button-up to VTK without letting our
#         own release observer re-enter and scramble the click-to-finalize state.
#         """
#         style = getattr(self, "style", None)
#         if style is None or getattr(self, "_suppress_release_observer", False):
#             return

#         self._suppress_release_observer = True
#         try:
#             style.OnLeftButtonUp()
#         except Exception:
#             pass
#         finally:
#             self._suppress_release_observer = False

#     def cleanup(self):
#         """Clear transient preview state before this interactor is discarded."""
#         # Remove all observers added in __init__ to prevent accumulation across
#         # tool switches (each switch creates a new interactor, adding 6+ observers
#         # to the same VTK style; after 100+ switches this causes O(N) dispatch cost
#         # and holds references preventing GC).
#         try:
#             if self.style is not None:
#                 for event in ("LeftButtonPressEvent", "LeftButtonReleaseEvent",
#                               "KeyPressEvent", "MouseMoveEvent",
#                               "MiddleButtonPressEvent", "MiddleButtonReleaseEvent",
#                               "LeftButtonDoubleClickEvent", "RightButtonPressEvent",
#                               "MouseWheelForwardEvent", "MouseWheelBackwardEvent"):
#                     try:
#                         self.style.RemoveObservers(event)
#                     except Exception:
#                         pass
#         except Exception:
#             pass

#         try:
#             self._finalize_pending_main_brush_stroke()
#         except Exception as e:
#             print(f"⚠️ ClassificationInteractor cleanup brush finalize failed: {e}")

#         try:
#             self._clear_all_previews()
#         except Exception as e:
#             print(f"⚠️ ClassificationInteractor cleanup preview clear failed: {e}")

#         overlay = getattr(self, "_preview_overlay", None)
#         if overlay is not None:
#             try:
#                 overlay.hide()
#                 overlay.deleteLater()
#             except Exception as e:
#                 print(f"⚠️ ClassificationInteractor overlay cleanup failed: {e}")
#             self._preview_overlay = None

#         # Remove double-click event filter
#         filter_obj = getattr(self, "_dblclick_filter", None)
#         if filter_obj is not None and getattr(self, "interactor", None) is not None:
#             try:
#                 self.interactor.removeEventFilter(filter_obj)
#             except Exception:
#                 pass
#             self._dblclick_filter = None

#         self.P1 = None
#         self.is_dragging = False
#         self.click_to_finalize = False
#         self._ignore_next_left_release = False
#         self._suppress_release_observer = False
#         self._press_pos = None
#         self.drawing_points = []
#         self.is_drawing_freehand = False
#         self._is_panning = False  # ✅ BUG FIX: Reset panning state on cleanup
#         self._pan_render_pending = False

#         self._hide_preview_overlay_for_pan()
#         timer = getattr(self, "_deferred_left_release_timer", None)
#         if timer is not None:
#             try:
#                 timer.stop()
#             except Exception:
#                 pass
#             try:
#                 timer.timeout.disconnect(self._check_deferred_left_release)
#             except Exception:
#                 pass
#             try:
#                 timer.deleteLater()
#             except Exception:
#                 pass
#             self._deferred_left_release_timer = None

#         # ── Task-2/3: cleanup ROI preview and worker on interactor discard ──
#         try:
#             roi = getattr(self, "_roi_preview", None)
#             if roi is not None:
#                 roi.destroy()
#                 self._roi_preview = None
#         except Exception:
#             pass
#         try:
#             w = getattr(self, "_brush_worker", None)
#             if w is not None:
#                 # 1. Disconnect signal FIRST — prevents queued results from
#                 #    arriving on a partially-destroyed slot after stop().
#                 try:
#                     w.result_ready.disconnect()
#                 except Exception:
#                     pass
#                 # 2. Signal the thread to stop.
#                 w.stop()
#                 # 3. Drain the input queue so the thread doesn't block on get().
#                 try:
#                     import queue as _q
#                     while True:
#                         w._queue.get_nowait()
#                 except Exception:
#                     pass
#                 # 4. Wait for thread to finish; hard-kill if it hangs.
#                 if not w.wait(3000):
#                     w.terminate()
#                     w.wait(500)
#                 self._brush_worker = None
#                 self._brush_worker_active = False
#         except Exception:
#             pass

#     def _get_preview_actor_names(self):
#         return [
#             "line_actor", "dotted_actor", "rect_actor", "circle_actor", "circle_actor_main",
#             "brush_actor", "polygon_actor", "freehand_actor", "poly_actor",
#             "line_actor_cut", "dotted_actor_cut", "rect_actor_cut", "circle_actor_cut",
#             "brush_actor_cut", "freehand_actor_cut", "poly_actor_cut",
#             "_locate_line_actor",
#         ]

#     def _get_active_tool_preview_actor_names(self, tool=None):
#         tool = tool or getattr(self.app, "active_classify_tool", None)
#         vtk_widget = self._get_active_vtk_widget()
#         cut_vtk = getattr(getattr(self.app, "cut_section_controller", None), "cut_vtk", None)
#         suffix = "_cut" if (vtk_widget is not None and cut_vtk is not None and vtk_widget == cut_vtk) else ""
#         mapping = {
#             "above_line": [f"line_actor{suffix}", f"dotted_actor{suffix}"],
#             "below_line": [f"line_actor{suffix}", f"dotted_actor{suffix}"],
#             "parallel_line": [f"line_actor{suffix}", f"dotted_actor{suffix}"],
#             "rectangle": [f"rect_actor{suffix}"],
#             "circle": [f"circle_actor_main"] if suffix == "" and self._is_main_view() else [f"circle_actor{suffix}"],
#             "freehand": [f"freehand_actor{suffix}"],
#             "polygon": [f"poly_actor{suffix}"],
#             "brush": [f"brush_actor{suffix}"],
#         }
#         return mapping.get(tool, [])

#     def _get_active_palette(self):
#         """Helper to get the correct palette for the current active view."""
#         app = getattr(self, "app", None)
#         if app is None:
#             return {}

#         def _get_slot_palette(slot_idx: int):
#             # Priority 1: dialog-owned per-slot palettes (authoritative)
#             for dlg_attr in ("display_mode_dialog", "display_dialog"):
#                 dlg = getattr(app, dlg_attr, None)
#                 vp = getattr(dlg, "view_palettes", None) if dlg is not None else None
#                 if isinstance(vp, dict):
#                     pal = vp.get(slot_idx)
#                     if pal:
#                         return pal

#             # Priority 2: app-owned per-slot palettes
#             vp = getattr(app, "view_palettes", None)
#             if isinstance(vp, dict):
#                 pal = vp.get(slot_idx)
#                 if pal:
#                     return pal

#             # Priority 3: canonical main palette fallback
#             if slot_idx == 0:
#                 return getattr(app, "class_palette", {}) or {}
#             return {}

#         def _seed_isolated_slot(slot_idx: int):
#             # Prefer app-level helper (keeps behavior aligned with AppWindow).
#             if hasattr(app, "_get_cross_section_palette"):
#                 try:
#                     pal = app._get_cross_section_palette(
#                         slot_idx,
#                         allow_default_seed=True,
#                         persist_seed=False,
#                     )
#                     if isinstance(pal, dict) and pal:
#                         return pal
#                 except Exception:
#                     pass

#             # Local fallback: clone main colors/weights with independent visibility.
#             master = getattr(app, "class_palette", {}) or {}
#             if not isinstance(master, dict) or not master:
#                 return {}
#             seeded = {}
#             for code, info in master.items():
#                 if not isinstance(info, dict):
#                     continue
#                 try:
#                     code_i = int(code)
#                 except Exception:
#                     continue
#                 seeded[code_i] = {
#                     "show": True,
#                     "description": str(info.get("description", "")),
#                     "color": tuple(info.get("color", (128, 128, 128))),
#                     "weight": float(info.get("weight", 1.0)),
#                     "draw": info.get("draw", ""),
#                     "lvl": str(info.get("lvl", "")),
#                 }
#             return seeded

#         # Main view must always use slot 0, regardless of section active_view state.
#         if self._is_main_view():
#             return _get_slot_palette(0)

#         vtk_widget = self._get_active_vtk_widget()

#         # Cut section uses slot 5. If not configured yet, seed isolated defaults.
#         cut_ctrl = getattr(app, "cut_section_controller", None)
#         if cut_ctrl is not None:
#             cut_vtk = getattr(cut_ctrl, "cut_vtk", None)
#             if cut_vtk is not None and vtk_widget == cut_vtk:
#                 return _get_slot_palette(5) or _seed_isolated_slot(5)

#         # Cross-section dock: view_idx is 0-based, DisplayMode slots are 1-based.
#         view_idx = self._get_view_index_from_interactor()
#         if view_idx is not None:
#             try:
#                 slot_idx = int(view_idx) + 1
#             except Exception:
#                 slot_idx = None
#             if slot_idx is not None:
#                 pal = _get_slot_palette(slot_idx)
#                 if pal:
#                     return pal
#                 seeded = _seed_isolated_slot(slot_idx)
#                 if seeded:
#                     return seeded

#         # Secondary fallback via section controller active view (also 0-based).
#         sec_ctrl = getattr(app, "section_controller", None)
#         if sec_ctrl is not None:
#             active_view = getattr(sec_ctrl, "active_view", None)
#             if active_view is not None:
#                 try:
#                     slot_idx = int(active_view) + 1
#                 except Exception:
#                     slot_idx = None
#                 if slot_idx is not None:
#                     pal = _get_slot_palette(slot_idx)
#                     if pal:
#                         return pal
#                     seeded = _seed_isolated_slot(slot_idx)
#                     if seeded:
#                         return seeded

#         # If context was not resolvable, do not leak slot-0 visibility.
#         return {}

#     def _suppress_snt_text(self, suppress: bool):
#         """
#         🚀 Senior Optimization: Suppress heavy SNT/DXF text actors during drag.
#         SNT labels (vtkVectorText) are extremely heavy due to high polygon counts 
#         and thousands of separate DrawCalls. Hiding them during interaction 
#         restores 60 FPS while keeping the context points visible.
#         """
#         app = getattr(self, 'app', None)
#         if app is None: return
        
#         if suppress:
#             if getattr(self, '_snt_text_suppressed', False):
#                 return
            
#             self._suppressed_text_actors = []
#             # Gather all SNT actors
#             for entry in getattr(app, 'snt_actors', []):
#                 for actor in entry.get('actors', []):
#                     # Only hide text labels (tagged in snt_attachment.py)
#                     if getattr(actor, 'is_grid_label', False):
#                         if actor.GetVisibility():
#                             actor.VisibilityOff()
#                             self._suppressed_text_actors.append(actor)
            
#             self._snt_text_suppressed = True
#             if len(self._suppressed_text_actors) > 50:
#                 print(f"   🚀 SNT Text Suppressed: Hiding {len(self._suppressed_text_actors)} heavy labels for 60FPS interaction")
#         else:
#             if not getattr(self, '_snt_text_suppressed', False):
#                 return
            
#             for actor in getattr(self, '_suppressed_text_actors', []):
#                 try:
#                     actor.VisibilityOn()
#                 except Exception:
#                     pass
            
#             self._suppressed_text_actors = []
#             self._snt_text_suppressed = False

#     def _set_preview_visibility(self, visible, actor_names=None):
#         """Toggle preview actors without clearing the active classification state."""
#         actor_names = actor_names or self._get_preview_actor_names()

#         for name in actor_names:
#             actor = getattr(self, name, None)
#             if actor is None:
#                 continue
#             if visible:
#                 actor.VisibilityOn()
#             else:
#                 actor.VisibilityOff()

#     def _get_visible_preview_actor_names(self, actor_names=None):
#         visible = []
#         vtk_widget = self._get_active_vtk_widget()
#         renderer = getattr(vtk_widget, "renderer", None) if vtk_widget is not None else None
#         actor_names = actor_names or self._get_preview_actor_names()
#         for name in actor_names:
#             actor = getattr(self, name, None)
#             if actor is None:
#                 continue
#             try:
#                 if renderer is not None and hasattr(renderer, "HasViewProp"):
#                     if not renderer.HasViewProp(actor):
#                         continue
#                 if actor.GetVisibility():
#                     visible.append(name)
#             except Exception:
#                 continue
#         return visible

#     def _collect_preview_overlay_shapes(self, actor_names=None):
#         """Capture currently visible preview actors as display-space line segments."""
#         actor_names = actor_names or self._get_preview_actor_names()
#         vtk_widget = self._get_active_vtk_widget()
#         renderer = getattr(vtk_widget, "renderer", None) if vtk_widget is not None else None

#         shapes = []
#         id_list = vtk.vtkIdList()

#         for name in actor_names:
#             actor = getattr(self, name, None)
#             if actor is None:
#                 continue

#             try:
#                 if renderer is not None and hasattr(renderer, "HasViewProp"):
#                     if not renderer.HasViewProp(actor):
#                         continue
#                 if not actor.GetVisibility():
#                     continue
#             except Exception:
#                 continue

#             try:
#                 mapper = actor.GetMapper()
#                 poly = mapper.GetInput() if mapper is not None else None
#                 points = poly.GetPoints() if poly is not None else None
#                 lines = poly.GetLines() if poly is not None else None
#             except Exception:
#                 continue

#             if points is None or lines is None:
#                 continue

#             try:
#                 lines.InitTraversal()
#             except Exception:
#                 continue

#             segments = []
#             while lines.GetNextCell(id_list):
#                 if id_list.GetNumberOfIds() < 2:
#                     continue

#                 prev_id = id_list.GetId(0)
#                 prev_pt = points.GetPoint(prev_id)
#                 for idx in range(1, id_list.GetNumberOfIds()):
#                     curr_id = id_list.GetId(idx)
#                     curr_pt = points.GetPoint(curr_id)

#                     # Freeze overlay in DISPLAY space so it stays visually fixed
#                     prev_xy = (float(prev_pt[0]), float(prev_pt[1]))
#                     curr_xy = (float(curr_pt[0]), float(curr_pt[1]))
#                     # Most classification preview actors are vtkActor2D with
#                     # display-space points already. Only world-project non-2D actors.
#                     if renderer is not None and not isinstance(actor, vtk.vtkActor2D):
#                         try:
#                             coord = vtk.vtkCoordinate()
#                             coord.SetCoordinateSystemToWorld()
#                             coord.SetValue(float(prev_pt[0]), float(prev_pt[1]), float(prev_pt[2]))
#                             d0 = coord.GetComputedDisplayValue(renderer)
#                             coord.SetValue(float(curr_pt[0]), float(curr_pt[1]), float(curr_pt[2]))
#                             d1 = coord.GetComputedDisplayValue(renderer)
#                             prev_xy = (float(d0[0]), float(d0[1]))
#                             curr_xy = (float(d1[0]), float(d1[1]))
#                         except Exception:
#                             pass

#                     segments.append(
#                         (
#                             prev_xy,
#                             curr_xy,
#                         )
#                     )
#                     prev_pt = curr_pt

#             if not segments:
#                 continue

#             prop = actor.GetProperty() if hasattr(actor, "GetProperty") else None
#             color = (255, 255, 0, 220)
#             width = 2.0
#             if prop is not None:
#                 try:
#                     rgb = prop.GetColor()
#                     opacity = prop.GetOpacity()
#                     color = (
#                         int(max(0.0, min(1.0, rgb[0])) * 255),
#                         int(max(0.0, min(1.0, rgb[1])) * 255),
#                         int(max(0.0, min(1.0, rgb[2])) * 255),
#                         int(max(0.0, min(1.0, opacity)) * 255),
#                     )
#                     width = float(prop.GetLineWidth())
#                 except Exception:
#                     pass

#             shapes.append({
#                 "segments": segments,
#                 "color": color,
#                 "width": width,
#             })

#         return shapes

#     def _show_preview_overlay_for_pan(self):
#         """Freeze the current preview in a Qt overlay while the camera pans."""
#         tool = getattr(self, "_gesture_tool", None) or getattr(self.app, "active_classify_tool", None)
#         active_actor_names = self._get_active_tool_preview_actor_names(tool)
#         if active_actor_names:
#             non_active_actor_names = [
#                 n for n in self._get_preview_actor_names() if n not in active_actor_names
#             ]
#             # Ensure stale previews from other tools/views are not visible during pan.
#             self._set_preview_visibility(False, non_active_actor_names)
#         self._pan_hidden_actor_names = self._get_visible_preview_actor_names(active_actor_names)
#         if not self._pan_hidden_actor_names:
#             return

#         shapes = self._collect_preview_overlay_shapes(self._pan_hidden_actor_names)
#         if not shapes:
#             # Keep live preview visible if overlay freeze data is unavailable.
#             self._preview_hidden_for_pan = False
#             return

#         if self._preview_overlay is None:
#             self._init_preview_overlay()
#         else:
#             self._update_overlay_geometry()

#         if self._preview_overlay is None:
#             return

#         self._preview_overlay.set_shapes(shapes)
#         try:
#             self._update_overlay_geometry()
#             self._preview_overlay.show()
#             self._preview_overlay.raise_()
#             self._preview_overlay.update()
#         except Exception:
#             pass
#         # Freeze mode: hide live preview actors so the visible overlay
#         # stays fixed while camera pans.
#         self._set_preview_visibility(False, self._pan_hidden_actor_names)
#         self._preview_hidden_for_pan = True

#     def _hide_preview_overlay_for_pan(self):
#         """Dismiss the frozen overlay preview and restore VTK preview actors."""
#         overlay = getattr(self, "_preview_overlay", None)
#         if overlay is not None:
#             try:
#                 overlay.clear_shapes()
#             except Exception:
#                 try:
#                     overlay.hide()
#                 except Exception:
#                     pass

#         hidden_actor_names = getattr(self, "_pan_hidden_actor_names", [])
#         if getattr(self, "_preview_hidden_for_pan", False):
#             self._set_preview_visibility(True, hidden_actor_names)

#         self._pan_hidden_actor_names = []
#         self._preview_hidden_for_pan = False

#     def _translate_preview_actors_for_pan(self, dx, dy, actor_names=None):
#         """Shift live preview actors in display space so they stay aligned while panning."""
#         if dx == 0 and dy == 0:
#             return

#         actor_names = actor_names or self._get_preview_actor_names()

#         for name in actor_names:
#             actor = getattr(self, name, None)
#             if actor is None:
#                 continue

#             try:
#                 if not actor.GetVisibility():
#                     continue
#             except Exception:
#                 continue

#             try:
#                 mapper = actor.GetMapper()
#                 poly = mapper.GetInput() if mapper is not None else None
#                 points = poly.GetPoints() if poly is not None else None
#                 if points is None:
#                     continue

#                 for idx in range(points.GetNumberOfPoints()):
#                     x, y, z = points.GetPoint(idx)
#                     points.SetPoint(idx, x + dx, y + dy, z)

#                 points.Modified()
#                 if poly is not None:
#                     poly.Modified()
#             except Exception:
#                 continue

#         overlay = getattr(self, "_preview_overlay", None)
#         if overlay is not None and getattr(self, "_preview_hidden_for_pan", False):
#             try:
#                 overlay.translate_shapes(dx, dy)
#             except Exception:
#                 pass

#     def _has_active_drag_classification(self, tool=None):
#         """Return True while a left-drag classification gesture is in progress."""
#         if getattr(self, "click_to_finalize", False):
#             return False  # waiting for second tap — not a drag
#         app = getattr(self, "app", None)
#         tool = tool or getattr(app, "active_classify_tool", None)
#         if tool == "freehand":
#             return bool(getattr(self, "is_drawing_freehand", False))
#         return bool(getattr(self, "is_dragging", False) and getattr(self, "P1", None) is not None)

#     def _stop_deferred_left_release_watch(self):
#         timer = getattr(self, "_deferred_left_release_timer", None)
#         if timer is None:
#             return
#         try:
#             timer.stop()
#         except Exception:
#             pass

#     def _start_deferred_left_release_watch(self):
#         """Fallback for the left-release event path during/after middle-button pan."""
#         if getattr(self, "app", None) is None:
#             return
#         if not self._has_active_drag_classification():
#             return

#         timer = getattr(self, "_deferred_left_release_timer", None)
#         if timer is None:
#             return

#         if not timer.isActive():
#             timer.start()

#     def _check_deferred_left_release(self):
#         """
#         Some middle-pan sequences do not forward a final LeftButtonReleaseEvent
#         through VTK. When that happens, finish the paused classification as soon
#         as Qt reports the left button is no longer pressed.
#         """
#         if getattr(self, "app", None) is None:
#             self._stop_deferred_left_release_watch()
#             return

#         if not self._has_active_drag_classification():
#             self._stop_deferred_left_release_watch()
#             return

#         try:
#             from PySide6.QtWidgets import QApplication
#             buttons = QApplication.mouseButtons()
#         except Exception:
#             return

#         middle_pressed = bool(buttons & Qt.MiddleButton)

#         left_pressed = bool(buttons & Qt.LeftButton)

#         # Keep our internal pan state in sync even if VTK misses MiddleButtonReleaseEvent.
#         if getattr(self, "_is_panning", False) and not middle_pressed:
#             self._is_panning = False
#             self._last_mouse_move_time = 0.0
#             self._last_render_time = 0.0
#             self._last_pan_pos = (0, 0)
#             self._hide_preview_overlay_for_pan()

#         if left_pressed:
#             return

#         if getattr(self, "_is_panning", False):
#             self._is_panning = False
#             self._last_pan_pos = (0, 0)
#             self._hide_preview_overlay_for_pan()

#         self._stop_deferred_left_release_watch()
#         self.on_left_release(self.interactor, "DeferredLeftReleaseAfterPan")

#     def _finalize_pending_main_brush_stroke(self):
#         """
#         Finalize a live main-view brush stroke when the tool ends without a
#         normal mouse-release event, so undo/redo still works.
#         """
#         if not self._is_main_view():
#             return None

#         mask = getattr(self, "_brush_accumulated_mask", None)
#         old_classes_dict = getattr(self, "_brush_old_classes", None) or {}
#         to_class = getattr(self.app, "to_class", None)
#         undo_mask = None

#         if mask is not None and np.any(mask):
#             if old_classes_dict and to_class is not None:
#                 indices = np.array(sorted(old_classes_dict.keys()), dtype=np.int64)
#                 old_cls = np.array(
#                     [old_classes_dict[int(i)] for i in indices],
#                     dtype=self.app.data["classification"].dtype,
#                 )
#                 new_cls = np.full(len(indices), to_class, dtype=old_cls.dtype)

#                 undo_mask = np.zeros(len(self.app.data["xyz"]), dtype=bool)
#                 undo_mask[indices] = True

#                 self.app.undo_stack.append({
#                     "indices": indices.copy(),
#                     "old_classes": old_cls,
#                     "new_classes": new_cls,
#                 })
#                 self.app.redo_stack.clear()
#                 self.app._last_changed_mask = undo_mask
#                 self.app._last_changed_indices = indices.copy()

#                 try:
#                     from gui.memory_manager import current_recommended_undo_steps
#                     max_steps = int(current_recommended_undo_steps(self.app))
#                 except Exception:
#                     max_steps = int(getattr(self.app, "_max_undo_steps", 30))
#                 while len(self.app.undo_stack) > max_steps:
#                     from gui.memory_manager import _free_undo_entry
#                     _free_undo_entry(self.app.undo_stack.pop(0))

#                 try:
#                     from gui.point_count_widget import refresh_point_statistics
#                     refresh_point_statistics(self.app)
#                 except Exception:
#                     pass

#                 if hasattr(self.app, "statusBar"):
#                     self.app.statusBar().showMessage(
#                         f"✅ {len(indices):,} points → class {to_class}", 3000
#                     )

#                 # 🖌️ Final exact stroke count → top bar center
#                 try:
#                     self.app._update_classify_count_display(int(len(indices)))
#                 except Exception:
#                     pass

#             if getattr(self.app, "display_mode", "class") != "shaded_class":
#                 self.app._gpu_sync_done = True
#                 self.app._section_visibility_refresh_required = False
#                 try:
#                     from gui.unified_actor_manager import guarantee_main_view_visual_refresh
#                     guarantee_main_view_visual_refresh(
#                         self.app,
#                         changed_mask=undo_mask,
#                         to_class=to_class,
#                         reason="main_brush_finalize",
#                     )
#                 except Exception:
#                     pass
#             elif _shading_mesh_exists(self.app):
#                 try:
#                     from gui.shading_display import refresh_shaded_after_classification_fast
#                     refresh_shaded_after_classification_fast(
#                         self.app,
#                         changed_mask=mask,
#                     )
#                 except Exception as e:
#                     print(f"⚠️ Brush cleanup shading refresh: {e}")
#             else:
#                 self.app._gpu_sync_done = True
#                 self.app._section_visibility_refresh_required = False
#                 try:
#                     from gui.unified_actor_manager import guarantee_main_view_visual_refresh
#                     guarantee_main_view_visual_refresh(
#                         self.app,
#                         changed_mask=undo_mask,
#                         to_class=to_class,
#                         reason="brush_cleanup_fallback",
#                     )
#                 except Exception:
#                     pass

#             try:
#                 if hasattr(self.app, "section_vtks") and undo_mask is not None:
#                     from gui.unified_actor_manager import fast_cross_section_update, build_section_unified_actor
#                     for view_idx in self.app.section_vtks:
#                         try:
#                             slot_idx = view_idx + 1
#                             dialog = (
#                                 getattr(self.app, "display_mode_dialog", None)
#                                 or getattr(self.app, "display_dialog", None)
#                             )
#                             view_palette = {}
#                             if dialog is not None and hasattr(dialog, "view_palettes"):
#                                 view_palette = dialog.view_palettes.get(slot_idx) or {}
#                             if not view_palette and hasattr(self.app, "view_palettes"):
#                                 view_palette = self.app.view_palettes.get(slot_idx) or {}

#                             ok = fast_cross_section_update(
#                                 self.app,
#                                 view_idx,
#                                 undo_mask,
#                                 palette=view_palette,
#                                 force_visibility_refresh=False,
#                             )
#                             if not ok:
#                                 border = float(getattr(self.app, "view_borders", {}).get(slot_idx, 0) or 0.0)
#                                 view_mode = getattr(self.app, "cross_view_mode", "front")
#                                 rebuilt = build_section_unified_actor(
#                                     self.app,
#                                     view_idx,
#                                     palette=view_palette,
#                                     border_percent=border,
#                                     view=view_mode,
#                                 )
#                                 if rebuilt is not None:
#                                     fast_cross_section_update(
#                                         self.app,
#                                         view_idx,
#                                         undo_mask,
#                                         palette=view_palette,
#                                         force_visibility_refresh=False,
#                                     )
#                         except Exception:
#                             pass

#                 mode_for_main_render = str(getattr(self.app, "display_mode", "class") or "class").lower()
#                 # Guard: do not re-render slot-0 in non-class modes after classify/undo cleanup.
#                 if mode_for_main_render in ("class", "shaded_class"):
#                     self._safe_render_pyvista(self.app.vtk_widget)
#                 if hasattr(self.app, "section_vtks"):
#                     for vtk_widget in self.app.section_vtks.values():
#                         if vtk_widget:
#                             self._safe_render_pyvista(vtk_widget)
#             except Exception:
#                 pass

#             try:
#                 if hasattr(self.app, "cut_section_controller"):
#                     ctrl = self.app.cut_section_controller
#                     if (
#                         ctrl.is_cut_view_active
#                         and ctrl.cut_points is not None
#                         and ctrl._cut_index_map is not None
#                         and ctrl.cut_vtk is not None
#                     ):
#                         ctrl._refresh_cut_colors_fast()
#             except Exception as e:
#                 print(f"⚠️ Brush cleanup cut refresh failed: {e}")

#         self._brush_accumulated_mask = None
#         self._brush_old_classes = {}
#         self._brush_frame_chunks = []
#         self._brush_stroke_positions = []
#         self._last_brush_center = None
#         self._brush_needs_render = False
#         self._brush_visible_classes = None
#         self.app._suppress_section_refresh = False
#         self.is_dragging = False

#         return undo_mask

#     # ═══════════════════════════════════════════════════════════════════════
#     # ✅ SAFE RENDER GUARDS - Prevents crash on stale VTK render windows
#     # Root cause: switching between cut section ↔ synchronized views leaves
#     # render windows in an invalid state. Unguarded Render() dereferences
#     # near-null pointer (0x24) → win32 memory access violation → hard crash.
#     # ═══════════════════════════════════════════════════════════════════════

#     @staticmethod
#     def _is_vtk_widget_alive(vtk_widget):
#         """Check if a VTK widget is still alive and renderable."""
#         if vtk_widget is None:
#             return False
#         try:
#             # Qt widget destroyed?
#             if hasattr(vtk_widget, 'isVisible') and callable(vtk_widget.isVisible):
#                 if not vtk_widget.isVisible():
#                     return False
#             # Render window accessible?
#             rw = vtk_widget.GetRenderWindow()
#             if rw is None:
#                 return False
#             # Interactor alive? (None if window finalized)
#             if rw.GetInteractor() is None:
#                 return False
#             # Has at least one renderer?
#             renderers = rw.GetRenderers()
#             if renderers is None or renderers.GetNumberOfItems() == 0:
#                 return False
#             return True
#         except (RuntimeError, AttributeError, OSError, ReferenceError):
#             return False
#         except Exception:
#             return False

#     @staticmethod
#     def _safe_render(vtk_widget):
#         """
#         Safely render a VTK widget. Returns True on success, False otherwise.
#         NEVER raises — all exceptions are caught to prevent crash.
#         """
#         if vtk_widget is None:
#             return False
#         try:
#             # Inline validity checks (avoid second method call for speed)
#             rw = vtk_widget.GetRenderWindow()
#             if rw is None:
#                 return False
#             if rw.GetInteractor() is None:
#                 return False
#             renderers = rw.GetRenderers()
#             if renderers is None or renderers.GetNumberOfItems() == 0:
#                 return False
#             rw.Render()
#             return True
#         except (RuntimeError, AttributeError, OSError, ReferenceError):
#             return False
#         except Exception:
#             return False

#     @staticmethod
#     def _safe_render_pyvista(vtk_widget):
#         """Safely call .render() on a PyVista widget."""
#         if vtk_widget is None:
#             return False
#         try:
#             if hasattr(vtk_widget, 'isVisible') and callable(vtk_widget.isVisible):
#                 if not vtk_widget.isVisible():
#                     return False
#             if hasattr(vtk_widget, "testAttribute"):
#                 try:
#                     if not vtk_widget.testAttribute(Qt.WA_WState_Created):
#                         return False
#                 except Exception:
#                     pass
#             if hasattr(vtk_widget, "windowHandle"):
#                 try:
#                     win = vtk_widget.windowHandle()
#                     # Child widgets may legitimately return None here; don't
#                     # block rendering in that case.
#                     if win is not None and hasattr(win, "isExposed") and not win.isExposed():
#                         return False
#                 except Exception:
#                     pass
#             rw = vtk_widget.GetRenderWindow()
#             if rw is None or rw.GetInteractor() is None:
#                 return False
#             vtk_widget.render()
#             return True
#         except (RuntimeError, AttributeError, OSError, ReferenceError):
#             return False
#         except Exception:
#             return False

#     def _safe_render_interactor(self):
#         """Safely render via self.interactor's render window."""
#         try:
#             if self.interactor is None:
#                 return False
#             rw = self.interactor.GetRenderWindow()
#             if rw is None or rw.GetInteractor() is None:
#                 return False
#             renderers = rw.GetRenderers()
#             if renderers is None or renderers.GetNumberOfItems() == 0:
#                 return False
#             rw.Render()
#             return True
#         except (RuntimeError, AttributeError, OSError, ReferenceError):
#             return False
#         except Exception:
#             return False

#     @staticmethod
#     def _safe_render_from_renderer(renderer):
#         """Safely render from a VTK renderer reference."""
#         if renderer is None:
#             return False
#         try:
#             rw = renderer.GetRenderWindow()
#             if rw is None or rw.GetInteractor() is None:
#                 return False
#             rw.Render()
#             return True
#         except (RuntimeError, AttributeError, OSError, ReferenceError):
#             return False
#         except Exception:
#             return False

#     @staticmethod
#     def _safe_get_render_window(vtk_widget):
#         """Safely get render window from a VTK widget. Returns None if dead."""
#         if vtk_widget is None:
#             return None
#         try:
#             rw = vtk_widget.GetRenderWindow()
#             if rw is None or rw.GetInteractor() is None:
#                 return None
#             return rw
#         except (RuntimeError, AttributeError, OSError, ReferenceError):
#             return None
#         except Exception:
#             return None

#     # ═══════════════════════════════════════════════════════════════════════
#     # ✅ CUSTOM SAFE PAN - Replaces VTK's built-in pan which crashes
#     # VTK's OnMiddleButtonDown/Up → OnMouseMove internally calls C++ Render()
#     # which segfaults on stale OpenGL contexts. This custom pan does camera
#     # math in Python and uses Qt deferred repaint — zero C++ Render() calls.
#     # ═══════════════════════════════════════════════════════════════════════

#     def _on_scroll_zoom_in(self, obj, event):
#         """Scroll-wheel zoom in — replaces VTK's Image2D 'Slice' behaviour."""
#         try:
#             if hasattr(obj, "AbortFlagOn"):
#                 obj.AbortFlagOn()
#             elif hasattr(obj, "SetAbortFlag"):
#                 obj.SetAbortFlag(1)
#         except Exception:
#             pass
#         self._apply_scroll_zoom(1.1)

#     def _on_scroll_zoom_out(self, obj, event):
#         """Scroll-wheel zoom out — replaces VTK's Image2D 'Slice' behaviour."""
#         try:
#             if hasattr(obj, "AbortFlagOn"):
#                 obj.AbortFlagOn()
#             elif hasattr(obj, "SetAbortFlag"):
#                 obj.SetAbortFlag(1)
#         except Exception:
#             pass
#         self._apply_scroll_zoom(1.0 / 1.1)

#     def _apply_scroll_zoom(self, factor):
#         """
#         Scale the camera's ParallelScale (orthographic zoom) by `factor`.
#         Works for any active cross-section / cut-section widget without touching
#         camera orientation, so the 2D lock is never disturbed.
#         """
#         try:
#             vtk_widget = self._get_active_vtk_widget()
#             if vtk_widget is None:
#                 return
#             renderer = getattr(vtk_widget, "renderer", None)
#             if renderer is None:
#                 return
#             camera = renderer.GetActiveCamera()
#             if camera is None:
#                 return

#             if camera.GetParallelProjection():
#                 camera.SetParallelScale(camera.GetParallelScale() / factor)
#             else:
#                 camera.Dolly(factor)
#                 renderer.ResetCameraClippingRange()

#             # Deferred render — same pattern as pan to avoid C++ Render() calls.
#             if not getattr(self, "_pan_render_pending", False):
#                 self._pan_render_pending = True
#                 from PySide6.QtCore import QTimer
#                 QTimer.singleShot(0, self._execute_deferred_pan_render)
#         except (RuntimeError, AttributeError, OSError, ReferenceError):
#             pass
#         except Exception:
#             pass

#     def _on_safe_pan_start(self, obj, event):
#         """Start custom pan (replaces style.OnMiddleButtonDown)."""
#         try:
#             if hasattr(obj, "AbortFlagOn"):
#                 obj.AbortFlagOn()
#             elif hasattr(obj, "SetAbortFlag"):
#                 obj.SetAbortFlag(1)
#         except Exception:
#             pass
#         try:
#             if self.interactor is None:
#                 return
            
#             # ═══════════════════════════════════════════════════════════════
#             # ✅ CRITICAL: Verify middle button is actually pressed via Qt
#             # VTK sends phantom MiddleButtonPressEvent after classification
#             # ═══════════════════════════════════════════════════════════════
#             try:
#                 from PySide6.QtWidgets import QApplication
#                 from PySide6.QtCore import Qt
#                 buttons = QApplication.mouseButtons()
#                 is_valid_press = bool(buttons & Qt.MiddleButton)
#                 if not is_valid_press:
#                     # Phantom event - ignore completely
#                     return
#             except Exception:
#                 pass
            
#             self._is_panning = True
#             self._last_pan_pos = self.interactor.GetEventPosition()

#             # Interactive-mode rendering: skip expensive still-render steps
#             try:
#                 rw = self.interactor.GetRenderWindow()
#                 if rw is not None:
#                     rw.SetDesiredUpdateRate(30.0)
#             except Exception:
#                 pass

#             tool = getattr(self.app, "active_classify_tool", None)
#             has_preview_gesture = bool(
#                 self._has_active_drag_classification(tool)
#                 or getattr(self, "P1", None) is not None
#                 or bool(getattr(self, "drawing_points", []))
#                 or bool(getattr(self, "is_drawing_freehand", False))
#             )
#             if tool in {"rectangle", "circle", "freehand", "above_line", "below_line", "parallel_line", "polygon", "brush"} and has_preview_gesture:
#                 # Keep the live preview actors active and translate them with the pan,
#                 # otherwise the preview appears frozen while the cloud moves.
#                 self._hide_preview_overlay_for_pan()
#                 self._start_deferred_left_release_watch()
            
#         except (RuntimeError, AttributeError):
#             pass

#     def _on_safe_pan_stop(self, obj, event):
#         """Stop custom pan (replaces style.OnMiddleButtonUp)."""
#         try:
#             if hasattr(obj, "AbortFlagOn"):
#                 obj.AbortFlagOn()
#             elif hasattr(obj, "SetAbortFlag"):
#                 obj.SetAbortFlag(1)
#         except Exception:
#             pass
#         self._is_panning = False
#         # Let the next post-pan mouse move update the preview immediately.
#         self._last_mouse_move_time = 0.0
#         self._last_render_time = 0.0
#         self._hide_preview_overlay_for_pan()
#         try:
#             self._start_deferred_left_release_watch()
#         except (AttributeError, RuntimeError):
#             return

#         # Restore still-render quality and force one full-quality frame
#         try:
#             rw = self.interactor.GetRenderWindow() if self.interactor else None
#             if rw is not None:
#                 rw.SetDesiredUpdateRate(0.001)
#                 rw.Render()
#         except Exception:
#             pass

#     def _is_classification_paused_for_pan(self, tool=None):
#         """
#         Return True while a drag-style classification gesture should be frozen
#         so middle-button pan can run without changing the active selection.
#         """
#         if not getattr(self, "_is_panning", False):
#             return False
#         return self._has_active_drag_classification(tool)

#     def _do_safe_pan(self):
#         """
#         Execute pan: move camera by mouse delta.
#         ✅ CRITICAL FIX: Verify middle button is ACTUALLY pressed before panning.
#         This prevents phantom pan from corrupted VTK interactor state.
#         """
#         # ═══════════════════════════════════════════════════════════════════
#         # ✅ CRITICAL: Check Qt button state, not just our flag
#         # VTK can send phantom MiddleButtonPressEvent after classification
#         # tools complete, setting _is_panning=True incorrectly.
#         # ═══════════════════════════════════════════════════════════════════
#         try:
#             from PySide6.QtWidgets import QApplication
#             from PySide6.QtCore import Qt
#             buttons = QApplication.mouseButtons()
#             middle_actually_pressed = bool(buttons & Qt.MiddleButton)
            
#             if not middle_actually_pressed:
#                 # Middle button not pressed - reset our flag if it was set
#                 if self._is_panning:
#                     self._is_panning = False
#                 return
#         except Exception:
#             # Fallback: use our flag only
#             if not getattr(self, '_is_panning', False):
#                 return
        
#         if not getattr(self, '_is_panning', False):
#             return
        
#         try:
#             if self.interactor is None:
#                 return
            
#             current_pos = self.interactor.GetEventPosition()
#             last = getattr(self, '_last_pan_pos', current_pos)
            
#             dx = current_pos[0] - last[0]
#             dy = current_pos[1] - last[1]
            
#             if dx == 0 and dy == 0:
#                 return
            
#             vtk_widget = self._get_active_vtk_widget()
#             if vtk_widget is None:
#                 return
            
#             renderer = getattr(vtk_widget, 'renderer', None)
#             if renderer is None:
#                 return
            
#             camera = renderer.GetActiveCamera()
#             if camera is None:
#                 return
            
#             # Get window size for pixel-to-world conversion
#             try:
#                 rw = vtk_widget.GetRenderWindow()
#                 size = rw.GetSize() if rw is not None else (800, 600)
#             except (RuntimeError, AttributeError):
#                 size = (800, 600)
            
#             w = max(size[0], 1)
#             h = max(size[1], 1)
            
#             # Use ParallelScale for orthographic views
#             if camera.GetParallelProjection():
#                 parallel_scale = camera.GetParallelScale()
#                 scale_x = (2.0 * parallel_scale) / h
#                 scale_y = scale_x
#             else:
#                 distance = camera.GetDistance()
#                 scale_x = distance / (w * 0.5)
#                 scale_y = scale_x
            
#             # Camera axes
#             camera.OrthogonalizeViewUp()
#             up = list(camera.GetViewUp())
#             pos = list(camera.GetPosition())
#             fp = list(camera.GetFocalPoint())
            
#             vd = [fp[i] - pos[i] for i in range(3)]
            
#             # Right vector = vd × up
#             right = [
#                 vd[1] * up[2] - vd[2] * up[1],
#                 vd[2] * up[0] - vd[0] * up[2],
#                 vd[0] * up[1] - vd[1] * up[0]
#             ]
#             mag = (right[0]**2 + right[1]**2 + right[2]**2) ** 0.5
#             if mag < 1e-12:
#                 self._last_pan_pos = current_pos
#                 return
#             right = [r / mag for r in right]
            
#             # Pan vector
#             pan = [-dx * scale_x * right[i] - dy * scale_y * up[i] for i in range(3)]
            
#             camera.SetPosition(*[pos[i] + pan[i] for i in range(3)])
#             camera.SetFocalPoint(*[fp[i] + pan[i] for i in range(3)])
            
#             self._last_pan_pos = current_pos

#             tool = getattr(self, "_gesture_tool", None) or getattr(self.app, "active_classify_tool", None)
#             if self._has_active_drag_classification(tool):
#                 if tool == "freehand" and hasattr(self, "drawing_points_display_cut"):
#                     self.drawing_points_display_cut = [
#                         (float(px) + dx, float(py) + dy)
#                         for px, py in self.drawing_points_display_cut
#                     ]
#                 elif tool == "rectangle" and getattr(self, "P1_display_cut", None) is not None:
#                     self.P1_display_cut = (
#                         float(self.P1_display_cut[0]) + dx,
#                         float(self.P1_display_cut[1]) + dy,
#                     )

#                 self._translate_preview_actors_for_pan(
#                     dx,
#                     dy,
#                     self._get_active_tool_preview_actor_names(tool),
#                 )
            
#             # Deferred render
#             if not getattr(self, '_pan_render_pending', False):
#                 self._pan_render_pending = True
#                 from PySide6.QtCore import QTimer
#                 QTimer.singleShot(0, self._execute_deferred_pan_render)
            
#         except (RuntimeError, AttributeError, OSError, ReferenceError):
#             self._is_panning = False
#         except Exception:
#             pass

#     def _execute_deferred_pan_render(self):
#         """Render on the Qt event loop (valid OpenGL context). Called by QTimer."""
#         self._pan_render_pending = False
#         try:
#             import time as _time
#             now = _time.perf_counter()
#             # Cap interactive pan renders to 60 fps (16 ms). If the last render
#             # finished too recently, re-schedule rather than saturating the GPU.
#             if now - getattr(self, '_last_pan_render_time', 0.0) < 0.016:
#                 if not getattr(self, '_pan_render_pending', False):
#                     self._pan_render_pending = True
#                     QTimer.singleShot(8, self._execute_deferred_pan_render)
#                 return
#             self._last_pan_render_time = now

#             vtk_widget = self._get_active_vtk_widget()
#             if vtk_widget is not None:
#                 # ✅ Check widget is visible (Qt-level, safe)
#                 if hasattr(vtk_widget, 'isVisible') and not vtk_widget.isVisible():
#                     return
#                 vtk_widget.render()
#                 overlay = getattr(self, "_preview_overlay", None)
#                 if overlay is not None and getattr(self, "_preview_hidden_for_pan", False):
#                     try:
#                         self._update_overlay_geometry()
#                         overlay.show()
#                         overlay.raise_()
#                         overlay.update()
#                     except Exception:
#                         pass
#         except (RuntimeError, AttributeError, OSError, ReferenceError):
#             pass
#         except Exception:
#             pass

#     # Added by bala for mouse smooth
#     def _get_display_point(self):
#         """Exact mouse position in DISPLAY coords (no snapping, very fast)."""
#         x, y = self.interactor.GetEventPosition()
#         return float(x), float(y)

#     def _display_to_world_fast(self, x, y):
#         """DISPLAY → WORLD without picker (no snapping). ✅ SAFE: guarded render window access."""
#         if not hasattr(self, "_cached_renderer") or self._cached_renderer is None:
#             try:
#                 rw = self.interactor.GetRenderWindow()
#                 if rw is None:
#                     return (0.0, 0.0, 0.0)
#                 renderers = rw.GetRenderers()
#                 if renderers is None:
#                     return (0.0, 0.0, 0.0)
#                 self._cached_renderer = renderers.GetFirstRenderer()
#             except (RuntimeError, AttributeError, OSError, ReferenceError):
#                 return (0.0, 0.0, 0.0)
#             except Exception:
#                 return (0.0, 0.0, 0.0)

#         if self._cached_renderer is None:
#             return (0.0, 0.0, 0.0)

#         coord = vtk.vtkCoordinate()
#         coord.SetCoordinateSystemToDisplay()
#         coord.SetValue(x, y, 0)
#         try:
#             return coord.GetComputedWorldValue(self._cached_renderer)
#         except Exception:
#             return (0.0, 0.0, 0.0)

#     def _is_main_view(self):
#         """True if this interactor is attached to the main plan view widget."""
#         try:
#             return self._get_active_vtk_widget() == getattr(self.app, 'vtk_widget', None)
#         except Exception:
#             return False

#     def _view2d_to_world(self, u, v):
#         """Map 2D tool coords to 3D world - OPTIMIZED with caching"""
        
#         if self._is_main_view():
#             return (float(u), float(v), 0.0)
        
#         view_mode = getattr(self.app, 'cross_view_mode', 'side')
        
#         # ✅ CRITICAL OPTIMIZATION: Cache perpendicular coordinate
#         # Only recalculate when view changes, not on every mouse move
#         cache_key = f"perp_coord_{view_mode}"
        
#         if not hasattr(self, '_coord_cache'):
#             self._coord_cache = {}
        
#         if cache_key not in self._coord_cache:
#             # Calculate once and cache
#             active_view = self._get_view_index_from_interactor()
#             perpendicular_coord = 0.0
            
#             if active_view is not None:
#                 core_mask = getattr(self.app, f"section_{active_view}_core_mask", None)
#                 if core_mask is not None and core_mask.sum() > 0:
#                     section_xyz = self.app.data["xyz"][core_mask]
                    
#                     if view_mode == 'front':
#                         perpendicular_coord = float(np.median(section_xyz[:, 0]))
#                     else:
#                         perpendicular_coord = float(np.median(section_xyz[:, 1]))
            
#             self._coord_cache[cache_key] = perpendicular_coord
        
#         perpendicular_coord = self._coord_cache[cache_key]
        
#         if view_mode == 'front':
#             return (float(perpendicular_coord), float(u), float(v))
#         else:
#             return (float(u), float(perpendicular_coord), float(v)) 
        
#     def _invalidate_coord_cache(self):
#         """Clear coordinate cache when section changes"""
#         if hasattr(self, '_coord_cache'):
#             self._coord_cache.clear()
        
#     def _get_view_coordinates(self, pt):
#         """
#         2D coordinates in the active view's plane.
#         - Main view: (x, y)
#         - Cross-section: (u, z) [side: XZ, front: YZ]
#         - Cut section: (u, z) [projected to cut plane]
#         """
#         if self._is_main_view():
#             return (pt[0], pt[1])
        
#         # ✅ FIX: Check if we're in cut section
#         vtk_widget = self._get_active_vtk_widget()
#         is_cut_section = (
#             hasattr(self.app, 'cut_section_controller') and
#             vtk_widget == getattr(self.app.cut_section_controller, 'cut_vtk', None)
#         )
        
#         if is_cut_section:
#             # ✅ CRITICAL FIX: Project to cut section coordinates
#             from ..classification_tools import _project_to_cut_view
#             pt_array = np.array([pt])
#             projected = _project_to_cut_view(self.app, pt_array)
#             return (projected[0, 0], projected[0, 1])  # Return (u, z)
        
#         # Regular cross-section
#         view_mode = getattr(self.app, 'cross_view_mode', 'side')
#         if view_mode == 'front':
#             return (pt[1], pt[2])  # Y-Z
#         else:
#             return (pt[0], pt[2])  # X-Z
 
#     def _get_active_vtk_widget(self):
#         """
#         Get the correct VTK widget by matching the current interactor.
#         ✅ SAFE: All GetRenderWindow() calls guarded to prevent crash on stale windows.
#         """
#         if self.interactor is None:
#             return None
       
#         # 1. Check Cut Section
#         if hasattr(self.app, 'cut_section_controller'):
#             cut_vtk = getattr(self.app.cut_section_controller, 'cut_vtk', None)
#             if cut_vtk:
#                 if getattr(cut_vtk, 'interactor', None) == self.interactor:
#                     return cut_vtk
#                 rw = self._safe_get_render_window(cut_vtk)
#                 if rw is not None:
#                     try:
#                         if rw.GetInteractor() == self.interactor:
#                             return cut_vtk
#                     except Exception:
#                         pass
 
#         # 2. Check Section Docks (SAFE)
#         section_vtks = getattr(self.app, 'section_vtks', None)
#         if isinstance(section_vtks, dict):
#             for widget in section_vtks.values():
#                 if widget:
#                     if getattr(widget, 'interactor', None) == self.interactor:
#                         return widget
#                     rw = self._safe_get_render_window(widget)
#                     if rw is not None:
#                         try:
#                             if rw.GetInteractor() == self.interactor:
#                                 return widget
#                         except Exception:
#                             pass

 
#         # 3. Check Main View
#         main_widget = getattr(self.app, 'vtk_widget', None)
#         if main_widget:
#             if getattr(main_widget, 'interactor', None) == self.interactor:
#                 return main_widget
#             rw = self._safe_get_render_window(main_widget)
#             if rw is not None:
#                 try:
#                     if rw.GetInteractor() == self.interactor:
#                         return main_widget
#                 except Exception:
#                     pass
 
#         # 4. FALLBACK: Match by RenderWindow
#         try:
#             current_rw = self.interactor.GetRenderWindow()
#             if current_rw is None:
#                 return None
           
#             if hasattr(self.app, 'cut_section_controller'):
#                 cut_vtk = getattr(self.app.cut_section_controller, 'cut_vtk', None)
#                 if cut_vtk:
#                     rw = self._safe_get_render_window(cut_vtk)
#                     if rw is not None and rw == current_rw:
#                         return cut_vtk
                   
#             if hasattr(self.app, 'section_vtks'):
#                 for widget in self.app.section_vtks.values():
#                     if widget:
#                         rw = self._safe_get_render_window(widget)
#                         if rw is not None and rw == current_rw:
#                             return widget
#         except Exception:
#             pass                  
#         return None
   
#     def _get_view_index_from_interactor(self):
#         """Return cross-section view index (0..3) for this interactor, or None."""
#         try:
#             if hasattr(self.app, "section_vtks"):
#                 for idx, vtk_widget in self.app.section_vtks.items():
#                     if getattr(vtk_widget, "interactor", None) == self.interactor:
#                         return idx
#                     rw = vtk_widget.GetRenderWindow() if hasattr(vtk_widget, "GetRenderWindow") else None
#                     if rw and rw.GetInteractor() == self.interactor:
#                         return idx
#         except Exception:
#             pass
#         return None
 
#     def _get_section_context_for_view(self, view_idx):
#         """
#         Build (mask, section_points) for the requested view index from app's stored per-view data.
        
#         Returns:
#             mask: boolean array over full dataset (includes core + buffer points)
#             section_points: transformed 2D coordinates for the section view OR 3D xyz[mask]
        
#         ✅ UPDATED LOGIC:
#             1. First tries to use pre-computed combined_mask + transformed points (fastest)
#             2. Falls back to building combined mask from core + buffer
#             3. Always prefers transformed points over raw xyz
#             4. Provides detailed debug logging
        
#         The transformed points are in the 2D coordinate system of the cross-section view:
#             - For side view: (x, z) coordinates
#             - For front view: (y, z) coordinates
#         """
#         try:
#             app = self.app
            
#             # ═══════════════════════════════════════════════════════════
#             # PRIORITY PATH 1: Use pre-computed combined_mask + transformed points
#             # ═══════════════════════════════════════════════════════════
#             # This is the fastest and most accurate path when available
#             combined_mask = getattr(app, f"section_{view_idx}_combined_mask", None)
#             section_points_transformed = getattr(app, f"section_{view_idx}_points_transformed", None)
            
#             if combined_mask is not None and section_points_transformed is not None:
#                 return combined_mask, section_points_transformed

#             core_mask   = getattr(app, f"section_{view_idx}_core_mask",   None)
#             buffer_mask = getattr(app, f"section_{view_idx}_buffer_mask", None)

#             if core_mask is None and buffer_mask is None:
#                 return None, None

#             if core_mask is None:
#                 combined_mask = buffer_mask
#             elif buffer_mask is None:
#                 combined_mask = core_mask
#             else:
#                 combined_mask = core_mask | buffer_mask

#             section_points_transformed = getattr(app, f"section_{view_idx}_points_transformed", None)
#             if section_points_transformed is not None:
#                 return combined_mask, section_points_transformed

#             section_points = app.data["xyz"][combined_mask]
#             return combined_mask, section_points

#         except Exception as e:
#             import traceback
#             traceback.print_exc()
#             return None, None
    
#     def _get_view_index_from_interactor(self):
#         """Return cross-section view index (0..3) for this interactor, or None."""
#         try:
#             if hasattr(self.app, "section_vtks"):
#                 for idx, vtk_widget in self.app.section_vtks.items():
#                     if getattr(vtk_widget, "interactor", None) == self.interactor:
#                         return idx
#                     rw = vtk_widget.GetRenderWindow() if hasattr(vtk_widget, "GetRenderWindow") else None
#                     if rw and rw.GetInteractor() == self.interactor:
#                         return idx
#         except Exception:
#             pass
#         return None

#     def _navigate_main_to_section_click(self, display_x, display_y):
#         """
#         MicroStation-style locate: click in section view while cross-section tool is
#         active → pan main view to the corresponding world XY AND start a rubber-band
#         preview line from that locked point.

#         Section local coords: X = along_c (distance along line from P1), Z = elevation.
#         Camera looks along Y (across_c), so _display_to_world_fast gives (along, ~0, Z).
#         Inverse: world_xy = sectionP1[:2] + along * dir_vec
#         """
#         try:
#             import numpy as np

#             v_idx = self._get_view_index_from_interactor()
#             if v_idx is None:
#                 sc = getattr(self.app, 'section_controller', None)
#                 v_idx = getattr(sc, 'active_view', 0) if sc else 0

#             sP1 = getattr(self.app, f'section_{v_idx}_P1', None)
#             sP2 = getattr(self.app, f'section_{v_idx}_P2', None)
#             if sP1 is None or sP2 is None:
#                 return

#             # Display → section-local (along, across≈0, Z)
#             local_pt = self._display_to_world_fast(display_x, display_y)
#             along = float(local_pt[0])

#             # Inverse transform → real world XY
#             v = np.asarray(sP2[:2], dtype=np.float64) - np.asarray(sP1[:2], dtype=np.float64)
#             length = float(np.linalg.norm(v))
#             if length < 1e-9:
#                 return
#             dir_vec = v / length
#             world_x = float(sP1[0]) + along * dir_vec[0]
#             world_y = float(sP1[1]) + along * dir_vec[1]

#             # ── Store locate state for rubber-band drawing in section view ──
#             self.app._section_locate_display = (float(display_x), float(display_y))
#             self.app._section_locate_view = v_idx

#             # ── Arm cross_interactor: set P1 so main-view preview starts from here ──
#             cross = getattr(self.app, 'cross_interactor', None)
#             if cross is not None:
#                 main_vtk = getattr(self.app, 'vtk_widget', None)
#                 if main_vtk is not None:
#                     # Use current main camera focal Z so the section line stays flat
#                     focal_z = main_vtk.renderer.GetActiveCamera().GetFocalPoint()[2]
#                     cross.P1 = np.array([world_x, world_y, focal_z], dtype=np.float64)
#                     cross.slice_state = 1

#             # ── Pan main view camera to (world_x, world_y) ──
#             main_vtk = getattr(self.app, 'vtk_widget', None)
#             if main_vtk is not None:
#                 cam = main_vtk.renderer.GetActiveCamera()
#                 focal = cam.GetFocalPoint()
#                 pos = cam.GetPosition()
#                 dx_pan = world_x - focal[0]
#                 dy_pan = world_y - focal[1]
#                 cam.SetFocalPoint(world_x, world_y, focal[2])
#                 cam.SetPosition(pos[0] + dx_pan, pos[1] + dy_pan, pos[2])
#                 main_vtk.renderer.ResetCameraClippingRange()
#                 main_vtk.render()

#             if hasattr(self.app, 'statusBar'):
#                 self.app.statusBar().showMessage(
#                     f"Locked ({world_x:.1f}, {world_y:.1f})  —  "
#                     "move to main view and draw cross-section",
#                     5000
#                 )
#         except Exception as e:
#             print(f"⚠️ _navigate_main_to_section_click: {e}")

#     def _draw_locate_line_in_section(self, vtk_widget, from_disp, to_disp):
#         """
#         Draw a rubber-band preview line in the section view from the locked click
#         position to the current cursor position (both in VTK display coords).
#         Uses the same color as the cross-section preview line (cross_line_color).
#         """
#         if vtk_widget is None:
#             return
#         ren = vtk_widget.renderer

#         # Lazy-init the dedicated 2D actor (separate from classification line_actor)
#         if not hasattr(self, '_locate_line_actor') or self._locate_line_actor is None:
#             pts = vtk.vtkPoints()
#             pts.SetNumberOfPoints(2)
#             lines = vtk.vtkCellArray()
#             lines.InsertNextCell(2)
#             lines.InsertCellPoint(0)
#             lines.InsertCellPoint(1)
#             poly = vtk.vtkPolyData()
#             poly.SetPoints(pts)
#             poly.SetLines(lines)

#             coord = vtk.vtkCoordinate()
#             coord.SetCoordinateSystemToDisplay()
#             mapper = vtk.vtkPolyDataMapper2D()
#             mapper.SetInputData(poly)
#             mapper.SetTransformCoordinate(coord)

#             actor = vtk.vtkActor2D()
#             actor.SetMapper(mapper)

#             self._locate_line_pts = pts
#             self._locate_line_poly = poly
#             self._locate_line_actor = actor

#         actor = self._locate_line_actor
#         if not ren.HasViewProp(actor):
#             ren.AddActor2D(actor)
#         actor.VisibilityOn()

#         color = getattr(self.app, 'cross_line_color', (1.0, 0.0, 1.0))
#         width = getattr(self.app, 'cross_line_width', 2)
#         prop = actor.GetProperty()
#         prop.SetColor(*color)
#         prop.SetLineWidth(max(2, width))
#         prop.SetOpacity(1.0)

#         self._locate_line_pts.SetPoint(0, from_disp[0], from_disp[1], 0.0)
#         self._locate_line_pts.SetPoint(1, to_disp[0], to_disp[1], 0.0)
#         self._locate_line_pts.Modified()
#         self._locate_line_poly.Modified()

#     def _clear_locate_state(self):
#         """Remove the section-view rubber-band actor and clear the locate state."""
#         try:
#             actor = getattr(self, '_locate_line_actor', None)
#             if actor is not None:
#                 actor.VisibilityOff()
#                 # Remove from ALL section view renderers to prevent ghost lines
#                 for vtk_widget in getattr(self.app, 'section_vtks', {}).values():
#                     try:
#                         if hasattr(vtk_widget, 'renderer') and vtk_widget.renderer.HasViewProp(actor):
#                             vtk_widget.renderer.RemoveActor2D(actor)
#                     except Exception:
#                         pass
#                 # Also remove from cut dock if present
#                 try:
#                     cut_vtk = getattr(getattr(self.app, 'cut_section_controller', None), 'cut_vtk', None)
#                     if cut_vtk is not None and hasattr(cut_vtk, 'renderer') and cut_vtk.renderer.HasViewProp(actor):
#                         cut_vtk.renderer.RemoveActor2D(actor)
#                 except Exception:
#                     pass
#         except Exception:
#             pass
#         self.app._section_locate_display = None
#         self.app._section_locate_view = None

#     #     """
#     #     mask: boolean array over full dataset
#     #     section_points: xyz[mask]
#     #     """




#     def _init_preview_overlay(self):
#         """Initialize Qt-based preview overlay"""
#         try:
#             # Get the parent widget for the overlay
#             parent_widget = self.interactor
            
#             # Create overlay
#             self._preview_overlay = PreviewOverlay(parent_widget)
#             self._preview_overlay.setGeometry(parent_widget.rect())
#             self._preview_overlay.show()
#             self._preview_overlay.raise_()
            
#             # Cache renderer and widget height for coordinate conversion
#             self._cached_renderer = self.interactor.GetRenderWindow().GetRenderers().GetFirstRenderer()
#             self._widget_height = parent_widget.height()
            
#             print("✅ Qt preview overlay initialized")
            
#         except Exception as e:
#             print(f"⚠️ Failed to create Qt preview overlay: {e}")
#             self._preview_overlay = None
    
#     def _update_overlay_geometry(self):
#         """Update overlay size to match interactor widget"""
#         if self._preview_overlay is None:
#             return
            
#         try:
#             self._preview_overlay.setGeometry(self.interactor.rect())
#             self._widget_height = self.interactor.height()
#         except Exception:
#             pass
    
#     def _should_update(self):
#         """Throttle updates to maintain smooth performance"""
#         current_time = time.time() * 1000  # Convert to milliseconds
#         self._update_interval = self._adaptive_preview_interval_ms()
#         if current_time - self._last_update_time >= self._update_interval:
#             self._last_update_time = current_time
#             return True
#         return False

#     def _adaptive_preview_interval_ms(self) -> int:
#         """
#         Adaptive preview cadence:
#         - default ~60 FPS (16 ms)
#         - medium pressure ~45 FPS (22 ms)
#         - high pressure ~30 FPS (33 ms)

#         Note: this is the conservative main-view cap. For cross-section / cut
#         docks, _preview_interval_sec() uses a fixed 60 FPS (16 ms) regardless
#         of the global point count, because those docks render at most ~100 K
#         points and can sustain 60 FPS comfortably. See _preview_interval_sec.
#         """
#         try:
#             mem = getattr(self.app, "_mem_guard", None)
#             stats = mem.get_stats() if mem is not None else {}
#             ram_pct = float(stats.get("ram_pct", 0.0) or 0.0)
#         except Exception:
#             ram_pct = 0.0

#         try:
#             n_points = len(getattr(self.app, "data", {}).get("xyz", []))
#         except Exception:
#             n_points = 0

#         if ram_pct >= 9.0 or n_points >= 12_000_000:
#             return 33
#         if ram_pct >= 7.5 or n_points >= 6_000_000:
#             return 22
#         return 16

#     def _preview_interval_sec(self) -> float:
#         """
#         Return preview-frame throttle interval in seconds.

#         ⚡ View-aware: when the user is dragging inside a cross-section / cut
#         dock, the render only touches a small section actor (≤ ~100 K pts),
#         not the 39 M-point main view. Capping at 30 FPS in that case was the
#         root cause of "preview feels heavy" — visible cursor lag during a
#         line/rect/circle drag. We force 60 FPS (16 ms) for section/cut docks
#         and keep the adaptive cap only for the main view.
#         """
#         try:
#             vtk_widget = self._get_active_vtk_widget()
#             main = getattr(self.app, 'vtk_widget', None)
#             if vtk_widget is not None and vtk_widget is not main:
#                 # Section / cut dock — bounded geometry, 60 FPS is safe.
#                 return 0.016
#         except Exception:
#             pass
#         return float(self._adaptive_preview_interval_ms()) / 1000.0
    
#     def _world_to_screen(self, world_point):
#         """Convert world coordinates to screen coordinates - OPTIMIZED"""
#         try:
#             if self._cached_renderer is None:
#                 self._cached_renderer = self.interactor.GetRenderWindow().GetRenderers().GetFirstRenderer()
            
#             # Create coordinate converter
#             coord = vtk.vtkCoordinate()
#             coord.SetCoordinateSystemToWorld()
            
#             # Handle both 2D (u, z) and 3D (x, y, z) coordinates
#             if len(world_point) == 2:
#                 coord.SetValue(world_point[0], 0, world_point[1])
#             else:
#                 coord.SetValue(world_point[0], world_point[1], world_point[2])
            
#             # Get display coordinates
#             display = coord.GetComputedDisplayValue(self._cached_renderer)
            
#             # VTK uses bottom-left origin, Qt uses top-left
#             screen_x = int(display[0])
#             screen_y = int(self._widget_height - display[1])
            
#             return QPoint(screen_x, screen_y)
            
#         except Exception as e:
#             return QPoint(0, 0)
    
#     def _world_radius_to_screen(self, center_world, radius_world):
#         """Convert world radius to screen pixels"""
#         try:
#             # Get screen position of center
#             center_screen = self._world_to_screen(center_world)
            
#             # Get screen position of a point at radius distance
#             edge_world = (center_world[0] + radius_world, center_world[1])
#             edge_screen = self._world_to_screen(edge_world)
            
#             # Calculate pixel radius
#             pixel_radius = abs(edge_screen.x() - center_screen.x())
#             return max(5, pixel_radius)  # Minimum 5 pixels
            
#         except Exception:
#             return 20  # Default radius

#     def _display_to_world_same_depth(self, vtk_widget, center_world, dx_px=0.0, dy_px=0.0):
#         """
#         Convert a display offset (dx_px, dy_px) around center_world into a world point
#         at the SAME display depth. This converts pixel size to world size (zoom-aware).
#         """
#         renderer = vtk_widget.renderer
#         cxw, cyw, czw = float(center_world[0]), float(center_world[1]), float(center_world[2])

#         # World -> Display to get depth (z in display space)
#         renderer.SetWorldPoint(cxw, cyw, czw, 1.0)
#         renderer.WorldToDisplay()
#         cx, cy, cz = renderer.GetDisplayPoint()

#         # Display -> World at same depth
#         renderer.SetDisplayPoint(float(cx + dx_px), float(cy + dy_px), float(cz))
#         renderer.DisplayToWorld()
#         wx, wy, wz, w = renderer.GetWorldPoint()
#         if abs(w) < 1e-12:
#             return (wx, wy, wz)
#         return (wx / w, wy / w, wz / w)

#     def _pixel_radius_to_view_radius(self, vtk_widget, center_world, radius_px):
#         """
#         Convert pixel radius (screen space) into radius in tool coords:
#         - Main view: (x,y)
#         - Cross-section: (u,z)
#         - Cut-section: projected (u,z)
#         """
#         try:
#             center_world = (float(center_world[0]), float(center_world[1]), float(center_world[2]))
#             edge_world = self._display_to_world_same_depth(vtk_widget, center_world, dx_px=float(radius_px), dy_px=0.0)

#             u0, v0 = self._get_view_coordinates(center_world)
#             u1, v1 = self._get_view_coordinates(edge_world)

#             r = float(np.hypot(u1 - u0, v1 - v0))
#             if (not np.isfinite(r)) or (r <= 1e-12):
#                 return float(getattr(self.app, "brush_radius", 1.0))
#             return r
#         except Exception:
#             # fallback to old behavior if something goes wrong
#             return float(getattr(self.app, "brush_radius", 1.0))

#     def get_visible_class_filter(self, section_mask):
#         """
#         Restrict section_mask to ONLY those points whose class is visible
#         in the ACTIVE view's Display Mode.
        
#         ✅ FIXED: Properly detects cut section and uses slot 5 palette
#         ✅ Works for: Main View, Cross-Section Views (1-4), Cut Section View
        
#         section_mask: boolean mask of points in the current section
#                     (same length as the full dataset)
#         returns: boolean mask, same shape, with invisible classes removed.
        
#         View mapping:
#         - Slot 0: Main View
#         - Slot 1-4: Cross-Section Views 1-4
#         - Slot 5: Cut Section View (NEW!)
#         """
        
#         # ═══════════════════════════════════════════════════════════════════════
#         # STEP 1: Safety checks
#         # ═══════════════════════════════════════════════════════════════════════
        
#         if section_mask is None:
#             return None
        
#         # Need classification array
#         if not hasattr(self.app, "data") or "classification" not in self.app.data:
#             print("⚠️ No classification data – skipping visibility filter")
#             return section_mask
        
#         # ═══════════════════════════════════════════════════════════════════════
#         # STEP 2: Determine which view is active (main, cross-section, or cut)
#         # ═══════════════════════════════════════════════════════════════════════
        
#         dialog = getattr(self.app, "display_mode_dialog", None)
        
#         if dialog is None:
#             print("ℹ️ No DisplayModeDialog – using section_mask only (no protection)")
#             return section_mask
        
#         # ✅ PRIORITY 1: Check if we're in CUT SECTION (highest priority)
#         is_cut_section = False
#         slot_idx = None
#         view_context = None
        
#         # Method 1: Check active_classify_target flag
#         if getattr(self.app, "active_classify_target", None) == "cut":
#             is_cut_section = True
        
#         # Method 2: Check by comparing interactor's widget with cut_vtk
#         if not is_cut_section and hasattr(self.app, 'cut_section_controller'):
#             try:
#                 active_widget = self._get_active_vtk_widget()
#                 cut_vtk = getattr(self.app.cut_section_controller, 'cut_vtk', None)
                
#                 if active_widget is not None and cut_vtk is not None:
#                     if active_widget == cut_vtk:
#                         is_cut_section = True
#                         print("✅ Detected: In CUT SECTION (by widget match)")
#             except Exception as e:
#                 print(f"⚠️ Widget detection failed: {e}")
        
#         # ✅ Cut section handling
#         if is_cut_section:
#             slot_idx = 5  # ✅ CUT SECTION always uses slot 5
#             view_context = "Cut Section View"
#             print(f"📍 Visibility filter for {view_context} (slot {slot_idx})")
        
#         # ✅ PRIORITY 2: Check if we're in CROSS-SECTION
#         else:
#             active_view = getattr(self.app.section_controller, "active_view", None)
            
#             if active_view is not None and 0 <= active_view <= 3:
#                 slot_idx = active_view + 1  # Convert 0-3 to 1-4
#                 view_context = f"Cross-Section View {active_view + 1}"
#                 print(f"📍 Visibility filter for {view_context} (slot {slot_idx})")
            
#             # ✅ PRIORITY 3: Fallback (shouldn't normally happen here)
#             else:
#                 print("ℹ️ No active_view detected – using section_mask only")
#                 return section_mask
        
#         # ═══════════════════════════════════════════════════════════════════════
#         # STEP 3: Get visible classes from display_mode_dialog
#         # ═══════════════════════════════════════════════════════════════════════
        
#         if slot_idx is None:
#             print("ℹ️ Could not determine view slot – using section_mask only")
#             return section_mask
        
#         try:
#             # ✅ Method 1: Use dialog's dedicated method
#             if hasattr(dialog, 'get_visible_classes_for_view'):
#                 visible_classes = set(dialog.get_visible_classes_for_view(slot_idx))
            
#             # ✅ Method 2: Direct access to view_palettes
#             elif hasattr(dialog, 'view_palettes') and slot_idx in dialog.view_palettes:
#                 palette = dialog.view_palettes[slot_idx]
#                 visible_classes = set([c for c, info in palette.items() if info.get('show', True)])
#                 print(f"   📋 Using view_palettes[{slot_idx}] directly")
            
#             else:
#                 print(f"⚠️ Could not get visible classes for {view_context} (slot {slot_idx})")
#                 return section_mask
        
#         except Exception as e:
#             print(f"⚠️ get_visible_classes_for_view({slot_idx}) failed: {e}")
#             return section_mask
        
#         # ═══════════════════════════════════════════════════════════════════════
#         # STEP 4: Check if any classes are visible
#         # ═══════════════════════════════════════════════════════════════════════
        
#         if not visible_classes:
#             print(f"ℹ️ {view_context}: no visible classes selected – allowing all section points")
#             return section_mask
        
#         print(f"🔍 {view_context} (slot {slot_idx}) visible classes: {visible_classes}")
        
#         # ═══════════════════════════════════════════════════════════════════════
#         # STEP 5: Build final mask = section_mask ∧ (class ∈ visible_classes)
#         # ═══════════════════════════════════════════════════════════════════════
        
#         cls_all = self.app.data["classification"]
        
#         # indices of points that are in this section
#         section_idx = np.flatnonzero(section_mask)
#         section_cls = cls_all[section_idx]
        
#         # True only for section points whose class is visible in THIS view
#         visible_in_section = np.isin(section_cls, list(visible_classes))
        
#         # Start from all False; enable only visible section points
#         filtered_mask = np.zeros_like(section_mask, dtype=bool)
#         filtered_mask[section_idx[visible_in_section]] = True
        
#         # ═══════════════════════════════════════════════════════════════════════
#         # STEP 6: Report statistics
#         # ═══════════════════════════════════════════════════════════════════════
        
#         total_section = section_mask.sum()
#         visible_section = filtered_mask.sum()
#         filtered_out = total_section - visible_section
        
#         print(f"   📊 Section statistics:")
#         print(f"      Total points in section: {total_section}")
#         print(f"      Visible points (will classify): {visible_section}")
#         print(f"      Hidden points (PROTECTED): {filtered_out}")
        
#         return filtered_mask


#     # ---------------- PICK ----------------
#     def _pick_world_point(self, x, y):
#         """Pick 3D world coordinates from screen position."""
#         picker = self.interactor.GetPicker()
#         renderer = self.interactor.GetRenderWindow().GetRenderers().GetFirstRenderer()
#         picker.Pick(x, y, 0, renderer)
#         return picker.GetPickPosition()

#     # ---------------- PREVIEWS ----------------
#     def _draw_temp_line(self, P1, P2):
#         """
#         ✅ Senior Refactor: Handles rapid-fire classification.
#         Ensures actor persistence even if the renderer is cleared between actions.
#         """
#         vtk_widget = self._get_active_vtk_widget()
#         if not vtk_widget: return
#         renderer = vtk_widget.renderer

#         # 1. Pipeline Initialization & Recovery
#         if not hasattr(self, "line_actor") or self.line_actor is None:
#             self.line_pts = vtk.vtkPoints()
#             self.line_pts.SetNumberOfPoints(2)
#             lines = vtk.vtkCellArray()
#             lines.InsertNextCell(2); lines.InsertCellPoint(0); lines.InsertCellPoint(1)
#             self.line_poly = vtk.vtkPolyData()
#             self.line_poly.SetPoints(self.line_pts); self.line_poly.SetLines(lines)
            
#             mapper = vtk.vtkPolyDataMapper2D()
#             mapper.SetInputData(self.line_poly)
#             c = vtk.vtkCoordinate(); c.SetCoordinateSystemToDisplay()
#             mapper.SetTransformCoordinate(c)

#             self.line_actor = vtk.vtkActor2D()
#             self.line_actor.SetMapper(mapper)
#             p = self.line_actor.GetProperty()
#             p.SetColor(1, 1, 0); p.SetLineWidth(2); p.SetOpacity(1.0)

#         # 2. MANDATORY SYNC: Ensure actor is actually in the renderer's draw list
#         if not renderer.HasViewProp(self.line_actor):
#             renderer.AddActor2D(self.line_actor)
        
#         self.line_actor.VisibilityOn()

#         # 3. Fast Coordinate Update
#         u1, v1 = self._get_view_coordinates(P1)
#         u2, v2 = self._get_view_coordinates(P2)
#         w1 = self._view2d_to_world(u1, v1)
#         w2 = self._view2d_to_world(u2, v2)

#         if not hasattr(self, "_coord_converter"):
#             self._coord_converter = vtk.vtkCoordinate()
#             self._coord_converter.SetCoordinateSystemToWorld()

#         for i, pos in enumerate([w1, w2]):
#             self._coord_converter.SetValue(*pos)
#             d = self._coord_converter.GetComputedDisplayValue(renderer)
#             self.line_pts.SetPoint(i, d[0], d[1], 0)

#         self.line_pts.Modified()
#         self.line_poly.Modified()

#         # 4. Vertical Guides Sync
#         tool = getattr(self.app, "active_classify_tool", None)
#         if tool in ("above_line", "below_line", "parallel_line"):
#             self._draw_vertical_guides(P1, P2, tool, renderer)
#         # Render is batched by _on_mouse_move_with_preview — do NOT call here

#     def _draw_vertical_guides(self, P1, P2, tool, renderer):
#         """
#         ✅ Senior Refactor: Persistent dashed guides.
#         Verified for high-speed repeated interaction.
#         """
#         if not hasattr(self, "dotted_actor") or self.dotted_actor is None:
#             self.dotted_pts = vtk.vtkPoints()
#             self.dotted_lines = vtk.vtkCellArray()
#             self.dotted_poly = vtk.vtkPolyData()
#             self.dotted_poly.SetPoints(self.dotted_pts); self.dotted_poly.SetLines(self.dotted_lines)
#             m = vtk.vtkPolyDataMapper2D(); m.SetInputData(self.dotted_poly)
#             c = vtk.vtkCoordinate(); c.SetCoordinateSystemToDisplay()
#             m.SetTransformCoordinate(c)
#             self.dotted_actor = vtk.vtkActor2D(); self.dotted_actor.SetMapper(m)
#             p = self.dotted_actor.GetProperty()
#             p.SetColor(1, 1, 0); p.SetLineWidth(2); p.SetOpacity(0.9)

#         # Ensure actor presence in the current renderer
#         if not renderer.HasViewProp(self.dotted_actor):
#             renderer.AddActor2D(self.dotted_actor)

#         # Update Display values
#         self._coord_converter.SetValue(*P1)
#         p1 = self._coord_converter.GetComputedDisplayValue(renderer)
#         self._coord_converter.SetValue(*P2)
#         p2 = self._coord_converter.GetComputedDisplayValue(renderer)

#         if tool == "parallel_line":
#             self._draw_parallel_band_guides_display(P1, P2, renderer)
#             return

#         win_h = renderer.GetRenderWindow().GetSize()[1]

#         self.dotted_pts.Reset(); self.dotted_lines.Reset()
#         dash, gap = 8, 6
#         step = dash + gap

#         # ── VECTORIZED DASH GENERATION (no Python loop) ──────────────────────
#         all_pts_list = []
#         all_cells = []
#         idx = 0
#         for px, py_start in [(p1[0], p1[1]), (p2[0], p2[1])]:
#             if tool == "above_line":
#                 y_starts = np.arange(py_start, win_h, step)
#                 y_ends   = np.minimum(y_starts + dash, win_h)
#             else:
#                 y_starts = np.arange(py_start, 0, -step)
#                 y_ends   = np.maximum(y_starts - dash, 0.0)
#             n = len(y_starts)
#             if n == 0:
#                 continue
#             col = np.full(n, float(px))
#             z   = np.zeros(n)
#             seg = np.column_stack([
#                 np.column_stack([col, y_starts, z]),
#                 np.column_stack([col, y_ends,   z]),
#             ]).reshape(-1, 3)          # shape (2n, 3) — start/end interleaved
#             all_pts_list.append(seg)
#             pairs = np.arange(idx, idx + 2 * n).reshape(n, 2)
#             all_cells.append(pairs)
#             idx += 2 * n

#         if idx > 0:
#             all_pts = np.vstack(all_pts_list)
#             from vtkmodules.util.numpy_support import (
#                 numpy_to_vtk as _n2v,
#                 numpy_to_vtkIdTypeArray as _n2vid,
#             )
#             # Deep copy avoids dangling NumPy pointers after this function returns.
#             vtk_pts_data = _n2v(all_pts, deep=True)
#             self.dotted_pts.SetData(vtk_pts_data)
#             self.dotted_pts.Modified()

#             # ⚡ Vectorized cell setup — replaces a Python for-loop that
#             # was burning ~3–7 ms per preview frame (240+ SWIG calls per
#             # frame at 60 FPS = 14 400 transitions per second). VTK
#             # cell-array format is flat: [n, idx0, idx1, ..., idxN-1, n, ...]
#             # for each cell, where n is the per-cell point count (here 2).
#             all_pairs = np.vstack(all_cells).astype(np.int64, copy=False)
#             n_lines = all_pairs.shape[0]
#             cells_flat = np.empty(n_lines * 3, dtype=np.int64)
#             cells_flat[0::3] = 2                 # 2 pts per line cell
#             cells_flat[1::3] = all_pairs[:, 0]
#             cells_flat[2::3] = all_pairs[:, 1]
#             vtk_cells = _n2vid(cells_flat, deep=True)
#             self.dotted_lines.SetCells(n_lines, vtk_cells)
#             self.dotted_poly.Modified()
#             self.dotted_actor.VisibilityOn()
#         else:
#             self.dotted_actor.VisibilityOff()

#     def _get_parallel_line_tolerances(self):
#         mode = str(getattr(self.app, "parallel_line_mode", "symmetric")).strip().lower()

#         def _safe_width(value, default):
#             try:
#                 v = float(value)
#             except Exception:
#                 v = float(default)
#             if not np.isfinite(v):
#                 v = float(default)
#             # Guardrail: extreme widths can produce invalid native render coords.
#             return max(0.0, min(float(v), 10000.0))

#         total_width = _safe_width(getattr(self.app, "parallel_line_total_width", 0.40), 0.40)
#         tol_above = _safe_width(getattr(self.app, "parallel_line_tol_above", 0.20), 0.20)
#         tol_below = _safe_width(getattr(self.app, "parallel_line_tol_below", 0.20), 0.20)

#         if mode == "asymmetric":
#             return tol_above, tol_below
#         half = total_width * 0.5
#         return half, half

#     def _draw_parallel_band_guides_display(self, P1, P2, renderer):
#         """
#         Draw two solid boundary lines parallel to the center line preview.
#         """
#         tol_above, tol_below = self._get_parallel_line_tolerances()
#         u1, z1 = self._get_view_coordinates(P1)
#         u2, z2 = self._get_view_coordinates(P2)

#         dv_u = float(u2 - u1)
#         dv_z = float(z2 - z1)
#         length = float(np.hypot(dv_u, dv_z))
#         if length <= 1e-12:
#             self.dotted_actor.VisibilityOff()
#             return

#         d_u = dv_u / length
#         d_z = dv_z / length
#         n_u, n_z = -d_z, d_u
#         if n_z < 0.0:
#             n_u, n_z = -n_u, -n_z

#         upper1 = (u1 + n_u * tol_above, z1 + n_z * tol_above)
#         upper2 = (u2 + n_u * tol_above, z2 + n_z * tol_above)
#         lower1 = (u1 - n_u * tol_below, z1 - n_z * tol_below)
#         lower2 = (u2 - n_u * tol_below, z2 - n_z * tol_below)

#         up_w1 = self._view2d_to_world(*upper1)
#         up_w2 = self._view2d_to_world(*upper2)
#         lo_w1 = self._view2d_to_world(*lower1)
#         lo_w2 = self._view2d_to_world(*lower2)

#         disp_pts = []
#         for world_pt in (up_w1, up_w2, lo_w1, lo_w2):
#             self._coord_converter.SetValue(*world_pt)
#             d = self._coord_converter.GetComputedDisplayValue(renderer)
#             dx = float(d[0])
#             dy = float(d[1])
#             if not (np.isfinite(dx) and np.isfinite(dy)):
#                 self.dotted_actor.VisibilityOff()
#                 return
#             disp_pts.append((dx, dy, 0.0))

#         self.dotted_pts.Reset()
#         self.dotted_lines.Reset()
#         for x, y, z in disp_pts:
#             self.dotted_pts.InsertNextPoint(x, y, z)
#         self.dotted_lines.InsertNextCell(2)
#         self.dotted_lines.InsertCellPoint(0)
#         self.dotted_lines.InsertCellPoint(1)
#         self.dotted_lines.InsertNextCell(2)
#         self.dotted_lines.InsertCellPoint(2)
#         self.dotted_lines.InsertCellPoint(3)
#         self.dotted_pts.Modified()
#         self.dotted_poly.Modified()
#         self.dotted_actor.VisibilityOn()

#     def _draw_rectangle_preview(self, P1, P2):
#         """
#         ✅ Senior Refactor: Persistent Rectangle.
#         Forces re-attachment to renderer to prevent disappearance during fast clicks.
#         """
#         vtk_widget = self._get_active_vtk_widget()
#         if not vtk_widget: return
#         renderer = vtk_widget.renderer

#         if not hasattr(self, "rect_actor") or self.rect_actor is None:
#             self.rect_pts = vtk.vtkPoints()
#             self.rect_pts.SetNumberOfPoints(4)
#             self.rect_lines = vtk.vtkCellArray()
#             for i in range(4):
#                 self.rect_lines.InsertNextCell(2)
#                 self.rect_lines.InsertCellPoint(i); self.rect_lines.InsertCellPoint((i + 1) % 4)
#             self.rect_poly = vtk.vtkPolyData()
#             self.rect_poly.SetPoints(self.rect_pts); self.rect_poly.SetLines(self.rect_lines)
#             m = vtk.vtkPolyDataMapper2D(); m.SetInputData(self.rect_poly)
#             c = vtk.vtkCoordinate(); c.SetCoordinateSystemToDisplay()
#             m.SetTransformCoordinate(c)
#             self.rect_actor = vtk.vtkActor2D(); self.rect_actor.SetMapper(m)
#             setattr(self.rect_actor, "_is_classification_preview", True)
#             setattr(self.rect_actor, "_naksha_preview_owner", "classification")
#             p = self.rect_actor.GetProperty()
#             p.SetColor(1, 1, 0); p.SetLineWidth(2); p.SetOpacity(1.0)

#         # Recovery Logic: If renderer was cleared, re-add actor immediately
#         if not renderer.HasViewProp(self.rect_actor):
#             renderer.AddActor2D(self.rect_actor)
        
#         self.rect_actor.VisibilityOn()

#         u1, v1 = self._get_view_coordinates(P1)
#         u2, v2 = self._get_view_coordinates(P2)
#         corners = [(u1, v1), (u2, v1), (u2, v2), (u1, v2)]

#         if not hasattr(self, "_coord_converter"):
#             self._coord_converter = vtk.vtkCoordinate()
#             self._coord_converter.SetCoordinateSystemToWorld()

#         for i, (u, v) in enumerate(corners):
#             xw, yw, zw = self._view2d_to_world(u, v)
#             self._coord_converter.SetValue(xw, yw, zw)
#             d = self._coord_converter.GetComputedDisplayValue(renderer)
#             self.rect_pts.SetPoint(i, d[0], d[1], 0)

#         self.rect_pts.Modified()
#         self.rect_poly.Modified()
#         # Render batched by caller

#     @staticmethod
#     def _catmull_rom_smooth(points, steps=8):
#         """
#         Apply Catmull-Rom spline interpolation to a list of (u, z) 2-D points.

#         Each consecutive pair of control points is expanded into `steps` smoothly
#         curved sub-points, so the preview and the classification polygon use true
#         curves instead of straight line segments.

#         Parameters
#         ----------
#         points : list of (u, z) tuples   — raw freehand control points
#         steps  : int                     — interpolated sub-points per segment

#         Returns
#         -------
#         list of (u, z) tuples with len ≈ (len(points)-1)*steps + 1
#         """
#         import numpy as np
#         n = len(points)
#         if n < 2:
#             return list(points)
#         if n == 2:
#             # Only two points — linear interpolation is already a straight line;
#             # return as-is so we don't over-elaborate degenerate cases.
#             return list(points)

#         pts = np.asarray(points, dtype=np.float64)
#         result = []

#         for i in range(n - 1):
#             # Ghost points at the boundaries use reflection to avoid a flat tail.
#             p0 = pts[i - 1] if i > 0       else (2.0 * pts[i] - pts[i + 1])
#             p1 = pts[i]
#             p2 = pts[i + 1]
#             p3 = pts[i + 2] if i + 2 < n   else (2.0 * pts[i + 1] - pts[i])

#             ts = np.linspace(0.0, 1.0, steps, endpoint=(i == n - 2))
#             t2 = ts * ts
#             t3 = t2 * ts

#             seg = 0.5 * (
#                 2.0 * p1
#                 + (-p0 + p2)                         * ts[:, None]
#                 + (2.0*p0 - 5.0*p1 + 4.0*p2 - p3)   * t2[:, None]
#                 + (-p0 + 3.0*p1 - 3.0*p2 + p3)       * t3[:, None]
#             )
#             result.extend(seg.tolist())

#         # Always include the exact last control point.
#         result.append(list(pts[-1]))
#         return result

#     def _draw_freehand_preview(self, live_point=None):
#         """
#         ✅ PERFORMANCE FIX: Reuse VTK actor — NO RemoveActor/AddActor per frame.
#         Only updates point coordinates and calls Modified(). Zero allocation after init.
#         Draws smooth Catmull-Rom curves instead of straight line segments.
#         """
#         if len(self.drawing_points) < 1:
#             return

#         vtk_widget = self._get_active_vtk_widget()
#         if vtk_widget is None:
#             return
#         renderer = vtk_widget.renderer

#         # Collect all raw control points
#         all_pts = list(self.drawing_points)
#         if live_point:
#             all_pts.append(live_point)

#         if len(all_pts) < 2:
#             return

#         # ── SMOOTH: replace straight segments with Catmull-Rom curves ────────
#         all_pts = self._catmull_rom_smooth(all_pts, steps=8)

#         n = len(all_pts)

#         # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
#         if not hasattr(self, "_freehand_pts") or self.freehand_actor is None:
#             from vtkmodules.vtkRenderingCore import vtkPolyDataMapper2D, vtkActor2D, vtkCoordinate as _C

#             self._freehand_pts   = vtkPoints()
#             self._freehand_lines = vtkCellArray()
#             self._freehand_poly  = vtkPolyData()
#             self._freehand_poly.SetPoints(self._freehand_pts)
#             self._freehand_poly.SetLines(self._freehand_lines)

#             mapper = vtkPolyDataMapper2D()
#             mapper.SetInputData(self._freehand_poly)
#             dc = _C(); dc.SetCoordinateSystemToDisplay()
#             mapper.SetTransformCoordinate(dc)

#             self.freehand_actor = vtkActor2D()
#             self.freehand_actor.SetMapper(mapper)
#             prop = self.freehand_actor.GetProperty()
#             prop.SetColor(1, 0.8, 0)
#             prop.SetLineWidth(2)
#             prop.SetOpacity(0.8)

#             renderer.AddActor2D(self.freehand_actor)

#         # Re-attach if renderer was cleared
#         if not renderer.HasViewProp(self.freehand_actor):
#             renderer.AddActor2D(self.freehand_actor)
#         self.freehand_actor.VisibilityOn()

#         # ── FAST COORDINATE UPDATE ───────────────────────────────────────────
#         if not hasattr(self, "_freehand_coord"):
#             from vtkmodules.vtkRenderingCore import vtkCoordinate as _C
#             self._freehand_coord = _C()
#             self._freehand_coord.SetCoordinateSystemToWorld()

#         coord = self._freehand_coord
#         self._freehand_pts.Reset()
#         self._freehand_lines.Reset()

#         for i, (u, z) in enumerate(all_pts):
#             xw, yw, zw = self._view2d_to_world(u, z)
#             coord.SetValue(xw, yw, zw)
#             d = coord.GetComputedDisplayValue(renderer)
#             self._freehand_pts.InsertNextPoint(d[0], d[1], 0)
#             if i > 0:
#                 self._freehand_lines.InsertNextCell(2)
#                 self._freehand_lines.InsertCellPoint(i - 1)
#                 self._freehand_lines.InsertCellPoint(i)

#         self._freehand_pts.Modified()
#         self._freehand_poly.Modified()
#         # Render is batched by caller (_on_mouse_move_with_preview)



#     def _draw_polygon_preview(self, live_point=None):
#         """
#         ✅ PERFORMANCE FIX: Reuse Actor2D — no RemoveActor/new objects per frame.
#         """
#         if len(self.drawing_points) < 1:
#             return

#         vtk_widget = self._get_active_vtk_widget()
#         if vtk_widget is None:
#             return
#         renderer = vtk_widget.renderer

#         all_pts = list(self.drawing_points)
#         if live_point:
#             all_pts.append(live_point)

#         n = len(all_pts)

#         # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
#         if not hasattr(self, "_poly_pts_2d") or self.poly_actor is None:
#             from vtkmodules.vtkRenderingCore import vtkPolyDataMapper2D, vtkActor2D, vtkCoordinate as _C

#             self._poly_pts_2d   = vtkPoints()
#             self._poly_lines_2d = vtkCellArray()
#             self._poly_poly_2d  = vtkPolyData()
#             self._poly_poly_2d.SetPoints(self._poly_pts_2d)
#             self._poly_poly_2d.SetLines(self._poly_lines_2d)

#             mapper = vtkPolyDataMapper2D()
#             mapper.SetInputData(self._poly_poly_2d)
#             dc = _C(); dc.SetCoordinateSystemToDisplay()
#             mapper.SetTransformCoordinate(dc)

#             self.poly_actor = vtkActor2D()
#             self.poly_actor.SetMapper(mapper)
#             prop = self.poly_actor.GetProperty()
#             prop.SetColor(1, 1, 0)
#             prop.SetLineWidth(3)
#             prop.SetOpacity(1.0)

#             renderer.AddActor2D(self.poly_actor)

#         if not renderer.HasViewProp(self.poly_actor):
#             renderer.AddActor2D(self.poly_actor)
#         self.poly_actor.VisibilityOn()

#         # ── FAST COORDINATE UPDATE ───────────────────────────────────────────
#         if not hasattr(self, "_poly_coord_conv"):
#             from vtkmodules.vtkRenderingCore import vtkCoordinate as _C
#             self._poly_coord_conv = _C()
#             self._poly_coord_conv.SetCoordinateSystemToWorld()

#         coord = self._poly_coord_conv
#         self._poly_pts_2d.Reset()
#         self._poly_lines_2d.Reset()

#         for i, (u, z) in enumerate(all_pts):
#             xw, yw, zw = self._view2d_to_world(u, z)
#             coord.SetValue(xw, yw, zw)
#             d = coord.GetComputedDisplayValue(renderer)
#             self._poly_pts_2d.InsertNextPoint(d[0], d[1], 0)
#             if i > 0:
#                 self._poly_lines_2d.InsertNextCell(2)
#                 self._poly_lines_2d.InsertCellPoint(i - 1)
#                 self._poly_lines_2d.InsertCellPoint(i)

#         # Close polygon if > 2 points
#         if n > 2:
#             self._poly_lines_2d.InsertNextCell(2)
#             self._poly_lines_2d.InsertCellPoint(n - 1)
#             self._poly_lines_2d.InsertCellPoint(0)

#         self._poly_pts_2d.Modified()
#         self._poly_poly_2d.Modified()
#         # Render batched by caller

#     def _draw_polygon_preview_cut(self, live_point=None):
#         """Polygon preview for cut-section view using world-space vertices."""
#         if not hasattr(self, "drawing_points_world_cut"):
#             return
#         if len(self.drawing_points_world_cut) < 1:
#             return

#         vtk_widget = self._get_active_vtk_widget()
#         if vtk_widget is None:
#             return
#         renderer = vtk_widget.renderer

#         all_pts = list(self.drawing_points_world_cut)
#         if live_point is not None:
#             all_pts.append(live_point)
#         n = len(all_pts)
#         if n < 1:
#             return

#         if not hasattr(self, "_poly_cut_pts") or getattr(self, "poly_actor_cut", None) is None:
#             from vtkmodules.vtkRenderingCore import vtkPolyDataMapper2D, vtkActor2D, vtkCoordinate

#             self._poly_cut_pts = vtkPoints()
#             self._poly_cut_lines = vtkCellArray()
#             self._poly_cut_poly = vtkPolyData()
#             self._poly_cut_poly.SetPoints(self._poly_cut_pts)
#             self._poly_cut_poly.SetLines(self._poly_cut_lines)

#             mapper = vtkPolyDataMapper2D()
#             mapper.SetInputData(self._poly_cut_poly)
#             # Keep polygon preview anchored during pan/zoom by mapping points from world space.
#             world_coord = vtkCoordinate()
#             world_coord.SetCoordinateSystemToWorld()
#             mapper.SetTransformCoordinate(world_coord)
#             mapper.TransformCoordinateUseDoubleOn()

#             self.poly_actor_cut = vtkActor2D()
#             self.poly_actor_cut.SetMapper(mapper)
#             prop = self.poly_actor_cut.GetProperty()
#             prop.SetColor(1.0, 1.0, 0.0)
#             prop.SetLineWidth(3)
#             prop.SetOpacity(1.0)

#             renderer.AddActor2D(self.poly_actor_cut)

#         if not renderer.HasViewProp(self.poly_actor_cut):
#             renderer.AddActor2D(self.poly_actor_cut)
#         self.poly_actor_cut.VisibilityOn()

#         pts = self._poly_cut_pts
#         lines = self._poly_cut_lines
#         pts.Reset()
#         lines.Reset()

#         for xw, yw, zw in all_pts:
#             pts.InsertNextPoint(float(xw), float(yw), float(zw))
#         for i in range(n - 1):
#             lines.InsertNextCell(2)
#             lines.InsertCellPoint(i)
#             lines.InsertCellPoint(i + 1)
#         if n > 2:
#             lines.InsertNextCell(2)
#             lines.InsertCellPoint(n - 1)
#             lines.InsertCellPoint(0)

#         pts.Modified()
#         self._poly_cut_poly.Modified()

#     def _draw_brush_cursor(self, center, radius=20.0):
#         """
#         ✅ PERFORMANCE FIX: Brush cursor in DISPLAY pixels (zoom invariant).
#         Reuses VTK actor — only updates point data. Zero allocation per frame.
#         """
#         vtk_widget = self._get_active_vtk_widget()
#         if vtk_widget is None:
#             return
#         renderer = vtk_widget.renderer

#         x, y = self.interactor.GetEventPosition()
#         r = max(float(radius), 3.0)

#         num_segments = 32
#         dot_segments = 8

#         # Total points: dashed circle (num_segments pts) + crosshair (4 pts) + dot (dot_segments*2 pts)
#         # Each dash: 2 pts  ×  num_segments/2 dashes = num_segments pts
#         # Crosshair: 4 pts (2 lines × 2 pts)
#         # Dot ring: dot_segments × 2 pts
#         total_pts = num_segments + 4 + dot_segments * 2

#         # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
#         if not hasattr(self, "_brush_pts") or self.brush_actor is None:
#             from vtkmodules.vtkRenderingCore import vtkPolyDataMapper2D, vtkActor2D, vtkCoordinate as _C

#             if getattr(self, "brush_actor", None) is not None:
#                 try:
#                     renderer.RemoveActor2D(self.brush_actor)
#                 except Exception:
#                     pass
#                 self.brush_actor = None

#             self._brush_pts   = vtkPoints()
#             self._brush_lines = vtkCellArray()
#             self._brush_poly  = vtkPolyData()
#             self._brush_poly.SetPoints(self._brush_pts)
#             self._brush_poly.SetLines(self._brush_lines)

#             mapper = vtkPolyDataMapper2D()
#             mapper.SetInputData(self._brush_poly)
#             dc = _C(); dc.SetCoordinateSystemToDisplay()
#             mapper.SetTransformCoordinate(dc)

#             self.brush_actor = vtkActor2D()
#             self.brush_actor.SetMapper(mapper)
#             prop = self.brush_actor.GetProperty()
#             prop.SetColor(1, 1, 0)
#             prop.SetLineWidth(3)
#             prop.SetOpacity(0.9)

#             renderer.AddActor2D(self.brush_actor)

#         if not renderer.HasViewProp(self.brush_actor):
#             renderer.AddActor2D(self.brush_actor)
#         self.brush_actor.VisibilityOn()

#         # ── FAST GEOMETRY UPDATE (display coords, no world conversion) ────────
#         pts   = self._brush_pts
#         lines = self._brush_lines
#         pts.Reset()
#         lines.Reset()

#         def add_seg(p0, p1):
#             i0 = pts.InsertNextPoint(float(p0[0]), float(p0[1]), 0.0)
#             i1 = pts.InsertNextPoint(float(p1[0]), float(p1[1]), 0.0)
#             lines.InsertNextCell(2)
#             lines.InsertCellPoint(i0)
#             lines.InsertCellPoint(i1)

#         # dashed circle (every other segment)
#         for i in range(0, num_segments, 2):
#             a1 = 2.0 * np.pi * i / num_segments
#             a2 = 2.0 * np.pi * (i + 1) / num_segments
#             p1 = (x + r * np.cos(a1), y + r * np.sin(a1))
#             p2 = (x + r * np.cos(a2), y + r * np.sin(a2))
#             add_seg(p1, p2)

#         # crosshair
#         ch = r * 0.3
#         add_seg((x - ch, y), (x + ch, y))
#         add_seg((x, y - ch), (x, y + ch))

#         # center dot ring
#         dot_r = max(2.0, r * 0.05)
#         for i in range(dot_segments):
#             a1 = 2.0 * np.pi * i / dot_segments
#             a2 = 2.0 * np.pi * (i + 1) / dot_segments
#             p1 = (x + dot_r * np.cos(a1), y + dot_r * np.sin(a1))
#             p2 = (x + dot_r * np.cos(a2), y + dot_r * np.sin(a2))
#             add_seg(p1, p2)

#         pts.Modified()
#         self._brush_poly.Modified()
#         # Render batched by caller

#     #     """
#     #     ✅ Optimized Clear: Uses Visibility toggles instead of Actor removal.
#     #     Eliminates the 'Delete-Recreate' lag and prevents preview flickering.
#     #     """
#     #     # 1. Identify all potential actor names
#     #         "line_actor", "dotted_actor", "rect_actor", "circle_actor",
#     #         "brush_actor", "polygon_actor", "freehand_actor", "poly_actor",
#     #         "line_actor_cut", "dotted_actor_cut", "rect_actor_cut", "circle_actor_cut",
#     #         "brush_actor_cut", "freehand_actor_cut", "poly_actor_cut",
#     #     ]

#     #     # 2. Batch Visibility Change (Instantaneous)
#     #             # Instead of removing/None, we just hide. 
#     #             # This keeps the VTK pipeline ready for the next interaction.
#     #             actor.VisibilityOff()

#     #     # 3. Reset state variables
#     #     self.P1 = None
#     #     self.is_dragging = False
#     #     self.drawing_points = []
#     #     self.is_drawing_freehand = False
        
#     #         self.drawing_points_display_cut = []
#     #         self.P1_display_cut = None

#     #     # 4. Single Final Render Call
#     #     # We only render ONCE to update the screen, not once per renderer/actor.
#     #             self._safe_render(vtk_widget)
        
#     #     # Also render cut-section if it exists
#     #                 self._safe_render(cut_vtk)

#     def _clear_all_previews(self):
#         """
#         ✅ Optimized Clear: Uses Visibility toggles instead of Actor removal.
#         Eliminates the 'Delete-Recreate' lag and prevents preview flickering.
    
#         ✅ FIX (fast-classification): Reset throttle timestamps so the very
#         first mouse-move of the NEXT classification is never skipped.
#         """
#         # 1. Identify all potential actor names
#         actor_names = [
#             "line_actor", "dotted_actor", "rect_actor", "circle_actor", "circle_actor_main",
#             "brush_actor", "polygon_actor", "freehand_actor", "poly_actor",
#             "line_actor_cut", "dotted_actor_cut", "rect_actor_cut", "circle_actor_cut",
#             "brush_actor_cut", "freehand_actor_cut", "poly_actor_cut",
#             "_locate_line_actor",
#         ]
    
#         # 2. Clear preview overlay first.
#         # Important: _hide_preview_overlay_for_pan() may restore hidden preview actors,
#         # so call it BEFORE hiding/removing the VTK preview actors.
#         self._hide_preview_overlay_for_pan()

#         preview_renderers = []

#         try:
#             vtk_widget = self._get_active_vtk_widget()
#             if vtk_widget is not None and getattr(vtk_widget, "renderer", None) is not None:
#                 preview_renderers.append(vtk_widget.renderer)
#         except Exception:
#             pass

#         try:
#             main_vtk = getattr(self.app, "vtk_widget", None)
#             if main_vtk is not None and getattr(main_vtk, "renderer", None) is not None:
#                 preview_renderers.append(main_vtk.renderer)
#         except Exception:
#             pass

#         try:
#             cut_ctrl = getattr(self.app, "cut_section_controller", None)
#             cut_vtk = getattr(cut_ctrl, "cut_vtk", None) if cut_ctrl is not None else None
#             if cut_vtk is not None and getattr(cut_vtk, "renderer", None) is not None:
#                 preview_renderers.append(cut_vtk.renderer)
#         except Exception:
#             pass

#         try:
#             for vtk_widget in list(getattr(self.app, "section_vtks", {}) or {}).values():
#                 if vtk_widget is not None and getattr(vtk_widget, "renderer", None) is not None:
#                     preview_renderers.append(vtk_widget.renderer)
#         except Exception:
#             pass

#         # Remove duplicates
#         unique_renderers = []
#         seen_renderers = set()
#         for renderer in preview_renderers:
#             if renderer is None:
#                 continue
#             marker = id(renderer)
#             if marker in seen_renderers:
#                 continue
#             seen_renderers.add(marker)
#             unique_renderers.append(renderer)

#         # Hide AND remove transient classification preview actors.
#         # VisibilityOff alone is not enough because Surface overlay restore can re-add
#         # main-renderer candidates and make them visible again.
#         for name in actor_names:
#             actor = getattr(self, name, None)
#             if actor:
#                 try:
#                     setattr(actor, "_is_classification_preview", True)
#                     setattr(actor, "_naksha_preview_owner", "classification")
#                 except Exception:
#                     pass

#                 try:
#                     actor.VisibilityOff()
#                 except Exception:
#                     pass

#                 for renderer in unique_renderers:
#                     try:
#                         renderer.RemoveActor(actor)
#                     except Exception:
#                         pass
#                     try:
#                         renderer.RemoveActor2D(actor)
#                     except Exception:
#                         pass
#                     try:
#                         renderer.RemoveViewProp(actor)
#                     except Exception:
#                         pass
    
#         # 3. Reset state variables
#         self.P1 = None
#         self.is_dragging = False
#         self.click_to_finalize = False
#         self._press_pos = None
#         self.drawing_points = []
#         self.is_drawing_freehand = False
    
#         if hasattr(self, 'drawing_points_display_cut'):
#             self.drawing_points_display_cut = []
#         if hasattr(self, 'drawing_points_world_cut'):
#             self.drawing_points_world_cut = []
#         if hasattr(self, 'P1_display_cut'):
#             self.P1_display_cut = None
    
#         # ✅ FIX A — Reset render/move throttles so the NEXT classification's
#         # first mouse-move is never accidentally skipped by the 16 ms guard.
#         self._last_render_time     = 0.0
#         self._last_mouse_move_time = 0.0
    
#         # 4. Single Final Render Call
#         vtk_widget = self._get_active_vtk_widget()
#         if vtk_widget:
#             try:
#                 self._safe_render(vtk_widget)
#             except Exception:
#                 pass
    
#         # Also render cut-section if it exists
#         if hasattr(self.app, 'cut_section_controller'):
#             cut_vtk = getattr(self.app.cut_section_controller, 'cut_vtk', None)
#             if cut_vtk and hasattr(cut_vtk, 'renderer'):
#                 try:
#                     self._safe_render(cut_vtk)
#                 except Exception:
#                     pass  
 

#     def _on_mouse_move_with_preview(self, obj, evt):
#             """
#             ✅ FIXED: Shows previews during drag for ALL tools
#             ✅ FIXED: Brush preview size does NOT change with zoom
#             ✅ FIXED: Brush classification area MATCHES the preview (pixel -> view radius conversion)
#             ✅ FIXED: Above/Below line uses EXACT same P2 on release as preview (prevents snapping to pole)
#             ✅ FIXED: Rectangle uses EXACT same P2 on release as preview (prevents coordinate mismatch)

#             ✅ FIX (safe): ensure np is always defined in this function scope to avoid:
#             "local variable 'np' referenced before assignment"
#             """
#             self._check_tool_changed()

#             # Capture deliberate line-drag distance before preview throttling.
#             # Quick taps often contain normal OS/VTK pointer jitter.
#             active_tool = getattr(self.app, "active_classify_tool", None)
#             press_pos = getattr(self, "_press_pos", None)
#             if (
#                 active_tool in ("above_line", "below_line", "parallel_line")
#                 and press_pos is not None
#                 and not getattr(self, "click_to_finalize", False)
#             ):
#                 try:
#                     move_x, move_y = self.interactor.GetEventPosition()
#                     movement = (
#                         (move_x - press_pos[0]) ** 2
#                         + (move_y - press_pos[1]) ** 2
#                     ) ** 0.5
#                     self._line_press_max_move_px = max(
#                         float(getattr(self, "_line_press_max_move_px", 0.0)),
#                         float(movement),
#                     )
#                 except Exception:
#                     pass
#             import numpy as np  # ✅ IMPORTANT: prevents UnboundLocalError for np in this method

#             # ══════════════════════════════════════════════════════════════════
#             # PAN ABSOLUTE PRIORITY — mirror digitizer._on_mouse_move pattern.
#             # Without this guard, slow middle-button pan moves (more than 16ms
#             # apart) fall through to _get_display_point + _display_to_world_fast
#             # — a per-event world-point pick that makes panning feel heavy/laggy.
#             # _do_safe_pan already translates preview actors for any in-progress
#             # classification drag (rectangle/freehand/etc.), so the active gesture
#             # stays locked to the cursor while panning.
#             # ══════════════════════════════════════════════════════════════════
#             if getattr(self, '_is_panning', False):
#                 self._do_safe_pan()
#                 try:
#                     if hasattr(obj, "AbortFlagOn"):
#                         obj.AbortFlagOn()
#                     elif hasattr(obj, "SetAbortFlag"):
#                         obj.SetAbortFlag(1)
#                 except Exception:
#                     pass
#                 return

#             current_time = time.time()

#             if not hasattr(self, '_last_mouse_move_time'):
#                 self._last_mouse_move_time = 0.0

#             preview_dt = self._preview_interval_sec()
#             # Skip if less than 16ms since last update (60 FPS)
#             if (current_time - self._last_mouse_move_time) < preview_dt:
#                 self._do_safe_pan()  # ✅ Custom pan — no VTK Render()
#                 return

#             self._last_mouse_move_time = current_time

#             # ✅ SAFETY CHECKS
#             if not hasattr(self, 'app') or self.app is None:
#                 return
#             if not hasattr(self, 'style') or self.style is None:
#                 return
#             if not hasattr(self, 'interactor') or self.interactor is None:
#                 return

#             tool = getattr(self.app, "active_classify_tool", None)

#             if tool is not None:
#                 try:
#                     if hasattr(obj, "AbortFlagOn"):
#                         obj.AbortFlagOn()
#                     elif hasattr(obj, "SetAbortFlag"):
#                         obj.SetAbortFlag(1)
#                 except Exception:
#                     pass

#             if self._is_classification_paused_for_pan(tool):
#                 self._do_safe_pan()
#                 try:
#                     if hasattr(obj, "AbortFlagOn"):
#                         obj.AbortFlagOn()
#                     elif hasattr(obj, "SetAbortFlag"):
#                         obj.SetAbortFlag(1)
#                 except Exception:
#                     pass
#                 return

#             # Try to pick world point (fast, non-snapping)
#             try:
#                 dx, dy = self._get_display_point()
#                 P2 = self._display_to_world_fast(dx, dy)
#             except Exception:
#                 self._do_safe_pan()  # ✅ Custom pan — no VTK Render()
#                 return

#             # ✅ Cache active vtk widget (refresh every 100ms)
#             if not hasattr(self, '_cached_vtk_widget'):
#                 self._cached_vtk_widget = None
#                 self._cached_widget_time = 0.0

#             if (current_time - self._cached_widget_time) > 0.1:
#                 self._cached_vtk_widget = self._get_active_vtk_widget()
#                 self._cached_widget_time = current_time

#             vtk_widget = self._cached_vtk_widget

#             # ✅ Detect CUT SECTION
#             is_cut_section = False
#             if hasattr(self.app, 'cut_section_controller'):
#                 if vtk_widget == getattr(self.app.cut_section_controller, 'cut_vtk', None):
#                     is_cut_section = True

#             # ── Section-locate rubber-band: draw preview line from locked click to cursor ──
#             if not self._is_main_view() and not is_cut_section:
#                 locate_disp = getattr(self.app, '_section_locate_display', None)
#                 locate_view = getattr(self.app, '_section_locate_view', None)
#                 if locate_disp is not None and locate_view == self._get_view_index_from_interactor():
#                     self._draw_locate_line_in_section(vtk_widget, locate_disp, (dx, dy))
#                     if vtk_widget:
#                         self._safe_render(vtk_widget)

#             # Render throttle
#             if not hasattr(self, '_last_render_time'):
#                 self._last_render_time = 0.0

#             # -----------------------
#             # PREVIEW RENDER LOGIC
#             # -----------------------
#             if getattr(self.app, '_debug_perf', False):
#                 t_start = time.perf_counter()
#                 n_actors = vtk_widget.renderer.GetNumberOfProps()
#                 print(f"DEBUG: Rendering {n_actors} actors...")

#             # ✅ Circle preview
#             if tool == "circle" and self.P1 is not None and self.is_dragging:
#                 # ✅ CRITICAL: Store P2 for classification to match preview
#                 self._last_circle_preview_P2 = P2
                
#                 if is_cut_section:
#                     self._draw_circle_preview_cut(self.P1, P2)
#                 elif self._is_main_view():
#                     self._draw_circle_preview_main(self.P1, P2)
#                 else:
#                     self._draw_circle_preview(self.P1, P2)

#                 if vtk_widget and (current_time - self._last_render_time) > preview_dt:
#                     self._safe_render(vtk_widget)
#                     self._last_render_time = current_time

#             # ✅ Rectangle preview
#             if tool == "rectangle" and self.P1 is not None and self.is_dragging:
#                 # ✅ CRITICAL: Store P2 for classification to match preview
#                 self._last_rectangle_preview_P2 = P2
                
#                 if is_cut_section:
#                     self._draw_rectangle_preview_cut(self.P1, P2)
#                 else:
#                     self._draw_rectangle_preview(self.P1, P2)

#                 if vtk_widget and (current_time - self._last_render_time) > preview_dt:
#                     self._safe_render(vtk_widget)
#                     self._last_render_time = current_time

#             # ✅ Line tools preview
#             elif tool in ("above_line", "below_line", "parallel_line") and self.P1 is not None and self.is_dragging:
#                 self.app._classification_preview_active = True
#                 self._last_line_preview_P2 = P2

#                 if is_cut_section:
#                     self._draw_line_preview_cut(self.P1, P2)
#                 else:
#                     self._draw_temp_line(self.P1, P2)

#                 if vtk_widget and (current_time - self._last_render_time) > preview_dt:
#                     self._safe_render(vtk_widget)
#                     self._last_render_time = current_time

#             # ✅ Brush tool
#             elif tool == "brush":
#                 to_class = getattr(self.app, "to_class", None)
#                 if self.is_dragging:
#                     active_actor_names = self._get_active_tool_preview_actor_names("brush")
#                     non_active_actor_names = [
#                         name for name in self._get_preview_actor_names()
#                         if name not in active_actor_names
#                     ]
#                     if non_active_actor_names:
#                         self._set_preview_visibility(False, non_active_actor_names)

#                     self._last_brush_preview_P2 = P2  # ✅ non-snapping endpoint used for preview
#                     # ✅ FIX 1: Calculate center differently for cut section
#                     if is_cut_section:
#                         try:
#                             from ..classification_tools import _project_to_cut_view
#                             P2_cut = _project_to_cut_view(self.app, np.array([P2]))[0]
#                             center = (float(P2_cut[0]), float(P2_cut[1]))
#                         except Exception:
#                             center = self._get_view_coordinates(P2)
#                     else:
#                         # Cross-section or main view - existing logic works
#                         center = self._get_view_coordinates(P2)

#                     brush_size_pixels = float(getattr(self.app, "brush_preview_px", 20.0))

#                     radius_view = float(getattr(self.app, "brush_radius", 1.0))
#                     if vtk_widget is not None:
#                         try:
#                             radius_view = float(self._pixel_radius_to_view_radius(
#                                 vtk_widget, P2, brush_size_pixels
#                             ))
#                         except Exception:
#                             radius_view = float(getattr(self.app, "brush_radius", 1.0))

#                     self._last_brush_radius_view = radius_view

#                     brush_shape = getattr(self.app, "brush_shape", "circle")

#                     if is_cut_section:
#                         if brush_shape == "rectangle":
#                             self._draw_rectangle_cursor_cut_display(P2, brush_size_pixels)
#                         else:
#                             self._draw_brush_cursor_cut_display(P2, brush_size_pixels)
#                     else:
#                         if brush_shape == "rectangle":
#                             self._draw_rectangle_cursor(center, brush_size_pixels)
#                         else:
#                             self._draw_brush_cursor(center, brush_size_pixels)

#                 # ✅ UNIFIED RENDER: For main view brush, DON'T render here —
#                 # the classification drag handler below does a throttled render
#                 # that includes both cursor + classification changes in one pass.
#                 if self._is_main_view() and self.is_dragging and tool == "brush":
#                     pass  # Skip — render happens in classification block below
#                 elif vtk_widget and (current_time - self._last_render_time) > preview_dt:
#                     self._safe_render(vtk_widget)
#                     self._last_render_time = current_time

#                 if self.is_dragging:
#                     if self._is_main_view():
#                         # ═══════════════════════════════════════════════════════
#                         # MICROSTATION PRODUCTION BRUSH
#                         # Phase 1 (every mouse move): spatial lookup → CPU classify → accumulate
#                         # Phase 2 (30fps tick only): single GPU injection + render
#                         # Zero allocation per stamp. All numpy, no Python loops.
#                         # ═══════════════════════════════════════════════════════
#                         xyz = self.app.data["xyz"]
#                         to_class = getattr(self.app, "to_class", None)

#                         # ✅ DATASET CHANGE SAFEGUARD: Ensure mask matches current dataset size
#                         if hasattr(self, "_brush_accumulated_mask") and self._brush_accumulated_mask is not None:
#                             if len(self._brush_accumulated_mask) != len(xyz):
#                                 print(f"🔄 Dataset size changed ({len(self._brush_accumulated_mask)} -> {len(xyz)}), clearing brush mask")
#                                 self._brush_accumulated_mask = None
#                                 self._brush_old_classes = {}

#                         if not hasattr(self, "_brush_accumulated_mask") or self._brush_accumulated_mask is None:
#                             self._brush_accumulated_mask = np.zeros(len(xyz), dtype=bool)
#                             self._brush_old_classes = {}
#                             self._brush_frame_chunks = []

#                         if not hasattr(self, "_brush_stroke_positions") or self._brush_stroke_positions is None:
#                             self._brush_stroke_positions = []

#                         prev = getattr(self, "_last_brush_center", None)
#                         classes = self.app.data["classification"]
#                         from_classes = getattr(self.app, "from_classes", None)
#                         visible_classes = getattr(self, "_brush_visible_classes", None)
#                         visible_classes_arr = None
#                         if visible_classes is not None and visible_classes != []:
#                             visible_classes_arr = np.asarray(visible_classes, dtype=classes.dtype)

#                         u1, z1 = self._get_view_coordinates(prev or center)
#                         u2, z2 = self._get_view_coordinates(center)
                        
#                         u_min, u_max = min(u1, u2) - radius_view, max(u1, u2) + radius_view
#                         z_min, z_max = min(z1, z2) - radius_view, max(z1, z2) + radius_view
                        
#                         grid = getattr(self, "_main_grid_index", None)
#                         if grid and grid.valid:
#                             hit = grid.query_rectangle_minmax(u_min, z_min, u_max, z_max)
#                         else:
#                             hit = np.array([], dtype=np.int32)
                        
#                         if len(hit) > 0:
#                             # Distance-to-segment filter (vectorized)
#                             pts2d = self._main_points_2d[hit]
#                             du, dz = u2 - u1, z2 - z1
#                             l2 = du*du + dz*dz
                            
#                             if l2 < 1e-9:
#                                 # Stationary brush
#                                 dist_sq = (pts2d[:, 0] - u1)**2 + (pts2d[:, 1] - z1)**2
#                             else:
#                                 # Projection of points onto the line segment
#                                 t = np.clip(((pts2d[:, 0] - u1) * du + (pts2d[:, 1] - z1) * dz) / l2, 0, 1)
#                                 proj_u, proj_z = u1 + t * du, z1 + t * dz
#                                 dist_sq = (pts2d[:, 0] - proj_u)**2 + (pts2d[:, 1] - proj_z)**2
                            
#                             hit = hit[dist_sq <= (radius_view * radius_view)]
                            
#                         if len(hit) > 0:
#                             # Filter already-done in this stroke
#                             fresh = hit[~self._brush_accumulated_mask[hit]]
#                             if len(fresh) > 0:
#                                 # Filter from_classes / visible_classes
#                                 if from_classes:
#                                     fc_arr = np.asarray(from_classes, dtype=classes.dtype)
#                                     if visible_classes_arr is not None:
#                                         fc_mask = np.isin(classes[fresh], fc_arr[np.isin(fc_arr, visible_classes_arr)])
#                                     else:
#                                         fc_mask = np.isin(classes[fresh], fc_arr)
#                                     fresh = fresh[fc_mask]
#                                 elif visible_classes_arr is not None:
#                                     fresh = fresh[np.isin(classes[fresh], visible_classes_arr)]

#                                 if len(fresh) > 0:
#                                     # 1. Unique and capture for undo
#                                     fresh = np.unique(fresh)
#                                     if not hasattr(self, "_brush_old_classes_arrays"):
#                                         self._brush_old_classes_arrays = []
#                                         self._brush_indices_arrays = []
                                    
#                                     self._brush_old_classes_arrays.append(classes[fresh].copy())
#                                     self._brush_indices_arrays.append(fresh.copy())

#                                     # 2. CPU classify immediately
#                                     classes[fresh] = to_class
#                                     self._brush_accumulated_mask[fresh] = True

#                                     # 🖌️ Live brush stroke total → top bar center
#                                     try:
#                                         self.app._update_classify_count_display(
#                                             int(np.count_nonzero(self._brush_accumulated_mask))
#                                         )
#                                     except Exception:
#                                         pass

#                                     # 3. Accumulate for GPU tick
#                                     self._brush_frame_chunks.append(fresh)
#                                     self._brush_needs_render = True

#                         self._last_brush_center = center
#                         self._brush_stroke_positions.append(center)

#                         # PHASE 2: 30fps tick — single GPU injection + render
#                         frame_chunks = getattr(self, '_brush_frame_chunks', None)
#                         frame_fresh = None
#                         if frame_chunks:
#                             if len(frame_chunks) == 1:
#                                 frame_fresh = np.unique(frame_chunks[0])
#                             else:
#                                 frame_fresh = np.unique(np.concatenate(frame_chunks))

#                         if (frame_fresh is not None and len(frame_fresh) > 0
#                                 and getattr(self, '_brush_needs_render', False)):
#                             now = time.time()
#                             if (now - getattr(self, '_brush_last_render_time', 0)) > 0.033:
#                                 try:
#                                     from gui.unified_actor_manager import (
#                                         _get_unified_actor, _mark_actor_dirty,
#                                         fast_cross_section_update, _push_uniforms_direct
#                                     )
#                                     palette = self._get_active_palette()
#                                     entry = palette.get(int(to_class), {})
#                                     color = np.array(
#                                         entry.get('color', (128, 128, 128))
#                                         if entry.get('show', True)
#                                         else (0, 0, 0),
#                                         dtype=np.uint8
#                                     )

#                                     # Direct RGB poke on unified actor (no mask scan)
#                                     actor = _get_unified_actor(self.app)
#                                     if actor is not None:
#                                         rgb_ptr = getattr(actor, '_naksha_rgb_ptr', None)
#                                         class_ptr = getattr(actor, '_naksha_section_class', None)
#                                         mesh = getattr(actor, '_naksha_mesh', None)
#                                         gi = getattr(self.app, '_main_global_indices', None)
#                                         main_display_mode = str(getattr(self.app, "display_mode", "class") or "class").lower()
#                                         allow_main_rgb_update = main_display_mode in ("class", "shaded_class")
                                        
#                                         if rgb_ptr is not None and rgb_ptr.flags.writeable:
#                                             local = None
#                                             if gi is not None:
#                                                 # O(M) LOD mapping via modular arithmetic.
#                                                 # gi = arange(0, N, step), so a global index g is in LOD
#                                                 # iff g % step == 0, and its local index is g // step.
#                                                 # Avoids intersect1d which sorts the full 10M gi array
#                                                 # every GPU tick (~30-100ms) and was the primary bottleneck.
#                                                 _step = int(getattr(self.app, '_main_lod_step', 0) or 0)
#                                                 if _step > 1:
#                                                     _in_lod = (frame_fresh % _step) == 0
#                                                     local = (frame_fresh[_in_lod] // _step).astype(np.int64)
#                                                     # clip to valid range (guards against stale gi after file reload)
#                                                     local = local[local < len(gi)]
#                                                 elif _step == 1:
#                                                     # step=1 → every global index is in LOD, local == global
#                                                     local = frame_fresh[frame_fresh < len(gi)]
#                                                 else:
#                                                     # _main_lod_step not set (old actor) — safe fallback
#                                                     _, local, _ = np.intersect1d(
#                                                         gi, frame_fresh, assume_unique=True, return_indices=True
#                                                     )

#                                                 if len(local) > 0 and allow_main_rgb_update:
#                                                     # Guard: skip slot-0 RGB writes outside class/shaded_class modes.
#                                                     rgb_ptr[local] = color
#                                             else:
#                                                 # No LOD (Full points): direct indexing
#                                                 local = frame_fresh
#                                                 if allow_main_rgb_update:
#                                                     # Guard: skip slot-0 RGB writes outside class/shaded_class modes.
#                                                     rgb_ptr[local] = color

#                                             # vtk_ca.Modified() intentionally omitted during drag.
#                                             # It triggers a full 30 MB CPU→GPU buffer upload (~150 ms)
#                                             # on every GPU tick. Deferring it to mouse-release
#                                             # (guarantee_main_view_visual_refresh) keeps each tick at
#                                             # ~1 ms. The paint overlay provides live visual feedback.

#                                             if local is not None and len(local) > 0:
#                                                 if class_ptr is not None and len(class_ptr) > local[-1]:
#                                                     class_ptr[local] = class_ptr.dtype.type(to_class)
#                                                 elif mesh is not None:
#                                                     class_vtk_arr = mesh.GetPointData().GetArray("Classification")
#                                                     if class_vtk_arr is not None:
#                                                         from vtkmodules.util import numpy_support
#                                                         cls_np = numpy_support.vtk_to_numpy(class_vtk_arr)
#                                                         cls_np[local] = cls_np.dtype.type(to_class)
#                                                         # class_vtk_arr.Modified() deferred to release
#                                         # ✅ CUT SECTION REAL-TIME UPDATE
#                                         if is_cut_section:
#                                             try:
#                                                 cut_ctrl = self.app.cut_section_controller
#                                                 cut_actor = cut_ctrl.cut_vtk.actors.get("_cut_section_unified")
#                                                 if cut_actor:
#                                                     # Inject classification into cut actor's mesh/ptr
#                                                     c_ptr = getattr(cut_actor, '_naksha_section_class', None)
#                                                     if c_ptr is not None:
#                                                         # Resolve which indices in the cut were hit
#                                                         # (This is a bit slow for real-time but small enough for cut sections)
#                                                         cut_idx = cut_ctrl._cut_index_map
#                                                         # find which 'fresh' indices are in 'cut_idx'
#                                                         # Actually, it's easier to just re-read the whole thing if it's small
#                                                         # but for real-time we want to be fast.
#                                                         # ✅ OPTIMIZED: Use intersect1d instead of isin (2.3x faster)
#                                                         _, _, mask_in_cut = np.intersect1d(
#                                                             frame_fresh, cut_idx, 
#                                                             return_indices=True, 
#                                                             assume_unique=True
#                                                         )
                                                        
#                                                         if mask_in_cut.size > 0:
#                                                             c_ptr[mask_in_cut] = c_ptr.dtype.type(to_class)
                                                            
#                                                             # Update colors too
#                                                             rgb_ptr = getattr(cut_actor, '_naksha_rgb_ptr', None)
#                                                             if rgb_ptr is not None:
#                                                                  rgb_ptr[mask_in_cut] = color
                                                                 
#                                                             # ✅ OPTIMIZED: Only sync uniforms if palette state changed
#                                                             ctx = getattr(cut_actor, '_naksha_shader_ctx', None)
#                                                             if ctx:
#                                                                  last_gen = getattr(cut_actor, '_last_uniform_gen', -1)
#                                                                  if ctx._generation != last_gen:
#                                                                      _bsz = float(getattr(self.app, 'point_size', 2.5))
#                                                                      ctx.force_reload()
#                                                                      ctx.load_from_palette(palette, 0, _bsz)
#                                                                      _push_uniforms_direct(cut_actor, ctx)
#                                                                      cut_actor._last_uniform_gen = ctx._generation
                                                             
#                                                             _mark_actor_dirty(cut_actor)
#                                                             self._safe_render_pyvista(cut_ctrl.cut_vtk)
#                                             except Exception as _ce:
#                                                 print(f"⚠️ Real-time cut refresh failed: {_ce}")
                                  
#                                         # ✅ MAIN VIEW ONLY during drag — sections update on release
#                                         if (main_display_mode == "shaded_class"
#                                                 and _shading_mesh_exists(self.app)):
#                                             try:
#                                                 from gui.shading_display import refresh_shaded_after_classification_fast
#                                                 acc_mask = self._brush_accumulated_mask
#                                                 refresh_shaded_after_classification_fast(self.app, changed_mask=acc_mask)
#                                             except Exception as _se:
#                                                 print(f"⚠️ Brush shading refresh: {_se}")
#                                         elif main_display_mode == "class":
#                                             # Append painted XYZ to the overlay (tiny actor, zero main-buffer upload).
#                                             # Then fire a direct render bypassing classify-streak coalescing.
#                                             # The main actor uses its cached GPU buffer → render costs ~1 ms.
#                                             self._append_brush_paint_overlay(frame_fresh)
#                                             _mgr = getattr(self.app, 'gpu_render_manager', None)
#                                             _orig = getattr(_mgr, '_original_render', None) if _mgr else None
#                                             if _orig is not None:
#                                                 try:
#                                                     _orig()
#                                                 except Exception:
#                                                     pass
#                                             else:
#                                                 self._safe_render_pyvista(self.app.vtk_widget)

#                                 except Exception as e:
#                                     print(f"⚠️ Brush GPU inject: {e}")

#                                 self._brush_frame_chunks = []
#                                 self._brush_last_render_time = now
#                                 self._brush_needs_render = False

#                         self._last_brush_center = center
#                         self._brush_stroke_positions.append(center)

#                     else:
#                         # ✅ FIX 2: This section now works correctly because 'center' is already projected for cut section
#                         # CROSS-SECTION / CUT accumulation
#                         try:
#                             if (
#                                 getattr(self, "_brush_section_local_mask", None) is None or
#                                 getattr(self, "_brush_section_pts2d", None) is None or
#                                 getattr(self, "_brush_section_indices", None) is None
#                             ):
#                                 pass  # Skip if buffers not initialized
#                             else:
#                                 # ✅ UNIFIED PERFORMANCE: Segment-based spatial query
#                                 # center is already projected for Cut Section at this point.
#                                 u, z = float(center[0]), float(center[1])
#                                 prev_uv = getattr(self, "_last_brush_center_uv", None) or (u, z)
#                                 u1, z1 = prev_uv
#                                 u2, z2 = u, z
                                
#                                 grid = getattr(self, "_brush_section_grid", None)
#                                 if grid and grid.valid:
#                                     # Query the bounding box of the whole stroke segment
#                                     u_min, u_max = min(u1, u2) - radius_view, max(u1, u2) + radius_view
#                                     z_min, z_max = min(z1, z2) - radius_view, max(z1, z2) + radius_view
                                    
#                                     hit = grid.query_rectangle_minmax(u_min, z_min, u_max, z_max)
#                                     if len(hit) > 0:
#                                         # Distance-to-segment filter (vectorized)
#                                         pts2d = self._brush_section_pts2d
#                                         du, dz = u2 - u1, z2 - z1
#                                         l2 = du*du + dz*dz
#                                         sub_pts = pts2d[hit]
                                        
#                                         if l2 < 1e-9:
#                                             # Stationary brush
#                                             dist_sq = (sub_pts[:, 0] - u1)**2 + (sub_pts[:, 1] - z1)**2
#                                         else:
#                                             # Projection of points onto the line segment
#                                             t = np.clip(((sub_pts[:, 0] - u1) * du + (sub_pts[:, 1] - z1) * dz) / l2, 0, 1)
#                                             proj_u, proj_z = u1 + t * du, z1 + t * dz
#                                             dist_sq = (sub_pts[:, 0] - proj_u)**2 + (sub_pts[:, 1] - proj_z)**2
                                        
#                                         fresh_local = hit[dist_sq <= (radius_view * radius_view)]
                                        
#                                         if len(fresh_local) > 0:
#                                             # Filter already-done in this stroke
#                                             fresh_local = fresh_local[~self._brush_section_local_mask[fresh_local]]
                                            
#                                             if len(fresh_local) > 0:
#                                                 # ✅ CLASS PROTECTION & VISIBILITY (Section View)
#                                                 global_indices = self._brush_section_indices[fresh_local]
#                                                 classes = self.app.data['classification']
#                                                 from_classes = getattr(self.app, "from_classes", None)
#                                                 visible_classes = getattr(self, "_brush_visible_classes", None)
                                                
#                                                 # Skip if all classes hidden
#                                                 if visible_classes == []:
#                                                     return

#                                                 # Filter logic (vectorized)
#                                                 keep_mask = np.ones(len(global_indices), dtype=bool)
#                                                 point_classes = classes[global_indices]
                                                
#                                                 if from_classes:
#                                                     fc_arr = np.asarray(from_classes, dtype=classes.dtype)
#                                                     keep_mask &= np.isin(point_classes, fc_arr)
                                                
#                                                 if visible_classes is not None:
#                                                     vis_arr = np.asarray(visible_classes, dtype=classes.dtype)
#                                                     keep_mask &= np.isin(point_classes, vis_arr)
                                                
#                                                 if not np.any(keep_mask):
#                                                     return
                                                
#                                                 # Apply filters
#                                                 fresh_local = fresh_local[keep_mask]
#                                                 global_indices = global_indices[keep_mask]
                                                
#                                                 # 1. Unique and capture for undo
#                                                 fresh_local = np.unique(fresh_local)
#                                                 global_indices = self._brush_section_indices[fresh_local]
                                                
#                                                 if not hasattr(self, "_brush_indices_arrays") or self._brush_indices_arrays is None:
#                                                     self._brush_indices_arrays = []
#                                                     self._brush_old_classes_arrays = []
                                                
#                                                 self._brush_old_classes_arrays.append(classes[global_indices].copy())
#                                                 self._brush_indices_arrays.append(global_indices.copy())

#                                                 # 2. CPU classify immediately
#                                                 classes[global_indices] = to_class
#                                                 self._brush_section_local_mask[fresh_local] = True

#                                                 # 🖌️ Live brush stroke total → top bar center
#                                                 try:
#                                                     self.app._update_classify_count_display(
#                                                         int(np.count_nonzero(self._brush_section_local_mask))
#                                                     )
#                                                 except Exception:
#                                                     pass

#                                 self._last_brush_center_uv = (u, z)

#                                 # ── REAL-TIME GPU INJECTION (Phase 2) ──────────
#                                 if self._brush_section_local_mask is not None and self._brush_section_local_mask.any():
#                                     v_idx = self._get_view_index_from_interactor()
                                    
#                                     # ✅ Support Cut Section real-time poke
#                                     if v_idx is None and is_cut_section:
#                                         sw = self.app.cut_section_controller.cut_vtk
#                                         actor = sw.actors.get("_cut_section_unified")
#                                     elif v_idx is not None:
#                                         actor_name = f"_section_{v_idx}_unified"
#                                         sw = self.app.section_vtks.get(v_idx)
#                                         actor = sw.actors.get(actor_name) if sw else None
#                                     else:
#                                         actor = None
#                                         sw = None

#                                     if actor and hasattr(actor, '_naksha_rgb_ptr'):
#                                             # Only poke if it's been long enough since last poke
#                                             now = time.time()
#                                             if (now - getattr(self, '_last_sec_poke', 0)) > 0.033:
#                                                 rgb_ptr = actor._naksha_rgb_ptr
#                                                 if rgb_ptr.flags.writeable:
#                                                     # Get color for target class
#                                                     palette = self._get_active_palette()
#                                                     entry = palette.get(int(to_class), {"color": (255, 255, 0)})
#                                                     color = entry.get("color", (255, 255, 0)) if entry.get("show", True) else (0, 0, 0)
                                                    
#                                                     # Get local indices that are set in the mask
#                                                     local_hit = np.flatnonzero(self._brush_section_local_mask)
#                                                     rgb_ptr[local_hit] = color
                                                    
#                                                     # Update VTK
#                                                     v_arr = getattr(actor, '_naksha_vtk_array', None)
#                                                     if v_arr:
#                                                         v_arr.Modified()
                                                    
#                                                     # Render
#                                                     self._safe_render_pyvista(sw)
#                                                     self._last_sec_poke = now

#                         except Exception as e:
#                             print(f"⚠️ Section/Cut brush real-time update failed: {e}")

#             # ✅ Polygon preview
#             elif tool == "polygon":
#                 if is_cut_section:
#                     self._draw_polygon_preview_cut((float(P2[0]), float(P2[1]), float(P2[2])))
#                 else:
#                     self._draw_polygon_preview(self._get_view_coordinates(P2))
#                 if vtk_widget and (current_time - self._last_render_time) > preview_dt:
#                     self._safe_render(vtk_widget)
#                     self._last_render_time = current_time

#             # ✅ Freehand preview
#             elif tool == "freehand" and self.is_drawing_freehand:
#                 u, v = self._get_view_coordinates(P2)

#                 if len(self.drawing_points) == 0:
#                     self.drawing_points.append((u, v))
#                     self._last_freehand_display_pos = (dx, dy)
#                 else:
#                     # ✅ FIX: Pixel-based threshold for smooth drawing at all zoom levels
#                     last_disp = getattr(self, "_last_freehand_display_pos", (0.0, 0.0))
#                     pixel_dist = np.sqrt((dx - last_disp[0])**2 + (dy - last_disp[1])**2)
                    
#                     # Add point if mouse moved > 3 pixels
#                     if pixel_dist > 3.0:
#                         self.drawing_points.append((u, v))
#                         self._last_freehand_display_pos = (dx, dy)

#                 if is_cut_section:
#                     x2, y2 = self.interactor.GetEventPosition()
#                     if not hasattr(self, "drawing_points_display_cut"):
#                         self.drawing_points_display_cut = []
#                     if len(self.drawing_points_display_cut) == 0:
#                         self.drawing_points_display_cut.append((x2, y2))
#                     else:
#                         last_x, last_y = self.drawing_points_display_cut[-1]
#                         pixel_dist = np.sqrt((x2 - last_x) ** 2 + (y2 - last_y) ** 2)
#                         if pixel_dist > 2.0:
#                             self.drawing_points_display_cut.append((x2, y2))
#                     self._draw_freehand_preview_cut()
#                 else:
#                     self._draw_freehand_preview()

#                 if vtk_widget and (current_time - self._last_render_time) > preview_dt:
#                     self._safe_render(vtk_widget)
#                     self._last_render_time = current_time

#             # ✅ SAFE PAN: Custom implementation — no VTK C++ Render()
#             self._do_safe_pan()

#     # ──────────────────────────────────────────────────────────────────────────
#     # Task-3: BrushQueryWorker result slot (runs on main thread via Qt queued)
#     # ──────────────────────────────────────────────────────────────────────────
#     def _on_brush_worker_result(self, indices: object, to_class: int) -> None:
#         """
#         Receive KDTree query results from the background worker.

#         Called on the MAIN thread (Qt auto-queued connection).
#         Applies any newly found points that the synchronous grid may have missed.
#         """
#         try:
#             hit = indices   # 1-D int64 ndarray
#             if hit is None or len(hit) == 0:
#                 return

#             if not self.is_dragging:
#                 return   # stroke already released — discard stale results

#             if not hasattr(self, '_brush_accumulated_mask') or self._brush_accumulated_mask is None:
#                 return

#             # Discard results from a previous stroke's worker (to_class mismatch)
#             if to_class != getattr(self, '_brush_current_to_class', None):
#                 return

#             classes = self.app.data.get("classification")
#             if classes is None:
#                 return

#             # Only process points not yet accumulated (idempotent)
#             fresh = hit[~self._brush_accumulated_mask[hit]]
#             if len(fresh) == 0:
#                 return

#             from_classes = getattr(self.app, "from_classes", None)
#             visible_classes = getattr(self, "_brush_visible_classes", None)
#             visible_classes_arr = None

#             if visible_classes == []:
#                 return

#             if visible_classes is not None:
#                 visible_classes_arr = np.asarray(visible_classes, dtype=classes.dtype)

#             if from_classes:
#                 fc_arr = np.asarray(from_classes, dtype=classes.dtype)
#                 if visible_classes_arr is not None:
#                     effective_fc = fc_arr[np.isin(fc_arr, visible_classes_arr)]
#                     if len(effective_fc) == 0:
#                         return
#                     fc_mask = np.isin(classes[fresh], effective_fc)
#                 else:
#                     fc_mask = np.isin(classes[fresh], fc_arr)
#                 fresh = fresh[fc_mask]
#                 if len(fresh) == 0:
#                     return
#             elif visible_classes_arr is not None:
#                 vis_mask = np.isin(classes[fresh], visible_classes_arr)
#                 fresh = fresh[vis_mask]
#                 if len(fresh) == 0:
#                     return

#             # Save old classes for undo (Unified array-based)
#             old_cls = classes[fresh].copy()
            
#             if not hasattr(self, "_brush_indices_arrays") or self._brush_indices_arrays is None:
#                 self._brush_indices_arrays = []
#                 self._brush_old_classes_arrays = []
            
#             self._brush_indices_arrays.append(fresh.copy())
#             self._brush_old_classes_arrays.append(old_cls)

#             # CPU classify
#             classes[fresh] = classes.dtype.type(to_class)
#             self._brush_accumulated_mask[fresh] = True
#             self._brush_frame_chunks.append(fresh)
#             self._brush_needs_render = True

#             # 🖌️ Live brush stroke total → top bar center
#             try:
#                 self.app._update_classify_count_display(
#                     int(np.count_nonzero(self._brush_accumulated_mask))
#                 )
#             except Exception:
#                 pass

#         except Exception as e:
#             print(f"⚠️ _on_brush_worker_result: {e}")

#     def on_left_press(self, obj, evt):
#         """✅ Start drawing - sets P1 for preview rendering (FIXED for above/below line snapping)"""

#         if not hasattr(self, 'app') or self.app is None:
#             return
#         if not hasattr(self, 'interactor') or self.interactor is None:
#             return

#         active_tool = getattr(self.app, "active_classify_tool", None)
#         cut_controller = getattr(self.app, "cut_section_controller", None)
#         cut_owns_left_click = bool(
#             cut_controller is not None
#             and hasattr(cut_controller, "owns_cross_section_left_click")
#             and cut_controller.owns_cross_section_left_click()
#         )
#         # A held Pan shortcut may borrow left click only while classification
#         # and cut placement are both idle.
#         if (
#             not cut_owns_left_click
#             and getattr(self.app, "_left_pan_shortcut_active", False)
#             and active_tool is None
#         ):
#             self._is_panning = True
#             self._last_pan_pos = self.interactor.GetEventPosition()
#             self._hide_preview_overlay_for_pan()
#             try:
#                 if hasattr(obj, "AbortFlagOn"):
#                     obj.AbortFlagOn()
#                 elif hasattr(obj, "SetAbortFlag"):
#                     obj.SetAbortFlag(1)
#             except Exception:
#                 pass
#             return
#         self._stop_deferred_left_release_watch()
#         self._hide_preview_overlay_for_pan()

#         self._check_tool_changed()
#         tool = getattr(self.app, "active_classify_tool", None)
#         if tool in ("above_line", "below_line", "parallel_line"):
#             self.app._classification_preview_active = True
#         if getattr(self, "click_to_finalize", False) and tool in ("above_line", "below_line", "parallel_line"):
#             # The first tap already locked P1. Resolve P2 from this second
#             # press and finalize immediately; waiting for its release allowed
#             # very fast tap-tap input to fall back into drag/release behavior.
#             self._press_pos = self.interactor.GetEventPosition()
#             self._last_release_time = 0.0
#             self._last_render_time = 0.0
#             self._last_mouse_move_time = 0.0
#             try:
#                 dx, dy = self._get_display_point()
#                 p2 = self._display_to_world_fast(dx, dy)
#                 self._last_line_preview_P2 = p2

#                 # Paint the exact final line once before classification. This
#                 # makes a rapid second tap visually deterministic even when no
#                 # hover/mouse-move frame occurred between the two taps.
#                 vtk_widget = self._get_active_vtk_widget()
#                 is_cut_section = bool(
#                     hasattr(self.app, "cut_section_controller")
#                     and vtk_widget
#                     == getattr(self.app.cut_section_controller, "cut_vtk", None)
#                 )
#                 if is_cut_section:
#                     self._draw_line_preview_cut(self.P1, p2)
#                 else:
#                     self._draw_temp_line(self.P1, p2)
#                 if vtk_widget is not None:
#                     self._safe_render(vtk_widget)
#             except Exception:
#                 # The tap finalizer has its own current-cursor fallback.
#                 pass

#             self._do_line_classify_tap()
#             try:
#                 if hasattr(obj, "AbortFlagOn"):
#                     obj.AbortFlagOn()
#                 elif hasattr(obj, "SetAbortFlag"):
#                     obj.SetAbortFlag(1)
#             except Exception:
#                 pass
#             return

#         self._press_pos = self.interactor.GetEventPosition()
#         self._line_press_max_move_px = 0.0

#         # ════════════════════════════════════════════════════════════════
#         # ✅ CRITICAL: Cancel any pending deferred rebuild immediately
#         # This prevents lag when user starts a new classification quickly
#         # ════════════════════════════════════════════════════════════════
#         try:
#             if hasattr(self.app, '_optimized_refresh') and self.app._optimized_refresh:
#                 optimizer = self.app._optimized_refresh
#                 # STOP TIMER instantly
#                 if hasattr(optimizer, '_deferred_rebuild_timer'):
#                     optimizer._deferred_rebuild_timer.stop()
                
#                 # 🚀 OPTIMIZATION: Hide heavy SNT text during interaction
#                 self._suppress_snt_text(True)
#                 # We do NOT clear data, so we can resume later if needed
#         except Exception:
#             pass

#         is_cut_interaction = False
#         vtk_widget = self._get_active_vtk_widget()

#         if hasattr(self.app, 'cut_section_controller'):
#             cut_vtk = getattr(self.app.cut_section_controller, 'cut_vtk', None)
#             if cut_vtk and vtk_widget == cut_vtk:
#                 is_cut_interaction = True

#         if getattr(self.app, "active_mode", None) == "cut":
#             if not is_cut_interaction:
#                 if hasattr(self.app, 'statusBar'):
#                     self.app.statusBar().showMessage(
#                         "⚠️ Cut Section is active - classifying in Main View",
#                         2000
#                     )
#             else:
#                 print("🔧 Interaction detected inside Cut Section — Allowing classification.")

#         if self._is_main_view():
#             self.app.active_classify_target = "main"
#         elif is_cut_interaction:
#             self.app.active_classify_target = "cut"
#         else:
#             self.app.active_classify_target = "section"

#         # When cross-section tool is active, a click in the section view navigates
#         # the main view to the corresponding world XY (MicroStation-style locate).
#         if not self._is_main_view() and not is_cut_interaction:
#             if getattr(self.app, 'cross_section_active', False) and tool is None:
#                 sx, sy = self.interactor.GetEventPosition()
#                 self._navigate_main_to_section_click(sx, sy)
#                 return

#         if not is_cut_interaction:
#             v_idx = self._get_view_index_from_interactor()
#             if v_idx is not None:
#                 try:
#                     self.app.section_controller.active_view = v_idx
#                     self.app.section_controller.current_vtk = self.app.section_vtks.get(v_idx)
#                 except Exception:
#                     pass

#         tool = getattr(self.app, "active_classify_tool", None)
#         self._gesture_tool = tool

#         if tool in ("above_line", "below_line", "parallel_line") and self._is_main_view():

#             if hasattr(self.app, "statusBar"):
#                 self.app.statusBar().showMessage(
#                     "❌ Line classification tools work only in cross-section views",
#                     3000
#                 )

#             # Let camera interaction continue, but STOP classification
#             try:
#                 self.style.OnLeftButtonDown()
#             except Exception:
#                 pass

#             return

#         if tool is None:
#             try:
#                 return self.style.OnLeftButtonDown()
#             except Exception:
#                 return

#         # Abort C++ handler during classification dragging/drawing (unless in main view line snapping)
#         if tool not in ("above_line", "below_line", "parallel_line") or not self._is_main_view():
#             try:
#                 if hasattr(obj, "AbortFlagOn"):
#                     obj.AbortFlagOn()
#                 elif hasattr(obj, "SetAbortFlag"):
#                     obj.SetAbortFlag(1)
#             except Exception:
#                 pass

#         # ✅ IMPORTANT: clear stored preview end when starting a new above/below line
#         if tool in ("above_line", "below_line", "parallel_line"):
#             self._last_line_preview_P2 = None

#         self.from_classes = getattr(self.app, "from_classes", None)
#         self.to_class = getattr(self.app, "to_class", None)
#         x, y = self.interactor.GetEventPosition()

#         try:
#             if tool in ("above_line", "below_line", "parallel_line") and (not self._is_main_view()):
#                 dx, dy = self._get_display_point()
#                 pt = self._display_to_world_fast(dx, dy)
#             elif tool == "polygon":
#                 # FIX: _pick_world_point() uses VTK's cell/point picker which snaps
#                 # to the nearest rendered point cloud point — causing the magnetic effect.
#                 # _display_to_world_fast() does pure display→world math with no snapping,
#                 # matching what the live preview line already shows during mouse move.
#                 dx, dy = self._get_display_point()
#                 pt = self._display_to_world_fast(dx, dy)
#             else:
#                 pt = self._pick_world_point(x, y)
#         except Exception:
#             return
        
#         P = self._get_view_coordinates(pt)

#         if tool == "rectangle" and is_cut_interaction:
#             self.P1_display_cut = (x, y)

#         if tool == "polygon":
#             self.drawing_points.append(P)
#             if is_cut_interaction:
#                 if not hasattr(self, "drawing_points_display_cut"):
#                     self.drawing_points_display_cut = []
#                 if not hasattr(self, "drawing_points_world_cut"):
#                     self.drawing_points_world_cut = []
#                 self.drawing_points_display_cut.append((float(x), float(y)))
#                 self.drawing_points_world_cut.append((float(pt[0]), float(pt[1]), float(pt[2])))
#                 self._draw_polygon_preview_cut()
#             else:
#                 self._draw_polygon_preview()
#             self._safe_render_interactor()
#             print(f"{'='*60}\n")
#             return

#         if tool == "freehand":
#             self.is_drawing_freehand = True
#             self.drawing_points = []
#             if is_cut_interaction:
#                 self.drawing_points_display_cut = [(x, y)]
#             try:
#                 # ✅ FIX: Use non-snapping projection for freehand start
#                 pt2 = self._display_to_world_fast(x, y)
#                 P2 = self._get_view_coordinates(pt2)
#                 self.drawing_points.append(P2)
#                 self._last_freehand_display_pos = (float(x), float(y))
#             except Exception:
#                 pass
#             print(f"{'='*60}\n")
#             return

#         # self.P1 = pt
#         # self.app._suppress_section_refresh = True
#         # self.is_dragging = True
#         # # ❌ REMOVED: self.app._suppress_section_refresh = True
#         # # ✅ This flag should ONLY be set for brush tool, not for rectangle/other tools
#         self.P1 = pt
#         self.app._suppress_section_refresh = True
#         self.is_dragging = True
#         # Clear release debounce so first-tap release of this new gesture is never blocked
#         # by the previous classification's release timestamp.
#         self._last_release_time = 0.0
#         # ✅ FIX B — reset throttles so first preview frame of this
#         # new classification is never skipped by the 16 ms guard.
#         self._last_render_time     = 0.0
#         self._last_mouse_move_time = 0.0
#         # ❌ REMOVED: self.app._suppress_section_refresh = Tru

#         # ⚡ Fix 11 — Cancel any pending deferred main-view render and enter
#         # streak mode immediately on press. Without this, a render scheduled
#         # by the PREVIOUS classify's request_render could fire mid-drag
#         # (~100 ms after previous release) and synchronously block the
#         # current drag for 20-30 ms on the 39 M-point main view. That race
#         # was the source of the "sometimes a classify is slow" pattern:
#         # most clicks fall outside the bad timing window, but ones that
#         # happen ~100-300 ms after the previous release hit the firing
#         # timer right when the user starts dragging.
#         try:
#             self.app._last_classify_ts = time.time()
#             _mgr = getattr(self.app, "gpu_render_manager", None)
#             if _mgr is not None:
#                 _timer = getattr(_mgr, "render_timer", None)
#                 if _timer is not None:
#                     _timer.stop()
#                 _mgr.pending_render = False
#         except Exception:
#             pass

#         # ✅ MAIN VIEW brush init
#         if tool == "brush" and self._is_main_view():
#             xyz_now = self.app.data.get("xyz")
#             index_stale = (
#                 getattr(self, "_main_grid_index", None) is None
#                 or getattr(self, '_brush_spatial_index_xyz_id', None) != id(xyz_now)
#             )
#             if index_stale:
#                 # Wait for background pre-warm (normally a near-zero tail-wait).
#                 # Timeout guards against a hung thread — after 10 s we fall through
#                 # and let the synchronous build run instead (with its own exception guard).
#                 _pt = getattr(self, '_prebuild_thread', None)
#                 if _pt is not None and _pt.is_alive():
#                     _pt.join(timeout=10.0)
#                 try:
#                     self._build_spatial_index_for_brush()
#                 except Exception as _e:
#                     print(f"⚠️ Brush spatial index build failed: {_e}")

#             import numpy as np
#             self._brush_accumulated_mask = np.zeros(len(self.app.data["xyz"]), dtype=bool)
#             self._brush_old_classes = {} # Legacy - keep for compat if needed elsewhere
#             self._brush_old_classes_arrays = []
#             self._brush_indices_arrays = []
#             self._brush_frame_chunks = []
#             self._brush_stroke_positions = []
#             self._last_brush_center = None
#             self._brush_render_counter = 0
#             self._brush_last_render_time = 0.0
#             self._brush_needs_render = False
#             self._brush_visible_classes = self._get_visible_classes_for_slot(0)
#             # Track current stroke's target class so _on_brush_worker_result can
#             # discard stale results from a previous stroke's still-running worker.
#             self._brush_current_to_class = getattr(self.app, "to_class", None)
#             # ✅ Only set suppress flag for BRUSH tool
#             self.app._suppress_section_refresh = True

#             # Create lightweight overlay actor for real-time paint preview during drag.
#             # The overlay avoids the 30 MB vtk_ca.Modified() GPU upload on every tick.
#             try:
#                 _tc_init  = self._brush_current_to_class
#                 if _tc_init is not None:
#                     _pal_init = self._get_active_palette()
#                     _e_init   = _pal_init.get(int(_tc_init), {})
#                     _rgb_init = (_e_init.get('color', (128, 128, 128))
#                                  if _e_init.get('show', True) else (128, 128, 128))
#                     self._create_brush_paint_overlay(_rgb_init)
#                 else:
#                     self._brush_overlay_actor = None
#             except Exception:
#                 self._brush_overlay_actor = None

#             # ── Task-3: start background KDTree worker (if not already running) ──
#             if not getattr(self, "_brush_worker_active", False):
#                 try:
#                     from gui.brush_worker import BrushQueryWorker
#                     from PySide6.QtCore import Qt
#                     w = BrushQueryWorker(parent=None)
#                     w.result_ready.connect(
#                         self._on_brush_worker_result, Qt.QueuedConnection
#                     )
#                     w.start()
#                     self._brush_worker = w
#                     self._brush_worker_active = True
#                 except Exception as _bwe:
#                     self._brush_worker = None
#                     self._brush_worker_active = False

#             # Disable ROI point overlay during brush drag; keep only the cursor.
#             self._roi_preview = None
                

#         if tool == "brush" and (not self._is_main_view()):
#             # ✅ Only set suppress flag for BRUSH tool
#             self.app._suppress_section_refresh = True
            
#             try:
#                 import numpy as np
    
#                 self._brush_section_indices = None
#                 self._brush_section_pts2d = None
#                 self._brush_section_local_mask = None
#                 self._last_brush_center_uv = None

#                 if is_cut_interaction:
#                     ctrl = getattr(self.app, "cut_section_controller", None)
#                     if ctrl is not None:
#                         cut_pts = getattr(ctrl, "cut_points", None)
#                         cut_idx = getattr(ctrl, "_cut_index_map", None)
#                         if cut_pts is not None and cut_idx is not None:
#                             from ..classification_tools import _project_to_cut_view
#                             idxs = np.asarray(cut_idx, dtype=np.int64)
#                             pts2d = _project_to_cut_view(self.app, cut_pts)
#                             self._brush_section_indices   = idxs
#                             self._brush_section_pts2d     = np.asarray(pts2d, dtype=np.float64)
#                             self._brush_section_local_mask = np.zeros(len(idxs), dtype=bool)
                            
#                             # ✅ GRID INIT (Cut Section)
#                             self._brush_section_grid = SpatialGridIndex(self._brush_section_pts2d)
#                             # Cut section is typically Slot 5 in this app
#                             self._brush_visible_classes = self._get_visible_classes_for_slot(5)
#                             self._brush_indices_arrays = []
#                             self._brush_old_classes_arrays = []
#                 else:
#                     # Cross-section brush init.
#                     # CRITICAL: idxs and pts2d MUST be in the same order.
#                     # _section_{v}_global_indices and section_{v}_points_transformed
#                     # are both stored in core-first order by build_section_unified_actor.
#                     # Using flatnonzero(combined_mask) for idxs but points_transformed for
#                     # pts3 is WRONG — flatnonzero gives scan-order, points_transformed is
#                     # core-first. They do not correspond element-by-element.
#                     v_idx = self._get_view_index_from_interactor()
#                     if v_idx is None:
#                         v_idx = getattr(self.app.section_controller, "active_view", 0)

#                     # PRIMARY: use pre-built aligned arrays (both core-first order)
#                     idxs = getattr(self.app, f"_section_{v_idx}_global_indices", None)
#                     pts3 = getattr(self.app, f"section_{v_idx}_points_transformed", None)

#                     if idxs is not None and pts3 is not None and len(idxs) == len(pts3):
#                         # Aligned: index i of idxs ↔ index i of pts3
#                         pass
#                     else:
#                         # FALLBACK: build core-first order manually from masks
#                         core_mask   = getattr(self.app, f"section_{v_idx}_core_mask",   None)
#                         buffer_mask = getattr(self.app, f"section_{v_idx}_buffer_mask", None)
#                         if core_mask is not None:
#                             core_idx = np.flatnonzero(core_mask)
#                             if buffer_mask is not None:
#                                 buf_idx = np.flatnonzero(buffer_mask & ~core_mask)
#                                 idxs = np.concatenate([core_idx, buf_idx])
#                             else:
#                                 idxs = core_idx
#                             pts3 = self.app.data["xyz"][idxs]   # aligned with idxs
#                         else:
#                             idxs = None
#                             pts3 = None

#                     if pts3 is not None and idxs is not None:
#                         view_mode = getattr(self.app, "cross_view_mode", "side")
#                         pts2d = pts3[:, [1, 2]] if view_mode == "front" else pts3[:, [0, 2]]
#                         self._brush_section_indices   = np.asarray(idxs, dtype=np.int64)
#                         self._brush_section_pts2d     = np.asarray(pts2d, dtype=np.float64)
#                         self._brush_section_local_mask = np.zeros(len(idxs), dtype=bool)
                        
#                         # ✅ GRID INIT: Build 2D spatial index for instant brush
#                         self._brush_section_grid = SpatialGridIndex(self._brush_section_pts2d)
                        
#                         # ✅ UNDO & VISIBILITY INIT (Section)
#                         v_idx = self._get_view_index_from_interactor()
#                         if v_idx is None:
#                             v_idx = getattr(self.app.section_controller, "active_view", 0)
                        
#                         self._brush_visible_classes = self._get_visible_classes_for_slot(v_idx + 1)
#                         self._brush_indices_arrays = []
#                         self._brush_old_classes_arrays = []
    
#             except Exception as e:
#                 print(f"⚠️ Section brush init skipped (safe): {e}")
                    
#     def on_left_release(self, obj, evt):
#         if getattr(self, "_is_panning", False):
#             self._is_panning = False
#             self._hide_preview_overlay_for_pan()
#             try:
#                 if hasattr(obj, "AbortFlagOn"):
#                     obj.AbortFlagOn()
#                 elif hasattr(obj, "SetAbortFlag"):
#                     obj.SetAbortFlag(1)
#             except Exception:
#                 pass
#             return

#         """
#         Execute classification on mouse release
#         ✅ Includes MAIN-VIEW path + fixed cross/cut section path
#         ✅ FIXED: Rectangle/Circle use EXACT same P2 as preview (no coordinate mismatch)
        
#         ✅ SENIOR REFACTOR:
#         - Brush tool accumulates strokes in memory and flushes ONCE on release.
#         - Unified coordinate projection for Cut Section vs Cross-Section views.
#         - Debounced and state-safe cleanup.
#         """
#         # 1. Safety checks
#         if not hasattr(self, 'app') or self.app is None:
#             return

#         # 🚀 OPTIMIZATION: Restore suppressed text
#         self._suppress_snt_text(False)

#         if not hasattr(self, 'style') or self.style is None:
#             try:
#                 if hasattr(self, 'style'):
#                     self.style.OnLeftButtonUp()
#             except Exception:
#                 pass
#             return

#         if getattr(self, "_suppress_release_observer", False):
#             return

#         if getattr(self, "_ignore_next_left_release", False):
#             self._ignore_next_left_release = False
#             self._last_release_time = 0.0
#             return

#         # 2. Stray-release guard for line tools: if the system fires
#         # LeftButtonReleaseEvent AFTER on_left_press already handled finalization
#         # (P1 reset, click_to_finalize cleared), skip early so the debounce
#         # timer is not polluted for the next interaction.
#         _tool_quick = getattr(self.app, "active_classify_tool", None)
#         if (_tool_quick in ("above_line", "below_line", "parallel_line") and
#                 self.P1 is None and
#                 not getattr(self, 'click_to_finalize', False) and
#                 not getattr(self, 'is_dragging', False)):
#             return

#         # 3. Debounce (20ms) — guards against accidental double-fires from a
#         # single physical click, but lets a fast user fire 30+ classifications
#         # per second (Microstation cadence). Raised back to 100ms = silently
#         # dropped clicks during streaks.
#         import time
#         current_time = time.time()
#         if hasattr(self, '_last_release_time'):
#             if current_time - self._last_release_time < 0.02:
#                 return
#         self._last_release_time = current_time

#         self._check_tool_changed()
#         tool = getattr(self.app, "active_classify_tool", None)
#         if tool in ("above_line", "below_line", "parallel_line"):
#             # Get current event position
#             x, y = self.interactor.GetEventPosition()
#             press_pos = getattr(self, "_press_pos", None)
#             if press_pos is not None:
#                 dist = ((x - press_pos[0])**2 + (y - press_pos[1])**2) ** 0.5
#                 max_move = max(
#                     float(dist),
#                     float(getattr(self, "_line_press_max_move_px", 0.0)),
#                 )
#                 if max_move <= 12.0 and not getattr(self, "click_to_finalize", False):
#                     # Drag is the default path. A near-zero-move release arms
#                     # the optional tap-to-finalize mode and keeps the preview
#                     # alive for the next interaction.
#                     self.click_to_finalize = True
#                     self._press_pos = None
#                     self._line_press_max_move_px = 0.0
#                     # Clear throttles so the very next hover after the first tap
#                     # paints the preview immediately instead of waiting a frame.
#                     self._last_mouse_move_time = 0.0
#                     self._last_render_time = 0.0
#                     print("👉 Click-to-finalize mode activated for", tool)
#                     return

#         from ..classification_tools import (
#             classify_above_line, classify_below_line,
#             classify_parallel_line_band,
#             classify_rectangle, classify_circle, classify_polygon,
#             classify_freehand, classify_brush, classify_point,
#             _get_cut_section_or_default, _get_visible_mask_from_viewport,
#             _apply_classification
#         )

#         # Polygon tool is vertex-by-vertex (left clicks add vertices),
#         # so left-button release must not flush state between clicks.
#         if getattr(self.app, "active_classify_tool", None) == "polygon":
#             try:
#                 self.style.OnLeftButtonUp()
#             except Exception:
#                 pass
#             return

#         # Abort C++ release handler if classification tool is active
#         if getattr(self.app, "active_classify_tool", None) is not None:
#             try:
#                 if hasattr(obj, "AbortFlagOn"):
#                     obj.AbortFlagOn()
#                 elif hasattr(obj, "SetAbortFlag"):
#                     obj.SetAbortFlag(1)
#             except Exception:
#                 pass

#         try:
#             tool = getattr(self.app, "active_classify_tool", None)
#             was_dragging = self.is_dragging
#             to_class = getattr(self.app, "to_class", None)
#             from_classes = getattr(self.app, "from_classes", None)

#             # ⚡ Streak stamp — single point covering EVERY classify path that
#             # comes through this release handler (brush, above/below line,
#             # rectangle, circle, polygon, freehand, point — in both main and
#             # cross-section/cut views). GPURenderManager.request_render reads
#             # this timestamp to coalesce main-view renders within a 1 s window
#             # so the 39 M-point main view does not block the click→release
#             # cycle. Branches that already stamp inside _apply_classification
#             # / _apply_mask_and_record are idempotent — re-stamping here just
#             # keeps the window fresh. Brush branches (A & B) bypass those
#             # helpers entirely, so this is the ONLY stamp they get.
#             if tool and (was_dragging or tool in ("polygon", "freehand")):
#                 self.app._last_classify_ts = current_time

#             # --- BRANCH A: MAIN VIEW BRUSH STROKE ---
#             if tool == "brush" and self._is_main_view() and was_dragging:
#                 undo_mask = None  # Will be set if points were classified
#                 if hasattr(self, '_brush_accumulated_mask') and np.any(self._brush_accumulated_mask):
#                     start = time.time()

#                     # ✅ MICROSTATION: Classification was already applied during drag.
#                     # Only push the undo entry here (no re-classification needed).
#                     # ✅ Lightning-fast array-based merge for undo
#                     if hasattr(self, '_brush_indices_arrays') and self._brush_indices_arrays:
#                         indices = np.concatenate(self._brush_indices_arrays)
#                         old_cls = np.concatenate(self._brush_old_classes_arrays)
                        
#                         # ✅ CRITICAL FIX: Ensure uniqueness to avoid undo failure
#                         # we MUST have exactly one entry in the mask per entry in old_cls.
#                         # We use the FIRST occurrence to keep the original class.
#                         if len(indices) > 0:
#                             _, first_idx = np.unique(indices, return_index=True)
#                             indices = indices[first_idx]
#                             old_cls = old_cls[first_idx]

#                         new_cls = np.full(len(indices), to_class, dtype=old_cls.dtype)

#                         undo_mask = np.zeros(len(self.app.data["xyz"]), dtype=bool)
#                         undo_mask[indices] = True

#                         self.app.undo_stack.append({
#                             "indices": indices.copy(),
#                             "old_classes": old_cls,
#                             "new_classes": new_cls,
#                         })
#                         self.app.redo_stack.clear()
#                         self.app._last_changed_mask = undo_mask
#                         self.app._last_changed_indices = indices.copy()

#                         # Point stats
#                         try:
#                             from gui.point_count_widget import refresh_point_statistics
#                             refresh_point_statistics(self.app)
#                         except Exception:
#                             pass

#                     _count = len(indices) if 'indices' in locals() else 0
#                     elapsed = (time.time() - start) * 1000
#                     print(f"✅ Main Brush stroke complete: {_count:,} points in {elapsed:.0f}ms")

#                     # 🖌️ Final exact stroke count → top bar center
#                     if _count > 0:
#                         try:
#                             self.app._update_classify_count_display(int(_count))
#                         except Exception:
#                             pass

#                 # ── Save mask BEFORE nulling — needed for shading refresh below ──
#                 _final_mask = self._brush_accumulated_mask

#                 # Remove paint overlay before the corrective render fires.
#                 # guarantee_main_view_visual_refresh (below) will call vtk_ca.Modified()
#                 # and do the one full GPU upload that shows all painted colours.
#                 self._destroy_brush_paint_overlay()

#                 # Clean up brush state
#                 self._brush_accumulated_mask = None
#                 self._brush_old_classes = {}
#                 self._brush_old_classes_arrays = []
#                 self._brush_indices_arrays = []
#                 self._brush_frame_chunks = []
#                 self._brush_stroke_positions = []
#                 self._last_brush_center = None
#                 self._brush_needs_render = False
#                 self._brush_visible_classes = None
#                 self.app._suppress_section_refresh = False

#                 # ── Task-2: destroy ROI preview actor ────────────────────────────
#                 try:
#                     roi = getattr(self, "_roi_preview", None)
#                     if roi is not None:
#                         roi.destroy()
#                         self._roi_preview = None
#                 except Exception:
#                     pass

#                 # ── Task-3: worker stays alive across strokes to avoid QThread
#                 # teardown race (destroying the Python wrapper while the C++ thread
#                 # is still mid-query on a 13M-point KDTree → segfault).
#                 # The worker is stopped only in cleanup() when the tool is deactivated.
#                 # Between strokes the queue is drained; stale results are discarded by
#                 # the `if not self.is_dragging: return` guard in _on_brush_worker_result.
#                 # ─────────────────────────────────────────────────────────────────────

#                 # In shaded_class mode keep OptimizedRefresh alive — shading mesh needs its own rebuild
#                 if getattr(self.app, "display_mode", "class") != "shaded_class":
#                     self.app._gpu_sync_done = True
#                     self.app._section_visibility_refresh_required = False
#                     try:
#                         from gui.unified_actor_manager import guarantee_main_view_visual_refresh
#                         guarantee_main_view_visual_refresh(
#                             self.app,
#                             changed_mask=_final_mask,
#                             to_class=to_class,
#                             reason="brush_release_finalize",
#                         )
#                     except Exception:
#                         pass
#                 elif _shading_mesh_exists(self.app):
#                     try:
#                         from gui.shading_display import refresh_shaded_after_classification_fast
#                         refresh_shaded_after_classification_fast(
#                             self.app,
#                             changed_mask=_final_mask   # use saved mask, not the now-None attribute
#                         )
#                     except Exception as _se:
#                         print(f"⚠️ Brush release shading refresh: {_se}")
#                 else:
#                     # No mesh yet — treat like class mode so GPU sync completes
#                     self.app._gpu_sync_done = True
#                     self.app._section_visibility_refresh_required = False
#                     try:
#                         from gui.unified_actor_manager import guarantee_main_view_visual_refresh
#                         guarantee_main_view_visual_refresh(
#                             self.app,
#                             changed_mask=_final_mask,
#                             to_class=to_class,
#                             reason="brush_release_fallback",
#                         )
#                     except Exception:
#                         pass
#                     print("⏭️ Brush release: skipping shading refresh — no mesh built yet")

#                  # ✅ Final: sync sections (deferred from drag) + render all
#                 try:
#                     # Update cross-sections with the full undo_mask
#                     if hasattr(self.app, 'section_vtks') and undo_mask is not None:
#                         from gui.unified_actor_manager import fast_cross_section_update, build_section_unified_actor
#                         for vi in self.app.section_vtks:
#                             try:
#                                 slot_idx = vi + 1
#                                 dialog = (
#                                     getattr(self.app, "display_mode_dialog", None)
#                                     or getattr(self.app, "display_dialog", None)
#                                 )
#                                 view_palette = {}
#                                 if dialog is not None and hasattr(dialog, "view_palettes"):
#                                     view_palette = dialog.view_palettes.get(slot_idx) or {}
#                                 if not view_palette and hasattr(self.app, "view_palettes"):
#                                     view_palette = self.app.view_palettes.get(slot_idx) or {}

#                                 ok = fast_cross_section_update(
#                                     self.app,
#                                     vi,
#                                     undo_mask,
#                                     palette=view_palette,
#                                     force_visibility_refresh=False,
#                                 )
#                                 if not ok:
#                                     border = float(getattr(self.app, "view_borders", {}).get(slot_idx, 0) or 0.0)
#                                     view_mode = getattr(self.app, "cross_view_mode", "front")
#                                     rebuilt = build_section_unified_actor(
#                                         self.app,
#                                         vi,
#                                         palette=view_palette,
#                                         border_percent=border,
#                                         view=view_mode,
#                                     )
#                                     if rebuilt is not None:
#                                         fast_cross_section_update(
#                                             self.app,
#                                             vi,
#                                             undo_mask,
#                                             palette=view_palette,
#                                             force_visibility_refresh=False,
#                                         )
#                             except Exception:
#                                 pass

#                     mode_for_main_render = str(getattr(self.app, "display_mode", "class") or "class").lower()
#                     # Guard: do not re-render slot-0 in non-class modes after brush finalize.
#                     if mode_for_main_render in ("class", "shaded_class"):
#                         self._safe_render_pyvista(self.app.vtk_widget)
#                     if hasattr(self.app, 'section_vtks'):
#                         for sw in self.app.section_vtks.values():
#                             if sw:
#                                 self._safe_render_pyvista(sw)
#                 except Exception:
#                     pass

#                 # ═══════════════════════════════════════════════════════════
#                 # ✅ FIX: Refresh cut section view after main view brush
#                 # 
#                 # Branch A returns early (before _refresh_all_views_after_classification),
#                 # so cross-sections get updated via fast_cross_section_update() above,
#                 # but the cut section was NEVER refreshed. This fixes that gap.
#                 # ═══════════════════════════════════════════════════════════
#                 try:
#                     if hasattr(self.app, 'cut_section_controller'):
#                         ctrl = self.app.cut_section_controller
#                         if (ctrl.is_cut_view_active 
#                                 and ctrl.cut_points is not None 
#                                 and ctrl._cut_index_map is not None
#                                 and ctrl.cut_vtk is not None):
#                             ctrl._refresh_cut_colors_fast()
#                             print(f"   ⚡ Cut section refreshed after main view brush")
#                 except Exception as e:
#                     print(f"   ⚠️ Cut section refresh failed: {e}")

#                 self.is_dragging = False
#                 try:
#                     self.style.OnLeftButtonUp()
#                 except Exception:
#                     pass
#                 return

#             # 3. Basic Validation
#             if (not was_dragging) and (tool not in ("freehand", "polygon")):
#                 print("⚠️ Release without drag - ignoring")
#                 try:
#                     self.style.OnLeftButtonUp()
#                 except Exception:
#                     pass
#                 return

#             if self.P1 is None and tool not in ("freehand", "polygon"):
#                 print("⚠️ P1 is None, cannot execute classification")
#                 return

#             x, y = self.interactor.GetEventPosition()
            
#             # 4. Resolve P2 (Endpoint) - FIXED TO MATCH PREVIEW
#             try:
#                 if tool in ("above_line", "below_line", "parallel_line") and (not self._is_main_view()):
#                     # Use stored preview P2 for line tools
#                     P2 = getattr(self, "_last_line_preview_P2", None)
#                     if P2 is None:
#                         dx, dy = self._get_display_point()
#                         P2 = self._display_to_world_fast(dx, dy)
#                 elif tool == "rectangle" and (not self._is_main_view()):
#                     # ✅ NEW: Use stored preview P2 for rectangle tool
#                     P2 = getattr(self, "_last_rectangle_preview_P2", None)
#                     if P2 is None:
#                         dx, dy = self._get_display_point()
#                         P2 = self._display_to_world_fast(dx, dy)
#                     print(f"🔍 Rectangle using preview P2: {P2}")
#                 elif tool == "circle" and (not self._is_main_view()):
#                     # ✅ NEW: Use stored preview P2 for circle tool
#                     P2 = getattr(self, "_last_circle_preview_P2", None)
#                     if P2 is None:
#                         dx, dy = self._get_display_point()
#                         P2 = self._display_to_world_fast(dx, dy)
#                 else:
#                     # Main view or point tool can use snapping
#                     P2 = self._pick_world_point(x, y)
#             except Exception as e:
#                 print(f"⚠️ Pick failed on release: {e}")
#                 return

#             # 5. Detect View Context
#             vtk_widget = self._get_active_vtk_widget()
#             is_cut_section = bool(
#                 hasattr(self.app, 'cut_section_controller') and
#                 vtk_widget == getattr(self.app.cut_section_controller, 'cut_vtk', None)
#             )

#             # --- BRANCH B: SECTION / CUT VIEW BRUSH STROKE ---
#             if tool == "brush" and (not self._is_main_view()) and was_dragging:
#                 try:
#                     idxs = getattr(self, "_brush_section_indices", None)
#                     pts2d = getattr(self, "_brush_section_pts2d", None)
#                     local_mask = getattr(self, "_brush_section_local_mask", None)

#                     if idxs is not None and pts2d is not None and local_mask is not None:
#                         idxs_arr = np.asarray(idxs)
#                         if idxs_arr.dtype == bool:
#                             idxs_arr = np.flatnonzero(idxs_arr)
#                         idxs_arr = idxs_arr.astype(np.int64, copy=False)

#                         # Final stamp at release position to close the gap
#                         radius_px = float(getattr(self.app, "brush_preview_px", 20.0))
#                         radius_view = float(self._pixel_radius_to_view_radius(vtk_widget, P2, radius_px))
#                         u2, z2 = self._get_view_coordinates(P2)
                        
#                         grid = getattr(self, "_brush_section_grid", None)
#                         if grid and grid.valid:
#                             if getattr(self.app, "brush_shape", "circle") == "rectangle":
#                                 hit = grid.query_rectangle(float(u2), float(z2), radius_view)
#                             else:
#                                 hit = grid.query_radius(float(u2), float(z2), radius_view)
#                             if len(hit) > 0:
#                                 local_mask[hit] = True
#                         else:
#                             # Fallback O(N)
#                             du, dz = pts2d[:, 0] - float(u2), pts2d[:, 1] - float(z2)
#                             if getattr(self.app, "brush_shape", "circle") == "rectangle":
#                                 local_mask |= (np.abs(du) <= radius_view) & (np.abs(dz) <= radius_view)
#                             else:
#                                 local_mask |= (du*du + dz*dz) <= (radius_view * radius_view)

#                         #     update_mask[idxs_arr[local_mask]] = True

#                         # self._refresh_all_views_after_classification(to_class)

#                         # ✅ Lightning-fast array-based merge for undo (matches Main View logic)
#                         if hasattr(self, '_brush_indices_arrays') and self._brush_indices_arrays:
#                             indices = np.concatenate(self._brush_indices_arrays)
#                             old_cls = np.concatenate(self._brush_old_classes_arrays)
                            
#                             # ✅ CRITICAL FIX: Ensure uniqueness to avoid undo failure
#                             if len(indices) > 0:
#                                 _, first_idx = np.unique(indices, return_index=True)
#                                 indices = indices[first_idx]
#                                 old_cls = old_cls[first_idx]

#                             new_cls = np.full(len(indices), to_class, dtype=old_cls.dtype)

#                             undo_mask = np.zeros(len(self.app.data["xyz"]), dtype=bool)
#                             undo_mask[indices] = True

#                             self.app.undo_stack.append({
#                                 "indices": indices.copy(),
#                                 "old_classes": old_cls,
#                                 "new_classes": new_cls,
#                             })
#                             self.app.redo_stack.clear()
#                             self.app._last_changed_mask = undo_mask
#                             self.app._last_changed_indices = indices.copy()

#                             # Point stats
#                             try:
#                                 from gui.point_count_widget import refresh_point_statistics
#                                 refresh_point_statistics(self.app)
#                             except Exception:
#                                 pass

#                             if hasattr(self.app, 'statusBar'):
#                                 self.app.statusBar().showMessage(
#                                     f"✅ {len(indices):,} points → class {to_class}", 3000)

#                         self._refresh_all_views_after_classification(to_class)
                        
#                         # ✅ CLEANUP SECTION BRUSH STATE
#                         self._brush_section_local_mask = None
#                         self._brush_old_classes_arrays = []
#                         self._brush_indices_arrays = []
#                         self._last_brush_center_uv = None
#                         return
                    
#                 except Exception as e:
#                     print(f"⚠️ Section brush failed (fallback): {e}")

#             # --- BRANCH C: MAIN VIEW (NON-BRUSH) ---
#             if self._is_main_view():
#                 if tool == "rectangle":
#                     u1, v1 = self._get_view_coordinates(self.P1)
#                     u2, v2 = self._get_view_coordinates(P2)

#                     # Clear preview BEFORE classification.
#                     # In Surface mode, classification triggers Surface rebuild immediately.
#                     # If preview is still visible during rebuild, Surface overlay restore can revive it.
#                     self._clear_all_previews()

#                     self._classify_rectangle_main((u1, v1), (u2, v2), to_class)

#                 elif tool == "circle":
#                     u1, v1 = self._get_view_coordinates(self.P1)
#                     u2, v2 = self._get_view_coordinates(P2)
#                     center = ((u1 + u2) / 2.0, (v1 + v2) / 2.0)
#                     radius = float(np.hypot(u2 - u1, v2 - v1)) / 2.0

#                     # Clear preview BEFORE classification for Surface mode safety.
#                     self._clear_all_previews()

#                     self._classify_circle_main(center, radius, to_class)

#                 elif tool == "freehand" and len(self.drawing_points) > 2:
#                     _smooth_fh = self._catmull_rom_smooth(self.drawing_points, steps=10)

#                     # Clear preview BEFORE classification for Surface mode safety.
#                     self._clear_all_previews()

#                     self._classify_polygon_main(_smooth_fh, to_class)

#                 elif tool == "point":
#                     u2, v2 = self._get_view_coordinates(P2)

#                     # Clear preview BEFORE classification for Surface mode safety.
#                     self._clear_all_previews()

#                     self._classify_point_main((u2, v2), getattr(self.app, "point_radius", 0.5), to_class)

#                 else:
#                     self._clear_all_previews()

#                 return

#             # --- BRANCH D: SECTION / CUT VIEW (NON-BRUSH) ---
#             # Resolve data context
#             #     mask, section_points = self._get_section_context_for_view(v_idx) if v_idx is not None else (None, None)

#             # section_indices, section_points = _get_cut_section_or_default(self.app, mask, section_points)

#             if is_cut_section:
#                 mask = getattr(self.app.section_controller, "last_mask", None)
#                 section_points = getattr(self.app, "section_points", None)
#             else:
#                 v_idx = self._get_view_index_from_interactor()
#                 if v_idx is not None:
#                     combined_mask = getattr(self.app, f"section_{v_idx}_combined_mask", None)
#                     section_points_transformed = getattr(self.app, f"section_{v_idx}_points_transformed", None)
#                     if combined_mask is not None and section_points_transformed is not None:
#                         mask = combined_mask
#                         section_points = section_points_transformed
#                     else:
#                         mask, section_points = self._get_section_context_for_view(v_idx)
#                 else:
#                     mask, section_points = None, None

#             section_indices, section_points = _get_cut_section_or_default(self.app, mask, section_points)
#             if section_indices is None or section_points is None:
#                 return

#             # Special case for Freehand completion
#             if tool == "freehand" and self.is_drawing_freehand:
#                 if len(self.drawing_points) >= 2:
#                     # ── SMOOTH: use Catmull-Rom polygon so the classified area
#                     # matches the smooth curved preview exactly. ────────────────
#                     smooth_pts = self._catmull_rom_smooth(self.drawing_points, steps=8)
#                     classify_freehand(self.app, smooth_pts, from_classes, to_class, mask, section_points, None)
#                     self._refresh_all_views_after_classification(to_class)
#                 return

#             # Geometric Tools (Line, Rect, Circle)
#             if is_cut_section:
#                 from ..classification_tools import _project_to_cut_view
#                 (u1, z1), (u2, z2) = _project_to_cut_view(self.app, np.vstack([self.P1, P2]))
#             else:
#                 u1, z1 = self._get_view_coordinates(self.P1)
#                 u2, z2 = self._get_view_coordinates(P2)

#             if tool == "above_line":
#                 classify_above_line(self.app, [(u1, z1), (u2, z2)], from_classes, to_class, mask, section_points, None)
#             elif tool == "below_line":
#                 classify_below_line(self.app, [(u1, z1), (u2, z2)], from_classes, to_class, mask, section_points, None)
#             elif tool == "parallel_line":
#                 classify_parallel_line_band(self.app, [(u1, z1), (u2, z2)], from_classes, to_class, mask, section_points, None)
#             elif tool == "rectangle":
#                 u_range = sorted([u1, u2])
#                 z_range = sorted([z1, z2])
#                 classify_rectangle(self.app, (u_range[0], u_range[1], z_range[0], z_range[1]), from_classes, to_class, mask, section_points, "disable_filter")
#             elif tool == "circle":
#                 center = ((u1 + u2) / 2, (z1 + z2) / 2)
#                 radius = float(np.hypot(u2 - u1, z2 - z1)) / 2.0
#                 classify_circle(self.app, center, radius, from_classes, to_class, mask, section_points, None)
#             elif tool == "point":
#                 classify_point(self.app, (u2, z2), getattr(self.app, "point_radius", 0.5), from_classes, to_class, mask, section_points, None)

#             # ✅ PATCH: Force cut section refresh only when the primary fast path
#             # did not already complete GPU sync for this classify operation.
#             if is_cut_section:
#                 try:
#                     ctrl = getattr(self.app, 'cut_section_controller', None)
#                     if (ctrl is not None
#                             and getattr(ctrl, 'is_cut_view_active', False)
#                             and ctrl._cut_index_map is not None
#                             and ctrl.cut_vtk is not None
#                             and (
#                                 not bool(getattr(self.app, "_gpu_sync_done", False))
#                                 or bool(getattr(self.app, "_section_visibility_refresh_required", False))
#                             )):
#                         ctrl._refresh_cut_colors_fast()
#                         self._safe_render_pyvista(ctrl.cut_vtk)
#                         print("   ⚡ Cut section explicit post-classify refresh (is_cut_section path)")
#                 except Exception as _cse:
#                     print(f"   ⚠️ Cut section post-classify refresh failed: {_cse}")

#             self._refresh_all_views_after_classification(to_class)

#         except Exception as e:
#             print(f"⚠️ Classification failed: {e}")
#             import traceback
#             traceback.print_exc()

#         finally:
#             self._stop_deferred_left_release_watch()
#             self._gesture_tool = None
#             # 1. CLEANUP STATE
#             self.is_dragging = False
#             self.P1 = None
#             self.click_to_finalize = False
#             self._press_pos = None
#             self.drawing_points = []
#             self.is_drawing_freehand = False
#             self._last_brush_center = None
#             self._is_panning = False
#             self._hide_preview_overlay_for_pan()
#             self._last_pan_pos = (0, 0) 
#             # Clear previews
#             self._last_rectangle_preview_P2 = None
#             self._last_circle_preview_P2 = None
#             self._last_line_preview_P2 = None

#             # Clean stroke buffers
#             for attr in ("_brush_accumulated_mask", "_brush_stroke_positions", 
#                         "_brush_section_indices", "_brush_section_pts2d", 
#                         "_brush_section_local_mask", "_last_brush_center_uv"):
#                 if hasattr(self, attr):
#                     try: delattr(self, attr)
#                     except: setattr(self, attr, None)

#             # _clear_all_previews() hides the preview actors AND calls
#             # _safe_render on the active widget internally (see line ~2387),
#             # so the preview visual is already gone by the time this returns.
#             # A second _safe_render here was redundant — it re-rendered the
#             # same widget for 2–5 ms with no visual change. Removed.
#             self._clear_all_previews()

#             # (removed) QApplication.processEvents() was synchronously
#             # draining the entire event queue (20–80 ms stall after fast
#             # bursts). Qt's event loop already dispatches the next mouse
#             # event automatically as soon as on_left_release returns — the
#             # synchronous drain is not needed and was hurting responsiveness.

#             # START DEFERRED TIMER (Non-blocking)
#             try:
#                 if hasattr(self.app, '_optimized_refresh') and self.app._optimized_refresh:
#                     optimizer = self.app._optimized_refresh
#                     if hasattr(optimizer, '_deferred_rebuild_data') and optimizer._deferred_rebuild_data:
#                         if hasattr(optimizer, '_deferred_rebuild_timer'):
#                             optimizer._deferred_rebuild_timer.stop()
#                             # 500ms delay ensures user has time to move mouse before heavy rebuild starts
#                             optimizer._deferred_rebuild_timer.start(500) 
#             except Exception as e:
#                 pass
            
#             try:
#                 self.style.OnLeftButtonUp()
#             except Exception:
#                 pass

#     def _ensure_target_class_visible(self, to_class):
#         """
#         ✅ FINAL SENIOR FIX
#         Auto-enable ONLY if user has NO filter active.
#         Never mutate filtered views.
#         """
#         try:
#             print(f"\n{'='*60}")
#             print(f"👁️ Checking target class {to_class} visibility")

#             dialog = getattr(self.app, 'display_mode_dialog', None)
#             if not dialog:
#                 print("   ⚠️ No display_mode_dialog found")
#                 return

#             # ✅ MAIN vs SECTION
#             active_view = getattr(self.app.section_controller, 'active_view', None)
#             target_slot = 0 if active_view is None else active_view + 1

#             print(f"   Active view: {active_view}, slot: {target_slot}")

#             dialog.slot_shows.setdefault(target_slot, {})
#             dialog.view_palettes.setdefault(target_slot, {})

#             view_palette = dialog.view_palettes.get(target_slot, {})
#             if not view_palette:
#                 print("   ℹ️ No palette - nothing to auto-enable")
#                 return

#             visible_count = sum(1 for v in view_palette.values() if v.get("show", False))
#             total_count = len(view_palette)

#             print(f"   📊 Visible: {visible_count}/{total_count} classes")

#             # 🔒 USER FILTER → NEVER AUTO-ENABLE
#             if 0 < visible_count < total_count:
#                 print("   🔒 User has filter - NOT auto-enabling")
#                 return

#             if view_palette.get(to_class, {}).get("show", False):
#                 print("   ✓ Already visible")
#                 return

#             # Enable only when safe
#             dialog.slot_shows[target_slot][int(to_class)] = True
#             dialog.view_palettes[target_slot][int(to_class)]["show"] = True

#             print(f"   ✅ Auto-enabled class {to_class} (no user filter)")

#         except Exception as e:
#             print(f"⚠️ _ensure_target_class_visible failed: {e}")
#             import traceback
#             traceback.print_exc()
            
#     def _do_line_classify_tap(self):
#         """
#         Finalize a click-to-click line classification directly from on_left_press.
#         Mirrors how interactor_slice calls finalize_section() directly in its press
#         handler — no cross-calling on_left_release, no debounce issues, no VTK
#         observer recursion.
#         """
#         import numpy as np
#         try:
#             from ..classification_tools import (
#                 classify_above_line, classify_below_line,
#                 classify_parallel_line_band,
#                 _get_cut_section_or_default,
#             )

#             tool = getattr(self.app, "active_classify_tool", None)
#             to_class = getattr(self.app, "to_class", None)
#             from_classes = getattr(self.app, "from_classes", None)

#             # P2: prefer the stored preview position so classification matches
#             # the visual line exactly; fall back to current cursor position.
#             P2 = getattr(self, "_last_line_preview_P2", None)
#             if P2 is None:
#                 try:
#                     dx, dy = self._get_display_point()
#                     P2 = self._display_to_world_fast(dx, dy)
#                 except Exception:
#                     pass

#             if self.P1 is None or P2 is None:
#                 print("⚠️ _do_line_classify_tap: P1 or P2 is None, skipping")
#                 return

#             # Detect view context
#             vtk_widget = self._get_active_vtk_widget()
#             is_cut_section = bool(
#                 hasattr(self.app, 'cut_section_controller') and
#                 vtk_widget == getattr(self.app.cut_section_controller, 'cut_vtk', None)
#             )

#             # Resolve section data (same logic as Branch D in on_left_release)
#             if is_cut_section:
#                 mask = getattr(self.app.section_controller, "last_mask", None)
#                 section_points = getattr(self.app, "section_points", None)
#             else:
#                 v_idx = self._get_view_index_from_interactor()
#                 if v_idx is not None:
#                     combined_mask = getattr(self.app, f"section_{v_idx}_combined_mask", None)
#                     section_points_transformed = getattr(self.app, f"section_{v_idx}_points_transformed", None)
#                     if combined_mask is not None and section_points_transformed is not None:
#                         mask = combined_mask
#                         section_points = section_points_transformed
#                     else:
#                         mask, section_points = self._get_section_context_for_view(v_idx)
#                 else:
#                     mask, section_points = None, None

#             section_indices, section_points = _get_cut_section_or_default(
#                 self.app, mask, section_points
#             )
#             if section_indices is None or section_points is None:
#                 print("⚠️ _do_line_classify_tap: no section data")
#                 return

#             # Project to view coordinates
#             if is_cut_section:
#                 from ..classification_tools import _project_to_cut_view
#                 (u1, z1), (u2, z2) = _project_to_cut_view(
#                     self.app, np.vstack([self.P1, P2])
#                 )
#             else:
#                 u1, z1 = self._get_view_coordinates(self.P1)
#                 u2, z2 = self._get_view_coordinates(P2)

#             if tool == "above_line":
#                 classify_above_line(
#                     self.app, [(u1, z1), (u2, z2)],
#                     from_classes, to_class, mask, section_points, None
#                 )
#             elif tool == "below_line":
#                 classify_below_line(
#                     self.app, [(u1, z1), (u2, z2)],
#                     from_classes, to_class, mask, section_points, None
#                 )
#             elif tool == "parallel_line":
#                 classify_parallel_line_band(
#                     self.app, [(u1, z1), (u2, z2)],
#                     from_classes, to_class, mask, section_points, None
#                 )

#             if is_cut_section:
#                 try:
#                     ctrl = getattr(self.app, 'cut_section_controller', None)
#                     if (ctrl is not None
#                             and getattr(ctrl, 'is_cut_view_active', False)
#                             and ctrl._cut_index_map is not None
#                             and ctrl.cut_vtk is not None
#                             and (not bool(getattr(self.app, "_gpu_sync_done", False))
#                                  or bool(getattr(self.app, "_section_visibility_refresh_required", False)))):
#                         ctrl._refresh_cut_colors_fast()
#                         self._safe_render_pyvista(ctrl.cut_vtk)
#                 except Exception:
#                     pass

#             self._refresh_all_views_after_classification(to_class)

#         except Exception as e:
#             print(f"⚠️ _do_line_classify_tap failed: {e}")
#             import traceback
#             traceback.print_exc()

#         finally:
#             # Reset drawing state — identical to the finally block in on_left_release.
#             # We explicitly notify VTK that the synthetic finalize-on-press path
#             # is complete, then ignore the one real LeftButtonReleaseEvent that
#             # still arrives when the user physically lets go of the mouse.
#             self._stop_deferred_left_release_watch()
#             self.is_dragging = False
#             self.P1 = None
#             self.click_to_finalize = False
#             self._press_pos = None
#             self._last_line_preview_P2 = None
#             self.app._classification_preview_active = False
#             self.app._suppress_section_refresh = False
#             self._clear_all_previews()
#             self._notify_style_left_button_up()
#             self._ignore_next_left_release = True
#             self._last_release_time = 0.0

#     def _refresh_all_views_after_classification(self, to_class):
#         # Cheap early-exit via the cached index array, not a 300M-bool np.any
#         # scan. _last_changed_indices is set by _apply_mask_and_record /
#         # _apply_classification right next to _last_changed_mask, so it's the
#         # authoritative "did anything change" signal.
#         changed_idx = getattr(self.app, "_last_changed_indices", None)
#         if not (isinstance(changed_idx, np.ndarray) and changed_idx.size > 0):
#             changed_mask = getattr(self.app, "_last_changed_mask", None)
#             if changed_mask is None or not isinstance(changed_mask, np.ndarray) or not np.any(changed_mask):
#                 print("⏭️ Skipping post-classification refresh - no changed points in current operation")
#                 return

#         display_mode = str(getattr(self.app, "display_mode", "class") or "class").lower()

#         if display_mode == "surface":
#             try:
#                 self.app.refresh_surface_after_classification(
#                     reason="cross_section_commit",
#                     changed_mask=getattr(self.app, "_last_changed_mask", None),
#                 )
#             except Exception as e:
#                 print(f"⚠️ Surface refresh from cross-section failed: {e}")
#             return

#         # Fast classify/update path already refreshed GPU buffers and views.
#         # Skip the optimizer pipeline to avoid duplicate slot sync + render work.
#         if (
#             bool(getattr(self.app, "_gpu_sync_done", False))
#             and not bool(getattr(self.app, "_section_visibility_refresh_required", False))
#         ):
#             # A cross-section RGB injection only synchronizes the point actors.
#             # In single-class shading the main view is a separate triangulated
#             # mesh, so it still needs its topology delta applied before this
#             # commit can be considered visually complete.
#             if display_mode == "shaded_class":
#                 try:
#                     from gui.unified_actor_manager import (
#                         guarantee_main_view_visual_refresh,
#                     )

#                     shaded_refresh_ok = guarantee_main_view_visual_refresh(
#                         self.app,
#                         changed_mask=getattr(self.app, "_last_changed_mask", None),
#                         to_class=to_class,
#                         reason="cross_section_gpu_commit",
#                         origin_view="cross_section",
#                     )
#                     if shaded_refresh_ok:
#                         self.app._gpu_sync_done = False
#                         return
#                     print(
#                         "⚠️ Shaded main-view commit refresh was not handled; "
#                         "continuing with the normal refresh fallback"
#                     )
#                 except Exception as exc:
#                     print(
#                         "⚠️ Shaded main-view commit refresh failed; "
#                         f"continuing with the normal refresh fallback: {exc}"
#                     )

#                 # The GPU point buffers are synchronized, but the shaded mesh
#                 # is not. Clear the marker and continue into the established
#                 # fallback path below instead of suppressing the refresh.
#                 self.app._gpu_sync_done = False
#             else:
#                 self.app._gpu_sync_done = False
#                 return

#         try:
#             # ✅ CRITICAL FIX: ALWAYS use optimizer for multi-view scenarios
#             # The optimizer is specifically designed to handle this efficiently
#             from gui.optimization_config import ENABLE_OPTIMIZED_REFRESH
#             if ENABLE_OPTIMIZED_REFRESH:
#                 from gui.optimized_refresh import get_optimizer
#                 optimizer = get_optimizer(self.app)

#                 from_classes = getattr(self.app, '_last_from_classes', None)
#                 if from_classes is None:
#                     from_classes = getattr(self.app, 'from_classes', None)

#                 active_view = None
#                 if hasattr(self.app, 'section_controller'):
#                     active_view = getattr(self.app.section_controller, 'active_view', None)

#                 optimizer.refresh_after_classification(
#                     to_class=to_class,
#                     from_classes=from_classes,
#                     active_view=active_view,
#                     fallback_func=self._refresh_all_views_original
#                 )
#                 return
#         except Exception as e:
#             print(f"⚠️ Optimizer unavailable: {e}")

#         self._refresh_all_views_original(to_class)


#     def _refresh_all_views_original(self, to_class):
#         """
#         ✅ ORIGINAL CODE - Renamed but unchanged.
        
#         This is your existing _refresh_all_views_after_classification method.
#         """
#         # ═══════════════════════════════════════════════════════════════════════
#         # YOUR EXISTING CODE GOES HERE - COMPLETELY UNCHANGED
#         # ═══════════════════════════════════════════════════════════════════════
        
#         print(f"\n{'='*60}")
#         print(f"🔄 REFRESHING VIEWS AFTER CLASSIFICATION")
#         print(f"   Target class: {to_class}")
#         print(f"   Display mode: {self.app.display_mode}")
#         print(f"{'='*60}")

#         num_changed = 0
#         class_text = "no changes"

#         try:
#             import numpy as np
#             app = self.app

#             # ✅ 1. Sync weights BEFORE any refresh
#             print(f"\n🔧 SYNCING WEIGHTS FROM DISPLAY MODE...")
#             self._sync_main_view_palette_weights()

#             # STEP 1: Info about what was classified
#             changed_mask = getattr(app, "_last_changed_mask", None)
#             if to_class is not None:
#                 # Pull the count from the cached index array (already computed
#                 # in _apply_mask_and_record / _apply_classification) instead of
#                 # rescanning a 300M-bool mask with .sum().
#                 changed_idx_cached = getattr(app, "_last_changed_indices", None)
#                 if isinstance(changed_idx_cached, np.ndarray):
#                     num_changed = int(changed_idx_cached.size)
#                 elif isinstance(changed_mask, np.ndarray):
#                     num_changed = int(changed_mask.sum())
#                 else:
#                     num_changed = "?"
#                 print(f"\nℹ️ Classified {num_changed} points to class {to_class}")

#                 already_visible = False
#                 if hasattr(app, 'display_mode_dialog') and app.display_mode_dialog:
#                     dialog = app.display_mode_dialog
#                     if hasattr(dialog, 'view_palettes') and 0 in dialog.view_palettes:
#                         slot0 = dialog.view_palettes[0]
#                         if to_class in slot0:
#                             already_visible = slot0[to_class].get("show", False)

#                 if already_visible:
#                     print(f"   ✅ Class {to_class} is already visible")
#                 else:
#                     print(f"   ⚠️ Class {to_class} is HIDDEN")

#             # STEP 2: Refresh ALL cross-section views
#             if hasattr(app, 'section_vtks') and app.section_vtks:
#                 num_views = len(app.section_vtks)
#                 print(f"\n🔄 Refreshing ALL {num_views} cross-section views...")

#                 for view_idx in sorted(app.section_vtks.keys()):
#                     try:
#                         # ✅ ISSUE 2 FIX: Skip refresh if no changed points intersect this section's bounding box
#                         section_combined_mask = getattr(app, f"section_{view_idx}_combined_mask", None)
#                         if (section_combined_mask is not None
#                             and isinstance(section_combined_mask, np.ndarray)
#                             and len(section_combined_mask) == len(changed_mask)):
#                             if not np.any(changed_mask & section_combined_mask):
#                                 continue  # no changed points in this section — skip
                        
#                         self._refresh_single_view(view_idx)
#                     except Exception as e:
#                         print(f"      ⚠️ View {view_idx + 1} refresh failed: {e}")

#             # STEP 3: MAIN VIEW REFRESH
#             print(f"\n🔄 Refreshing Main View...")

#             vtk_widget = getattr(app, 'vtk_widget', None)
#             saved_camera = None
#             if vtk_widget is not None:
#                 try:
#                     if hasattr(vtk_widget, 'camera_position') and vtk_widget.camera_position is not None:
#                         saved_camera = vtk_widget.camera_position
#                 except Exception as e:
#                     print(f"   ⚠️ Camera save failed: {e}")

#             display_mode = str(getattr(app, "display_mode", "class") or "class").lower()

#             if display_mode == "class":
#                 # ✅ UNIFIED ACTOR: write directly into shared GPU buffer — no rebuild
#                 from gui.unified_actor_manager import fast_classify_update, is_unified_actor_ready
#                 if is_unified_actor_ready(app):
#                     changed_mask = getattr(app, '_last_changed_mask', None)
#                     palette      = getattr(app, 'class_palette', {})
#                     border_pct   = float(getattr(app, 'point_border_percent', 0) or 0.0)
#                     fast_classify_update(app, changed_mask=changed_mask,
#                                          to_class=to_class, palette=palette,
#                                          border_percent=border_pct,
#                                          skip_render=True)
#                     try:
#                         self._safe_render_pyvista(vtk_widget)
#                     except Exception:
#                         pass
#                     print(f"   ✅ Unified actor GPU poke complete")
#                 else:
#                     # Unified actor not built yet — trigger a one-time build
#                     print(f"   ⚠️ Unified actor not ready — triggering build")
#                     from gui.class_display import update_class_mode
#                     update_class_mode(app, force_refresh=True)

#             elif display_mode == "shaded_class":
#                 print(f"   🌗 Shaded mode – forcing rebuild...")
#                 azimuth = getattr(app, "last_shade_azimuth", 45.0)
#                 angle = getattr(app, "last_shade_angle", 45.0)
#                 ambient = getattr(app, "shade_ambient", 0.2)
#                 from gui.shading_display import update_shaded_class
#                 update_shaded_class(app, azimuth, angle, ambient)

#             elif display_mode == "surface":
#                 print("   🏔️ Surface mode – forcing mesh rebuild after cross-section classification...")
#                 try:
#                     self.app.refresh_surface_after_classification(
#                         reason="cross_section_commit",
#                         changed_mask=changed_mask,
#                     )
#                 except Exception as e:
#                     print(f"   ⚠️ Surface refresh failed: {e}")
#             else:
#                 # We are in depth, intensity, elevation, or rgb mode.
#                 # Guard: keep current non-class main-view buffer untouched until mode switch.
#                 print(f"   📊 Mode '{display_mode}' – main view frozen, classification data saved, will render on mode switch")

#             if saved_camera is not None and vtk_widget is not None:
#                 try:
#                     vtk_widget.camera_position = saved_camera
#                     # Guard: do not trigger main-view render in non-class modes after classify.
#                     if display_mode in ("class", "shaded_class"):
#                         self._safe_render_pyvista(vtk_widget)
#                 except Exception as e:
#                     print(f"   ⚠️ Camera restore failed: {e}")

#             # # STEP 4: CUT SECTION
#             #             cut_ctrl.onclassificationchanged()
#             # STEP 4: CUT SECTION
#             if (hasattr(app, "cut_section_controller") and app.cut_section_controller is not None):
#                 try:
#                     cut_ctrl = app.cut_section_controller
#                     if getattr(cut_ctrl, 'is_cut_view_active', False):
#                         if hasattr(cut_ctrl, 'onclassificationchanged'):
#                             cut_ctrl.onclassificationchanged()
#                         else:
#                             # Fallback if method is missing
#                             cut_ctrl._refresh_cut_colors_fast()
#                         print("   ✅ Cut Section refreshed")
#                 except Exception as e:
#                     print(f"   ⚠️ Cut section refresh failed: {e}")

#             # STEP 5: POINT STATISTICS
#             try:
#                 from gui.point_count_widget import refresh_point_statistics
#                 refresh_point_statistics(app)
#             except Exception:
#                 pass

#             # STEP 6: Status bar
#             def _get_class_name(code):
#                 try:
#                     palette = getattr(app, 'class_palette', {})
#                     if code in palette:
#                         return palette[code].get("description", f"Class {code}")
#                 except Exception:
#                     pass
#                 return f"Class {code}"

#             to_name = _get_class_name(to_class)
#             app.statusBar().showMessage(
#             # REPLACE with this safe version:
#             f"✅ {num_changed:,} points classified to {to_name}" if isinstance(num_changed, (int, float)) else f"✅ {num_changed} points classified to {to_name}",
#                 3000
#             )

#             print(f"\n{'='*60}")
#             print(f"✅ REFRESH COMPLETE")
#             print(f"{'='*60}\n")

#         except Exception as e:
#             print(f"\n⚠️ MULTI-VIEW REFRESH ERROR: {e}")
#             import traceback
#             traceback.print_exc()

#     def _refresh_main_view_with_filter(self, to_class):
#         """
#         ✅ UNIFIED ACTOR: Apply visibility filter via sync_palette_to_gpu (slot 0).
#         Never calls plotter.clear() — that would destroy the unified actor.
#         """
#         print(f"\n{'='*60}")
#         print(f"🔄 FILTERED MAIN VIEW REFRESH (UNIFIED GPU PATH)")
#         print(f"{'='*60}")

#         try:
#             app = self.app

#             if app.display_mode != "class":
#                 print("   ⏭️ Not in class mode")
#                 return

#             visible_classes = self._get_visible_classes_for_slot(0)
#             if visible_classes is None:
#                 print("   ⏭️ No filter active")
#                 return

#             print(f"   👁️ Filter: showing {len(visible_classes)} classes: {visible_classes}")

#             from gui.unified_actor_manager import sync_palette_to_gpu, is_unified_actor_ready
#             if not is_unified_actor_ready(app):
#                 print("   ⚠️ Unified actor not ready — skipping filter refresh")
#                 return

#             # Build a palette copy with show=False for hidden classes, then push to GPU
#             palette = dict(getattr(app, 'class_palette', {}))
#             visible_set = set(visible_classes)
#             filtered_palette = {}
#             for code, info in palette.items():
#                 entry = dict(info)
#                 entry['show'] = (code in visible_set)
#                 filtered_palette[code] = entry

#             border_pct = float(getattr(app, 'point_border_percent', 0) or 0.0)
#             sync_palette_to_gpu(app, 0, filtered_palette, border_pct, render=True)
#             print(f"   ✅ Unified filter refresh complete")

#         except Exception as e:
#             print(f"   ❌ Filtered refresh FAILED: {e}")
#             import traceback
#             traceback.print_exc()

#         print(f"{'='*60}\n")

#     def _refresh_single_view(self, view_index, changed_mask=None):
#         """
#         Refresh a single cross-section view:
#         ✅ ZERO BLINK: Routes directly to unified_actor_manager.fast_cross_section_update
#         """
#         import time
#         from gui.unified_actor_manager import fast_cross_section_update
        
#         t0 = time.perf_counter()
#         slot = view_index + 1
        
#         # Get palette for this specific view (slot 1-4)
#         view_palette = None
#         if hasattr(self.app, 'display_mode_dialog') and self.app.display_mode_dialog:
#             dialog = self.app.display_mode_dialog
#             if hasattr(dialog, 'view_palettes') and slot in dialog.view_palettes:
#                 view_palette = dialog.view_palettes[slot]
        
#         if not view_palette and hasattr(self.app, 'view_palettes'):
#             view_palette = self.app.view_palettes.get(slot)
        
#         if not view_palette:
#             if slot == 0 and hasattr(self.app, 'class_palette'):
#                 view_palette = self.app.class_palette
#             elif hasattr(self.app, "_get_cross_section_palette"):
#                 try:
#                     view_palette = self.app._get_cross_section_palette(
#                         slot,
#                         allow_default_seed=True,
#                         persist_seed=False,
#                     )
#                 except Exception:
#                     pass
             
#         ok = fast_cross_section_update(
#             self.app,
#             view_index,
#             changed_mask,
#             palette=view_palette,
#             force_visibility_refresh=False,
#         )
#         if not ok:
#             from gui.unified_actor_manager import build_section_unified_actor
#             border = float(getattr(self.app, "view_borders", {}).get(slot, 0) or 0.0)
#             view_mode = getattr(self.app, "cross_view_mode", "front")
#             rebuilt = build_section_unified_actor(
#                 self.app,
#                 view_index,
#                 palette=view_palette,
#                 border_percent=border,
#                 view=view_mode,
#             )
#             if rebuilt is not None:
#                 fast_cross_section_update(
#                     self.app,
#                     view_index,
#                     changed_mask,
#                     palette=view_palette,
#                     force_visibility_refresh=False,
#                 )
        
#         elapsed = (time.perf_counter() - t0) * 1000
#         print(f"      ⚡ View {view_index + 1} unified update: {elapsed:.1f} ms")

#     def refresh_main_view_partial(self):
#         """
#         SMART PARTIAL REFRESH: Update ONLY the cross-sectioned area in Main View.
#         - Reads changed points from _last_changed_mask
#         - Updates actors for BOTH old and new classes
#         - Preserves independent main view display settings
#         """
#         print("\n" + "="*60)
#         print("🔄 PARTIAL MAIN VIEW REFRESH")
#         print("="*60)
        
#         try:
#             if self.app.display_mode != "class":
#                 print("⏭️  Not in class mode - skipping")
#                 print("="*60 + "\n")
#                 return
            
#             if not hasattr(self.app, '_last_changed_mask') or self.app._last_changed_mask is None:
#                 print("⏭️  No changed points mask")
#                 print("="*60 + "\n")
#                 return
            
#             changed_mask = self.app._last_changed_mask
#             num_changed = np.sum(changed_mask)
            
#             if num_changed == 0:
#                 print("⏭️  No points changed")
#                 print("="*60 + "\n")
#                 return
            
#             print(f"   📊 Changed points: {num_changed:,}")

#             palette = getattr(self.app, "class_palette", {})
#             if not palette:
#                 print("⚠️  No palette - cannot refresh")
#                 print("="*60 + "\n")
#                 return

#             # ✅ UNIFIED ACTOR: write changed colours directly into _naksha_rgb_ptr.
#             # Never remove/re-add class_ actors — that path is for per-class mode only.
#             from gui.unified_actor_manager import fast_classify_update, is_unified_actor_ready

#             classes    = self.app.data["classification"]
#             changed_indices  = np.flatnonzero(changed_mask)
#             changed_classes  = classes[changed_indices]

#             print(f"   📍 Changed points by class:")
#             for cls in np.unique(changed_classes):
#                 count = np.sum(changed_classes == cls)
#                 print(f"      Class {cls}: {count:,} points")

#             if is_unified_actor_ready(self.app):
#                 border_pct = float(getattr(self.app, 'point_border_percent', 0) or 0.0)
#                 to_class   = getattr(self.app, '_last_to_class', None)
#                 fast_classify_update(self.app,
#                                      changed_mask=changed_mask,
#                                      to_class=to_class,
#                                      palette=palette,
#                                      border_percent=border_pct,
#                                      skip_render=True)
#                 try:
#                     self._safe_render_pyvista(self.app.vtk_widget)
#                 except Exception:
#                     pass
#                 print(f"   ✅ Unified partial refresh complete")
#             else:
#                 print(f"   ⚠️ Unified actor not ready — skipping partial refresh")

#             print("="*60 + "\n")
            
#         except Exception as e:
#             print(f"❌ Partial main view refresh failed: {e}")
#             import traceback
#             traceback.print_exc()
#             print("="*60 + "\n")

#     def _get_main_view_palette(self):
#         """
#         Get the Main View's color palette (Slot 0 in Display Mode).
        
#         Returns:
#             dict: Color palette for main view, or None if not available
#         """
#         # Try Display Mode dialog first
#         if hasattr(self.app, 'display_mode_dialog'):
#             dialog = self.app.display_mode_dialog
            
#             if hasattr(dialog, 'view_palettes') and 0 in dialog.view_palettes:
#                 return dialog.view_palettes[0]
        
#         # Try display_dialog attribute
#         if hasattr(self.app, 'display_dialog'):
#             dialog = self.app.display_dialog
            
#             if hasattr(dialog, 'view_palettes') and 0 in dialog.view_palettes:
#                 return dialog.view_palettes[0]
        
#         # Fallback to global class_palette
#         if hasattr(self.app, 'class_palette') and self.app.class_palette:
#             return self.app.class_palette
        
#         return None
    
#     def _refresh_main_view(self):
#         """
#         ✅ FIXED: Refresh Main View with proper weight synchronization
#         """
#         print(f"      📋 Main View (Slot 0):")
        
#         # Only refresh if in class mode
#         if self.app.display_mode != "class":
#             print(f"         ⏭️ Not in class mode")
#             return
        
#         # ✅ CRITICAL FIX: Sync weights BEFORE refresh
#         self._sync_main_view_palette_weights()
        
#         # Get visible classes
#         palette = getattr(self.app, 'class_palette', {})
        
#         if not palette:
#             print(f"         ❌ No palette available")
#             return
        
#         visible = [c for c, v in palette.items() if v.get("show", False)]
        
#         if not visible:
#             print(f"         ⏭️ No visible classes")
#             return
        
#         print(f"         👁️ Visible classes: {len(visible)}")
        
#         # ✅ DEBUG: Show current weights
#         print(f"         📊 Current weights:")
#         for code in sorted(visible):
#             weight = palette[code].get('weight', 1.0)
#             desc = palette[code].get('description', f'Class {code}')
#             print(f"            Class {code} ({desc[:20]}): {weight:.2f}x")
        
#         # ✅ UNIFIED ACTOR: push updated weights into GPU uniform LUT — no rebuild
#         try:
#             from gui.unified_actor_manager import sync_palette_to_gpu, is_unified_actor_ready
#             if is_unified_actor_ready(self.app):
#                 border_pct = float(getattr(self.app, 'point_border_percent', 0) or 0.0)
#                 sync_palette_to_gpu(self.app, 0, palette, border_pct, render=True)
#                 print(f"         ✅ Unified GPU weight sync complete")
#             else:
#                 from gui.class_display import update_class_mode
#                 update_class_mode(self.app)
#                 print(f"         ✅ Refreshed with updated weights (build path)")
#         except Exception as e:
#             print(f"         ⚠️ Refresh failed: {e}")
#             import traceback
#             traceback.print_exc()
            
#     def _show_refresh_flash(self, view_index):
#         """
#         ✅ Show a subtle visual flash to indicate refresh happened
#         Makes the user aware their classification was instantly applied
#         """
#         try:
#             if not hasattr(self.app, 'section_vtks') or view_index not in self.app.section_vtks:
#                 return
            
#             vtk_widget = self.app.section_vtks[view_index]
            
#             # Method 1: Brief background color flash (subtle)
#             original_bg = vtk_widget.renderer.GetBackground()
            
#             # Flash to slightly brighter background
#             flash_color = (
#                 min(original_bg[0] + 0.1, 1.0),
#                 min(original_bg[1] + 0.1, 1.0),
#                 min(original_bg[2] + 0.1, 1.0)
#             )
            
#             vtk_widget.renderer.SetBackground(*flash_color)
#             self._safe_render_pyvista(vtk_widget)
            
#             # Restore original background after brief delay
#             from PySide6.QtCore import QTimer
            
#             def restore_background():
#                 try:
#                     vtk_widget.renderer.SetBackground(*original_bg)
#                     self._safe_render_pyvista(vtk_widget)
#                 except Exception:
#                     pass
            
#             # Flash duration: 150ms (very brief, not annoying)
#             QTimer.singleShot(150, restore_background)
            
#             print(f"   ✨ Visual feedback shown")
            
#         except Exception as e:
#             # Visual feedback is optional - don't break if it fails
#             print(f"   ⚠️ Visual feedback failed (non-critical): {e}")

#     def _show_cursor_feedback(self):
#         """
#         Alternative visual feedback: briefly change cursor to indicate success
#         Less intrusive than background flash
#         """
#         try:
#             from PySide6.QtCore import Qt, QTimer
#             from PySide6.QtWidgets import QApplication
            
#             # Save original cursor
#             original_cursor = QApplication.overrideCursor()
            
#             # Show "success" cursor briefly (pointing hand)
#             QApplication.setOverrideCursor(Qt.PointingHandCursor)
            
#             # Restore after 200ms
#             def restore_cursor():
#                 QApplication.restoreOverrideCursor()
#                 if original_cursor:
#                     QApplication.setOverrideCursor(original_cursor)
            
#             QTimer.singleShot(200, restore_cursor)
            
#         except Exception as e:
#             print(f"   ⚠️ Cursor feedback failed: {e}")

#     def _verify_actor_update(self, view_index, class_code, expected_size):
#         """
#         ✅ UNIFIED ACTOR: Verify weight via ViewShaderContext.weight_lut.
#         Per-class actors no longer exist — point sizes live in the GPU uniform LUT.
#         """
#         try:
#             vtk_widget = getattr(self.app, 'section_vtks', {}).get(view_index)
#             if vtk_widget is None:
#                 return False

#             # Unified section actor name
#             actor = vtk_widget.actors.get(f"_section_{view_index}_unified")
#             if actor is None:
#                 print(f"      ⚠️ View {view_index+1}: unified actor not found — skipping verify")
#                 return False

#             ctx = getattr(actor, '_naksha_shader_ctx', None)
#             if ctx is None:
#                 print(f"      ⚠️ View {view_index+1}: no shader ctx on unified actor")
#                 return False

#             actual_size = float(ctx.weight_lut[int(class_code)])
#             size_match  = abs(actual_size - expected_size) < 0.05

#             if size_match:
#                 print(f"      ✅ View {view_index+1}: Class {class_code} LUT verified ({actual_size:.2f})")
#             else:
#                 print(f"      ❌ View {view_index+1}: Class {class_code} LUT MISMATCH! "
#                       f"expected={expected_size:.2f} actual={actual_size:.2f}")
#             return size_match

#         except Exception as e:
#             print(f"      ⚠️ Verify failed: {e}")
#             return False

#     def on_key_press(self, obj, evt):
#         """Handle keyboard shortcuts."""
#         # ✅ CRITICAL SAFETY CHECK
#         if not hasattr(self, 'app') or self.app is None:
#             return
        
#         if not hasattr(self, 'interactor') or self.interactor is None:
#             return
        
#         key = self.interactor.GetKeySym().lower()

#         if key in ("return", "enter") and getattr(self.app, "active_classify_tool", None) == "polygon":
#             self._append_polygon_vertex_from_cursor()
#             if self._finalize_polygon_classification():
#                 self._clear_all_previews()
#             return
        
#         # EXISTING ESC HANDLER
#         if key == "escape":
#             print("\n⚠️ ESC pressed - deactivating classification tool")
            
#             if hasattr(self.app, 'deactivate_classification'):
#                 self.app.deactivate_classification()
#             else:
#                 self.app.active_classify_tool = None
#                 self.app.skip_main_view_refresh = False
#                 if hasattr(self.app, "_gpu_sync_done"):
#                     self.app._gpu_sync_done = False
#                 if hasattr(self.app, "_section_visibility_refresh_required"):
#                     self.app._section_visibility_refresh_required = False
#                 try:
#                     if hasattr(self.app, "vtk_widget") and self.app.vtk_widget is not None:
#                         self.app.vtk_widget.render()
#                 except Exception:
#                     pass
            
#             self.P1 = None
#             self.is_dragging = False
#             self.drawing_points = []
#             self.is_drawing_freehand = False
#             self._clear_all_previews()
            
#             print("✅ Classification tool cancelled")
        

#     def handle_escape(self):
#         """Handle ESC key inside classification context safely."""
#         try:
#             self.app._classification_preview_active = False
#             if hasattr(self.app, "active_classify_tool"):
#                 print(f"🟣 ESC pressed in classification tool '{self.app.active_classify_tool}'")
#                 self.app.active_classify_tool = None
#                 if hasattr(self.app, "statusBar"):
#                     self.app.statusBar().showMessage("Classification cancelled.", 3000)
#                 print("✅ Classification ESC handled cleanly.")
#         except Exception as e:
#             print(f"⚠️ Classification ESC error: {e}")


#     def _draw_freehand_preview(self, live_point=None):
#         """
#         Smooth freehand preview using Catmull-Rom spline interpolation.
#         Works for MAIN (XY) and SECTION (u,z) — uses self._freehand_* reusable pipeline.
#         """
#         if len(self.drawing_points) < 1:
#             return

#         vtk_widget = self._get_active_vtk_widget()
#         if vtk_widget is None:
#             # Fallback: use first renderer
#             try:
#                 rw = self.interactor.GetRenderWindow()
#                 renderer = rw.GetRenderers().GetFirstRenderer()
#             except Exception:
#                 return
#         else:
#             renderer = vtk_widget.renderer

#         # Collect raw control points
#         all_pts = list(self.drawing_points)
#         if live_point:
#             all_pts.append(live_point)
#         if len(all_pts) < 2:
#             return

#         # ── SMOOTH: Catmull-Rom spline → true curves, not straight segments ──
#         all_pts = self._catmull_rom_smooth(all_pts, steps=10)

#         # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
#         if not hasattr(self, "_freehand_pts") or self.freehand_actor is None:
#             from vtkmodules.vtkRenderingCore import vtkPolyDataMapper2D, vtkActor2D, vtkCoordinate as _C

#             self._freehand_pts   = vtkPoints()
#             self._freehand_lines = vtkCellArray()
#             self._freehand_poly  = vtkPolyData()
#             self._freehand_poly.SetPoints(self._freehand_pts)
#             self._freehand_poly.SetLines(self._freehand_lines)

#             mapper = vtkPolyDataMapper2D()
#             mapper.SetInputData(self._freehand_poly)
#             dc = _C(); dc.SetCoordinateSystemToDisplay()
#             mapper.SetTransformCoordinate(dc)

#             self.freehand_actor = vtkActor2D()
#             self.freehand_actor.SetMapper(mapper)
#             prop = self.freehand_actor.GetProperty()
#             prop.SetColor(1, 0.8, 0)
#             prop.SetLineWidth(2)
#             prop.SetOpacity(0.8)

#             renderer.AddActor2D(self.freehand_actor)

#         if not renderer.HasViewProp(self.freehand_actor):
#             renderer.AddActor2D(self.freehand_actor)
#         self.freehand_actor.VisibilityOn()

#         if not hasattr(self, "_freehand_coord"):
#             from vtkmodules.vtkRenderingCore import vtkCoordinate as _C
#             self._freehand_coord = _C()
#             self._freehand_coord.SetCoordinateSystemToWorld()

#         coord = self._freehand_coord
#         self._freehand_pts.Reset()
#         self._freehand_lines.Reset()

#         for i, pt in enumerate(all_pts):
#             u, v = pt[0], pt[1]
#             xw, yw, zw = self._view2d_to_world(u, v)
#             coord.SetValue(xw, yw, zw)
#             d = coord.GetComputedDisplayValue(renderer)
#             self._freehand_pts.InsertNextPoint(d[0], d[1], 0)
#             if i > 0:
#                 self._freehand_lines.InsertNextCell(2)
#                 self._freehand_lines.InsertCellPoint(i - 1)
#                 self._freehand_lines.InsertCellPoint(i)

#         self._freehand_pts.Modified()
#         self._freehand_poly.Modified()
#         # Render batched by caller

#     def _draw_point_cursor(self, center, radius=0.5):
#         """✅ POINT CURSOR: Filled circle with outline (different from brush/circle tools)"""
#         renderWindow = self.interactor.GetRenderWindow()
#         renderer = renderWindow.GetRenderers().GetFirstRenderer()
 
#         # Remove previous actor
#         if hasattr(self, "brush_actor") and self.brush_actor:
#             renderer.RemoveActor2D(self.brush_actor)
#             self.brush_actor = None
 
#         cx, cz = center
 
#         # Force minimum radius
#         if radius < 0.05:
#             radius = 0.1
 
#         from vtkmodules.vtkRenderingCore import vtkCoordinate
       
#         # ========== CREATE FILLED POINT SHAPE ==========
#         # 1. Filled circle (using triangle fan)
#         # 2. Outline circle
       
#         num_segments = 32
       
#         # Create circle points in world space
#         circle_world = []
#         for i in range(num_segments + 1):
#             angle = 2 * np.pi * i / num_segments
#             x = cx + radius * np.cos(angle)
#             z = cz + radius * np.sin(angle)
#             circle_world.append((x, 0, z))
       
#         # Convert to 2D display coordinates
#         pts = vtkPoints()
       
#         # Add center point first (for triangle fan)
#         coord_center = vtkCoordinate()
#         coord_center.SetCoordinateSystemToWorld()
#         coord_center.SetValue(cx, 0, cz)
#         display_center = coord_center.GetComputedDisplayValue(renderer)
#         pts.InsertNextPoint(display_center[0], display_center[1], 0)
       
#         # Add circle perimeter points
#         for point in circle_world:
#             coord = vtkCoordinate()
#             coord.SetCoordinateSystemToWorld()
#             coord.SetValue(point[0], point[1], point[2])
#             display_pos = coord.GetComputedDisplayValue(renderer)
#             pts.InsertNextPoint(display_pos[0], display_pos[1], 0)
 
#         # Create filled circle using triangle fan
#         from vtkmodules.vtkCommonDataModel import vtkCellArray
#         polys = vtkCellArray()
       
#         # Triangle fan from center to perimeter
#         for i in range(1, num_segments + 1):
#             polys.InsertNextCell(3)
#             polys.InsertCellPoint(0)  # Center
#             polys.InsertCellPoint(i)
#             polys.InsertCellPoint(i + 1 if i < num_segments else 1)
 
#         # Create outline
#         lines = vtkCellArray()
#         for i in range(1, num_segments + 1):
#             lines.InsertNextCell(2)
#             lines.InsertCellPoint(i)
#             lines.InsertCellPoint(i + 1 if i < num_segments else 1)
 
#         poly = vtkPolyData()
#         poly.SetPoints(pts)
#         poly.SetPolys(polys)  # Filled triangles
#         poly.SetLines(lines)   # Outline
 
#         # Create 2D mapper
#         from vtkmodules.vtkRenderingCore import vtkPolyDataMapper2D
#         mapper = vtkPolyDataMapper2D()
#         mapper.SetInputData(poly)
       
#         coordinate = vtkCoordinate()
#         coordinate.SetCoordinateSystemToDisplay()
#         mapper.SetTransformCoordinate(coordinate)
 
#         # Create 2D actor
#         from vtkmodules.vtkRenderingCore import vtkActor2D
#         self.brush_actor = vtkActor2D()
#         self.brush_actor.SetMapper(mapper)
 
#         # Set properties - Semi-transparent filled yellow circle with outline
#         prop = self.brush_actor.GetProperty()
#         prop.SetColor(1, 1, 0)  # Yellow
#         prop.SetLineWidth(2)
#         prop.SetOpacity(0.5)  # Semi-transparent for filled area
 
#         renderer.AddActor2D(self.brush_actor)
#         print(f"🎯 Point cursor drawn (filled circle, radius={radius:.2f})")

#      # ======== MAIN-VIEW CLASSIFIERS (Plan View: XY) ========
 
#     def _apply_mask_and_record(self, mask, to_class):
#         """
#         Apply classification to app.data using mask and push an undo step.

#         ✅ FIX: In MAIN VIEW, classification affects ONLY classes that are visible
#             in Display Mode (slot 0). Hidden classes are protected.
#         ✅ Also enforces from_classes here (so brush + any caller is consistent).
#         """
#         self.app._gpu_sync_done = False
#         self.app._section_visibility_refresh_required = False
#         self.app._last_changed_mask = None
#         self.app._last_changed_indices = None
#         # Stamp BEFORE the GPU sync calls (which trigger main-view render via
#         # the wrapped vtk_widget.render). GPURenderManager reads this to
#         # coalesce main-view renders during a classify streak — see
#         # request_render().
#         self.app._last_classify_ts = time.time()

#         if mask is None or not np.any(mask):
#             if hasattr(self.app, 'statusBar'):
#                 self.app.statusBar().showMessage("No points found in selection.", 2000)
#             return False

#         if to_class is None:
#             to_class = getattr(self.app, "to_class", None)
#         if to_class is None:
#             if hasattr(self.app, 'statusBar'):
#                 self.app.statusBar().showMessage("Target class not set.", 2000)
#             return False

#         classes = self.app.data["classification"]

#         # ---------------------------------------------------------
#         # ✅ 1) Enforce from_classes filter (safe even if caller did it already)
#         # ---------------------------------------------------------
#         mask = self._filter_from_classes(mask)

#         # ---------------------------------------------------------
#         # ✅ 1b) In Surface mode, only allow edits on visible classes.
#         # ---------------------------------------------------------
#         if str(getattr(self.app, "display_mode", "") or "").lower() == "surface":
#             visible_classes = self._get_visible_classes_for_slot(0)
#             if visible_classes == []:
#                 if hasattr(self.app, 'statusBar'):
#                     self.app.statusBar().showMessage("No visible classes selected in Surface mode.", 2500)
#                 return False

#             if visible_classes is not None:
#                 sel_idx = np.flatnonzero(mask)
#                 if sel_idx.size > 0:
#                     keep = np.isin(
#                         classes[sel_idx],
#                         np.asarray(visible_classes, dtype=classes.dtype),
#                     )
#                     mask = np.zeros_like(mask)
#                     mask[sel_idx[keep]] = True

#         # ---------------------------------------------------------
#         # ✅ 2) Enforce Display Mode visibility filter for MAIN VIEW (slot 0)
#         # ---------------------------------------------------------
#         if self._is_main_view() and str(getattr(self.app, "display_mode", "") or "").lower() != "surface":
#             visible_classes = self._get_visible_classes_for_slot(0)

#             if visible_classes == []:
#                 if hasattr(self.app, 'statusBar'):
#                     self.app.statusBar().showMessage("No visible classes selected in Display Mode.", 2500)
#                 return False

#             if visible_classes is not None:
#                 # ⚡ O(K) filter — only check the K selected points (typically
#                 # a few thousand for rect/circle/freehand), not all 39M
#                 # points. The naive `np.isin(classes, visible_classes)` over
#                 # 39M points takes 100–200 ms and was the dominant cost for
#                 # main-view rect/circle/freehand classify.
#                 sel_idx = np.flatnonzero(mask)
#                 if sel_idx.size > 0:
#                     keep = np.isin(
#                         classes[sel_idx],
#                         np.asarray(visible_classes, dtype=classes.dtype),
#                     )
#                     mask = np.zeros_like(mask)
#                     mask[sel_idx[keep]] = True

#         # Compute changed_indices ONCE — the previous code scanned the full
#         # N=300M-point mask up to 5 separate times (np.any, classes[mask].copy,
#         # np.flatnonzero, classes[mask] = to_class). Working in index-space
#         # below saves ~3 full-array scans (~300-400 ms per classify on 300M pts).
#         changed_indices = np.flatnonzero(mask).astype(np.int64, copy=False)
#         if changed_indices.size == 0:
#             if hasattr(self.app, 'statusBar'):
#                 self.app.statusBar().showMessage("No visible/from-class points in selection.", 2500)
#             return False

#         # ---------------------------------------------------------
#         # Apply + Undo/Redo (all O(K) — K = changed point count)
#         # ---------------------------------------------------------
#         old = classes[changed_indices].copy()
#         new = np.full(changed_indices.size, to_class, dtype=classes.dtype)

#         self.app.undo_stack.append({
#             "indices": changed_indices,
#             "old_classes": old,
#             "new_classes": new,
#         })
#         self.app.redo_stack.clear()

#         classes[changed_indices] = to_class
#         self.app._last_changed_mask = mask
#         self.app._last_changed_indices = changed_indices.copy()

#         # 🎯 Exact classified count → top bar center (point tool, main-view
#         # circle/rect/polygon/freehand via interactor, etc.)
#         try:
#             self.app._update_classify_count_display(int(changed_indices.size))
#         except Exception:
#             pass

#         # ✅ Store from_classes for undo
#         self.app._last_from_classes = list(getattr(self.app, "from_classes", []) or [])

#         # ─────────────────────────────────────────────────────────────
#         # ⚡ MICROSTATION-STYLE INSTANT GPU INJECTION (replaces heavy optimizer)
#         # ─────────────────────────────────────────────────────────────
#         try:
#             from gui.unified_actor_manager import fast_partial_classify_update, fast_partial_cross_section_update
            
#             # 1. Main view: inject colors directly into GPU buffer (O(changed_points))
#             fast_partial_classify_update(self.app, mask)
            
#             # 2. Cross-sections: partial shared memory slave update
#             section_widgets_to_render = []
#             if hasattr(self.app, 'section_vtks') and self.app.section_vtks:
#                 for view_idx, section_widget in self.app.section_vtks.items():
#                     try:
#                         # Render only views that actually contain changed points.
#                         should_render = False
#                         section_mask = getattr(self.app, f"section_{view_idx}_combined_mask", None)
#                         if (section_mask is not None
#                                 and len(section_mask) == len(mask)
#                                 and changed_indices.size > 0):
#                             should_render = bool(np.any(section_mask[changed_indices]))

#                         fast_partial_cross_section_update(self.app, view_idx, mask)

#                         if should_render and section_widget is not None:
#                             section_widgets_to_render.append(section_widget)
#                     except Exception:
#                         pass
            
#             if hasattr(self.app, 'cut_section_controller') and self.app.cut_section_controller:
#                 ctrl = self.app.cut_section_controller
#                 if getattr(ctrl, 'is_cut_view_active', False) and ctrl._cut_index_map is not None:
#                     try:
#                         ctrl._refresh_cut_colors_fast()
#                         if hasattr(ctrl, 'cut_vtk') and ctrl.cut_vtk:
#                             self._safe_render_pyvista(ctrl.cut_vtk)
#                     except Exception as _ce:
#                         print(f"⚠️ Cut section refresh failed: {_ce}")
            
#             # 4. Render only the updated visible cross-section views.
#             for section_widget in section_widgets_to_render:
#                 try:
#                     if (hasattr(section_widget, 'isVisible')
#                             and callable(section_widget.isVisible)
#                             and not section_widget.isVisible()):
#                         continue
#                     self._safe_render_pyvista(section_widget)
#                 except Exception:
#                     pass

#             # 5. Main-view presentation is deferred to
#             # guarantee_main_view_visual_refresh below. Rendering the complete
#             # faceted mesh here produced a stale pre-patch frame and the
#             # guarantee immediately rendered the same main view again.
            
#             # 6. Point statistics
#             try:
#                 from gui.point_count_widget import refresh_point_statistics
#                 refresh_point_statistics(self.app)
#             except Exception:
#                 pass
            
#             # 7. Status bar
#             n_pts = int(changed_indices.size)
#             if hasattr(self.app, 'statusBar'):
#                 self.app.statusBar().showMessage(f"✅ {n_pts:,} points → class {to_class}", 3000)

#             # ✅ FIX: Signal that GPU sync is complete — prevents the
#             # classify_with_stats_update decorator from triggering a
#             # redundant 170ms OptimizedRefreshPipeline cycle.
#             self.app._gpu_sync_done = True
#             self.app._section_visibility_refresh_required = False
#             try:
#                 from gui.unified_actor_manager import guarantee_main_view_visual_refresh
#                 guarantee_main_view_visual_refresh(
#                     self.app,
#                     changed_mask=mask,
#                     to_class=to_class,
#                     reason="main_view_commit",
#                 )
#             except Exception:
#                 pass
#             # Mark active-classify timestamp so MemoryLeakGuard defers its
#             # gen-2 GC while the user is mid-streak.
#             self.app._last_classify_ts = time.time()
            
#             # The guarantee above is the sole shaded-class updater and presenter
#             # for this commit. Repeating the shading refresh here traversed the
#             # facet colors twice and scheduled another render of the same mask.

#         except Exception as e:
#             print(f"⚠️ Fast injection failed, falling back to full refresh: {e}")
#             import traceback
#             traceback.print_exc()
#             self._refresh_all_views_after_classification(to_class)
#             return True

#         return True

#     def _get_visible_classes_for_slot(self, slot: int):
#         """
#         slot 0 = Main View, slot 1..4 = Cross Section views.
#         Returns:
#         - []  : if palette exists but nothing is visible (block classification)
#         - None: if no palette available (do not filter)
#         - list of visible class codes otherwise
#         """
#         dialog = getattr(self.app, "display_mode_dialog", None) or getattr(self.app, "display_dialog", None)

#         palette = None

#         # Priority 1: dialog palettes (most accurate)
#         if dialog is not None and hasattr(dialog, "view_palettes") and slot in dialog.view_palettes:
#             palette = dialog.view_palettes.get(slot)

#         # Priority 2: app stored palettes
#         if (not palette) and hasattr(self.app, "view_palettes"):
#             palette = self.app.view_palettes.get(slot)

#         # Priority 3: fallback main palette for slot 0, isolated seed for slots 1..5
#         if not palette:
#             if slot == 0:
#                 palette = getattr(self.app, "class_palette", None)
#             elif hasattr(self.app, "_get_cross_section_palette"):
#                 try:
#                     palette = self.app._get_cross_section_palette(
#                         slot,
#                         allow_default_seed=True,
#                         persist_seed=False,
#                     )
#                 except Exception:
#                     palette = None

#         if not palette:
#             return None  # no filtering possible

#         visible = [int(code) for code, info in palette.items() if info.get("show", False)]
#         return visible  # can be [] intentionally
 
#     def _filter_from_classes(self, mask):
#         """If from-classes are set, keep only those; otherwise return mask.

#         ⚡ O(K) filter — checks only the K selected points (np.flatnonzero
#         on the mask), not all N=39M classes. The naive `np.isin(classes, fc)`
#         version scans the entire classification array (~100–200 ms on 39M
#         points) and was a major bottleneck for main-view rect/circle/freehand
#         classify (where this function is called from _apply_mask_and_record).
#         """
#         fc = getattr(self.app, "from_classes", None)
#         if not fc:
#             return mask
#         classes = self.app.data["classification"]
#         sel_idx = np.flatnonzero(mask)
#         if sel_idx.size == 0:
#             return mask
#         keep = np.isin(classes[sel_idx], np.asarray(fc, dtype=classes.dtype))
#         out = np.zeros_like(mask)
#         out[sel_idx[keep]] = True
#         return out
 
#     def _classify_rectangle_main(self, p1, p2, to_class):
#         """Rectangle selection on plan view (XY)."""
#         xyz = self.app.data["xyz"]
#         x1, y1 = p1
#         x2, y2 = p2
#         xmin, xmax = sorted([x1, x2])
#         ymin, ymax = sorted([y1, y2])
#         mask = (xyz[:, 0] >= xmin) & (xyz[:, 0] <= xmax) & (xyz[:, 1] >= ymin) & (xyz[:, 1] <= ymax)
#         # _apply_mask_and_record calls _filter_from_classes internally — no need to call it here
#         self._apply_mask_and_record(mask, to_class)

#     def _classify_circle_main(self, center, radius, to_class):
#         """Circle selection on plan view (XY). Bounding-box pre-filter for large datasets."""
#         xyz = self.app.data["xyz"]
#         cx, cy = center
#         r2 = radius * radius
#         # BB pre-filter eliminates ~75% of points before the distance check
#         bb = (xyz[:, 0] >= cx - radius) & (xyz[:, 0] <= cx + radius) & \
#              (xyz[:, 1] >= cy - radius) & (xyz[:, 1] <= cy + radius)
#         cands = np.flatnonzero(bb)
#         final_mask = np.zeros(len(xyz), dtype=bool)
#         if cands.size > 0:
#             dx = xyz[cands, 0] - cx
#             dy = xyz[cands, 1] - cy
#             final_mask[cands[(dx * dx + dy * dy) <= r2]] = True
#         # _apply_mask_and_record calls _filter_from_classes internally — no need to call it here
#         self._apply_mask_and_record(final_mask, to_class)
 
#     def _classify_polygon_main(self, poly2d, to_class):
#         """Polygon/freehand selection on plan view (XY)."""
#         xs = np.array([p[0] for p in poly2d], dtype=np.float64)
#         ys = np.array([p[1] for p in poly2d], dtype=np.float64)
#         xmin, xmax = xs.min(), xs.max()
#         ymin, ymax = ys.min(), ys.max()
 
#         xy = self.app.data["xyz"][:, :2]
#         bbmask = (xy[:, 0] >= xmin) & (xy[:, 0] <= xmax) & (xy[:, 1] >= ymin) & (xy[:, 1] <= ymax)
#         candidates = np.flatnonzero(bbmask)
 
#         if candidates.size == 0:
#             self._apply_mask_and_record(bbmask, to_class)  # no points
#             return
 
#         # Fast point-in-polygon using matplotlib, with fallback
#         try:
#             from matplotlib.path import Path
#             path = Path(np.c_[xs, ys], closed=True)
#             inside = path.contains_points(xy[candidates])
#             final_mask = np.zeros(len(xy), dtype=bool)
#             final_mask[candidates[inside]] = True
#         except Exception:
#             # Ray casting fallback
#             final_mask = np.zeros(len(xy), dtype=bool)
#             poly = np.c_[xs, ys]
#             for idx in candidates:
#                 x, y = xy[idx]
#                 c = False
#                 j = len(poly) - 1
#                 for i in range(len(poly)):
#                     xi, yi = poly[i]
#                     xj, yj = poly[j]
#                     if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
#                         c = not c
#                     j = i
#                 if c:
#                     final_mask[idx] = True
 
#         final_mask = self._filter_from_classes(final_mask)
#         self._apply_mask_and_record(final_mask, to_class)
 
#     def _classify_brush_main(self, center, radius, to_class):
#         """Brush = circle selection at current mouse position (plan view)."""
#         self._classify_circle_main(center, radius, to_class)
 
#     def _classify_point_main(self, center, radius, to_class):
#         """Classify exactly ONE nearest eligible point in plan view (XY)."""
#         xyz = self.app.data["xyz"]
#         if xyz is None or len(xyz) == 0:
#             return

#         try:
#             cx = float(center[0])
#             cy = float(center[1])
#             r = float(radius)
#         except Exception:
#             return

#         if (not np.isfinite(cx)) or (not np.isfinite(cy)):
#             return
#         if (not np.isfinite(r)) or r <= 0.0:
#             r = 0.5
#         r2 = r * r

#         # Use main-view spatial grid when available (same speed strategy as brush).
#         _xyz_now = self.app.data.get("xyz")
#         if (getattr(self, "_main_grid_index", None) is None
#                 or getattr(self, "_brush_spatial_index_xyz_id", None) != id(_xyz_now)):
#             self._build_spatial_index_for_brush()

#         grid = getattr(self, "_main_grid_index", None)
#         if grid is not None and getattr(grid, "valid", False):
#             candidates = grid.query_rectangle_minmax(cx - r, cy - r, cx + r, cy + r)
#         else:
#             bb = (
#                 (xyz[:, 0] >= cx - r) & (xyz[:, 0] <= cx + r) &
#                 (xyz[:, 1] >= cy - r) & (xyz[:, 1] <= cy + r)
#             )
#             candidates = np.flatnonzero(bb).astype(np.int64, copy=False)

#         if candidates.size == 0:
#             return

#         pts2 = xyz[candidates, :2]
#         dx = pts2[:, 0] - cx
#         dy = pts2[:, 1] - cy
#         dist2 = dx * dx + dy * dy
#         in_radius = dist2 <= r2
#         if not np.any(in_radius):
#             return

#         candidates = candidates[in_radius]
#         dist2 = dist2[in_radius]

#         classes = self.app.data["classification"]
#         eligible = np.ones(candidates.shape[0], dtype=bool)

#         visible_classes = self._get_visible_classes_for_slot(0)
#         if visible_classes == []:
#             return
#         if visible_classes is not None:
#             eligible &= np.isin(
#                 classes[candidates],
#                 np.asarray(visible_classes, dtype=classes.dtype),
#             )

#         fc = getattr(self.app, "from_classes", None)
#         if fc:
#             eligible &= np.isin(
#                 classes[candidates],
#                 np.asarray(fc, dtype=classes.dtype),
#             )

#         if not np.any(eligible):
#             return

#         candidates = candidates[eligible]
#         dist2 = dist2[eligible]
#         chosen_idx = int(candidates[int(np.argmin(dist2))])

#         mask = np.zeros(len(xyz), dtype=bool)
#         mask[chosen_idx] = True
#         self._apply_mask_and_record(mask, to_class)
        
#     def _draw_circle_preview_main(self, P1, P2):
#         """✅ PERFORMANCE FIX: Circle preview for MAIN VIEW (XY plane). One-time VTK pipeline init."""
#         renderWindow = self.interactor.GetRenderWindow()
#         renderer = renderWindow.GetRenderers().GetFirstRenderer()

#         x1, y1 = P1[0], P1[1]
#         x2, y2 = P2[0], P2[1]
#         center_x = (x1 + x2) / 2.0
#         center_y = (y1 + y2) / 2.0
#         radius = np.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2) / 2.0
#         if radius < 0.01:
#             return

#         num_segments = 64

#         # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
#         if not hasattr(self, "_circle_main_pts") or getattr(self, "circle_actor_main", None) is None:
#             from vtkmodules.vtkRenderingCore import vtkPolyDataMapper2D, vtkActor2D, vtkCoordinate as _C

#             self._circle_main_pts   = vtkPoints()
#             self._circle_main_lines = vtkCellArray()
#             self._circle_main_poly  = vtkPolyData()
#             self._circle_main_poly.SetPoints(self._circle_main_pts)
#             self._circle_main_poly.SetLines(self._circle_main_lines)

#             mapper = vtkPolyDataMapper2D()
#             mapper.SetInputData(self._circle_main_poly)
#             dc = _C(); dc.SetCoordinateSystemToDisplay()
#             mapper.SetTransformCoordinate(dc)

#             self.circle_actor_main = vtkActor2D()
#             self.circle_actor_main.SetMapper(mapper)
#             setattr(self.circle_actor_main, "_is_classification_preview", True)
#             setattr(self.circle_actor_main, "_naksha_preview_owner", "classification")
#             prop = self.circle_actor_main.GetProperty()
#             prop.SetColor(1.0, 1.0, 0.0)
#             prop.SetLineWidth(2)
#             prop.SetOpacity(1.0)

#             renderer.AddActor2D(self.circle_actor_main)

#         if not renderer.HasViewProp(self.circle_actor_main):
#             renderer.AddActor2D(self.circle_actor_main)
#         self.circle_actor_main.VisibilityOn()

#         # ── FAST GEOMETRY UPDATE ─────────────────────────────────────────────
#         if not hasattr(self, "_circle_main_coord"):
#             from vtkmodules.vtkRenderingCore import vtkCoordinate as _C
#             self._circle_main_coord = _C()
#             self._circle_main_coord.SetCoordinateSystemToWorld()

#         coord = self._circle_main_coord
#         theta = np.linspace(0, 2 * np.pi, num_segments + 1)
#         circle_x = center_x + radius * np.cos(theta)
#         circle_y = center_y + radius * np.sin(theta)

#         self._circle_main_pts.Reset()
#         self._circle_main_lines.Reset()

#         for i in range(num_segments + 1):
#             coord.SetValue(circle_x[i], circle_y[i], 0.0)
#             d = coord.GetComputedDisplayValue(renderer)
#             self._circle_main_pts.InsertNextPoint(d[0], d[1], 0)
#             if i > 0:
#                 self._circle_main_lines.InsertNextCell(2)
#                 self._circle_main_lines.InsertCellPoint(i - 1)
#                 self._circle_main_lines.InsertCellPoint(i)

#         self._circle_main_pts.Modified()
#         self._circle_main_poly.Modified()
#         # Render batched by caller

#     def _draw_circle_preview(self, P1, P2):
#         """
#         ✅ PERFORMANCE FIX: Reuse Actor2D — no RemoveActor/new objects per frame.
#         """
#         vtk_widget = self._get_active_vtk_widget()
#         if vtk_widget is None:
#             return
#         renderer = vtk_widget.renderer

#         u1, z1 = self._get_view_coordinates(P1)
#         u2, z2 = self._get_view_coordinates(P2)
#         center_u = (u1 + u2) / 2.0
#         center_z = (z1 + z2) / 2.0
#         radius = float(np.hypot(u2 - u1, z2 - z1)) / 2.0
#         if radius < 0.01:
#             return

#         num_segments = 64

#         # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
#         if not hasattr(self, "_circle_pts") or self.circle_actor is None:
#             from vtkmodules.vtkRenderingCore import vtkPolyDataMapper2D, vtkActor2D, vtkCoordinate as _C

#             self._circle_pts   = vtkPoints()
#             self._circle_lines = vtkCellArray()
#             self._circle_poly  = vtkPolyData()
#             self._circle_poly.SetPoints(self._circle_pts)
#             self._circle_poly.SetLines(self._circle_lines)

#             mapper = vtkPolyDataMapper2D()
#             mapper.SetInputData(self._circle_poly)
#             dc = _C(); dc.SetCoordinateSystemToDisplay()
#             mapper.SetTransformCoordinate(dc)

#             self.circle_actor = vtkActor2D()
#             self.circle_actor.SetMapper(mapper)
#             setattr(self.circle_actor, "_is_classification_preview", True)
#             setattr(self.circle_actor, "_naksha_preview_owner", "classification")
#             prop = self.circle_actor.GetProperty()
#             prop.SetColor(1.0, 1.0, 0.0)
#             prop.SetLineWidth(2)
#             prop.SetOpacity(1.0)

#             renderer.AddActor2D(self.circle_actor)

#         if not renderer.HasViewProp(self.circle_actor):
#             renderer.AddActor2D(self.circle_actor)
#         self.circle_actor.VisibilityOn()

#         # ── FAST GEOMETRY UPDATE ─────────────────────────────────────────────
#         if not hasattr(self, "_circle_coord"):
#             from vtkmodules.vtkRenderingCore import vtkCoordinate as _C
#             self._circle_coord = _C()
#             self._circle_coord.SetCoordinateSystemToWorld()

#         coord = self._circle_coord
#         self._circle_pts.Reset()
#         self._circle_lines.Reset()

#         theta = np.linspace(0, 2 * np.pi, num_segments + 1)
#         circle_u = center_u + radius * np.cos(theta)
#         circle_z = center_z + radius * np.sin(theta)

#         for i in range(num_segments + 1):
#             wx, wy, wz = self._view2d_to_world(circle_u[i], circle_z[i])
#             coord.SetValue(wx, wy, wz)
#             d = coord.GetComputedDisplayValue(renderer)
#             self._circle_pts.InsertNextPoint(d[0], d[1], 0)
#             if i > 0:
#                 self._circle_lines.InsertNextCell(2)
#                 self._circle_lines.InsertCellPoint(i - 1)
#                 self._circle_lines.InsertCellPoint(i)

#         self._circle_pts.Modified()
#         self._circle_poly.Modified()
#         # Render batched by caller

#     def _calculate_perpendicular_coord(self):
#         """Calculate perpendicular coordinate ONCE per drawing session"""
#         view_mode = getattr(self.app, 'cross_view_mode', 'side')
#         active_view = self._get_view_index_from_interactor()
        
#         if active_view is None:
#             return 0.0
        
#         core_mask = getattr(self.app, f"section_{active_view}_core_mask", None)
#         if core_mask is None or core_mask.sum() == 0:
#             return 0.0
        
#         section_xyz = self.app.data["xyz"][core_mask]
        
#         if view_mode == 'front':
#             return float(np.median(section_xyz[:, 0]))
#         else:
#             return float(np.median(section_xyz[:, 1]))

#     def _force_immediate_refresh(self):
#         """
#         Force immediate visual refresh after classification.
#         DOES NOT re-apply classifications, only updates colors/rendering.
        
#         IMPORTANT: This should ONLY refresh cross-section/cut-section views.
#         The main view has its own independent display settings and should NOT
#         be modified during cross-section classification operations.
#         """
#         print(f"\n{'='*60}")
#         print(f"🔄 FORCING VISUAL REFRESH (CROSS-SECTIONS ONLY)")
#         print(f"{'='*60}")
        
#         try:
#             # DO NOT REFRESH MAIN VIEW OR TRIGGER apply_class_map
#             # The main view has independent display mode settings that should
#             # not be affected by cross-section classification operations
#             print("⏭️ Skipping main view refresh (preserving independent display settings)")
#             print("⏭️ NOT calling apply_class_map (would trigger unwanted main view update)")
            
#             # 1. Refresh cross-section colors (ALWAYS) - Direct color update only
#             if hasattr(self.app, 'section_controller'):
#                 if hasattr(self.app.section_controller, 'current_section_indices'):
#                     if self.app.section_controller.current_section_indices is not None:
#                         print("🔄 Refreshing cross-section colors...")
#                         # Instead of calling apply_class_map, directly update colors
#                         self.app.section_controller.refresh_colors_direct()
#                         print("   ✅ Cross-section colors updated")
            
#             # 2. Refresh cut-section colors if active (ALWAYS) - Direct color update only
#             if hasattr(self.app, 'cut_section_controller'):
#                 if self.app.cut_section_controller.cut_points is not None:
#                     print("🔄 Refreshing cut-section colors...")
#                     # Direct color update without triggering apply_class_map
#                     self.app.cut_section_controller.refresh_colors_direct()
#                     print("   ✅ Cut-section colors updated")
            
#             # 3. Update status bar
#             self.app.statusBar().showMessage(
#                 "✅ Cross-section classification updated", 
#                 3000
#             )
            
#             print(f"{'='*60}")
#             print(f"✅ CROSS-SECTION REFRESH COMPLETE")
#             print(f"   Main view display settings preserved")
#             print(f"{'='*60}\n")
            
#         except Exception as e:
#             print(f"\n{'='*60}")
#             print(f"⚠️ ERROR DURING REFRESH: {e}")
#             print(f"{'='*60}\n")
#             import traceback
#             traceback.print_exc()

#     def test_palette_isolation(dialog):
#         """Test that view palettes are truly independent"""
#         print("\n" + "="*60)
#         print("🧪 TESTING PALETTE ISOLATION")
#         print("="*60)
        
#         # Test 1: Check dictionary IDs
#         print("\n1. Dictionary Identity Test:")
#         for i in range(5):
#             dict_id = id(dialog.view_palettes[i])
#             print(f"   Slot {i}: ID = {dict_id}")
        
#         # Test 2: Modify one slot
#         print("\n2. Modification Test:")
#         print("   Adding test class to slot 2...")
#         dialog.view_palettes[2][999] = {"show": True, "description": "TEST", "color": (255, 0, 0), "weight": 1.0}
        
#         for i in range(5):
#             has_test = 999 in dialog.view_palettes[i]
#             print(f"   Slot {i}: Has test class = {has_test} (should be True ONLY for slot 2)")
        
#         # Clean up
#         if 999 in dialog.view_palettes[2]:
#             del dialog.view_palettes[2][999]
        
#         print("\n✅ Test complete")
#         print("="*60 + "\n")

#     def _ensure_target_class_visible_single_view(self, to_class, view_index):
#         """
#         ✅ SAFE VERSION
#         Auto-enable target class ONLY if NO user filter is active.
#         """
#         try:
#             if not hasattr(self.app, 'display_mode_dialog'):
#                 return

#             dialog = self.app.display_mode_dialog

#             if view_index == -1:
#                 target_slot = 0
#                 view_name = "Main View"
#             else:
#                 target_slot = view_index + 1
#                 view_name = f"View {view_index + 1}"

#             print(f"      🔍 Checking {view_name} (slot {target_slot})...")

#             dialog.view_palettes.setdefault(target_slot, {})

#             # 🔒 USER FILTER GUARD (THIS IS THE FIX)
#             view_palette = dialog.view_palettes[target_slot]
#             visible_count = sum(1 for v in view_palette.values() if v.get("show", False))
#             total_count = len(view_palette)

#             if 0 < visible_count < total_count:
#                 print(f"         🔒 User filter active - NOT auto-enabling")
#                 return

#             if to_class in view_palette and view_palette[to_class].get("show", False):
#                 print(f"         ✓ Class {to_class} already visible")
#                 return

#             table = dialog.table
#             for row in range(table.rowCount()):
#                 code = int(table.item(row, 1).text())
#                 if code == to_class:
#                     class_desc = table.item(row, 2).text()
#                     color = table.item(row, 5).background().color().getRgb()[:3]
#                     weight_item = table.item(row, 6)
#                     weight = float(weight_item.text()) if weight_item else 1.0

#                     dialog.view_palettes[target_slot][to_class] = {
#                         "show": True,
#                         "description": class_desc,
#                         "color": tuple(map(int, color)),
#                         "weight": float(weight)
#                     }

#                     print(f"         ✅ Enabled class {to_class} in {view_name}")

#                     if target_slot == 0:
#                         self.app.class_palette[to_class]["show"] = True

#                     if dialog.current_slot == target_slot:
#                         chk = table.cellWidget(row, 0)
#                         if chk:
#                             chk.setChecked(True)

#                     break

#         except Exception as e:
#             print(f"         ⚠️ Error: {e}")
#             import traceback
#             traceback.print_exc()

#     def _refresh_only_active_dock(self):
#         """Refresh ONLY the currently active cross-section dock."""
#         try:
#             active = getattr(self.app.section_controller, "active_view", None)
#             if active is None:
#                 return

#             # Extract changed indices from the changed mask
#             if hasattr(self.app, '_last_changed_mask') and self.app._last_changed_mask is not None:
#                 changed_indices = np.flatnonzero(self.app._last_changed_mask)
#             else:
#                 changed_indices = np.array([], dtype=np.int64)

#             # refresh ONLY this view
#             self.app.section_controller.active_view = active
#             self.app.section_controller.update_section_colors_partial(changed_indices)


#             # render its vtk widget
#             if active in self.app.section_vtks:
#                 self._safe_render_pyvista(self.app.section_vtks[active])

#             print(f"🔄 Refreshed ONLY Cross-Section View {active+1}")

#         except Exception as e:
#             print(f"⚠️ Failed refreshing only active dock: {e}")

#     def _refresh_changed_points_in_all_docks(self):
#         """
#         Refresh ONLY the points modified by classification across all docks.
#         Uses each dock's own Display Mode palette.
#         """

#         if not hasattr(self.app, "_last_changed_mask"):
#             return

#         changed_mask = self.app._last_changed_mask
#         if changed_mask.sum() == 0:
#             return

#         # Extract changed indices from the changed mask
#         changed_indices = np.flatnonzero(changed_mask)

#         # All cross-section views
#         for view_idx, vtk_view in self.app.section_vtks.items():
#             # Set active view so refresh uses correct palette
#             old = self.app.section_controller.active_view
#             self.app.section_controller.active_view = view_idx

#             # Refresh only changed points
#             self.app.section_controller.update_section_colors_partial(changed_indices)

#             # Restore original active view
#             self.app.section_controller.active_view = old

#         print("🔄 Updated ONLY changed points across all views")
#     def _refresh_shaded_mode_partial(self):
#         """
#         ✅ PARTIAL SHADED REFRESH: Update ONLY triangles containing changed points
#         Much faster than full mesh rebuild - only affects classified area
#         """
#         if self.app.display_mode != "shaded_class":
#             return
        
#         if not hasattr(self.app, '_last_changed_mask') or self.app._last_changed_mask is None:
#             print("⏭️ No changed points mask for shaded refresh")
#             return
        
#         changed_mask = self.app._last_changed_mask
#         num_changed = np.sum(changed_mask)
        
#         if num_changed == 0:
#             return
        
#         print(f"\n{'='*60}")
#         print(f"🌗 PARTIAL SHADED REFRESH")
#         print(f"   Changed points: {num_changed:,}")
#         print(f"{'='*60}")
        
#         try:
#             import numpy as np
#             import pyvista as pv
            
#             # ============================================================
#             # STEP 1: Get existing mesh from VTK
#             # ============================================================
#             plotter = getattr(self.app.vtk_widget, "plotter", None)
#             if plotter is None or not plotter.renderer.actors:
#                 print("   ⚠️ No existing shaded mesh - doing full refresh")
#                 self._refresh_shaded_mode_after_classification()
#                 return
            
#             # Get the first actor (main shaded mesh)
#             actor = list(plotter.renderer.actors.values())[0]
#             _am = actor.GetMapper() if actor is not None else None
#             mesh = _am.GetInput() if _am is not None else None

#             if mesh is None or mesh.GetNumberOfCells() == 0:
#                 print("   ⚠️ No mesh cells - doing full refresh")
#                 self._refresh_shaded_mode_after_classification()
#                 return
            
#             print(f"   ✅ Found existing mesh with {mesh.GetNumberOfCells()} triangles")
            
#             # ============================================================
#             # STEP 2: Find which triangles contain changed points
#             # ============================================================
#             cells = np.asarray(mesh.faces).reshape(-1, 4)[:, 1:]  # Get triangle indices
            
#             # Check which triangles have at least one changed vertex
#             changed_indices = np.flatnonzero(changed_mask)
#             affected_triangles = np.isin(cells, changed_indices).any(axis=1)
#             num_affected = np.sum(affected_triangles)
            
#             print(f"   📊 Affected triangles: {num_affected:,} / {len(cells):,}")
            
#             if num_affected == 0:
#                 print("   ⏭️ No triangles affected")
#                 print(f"{'='*60}\n")
#                 return
            
#             # ============================================================
#             # STEP 3: Recompute colors ONLY for affected triangles
#             # ============================================================
#             xyz = self.app.data["xyz"]
#             classes = self.app.data["classification"]
            
#             # Get shading parameters
#             az = np.deg2rad(getattr(self.app, "last_shade_azimuth", 45.0))
#             el = np.deg2rad(getattr(self.app, "last_shade_angle", 45.0))
#             Ld = np.array([
#                 np.cos(el) * np.cos(az),
#                 np.cos(el) * np.sin(az),
#                 np.sin(el)
#             ])
#             Ld /= np.linalg.norm(Ld)
#             ambient = getattr(self.app, "shade_ambient", 0.2)
            
#             # Get current colors array
#             current_colors = np.asarray(mesh.GetCellData().GetArray("RGB"))
            
#             # Recompute ONLY affected triangles
#             affected_indices = np.flatnonzero(affected_triangles)
            
#             for idx in affected_indices:
#                 face = cells[idx]
                
#                 # Recompute normal
#                 v1, v2, v3 = xyz[face[0]], xyz[face[1]], xyz[face[2]]
#                 fn = np.cross(v2 - v1, v3 - v1)
#                 fn /= np.linalg.norm(fn) + 1e-9
                
#                 # Recompute shading
#                 shade = ambient + (1 - ambient) * np.clip(np.dot(fn, Ld), 0, 1)
                
#                 # Get majority class (with CURRENT classifications)
#                 c = classes[face]
#                 majority = np.bincount(c).argmax()
#                 entry = self.app.class_palette.get(int(majority), {"color": (128, 128, 128)})
                
#                 # Check visibility
#                 if not entry.get("show", True):
#                     current_colors[idx] = [0, 0, 0]  # Black for hidden
#                 else:
#                     base = np.array(entry["color"], dtype=np.float32)
#                     current_colors[idx] = np.clip(base * shade, 0, 255).astype(np.uint8)
            
#             print(f"   ✅ Recomputed colors for {len(affected_indices)} triangles")
            
#             # ============================================================
#             # STEP 4: Update mesh colors (in-place, no rebuild!)
#             # ============================================================
#             arr = pv.convert_array(current_colors, name="RGB")
#             mesh.GetCellData().RemoveArray("RGB")
#             mesh.GetCellData().AddArray(arr)
#             mesh.GetCellData().SetActiveScalars("RGB")
#             mesh.Modified()
            
#             # Single render
#             self._safe_render_pyvista(self.app.vtk_widget)
            
#             print(f"   ✅ Mesh updated in-place (no rebuild!)")
#             print(f"{'='*60}\n")
            
#         except Exception as e:
#             print(f"   ❌ Partial shaded refresh failed: {e}")
#             print(f"   ⚠️ Falling back to full refresh")
#             import traceback
#             traceback.print_exc()
            
#             # Fallback to full refresh if partial fails
#             self._refresh_shaded_mode_after_classification()
#             print(f"{'='*60}\n")

#     def _draw_circle_preview_cut(self, P1, P2):
#         """✅ PERFORMANCE FIX: Circle preview for CUT SECTION. One-time VTK pipeline init, geometry update only."""
#         import math

#         vtk_widget = self._get_active_vtk_widget()
#         if vtk_widget is None:
#             return
#         renderer = vtk_widget.renderer

#         P1 = tuple(P1) if not isinstance(P1[0], (list, tuple)) else tuple(P1[0])
#         P2 = tuple(P2) if not isinstance(P2[0], (list, tuple)) else tuple(P2[0])

#         num_segments = 64

#         # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
#         if not hasattr(self, "_circle_cut_pts") or getattr(self, "circle_actor_cut", None) is None:
#             from vtkmodules.vtkRenderingCore import vtkPolyDataMapper2D, vtkActor2D, vtkCoordinate as _C

#             self._circle_cut_pts   = vtkPoints()
#             self._circle_cut_lines = vtkCellArray()
#             self._circle_cut_poly  = vtkPolyData()
#             self._circle_cut_poly.SetPoints(self._circle_cut_pts)
#             self._circle_cut_poly.SetLines(self._circle_cut_lines)

#             mapper = vtkPolyDataMapper2D()
#             mapper.SetInputData(self._circle_cut_poly)
#             dc = _C(); dc.SetCoordinateSystemToDisplay()
#             mapper.SetTransformCoordinate(dc)

#             self.circle_actor_cut = vtkActor2D()
#             self.circle_actor_cut.SetMapper(mapper)
#             prop = self.circle_actor_cut.GetProperty()
#             prop.SetColor(1.0, 1.0, 0.0)
#             prop.SetLineWidth(2)
#             prop.SetOpacity(1.0)

#             renderer.AddActor2D(self.circle_actor_cut)

#         if not renderer.HasViewProp(self.circle_actor_cut):
#             renderer.AddActor2D(self.circle_actor_cut)
#         self.circle_actor_cut.VisibilityOn()

#         # ── FAST GEOMETRY UPDATE (world → display coords) ────────────────────
#         if not hasattr(self, "_circle_cut_coord"):
#             from vtkmodules.vtkRenderingCore import vtkCoordinate as _C
#             self._circle_cut_coord = _C()
#             self._circle_cut_coord.SetCoordinateSystemToWorld()

#         coord = self._circle_cut_coord
#         coord.SetValue(float(P1[0]), float(P1[1]), float(P1[2]))
#         display_p1 = coord.GetComputedDisplayValue(renderer)
#         coord.SetValue(float(P2[0]), float(P2[1]), float(P2[2]))
#         display_p2 = coord.GetComputedDisplayValue(renderer)

#         cx = (display_p1[0] + display_p2[0]) / 2.0
#         cy = (display_p1[1] + display_p2[1]) / 2.0
#         dx = display_p2[0] - display_p1[0]
#         dy = display_p2[1] - display_p1[1]
#         radius_display = max(5.0, math.sqrt(dx * dx + dy * dy) / 2.0)

#         self._circle_cut_pts.Reset()
#         self._circle_cut_lines.Reset()

#         for i in range(num_segments + 1):
#             angle = 2.0 * math.pi * i / num_segments
#             self._circle_cut_pts.InsertNextPoint(
#                 cx + radius_display * math.cos(angle),
#                 cy + radius_display * math.sin(angle),
#                 0.0)
#             if i > 0:
#                 self._circle_cut_lines.InsertNextCell(2)
#                 self._circle_cut_lines.InsertCellPoint(i - 1)
#                 self._circle_cut_lines.InsertCellPoint(i)

#         self._circle_cut_pts.Modified()
#         self._circle_cut_poly.Modified()
#         # Render batched by caller



#     def _draw_line_preview_cut(self, P1, P2):
#         """
#         ✅ PERFORMANCE FIX: Line preview for CUT SECTION.
#         Reuses Actor2D — no RemoveActor/new objects per frame. Zero print spam.
#         """
#         from vtkmodules.vtkRenderingCore import vtkCoordinate, vtkPolyDataMapper2D, vtkActor2D

#         vtk_widget = self._get_active_vtk_widget()
#         if vtk_widget is None:
#             return
#         renderer = vtk_widget.renderer

#         # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
#         if not hasattr(self, "_line_cut_pts") or self.line_actor_cut is None:
#             self._line_cut_pts = vtkPoints()
#             self._line_cut_pts.SetNumberOfPoints(2)
#             _lines = vtkCellArray()
#             _lines.InsertNextCell(2)
#             _lines.InsertCellPoint(0)
#             _lines.InsertCellPoint(1)
#             self._line_cut_poly = vtkPolyData()
#             self._line_cut_poly.SetPoints(self._line_cut_pts)
#             self._line_cut_poly.SetLines(_lines)

#             mapper = vtkPolyDataMapper2D()
#             mapper.SetInputData(self._line_cut_poly)
#             dc = vtkCoordinate(); dc.SetCoordinateSystemToDisplay()
#             mapper.SetTransformCoordinate(dc)

#             self.line_actor_cut = vtkActor2D()
#             self.line_actor_cut.SetMapper(mapper)
#             prop = self.line_actor_cut.GetProperty()
#             prop.SetColor(1, 1, 0)
#             prop.SetLineWidth(2)
#             prop.SetOpacity(1.0)

#             renderer.AddActor2D(self.line_actor_cut)

#         if not renderer.HasViewProp(self.line_actor_cut):
#             renderer.AddActor2D(self.line_actor_cut)
#         self.line_actor_cut.VisibilityOn()

#         # ── FAST COORDINATE UPDATE ───────────────────────────────────────────
#         if not hasattr(self, "_line_cut_coord"):
#             self._line_cut_coord = vtkCoordinate()
#             self._line_cut_coord.SetCoordinateSystemToWorld()

#         coord = self._line_cut_coord
#         coord.SetValue(float(P1[0]), float(P1[1]), float(P1[2]))
#         d1 = coord.GetComputedDisplayValue(renderer)
#         coord.SetValue(float(P2[0]), float(P2[1]), float(P2[2]))
#         d2 = coord.GetComputedDisplayValue(renderer)

#         self._line_cut_pts.SetPoint(0, d1[0], d1[1], 0)
#         self._line_cut_pts.SetPoint(1, d2[0], d2[1], 0)
#         self._line_cut_pts.Modified()
#         self._line_cut_poly.Modified()

#         # Vertical dashed guides for above/below line
#         tool = getattr(self.app, "active_classify_tool", None)
#         if tool in ("above_line", "below_line", "parallel_line"):
#             self._draw_vertical_guides_cut(P1, P2, tool, renderer)
#         # Render batched by caller


#     def _draw_vertical_guides_cut(self, P1, P2, tool, renderer):
#         """
#         ✅ PERFORMANCE FIX: Vertical dashed guides for CUT SECTION above/below line.
#         Reuses Actor2D — no RemoveActor/new objects per frame. Zero render call here.
#         """
#         from vtkmodules.vtkRenderingCore import vtkCoordinate, vtkPolyDataMapper2D, vtkActor2D

#         # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
#         if not hasattr(self, "_dotted_cut_pts") or self.dotted_actor_cut is None:
#             self._dotted_cut_pts   = vtkPoints()
#             self._dotted_cut_lines = vtkCellArray()
#             self._dotted_cut_poly  = vtkPolyData()
#             self._dotted_cut_poly.SetPoints(self._dotted_cut_pts)
#             self._dotted_cut_poly.SetLines(self._dotted_cut_lines)

#             mapper = vtkPolyDataMapper2D()
#             mapper.SetInputData(self._dotted_cut_poly)
#             dc = vtkCoordinate(); dc.SetCoordinateSystemToDisplay()
#             mapper.SetTransformCoordinate(dc)

#             self.dotted_actor_cut = vtkActor2D()
#             self.dotted_actor_cut.SetMapper(mapper)
#             prop = self.dotted_actor_cut.GetProperty()
#             prop.SetColor(1, 1, 0)
#             prop.SetLineWidth(2)
#             prop.SetOpacity(0.8)

#             renderer.AddActor2D(self.dotted_actor_cut)

#         if not renderer.HasViewProp(self.dotted_actor_cut):
#             renderer.AddActor2D(self.dotted_actor_cut)

#         # ── FAST COORDINATE UPDATE ───────────────────────────────────────────
#         if not hasattr(self, "_dotted_cut_coord"):
#             self._dotted_cut_coord = vtkCoordinate()
#             self._dotted_cut_coord.SetCoordinateSystemToWorld()

#         coord = self._dotted_cut_coord
#         coord.SetValue(float(P1[0]), float(P1[1]), float(P1[2]))
#         p1_disp = coord.GetComputedDisplayValue(renderer)
#         coord.SetValue(float(P2[0]), float(P2[1]), float(P2[2]))
#         p2_disp = coord.GetComputedDisplayValue(renderer)

#         if tool == "parallel_line":
#             self._draw_parallel_band_guides_cut_display(P1, P2, renderer, p1_disp, p2_disp)
#             return

#         try:
#             win_size = renderer.GetRenderWindow().GetSize()
#             height = win_size[1]
#         except Exception:
#             height = 600

#         seg_len = 10
#         gap = 6

#         pts   = self._dotted_cut_pts
#         lines = self._dotted_cut_lines
#         pts.Reset()
#         lines.Reset()
#         idx = 0

#         def fill(x_disp, y_start):
#             nonlocal idx
#             if tool == "above_line":
#                 y, y_end, step = y_start, height, seg_len + gap
#                 while y < y_end:
#                     y2 = min(y + seg_len, y_end)
#                     pts.InsertNextPoint(x_disp, y, 0)
#                     pts.InsertNextPoint(x_disp, y2, 0)
#                     lines.InsertNextCell(2)
#                     lines.InsertCellPoint(idx); lines.InsertCellPoint(idx + 1)
#                     idx += 2; y += step
#             else:
#                 y, y_end, step = y_start, 0, seg_len + gap
#                 while y > y_end:
#                     y2 = max(y - seg_len, y_end)
#                     pts.InsertNextPoint(x_disp, y, 0)
#                     pts.InsertNextPoint(x_disp, y2, 0)
#                     lines.InsertNextCell(2)
#                     lines.InsertCellPoint(idx); lines.InsertCellPoint(idx + 1)
#                     idx += 2; y -= step

#         fill(p1_disp[0], p1_disp[1])
#         fill(p2_disp[0], p2_disp[1])

#         if idx > 0:
#             pts.Modified()
#             self._dotted_cut_poly.Modified()
#             self.dotted_actor_cut.VisibilityOn()
#         else:
#             self.dotted_actor_cut.VisibilityOff()
#         # Render batched by caller

#     def _draw_parallel_band_guides_cut_display(self, P1, P2, renderer, p1_disp, p2_disp):
#         tol_above, tol_below = self._get_parallel_line_tolerances()
#         mid_world = (
#             (float(P1[0]) + float(P2[0])) * 0.5,
#             (float(P1[1]) + float(P2[1])) * 0.5,
#             (float(P1[2]) + float(P2[2])) * 0.5,
#         )
#         vtk_widget = self._get_active_vtk_widget()
#         if vtk_widget is None:
#             self.dotted_actor_cut.VisibilityOff()
#             return

#         view_units_per_px = float(self._pixel_radius_to_view_radius(vtk_widget, mid_world, 1.0))
#         if (not np.isfinite(view_units_per_px)) or (view_units_per_px <= 1e-9):
#             self.dotted_actor_cut.VisibilityOff()
#             return

#         above_px = float(tol_above / view_units_per_px)
#         below_px = float(tol_below / view_units_per_px)
#         if (not np.isfinite(above_px)) or (not np.isfinite(below_px)):
#             self.dotted_actor_cut.VisibilityOff()
#             return
#         rw, rh = renderer.GetSize()
#         max_px = max(float(rw), float(rh), 1.0) * 4.0
#         above_px = max(-max_px, min(above_px, max_px))
#         below_px = max(-max_px, min(below_px, max_px))

#         x1, y1 = float(p1_disp[0]), float(p1_disp[1])
#         x2, y2 = float(p2_disp[0]), float(p2_disp[1])
#         dx = x2 - x1
#         dy = y2 - y1
#         seg_len = float(np.hypot(dx, dy))
#         if seg_len <= 1e-9:
#             self.dotted_actor_cut.VisibilityOff()
#             return

#         nx = -dy / seg_len
#         ny = dx / seg_len
#         # Screen-space Y grows downward. Keep "above" toward visual upward.
#         if ny > 0.0:
#             nx, ny = -nx, -ny

#         up1 = (x1 + nx * above_px, y1 + ny * above_px, 0.0)
#         up2 = (x2 + nx * above_px, y2 + ny * above_px, 0.0)
#         lo1 = (x1 - nx * below_px, y1 - ny * below_px, 0.0)
#         lo2 = (x2 - nx * below_px, y2 - ny * below_px, 0.0)

#         pts = self._dotted_cut_pts
#         lines = self._dotted_cut_lines
#         pts.Reset()
#         lines.Reset()
#         for p in (up1, up2, lo1, lo2):
#             if not (np.isfinite(p[0]) and np.isfinite(p[1])):
#                 self.dotted_actor_cut.VisibilityOff()
#                 return
#             pts.InsertNextPoint(*p)
#         lines.InsertNextCell(2)
#         lines.InsertCellPoint(0)
#         lines.InsertCellPoint(1)
#         lines.InsertNextCell(2)
#         lines.InsertCellPoint(2)
#         lines.InsertCellPoint(3)
#         pts.Modified()
#         self._dotted_cut_poly.Modified()
#         self.dotted_actor_cut.VisibilityOn()

#     def _draw_rectangle_preview_cut(self, P1, P2):
#         """✅ PERFORMANCE FIX: Rectangle preview for CUT SECTION. One-time VTK pipeline init."""
#         vtk_widget = self._get_active_vtk_widget()
#         if vtk_widget is None:
#             return
#         renderer = vtk_widget.renderer

#         # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
#         if not hasattr(self, "_rect_cut_pts") or getattr(self, "rect_actor_cut", None) is None:
#             from vtkmodules.vtkRenderingCore import vtkCoordinate, vtkPolyDataMapper2D, vtkActor2D

#             self._rect_cut_pts = vtkPoints()
#             self._rect_cut_pts.SetNumberOfPoints(5)
#             _lines = vtkCellArray()
#             _lines.InsertNextCell(5)
#             for i in range(5):
#                 _lines.InsertCellPoint(i)
#             self._rect_cut_poly = vtkPolyData()
#             self._rect_cut_poly.SetPoints(self._rect_cut_pts)
#             self._rect_cut_poly.SetLines(_lines)

#             mapper = vtkPolyDataMapper2D()
#             mapper.SetInputData(self._rect_cut_poly)
#             dc = vtkCoordinate(); dc.SetCoordinateSystemToDisplay()
#             mapper.SetTransformCoordinate(dc)

#             self.rect_actor_cut = vtkActor2D()
#             self.rect_actor_cut.SetMapper(mapper)
#             prop = self.rect_actor_cut.GetProperty()
#             prop.SetColor(1, 1, 0)
#             prop.SetLineWidth(2)
#             prop.SetOpacity(1.0)

#             renderer.AddActor2D(self.rect_actor_cut)

#         if not renderer.HasViewProp(self.rect_actor_cut):
#             renderer.AddActor2D(self.rect_actor_cut)
#         self.rect_actor_cut.VisibilityOn()

#         # ── FAST COORDINATE UPDATE (world → display) ─────────────────────────
#         if not hasattr(self, "_rect_cut_coord"):
#             from vtkmodules.vtkRenderingCore import vtkCoordinate
#             self._rect_cut_coord = vtkCoordinate()
#             self._rect_cut_coord.SetCoordinateSystemToWorld()

#         coord = self._rect_cut_coord
#         coord.SetValue(P1[0], P1[1], P1[2])
#         d1 = coord.GetComputedDisplayValue(renderer)
#         coord.SetValue(P2[0], P2[1], P2[2])
#         d2 = coord.GetComputedDisplayValue(renderer)

#         x1, y1 = d1[0], d1[1]
#         x2, y2 = d2[0], d2[1]
#         corners = [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]
#         for i, (cx, cy) in enumerate(corners):
#             self._rect_cut_pts.SetPoint(i, cx, cy, 0)

#         self._rect_cut_pts.Modified()
#         self._rect_cut_poly.Modified()
#         # Render batched by caller


#     def _draw_brush_preview_cut(self, center, radius_world):
#         """Brush preview for CUT SECTION - same as circle."""
#         self._draw_circle_preview_cut(center, radius_world)


    
#     def _draw_freehand_preview_cut(self, live_point=None):
#         """Smooth freehand preview for CUT SECTION. One-time init, Catmull-Rom curves per frame."""
#         if len(self.drawing_points) < 1:
#             return

#         all_pts = list(self.drawing_points_display_cut) if hasattr(self, "drawing_points_display_cut") else []
#         if live_point:
#             all_pts.append(live_point)
#         if len(all_pts) < 2:
#             return

#         # ── SMOOTH: Catmull-Rom on screen-space (display) coords ─────────────
#         all_pts = self._catmull_rom_smooth(all_pts, steps=10)

#         vtk_widget = self._get_active_vtk_widget()
#         if vtk_widget is None:
#             return
#         renderer = vtk_widget.renderer

#         n = len(all_pts)

#         # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
#         if not hasattr(self, "_freehand_cut_pts") or getattr(self, "freehand_actor_cut", None) is None:
#             from vtkmodules.vtkRenderingCore import vtkPolyDataMapper2D, vtkActor2D

#             self._freehand_cut_pts   = vtkPoints()
#             self._freehand_cut_lines = vtkCellArray()
#             self._freehand_cut_poly  = vtkPolyData()
#             self._freehand_cut_poly.SetPoints(self._freehand_cut_pts)
#             self._freehand_cut_poly.SetLines(self._freehand_cut_lines)

#             mapper = vtkPolyDataMapper2D()
#             mapper.SetInputData(self._freehand_cut_poly)

#             self.freehand_actor_cut = vtkActor2D()
#             self.freehand_actor_cut.SetMapper(mapper)
#             prop = self.freehand_actor_cut.GetProperty()
#             prop.SetColor(1, 0.8, 0)
#             prop.SetLineWidth(2)
#             prop.SetOpacity(0.8)

#             renderer.AddActor2D(self.freehand_actor_cut)

#         if not renderer.HasViewProp(self.freehand_actor_cut):
#             renderer.AddActor2D(self.freehand_actor_cut)
#         self.freehand_actor_cut.VisibilityOn()

#         # ── FAST GEOMETRY UPDATE (display coords — no world conversion) ──────
#         pts   = self._freehand_cut_pts
#         lines = self._freehand_cut_lines
#         pts.Reset()
#         lines.Reset()

#         for pt in all_pts:
#             pts.InsertNextPoint(pt[0], pt[1], 0.0)
#         for i in range(n - 1):
#             lines.InsertNextCell(2)
#             lines.InsertCellPoint(i)
#             lines.InsertCellPoint(i + 1)

#         pts.Modified()
#         self._freehand_cut_poly.Modified()
#         self._safe_render(vtk_widget)


#     def _draw_brush_cursor_cut(self, center, radius=1.0):
#         """Brush cursor for CUT SECTION - dashed circle with crosshair."""
#         from vtkmodules.vtkRenderingCore import vtkCoordinate, vtkPolyDataMapper2D, vtkActor2D
#         from vtkmodules.vtkCommonCore import vtkPoints
#         from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
#         import numpy as np
        
#         print(f"🖌️ _draw_brush_cursor_cut called: center={center}, radius={radius}")
        
#         vtk_widget = self._get_active_vtk_widget()
#         if vtk_widget is None:
#             print("❌ No VTK widget for brush cursor (cut section)")
#             return
        
#         # ✅ CRITICAL FIX: Get renderer properly
#         if hasattr(vtk_widget, 'renderer'):
#             renderer = vtk_widget.renderer
#         elif hasattr(vtk_widget, 'GetRenderer'):
#             renderer = vtk_widget.GetRenderer()
#         else:
#             rw = vtk_widget.GetRenderWindow()
#             renderer = rw.GetRenderers().GetFirstRenderer()
        
#         print(f"   ✅ Got renderer: {renderer}")
        
#         # Remove previous brush actor
#         if hasattr(self, "brush_actor_cut") and self.brush_actor_cut:
#             try:
#                 renderer.RemoveActor2D(self.brush_actor_cut)
#                 print("   🧹 Removed old brush_actor_cut")
#             except Exception as e:
#                 print(f"   ⚠️ Could not remove old brush_actor_cut: {e}")
#             self.brush_actor_cut = None
        
#         cu, cv = center
        
#         # Force minimum radius
#         if radius < 0.05:
#             radius = 0.1
        
#         print(f"   📐 Creating brush shape: center=({cu:.2f}, {cv:.2f}), radius={radius:.2f}")
        
#         # ========== CREATE BRUSH SHAPE IN WORLD COORDINATES ==========
#         num_segments = 32
#         brush_world = []
        
#         # Outer circle (every other segment for dashed effect)
#         for i in range(0, num_segments, 2):  # Skip every other segment
#             angle1 = 2 * np.pi * i / num_segments
#             angle2 = 2 * np.pi * (i + 1) / num_segments
            
#             u1 = cu + radius * np.cos(angle1)
#             v1 = cv + radius * np.sin(angle1)
#             brush_world.append(self._view2d_to_world(u1, v1))
            
#             u2 = cu + radius * np.cos(angle2)
#             v2 = cv + radius * np.sin(angle2)
#             brush_world.append(self._view2d_to_world(u2, v2))
        
#         # Add crosshair lines
#         crosshair_size = radius * 0.3  # 30% of radius
        
#         # Horizontal crosshair
#         brush_world.append(self._view2d_to_world(cu - crosshair_size, cv))
#         brush_world.append(self._view2d_to_world(cu + crosshair_size, cv))
        
#         # Vertical crosshair
#         brush_world.append(self._view2d_to_world(cu, cv - crosshair_size))
#         brush_world.append(self._view2d_to_world(cu, cv + crosshair_size))
        
#         # Center dot (small circle)
#         dot_segments = 8
#         dot_radius = radius * 0.05
#         for i in range(dot_segments + 1):
#             angle = 2 * np.pi * i / dot_segments
#             u = cu + dot_radius * np.cos(angle)
#             v = cv + dot_radius * np.sin(angle)
#             brush_world.append(self._view2d_to_world(u, v))
        
#         print(f"   ✅ Created {len(brush_world)} world coordinate points")
        
#         # ========== CONVERT TO DISPLAY COORDINATES ==========
#         pts = vtkPoints()
#         coord = vtkCoordinate()
#         coord.SetCoordinateSystemToWorld()
        
#         for point in brush_world:
#             coord.SetValue(point[0], point[1], point[2])
#             display_pos = coord.GetComputedDisplayValue(renderer)
#             pts.InsertNextPoint(display_pos[0], display_pos[1], 0)
        
#         print(f"   ✅ Converted to display coordinates")
        
#         # ========== CREATE LINE SEGMENTS ==========
#         lines = vtkCellArray()
        
#         # Dashed circle (pairs of points)
#         num_circle_points = num_segments  # Each dash is 2 points
#         for i in range(0, num_circle_points, 2):
#             lines.InsertNextCell(2)
#             lines.InsertCellPoint(i)
#             lines.InsertCellPoint(i + 1)
        
#         # Horizontal crosshair
#         h_start = num_circle_points
#         lines.InsertNextCell(2)
#         lines.InsertCellPoint(h_start)
#         lines.InsertCellPoint(h_start + 1)
        
#         # Vertical crosshair
#         v_start = h_start + 2
#         lines.InsertNextCell(2)
#         lines.InsertCellPoint(v_start)
#         lines.InsertCellPoint(v_start + 1)
        
#         # Center dot
#         dot_start = v_start + 2
#         for i in range(dot_segments):
#             lines.InsertNextCell(2)
#             lines.InsertCellPoint(dot_start + i)
#             lines.InsertCellPoint(dot_start + i + 1)
        
#         # ========== CREATE 2D ACTOR ==========
#         poly = vtkPolyData()
#         poly.SetPoints(pts)
#         poly.SetLines(lines)
        
#         mapper = vtkPolyDataMapper2D()
#         mapper.SetInputData(poly)
        
#         coordinate = vtkCoordinate()
#         coordinate.SetCoordinateSystemToDisplay()
#         mapper.SetTransformCoordinate(coordinate)
        
#         self.brush_actor_cut = vtkActor2D()
#         self.brush_actor_cut.SetMapper(mapper)
        
#         # Set properties - Yellow like other tools
#         prop = self.brush_actor_cut.GetProperty()
#         prop.SetColor(1, 1, 0)  # Yellow
#         prop.SetLineWidth(3)
#         prop.SetOpacity(0.9)
        
#         # Add as 2D actor (always on top!)
#         renderer.AddActor2D(self.brush_actor_cut)
#         print(f"   ✅ Added brush_actor_cut to renderer")
#         print(f"   📊 Renderer now has {renderer.GetActors2D().GetNumberOfItems()} 2D actors")
        
#         # ✅ CRITICAL: Force immediate render
#         try:
#             if hasattr(vtk_widget, 'GetRenderWindow'):
#                 self._safe_render(vtk_widget)
#             elif hasattr(vtk_widget, 'render'):
#                 self._safe_render_pyvista(vtk_widget)
#             print(f"   ✅ Render called successfully")
#         except Exception as e:
#             print(f"   ⚠️ Render failed: {e}")
            
#     def _draw_brush_cursor_cut_display(self, center_world, radius_px=20.0):
#         """
#         ✅ PERFORMANCE FIX: Brush cursor for CUT SECTION in DISPLAY coords.
#         Reuses actor — zero allocation per frame after init.
#         """
#         from vtkmodules.vtkRenderingCore import vtkCoordinate, vtkPolyDataMapper2D, vtkActor2D

#         vtk_widget = self._get_active_vtk_widget()
#         if vtk_widget is None:
#             return
#         renderer = vtk_widget.renderer

#         # Convert center to display coords
#         coord = vtkCoordinate()
#         coord.SetCoordinateSystemToWorld()
#         coord.SetValue(float(center_world[0]), float(center_world[1]), float(center_world[2]))
#         c_disp = coord.GetComputedDisplayValue(renderer)
#         cx, cy = float(c_disp[0]), float(c_disp[1])
#         pixel_radius = max(6.0, float(radius_px))

#         num_segments = 32
#         dot_segments = 8

#         # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
#         if not hasattr(self, "_brush_cut_pts") or self.brush_actor_cut is None:
#             if getattr(self, "brush_actor_cut", None) is not None:
#                 try:
#                     renderer.RemoveActor2D(self.brush_actor_cut)
#                 except Exception:
#                     pass
#                 self.brush_actor_cut = None

#             self._brush_cut_pts   = vtkPoints()
#             self._brush_cut_lines = vtkCellArray()
#             self._brush_cut_poly  = vtkPolyData()
#             self._brush_cut_poly.SetPoints(self._brush_cut_pts)
#             self._brush_cut_poly.SetLines(self._brush_cut_lines)

#             mapper = vtkPolyDataMapper2D()
#             mapper.SetInputData(self._brush_cut_poly)
#             dc = vtkCoordinate(); dc.SetCoordinateSystemToDisplay()
#             mapper.SetTransformCoordinate(dc)

#             self.brush_actor_cut = vtkActor2D()
#             self.brush_actor_cut.SetMapper(mapper)
#             prop = self.brush_actor_cut.GetProperty()
#             prop.SetColor(1, 1, 0)
#             prop.SetLineWidth(3)
#             prop.SetOpacity(0.9)

#             renderer.AddActor2D(self.brush_actor_cut)

#         if not renderer.HasViewProp(self.brush_actor_cut):
#             renderer.AddActor2D(self.brush_actor_cut)
#         self.brush_actor_cut.VisibilityOn()

#         # ── FAST GEOMETRY UPDATE ─────────────────────────────────────────────
#         pts   = self._brush_cut_pts
#         lines = self._brush_cut_lines
#         pts.Reset()
#         lines.Reset()

#         def add_seg(p0, p1):
#             i0 = pts.InsertNextPoint(p0[0], p0[1], 0.0)
#             i1 = pts.InsertNextPoint(p1[0], p1[1], 0.0)
#             lines.InsertNextCell(2)
#             lines.InsertCellPoint(i0)
#             lines.InsertCellPoint(i1)

#         for i in range(0, num_segments, 2):
#             a1 = 2.0 * np.pi * i / num_segments
#             a2 = 2.0 * np.pi * (i + 1) / num_segments
#             add_seg((cx + pixel_radius * np.cos(a1), cy + pixel_radius * np.sin(a1)),
#                     (cx + pixel_radius * np.cos(a2), cy + pixel_radius * np.sin(a2)))

#         ch = pixel_radius * 0.3
#         add_seg((cx - ch, cy), (cx + ch, cy))
#         add_seg((cx, cy - ch), (cx, cy + ch))

#         dot_r = max(2.0, pixel_radius * 0.05)
#         for i in range(dot_segments):
#             a1 = 2.0 * np.pi * i / dot_segments
#             a2 = 2.0 * np.pi * (i + 1) / dot_segments
#             add_seg((cx + dot_r * np.cos(a1), cy + dot_r * np.sin(a1)),
#                     (cx + dot_r * np.cos(a2), cy + dot_r * np.sin(a2)))

#         pts.Modified()
#         self._brush_cut_poly.Modified()
#         # Render batched by caller
        
#     def _draw_rectangle_cursor_cut_display(self, center_world, radius_px=20.0):
#         """
#         ✅ PERFORMANCE FIX: Rectangle brush cursor for CUT SECTION in DISPLAY coords.
#         Reuses actor — zero allocation per frame after init.
#         """
#         from vtkmodules.vtkRenderingCore import vtkCoordinate, vtkPolyDataMapper2D, vtkActor2D

#         vtk_widget = self._get_active_vtk_widget()
#         if vtk_widget is None:
#             return
#         renderer = vtk_widget.renderer

#         coord = vtkCoordinate()
#         coord.SetCoordinateSystemToWorld()
#         coord.SetValue(float(center_world[0]), float(center_world[1]), float(center_world[2]))
#         c_disp = coord.GetComputedDisplayValue(renderer)
#         cx, cy = float(c_disp[0]), float(c_disp[1])
#         dx = dy = max(6.0, float(radius_px))
#         x_min, x_max = cx - dx, cx + dx
#         y_min, y_max = cy - dy, cy + dy

#         # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
#         if not hasattr(self, "_rect_cut_pts") or self.brush_actor_cut is None:
#             if getattr(self, "brush_actor_cut", None) is not None:
#                 try:
#                     renderer.RemoveActor2D(self.brush_actor_cut)
#                 except Exception:
#                     pass
#                 self.brush_actor_cut = None

#             self._rect_cut_pts   = vtkPoints()
#             self._rect_cut_lines = vtkCellArray()
#             self._rect_cut_poly  = vtkPolyData()
#             self._rect_cut_poly.SetPoints(self._rect_cut_pts)
#             self._rect_cut_poly.SetLines(self._rect_cut_lines)

#             mapper = vtkPolyDataMapper2D()
#             mapper.SetInputData(self._rect_cut_poly)
#             dc = vtkCoordinate(); dc.SetCoordinateSystemToDisplay()
#             mapper.SetTransformCoordinate(dc)

#             self.brush_actor_cut = vtkActor2D()
#             self.brush_actor_cut.SetMapper(mapper)
#             prop = self.brush_actor_cut.GetProperty()
#             prop.SetColor(1, 1, 0)
#             prop.SetLineWidth(3)
#             prop.SetOpacity(0.9)

#             renderer.AddActor2D(self.brush_actor_cut)

#         if not renderer.HasViewProp(self.brush_actor_cut):
#             renderer.AddActor2D(self.brush_actor_cut)
#         self.brush_actor_cut.VisibilityOn()

#         # ── FAST GEOMETRY UPDATE ─────────────────────────────────────────────
#         pts   = self._rect_cut_pts
#         lines = self._rect_cut_lines
#         pts.Reset()
#         lines.Reset()

#         corners = [
#             (x_min, y_min, 0.0),
#             (x_max, y_min, 0.0),
#             (x_max, y_max, 0.0),
#             (x_min, y_max, 0.0),
#             (x_min, y_min, 0.0),
#         ]
#         for c in corners:
#             pts.InsertNextPoint(*c)
#         for i in range(1, len(corners)):
#             lines.InsertNextCell(2)
#             lines.InsertCellPoint(i - 1)
#             lines.InsertCellPoint(i)

#         pts.Modified()
#         self._rect_cut_poly.Modified()
#         # Render batched by caller

#     # Add this helper method to ClassificationInteractor class

#     def _transform_cut_to_world_rect(self, P1, P2):
#         """
#         Transform rectangle coordinates from cut section's local space 
#         to global world space for classification.
        
#         Returns: (u_min, u_max, z_min, z_max) in world coordinates
#         """
#         # Check if we're in cut section mode
#         is_cut_section = getattr(self, 'is_cut_section', False) or \
#                         (hasattr(self.app, 'cut_section_controller') and
#                         getattr(self.app.cut_section_controller, 'is_cut_view_active', False))
        
#         if not is_cut_section:
#             # Normal cross-section - use coordinates as-is
#             u1, z1 = P1[0], P1[2]
#             u2, z2 = P2[0], P2[2]
#             return (min(u1, u2), max(u1, u2), min(z1, z2), max(z1, z2))
        
#         # ✅ CUT SECTION: Transform using cutting plane basis
#         try:
#             cut_ctrl = self.app.cut_section_controller
            
#             # Get the cutting plane's coordinate system
#             origin = np.array(cut_ctrl.plane_origin)  # Cut plane origin in world space
#             normal = np.array(cut_ctrl.plane_normal)  # Cut plane normal (perpendicular to view)
            
#             # Build the cut section's local coordinate system
#             # u_axis: along the cutting direction (horizontal in cut view)
#             # v_axis: perpendicular to normal and u_axis (vertical in cut view)
#             # normal: points "out of screen" in cut view
            
#             # Get u_axis from the plane definition
#             if hasattr(cut_ctrl, 'u_axis'):
#                 u_axis = np.array(cut_ctrl.u_axis)
#             else:
#                 # Fallback: compute perpendicular to normal
#                 if abs(normal[2]) < 0.9:
#                     u_axis = np.cross(normal, [0, 0, 1])
#                 else:
#                     u_axis = np.cross(normal, [0, 1, 0])
            
#             u_axis = u_axis / np.linalg.norm(u_axis)
#             v_axis = np.cross(normal, u_axis)
#             v_axis = v_axis / np.linalg.norm(v_axis)
            
#             # Transform P1 and P2 from local cut coordinates to world coordinates
#             # Local coords: (u, 0, z) where u is horizontal, z is vertical in cut view
#             # World coords: origin + u*u_axis + z*v_axis
            
#             def local_to_world(p):
#                 """Convert (u, y, z) in cut section to (x, y, z) in world"""
#                 u_local = p[0]  # Horizontal in cut view
#                 z_local = p[2]  # Vertical in cut view
                
#                 # Transform to world space
#                 world_pt = origin + u_local * u_axis + z_local * v_axis
#                 return world_pt
            
#             # Transform both corners
#             world_p1 = local_to_world(P1)
#             world_p2 = local_to_world(P2)
            
#             # Now we need to define the rectangle in the cutting plane's 2D space
#             # that corresponds to the 3D world rectangle
            
#             # Project world corners back to local u,z coordinates
#             u1 = np.dot(world_p1 - origin, u_axis)
#             z1 = np.dot(world_p1 - origin, v_axis)
#             u2 = np.dot(world_p2 - origin, u_axis)
#             z2 = np.dot(world_p2 - origin, v_axis)
            
#             print(f"   🔄 Cut section rect transform:")
#             print(f"      Local: P1=({P1[0]:.2f}, {P1[2]:.2f}), P2=({P2[0]:.2f}, {P2[2]:.2f})")
#             print(f"      World: u1={u1:.2f}, z1={z1:.2f}, u2={u2:.2f}, z2={z2:.2f}")
            
#             return (min(u1, u2), max(u1, u2), min(z1, z2), max(z1, z2))
            
#         except Exception as e:
#             print(f"   ⚠️ Cut section transform failed: {e}")
#             import traceback
#             traceback.print_exc()
            
#             # Fallback to direct coordinates
#             u1, z1 = P1[0], P1[2]
#             u2, z2 = P2[0], P2[2]
#             return (min(u1, u2), max(u1, u2), min(z1, z2), max(z1, z2))

#     def _partial_refresh_main_view(self, changed_indices):
#         """Fast main-view color update (no full rebuild)."""
#         try:
#             from gui.performance_optimizations import fast_update_colors_optimized
#             fast_update_colors_optimized(self.app, changed_mask=changed_indices)
#             print(f"⚡ Partial main-view refresh: {len(changed_indices)} points updated")
#         except Exception as e:
#             print(f"❌ Partial main-view refresh failed: {e}")

#     def clear_guides(self):
#         """Clear only temporary visual guides (rubber-band line during drawing)"""
#         for actor in self.guide_actors:
#             self.renderer.RemoveActor(actor)
#         self.guide_actors.clear()
    
#     def clear_all(self):
#         """Clear everything including saved cross-sections (use for reset/clear all)"""
#         self.clear_guides()
#         for actor in self.section_actors:
#             self.renderer.RemoveActor(actor)
#         self.section_actors.clear()
    
#     def add_section(self, actor):
#         """Add a permanent cross-section actor"""
#         self.section_actors.append(actor)
#         self.renderer.AddActor(actor)
    
#     def add_guide(self, actor):
#         """Add a temporary guide actor (like rubber-band line)"""
#         self.guide_actors.append(actor)
#         self.renderer.AddActor(actor)
        
#     # Add this method to ClassificationInteractor class:

#     def _classify_brush_with_shape(self, center, radius, to_class, section_mask, section_points):
#         """
#         Classify points using brush with configurable shape (circle or rectangle).
#         """
#         from ..classification_tools import classify_brush, classify_rectangle
        
#         # Get brush shape from app
#         brush_shape = getattr(self.app, "brush_shape", "circle")
        
#         if brush_shape == "rectangle":
#             # Convert center + radius to rectangle bounds
#             u, z = center
#             u_min = u - radius
#             u_max = u + radius
#             z_min = z - radius
#             z_max = z + radius
            
#             print(f"🖌️ Rectangle brush: ({u_min:.2f}, {u_max:.2f}, {z_min:.2f}, {z_max:.2f})")
            
#             # Use rectangle classification
#             classify_rectangle(
#                 self.app,
#                 (u_min, u_max, z_min, z_max),
#                 self.app.from_classes,
#                 to_class,
#                 section_mask,
#                 section_points,
#                 None
#             )
#         else:
#             # Default circle brush
#             print(f"🖌️ Circle brush: center=({center[0]:.2f}, {center[1]:.2f}), radius={radius:.2f}")
            
#             classify_brush(
#                 self.app,
#                 center,
#                 radius,
#                 self.app.from_classes,
#                 to_class,
#                 section_mask,
#                 section_points,
#                 None
#             )
                
#     def _draw_rectangle_cursor(self, center, radius):
#         """
#         ✅ PERFORMANCE FIX: Rectangle brush cursor in DISPLAY pixels (zoom invariant).
#         Reuses actor — zero allocation per frame after init.
#         """
#         vtk_widget = self._get_active_vtk_widget()
#         if vtk_widget is None:
#             return
#         renderer = vtk_widget.renderer

#         x, y = self.interactor.GetEventPosition()
#         r = max(float(radius), 3.0)
#         x_min, x_max = x - r, x + r
#         y_min, y_max = y - r, y + r

#         # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
#         if not hasattr(self, "_rect_cursor_pts") or self.brush_actor is None:
#             from vtkmodules.vtkRenderingCore import vtkPolyDataMapper2D, vtkActor2D, vtkCoordinate as _C

#             if getattr(self, "brush_actor", None) is not None:
#                 try:
#                     renderer.RemoveActor2D(self.brush_actor)
#                 except Exception:
#                     pass
#                 self.brush_actor = None

#             self._rect_cursor_pts   = vtkPoints()
#             self._rect_cursor_lines = vtkCellArray()
#             self._rect_cursor_poly  = vtkPolyData()
#             self._rect_cursor_poly.SetPoints(self._rect_cursor_pts)
#             self._rect_cursor_poly.SetLines(self._rect_cursor_lines)

#             mapper = vtkPolyDataMapper2D()
#             mapper.SetInputData(self._rect_cursor_poly)
#             dc = _C(); dc.SetCoordinateSystemToDisplay()
#             mapper.SetTransformCoordinate(dc)

#             self.brush_actor = vtkActor2D()
#             self.brush_actor.SetMapper(mapper)
#             prop = self.brush_actor.GetProperty()
#             prop.SetColor(1, 1, 0)
#             prop.SetLineWidth(3)
#             prop.SetOpacity(0.9)

#             renderer.AddActor2D(self.brush_actor)

#         if not renderer.HasViewProp(self.brush_actor):
#             renderer.AddActor2D(self.brush_actor)
#         self.brush_actor.VisibilityOn()

#         # ── FAST GEOMETRY UPDATE ─────────────────────────────────────────────
#         pts   = self._rect_cursor_pts
#         lines = self._rect_cursor_lines
#         pts.Reset()
#         lines.Reset()

#         corners = [
#             (x_min, y_min, 0.0),
#             (x_max, y_min, 0.0),
#             (x_max, y_max, 0.0),
#             (x_min, y_max, 0.0),
#             (x_min, y_min, 0.0),
#         ]
#         for c in corners:
#             pts.InsertNextPoint(*c)
#         for i in range(1, len(corners)):
#             lines.InsertNextCell(2)
#             lines.InsertCellPoint(i - 1)
#             lines.InsertCellPoint(i)

#         pts.Modified()
#         self._rect_cursor_poly.Modified()
#         # Render batched by caller
        
#     def _draw_rectangle_cursor_cut(self, center, radius):
#         """Draw rectangle cursor for brush tool in CUT SECTION"""
#         from vtkmodules.vtkRenderingCore import vtkCoordinate, vtkPolyDataMapper2D, vtkActor2D
#         from vtkmodules.vtkCommonCore import vtkPoints
#         from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
        
#         print(f"■ _draw_rectangle_cursor_cut called: center={center}, radius={radius}")
        
#         vtk_widget = self._get_active_vtk_widget()
#         if vtk_widget is None:
#             print("❌ No VTK widget for rectangle cursor (cut section)")
#             return
        
#         # ✅ CRITICAL FIX: Get renderer properly
#         if hasattr(vtk_widget, 'renderer'):
#             renderer = vtk_widget.renderer
#         elif hasattr(vtk_widget, 'GetRenderer'):
#             renderer = vtk_widget.GetRenderer()
#         else:
#             rw = vtk_widget.GetRenderWindow()
#             renderer = rw.GetRenderers().GetFirstRenderer()
        
#         print(f"   ✅ Got renderer: {renderer}")
        
#         # Remove previous brush actor
#         if hasattr(self, "brush_actor_cut") and self.brush_actor_cut:
#             try:
#                 renderer.RemoveActor2D(self.brush_actor_cut)
#             except Exception:
#                 pass
#             self.brush_actor_cut = None
        
#         cu, cv = center
        
#         # Force minimum radius
#         if radius < 0.05:
#             radius = 0.1
        
#         # Build rectangle corners in world space
#         u_min, u_max = cu - radius, cu + radius
#         v_min, v_max = cv - radius, cv + radius
        
#         corners_world = [
#             self._view2d_to_world(u_min, v_min),
#             self._view2d_to_world(u_max, v_min),
#             self._view2d_to_world(u_max, v_max),
#             self._view2d_to_world(u_min, v_max),
#             self._view2d_to_world(u_min, v_min)  # Close the rectangle
#         ]
        
#         # Convert to display coordinates
#         pts = vtkPoints()
#         coord = vtkCoordinate()
#         coord.SetCoordinateSystemToWorld()
        
#         for point in corners_world:
#             coord.SetValue(point[0], point[1], point[2])
#             display_pos = coord.GetComputedDisplayValue(renderer)
#             pts.InsertNextPoint(display_pos[0], display_pos[1], 0)
        
#         # Create line segments
#         lines = vtkCellArray()
#         for i in range(len(corners_world) - 1):
#             lines.InsertNextCell(2)
#             lines.InsertCellPoint(i)
#             lines.InsertCellPoint(i + 1)
        
#         # Create polydata
#         poly = vtkPolyData()
#         poly.SetPoints(pts)
#         poly.SetLines(lines)
        
#         # Create 2D mapper
#         mapper = vtkPolyDataMapper2D()
#         mapper.SetInputData(poly)
        
#         coordinate = vtkCoordinate()
#         coordinate.SetCoordinateSystemToDisplay()
#         mapper.SetTransformCoordinate(coordinate)
        
#         # Create 2D actor
#         self.brush_actor_cut = vtkActor2D()
#         self.brush_actor_cut.SetMapper(mapper)
        
#         # Set properties - Yellow like other tools
#         prop = self.brush_actor_cut.GetProperty()
#         prop.SetColor(1, 1, 0)  # Yellow
#         prop.SetLineWidth(3)
#         prop.SetOpacity(0.9)
        
#         # Add as 2D actor (always on top!)
#         renderer.AddActor2D(self.brush_actor_cut)
#         print(f"   ✅ Added brush_actor_cut (rectangle) to renderer")
        
#         # ✅ CRITICAL: Force immediate render
#         try:
#             if hasattr(vtk_widget, 'GetRenderWindow'):
#                 self._safe_render(vtk_widget)
#             elif hasattr(vtk_widget, 'render'):
#                 self._safe_render_pyvista(vtk_widget)
#             print(f"   ✅ Render called successfully")
#         except Exception as e:
#             print(f"   ⚠️ Render failed: {e}")
            
#     def _sync_main_view_palette_weights(self):
#         """
#         Sync main-view (slot 0) weights only.
#         Keeps app.class_palette canonical to slot 0 and prevents cross-slot leakage.
#         """
#         from gui.classification_state import get_weight_cache
        
#         try:
#             dialog = getattr(self.app, 'display_mode_dialog', None)
#             if not dialog or not hasattr(dialog, 'view_palettes'):
#                 return
#             if 0 not in dialog.view_palettes or not dialog.view_palettes[0]:
#                 return
             
#             weight_cache = get_weight_cache()
#             slot0_map = {0: dialog.view_palettes[0]}
             
#             # Check if slot-0 weights changed
#             if not weight_cache.has_changes(slot0_map):
#                 return
             
#             slot0_changes = weight_cache.get_changed_weights(slot0_map).get(0, {})
#             if not slot0_changes:
#                 return
             
#             print(f"   🔄 Syncing {len(slot0_changes)} changed slot-0 weights")
             
#             # Update only changed weights
#             if not hasattr(self.app, 'class_palette'):
#                 self.app.class_palette = {}
             
#             slot0_palette = dialog.view_palettes[0]
#             for class_code, new_weight in slot0_changes.items():
#                 if class_code not in self.app.class_palette:
#                     if class_code in slot0_palette:
#                         self.app.class_palette[class_code] = dict(slot0_palette[class_code])
#                     else:
#                         self.app.class_palette[class_code] = {'weight': new_weight}
#                 else:
#                     self.app.class_palette[class_code]['weight'] = new_weight
             
#             # Update cache with slot 0 only
#             weight_cache.update_cache(slot0_map)
             
#         except Exception as e:
#             print(f"⚠️ Delta weight sync failed: {e}")    

#     def force_main_view_refresh_with_weights(self):
#         """
#         ✅ UNIFIED ACTOR: Force weight sync and GPU uniform push.
#         Replaces the old full-actor-wipe rebuild — no actor destroy/create needed.
#         """
#         print(f"\n{'='*60}")
#         print(f"💥 FORCING MAIN VIEW WEIGHT REFRESH (UNIFIED GPU POKE)")
#         print(f"{'='*60}")

#         try:
#             self._sync_main_view_palette_weights()

#             from gui.unified_actor_manager import sync_palette_to_gpu, is_unified_actor_ready
#             app = self.app

#             if is_unified_actor_ready(app):
#                 palette    = getattr(app, 'class_palette', {})
#                 border_pct = float(getattr(app, 'point_border_percent', 0) or 0.0)
#                 sync_palette_to_gpu(app, 0, palette, border_pct, render=True)
#                 print(f"   ✅ Unified GPU poke complete — weights applied without rebuild")
#             else:
#                 # Unified actor not built yet — do a one-time build
#                 print(f"   ⚠️ Unified actor not ready — triggering build")
#                 from gui.class_display import update_class_mode
#                 update_class_mode(app, force_refresh=True)

#             print(f"{'='*60}\n")

#         except Exception as e:
#             print(f"❌ Force refresh failed: {e}")
#             import traceback
#             traceback.print_exc()
                  
#     def _update_brush_preview_colors(self):
#         """
#         ✅ UNIFIED ACTOR: Instant brush preview via direct _naksha_rgb_ptr write.
#         ✅ FIX: Maps global mask → LOD local indices before writing to GPU buffer.
#         """
#         try:
#             if not hasattr(self, '_brush_accumulated_mask'):
#                 return
#             if not np.any(self._brush_accumulated_mask):
#                 return

#             to_class = getattr(self.app, "to_class", None)
#             if to_class is None:
#                 return

#             palette = getattr(self.app, "class_palette", {})
#             if to_class not in palette:
#                 return

#             target_color = np.array(palette[to_class].get("color", (255, 255, 0)),
#                                     dtype=np.uint8)

#             if self.app.display_mode != "class":
#                 return

#             # ✅ UNIFIED ACTOR path: write directly into shared GPU colour buffer
#             from gui.unified_actor_manager import _get_unified_actor
#             actor = _get_unified_actor(self.app)
#             if actor is None:
#                 return

#             rgb_ptr = getattr(actor, '_naksha_rgb_ptr', None)
#             if rgb_ptr is None or not rgb_ptr.flags.writeable:
#                 return

#             # ✅ FIX: Map global mask through LOD indices
#             # rgb_ptr only has len(_main_global_indices) entries, not len(xyz)
#             global_indices = getattr(self.app, '_main_global_indices', None)
#             if global_indices is not None:
#                 local_changed = np.flatnonzero(self._brush_accumulated_mask[global_indices])
#             else:
#                 local_changed = np.flatnonzero(self._brush_accumulated_mask)

#             if len(local_changed) == 0:
#                 return

#             rgb_ptr[local_changed] = target_color

#             vtk_ca = getattr(actor, '_naksha_vtk_array', None)
#             if vtk_ca is not None:
#                 vtk_ca.Modified()
#             from gui.unified_actor_manager import _mark_actor_dirty
#             _mark_actor_dirty(actor)

#             try:
#                 self._safe_render_pyvista(self.app.vtk_widget)
#             except Exception:
#                 pass

#         except Exception:
#             # Preview is optional — never break classification on preview failure
#             pass

#     def _create_brush_paint_overlay(self, rgb_color):
#         """
#         Add a tiny VTK actor to the main renderer showing freshly painted points.
#         Avoids calling vtk_ca.Modified() (the 30 MB GPU upload) during drag;
#         only the overlay's small buffer is uploaded each tick.
#         """
#         try:
#             self._destroy_brush_paint_overlay()   # clean any stale instance

#             import vtk
#             from vtkmodules.vtkFiltersGeneral import vtkVertexGlyphFilter
#             from vtkmodules.vtkRenderingCore import vtkPolyDataMapper, vtkActor

#             pts = vtk.vtkPoints()
#             pts.SetDataTypeToFloat()

#             pd = vtk.vtkPolyData()
#             pd.SetPoints(pts)

#             glyph = vtkVertexGlyphFilter()
#             glyph.SetInputData(pd)

#             mapper = vtkPolyDataMapper()
#             mapper.SetInputConnection(glyph.GetOutputPort())

#             actor = vtkActor()
#             actor.SetMapper(mapper)
#             r = float(rgb_color[0]) / 255.0
#             g = float(rgb_color[1]) / 255.0
#             b = float(rgb_color[2]) / 255.0
#             prop = actor.GetProperty()
#             prop.SetColor(r, g, b)
#             prop.SetPointSize(float(getattr(self.app, 'point_size', 2.5)))
#             prop.LightingOff()

#             self.app.vtk_widget.renderer.AddActor(actor)

#             self._brush_overlay_actor = actor
#             self._brush_overlay_pd    = pd
#             self._brush_overlay_pts   = pts
#             self._brush_overlay_acc   = np.zeros((0, 3), dtype=np.float32)

#         except Exception as _e:
#             print(f"⚠️ Brush overlay create: {_e}")
#             self._brush_overlay_actor = None

#     def _append_brush_paint_overlay(self, global_indices):
#         """
#         Append newly painted XYZ to the overlay and push the (small) buffer to GPU.
#         Called each 30-fps GPU tick during brush drag.
#         """
#         if getattr(self, '_brush_overlay_actor', None) is None:
#             return
#         try:
#             from vtkmodules.util.numpy_support import numpy_to_vtk

#             new_xyz = self.app.data["xyz"][global_indices].astype(np.float32)
#             acc = self._brush_overlay_acc
#             self._brush_overlay_acc = (
#                 np.vstack([acc, new_xyz]) if acc.shape[0] else new_xyz
#             )
#             vtk_arr = numpy_to_vtk(self._brush_overlay_acc, deep=False)
#             self._brush_overlay_pts.SetData(vtk_arr)
#             self._brush_overlay_pd.Modified()

#         except Exception as _e:
#             print(f"⚠️ Brush overlay append: {_e}")

#     def _destroy_brush_paint_overlay(self):
#         """Remove the overlay actor from the renderer and release references."""
#         actor = getattr(self, '_brush_overlay_actor', None)
#         if actor is not None:
#             try:
#                 self.app.vtk_widget.renderer.RemoveActor(actor)
#             except Exception:
#                 pass
#         self._brush_overlay_actor = None
#         self._brush_overlay_pd    = None
#         self._brush_overlay_pts   = None
#         self._brush_overlay_acc   = None

#     def _build_spatial_index_for_brush(self):
#         """
#         SPATIAL GRID INDEX: Vectorized build for O(1) brush lookup in Main View.
#         Thread-safe via double-checked locking: safe to call from a background
#         thread (pre-warm) and from the main thread (on_left_press guard).
#         _build_index_lock is created in __init__ — no lazy init race.
#         """
#         import time
#         xyz = self.app.data["xyz"]

#         # Fast path — GIL ensures atomic attribute read; no lock needed here
#         if (getattr(self, "_main_grid_index", None) is not None
#                 and getattr(self, '_brush_spatial_index_xyz_id', None) == id(xyz)):
#             return

#         with self._build_index_lock:
#             # Re-check inside lock (another thread may have built it while we waited)
#             if (getattr(self, "_main_grid_index", None) is not None
#                     and getattr(self, '_brush_spatial_index_xyz_id', None) == id(xyz)):
#                 return

#             start = time.time()

#             if (not hasattr(self, '_main_points_2d')
#                     or self._main_points_2d is None
#                     or len(self._main_points_2d) != len(xyz)):
#                 self._main_points_2d = xyz[:, [0, 1]].copy()

#             index = SpatialGridIndex(self._main_points_2d)

#             # Assign atomically so readers never see a half-built index
#             self._main_grid_index = index
#             self._brush_spatial_index_xyz_id = id(xyz)

#             build_time = (time.time() - start) * 1000
#             print(f"   ⚡ Main View Spatial index built in {build_time:.0f}ms ({len(xyz):,} pts)")
        
        
#     def _get_points_in_radius_fast(self, center_x, center_y, radius):
#         """
#         ✅ MICROSTATION O(1) GRID LOOKUP: Get points within radius.
#         Uses sort-order slicing — zero Python loops for candidate collection.
#         Typical: 0.1-0.5ms per stamp (vs 10-50ms for full scan).
#         """
#         xyz = self.app.data["xyz"]
#         if (self._brush_spatial_index is None
#                 or getattr(self, '_brush_spatial_index_xyz_id', None) != id(xyz)):
#             self._build_spatial_index_for_brush()
        
#         params = self._brush_grid_params
#         grid_size = params['grid_size']
#         sort_order = self._brush_sort_order
        
#         # Calculate which grid cells overlap with the brush circle
#         cell_x_min = max(0, int((center_x - radius - params['x_min']) / params['x_step']))
#         cell_x_max = min(grid_size - 1, int((center_x + radius - params['x_min']) / params['x_step']))
#         cell_y_min = max(0, int((center_y - radius - params['y_min']) / params['y_step']))
#         cell_y_max = min(grid_size - 1, int((center_y + radius - params['y_min']) / params['y_step']))
        
#         # Collect candidate point indices from overlapping cells via sort_order slices
#         slices = []
#         for cx in range(cell_x_min, cell_x_max + 1):
#             for cy in range(cell_y_min, cell_y_max + 1):
#                 cell_range = self._brush_spatial_index.get((cx, cy))
#                 if cell_range is not None:
#                     s, e = cell_range
#                     slices.append(sort_order[s:e])
        
#         if not slices:
#             return np.array([], dtype=np.int64)
        
#         candidates = np.concatenate(slices)
        
#         # Fast vectorized distance check on candidates only
#         xyz = self.app.data["xyz"]
#         pts = xyz[candidates]
#         dx = pts[:, 0] - center_x
#         dy = pts[:, 1] - center_y
#         within = candidates[(dx * dx + dy * dy) <= (radius * radius)]
#         return within
    
#     def _interpolate_brush_stroke(self, p1, p2, radius):
#         """
#         ✅ SMOOTH STROKES: Interpolate between positions for gap-free painting
#         """
#         x1, y1 = p1
#         x2, y2 = p2
        
#         # Calculate distance
#         dx = x2 - x1
#         dy = y2 - y1
#         dist = np.sqrt(dx*dx + dy*dy)
        
#         # Calculate number of interpolation steps
#         step_size = self._brush_interpolation_step * radius
#         num_steps = max(1, int(dist / step_size))
        
#         # Generate interpolated positions
#         positions = []
#         for i in range(num_steps + 1):
#             t = i / num_steps
#             x = x1 + dx * t
#             y = y1 + dy * t
#             positions.append((x, y))
        
#         return positions
    
    
#     def _get_points_in_box_fast(self, x_min, x_max, y_min, y_max):
#         """
#         ✅ FAST: Get points in rectangle using spatial index
#         """
#         # Rebuild index if array identity changed (new file loaded)
#         _xyz_now = self.app.data.get("xyz")
#         if (self._brush_spatial_index is None
#                 or getattr(self, '_brush_spatial_index_xyz_id', None) != id(_xyz_now)):
#             self._build_spatial_index_for_brush()

#         params = self._brush_grid_params
#         grid_size = params['grid_size']

#         # Calculate overlapping grid cells
#         cell_x_min = int((x_min - params['x_min']) / params['x_step'])
#         cell_x_max = int((x_max - params['x_min']) / params['x_step'])
#         cell_y_min = int((y_min - params['y_min']) / params['y_step'])
#         cell_y_max = int((y_max - params['y_min']) / params['y_step'])
        
#         # Clip to grid bounds
#         cell_x_min = max(0, cell_x_min)
#         cell_x_max = min(grid_size - 1, cell_x_max)
#         cell_y_min = max(0, cell_y_min)
#         cell_y_max = min(grid_size - 1, cell_y_max)
        
#         # Collect candidates via sort_order slices (no Python list append)
#         sort_order = self._brush_sort_order
#         slices = []
#         for cx in range(cell_x_min, cell_x_max + 1):
#             for cy in range(cell_y_min, cell_y_max + 1):
#                 cell_range = self._brush_spatial_index.get((cx, cy))
#                 if cell_range is not None:
#                     s, e = cell_range
#                     slices.append(sort_order[s:e])

#         if not slices:
#             return np.array([], dtype=np.int64)

#         # Precise box check on candidates
#         xyz = self.app.data["xyz"]
#         candidates = np.concatenate(slices).astype(np.int64)
#         pts = xyz[candidates]
        
#         in_box = (
#             (pts[:, 0] >= x_min) & (pts[:, 0] <= x_max) &
#             (pts[:, 1] >= y_min) & (pts[:, 1] <= y_max)
#         )
        
#         return candidates[in_box]

#     def _finalize_polygon_classification(self):
#         """Finalize polygon selection and classify enclosed points."""
#         if len(self.drawing_points) < 3:
#             if hasattr(self.app, "statusBar"):
#                 self.app.statusBar().showMessage(
#                     "Polygon needs at least 3 vertices. Left-click to add more points.",
#                     2500,
#                 )
#             return False

#         to_class = getattr(self.app, "to_class", None)
#         from_classes = getattr(self.app, "from_classes", None)

#         if self._is_main_view():
#             self._classify_polygon_main(self.drawing_points, to_class)
#             return True

#         from ..classification_tools import classify_polygon, _get_cut_section_or_default

#         vtk_widget = self._get_active_vtk_widget()
#         is_cut_section = bool(
#             hasattr(self.app, "cut_section_controller")
#             and vtk_widget == getattr(self.app.cut_section_controller, "cut_vtk", None)
#         )

#         if is_cut_section:
#             sec_ctrl = getattr(self.app, "section_controller", None)
#             mask = getattr(sec_ctrl, "last_mask", None)
#             section_points = getattr(self.app, "section_points", None)
#         else:
#             v_idx = self._get_view_index_from_interactor()
#             if v_idx is not None:
#                 combined_mask = getattr(self.app, f"section_{v_idx}_combined_mask", None)
#                 section_points_transformed = getattr(
#                     self.app, f"section_{v_idx}_points_transformed", None
#                 )
#                 if combined_mask is not None and section_points_transformed is not None:
#                     mask = combined_mask
#                     section_points = section_points_transformed
#                 else:
#                     mask, section_points = self._get_section_context_for_view(v_idx)
#             else:
#                 mask, section_points = None, None

#         section_indices, section_points = _get_cut_section_or_default(
#             self.app, mask, section_points
#         )
#         if section_indices is None or section_points is None:
#             return False

#         classify_polygon(
#             self.app,
#             self.drawing_points,
#             from_classes,
#             to_class,
#             mask,
#             section_points,
#             None,
#         )
#         self._refresh_all_views_after_classification(to_class)
#         return True

#     def _append_polygon_vertex_from_cursor(self):
#         """
#         Capture current cursor location as a polygon vertex before finalize.
#         This ensures right-click close includes the last intended corner.
#         """
#         try:
#             x, y = self.interactor.GetEventPosition()
#         except Exception:
#             return

#         try:
#             pt = self._display_to_world_fast(float(x), float(y))
#             u, v = self._get_view_coordinates(pt)
#             uv = (float(u), float(v))
#         except Exception:
#             return

#         add_vertex = True
#         if self.drawing_points:
#             lu, lv = self.drawing_points[-1]
#             # Tiny tolerance prevents duplicate last-point insertion.
#             if (abs(uv[0] - float(lu)) <= 1e-6) and (abs(uv[1] - float(lv)) <= 1e-6):
#                 add_vertex = False

#         if add_vertex:
#             self.drawing_points.append(uv)

#         # Keep cut-section display preview list in sync.
#         vtk_widget = self._get_active_vtk_widget()
#         is_cut_section = bool(
#             hasattr(self.app, "cut_section_controller")
#             and vtk_widget == getattr(self.app.cut_section_controller, "cut_vtk", None)
#         )
#         if is_cut_section:
#             if not hasattr(self, "drawing_points_display_cut"):
#                 self.drawing_points_display_cut = []
#             if not hasattr(self, "drawing_points_world_cut"):
#                 self.drawing_points_world_cut = []
#             if not self.drawing_points_display_cut:
#                 self.drawing_points_display_cut.append((float(x), float(y)))
#                 self.drawing_points_world_cut.append((float(pt[0]), float(pt[1]), float(pt[2])))
#             else:
#                 lx, ly = self.drawing_points_display_cut[-1]
#                 if np.hypot(float(x) - float(lx), float(y) - float(ly)) > 1.5:
#                     self.drawing_points_display_cut.append((float(x), float(y)))
#                     self.drawing_points_world_cut.append((float(pt[0]), float(pt[1]), float(pt[2])))

#     def on_right_press(self, obj, evt):
#         """Right-click handler for cross-section views."""
#         if getattr(self.app, "active_classify_tool", None) == "polygon":
#             try:
#                 if hasattr(obj, "AbortFlagOn"):
#                     obj.AbortFlagOn()
#                 elif hasattr(obj, "SetAbortFlag"):
#                     obj.SetAbortFlag(1)
#             except Exception:
#                 pass
#             try:
#                 self._append_polygon_vertex_from_cursor()
#                 if self._finalize_polygon_classification():
#                     self._clear_all_previews()
#                     if hasattr(self.app, "statusBar"):
#                         self.app.statusBar().showMessage(
#                             "Polygon classified. Draw next polygon or press Esc to exit.",
#                             2200,
#                         )
#             except Exception as e:
#                 print(f"⚠️ Polygon finalize failed: {e}")
#                 import traceback
#                 traceback.print_exc()
#             return

#         if self._is_main_view():
#             try:
#                 self.style.OnRightButtonDown()
#             except Exception:
#                 pass
#             return
        
#         x, y = self.interactor.GetEventPosition()
        
#         try:
#             pt = self._pick_world_point(x, y)
            
#             # ✅ ADD: Set flag to trigger fly-to after next classification
#             self._fly_to_after_classify = pt
            
#         except Exception as e:
#             print(f"⚠️ Right-click handler failed: {e}")


#     def _execute_fly_to(self, world_point):
#         """Execute fly-to with delay"""
#         from PySide6.QtCore import QTimer
        
#         # Clear the flag (user chose manual fly-to)
#         self._fly_to_after_classify = None
        
#         # Add delay to let any pending refresh complete
#         QTimer.singleShot(200, lambda: self._fly_to_main_view(world_point))

#     def _fly_to_main_view(self, world_point):
#         """
#         Smoothly fly camera to classification location in main view.
#         Shows visual indicator at target location.
#         """
#         print(f"\n{'='*60}")
#         print(f"✈️ FLYING TO CLASSIFICATION LOCATION")
#         print(f"{'='*60}")
#         print(f"   Target (raw): ({world_point[0]:.2f}, {world_point[1]:.2f}, {world_point[2]:.2f})")
        
#         # ✅ CRITICAL FIX: Transform cross-section coordinates to global world coordinates
#         try:
#             # Check if we're in a cross-section view
#             active_view = self._get_view_index_from_interactor()
            
#             if active_view is not None:
#                 # We're in a cross-section - need to transform coordinates
#                 print(f"   🔄 Transforming from cross-section View {active_view + 1} coordinates...")
                
#                 # Get section data to find the actual world coordinates
#                 core_mask = getattr(self.app, f"section_{active_view}_core_mask", None)
#                 buffer_mask = getattr(self.app, f"section_{active_view}_buffer_mask", None)
                
#                 if core_mask is not None:
#                     # Combine masks
#                     if buffer_mask is not None:
#                         section_mask = core_mask | buffer_mask
#                     else:
#                         section_mask = core_mask
                    
#                     # Get the actual 3D points in this section
#                     section_xyz = self.app.data["xyz"][section_mask]
                    
#                     print(f"   📊 Section has {len(section_xyz)} points")
                    
#                     # Get the 2D coordinates we clicked on (u, z in cross-section view)
#                     u_clicked = world_point[0]
#                     z_clicked = world_point[2]
                    
#                     print(f"   🎯 Clicked at: u={u_clicked:.2f}, z={z_clicked:.2f}")
                    
#                     # Get view mode to know which coordinates to use
#                     view_mode = getattr(self.app, 'cross_view_mode', 'side')
#                     print(f"   📐 View mode: {view_mode}")
                    
#                     # Project section points to 2D (same way classification does it)
#                     if view_mode == 'front':
#                         # Front view: u is Y coordinate, z is Z coordinate
#                         section_2d = np.column_stack([section_xyz[:, 1], section_xyz[:, 2]])
#                         print(f"   📐 Using front view projection (Y, Z)")
#                     else:
#                         # Side view (default): u is X coordinate, z is Z coordinate
#                         section_2d = np.column_stack([section_xyz[:, 0], section_xyz[:, 2]])
#                         print(f"   📐 Using side view projection (X, Z)")
                    
#                     # Find the closest point in the section to where we clicked
#                     clicked_2d = np.array([u_clicked, z_clicked])
#                     distances = np.sum((section_2d - clicked_2d) ** 2, axis=1)
#                     closest_idx = np.argmin(distances)
#                     closest_distance = np.sqrt(distances[closest_idx])
                    
#                     # Get the actual 3D world coordinates of the closest point
#                     world_point = tuple(section_xyz[closest_idx])
                    
#                     print(f"   ✅ Found closest point in section:")
#                     print(f"      2D distance: {closest_distance:.2f}")
#                     print(f"      World coords: ({world_point[0]:.2f}, {world_point[1]:.2f}, {world_point[2]:.2f})")
#                 else:
#                     print(f"   ⚠️ No section mask for View {active_view + 1}")
            
#             # Validate against data bounds
#             xyz = self.app.data["xyz"]
#             x_min, x_max = xyz[:, 0].min(), xyz[:, 0].max()
#             y_min, y_max = xyz[:, 1].min(), xyz[:, 1].max()
#             z_min, z_max = xyz[:, 2].min(), xyz[:, 2].max()
            
#             print(f"   📊 Data bounds:")
#             print(f"      X: {x_min:.2f} to {x_max:.2f}")
#             print(f"      Y: {y_min:.2f} to {y_max:.2f}")
#             print(f"      Z: {z_min:.2f} to {z_max:.2f}")
            
#             # Check if transformed point is within reasonable bounds
#             if (world_point[0] < x_min or world_point[0] > x_max or
#                 world_point[1] < y_min or world_point[1] > y_max):
                
#                 print(f"   ⚠️ Warning: Target may be outside main data bounds")
#                 print(f"      X: {world_point[0]:.2f} (bounds: {x_min:.2f} to {x_max:.2f})")
#                 print(f"      Y: {world_point[1]:.2f} (bounds: {y_min:.2f} to {y_max:.2f})")
        
#         except Exception as e:
#             print(f"   ⚠️ Coordinate transformation failed: {e}")
#             import traceback
#             traceback.print_exc()
#             # Don't return - still try to fly to the point even if transformation failed
        
#         try:
#             main_plotter = self.app.vtk_widget
            
#             # Get current camera
#             if hasattr(main_plotter, 'renderer'):
#                 renderer = main_plotter.renderer
#                 cam = renderer.GetActiveCamera()
#             elif hasattr(main_plotter, 'camera'):
#                 cam = main_plotter.camera
#             else:
#                 print("   ❌ No camera in main view")
#                 return
            
#             # Calculate new camera position
#             target_x, target_y, target_z = world_point
            
#             # Get current camera distance to maintain zoom level
#             current_pos = np.array(cam.GetPosition())
#             current_focal = np.array(cam.GetFocalPoint())
#             current_distance = np.linalg.norm(current_pos - current_focal)
            
#             # New focal point is the target location at its actual elevation
#             new_focal_point = np.array([target_x, target_y, target_z])
            
#             # Position camera above the target at current distance
#             view_direction = np.array([0, 0, 1])  # Always look straight down in main view
#             new_position = new_focal_point + view_direction * current_distance
                        
#             print(f"   📷 Current focal: {current_focal}")
#             print(f"   📷 New focal: {new_focal_point}")
#             print(f"   📷 New position: {new_position}")
#             print(f"   📷 Distance: {current_distance:.2f}")

#             # ✅ FIX: Delay animation to let refresh complete
#             from PySide6.QtCore import QTimer

#             def start_animation():
#                 self._animate_camera_to(
#                     main_plotter,
#                     new_position,
#                     new_focal_point,
#                     duration_ms=800
#                 )

#             # Wait 100ms for any pending refreshes to complete
#             QTimer.singleShot(100, start_animation)
            
#             # Show visual indicator at target
#             self._show_target_indicator(world_point)
            
#             # Update status bar
#             if hasattr(self.app, 'statusBar'):
#                 self.app.statusBar().showMessage(
#                     f"✈️ Flew to ({target_x:.1f}, {target_y:.1f}, {target_z:.1f})",
#                     3000
#                 )
            
#             print(f"   ✅ Camera animated to target")
#             print(f"{'='*60}\n")
            
#         except Exception as e:
#             print(f"   ❌ Fly-to failed: {e}")
#             import traceback
#             traceback.print_exc()

#     def _animate_camera_to(self, plotter, new_pos, new_focal, duration_ms=800):
#         """
#         Smooth camera animation using Qt timer.
#         Uses easing curve for natural motion.
#         """
#         from PySide6.QtCore import QTimer
#         import numpy as np
#         self.app._fly_to_in_progress = True
        
#         try:
#             # Get camera
#             if hasattr(plotter, 'renderer'):
#                 cam = plotter.renderer.GetActiveCamera()
#             elif hasattr(plotter, 'camera'):
#                 cam = plotter.camera
#             else:
#                 print("   ⚠️ No camera found for animation")
#                 return
            
#             # Store start state
#             start_pos = np.array(cam.GetPosition())
#             start_focal = np.array(cam.GetFocalPoint())
            
#             end_pos = np.array(new_pos)
#             end_focal = np.array(new_focal)
            
#             # Animation parameters
#             steps = 30
#             step_duration = duration_ms // steps
            
#             # Easing function (ease in-out cubic)
#             def ease_in_out_cubic(t):
#                 """Smooth easing curve"""
#                 if t < 0.5:
#                     return 4 * t * t * t
#                 else:
#                     p = 2 * t - 2
#                     return 1 + p * p * p / 2
            
#             current_step = [0]  # Mutable container for closure
            
#             def animate_step():
#                 try:
#                     t = current_step[0] / steps
#                     eased_t = ease_in_out_cubic(t)
                    
#                     # Interpolate position and focal point
#                     interp_pos = start_pos + (end_pos - start_pos) * eased_t
#                     interp_focal = start_focal + (end_focal - start_focal) * eased_t
                    
#                     # Update camera
#                     cam.SetPosition(interp_pos.tolist())
#                     cam.SetFocalPoint(interp_focal.tolist())
                    
#                     # Render
#                     if hasattr(plotter, 'GetRenderWindow'):
#                         self._safe_render(plotter)
#                     elif hasattr(plotter, 'render'):
#                         self._safe_render_pyvista(plotter)
                    
#                     current_step[0] += 1
                    
#                     if current_step[0] <= steps:
#                         QTimer.singleShot(step_duration, animate_step)
#                     else:
#                         print("   ✅ Animation complete")
#                         self.app._fly_to_in_progress = False
                        
#                 except Exception as e:
#                     print(f"   ⚠️ Animation step failed: {e}")
            
#             # Start animation
#             animate_step()
            
#         except Exception as e:
#             print(f"   ⚠️ Animation setup failed: {e}")
#             # Fallback: instant camera moveccccccc
#             try:
#                 cam.SetPosition(new_pos.tolist())
#                 cam.SetFocalPoint(new_focal.tolist())
#                 if hasattr(plotter, 'render'):
#                     self._safe_render_pyvista(plotter)
#             except Exception:
#                 pass

#     def _show_target_indicator(self, world_point):
#         """
#         Flash MULTIPLE visual indicators at target location.
#         Creates sphere + crosshair + rings + label for unmistakable visibility.
#         """
#         try:
#             main_plotter = self.app.vtk_widget
#             import pyvista as pv
            
#             # Determine appropriate radius based on data bounds
#             xyz = self.app.data.get("xyz")
#             if xyz is not None:
#                 data_range = np.max(xyz, axis=0) - np.min(xyz, axis=0)
#                 base_radius = np.mean(data_range) * 0.008  # Smaller radius (0.8%)
#             else:
#                 base_radius = 1.5
            
#             print(f"   ✨ Creating multi-part indicator (base_radius={base_radius:.2f})")
            
#             # 1. CENTRAL SPHERE (Smooth, semi-transparent)
#             sphere = pv.Sphere(radius=base_radius, center=world_point, theta_resolution=30, phi_resolution=30)
#             main_plotter.add_mesh(
#                 sphere, 
#                 color='red', 
#                 opacity=0.6,  # Semi-transparent
#                 name='fly_to_sphere', 
#                 lighting=True,  # Enable lighting for better look
#                 smooth_shading=True
#             )
            
#             # 2. OUTER RING (Circle at same height)
#             ring = pv.Circle(radius=base_radius * 2.5, center=world_point, resolution=64)
#             main_plotter.add_mesh(
#                 ring, 
#                 color='yellow', 
#                 line_width=4,
#                 opacity=0.8,
#                 name='fly_to_ring', 
#                 lighting=False
#             )
            
#             # 3. CROSSHAIR LINES (Thinner, longer)
#             crosshair_size = base_radius * 5
            
#             # Horizontal line (X-axis)
#             h_line = pv.Line(
#                 [world_point[0] - crosshair_size, world_point[1], world_point[2]],
#                 [world_point[0] + crosshair_size, world_point[1], world_point[2]]
#             )
#             main_plotter.add_mesh(
#                 h_line, 
#                 color='yellow', 
#                 line_width=2,
#                 opacity=0.7,
#                 name='fly_to_h_line', 
#                 lighting=False
#             )
            
#             # Vertical line (Y-axis)
#             v_line = pv.Line(
#                 [world_point[0], world_point[1] - crosshair_size, world_point[2]],
#                 [world_point[0], world_point[1] + crosshair_size, world_point[2]]
#             )
#             main_plotter.add_mesh(
#                 v_line, 
#                 color='yellow', 
#                 line_width=2,
#                 opacity=0.7,
#                 name='fly_to_v_line', 
#                 lighting=False
#             )
            
#             # 4. TEXT LABEL (Above the sphere)
#             label_pos = [world_point[0], world_point[1], world_point[2] + base_radius * 4]
#             main_plotter.add_point_labels(
#                 [label_pos], 
#                 ['🎯 TARGET'],
#                 point_size=0,  # Hide the point itself
#                 font_size=28,
#                 text_color='yellow', 
#                 name='fly_to_label',
#                 bold=True, 
#                 shadow=True,
#                 always_visible=True
#             )
            
#             print(f"   ✅ Multi-element indicator created")
            
#             # Animate for 12 seconds (10-15 range)
#             self._animate_indicator(main_plotter, base_radius, duration_ms=12000)
            
#         except Exception as e:
#             print(f"   ⚠️ Indicator creation failed: {e}")
#             import traceback
#             traceback.print_exc()
            
#     def _animate_indicator(self, plotter, base_radius, duration_ms=12000):
#         """Animate all indicator elements with pulse + expand + fade over 12 seconds"""
#         from PySide6.QtCore import QTimer
        
#         steps = 120  # More steps for smoother animation over 12 seconds
#         step_duration = duration_ms // steps
#         current_step = [0]
        
#         # Use static singleShot scheduling to avoid accumulating QObject timers
#         # on the app object across repeated fly-to animations.
        
#         def animate_step():
#             try:
#                 t = current_step[0] / steps
                
#                 # Gentle fade (starts fading after 60% of time)
#                 if t < 0.6:
#                     opacity = 1.0
#                 else:
#                     fade_progress = (t - 0.6) / 0.4
#                     opacity = 1.0 - fade_progress
                
#                 # Sphere: gentle pulsing
#                 pulse = 0.15 * np.sin(t * 3 * np.pi)  # 3 slow pulses
#                 sphere_opacity = max(0, min(1, (opacity * 0.6) + pulse))
                
#                 # Update actors if they exist
#                 if 'fly_to_sphere' in plotter.actors:
#                     plotter.actors['fly_to_sphere'].GetProperty().SetOpacity(sphere_opacity)
                
#                 if 'fly_to_ring' in plotter.actors:
#                     plotter.actors['fly_to_ring'].GetProperty().SetOpacity(opacity * 0.8)
#                     # Gentle ring expansion
#                     scale = 1 + t * 0.5  # Grows to 1.5x size slowly
#                     plotter.actors['fly_to_ring'].SetScale(scale, scale, 1)
                
#                 if 'fly_to_h_line' in plotter.actors:
#                     plotter.actors['fly_to_h_line'].GetProperty().SetOpacity(opacity * 0.7)
                
#                 if 'fly_to_v_line' in plotter.actors:
#                     plotter.actors['fly_to_v_line'].GetProperty().SetOpacity(opacity * 0.7)
                
#                 if 'fly_to_label' in plotter.actors:
#                     # Label stays visible longer
#                     label_opacity = 1.0 if t < 0.8 else (1.0 - (t - 0.8) / 0.2)
#                     plotter.actors['fly_to_label'].GetProperty().SetOpacity(max(0, label_opacity))
                
#                 self._safe_render_pyvista(plotter)
                
#                 current_step[0] += 1
                
#                 if current_step[0] <= steps:
#                     QTimer.singleShot(step_duration, animate_step)
#                 else:
#                     # Remove all indicators after animation completes
#                     indicator_names = ['fly_to_sphere', 'fly_to_ring', 'fly_to_h_line', 
#                                     'fly_to_v_line', 'fly_to_label']
#                     removed_count = 0
                    
#                     for name in indicator_names:
#                         if name in plotter.actors:
#                             try:
#                                 plotter.remove_actor(name)
#                                 removed_count += 1
#                                 print(f"   ✅ Removed actor: {name}")
#                             except Exception as e:
#                                 print(f"   ⚠️ Failed to remove {name}: {e}")
                    
#                     self._safe_render_pyvista(plotter)
#                     print(f"   ✅ Animation complete. Removed {removed_count} indicators")
                    
#             except Exception as e:
#                 print(f"   ⚠️ Animation step failed at step {current_step[0]}: {e}")
#                 import traceback
#                 traceback.print_exc()
        
#         QTimer.singleShot(0, animate_step)


#     def _verify_buffer_classification(self, view_idx):
#         """
#         Debug helper to verify buffer points are being included in classification.
#         Call this after classification to confirm buffer points were modified.
#         """
#         try:
#             app = self.app
            
#             # Get masks
#             core_mask = getattr(app, f"section_{view_idx}_core_mask", None)
#             buffer_mask = getattr(app, f"section_{view_idx}_buffer_mask", None)
#             combined_mask = getattr(app, f"section_{view_idx}_combined_mask", None)
            
#             # Get last changed mask from classification
#             last_changed = getattr(app, "_last_changed_mask", None)
            
#             if last_changed is None:
#                 print(f"   ⚠️ No _last_changed_mask found")
#                 return
            
#             print(f"\n{'='*60}")
#             print(f"🔍 BUFFER CLASSIFICATION VERIFICATION - View {view_idx}")
#             print(f"{'='*60}")
            
#             # Count points in each category
#             if core_mask is not None:
#                 core_classified = (last_changed & core_mask).sum()
#                 print(f"   Core points classified: {core_classified:,}")
            
#             if buffer_mask is not None:
#                 buffer_classified = (last_changed & buffer_mask).sum()
#                 print(f"   Buffer points classified: {buffer_classified:,}")
#                 if buffer_classified > 0:
#                     print(f"   ✅ SUCCESS: Buffer points WERE classified!")
#                 else:
#                     print(f"   ⚠️ WARNING: No buffer points were classified")
            
#             if combined_mask is not None:
#                 total_classified = (last_changed & combined_mask).sum()
#                 print(f"   Total points classified: {total_classified:,}")
            
#             print(f"{'='*60}\n")
            
#         except Exception as e:
#             print(f"   ❌ Verification failed: {e}")    

#     def verify_brush_buffer_classification(app):
#         """
#         Debug helper to verify brush tool includes buffer points.
#         Call this after brush initialization (after left press).
#         """
#         print(f"\n{'='*60}")
#         print(f"🔍 BRUSH BUFFER VERIFICATION")
#         print(f"{'='*60}")
        
#         # Check if brush is initialized
#         if not hasattr(app, 'section_controller'):
#             print("   ⚠️ No section controller")
#             return
        
#         active_view = getattr(app.section_controller, "active_view", None)
#         if active_view is None:
#             print("   ⚠️ No active view")
#             return
        
#         # Check masks
#         combined_mask = getattr(app, f"section_{active_view}_combined_mask", None)
#         core_mask = getattr(app, f"section_{active_view}_core_mask", None)
#         buffer_mask = getattr(app, f"section_{active_view}_buffer_mask", None)
        
#         if combined_mask is not None:
#             combined_count = combined_mask.sum()
#             print(f"   ✅ Combined mask: {combined_count:,} points")
        
#         if core_mask is not None:
#             core_count = core_mask.sum()
#             print(f"   Core mask: {core_count:,} points")
        
#         if buffer_mask is not None and core_mask is not None:
#             buffer_only = (buffer_mask & ~core_mask).sum()
#             print(f"   Buffer mask: {buffer_only:,} exclusive buffer points")
        
#         # Check brush initialization
#         interactor = None
#         for vtk_widget in getattr(app, 'section_vtks', {}).values():
#             if hasattr(vtk_widget, 'interactor'):
#                 interactor = vtk_widget.interactor
#                 if hasattr(interactor, 'GetInteractorStyle'):
#                     style = interactor.GetInteractorStyle()
#                     if hasattr(style, '_brush_section_indices'):
#                         brush_indices = style._brush_section_indices
#                         print(f"\n   🖌️ Brush indices: {len(brush_indices):,}")
                        
#                         if combined_mask is not None:
#                             expected = combined_mask.sum()
#                             if len(brush_indices) == expected:
#                                 print(f"   ✅ Brush indices match combined mask!")
#                             elif core_mask is not None and len(brush_indices) == core_mask.sum():
#                                 print(f"   ❌ Brush indices only include CORE (missing buffer!)")
#                             else:
#                                 print(f"   ⚠️ Brush indices count unexpected: {len(brush_indices)} vs {expected}")
#                         break
        
#         print(f"{'='*60}\n")


#     # In interactor_classify.py — wherever classification is committed:
#     def _commit_classification(self, mask, from_classes, to_class):
#         """Apply classification and maintain undo/redo stacks correctly."""
#         import numpy as np

#         classification = self.app.data["classification"]
#         old_classes = classification[mask].copy()

#         # Apply to ground truth
#         classification[mask] = to_class

#         # Push undo step
#         step = {
#             "indices": np.flatnonzero(mask).astype(np.int64, copy=False),
#             "old_classes": old_classes,
#             "new_classes": np.full(mask.sum(), to_class, dtype=classification.dtype),
#         }
#         self.app.undo_stack.append(step)

#         # *** CRITICAL: Always clear redo when new classification is committed ***
#         # Without this, stale redo steps pollute subsequent undo/redo cycles.
#         self.app.redo_stack.clear()

#         # Cap undo stack
#         try:
#             from gui.memory_manager import current_recommended_undo_steps
#             max_steps = int(current_recommended_undo_steps(self.app))
#         except Exception:
#             max_steps = int(getattr(self.app, '_max_undo_steps', 30))
#         while len(self.app.undo_stack) > max_steps:
#             from gui.memory_manager import _free_undo_entry
#             _free_undo_entry(self.app.undo_stack.pop(0))

#         # Invalidate section mirrors BEFORE emitting so _on_classification_finished
#         # gets clean mirrors to work with
#         if hasattr(self.app, 'section_vtks'):
#             for view_idx in self.app.section_vtks.keys():
#                 self.app._sync_section_mirror_from_data(view_idx)

#         # Emit signal — triggers cross-section refresh via _on_classification_finished
#         self.app.classification_finished.emit(mask)

from json import tool
from vtkmodules.vtkInteractionStyle import vtkInteractorStyleImage, vtkInteractorStyleTrackballCamera
from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper, vtkCoordinate
from vtkmodules.vtkCommonDataModel import vtkPolyData, vtkCellArray
from vtkmodules.vtkCommonCore import vtkPoints
import vtk
import numpy as np
import time
from builtins import print as _builtin_print
from gui.vtk_utils import force_vtk_pipeline_update
# ✅ Shared helper — avoids triggering shading rebuild when no mesh exists yet
from gui.classification_tools import _shading_mesh_exists
          
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
    QDoubleSpinBox, QPushButton, QSlider, QWidget
)
from PySide6.QtCore import QTimer, QObject, QEvent
from PySide6.QtCore import Qt, QPoint, QPointF, QLineF
from PySide6.QtGui import QPainter, QColor, QPen, QMouseEvent
import pyvista as pv

_INTERACTOR_DEBUG = True

# MicroStation-style tap-vs-drag for above/below/parallel-line tools: a brief
# settle window right after press during which mouse movement is never
# counted toward drag detection (a real fast tap's hand jitter happens almost
# entirely within this window), plus the distance threshold (top of
# MicroStation's own stated 4-6px range) that decides drag once past it.
# Shared by _on_mouse_move_with_preview (accumulation) and on_left_release
# (the tap-vs-drag decision) so both stay in sync.
_LINE_TAP_GRACE_SEC = 0.12
_LINE_TAP_DRAG_THRESHOLD_PX = 6.0


def print(*args, **kwargs):  # type: ignore[override]
    if _INTERACTOR_DEBUG:
        _builtin_print(*args, **kwargs)


class Camera2DLock:
    def __init__(self, camera, app, is_cut_section=False):
        self.camera = camera
        self.app = app
        self.is_cut_section = is_cut_section
        self._in_sync = False

    def __call__(self, obj, event):
        if self._in_sync:
            return
        self._in_sync = True
        try:
            cam = self.camera
            if cam is None:
                return

            # 1. Enforce Parallel Projection (2D)
            if not cam.GetParallelProjection():
                cam.ParallelProjectionOn()

            # 2. Enforce ViewUp to be strictly (0, 0, 1)
            up = cam.GetViewUp()
            if abs(up[0]) > 1e-4 or abs(up[1]) > 1e-4 or abs(up[2] - 1.0) > 1e-4:
                cam.SetViewUp(0.0, 0.0, 1.0)

            # 3. Enforce Direction of Projection (align Position and Focal Point)
            view_mode = getattr(self.app, 'cross_view_mode', 'side')
            pos = list(cam.GetPosition())
            fp = list(cam.GetFocalPoint())

            if view_mode == 'side':
                # Force X and Z to match between position and focal point
                if abs(pos[0] - fp[0]) > 1e-4 or abs(pos[2] - fp[2]) > 1e-4:
                    pos[0] = fp[0]
                    pos[2] = fp[2]
                    cam.SetPosition(pos)
            else:
                # Force Y and Z to match between position and focal point
                if abs(pos[1] - fp[1]) > 1e-4 or abs(pos[2] - fp[2]) > 1e-4:
                    pos[1] = fp[1]
                    pos[2] = fp[2]
                    cam.SetPosition(pos)

        except Exception:
            pass
        finally:
            self._in_sync = False


def install_camera_2d_lock(vtk_widget, app, is_cut_section=False):
    """
    Ensure the active camera of the given VTK widget is locked to 2D.
    Prevents any rotation or 3D drift at the camera level, while fully
    preserving panning and zooming.
    """
    if vtk_widget is None:
        return
    # Guard: do not install lock on the main view
    if hasattr(app, "vtk_widget") and vtk_widget is app.vtk_widget:
        # If there was a lock on it, remove it
        if hasattr(vtk_widget, "_camera_2d_lock_id"):
            old_cam = getattr(vtk_widget, "_locked_camera", None)
            if old_cam is not None:
                try:
                    old_cam.RemoveObserver(vtk_widget._camera_2d_lock_id)
                except Exception:
                    pass
            try:
                delattr(vtk_widget, "_camera_2d_lock_id")
                delattr(vtk_widget, "_locked_camera")
            except Exception:
                pass
        return
    try:
        renderer = getattr(vtk_widget, "renderer", None)
        if renderer is None:
            return
        camera = renderer.GetActiveCamera()
        if camera is None:
            return

        # Avoid duplicate lock
        if getattr(vtk_widget, "_locked_camera", None) is camera:
            # Already locked to this camera, check if observer is active
            if hasattr(vtk_widget, "_camera_2d_lock_id"):
                return

        # If there was an old lock on a different camera, remove it
        if hasattr(vtk_widget, "_camera_2d_lock_id"):
            old_cam = getattr(vtk_widget, "_locked_camera", None)
            if old_cam is not None:
                try:
                    old_cam.RemoveObserver(vtk_widget._camera_2d_lock_id)
                except Exception:
                    pass
            try:
                delattr(vtk_widget, "_camera_2d_lock_id")
                delattr(vtk_widget, "_locked_camera")
            except Exception:
                pass

        # Create and install new lock callback
        lock_callback = Camera2DLock(camera, app, is_cut_section)
        obs_id = camera.AddObserver("ModifiedEvent", lock_callback)
        vtk_widget._camera_2d_lock_id = obs_id
        vtk_widget._locked_camera = camera

        # Enforce initial lock state immediately
        lock_callback(camera, "ModifiedEvent")
    except Exception as e:
        print(f"⚠️ Failed to install 2D Camera Lock: {e}")


class SpatialGridIndex:
    """
    Ultra-fast 2D grid index for point clouds (Main View or Section View).
    Reduces brush classification time from O(N) to O(1) cell lookup.
    Built in ~200ms for 15M points; queried in <1ms.
    """
    def __init__(self, pts2d):
        self.valid = False
        if pts2d is None or len(pts2d) == 0:
            return
            
        try:
            self.pts2d = pts2d
            self.min_u, self.min_z = pts2d.min(axis=0)
            max_u, max_z = pts2d.max(axis=0)
            
            range_u = max(max_u - self.min_u, 1e-6)
            range_z = max(max_z - self.min_z, 1e-6)
            
            # Heuristic for grid resolution: target ~150 points per cell
            n = len(pts2d)
            res = int(np.sqrt(n / 150))
            self.grid_res = max(20, min(res, 400)) # Cap resolution
            
            self.cell_u = range_u / self.grid_res
            self.cell_z = range_z / self.grid_res
            
            # Assign points to cells
            u_idx = ((pts2d[:, 0] - self.min_u) / self.cell_u).astype(np.int32)
            z_idx = ((pts2d[:, 1] - self.min_z) / self.cell_z).astype(np.int32)
            
            np.clip(u_idx, 0, self.grid_res - 1, out=u_idx)
            np.clip(z_idx, 0, self.grid_res - 1, out=z_idx)
            
            # Flattened grid index
            flat_idx = u_idx * self.grid_res + z_idx
            
            # Sort points by grid cell for fast slicing
            self.sort_perm = np.argsort(flat_idx)
            self.sorted_flat_idx = flat_idx[self.sort_perm]
            
            # Find start/end of each cell in the sorted array
            all_cell_ids = np.arange(self.grid_res * self.grid_res + 1)
            self.cell_boundaries = np.searchsorted(self.sorted_flat_idx, all_cell_ids)
            self.valid = True
        except Exception as e:
            print(f"⚠️ SpatialGridIndex build failed: {e}")

    def query_rectangle_minmax(self, u_min, z_min, u_max, z_max):
        """Returns indices of points within the specified bounding box."""
        if not self.valid:
            return np.array([], dtype=np.int32)

        u0 = max(0, int((u_min - self.min_u) / self.cell_u))
        u1 = min(self.grid_res - 1, int((u_max - self.min_u) / self.cell_u))
        z0 = max(0, int((z_min - self.min_z) / self.cell_z))
        z1 = min(self.grid_res - 1, int((z_max - self.min_z) / self.cell_z))

        if u0 > u1 or z0 > z1:
            return np.array([], dtype=np.int32)

        # Vectorized cell-ID computation — eliminates the Python nested for-loop.
        iu = np.arange(u0, u1 + 1, dtype=np.int32)
        iz = np.arange(z0, z1 + 1, dtype=np.int32)
        cell_ids = (iu[:, None] * self.grid_res + iz[None, :]).ravel()

        starts = self.cell_boundaries[cell_ids]
        ends   = self.cell_boundaries[cell_ids + 1]
        nonempty = ends > starts
        if not nonempty.any():
            return np.array([], dtype=np.int32)

        slices = [self.sort_perm[s:e] for s, e in zip(starts[nonempty], ends[nonempty])]
        indices = np.concatenate(slices) if len(slices) > 1 else slices[0]

        # Exact box filter on the candidates
        sub_pts = self.pts2d[indices]
        mask = ((sub_pts[:, 0] >= u_min) & (sub_pts[:, 0] <= u_max) &
                (sub_pts[:, 1] >= z_min) & (sub_pts[:, 1] <= z_max))
        return indices[mask]

    def query_radius(self, u, z, radius):
        """Legacy helper for circular queries."""
        return self.query_rectangle_minmax(u-radius, z-radius, u+radius, z+radius)

    def query_rectangle(self, u, z, radius):
        """Legacy helper for square queries."""
        return self.query_rectangle_minmax(u-radius, z-radius, u+radius, z+radius)

class PreviewOverlay(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        # Allow mouse events to pass through to the VTK interactor
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        # Allow a translucent background so we can draw overlays if needed
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # Start hidden; _init_preview_overlay will show and size it.
        self._shapes = []  # placeholder for future use
        self.hide()

    def set_shapes(self, shapes):
        self._shapes = list(shapes or [])
        if self._shapes:
            self.show()
            self.raise_()
        else:
            self.hide()
        self.update()

    def clear_shapes(self):
        self.set_shapes([])

    def translate_shapes(self, dx, dy):
        if not self._shapes or (dx == 0 and dy == 0):
            return

        shifted_shapes = []
        for shape in self._shapes:
            shifted_segments = []
            for p0, p1 in shape.get("segments", []):
                shifted_segments.append((
                    (float(p0[0]) + dx, float(p0[1]) + dy),
                    (float(p1[0]) + dx, float(p1[1]) + dy),
                ))

            shifted_shape = dict(shape)
            shifted_shape["segments"] = shifted_segments
            shifted_shapes.append(shifted_shape)

        self._shapes = shifted_shapes
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        for shape in self._shapes:
            color = shape.get("color", (255, 255, 0, 220))
            width = float(shape.get("width", 2.0))
            pen = QPen(QColor(*color))
            pen.setWidthF(max(width, 1.0))
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)

            for p0, p1 in shape.get("segments", []):
                painter.drawLine(
                    QLineF(
                        QPointF(float(p0[0]), float(p0[1])),
                        QPointF(float(p1[0]), float(p1[1])),
                    )
                )
        painter.end()


class ClassificationDoubleClickFilter(QObject):
    def __init__(self, parent=None, owner=None):
        super().__init__(parent)
        self.ignore_until = 0.0
        # Wrapping ClassificationInteractor, if provided — lets this filter know
        # which classify tool is active without importing app_window here.
        self.owner = owner

    def eventFilter(self, obj, event):
        if event is not None:
            et = event.type()
            if et in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease, QEvent.MouseButtonDblClick):
                # ✅ MicroStation tap-tap tools (above/below/parallel line) are two
                # deliberate clicks placed fast — well inside the OS double-click
                # window. Swallowing that here as a "double click" would eat the
                # second tap entirely (and block clicks for 500ms after), making
                # fast continuous classification look like it randomly stalls.
                # These tools don't use double-click for anything, so let every
                # click pass straight through untouched.
                app = getattr(self.owner, "app", None)
                if getattr(app, "active_classify_tool", None) in (
                    "above_line", "below_line", "parallel_line"
                ):
                    # This filter exists only to normalize rapid LEFT-button
                    # tap/tap input. Never translate middle-button pan (or
                    # right-button navigation) into a synthetic left click.
                    try:
                        event_button = event.button()
                        held_buttons = event.buttons()
                        if (
                            event_button == Qt.MiddleButton
                            or bool(held_buttons & Qt.MiddleButton)
                        ):
                            return False
                        if (
                            event_button not in (Qt.LeftButton, Qt.NoButton)
                            and not bool(held_buttons & Qt.LeftButton)
                        ):
                            return False
                    except Exception:
                        return False

                    # ROOT CAUSE of "classification silently stops responding
                    # mid-session" — two distinct bugs, both bypassed here by
                    # driving VTK's interactor directly instead of going
                    # through Qt's mousePressEvent/mouseReleaseEvent:
                    #
                    # 1) Under rapid tap-tap clicking, Qt/Windows can deliver a
                    #    Press or Release QMouseEvent where event.button()
                    #    intermittently comes back NoButton even though
                    #    event.buttons() (the held-button bitmask) correctly
                    #    shows LeftButton. QVTKRenderWindowInteractor's
                    #    mousePressEvent/mouseReleaseEvent gate purely on
                    #    event.button(), so the click is silently swallowed —
                    #    no exception, no crash, just dead silence forever
                    #    after. A coalesced second tap can also arrive as a
                    #    genuine MouseButtonDblClick, which this VTK class
                    #    never overrides either.
                    #
                    # 2) PyVista's own RenderWindowInteractor caches the style
                    #    it thinks is active and periodically reasserts it via
                    #    its own internal chart/context-style bookkeeping.
                    #    Since we set styles by calling SetInteractorStyle
                    #    directly on the raw VTK interactor (bypassing
                    #    pyvista's `.style` property), that cache goes stale
                    #    and pyvista silently stomps our style back to its own
                    #    default TrackballCamera mid-session.
                    try:
                        if et == QEvent.MouseButtonDblClick:
                            # Re-run SetInteractorStyle with our own style —
                            # this is the exact call
                            # attach_classification_to_all_section_views()
                            # makes when re-arming a tool onto an unchanged
                            # interactor, which reliably un-freezes a stuck
                            # session. Do it proactively here too.
                            style = getattr(self.owner, "style", None)
                            if style is not None:
                                try:
                                    obj.SetInteractorStyle(style)
                                except Exception:
                                    pass
                        pos = event.position()
                        ctrl = bool(event.modifiers() & Qt.ControlModifier)
                        shift = bool(event.modifiers() & Qt.ShiftModifier)
                        if hasattr(obj, "_setEventInformation"):
                            obj._setEventInformation(pos.x(), pos.y(), ctrl, shift, chr(0), 0, None)
                        iren = getattr(obj, "_Iren", obj)
                        # Self-heal bug (2): if the live style on the render
                        # window has drifted from ours, restore it before
                        # dispatching — every time, unconditionally, rather
                        # than trying to catch every place pyvista might do
                        # this internally.
                        try:
                            live_style = iren.GetInteractorStyle() if hasattr(iren, "GetInteractorStyle") else None
                            mine = getattr(self.owner, "style", None)
                            if live_style is not None and mine is not None and str(live_style) != str(mine):
                                obj.SetInteractorStyle(mine)
                        except Exception:
                            pass
                        if hasattr(iren, "GetEnabled") and not iren.GetEnabled():
                            iren.Enable()
                        obj._ActiveButton = Qt.LeftButton
                        if et == QEvent.MouseButtonRelease:
                            iren.LeftButtonReleaseEvent()
                        else:
                            iren.LeftButtonPressEvent()
                    except Exception:
                        pass
                    event.accept()
                    return True
                try:
                    import time
                    now = time.time()
                    
                    if now < self.ignore_until:
                        # Keep extending the block window slightly if they click continuously
                        self.ignore_until = max(self.ignore_until, now + 0.3)
                        event.accept()
                        return True
                        
                    if et == QEvent.MouseButtonDblClick:
                        # Block the double click and start the 500ms ignore window
                        self.ignore_until = now + 0.5
                        event.accept()
                        return True
                except Exception:
                    if et == QEvent.MouseButtonDblClick:
                        try:
                            event.accept()
                        except Exception:
                            pass
                        return True
        return False


class ClassificationInteractor:
    """
    Hybrid interactor for classification in cross-section dock.
    Left mouse → classification tools.
    Middle mouse → pan (while tools remain active).
    """

    def __init__(self, app, interactor, mode="2d"):
        """
        ✅ FIXED: Set self.app FIRST before any operations that might access it
        """
        # ✅ CRITICAL: Set instance variables FIRST
        self.app = app
        self.interactor = interactor
        self.mode = mode
        self.guide_actors = []  # Temporary visual guides (rubber-band lines)
        self.section_actors = [] 

        # added by bala
        # SAFE MULTI-CLASS SYNC (read-only, not modifying app)
        self.from_classes = getattr(app, "from_classes", None)
        self.to_class = getattr(app, "to_class", None)

         # ✅ NEW: Qt-based preview overlay (ultra-fast)
        self._preview_overlay = None
        self._last_update_time = 0
        self._last_render_time = 0 
        self._update_interval = 16  # ~60 FPS throttle (milliseconds)
        self._cached_renderer = None
        self._widget_height = 0
                
        # ✅ NEW FIX: Deactivate cut section tool if it's in WAITING state
        if hasattr(app, 'cut_section_controller'):
            cut_ctrl = app.cut_section_controller
            if hasattr(cut_ctrl, 'deactivate_if_waiting'):
                try:
                    cut_ctrl.deactivate_if_waiting()
                    print("✅ Checked and handled cut section tool state")
                except Exception as e:
                    print(f"⚠️ Failed to deactivate cut section: {e}")

        # Cancel any active cut section mode (legacy code - kept for backwards compatibility)
        if hasattr(app, 'section_controller') and hasattr(app.section_controller, "_cut_active"):
            if app.section_controller._cut_active:
                app.section_controller.cancel_cut_section()

        # base style
        if mode == "2d":
            self.style = vtkInteractorStyleImage()
            # Lock to 2D: prevents VTK's default Image3D mode from allowing camera
            # rotation during rapid click sequences (the root cause of 2D→3D drift).
            self.style.SetInteractionModeToImage2D()
        else:
            self.style = vtkInteractorStyleTrackballCamera()

        self.interactor.SetInteractorStyle(self.style)

        # Block Qt-level double clicks to prevent propagation to QDockWidget (which causes deactivation)
        self._dblclick_filter = ClassificationDoubleClickFilter(self.interactor, owner=self)
        self.interactor.installEventFilter(self._dblclick_filter)

        # disable double-click reset and abort event
        #
        # ROOT CAUSE of "classification silently stops responding mid-session":
        # when two taps land close enough together for the OS to coalesce them,
        # Qt delivers a MouseButtonDblClick, and QVTKRenderWindowInteractor's own
        # mousePressEvent sets repeat=1 for it. VTK's interactor then fires
        # "LeftButtonDoubleClickEvent" INSTEAD OF "LeftButtonPressEvent" for that
        # click. Previously we just AbortFlagOn()'d it here (to block VTK's
        # default double-click camera behavior) — which silently ate the tap
        # entirely for above/below/parallel-line tools. Since that tap never
        # reached on_left_press, click_to_finalize/P1 state was left dangling,
        # and the interaction looked "stuck" even though every later real click
        # kept arriving fine (confirmed via QApplication.notify tracing).
        def _on_left_dbl_click(o, e):
            tool = getattr(getattr(self, "app", None), "active_classify_tool", None)
            if tool in ("above_line", "below_line", "parallel_line"):
                self.on_left_press(o, e)
            if hasattr(o, "AbortFlagOn"):
                o.AbortFlagOn()
        self.style.AddObserver("LeftButtonDoubleClickEvent", _on_left_dbl_click)

        # attach events
        self.style.AddObserver("LeftButtonPressEvent", self.on_left_press)
        self.style.AddObserver("LeftButtonReleaseEvent", self.on_left_release)
        self.style.AddObserver("KeyPressEvent", self.on_key_press)

        # ✅ CRITICAL FIX: Use custom mouse move handler that ALWAYS shows previews
        self.style.AddObserver("MouseMoveEvent", self._on_mouse_move_with_preview)

        # Install 2D camera lock immediately for 2D mode
        if mode == "2d":
            try:
                vtk_widget = self._get_active_vtk_widget()
                is_cut = (getattr(self.app, "active_mode", None) == "cut") or getattr(self, "is_cut_section_mode", False)
                install_camera_2d_lock(vtk_widget, self.app, is_cut)
            except Exception as e:
                print(f"⚠️ Initial lock install failed: {e}")

        # ✅ CUSTOM PAN: Do NOT forward to style.OnMiddleButtonDown/Up — those call
        # VTK's internal C++ Render() which segfaults on stale render windows.
        # Instead, implement pan manually with safe deferred rendering.
        self._is_panning = False
        self._last_pan_pos = (0, 0)
        self._pan_render_pending = False
        self._preview_hidden_for_pan = False
        self._pan_hidden_actor_names = []
        self._deferred_left_release_timer = QTimer()
        self._deferred_left_release_timer.setInterval(16)
        self._deferred_left_release_timer.timeout.connect(self._check_deferred_left_release)
        self.style.AddObserver("MiddleButtonPressEvent", self._on_safe_pan_start)
        self.style.AddObserver("MiddleButtonReleaseEvent", self._on_safe_pan_stop)

        # Restore scroll-wheel zoom for 2D mode.
        # SetInteractionModeToImage2D() changes VTK's scroll handler to "Slice"
        # (steps through image slices) instead of Dolly.  Override it here so
        # scroll always zooms via ParallelScale regardless of interaction mode.
        if mode == "2d":
            self.style.AddObserver("MouseWheelForwardEvent", self._on_scroll_zoom_in)
            self.style.AddObserver("MouseWheelBackwardEvent", self._on_scroll_zoom_out)

        # state
        self.P1 = None
        self.is_dragging = False 
        self._gesture_tool = None
        self.line_actor = None
        self.dotted_actor = None
        self.rect_actor = None
        self.circle_actor = None
        self.poly_actor = None
        self.poly_actor_cut = None
        self.brush_actor = None
        self.freehand_actor = None

        # polygon / freehand classification state
        self.drawing_points = []
        self.is_drawing_freehand = False
        
        self._brush_grid_size = 50  # Grid cells for spatial lookup
        self._brush_spatial_index      = None
        self._brush_spatial_index_xyz_id = None  # id()-based cache key; rebuild only on new file
        self._brush_sort_order         = None
        self._brush_grid_params        = None
        self._main_grid_index          = None   # SpatialGridIndex for main-view brush
        self._main_points_2d           = None   # 2-D projection cache for main-view
        self._prebuild_thread          = None   # background pre-warm thread
        import threading
        self._build_index_lock         = threading.Lock()  # guards _build_spatial_index_for_brush


        # ✅ BRUSH OPTIMIZATION: Smooth interpolation
        self._brush_interpolation_step = 0.5  # Smaller = smoother
        self._brush_min_move_distance = 0.1  # Minimum movement to register

        # ✅ BRUSH OPTIMIZATION: Render throttling
        self._brush_render_counter = 0
        self._brush_render_every = 3

        # Brush stroke state (initialized here so cleanup() is always safe)
        self._brush_accumulated_mask = None
        self._brush_old_classes = {}
        self._brush_frame_chunks = []
        self._brush_stroke_positions = []
        self._last_brush_center = None
        self._brush_needs_render = False
        self._brush_last_render_time = 0.0
        self._brush_current_to_class = None
        self._brush_visible_classes = None

        # Background worker (kept alive across strokes, stopped in cleanup())
        self._brush_worker = None
        self._brush_worker_active = False
        self._roi_preview = None
        
        self.click_to_finalize = False
        self._press_pos = None
        self._line_press_max_move_px = 0.0
        self._last_active_tool = getattr(app, "active_classify_tool", None)
        self._ignore_next_left_release = False
        self._suppress_release_observer = False

        print("✅ ClassificationInteractor initialized with live previews")
        self.style.AddObserver("RightButtonPressEvent", self.on_right_press)

    def _check_tool_changed(self):
        """Reset drawing state if the active tool has changed since the last event."""
        if not hasattr(self, 'app') or self.app is None:
            return
        current_tool = getattr(self.app, "active_classify_tool", None)
        if getattr(self, "_last_active_tool", None) != current_tool:
            self.click_to_finalize = False
            self.P1 = None
            self.is_dragging = False
            self._ignore_next_left_release = False
            self._suppress_release_observer = False
            self.drawing_points = []
            self.is_drawing_freehand = False
            self._clear_all_previews()
            self._last_active_tool = current_tool

    def _has_incomplete_classification_gesture(self):
        """Return whether this view owns classification input that is not finished."""
        gesture_tool = (
            getattr(self, "_gesture_tool", None)
            or getattr(getattr(self, "app", None), "active_classify_tool", None)
        )
        # Brush changes are applied continuously while dragging and its release
        # path records the undo entry. Never discard that transactional state as
        # if it were a preview-only gesture.
        if gesture_tool == "brush":
            return False

        drawing_points = getattr(self, "drawing_points", None)
        cut_drawing_points = getattr(self, "drawing_points_display_cut", None)
        cut_world_points = getattr(self, "drawing_points_world_cut", None)

        return bool(
            getattr(self, "P1", None) is not None
            or getattr(self, "P1_display_cut", None) is not None
            or getattr(self, "is_dragging", False)
            or getattr(self, "click_to_finalize", False)
            or getattr(self, "is_drawing_freehand", False)
            or (drawing_points is not None and len(drawing_points) > 0)
            or (cut_drawing_points is not None and len(cut_drawing_points) > 0)
            or (cut_world_points is not None and len(cut_world_points) > 0)
        )

    def _cancel_incomplete_classification_gesture(self):
        """Cancel this view's transient gesture without changing the active tool."""
        if not self._has_incomplete_classification_gesture():
            return False

        try:
            self._stop_deferred_left_release_watch()
        except Exception:
            pass
        try:
            self._suppress_snt_text(False)
        except Exception:
            pass

        self._gesture_tool = None
        self._press_pos = None
        self._line_press_max_move_px = 0.0
        self._last_line_preview_P2 = None
        self._last_rectangle_preview_P2 = None
        self._last_circle_preview_P2 = None

        app = getattr(self, "app", None)
        if app is not None:
            app._classification_preview_active = False
            app._suppress_section_refresh = False

        self._clear_all_previews()
        return True

    def _cancel_incomplete_classification_gestures_in_other_views(self):
        """Make an unfinished classification gesture exclusive to one section view."""
        app = getattr(self, "app", None)
        if app is None:
            return 0

        peers = []
        section_interactors = getattr(app, "classify_interactors", None)
        if isinstance(section_interactors, dict):
            peers.extend(section_interactors.values())

        cut_interactor = getattr(app, "cut_classify_interactor", None)
        if cut_interactor is not None:
            peers.append(cut_interactor)

        cancelled = 0
        seen = set()
        for peer in peers:
            if peer is None or peer is self:
                continue
            marker = id(peer)
            if marker in seen:
                continue
            seen.add(marker)

            cancel = getattr(peer, "_cancel_incomplete_classification_gesture", None)
            if not callable(cancel):
                continue
            try:
                if cancel():
                    cancelled += 1
            except Exception as exc:
                print(f"⚠️ Failed to clear classification preview in another view: {exc}")

        if cancelled:
            print(
                f"🧹 Cleared unfinished classification gesture from "
                f"{cancelled} other view(s)"
            )
        return cancelled

    def _notify_style_left_button_up(self):
        """
        Safely forward a synthetic left-button-up to VTK without letting our
        own release observer re-enter and scramble the click-to-finalize state.
        """
        style = getattr(self, "style", None)
        if style is None or getattr(self, "_suppress_release_observer", False):
            return

        self._suppress_release_observer = True
        try:
            style.OnLeftButtonUp()
        except Exception:
            pass
        finally:
            self._suppress_release_observer = False

    def cleanup(self):
        """Clear transient preview state before this interactor is discarded."""
        # Remove all observers added in __init__ to prevent accumulation across
        # tool switches (each switch creates a new interactor, adding 6+ observers
        # to the same VTK style; after 100+ switches this causes O(N) dispatch cost
        # and holds references preventing GC).
        try:
            if self.style is not None:
                for event in ("LeftButtonPressEvent", "LeftButtonReleaseEvent",
                              "KeyPressEvent", "MouseMoveEvent",
                              "MiddleButtonPressEvent", "MiddleButtonReleaseEvent",
                              "LeftButtonDoubleClickEvent", "RightButtonPressEvent",
                              "MouseWheelForwardEvent", "MouseWheelBackwardEvent"):
                    try:
                        self.style.RemoveObservers(event)
                    except Exception:
                        pass
        except Exception:
            pass

        try:
            self._finalize_pending_main_brush_stroke()
        except Exception as e:
            print(f"⚠️ ClassificationInteractor cleanup brush finalize failed: {e}")

        try:
            self._clear_all_previews()
        except Exception as e:
            print(f"⚠️ ClassificationInteractor cleanup preview clear failed: {e}")

        overlay = getattr(self, "_preview_overlay", None)
        if overlay is not None:
            try:
                overlay.hide()
                overlay.deleteLater()
            except Exception as e:
                print(f"⚠️ ClassificationInteractor overlay cleanup failed: {e}")
            self._preview_overlay = None

        # Remove double-click event filter
        filter_obj = getattr(self, "_dblclick_filter", None)
        if filter_obj is not None and getattr(self, "interactor", None) is not None:
            try:
                self.interactor.removeEventFilter(filter_obj)
            except Exception:
                pass
            self._dblclick_filter = None

        self.P1 = None
        self.is_dragging = False
        self.click_to_finalize = False
        self._ignore_next_left_release = False
        self._suppress_release_observer = False
        self._press_pos = None
        self.drawing_points = []
        self.is_drawing_freehand = False
        self._is_panning = False  # ✅ BUG FIX: Reset panning state on cleanup
        self._pan_render_pending = False

        self._hide_preview_overlay_for_pan()
        timer = getattr(self, "_deferred_left_release_timer", None)
        if timer is not None:
            try:
                timer.stop()
            except Exception:
                pass
            try:
                timer.timeout.disconnect(self._check_deferred_left_release)
            except Exception:
                pass
            try:
                timer.deleteLater()
            except Exception:
                pass
            self._deferred_left_release_timer = None

        # ── Task-2/3: cleanup ROI preview and worker on interactor discard ──
        try:
            roi = getattr(self, "_roi_preview", None)
            if roi is not None:
                roi.destroy()
                self._roi_preview = None
        except Exception:
            pass
        try:
            w = getattr(self, "_brush_worker", None)
            if w is not None:
                # 1. Disconnect signal FIRST — prevents queued results from
                #    arriving on a partially-destroyed slot after stop().
                try:
                    w.result_ready.disconnect()
                except Exception:
                    pass
                # 2. Signal the thread to stop.
                w.stop()
                # 3. Drain the input queue so the thread doesn't block on get().
                try:
                    import queue as _q
                    while True:
                        w._queue.get_nowait()
                except Exception:
                    pass
                # 4. Wait for thread to finish; hard-kill if it hangs.
                if not w.wait(3000):
                    w.terminate()
                    w.wait(500)
                self._brush_worker = None
                self._brush_worker_active = False
        except Exception:
            pass

    def _get_preview_actor_names(self):
        return [
            "line_actor", "dotted_actor", "rect_actor", "circle_actor", "circle_actor_main",
            "brush_actor", "polygon_actor", "freehand_actor", "poly_actor",
            "line_actor_cut", "dotted_actor_cut", "rect_actor_cut", "circle_actor_cut",
            "brush_actor_cut", "freehand_actor_cut", "poly_actor_cut",
            "_locate_line_actor",
        ]

    def _get_active_tool_preview_actor_names(self, tool=None):
        tool = tool or getattr(self.app, "active_classify_tool", None)
        vtk_widget = self._get_active_vtk_widget()
        cut_vtk = getattr(getattr(self.app, "cut_section_controller", None), "cut_vtk", None)
        suffix = "_cut" if (vtk_widget is not None and cut_vtk is not None and vtk_widget == cut_vtk) else ""
        mapping = {
            "above_line": [f"line_actor{suffix}", f"dotted_actor{suffix}"],
            "below_line": [f"line_actor{suffix}", f"dotted_actor{suffix}"],
            "parallel_line": [f"line_actor{suffix}", f"dotted_actor{suffix}"],
            "rectangle": [f"rect_actor{suffix}"],
            "circle": [f"circle_actor_main"] if suffix == "" and self._is_main_view() else [f"circle_actor{suffix}"],
            "freehand": [f"freehand_actor{suffix}"],
            "polygon": [f"poly_actor{suffix}"],
            "brush": [f"brush_actor{suffix}"],
        }
        return mapping.get(tool, [])

    def _get_active_palette(self):
        """Helper to get the correct palette for the current active view."""
        app = getattr(self, "app", None)
        if app is None:
            return {}

        def _get_slot_palette(slot_idx: int):
            # Priority 1: dialog-owned per-slot palettes (authoritative)
            for dlg_attr in ("display_mode_dialog", "display_dialog"):
                dlg = getattr(app, dlg_attr, None)
                vp = getattr(dlg, "view_palettes", None) if dlg is not None else None
                if isinstance(vp, dict):
                    pal = vp.get(slot_idx)
                    if pal:
                        return pal

            # Priority 2: app-owned per-slot palettes
            vp = getattr(app, "view_palettes", None)
            if isinstance(vp, dict):
                pal = vp.get(slot_idx)
                if pal:
                    return pal

            # Priority 3: canonical main palette fallback
            if slot_idx == 0:
                return getattr(app, "class_palette", {}) or {}
            return {}

        def _seed_isolated_slot(slot_idx: int):
            # Prefer app-level helper (keeps behavior aligned with AppWindow).
            if hasattr(app, "_get_cross_section_palette"):
                try:
                    pal = app._get_cross_section_palette(
                        slot_idx,
                        allow_default_seed=True,
                        persist_seed=False,
                    )
                    if isinstance(pal, dict) and pal:
                        return pal
                except Exception:
                    pass

            # Local fallback: clone main colors/weights with independent visibility.
            master = getattr(app, "class_palette", {}) or {}
            if not isinstance(master, dict) or not master:
                return {}
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
                    "description": str(info.get("description", "")),
                    "color": tuple(info.get("color", (128, 128, 128))),
                    "weight": float(info.get("weight", 1.0)),
                    "draw": info.get("draw", ""),
                    "lvl": str(info.get("lvl", "")),
                }
            return seeded

        # Main view must always use slot 0, regardless of section active_view state.
        if self._is_main_view():
            return _get_slot_palette(0)

        vtk_widget = self._get_active_vtk_widget()

        # Cut section uses slot 5. If not configured yet, seed isolated defaults.
        cut_ctrl = getattr(app, "cut_section_controller", None)
        if cut_ctrl is not None:
            cut_vtk = getattr(cut_ctrl, "cut_vtk", None)
            if cut_vtk is not None and vtk_widget == cut_vtk:
                return _get_slot_palette(5) or _seed_isolated_slot(5)

        # Cross-section dock: view_idx is 0-based, DisplayMode slots are 1-based.
        view_idx = self._get_view_index_from_interactor()
        if view_idx is not None:
            try:
                slot_idx = int(view_idx) + 1
            except Exception:
                slot_idx = None
            if slot_idx is not None:
                pal = _get_slot_palette(slot_idx)
                if pal:
                    return pal
                seeded = _seed_isolated_slot(slot_idx)
                if seeded:
                    return seeded

        # Secondary fallback via section controller active view (also 0-based).
        sec_ctrl = getattr(app, "section_controller", None)
        if sec_ctrl is not None:
            active_view = getattr(sec_ctrl, "active_view", None)
            if active_view is not None:
                try:
                    slot_idx = int(active_view) + 1
                except Exception:
                    slot_idx = None
                if slot_idx is not None:
                    pal = _get_slot_palette(slot_idx)
                    if pal:
                        return pal
                    seeded = _seed_isolated_slot(slot_idx)
                    if seeded:
                        return seeded

        # If context was not resolvable, do not leak slot-0 visibility.
        return {}

    def _suppress_snt_text(self, suppress: bool):
        """
        🚀 Senior Optimization: Suppress heavy SNT/DXF text actors during drag.
        SNT labels (vtkVectorText) are extremely heavy due to high polygon counts 
        and thousands of separate DrawCalls. Hiding them during interaction 
        restores 60 FPS while keeping the context points visible.
        """
        app = getattr(self, 'app', None)
        if app is None: return
        
        if suppress:
            if getattr(self, '_snt_text_suppressed', False):
                return
            
            self._suppressed_text_actors = []
            # Gather all SNT actors
            for entry in getattr(app, 'snt_actors', []):
                for actor in entry.get('actors', []):
                    # Only hide text labels (tagged in snt_attachment.py)
                    if getattr(actor, 'is_grid_label', False):
                        if actor.GetVisibility():
                            actor.VisibilityOff()
                            self._suppressed_text_actors.append(actor)
            
            self._snt_text_suppressed = True
            if len(self._suppressed_text_actors) > 50:
                print(f"   🚀 SNT Text Suppressed: Hiding {len(self._suppressed_text_actors)} heavy labels for 60FPS interaction")
        else:
            if not getattr(self, '_snt_text_suppressed', False):
                return
            
            for actor in getattr(self, '_suppressed_text_actors', []):
                try:
                    actor.VisibilityOn()
                except Exception:
                    pass
            
            self._suppressed_text_actors = []
            self._snt_text_suppressed = False

    def _set_preview_visibility(self, visible, actor_names=None):
        """Toggle preview actors without clearing the active classification state."""
        actor_names = actor_names or self._get_preview_actor_names()

        for name in actor_names:
            actor = getattr(self, name, None)
            if actor is None:
                continue
            if visible:
                actor.VisibilityOn()
            else:
                actor.VisibilityOff()

    def _get_visible_preview_actor_names(self, actor_names=None):
        visible = []
        vtk_widget = self._get_active_vtk_widget()
        renderer = getattr(vtk_widget, "renderer", None) if vtk_widget is not None else None
        actor_names = actor_names or self._get_preview_actor_names()
        for name in actor_names:
            actor = getattr(self, name, None)
            if actor is None:
                continue
            try:
                if renderer is not None and hasattr(renderer, "HasViewProp"):
                    if not renderer.HasViewProp(actor):
                        continue
                if actor.GetVisibility():
                    visible.append(name)
            except Exception:
                continue
        return visible

    def _collect_preview_overlay_shapes(self, actor_names=None):
        """Capture currently visible preview actors as display-space line segments."""
        actor_names = actor_names or self._get_preview_actor_names()
        vtk_widget = self._get_active_vtk_widget()
        renderer = getattr(vtk_widget, "renderer", None) if vtk_widget is not None else None

        shapes = []
        id_list = vtk.vtkIdList()

        for name in actor_names:
            actor = getattr(self, name, None)
            if actor is None:
                continue

            try:
                if renderer is not None and hasattr(renderer, "HasViewProp"):
                    if not renderer.HasViewProp(actor):
                        continue
                if not actor.GetVisibility():
                    continue
            except Exception:
                continue

            try:
                mapper = actor.GetMapper()
                poly = mapper.GetInput() if mapper is not None else None
                points = poly.GetPoints() if poly is not None else None
                lines = poly.GetLines() if poly is not None else None
            except Exception:
                continue

            if points is None or lines is None:
                continue

            try:
                lines.InitTraversal()
            except Exception:
                continue

            segments = []
            while lines.GetNextCell(id_list):
                if id_list.GetNumberOfIds() < 2:
                    continue

                prev_id = id_list.GetId(0)
                prev_pt = points.GetPoint(prev_id)
                for idx in range(1, id_list.GetNumberOfIds()):
                    curr_id = id_list.GetId(idx)
                    curr_pt = points.GetPoint(curr_id)

                    # Freeze overlay in DISPLAY space so it stays visually fixed
                    prev_xy = (float(prev_pt[0]), float(prev_pt[1]))
                    curr_xy = (float(curr_pt[0]), float(curr_pt[1]))
                    # Most classification preview actors are vtkActor2D with
                    # display-space points already. Only world-project non-2D actors.
                    if renderer is not None and not isinstance(actor, vtk.vtkActor2D):
                        try:
                            coord = vtk.vtkCoordinate()
                            coord.SetCoordinateSystemToWorld()
                            coord.SetValue(float(prev_pt[0]), float(prev_pt[1]), float(prev_pt[2]))
                            d0 = coord.GetComputedDisplayValue(renderer)
                            coord.SetValue(float(curr_pt[0]), float(curr_pt[1]), float(curr_pt[2]))
                            d1 = coord.GetComputedDisplayValue(renderer)
                            prev_xy = (float(d0[0]), float(d0[1]))
                            curr_xy = (float(d1[0]), float(d1[1]))
                        except Exception:
                            pass

                    segments.append(
                        (
                            prev_xy,
                            curr_xy,
                        )
                    )
                    prev_pt = curr_pt

            if not segments:
                continue

            prop = actor.GetProperty() if hasattr(actor, "GetProperty") else None
            color = (255, 255, 0, 220)
            width = 2.0
            if prop is not None:
                try:
                    rgb = prop.GetColor()
                    opacity = prop.GetOpacity()
                    color = (
                        int(max(0.0, min(1.0, rgb[0])) * 255),
                        int(max(0.0, min(1.0, rgb[1])) * 255),
                        int(max(0.0, min(1.0, rgb[2])) * 255),
                        int(max(0.0, min(1.0, opacity)) * 255),
                    )
                    width = float(prop.GetLineWidth())
                except Exception:
                    pass

            shapes.append({
                "segments": segments,
                "color": color,
                "width": width,
            })

        return shapes

    def _show_preview_overlay_for_pan(self):
        """Freeze the current preview in a Qt overlay while the camera pans."""
        tool = getattr(self, "_gesture_tool", None) or getattr(self.app, "active_classify_tool", None)
        active_actor_names = self._get_active_tool_preview_actor_names(tool)
        if active_actor_names:
            non_active_actor_names = [
                n for n in self._get_preview_actor_names() if n not in active_actor_names
            ]
            # Ensure stale previews from other tools/views are not visible during pan.
            self._set_preview_visibility(False, non_active_actor_names)
        self._pan_hidden_actor_names = self._get_visible_preview_actor_names(active_actor_names)
        if not self._pan_hidden_actor_names:
            return

        shapes = self._collect_preview_overlay_shapes(self._pan_hidden_actor_names)
        if not shapes:
            # Keep live preview visible if overlay freeze data is unavailable.
            self._preview_hidden_for_pan = False
            return

        if self._preview_overlay is None:
            self._init_preview_overlay()
        else:
            self._update_overlay_geometry()

        if self._preview_overlay is None:
            return

        self._preview_overlay.set_shapes(shapes)
        try:
            self._update_overlay_geometry()
            self._preview_overlay.show()
            self._preview_overlay.raise_()
            self._preview_overlay.update()
        except Exception:
            pass
        # Freeze mode: hide live preview actors so the visible overlay
        # stays fixed while camera pans.
        self._set_preview_visibility(False, self._pan_hidden_actor_names)
        self._preview_hidden_for_pan = True

    def _hide_preview_overlay_for_pan(self):
        """Dismiss the frozen overlay preview and restore VTK preview actors."""
        overlay = getattr(self, "_preview_overlay", None)
        if overlay is not None:
            try:
                overlay.clear_shapes()
            except Exception:
                try:
                    overlay.hide()
                except Exception:
                    pass

        hidden_actor_names = getattr(self, "_pan_hidden_actor_names", [])
        if getattr(self, "_preview_hidden_for_pan", False):
            self._set_preview_visibility(True, hidden_actor_names)

        self._pan_hidden_actor_names = []
        self._preview_hidden_for_pan = False

    def _translate_preview_actors_for_pan(self, dx, dy, actor_names=None):
        """Shift live preview actors in display space so they stay aligned while panning."""
        if dx == 0 and dy == 0:
            return

        actor_names = actor_names or self._get_preview_actor_names()

        for name in actor_names:
            actor = getattr(self, name, None)
            if actor is None:
                continue

            try:
                if not actor.GetVisibility():
                    continue
            except Exception:
                continue

            try:
                mapper = actor.GetMapper()
                poly = mapper.GetInput() if mapper is not None else None
                points = poly.GetPoints() if poly is not None else None
                if points is None:
                    continue

                for idx in range(points.GetNumberOfPoints()):
                    x, y, z = points.GetPoint(idx)
                    points.SetPoint(idx, x + dx, y + dy, z)

                points.Modified()
                if poly is not None:
                    poly.Modified()
            except Exception:
                continue

        overlay = getattr(self, "_preview_overlay", None)
        if overlay is not None and getattr(self, "_preview_hidden_for_pan", False):
            try:
                overlay.translate_shapes(dx, dy)
            except Exception:
                pass

    def _has_active_drag_classification(self, tool=None):
        """Return True while a left-drag classification gesture is in progress."""
        if getattr(self, "click_to_finalize", False):
            return False  # waiting for second tap — not a drag
        app = getattr(self, "app", None)
        tool = tool or getattr(app, "active_classify_tool", None)
        if tool == "freehand":
            return bool(getattr(self, "is_drawing_freehand", False))
        return bool(getattr(self, "is_dragging", False) and getattr(self, "P1", None) is not None)

    def _stop_deferred_left_release_watch(self):
        timer = getattr(self, "_deferred_left_release_timer", None)
        if timer is None:
            return
        try:
            timer.stop()
        except Exception:
            pass

    def _start_deferred_left_release_watch(self):
        """Fallback for the left-release event path during/after middle-button pan."""
        if getattr(self, "app", None) is None:
            return
        if not self._has_active_drag_classification():
            return

        timer = getattr(self, "_deferred_left_release_timer", None)
        if timer is None:
            return

        if not timer.isActive():
            timer.start()

    def _check_deferred_left_release(self):
        """
        Some middle-pan sequences do not forward a final LeftButtonReleaseEvent
        through VTK. When that happens, finish the paused classification as soon
        as Qt reports the left button is no longer pressed.
        """
        if getattr(self, "app", None) is None:
            self._stop_deferred_left_release_watch()
            return

        if not self._has_active_drag_classification():
            self._stop_deferred_left_release_watch()
            return

        try:
            from PySide6.QtWidgets import QApplication
            buttons = QApplication.mouseButtons()
        except Exception:
            return

        middle_pressed = bool(buttons & Qt.MiddleButton)

        left_pressed = bool(buttons & Qt.LeftButton)

        # Keep our internal pan state in sync even if VTK misses MiddleButtonReleaseEvent.
        if getattr(self, "_is_panning", False) and not middle_pressed:
            self._is_panning = False
            self._last_mouse_move_time = 0.0
            self._last_render_time = 0.0
            self._last_pan_pos = (0, 0)
            self._hide_preview_overlay_for_pan()

        if left_pressed:
            return

        if getattr(self, "_is_panning", False):
            self._is_panning = False
            self._last_pan_pos = (0, 0)
            self._hide_preview_overlay_for_pan()

        self._stop_deferred_left_release_watch()
        self.on_left_release(self.interactor, "DeferredLeftReleaseAfterPan")

    def _finalize_pending_main_brush_stroke(self):
        """
        Finalize a live main-view brush stroke when the tool ends without a
        normal mouse-release event, so undo/redo still works.
        """
        if not self._is_main_view():
            return None

        mask = getattr(self, "_brush_accumulated_mask", None)
        old_classes_dict = getattr(self, "_brush_old_classes", None) or {}
        to_class = getattr(self.app, "to_class", None)
        undo_mask = None

        if mask is not None and np.any(mask):
            if old_classes_dict and to_class is not None:
                indices = np.array(sorted(old_classes_dict.keys()), dtype=np.int64)
                old_cls = np.array(
                    [old_classes_dict[int(i)] for i in indices],
                    dtype=self.app.data["classification"].dtype,
                )
                new_cls = np.full(len(indices), to_class, dtype=old_cls.dtype)

                undo_mask = np.zeros(len(self.app.data["xyz"]), dtype=bool)
                undo_mask[indices] = True

                self.app.undo_stack.append({
                    "indices": indices.copy(),
                    "old_classes": old_cls,
                    "new_classes": new_cls,
                })
                self.app.redo_stack.clear()
                self.app._last_changed_mask = undo_mask
                self.app._last_changed_indices = indices.copy()

                try:
                    from gui.memory_manager import current_recommended_undo_steps
                    max_steps = int(current_recommended_undo_steps(self.app))
                except Exception:
                    max_steps = int(getattr(self.app, "_max_undo_steps", 30))
                while len(self.app.undo_stack) > max_steps:
                    from gui.memory_manager import _free_undo_entry
                    _free_undo_entry(self.app.undo_stack.pop(0))

                try:
                    from gui.point_count_widget import refresh_point_statistics
                    refresh_point_statistics(self.app)
                except Exception:
                    pass

                if hasattr(self.app, "statusBar"):
                    self.app.statusBar().showMessage(
                        f"✅ {len(indices):,} points → class {to_class}", 3000
                    )

            if getattr(self.app, "display_mode", "class") != "shaded_class":
                self.app._gpu_sync_done = True
                self.app._section_visibility_refresh_required = False
                try:
                    from gui.unified_actor_manager import guarantee_main_view_visual_refresh
                    guarantee_main_view_visual_refresh(
                        self.app,
                        changed_mask=undo_mask,
                        to_class=to_class,
                        reason="main_brush_finalize",
                    )
                except Exception:
                    pass
            elif _shading_mesh_exists(self.app):
                try:
                    from gui.shading_display import refresh_shaded_after_classification_fast
                    refresh_shaded_after_classification_fast(
                        self.app,
                        changed_mask=mask,
                    )
                except Exception as e:
                    print(f"⚠️ Brush cleanup shading refresh: {e}")
            else:
                self.app._gpu_sync_done = True
                self.app._section_visibility_refresh_required = False
                try:
                    from gui.unified_actor_manager import guarantee_main_view_visual_refresh
                    guarantee_main_view_visual_refresh(
                        self.app,
                        changed_mask=undo_mask,
                        to_class=to_class,
                        reason="brush_cleanup_fallback",
                    )
                except Exception:
                    pass

            try:
                if hasattr(self.app, "section_vtks") and undo_mask is not None:
                    from gui.unified_actor_manager import fast_cross_section_update, build_section_unified_actor
                    for view_idx in self.app.section_vtks:
                        try:
                            slot_idx = view_idx + 1
                            dialog = (
                                getattr(self.app, "display_mode_dialog", None)
                                or getattr(self.app, "display_dialog", None)
                            )
                            view_palette = {}
                            if dialog is not None and hasattr(dialog, "view_palettes"):
                                view_palette = dialog.view_palettes.get(slot_idx) or {}
                            if not view_palette and hasattr(self.app, "view_palettes"):
                                view_palette = self.app.view_palettes.get(slot_idx) or {}

                            ok = fast_cross_section_update(
                                self.app,
                                view_idx,
                                undo_mask,
                                palette=view_palette,
                                force_visibility_refresh=False,
                            )
                            if not ok:
                                border = float(getattr(self.app, "view_borders", {}).get(slot_idx, 0) or 0.0)
                                view_mode = getattr(self.app, "cross_view_mode", "front")
                                rebuilt = build_section_unified_actor(
                                    self.app,
                                    view_idx,
                                    palette=view_palette,
                                    border_percent=border,
                                    view=view_mode,
                                )
                                if rebuilt is not None:
                                    fast_cross_section_update(
                                        self.app,
                                        view_idx,
                                        undo_mask,
                                        palette=view_palette,
                                        force_visibility_refresh=False,
                                    )
                        except Exception:
                            pass

                mode_for_main_render = str(getattr(self.app, "display_mode", "class") or "class").lower()
                # Guard: do not re-render slot-0 in non-class modes after classify/undo cleanup.
                if mode_for_main_render in ("class", "shaded_class"):
                    self._safe_render_pyvista(self.app.vtk_widget)
                if hasattr(self.app, "section_vtks"):
                    for vtk_widget in self.app.section_vtks.values():
                        if vtk_widget:
                            self._safe_render_pyvista(vtk_widget)
            except Exception:
                pass

            try:
                if hasattr(self.app, "cut_section_controller"):
                    ctrl = self.app.cut_section_controller
                    if (
                        ctrl.is_cut_view_active
                        and ctrl.cut_points is not None
                        and ctrl._cut_index_map is not None
                        and ctrl.cut_vtk is not None
                    ):
                        ctrl._refresh_cut_colors_fast()
            except Exception as e:
                print(f"⚠️ Brush cleanup cut refresh failed: {e}")

        self._brush_accumulated_mask = None
        self._brush_old_classes = {}
        self._brush_frame_chunks = []
        self._brush_stroke_positions = []
        self._last_brush_center = None
        self._brush_needs_render = False
        self._brush_visible_classes = None
        self.app._suppress_section_refresh = False
        self.is_dragging = False

        return undo_mask

    # ═══════════════════════════════════════════════════════════════════════
    # ✅ SAFE RENDER GUARDS - Prevents crash on stale VTK render windows
    # Root cause: switching between cut section ↔ synchronized views leaves
    # render windows in an invalid state. Unguarded Render() dereferences
    # near-null pointer (0x24) → win32 memory access violation → hard crash.
    # ═══════════════════════════════════════════════════════════════════════

    @staticmethod
    def _is_vtk_widget_alive(vtk_widget):
        """Check if a VTK widget is still alive and renderable."""
        if vtk_widget is None:
            return False
        try:
            # Qt widget destroyed?
            if hasattr(vtk_widget, 'isVisible') and callable(vtk_widget.isVisible):
                if not vtk_widget.isVisible():
                    return False
            # Render window accessible?
            rw = vtk_widget.GetRenderWindow()
            if rw is None:
                return False
            # Interactor alive? (None if window finalized)
            if rw.GetInteractor() is None:
                return False
            # Has at least one renderer?
            renderers = rw.GetRenderers()
            if renderers is None or renderers.GetNumberOfItems() == 0:
                return False
            return True
        except (RuntimeError, AttributeError, OSError, ReferenceError):
            return False
        except Exception:
            return False

    @staticmethod
    def _safe_render(vtk_widget):
        """
        Safely render a VTK widget. Returns True on success, False otherwise.
        NEVER raises — all exceptions are caught to prevent crash.
        """
        if vtk_widget is None:
            return False
        try:
            # Inline validity checks (avoid second method call for speed)
            rw = vtk_widget.GetRenderWindow()
            if rw is None:
                return False
            if rw.GetInteractor() is None:
                return False
            renderers = rw.GetRenderers()
            if renderers is None or renderers.GetNumberOfItems() == 0:
                return False
            rw.Render()
            return True
        except (RuntimeError, AttributeError, OSError, ReferenceError):
            return False
        except Exception:
            return False

    @staticmethod
    def _safe_render_pyvista(vtk_widget):
        """Safely call .render() on a PyVista widget."""
        if vtk_widget is None:
            return False
        try:
            if hasattr(vtk_widget, 'isVisible') and callable(vtk_widget.isVisible):
                if not vtk_widget.isVisible():
                    return False
            if hasattr(vtk_widget, "testAttribute"):
                try:
                    if not vtk_widget.testAttribute(Qt.WA_WState_Created):
                        return False
                except Exception:
                    pass
            if hasattr(vtk_widget, "windowHandle"):
                try:
                    win = vtk_widget.windowHandle()
                    # Child widgets may legitimately return None here; don't
                    # block rendering in that case.
                    if win is not None and hasattr(win, "isExposed") and not win.isExposed():
                        return False
                except Exception:
                    pass
            rw = vtk_widget.GetRenderWindow()
            if rw is None or rw.GetInteractor() is None:
                return False
            vtk_widget.render()
            return True
        except (RuntimeError, AttributeError, OSError, ReferenceError):
            return False
        except Exception:
            return False

    def _safe_render_interactor(self):
        """Safely render via self.interactor's render window."""
        try:
            if self.interactor is None:
                return False
            rw = self.interactor.GetRenderWindow()
            if rw is None or rw.GetInteractor() is None:
                return False
            renderers = rw.GetRenderers()
            if renderers is None or renderers.GetNumberOfItems() == 0:
                return False
            rw.Render()
            return True
        except (RuntimeError, AttributeError, OSError, ReferenceError):
            return False
        except Exception:
            return False

    @staticmethod
    def _safe_render_from_renderer(renderer):
        """Safely render from a VTK renderer reference."""
        if renderer is None:
            return False
        try:
            rw = renderer.GetRenderWindow()
            if rw is None or rw.GetInteractor() is None:
                return False
            rw.Render()
            return True
        except (RuntimeError, AttributeError, OSError, ReferenceError):
            return False
        except Exception:
            return False

    @staticmethod
    def _safe_get_render_window(vtk_widget):
        """Safely get render window from a VTK widget. Returns None if dead."""
        if vtk_widget is None:
            return None
        try:
            rw = vtk_widget.GetRenderWindow()
            if rw is None or rw.GetInteractor() is None:
                return None
            return rw
        except (RuntimeError, AttributeError, OSError, ReferenceError):
            return None
        except Exception:
            return None

    # ═══════════════════════════════════════════════════════════════════════
    # ✅ CUSTOM SAFE PAN - Replaces VTK's built-in pan which crashes
    # VTK's OnMiddleButtonDown/Up → OnMouseMove internally calls C++ Render()
    # which segfaults on stale OpenGL contexts. This custom pan does camera
    # math in Python and uses Qt deferred repaint — zero C++ Render() calls.
    # ═══════════════════════════════════════════════════════════════════════

    def _on_scroll_zoom_in(self, obj, event):
        """Scroll-wheel zoom in — replaces VTK's Image2D 'Slice' behaviour."""
        try:
            if hasattr(obj, "AbortFlagOn"):
                obj.AbortFlagOn()
            elif hasattr(obj, "SetAbortFlag"):
                obj.SetAbortFlag(1)
        except Exception:
            pass
        self._apply_scroll_zoom(1.1)

    def _on_scroll_zoom_out(self, obj, event):
        """Scroll-wheel zoom out — replaces VTK's Image2D 'Slice' behaviour."""
        try:
            if hasattr(obj, "AbortFlagOn"):
                obj.AbortFlagOn()
            elif hasattr(obj, "SetAbortFlag"):
                obj.SetAbortFlag(1)
        except Exception:
            pass
        self._apply_scroll_zoom(1.0 / 1.1)

    def _apply_scroll_zoom(self, factor):
        """
        Scale the camera's ParallelScale (orthographic zoom) by `factor`.
        Works for any active cross-section / cut-section widget without touching
        camera orientation, so the 2D lock is never disturbed.
        """
        try:
            vtk_widget = self._get_active_vtk_widget()
            if vtk_widget is None:
                return
            renderer = getattr(vtk_widget, "renderer", None)
            if renderer is None:
                return
            camera = renderer.GetActiveCamera()
            if camera is None:
                return

            if camera.GetParallelProjection():
                camera.SetParallelScale(camera.GetParallelScale() / factor)
            else:
                camera.Dolly(factor)
                renderer.ResetCameraClippingRange()

            # Deferred render — same pattern as pan to avoid C++ Render() calls.
            if not getattr(self, "_pan_render_pending", False):
                self._pan_render_pending = True
                from PySide6.QtCore import QTimer
                QTimer.singleShot(0, self._execute_deferred_pan_render)
        except (RuntimeError, AttributeError, OSError, ReferenceError):
            pass
        except Exception:
            pass

    def _on_safe_pan_start(self, obj, event):
        """Start custom pan (replaces style.OnMiddleButtonDown)."""
        try:
            if hasattr(obj, "AbortFlagOn"):
                obj.AbortFlagOn()
            elif hasattr(obj, "SetAbortFlag"):
                obj.SetAbortFlag(1)
        except Exception:
            pass
        try:
            if self.interactor is None:
                return
            
            self._is_panning = True
            self._last_pan_pos = self.interactor.GetEventPosition()

            # Interactive-mode rendering: skip expensive still-render steps
            try:
                rw = self.interactor.GetRenderWindow()
                if rw is not None:
                    rw.SetDesiredUpdateRate(30.0)
            except Exception:
                pass

            tool = getattr(self.app, "active_classify_tool", None)
            has_preview_gesture = bool(
                self._has_active_drag_classification(tool)
                or getattr(self, "P1", None) is not None
                or bool(getattr(self, "drawing_points", []))
                or bool(getattr(self, "is_drawing_freehand", False))
            )
            if tool in {"rectangle", "circle", "freehand", "above_line", "below_line", "parallel_line", "polygon", "brush"} and has_preview_gesture:
                # Keep the live preview actors active and translate them with the pan,
                # otherwise the preview appears frozen while the cloud moves.
                self._hide_preview_overlay_for_pan()
                self._start_deferred_left_release_watch()
            
        except (RuntimeError, AttributeError):
            pass

    def _on_safe_pan_stop(self, obj, event):
        """Stop custom pan (replaces style.OnMiddleButtonUp)."""
        try:
            if hasattr(obj, "AbortFlagOn"):
                obj.AbortFlagOn()
            elif hasattr(obj, "SetAbortFlag"):
                obj.SetAbortFlag(1)
        except Exception:
            pass
        self._is_panning = False
        # Let the next post-pan mouse move update the preview immediately.
        self._last_mouse_move_time = 0.0
        self._last_render_time = 0.0
        self._hide_preview_overlay_for_pan()
        try:
            self._start_deferred_left_release_watch()
        except (AttributeError, RuntimeError):
            return

        # Restore still-render quality and force one full-quality frame
        try:
            rw = self.interactor.GetRenderWindow() if self.interactor else None
            if rw is not None:
                rw.SetDesiredUpdateRate(0.001)
                rw.Render()
        except Exception:
            pass

    def _is_classification_paused_for_pan(self, tool=None):
        """
        Return True while a drag-style classification gesture should be frozen
        so middle-button pan can run without changing the active selection.
        """
        if not getattr(self, "_is_panning", False):
            return False
        return self._has_active_drag_classification(tool)

    def _do_safe_pan(self):
        """
        Execute pan from the VTK middle-button press/release state.

        Qt's global mouse-button state is intentionally not consulted here:
        QVTK delivery can clear it before this callback even though the
        physical middle-button gesture is still active.
        """
        if not getattr(self, '_is_panning', False):
            return
        
        try:
            if self.interactor is None:
                return
            
            current_pos = self.interactor.GetEventPosition()
            last = getattr(self, '_last_pan_pos', current_pos)
            
            dx = current_pos[0] - last[0]
            dy = current_pos[1] - last[1]
            
            if dx == 0 and dy == 0:
                return
            
            vtk_widget = self._get_active_vtk_widget()
            if vtk_widget is None:
                return
            
            renderer = getattr(vtk_widget, 'renderer', None)
            if renderer is None:
                return
            
            camera = renderer.GetActiveCamera()
            if camera is None:
                return
            
            # Get window size for pixel-to-world conversion
            try:
                rw = vtk_widget.GetRenderWindow()
                size = rw.GetSize() if rw is not None else (800, 600)
            except (RuntimeError, AttributeError):
                size = (800, 600)
            
            w = max(size[0], 1)
            h = max(size[1], 1)
            
            # Use ParallelScale for orthographic views
            if camera.GetParallelProjection():
                parallel_scale = camera.GetParallelScale()
                scale_x = (2.0 * parallel_scale) / h
                scale_y = scale_x
            else:
                distance = camera.GetDistance()
                scale_x = distance / (w * 0.5)
                scale_y = scale_x
            
            # Camera axes
            camera.OrthogonalizeViewUp()
            up = list(camera.GetViewUp())
            pos = list(camera.GetPosition())
            fp = list(camera.GetFocalPoint())
            
            vd = [fp[i] - pos[i] for i in range(3)]
            
            # Right vector = vd × up
            right = [
                vd[1] * up[2] - vd[2] * up[1],
                vd[2] * up[0] - vd[0] * up[2],
                vd[0] * up[1] - vd[1] * up[0]
            ]
            mag = (right[0]**2 + right[1]**2 + right[2]**2) ** 0.5
            if mag < 1e-12:
                self._last_pan_pos = current_pos
                return
            right = [r / mag for r in right]
            
            # Pan vector
            pan = [-dx * scale_x * right[i] - dy * scale_y * up[i] for i in range(3)]
            
            camera.SetPosition(*[pos[i] + pan[i] for i in range(3)])
            camera.SetFocalPoint(*[fp[i] + pan[i] for i in range(3)])
            
            self._last_pan_pos = current_pos

            tool = getattr(self, "_gesture_tool", None) or getattr(self.app, "active_classify_tool", None)
            if self._has_active_drag_classification(tool):
                if tool == "freehand" and hasattr(self, "drawing_points_display_cut"):
                    self.drawing_points_display_cut = [
                        (float(px) + dx, float(py) + dy)
                        for px, py in self.drawing_points_display_cut
                    ]
                elif tool == "rectangle" and getattr(self, "P1_display_cut", None) is not None:
                    self.P1_display_cut = (
                        float(self.P1_display_cut[0]) + dx,
                        float(self.P1_display_cut[1]) + dy,
                    )

                self._translate_preview_actors_for_pan(
                    dx,
                    dy,
                    self._get_active_tool_preview_actor_names(tool),
                )
            
            # Deferred render
            if not getattr(self, '_pan_render_pending', False):
                self._pan_render_pending = True
                from PySide6.QtCore import QTimer
                QTimer.singleShot(0, self._execute_deferred_pan_render)
            
        except (RuntimeError, AttributeError, OSError, ReferenceError):
            self._is_panning = False
        except Exception:
            pass

    def _execute_deferred_pan_render(self):
        """Render on the Qt event loop (valid OpenGL context). Called by QTimer."""
        self._pan_render_pending = False
        try:
            import time as _time
            now = _time.perf_counter()
            # Cap interactive pan renders to 60 fps (16 ms). If the last render
            # finished too recently, re-schedule rather than saturating the GPU.
            if now - getattr(self, '_last_pan_render_time', 0.0) < 0.016:
                if not getattr(self, '_pan_render_pending', False):
                    self._pan_render_pending = True
                    QTimer.singleShot(8, self._execute_deferred_pan_render)
                return
            self._last_pan_render_time = now

            vtk_widget = self._get_active_vtk_widget()
            if vtk_widget is not None:
                # ✅ Check widget is visible (Qt-level, safe)
                if hasattr(vtk_widget, 'isVisible') and not vtk_widget.isVisible():
                    return
                vtk_widget.render()
                overlay = getattr(self, "_preview_overlay", None)
                if overlay is not None and getattr(self, "_preview_hidden_for_pan", False):
                    try:
                        self._update_overlay_geometry()
                        overlay.show()
                        overlay.raise_()
                        overlay.update()
                    except Exception:
                        pass
        except (RuntimeError, AttributeError, OSError, ReferenceError):
            pass
        except Exception:
            pass

    # Added by bala for mouse smooth
    def _get_display_point(self):
        """Exact mouse position in DISPLAY coords (no snapping, very fast)."""
        x, y = self.interactor.GetEventPosition()
        return float(x), float(y)

    def _display_to_world_fast(self, x, y):
        """DISPLAY → WORLD without picker (no snapping). ✅ SAFE: guarded render window access."""
        if not hasattr(self, "_cached_renderer") or self._cached_renderer is None:
            try:
                rw = self.interactor.GetRenderWindow()
                if rw is None:
                    return (0.0, 0.0, 0.0)
                renderers = rw.GetRenderers()
                if renderers is None:
                    return (0.0, 0.0, 0.0)
                self._cached_renderer = renderers.GetFirstRenderer()
            except (RuntimeError, AttributeError, OSError, ReferenceError):
                return (0.0, 0.0, 0.0)
            except Exception:
                return (0.0, 0.0, 0.0)

        if self._cached_renderer is None:
            return (0.0, 0.0, 0.0)

        # Reuse the VTK coordinate object. Main-view freehand now captures
        # native mouse samples before the preview-render throttle, so creating
        # a SWIG object per MouseMove would add avoidable interaction jitter.
        coord = getattr(self, "_display_to_world_coord", None)
        if coord is None:
            coord = vtk.vtkCoordinate()
            coord.SetCoordinateSystemToDisplay()
            self._display_to_world_coord = coord
        coord.SetValue(float(x), float(y), 0.0)
        try:
            return coord.GetComputedWorldValue(self._cached_renderer)
        except Exception:
            return (0.0, 0.0, 0.0)

    def _is_main_view(self):
        """True if this interactor is attached to the main plan view widget."""
        try:
            return self._get_active_vtk_widget() == getattr(self.app, 'vtk_widget', None)
        except Exception:
            return False

    def _view2d_to_world(self, u, v):
        """Map 2D tool coords to 3D world - OPTIMIZED with caching"""
        
        if self._is_main_view():
            return (float(u), float(v), 0.0)
        
        view_mode = getattr(self.app, 'cross_view_mode', 'side')
        
        # ✅ CRITICAL OPTIMIZATION: Cache perpendicular coordinate
        # Only recalculate when view changes, not on every mouse move
        cache_key = f"perp_coord_{view_mode}"
        
        if not hasattr(self, '_coord_cache'):
            self._coord_cache = {}
        
        if cache_key not in self._coord_cache:
            # Calculate once and cache
            active_view = self._get_view_index_from_interactor()
            perpendicular_coord = 0.0
            
            if active_view is not None:
                core_mask = getattr(self.app, f"section_{active_view}_core_mask", None)
                if core_mask is not None and core_mask.sum() > 0:
                    section_xyz = self.app.data["xyz"][core_mask]
                    
                    if view_mode == 'front':
                        perpendicular_coord = float(np.median(section_xyz[:, 0]))
                    else:
                        perpendicular_coord = float(np.median(section_xyz[:, 1]))
            
            self._coord_cache[cache_key] = perpendicular_coord
        
        perpendicular_coord = self._coord_cache[cache_key]
        
        if view_mode == 'front':
            return (float(perpendicular_coord), float(u), float(v))
        else:
            return (float(u), float(perpendicular_coord), float(v)) 
        
    def _invalidate_coord_cache(self):
        """Clear coordinate cache when section changes"""
        if hasattr(self, '_coord_cache'):
            self._coord_cache.clear()
        
    def _get_view_coordinates(self, pt):
        """
        2D coordinates in the active view's plane.
        - Main view: (x, y)
        - Cross-section: (u, z) [side: XZ, front: YZ]
        - Cut section: (u, z) [projected to cut plane]
        """
        if self._is_main_view():
            return (pt[0], pt[1])
        
        # ✅ FIX: Check if we're in cut section
        vtk_widget = self._get_active_vtk_widget()
        is_cut_section = (
            hasattr(self.app, 'cut_section_controller') and
            vtk_widget == getattr(self.app.cut_section_controller, 'cut_vtk', None)
        )
        
        if is_cut_section:
            # ✅ CRITICAL FIX: Project to cut section coordinates
            from ..classification_tools import _project_to_cut_view
            pt_array = np.array([pt])
            projected = _project_to_cut_view(self.app, pt_array)
            return (projected[0, 0], projected[0, 1])  # Return (u, z)
        
        # Regular cross-section
        view_mode = getattr(self.app, 'cross_view_mode', 'side')
        if view_mode == 'front':
            return (pt[1], pt[2])  # Y-Z
        else:
            return (pt[0], pt[2])  # X-Z
 
    def _get_active_vtk_widget(self):
        """
        Get the correct VTK widget by matching the current interactor.
        ✅ SAFE: All GetRenderWindow() calls guarded to prevent crash on stale windows.
        """
        if self.interactor is None:
            return None
       
        # 1. Check Cut Section
        if hasattr(self.app, 'cut_section_controller'):
            cut_vtk = getattr(self.app.cut_section_controller, 'cut_vtk', None)
            if cut_vtk:
                if getattr(cut_vtk, 'interactor', None) == self.interactor:
                    return cut_vtk
                rw = self._safe_get_render_window(cut_vtk)
                if rw is not None:
                    try:
                        if rw.GetInteractor() == self.interactor:
                            return cut_vtk
                    except Exception:
                        pass
 
        # 2. Check Section Docks (SAFE)
        section_vtks = getattr(self.app, 'section_vtks', None)
        if isinstance(section_vtks, dict):
            for widget in section_vtks.values():
                if widget:
                    if getattr(widget, 'interactor', None) == self.interactor:
                        return widget
                    rw = self._safe_get_render_window(widget)
                    if rw is not None:
                        try:
                            if rw.GetInteractor() == self.interactor:
                                return widget
                        except Exception:
                            pass

 
        # 3. Check Main View
        main_widget = getattr(self.app, 'vtk_widget', None)
        if main_widget:
            if getattr(main_widget, 'interactor', None) == self.interactor:
                return main_widget
            rw = self._safe_get_render_window(main_widget)
            if rw is not None:
                try:
                    if rw.GetInteractor() == self.interactor:
                        return main_widget
                except Exception:
                    pass
 
        # 4. FALLBACK: Match by RenderWindow
        try:
            current_rw = self.interactor.GetRenderWindow()
            if current_rw is None:
                return None
           
            if hasattr(self.app, 'cut_section_controller'):
                cut_vtk = getattr(self.app.cut_section_controller, 'cut_vtk', None)
                if cut_vtk:
                    rw = self._safe_get_render_window(cut_vtk)
                    if rw is not None and rw == current_rw:
                        return cut_vtk
                   
            if hasattr(self.app, 'section_vtks'):
                for widget in self.app.section_vtks.values():
                    if widget:
                        rw = self._safe_get_render_window(widget)
                        if rw is not None and rw == current_rw:
                            return widget
        except Exception:
            pass                  
        return None
   
    def _get_view_index_from_interactor(self):
        """Return cross-section view index (0..3) for this interactor, or None."""
        try:
            if hasattr(self.app, "section_vtks"):
                for idx, vtk_widget in self.app.section_vtks.items():
                    if getattr(vtk_widget, "interactor", None) == self.interactor:
                        return idx
                    rw = vtk_widget.GetRenderWindow() if hasattr(vtk_widget, "GetRenderWindow") else None
                    if rw and rw.GetInteractor() == self.interactor:
                        return idx
        except Exception:
            pass
        return None
 
    def _get_section_context_for_view(self, view_idx):
        """
        Build (mask, section_points) for the requested view index from app's stored per-view data.
        
        Returns:
            mask: boolean array over full dataset (includes core + buffer points)
            section_points: transformed 2D coordinates for the section view OR 3D xyz[mask]
        
        ✅ UPDATED LOGIC:
            1. First tries to use pre-computed combined_mask + transformed points (fastest)
            2. Falls back to building combined mask from core + buffer
            3. Always prefers transformed points over raw xyz
            4. Provides detailed debug logging
        
        The transformed points are in the 2D coordinate system of the cross-section view:
            - For side view: (x, z) coordinates
            - For front view: (y, z) coordinates
        """
        try:
            app = self.app
            
            # ═══════════════════════════════════════════════════════════
            # PRIORITY PATH 1: Use pre-computed combined_mask + transformed points
            # ═══════════════════════════════════════════════════════════
            # This is the fastest and most accurate path when available
            combined_mask = getattr(app, f"section_{view_idx}_combined_mask", None)
            section_points_transformed = getattr(app, f"section_{view_idx}_points_transformed", None)
            
            if combined_mask is not None and section_points_transformed is not None:
                return combined_mask, section_points_transformed

            core_mask   = getattr(app, f"section_{view_idx}_core_mask",   None)
            buffer_mask = getattr(app, f"section_{view_idx}_buffer_mask", None)

            if core_mask is None and buffer_mask is None:
                return None, None

            if core_mask is None:
                combined_mask = buffer_mask
            elif buffer_mask is None:
                combined_mask = core_mask
            else:
                combined_mask = core_mask | buffer_mask

            section_points_transformed = getattr(app, f"section_{view_idx}_points_transformed", None)
            if section_points_transformed is not None:
                return combined_mask, section_points_transformed

            section_points = app.data["xyz"][combined_mask]
            return combined_mask, section_points

        except Exception as e:
            import traceback
            traceback.print_exc()
            return None, None
    
    def _get_view_index_from_interactor(self):
        """Return cross-section view index (0..3) for this interactor, or None."""
        try:
            if hasattr(self.app, "section_vtks"):
                for idx, vtk_widget in self.app.section_vtks.items():
                    if getattr(vtk_widget, "interactor", None) == self.interactor:
                        return idx
                    rw = vtk_widget.GetRenderWindow() if hasattr(vtk_widget, "GetRenderWindow") else None
                    if rw and rw.GetInteractor() == self.interactor:
                        return idx
        except Exception:
            pass
        return None

    def _navigate_main_to_section_click(self, display_x, display_y):
        """
        MicroStation-style locate: click in section view while cross-section tool is
        active → pan main view to the corresponding world XY AND start a rubber-band
        preview line from that locked point.

        Section local coords: X = along_c (distance along line from P1), Z = elevation.
        Camera looks along Y (across_c), so _display_to_world_fast gives (along, ~0, Z).
        Inverse: world_xy = sectionP1[:2] + along * dir_vec
        """
        try:
            import numpy as np

            v_idx = self._get_view_index_from_interactor()
            if v_idx is None:
                sc = getattr(self.app, 'section_controller', None)
                v_idx = getattr(sc, 'active_view', 0) if sc else 0

            sP1 = getattr(self.app, f'section_{v_idx}_P1', None)
            sP2 = getattr(self.app, f'section_{v_idx}_P2', None)
            if sP1 is None or sP2 is None:
                return

            # Display → section-local (along, across≈0, Z)
            local_pt = self._display_to_world_fast(display_x, display_y)
            along = float(local_pt[0])

            # Inverse transform → real world XY
            v = np.asarray(sP2[:2], dtype=np.float64) - np.asarray(sP1[:2], dtype=np.float64)
            length = float(np.linalg.norm(v))
            if length < 1e-9:
                return
            dir_vec = v / length
            world_x = float(sP1[0]) + along * dir_vec[0]
            world_y = float(sP1[1]) + along * dir_vec[1]

            # ── Store locate state for rubber-band drawing in section view ──
            self.app._section_locate_display = (float(display_x), float(display_y))
            self.app._section_locate_view = v_idx

            # ── Arm cross_interactor: set P1 so main-view preview starts from here ──
            cross = getattr(self.app, 'cross_interactor', None)
            if cross is not None:
                main_vtk = getattr(self.app, 'vtk_widget', None)
                if main_vtk is not None:
                    # Use current main camera focal Z so the section line stays flat
                    focal_z = main_vtk.renderer.GetActiveCamera().GetFocalPoint()[2]
                    cross.P1 = np.array([world_x, world_y, focal_z], dtype=np.float64)
                    cross.slice_state = 1

            # ── Pan main view camera to (world_x, world_y) ──
            main_vtk = getattr(self.app, 'vtk_widget', None)
            if main_vtk is not None:
                cam = main_vtk.renderer.GetActiveCamera()
                focal = cam.GetFocalPoint()
                pos = cam.GetPosition()
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
        except Exception as e:
            print(f"⚠️ _navigate_main_to_section_click: {e}")

    def _draw_locate_line_in_section(self, vtk_widget, from_disp, to_disp):
        """
        Draw a rubber-band preview line in the section view from the locked click
        position to the current cursor position (both in VTK display coords).
        Uses the same color as the cross-section preview line (cross_line_color).
        """
        if vtk_widget is None:
            return
        ren = vtk_widget.renderer

        # Lazy-init the dedicated 2D actor (separate from classification line_actor)
        if not hasattr(self, '_locate_line_actor') or self._locate_line_actor is None:
            pts = vtk.vtkPoints()
            pts.SetNumberOfPoints(2)
            lines = vtk.vtkCellArray()
            lines.InsertNextCell(2)
            lines.InsertCellPoint(0)
            lines.InsertCellPoint(1)
            poly = vtk.vtkPolyData()
            poly.SetPoints(pts)
            poly.SetLines(lines)

            coord = vtk.vtkCoordinate()
            coord.SetCoordinateSystemToDisplay()
            mapper = vtk.vtkPolyDataMapper2D()
            mapper.SetInputData(poly)
            mapper.SetTransformCoordinate(coord)

            actor = vtk.vtkActor2D()
            actor.SetMapper(mapper)

            self._locate_line_pts = pts
            self._locate_line_poly = poly
            self._locate_line_actor = actor

        actor = self._locate_line_actor
        if not ren.HasViewProp(actor):
            ren.AddActor2D(actor)
        actor.VisibilityOn()

        color = getattr(self.app, 'cross_line_color', (1.0, 0.0, 1.0))
        width = getattr(self.app, 'cross_line_width', 2)
        prop = actor.GetProperty()
        prop.SetColor(*color)
        prop.SetLineWidth(max(2, width))
        prop.SetOpacity(1.0)

        self._locate_line_pts.SetPoint(0, from_disp[0], from_disp[1], 0.0)
        self._locate_line_pts.SetPoint(1, to_disp[0], to_disp[1], 0.0)
        self._locate_line_pts.Modified()
        self._locate_line_poly.Modified()

    def _clear_locate_state(self):
        """Remove the section-view rubber-band actor and clear the locate state."""
        try:
            actor = getattr(self, '_locate_line_actor', None)
            if actor is not None:
                actor.VisibilityOff()
                # Remove from ALL section view renderers to prevent ghost lines
                for vtk_widget in getattr(self.app, 'section_vtks', {}).values():
                    try:
                        if hasattr(vtk_widget, 'renderer') and vtk_widget.renderer.HasViewProp(actor):
                            vtk_widget.renderer.RemoveActor2D(actor)
                    except Exception:
                        pass
                # Also remove from cut dock if present
                try:
                    cut_vtk = getattr(getattr(self.app, 'cut_section_controller', None), 'cut_vtk', None)
                    if cut_vtk is not None and hasattr(cut_vtk, 'renderer') and cut_vtk.renderer.HasViewProp(actor):
                        cut_vtk.renderer.RemoveActor2D(actor)
                except Exception:
                    pass
        except Exception:
            pass
        self.app._section_locate_display = None
        self.app._section_locate_view = None

    #     """
    #     mask: boolean array over full dataset
    #     section_points: xyz[mask]
    #     """




    def _init_preview_overlay(self):
        """Initialize Qt-based preview overlay"""
        try:
            # Get the parent widget for the overlay
            parent_widget = self.interactor
            
            # Create overlay
            self._preview_overlay = PreviewOverlay(parent_widget)
            self._preview_overlay.setGeometry(parent_widget.rect())
            self._preview_overlay.show()
            self._preview_overlay.raise_()
            
            # Cache renderer and widget height for coordinate conversion
            self._cached_renderer = self.interactor.GetRenderWindow().GetRenderers().GetFirstRenderer()
            self._widget_height = parent_widget.height()
            
            print("✅ Qt preview overlay initialized")
            
        except Exception as e:
            print(f"⚠️ Failed to create Qt preview overlay: {e}")
            self._preview_overlay = None
    
    def _update_overlay_geometry(self):
        """Update overlay size to match interactor widget"""
        if self._preview_overlay is None:
            return
            
        try:
            self._preview_overlay.setGeometry(self.interactor.rect())
            self._widget_height = self.interactor.height()
        except Exception:
            pass
    
    def _should_update(self):
        """Throttle updates to maintain smooth performance"""
        current_time = time.time() * 1000  # Convert to milliseconds
        self._update_interval = self._adaptive_preview_interval_ms()
        if current_time - self._last_update_time >= self._update_interval:
            self._last_update_time = current_time
            return True
        return False

    def _adaptive_preview_interval_ms(self) -> int:
        """
        Adaptive preview cadence:
        - default ~60 FPS (16 ms)
        - medium pressure ~45 FPS (22 ms)
        - high pressure ~30 FPS (33 ms)

        Note: this is the conservative main-view cap. For cross-section / cut
        docks, _preview_interval_sec() uses a fixed 60 FPS (16 ms) regardless
        of the global point count, because those docks render at most ~100 K
        points and can sustain 60 FPS comfortably. See _preview_interval_sec.
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

    def _preview_interval_sec(self) -> float:
        """
        Return preview-frame throttle interval in seconds.

        ⚡ View-aware: when the user is dragging inside a cross-section / cut
        dock, the render only touches a small section actor (≤ ~100 K pts),
        not the 39 M-point main view. Capping at 30 FPS in that case was the
        root cause of "preview feels heavy" — visible cursor lag during a
        line/rect/circle drag. We force 60 FPS (16 ms) for section/cut docks
        and keep the adaptive cap only for the main view.
        """
        try:
            vtk_widget = self._get_active_vtk_widget()
            main = getattr(self.app, 'vtk_widget', None)
            if vtk_widget is not None and vtk_widget is not main:
                # Section / cut dock — bounded geometry, 60 FPS is safe.
                return 0.016
        except Exception:
            pass
        return float(self._adaptive_preview_interval_ms()) / 1000.0
    
    def _world_to_screen(self, world_point):
        """Convert world coordinates to screen coordinates - OPTIMIZED"""
        try:
            if self._cached_renderer is None:
                self._cached_renderer = self.interactor.GetRenderWindow().GetRenderers().GetFirstRenderer()
            
            # Create coordinate converter
            coord = vtk.vtkCoordinate()
            coord.SetCoordinateSystemToWorld()
            
            # Handle both 2D (u, z) and 3D (x, y, z) coordinates
            if len(world_point) == 2:
                coord.SetValue(world_point[0], 0, world_point[1])
            else:
                coord.SetValue(world_point[0], world_point[1], world_point[2])
            
            # Get display coordinates
            display = coord.GetComputedDisplayValue(self._cached_renderer)
            
            # VTK uses bottom-left origin, Qt uses top-left
            screen_x = int(display[0])
            screen_y = int(self._widget_height - display[1])
            
            return QPoint(screen_x, screen_y)
            
        except Exception as e:
            return QPoint(0, 0)
    
    def _world_radius_to_screen(self, center_world, radius_world):
        """Convert world radius to screen pixels"""
        try:
            # Get screen position of center
            center_screen = self._world_to_screen(center_world)
            
            # Get screen position of a point at radius distance
            edge_world = (center_world[0] + radius_world, center_world[1])
            edge_screen = self._world_to_screen(edge_world)
            
            # Calculate pixel radius
            pixel_radius = abs(edge_screen.x() - center_screen.x())
            return max(5, pixel_radius)  # Minimum 5 pixels
            
        except Exception:
            return 20  # Default radius

    def _display_to_world_same_depth(self, vtk_widget, center_world, dx_px=0.0, dy_px=0.0):
        """
        Convert a display offset (dx_px, dy_px) around center_world into a world point
        at the SAME display depth. This converts pixel size to world size (zoom-aware).
        """
        renderer = vtk_widget.renderer
        cxw, cyw, czw = float(center_world[0]), float(center_world[1]), float(center_world[2])

        # World -> Display to get depth (z in display space)
        renderer.SetWorldPoint(cxw, cyw, czw, 1.0)
        renderer.WorldToDisplay()
        cx, cy, cz = renderer.GetDisplayPoint()

        # Display -> World at same depth
        renderer.SetDisplayPoint(float(cx + dx_px), float(cy + dy_px), float(cz))
        renderer.DisplayToWorld()
        wx, wy, wz, w = renderer.GetWorldPoint()
        if abs(w) < 1e-12:
            return (wx, wy, wz)
        return (wx / w, wy / w, wz / w)

    def _pixel_radius_to_view_radius(self, vtk_widget, center_world, radius_px):
        """
        Convert pixel radius (screen space) into radius in tool coords:
        - Main view: (x,y)
        - Cross-section: (u,z)
        - Cut-section: projected (u,z)
        """
        try:
            center_world = (float(center_world[0]), float(center_world[1]), float(center_world[2]))
            edge_world = self._display_to_world_same_depth(vtk_widget, center_world, dx_px=float(radius_px), dy_px=0.0)

            u0, v0 = self._get_view_coordinates(center_world)
            u1, v1 = self._get_view_coordinates(edge_world)

            r = float(np.hypot(u1 - u0, v1 - v0))
            if (not np.isfinite(r)) or (r <= 1e-12):
                return float(getattr(self.app, "brush_radius", 1.0))
            return r
        except Exception:
            # fallback to old behavior if something goes wrong
            return float(getattr(self.app, "brush_radius", 1.0))

    def get_visible_class_filter(self, section_mask):
        """
        Restrict section_mask to ONLY those points whose class is visible
        in the ACTIVE view's Display Mode.
        
        ✅ FIXED: Properly detects cut section and uses slot 5 palette
        ✅ Works for: Main View, Cross-Section Views (1-4), Cut Section View
        
        section_mask: boolean mask of points in the current section
                    (same length as the full dataset)
        returns: boolean mask, same shape, with invisible classes removed.
        
        View mapping:
        - Slot 0: Main View
        - Slot 1-4: Cross-Section Views 1-4
        - Slot 5: Cut Section View (NEW!)
        """
        
        # ═══════════════════════════════════════════════════════════════════════
        # STEP 1: Safety checks
        # ═══════════════════════════════════════════════════════════════════════
        
        if section_mask is None:
            return None
        
        # Need classification array
        if not hasattr(self.app, "data") or "classification" not in self.app.data:
            print("⚠️ No classification data – skipping visibility filter")
            return section_mask
        
        # ═══════════════════════════════════════════════════════════════════════
        # STEP 2: Determine which view is active (main, cross-section, or cut)
        # ═══════════════════════════════════════════════════════════════════════
        
        dialog = getattr(self.app, "display_mode_dialog", None)
        
        if dialog is None:
            print("ℹ️ No DisplayModeDialog – using section_mask only (no protection)")
            return section_mask
        
        # ✅ PRIORITY 1: Check if we're in CUT SECTION (highest priority)
        is_cut_section = False
        slot_idx = None
        view_context = None
        
        # Method 1: Check active_classify_target flag
        if getattr(self.app, "active_classify_target", None) == "cut":
            is_cut_section = True
        
        # Method 2: Check by comparing interactor's widget with cut_vtk
        if not is_cut_section and hasattr(self.app, 'cut_section_controller'):
            try:
                active_widget = self._get_active_vtk_widget()
                cut_vtk = getattr(self.app.cut_section_controller, 'cut_vtk', None)
                
                if active_widget is not None and cut_vtk is not None:
                    if active_widget == cut_vtk:
                        is_cut_section = True
                        print("✅ Detected: In CUT SECTION (by widget match)")
            except Exception as e:
                print(f"⚠️ Widget detection failed: {e}")
        
        # ✅ Cut section handling
        if is_cut_section:
            slot_idx = 5  # ✅ CUT SECTION always uses slot 5
            view_context = "Cut Section View"
            print(f"📍 Visibility filter for {view_context} (slot {slot_idx})")
        
        # ✅ PRIORITY 2: Check if we're in CROSS-SECTION
        else:
            active_view = getattr(self.app.section_controller, "active_view", None)
            
            if active_view is not None and 0 <= active_view <= 3:
                slot_idx = active_view + 1  # Convert 0-3 to 1-4
                view_context = f"Cross-Section View {active_view + 1}"
                print(f"📍 Visibility filter for {view_context} (slot {slot_idx})")
            
            # ✅ PRIORITY 3: Fallback (shouldn't normally happen here)
            else:
                print("ℹ️ No active_view detected – using section_mask only")
                return section_mask
        
        # ═══════════════════════════════════════════════════════════════════════
        # STEP 3: Get visible classes from display_mode_dialog
        # ═══════════════════════════════════════════════════════════════════════
        
        if slot_idx is None:
            print("ℹ️ Could not determine view slot – using section_mask only")
            return section_mask
        
        try:
            # ✅ Method 1: Use dialog's dedicated method
            if hasattr(dialog, 'get_visible_classes_for_view'):
                visible_classes = set(dialog.get_visible_classes_for_view(slot_idx))
            
            # ✅ Method 2: Direct access to view_palettes
            elif hasattr(dialog, 'view_palettes') and slot_idx in dialog.view_palettes:
                palette = dialog.view_palettes[slot_idx]
                visible_classes = set([c for c, info in palette.items() if info.get('show', True)])
                print(f"   📋 Using view_palettes[{slot_idx}] directly")
            
            else:
                print(f"⚠️ Could not get visible classes for {view_context} (slot {slot_idx})")
                return section_mask
        
        except Exception as e:
            print(f"⚠️ get_visible_classes_for_view({slot_idx}) failed: {e}")
            return section_mask
        
        # ═══════════════════════════════════════════════════════════════════════
        # STEP 4: Check if any classes are visible
        # ═══════════════════════════════════════════════════════════════════════
        
        if not visible_classes:
            print(f"ℹ️ {view_context}: no visible classes selected – allowing all section points")
            return section_mask
        
        print(f"🔍 {view_context} (slot {slot_idx}) visible classes: {visible_classes}")
        
        # ═══════════════════════════════════════════════════════════════════════
        # STEP 5: Build final mask = section_mask ∧ (class ∈ visible_classes)
        # ═══════════════════════════════════════════════════════════════════════
        
        cls_all = self.app.data["classification"]
        
        # indices of points that are in this section
        section_idx = np.flatnonzero(section_mask)
        section_cls = cls_all[section_idx]
        
        # True only for section points whose class is visible in THIS view
        visible_in_section = np.isin(section_cls, list(visible_classes))
        
        # Start from all False; enable only visible section points
        filtered_mask = np.zeros_like(section_mask, dtype=bool)
        filtered_mask[section_idx[visible_in_section]] = True
        
        # ═══════════════════════════════════════════════════════════════════════
        # STEP 6: Report statistics
        # ═══════════════════════════════════════════════════════════════════════
        
        total_section = section_mask.sum()
        visible_section = filtered_mask.sum()
        filtered_out = total_section - visible_section
        
        print(f"   📊 Section statistics:")
        print(f"      Total points in section: {total_section}")
        print(f"      Visible points (will classify): {visible_section}")
        print(f"      Hidden points (PROTECTED): {filtered_out}")
        
        return filtered_mask


    # ---------------- PICK ----------------
    def _pick_world_point(self, x, y):
        """Pick 3D world coordinates from screen position."""
        picker = self.interactor.GetPicker()
        renderer = self.interactor.GetRenderWindow().GetRenderers().GetFirstRenderer()
        picker.Pick(x, y, 0, renderer)
        return picker.GetPickPosition()

    # ---------------- PREVIEWS ----------------
    def _draw_temp_line(self, P1, P2):
        """
        ✅ Senior Refactor: Handles rapid-fire classification.
        Ensures actor persistence even if the renderer is cleared between actions.
        """
        vtk_widget = self._get_active_vtk_widget()
        if not vtk_widget: return
        renderer = vtk_widget.renderer

        # 1. Pipeline Initialization & Recovery
        if not hasattr(self, "line_actor") or self.line_actor is None:
            self.line_pts = vtk.vtkPoints()
            self.line_pts.SetNumberOfPoints(2)
            lines = vtk.vtkCellArray()
            lines.InsertNextCell(2); lines.InsertCellPoint(0); lines.InsertCellPoint(1)
            self.line_poly = vtk.vtkPolyData()
            self.line_poly.SetPoints(self.line_pts); self.line_poly.SetLines(lines)
            
            mapper = vtk.vtkPolyDataMapper2D()
            mapper.SetInputData(self.line_poly)
            c = vtk.vtkCoordinate(); c.SetCoordinateSystemToDisplay()
            mapper.SetTransformCoordinate(c)

            self.line_actor = vtk.vtkActor2D()
            self.line_actor.SetMapper(mapper)
            p = self.line_actor.GetProperty()
            p.SetColor(1, 1, 0); p.SetLineWidth(2); p.SetOpacity(1.0)

        # 2. MANDATORY SYNC: Ensure actor is actually in the renderer's draw list
        if not renderer.HasViewProp(self.line_actor):
            renderer.AddActor2D(self.line_actor)
        
        self.line_actor.VisibilityOn()

        # 3. Fast Coordinate Update
        u1, v1 = self._get_view_coordinates(P1)
        u2, v2 = self._get_view_coordinates(P2)
        w1 = self._view2d_to_world(u1, v1)
        w2 = self._view2d_to_world(u2, v2)

        if not hasattr(self, "_coord_converter"):
            self._coord_converter = vtk.vtkCoordinate()
            self._coord_converter.SetCoordinateSystemToWorld()

        for i, pos in enumerate([w1, w2]):
            self._coord_converter.SetValue(*pos)
            d = self._coord_converter.GetComputedDisplayValue(renderer)
            self.line_pts.SetPoint(i, d[0], d[1], 0)

        self.line_pts.Modified()
        self.line_poly.Modified()

        # 4. Vertical Guides Sync
        tool = getattr(self.app, "active_classify_tool", None)
        if tool in ("above_line", "below_line", "parallel_line"):
            self._draw_vertical_guides(P1, P2, tool, renderer)
        # Render is batched by _on_mouse_move_with_preview — do NOT call here

    def _draw_vertical_guides(self, P1, P2, tool, renderer):
        """
        ✅ Senior Refactor: Persistent dashed guides.
        Verified for high-speed repeated interaction.
        """
        if not hasattr(self, "dotted_actor") or self.dotted_actor is None:
            self.dotted_pts = vtk.vtkPoints()
            self.dotted_lines = vtk.vtkCellArray()
            self.dotted_poly = vtk.vtkPolyData()
            self.dotted_poly.SetPoints(self.dotted_pts); self.dotted_poly.SetLines(self.dotted_lines)
            m = vtk.vtkPolyDataMapper2D(); m.SetInputData(self.dotted_poly)
            c = vtk.vtkCoordinate(); c.SetCoordinateSystemToDisplay()
            m.SetTransformCoordinate(c)
            self.dotted_actor = vtk.vtkActor2D(); self.dotted_actor.SetMapper(m)
            p = self.dotted_actor.GetProperty()
            p.SetColor(1, 1, 0); p.SetLineWidth(2); p.SetOpacity(0.9)

        # Ensure actor presence in the current renderer
        if not renderer.HasViewProp(self.dotted_actor):
            renderer.AddActor2D(self.dotted_actor)

        # Update Display values
        self._coord_converter.SetValue(*P1)
        p1 = self._coord_converter.GetComputedDisplayValue(renderer)
        self._coord_converter.SetValue(*P2)
        p2 = self._coord_converter.GetComputedDisplayValue(renderer)

        if tool == "parallel_line":
            self._draw_parallel_band_guides_display(P1, P2, renderer)
            return

        win_h = renderer.GetRenderWindow().GetSize()[1]

        self.dotted_pts.Reset(); self.dotted_lines.Reset()
        dash, gap = 8, 6
        step = dash + gap

        # ── VECTORIZED DASH GENERATION (no Python loop) ──────────────────────
        all_pts_list = []
        all_cells = []
        idx = 0
        for px, py_start in [(p1[0], p1[1]), (p2[0], p2[1])]:
            if tool == "above_line":
                y_starts = np.arange(py_start, win_h, step)
                y_ends   = np.minimum(y_starts + dash, win_h)
            else:
                y_starts = np.arange(py_start, 0, -step)
                y_ends   = np.maximum(y_starts - dash, 0.0)
            n = len(y_starts)
            if n == 0:
                continue
            col = np.full(n, float(px))
            z   = np.zeros(n)
            seg = np.column_stack([
                np.column_stack([col, y_starts, z]),
                np.column_stack([col, y_ends,   z]),
            ]).reshape(-1, 3)          # shape (2n, 3) — start/end interleaved
            all_pts_list.append(seg)
            pairs = np.arange(idx, idx + 2 * n).reshape(n, 2)
            all_cells.append(pairs)
            idx += 2 * n

        if idx > 0:
            all_pts = np.vstack(all_pts_list)
            from vtkmodules.util.numpy_support import (
                numpy_to_vtk as _n2v,
                numpy_to_vtkIdTypeArray as _n2vid,
            )
            # Deep copy avoids dangling NumPy pointers after this function returns.
            vtk_pts_data = _n2v(all_pts, deep=True)
            self.dotted_pts.SetData(vtk_pts_data)
            self.dotted_pts.Modified()

            # ⚡ Vectorized cell setup — replaces a Python for-loop that
            # was burning ~3–7 ms per preview frame (240+ SWIG calls per
            # frame at 60 FPS = 14 400 transitions per second). VTK
            # cell-array format is flat: [n, idx0, idx1, ..., idxN-1, n, ...]
            # for each cell, where n is the per-cell point count (here 2).
            all_pairs = np.vstack(all_cells).astype(np.int64, copy=False)
            n_lines = all_pairs.shape[0]
            cells_flat = np.empty(n_lines * 3, dtype=np.int64)
            cells_flat[0::3] = 2                 # 2 pts per line cell
            cells_flat[1::3] = all_pairs[:, 0]
            cells_flat[2::3] = all_pairs[:, 1]
            vtk_cells = _n2vid(cells_flat, deep=True)
            self.dotted_lines.SetCells(n_lines, vtk_cells)
            self.dotted_poly.Modified()
            self.dotted_actor.VisibilityOn()
        else:
            self.dotted_actor.VisibilityOff()

    def _get_parallel_line_tolerances(self):
        mode = str(getattr(self.app, "parallel_line_mode", "symmetric")).strip().lower()

        def _safe_width(value, default):
            try:
                v = float(value)
            except Exception:
                v = float(default)
            if not np.isfinite(v):
                v = float(default)
            # Guardrail: extreme widths can produce invalid native render coords.
            return max(0.0, min(float(v), 10000.0))

        total_width = _safe_width(getattr(self.app, "parallel_line_total_width", 0.40), 0.40)
        tol_above = _safe_width(getattr(self.app, "parallel_line_tol_above", 0.20), 0.20)
        tol_below = _safe_width(getattr(self.app, "parallel_line_tol_below", 0.20), 0.20)

        if mode == "asymmetric":
            return tol_above, tol_below
        half = total_width * 0.5
        return half, half

    def _draw_parallel_band_guides_display(self, P1, P2, renderer):
        """
        Draw two solid boundary lines parallel to the center line preview.
        """
        tol_above, tol_below = self._get_parallel_line_tolerances()
        u1, z1 = self._get_view_coordinates(P1)
        u2, z2 = self._get_view_coordinates(P2)

        dv_u = float(u2 - u1)
        dv_z = float(z2 - z1)
        length = float(np.hypot(dv_u, dv_z))
        if length <= 1e-12:
            self.dotted_actor.VisibilityOff()
            return

        d_u = dv_u / length
        d_z = dv_z / length
        n_u, n_z = -d_z, d_u
        if n_z < 0.0:
            n_u, n_z = -n_u, -n_z

        upper1 = (u1 + n_u * tol_above, z1 + n_z * tol_above)
        upper2 = (u2 + n_u * tol_above, z2 + n_z * tol_above)
        lower1 = (u1 - n_u * tol_below, z1 - n_z * tol_below)
        lower2 = (u2 - n_u * tol_below, z2 - n_z * tol_below)

        up_w1 = self._view2d_to_world(*upper1)
        up_w2 = self._view2d_to_world(*upper2)
        lo_w1 = self._view2d_to_world(*lower1)
        lo_w2 = self._view2d_to_world(*lower2)

        disp_pts = []
        for world_pt in (up_w1, up_w2, lo_w1, lo_w2):
            self._coord_converter.SetValue(*world_pt)
            d = self._coord_converter.GetComputedDisplayValue(renderer)
            dx = float(d[0])
            dy = float(d[1])
            if not (np.isfinite(dx) and np.isfinite(dy)):
                self.dotted_actor.VisibilityOff()
                return
            disp_pts.append((dx, dy, 0.0))

        self.dotted_pts.Reset()
        self.dotted_lines.Reset()
        for x, y, z in disp_pts:
            self.dotted_pts.InsertNextPoint(x, y, z)
        self.dotted_lines.InsertNextCell(2)
        self.dotted_lines.InsertCellPoint(0)
        self.dotted_lines.InsertCellPoint(1)
        self.dotted_lines.InsertNextCell(2)
        self.dotted_lines.InsertCellPoint(2)
        self.dotted_lines.InsertCellPoint(3)
        self.dotted_pts.Modified()
        self.dotted_poly.Modified()
        self.dotted_actor.VisibilityOn()

    def _draw_rectangle_preview(self, P1, P2):
        """
        ✅ Senior Refactor: Persistent Rectangle.
        Forces re-attachment to renderer to prevent disappearance during fast clicks.
        """
        vtk_widget = self._get_active_vtk_widget()
        if not vtk_widget: return
        renderer = vtk_widget.renderer

        if not hasattr(self, "rect_actor") or self.rect_actor is None:
            self.rect_pts = vtk.vtkPoints()
            self.rect_pts.SetNumberOfPoints(4)
            self.rect_lines = vtk.vtkCellArray()
            for i in range(4):
                self.rect_lines.InsertNextCell(2)
                self.rect_lines.InsertCellPoint(i); self.rect_lines.InsertCellPoint((i + 1) % 4)
            self.rect_poly = vtk.vtkPolyData()
            self.rect_poly.SetPoints(self.rect_pts); self.rect_poly.SetLines(self.rect_lines)
            m = vtk.vtkPolyDataMapper2D(); m.SetInputData(self.rect_poly)
            c = vtk.vtkCoordinate(); c.SetCoordinateSystemToDisplay()
            m.SetTransformCoordinate(c)
            self.rect_actor = vtk.vtkActor2D(); self.rect_actor.SetMapper(m)
            setattr(self.rect_actor, "_is_classification_preview", True)
            setattr(self.rect_actor, "_naksha_preview_owner", "classification")
            p = self.rect_actor.GetProperty()
            p.SetColor(1, 1, 0); p.SetLineWidth(2); p.SetOpacity(1.0)

        # Recovery Logic: If renderer was cleared, re-add actor immediately
        if not renderer.HasViewProp(self.rect_actor):
            renderer.AddActor2D(self.rect_actor)
        
        self.rect_actor.VisibilityOn()

        u1, v1 = self._get_view_coordinates(P1)
        u2, v2 = self._get_view_coordinates(P2)
        corners = [(u1, v1), (u2, v1), (u2, v2), (u1, v2)]

        if not hasattr(self, "_coord_converter"):
            self._coord_converter = vtk.vtkCoordinate()
            self._coord_converter.SetCoordinateSystemToWorld()

        for i, (u, v) in enumerate(corners):
            xw, yw, zw = self._view2d_to_world(u, v)
            self._coord_converter.SetValue(xw, yw, zw)
            d = self._coord_converter.GetComputedDisplayValue(renderer)
            self.rect_pts.SetPoint(i, d[0], d[1], 0)

        self.rect_pts.Modified()
        self.rect_poly.Modified()
        # Render batched by caller

    @staticmethod
    def _catmull_rom_smooth(points, steps=8):
        """
        Apply Catmull-Rom spline interpolation to a list of (u, z) 2-D points.

        Each consecutive pair of control points is expanded into `steps` smoothly
        curved sub-points, so the preview and the classification polygon use true
        curves instead of straight line segments.

        Parameters
        ----------
        points : list of (u, z) tuples   — raw freehand control points
        steps  : int                     — interpolated sub-points per segment

        Returns
        -------
        list of (u, z) tuples with len ≈ (len(points)-1)*steps + 1
        """
        import numpy as np
        n = len(points)
        if n < 2:
            return list(points)
        if n == 2:
            # Only two points — linear interpolation is already a straight line;
            # return as-is so we don't over-elaborate degenerate cases.
            return list(points)

        pts = np.asarray(points, dtype=np.float64)
        result = []

        for i in range(n - 1):
            # Ghost points at the boundaries use reflection to avoid a flat tail.
            p0 = pts[i - 1] if i > 0       else (2.0 * pts[i] - pts[i + 1])
            p1 = pts[i]
            p2 = pts[i + 1]
            p3 = pts[i + 2] if i + 2 < n   else (2.0 * pts[i + 1] - pts[i])

            ts = np.linspace(0.0, 1.0, steps, endpoint=(i == n - 2))
            t2 = ts * ts
            t3 = t2 * ts

            seg = 0.5 * (
                2.0 * p1
                + (-p0 + p2)                         * ts[:, None]
                + (2.0*p0 - 5.0*p1 + 4.0*p2 - p3)   * t2[:, None]
                + (-p0 + 3.0*p1 - 3.0*p2 + p3)       * t3[:, None]
            )
            result.extend(seg.tolist())

        # Always include the exact last control point.
        result.append(list(pts[-1]))
        return result


    @staticmethod
    def _centripetal_catmull_rom_smooth(points, steps=4):
        """Corner-safe MAIN-view freehand spline for uneven mouse samples."""
        import numpy as _np
        pts = _np.asarray(points, dtype=_np.float64)
        n = len(pts)
        if n < 2:
            return [tuple(map(float, p)) for p in pts]
        if n == 2:
            return [tuple(map(float, p)) for p in pts]

        steps = max(2, int(steps))
        eps = 1.0e-9
        out = []

        def _tj(ti, pa, pb):
            dist = float(_np.linalg.norm(pb - pa))
            return ti + max(dist ** 0.5, 1.0e-6)

        def _blend(pa, pb, ta, tb, t):
            den = tb - ta
            if abs(den) <= eps:
                return pa.copy()
            return ((tb - t) / den) * pa + ((t - ta) / den) * pb

        for i in range(n - 1):
            p1 = pts[i]
            p2 = pts[i + 1]
            p0 = pts[i - 1] if i > 0 else (2.0 * p1 - p2)
            p3 = pts[i + 2] if (i + 2) < n else (2.0 * p2 - p1)

            t0 = 0.0
            t1 = _tj(t0, p0, p1)
            t2 = _tj(t1, p1, p2)
            t3 = _tj(t2, p2, p3)

            taus = _np.linspace(t1, t2, steps, endpoint=False)
            chord = p2 - p1
            chord2 = float(_np.dot(chord, chord))

            for t in taus:
                a1 = _blend(p0, p1, t0, t1, t)
                a2 = _blend(p1, p2, t1, t2, t)
                a3 = _blend(p2, p3, t2, t3, t)
                b1 = _blend(a1, a2, t0, t2, t)
                b2 = _blend(a2, a3, t1, t3, t)
                q = _blend(b1, b2, t1, t2, t)

                if chord2 > eps:
                    progress = float(_np.dot(q - p1, chord) / chord2)
                    tau01 = float((t - t1) / max(t2 - t1, eps))
                    if (not _np.isfinite(progress)) or progress < -0.02 or progress > 1.02:
                        q = p1 + tau01 * chord

                if _np.all(_np.isfinite(q)):
                    out.append((float(q[0]), float(q[1])))

        out.append((float(pts[-1, 0]), float(pts[-1, 1])))
        return out

    def _capture_main_freehand_sample(self, dx, dy, min_pixel_step=2.0):
        """Capture MAIN freehand path at native MouseMove cadence, without rendering."""
        if not getattr(self, "is_drawing_freehand", False):
            return False
        if not self._is_main_view():
            return False

        try:
            dx = float(dx)
            dy = float(dy)
            last = getattr(self, "_last_freehand_display_pos", None)
            if last is not None:
                ddx = dx - float(last[0])
                ddy = dy - float(last[1])
                if (ddx * ddx + ddy * ddy) < float(min_pixel_step) ** 2:
                    return False

            pt = self._display_to_world_fast(dx, dy)
            u, v = self._get_view_coordinates(pt)
            if not (np.isfinite(float(u)) and np.isfinite(float(v))):
                return False

            self.drawing_points.append((float(u), float(v)))
            self._last_freehand_display_pos = (dx, dy)
            self._freehand_native_samples = int(getattr(self, "_freehand_native_samples", 0)) + 1
            return True
        except Exception:
            return False

    def _draw_freehand_preview(self, live_point=None):
        """
        ✅ PERFORMANCE FIX: Reuse VTK actor — NO RemoveActor/AddActor per frame.
        Only updates point coordinates and calls Modified(). Zero allocation after init.
        Draws smooth Catmull-Rom curves instead of straight line segments.
        """
        if len(self.drawing_points) < 1:
            return

        vtk_widget = self._get_active_vtk_widget()
        if vtk_widget is None:
            return
        renderer = vtk_widget.renderer

        # Collect all raw control points
        all_pts = list(self.drawing_points)
        if live_point:
            all_pts.append(live_point)

        if len(all_pts) < 2:
            return

        # ── SMOOTH: replace straight segments with Catmull-Rom curves ────────
        all_pts = self._catmull_rom_smooth(all_pts, steps=8)

        n = len(all_pts)

        # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
        if not hasattr(self, "_freehand_pts") or self.freehand_actor is None:
            from vtkmodules.vtkRenderingCore import vtkPolyDataMapper2D, vtkActor2D, vtkCoordinate as _C

            self._freehand_pts   = vtkPoints()
            self._freehand_lines = vtkCellArray()
            self._freehand_poly  = vtkPolyData()
            self._freehand_poly.SetPoints(self._freehand_pts)
            self._freehand_poly.SetLines(self._freehand_lines)

            mapper = vtkPolyDataMapper2D()
            mapper.SetInputData(self._freehand_poly)
            dc = _C(); dc.SetCoordinateSystemToDisplay()
            mapper.SetTransformCoordinate(dc)

            self.freehand_actor = vtkActor2D()
            self.freehand_actor.SetMapper(mapper)
            prop = self.freehand_actor.GetProperty()
            prop.SetColor(1, 0.8, 0)
            prop.SetLineWidth(2)
            prop.SetOpacity(0.8)

            renderer.AddActor2D(self.freehand_actor)

        # Re-attach if renderer was cleared
        if not renderer.HasViewProp(self.freehand_actor):
            renderer.AddActor2D(self.freehand_actor)
        self.freehand_actor.VisibilityOn()

        # ── FAST COORDINATE UPDATE ───────────────────────────────────────────
        if not hasattr(self, "_freehand_coord"):
            from vtkmodules.vtkRenderingCore import vtkCoordinate as _C
            self._freehand_coord = _C()
            self._freehand_coord.SetCoordinateSystemToWorld()

        coord = self._freehand_coord
        self._freehand_pts.Reset()
        self._freehand_lines.Reset()

        for i, (u, z) in enumerate(all_pts):
            xw, yw, zw = self._view2d_to_world(u, z)
            coord.SetValue(xw, yw, zw)
            d = coord.GetComputedDisplayValue(renderer)
            self._freehand_pts.InsertNextPoint(d[0], d[1], 0)
            if i > 0:
                self._freehand_lines.InsertNextCell(2)
                self._freehand_lines.InsertCellPoint(i - 1)
                self._freehand_lines.InsertCellPoint(i)

        self._freehand_pts.Modified()
        self._freehand_poly.Modified()
        # Render is batched by caller (_on_mouse_move_with_preview)



    def _draw_polygon_preview(self, live_point=None):
        """
        ✅ PERFORMANCE FIX: Reuse Actor2D — no RemoveActor/new objects per frame.
        """
        if len(self.drawing_points) < 1:
            return

        vtk_widget = self._get_active_vtk_widget()
        if vtk_widget is None:
            return
        renderer = vtk_widget.renderer

        all_pts = list(self.drawing_points)
        if live_point:
            all_pts.append(live_point)

        n = len(all_pts)

        # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
        if not hasattr(self, "_poly_pts_2d") or self.poly_actor is None:
            from vtkmodules.vtkRenderingCore import vtkPolyDataMapper2D, vtkActor2D, vtkCoordinate as _C

            self._poly_pts_2d   = vtkPoints()
            self._poly_lines_2d = vtkCellArray()
            self._poly_poly_2d  = vtkPolyData()
            self._poly_poly_2d.SetPoints(self._poly_pts_2d)
            self._poly_poly_2d.SetLines(self._poly_lines_2d)

            mapper = vtkPolyDataMapper2D()
            mapper.SetInputData(self._poly_poly_2d)
            dc = _C(); dc.SetCoordinateSystemToDisplay()
            mapper.SetTransformCoordinate(dc)

            self.poly_actor = vtkActor2D()
            self.poly_actor.SetMapper(mapper)
            prop = self.poly_actor.GetProperty()
            prop.SetColor(1, 1, 0)
            prop.SetLineWidth(3)
            prop.SetOpacity(1.0)

            renderer.AddActor2D(self.poly_actor)

        if not renderer.HasViewProp(self.poly_actor):
            renderer.AddActor2D(self.poly_actor)
        self.poly_actor.VisibilityOn()

        # ── FAST COORDINATE UPDATE ───────────────────────────────────────────
        if not hasattr(self, "_poly_coord_conv"):
            from vtkmodules.vtkRenderingCore import vtkCoordinate as _C
            self._poly_coord_conv = _C()
            self._poly_coord_conv.SetCoordinateSystemToWorld()

        coord = self._poly_coord_conv
        self._poly_pts_2d.Reset()
        self._poly_lines_2d.Reset()

        for i, (u, z) in enumerate(all_pts):
            xw, yw, zw = self._view2d_to_world(u, z)
            coord.SetValue(xw, yw, zw)
            d = coord.GetComputedDisplayValue(renderer)
            self._poly_pts_2d.InsertNextPoint(d[0], d[1], 0)
            if i > 0:
                self._poly_lines_2d.InsertNextCell(2)
                self._poly_lines_2d.InsertCellPoint(i - 1)
                self._poly_lines_2d.InsertCellPoint(i)

        # Close polygon if > 2 points
        if n > 2:
            self._poly_lines_2d.InsertNextCell(2)
            self._poly_lines_2d.InsertCellPoint(n - 1)
            self._poly_lines_2d.InsertCellPoint(0)

        self._poly_pts_2d.Modified()
        self._poly_poly_2d.Modified()
        # Render batched by caller

    def _draw_polygon_preview_cut(self, live_point=None):
        """Polygon preview for cut-section view using world-space vertices."""
        if not hasattr(self, "drawing_points_world_cut"):
            return
        if len(self.drawing_points_world_cut) < 1:
            return

        vtk_widget = self._get_active_vtk_widget()
        if vtk_widget is None:
            return
        renderer = vtk_widget.renderer

        all_pts = list(self.drawing_points_world_cut)
        if live_point is not None:
            all_pts.append(live_point)
        n = len(all_pts)
        if n < 1:
            return

        if not hasattr(self, "_poly_cut_pts") or getattr(self, "poly_actor_cut", None) is None:
            from vtkmodules.vtkRenderingCore import vtkPolyDataMapper2D, vtkActor2D, vtkCoordinate

            self._poly_cut_pts = vtkPoints()
            self._poly_cut_lines = vtkCellArray()
            self._poly_cut_poly = vtkPolyData()
            self._poly_cut_poly.SetPoints(self._poly_cut_pts)
            self._poly_cut_poly.SetLines(self._poly_cut_lines)

            mapper = vtkPolyDataMapper2D()
            mapper.SetInputData(self._poly_cut_poly)
            # Keep polygon preview anchored during pan/zoom by mapping points from world space.
            world_coord = vtkCoordinate()
            world_coord.SetCoordinateSystemToWorld()
            mapper.SetTransformCoordinate(world_coord)
            mapper.TransformCoordinateUseDoubleOn()

            self.poly_actor_cut = vtkActor2D()
            self.poly_actor_cut.SetMapper(mapper)
            prop = self.poly_actor_cut.GetProperty()
            prop.SetColor(1.0, 1.0, 0.0)
            prop.SetLineWidth(3)
            prop.SetOpacity(1.0)

            renderer.AddActor2D(self.poly_actor_cut)

        if not renderer.HasViewProp(self.poly_actor_cut):
            renderer.AddActor2D(self.poly_actor_cut)
        self.poly_actor_cut.VisibilityOn()

        pts = self._poly_cut_pts
        lines = self._poly_cut_lines
        pts.Reset()
        lines.Reset()

        for xw, yw, zw in all_pts:
            pts.InsertNextPoint(float(xw), float(yw), float(zw))
        for i in range(n - 1):
            lines.InsertNextCell(2)
            lines.InsertCellPoint(i)
            lines.InsertCellPoint(i + 1)
        if n > 2:
            lines.InsertNextCell(2)
            lines.InsertCellPoint(n - 1)
            lines.InsertCellPoint(0)

        pts.Modified()
        self._poly_cut_poly.Modified()

    def _draw_brush_cursor(self, center, radius=20.0):
        """
        ✅ PERFORMANCE FIX: Brush cursor in DISPLAY pixels (zoom invariant).
        Reuses VTK actor — only updates point data. Zero allocation per frame.
        """
        vtk_widget = self._get_active_vtk_widget()
        if vtk_widget is None:
            return
        renderer = vtk_widget.renderer

        x, y = self.interactor.GetEventPosition()
        r = max(float(radius), 3.0)

        num_segments = 32
        dot_segments = 8

        # Total points: dashed circle (num_segments pts) + crosshair (4 pts) + dot (dot_segments*2 pts)
        # Each dash: 2 pts  ×  num_segments/2 dashes = num_segments pts
        # Crosshair: 4 pts (2 lines × 2 pts)
        # Dot ring: dot_segments × 2 pts
        total_pts = num_segments + 4 + dot_segments * 2

        # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
        if not hasattr(self, "_brush_pts") or self.brush_actor is None:
            from vtkmodules.vtkRenderingCore import vtkPolyDataMapper2D, vtkActor2D, vtkCoordinate as _C

            if getattr(self, "brush_actor", None) is not None:
                try:
                    renderer.RemoveActor2D(self.brush_actor)
                except Exception:
                    pass
                self.brush_actor = None

            self._brush_pts   = vtkPoints()
            self._brush_lines = vtkCellArray()
            self._brush_poly  = vtkPolyData()
            self._brush_poly.SetPoints(self._brush_pts)
            self._brush_poly.SetLines(self._brush_lines)

            mapper = vtkPolyDataMapper2D()
            mapper.SetInputData(self._brush_poly)
            dc = _C(); dc.SetCoordinateSystemToDisplay()
            mapper.SetTransformCoordinate(dc)

            self.brush_actor = vtkActor2D()
            self.brush_actor.SetMapper(mapper)
            prop = self.brush_actor.GetProperty()
            prop.SetColor(1, 1, 0)
            prop.SetLineWidth(3)
            prop.SetOpacity(0.9)

            renderer.AddActor2D(self.brush_actor)

        if not renderer.HasViewProp(self.brush_actor):
            renderer.AddActor2D(self.brush_actor)
        self.brush_actor.VisibilityOn()

        # ── FAST GEOMETRY UPDATE (display coords, no world conversion) ────────
        pts   = self._brush_pts
        lines = self._brush_lines
        pts.Reset()
        lines.Reset()

        def add_seg(p0, p1):
            i0 = pts.InsertNextPoint(float(p0[0]), float(p0[1]), 0.0)
            i1 = pts.InsertNextPoint(float(p1[0]), float(p1[1]), 0.0)
            lines.InsertNextCell(2)
            lines.InsertCellPoint(i0)
            lines.InsertCellPoint(i1)

        # dashed circle (every other segment)
        for i in range(0, num_segments, 2):
            a1 = 2.0 * np.pi * i / num_segments
            a2 = 2.0 * np.pi * (i + 1) / num_segments
            p1 = (x + r * np.cos(a1), y + r * np.sin(a1))
            p2 = (x + r * np.cos(a2), y + r * np.sin(a2))
            add_seg(p1, p2)

        # crosshair
        ch = r * 0.3
        add_seg((x - ch, y), (x + ch, y))
        add_seg((x, y - ch), (x, y + ch))

        # center dot ring
        dot_r = max(2.0, r * 0.05)
        for i in range(dot_segments):
            a1 = 2.0 * np.pi * i / dot_segments
            a2 = 2.0 * np.pi * (i + 1) / dot_segments
            p1 = (x + dot_r * np.cos(a1), y + dot_r * np.sin(a1))
            p2 = (x + dot_r * np.cos(a2), y + dot_r * np.sin(a2))
            add_seg(p1, p2)

        pts.Modified()
        self._brush_poly.Modified()
        # Render batched by caller

    #     """
    #     ✅ Optimized Clear: Uses Visibility toggles instead of Actor removal.
    #     Eliminates the 'Delete-Recreate' lag and prevents preview flickering.
    #     """
    #     # 1. Identify all potential actor names
    #         "line_actor", "dotted_actor", "rect_actor", "circle_actor",
    #         "brush_actor", "polygon_actor", "freehand_actor", "poly_actor",
    #         "line_actor_cut", "dotted_actor_cut", "rect_actor_cut", "circle_actor_cut",
    #         "brush_actor_cut", "freehand_actor_cut", "poly_actor_cut",
    #     ]

    #     # 2. Batch Visibility Change (Instantaneous)
    #             # Instead of removing/None, we just hide. 
    #             # This keeps the VTK pipeline ready for the next interaction.
    #             actor.VisibilityOff()

    #     # 3. Reset state variables
    #     self.P1 = None
    #     self.is_dragging = False
    #     self.drawing_points = []
    #     self.is_drawing_freehand = False
        
    #         self.drawing_points_display_cut = []
    #         self.P1_display_cut = None

    #     # 4. Single Final Render Call
    #     # We only render ONCE to update the screen, not once per renderer/actor.
    #             self._safe_render(vtk_widget)
        
    #     # Also render cut-section if it exists
    #                 self._safe_render(cut_vtk)

    def _clear_all_previews(self):
        """
        ✅ Optimized Clear: Uses Visibility toggles instead of Actor removal.
        Eliminates the 'Delete-Recreate' lag and prevents preview flickering.
    
        ✅ FIX (fast-classification): Reset throttle timestamps so the very
        first mouse-move of the NEXT classification is never skipped.
        """
        # 1. Identify all potential actor names
        actor_names = [
            "line_actor", "dotted_actor", "rect_actor", "circle_actor", "circle_actor_main",
            "brush_actor", "polygon_actor", "freehand_actor", "poly_actor",
            "line_actor_cut", "dotted_actor_cut", "rect_actor_cut", "circle_actor_cut",
            "brush_actor_cut", "freehand_actor_cut", "poly_actor_cut",
            "_locate_line_actor",
        ]
    
        # 2. Clear preview overlay first.
        # Important: _hide_preview_overlay_for_pan() may restore hidden preview actors,
        # so call it BEFORE hiding/removing the VTK preview actors.
        self._hide_preview_overlay_for_pan()

        preview_renderers = []

        try:
            vtk_widget = self._get_active_vtk_widget()
            if vtk_widget is not None and getattr(vtk_widget, "renderer", None) is not None:
                preview_renderers.append(vtk_widget.renderer)
        except Exception:
            pass

        try:
            main_vtk = getattr(self.app, "vtk_widget", None)
            if main_vtk is not None and getattr(main_vtk, "renderer", None) is not None:
                preview_renderers.append(main_vtk.renderer)
        except Exception:
            pass

        try:
            cut_ctrl = getattr(self.app, "cut_section_controller", None)
            cut_vtk = getattr(cut_ctrl, "cut_vtk", None) if cut_ctrl is not None else None
            if cut_vtk is not None and getattr(cut_vtk, "renderer", None) is not None:
                preview_renderers.append(cut_vtk.renderer)
        except Exception:
            pass

        try:
            for vtk_widget in list(getattr(self.app, "section_vtks", {}) or {}).values():
                if vtk_widget is not None and getattr(vtk_widget, "renderer", None) is not None:
                    preview_renderers.append(vtk_widget.renderer)
        except Exception:
            pass

        # Remove duplicates
        unique_renderers = []
        seen_renderers = set()
        for renderer in preview_renderers:
            if renderer is None:
                continue
            marker = id(renderer)
            if marker in seen_renderers:
                continue
            seen_renderers.add(marker)
            unique_renderers.append(renderer)

        # Hide AND remove transient classification preview actors.
        # VisibilityOff alone is not enough because Surface overlay restore can re-add
        # main-renderer candidates and make them visible again.
        for name in actor_names:
            actor = getattr(self, name, None)
            if actor:
                try:
                    setattr(actor, "_is_classification_preview", True)
                    setattr(actor, "_naksha_preview_owner", "classification")
                except Exception:
                    pass

                try:
                    actor.VisibilityOff()
                except Exception:
                    pass

                for renderer in unique_renderers:
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
    
        # 3. Reset state variables
        self.P1 = None
        self.is_dragging = False
        self.click_to_finalize = False
        self._press_pos = None
        self.drawing_points = []
        self.is_drawing_freehand = False
    
        if hasattr(self, 'drawing_points_display_cut'):
            self.drawing_points_display_cut = []
        if hasattr(self, 'drawing_points_world_cut'):
            self.drawing_points_world_cut = []
        if hasattr(self, 'P1_display_cut'):
            self.P1_display_cut = None
    
        # ✅ FIX A — Reset render/move throttles so the NEXT classification's
        # first mouse-move is never accidentally skipped by the 16 ms guard.
        self._last_render_time     = 0.0
        self._last_mouse_move_time = 0.0
    
        # 4. Single Final Render Call
        vtk_widget = self._get_active_vtk_widget()
        if vtk_widget:
            try:
                self._safe_render(vtk_widget)
            except Exception:
                pass
    
        # Also render cut-section if it exists
        if hasattr(self.app, 'cut_section_controller'):
            cut_vtk = getattr(self.app.cut_section_controller, 'cut_vtk', None)
            if cut_vtk and hasattr(cut_vtk, 'renderer'):
                try:
                    self._safe_render(cut_vtk)
                except Exception:
                    pass  
 

    def _on_mouse_move_with_preview(self, obj, evt):
            """
            ✅ FIXED: Shows previews during drag for ALL tools
            ✅ FIXED: Brush preview size does NOT change with zoom
            ✅ FIXED: Brush classification area MATCHES the preview (pixel -> view radius conversion)
            ✅ FIXED: Above/Below line uses EXACT same P2 on release as preview (prevents snapping to pole)
            ✅ FIXED: Rectangle uses EXACT same P2 on release as preview (prevents coordinate mismatch)

            ✅ FIX (safe): ensure np is always defined in this function scope to avoid:
            "local variable 'np' referenced before assignment"
            """
            self._check_tool_changed()

            import numpy as np  # ✅ IMPORTANT: prevents UnboundLocalError for np in this method

            # ══════════════════════════════════════════════════════════════════
            # PAN ABSOLUTE PRIORITY — mirror digitizer._on_mouse_move pattern.
            # Without this guard, slow middle-button pan moves (more than 16ms
            # apart) fall through to _get_display_point + _display_to_world_fast
            # — a per-event world-point pick that makes panning feel heavy/laggy.
            # _do_safe_pan already translates preview actors for any in-progress
            # classification drag (rectangle/freehand/etc.), so the active gesture
            # stays locked to the cursor while panning.
            # ══════════════════════════════════════════════════════════════════
            if getattr(self, '_is_panning', False):
                self._do_safe_pan()
                try:
                    if hasattr(obj, "AbortFlagOn"):
                        obj.AbortFlagOn()
                    elif hasattr(obj, "SetAbortFlag"):
                        obj.SetAbortFlag(1)
                except Exception:
                    pass
                return

            # ══════════════════════════════════════════════════════════════════
            # LINE TAP-VS-DRAG: track the MAXIMUM distance moved from the press
            # point while the button is held — not just the final displacement
            # at release. MicroStation's own state machine commits to DRAGGING
            # the instant the threshold is crossed and stays committed even if
            # the cursor drifts back near the origin before release; checking
            # only net (press→release) displacement would wrongly reclassify
            # a "dragged out and back" gesture as a tap. This runs unthrottled
            # (before the 16ms preview-rate skip below) so a fast drag can't
            # slip past the peak between throttled frames.
            #
            # Movement during the initial grace window (_LINE_TAP_GRACE_SEC,
            # matching on_left_release) is never accumulated at all — a real
            # fast tap's natural hand jitter happens almost entirely within
            # this window, and MicroStation itself does not start counting
            # distance toward drag detection until this brief settle period
            # has passed.
            # ══════════════════════════════════════════════════════════════════
            if getattr(self, 'is_dragging', False) and getattr(self, '_press_pos', None) is not None:
                _mm_tool = getattr(getattr(self, 'app', None), "active_classify_tool", None)
                if (_mm_tool in ("above_line", "below_line", "parallel_line")
                        and (time.time() - getattr(self, '_line_press_time', 0.0)) >= _LINE_TAP_GRACE_SEC):
                    try:
                        _cx, _cy = self.interactor.GetEventPosition()
                        _px, _py = self._press_pos
                        _d = ((_cx - _px) ** 2 + (_cy - _py) ** 2) ** 0.5
                        if _d > getattr(self, '_line_press_max_move_px', 0.0):
                            self._line_press_max_move_px = _d
                    except Exception:
                        pass

            # MAIN FREEHAND ONLY: capture geometry before the generic preview
            # throttle. Rendering may stay at 30 FPS on huge point clouds, but
            # the hand path itself is sampled at native mouse-event cadence.
            try:
                _pre_tool = getattr(getattr(self, "app", None), "active_classify_tool", None)
                if (_pre_tool == "freehand"
                        and getattr(self, "is_drawing_freehand", False)
                        and self._is_main_view()):
                    _fh_dx, _fh_dy = self._get_display_point()
                    self._capture_main_freehand_sample(_fh_dx, _fh_dy, min_pixel_step=2.0)
            except Exception:
                pass

            current_time = time.time()

            if not hasattr(self, '_last_mouse_move_time'):
                self._last_mouse_move_time = 0.0

            preview_dt = self._preview_interval_sec()
            # Skip if less than 16ms since last update (60 FPS)
            if (current_time - self._last_mouse_move_time) < preview_dt:
                self._do_safe_pan()  # ✅ Custom pan — no VTK Render()
                return

            self._last_mouse_move_time = current_time

            # ✅ SAFETY CHECKS
            if not hasattr(self, 'app') or self.app is None:
                return
            if not hasattr(self, 'style') or self.style is None:
                return
            if not hasattr(self, 'interactor') or self.interactor is None:
                return

            tool = getattr(self.app, "active_classify_tool", None)

            if tool is not None:
                try:
                    if hasattr(obj, "AbortFlagOn"):
                        obj.AbortFlagOn()
                    elif hasattr(obj, "SetAbortFlag"):
                        obj.SetAbortFlag(1)
                except Exception:
                    pass

            if self._is_classification_paused_for_pan(tool):
                self._do_safe_pan()
                try:
                    if hasattr(obj, "AbortFlagOn"):
                        obj.AbortFlagOn()
                    elif hasattr(obj, "SetAbortFlag"):
                        obj.SetAbortFlag(1)
                except Exception:
                    pass
                return

            # Try to pick world point (fast, non-snapping)
            try:
                dx, dy = self._get_display_point()
                P2 = self._display_to_world_fast(dx, dy)
            except Exception:
                self._do_safe_pan()  # ✅ Custom pan — no VTK Render()
                return

            # ✅ Cache active vtk widget (refresh every 100ms)
            if not hasattr(self, '_cached_vtk_widget'):
                self._cached_vtk_widget = None
                self._cached_widget_time = 0.0

            if (current_time - self._cached_widget_time) > 0.1:
                self._cached_vtk_widget = self._get_active_vtk_widget()
                self._cached_widget_time = current_time

            vtk_widget = self._cached_vtk_widget

            # ✅ Detect CUT SECTION
            is_cut_section = False
            if hasattr(self.app, 'cut_section_controller'):
                if vtk_widget == getattr(self.app.cut_section_controller, 'cut_vtk', None):
                    is_cut_section = True

            # ── Section-locate rubber-band: draw preview line from locked click to cursor ──
            if not self._is_main_view() and not is_cut_section:
                locate_disp = getattr(self.app, '_section_locate_display', None)
                locate_view = getattr(self.app, '_section_locate_view', None)
                if locate_disp is not None and locate_view == self._get_view_index_from_interactor():
                    self._draw_locate_line_in_section(vtk_widget, locate_disp, (dx, dy))
                    if vtk_widget:
                        self._safe_render(vtk_widget)

            # Render throttle
            if not hasattr(self, '_last_render_time'):
                self._last_render_time = 0.0

            # -----------------------
            # PREVIEW RENDER LOGIC
            # -----------------------
            if getattr(self.app, '_debug_perf', False):
                t_start = time.perf_counter()
                n_actors = vtk_widget.renderer.GetNumberOfProps()
                print(f"DEBUG: Rendering {n_actors} actors...")

            # ✅ Circle preview
            if tool == "circle" and self.P1 is not None and self.is_dragging:
                # ✅ CRITICAL: Store P2 for classification to match preview
                self._last_circle_preview_P2 = P2
                
                if is_cut_section:
                    self._draw_circle_preview_cut(self.P1, P2)
                elif self._is_main_view():
                    self._draw_circle_preview_main(self.P1, P2)
                else:
                    self._draw_circle_preview(self.P1, P2)

                if vtk_widget and (current_time - self._last_render_time) > preview_dt:
                    self._safe_render(vtk_widget)
                    self._last_render_time = current_time

            # ✅ Rectangle preview
            if tool == "rectangle" and self.P1 is not None and self.is_dragging:
                # ✅ CRITICAL: Store P2 for classification to match preview
                self._last_rectangle_preview_P2 = P2
                
                if is_cut_section:
                    self._draw_rectangle_preview_cut(self.P1, P2)
                else:
                    self._draw_rectangle_preview(self.P1, P2)

                if vtk_widget and (current_time - self._last_render_time) > preview_dt:
                    self._safe_render(vtk_widget)
                    self._last_render_time = current_time

            # ✅ Line tools preview
            elif tool in ("above_line", "below_line", "parallel_line") and self.P1 is not None and self.is_dragging:
                self.app._classification_preview_active = True
                self._last_line_preview_P2 = P2

                if is_cut_section:
                    self._draw_line_preview_cut(self.P1, P2)
                else:
                    self._draw_temp_line(self.P1, P2)

                if vtk_widget and (current_time - self._last_render_time) > preview_dt:
                    self._safe_render(vtk_widget)
                    self._last_render_time = current_time

            # ✅ Brush tool
            elif tool == "brush":
                to_class = getattr(self.app, "to_class", None)
                if self.is_dragging:
                    active_actor_names = self._get_active_tool_preview_actor_names("brush")
                    non_active_actor_names = [
                        name for name in self._get_preview_actor_names()
                        if name not in active_actor_names
                    ]
                    if non_active_actor_names:
                        self._set_preview_visibility(False, non_active_actor_names)

                    self._last_brush_preview_P2 = P2  # ✅ non-snapping endpoint used for preview
                    # ✅ FIX 1: Calculate center differently for cut section
                    if is_cut_section:
                        try:
                            from ..classification_tools import _project_to_cut_view
                            P2_cut = _project_to_cut_view(self.app, np.array([P2]))[0]
                            center = (float(P2_cut[0]), float(P2_cut[1]))
                        except Exception:
                            center = self._get_view_coordinates(P2)
                    else:
                        # Cross-section or main view - existing logic works
                        center = self._get_view_coordinates(P2)

                    brush_size_pixels = float(getattr(self.app, "brush_preview_px", 20.0))

                    radius_view = float(getattr(self.app, "brush_radius", 1.0))
                    if vtk_widget is not None:
                        try:
                            radius_view = float(self._pixel_radius_to_view_radius(
                                vtk_widget, P2, brush_size_pixels
                            ))
                        except Exception:
                            radius_view = float(getattr(self.app, "brush_radius", 1.0))

                    self._last_brush_radius_view = radius_view

                    brush_shape = getattr(self.app, "brush_shape", "circle")

                    if is_cut_section:
                        if brush_shape == "rectangle":
                            self._draw_rectangle_cursor_cut_display(P2, brush_size_pixels)
                        else:
                            self._draw_brush_cursor_cut_display(P2, brush_size_pixels)
                    else:
                        if brush_shape == "rectangle":
                            self._draw_rectangle_cursor(center, brush_size_pixels)
                        else:
                            self._draw_brush_cursor(center, brush_size_pixels)

                # ✅ UNIFIED RENDER: For main view brush, DON'T render here —
                # the classification drag handler below does a throttled render
                # that includes both cursor + classification changes in one pass.
                if self._is_main_view() and self.is_dragging and tool == "brush":
                    pass  # Skip — render happens in classification block below
                elif vtk_widget and (current_time - self._last_render_time) > preview_dt:
                    self._safe_render(vtk_widget)
                    self._last_render_time = current_time

                if self.is_dragging:
                    if self._is_main_view():
                        # ═══════════════════════════════════════════════════════
                        # MICROSTATION PRODUCTION BRUSH
                        # Phase 1 (every mouse move): spatial lookup → CPU classify → accumulate
                        # Phase 2 (30fps tick only): single GPU injection + render
                        # Zero allocation per stamp. All numpy, no Python loops.
                        # ═══════════════════════════════════════════════════════
                        xyz = self.app.data["xyz"]
                        to_class = getattr(self.app, "to_class", None)

                        # ✅ DATASET CHANGE SAFEGUARD: Ensure mask matches current dataset size
                        if hasattr(self, "_brush_accumulated_mask") and self._brush_accumulated_mask is not None:
                            if len(self._brush_accumulated_mask) != len(xyz):
                                print(f"🔄 Dataset size changed ({len(self._brush_accumulated_mask)} -> {len(xyz)}), clearing brush mask")
                                self._brush_accumulated_mask = None
                                self._brush_old_classes = {}

                        if not hasattr(self, "_brush_accumulated_mask") or self._brush_accumulated_mask is None:
                            self._brush_accumulated_mask = np.zeros(len(xyz), dtype=bool)
                            self._brush_old_classes = {}
                            self._brush_frame_chunks = []

                        if not hasattr(self, "_brush_stroke_positions") or self._brush_stroke_positions is None:
                            self._brush_stroke_positions = []

                        prev = getattr(self, "_last_brush_center", None)
                        classes = self.app.data["classification"]
                        from_classes = getattr(self.app, "from_classes", None)
                        visible_classes = getattr(self, "_brush_visible_classes", None)
                        visible_classes_arr = None
                        if visible_classes is not None and visible_classes != []:
                            visible_classes_arr = np.asarray(visible_classes, dtype=classes.dtype)

                        u1, z1 = self._get_view_coordinates(prev or center)
                        u2, z2 = self._get_view_coordinates(center)
                        
                        u_min, u_max = min(u1, u2) - radius_view, max(u1, u2) + radius_view
                        z_min, z_max = min(z1, z2) - radius_view, max(z1, z2) + radius_view
                        
                        grid = getattr(self, "_main_grid_index", None)
                        if grid and grid.valid:
                            hit = grid.query_rectangle_minmax(u_min, z_min, u_max, z_max)
                        else:
                            hit = np.array([], dtype=np.int32)
                        
                        if len(hit) > 0:
                            # Distance-to-segment filter (vectorized)
                            pts2d = self._main_points_2d[hit]
                            du, dz = u2 - u1, z2 - z1
                            l2 = du*du + dz*dz
                            
                            if l2 < 1e-9:
                                # Stationary brush
                                dist_sq = (pts2d[:, 0] - u1)**2 + (pts2d[:, 1] - z1)**2
                            else:
                                # Projection of points onto the line segment
                                t = np.clip(((pts2d[:, 0] - u1) * du + (pts2d[:, 1] - z1) * dz) / l2, 0, 1)
                                proj_u, proj_z = u1 + t * du, z1 + t * dz
                                dist_sq = (pts2d[:, 0] - proj_u)**2 + (pts2d[:, 1] - proj_z)**2
                            
                            hit = hit[dist_sq <= (radius_view * radius_view)]
                            
                        if len(hit) > 0:
                            # Filter already-done in this stroke
                            fresh = hit[~self._brush_accumulated_mask[hit]]
                            if len(fresh) > 0:
                                # Filter from_classes / visible_classes
                                if from_classes:
                                    fc_arr = np.asarray(from_classes, dtype=classes.dtype)
                                    if visible_classes_arr is not None:
                                        fc_mask = np.isin(classes[fresh], fc_arr[np.isin(fc_arr, visible_classes_arr)])
                                    else:
                                        fc_mask = np.isin(classes[fresh], fc_arr)
                                    fresh = fresh[fc_mask]
                                elif visible_classes_arr is not None:
                                    fresh = fresh[np.isin(classes[fresh], visible_classes_arr)]

                                if len(fresh) > 0:
                                    from gui.flight_line_filter import filter_visible_flight_line_indices
                                    fresh = filter_visible_flight_line_indices(
                                        self.app,
                                        fresh,
                                        len(classes),
                                        slot=self._active_flight_line_slot(),
                                    )

                                if len(fresh) > 0:
                                    # 1. Unique and capture for undo
                                    fresh = np.unique(fresh)
                                    if not hasattr(self, "_brush_old_classes_arrays"):
                                        self._brush_old_classes_arrays = []
                                        self._brush_indices_arrays = []
                                    
                                    self._brush_old_classes_arrays.append(classes[fresh].copy())
                                    self._brush_indices_arrays.append(fresh.copy())

                                    # 2. CPU classify immediately
                                    classes[fresh] = to_class
                                    self._brush_accumulated_mask[fresh] = True

                                    # 3. Accumulate for GPU tick
                                    self._brush_frame_chunks.append(fresh)
                                    self._brush_needs_render = True

                        self._last_brush_center = center
                        self._brush_stroke_positions.append(center)

                        # PHASE 2: 30fps tick — single GPU injection + render
                        frame_chunks = getattr(self, '_brush_frame_chunks', None)
                        frame_fresh = None
                        if frame_chunks:
                            if len(frame_chunks) == 1:
                                frame_fresh = np.unique(frame_chunks[0])
                            else:
                                frame_fresh = np.unique(np.concatenate(frame_chunks))

                        if (frame_fresh is not None and len(frame_fresh) > 0
                                and getattr(self, '_brush_needs_render', False)):
                            now = time.time()
                            if (now - getattr(self, '_brush_last_render_time', 0)) > 0.033:
                                try:
                                    from gui.unified_actor_manager import (
                                        _get_unified_actor, _mark_actor_dirty,
                                        fast_cross_section_update, _push_uniforms_direct
                                    )
                                    palette = self._get_active_palette()
                                    entry = palette.get(int(to_class), {})
                                    color = np.array(
                                        entry.get('color', (128, 128, 128))
                                        if entry.get('show', True)
                                        else (0, 0, 0),
                                        dtype=np.uint8
                                    )

                                    # Direct RGB poke on unified actor (no mask scan)
                                    actor = _get_unified_actor(self.app)
                                    if actor is not None:
                                        rgb_ptr = getattr(actor, '_naksha_rgb_ptr', None)
                                        class_ptr = getattr(actor, '_naksha_section_class', None)
                                        mesh = getattr(actor, '_naksha_mesh', None)
                                        gi = getattr(self.app, '_main_global_indices', None)
                                        main_display_mode = str(getattr(self.app, "display_mode", "class") or "class").lower()
                                        allow_main_rgb_update = main_display_mode in ("class", "shaded_class")
                                        
                                        if rgb_ptr is not None and rgb_ptr.flags.writeable:
                                            local = None
                                            if gi is not None:
                                                # O(M) LOD mapping via modular arithmetic.
                                                # gi = arange(0, N, step), so a global index g is in LOD
                                                # iff g % step == 0, and its local index is g // step.
                                                # Avoids intersect1d which sorts the full 10M gi array
                                                # every GPU tick (~30-100ms) and was the primary bottleneck.
                                                _step = int(getattr(self.app, '_main_lod_step', 0) or 0)
                                                if _step > 1:
                                                    _in_lod = (frame_fresh % _step) == 0
                                                    local = (frame_fresh[_in_lod] // _step).astype(np.int64)
                                                    # clip to valid range (guards against stale gi after file reload)
                                                    local = local[local < len(gi)]
                                                elif _step == 1:
                                                    # step=1 → every global index is in LOD, local == global
                                                    local = frame_fresh[frame_fresh < len(gi)]
                                                else:
                                                    # _main_lod_step not set (old actor) — safe fallback
                                                    _, local, _ = np.intersect1d(
                                                        gi, frame_fresh, assume_unique=True, return_indices=True
                                                    )

                                                if len(local) > 0 and allow_main_rgb_update:
                                                    # Guard: skip slot-0 RGB writes outside class/shaded_class modes.
                                                    rgb_ptr[local] = color
                                            else:
                                                # No LOD (Full points): direct indexing
                                                local = frame_fresh
                                                if allow_main_rgb_update:
                                                    # Guard: skip slot-0 RGB writes outside class/shaded_class modes.
                                                    rgb_ptr[local] = color

                                            # vtk_ca.Modified() intentionally omitted during drag.
                                            # It triggers a full 30 MB CPU→GPU buffer upload (~150 ms)
                                            # on every GPU tick. Deferring it to mouse-release
                                            # (guarantee_main_view_visual_refresh) keeps each tick at
                                            # ~1 ms. The paint overlay provides live visual feedback.

                                            if local is not None and len(local) > 0:
                                                if class_ptr is not None and len(class_ptr) > local[-1]:
                                                    class_ptr[local] = class_ptr.dtype.type(to_class)
                                                elif mesh is not None:
                                                    class_vtk_arr = mesh.GetPointData().GetArray("Classification")
                                                    if class_vtk_arr is not None:
                                                        from vtkmodules.util import numpy_support
                                                        cls_np = numpy_support.vtk_to_numpy(class_vtk_arr)
                                                        cls_np[local] = cls_np.dtype.type(to_class)
                                                        # class_vtk_arr.Modified() deferred to release
                                        # ✅ CUT SECTION REAL-TIME UPDATE
                                        if is_cut_section:
                                            try:
                                                cut_ctrl = self.app.cut_section_controller
                                                cut_actor = cut_ctrl.cut_vtk.actors.get("_cut_section_unified")
                                                if cut_actor:
                                                    # Inject classification into cut actor's mesh/ptr
                                                    c_ptr = getattr(cut_actor, '_naksha_section_class', None)
                                                    if c_ptr is not None:
                                                        # Resolve which indices in the cut were hit
                                                        # (This is a bit slow for real-time but small enough for cut sections)
                                                        cut_idx = cut_ctrl._cut_index_map
                                                        # find which 'fresh' indices are in 'cut_idx'
                                                        # Actually, it's easier to just re-read the whole thing if it's small
                                                        # but for real-time we want to be fast.
                                                        # ✅ OPTIMIZED: Use intersect1d instead of isin (2.3x faster)
                                                        _, _, mask_in_cut = np.intersect1d(
                                                            frame_fresh, cut_idx, 
                                                            return_indices=True, 
                                                            assume_unique=True
                                                        )
                                                        
                                                        if mask_in_cut.size > 0:
                                                            c_ptr[mask_in_cut] = c_ptr.dtype.type(to_class)
                                                            
                                                            # Update colors too
                                                            rgb_ptr = getattr(cut_actor, '_naksha_rgb_ptr', None)
                                                            if rgb_ptr is not None:
                                                                 rgb_ptr[mask_in_cut] = color
                                                                 
                                                            # ✅ OPTIMIZED: Only sync uniforms if palette state changed
                                                            ctx = getattr(cut_actor, '_naksha_shader_ctx', None)
                                                            if ctx:
                                                                 last_gen = getattr(cut_actor, '_last_uniform_gen', -1)
                                                                 if ctx._generation != last_gen:
                                                                     _bsz = float(getattr(self.app, 'point_size', 2.5))
                                                                     ctx.force_reload()
                                                                     ctx.load_from_palette(palette, 0, _bsz)
                                                                     _push_uniforms_direct(cut_actor, ctx)
                                                                     cut_actor._last_uniform_gen = ctx._generation
                                                             
                                                            _mark_actor_dirty(cut_actor)
                                                            self._safe_render_pyvista(cut_ctrl.cut_vtk)
                                            except Exception as _ce:
                                                print(f"⚠️ Real-time cut refresh failed: {_ce}")
                                  
                                        # ✅ MAIN VIEW ONLY during drag — sections update on release
                                        if (main_display_mode == "shaded_class"
                                                and _shading_mesh_exists(self.app)):
                                            try:
                                                from gui.shading_display import refresh_shaded_after_classification_fast
                                                acc_mask = self._brush_accumulated_mask
                                                refresh_shaded_after_classification_fast(self.app, changed_mask=acc_mask)
                                            except Exception as _se:
                                                print(f"⚠️ Brush shading refresh: {_se}")
                                        elif main_display_mode == "class":
                                            # Append painted XYZ to the overlay (tiny actor, zero main-buffer upload).
                                            # Then fire a direct render bypassing classify-streak coalescing.
                                            # The main actor uses its cached GPU buffer → render costs ~1 ms.
                                            self._append_brush_paint_overlay(frame_fresh)
                                            _mgr = getattr(self.app, 'gpu_render_manager', None)
                                            _orig = getattr(_mgr, '_original_render', None) if _mgr else None
                                            if _orig is not None:
                                                try:
                                                    _orig()
                                                except Exception:
                                                    pass
                                            else:
                                                self._safe_render_pyvista(self.app.vtk_widget)

                                except Exception as e:
                                    print(f"⚠️ Brush GPU inject: {e}")

                                self._brush_frame_chunks = []
                                self._brush_last_render_time = now
                                self._brush_needs_render = False

                        self._last_brush_center = center
                        self._brush_stroke_positions.append(center)

                    else:
                        # ✅ FIX 2: This section now works correctly because 'center' is already projected for cut section
                        # CROSS-SECTION / CUT accumulation
                        try:
                            if (
                                getattr(self, "_brush_section_local_mask", None) is None or
                                getattr(self, "_brush_section_pts2d", None) is None or
                                getattr(self, "_brush_section_indices", None) is None
                            ):
                                pass  # Skip if buffers not initialized
                            else:
                                # ✅ UNIFIED PERFORMANCE: Segment-based spatial query
                                # center is already projected for Cut Section at this point.
                                u, z = float(center[0]), float(center[1])
                                prev_uv = getattr(self, "_last_brush_center_uv", None) or (u, z)
                                u1, z1 = prev_uv
                                u2, z2 = u, z
                                
                                grid = getattr(self, "_brush_section_grid", None)
                                if grid and grid.valid:
                                    # Query the bounding box of the whole stroke segment
                                    u_min, u_max = min(u1, u2) - radius_view, max(u1, u2) + radius_view
                                    z_min, z_max = min(z1, z2) - radius_view, max(z1, z2) + radius_view
                                    
                                    hit = grid.query_rectangle_minmax(u_min, z_min, u_max, z_max)
                                    if len(hit) > 0:
                                        # Distance-to-segment filter (vectorized)
                                        pts2d = self._brush_section_pts2d
                                        du, dz = u2 - u1, z2 - z1
                                        l2 = du*du + dz*dz
                                        sub_pts = pts2d[hit]
                                        
                                        if l2 < 1e-9:
                                            # Stationary brush
                                            dist_sq = (sub_pts[:, 0] - u1)**2 + (sub_pts[:, 1] - z1)**2
                                        else:
                                            # Projection of points onto the line segment
                                            t = np.clip(((sub_pts[:, 0] - u1) * du + (sub_pts[:, 1] - z1) * dz) / l2, 0, 1)
                                            proj_u, proj_z = u1 + t * du, z1 + t * dz
                                            dist_sq = (sub_pts[:, 0] - proj_u)**2 + (sub_pts[:, 1] - proj_z)**2
                                        
                                        fresh_local = hit[dist_sq <= (radius_view * radius_view)]
                                        
                                        if len(fresh_local) > 0:
                                            # Filter already-done in this stroke
                                            fresh_local = fresh_local[~self._brush_section_local_mask[fresh_local]]
                                            
                                            if len(fresh_local) > 0:
                                                # ✅ CLASS PROTECTION & VISIBILITY (Section View)
                                                global_indices = self._brush_section_indices[fresh_local]
                                                classes = self.app.data['classification']
                                                from_classes = getattr(self.app, "from_classes", None)
                                                visible_classes = getattr(self, "_brush_visible_classes", None)
                                                
                                                # Skip if all classes hidden
                                                if visible_classes == []:
                                                    return

                                                # Filter logic (vectorized)
                                                keep_mask = np.ones(len(global_indices), dtype=bool)
                                                point_classes = classes[global_indices]
                                                
                                                if from_classes:
                                                    fc_arr = np.asarray(from_classes, dtype=classes.dtype)
                                                    keep_mask &= np.isin(point_classes, fc_arr)
                                                
                                                if visible_classes is not None:
                                                    vis_arr = np.asarray(visible_classes, dtype=classes.dtype)
                                                    keep_mask &= np.isin(point_classes, vis_arr)
                                                
                                                if not np.any(keep_mask):
                                                    return
                                                
                                                # Apply filters
                                                fresh_local = fresh_local[keep_mask]
                                                global_indices = global_indices[keep_mask]

                                                from gui.flight_line_filter import flight_line_visibility_mask
                                                line_mask = flight_line_visibility_mask(
                                                    self.app,
                                                    len(classes),
                                                    slot=self._active_flight_line_slot(),
                                                )
                                                line_keep = line_mask[global_indices]
                                                fresh_local = fresh_local[line_keep]
                                                global_indices = global_indices[line_keep]
                                                if len(global_indices) == 0:
                                                    return
                                                
                                                # 1. Unique and capture for undo
                                                fresh_local = np.unique(fresh_local)
                                                global_indices = self._brush_section_indices[fresh_local]
                                                
                                                if not hasattr(self, "_brush_indices_arrays") or self._brush_indices_arrays is None:
                                                    self._brush_indices_arrays = []
                                                    self._brush_old_classes_arrays = []
                                                
                                                self._brush_old_classes_arrays.append(classes[global_indices].copy())
                                                self._brush_indices_arrays.append(global_indices.copy())

                                                # 2. CPU classify immediately
                                                classes[global_indices] = to_class
                                                self._brush_section_local_mask[fresh_local] = True

                                self._last_brush_center_uv = (u, z)

                                # ── REAL-TIME GPU INJECTION (Phase 2) ──────────
                                if self._brush_section_local_mask is not None and self._brush_section_local_mask.any():
                                    v_idx = self._get_view_index_from_interactor()
                                    
                                    # ✅ Support Cut Section real-time poke
                                    if v_idx is None and is_cut_section:
                                        sw = self.app.cut_section_controller.cut_vtk
                                        actor = sw.actors.get("_cut_section_unified")
                                    elif v_idx is not None:
                                        actor_name = f"_section_{v_idx}_unified"
                                        sw = self.app.section_vtks.get(v_idx)
                                        actor = sw.actors.get(actor_name) if sw else None
                                    else:
                                        actor = None
                                        sw = None

                                    if actor and hasattr(actor, '_naksha_rgb_ptr'):
                                            # Only poke if it's been long enough since last poke
                                            now = time.time()
                                            if (now - getattr(self, '_last_sec_poke', 0)) > 0.033:
                                                rgb_ptr = actor._naksha_rgb_ptr
                                                if rgb_ptr.flags.writeable:
                                                    # Get color for target class
                                                    palette = self._get_active_palette()
                                                    entry = palette.get(int(to_class), {"color": (255, 255, 0)})
                                                    color = entry.get("color", (255, 255, 0)) if entry.get("show", True) else (0, 0, 0)
                                                    
                                                    # Get local indices that are set in the mask
                                                    local_hit = np.flatnonzero(self._brush_section_local_mask)
                                                    rgb_ptr[local_hit] = color
                                                    
                                                    # Update VTK
                                                    v_arr = getattr(actor, '_naksha_vtk_array', None)
                                                    if v_arr:
                                                        v_arr.Modified()
                                                    
                                                    # Render
                                                    self._safe_render_pyvista(sw)
                                                    self._last_sec_poke = now

                        except Exception as e:
                            print(f"⚠️ Section/Cut brush real-time update failed: {e}")

            # ✅ Polygon preview
            elif tool == "polygon":
                if is_cut_section:
                    self._draw_polygon_preview_cut((float(P2[0]), float(P2[1]), float(P2[2])))
                else:
                    self._draw_polygon_preview(self._get_view_coordinates(P2))
                if vtk_widget and (current_time - self._last_render_time) > preview_dt:
                    self._safe_render(vtk_widget)
                    self._last_render_time = current_time

            # ✅ Freehand preview
            elif tool == "freehand" and self.is_drawing_freehand:
                u, v = self._get_view_coordinates(P2)

                if self._is_main_view():
                    # Main path already sampled above before preview throttling.
                    pass
                elif len(self.drawing_points) == 0:
                    self.drawing_points.append((u, v))
                    self._last_freehand_display_pos = (dx, dy)
                else:
                    # Preserve established section/cut sampling behaviour.
                    last_disp = getattr(self, "_last_freehand_display_pos", (0.0, 0.0))
                    pixel_dist = np.sqrt((dx - last_disp[0])**2 + (dy - last_disp[1])**2)
                    if pixel_dist > 3.0:
                        self.drawing_points.append((u, v))
                        self._last_freehand_display_pos = (dx, dy)

                if is_cut_section:
                    x2, y2 = self.interactor.GetEventPosition()
                    if not hasattr(self, "drawing_points_display_cut"):
                        self.drawing_points_display_cut = []
                    if len(self.drawing_points_display_cut) == 0:
                        self.drawing_points_display_cut.append((x2, y2))
                    else:
                        last_x, last_y = self.drawing_points_display_cut[-1]
                        pixel_dist = np.sqrt((x2 - last_x) ** 2 + (y2 - last_y) ** 2)
                        if pixel_dist > 2.0:
                            self.drawing_points_display_cut.append((x2, y2))
                    self._draw_freehand_preview_cut()
                else:
                    self._draw_freehand_preview()

                if vtk_widget and (current_time - self._last_render_time) > preview_dt:
                    self._safe_render(vtk_widget)
                    self._last_render_time = current_time

            # ✅ SAFE PAN: Custom implementation — no VTK C++ Render()
            self._do_safe_pan()

    # ──────────────────────────────────────────────────────────────────────────
    # Task-3: BrushQueryWorker result slot (runs on main thread via Qt queued)
    # ──────────────────────────────────────────────────────────────────────────
    def _on_brush_worker_result(self, indices: object, to_class: int) -> None:
        """
        Receive KDTree query results from the background worker.

        Called on the MAIN thread (Qt auto-queued connection).
        Applies any newly found points that the synchronous grid may have missed.
        """
        try:
            hit = indices   # 1-D int64 ndarray
            if hit is None or len(hit) == 0:
                return

            if not self.is_dragging:
                return   # stroke already released — discard stale results

            if not hasattr(self, '_brush_accumulated_mask') or self._brush_accumulated_mask is None:
                return

            # Discard results from a previous stroke's worker (to_class mismatch)
            if to_class != getattr(self, '_brush_current_to_class', None):
                return

            classes = self.app.data.get("classification")
            if classes is None:
                return

            # Only process points not yet accumulated (idempotent)
            fresh = hit[~self._brush_accumulated_mask[hit]]
            if len(fresh) == 0:
                return

            from_classes = getattr(self.app, "from_classes", None)
            visible_classes = getattr(self, "_brush_visible_classes", None)
            visible_classes_arr = None

            if visible_classes == []:
                return

            if visible_classes is not None:
                visible_classes_arr = np.asarray(visible_classes, dtype=classes.dtype)

            if from_classes:
                fc_arr = np.asarray(from_classes, dtype=classes.dtype)
                if visible_classes_arr is not None:
                    effective_fc = fc_arr[np.isin(fc_arr, visible_classes_arr)]
                    if len(effective_fc) == 0:
                        return
                    fc_mask = np.isin(classes[fresh], effective_fc)
                else:
                    fc_mask = np.isin(classes[fresh], fc_arr)
                fresh = fresh[fc_mask]
                if len(fresh) == 0:
                    return
            elif visible_classes_arr is not None:
                vis_mask = np.isin(classes[fresh], visible_classes_arr)
                fresh = fresh[vis_mask]
                if len(fresh) == 0:
                    return

            from gui.flight_line_filter import filter_visible_flight_line_indices
            fresh = filter_visible_flight_line_indices(
                self.app, fresh, len(classes), slot=0
            )
            if len(fresh) == 0:
                return

            # Save old classes for undo (Unified array-based)
            old_cls = classes[fresh].copy()
            
            if not hasattr(self, "_brush_indices_arrays") or self._brush_indices_arrays is None:
                self._brush_indices_arrays = []
                self._brush_old_classes_arrays = []
            
            self._brush_indices_arrays.append(fresh.copy())
            self._brush_old_classes_arrays.append(old_cls)

            # CPU classify
            classes[fresh] = classes.dtype.type(to_class)
            self._brush_accumulated_mask[fresh] = True
            self._brush_frame_chunks.append(fresh)
            self._brush_needs_render = True

        except Exception as e:
            print(f"⚠️ _on_brush_worker_result: {e}")

    def on_left_press(self, obj, evt):
        """✅ Start drawing - sets P1 for preview rendering (FIXED for above/below line snapping)"""

        if not hasattr(self, 'app') or self.app is None:
            return
        if not hasattr(self, 'interactor') or self.interactor is None:
            return

        active_tool = getattr(self.app, "active_classify_tool", None)
        cut_controller = getattr(self.app, "cut_section_controller", None)
        cut_owns_left_click = bool(
            cut_controller is not None
            and hasattr(cut_controller, "owns_cross_section_left_click")
            and cut_controller.owns_cross_section_left_click()
        )
        # A held Pan shortcut may borrow left click only while classification
        # and cut placement are both idle.
        if (
            not cut_owns_left_click
            and getattr(self.app, "_left_pan_shortcut_active", False)
            and active_tool is None
        ):
            self._is_panning = True
            self._last_pan_pos = self.interactor.GetEventPosition()
            self._hide_preview_overlay_for_pan()
            try:
                if hasattr(obj, "AbortFlagOn"):
                    obj.AbortFlagOn()
                elif hasattr(obj, "SetAbortFlag"):
                    obj.SetAbortFlag(1)
            except Exception:
                pass
            return
        self._stop_deferred_left_release_watch()
        self._hide_preview_overlay_for_pan()

        self._check_tool_changed()
        tool = getattr(self.app, "active_classify_tool", None)
        if tool is not None:
            self._cancel_incomplete_classification_gestures_in_other_views()
        if tool in ("above_line", "below_line", "parallel_line"):
            self.app._classification_preview_active = True
        if getattr(self, "click_to_finalize", False) and tool in ("above_line", "below_line", "parallel_line"):
            # The first tap already locked P1. Resolve P2 from this second
            # press and finalize immediately; waiting for its release allowed
            # very fast tap-tap input to fall back into drag/release behavior.
            self._press_pos = self.interactor.GetEventPosition()
            self._last_release_time = 0.0
            self._last_render_time = 0.0
            self._last_mouse_move_time = 0.0
            try:
                dx, dy = self._get_display_point()
                p2 = self._display_to_world_fast(dx, dy)
                self._last_line_preview_P2 = p2

                # Paint the exact final line once before classification. This
                # makes a rapid second tap visually deterministic even when no
                # hover/mouse-move frame occurred between the two taps.
                vtk_widget = self._get_active_vtk_widget()
                is_cut_section = bool(
                    hasattr(self.app, "cut_section_controller")
                    and vtk_widget
                    == getattr(self.app.cut_section_controller, "cut_vtk", None)
                )
                if is_cut_section:
                    self._draw_line_preview_cut(self.P1, p2)
                else:
                    self._draw_temp_line(self.P1, p2)
                if vtk_widget is not None:
                    self._safe_render(vtk_widget)
            except Exception:
                # The tap finalizer has its own current-cursor fallback.
                pass

            self._do_line_classify_tap()
            try:
                if hasattr(obj, "AbortFlagOn"):
                    obj.AbortFlagOn()
                elif hasattr(obj, "SetAbortFlag"):
                    obj.SetAbortFlag(1)
            except Exception:
                pass
            return

        self._press_pos = self.interactor.GetEventPosition()
        self._line_press_max_move_px = 0.0
        self._line_press_time = time.time()

        # ════════════════════════════════════════════════════════════════
        # ✅ CRITICAL: Cancel any pending deferred rebuild immediately
        # This prevents lag when user starts a new classification quickly
        # ════════════════════════════════════════════════════════════════
        try:
            if hasattr(self.app, '_optimized_refresh') and self.app._optimized_refresh:
                optimizer = self.app._optimized_refresh
                # STOP TIMER instantly
                if hasattr(optimizer, '_deferred_rebuild_timer'):
                    optimizer._deferred_rebuild_timer.stop()
                
                # 🚀 OPTIMIZATION: Hide heavy SNT text during interaction
                self._suppress_snt_text(True)
                # We do NOT clear data, so we can resume later if needed
        except Exception:
            pass

        is_cut_interaction = False
        vtk_widget = self._get_active_vtk_widget()

        if hasattr(self.app, 'cut_section_controller'):
            cut_vtk = getattr(self.app.cut_section_controller, 'cut_vtk', None)
            if cut_vtk and vtk_widget == cut_vtk:
                is_cut_interaction = True

        if getattr(self.app, "active_mode", None) == "cut":
            if not is_cut_interaction:
                if hasattr(self.app, 'statusBar'):
                    self.app.statusBar().showMessage(
                        "⚠️ Cut Section is active - classifying in Main View",
                        2000
                    )
            else:
                print("🔧 Interaction detected inside Cut Section — Allowing classification.")

        if self._is_main_view():
            self.app.active_classify_target = "main"
        elif is_cut_interaction:
            self.app.active_classify_target = "cut"
        else:
            self.app.active_classify_target = "section"

        # When cross-section tool is active, a click in the section view navigates
        # the main view to the corresponding world XY (MicroStation-style locate).
        if not self._is_main_view() and not is_cut_interaction:
            if getattr(self.app, 'cross_section_active', False) and tool is None:
                sx, sy = self.interactor.GetEventPosition()
                self._navigate_main_to_section_click(sx, sy)
                return

        if not is_cut_interaction:
            v_idx = self._get_view_index_from_interactor()
            if v_idx is not None:
                try:
                    self.app.section_controller.active_view = v_idx
                    self.app.section_controller.current_vtk = self.app.section_vtks.get(v_idx)
                except Exception:
                    pass

        tool = getattr(self.app, "active_classify_tool", None)
        self._gesture_tool = tool

        if tool in ("above_line", "below_line", "parallel_line") and self._is_main_view():

            if hasattr(self.app, "statusBar"):
                self.app.statusBar().showMessage(
                    "❌ Line classification tools work only in cross-section views",
                    3000
                )

            # Let camera interaction continue, but STOP classification
            try:
                self.style.OnLeftButtonDown()
            except Exception:
                pass

            return

        if tool is None:
            try:
                return self.style.OnLeftButtonDown()
            except Exception:
                return

        # Abort C++ handler during classification dragging/drawing (unless in main view line snapping)
        if tool not in ("above_line", "below_line", "parallel_line") or not self._is_main_view():
            try:
                if hasattr(obj, "AbortFlagOn"):
                    obj.AbortFlagOn()
                elif hasattr(obj, "SetAbortFlag"):
                    obj.SetAbortFlag(1)
            except Exception:
                pass

        # ✅ IMPORTANT: clear stored preview end when starting a new above/below line
        if tool in ("above_line", "below_line", "parallel_line"):
            self._last_line_preview_P2 = None

        self.from_classes = getattr(self.app, "from_classes", None)
        self.to_class = getattr(self.app, "to_class", None)
        x, y = self.interactor.GetEventPosition()

        try:
            if tool in ("above_line", "below_line", "parallel_line") and (not self._is_main_view()):
                dx, dy = self._get_display_point()
                pt = self._display_to_world_fast(dx, dy)
            elif tool == "polygon":
                # FIX: _pick_world_point() uses VTK's cell/point picker which snaps
                # to the nearest rendered point cloud point — causing the magnetic effect.
                # _display_to_world_fast() does pure display→world math with no snapping,
                # matching what the live preview line already shows during mouse move.
                dx, dy = self._get_display_point()
                pt = self._display_to_world_fast(dx, dy)
            else:
                pt = self._pick_world_point(x, y)
        except Exception:
            return
        
        P = self._get_view_coordinates(pt)

        if tool == "rectangle" and is_cut_interaction:
            self.P1_display_cut = (x, y)

        if tool == "polygon":
            self.drawing_points.append(P)
            if is_cut_interaction:
                if not hasattr(self, "drawing_points_display_cut"):
                    self.drawing_points_display_cut = []
                if not hasattr(self, "drawing_points_world_cut"):
                    self.drawing_points_world_cut = []
                self.drawing_points_display_cut.append((float(x), float(y)))
                self.drawing_points_world_cut.append((float(pt[0]), float(pt[1]), float(pt[2])))
                self._draw_polygon_preview_cut()
            else:
                self._draw_polygon_preview()
            self._safe_render_interactor()
            print(f"{'='*60}\n")
            return

        if tool == "freehand":
            self.is_drawing_freehand = True
            self.drawing_points = []
            self._freehand_native_samples = 0
            if is_cut_interaction:
                self.drawing_points_display_cut = [(x, y)]
            try:
                # ✅ FIX: Use non-snapping projection for freehand start
                pt2 = self._display_to_world_fast(x, y)
                P2 = self._get_view_coordinates(pt2)
                self.drawing_points.append(P2)
                self._last_freehand_display_pos = (float(x), float(y))
            except Exception:
                pass
            print(f"{'='*60}\n")
            return

        # self.P1 = pt
        # self.app._suppress_section_refresh = True
        # self.is_dragging = True
        # # ❌ REMOVED: self.app._suppress_section_refresh = True
        # # ✅ This flag should ONLY be set for brush tool, not for rectangle/other tools
        self.P1 = pt
        self.app._suppress_section_refresh = True
        self.is_dragging = True
        # Clear release debounce so first-tap release of this new gesture is never blocked
        # by the previous classification's release timestamp.
        self._last_release_time = 0.0
        # ✅ FIX B — reset throttles so first preview frame of this
        # new classification is never skipped by the 16 ms guard.
        self._last_render_time     = 0.0
        self._last_mouse_move_time = 0.0

        # ✅ MicroStation tap-tap AND drag, combined: whether this gesture
        # becomes a tap (arm click_to_finalize, wait for the confirming
        # second tap) or a drag (classify immediately on release using
        # press→release as the line) is now decided in on_left_release by
        # how far the mouse actually moved — not forced here. See the
        # "LINE TAP-VS-DRAG" block at the top of on_left_release.
        # ❌ REMOVED: self.app._suppress_section_refresh = Tru

        # ⚡ Fix 11 — Cancel any pending deferred main-view render and enter
        # streak mode immediately on press. Without this, a render scheduled
        # by the PREVIOUS classify's request_render could fire mid-drag
        # (~100 ms after previous release) and synchronously block the
        # current drag for 20-30 ms on the 39 M-point main view. That race
        # was the source of the "sometimes a classify is slow" pattern:
        # most clicks fall outside the bad timing window, but ones that
        # happen ~100-300 ms after the previous release hit the firing
        # timer right when the user starts dragging.
        try:
            self.app._last_classify_ts = time.time()
            _mgr = getattr(self.app, "gpu_render_manager", None)
            if _mgr is not None:
                _timer = getattr(_mgr, "render_timer", None)
                if _timer is not None:
                    _timer.stop()
                _mgr.pending_render = False
        except Exception:
            pass

        # ✅ MAIN VIEW brush init
        if tool == "brush" and self._is_main_view():
            xyz_now = self.app.data.get("xyz")
            index_stale = (
                getattr(self, "_main_grid_index", None) is None
                or getattr(self, '_brush_spatial_index_xyz_id', None) != id(xyz_now)
            )
            if index_stale:
                # Wait for background pre-warm (normally a near-zero tail-wait).
                # Timeout guards against a hung thread — after 10 s we fall through
                # and let the synchronous build run instead (with its own exception guard).
                _pt = getattr(self, '_prebuild_thread', None)
                if _pt is not None and _pt.is_alive():
                    _pt.join(timeout=10.0)
                try:
                    self._build_spatial_index_for_brush()
                except Exception as _e:
                    print(f"⚠️ Brush spatial index build failed: {_e}")

            import numpy as np
            self._brush_accumulated_mask = np.zeros(len(self.app.data["xyz"]), dtype=bool)
            self._brush_old_classes = {} # Legacy - keep for compat if needed elsewhere
            self._brush_old_classes_arrays = []
            self._brush_indices_arrays = []
            self._brush_frame_chunks = []
            self._brush_stroke_positions = []
            self._last_brush_center = None
            self._brush_render_counter = 0
            self._brush_last_render_time = 0.0
            self._brush_needs_render = False
            self._brush_visible_classes = self._get_visible_classes_for_slot(0)
            # Track current stroke's target class so _on_brush_worker_result can
            # discard stale results from a previous stroke's still-running worker.
            self._brush_current_to_class = getattr(self.app, "to_class", None)
            # ✅ Only set suppress flag for BRUSH tool
            self.app._suppress_section_refresh = True

            # Create lightweight overlay actor for real-time paint preview during drag.
            # The overlay avoids the 30 MB vtk_ca.Modified() GPU upload on every tick.
            try:
                _tc_init  = self._brush_current_to_class
                if _tc_init is not None:
                    _pal_init = self._get_active_palette()
                    _e_init   = _pal_init.get(int(_tc_init), {})
                    _rgb_init = (_e_init.get('color', (128, 128, 128))
                                 if _e_init.get('show', True) else (128, 128, 128))
                    self._create_brush_paint_overlay(_rgb_init)
                else:
                    self._brush_overlay_actor = None
            except Exception:
                self._brush_overlay_actor = None

            # ── Task-3: start background KDTree worker (if not already running) ──
            if not getattr(self, "_brush_worker_active", False):
                try:
                    from gui.brush_worker import BrushQueryWorker
                    from PySide6.QtCore import Qt
                    w = BrushQueryWorker(parent=None)
                    w.result_ready.connect(
                        self._on_brush_worker_result, Qt.QueuedConnection
                    )
                    w.start()
                    self._brush_worker = w
                    self._brush_worker_active = True
                except Exception as _bwe:
                    self._brush_worker = None
                    self._brush_worker_active = False

            # Disable ROI point overlay during brush drag; keep only the cursor.
            self._roi_preview = None
                

        if tool == "brush" and (not self._is_main_view()):
            # ✅ Only set suppress flag for BRUSH tool
            self.app._suppress_section_refresh = True
            
            try:
                import numpy as np
    
                self._brush_section_indices = None
                self._brush_section_pts2d = None
                self._brush_section_local_mask = None
                self._last_brush_center_uv = None

                if is_cut_interaction:
                    ctrl = getattr(self.app, "cut_section_controller", None)
                    if ctrl is not None:
                        cut_pts = getattr(ctrl, "cut_points", None)
                        cut_idx = getattr(ctrl, "_cut_index_map", None)
                        if cut_pts is not None and cut_idx is not None:
                            from ..classification_tools import _project_to_cut_view
                            idxs = np.asarray(cut_idx, dtype=np.int64)
                            pts2d = _project_to_cut_view(self.app, cut_pts)
                            self._brush_section_indices   = idxs
                            self._brush_section_pts2d     = np.asarray(pts2d, dtype=np.float64)
                            self._brush_section_local_mask = np.zeros(len(idxs), dtype=bool)
                            
                            # ✅ GRID INIT (Cut Section)
                            self._brush_section_grid = SpatialGridIndex(self._brush_section_pts2d)
                            # Cut section is typically Slot 5 in this app
                            self._brush_visible_classes = self._get_visible_classes_for_slot(5)
                            self._brush_indices_arrays = []
                            self._brush_old_classes_arrays = []
                else:
                    # Cross-section brush init.
                    # CRITICAL: idxs and pts2d MUST be in the same order.
                    # _section_{v}_global_indices and section_{v}_points_transformed
                    # are both stored in core-first order by build_section_unified_actor.
                    # Using flatnonzero(combined_mask) for idxs but points_transformed for
                    # pts3 is WRONG — flatnonzero gives scan-order, points_transformed is
                    # core-first. They do not correspond element-by-element.
                    v_idx = self._get_view_index_from_interactor()
                    if v_idx is None:
                        v_idx = getattr(self.app.section_controller, "active_view", 0)

                    # PRIMARY: use pre-built aligned arrays (both core-first order)
                    idxs = getattr(self.app, f"_section_{v_idx}_global_indices", None)
                    pts3 = getattr(self.app, f"section_{v_idx}_points_transformed", None)

                    if idxs is not None and pts3 is not None and len(idxs) == len(pts3):
                        # Aligned: index i of idxs ↔ index i of pts3
                        pass
                    else:
                        # FALLBACK: build core-first order manually from masks
                        core_mask   = getattr(self.app, f"section_{v_idx}_core_mask",   None)
                        buffer_mask = getattr(self.app, f"section_{v_idx}_buffer_mask", None)
                        if core_mask is not None:
                            core_idx = np.flatnonzero(core_mask)
                            if buffer_mask is not None:
                                buf_idx = np.flatnonzero(buffer_mask & ~core_mask)
                                idxs = np.concatenate([core_idx, buf_idx])
                            else:
                                idxs = core_idx
                            pts3 = self.app.data["xyz"][idxs]   # aligned with idxs
                        else:
                            idxs = None
                            pts3 = None

                    if pts3 is not None and idxs is not None:
                        view_mode = getattr(self.app, "cross_view_mode", "side")
                        pts2d = pts3[:, [1, 2]] if view_mode == "front" else pts3[:, [0, 2]]
                        self._brush_section_indices   = np.asarray(idxs, dtype=np.int64)
                        self._brush_section_pts2d     = np.asarray(pts2d, dtype=np.float64)
                        self._brush_section_local_mask = np.zeros(len(idxs), dtype=bool)
                        
                        # ✅ GRID INIT: Build 2D spatial index for instant brush
                        self._brush_section_grid = SpatialGridIndex(self._brush_section_pts2d)
                        
                        # ✅ UNDO & VISIBILITY INIT (Section)
                        v_idx = self._get_view_index_from_interactor()
                        if v_idx is None:
                            v_idx = getattr(self.app.section_controller, "active_view", 0)
                        
                        self._brush_visible_classes = self._get_visible_classes_for_slot(v_idx + 1)
                        self._brush_indices_arrays = []
                        self._brush_old_classes_arrays = []
    
            except Exception as e:
                print(f"⚠️ Section brush init skipped (safe): {e}")
                    
    def on_left_release(self, obj, evt):
        if getattr(self, "_is_panning", False):
            self._is_panning = False
            self._hide_preview_overlay_for_pan()
            try:
                if hasattr(obj, "AbortFlagOn"):
                    obj.AbortFlagOn()
                elif hasattr(obj, "SetAbortFlag"):
                    obj.SetAbortFlag(1)
            except Exception:
                pass
            return

        """
        Execute classification on mouse release
        ✅ Includes MAIN-VIEW path + fixed cross/cut section path
        ✅ FIXED: Rectangle/Circle use EXACT same P2 as preview (no coordinate mismatch)
        
        ✅ SENIOR REFACTOR:
        - Brush tool accumulates strokes in memory and flushes ONCE on release.
        - Unified coordinate projection for Cut Section vs Cross-Section views.
        - Debounced and state-safe cleanup.
        """
        # 1. Safety checks
        if not hasattr(self, 'app') or self.app is None:
            return

        # 🚀 OPTIMIZATION: Restore suppressed text
        self._suppress_snt_text(False)

        if not hasattr(self, 'style') or self.style is None:
            try:
                if hasattr(self, 'style'):
                    self.style.OnLeftButtonUp()
            except Exception:
                pass
            return

        if getattr(self, "_suppress_release_observer", False):
            return

        if getattr(self, "_ignore_next_left_release", False):
            self._ignore_next_left_release = False
            self._last_release_time = 0.0
            return

        # 2. Stray-release guard for line tools: if the system fires
        # LeftButtonReleaseEvent AFTER on_left_press already handled finalization
        # (P1 reset, click_to_finalize cleared), skip early so the debounce
        # timer is not polluted for the next interaction.
        _tool_quick = getattr(self.app, "active_classify_tool", None)
        if (_tool_quick in ("above_line", "below_line", "parallel_line") and
                self.P1 is None and
                not getattr(self, 'click_to_finalize', False) and
                not getattr(self, 'is_dragging', False)):
            return

        # ✅ LINE TAP-VS-DRAG (MicroStation-style, combined): a first release
        # for above/below/parallel-line hasn't been classified as tap or drag
        # yet at this point (is_dragging=True from press, click_to_finalize
        # still False). Decide now using the MAXIMUM distance moved from the
        # press point at any point during the gesture (tracked continuously
        # in _on_mouse_move_with_preview) — not just the net press→release
        # displacement. MicroStation's engine commits to DRAGGING the instant
        # the threshold is crossed and stays committed even if the cursor
        # drifts back near the origin before release; using only the final
        # displacement would wrongly reclassify a "dragged out and back"
        # gesture as a tap.
        #   - never exceeded threshold → TAP. Arm click_to_finalize and
        #     return without classifying; the confirming second tap's PRESS
        #     (handled at the top of on_left_press) finalizes exactly as
        #     before — tap-tap behavior is completely unaffected.
        #   - threshold exceeded       → DRAG. Fall through to the existing
        #     Branch D drag-classify path below (untouched code that already
        #     handles above_line/below_line/parallel_line correctly using
        #     self.P1 as the press point and P2 resolved just below), so it
        #     classifies immediately on release, MicroStation-style.
        # A release that lands inside the initial settle window
        # (_LINE_TAP_GRACE_SEC) is always a tap — a real fast click's own
        # press→release displacement can otherwise exceed the pixel
        # threshold purely from hand jitter, well before any deliberate drag
        # motion is possible. _on_mouse_move_with_preview applies the same
        # grace window to the accumulated max-distance tracking, so both
        # sides of this check stay consistent.
        if (_tool_quick in ("above_line", "below_line", "parallel_line") and
                getattr(self, 'is_dragging', False) and
                not getattr(self, 'click_to_finalize', False) and
                self.P1 is not None):
            held_sec = time.time() - getattr(self, '_line_press_time', 0.0)
            if held_sec < _LINE_TAP_GRACE_SEC:
                moved_px = 0.0
            else:
                try:
                    rx, ry = self.interactor.GetEventPosition()
                    press_pos = getattr(self, '_press_pos', None) or (rx, ry)
                    release_moved_px = ((rx - press_pos[0]) ** 2 + (ry - press_pos[1]) ** 2) ** 0.5
                    moved_px = max(release_moved_px, getattr(self, '_line_press_max_move_px', 0.0))
                except Exception:
                    moved_px = 0.0
            if moved_px < _LINE_TAP_DRAG_THRESHOLD_PX:
                self.click_to_finalize = True
                return
            # else: genuine drag — continue past this block into the normal
            # release-classify path further down, unchanged.

        # ✅ MicroStation tap-tap only: if a stray release arrives while
        # click_to_finalize is still armed (waiting on the confirming second
        # tap), it must never run the drag-classify path — it would
        # double-classify or misfire.
        if (_tool_quick in ("above_line", "below_line", "parallel_line") and
                getattr(self, 'click_to_finalize', False)):
            return

        # 3. Debounce (20ms) — guards against accidental double-fires from a
        # single physical click, but lets a fast user fire 30+ classifications
        # per second (Microstation cadence). Raised back to 100ms = silently
        # dropped clicks during streaks.
        current_time = time.time()
        if hasattr(self, '_last_release_time'):
            if current_time - self._last_release_time < 0.02:
                return
        self._last_release_time = current_time

        self._check_tool_changed()
        tool = getattr(self.app, "active_classify_tool", None)

        from ..classification_tools import (
            classify_above_line, classify_below_line,
            classify_parallel_line_band,
            classify_rectangle, classify_circle, classify_polygon,
            classify_freehand, classify_brush, classify_point,
            _get_cut_section_or_default, _get_visible_mask_from_viewport,
            _apply_classification
        )

        # Polygon tool is vertex-by-vertex (left clicks add vertices),
        # so left-button release must not flush state between clicks.
        if getattr(self.app, "active_classify_tool", None) == "polygon":
            try:
                self.style.OnLeftButtonUp()
            except Exception:
                pass
            return

        # Abort C++ release handler if classification tool is active
        if getattr(self.app, "active_classify_tool", None) is not None:
            try:
                if hasattr(obj, "AbortFlagOn"):
                    obj.AbortFlagOn()
                elif hasattr(obj, "SetAbortFlag"):
                    obj.SetAbortFlag(1)
            except Exception:
                pass

        try:
            tool = getattr(self.app, "active_classify_tool", None)
            was_dragging = self.is_dragging
            to_class = getattr(self.app, "to_class", None)
            from_classes = getattr(self.app, "from_classes", None)

            # ⚡ Streak stamp — single point covering EVERY classify path that
            # comes through this release handler (brush, above/below line,
            # rectangle, circle, polygon, freehand, point — in both main and
            # cross-section/cut views). GPURenderManager.request_render reads
            # this timestamp to coalesce main-view renders within a 1 s window
            # so the 39 M-point main view does not block the click→release
            # cycle. Branches that already stamp inside _apply_classification
            # / _apply_mask_and_record are idempotent — re-stamping here just
            # keeps the window fresh. Brush branches (A & B) bypass those
            # helpers entirely, so this is the ONLY stamp they get.
            if tool and (was_dragging or tool in ("polygon", "freehand")):
                self.app._last_classify_ts = current_time

            # --- BRANCH A: MAIN VIEW BRUSH STROKE ---
            if tool == "brush" and self._is_main_view() and was_dragging:
                undo_mask = None  # Will be set if points were classified
                if hasattr(self, '_brush_accumulated_mask') and np.any(self._brush_accumulated_mask):
                    start = time.time()

                    # ✅ MICROSTATION: Classification was already applied during drag.
                    # Only push the undo entry here (no re-classification needed).
                    # ✅ Lightning-fast array-based merge for undo
                    if hasattr(self, '_brush_indices_arrays') and self._brush_indices_arrays:
                        indices = np.concatenate(self._brush_indices_arrays)
                        old_cls = np.concatenate(self._brush_old_classes_arrays)
                        
                        # ✅ CRITICAL FIX: Ensure uniqueness to avoid undo failure
                        # we MUST have exactly one entry in the mask per entry in old_cls.
                        # We use the FIRST occurrence to keep the original class.
                        if len(indices) > 0:
                            _, first_idx = np.unique(indices, return_index=True)
                            indices = indices[first_idx]
                            old_cls = old_cls[first_idx]

                        new_cls = np.full(len(indices), to_class, dtype=old_cls.dtype)

                        undo_mask = np.zeros(len(self.app.data["xyz"]), dtype=bool)
                        undo_mask[indices] = True

                        self.app.undo_stack.append({
                            "indices": indices.copy(),
                            "old_classes": old_cls,
                            "new_classes": new_cls,
                        })
                        self.app.redo_stack.clear()
                        self.app._last_changed_mask = undo_mask
                        self.app._last_changed_indices = indices.copy()

                        # Point stats
                        try:
                            from gui.point_count_widget import refresh_point_statistics
                            refresh_point_statistics(self.app)
                        except Exception:
                            pass

                    _count = len(indices) if 'indices' in locals() else 0
                    elapsed = (time.time() - start) * 1000
                    print(f"✅ Main Brush stroke complete: {_count:,} points in {elapsed:.0f}ms")

                # ── Save mask BEFORE nulling — needed for shading refresh below ──
                _final_mask = self._brush_accumulated_mask

                # Remove paint overlay before the corrective render fires.
                # guarantee_main_view_visual_refresh (below) will call vtk_ca.Modified()
                # and do the one full GPU upload that shows all painted colours.
                self._destroy_brush_paint_overlay()

                # Clean up brush state
                self._brush_accumulated_mask = None
                self._brush_old_classes = {}
                self._brush_old_classes_arrays = []
                self._brush_indices_arrays = []
                self._brush_frame_chunks = []
                self._brush_stroke_positions = []
                self._last_brush_center = None
                self._brush_needs_render = False
                self._brush_visible_classes = None
                self.app._suppress_section_refresh = False

                # ── Task-2: destroy ROI preview actor ────────────────────────────
                try:
                    roi = getattr(self, "_roi_preview", None)
                    if roi is not None:
                        roi.destroy()
                        self._roi_preview = None
                except Exception:
                    pass

                # ── Task-3: worker stays alive across strokes to avoid QThread
                # teardown race (destroying the Python wrapper while the C++ thread
                # is still mid-query on a 13M-point KDTree → segfault).
                # The worker is stopped only in cleanup() when the tool is deactivated.
                # Between strokes the queue is drained; stale results are discarded by
                # the `if not self.is_dragging: return` guard in _on_brush_worker_result.
                # ─────────────────────────────────────────────────────────────────────

                # In shaded_class mode keep OptimizedRefresh alive — shading mesh needs its own rebuild
                if getattr(self.app, "display_mode", "class") != "shaded_class":
                    self.app._gpu_sync_done = True
                    self.app._section_visibility_refresh_required = False
                    try:
                        from gui.unified_actor_manager import guarantee_main_view_visual_refresh
                        guarantee_main_view_visual_refresh(
                            self.app,
                            changed_mask=_final_mask,
                            to_class=to_class,
                            reason="brush_release_finalize",
                        )
                    except Exception:
                        pass
                elif _shading_mesh_exists(self.app):
                    try:
                        from gui.shading_display import refresh_shaded_after_classification_fast
                        refresh_shaded_after_classification_fast(
                            self.app,
                            changed_mask=_final_mask   # use saved mask, not the now-None attribute
                        )
                    except Exception as _se:
                        print(f"⚠️ Brush release shading refresh: {_se}")
                else:
                    # No mesh yet — treat like class mode so GPU sync completes
                    self.app._gpu_sync_done = True
                    self.app._section_visibility_refresh_required = False
                    try:
                        from gui.unified_actor_manager import guarantee_main_view_visual_refresh
                        guarantee_main_view_visual_refresh(
                            self.app,
                            changed_mask=_final_mask,
                            to_class=to_class,
                            reason="brush_release_fallback",
                        )
                    except Exception:
                        pass
                    print("⏭️ Brush release: skipping shading refresh — no mesh built yet")

                 # ✅ Final: sync sections (deferred from drag) + render all
                try:
                    # Update cross-sections with the full undo_mask
                    if hasattr(self.app, 'section_vtks') and undo_mask is not None:
                        from gui.unified_actor_manager import fast_cross_section_update, build_section_unified_actor
                        for vi in self.app.section_vtks:
                            try:
                                slot_idx = vi + 1
                                dialog = (
                                    getattr(self.app, "display_mode_dialog", None)
                                    or getattr(self.app, "display_dialog", None)
                                )
                                view_palette = {}
                                if dialog is not None and hasattr(dialog, "view_palettes"):
                                    view_palette = dialog.view_palettes.get(slot_idx) or {}
                                if not view_palette and hasattr(self.app, "view_palettes"):
                                    view_palette = self.app.view_palettes.get(slot_idx) or {}

                                ok = fast_cross_section_update(
                                    self.app,
                                    vi,
                                    undo_mask,
                                    palette=view_palette,
                                    force_visibility_refresh=False,
                                )
                                if not ok:
                                    border = float(getattr(self.app, "view_borders", {}).get(slot_idx, 0) or 0.0)
                                    view_mode = getattr(self.app, "cross_view_mode", "front")
                                    rebuilt = build_section_unified_actor(
                                        self.app,
                                        vi,
                                        palette=view_palette,
                                        border_percent=border,
                                        view=view_mode,
                                    )
                                    if rebuilt is not None:
                                        fast_cross_section_update(
                                            self.app,
                                            vi,
                                            undo_mask,
                                            palette=view_palette,
                                            force_visibility_refresh=False,
                                        )
                            except Exception:
                                pass

                    mode_for_main_render = str(getattr(self.app, "display_mode", "class") or "class").lower()
                    # Guard: do not re-render slot-0 in non-class modes after brush finalize.
                    if mode_for_main_render in ("class", "shaded_class"):
                        self._safe_render_pyvista(self.app.vtk_widget)
                    if hasattr(self.app, 'section_vtks'):
                        for sw in self.app.section_vtks.values():
                            if sw:
                                self._safe_render_pyvista(sw)
                except Exception:
                    pass

                # ═══════════════════════════════════════════════════════════
                # ✅ FIX: Refresh cut section view after main view brush
                # 
                # Branch A returns early (before _refresh_all_views_after_classification),
                # so cross-sections get updated via fast_cross_section_update() above,
                # but the cut section was NEVER refreshed. This fixes that gap.
                # ═══════════════════════════════════════════════════════════
                try:
                    if hasattr(self.app, 'cut_section_controller'):
                        ctrl = self.app.cut_section_controller
                        if (ctrl.is_cut_view_active 
                                and ctrl.cut_points is not None 
                                and ctrl._cut_index_map is not None
                                and ctrl.cut_vtk is not None):
                            ctrl._refresh_cut_colors_fast()
                            print(f"   ⚡ Cut section refreshed after main view brush")
                except Exception as e:
                    print(f"   ⚠️ Cut section refresh failed: {e}")

                self.is_dragging = False
                try:
                    self.style.OnLeftButtonUp()
                except Exception:
                    pass
                return

            # 3. Basic Validation
            if (not was_dragging) and (tool not in ("freehand", "polygon")):
                print("⚠️ Release without drag - ignoring")
                try:
                    self.style.OnLeftButtonUp()
                except Exception:
                    pass
                return

            if self.P1 is None and tool not in ("freehand", "polygon"):
                print("⚠️ P1 is None, cannot execute classification")
                return

            x, y = self.interactor.GetEventPosition()
            
            # 4. Resolve P2 (Endpoint) - FIXED TO MATCH PREVIEW
            try:
                if tool in ("above_line", "below_line", "parallel_line") and (not self._is_main_view()):
                    # Use stored preview P2 for line tools
                    P2 = getattr(self, "_last_line_preview_P2", None)
                    if P2 is None:
                        dx, dy = self._get_display_point()
                        P2 = self._display_to_world_fast(dx, dy)
                elif tool == "rectangle" and (not self._is_main_view()):
                    # ✅ NEW: Use stored preview P2 for rectangle tool
                    P2 = getattr(self, "_last_rectangle_preview_P2", None)
                    if P2 is None:
                        dx, dy = self._get_display_point()
                        P2 = self._display_to_world_fast(dx, dy)
                    print(f"🔍 Rectangle using preview P2: {P2}")
                elif tool == "circle" and (not self._is_main_view()):
                    # ✅ NEW: Use stored preview P2 for circle tool
                    P2 = getattr(self, "_last_circle_preview_P2", None)
                    if P2 is None:
                        dx, dy = self._get_display_point()
                        P2 = self._display_to_world_fast(dx, dy)
                else:
                    # Main view or point tool can use snapping
                    P2 = self._pick_world_point(x, y)
            except Exception as e:
                print(f"⚠️ Pick failed on release: {e}")
                return

            # 5. Detect View Context
            vtk_widget = self._get_active_vtk_widget()
            is_cut_section = bool(
                hasattr(self.app, 'cut_section_controller') and
                vtk_widget == getattr(self.app.cut_section_controller, 'cut_vtk', None)
            )

            # --- BRANCH B: SECTION / CUT VIEW BRUSH STROKE ---
            if tool == "brush" and (not self._is_main_view()) and was_dragging:
                try:
                    idxs = getattr(self, "_brush_section_indices", None)
                    pts2d = getattr(self, "_brush_section_pts2d", None)
                    local_mask = getattr(self, "_brush_section_local_mask", None)

                    if idxs is not None and pts2d is not None and local_mask is not None:
                        idxs_arr = np.asarray(idxs)
                        if idxs_arr.dtype == bool:
                            idxs_arr = np.flatnonzero(idxs_arr)
                        idxs_arr = idxs_arr.astype(np.int64, copy=False)

                        # Final stamp at release position to close the gap
                        radius_px = float(getattr(self.app, "brush_preview_px", 20.0))
                        radius_view = float(self._pixel_radius_to_view_radius(vtk_widget, P2, radius_px))
                        u2, z2 = self._get_view_coordinates(P2)
                        
                        grid = getattr(self, "_brush_section_grid", None)
                        if grid and grid.valid:
                            if getattr(self.app, "brush_shape", "circle") == "rectangle":
                                hit = grid.query_rectangle(float(u2), float(z2), radius_view)
                            else:
                                hit = grid.query_radius(float(u2), float(z2), radius_view)
                            if len(hit) > 0:
                                local_mask[hit] = True
                        else:
                            # Fallback O(N)
                            du, dz = pts2d[:, 0] - float(u2), pts2d[:, 1] - float(z2)
                            if getattr(self.app, "brush_shape", "circle") == "rectangle":
                                local_mask |= (np.abs(du) <= radius_view) & (np.abs(dz) <= radius_view)
                            else:
                                local_mask |= (du*du + dz*dz) <= (radius_view * radius_view)

                        #     update_mask[idxs_arr[local_mask]] = True

                        # self._refresh_all_views_after_classification(to_class)

                        # ✅ Lightning-fast array-based merge for undo (matches Main View logic)
                        if hasattr(self, '_brush_indices_arrays') and self._brush_indices_arrays:
                            indices = np.concatenate(self._brush_indices_arrays)
                            old_cls = np.concatenate(self._brush_old_classes_arrays)
                            
                            # ✅ CRITICAL FIX: Ensure uniqueness to avoid undo failure
                            if len(indices) > 0:
                                _, first_idx = np.unique(indices, return_index=True)
                                indices = indices[first_idx]
                                old_cls = old_cls[first_idx]

                            new_cls = np.full(len(indices), to_class, dtype=old_cls.dtype)

                            undo_mask = np.zeros(len(self.app.data["xyz"]), dtype=bool)
                            undo_mask[indices] = True

                            self.app.undo_stack.append({
                                "indices": indices.copy(),
                                "old_classes": old_cls,
                                "new_classes": new_cls,
                            })
                            self.app.redo_stack.clear()
                            self.app._last_changed_mask = undo_mask
                            self.app._last_changed_indices = indices.copy()

                            # Point stats
                            try:
                                from gui.point_count_widget import refresh_point_statistics
                                refresh_point_statistics(self.app)
                            except Exception:
                                pass

                            if hasattr(self.app, 'statusBar'):
                                self.app.statusBar().showMessage(
                                    f"✅ {len(indices):,} points → class {to_class}", 3000)

                        self._refresh_all_views_after_classification(to_class)
                        
                        # ✅ CLEANUP SECTION BRUSH STATE
                        self._brush_section_local_mask = None
                        self._brush_old_classes_arrays = []
                        self._brush_indices_arrays = []
                        self._last_brush_center_uv = None
                        return
                    
                except Exception as e:
                    print(f"⚠️ Section brush failed (fallback): {e}")

            # --- BRANCH C: MAIN VIEW (NON-BRUSH) ---
            if self._is_main_view():
                if tool == "rectangle":
                    u1, v1 = self._get_view_coordinates(self.P1)
                    u2, v2 = self._get_view_coordinates(P2)

                    # Clear preview BEFORE classification.
                    # In Surface mode, classification triggers Surface rebuild immediately.
                    # If preview is still visible during rebuild, Surface overlay restore can revive it.
                    self._clear_all_previews()

                    self._classify_rectangle_main((u1, v1), (u2, v2), to_class)

                elif tool == "circle":
                    u1, v1 = self._get_view_coordinates(self.P1)
                    u2, v2 = self._get_view_coordinates(P2)
                    center = ((u1 + u2) / 2.0, (v1 + v2) / 2.0)
                    radius = float(np.hypot(u2 - u1, v2 - v1)) / 2.0

                    # Clear preview BEFORE classification for Surface mode safety.
                    self._clear_all_previews()

                    self._classify_circle_main(center, radius, to_class)

                elif tool == "freehand" and len(self.drawing_points) > 2:
                    # Same corner-safe curve as MAIN preview so geometry matches.
                    _smooth_fh = self._centripetal_catmull_rom_smooth(
                        self.drawing_points, steps=4
                    )
                    if getattr(self.app, "_debug_perf", False):
                        print(
                            "FREEHAND_MAIN_PROFILE "
                            f"native_samples={getattr(self, '_freehand_native_samples', 0)} "
                            f"control_points={len(self.drawing_points)} "
                            f"curve_points={len(_smooth_fh)} "
                            f"preview_interval_ms={self._preview_interval_sec() * 1000.0:.1f} "
                            "smoother=centripetal corner_guard=on"
                        )

                    # Clear preview BEFORE classification for Surface mode safety.
                    self._clear_all_previews()

                    self._classify_polygon_main(_smooth_fh, to_class)

                elif tool == "point":
                    u2, v2 = self._get_view_coordinates(P2)

                    # Clear preview BEFORE classification for Surface mode safety.
                    self._clear_all_previews()

                    self._classify_point_main((u2, v2), getattr(self.app, "point_radius", 0.5), to_class)

                else:
                    self._clear_all_previews()

                return

            # --- BRANCH D: SECTION / CUT VIEW (NON-BRUSH) ---
            # Resolve data context
            #     mask, section_points = self._get_section_context_for_view(v_idx) if v_idx is not None else (None, None)

            # section_indices, section_points = _get_cut_section_or_default(self.app, mask, section_points)

            if is_cut_section:
                mask = getattr(self.app.section_controller, "last_mask", None)
                section_points = getattr(self.app, "section_points", None)
            else:
                v_idx = self._get_view_index_from_interactor()
                if v_idx is not None:
                    combined_mask = getattr(self.app, f"section_{v_idx}_combined_mask", None)
                    section_points_transformed = getattr(self.app, f"section_{v_idx}_points_transformed", None)
                    if combined_mask is not None and section_points_transformed is not None:
                        mask = combined_mask
                        section_points = section_points_transformed
                    else:
                        mask, section_points = self._get_section_context_for_view(v_idx)
                else:
                    mask, section_points = None, None

            section_indices, section_points = _get_cut_section_or_default(self.app, mask, section_points)
            if section_indices is None or section_points is None:
                return

            # Special case for Freehand completion
            if tool == "freehand" and self.is_drawing_freehand:
                if len(self.drawing_points) >= 2:
                    # ── SMOOTH: use Catmull-Rom polygon so the classified area
                    # matches the smooth curved preview exactly. ────────────────
                    smooth_pts = self._catmull_rom_smooth(self.drawing_points, steps=8)
                    classify_freehand(self.app, smooth_pts, from_classes, to_class, mask, section_points, None)
                    self._refresh_all_views_after_classification(to_class)
                return

            # Geometric Tools (Line, Rect, Circle)
            if is_cut_section:
                from ..classification_tools import _project_to_cut_view
                (u1, z1), (u2, z2) = _project_to_cut_view(self.app, np.vstack([self.P1, P2]))
            else:
                u1, z1 = self._get_view_coordinates(self.P1)
                u2, z2 = self._get_view_coordinates(P2)

            if tool == "above_line":
                classify_above_line(self.app, [(u1, z1), (u2, z2)], from_classes, to_class, mask, section_points, None)
            elif tool == "below_line":
                classify_below_line(self.app, [(u1, z1), (u2, z2)], from_classes, to_class, mask, section_points, None)
            elif tool == "parallel_line":
                classify_parallel_line_band(self.app, [(u1, z1), (u2, z2)], from_classes, to_class, mask, section_points, None)
            elif tool == "rectangle":
                u_range = sorted([u1, u2])
                z_range = sorted([z1, z2])
                classify_rectangle(self.app, (u_range[0], u_range[1], z_range[0], z_range[1]), from_classes, to_class, mask, section_points, "disable_filter")
            elif tool == "circle":
                center = ((u1 + u2) / 2, (z1 + z2) / 2)
                radius = float(np.hypot(u2 - u1, z2 - z1)) / 2.0
                classify_circle(self.app, center, radius, from_classes, to_class, mask, section_points, None)
            elif tool == "point":
                classify_point(self.app, (u2, z2), getattr(self.app, "point_radius", 0.5), from_classes, to_class, mask, section_points, None)

            # ✅ PATCH: Force cut section refresh only when the primary fast path
            # did not already complete GPU sync for this classify operation.
            if is_cut_section:
                try:
                    ctrl = getattr(self.app, 'cut_section_controller', None)
                    if (ctrl is not None
                            and getattr(ctrl, 'is_cut_view_active', False)
                            and ctrl._cut_index_map is not None
                            and ctrl.cut_vtk is not None
                            and (
                                not bool(getattr(self.app, "_gpu_sync_done", False))
                                or bool(getattr(self.app, "_section_visibility_refresh_required", False))
                            )):
                        ctrl._refresh_cut_colors_fast()
                        self._safe_render_pyvista(ctrl.cut_vtk)
                        print("   ⚡ Cut section explicit post-classify refresh (is_cut_section path)")
                except Exception as _cse:
                    print(f"   ⚠️ Cut section post-classify refresh failed: {_cse}")

            self._refresh_all_views_after_classification(to_class)

        except Exception as e:
            print(f"⚠️ Classification failed: {e}")
            import traceback
            traceback.print_exc()

        finally:
            self._stop_deferred_left_release_watch()
            self._gesture_tool = None
            # 1. CLEANUP STATE
            self.is_dragging = False
            self.P1 = None
            self.click_to_finalize = False
            self._press_pos = None
            self.drawing_points = []
            self.is_drawing_freehand = False
            self._last_brush_center = None
            self._is_panning = False
            self._hide_preview_overlay_for_pan()
            self._last_pan_pos = (0, 0) 
            # Clear previews
            self._last_rectangle_preview_P2 = None
            self._last_circle_preview_P2 = None
            self._last_line_preview_P2 = None

            # Clean stroke buffers
            for attr in ("_brush_accumulated_mask", "_brush_stroke_positions", 
                        "_brush_section_indices", "_brush_section_pts2d", 
                        "_brush_section_local_mask", "_last_brush_center_uv"):
                if hasattr(self, attr):
                    try: delattr(self, attr)
                    except: setattr(self, attr, None)

            # _clear_all_previews() hides the preview actors AND calls
            # _safe_render on the active widget internally (see line ~2387),
            # so the preview visual is already gone by the time this returns.
            # A second _safe_render here was redundant — it re-rendered the
            # same widget for 2–5 ms with no visual change. Removed.
            self._clear_all_previews()

            # (removed) QApplication.processEvents() was synchronously
            # draining the entire event queue (20–80 ms stall after fast
            # bursts). Qt's event loop already dispatches the next mouse
            # event automatically as soon as on_left_release returns — the
            # synchronous drain is not needed and was hurting responsiveness.

            # START DEFERRED TIMER (Non-blocking)
            try:
                if hasattr(self.app, '_optimized_refresh') and self.app._optimized_refresh:
                    optimizer = self.app._optimized_refresh
                    if hasattr(optimizer, '_deferred_rebuild_data') and optimizer._deferred_rebuild_data:
                        if hasattr(optimizer, '_deferred_rebuild_timer'):
                            optimizer._deferred_rebuild_timer.stop()
                            # 500ms delay ensures user has time to move mouse before heavy rebuild starts
                            optimizer._deferred_rebuild_timer.start(500) 
            except Exception as e:
                pass
            
            try:
                self.style.OnLeftButtonUp()
            except Exception:
                pass

    def _ensure_target_class_visible(self, to_class):
        """
        ✅ FINAL SENIOR FIX
        Auto-enable ONLY if user has NO filter active.
        Never mutate filtered views.
        """
        try:
            print(f"\n{'='*60}")
            print(f"👁️ Checking target class {to_class} visibility")

            dialog = getattr(self.app, 'display_mode_dialog', None)
            if not dialog:
                print("   ⚠️ No display_mode_dialog found")
                return

            # ✅ MAIN vs SECTION
            active_view = getattr(self.app.section_controller, 'active_view', None)
            target_slot = 0 if active_view is None else active_view + 1

            print(f"   Active view: {active_view}, slot: {target_slot}")

            dialog.slot_shows.setdefault(target_slot, {})
            dialog.view_palettes.setdefault(target_slot, {})

            view_palette = dialog.view_palettes.get(target_slot, {})
            if not view_palette:
                print("   ℹ️ No palette - nothing to auto-enable")
                return

            visible_count = sum(1 for v in view_palette.values() if v.get("show", False))
            total_count = len(view_palette)

            print(f"   📊 Visible: {visible_count}/{total_count} classes")

            # 🔒 USER FILTER → NEVER AUTO-ENABLE
            if 0 < visible_count < total_count:
                print("   🔒 User has filter - NOT auto-enabling")
                return

            if view_palette.get(to_class, {}).get("show", False):
                print("   ✓ Already visible")
                return

            # Enable only when safe
            dialog.slot_shows[target_slot][int(to_class)] = True
            dialog.view_palettes[target_slot][int(to_class)]["show"] = True

            print(f"   ✅ Auto-enabled class {to_class} (no user filter)")

        except Exception as e:
            print(f"⚠️ _ensure_target_class_visible failed: {e}")
            import traceback
            traceback.print_exc()
            
    def _do_line_classify_tap(self):
        """
        Finalize a click-to-click line classification directly from on_left_press.
        Mirrors how interactor_slice calls finalize_section() directly in its press
        handler — no cross-calling on_left_release, no debounce issues, no VTK
        observer recursion.
        """
        import numpy as np
        try:
            from ..classification_tools import (
                classify_above_line, classify_below_line,
                classify_parallel_line_band,
                _get_cut_section_or_default,
            )

            tool = getattr(self.app, "active_classify_tool", None)
            to_class = getattr(self.app, "to_class", None)
            from_classes = getattr(self.app, "from_classes", None)

            # P2: prefer the stored preview position so classification matches
            # the visual line exactly; fall back to current cursor position.
            P2 = getattr(self, "_last_line_preview_P2", None)
            if P2 is None:
                try:
                    dx, dy = self._get_display_point()
                    P2 = self._display_to_world_fast(dx, dy)
                except Exception:
                    pass

            if self.P1 is None or P2 is None:
                print("⚠️ _do_line_classify_tap: P1 or P2 is None, skipping")
                return

            # Detect view context
            vtk_widget = self._get_active_vtk_widget()
            is_cut_section = bool(
                hasattr(self.app, 'cut_section_controller') and
                vtk_widget == getattr(self.app.cut_section_controller, 'cut_vtk', None)
            )

            # Resolve section data (same logic as Branch D in on_left_release)
            if is_cut_section:
                mask = getattr(self.app.section_controller, "last_mask", None)
                section_points = getattr(self.app, "section_points", None)
            else:
                v_idx = self._get_view_index_from_interactor()
                if v_idx is not None:
                    combined_mask = getattr(self.app, f"section_{v_idx}_combined_mask", None)
                    section_points_transformed = getattr(self.app, f"section_{v_idx}_points_transformed", None)
                    if combined_mask is not None and section_points_transformed is not None:
                        mask = combined_mask
                        section_points = section_points_transformed
                    else:
                        mask, section_points = self._get_section_context_for_view(v_idx)
                else:
                    mask, section_points = None, None

            section_indices, section_points = _get_cut_section_or_default(
                self.app, mask, section_points
            )
            if section_indices is None or section_points is None:
                print("⚠️ _do_line_classify_tap: no section data")
                return

            # Project to view coordinates
            if is_cut_section:
                from ..classification_tools import _project_to_cut_view
                (u1, z1), (u2, z2) = _project_to_cut_view(
                    self.app, np.vstack([self.P1, P2])
                )
            else:
                u1, z1 = self._get_view_coordinates(self.P1)
                u2, z2 = self._get_view_coordinates(P2)

            if tool == "above_line":
                classify_above_line(
                    self.app, [(u1, z1), (u2, z2)],
                    from_classes, to_class, mask, section_points, None
                )
            elif tool == "below_line":
                classify_below_line(
                    self.app, [(u1, z1), (u2, z2)],
                    from_classes, to_class, mask, section_points, None
                )
            elif tool == "parallel_line":
                classify_parallel_line_band(
                    self.app, [(u1, z1), (u2, z2)],
                    from_classes, to_class, mask, section_points, None
                )

            if is_cut_section:
                try:
                    ctrl = getattr(self.app, 'cut_section_controller', None)
                    if (ctrl is not None
                            and getattr(ctrl, 'is_cut_view_active', False)
                            and ctrl._cut_index_map is not None
                            and ctrl.cut_vtk is not None
                            and (not bool(getattr(self.app, "_gpu_sync_done", False))
                                 or bool(getattr(self.app, "_section_visibility_refresh_required", False)))):
                        ctrl._refresh_cut_colors_fast()
                        self._safe_render_pyvista(ctrl.cut_vtk)
                except Exception:
                    pass

            self._refresh_all_views_after_classification(to_class)

        except Exception as e:
            print(f"⚠️ _do_line_classify_tap failed: {e}")
            import traceback
            traceback.print_exc()

        finally:
            # Reset drawing state — identical to the finally block in on_left_release.
            # We explicitly notify VTK that the synthetic finalize-on-press path
            # is complete, then ignore the one real LeftButtonReleaseEvent that
            # still arrives when the user physically lets go of the mouse.
            self._stop_deferred_left_release_watch()
            self.is_dragging = False
            self.P1 = None
            self.click_to_finalize = False
            self._press_pos = None
            self._last_line_preview_P2 = None
            self.app._classification_preview_active = False
            self.app._suppress_section_refresh = False
            self._clear_all_previews()
            self._notify_style_left_button_up()
            self._ignore_next_left_release = True
            self._last_release_time = 0.0

    def _refresh_all_views_after_classification(self, to_class):
        # Cheap early-exit via the cached index array, not a 300M-bool np.any
        # scan. _last_changed_indices is set by _apply_mask_and_record /
        # _apply_classification right next to _last_changed_mask, so it's the
        # authoritative "did anything change" signal.
        changed_idx = getattr(self.app, "_last_changed_indices", None)
        if not (isinstance(changed_idx, np.ndarray) and changed_idx.size > 0):
            changed_mask = getattr(self.app, "_last_changed_mask", None)
            if changed_mask is None or not isinstance(changed_mask, np.ndarray) or not np.any(changed_mask):
                print("⏭️ Skipping post-classification refresh - no changed points in current operation")
                return

        display_mode = str(getattr(self.app, "display_mode", "class") or "class").lower()

        if display_mode == "surface":
            try:
                self.app.refresh_surface_after_classification(
                    reason="cross_section_commit",
                    changed_mask=getattr(self.app, "_last_changed_mask", None),
                )
            except Exception as e:
                print(f"⚠️ Surface refresh from cross-section failed: {e}")
            return

        # Fast classify/update path already refreshed GPU buffers and views.
        # Skip the optimizer pipeline to avoid duplicate slot sync + render work.
        if (
            bool(getattr(self.app, "_gpu_sync_done", False))
            and not bool(getattr(self.app, "_section_visibility_refresh_required", False))
        ):
            # A cross-section RGB injection only synchronizes the point actors.
            # In single-class shading the main view is a separate triangulated
            # mesh, so it still needs its topology delta applied before this
            # commit can be considered visually complete.
            if display_mode == "shaded_class":
                try:
                    from gui.unified_actor_manager import (
                        guarantee_main_view_visual_refresh,
                    )

                    shaded_refresh_ok = guarantee_main_view_visual_refresh(
                        self.app,
                        changed_mask=getattr(self.app, "_last_changed_mask", None),
                        to_class=to_class,
                        reason="cross_section_gpu_commit",
                        origin_view="cross_section",
                    )
                    if shaded_refresh_ok:
                        self.app._gpu_sync_done = False
                        return
                    print(
                        "⚠️ Shaded main-view commit refresh was not handled; "
                        "continuing with the normal refresh fallback"
                    )
                except Exception as exc:
                    print(
                        "⚠️ Shaded main-view commit refresh failed; "
                        f"continuing with the normal refresh fallback: {exc}"
                    )

                # The GPU point buffers are synchronized, but the shaded mesh
                # is not. Clear the marker and continue into the established
                # fallback path below instead of suppressing the refresh.
                self.app._gpu_sync_done = False
            else:
                self.app._gpu_sync_done = False
                return

        try:
            # ✅ CRITICAL FIX: ALWAYS use optimizer for multi-view scenarios
            # The optimizer is specifically designed to handle this efficiently
            from gui.optimization_config import ENABLE_OPTIMIZED_REFRESH
            if ENABLE_OPTIMIZED_REFRESH:
                from gui.optimized_refresh import get_optimizer
                optimizer = get_optimizer(self.app)

                from_classes = getattr(self.app, '_last_from_classes', None)
                if from_classes is None:
                    from_classes = getattr(self.app, 'from_classes', None)

                active_view = None
                if hasattr(self.app, 'section_controller'):
                    active_view = getattr(self.app.section_controller, 'active_view', None)

                optimizer.refresh_after_classification(
                    to_class=to_class,
                    from_classes=from_classes,
                    active_view=active_view,
                    fallback_func=self._refresh_all_views_original
                )
                return
        except Exception as e:
            print(f"⚠️ Optimizer unavailable: {e}")

        self._refresh_all_views_original(to_class)


    def _refresh_all_views_original(self, to_class):
        """
        ✅ ORIGINAL CODE - Renamed but unchanged.
        
        This is your existing _refresh_all_views_after_classification method.
        """
        # ═══════════════════════════════════════════════════════════════════════
        # YOUR EXISTING CODE GOES HERE - COMPLETELY UNCHANGED
        # ═══════════════════════════════════════════════════════════════════════
        
        print(f"\n{'='*60}")
        print(f"🔄 REFRESHING VIEWS AFTER CLASSIFICATION")
        print(f"   Target class: {to_class}")
        print(f"   Display mode: {self.app.display_mode}")
        print(f"{'='*60}")

        num_changed = 0
        class_text = "no changes"

        try:
            import numpy as np
            app = self.app

            # ✅ 1. Sync weights BEFORE any refresh
            print(f"\n🔧 SYNCING WEIGHTS FROM DISPLAY MODE...")
            self._sync_main_view_palette_weights()

            # STEP 1: Info about what was classified
            changed_mask = getattr(app, "_last_changed_mask", None)
            if to_class is not None:
                # Pull the count from the cached index array (already computed
                # in _apply_mask_and_record / _apply_classification) instead of
                # rescanning a 300M-bool mask with .sum().
                changed_idx_cached = getattr(app, "_last_changed_indices", None)
                if isinstance(changed_idx_cached, np.ndarray):
                    num_changed = int(changed_idx_cached.size)
                elif isinstance(changed_mask, np.ndarray):
                    num_changed = int(changed_mask.sum())
                else:
                    num_changed = "?"
                print(f"\nℹ️ Classified {num_changed} points to class {to_class}")

                already_visible = False
                if hasattr(app, 'display_mode_dialog') and app.display_mode_dialog:
                    dialog = app.display_mode_dialog
                    if hasattr(dialog, 'view_palettes') and 0 in dialog.view_palettes:
                        slot0 = dialog.view_palettes[0]
                        if to_class in slot0:
                            already_visible = slot0[to_class].get("show", False)

                if already_visible:
                    print(f"   ✅ Class {to_class} is already visible")
                else:
                    print(f"   ⚠️ Class {to_class} is HIDDEN")

            # STEP 2: Refresh ALL cross-section views
            if hasattr(app, 'section_vtks') and app.section_vtks:
                num_views = len(app.section_vtks)
                print(f"\n🔄 Refreshing ALL {num_views} cross-section views...")

                for view_idx in sorted(app.section_vtks.keys()):
                    try:
                        # ✅ ISSUE 2 FIX: Skip refresh if no changed points intersect this section's bounding box
                        section_combined_mask = getattr(app, f"section_{view_idx}_combined_mask", None)
                        if (section_combined_mask is not None
                            and isinstance(section_combined_mask, np.ndarray)
                            and len(section_combined_mask) == len(changed_mask)):
                            if not np.any(changed_mask & section_combined_mask):
                                continue  # no changed points in this section — skip
                        
                        self._refresh_single_view(view_idx)
                    except Exception as e:
                        print(f"      ⚠️ View {view_idx + 1} refresh failed: {e}")

            # STEP 3: MAIN VIEW REFRESH
            print(f"\n🔄 Refreshing Main View...")

            vtk_widget = getattr(app, 'vtk_widget', None)
            saved_camera = None
            if vtk_widget is not None:
                try:
                    if hasattr(vtk_widget, 'camera_position') and vtk_widget.camera_position is not None:
                        saved_camera = vtk_widget.camera_position
                except Exception as e:
                    print(f"   ⚠️ Camera save failed: {e}")

            display_mode = str(getattr(app, "display_mode", "class") or "class").lower()

            if display_mode == "class":
                # ✅ UNIFIED ACTOR: write directly into shared GPU buffer — no rebuild
                from gui.unified_actor_manager import fast_classify_update, is_unified_actor_ready
                if is_unified_actor_ready(app):
                    changed_mask = getattr(app, '_last_changed_mask', None)
                    palette      = getattr(app, 'class_palette', {})
                    border_pct   = float(getattr(app, 'point_border_percent', 0) or 0.0)
                    fast_classify_update(app, changed_mask=changed_mask,
                                         to_class=to_class, palette=palette,
                                         border_percent=border_pct,
                                         skip_render=True)
                    try:
                        self._safe_render_pyvista(vtk_widget)
                    except Exception:
                        pass
                    print(f"   ✅ Unified actor GPU poke complete")
                else:
                    # Unified actor not built yet — trigger a one-time build
                    print(f"   ⚠️ Unified actor not ready — triggering build")
                    from gui.class_display import update_class_mode
                    update_class_mode(app, force_refresh=True)

            elif display_mode == "shaded_class":
                print(f"   🌗 Shaded mode – forcing rebuild...")
                azimuth = getattr(app, "last_shade_azimuth", 45.0)
                angle = getattr(app, "last_shade_angle", 45.0)
                ambient = getattr(app, "shade_ambient", 0.2)
                from gui.shading_display import update_shaded_class
                update_shaded_class(app, azimuth, angle, ambient)

            elif display_mode == "surface":
                print("   🏔️ Surface mode – forcing mesh rebuild after cross-section classification...")
                try:
                    self.app.refresh_surface_after_classification(
                        reason="cross_section_commit",
                        changed_mask=changed_mask,
                    )
                except Exception as e:
                    print(f"   ⚠️ Surface refresh failed: {e}")
            else:
                # We are in depth, intensity, elevation, or rgb mode.
                # Guard: keep current non-class main-view buffer untouched until mode switch.
                print(f"   📊 Mode '{display_mode}' – main view frozen, classification data saved, will render on mode switch")

            if saved_camera is not None and vtk_widget is not None:
                try:
                    vtk_widget.camera_position = saved_camera
                    # Guard: do not trigger main-view render in non-class modes after classify.
                    if display_mode in ("class", "shaded_class"):
                        self._safe_render_pyvista(vtk_widget)
                except Exception as e:
                    print(f"   ⚠️ Camera restore failed: {e}")

            # # STEP 4: CUT SECTION
            #             cut_ctrl.onclassificationchanged()
            # STEP 4: CUT SECTION
            if (hasattr(app, "cut_section_controller") and app.cut_section_controller is not None):
                try:
                    cut_ctrl = app.cut_section_controller
                    if getattr(cut_ctrl, 'is_cut_view_active', False):
                        if hasattr(cut_ctrl, 'onclassificationchanged'):
                            cut_ctrl.onclassificationchanged()
                        else:
                            # Fallback if method is missing
                            cut_ctrl._refresh_cut_colors_fast()
                        print("   ✅ Cut Section refreshed")
                except Exception as e:
                    print(f"   ⚠️ Cut section refresh failed: {e}")

            # STEP 5: POINT STATISTICS
            try:
                from gui.point_count_widget import refresh_point_statistics
                refresh_point_statistics(app)
            except Exception:
                pass

            # STEP 6: Status bar
            def _get_class_name(code):
                try:
                    palette = getattr(app, 'class_palette', {})
                    if code in palette:
                        return palette[code].get("description", f"Class {code}")
                except Exception:
                    pass
                return f"Class {code}"

            to_name = _get_class_name(to_class)
            app.statusBar().showMessage(
            # REPLACE with this safe version:
            f"✅ {num_changed:,} points classified to {to_name}" if isinstance(num_changed, (int, float)) else f"✅ {num_changed} points classified to {to_name}",
                3000
            )

            print(f"\n{'='*60}")
            print(f"✅ REFRESH COMPLETE")
            print(f"{'='*60}\n")

        except Exception as e:
            print(f"\n⚠️ MULTI-VIEW REFRESH ERROR: {e}")
            import traceback
            traceback.print_exc()

    def _refresh_main_view_with_filter(self, to_class):
        """
        ✅ UNIFIED ACTOR: Apply visibility filter via sync_palette_to_gpu (slot 0).
        Never calls plotter.clear() — that would destroy the unified actor.
        """
        print(f"\n{'='*60}")
        print(f"🔄 FILTERED MAIN VIEW REFRESH (UNIFIED GPU PATH)")
        print(f"{'='*60}")

        try:
            app = self.app

            if app.display_mode != "class":
                print("   ⏭️ Not in class mode")
                return

            visible_classes = self._get_visible_classes_for_slot(0)
            if visible_classes is None:
                print("   ⏭️ No filter active")
                return

            print(f"   👁️ Filter: showing {len(visible_classes)} classes: {visible_classes}")

            from gui.unified_actor_manager import sync_palette_to_gpu, is_unified_actor_ready
            if not is_unified_actor_ready(app):
                print("   ⚠️ Unified actor not ready — skipping filter refresh")
                return

            # Build a palette copy with show=False for hidden classes, then push to GPU
            palette = dict(getattr(app, 'class_palette', {}))
            visible_set = set(visible_classes)
            filtered_palette = {}
            for code, info in palette.items():
                entry = dict(info)
                entry['show'] = (code in visible_set)
                filtered_palette[code] = entry

            border_pct = float(getattr(app, 'point_border_percent', 0) or 0.0)
            sync_palette_to_gpu(app, 0, filtered_palette, border_pct, render=True)
            print(f"   ✅ Unified filter refresh complete")

        except Exception as e:
            print(f"   ❌ Filtered refresh FAILED: {e}")
            import traceback
            traceback.print_exc()

        print(f"{'='*60}\n")

    def _refresh_single_view(self, view_index, changed_mask=None):
        """
        Refresh a single cross-section view:
        ✅ ZERO BLINK: Routes directly to unified_actor_manager.fast_cross_section_update
        """
        import time
        from gui.unified_actor_manager import fast_cross_section_update
        
        t0 = time.perf_counter()
        slot = view_index + 1
        
        # Get palette for this specific view (slot 1-4)
        view_palette = None
        if hasattr(self.app, 'display_mode_dialog') and self.app.display_mode_dialog:
            dialog = self.app.display_mode_dialog
            if hasattr(dialog, 'view_palettes') and slot in dialog.view_palettes:
                view_palette = dialog.view_palettes[slot]
        
        if not view_palette and hasattr(self.app, 'view_palettes'):
            view_palette = self.app.view_palettes.get(slot)
        
        if not view_palette:
            if slot == 0 and hasattr(self.app, 'class_palette'):
                view_palette = self.app.class_palette
            elif hasattr(self.app, "_get_cross_section_palette"):
                try:
                    view_palette = self.app._get_cross_section_palette(
                        slot,
                        allow_default_seed=True,
                        persist_seed=False,
                    )
                except Exception:
                    pass
             
        ok = fast_cross_section_update(
            self.app,
            view_index,
            changed_mask,
            palette=view_palette,
            force_visibility_refresh=False,
        )
        if not ok:
            from gui.unified_actor_manager import build_section_unified_actor
            border = float(getattr(self.app, "view_borders", {}).get(slot, 0) or 0.0)
            view_mode = getattr(self.app, "cross_view_mode", "front")
            rebuilt = build_section_unified_actor(
                self.app,
                view_index,
                palette=view_palette,
                border_percent=border,
                view=view_mode,
            )
            if rebuilt is not None:
                fast_cross_section_update(
                    self.app,
                    view_index,
                    changed_mask,
                    palette=view_palette,
                    force_visibility_refresh=False,
                )
        
        elapsed = (time.perf_counter() - t0) * 1000
        print(f"      ⚡ View {view_index + 1} unified update: {elapsed:.1f} ms")

    def refresh_main_view_partial(self):
        """
        SMART PARTIAL REFRESH: Update ONLY the cross-sectioned area in Main View.
        - Reads changed points from _last_changed_mask
        - Updates actors for BOTH old and new classes
        - Preserves independent main view display settings
        """
        print("\n" + "="*60)
        print("🔄 PARTIAL MAIN VIEW REFRESH")
        print("="*60)
        
        try:
            if self.app.display_mode != "class":
                print("⏭️  Not in class mode - skipping")
                print("="*60 + "\n")
                return
            
            if not hasattr(self.app, '_last_changed_mask') or self.app._last_changed_mask is None:
                print("⏭️  No changed points mask")
                print("="*60 + "\n")
                return
            
            changed_mask = self.app._last_changed_mask
            num_changed = np.sum(changed_mask)
            
            if num_changed == 0:
                print("⏭️  No points changed")
                print("="*60 + "\n")
                return
            
            print(f"   📊 Changed points: {num_changed:,}")

            palette = getattr(self.app, "class_palette", {})
            if not palette:
                print("⚠️  No palette - cannot refresh")
                print("="*60 + "\n")
                return

            # ✅ UNIFIED ACTOR: write changed colours directly into _naksha_rgb_ptr.
            # Never remove/re-add class_ actors — that path is for per-class mode only.
            from gui.unified_actor_manager import fast_classify_update, is_unified_actor_ready

            classes    = self.app.data["classification"]
            changed_indices  = np.flatnonzero(changed_mask)
            changed_classes  = classes[changed_indices]

            print(f"   📍 Changed points by class:")
            for cls in np.unique(changed_classes):
                count = np.sum(changed_classes == cls)
                print(f"      Class {cls}: {count:,} points")

            if is_unified_actor_ready(self.app):
                border_pct = float(getattr(self.app, 'point_border_percent', 0) or 0.0)
                to_class   = getattr(self.app, '_last_to_class', None)
                fast_classify_update(self.app,
                                     changed_mask=changed_mask,
                                     to_class=to_class,
                                     palette=palette,
                                     border_percent=border_pct,
                                     skip_render=True)
                try:
                    self._safe_render_pyvista(self.app.vtk_widget)
                except Exception:
                    pass
                print(f"   ✅ Unified partial refresh complete")
            else:
                print(f"   ⚠️ Unified actor not ready — skipping partial refresh")

            print("="*60 + "\n")
            
        except Exception as e:
            print(f"❌ Partial main view refresh failed: {e}")
            import traceback
            traceback.print_exc()
            print("="*60 + "\n")

    def _get_main_view_palette(self):
        """
        Get the Main View's color palette (Slot 0 in Display Mode).
        
        Returns:
            dict: Color palette for main view, or None if not available
        """
        # Try Display Mode dialog first
        if hasattr(self.app, 'display_mode_dialog'):
            dialog = self.app.display_mode_dialog
            
            if hasattr(dialog, 'view_palettes') and 0 in dialog.view_palettes:
                return dialog.view_palettes[0]
        
        # Try display_dialog attribute
        if hasattr(self.app, 'display_dialog'):
            dialog = self.app.display_dialog
            
            if hasattr(dialog, 'view_palettes') and 0 in dialog.view_palettes:
                return dialog.view_palettes[0]
        
        # Fallback to global class_palette
        if hasattr(self.app, 'class_palette') and self.app.class_palette:
            return self.app.class_palette
        
        return None
    
    def _refresh_main_view(self):
        """
        ✅ FIXED: Refresh Main View with proper weight synchronization
        """
        print(f"      📋 Main View (Slot 0):")
        
        # Only refresh if in class mode
        if self.app.display_mode != "class":
            print(f"         ⏭️ Not in class mode")
            return
        
        # ✅ CRITICAL FIX: Sync weights BEFORE refresh
        self._sync_main_view_palette_weights()
        
        # Get visible classes
        palette = getattr(self.app, 'class_palette', {})
        
        if not palette:
            print(f"         ❌ No palette available")
            return
        
        visible = [c for c, v in palette.items() if v.get("show", False)]
        
        if not visible:
            print(f"         ⏭️ No visible classes")
            return
        
        print(f"         👁️ Visible classes: {len(visible)}")
        
        # ✅ DEBUG: Show current weights
        print(f"         📊 Current weights:")
        for code in sorted(visible):
            weight = palette[code].get('weight', 1.0)
            desc = palette[code].get('description', f'Class {code}')
            print(f"            Class {code} ({desc[:20]}): {weight:.2f}x")
        
        # ✅ UNIFIED ACTOR: push updated weights into GPU uniform LUT — no rebuild
        try:
            from gui.unified_actor_manager import sync_palette_to_gpu, is_unified_actor_ready
            if is_unified_actor_ready(self.app):
                border_pct = float(getattr(self.app, 'point_border_percent', 0) or 0.0)
                sync_palette_to_gpu(self.app, 0, palette, border_pct, render=True)
                print(f"         ✅ Unified GPU weight sync complete")
            else:
                from gui.class_display import update_class_mode
                update_class_mode(self.app)
                print(f"         ✅ Refreshed with updated weights (build path)")
        except Exception as e:
            print(f"         ⚠️ Refresh failed: {e}")
            import traceback
            traceback.print_exc()
            
    def _show_refresh_flash(self, view_index):
        """
        ✅ Show a subtle visual flash to indicate refresh happened
        Makes the user aware their classification was instantly applied
        """
        try:
            if not hasattr(self.app, 'section_vtks') or view_index not in self.app.section_vtks:
                return
            
            vtk_widget = self.app.section_vtks[view_index]
            
            # Method 1: Brief background color flash (subtle)
            original_bg = vtk_widget.renderer.GetBackground()
            
            # Flash to slightly brighter background
            flash_color = (
                min(original_bg[0] + 0.1, 1.0),
                min(original_bg[1] + 0.1, 1.0),
                min(original_bg[2] + 0.1, 1.0)
            )
            
            vtk_widget.renderer.SetBackground(*flash_color)
            self._safe_render_pyvista(vtk_widget)
            
            # Restore original background after brief delay
            from PySide6.QtCore import QTimer
            
            def restore_background():
                try:
                    vtk_widget.renderer.SetBackground(*original_bg)
                    self._safe_render_pyvista(vtk_widget)
                except Exception:
                    pass
            
            # Flash duration: 150ms (very brief, not annoying)
            QTimer.singleShot(150, restore_background)
            
            print(f"   ✨ Visual feedback shown")
            
        except Exception as e:
            # Visual feedback is optional - don't break if it fails
            print(f"   ⚠️ Visual feedback failed (non-critical): {e}")

    def _show_cursor_feedback(self):
        """
        Alternative visual feedback: briefly change cursor to indicate success
        Less intrusive than background flash
        """
        try:
            from PySide6.QtCore import Qt, QTimer
            from PySide6.QtWidgets import QApplication
            
            # Save original cursor
            original_cursor = QApplication.overrideCursor()
            
            # Show "success" cursor briefly (pointing hand)
            QApplication.setOverrideCursor(Qt.PointingHandCursor)
            
            # Restore after 200ms
            def restore_cursor():
                QApplication.restoreOverrideCursor()
                if original_cursor:
                    QApplication.setOverrideCursor(original_cursor)
            
            QTimer.singleShot(200, restore_cursor)
            
        except Exception as e:
            print(f"   ⚠️ Cursor feedback failed: {e}")

    def _verify_actor_update(self, view_index, class_code, expected_size):
        """
        ✅ UNIFIED ACTOR: Verify weight via ViewShaderContext.weight_lut.
        Per-class actors no longer exist — point sizes live in the GPU uniform LUT.
        """
        try:
            vtk_widget = getattr(self.app, 'section_vtks', {}).get(view_index)
            if vtk_widget is None:
                return False

            # Unified section actor name
            actor = vtk_widget.actors.get(f"_section_{view_index}_unified")
            if actor is None:
                print(f"      ⚠️ View {view_index+1}: unified actor not found — skipping verify")
                return False

            ctx = getattr(actor, '_naksha_shader_ctx', None)
            if ctx is None:
                print(f"      ⚠️ View {view_index+1}: no shader ctx on unified actor")
                return False

            actual_size = float(ctx.weight_lut[int(class_code)])
            size_match  = abs(actual_size - expected_size) < 0.05

            if size_match:
                print(f"      ✅ View {view_index+1}: Class {class_code} LUT verified ({actual_size:.2f})")
            else:
                print(f"      ❌ View {view_index+1}: Class {class_code} LUT MISMATCH! "
                      f"expected={expected_size:.2f} actual={actual_size:.2f}")
            return size_match

        except Exception as e:
            print(f"      ⚠️ Verify failed: {e}")
            return False

    def on_key_press(self, obj, evt):
        """Handle keyboard shortcuts."""
        # ✅ CRITICAL SAFETY CHECK
        if not hasattr(self, 'app') or self.app is None:
            return
        
        if not hasattr(self, 'interactor') or self.interactor is None:
            return
        
        key = self.interactor.GetKeySym().lower()

        if key in ("return", "enter") and getattr(self.app, "active_classify_tool", None) == "polygon":
            self._append_polygon_vertex_from_cursor()
            if self._finalize_polygon_classification():
                self._clear_all_previews()
            return
        
        # EXISTING ESC HANDLER
        if key == "escape":
            print("\n⚠️ ESC pressed - deactivating classification tool")
            
            if hasattr(self.app, 'deactivate_classification'):
                self.app.deactivate_classification()
            else:
                self.app.active_classify_tool = None
                self.app.skip_main_view_refresh = False
                if hasattr(self.app, "_gpu_sync_done"):
                    self.app._gpu_sync_done = False
                if hasattr(self.app, "_section_visibility_refresh_required"):
                    self.app._section_visibility_refresh_required = False
                try:
                    if hasattr(self.app, "vtk_widget") and self.app.vtk_widget is not None:
                        self.app.vtk_widget.render()
                except Exception:
                    pass
            
            self.P1 = None
            self.is_dragging = False
            self.drawing_points = []
            self.is_drawing_freehand = False
            self._clear_all_previews()
            
            print("✅ Classification tool cancelled")
        

    def handle_escape(self):
        """Handle ESC key inside classification context safely."""
        try:
            self.app._classification_preview_active = False
            if hasattr(self.app, "active_classify_tool"):
                print(f"🟣 ESC pressed in classification tool '{self.app.active_classify_tool}'")
                self.app.active_classify_tool = None
                if hasattr(self.app, "statusBar"):
                    self.app.statusBar().showMessage("Classification cancelled.", 3000)
                print("✅ Classification ESC handled cleanly.")
        except Exception as e:
            print(f"⚠️ Classification ESC error: {e}")


    def _draw_freehand_preview(self, live_point=None):
        """
        Smooth freehand preview using Catmull-Rom spline interpolation.
        Works for MAIN (XY) and SECTION (u,z) — uses self._freehand_* reusable pipeline.
        """
        if len(self.drawing_points) < 1:
            return

        vtk_widget = self._get_active_vtk_widget()
        if vtk_widget is None:
            # Fallback: use first renderer
            try:
                rw = self.interactor.GetRenderWindow()
                renderer = rw.GetRenderers().GetFirstRenderer()
            except Exception:
                return
        else:
            renderer = vtk_widget.renderer

        # Collect raw control points
        all_pts = list(self.drawing_points)
        if live_point:
            all_pts.append(live_point)
        if len(all_pts) < 2:
            return

        # MAIN: dense native-event samples + corner-safe centripetal spline.
        # SECTION/CUT: preserve the established smoother unchanged.
        if self._is_main_view():
            all_pts = self._centripetal_catmull_rom_smooth(all_pts, steps=4)
        else:
            all_pts = self._catmull_rom_smooth(all_pts, steps=10)

        # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
        if not hasattr(self, "_freehand_pts") or self.freehand_actor is None:
            from vtkmodules.vtkRenderingCore import vtkPolyDataMapper2D, vtkActor2D, vtkCoordinate as _C

            self._freehand_pts   = vtkPoints()
            self._freehand_lines = vtkCellArray()
            self._freehand_poly  = vtkPolyData()
            self._freehand_poly.SetPoints(self._freehand_pts)
            self._freehand_poly.SetLines(self._freehand_lines)

            mapper = vtkPolyDataMapper2D()
            mapper.SetInputData(self._freehand_poly)
            dc = _C(); dc.SetCoordinateSystemToDisplay()
            mapper.SetTransformCoordinate(dc)

            self.freehand_actor = vtkActor2D()
            self.freehand_actor.SetMapper(mapper)
            prop = self.freehand_actor.GetProperty()
            prop.SetColor(1, 0.8, 0)
            prop.SetLineWidth(2)
            prop.SetOpacity(0.8)

            renderer.AddActor2D(self.freehand_actor)

        if not renderer.HasViewProp(self.freehand_actor):
            renderer.AddActor2D(self.freehand_actor)
        self.freehand_actor.VisibilityOn()

        if not hasattr(self, "_freehand_coord"):
            from vtkmodules.vtkRenderingCore import vtkCoordinate as _C
            self._freehand_coord = _C()
            self._freehand_coord.SetCoordinateSystemToWorld()

        coord = self._freehand_coord
        self._freehand_pts.Reset()
        self._freehand_lines.Reset()

        for i, pt in enumerate(all_pts):
            u, v = pt[0], pt[1]
            xw, yw, zw = self._view2d_to_world(u, v)
            coord.SetValue(xw, yw, zw)
            d = coord.GetComputedDisplayValue(renderer)
            self._freehand_pts.InsertNextPoint(d[0], d[1], 0)
            if i > 0:
                self._freehand_lines.InsertNextCell(2)
                self._freehand_lines.InsertCellPoint(i - 1)
                self._freehand_lines.InsertCellPoint(i)

        self._freehand_pts.Modified()
        self._freehand_poly.Modified()
        # Render batched by caller

    def _draw_point_cursor(self, center, radius=0.5):
        """✅ POINT CURSOR: Filled circle with outline (different from brush/circle tools)"""
        renderWindow = self.interactor.GetRenderWindow()
        renderer = renderWindow.GetRenderers().GetFirstRenderer()
 
        # Remove previous actor
        if hasattr(self, "brush_actor") and self.brush_actor:
            renderer.RemoveActor2D(self.brush_actor)
            self.brush_actor = None
 
        cx, cz = center
 
        # Force minimum radius
        if radius < 0.05:
            radius = 0.1
 
        from vtkmodules.vtkRenderingCore import vtkCoordinate
       
        # ========== CREATE FILLED POINT SHAPE ==========
        # 1. Filled circle (using triangle fan)
        # 2. Outline circle
       
        num_segments = 32
       
        # Create circle points in world space
        circle_world = []
        for i in range(num_segments + 1):
            angle = 2 * np.pi * i / num_segments
            x = cx + radius * np.cos(angle)
            z = cz + radius * np.sin(angle)
            circle_world.append((x, 0, z))
       
        # Convert to 2D display coordinates
        pts = vtkPoints()
       
        # Add center point first (for triangle fan)
        coord_center = vtkCoordinate()
        coord_center.SetCoordinateSystemToWorld()
        coord_center.SetValue(cx, 0, cz)
        display_center = coord_center.GetComputedDisplayValue(renderer)
        pts.InsertNextPoint(display_center[0], display_center[1], 0)
       
        # Add circle perimeter points
        for point in circle_world:
            coord = vtkCoordinate()
            coord.SetCoordinateSystemToWorld()
            coord.SetValue(point[0], point[1], point[2])
            display_pos = coord.GetComputedDisplayValue(renderer)
            pts.InsertNextPoint(display_pos[0], display_pos[1], 0)
 
        # Create filled circle using triangle fan
        from vtkmodules.vtkCommonDataModel import vtkCellArray
        polys = vtkCellArray()
       
        # Triangle fan from center to perimeter
        for i in range(1, num_segments + 1):
            polys.InsertNextCell(3)
            polys.InsertCellPoint(0)  # Center
            polys.InsertCellPoint(i)
            polys.InsertCellPoint(i + 1 if i < num_segments else 1)
 
        # Create outline
        lines = vtkCellArray()
        for i in range(1, num_segments + 1):
            lines.InsertNextCell(2)
            lines.InsertCellPoint(i)
            lines.InsertCellPoint(i + 1 if i < num_segments else 1)
 
        poly = vtkPolyData()
        poly.SetPoints(pts)
        poly.SetPolys(polys)  # Filled triangles
        poly.SetLines(lines)   # Outline
 
        # Create 2D mapper
        from vtkmodules.vtkRenderingCore import vtkPolyDataMapper2D
        mapper = vtkPolyDataMapper2D()
        mapper.SetInputData(poly)
       
        coordinate = vtkCoordinate()
        coordinate.SetCoordinateSystemToDisplay()
        mapper.SetTransformCoordinate(coordinate)
 
        # Create 2D actor
        from vtkmodules.vtkRenderingCore import vtkActor2D
        self.brush_actor = vtkActor2D()
        self.brush_actor.SetMapper(mapper)
 
        # Set properties - Semi-transparent filled yellow circle with outline
        prop = self.brush_actor.GetProperty()
        prop.SetColor(1, 1, 0)  # Yellow
        prop.SetLineWidth(2)
        prop.SetOpacity(0.5)  # Semi-transparent for filled area
 
        renderer.AddActor2D(self.brush_actor)
        print(f"🎯 Point cursor drawn (filled circle, radius={radius:.2f})")

     # ======== MAIN-VIEW CLASSIFIERS (Plan View: XY) ========

    def _active_flight_line_slot(self):
        """Map the active classification interactor to its Display Mode slot."""
        if self._is_main_view():
            return 0
        try:
            active = self._get_active_vtk_widget()
            ctrl = getattr(self.app, "cut_section_controller", None)
            if ctrl is not None and active == getattr(ctrl, "cut_vtk", None):
                return 5
        except Exception:
            pass
        view_idx = self._get_view_index_from_interactor()
        return int(view_idx) + 1 if view_idx is not None else 0
 
    def _apply_mask_and_record(self, mask, to_class):
        """
        Apply classification to app.data using mask and push an undo step.

        ✅ FIX: In MAIN VIEW, classification affects ONLY classes that are visible
            in Display Mode (slot 0). Hidden classes are protected.
        ✅ Also enforces from_classes here (so brush + any caller is consistent).
        """
        self.app._gpu_sync_done = False
        self.app._section_visibility_refresh_required = False
        self.app._last_changed_mask = None
        self.app._last_changed_indices = None
        # Stamp BEFORE the GPU sync calls (which trigger main-view render via
        # the wrapped vtk_widget.render). GPURenderManager reads this to
        # coalesce main-view renders during a classify streak — see
        # request_render().
        self.app._last_classify_ts = time.time()

        if mask is None or not np.any(mask):
            if hasattr(self.app, 'statusBar'):
                self.app.statusBar().showMessage("No points found in selection.", 2000)
            return False

        if to_class is None:
            to_class = getattr(self.app, "to_class", None)
        if to_class is None:
            if hasattr(self.app, 'statusBar'):
                self.app.statusBar().showMessage("Target class not set.", 2000)
            return False

        classes = self.app.data["classification"]

        # ---------------------------------------------------------
        # ✅ 1) Enforce from_classes filter (safe even if caller did it already)
        # ---------------------------------------------------------
        mask = self._filter_from_classes(mask)

        # ---------------------------------------------------------
        # ✅ 1b) In Surface mode, only allow edits on visible classes.
        # ---------------------------------------------------------
        if str(getattr(self.app, "display_mode", "") or "").lower() == "surface":
            visible_classes = self._get_visible_classes_for_slot(0)
            if visible_classes == []:
                if hasattr(self.app, 'statusBar'):
                    self.app.statusBar().showMessage("No visible classes selected in Surface mode.", 2500)
                return False

            if visible_classes is not None:
                sel_idx = np.flatnonzero(mask)
                if sel_idx.size > 0:
                    keep = np.isin(
                        classes[sel_idx],
                        np.asarray(visible_classes, dtype=classes.dtype),
                    )
                    mask = np.zeros_like(mask)
                    mask[sel_idx[keep]] = True

        # ---------------------------------------------------------
        # ✅ 2) Enforce Display Mode visibility filter for MAIN VIEW (slot 0)
        # ---------------------------------------------------------
        if self._is_main_view() and str(getattr(self.app, "display_mode", "") or "").lower() != "surface":
            visible_classes = self._get_visible_classes_for_slot(0)

            if visible_classes == []:
                if hasattr(self.app, 'statusBar'):
                    self.app.statusBar().showMessage("No visible classes selected in Display Mode.", 2500)
                return False

            if visible_classes is not None:
                # ⚡ O(K) filter — only check the K selected points (typically
                # a few thousand for rect/circle/freehand), not all 39M
                # points. The naive `np.isin(classes, visible_classes)` over
                # 39M points takes 100–200 ms and was the dominant cost for
                # main-view rect/circle/freehand classify.
                sel_idx = np.flatnonzero(mask)
                if sel_idx.size > 0:
                    keep = np.isin(
                        classes[sel_idx],
                        np.asarray(visible_classes, dtype=classes.dtype),
                    )
                    mask = np.zeros_like(mask)
                    mask[sel_idx[keep]] = True

        # Compute changed_indices ONCE — the previous code scanned the full
        # N=300M-point mask up to 5 separate times (np.any, classes[mask].copy,
        # np.flatnonzero, classes[mask] = to_class). Working in index-space
        # below saves ~3 full-array scans (~300-400 ms per classify on 300M pts).
        changed_indices = np.flatnonzero(mask).astype(np.int64, copy=False)
        # Disabled LAS flight lines are protected for every tool and view,
        # independent of the active display colour mode.
        from gui.flight_line_filter import filter_visible_flight_line_indices
        changed_indices = filter_visible_flight_line_indices(
            self.app, changed_indices, len(classes), slot=self._active_flight_line_slot()
        )
        if changed_indices.size == 0:
            if hasattr(self.app, 'statusBar'):
                self.app.statusBar().showMessage("No visible/from-class points in selection.", 2500)
            return False

        # ---------------------------------------------------------
        # Apply + Undo/Redo (all O(K) — K = changed point count)
        # ---------------------------------------------------------
        old = classes[changed_indices].copy()
        new = np.full(changed_indices.size, to_class, dtype=classes.dtype)

        self.app.undo_stack.append({
            "indices": changed_indices,
            "old_classes": old,
            "new_classes": new,
        })
        self.app.redo_stack.clear()

        classes[changed_indices] = to_class
        # Downstream partial refreshes must use the final protected set.
        mask = np.zeros_like(mask, dtype=bool)
        mask[changed_indices] = True
        self.app._last_changed_mask = mask
        self.app._last_changed_indices = changed_indices.copy()

        # ✅ Store from_classes for undo
        self.app._last_from_classes = list(getattr(self.app, "from_classes", []) or [])

        # ─────────────────────────────────────────────────────────────
        # ⚡ MICROSTATION-STYLE INSTANT GPU INJECTION (replaces heavy optimizer)
        # ─────────────────────────────────────────────────────────────
        try:
            from gui.unified_actor_manager import fast_partial_classify_update, fast_partial_cross_section_update
            
            # 1. Main view: inject colors directly into GPU buffer (O(changed_points))
            fast_partial_classify_update(self.app, mask)
            
            # 2. Cross-sections: partial shared memory slave update
            section_widgets_to_render = []
            if hasattr(self.app, 'section_vtks') and self.app.section_vtks:
                for view_idx, section_widget in self.app.section_vtks.items():
                    try:
                        # Render only views that actually contain changed points.
                        should_render = False
                        section_mask = getattr(self.app, f"section_{view_idx}_combined_mask", None)
                        if (section_mask is not None
                                and len(section_mask) == len(mask)
                                and changed_indices.size > 0):
                            should_render = bool(np.any(section_mask[changed_indices]))

                        fast_partial_cross_section_update(self.app, view_idx, mask)

                        if should_render and section_widget is not None:
                            section_widgets_to_render.append(section_widget)
                    except Exception:
                        pass
            
            if hasattr(self.app, 'cut_section_controller') and self.app.cut_section_controller:
                ctrl = self.app.cut_section_controller
                if getattr(ctrl, 'is_cut_view_active', False) and ctrl._cut_index_map is not None:
                    try:
                        ctrl._refresh_cut_colors_fast()
                        if hasattr(ctrl, 'cut_vtk') and ctrl.cut_vtk:
                            self._safe_render_pyvista(ctrl.cut_vtk)
                    except Exception as _ce:
                        print(f"⚠️ Cut section refresh failed: {_ce}")
            
            # 4. Render only the updated visible cross-section views.
            for section_widget in section_widgets_to_render:
                try:
                    if (hasattr(section_widget, 'isVisible')
                            and callable(section_widget.isVisible)
                            and not section_widget.isVisible()):
                        continue
                    self._safe_render_pyvista(section_widget)
                except Exception:
                    pass

            # 5. Main-view presentation is deferred to
            # guarantee_main_view_visual_refresh below. Rendering the complete
            # faceted mesh here produced a stale pre-patch frame and the
            # guarantee immediately rendered the same main view again.
            
            # 6. Point statistics
            try:
                from gui.point_count_widget import refresh_point_statistics
                refresh_point_statistics(self.app)
            except Exception:
                pass
            
            # 7. Status bar
            n_pts = int(changed_indices.size)
            if hasattr(self.app, 'statusBar'):
                self.app.statusBar().showMessage(f"✅ {n_pts:,} points → class {to_class}", 3000)

            # ✅ FIX: Signal that GPU sync is complete — prevents the
            # classify_with_stats_update decorator from triggering a
            # redundant 170ms OptimizedRefreshPipeline cycle.
            self.app._gpu_sync_done = True
            self.app._section_visibility_refresh_required = False
            try:
                from gui.unified_actor_manager import guarantee_main_view_visual_refresh
                guarantee_main_view_visual_refresh(
                    self.app,
                    changed_mask=mask,
                    to_class=to_class,
                    reason="main_view_commit",
                )
            except Exception:
                pass
            # Mark active-classify timestamp so MemoryLeakGuard defers its
            # gen-2 GC while the user is mid-streak.
            self.app._last_classify_ts = time.time()
            
            # The guarantee above is the sole shaded-class updater and presenter
            # for this commit. Repeating the shading refresh here traversed the
            # facet colors twice and scheduled another render of the same mask.

        except Exception as e:
            print(f"⚠️ Fast injection failed, falling back to full refresh: {e}")
            import traceback
            traceback.print_exc()
            self._refresh_all_views_after_classification(to_class)
            return True

        return True

    def _get_visible_classes_for_slot(self, slot: int):
        """
        slot 0 = Main View, slot 1..4 = Cross Section views.
        Returns:
        - []  : if palette exists but nothing is visible (block classification)
        - None: if no palette available (do not filter)
        - list of visible class codes otherwise
        """
        dialog = getattr(self.app, "display_mode_dialog", None) or getattr(self.app, "display_dialog", None)

        palette = None

        # Priority 1: dialog palettes (most accurate)
        if dialog is not None and hasattr(dialog, "view_palettes") and slot in dialog.view_palettes:
            palette = dialog.view_palettes.get(slot)

        # Priority 2: app stored palettes
        if (not palette) and hasattr(self.app, "view_palettes"):
            palette = self.app.view_palettes.get(slot)

        # Priority 3: fallback main palette for slot 0, isolated seed for slots 1..5
        if not palette:
            if slot == 0:
                palette = getattr(self.app, "class_palette", None)
            elif hasattr(self.app, "_get_cross_section_palette"):
                try:
                    palette = self.app._get_cross_section_palette(
                        slot,
                        allow_default_seed=True,
                        persist_seed=False,
                    )
                except Exception:
                    palette = None

        if not palette:
            return None  # no filtering possible

        visible = [int(code) for code, info in palette.items() if info.get("show", False)]
        return visible  # can be [] intentionally
 
    def _filter_from_classes(self, mask):
        """If from-classes are set, keep only those; otherwise return mask.

        ⚡ O(K) filter — checks only the K selected points (np.flatnonzero
        on the mask), not all N=39M classes. The naive `np.isin(classes, fc)`
        version scans the entire classification array (~100–200 ms on 39M
        points) and was a major bottleneck for main-view rect/circle/freehand
        classify (where this function is called from _apply_mask_and_record).
        """
        fc = getattr(self.app, "from_classes", None)
        if not fc:
            return mask
        classes = self.app.data["classification"]
        sel_idx = np.flatnonzero(mask)
        if sel_idx.size == 0:
            return mask
        keep = np.isin(classes[sel_idx], np.asarray(fc, dtype=classes.dtype))
        out = np.zeros_like(mask)
        out[sel_idx[keep]] = True
        return out
 
    def _classify_rectangle_main(self, p1, p2, to_class):
        """Rectangle selection on plan view (XY)."""
        xyz = self.app.data["xyz"]
        x1, y1 = p1
        x2, y2 = p2
        xmin, xmax = sorted([x1, x2])
        ymin, ymax = sorted([y1, y2])
        mask = (xyz[:, 0] >= xmin) & (xyz[:, 0] <= xmax) & (xyz[:, 1] >= ymin) & (xyz[:, 1] <= ymax)
        # _apply_mask_and_record calls _filter_from_classes internally — no need to call it here
        self._apply_mask_and_record(mask, to_class)

    def _classify_circle_main(self, center, radius, to_class):
        """Circle selection on plan view (XY). Bounding-box pre-filter for large datasets."""
        xyz = self.app.data["xyz"]
        cx, cy = center
        r2 = radius * radius
        # BB pre-filter eliminates ~75% of points before the distance check
        bb = (xyz[:, 0] >= cx - radius) & (xyz[:, 0] <= cx + radius) & \
             (xyz[:, 1] >= cy - radius) & (xyz[:, 1] <= cy + radius)
        cands = np.flatnonzero(bb)
        final_mask = np.zeros(len(xyz), dtype=bool)
        if cands.size > 0:
            dx = xyz[cands, 0] - cx
            dy = xyz[cands, 1] - cy
            final_mask[cands[(dx * dx + dy * dy) <= r2]] = True
        # _apply_mask_and_record calls _filter_from_classes internally — no need to call it here
        self._apply_mask_and_record(final_mask, to_class)
 
    def _classify_polygon_main(self, poly2d, to_class):
        """Polygon/freehand selection on plan view (XY)."""
        xs = np.array([p[0] for p in poly2d], dtype=np.float64)
        ys = np.array([p[1] for p in poly2d], dtype=np.float64)
        xmin, xmax = xs.min(), xs.max()
        ymin, ymax = ys.min(), ys.max()
 
        xy = self.app.data["xyz"][:, :2]
        bbmask = (xy[:, 0] >= xmin) & (xy[:, 0] <= xmax) & (xy[:, 1] >= ymin) & (xy[:, 1] <= ymax)
        candidates = np.flatnonzero(bbmask)
 
        if candidates.size == 0:
            self._apply_mask_and_record(bbmask, to_class)  # no points
            return
 
        # Fast point-in-polygon using matplotlib, with fallback
        try:
            from matplotlib.path import Path
            path = Path(np.c_[xs, ys], closed=True)
            inside = path.contains_points(xy[candidates])
            final_mask = np.zeros(len(xy), dtype=bool)
            final_mask[candidates[inside]] = True
        except Exception:
            # Ray casting fallback
            final_mask = np.zeros(len(xy), dtype=bool)
            poly = np.c_[xs, ys]
            for idx in candidates:
                x, y = xy[idx]
                c = False
                j = len(poly) - 1
                for i in range(len(poly)):
                    xi, yi = poly[i]
                    xj, yj = poly[j]
                    if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
                        c = not c
                    j = i
                if c:
                    final_mask[idx] = True
 
        final_mask = self._filter_from_classes(final_mask)
        self._apply_mask_and_record(final_mask, to_class)
 
    def _classify_brush_main(self, center, radius, to_class):
        """Brush = circle selection at current mouse position (plan view)."""
        self._classify_circle_main(center, radius, to_class)
 
    def _classify_point_main(self, center, radius, to_class):
        """Classify exactly ONE nearest eligible point in plan view (XY)."""
        xyz = self.app.data["xyz"]
        if xyz is None or len(xyz) == 0:
            return

        try:
            cx = float(center[0])
            cy = float(center[1])
            r = float(radius)
        except Exception:
            return

        if (not np.isfinite(cx)) or (not np.isfinite(cy)):
            return
        if (not np.isfinite(r)) or r <= 0.0:
            r = 0.5
        r2 = r * r

        # Use main-view spatial grid when available (same speed strategy as brush).
        _xyz_now = self.app.data.get("xyz")
        if (getattr(self, "_main_grid_index", None) is None
                or getattr(self, "_brush_spatial_index_xyz_id", None) != id(_xyz_now)):
            self._build_spatial_index_for_brush()

        grid = getattr(self, "_main_grid_index", None)
        if grid is not None and getattr(grid, "valid", False):
            candidates = grid.query_rectangle_minmax(cx - r, cy - r, cx + r, cy + r)
        else:
            bb = (
                (xyz[:, 0] >= cx - r) & (xyz[:, 0] <= cx + r) &
                (xyz[:, 1] >= cy - r) & (xyz[:, 1] <= cy + r)
            )
            candidates = np.flatnonzero(bb).astype(np.int64, copy=False)

        if candidates.size == 0:
            return

        pts2 = xyz[candidates, :2]
        dx = pts2[:, 0] - cx
        dy = pts2[:, 1] - cy
        dist2 = dx * dx + dy * dy
        in_radius = dist2 <= r2
        if not np.any(in_radius):
            return

        candidates = candidates[in_radius]
        dist2 = dist2[in_radius]

        classes = self.app.data["classification"]
        eligible = np.ones(candidates.shape[0], dtype=bool)

        visible_classes = self._get_visible_classes_for_slot(0)
        if visible_classes == []:
            return
        if visible_classes is not None:
            eligible &= np.isin(
                classes[candidates],
                np.asarray(visible_classes, dtype=classes.dtype),
            )

        fc = getattr(self.app, "from_classes", None)
        if fc:
            eligible &= np.isin(
                classes[candidates],
                np.asarray(fc, dtype=classes.dtype),
            )

        if not np.any(eligible):
            return

        candidates = candidates[eligible]
        dist2 = dist2[eligible]
        chosen_idx = int(candidates[int(np.argmin(dist2))])

        mask = np.zeros(len(xyz), dtype=bool)
        mask[chosen_idx] = True
        self._apply_mask_and_record(mask, to_class)
        
    def _draw_circle_preview_main(self, P1, P2):
        """✅ PERFORMANCE FIX: Circle preview for MAIN VIEW (XY plane). One-time VTK pipeline init."""
        renderWindow = self.interactor.GetRenderWindow()
        renderer = renderWindow.GetRenderers().GetFirstRenderer()

        x1, y1 = P1[0], P1[1]
        x2, y2 = P2[0], P2[1]
        center_x = (x1 + x2) / 2.0
        center_y = (y1 + y2) / 2.0
        radius = np.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2) / 2.0
        if radius < 0.01:
            return

        num_segments = 64

        # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
        if not hasattr(self, "_circle_main_pts") or getattr(self, "circle_actor_main", None) is None:
            from vtkmodules.vtkRenderingCore import vtkPolyDataMapper2D, vtkActor2D, vtkCoordinate as _C

            self._circle_main_pts   = vtkPoints()
            self._circle_main_lines = vtkCellArray()
            self._circle_main_poly  = vtkPolyData()
            self._circle_main_poly.SetPoints(self._circle_main_pts)
            self._circle_main_poly.SetLines(self._circle_main_lines)

            mapper = vtkPolyDataMapper2D()
            mapper.SetInputData(self._circle_main_poly)
            dc = _C(); dc.SetCoordinateSystemToDisplay()
            mapper.SetTransformCoordinate(dc)

            self.circle_actor_main = vtkActor2D()
            self.circle_actor_main.SetMapper(mapper)
            setattr(self.circle_actor_main, "_is_classification_preview", True)
            setattr(self.circle_actor_main, "_naksha_preview_owner", "classification")
            prop = self.circle_actor_main.GetProperty()
            prop.SetColor(1.0, 1.0, 0.0)
            prop.SetLineWidth(2)
            prop.SetOpacity(1.0)

            renderer.AddActor2D(self.circle_actor_main)

        if not renderer.HasViewProp(self.circle_actor_main):
            renderer.AddActor2D(self.circle_actor_main)
        self.circle_actor_main.VisibilityOn()

        # ── FAST GEOMETRY UPDATE ─────────────────────────────────────────────
        if not hasattr(self, "_circle_main_coord"):
            from vtkmodules.vtkRenderingCore import vtkCoordinate as _C
            self._circle_main_coord = _C()
            self._circle_main_coord.SetCoordinateSystemToWorld()

        coord = self._circle_main_coord
        theta = np.linspace(0, 2 * np.pi, num_segments + 1)
        circle_x = center_x + radius * np.cos(theta)
        circle_y = center_y + radius * np.sin(theta)

        self._circle_main_pts.Reset()
        self._circle_main_lines.Reset()

        for i in range(num_segments + 1):
            coord.SetValue(circle_x[i], circle_y[i], 0.0)
            d = coord.GetComputedDisplayValue(renderer)
            self._circle_main_pts.InsertNextPoint(d[0], d[1], 0)
            if i > 0:
                self._circle_main_lines.InsertNextCell(2)
                self._circle_main_lines.InsertCellPoint(i - 1)
                self._circle_main_lines.InsertCellPoint(i)

        self._circle_main_pts.Modified()
        self._circle_main_poly.Modified()
        # Render batched by caller

    def _draw_circle_preview(self, P1, P2):
        """
        ✅ PERFORMANCE FIX: Reuse Actor2D — no RemoveActor/new objects per frame.
        """
        vtk_widget = self._get_active_vtk_widget()
        if vtk_widget is None:
            return
        renderer = vtk_widget.renderer

        u1, z1 = self._get_view_coordinates(P1)
        u2, z2 = self._get_view_coordinates(P2)
        center_u = (u1 + u2) / 2.0
        center_z = (z1 + z2) / 2.0
        radius = float(np.hypot(u2 - u1, z2 - z1)) / 2.0
        if radius < 0.01:
            return

        num_segments = 64

        # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
        if not hasattr(self, "_circle_pts") or self.circle_actor is None:
            from vtkmodules.vtkRenderingCore import vtkPolyDataMapper2D, vtkActor2D, vtkCoordinate as _C

            self._circle_pts   = vtkPoints()
            self._circle_lines = vtkCellArray()
            self._circle_poly  = vtkPolyData()
            self._circle_poly.SetPoints(self._circle_pts)
            self._circle_poly.SetLines(self._circle_lines)

            mapper = vtkPolyDataMapper2D()
            mapper.SetInputData(self._circle_poly)
            dc = _C(); dc.SetCoordinateSystemToDisplay()
            mapper.SetTransformCoordinate(dc)

            self.circle_actor = vtkActor2D()
            self.circle_actor.SetMapper(mapper)
            setattr(self.circle_actor, "_is_classification_preview", True)
            setattr(self.circle_actor, "_naksha_preview_owner", "classification")
            prop = self.circle_actor.GetProperty()
            prop.SetColor(1.0, 1.0, 0.0)
            prop.SetLineWidth(2)
            prop.SetOpacity(1.0)

            renderer.AddActor2D(self.circle_actor)

        if not renderer.HasViewProp(self.circle_actor):
            renderer.AddActor2D(self.circle_actor)
        self.circle_actor.VisibilityOn()

        # ── FAST GEOMETRY UPDATE ─────────────────────────────────────────────
        if not hasattr(self, "_circle_coord"):
            from vtkmodules.vtkRenderingCore import vtkCoordinate as _C
            self._circle_coord = _C()
            self._circle_coord.SetCoordinateSystemToWorld()

        coord = self._circle_coord
        self._circle_pts.Reset()
        self._circle_lines.Reset()

        theta = np.linspace(0, 2 * np.pi, num_segments + 1)
        circle_u = center_u + radius * np.cos(theta)
        circle_z = center_z + radius * np.sin(theta)

        for i in range(num_segments + 1):
            wx, wy, wz = self._view2d_to_world(circle_u[i], circle_z[i])
            coord.SetValue(wx, wy, wz)
            d = coord.GetComputedDisplayValue(renderer)
            self._circle_pts.InsertNextPoint(d[0], d[1], 0)
            if i > 0:
                self._circle_lines.InsertNextCell(2)
                self._circle_lines.InsertCellPoint(i - 1)
                self._circle_lines.InsertCellPoint(i)

        self._circle_pts.Modified()
        self._circle_poly.Modified()
        # Render batched by caller

    def _calculate_perpendicular_coord(self):
        """Calculate perpendicular coordinate ONCE per drawing session"""
        view_mode = getattr(self.app, 'cross_view_mode', 'side')
        active_view = self._get_view_index_from_interactor()
        
        if active_view is None:
            return 0.0
        
        core_mask = getattr(self.app, f"section_{active_view}_core_mask", None)
        if core_mask is None or core_mask.sum() == 0:
            return 0.0
        
        section_xyz = self.app.data["xyz"][core_mask]
        
        if view_mode == 'front':
            return float(np.median(section_xyz[:, 0]))
        else:
            return float(np.median(section_xyz[:, 1]))

    def _force_immediate_refresh(self):
        """
        Force immediate visual refresh after classification.
        DOES NOT re-apply classifications, only updates colors/rendering.
        
        IMPORTANT: This should ONLY refresh cross-section/cut-section views.
        The main view has its own independent display settings and should NOT
        be modified during cross-section classification operations.
        """
        print(f"\n{'='*60}")
        print(f"🔄 FORCING VISUAL REFRESH (CROSS-SECTIONS ONLY)")
        print(f"{'='*60}")
        
        try:
            # DO NOT REFRESH MAIN VIEW OR TRIGGER apply_class_map
            # The main view has independent display mode settings that should
            # not be affected by cross-section classification operations
            print("⏭️ Skipping main view refresh (preserving independent display settings)")
            print("⏭️ NOT calling apply_class_map (would trigger unwanted main view update)")
            
            # 1. Refresh cross-section colors (ALWAYS) - Direct color update only
            if hasattr(self.app, 'section_controller'):
                if hasattr(self.app.section_controller, 'current_section_indices'):
                    if self.app.section_controller.current_section_indices is not None:
                        print("🔄 Refreshing cross-section colors...")
                        # Instead of calling apply_class_map, directly update colors
                        self.app.section_controller.refresh_colors_direct()
                        print("   ✅ Cross-section colors updated")
            
            # 2. Refresh cut-section colors if active (ALWAYS) - Direct color update only
            if hasattr(self.app, 'cut_section_controller'):
                if self.app.cut_section_controller.cut_points is not None:
                    print("🔄 Refreshing cut-section colors...")
                    # Direct color update without triggering apply_class_map
                    self.app.cut_section_controller.refresh_colors_direct()
                    print("   ✅ Cut-section colors updated")
            
            # 3. Update status bar
            self.app.statusBar().showMessage(
                "✅ Cross-section classification updated", 
                3000
            )
            
            print(f"{'='*60}")
            print(f"✅ CROSS-SECTION REFRESH COMPLETE")
            print(f"   Main view display settings preserved")
            print(f"{'='*60}\n")
            
        except Exception as e:
            print(f"\n{'='*60}")
            print(f"⚠️ ERROR DURING REFRESH: {e}")
            print(f"{'='*60}\n")
            import traceback
            traceback.print_exc()

    def test_palette_isolation(dialog):
        """Test that view palettes are truly independent"""
        print("\n" + "="*60)
        print("🧪 TESTING PALETTE ISOLATION")
        print("="*60)
        
        # Test 1: Check dictionary IDs
        print("\n1. Dictionary Identity Test:")
        for i in range(5):
            dict_id = id(dialog.view_palettes[i])
            print(f"   Slot {i}: ID = {dict_id}")
        
        # Test 2: Modify one slot
        print("\n2. Modification Test:")
        print("   Adding test class to slot 2...")
        dialog.view_palettes[2][999] = {"show": True, "description": "TEST", "color": (255, 0, 0), "weight": 1.0}
        
        for i in range(5):
            has_test = 999 in dialog.view_palettes[i]
            print(f"   Slot {i}: Has test class = {has_test} (should be True ONLY for slot 2)")
        
        # Clean up
        if 999 in dialog.view_palettes[2]:
            del dialog.view_palettes[2][999]
        
        print("\n✅ Test complete")
        print("="*60 + "\n")

    def _ensure_target_class_visible_single_view(self, to_class, view_index):
        """
        ✅ SAFE VERSION
        Auto-enable target class ONLY if NO user filter is active.
        """
        try:
            if not hasattr(self.app, 'display_mode_dialog'):
                return

            dialog = self.app.display_mode_dialog

            if view_index == -1:
                target_slot = 0
                view_name = "Main View"
            else:
                target_slot = view_index + 1
                view_name = f"View {view_index + 1}"

            print(f"      🔍 Checking {view_name} (slot {target_slot})...")

            dialog.view_palettes.setdefault(target_slot, {})

            # 🔒 USER FILTER GUARD (THIS IS THE FIX)
            view_palette = dialog.view_palettes[target_slot]
            visible_count = sum(1 for v in view_palette.values() if v.get("show", False))
            total_count = len(view_palette)

            if 0 < visible_count < total_count:
                print(f"         🔒 User filter active - NOT auto-enabling")
                return

            if to_class in view_palette and view_palette[to_class].get("show", False):
                print(f"         ✓ Class {to_class} already visible")
                return

            table = dialog.table
            for row in range(table.rowCount()):
                code = int(table.item(row, 1).text())
                if code == to_class:
                    class_desc = table.item(row, 2).text()
                    color = table.item(row, 5).background().color().getRgb()[:3]
                    weight_item = table.item(row, 6)
                    weight = float(weight_item.text()) if weight_item else 1.0

                    dialog.view_palettes[target_slot][to_class] = {
                        "show": True,
                        "description": class_desc,
                        "color": tuple(map(int, color)),
                        "weight": float(weight)
                    }

                    print(f"         ✅ Enabled class {to_class} in {view_name}")

                    if target_slot == 0:
                        self.app.class_palette[to_class]["show"] = True

                    if dialog.current_slot == target_slot:
                        chk = table.cellWidget(row, 0)
                        if chk:
                            chk.setChecked(True)

                    break

        except Exception as e:
            print(f"         ⚠️ Error: {e}")
            import traceback
            traceback.print_exc()

    def _refresh_only_active_dock(self):
        """Refresh ONLY the currently active cross-section dock."""
        try:
            active = getattr(self.app.section_controller, "active_view", None)
            if active is None:
                return

            # Extract changed indices from the changed mask
            if hasattr(self.app, '_last_changed_mask') and self.app._last_changed_mask is not None:
                changed_indices = np.flatnonzero(self.app._last_changed_mask)
            else:
                changed_indices = np.array([], dtype=np.int64)

            # refresh ONLY this view
            self.app.section_controller.active_view = active
            self.app.section_controller.update_section_colors_partial(changed_indices)


            # render its vtk widget
            if active in self.app.section_vtks:
                self._safe_render_pyvista(self.app.section_vtks[active])

            print(f"🔄 Refreshed ONLY Cross-Section View {active+1}")

        except Exception as e:
            print(f"⚠️ Failed refreshing only active dock: {e}")

    def _refresh_changed_points_in_all_docks(self):
        """
        Refresh ONLY the points modified by classification across all docks.
        Uses each dock's own Display Mode palette.
        """

        if not hasattr(self.app, "_last_changed_mask"):
            return

        changed_mask = self.app._last_changed_mask
        if changed_mask.sum() == 0:
            return

        # Extract changed indices from the changed mask
        changed_indices = np.flatnonzero(changed_mask)

        # All cross-section views
        for view_idx, vtk_view in self.app.section_vtks.items():
            # Set active view so refresh uses correct palette
            old = self.app.section_controller.active_view
            self.app.section_controller.active_view = view_idx

            # Refresh only changed points
            self.app.section_controller.update_section_colors_partial(changed_indices)

            # Restore original active view
            self.app.section_controller.active_view = old

        print("🔄 Updated ONLY changed points across all views")
    def _refresh_shaded_mode_partial(self):
        """
        ✅ PARTIAL SHADED REFRESH: Update ONLY triangles containing changed points
        Much faster than full mesh rebuild - only affects classified area
        """
        if self.app.display_mode != "shaded_class":
            return
        
        if not hasattr(self.app, '_last_changed_mask') or self.app._last_changed_mask is None:
            print("⏭️ No changed points mask for shaded refresh")
            return
        
        changed_mask = self.app._last_changed_mask
        num_changed = np.sum(changed_mask)
        
        if num_changed == 0:
            return
        
        print(f"\n{'='*60}")
        print(f"🌗 PARTIAL SHADED REFRESH")
        print(f"   Changed points: {num_changed:,}")
        print(f"{'='*60}")
        
        try:
            import numpy as np
            import pyvista as pv
            
            # ============================================================
            # STEP 1: Get existing mesh from VTK
            # ============================================================
            plotter = getattr(self.app.vtk_widget, "plotter", None)
            if plotter is None or not plotter.renderer.actors:
                print("   ⚠️ No existing shaded mesh - doing full refresh")
                self._refresh_shaded_mode_after_classification()
                return
            
            # Get the first actor (main shaded mesh)
            actor = list(plotter.renderer.actors.values())[0]
            _am = actor.GetMapper() if actor is not None else None
            mesh = _am.GetInput() if _am is not None else None

            if mesh is None or mesh.GetNumberOfCells() == 0:
                print("   ⚠️ No mesh cells - doing full refresh")
                self._refresh_shaded_mode_after_classification()
                return
            
            print(f"   ✅ Found existing mesh with {mesh.GetNumberOfCells()} triangles")
            
            # ============================================================
            # STEP 2: Find which triangles contain changed points
            # ============================================================
            cells = np.asarray(mesh.faces).reshape(-1, 4)[:, 1:]  # Get triangle indices
            
            # Check which triangles have at least one changed vertex
            changed_indices = np.flatnonzero(changed_mask)
            affected_triangles = np.isin(cells, changed_indices).any(axis=1)
            num_affected = np.sum(affected_triangles)
            
            print(f"   📊 Affected triangles: {num_affected:,} / {len(cells):,}")
            
            if num_affected == 0:
                print("   ⏭️ No triangles affected")
                print(f"{'='*60}\n")
                return
            
            # ============================================================
            # STEP 3: Recompute colors ONLY for affected triangles
            # ============================================================
            xyz = self.app.data["xyz"]
            classes = self.app.data["classification"]
            
            # Get shading parameters
            az = np.deg2rad(getattr(self.app, "last_shade_azimuth", 45.0))
            el = np.deg2rad(getattr(self.app, "last_shade_angle", 45.0))
            Ld = np.array([
                np.cos(el) * np.cos(az),
                np.cos(el) * np.sin(az),
                np.sin(el)
            ])
            Ld /= np.linalg.norm(Ld)
            ambient = getattr(self.app, "shade_ambient", 0.2)
            
            # Get current colors array
            current_colors = np.asarray(mesh.GetCellData().GetArray("RGB"))
            
            # Recompute ONLY affected triangles
            affected_indices = np.flatnonzero(affected_triangles)
            
            for idx in affected_indices:
                face = cells[idx]
                
                # Recompute normal
                v1, v2, v3 = xyz[face[0]], xyz[face[1]], xyz[face[2]]
                fn = np.cross(v2 - v1, v3 - v1)
                fn /= np.linalg.norm(fn) + 1e-9
                
                # Recompute shading
                shade = ambient + (1 - ambient) * np.clip(np.dot(fn, Ld), 0, 1)
                
                # Get majority class (with CURRENT classifications)
                c = classes[face]
                majority = np.bincount(c).argmax()
                entry = self.app.class_palette.get(int(majority), {"color": (128, 128, 128)})
                
                # Check visibility
                if not entry.get("show", True):
                    current_colors[idx] = [0, 0, 0]  # Black for hidden
                else:
                    base = np.array(entry["color"], dtype=np.float32)
                    current_colors[idx] = np.clip(base * shade, 0, 255).astype(np.uint8)
            
            print(f"   ✅ Recomputed colors for {len(affected_indices)} triangles")
            
            # ============================================================
            # STEP 4: Update mesh colors (in-place, no rebuild!)
            # ============================================================
            arr = pv.convert_array(current_colors, name="RGB")
            mesh.GetCellData().RemoveArray("RGB")
            mesh.GetCellData().AddArray(arr)
            mesh.GetCellData().SetActiveScalars("RGB")
            mesh.Modified()
            
            # Single render
            self._safe_render_pyvista(self.app.vtk_widget)
            
            print(f"   ✅ Mesh updated in-place (no rebuild!)")
            print(f"{'='*60}\n")
            
        except Exception as e:
            print(f"   ❌ Partial shaded refresh failed: {e}")
            print(f"   ⚠️ Falling back to full refresh")
            import traceback
            traceback.print_exc()
            
            # Fallback to full refresh if partial fails
            self._refresh_shaded_mode_after_classification()
            print(f"{'='*60}\n")

    def _draw_circle_preview_cut(self, P1, P2):
        """✅ PERFORMANCE FIX: Circle preview for CUT SECTION. One-time VTK pipeline init, geometry update only."""
        import math

        vtk_widget = self._get_active_vtk_widget()
        if vtk_widget is None:
            return
        renderer = vtk_widget.renderer

        P1 = tuple(P1) if not isinstance(P1[0], (list, tuple)) else tuple(P1[0])
        P2 = tuple(P2) if not isinstance(P2[0], (list, tuple)) else tuple(P2[0])

        num_segments = 64

        # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
        if not hasattr(self, "_circle_cut_pts") or getattr(self, "circle_actor_cut", None) is None:
            from vtkmodules.vtkRenderingCore import vtkPolyDataMapper2D, vtkActor2D, vtkCoordinate as _C

            self._circle_cut_pts   = vtkPoints()
            self._circle_cut_lines = vtkCellArray()
            self._circle_cut_poly  = vtkPolyData()
            self._circle_cut_poly.SetPoints(self._circle_cut_pts)
            self._circle_cut_poly.SetLines(self._circle_cut_lines)

            mapper = vtkPolyDataMapper2D()
            mapper.SetInputData(self._circle_cut_poly)
            dc = _C(); dc.SetCoordinateSystemToDisplay()
            mapper.SetTransformCoordinate(dc)

            self.circle_actor_cut = vtkActor2D()
            self.circle_actor_cut.SetMapper(mapper)
            prop = self.circle_actor_cut.GetProperty()
            prop.SetColor(1.0, 1.0, 0.0)
            prop.SetLineWidth(2)
            prop.SetOpacity(1.0)

            renderer.AddActor2D(self.circle_actor_cut)

        if not renderer.HasViewProp(self.circle_actor_cut):
            renderer.AddActor2D(self.circle_actor_cut)
        self.circle_actor_cut.VisibilityOn()

        # ── FAST GEOMETRY UPDATE (world → display coords) ────────────────────
        if not hasattr(self, "_circle_cut_coord"):
            from vtkmodules.vtkRenderingCore import vtkCoordinate as _C
            self._circle_cut_coord = _C()
            self._circle_cut_coord.SetCoordinateSystemToWorld()

        coord = self._circle_cut_coord
        coord.SetValue(float(P1[0]), float(P1[1]), float(P1[2]))
        display_p1 = coord.GetComputedDisplayValue(renderer)
        coord.SetValue(float(P2[0]), float(P2[1]), float(P2[2]))
        display_p2 = coord.GetComputedDisplayValue(renderer)

        cx = (display_p1[0] + display_p2[0]) / 2.0
        cy = (display_p1[1] + display_p2[1]) / 2.0
        dx = display_p2[0] - display_p1[0]
        dy = display_p2[1] - display_p1[1]
        radius_display = max(5.0, math.sqrt(dx * dx + dy * dy) / 2.0)

        self._circle_cut_pts.Reset()
        self._circle_cut_lines.Reset()

        for i in range(num_segments + 1):
            angle = 2.0 * math.pi * i / num_segments
            self._circle_cut_pts.InsertNextPoint(
                cx + radius_display * math.cos(angle),
                cy + radius_display * math.sin(angle),
                0.0)
            if i > 0:
                self._circle_cut_lines.InsertNextCell(2)
                self._circle_cut_lines.InsertCellPoint(i - 1)
                self._circle_cut_lines.InsertCellPoint(i)

        self._circle_cut_pts.Modified()
        self._circle_cut_poly.Modified()
        # Render batched by caller



    def _draw_line_preview_cut(self, P1, P2):
        """
        ✅ PERFORMANCE FIX: Line preview for CUT SECTION.
        Reuses Actor2D — no RemoveActor/new objects per frame. Zero print spam.
        """
        from vtkmodules.vtkRenderingCore import vtkCoordinate, vtkPolyDataMapper2D, vtkActor2D

        vtk_widget = self._get_active_vtk_widget()
        if vtk_widget is None:
            return
        renderer = vtk_widget.renderer

        # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
        if not hasattr(self, "_line_cut_pts") or self.line_actor_cut is None:
            self._line_cut_pts = vtkPoints()
            self._line_cut_pts.SetNumberOfPoints(2)
            _lines = vtkCellArray()
            _lines.InsertNextCell(2)
            _lines.InsertCellPoint(0)
            _lines.InsertCellPoint(1)
            self._line_cut_poly = vtkPolyData()
            self._line_cut_poly.SetPoints(self._line_cut_pts)
            self._line_cut_poly.SetLines(_lines)

            mapper = vtkPolyDataMapper2D()
            mapper.SetInputData(self._line_cut_poly)
            dc = vtkCoordinate(); dc.SetCoordinateSystemToDisplay()
            mapper.SetTransformCoordinate(dc)

            self.line_actor_cut = vtkActor2D()
            self.line_actor_cut.SetMapper(mapper)
            prop = self.line_actor_cut.GetProperty()
            prop.SetColor(1, 1, 0)
            prop.SetLineWidth(2)
            prop.SetOpacity(1.0)

            renderer.AddActor2D(self.line_actor_cut)

        if not renderer.HasViewProp(self.line_actor_cut):
            renderer.AddActor2D(self.line_actor_cut)
        self.line_actor_cut.VisibilityOn()

        # ── FAST COORDINATE UPDATE ───────────────────────────────────────────
        if not hasattr(self, "_line_cut_coord"):
            self._line_cut_coord = vtkCoordinate()
            self._line_cut_coord.SetCoordinateSystemToWorld()

        coord = self._line_cut_coord
        coord.SetValue(float(P1[0]), float(P1[1]), float(P1[2]))
        d1 = coord.GetComputedDisplayValue(renderer)
        coord.SetValue(float(P2[0]), float(P2[1]), float(P2[2]))
        d2 = coord.GetComputedDisplayValue(renderer)

        self._line_cut_pts.SetPoint(0, d1[0], d1[1], 0)
        self._line_cut_pts.SetPoint(1, d2[0], d2[1], 0)
        self._line_cut_pts.Modified()
        self._line_cut_poly.Modified()

        # Vertical dashed guides for above/below line
        tool = getattr(self.app, "active_classify_tool", None)
        if tool in ("above_line", "below_line", "parallel_line"):
            self._draw_vertical_guides_cut(P1, P2, tool, renderer)
        # Render batched by caller


    def _draw_vertical_guides_cut(self, P1, P2, tool, renderer):
        """
        ✅ PERFORMANCE FIX: Vertical dashed guides for CUT SECTION above/below line.
        Reuses Actor2D — no RemoveActor/new objects per frame. Zero render call here.
        """
        from vtkmodules.vtkRenderingCore import vtkCoordinate, vtkPolyDataMapper2D, vtkActor2D

        # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
        if not hasattr(self, "_dotted_cut_pts") or self.dotted_actor_cut is None:
            self._dotted_cut_pts   = vtkPoints()
            self._dotted_cut_lines = vtkCellArray()
            self._dotted_cut_poly  = vtkPolyData()
            self._dotted_cut_poly.SetPoints(self._dotted_cut_pts)
            self._dotted_cut_poly.SetLines(self._dotted_cut_lines)

            mapper = vtkPolyDataMapper2D()
            mapper.SetInputData(self._dotted_cut_poly)
            dc = vtkCoordinate(); dc.SetCoordinateSystemToDisplay()
            mapper.SetTransformCoordinate(dc)

            self.dotted_actor_cut = vtkActor2D()
            self.dotted_actor_cut.SetMapper(mapper)
            prop = self.dotted_actor_cut.GetProperty()
            prop.SetColor(1, 1, 0)
            prop.SetLineWidth(2)
            prop.SetOpacity(0.8)

            renderer.AddActor2D(self.dotted_actor_cut)

        if not renderer.HasViewProp(self.dotted_actor_cut):
            renderer.AddActor2D(self.dotted_actor_cut)

        # ── FAST COORDINATE UPDATE ───────────────────────────────────────────
        if not hasattr(self, "_dotted_cut_coord"):
            self._dotted_cut_coord = vtkCoordinate()
            self._dotted_cut_coord.SetCoordinateSystemToWorld()

        coord = self._dotted_cut_coord
        coord.SetValue(float(P1[0]), float(P1[1]), float(P1[2]))
        p1_disp = coord.GetComputedDisplayValue(renderer)
        coord.SetValue(float(P2[0]), float(P2[1]), float(P2[2]))
        p2_disp = coord.GetComputedDisplayValue(renderer)

        if tool == "parallel_line":
            self._draw_parallel_band_guides_cut_display(P1, P2, renderer, p1_disp, p2_disp)
            return

        try:
            win_size = renderer.GetRenderWindow().GetSize()
            height = win_size[1]
        except Exception:
            height = 600

        seg_len = 10
        gap = 6

        pts   = self._dotted_cut_pts
        lines = self._dotted_cut_lines
        pts.Reset()
        lines.Reset()
        idx = 0

        def fill(x_disp, y_start):
            nonlocal idx
            if tool == "above_line":
                y, y_end, step = y_start, height, seg_len + gap
                while y < y_end:
                    y2 = min(y + seg_len, y_end)
                    pts.InsertNextPoint(x_disp, y, 0)
                    pts.InsertNextPoint(x_disp, y2, 0)
                    lines.InsertNextCell(2)
                    lines.InsertCellPoint(idx); lines.InsertCellPoint(idx + 1)
                    idx += 2; y += step
            else:
                y, y_end, step = y_start, 0, seg_len + gap
                while y > y_end:
                    y2 = max(y - seg_len, y_end)
                    pts.InsertNextPoint(x_disp, y, 0)
                    pts.InsertNextPoint(x_disp, y2, 0)
                    lines.InsertNextCell(2)
                    lines.InsertCellPoint(idx); lines.InsertCellPoint(idx + 1)
                    idx += 2; y -= step

        fill(p1_disp[0], p1_disp[1])
        fill(p2_disp[0], p2_disp[1])

        if idx > 0:
            pts.Modified()
            self._dotted_cut_poly.Modified()
            self.dotted_actor_cut.VisibilityOn()
        else:
            self.dotted_actor_cut.VisibilityOff()
        # Render batched by caller

    def _draw_parallel_band_guides_cut_display(self, P1, P2, renderer, p1_disp, p2_disp):
        tol_above, tol_below = self._get_parallel_line_tolerances()
        mid_world = (
            (float(P1[0]) + float(P2[0])) * 0.5,
            (float(P1[1]) + float(P2[1])) * 0.5,
            (float(P1[2]) + float(P2[2])) * 0.5,
        )
        vtk_widget = self._get_active_vtk_widget()
        if vtk_widget is None:
            self.dotted_actor_cut.VisibilityOff()
            return

        view_units_per_px = float(self._pixel_radius_to_view_radius(vtk_widget, mid_world, 1.0))
        if (not np.isfinite(view_units_per_px)) or (view_units_per_px <= 1e-9):
            self.dotted_actor_cut.VisibilityOff()
            return

        above_px = float(tol_above / view_units_per_px)
        below_px = float(tol_below / view_units_per_px)
        if (not np.isfinite(above_px)) or (not np.isfinite(below_px)):
            self.dotted_actor_cut.VisibilityOff()
            return
        rw, rh = renderer.GetSize()
        max_px = max(float(rw), float(rh), 1.0) * 4.0
        above_px = max(-max_px, min(above_px, max_px))
        below_px = max(-max_px, min(below_px, max_px))

        x1, y1 = float(p1_disp[0]), float(p1_disp[1])
        x2, y2 = float(p2_disp[0]), float(p2_disp[1])
        dx = x2 - x1
        dy = y2 - y1
        seg_len = float(np.hypot(dx, dy))
        if seg_len <= 1e-9:
            self.dotted_actor_cut.VisibilityOff()
            return

        nx = -dy / seg_len
        ny = dx / seg_len
        # Screen-space Y grows downward. Keep "above" toward visual upward.
        if ny > 0.0:
            nx, ny = -nx, -ny

        up1 = (x1 + nx * above_px, y1 + ny * above_px, 0.0)
        up2 = (x2 + nx * above_px, y2 + ny * above_px, 0.0)
        lo1 = (x1 - nx * below_px, y1 - ny * below_px, 0.0)
        lo2 = (x2 - nx * below_px, y2 - ny * below_px, 0.0)

        pts = self._dotted_cut_pts
        lines = self._dotted_cut_lines
        pts.Reset()
        lines.Reset()
        for p in (up1, up2, lo1, lo2):
            if not (np.isfinite(p[0]) and np.isfinite(p[1])):
                self.dotted_actor_cut.VisibilityOff()
                return
            pts.InsertNextPoint(*p)
        lines.InsertNextCell(2)
        lines.InsertCellPoint(0)
        lines.InsertCellPoint(1)
        lines.InsertNextCell(2)
        lines.InsertCellPoint(2)
        lines.InsertCellPoint(3)
        pts.Modified()
        self._dotted_cut_poly.Modified()
        self.dotted_actor_cut.VisibilityOn()

    def _draw_rectangle_preview_cut(self, P1, P2):
        """✅ PERFORMANCE FIX: Rectangle preview for CUT SECTION. One-time VTK pipeline init."""
        vtk_widget = self._get_active_vtk_widget()
        if vtk_widget is None:
            return
        renderer = vtk_widget.renderer

        # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
        if not hasattr(self, "_rect_cut_pts") or getattr(self, "rect_actor_cut", None) is None:
            from vtkmodules.vtkRenderingCore import vtkCoordinate, vtkPolyDataMapper2D, vtkActor2D

            self._rect_cut_pts = vtkPoints()
            self._rect_cut_pts.SetNumberOfPoints(5)
            _lines = vtkCellArray()
            _lines.InsertNextCell(5)
            for i in range(5):
                _lines.InsertCellPoint(i)
            self._rect_cut_poly = vtkPolyData()
            self._rect_cut_poly.SetPoints(self._rect_cut_pts)
            self._rect_cut_poly.SetLines(_lines)

            mapper = vtkPolyDataMapper2D()
            mapper.SetInputData(self._rect_cut_poly)
            dc = vtkCoordinate(); dc.SetCoordinateSystemToDisplay()
            mapper.SetTransformCoordinate(dc)

            self.rect_actor_cut = vtkActor2D()
            self.rect_actor_cut.SetMapper(mapper)
            prop = self.rect_actor_cut.GetProperty()
            prop.SetColor(1, 1, 0)
            prop.SetLineWidth(2)
            prop.SetOpacity(1.0)

            renderer.AddActor2D(self.rect_actor_cut)

        if not renderer.HasViewProp(self.rect_actor_cut):
            renderer.AddActor2D(self.rect_actor_cut)
        self.rect_actor_cut.VisibilityOn()

        # ── FAST COORDINATE UPDATE (world → display) ─────────────────────────
        if not hasattr(self, "_rect_cut_coord"):
            from vtkmodules.vtkRenderingCore import vtkCoordinate
            self._rect_cut_coord = vtkCoordinate()
            self._rect_cut_coord.SetCoordinateSystemToWorld()

        coord = self._rect_cut_coord
        coord.SetValue(P1[0], P1[1], P1[2])
        d1 = coord.GetComputedDisplayValue(renderer)
        coord.SetValue(P2[0], P2[1], P2[2])
        d2 = coord.GetComputedDisplayValue(renderer)

        x1, y1 = d1[0], d1[1]
        x2, y2 = d2[0], d2[1]
        corners = [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]
        for i, (cx, cy) in enumerate(corners):
            self._rect_cut_pts.SetPoint(i, cx, cy, 0)

        self._rect_cut_pts.Modified()
        self._rect_cut_poly.Modified()
        # Render batched by caller


    def _draw_brush_preview_cut(self, center, radius_world):
        """Brush preview for CUT SECTION - same as circle."""
        self._draw_circle_preview_cut(center, radius_world)


    
    def _draw_freehand_preview_cut(self, live_point=None):
        """Smooth freehand preview for CUT SECTION. One-time init, Catmull-Rom curves per frame."""
        if len(self.drawing_points) < 1:
            return

        all_pts = list(self.drawing_points_display_cut) if hasattr(self, "drawing_points_display_cut") else []
        if live_point:
            all_pts.append(live_point)
        if len(all_pts) < 2:
            return

        # ── SMOOTH: Catmull-Rom on screen-space (display) coords ─────────────
        all_pts = self._catmull_rom_smooth(all_pts, steps=10)

        vtk_widget = self._get_active_vtk_widget()
        if vtk_widget is None:
            return
        renderer = vtk_widget.renderer

        n = len(all_pts)

        # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
        if not hasattr(self, "_freehand_cut_pts") or getattr(self, "freehand_actor_cut", None) is None:
            from vtkmodules.vtkRenderingCore import vtkPolyDataMapper2D, vtkActor2D

            self._freehand_cut_pts   = vtkPoints()
            self._freehand_cut_lines = vtkCellArray()
            self._freehand_cut_poly  = vtkPolyData()
            self._freehand_cut_poly.SetPoints(self._freehand_cut_pts)
            self._freehand_cut_poly.SetLines(self._freehand_cut_lines)

            mapper = vtkPolyDataMapper2D()
            mapper.SetInputData(self._freehand_cut_poly)

            self.freehand_actor_cut = vtkActor2D()
            self.freehand_actor_cut.SetMapper(mapper)
            prop = self.freehand_actor_cut.GetProperty()
            prop.SetColor(1, 0.8, 0)
            prop.SetLineWidth(2)
            prop.SetOpacity(0.8)

            renderer.AddActor2D(self.freehand_actor_cut)

        if not renderer.HasViewProp(self.freehand_actor_cut):
            renderer.AddActor2D(self.freehand_actor_cut)
        self.freehand_actor_cut.VisibilityOn()

        # ── FAST GEOMETRY UPDATE (display coords — no world conversion) ──────
        pts   = self._freehand_cut_pts
        lines = self._freehand_cut_lines
        pts.Reset()
        lines.Reset()

        for pt in all_pts:
            pts.InsertNextPoint(pt[0], pt[1], 0.0)
        for i in range(n - 1):
            lines.InsertNextCell(2)
            lines.InsertCellPoint(i)
            lines.InsertCellPoint(i + 1)

        pts.Modified()
        self._freehand_cut_poly.Modified()
        self._safe_render(vtk_widget)


    def _draw_brush_cursor_cut(self, center, radius=1.0):
        """Brush cursor for CUT SECTION - dashed circle with crosshair."""
        from vtkmodules.vtkRenderingCore import vtkCoordinate, vtkPolyDataMapper2D, vtkActor2D
        from vtkmodules.vtkCommonCore import vtkPoints
        from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
        import numpy as np
        
        print(f"🖌️ _draw_brush_cursor_cut called: center={center}, radius={radius}")
        
        vtk_widget = self._get_active_vtk_widget()
        if vtk_widget is None:
            print("❌ No VTK widget for brush cursor (cut section)")
            return
        
        # ✅ CRITICAL FIX: Get renderer properly
        if hasattr(vtk_widget, 'renderer'):
            renderer = vtk_widget.renderer
        elif hasattr(vtk_widget, 'GetRenderer'):
            renderer = vtk_widget.GetRenderer()
        else:
            rw = vtk_widget.GetRenderWindow()
            renderer = rw.GetRenderers().GetFirstRenderer()
        
        print(f"   ✅ Got renderer: {renderer}")
        
        # Remove previous brush actor
        if hasattr(self, "brush_actor_cut") and self.brush_actor_cut:
            try:
                renderer.RemoveActor2D(self.brush_actor_cut)
                print("   🧹 Removed old brush_actor_cut")
            except Exception as e:
                print(f"   ⚠️ Could not remove old brush_actor_cut: {e}")
            self.brush_actor_cut = None
        
        cu, cv = center
        
        # Force minimum radius
        if radius < 0.05:
            radius = 0.1
        
        print(f"   📐 Creating brush shape: center=({cu:.2f}, {cv:.2f}), radius={radius:.2f}")
        
        # ========== CREATE BRUSH SHAPE IN WORLD COORDINATES ==========
        num_segments = 32
        brush_world = []
        
        # Outer circle (every other segment for dashed effect)
        for i in range(0, num_segments, 2):  # Skip every other segment
            angle1 = 2 * np.pi * i / num_segments
            angle2 = 2 * np.pi * (i + 1) / num_segments
            
            u1 = cu + radius * np.cos(angle1)
            v1 = cv + radius * np.sin(angle1)
            brush_world.append(self._view2d_to_world(u1, v1))
            
            u2 = cu + radius * np.cos(angle2)
            v2 = cv + radius * np.sin(angle2)
            brush_world.append(self._view2d_to_world(u2, v2))
        
        # Add crosshair lines
        crosshair_size = radius * 0.3  # 30% of radius
        
        # Horizontal crosshair
        brush_world.append(self._view2d_to_world(cu - crosshair_size, cv))
        brush_world.append(self._view2d_to_world(cu + crosshair_size, cv))
        
        # Vertical crosshair
        brush_world.append(self._view2d_to_world(cu, cv - crosshair_size))
        brush_world.append(self._view2d_to_world(cu, cv + crosshair_size))
        
        # Center dot (small circle)
        dot_segments = 8
        dot_radius = radius * 0.05
        for i in range(dot_segments + 1):
            angle = 2 * np.pi * i / dot_segments
            u = cu + dot_radius * np.cos(angle)
            v = cv + dot_radius * np.sin(angle)
            brush_world.append(self._view2d_to_world(u, v))
        
        print(f"   ✅ Created {len(brush_world)} world coordinate points")
        
        # ========== CONVERT TO DISPLAY COORDINATES ==========
        pts = vtkPoints()
        coord = vtkCoordinate()
        coord.SetCoordinateSystemToWorld()
        
        for point in brush_world:
            coord.SetValue(point[0], point[1], point[2])
            display_pos = coord.GetComputedDisplayValue(renderer)
            pts.InsertNextPoint(display_pos[0], display_pos[1], 0)
        
        print(f"   ✅ Converted to display coordinates")
        
        # ========== CREATE LINE SEGMENTS ==========
        lines = vtkCellArray()
        
        # Dashed circle (pairs of points)
        num_circle_points = num_segments  # Each dash is 2 points
        for i in range(0, num_circle_points, 2):
            lines.InsertNextCell(2)
            lines.InsertCellPoint(i)
            lines.InsertCellPoint(i + 1)
        
        # Horizontal crosshair
        h_start = num_circle_points
        lines.InsertNextCell(2)
        lines.InsertCellPoint(h_start)
        lines.InsertCellPoint(h_start + 1)
        
        # Vertical crosshair
        v_start = h_start + 2
        lines.InsertNextCell(2)
        lines.InsertCellPoint(v_start)
        lines.InsertCellPoint(v_start + 1)
        
        # Center dot
        dot_start = v_start + 2
        for i in range(dot_segments):
            lines.InsertNextCell(2)
            lines.InsertCellPoint(dot_start + i)
            lines.InsertCellPoint(dot_start + i + 1)
        
        # ========== CREATE 2D ACTOR ==========
        poly = vtkPolyData()
        poly.SetPoints(pts)
        poly.SetLines(lines)
        
        mapper = vtkPolyDataMapper2D()
        mapper.SetInputData(poly)
        
        coordinate = vtkCoordinate()
        coordinate.SetCoordinateSystemToDisplay()
        mapper.SetTransformCoordinate(coordinate)
        
        self.brush_actor_cut = vtkActor2D()
        self.brush_actor_cut.SetMapper(mapper)
        
        # Set properties - Yellow like other tools
        prop = self.brush_actor_cut.GetProperty()
        prop.SetColor(1, 1, 0)  # Yellow
        prop.SetLineWidth(3)
        prop.SetOpacity(0.9)
        
        # Add as 2D actor (always on top!)
        renderer.AddActor2D(self.brush_actor_cut)
        print(f"   ✅ Added brush_actor_cut to renderer")
        print(f"   📊 Renderer now has {renderer.GetActors2D().GetNumberOfItems()} 2D actors")
        
        # ✅ CRITICAL: Force immediate render
        try:
            if hasattr(vtk_widget, 'GetRenderWindow'):
                self._safe_render(vtk_widget)
            elif hasattr(vtk_widget, 'render'):
                self._safe_render_pyvista(vtk_widget)
            print(f"   ✅ Render called successfully")
        except Exception as e:
            print(f"   ⚠️ Render failed: {e}")
            
    def _draw_brush_cursor_cut_display(self, center_world, radius_px=20.0):
        """
        ✅ PERFORMANCE FIX: Brush cursor for CUT SECTION in DISPLAY coords.
        Reuses actor — zero allocation per frame after init.
        """
        from vtkmodules.vtkRenderingCore import vtkCoordinate, vtkPolyDataMapper2D, vtkActor2D

        vtk_widget = self._get_active_vtk_widget()
        if vtk_widget is None:
            return
        renderer = vtk_widget.renderer

        # Convert center to display coords
        coord = vtkCoordinate()
        coord.SetCoordinateSystemToWorld()
        coord.SetValue(float(center_world[0]), float(center_world[1]), float(center_world[2]))
        c_disp = coord.GetComputedDisplayValue(renderer)
        cx, cy = float(c_disp[0]), float(c_disp[1])
        pixel_radius = max(6.0, float(radius_px))

        num_segments = 32
        dot_segments = 8

        # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
        if not hasattr(self, "_brush_cut_pts") or self.brush_actor_cut is None:
            if getattr(self, "brush_actor_cut", None) is not None:
                try:
                    renderer.RemoveActor2D(self.brush_actor_cut)
                except Exception:
                    pass
                self.brush_actor_cut = None

            self._brush_cut_pts   = vtkPoints()
            self._brush_cut_lines = vtkCellArray()
            self._brush_cut_poly  = vtkPolyData()
            self._brush_cut_poly.SetPoints(self._brush_cut_pts)
            self._brush_cut_poly.SetLines(self._brush_cut_lines)

            mapper = vtkPolyDataMapper2D()
            mapper.SetInputData(self._brush_cut_poly)
            dc = vtkCoordinate(); dc.SetCoordinateSystemToDisplay()
            mapper.SetTransformCoordinate(dc)

            self.brush_actor_cut = vtkActor2D()
            self.brush_actor_cut.SetMapper(mapper)
            prop = self.brush_actor_cut.GetProperty()
            prop.SetColor(1, 1, 0)
            prop.SetLineWidth(3)
            prop.SetOpacity(0.9)

            renderer.AddActor2D(self.brush_actor_cut)

        if not renderer.HasViewProp(self.brush_actor_cut):
            renderer.AddActor2D(self.brush_actor_cut)
        self.brush_actor_cut.VisibilityOn()

        # ── FAST GEOMETRY UPDATE ─────────────────────────────────────────────
        pts   = self._brush_cut_pts
        lines = self._brush_cut_lines
        pts.Reset()
        lines.Reset()

        def add_seg(p0, p1):
            i0 = pts.InsertNextPoint(p0[0], p0[1], 0.0)
            i1 = pts.InsertNextPoint(p1[0], p1[1], 0.0)
            lines.InsertNextCell(2)
            lines.InsertCellPoint(i0)
            lines.InsertCellPoint(i1)

        for i in range(0, num_segments, 2):
            a1 = 2.0 * np.pi * i / num_segments
            a2 = 2.0 * np.pi * (i + 1) / num_segments
            add_seg((cx + pixel_radius * np.cos(a1), cy + pixel_radius * np.sin(a1)),
                    (cx + pixel_radius * np.cos(a2), cy + pixel_radius * np.sin(a2)))

        ch = pixel_radius * 0.3
        add_seg((cx - ch, cy), (cx + ch, cy))
        add_seg((cx, cy - ch), (cx, cy + ch))

        dot_r = max(2.0, pixel_radius * 0.05)
        for i in range(dot_segments):
            a1 = 2.0 * np.pi * i / dot_segments
            a2 = 2.0 * np.pi * (i + 1) / dot_segments
            add_seg((cx + dot_r * np.cos(a1), cy + dot_r * np.sin(a1)),
                    (cx + dot_r * np.cos(a2), cy + dot_r * np.sin(a2)))

        pts.Modified()
        self._brush_cut_poly.Modified()
        # Render batched by caller
        
    def _draw_rectangle_cursor_cut_display(self, center_world, radius_px=20.0):
        """
        ✅ PERFORMANCE FIX: Rectangle brush cursor for CUT SECTION in DISPLAY coords.
        Reuses actor — zero allocation per frame after init.
        """
        from vtkmodules.vtkRenderingCore import vtkCoordinate, vtkPolyDataMapper2D, vtkActor2D

        vtk_widget = self._get_active_vtk_widget()
        if vtk_widget is None:
            return
        renderer = vtk_widget.renderer

        coord = vtkCoordinate()
        coord.SetCoordinateSystemToWorld()
        coord.SetValue(float(center_world[0]), float(center_world[1]), float(center_world[2]))
        c_disp = coord.GetComputedDisplayValue(renderer)
        cx, cy = float(c_disp[0]), float(c_disp[1])
        dx = dy = max(6.0, float(radius_px))
        x_min, x_max = cx - dx, cx + dx
        y_min, y_max = cy - dy, cy + dy

        # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
        if not hasattr(self, "_rect_cut_pts") or self.brush_actor_cut is None:
            if getattr(self, "brush_actor_cut", None) is not None:
                try:
                    renderer.RemoveActor2D(self.brush_actor_cut)
                except Exception:
                    pass
                self.brush_actor_cut = None

            self._rect_cut_pts   = vtkPoints()
            self._rect_cut_lines = vtkCellArray()
            self._rect_cut_poly  = vtkPolyData()
            self._rect_cut_poly.SetPoints(self._rect_cut_pts)
            self._rect_cut_poly.SetLines(self._rect_cut_lines)

            mapper = vtkPolyDataMapper2D()
            mapper.SetInputData(self._rect_cut_poly)
            dc = vtkCoordinate(); dc.SetCoordinateSystemToDisplay()
            mapper.SetTransformCoordinate(dc)

            self.brush_actor_cut = vtkActor2D()
            self.brush_actor_cut.SetMapper(mapper)
            prop = self.brush_actor_cut.GetProperty()
            prop.SetColor(1, 1, 0)
            prop.SetLineWidth(3)
            prop.SetOpacity(0.9)

            renderer.AddActor2D(self.brush_actor_cut)

        if not renderer.HasViewProp(self.brush_actor_cut):
            renderer.AddActor2D(self.brush_actor_cut)
        self.brush_actor_cut.VisibilityOn()

        # ── FAST GEOMETRY UPDATE ─────────────────────────────────────────────
        pts   = self._rect_cut_pts
        lines = self._rect_cut_lines
        pts.Reset()
        lines.Reset()

        corners = [
            (x_min, y_min, 0.0),
            (x_max, y_min, 0.0),
            (x_max, y_max, 0.0),
            (x_min, y_max, 0.0),
            (x_min, y_min, 0.0),
        ]
        for c in corners:
            pts.InsertNextPoint(*c)
        for i in range(1, len(corners)):
            lines.InsertNextCell(2)
            lines.InsertCellPoint(i - 1)
            lines.InsertCellPoint(i)

        pts.Modified()
        self._rect_cut_poly.Modified()
        # Render batched by caller

    # Add this helper method to ClassificationInteractor class

    def _transform_cut_to_world_rect(self, P1, P2):
        """
        Transform rectangle coordinates from cut section's local space 
        to global world space for classification.
        
        Returns: (u_min, u_max, z_min, z_max) in world coordinates
        """
        # Check if we're in cut section mode
        is_cut_section = getattr(self, 'is_cut_section', False) or \
                        (hasattr(self.app, 'cut_section_controller') and
                        getattr(self.app.cut_section_controller, 'is_cut_view_active', False))
        
        if not is_cut_section:
            # Normal cross-section - use coordinates as-is
            u1, z1 = P1[0], P1[2]
            u2, z2 = P2[0], P2[2]
            return (min(u1, u2), max(u1, u2), min(z1, z2), max(z1, z2))
        
        # ✅ CUT SECTION: Transform using cutting plane basis
        try:
            cut_ctrl = self.app.cut_section_controller
            
            # Get the cutting plane's coordinate system
            origin = np.array(cut_ctrl.plane_origin)  # Cut plane origin in world space
            normal = np.array(cut_ctrl.plane_normal)  # Cut plane normal (perpendicular to view)
            
            # Build the cut section's local coordinate system
            # u_axis: along the cutting direction (horizontal in cut view)
            # v_axis: perpendicular to normal and u_axis (vertical in cut view)
            # normal: points "out of screen" in cut view
            
            # Get u_axis from the plane definition
            if hasattr(cut_ctrl, 'u_axis'):
                u_axis = np.array(cut_ctrl.u_axis)
            else:
                # Fallback: compute perpendicular to normal
                if abs(normal[2]) < 0.9:
                    u_axis = np.cross(normal, [0, 0, 1])
                else:
                    u_axis = np.cross(normal, [0, 1, 0])
            
            u_axis = u_axis / np.linalg.norm(u_axis)
            v_axis = np.cross(normal, u_axis)
            v_axis = v_axis / np.linalg.norm(v_axis)
            
            # Transform P1 and P2 from local cut coordinates to world coordinates
            # Local coords: (u, 0, z) where u is horizontal, z is vertical in cut view
            # World coords: origin + u*u_axis + z*v_axis
            
            def local_to_world(p):
                """Convert (u, y, z) in cut section to (x, y, z) in world"""
                u_local = p[0]  # Horizontal in cut view
                z_local = p[2]  # Vertical in cut view
                
                # Transform to world space
                world_pt = origin + u_local * u_axis + z_local * v_axis
                return world_pt
            
            # Transform both corners
            world_p1 = local_to_world(P1)
            world_p2 = local_to_world(P2)
            
            # Now we need to define the rectangle in the cutting plane's 2D space
            # that corresponds to the 3D world rectangle
            
            # Project world corners back to local u,z coordinates
            u1 = np.dot(world_p1 - origin, u_axis)
            z1 = np.dot(world_p1 - origin, v_axis)
            u2 = np.dot(world_p2 - origin, u_axis)
            z2 = np.dot(world_p2 - origin, v_axis)
            
            print(f"   🔄 Cut section rect transform:")
            print(f"      Local: P1=({P1[0]:.2f}, {P1[2]:.2f}), P2=({P2[0]:.2f}, {P2[2]:.2f})")
            print(f"      World: u1={u1:.2f}, z1={z1:.2f}, u2={u2:.2f}, z2={z2:.2f}")
            
            return (min(u1, u2), max(u1, u2), min(z1, z2), max(z1, z2))
            
        except Exception as e:
            print(f"   ⚠️ Cut section transform failed: {e}")
            import traceback
            traceback.print_exc()
            
            # Fallback to direct coordinates
            u1, z1 = P1[0], P1[2]
            u2, z2 = P2[0], P2[2]
            return (min(u1, u2), max(u1, u2), min(z1, z2), max(z1, z2))

    def _partial_refresh_main_view(self, changed_indices):
        """Fast main-view color update (no full rebuild)."""
        try:
            from gui.performance_optimizations import fast_update_colors_optimized
            fast_update_colors_optimized(self.app, changed_mask=changed_indices)
            print(f"⚡ Partial main-view refresh: {len(changed_indices)} points updated")
        except Exception as e:
            print(f"❌ Partial main-view refresh failed: {e}")

    def clear_guides(self):
        """Clear only temporary visual guides (rubber-band line during drawing)"""
        for actor in self.guide_actors:
            self.renderer.RemoveActor(actor)
        self.guide_actors.clear()
    
    def clear_all(self):
        """Clear everything including saved cross-sections (use for reset/clear all)"""
        self.clear_guides()
        for actor in self.section_actors:
            self.renderer.RemoveActor(actor)
        self.section_actors.clear()
    
    def add_section(self, actor):
        """Add a permanent cross-section actor"""
        self.section_actors.append(actor)
        self.renderer.AddActor(actor)
    
    def add_guide(self, actor):
        """Add a temporary guide actor (like rubber-band line)"""
        self.guide_actors.append(actor)
        self.renderer.AddActor(actor)
        
    # Add this method to ClassificationInteractor class:

    def _classify_brush_with_shape(self, center, radius, to_class, section_mask, section_points):
        """
        Classify points using brush with configurable shape (circle or rectangle).
        """
        from ..classification_tools import classify_brush, classify_rectangle
        
        # Get brush shape from app
        brush_shape = getattr(self.app, "brush_shape", "circle")
        
        if brush_shape == "rectangle":
            # Convert center + radius to rectangle bounds
            u, z = center
            u_min = u - radius
            u_max = u + radius
            z_min = z - radius
            z_max = z + radius
            
            print(f"🖌️ Rectangle brush: ({u_min:.2f}, {u_max:.2f}, {z_min:.2f}, {z_max:.2f})")
            
            # Use rectangle classification
            classify_rectangle(
                self.app,
                (u_min, u_max, z_min, z_max),
                self.app.from_classes,
                to_class,
                section_mask,
                section_points,
                None
            )
        else:
            # Default circle brush
            print(f"🖌️ Circle brush: center=({center[0]:.2f}, {center[1]:.2f}), radius={radius:.2f}")
            
            classify_brush(
                self.app,
                center,
                radius,
                self.app.from_classes,
                to_class,
                section_mask,
                section_points,
                None
            )
                
    def _draw_rectangle_cursor(self, center, radius):
        """
        ✅ PERFORMANCE FIX: Rectangle brush cursor in DISPLAY pixels (zoom invariant).
        Reuses actor — zero allocation per frame after init.
        """
        vtk_widget = self._get_active_vtk_widget()
        if vtk_widget is None:
            return
        renderer = vtk_widget.renderer

        x, y = self.interactor.GetEventPosition()
        r = max(float(radius), 3.0)
        x_min, x_max = x - r, x + r
        y_min, y_max = y - r, y + r

        # ── ONE-TIME PIPELINE INIT ───────────────────────────────────────────
        if not hasattr(self, "_rect_cursor_pts") or self.brush_actor is None:
            from vtkmodules.vtkRenderingCore import vtkPolyDataMapper2D, vtkActor2D, vtkCoordinate as _C

            if getattr(self, "brush_actor", None) is not None:
                try:
                    renderer.RemoveActor2D(self.brush_actor)
                except Exception:
                    pass
                self.brush_actor = None

            self._rect_cursor_pts   = vtkPoints()
            self._rect_cursor_lines = vtkCellArray()
            self._rect_cursor_poly  = vtkPolyData()
            self._rect_cursor_poly.SetPoints(self._rect_cursor_pts)
            self._rect_cursor_poly.SetLines(self._rect_cursor_lines)

            mapper = vtkPolyDataMapper2D()
            mapper.SetInputData(self._rect_cursor_poly)
            dc = _C(); dc.SetCoordinateSystemToDisplay()
            mapper.SetTransformCoordinate(dc)

            self.brush_actor = vtkActor2D()
            self.brush_actor.SetMapper(mapper)
            prop = self.brush_actor.GetProperty()
            prop.SetColor(1, 1, 0)
            prop.SetLineWidth(3)
            prop.SetOpacity(0.9)

            renderer.AddActor2D(self.brush_actor)

        if not renderer.HasViewProp(self.brush_actor):
            renderer.AddActor2D(self.brush_actor)
        self.brush_actor.VisibilityOn()

        # ── FAST GEOMETRY UPDATE ─────────────────────────────────────────────
        pts   = self._rect_cursor_pts
        lines = self._rect_cursor_lines
        pts.Reset()
        lines.Reset()

        corners = [
            (x_min, y_min, 0.0),
            (x_max, y_min, 0.0),
            (x_max, y_max, 0.0),
            (x_min, y_max, 0.0),
            (x_min, y_min, 0.0),
        ]
        for c in corners:
            pts.InsertNextPoint(*c)
        for i in range(1, len(corners)):
            lines.InsertNextCell(2)
            lines.InsertCellPoint(i - 1)
            lines.InsertCellPoint(i)

        pts.Modified()
        self._rect_cursor_poly.Modified()
        # Render batched by caller
        
    def _draw_rectangle_cursor_cut(self, center, radius):
        """Draw rectangle cursor for brush tool in CUT SECTION"""
        from vtkmodules.vtkRenderingCore import vtkCoordinate, vtkPolyDataMapper2D, vtkActor2D
        from vtkmodules.vtkCommonCore import vtkPoints
        from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
        
        print(f"■ _draw_rectangle_cursor_cut called: center={center}, radius={radius}")
        
        vtk_widget = self._get_active_vtk_widget()
        if vtk_widget is None:
            print("❌ No VTK widget for rectangle cursor (cut section)")
            return
        
        # ✅ CRITICAL FIX: Get renderer properly
        if hasattr(vtk_widget, 'renderer'):
            renderer = vtk_widget.renderer
        elif hasattr(vtk_widget, 'GetRenderer'):
            renderer = vtk_widget.GetRenderer()
        else:
            rw = vtk_widget.GetRenderWindow()
            renderer = rw.GetRenderers().GetFirstRenderer()
        
        print(f"   ✅ Got renderer: {renderer}")
        
        # Remove previous brush actor
        if hasattr(self, "brush_actor_cut") and self.brush_actor_cut:
            try:
                renderer.RemoveActor2D(self.brush_actor_cut)
            except Exception:
                pass
            self.brush_actor_cut = None
        
        cu, cv = center
        
        # Force minimum radius
        if radius < 0.05:
            radius = 0.1
        
        # Build rectangle corners in world space
        u_min, u_max = cu - radius, cu + radius
        v_min, v_max = cv - radius, cv + radius
        
        corners_world = [
            self._view2d_to_world(u_min, v_min),
            self._view2d_to_world(u_max, v_min),
            self._view2d_to_world(u_max, v_max),
            self._view2d_to_world(u_min, v_max),
            self._view2d_to_world(u_min, v_min)  # Close the rectangle
        ]
        
        # Convert to display coordinates
        pts = vtkPoints()
        coord = vtkCoordinate()
        coord.SetCoordinateSystemToWorld()
        
        for point in corners_world:
            coord.SetValue(point[0], point[1], point[2])
            display_pos = coord.GetComputedDisplayValue(renderer)
            pts.InsertNextPoint(display_pos[0], display_pos[1], 0)
        
        # Create line segments
        lines = vtkCellArray()
        for i in range(len(corners_world) - 1):
            lines.InsertNextCell(2)
            lines.InsertCellPoint(i)
            lines.InsertCellPoint(i + 1)
        
        # Create polydata
        poly = vtkPolyData()
        poly.SetPoints(pts)
        poly.SetLines(lines)
        
        # Create 2D mapper
        mapper = vtkPolyDataMapper2D()
        mapper.SetInputData(poly)
        
        coordinate = vtkCoordinate()
        coordinate.SetCoordinateSystemToDisplay()
        mapper.SetTransformCoordinate(coordinate)
        
        # Create 2D actor
        self.brush_actor_cut = vtkActor2D()
        self.brush_actor_cut.SetMapper(mapper)
        
        # Set properties - Yellow like other tools
        prop = self.brush_actor_cut.GetProperty()
        prop.SetColor(1, 1, 0)  # Yellow
        prop.SetLineWidth(3)
        prop.SetOpacity(0.9)
        
        # Add as 2D actor (always on top!)
        renderer.AddActor2D(self.brush_actor_cut)
        print(f"   ✅ Added brush_actor_cut (rectangle) to renderer")
        
        # ✅ CRITICAL: Force immediate render
        try:
            if hasattr(vtk_widget, 'GetRenderWindow'):
                self._safe_render(vtk_widget)
            elif hasattr(vtk_widget, 'render'):
                self._safe_render_pyvista(vtk_widget)
            print(f"   ✅ Render called successfully")
        except Exception as e:
            print(f"   ⚠️ Render failed: {e}")
            
    def _sync_main_view_palette_weights(self):
        """
        Sync main-view (slot 0) weights only.
        Keeps app.class_palette canonical to slot 0 and prevents cross-slot leakage.
        """
        from gui.classification_state import get_weight_cache
        
        try:
            dialog = getattr(self.app, 'display_mode_dialog', None)
            if not dialog or not hasattr(dialog, 'view_palettes'):
                return
            if 0 not in dialog.view_palettes or not dialog.view_palettes[0]:
                return
             
            weight_cache = get_weight_cache()
            slot0_map = {0: dialog.view_palettes[0]}
             
            # Check if slot-0 weights changed
            if not weight_cache.has_changes(slot0_map):
                return
             
            slot0_changes = weight_cache.get_changed_weights(slot0_map).get(0, {})
            if not slot0_changes:
                return
             
            print(f"   🔄 Syncing {len(slot0_changes)} changed slot-0 weights")
             
            # Update only changed weights
            if not hasattr(self.app, 'class_palette'):
                self.app.class_palette = {}
             
            slot0_palette = dialog.view_palettes[0]
            for class_code, new_weight in slot0_changes.items():
                if class_code not in self.app.class_palette:
                    if class_code in slot0_palette:
                        self.app.class_palette[class_code] = dict(slot0_palette[class_code])
                    else:
                        self.app.class_palette[class_code] = {'weight': new_weight}
                else:
                    self.app.class_palette[class_code]['weight'] = new_weight
             
            # Update cache with slot 0 only
            weight_cache.update_cache(slot0_map)
             
        except Exception as e:
            print(f"⚠️ Delta weight sync failed: {e}")    

    def force_main_view_refresh_with_weights(self):
        """
        ✅ UNIFIED ACTOR: Force weight sync and GPU uniform push.
        Replaces the old full-actor-wipe rebuild — no actor destroy/create needed.
        """
        print(f"\n{'='*60}")
        print(f"💥 FORCING MAIN VIEW WEIGHT REFRESH (UNIFIED GPU POKE)")
        print(f"{'='*60}")

        try:
            self._sync_main_view_palette_weights()

            from gui.unified_actor_manager import sync_palette_to_gpu, is_unified_actor_ready
            app = self.app

            if is_unified_actor_ready(app):
                palette    = getattr(app, 'class_palette', {})
                border_pct = float(getattr(app, 'point_border_percent', 0) or 0.0)
                sync_palette_to_gpu(app, 0, palette, border_pct, render=True)
                print(f"   ✅ Unified GPU poke complete — weights applied without rebuild")
            else:
                # Unified actor not built yet — do a one-time build
                print(f"   ⚠️ Unified actor not ready — triggering build")
                from gui.class_display import update_class_mode
                update_class_mode(app, force_refresh=True)

            print(f"{'='*60}\n")

        except Exception as e:
            print(f"❌ Force refresh failed: {e}")
            import traceback
            traceback.print_exc()
                  
    def _update_brush_preview_colors(self):
        """
        ✅ UNIFIED ACTOR: Instant brush preview via direct _naksha_rgb_ptr write.
        ✅ FIX: Maps global mask → LOD local indices before writing to GPU buffer.
        """
        try:
            if not hasattr(self, '_brush_accumulated_mask'):
                return
            if not np.any(self._brush_accumulated_mask):
                return

            to_class = getattr(self.app, "to_class", None)
            if to_class is None:
                return

            palette = getattr(self.app, "class_palette", {})
            if to_class not in palette:
                return

            target_color = np.array(palette[to_class].get("color", (255, 255, 0)),
                                    dtype=np.uint8)

            if self.app.display_mode != "class":
                return

            # ✅ UNIFIED ACTOR path: write directly into shared GPU colour buffer
            from gui.unified_actor_manager import _get_unified_actor
            actor = _get_unified_actor(self.app)
            if actor is None:
                return

            rgb_ptr = getattr(actor, '_naksha_rgb_ptr', None)
            if rgb_ptr is None or not rgb_ptr.flags.writeable:
                return

            # ✅ FIX: Map global mask through LOD indices
            # rgb_ptr only has len(_main_global_indices) entries, not len(xyz)
            global_indices = getattr(self.app, '_main_global_indices', None)
            if global_indices is not None:
                local_changed = np.flatnonzero(self._brush_accumulated_mask[global_indices])
            else:
                local_changed = np.flatnonzero(self._brush_accumulated_mask)

            if len(local_changed) == 0:
                return

            rgb_ptr[local_changed] = target_color

            vtk_ca = getattr(actor, '_naksha_vtk_array', None)
            if vtk_ca is not None:
                vtk_ca.Modified()
            from gui.unified_actor_manager import _mark_actor_dirty
            _mark_actor_dirty(actor)

            try:
                self._safe_render_pyvista(self.app.vtk_widget)
            except Exception:
                pass

        except Exception:
            # Preview is optional — never break classification on preview failure
            pass

    def _create_brush_paint_overlay(self, rgb_color):
        """
        Add a tiny VTK actor to the main renderer showing freshly painted points.
        Avoids calling vtk_ca.Modified() (the 30 MB GPU upload) during drag;
        only the overlay's small buffer is uploaded each tick.
        """
        try:
            self._destroy_brush_paint_overlay()   # clean any stale instance

            import vtk
            from vtkmodules.vtkFiltersGeneral import vtkVertexGlyphFilter
            from vtkmodules.vtkRenderingCore import vtkPolyDataMapper, vtkActor

            pts = vtk.vtkPoints()
            pts.SetDataTypeToFloat()

            pd = vtk.vtkPolyData()
            pd.SetPoints(pts)

            glyph = vtkVertexGlyphFilter()
            glyph.SetInputData(pd)

            mapper = vtkPolyDataMapper()
            mapper.SetInputConnection(glyph.GetOutputPort())

            actor = vtkActor()
            actor.SetMapper(mapper)
            r = float(rgb_color[0]) / 255.0
            g = float(rgb_color[1]) / 255.0
            b = float(rgb_color[2]) / 255.0
            prop = actor.GetProperty()
            prop.SetColor(r, g, b)
            prop.SetPointSize(float(getattr(self.app, 'point_size', 2.5)))
            prop.LightingOff()

            self.app.vtk_widget.renderer.AddActor(actor)

            self._brush_overlay_actor = actor
            self._brush_overlay_pd    = pd
            self._brush_overlay_pts   = pts
            self._brush_overlay_acc   = np.zeros((0, 3), dtype=np.float32)

        except Exception as _e:
            print(f"⚠️ Brush overlay create: {_e}")
            self._brush_overlay_actor = None

    def _append_brush_paint_overlay(self, global_indices):
        """
        Append newly painted XYZ to the overlay and push the (small) buffer to GPU.
        Called each 30-fps GPU tick during brush drag.
        """
        if getattr(self, '_brush_overlay_actor', None) is None:
            return
        try:
            from vtkmodules.util.numpy_support import numpy_to_vtk

            new_xyz = self.app.data["xyz"][global_indices].astype(np.float32)
            acc = self._brush_overlay_acc
            self._brush_overlay_acc = (
                np.vstack([acc, new_xyz]) if acc.shape[0] else new_xyz
            )
            vtk_arr = numpy_to_vtk(self._brush_overlay_acc, deep=False)
            self._brush_overlay_pts.SetData(vtk_arr)
            self._brush_overlay_pd.Modified()

        except Exception as _e:
            print(f"⚠️ Brush overlay append: {_e}")

    def _destroy_brush_paint_overlay(self):
        """Remove the overlay actor from the renderer and release references."""
        actor = getattr(self, '_brush_overlay_actor', None)
        if actor is not None:
            try:
                self.app.vtk_widget.renderer.RemoveActor(actor)
            except Exception:
                pass
        self._brush_overlay_actor = None
        self._brush_overlay_pd    = None
        self._brush_overlay_pts   = None
        self._brush_overlay_acc   = None

    def _build_spatial_index_for_brush(self):
        """
        SPATIAL GRID INDEX: Vectorized build for O(1) brush lookup in Main View.
        Thread-safe via double-checked locking: safe to call from a background
        thread (pre-warm) and from the main thread (on_left_press guard).
        _build_index_lock is created in __init__ — no lazy init race.
        """
        import time
        xyz = self.app.data["xyz"]

        # Fast path — GIL ensures atomic attribute read; no lock needed here
        if (getattr(self, "_main_grid_index", None) is not None
                and getattr(self, '_brush_spatial_index_xyz_id', None) == id(xyz)):
            return

        with self._build_index_lock:
            # Re-check inside lock (another thread may have built it while we waited)
            if (getattr(self, "_main_grid_index", None) is not None
                    and getattr(self, '_brush_spatial_index_xyz_id', None) == id(xyz)):
                return

            start = time.time()

            if (not hasattr(self, '_main_points_2d')
                    or self._main_points_2d is None
                    or len(self._main_points_2d) != len(xyz)):
                self._main_points_2d = xyz[:, [0, 1]].copy()

            index = SpatialGridIndex(self._main_points_2d)

            # Assign atomically so readers never see a half-built index
            self._main_grid_index = index
            self._brush_spatial_index_xyz_id = id(xyz)

            build_time = (time.time() - start) * 1000
            print(f"   ⚡ Main View Spatial index built in {build_time:.0f}ms ({len(xyz):,} pts)")
        
        
    def _get_points_in_radius_fast(self, center_x, center_y, radius):
        """
        ✅ MICROSTATION O(1) GRID LOOKUP: Get points within radius.
        Uses sort-order slicing — zero Python loops for candidate collection.
        Typical: 0.1-0.5ms per stamp (vs 10-50ms for full scan).
        """
        xyz = self.app.data["xyz"]
        if (self._brush_spatial_index is None
                or getattr(self, '_brush_spatial_index_xyz_id', None) != id(xyz)):
            self._build_spatial_index_for_brush()
        
        params = self._brush_grid_params
        grid_size = params['grid_size']
        sort_order = self._brush_sort_order
        
        # Calculate which grid cells overlap with the brush circle
        cell_x_min = max(0, int((center_x - radius - params['x_min']) / params['x_step']))
        cell_x_max = min(grid_size - 1, int((center_x + radius - params['x_min']) / params['x_step']))
        cell_y_min = max(0, int((center_y - radius - params['y_min']) / params['y_step']))
        cell_y_max = min(grid_size - 1, int((center_y + radius - params['y_min']) / params['y_step']))
        
        # Collect candidate point indices from overlapping cells via sort_order slices
        slices = []
        for cx in range(cell_x_min, cell_x_max + 1):
            for cy in range(cell_y_min, cell_y_max + 1):
                cell_range = self._brush_spatial_index.get((cx, cy))
                if cell_range is not None:
                    s, e = cell_range
                    slices.append(sort_order[s:e])
        
        if not slices:
            return np.array([], dtype=np.int64)
        
        candidates = np.concatenate(slices)
        
        # Fast vectorized distance check on candidates only
        xyz = self.app.data["xyz"]
        pts = xyz[candidates]
        dx = pts[:, 0] - center_x
        dy = pts[:, 1] - center_y
        within = candidates[(dx * dx + dy * dy) <= (radius * radius)]
        return within
    
    def _interpolate_brush_stroke(self, p1, p2, radius):
        """
        ✅ SMOOTH STROKES: Interpolate between positions for gap-free painting
        """
        x1, y1 = p1
        x2, y2 = p2
        
        # Calculate distance
        dx = x2 - x1
        dy = y2 - y1
        dist = np.sqrt(dx*dx + dy*dy)
        
        # Calculate number of interpolation steps
        step_size = self._brush_interpolation_step * radius
        num_steps = max(1, int(dist / step_size))
        
        # Generate interpolated positions
        positions = []
        for i in range(num_steps + 1):
            t = i / num_steps
            x = x1 + dx * t
            y = y1 + dy * t
            positions.append((x, y))
        
        return positions
    
    
    def _get_points_in_box_fast(self, x_min, x_max, y_min, y_max):
        """
        ✅ FAST: Get points in rectangle using spatial index
        """
        # Rebuild index if array identity changed (new file loaded)
        _xyz_now = self.app.data.get("xyz")
        if (self._brush_spatial_index is None
                or getattr(self, '_brush_spatial_index_xyz_id', None) != id(_xyz_now)):
            self._build_spatial_index_for_brush()

        params = self._brush_grid_params
        grid_size = params['grid_size']

        # Calculate overlapping grid cells
        cell_x_min = int((x_min - params['x_min']) / params['x_step'])
        cell_x_max = int((x_max - params['x_min']) / params['x_step'])
        cell_y_min = int((y_min - params['y_min']) / params['y_step'])
        cell_y_max = int((y_max - params['y_min']) / params['y_step'])
        
        # Clip to grid bounds
        cell_x_min = max(0, cell_x_min)
        cell_x_max = min(grid_size - 1, cell_x_max)
        cell_y_min = max(0, cell_y_min)
        cell_y_max = min(grid_size - 1, cell_y_max)
        
        # Collect candidates via sort_order slices (no Python list append)
        sort_order = self._brush_sort_order
        slices = []
        for cx in range(cell_x_min, cell_x_max + 1):
            for cy in range(cell_y_min, cell_y_max + 1):
                cell_range = self._brush_spatial_index.get((cx, cy))
                if cell_range is not None:
                    s, e = cell_range
                    slices.append(sort_order[s:e])

        if not slices:
            return np.array([], dtype=np.int64)

        # Precise box check on candidates
        xyz = self.app.data["xyz"]
        candidates = np.concatenate(slices).astype(np.int64)
        pts = xyz[candidates]
        
        in_box = (
            (pts[:, 0] >= x_min) & (pts[:, 0] <= x_max) &
            (pts[:, 1] >= y_min) & (pts[:, 1] <= y_max)
        )
        
        return candidates[in_box]

    def _finalize_polygon_classification(self):
        """Finalize polygon selection and classify enclosed points."""
        if len(self.drawing_points) < 3:
            if hasattr(self.app, "statusBar"):
                self.app.statusBar().showMessage(
                    "Polygon needs at least 3 vertices. Left-click to add more points.",
                    2500,
                )
            return False

        to_class = getattr(self.app, "to_class", None)
        from_classes = getattr(self.app, "from_classes", None)

        if self._is_main_view():
            self._classify_polygon_main(self.drawing_points, to_class)
            return True

        from ..classification_tools import classify_polygon, _get_cut_section_or_default

        vtk_widget = self._get_active_vtk_widget()
        is_cut_section = bool(
            hasattr(self.app, "cut_section_controller")
            and vtk_widget == getattr(self.app.cut_section_controller, "cut_vtk", None)
        )

        if is_cut_section:
            sec_ctrl = getattr(self.app, "section_controller", None)
            mask = getattr(sec_ctrl, "last_mask", None)
            section_points = getattr(self.app, "section_points", None)
        else:
            v_idx = self._get_view_index_from_interactor()
            if v_idx is not None:
                combined_mask = getattr(self.app, f"section_{v_idx}_combined_mask", None)
                section_points_transformed = getattr(
                    self.app, f"section_{v_idx}_points_transformed", None
                )
                if combined_mask is not None and section_points_transformed is not None:
                    mask = combined_mask
                    section_points = section_points_transformed
                else:
                    mask, section_points = self._get_section_context_for_view(v_idx)
            else:
                mask, section_points = None, None

        section_indices, section_points = _get_cut_section_or_default(
            self.app, mask, section_points
        )
        if section_indices is None or section_points is None:
            return False

        classify_polygon(
            self.app,
            self.drawing_points,
            from_classes,
            to_class,
            mask,
            section_points,
            None,
        )
        self._refresh_all_views_after_classification(to_class)
        return True

    def _append_polygon_vertex_from_cursor(self):
        """
        Capture current cursor location as a polygon vertex before finalize.
        This ensures right-click close includes the last intended corner.
        """
        try:
            x, y = self.interactor.GetEventPosition()
        except Exception:
            return

        try:
            pt = self._display_to_world_fast(float(x), float(y))
            u, v = self._get_view_coordinates(pt)
            uv = (float(u), float(v))
        except Exception:
            return

        add_vertex = True
        if self.drawing_points:
            lu, lv = self.drawing_points[-1]
            # Tiny tolerance prevents duplicate last-point insertion.
            if (abs(uv[0] - float(lu)) <= 1e-6) and (abs(uv[1] - float(lv)) <= 1e-6):
                add_vertex = False

        if add_vertex:
            self.drawing_points.append(uv)

        # Keep cut-section display preview list in sync.
        vtk_widget = self._get_active_vtk_widget()
        is_cut_section = bool(
            hasattr(self.app, "cut_section_controller")
            and vtk_widget == getattr(self.app.cut_section_controller, "cut_vtk", None)
        )
        if is_cut_section:
            if not hasattr(self, "drawing_points_display_cut"):
                self.drawing_points_display_cut = []
            if not hasattr(self, "drawing_points_world_cut"):
                self.drawing_points_world_cut = []
            if not self.drawing_points_display_cut:
                self.drawing_points_display_cut.append((float(x), float(y)))
                self.drawing_points_world_cut.append((float(pt[0]), float(pt[1]), float(pt[2])))
            else:
                lx, ly = self.drawing_points_display_cut[-1]
                if np.hypot(float(x) - float(lx), float(y) - float(ly)) > 1.5:
                    self.drawing_points_display_cut.append((float(x), float(y)))
                    self.drawing_points_world_cut.append((float(pt[0]), float(pt[1]), float(pt[2])))

    def on_right_press(self, obj, evt):
        """Right-click handler for cross-section views."""
        # ✅ MicroStation "Reset Button" behavior: a right-click at any point
        # during an in-progress above/below/parallel-line gesture (awaiting
        # the confirming second tap, or mid-drag) must immediately cancel it
        # and revert to IDLE — never let it silently finalize against a
        # stale P1 on the next left click.
        _rc_tool = getattr(self.app, "active_classify_tool", None)
        if _rc_tool in ("above_line", "below_line", "parallel_line") and (
            getattr(self, "P1", None) is not None
            or getattr(self, "is_dragging", False)
            or getattr(self, "click_to_finalize", False)
        ):
            try:
                if hasattr(obj, "AbortFlagOn"):
                    obj.AbortFlagOn()
                elif hasattr(obj, "SetAbortFlag"):
                    obj.SetAbortFlag(1)
            except Exception:
                pass
            self.P1 = None
            self.is_dragging = False
            self.click_to_finalize = False
            self._press_pos = None
            self._line_press_max_move_px = 0.0
            self._last_line_preview_P2 = None
            try:
                self._clear_all_previews()
            except Exception:
                pass
            if hasattr(self.app, "statusBar"):
                try:
                    self.app.statusBar().showMessage("Line classification cancelled.", 2000)
                except Exception:
                    pass
            return

        if getattr(self.app, "active_classify_tool", None) == "polygon":
            try:
                if hasattr(obj, "AbortFlagOn"):
                    obj.AbortFlagOn()
                elif hasattr(obj, "SetAbortFlag"):
                    obj.SetAbortFlag(1)
            except Exception:
                pass
            try:
                self._append_polygon_vertex_from_cursor()
                if self._finalize_polygon_classification():
                    self._clear_all_previews()
                    if hasattr(self.app, "statusBar"):
                        self.app.statusBar().showMessage(
                            "Polygon classified. Draw next polygon or press Esc to exit.",
                            2200,
                        )
            except Exception as e:
                print(f"⚠️ Polygon finalize failed: {e}")
                import traceback
                traceback.print_exc()
            return

        if self._is_main_view():
            try:
                self.style.OnRightButtonDown()
            except Exception:
                pass
            return
        
        x, y = self.interactor.GetEventPosition()
        
        try:
            pt = self._pick_world_point(x, y)
            
            # ✅ ADD: Set flag to trigger fly-to after next classification
            self._fly_to_after_classify = pt
            
        except Exception as e:
            print(f"⚠️ Right-click handler failed: {e}")


    def _execute_fly_to(self, world_point):
        """Execute fly-to with delay"""
        from PySide6.QtCore import QTimer
        
        # Clear the flag (user chose manual fly-to)
        self._fly_to_after_classify = None
        
        # Add delay to let any pending refresh complete
        QTimer.singleShot(200, lambda: self._fly_to_main_view(world_point))

    def _fly_to_main_view(self, world_point):
        """
        Smoothly fly camera to classification location in main view.
        Shows visual indicator at target location.
        """
        print(f"\n{'='*60}")
        print(f"✈️ FLYING TO CLASSIFICATION LOCATION")
        print(f"{'='*60}")
        print(f"   Target (raw): ({world_point[0]:.2f}, {world_point[1]:.2f}, {world_point[2]:.2f})")
        
        # ✅ CRITICAL FIX: Transform cross-section coordinates to global world coordinates
        try:
            # Check if we're in a cross-section view
            active_view = self._get_view_index_from_interactor()
            
            if active_view is not None:
                # We're in a cross-section - need to transform coordinates
                print(f"   🔄 Transforming from cross-section View {active_view + 1} coordinates...")
                
                # Get section data to find the actual world coordinates
                core_mask = getattr(self.app, f"section_{active_view}_core_mask", None)
                buffer_mask = getattr(self.app, f"section_{active_view}_buffer_mask", None)
                
                if core_mask is not None:
                    # Combine masks
                    if buffer_mask is not None:
                        section_mask = core_mask | buffer_mask
                    else:
                        section_mask = core_mask
                    
                    # Get the actual 3D points in this section
                    section_xyz = self.app.data["xyz"][section_mask]
                    
                    print(f"   📊 Section has {len(section_xyz)} points")
                    
                    # Get the 2D coordinates we clicked on (u, z in cross-section view)
                    u_clicked = world_point[0]
                    z_clicked = world_point[2]
                    
                    print(f"   🎯 Clicked at: u={u_clicked:.2f}, z={z_clicked:.2f}")
                    
                    # Get view mode to know which coordinates to use
                    view_mode = getattr(self.app, 'cross_view_mode', 'side')
                    print(f"   📐 View mode: {view_mode}")
                    
                    # Project section points to 2D (same way classification does it)
                    if view_mode == 'front':
                        # Front view: u is Y coordinate, z is Z coordinate
                        section_2d = np.column_stack([section_xyz[:, 1], section_xyz[:, 2]])
                        print(f"   📐 Using front view projection (Y, Z)")
                    else:
                        # Side view (default): u is X coordinate, z is Z coordinate
                        section_2d = np.column_stack([section_xyz[:, 0], section_xyz[:, 2]])
                        print(f"   📐 Using side view projection (X, Z)")
                    
                    # Find the closest point in the section to where we clicked
                    clicked_2d = np.array([u_clicked, z_clicked])
                    distances = np.sum((section_2d - clicked_2d) ** 2, axis=1)
                    closest_idx = np.argmin(distances)
                    closest_distance = np.sqrt(distances[closest_idx])
                    
                    # Get the actual 3D world coordinates of the closest point
                    world_point = tuple(section_xyz[closest_idx])
                    
                    print(f"   ✅ Found closest point in section:")
                    print(f"      2D distance: {closest_distance:.2f}")
                    print(f"      World coords: ({world_point[0]:.2f}, {world_point[1]:.2f}, {world_point[2]:.2f})")
                else:
                    print(f"   ⚠️ No section mask for View {active_view + 1}")
            
            # Validate against data bounds
            xyz = self.app.data["xyz"]
            x_min, x_max = xyz[:, 0].min(), xyz[:, 0].max()
            y_min, y_max = xyz[:, 1].min(), xyz[:, 1].max()
            z_min, z_max = xyz[:, 2].min(), xyz[:, 2].max()
            
            print(f"   📊 Data bounds:")
            print(f"      X: {x_min:.2f} to {x_max:.2f}")
            print(f"      Y: {y_min:.2f} to {y_max:.2f}")
            print(f"      Z: {z_min:.2f} to {z_max:.2f}")
            
            # Check if transformed point is within reasonable bounds
            if (world_point[0] < x_min or world_point[0] > x_max or
                world_point[1] < y_min or world_point[1] > y_max):
                
                print(f"   ⚠️ Warning: Target may be outside main data bounds")
                print(f"      X: {world_point[0]:.2f} (bounds: {x_min:.2f} to {x_max:.2f})")
                print(f"      Y: {world_point[1]:.2f} (bounds: {y_min:.2f} to {y_max:.2f})")
        
        except Exception as e:
            print(f"   ⚠️ Coordinate transformation failed: {e}")
            import traceback
            traceback.print_exc()
            # Don't return - still try to fly to the point even if transformation failed
        
        try:
            main_plotter = self.app.vtk_widget
            
            # Get current camera
            if hasattr(main_plotter, 'renderer'):
                renderer = main_plotter.renderer
                cam = renderer.GetActiveCamera()
            elif hasattr(main_plotter, 'camera'):
                cam = main_plotter.camera
            else:
                print("   ❌ No camera in main view")
                return
            
            # Calculate new camera position
            target_x, target_y, target_z = world_point
            
            # Get current camera distance to maintain zoom level
            current_pos = np.array(cam.GetPosition())
            current_focal = np.array(cam.GetFocalPoint())
            current_distance = np.linalg.norm(current_pos - current_focal)
            
            # New focal point is the target location at its actual elevation
            new_focal_point = np.array([target_x, target_y, target_z])
            
            # Position camera above the target at current distance
            view_direction = np.array([0, 0, 1])  # Always look straight down in main view
            new_position = new_focal_point + view_direction * current_distance
                        
            print(f"   📷 Current focal: {current_focal}")
            print(f"   📷 New focal: {new_focal_point}")
            print(f"   📷 New position: {new_position}")
            print(f"   📷 Distance: {current_distance:.2f}")

            # ✅ FIX: Delay animation to let refresh complete
            from PySide6.QtCore import QTimer

            def start_animation():
                self._animate_camera_to(
                    main_plotter,
                    new_position,
                    new_focal_point,
                    duration_ms=800
                )

            # Wait 100ms for any pending refreshes to complete
            QTimer.singleShot(100, start_animation)
            
            # Show visual indicator at target
            self._show_target_indicator(world_point)
            
            # Update status bar
            if hasattr(self.app, 'statusBar'):
                self.app.statusBar().showMessage(
                    f"✈️ Flew to ({target_x:.1f}, {target_y:.1f}, {target_z:.1f})",
                    3000
                )
            
            print(f"   ✅ Camera animated to target")
            print(f"{'='*60}\n")
            
        except Exception as e:
            print(f"   ❌ Fly-to failed: {e}")
            import traceback
            traceback.print_exc()

    def _animate_camera_to(self, plotter, new_pos, new_focal, duration_ms=800):
        """
        Smooth camera animation using Qt timer.
        Uses easing curve for natural motion.
        """
        from PySide6.QtCore import QTimer
        import numpy as np
        self.app._fly_to_in_progress = True
        
        try:
            # Get camera
            if hasattr(plotter, 'renderer'):
                cam = plotter.renderer.GetActiveCamera()
            elif hasattr(plotter, 'camera'):
                cam = plotter.camera
            else:
                print("   ⚠️ No camera found for animation")
                return
            
            # Store start state
            start_pos = np.array(cam.GetPosition())
            start_focal = np.array(cam.GetFocalPoint())
            
            end_pos = np.array(new_pos)
            end_focal = np.array(new_focal)
            
            # Animation parameters
            steps = 30
            step_duration = duration_ms // steps
            
            # Easing function (ease in-out cubic)
            def ease_in_out_cubic(t):
                """Smooth easing curve"""
                if t < 0.5:
                    return 4 * t * t * t
                else:
                    p = 2 * t - 2
                    return 1 + p * p * p / 2
            
            current_step = [0]  # Mutable container for closure
            
            def animate_step():
                try:
                    t = current_step[0] / steps
                    eased_t = ease_in_out_cubic(t)
                    
                    # Interpolate position and focal point
                    interp_pos = start_pos + (end_pos - start_pos) * eased_t
                    interp_focal = start_focal + (end_focal - start_focal) * eased_t
                    
                    # Update camera
                    cam.SetPosition(interp_pos.tolist())
                    cam.SetFocalPoint(interp_focal.tolist())
                    
                    # Render
                    if hasattr(plotter, 'GetRenderWindow'):
                        self._safe_render(plotter)
                    elif hasattr(plotter, 'render'):
                        self._safe_render_pyvista(plotter)
                    
                    current_step[0] += 1
                    
                    if current_step[0] <= steps:
                        QTimer.singleShot(step_duration, animate_step)
                    else:
                        print("   ✅ Animation complete")
                        self.app._fly_to_in_progress = False
                        
                except Exception as e:
                    print(f"   ⚠️ Animation step failed: {e}")
            
            # Start animation
            animate_step()
            
        except Exception as e:
            print(f"   ⚠️ Animation setup failed: {e}")
            # Fallback: instant camera moveccccccc
            try:
                cam.SetPosition(new_pos.tolist())
                cam.SetFocalPoint(new_focal.tolist())
                if hasattr(plotter, 'render'):
                    self._safe_render_pyvista(plotter)
            except Exception:
                pass

    def _show_target_indicator(self, world_point):
        """
        Flash MULTIPLE visual indicators at target location.
        Creates sphere + crosshair + rings + label for unmistakable visibility.
        """
        try:
            main_plotter = self.app.vtk_widget
            import pyvista as pv
            
            # Determine appropriate radius based on data bounds
            xyz = self.app.data.get("xyz")
            if xyz is not None:
                data_range = np.max(xyz, axis=0) - np.min(xyz, axis=0)
                base_radius = np.mean(data_range) * 0.008  # Smaller radius (0.8%)
            else:
                base_radius = 1.5
            
            print(f"   ✨ Creating multi-part indicator (base_radius={base_radius:.2f})")
            
            # 1. CENTRAL SPHERE (Smooth, semi-transparent)
            sphere = pv.Sphere(radius=base_radius, center=world_point, theta_resolution=30, phi_resolution=30)
            main_plotter.add_mesh(
                sphere, 
                color='red', 
                opacity=0.6,  # Semi-transparent
                name='fly_to_sphere', 
                lighting=True,  # Enable lighting for better look
                smooth_shading=True
            )
            
            # 2. OUTER RING (Circle at same height)
            ring = pv.Circle(radius=base_radius * 2.5, center=world_point, resolution=64)
            main_plotter.add_mesh(
                ring, 
                color='yellow', 
                line_width=4,
                opacity=0.8,
                name='fly_to_ring', 
                lighting=False
            )
            
            # 3. CROSSHAIR LINES (Thinner, longer)
            crosshair_size = base_radius * 5
            
            # Horizontal line (X-axis)
            h_line = pv.Line(
                [world_point[0] - crosshair_size, world_point[1], world_point[2]],
                [world_point[0] + crosshair_size, world_point[1], world_point[2]]
            )
            main_plotter.add_mesh(
                h_line, 
                color='yellow', 
                line_width=2,
                opacity=0.7,
                name='fly_to_h_line', 
                lighting=False
            )
            
            # Vertical line (Y-axis)
            v_line = pv.Line(
                [world_point[0], world_point[1] - crosshair_size, world_point[2]],
                [world_point[0], world_point[1] + crosshair_size, world_point[2]]
            )
            main_plotter.add_mesh(
                v_line, 
                color='yellow', 
                line_width=2,
                opacity=0.7,
                name='fly_to_v_line', 
                lighting=False
            )
            
            # 4. TEXT LABEL (Above the sphere)
            label_pos = [world_point[0], world_point[1], world_point[2] + base_radius * 4]
            main_plotter.add_point_labels(
                [label_pos], 
                ['🎯 TARGET'],
                point_size=0,  # Hide the point itself
                font_size=28,
                text_color='yellow', 
                name='fly_to_label',
                bold=True, 
                shadow=True,
                always_visible=True
            )
            
            print(f"   ✅ Multi-element indicator created")
            
            # Animate for 12 seconds (10-15 range)
            self._animate_indicator(main_plotter, base_radius, duration_ms=12000)
            
        except Exception as e:
            print(f"   ⚠️ Indicator creation failed: {e}")
            import traceback
            traceback.print_exc()
            
    def _animate_indicator(self, plotter, base_radius, duration_ms=12000):
        """Animate all indicator elements with pulse + expand + fade over 12 seconds"""
        from PySide6.QtCore import QTimer
        
        steps = 120  # More steps for smoother animation over 12 seconds
        step_duration = duration_ms // steps
        current_step = [0]
        
        # Use static singleShot scheduling to avoid accumulating QObject timers
        # on the app object across repeated fly-to animations.
        
        def animate_step():
            try:
                t = current_step[0] / steps
                
                # Gentle fade (starts fading after 60% of time)
                if t < 0.6:
                    opacity = 1.0
                else:
                    fade_progress = (t - 0.6) / 0.4
                    opacity = 1.0 - fade_progress
                
                # Sphere: gentle pulsing
                pulse = 0.15 * np.sin(t * 3 * np.pi)  # 3 slow pulses
                sphere_opacity = max(0, min(1, (opacity * 0.6) + pulse))
                
                # Update actors if they exist
                if 'fly_to_sphere' in plotter.actors:
                    plotter.actors['fly_to_sphere'].GetProperty().SetOpacity(sphere_opacity)
                
                if 'fly_to_ring' in plotter.actors:
                    plotter.actors['fly_to_ring'].GetProperty().SetOpacity(opacity * 0.8)
                    # Gentle ring expansion
                    scale = 1 + t * 0.5  # Grows to 1.5x size slowly
                    plotter.actors['fly_to_ring'].SetScale(scale, scale, 1)
                
                if 'fly_to_h_line' in plotter.actors:
                    plotter.actors['fly_to_h_line'].GetProperty().SetOpacity(opacity * 0.7)
                
                if 'fly_to_v_line' in plotter.actors:
                    plotter.actors['fly_to_v_line'].GetProperty().SetOpacity(opacity * 0.7)
                
                if 'fly_to_label' in plotter.actors:
                    # Label stays visible longer
                    label_opacity = 1.0 if t < 0.8 else (1.0 - (t - 0.8) / 0.2)
                    plotter.actors['fly_to_label'].GetProperty().SetOpacity(max(0, label_opacity))
                
                self._safe_render_pyvista(plotter)
                
                current_step[0] += 1
                
                if current_step[0] <= steps:
                    QTimer.singleShot(step_duration, animate_step)
                else:
                    # Remove all indicators after animation completes
                    indicator_names = ['fly_to_sphere', 'fly_to_ring', 'fly_to_h_line', 
                                    'fly_to_v_line', 'fly_to_label']
                    removed_count = 0
                    
                    for name in indicator_names:
                        if name in plotter.actors:
                            try:
                                plotter.remove_actor(name)
                                removed_count += 1
                                print(f"   ✅ Removed actor: {name}")
                            except Exception as e:
                                print(f"   ⚠️ Failed to remove {name}: {e}")
                    
                    self._safe_render_pyvista(plotter)
                    print(f"   ✅ Animation complete. Removed {removed_count} indicators")
                    
            except Exception as e:
                print(f"   ⚠️ Animation step failed at step {current_step[0]}: {e}")
                import traceback
                traceback.print_exc()
        
        QTimer.singleShot(0, animate_step)


    def _verify_buffer_classification(self, view_idx):
        """
        Debug helper to verify buffer points are being included in classification.
        Call this after classification to confirm buffer points were modified.
        """
        try:
            app = self.app
            
            # Get masks
            core_mask = getattr(app, f"section_{view_idx}_core_mask", None)
            buffer_mask = getattr(app, f"section_{view_idx}_buffer_mask", None)
            combined_mask = getattr(app, f"section_{view_idx}_combined_mask", None)
            
            # Get last changed mask from classification
            last_changed = getattr(app, "_last_changed_mask", None)
            
            if last_changed is None:
                print(f"   ⚠️ No _last_changed_mask found")
                return
            
            print(f"\n{'='*60}")
            print(f"🔍 BUFFER CLASSIFICATION VERIFICATION - View {view_idx}")
            print(f"{'='*60}")
            
            # Count points in each category
            if core_mask is not None:
                core_classified = (last_changed & core_mask).sum()
                print(f"   Core points classified: {core_classified:,}")
            
            if buffer_mask is not None:
                buffer_classified = (last_changed & buffer_mask).sum()
                print(f"   Buffer points classified: {buffer_classified:,}")
                if buffer_classified > 0:
                    print(f"   ✅ SUCCESS: Buffer points WERE classified!")
                else:
                    print(f"   ⚠️ WARNING: No buffer points were classified")
            
            if combined_mask is not None:
                total_classified = (last_changed & combined_mask).sum()
                print(f"   Total points classified: {total_classified:,}")
            
            print(f"{'='*60}\n")
            
        except Exception as e:
            print(f"   ❌ Verification failed: {e}")    

    def verify_brush_buffer_classification(app):
        """
        Debug helper to verify brush tool includes buffer points.
        Call this after brush initialization (after left press).
        """
        print(f"\n{'='*60}")
        print(f"🔍 BRUSH BUFFER VERIFICATION")
        print(f"{'='*60}")
        
        # Check if brush is initialized
        if not hasattr(app, 'section_controller'):
            print("   ⚠️ No section controller")
            return
        
        active_view = getattr(app.section_controller, "active_view", None)
        if active_view is None:
            print("   ⚠️ No active view")
            return
        
        # Check masks
        combined_mask = getattr(app, f"section_{active_view}_combined_mask", None)
        core_mask = getattr(app, f"section_{active_view}_core_mask", None)
        buffer_mask = getattr(app, f"section_{active_view}_buffer_mask", None)
        
        if combined_mask is not None:
            combined_count = combined_mask.sum()
            print(f"   ✅ Combined mask: {combined_count:,} points")
        
        if core_mask is not None:
            core_count = core_mask.sum()
            print(f"   Core mask: {core_count:,} points")
        
        if buffer_mask is not None and core_mask is not None:
            buffer_only = (buffer_mask & ~core_mask).sum()
            print(f"   Buffer mask: {buffer_only:,} exclusive buffer points")
        
        # Check brush initialization
        interactor = None
        for vtk_widget in getattr(app, 'section_vtks', {}).values():
            if hasattr(vtk_widget, 'interactor'):
                interactor = vtk_widget.interactor
                if hasattr(interactor, 'GetInteractorStyle'):
                    style = interactor.GetInteractorStyle()
                    if hasattr(style, '_brush_section_indices'):
                        brush_indices = style._brush_section_indices
                        print(f"\n   🖌️ Brush indices: {len(brush_indices):,}")
                        
                        if combined_mask is not None:
                            expected = combined_mask.sum()
                            if len(brush_indices) == expected:
                                print(f"   ✅ Brush indices match combined mask!")
                            elif core_mask is not None and len(brush_indices) == core_mask.sum():
                                print(f"   ❌ Brush indices only include CORE (missing buffer!)")
                            else:
                                print(f"   ⚠️ Brush indices count unexpected: {len(brush_indices)} vs {expected}")
                        break
        
        print(f"{'='*60}\n")


    # In interactor_classify.py — wherever classification is committed:
    def _commit_classification(self, mask, from_classes, to_class):
        """Apply classification and maintain undo/redo stacks correctly."""
        import numpy as np

        classification = self.app.data["classification"]
        from gui.flight_line_filter import filter_visible_flight_line_indices
        changed_indices = filter_visible_flight_line_indices(
            self.app, np.flatnonzero(mask), len(classification),
            slot=self._active_flight_line_slot(),
        )
        if changed_indices.size == 0:
            return False
        mask = np.zeros_like(mask, dtype=bool)
        mask[changed_indices] = True
        old_classes = classification[changed_indices].copy()

        # Apply to ground truth
        classification[changed_indices] = to_class

        # Push undo step
        step = {
            "indices": changed_indices,
            "old_classes": old_classes,
            "new_classes": np.full(mask.sum(), to_class, dtype=classification.dtype),
        }
        self.app.undo_stack.append(step)

        # *** CRITICAL: Always clear redo when new classification is committed ***
        # Without this, stale redo steps pollute subsequent undo/redo cycles.
        self.app.redo_stack.clear()

        # Cap undo stack
        try:
            from gui.memory_manager import current_recommended_undo_steps
            max_steps = int(current_recommended_undo_steps(self.app))
        except Exception:
            max_steps = int(getattr(self.app, '_max_undo_steps', 30))
        while len(self.app.undo_stack) > max_steps:
            from gui.memory_manager import _free_undo_entry
            _free_undo_entry(self.app.undo_stack.pop(0))

        # Invalidate section mirrors BEFORE emitting so _on_classification_finished
        # gets clean mirrors to work with
        if hasattr(self.app, 'section_vtks'):
            for view_idx in self.app.section_vtks.keys():
                self.app._sync_section_mirror_from_data(view_idx)

        # Emit signal — triggers cross-section refresh via _on_classification_finished
        self.app.classification_finished.emit(mask)