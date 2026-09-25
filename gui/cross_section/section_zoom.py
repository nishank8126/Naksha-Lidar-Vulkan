"""Lifecycle-safe, immediate wheel zoom for section viewports.

The section windows use several VTK interactor styles depending on the active
tool.  Installing the wheel handler at the Qt boundary keeps zoom behaviour
consistent while leaving every existing VTK handler available as a fallback.
"""

from __future__ import annotations

import math
import weakref

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QTimer, Qt

from gui.zoom_navigation import (
    FAST_WHEEL_ZOOM_FACTOR,
    MAX_WHEEL_STEPS_PER_EVENT,
    fast_wheel_zoom_factor,
    qt_position_to_vtk_display,
)

try:
    from shiboken6 import isValid as _qt_object_is_valid
except Exception:  # pragma: no cover - only used with incomplete Qt installs
    def _qt_object_is_valid(obj):
        return obj is not None


SECTION_WHEEL_ZOOM_FACTOR = FAST_WHEEL_ZOOM_FACTOR
MIN_PARALLEL_SCALE = 1.0e-6
MAX_PARALLEL_SCALE = 1.0e12
SECTION_RENDER_INTERVAL_MS = 16


def section_wheel_zoom_factor(
    wheel_delta: float,
    *,
    base_factor: float = SECTION_WHEEL_ZOOM_FACTOR,
    max_steps: float = MAX_WHEEL_STEPS_PER_EVENT,
) -> float:
    """Convert raw Qt wheel units to a bounded, continuous zoom multiplier."""
    return fast_wheel_zoom_factor(
        wheel_delta,
        base_factor=base_factor,
        max_steps=max_steps,
    )


def _display_to_world_on_focal_plane(renderer, camera, display_x, display_y):
    """Resolve one display coordinate on the active camera's focal plane."""
    focal_point = camera.GetFocalPoint()
    renderer.SetWorldPoint(
        float(focal_point[0]),
        float(focal_point[1]),
        float(focal_point[2]),
        1.0,
    )
    renderer.WorldToDisplay()
    focal_display = renderer.GetDisplayPoint()
    if focal_display is None or len(focal_display) < 3:
        return None

    renderer.SetDisplayPoint(
        float(display_x),
        float(display_y),
        float(focal_display[2]),
    )
    renderer.DisplayToWorld()
    world_point = renderer.GetWorldPoint()
    if world_point is None or len(world_point) < 4:
        return None
    homogeneous_w = float(world_point[3])
    if not math.isfinite(homogeneous_w) or abs(homogeneous_w) < 1.0e-12:
        return None
    result = tuple(float(world_point[index]) / homogeneous_w for index in range(3))
    if not all(math.isfinite(value) for value in result):
        return None
    return result


def apply_cursor_anchored_parallel_zoom(
    renderer,
    camera,
    factor,
    display_position,
) -> bool:
    """Zoom an orthographic section while keeping the cursor world point fixed."""
    if renderer is None or camera is None or display_position is None:
        return False

    snapshot = {
        "position": tuple(camera.GetPosition()),
        "focal_point": tuple(camera.GetFocalPoint()),
        "parallel_scale": float(camera.GetParallelScale()),
        "parallel_projection": bool(camera.GetParallelProjection()),
    }

    def _restore():
        camera.SetPosition(snapshot["position"])
        camera.SetFocalPoint(snapshot["focal_point"])
        camera.SetParallelScale(snapshot["parallel_scale"])
        if snapshot["parallel_projection"]:
            camera.ParallelProjectionOn()
        else:
            camera.ParallelProjectionOff()

    try:
        zoom_factor = float(factor)
        old_scale = snapshot["parallel_scale"]
        if (
            not math.isfinite(zoom_factor)
            or zoom_factor <= 0.0
            or not math.isfinite(old_scale)
            or old_scale <= 0.0
        ):
            return False

        display_x = float(display_position[0])
        display_y = float(display_position[1])
        if not math.isfinite(display_x) or not math.isfinite(display_y):
            return False

        camera.ParallelProjectionOn()
        world_before = _display_to_world_on_focal_plane(
            renderer, camera, display_x, display_y
        )
        if world_before is None:
            _restore()
            return False

        next_scale = max(
            MIN_PARALLEL_SCALE,
            min(MAX_PARALLEL_SCALE, old_scale / zoom_factor),
        )
        camera.SetParallelScale(next_scale)

        world_after = _display_to_world_on_focal_plane(
            renderer, camera, display_x, display_y
        )
        if world_after is None:
            _restore()
            return False

        delta = tuple(
            world_before[index] - world_after[index]
            for index in range(3)
        )
        if not all(math.isfinite(value) for value in delta):
            _restore()
            return False

        position = camera.GetPosition()
        focal_point = camera.GetFocalPoint()
        camera.SetPosition(tuple(position[index] + delta[index] for index in range(3)))
        camera.SetFocalPoint(
            tuple(focal_point[index] + delta[index] for index in range(3))
        )
        return True
    except (RuntimeError, AttributeError, OSError, ReferenceError, TypeError, ValueError):
        try:
            _restore()
        except Exception:
            pass
        return False
    except Exception:
        try:
            _restore()
        except Exception:
            pass
        return False


def _weak_ref(value):
    try:
        return weakref.ref(value)
    except TypeError:
        # Normal application objects are weak-referenceable.  This fallback is
        # useful for small test doubles and remains owned by this short-lived
        # QObject, which is parented to the canvas widget.
        return lambda: value


class SectionWheelZoomEventFilter(QObject):
    """Apply section zoom immediately and coalesce only the expensive repaint."""

    def __init__(self, app, vtk_widget, qt_interactor):
        super().__init__(qt_interactor)
        self._app_ref = _weak_ref(app)
        self._vtk_widget_ref = _weak_ref(vtk_widget)
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(SECTION_RENDER_INTERVAL_MS)
        self._render_timer.timeout.connect(self._flush_render)
        self._settle_timer = QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.setInterval(150)
        self._settle_timer.timeout.connect(self._settle_full_detail)
        # Nakshatech-style dynamic pan for every cross/cut viewport. The
        # first left tap starts a button-free pan and the second tap stops it.
        self._tap_pan_active = False
        self._tap_last_position = None
        self._tap_swallow_release = False

    @property
    def vtk_widget(self):
        return self._vtk_widget_ref()

    def _is_viewport_event(self, obj) -> bool:
        """Limit the application-level hook to this section's Qt viewport."""
        qt_interactor = self.parent()
        if obj is None or qt_interactor is None:
            return False
        try:
            current = obj
            while current is not None:
                if current is qt_interactor:
                    return True
                current = current.parent()
        except (RuntimeError, AttributeError, ReferenceError):
            return False
        except Exception:
            return False
        return False

    def _teardown_in_progress(self) -> bool:
        """Return True when a wheel event must be dropped, not forwarded."""
        app = self._app_ref()
        vtk_widget = self.vtk_widget
        if app is None or vtk_widget is None:
            return True
        if bool(getattr(app, "_shutdown_in_progress", False)):
            return True
        if bool(getattr(vtk_widget, "_naksha_skip_render", False)):
            return True
        if bool(getattr(vtk_widget, "_naksha_view_finalized", False)):
            return True

        cut_controller = getattr(app, "cut_section_controller", None)
        if cut_controller is not None and getattr(cut_controller, "cut_vtk", None) is vtk_widget:
            if bool(getattr(cut_controller, "_is_destroying", False)):
                return True
            if bool(getattr(cut_controller, "_cut_vtk_finalized", False)):
                return True
        return False

    def _live_components(self, *, require_visible: bool):
        """Resolve live Qt/VTK objects without ever allowing an exception out."""
        if self._teardown_in_progress():
            return None

        qt_interactor = self.parent()
        vtk_widget = self.vtk_widget
        if qt_interactor is None or vtk_widget is None:
            return None
        try:
            if not _qt_object_is_valid(qt_interactor):
                return None
            if require_visible and hasattr(vtk_widget, "isVisible"):
                if not vtk_widget.isVisible():
                    return None

            render_window = vtk_widget.GetRenderWindow()
            if render_window is None or render_window.GetInteractor() is None:
                return None
            renderer = getattr(vtk_widget, "renderer", None)
            if renderer is None:
                return None
            camera = renderer.GetActiveCamera()
            if camera is None:
                return None
            return vtk_widget, qt_interactor, renderer, camera
        except (RuntimeError, AttributeError, OSError, ReferenceError):
            return None
        except Exception:
            return None

    @staticmethod
    def _camera_snapshot(camera):
        return {
            "position": tuple(camera.GetPosition()),
            "focal_point": tuple(camera.GetFocalPoint()),
            "parallel_scale": float(camera.GetParallelScale()),
            "parallel_projection": bool(camera.GetParallelProjection()),
        }

    @staticmethod
    def _restore_camera(camera, snapshot) -> None:
        try:
            camera.SetPosition(snapshot["position"])
            camera.SetFocalPoint(snapshot["focal_point"])
            camera.SetParallelScale(snapshot["parallel_scale"])
            if snapshot["parallel_projection"]:
                camera.ParallelProjectionOn()
            else:
                camera.ParallelProjectionOff()
        except Exception:
            pass

    def _apply_anchored_zoom(
        self,
        qt_interactor,
        renderer,
        camera,
        factor,
        display_position=None,
    ) -> bool:
        """Apply the mandatory section cursor anchor without rendering."""
        if display_position is None:
            try:
                display_position = qt_interactor.GetEventPosition()
            except Exception:
                return False
        return apply_cursor_anchored_parallel_zoom(
            renderer,
            camera,
            factor,
            display_position,
        )

    def handle_wheel_delta(self, wheel_delta: float, *, display_position=None) -> bool:
        """Handle raw wheel input. False lets the established VTK path run."""
        components = self._live_components(require_visible=False)
        if components is None:
            return False

        vtk_widget, qt_interactor, renderer, camera = components
        snapshot = None
        try:
            factor = section_wheel_zoom_factor(wheel_delta)
            if math.isclose(factor, 1.0, rel_tol=0.0, abs_tol=1.0e-12):
                return False

            snapshot = self._camera_snapshot(camera)
            old_scale = snapshot["parallel_scale"]
            if not math.isfinite(old_scale) or old_scale <= 0.0:
                return False

            # Section cameras are orthographic by contract.  Enforce that before
            # using Camera.Zoom so cursor/picked-point helpers change ParallelScale.
            if not camera.GetParallelProjection():
                camera.ParallelProjectionOn()

            anchored = False
            try:
                anchored = self._apply_anchored_zoom(
                    qt_interactor,
                    renderer,
                    camera,
                    factor,
                    display_position=display_position,
                )
            except Exception:
                self._restore_camera(camera, snapshot)
                anchored = False

            if not anchored:
                # A helper may fail after touching the camera.  Restore first so
                # the center fallback always applies exactly one zoom operation.
                self._restore_camera(camera, snapshot)
                camera.ParallelProjectionOn()
                next_scale = old_scale / factor
                next_scale = max(MIN_PARALLEL_SCALE, min(MAX_PARALLEL_SCALE, next_scale))
                camera.SetParallelScale(next_scale)
            else:
                next_scale = float(camera.GetParallelScale())
                if not math.isfinite(next_scale) or next_scale <= 0.0:
                    self._restore_camera(camera, snapshot)
                    return False
                camera.SetParallelScale(
                    max(MIN_PARALLEL_SCALE, min(MAX_PARALLEL_SCALE, next_scale))
                )

            if self._engage_interaction_detail(vtk_widget):
                self._settle_timer.start()
            if not self._render_timer.isActive():
                self._render_timer.start()
            return True
        except (RuntimeError, AttributeError, OSError, ReferenceError, ValueError):
            if snapshot is not None:
                self._restore_camera(camera, snapshot)
            return False
        except Exception:
            if snapshot is not None:
                self._restore_camera(camera, snapshot)
            return False

    def _flush_render(self) -> None:
        """Render once on the Qt thread, dropping stale callbacks during teardown."""
        components = self._live_components(require_visible=True)
        if components is None:
            return
        vtk_widget, _qt_interactor, _renderer, _camera = components
        try:
            vtk_widget.render()
        except (RuntimeError, AttributeError, OSError, ReferenceError):
            pass
        except Exception:
            pass

    @staticmethod
    def _engage_interaction_detail(vtk_widget) -> bool:
        # Production views must not change density, colors, class visibility,
        # weights or border coverage in the middle of navigation.
        return False

    def _settle_full_detail(self) -> None:
        components = self._live_components(require_visible=True)
        if components is None:
            return
        vtk_widget, _qt_interactor, renderer, _camera = components
        try:
            from gui.unified_actor_manager import restore_view_full_detail
            restored = bool(restore_view_full_detail(vtk_widget))
        except Exception:
            restored = False
        if not restored:
            return
        try:
            self._render_timer.stop()
            renderer.ResetCameraClippingRange()
            vtk_widget.render()
        except (RuntimeError, AttributeError, OSError, ReferenceError):
            pass
        except Exception:
            pass

    def _tap_pan_configured(self) -> bool:
        app = self._app_ref()
        return app is not None and getattr(app, "panning_button", "scroll") == "tap"

    def _left_click_owned_by_tool(self) -> bool:
        """Keep navigation from stealing section/cut tool input."""
        app = self._app_ref()
        vtk_widget = self.vtk_widget
        if app is None:
            return True
        if getattr(app, "cross_section_active", False):
            return True
        if getattr(app, "active_classify_tool", None) is not None:
            return True
        for name in (
            "cross_section_measurement_tool",
            "measurement_tool",
            "identification_tool",
            "point_sync_tool",
            "snt_layer_pick_tool",
        ):
            tool = getattr(app, name, None)
            if tool is not None and (
                getattr(tool, "active", False)
                or getattr(tool, "is_measuring", False)
            ):
                return True
        cut = getattr(app, "cut_section_controller", None)
        if cut is not None:
            state = getattr(cut, "_state", 0)
            if state in (1, 2):  # WAITING_CENTER / WAITING_DEPTH
                owns = getattr(cut, "owns_cross_section_left_click", None)
                if getattr(cut, "cut_vtk", None) is vtk_widget:
                    return True
                if callable(owns) and owns(vtk_widget):
                    return True
        return False

    def _finish_tap_pan(self) -> None:
        self._tap_pan_active = False
        self._tap_last_position = None
        self._tap_swallow_release = False

    def _apply_tap_pan_move(self, position) -> bool:
        components = self._live_components(require_visible=False)
        if components is None or self._tap_last_position is None:
            self._finish_tap_pan()
            return False
        vtk_widget, _qt_interactor, renderer, camera = components
        try:
            current = (float(position.x()), float(position.y()))
            dx = current[0] - self._tap_last_position[0]
            dy = current[1] - self._tap_last_position[1]
            self._tap_last_position = current
            if dx == 0.0 and dy == 0.0:
                return True

            render_window = vtk_widget.GetRenderWindow()
            size = render_window.GetSize() if render_window is not None else (800, 600)
            height = max(float(size[1]), 1.0)
            if camera.GetParallelProjection():
                scale = (2.0 * float(camera.GetParallelScale())) / height
            else:
                scale = float(camera.GetDistance()) / height

            camera.OrthogonalizeViewUp()
            up = [float(value) for value in camera.GetViewUp()]
            direction = [float(value) for value in camera.GetDirectionOfProjection()]
            right = [
                direction[1] * up[2] - direction[2] * up[1],
                direction[2] * up[0] - direction[0] * up[2],
                direction[0] * up[1] - direction[1] * up[0],
            ]
            magnitude = math.sqrt(sum(value * value for value in right))
            if magnitude <= 1.0e-12:
                return False
            right = [value / magnitude for value in right]
            position_before = tuple(camera.GetPosition())
            focal_before = tuple(camera.GetFocalPoint())
            delta = [
                -dx * scale * right[index] - dy * scale * up[index]
                for index in range(3)
            ]
            camera.SetPosition(*[
                float(position_before[index]) + delta[index]
                for index in range(3)
            ])
            camera.SetFocalPoint(*[
                float(focal_before[index]) + delta[index]
                for index in range(3)
            ])
            if not self._render_timer.isActive():
                self._render_timer.start()
            return True
        except Exception:
            self._finish_tap_pan()
            return False

    def eventFilter(self, obj, event):
        event_type = event.type()
        if event_type != QEvent.Wheel:
            if not self._is_viewport_event(obj):
                return False
            if self._tap_pan_active and self._left_click_owned_by_tool():
                self._finish_tap_pan()
                return False
            if event_type == QEvent.KeyPress and self._tap_pan_active:
                is_escape = event.key() == Qt.Key_Escape
                self._finish_tap_pan()
                if is_escape:
                    event.accept()
                    return True
                return False
            if event_type in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick):
                button = event.button()
                if button == Qt.RightButton and self._tap_pan_active:
                    self._finish_tap_pan()
                    event.accept()
                    return True
                if (
                    button == Qt.LeftButton
                    and self._tap_pan_configured()
                    and not (event.modifiers() & Qt.ShiftModifier)
                    and not self._left_click_owned_by_tool()
                ):
                    if self._tap_pan_active:
                        self._finish_tap_pan()
                    else:
                        position = event.position()
                        self._tap_pan_active = True
                        self._tap_last_position = (
                            float(position.x()), float(position.y())
                        )
                    self._tap_swallow_release = True
                    event.accept()
                    return True
            elif event_type == QEvent.MouseMove and self._tap_pan_active:
                if self._apply_tap_pan_move(event.position()):
                    event.accept()
                    return True
            elif event_type == QEvent.MouseButtonRelease:
                if event.button() == Qt.LeftButton and self._tap_swallow_release:
                    self._tap_swallow_release = False
                    event.accept()
                    return True
            return False
        # This filter is also installed on QCoreApplication so wheel input is
        # still owned when an active left-button pan changes Qt's receiver or
        # mouse-grab path.  Never intercept another canvas or a settings popup.
        if not self._is_viewport_event(obj):
            return False

        # Never let a teardown-time wheel reach VTK's native Render() path.
        if self._teardown_in_progress():
            try:
                event.accept()
            except Exception:
                pass
            return True

        try:
            delta = float(event.angleDelta().y())
            if delta == 0.0:
                delta = float(event.pixelDelta().y())
        except Exception:
            return False

        display_position = None
        try:
            vtk_widget = self.vtk_widget
            render_window = vtk_widget.GetRenderWindow() if vtk_widget is not None else None
            render_size = render_window.GetSize() if render_window is not None else None
            position = event.position()
            display_position = qt_position_to_vtk_display(
                position.x(),
                position.y(),
                obj.width(),
                obj.height(),
                render_size[0],
                render_size[1],
            )
        except Exception:
            display_position = None

        if delta == 0.0 or not self.handle_wheel_delta(
            delta,
            display_position=display_position,
        ):
            return False

        # Display Mode is deliberately never "always on top" (a click on the
        # main/section window should naturally bring it forward), but this
        # section/cut viewport is its own separate top-level window when
        # undocked - scrolling in it (not just clicking) also activates that
        # window, which silently buries Display Mode behind it. Re-raise
        # only, never activateWindow(): this keeps Display Mode visually on
        # top without stealing focus back from the view being scrolled, and
        # a later click on this window still brings it forward exactly as
        # before, so nothing about the existing click-to-front design changes.
        try:
            app = self._app_ref()
            dlg = getattr(app, "display_mode_dialog", None) if app is not None else None
            if dlg is not None and dlg.isVisible():
                dlg.raise_()
        except Exception:
            pass

        try:
            event.accept()
        except Exception:
            pass
        return True


def install_section_wheel_zoom(app, vtk_widget):
    """Install the shared filter once and keep legacy VTK handlers as fallback."""
    if app is None or vtk_widget is None:
        return None
    try:
        qt_interactor = getattr(vtk_widget, "interactor", None)
        if qt_interactor is None or not _qt_object_is_valid(qt_interactor):
            return None

        existing = getattr(vtk_widget, "_naksha_section_wheel_filter", None)
        if existing is not None:
            try:
                if _qt_object_is_valid(existing) and existing.parent() is qt_interactor:
                    return existing
            except Exception:
                pass

        wheel_filter = SectionWheelZoomEventFilter(app, vtk_widget, qt_interactor)
        # Application-level delivery runs before receiver-local filters.  It
        # closes the left-pan mouse-grab gap while _is_viewport_event keeps the
        # hook strictly scoped to this cross/cut-section viewport.  Returning
        # True there also prevents the same event reaching this local install,
        # preserving exactly one camera update per physical wheel event.
        qt_app = QCoreApplication.instance()
        if qt_app is not None:
            qt_app.installEventFilter(wheel_filter)
        qt_interactor.installEventFilter(wheel_filter)
        vtk_widget._naksha_section_wheel_filter = wheel_filter
        return wheel_filter
    except (RuntimeError, AttributeError, OSError, ReferenceError):
        return None
    except Exception:
        return None
