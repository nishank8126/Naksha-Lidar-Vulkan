"""
GPU Render Manager - Non-invasive performance optimization
Handles render throttling, LOD, and GPU memory management
"""

import numpy as np
import weakref
import time
from PySide6.QtCore import QTimer, QObject
from PySide6.QtWidgets import QApplication

try:
    from shiboken6 import isValid as _qt_object_is_valid
except Exception:
    def _qt_object_is_valid(obj):
        return obj is not None


class GPURenderManager(QObject):
    """
    Wraps VTK rendering with intelligent throttling and LOD.
    Zero modifications to existing code - pure wrapper pattern.
    """
    
    def __init__(self, app):
        super().__init__(app)          # parent=app so Qt owns lifetime
        self._app_ref = weakref.ref(app)

        # Render throttling
        self.render_timer = QTimer(self)   # parented — cleaned up with self
        self.render_timer.setSingleShot(True)
        self.render_timer.timeout.connect(self._execute_render)
        self.pending_render = False
        self.render_delay_ms = 100  # 100ms debounce (10 FPS during scroll)

        # Idle timer that ends a wheel-zoom "interaction" after a short pause so
        # full-quality rendering resumes automatically.
        self._wheel_idle_timer = QTimer(self)
        self._wheel_idle_timer.setSingleShot(True)
        self._wheel_idle_timer.timeout.connect(self._on_wheel_idle)
        self._wheel_idle_delay_ms = 150
        # Frame budget (ms) used to coalesce renders during an active pan/zoom/
        # rotate gesture. ~33ms = 30 FPS, which is imperceptibly smooth during a
        # drag while roughly halving GPU work versus rendering every tick.
        self.interaction_frame_ms = 33
        # ✅ FIX: Track pan state to bypass throttle during interactive panning
        self._pan_in_progress = False
        # Manual pan uses a leading-edge frame followed by a bounded cadence.
        # Camera deltas are always current; only redundant repaints are merged.
        self._pan_frame_started = False
        self._pan_last_frame_monotonic = 0.0
        # Whether any camera interaction (pan/zoom/rotate) is active.
        self._interaction_active = False
        # Whether we disabled the interactor's native auto-render.
        self._interactor_render_disabled = False
        
        # Performance tracking
        self.last_render_time = 0
        self.render_count = 0
        self.skipped_renders = 0
        
        # LOD management
        self.lod_enabled = True
        self.last_camera_distance = None
        
        # Original render functions (to restore if needed)
        self._original_render = None
        self._original_add_points = None
        self._wrapped_vtk_widget = None
        self._render_wrapped = False

        # Track installed VTK observer IDs for clean removal
        self._camera_observer_ids: list = []
        self._camera_interactor = None
        
        print("✅ GPU Render Manager initialized")


    @property
    def app(self):
        """Return live app or None if destroyed."""
        return self._app_ref()

    def install(self):
        """Install render hooks into app"""
        try:
            app = self.app
            if app is None:
                return

            # Hook into main VTK widget
            if hasattr(app, 'vtk_widget') and app.vtk_widget is not None:
                self._wrap_vtk_widget(app.vtk_widget)
                print("   📌 Hooked main VTK widget")
            
            # Hook into camera observers
            if (
                hasattr(app, 'vtk_widget')
                and app.vtk_widget is not None
                and getattr(app.vtk_widget, 'interactor', None) is not None
            ):
                current_interactor = app.vtk_widget.interactor
                if self._camera_interactor is not None and self._camera_interactor is not current_interactor:
                    self._remove_camera_observers()
                if not self._camera_observer_ids:
                    self._install_camera_observer()
                    print("   📌 Installed camera observer")
            else:
                print("   ℹ️ Camera observer deferred (VTK widget not ready yet)")
            
            # Activate LOD if available
            if not hasattr(app, 'lod_manager'):
                from gui.performance_optimizations import LODManager
                app.lod_manager = LODManager(app)
                print("   📌 LOD Manager activated")
            
            print("✅ GPU Render Manager installed successfully")
            
        except Exception as e:
            print(f"⚠️  GPU Render Manager installation warning: {e}")
    
    def _wrap_vtk_widget(self, vtk_widget):
        """Wrap VTK widget's render method with throttling.

        Uses weakrefs on both vtk_widget and self to avoid a reference
        cycle that would prevent GC on project reload.
        """
        if not hasattr(vtk_widget, 'render'):
            return
        if self._render_wrapped:
            ref = self._wrapped_vtk_widget
            if ref is not None and ref() is vtk_widget:
                return
            # Restore previous wrapped widget before re-wrapping a new instance.
            self._unwrap_vtk_widget()

        self._original_render = vtk_widget.render
        self._wrapped_vtk_widget = weakref.ref(vtk_widget)
        self._render_wrapped = True

        mgr_ref = weakref.ref(self)
        widget_ref = weakref.ref(vtk_widget)

        def throttled_render(*args, **kwargs):
            mgr = mgr_ref()
            widget = widget_ref()
            if mgr is None or widget is None:
                # Owner gone — fall back to nothing (widget is being torn down)
                return
            return mgr.request_render(widget, *args, **kwargs)

        vtk_widget.render = throttled_render
        print(f"      ✓ Wrapped vtk_widget.render()")

        # Make the interactor's native auto-render a no-op during interaction so
        # that pan/zoom/rotate go through our throttled request_render() path
        # (LOD + debounce) instead of doing a full unthrottled render on every
        # mouse-move tick. Manual vtk_widget.render() (our _original_render)
        # still works and is what request_render/_execute_render use.
        try:
            inter = getattr(vtk_widget, "interactor", None)
            if inter is not None and hasattr(inter, "EnableRenderOff"):
                inter.EnableRenderOff()
                self._interactor_render_disabled = True
                print("      ✓ Interactor auto-render disabled (routed via throttle)")
        except Exception:
            self._interactor_render_disabled = False

    def _unwrap_vtk_widget(self):
        """Restore the original render method and break the closure cycle."""
        ref = getattr(self, '_wrapped_vtk_widget', None)
        if ref is None:
            return
        widget = ref()
        if widget is not None and _qt_object_is_valid(widget) and self._original_render is not None:
            try:
                widget.render = self._original_render
                print("[GPURenderManager] vtk_widget.render restored")
            except Exception:
                pass
        # Restore native interactor auto-render so behavior is exactly as before
        # the manager was installed.
        if getattr(self, "_interactor_render_disabled", False):
            try:
                inter = getattr(widget, "interactor", None) if widget is not None else None
                if inter is not None and hasattr(inter, "EnableRenderOn"):
                    inter.EnableRenderOn()
            except Exception:
                pass
            self._interactor_render_disabled = False
        self._original_render = None
        self._wrapped_vtk_widget = None
        self._render_wrapped = False
    
    def _install_camera_observer(self):
        """Install observer for camera movements — IDs stored for cleanup."""
        try:
            interactor = self.app.vtk_widget.interactor
            self._camera_interactor = interactor

            id1 = interactor.AddObserver("MouseWheelForwardEvent", self._on_camera_interaction)
            id2 = interactor.AddObserver("MouseWheelBackwardEvent", self._on_camera_interaction)
            id3 = interactor.AddObserver("InteractionEvent", self._on_camera_interaction)
            # ✅ FIX: Track middle-mouse pan so renders are immediate during the gesture
            id4 = interactor.AddObserver("MiddleButtonPressEvent", self._on_pan_start)
            id5 = interactor.AddObserver("MiddleButtonReleaseEvent", self._on_pan_end)
            # ✅ Drive all interaction renders through the throttled render path so
            # pan/zoom/rotate use LOD + debounce instead of a full unthrottled
            # render on every mouse-move tick.
            id6 = interactor.AddObserver("StartInteractionEvent", self._on_interaction_start)
            id7 = interactor.AddObserver("EndInteractionEvent", self._on_interaction_end)

            self._camera_observer_ids = [id1, id2, id3, id4, id5, id6, id7]
            print("      ✓ Camera observers installed")

        except Exception as e:
            print(f"      ⚠️  Camera observer warning: {e}")

    def _on_pan_start(self, obj, event):
        """Middle mouse pressed — disable render throttle for smooth pan."""
        app = self.app
        try:
            point_count = len(app.data.get("xyz", ())) if app is not None and app.data else 0
        except Exception:
            point_count = 0
        self.begin_pan_interaction(point_count=point_count)

    def _on_pan_end(self, obj, event):
        """Middle mouse released — restore normal render throttle and settle frame."""
        self.finish_pan_interaction()

    def begin_pan_interaction(self, *, point_count: int = 0):
        """Enter the coalesced render path for a manual middle-button pan."""
        app = self.app
        if app is None or getattr(app, "_shutdown_in_progress", False):
            return False

        # Pan becomes the sole camera interaction. A wheel-idle callback or a
        # render queued by the preceding gesture must not wake up mid-drag and
        # block Qt long enough for mouse moves to accumulate behind it.
        try:
            self._wheel_idle_timer.stop()
        except Exception:
            pass
        try:
            self.render_timer.stop()
        except Exception:
            pass
        self.pending_render = False

        try:
            from gui.zoom_navigation import pan_frame_interval_ms
            self.interaction_frame_ms = pan_frame_interval_ms(point_count)
        except Exception:
            self.interaction_frame_ms = max(33, int(self.interaction_frame_ms or 33))
        self._pan_in_progress = True
        self._pan_frame_started = False
        self._pan_last_frame_monotonic = 0.0
        self._interaction_active = True
        return True

    def finish_pan_interaction(self):
        """Leave manual-pan mode and draw one final full-fidelity frame."""
        was_active = bool(self._pan_in_progress)
        self._pan_in_progress = False
        self._pan_frame_started = False
        self._pan_last_frame_monotonic = 0.0
        self._interaction_active = False
        app = self.app
        if app is None or getattr(app, "_shutdown_in_progress", False):
            return False
        if not was_active:
            return False
        try:
            from gui.unified_actor_manager import refresh_widget_camera_uniforms
            refresh_widget_camera_uniforms(getattr(app, "vtk_widget", None))
        except Exception:
            pass
        self.force_render()
        return True

    def _on_interaction_start(self, obj, event):
        """Any camera interaction (pan/zoom/rotate) began — mark active so the
        throttle can treat interaction renders specially (immediate during pan,
        debounced otherwise) and LOD can engage."""
        self._interaction_active = True
        self._engage_lod()

    def _on_interaction_end(self, obj, event):
        """Camera interaction finished — settle with one full-quality render and
        restore full-detail LOD."""
        self._interaction_active = False
        app = self.app
        if app is None or getattr(app, "_shutdown_in_progress", False):
            return
        # Restore full-resolution cloud (if a decimated LOD proxy was applied).
        self._restore_full_detail()
        try:
            from gui.unified_actor_manager import refresh_widget_camera_uniforms
            refresh_widget_camera_uniforms(getattr(app, "vtk_widget", None))
        except Exception:
            pass
        # Single final render at full quality.
        self.force_render()

    def begin_wheel_interaction(self, *, point_count: int = 0):
        """Start/restart a Qt-routed wheel gesture without invoking VTK events."""
        app = self.app
        if app is None or getattr(app, "_shutdown_in_progress", False):
            return False

        try:
            from gui.zoom_navigation import interaction_frame_interval_ms
            self.interaction_frame_ms = interaction_frame_interval_ms(point_count)
        except Exception:
            self.interaction_frame_ms = max(33, int(self.interaction_frame_ms or 33))

        self._interaction_active = True
        self._engage_lod()
        try:
            self._wheel_idle_timer.stop()
        except Exception:
            pass
        self._wheel_idle_timer.start(self._wheel_idle_delay_ms)
        return True

    def finish_wheel_interaction(self):
        """Settle a wheel gesture immediately (kept for legacy callers)."""
        self._on_wheel_idle()

    def _on_wheel_idle(self):
        """Wheel-zoom gesture settled — drop interaction flag, restore full
        detail, and repaint once at full quality."""
        self._interaction_active = False
        self._restore_full_detail()
        app = self.app
        if app is None or getattr(app, "_shutdown_in_progress", False):
            return
        self.force_render()

    def _engage_lod(self):
        """Keep the production actor unchanged throughout navigation.

        A sampled actor cannot reproduce the full cloud's point coverage or
        boundary appearance exactly. Camera responsiveness is therefore
        provided only by event de-duplication and render coalescing; the actor
        and every display-mode buffer remain identical before, during and after
        pan/zoom.
        """
        return False

    def _remove_camera_observers(self):
        """Remove all registered VTK camera observers and reset interaction state."""
        interactor = self._camera_interactor
        if interactor is None:
            return
        for oid in self._camera_observer_ids:
            try:
                interactor.RemoveObserver(oid)
            except Exception:
                pass
        self._camera_observer_ids.clear()
        self._camera_interactor = None
        # Reset pan flag — if the interactor is replaced while the middle button
        # is held, there will be no MiddleButtonReleaseEvent on the new interactor,
        # so we must clear the flag here to avoid a stuck-throttle-bypass state.
        self._pan_in_progress = False
        self._pan_frame_started = False
        self._pan_last_frame_monotonic = 0.0
        self._interaction_active = False
        print("[GPURenderManager] Camera observers removed")
    
    def _on_camera_interaction(self, obj, event):
        """Called on every camera movement - route through throttled render.

        Because the interactor's native auto-render is disabled
        (EnableRenderOff in _wrap_vtk_widget), this is what actually repaints
        the view during pan/zoom/rotate. request_render() applies the existing
        pan-immediate / debounce / LOD logic instead of a full unthrottled
        render per mouse-move tick.
        """
        try:
            app = self.app
            if app is None:
                return
            widget = getattr(app, "vtk_widget", None)
            if widget is None:
                return

            # Recompute LOD distance and apply a decimated proxy when warranted.
            self._check_lod_update()

            # Treat wheel zoom as an active interaction so it shares the frame
            # budget (smooth + lighter). A short timer clears the flag after the
            # gesture settles, restoring full-quality rendering.
            if event in ("MouseWheelForwardEvent", "MouseWheelBackwardEvent"):
                self._interaction_active = True
                self._engage_lod()
                try:
                    self._wheel_idle_timer.stop()
                except Exception:
                    pass
                self._wheel_idle_timer.start(120)

            # Route the repaint through the throttled manager (no-op if a render
            # is already pending within the debounce window).
            self.request_render(widget)
        except Exception:
            pass  # Silent fail during interaction
    
    def _check_lod_update(self):
        """Track camera distance (used for diagnostics / future tuning).

        The LOD proxy is now engaged/restored by the interaction start/end
        handlers (``_engage_lod`` / ``_restore_full_detail``), so this only
        records the distance. Pure no-op if LOD is disabled.
        """
        try:
            if not self.lod_enabled or not hasattr(self.app, 'lod_manager'):
                return
            app = self.app
            if app is None or not hasattr(app, 'vtk_widget'):
                return
            renderer = getattr(app.vtk_widget, 'renderer', None)
            if renderer is None:
                return
            cam = renderer.GetActiveCamera()
            cam_pos = np.array(cam.GetPosition())
            focal = np.array(cam.GetFocalPoint())
            distance = float(np.linalg.norm(cam_pos - focal))
            self.last_camera_distance = distance
        except Exception:
            pass

    def _restore_full_detail(self):
        """Restore full-resolution cloud after an interaction gesture.

        No-op unless the app implements ``restore_full_detail_cloud()``.
        """
        try:
            app = self.app
            if app is None:
                return
            restore = getattr(app, "restore_full_detail_cloud", None)
            if callable(restore):
                restore()
        except Exception:
            pass
    
    def request_render(self, vtk_widget, *args, **kwargs):
        """
        Throttled render request - batches rapid calls.

        Key optimization: Multiple rapid renders → Single delayed render

        ⚡ Classification-streak coalescing:
        While the user is actively classifying (any classify within the last
        1.0 s), every main-view render request RESTARTS the timer instead of
        firing synchronously. The render fires only after the streak pauses
        for ``render_delay_ms``. This eliminates the mid-streak ~10–30 ms
        synchronous render on the 39 M-point main view that users feel as
        "stutter every few clicks." Camera/scroll/palette paths keep the
        original throttle-or-immediate behavior — they're unaffected.
        """
        current_time = time.time()
        app = self.app
        if app is None:
            return
        if getattr(app, "_shutdown_in_progress", False):
            return
        if not self._is_widget_renderable(vtk_widget):
            return

        # Detect active classification streak via _last_classify_ts (stamped
        # by _apply_classification / _apply_mask_and_record).
        last_classify = getattr(app, "_last_classify_ts", 0.0) if app is not None else 0.0
        in_classify_streak = bool(last_classify) and (current_time - last_classify) < 1.0

        if in_classify_streak:
            # Coalesce: restart timer on every request. Main view repaints
            # only after the user pauses (no classify for render_delay_ms).
            try:
                self.render_timer.stop()
            except Exception:
                pass
            if self.pending_render:
                self.skipped_renders += 1
            self.pending_render = True
            self.render_timer.start(self.render_delay_ms)
            return

        # Manual pan gets a zero-delay leading frame for immediate feedback.
        # Later requests keep the most recent camera and render at the adaptive
        # cadence. The timer is deliberately NOT restarted: restart/debounce
        # made large clouds appear frozen until the pointer stopped moving.
        if self._pan_in_progress:
            if self.pending_render:
                self.skipped_renders += 1
                return

            delay_ms = 0
            if self._pan_frame_started:
                elapsed_ms = (
                    time.monotonic() - self._pan_last_frame_monotonic
                ) * 1000.0
                delay_ms = max(0, int(round(self.interaction_frame_ms - elapsed_ms)))

            self.pending_render = True
            self.render_timer.start(delay_ms)
            return

        if self._interaction_active:
            if not self.pending_render:
                self.pending_render = True
                self.render_timer.start(self.interaction_frame_ms)
            else:
                self.skipped_renders += 1
            return

        # Non-streak: original throttle behavior (immediate if window elapsed,
        # else schedule once for the remainder of the window).
        time_since_last = (current_time - self.last_render_time) * 1000  # ms

        if time_since_last < self.render_delay_ms:
            # Too soon - schedule for later
            if not self.pending_render:
                self.pending_render = True
                self.render_timer.start(self.render_delay_ms)
                self.skipped_renders += 1
            return

        # Enough time passed - render immediately
        self._execute_render()
    
    def _execute_render(self):
        """Execute the actual render (called after debounce delay)"""
        try:
            self.pending_render = False
            app = self.app
            if app is None or getattr(app, "_shutdown_in_progress", False):
                return

            widget = None
            wrapped_ref = getattr(self, "_wrapped_vtk_widget", None)
            if wrapped_ref is not None:
                widget = wrapped_ref()
            if widget is None:
                widget = getattr(app, "vtk_widget", None)
            if not self._is_widget_renderable(widget):
                return

            # Measure pan cadence from render start. If drawing the 30M+ point
            # actor already consumes the frame budget, the next coalesced
            # camera position may render immediately instead of waiting again.
            if self._pan_in_progress:
                self._pan_frame_started = True
                self._pan_last_frame_monotonic = time.monotonic()
             
            # Use original render method
            if self._original_render:
                self._original_render()
            else:
                # Fallback
                rw = widget.GetRenderWindow() if hasattr(widget, "GetRenderWindow") else None
                if rw is None:
                    return
                rw.Render()
            
            self.last_render_time = time.time()
            self.render_count += 1
            
            # Log performance every 50 renders
            if self.render_count % 50 == 0:
                efficiency = (self.skipped_renders / (self.render_count + self.skipped_renders + 1)) * 100
                print(f"🎯 Render efficiency: {efficiency:.1f}% saved ({self.skipped_renders} skipped / {self.render_count} executed)")
                
        except Exception as e:
            print(f"⚠️  Render execution error: {e}")

    def _is_widget_renderable(self, vtk_widget) -> bool:
        """Best-effort guard against rendering stale Qt/VTK widgets."""
        try:
            if vtk_widget is None:
                return False
            if not _qt_object_is_valid(vtk_widget):
                return False
            if bool(getattr(vtk_widget, "_naksha_skip_render", False)):
                return False
            if hasattr(vtk_widget, "isVisible") and callable(vtk_widget.isVisible):
                if not vtk_widget.isVisible():
                    return False
            rw = vtk_widget.GetRenderWindow() if hasattr(vtk_widget, "GetRenderWindow") else None
            if rw is None:
                return False
            if hasattr(rw, "GetInteractor") and rw.GetInteractor() is None:
                return False
            return True
        except Exception:
            return False
    
    def force_render(self):
        """Force immediate render (bypass throttling)"""
        self.render_timer.stop()
        self.pending_render = False
        self._execute_render()
    
    def set_lod_enabled(self, enabled: bool):
        """Enable/disable LOD system"""
        self.lod_enabled = enabled
        print(f"{'✅' if enabled else '❌'} LOD {'enabled' if enabled else 'disabled'}")

    def cleanup(self):
        """
        Full teardown: remove VTK observers and restore the original render
        method.  Call this on project close, VTK widget replacement, or
        application shutdown.
        """
        self.render_timer.stop()
        self._wheel_idle_timer.stop()
        self.pending_render = False
        try:
            self.render_timer.timeout.disconnect(self._execute_render)
        except Exception:
            pass
        self._remove_camera_observers()
        self._unwrap_vtk_widget()
        print("[GPURenderManager] Cleanup complete")


    def set_render_delay(self, delay_ms: int):
        """
        Adjust render throttle delay.
        
        Lower = more responsive but higher GPU load
        Higher = smoother but slight lag
        
        Recommended: 100ms for 4GB GPU, 50ms for 8GB+ GPU
        """
        self.render_delay_ms = max(16, min(delay_ms, 500))  # Clamp 16-500ms
        print(f"⏱️  Render delay: {self.render_delay_ms}ms")
    
    def get_stats(self):
        """Return performance statistics"""
        total = self.render_count + self.skipped_renders
        if total == 0:
            return {"efficiency": 0, "renders": 0, "skipped": 0}
        
        return {
            "efficiency": (self.skipped_renders / total) * 100,
            "renders": self.render_count,
            "skipped": self.skipped_renders,
            "avg_delay_ms": self.render_delay_ms
        }


class AdaptivePointSize:
    """
    Automatically adjust point size based on zoom level.
    Smaller points when zoomed out = less GPU geometry.
    """
    
    def __init__(self, app):
        self.app = app
        self.base_point_size = 2.0
        self.min_size = 1.0
        self.max_size = 5.0
    
    def get_adaptive_size(self) -> float:
        """Calculate point size based on camera distance"""
        try:
            cam = self.app.vtk_widget.renderer.GetActiveCamera()
            cam_pos = np.array(cam.GetPosition())
            focal = np.array(cam.GetFocalPoint())
            distance = np.linalg.norm(cam_pos - focal)
            
            # Simple heuristic: closer = larger points
            if distance < 100:
                return self.max_size
            elif distance < 500:
                return self.base_point_size
            elif distance < 1000:
                return self.base_point_size * 0.75
            else:
                return self.min_size
                
        except Exception:
            return self.base_point_size


# ============================================================================
# HELPER FUNCTIONS - Call these from your existing code
# ============================================================================

def safe_render(app):
    """
    Replacement for app.vtk_widget.render()
    Use this in critical places where you need guaranteed render.
    """
    if hasattr(app, 'gpu_render_manager'):
        app.gpu_render_manager.force_render()
    else:
        app.vtk_widget.render()


def get_render_stats(app):
    """Get performance statistics"""
    if hasattr(app, 'gpu_render_manager'):
        return app.gpu_render_manager.get_stats()
    return None


def configure_render_delay(app, delay_ms: int):
    """
    Configure render throttle delay.
    
    Call this based on GPU capability:
    - 4GB GPU: configure_render_delay(app, 100)  # Conservative
    - 8GB+ GPU: configure_render_delay(app, 50)   # Responsive
    """
    if hasattr(app, 'gpu_render_manager'):
        app.gpu_render_manager.set_render_delay(delay_ms)
