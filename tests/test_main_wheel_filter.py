from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent, QWheelEvent
import pytest

from gui.app_window import MainWheelZoomEventFilter, NakshaApp


class _RenderWindow:
    def GetSize(self):
        return (400, 200)


class _VTKWidget:
    def GetRenderWindow(self):
        return _RenderWindow()


class _Canvas:
    def width(self):
        return 200

    def height(self):
        return 100


class _App(QObject):
    def __init__(self):
        super().__init__()
        self._shutdown_in_progress = False
        self.is_3d_mode = False
        self.panning_button = "scroll"
        self.vtk_widget = _VTKWidget()
        self.calls = []
        self.pan_calls = []
        self._qt_main_pan_active = False
        self.repair_result = False
        self.repair_calls = 0

    def _handle_fast_main_wheel(self, delta, *, display_position=None):
        self.calls.append((delta, display_position))
        return True

    def _handle_fast_main_pan_press(self, position, width, height):
        self._qt_main_pan_active = True
        self.pan_calls.append(("press", position.x(), position.y(), width, height))
        return True

    def _handle_fast_main_pan_move(
        self, position, width, height, *, middle_down=True
    ):
        self.pan_calls.append(
            ("move", position.x(), position.y(), width, height, middle_down)
        )
        if not middle_down:
            self._handle_fast_main_pan_release()
        return True

    def _handle_fast_main_pan_release(self):
        if not self._qt_main_pan_active:
            return False
        self._qt_main_pan_active = False
        self.pan_calls.append(("release",))
        return True

    def _repair_stale_main_interactor_drag(self):
        self.repair_calls += 1
        return self.repair_result


class _ObserverInteractor:
    def __init__(self):
        self.observers = {}
        self.invoked = []

    def AddObserver(self, event_name, callback, _priority):
        self.observers[event_name] = callback
        return len(self.observers)

    def InvokeEvent(self, event_name):
        self.invoked.append(event_name)


class _SwapperApp:
    _setup_interactor_swapper = NakshaApp._setup_interactor_swapper

    def __init__(self):
        self.panning_button = "left"
        self.active_classify_tool = None
        self.cross_section_active = False
        self.digitizer = None
        self.measurement_tool = None
        self.identification_tool = None
        self.point_sync_tool = None
        self.snt_layer_pick_tool = None


class _EscTool:
    def __init__(self, active=True):
        self.active = active
        self.deactivate_calls = 0

    def deactivate(self):
        self.deactivate_calls += 1
        self.active = False


class _EscapeApp:
    _deactivate_active_identification_tools_for_escape = (
        NakshaApp._deactivate_active_identification_tools_for_escape
    )

    def __init__(self):
        self.identification_tool = _EscTool()
        self.point_sync_tool = _EscTool()
        self.snt_layer_pick_tool = _EscTool()
        self.ribbon_manager = None


def _wheel_event(delta=120):
    return QWheelEvent(
        QPointF(50.0, 25.0),
        QPointF(50.0, 25.0),
        QPoint(0, 0),
        QPoint(0, delta),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.ScrollUpdate,
        False,
    )


def _mouse_event(event_type, x, y, *, button, buttons):
    return QMouseEvent(
        event_type,
        QPointF(float(x), float(y)),
        QPointF(float(x), float(y)),
        button,
        buttons,
        Qt.NoModifier,
    )


def test_main_filter_consumes_2d_wheel_once_with_exact_vtk_position():
    app = _App()
    wheel_filter = MainWheelZoomEventFilter(app)

    assert wheel_filter.eventFilter(_Canvas(), _wheel_event())
    assert app.calls == [(120.0, (100.0, 150.0))]


def test_main_filter_preserves_native_3d_wheel_path():
    app = _App()
    app.is_3d_mode = True
    wheel_filter = MainWheelZoomEventFilter(app)

    assert not wheel_filter.eventFilter(_Canvas(), _wheel_event())
    assert app.calls == []


def test_section_swapper_never_converts_physical_middle_pan_to_left():
    app = _SwapperApp()
    interactor = _ObserverInteractor()
    app._setup_interactor_swapper(
        interactor,
        preserve_physical_middle_pan=True,
    )

    interactor.observers["MiddleButtonPressEvent"](interactor, "event")
    interactor.observers["MiddleButtonReleaseEvent"](interactor, "event")

    assert interactor.invoked == []


def test_main_swapper_retains_configured_middle_to_left_mapping():
    app = _SwapperApp()
    interactor = _ObserverInteractor()
    app._setup_interactor_swapper(interactor)

    interactor.observers["MiddleButtonPressEvent"](interactor, "event")
    interactor.observers["MiddleButtonReleaseEvent"](interactor, "event")

    assert interactor.invoked == [
        "LeftButtonPressEvent",
        "LeftButtonReleaseEvent",
    ]


def test_shutdown_wheel_is_dropped_before_vtk_access():
    app = _App()
    app._shutdown_in_progress = True
    wheel_filter = MainWheelZoomEventFilter(app)

    assert wheel_filter.eventFilter(_Canvas(), _wheel_event())
    assert app.calls == []


def test_main_filter_owns_one_complete_middle_pan_before_vtk():
    app = _App()
    canvas = _Canvas()
    navigation_filter = MainWheelZoomEventFilter(app)

    assert navigation_filter.eventFilter(
        canvas,
        _mouse_event(
            QEvent.MouseButtonPress,
            20,
            30,
            button=Qt.MiddleButton,
            buttons=Qt.MiddleButton,
        ),
    )
    assert navigation_filter.eventFilter(
        canvas,
        _mouse_event(
            QEvent.MouseMove,
            35,
            42,
            button=Qt.NoButton,
            buttons=Qt.MiddleButton,
        ),
    )
    assert navigation_filter.eventFilter(
        canvas,
        _mouse_event(
            QEvent.MouseButtonRelease,
            35,
            42,
            button=Qt.MiddleButton,
            buttons=Qt.NoButton,
        ),
    )

    assert app.pan_calls == [
        ("press", 20.0, 30.0, 200, 100),
        ("move", 35.0, 42.0, 200, 100, True),
        ("release",),
    ]


def test_configured_left_pan_is_owned_before_vtk_button_swapper():
    app = _App()
    app.panning_button = "left"
    app._left_pan_shortcut_active = True
    canvas = _Canvas()
    navigation_filter = MainWheelZoomEventFilter(app)

    assert navigation_filter.eventFilter(
        canvas,
        _mouse_event(
            QEvent.MouseButtonPress,
            20,
            30,
            button=Qt.LeftButton,
            buttons=Qt.LeftButton,
        ),
    )
    assert navigation_filter.eventFilter(
        canvas,
        _mouse_event(
            QEvent.MouseMove,
            35,
            42,
            button=Qt.NoButton,
            buttons=Qt.LeftButton,
        ),
    )
    assert navigation_filter.eventFilter(
        canvas,
        _mouse_event(
            QEvent.MouseButtonRelease,
            35,
            42,
            button=Qt.LeftButton,
            buttons=Qt.NoButton,
        ),
    )

    assert app.pan_calls == [
        ("press", 20.0, 30.0, 200, 100),
        ("move", 35.0, 42.0, 200, 100, True),
        ("release",),
    ]
    assert not app._qt_main_pan_active


def test_configured_left_pan_stays_off_until_pan_shortcut_is_held():
    app = _App()
    app.panning_button = "left"
    app._left_pan_shortcut_active = False
    canvas = _Canvas()
    navigation_filter = MainWheelZoomEventFilter(app)

    assert not navigation_filter.eventFilter(
        canvas,
        _mouse_event(
            QEvent.MouseButtonPress,
            20,
            30,
            button=Qt.LeftButton,
            buttons=Qt.LeftButton,
        ),
    )
    assert app.pan_calls == []


def test_middle_pan_remains_available_when_left_pan_is_configured():
    app = _App()
    app.panning_button = "left"
    canvas = _Canvas()
    navigation_filter = MainWheelZoomEventFilter(app)

    assert navigation_filter.eventFilter(
        canvas,
        _mouse_event(
            QEvent.MouseButtonPress,
            12,
            18,
            button=Qt.MiddleButton,
            buttons=Qt.MiddleButton,
        ),
    )
    assert navigation_filter.eventFilter(
        canvas,
        _mouse_event(
            QEvent.MouseMove,
            32,
            38,
            button=Qt.NoButton,
            buttons=Qt.MiddleButton,
        ),
    )
    assert navigation_filter.eventFilter(
        canvas,
        _mouse_event(
            QEvent.MouseButtonRelease,
            32,
            38,
            button=Qt.MiddleButton,
            buttons=Qt.NoButton,
        ),
    )

    assert app.pan_calls == [
        ("press", 12.0, 18.0, 200, 100),
        ("move", 32.0, 38.0, 200, 100, True),
        ("release",),
    ]
    assert navigation_filter._owned_main_pan_button is None


def test_configured_left_pan_does_not_steal_digitizer_left_click():
    app = _App()
    app.panning_button = "left"
    app._left_pan_shortcut_active = True
    app.digitizer = type("DigitizerState", (), {"active_tool": "polyline"})()
    navigation_filter = MainWheelZoomEventFilter(app)

    assert not navigation_filter.eventFilter(
        _Canvas(),
        _mouse_event(
            QEvent.MouseButtonPress,
            20,
            30,
            button=Qt.LeftButton,
            buttons=Qt.LeftButton,
        ),
    )
    assert app.pan_calls == []


@pytest.mark.parametrize(
    "tool_name",
    ("identification_tool", "point_sync_tool", "snt_layer_pick_tool"),
)
def test_configured_left_pan_does_not_steal_identification_click(tool_name):
    app = _App()
    app.panning_button = "left"
    app._left_pan_shortcut_active = True
    setattr(app, tool_name, type("ActiveTool", (), {"active": True})())
    navigation_filter = MainWheelZoomEventFilter(app)

    assert not navigation_filter.eventFilter(
        _Canvas(),
        _mouse_event(
            QEvent.MouseButtonPress,
            20,
            30,
            button=Qt.LeftButton,
            buttons=Qt.LeftButton,
        ),
    )
    assert app.pan_calls == []


@pytest.mark.parametrize(
    "tool_name",
    ("identification_tool", "point_sync_tool", "snt_layer_pick_tool"),
)
def test_main_swapper_preserves_identification_left_click(tool_name):
    app = _SwapperApp()
    setattr(app, tool_name, type("ActiveTool", (), {"active": True})())
    interactor = _ObserverInteractor()
    app._setup_interactor_swapper(interactor)

    interactor.observers["LeftButtonPressEvent"](interactor, "event")
    interactor.observers["LeftButtonReleaseEvent"](interactor, "event")

    assert interactor.invoked == []


def test_escape_deactivates_every_active_identification_mode_once():
    app = _EscapeApp()

    assert app._deactivate_active_identification_tools_for_escape()
    for tool_name in (
        "identification_tool",
        "point_sync_tool",
        "snt_layer_pick_tool",
    ):
        tool = getattr(app, tool_name)
        assert not tool.active
        assert tool.deactivate_calls == 1

    assert not app._deactivate_active_identification_tools_for_escape()


def test_main_canvas_escape_deactivates_point_sync_before_vtk_consumes_key():
    app = _App()
    point_sync = _EscTool()
    app.point_sync_tool = point_sync
    app._deactivate_active_identification_tools_for_escape = lambda: (
        point_sync.deactivate() or True
    ) if point_sync.active else False
    navigation_filter = MainWheelZoomEventFilter(app)
    event = QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier)

    assert navigation_filter.eventFilter(_Canvas(), event)
    assert not point_sync.active
    assert point_sync.deactivate_calls == 1


def test_main_canvas_escape_is_preserved_when_identification_is_inactive():
    app = _App()
    app._deactivate_active_identification_tools_for_escape = lambda: False
    navigation_filter = MainWheelZoomEventFilter(app)
    event = QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier)

    assert not navigation_filter.eventFilter(_Canvas(), event)


def test_queued_move_without_middle_finishes_and_later_release_is_swallowed():
    app = _App()
    canvas = _Canvas()
    navigation_filter = MainWheelZoomEventFilter(app)

    assert navigation_filter.eventFilter(
        canvas,
        _mouse_event(
            QEvent.MouseButtonPress,
            20,
            30,
            button=Qt.MiddleButton,
            buttons=Qt.MiddleButton,
        ),
    )
    assert navigation_filter.eventFilter(
        canvas,
        _mouse_event(
            QEvent.MouseMove,
            50,
            60,
            button=Qt.NoButton,
            buttons=Qt.NoButton,
        ),
    )
    assert not app._qt_main_pan_active

    # Qt can deliver the physical release after the no-button move. It still
    # belongs to this gesture and must not become an unmatched VTK release.
    assert navigation_filter.eventFilter(
        canvas,
        _mouse_event(
            QEvent.MouseButtonRelease,
            50,
            60,
            button=Qt.MiddleButton,
            buttons=Qt.NoButton,
        ),
    )
    assert app.pan_calls.count(("release",)) == 1


def test_main_filter_preserves_classification_pan_path():
    app = _App()
    app.active_classify_tool = "polygon"
    navigation_filter = MainWheelZoomEventFilter(app)

    assert not navigation_filter.eventFilter(
        _Canvas(),
        _mouse_event(
            QEvent.MouseButtonPress,
            20,
            30,
            button=Qt.MiddleButton,
            buttons=Qt.MiddleButton,
        ),
    )
    assert app.pan_calls == []


def test_no_button_hover_consumes_one_leaked_vtk_drag_state():
    app = _App()
    app.repair_result = True
    navigation_filter = MainWheelZoomEventFilter(app)

    assert navigation_filter.eventFilter(
        _Canvas(),
        _mouse_event(
            QEvent.MouseMove,
            20,
            30,
            button=Qt.NoButton,
            buttons=Qt.NoButton,
        ),
    )
    assert app.repair_calls == 1
    assert app.pan_calls == []


class _Camera:
    def __init__(self):
        self.position = (0.0, 0.0, 10.0)
        self.focal = (0.0, 0.0, 0.0)

    def GetPosition(self):
        return self.position

    def GetFocalPoint(self):
        return self.focal

    def SetPosition(self, *value):
        self.position = tuple(value)

    def SetFocalPoint(self, *value):
        self.focal = tuple(value)


class _Renderer:
    def __init__(self):
        self.camera = _Camera()
        self.clipping_resets = 0

    def GetActiveCamera(self):
        return self.camera

    def ResetCameraClippingRange(self):
        self.clipping_resets += 1


class _PanRenderWindow:
    def __init__(self):
        self.update_rates = []

    def SetDesiredUpdateRate(self, rate):
        self.update_rates.append(rate)


class _PanWidget:
    def __init__(self):
        self.renderer = _Renderer()
        self.render_window = _PanRenderWindow()
        self.render_count = 0

    def GetRenderWindow(self):
        return self.render_window

    def render(self):
        self.render_count += 1


class _PanManager:
    def __init__(self):
        self.begin_counts = []
        self.requests = 0
        self.finishes = 0

    def begin_pan_interaction(self, *, point_count=0):
        self.begin_counts.append(point_count)
        return True

    def request_render(self, widget):
        self.requests += 1

    def finish_pan_interaction(self):
        self.finishes += 1
        return True


class _MainPanApp:
    _set_digitizer_qt_pan_guard = NakshaApp._set_digitizer_qt_pan_guard
    _handle_fast_main_pan_press = NakshaApp._handle_fast_main_pan_press
    _handle_fast_main_pan_move = NakshaApp._handle_fast_main_pan_move
    _handle_fast_main_pan_release = NakshaApp._handle_fast_main_pan_release

    def __init__(self):
        self._shutdown_in_progress = False
        self.is_3d_mode = False
        self.active_classify_tool = None
        self.zoom_rectangle_tool = None
        self.vtk_widget = _PanWidget()
        self.gpu_render_manager = _PanManager()
        self.data = {"xyz": range(10)}
        self.history = []

    def _main_pan_display_position(self, position, width, height):
        return (position.x(), position.y())

    def _display_to_world_on_focal_plane(self, renderer, x, y):
        return (float(x), float(y), 0.0)

    def _cancel_smooth_zoom_for_pan(self):
        return False

    def _schedule_main_view_history_commit(self, reason, delay_ms):
        self.history.append((reason, delay_ms))


def test_qt_pan_handler_applies_each_delta_once_and_settles_once():
    app = _MainPanApp()

    assert app._handle_fast_main_pan_press(QPointF(20, 30), 200, 100)
    assert app._handle_fast_main_pan_move(
        QPointF(35, 45), 200, 100, middle_down=True
    )
    camera = app.vtk_widget.renderer.camera
    assert camera.position == (-15.0, -15.0, 10.0)
    assert camera.focal == (-15.0, -15.0, 0.0)
    assert app.gpu_render_manager.requests == 1

    # A stale queued move whose button state says "up" ends the gesture but
    # cannot mutate the camera a second time.
    assert app._handle_fast_main_pan_move(
        QPointF(120, 90), 200, 100, middle_down=False
    )
    assert camera.position == (-15.0, -15.0, 10.0)
    assert app.gpu_render_manager.finishes == 1
    assert app.vtk_widget.renderer.clipping_resets == 1
    assert app.history == [("pan_or_view_change", 80)]
