from plugins.naksha_converter.nakshaapp_dgn_converter._snt_core import (
    _normalise_block_identifier,
    detect_scale_from_prj_blocks,
    detect_scale_from_grid_labels,
)


def _text(value, x, y):
    return {"model_idx": 0, "type": 18, "text": value,
            "origin": (x, y, 0.0)}


def test_grid_pair_in_filename_corrects_bridge_scale():
    elements = [
        _text("51000_216000", 512_500_000.0, 2_157_500_000.0),
        _text("tile+52000-215500.las", 522_500_000.0, 2_152_500_000.0),
    ]
    assert detect_scale_from_grid_labels(elements, 0, 1000.0) == 10000.0


def test_serial_number_without_separator_is_not_a_scale_clue():
    elements = [_text("DJ2010103003789", 512_500_000.0, 2_157_500_000.0)]
    assert detect_scale_from_grid_labels(elements, 0, 1000.0) == 1000.0


def test_special_characters_around_grid_pair_are_supported():
    elements = [
        _text("area#51000~216000!", 512_500_000.0, 2_157_500_000.0),
        _text("[52000/215500]", 522_500_000.0, 2_152_500_000.0),
    ]
    assert detect_scale_from_grid_labels(elements, 0, 1000.0) == 10000.0


def test_numeric_project_prefix_does_not_replace_correct_sdk_scale():
    elements = [
        _text("8114_587500_5917000", 58_775_000.0, 591_725_000.0),
        _text("8114_588000_5917500.laz", 58_825_000.0, 591_775_000.0),
    ]
    assert detect_scale_from_grid_labels(elements, 0, 100.0) == 100.0


def test_prj_coordinates_are_primary_with_unrelated_prj_filename(tmp_path):
    dgn_path = tmp_path / "drawing with symbols #1.dgn"
    dgn_path.write_bytes(b"")
    (tmp_path / "tiles arbitrary name.prj").write_text(
        "Block ZONE-A_587500_5917000.laz\n"
        "GroupFirst=1\nGroupCount=2\n"
        "  587500 5917000\n  588000 5917000\n"
        "  588000 5917500\n  587500 5917500\n",
        encoding="utf-8",
    )
    elements = [_text(
        "zone-a_587500_5917000", 58_775_000.0, 591_725_000.0
    )]
    assert detect_scale_from_prj_blocks(elements, 0, dgn_path) == 100.0


def test_block_identifier_normalisation_handles_unicode_and_extensions():
    assert (
        _normalise_block_identifier("Folder/Z\u00d6NE # 4_500_600.LAZ")
        == _normalise_block_identifier("z\u00f6ne # 4_500_600")
    )


def test_prj_first_matching_supports_legacy_windows_special_characters(tmp_path):
    dgn_path = tmp_path / "drawing.dgn"
    dgn_path.write_bytes(b"")
    prj_text = (
        "Block Z\u00d6NE # 4_587500_5917000.laz\n"
        "  587500 5917000\n  588000 5917000\n"
        "  588000 5917500\n  587500 5917500\n"
    )
    (tmp_path / "legacy.prj").write_bytes(prj_text.encode("cp1252"))
    elements = [_text(
        "z\u00f6ne # 4_587500_5917000", 58_775_000.0, 591_725_000.0
    )]
    assert detect_scale_from_prj_blocks(elements, 0, dgn_path) == 100.0
