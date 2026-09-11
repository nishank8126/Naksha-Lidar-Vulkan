import math

from plugins.naksha_converter.nakshaapp_dgn_converter._snt_core import (
    _scale_bridge_text_height,
)


def test_bridge_text_height_preserves_valid_world_height():
    assert _scale_bridge_text_height(1_000.0, 10_000.0) == 0.1
    assert _scale_bridge_text_height(50_000.0, 10_000.0) == 5.0
    assert _scale_bridge_text_height(800_000.0, 10_000.0) == 80.0


def test_bridge_text_height_uses_desktop_fallback_for_implausible_value():
    # Exact corrupt value emitted by DK1019807 S. PIETRO.dgn's bbox text.
    assert _scale_bridge_text_height(500_000_000.0, 10_000.0) == 5.0
    assert _scale_bridge_text_height(
        500_000_000.0, 10_000.0, "BLOCKS"
    ) == 0.1
    assert _scale_bridge_text_height(
        500_000_000.0, 10_000.0, "FileNames"
    ) == 0.1


def test_bridge_text_height_handles_invalid_metadata():
    assert _scale_bridge_text_height(0.0, 10_000.0) == 1.0
    assert _scale_bridge_text_height(math.nan, 10_000.0) == 1.0
    assert _scale_bridge_text_height(10.0, 0.0) == 10.0
