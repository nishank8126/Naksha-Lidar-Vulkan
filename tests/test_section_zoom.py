import math
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QObject, QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent

from gui.cross_section.section_zoom import (
    MAX_WHEEL_STEPS_PER_EVENT,
    SECTION_WHEEL_ZOOM_FACTOR,
    SectionWheelZoomEventFilter,
    apply_cursor_anchored_parallel_zoom,
    install_section_wheel_zoom,
    section_wheel_zoom_factor,
)
from gui.cross_section.cut_section_controller import CutSectionInteractorStyle


class _FakeCamera:
    def __init__(self, scale=100.0):
        self.scale = float(scale)
        self.position = (0.0, -100.0, 0.0)
        self.focal_point = (0.0, 0.0, 0.0)
        self.parallel = True

    def GetPosition(self):
        return self.position

    def SetPosition(self, value):
        self.position = tuple(value)

    def GetFocalPoint(self):
        return self.focal_point

    def SetFocalPoint(self, value):
        self.focal_point = tuple(value)

    def GetParallelScale(self):
        return self.scale

    def SetParallelScale(self, value):
        self.scale = float(value)

    def GetParallelProjection(self):
        return int(self.parallel)

    def ParallelProjectionOn(self):
        self.parallel = True

    def ParallelProjectionOff(self):
        self.parallel = False


class _FakeRenderer:
    def __init__(self, camera):
        self.camera = camera
        self.clipping_resets = 0
        self._world_input = None
        self._display_input = None
        self._display_output = None
        self._world_output = None

    def GetActiveCamera(self):
        return self.camera

    def ResetCameraClippingRange(self):
        self.clipping_resets += 1

    def SetWorldPoint(self, *value):
        self._world_input = tuple(value)

    def WorldToDisplay(self):
        world_x, world_y, world_z = self._world_input[:3]
        focal = self.camera.GetFocalPoint()
        pixels_per_world = 100.0 / (2.0 * self.camera.GetParallelScale())
        self._display_output = (
            100.0 + (world_x - focal[0]) * pixels_per_world,
            50.0 + (world_y - focal[1]) * pixels_per_world,
            world_z - focal[2],
        )

    def GetDisplayPoint(self):
        return self._display_output

    def SetDisplayPoint(self, *value):
        self._display_input = tuple(value)

    def DisplayToWorld(self):
        display_x, display_y, display_z = self._display_input
        focal = self.camera.GetFocalPoint()
        world_per_pixel = (2.0 * self.camera.GetParallelScale()) / 100.0
        self._world_output = (
            focal[0] + (display_x - 100.0) * world_per_pixel,
            focal[1] + (display_y - 50.0) * world_per_pixel,
            focal[2] + display_z,
            1.0,
        )

    def GetWorldPoint(self):
        return self._world_output


class _FakeRenderWindow:
    def __init__(self, interactor):
        self.interactor = interactor

    def GetInteractor(self):
        return self.interactor

    def GetSize(self):
        return (200, 100)


class _FakeInteractor(QObject):
    def __init__(self):
        super().__init__()
        self.event_position = (100, 50)

    def width(self):
        return 200

    def height(self):
        return 100

    def GetEventPosition(self):
        return self.event_position


class _FakeWidget:
    def __init__(self, interactor, scale=100.0):
        self.interactor = interactor
        self.camera = _FakeCamera(scale)
        self.renderer = _FakeRenderer(self.camera)
        self.render_window = _FakeRenderWindow(interactor)
        self.render_count = 0
        self.visible = True
        self._naksha_skip_render = False
        self._naksha_view_finalized = False

    def GetRenderWindow(self):
        return self.render_window

    def isVisible(self):
        return self.visible

    def render(self):
        self.render_count += 1


class _FakeApp:
    def __init__(self):
        self._shutdown_in_progress = False
        self.zoom_behavior = "center"
        self.cut_section_controller = None


def _make_filter(scale=100.0):
    QCoreApplication.instance() or QCoreApplication([])
    app = _FakeApp()
    interactor = _FakeInteractor()
    widget = _FakeWidget(interactor, scale=scale)
    wheel_filter = SectionWheelZoomEventFilter(app, widget, interactor)
    # Keep these alive independently of the filter's weak references.
    wheel_filter._test_app = app
    wheel_filter._test_widget = widget
    wheel_filter._test_interactor = interactor
    return wheel_filter, app, widget


def _flush_timer(wheel_filter):
    wheel_filter._render_timer.stop()
    wheel_filter._flush_render()


def _wheel_event(delta, *, buttons=Qt.NoButton):
    return QWheelEvent(
        QPointF(10.0, 10.0),
        QPointF(10.0, 10.0),
        QPoint(0, 0),
        QPoint(0, int(delta)),
        buttons,
        Qt.NoModifier,
        Qt.ScrollUpdate,
        False,
    )


def test_standard_notch_uses_fast_reciprocal_zoom():
    assert section_wheel_zoom_factor(120) == pytest.approx(SECTION_WHEEL_ZOOM_FACTOR)
    assert section_wheel_zoom_factor(-120) == pytest.approx(1.0 / SECTION_WHEEL_ZOOM_FACTOR)


def test_raw_multi_notch_delta_is_preserved_and_bounded():
    assert section_wheel_zoom_factor(240) == pytest.approx(SECTION_WHEEL_ZOOM_FACTOR**2)
    assert section_wheel_zoom_factor(12000) == pytest.approx(
        SECTION_WHEEL_ZOOM_FACTOR**MAX_WHEEL_STEPS_PER_EVENT
    )
    assert section_wheel_zoom_factor(-12000) == pytest.approx(
        SECTION_WHEEL_ZOOM_FACTOR ** -MAX_WHEEL_STEPS_PER_EVENT
    )


def test_non_finite_delta_is_rejected_without_camera_access():
    with pytest.raises(ValueError):
        section_wheel_zoom_factor(math.inf)


def test_camera_updates_immediately_but_rapid_renders_are_coalesced():
    wheel_filter, _app, widget = _make_filter(scale=100.0)

    assert wheel_filter.handle_wheel_delta(120)
    assert widget.camera.scale == pytest.approx(100.0 / SECTION_WHEEL_ZOOM_FACTOR)
    assert wheel_filter._render_timer.isActive()
    assert widget.render_count == 0

    assert wheel_filter.handle_wheel_delta(120)
    assert widget.camera.scale == pytest.approx(
        100.0 / (SECTION_WHEEL_ZOOM_FACTOR**2)
    )
    assert widget.render_count == 0

    _flush_timer(wheel_filter)
    assert widget.render_count == 1
    # Orthographic zoom does not change scene bounds, so clipping is deferred
    # until a lightweight interaction actor is restored.
    assert widget.renderer.clipping_resets == 0


def test_qt_wheel_event_is_consumed_only_after_camera_update():
    wheel_filter, _app, widget = _make_filter(scale=100.0)
    event = _wheel_event(240)

    assert wheel_filter.eventFilter(wheel_filter.parent(), event)
    assert widget.camera.scale == pytest.approx(
        100.0 / (SECTION_WHEEL_ZOOM_FACTOR**2)
    )
    assert wheel_filter._render_timer.isActive()


def test_left_pan_button_state_cannot_block_section_wheel_zoom():
    wheel_filter, app, widget = _make_filter(scale=100.0)
    app.panning_button = "left"

    event = _wheel_event(120, buttons=Qt.LeftButton)

    assert wheel_filter.eventFilter(wheel_filter.parent(), event)
    assert widget.camera.scale == pytest.approx(100.0 / SECTION_WHEEL_ZOOM_FACTOR)


def test_application_hook_ignores_wheel_from_another_viewport():
    wheel_filter, _app, widget = _make_filter(scale=100.0)
    unrelated = QObject()

    assert not wheel_filter.eventFilter(unrelated, _wheel_event(120))
    assert widget.camera.scale == pytest.approx(100.0)


def test_qt_cursor_coordinates_anchor_the_requested_off_center_location():
    wheel_filter, app, widget = _make_filter(scale=100.0)
    app.zoom_behavior = "center"

    assert wheel_filter.eventFilter(wheel_filter.parent(), _wheel_event(120))

    # Qt (10, 10) maps to VTK display (10, 90). Both axes are off-center,
    # therefore a correctly anchored camera must translate in both axes.
    assert widget.camera.focal_point[0] < 0.0
    assert widget.camera.focal_point[1] > 0.0


def test_filter_install_is_idempotent_for_each_widget():
    _existing_filter, app, widget = _make_filter()
    first = install_section_wheel_zoom(app, widget)
    second = install_section_wheel_zoom(app, widget)

    assert first is not None
    assert second is first


def test_saved_center_setting_cannot_disable_section_cursor_anchor():
    wheel_filter, app, widget = _make_filter(scale=100.0)
    app.zoom_behavior = "center"

    def _legacy_helper_must_not_run(*args, **kwargs):
        raise AssertionError("section zoom must use its own cursor anchor")

    app._zoom_widget_at_cursor = _legacy_helper_must_not_run

    assert wheel_filter.handle_wheel_delta(120, display_position=(25.0, 50.0))
    assert widget.camera.scale == pytest.approx(100.0 / SECTION_WHEEL_ZOOM_FACTOR)
    assert widget.camera.focal_point[0] < 0.0


def test_world_point_under_section_cursor_stays_fixed_for_zoom_in_and_out():
    _wheel_filter, _app, widget = _make_filter(scale=100.0)
    cursor = (25.0, 50.0)

    def _world_at_cursor():
        renderer = widget.renderer
        camera = widget.camera
        renderer.SetWorldPoint(*camera.GetFocalPoint(), 1.0)
        renderer.WorldToDisplay()
        depth = renderer.GetDisplayPoint()[2]
        renderer.SetDisplayPoint(cursor[0], cursor[1], depth)
        renderer.DisplayToWorld()
        return renderer.GetWorldPoint()[:3]

    original_world = _world_at_cursor()
    assert apply_cursor_anchored_parallel_zoom(
        widget.renderer,
        widget.camera,
        SECTION_WHEEL_ZOOM_FACTOR,
        cursor,
    )
    assert _world_at_cursor() == pytest.approx(original_world)

    assert apply_cursor_anchored_parallel_zoom(
        widget.renderer,
        widget.camera,
        1.0 / SECTION_WHEEL_ZOOM_FACTOR,
        cursor,
    )
    assert _world_at_cursor() == pytest.approx(original_world)
    assert widget.camera.scale == pytest.approx(100.0)


def test_cut_style_fallback_uses_the_shared_cursor_anchor():
    _wheel_filter, _app, widget = _make_filter(scale=100.0)
    interactor = widget.interactor
    interactor.event_position = (25, 50)
    source = SimpleNamespace(aborted=False)
    source.AbortFlagOn = lambda: setattr(source, "aborted", True)
    style_state = SimpleNamespace(vtk_widget=widget)

    assert CutSectionInteractorStyle._apply_cursor_wheel_zoom(
        style_state,
        interactor,
        1.2,
        source,
    )
    assert widget.camera.scale == pytest.approx(100.0 / 1.2)
    assert widget.camera.focal_point[0] < 0.0
    assert widget.render_count == 1
    assert source.aborted


def test_zoom_in_then_out_restores_original_parallel_scale():
    wheel_filter, _app, widget = _make_filter(scale=73.5)

    assert wheel_filter.handle_wheel_delta(120)
    assert wheel_filter.handle_wheel_delta(-120)
    assert widget.camera.scale == pytest.approx(73.5)


def test_pending_render_is_dropped_after_view_finalization():
    wheel_filter, _app, widget = _make_filter()

    assert wheel_filter.handle_wheel_delta(120)
    widget._naksha_view_finalized = True
    _flush_timer(wheel_filter)

    assert widget.render_count == 0
    assert widget.renderer.clipping_resets == 0


def test_invalid_render_window_falls_back_without_changing_camera():
    wheel_filter, _app, widget = _make_filter(scale=50.0)
    widget.render_window.interactor = None

    assert not wheel_filter.handle_wheel_delta(120)
    assert widget.camera.scale == pytest.approx(50.0)
    assert not wheel_filter._render_timer.isActive()


def test_shutdown_drops_pending_render():
    wheel_filter, app, widget = _make_filter()

    assert wheel_filter.handle_wheel_delta(120)
    app._shutdown_in_progress = True
    _flush_timer(wheel_filter)

    assert widget.render_count == 0


def test_shutdown_time_wheel_is_consumed_without_touching_vtk():
    wheel_filter, app, widget = _make_filter(scale=42.0)
    app._shutdown_in_progress = True

    assert wheel_filter.eventFilter(wheel_filter.parent(), _wheel_event(120))
    assert widget.camera.scale == pytest.approx(42.0)
    assert not wheel_filter._render_timer.isActive()
