import inspect

from gui import display_mode as display_modes
from gui import unified_actor_manager as actors


def test_main_gpu_warmup_runs_on_first_idle_turn():
    source = inspect.getsource(actors.build_unified_actor)

    assert "t.start(0)" in source
    assert "t.start(500)" not in source


def test_section_gpu_warmup_does_not_wait_for_first_pan():
    source = inspect.getsource(actors.build_section_unified_actor)

    assert "QTimer.singleShot(0, lambda: _deferred_actor_gpu_init" in source
    assert "QTimer.singleShot(500, lambda: _deferred_actor_gpu_init" not in source


def test_flight_line_section_rebuild_preserves_color_mode():
    popup_source = inspect.getsource(display_modes.DisplayModeDialog._open_lines_dialog)
    builder_source = inspect.getsource(actors.build_section_unified_actor)

    assert "color_mode=section_mode" in popup_source
    assert '7: "line"' in popup_source
    assert 'color_mode: str = "class"' in builder_source
    assert builder_source.count("_rewrite_section_rgb_for_mode(") >= 3
    assert 'getattr(vtk_widget, "_naksha_color_mode", color_mode)' in builder_source