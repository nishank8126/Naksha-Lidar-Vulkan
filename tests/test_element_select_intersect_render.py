from PySide6.QtWidgets import QApplication

from gui.element_select_tool import ElementSelectTool


class _FakeCamera:
    def __init__(self):
        self.modified_count = 0
        self.position = (1.0, 2.0, 30.0)
        self.focal_point = (1.0, 2.0, 0.0)
        self.view_up = (0.0, 1.0, 0.0)
        self.parallel_projection = 1
        self.parallel_scale = 25.0
        self.view_angle = 30.0
        self.window_center = (0.0, 0.0)
        self.clipping_range = (0.1, 1000.0)

    def Modified(self):
        self.modified_count += 1

    def GetPosition(self):
        return self.position

    def SetPosition(self, *value):
        self.position = tuple(value)

    def GetFocalPoint(self):
        return self.focal_point

    def SetFocalPoint(self, *value):
        self.focal_point = tuple(value)

    def GetViewUp(self):
        return self.view_up

    def SetViewUp(self, *value):
        self.view_up = tuple(value)

    def GetParallelProjection(self):
        return self.parallel_projection

    def SetParallelProjection(self, value):
        self.parallel_projection = int(value)

    def GetParallelScale(self):
        return self.parallel_scale

    def SetParallelScale(self, value):
        self.parallel_scale = float(value)

    def GetViewAngle(self):
        return self.view_angle

    def SetViewAngle(self, value):
        self.view_angle = float(value)

    def GetWindowCenter(self):
        return self.window_center

    def SetWindowCenter(self, *value):
        self.window_center = tuple(value)

    def GetClippingRange(self):
        return self.clipping_range

    def SetClippingRange(self, *value):
        self.clipping_range = tuple(value)


class _FakeRenderer:
    def __init__(self):
        self.modified_count = 0
        self.camera = _FakeCamera()

    def Modified(self):
        self.modified_count += 1

    def GetActiveCamera(self):
        return self.camera


class _FakeRenderWindow:
    def __init__(self):
        self.interactor = object()
        self.modified_count = 0

    def GetInteractor(self):
        return self.interactor

    def Modified(self):
        self.modified_count += 1


class _FakeWidget:
    def __init__(self):
        self.render_window = _FakeRenderWindow()
        self.render_count = 0
        self.visible = True
        self._naksha_skip_render = False
        self.update_count = 0

    def GetRenderWindow(self):
        return self.render_window

    def isVisible(self):
        return self.visible

    def render(self):
        self.render_count += 1

    def update(self):
        self.update_count += 1


class _FakeRenderManager:
    def __init__(self):
        self.force_count = 0

    def force_render(self):
        self.force_count += 1


class _FakeApp:
    def __init__(self):
        self._shutdown_in_progress = False
        self.vtk_widget = _FakeWidget()
        self.gpu_render_manager = _FakeRenderManager()


class _FakeDigitizer:
    def __init__(self):
        self.renderer = _FakeRenderer()
        self.overlay_renderer = _FakeRenderer()
        self.interactor = object()


class _FakeObserverCommand:
    def __init__(self):
        self.abort_count = 0

    def AbortFlagOn(self):
        self.abort_count += 1


class _FakeActor:
    def __init__(self, visible):
        self.visible = bool(visible)

    def GetVisibility(self):
        return int(self.visible)

    def SetVisibility(self, visible):
        self.visible = bool(visible)

    def VisibilityOn(self):
        self.visible = True

    def VisibilityOff(self):
        self.visible = False


class _FakeKeyInteractor:
    def __init__(self, key="y", ctrl=True, shift=False):
        self.key = key
        self.ctrl = ctrl
        self.shift = shift
        self.commands = {}

    def GetKeySym(self):
        return self.key

    def GetControlKey(self):
        return int(self.ctrl)

    def GetShiftKey(self):
        return int(self.shift)

    def GetCommand(self, observer_id):
        return self.commands.get(observer_id)


def _make_tool():
    qt_app = QApplication.instance() or QApplication([])
    app = _FakeApp()
    digitizer = _FakeDigitizer()
    tool = ElementSelectTool(app, digitizer)
    tool._test_qt_app = qt_app
    tool._active = True
    tool._intersect_mode = True
    return tool, app, digitizer


def test_history_render_waits_for_owned_qt_timer_and_coalesces():
    tool, app, digitizer = _make_tool()

    tool._schedule_intersect_history_render()
    tool._schedule_intersect_history_render()

    assert tool._intersect_history_render_timer.isActive()
    assert app.gpu_render_manager.force_count == 0
    assert digitizer.renderer.modified_count == 0
    assert digitizer.overlay_renderer.modified_count == 0

    tool._intersect_history_render_timer.stop()
    tool._flush_intersect_history_render()

    assert app.gpu_render_manager.force_count == 1
    assert digitizer.renderer.modified_count == 1
    assert digitizer.overlay_renderer.modified_count == 1
    assert digitizer.renderer.camera.modified_count == 1
    assert app.vtk_widget.render_window.modified_count == 1
    assert app.vtk_widget.update_count == 1
    assert not tool._intersect_history_render_pending


def test_pending_history_render_is_dropped_during_shutdown():
    tool, app, digitizer = _make_tool()

    tool._schedule_intersect_history_render()
    app._shutdown_in_progress = True
    tool._intersect_history_render_timer.stop()
    tool._flush_intersect_history_render()

    assert app.gpu_render_manager.force_count == 0
    assert digitizer.renderer.modified_count == 0
    assert digitizer.overlay_renderer.modified_count == 0
    assert not tool._intersect_history_render_pending


def test_history_render_restores_camera_if_shortcut_handler_moves_it():
    tool, _app, digitizer = _make_tool()
    original_position = digitizer.renderer.camera.position
    original_scale = digitizer.renderer.camera.parallel_scale

    tool._schedule_intersect_history_render()
    digitizer.renderer.camera.position = (1000.0, 2000.0, 3000.0)
    digitizer.renderer.camera.parallel_scale = 999999.0

    tool._intersect_history_render_timer.stop()
    tool._flush_intersect_history_render()

    assert digitizer.renderer.camera.position == original_position
    assert digitizer.renderer.camera.parallel_scale == original_scale


def test_redo_updates_one_actor_and_schedules_complete_frame():
    tool, _app, _digitizer = _make_tool()
    tool._intersect_parts = [{"deleted": False}]
    tool._intersect_part_actors = [object()]
    tool._intersect_delete_history = []
    tool._intersect_redo_history = [0]

    calls = []
    tool._remove_intersect_actor = lambda actor: calls.append(("remove", actor))
    tool._rebuild_intersect_actor_colors = lambda: calls.append(("rebuild", None))
    tool._schedule_intersect_history_render = lambda: calls.append(("schedule", None))
    tool._status = lambda *_args, **_kwargs: None

    tool._intersect_redo_last_delete()

    assert tool._intersect_parts[0]["deleted"] is True
    assert tool._intersect_delete_history == [0]
    assert tool._intersect_redo_history == []
    assert ("rebuild", None) not in calls
    assert calls[-1] == ("schedule", None)


def test_undo_restores_one_actor_without_rebuilding_overlay():
    tool, _app, _digitizer = _make_tool()
    tool._intersect_parts = [
        {
            "deleted": True,
            "coords": [(0.0, 0.0, 0.0), (1.0, 1.0, 0.0)],
            "orig_color": (0.2, 0.4, 0.6),
            "orig_width": 3.0,
        }
    ]
    tool._intersect_part_actors = [None]
    tool._intersect_delete_history = [0]
    tool._intersect_redo_history = []

    restored_actor = object()
    calls = []
    tool._remove_intersect_actor = lambda actor: calls.append(("remove", actor))
    tool._make_world_polyline = lambda *_args, **_kwargs: restored_actor
    tool._add_intersect_actor = lambda actor: calls.append(("add", actor))
    tool._rebuild_intersect_actor_colors = lambda: calls.append(("rebuild", None))
    tool._schedule_intersect_history_render = lambda: calls.append(("schedule", None))
    tool._status = lambda *_args, **_kwargs: None

    tool._intersect_undo_last_delete()

    assert tool._intersect_parts[0]["deleted"] is False
    assert tool._intersect_delete_history == []
    assert tool._intersect_redo_history == [0]
    assert tool._intersect_part_actors == [restored_actor]
    assert ("rebuild", None) not in calls
    assert calls[-2:] == [("add", restored_actor), ("schedule", None)]


def test_vtk_fallback_aborts_keypress_and_separate_char_event():
    tool, _app, _digitizer = _make_tool()
    interactor = _FakeKeyInteractor()
    key_command = _FakeObserverCommand()
    char_command = _FakeObserverCommand()
    interactor.commands = {11: key_command, 12: char_command}
    tool.interactor = interactor
    tool._key_press_observer_id = 11
    tool._char_observer_id = 12

    redo_calls = []
    tool._intersect_redo_last_delete = lambda: redo_calls.append(True)

    tool._on_key_press(interactor, "KeyPressEvent")
    tool._on_char(interactor, "CharEvent")

    assert redo_calls == [True]
    assert key_command.abort_count == 1
    assert char_command.abort_count == 1


def test_regular_render_marks_base_and_overlay_layers():
    tool, app, digitizer = _make_tool()

    tool._render()

    assert digitizer.renderer.modified_count == 1
    assert digitizer.overlay_renderer.modified_count == 1
    assert app.vtk_widget.render_count == 1


def test_intersect_cancel_restores_exact_actor_visibility():
    tool, _app, _digitizer = _make_tool()
    line = _FakeActor(True)
    hidden_vertex = _FakeActor(False)
    visible_arrow = _FakeActor(True)
    drawing = {
        "actor": line,
        "vertex_markers": [hidden_vertex],
        "arrow_actor": visible_arrow,
    }

    tool._intersect_hidden_drawings = []
    tool._intersect_visibility_states = []
    tool._hide_intersect_source_drawing(drawing)

    assert not line.visible
    assert not hidden_vertex.visible
    assert not visible_arrow.visible

    # Simulate stale renderer state immediately before cancellation.  Restore
    # must use the captured values, not turn every related actor on.
    hidden_vertex.VisibilityOn()
    tool._restore_intersect_hidden_drawings()

    assert line.visible
    assert not hidden_vertex.visible
    assert visible_arrow.visible


def test_tool_switch_finishes_intersect_before_generic_cleanup():
    tool, _app, _digitizer = _make_tool()
    calls = []

    def finish_intersect():
        calls.append("finish")
        tool._intersect_mode = False

    tool.exit_intersect_mode = finish_intersect
    tool._cancel_intersect_history_render = lambda: calls.append("cancel-render")
    tool._cancel_in_progress_pick = lambda: calls.append("cleanup")
    tool._clear_hover = lambda: None
    tool._reset_hover_pick_state = lambda: None
    tool._reset_pick_caches = lambda: None
    tool._uninstall_observers = lambda: None
    tool._status = lambda *_args, **_kwargs: None

    tool.deactivate()

    assert calls[:3] == ["finish", "cancel-render", "cleanup"]
