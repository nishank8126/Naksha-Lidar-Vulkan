from types import SimpleNamespace

import vtk

from gui.scene_render_pipeline import (
    DATA_LAYER,
    NUMBER_OF_LAYERS,
    OVERLAY_LAYER,
    RASTER_LAYER,
    ROLE_DATA,
    ROLE_RASTER,
    TEXT_LAYER,
    ensure_scene_render_pipeline,
    route_actor,
)


def _app_with_renderers():
    window = vtk.vtkRenderWindow()
    data = vtk.vtkRenderer()
    overlay = vtk.vtkRenderer()
    text = vtk.vtkRenderer()
    window.AddRenderer(data)
    window.AddRenderer(overlay)
    window.AddRenderer(text)
    widget = SimpleNamespace(renderer=data, GetRenderWindow=lambda: window)
    digitizer = SimpleNamespace(
        overlay_renderer=overlay,
        text_overlay_renderer=text,
    )
    app = SimpleNamespace(
        vtk_widget=widget,
        digitizer=digitizer,
        geotiff_actors=[],
        gis_layers=[],
    )
    return app, window, data, overlay, text


def test_pipeline_has_one_renderer_per_scene_role():
    app, window, data, overlay, text = _app_with_renderers()

    pipeline = ensure_scene_render_pipeline(app)

    assert window.GetNumberOfLayers() == NUMBER_OF_LAYERS
    assert pipeline["raster"].GetLayer() == RASTER_LAYER
    assert data.GetLayer() == DATA_LAYER
    assert overlay.GetLayer() == OVERLAY_LAYER
    assert text.GetLayer() == TEXT_LAYER
    assert data.GetPreserveColorBuffer()
    assert not data.GetPreserveDepthBuffer()
    raster_camera = pipeline["raster"].GetActiveCamera()
    assert raster_camera is not data.GetActiveCamera()
    data.GetActiveCamera().SetPosition(7, 8, 9)
    assert raster_camera.GetPosition() == data.GetActiveCamera().GetPosition()


def test_data_clear_cannot_remove_pipeline_owned_raster():
    app, _window, data, overlay, _text = _app_with_renderers()
    raster = vtk.vtkActor()
    data.AddActor(raster)  # simulate a legacy import into the wrong renderer
    overlay.AddActor(raster)
    app.geotiff_actors = [raster]

    raster_renderer = route_actor(app, raster, ROLE_RASTER)

    assert raster_renderer.HasViewProp(raster)
    assert not data.HasViewProp(raster)
    assert not overlay.HasViewProp(raster)

    data.RemoveAllViewProps()
    assert raster_renderer.HasViewProp(raster)


def test_repeated_mode_rebuilds_preserve_roles_and_repair_layers():
    app, window, data, overlay, text = _app_with_renderers()
    raster = vtk.vtkActor()
    app.geotiff_actors = [raster]
    raster_renderer = route_actor(app, raster, ROLE_RASTER)

    for _mode in ("points", "shading", "surface", "classification", "points"):
        data.RemoveAllViewProps()
        data.AddActor(vtk.vtkActor())

        # Simulate a feature accidentally disturbing renderer configuration.
        data.SetLayer(0)
        overlay.SetLayer(0)
        text.SetLayer(0)
        window.SetNumberOfLayers(1)

        pipeline = ensure_scene_render_pipeline(app)
        assert window.GetNumberOfLayers() == NUMBER_OF_LAYERS
        assert data.GetLayer() == DATA_LAYER
        assert overlay.GetLayer() == OVERLAY_LAYER
        assert text.GetLayer() == TEXT_LAYER
        assert raster_renderer.HasViewProp(raster)
        assert pipeline[ROLE_RASTER] is raster_renderer


def _quad_actor(xmin, xmax, ymin, ymax, z, rgb):
    points = vtk.vtkPoints()
    for point in ((xmin, ymin, z), (xmax, ymin, z),
                  (xmax, ymax, z), (xmin, ymax, z)):
        points.InsertNextPoint(*point)
    polygon = vtk.vtkPolygon()
    polygon.GetPointIds().SetNumberOfIds(4)
    for index in range(4):
        polygon.GetPointIds().SetId(index, index)
    cells = vtk.vtkCellArray()
    cells.InsertNextCell(polygon)
    polydata = vtk.vtkPolyData()
    polydata.SetPoints(points)
    polydata.SetPolys(cells)
    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputData(polydata)
    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    actor.GetProperty().SetColor(*rgb)
    actor.GetProperty().LightingOff()
    return actor


def test_lidar_composites_over_physically_closer_orthophoto():
    app, window, data, _overlay, _text = _app_with_renderers()
    window.SetOffScreenRendering(1)
    window.SetMultiSamples(0)
    window.SetSize(64, 64)

    # The red raster is closer to the camera than the green LiDAR quad. In a
    # shared depth buffer it wins incorrectly; separate passes must show green.
    raster = _quad_actor(-1, 1, -1, 1, 0.5, (1.0, 0.0, 0.0))
    lidar = _quad_actor(-0.5, 0.5, -0.5, 0.5, 0.0, (0.0, 1.0, 0.0))
    route_actor(app, raster, ROLE_RASTER)
    route_actor(app, lidar, ROLE_DATA)

    camera = data.GetActiveCamera()
    camera.ParallelProjectionOn()
    camera.SetParallelScale(1.25)
    camera.SetPosition(0, 0, 10)
    camera.SetFocalPoint(0, 0, 0)
    camera.SetClippingRange(0.1, 20)
    window.Render()

    image_filter = vtk.vtkWindowToImageFilter()
    image_filter.SetInput(window)
    image_filter.SetInputBufferTypeToRGB()
    image_filter.ReadFrontBufferOff()
    image_filter.Update()
    image = image_filter.GetOutput()

    assert image.GetScalarComponentAsDouble(32, 32, 0, 1) > 200  # green LiDAR
    assert image.GetScalarComponentAsDouble(12, 12, 0, 0) > 200  # red raster


def test_lidar_clipping_reset_cannot_clip_lower_raster_pass():
    app, window, data, _overlay, _text = _app_with_renderers()
    window.SetOffScreenRendering(1)
    window.SetMultiSamples(0)
    window.SetSize(64, 64)

    raster = _quad_actor(-1, 1, -1, 1, 0.0, (1.0, 0.0, 0.0))
    lidar = _quad_actor(-0.5, 0.5, -0.5, 0.5, 250.0, (0.0, 1.0, 0.0))
    route_actor(app, raster, ROLE_RASTER)
    route_actor(app, lidar, ROLE_DATA)

    camera = data.GetActiveCamera()
    camera.ParallelProjectionOn()
    camera.SetParallelScale(1.25)
    camera.SetPosition(0, 0, 1000)
    camera.SetFocalPoint(0, 0, 280)
    data.ResetCameraClippingRange()  # intentionally considers only LiDAR
    window.Render()

    image_filter = vtk.vtkWindowToImageFilter()
    image_filter.SetInput(window)
    image_filter.SetInputBufferTypeToRGB()
    image_filter.ReadFrontBufferOff()
    image_filter.Update()
    image = image_filter.GetOutput()

    assert image.GetScalarComponentAsDouble(32, 32, 0, 1) > 200  # LiDAR center
    assert image.GetScalarComponentAsDouble(12, 12, 0, 0) > 200  # raster outside it
