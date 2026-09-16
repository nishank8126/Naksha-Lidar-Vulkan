import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from pyproj import CRS

from gui.gis.gdb.writer import FieldSpec, create_feature_class, create_file_gdb
from gui.gis.gis_layers import AttributeTableDialog
from osgeo import gdal


def _schema(gdb_path, layer_name):
    ds = gdal.OpenEx(str(gdb_path), gdal.OF_VECTOR | gdal.OF_READONLY)
    assert ds is not None
    layer = ds.GetLayerByName(layer_name)
    assert layer is not None
    definition = layer.GetLayerDefn()
    result = (
        layer.GetFIDColumn(),
        layer.GetGeometryColumn(),
        [definition.GetFieldDefn(i).GetName() for i in range(definition.GetFieldCount())],
    )
    layer = None
    ds = None
    return result


def test_filegdb_managed_geometry_fields_and_attribute_headers(tmp_path):
    qt = QApplication.instance() or QApplication([])
    gdb_path = tmp_path / "managed.gdb"
    assert create_file_gdb(str(gdb_path)).success
    crs = CRS.from_epsg(32643)

    assert create_feature_class(str(gdb_path), "points", geometry="Point", crs=crs).success
    assert create_feature_class(str(gdb_path), "lines", geometry="Polyline", crs=crs).success
    assert create_feature_class(
        str(gdb_path), "polygons", geometry="Polygon", crs=crs,
        fields=[FieldSpec("Owner", "String", 80)],
    ).success

    assert _schema(gdb_path, "points") == ("OBJECTID", "SHAPE", [])
    assert _schema(gdb_path, "lines") == ("OBJECTID", "SHAPE", ["Shape_Length"])
    polygon_fid, polygon_shape, polygon_fields = _schema(gdb_path, "polygons")
    assert polygon_fid == "OBJECTID"
    assert polygon_shape == "SHAPE"
    assert set(polygon_fields) == {"Shape_Length", "Shape_Area", "Owner"}

    dialog = AttributeTableDialog(
        None,
        {"name": "polygons", "path": str(gdb_path), "gdb_layer_name": "polygons"},
    )
    assert dialog.headers == ["OBJECTID", "SHAPE", "Shape_Length", "Shape_Area", "Owner"]
    dialog.close()
    dialog.deleteLater()
    qt.processEvents()