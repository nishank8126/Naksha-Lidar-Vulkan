import numpy as np
import pyvista as pv
from scipy.spatial import Delaunay
from .views import set_view
from PySide6.QtCore import QObject, QThread, Signal, QTimer
import vtk


# ─────────────────────────────────────────────────────────────
# SNT RESTORE HELPER
# ─────────────────────────────────────────────────────────────
def _restore_snt_after_clear(app):
    """
    Re-add SNT actors to the renderer after clear() and apply Z offset
    so SNT always renders on top of the current point cloud.
    """
    if hasattr(app, 'snt_dialog') and app.snt_dialog is not None:
        try:
            app.snt_dialog.restore_snt_actors()
        except Exception as e:
            print(f"  ⚠️ SNT restore via dialog: {e}") 
        return

    from gui.snt_attachment import _get_snt_z_offset, _apply_z_offset_to_actor

    try:
        renderer = app.vtk_widget.renderer
    except Exception:
        return

    z_offset = _get_snt_z_offset(app)
    restored = 0

    for store_name in ['snt_actors', 'dxf_actors']:
        for entry in getattr(app, store_name, []):
            for actor in entry.get("actors", []):
                try:
                    is_overlay = getattr(actor, '_is_dxf_actor', False)
                    if is_overlay and z_offset > 0:
                        _apply_z_offset_to_actor(actor, z_offset)
                    renderer.AddActor(actor)
                    restored += 1
                except Exception:
                    pass

    if restored > 0:
        renderer.ResetCameraClippingRange()
        print(f"  🔄 Fallback SNT restore: {restored} actors (z_offset={z_offset:.1f})")
        try:
            app.vtk_widget.GetRenderWindow().Render()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# OVERLAY CLIPPING RANGE SYNC (was missing → caused NameError)
# ─────────────────────────────────────────────────────────────
def _sync_overlay_clipping_range(app):
    """
    Expand the main camera clipping range to include SNT/DXF overlay
    actors so they are never clipped out of view.
    
    This fixes the NameError that occurred when depth mode called
    this function which didn't exist in the module.
    """
    try:
        renderer = app.vtk_widget.renderer
        if renderer is None:
            return

        camera = renderer.GetActiveCamera()
        if camera is None:
            return

        # Get current clipping range from point cloud
        near, far = camera.GetClippingRange()

        # Check all SNT/DXF actors for extended bounds
        expanded = False
        for store_name in ['snt_actors', 'dxf_actors']:
            for entry in getattr(app, store_name, []):
                for actor in entry.get("actors", []):
                    try:
                        if actor.GetVisibility():
                            bounds = actor.GetBounds()
                            if bounds and bounds[0] != 1.0 and bounds[1] != -1.0:
                                # Actor has valid bounds - renderer should include it
                                expanded = True
                    except Exception:
                        pass

        if expanded:
            # Let VTK recalculate to include all actors
            renderer.ResetCameraClippingRange()
            new_near, new_far = camera.GetClippingRange()
            
            # Ensure we don't clip too aggressively - add margin
            margin = (new_far - new_near) * 0.1
            camera.SetClippingRange(
                max(new_near - margin, 0.01),
                new_far + margin
            )
    except Exception as e:
        print(f"  ⚠️ _sync_overlay_clipping_range: {e}")


# ─────────────────────────────────────────────────────────────
# PYVISTA ORPHAN ACTOR CLEANUP
# ─────────────────────────────────────────────────────────────
def _remove_pyvista_point_actors(app):
    """
    Remove any pyvista-added point cloud actors from the renderer.
    
    When depth/intensity/elevation/rgb modes use app.vtk_widget.add_points(),
    they create actors that are NOT managed by the unified_actor_manager.
    These 'orphan' actors must be explicitly removed before switching to
    class or shading modes, otherwise they bleed through.
    
    We identify them by checking for the '_naksha_pyvista_points' flag
    we set when adding them, or by checking they are NOT the unified actor
    and NOT SNT/DXF actors.
    """
    try:
        renderer = app.vtk_widget.renderer
        if renderer is None:
            return

        actors_to_remove = []
        actor_collection = renderer.GetActors()
        actor_collection.InitTraversal()

        # Get the unified actor name so we don't remove it
        unified_actor = getattr(app, '_unified_actor', None)

        for i in range(actor_collection.GetNumberOfItems()):
            actor = actor_collection.GetNextActor()
            if actor is None:
                continue

            # Skip unified actor managed by unified_actor_manager
            if unified_actor is not None and actor is unified_actor:
                continue

            # Skip SNT/DXF overlay actors
            if getattr(actor, '_is_dxf_actor', False):
                continue
            if getattr(actor, '_is_snt_actor', False):
                continue

            # Skip shading mesh actors
            if getattr(actor, '_is_shading_mesh', False):
                continue

            # Skip any actor with a custom preservation flag
            if getattr(actor, '_naksha_preserve', False):
                continue

            # Check if this is a pyvista-added point cloud actor
            if getattr(actor, '_naksha_pyvista_points', False):
                actors_to_remove.append(actor)
                continue

            # Also check by mapper type - pyvista adds actors with PolyDataMapper
            # that have RGB scalars but are not the unified actor
            mapper = actor.GetMapper()
            if mapper is not None:
                input_data = mapper.GetInput()
                if input_data is not None:
                    # Check if it's a point cloud (has points but no cells or only vertex cells)
                    n_points = input_data.GetNumberOfPoints()
                    n_cells = input_data.GetNumberOfCells()
                    if n_points > 1000 and (n_cells == 0 or n_cells == n_points):
                        # Large point cloud not managed by unified actor - likely orphan
                        if hasattr(input_data, 'GetPointData'):
                            pd = input_data.GetPointData()
                            if pd and pd.GetArray("RGB"):
                                actors_to_remove.append(actor)
                                continue

        for actor in actors_to_remove:
            renderer.RemoveActor(actor)

        if actors_to_remove:
            print(f"  🧹 Removed {len(actors_to_remove)} orphan pyvista point actors")

    except Exception as e:
        print(f"  ⚠️ _remove_pyvista_point_actors: {e}")


def _tag_pyvista_actor(app, tag="_naksha_pyvista_points"):
    """
    Tag the most recently added actor with a flag so we can find and
    remove it later. Call this right after app.vtk_widget.add_points().
    """
    try:
        renderer = app.vtk_widget.renderer
        if renderer is None:
            return

        # The last added actor is typically the last in the collection
        actor_collection = renderer.GetActors()
        actor_collection.InitTraversal()
        last_actor = None
        for i in range(actor_collection.GetNumberOfItems()):
            a = actor_collection.GetNextActor()
            if a is not None:
                last_actor = a

        if last_actor is not None:
            setattr(last_actor, tag, True)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# WORKER
# ─────────────────────────────────────────────────────────────
class ColorUpdateWorker(QObject):
    finished = Signal(object)

    def __init__(self, app, mask=None):
        super().__init__()
        self.app = app
        self.mask = mask

    def run(self):
        try:
            colors = compute_colors(self.app, mask=self.mask)
            self.finished.emit(colors)
        except Exception as e:
            print(f"⚠️ Worker color computation failed: {e}")
            self.finished.emit(None)


import numpy as np


def _microstation_auto_normalize(values, low_pct=1.0, high_pct=99.0, ignore_zero=False, return_clip=False):
    """
    Robust auto-normalization similar to MicroStation display stretching.
    """
    arr = np.asarray(values, dtype=np.float64)

    if arr.size == 0:
        norm = np.zeros_like(arr, dtype=np.float64)
        if return_clip:
            return norm, 0.0, 1.0
        return norm

    finite_mask = np.isfinite(arr)
    work = arr[finite_mask]

    if work.size == 0:
        norm = np.zeros_like(arr, dtype=np.float64)
        if return_clip:
            return norm, 0.0, 1.0
        return norm

    if ignore_zero:
        nonzero = work[work > 0.0]
        if nonzero.size > 0:
            work = nonzero

    lo = float(np.percentile(work, low_pct))
    hi = float(np.percentile(work, high_pct))

    if hi <= lo:
        lo = float(work.min())
        hi = float(work.max())

    if hi <= lo:
        norm = np.full(arr.shape, 0.5, dtype=np.float64)
        norm[~finite_mask] = 0.0
        if return_clip:
            return norm, lo, hi
        return norm

    norm = (arr - lo) / (hi - lo)
    norm = np.clip(norm, 0.0, 1.0)
    norm[~finite_mask] = 0.0

    if return_clip:
        return norm, lo, hi
    return norm


def _microstation_rainbow_5color(norm):
    """
    Blue -> Cyan -> Green -> Yellow -> Red
    """
    norm = np.asarray(norm, dtype=np.float64)
    colors = np.zeros((len(norm), 3), dtype=np.uint8)

    for i, val in enumerate(norm):
        if val <= 0.25:
            t = val / 0.25
            r = 0
            g = int(t * 255)
            b = 255
        elif val <= 0.50:
            t = (val - 0.25) / 0.25
            r = 0
            g = 255
            b = int((1.0 - t) * 255)
        elif val <= 0.75:
            t = (val - 0.50) / 0.25
            r = int(t * 255)
            g = 255
            b = 0
        else:
            t = (val - 0.75) / 0.25
            r = 255
            g = int((1.0 - t) * 255)
            b = 0

        colors[i] = [r, g, b]

    return colors

def _camera_forward_vector(cam):
    pos = np.asarray(cam.GetPosition(), dtype=np.float64)
    fp = np.asarray(cam.GetFocalPoint(), dtype=np.float64)
    v = fp - pos
    n = np.linalg.norm(v)
    if n < 1e-12:
        return np.array([0.0, 0.0, -1.0], dtype=np.float64)
    return v / n

def _microstation_view_depth_values(points, cam):
    """
    Depth along the camera viewing direction.
    This matches display/eye-space depth much better than Euclidean distance.
    """
    pts = np.asarray(points, dtype=np.float64)
    pos = np.asarray(cam.GetPosition(), dtype=np.float64)
    fwd = _camera_forward_vector(cam)
    return np.dot(pts - pos, fwd)

def _microstation_depth_rgb_from_camera(points, cam, low_pct=1.0, high_pct=99.0, 
                                        color_scheme="grayscale", gamma=1.0, 
                                        return_debug=False):
    """
    Compute depth-based colors with customizable color schemes.
    
    Args:
        points: Point cloud coordinates
        cam: VTK camera
        low_pct: Near depth percentile clipping
        high_pct: Far depth percentile clipping
        color_scheme: "grayscale", "inverted", "rainbow", or "heatmap"
        gamma: Contrast adjustment (0.5=dark, 1.0=normal, 2.0=bright)
        return_debug: Return debug info
    """
    depth_vals = _microstation_view_depth_values(points, cam)
    norm, lo, hi = _microstation_auto_normalize(
        depth_vals,
        low_pct=low_pct,
        high_pct=high_pct,
        ignore_zero=False,
        return_clip=True,
    )
    
    # Apply gamma correction for contrast
    if gamma != 1.0:
        norm = np.power(norm, gamma)
    
    # Apply color scheme
    if color_scheme == "inverted":
        gray = (norm * 255.0).astype(np.uint8)
        rgb = np.stack([gray, gray, gray], axis=1)
    
    elif color_scheme == "rainbow":
        rgb = _microstation_rainbow_5color(norm)
    
    elif color_scheme == "heatmap":
        # Dark blue → Purple → Red → Yellow
        rgb = _depth_heatmap_color(norm)
    
    else:  # "grayscale" (default)
        gray = ((1.0 - norm) * 255.0).astype(np.uint8)
        rgb = np.stack([gray, gray, gray], axis=1)

    if return_debug:
        return rgb, lo, hi
    return rgb


def _depth_heatmap_color(norm):
    """Heat map color scheme for depth: Dark Blue → Purple → Red → Yellow."""
    norm = np.clip(np.asarray(norm, dtype=np.float32), 0.0, 1.0)
    
    stops = np.array([0.00, 0.33, 0.67, 1.00], dtype=np.float32)
    
    r = np.interp(norm, stops, np.array([0, 128, 255, 255], dtype=np.float32))
    g = np.interp(norm, stops, np.array([0,   0,   0, 255], dtype=np.float32))
    b = np.interp(norm, stops, np.array([128, 128,  0,   0], dtype=np.float32))
    
    return np.column_stack((r, g, b)).astype(np.uint8)


def _apply_custom_color_ramp(norm, color_ramp):
    """
    color_ramp = [(pos0, (r,g,b)), (pos1, (r,g,b)), ...]
    positions are in [0, 1]
    """
    norm = np.asarray(norm, dtype=np.float64)
    colors = np.zeros((len(norm), 3), dtype=np.uint8)

    ramp = sorted(color_ramp, key=lambda x: x[0])
    if len(ramp) < 2:
        return _microstation_rainbow_5color(norm)

    for i, val in enumerate(norm):
        if val <= ramp[0][0]:
            colors[i] = ramp[0][1]
            continue
        if val >= ramp[-1][0]:
            colors[i] = ramp[-1][1]
            continue

        for j in range(len(ramp) - 1):
            p1, c1 = ramp[j]
            p2, c2 = ramp[j + 1]
            if p1 <= val <= p2:
                if p2 <= p1:
                    colors[i] = c1
                else:
                    t = (val - p1) / (p2 - p1)
                    r = int(c1[0] * (1.0 - t) + c2[0] * t)
                    g = int(c1[1] * (1.0 - t) + c2[1] * t)
                    b = int(c1[2] * (1.0 - t) + c2[2] * t)
                    colors[i] = [r, g, b]
                break

    return colors


def _microstation_intensity_rgb(
    intensity,
    low_pct=0.5,
    high_pct=99.8,
    gamma=1.65,   # darker than before
    return_debug=False,
):
    """
    MicroStation-like intensity display:
    - ignore zero / invalid intensity
    - robust histogram stretch
    - perceptual darkening of mid-tones
    """
    arr = np.asarray(intensity, dtype=np.float64)

    if arr.size == 0:
        rgb = np.zeros((0, 3), dtype=np.uint8)
        if return_debug:
            return rgb, 0.0, 1.0
        return rgb

    finite_mask = np.isfinite(arr)
    work = arr[finite_mask]

    if work.size == 0:
        gray = np.full(arr.shape, 128, dtype=np.uint8)
        rgb = np.stack([gray, gray, gray], axis=1)
        if return_debug:
            return rgb, 0.0, 1.0
        return rgb

    # Ignore zero intensity if possible
    nonzero = work[work > 0.0]
    if nonzero.size > 0:
        work = nonzero

    lo = float(np.percentile(work, low_pct))
    hi = float(np.percentile(work, high_pct))

    if hi <= lo:
        lo = float(work.min())
        hi = float(work.max())

    if hi <= lo:
        gray = np.full(arr.shape, 128, dtype=np.uint8)
        rgb = np.stack([gray, gray, gray], axis=1)
        if return_debug:
            return rgb, lo, hi
        return rgb

    norm = (arr - lo) / (hi - lo)
    norm = np.clip(norm, 0.0, 1.0)

    # Darken the mid-tones to match MicroStation look
    norm = np.power(norm, gamma)

    gray = (norm * 255.0).astype(np.uint8)
    rgb = np.stack([gray, gray, gray], axis=1)

    if return_debug:
        return rgb, lo, hi
    return rgb


def _microstation_rgb_enhancement(
    rgb_norm,
    auto_stretch=True,
    gamma=1.1,
    black_point=2.0,
    white_point=98.0,
    return_debug=False
):
    """
    MicroStation-style RGB enhancement for natural photo-realistic display.
    
    Args:
        rgb_norm: RGB values in 0.0-1.0 range (N x 3 array)
        auto_stretch: Apply per-channel histogram stretching
        gamma: Perceptual gamma correction (1.1 = slight brightening)
        black_point: Lower percentile to clip (darkest pixels become black)
        white_point: Upper percentile to clip (brightest pixels become white)
        return_debug: Return debug info
    
    Returns:
        RGB colors array (N x 3, uint8)
    """
    import numpy as np
    
    rgb = np.asarray(rgb_norm, dtype=np.float32)
    
    if rgb.size == 0:
        result = np.zeros((0, 3), dtype=np.uint8)
        if return_debug:
            return result, {}, {}
        return result
    
    # ══════════════════════════════════════════════════════════════
    # STEP 1: Per-Channel Histogram Stretching (like MicroStation)
    # ══════════════════════════════════════════════════════════════
    if auto_stretch:
        stretched = np.zeros_like(rgb)
        clip_info = {}
        
        for ch in range(3):  # R, G, B
            channel = rgb[:, ch]
            
            # Find valid (non-zero) values
            valid = channel[channel > 0.0]
            
            if valid.size == 0:
                stretched[:, ch] = channel
                continue
            
            # Calculate stretch range (clip extreme outliers)
            lo = float(np.percentile(valid, black_point))
            hi = float(np.percentile(valid, white_point))
            
            clip_info[ch] = {'lo': lo, 'hi': hi}
            
            # Avoid division by zero
            if hi <= lo:
                lo = float(valid.min())
                hi = float(valid.max())
            
            if hi <= lo:
                stretched[:, ch] = channel
                continue
            
            # Stretch to 0-1 range
            stretched_ch = (channel - lo) / (hi - lo)
            stretched_ch = np.clip(stretched_ch, 0.0, 1.0)
            stretched[:, ch] = stretched_ch
        
        rgb = stretched
    else:
        clip_info = {}
    
    # ══════════════════════════════════════════════════════════════
    # STEP 2: Gamma Correction (Perceptual Brightness)
    # ══════════════════════════════════════════════════════════════
    # MicroStation typically uses gamma 1.0-1.2 for natural look
    # Lower gamma = brighter midtones (good for dark LiDAR scans)
    if gamma != 1.0:
        rgb = np.power(rgb, 1.0 / gamma)
    
    # ══════════════════════════════════════════════════════════════
    # STEP 3: Black Point Lift (Prevent Muddy Shadows)
    # ══════════════════════════════════════════════════════════════
    # Add slight lift to very dark values (like MicroStation's "shadow detail")
    black_lift = 0.02  # 2% lift for darkest values
    rgb = rgb * (1.0 - black_lift) + black_lift
    rgb = np.clip(rgb, 0.0, 1.0)
    
    # ══════════════════════════════════════════════════════════════
    # STEP 4: Convert to uint8
    # ══════════════════════════════════════════════════════════════
    rgb_u8 = (rgb * 255.0).astype(np.uint8)
    
    if return_debug:
        stats = {
            'min': rgb.min(axis=0),
            'max': rgb.max(axis=0),
            'mean': rgb.mean(axis=0),
            'gamma': gamma
        }
        return rgb_u8, clip_info, stats
    
    return rgb_u8


def _microstation_elevation_rgb(z_values, color_ramp=None, low_pct=1.0, high_pct=99.0, return_debug=False):
    norm, lo, hi = _microstation_auto_normalize(z_values, low_pct, high_pct, return_clip=True)

    if color_ramp and len(color_ramp) >= 2:
        rgb = _apply_custom_color_ramp(norm, color_ramp)
    else:
        rgb = _microstation_rainbow_5color(norm)

    if return_debug:
        return rgb, lo, hi
    return rgb


def _microstation_depth_rgb_from_distance(distances, low_pct=1.0, high_pct=99.0, return_debug=False):
    norm, lo, hi = _microstation_auto_normalize(distances, low_pct, high_pct, return_clip=True)
    gray = ((1.0 - norm) * 255.0).astype(np.uint8)
    rgb = np.stack([gray, gray, gray], axis=1)
    if return_debug:
        return rgb, lo, hi
    return rgb


def _normalize_rgb_to_uint8(rgb):
    arr = np.asarray(rgb)
    if arr.size == 0:
        return np.zeros((0, 3), dtype=np.uint8)

    if arr.ndim != 2 or arr.shape[1] < 3:
        raise ValueError(f"RGB array must be Nx3, got shape {arr.shape}")

    arr = arr[:, :3]

    if arr.dtype == np.uint8:
        return arr

    if np.issubdtype(arr.dtype, np.floating):
        max_val = float(np.nanmax(arr))
        if max_val <= 1.0 + 1e-6:
            return np.clip(arr * 255.0, 0, 255).astype(np.uint8)
        elif max_val <= 255.0 + 1e-6:
            return np.clip(arr, 0, 255).astype(np.uint8)
        else:
            return np.clip(arr / 257.0, 0, 255).astype(np.uint8)

    if np.issubdtype(arr.dtype, np.integer):
        max_val = int(arr.max())
        if max_val <= 255:
            return arr.astype(np.uint8)
        return (arr.astype(np.uint32) >> 8).astype(np.uint8)

    return np.clip(arr, 0, 255).astype(np.uint8)


# ─────────────────────────────────────────────────────────────
# COLOR COMPUTATION
# ─────────────────────────────────────────────────────────────
def compute_colors(app, mask=None, section_points=None):
    """
    Compute per-point colors for non-shaded modes.
    Main logic aligned with MicroStation-like display stretch.
    """
    mode = app.display_mode
    xyz = app.data["xyz"]

    if mask is None:
        mask = np.ones(xyz.shape[0], dtype=bool)

    pts = xyz[mask]
    colors = np.full((pts.shape[0], 3), 200, dtype=np.uint8)

    # RGB - use raw RGB by default, normalized safely
    if mode == "rgb" and app.data.get("rgb") is not None:
        rgb_raw = app.data["rgb"][mask]
        colors = _normalize_rgb_to_uint8(rgb_raw)

    # INTENSITY
    elif mode == "intensity" and app.data.get("intensity") is not None:
        intens = app.data["intensity"][mask].astype(np.float64)
        colors = _microstation_intensity_rgb(
            intens,
            low_pct=getattr(app, "intensity_clip_low", 0.5),
            high_pct=getattr(app, "intensity_clip_high", 99.8),
            gamma=getattr(app, "intensity_gamma", 1.35),
        )

    # ELEVATION
    elif mode == "elevation":
        if section_points is not None:
            if section_points.shape[1] >= 3:
                z = section_points[:, 2]
            else:
                z = section_points[:, 1]
        else:
            z = pts[:, 2]

        colors = _microstation_elevation_rgb(
            z,
            color_ramp=getattr(app, "elevation_color_ramp", None),
            low_pct=getattr(app, "elevation_clip_low", 1.0),
            high_pct=getattr(app, "elevation_clip_high", 99.0),
        )

    # DEPTH
    elif mode == "depth":
        try:
            if section_points is not None and hasattr(app, "sec_vtk") and app.sec_vtk is not None:
                cam = app.sec_vtk.renderer.GetActiveCamera()
                colors = _microstation_depth_rgb_from_camera(
                    section_points,
                    cam,
                    low_pct=getattr(app, "depth_clip_low", 1.0),
                    high_pct=getattr(app, "depth_clip_high", 99.0),
                    color_scheme=getattr(app, "depth_color_scheme", "grayscale"),
                    gamma=getattr(app, "depth_gamma", 1.0),
                )
            else:
                cam = app.vtk_widget.renderer.GetActiveCamera()
                colors = _microstation_depth_rgb_from_camera(
                    pts,
                    cam,
                    low_pct=getattr(app, "depth_clip_low", 1.0),
                    high_pct=getattr(app, "depth_clip_high", 99.0),
                    color_scheme=getattr(app, "depth_color_scheme", "grayscale"),
                    gamma=getattr(app, "depth_gamma", 1.0),
                )
        except Exception:
            colors[:] = 128

    # CLASSIFICATION
    elif mode in ("class", "shaded_class") and app.data.get("classification") is not None:
        classes = app.data["classification"][mask]
        colors = np.zeros((pts.shape[0], 3), dtype=np.uint8)

        if not hasattr(app, "class_palette") or not app.class_palette:
            unique_classes = np.unique(classes)
            app.class_palette = {
                int(code): {"color": (160, 160, 160), "show": True}
                for code in unique_classes
            }

        for code in np.unique(classes):
            local_mask = classes == code
            entry = app.class_palette.get(int(code), {"color": (128, 128, 128), "show": True})
            if entry["show"]:
                colors[local_mask] = entry["color"]
            else:
                colors[local_mask] = [0, 0, 0]

        weight = getattr(app, "class_weight", 1.0)
        colors = np.clip(colors * weight, 0, 255).astype(np.uint8)

    return colors


# ═══════════════════════════════════════════════════════════════════════════
# HELPER: MicroStation 5-Color Rainbow (EXACT)
# ═══════════════════════════════════════════════════════════════════════════
def _microstation_rainbow_5color(norm):
    """
    Fast vectorized Blue -> Cyan -> Green -> Yellow -> Red
    """
    norm = np.clip(np.asarray(norm, dtype=np.float32), 0.0, 1.0)

    stops = np.array([0.00, 0.25, 0.50, 0.75, 1.00], dtype=np.float32)

    r = np.interp(norm, stops, np.array([0,   0,   0, 255, 255], dtype=np.float32))
    g = np.interp(norm, stops, np.array([0, 255, 255, 255,   0], dtype=np.float32))
    b = np.interp(norm, stops, np.array([255,255,   0,   0,   0], dtype=np.float32))

    return np.column_stack((r, g, b)).astype(np.uint8)


def _apply_custom_color_ramp(norm, color_ramp):
    """
    Apply user-defined color ramp from elevation settings dialog.
    
    Args:
        norm: Normalized values (0.0 to 1.0)
        color_ramp: List of (position, (r, g, b)) tuples
        
    Returns:
        RGB colors array (N x 3, uint8)
    """
    colors = np.zeros((len(norm), 3), dtype=np.uint8)
    ramp = sorted(color_ramp, key=lambda x: x[0])  # Sort by position
    
    for i, val in enumerate(norm):
        # Find surrounding color stops
        for j in range(len(ramp) - 1):
            pos1, col1 = ramp[j]
            pos2, col2 = ramp[j + 1]
            
            if pos1 <= val <= pos2:
                # Linear interpolation between two control points
                if pos2 - pos1 < 1e-6:
                    colors[i] = col1
                else:
                    t = (val - pos1) / (pos2 - pos1)
                    r = int(col1[0] * (1 - t) + col2[0] * t)
                    g = int(col1[1] * (1 - t) + col2[1] * t)
                    b = int(col1[2] * (1 - t) + col2[2] * t)
                    colors[i] = [r, g, b]
                break
        else:
            # Handle edge cases
            if val <= ramp[0][0]:
                colors[i] = ramp[0][1]
            else:
                colors[i] = ramp[-1][1]
    
    return colors


def _apply_custom_color_ramp(norm, color_ramp):
    """
    Fast vectorized custom ramp interpolation.
    color_ramp = [(pos, (r,g,b)), ...]
    """
    norm = np.clip(np.asarray(norm, dtype=np.float32), 0.0, 1.0)

    ramp = sorted(color_ramp, key=lambda x: x[0])
    if len(ramp) < 2:
        return _microstation_rainbow_5color(norm)

    pos = np.array([float(p) for p, _ in ramp], dtype=np.float32)
    cols = np.array([c for _, c in ramp], dtype=np.float32)

    r = np.interp(norm, pos, cols[:, 0])
    g = np.interp(norm, pos, cols[:, 1])
    b = np.interp(norm, pos, cols[:, 2])

    return np.column_stack((r, g, b)).astype(np.uint8)


# ═══════════════════════════════════════════════════════════════════════════
# HELPER FUNCTION - MANUAL JET COLORMAP (if matplotlib unavailable)
# ═══════════════════════════════════════════════════════════════════════════
def _manual_jet_colormap(norm):
    """
    Approximate matplotlib's 'jet' colormap without matplotlib dependency.
    Blue (0.0) → Cyan (0.25) → Green (0.5) → Yellow (0.75) → Red (1.0)
    """
    colors = np.zeros((len(norm), 3), dtype=np.uint8)
    
    for i, val in enumerate(norm):
        if val < 0.25:
            # Blue → Cyan
            t = val / 0.25
            colors[i] = [0, int(t * 255), 255]
        elif val < 0.5:
            # Cyan → Green
            t = (val - 0.25) / 0.25
            colors[i] = [0, 255, int((1 - t) * 255)]
        elif val < 0.75:
            # Green → Yellow
            t = (val - 0.5) / 0.25
            colors[i] = [int(t * 255), 255, 0]
        else:
            # Yellow → Red
            t = (val - 0.75) / 0.25
            colors[i] = [255, int((1 - t) * 255), 0]
    
    return colors


class ShadedMeshWorker(QThread):
    """Worker thread to build shaded mesh asynchronously."""
    finished = Signal(object)

    def __init__(self, app):
        super().__init__()
        self.app = app

    def run(self):
        try:
            xyz = self.app.data["xyz"]
            classes = self.app.data["classification"]
            if xyz is None or classes is None or len(xyz) < 3:
                self.finished.emit(None)
                return

            xy = np.asarray(xyz[:, :2], dtype=np.float64)
            if not np.isfinite(xy).all():
                self.finished.emit(None)
                return
            if np.ptp(xy[:, 0]) <= 1e-12 or np.ptp(xy[:, 1]) <= 1e-12:
                self.finished.emit(None)
                return

            # Prevent runaway Delaunay RAM spikes on very large clouds.
            max_tri_points = 1_500_000
            if len(xy) > max_tri_points:
                keep_idx = np.linspace(0, len(xy) - 1, max_tri_points, dtype=np.int64)
                xyz = xyz[keep_idx]
                classes = classes[keep_idx]
                xy = xy[keep_idx]

            tri = Delaunay(xy)
            F = tri.simplices
            v1, v2, v3 = xyz[F[:, 0]], xyz[F[:, 1]], xyz[F[:, 2]]
            fn = np.cross(v2 - v1, v3 - v1)
            fn /= np.linalg.norm(fn, axis=1, keepdims=True) + 1e-9

            az = np.deg2rad(getattr(self.app, "last_shade_azimuth", 45.0))
            el = np.deg2rad(getattr(self.app, "last_shade_angle", 45.0))
            Ld = np.array([np.cos(el) * np.cos(az),
                           np.cos(el) * np.sin(az),
                           np.sin(el)])
            Ld /= np.linalg.norm(Ld)
            shade = getattr(self.app, "shade_ambient", 0.2) + \
                    (1 - getattr(self.app, "shade_ambient", 0.2)) * np.clip(fn @ Ld, 0, 1)

            colors = np.zeros((F.shape[0], 3), dtype=np.uint8)
            for i, face in enumerate(F):
                c = classes[face]
                majority = np.bincount(c).argmax()
                entry = self.app.class_palette.get(int(majority), {"color": (128, 128, 128)})
                base = np.array(entry["color"], dtype=np.float32)
                colors[i] = np.clip(base * shade[i], 0, 255)

            faces = np.hstack([np.full((F.shape[0], 1), 3), F]).astype(np.int32)
            mesh = pv.PolyData(xyz, faces)
            mesh.cell_data["RGB"] = colors

            self.finished.emit(mesh)
        except Exception as e:
            print(f"⚠️ ShadedMeshWorker failed: {e}")
            self.finished.emit(None)


# ─────────────────────────────────────────────────────────────
# MAIN UPDATE FUNCTION
# ─────────────────────────────────────────────────────────────
def update_pointcloud(app, mode="rgb"):
    """
    Single unified point cloud update function.
    ✅ Handles all display modes with proper validation
    ✅ Supports saturation and sharpness amplifiers for depth/intensity modes
    ✅ FIX: Properly cleans up orphan actors when switching between modes
    ✅ FIX: _sync_overlay_clipping_range is now defined in this module
    """
    import numpy as np
    import pyvista as pv

    if app.data is None or "xyz" not in app.data:
        print("⚠️ No point cloud data loaded")
        return

    xyz = app.data["xyz"]

    if len(xyz) == 0:
        print("⚠️ Empty point cloud")
        app.vtk_widget.clear()
        app.vtk_widget.render()
        return

    # ─────────────────────────────────────────────────────────
    # SURFACE MODE - isolated elevation + shading mesh
    # ─────────────────────────────────────────────────────────
    if mode == "surface":
        try:
            from gui.surface_mode import render_surface_mode, remove_shaded_class_mesh_actors
            _remove_pyvista_point_actors(app)
            remove_shaded_class_mesh_actors(app)
            app.display_mode = "surface"
            if render_surface_mode(app):
                _restore_snt_after_clear(app)
                return
        except Exception as _surface_err:
            print(f"⚠️ Surface mode failed, falling back to elevation: {_surface_err}")
        return update_pointcloud(app, "elevation")

    # Any non-Surface mode must detach Surface actors first so Surface never bleeds
    # into RGB/intensity/elevation/class/shaded views.
    try:
        from gui.surface_mode import detach_surface_before_non_surface_mode
        detach_surface_before_non_surface_mode(app, requested_mode=mode)
    except Exception:
        pass

    # ─────────────────────────────────────────────────────────
    # SHADED CLASS MODE
    # ─────────────────────────────────────────────────────────
    if mode == "shaded_class":
        from gui.shading_display import update_shaded_class, clear_shading_cache

        classes = app.data.get("classification")
        if classes is None:
            print("⚠️ No classification found, falling back to class view")
            return update_pointcloud(app, "class")

        # ✅ FIX: Remove orphan pyvista actors BEFORE shading
        # This prevents depth/intensity point actors from bleeding through
        _remove_pyvista_point_actors(app)

        app.display_mode = "shaded_class"
        clear_shading_cache("mode switch from menu")
        update_shaded_class(
            app,
            getattr(app, "last_shade_azimuth", 45.0),
            getattr(app, "last_shade_angle", 45.0),
            getattr(app, "shade_ambient", 0.2),
            force_rebuild=True
        )
        _restore_snt_after_clear(app)
        return

    # ─────────────────────────────────────────────────────────
    # ─────────────────────────────────────────────────────────
    if mode == "class":
        # ✅ FIX: Remove orphan pyvista actors BEFORE switching to class
        # This prevents depth/intensity actors from persisting over the
        # unified actor that class mode uses
        _remove_pyvista_point_actors(app)

        from gui.class_display import update_class_mode
        update_class_mode(app, force_refresh=True)
        _restore_snt_after_clear(app)
        return

    # ─────────────────────────────────────────────────────────
    # PYVISTA-BASED MODES: RGB / INTENSITY / ELEVATION / DEPTH
    # ─────────────────────────────────────────────────────────

    # Line mode uses LAS Point Source ID as the flight-line identifier and
    # filters strictly to entries checked in Display Mode > Lines.
    if mode == "line":
        source_ids = app.data.get("point_source_id")
        if source_ids is None or len(source_ids) != len(xyz):
            print("⚠️ This point cloud has no usable Point Source ID flight-line data")
            return
        from gui.flight_line_filter import flight_line_visibility_mask
        visibility = dict(getattr(app, "flight_line_visibility", {}) or {})
        selected = [int(v) for v in np.unique(source_ids) if visibility.get(int(v), True)]
        vis_mask = flight_line_visibility_mask(app, len(xyz))
        xyz = xyz[vis_mask]
        if len(xyz) == 0:
            app.vtk_widget.clear()
            app.vtk_widget.render()
            return
        visible_ids = source_ids[vis_mask].astype(np.uint32, copy=False)
        # Stable, high-contrast per-line colours without storing a large LUT.
        colors = np.empty((len(visible_ids), 3), dtype=np.uint8)
        line_colors = dict(getattr(app, "flight_line_colors", {}) or {})
        for line_id in np.unique(visible_ids):
            lid = int(line_id)
            rgb = line_colors.get(
                lid,
                ((lid * 67 + 53) % 256,
                 (lid * 131 + 97) % 256,
                 (lid * 193 + 181) % 256),
            )
            colors[visible_ids == line_id] = rgb
        print(f"✈ Line visibility mask: {len(xyz):,}/{len(source_ids):,} points; {len(selected)} lines")
    else:
        from gui.flight_line_filter import flight_line_visibility_mask
        vis_mask = flight_line_visibility_mask(app, len(xyz))

    # Build a class-visibility mask so only checked classes are rendered.
    # This applies to depth / intensity / elevation / rgb — matching the
    # behaviour users expect from the Display Mode dialog checkboxes.
    _classes = app.data.get("classification")
    _palette = getattr(app, "class_palette", None)
    if mode != "line" and _classes is not None and _palette:
        class_mask = np.zeros(len(app.data["xyz"]), dtype=bool)
        for _code in np.unique(_classes):
            _entry = _palette.get(int(_code), {"show": True})
            if _entry.get("show", True):
                class_mask |= (_classes == _code)
        vis_mask &= class_mask
        _vis_count = int(vis_mask.sum())
        _total = len(xyz)
        print(f"🎯 Class visibility mask: {_vis_count:,}/{_total:,} points visible ({mode} mode)")

    if mode != "line":
        xyz = xyz[vis_mask]

    if len(xyz) == 0:
        print("⚠️ No visible points after class filter — check Display Mode checkboxes")
        app.vtk_widget.clear()
        app.vtk_widget.render()
        return

    if mode != "line":
        colors = compute_colors(app, mask=vis_mask)

    # ✅ CRITICAL VALIDATION
    if len(colors) == 0:
        print("⚠️ Empty colors array from compute_colors()")
        app.vtk_widget.clear()
        app.vtk_widget.render()
        return

    if len(xyz) != len(colors):
        print(f"⚠️ Length mismatch: xyz={len(xyz)}, colors={len(colors)}")
        min_len = min(len(xyz), len(colors))
        xyz = xyz[:min_len]
        colors = colors[:min_len]
        print(f"   Truncated to {min_len:,} points")

    # ✅ Apply frequency amplifiers for depth and intensity modes
    if mode in ["depth", "intensity"]:
        saturation = getattr(app, "current_saturation", 1.0)
        sharpness = getattr(app, "current_sharpness", 1.0)

        print(f"🎚️ Applying amplifiers: saturation={saturation:.2f}x, sharpness={sharpness:.2f}x")

        if sharpness != 1.0:
            colors_norm = colors.astype(np.float32) / 255.0
            colors_norm = 0.5 + (colors_norm - 0.5) * sharpness
            colors_norm = np.clip(colors_norm, 0, 1)
            colors = (colors_norm * 255).astype(np.uint8)
            print(f"   ✅ Sharpness applied: {sharpness:.2f}x")

        if saturation != 1.0:
            colors_float = colors.astype(np.float32)
            gray = 0.299 * colors_float[:, 0] + 0.587 * colors_float[:, 1] + 0.114 * colors_float[:, 2]
            gray = gray[:, np.newaxis]
            colors_float = gray + (colors_float - gray) * saturation
            colors = np.clip(colors_float, 0, 255).astype(np.uint8)
            print(f"   ✅ Saturation applied: {saturation:.2f}x")

    # ✅ FIX: Remove the unified actor if it exists, so it doesn't
    # render underneath the pyvista-added actors
    try:
        unified_actor = getattr(app, '_unified_actor', None)
        if unified_actor is not None:
            renderer = app.vtk_widget.renderer
            if renderer is not None:
                renderer.RemoveActor(unified_actor)
                print("  🧹 Removed unified actor before pyvista mode")
    except Exception:
        pass

    # ✅ FIX: Also remove any shading mesh actors
    try:
        renderer = app.vtk_widget.renderer
        if renderer is not None:
            actors_to_remove = []
            actor_collection = renderer.GetActors()
            actor_collection.InitTraversal()
            for i in range(actor_collection.GetNumberOfItems()):
                actor = actor_collection.GetNextActor()
                if actor is not None and getattr(actor, '_is_shading_mesh', False):
                    actors_to_remove.append(actor)
            for actor in actors_to_remove:
                renderer.RemoveActor(actor)
            if actors_to_remove:
                print(f"  🧹 Removed {len(actors_to_remove)} shading mesh actors")
    except Exception:
        pass

    app.vtk_widget.clear()

    # Dynamic class-weighted point size
    # Note: computed on full original-length arrays before vis_mask is applied.
    base_point_size = 2.0
    classes = app.data.get("classification")
    _orig_xyz_len = app.data["xyz"].shape[0]   # full length before vis_mask filtering

    if not hasattr(app, "class_weights"):
        app.class_weights = {}

    if classes is not None:
        weights = np.ones_like(classes, dtype=float)
        for cls_code, w in app.class_weights.items():
            weights[classes == cls_code] = w
        point_sizes = np.clip(base_point_size * weights, 1.0, 8.0)
    else:
        point_sizes = np.ones(_orig_xyz_len, dtype=float) * base_point_size

    app.data["point_size"] = point_sizes
    print(f"📏 Point sizes: min={point_sizes.min():.1f}, max={point_sizes.max():.1f}")

    # Apply the visibility mask to point_sizes to match filtered xyz/colors length
    point_sizes = point_sizes[vis_mask]

    border_pct = getattr(app, "point_border_percent", 0)
    halo_add = min(1 + int(border_pct / 4), 5)

    colors_u8 = colors.astype(np.uint8)

    # Draw border FIRST (underneath) if enabled
    if border_pct > 0:
        border_cloud = pv.PolyData(xyz)
        border_cloud["RGB"] = np.full_like(colors_u8, 255, dtype=np.uint8)
        app.vtk_widget.add_points(
            border_cloud,
            scalars="RGB",
            rgb=True,
            point_size=np.mean(point_sizes) + halo_add,
            opacity=0.3,
        )
        _tag_pyvista_actor(app)  # ✅ Tag for cleanup

    # Draw main points SECOND (on top) - ONLY ONCE
    cloud = pv.PolyData(xyz)
    cloud["RGB"] = colors_u8
    app.vtk_widget.add_points(
        cloud,
        scalars="RGB",
        rgb=True,
        point_size=np.mean(point_sizes),
    )
    _tag_pyvista_actor(app)  # ✅ Tag for cleanup

    from gui.theme_manager import ThemeManager
    bg_color = "white" if ThemeManager.current() == "light" else "black"
    app.vtk_widget.set_background(bg_color)
    from gui.scene_render_pipeline import sync_scene_background
    sync_scene_background(app)
    from gui.views import set_view
    set_view(app, app.current_view)

    # Cross-section
    if hasattr(app, "sec_vtk") and app.sec_vtk is not None:
        try:
            app.sec_vtk.clear()
        except AttributeError:
            print("⚠️ sec_vtk already cleared")
        if getattr(app, "section_points", None) is not None:
            slice_xyz = app.section_points
            slice_colors = compute_colors(
                app,
                mask=getattr(app.section_controller, "last_mask", None),
                section_points=slice_xyz
            )

            if mode in ["depth", "intensity"]:
                # Only apply if user explicitly enabled them (not default)
                if hasattr(app, '_use_display_amplifiers') and app._use_display_amplifiers:
                    saturation = getattr(app, "current_saturation", 1.0)
                    sharpness = getattr(app, "current_sharpness", 1.0)

                if sharpness != 1.0:
                    slice_colors_norm = slice_colors.astype(np.float32) / 255.0
                    slice_colors_norm = 0.5 + (slice_colors_norm - 0.5) * sharpness
                    slice_colors_norm = np.clip(slice_colors_norm, 0, 1)
                    slice_colors = (slice_colors_norm * 255).astype(np.uint8)

                if saturation != 1.0:
                    slice_colors_float = slice_colors.astype(np.float32)
                    gray = 0.299 * slice_colors_float[:, 0] + 0.587 * slice_colors_float[:, 1] + 0.114 * slice_colors_float[:, 2]
                    gray = gray[:, np.newaxis]
                    slice_colors_float = gray + (slice_colors_float - gray) * saturation
                    slice_colors = np.clip(slice_colors_float, 0, 255).astype(np.uint8)

            slice_cloud = pv.PolyData(slice_xyz)
            slice_cloud["RGB"] = slice_colors
            app.sec_vtk.add_points(slice_cloud, scalars="RGB", rgb=True, point_size=2)
            app.sec_vtk.set_background(bg_color)

    # Restore camera
    try:
        if hasattr(app, "_saved_camera_state") and app._saved_camera_state:
            s = app._saved_camera_state
            cam = app.vtk_widget.renderer.GetActiveCamera()
            cam.SetPosition(s["pos"])
            cam.SetFocalPoint(s["fp"])
            cam.SetViewUp(s["vu"])
            cam.SetParallelProjection(s["parallel"])
            cam.SetParallelScale(s["scale"])
            print("✅ Camera restored")
    except Exception as e:
        print(f"⚠️ Camera restore failed: {e}")

    # Re-add SNT actors with Z offset
    _restore_snt_after_clear(app)

    # Expand clipping range to include SNT overlay actors
    _sync_overlay_clipping_range(app)


def force_interactor_ready(app, delay_ms=200):
    """Fully re-initialize VTK interactor."""
    try:
        def _activate():
            try:
                plotter = getattr(app.vtk_widget, "plotter", None)
                if plotter is None:
                    return
                iren = getattr(plotter, "iren", None)
                if iren is None:
                    return

                if hasattr(iren, "Initialize"):
                    iren.Initialize()
                if hasattr(iren, "Start"):
                    iren.Start()

                if hasattr(app.vtk_widget, "setFocus"):
                    app.vtk_widget.setFocus()
                if hasattr(app.vtk_widget, "activateWindow"):
                    app.vtk_widget.activateWindow()

                camera = plotter.renderer.GetActiveCamera()
                current_style = iren.GetInteractorStyle()
                current_style_name = (
                    current_style.GetClassName() if current_style is not None else "None"
                )

                if getattr(app, "is_3d_mode", False):
                    if current_style_name != "vtkInteractorStyleTrackballCamera":
                        if hasattr(plotter, "enable_trackball_style"):
                            plotter.enable_trackball_style()
                        else:
                            plotter.enable_trackball_camera()
                    if camera is not None:
                        camera.ParallelProjectionOff()
                elif hasattr(app, "ensure_main_view_2d_interaction"):
                    app.ensure_main_view_2d_interaction(
                        preserve_camera=True,
                        reason="force_interactor_ready",
                    )
                else:
                    from vtkmodules.vtkInteractionStyle import vtkInteractorStyleImage

                    style_2d = vtkInteractorStyleImage()
                    try:
                        style_2d.SetInteractionModeToImageSlicing()
                    except Exception:
                        pass
                    iren.SetInteractorStyle(style_2d)
                    if camera is not None:
                        camera.ParallelProjectionOn()

                plotter.render()

                # ✅ FIX: After iren.Initialize()/Start() may have recreated the
                # underlying VTK interactor, reinstall digitize tool observers.
                # Without this, the observers installed during _on_load_finished
                # or the open_file inline clear are lost, and digitize tools
                # silently stop responding to mouse events.
                if hasattr(app, "digitizer") and app.digitizer:
                    try:
                        app.digitizer._check_and_update_renderers()
                        app.digitizer._reinstall_all_observers()
                    except Exception:
                        pass

                print("🟢 Interactor ready")
            except Exception as e:
                print(f"⚠️ _activate() failed: {e}")

        QTimer.singleShot(delay_ms, _activate)
    except Exception as e:
        print(f"⚠️ force_interactor_ready() failed: {e}")


def fast_update_colors(app, changed_mask=None):
    """
    ✅ TRUE PARTIAL UPDATE: Routes directly to unified_actor_manager zero-copy functions.
    """
    from gui.unified_actor_manager import fast_palette_refresh, fast_undo_update

    if changed_mask is None:
        return fast_palette_refresh(app, border_percent=getattr(app, "point_border_percent", 0.0))
    else:
        return fast_undo_update(app, changed_mask, border_percent=getattr(app, "point_border_percent", 0.0))


def fast_update_main_view(app):
    """Fast refresh for main view."""
    try:
        print("⚡ fast_update_main_view()")
        fast_update_colors(app, None)
    except Exception as e:
        print(f"⚠️ fast_update_main_view() failed: {e}")

    _restore_snt_after_clear(app)
