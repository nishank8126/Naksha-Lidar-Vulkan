from types import SimpleNamespace

import pytest

from gui.measurement_tools import MeasurementTool
from gui import measure_settings_dialog
from gui.snt_attachment import build_snt_block_polygons
from gui.element_select_tool import ElementSelectTool


def _tool_with_polygons(polygons):
    tool = MeasurementTool.__new__(MeasurementTool)
    tool.app = SimpleNamespace(snt_block_polygons=polygons)
    return tool


def test_measurement_temporarily_owns_left_click_and_restores_pan():
    style = SimpleNamespace(
        OnMiddleButtonUp=lambda: None,
        OnLeftButtonUp=lambda: None,
    )
    tool = MeasurementTool.__new__(MeasurementTool)
    tool.app = SimpleNamespace(panning_button="left")
    tool.interactor = SimpleNamespace(GetInteractorStyle=lambda: style)
    tool._suspended_pan_button = None

    tool._suspend_left_click_pan()

    assert tool.app.panning_button == "scroll"
    assert tool._suspended_pan_button == "left"

    tool._restore_left_click_pan()

    assert tool.app.panning_button == "left"
    assert tool._suspended_pan_button is None


def test_element_selection_temporarily_owns_left_click_and_restores_pan():
    style = SimpleNamespace(
        OnMiddleButtonUp=lambda: None,
        OnLeftButtonUp=lambda: None,
    )
    tool = ElementSelectTool.__new__(ElementSelectTool)
    tool.app = SimpleNamespace(panning_button="left")
    tool.interactor = SimpleNamespace(GetInteractorStyle=lambda: style)
    tool._suspended_pan_button = None

    tool._suspend_left_click_pan()

    assert tool.app.panning_button == "scroll"
    assert tool._suspended_pan_button == "left"

    tool._restore_left_click_pan()

    assert tool.app.panning_button == "left"
    assert tool._suspended_pan_button is None


def test_grid_index_includes_only_snt_grid_geometry_with_exact_area():
    tool = _tool_with_polygons([
        {
            "source": "snt_grid",
            "grid_name": "51000_216000",
            "points_2d": [(0, 0), (500, 0), (500, 500), (0, 500)],
            "snt_filename": "project.snt",
            "poly_layer": "GRID",
        },
        {
            "source": "prj_authority",
            "grid_name": "M-34-authoritative",
            "points_2d": [(1000, 0), (1500, 0), (1500, 500), (1000, 500)],
            "snt_filename": "project.snt",
            "prj_path": "project.prj",
            "poly_layer": "PRJ",
        },
        {
            "source": "prj",
            "grid_name": "block_file",
            "points_2d": [(0, 0), (10, 0), (10, 10), (0, 10)],
        },
        {
            "source": "snt_text",
            "grid_name": "reference",
            "points_2d": [(0, 0), (20, 0), (20, 20), (0, 20)],
        },
    ])

    grids = tool._load_grid_boundaries()

    assert len(grids) == 2
    assert grids[0]["label"] == "51000_216000"
    assert grids[0]["area"] == pytest.approx(250000.0)
    assert grids[0]["source_type"] == "snt_grid"
    assert grids[0]["source_layer"] == "GRID"
    assert grids[1]["label"] == "M-34-authoritative"
    assert grids[1]["area"] == pytest.approx(250000.0)
    assert grids[1]["index_source"] == "prj_authority"
    assert grids[1]["source_prj"] == "project.prj"


def test_grid_world_pick_uses_most_specific_overlapping_cell():
    tool = _tool_with_polygons([
        {
            "source": "snt_grid",
            "grid_name": "large",
            "points_2d": [(0, 0), (100, 0), (100, 100), (0, 100)],
        },
        {
            "source": "snt_grid",
            "grid_name": "small",
            "points_2d": [(10, 10), (20, 10), (20, 20), (10, 20)],
        },
    ])

    selected = tool._find_grid_at_world_point((15.0, 15.0, 0.0))

    assert selected is not None
    assert selected["label"] == "small"
    assert selected["area"] == pytest.approx(100.0)


def test_block_index_accepts_current_prj_authority_records():
    tool = _tool_with_polygons([{
        "source": "prj_authority",
        "grid_name": "M-34-block",
        "points_2d": [(0, 0), (20, 0), (20, 10), (0, 10)],
        "snt_filename": "project.snt",
        "prj_path": "project.prj",
    }])

    blocks = tool._parse_attached_snt_blocks()

    assert len(blocks) == 1
    assert blocks[0]["label"] == "M-34-block"
    assert blocks[0]["area"] == pytest.approx(200.0)
    assert blocks[0]["source_prj"] == "project.prj"


def test_grid_measurement_state_preserves_grid_identity_and_source():
    tool = MeasurementTool.__new__(MeasurementTool)
    tool.measurements = [{
        "type": "measure_grid_area",
        "block_name": "51000_216000",
        "points": [(0.0, 0.0, 5.0), (10.0, 0.0, 5.0), (10.0, 10.0, 5.0)],
        "area": 50.0,
        "source_type": "snt_grid",
        "source_snt": "project.snt",
        "source_layer": "GRID",
    }]

    state = tool._capture_state()

    assert state == [{
        "type": "measure_grid_area",
        "points": [(0.0, 0.0, 5.0), (10.0, 0.0, 5.0), (10.0, 10.0, 5.0)],
        "block_name": "51000_216000",
        "area": 50.0,
        "source_type": "snt_grid",
        "source_prj": None,
        "source_snt": "project.snt",
        "source_layer": "GRID",
    }]


def test_grid_settings_are_persisted_separately_from_block(monkeypatch):
    values = {}

    class _Settings:
        def __init__(self, *_args):
            pass

        def value(self, key, default=None):
            return values.get(key, default)

        def setValue(self, key, value):
            values[key] = value

    monkeypatch.setattr(measure_settings_dialog, "QSettings", _Settings)
    style = measure_settings_dialog.load_measure_settings()
    style["block"].update({"unit": "m", "label_font_size": 20})
    style["grid"].update({"unit": "km", "label_font_size": 31})

    measure_settings_dialog.save_measure_settings(style)
    loaded = measure_settings_dialog.load_measure_settings()

    assert loaded["block"] == {"unit": "m", "label_font_size": 20}
    assert loaded["grid"] == {"unit": "km", "label_font_size": 31}


def test_nonstandard_snt_level_with_repeated_labeled_cells_is_indexed(tmp_path):
    entities = []
    for index in range(4):
        x0 = float(index * 10)
        entities.extend([
            {
                "type": "polyline",
                "layer": "PART_01_ALLOTMENT",
                "closed": True,
                "points": [
                    (x0, 0.0), (x0 + 10.0, 0.0),
                    (x0 + 10.0, 10.0), (x0, 10.0),
                ],
            },
            {
                "type": "text",
                "layer": "PART_01_ALLOTMENT",
                "position": (x0 + 5.0, 5.0, 0.0),
                "text": f"M-34-test-{index}",
            },
        ])

    app = SimpleNamespace()
    build_snt_block_polygons(app, entities, str(tmp_path / "classified.snt"))

    assert len(app.snt_block_polygons) == 4
    assert {entry["source"] for entry in app.snt_block_polygons} == {"snt_grid"}
    assert {entry["poly_layer"] for entry in app.snt_block_polygons} == {
        "PART_01_ALLOTMENT"
    }
