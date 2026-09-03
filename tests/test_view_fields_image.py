import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import laspy
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from gui.dialogs.view_fields_dialog import (
    IMAGE_FIELD_LABEL,
    IMAGE_SOURCE_KEY,
    ViewFieldsDialog,
    find_image_dimension_name,
)
from gui.dialogs.view_fields_table import (
    PointTableModel,
    ViewFieldsTableDialog,
    _ExtraFieldLoader,
)


def _qt_app():
    return QApplication.instance() or QApplication([])


def test_image_dimension_discovery_supports_vendor_aliases():
    assert find_image_dimension_name(["intensity", "image_id"]) == "image_id"
    assert find_image_dimension_name(["Photo Number"]) == "Photo Number"
    assert find_image_dimension_name(["camera_id"]) == "camera_id"
    assert find_image_dimension_name(["frame_index"]) == "frame_index"
    assert find_image_dimension_name(["intensity", "scan_angle"]) is None


def test_image_column_uses_available_per_point_dimension():
    model = PointTableModel({
        "xyz": np.zeros((2, 3), dtype=float),
        "image_id": np.asarray([17, 23]),
    })

    headers = [
        model.headerData(column, Qt.Horizontal, Qt.DisplayRole)
        for column in range(model.columnCount())
    ]
    image_column = headers.index(IMAGE_FIELD_LABEL)
    assert model.data(model.index(1, image_column), Qt.DisplayRole) == "23"


def test_pending_image_selection_survives_async_column_refresh():
    data = {"xyz": np.zeros((1, 3), dtype=float)}
    model = PointTableModel(data)

    assert model.set_visible_columns([IMAGE_FIELD_LABEL])
    assert model.columnCount() == 0

    data["image_number"] = np.asarray([42])
    model.refresh_columns()
    assert model.columnCount() == 1
    assert model.headerData(0, Qt.Horizontal, Qt.DisplayRole) == IMAGE_FIELD_LABEL


def test_extra_field_loader_reads_image_id_dimension(tmp_path):
    path = tmp_path / "with_image_id.las"
    las = laspy.LasData(laspy.LasHeader(point_format=3, version="1.2"))
    las.x = np.asarray([0.0, 1.0])
    las.y = np.asarray([0.0, 1.0])
    las.z = np.asarray([0.0, 1.0])
    las.add_extra_dim(laspy.ExtraBytesParams(name="image_id", type=np.uint32))
    las.image_id = np.asarray([101, 202], dtype=np.uint32)
    las.write(path)

    loaded = []
    loader = _ExtraFieldLoader(str(path), [IMAGE_SOURCE_KEY])
    loader.finished_loading.connect(loaded.append)
    loader.run()

    assert len(loaded) == 1
    assert loaded[0]["image_id"].tolist() == [101, 202]


def test_view_fields_dialog_is_freely_resizable_and_enables_image():
    _qt_app()
    dialog = ViewFieldsDialog(
        available_dimensions=["classification", "image_id"]
    )
    try:
        assert dialog.minimumWidth() == 0
        assert dialog.minimumHeight() == 0
        assert dialog.isSizeGripEnabled()
        assert dialog.windowFlags() & Qt.WindowMaximizeButtonHint

        image_items = dialog.fields_list.findItems(
            IMAGE_FIELD_LABEL, Qt.MatchExactly
        )
        assert len(image_items) == 1
        assert image_items[0].flags() & Qt.ItemIsEnabled
        assert "image_id" in image_items[0].toolTip()
    finally:
        dialog.close()


def test_main_fields_table_is_freely_resizable():
    _qt_app()
    dialog = ViewFieldsTableDialog(
        filename=None,
        app_data={"xyz": np.zeros((0, 3), dtype=float)},
    )
    try:
        assert dialog.minimumWidth() == 0
        assert dialog.minimumHeight() == 0
        assert dialog.isSizeGripEnabled()
        assert dialog.windowFlags() & Qt.WindowMaximizeButtonHint
    finally:
        dialog.close()
