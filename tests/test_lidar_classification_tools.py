import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QGroupBox,
    QLabel,
    QMainWindow,
    QPushButton,
    QWidget,
)
from PySide6.QtCore import Qt

from gui.lidar_classification_tools import (
    ClassifyGroundDialog,
    ClassifyIsolatedPointsDialog,
    ClassifyLowPointsDialog,
    ClassifyBelowSurfaceDialog,
    ClassifySurfacePointsDialog,
    GROUND_TERRAIN_PRESETS,
    PersistentClassSettings,
    _BaseClassifyDialog,
    _apply_fence_picker_row_style,
    _connect_fence_picker_checkbox,
    _estimate_adaptive_ground_thresholds,
    _open_or_restore,
    _resolve_ground_class_defaults,
    _resolve_noise_class_defaults,
    _resolve_surface_class_defaults,
    _select_ground_class0_holdout,
    _bounded_ground_seed_indices,
    _subsample_ground_tin_points,
    classify_below_surface,
    classify_ground_ptd,
    classify_isolated_points,
    classify_low_points,
    classify_surface_points,
)
from gui.theme_manager import ThemeColors


def _qt_app():
    return QApplication.instance() or QApplication([])


def test_initial_checked_fence_can_toggle_before_checkbox_has_parent():
    qt_app = _qt_app()
    shape = {"type": "polygon"}
    row = QWidget()
    badge = QLabel("Selected")
    checkbox = QCheckBox()
    toggles = []

    assert checkbox.parentWidget() is None
    _connect_fence_picker_checkbox(
        shape,
        badge,
        checkbox,
        row,
        False,
        lambda selected_shape, checked: toggles.append(
            (selected_shape, checked)
        ),
    )

    checkbox.setChecked(True)

    assert toggles == [(shape, True)]
    assert badge.isVisibleTo(badge) is True
    assert ThemeColors.get("bg_active") in row.styleSheet()
    assert qt_app is QApplication.instance()


def test_missing_or_destroyed_fence_row_is_nonfatal():
    assert _apply_fence_picker_row_style(None, True, False) is False


def test_every_classifier_completion_dispatches_live_view_refresh():
    qt_app = _qt_app()
    app = QMainWindow()
    emitted = []
    app.classification_finished = SimpleNamespace(
        emit=lambda changed_mask: emitted.append(changed_mask)
    )
    dialog = _BaseClassifyDialog(app, "Refresh Test")
    changed_mask = np.array([False, True, False, True])
    changed_indices = np.array([1, 3], dtype=np.intp)

    assert dialog._notify_low_noise_views(
        {"routine": "low_points"},
        changed_mask,
        changed_indices,
    )
    assert emitted == [changed_mask]
    assert app._last_changed_mask is changed_mask
    np.testing.assert_array_equal(
        app._last_changed_indices,
        changed_indices,
    )

    assert dialog._notify_low_noise_views(
        {"routine": "isolated_points"},
        changed_mask,
        changed_indices,
    )
    assert len(emitted) == 2

    assert dialog._notify_classification_views(
        {"routine": "surface_points"},
        changed_mask,
        changed_indices,
    )
    assert dialog._notify_classification_views(
        {"routine": "below_surface"},
        changed_mask,
        changed_indices,
    )
    assert dialog._notify_classification_views(
        {"routine": "ground_points"},
        changed_mask,
        changed_indices,
    )
    assert len(emitted) == 5
    assert qt_app is QApplication.instance()
    dialog.close()


def test_sparse_refresh_cache_cleanup_does_not_erase_a_newer_run():
    app = SimpleNamespace()
    old_mask = np.array([True, False])
    new_mask = np.array([False, True])
    new_indices = np.array([1], dtype=np.intp)
    app._last_changed_mask = new_mask
    app._last_changed_indices = new_indices

    _BaseClassifyDialog._clear_sparse_change_cache(app, old_mask)

    assert app._last_changed_mask is new_mask
    assert app._last_changed_indices is new_indices

    _BaseClassifyDialog._clear_sparse_change_cache(app, new_mask)
    assert app._last_changed_mask is None
    assert app._last_changed_indices is None


def test_failed_or_cancelled_run_can_restore_the_shared_class_snapshot():
    qt_app = _qt_app()
    app = QMainWindow()
    original = np.array([1, 2, 3, 4], dtype=np.uint8)
    app.data = {
        "xyz": np.zeros((4, 3), dtype=np.float64),
        "classification": original.copy(),
    }
    dialog = _BaseClassifyDialog(app, "Rollback Test")
    app.data["classification"][[0, 2]] = [7, 7]

    restored = dialog._rollback_classification_snapshot(original)

    np.testing.assert_array_equal(restored, [0, 2])
    np.testing.assert_array_equal(
        app.data["classification"],
        original,
    )
    assert qt_app is QApplication.instance()
    dialog.close()


def test_ground_classification_does_not_flood_when_tin_has_too_few_seeds():
    xyz = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.1],
        ],
        dtype=np.float64,
    )
    classes = np.array([1, 1], dtype=np.uint8)

    result = classify_ground_ptd(
        xyz,
        classes,
        from_classes=[1],
        to_class=2,
        current_ground=[2],
        seed_method="ground_only",
    )

    assert result["changed"] == 0
    assert result["stop_reason"] == "fewer than 3 valid ground seeds"
    np.testing.assert_array_equal(classes, [1, 1])


def test_dense_existing_ground_seeds_are_spatially_bounded():
    xx, yy = np.meshgrid(np.arange(100.0), np.arange(100.0))
    xyz = np.column_stack((xx.ravel(), yy.ravel(), (xx + yy).ravel()))
    indices = np.arange(len(xyz), dtype=np.intp)

    selected = _bounded_ground_seed_indices(xyz, indices, max_points=400)

    assert 3 <= len(selected) <= 400
    assert len(np.unique(selected)) == len(selected)
    selected_xy = xyz[selected, :2]
    assert np.ptp(selected_xy[:, 0]) > 90.0
    assert np.ptp(selected_xy[:, 1]) > 90.0


def test_ground_density_reserve_moves_exact_spatial_ratio_to_class_zero():
    # Four trusted ground corners define a flat TIN around 100 source points.
    corners = np.array(
        [
            [-1.0, -1.0, 0.0],
            [10.0, -1.0, 0.0],
            [-1.0, 10.0, 0.0],
            [10.0, 10.0, 0.0],
        ],
        dtype=np.float64,
    )
    xx, yy = np.meshgrid(np.arange(10.0), np.arange(10.0))
    source = np.column_stack(
        [xx.ravel(), yy.ravel(), np.zeros(xx.size, dtype=np.float64)]
    )
    xyz = np.vstack([corners, source])
    classes = np.concatenate(
        [
            np.full(len(corners), 2, dtype=np.uint8),
            np.full(len(source), 1, dtype=np.uint8),
        ]
    )

    result = classify_ground_ptd(
        xyz,
        classes,
        from_classes=[1],
        to_class=2,
        current_ground=[2],
        seed_method="ground_only",
        reduce_angle_edge=False,
        class0_holdout_percent=5,
    )

    assert result["changed"] == 100
    assert result["accepted_candidates"] == 100
    assert result["ground_count"] == 95
    assert result["class0_count"] == 5
    np.testing.assert_array_equal(classes[:4], [2, 2, 2, 2])
    assert np.count_nonzero(classes[4:] == 2) == 95
    assert np.count_nonzero(classes[4:] == 0) == 5


def test_class_zero_holdout_is_deterministic_and_protects_tin_seeds():
    xyz = np.column_stack(
        [
            np.arange(100, dtype=np.float64),
            np.arange(100, dtype=np.float64) % 7,
            np.zeros(100, dtype=np.float64),
        ]
    )
    candidates = np.arange(100, dtype=np.intp)
    protected = np.array([5, 15, 25, 35, 45], dtype=np.intp)

    first = _select_ground_class0_holdout(
        xyz,
        candidates,
        10,
        protected_indices=protected,
    )
    second = _select_ground_class0_holdout(
        xyz,
        candidates,
        10,
        protected_indices=protected,
    )

    assert len(first) == 10
    assert not np.isin(first, protected).any()
    np.testing.assert_array_equal(first, second)


def test_large_ground_tin_subsample_keeps_simplex_coordinates_aligned():
    points = np.column_stack(
        [
            np.arange(12, dtype=np.float64),
            np.arange(12, dtype=np.float64) * 10.0,
            np.arange(12, dtype=np.float64) * 100.0,
        ]
    )
    ground_local = np.array([1, 3, 5, 7, 9, 11], dtype=np.intp)

    sampled_local, sampled_points = _subsample_ground_tin_points(
        points,
        ground_local,
        max_points=3,
    )

    np.testing.assert_array_equal(sampled_local, [1, 5, 9])
    np.testing.assert_array_equal(sampled_points, points[sampled_local])


def test_ground_defaults_follow_the_active_project_class_map():
    class FakeApp:
        class_palette = {
            0: {"name": "Created"},
            1: {"name": "Ground"},
            2: {"name": "Low vegetation"},
        }

    assert _resolve_ground_class_defaults(FakeApp()) == (0, 1)


def test_ground_defaults_use_standard_las_codes_without_project_map():
    assert _resolve_ground_class_defaults(None) == (1, 2)


def test_surface_defaults_follow_the_active_project_class_map():
    class FakeApp:
        class_palette = {
            0: {"name": "Created"},
            1: {"name": "Ground"},
            11: {"name": "Road Surface"},
        }

    assert _resolve_surface_class_defaults(FakeApp()) == (0, 11)


def test_noise_defaults_follow_active_project_names_and_never_choose_water():
    class FakeApp:
        class_palette = {
            0: {"name": "Created"},
            1: {"name": "Ground"},
            9: {"name": "Water"},
            17: {"name": "Low Noise"},
            52: {"name": "Isolated point"},
        }

    assert _resolve_noise_class_defaults(FakeApp()) == (0, 17)
    assert _resolve_noise_class_defaults(
        FakeApp(),
        isolated=True,
    ) == (0, 52)


def test_low_points_single_uses_source_surrounding_not_ground_class():
    xyz = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 1.0],
            [0.0, 1.0, 1.1],
            [1.0, 1.0, 1.2],
            [20.0, 20.0, -10.0],  # unrelated Ground-class point
        ],
        dtype=np.float64,
    )
    classes = np.array([1, 1, 1, 1, 2], dtype=np.uint8)

    result = classify_low_points(
        xyz,
        classes,
        from_classes=[1],
        to_class=7,
        ground_classes=[2],  # retained compatibility argument; ignored
        search_mode="single",
        more_than=0.5,
        within=2.0,
    )

    assert result["routine"] == "low_points"
    assert result["low_accepted"] == 1
    np.testing.assert_array_equal(classes, [7, 1, 1, 1, 2])


def test_low_points_group_honors_maximum_group_count():
    xyz = np.array(
        [
            [0.0, 0.0, 0.0],
            [0.2, 0.0, 0.1],
            [0.0, 1.0, 1.2],
            [1.0, 0.0, 1.3],
            [1.0, 1.0, 1.4],
        ],
        dtype=np.float64,
    )

    accepted_classes = np.ones(5, dtype=np.uint8)
    accepted = classify_low_points(
        xyz,
        accepted_classes,
        from_classes=[1],
        to_class=7,
        search_mode="groups",
        max_count=2,
        more_than=0.5,
        within=2.0,
    )
    assert accepted["changed"] == 2
    np.testing.assert_array_equal(accepted_classes, [7, 7, 1, 1, 1])

    rejected_classes = np.ones(5, dtype=np.uint8)
    rejected = classify_low_points(
        xyz,
        rejected_classes,
        from_classes=[1],
        to_class=7,
        search_mode="groups",
        max_count=1,
        more_than=0.5,
        within=2.0,
    )
    assert rejected["changed"] == 0
    np.testing.assert_array_equal(rejected_classes, np.ones(5, dtype=np.uint8))


def test_low_points_one_run_moves_only_the_lowest_elevation_tier():
    xyz = np.array(
        [
            [0.0, 0.0, 0.0],
            [0.5, 0.0, 1.0],
            [1.0, 0.0, 2.0],
        ],
        dtype=np.float64,
    )
    classes = np.ones(3, dtype=np.uint8)

    result = classify_low_points(
        xyz,
        classes,
        from_classes=[1],
        to_class=7,
        search_mode="groups",
        max_count=2,
        more_than=0.5,
        within=2.0,
    )

    assert result["changed"] == 1
    np.testing.assert_array_equal(classes, [7, 1, 1])


def test_low_points_does_not_report_already_target_points_as_changed():
    xyz = np.array(
        [[0.0, 0.0, 0.0], [0.5, 0.0, 1.0], [1.0, 0.0, 1.1]],
        dtype=np.float64,
    )
    classes = np.full(3, 7, dtype=np.uint8)

    result = classify_low_points(
        xyz,
        classes,
        from_classes=[7],
        to_class=7,
        search_mode="single",
        more_than=0.5,
        within=2.0,
    )

    assert result["low_detected"] == 1
    assert result["changed"] == 0
    assert result["indices"].size == 0


def test_isolated_points_counts_other_points_in_a_true_3d_sphere():
    xyz = np.array(
        [
            [0.0, 0.0, 0.0],
            [0.5, 0.0, 0.0],
            [10.0, 0.0, 0.0],
            [10.0, 0.0, 2.0],  # same XY but outside a 1 m 3D sphere
        ],
        dtype=np.float64,
    )
    classes = np.ones(4, dtype=np.uint8)

    result = classify_isolated_points(
        xyz,
        classes,
        from_classes=[1],
        to_class=18,
        in_classes=[1],
        if_fewer_than=1,
        within=1.0,
    )

    assert result["routine"] == "isolated_points"
    assert result["isolated_accepted"] == 2
    np.testing.assert_array_equal(classes, [1, 1, 18, 18])


def test_isolated_fence_limits_changes_but_not_neighbor_context():
    xyz = np.array(
        [
            [0.0, 0.0, 0.0],
            [0.2, 0.0, 0.0],
            [10.0, 0.0, 0.0],
        ],
        dtype=np.float64,
    )
    classes = np.ones(3, dtype=np.uint8)
    fence = np.array([True, False, False])

    result = classify_isolated_points(
        xyz,
        classes,
        from_classes=[1],
        to_class=18,
        in_classes=[1],
        if_fewer_than=1,
        within=1.0,
        fence_mask=fence,
    )

    assert result["isolated_candidates"] == 1
    assert result["changed"] == 0
    np.testing.assert_array_equal(classes, [1, 1, 1])


def test_isolated_points_classifies_when_no_in_class_reference_exists():
    xyz = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]],
        dtype=np.float64,
    )
    classes = np.ones(2, dtype=np.uint8)

    result = classify_isolated_points(
        xyz,
        classes,
        from_classes=[1],
        to_class=18,
        in_classes=[2],
        if_fewer_than=1,
        within=1.0,
    )

    assert result["changed"] == 2
    np.testing.assert_array_equal(classes, [18, 18])


def test_isolated_points_does_not_report_target_class_noops():
    xyz = np.array(
        [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]],
        dtype=np.float64,
    )
    classes = np.full(2, 18, dtype=np.uint8)

    result = classify_isolated_points(
        xyz,
        classes,
        from_classes=[18],
        to_class=18,
        in_classes=[18],
        if_fewer_than=1,
        within=1.0,
    )

    assert result["isolated_detected"] == 2
    assert result["changed"] == 0
    assert result["indices"].size == 0


def test_surface_points_recognizes_horizontal_and_vertical_planes():
    xx, yy = np.meshgrid(np.linspace(0, 3, 7), np.linspace(0, 3, 7))
    horizontal = np.column_stack(
        [xx.ravel(), yy.ravel(), np.zeros(xx.size)]
    )
    yy2, zz2 = np.meshgrid(np.linspace(0, 3, 7), np.linspace(0, 3, 7))
    vertical = np.column_stack(
        [np.full(yy2.size, 10.0), yy2.ravel(), zz2.ravel()]
    )
    xyz = np.vstack([horizontal, vertical])
    classes = np.zeros(len(xyz), dtype=np.uint8)

    result = classify_surface_points(
        xyz,
        classes,
        from_classes=[0],
        to_class=11,
        tolerance=0.01,
        num_neighbors=12,
        batch_size=1_000,
    )

    assert result["routine"] == "surface_points"
    assert result["surface_accepted"] == len(xyz)
    assert result["changed"] == len(xyz)
    assert result["rejected_degenerate"] == 0
    np.testing.assert_array_equal(classes, np.full(len(xyz), 11))


def test_surface_points_rejects_point_outside_local_tolerance():
    xx, yy = np.meshgrid(np.linspace(0, 3, 7), np.linspace(0, 3, 7))
    plane = np.column_stack([xx.ravel(), yy.ravel(), np.zeros(xx.size)])
    outlier = np.array([[1.5, 1.5, 1.0]])
    xyz = np.vstack([plane, outlier])
    classes = np.zeros(len(xyz), dtype=np.uint8)

    result = classify_surface_points(
        xyz,
        classes,
        from_classes=[0],
        to_class=11,
        tolerance=0.03,
        num_neighbors=12,
        batch_size=1_000,
    )

    assert result["surface_accepted"] == len(plane)
    assert result["rejected_tolerance"] == 1
    np.testing.assert_array_equal(classes[:-1], np.full(len(plane), 11))
    assert classes[-1] == 0


def test_surface_points_respects_the_active_fence():
    xx, yy = np.meshgrid(np.linspace(0, 3, 7), np.linspace(0, 3, 7))
    xyz = np.column_stack([xx.ravel(), yy.ravel(), np.zeros(xx.size)])
    classes = np.zeros(len(xyz), dtype=np.uint8)
    fence = xyz[:, 0] <= 1.5

    result = classify_surface_points(
        xyz,
        classes,
        from_classes=[0],
        to_class=11,
        tolerance=0.01,
        num_neighbors=8,
        fence_mask=fence,
        batch_size=1_000,
    )

    assert result["surface_candidates"] == int(np.count_nonzero(fence))
    assert np.all(classes[fence] == 11)
    assert np.all(classes[~fence] == 0)


def test_below_surface_uses_plane_distance_and_standard_deviation():
    xx, yy = np.meshgrid(np.linspace(0, 3, 7), np.linspace(0, 3, 7))
    zz = 0.5 * xx + 0.2 * yy
    plane = np.column_stack([xx.ravel(), yy.ravel(), zz.ravel()])
    low_point = np.array([[1.5, 1.5, 0.5 * 1.5 + 0.2 * 1.5 - 1.0]])
    xyz = np.vstack([plane, low_point])
    classes = np.full(len(xyz), 2, dtype=np.uint8)

    result = classify_below_surface(
        xyz,
        classes,
        from_classes=[2],
        to_class=7,
        surface_type="planar",
        limit=4.0,
        z_tolerance=0.10,
        num_neighbors=12,
    )

    assert result["routine"] == "below_surface"
    assert result["below_surface_flagged"] == 1
    assert result["iterations"] == 1
    np.testing.assert_array_equal(classes[:-1], np.full(len(plane), 2))
    assert classes[-1] == 7


def test_below_surface_chunking_matches_a_single_large_batch():
    xx, yy = np.meshgrid(np.linspace(0, 3, 7), np.linspace(0, 3, 7))
    zz = 0.3 * xx - 0.1 * yy
    plane = np.column_stack([xx.ravel(), yy.ravel(), zz.ravel()])
    low = np.array(
        [
            [1.0, 1.0, 0.3 * 1.0 - 0.1 * 1.0 - 1.0],
            [2.0, 2.0, 0.3 * 2.0 - 0.1 * 2.0 - 0.8],
        ]
    )
    xyz = np.vstack([plane, low])
    small_batch_classes = np.full(len(xyz), 2, dtype=np.uint8)
    large_batch_classes = small_batch_classes.copy()

    small = classify_below_surface(
        xyz,
        small_batch_classes,
        from_classes=[2],
        to_class=7,
        num_neighbors=12,
        batch_size=7,
    )
    large = classify_below_surface(
        xyz,
        large_batch_classes,
        from_classes=[2],
        to_class=7,
        num_neighbors=12,
        batch_size=10_000,
    )

    np.testing.assert_array_equal(small_batch_classes, large_batch_classes)
    np.testing.assert_array_equal(small["indices"], large["indices"])
    assert small["changed"] == large["changed"]


def test_below_surface_does_not_repeat_target_class_noops():
    xx, yy = np.meshgrid(np.linspace(0, 3, 7), np.linspace(0, 3, 7))
    plane = np.column_stack([xx.ravel(), yy.ravel(), np.zeros(xx.size)])
    low = np.array([[1.5, 1.5, -1.0]])
    xyz = np.vstack([plane, low])
    classes = np.full(len(xyz), 7, dtype=np.uint8)

    result = classify_below_surface(
        xyz,
        classes,
        from_classes=[7],
        to_class=7,
        num_neighbors=12,
        iterative=True,
        max_iterations=3,
        batch_size=8,
    )

    assert result["changed"] == 0
    assert result["indices"].size == 0
    assert result["stop_reason"] == "stable"


def test_classifiers_skip_non_finite_xyz_without_crashing():
    xyz = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 1.0],
            [0.0, 1.0, 1.0],
            [np.nan, 2.0, 2.0],
            [2.0, np.inf, 2.0],
        ],
        dtype=np.float64,
    )

    low_classes = np.ones(len(xyz), dtype=np.uint8)
    low = classify_low_points(
        xyz,
        low_classes,
        from_classes=[1],
        to_class=7,
        search_mode="single",
        more_than=0.5,
        within=2.0,
    )
    assert low["changed"] == 1
    np.testing.assert_array_equal(low_classes[-2:], [1, 1])

    isolated_classes = np.ones(len(xyz), dtype=np.uint8)
    isolated = classify_isolated_points(
        xyz,
        isolated_classes,
        from_classes=[1],
        to_class=18,
        in_classes=[1],
        within=2.0,
    )
    assert isolated["routine"] == "isolated_points"
    np.testing.assert_array_equal(isolated_classes[-2:], [1, 1])

    ground_classes = np.array([2, 2, 2, 1, 1], dtype=np.uint8)
    ground = classify_ground_ptd(
        xyz,
        ground_classes,
        from_classes=[1],
        to_class=2,
        current_ground=[2],
        seed_method="ground_only",
    )
    assert ground["changed"] == 0
    np.testing.assert_array_equal(ground_classes[-2:], [1, 1])


def test_adaptive_ground_thresholds_stay_conservative_on_flat_terrain():
    xx, yy = np.meshgrid(np.arange(6.0), np.arange(6.0))
    seeds = np.column_stack(
        [xx.ravel(), yy.ravel(), np.zeros(xx.size, dtype=np.float64)]
    )

    result = _estimate_adaptive_ground_thresholds(seeds)

    assert result["estimated_slope"] == 0.0
    assert result["terrain_angle"] == 15.0
    assert result["iteration_angle"] == 4.0
    assert result["iteration_distance"] == 0.5


def test_adaptive_ground_thresholds_expand_for_mountain_slope():
    xx, yy = np.meshgrid(np.arange(6.0), np.arange(6.0))
    # z=x is a mathematically exact 45-degree terrain plane.
    seeds = np.column_stack([xx.ravel(), yy.ravel(), xx.ravel()])

    result = _estimate_adaptive_ground_thresholds(seeds)

    assert np.isclose(result["estimated_slope"], 45.0)
    assert np.isclose(result["terrain_angle"], 57.0)
    assert np.isclose(result["iteration_angle"], 10.0)
    assert np.isclose(result["iteration_distance"], 1.4)


def test_ground_terrain_presets_are_locked_to_increasing_ruggedness():
    ordered = [
        GROUND_TERRAIN_PRESETS[name]
        for name in ("flat_urban", "rolling", "hilly", "mountain")
    ]

    assert [item["iteration_angle"] for item in ordered] == [4.0, 6.0, 8.0, 10.0]
    assert [item["iteration_distance"] for item in ordered] == [0.5, 0.8, 1.1, 1.4]
    assert all(0.5 <= item["iteration_distance"] <= 1.5 for item in ordered)


def test_ground_classification_reports_effective_adaptive_thresholds():
    corners = np.array(
        [
            [-1.0, -1.0, 0.0],
            [10.0, -1.0, 0.0],
            [-1.0, 10.0, 0.0],
            [10.0, 10.0, 0.0],
        ],
        dtype=np.float64,
    )
    source = np.array([[2.0, 2.0, 0.0]], dtype=np.float64)
    xyz = np.vstack([corners, source])
    classes = np.array([2, 2, 2, 2, 1], dtype=np.uint8)

    result = classify_ground_ptd(
        xyz,
        classes,
        from_classes=[1],
        to_class=2,
        current_ground=[2],
        seed_method="ground_only",
        reduce_angle_edge=False,
        adaptive_thresholds=True,
    )

    assert result["adaptive_thresholds"] is True
    assert result["estimated_slope"] == 0.0
    assert result["effective_terrain_angle"] == 15.0
    assert result["effective_iteration_angle"] == 4.0
    assert result["effective_iteration_distance"] == 0.5
    np.testing.assert_array_equal(classes, [2, 2, 2, 2, 2])


def _ground_dialog():
    qt_app = _qt_app()
    PersistentClassSettings._store.clear()
    app = QMainWindow()
    app.class_palette = {
        0: {"name": "Created"},
        1: {"name": "Ground"},
        2: {"name": "Low vegetation"},
    }
    dialog = ClassifyGroundDialog(app)
    return qt_app, app, dialog


def _surface_dialog():
    qt_app = _qt_app()
    PersistentClassSettings._store.clear()
    app = QMainWindow()
    app.class_palette = {
        0: {"name": "Created"},
        1: {"name": "Ground"},
        11: {"name": "Road Surface"},
    }
    dialog = ClassifySurfacePointsDialog(app)
    return qt_app, app, dialog


def _noise_dialogs():
    qt_app = _qt_app()
    PersistentClassSettings._store.clear()
    app = QMainWindow()
    app.class_palette = {
        0: {"name": "Created"},
        1: {"name": "Ground"},
        9: {"name": "Water"},
        17: {"name": "Low Noise"},
        52: {"name": "Isolated point"},
    }
    low = ClassifyLowPointsDialog(app)
    isolated = ClassifyIsolatedPointsDialog(app)
    return qt_app, app, low, isolated


def test_ground_dialog_uses_compact_tabs_without_default_scrolling():
    qt_app, _, dialog = _ground_dialog()
    dialog.show()
    qt_app.processEvents()

    assert [
        dialog._tabs.tabText(index)
        for index in range(dialog._tabs.count())
    ] == ["Setup", "Terrain", "Strategy & QC"]
    assert dialog.findChildren(QGroupBox) == []
    assert dialog.terrain_spin.toolTip()
    assert dialog.reduce_chk.toolTip()
    assert not dialog.dist_rating_chk.isEnabled()
    assert not dialog.dist_rating_chk.isChecked()

    for index in range(dialog._tabs.count()):
        dialog._tabs.setCurrentIndex(index)
        qt_app.processEvents()
        assert dialog._scroll.verticalScrollBar().maximum() == 0
        assert dialog._scroll.horizontalScrollBar().maximum() == 0

    dialog.close()


def test_below_surface_dialog_uses_project_defaults_and_tool_fence_picker():
    qt_app = _qt_app()
    PersistentClassSettings._store.clear()
    app = QMainWindow()
    app.class_palette = {
        0: {"name": "Created"},
        1: {"name": "Ground"},
        17: {"name": "Low Noise"},
    }
    drawing = {
        "type": "polygon",
        "coords": [
            [0.0, 0.0, 0.0],
            [2.0, 0.0, 0.0],
            [0.0, 2.0, 0.0],
        ],
    }
    app.digitizer = SimpleNamespace(drawings=[drawing])
    dialog = ClassifyBelowSurfaceDialog(app)

    assert dialog._get_from() == [1]
    assert dialog._get_to() == [17]
    assert dialog._keep_visible_during_run
    dialog.show()
    dialog._fence_sel.select_btn.click()
    qt_app.processEvents()
    picker = dialog._fence_sel._active_picker_dialog
    assert picker is not None
    assert picker.windowFlags() & Qt.Tool
    for index in range(dialog._tabs.count()):
        dialog._tabs.setCurrentIndex(index)
        qt_app.processEvents()
        assert dialog._scroll.horizontalScrollBar().maximum() == 0
        assert dialog._scroll.verticalScrollBar().maximum() == 0
    picker.close()
    dialog.close()


def test_low_and_isolated_dialogs_expose_only_terrascan_conditions():
    qt_app, _, low, isolated = _noise_dialogs()
    low.show()
    isolated.show()
    qt_app.processEvents()

    low_text = " ".join(
        label.text() for label in low.findChildren(QLabel)
    ).lower()
    isolated_text = " ".join(
        label.text() for label in isolated.findChildren(QLabel)
    ).lower()

    assert low._get_from() == [0]
    assert low._get_to() == [17]
    assert low.mt_spin.minimum() > 0
    assert "does <b>not</b> use ground class" in low_text
    assert not hasattr(low, "_get_ground_ref")

    assert isolated._get_from() == [0]
    assert isolated._get_to() == [52]
    assert isolated._get_in() == [0]
    assert isolated.fewer_spin.minimum() == 1
    assert "candidate itself" not in isolated_text
    assert "other</b> points" in isolated_text
    assert not hasattr(isolated, "height_filter_chk")
    assert not hasattr(isolated, "iter_chk")
    assert low._scroll.horizontalScrollBar().maximum() == 0
    assert low._scroll.verticalScrollBar().maximum() == 0
    assert isolated._scroll.horizontalScrollBar().maximum() == 0
    assert isolated._scroll.verticalScrollBar().maximum() == 0

    low.close()
    isolated.close()


def test_surface_dialog_exposes_microstation_surface_controls():
    qt_app, _, dialog = _surface_dialog()
    dialog.show()
    qt_app.processEvents()

    assert dialog.windowTitle() == "Classify Surface Points"
    assert np.isclose(dialog.tolerance_spin.value(), 0.05)
    assert dialog.tolerance_spin.toolTip()
    assert "full 3D" in " ".join(
        label.text() for label in dialog.findChildren(QLabel)
    )
    assert dialog._scroll.horizontalScrollBar().maximum() == 0
    assert dialog._scroll.verticalScrollBar().maximum() == 0

    dialog.close()


def test_surface_dialog_stays_visible_and_preserves_selected_fence():
    qt_app, app, dialog = _surface_dialog()
    drawing = {
        "type": "polygon",
        "coords": [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
        ],
    }
    dialog._fence_sel.selected_fences = [drawing]
    app._test_surface_dialog = dialog
    dialog.show()
    dialog._set_algorithm_running(True)
    dialog._restore_after_algorithm()

    _open_or_restore(
        app,
        "_test_surface_dialog",
        ClassifySurfacePointsDialog,
    )
    qt_app.processEvents()

    assert dialog.isVisible()
    assert dialog._scroll.isEnabled()
    assert dialog._fence_sel.selected_fences == [drawing]
    dialog.close()


def test_surface_fence_picker_is_visible_and_accepts_restored_point_vertices():
    qt_app, app, dialog = _surface_dialog()
    restored_fence = {
        "type": "polyline",
        "points": [
            [0.0, 0.0, 0.0],
            [2.0, 0.0, 0.0],
            [2.0, 2.0, 0.0],
            [0.0, 2.0, 0.0],
            [0.0, 0.0, 0.0],
        ],
    }
    app.digitizer = SimpleNamespace(drawings=[restored_fence])
    dialog.show()
    dialog._fence_sel.select_btn.click()
    qt_app.processEvents()

    picker = dialog._fence_sel._active_picker_dialog
    assert picker is not None
    assert picker.isVisible()
    assert picker.windowFlags() & Qt.Tool

    row_checkboxes = [
        checkbox
        for checkbox in picker.findChildren(QCheckBox)
        if "Permanent Fence Mode" not in checkbox.text()
    ]
    assert len(row_checkboxes) == 1
    row_checkboxes[0].setChecked(True)
    apply_button = next(
        button
        for button in picker.findChildren(QPushButton)
        if button.text() == "Apply Selection"
    )
    apply_button.click()
    qt_app.processEvents()

    assert dialog._fence_sel.selected_fences == [restored_fence]
    xyz = np.array(
        [[1.0, 1.0, 0.0], [3.0, 3.0, 0.0]],
        dtype=np.float64,
    )
    np.testing.assert_array_equal(
        dialog._fence_sel.get_fence_mask(xyz),
        [True, False],
    )
    dialog.close()


def test_ground_dialog_stays_visible_and_unlocks_after_a_run():
    qt_app, _, dialog = _ground_dialog()
    dialog.show()
    qt_app.processEvents()

    dialog._set_algorithm_running(True)
    assert dialog.isVisible()
    assert not dialog._scroll.isEnabled()
    assert not dialog._apply_btn.isEnabled()

    dialog._restore_after_algorithm()
    qt_app.processEvents()

    assert dialog.isVisible()
    assert dialog._scroll.isEnabled()
    assert dialog._apply_btn.isEnabled()
    assert dialog._apply_btn.text() == "Apply Classification"
    dialog.close()


def test_completed_ground_fence_remains_selected_until_user_clears_it():
    _, app, dialog = _ground_dialog()
    drawing = {
        "type": "polygon",
        "coords": [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0],
        ],
    }
    app.digitizer = SimpleNamespace(drawings=[drawing])
    dialog._fence_sel.selected_fences = [drawing]
    dialog._fence_sel._conversion_completed = True

    dialog._fence_sel._mark_fences_as_classified()

    assert dialog._fence_sel.selected_fences == [drawing]
    assert drawing["classified_fence"] is True
    assert "selected" in dialog._fence_sel.status_lbl.text()

    dialog._fence_sel._clear_selected_fences_only()

    assert dialog._fence_sel.selected_fences == []
    assert app.digitizer.drawings == [drawing]
    assert drawing["classified_fence"] is True
    dialog.close()


def test_raising_existing_ground_tool_does_not_reset_its_fence():
    qt_app, app, dialog = _ground_dialog()
    drawing = {
        "type": "polygon",
        "coords": [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
        ],
    }
    dialog._fence_sel.selected_fences = [drawing]
    app._test_ground_dialog = dialog

    _open_or_restore(
        app,
        "_test_ground_dialog",
        ClassifyGroundDialog,
    )
    qt_app.processEvents()

    assert dialog._fence_sel.selected_fences == [drawing]
    dialog.close()
