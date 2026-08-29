from types import SimpleNamespace

import numpy as np

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


def test_flight_line_data_patch_does_not_reveal_actor_hidden_by_shading(monkeypatch):
    class _FakeArray:
        def __init__(self):
            self.values = np.zeros(3, dtype=np.uint8)
            self.modified = False

        def Modified(self):
            self.modified = True

    class _FakePointData:
        def __init__(self, array):
            self.array = array

        def GetArray(self, name):
            return self.array if name == "FlightVisible" else None

    class _FakeMesh:
        def __init__(self, array):
            self.point_data = _FakePointData(array)
            self.modified = False

        def GetPointData(self):
            return self.point_data

        def Modified(self):
            self.modified = True

    flight_array = _FakeArray()
    mesh = _FakeMesh(flight_array)
    actor = _FakeActor(visible=0)
    actor._naksha_mesh = mesh
    plotter = SimpleNamespace(actors={uam.UNIFIED_ACTOR_NAME: actor})
    app = SimpleNamespace(
        display_mode="shaded_class",
        _unified_actor=actor,
        vtk_widget=plotter,
        data={
            "xyz": np.zeros((3, 3), dtype=np.float64),
            "point_source_id": np.array([10, 20, 10], dtype=np.uint16),
        },
        flight_line_visibility_by_slot={0: {10: True, 20: False}},
    )
    monkeypatch.setattr(
        uam.numpy_support,
        "vtk_to_numpy",
        lambda vtk_array: vtk_array.values,
    )

    assert uam.fast_main_flight_line_visibility_update(app, render=False)
    np.testing.assert_array_equal(flight_array.values, [1, 0, 1])
    assert flight_array.modified
    assert mesh.modified
    assert actor.visible == 0


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
