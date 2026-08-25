from types import SimpleNamespace

import gui.unified_actor_manager as uam
from gui.cross_section.section_zoom import SectionWheelZoomEventFilter
from gui.gpu_render_manager import GPURenderManager


class _FakeActor:
    def __init__(self, visible=1, data_id=1):
        self.visible = int(visible)
        self._naksha_data_id = data_id
        self._naksha_lod_active = False

    def GetVisibility(self):
        return self.visible

    def SetVisibility(self, visible):
        self.visible = int(visible)


def test_main_lod_switch_changes_only_actor_visibility(monkeypatch):
    full = _FakeActor(visible=1)
    lod = _FakeActor(visible=0)
    app = SimpleNamespace(
        _shutdown_in_progress=False,
        _unified_actor=full,
        _main_interaction_actor=lod,
    )
    monkeypatch.setattr(uam, "INTERACTION_ACTOR_SWITCHING_ENABLED", True)
    monkeypatch.setattr(uam, "_sync_interaction_actor_subset", lambda *_: True)

    assert uam.apply_main_lod(app)
    assert full.visible == 0
    assert lod.visible == 1
    assert full._naksha_lod_active

    assert uam.restore_main_full_detail(app)
    assert full.visible == 1
    assert lod.visible == 0
    assert not full._naksha_lod_active


def test_view_lod_switch_changes_only_actor_visibility(monkeypatch):
    full = _FakeActor(visible=1)
    lod = _FakeActor(visible=0)
    widget = SimpleNamespace(
        _naksha_view_finalized=False,
        _naksha_full_detail_actor=full,
        _naksha_interaction_actor=lod,
        _naksha_interaction_lod_active=False,
    )
    monkeypatch.setattr(uam, "INTERACTION_ACTOR_SWITCHING_ENABLED", True)
    monkeypatch.setattr(uam, "_sync_interaction_actor_subset", lambda *_: True)

    assert uam.apply_view_interaction_lod(widget)
    assert full.visible == 0
    assert lod.visible == 1

    assert uam.restore_view_full_detail(widget)
    assert full.visible == 1
    assert lod.visible == 0


def test_intentionally_hidden_full_actor_is_never_revealed(monkeypatch):
    full = _FakeActor(visible=0)
    lod = _FakeActor(visible=0)
    app = SimpleNamespace(
        _shutdown_in_progress=False,
        _unified_actor=full,
        _main_interaction_actor=lod,
    )
    monkeypatch.setattr(uam, "INTERACTION_ACTOR_SWITCHING_ENABLED", True)
    sync_calls = []
    monkeypatch.setattr(
        uam,
        "_sync_interaction_actor_subset",
        lambda *_: sync_calls.append(True) or True,
    )

    assert not uam.apply_main_lod(app)
    assert full.visible == 0
    assert lod.visible == 0
    assert sync_calls == []


def test_production_navigation_never_switches_actors():
    full = _FakeActor(visible=1)
    lod = _FakeActor(visible=0)
    app = SimpleNamespace(
        _shutdown_in_progress=False,
        _unified_actor=full,
        _main_interaction_actor=lod,
    )
    widget = SimpleNamespace(
        _naksha_view_finalized=False,
        _naksha_full_detail_actor=full,
        _naksha_interaction_actor=lod,
        _naksha_interaction_lod_active=False,
    )

    assert not uam.INTERACTION_ACTOR_SWITCHING_ENABLED
    assert not uam.apply_main_lod(app)
    assert not uam.apply_view_interaction_lod(widget)
    assert full.visible == 1
    assert lod.visible == 0


def test_navigation_controllers_do_not_request_sampled_actors():
    assert GPURenderManager._engage_lod(SimpleNamespace()) is False
    assert SectionWheelZoomEventFilter._engage_interaction_detail(
        SimpleNamespace()
    ) is False
