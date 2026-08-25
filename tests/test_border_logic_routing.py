from types import SimpleNamespace

import pytest

from gui import unified_actor_manager as actors


class _Dialog:
    def __init__(self, current_slot, current_mode, modes):
        self.current_slot = current_slot
        self._current_mode = current_mode
        self.view_border_modes = dict(modes)

    def get_border_mode(self):
        return self._current_mode


def test_main_sync_uses_main_mode_when_dialog_is_showing_another_view():
    dialog = _Dialog(current_slot=1, current_mode=0, modes={0: 1, 1: 0})
    app = SimpleNamespace(display_mode_dialog=dialog)

    assert actors.resolve_border_logic_mode(app, 0) == 1.0


def test_current_slot_mode_updates_target_slot_cache():
    dialog = _Dialog(current_slot=0, current_mode=2, modes={0: 1})
    app = SimpleNamespace(display_mode_dialog=dialog)

    assert actors.resolve_border_logic_mode(app, 0) == 2.0
    assert dialog.view_border_modes[0] == 2
    assert app._naksha_border_logic_modes[0] == 2.0


@pytest.mark.parametrize("saved_mode", [1.0, 2.0])
def test_grid_load_without_dialog_uses_saved_border_mode(monkeypatch, saved_mode):
    monkeypatch.setattr(
        actors, "_read_saved_border_logic_mode", lambda: saved_mode
    )
    app = SimpleNamespace()

    assert actors.resolve_border_logic_mode(app, 0) == saved_mode
    assert app._naksha_border_logic_modes[0] == saved_mode


def test_invalid_mode_cannot_reach_gpu_uniform():
    assert actors._coerce_border_logic_mode(99) == 0.0
    assert actors._coerce_border_logic_mode("structured") == 0.0
