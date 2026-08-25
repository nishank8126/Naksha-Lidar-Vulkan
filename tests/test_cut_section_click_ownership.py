from types import SimpleNamespace

from gui.cross_section.cut_section_controller import (
    CutSectionController,
    CutSectionState,
)


def _controller(state=CutSectionState.WAITING_CENTER, source="cross"):
    controller = CutSectionController.__new__(CutSectionController)
    controller.app = SimpleNamespace(panning_button="left", section_vtks={})
    controller._state = state
    controller._cut_source = source
    controller._suspended_cross_pan_button = None
    return controller


def test_pending_cross_cut_owns_left_click_and_restores_pan():
    controller = _controller()

    controller._suspend_cross_section_left_pan()

    assert controller.app.panning_button == "scroll"
    assert controller.owns_cross_section_left_click() is True

    controller._restore_cross_section_left_pan()

    assert controller.app.panning_button == "left"
    assert controller._suspended_cross_pan_button is None


def test_finalized_or_nested_cut_does_not_claim_cross_section_click():
    finalized = _controller(state=CutSectionState.FINALIZED)
    nested = _controller(source="cut")

    assert finalized.owns_cross_section_left_click() is False
    assert nested.owns_cross_section_left_click() is False


def test_escape_stops_persistent_nested_cut_without_restoring_classification():
    controller = _controller(source="cut")
    controller.app.active_classify_tool = "above_line"
    controller.app.statusBar = lambda: SimpleNamespace(showMessage=lambda *args: None)
    controller._saved_classify_state = {"active_tool": "above_line"}
    controller.cut_vtk = None
    controller.is_cut_view_active = False
    controller._has_valid_cut_dock = lambda: True

    cleanup_calls = []

    def cleanup():
        cleanup_calls.append(True)
        controller._state = CutSectionState.IDLE

    controller._force_deactivate_pending_state = cleanup

    assert controller.cancel_persistent_cut_in_cut() is True
    assert cleanup_calls == [True]
    assert controller._state == CutSectionState.IDLE
    assert controller.is_cut_view_active is True
    assert controller.app.active_classify_tool is None
    assert not hasattr(controller, "_saved_classify_state")


def test_escape_ignores_non_nested_cut_state():
    controller = _controller(source="cross")

    assert controller.cancel_persistent_cut_in_cut() is False
