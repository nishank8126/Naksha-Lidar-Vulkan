import pytest
from types import SimpleNamespace

import gui.shading_display as shading


def test_legacy_sharpness_response_is_unchanged_through_90():
    assert shading._shading_sharpness_response(45.0) == (0.5, 0.0)
    assert shading._shading_sharpness_response(90.0) == (1.0, 0.0)


def test_sharpness_has_strong_monotonic_response_from_90_to_200():
    values = [90.0, 105.0, 120.0, 150.0, 180.0, 200.0]
    overdrive = [
        shading._shading_sharpness_response(value)[1] for value in values
    ]

    assert overdrive == sorted(overdrive)
    assert len(set(overdrive)) == len(overdrive)
    assert overdrive[1] > 0.10
    assert overdrive[-1] == pytest.approx(0.90)


def test_sharpness_retains_progressive_tail_after_200():
    at_200 = shading._shading_sharpness_response(200.0)[1]
    at_500 = shading._shading_sharpness_response(500.0)[1]
    at_999 = shading._shading_sharpness_response(999.0)[1]

    assert 0.0 < at_200 < at_500 < at_999
    assert at_999 == pytest.approx(1.0)


def test_overdrive_changes_actual_light_elevation_through_200():
    app = SimpleNamespace(shading_light_elevation=45.0)
    values = [90.0, 105.0, 140.0, 165.0, 195.0, 200.0]
    elevations = [
        shading._shading_effective_light_elevation(
            app, shading._shading_sharpness_response(value)[1]
        )
        for value in values
    ]

    assert elevations[0] == pytest.approx(45.0)
    assert all(a > b for a, b in zip(elevations, elevations[1:]))
    assert elevations[-1] == pytest.approx(15.3)


def test_multiclass_point_tip_overlay_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv('NAKSHA_SHADING_SHARP_TIPS', raising=False)
    app = SimpleNamespace()
    cache = SimpleNamespace(n_visible_classes=18)
    assert shading._sharp_multiclass_tip_enabled(app, cache) is False


def test_multiclass_point_tip_overlay_remains_explicitly_opt_in(monkeypatch):
    monkeypatch.setenv('NAKSHA_SHADING_SHARP_TIPS', '1')
    app = SimpleNamespace()
