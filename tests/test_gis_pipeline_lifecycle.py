import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QToolButton, QTreeWidget, QWidget

from gui.gis import gis_layers
from gui.gis import raster_lod
from gui.gis.gdb import reader


class _Actor:
    def __init__(self, bounds=(0, 1, 0, 1, 0, 0)):
        self.visible = 1
        self.bounds = bounds

    def SetVisibility(self, value):
        self.visible = int(value)

    def GetVisibility(self):
        return self.visible

    def GetMapper(self):
        return None

    def GetBounds(self):
        return self.bounds


def _entry(layer_id, actor, **extra):
    value = {
        "id": layer_id,
        "name": f"Layer {layer_id}",
        "path": "",
        "kind": "vector",
        "fmt": "gdb",
        "actors": [actor],
        "visible": True,
        "opacity": 1.0,
        "geom": "polygon",
    }
    value.update(extra)
    return value


def test_hide_show_all_preserves_qt_tree_items_and_batches_render():
    qt = QApplication.instance() or QApplication([])
    owner = QWidget()
    renders = []
    owner.vtk_widget = SimpleNamespace(render=lambda: renders.append(1))
    actor_a = _Actor()
    child_a, child_b = _Actor(), _Actor()
    owner.gis_layers = [
        _entry(1, actor_a),
        _entry(
            2,
            child_a,
            actors=[child_a, child_b],
            sub_layers={
                "a": {"label": "A", "actors": [child_a], "color": "#ff0000"},
                "b": {"label": "B", "actors": [child_b], "color": "#00ff00"},
            },
        ),
    ]

    panel = gis_layers.GisLayersDock.create(owner)
    tree = panel.findChild(QTreeWidget, "occTree")
    eye = next(
        button
        for button in panel.findChildren(QToolButton)
        if "Show / hide all" in button.toolTip()
    )
    original_item = tree.topLevelItem(0)
    tree.setCurrentItem(original_item)

    eye.click()
    qt.processEvents()
    assert tree.topLevelItem(0) is original_item
    assert actor_a.visible == child_a.visible == child_b.visible == 0
    assert len(renders) == 1

    eye.click()
    qt.processEvents()
    assert tree.topLevelItem(0) is original_item
    assert actor_a.visible == child_a.visible == child_b.visible == 1
    assert len(renders) == 2

    panel.deleteLater()
    owner.deleteLater()
    qt.processEvents()


def test_nested_gis_batch_flushes_one_combined_zoom_and_render(monkeypatch):
    renders = []
    camera_calls = []

    class Renderer:
        def ResetCamera(self, bounds):
            camera_calls.append(tuple(bounds))

        def ResetCameraClippingRange(self):
            pass

    app = SimpleNamespace(
        vtk_widget=SimpleNamespace(render=lambda: renders.append(1)),
    )
    entries = [
        _entry(1, _Actor((0, 2, 1, 3, 0, 0))),
        _entry(2, _Actor((-5, -1, 10, 20, 0, 2))),
    ]
    monkeypatch.setattr(gis_layers, "_main_renderer", lambda _app: Renderer())
    monkeypatch.setattr(
        gis_layers,
        "_apply_order",
        lambda obj: setattr(obj, "_gis_batch_order_pending", False),
    )

    gis_layers.suspend_layer_panel_refresh(app)
    gis_layers.suspend_layer_panel_refresh(app)
    gis_layers._render(app)
    gis_layers.zoom_to_gis_entries(app, entries)
    app._gis_batch_order_pending = True

    gis_layers.resume_layer_panel_refresh(app)
    assert renders == []
    gis_layers.resume_layer_panel_refresh(app)

    # The zoom/render flush is deferred one tick (QTimer.singleShot(0, ...))
    # so it never re-enters Qt's widget/paint machinery from inside the
    # caller's still-live event - pump the loop once to let it fire.
    (QApplication.instance() or QApplication([])).processEvents()

    assert renders == [1]
    assert camera_calls == [(-5, 2, 1, 20, 0, 2)]
    assert app._gis_batch_update_depth == 0
    assert app._gis_batch_zoom_entries == []


def test_non_finite_gdb_coordinates_are_rejected_before_vtk():
    assert reader._finite_xyz((1, 2, 3)) == (1.0, 2.0, 3.0)
    assert reader._finite_xyz((float("nan"), 2, 3)) is None
    assert reader._finite_xyz((1, float("inf"), 3)) is None
    assert reader._finite_xyz((1, 2, float("inf"))) == (1.0, 2.0, 0.0)
    assert reader._sanitize_ring_points(
        [(0, 0, 0), (float("nan"), 1, 0), (1, 0, 0), (0, 0, 0)]
    ) == [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)]


def test_raster_request_handles_invalid_pixel_window(monkeypatch):
    actor = _Actor()
    actor._raster_lod_meta = {
        "eligible": True,
        "native_bounds": (0.0, 10.0, 0.0, 10.0),
        "native_size": (100, 100),
        "path": "dummy.tif",
    }
    monkeypatch.setattr(raster_lod, "_style_for", lambda *_args: {})
    monkeypatch.setattr(raster_lod, "_pixel_window_for_world", lambda *_args: None)

    request = raster_lod._request_for(
        SimpleNamespace(), actor, (0.0, 5.0, 0.0, 5.0, 500, 500)
    )
    assert request is None
