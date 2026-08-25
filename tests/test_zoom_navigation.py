import math

import pytest

from gui.zoom_navigation import (
    FAST_WHEEL_ZOOM_FACTOR,
    MAX_WHEEL_STEPS_PER_EVENT,
    fast_wheel_zoom_factor,
    interaction_frame_interval_ms,
    pan_frame_interval_ms,
    qt_position_to_vtk_display,
)


def test_fast_zoom_is_reciprocal_and_preserves_multi_notch_input():
    zoom_in = fast_wheel_zoom_factor(120)
    zoom_out = fast_wheel_zoom_factor(-120)

    assert zoom_in == pytest.approx(FAST_WHEEL_ZOOM_FACTOR)
    assert zoom_out == pytest.approx(1.0 / FAST_WHEEL_ZOOM_FACTOR)
    assert zoom_in * zoom_out == pytest.approx(1.0)
    assert fast_wheel_zoom_factor(240) == pytest.approx(FAST_WHEEL_ZOOM_FACTOR**2)


def test_fast_zoom_caps_accidental_wheel_bursts():
    assert fast_wheel_zoom_factor(12000) == pytest.approx(
        FAST_WHEEL_ZOOM_FACTOR**MAX_WHEEL_STEPS_PER_EVENT
    )
    assert fast_wheel_zoom_factor(-12000) == pytest.approx(
        FAST_WHEEL_ZOOM_FACTOR ** -MAX_WHEEL_STEPS_PER_EVENT
    )


@pytest.mark.parametrize("bad_delta", [math.inf, -math.inf, math.nan])
def test_fast_zoom_rejects_non_finite_input(bad_delta):
    with pytest.raises(ValueError):
        fast_wheel_zoom_factor(bad_delta)


def test_qt_position_maps_to_vtk_coordinates_and_accounts_for_high_dpi():
    assert qt_position_to_vtk_display(25, 10, 100, 50, 200, 100) == pytest.approx(
        (50, 80)
    )


def test_qt_position_is_clamped_to_render_viewport():
    assert qt_position_to_vtk_display(-5, 100, 100, 50, 200, 100) == pytest.approx(
        (0, 0)
    )


def test_large_clouds_use_a_slower_repaint_budget_without_slowing_camera_input():
    assert interaction_frame_interval_ms(1_000_000) == 33
    assert interaction_frame_interval_ms(5_000_000) == 50
    assert interaction_frame_interval_ms(15_000_000) == 66
    assert interaction_frame_interval_ms(30_000_000) == 80


def test_main_pan_uses_a_faster_budget_with_gpu_time_as_the_natural_cap():
    assert pan_frame_interval_ms(1_000_000) == 16
    assert pan_frame_interval_ms(5_000_000) == 24
    assert pan_frame_interval_ms(15_000_000) == 30
    assert pan_frame_interval_ms(30_000_000) == 33
