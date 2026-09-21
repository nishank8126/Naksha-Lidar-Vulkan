from types import SimpleNamespace

import pytest

from gui import unified_actor_manager as actors
from gui import classification_tools


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

@pytest.mark.parametrize("mode_idx", [0, 1])
def test_section_class_modes_allow_classification_rgb_updates(mode_idx):
    dialog = SimpleNamespace(view_color_modes={1: mode_idx})
    app = SimpleNamespace(display_mode_dialog=dialog)

    assert actors._slot_uses_class_rgb(app, 1) is True


@pytest.mark.parametrize("mode_idx", [2, 3, 4, 5, 6, 7])
def test_section_non_class_modes_preserve_their_rgb_buffer(mode_idx):
    dialog = SimpleNamespace(view_color_modes={1: mode_idx})
    app = SimpleNamespace(display_mode_dialog=dialog)

    assert actors._slot_uses_class_rgb(app, 1) is False


def test_section_color_mode_falls_back_to_app_state_without_dialog():
    app = SimpleNamespace(view_color_modes={2: 7})

    assert actors._slot_uses_class_rgb(app, 2) is False


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("class", True), ("shaded_class", True), ("depth", False), ("rgb", False)],
)
def test_main_color_mode_behavior_is_unchanged(mode, expected):
    app = SimpleNamespace(display_mode=mode)

    assert actors._slot_uses_class_rgb(app, 0) is expected

@pytest.mark.parametrize("mode", ["depth", "intensity", "rgb", "elevation", "surface", "line"])
def test_live_section_actor_mode_overrides_stale_class_dialog_state(mode):
    actor = SimpleNamespace(_naksha_color_mode=mode)
    dialog = SimpleNamespace(view_color_modes={1: 0})
    app = SimpleNamespace(display_mode_dialog=dialog, section_vtks={})

    assert actors._slot_uses_class_rgb(app, 1, actor) is False


def test_live_section_class_actor_overrides_stale_non_class_dialog_state():
    actor = SimpleNamespace(_naksha_color_mode="class")
    dialog = SimpleNamespace(view_color_modes={1: 7})
    app = SimpleNamespace(display_mode_dialog=dialog, section_vtks={})

    assert actors._slot_uses_class_rgb(app, 1, actor) is True

@pytest.mark.parametrize(
    "mode",
    ["depth", "intensity", "rgb", "elevation", "surface", "shaded_class", "line"],
)
def test_brush_direct_rgb_poke_is_blocked_outside_class_mode(mode):
    actor = SimpleNamespace(_naksha_color_mode=mode)
    dialog = SimpleNamespace(view_color_modes={1: 0})
    app = SimpleNamespace(display_mode_dialog=dialog, section_vtks={})

    assert classification_tools._section_brush_uses_class_rgb(
        app, 0, actor
    ) is False


def test_brush_direct_rgb_poke_is_allowed_in_class_mode():
    actor = SimpleNamespace(_naksha_color_mode="class")
    dialog = SimpleNamespace(view_color_modes={1: 2})
    app = SimpleNamespace(display_mode_dialog=dialog, section_vtks={})

    assert classification_tools._section_brush_uses_class_rgb(
        app, 0, actor
    ) is True
