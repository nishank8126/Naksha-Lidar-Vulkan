import pytest

from gui.snt_attachment import (
    _infer_snt_coordinate_scale,
    _scale_snt_source_entities_in_place,
)


def _prj_block(x0, x1, y0, y1):
    return {
        "points_2d": [
            (x0, y0),
            (x1, y0),
            (x1, y1),
            (x0, y1),
            (x0, y0),
        ]
    }


def test_legacy_snt_scale_is_derived_from_its_associated_prj_extent():
    # ALLOTMENT-1034.snt is ten times larger than the PRJ/LAS coordinates.
    snt_bounds = (293702.0, 758179.0, 1909801.0, 2215000.0)
    blocks = [_prj_block(30000.0, 62500.0, 195500.0, 221500.0)]

    assert _infer_snt_coordinate_scale(snt_bounds, blocks) == pytest.approx(0.1)


def test_already_aligned_snt_is_not_rescaled():
    snt_bounds = (376050.0, 377850.0, 5104800.0, 5106000.0)
    blocks = [_prj_block(376241.0, 377329.0, 5105207.0, 5105558.0)]

    assert _infer_snt_coordinate_scale(snt_bounds, blocks) == pytest.approx(1.0)


def test_scale_is_not_guessed_without_a_close_power_of_ten_match():
    snt_bounds = (10_000_000.0, 11_000_000.0, 20_000_000.0, 21_000_000.0)
    blocks = [_prj_block(0.0, 100.0, 0.0, 100.0)]

    assert _infer_snt_coordinate_scale(snt_bounds, blocks) == pytest.approx(1.0)


def test_source_entities_are_scaled_in_place_before_render_conversion():
    vertices = [(10.0, 20.0, 30.0), (40.0, 50.0, 60.0)]
    boundary = [(10.0, 20.0, 0.0), (20.0, 30.0, 0.0)]
    entities = [
        # Some producers may alias both coordinate keys to one large list.
        {"type": "POLYLINE", "points": vertices, "vertices": vertices},
        {"type": "HATCH", "boundaries": [boundary]},
        {"type": "3DFACE", "vertices": [(10.0, 20.0, 30.0)] * 3},
        {"type": "TEXT", "position": (10.0, 20.0, 30.0), "height": 5.0},
        {"type": "POINT", "position": (10.0, 20.0, 30.0)},
    ]

    _scale_snt_source_entities_in_place(entities, 0.1)

    assert entities[0]["vertices"] is vertices
    assert entities[0]["vertices"][0] == pytest.approx((1.0, 2.0, 3.0))
    assert entities[1]["boundaries"][0] is boundary
    assert entities[1]["boundaries"][0][1] == pytest.approx((2.0, 3.0, 0.0))
    assert entities[2]["vertices"][0] == pytest.approx((1.0, 2.0, 3.0))
    assert entities[3]["position"] == pytest.approx((1.0, 2.0, 3.0))
    assert entities[3]["height"] == pytest.approx(0.5)
    assert entities[4]["position"] == pytest.approx((1.0, 2.0, 3.0))
