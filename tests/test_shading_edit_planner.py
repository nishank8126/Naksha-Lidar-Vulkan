from types import SimpleNamespace
from unittest import mock

import numpy as np

from gui import shading_display as shading
from gui import unified_actor_manager as actors


def _plan(old, new, visible=2):
    old = np.asarray(old, dtype=np.uint8)
    new = np.asarray(new, dtype=np.uint8)
    cache = SimpleNamespace(unique_indices=np.arange(10_000))
    with mock.patch.object(shading, "get_cache", return_value=cache):
        return shading.plan_single_class_shading_edit(
            SimpleNamespace(), np.arange(len(old)), old, new, visible
        )


def test_local_add_uses_actual_transition():
    assert _plan([5, 7], [2, 2]) is shading.ShadingEditKind.LOCAL_ADD


def test_local_remove_uses_actual_transition():
    assert _plan([2, 2], [5, 7]) is shading.ShadingEditKind.LOCAL_REMOVE


def test_non_visible_transition_has_no_geometry_change():
    assert _plan([5, 7], [8, 9]) is shading.ShadingEditKind.NO_GEOMETRY_CHANGE


def test_all_visible_classes_use_faceted_shading():
    xyz = np.array([
        [0.0, 0.0, 0.0],
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 1.0],
        [1.0, 1.0, 0.0],
    ])
    classes = np.array([0, 0, 1, 1], dtype=np.uint8)

    result = shading._compute_shading_geometry_backend(
        xyz, classes, {0, 1}, 45.0, 45.0, 0.25, 10.0, 10.0, 123
    )

    assert result["smooth_all_classes"] is False


def test_faceted_classification_targets_only_incident_faces():
    cache = SimpleNamespace(
        unique_indices=np.arange(5, dtype=np.int64),
        faces=np.array([
            [0, 1, 2],
            [1, 3, 2],
            [3, 4, 2],
        ], dtype=np.int32),
        build_global_to_unique=lambda total: np.arange(total, dtype=np.int64),
    )
    changed = np.array([False, True, False, False, False])

    vertices, faces = shading._affected_faceted_faces(cache, changed, 5)

    np.testing.assert_array_equal(vertices, [1])
    np.testing.assert_array_equal(faces, [0, 1])


def test_mixed_transition_uses_safe_fallback():
    assert _plan([5, 2], [2, 7]) is shading.ShadingEditKind.FULL_REBUILD


def _same_preset_shading_app():
    class App:
        pass

    actor = object()
    app = App()
    app.data = {
        "xyz": np.array([
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 1.0],
        ]),
        "classification": np.full(3, 2, dtype=np.uint8),
    }
    app.vtk_widget = SimpleNamespace(
        actors={"shaded_mesh": actor},
        render=mock.Mock(),
    )
    app._shaded_mesh_actor = actor
    app.class_palette = {2: {"show": True}}
    app.shading_quality = "normal"
    return app


def test_force_rebuild_bypasses_same_preset_current_shortcut():
    app = _same_preset_shading_app()
    cache = SimpleNamespace(
        is_fully_current=mock.Mock(return_value=True),
        is_valid=mock.Mock(return_value=True),
    )

    with (
        mock.patch.object(shading, "_prepare_scene_for_shading"),
        mock.patch(
            "gui.flight_line_filter.flight_line_visibility_mask",
            return_value=np.ones(3, dtype=bool),
        ),
        mock.patch.object(shading, "_get_shading_visibility", return_value={2}),
        mock.patch.object(shading, "_build_cache_key", return_value=("same",)),
        mock.patch.object(shading, "get_cache", return_value=cache),
        mock.patch.object(
            shading, "_get_rendered_cache_key", return_value=("same",)
        ),
        mock.patch.object(shading, "_build_visible_geometry") as rebuild,
    ):
        shading.update_shaded_class(app, force_rebuild=True)

    cache.is_fully_current.assert_not_called()
    rebuild.assert_called_once()


def test_same_preset_current_shortcut_remains_for_normal_refresh():
    app = _same_preset_shading_app()
    cache = SimpleNamespace(is_fully_current=mock.Mock(return_value=True))

    with (
        mock.patch.object(shading, "_prepare_scene_for_shading"),
        mock.patch(
            "gui.flight_line_filter.flight_line_visibility_mask",
            return_value=np.ones(3, dtype=bool),
        ),
        mock.patch.object(shading, "_get_shading_visibility", return_value={2}),
        mock.patch.object(shading, "_build_cache_key", return_value=("same",)),
        mock.patch.object(shading, "get_cache", return_value=cache),
        mock.patch.object(
            shading, "_get_rendered_cache_key", return_value=("same",)
        ),
        mock.patch.object(shading, "_hide_point_cloud_actors_for_shading"),
        mock.patch.object(shading, "_build_visible_geometry") as rebuild,
    ):
        shading.update_shaded_class(app)

    cache.is_fully_current.assert_called_once()
    rebuild.assert_not_called()


def test_recovers_exact_delta_from_matching_undo_entry():
    app = SimpleNamespace(undo_stack=[{
        "indices": np.array([3, 9]),
        "old_classes": np.array([5, 7], dtype=np.uint8),
        "new_classes": np.array([2, 2], dtype=np.uint8),
    }])
    delta = shading._recover_classification_delta(app, np.array([3, 9]))
    assert delta is not None
    np.testing.assert_array_equal(delta.old_classes, [5, 7])
    np.testing.assert_array_equal(delta.new_classes, [2, 2])


def test_does_not_recover_unrelated_undo_entry():
    app = SimpleNamespace(undo_stack=[{
        "indices": np.array([1, 2]),
        "old_classes": np.array([5, 7]),
        "new_classes": np.array([2, 2]),
    }])
    assert shading._recover_classification_delta(app, np.array([3, 9])) is None


def test_multiclass_history_fast_path_accepts_scalar_target_classes():
    cache = SimpleNamespace(n_visible_classes=3)
    app = SimpleNamespace(_shading_visibility_override={1, 2, 3})
    changed_mask = np.array([False, True, False, True], dtype=bool)

    with (
        mock.patch.object(shading, "get_cache", return_value=cache),
        mock.patch.object(
            shading, "_fast_multiclass_color_overlay", return_value=True
        ) as overlay,
        mock.patch.object(
            shading, "refresh_shaded_after_classification_fast"
        ) as generic_refresh,
    ):
        result = shading.refresh_shaded_after_history_fast(
            app,
            changed_mask,
            np.array([1, 2], dtype=np.uint8),
            np.uint8(3),
            "undo",
        )

    assert result is True
    np.testing.assert_array_equal(
        overlay.call_args.kwargs["changed_indices"], [1, 3]
    )
    generic_refresh.assert_not_called()


def test_history_membership_change_delegates_with_normalized_delta():
    cache = SimpleNamespace(n_visible_classes=3)
    app = SimpleNamespace(_shading_visibility_override={1, 2, 3})
    changed_mask = np.array([False, True, True], dtype=bool)

    with (
        mock.patch.object(shading, "get_cache", return_value=cache),
        mock.patch.object(
            shading,
            "refresh_shaded_after_classification_fast",
            return_value=True,
        ) as generic_refresh,
    ):
        result = shading.refresh_shaded_after_history_fast(
            app,
            changed_mask,
            np.uint8(9),
            np.array([1, 2], dtype=np.uint8),
            "redo",
        )

    assert result is True
    delta = generic_refresh.call_args.kwargs["delta"]
    np.testing.assert_array_equal(delta.changed_indices, [1, 2])
    np.testing.assert_array_equal(delta.old_classes, [9, 9])
    np.testing.assert_array_equal(delta.new_classes, [1, 2])


def test_overlay_remove_drops_affected_actor_and_rebuilds_survivors():
    widget = SimpleNamespace(remove_actor=mock.Mock())
    app = SimpleNamespace(
        vtk_widget=widget,
        data={"classification": np.array([1, 2, 1, 1], dtype=np.uint8)},
        _shading_add_overlays=[
            {"name": "overlay_a", "indices": np.array([0, 1, 2])},
            {"name": "overlay_b", "indices": np.array([3])},
        ],
    )
    cache = SimpleNamespace(single_class_id=1)
    with (mock.patch.object(shading, "get_cache", return_value=cache),
          mock.patch.object(shading, "_fast_add_overlay", return_value=True) as rebuild):
        removed = shading._remove_from_add_overlays(app, np.array([1]))
    assert removed == 1
    widget.remove_actor.assert_called_once_with("overlay_a", render=False)
    np.testing.assert_array_equal(app._shading_add_overlays[0]["indices"], [3])
    np.testing.assert_array_equal(rebuild.call_args.args[1], [0, 2])


def test_overlay_remove_ignores_unrelated_actor():
    widget = SimpleNamespace(remove_actor=mock.Mock())
    entry = {"name": "overlay_a", "indices": np.array([0, 2])}
    app = SimpleNamespace(
        vtk_widget=widget,
        data={"classification": np.ones(4, dtype=np.uint8)},
        _shading_add_overlays=[entry],
    )
    cache = SimpleNamespace(single_class_id=1)
    with mock.patch.object(shading, "get_cache", return_value=cache):
        assert shading._remove_from_add_overlays(app, np.array([3])) == 0
    widget.remove_actor.assert_not_called()
    assert app._shading_add_overlays == [entry]


def test_main_actor_patch_converts_global_index_to_lod_local():
    global_map = np.arange(0, 39, 3, dtype=np.int64)
    rgb = np.zeros((len(global_map), 3), dtype=np.uint8)
    actor = SimpleNamespace(
        _naksha_rgb_ptr=rgb,
        _naksha_vtk_array=None,
        _naksha_mesh=None,
    )
    actor.Modified = mock.Mock()
    app = SimpleNamespace(
        data={"classification": np.ones(39, dtype=np.uint8)},
        _main_global_indices=global_map,
        display_mode="shaded_class",
        class_palette={1: {"color": (10, 20, 30), "show": True}},
    )
    actors._patch_actor_memory(app, actor, np.array([30]), slot_idx=0)
    np.testing.assert_array_equal(rgb[10], [10, 20, 30])


def test_shading_overlay_cleanup_does_not_touch_class_actors():
    widget = SimpleNamespace(
        actors={
            "class_1": object(),
            "shaded_mesh_live_add_4": object(),
            "shaded_mesh_live_add_5": object(),
        },
        remove_actor=mock.Mock(),
    )
    app = SimpleNamespace(
        vtk_widget=widget,
        _shading_add_overlays=[
            {"name": "shaded_mesh_live_add_4", "indices": np.array([1])}
        ],
    )
    assert shading._remove_fast_shading_overlays(app) == 2
    removed_names = {call.args[0] for call in widget.remove_actor.call_args_list}
    assert removed_names == {
        "shaded_mesh_live_add_4", "shaded_mesh_live_add_5"
    }
    assert "class_1" not in removed_names
    assert app._shading_add_overlays == []


def test_full_resolution_actor_validator_expects_every_loaded_point():
    assert actors._expected_main_actor_point_count(32_782_650) == 32_782_650


def test_detach_shading_removes_patches_but_preserves_class_actor():
    widget = SimpleNamespace(
        actors={
            "class_2": object(),
            "shaded_mesh": object(),
            "shaded_mesh_edges": object(),
            "shaded_mesh_live_add_9": object(),
        },
        remove_actor=mock.Mock(),
    )
    app = SimpleNamespace(
        vtk_widget=widget,
        _shading_add_overlays=[{"name": "shaded_mesh_live_add_9"}],
        _rendered_shading_cache_key=("cached",),
    )
    shading.detach_shading_before_non_shading_mode(app)
    removed = {call.args[0] for call in widget.remove_actor.call_args_list}
    assert removed == {
        "shaded_mesh", "shaded_mesh_edges", "shaded_mesh_live_add_9"
    }
    assert "class_2" not in removed
    assert app._shaded_mesh_actor is None
    assert app._rendered_shading_cache_key is None


def test_local_removal_fill_can_bridge_region_larger_than_normal_edge_limit():
    xy = np.array([
        [0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0],
        [1.0, 1.0],
    ])
    removed = np.array([
        [0, 1, 4], [1, 2, 4], [2, 3, 4], [3, 0, 4],
    ], dtype=np.int32)
    candidate = np.array([[0, 1, 2], [0, 2, 3]], dtype=np.int32)
    result = shading._clip_fill_faces_to_removed_region(
        xy, candidate, removed, base_max_edge=0.5
    )
    assert len(result) == 2


def test_local_removal_fill_rejects_triangle_outside_removed_footprint():
    xy = np.array([
        [0.0, 0.0], [1.0, 0.0], [2.0, 0.0],
        [0.0, 1.0], [1.0, 1.0], [0.0, 2.0],
    ])
    removed = np.array([
        [0, 1, 4], [0, 4, 3], [3, 4, 5],
    ], dtype=np.int32)
    candidate = np.array([
        [0, 1, 4],  # inside
        [1, 2, 4],  # outside the removed L-shaped footprint
    ], dtype=np.int32)
    result = shading._clip_fill_faces_to_removed_region(
        xy, candidate, removed, base_max_edge=10.0
    )
    np.testing.assert_array_equal(result, [[0, 1, 4]])
