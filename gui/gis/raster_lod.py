# ─────────────────────────────────────────────────────────────────────────────
# raster_lod.py — dynamic zoom-based reload for imported GeoTIFF rasters
#
# import_geotiff_as_texture() (vector_export.py) bakes ONE static texture for
# the whole file, capped by VRAM budget. That's fine at overview zoom, but
# once a user zooms in past that baked resolution there is no more detail to
# show - VTK just magnifies the same texels. QGIS avoids this by re-reading
# the source file windowed to the current view on every zoom/pan step.
#
# This module reproduces that behaviour: on every camera change (2D parallel
# / top view only - a perspective camera has no single rectangular "visible
# extent" to key a window off of), it works out the world-space window
# currently on screen, re-reads just that window from the source GeoTIFF at a
# resolution matched to the viewport, and swaps the raster actor's texture +
# plane geometry to it. Debounced so a drag/zoom gesture doesn't spawn a read
# per frame, and skipped when the current texture already covers the view at
# sufficient detail so plain panning within a loaded window is free.
#
# Only rasters flagged "eligible" in actor._raster_lod_meta participate (see
# vector_export.py) - GCP (rotated) placements and single-band/elevation
# rasters keep the original static-texture behaviour.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import math
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from functools import lru_cache
import os

_DEBOUNCE_MS = 120
# How far past the visible view to pre-load on each side, as a fraction of
# the view's own width/height. Panning within this padded region reuses the
# already-loaded texture with zero fetch - genuinely instant, not just fast.
# Kept moderate rather than large: the padded window's target pixel count
# grows with the SQUARE of (1 + 2*margin), and that has to stay within
# _BASE_MEGAPIXEL_CAP/_TIGHT_ZOOM_MEGAPIXEL_CAP below or the extra padding
# would eat into the sharpness budget meant for the visible center, not just
# add buffer around it. 0.35 covers roughly meaningful pan distance (more
# than a third of the current view in every direction) while keeping the
# fetched crop well clear of those caps and of GPU max-texture-size limits.
_REFETCH_MARGIN = 0.35
_ZOOM_TOLERANCE = 1.1    # don't refetch for <10% extra zoom over what's already loaded
_FAIL_BACKOFF_BASE_S = 2.0
_FAIL_BACKOFF_MAX_S = 30.0
_FRAME_CHECK_THROTTLE_S = 0.1  # min gap between resize/camera-rebind checks

# Texture budget per background fetch. Wide/overview navigation keeps the
# original conservative cap (cheap, fast background reads while browsing).
# Once the view is zoomed in tight enough that classification precision
# matters (view width below _TIGHT_ZOOM_EXTENT_M), the cap is raised so
# building edges etc. aren't softened by downsampling below the ortho's
# native resolution. Tight zoom on a fine-GSD ortho can still need a
# genuinely large crop - this trades a longer (but still fully async,
# non-blocking) background fetch for real sharpness exactly when it's
# needed, not a free change.
_BASE_MEGAPIXEL_CAP = 8_000_000
_BASE_DIM_CAP = 4096
_TIGHT_ZOOM_EXTENT_M = 400.0
_TIGHT_ZOOM_MEGAPIXEL_CAP = 32_000_000
_TIGHT_ZOOM_DIM_CAP = 8192


def _get_render_window(vtk_widget):
    """Resolve the host VTK render window without raising if not yet wired."""
    if vtk_widget is None:
        return None
    for getter in (
        lambda: vtk_widget.interactor.GetRenderWindow(),
        lambda: vtk_widget.GetRenderWindow(),
        lambda: getattr(vtk_widget, "render_window", None),
    ):
        try:
            rw = getter()
            if rw is not None:
                return rw
        except Exception:
            continue
    return None


def _visible_world_bounds(app):
    """(minx, maxx, miny, maxy, screen_w, screen_h) of the current camera
    view in scene coords, or None if not in 2D parallel (top) projection."""
    # Camera queries are a navigation hot path. Pipeline repair traverses all
    # scene actors and belongs to layer setup, never each camera modification.
    renderer = getattr(getattr(app, "vtk_widget", None), "renderer", None)
    if renderer is None:
        return None
    rw = _get_render_window(getattr(app, "vtk_widget", None))
    if rw is None:
        return None
    try:
        cam = renderer.GetActiveCamera()
        direction = cam.GetDirectionOfProjection()
        up = cam.GetViewUp()
        # VTK tools can leave tiny floating-point drift in an otherwise 2D
        # top camera. Reject genuinely tilted views, but do not disable native
        # raster LOD because another operation caused insignificant drift.
        if (not cam.GetParallelProjection() or abs(direction[2] + 1) > 1e-3
                or abs(up[0]) > 1e-3 or up[1] < 0.999):
            return None
        width, height = rw.GetSize()
        if width <= 0 or height <= 0:
            return None
        fx, fy, _fz = cam.GetFocalPoint()
        parallel_scale = max(1e-9, float(cam.GetParallelScale()))
        aspect = width / float(height)
        half_w = parallel_scale * aspect
        return (fx - half_w, fx + half_w, fy - parallel_scale, fy + parallel_scale,
                int(width), int(height))
    except Exception:
        return None


def _same_visible_view(first, second):
    """True when camera modifications did not change the 2D viewport.

    ResetCameraClippingRange and classification rendering modify the VTK
    camera too, but they do not change visible XY bounds or raster resolution.
    Treating those events as navigation needlessly discards a sharp LOD crop.
    """
    if first is None or second is None:
        return first is second
    if first[4:] != second[4:]:
        return False
    return all(
        math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-7)
        for a, b in zip(first[:4], second[:4])
    )


def _camera_xy_zoom_signature(app):
    """Cheap signature excluding focal Z and clipping-only camera changes."""
    renderer = getattr(getattr(app, "vtk_widget", None), "renderer", None)
    rw = _get_render_window(getattr(app, "vtk_widget", None))
    if renderer is None or rw is None:
        return None
    try:
        camera = renderer.GetActiveCamera()
        focal = camera.GetFocalPoint()
        return (
            float(focal[0]), float(focal[1]),
            float(camera.GetParallelScale()), tuple(rw.GetSize()),
        )
    except Exception:
        return None


def _same_camera_xy_zoom(first, second):
    if first is None or second is None:
        return first is second
    return (
        first[3] == second[3]
        and all(math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-7)
                for a, b in zip(first[:3], second[:3]))
    )


def _pixel_window_for_world(native_bounds, native_size, view_bounds, margin):
    left, right, bottom, top = native_bounds
    src_w, src_h = native_size
    vminx, vmaxx, vminy, vmaxy = view_bounds
    pad_x = (vmaxx - vminx) * margin
    pad_y = (vmaxy - vminy) * margin
    wx0, wx1 = max(vminx - pad_x, left), min(vmaxx + pad_x, right)
    wy0, wy1 = max(vminy - pad_y, bottom), min(vmaxy + pad_y, top)
    if wx1 <= wx0 or wy1 <= wy0:
        return None

    dxr = max(1e-9, right - left)
    dyr = max(1e-9, top - bottom)
    col0 = (wx0 - left) / dxr * src_w
    col1 = (wx1 - left) / dxr * src_w
    row0 = (top - wy1) / dyr * src_h
    row1 = (top - wy0) / dyr * src_h
    col0, col1 = max(0, int(col0)), min(src_w, int(math.ceil(col1)))
    row0, row1 = max(0, int(row0)), min(src_h, int(math.ceil(row1)))
    if col1 <= col0 or row1 <= row0:
        return None
    return {
        "col_off": col0, "row_off": row0,
        "width": col1 - col0, "height": row1 - row0,
        "wx0": left + col0 / src_w * dxr, "wx1": left + col1 / src_w * dxr,
        "wy0": top - row1 / src_h * dyr, "wy1": top - row0 / src_h * dyr,
    }


def _already_covers(last, view_bounds, screen_w):
    """True if the last-loaded window already covers the current view at
    equal-or-better detail, so this tick's refresh can be skipped."""
    if last is None:
        return False
    vminx, vmaxx, vminy, vmaxy = view_bounds
    if not (last["wx0"] <= vminx and vmaxx <= last["wx1"]
            and last["wy0"] <= vminy and vmaxy <= last["wy1"]):
        return False
    view_px_per_world = screen_w / max(1e-9, vmaxx - vminx)
    loaded_px_per_world = last["out_w"] / max(1e-9, last["wx1"] - last["wx0"])
    return loaded_px_per_world >= view_px_per_world / _ZOOM_TOLERANCE


def _style_for(app, path, actor=None):
    """The user's Raster Properties style for this layer, so the zoom refresh
    doesn't silently revert band mapping / stretch / gamma back to raw RGB."""
    try:
        from gui.gis.gis_layers import _registry
        for entry in _registry(app):
            if (entry.get("kind") == "raster" and entry.get("path") == path
                    and (actor is None or any(a is actor for a in entry.get("actors", [])))):
                if entry.get("style"):
                    return entry["style"]
                break
    except Exception:
        pass
    from gui.gis.raster_properties import default_raster_style
    return default_raster_style(3)


@lru_cache(maxsize=64)
def _source_ranges(path, stamp, size, indexes):
    """Small, source-wide sample; identical contrast limits for every crop."""
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    with rasterio.open(path) as src:
        data = src.read(list(indexes), out_shape=(len(indexes), min(256, src.height),
                                                 min(256, src.width)),
                        masked=True, resampling=Resampling.nearest)
    ranges = {}
    for index, band in zip(indexes, data):
        values = band.compressed()
        values = values[np.isfinite(values)]
        ranges[index] = tuple(np.percentile(values, (2, 98))) if values.size else (0, 255)
    return ranges


def _restore_overview(actor):
    """Keep the complete layer visible while a new crop is being read."""
    meta = getattr(actor, "_raster_lod_meta", None)
    if not meta or not meta.get("_last_window") or meta.get("_preview_image") is None:
        return False
    plane = actor.GetMapper().GetInputConnection(0, 0).GetProducer()
    left, right, bottom, top = meta["native_bounds"]
    plane.SetOrigin(left, bottom, meta["z"])
    plane.SetPoint1(right, bottom, meta["z"])
    plane.SetPoint2(left, top, meta["z"])
    plane.Update()
    actor.GetTexture().SetInputData(meta["_preview_image"])
    meta.pop("_last_window", None)
    return True


def _restore_cropped_overviews(app):
    """Restore full-extent previews before a new navigation frame is drawn.

    A detail texture changes its actor's plane to a viewport-sized crop. If
    the camera moves beyond that crop, keeping it until the debounced read
    finishes exposes the renderer background as a black rectangle. The
    overview is already resident, so restoring it synchronously is cheap and
    guarantees continuous coverage while the sharper window is fetched.
    """
    restored = 0
    for actor in list(getattr(app, "geotiff_actors", []) or []):
        try:
            if actor.GetVisibility() and _restore_overview(actor):
                restored += 1
        except (AttributeError, RuntimeError):
            continue
    return restored


def _load_window_texture(path, window, target_w, target_h, style):
    import numpy as np
    from rasterio.windows import Window
    from gui.gis.raster_properties import _read_source_bands, process_raster_array

    win = Window(window["col_off"], window["row_off"], window["width"], window["height"])
    bands, _count = _read_source_bands(path, style, window=win,
                                       out_shape=(target_h, target_w))
    if style.get("enhancement") == "minmax" and (style.get("min") is None or style.get("max") is None):
        stat = os.stat(path)
        style = {**style, "_band_ranges": _source_ranges(
            path, stat.st_mtime_ns, stat.st_size, tuple(sorted(bands)))}
    rgb = process_raster_array(bands, style)          # (h, w, 3) uint8
    return np.ascontiguousarray(rgb[::-1, :, :])


def _request_for(app, actor, view):
    meta = getattr(actor, "_raster_lod_meta", None)
    if not meta or not meta.get("eligible") or not actor.GetVisibility() or view is None:
        return None
    vminx, vmaxx, vminy, vmaxy, screen_w, screen_h = view
    bounds = meta["native_bounds"]
    clipped = (max(vminx, bounds[0]), min(vmaxx, bounds[1]),
               max(vminy, bounds[2]), min(vmaxy, bounds[3]))
    if clipped[0] >= clipped[1] or clipped[2] >= clipped[3]:
        return None
    style = deepcopy(_style_for(app, meta["path"], actor))
    last = meta.get("_last_window")
    # Demand no more than native resolution, including at the raster edges.
    density_x = min(screen_w / (vmaxx - vminx), meta["native_size"][0] / (bounds[1] - bounds[0]))
    density_y = min(screen_h / (vmaxy - vminy), meta["native_size"][1] / (bounds[3] - bounds[2]))
    if (last and meta.get("_last_style") == style
            and _already_covers(last, clipped, density_x * (clipped[1] - clipped[0]))
            and last["out_h"] / (last["wy1"] - last["wy0"]) >= density_y / _ZOOM_TOLERANCE):
        return None
    window = _pixel_window_for_world(bounds, meta["native_size"], view[:4], _REFETCH_MARGIN)
    target_w = min(window["width"], max(1, math.ceil((window["wx1"] - window["wx0"]) * density_x)))
    target_h = min(window["height"], max(1, math.ceil((window["wy1"] - window["wy0"]) * density_y)))
    if (vmaxx - vminx) <= _TIGHT_ZOOM_EXTENT_M:
        mp_cap, dim_cap = _TIGHT_ZOOM_MEGAPIXEL_CAP, _TIGHT_ZOOM_DIM_CAP
    else:
        mp_cap, dim_cap = _BASE_MEGAPIXEL_CAP, _BASE_DIM_CAP
    scale = min(1.0, (mp_cap / (target_w * target_h)) ** 0.5,
                dim_cap / target_w, dim_cap / target_h)
    w, h = max(1, int(target_w * scale)), max(1, int(target_h * scale))
    # At the texture budget limit, rereading the identical request adds no detail.
    if (last == {**window, "out_w": w, "out_h": h}
            and meta.get("_last_style") == style):
        return None
    return actor, meta, window, w, h, style


def _apply_texture(actor, meta, window, style, rgb_array):
    # Called only by the GUI timer: workers never access Qt, VTK, or app state.
    import vtk
    from vtk.util import numpy_support
    h, w = rgb_array.shape[0], rgb_array.shape[1]
    vtk_colors = numpy_support.numpy_to_vtk(
        rgb_array.reshape(-1, 3), deep=True, array_type=vtk.VTK_UNSIGNED_CHAR)
    vtk_colors.SetNumberOfComponents(3)
    vtk_colors.SetName("Colors")

    vtk_image = vtk.vtkImageData()
    vtk_image.SetDimensions(w, h, 1)
    vtk_image.GetPointData().SetScalars(vtk_colors)

    texture = actor.GetTexture()
    if texture is None:
        return
    texture.SetInputData(vtk_image)
    texture.InterpolateOff() if style.get("resampling", "nearest") == "nearest" else texture.InterpolateOn()
    texture.Modified()

    # The loaded image only covers `window`'s world extent, not the whole
    # raster - reposition the plane to match, or the texture would appear
    # squashed/misaligned across the old (larger) quad.
    try:
        plane = actor.GetMapper().GetInputConnection(0, 0).GetProducer()
        z = meta["z"]
        plane.SetOrigin(window["wx0"], window["wy0"], z)
        plane.SetPoint1(window["wx1"], window["wy0"], z)
        plane.SetPoint2(window["wx0"], window["wy1"], z)
        plane.Update()
    except Exception as exc:
        print(f"   ⚠️ Raster LOD plane update failed: {exc}")
        return

    meta["_last_window"] = {**window, "out_w": w, "out_h": h}
    meta["_last_style"] = style
    return True


_LOAD_SUPPRESS_MAX_S = 45.0  # hard ceiling in case _file_loader_worker never clears


def _load_in_progress(app):
    # LAS/LAZ load finalization (_on_load_finished in app_window.py) ends with
    # toggle_view_mode("2d") + fit_view(), a large camera jump from origin to
    # the dataset's real coordinates. Without this, that one jump queues a
    # full-resolution background raster read on top of everything else load
    # finalization is already doing. _file_loader_worker is set for the
    # worker's whole run AND for _on_load_finished's main-thread tail (cleared
    # only at its very last line) - not classification, which uses a separate
    # navigation path this flag never touches.
    #
    # _on_load_finished calls toggle_view_mode("2d") completely unguarded
    # partway through a ~300-line stretch; if that (or anything else in it)
    # raises, the function never reaches the line that clears
    # _file_loader_worker, leaving this flag stuck forever - which would
    # silently disable raster refresh for the rest of the session, not just
    # delay it. A time bound makes that self-heal instead of staying broken:
    # any real load finishes in well under _LOAD_SUPPRESS_MAX_S.
    worker = getattr(app, "_file_loader_worker", None)
    if worker is None:
        return False
    started = getattr(app, "_raster_lod_load_seen_at", None)
    now = time.monotonic()
    if started is None or getattr(app, "_raster_lod_load_seen_worker", None) is not worker:
        app._raster_lod_load_seen_at = now
        app._raster_lod_load_seen_worker = worker
        return True
    return (now - started) < _LOAD_SUPPRESS_MAX_S


_INTERACTION_SUPPRESS_MAX_S = 0.75  # self-heal stale UI flags without visible multi-second blur


def _raw_interaction_active(app):
    manager = getattr(app, "gpu_render_manager", None)
    return bool(getattr(manager, "_interaction_active", False)
                or getattr(manager, "_pan_in_progress", False)
                or getattr(app, "_qt_main_pan_active", False)
                or getattr(app, "_zoom_anim_active", False))


def _interaction_active_bounded(app):
    # Diagnostics on a real classification session (2026-09-07) showed
    # nav_active=True persisting continuously for minutes - through
    # classification, tool switches, cross-section create/dismiss, all of
    # it - with import/load both False, meaning one of the four flags above
    # got stuck true and never cleared. None of them are owned by this
    # module (gpu_render_manager / app_window.py's pan-tracking own setting
    # them), so the actual stuck-flag bug can't be fixed here - but this
    # bounds how long raster refresh stays suppressed by it regardless of
    # which flag or why, the same self-healing pattern as _load_in_progress
    # below. Any real gesture clears well under _INTERACTION_SUPPRESS_MAX_S.
    now = time.monotonic()
    if not _raw_interaction_active(app):
        app._raster_lod_interaction_since = None
        return False
    since = getattr(app, "_raster_lod_interaction_since", None)
    if since is None:
        app._raster_lod_interaction_since = now
        return True
    return (now - since) < _INTERACTION_SUPPRESS_MAX_S


def _navigation_active(app):
    return _interaction_active_bounded(app) or _load_in_progress(app)


class _Loader:
    """One in-flight read per app; new navigation replaces pending demand."""
    def __init__(self, app):
        from PySide6.QtCore import QTimer, QCoreApplication
        self.app = app
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="raster-read")
        self.future = None
        self.request = None
        self.generation = 0
        self.closed = False
        self.failed_keys = {}
        self.cursor = 0
        self.observers = []
        self.poll = QTimer()
        self.poll.setInterval(30)
        self.poll.timeout.connect(self.finish)
        qt_app = QCoreApplication.instance()
        if qt_app is not None:
            qt_app.aboutToQuit.connect(self.close)
        if hasattr(app, "destroyed"):
            app.destroyed.connect(self.close)

    def close(self, *_):
        if self.closed:
            return
        self.closed = True
        for obj, tag in self.observers:
            obj.RemoveObserver(tag)
        self.observers.clear()
        for timer in (self.poll, getattr(self.app, "_raster_lod_timer", None)):
            try:
                if timer is not None:
                    timer.stop()
            except RuntimeError:
                pass  # Qt may already have destroyed children during shutdown.
        self.executor.shutdown(wait=False, cancel_futures=True)

    def start(self):
        if self.closed or self.future is not None:
            return
        if getattr(self.app, "_shutdown_in_progress", False):
            self.close()
            return
        if getattr(self.app, "_geotiff_import_in_progress", False) or _navigation_active(self.app):
            self.app._raster_lod_timer.start(_DEBOUNCE_MS)
            return
        view = _visible_world_bounds(self.app)
        actors = list(getattr(self.app, "geotiff_actors", []) or [])
        now = time.monotonic()
        # Round-robin prevents a failing or repeatedly invalidated first layer
        # from starving the remaining files.
        for offset in range(len(actors)):
            index = (self.cursor + offset) % len(actors)
            actor = actors[index]
            request = _request_for(self.app, actor, view)
            if request is None:
                continue
            actor, meta, window, w, h, style = request
            key = (id(actor), repr(window), w, h, repr(style))
            failed = self.failed_keys.get(id(actor))
            if failed is not None and failed["key"] == key and now < failed["retry_at"]:
                continue
            self.cursor = (index + 1) % len(actors)
            self.request = (request, view, self.generation, key)
            self.future = self.executor.submit(_load_window_texture, meta["path"], window, w, h, style)
            self.poll.start()
            return

    def finish(self):
        if self.future is None or not self.future.done():
            return
        if getattr(self.app, "_shutdown_in_progress", False):
            self.close()
            return
        if getattr(self.app, "_geotiff_import_in_progress", False) or _navigation_active(self.app):
            return  # Keep disk results off the GPU until navigation/import settles.
        self.poll.stop()
        future, self.future = self.future, None
        request, view, generation, key = self.request
        self.request = None
        actor, meta, window, w, h, style = request
        try:
            rgb = future.result()
        except Exception as exc:
            prev = self.failed_keys.get(id(actor))
            attempts = prev["attempts"] + 1 if prev is not None and prev["key"] == key else 1
            backoff = min(_FAIL_BACKOFF_MAX_S, _FAIL_BACKOFF_BASE_S * attempts)
            self.failed_keys[id(actor)] = {
                "key": key, "attempts": attempts, "retry_at": time.monotonic() + backoff,
            }
            print(f"Raster LOD read failed, retrying in {backoff:.0f}s: {exc}")
            timer = getattr(self.app, "_raster_lod_timer", None)
            if timer is not None:
                remaining = timer.remainingTime() if timer.isActive() else -1
                delay_ms = int(backoff * 1000)
                if remaining < 0 or remaining > delay_ms:
                    timer.start(delay_ms)
        else:
            self.failed_keys.pop(id(actor), None)
            if (not self.closed and generation == self.generation
                    and view == _visible_world_bounds(self.app)
                    and any(a is actor for a in getattr(self.app, "geotiff_actors", []))
                    and actor.GetVisibility()
                    and getattr(actor, "_raster_lod_meta", None) is meta
                    and style == _style_for(self.app, meta["path"], actor)):
                if _apply_texture(actor, meta, window, style, rgb):
                    self.app.vtk_widget.render()
        # A camera debounce already pending gets priority over starting another read.
        if not self.closed and not self.app._raster_lod_timer.isActive():
            self.start()


def refresh_all(app):
    loader = getattr(app, "_raster_lod_loader", None)
    if loader is not None:
        loader.start()


def ensure_installed(app):
    """Wire up the debounced camera watcher once per app instance."""
    if getattr(app, "_raster_lod_installed", False):
        return
    try:
        from gui.scene_render_pipeline import ROLE_DATA, renderer_for_role
        renderer = renderer_for_role(app, ROLE_DATA)
        cam = renderer.GetActiveCamera() if renderer is not None else None
    except Exception:
        cam = None
    if cam is None:
        return
    app._raster_lod_installed = True
    app._raster_lod_loader = _Loader(app)
    app._raster_lod_renderer = renderer

    from PySide6.QtCore import QTimer
    timer = QTimer(app if hasattr(app, "children") else None)
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: refresh_all(app))
    app._raster_lod_timer = timer  # keep alive
    app._raster_lod_camera_signature = _camera_xy_zoom_signature(app)

    def _on_camera_changed(_obj=None, _evt=None):
        current_signature = _camera_xy_zoom_signature(app)
        previous_signature = getattr(app, "_raster_lod_camera_signature", None)
        if _same_camera_xy_zoom(previous_signature, current_signature):
            return
        app._raster_lod_camera_signature = current_signature
        # Do not discard a sharp crop here. _prepare_frame checks its world
        # coverage immediately before drawing: zooming further into the crop
        # keeps native pixels visible, while navigation beyond it restores the
        # full overview so uncovered areas never become black.
        app._raster_lod_loader.generation += 1
        app._raster_lod_frame_dirty = True
        timer.start(_DEBOUNCE_MS)

    def _rebind_camera_if_replaced():
        # The active camera can be swapped out (view reset, section tools,
        # etc.) without the old one firing a ModifiedEvent - re-point the
        # watcher or navigation on the new camera never triggers a refresh.
        renderer = getattr(app, "_raster_lod_renderer", None)
        current = renderer.GetActiveCamera() if renderer is not None else None
        old = getattr(app, "_raster_lod_camera", None)
        if current is None or current is old:
            return
        loader = app._raster_lod_loader
        for obj, tag in list(loader.observers):
            if obj is old:
                try:
                    obj.RemoveObserver(tag)
                except Exception:
                    pass
                loader.observers.remove((obj, tag))
        tag = current.AddObserver("ModifiedEvent", _on_camera_changed, -5.0)
        loader.observers.append((current, tag))
        app._raster_lod_camera = current
        _on_camera_changed()

    def _prepare_frame(_obj=None, _evt=None):
        # This fires on every render anywhere in the app once a raster is
        # imported (classification alone drives dozens of renders/sec), so
        # the resize/camera-rebind checks below - each a real VTK API call -
        # are throttled to run at most every _FRAME_CHECK_THROTTLE_S instead
        # of on literally every frame. A resize or camera swap can still lag
        # up to that long before being caught, imperceptible to a user but a
        # real cut to per-frame overhead during rapid classification renders.
        # The frame_dirty-triggered work below (the actual overview-restore
        # loop) is untouched - still evaluated every dirty frame, exactly as
        # before.
        now = time.monotonic()
        last_check = getattr(app, "_raster_lod_last_frame_check", 0.0)
        if now - last_check >= _FRAME_CHECK_THROTTLE_S:
            app._raster_lod_last_frame_check = now
            _rebind_camera_if_replaced()
            # A pure resize changes the viewport in pixels without touching
            # the camera, so it never reaches _on_camera_changed - detect it
            # here instead, via the cheap window size query rather than the
            # full _visible_world_bounds() coverage check below (kept off
            # the hot per-frame path unless something actually invalidated
            # the view).
            rw_now = _get_render_window(getattr(app, "vtk_widget", None))
            size = rw_now.GetSize() if rw_now is not None else None
            if size is not None and size != getattr(app, "_raster_lod_last_size", None):
                app._raster_lod_last_size = size
                _on_camera_changed()
        if not getattr(app, "_raster_lod_frame_dirty", False):
            return
        app._raster_lod_frame_dirty = False
        view = _visible_world_bounds(app)
        for actor in list(getattr(app, "geotiff_actors", []) or []):
            meta = getattr(actor, "_raster_lod_meta", {})
            last = meta.get("_last_window")
            if not last or not actor.GetVisibility():
                continue
            bounds = meta["native_bounds"]
            clipped = None if view is None else (max(view[0], bounds[0]), min(view[1], bounds[1]),
                                                  max(view[2], bounds[2]), min(view[3], bounds[3]))
            if clipped is None or not _already_covers(last, clipped, 0):
                _restore_overview(actor)

    rw = _get_render_window(getattr(app, "vtk_widget", None))
    if rw is not None:
        tag = rw.AddObserver("StartEvent", _prepare_frame)
        app._raster_lod_loader.observers.append((rw, tag))
    tag = cam.AddObserver("ModifiedEvent", _on_camera_changed, -5.0)
    app._raster_lod_loader.observers.append((cam, tag))
    app._raster_lod_camera = cam
    timer.start(_DEBOUNCE_MS)


def kick(app):
    """Install the watcher if needed and schedule a refresh soon - called
    right after a new raster is registered, so it gets refined without
    waiting for the user to first touch the camera."""
    ensure_installed(app)
    timer = getattr(app, "_raster_lod_timer", None)
    if timer is not None:
        app._raster_lod_loader.generation += 1
        app._raster_lod_loader.failed_keys.clear()
        timer.start(_DEBOUNCE_MS)


def _demo():
    """Self-check for the pixel<->world window math (no GDAL/VTK needed)."""
    native_bounds = (0.0, 1000.0, 0.0, 500.0)   # left, right, bottom, top
    native_size = (4000, 2000)                  # src_w, src_h (4px per world unit)

    # Zoomed into the raster's lower-left quadrant.
    view = (0.0, 100.0, 0.0, 100.0)
    win = _pixel_window_for_world(native_bounds, native_size, view, margin=0.0)
    assert win is not None
    assert win["col_off"] == 0 and win["row_off"] == 1600  # top-origin rows
    assert win["width"] == 400 and win["height"] == 400

    # View entirely outside the raster's bounds -> no window.
    assert _pixel_window_for_world(native_bounds, native_size, (2000, 2100, 0, 100), 0.0) is None

    # Margin pads the request without going outside native_bounds.
    padded = _pixel_window_for_world(native_bounds, native_size, view, margin=0.5)
    assert padded["width"] > win["width"]

    # A previously-loaded window covering the view at >= detail is reused.
    last = {"wx0": -10, "wx1": 110, "wy0": -10, "wy1": 110, "out_w": 480, "out_h": 480}
    assert _already_covers(last, (0.0, 100.0, 0.0, 100.0), screen_w=400) is True
    # Same area but the view now demands much higher pixel density -> refetch.
    assert _already_covers(last, (40.0, 60.0, 40.0, 60.0), screen_w=400) is False
    # No prior window -> always refetch.
    assert _already_covers(None, (0.0, 100.0, 0.0, 100.0), screen_w=400) is False

    print("raster_lod self-check OK")


if __name__ == "__main__":
    _demo()
