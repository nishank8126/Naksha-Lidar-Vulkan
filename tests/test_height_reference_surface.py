import numpy as np

from gui.height_reference_surface import heights_above_reference_tin


def test_tin_uses_barycentric_plane_height_not_nearest_vertex():
    reference = np.array([
        [0.0, 0.0, 0.0],
        [10.0, 0.0, 10.0],
        [0.0, 10.0, 20.0],
    ])
    query = np.array([[2.0, 3.0, 13.0]])

    heights, valid, info = heights_above_reference_tin(
        reference, query, max_triangle=20.0,
    )

    assert valid.tolist() == [True]
    assert heights[0] == 5.0
    assert info["usable_triangles"] == 1


def test_max_triangle_rejects_gap_spanning_surface_and_outside_queries():
    reference = np.array([
        [0.0, 0.0, 0.0],
        [10.0, 0.0, 0.0],
        [0.0, 10.0, 0.0],
    ])
    query = np.array([
        [2.0, 2.0, 1.0],
        [20.0, 20.0, 1.0],
    ])

    heights, valid, info = heights_above_reference_tin(
        reference, query, max_triangle=9.0,
    )

    assert valid.tolist() == [False, False]
    assert np.isnan(heights).all()
    assert info["usable_triangles"] == 0


def test_duplicate_reference_xy_is_handled_deterministically():
    reference = np.array([
        [0.0, 0.0, 0.0],
        [0.0, 0.0, 99.0],
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
    ])
    query = np.array([[0.25, 0.25, 2.0]])

    heights, valid, _ = heights_above_reference_tin(
        reference, query, max_triangle=2.0,
    )

    assert valid.tolist() == [True]
    assert heights[0] == 2.0
