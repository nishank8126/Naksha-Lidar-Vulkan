from types import SimpleNamespace

from gui.cross_section.interactor_classify import ClassificationInteractor


class _EventSource:
    def __init__(self):
        self.aborted = False

    def AbortFlagOn(self):
        self.aborted = True


def test_second_line_tap_finalizes_on_press_without_waiting_for_release():
    tool = ClassificationInteractor.__new__(ClassificationInteractor)
    tool.app = SimpleNamespace(
        panning_button="scroll",
        active_classify_tool="above_line",
    )
    tool.interactor = SimpleNamespace(GetEventPosition=lambda: (20, 30))
    tool.click_to_finalize = True
    tool.P1 = (1.0, 2.0, 3.0)
    tool._last_line_preview_P2 = None
    tool._stop_deferred_left_release_watch = lambda: None
    tool._hide_preview_overlay_for_pan = lambda: None
    tool._check_tool_changed = lambda: None
    tool._get_display_point = lambda: (20, 30)
    tool._display_to_world_fast = lambda _x, _y: (4.0, 5.0, 6.0)
    widget = object()
    tool._get_active_vtk_widget = lambda: widget
    preview_calls = []
    tool._draw_temp_line = lambda p1, p2: preview_calls.append((p1, p2))
    tool._safe_render = lambda _widget: None
    finalize_calls = []
    tool._do_line_classify_tap = lambda: finalize_calls.append(True)

    source = _EventSource()
    tool.on_left_press(source, "LeftButtonPressEvent")

    assert tool._last_line_preview_P2 == (4.0, 5.0, 6.0)
    assert preview_calls == [((1.0, 2.0, 3.0), (4.0, 5.0, 6.0))]
    assert finalize_calls == [True]
    assert source.aborted is True


def test_first_quick_click_with_pointer_jitter_arms_two_tap_mode():
    tool = ClassificationInteractor.__new__(ClassificationInteractor)
    tool.app = SimpleNamespace(active_classify_tool="above_line")
    tool.interactor = SimpleNamespace(GetEventPosition=lambda: (108, 105))
    tool.style = SimpleNamespace(OnLeftButtonUp=lambda: None)
    tool.P1 = (1.0, 2.0, 3.0)
    tool.is_dragging = True
    tool.click_to_finalize = False
    tool._press_pos = (100, 100)
    tool._line_press_max_move_px = 9.5
    tool._last_release_time = 0.0
    tool._suppress_release_observer = False
    tool._ignore_next_left_release = False
    tool._suppress_snt_text = lambda _hidden: None
    tool._check_tool_changed = lambda: None

    tool.on_left_release(_EventSource(), "LeftButtonReleaseEvent")

    assert tool.click_to_finalize is True
    assert tool.P1 == (1.0, 2.0, 3.0)
    assert tool.is_dragging is True
    assert tool._press_pos is None
