"""Fixed-camera legacy VTK reference and measured Vulkan Surface evidence."""
import hashlib
import json
from pathlib import Path

import numpy as np


def camera_state(window, mode):
    camera = window.render_backend.main_camera
    vtk_camera = window.vtk_widget.renderer.GetActiveCamera()
    return dict(center_x=camera.center_x, center_y=camera.center_y,
                center_z=camera.center_z, parallel_scale=camera.parallel_scale,
                world_width=camera.world_width, world_height=camera.world_height,
                viewport_width=camera.viewport_width, viewport_height=camera.viewport_height,
                camera_generation=camera.generation, clipping_range=list(vtk_camera.GetClippingRange()),
                active_mode=mode, position=list(vtk_camera.GetPosition()),
                focal_point=list(vtk_camera.GetFocalPoint()), view_up=list(vtk_camera.GetViewUp()),
                parallel_projection=bool(vtk_camera.GetParallelProjection()))


def save_camera(path, window, mode):
    Path(path).with_suffix('.camera.json').write_text(json.dumps(camera_state(window, mode), indent=2))


def capture_surface_reference(window, manager, candidate, output):
    """Diagnostic-only global Delaunay of the bounded visible vertex set.

    Production never calls this helper. The legacy geometry and actor helpers
    are used unchanged, rather than reconstructing their material from memory.
    """
    import vtk
    from vtk.util.numpy_support import vtk_to_numpy
    from PIL import Image
    from gui.surface_mode import (_compute_surface_geometry_backend,
                                  _build_surface_polydata_native, _configure_surface_base_actor)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    result = manager.surface_result
    service = manager.surface_service
    positions = result.positions
    azimuth, angle, ambient, _, _, ramp = result.request.style
    maximum_edge = result.request.settings[0]
    if maximum_edge <= 0:
        nodes = manager.idx.nodes
        extent = max(nodes['bounds_max'][:, :2].max(axis=0)-nodes['bounds_min'][:, :2].min(axis=0))
        maximum_edge = max(float(extent)*.10, float(np.median(nodes['lod_spacing'][:, 0]))*100)
    legacy = _compute_surface_geometry_backend(positions, np.arange(len(positions)), 0,
              len(positions), maximum_edge, azimuth, angle, ambient, json.loads(ramp),
              quality_mode='slow', z_bounds=service.elevation_bounds)
    if legacy.get('empty'):
        raise RuntimeError('legacy VTK reference contains no triangles')
    poly, buffers = _build_surface_polydata_native(legacy['points'], legacy['faces'], legacy['colors'])
    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputData(poly)
    mapper.SetScalarModeToUseCellData()
    mapper.SetColorModeToDirectScalars()
    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    _configure_surface_base_actor(actor)
    renderer = vtk.vtkRenderer()
    renderer.SetBackground(0, 0, 0)
    renderer.AddActor(actor)
    camera = vtk.vtkCamera()
    camera.DeepCopy(window.vtk_widget.renderer.GetActiveCamera())
    renderer.SetActiveCamera(camera)
    # The mirror camera in the streaming Vulkan viewport carries a token
    # clipping range (the recorded state is [0.001, 100.0] while the data sits
    # ~5000 units from the eye), so DeepCopy alone clipped the entire reference
    # away and produced a ~3 KB all-black PNG. Re-derive near/far from the
    # actor bounds, exactly as the legacy surface viewport does.
    renderer.ResetCameraClippingRange()
    render_window = vtk.vtkRenderWindow()
    render_window.SetOffScreenRendering(1)
    render_window.SetMultiSamples(0)
    render_window.SetSize(candidate.shape[1], candidate.shape[0])
    render_window.AddRenderer(renderer)
    render_window.Render()
    grab = vtk.vtkWindowToImageFilter()
    grab.SetInput(render_window)
    grab.SetInputBufferTypeToRGB()
    grab.ReadFrontBufferOff()
    grab.Update()
    reference = vtk_to_numpy(grab.GetOutput().GetPointData().GetScalars()).reshape(candidate.shape[0], candidate.shape[1], 3)[::-1].copy()
    render_window.Finalize()
    reference_path = output / 'vtk_surface_reference.png'
    if reference_path.exists():
        # Never silently overwrite the immutable accepted diagnostic reference.
        stored = np.array(Image.open(reference_path).convert('RGB'))
        if stored.shape != reference.shape or not np.array_equal(stored, reference):
            raise ValueError('fixed VTK reference differs; choose a new evidence directory')
    else:
        Image.fromarray(reference).save(reference_path)
        save_camera(reference_path, window, 'surface')
    candidate_path = output / 'vulkan_surface_candidate.png'
    Image.fromarray(candidate[:, :, :3]).save(candidate_path)
    save_camera(candidate_path, window, 'surface')
    difference = np.abs(reference.astype(np.int16)-candidate[:, :, :3].astype(np.int16))
    Image.fromarray(np.minimum(difference*4, 255).astype(np.uint8)).save(output / 'surface_difference.png')
    prop = actor.GetProperty()
    def topology(points, faces):
        # Coordinates, rather than local index order, identify the same faces.
        tri = np.sort(np.asarray(points)[faces], axis=1)
        # Lexicographically sorted rows permit a stable order-independent hash.
        rows = np.sort(tri.reshape(len(faces), 9).view(np.dtype((np.void, 72))).reshape(-1))
        return hashlib.sha256(rows.tobytes()).hexdigest()
    metadata = dict(camera=camera_state(window, 'surface'),
        reference_scope='bounded visible representatives at the production common LOD',
        legacy_triangulator=legacy['triangulator'], representative_selection=legacy['representative_meta'],
        max_edge=maximum_edge, elevation_percentiles=service.elevation_bounds,
        elevation_color_function='_compute_surface_face_colors', normal_behavior='upward cell cross products; CPU-baked Lambert; no vtkPolyDataNormals filter',
        azimuth=azimuth, light_elevation=angle, shade_ambient=ambient,
        light_space='world directional; baked into cell RGB',
        lighting=bool(prop.GetLighting()), ambient=prop.GetAmbient(), diffuse=prop.GetDiffuse(),
        specular=prop.GetSpecular(), specular_power=prop.GetSpecularPower(), interpolation=prop.GetInterpolationAsString(),
        opacity=prop.GetOpacity(), edge_visibility=bool(prop.GetEdgeVisibility()), backface_property=actor.GetBackfaceProperty() is not None,
        vtk_triangles=len(legacy['faces']), vulkan_triangles=len(result.indices),
        geometry_equal=topology(legacy['points'], legacy['faces']) == topology(result.positions, result.indices),
        mean_absolute_rgb_error=float(difference.mean()),
        percent_pixels_within_2=float(np.mean(np.max(difference, axis=2) <= 2)*100),
        reference_sha256=hashlib.sha256(reference_path.read_bytes()).hexdigest())
    (output / 'vtk_surface_reference.json').write_text(json.dumps(metadata, indent=2))
    return metadata
