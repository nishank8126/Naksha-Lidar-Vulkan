import numpy as np
import time
import pyvista as pv
from pyvistaqt import QtInteractor
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QDoubleSpinBox, QAbstractSpinBox, QMessageBox, QDockWidget, QPushButton, QHBoxLayout
from PySide6.QtCore import Qt, QTimer
import vtk
from scipy.spatial import cKDTree

# ============ SAFE RENDER HELPER ============
def _safe_vtk_render(vtk_widget):
    """
    Safely render a VTK/PyVista widget. NEVER raises.
    Prevents crash from stale render windows when switching between
    cut section and synchronized views.
    """
    if vtk_widget is None:
        return False
    try:
        # Explicit kill-switch used during shutdown to avoid stale-context renders.
        if bool(getattr(vtk_widget, "_naksha_skip_render", False)):
            return False
        # Check Qt widget is alive
        if hasattr(vtk_widget, 'isVisible') and callable(vtk_widget.isVisible):
            if not vtk_widget.isVisible():
                return False
        # Validate render window
        rw = vtk_widget.GetRenderWindow()
        if rw is None:
            return False
        if rw.GetInteractor() is None:
            return False
        vtk_widget.render()
        return True
    except (RuntimeError, AttributeError, OSError, ReferenceError):
        return False
    except Exception:
        return False

# ============ INTERACTOR STYLE ============
class CutSectionInteractorStyle(vtk.vtkInteractorStyleUser):
    """
    Custom interactor style for cut section tool in cross-section views.
    
    ✅ ENABLES: Zoom (mouse wheel) and Pan (middle-click)
    ✅ ALLOWS: Left-click point selection
    ❌ BLOCKS: Right-click rotation (prevents accidental view changes)
    
    This allows users to navigate the view while placing cut points.
    """
    def __init__(self, app=None, vtk_widget=None):
        """Initialize with proper event handlers for zoom and pan."""
        super().__init__()
        self.app = app
        self.vtk_widget = vtk_widget
        
        # ❌ DO NOT block these - instead handle them properly!
        # self.AddObserver("MouseWheelForwardEvent", lambda obj, evt: None)  # ← REMOVE
        # self.AddObserver("MouseWheelBackwardEvent", lambda obj, evt: None)  # ← REMOVE
        
        # ✅ DO add proper handlers for zoom and pan
        self.AddObserver("MouseWheelForwardEvent", self._on_mouse_wheel_forward)
        self.AddObserver("MouseWheelBackwardEvent", self._on_mouse_wheel_backward)
        self.AddObserver("LeftButtonPressEvent", self._on_left_press)
        self.AddObserver("LeftButtonReleaseEvent", self._on_left_release)
        self.AddObserver("MiddleButtonPressEvent", self._on_middle_press)
        self.AddObserver("MiddleButtonReleaseEvent", self._on_middle_release)
        self.AddObserver("MouseMoveEvent", self._on_mouse_move)
        
        # Block rotation (but allow zoom/pan)
        self.AddObserver("RightButtonPressEvent", self._block_event)
        self.AddObserver("RightButtonReleaseEvent", self._block_event)
        
        # State for panning
        self._is_panning = False
        self._last_pos = (0, 0)
    
    def _block_event(self, obj, event):
        """Block right-click rotation; reactivate last classification tool if none is active."""
        if getattr(self.app, "active_classify_tool", None) is None:
            last_tool = getattr(self.app, "last_classify_tool", None)
            if last_tool and hasattr(self.app, "set_classify_tool"):
                try:
                    from_classes = getattr(self.app, "last_classify_from_classes", None)
                    to_class = getattr(self.app, "last_classify_to_class", None)
                    self.app.from_classes = from_classes
                    self.app.to_class = to_class
                    self.app._right_click_reactivating = True
                    try:
                        self.app.set_classify_tool(last_tool)
                    finally:
                        self.app._right_click_reactivating = False
                except Exception as e:
                    print(f"⚠️ Cut-section right-click reactivate failed: {e}")

    def _on_left_press(self, obj, event):
        try:
            interactor = self.GetInteractor()
            if interactor is None:
                return
            
            cut_controller = getattr(self.app, "cut_section_controller", None) if self.app else None
            is_cut_dock = bool(
                cut_controller is not None
                and self.vtk_widget is getattr(cut_controller, "cut_vtk", None)
            )
            cut_placement_active = bool(
                is_cut_dock
                and getattr(cut_controller, "_cut_source", None) == "cut"
                and getattr(cut_controller, "_state", CutSectionState.IDLE) in (
                    CutSectionState.WAITING_CENTER,
                    CutSectionState.WAITING_DEPTH,
                )
            )
            plain_idle_cut_pan = bool(
                is_cut_dock
                and not cut_placement_active
                and getattr(self.app, "active_classify_tool", None) is None
            )
            if (
                getattr(self.app, "_left_pan_shortcut_active", False)
                and plain_idle_cut_pan
            ):
                self._is_panning = True
                self._last_pos = interactor.GetEventPosition()
                if hasattr(obj, "AbortFlagOn"):
                    obj.AbortFlagOn()
                elif hasattr(obj, "SetAbortFlag"):
                    obj.SetAbortFlag(1)
                return

            if self.app is None or self.vtk_widget is None:
                return
            if getattr(self.app, "zoom_behavior", "center") != "picked_point":
                return
            if hasattr(self.app, "_store_zoom_anchor"):
                self.app._store_zoom_anchor(self.vtk_widget, interactor=interactor)
        except Exception:
            pass

    def _on_left_release(self, obj, event):
        if getattr(self, "_is_panning", False):
            self._is_panning = False
            if hasattr(obj, "AbortFlagOn"):
                obj.AbortFlagOn()
            elif hasattr(obj, "SetAbortFlag"):
                obj.SetAbortFlag(1)
            return

    def _apply_cursor_wheel_zoom(self, interactor, factor, event_source) -> bool:
        """Use the same cursor anchor as every other section viewport."""
        if interactor is None or self.vtk_widget is None:
            return False
        try:
            from gui.cross_section.section_zoom import (
                apply_cursor_anchored_parallel_zoom,
            )

            renderer = getattr(self.vtk_widget, "renderer", None)
            camera = renderer.GetActiveCamera() if renderer is not None else None
            handled = apply_cursor_anchored_parallel_zoom(
                renderer,
                camera,
                factor,
                interactor.GetEventPosition(),
            )
            if not handled:
                return False

            _safe_vtk_render(self.vtk_widget)
            if hasattr(event_source, "AbortFlagOn"):
                event_source.AbortFlagOn()
            elif hasattr(event_source, "SetAbortFlag"):
                event_source.SetAbortFlag(1)
            return True
        except (RuntimeError, AttributeError, OSError, ReferenceError):
            return False
        except Exception:
            return False
    
    def _on_mouse_wheel_forward(self, obj, event):
        """Handle mouse wheel forward (zoom in). ✅ SAFE render."""
        try:
            interactor = self.GetInteractor()
            if interactor is None:
                return

            if self._apply_cursor_wheel_zoom(interactor, 1.2, obj):
                return
            
            render_window = interactor.GetRenderWindow()
            if render_window is None or render_window.GetInteractor() is None:
                return
            renderers = render_window.GetRenderers()
            if renderers is None or renderers.GetNumberOfItems() == 0:
                return
            renderer = renderers.GetFirstRenderer()
            
            if renderer is None:
                return
            
            camera = renderer.GetActiveCamera()
            if camera is None:
                return
            
            camera.Zoom(1.2)
            render_window.Render()
            
        except (RuntimeError, AttributeError, OSError, ReferenceError):
            pass
        except Exception as e:
            print(f"⚠️ Zoom forward error: {e}")
    
    def _on_mouse_wheel_backward(self, obj, event):
        """Handle mouse wheel backward (zoom out). ✅ SAFE render."""
        try:
            interactor = self.GetInteractor()
            if interactor is None:
                return

            if self._apply_cursor_wheel_zoom(interactor, 1.0 / 1.2, obj):
                return
            
            render_window = interactor.GetRenderWindow()
            if render_window is None or render_window.GetInteractor() is None:
                return
            renderers = render_window.GetRenderers()
            if renderers is None or renderers.GetNumberOfItems() == 0:
                return
            renderer = renderers.GetFirstRenderer()
            
            if renderer is None:
                return
            
            camera = renderer.GetActiveCamera()
            if camera is None:
                return
            
            camera.Zoom(0.833)
            render_window.Render()
            
        except (RuntimeError, AttributeError, OSError, ReferenceError):
            pass
        except Exception as e:
            print(f"⚠️ Zoom backward error: {e}")
    
    def _on_middle_press(self, obj, event):
        """Handle middle-click press (start pan)."""
        try:
            interactor = self.GetInteractor()
            if interactor is None:
                return
            
            self._is_panning = True
            self._last_pos = interactor.GetEventPosition()
            print("✋ Pan START (middle-click)")
            
        except Exception as e:
            print(f"⚠️ Middle press error: {e}")
    
    def _on_middle_release(self, obj, event):
        """Handle middle-click release (stop pan)."""
        try:
            self._is_panning = False
            print("✋ Pan STOP (middle-click release)")
        except Exception as e:
            print(f"⚠️ Middle release error: {e}")
    
    def _on_mouse_move(self, obj, event):
        """Handle mouse move for panning. ✅ SAFE: all VTK access guarded."""
        if not self._is_panning:
            return
        
        try:
            interactor = self.GetInteractor()
            if interactor is None:
                return
            
            current_pos = interactor.GetEventPosition()
            
            dx = current_pos[0] - self._last_pos[0]
            dy = current_pos[1] - self._last_pos[1]
            
            if dx == 0 and dy == 0:
                return
            
            render_window = interactor.GetRenderWindow()
            if render_window is None or render_window.GetInteractor() is None:
                return
            renderers = render_window.GetRenderers()
            if renderers is None or renderers.GetNumberOfItems() == 0:
                return
            renderer = renderers.GetFirstRenderer()
            
            if renderer is None:
                return
            
            camera = renderer.GetActiveCamera()
            if camera is None:
                return
            
            size = render_window.GetSize()
            if size[0] == 0 or size[1] == 0:
                return
            
            camera.OrthogonalizeViewUp()

            up = np.asarray(camera.GetViewUp(), dtype=float)
            view_direction = np.asarray(
                camera.GetDirectionOfProjection(), dtype=float
            )
            up_norm = float(np.linalg.norm(up))
            direction_norm = float(np.linalg.norm(view_direction))
            if up_norm <= 1e-12 or direction_norm <= 1e-12:
                return
            up /= up_norm
            view_direction /= direction_norm
            right = np.cross(view_direction, up)
            right_norm = float(np.linalg.norm(right))
            if right_norm <= 1e-12:
                return
            right /= right_norm
            
            # ✅ FIX: Use ParallelScale for orthographic views (correct 1:1 mapping)
            if camera.GetParallelProjection():
                parallel_scale = camera.GetParallelScale()
                scale = (2.0 * parallel_scale) / max(size[1], 1)
            else:
                distance = camera.GetDistance()
                scale = distance / (size[0] * 0.5) if size[0] > 0 else 0
            
            pan_vector = -dx * scale * right - dy * scale * up
            
            current_cam_pos = np.array(camera.GetPosition())
            new_pos = current_cam_pos + pan_vector
            camera.SetPosition(new_pos)
            
            focal = np.array(camera.GetFocalPoint())
            new_focal = focal + pan_vector
            camera.SetFocalPoint(new_focal)
            
            self._last_pos = current_pos
            
            try:
                render_window.Render()
            except (RuntimeError, AttributeError, OSError, ReferenceError):
                pass
            
        except (RuntimeError, AttributeError, OSError, ReferenceError):
            self._is_panning = False
        except Exception as e:
            print(f"⚠️ Mouse move pan error: {e}")

# ============ STATE ENUM ============
class CutSectionState:
    IDLE = 0
    WAITING_CENTER = 1
    WAITING_DEPTH = 2
    FINALIZED = 3

def safe_lut_indexing(lut, classes):
    """
    Safely index into LUT array, preventing memory access violations.
    
    This is CRITICAL for stability when classification codes exceed LUT size.
    Uses np.clip to clamp indices to valid range [0, len(lut)-1].
    
    Args:
        lut: numpy array of shape (N, 3) with RGB colors
        classes: numpy array of classification codes
        
    Returns:
        numpy array of RGB colors for each class
    """
    import numpy as np
    
    # Ensure classes are integers
    classes = np.asarray(classes, dtype=int)
    
    # Clamp class indices to valid LUT range
    max_lut_idx = len(lut) - 1
    safe_classes = np.clip(classes, 0, max_lut_idx)
    
    # Warn if we had to clamp (helps debugging)
    if np.any(classes != safe_classes):
        out_of_bounds = classes[classes != safe_classes]
        unique_oob = np.unique(out_of_bounds)
        print(f"⚠️ WARNING: {len(out_of_bounds)} points with out-of-bounds classifications")
        print(f"   Out-of-bounds codes: {unique_oob.tolist()}")
        print(f"   LUT size: {len(lut)} (max index: {max_lut_idx})")
        print(f"   → Clamped to valid range to prevent crash")
    
    return lut[safe_classes]

# ============ CONTROLLER ============
class CutSectionController:
    """
    Cut Section Controller - Works like Cross-Section with modern inline dock UI.
    ✅ State machine for robust state management
    ✅ Real-time refresh like cross-section
    ✅ Independent cut window (doesn't override cross-section)
    ✅ NO floating dialog - all depth control in dock
    ✅ Real-time classification updates (cut + main + cross-section sync)
    """
    def __init__(self, app):
        self.app = app
        self.cut_palette = {}

        self._is_destroying = False
        # State machine
        self._state = CutSectionState.IDLE
        self.cut_phase = 0
        self.center_point = None
        self.dynamic_depth = getattr(app, "default_cut_width", 1.0)

        # ✅ ADD THIS: Track saved interactor styles per view
        self._saved_interactor_styles = {}
        self._cut_source = None
        self._suspended_cross_pan_button = None

        # Data
        self.cut_points = None
        self._cut_index_map = None
        self._kdtree_cache = None
        self.section_tangent = None
        self.is_cut_view_active = False

        # Visuals - cross-section preview
        self._cross_camera_state = None
        self.active_vtk = None
        self.line_actor = None
        self.buffer_actor_upper = None
        self.buffer_actor_lower = None

        # Visuals - cut dock preview
        self.cut_preview_upper = None
        self.cut_preview_lower = None

        # Config
        self.tail_length = 3.0
        self.buffer_display_width = 0.5
        self.cut_yaw_deg = 0.0

        # Per-view observer ids
        self._view_observer_ids = {}

        # UI state
        self._cut_camera_state = None
        self._is_refreshing = False
        self._old_classify_interactor = None
        self._cut_preview_render_pending = False
        self._cut_preview_render_interval_ms = 16
        # Cut-in-Cut hot-path state.  Reuse one picker and invalidate stale
        # preview timers instead of allocating/finishing VTK work per mouse event.
        self._cut_world_picker = vtk.vtkWorldPointPicker()
        self._cut_preview_render_generation = 0
        self._cut_fast_rearm_count = 0

        # Cut-in-Cut drag hot path.
        #
        # Exact vtkWorldPointPicker picking is retained for mouse CLICKS so
        # finalized cut coordinates/mapping remain unchanged. Mouse MOVE
        # previews use camera projection only, avoiding synchronous picker /
        # Z-buffer work in the high-frequency drag path.
        self._cut_preview_pending_depth_ui = None
        self._cut_preview_base_signature = None
        self._cut_preview_motion_events = 0
        self._cut_preview_render_frames = 0
        self._cut_preview_render_ms_total = 0.0
        self._cut_preview_map_ms_total = 0.0
        self._cut_preview_profile_last_print = time.perf_counter()

        # Dedicated cut section widgets (with INLINE DEPTH)
        self.cut_vtk = None
        self.cut_dock = None
        self._cut_vtk_finalized = False
        self._force_close_requested = False
        self._cut_right_click_observer_id = None
        self.cut_core_actor = None
        self.cut_buffer_actor = None
        self.depth_label = None
        self.depth_spin = None
        
        # State saves
        self._saved_section_state = None
        self._original_section_points = None
        self._original_section_indices = None
        
        #
        self.cut_level = 0
        self.parent_cut_points = None
        self.parent_cut_index_map = None
        self.cut_history = []
        self._cut_source_dialog = None
        self._cut_source_dialog_open = False

        # ✅ ADD: MicroStation-style rotation tracking
        self.original_section_tangent = None  # Store initial cross-section line direction
        self.accumulated_rotation = 0  # Track total rotation (0°, 90°, 180°, 270°)
        self._depth_adjustment_started = False

    def _adaptive_cut_preview_interval_ms(self) -> int:
        """
        Preview cadence.

        Cut-in-Cut uses zero-delay event-loop coalescing. The VTK render itself
        provides back-pressure; adding a fixed 16 ms timer after a 16 ms mouse
        throttle creates visible drag latency. Cross->Cut keeps the historical
        adaptive cadence.
        """
        if getattr(self, "_cut_source", None) == "cut":
            return 0

        try:
            section_controller = getattr(self.app, "section_controller", None)
            adaptive_interval = getattr(
                section_controller,
                "_adaptive_preview_interval_ms",
                None,
            )
            if callable(adaptive_interval):
                return max(16, int(adaptive_interval()))
        except Exception:
            pass

        try:
            point_count = len(self.cut_points) if self.cut_points is not None else 0
        except Exception:
            point_count = 0
        if point_count >= 500_000:
            return 33
        if point_count >= 150_000:
            return 22
        return 16

    def _cut_cursor_world_on_focal_plane(self, pos):
        """
        Convert a cut-dock display position to the camera focal plane without
        vtkWorldPointPicker.

        This is preview-only. Exact left clicks still use vtkWorldPointPicker,
        preserving the existing finalized cut position and nested index logic.
        """
        if self.cut_vtk is None:
            return None

        ren = getattr(self.cut_vtk, "renderer", None)
        if ren is None:
            return None

        try:
            cam = ren.GetActiveCamera()
            if cam is None:
                return None

            if cam.GetParallelProjection():
                size = ren.GetSize()
                origin = ren.GetOrigin()
                width = max(int(size[0]), 1)
                height = max(int(size[1]), 1)

                x = float(pos[0]) - float(origin[0])
                y = float(pos[1]) - float(origin[1])
                nx = (x / float(width) - 0.5) * 2.0
                ny = (y / float(height) - 0.5) * 2.0

                focal = np.asarray(cam.GetFocalPoint(), dtype=float)
                up = np.asarray(cam.GetViewUp(), dtype=float)
                dop = np.asarray(cam.GetDirectionOfProjection(), dtype=float)

                up_norm = float(np.linalg.norm(up))
                dop_norm = float(np.linalg.norm(dop))
                if up_norm <= 1e-12 or dop_norm <= 1e-12:
                    return None
                up /= up_norm
                dop /= dop_norm

                right = np.cross(dop, up)
                right_norm = float(np.linalg.norm(right))
                if right_norm <= 1e-12:
                    return None
                right /= right_norm

                half_h = float(cam.GetParallelScale())
                half_w = half_h * (float(width) / float(height))

                return (
                    focal
                    + right * (nx * half_w)
                    + up * (ny * half_h)
                )

            # Unexpected perspective-camera fallback. Matrix projection only;
            # still no picker/Z-buffer readback.
            focal = cam.GetFocalPoint()
            ren.SetWorldPoint(
                float(focal[0]),
                float(focal[1]),
                float(focal[2]),
                1.0,
            )
            ren.WorldToDisplay()
            display_z = float(ren.GetDisplayPoint()[2])
            ren.SetDisplayPoint(
                float(pos[0]),
                float(pos[1]),
                display_z,
            )
            ren.DisplayToWorld()
            world = ren.GetWorldPoint()
            w = float(world[3])
            if abs(w) <= 1e-12:
                return None
            return np.asarray(
                (world[0] / w, world[1] / w, world[2] / w),
                dtype=float,
            )
        except Exception:
            return None

    def _schedule_cut_preview_render(self) -> None:
        """
        Coalesce Cut-in-Cut preview frames with minimum latency.

        Nested cut:
            every mouse event updates the latest line transform
            -> one zero-delay Qt render is queued
            -> intermediate cursor positions are automatically collapsed

        This removes the previous double throttle:
            16 ms mouse throttle + 16 ms render timer.
        """
        if self._cut_preview_render_pending:
            return

        delay_ms = int(self._adaptive_cut_preview_interval_ms())
        self._cut_preview_render_interval_ms = delay_ms
        self._cut_preview_render_pending = True
        scheduled_widget = self.cut_vtk
        scheduled_generation = int(
            getattr(self, "_cut_preview_render_generation", 0)
        )

        def _flush():
            # Always clear first. A stale generation must not permanently
            # leave the scheduler marked pending.
            self._cut_preview_render_pending = False

            if scheduled_generation != int(
                getattr(self, "_cut_preview_render_generation", 0)
            ):
                return
            if getattr(self, "_is_destroying", False):
                return
            if scheduled_widget is None or scheduled_widget is not self.cut_vtk:
                return

            pending_depth = getattr(
                self,
                "_cut_preview_pending_depth_ui",
                None,
            )
            self._cut_preview_pending_depth_ui = None
            if pending_depth is not None and self.depth_spin is not None:
                try:
                    self.depth_spin.blockSignals(True)
                    self.depth_spin.setValue(float(pending_depth))
                    self.depth_spin.blockSignals(False)
                except Exception:
                    pass

            t0 = time.perf_counter()
            rendered = _safe_vtk_render(scheduled_widget)
            render_ms = (time.perf_counter() - t0) * 1000.0

            if getattr(self, "_cut_source", None) == "cut" and rendered:
                self._cut_preview_render_frames = int(
                    getattr(self, "_cut_preview_render_frames", 0)
                ) + 1
                self._cut_preview_render_ms_total = float(
                    getattr(self, "_cut_preview_render_ms_total", 0.0)
                ) + render_ms

                # Low-noise profiler: at most about once every two seconds.
                now = time.perf_counter()
                last_print = float(
                    getattr(self, "_cut_preview_profile_last_print", now)
                )
                if now - last_print >= 2.0:
                    frames = max(
                        int(getattr(self, "_cut_preview_render_frames", 0)),
                        1,
                    )
                    events = int(
                        getattr(self, "_cut_preview_motion_events", 0)
                    )
                    avg_render = float(
                        getattr(self, "_cut_preview_render_ms_total", 0.0)
                    ) / frames
                    avg_map = float(
                        getattr(self, "_cut_preview_map_ms_total", 0.0)
                    ) / max(events, 1)
                    print(
                        "CUT_PREVIEW_PROFILE "
                        f"events={events} frames={frames} "
                        f"map_avg={avg_map:.3f}ms "
                        f"render_avg={avg_render:.2f}ms "
                        "picker_on_move=0 fixed_timer_delay=0ms"
                    )
                    self._cut_preview_motion_events = 0
                    self._cut_preview_render_frames = 0
                    self._cut_preview_render_ms_total = 0.0
                    self._cut_preview_map_ms_total = 0.0
                    self._cut_preview_profile_last_print = now

        QTimer.singleShot(max(delay_ms, 0), _flush)

    def _reset_depth_adjustment_guard(self):
        """Require an actual depth adjustment before accepting finalize click."""
        self._depth_adjustment_started = False

    def _mark_depth_adjustment_started(self):
        self._depth_adjustment_started = True

    def _mark_cut_vtk_active(self):
        """Reset one-shot finalize guard when a fresh cut VTK widget is active."""
        self._cut_vtk_finalized = False

    def _on_cut_view_right_click_reactivate(self, obj, event):
        """Reactivate the last classification tool from an idle cut view."""
        if self._is_destroying or getattr(self.app, "_shutdown_in_progress", False):
            return
        if getattr(self.app, "active_classify_tool", None) is not None:
            return

        last_tool = getattr(self.app, "last_classify_tool", None)
        if not last_tool or not hasattr(self.app, "set_classify_tool"):
            return

        try:
            if hasattr(obj, "AbortFlagOn"):
                obj.AbortFlagOn()
            elif hasattr(obj, "SetAbortFlag"):
                obj.SetAbortFlag(1)

            print(f"Right-click detected in cut section - reactivating {last_tool}")
            self.app.from_classes = getattr(
                self.app, "last_classify_from_classes", None
            )
            self.app.to_class = getattr(self.app, "last_classify_to_class", None)
            self.app._right_click_reactivating = True
            try:
                self.app.set_classify_tool(last_tool)
            finally:
                self.app._right_click_reactivating = False
        except Exception as e:
            print(f"Cut-section right-click reactivate failed: {e}")

    def _remove_cut_right_click_observer(self):
        """Detach the cut-view right-click observer before widget teardown."""
        observer_id = self._cut_right_click_observer_id
        self._cut_right_click_observer_id = None
        if observer_id is not None and self.cut_vtk is not None:
            try:
                self.cut_vtk.interactor.RemoveObserver(observer_id)
            except Exception:
                pass

        # Same teardown discipline for the locate click/move observers
        # (added alongside the right-click observer at cut_vtk creation) --
        # without this they'd dangle on the about-to-be-closed widget
        # instead of being detached like every other observer here.
        locate_id = getattr(self, "_cut_locate_observer_id", None)
        self._cut_locate_observer_id = None
        if locate_id is not None and self.cut_vtk is not None:
            try:
                self.cut_vtk.interactor.RemoveObserver(locate_id)
            except Exception:
                pass

        move_id = getattr(self, "_cut_locate_move_observer_id", None)
        self._cut_locate_move_observer_id = None
        if move_id is not None and self.cut_vtk is not None:
            try:
                self.cut_vtk.interactor.RemoveObserver(move_id)
            except Exception:
                pass

    def _finalize_cut_render_window_once(self, vtk_widget=None):
        target = vtk_widget if vtk_widget is not None else self.cut_vtk
        if target is None:
            return False
        if self._cut_vtk_finalized:
            return False

        rw = None
        try:
            rw = target.GetRenderWindow()
        except Exception:
            rw = None

        if rw is None:
            self._cut_vtk_finalized = True
            return False

        try:
            if hasattr(rw, "SetAbortRender"):
                rw.SetAbortRender(1)
        except Exception:
            pass

        try:
            if hasattr(rw, "SetMapped"):
                rw.SetMapped(False)
        except Exception:
            pass

        # ✅ PATCH: Disconnect the interactor BEFORE Finalize() so VTK
        # does not attempt wglMakeCurrent on the HWND while it's still alive.
        try:
            iren = rw.GetInteractor()
            if iren is not None:
                rw.SetInteractor(None)
                iren.SetRenderWindow(None)
        except Exception:
            pass

        # ✅ PATCH: Switch to off-screen so no Win32 context is needed for Finalize.
        try:
            rw.SetOffScreenRendering(True)
        except Exception:
            pass

        prev_warn_state = None
        try:
            # Suppress expected VTK Win32 teardown warnings for stale HWND/OpenGL handles.
            prev_warn_state = vtk.vtkObject.GetGlobalWarningDisplay()
            vtk.vtkObject.SetGlobalWarningDisplay(0)
        except Exception:
            prev_warn_state = None

        try:
            rw.Finalize()
            self._cut_vtk_finalized = True
            return True
        except Exception:
            self._cut_vtk_finalized = True   # mark done even on failure
            return False
        finally:
            if prev_warn_state is not None:
                try:
                    vtk.vtkObject.SetGlobalWarningDisplay(int(prev_warn_state))
                except Exception:
                    pass

    def apply_palette(self, palette):
        """
        Apply Display Mode palette to cut section view.
        ✅ Stores palette independently - survives new cuts and classifications
        ✅ Normalizes class codes to int (prevents key mismatch issues)
        
        Args:
            palette: Dictionary from Display Mode (view_palettes[5])
        """
        if not palette or not isinstance(palette, dict):
            print("   ⚠️ Empty/invalid palette provided")
            return

        # ✅ Normalize + deep-copy (avoid references + ensure int keys)
        new_palette = {}
        for code, info in palette.items():
            try:
                code_int = int(code)
            except Exception:
                # Skip non-numeric codes safely
                continue

            info = info or {}
            new_palette[code_int] = {
                "show": bool(info.get("show", False)),
                "description": str(info.get("description", "")),
                "color": tuple(info.get("color", (128, 128, 128))),
                "weight": float(info.get("weight", 1.0)),
            }

        self.cut_palette = new_palette
        print(f"   ✅ Cut palette updated: {len(self.cut_palette)} classes")

        if not self.is_cut_view_active or self.cut_vtk is None:
            print("   ℹ️ Cut section view not active - palette stored for next cut")
            return

        # Refresh view with new palette
        try:
            refreshed = False
            try:
                from gui.unified_actor_manager import sync_palette_to_gpu
                border_pct = float(getattr(self.app, "view_borders", {}).get(5, 0.0))
                refreshed = bool(sync_palette_to_gpu(self.app, 5, self.cut_palette, border_pct, render=True))
            except Exception:
                refreshed = False

            if not refreshed:
                self._refresh_cut_colors_fast()
            print("   ✅ Cut section view refreshed with new palette")
        except Exception as e:
            print(f"   ⚠️ Cut palette refresh failed: {e}")
            import traceback
            traceback.print_exc()

    def _reset_cut_view_camera(self):
        """Reset cut section camera to correct orthogonal view along tangent."""
        if self.cut_vtk is None or self.cut_points is None:
            print("⚠️ Cannot reset view: cut section not active")
            return

        # ✅ PATCH: Recover tangent if missing — always use [0,1,0] for cut section.
        # The cut section coordinate space is defined such that the viewing direction
        # is always along Y. _estimate_section_tangent_3d returns the PCA spread axis
        # which is wrong (points spread along X in a narrow cut → tangent ≈ [-1,0,0]).
        if self.section_tangent is None:
            if self.original_section_tangent is not None:
                # Prefer the stored original over any estimation
                self.section_tangent = self.original_section_tangent.copy()
                print(f"⚠️ section_tangent was None — restored from original: {self.section_tangent}")
            else:
                # Cut section coordinate space always views along Y
                self.section_tangent = np.array([0.0, 1.0, 0.0])
                self.original_section_tangent = self.section_tangent.copy()
                print(f"⚠️ section_tangent was None — using cut-section default [0,1,0]")

        try:
            print("🔄 Resetting cut section camera to orthogonal view...")

            current_interactor = getattr(self.app, "classify_interactor", None)
            is_cut_interactor = (
                current_interactor is not None and
                hasattr(current_interactor, "vtk_widget") and
                current_interactor.vtk_widget == self.cut_vtk
            )

            self._set_camera_along_tangent(self.cut_vtk, self.cut_points, self.section_tangent)
            _safe_vtk_render(self.cut_vtk)

            if is_cut_interactor and current_interactor is not None:
                print("🔧 Restoring classification interactor to cut section...")
                self.cut_vtk.interactor.SetInteractorStyle(current_interactor.style)
                current_interactor.vtk_widget = self.cut_vtk
                current_interactor.is_cut_section = True
                print("✅ Classification interactor restored to cut section")

            print("✅ Camera reset successfully")
            self.app.statusBar().showMessage("✅ Cut section view reset", 2000)

        except Exception as e:
            print(f"⚠️ Failed to reset camera: {e}")
            import traceback
            traceback.print_exc()

    # def _reset_cut_view_camera(self):
    #     """Reset cut section camera to correct orthogonal view along tangent."""
    #     if self.cut_vtk is None or self.cut_points is None:
    #         print("⚠️ Cannot reset view: cut section not active")
    #         return

    #     # ✅ PATCH: If tangent was cleared by a state reset but cut_points are
    #     # still valid, re-derive the tangent instead of refusing to reset.
    #     if self.section_tangent is None:
    #         if self.cut_points is not None and len(self.cut_points) >= 2:
    #             print("⚠️ section_tangent was None — re-estimating from cut_points")
    #             self.section_tangent = self._estimate_section_tangent_3d(self.cut_points)
    #             self.original_section_tangent = self.section_tangent.copy()
    #             print(f"   ↳ Recovered tangent: {self.section_tangent}")
    #         else:
    #             # Absolute fallback: look along Y axis (cut section default)
    #             print("⚠️ Cannot estimate tangent — using default [0, 1, 0]")
    #             self.section_tangent = np.array([0.0, 1.0, 0.0])
    #             self.original_section_tangent = self.section_tangent.copy()

    #     try:
    #         print("🔄 Resetting cut section camera to orthogonal view...")

    #         current_interactor = getattr(self.app, "classify_interactor", None)
    #         is_cut_interactor = (
    #             current_interactor is not None and
    #             hasattr(current_interactor, "vtk_widget") and
    #             current_interactor.vtk_widget == self.cut_vtk
    #         )

    #         self._set_camera_along_tangent(self.cut_vtk, self.cut_points, self.section_tangent)

    #         _safe_vtk_render(self.cut_vtk)

    #         if is_cut_interactor and current_interactor is not None:
    #             print("🔧 Restoring classification interactor to cut section...")
    #             self.cut_vtk.interactor.SetInteractorStyle(current_interactor.style)
    #             current_interactor.vtk_widget = self.cut_vtk
    #             current_interactor.is_cut_section = True
    #             print("✅ Classification interactor restored to cut section")

    #         print("✅ Camera reset successfully")
    #         self.app.statusBar().showMessage("✅ Cut section view reset", 2000)

    #     except Exception as e:
    #         print(f"⚠️ Failed to reset camera: {e}")
    #         import traceback
    #         traceback.print_exc()


    def clear(self):
        """
        Clear all cut section data and state.
        ✅ FIXED: Proper actor cleanup before widget destruction
        ✅ FIXED: Does NOT clear cut_palette (preserves user settings)
        ✅ BUG #7 FIX: Drop actor refs after renderer removal to avoid native lifetime hazards
        ✅ BUG #8 FIX: Clear KDTree cache
        ✅ BUG FIX: Prevents OpenGL "invalid pixel format" error on app close
        """
        try:
            self._remove_cut_right_click_observer()
            print("\n🧹 Clearing Cut Section Controller...")
            try:
                sc = getattr(self.app, 'section_controller', None)
                if sc is not None and hasattr(sc, 'set_section_locate_enabled'):
                    sc.set_section_locate_enabled(True, clear_state=True)
            except Exception:
                pass
            
            # ✅ CRITICAL FIX: Check if render window is still valid
            is_app_shutdown = bool(getattr(self.app, "_shutdown_in_progress", False))
            is_vtk_valid = False
            if self.cut_vtk is not None:
                try:
                    setattr(self.cut_vtk, "_naksha_skip_render", True)
                    rw = self.cut_vtk.GetRenderWindow()
                    if rw is not None:
                        is_vtk_valid = bool(rw.GetMapped())
                except Exception:
                    is_vtk_valid = False
                    print("   ⚠️ VTK render window already destroyed")
            
            # ✅ CRITICAL: Remove actors BEFORE destroying widget (only if VTK valid)
            if self.cut_vtk is not None and is_vtk_valid:
                try:
                    renderer = self.cut_vtk.renderer
                    
                    # Remove all cut section actors
                    actors_to_remove = [
                        self.cut_core_actor,
                        self.cut_buffer_actor,
                        self.line_actor,
                        self.buffer_actor_upper,
                        self.buffer_actor_lower,
                        self.cut_preview_upper,
                        self.cut_preview_lower
                    ]
                    
                    for actor in actors_to_remove:
                        if actor is not None:
                            try:
                                renderer.RemoveActor(actor)
                            except Exception as e:
                                print(f"   ⚠️ Actor removal warning: {e}")
                except Exception as e:
                    print(f"   ⚠️ Actor cleanup skipped (widget invalid): {e}")
                
                # ✅ FIX: Safe widget cleanup with render window validation
                try:
                    # Step 1: Disable rendering FIRST (idempotent finalize)
                    if self._finalize_cut_render_window_once(self.cut_vtk):
                        print("   ✅ Render window disabled")

                    
                    # Step 2: Clear widget (skip during app shutdown to avoid stale-context renders)
                    if not is_app_shutdown:
                        self.cut_vtk.clear()
                    
                    # Step 3: Close widget
                    self.cut_vtk.close()
                    
                    print("   ✅ Cut VTK widget finalized")
                except Exception as e:
                    print(f"   ⚠️ VTK finalize warning: {e}")
            
            # Clear state (keep your existing state clearing code)
            self._state = CutSectionState.IDLE
            self.cut_phase = 0
            self.center_point = None
            self.dynamic_depth = getattr(self.app, "default_cut_width", 1.0)
            self._reset_depth_adjustment_guard()
            
            # Clear data
            self.cut_points = None
            self._cut_index_map = None
            
            # ✅ BUG #8 FIX: Explicitly clear KDTree cache
            if self._kdtree_cache is not None:
                try:
                    # Python GC will collect it, but explicit deletion helps
                    del self._kdtree_cache
                    print("   ✅ KDTree cache cleared")
                except Exception:
                    pass
            self._kdtree_cache = None
            
            self.section_tangent = None
            self.is_cut_view_active = False
            
            # ============================================================
            # ✅ CRITICAL: DO NOT CLEAR self.cut_palette
            # This preserves user's Display Mode settings across cuts
            # ============================================================
            # self.cut_palette = {}  # ← DO NOT DO THIS!
            
            if hasattr(self, 'cut_palette') and self.cut_palette:
                print(f"   💾 Preserved cut palette: {len(self.cut_palette)} classes")
            # ============================================================
            
            # Clear actor references
            self.cut_core_actor = None
            self.cut_buffer_actor = None
            self.line_actor = None
            self.buffer_actor_upper = None
            self.buffer_actor_lower = None
            self.cut_preview_upper = None
            self.cut_preview_lower = None
            
            # ✅ BUG #7 FIX: Clear cached geometry objects (prevents leak)
            for attr in ['_line_actor_points', '_line_actor_poly', '_line_actor_mapper',
                        '_cut_preview_upper_points', '_cut_preview_upper_lines', 
                        '_cut_preview_upper_poly', '_cut_preview_upper_mapper',
                        '_cut_preview_lower_points', '_cut_preview_lower_lines',
                        '_cut_preview_lower_poly', '_cut_preview_lower_mapper']:
                if hasattr(self, attr):
                    try:
                        delattr(self, attr)
                    except Exception:
                        pass
            
            print("   ✅ All cached geometry cleared")
            
            # Clear visuals
            self._cross_camera_state = None
            self.active_vtk = None
            
            # Clear history
            self.cut_level = 0
            self.parent_cut_points = None
            self.parent_cut_index_map = None
            self.cut_history = []
            
            # Clear rotation tracking
            self.original_section_tangent = None
            self.accumulated_rotation = 0
            
            # Clear saved state
            self._saved_section_state = None
            self._original_section_points = None
            self._original_section_indices = None
            
            # Detach observers
            self._detach_all_view_observers()
            
            # ✅ Close dock AFTER widget cleanup
            if self.cut_dock is not None:
                try:
                    if hasattr(self.app, 'removeDockWidget'):
                        self.app.removeDockWidget(self.cut_dock)
                    
                    self.cut_dock.hide()
                    self.cut_dock.deleteLater()
                    print("   ✅ Cut dock closed and deleted")
                except Exception as e:
                    print(f"   ⚠️ Dock close warning: {e}")
            
            self.cut_vtk = None
            self.cut_dock = None
            self.depth_label = None
            self.depth_spin = None
            
            print("✅ Cut Section Controller cleared (palette preserved)")
            
        except Exception as e:
            print(f"⚠️ Cut Section clear error: {e}")
            import traceback
            traceback.print_exc()



    def _configure_line_on_top(self, actor):
        """NUCLEAR OPTION: Force lines ALWAYS visible (disable depth test)."""
        try:
            if actor is None:
                return
            
            mapper = actor.GetMapper()
            if mapper is None:
                print("⚠️ Skipping line depth config: actor has no mapper")
                return
            
            # ✅ AGGRESSIVE OFFSETS
            mapper.SetResolveCoincidentTopologyToPolygonOffset()
            mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(-5.0, -5.0)
            
            try:
                mapper.SetRelativeCoincidentTopologyLineOffsetParameters(-10.0, -10.0)
            except AttributeError:
                pass
            
            # ✅ NUCLEAR: Access OpenGL state directly (VTK 9+)
            try:
                # This forces the actor to render WITHOUT depth testing
                prop = actor.GetProperty()
                prop.SetRenderLinesAsTubes(False)
                
                # Force to translucent pass (rendered last, no depth test)
                actor.ForceTranslucentOn()
                prop.SetOpacity(0.99)  # Just below 1.0 to trigger translucent pass
            except Exception:
                pass
            
            actor.SetUseBounds(False)
            
        except Exception as e:
            print(f"⚠️ Could not set line depth priority: {e}")


    def _activate_from_cut_dock(self):
        """Activate cut section INSIDE the existing cut view (nested cut)."""
        # ✅ MUTUAL EXCLUSION: disable Identify (Point Target) so it cannot
        # collide with the nested cut placement handlers.
        if getattr(self.app, "_deactivate_point_pick_tools", None) is not None:
            self.app._deactivate_point_pick_tools()
        print(f"📐 Nested cut: taking cut from existing cut view")
        purged = self._purge_stale_cut_preview_actors(include_cut_dock=True)
        if purged > 0:
            print(f"  🧹 Purged {purged} stale preview actor(s) before nested cut")
        self._cut_source = 'cut'

        try:
            sc = getattr(self.app, 'section_controller', None)
            if sc is not None and hasattr(sc, 'clear_locate_state'):
                sc.clear_locate_state()
            if sc is not None and hasattr(sc, 'set_section_locate_enabled'):
                sc.set_section_locate_enabled(False, clear_state=True)
        except Exception:
            pass

        self._temporarily_disable_classification()
        
        # ✅ BUG #2 FIX: Remove old observers with VERIFICATION
        if 'cut_dock' in self._view_observer_ids:
            old_ids = self._view_observer_ids['cut_dock']
            if self.cut_vtk is not None:
                iren = self.cut_vtk.interactor
                removed_count = 0
                
                for oid in old_ids:
                    try:
                        # ✅ CRITICAL: Verify observer exists before removing
                        if iren.HasObserver(oid):
                            iren.RemoveObserver(oid)
                            removed_count += 1
                            print(f"  🧹 Removed old observer: {oid}")
                        else:
                            print(f"  ⚠️ Observer {oid} already removed")
                    except Exception as e:
                        print(f"  ⚠️ Failed to remove observer {oid}: {e}")
                
                print(f"  ✅ Verified removal: {removed_count}/{len(old_ids)} observers cleared")
            
            del self._view_observer_ids['cut_dock']
        
        # Reset state
        self._detach_all_view_observers()
        self._clear_preview_actors()
        
        self._state = CutSectionState.WAITING_CENTER
        self.cut_phase = 0
        self.center_point = None
        self.dynamic_depth = max(getattr(self.app, "default_cut_width", 1.0), 0.5)
        self._reset_depth_adjustment_guard()
        
        # ✅ CRITICAL: Clear cached line geometry to force recreation
        for attr in ['_line_actor_points', '_line_actor_poly', '_line_actor', '_line_actor_mapper',
                    '_cut_preview_upper_points', '_cut_preview_upper_lines', '_cut_preview_upper_poly', '_cut_preview_upper_mapper',
                    '_cut_preview_lower_points', '_cut_preview_lower_lines', '_cut_preview_lower_poly', '_cut_preview_lower_mapper']:
            if hasattr(self, attr):
                delattr(self, attr)
        
        self.line_actor = None
        self.cut_preview_upper = None
        self.cut_preview_lower = None
        
        # ✅ Reset spinbox for new cut
        if self.depth_spin:
            self.depth_spin.blockSignals(True)
            self.depth_spin.setValue(self.dynamic_depth)
            self.depth_spin.blockSignals(False)
        
        # ✅ CRITICAL: Store last mouse position to prevent redundant updates
        self._last_mouse_pos = None
        self._last_update_time = 0
        self._cut_preview_pending_depth_ui = None
        self._cut_preview_base_signature = None
        self._cut_preview_motion_events = 0
        self._cut_preview_render_frames = 0
        self._cut_preview_render_ms_total = 0.0
        self._cut_preview_map_ms_total = 0.0
        self._cut_preview_profile_last_print = time.perf_counter()
        
        # ✅ BUG #2 FIX: Clean slate before attaching new observers
        if self.cut_vtk is not None:
            iren = self.cut_vtk.interactor
            
            # ✅ NUCLEAR CLEANUP: Remove ALL LeftButton/MouseMove observers
            # This prevents ghost observers from stacking up
            try:
                iren.RemoveObservers("LeftButtonPressEvent")
                iren.RemoveObservers("MouseMoveEvent")
                print(f"  🧹 Cleared all LeftButton/MouseMove observers (nuclear cleanup)")
            except Exception as e:
                print(f"  ⚠️ Nuclear cleanup warning: {e}")
            
            def on_left_click(obj, evt):
                if self._cut_source != 'cut':
                    return
                if self._state not in (CutSectionState.WAITING_CENTER, CutSectionState.WAITING_DEPTH):
                    return
                print(f"🖱️ Left click in cut dock (state={self._state})")
                
                pos = self.cut_vtk.interactor.GetEventPosition()
                picker = self._cut_world_picker
                picker.Pick(pos[0], pos[1], 0, self.cut_vtk.renderer)
                pt = np.asarray(picker.GetPickPosition(), dtype=float)
                
                if np.allclose(pt, (0, 0, 0), atol=1e-6):
                    print("  ⚠️ Invalid pick position")
                    return
                
                if self._state == CutSectionState.WAITING_CENTER:
                    self.center_point = pt
                    self._state = CutSectionState.WAITING_DEPTH
                    self.cut_phase = 1
                    print(f"  ✅ Center set at {pt}, now WAITING_DEPTH")
                    
                    # ✅ CRITICAL: Force immediate line update
                    self._draw_dynamic_center_line_in_cut(self.center_point)
                    _safe_vtk_render(self.cut_vtk)
                    return
                
                if self._state == CutSectionState.WAITING_DEPTH:
                    print(f"  ✅ Finalizing with depth={self.dynamic_depth}")
                    self._finalize_dynamic_cut_section()
                    return
            
            def on_mouse_move(obj, evt):
                if self._cut_source != 'cut':
                    return
                if self._state not in (
                    CutSectionState.WAITING_CENTER,
                    CutSectionState.WAITING_DEPTH,
                ):
                    return

                # Do not throttle cursor state. Rendering is already coalesced,
                # so every event can cheaply update the latest desired preview
                # position without scheduling an extra frame.
                pos = self.cut_vtk.interactor.GetEventPosition()

                # One-pixel dead zone only. The previous 2px + 16ms throttle
                # created visible stepping during precise slow drags.
                if self._last_mouse_pos is not None:
                    dx = abs(pos[0] - self._last_mouse_pos[0])
                    dy = abs(pos[1] - self._last_mouse_pos[1])
                    if dx < 1 and dy < 1:
                        return
                self._last_mouse_pos = pos

                map_t0 = time.perf_counter()
                curr = self._cut_cursor_world_on_focal_plane(pos)
                map_ms = (time.perf_counter() - map_t0) * 1000.0
                if curr is None:
                    return

                self._cut_preview_motion_events = int(
                    getattr(self, "_cut_preview_motion_events", 0)
                ) + 1
                self._cut_preview_map_ms_total = float(
                    getattr(self, "_cut_preview_map_ms_total", 0.0)
                ) + map_ms

                if self._state == CutSectionState.WAITING_CENTER:
                    # Preview only. The actual click still uses the exact
                    # vtkWorldPointPicker path below.
                    self._draw_dynamic_center_line_in_cut(curr)
                    return

                if (
                    self._state == CutSectionState.WAITING_DEPTH
                    and self.center_point is not None
                ):
                    # Visually the cut dock horizontal axis is world X.
                    # Nested rotation/filter semantics are unchanged at finalize.
                    new_depth = abs(
                        float(curr[0]) - float(self.center_point[0])
                    )

                    if new_depth < 0.01:
                        return

                    self.dynamic_depth = new_depth
                    self._mark_depth_adjustment_started()
                    self._draw_cut_section_preview(
                        self.center_point,
                        self.dynamic_depth,
                    )

                    # QWidget update is coalesced with the next preview frame.
                    self._cut_preview_pending_depth_ui = self.dynamic_depth
                    return

            try:
                lid = iren.AddObserver("LeftButtonPressEvent", on_left_click)
                mid = iren.AddObserver("MouseMoveEvent", on_mouse_move)
                self._view_observer_ids['cut_dock'] = [lid, mid]
                print(f"  ✅ New observers attached: left={lid}, move={mid}")
                
                # ✅ BUG #2 FIX: Verify observers were successfully registered
                if not iren.HasObserver("LeftButtonPressEvent"):
                    print(f"  ⚠️ WARNING: LeftButtonPressEvent observer {lid} not registered!")
                if not iren.HasObserver("MouseMoveEvent"):
                    print(f"  ⚠️ WARNING: MouseMoveEvent observer {mid} not registered!")
                    
            except Exception as e:
                print(f"  ⚠️ Cut dock observer attachment failed: {e}")
        
        self.app.statusBar().showMessage("✂️ Nested Cut: click center, adjust depth, click to finalize", 0)


    def _fast_rearm_persistent_cut_in_cut(self) -> bool:
        """
        Re-arm the next Cut-in-Cut placement without rebuilding interaction state.

        During a persistent nested-cut session the cut-dock observers are already
        installed and classification is already suspended.  The old path restored
        every cross-section/classification interactor after each cut and then
        immediately tore all of it down again via _activate_from_cut_dock().
        Keeping the existing observers/style alive removes that redundant Qt/VTK
        churn while preserving the same cut geometry, palette and index mapping.

        Returns True when the hot path was used.  False lets the caller fall back
        to the original defensive re-arm path.
        """
        if self.cut_vtk is None or not self._has_valid_cut_dock():
            return False

        observer_ids = self._view_observer_ids.get("cut_dock")
        if not observer_ids:
            return False

        # Remove references to preview props that were removed by
        # _plot_cut_to_dedicated_widget().  Do NOT touch the completed cut actor.
        self.line_actor = None
        self.buffer_actor_upper = None
        self.buffer_actor_lower = None
        self.cut_preview_upper = None
        self.cut_preview_lower = None

        for attr in (
            "_line_actor_points", "_line_actor_poly", "_line_actor_mapper",
            "_cut_preview_upper_points", "_cut_preview_upper_lines",
            "_cut_preview_upper_poly", "_cut_preview_upper_mapper",
            "_cut_preview_lower_points", "_cut_preview_lower_lines",
            "_cut_preview_lower_poly", "_cut_preview_lower_mapper",
        ):
            if hasattr(self, attr):
                try:
                    delattr(self, attr)
                except Exception:
                    pass

        # Cancel any preview frame queued for the just-finished placement.
        self._cut_preview_render_generation = int(
            getattr(self, "_cut_preview_render_generation", 0)
        ) + 1
        self._cut_preview_render_pending = False

        self._cut_source = "cut"
        self._state = CutSectionState.WAITING_CENTER
        self.cut_phase = 0
        self.center_point = None
        self.dynamic_depth = max(
            float(getattr(self.app, "default_cut_width", 1.0)), 0.5
        )
        self._reset_depth_adjustment_guard()
        self._last_mouse_pos = None
        self._last_update_time = 0.0
        self._cut_preview_pending_depth_ui = None
        self._cut_preview_base_signature = None
        self._cut_preview_motion_events = 0
        self._cut_preview_render_frames = 0
        self._cut_preview_render_ms_total = 0.0
        self._cut_preview_map_ms_total = 0.0
        self._cut_preview_profile_last_print = time.perf_counter()

        if self.depth_spin is not None:
            try:
                self.depth_spin.blockSignals(True)
                self.depth_spin.setValue(self.dynamic_depth)
                self.depth_spin.blockSignals(False)
            except Exception:
                pass

        # A persistent nested-cut session owns the cut-dock left click, so keep
        # locate/classification interaction suspended until the user exits the
        # tool or explicitly activates a classification tool.
        try:
            sc = getattr(self.app, "section_controller", None)
            if sc is not None and hasattr(sc, "set_section_locate_enabled"):
                sc.set_section_locate_enabled(False, clear_state=True)
        except Exception:
            pass

        self._cut_fast_rearm_count = int(getattr(self, "_cut_fast_rearm_count", 0)) + 1
        try:
            self.app.statusBar().showMessage(
                "✂️ Cut-in-Cut ready: click next center, adjust depth, click to finalize",
                0,
            )
        except Exception:
            pass

        print(
            "⚡ CUT_IN_CUT_FAST_REARM "
            f"count={self._cut_fast_rearm_count} observers=reused "
            "classification_rebuild=skipped cross_view_restore=skipped"
        )
        return True


    def _draw_dynamic_center_line_in_cut(self, center):
        """Draw center line INSIDE cut dock with smooth updates."""
        if self.cut_vtk is None:
            return
        
        # ✅ SAFETY: Check if renderer exists
        if not hasattr(self.cut_vtk, "renderer") or self.cut_vtk.renderer is None:
            print("  ⚠️ No renderer available")
            return
        
        ren = self.cut_vtk.renderer
        
        # Get Z bounds from cut points (use viewport for nested cuts)
        try:
            cam = ren.GetActiveCamera()
            parallel_scale = cam.GetParallelScale()
            height_extent = parallel_scale * 2.0
            zmin = center[2] - height_extent
            zmax = center[2] + height_extent
        except Exception:
            if self.cut_points is not None:
                zmin = float(np.min(self.cut_points[:, 2]))
                zmax = float(np.max(self.cut_points[:, 2]))
            else:
                zmin, zmax = center[2] - 10.0, center[2] + 10.0
        
        p0 = np.array([center[0], center[1], zmin], dtype=float)
        p1 = np.array([center[0], center[1], zmax], dtype=float)
        
        # ✅ SMOOTH UPDATE: Reuse actor if exists
        if self.line_actor is None or not hasattr(self, '_line_actor_points'):
            print(f"  🆕 Creating new line actor at {center}")
            
            # Create ONCE
            pts = vtk.vtkPoints()
            pts.InsertNextPoint(*p0)
            pts.InsertNextPoint(*p1)
            
            lines = vtk.vtkCellArray()
            lines.InsertNextCell(2)
            lines.InsertCellPoint(0)
            lines.InsertCellPoint(1)
            
            poly = vtk.vtkPolyData()
            poly.SetPoints(pts)
            poly.SetLines(lines)
            
            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputData(poly)
            
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(0, 1, 0)  # Green
            actor.GetProperty().SetLineWidth(2)  # ✅ Slightly thicker for visibility
            self._mark_cut_preview_actor(actor, "cut_center_line")
            
            # ✅ FORCE LINES ON TOP
            self._configure_line_on_top(actor)
            
            ren.AddActor(actor)
            self.line_actor = actor
            
            # Cache for updates
            self._line_actor_points = pts
            self._line_actor_poly = poly
            self._line_actor_mapper = mapper
        else:
            # ✅ FAST UPDATE (no recreation)
            pts = self._line_actor_points
            poly = self._line_actor_poly
            
            # ✅ CRITICAL: Update points AND notify VTK
            pts.SetPoint(0, *p0)
            pts.SetPoint(1, *p1)
            pts.Modified()
            poly.Modified()
            
        self._schedule_cut_preview_render()

    def _finalize_dynamic_cut_section(self):
        """Create cut section - FIXED coordinate transformation for proper orthogonal view"""

        # Preserve how this operation was started before the cut widget and its
        # interactor are rebuilt. Cut-in-Cut stays armed after every valid cut.
        persistent_nested_cut = self._cut_source == 'cut'
        _nested_total_t0 = time.perf_counter() if persistent_nested_cut else None
        _nested_input_count = int(len(self.cut_points)) if (persistent_nested_cut and self.cut_points is not None) else 0
        _nested_filter_rotate_ms = 0.0
        _nested_plot_ms = 0.0

        # Cross-section locate should stay suspended throughout a persistent
        # Cut-in-Cut session.  Toggling it on here only to disable it again on
        # re-arm caused unnecessary observer/UI churn.
        if not persistent_nested_cut:
            try:
                sc = getattr(self.app, 'section_controller', None)
                if sc is not None and hasattr(sc, 'set_section_locate_enabled'):
                    sc.set_section_locate_enabled(True, clear_state=True)
            except Exception:
                pass

        # Invalidate a preview render queued just before the final click.
        self._cut_preview_render_generation = int(
            getattr(self, "_cut_preview_render_generation", 0)
        ) + 1
        self._cut_preview_render_pending = False

        if self.center_point is None:
            print("⚠️ finalize: invalid center")
            return
        
        # ✅ FALLBACK: Use minimum depth if current depth is too small
        if self.dynamic_depth <= 0.01:
            self.dynamic_depth = max(0.5, getattr(self.app, "default_cut_width", 1.0))
            print(f"⚠️ Depth too small, using fallback: {self.dynamic_depth}m")
            
            # Update UI
            if self.depth_spin:
                self.depth_spin.blockSignals(True)
                self.depth_spin.setValue(self.dynamic_depth)
                self.depth_spin.blockSignals(False)
                
        if self.center_point is None or self.dynamic_depth <= 0:
            print("⚠️ finalize: invalid center/depth")
            
            # ✅ BUG #11 FIX: Reset state
            self._state = CutSectionState.IDLE
            self.cut_phase = 0
            self.app.statusBar().showMessage("❌ Invalid cut parameters", 3000)
            return

        # ============================================================
        # ✅ CUT PALETTE PERSISTENCE (PRESERVE BEFORE NEW CUT)
        # ============================================================
        preserved_cut_palette = None
        try:
            if hasattr(self, "cut_palette") and self.cut_palette:
                preserved_cut_palette = {
                    int(code): {
                        "show": bool(info.get("show", False)),
                        "description": str(info.get("description", "")),
                        "color": tuple(info.get("color", (128, 128, 128))),
                        "weight": float(info.get("weight", 1.0)),
                    }
                    for code, info in self.cut_palette.items()
                }
                print(f"💾 Preserving cut palette before new cut: {len(preserved_cut_palette)} classes")
        except Exception as e:
            print(f"⚠️ Could not preserve cut palette: {e}")
            preserved_cut_palette = None

        # Nested cut logic (cut-from-cut)
        if self.is_cut_view_active and self.cut_points is not None:
            _nested_filter_t0 = time.perf_counter()
            print(f"🔄 Creating nested cut from {len(self.cut_points)} existing cut points")

            # Read the existing cut arrays without cloning them first.  The old
            # path copied the full XYZ + index map, then created two more arrays
            # during filtering/rotation.  Only the filtered/rotated output needs
            # to be materialized.
            source_xyz = np.asarray(self.cut_points)
            source_index_map = (
                np.asarray(self._cut_index_map)
                if self._cut_index_map is not None
                else None
            )
            
            # ✅ BUG #4 FIX: Use correct axis based on rotation (alternates 0→1→0→1)
            # After each 90° rotation, the filtering axis switches
            # 0° or 180° → filter X-axis (0)
            # 90° or 270° → filter Y-axis (1)
            axis = (self.accumulated_rotation // 90) % 2
            
            print(f"🔍 Rotation state: {self.accumulated_rotation}° → using axis {axis} ({'X' if axis == 0 else 'Y'})")
            
            # Filter by depth along the calculated axis
            center_val = float(self.center_point[axis])
            cut_lower = center_val - self.dynamic_depth
            cut_upper = center_val + self.dynamic_depth
            
            print(f"✅ Filtering nested cut: axis={axis}, center={center_val:.2f}, depth=±{self.dynamic_depth:.2f}")
            
            axis_values = source_xyz[:, axis]
            depth_mask = (axis_values >= cut_lower) & (axis_values <= cut_upper)
            kept_count = int(np.count_nonzero(depth_mask))

            print(f"✅ Depth filter: {kept_count}/{len(source_xyz)} points")
            
            if kept_count == 0:
                print(f"❌ No points in depth range [{cut_lower:.2f}, {cut_upper:.2f}] on axis {axis}")
                print(f"   Data range on axis {axis}: [{axis_values.min():.2f}, {axis_values.max():.2f}]")
                
                # ✅ BUG #11 FIX: Reset state before returning
                self._state = CutSectionState.IDLE
                self.cut_phase = 0
                self.center_point = None
                self._clear_preview_actors()
                
                self.app.statusBar().showMessage("❌ No points in selected range - try different depth", 3000)
                print("✅ State reset to IDLE")
                return

            # Transform to the next orthogonal coordinate system.  Allocate one
            # output array only; avoid the old full-copy -> filtered-copy ->
            # rotated-copy chain.
            if kept_count == len(source_xyz):
                xyz_filtered = source_xyz
                parent_index_map = source_index_map
            else:
                xyz_filtered = source_xyz[depth_mask]
                parent_index_map = (
                    source_index_map[depth_mask]
                    if source_index_map is not None
                    else None
                )

            xyz = np.empty_like(xyz_filtered)
            xyz[:, 0] = xyz_filtered[:, 1]   # New X = old Y
            xyz[:, 1] = -xyz_filtered[:, 0]  # New Y = -old X (90° rotation)
            xyz[:, 2] = xyz_filtered[:, 2]   # Z unchanged

            _nested_filter_rotate_ms = (time.perf_counter() - _nested_filter_t0) * 1000.0

            # Increment rotation
            self.accumulated_rotation = (self.accumulated_rotation + 90) % 360
            print(f"✅ Rotation incremented: now at {self.accumulated_rotation}°")
            print(f"   Next cut will filter on axis {(self.accumulated_rotation // 90) % 2}")

        else:
            print("✂️ First cut from cross-section")

            active_view = getattr(self.app.section_controller, "active_view", 0)
            section_points_transformed = getattr(self.app, f"section_{active_view}_points_transformed", None)
            combined_mask = getattr(self.app, f"section_{active_view}_combined_mask", None)
            section_global_indices = getattr(self.app, f"section_{active_view}_indices", None)
            if section_global_indices is None:
                core_indices = getattr(self.app, f"section_{active_view}_core_indices", None)
                buffer_indices = getattr(self.app, f"section_{active_view}_buffer_indices", None)
                if core_indices is not None and buffer_indices is not None:
                    section_global_indices = np.concatenate([core_indices, buffer_indices])
                elif combined_mask is not None:
                    section_global_indices = np.flatnonzero(combined_mask)

            # cut_section_controller.py, replacing lines 1274-1281
            # ✅ FIX: active_view is just tab focus; it can point at a view whose
            # section was only mirrored via sync (display-only) and never actually
            # computed. Before giving up, fall back to any open view that DOES have
            # real transformed data, so a tab switch doesn't silently kill the tool.
            if section_points_transformed is None or section_global_indices is None:
                fallback_view = None
                for v_idx in getattr(self.app, 'section_vtks', {}).keys():
                    if getattr(self.app, f"section_{v_idx}_points_transformed", None) is not None:
                        fallback_view = v_idx
                        break
                if fallback_view is not None:
                    print(f"⚠️ View {active_view+1} has no section data — using View {fallback_view+1} instead")
                    active_view = fallback_view
                    section_points_transformed = getattr(self.app, f"section_{active_view}_points_transformed", None)
                    section_global_indices = getattr(self.app, f"section_{active_view}_indices", None)

            if section_points_transformed is None or section_global_indices is None:
                print("❌ No transformed section data!")
                self._state = CutSectionState.IDLE
                self.cut_phase = 0
                self.app.statusBar().showMessage(
                    f"❌ View {active_view+1} has no cross-section drawn — draw a section there first", 4000)
                return

            # Persist the actual data-owning source view. Tab focus can change
            # later, but cut palette/weight inheritance must remain bound to
            # the section from which these points were taken.
            self._cut_source_view_index = int(active_view)

            # ✅ FIX: Detect view mode and use correct axis
            view_mode = getattr(self.app, "cross_view_mode", "side")
            # Clean the string (handle typo with trailing space)
            view_mode = view_mode.strip().lower() if isinstance(view_mode, str) else "side"
            
            # In cross-section coordinate system:
            if view_mode == "side":
                axis = 0  # X-axis (along section) - correct for side view
            else:  # "front" or anything else
                axis = 1  # Y-axis (perpendicular) - correct for front view
            
            print(f"🔍 View mode: '{view_mode}', filtering axis: {axis} ({'X - along section' if axis == 0 else 'Y - perpendicular'})")

            # Get range of valid data on the filtering axis
            axis_min = float(np.min(section_points_transformed[:, axis]))
            axis_max = float(np.max(section_points_transformed[:, axis]))

            # Get center value on the correct axis from the picked point
            raw_center = float(self.center_point[axis])

            picked_world = False

            if raw_center < (axis_min - 10.0) or raw_center > (axis_max + 10.0):
                picked_world = True

                P1 = getattr(self.app, f"section_{active_view}_P1", None)
                P2 = getattr(self.app, f"section_{active_view}_P2", None)

                if P1 is None or P2 is None:
                    print("❌ Cannot convert WORLD→SECTION (missing P1/P2)")
                    
                    self._state = CutSectionState.IDLE
                    self.cut_phase = 0
                    self.app.statusBar().showMessage("❌ Section coordinates invalid", 3000)
                    return

                v = np.asarray(P2[:2], dtype=float) - np.asarray(P1[:2], dtype=float)
                L = float(np.linalg.norm(v))
                if L < 1e-9:
                    print("❌ Cannot convert WORLD→SECTION (invalid section line)")
                    
                    self._state = CutSectionState.IDLE
                    self.cut_phase = 0
                    self.app.statusBar().showMessage("❌ Section line invalid", 3000)
                    return

                dir_vec = v / L
                perp_vec = np.array([-dir_vec[1], dir_vec[0]], dtype=float)

                # Convert picked world XY to section-local coordinates
                rel = np.asarray(self.center_point[:2], dtype=float) - np.asarray(P1[:2], dtype=float)
                along_dist = float(np.dot(rel, dir_vec))  # X in section space
                perp_dist = float(np.dot(rel, perp_vec))  # Y in section space

                if axis == 0:
                    center_val = along_dist
                    print(f"🔧 Pick looked like WORLD coords. Converted to section-local X={center_val:.2f}m (along)")
                else:
                    center_val = perp_dist
                    print(f"🔧 Pick looked like WORLD coords. Converted to section-local Y={center_val:.2f}m (perpendicular)")
            else:
                center_val = raw_center

            cut_lower = center_val - float(self.dynamic_depth)
            cut_upper = center_val + float(self.dynamic_depth)

            print(f"✅ Filtering by axis {axis} ({'X - along' if axis == 0 else 'Y - perpendicular'}): [{cut_lower:.2f}, {cut_upper:.2f}]")
            print(f"   🔍 Data range on axis {axis}: [{axis_min:.2f}, {axis_max:.2f}]")

            depth_mask = (
                (section_points_transformed[:, axis] >= cut_lower) &
                (section_points_transformed[:, axis] <= cut_upper)
            )

            print(f"✅ Depth filter: {int(np.sum(depth_mask))}/{len(section_points_transformed)} points")

            if not np.any(depth_mask):
                print(f"❌ No points in depth range [{cut_lower:.2f}, {cut_upper:.2f}] on axis {axis}")
                print(f"   Data range on axis {axis}: [{axis_min:.2f}, {axis_max:.2f}]")
                
                # ✅ BUG #11 FIX: Reset state before returning
                self._state = CutSectionState.IDLE
                self.cut_phase = 0
                self.center_point = None
                self._clear_preview_actors()
                
                self.app.statusBar().showMessage("❌ No points in selected range - try different depth", 3000)
                print("✅ State reset to IDLE")
                return

            xyz_in_depth = section_points_transformed[depth_mask]
            depth_indices = section_global_indices[depth_mask]

            # Viewport filter
            if picked_world:
                xyz_cross_space = xyz_in_depth
                parent_index_map = depth_indices
                print(f"⚠️ Skipping viewport filter (WORLD pick detected). Using {len(xyz_cross_space)} points after depth filter.")
            else:
                vtk_widget = self.app.section_vtks.get(active_view)
                if vtk_widget is None:
                    print("❌ No VTK widget!")
                    
                    self._state = CutSectionState.IDLE
                    self.cut_phase = 0
                    self.app.statusBar().showMessage("❌ VTK widget not available", 3000)
                    return

                try:
                    _rw = vtk_widget.GetRenderWindow()
                    if _rw is None or _rw.GetInteractor() is None:
                        print("⚠️ Render window not available for viewport bounds")
                        return
                    ren = _rw.GetRenderers().GetFirstRenderer()
                    if ren is None:
                        return
                    cam = ren.GetActiveCamera()
                    fp = np.array(cam.GetFocalPoint())
                    scale = cam.GetParallelScale()
                    aspect = ren.GetTiledAspectRatio()

                    half_w = scale * aspect
                    half_h = scale

                    # Viewport bounds depend on view mode
                    if view_mode == "side":
                        # Side view: XZ plane (X=horizontal, Z=vertical)
                        x_lim = (fp[0] - half_w, fp[0] + half_w)
                        z_lim = (fp[2] - half_h, fp[2] + half_h)
                        
                        viewport_mask = (
                            (xyz_in_depth[:, 0] >= x_lim[0]) &
                            (xyz_in_depth[:, 0] <= x_lim[1]) &
                            (xyz_in_depth[:, 2] >= z_lim[0]) &
                            (xyz_in_depth[:, 2] <= z_lim[1])
                        )
                    else:
                        # Front view: YZ plane (Y=horizontal, Z=vertical)
                        y_lim = (fp[1] - half_w, fp[1] + half_w)
                        z_lim = (fp[2] - half_h, fp[2] + half_h)
                        
                        viewport_mask = (
                            (xyz_in_depth[:, 1] >= y_lim[0]) &
                            (xyz_in_depth[:, 1] <= y_lim[1]) &
                            (xyz_in_depth[:, 2] >= z_lim[0]) &
                            (xyz_in_depth[:, 2] <= z_lim[1])
                        )
                except Exception as e:
                    print(f"⚠️ Viewport bounds failed: {e}")
                    viewport_mask = np.ones(len(xyz_in_depth), dtype=bool)

                xyz_cross_space = xyz_in_depth[viewport_mask]
                parent_index_map = depth_indices[viewport_mask]

                print(f"✅ Filtered: {len(xyz_cross_space)} points (axis {axis} depth + viewport)")

                if len(xyz_cross_space) == 0:
                    print("❌ No points left after viewport filter!")
                    
                    self._state = CutSectionState.IDLE
                    self.cut_phase = 0
                    self.center_point = None
                    self._clear_preview_actors()
                    
                    self.app.statusBar().showMessage("❌ No points in viewport - zoom out or adjust view", 3000)
                    print("✅ State reset to IDLE")
                    return

            # Transform to cut section coordinate space
            print("🔧 Transforming to cut section coordinate space...")

            xyz = np.zeros_like(xyz_cross_space)
            
            # ✅ FIX: Transform based on which view we're cutting from
            if view_mode == "side":
                # Side view cut: swap X and Y so we look perpendicular to original view
                xyz[:, 0] = xyz_cross_space[:, 1]  # New X = perpendicular distance (was Y)
                xyz[:, 1] = xyz_cross_space[:, 0]  # New Y = along distance (was X)
            else:
                # Front view cut: swap X and Y the other way
                xyz[:, 0] = xyz_cross_space[:, 0]  # New X = along distance (keep X)
                xyz[:, 1] = xyz_cross_space[:, 1]  # New Y = perpendicular (keep Y)
            
            xyz[:, 2] = xyz_cross_space[:, 2]  # Z unchanged

            print(f"✅ Transformed to cut section space:")
            print(f"   X range: [{xyz[:, 0].min():.2f}, {xyz[:, 0].max():.2f}]")
            print(f"   Y range: [{xyz[:, 1].min():.2f}, {xyz[:, 1].max():.2f}]")
            print(f"   Z range: [{xyz[:, 2].min():.2f}, {xyz[:, 2].max():.2f}]")

            self.accumulated_rotation = 0

        if xyz is None or len(xyz) == 0:
            print("⚠️ finalize: no points available")
            return

        # Calculate tangent for camera setup
        # Tangent is along Y-axis (the old along-line direction)
        self.section_tangent = np.array([0.0, 1.0, 0.0])
        self.original_section_tangent = self.section_tangent.copy()
        print(f"📐 Cut section tangent: {self.section_tangent}")
        
        # Store cut points
        self.cut_mask_in_section = np.ones(len(xyz), dtype=bool)
        self.cut_points = xyz
        
        print(f"✂️ Final cut: {len(self.cut_points)} points")

        # ============================================================
        # ✅ CUT PALETTE PERSISTENCE (RESTORE AFTER NEW CUT)
        # ============================================================
        # ============================================================
        # ✅ BIDIRECTIONAL PALETTE SYNC (REPLACES OLD PRESERVATION CODE)
        # ============================================================
        
        # Try to sync from source view first
        palette_synced = self.sync_palette_from_source_view()
        
        if not palette_synced:
            # Fallback: Use global Display Mode slot 5
            print("   ℹ️ No source palette - initializing from Display Mode slot 5")
            
            src = None
            if hasattr(self.app, "view_palettes") and 5 in self.app.view_palettes:
                src = self.app.view_palettes[5]
            elif hasattr(self.app, "display_mode_dialog") and self.app.display_mode_dialog:
                dlg = self.app.display_mode_dialog
                if hasattr(dlg, "view_palettes") and 5 in dlg.view_palettes:
                    src = dlg.view_palettes[5]
            
            if src:
                self.cut_palette = {int(code): dict(info) for code, info in src.items()}
                print(f"   ✅ Initialized from Display Mode: {len(self.cut_palette)} classes")
            else:
                # Final fallback: keep existing or empty
                if not hasattr(self, "cut_palette"):
                    self.cut_palette = {}
                print("   ℹ️ No palette source - using existing/empty")
        

        
        if len(self.cut_points) == 0:
            print("❌ No points in final cut!")
            
            # ✅ BUG #11 FIX: Reset state
            self._state = CutSectionState.IDLE
            self.cut_phase = 0
            self.cut_points = None
            self._clear_preview_actors()
            self.app.statusBar().showMessage("❌ Cut section is empty", 3000)
            return

        # Index mapping
        if parent_index_map is not None:
            self._cut_index_map = parent_index_map
        else:
            self._rebuild_cut_index_map()

        self._ensure_cut_section_dock()
        
        # Clear preview from cross-section
        print("  🧹 Clearing preview from cross-section...")
        for a in (self.line_actor, self.buffer_actor_upper, self.buffer_actor_lower):
            if a:
                try:
                    if self.active_vtk:
                        self.active_vtk.renderer.RemoveActor(a)
                except Exception:
                    pass
        
        self.line_actor = self.buffer_actor_upper = self.buffer_actor_lower = None
        
        try:
            if self.active_vtk:
                _safe_vtk_render(self.active_vtk)
        except Exception:
            pass
        
        # Plot to cut section widget
        _plot_t0 = time.perf_counter()
        self._plot_cut_to_dedicated_widget(self.cut_points)
        if persistent_nested_cut:
            _nested_plot_ms = (time.perf_counter() - _plot_t0) * 1000.0
        
        # ✅ RESTORE DOCK FROM MINIMIZED STATE (NEW CODE)
        print("   🔄 Ensuring cut dock is visible and active...")
        
        if self.cut_dock.isMinimized():
            print("      → Restoring from minimized state")
            self.cut_dock.showNormal()  # Restore from minimized
        elif self.cut_dock.isHidden():
            print("      → Showing hidden dock")
            self.cut_dock.show()
        else:
            self.cut_dock.setVisible(True)  # Ensure visible
        
        # Bringing an already-active Windows/Qt dock to the front on every
        # nested cut causes avoidable focus/layout work.  It is only needed for
        # first/non-persistent activation; a Cut-in-Cut final click already came
        # from this dock.
        if not persistent_nested_cut:
            self.cut_dock.raise_()
            self.cut_dock.activateWindow()

            # Explicitly set window state to active (removes minimize flag)
            from PySide6.QtCore import Qt
            self.cut_dock.setWindowState(
                self.cut_dock.windowState() & ~Qt.WindowMinimized | Qt.WindowActive
            )
            print("   ✅ Cut dock shown and activated")
        else:
            print("   ⚡ Cut dock already active - focus churn skipped")
        
        self.is_cut_view_active = True
        
        try:
            self._cut_camera_state = self.cut_vtk.camera_position
        except Exception:
            pass
        
        self._set_camera_along_tangent(self.cut_vtk, self.cut_points, self.section_tangent)

        # ------------------------------------------------------------
        # FAST PERSISTENT CUT-IN-CUT PATH
        # ------------------------------------------------------------
        # The completed nested cut is already plotted.  While Cut-in-Cut stays
        # armed there is no reason to restore every cross-section interactor,
        # create a new cut ClassificationInteractor, restore classification, and
        # immediately tear all of that down again.  Reuse the existing cut-dock
        # observers/style and reset only the lightweight placement state.
        if persistent_nested_cut:
            _rearm_t0 = time.perf_counter()
            if self._fast_rearm_persistent_cut_in_cut():
                _rearm_ms = (time.perf_counter() - _rearm_t0) * 1000.0
                _total_ms = (time.perf_counter() - _nested_total_t0) * 1000.0
                print(
                    "CUT_IN_CUT_PROFILE "
                    f"input={_nested_input_count} output={len(self.cut_points)} "
                    f"filter_rotate={_nested_filter_rotate_ms:.2f}ms "
                    f"plot={_nested_plot_ms:.2f}ms "
                    f"rearm={_rearm_ms:.2f}ms total={_total_ms:.2f}ms "
                    "interactor_rebuild=0 classification_toggle=0"
                )
                return

            print("⚠️ CUT_IN_CUT_FAST_REARM unavailable - using original safe re-arm path")

        # ✅ CRITICAL FIX: Re-attach ClassificationInteractor to ALL cross-section views
        # This ensures proper camera controls (rotation/pan/zoom) are restored
        print("   🔓 Restoring ClassificationInteractor in cross-section views...")
        
        if hasattr(self.app, 'section_vtks'):
            from vtkmodules.vtkInteractionStyle import vtkInteractorStyleImage

            if not hasattr(self.app, '_section_right_click_observers'):
                self.app._section_right_click_observers = {}
            sc = getattr(self.app, "section_controller", None)
            right_click_cb = getattr(sc, "_on_section_right_click", None) if sc else None

            ci_map = getattr(self.app, "classify_interactors", {})
            for view_index, vtk_widget in self.app.section_vtks.items():
                try:
                    iren = vtk_widget.interactor
                    # Restore plain pan/zoom style — classification will overlay
                    # on top only when the user actually picks a classify tool.
                    style = vtkInteractorStyleImage()
                    iren.SetInteractorStyle(style)
                    # Discard the stale ClassificationInteractor wrapper for this view
                    # so the right-click guard (classify_interactors check) is accurate.
                    old_ci = ci_map.pop(view_index, None)
                    if old_ci is not None:
                        try:
                            old_ci.cleanup()
                        except Exception:
                            pass
                    # Reinstall right-click reactivation observer after style replacement.
                    old_tag = self.app._section_right_click_observers.get(view_index)
                    if old_tag is not None:
                        try:
                            iren.RemoveObserver(old_tag)
                        except Exception:
                            pass
                    if right_click_cb is not None:
                        tag = iren.AddObserver("RightButtonPressEvent", right_click_cb, 1.0)
                        self.app._section_right_click_observers[view_index] = tag
                    print(f"   ✅ Pan/zoom style restored for cross-section View {view_index + 1}")
                except Exception as e:
                    print(f"   ⚠️ Failed to restore interactor for View {view_index + 1}: {e}")
        
        # Clear the saved styles (no longer needed)
        if hasattr(self, '_saved_interactor_styles'):
            self._saved_interactor_styles.clear()
            print("   🧹 Cleared saved interactor styles dictionary")

        # ✅ CRITICAL: Re-install camera sync observers after interactor replacement
        # Creating new ClassificationInteractor replaces the interactor style,
        # which destroys the MouseMoveEvent observers that drive camera sync.
        if hasattr(self.app, 'view_sync_map') and self.app.view_sync_map:
            print("   🔗 Re-installing camera sync observers...")
            try:
                # Clear old observers (they're dead — interactor style was replaced)
                if hasattr(self.app, "_reset_camera_sync_observers"):
                    self.app._reset_camera_sync_observers()
                elif hasattr(self.app, "_camera_observers"):
                    for _view_idx in list(self.app._camera_observers.keys()):
                        try:
                            self.app._remove_camera_sync_observer(_view_idx)
                        except Exception:
                            pass
                    self.app._camera_observers.clear()
                
                # Re-install for all synced views
                for view_idx, vtk_widget in self.app.section_vtks.items():
                    is_synced = (
                        view_idx in self.app.view_sync_map or 
                        any(src == view_idx for src in self.app.view_sync_map.values())
                    )
                    if is_synced and hasattr(self.app, '_install_realtime_camera_observer'):
                        self.app._install_realtime_camera_observer(view_idx, vtk_widget)
                        print(f"   🔗 Camera sync re-installed for View {view_idx + 1}")
            except Exception as e:
                print(f"   ⚠️ Camera sync re-install failed: {e}")

        # Re-attach ClassificationInteractor for cut section
        print("   🔧 Re-attaching ClassificationInteractor for cut section...")
        from .interactor_classify import ClassificationInteractor

        existing_interactor = getattr(self.app, "classify_interactor", None)
        if (
            existing_interactor is not None
            and (
                getattr(existing_interactor, "is_cut_section", False)
                or getattr(existing_interactor, "vtk_widget", None) == self.cut_vtk
            )
        ):
            try:
                existing_interactor.cleanup()
            except Exception as e:
                print(f"   ⚠️ Previous cut ClassificationInteractor cleanup failed: {e}")
            self._old_classify_interactor = None
        else:
            self._old_classify_interactor = existing_interactor
        
        wrapper = ClassificationInteractor(
            self.app,
            self.cut_vtk.interactor,
            mode="2d"
        )
        
        wrapper.vtk_widget = self.cut_vtk
        wrapper.is_cut_section = True
        
        self.cut_vtk.interactor.SetInteractorStyle(wrapper.style)
        self.app.classify_interactor = wrapper
        self.app.cut_classify_interactor = wrapper
        
        if hasattr(self.app, '_shortcut_filter'):
            self.cut_vtk.interactor.installEventFilter(self.app._shortcut_filter)
        if hasattr(self.app, "_register_canvas_cursor_widget"):
            self.app._register_canvas_cursor_widget(self.cut_vtk.interactor)
            print("✅ Undo/Redo shortcuts enabled for Cut Section dock")
        if hasattr(self.app, "_install_section_wheel_zoom"):
            self.app._install_section_wheel_zoom(self.cut_vtk)
        
        def on_classify_done_cut(changed_indices):
            print("[CUT] Real-time classification changed in cut section")
            self.onclassificationchanged(changed_indices)
        
        setattr(wrapper, "on_classify_done", on_classify_done_cut)
        self._restore_classification_tools()    
        
        if not hasattr(self.app, 'original_undo_classification'):
            self.app.original_undo_classification = self.app.undo_classification
            self.app.original_redo_classification = self.app.redo_classification
            
            def undo_with_cut_refresh():
                self.app.original_undo_classification()
                if hasattr(self.app, 'cut_section_controller'):
                    ctrl = self.app.cut_section_controller
                    if ctrl.is_cut_view_active and ctrl.cut_points is not None:
                        try:
                            print("🔄 [UNDO] Refreshing cut section...")
                            ctrl._refresh_cut_colors_fast()
                        except Exception as e:
                            print(f"⚠️ Cut undo refresh failed: {e}")
            
            def redo_with_cut_refresh():
                self.app.original_redo_classification()
                if hasattr(self.app, 'cut_section_controller'):
                    ctrl = self.app.cut_section_controller
                    if ctrl.is_cut_view_active and ctrl.cut_points is not None:
                        try:
                            print("🔄 [REDO] Refreshing cut section...")
                            ctrl._refresh_cut_colors_fast()
                        except Exception as e:
                            print(f"⚠️ Cut redo refresh failed: {e}")
            
            self.app.undo_classification = undo_with_cut_refresh
            self.app.redo_classification = redo_with_cut_refresh
            print("✅ Cut section undo/redo hooks installed")
        
        print("✅ ClassificationInteractor attached to dedicated cut widget!")

        # The "Nuclear cleanup" earlier in this cut flow
        # (iren_cut.RemoveObservers("LeftButtonPressEvent")) wipes EVERY
        # LeftButtonPressEvent observer on the cut widget, including
        # PointSyncTool's own click-redirect observer if one was attached --
        # but PointSyncTool's internal bookkeeping (_cut_observer) still
        # thinks it's attached, so it never re-attaches on its own. Without
        # this, clicking inside a Cut View silently stopped redirecting to
        # Main View after any cut-from-cross/cut-from-cut action, even
        # though the feature worked immediately after the cut dock was
        # first created. Clear the stale reference and let
        # activate_for_cut_view attach a fresh observer bound to the
        # current cut_vtk widget.
        try:
            point_sync_tool = getattr(self.app, "point_sync_tool", None)
            if point_sync_tool is not None and getattr(point_sync_tool, "active", False):
                point_sync_tool._cut_observer = None
                point_sync_tool.activate_for_cut_view(self.cut_vtk)
        except Exception as _point_sync_reattach_err:
            print(f"   ⚠️ Point sync re-attach to Cut View failed: {_point_sync_reattach_err}")

        # Same nuclear-cleanup problem, same fix, for the Cut View's own
        # MicroStation-style locate observer (click in Cut View while the
        # cross-section tool is active -> pan Main View there). This runs
        # on every finalize, including the very first one (where
        # _ensure_cut_section_dock already added the observer once) -- so
        # remove any previously tracked id first to avoid a duplicate
        # observer double-firing every click. RemoveObserver on an
        # already-invalid id (wiped by the nuclear cleanup) is a safe no-op.
        try:
            if self.cut_vtk is not None:
                old_locate_id = getattr(self, "_cut_locate_observer_id", None)
                if old_locate_id is not None:
                    try:
                        self.cut_vtk.interactor.RemoveObserver(old_locate_id)
                    except Exception:
                        pass
                self._cut_locate_observer_id = self.cut_vtk.interactor.AddObserver(
                    "LeftButtonPressEvent",
                    self._on_cut_view_left_click_locate,
                    1.0,
                )
                old_move_id = getattr(self, "_cut_locate_move_observer_id", None)
                if old_move_id is not None:
                    try:
                        self.cut_vtk.interactor.RemoveObserver(old_move_id)
                    except Exception:
                        pass
                self._cut_locate_move_observer_id = self.cut_vtk.interactor.AddObserver(
                    "MouseMoveEvent",
                    self._on_cut_view_mouse_move_locate,
                    1.0,
                )
        except Exception as _locate_reattach_err:
            print(f"   ⚠️ Cut View locate observer re-attach failed: {_locate_reattach_err}")

        self._state = CutSectionState.FINALIZED
        self.cut_phase = 2
        self._restore_cross_section_left_pan()

        if persistent_nested_cut:
            try:
                print("Cut-in-Cut fallback re-arm - restoring original defensive path")
                self._activate_from_cut_dock()
                return
            except Exception as e:
                # Preserve the completed result if re-arming unexpectedly fails.
                self._state = CutSectionState.FINALIZED
                self.cut_phase = 2
                print(f"Could not re-arm persistent Cut-in-Cut mode: {e}")
        
        self.app.statusBar().showMessage("✅ Cut Section ready - Classification tools active", 0)

            
    def _reuse_cut_dock_for_new_cross_cut(self):
        """Reuse existing cut dock for NEW cut from cross-section (without closing)."""
        # ✅ MUTUAL EXCLUSION: disable Identify (Point Target) so it cannot
        # collide with the cut placement handlers (shortcut path reuses this).
        if getattr(self.app, "_deactivate_point_pick_tools", None) is not None:
            self.app._deactivate_point_pick_tools()
        print("📐 NEW FEATURE: Reusing cut dock for fresh cross-section cut...")
        purged = self._purge_stale_cut_preview_actors(include_cut_dock=True)
        if purged > 0:
            print(f"  🧹 Purged {purged} stale preview actor(s) before reusing dock")

        # Any rebuilt cut invalidates cached brush section data immediately.
        try:
            if hasattr(self.app, "_brush_cache"):
                del self.app._brush_cache
                print("  🧹 Cleared stale app brush cache before reusing cut dock")
        except Exception as e:
            print(f"  ⚠️ Failed to clear app brush cache: {e}")

        try:
            cut_ci = getattr(self.app, "cut_classify_interactor", None)
            if cut_ci is not None:
                cut_ci.cleanup()
                self.app.cut_classify_interactor = None
                print("  🧹 Cleaned previous cut ClassificationInteractor before reuse")
        except Exception as e:
            print(f"  ⚠️ Previous cut ClassificationInteractor cleanup failed: {e}")


        # ================================================================
        # FIX:
        # CutFromCross must own the cross-section left click exclusively.
        #
        # The normal activate() path already disables classification,
        # but the REUSE path was skipping it.
        #
        # Save the active classification state and remove its interactors
        # BEFORE CutFromCross observers are installed.
        # ================================================================
        self._temporarily_disable_classification()

        print(
            "  🔒 CUT_CLASSIFICATION_GUARD status=armed "
            "source=cross reuse=1 classification_suspended=1"
        )


        # ✅ Mark source as cross-section
        self._cut_source = 'cross'
        self._suspend_cross_section_left_pan()

        # ✅ STEP 1: Clear old observers from cut dock (tracked IDs)
        if 'cut_dock' in self._view_observer_ids:
            old_ids = self._view_observer_ids['cut_dock']
            if self.cut_vtk is not None:
                iren = self.cut_vtk.interactor
                for oid in old_ids:
                    try:
                        iren.RemoveObserver(oid)
                        print(f"  🧹 Removed tracked observer: {oid}")
                    except Exception:
                        pass
            del self._view_observer_ids['cut_dock']

        # ═══════════════════════════════════════════════════════════════
        # ✅ CRITICAL FIX: Nuclear cleanup of ALL mouse observers on
        #    the cut dock.  After a cut-in-cut the old on_mouse_move
        #    handler from _activate_from_cut_dock() may still be alive
        #    (observer IDs survive SetInteractorStyle changes).  When
        #    state flips to WAITING_CENTER these ghosts would draw
        #    preview lines inside the cut dock.
        # ═══════════════════════════════════════════════════════════════
        if self.cut_vtk is not None:
            try:
                iren_cut = self.cut_vtk.interactor
                iren_cut.RemoveObservers("LeftButtonPressEvent")
                iren_cut.RemoveObservers("MouseMoveEvent")
                print("  🧹 Nuclear cleanup: ALL LeftButton/MouseMove "
                    "observers removed from cut dock")
            except Exception as e:
                print(f"  ⚠️ Cut dock nuclear cleanup warning: {e}")
        # ═══════════════════════════════════════════════════════════════

        # ✅ STEP 2: Clear ALL preview actors from ALL views
        # ✅ FIX: Save references FIRST, then remove from cross-section views,
        # then from cut dock, then clear references.
        # Old code set self.line_actor = None BEFORE _clear_preview_actors(),
        # so cross-section view actors were never removed (ghost lines).
        _saved_line = self.line_actor
        _saved_upper = self.buffer_actor_upper
        _saved_lower = self.buffer_actor_lower
        _saved_cut_upper = self.cut_preview_upper
        _saved_cut_lower = self.cut_preview_lower
        _saved_cut_core = self.cut_core_actor
        _saved_cut_buffer = self.cut_buffer_actor

        # Remove from ALL cross-section views first
        if hasattr(self.app, 'section_vtks'):
            for view_idx, vtk_widget in self.app.section_vtks.items():
                try:
                    ren = vtk_widget.renderer
                    if ren is None:
                        continue
                    for actor in [_saved_line, _saved_upper, _saved_lower]:
                        if actor is not None:
                            try:
                                ren.RemoveActor(actor)
                            except Exception:
                                pass
                    _safe_vtk_render(vtk_widget)
                except Exception:
                    pass

        # Remove from cut dock
        if self.cut_vtk and hasattr(self.cut_vtk, "renderer") and self.cut_vtk.renderer:
            ren_cut = self.cut_vtk.renderer
            for a in [_saved_cut_upper, _saved_cut_lower, _saved_line,
                      _saved_cut_core, _saved_cut_buffer]:
                if a is not None:
                    try:
                        ren_cut.RemoveActor(a)
                    except Exception:
                        pass
            try:
                _safe_vtk_render(self.cut_vtk)
            except Exception:
                pass

        # Clear ALL references
        self.cut_preview_upper = None
        self.cut_preview_lower = None
        self.line_actor = None
        self.buffer_actor_upper = None
        self.buffer_actor_lower = None
        self.cut_core_actor = None
        self.cut_buffer_actor = None

        for attr in ['_line_actor_points', '_line_actor_poly',
                    '_cut_preview_upper_points', '_cut_preview_upper_lines',
                    '_cut_preview_upper_poly',
                    '_cut_preview_lower_points', '_cut_preview_lower_lines',
                    '_cut_preview_lower_poly']:
            if hasattr(self, attr):
                delattr(self, attr)

        # ✅ STEP 3.5: Clear previous cut points from the cut dock itself.
        # Keep the dock/palette alive, but make the view empty while the new
        # cut tool is waiting for user input.
        self._clear_cut_dock_points(preserve_source_data=False)

        # ✅ FIX: Clear section locate rubber-band from cross-section views
        try:
            sc = getattr(self.app, 'section_controller', None)
            if sc is not None and hasattr(sc, 'clear_locate_state'):
                sc.clear_locate_state()
            if sc is not None and hasattr(sc, 'set_section_locate_enabled'):
                sc.set_section_locate_enabled(False, clear_state=True)
        except Exception:
            pass

        # ✅ FIX: Clear classification interactor's locate line in cross-section views
        try:
            ci = getattr(self.app, 'classify_interactor', None)
            if ci is not None and hasattr(ci, '_clear_locate_state'):
                ci._clear_locate_state()
            elif ci is not None and hasattr(ci, '_clear_all_previews'):
                ci._clear_all_previews()
        except Exception:
            pass

        # ✅ STEP 4: Reset state for NEW cross→cut
        self._detach_all_view_observers()
        self._state = CutSectionState.WAITING_CENTER
        self.cut_phase = 0
        self.center_point = None
        self.dynamic_depth = getattr(self.app, "default_cut_width", 1.0)
        self.section_tangent = None
        self.is_cut_view_active = False
        self.accumulated_rotation = 0
        self.original_section_tangent = None
        self._reset_depth_adjustment_guard()
        self._last_update_time = 0  
        self._last_mouse_pos = None      # ← ADD
        # ✅ STEP 5: Reset depth spinbox
        if self.depth_spin:
            self.depth_spin.blockSignals(True)
            self.depth_spin.setValue(self.dynamic_depth)
            self.depth_spin.blockSignals(False)

        # ✅ STEP 6: Save cross-section state
        self._save_section_controller_state()

        # ✅ STEP 6.5: Initialize saved interactor styles dictionary
        if not hasattr(self, '_saved_interactor_styles'):
            self._saved_interactor_styles = {}

        # ✅ STEP 7: Attach observers to CROSS-SECTION views (not cut dock)
        print("  🔄 Attaching observers to cross-section views for NEW cut...")
        for view_index, vtk_widget in self.app.section_vtks.items():
            iren = vtk_widget.interactor

            if view_index not in self._saved_interactor_styles:
                current_style = iren.GetInteractorStyle()
                self._saved_interactor_styles[view_index] = current_style
                print(f"📌 Saved interactor style for cross-section View {view_index + 1}")

            cut_style = CutSectionInteractorStyle()
            cut_style.app = self.app
            cut_style.vtk_widget = vtk_widget
            iren.SetInteractorStyle(cut_style)
            print(f"🔒 Camera rotation BLOCKED in cross-section View {view_index + 1}")

            def make_click_handler(vw, v_idx):
                def on_left_click(obj, evt):
                    if self._cut_source != 'cross':
                        return
                    if self._state not in (CutSectionState.WAITING_CENTER, CutSectionState.WAITING_DEPTH):
                        return
                    self.app.section_controller.active_view = v_idx
                    self.active_vtk = vw
                    pos = vw.interactor.GetEventPosition()
                    picker = vtk.vtkWorldPointPicker()
                    picker.Pick(pos[0], pos[1], 0, vw.renderer)
                    pt = np.array(picker.GetPickPosition())
                    if np.allclose(pt, (0, 0, 0), atol=1e-6):
                        return
                    if self._state == CutSectionState.WAITING_CENTER:
                        self.center_point = pt
                        self._state = CutSectionState.WAITING_DEPTH
                        self.cut_phase = 1
                        self._reset_depth_adjustment_guard()
                        self._draw_dynamic_center_line(self.center_point)
                        return
                    if self._state == CutSectionState.WAITING_DEPTH:
                        if not getattr(self, "_depth_adjustment_started", False):
                            print("  ℹ️ Ignoring finalize click until depth is adjusted")
                            return
                        self._finalize_dynamic_cut_section()
                        return
                return on_left_click

            def make_move_handler(vw, v_idx):
                def on_mouse_move(obj, evt):
                    if self._cut_source != 'cross':
                        return
                    if self._state not in (CutSectionState.WAITING_CENTER, CutSectionState.WAITING_DEPTH):
                        return

                    # ✅ THROTTLING: Prevent excessive updates (60 FPS cap)
                    import time
                    current_time = time.time()
                    if current_time - self._last_update_time < 0.016:
                        return
                    self._last_update_time = current_time

                    pos = vw.interactor.GetEventPosition()

                    # ✅ Dead zone: ignore tiny mouse movements
                    if self._last_mouse_pos is not None:
                        dx = abs(pos[0] - self._last_mouse_pos[0])
                        dy = abs(pos[1] - self._last_mouse_pos[1])
                        if dx < 2 and dy < 2:
                            return
                    self._last_mouse_pos = pos

                    picker = vtk.vtkWorldPointPicker()
                    picker.Pick(pos[0], pos[1], 0, vw.renderer)

                    curr = np.array(picker.GetPickPosition())
                    if np.allclose(curr, (0, 0, 0), atol=1e-6):
                        return
                    if self._state == CutSectionState.WAITING_CENTER:
                        self.app.section_controller.active_view = v_idx
                        self.active_vtk = vw
                        self._draw_dynamic_center_line(curr)
                        return
                    if self._state == CutSectionState.WAITING_DEPTH and self.center_point is not None:
                        axis = 0 if getattr(self.app, "cross_view_mode", "side") == "side" else 1
                        old_depth = self.dynamic_depth
                        self.dynamic_depth = abs(curr[axis] - self.center_point[axis])
                        self._mark_depth_adjustment_started()

                        # ✅ Draw preview ONLY in cross-section views
                        self._draw_dynamic_band_preview(self.center_point, self.dynamic_depth)

                        # ═══════════════════════════════════════════════════
                        # ✅ FIX: Do NOT draw preview in cut dock.
                        #    We are cutting FROM cross-section, so the
                        #    preview lines belong in cross-section views
                        #    only.  Drawing them in the cut dock confused
                        #    users after a cut-in-cut sequence.
                        # ═══════════════════════════════════════════════════
                        # REMOVED:
                        #     self._draw_cut_section_preview(
                        #         self.center_point, self.dynamic_depth)
                        # ═══════════════════════════════════════════════════

                        if self.depth_spin:
                            self.depth_spin.blockSignals(True)
                            self.depth_spin.setValue(self.dynamic_depth)
                            self.depth_spin.blockSignals(False)
                        return
                return on_mouse_move

            # <----------
            try:
                lid = iren.AddObserver("LeftButtonPressEvent",
                                    make_click_handler(vtk_widget, view_index), 100.0)
                mid = iren.AddObserver("MouseMoveEvent",
                                    make_move_handler(vtk_widget, view_index), 100.0)
                self._view_observer_ids[view_index] = [lid, mid]
                print(f"✅ Cut tool observers attached to cross-section "
                    f"View {view_index + 1}: LeftButton={lid}, MouseMove={mid}")
            except Exception as e:
                print(f"⚠️ Observer attachment failed for View {view_index + 1}: {e}")
                if view_index in self._view_observer_ids:
                    del self._view_observer_ids[view_index]

        self.app.statusBar().showMessage(
            "✂️ Cut Section (Reusing dock): click center in cross-section, "
            "adjust depth, click to finalize", 0)
        print("✅ Cut dock reused - ready for new cross-section cut")

    def _force_deactivate_pending_state(self):
        """
        ✅ CRITICAL FIX: Force deactivate any pending cut tool state.
        
        This method ensures symmetric tool activation by:
        1. Clearing ALL preview actors from ALL views (including cut dock)
        2. Detaching ALL observers from ALL views  
        3. Restoring interactor styles in cross-section views
        4. Resetting state machine to IDLE
        5. Re-enabling classification tools
        
        Must be called at the START of any shortcut activation to prevent
        blocked states where one tool prevents another from executing.
        """
        # Check if there's actually something to clean up
        has_pending_state = (
            self._state != CutSectionState.IDLE or
            self.line_actor is not None or
            self.buffer_actor_upper is not None or
            self.buffer_actor_lower is not None or
            self.cut_preview_upper is not None or
            self.cut_preview_lower is not None or
            len(self._view_observer_ids) > 0 or
            (hasattr(self, '_saved_interactor_styles') and len(self._saved_interactor_styles) > 0)
        )
        
        if not has_pending_state:
            return  # Nothing to clean up
        
        print("\n🔄 Force deactivating pending cut tool state...")
        print(f"   Current state: {self._state}")
        print(f"   Observer IDs: {list(self._view_observer_ids.keys())}")
        print(f"   Saved styles: {list(getattr(self, '_saved_interactor_styles', {}).keys())}")
        
        # ========== STEP 1: Clear preview actors from ALL views ==========
        actors_to_clear = [
            self.line_actor,
            self.buffer_actor_upper,
            self.buffer_actor_lower
        ]
        
        # Clear from cross-section views
        if hasattr(self.app, 'section_vtks'):
            for view_idx, vtk_widget in self.app.section_vtks.items():
                try:
                    ren = vtk_widget.renderer
                    for actor in actors_to_clear:
                        if actor is not None:
                            try:
                                ren.RemoveActor(actor)
                            except Exception:
                                pass
                    _safe_vtk_render(vtk_widget)
                except Exception as e:
                    print(f"   ⚠️ Actor cleanup for view {view_idx}: {e}")
        
        # Clear from active_vtk if set
        if self.active_vtk is not None:
            try:
                ren = self.active_vtk.renderer
                for actor in actors_to_clear:
                    if actor is not None:
                        try:
                            ren.RemoveActor(actor)
                        except Exception:
                            pass
                _safe_vtk_render(self.active_vtk)
            except Exception:
                pass
        
        # ✅ CRITICAL FIX: Clear preview actors from CUT DOCK view
        _waiting_for_cut = self._state in (
            CutSectionState.WAITING_CENTER,
            CutSectionState.WAITING_DEPTH,
        )
        # Persistent Cut-in-Cut waits for the next placement while the previous
        # result remains the valid cut-dock dataset.  Deactivating that tool must
        # remove only its preview lines, never the completed point-cloud actor.
        _has_completed_nested_result = (
            self._cut_source == 'cut' and self._has_valid_cut_dock()
        )
        _incomplete_cut = _waiting_for_cut and not _has_completed_nested_result
        if self.cut_vtk is not None and hasattr(self.cut_vtk, 'renderer') and self.cut_vtk.renderer:
            try:
                ren = self.cut_vtk.renderer
                
                # Always clear preview actors (lines)
                cut_dock_actors = [
                    self.line_actor,           # ✅ Center line in cut dock
                    self.cut_preview_upper,    # ✅ Upper buffer line
                    self.cut_preview_lower,    # ✅ Lower buffer line
                ]
                
                # Only clear finalized cut data if cut was INCOMPLETE
                # (when cut is done, user wants to see results for classification)
                if _incomplete_cut:
                    cut_dock_actors.extend([
                        self.cut_core_actor,       # ✅ Finalized cut data
                        self.cut_buffer_actor,     # ✅ Buffer actor
                    ])
                
                removed_count = 0
                for actor in cut_dock_actors:
                    if actor is not None:
                        try:
                            ren.RemoveActor(actor)
                            removed_count += 1
                        except Exception:
                            pass
                
                if removed_count > 0:
                    print(f"   🧹 Removed {removed_count} preview actors from cut dock")
                
                # Force render to show the cleared view
                _safe_vtk_render(self.cut_vtk)
                
            except Exception as e:
                print(f"   ⚠️ Cut dock actor cleanup error: {e}")
        
        # ✅ SAFETY NET: Purge any remaining tagged preview actors from ALL renderers
        purged = self._purge_stale_cut_preview_actors(include_cut_dock=True)
        if purged > 0:
            print(f"   🧹 Purged {purged} additional tagged preview actor(s)")
            if self.cut_vtk is not None:
                try:
                    _safe_vtk_render(self.cut_vtk)
                except Exception:
                    pass
        
        # ✅ FIX: Also clean Qt overlay and main VTK preview actors
        try:
            sc = getattr(self.app, 'section_controller', None)
            if sc is not None and hasattr(sc, 'clear_preview'):
                sc.clear_preview(force_overlay_destroy=True)
        except Exception:
            pass
        
        # ✅ FIX: Also clear section locate rubber-band from cross-section views
        try:
            sc = getattr(self.app, 'section_controller', None)
            if sc is not None and hasattr(sc, 'clear_locate_state'):
                sc.clear_locate_state()
            if sc is not None and hasattr(sc, 'set_section_locate_enabled'):
                sc.set_section_locate_enabled(False, clear_state=True)
        except Exception:
            pass
        
        # Clear actor references
        self.line_actor = None
        self.buffer_actor_upper = None
        self.buffer_actor_lower = None

        # Clear cut dock data references (only if cut was incomplete)
        if _incomplete_cut:
            self.cut_core_actor = None
            self.cut_buffer_actor = None
            self.cut_preview_upper = None
            self.cut_preview_lower = None
        
        # Clear cached geometry objects
        for attr in ['_line_actor_points', '_line_actor_poly', '_line_actor_mapper',
                    '_buffer_actor_upper_points', '_buffer_actor_upper_lines', 
                    '_buffer_actor_upper_poly', '_buffer_actor_upper_mapper',
                    '_buffer_actor_lower_points', '_buffer_actor_lower_lines', 
                    '_buffer_actor_lower_poly', '_buffer_actor_lower_mapper',
                    '_cut_preview_upper_points', '_cut_preview_upper_lines',
                    '_cut_preview_upper_poly', '_cut_preview_upper_mapper',
                    '_cut_preview_lower_points', '_cut_preview_lower_lines',
                    '_cut_preview_lower_poly', '_cut_preview_lower_mapper']:
            if hasattr(self, attr):
                try:
                    delattr(self, attr)
                except Exception:
                    pass
        
        print("   ✅ Preview actors cleared")
        
        # ========== STEP 2: Detach tracked observers ==========
        # Only remove observers that WE added (tracked in _view_observer_ids)
        # ✅ FIX: Do NOT nuclear-remove ALL LeftButton/MouseMove observers —
        # that destroys section_controller's locate/rubber-band observers,
        # camera sync observers, and right-click classification reactivation.
        for v_idx, ids in list(self._view_observer_ids.items()):
            try:
                if v_idx == 'cut_dock':
                    if self.cut_vtk is not None:
                        iren = self.cut_vtk.interactor
                        for oid in ids:
                            try:
                                iren.RemoveObserver(oid)
                            except Exception:
                                pass
                elif v_idx in getattr(self.app, "section_vtks", {}):
                    iren = self.app.section_vtks[v_idx].interactor
                    for oid in ids:
                        try:
                            iren.RemoveObserver(oid)
                        except Exception:
                            pass
            except Exception as e:
                print(f"   ⚠️ Observer detach for {v_idx}: {e}")
        
        self._view_observer_ids.clear()
        print("   ✅ Tracked observers detached (other tools' observers preserved)")

        try:
            sc = getattr(self.app, 'section_controller', None)
            if sc is not None and hasattr(sc, 'set_section_locate_enabled'):
                sc.set_section_locate_enabled(True, clear_state=True)
        except Exception:
            pass
        
        # ========== STEP 3: Restore interaction after observer cleanup ==========
        # Always rebind styles because RemoveObservers(MouseMoveEvent) can
        # detach current style callbacks and leave pan inactive.
        self._restore_cross_section_pan_zoom_styles()
        if hasattr(self, '_saved_interactor_styles') and self._saved_interactor_styles:
            self._saved_interactor_styles.clear()
            print("   🧹 Cleared saved interactor styles dictionary")
        
        # ========== STEP 4: Reset state machine ==========
        self._state = CutSectionState.IDLE
        self.cut_phase = 0
        self._restore_cross_section_left_pan()
        self.center_point = None
        self.dynamic_depth = getattr(self.app, "default_cut_width", 1.0)
        self.section_tangent = None
        self.active_vtk = None
        self._reset_depth_adjustment_guard()
        
        # Reset depth spinbox if exists
        if self.depth_spin:
            try:
                self.depth_spin.blockSignals(True)
                self.depth_spin.setValue(self.dynamic_depth)
                self.depth_spin.blockSignals(False)
            except Exception:
                pass
        
        print("   ✅ State machine reset to IDLE")
        
        # ========== STEP 5: Re-enable classification tools ==========
        self._restore_classification_tools()
        
        # Update status bar
        try:
            self.app.statusBar().showMessage("🔄 Cut tool state reset", 1500)
        except Exception:
            pass
        
        print("✅ Pending state deactivated\n")

    def _suspend_cross_section_left_pan(self):
        """Give pending cross-to-cut placement exclusive ownership of left click."""
        if self._suspended_cross_pan_button is not None:
            return
        current = getattr(self.app, "panning_button", "scroll")
        if current != "left":
            return
        self._suspended_cross_pan_button = current
        self.app.panning_button = "scroll"
        # End any drag that began immediately before the shortcut was handled.
        for vtk_widget in getattr(self.app, "section_vtks", {}).values():
            try:
                style = vtk_widget.interactor.GetInteractorStyle()
                if style is not None:
                    style.OnMiddleButtonUp()
                    style.OnLeftButtonUp()
            except Exception:
                pass
        print("Cut-from-cross active: left-click pan suspended; middle-click pan remains available")

    def _restore_cross_section_left_pan(self):
        """Restore the configured left-pan mapping after cut placement exits."""
        if self._suspended_cross_pan_button is None:
            return
        self.app.panning_button = self._suspended_cross_pan_button
        self._suspended_cross_pan_button = None
        print("Cut-from-cross inactive: left-click pan restored")

    def owns_cross_section_left_click(self, vtk_widget=None):
        """Whether a pending cross-to-cut placement owns this viewport click."""
        if self._cut_source != "cross" or self._state not in (
            CutSectionState.WAITING_CENTER,
            CutSectionState.WAITING_DEPTH,
        ):
            return False
        if vtk_widget is None:
            return True
        return any(
            candidate is vtk_widget
            for candidate in getattr(self.app, "section_vtks", {}).values()
        )

    def _has_valid_cut_dock(self):
        """
        ✅ Check if there's a valid cut dock with data that can be used.
        
        This is more reliable than checking is_cut_view_active because
        that flag can be incorrectly reset by _reuse_cut_dock_for_new_cross_cut()
        while the actual cut dock resources still exist.
        """
        return (
            self.cut_vtk is not None and
            self.cut_points is not None and
            len(self.cut_points) > 0
        )

    def deactivate_tool_only(self):
        """
        Deactivate pending cut placement without closing a completed cut dock.

        This is used when leaving the Tools ribbon: unfinished interaction should
        stop, but an existing cut section window must remain visible.
        """
        try:
            has_persistent_cut = self._has_valid_cut_dock() and self._state not in (
                CutSectionState.WAITING_CENTER,
                CutSectionState.WAITING_DEPTH,
            )

            if has_persistent_cut:
                print("Leaving Tools tab - keeping cut section dock open")
                return

            self._force_deactivate_pending_state()
        except Exception as e:
            print(f"deactivate_tool_only failed: {e}")
            import traceback
            traceback.print_exc()

    def cancel_persistent_cut_in_cut(self):
        """ESC: stop a re-armed nested cut while keeping its last result visible."""
        waiting = self._state in (
            CutSectionState.WAITING_CENTER,
            CutSectionState.WAITING_DEPTH,
        )
        if self._cut_source != "cut" or not waiting:
            return False

        has_completed_result = self._has_valid_cut_dock()
        print("ESC: deactivating persistent Cut-in-Cut placement")

        # Escape means leave tools off; do not restore the classification tool
        # that nested-cut activation temporarily saved.
        if hasattr(self, "_saved_classify_state"):
            del self._saved_classify_state

        self._force_deactivate_pending_state()
        self.is_cut_view_active = bool(has_completed_result)

        # Return the completed cut dock to its ordinary left-pan/zoom style.
        if has_completed_result and self.cut_vtk is not None:
            try:
                self.cut_vtk.interactor.SetInteractorStyle(
                    CutSectionInteractorStyle(self.app, self.cut_vtk)
                )
                _safe_vtk_render(self.cut_vtk)
            except Exception as e:
                print(f"Cut-in-Cut ESC pan restore warning: {e}")

        try:
            self.app.active_classify_tool = None
            self.app.statusBar().showMessage(
                "Cut-in-Cut deactivated - left-click pan restored", 2500
            )
        except Exception:
            pass

        print("Persistent Cut-in-Cut off; completed cut preserved and left pan restored")
        return True

    def activate_from_cross_shortcut(self):
        """Shortcut: Shift+1 - new cut from cross-section, reusing dock if present."""
        try:
            if self._cut_source == 'cross' and self._state in (
                CutSectionState.WAITING_CENTER,
                CutSectionState.WAITING_DEPTH,
            ):
                print("ℹ️ Shift+1 ignored - cross-section cut setup already active")
                return

            # ✅ CRITICAL: Force deactivate any pending state FIRST
            if self._state != CutSectionState.IDLE:
                print("🔄 Shift+1: Clearing pending cut state first...")
                self._force_deactivate_pending_state()
            
            # ✅ FIX: Check actual resources, not just the flag
            if self._has_valid_cut_dock():
                print("⚡ Shift+1: REUSE cut dock for NEW cross-section cut")
                self._reuse_cut_dock_for_new_cross_cut()
            else:
                print("⚡ Shift+1: Normal cross-section cut activate()")
                self.activate(show_source_dialog=False, shortcut_preference='cross')
        except Exception as e:
            print(f"⚠️ activate_from_cross_shortcut error: {e}")
            import traceback
            traceback.print_exc()


    def activate_from_cut_shortcut(self):
        """Shortcut: Shift+2 – nested cut from existing cut view."""
        try:
            if self._cut_source == 'cut' and self._state in (
                CutSectionState.WAITING_CENTER,
                CutSectionState.WAITING_DEPTH,
            ):
                print("ℹ️ Shift+2 ignored - nested cut setup already active")
                return

            # ✅ CRITICAL: Force deactivate any pending state FIRST
            if self._state != CutSectionState.IDLE:
                print("🔄 Shift+2: Clearing pending Cut-in-Cross state first...")
                self._force_deactivate_pending_state()
            
            # ✅ FIX: Check actual resources, not just the flag
            if self._has_valid_cut_dock():
                # ✅ RECOVERY: Restore flag if it was incorrectly reset
                if not self.is_cut_view_active:
                    print("🔧 Restoring is_cut_view_active flag (was incorrectly reset)")
                    self.is_cut_view_active = True
                
                print("⚡ Shift+2: Nested cut from existing cut view")
                self._activate_from_cut_dock()
            else:
                print("ℹ️ Shift+2: No active cut view - use Shift+1 first to create a cut section")
                self.app.statusBar().showMessage(
                    "ℹ️ No cut section active. Use Shift+1 first to create a cut from cross-section.", 
                    3000
                )
        except Exception as e:
            print(f"⚠️ activate_from_cut_shortcut error: {e}")
            import traceback
            traceback.print_exc()

    def _temporarily_disable_classification(self):
        """
        ✅ CRITICAL FIX: Disable classification tools during cut section setup
        Prevents accidental classification while selecting depth
        """
        try:
            print("🔒 Temporarily disabling classification tools...")
           
            # Save current classification state
            self._saved_classify_state = {
                'active_tool': getattr(self.app, 'active_classify_tool', None),
                'interactor': getattr(self.app, 'classify_interactor', None),
                'from_classes': getattr(self.app, 'from_classes', None),
                'to_class': getattr(self.app, 'to_class', None)
            }
           
            # Deactivate classification tool
            # ✅ FIX: Use deactivate_classification_tool (preserve_cross_section=True) so
            # the main view's interactor is properly restored to vtkInteractorStyleImage.
            # The older deactivate_classification() skips the main vtk_widget restore step,
            # leaving the main view unable to pan after a nested CutFromCut operation.
            if hasattr(self.app, 'deactivate_classification_tool'):
                self.app.deactivate_classification_tool(preserve_cross_section=True)
            elif hasattr(self.app, 'deactivate_classification'):
                self.app.deactivate_classification()
            else:
                self.app.active_classify_tool = None
           
            # Detach classification interactor from ALL views
            if self._saved_classify_state['interactor'] is not None:
                ci = self._saved_classify_state['interactor']
               
                # Clear from cross-section views
                if hasattr(self.app, 'section_vtks'):
                    for vtk_widget in self.app.section_vtks.values():
                        try:
                            # Reset to default camera interactor
                            iren = vtk_widget.interactor
                            default_style = vtk.vtkInteractorStyleTrackballCamera()
                            iren.SetInteractorStyle(default_style)
                        except Exception as e:
                            print(f"   ⚠️ Reset interactor warning: {e}")
               
                # Clear references
                ci.vtk_widget = None
                ci.is_cut_section = False
           
            # Update UI to show tools are disabled
            if hasattr(self.app, 'statusBar'):
                self.app.statusBar().showMessage(
                    "🔒 Classification tools disabled during cut section setup",
                    2000
                )
           
            print("   ✅ Classification tools disabled")
           
        except Exception as e:
            print(f"   ⚠️ Disable classification warning: {e}")
 
 
    def _restore_classification_tools(self):
        """
        ✅ Restore classification tools after cut section is finalized
        """
        try:
            if not hasattr(self, '_saved_classify_state'):
                return
           
            print("🔓 Restoring classification tools...")
           
            state = self._saved_classify_state
           
            # Restore tool state
            if state['active_tool'] is not None:
                self.app.active_classify_tool = state['active_tool']
           
            if state['from_classes'] is not None:
                self.app.from_classes = state['from_classes']
           
            if state['to_class'] is not None:
                self.app.to_class = state['to_class']
           
            # Note: Classification interactor will be re-attached by _finalize_dynamic_cut_section
            # We don't restore it here to avoid conflicts
           
            print("   ✅ Classification state restored (interactor will be re-attached to cut view)")
           
            # Cleanup
            del self._saved_classify_state
           
        except Exception as e:
            print(f"   ⚠️ Restore classification warning: {e}")

    def unlock_after_classification(self):
        """
        Restore normal interaction in the cut dock after classification is complete.
        Called by app.deactivate_classification_tool / app.deactivate_classification.
        Without this method those callers silently fail with a warning and leave the
        cut dock's interactor in a stale classification style.
        """
        try:
            if self.cut_vtk is not None and hasattr(self.cut_vtk, 'interactor'):
                style = CutSectionInteractorStyle(self.app, self.cut_vtk)
                self.cut_vtk.interactor.SetInteractorStyle(style)
                print("   ✅ Cut section interactor unlocked")
        except Exception as e:
            print(f"   ⚠️ cut_section unlock_after_classification failed: {e}")

    def deactivate_if_waiting(self):
        """
        ✅ CRITICAL FIX: Deactivate cut section tool if it's in WAITING state
        Enhanced with nuclear cleanup to prevent state corruption after extended use.
        
        This prevents conflicts when user activates classification tools
        while cut section tool is still waiting for user input.
        
        Also handles corrupted states where preview actors exist but state is IDLE.
        
        Called by ClassificationInteractor when classification tools are activated.
        """
        try:
            try:
                sc = getattr(self.app, 'section_controller', None)
                if sc is not None and hasattr(sc, 'set_section_locate_enabled'):
                    sc.set_section_locate_enabled(True, clear_state=True)
            except Exception:
                pass

            # ✅ NEW: Force cleanup even if state appears IDLE
            # This handles cases where state was corrupted after extended use
            force_cleanup = False
            
            # Check if cut tool is in WAITING state
            if self._state == CutSectionState.IDLE:
                # Double-check: are there actually preview actors present?
                if (self.line_actor is not None or 
                    self.buffer_actor_upper is not None or 
                    self.buffer_actor_lower is not None):
                    print("   ⚠️ State is IDLE but preview actors exist - forcing cleanup!")
                    force_cleanup = True
                else:
                    # Truly idle, nothing to do
                    return
            
            # Check if cut is already performed (is_cut_view_active)
            waiting_for_cut = self._state in (
                CutSectionState.WAITING_CENTER,
                CutSectionState.WAITING_DEPTH,
            )
            if self.is_cut_view_active and not waiting_for_cut and not force_cleanup:
                # Cut already performed, don't deactivate
                print("   ℹ️ Cut section already performed - not deactivating")
                return
            
            # Cut tool is in WAITING state or needs force cleanup - deactivate it
            print("🔒 Deactivating cut section tool (classification tool activated)...")
            if force_cleanup:
                print("   🔥 FORCE CLEANUP MODE: Clearing corrupted state")
            
            # ✅ CRITICAL FIX: Clear preview actors from ALL cross-section views
            if hasattr(self.app, 'section_vtks'):
                actors_to_clear = [
                    self.line_actor,
                    self.buffer_actor_upper,
                    self.buffer_actor_lower
                ]
                
                for view_idx, vtk_widget in self.app.section_vtks.items():
                    try:
                        ren = vtk_widget.renderer
                        removed_count = 0
                        
                        for actor in actors_to_clear:
                            if actor is not None:
                                try:
                                    ren.RemoveActor(actor)
                                    removed_count += 1
                                except Exception as e:
                                    pass
                        
                        if removed_count > 0:
                            print(f"   🧹 Removed {removed_count} preview actors from cross-section View {view_idx + 1}")
                        
                        # Render to show the removal
                        try:
                            _safe_vtk_render(vtk_widget)
                            if removed_count > 0:
                                print(f"   ✅ Cross-section View {view_idx + 1} rendered (previews cleared)")
                        except Exception as e:
                            pass
                            
                    except Exception as e:
                        print(f"   ⚠️ Failed to clear preview from view {view_idx + 1}: {e}")
            
            # Clear actor references
            self.line_actor = None
            self.buffer_actor_upper = None
            self.buffer_actor_lower = None
            
            # Also clear cut view preview actors (if any)
            self._clear_preview_actors()
            
            # ✅ FIX: Also clean Qt overlay and main VTK preview actors
            try:
                sc = getattr(self.app, 'section_controller', None)
                if sc is not None and hasattr(sc, 'clear_preview'):
                    sc.clear_preview(force_overlay_destroy=True)
            except Exception:
                pass
            
            # ✅ FIX: Also clear section locate rubber-band from cross-section views
            try:
                sc = getattr(self.app, 'section_controller', None)
                if sc is not None and hasattr(sc, 'clear_locate_state'):
                    sc.clear_locate_state()
                if sc is not None and hasattr(sc, 'set_section_locate_enabled'):
                    sc.set_section_locate_enabled(False, clear_state=True)
            except Exception:
                pass
            
            # ✅ NUCLEAR CLEANUP: Detach ALL observers from cross-section views
            self._detach_all_view_observers()
            
            # ✅ FIX: No nuclear cleanup — tracked observers already removed above.
            # Rebind fresh pan/zoom styles to ensure interaction works.
            self._restore_cross_section_pan_zoom_styles()
            if hasattr(self, '_saved_interactor_styles') and self._saved_interactor_styles:
                self._saved_interactor_styles.clear()
            
            # Reset state to IDLE
            self._state = CutSectionState.IDLE
            self.cut_phase = 0
            self._restore_cross_section_left_pan()
            self.center_point = None
            self.dynamic_depth = None
            self.section_tangent = None
            
            # ✅ EXTRA: Clear any cached geometry
            for attr in ['_line_actor_points', '_line_actor_poly', 
                        '_buffer_upper_points', '_buffer_upper_poly',
                        '_buffer_lower_points', '_buffer_lower_poly']:
                if hasattr(self, attr):
                    try:
                        delattr(self, attr)
                    except Exception:
                        pass
            
            # Update status bar
            if hasattr(self.app, 'statusBar'):
                msg = "✅ Cut section tool deactivated (classification tool activated)"
                if force_cleanup:
                    msg = "✅ Cut section tool deactivated (forced cleanup - state was corrupted)"
                self.app.statusBar().showMessage(msg, 2000)
            
            print("   ✅ Cut section tool deactivated successfully (all previews and observers cleared)")
            
        except Exception as e:
            print(f"   ⚠️ Deactivate cut section warning: {e}")
            import traceback
            traceback.print_exc()
         
    def activate(self, show_source_dialog: bool = True, shortcut_preference: str | None = None):
        """Enable CutSection in all open cross-section panes."""
        try:
            # ✅ MUTUAL EXCLUSION: Identify (Point Target) must never be active
            # while a cut is taken — their click handlers collide on the same
            # cross-section views. Auto-disable Identify (it just yields).
            # Covers both ribbon and shortcut entry points.
            if getattr(self.app, "_deactivate_point_pick_tools", None) is not None:
                self.app._deactivate_point_pick_tools()

            print("🔥 Starting cut tool activation with nuclear cleanup...")
            purged = self._purge_stale_cut_preview_actors(include_cut_dock=True)
            if purged > 0:
                print(f"   🧹 Purged {purged} stale cut preview actor(s) from renderers")
            if hasattr(self.app, 'section_vtks'):
                for view_idx, vtk_widget in self.app.section_vtks.items():
                    try:
                        ren = vtk_widget.renderer
                        # Remove any cut section actors that might be lingering
                        removed_count = 0
                        
                        if self.line_actor is not None:
                            try:
                                ren.RemoveActor(self.line_actor)
                                removed_count += 1
                            except Exception:
                                pass
                        if self.buffer_actor_upper is not None:
                            try:
                                ren.RemoveActor(self.buffer_actor_upper)
                                removed_count += 1
                            except Exception:
                                pass
                        if self.buffer_actor_lower is not None:
                            try:
                                ren.RemoveActor(self.buffer_actor_lower)
                                removed_count += 1
                            except Exception:
                                pass
                        
                        if removed_count > 0:
                            print(f"   🧹 Removed {removed_count} lingering actors from View {view_idx + 1}")
                            _safe_vtk_render(vtk_widget)
                    except Exception:
                        pass

            # Clear actor references
            self.line_actor = None
            self.buffer_actor_upper = None
            self.buffer_actor_lower = None

            # Observer cleanup BEFORE adding new ones
            # ✅ FIX: Only remove tracked observers — do NOT nuclear-remove
            # ALL LeftButton/MouseMove observers as that destroys
            # section_controller's locate/rubber-band/classification observers.
            for view_index in list(self._view_observer_ids.keys()):
                if view_index in self.app.section_vtks:
                    vtk_widget = self.app.section_vtks[view_index]
                    iren = vtk_widget.interactor
                    
                    # Remove tracked observers only
                    if view_index in self._view_observer_ids:
                        old_ids = self._view_observer_ids[view_index]
                        removed_count = 0
                        for oid in old_ids:
                            try:
                                if iren.HasObserver(oid):
                                    iren.RemoveObserver(oid)
                                    removed_count += 1
                            except Exception:
                                pass
                        if removed_count > 0:
                            print(f"   🧹 Removed {removed_count} tracked observers from View {view_index + 1}")

            # Clear observer tracking dictionary
            self._view_observer_ids.clear()

            print("   ✅ Tracked observers cleared (other tools' observers preserved)")
            print("")

            # ✅ FIX: Clear section locate rubber-band from cross-section views
            # (magenta preview line that stays stuck when cut tool activates mid-locate)
            try:
                sc = getattr(self.app, 'section_controller', None)
                if sc is not None and hasattr(sc, 'clear_locate_state'):
                    sc.clear_locate_state()
            except Exception:
                pass

            # ✅ FIX: Also clear classification interactor's locate line
            try:
                ci = getattr(self.app, 'classify_interactor', None)
                if ci is not None and hasattr(ci, '_clear_locate_state'):
                    ci._clear_locate_state()
                elif ci is not None and hasattr(ci, '_clear_all_previews'):
                    ci._clear_all_previews()
            except Exception:
                pass

            self._temporarily_disable_classification()

            # # Empty the existing cut dock as soon as cut-tool activation starts.
            # # This matches cross-section behavior: old points should not remain
            # # visible while the next cut is being defined.
            # if self.cut_vtk is not None:
            #     self._clear_cut_dock_points(preserve_source_data=True)

            # ✅ NEW FEATURE: If already in cut view, ask user which source to use
            if self.cut_vtk is not None and self.cut_dock is not None:
                if not show_source_dialog:
                    preferred = (shortcut_preference or 'cross').strip().lower() if shortcut_preference else 'cross'
                    if preferred == 'cut':
                        print("⚡ Shortcut activation: using existing cut view directly (no dialog)")
                        self._activate_from_cut_dock()
                    else:
                        print("⚡ Shortcut activation: reusing dock for cross-section cut (no dialog)")
                        self._reuse_cut_dock_for_new_cross_cut()
                    return

                if getattr(self, "_cut_source_dialog_open", False):
                    print("ℹ️ Cut source dialog already open - reusing existing dialog")
                    existing = getattr(self, "_cut_source_dialog", None)
                    if existing is not None:
                        try:
                            existing.raise_()
                            existing.activateWindow()
                        except Exception:
                            pass
                    return

                print("🔄 Cut view active - asking user for cut source...")
                
                from PySide6.QtWidgets import QMessageBox
                
                msg = QMessageBox(self.app)
                msg.setWindowTitle("Cut Tool Activate")
                msg.setText("")
                msg.setWindowFlags(msg.windowFlags() | Qt.WindowCloseButtonHint)
                self._cut_source_dialog = msg
                self._cut_source_dialog_open = True
                
                btn_cross = msg.addButton("Cross Section", QMessageBox.ActionRole)
                btn_cut = msg.addButton("Cut Section", QMessageBox.ActionRole)

                try:
                    msg.exec()
                    clicked = msg.clickedButton()
                    
                    if clicked == btn_cross:
                        print("✅ User chose: Cross Section (new cut from cross-section)")
                        self._reuse_cut_dock_for_new_cross_cut()
                        return
                    elif clicked == btn_cut:
                        print("✅ User chose: Cut Section (nested cut)")
                        self._activate_from_cut_dock()
                        return
                    else:
                        print("❌ User cancelled (X button)")
                        return
                finally:
                    self._cut_source_dialog_open = False
                    if self._cut_source_dialog is msg:
                        self._cut_source_dialog = None
            
            if not hasattr(self.app, "section_vtks") or len(self.app.section_vtks) == 0:
                QMessageBox.warning(self.app, "No Cross Section",
                    "Create a cross-section first (Tools → Cross).")
                return

            if self._state != CutSectionState.IDLE:
                self._detach_all_view_observers()
                self._clear_preview_actors()

            self._state = CutSectionState.WAITING_CENTER
            self.cut_phase = 0
            self.center_point = None
            self.dynamic_depth = getattr(self.app, "default_cut_width", 1.0)
            self.section_tangent = None
            self.is_cut_view_active = False
            self.accumulated_rotation = 0
            self.original_section_tangent = None
            self._cut_source = 'cross'
            self._suspend_cross_section_left_pan()
            self._reset_depth_adjustment_guard()

            # Save section controller state
            self._save_section_controller_state()

            # ✅ CRITICAL FIX: Initialize saved interactor styles dictionary if not exists
            if not hasattr(self, '_saved_interactor_styles'):
                self._saved_interactor_styles = {}

            # ✅ CRITICAL FIX: Block camera rotation in cross-section views during cut tool
            for view_index, vtk_widget in self.app.section_vtks.items():
                iren = vtk_widget.interactor

                # ✅ Save current interactor style before blocking
                if view_index not in self._saved_interactor_styles:
                    current_style = iren.GetInteractorStyle()
                    self._saved_interactor_styles[view_index] = current_style
                    print(f"📌 Saved interactor style for cross-section View {view_index + 1}")
                
                # ✅ Block camera rotation by setting empty interactor style
                cut_style = CutSectionInteractorStyle()     # ← ADD THIS
                cut_style.app = self.app
                cut_style.vtk_widget = vtk_widget
                iren.SetInteractorStyle(cut_style) 
                print(f"🔒 Camera rotation BLOCKED in cross-section View {view_index + 1}")

                def make_click_handler(vw, v_idx):
                    def on_left_click(obj, evt):
                        if self._cut_source != 'cross':
                            return
                        if self._state not in (CutSectionState.WAITING_CENTER, CutSectionState.WAITING_DEPTH):
                            return
                        self.app.section_controller.active_view = v_idx
                        self.active_vtk = vw
                        pos = vw.interactor.GetEventPosition()
                        picker = vtk.vtkWorldPointPicker()
                        picker.Pick(pos[0], pos[1], 0, vw.renderer)
                        pt = np.array(picker.GetPickPosition())
                        if np.allclose(pt, (0, 0, 0), atol=1e-6):
                            return
                        if self._state == CutSectionState.WAITING_CENTER:
                            self.center_point = pt
                            self._state = CutSectionState.WAITING_DEPTH
                            self.cut_phase = 1
                            self._reset_depth_adjustment_guard()
                            self._draw_dynamic_center_line(self.center_point)
                            return
                        if self._state == CutSectionState.WAITING_DEPTH:
                            if not getattr(self, "_depth_adjustment_started", False):
                                print("  ℹ️ Ignoring finalize click until depth is adjusted")
                                return
                            self._finalize_dynamic_cut_section()
                            return
                    return on_left_click

                def make_move_handler(vw, v_idx):
                    def on_mouse_move(obj, evt):
                        if self._cut_source != 'cross':
                            return
                        if self._state not in (CutSectionState.WAITING_CENTER, CutSectionState.WAITING_DEPTH):
                            return

                        pos = vw.interactor.GetEventPosition()
                        picker = vtk.vtkWorldPointPicker()
                        picker.Pick(pos[0], pos[1], 0, vw.renderer)
                        curr = np.array(picker.GetPickPosition())

                        if np.allclose(curr, (0, 0, 0), atol=1e-6):
                            return

                        if self._state == CutSectionState.WAITING_CENTER:
                            self.app.section_controller.active_view = v_idx
                            self.active_vtk = vw
                            self._draw_dynamic_center_line(curr)
                            return

                        if self._state == CutSectionState.WAITING_DEPTH and self.center_point is not None:
                            axis = 0 if getattr(self.app, "cross_view_mode", "side") == "side" else 1
                            old_depth = self.dynamic_depth
                            self.dynamic_depth = abs(curr[axis] - self.center_point[axis])
                            self._mark_depth_adjustment_started()

                            # ✅ Draw preview ONLY in cross-section views
                            self._draw_dynamic_band_preview(self.center_point, self.dynamic_depth)

                            # ✅ FIX: Do NOT draw preview in cut dock when cutting
                            #    the cross-section views for this mode.
                            # REMOVED:
                            #     self._draw_cut_section_preview(
                            #         self.center_point, self.dynamic_depth)

                            if self.depth_spin:
                                self.depth_spin.blockSignals(True)
                                self.depth_spin.setValue(self.dynamic_depth)
                                self.depth_spin.blockSignals(False)
                            return

                    return on_mouse_move

                # -----------
                try:
                    lid = iren.AddObserver("LeftButtonPressEvent",
                                        make_click_handler(vtk_widget, view_index), 100.0)
                    mid = iren.AddObserver("MouseMoveEvent",
                                        make_move_handler(vtk_widget, view_index), 100.0)
                    self._view_observer_ids[view_index] = [lid, mid]
                    print(f"✅ Cut tool observers attached to cross-section "
                        f"View {view_index + 1}: LeftButton={lid}, MouseMove={mid}")
                except Exception as e:
                    print(f"⚠️ Observer attachment failed for View {view_index + 1}: {e}")
                    if view_index in self._view_observer_ids:
                        del self._view_observer_ids[view_index]

            self.app.statusBar().showMessage(
                "✂️ Cut Section: click center, adjust depth, click to finalize", 0)

        except Exception as e:
            print(f"[CutSection.activate] {e}")
            import traceback
            traceback.print_exc()

    #     """Save current section controller state before cut takes over."""
    #         self._saved_section_state = {
    #             'active_view': sc.active_view,
    #             'section_points': getattr(self.app, 'section_points', None),
    #             'section_core_points': getattr(self.app, 'section_core_points', None),
    #             'section_buffer_points': getattr(self.app, 'section_buffer_points', None),
    #             'section_core_mask': getattr(self.app, 'section_core_mask', None),
    #             'section_buffer_mask': getattr(self.app, 'section_buffer_mask', None),
    #             'section_indices': getattr(self.app, 'section_indices', None),
    #             'P1': sc.P1,
    #             'P2': sc.P2,
    #             'half_width': sc.half_width,
    #             'last_mask': sc.last_mask,
    #         }

    def _save_section_controller_state(self):
        """Save current section controller state before cut takes over - DEEP COPY."""
        try:
            sc = self.app.section_controller
            
            # Deep copy all numpy arrays to preserve data
            self._saved_section_state = {
                'active_view': sc.active_view,
                'section_points': np.copy(getattr(self.app, 'section_points', None)) 
                                if getattr(self.app, 'section_points', None) is not None else None,
                'section_core_points': np.copy(getattr(self.app, 'section_core_points', None))
                                    if getattr(self.app, 'section_core_points', None) is not None else None,
                'section_buffer_points': np.copy(getattr(self.app, 'section_buffer_points', None))
                                        if getattr(self.app, 'section_buffer_points', None) is not None else None,
                'section_core_mask': np.copy(getattr(self.app, 'section_core_mask', None))
                                    if getattr(self.app, 'section_core_mask', None) is not None else None,
                'section_buffer_mask': np.copy(getattr(self.app, 'section_buffer_mask', None))
                                    if getattr(self.app, 'section_buffer_mask', None) is not None else None,
                'section_indices': np.copy(getattr(self.app, 'section_indices', None))
                                if getattr(self.app, 'section_indices', None) is not None else None,
                'P1': np.copy(sc.P1) if sc.P1 is not None else None,
                'P2': np.copy(sc.P2) if sc.P2 is not None else None,
                'half_width': sc.half_width,
                'last_mask': np.copy(sc.last_mask) if sc.last_mask is not None else None,
            }
            print("💾 Saved section controller state (deep copy)")
        except Exception as e:
            print(f"⚠️ Could not save section state: {e}")
            import traceback
            traceback.print_exc()

    #     """Restore section controller to pre-cut state."""
            
            
    #         sc.active_view = state['active_view']
    #         self.app.section_points = state['section_points']
    #         self.app.section_core_points = state['section_core_points']
    #         self.app.section_buffer_points = state['section_buffer_points']
    #         self.app.section_core_mask = state['section_core_mask']
    #         self.app.section_buffer_mask = state['section_buffer_mask']
    #         self.app.section_indices = state['section_indices']
    #         sc.P1 = state['P1']
    #         sc.P2 = state['P2']
    #         sc.half_width = state['half_width']
    #         sc.last_mask = state['last_mask']
            

    def _restore_section_controller_state(self):
        """Restore section controller to pre-cut state with validation."""
        try:
            if self._saved_section_state is None:
                print("⚠️ No saved section state to restore")
                return
            
            sc = self.app.section_controller
            state = self._saved_section_state
            
            # Validate saved data exists
            if state['section_points'] is None:
                print("❌ ERROR: Saved section_points is None - cannot restore")
                return
            
            # Restore with validation
            sc.active_view = state['active_view']
            self.app.section_points = state['section_points']
            self.app.section_core_points = state['section_core_points']
            self.app.section_buffer_points = state['section_buffer_points']
            self.app.section_core_mask = state['section_core_mask']
            self.app.section_buffer_mask = state['section_buffer_mask']
            self.app.section_indices = state['section_indices']
            sc.P1 = state['P1']
            sc.P2 = state['P2']
            sc.half_width = state['half_width']
            sc.last_mask = state['last_mask']
            
            # Validation
            if self.app.section_points is not None and len(self.app.section_points) > 0:
                print(f"✅ Restored section controller state ({len(self.app.section_points)} points) - cross-section ready")
            else:
                print("❌ WARNING: Restored state has no points!")
                
        except Exception as e:
            print(f"❌ Could not restore section state: {e}")
            import traceback
            traceback.print_exc()
        
    def cancel_cut_section(self):
        """
        Cancel cut section, clear previews, and close dedicated widget.
        ✅ FIXED: Prevents crash by properly detaching all observers and interactors.
        ✅ FIXED: Clears VTK actor attachments and references without forcing native delete
        ✅ FIXED: Restores camera rotation in cross-section views (Bug #Camera Blocking)
        """
        try:
            # ✅ STEP 1: Set destruction flag FIRST
            self._is_destroying = True
            self._remove_cut_right_click_observer()
            print("\n🧹 CANCELING CUT SECTION...")
            try:
                sc = getattr(self.app, 'section_controller', None)
                if sc is not None and hasattr(sc, 'set_section_locate_enabled'):
                    sc.set_section_locate_enabled(True, clear_state=True)
            except Exception:
                pass
            
            # ✅ STEP 1.5: Remove ALL actors from renderers BEFORE any widget cleanup (Bug #1 + #7)

            # ✅ FIX: Also clean Qt overlay and main VTK preview actors
            try:
                sc = getattr(self.app, 'section_controller', None)
                if sc is not None and hasattr(sc, 'clear_preview'):
                    sc.clear_preview(force_overlay_destroy=True)
            except Exception:
                pass

            # ✅ FIX: Also clear section locate rubber-band from cross-section views
            try:
                sc = getattr(self.app, 'section_controller', None)
                if sc is not None and hasattr(sc, 'clear_locate_state'):
                    sc.clear_locate_state()
            except Exception:
                pass

            # Save cross-section preview actor references BEFORE nullifying them
            _cross_line = self.line_actor
            _cross_upper = self.buffer_actor_upper
            _cross_lower = self.buffer_actor_lower

            # Remove cross-section preview actors from ALL cross-section views first
            for _actor in [_cross_line, _cross_upper, _cross_lower]:
                if _actor is not None:
                    if hasattr(self.app, 'section_vtks'):
                        for _vw in self.app.section_vtks.values():
                            try:
                                _vw.renderer.RemoveActor(_actor)
                            except Exception:
                                pass
                    if self.active_vtk is not None:
                        try:
                            self.active_vtk.renderer.RemoveActor(_actor)
                        except Exception:
                            pass

            if self.cut_vtk is not None and hasattr(self.cut_vtk, 'renderer') and self.cut_vtk.renderer:
                print("   🧹 Removing actors from cut section renderer...")
                renderer = self.cut_vtk.renderer

                # List all actors to remove
                actors_to_remove = [
                    self.cut_core_actor,
                    self.cut_buffer_actor,
                    self.line_actor,
                    self.buffer_actor_upper,
                    self.buffer_actor_lower,
                    self.cut_preview_upper,
                    self.cut_preview_lower
                ]

                for actor in actors_to_remove:
                    if actor is not None:
                        try:
                            renderer.RemoveActor(actor)
                        except Exception as e:
                            print(f"    ⚠️ Actor removal warning: {e}")

                # Clear actor references immediately
                self.cut_core_actor = None
                self.cut_buffer_actor = None
                self.line_actor = None
                self.buffer_actor_upper = None
                self.buffer_actor_lower = None
                self.cut_preview_upper = None
                self.cut_preview_lower = None

                print("   ✅ All actors removed")
            else:
                # cut_vtk was already gone — still null out the references
                self.cut_core_actor = None
                self.cut_buffer_actor = None
                self.line_actor = None
                self.buffer_actor_upper = None
                self.buffer_actor_lower = None
                self.cut_preview_upper = None
                self.cut_preview_lower = None

            # Re-render cross-section views to show the cleared state
            if hasattr(self.app, 'section_vtks'):
                for _vw in self.app.section_vtks.values():
                    try:
                        _safe_vtk_render(_vw)
                    except Exception:
                        pass
            
            # ✅ STEP 2: CRITICAL - Disable render window IMMEDIATELY
            if self.cut_vtk is not None:
                try:
                    print("   🛑 Disabling render window...")
                    # CRITICAL: Finalize once (before other cleanup)
                    self._finalize_cut_render_window_once(self.cut_vtk)
                    print("   ✅ Render window disabled")
                except Exception as e:
                    print(f"   ⚠️ Render window disable: {e}")
            
            # ✅ STEP 3: Remove ALL VTK observers and timers
            if self.cut_vtk is not None:
                try:
                    print("   🛑 Removing VTK observers and timers...")
                    iren = self.cut_vtk.interactor
                    
                    # CRITICAL: Destroy ALL timers first
                    timer_id = 0
                    while timer_id < 100:  # Remove up to 100 timers
                        try:
                            iren.DestroyTimer(timer_id)
                        except Exception:
                            pass
                        timer_id += 1
                    
                    # Remove all event observers
                    iren.RemoveObservers("TimerEvent")
                    iren.RemoveObservers("ModifiedEvent")
                    iren.RemoveObservers("RenderEvent")
                    iren.RemoveObservers("LeftButtonPressEvent")
                    iren.RemoveObservers("LeftButtonReleaseEvent")
                    iren.RemoveObservers("MouseMoveEvent")
                    
                    # Terminate event loop
                    try:
                        iren.TerminateApp()
                    except Exception:
                        pass
                    
                    print("   ✅ Observers and timers removed")
                except Exception as e:
                    print(f"   ⚠️ Observer removal: {e}")
            
            # ✅ STEP 4: Detach classification interactor BEFORE anything else
            if hasattr(self.app, 'classify_interactor'):
                ci = self.app.classify_interactor
                if ci is not None:
                    # Check if attached to cut section
                    if (hasattr(ci, 'vtk_widget') and ci.vtk_widget == self.cut_vtk) or \
                    (hasattr(ci, 'is_cut_section') and getattr(ci, 'is_cut_section', False)):
                        print("   🔧 Detaching classification interactor...")
                        ci.vtk_widget = None
                        ci.is_cut_section = False
                        # Restore previous interactor if exists
                        if self._old_classify_interactor is not None:
                            self.app.classify_interactor = self._old_classify_interactor
                            print("   ✅ Restored previous interactor")
                        else:
                            self.app.classify_interactor = None
                            print("   ✅ Interactor cleared")
            
            # ✅ STEP 4.5: RESTORE camera rotation in cross-section views
            if hasattr(self.app, 'section_vtks') and hasattr(self, '_saved_interactor_styles'):
                from vtkmodules.vtkInteractionStyle import vtkInteractorStyleImage
                if not hasattr(self.app, '_section_right_click_observers'):
                    self.app._section_right_click_observers = {}
                sc = getattr(self.app, "section_controller", None)
                right_click_cb = getattr(sc, "_on_section_right_click", None) if sc else None
                ci_map = getattr(self.app, "classify_interactors", {})
                for view_index in getattr(self.app, 'section_vtks', {}):
                    if view_index in self._saved_interactor_styles:
                        try:
                            iren = self.app.section_vtks[view_index].interactor
                            iren.SetInteractorStyle(vtkInteractorStyleImage())
                            # Discard stale ClassificationInteractor wrapper for this view.
                            old_ci = ci_map.pop(view_index, None)
                            if old_ci is not None:
                                try:
                                    old_ci.cleanup()
                                except Exception:
                                    pass
                            # Reinstall right-click reactivation observer.
                            old_tag = self.app._section_right_click_observers.get(view_index)
                            if old_tag is not None:
                                try:
                                    iren.RemoveObserver(old_tag)
                                except Exception:
                                    pass
                            if right_click_cb is not None:
                                tag = iren.AddObserver("RightButtonPressEvent", right_click_cb, 1.0)
                                self.app._section_right_click_observers[view_index] = tag
                        except Exception as e:
                            print(f"   ⚠️ Interactor restore failed view {view_index}: {e}")
                self._saved_interactor_styles.clear()
                print("   🔓 All cross-section views restored")
                
                # ✅ Re-install camera sync observers (destroyed when styles were replaced)
                if hasattr(self.app, 'view_sync_map') and self.app.view_sync_map:
                    try:
                        if hasattr(self.app, "_reset_camera_sync_observers"):
                            self.app._reset_camera_sync_observers()
                        elif hasattr(self.app, "_camera_observers"):
                            for _view_idx in list(self.app._camera_observers.keys()):
                                try:
                                    self.app._remove_camera_sync_observer(_view_idx)
                                except Exception:
                                    pass
                            self.app._camera_observers.clear()
                        for v_idx, vw in self.app.section_vtks.items():
                            is_synced = (
                                v_idx in self.app.view_sync_map or
                                any(src == v_idx for src in self.app.view_sync_map.values())
                            )
                            if is_synced and hasattr(self.app, '_install_realtime_camera_observer'):
                                self.app._install_realtime_camera_observer(v_idx, vw)
                        print("   🔗 Camera sync observers re-installed")
                    except Exception as e:
                        print(f"   ⚠️ Camera sync re-install failed: {e}")
            
            # ✅ STEP 5: Detach cross-section view observers
            self._detach_all_view_observers()
            
            # ✅ STEP 6: Clear all preview actors
            if self.cut_vtk is not None and hasattr(self.cut_vtk, 'renderer'):
                renderer = self.cut_vtk.renderer
                cut_specific_actors = [
                    self.cut_preview_upper,
                    self.cut_preview_lower,
                ]
                for actor in cut_specific_actors:
                    if actor is not None:
                        try:
                            renderer.RemoveActor(actor)
                        except Exception:
                            pass
     
            # ✅ STEP 7: Reset state flags
            self._state = CutSectionState.IDLE
            self.cut_phase = 0
            self.center_point = None
            self.section_tangent = None
            self.is_cut_view_active = False
            self._cut_camera_state = None
            
            # ✅ STEP 8: Restore cross-section data (FIXED - method name)
            print("   🔄 Restoring cross-section...")
            self._restore_section_controller_state()  # ✅ FIXED: Use correct method name
            
            # ✅ STEP 9: Cleanup VTK widget (AFTER disabling render window)
            if self.cut_vtk is not None:
                try:
                    print("   🧹 Finalizing VTK widget...")
                    # Clear all actors
                    self.cut_vtk.clear()
                    # Close the widget
                    self.cut_vtk.close()
                    print("   ✅ VTK finalized")
                except Exception as e:
                    print(f"   ⚠️ VTK cleanup: {e}")
                finally:
                    self.cut_vtk = None
            
            # ✅ STEP 10: Cleanup dock widget
            if self.cut_dock is not None:
                try:
                    print("   🧹 Closing dock...")
                    # Disconnect signals
                    try:
                        self.cut_dock.visibilityChanged.disconnect()
                    except Exception:
                        pass
                    # Hide first
                    self.cut_dock.setVisible(False)
                    # Remove from main window
                    if hasattr(self.app, 'removeDockWidget'):
                        self.app.removeDockWidget(self.cut_dock)
                    # Close and delete
                    self.cut_dock.close()
                    self.cut_dock.deleteLater()
                    print("   ✅ Dock closed")
                except Exception as e:
                    print(f"   ⚠️ Dock cleanup: {e}")
                finally:
                    self.cut_dock = None
            
            # ✅ STEP 11: Clear UI references
            self.depth_label = None
            self.depth_spin = None
            self._restore_classification_tools()
            
            # ✅ STEP 12: Refresh cross-section views (with safety)
            #         # Only refresh if method exists
            #             self.app.section_controller.refresh_colors_direct()
            #         # Render only FIRST view (not all)
            #                     vtk_widget.render()
            # ✅ STEP 12: Refresh cross-section views (with safety)
            try:
                if hasattr(self.app, 'section_controller'):
                    print("   🔄 Refreshing cross-section...")
                    # Only refresh if method exists
                    #     self.app.section_controller.refresh_colors_direct()
                    
                    # ✅ FIXED: Refresh ALL views, not just first one
                    refreshed_count = 0
                    for view_index, vtk_widget in enumerate(getattr(self.app, 'section_vtks', {}).values()):
                        if vtk_widget is not None:
                            try:
                                _safe_vtk_render(vtk_widget)
                                refreshed_count += 1
                            except Exception as e:
                                print(f"   ⚠️ Failed to refresh view {view_index}: {e}")
                    
                    print(f"   ✅ Cross-section refreshed ({refreshed_count} views)")
            except Exception as e:
                print(f"   ⚠️ Refresh warning: {e}")
                        
            # ✅ STEP 13: Update status bar
            try:
                self.app.statusBar().showMessage("✂️ Cut Section closed - Cross-section restored", 1500)
            except Exception:
                pass
            
            print("✅ CUT SECTION CANCELED\n")
            
        except Exception as e:
            print(f"❌ [CutSection.cancel] Error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            # ✅ CRITICAL: Always reset state even if error
            self._state = CutSectionState.IDLE
            self._is_destroying = False
            try:
                self.app.set_cross_cursor_active(False)
            except Exception:
                pass

    def _detach_all_view_observers(self):
        """Guaranteed observer cleanup."""
        for v_idx, ids in list(self._view_observer_ids.items()):
            try:
                if v_idx in getattr(self.app, "section_vtks", {}):
                    iren = self.app.section_vtks[v_idx].interactor
                    for oid in ids:
                        try:
                            iren.RemoveObserver(oid)
                        except Exception:
                            pass
            except Exception:
                pass
        self._view_observer_ids.clear()

    def _restore_cross_section_pan_zoom_styles(self, view_indices=None, reinstall_camera_sync=True):
        """
        Rebind cross-section interactors to a fresh pan/zoom style after observer cleanup.
        Why: RemoveObservers("MouseMoveEvent") can detach style callbacks from the
        interactor; rebinding guarantees panning works immediately.
        """
        try:
            section_vtks = getattr(self.app, "section_vtks", {}) or {}
            if not section_vtks:
                return 0

            if view_indices is None:
                target_views = set(section_vtks.keys())
            else:
                target_views = {int(v) for v in view_indices if isinstance(v, (int, np.integer))}
                if not target_views:
                    target_views = set(section_vtks.keys())

            from vtkmodules.vtkInteractionStyle import vtkInteractorStyleImage

            # Ensure right-click observer tag storage exists
            if not hasattr(self.app, '_section_right_click_observers'):
                self.app._section_right_click_observers = {}

            # Resolve the right-click handler: prefer section_controller's method so
            # it carries the `self.app` reference correctly, fall back to a closure.
            sc = getattr(self.app, "section_controller", None)
            right_click_cb = getattr(sc, "_on_section_right_click", None) if sc else None

            ci_map = getattr(self.app, "classify_interactors", {})
            restored = 0
            for view_idx, vtk_widget in section_vtks.items():
                if view_idx not in target_views:
                    continue
                try:
                    if vtk_widget is None or not hasattr(vtk_widget, "interactor"):
                        continue
                    iren = vtk_widget.interactor
                    if iren is None:
                        continue

                    style = vtkInteractorStyleImage()
                    try:
                        style.SetInteractionModeToImageSlicing()
                    except Exception:
                        pass
                    iren.SetInteractorStyle(style)

                    # Discard stale ClassificationInteractor wrapper for this view.
                    old_ci = ci_map.pop(view_idx, None)
                    if old_ci is not None:
                        try:
                            old_ci.cleanup()
                        except Exception:
                            pass

                    # Reinstall right-click reactivation observer so subsequent
                    # right-clicks continue to work after this style replacement.
                    old_tag = self.app._section_right_click_observers.get(view_idx)
                    if old_tag is not None:
                        try:
                            iren.RemoveObserver(old_tag)
                        except Exception:
                            pass
                    if right_click_cb is not None:
                        tag = iren.AddObserver("RightButtonPressEvent", right_click_cb, 1.0)
                        self.app._section_right_click_observers[view_idx] = tag

                    restored += 1
                except Exception as e:
                    print(f"   ⚠️ Failed to restore pan/zoom style for View {view_idx + 1}: {e}")

            if reinstall_camera_sync and hasattr(self.app, "view_sync_map") and self.app.view_sync_map:
                try:
                    if hasattr(self.app, "_reset_camera_sync_observers"):
                        self.app._reset_camera_sync_observers()
                    elif hasattr(self.app, "_camera_observers"):
                        for _view_idx in list(self.app._camera_observers.keys()):
                            try:
                                self.app._remove_camera_sync_observer(_view_idx)
                            except Exception:
                                pass
                        self.app._camera_observers.clear()
                    for v_idx, vw in section_vtks.items():
                        is_synced = (
                            v_idx in self.app.view_sync_map or
                            any(src == v_idx for src in self.app.view_sync_map.values())
                        )
                        if is_synced and hasattr(self.app, "_install_realtime_camera_observer"):
                            self.app._install_realtime_camera_observer(v_idx, vw)
                except Exception as e:
                    print(f"   ⚠️ Camera sync re-install failed: {e}")

            if restored > 0:
                print(f"   ✅ Pan/zoom interactor rebound for {restored} cross-section view(s)")
            return restored
        except Exception as e:
            print(f"   ⚠️ Pan/zoom style restore failed: {e}")
            return 0


    #     """Robust actor cleanup from BOTH cross-section and cut section"""
    #     # Clear from cross-section
    #                     ren.RemoveActor(a)
    #     self.line_actor = self.buffer_actor_upper = self.buffer_actor_lower = None

    #     # Clear from ALL section views
    #                         ren.RemoveActor(a)
    #                 vtk_widget.render()

    #     # Clear from cut dock
    #                     ren_cut.RemoveActor(a)
    #     self.cut_preview_upper = self.cut_preview_lower = None

    def _clear_preview_actors(self):
        """
        Clear cut-section preview actors from the cut dock renderer.
        Also clears line_actor/buffer_actor from cross-section views if present
        as ghost leftovers from nested cuts.
        """
        try:
            # Save references before clearing
            _line = self.line_actor
            _upper = self.buffer_actor_upper
            _lower = self.buffer_actor_lower
            _cut_upper = self.cut_preview_upper
            _cut_lower = self.cut_preview_lower

            # ========== CLEAR CUT-DOCK PREVIEW ACTORS ==========
            if self.cut_vtk is not None and hasattr(self.cut_vtk, 'renderer') and self.cut_vtk.renderer:
                ren_cut = self.cut_vtk.renderer
                for actor in [_cut_upper, _cut_lower, _line, _upper, _lower]:
                    if actor is not None:
                        try:
                            ren_cut.RemoveActor(actor)
                        except Exception:
                            pass

            # ========== CLEAR CROSS-SECTION VIEW PREVIEW ACTORS ==========
            if hasattr(self.app, 'section_vtks'):
                for view_idx, vtk_widget in self.app.section_vtks.items():
                    try:
                        ren = vtk_widget.renderer
                        if ren is None:
                            continue
                        for actor in [_line, _upper, _lower]:
                            if actor is not None:
                                try:
                                    ren.RemoveActor(actor)
                                except Exception:
                                    pass
                    except Exception:
                        pass

            # Clear all references AFTER removing from all renderers
            self.cut_preview_upper = None
            self.cut_preview_lower = None
            self.line_actor = None
            self.buffer_actor_upper = None
            self.buffer_actor_lower = None
            
        except Exception:
            pass

    def _robust_clear_renderer(self, vtk_widget):
        """Ensure we really remove old props."""
        ren = vtk_widget.renderer
        try:
            actors = ren.GetActors()
            actors.InitTraversal()
            to_remove = []
            for _ in range(actors.GetNumberOfItems()):
                act = actors.GetNextActor()
                if act:
                    to_remove.append(act)
            for act in to_remove:
                ren.RemoveActor(act)
            ren.RemoveAllViewProps()
        except Exception:
            pass
        try:
            vtk_widget.clear()
        except Exception:
            pass

    def _mark_cut_preview_actor(self, actor, role: str) -> None:
        """Tag preview actors so stale VTK props can be purged later."""
        try:
            actor._naksha_cut_preview_actor = True
            actor._naksha_cut_preview_role = role
        except Exception:
            pass

    def _purge_stale_cut_preview_actors(self, include_cut_dock: bool = True) -> int:
        """Remove any tagged cut-preview actors still lingering in renderers."""
        removed = 0

        def _purge_renderer(renderer):
            nonlocal removed
            if renderer is None:
                return
            try:
                actors = renderer.GetActors()
                actors.InitTraversal()
                stale = []
                for _ in range(actors.GetNumberOfItems()):
                    actor = actors.GetNextActor()
                    if actor is not None and getattr(actor, "_naksha_cut_preview_actor", False):
                        stale.append(actor)
                for actor in stale:
                    try:
                        renderer.RemoveActor(actor)
                        removed += 1
                    except Exception:
                        pass
            except Exception:
                pass

        try:
            for vtk_widget in getattr(self.app, "section_vtks", {}).values():
                if vtk_widget is not None and hasattr(vtk_widget, "renderer"):
                    _purge_renderer(vtk_widget.renderer)
        except Exception:
            pass

        if include_cut_dock:
            try:
                if self.cut_vtk is not None and hasattr(self.cut_vtk, "renderer"):
                    _purge_renderer(self.cut_vtk.renderer)
            except Exception:
                pass

        return removed


    def _on_depth_spin_changed(self, value: float):
        """Update depth from spinbox (called during depth adjustment)."""
        self.dynamic_depth = float(value)
        if self.center_point is not None and self._state == CutSectionState.WAITING_DEPTH:
            self._mark_depth_adjustment_started()
            self._draw_dynamic_band_preview(self.center_point, self.dynamic_depth)
            if self.cut_vtk is not None:
                self._draw_cut_section_preview(self.center_point, self.dynamic_depth)


    def _get_vertical_segment(self, center, vtk_widget=None):
        """Get vertical segment bounds using VIEWPORT height, not data bounds."""
        if vtk_widget is None:
            vtk_widget = self.active_vtk or getattr(self.app, "sec_vtk", None)
        
        if vtk_widget is None:
            # Fallback: use large default
            zmin, zmax = center[2] - 100.0, center[2] + 100.0
        else:
            # ✅ MICROSTATION APPROACH: Use camera view bounds
            try:
                ren = vtk_widget.renderer
                cam = ren.GetActiveCamera()
                
                # Get parallel scale (viewport height in world units)
                parallel_scale = cam.GetParallelScale()
                
                # Line extends ±2× viewport height (ensures full coverage)
                height_extent = parallel_scale * 2.0
                
                zmin = center[2] - height_extent
                zmax = center[2] + height_extent
                
            except Exception:
                # Fallback: use data bounds
                xyz = getattr(self.app, "section_points", None)
                if xyz is not None and hasattr(xyz, 'shape') and xyz.shape[0] > 0:
                    try:
                        xyz = np.asarray(xyz, dtype=float)
                        zmin = float(np.min(xyz[:, 2])) - 10.0
                        zmax = float(np.max(xyz[:, 2])) + 10.0
                    except Exception:
                        zmin, zmax = center[2] - 100.0, center[2] + 100.0
                else:
                    zmin, zmax = center[2] - 100.0, center[2] + 100.0
        
        p0 = np.array([center[0], center[1], zmin], dtype=float)
        p1 = np.array([center[0], center[1], zmax], dtype=float)
        
        return p0, p1

    def _draw_dynamic_center_line(self, center):
        """Draw center line in cross-section with smooth updates."""

        if self._state not in [CutSectionState.WAITING_CENTER, CutSectionState.WAITING_DEPTH]:
            print(f"   ⚠️ Blocked _draw_dynamic_center_line: wrong state ({self._state})")
            return
    
        vtk_widget = self.active_vtk or getattr(self.app, "sec_vtk", None)
        if vtk_widget is None:
            return
        
        # ✅ FIX: Clear preview from ALL other cross-section views
        self._clear_preview_from_other_views(vtk_widget)
        
        ren = vtk_widget.renderer
        p0, p1 = self._get_vertical_segment(center, vtk_widget)
        
        # ✅ SMOOTH: Update existing, don't recreate
        if self.line_actor is None:
            # Create ONCE
            pts = vtk.vtkPoints()
            pts.InsertNextPoint(*p0)
            pts.InsertNextPoint(*p1)
            
            lines = vtk.vtkCellArray()
            lines.InsertNextCell(2)
            lines.InsertCellPoint(0)
            lines.InsertCellPoint(1)
            
            poly = vtk.vtkPolyData()
            poly.SetPoints(pts)
            poly.SetLines(lines)
            
            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputData(poly)
            
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(0, 1, 0)
            actor.GetProperty().SetLineWidth(1)
            actor.GetProperty().SetOpacity(1.0)
            self._mark_cut_preview_actor(actor, "section_center_line")
            
            # ✅ FORCE LINES ON TOP
            self._configure_line_on_top(actor)
            
            ren.AddActor(actor)
            self.line_actor = actor
            
            # Cache for fast updates
            self._line_actor_points = pts
            self._line_actor_poly = poly
        else:
            # ✅ FAST UPDATE (no recreation)
            pts = self._line_actor_points
            poly = self._line_actor_poly
            
            pts.SetPoint(0, *p0)
            pts.SetPoint(1, *p1)
            pts.Modified()
            poly.Modified()
            
            # ✅ CRITICAL: Ensure actor is in CURRENT view's renderer
            try:
                # Remove from all renderers first
                for view_idx, vw in self.app.section_vtks.items():
                    try:
                        vw.renderer.RemoveActor(self.line_actor)
                    except Exception:
                        pass
                
                # Add to current view only
                ren.AddActor(self.line_actor)
            except Exception:
                pass
        
        try:
            _safe_vtk_render(vtk_widget)
        except Exception:
            pass

    #     """Draw buffer band lines in cross-section."""

        
        
    #     # ✅ FIX: Clear preview from ALL other cross-section views
    #     self._clear_preview_from_other_views(vtk_widget)
        
    #     base_p0, base_p1 = self._get_vertical_segment(center, vtk_widget)
        
            
                
    #             poly.SetPoints(pts)
    #             poly.SetLines(lines)
    #             mapper.SetInputData(poly)
    #             actor.SetMapper(mapper)
                
    #             # ✅ MICROSTATION STYLE: Yellow, thin lines
    #             actor.GetProperty().SetColor(1.0, 1.0, 0.0)  # Yellow
    #             actor.GetProperty().SetLineWidth(1)  # ✅ THIN
    #             actor.GetProperty().SetOpacity(0.8)
                
    #             # ✅ FORCE LINES ON TOP
    #             self._configure_line_on_top(actor)

    #             ren.AddActor(actor)
                
                    
    #                 poly.SetPoints(pts)
    #                 poly.SetLines(lines)
    #                 mapper.SetInputData(poly)
    #                 actor.SetMapper(mapper)
                    
    #                 pts.Reset()
    #                 lines.Reset()
                
    #             # ✅ CRITICAL: Ensure actor is in CURRENT view's renderer
    #                 # Remove from all renderers first
    #                         vw.renderer.RemoveActor(actor)
                    
    #                 # Add to current view only
    #                 ren.AddActor(actor)
            
    #         p0[axis] += sign * depth
    #         p1[axis] += sign * depth
            
    #         pts.InsertNextPoint(*p0)
    #         pts.InsertNextPoint(*p1)
            
    #         lines.InsertNextCell(2)
    #         lines.InsertCellPoint(0)
    #         lines.InsertCellPoint(1)
            
    #         poly.Modified()
    #         mapper.Update()
    #         actor.VisibilityOn()
        
    #         vtk_widget.render()
    
    #     """Draw preview inside cut dock."""



    #     # ✅ FIX: ALWAYS use X-axis (0) for cut section preview lines
    #     # Camera ALWAYS looks along Y-axis, so only X-axis offset is visible
    #     # Using Y-axis makes lines invisible (they overlap with center line)


    #         # ✅ CRITICAL FIX: Pass cut_vtk widget explicitly for correct Z bounds
    #         base_p0, base_p1 = self._get_vertical_segment(center, self.cut_vtk)
            
    #         # Calculate offset points FIRST
    #         p0[axis] += sign * debug_depth
    #         p1[axis] += sign * debug_depth
            

    #             # ✅ CREATE NEW ACTOR ONCE

    #             # Insert initial points
    #             pts.InsertNextPoint(*p0)
    #             pts.InsertNextPoint(*p1)
                
    #             lines.InsertNextCell(2)
    #             lines.InsertCellPoint(0)
    #             lines.InsertCellPoint(1)
                
    #             poly.SetPoints(pts)
    #             poly.SetLines(lines)
    #             mapper.SetInputData(poly)
    #             actor.SetMapper(mapper)
                
    #             prop.SetColor(1.0, 1.0, 0.0)
    #             prop.SetLineWidth(2)
    #             prop.SetOpacity(0.8)
                
    #             # ✅ CONFIGURE DEPTH ONLY ONCE
    #             self._configure_line_on_top(actor)
                
    #             actor.VisibilityOn()
    #             ren.AddActor(actor)
                
    #             # ✅ UPDATE EXISTING POINTS (DON'T RESET)
                
    #             # ✅ JUST UPDATE THE 2 EXISTING POINTS
    #             pts.SetPoint(0, *p0)
    #             pts.SetPoint(1, *p1)
                
    #             # ✅ NOTIFY VTK
    #             pts.Modified()
    #             poly.Modified()
    #             mapper.Update()
            
    #         actor.VisibilityOn()

    #     # ✅ CRITICAL: Always render

    def _draw_dynamic_band_preview(self, center, depth):
        """Draw buffer band lines in cross-section with robust actor management."""

        if self._state != CutSectionState.WAITING_DEPTH:
            return
        
        vtk_widget = self.active_vtk or getattr(self.app, "sec_vtk", None)
        if vtk_widget is None:
            return
        
        # Clear preview from other views
        self._clear_preview_from_other_views(vtk_widget)
        
        ren = vtk_widget.renderer
        view_mode = getattr(self.app, "cross_view_mode", "side")
        view_mode = view_mode.strip().lower() if isinstance(view_mode, str) else "side"
        axis = 0 if view_mode == "side" else 1
        base_p0, base_p1 = self._get_vertical_segment(center, vtk_widget)
        
        for sign, attr in [(-1.0, "buffer_actor_lower"), (+1.0, "buffer_actor_upper")]:
            # Calculate offset points
            p0 = base_p0.copy()
            p1 = base_p1.copy()
            p0[axis] += sign * depth
            p1[axis] += sign * depth
            
            actor = getattr(self, attr, None)
            pts_attr = f"_{attr}_points"
            
            # ✅ FIX: Check if we have VALID cached geometry
            pts = getattr(self, pts_attr, None)
            needs_recreation = (
                actor is None or 
                pts is None or 
                not hasattr(self, f"_{attr}_poly") or
                getattr(self, f"_{attr}_poly", None) is None
            )
            
            if needs_recreation:
                # ✅ ALWAYS create fresh geometry
                pts = vtk.vtkPoints()
                pts.InsertNextPoint(*p0)
                pts.InsertNextPoint(*p1)
                
                lines = vtk.vtkCellArray()
                lines.InsertNextCell(2)
                lines.InsertCellPoint(0)
                lines.InsertCellPoint(1)
                
                poly = vtk.vtkPolyData()
                poly.SetPoints(pts)
                poly.SetLines(lines)
                
                mapper = vtk.vtkPolyDataMapper()
                mapper.SetInputData(poly)
                
                # Remove old actor if exists
                if actor is not None:
                    try:
                        ren.RemoveActor(actor)
                    except Exception:
                        pass
                
                actor = vtk.vtkActor()
                actor.SetMapper(mapper)
                self._mark_cut_preview_actor(actor, attr)
                
                # Yellow, visible lines
                prop = actor.GetProperty()
                prop.SetColor(1.0, 1.0, 0.0)  # Yellow
                prop.SetLineWidth(2)  # ✅ Thicker for visibility
                prop.SetOpacity(1.0)  # ✅ Full opacity
                
                # Force lines on top
                self._configure_line_on_top(actor)
                
                ren.AddActor(actor)
                
                # Cache references
                setattr(self, attr, actor)
                setattr(self, pts_attr, pts)
                setattr(self, f"_{attr}_lines", lines)
                setattr(self, f"_{attr}_poly", poly)
                setattr(self, f"_{attr}_mapper", mapper)
                
            else:
                # ✅ FIX: Update existing points WITHOUT Reset()
                pts = getattr(self, pts_attr)
                poly = getattr(self, f"_{attr}_poly")
                
                # Update the 2 existing points directly
                pts.SetPoint(0, *p0)
                pts.SetPoint(1, *p1)
                pts.Modified()
                poly.Modified()
                
                # Ensure actor is in current renderer
                try:
                    for view_idx, vw in self.app.section_vtks.items():
                        if vw != vtk_widget:
                            try:
                                vw.renderer.RemoveActor(actor)
                            except Exception:
                                pass
                    
                    # Check if actor is already in renderer
                    actors = ren.GetActors()
                    actors.InitTraversal()
                    found = False
                    for _ in range(actors.GetNumberOfItems()):
                        if actors.GetNextActor() == actor:
                            found = True
                            break
                    
                    if not found:
                        ren.AddActor(actor)
                except Exception:
                    pass
            
            # Ensure visibility
            actor.VisibilityOn()
        
        # ✅ CRITICAL: Force render
        try:
            _safe_vtk_render(vtk_widget)
        except Exception as e:
            print(f"⚠️ Render failed in _draw_dynamic_band_preview: {e}")

    def _draw_cut_section_preview(self, center, depth):
        """
        Draw Cut-in-Cut depth preview lines with transform-only drag updates.

        For nested cuts the two vertical line geometries are created once at
        the selected centre. During drag only vtkActor.SetPosition() changes,
        avoiding vtkPoints/vtkPolyData uploads every mouse event.

        Non-nested behavior is preserved.
        """
        if self._state != CutSectionState.WAITING_DEPTH:
            return
        if getattr(self, "_is_destroying", False):
            return
        if (
            self.cut_vtk is None
            or not hasattr(self.cut_vtk, "renderer")
            or self.cut_vtk.renderer is None
        ):
            return
        if center is None:
            return

        ren = self.cut_vtk.renderer
        preview_depth = (
            float(depth)
            if depth is not None and abs(float(depth)) > 1e-6
            else max(0.5, float(getattr(self, "dynamic_depth", 1.0)))
        )

        fast_nested = getattr(self, "_cut_source", None) == "cut"

        try:
            cam = ren.GetActiveCamera()
            cam_scale = float(cam.GetParallelScale()) if cam is not None else 0.0
        except Exception:
            cam_scale = 0.0

        base_signature = (
            round(float(center[0]), 9),
            round(float(center[1]), 9),
            round(float(center[2]), 9),
            round(cam_scale, 9),
        )
        base_changed = (
            getattr(self, "_cut_preview_base_signature", None)
            != base_signature
        )

        if base_changed or not fast_nested:
            base_p0, base_p1 = self._get_vertical_segment(
                center,
                self.cut_vtk,
            )
        else:
            base_p0 = None
            base_p1 = None

        for sign, attr in (
            (-1.0, "cut_preview_lower"),
            (+1.0, "cut_preview_upper"),
        ):
            actor = getattr(self, attr, None)
            pts_attr = f"_{attr}_points"
            pts = getattr(self, pts_attr, None)
            poly = getattr(self, f"_{attr}_poly", None)

            needs_recreation = (
                actor is None
                or pts is None
                or poly is None
            )

            if needs_recreation:
                if base_p0 is None or base_p1 is None:
                    base_p0, base_p1 = self._get_vertical_segment(
                        center,
                        self.cut_vtk,
                    )

                if fast_nested:
                    p0 = np.asarray(base_p0, dtype=float)
                    p1 = np.asarray(base_p1, dtype=float)
                else:
                    p0 = np.asarray(base_p0, dtype=float).copy()
                    p1 = np.asarray(base_p1, dtype=float).copy()
                    p0[0] += sign * preview_depth
                    p1[0] += sign * preview_depth

                pts = vtk.vtkPoints()
                pts.InsertNextPoint(*p0)
                pts.InsertNextPoint(*p1)

                lines = vtk.vtkCellArray()
                lines.InsertNextCell(2)
                lines.InsertCellPoint(0)
                lines.InsertCellPoint(1)

                poly = vtk.vtkPolyData()
                poly.SetPoints(pts)
                poly.SetLines(lines)

                mapper = vtk.vtkPolyDataMapper()
                mapper.SetInputData(poly)

                if actor is not None:
                    try:
                        ren.RemoveActor(actor)
                    except Exception:
                        pass

                actor = vtk.vtkActor()
                actor.SetMapper(mapper)
                self._mark_cut_preview_actor(actor, attr)

                prop = actor.GetProperty()
                prop.SetColor(1.0, 1.0, 0.0)
                prop.SetLineWidth(2)
                prop.SetOpacity(1.0)
                self._configure_line_on_top(actor)

                if fast_nested:
                    actor.SetPosition(
                        float(sign * preview_depth),
                        0.0,
                        0.0,
                    )

                ren.AddActor(actor)

                setattr(self, attr, actor)
                setattr(self, pts_attr, pts)
                setattr(self, f"_{attr}_lines", lines)
                setattr(self, f"_{attr}_poly", poly)
                setattr(self, f"_{attr}_mapper", mapper)

                print(
                    f"  🆕 Created {attr} at offset "
                    f"{sign * preview_depth:.2f}m"
                )

            elif fast_nested:
                if base_changed:
                    if base_p0 is None or base_p1 is None:
                        base_p0, base_p1 = self._get_vertical_segment(
                            center,
                            self.cut_vtk,
                        )
                    pts.SetPoint(0, *base_p0)
                    pts.SetPoint(1, *base_p1)
                    pts.Modified()
                    poly.Modified()

                # Normal drag frame: transforms only.
                actor.SetPosition(
                    float(sign * preview_depth),
                    0.0,
                    0.0,
                )

            else:
                # Historical non-nested geometry-update path.
                p0 = np.asarray(base_p0, dtype=float).copy()
                p1 = np.asarray(base_p1, dtype=float).copy()
                p0[0] += sign * preview_depth
                p1[0] += sign * preview_depth
                pts.SetPoint(0, *p0)
                pts.SetPoint(1, *p1)
                pts.Modified()
                poly.Modified()

            actor.VisibilityOn()

        self._cut_preview_base_signature = base_signature
        self._schedule_cut_preview_render()

    def _rebuild_cut_index_map(self):
        """
        Rebuild the mapping from cut section indices to original dataset indices.
        ✅ Handles both cross-section AND nested cuts correctly
        ✅ Chains indices through parent mappings if nested
        ✅ BUG #3 FIX: Bounds checking to prevent IndexError
        """
        if self.cut_points is None or self.cut_mask_in_section is None:
            self._cut_index_map = None
            return
        
        print(f"🔧 Rebuilding cut index map...")
        print(f"   Cut section: {len(self.cut_points)} points")
        print(f"   Cut mask: {np.sum(self.cut_mask_in_section)} True values")
        
        try:
            # ✅ CHECK 1: NESTED CUT (cut-from-cut)
            if hasattr(self, 'parent_index_map') and self.parent_index_map is not None:
                print("   Type: NESTED CUT (cut-from-cut)")
                
                local_indices = np.flatnonzero(self.cut_mask_in_section)
                
                # ✅ BUG #3 FIX: Validate indices are within parent_index_map bounds
                parent_size = len(self.parent_index_map)
                print(f"   Local indices: {len(local_indices)} values")
                print(f"   Parent map size: {parent_size}")
                
                if len(local_indices) == 0:
                    print(f"   ⚠️ No local indices after mask - empty cut")
                    self._cut_index_map = np.array([], dtype=np.int64)
                    return
                
                # ✅ CRITICAL: Check for out-of-bounds indices
                max_local_idx = np.max(local_indices)
                if max_local_idx >= parent_size:
                    print(f"   ❌ OUT OF BOUNDS: max local index {max_local_idx} >= parent size {parent_size}")
                    print(f"   Filtering invalid indices...")
                    
                    # Filter to keep only valid indices
                    valid_mask = local_indices < parent_size
                    local_indices = local_indices[valid_mask]
                    
                    print(f"   ✅ Filtered to {len(local_indices)} valid indices")
                    
                    if len(local_indices) == 0:
                        print(f"   ❌ No valid indices remain after filtering")
                        self._cut_index_map = None
                        return
                
                # ✅ Safe indexing (indices are now validated)
                original_indices = self.parent_index_map[local_indices]
                self._cut_index_map = original_indices
                
                print(f"   ✅ Chained {len(self._cut_index_map)} indices through parent")
                
                if len(self._cut_index_map) > 0:
                    print(f"      Index range: {np.min(self._cut_index_map)} to {np.max(self._cut_index_map)}")
                
                # ✅ BUG #3 FIX: Verify against dataset bounds
                if hasattr(self.app, 'data') and 'xyz' in self.app.data:
                    dataset_size = len(self.app.data['xyz'])
                    max_cut_idx = np.max(self._cut_index_map) if len(self._cut_index_map) > 0 else -1
                    
                    if max_cut_idx >= dataset_size:
                        print(f"   ❌ CRITICAL: Cut indices [{np.min(self._cut_index_map)}-{max_cut_idx}] exceed dataset size {dataset_size}")
                        print(f"   Clamping to valid range...")
                        
                        # Clamp to valid dataset range
                        valid_in_dataset = self._cut_index_map < dataset_size
                        self._cut_index_map = self._cut_index_map[valid_in_dataset]
                        
                        print(f"   ✅ Clamped to {len(self._cut_index_map)} valid indices")
            
            # ✅ CHECK 2: REGULAR CROSS-SECTION CUT
            else:
                print("   Type: CROSS-SECTION CUT")
                
                # Get the cross-section rectangle selection mask
                if hasattr(self.app.section_controller, 'last_mask'):
                    cross_section_mask = self.app.section_controller.last_mask
                    cross_section_indices = np.flatnonzero(cross_section_mask)
                    
                    print(f"   Rectangle selection: {len(cross_section_indices)} indices")
                    
                    # ✅ CRITICAL: Check if we have the cut mask
                    if hasattr(self, 'cut_mask_in_section') and self.cut_mask_in_section is not None:
                        cut_local_mask = self.cut_mask_in_section
                        
                        # ✅ Verify mask size matches cross-section indices
                        if len(cut_local_mask) != len(cross_section_indices):
                            print(f"   ⚠️ Size mismatch: mask {len(cut_local_mask)} vs indices {len(cross_section_indices)}")
                            print(f"   Attempting to rebuild from raw data...")
                            
                            # Fallback: Rebuild mask from cut_points
                            all_xyz = getattr(self.app, "data", {}).get("xyz", None)
                            if all_xyz is None:
                                print(f"   ❌ Cannot fallback - no dataset available")
                                self._cut_index_map = None
                                return
                            
                            selected_xyz = all_xyz[cross_section_indices]
                            active_view = getattr(self.app.section_controller, "active_view", None)
                            axis = 0 if getattr(self.app, "cross_view_mode", "side") == "side" else 1
                            cval = float(self.center_point[axis])
                            
                            new_mask = np.abs(selected_xyz[:, axis] - cval) <= self.dynamic_depth
                            local_indices = np.flatnonzero(new_mask)
                            
                            # ✅ BUG #3 FIX: Bounds check before indexing
                            if len(local_indices) > 0 and np.max(local_indices) >= len(cross_section_indices):
                                print(f"   ❌ Fallback indices out of bounds, clamping...")
                                local_indices = local_indices[local_indices < len(cross_section_indices)]
                            
                            self._cut_index_map = cross_section_indices[local_indices]
                            
                            print(f"   ✅ Rebuilt and mapped {len(self._cut_index_map)} cut points")
                        else:
                            # Sizes match - use the mask directly
                            local_indices = np.flatnonzero(cut_local_mask)
                            
                            # ✅ BUG #3 FIX: Bounds check before indexing
                            if len(local_indices) > 0:
                                max_local_idx = np.max(local_indices)
                                if max_local_idx >= len(cross_section_indices):
                                    print(f"   ⚠️ Local indices exceed cross_section_indices bounds")
                                    print(f"      Max local: {max_local_idx}, Cross-section size: {len(cross_section_indices)}")
                                    
                                    # Filter invalid indices
                                    valid_mask = local_indices < len(cross_section_indices)
                                    local_indices = local_indices[valid_mask]
                                    print(f"   ✅ Filtered to {len(local_indices)} valid indices")
                            
                            if len(local_indices) > 0:
                                self._cut_index_map = cross_section_indices[local_indices]
                            else:
                                self._cut_index_map = np.array([], dtype=np.int64)
                            
                            print(f"   ✅ Mapped {len(self._cut_index_map)} cut points to original dataset")
                        
                        if len(self._cut_index_map) > 0:
                            print(f"      Index range: {np.min(self._cut_index_map)} to {np.max(self._cut_index_map)}")
                        
                        # ✅ Verify count
                        if len(self._cut_index_map) != len(self.cut_points):
                            print(f"   ⚠️ Warning: Index map {len(self._cut_index_map)} ≠ cut points {len(self.cut_points)}")
                    else:
                        print(f"   ❌ No cut_mask_in_section found - cannot rebuild map")
                        self._cut_index_map = None
                else:
                    print(f"   ❌ No rectangle selection (last_mask) found")
                    self._cut_index_map = None
            
            # ✅ FINAL VALIDATION
            if self._cut_index_map is None or len(self._cut_index_map) == 0:
                print(f"   ⚠️ Index map is empty or invalid!")
                self._cut_index_map = None
            else:
                print(f"   ✅ Index map ready: {len(self._cut_index_map)} indices")
        
        except Exception as e:
            print(f"   ❌ Error rebuilding index map: {e}")
            import traceback
            traceback.print_exc()
            self._cut_index_map = None



    def _clear_cut_view(self):
        """Clear cut view and restore section data."""
        av = getattr(self.app.section_controller, "active_view", None)
        if av is not None and av in self.app.section_vtks:
            vw = self.app.section_vtks[av]
            self._robust_clear_renderer(vw)
            _safe_vtk_render(vw)
        self.is_cut_view_active = False
        self.restore_section_data()

    def _clear_cut_dock_points(self, preserve_source_data: bool = False):
        """Empty the cut dock while preserving the dock widget and palette state."""
        try:
            if not preserve_source_data:
                self.cut_points = None
                self._cut_index_map = None
                self.parent_cut_points = None
                self.parent_cut_index_map = None
                self.cut_history = []
                self.cut_level = 0
                self.section_tangent = None
                self.original_section_tangent = None
                self.accumulated_rotation = 0
                self.is_cut_view_active = False

            self.cut_core_actor = None
            self.cut_buffer_actor = None

            if self.cut_vtk is not None and hasattr(self.cut_vtk, "renderer") and self.cut_vtk.renderer:
                try:
                    self.cut_vtk.renderer.RemoveAllViewProps()
                except Exception:
                    pass
                _safe_vtk_render(self.cut_vtk)
        except Exception as e:
            print(f"⚠️ Failed to clear cut dock points: {e}")


    def _estimate_section_tangent_3d(self, points_xyz: np.ndarray) -> np.ndarray:
        """
        Estimate tangent direction.
        ✅ BUG #12 FIX: Safe division with proper epsilon checking
        """
        if points_xyz is None or points_xyz.shape[0] < 2:
            return np.array([1.0, 0.0, 0.0])
        
        try:
            xy = np.asarray(points_xyz[:, :2], dtype=float)
            xy_mean = np.mean(xy, axis=0)
            xy_centered = xy - xy_mean
            cov = np.dot(xy_centered.T, xy_centered) / max(len(xy) - 1, 1)
            eigenvalues, eigenvectors = np.linalg.eigh(cov)
            tangent_2d = eigenvectors[:, np.argmax(eigenvalues)]
            
            # ✅ BUG #12 FIX: Use stricter epsilon and check BEFORE division
            norm = np.linalg.norm(tangent_2d)
            
            # Use 1e-6 instead of 1e-9 (safer threshold)
            if norm < 1e-6:
                print(f"⚠️ Tangent norm too small ({norm:.2e}), using fallback")
                return np.array([1.0, 0.0, 0.0])
            
            # ✅ Safe division (norm is guaranteed >= 1e-6)
            tangent_2d = tangent_2d / norm
            
            # ✅ Verify result is valid (additional safety check)
            if not np.all(np.isfinite(tangent_2d)):
                print(f"⚠️ Tangent contains NaN/Inf, using fallback")
                return np.array([1.0, 0.0, 0.0])
            
            tangent_3d = np.array([tangent_2d[0], tangent_2d[1], 0.0])
            return tangent_3d
            
        except Exception as e:
            print(f"⚠️ Tangent calculation failed: {e}, using fallback")
            return np.array([1.0, 0.0, 0.0])



    def _set_camera_along_tangent(self, vtk_widget, cut_points_xyz, tangent_3d):
        """Set camera to look ALONG the section tangent direction."""
        ren = vtk_widget.renderer
        cam = ren.GetActiveCamera()

        if cut_points_xyz is None or len(cut_points_xyz) == 0:
            return

        pts = np.asarray(cut_points_xyz, float)
        xmin, ymin, zmin = np.min(pts, axis=0)
        xmax, ymax, zmax = np.max(pts, axis=0)
        xmid, ymid, zmid = (xmin + xmax)/2.0, (ymin + ymax)/2.0, (zmin + zmax)/2.0
        focal_point = np.array([xmid, ymid, zmid])
        
        tangent = np.asarray(tangent_3d, float)
        tangent_norm = np.linalg.norm(tangent)
        if tangent_norm < 1e-9:
            tangent = np.array([1.0, 0.0, 0.0])
        else:
            tangent = tangent / tangent_norm
        
        extent = np.linalg.norm([xmax - xmin, ymax - ymin, zmax - zmin])
        distance = max(10.0, extent * 2.0)
        camera_position = focal_point - tangent * distance
        view_up = np.array([0.0, 0.0, 1.0])
        
        cam.SetPosition(*camera_position)
        cam.SetFocalPoint(*focal_point)
        cam.SetViewUp(*view_up)
        cam.ParallelProjectionOn()
        
        z_extent = max(zmax - zmin, 0.5)
        perp_xy = np.array([-tangent[1], tangent[0], 0.0])
        perp_norm = np.linalg.norm(perp_xy)
        if perp_norm > 1e-9:
            perp_xy = perp_xy / perp_norm
            proj_perp = np.dot(pts[:, :2], perp_xy[:2])
            perp_extent = max(np.max(proj_perp) - np.min(proj_perp), 0.5)
        else:
            perp_extent = max(xmax - xmin, ymax - ymin, 0.5)
        
        scale = max(z_extent, perp_extent) * 0.6
        cam.SetParallelScale(scale)
        ren.ResetCameraClippingRange()
        
        if abs(self.cut_yaw_deg) > 1e-6:
            cam.Azimuth(self.cut_yaw_deg)
            ren.ResetCameraClippingRange()

    def fit_cut_section_view(self):
        """
        Fit the dedicated cut section widget to its current visible points.
        Intended for Shift+F when the cut section view has focus.
        """
        if not getattr(self, "is_cut_view_active", False):
            print("   ⚠️ Cut section fit skipped - cut view not active")
            return False

        if self.cut_vtk is None:
            print("   ⚠️ Cut section fit skipped - no cut widget")
            return False

        points = getattr(self, "cut_points", None)
        if points is None or len(points) == 0:
            print("   ⚠️ Cut section fit skipped - no cut points")
            return False

        try:
            print("\n" + "=" * 60)
            print("🧲 FITTING CUT SECTION VIEW")
            print("=" * 60)

            if self.section_tangent is not None:
                self._set_camera_along_tangent(self.cut_vtk, points, self.section_tangent)
            else:
                ren = self.cut_vtk.renderer
                ren.ResetCamera()
                ren.GetActiveCamera().ParallelProjectionOn()
                ren.ResetCameraClippingRange()

            _safe_vtk_render(self.cut_vtk)
            print(f"   ✅ Cut section fitted to {len(points):,} visible points")
            print("=" * 60 + "\n")
            return True
        except Exception as e:
            print(f"⚠️ Cut section fit failed: {e}")
            import traceback
            traceback.print_exc()
            return False


    def _get_cut_slot_palette(self, ensure_seed: bool = True):
        """
        Resolve cut-section palette (slot 5) without leaking slot-0 visibility.
        Priority:
        1) existing self.cut_palette
        2) app slot-5 palette via AppWindow helper
        3) isolated seed from main colors/weights (show=True)
        """
        palette = getattr(self, "cut_palette", None)

        if isinstance(palette, dict) and palette:
            # Keep colors fresh with main palette edits; keep cut-specific show/weight.
            master = getattr(self.app, "class_palette", {}) or {}
            if master:
                for code, info in palette.items():
                    if code in master and isinstance(master.get(code), dict) and "color" in master[code]:
                        info["color"] = tuple(master[code]["color"])
            return palette

        slot_palette = None
        if hasattr(self.app, "_get_cross_section_palette"):
            try:
                slot_palette = self.app._get_cross_section_palette(
                    5,
                    allow_default_seed=ensure_seed,
                    persist_seed=False,
                )
            except Exception:
                slot_palette = None

        if isinstance(slot_palette, dict) and slot_palette:
            resolved = {}
            for code, info in slot_palette.items():
                if not isinstance(info, dict):
                    continue
                try:
                    code_i = int(code)
                except Exception:
                    continue
                resolved[code_i] = dict(info)
            if resolved:
                self.cut_palette = resolved
                return resolved

        if not ensure_seed:
            return {}

        master = getattr(self.app, "class_palette", {}) or {}
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
        if seeded:
            self.cut_palette = seeded
        return seeded

    def _make_colors(self, pts, classes):
        """Generate RGB colors from classifications."""
        palette = self._get_cut_slot_palette(ensure_seed=True)
        colors = np.zeros((pts.shape[0], 3), dtype=np.uint8)

        for code in np.unique(classes):
            entry = palette.get(int(code), {"color": (200, 200, 200), "show": True})
            if entry.get("show", True):
                colors[classes == code] = entry["color"]
            else:
                colors[classes == code] = (0, 0, 0)  # hidden

        return colors


    def get_cut_section_classification_data(self):
        """
        Returns cut section points and their original dataset indices.
        
        Returns:
            tuple: (cut_points, original_indices) or (None, None) if not available
        """
        if self.cut_points is None or self._cut_index_map is None:
            return None, None
        
        # Verify index map is valid
        if len(self._cut_index_map) != len(self.cut_points):
            print(f"⚠️ Index map size mismatch - rebuilding...")
            self._rebuild_cut_index_map()
        
        # ✅ Verify indices are within bounds
        max_idx = len(self.app.data['xyz']) if hasattr(self.app, 'data') else 0
        if max_idx > 0 and np.max(self._cut_index_map) >= max_idx:
            print(f"❌ ERROR: Cut index map contains out-of-bounds indices!")
            print(f"   Max index in map: {np.max(self._cut_index_map)}")
            print(f"   Dataset size: {max_idx}")
            return None, None
        
        return self.cut_points, self._cut_index_map

    def _do_cut_section_locate(self, vtk_interactor, display_x, display_y):
        """MicroStation-style locate for the Cut View, mirroring
        SectionController._do_section_locate for cross-section views 1-4:
        click inside the Cut View while the cross-section tool is active ->
        pan Main View's camera to that world XY.

        The Cut View's own local coordinate frame (side/front axis swap,
        accumulated rotation -- see _finalize_dynamic_cut_section) makes an
        exact inverse-transform fragile to re-derive here. Instead, resolve
        the click the same way point_sync_tool.py's cut-view click handler
        already does reliably: nearest-neighbor against the Cut View's own
        rendered points (self.cut_points, local space) to get a local
        index, then self._cut_index_map[local_index] for the ORIGINAL
        dataset global index, then app.data['xyz'][global_index] for the
        true world position -- independent of whatever local transform was
        used to build the Cut View.
        """
        if self.cut_points is None or self._cut_index_map is None or len(self.cut_points) == 0:
            return

        try:
            ren = vtk_interactor.GetRenderWindow().GetRenderers().GetFirstRenderer()
            picker = vtk.vtkPointPicker()
            picker.SetTolerance(0.01)
            if not picker.Pick(display_x, display_y, 0, ren):
                return
            local_pt = np.asarray(picker.GetPickPosition(), dtype=np.float64)
        except Exception as e:
            print(f"⚠️ Cut section locate: pick failed: {e}")
            return

        distances = np.linalg.norm(self.cut_points - local_pt, axis=1)
        local_index = int(np.argmin(distances))
        if distances[local_index] > 2.0:
            return
        if local_index >= len(self._cut_index_map):
            return

        global_index = int(self._cut_index_map[local_index])
        xyz = getattr(self.app, "data", {}).get("xyz") if hasattr(self.app, "data") else None
        if xyz is None or global_index < 0 or global_index >= len(xyz):
            return

        world_x, world_y = float(xyz[global_index, 0]), float(xyz[global_index, 1])

        # Arm cross_interactor so main-view preview line starts from this
        # point, same as cross-section's own locate.
        cross = getattr(self.app, "cross_interactor", None)
        main_vtk = getattr(self.app, "vtk_widget", None)
        if cross is not None and main_vtk is not None:
            focal_z = main_vtk.renderer.GetActiveCamera().GetFocalPoint()[2]
            cross.P1 = np.array([world_x, world_y, focal_z], dtype=np.float64)
            cross.slice_state = 1

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

        if hasattr(self.app, "statusBar"):
            self.app.statusBar().showMessage(
                f"Locked ({world_x:.1f}, {world_y:.1f})  —  "
                "move to main view and draw cross-section",
                5000,
            )
        print(f"📍 Cut section locate: ({world_x:.2f}, {world_y:.2f})")

        # Mirrors SectionController._do_section_locate's own bookkeeping:
        # stores the click's DISPLAY position + which view it came from, so
        # a MouseMoveEvent handler can draw a rubber-band line inside THIS
        # view (not just pan Main View), matching cross-section's own
        # _draw_locate_rubber_band behavior. "cut" is a sentinel distinct
        # from the integer view indices section_vtks uses.
        self.app._section_locate_display = (float(display_x), float(display_y))
        self.app._section_locate_view = "cut"

    def _draw_cut_locate_rubber_band(self, cursor_x, cursor_y):
        """Draw a rubber-band line inside the Cut View from the locked
        locate point to the current cursor position, mirroring
        SectionController._draw_locate_rubber_band for cross-section views.
        """
        if self.cut_vtk is None:
            return
        ren = self.cut_vtk.renderer
        locate_disp = getattr(self.app, "_section_locate_display", None)
        if locate_disp is None:
            return

        if not hasattr(self, "_cut_locate_rb_actor") or self._cut_locate_rb_actor is None:
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
            self._cut_locate_rb_pts = pts
            self._cut_locate_rb_poly = poly
            self._cut_locate_rb_actor = actor

        actor = self._cut_locate_rb_actor
        if not ren.HasViewProp(actor):
            ren.AddActor2D(actor)
        actor.VisibilityOn()

        color = getattr(self.app, "cross_line_color", (1.0, 0.0, 1.0))
        width = getattr(self.app, "cross_line_width", 2)
        prop = actor.GetProperty()
        prop.SetColor(*color)
        prop.SetLineWidth(max(2, width))
        prop.SetOpacity(1.0)

        self._cut_locate_rb_pts.SetPoint(0, locate_disp[0], locate_disp[1], 0.0)
        self._cut_locate_rb_pts.SetPoint(1, float(cursor_x), float(cursor_y), 0.0)
        self._cut_locate_rb_pts.Modified()
        self._cut_locate_rb_poly.Modified()
        self.cut_vtk.render()

    def clear_cut_locate_rubber_band(self):
        """Remove the Cut View's own locate rubber-band actor, if present.
        Called from SectionController.clear_locate_state so a click that
        started in the Cut View gets cleaned up the same way a click
        started in a regular cross-section view does.
        """
        actor = getattr(self, "_cut_locate_rb_actor", None)
        if actor is None:
            return
        try:
            if self.cut_vtk is not None and self.cut_vtk.renderer.HasViewProp(actor):
                self.cut_vtk.renderer.RemoveActor2D(actor)
                self.cut_vtk.render()
        except Exception:
            pass
        self._cut_locate_rb_actor = None

    def _on_cut_view_left_click_locate(self, obj, event):
        if not getattr(self.app, "cross_section_active", False):
            return
        if not getattr(self.app, "section_locate_enabled", True):
            return
        x, y = obj.GetEventPosition()
        self._do_cut_section_locate(obj, x, y)

    def _on_cut_view_mouse_move_locate(self, obj, event):
        if not getattr(self.app, "section_locate_enabled", True):
            return
        if getattr(self.app, "_section_locate_view", None) != "cut":
            return
        x, y = obj.GetEventPosition()
        self._draw_cut_locate_rubber_band(x, y)

    def onclassificationchanged(self, changedoriginalindices=None):
        """🚀 MICROSTATION-STYLE REFRESH: Signal handler for classification changes."""
        if self._is_refreshing or not self.is_cut_view_active or self.cut_vtk is None:
            return
        self._is_refreshing = True
        try:
            print(f"🔪 CUT SECTION SYNC: Classification changed...")

            # Refresh cut section view IN-PLACE
            print("Refreshing cut section view preserving state")
            self._refresh_cut_colors_fast()
            print("Cut section view updated")

            # Optional main-view partial/full refresh (existing logic)
            if changedoriginalindices is not None and len(changedoriginalindices) > 0:
                print("Updating main view", len(changedoriginalindices), "changed points")
                if hasattr(self.app, "updatemainviewpartial"):
                    self.app.updatemainviewpartial(changedoriginalindices)
                elif hasattr(self.app, "refreshmainviewcolors"):
                    self.app.refreshmainviewcolors(changedoriginalindices)
                else:
                    if hasattr(self.app, "refreshdisplay"):
                        print("Using full refresh")
                        self.app.refreshdisplay()
                        print("Main view updated")

            if hasattr(self.app, "statusBar"):
                numchanged = len(changedoriginalindices) if changedoriginalindices is not None else 0
                self.app.statusBar().showMessage(
                    f"Classified {numchanged} points in cut section", 2000
                )
        except Exception as e:
            print("Classification refresh error", e)
            import traceback
            traceback.print_exc()
        finally:
            self._is_refreshing = False

    def _build_cut_unified_actor(self, points, classes, point_size=3):
        """Build cut-section actor with the same shader contract as section views."""
        if points is None or len(points) == 0 or self.cut_vtk is None:
            return None

        try:
            from vtkmodules.util import numpy_support
            from gui.unified_actor_manager import (
                ViewShaderContext,
                _BASE_POINT_SIZE,
                _attach_view_shader_context,
                _build_vtk_actor_from_arrays,
                _compute_boundary_flags,
                _ensure_opengl_polydata_mapper,
                _get_lut,
                INTERACTION_ACTOR_SWITCHING_ENABLED,
                _push_uniforms_direct,
                _uniform_pick_indices,
                _wire_actor_metadata,
            )

            actor_name = "_cut_section_unified"
            interaction_actor_name = "_cut_section_interaction_lod"
            palette = self._get_cut_slot_palette(ensure_seed=True)
            
            border_pct = float(getattr(self.app, "view_borders", {}).get(5, 0.0))
            actual_pt_size = max(1.0, float(point_size or _BASE_POINT_SIZE))

            try:
                if hasattr(self.cut_vtk, "actors") and actor_name in self.cut_vtk.actors:
                    self.cut_vtk.remove_actor(actor_name, render=False)
                if hasattr(self.cut_vtk, "actors") and interaction_actor_name in self.cut_vtk.actors:
                    self.cut_vtk.remove_actor(interaction_actor_name, render=False)
            except Exception:
                pass
            self.cut_vtk._naksha_full_detail_actor = None
            self.cut_vtk._naksha_interaction_actor = None
            self.cut_vtk._naksha_interaction_lod_active = False

            cloud = pv.PolyData(points)

            cls_i32 = np.asarray(classes, dtype=np.int32)
            cls_f32 = cls_i32.astype(np.float32, copy=False)
            class_vtk = numpy_support.numpy_to_vtk(cls_f32, deep=True)
            class_vtk.SetName("Classification")
            cloud.GetPointData().AddArray(class_vtk)

            boundary = _compute_boundary_flags(points, cls_i32) if border_pct > 0.0 else np.zeros(len(points), dtype=np.float32)
            boundary_vtk = numpy_support.numpy_to_vtk(boundary, deep=True)
            boundary_vtk.SetName("BoundaryFlag")
            cloud.GetPointData().AddArray(boundary_vtk)

            rgb_buffer = _get_lut("cut_section").map_classes(cls_i32, palette)
            rgb_vtk = numpy_support.numpy_to_vtk(rgb_buffer, deep=True)
            rgb_vtk.SetName("RGB")
            cloud.GetPointData().SetScalars(rgb_vtk)

            actor = self.cut_vtk.add_points(
                cloud,
                scalars="RGB",
                rgb=True,
                point_size=actual_pt_size,
                render_points_as_spheres=False,
                name=actor_name,
                reset_camera=False,
                render=False,
            )
            if actor is None:
                return None

            actor.GetProperty().LightingOff()
            _ensure_opengl_polydata_mapper(actor, cloud)

            mapper = actor.GetMapper()
            mesh = mapper.GetInput() if mapper is not None else None
            if mesh is None:
                self.cut_core_actor = actor
                self.cut_buffer_actor = None
                return actor

            vtk_ca = mesh.GetPointData().GetScalars()
            if vtk_ca is None:
                self.cut_core_actor = actor
                self.cut_buffer_actor = None
                setattr(self.cut_vtk, "_cut_section_unified_actor", actor)
                return actor
            vtk_rgb = numpy_support.vtk_to_numpy(vtk_ca)
            np.copyto(vtk_rgb, rgb_buffer)
            vtk_ca.Modified()

            class_vtk_arr = mesh.GetPointData().GetArray("Classification")
            vtk_cls = numpy_support.vtk_to_numpy(class_vtk_arr) if class_vtk_arr is not None else cls_f32.copy()

            try:
                actor._naksha_render_window = self.cut_vtk.render_window
                actor._naksha_renderer = self.cut_vtk.renderer
            except Exception:
                pass

            actor._naksha_rgb_ptr = vtk_rgb
            actor._naksha_vtk_array = vtk_ca
            actor._naksha_vtk_rgb_ref = vtk_ca
            actor._naksha_mesh = mesh
            actor._naksha_section_class = vtk_cls
            actor._naksha_base_point_size = actual_pt_size
            actor._naksha_boundary_vtk = boundary_vtk

            # Standard VTK points expose one global point size and cannot show
            # the source section's per-class weights.  The current unified
            # shader no longer contains the camera-uniform path that caused the
            # earlier cut-context compile failure, so use it for cut rendering.
            cut_ctx = ViewShaderContext(slot_idx=5)
            cut_ctx.load_from_palette(palette, border_pct, actual_pt_size)
            _attach_view_shader_context(actor, cut_ctx, actor_name)
            _push_uniforms_direct(actor, cut_ctx)

            self.cut_vtk._naksha_full_detail_actor = actor
            if INTERACTION_ACTOR_SWITCHING_ENABLED and len(points) > 750_000:
                try:
                    lod_sel = _uniform_pick_indices(len(points), 750_000)
                    lod_arrays = {
                        "pts": np.ascontiguousarray(np.asarray(points)[lod_sel]),
                        "cls_f32": np.ascontiguousarray(cls_f32[lod_sel], dtype=np.float32),
                        "bf": np.ascontiguousarray(boundary[lod_sel], dtype=np.float32),
                        "rgb": np.ascontiguousarray(rgb_buffer[lod_sel], dtype=np.uint8),
                    }
                    lod_result = _build_vtk_actor_from_arrays(
                        self.cut_vtk,
                        interaction_actor_name,
                        lod_arrays,
                        actual_pt_size,
                        palette,
                        border_pct,
                        5,
                        _attach_view_shader_context,
                        _ensure_opengl_polydata_mapper,
                        ViewShaderContext,
                    )
                    (
                        lod_actor,
                        lod_ctx,
                        lod_mesh,
                        lod_vtk_ca,
                        lod_vtk_rgb,
                        lod_vtk_cls,
                        lod_class_vtk,
                        lod_bf_vtk,
                    ) = lod_result
                    if lod_actor is not None:
                        _wire_actor_metadata(
                            lod_actor,
                            lod_mesh,
                            lod_vtk_ca,
                            lod_vtk_rgb,
                            lod_vtk_cls,
                            lod_class_vtk,
                            lod_bf_vtk,
                            None,
                            actual_pt_size,
                        )
                        lod_actor._naksha_lod_source_indices = lod_sel
                        lod_actor._naksha_points_np_ref = lod_arrays["pts"]
                        lod_actor.SetVisibility(0)
                        _push_uniforms_direct(lod_actor, lod_ctx)
                        self.cut_vtk._naksha_interaction_actor = lod_actor
                except Exception as lod_error:
                    print(f"Cut interaction actor skipped: {lod_error}")

            self.cut_core_actor = actor
            self.cut_buffer_actor = None
            setattr(self.cut_vtk, "_cut_section_unified_actor", actor)
            return actor

        except Exception as e:
            print(f"Failed to build unified cut actor: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _refresh_cut_colors_fast(self):
        """Refresh cut-section colors and shader classification state in-place."""
        if not self.is_cut_view_active or self.cut_points is None or self._cut_index_map is None:
            return

        if self.cut_vtk is None or not hasattr(self.app, 'data'):
            return

        try:
            from vtkmodules.util import numpy_support
            classification = self.app.data.get("classification") if isinstance(self.app.data, dict) else None
            if classification is None:
                return

            index_map = np.asarray(self._cut_index_map, dtype=np.int64).reshape(-1)
            if index_map.size == 0:
                return

            # Guard against stale indices after dataset switches.
            n_points = int(len(classification))
            valid_mask = (index_map >= 0) & (index_map < n_points)
            if not np.all(valid_mask):
                invalid = int(np.size(valid_mask) - np.count_nonzero(valid_mask))
                print(f"⚠️ Cut index-map contains {invalid} stale indices for current dataset ({n_points:,} pts) — pruning")

                index_map = index_map[valid_mask]
                self._cut_index_map = index_map
                print(
                    f"[RUNTIME-CHECK] cut-map-prune invalid={invalid} "
                    f"kept={int(index_map.size)} dataset={n_points}"
                )

                # Keep cut_points and index_map aligned when possible.
                if self.cut_points is not None and len(self.cut_points) == len(valid_mask):
                    self.cut_points = self.cut_points[valid_mask]

                if index_map.size == 0:
                    print("⚠️ Cut section became invalid after file switch — deactivating stale cut state")
                    self.cut_points = None
                    self.is_cut_view_active = False
                    print("[RUNTIME-CHECK] cut-map-prune result=deactivated")
                    return

            palette = self._get_cut_slot_palette(ensure_seed=True)

            target_actor = getattr(self, 'cut_core_actor', None)
            if target_actor is None and hasattr(self.cut_vtk, "actors"):
                target_actor = self.cut_vtk.actors.get("_cut_section_unified")

            if target_actor and hasattr(target_actor, 'GetMapper'):
                mapper = target_actor.GetMapper()
                polydata = mapper.GetInput() if mapper is not None else None
                if polydata:
                    vtk_colors = polydata.GetPointData().GetScalars()
                    if vtk_colors and vtk_colors.GetNumberOfTuples() == len(index_map):
                        vtk_ptr = numpy_support.vtk_to_numpy(vtk_colors)
                        class_vtk_arr = polydata.GetPointData().GetArray("Classification")
                        cls_ptr = numpy_support.vtk_to_numpy(class_vtk_arr) if class_vtk_arr is not None else None

                        changed_local = None
                        changed_classes = None
                        changed_global = getattr(self.app, "_last_changed_indices", None)
                        if isinstance(changed_global, np.ndarray) and changed_global.size > 0:
                            try:
                                g_to_l = getattr(target_actor, "_naksha_cut_global_to_local_arr", None)
                                g_to_l_ref = getattr(target_actor, "_naksha_cut_global_to_local_ref", None)
                                if (
                                    g_to_l is None
                                    or len(g_to_l) != n_points
                                    or g_to_l_ref is not self._cut_index_map
                                ):
                                    g_to_l = np.full(n_points, -1, dtype=np.int32)
                                    g_to_l[index_map] = np.arange(index_map.size, dtype=np.int32)
                                    target_actor._naksha_cut_global_to_local_arr = g_to_l
                                    target_actor._naksha_cut_global_to_local_ref = self._cut_index_map

                                valid_changed = changed_global[
                                    (changed_global >= 0) & (changed_global < len(g_to_l))
                                ]
                                if valid_changed.size > 0:
                                    mapped = g_to_l[valid_changed]
                                    mapped = mapped[mapped >= 0]
                                    if mapped.size > 0:
                                        changed_local = np.unique(mapped)
                                        changed_classes = classification[index_map[changed_local]].astype(np.int32, copy=False)
                            except Exception:
                                changed_local = None
                                changed_classes = None

                        if changed_local is not None and changed_classes is not None and changed_local.size > 0:
                            max_class_val = int(changed_classes.max()) if changed_classes.size > 0 else 0
                        else:
                            classes = classification[index_map].astype(np.int32, copy=False)
                            max_class_val = int(classes.max()) if classes.size > 0 else 0

                        palette_max = max((int(k) for k in palette.keys()), default=0) if palette else 0
                        lut_size = max(max_class_val, palette_max) + 1
                        lut = np.zeros((lut_size, 3), dtype=np.uint8)
                        for code, info in palette.items():
                            key = int(code)
                            lut[key] = info.get('color', (128, 128, 128)) if info.get('show', True) else (0, 0, 0)

                        if changed_local is not None and changed_classes is not None and changed_local.size > 0:
                            vtk_ptr[changed_local] = safe_lut_indexing(lut, changed_classes)
                            if cls_ptr is not None:
                                cls_ptr[changed_local] = changed_classes.astype(cls_ptr.dtype, copy=False)
                        else:
                            np.copyto(vtk_ptr, safe_lut_indexing(lut, classes))
                            if cls_ptr is not None:
                                np.copyto(cls_ptr, classes.astype(cls_ptr.dtype, copy=False))

                        if class_vtk_arr is not None:
                            class_vtk_arr.Modified()
                            target_actor._naksha_section_class = cls_ptr

                        vtk_colors.Modified()
                        try:
                            from gui.unified_actor_manager import _mark_actor_dirty
                            _mark_actor_dirty(target_actor)
                        except Exception:
                            pass
                        # Push weight_lut uniform so class weights are applied.
                        # Without this the GPU shader uses the stale weight_lut
                        try:
                            from gui.unified_actor_manager import _push_uniforms_direct
                            ctx = getattr(target_actor, '_naksha_shader_ctx', None)
                            if ctx is not None:
                                _bdr = float(getattr(self.app, 'view_borders', {}).get(5, 0.0))
                                _bsz = float(getattr(self.app, 'point_size', 2.5))
                                palette_sig = tuple(
                                    sorted(
                                        (
                                            int(code),
                                            bool(info.get("show", True)),
                                            tuple(info.get("color", (128, 128, 128))),
                                            round(float(info.get("weight", 1.0)), 6),
                                        )
                                        for code, info in palette.items()
                                    )
                                )
                                uniform_sig = (palette_sig, round(_bdr, 6), round(_bsz, 6))
                                if uniform_sig != getattr(target_actor, "_naksha_cut_uniform_sig", None):
                                    ctx.force_reload()
                                    ctx.load_from_palette(palette, _bdr, _bsz)
                                    _push_uniforms_direct(target_actor, ctx)
                                    target_actor._last_uniform_gen = ctx._generation
                                    target_actor._naksha_base_point_size = _bsz
                                    target_actor._naksha_cut_uniform_sig = uniform_sig
                        except Exception:
                            pass
                        _safe_vtk_render(self.cut_vtk)
                        return

            classes = classification[index_map].astype(int, copy=False)
            self._build_cut_unified_actor(self.cut_points, classes, point_size=3)
            _safe_vtk_render(self.cut_vtk)

        except Exception as e:
            print(f"Cut color refresh failed: {e}")
            import traceback
            traceback.print_exc()

    def _ensure_cut_section_dock(self):
            """Create dedicated dock window for cut section with INLINE depth control."""
            if self.cut_dock is not None and self.cut_vtk is not None:
                return

            # Cleanup existing dock/VTK
            if self.cut_vtk is not None:
                self._remove_cut_right_click_observer()
                try:
                    self._finalize_cut_render_window_once(self.cut_vtk)
                    self.cut_vtk.close()
                except Exception:
                    pass
                self.cut_vtk = None
            
            if self.cut_dock is not None:
                try:
                    self.cut_dock.close()
                    self.cut_dock.deleteLater()
                except Exception:
                    pass
                self.cut_dock = None

            # Create new dock
            self.cut_dock = QDockWidget("✂️ Cut Section", self.app)
            self.cut_dock.setWindowFlags(Qt.Window | Qt.WindowMinimizeButtonHint | Qt.WindowCloseButtonHint)
            self.cut_dock.setFeatures(QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetFloatable)
            self.cut_dock.setAllowedAreas(Qt.NoDockWidgetArea)
            
            container = QWidget()
            layout = QVBoxLayout(container)
            layout.setContentsMargins(8, 8, 8, 8)
            layout.setSpacing(6)

            # ── CRITICAL: Flush pending Qt/OpenGL events before creating a new  ──
            # VTK render window.  After AI inference (CUDA ops) or heavy rendering,
            # unflushed events can leave the OpenGL driver in a state where
            # wglChoosePixelFormat fails on Windows → null render window → crash.
            from PySide6.QtWidgets import QApplication
            QApplication.processEvents()

            # Create DEDICATED VTK widget for cut section
            self.cut_vtk = QtInteractor(container)
            if hasattr(self.app, "_setup_interactor_swapper"):
                self.app._setup_interactor_swapper(
                    self.cut_vtk.interactor,
                    preserve_physical_middle_pan=True,
                )
            self._mark_cut_vtk_active()

            # ── CRITICAL: Verify the render window was actually initialised ────
            # QtInteractor.__init__ silently continues even when the underlying
            # vtkWin32OpenGLRenderWindow fails to get a valid pixel format.
            # 0x0000...0000 memory access violation (null-pointer deref in VTK).
            try:
                _rw = self.cut_vtk.GetRenderWindow()
                if _rw is None:
                    raise RuntimeError("VTK render window is None after QtInteractor init")
                # Force the window to initialise its OpenGL context
                _rw.Initialize()
            except Exception as _vtk_init_err:
                print(f"❌ Cut section VTK widget failed to initialise: {_vtk_init_err}")
                print("   This usually means the GPU driver cannot create a new OpenGL")
                print("   context right now (e.g. after heavy AI/CUDA usage).")
                print("   → Try again in a moment, or restart the application.")
                # Clean up the broken widget so we don't crash later
                try:
                    self.cut_vtk.close()
                except Exception:
                    pass
                self.cut_vtk = None
                # Tear down the half-built dock
                if self.cut_dock is not None:
                    try:
                        self.cut_dock.deleteLater()
                    except Exception:
                        pass
                    self.cut_dock = None
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.critical(
                    self.app,
                    "Cut Section Error",
                    "Could not create the cut section view.\n\n"
                    "The GPU driver returned an error when opening a new 3-D window.\n\n"
                    "Possible causes:\n"
                    "  • GPU memory exhausted after AI classification\n"
                    "  • Too many render windows open\n\n"
                    "Please try again in a moment.\n"
                    "If the problem persists, save your work and restart the application."
                )
                return
            # ─────────────────────────────────────────────────────────────────────
            self.cut_vtk.set_background("black")
            layout.addWidget(self.cut_vtk.interactor)
            self._cut_right_click_observer_id = self.cut_vtk.interactor.AddObserver(
                "RightButtonPressEvent",
                self._on_cut_view_right_click_reactivate,
                1.0,
            )
            # MicroStation-style locate (same feature as cross-section views
            # 1-4): click inside the Cut View while the cross-section tool
            # is active -> pan Main View to that location. Priority 1.0
            # matches SectionController's own left-click locate observer.
            self._cut_locate_observer_id = self.cut_vtk.interactor.AddObserver(
                "LeftButtonPressEvent",
                self._on_cut_view_left_click_locate,
                1.0,
            )
            # Rubber-band preview line inside the Cut View itself while the
            # locate point is locked, mirroring the cross-section views'
            # own mouse-move rubber-band.
            self._cut_locate_move_observer_id = self.cut_vtk.interactor.AddObserver(
                "MouseMoveEvent",
                self._on_cut_view_mouse_move_locate,
                1.0,
            )
            if hasattr(self.app, "_register_canvas_cursor_widget"):
                self.app._register_canvas_cursor_widget(self.cut_vtk.interactor)
            if hasattr(self.app, "_install_section_wheel_zoom"):
                self.app._install_section_wheel_zoom(self.cut_vtk)
            if hasattr(self.app, "point_sync_tool") and self.app.point_sync_tool.active:
                self.app.point_sync_tool.activate_for_cut_view(self.cut_vtk)

            # Modern bottom bar with inline depth control
            btn_layout = QHBoxLayout()

            # Depth label
            self.depth_label = QLabel("Depth (m):")
            self.depth_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            btn_layout.addWidget(self.depth_label)

            # Depth spinbox

            self.depth_spin = QDoubleSpinBox()
            self.depth_spin.setDecimals(2)

            # ✅ CRITICAL: Calculate reasonable max depth based on data bounds
            max_depth = 100.0  # Default fallback
            try:
                if hasattr(self.app, 'data') and 'xyz' in self.app.data:
                    xyz = self.app.data['xyz']
                    # Max depth should be ~10% of dataset extent (prevents freezing)
                    x_extent = float(xyz[:, 0].max() - xyz[:, 0].min())
                    y_extent = float(xyz[:, 1].max() - xyz[:, 1].min())
                    z_extent = float(xyz[:, 2].max() - xyz[:, 2].min())
                    
                    max_extent = max(x_extent, y_extent, z_extent)
                    max_depth = min(max_extent * 0.1, 100.0)  # Cap at 100m
                    
                    print(f"📏 Dataset extent: X={x_extent:.1f}m, Y={y_extent:.1f}m, Z={z_extent:.1f}m")
                    print(f"   Max cut depth set to: {max_depth:.1f}m (10% of max extent)")
                else:
                    print(f"⚠️ No dataset available, using default max depth: {max_depth}m")
            except Exception as e:
                print(f"⚠️ Could not calculate max depth: {e}, using {max_depth}m")

            # ✅ Set validated range (min: 0.01m, max: calculated or 100m)
            self.depth_spin.setRange(0.01, max_depth)
            self.depth_spin.setSingleStep(0.10)
            self.depth_spin.setValue(getattr(self, 'dynamic_depth', 1.0))
            self.depth_spin.setMinimumWidth(90)
            self.depth_spin.setFixedHeight(28)
            self.depth_spin.setAlignment(Qt.AlignRight)
            self.depth_spin.setButtonSymbols(QAbstractSpinBox.NoButtons)

            # ✅ BUG #6 FIX: Add tooltip showing valid range
            self.depth_spin.setToolTip(
                f"Cut section depth (±meters from center line)\n"
                f"Valid range: 0.01m - {max_depth:.1f}m\n"
                f"Adjust with mouse or type value"
            )

            btn_layout.addWidget(self.depth_spin)
            self.depth_spin.valueChanged.connect(self._on_depth_spin_changed)

            btn_layout.addStretch()


            # Reset View button
            reset_view_btn = QPushButton("Reset View")
            reset_view_btn.setToolTip("Reset camera to correct orthogonal view along tangent")
            reset_view_btn.clicked.connect(self._reset_cut_view_camera)
            btn_layout.addWidget(reset_view_btn)

            layout.addLayout(btn_layout)
            self.cut_dock.setWidget(container)

            # ✅ SAFE CLOSE EVENT HANDLER (INSIDE METHOD - self exists)
            def safe_close_event(event):
                """
                Safely close cut section dock by cleaning up FIRST.
                ✅ BUG #13 FIX: Only accept close if cleanup succeeds
                """
                cleanup_succeeded = False
                
                try:
                    if getattr(self.app, "_shutdown_in_progress", False) or getattr(self, "_force_close_requested", False):
                        self._force_close_requested = False
                        cleanup_succeeded = True
                        event.accept()
                        return

                    print("🚪 User clicked X on cut section dock - cleaning up...")
                    
                    # Save geometry BEFORE closing
                    from PySide6.QtCore import QSettings
                    settings = QSettings("NakshaAI", "LidarApp")
                    if self.cut_dock is not None:
                        try:
                            settings.setValue("CutSectionDock_geometry", self.cut_dock.saveGeometry())
                            print("   💾 Cut section dock geometry saved")
                        except Exception as e:
                            print(f"   ⚠️ Geometry save failed: {e}")
                    
                    # Check if forced close from clear_project
                    force_close = getattr(event, '_force_close', False)
                    
                    if force_close:
                        print("   ✅ Forced close from clear_project - accepting")
                        self._force_close_requested = False
                        cleanup_succeeded = True
                        event.accept()
                    else:
                        print("   🧹 Normal user close - full cleanup...")
                        
                        # ✅ BUG #13 FIX: Try cleanup and track success
                        try:
                            self.cancel_cut_section()
                            cleanup_succeeded = True
                            print("   ✅ Cleanup completed successfully")
                        except Exception as cleanup_error:
                            print(f"   ❌ Cleanup failed: {cleanup_error}")
                            import traceback
                            traceback.print_exc()
                            cleanup_succeeded = False
                        
                        # ✅ BUG #13 FIX: Only accept if cleanup succeeded
                        if cleanup_succeeded:
                            event.accept()
                            print("   ✅ Cut section closed safely")
                        else:
                            event.ignore()
                            print("   ⚠️ Close cancelled - cleanup failed, dock remains open")
                            
                            # Show error to user
                            from PySide6.QtWidgets import QMessageBox
                            QMessageBox.critical(
                                self.cut_dock,
                                "Close Failed",
                                "Failed to close cut section properly.\n"
                                "Check console for errors.\n\n"
                                "Try again or restart application."
                            )
                
                except Exception as e:
                    print(f"   ❌ Close event error: {e}")
                    import traceback
                    traceback.print_exc()
                    
                    # ✅ BUG #13 FIX: On unexpected error, ignore close (safer than accept)
                    if not cleanup_succeeded:
                        print("   ⚠️ Close cancelled due to unexpected error")
                        event.ignore()
                        
                        # Show error to user
                        try:
                            from PySide6.QtWidgets import QMessageBox
                            QMessageBox.critical(
                                self.cut_dock if self.cut_dock else self.app,
                                "Critical Error",
                                f"Unexpected error during close:\n{str(e)}\n\n"
                                "Dock will remain open. Please restart application."
                            )
                        except Exception:
                            pass
                    else:
                        # Cleanup succeeded but post-processing failed
                        event.accept()


            # ✅ ASSIGN HANDLER
            self.cut_dock.closeEvent = safe_close_event

            # ✅ RESTORE GEOMETRY
            from PySide6.QtCore import QSettings
            settings = QSettings("NakshaAI", "LidarApp")
            saved_geometry = settings.value("CutSectionDock_geometry")

            if saved_geometry is not None:
                self.cut_dock.restoreGeometry(saved_geometry)
                print("✅ Restored cut section dock geometry")
            else:
                self.cut_dock.move(self.app.x() + 100, self.app.y() + 150)
                self.cut_dock.resize(600, 500)
                print("→ Default position for cut dock")

            # ✅ SHOW DOCK
            self.cut_dock.show()

    def _plot_cut_to_dedicated_widget(self, points):
        """
        Plot cut section to the DEDICATED cut section widget.
        ✅ FIXED: Uses cut-specific palette via _make_colors()
        """
        if points is None or points.shape[0] == 0:
            return
        
        # Get classifications
        classes = self.app.data.get("classification", None)
        
        if self._cut_index_map is None or len(self._cut_index_map) != len(points):
            self._rebuild_cut_index_map()
        
        # ✅ Build colors using cut-specific palette
        if classes is not None and self._cut_index_map is not None:
            cut_classes = classes[self._cut_index_map]
            colors = self._make_colors(points, cut_classes)  # ← Uses self.cut_palette!
            print(f"   🎨 Colors built using {'cut palette' if hasattr(self, 'cut_palette') and self.cut_palette else 'global palette'}")
        else:
            colors = np.full((points.shape[0], 3), 200, dtype=np.uint8)
        
        # Clear the dedicated widget
        try:
            ren = self.cut_vtk.renderer
            ren.RemoveAllViewProps()
        except Exception:
            pass
        
        base_size = 3
        actor = None
        if classes is not None and self._cut_index_map is not None:
            actor = self._build_cut_unified_actor(points, cut_classes, point_size=base_size)

        if actor is None:
            cloud = pv.PolyData(points)
            cloud["RGB"] = colors
            self.cut_core_actor = self.cut_vtk.add_points(
                cloud,
                scalars="RGB",
                rgb=True,
                point_size=base_size,
                render_points_as_spheres=False,
                name="_cut_section_unified",
                reset_camera=False,
                render=False,
            )
            self.cut_buffer_actor = None
        
        # Set camera along tangent
        if self.section_tangent is not None:
            self._set_camera_along_tangent(self.cut_vtk, points, self.section_tangent)
        else:
            ren = self.cut_vtk.renderer
            ren.ResetCamera()
            ren.GetActiveCamera().ParallelProjectionOn()
            ren.ResetCameraClippingRange()
        _safe_vtk_render(self.cut_vtk)
        
        print(f"✅ Cut plotted to dedicated widget: {points.shape[0]} pts")


    def _debug_coordinate_spaces(self):
        """Debug helper to verify coordinate transformations."""
        print("\n" + "="*60)
        print("🔍 COORDINATE SPACE DEBUG")
        print("="*60)
        
        # Check if we have cross-section data
        if hasattr(self.app, 'section_points'):
            sec_pts = self.app.section_points
            print(f"section_points (TRANSFORMED): {len(sec_pts) if sec_pts is not None else 0}")
            if sec_pts is not None and len(sec_pts) > 0:
                print(f"   X: [{sec_pts[:, 0].min():.2f}, {sec_pts[:, 0].max():.2f}]")
                print(f"   Y: [{sec_pts[:, 1].min():.2f}, {sec_pts[:, 1].max():.2f}]")
                print(f"   Z: [{sec_pts[:, 2].min():.2f}, {sec_pts[:, 2].max():.2f}]")
        
        # Check original world coordinates
        if hasattr(self.app, 'data') and 'xyz' in self.app.data:
            all_xyz = self.app.data['xyz']
            if hasattr(self.app.section_controller, 'last_mask'):
                mask = self.app.section_controller.last_mask
                selected = all_xyz[mask]
                print(f"\nOriginal world coords (SELECTED): {len(selected)}")
                print(f"   X: [{selected[:, 0].min():.2f}, {selected[:, 0].max():.2f}]")
                print(f"   Y: [{selected[:, 1].min():.2f}, {selected[:, 1].max():.2f}]")
                print(f"   Z: [{selected[:, 2].min():.2f}, {selected[:, 2].max():.2f}]")
        
        # Check cut points
        if self.cut_points is not None and len(self.cut_points) > 0:
            print(f"\ncut_points: {len(self.cut_points)}")
            print(f"   X: [{self.cut_points[:, 0].min():.2f}, {self.cut_points[:, 0].max():.2f}]")
            print(f"   Y: [{self.cut_points[:, 1].min():.2f}, {self.cut_points[:, 1].max():.2f}]")
            print(f"   Z: [{self.cut_points[:, 2].min():.2f}, {self.cut_points[:, 2].max():.2f}]")
        
        print("="*60 + "\n")
             
    def sync_palette_from_source_view(self):
        """
        ✅ FIXED: Properly sync palette while preserving EXACT visibility state.
        Prevents "all classes visible" bug after classification in cut section.
        """
        try:
            # Determine source view index
            source_view_idx = None
           
            if self.is_cut_view_active and hasattr(self, 'parent_cut_points'):
                # Nested cut - inherit from previous cut section
                source_view_idx = 5  # Cut Section View slot
                print(f"📋 Nested cut: inheriting palette from previous cut section")
            else:
                # First cut - inherit from active cross-section view
                if hasattr(self.app, 'section_controller'):
                    source_view = getattr(
                        self,
                        '_cut_source_view_index',
                        getattr(self.app.section_controller, 'active_view', 0),
                    )
                    source_view_idx = int(source_view) + 1  # slots 1-4
                    print(f"📋 First cut: inheriting palette from Cross-Section View {int(source_view) + 1}")
           
            if source_view_idx is None:
                print("⚠️ No source view to inherit palette from")
                return False
           
            # ═══════════════════════════════════════════════════════════════
            # ✅ CRITICAL FIX: Get palette from DISPLAY DIALOG FIRST
            # This is the authoritative source for visibility states
            # ═══════════════════════════════════════════════════════════════
            source_palette = None
           
            # Priority 1: Display Mode Dialog (most reliable)
            if hasattr(self.app, 'display_mode_dialog') and self.app.display_mode_dialog:
                dialog = self.app.display_mode_dialog
                if hasattr(dialog, 'view_palettes') and source_view_idx in dialog.view_palettes:
                    source_palette = dialog.view_palettes[source_view_idx]
                    print(f"   ✅ Found palette in dialog.view_palettes[{source_view_idx}] (authoritative)")
           
            # Priority 2: App view_palettes (fallback)
            if not source_palette:
                if hasattr(self.app, 'view_palettes') and source_view_idx in self.app.view_palettes:
                    source_palette = self.app.view_palettes[source_view_idx]
                    print(f"   ✅ Found palette in app.view_palettes[{source_view_idx}] (fallback)")
           
            if not source_palette:
                print(f"⚠️ No palette found for source view {source_view_idx}")
                return False
           
            # ═══════════════════════════════════════════════════════════════
            # ✅ CRITICAL FIX: Deep copy with STRICT validation
            # ═══════════════════════════════════════════════════════════════
            self.cut_palette = {}
            visible_count = 0
            hidden_count = 0
           
            print(f"\n   🔍 DEBUG: Syncing palette from source view {source_view_idx}...")
 
            for code, info in source_palette.items():
                code_int = int(code)
               
                # ✅ CRITICAL: Strict validation of 'show' field
                if 'show' not in info:
                    print(f"      ⚠️ Class {code_int}: Missing 'show' field - defaulting to True")
                    is_visible = True
                else:
                    is_visible = info['show']
                   
                    # ✅ EXTRA VALIDATION: Ensure it's actually a boolean
                    if not isinstance(is_visible, bool):
                        print(f"      ⚠️ Class {code_int}: 'show' is {type(is_visible)}, converting...")
                        is_visible = bool(is_visible)
               
                # ✅ Create new palette entry with validated visibility
                self.cut_palette[code_int] = {
                    'show': is_visible,  # ← STRICTLY validated boolean
                    'description': str(info.get('description', '')),
                    'color': tuple(info.get('color', (128, 128, 128))),
                    'weight': float(info.get('weight', 1.0))
                }
               
                # Track stats
                if is_visible:
                    visible_count += 1
                else:
                    hidden_count += 1
               
                # ✅ DEBUG: Log first 5 classes
                if len(self.cut_palette) <= 5:
                    print(f"      Class {code_int}: show={is_visible} ({'visible' if is_visible else 'HIDDEN'})")
 
            print(f"   ✅ Synced palette: {visible_count} visible, {hidden_count} hidden classes")
           
            # ═══════════════════════════════════════════════════════════════
            # ✅ VERIFICATION: Check if result makes sense
            # ═══════════════════════════════════════════════════════════════
            if hidden_count == 0 and len(self.cut_palette) > 5:
                print(f"   ⚠️ WARNING: All {len(self.cut_palette)} classes are visible - this might be wrong!")
                print(f"   🔍 Source palette had these visibility states:")
                for code, info in list(source_palette.items())[:5]:
                    print(f"      Class {code}: show={info.get('show', 'MISSING')}")
           
            # ✅ CRITICAL: Also update app.view_palettes[5] for persistence
            if hasattr(self.app, 'view_palettes'):
                self.app.view_palettes[5] = dict(self.cut_palette)
           
            # ✅ Update display dialog if open
            if hasattr(self.app, 'display_mode_dialog') and self.app.display_mode_dialog:
                dialog = self.app.display_mode_dialog
                if hasattr(dialog, 'view_palettes'):
                    dialog.view_palettes[5] = dict(self.cut_palette)
           
            return True
           
        except Exception as e:
            print(f"❌ Palette sync failed: {e}")
            import traceback
            traceback.print_exc()
            return False

    def sync_palette_to_display_dialog(self):
        """
        ✅ NEW: Sync cut section palette TO Display Mode dialog.
        Called when cut section palette is updated via Display Mode.
        
        Flow: Cut Section View → Display Mode Dialog
        """
        try:
            if not hasattr(self, 'cut_palette') or not self.cut_palette:
                return False
            
            dialog = None
            if hasattr(self.app, 'display_mode_dialog') and self.app.display_mode_dialog:
                dialog = self.app.display_mode_dialog
            
            if not dialog:
                print("⚠️ Display Mode dialog not open")
                return False
            
            # Update dialog's view_palettes[5]
            if not hasattr(dialog, 'view_palettes'):
                dialog.view_palettes = {}
            
            dialog.view_palettes[5] = {}
            for code, info in self.cut_palette.items():
                dialog.view_palettes[5][int(code)] = {
                    'show': bool(info.get('show', True)),
                    'description': str(info.get('description', '')),
                    'color': tuple(info.get('color', (128, 128, 128))),
                    'weight': float(info.get('weight', 1.0))
                }
            
            print(f"   ✅ Synced {len(self.cut_palette)} classes to Display Mode dialog")
            
            # ✅ If dialog is showing Cut Section View, refresh table
            if hasattr(dialog, 'current_slot') and dialog.current_slot == 5:
                if hasattr(dialog, '_load_slot_checkboxes'):
                    dialog._load_slot_checkboxes(5)
                    print(f"   🔄 Refreshed Cut Section View checkboxes in dialog")
            
            return True
            
        except Exception as e:
            print(f"❌ Dialog sync failed: {e}")
            import traceback
            traceback.print_exc()
            return False


    def on_display_mode_apply(self, payload):
        """
        ✅ NEW: Handle Display Mode apply for cut section view.
        Called when user clicks Apply button in Display Mode dialog.
        
        Args:
            payload: Dictionary from DisplayModeDialog.on_apply()
        """
        try:
            # Only handle if targeting Cut Section View (slot 5)
            if payload.get('target_view') != 5 and payload.get('slot') != 5:
                return
            
            print(f"\n{'='*60}")
            print(f"🎨 DISPLAY MODE APPLIED TO CUT SECTION VIEW")
            print(f"{'='*60}")
            
            # Extract new palette
            new_palette = payload.get('classes', {})
            if not new_palette:
                print("⚠️ No classes in payload")
                return
            
            # Update cut_palette
            self.cut_palette = {}
            visible_count = 0
            
            for code, info in new_palette.items():
                code = int(code)
                is_visible = info.get('show', True)
                
                self.cut_palette[code] = {
                    'show': bool(is_visible),
                    'description': str(info.get('description', '')),
                    'color': tuple(info.get('color', (128, 128, 128))),
                    'weight': float(info.get('weight', 1.0))
                }
                
                if is_visible:
                    visible_count += 1
            
            print(f"   ✅ Updated cut palette: {len(self.cut_palette)} classes ({visible_count} visible)")
            
            # ✅ Update border value
            border_percent = payload.get('border_percent', 0)
            if hasattr(self.app, 'view_borders'):
                self.app.view_borders[5] = border_percent
                print(f"   ✅ Updated border: {border_percent}%")
            
            # ✅ Refresh cut section view with new palette
            if self.is_cut_view_active and self.cut_vtk is not None:
                self._refresh_cut_colors_fast()
                print(f"   🔄 Cut section view refreshed")
            
            print(f"{'='*60}\n")
            
        except Exception as e:
            print(f"❌ Display mode apply failed: {e}")
            import traceback
            traceback.print_exc()
            
    def sync_colors_from_master(self):
        """
        ✅ FIX: Sync color values from app.class_palette into cut_palette.
        Called before every color refresh so that PTC class color changes
        (orange instead of pink, etc.) are reflected in the cut section view
        WITHOUT touching visibility or weight — those stay per-cut.
        """
        master = getattr(self.app, 'class_palette', {})
        if not master or not hasattr(self, 'cut_palette') or not self.cut_palette:
            return
        for code, info in self.cut_palette.items():
            if code in master and 'color' in master[code]:
                info['color'] = tuple(master[code]['color'])
        # ✅ Also invalidate the UAM palette resolve cache for slot 5 so that
        # _get_slot_palette returns the updated color on the next call.
        try:
            from gui.unified_actor_manager import invalidate_palette_cache
            invalidate_palette_cache(5)
        except Exception:
            pass

    def refresh_colors_direct(self):
            """
            Refresh colors of cut section visualization based on current classification.
            Called after points are classified in the cut section view.
            
            ✅ Delegates to the proven working method: _refresh_cut_colors_fast()
            ✅ That method handles all the complex VTK buffer updates correctly.
            ✅ FIX: sync_colors_from_master() is called first so PTC color changes
            (e.g. class changed to orange) are reflected immediately.
            ✅ PATCH: Clears _gpu_sync_done so the optimizer doesn't suppress this
            refresh when called from the slow path after a cut-section classification.
            """
            try:
                print("   🎨 Updating cut section colors...")

                # ✅ PATCH: Clear optimizer skip flag so this refresh is never suppressed.
                # The fast path sets _gpu_sync_done=True for cross-section GPU injection,
                # but that must NOT prevent an explicit cut-section color update.
                if hasattr(self.app, '_gpu_sync_done'):
                    self.app._gpu_sync_done = False

                # ✅ FIX: Pull latest colors from class_palette into cut_palette before rendering.
                self.sync_colors_from_master()

                # ✅ Call the proven, working method
                self._refresh_cut_colors_fast()
                
                print("   ✅ Cut section colors updated")
                
            except Exception as e:
                print(f"   ❌ Error refreshing cut section colors: {e}")
                import traceback
                traceback.print_exc()

    def _compute_colors_for_indices(self, classifications):
        """
        Compute RGB colors for cut section points based on their classification.
        
        Args:
            classifications: numpy array of classification codes
            
        Returns:
            numpy array of shape (N, 3) with RGB values [0-255]
        """
        try:
            n_points = len(classifications)
            colors = np.zeros((n_points, 3), dtype=np.uint8)
            
            palette = self._get_cut_slot_palette(ensure_seed=True)
            if not palette:
                # Default: gray for all
                colors[:] = [128, 128, 128]
                return colors
            
            for i, class_code in enumerate(classifications):
                if class_code in palette:
                    class_entry = palette[class_code]
                    
                    # Extract color from palette
                    if 'color' in class_entry:
                        rgb = class_entry['color']
                        
                        # Handle different color formats
                        if isinstance(rgb, (list, tuple)):
                            # Could be [0-1] or [0-255]
                            if max(rgb) <= 1.0:
                                colors[i] = np.array(rgb[:3]) * 255
                            else:
                                colors[i] = rgb[:3]
                        else:
                            colors[i] = [128, 128, 128]
                    else:
                        colors[i] = [128, 128, 128]
                else:
                    # Unknown class: light gray
                    colors[i] = [200, 200, 200]
            
            return colors
            
        except Exception as e:
            print(f"   ⚠️ Error computing colors: {e}")
            return None

    def _apply_colors_to_visualization(self, colors):
        """
        Apply RGB colors to the cut section visualization.
        
        Args:
            colors: numpy array of shape (N, 3) with RGB values [0-255]
        """
        try:
            # Update the dataset colors in cut_vtk
            if not hasattr(self, 'cut_vtk') or self.cut_vtk is None:
                print("   ⚠️ No cut_vtk object")
                return
            
            # Method 1: Update dataset directly
            if hasattr(self.cut_vtk, 'dataset') and self.cut_vtk.dataset is not None:
                try:
                    # pyvista dataset
                    self.cut_vtk.dataset['RGB'] = colors
                    print("   ✅ Colors applied to dataset")
                    return
                except Exception as e:
                    print(f"   ⚠️ Dataset color update failed: {e}")
            
            # Method 2: Update actor mapper colors
            if hasattr(self.cut_vtk, 'actor') and self.cut_vtk.actor is not None:
                try:
                    actor = self.cut_vtk.actor
                    mapper = actor.GetMapper()
                    
                    if mapper is not None:
                        _poly = mapper.GetInput()
                        if _poly is None:
                            raise RuntimeError("mapper.GetInput() returned None")
                        # Create color array for VTK
                        vtk_colors = vtk.vtkUnsignedCharArray()
                        vtk_colors.SetNumberOfComponents(3)
                        vtk_colors.SetName("RGB")

                        for color in colors:
                            vtk_colors.InsertNextTuple3(int(color[0]), int(color[1]), int(color[2]))

                        # Assign to mapper
                        _poly.GetPointData().SetScalars(vtk_colors)
                        print("   ✅ Colors applied to actor mapper")
                        return
                except Exception as e:
                    print(f"   ⚠️ Actor mapper color update failed: {e}")
            
            # Method 3: If it's a PolyData with point colors
            if hasattr(self.cut_vtk, '_mesh') and self.cut_vtk._mesh is not None:
                try:
                    self.cut_vtk._mesh['RGB'] = colors
                    print("   ✅ Colors applied to mesh")
                    return
                except Exception as e:
                    print(f"   ⚠️ Mesh color update failed: {e}")
            
            print("   ⚠️ Could not find visualization object to update colors")
            
        except Exception as e:
            print(f"   ❌ Error applying colors: {e}")
            import traceback
            traceback.print_exc()        
            
    def _clear_preview_from_other_views(self, current_vtk):
        if not hasattr(self.app, 'section_vtks'):
            return
        
        actors_to_clear = [
            self.line_actor,
            self.buffer_actor_upper,
            self.buffer_actor_lower
        ]  
        for view_idx, vtk_widget in self.app.section_vtks.items():
            if vtk_widget == current_vtk:
                continue  # Skip the active view
            
            try:
                ren = vtk_widget.renderer
                for actor in actors_to_clear:
                    if actor is not None:
                        try:
                            ren.RemoveActor(actor)
                        except Exception:
                            pass
                
                # Render to show the removal
                try:
                    _safe_vtk_render(vtk_widget)
                except Exception:
                    pass
            except Exception:
                pass
            
