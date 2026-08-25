import inspect

from gui import unified_actor_manager as actors


def test_main_gpu_warmup_runs_on_first_idle_turn():
    source = inspect.getsource(actors.build_unified_actor)

    assert "t.start(0)" in source
    assert "t.start(500)" not in source


def test_section_gpu_warmup_does_not_wait_for_first_pan():
    source = inspect.getsource(actors.build_section_unified_actor)

    assert "QTimer.singleShot(0, lambda: _deferred_actor_gpu_init" in source
    assert "QTimer.singleShot(500, lambda: _deferred_actor_gpu_init" not in source
