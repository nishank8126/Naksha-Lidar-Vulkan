from gui.snt_attachment import (
    build_snt_block_polygons,
    _normalized_delivery_project_stem,
    _parse_snt_adjacent_prj_blocks,
)
from gui.grid_label_system import GridLabelManager
from types import SimpleNamespace


def test_classified_snt_stem_matches_base_project():
    assert _normalized_delivery_project_stem("3206_CLASS-1.snt") == "3206"
    assert _normalized_delivery_project_stem("3206_CLASS.snt") == "3206"
    assert _normalized_delivery_project_stem("3206_classification_2.snt") == "3206"
    assert _normalized_delivery_project_stem("unrelated.snt") == "unrelated"


def test_classified_snt_selects_matching_prj_among_multiple(tmp_path):
    snt_path = tmp_path / "3206_CLASS-1.snt"
    snt_path.write_bytes(b"")
    (tmp_path / "unrelated.prj").write_text(
        "Block wrong.laz\n0 0\n1 0\n1 1\n0 0\n",
        encoding="utf-8",
    )
    (tmp_path / "3206.prj").write_text(
        "Block M-34-61-D-b-3-4-3-4.laz\n"
        "GroupFirst=32000000\n"
        "GroupCount=1000000\n"
        "0 0\n10 0\n10 10\n0 10\n0 0\n",
        encoding="utf-8",
    )

    blocks = _parse_snt_adjacent_prj_blocks(str(snt_path))

    assert len(blocks) == 1
    assert blocks[0]["label"] == "M-34-61-D-b-3-4-3-4"
    assert blocks[0]["prj_path"] == str(tmp_path / "3206.prj")


def test_prj_authority_is_retained_when_snt_has_no_grid_polygons(tmp_path):
    snt_path = tmp_path / "3206_CLASS.snt"
    laz_path = tmp_path / "M-34-61-D-b-3-4-4-3.laz"
    app = SimpleNamespace(snt_block_polygons=[])
    prj_blocks = [{
        "label": "M-34-61-D-b-3-4-4-3",
        "block_file": laz_path.name,
        "points_2d": [(0, 0), (10, 0), (10, 10), (0, 10)],
        "file_path": str(laz_path),
        "prj_path": str(tmp_path / "3206.prj"),
    }]

    build_snt_block_polygons(app, [], str(snt_path), prj_blocks=prj_blocks)

    assert len(app.snt_block_polygons) == 1
    assert app.snt_block_polygons[0]["source"] == "prj_authority"
    manager = object.__new__(GridLabelManager)
    manager.app = app
    manager._ensure_snt_block_index = lambda: app.snt_block_polygons
    resolved = manager._resolve_primary_buffer_block(
        "M-34-61-D-b-3-4-4-3",
        str(snt_path),
        str(laz_path),
    )
    assert resolved is app.snt_block_polygons[0]


def test_complete_prj_index_survives_incidental_saved_snt_polygon(tmp_path):
    snt_path = tmp_path / "3206_CLASSS.snt"
    first_laz = tmp_path / "M-34-61-D-b-3-4-3-4.laz"
    selected_laz = tmp_path / "M-34-61-D-b-3-4-4-3.laz"
    app = SimpleNamespace(snt_block_polygons=[])
    incidental_entity = {
        "type": "polyline",
        "layer": "New Level",
        "points": [(100, 100), (110, 100), (110, 110), (100, 110), (100, 100)],
        "closed": True,
    }
    prj_blocks = [
        {
            "label": first_laz.stem,
            "block_file": first_laz.name,
            "points_2d": [(0, 0), (10, 0), (10, 10), (0, 10)],
            "file_path": str(first_laz),
        },
        {
            "label": selected_laz.stem,
            "block_file": selected_laz.name,
            "points_2d": [(20, 0), (30, 0), (30, 10), (20, 10)],
            "file_path": str(selected_laz),
        },
    ]

    build_snt_block_polygons(
        app,
        [incidental_entity],
        str(snt_path),
        prj_blocks=prj_blocks,
    )

    prj_entries = [entry for entry in app.snt_block_polygons if entry["source"] == "prj_authority"]
    assert {entry["grid_name"] for entry in prj_entries} == {first_laz.stem, selected_laz.stem}
    assert len([entry for entry in app.snt_block_polygons if entry["grid_name"] == selected_laz.stem]) == 1
    selected = next(entry for entry in prj_entries if entry["grid_name"] == selected_laz.stem)
    assert selected["file_path"] == str(selected_laz)
