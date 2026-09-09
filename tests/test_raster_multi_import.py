from types import SimpleNamespace
import numpy as np
import rasterio
from rasterio.transform import from_origin
import vtk
from PySide6.QtWidgets import QApplication, QWidget
from gui.gis import gis_layers, raster_lod as lod
from gui import vector_export

_qt = QApplication.instance() or QApplication([])


def test_three_real_tiffs_import_and_refresh(tmp_path, monkeypatch):
    app = QWidget()
    rw = vtk.vtkRenderWindow()
    rw.SetSize(300, 200)
    rw.SetOffScreenRendering(1)
    ren = vtk.vtkRenderer()
    rw.AddRenderer(ren)
    cam = ren.GetActiveCamera()
    cam.ParallelProjectionOn()
    cam.SetPosition(50, 50, 100)
    cam.SetFocalPoint(50, 50, 0)
    cam.SetViewUp(0, 1, 0)
    app.vtk_widget = SimpleNamespace(renderer=ren, GetRenderWindow=lambda: rw, render=lambda: None)
    monkeypatch.setattr(vector_export, "_infer_import_scene_z", lambda app: 0)
    monkeypatch.setattr(vector_export, "_geotiff_texture_pixel_budget", lambda *a, **kw: 4000000)
    monkeypatch.setattr(
        gis_layers, "get_layer_epsg", lambda path, layer_name=None: None
    )
    try:
        for i in range(3):
            path = tmp_path / f"tile{i}.tif"
            data = np.full((3, 100, 100), 60 + 30*i, dtype=np.uint8)
            with rasterio.open(path, "w", driver="GTiff", width=100, height=100, count=3,
                               dtype="uint8", crs="EPSG:32643", transform=from_origin(i*100, 100, 1, 1)) as dst:
                dst.write(data)
            assert gis_layers._import_one(app, str(path))
            if i == 0:
                # Reproduce: zoom/crop the first file, then add a second file.
                first = app.geotiff_actors[0]
                meta = first._raster_lod_meta
                crop = lod._pixel_window_for_world(meta["native_bounds"], meta["native_size"],
                                                   (40, 60, 40, 60), 0)
                lod._apply_texture(first, meta, crop, {}, np.full((20, 20, 3), 60, dtype=np.uint8))
                assert first.GetBounds()[0:4] == (40, 60, 40, 60)
                assert gis_layers._layer_bounds(app.gis_layers[0])[0:4] == (0, 100, 0, 100)
                app.fit_view = lambda: cam.SetFocalPoint(-10000, -10000, 0)
        assert len(app.geotiff_actors) == 3
        assert len(app.gis_layers) == 3
        loader = app._raster_lod_loader
        app._raster_lod_timer.stop()
        for _ in range(10):
            loader.start()
            if loader.future is None:
                break
            loader.future.result(timeout=5)
            loader.finish()
        assert all(a._raster_lod_meta.get("_last_window") for a in app.geotiff_actors)
        from vtk.util.numpy_support import vtk_to_numpy
        for i, actor in enumerate(app.geotiff_actors):
            colors = vtk_to_numpy(actor.GetTexture().GetInput().GetPointData().GetScalars())
            assert np.all(colors == 60 + 30*i)
        # Actual offscreen VTK draw: all three distinct tile colors must appear.
        rw.Render()
        capture = vtk.vtkWindowToImageFilter()
        capture.SetInput(rw)
        capture.ReadFrontBufferOff()
        capture.Update()
        pixels = vtk_to_numpy(capture.GetOutput().GetPointData().GetScalars()).astype(int)
        for value in (60, 90, 120):
            assert np.count_nonzero(np.all(abs(pixels[:, :3] - value) <= 2, axis=1)) > 100
    finally:
        if hasattr(app, "_raster_lod_loader"):
            app._raster_lod_loader.close()
            app._raster_lod_timer.stop()
        app.close()
