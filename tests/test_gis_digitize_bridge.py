from pathlib import Path

import pytest
from pyproj import CRS, Transformer

from gui.gis.gdb.writer import FieldSpec, create_feature_class, create_file_gdb
from gui.gis.vector_writer import create_vector_dataset
from gui.gis.edit_bridge import (
    FeatureWriteResult,
    GISDigitizeBridge,
    drawing_geometry_kind,
    drawing_to_ogr_geometry,
    insert_drawing_feature,
    inspect_edit_target,
)
from osgeo import gdal, ogr


class _App:
    def __init__(self, epsg):
        self.canvas_crs = CRS.from_epsg(epsg)


def _entry(path, layer_name, kind):
    return {
        "id": 1,
        "path": str(path),
        "gdb_path": str(path),
        "source_layer": layer_name,
        "gdb_layer_name": layer_name,
        "kind": "vector",
        "geom": kind,
    }


def _read_layer(path, name):
    ds = gdal.OpenEx(str(path), gdal.OF_VECTOR | gdal.OF_READONLY)
    assert ds is not None
    layer = ds.GetLayerByName(name)
    assert layer is not None
    return ds, layer


def test_draw_geometry_kind_and_mismatch_rejection():
    assert drawing_geometry_kind({"type": "gispoint", "coords": [(1, 2, 0)]}) == "point"
    assert drawing_geometry_kind({"type": "curve", "coords": [(0, 0, 0), (1, 1, 0)]}) == "line"
    assert drawing_geometry_kind({"type": "rectangle", "coords": [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 0, 0)]}) == "polygon"
    with pytest.raises(ValueError, match="requires line"):
        drawing_to_ogr_geometry(
            {"type": "rectangle", "coords": [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 0, 0)]},
            ogr.wkbLineString,
        )


def test_generated_tool_types_are_classified_from_their_shape():
    """Offset/Parallel, Ortho and any future tool must still reach the writer."""
    ring = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0), (0, 0, 0)]
    chain = [(0, 0, 0), (10, 0, 0), (20, 5, 0)]
    assert drawing_geometry_kind({"type": "parallel_line", "coords": chain}) == "line"
    assert drawing_geometry_kind({"type": "parallel_polygon", "coords": ring}) == "polygon"
    assert drawing_geometry_kind({"type": "ortho_right", "coords": ring}) == "polygon"
    assert drawing_geometry_kind({"type": "centerline", "coords": chain}) == "line"
    # A brand-new tool with an unrecognised label falls back to the shape.
    assert drawing_geometry_kind({"type": "future_tool", "coords": ring}) == "polygon"
    assert drawing_geometry_kind({"type": "future_tool", "coords": chain}) == "line"


def test_draw_pipeline_persists_point_line_polygon_and_attributes(tmp_path):
    gdb = tmp_path / "edit.gdb"
    assert create_file_gdb(str(gdb)).success
    assert create_feature_class(str(gdb), "points", geometry="Point", crs=32643,
                                fields=[FieldSpec("code", "String", width=20)]).success
    assert create_feature_class(str(gdb), "roads", geometry="Polyline", crs=32643,
                                fields=[FieldSpec("name", "String", width=40)]).success
    assert create_feature_class(str(gdb), "buildings", geometry="Polygon", crs=32643,
                                fields=[FieldSpec("name", "String", width=40)]).success
    app = _App(32643)

    point = insert_drawing_feature(
        app, _entry(gdb, "points", "point"),
        {"type": "gispoint", "coords": [(500000, 2500000, 0)]}, {"code": "P1"},
    )
    line = insert_drawing_feature(
        app, _entry(gdb, "roads", "line"),
        {"type": "curve", "coords": [(500000, 2500000, 0), (500025, 2500030, 0), (500050, 2500040, 0)]},
        {"name": "Main Road"},
    )
    polygon = insert_drawing_feature(
        app, _entry(gdb, "buildings", "polygon"),
        {"type": "rectangle", "coords": [(500000, 2500000, 0), (500020, 2500000, 0),
                                             (500020, 2500010, 0), (500000, 2500010, 0),
                                             (500000, 2500000, 0)]},
        {"name": "Office"},
    )
    assert point.success, point.error
    assert line.success, line.error
    assert polygon.success, polygon.error

    ds, layer = _read_layer(gdb, "points")
    assert layer.GetFeatureCount() == 1
    feature = layer.GetNextFeature()
    assert feature.GetField("code") == "P1"
    assert feature.GetGeometryRef().GetGeometryName() in {"POINT", "MULTIPOINT"}
    feature = None
    ds = None

    ds, layer = _read_layer(gdb, "roads")
    feature = layer.GetNextFeature()
    assert feature.GetField("name") == "Main Road"
    assert feature.GetField("Shape_Length") > 0
    feature = None
    ds = None

    ds, layer = _read_layer(gdb, "buildings")
    feature = layer.GetNextFeature()
    assert feature.GetField("name") == "Office"
    assert feature.GetField("Shape_Length") == pytest.approx(60.0, rel=1e-5)
    assert feature.GetField("Shape_Area") == pytest.approx(200.0, rel=1e-5)
    feature = None
    ds = None


def test_project_coordinates_are_transformed_back_to_layer_crs(tmp_path):
    gdb = tmp_path / "crs_edit.gdb"
    assert create_file_gdb(str(gdb)).success
    assert create_feature_class(str(gdb), "wgs_points", geometry="Point", crs=4326).success
    x, y = Transformer.from_crs(4326, 32643, always_xy=True).transform(75.0, 23.0)
    result = insert_drawing_feature(
        _App(32643), _entry(gdb, "wgs_points", "point"),
        {"type": "gispoint", "coords": [(x, y, 0)]},
    )
    assert result.success, result.error
    ds, layer = _read_layer(gdb, "wgs_points")
    feature = layer.GetNextFeature()
    geom = feature.GetGeometryRef().Clone()
    feature = None
    assert geom.GetX() == pytest.approx(75.0, abs=1e-7)
    assert geom.GetY() == pytest.approx(23.0, abs=1e-7)
    ds = None


def test_target_schema_excludes_driver_managed_measure_fields(tmp_path):
    gdb = tmp_path / "schema.gdb"
    assert create_file_gdb(str(gdb)).success
    assert create_feature_class(str(gdb), "parcels", geometry="Polygon", crs=32643,
                                fields=[FieldSpec("owner", "String")]).success
    schema = inspect_edit_target(_entry(gdb, "parcels", "polygon"))
    assert schema["kind"] == "polygon"
    assert [field["name"] for field in schema["fields"]] == ["owner"]
@pytest.mark.parametrize(
    "format_key,extension",
    [
        ("shapefile", ".shp"),
        ("geojson", ".geojson"),
        ("geopackage", ".gpkg"),
        ("flatgeobuf", ".fgb"),
    ],
)
def test_catalog_spatial_formats_accept_draw_features(tmp_path, format_key, extension):
    result = create_vector_dataset(
        str(tmp_path / f"editable{extension}"),
        format_key=format_key,
        layer_name="features",
        geometry="Polygon",
        crs=CRS.from_epsg(32643),
        fields=[FieldSpec("name", "String")],
    )
    assert result.success, result.error
    entry = {
        "path": result.path,
        "source_layer": result.layer_name,
        "kind": "vector",
        "geom": "polygon",
    }
    schema = inspect_edit_target(entry)
    assert schema["kind"] == "polygon"
    # An empty GeoJSON has no schema of its own; the Catalog sidecar must be
    # merged back in so the user-defined fields survive a reopen.
    assert "name" in [field["name"] for field in schema["fields"]]
    write = insert_drawing_feature(
        _App(32643), entry,
        {"type": "rectangle", "coords": [(0, 0, 0), (10, 0, 0), (10, 10, 0),
                                           (0, 10, 0), (0, 0, 0)]},
        {"name": "feature"},
    )
    assert write.success, write.error
    ds, layer = _read_layer(result.path, result.layer_name)
    assert layer.GetFeatureCount() == 1
    assert layer.GetNextFeature().GetField("name") == "feature"
    ds = None


# ---------------------------------------------------------------------------
# Bridge orchestration (Draw -> OGR) with a stubbed digitizer/app.
# ---------------------------------------------------------------------------
class _FakeProperty:
    def __init__(self):
        self.color = None

    def SetColor(self, *rgb):
        self.color = tuple(rgb)


class _FakeActor:
    def __init__(self):
        self._property = _FakeProperty()

    def GetProperty(self):
        return self._property


class _FakeDigitizer:
    def __init__(self, drawing):
        self.drawings = [drawing]
        self.undo_stack = [object()]
        self.callbacks = []
        self.removed = []

    def add_drawing_finalized_callback(self, callback):
        if callback not in self.callbacks:
            self.callbacks.append(callback)

    def _remove_actor_from_overlay(self, actor):
        self.removed.append(actor)


class _FakePanel:
    def __init__(self):
        self.updated = None

    def update_feature_count(self, layer_id, subtype):
        self.updated = (layer_id, subtype)


class _FakeStatus:
    def __init__(self):
        self.messages = []

    def showMessage(self, text, *_args):
        self.messages.append(text)


class _FakeVTK:
    def __init__(self):
        self.renders = 0

    def render(self):
        self.renders += 1


class _BridgeApp:
    def __init__(self, entry, drawing):
        self.active_gis_edit_layer = entry
        self.active_gis_edit_subtype = None
        self.digitizer = _FakeDigitizer(drawing)
        self._gis_layers_panel = _FakePanel()
        self._attribute_tables = {}
        self.vtk_widget = _FakeVTK()
        self._status = _FakeStatus()

    def statusBar(self):
        return self._status


def _polygon_drawing():
    return {
        "type": "rectangle",
        "coords": [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 0, 0)],
        "actor": _FakeActor(),
    }


def test_bridge_commits_once_and_transfers_visual_to_native_layer(monkeypatch):
    drawing = _polygon_drawing()
    actor = drawing["actor"]
    entry = {"id": 7, "kind": "vector", "geom": "polygon", "actors": [],
             "feature_count": 0, "placeholder": True, "color": "#ff0000"}
    app = _BridgeApp(entry, drawing)

    monkeypatch.setattr("gui.gis.edit_bridge.inspect_edit_target", lambda _entry: {
        "path": "", "layer_name": "buildings", "kind": "polygon", "fields": [],
    })
    monkeypatch.setattr("gui.gis.edit_bridge.ask_feature_attributes",
                        lambda *_args: (True, {}))
    monkeypatch.setattr("gui.gis.edit_bridge.insert_drawing_feature",
                        lambda *_args, **_kwargs: FeatureWriteResult(True, fid=12))

    bridge = GISDigitizeBridge(app)
    assert bridge.install()
    assert len(app.digitizer.callbacks) == 1

    bridge.on_drawing_finalized(drawing)
    # A second notification for the same drawing must not double-write.
    bridge.on_drawing_finalized(drawing)

    assert entry["feature_count"] == 1
    assert entry["placeholder"] is False
    assert actor in entry["actors"]
    assert drawing not in app.digitizer.drawings
    assert drawing.get("_gis_feature_fid") == 12
    assert app._gis_layers_panel.updated == (7, None)
    # The committed shape keeps its actor; only the temporary undo entry is
    # dropped so the user cannot undo a datasource write.
    assert actor not in app.digitizer.removed
    assert app.digitizer.undo_stack == []
    assert app.vtk_widget.renders >= 1


def test_bridge_rejects_geometry_kind_mismatch_without_writing(monkeypatch):
    drawing = _polygon_drawing()
    entry = {"id": 8, "kind": "vector", "geom": "line", "actors": [],
             "feature_count": 0}
    app = _BridgeApp(entry, drawing)

    monkeypatch.setattr("gui.gis.edit_bridge.inspect_edit_target", lambda _entry: {
        "path": "", "layer_name": "roads", "kind": "line", "fields": [],
    })

    def _must_not_write(*_args, **_kwargs):
        raise AssertionError("insert must not be called on a geometry mismatch")

    monkeypatch.setattr("gui.gis.edit_bridge.insert_drawing_feature", _must_not_write)

    GISDigitizeBridge(app).on_drawing_finalized(drawing)

    assert drawing not in app.digitizer.drawings
    assert drawing["actor"] in app.digitizer.removed
    assert entry.get("feature_count") == 0
    assert any("roads" in message for message in app._status.messages)


def test_bridge_is_inert_without_an_active_edit_layer():
    drawing = _polygon_drawing()
    app = _BridgeApp(None, drawing)

    GISDigitizeBridge(app).on_drawing_finalized(drawing)

    # CAD / temporary drawing behaviour is untouched when no GIS target exists.
    assert drawing in app.digitizer.drawings
    assert app.digitizer.removed == []
    assert app._status.messages == []