from types import SimpleNamespace

from gui.app_window import NakshaApp
from gui.digitize_tools import DigitizeManager


class _Style:
    def __init__(self, state=4):
        self.state = state
        self.releases = []
        self.stop_calls = 0

    def GetState(self):
        return self.state

    def StopState(self):
        self.stop_calls += 1
        self.state = 0

    def OnLeftButtonUp(self):
        self.releases.append("left")

    def OnRightButtonUp(self):
        self.releases.append("right")

    def OnMiddleButtonUp(self):
        self.releases.append("middle")


class _Interactor:
    def __init__(self, style):
        self.style = style
        self.modifiers = []
        self.focus_releases = 0

    def GetInteractorStyle(self):
        return self.style

    def SetControlKey(self, value):
        self.modifiers.append(("ctrl", value))

    def SetShiftKey(self, value):
        self.modifiers.append(("shift", value))

    def SetAltKey(self, value):
        self.modifiers.append(("alt", value))

    def ReleaseFocus(self):
        self.focus_releases += 1


def test_grid_reset_explicitly_stops_vtk_dolly_state():
    style = _Style(state=4)
    interactor = _Interactor(style)
    manager = SimpleNamespace(
        interactor=interactor,
        left_down=True,
        middle_down=True,
        _is_panning=True,
        _pan_start_pos=(1, 2),
        _last_pos=(2, 3),
        _blocked_fake_middle=True,
        _middle_button_down=True,
    )

    DigitizeManager._reset_stale_zoom_mouse_state(manager)

    assert style.state == 0
    assert style.stop_calls == 1
    assert style.releases == ["left", "right", "middle"]
    assert interactor.focus_releases == 1
    assert manager.left_down is False
    assert manager.middle_down is False


def test_hover_guard_repairs_only_nonzero_vtk_state():
    style = _Style(state=4)
    interactor = _Interactor(style)
    reset_calls = []
    app = SimpleNamespace(
        vtk_widget=SimpleNamespace(interactor=interactor),
        digitizer=SimpleNamespace(
            _reset_stale_zoom_mouse_state=lambda: (
                reset_calls.append(True), style.StopState()
            )
        ),
    )

    assert NakshaApp._repair_stale_main_interactor_drag(app)
    assert style.state == 0
    assert reset_calls == [True]
    assert not NakshaApp._repair_stale_main_interactor_drag(app)
