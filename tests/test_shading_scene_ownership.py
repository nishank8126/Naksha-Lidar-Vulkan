from types import SimpleNamespace

import gui.shading_display as shading


class _Actor:
    def __init__(self, visible=True, **tags):
        self._visible = bool(visible)
        for name, value in tags.items():
            setattr(self, name, value)

    def GetVisibility(self):
        return int(self._visible)

    def SetVisibility(self, visible):
        self._visible = bool(visible)

    def VisibilityOff(self):
        self._visible = False


class _Plotter:
    def __init__(self, actors):
        self.actors = dict(actors)
        self.renderer = None

    def remove_actor(self, name, render=False):
        self.actors.pop(name, None)


def test_shading_scene_guard_parks_only_owned_terrain_and_is_idempotent():
    surface = _Actor(_is_surface_mesh=True)
    surface_patch = _Actor(_is_surface_mesh=True)
    cad_surface = _Actor(_naksha_cad_class_surface=True)
    attachment = _Actor(
        _is_dxf_actor=True,
        _naksha_cad_class_surface=True,
    )
    unrelated_polygon = _Actor()
    shaded_mesh = _Actor(_is_shading_mesh=True)
    legacy_edges = _Actor()
    plotter = _Plotter({
        "surface_mesh": surface,
        "surface_patch": surface_patch,
        "cad_surface": cad_surface,
        "dxf_surface": attachment,
        "unrelated_polygon": unrelated_polygon,
        "shaded_mesh": shaded_mesh,
        "shaded_mesh_edges": legacy_edges,
    })
    surface_cache = {"signature": "resident", "transient": False}
    app = SimpleNamespace(
        vtk_widget=plotter,
        _surface_mesh_actor=surface,
        _surface_mesh_cache=surface_cache,
        _surface_resident_signature="resident",
        _shaded_mesh_actor=shaded_mesh,
        display_mode="surface",
        current_display_mode="surface",
        _unified_actor=None,
    )

    first = shading._prepare_scene_for_shading(app, source="test")
    second = shading._prepare_scene_for_shading(app, source="test-repeat")

    assert not surface.GetVisibility()
    assert not surface_patch.GetVisibility()
    assert not cad_surface.GetVisibility()
    assert attachment.GetVisibility()
    assert unrelated_polygon.GetVisibility()
    assert shaded_mesh.GetVisibility()
    assert "shaded_mesh_edges" not in plotter.actors
    assert app._surface_mesh_cache is surface_cache
    assert app.display_mode == "shaded_class"
    assert app.current_display_mode == "shaded_class"
    assert first == {
        "surface_parked": 2,
        "cad_surface_parked": 1,
        "legacy_edges_removed": 1,
        "competing_terrain": 0,
    }
    assert second == {
        "surface_parked": 0,
        "cad_surface_parked": 0,
        "legacy_edges_removed": 0,
        "competing_terrain": 0,
    }
def test_legacy_physical_angle_reentry_keeps_user_sharpness():
    app = SimpleNamespace(
        shading_sharpness_angle=150.0,
        last_shade_angle=27.5,
    )

    assert shading._resolve_requested_multiclass_sharpness(app, 27.5) == 150.0
