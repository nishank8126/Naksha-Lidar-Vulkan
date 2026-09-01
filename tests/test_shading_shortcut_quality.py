import json

import pytest

from gui.shading_preset_quality import (
    normalize_shading_preset_quality,
    shading_quality_label,
    slow_all_points_requires_confirmation,
)
from gui.shortcut_manager import decode_shading_preset, encode_shading_preset


def test_named_quality_round_trips_without_legacy_numeric_speed():
    encoded = encode_shading_preset({
        "quality_mode": "fast",
        "classes": {2: {"show": True, "color": (1, 2, 3)}},
    })

    raw = json.loads(encoded)
    decoded = decode_shading_preset(encoded)

    assert raw["quality_mode"] == "fast"
    assert "speed" not in raw
    assert decoded["quality_mode"] == "fast"


@pytest.mark.parametrize(
    ("legacy_speed", "expected"),
    [(1, "slow"), (2, "normal"), (6, "normal"), (7, "fast"), (10, "fast")],
)
def test_legacy_numeric_speed_maps_to_named_quality(legacy_speed, expected):
    encoded = json.dumps({
        "__type__": "shading_mode_preset",
        "speed": legacy_speed,
        "classes": {},
    })

    assert decode_shading_preset(encoded)["quality_mode"] == expected


def test_quality_labels_match_display_mode_selector():
    assert shading_quality_label("fast") == "Fast"
    assert shading_quality_label("normal") == "Normal"
    assert shading_quality_label("slow") == "Slow – all points"
    assert normalize_shading_preset_quality("unexpected") == "normal"


def test_slow_all_points_warning_starts_at_25_million_points():
    assert not slow_all_points_requires_confirmation(0)
    assert not slow_all_points_requires_confirmation(24_999_999)
    assert slow_all_points_requires_confirmation(25_000_000)
    assert slow_all_points_requires_confirmation(25_000_001)
