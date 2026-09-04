"""Authoritative renderer ownership for the main map canvas.

The main view is a compositor, not one shared 3D bucket:

    layer 0  raster/background (GeoTIFF and basemap)
    layer 1  LiDAR data (points, Shading, Surface)
    layer 2  vector/drawing overlays
    layer 3  text/marker overlays

Separating rasters from LiDAR removes camera-Z and depth-buffer ordering from
the equation. The raster pass follows the main camera's view transform but has
its own clipping range, so a LiDAR-only clipping reset cannot cut it away. A
point-cloud clear can clear only the data renderer without raster backups.
"""

RASTER_LAYER = 0
DATA_LAYER = 1
OVERLAY_LAYER = 2
TEXT_LAYER = 3
NUMBER_OF_LAYERS = 4

ROLE_RASTER = "raster"
ROLE_DATA = "data"
ROLE_OVERLAY = "overlay"
ROLE_TEXT = "text"


def _has_renderer(render_window, renderer) -> bool:
    if render_window is None or renderer is None:
        return False
    try:
        return bool(render_window.HasRenderer(renderer))
    except Exception:
        try:
            return bool(render_window.GetRenderers().IsItemPresent(renderer))
        except Exception:
            return False


def _set_preserve(renderer, *, color: bool, depth: bool) -> None:
    try:
        renderer.SetPreserveColorBuffer(bool(color))
    except Exception:
        pass
    try:
        renderer.SetPreserveDepthBuffer(bool(depth))
    except Exception:
        pass


def _remove_prop(renderer, actor) -> None:
    if renderer is None or actor is None:
        return
    try:
        if not renderer.HasViewProp(actor):
            return
    except Exception:
        pass
    try:
        renderer.RemoveViewProp(actor)
    except Exception:
        try:
            renderer.RemoveActor(actor)
        except Exception:
            pass


def _add_prop(renderer, actor) -> None:
    if renderer is None or actor is None:
        return
    try:
        if renderer.HasViewProp(actor):
            return
    except Exception:
        pass
    try:
        renderer.AddViewProp(actor)
    except Exception:
        renderer.AddActor(actor)


def _ensure_raster_camera_binding(app, data_renderer, raster_renderer):
    """Mirror the main view camera while keeping raster clipping independent."""
    source_camera = data_renderer.GetActiveCamera()
    binding = getattr(app, "_raster_camera_binding", None)
    if (
        not isinstance(binding, dict)
        or binding.get("source") is not source_camera
        or binding.get("renderer") is not raster_renderer
    ):
        if isinstance(binding, dict):
            try:
                binding["source"].RemoveObserver(binding["observer_tag"])
            except Exception:
                pass

        import vtk
        raster_camera = vtk.vtkCamera()
        binding = {
            "source": source_camera,
            "target": raster_camera,
            "renderer": raster_renderer,
            "syncing": False,
        }

        def sync_camera(_caller=None, _event=None):
            if binding["syncing"]:
                return
            binding["syncing"] = True
            try:
                raster_camera.DeepCopy(source_camera)
                raster_renderer.SetActiveCamera(raster_camera)
                # This changes only the raster camera. Pan/zoom/projection remain
                # identical to the data camera, but each pass clips to its data.
                raster_renderer.ResetCameraClippingRange()
            finally:
                binding["syncing"] = False

        binding["sync"] = sync_camera
        binding["observer_tag"] = source_camera.AddObserver("ModifiedEvent", sync_camera)
        app._raster_camera_binding = binding

    binding["renderer"] = raster_renderer
    binding["sync"]()
    return binding["target"]


def _known_raster_actors(app):
    seen = set()

    def emit(actor):
        if actor is None or id(actor) in seen:
            return
        seen.add(id(actor))
        yield actor

    for actor in list(getattr(app, "geotiff_actors", []) or []):
        yield from emit(actor)

    for entry in list(getattr(app, "gis_layers", []) or []):
        if not isinstance(entry, dict) or entry.get("kind") != ROLE_RASTER:
            continue
        for actor in list(entry.get("actors", []) or []):
            yield from emit(actor)
        for sub_info in (entry.get("sub_layers", {}) or {}).values():
            if not isinstance(sub_info, dict):
                continue
            for actor in list(sub_info.get("actors", []) or []):
                yield from emit(actor)
            yield from emit(sub_info.get("actor"))


def _route_prop(pipeline, actor, role: str) -> None:
    target = pipeline.get(role)
    if target is None or actor is None:
        return
    for renderer in pipeline.values():
        if renderer is not target:
            _remove_prop(renderer, actor)
    _add_prop(target, actor)
    try:
        actor._naksha_scene_role = role
    except Exception:
        pass


def ensure_scene_render_pipeline(app, overlay_renderer=None, text_renderer=None):
    """Create/repair the four main-view renderers and enforce raster ownership."""
    widget = getattr(app, "vtk_widget", None)
    data_renderer = getattr(widget, "renderer", None) if widget is not None else None
    if widget is None or data_renderer is None:
        return {}
    try:
        render_window = widget.GetRenderWindow()
    except Exception:
        render_window = None
    if render_window is None:
        return {}

    raster_renderer = getattr(app, "_raster_renderer", None)
    if raster_renderer is None:
        import vtk
        raster_renderer = vtk.vtkRenderer()
        app._raster_renderer = raster_renderer
    if not _has_renderer(render_window, raster_renderer):
        render_window.AddRenderer(raster_renderer)

    digitizer = getattr(app, "digitizer", None)
    if overlay_renderer is None and digitizer is not None:
        overlay_renderer = getattr(digitizer, "overlay_renderer", None)
    if text_renderer is None and digitizer is not None:
        text_renderer = getattr(digitizer, "text_overlay_renderer", None)
    for renderer in (overlay_renderer, text_renderer):
        if renderer is not None and not _has_renderer(render_window, renderer):
            render_window.AddRenderer(renderer)

    render_window.SetNumberOfLayers(NUMBER_OF_LAYERS)
    camera = data_renderer.GetActiveCamera()

    raster_renderer.SetLayer(RASTER_LAYER)
    raster_renderer.SetInteractive(0)
    raster_renderer.SetErase(1)
    _set_preserve(raster_renderer, color=False, depth=False)
    _ensure_raster_camera_binding(app, data_renderer, raster_renderer)
    try:
        raster_renderer.SetBackground(data_renderer.GetBackground())
        raster_renderer.SetBackground2(data_renderer.GetBackground2())
        raster_renderer.SetGradientBackground(data_renderer.GetGradientBackground())
    except Exception:
        pass

    data_renderer.SetLayer(DATA_LAYER)
    data_renderer.SetErase(1)
    _set_preserve(data_renderer, color=True, depth=False)
    try:
        data_renderer.SetBackgroundAlpha(0.0)
    except Exception:
        pass

    if overlay_renderer is not None:
        overlay_renderer.SetLayer(OVERLAY_LAYER)
        overlay_renderer.SetInteractive(0)
        overlay_renderer.SetErase(1)
        _set_preserve(overlay_renderer, color=True, depth=False)
        overlay_renderer.SetActiveCamera(camera)

    if text_renderer is not None:
        text_renderer.SetLayer(TEXT_LAYER)
        text_renderer.SetInteractive(0)
        text_renderer.SetErase(1)
        _set_preserve(text_renderer, color=True, depth=False)
        text_renderer.SetActiveCamera(camera)

    pipeline = {
        ROLE_RASTER: raster_renderer,
        ROLE_DATA: data_renderer,
        ROLE_OVERLAY: overlay_renderer,
        ROLE_TEXT: text_renderer,
    }
    app._scene_render_pipeline = pipeline

    signature = tuple(id(pipeline[role]) if pipeline[role] is not None else None
                      for role in (ROLE_RASTER, ROLE_DATA, ROLE_OVERLAY, ROLE_TEXT))
    if getattr(app, "_scene_render_pipeline_signature", None) != signature:
        app._scene_render_pipeline_signature = signature
        print(
            "SCENE_RENDER_PIPELINE configured: "
            "raster=0, lidar=1, vector=2, text=3; synchronized view, isolated depth/clipping"
        )

    # Repair actors loaded before the pipeline existed or left in an old renderer.
    for actor in _known_raster_actors(app):
        _route_prop(pipeline, actor, ROLE_RASTER)
        try:
            actor.GetProperty().SetDepthTestingEnabled(True)
        except Exception:
            pass
    _ensure_raster_camera_binding(app, data_renderer, raster_renderer)
    return pipeline


def renderer_for_role(app, role: str):
    return ensure_scene_render_pipeline(app).get(role)


def route_actor(app, actor, role: str):
    """Move one actor to its sole authoritative renderer."""
    if role not in {ROLE_RASTER, ROLE_DATA, ROLE_OVERLAY, ROLE_TEXT}:
        raise ValueError(f"Unknown scene role: {role}")
    pipeline = ensure_scene_render_pipeline(app)
    _route_prop(pipeline, actor, role)
    if role == ROLE_RASTER:
        _ensure_raster_camera_binding(
            app, pipeline.get(ROLE_DATA), pipeline.get(ROLE_RASTER)
        )
    return pipeline.get(role)


def add_raster_actor(app, actor):
    renderer = route_actor(app, actor, ROLE_RASTER)
    try:
        actor.GetProperty().SetDepthTestingEnabled(True)
    except Exception:
        pass
    return renderer


def remove_actor_from_pipeline(app, actor) -> None:
    for renderer in ensure_scene_render_pipeline(app).values():
        _remove_prop(renderer, actor)


def sync_scene_background(app) -> None:
    """Copy the data renderer's canvas color to the layer-0 renderer."""
    pipeline = ensure_scene_render_pipeline(app)
    data_renderer = pipeline.get(ROLE_DATA)
    raster_renderer = pipeline.get(ROLE_RASTER)
    if data_renderer is None or raster_renderer is None:
        return
    try:
        raster_renderer.SetBackground(data_renderer.GetBackground())
        raster_renderer.SetBackground2(data_renderer.GetBackground2())
        raster_renderer.SetGradientBackground(data_renderer.GetGradientBackground())
    except Exception:
        pass
