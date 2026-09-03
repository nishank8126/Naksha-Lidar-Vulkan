import json

from gui.shortcut_manager import (
    _is_owned_qt_object,
    decode_display_visibility_preset,
    decode_line_mode_preset,
    encode_display_visibility_preset,
    encode_line_mode_preset,
    line_mode_to_display_visibility_preset,
    shortcut_search_matches,
)


class _FakeQtObject:
    def __init__(self, parent=None):
        self._parent = parent

    def parent(self):
        return self._parent


def test_line_visibility_preset_round_trips_classes_and_flight_lines():
    preset = {
        "mode": "Line",
        "target_view": 0,
        "classes": {
            2: {"show": True},
            5: {"show": False},
        },
        "flight_lines": {
            7: False,
            8: True,
            24: True,
        },
    }

    decoded = decode_display_visibility_preset(
        encode_display_visibility_preset(preset)
    )

    assert decoded["classes"] == {
        2: {"show": True},
        5: {"show": False},
    }
    assert decoded["flight_lines"] == {
        7: False,
        8: True,
        24: True,
    }


def test_legacy_line_mode_round_trips_new_class_selection():
    encoded = encode_line_mode_preset({
        "classes": {
            2: {"show": True},
            5: {"show": False},
        },
        "lines": {
            7: False,
            8: True,
        },
    })

    raw = json.loads(encoded)
    decoded = decode_line_mode_preset(encoded)

    assert raw["classes"]["2"]["show"] is True
    assert decoded["classes"][5]["show"] is False
    assert decoded["lines"] == {7: False, 8: True}


def test_legacy_lines_key_converts_to_canonical_runtime_preset():
    canonical = line_mode_to_display_visibility_preset({
        "classes": {3: {"show": True}},
        "lines": {7: False, 85: True},
    })

    assert canonical == {
        "mode": "Line",
        "target_view": 0,
        "classes": {3: {"show": True}},
        "flight_lines": {7: False, 85: True},
    }


def test_nested_flight_line_dialog_is_owned_by_line_preset_dialog():
    line_preset_dialog = _FakeQtObject()
    flight_line_dialog = _FakeQtObject(parent=line_preset_dialog)
    nested_widget = _FakeQtObject(parent=flight_line_dialog)
    unrelated_dialog = _FakeQtObject()

    assert _is_owned_qt_object(line_preset_dialog, flight_line_dialog)
    assert _is_owned_qt_object(line_preset_dialog, nested_widget)
    assert not _is_owned_qt_object(line_preset_dialog, unrelated_dialog)


def test_shortcut_search_matches_captured_modifier_and_key_combination():
    assert shortcut_search_matches("alt+f1", "alt", "F1", "DisplayMode", "V1: 22vis")
    assert shortcut_search_matches(" ALT + F1 ", "alt", "F1", "DisplayMode")
    assert not shortcut_search_matches("alt+f2", "alt", "F1", "DisplayMode")


def test_shortcut_search_keeps_individual_column_matching():
    assert shortcut_search_matches("alt", "alt", "F1", "DisplayMode", "V1: 22vis")
    assert shortcut_search_matches("displaymode", "alt", "F1", "DisplayMode")
    assert shortcut_search_matches("22vis", "alt", "F1", "DisplayMode", "V1: 22vis")
