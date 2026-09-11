"""Regression tests for SNT canvas reprojection.

Reprojecting an SNT feature-by-feature used to rebuild a PROJ TransformerGroup
for every entity, which stalled the GUI thread for hours on a multi-million
feature file.  These tests pin the batched path and the transformer cache.
"""
import time

import pytest
from pyproj import CRS

import gui.projection_engine as projection_engine
from gui.projection_engine import build_transformer, transform_point_sequence
from gui.snt_attachment import _reproject_entities_for_render

SOURCE_CRS = CRS.from_epsg(31370)
CANVAS_CRS = CRS.from_epsg(3812)


def _entity_reference_transform(entity, source_crs, canvas_crs):
    """The original per-entity transform, kept as the correctness reference."""
    if not isinstance(entity, dict):
        return entity

    def tx_points(seq):
        if not seq:
            return list(seq or [])
        try:
            if source_crs.equals(canvas_crs):
                return list(seq)
        except Exception:
            pass
        try:
            return transform_point_sequence(seq, source_crs, canvas_crs)
        except Exception:
            # Ragged/malformed sequences were left in native coordinates.
            return list(seq)

    out = dict(entity)
    for key in ("points", "vertices"):
        values = entity.get(key)
        if values:
            out[key] = tx_points(values)
    boundaries = entity.get("boundaries")
    if isinstance(boundaries, list):
        out["boundaries"] = [
            tx_points(ring) if isinstance(ring, list) else ring for ring in boundaries
        ]
    for key in ("position", "center", "insert"):
        position = entity.get(key)
        if isinstance(position, (tuple, list)) and len(position) >= 2:
            converted = tx_points([position])
            if converted:
                out[key] = converted[0]
    return out


def _sample_entities():
    return [
        {"type": "POLYLINE", "layer": "BL_GRID", "color": (1, 2, 3),
         "points": [(150000.0, 170000.0, 12.0), (150010.5, 170005.25, 12.5)]},
        {"type": "LWPOLYLINE", "layer": "L1", "color": (4, 5, 6), "closed": True,
         "vertices": [(150100.0, 170100.0, 3.0), (150101.0, 170100.0, 3.0),
                      (150101.0, 170101.0, 3.0)]},
        {"type": "TEXT", "layer": "FileNames", "color": (7, 8, 9), "text": "A.laz",
         "position": (150200.0, 170200.0, 4.0), "height": 2.0},
        {"type": "POINT", "layer": "P", "color": (9, 9, 9),
         "position": (150300.0, 170300.0, 5.0)},
        {"type": "HATCH", "layer": "H", "color": (3, 3, 3),
         "boundaries": [[(150400.0, 170400.0, 0.0), (150402.0, 170400.0, 0.0),
                         (150402.0, 170402.0, 0.0)]]},
        {"type": "3DFACE", "layer": "F", "color": (2, 2, 2),
         "vertices": [(150500.0, 170500.0, 1.0), (150501.0, 170500.0, 1.0),
                      (150501.0, 170501.0, 1.0)]},
    ]


def _assert_close(expected, actual, tolerance=1e-9):
    assert type(expected) is type(actual), (expected, actual)
    if isinstance(expected, dict):
        assert set(expected) == set(actual)
        for key in expected:
            _assert_close(expected[key], actual[key], tolerance)
    elif isinstance(expected, (list, tuple)):
        assert len(expected) == len(actual), (expected, actual)
        for left, right in zip(expected, actual):
            _assert_close(left, right, tolerance)
    elif isinstance(expected, float):
        assert expected == pytest.approx(actual, abs=tolerance)
    else:
        assert expected == actual


def test_batched_reprojection_matches_per_entity_reference():
    entities = _sample_entities()
    expected = [_entity_reference_transform(e, SOURCE_CRS, CANVAS_CRS) for e in entities]
    actual = _reproject_entities_for_render(entities, SOURCE_CRS, CANVAS_CRS)
    for left, right in zip(expected, actual):
        _assert_close(left, right)


def test_batched_reprojection_does_not_mutate_source_entities():
    entities = _sample_entities()
    before = [_entity_reference_transform(e, SOURCE_CRS, SOURCE_CRS) for e in entities]
    _reproject_entities_for_render(entities, SOURCE_CRS, CANVAS_CRS)
    for original, entity in zip(before, entities):
        _assert_close(original, entity)


def test_reprojection_is_identity_when_crs_already_matches():
    entities = _sample_entities()
    result = _reproject_entities_for_render(entities, SOURCE_CRS, SOURCE_CRS)
    assert result == entities


def test_reprojection_is_identity_without_a_crs():
    entities = _sample_entities()
    assert _reproject_entities_for_render(entities, None, CANVAS_CRS) == entities
    assert _reproject_entities_for_render(entities, SOURCE_CRS, None) == entities


def test_build_transformer_is_cached_per_thread():
    projection_engine._transformer_cache().clear()
    first_transformer, _ = build_transformer(SOURCE_CRS, CANVAS_CRS)

    start = time.perf_counter()
    for _ in range(50):
        again, _ = build_transformer(SOURCE_CRS, CANVAS_CRS)
    elapsed = time.perf_counter() - start

    assert again is first_transformer
    # An uncached build costs several milliseconds; 50 cached lookups must be
    # far cheaper than even one of them.
    assert elapsed < 0.1


def test_batched_reprojection_handles_ragged_sequences():
    entities = [
        {"type": "POLYLINE", "layer": "L", "color": (1, 1, 1),
         "points": [(150000.0, 170000.0), (150010.0, 170010.0, 1.0)]},
        {"type": "TEXT", "layer": "T", "color": (1, 1, 1), "text": "x",
         "position": (150000.0, 170000.0, 0.0)},
    ]
    expected = [_entity_reference_transform(e, SOURCE_CRS, CANVAS_CRS) for e in entities]
    actual = _reproject_entities_for_render(entities, SOURCE_CRS, CANVAS_CRS)
    for left, right in zip(expected, actual):
        _assert_close(left, right)
