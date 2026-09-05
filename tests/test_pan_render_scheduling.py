import time
from types import SimpleNamespace

from gui.gpu_render_manager import GPURenderManager


class _FakeManager:
    def __init__(self):
        self.app = SimpleNamespace(_shutdown_in_progress=False)
        self.interaction_frame_ms = 33
        self._pan_in_progress = False
        self._pan_frame_started = False
        self._pan_last_frame_monotonic = 0.0
        self._interaction_active = False
        self.force_render_count = 0

    def force_render(self):
        self.force_render_count += 1


def test_manual_pan_uses_adaptive_interaction_budget_and_one_settle_render():
    manager = _FakeManager()

    assert GPURenderManager.begin_pan_interaction(
        manager,
        point_count=12_500_000,
    )
    assert manager._pan_in_progress
    assert not manager._pan_frame_started
    assert manager._interaction_active
    assert not manager.pending_render
    assert manager.interaction_frame_ms == 24

    assert GPURenderManager.finish_pan_interaction(manager)
    assert not manager._pan_in_progress
    assert not manager._pan_frame_started
    assert not manager._interaction_active
    assert manager.force_render_count == 1


def test_duplicate_pan_finish_does_not_force_another_render():
    manager = _FakeManager()

    assert not GPURenderManager.finish_pan_interaction(manager)
    assert manager.force_render_count == 0


def test_small_cloud_uses_the_same_leading_frame_pan_path():
    manager = _FakeManager()

    assert GPURenderManager.begin_pan_interaction(manager, point_count=500_000)
    assert manager._pan_in_progress
    assert not manager._pan_frame_started


def test_large_pan_start_never_traverses_or_mutates_scene_mappers():
    class _NoMapperTouchApp:
        _shutdown_in_progress = False

        @property
        def vtk_widget(self):
            raise AssertionError("pan start must not traverse VTK actor mappers")

    manager = _FakeManager()
    manager.app = _NoMapperTouchApp()

    assert GPURenderManager.begin_pan_interaction(
        manager,
        point_count=13_879_131,
    )


class _FakeTimer:
    def __init__(self):
        self.starts = []

    def start(self, delay):
        self.starts.append(delay)

    def stop(self):
        pass


class _RequestManager:
    def __init__(self):
        self.app = SimpleNamespace(
            _shutdown_in_progress=False,
            _last_classify_ts=0.0,
        )
        self._pan_in_progress = True
        self._pan_frame_started = False
        self._pan_last_frame_monotonic = 0.0
        self._interaction_active = True
        self.interaction_frame_ms = 80
        self.pending_render = False
        self.skipped_renders = 0
        self.render_delay_ms = 75
        self.last_render_time = 0.0
        self.render_timer = _FakeTimer()

    def _is_widget_renderable(self, widget):
        return True

    def _execute_render(self):
        raise AssertionError("request_render must schedule, not render inline")


def test_pan_first_frame_is_immediate_and_pending_moves_do_not_restart_it():
    manager = _RequestManager()

    GPURenderManager.request_render(manager, object())
    GPURenderManager.request_render(manager, object())

    assert manager.render_timer.starts == [0]
    assert manager.pending_render
    assert manager.skipped_renders == 1


def test_pan_followup_frame_uses_only_remaining_budget():
    manager = _RequestManager()
    manager._pan_frame_started = True
    manager._pan_last_frame_monotonic = time.monotonic() - 0.040

    GPURenderManager.request_render(manager, object())

    assert len(manager.render_timer.starts) == 1
    assert 30 <= manager.render_timer.starts[0] <= 45


def test_wheel_first_frame_is_immediate_and_coalesced():
    manager = _RequestManager()
    manager._pan_in_progress = False
    GPURenderManager.request_render(manager, object())
    GPURenderManager.request_render(manager, object())
    assert manager.render_timer.starts == [0]
    assert manager.skipped_renders == 1


def test_wheel_slow_frame_does_not_pay_another_full_delay(monkeypatch):
    manager = _RequestManager()
    manager._pan_in_progress = False
    manager._interaction_frame_started = True
    manager._interaction_last_frame_monotonic = 10.0
    monkeypatch.setattr(time, "monotonic", lambda: 10.090)
    GPURenderManager.request_render(manager, object())
    assert manager.render_timer.starts == [0]


def test_wheel_fast_frame_waits_only_remaining_budget(monkeypatch):
    manager = _RequestManager()
    manager._pan_in_progress = False
    manager._interaction_frame_started = True
    manager._interaction_last_frame_monotonic = 10.0
    monkeypatch.setattr(time, "monotonic", lambda: 10.050)
    GPURenderManager.request_render(manager, object())
    assert manager.render_timer.starts == [30]
