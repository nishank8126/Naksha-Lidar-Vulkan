from types import SimpleNamespace
import threading
import time

import numpy as np
import vtk
from PySide6.QtCore import QCoreApplication, QTimer

from gui.gis import raster_lod as lod
from gui.gis.raster_properties import _read_source_bands


def make_actor():
    actor = vtk.vtkActor()
    plane = vtk.vtkPlaneSource()
    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputConnection(plane.GetOutputPort())
    actor.SetMapper(mapper)
    actor.SetTexture(vtk.vtkTexture())
    actor._raster_lod_meta = dict(path="sample.tif", eligible=True,
                                native_bounds=(0, 100, 0, 100),
                                native_size=(100, 100), z=0)
    return actor


def test_pixel_window_snaps_world_extent():
    win = lod._pixel_window_for_world((0, 100, 0, 100), (100, 100),
                                       (0.2, 10.2, 20.2, 30.2), 0)
    assert (win["wx0"], win["wx1"], win["wy0"], win["wy1"]) == (0, 11, 20, 31)


def test_native_resolution_and_edges_reused(monkeypatch):
    actor = make_actor()
    style = {"resampling": "nearest"}
    monkeypatch.setattr(lod, "_style_for", lambda *args: style)
    app = SimpleNamespace()
    view = (-10, 110, -10, 110, 1200, 1200)
    request = lod._request_for(app, actor, view)
    _, meta, window, w, h, _ = request
    assert (w, h) == (100, 100)
    meta["_last_window"] = dict(window, out_w=w, out_h=h)
    meta["_last_style"] = style
    assert lod._request_for(app, actor, view) is None
    assert lod._request_for(app, actor, (30, 40, 30, 40, 1200, 1200)) is None
    actor.VisibilityOff()
    meta.pop("_last_window")
    assert lod._request_for(app, actor, view) is None


def test_worker_does_not_block_gui_and_drops_stale_result(monkeypatch):
    qt = QCoreApplication.instance() or QCoreApplication([])
    actor = make_actor()
    app = SimpleNamespace(geotiff_actors=[actor], _raster_lod_timer=QTimer(),
                          vtk_widget=SimpleNamespace(render=lambda: None))
    monkeypatch.setattr(lod, "_visible_world_bounds", lambda app: (0, 100, 0, 100, 100, 100))
    monkeypatch.setattr(lod, "_style_for", lambda *args: {})
    entered, release = threading.Event(), threading.Event()
    worker_threads, applied = [], []
    def read(*args):
        worker_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        return np.zeros((100, 100, 3), dtype=np.uint8)
    monkeypatch.setattr(lod, "_load_window_texture", read)
    monkeypatch.setattr(lod, "_apply_texture", lambda *args: applied.append(threading.get_ident()) or True)
    loader = lod._Loader(app)
    try:
        loader.start()
        first = loader.future
        assert entered.wait(2)
        loader.start()
        assert loader.future is first
        ticks = []
        QTimer.singleShot(0, lambda: ticks.append(True))
        qt.processEvents()
        assert ticks and not first.done()
        loader.generation += 1
        app._raster_lod_timer.start(10000)
        release.set()
        first.result(timeout=3)
        loader.finish()
        assert not applied
        loader.start()
        loader.future.result(timeout=3)
        loader.finish()
        assert applied == [threading.get_ident()]
        assert all(t != threading.get_ident() for t in worker_threads)
    finally:
        release.set()
        loader.close()
        app._raster_lod_timer.stop()


def test_window_read_honors_nearest(tmp_path):
    import rasterio
    from rasterio.transform import from_origin
    path = tmp_path / "checker.tif"
    data = np.array([[0, 255], [255, 0]], dtype=np.uint8)
    with rasterio.open(path, "w", driver="GTiff", width=2, height=2, count=3,
                       dtype="uint8", transform=from_origin(0, 2, 1, 1)) as dst:
        dst.write(np.stack([data] * 3))
    bands, count = _read_source_bands(str(path), {"resampling": "nearest"}, out_shape=(7, 7))
    assert count == 3
    assert set(np.unique(bands[1])) == {0, 255}
    smooth, _ = _read_source_bands(str(path), {"resampling": "bilinear"}, out_shape=(7, 7))
    assert len(np.unique(smooth[1])) > 2


def test_failed_files_do_not_starve_other_layers(monkeypatch):
    qt = QCoreApplication.instance() or QCoreApplication([])
    actors = [make_actor() for _ in range(3)]
    for i, actor in enumerate(actors):
        actor._raster_lod_meta["path"] = str(i)
    app = SimpleNamespace(geotiff_actors=actors, _raster_lod_timer=QTimer(),
                          vtk_widget=SimpleNamespace(render=lambda: None))
    monkeypatch.setattr(lod, "_visible_world_bounds", lambda app: (0, 100, 0, 100, 100, 100))
    monkeypatch.setattr(lod, "_style_for", lambda *args: {})
    reads = []
    def read(path, *args):
        reads.append(path)
        if path != "2":
            raise OSError("unavailable")
        return np.zeros((100, 100, 3), dtype=np.uint8)
    monkeypatch.setattr(lod, "_load_window_texture", read)
    loader = lod._Loader(app)
    try:
        for _ in range(6):
            loader.start()
            if loader.future is None:
                break
            try:
                loader.future.result(timeout=3)
            except OSError:
                pass
            loader.finish()
        assert reads == ["0", "1", "2"]
        assert actors[2]._raster_lod_meta.get("_last_window")
        assert loader.future is None
    finally:
        loader.close()


def test_import_defers_background_texture_mutation(monkeypatch):
    qt = QCoreApplication.instance() or QCoreApplication([])
    app = SimpleNamespace(_geotiff_import_in_progress=True, _raster_lod_timer=QTimer())
    loader = lod._Loader(app)
    try:
        loader.start()
        assert loader.future is None
        assert app._raster_lod_timer.isActive()
    finally:
        loader.close()
        app._raster_lod_timer.stop()


def test_contrast_limits_are_stable_between_crops(tmp_path):
    import rasterio
    from rasterio.transform import from_origin
    path = tmp_path / "gradient.tif"
    data = np.tile(np.arange(100, dtype=np.uint8), (100, 1))
    with rasterio.open(path, "w", driver="GTiff", width=100, height=100, count=3,
                       dtype="uint8", transform=from_origin(0, 100, 1, 1)) as dst:
        dst.write(np.stack([data] * 3))
    full = dict(col_off=0, row_off=0, width=100, height=100)
    crop = dict(col_off=40, row_off=0, width=20, height=100)
    style = {"enhancement": "minmax", "resampling": "nearest"}
    a = lod._load_window_texture(str(path), full, 100, 100, style)
    b = lod._load_window_texture(str(path), crop, 20, 100, style)
    np.testing.assert_array_equal(a[:, 40:60], b)


def test_same_file_instances_have_independent_styles():
    first, second = make_actor(), make_actor()
    app = SimpleNamespace(gis_layers=[
        dict(kind="raster", path="sample.tif", actors=[first], style={"gamma": 1}),
        dict(kind="raster", path="sample.tif", actors=[second], style={"gamma": 2})])
    assert lod._style_for(app, "sample.tif", first) == {"gamma": 1}
    assert lod._style_for(app, "sample.tif", second) == {"gamma": 2}


def test_navigation_defers_finished_texture_upload(monkeypatch):
    from concurrent.futures import Future
    qt = QCoreApplication.instance() or QCoreApplication([])
    actor = make_actor()
    manager = SimpleNamespace(_interaction_active=True)
    app = SimpleNamespace(gpu_render_manager=manager, geotiff_actors=[actor],
                          _raster_lod_timer=QTimer(), vtk_widget=SimpleNamespace(render=lambda: None))
    loader = lod._Loader(app)
    view = (0, 100, 0, 100, 100, 100)
    monkeypatch.setattr(lod, "_visible_world_bounds", lambda app: view)
    monkeypatch.setattr(lod, "_style_for", lambda *args: {})
    calls = []
    monkeypatch.setattr(lod, "_apply_texture", lambda *args: calls.append(True) or True)
    try:
        loader.start()
        assert loader.future is None
        assert app._raster_lod_timer.isActive()
        request = lod._request_for(app, actor, view)
        future = Future()
        future.set_result(np.zeros((100, 100, 3), dtype=np.uint8))
        loader.future = future
        loader.request = (request, view, loader.generation, ())
        loader.finish()
        assert loader.future is future and not calls
        manager._interaction_active = False
        loader.finish()
        assert loader.future is None and calls == [True]
    finally:
        loader.close()


def test_camera_burst_checks_coverage_once_per_frame(monkeypatch):
    qt = QCoreApplication.instance() or QCoreApplication([])
    rw = vtk.vtkRenderWindow()
    renderer = vtk.vtkRenderer()
    rw.AddRenderer(renderer)
    app = SimpleNamespace(vtk_widget=SimpleNamespace(renderer=renderer, GetRenderWindow=lambda: rw),
                          geotiff_actors=[])
    lod.ensure_installed(app)
    calls = []
    monkeypatch.setattr(lod, "_visible_world_bounds", lambda app: calls.append(True) or None)
    cam = renderer.GetActiveCamera()
    try:
        for i in range(30):
            cam.SetParallelScale(i + 2)
        assert calls == []
        rw.InvokeEvent("StartEvent")
        assert calls == [True]
        rw.InvokeEvent("StartEvent")
        assert calls == [True]
        loader = app._raster_lod_loader
        generation = loader.generation
        loader.close()
        loader.close()
        cam.SetParallelScale(100)
        assert loader.generation == generation
    finally:
        app._raster_lod_loader.close()


def test_camera_navigation_keeps_detail_until_frame_coverage_check(monkeypatch):
    qt = QCoreApplication.instance() or QCoreApplication([])
    rw = vtk.vtkRenderWindow()
    renderer = vtk.vtkRenderer()
    rw.AddRenderer(renderer)
    actor = make_actor()
    actor._raster_lod_meta.update(
        _last_window={"wx0": 20, "wx1": 40, "wy0": 20, "wy1": 40,
                      "out_w": 100, "out_h": 100},
        _preview_image=vtk.vtkImageData(),
    )
    app = SimpleNamespace(
        vtk_widget=SimpleNamespace(renderer=renderer,
                                   GetRenderWindow=lambda: rw),
        geotiff_actors=[actor],
    )
    restored = []
    monkeypatch.setattr(
        lod, "_restore_overview",
        lambda current: restored.append(current) or current._raster_lod_meta.pop(
            "_last_window", None
        ) is not None,
    )
    lod.ensure_installed(app)
    try:
        renderer.GetActiveCamera().SetParallelScale(25)
        assert restored == []
        assert "_last_window" in actor._raster_lod_meta
        rw.InvokeEvent("StartEvent")
        assert restored == [actor]
        assert "_last_window" not in actor._raster_lod_meta
    finally:
        app._raster_lod_loader.close()


def test_clipping_only_camera_change_preserves_sharp_detail(monkeypatch):
    qt = QCoreApplication.instance() or QCoreApplication([])
    rw = vtk.vtkRenderWindow()
    renderer = vtk.vtkRenderer()
    rw.AddRenderer(renderer)
    actor = make_actor()
    actor._raster_lod_meta.update(
        _last_window={"wx0": 0, "wx1": 100, "wy0": 0, "wy1": 100,
                      "out_w": 1000, "out_h": 1000},
        _preview_image=vtk.vtkImageData(),
    )
    app = SimpleNamespace(
        vtk_widget=SimpleNamespace(renderer=renderer,
                                   GetRenderWindow=lambda: rw),
        geotiff_actors=[actor],
    )
    restored = []
    monkeypatch.setattr(lod, "_restore_overview",
                        lambda current: restored.append(current) or True)
    lod.ensure_installed(app)
    try:
        generation = app._raster_lod_loader.generation
        renderer.GetActiveCamera().SetClippingRange(0.25, 5000.0)
        assert restored == []
        assert app._raster_lod_loader.generation == generation
        assert "_last_window" in actor._raster_lod_meta
    finally:
        app._raster_lod_loader.close()


def test_zoom_inside_loaded_crop_keeps_sharp_texture(monkeypatch):
    qt = QCoreApplication.instance() or QCoreApplication([])
    rw = vtk.vtkRenderWindow()
    rw.SetSize(100, 100)
    renderer = vtk.vtkRenderer()
    rw.AddRenderer(renderer)
    camera = renderer.GetActiveCamera()
    camera.SetParallelProjection(True)
    camera.SetPosition(30, 30, 10)
    camera.SetFocalPoint(30, 30, 0)
    camera.SetViewUp(0, 1, 0)
    camera.SetParallelScale(5)
    actor = make_actor()
    actor._raster_lod_meta.update(
        _last_window={"wx0": 20, "wx1": 40, "wy0": 20, "wy1": 40,
                      "out_w": 1000, "out_h": 1000},
        _preview_image=vtk.vtkImageData(),
    )
    app = SimpleNamespace(
        vtk_widget=SimpleNamespace(renderer=renderer, GetRenderWindow=lambda: rw),
        geotiff_actors=[actor],
    )
    restored = []
    monkeypatch.setattr(lod, "_restore_overview",
                        lambda current: restored.append(current) or True)
    lod.ensure_installed(app)
    try:
        camera.SetParallelScale(4)
        rw.InvokeEvent("StartEvent")
        assert restored == []
        assert "_last_window" in actor._raster_lod_meta
    finally:
        app._raster_lod_loader.close()


def test_default_orthophoto_style_adds_controlled_clarity():
    from gui.gis.raster_properties import default_raster_style

    style = default_raster_style(3)
    assert 0 < style["brightness"] <= 5
    assert 0 < style["contrast"] <= 10
    assert 1.0 < style["gamma"] <= 1.1
    assert 0 < style["saturation"] <= 20
    assert style["resampling"] == "nearest"
