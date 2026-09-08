"""Regression tests for the defects recorded in SNT_ORTHO_LIDAR_AUDIT.md.

These originally asserted the *broken* behavior as evidence of each defect.
Now that findings #2, #3, #4, #5 and #6 (resize / camera-replacement gaps)
are repaired, they assert the corrected behavior instead.

Run: chiru/Scripts/python.exe -m pytest audit/test_raster_audit.py -q
"""
from concurrent.futures import Future
from types import SimpleNamespace

import numpy as np
import vtk
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from gui.gis import raster_lod as lod, raster_properties

QT = QApplication.instance() or QApplication([])


def scene():
    rw = vtk.vtkRenderWindow()
    rw.SetSize(400, 400)
    renderer = vtk.vtkRenderer()
    rw.AddRenderer(renderer)
    cam = renderer.GetActiveCamera()
    cam.ParallelProjectionOn()
    cam.SetPosition(50, 50, 100)
    cam.SetFocalPoint(50, 50, 0)
    cam.SetViewUp(0, 1, 0)
    cam.SetParallelScale(10)
    plane = vtk.vtkPlaneSource()
    plane.SetOrigin(0, 0, 0)
    plane.SetPoint1(100, 0, 0)
    plane.SetPoint2(0, 100, 0)
    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputConnection(plane.GetOutputPort())
    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    preview = vtk.vtkImageData()
    preview.SetDimensions(10, 10, 1)
    preview.AllocateScalars(vtk.VTK_UNSIGNED_CHAR, 3)
    texture = vtk.vtkTexture()
    texture.SetInputData(preview)
    actor.SetTexture(texture)
    actor._raster_lod_meta = dict(path="audit.tif", eligible=True,
        native_bounds=(0, 100, 0, 100), native_size=(1000, 1000), z=0,
        _preview_image=preview)
    entry = dict(kind="raster", path="audit.tif", actors=[actor], style={})
    app = SimpleNamespace(geotiff_actors=[actor], gis_layers=[entry],
        vtk_widget=SimpleNamespace(renderer=renderer, GetRenderWindow=lambda: rw,
                                   render=lambda: None))
    return app, rw, cam, actor, entry


def test_transient_read_failure_retries_after_backoff(monkeypatch):
    app, _, _, actor, _ = scene()
    app._raster_lod_timer = QTimer()
    loader = lod._Loader(app)
    reads = []

    def flaky(*args):
        reads.append(True)
        if len(reads) == 1:
            raise OSError("temporary disk failure")
        return np.zeros((10, 10, 3), np.uint8)

    monkeypatch.setattr(lod, "_load_window_texture", flaky)
    clock = {"t": 0.0}
    monkeypatch.setattr(lod.time, "monotonic", lambda: clock["t"])
    try:
        loader.start()
        try:
            loader.future.result(timeout=3)
        except OSError:
            pass
        loader.finish()
        assert len(reads) == 1
        assert id(actor) in loader.failed_keys

        # Still inside the backoff window - no immediate retry.
        loader.start()
        assert loader.future is None

        # Advance the clock past the backoff and retry succeeds.
        clock["t"] += 5.0
        loader.start()
        assert loader.future is not None
        loader.future.result(timeout=3)
        loader.finish()
        assert len(reads) == 2
        assert id(actor) not in loader.failed_keys
    finally:
        loader.close()


def test_style_change_keeps_crop_coverage_until_replacement():
    app, rw, cam, actor, entry = scene()
    lod.ensure_installed(app)
    try:
        meta = actor._raster_lod_meta
        window = lod._pixel_window_for_world(meta["native_bounds"], meta["native_size"],
                                              (40, 60, 40, 60), 0)
        lod._apply_texture(actor, meta, window, {}, np.zeros((200, 200, 3), np.uint8))
        assert raster_properties.apply_raster_style(app, entry, {"gamma": 2})
        # Crop geometry/metadata survive a style change - only the cached
        # style key is invalidated so the next read re-renders this window.
        assert actor.GetBounds()[:4] == (40, 60, 40, 60)
        assert meta.get("_last_window") is not None
        assert meta.get("_last_style") is None
        cam.SetFocalPoint(80, 50, 0)
        cam.SetPosition(80, 50, 100)
        rw.InvokeEvent("StartEvent")
        # Panning away from a stale-styled crop falls back to the
        # full-coverage overview automatically on the next frame.
        assert actor.GetBounds()[:4] == (0, 100, 0, 100)
        assert meta.get("_last_window") is None
    finally:
        app._raster_lod_loader.close()


def test_camera_replacement_rebinds_watcher():
    app, rw, old, _, _ = scene()
    lod.ensure_installed(app)
    try:
        replacement = vtk.vtkCamera()
        replacement.DeepCopy(old)
        app.vtk_widget.renderer.SetActiveCamera(replacement)
        rw.InvokeEvent("StartEvent")
        assert app._raster_lod_camera is replacement

        app._raster_lod_timer.stop()
        generation = app._raster_lod_loader.generation
        replacement.SetParallelScale(2)
        assert app._raster_lod_loader.generation == generation + 1
        assert app._raster_lod_timer.isActive()
    finally:
        app._raster_lod_loader.close()


def test_resize_schedules_refinement():
    app, rw, _, _, _ = scene()
    lod.ensure_installed(app)
    try:
        app._raster_lod_timer.stop()
        before = lod._visible_world_bounds(app)
        generation = app._raster_lod_loader.generation
        rw.SetSize(1600, 1600)
        rw.InvokeEvent("StartEvent")
        assert lod._visible_world_bounds(app) != before
        assert app._raster_lod_loader.generation == generation + 1
        assert app._raster_lod_timer.isActive()
    finally:
        app._raster_lod_loader.close()


def test_rolled_top_view_disables_native_detail():
    # By design: a rolled/tilted camera has no single rectangular "visible
    # extent" to key a re-read window off of, so LOD refinement is skipped
    # (not a defect - see SNT_ORTHO_LIDAR_AUDIT.md finding #6).
    app, _, cam, _, _ = scene()
    assert lod._visible_world_bounds(app) is not None
    cam.Roll(1)
    assert lod._visible_world_bounds(app) is None


def test_shutdown_flag_prevents_finished_gpu_upload(monkeypatch):
    app, _, _, actor, _ = scene()
    app._raster_lod_timer = QTimer()
    loader = lod._Loader(app)
    app._raster_lod_timer.start(10000)
    calls = []
    monkeypatch.setattr(lod, "_apply_texture", lambda *args: calls.append(True) or True)
    try:
        view = lod._visible_world_bounds(app)
        request = lod._request_for(app, actor, view)
        loader.request = (request, view, loader.generation, ())
        loader.future = Future()
        loader.future.set_result(np.zeros((200, 200, 3), np.uint8))
        app._shutdown_in_progress = True
        loader.finish()
        assert calls == []
        assert loader.closed is True
    finally:
        loader.close()


def test_layer_order_offset_overrides_import_plane_heights():
    from gui.gis import gis_layers
    app, _, _, first, first_entry = scene()
    # Importer used to bake a per-file elevation into the plane; simulate a
    # leftover pre-fix plane to prove the ordering pass now normalizes it.
    plane = first.GetMapper().GetInputConnection(0, 0).GetProducer()
    plane.SetOrigin(0, 0, -10)
    plane.SetPoint1(100, 0, -10)
    plane.SetPoint2(0, 100, -10)
    _, _, _, second, second_entry = scene()
    plane = second.GetMapper().GetInputConnection(0, 0).GetProducer()
    plane.SetOrigin(0, 0, -1)
    plane.SetPoint1(100, 0, -1)
    plane.SetPoint2(0, 100, -1)
    first_entry.update(id=1, visible=True)
    second_entry.update(id=2, visible=True)
    # first is listed first (top of panel) - it must draw on top regardless
    # of the stale per-file elevation baked into its plane geometry.
    app.gis_layers = [first_entry, second_entry]
    app.geotiff_actors = [first, second]
    gis_layers._apply_order(app)
    assert first.GetBounds()[4] > second.GetBounds()[5]
