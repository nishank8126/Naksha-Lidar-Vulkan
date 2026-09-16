from types import MethodType, SimpleNamespace

import pytest
import vtk

from gui.app_window import NakshaApp
from gui.gpu_render_manager import GPURenderManager
from gui.global_shortcuts import GlobalShortcutFilter
from gui.views import set_view


class _Widget:
    def __init__(self):
        self.renderer = vtk.vtkRenderer()
        self.interactor = vtk.vtkRenderWindowInteractor()
        self.interactor.SetInteractorStyle(vtk.vtkInteractorStyleImage())
        self.camera = self.renderer.GetActiveCamera()
        self.render_count = 0

    def render(self):
        self.render_count += 1

    def view_xy(self):
        self.camera.SetFocalPoint(0.0, 0.0, 0.0)
        self.camera.SetPosition(0.0, 0.0, 1.0)
        self.camera.SetViewUp(0.0, 1.0, 0.0)

    def view_xz(self):
        self.camera.SetFocalPoint(0.0, 0.0, 0.0)
        self.camera.SetPosition(0.0, -1.0, 0.0)
        self.camera.SetViewUp(0.0, 0.0, 1.0)

    def view_yz(self):
        self.camera.SetFocalPoint(0.0, 0.0, 0.0)
        self.camera.SetPosition(1.0, 0.0, 0.0)
        self.camera.SetViewUp(0.0, 0.0, 1.0)

    def isometric_view(self):
        self.camera.SetPosition(1.0, -1.0, 1.0)


class _StatusBar:
    def showMessage(self, *_args):
        pass


def _policy_app(current_view="top"):
    app = SimpleNamespace(
        vtk_widget=_Widget(),
        current_view=current_view,
        is_3d_mode=False,
        _main_view_2d_locked=True,
        _main_view_3d_user_enabled=False,
        active_classify_tool=None,
        cross_interactor=None,
        measurement_tool=None,
        digitizer=None,
    )
    for name in (
        "_normalize_main_view_2d_camera",
        "_main_tool_owns_interactor_style",
        "_repair_main_view_2d_style",
        "_install_main_view_2d_policy_guard",
        "_refresh_main_view_clipping_after_navigation",
    ):
        setattr(app, name, MethodType(getattr(NakshaApp, name), app))
    return app


def test_stale_current_view_cannot_authorize_automatic_3d():
    calls = []
    app = SimpleNamespace(
        current_view="3d",
        _main_view_3d_user_enabled=False,
        ensure_main_view_2d_interaction=lambda **kwargs: calls.append(kwargs),
    )

    set_view(app, "3d")

    assert app.current_view == "3d"
    assert calls == [{
        "preserve_camera": True,
        "reason": "blocked_automatic_3d_view",
    }]


def test_explicit_authorization_allows_3d_view():
    app = _policy_app()
    app._main_view_3d_user_enabled = True

    set_view(app, "3d")

    camera = app.vtk_widget.renderer.GetActiveCamera()
    assert app.current_view == "3d"
    assert not camera.GetParallelProjection()
    assert (
        app.vtk_widget.interactor.GetInteractorStyle().GetClassName()
        == "vtkInteractorStyleTrackballCamera"
    )


def test_plan_policy_flattens_tilt_without_losing_framing():
    app = _policy_app()
    camera = app.vtk_widget.renderer.GetActiveCamera()
    camera.SetFocalPoint(100.0, 200.0, 30.0)
    camera.SetPosition(130.0, 160.0, 80.0)
    camera.SetViewUp(1.0, 0.0, 0.0)
    camera.SetParallelProjection(False)
    camera.SetParallelScale(42.0)
    old_distance = camera.GetDistance()

    assert app._normalize_main_view_2d_camera(camera) is True

    assert camera.GetParallelProjection()
    assert camera.GetFocalPoint() == pytest.approx((100.0, 200.0, 30.0))
    assert camera.GetPosition() == pytest.approx((100.0, 200.0, 30.0 + old_distance))
    assert camera.GetViewUp() == pytest.approx((0.0, 1.0, 0.0))
    assert camera.GetParallelScale() == pytest.approx(42.0)


def test_policy_observer_repairs_loader_camera_mutation():
    app = _policy_app()
    camera = app.vtk_widget.renderer.GetActiveCamera()
    app._install_main_view_2d_policy_guard()

    camera.ParallelProjectionOff()
    camera.SetPosition(10.0, -20.0, 30.0)
    camera.SetViewUp(1.0, 0.0, 0.0)

    focal = camera.GetFocalPoint()
    position = camera.GetPosition()
    assert camera.GetParallelProjection()
    assert position[0] == pytest.approx(focal[0])
    assert position[1] == pytest.approx(focal[1])
    assert position[2] > focal[2]
    assert camera.GetViewUp() == pytest.approx((0.0, 1.0, 0.0))


def test_policy_keeps_cross_section_tool_style_but_blocks_camera_rotation():
    app = _policy_app()
    cross_style = vtk.vtkInteractorStyleTrackballCamera()
    app.cross_interactor = cross_style
    app.vtk_widget.interactor.SetInteractorStyle(cross_style)
    camera = app.vtk_widget.renderer.GetActiveCamera()
    app._install_main_view_2d_policy_guard()

    camera.SetPosition(20.0, -30.0, 40.0)
    camera.SetViewUp(1.0, 0.0, 0.0)
    app._repair_main_view_2d_style()

    assert app.vtk_widget.interactor.GetInteractorStyle() is cross_style
    assert camera.GetPosition()[:2] == pytest.approx(camera.GetFocalPoint()[:2])
    assert camera.GetViewUp() == pytest.approx((0.0, 1.0, 0.0))


def test_toggle_to_2d_clears_stale_3d_state():
    app = _policy_app(current_view="3d")
    app._main_view_3d_user_enabled = True
    app.is_3d_mode = True
    app._main_view_2d_locked = False
    app.section_vtks = {}
    app.statusBar = lambda: _StatusBar()

    NakshaApp.toggle_view_mode(app, "2d")

    camera = app.vtk_widget.renderer.GetActiveCamera()
    assert app.current_view == "top"
    assert app._main_view_3d_user_enabled is False
    assert app.is_3d_mode is False
    assert app._main_view_2d_locked is True
    assert camera.GetParallelProjection()
    assert camera.GetPosition() == pytest.approx((0.0, 0.0, 1.0))
    assert (
        app.vtk_widget.interactor.GetInteractorStyle().GetClassName()
        == "vtkInteractorStyleImage"
    )


def test_programmatic_toggle_to_3d_is_blocked():
    calls = []
    app = SimpleNamespace(
        section_vtks={},
        _main_view_3d_user_enabled=False,
        ensure_main_view_2d_interaction=lambda **kwargs: calls.append(kwargs),
    )

    assert NakshaApp.toggle_view_mode(app, "3d") is False
    assert calls == [{
        "preserve_camera": True,
        "reason": "blocked_toggle_view_mode_3d",
    }]


def test_camera_restore_cannot_restore_perspective_without_user_authority():
    app = _policy_app()
    app._restore_main_camera = MethodType(NakshaApp._restore_main_camera, app)

    app._restore_main_camera({
        "pos": (30.0, -40.0, 80.0),
        "fp": (10.0, 20.0, 5.0),
        "up": (1.0, 0.0, 0.0),
        "ps": 19.0,
        "pp": 0,
        "va": 25.0,
    })

    camera = app.vtk_widget.renderer.GetActiveCamera()
    assert camera.GetParallelProjection()
    assert camera.GetFocalPoint() == pytest.approx((10.0, 20.0, 5.0))
    assert camera.GetPosition()[:2] == pytest.approx((10.0, 20.0))
    assert camera.GetViewUp() == pytest.approx((0.0, 1.0, 0.0))


def test_shift_p_main_unlock_grants_3d_authority():
    app = _policy_app()
    shortcut_filter = SimpleNamespace(
        app_window=app,
        _clear_main_camera_lock_observer=lambda _camera: None,
    )

    GlobalShortcutFilter._unlock_main_view(shortcut_filter)

    camera = app.vtk_widget.renderer.GetActiveCamera()
    assert app._main_view_3d_user_enabled is True
    assert app.current_view == "3d"
    assert app.is_3d_mode is True
    assert app._main_view_2d_locked is False
    assert not camera.GetParallelProjection()
    assert (
        app.vtk_widget.interactor.GetInteractorStyle().GetClassName()
        == "vtkInteractorStyleTrackballCamera"
    )


def test_shift_p_cross_unlock_does_not_unlock_main_view():
    app = _policy_app()
    app._cross_section_2d_mode = {0: {}}
    shortcut_filter = SimpleNamespace(
        app_window=app,
        _clear_cross_view_camera_lock_observer=lambda _view_idx: None,
    )
    cross_widget = _Widget()

    GlobalShortcutFilter._unlock_cross_section_view(
        shortcut_filter, 0, cross_widget
    )

    assert app._main_view_3d_user_enabled is False
    assert app.current_view == "top"
    assert not cross_widget.renderer.GetActiveCamera().GetParallelProjection()


def test_2d_navigation_rebuilds_clip_range_for_planar_snt_actor():
    app = _policy_app()
    renderer = app.vtk_widget.renderer
    camera = renderer.GetActiveCamera()
    camera.SetFocalPoint(0.0, 0.0, 0.0)
    camera.SetPosition(0.0, 0.0, 100.0)
    camera.ParallelProjectionOn()

    source = vtk.vtkLineSource()
    source.SetPoint1(-10.0, 0.0, 0.0)
    source.SetPoint2(10.0, 0.0, 0.0)
    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputConnection(source.GetOutputPort())
    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    renderer.AddActor(actor)

    camera.SetClippingRange(0.01, 0.02)
    assert app._refresh_main_view_clipping_after_navigation(renderer)

    near_value, far_value = camera.GetClippingRange()
    assert near_value < 100.0 < far_value
    assert near_value > 0.0


def test_wheel_idle_repairs_clipping_before_final_render():
    calls = []
    app = SimpleNamespace(
        _shutdown_in_progress=False,
        _refresh_main_view_clipping_after_navigation=lambda: calls.append("clip"),
    )
    manager = SimpleNamespace(
        _interaction_active=True,
        app=app,
        _restore_full_detail=lambda: calls.append("detail"),
        force_render=lambda: calls.append("render"),
    )

    GPURenderManager._on_wheel_idle(manager)

    assert manager._interaction_active is False
    assert calls == ["detail", "clip", "render"]


def test_top_cursor_zoom_cannot_drift_along_camera_depth_axis():
    camera = SimpleNamespace(
        position=[0.0, 0.0, 100.0],
        focal=[0.0, 0.0, 0.0],
        scale=50.0,
        GetPosition=lambda: tuple(camera.position),
        GetFocalPoint=lambda: tuple(camera.focal),
        GetParallelScale=lambda: camera.scale,
        GetViewAngle=lambda: 30.0,
        GetParallelProjection=lambda: True,
        SetPosition=lambda *value: setattr(camera, "position", list(value)),
        SetFocalPoint=lambda *value: setattr(camera, "focal", list(value)),
        SetParallelScale=lambda value: setattr(camera, "scale", value),
        SetViewAngle=lambda _value: None,
        ParallelProjectionOn=lambda: None,
        ParallelProjectionOff=lambda: None,
        Zoom=lambda factor: setattr(camera, "scale", camera.scale / factor),
    )
    renderer = SimpleNamespace(GetActiveCamera=lambda: camera)
    interactor = SimpleNamespace(GetEventPosition=lambda: (10, 20))
    widget = SimpleNamespace(renderer=renderer, interactor=interactor)
    world_points = iter(((10.0, 20.0, 0.0), (8.0, 16.0, 25.0)))
    app = SimpleNamespace(
        current_view="top",
        _display_to_world_on_focal_plane=lambda *_args: next(world_points),
    )

    assert NakshaApp._zoom_widget_at_cursor(
        app, widget, 2.0, interactor=interactor, render=False
    )

    assert camera.position == pytest.approx((2.0, 4.0, 100.0))
    assert camera.focal == pytest.approx((2.0, 4.0, 0.0))
