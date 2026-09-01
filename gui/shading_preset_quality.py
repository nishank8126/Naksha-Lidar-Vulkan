"""Shared quality choices and legacy conversion for ShadingMode presets."""

SHADING_QUALITY_CHOICES = (
    ("Fast", "fast"),
    ("Normal", "normal"),
    ("Slow – all points", "slow"),
)

SLOW_ALL_POINTS_CONFIRM_THRESHOLD = 25_000_000


def slow_all_points_requires_confirmation(point_count):
    """Return whether Slow all-points conversion should show the RAM warning."""
    return int(point_count) >= SLOW_ALL_POINTS_CONFIRM_THRESHOLD


def normalize_shading_preset_quality(value=None, legacy_speed=None):
    """Return a renderer quality key, including conversion of old 1-10 speeds."""
    quality = str(value or "").strip().lower()
    if quality in ("fast", "normal", "slow"):
        return quality

    if legacy_speed is not None:
        try:
            speed = int(legacy_speed)
        except (TypeError, ValueError):
            speed = None
        if speed is not None:
            # The retired control was a sampling stride: 1 retained all points,
            # while larger values progressively favored speed.
            if speed <= 1:
                return "slow"
            if speed >= 7:
                return "fast"

    return "normal"


def shading_quality_label(value):
    """Return the exact user-facing label used by Display Mode."""
    quality = normalize_shading_preset_quality(value)
    return dict((key, label) for label, key in SHADING_QUALITY_CHOICES)[quality]