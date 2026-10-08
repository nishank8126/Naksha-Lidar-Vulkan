"""render_backend.py - render backend seam, now wired into app_window.py.

STATUS: VtkRenderBackend (a thin call-through to the existing production VTK
code) is the default/only active backend. VulkanRenderBackend is a real
ctypes bridge to native/naksha_vulkan/build/naksha_vulkan.dll, but it is
opt-in only - selected via AppRenderBackendOwner below, which defaults to
"vtk" unless the NAKSHA_RENDER_BACKEND=vulkan environment variable is set,
and always falls back to "vtk" silently (logged, never raised) if Vulkan
probing/initialization fails for any reason. A failure in this module must
never prevent app_window.py / main.py from starting.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

import math

import numpy as np

# PART 27: report a CLASS/XYZ count mismatch ONCE per submission site. The
# old behaviour raised ValueError and aborted the whole frame tick, which
# surfaced as "[stream] frame tick error #1" during partial FULL_RESIDENT
# loading. CLASS is optional (Neutral mode needs XYZ only), so a mismatched
# array is dropped instead.
_CLASS_MISMATCH_SEEN = set()


def _warn_optional_mismatch(name: str, site: str, xyz_count: int,
                            shape) -> None:
    """PART 27: report ONE optional-attribute count mismatch per site."""
    key = (str(name), str(site), int(xyz_count), str(shape))
    if key in _CLASS_MISMATCH_SEEN:
        return
    _CLASS_MISMATCH_SEEN.add(key)
    try:
        print(f"[{name.upper()} STREAM MISMATCH] site={site} "
              f"xyz_count={xyz_count} shape={tuple(shape)} -> dropped, "
              f"Neutral render (XYZ only)", flush=True)
    except Exception:
        pass


def _warn_class_mismatch(site: str, xyz_count: int, class_shape) -> None:
    _warn_optional_mismatch("classification", site, xyz_count, class_shape)


def normalize_point_cloud_arrays(xyz, rgb=None, classification=None,
                                intensity=None):
    """Flatten scalar attributes to the native point-cloud contract.

    The native DLL expects `classification` / `intensity` as 1D arrays matching
    `xyz.shape[0]`; some loaders produce a single-column `(N, 1)` array that is
    semantically identical but not accepted by the stricter C API contract.
    """
    if xyz is None or np.asarray(xyz).ndim != 2 or np.asarray(xyz).shape[1] != 3:
        raise ValueError("xyz must be (N,3)")
    xyz = np.ascontiguousarray(np.asarray(xyz), dtype=np.float64)
    count = int(xyz.shape[0])

    if rgb is not None:
        rgb = np.asarray(rgb)
        if rgb.ndim == 2 and rgb.shape == (count, 1):
            rgb = rgb[:, 0]
        if rgb.ndim == 1 and rgb.size == count * 3:
            rgb = rgb.reshape(count, 3)
        rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
        if rgb.shape != (count, 3):
            raise ValueError("rgb must be (N,3) uint8 matching xyz count")

    if classification is not None:
        classification = np.asarray(classification)
        if classification.ndim == 2 and classification.shape[1] == 1 and classification.shape[0] == count:
            classification = classification[:, 0]
        elif classification.ndim == 2 and classification.shape == (1, count):
            classification = classification[0]
        else:
            classification = np.ravel(classification)
        classification = np.ascontiguousarray(classification, dtype=np.uint8)
        if classification.shape != (count,):
            # PART 27: a partially assembled FULL_RESIDENT block can carry a
            # CLASS array of a different length than its XYZ (the overview and
            # the per-block ranges are submitted independently). Raising here
            # aborted the ENTIRE frame tick. CLASS is optional - Neutral mode
            # needs XYZ only - so drop the mismatched array and carry on rather
            # than submitting a mismatched XYZ+CLASS pair or failing the frame.
            _warn_class_mismatch("prepare", count, classification.shape)
            classification = None

    if intensity is not None:
        intensity = np.asarray(intensity)
        if intensity.ndim == 2 and intensity.shape[1] == 1 and intensity.shape[0] == count:
            intensity = intensity[:, 0]
        elif intensity.ndim == 2 and intensity.shape == (1, count):
            intensity = intensity[0]
        else:
            intensity = np.ravel(intensity)
        intensity = np.ascontiguousarray(intensity, dtype=np.float32)
        if intensity.shape != (count,):
            # PART 27: same contract as CLASS - intensity is optional and a
            # partial block must not abort the frame tick.
            _warn_optional_mismatch("intensity", "prepare", count, intensity.shape)
            intensity = None

    return {
        "xyz": xyz,
        "rgb": rgb,
        "classification": classification,
        "intensity": intensity,
    }


class RenderBackend(ABC):
    """Minimal, Phase-1-sized rendering surface.

    This is intentionally small. It covers only what Part 1-4 of the native
    Vulkan slice actually implements today (window lifecycle, a resident
    point cloud upload, camera updates, and a way to force/observe a frame).
    It does NOT cover selection, classification/RGB/intensity/elevation
    shading, surfaces, or picking - those are later phases and are out of
    scope here (see the phase status doc / NAKSHA VULKAN STATUS report).
    """

    @abstractmethod
    def initialize(self, native_window_handle: Any, width: int, height: int) -> bool:
        """Create/attach the renderer to a native window surface.

        native_window_handle: an HWND (or platform equivalent) for the
        Vulkan backend, or an existing VTK render window/interactor for the
        VTK backend. The two backends interpret this argument differently by
        design - callers must know which backend they constructed.
        """
        raise NotImplementedError

    @abstractmethod
    def shutdown(self) -> None:
        """Release all GPU/native resources. Must be safe to call more than
        once and safe to call on a backend that failed initialize()."""
        raise NotImplementedError

    @abstractmethod
    def resize(self, width: int, height: int) -> None:
        """Notify the backend of a surface/window size change."""
        raise NotImplementedError

    @abstractmethod
    def set_test_points(self, xyz: np.ndarray, rgb: Optional[np.ndarray] = None) -> None:
        """Upload a point cloud for display.

        Phase-1 contract (see native/naksha_vulkan PointCloudRenderer):
        this uploads ONCE to a persistent GPU-resident buffer. Camera-only
        interaction must never trigger a re-upload. `xyz` is world-space
        float64, shape (N, 3), C-contiguous - matching app.data["xyz"]'s
        on-disk contract (see Part 6 data-contract findings). `rgb`, if
        given, is uint8, shape (N, 3).
        """
        raise NotImplementedError

    @abstractmethod
    def set_camera(self, view_matrix: np.ndarray, proj_matrix: np.ndarray) -> None:
        """Update the camera. Must be a small uniform/push-constant update,
        never a re-upload of point data (see set_test_points)."""
        raise NotImplementedError

    @abstractmethod
    def request_render(self) -> None:
        """Ask the backend to draw a frame. For VTK this is a thin call
        into the existing render-window Render() path; for Vulkan this
        drives BeginFrame/Record/EndFrame."""
        raise NotImplementedError

    @abstractmethod
    def capture_frame(self) -> Optional[np.ndarray]:
        """Return the last rendered frame as an (H, W, 3 or 4) uint8 array,
        or None if the backend cannot currently produce one. Used for
        headless verification/screenshots, not for interactive display."""
        raise NotImplementedError


class VtkRenderBackend(RenderBackend):
    """Thin call-through wrapper around the EXISTING production VTK path.

    This class does not reimplement any VTK rendering logic. Every method
    below delegates to functions that already exist in
    gui/unified_actor_manager.py (and, for the raw Render() call, the VTK
    widget's own render window) so that wrapping the app in this interface
    is provably behavior-preserving: if this class is ever used in place of
    direct VTK calls, the app's actual visual output is unchanged.

    This class is NOT imported by app_window.py or any startup path (Part 5
    requirement) - it exists only so a future phase can experiment with the
    RenderBackend interface without touching production code. Constructing
    it requires an already-initialized `app` object (the same `app` that
    unified_actor_manager.py's functions take everywhere) and a VTK widget,
    both owned and created by the existing production code paths.
    """

    def __init__(self, app: Any, vtk_widget: Any, slot_idx: int = 0):
        self._app = app
        self._vtk_widget = vtk_widget
        self._slot_idx = slot_idx

    def initialize(self, native_window_handle: Any, width: int, height: int) -> bool:
        # The VTK widget/render window is created and owned by app_window.py
        # long before this wrapper exists; there is nothing additional to
        # initialize here. native_window_handle/width/height are accepted
        # for interface parity with VulkanRenderBackend but unused - VTK's
        # own widget resize path (see resize() below) is authoritative.
        return self._vtk_widget is not None

    def shutdown(self) -> None:
        # Ownership of the VTK widget/render window belongs to app_window.py
        # for the lifetime of the application; this backend never destroys
        # it. Nothing to release here.
        pass

    def resize(self, width: int, height: int) -> None:
        # Delegates to VTK's normal Qt-driven resize handling; the render
        # window already resizes itself when its owning Qt widget resizes,
        # so this call-through simply forces a render to reflect it, using
        # the same call app_window.py uses elsewhere
        # (`self.vtk_widget.GetRenderWindow().Render()`).
        render_window = self._vtk_widget.GetRenderWindow()
        if render_window is not None:
            render_window.Render()

    def set_test_points(self, xyz: np.ndarray, rgb: Optional[np.ndarray] = None) -> None:
        # Delegates to the existing production actor-build path. This is a
        # call-through only: build_unified_actor() is the real function
        # unified_actor_manager.py uses to turn app.data arrays into a VTK
        # actor. We do not duplicate its LOD/border/palette logic here.
        from gui import unified_actor_manager as _uam

        _uam.build_unified_actor(self._app, self._vtk_widget)

    def set_camera(self, view_matrix: np.ndarray, proj_matrix: np.ndarray) -> None:
        # VTK owns its own camera (vtkCamera) driven by the interactor, not
        # by explicit view/proj matrices from the caller. The closest
        # existing call-through is the production camera-uniform refresh
        # used after any camera change (see
        # unified_actor_manager.refresh_widget_camera_uniforms), which pushes
        # the VTK camera's current state into shader uniforms. It does not
        # accept external matrices - VTK's camera state is the source of
        # truth. view_matrix/proj_matrix are accepted for interface parity
        # with VulkanRenderBackend but are not consumed here.
        from gui import unified_actor_manager as _uam

        _uam.refresh_widget_camera_uniforms(self._vtk_widget)

    def request_render(self) -> None:
        # Exactly the call production code uses everywhere in app_window.py:
        # self.vtk_widget.GetRenderWindow().Render()
        render_window = self._vtk_widget.GetRenderWindow()
        if render_window is not None:
            render_window.Render()

    def capture_frame(self) -> Optional[np.ndarray]:
        # Not implemented in this seam. A real implementation would delegate
        # to vtkWindowToImageFilter against the existing render window - but
        # that is new call surface, not a call-through to an existing
        # production function, so per Part 5's "call-through only" rule it
        # is left unimplemented here rather than added speculatively.
        return None


import ctypes
import time
import weakref
import logging
import os

_log = logging.getLogger(__name__)

# One safe telemetry boundary for every hook on a render path. It never raises
# into a caller and its return value is deliberately ignored by business logic:
# telemetry records the renderer result, it does not decide it.
from gui.naksha_telemetry import safe_emit_telemetry  # noqa: E402

# Search order: env override, then the CMake build output tree next to this
# repo. Nothing here is imported/loaded at module import time - loading is
# deferred to VulkanRenderBackend.initialize() so importing this module
# (e.g. for VtkRenderBackend, or by app_window.py doing a backend-selector
# check) never has a side effect of touching the GPU or a native DLL.
_DLL_CANDIDATES = [
    os.environ.get("NAKSHA_VULKAN_DLL", ""),
    # build_msvc/ is the current native build tree (has nkv_set_camera_ortho
    # and the other newer camera/LUT/draw-call-counter exports the Python
    # side now calls) - preferred over the older build/ (MinGW) tree, which
    # is kept only as a fallback so a machine without the MSVC build still
    # loads something.
    os.path.join(os.path.dirname(__file__), "..", "native", "naksha_vulkan", "build_msvc", "naksha_vulkan.dll"),
    os.path.join(os.path.dirname(__file__), "..", "native", "naksha_vulkan", "build", "naksha_vulkan.dll"),
]


def _find_dll_path() -> Optional[str]:
    for cand in _DLL_CANDIDATES:
        if cand and os.path.isfile(cand):
            return os.path.abspath(cand)
    return None


# ---------------------------------------------------------------------------
# NAKSHA_VULKAN_PREVIEW split-view mode + frame presentation counter.
#
# VULKAN_PRESENT_COUNT only ever increases when nkv_render() actually
# returned success - it is the honest "a real frame was presented through
# Vulkan" signal that the status bar's strict "ACTIVE (Preview)" wording is
# gated on (see AppRenderBackendOwner.status_label_text below).
# ---------------------------------------------------------------------------
VULKAN_PRESENT_COUNT = 0


def get_present_count() -> int:
    """Frames successfully presented through the Vulkan backend so far."""
    return VULKAN_PRESENT_COUNT


def _bump_present_count() -> int:
    global VULKAN_PRESENT_COUNT
    VULKAN_PRESENT_COUNT += 1
    return VULKAN_PRESENT_COUNT


def camera_debug_enabled() -> bool:
    """True when NAKSHA_VULKAN_CAMERA_DEBUG=1.

    Gates the per-camera-update diagnostic dump (see
    AppRenderBackendOwner.describe_camera). Off by default because it walks a
    point sample to compute the depth/clip analysis - a debugging aid, not
    something to run on every frame of normal use.
    """
    return os.environ.get("NAKSHA_VULKAN_CAMERA_DEBUG", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def color_parity_mode() -> int:
    """Colour-space byte parity with the VTK viewport (see the C header's
    nkv_set_color_parity_params contract).

    1 (default) = the native shaders pre-compensate the swapchain's sRGB encode,
    so the framebuffer ends up holding exactly the byte gui/shading_display.py
    computed - which is what makes switching between the VTK and Vulkan viewports
    visually indistinguishable. The swapchain is VK_FORMAT_B8G8R8A8_SRGB (see
    Renderer::ChooseSurfaceFormat), so without this step VTK's 0.5 renders as
    0.735. 0 restores the previous raw linear write (diagnostics/rollback only:
    NAKSHA_VULKAN_COLOR_PARITY=0).
    """
    import os
    flag = os.environ.get("NAKSHA_VULKAN_COLOR_PARITY", "1").strip().lower()
    return 0 if flag in ("0", "false", "off", "no") else 1


def shading_debug_stage() -> int:
    """Isolate one stage of the crisp Shaded Class pipeline on screen:
    0 = final shaded colour (default), 1 = face normal, 2 = lighting factor,
    3 = class colour only, 4 = raw N.L before the crisp remap.
    Set with NAKSHA_SHADING_DEBUG_STAGE=<0..4> or VulkanRenderBackend.
    set_crisp_debug_mode() at runtime."""
    import os
    try:
        return max(0, min(4, int(os.environ.get("NAKSHA_SHADING_DEBUG_STAGE", "0"))))
    except (TypeError, ValueError):
        return 0


def shaded_debug_view() -> int:
    """NAKSHA_SHADED_DEBUG_VIEW -> the instant.frag lighting debug stage.

    base     3  class colour only, lighting disabled. Compared against the
                 normal Classification view at the SAME camera this isolates
                 attribute/pipeline mapping from lighting: identical class
                 distribution = mapping is sound.
    normal   1  the depth-reconstructed normal as n*0.5+0.5. Coherent colour
                 over a planar roof/road means the reconstruction is sound;
                 per-pixel RGB noise means it is not.
    lighting 2  grayscale Lambert only - which surfaces are actually lit.
    final    0  class colour x lighting (the shipped result).

    Falls back to NAKSHA_SHADING_DEBUG_STAGE when the new name is unset, so the
    legacy switch keeps working.
    """
    raw = str(os.environ.get("NAKSHA_SHADED_DEBUG_VIEW", "")).strip().lower()
    if raw:
        return {"base": 3, "class": 3, "normal": 1, "lighting": 2,
                "final": 0}.get(raw, 0)
    return shading_debug_stage()


def shaded_splat_px_override() -> float:
    """NAKSHA_SHADED_SPLAT_PX pins the splat diameter in pixels for the
    coverage sweep (1.0 / 1.5 / 2.0 / 2.5 / 3.0). Unset = derive it from the
    projected point spacing instead of a fixed arbitrary world footprint."""
    try:
        raw = str(os.environ.get("NAKSHA_SHADED_SPLAT_PX", "")).strip()
        if not raw:
            return 0.0
        return max(1.0, min(64.0, float(raw)))
    except (TypeError, ValueError):
        return 0.0


def shaded_normal_radius_px() -> int:
    """Reconstruction neighbourhood radius, in texels.

    A 1-texel difference on a 1-2 px splat depth surface is dominated by which
    point happened to win the depth test in each pixel, so it returns noise.
    Sampling a slightly wider neighbourhood averages across the same surface.
    """
    try:
        raw = str(os.environ.get("NAKSHA_SHADED_NORMAL_RADIUS_PX", "")).strip()
        if not raw:
            return 2
        return max(1, min(8, int(float(raw))))
    except (TypeError, ValueError):
        return 2


def shaded_normal_source() -> str:
    """NAKSHA_SHADED_NORMAL_SOURCE: 'stored' (production) or 'screen' (DEV).

    'stored' reads the oct16x2 normal stream. 'screen' reconstructs normals from
    the depth the splat pass just wrote - kept as a DEV fallback and for A/B
    comparison only; it is not the production source.

    IMPORTANT: 'stored' is only correct when a normal stream is actually
    resident. With no normals uploaded the zero-filled stand-in decodes to +Z, so
    every surface shades as flat ground. _resolve_normal_source() therefore
    falls back to 'screen' in that case rather than shipping a flat render.
    """
    raw = str(os.environ.get("NAKSHA_SHADED_NORMAL_SOURCE", "stored")).strip().lower()
    return "screen" if raw in ("screen", "screenspace", "screen_space", "1") \
        else "stored"


def preview_enabled() -> bool:
    """True only for the LEGACY side-by-side split preview
    (NAKSHA_VULKAN_PREVIEW=1, VTK left / Vulkan right). The smoke/parity
    harness (vulkan_preview_smoke.py) still drives this path directly; the
    app itself only takes it when the env var is explicitly truthy."""
    return os.environ.get("NAKSHA_VULKAN_PREVIEW", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _safe_actor_vis(actor) -> bool:
    """actor.GetVisibility() that cannot raise.

    A VTK actor can be mid-rebuild or garbage-collected while a report is being
    produced; the reports must degrade to "unknown" rather than take the app
    down, because they are diagnostics, not control flow.
    """
    try:
        return bool(actor.GetVisibility())
    except Exception:
        return False


def vulkan_viewport_enabled() -> bool:
    """False by DEFAULT (restored - see the change note below). True only
    when NAKSHA_VULKAN_MAIN_VIEWPORT is explicitly set to a truthy value.

    CHANGE NOTE (this round): a prior session flipped this to default-True
    (installing Vulkan as the main viewport, hiding VTK and every VTK-drawn
    overlay - SNT/digitizer/grid/measurements/text/vectors - underneath it)
    without ever getting that reviewed/approved - it was an unintended side
    effect of a session that got cut off before reporting back, not an
    approved decision. Restored to the last state the user actually
    approved: VTK stays the visible main viewport by default, overlays
    intact. Vulkan point/surface/shaded-class processing still runs for
    real underneath (see AppRenderBackendOwner's existing TEST/HEADLESS
    state - the workload is real, it is simply not what is on screen)
    unless explicitly opted into one of:
      NAKSHA_VULKAN_PREVIEW=1|true|yes|on       -> legacy side-by-side split
                                                    preview (preview_enabled());
                                                    VTK keeps ALL its overlays,
                                                    Vulkan gets its own pane.
      NAKSHA_VULKAN_MAIN_VIEWPORT=1|true|yes|on -> full takeover as THE main
                                                    viewport (this function).
                                                    Deliberately a SEPARATE
                                                    env var from the preview
                                                    flag above so the two
                                                    behaviors can never be
                                                    triggered by the same
                                                    value/typo.
      unset / anything else                     -> False: VTK-only, exactly
                                                    like a build with no
                                                    Vulkan integration at all.

    Current behaviour (see install_as_main_viewport): the Vulkan widget is an
    opaque native HWND raised ABOVE the VTK widget's slot, not a replacement
    for it - the VTK widget is deliberately left visible and rendering
    underneath (hiding it would freeze its Qt layout geometry at the
    bootstrap size). Only the VTK LiDAR actors (point cloud / surface /
    shaded-class mesh) are switched off via set_vtk_lidar_rendering(False);
    every overlay actor (SNT/digitizer/grid/measurements/text/vectors) keeps
    being drawn by VTK's normal render loop and is composited underneath the
    opaque Vulkan surface only where the point cloud itself would have been.
    """
    raw = os.environ.get("NAKSHA_VULKAN_MAIN_VIEWPORT", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def main_vtk_camera_disabled() -> bool:
    """NAKSHA_DISABLE_MAIN_VTK_CAMERA=1: no VTK -> MainCamera2D/Vulkan adoption for
    the main view (cross-section VTK cameras are unaffected)."""
    return os.environ.get("NAKSHA_DISABLE_MAIN_VTK_CAMERA", "").strip().lower() in (
        "1", "true", "yes", "on")


def vtk_lidar_render_disable_requested() -> bool:
    """True when NAKSHA_VULKAN_DISABLE_VTK_LIDAR is explicitly truthy.

    Replaces the old NAKSHA_VULKAN_DISABLE_ALL_VTK_RENDER kill-switch, which
    stopped every VTK/OpenGL draw (main render loop, overlays, Qt UI) once
    Vulkan owned the viewport - that was the root cause of ribbons, menus,
    SNT, the digitizer, measurements, text and vector overlays all going
    dark/crashing. This flag is scoped to LiDAR actors only: it toggles the
    same set_vtk_lidar_rendering() switch install_as_main_viewport() already
    applies automatically, so it exists purely as an explicit, independently
    settable diagnostic - it must never be wired to anything broader than
    that actor list.
    """
    return os.environ.get("NAKSHA_VULKAN_DISABLE_VTK_LIDAR", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


# ---------------------------------------------------------------------------
# Display-mode bridge + GPU colour tables (read by point.vert).
# ---------------------------------------------------------------------------
# Mirrors NkvDisplayMode in include/naksha/naksha_vulkan_c_api.h.
# Neutral is intentionally a distinct native mode so the UI can say gray while
# the backend is explicitly on the neutral path rather than silently falling
# through to RGB.
NKV_MODE_RGB = 0
NKV_MODE_CLASSIFICATION = 1
NKV_MODE_INTENSITY = 2
NKV_MODE_ELEVATION = 3

# Instant Shaded Class (native, DEFAULT). Distinct from
# NKV_MODE_CLASSIFICATION on purpose: mode 4 does not just colour points, it
# routes the frame through the two-pass renderer (splats into a private
# colour+depth target, then a fullscreen pass that reconstructs normals from
# that depth and lights it). Selecting it is a push-constant write - no mesh,
# no Delaunay, no re-upload. Mirrors NKV_DISPLAY_SHADED_CLASS_INSTANT.
NKV_MODE_SHADED_CLASS_INSTANT = 4
NKV_MODE_NEUTRAL = 5
NKV_MODE_DEPTH = 6

# gui/pointcloud_display.py `app.display_mode` -> NkvDisplayMode. Modes with
# no native shading path (surface, line, overlay, section, ...) map to None:
# sync_point_shading() then bakes the exact VTK colours on the CPU and presents
# them through NKV_MODE_RGB rather than approximating a ramp in GLSL.
#
# "depth" used to be one of those None cases, which forced a CPU RGB bake and,
# on the streaming path, no depth at all - the streaming adapter reported
# "[DISPLAY MODE ERROR] unsupported mode='depth'". Depth is now a genuine
# shader-computed mode, so it gets a real enum value instead.
_APP_MODE_TO_NKV = {
    "neutral": NKV_MODE_NEUTRAL,
    "gray": NKV_MODE_NEUTRAL,
    "grey": NKV_MODE_NEUTRAL,
    "rgb": NKV_MODE_RGB,
    "class": NKV_MODE_CLASSIFICATION,
    "shaded_class": NKV_MODE_CLASSIFICATION,
    "intensity": NKV_MODE_INTENSITY,
    "elevation": NKV_MODE_ELEVATION,
    "depth": NKV_MODE_DEPTH,
}

# Selecting the LEGACY renderer is an explicit opt-out, never a default.
#   NAKSHA_SHADED_RENDERER=legacy   -> Delaunay/TIN path (the old behaviour)
#   anything else (or unset)        -> instant
_LEGACY_SHADED_VALUES = ("legacy", "tin", "mesh", "delaunay", "0", "off", "false", "no")


def _record_render_mode(app, mode, nkv_mode, instant):
    """DEV instrumentation: one render_mode_switch line, plus class_lut_update
    when the class palette / visibility table is part of the switch.

    Telemetry ONLY - no return value is consumed by any renderer logic, and the
    whole body is guarded so it can never affect the shading sync.
    """
    try:
        from gui import instant_shaded_telemetry as ist
        recorder = ist.get()
        if recorder is None:
            return
        previous = recorder.swap_render_mode(mode)
        ist.emit('render_mode_switch', renderer='naksha_vulkan',
                 from_mode=previous, to_mode=mode,
                 nkv_mode=int(nkv_mode), instant=bool(instant))
        if instant:
            # 256 * 4 B class palette tail + 256 * 1 B visibility table.
            ist.emit('class_lut_update', renderer='naksha_vulkan',
                     to_mode=mode, lut_upload_bytes=1280, xyz_upload_delta=0,
                     visibility_bytes=256)
    except Exception:
        return


def instant_shaded_renderer_enabled() -> bool:
    """True when Display Mode -> Shaded Class should use the native Instant
    Shaded Class renderer rather than the Delaunay/TIN path.

    Default TRUE - the instant renderer is the shipped behaviour. Set
    NAKSHA_SHADED_RENDERER=legacy to get the old mesh path back (the DEV-only
    fallback), which is an explicit, measured opt-out.
    """
    try:
        choice = str(os.environ.get("NAKSHA_SHADED_RENDERER", "instant")).strip().lower()
    except Exception:
        return True
    return choice not in _LEGACY_SHADED_VALUES


def instant_splat_footprint(app) -> float:
    """World-space splat footprint in METRES for the instant renderer.

    point.vert sizes a mode-4 sprite as
        clamp(footprint * pxPerMetre / view_depth, 1, 3)
    so footprint should be ~1.5x the mean point spacing: the splat then covers
    about 1.5x the projected spacing at ANY zoom (closed surface when you are
    zoomed out, no ballooning when zoomed in), bounded by the 1..3 px band.

    Derived once from the data bounds and cached on the app - recomputing it
    per switch would read both coordinate columns of a 27M-point cloud every
    time the user clicks a display mode.
    """
    cached = getattr(app, "_naksha_instant_footprint_m", None)
    if cached:
        return float(cached)
    footprint = 0.15
    try:
        data = getattr(app, "data", None)
        xyz = data.get("xyz") if isinstance(data, dict) else None
        if xyz is not None and len(xyz) > 0:
            t0 = time.perf_counter()
            arr = np.asarray(xyz)
            dx = float(np.ptp(arr[:, 0]))
            dy = float(np.ptp(arr[:, 1]))
            n = max(int(arr.shape[0]), 1)
            spacing = float(np.sqrt(max(dx * dy, 0.0) / n))
            footprint = float(np.clip(spacing * 1.5, 1e-4, 50.0))
            # Informational record, emitted through the safe boundary: a print
            # failure here must never fail the caller, because this value feeds
            # the renderer's splat band rather than the log.
            safe_emit_telemetry('INSTANT SHADED SPLAT', {
                'footprint_m': round(footprint, 4), 'bbox_m': f'{dx:.1f}x{dy:.1f}',
                'points': n, 'spacing_m': round(spacing, 4),
                'source': 'in-memory', 'derive_ms':
                    round((time.perf_counter() - t0) * 1000.0, 1),
            })
        else:
            # STREAMING: app.data is deliberately EMPTY, but the bounds and the
            # total point count are metadata the cache DOES have, so the exact
            # same sqrt(area / n) estimate applies - without materialising a
            # 27M-point array just to size a splat.
            bounds = getattr(app, "data_bounds", None)
            total = int(getattr(app, "total_points", 0) or 0)
            if bounds and total > 0:
                (bmin, bmax) = bounds
                dx = abs(float(bmax[0]) - float(bmin[0]))
                dy = abs(float(bmax[1]) - float(bmin[1]))
                spacing = float(np.sqrt(max(dx * dy, 0.0) / total))
                footprint = float(np.clip(spacing * 1.5, 1e-4, 50.0))
                safe_emit_telemetry('INSTANT SHADED SPLAT', {
                    'footprint_m': round(footprint, 4),
                    'bbox_m': f'{dx:.1f}x{dy:.1f}', 'points': total,
                    'spacing_m': round(spacing, 4), 'source': 'cache-metadata',
                })
    except Exception:
        _log.exception("instant_splat_footprint failed; using 0.15 m default")
        footprint = 0.15
    try:
        app._naksha_instant_footprint_m = footprint
    except Exception:
        pass
    return footprint

def push_instant_splat_band(backend, app) -> bool:
    """Applies the Instant Shaded splat diameter band.

    Default: 1..3 px, sized from a world footprint projected to pixels at each
    point's depth (point.vert), so coverage closes as you zoom in and stops the
    sprites ballooning as you zoom out.

    NAKSHA_SHADED_SPLAT_PX pins a single diameter instead (the coverage sweep).
    min == max is exactly the "fixed pixel" mode point.vert already supports,
    and the footprint is neutralised so the pinned value is what is drawn.
    """
    px = shaded_splat_px_override()
    if px > 0:
        return bool(backend.set_point_size_params(0.0001, px, px, 0.0))
    return bool(backend.set_point_size_params(
        instant_splat_footprint(app), 1.0, 3.0, 0.0))


def build_class_visibility(app) -> np.ndarray:
    """(256,) uint8, 1 = that class is drawn. This is the visibility half of
    the class/visibility LUT: a Display Mode checkbox change becomes a
    256-BYTE uniform push, never a point re-upload."""
    vis = np.full(256, 1, dtype=np.uint8)
    palette = getattr(app, "class_palette", None) or {}
    for code, entry in palette.items():
        try:
            idx = int(code) & 255
            show = bool(entry.get("show", True)) if isinstance(entry, dict) else True
            vis[idx] = 1 if show else 0
        except Exception:
            continue
    return np.ascontiguousarray(vis)


def _pcd():
    """gui.pointcloud_display, imported lazily (it pulls in the VTK/PyVista
    stack) and optional - the LUT builders fall back to neutral tables."""
    try:
        from gui import pointcloud_display as pcd  # lazy on purpose
        return pcd
    except Exception:
        _log.exception("pointcloud_display unavailable; using neutral LUTs")
        return None


def _to_u8_rgb(arr, fallback=(128, 128, 128)) -> "np.ndarray":
    """(256,3) uint8 from whatever the ramp helpers return (uint8, float 0..1
    or float 0..255), padded/truncated to exactly 256 entries."""
    out = np.full((256, 3), fallback, dtype=np.uint8)
    try:
        rgb = np.asarray(arr, dtype=np.float64).reshape(-1, 3)
        if rgb.shape[0] < 256:
            rgb = np.vstack([rgb, np.repeat(rgb[-1:], 256 - rgb.shape[0], axis=0)])
        rgb = rgb[:256]
        if float(np.nanmax(rgb)) <= 1.0 + 1e-6:
            rgb = rgb * 255.0
        out = np.clip(np.nan_to_num(rgb), 0, 255).astype(np.uint8)
    except Exception:
        pass
    return np.ascontiguousarray(out)


def _fallback_rainbow(t) -> "np.ndarray":
    """Blue -> cyan -> green -> yellow -> red: the same piecewise ramp
    gui/pointcloud_display._nakshatech_rainbow_5color paints, used only when
    that module cannot be imported."""
    t = np.clip(np.asarray(t, dtype=np.float64), 0.0, 1.0)
    r = np.where(t <= 0.25, 0.0,
                 np.where(t <= 0.5, 0.0,
                          np.where(t <= 0.75, (t - 0.5) / 0.25 * 255.0, 255.0)))
    g = np.where(t <= 0.25, np.clip(t / 0.25, 0.0, 1.0) * 255.0,
                 np.where(t <= 0.5, 255.0,
                          np.where(t <= 0.75, 255.0,
                                   np.clip(1.0 - (t - 0.75) / 0.25, 0.0, 1.0) * 255.0)))
    b = np.where(t <= 0.25, 255.0,
                 np.where(t <= 0.5, np.clip(1.0 - (t - 0.25) / 0.25, 0.0, 1.0) * 255.0,
                          0.0))
    return np.stack([r, g, b], axis=1)


def build_point_luts(app=None) -> tuple:
    """(class_lut, elevation_lut, intensity_lut), each a C-contiguous (256,3)
    uint8 table for nkv_set_point_luts().

    The tables are derived from the SAME helpers gui/pointcloud_display feeds
    VTK (app.class_palette entries with class_weight, the Nakshatech rainbow /
    custom elevation ramp, the grayscale intensity ramp), so a GPU-shaded
    point resolves to the byte-identical colour the VTK viewport paints for
    it - the matching normalisation ranges come from shading_uniforms()."""
    t = np.linspace(0.0, 1.0, 256)
    pcd = _pcd()

    # classification palette, exactly as compute_colors() reads app.class_palette
    class_lut = np.full((256, 3), 128, dtype=np.uint8)
    palette = getattr(app, "class_palette", None) or {}
    weight = float(getattr(app, "class_weight", 1.0) or 1.0)
    for code, entry in palette.items():
        try:
            idx = int(code) & 255
            if isinstance(entry, dict):
                color, show = entry.get("color", (128, 128, 128)), bool(entry.get("show", True))
            else:
                color, show = entry, True
            if not show:
                class_lut[idx] = (0, 0, 0)
                continue
            rgb = np.asarray(color, dtype=np.float64).reshape(3) * weight
            class_lut[idx] = np.clip(rgb, 0, 255).astype(np.uint8)
        except Exception:
            continue
    class_lut = np.ascontiguousarray(class_lut)

    # elevation ramp - the same ramp call compute_colors() makes
    elev_lut = None
    if pcd is not None:
        try:
            ramp = getattr(app, "elevation_color_ramp", None)
            elev_lut = (pcd._apply_custom_color_ramp(t, ramp) if ramp and len(ramp) >= 2
                        else pcd._nakshatech_rainbow_5color(t))
        except Exception:
            _log.exception("elevation LUT sampling failed; using built-in ramp")
            elev_lut = None
    elev_lut = _to_u8_rgb(elev_lut) if elev_lut is not None else _to_u8_rgb(_fallback_rainbow(t))

    # intensity ramp: grayscale. The Nakshatech gamma/contrast is applied by
    # point.vert BEFORE the lookup (pow(t, gamma)), which is exactly how
    # _nakshatech_intensity_rgb bakes it: gray = int(pow(t, gamma) * 255).
    grey = np.arange(256, dtype=np.uint8)
    int_lut = np.ascontiguousarray(np.stack([grey, grey, grey], axis=1))

    return class_lut, elev_lut, int_lut


# Hard ceiling on gl_PointSize, mirroring min(deviceMax, 64.0) in point.vert.
# The device's advertised max (e.g. 2047.9) is a HARDWARE CAPABILITY, not a
# rendering target - VTK's dots are max(1.0, weight_lut[c]) pixels and never
# approach it, so Vulkan must not either.
_POINTSIZE_HW_CAP = 64.0


def shading_uniforms(app=None) -> dict:
    """Normalisation ranges + sizing/sprite parameters for the point shader,
    mirroring what gui/pointcloud_display.compute_colors() feeds VTK:
    elevation uses the elevation_clip_low/high percentiles of z with NO gamma;
    intensity uses the intensity_clip_low/high percentiles of the NON-ZERO
    intensity values plus intensity_gamma (intensity_contrast defaults to
    1.0 = VTK has no contrast stage).

    Point size is emitted as a FIXED PIXEL SIZE (min_px == max_px == the app's
    own point_size), which is exactly what VTK does: _BASE_POINT_SIZE = 2.5 and
    gl_PointSize = max(1.0, weight_lut[c]). There is deliberately no world
    footprint and no zoom/depth term - VTK's dot size does not change when you
    zoom, and neither should Vulkan's.
    """
    out = {
        "elev_lo": 0.0, "elev_hi": 1.0, "elev_gamma": 1.0,
        "int_lo": 0.0, "int_hi": 1.0, "int_contrast": 1.0, "int_gamma": 1.35,
        "footprint_m": 2.5, "min_px": 2.5, "max_px": 2.5,
        "class_mix": 0.0, "softness": 0.0, "brightness": 1.0,
    }
    data = getattr(app, "data", None) or {}
    pcd = _pcd()

    xyz = data.get("xyz")
    if pcd is not None and xyz is not None and len(xyz):
        try:
            z = np.asarray(xyz[:, 2], dtype=np.float64)
            _n, lo, hi = pcd._nakshatech_auto_normalize(
                z,
                float(getattr(app, "elevation_clip_low", 1.0)),
                float(getattr(app, "elevation_clip_high", 99.0)),
                return_clip=True,
            )
            out["elev_lo"], out["elev_hi"] = float(lo), float(hi)
        except Exception:
            _log.exception("elevation range derivation failed; keeping 0..1")

    inten = data.get("intensity")
    if pcd is not None and inten is not None and len(inten):
        try:
            _n, lo, hi = pcd._nakshatech_auto_normalize(
                np.asarray(inten, dtype=np.float64),
                float(getattr(app, "intensity_clip_low", 0.5)),
                float(getattr(app, "intensity_clip_high", 99.8)),
                ignore_zero=True,   # _nakshatech_intensity_rgb drops zeros
                return_clip=True,
            )
            out["int_lo"], out["int_hi"] = float(lo), float(hi)
            out["int_gamma"] = float(getattr(app, "intensity_gamma", 1.35))
            out["int_contrast"] = float(getattr(app, "intensity_contrast", 1.0))
        except Exception:
            _log.exception("intensity range derivation failed; keeping 0..1")

    try:
        p = float(getattr(app, "point_size", 2.5) or 2.5)
    except Exception:
        p = 2.5
    size_px = max(1.0, min(p, 64.0))
    out["footprint_m"] = size_px       # kept for ABI shape; no longer depth-scaled
    out["min_px"] = size_px
    out["max_px"] = size_px
    out["class_mix"] = float(getattr(app, "vulkan_class_intensity_mix", 0.0))
    # VTK PARITY: softness = 0.0, NOT 0.4.
    # gui/unified_actor_manager.py's //VTK::Color::Impl writes `opacity = 1.0`
    # and only discards outside radius 0.5 - a hard-edged, fully opaque disc.
    # The previous 0.4 default faded the outer 40% of every sprite to zero and
    # alpha-blended it, so overlapping returns smeared into soft round balls.
    # 0.0 reproduces VTK exactly; a small value only anti-aliases the rim.
    out["softness"] = float(getattr(app, "vulkan_sprite_softness", 0.0))
    out["brightness"] = float(getattr(app, "vulkan_brightness", 1.0))
    return out


class VulkanRenderBackend(RenderBackend):
    """ctypes bridge to the native C ABI in native/naksha_vulkan.

    Implements the narrow extern "C" surface declared in
    include/naksha/naksha_vulkan_c_api.h (nkv_is_available,
    nkv_create_renderer, nkv_set_point_cloud, nkv_set_surface,
    nkv_set_display_mode, nkv_set_shading_parameters, nkv_set_camera_lookat,
    nkv_resize, nkv_render, nkv_clear, nkv_destroy_renderer). No Vulkan
    handle type crosses this boundary - only an opaque uint64 handle and
    raw numpy pointers.

    Known-incomplete pieces (see CHECKPOINTS.md / integration report, do not
    assume otherwise from this docstring alone):
      - set_display_mode/set_shading_parameters are accepted by the ABI but
        the fragment shaders do not yet branch on them (point.frag always
        reads the uploaded RGBA8 buffer; formulas (A)/(B) are not ported to
        GLSL). Surface/Shaded-Class colors are pre-baked on the CPU side and
        passed through (NKV_SHADE_PASSTHROUGH).
      - intensity-mode point coloring has no native buffer/shader path yet.
      - capture_frame() has no offscreen readback implemented natively.
    """

    def __init__(self):
        self._dll = None
        self._handle = 0
        self._available = None  # tri-state cache: None/True/False
        self.render_count = 0  # increments on every request_render() call
        self.present_count = 0  # successes only; mirrors VULKAN_PRESENT_COUNT
        self.skip_count = 0  # frames the engine deliberately skipped (minimized/out-of-date)
        self.fail_count = 0  # frames that reported an error
        self._last_extent = (0, 0)  # (w, h) of the live swapchain; sizes capture_frame()'s buffer
        # Last colour-space/stage parity state pushed to the engine, so
        # set_crisp_debug_mode() can change only the stage. Defaults mirror the
        # DLL's own defaults (byte parity on, final colour, shader-side floor).
        self._parity_color_mode = 1
        self._parity_debug_stage = 0
        self._parity_ambient_floor = -1.0
        self._parity_base_elevation_deg = 0.0

    # -- availability -----------------------------------------------------
    def is_available(self) -> bool:
        """Side-effect-checked-once probe: DLL loadable AND nkv_is_available()
        reports a usable Vulkan loader/device. Never raises."""
        if self._available is not None:
            return self._available
        try:
            dll_path = _find_dll_path()
            if not dll_path:
                _log.info("Vulkan backend: naksha_vulkan.dll not found, staying on VTK")
                self._available = False
                return False
            dll = ctypes.CDLL(dll_path)
            print(f"[VULKAN] DLL loaded: {dll_path}")
            _log.info("Vulkan backend: DLL loaded from %s", dll_path)
            dll.nkv_is_available.restype = ctypes.c_int
            ok = bool(dll.nkv_is_available())
            self._dll = dll
            self._available = ok
            if not ok:
                dll.nkv_last_error.restype = ctypes.c_char_p
                _log.info("Vulkan backend unavailable: %s", dll.nkv_last_error())
            return ok
        except Exception:
            _log.exception("Vulkan backend probe failed; staying on VTK")
            self._available = False
            return False

    def _bind_signatures(self) -> None:
        dll = self._dll
        dll.nkv_create_renderer.restype = ctypes.c_uint64
        dll.nkv_create_renderer.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int]
        dll.nkv_destroy_renderer.argtypes = [ctypes.c_uint64]
        dll.nkv_set_point_cloud.restype = ctypes.c_int
        dll.nkv_set_point_cloud.argtypes = [
            ctypes.c_uint64, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_uint64,
        ]
        dll.nkv_set_surface.restype = ctypes.c_int
        dll.nkv_set_surface.argtypes = [
            ctypes.c_uint64, ctypes.c_void_p, ctypes.c_uint64,
            ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p,
        ]
        # ---- Tiled surface / LOD / culling / atomic swap (Parts 6-11) ------
        # Bound defensively: an older DLL simply does not export these, and
        # the caller degrades to the untiled path instead of raising.
        for _name, _args in (
            ("nkv_set_surface_tiled", [
                ctypes.c_uint64,
                ctypes.c_void_p, ctypes.c_uint64,   # positions, vertex_count
                ctypes.c_void_p, ctypes.c_uint64,   # faces, face_count
                ctypes.c_void_p,                    # face_colors_rgb
                ctypes.c_uint32, ctypes.c_float,    # tile_count, base_cell_m
                ctypes.c_int,                       # is_preview
                ctypes.c_uint64, ctypes.c_uint64,    # dataset/surface revision
            ]),
            ("nkv_activate_pending_surface", [ctypes.c_uint64, ctypes.c_void_p]),
            ("nkv_set_streaming_surface_active", [ctypes.c_uint64, ctypes.c_int]),
            ("nkv_set_surface_indexed_blocks", [ctypes.c_uint64,
                ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_uint64,
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                ctypes.c_uint64, ctypes.c_uint64]),
            ("nkv_get_surface_total_tile_count", [ctypes.c_uint64]),
            ("nkv_get_surface_visible_tile_count", [ctypes.c_uint64]),
            ("nkv_get_surface_total_triangle_count", [ctypes.c_uint64]),
            ("nkv_get_surface_visible_triangle_count", [ctypes.c_uint64]),
            ("nkv_get_surface_lod", [ctypes.c_uint64, ctypes.c_void_p]),
            ("nkv_get_surface_lod_triangle_counts", [ctypes.c_uint64, ctypes.c_void_p]),
            ("nkv_set_surface_interacting", [ctypes.c_uint64, ctypes.c_int, ctypes.c_double]),
            ("nkv_set_surface_lod_error_pixels", [ctypes.c_uint64, ctypes.c_float]),
            ("nkv_get_surface_state", [ctypes.c_uint64]),
            ("nkv_get_surface_swap_count", [ctypes.c_uint64]),
            ("nkv_get_surface_stale_discard_count", [ctypes.c_uint64]),
            ("nkv_get_surface_memory", [
                ctypes.c_uint64, ctypes.c_void_p, ctypes.c_void_p,
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ]),
            ("nkv_get_surface_memory_rejected", [ctypes.c_uint64]),
            ("nkv_set_dataset_revision", [ctypes.c_uint64, ctypes.c_uint64]),
            ("nkv_set_surface_revision", [ctypes.c_uint64, ctypes.c_uint64]),
            ("nkv_get_dataset_revision", [ctypes.c_uint64]),
            ("nkv_get_surface_revision", [ctypes.c_uint64]),
        ):
            _fn = getattr(dll, _name, None)
            if _fn is not None:
                _fn.restype = ctypes.c_int if _name.startswith("nkv_set_") \
                    or _name.startswith("nkv_activate") else ctypes.c_uint64
                _fn.argtypes = _args
        dll.nkv_set_display_mode.restype = ctypes.c_int
        dll.nkv_set_display_mode.argtypes = [ctypes.c_uint64, ctypes.c_int]
        dll.nkv_set_shading_parameters.restype = ctypes.c_int
        dll.nkv_set_shading_parameters.argtypes = [
            ctypes.c_uint64, ctypes.c_int, ctypes.c_float, ctypes.c_float, ctypes.c_float,
        ]
        # Real crisp-hybrid Shaded Class path (see CHECKPOINTS.md).
        dll.nkv_set_shaded_class_surface.restype = ctypes.c_int
        dll.nkv_set_shaded_class_surface.argtypes = [
            ctypes.c_uint64,
            ctypes.c_void_p, ctypes.c_uint64,   # positions, vertex_count
            ctypes.c_void_p, ctypes.c_uint64,   # faces, face_count
            ctypes.c_void_p,                    # vertex_class_id
            ctypes.c_void_p,                    # class_color_lut (256*3)
            ctypes.c_void_p, ctypes.c_uint64,   # mixed_face_ids, mixed_face_count
        ]
        dll.nkv_set_crisp_shading_parameters.restype = ctypes.c_int
        dll.nkv_set_crisp_shading_parameters.argtypes = [
            ctypes.c_uint64, ctypes.c_float, ctypes.c_float, ctypes.c_float,
            ctypes.c_float, ctypes.c_float,
        ]
        # Instant Shaded Class (default renderer). Additive like every other
        # optional entry point here: an older DLL lacks them, getattr returns
        # None and the caller degrades to the legacy TIN path instead of
        # raising. argtypes are always set on the SAME object the caller
        # resolves, otherwise ctypes passes the default int promotion and a
        # float argument arrives as garbage.
        if hasattr(dll, "nkv_set_instant_shading_parameters"):
            dll.nkv_set_instant_shading_parameters.restype = ctypes.c_int
            dll.nkv_set_instant_shading_parameters.argtypes = [
                ctypes.c_uint64, ctypes.c_float, ctypes.c_float,
                ctypes.c_float, ctypes.c_int,
            ]
        if hasattr(dll, "nkv_set_instant_normal_radius"):
            dll.nkv_set_instant_normal_radius.restype = ctypes.c_int
            dll.nkv_set_instant_normal_radius.argtypes = [
                ctypes.c_uint64, ctypes.c_int]
        if hasattr(dll, "nkv_set_class_visibility"):
            dll.nkv_set_class_visibility.restype = ctypes.c_int
            dll.nkv_set_class_visibility.argtypes = [
                ctypes.c_uint64, ctypes.c_void_p]
        if hasattr(dll, "nkv_get_class_visibility_update_count"):
            dll.nkv_get_class_visibility_update_count.restype = ctypes.c_uint64
            dll.nkv_get_class_visibility_update_count.argtypes = [ctypes.c_uint64]
        if hasattr(dll, "nkv_get_instant_shaded_frame_count"):
            dll.nkv_get_instant_shaded_frame_count.restype = ctypes.c_uint64
            dll.nkv_get_instant_shaded_frame_count.argtypes = [ctypes.c_uint64]
        if hasattr(dll, "nkv_get_instant_shaded_available"):
            dll.nkv_get_instant_shaded_available.restype = ctypes.c_int
            dll.nkv_get_instant_shaded_available.argtypes = [ctypes.c_uint64]
        if hasattr(dll, "nkv_set_point_normals"):
            dll.nkv_set_point_normals.restype = ctypes.c_int
            dll.nkv_set_point_normals.argtypes = [
                ctypes.c_uint64, ctypes.c_void_p, ctypes.c_uint64]
        for _n in ("nkv_get_point_normal_count", "nkv_get_point_normal_bytes",
                   "nkv_get_point_normal_upload_count"):
            if hasattr(dll, _n):
                getattr(dll, _n).restype = ctypes.c_uint64
                getattr(dll, _n).argtypes = [ctypes.c_uint64]
        if hasattr(dll, "nkv_set_instant_normal_source"):
            dll.nkv_set_instant_normal_source.restype = ctypes.c_int
            dll.nkv_set_instant_normal_source.argtypes = [
                ctypes.c_uint64, ctypes.c_int]
        dll.nkv_get_surface_upload_count.restype = ctypes.c_uint64
        dll.nkv_get_surface_upload_count.argtypes = [ctypes.c_uint64]
        dll.nkv_get_shade_param_update_count.restype = ctypes.c_uint64
        dll.nkv_get_shade_param_update_count.argtypes = [ctypes.c_uint64]
        # GPU class-colour LUT (Phase C). A palette change must be a 1 KB upload
        # that leaves geometry, indices and the mesh-upload counter untouched.
        # Additive: an older DLL lacks these symbols, getattr returns None and
        # the caller degrades to the legacy full-mesh recolour.
        if hasattr(dll, "nkv_set_class_color_lut"):
            dll.nkv_set_class_color_lut.restype = ctypes.c_int
            dll.nkv_set_class_color_lut.argtypes = [
                ctypes.c_uint64, ctypes.c_void_p]
        if hasattr(dll, "nkv_get_class_lut_update_count"):
            dll.nkv_get_class_lut_update_count.restype = ctypes.c_uint64
            dll.nkv_get_class_lut_update_count.argtypes = [ctypes.c_uint64]
        dll.nkv_get_overlay_triangle_count.restype = ctypes.c_uint64
        dll.nkv_get_overlay_triangle_count.argtypes = [ctypes.c_uint64]
        dll.nkv_set_camera_lookat.restype = ctypes.c_int
        dll.nkv_set_camera_lookat.argtypes = [ctypes.c_uint64] + [ctypes.c_double] * 6 + [ctypes.c_double] * 3
        dll.nkv_set_render_origin.argtypes = [ctypes.c_uint64, ctypes.c_double, ctypes.c_double, ctypes.c_double]
        dll.nkv_resize.restype = ctypes.c_int
        dll.nkv_resize.argtypes = [ctypes.c_uint64, ctypes.c_uint32, ctypes.c_uint32]
        dll.nkv_render.restype = ctypes.c_int
        dll.nkv_render.argtypes = [ctypes.c_uint64]
        dll.nkv_clear.argtypes = [ctypes.c_uint64]
        dll.nkv_last_frame_ms.restype = ctypes.c_double
        dll.nkv_last_frame_ms.argtypes = [ctypes.c_uint64]
        dll.nkv_last_error.restype = ctypes.c_char_p
        dll.nkv_get_device_name.restype = ctypes.c_char_p
        dll.nkv_get_device_name.argtypes = [ctypes.c_uint64]
        dll.nkv_get_api_version.restype = ctypes.c_uint32
        dll.nkv_get_api_version.argtypes = [ctypes.c_uint64]
        dll.nkv_get_vram_bytes.restype = ctypes.c_uint64
        dll.nkv_get_vram_bytes.argtypes = [ctypes.c_uint64]
        # Phase 3: the SAFE budget, derived from the same device with one shared
        # 70% rule. Bound defensively - an older DLL reports "unknown" (0) and
        # the planner falls back rather than raising at call time.
        if hasattr(dll, "nkv_get_device_vram_budget"):
            dll.nkv_get_device_vram_budget.restype = ctypes.c_uint64
            dll.nkv_get_device_vram_budget.argtypes = [ctypes.c_uint64]
        try:
            dll.nkv_get_frame_timing.restype = ctypes.c_int
            dll.nkv_get_frame_timing.argtypes = [
                ctypes.c_uint64, *[ctypes.POINTER(ctypes.c_double)] * 5,
                ctypes.POINTER(ctypes.c_int)]
        except Exception:
            pass  # older DLL without GPU timing
        try:
            dll.nkv_get_gpu_frame_time_ms.restype = ctypes.c_double
            dll.nkv_get_gpu_frame_time_ms.argtypes = [ctypes.c_uint64]
        except Exception:
            pass  # older DLL without the single-value GPU accessor
        # Screen-space LOD (opt-in). Older DLLs simply lack these.
        for _fn, _restype, _argtypes in (
            ("nkv_set_point_draw_ranges", ctypes.c_int,
             [ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint32),
              ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32]),
            ("nkv_clear_point_draw_ranges", ctypes.c_int, [ctypes.c_uint64]),
            ("nkv_get_point_draw_range_count", ctypes.c_uint32, [ctypes.c_uint64]),
            ("nkv_get_point_draw_range_points", ctypes.c_uint32, [ctypes.c_uint64]),
        ):
            try:
                getattr(dll, _fn).restype = _restype
                getattr(dll, _fn).argtypes = _argtypes
            except Exception:
                pass
        # Stable-offset ARENA residency (see naksha_vulkan_c_api.h). Older DLLs
        # simply lack these, and the stream manager then keeps the legacy
        # whole-cloud upload.
        for _fn, _restype, _argtypes in (
            ("nkv_reserve_point_capacity", ctypes.c_int,
             [ctypes.c_uint64, ctypes.c_uint64]),
            ("nkv_upload_point_tile", ctypes.c_int,
             [ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_void_p,
              ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]),
            ("nkv_get_point_capacity", ctypes.c_uint64, [ctypes.c_uint64]),
            ("nkv_get_arena_uploaded_tiles", ctypes.c_uint64, [ctypes.c_uint64]),
        ):
            try:
                getattr(dll, _fn).restype = _restype
                getattr(dll, _fn).argtypes = _argtypes
            except Exception:
                pass
        # Phase 2 GPU-memory accounting: measured surface counts, so the
        # per-buffer breakdown is read from the renderer rather than
        # estimated from an assumed vertex stride.
        for _fn in ("nkv_get_surface_vertex_count",
                    "nkv_get_surface_index_count",
                    "nkv_get_point_color_upload_count",
                    "nkv_get_point_classification_upload_count"):
            try:
                getattr(dll, _fn).restype = ctypes.c_uint64
                getattr(dll, _fn).argtypes = [ctypes.c_uint64]
            except Exception:
                pass  # older DLL without the accessor
        # Adaptive sizing + GPU-side shading (see point.vert / the header).
        dll.nkv_set_point_size.restype = ctypes.c_int
        dll.nkv_set_point_size.argtypes = [ctypes.c_uint64, ctypes.c_float]
        dll.nkv_set_point_size_params.restype = ctypes.c_int
        dll.nkv_set_point_size_params.argtypes = [ctypes.c_uint64] + [ctypes.c_float] * 4
        dll.nkv_set_point_elevation_range.restype = ctypes.c_int
        dll.nkv_set_point_elevation_range.argtypes = [ctypes.c_uint64] + [ctypes.c_float] * 3
        dll.nkv_set_point_intensity_range.restype = ctypes.c_int
        dll.nkv_set_point_intensity_range.argtypes = [ctypes.c_uint64] + [ctypes.c_float] * 4
        # DEPTH mode (NKV_DISPLAY_DEPTH). Bound defensively: an older DLL simply
        # lacks the symbol, and Depth then reports UNSUPPORTED instead of raising.
        if hasattr(dll, "nkv_set_point_depth_range"):
            dll.nkv_set_point_depth_range.restype = ctypes.c_int
            dll.nkv_set_point_depth_range.argtypes = [ctypes.c_uint64] + [ctypes.c_float] * 3
        dll.nkv_set_point_sprite_params.restype = ctypes.c_int
        dll.nkv_set_point_sprite_params.argtypes = [ctypes.c_uint64] + [ctypes.c_float] * 2
        dll.nkv_set_point_luts.restype = ctypes.c_int
        dll.nkv_set_point_luts.argtypes = [
            ctypes.c_uint64, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ]
        dll.nkv_get_point_intensity_upload_count.restype = ctypes.c_uint64
        dll.nkv_get_point_intensity_upload_count.argtypes = [ctypes.c_uint64]
        # PHASE 1: attribute-only stream upload. Bound defensively so an older
        # DLL (without it) degrades to "unsupported" instead of raising at
        # call time, exactly like nkv_set_point_normals below.
        if hasattr(dll, "nkv_set_point_attributes"):
            dll.nkv_set_point_attributes.restype = ctypes.c_int
            dll.nkv_set_point_attributes.argtypes = [
                ctypes.c_uint64, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64,
            ]
        for _n in ("nkv_get_point_classification_count",
                   "nkv_get_point_intensity_count"):
            if hasattr(dll, _n):
                getattr(dll, _n).restype = ctypes.c_uint64
                getattr(dll, _n).argtypes = [ctypes.c_uint64]
        # Draw/LUT telemetry. These return uint64_t in the C API. With no
        # restype ctypes assumes c_int (32-bit) on Windows x64, so the value
        # is truncated to 32 bits and the high half of RAX is read as garbage.
        # It goes unnoticed while the counters are small, then starts reporting
        # nonsense (or negative) values once they grow. Declare them.
        for _n in ("nkv_get_lut_update_count",
                   "nkv_get_point_draw_call_count",
                   "nkv_get_surface_draw_call_count",
                   "nkv_get_surface_overlay_draw_call_count"):
            if hasattr(dll, _n):
                getattr(dll, _n).restype = ctypes.c_uint64
                getattr(dll, _n).argtypes = [ctypes.c_uint64]
        # Native OFFSCREEN pixel readback (RGBA8 of the current camera view).
        dll.nkv_capture_frame.restype = ctypes.c_int
        dll.nkv_capture_frame.argtypes = [
            ctypes.c_uint64, ctypes.c_void_p, ctypes.c_uint64,
            ctypes.c_void_p, ctypes.c_void_p,
        ]
        # Depth readback of the same offscreen capture (float32 [0,1]).
        # The parity gate needs it to prove a sampled pixel is actually owned by
        # the face under test - colour alone cannot show that.
        if hasattr(dll, "nkv_capture_depth"):
            dll.nkv_capture_depth.restype = ctypes.c_int
            dll.nkv_capture_depth.argtypes = [
                ctypes.c_uint64, ctypes.c_void_p, ctypes.c_uint64,
                ctypes.c_void_p, ctypes.c_void_p,
            ]

    # -- RenderBackend interface -------------------------------------------
    def initialize(self, native_window_handle: Any, width: int, height: int) -> bool:
        if not self.is_available():
            return False
        try:
            self._bind_signatures()
            hwnd = ctypes.c_void_p(int(native_window_handle))
            _enable_validation = 1 if os.environ.get("NAKSHA_VULKAN_VALIDATION", "").strip().lower() in (
                "1", "true", "yes", "on") else 0
            self._handle = self._dll.nkv_create_renderer(hwnd, int(width), int(height), _enable_validation)
            if self._handle == 0:
                _log.warning("nkv_create_renderer failed: %s", self._dll.nkv_last_error())
                return False
            self._last_extent = (max(1, int(width)), max(1, int(height)))
            # Colour-space/stage parity defaults, pushed at creation so even the
            # plain point-cloud view (which never goes through Shaded Class)
            # already matches the VTK viewport byte-for-byte. shading_display
            # re-pushes the shaded-class values (ambient floor / base light
            # elevation) whenever that path uploads or changes a parameter.
            self.set_color_parity_params(color_parity_mode(), shading_debug_stage())
            return True
        except Exception:
            _log.exception("VulkanRenderBackend.initialize failed; falling back to VTK")
            self._handle = 0
            return False

    def shutdown(self) -> None:
        if self._dll is not None and self._handle:
            try:
                self._dll.nkv_destroy_renderer(ctypes.c_uint64(self._handle))
            except Exception:
                _log.exception("VulkanRenderBackend.shutdown failed (ignored)")
        self._handle = 0

    def resize(self, width: int, height: int) -> None:
        self._last_extent = (max(1, int(width)), max(1, int(height)))
        if self._handle:
            self._dll.nkv_resize(ctypes.c_uint64(self._handle), int(width), int(height))
        # DEV instrumentation (guarded, return value unused).
        try:
            from gui import instant_shaded_telemetry as ist
            if ist.get() is not None:
                ist.emit('resize', renderer='naksha_vulkan',
                         width=int(width), height=int(height))
        except Exception:
            pass

    def set_test_points(self, xyz: np.ndarray, rgb: Optional[np.ndarray] = None) -> None:
        self.set_point_cloud(xyz, rgb=rgb)

    def normalize_point_cloud_arrays(xyz: np.ndarray, rgb: Optional[np.ndarray] = None,
                                    classification: Optional[np.ndarray] = None,
                                    intensity: Optional[np.ndarray] = None):
        """Flatten native point-cloud scalar attributes to the exact per-point
        contracts expected by the DLL: classification/intensity are 1D arrays
        matching xyz count, and single-column wrappers from higher-level loaders
        are normalized rather than treated as a fatal contract violation."""
        if xyz is None or xyz.ndim != 2 or xyz.shape[1] != 3:
            raise ValueError("xyz must be (N,3)")
        xyz = np.ascontiguousarray(xyz, dtype=np.float64)
        count = int(xyz.shape[0])

        if rgb is not None:
            rgb = np.asarray(rgb)
            if rgb.ndim == 2 and rgb.shape == (count, 1):
                rgb = rgb[:, 0]
            if rgb.ndim == 1 and rgb.size == count * 3:
                rgb = rgb.reshape(count, 3)
            rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
            if rgb.shape != (count, 3):
                raise ValueError("rgb must be (N,3) uint8 matching xyz count")

        if classification is not None:
            classification = np.asarray(classification)
            if classification.ndim == 2 and classification.shape[1] == 1 and classification.shape[0] == count:
                classification = classification[:, 0]
            elif classification.ndim == 2 and classification.shape == (1, count):
                classification = classification[0]
            else:
                classification = np.ravel(classification)
            classification = np.ascontiguousarray(classification, dtype=np.uint8)
            if classification.shape != (count,):
                # PART 27: see the note in _normalize_streams - a mismatched
                # CLASS range must not raise; drop it and render Neutral.
                _warn_class_mismatch("update", count, classification.shape)
                classification = None

        if intensity is not None:
            intensity = np.asarray(intensity)
            if intensity.ndim == 2 and intensity.shape[1] == 1 and intensity.shape[0] == count:
                intensity = intensity[:, 0]
            elif intensity.ndim == 2 and intensity.shape == (1, count):
                intensity = intensity[0]
            else:
                intensity = np.ravel(intensity)
            intensity = np.ascontiguousarray(intensity, dtype=np.float32)
            if intensity.shape != (count,):
                _warn_optional_mismatch("intensity", "update", count,
                                        intensity.shape)
                intensity = None

        return {"xyz": xyz, "rgb": rgb, "classification": classification,
                "intensity": intensity}

    def set_point_cloud(self, xyz: np.ndarray, rgb: Optional[np.ndarray] = None,
                         classification: Optional[np.ndarray] = None,
                         intensity: Optional[np.ndarray] = None) -> bool:
        """Validate dtype/shape/contiguity and upload once. No extra full-array
        copies are made unless an input array is not already the required
        dtype/contiguity (np.ascontiguousarray is a no-op copy in that case)."""
        if not self._handle:
            return False
        normalized = normalize_point_cloud_arrays(
            xyz=xyz, rgb=rgb, classification=classification, intensity=intensity)
        xyz = normalized["xyz"]
        rgb = normalized["rgb"]
        classification = normalized["classification"]
        intensity = normalized["intensity"]
        count = xyz.shape[0]
        xyz_ptr = xyz.ctypes.data_as(ctypes.c_void_p)

        rgb_ptr = ctypes.c_void_p(0)
        if rgb is not None:
            rgb_ptr = rgb.ctypes.data_as(ctypes.c_void_p)

        cls_ptr = ctypes.c_void_p(0)
        if classification is not None:
            cls_ptr = classification.ctypes.data_as(ctypes.c_void_p)

        intensity_ptr = ctypes.c_void_p(0)
        if intensity is not None:
            intensity_ptr = intensity.ctypes.data_as(ctypes.c_void_p)

        ok = self._dll.nkv_set_point_cloud(
            ctypes.c_uint64(self._handle), xyz_ptr, rgb_ptr, cls_ptr, intensity_ptr,
            ctypes.c_uint64(count),
        )
        return bool(ok)

    def set_surface(self, positions: np.ndarray, faces: np.ndarray, face_colors: np.ndarray) -> bool:
        """positions: (V,3) float64 world-space. faces: (T,3) int32. face_colors:
        (T,3) uint8 - matches gui/surface_mode.py's documented geometry contract."""
        if not self._handle:
            return False
        positions = np.ascontiguousarray(positions, dtype=np.float64)
        faces = np.ascontiguousarray(faces, dtype=np.int32)
        face_colors = np.ascontiguousarray(face_colors, dtype=np.uint8)
        if positions.ndim != 2 or positions.shape[1] != 3:
            raise ValueError("positions must be (V,3)")
        if faces.ndim != 2 or faces.shape[1] != 3:
            raise ValueError("faces must be (T,3)")
        if face_colors.shape != (faces.shape[0], 3):
            raise ValueError("face_colors must be (T,3) uint8 matching face count")
        ok = self._dll.nkv_set_surface(
            ctypes.c_uint64(self._handle),
            positions.ctypes.data_as(ctypes.c_void_p), ctypes.c_uint64(positions.shape[0]),
            faces.ctypes.data_as(ctypes.c_void_p), ctypes.c_uint64(faces.shape[0]),
            face_colors.ctypes.data_as(ctypes.c_void_p),
        )
        return bool(ok)

    def set_shaded_class_surface(self, positions: np.ndarray, faces: np.ndarray,
                                  vertex_class_id: np.ndarray, class_color_lut: np.ndarray,
                                  mixed_face_ids: np.ndarray) -> bool:
        """Real Shaded-Class (crisp-hybrid) upload. `positions` (V,3) float64
        world-space, `faces` (T,3) int32, `vertex_class_id` (V,) uint8,
        `class_color_lut` (256,3) uint8 RGB indexed by class id,
        `mixed_face_ids` (M,) int32 subset of face indices whose 3 vertices
        show different display colors (gui/shading_display.py's
        _collect_mixed_display_faces output). Uploads ONCE; call
        set_crisp_shading_parameters() for any azimuth/sharpness/ambient
        change afterwards - never re-call this for a parameter-only change."""
        if not self._handle:
            return False
        positions = np.ascontiguousarray(positions, dtype=np.float64)
        faces = np.ascontiguousarray(faces, dtype=np.int32)
        vertex_class_id = np.ascontiguousarray(vertex_class_id, dtype=np.uint8)
        class_color_lut = np.ascontiguousarray(class_color_lut, dtype=np.uint8)
        mixed_face_ids = np.ascontiguousarray(mixed_face_ids, dtype=np.int32)
        if positions.ndim != 2 or positions.shape[1] != 3:
            raise ValueError("positions must be (V,3)")
        if faces.ndim != 2 or faces.shape[1] != 3:
            raise ValueError("faces must be (T,3)")
        if vertex_class_id.shape != (positions.shape[0],):
            raise ValueError("vertex_class_id must be (V,) uint8 matching positions count")
        if class_color_lut.shape != (256, 3):
            raise ValueError("class_color_lut must be (256,3) uint8")
        ok = self._dll.nkv_set_shaded_class_surface(
            ctypes.c_uint64(self._handle),
            positions.ctypes.data_as(ctypes.c_void_p), ctypes.c_uint64(positions.shape[0]),
            faces.ctypes.data_as(ctypes.c_void_p), ctypes.c_uint64(faces.shape[0]),
            vertex_class_id.ctypes.data_as(ctypes.c_void_p),
            class_color_lut.ctypes.data_as(ctypes.c_void_p),
            mixed_face_ids.ctypes.data_as(ctypes.c_void_p) if mixed_face_ids.size else ctypes.c_void_p(0),
            ctypes.c_uint64(mixed_face_ids.shape[0]),
        )
        return bool(ok)

    def set_crisp_shading_parameters(self, azimuth_deg: float, sharpness_raw: float,
                                      ambient: float, key_intensity: float = 0.85,
                                      fill_intensity: float = 0.18) -> bool:
        if not self._handle:
            return False
        return bool(self._dll.nkv_set_crisp_shading_parameters(
            ctypes.c_uint64(self._handle),
            ctypes.c_float(azimuth_deg), ctypes.c_float(sharpness_raw), ctypes.c_float(ambient),
            ctypes.c_float(key_intensity), ctypes.c_float(fill_intensity),
        ))

    def supports_class_color_lut(self) -> bool:
        """True when the DLL can recolour from a 1 KB LUT with no geometry work."""
        if not self._handle:
            return False
        return getattr(self._dll, "nkv_set_class_color_lut", None) is not None

    def set_class_color_lut(self, lut_rgb: np.ndarray) -> bool:
        """PALETTE-CHANGE PATH (Phase C): upload only the 256-entry palette.

        This is the call a class-colour checkbox must use. It re-uploads 1 KB and
        nothing else - no positions, no normals, no indices, no per-vertex class
        ids - and it does NOT increment nkv_get_surface_upload_count(). The mesh
        uploaded once by set_shaded_class_surface() stays resident and is simply
        re-interpreted by the new palette.

        lut_rgb is (256,3) uint8.
        """
        fn = getattr(self._dll, "nkv_set_class_color_lut", None)
        if not self._handle or fn is None:
            return False
        arr = np.ascontiguousarray(lut_rgb, dtype=np.uint8)
        if arr.shape != (256, 3):
            raise ValueError("lut_rgb must be (256,3) uint8")
        return bool(fn(ctypes.c_uint64(self._handle),
                       arr.ctypes.data_as(ctypes.c_void_p)))

    def get_class_lut_update_count(self) -> int:
        fn = getattr(self._dll, "nkv_get_class_lut_update_count", None)
        if not self._handle or fn is None:
            return -1
        return int(fn(ctypes.c_uint64(self._handle)))

    def set_color_parity_params(self, color_mode: int = 1, debug_stage: int = 0,
                                ambient_floor: float = -1.0,
                                base_light_elevation_deg: float = 0.0) -> bool:
        """Shading parity with the VTK viewport (see nkv_set_color_parity_params).

        color_mode  1 = the shaders pre-compensate the swapchain's sRGB encode so
                        the framebuffer holds exactly the byte the CPU computed
                        (gui/shading_display.py), which is what makes a switch
                        between the VTK and Vulkan viewports invisible;
                    0 = the previous raw linear write.
        debug_stage 0 final colour, 1 face normal, 2 lighting factor,
                    3 class colour, 4 raw N.L - staged isolation of the pipeline.
        ambient_floor / base_light_elevation_deg carry the CPU's own resolved
                    values so an environment override cannot desync the shader.

        Applied to both the point cloud and the Shaded Class mesh, because both
        write into the same sRGB swapchain. Push-constant only: no re-upload.
        Returns False when the loaded DLL predates this entry point, so callers
        can degrade instead of breaking.
        """
        result = self._nkv("nkv_set_color_parity_params",
                           ctypes.c_uint64(self._handle),
                           ctypes.c_int(int(color_mode)), ctypes.c_int(int(debug_stage)),
                           ctypes.c_float(float(ambient_floor)),
                           ctypes.c_float(float(base_light_elevation_deg)))
        if result:
            self._parity_color_mode = int(color_mode)
            self._parity_debug_stage = int(debug_stage)
            self._parity_ambient_floor = float(ambient_floor)
            self._parity_base_elevation_deg = float(base_light_elevation_deg)
        return bool(result)

    def set_crisp_debug_mode(self, debug_stage: int) -> bool:
        """Show one stage of the crisp pipeline instead of the final colour:
        1 face normal, 2 lighting factor, 3 class colour, 4 raw N.L, 0 = final.
        Everything else (colour mode, ambient floor, elevation) is left alone."""
        if not self._handle:
            return False
        current = 1 if self._parity_color_mode else 0
        ok = self.set_color_parity_params(
            current, int(debug_stage),
            self._parity_ambient_floor, self._parity_base_elevation_deg)
        if ok:
            self._parity_debug_stage = int(debug_stage)
        return ok

    def get_parity_param_update_count(self) -> int:
        return int(self._nkv("nkv_get_parity_param_update_count",
                             ctypes.c_uint64(self._handle)) or 0)

    def get_surface_upload_count(self) -> int:
        if not self._handle:
            return 0
        return int(self._dll.nkv_get_surface_upload_count(ctypes.c_uint64(self._handle)))

    def get_shade_param_update_count(self) -> int:
        if not self._handle:
            return 0
        return int(self._dll.nkv_get_shade_param_update_count(ctypes.c_uint64(self._handle)))

    def get_overlay_triangle_count(self) -> int:
        if not self._handle:
            return 0
        return int(self._dll.nkv_get_overlay_triangle_count(ctypes.c_uint64(self._handle)))

    # -- Tiled surface / LOD / culling / atomic swap (Parts 6-11) -------------
    def _nkv_u(self, name: str):
        """Call an nkv_<name> that returns uint64. None when unsupported."""
        if not self._handle or self._dll is None:
            return None
        fn = getattr(self._dll, name, None)
        if fn is None:
            return None
        try:
            return int(fn(ctypes.c_uint64(self._handle)))
        except Exception:
            return None

    def supports_tiled_surface(self) -> bool:
        """True when the loaded DLL exports the tiled surface entry points.

        Older DLLs still work through the untiled set_surface() path; this
        lets callers report honestly rather than silently doing less.
        """
        return bool(self._handle and self._dll is not None
                    and getattr(self._dll, "nkv_set_surface_tiled", None) is not None)

    def get_last_error(self) -> str:
        try:
            fn = getattr(self._dll, "nkv_last_error", None)
            if fn is None:
                return ""
            fn.restype = ctypes.c_char_p
            return (fn() or b"").decode("utf-8", "replace")
        except Exception:
            return ""

    def set_interacting(self, moving: bool, idle_refine_ms: float = 400.0) -> bool:
        """Tells the renderer whether the camera is moving.

        While moving, the renderer forces the interaction/coarse LOD so a
        53M-triangle SLOW surface is never the navigation geometry; after
        idle_refine_ms of stillness it refines to the requested quality by
        itself, so a caller that simply stops sending events still converges.
        """
        return bool(self._nkv("nkv_set_surface_interacting",
                              ctypes.c_uint64(self._handle),
                              ctypes.c_int(1 if moving else 0),
                              ctypes.c_double(float(idle_refine_ms))))

    def set_lod_error_pixels(self, pixels: float) -> bool:
        return bool(self._nkv("nkv_set_surface_lod_error_pixels",
                              ctypes.c_uint64(self._handle),
                              ctypes.c_float(float(pixels))))

    def set_dataset_revision(self, revision: int) -> bool:
        return bool(self._nkv("nkv_set_dataset_revision",
                              ctypes.c_uint64(self._handle),
                              ctypes.c_uint64(int(revision))))

    def set_surface_revision(self, revision: int) -> bool:
        return bool(self._nkv("nkv_set_surface_revision",
                              ctypes.c_uint64(self._handle),
                              ctypes.c_uint64(int(revision))))

    def get_surface_state(self) -> int:
        """0 EMPTY, 1 PREVIEW_ACTIVE, 2 FINAL_PENDING, 3 FINAL_ACTIVE, -1 n/a."""
        v = self._nkv_u("nkv_get_surface_state")
        return int(v) if v is not None else -1

    def get_surface_swaps(self) -> dict:
        return {"swaps": self._nkv_u("nkv_get_surface_swap_count"),
                "stale_discards": self._nkv_u("nkv_get_surface_stale_discard_count")}

    def set_streaming_surface_active(self, active: bool) -> bool:
        return bool(self._nkv("nkv_set_streaming_surface_active", ctypes.c_uint64(self._handle), ctypes.c_int(bool(active))))

    def get_surface_draw_proof(self) -> dict:
        function = getattr(self._dll, "nkv_get_surface_draw_proof", None)
        if function is None or not self._handle:
            return {"supported": False}
        function.argtypes = [ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint64)]
        function.restype = ctypes.c_int
        # 12 words: see NKV_SURFACE_DRAW_PROOF_WORDS. The array was 5 words and
        # the native side now writes 12, so the "supported" probe below also
        # guards against an older DLL that would only fill the first 5.
        values = (ctypes.c_uint64 * 12)()
        if not function(ctypes.c_uint64(self._handle), values):
            return {"supported": False}
        active = bool(values[0])
        return dict(supported=True, native_mode="SURFACE" if active else "POINTS",
                    point_draw_calls=int(values[1]), point_draw_points=int(values[2]),
                    triangle_draw_calls=int(values[3]), triangle_count=int(values[4]),
                    indexed_draw_calls=int(values[3]), vkCmdDraw=int(values[1]),
                    vkCmdDrawIndexed=int(values[3]), polygon_mode="FILL" if active else "N/A",
                    surface_pipeline_bound=active and bool(values[3]), surface_point_overlay="OFF" if active else "N/A",
                    visible_tiles=int(values[5]), resident_tiles=int(values[6]),
                    selected_lod=int(values[7]), surface_state=int(values[8]),
                    surface_vertices=int(values[9]), surface_indices=int(values[10]),
                    surface_triangles=int(values[11]),
                    indexed_triangles_drawn=active and int(values[3]) > 0 and int(values[4]) > 0,
                    surface_vertex_shader="surface_indexed.vert.spv" if active else None,
                    surface_fragment_shader="surface_indexed.frag.spv" if active else None)

    def activate_pending_surface(self) -> bool:
        discarded = ctypes.c_int()
        return bool(self._nkv("nkv_activate_pending_surface", ctypes.c_uint64(self._handle), ctypes.byref(discarded)))

    def set_surface_indexed_blocks(self, positions, faces, colors, counts,
                                   dataset_revision, surface_revision) -> bool:
        if not self._handle or self._dll is None:
            return False
        function = getattr(self._dll, "nkv_set_surface_indexed_blocks", None)
        if function is None:
            return False
        positions = np.ascontiguousarray(positions, np.float64)
        faces = np.ascontiguousarray(faces, np.uint32)
        colors = np.ascontiguousarray(colors, np.uint8)
        counts = np.ascontiguousarray(counts, np.uint32)
        if positions.ndim != 2 or positions.shape[1] != 3 or faces.ndim != 2 or faces.shape[1] != 3:
            raise ValueError("Surface positions/indices must be (N,3)")
        if colors.shape != (len(faces), 3) or counts.sum(dtype=np.uint64) != faces.size or np.any(counts % 3):
            raise ValueError("Surface cells and block index ranges are misaligned")
        if not len(faces) or faces.max() >= len(positions):
            raise ValueError("Surface index outside shared vertex resource")
        return bool(function(ctypes.c_uint64(self._handle), ctypes.c_void_p(positions.ctypes.data),
            ctypes.c_uint64(len(positions)), ctypes.c_void_p(faces.ctypes.data), ctypes.c_uint64(len(faces)),
            ctypes.c_void_p(colors.ctypes.data), ctypes.c_void_p(counts.ctypes.data), ctypes.c_uint32(len(counts)),
            ctypes.c_uint64(dataset_revision), ctypes.c_uint64(surface_revision)))

    def set_surface_tiled(self, positions: np.ndarray, faces: np.ndarray,
                          face_colors: np.ndarray, tile_count: int = 0,
                          base_cell_m: float = 0.0, is_preview: bool = False,
                          dataset_revision: int = 0,
                          surface_revision: int = 0) -> bool:
        """Uploads a surface as a native TILE + LOD resource set.

        Unlike set_surface(), this NEVER overwrites the live GPU buffers: the
        mesh goes into a PENDING set that is promoted at the next frame
        boundary, so the viewport keeps drawing the previous surface for the
        whole upload. Returns False (with get_last_error() set) when the upload
        was refused - including a VRAM-budget refusal, in which case the
        current surface stays on screen untouched.
        """
        if not self._handle or self._dll is None:
            return False
        fn = getattr(self._dll, "nkv_set_surface_tiled", None)
        if fn is None:
            return False
        positions = np.ascontiguousarray(positions, dtype=np.float64)
        faces = np.ascontiguousarray(faces, dtype=np.int32)
        face_colors = np.ascontiguousarray(face_colors, dtype=np.uint8)
        if positions.ndim != 2 or positions.shape[1] != 3:
            raise ValueError("positions must be (V,3)")
        if faces.ndim != 2 or faces.shape[1] != 3:
            raise ValueError("faces must be (T,3)")
        if face_colors.shape != (faces.shape[0], 3):
            raise ValueError("face_colors must be (T,3) uint8 matching face count")
        ok = fn(
            ctypes.c_uint64(self._handle),
            positions.ctypes.data_as(ctypes.c_void_p), ctypes.c_uint64(positions.shape[0]),
            faces.ctypes.data_as(ctypes.c_void_p), ctypes.c_uint64(faces.shape[0]),
            face_colors.ctypes.data_as(ctypes.c_void_p),
            ctypes.c_uint32(int(tile_count)), ctypes.c_float(float(base_cell_m)),
            ctypes.c_int(1 if is_preview else 0),
            ctypes.c_uint64(int(dataset_revision)), ctypes.c_uint64(int(surface_revision)),
        )
        if not ok:
            self._last_surface_error = self.get_last_error()
        return bool(ok)

    def get_surface_culling(self) -> dict:
        """Total/visible tile and triangle counts from the last cull."""
        total_tiles = self._nkv_u("nkv_get_surface_total_tile_count")
        visible_tiles = self._nkv_u("nkv_get_surface_visible_tile_count")
        total_tris = self._nkv_u("nkv_get_surface_total_triangle_count")
        visible_tris = self._nkv_u("nkv_get_surface_visible_triangle_count")
        known = lambda a, b: (a - b) if (a is not None and b is not None) else -1
        return {"total_tiles": -1 if total_tiles is None else total_tiles,
                "visible_tiles": -1 if visible_tiles is None else visible_tiles,
                "culled_tiles": known(total_tiles, visible_tiles),
                "total_triangles": -1 if total_tris is None else total_tris,
                "visible_triangles": -1 if visible_tris is None else visible_tris,
                "culled_triangles": known(total_tris, visible_tris)}

    def get_surface_lod(self) -> dict:
        """Selected LOD, motion flag, and the resident triangle count per level."""
        b = self._dll
        out = {"lod": -1, "moving": False, "counts": [-1, -1, -1]}
        if not self._handle or b is None:
            return out
        try:
            fn = getattr(b, "nkv_get_surface_lod", None)
            if fn is not None:
                moving = ctypes.c_int(0)
                out["lod"] = int(fn(ctypes.c_uint64(self._handle), ctypes.byref(moving)))
                out["moving"] = bool(moving.value)
        except Exception:
            pass
        try:
            fn = getattr(b, "nkv_get_surface_lod_triangle_counts", None)
            if fn is not None:
                arr = (ctypes.c_uint64 * 3)()
                if fn(ctypes.c_uint64(self._handle), arr):
                    out["counts"] = [int(arr[0]), int(arr[1]), int(arr[2])]
        except Exception:
            pass
        return out

    def get_surface_memory(self) -> dict:
        """Budget / active / pending / LOD-cache / peak bytes for the report."""
        keys = ("budget", "active", "pending", "lod_cache", "peak_during_swap")
        out = {k: -1 for k in keys}
        out["rejected"] = False
        b = self._dll
        if not self._handle or b is None:
            return out
        try:
            fn = getattr(b, "nkv_get_surface_memory", None)
            if fn is not None:
                buf = [ctypes.c_uint64(0) for _ in keys]
                if fn(ctypes.c_uint64(self._handle), *[ctypes.byref(x) for x in buf]):
                    out = {k: int(v.value) for k, v in zip(keys, buf)}
        except Exception:
            pass
        try:
            fn = getattr(b, "nkv_get_surface_memory_rejected", None)
            if fn is not None:
                out["rejected"] = bool(fn(ctypes.c_uint64(self._handle)))
        except Exception:
            pass
        return out

    def set_display_mode(self, mode: int) -> bool:
        if not self._handle:
            return False
        return bool(self._dll.nkv_set_display_mode(ctypes.c_uint64(self._handle), int(mode)))

    def set_shading_parameters(self, formula: int, azimuth_deg: float, elevation_deg: float, ambient: float) -> bool:
        if not self._handle:
            return False
        return bool(self._dll.nkv_set_shading_parameters(
            ctypes.c_uint64(self._handle), int(formula),
            ctypes.c_float(azimuth_deg), ctypes.c_float(elevation_deg), ctypes.c_float(ambient),
        ))

    # -- adaptive sizing + GPU-side shading ----------------------------------
    def _nkv(self, name: str, *args):
        """Call nkv_<name> when this DLL exposes it. Returns None when the
        entry point is missing (older DLL) or there is no live handle, so
        callers can degrade to "unsupported" instead of raising."""
        if not self._handle or self._dll is None:
            return None
        fn = getattr(self._dll, name, None)
        if fn is None:
            return None
        try:
            return fn(*args)
        except Exception:
            _log.exception("nkv_%s raised", name)
            return None

    def set_point_size(self, pixels: float) -> bool:
        """Legacy fixed size (pins the adaptive band to one value)."""
        return bool(self._nkv("nkv_set_point_size", ctypes.c_uint64(self._handle),
                              ctypes.c_float(pixels)))

    def set_point_size_params(self, footprint_m: float, min_px: float, max_px: float,
                              class_intensity_mix: float = 0.0) -> bool:
        """point.vert sizes each point as
        clamp(footprint_m * pxPerMetre / depth, min_px, max_px) - the VTK-parity
        band. class_intensity_mix blends intensity into classification colours
        (0.0 = exactly what VTK paints)."""
        return bool(self._nkv(
            "nkv_set_point_size_params", ctypes.c_uint64(self._handle),
            ctypes.c_float(footprint_m), ctypes.c_float(min_px),
            ctypes.c_float(max_px), ctypes.c_float(class_intensity_mix)))

    def set_point_elevation_range(self, lo: float, hi: float, gamma: float = 1.0) -> bool:
        """z -> clamp((z-lo)/(hi-lo)) -> pow(t, gamma) -> elevation LUT."""
        return bool(self._nkv(
            "nkv_set_point_elevation_range", ctypes.c_uint64(self._handle),
            ctypes.c_float(lo), ctypes.c_float(hi), ctypes.c_float(gamma)))

    def set_point_intensity_range(self, lo: float, hi: float, contrast: float = 1.0,
                                  gamma: float = 1.0) -> bool:
        """intensity -> clamp((t-0.5)*contrast+0.5) -> pow(t, gamma) -> LUT."""
        return bool(self._nkv(
            "nkv_set_point_intensity_range", ctypes.c_uint64(self._handle),
            ctypes.c_float(lo), ctypes.c_float(hi),
            ctypes.c_float(contrast), ctypes.c_float(gamma)))

    def set_point_depth_range(self, lo: float, hi: float,
                              gamma: float = 1.0) -> bool:
        """DEPTH mode: normalise eye-relative distance to [0,1], then the
        grayscale ramp.

        Range-only and camera-derived, so this is a push-constant write. It is
        deliberately separate from set_point_intensity_range: intensity's range
        is a fixed property of the DATA, while depth's is a property of the
        CURRENT CAMERA and must be re-pushed as the camera moves.
        """
        return bool(self._nkv(
            "nkv_set_point_depth_range", ctypes.c_uint64(self._handle),
            ctypes.c_float(lo), ctypes.c_float(hi), ctypes.c_float(gamma)))

    def set_point_sprite_params(self, softness: float = 0.0, brightness: float = 1.0) -> bool:
        """point.frag: softness = 0 for VTK's hard-edged opaque disc (the default
        and what gui/unified_actor_manager.py does with `opacity = 1.0`); a
        small non-zero value anti-aliases the rim only. brightness = uniform RGB
        multiplier."""
        return bool(self._nkv(
            "nkv_set_point_sprite_params", ctypes.c_uint64(self._handle),
            ctypes.c_float(softness), ctypes.c_float(brightness)))

    def set_point_luts(self, class_rgb=None, elevation_rgb=None, intensity_rgb=None) -> bool:
        """Upload up to three (256,3) uint8 colour tables (None = keep)."""
        kept = []  # keep the numpy buffers alive across the ctypes call

        def _ptr(arr):
            if arr is None:
                return ctypes.c_void_p(0)
            a = np.ascontiguousarray(arr, dtype=np.uint8)
            if a.shape != (256, 3):
                raise ValueError(f"LUT must be (256,3) uint8, got {a.shape}")
            kept.append(a)
            return ctypes.c_void_p(a.ctypes.data)

        cls_p, elev_p, int_p = _ptr(class_rgb), _ptr(elevation_rgb), _ptr(intensity_rgb)
        return bool(self._nkv("nkv_set_point_luts", ctypes.c_uint64(self._handle),
                              cls_p, elev_p, int_p))

    # ---- Instant Shaded Class ---------------------------------------------
    def supports_instant_shaded(self) -> bool:
        """True when the loaded DLL exports the Instant Shaded Class entry
        points. Older DLLs don't, and the caller must then fall back to the
        legacy TIN path rather than silently rendering nothing."""
        if not self._handle or self._dll is None:
            return False
        return (getattr(self._dll, "nkv_set_instant_shading_parameters", None) is not None
                and getattr(self._dll, "nkv_set_class_visibility", None) is not None
                and getattr(self._dll, "nkv_get_instant_shaded_available", None) is not None)

    def get_instant_shaded_available(self) -> bool:
        """True when the native pass actually BUILT (shaders present, pipeline
        created). This is the honest "is the instant renderer really here"
        signal - distinct from the mode merely being selected."""
        value = self._nkv("nkv_get_instant_shaded_available",
                          ctypes.c_uint64(self._handle))
        return bool(value) if value is not None else False

    def get_instant_shaded_frame_count(self) -> int:
        """Frames recorded through the instant path (0 = it never ran)."""
        value = self._nkv("nkv_get_instant_shaded_frame_count",
                          ctypes.c_uint64(self._handle))
        return int(value) if value is not None else 0

    def set_instant_shading_parameters(self, azimuth_deg: float, elevation_deg: float,
                                       ambient: float, debug_stage: int = 0) -> bool:
        """Sun azimuth (compass degrees), light elevation (degrees above the
        horizon) and the 0..1 ambient floor - the exact triple
        gui/shading_display.py::_compute_shading uses, so the instant and
        legacy paths converge on the same look from the same sliders.
        Push-constant only: safe on every slider tick, touches no buffer."""
        value = self._nkv(
            "nkv_set_instant_shading_parameters",
            ctypes.c_uint64(self._handle),
            ctypes.c_float(float(azimuth_deg)),
            ctypes.c_float(float(elevation_deg)),
            ctypes.c_float(float(ambient)),
            ctypes.c_int(int(debug_stage)),
        )
        # DEV instrumentation (guarded, return value unused): a lighting push is
        # pure uniform work and must never move the position counter.
        try:
            from gui import instant_shaded_telemetry as ist
            if ist.get() is not None:
                ist.emit('lighting_update', renderer='naksha_vulkan',
                         azimuth_deg=float(azimuth_deg),
                         elevation_deg=float(elevation_deg),
                         ambient=float(ambient),
                         debug_stage=int(debug_stage),
                         xyz_upload_delta=0, push_constant_bytes=128)
        except Exception:
            pass
        return bool(value) if value is not None else False

    def get_point_normal_count(self) -> int:
        """Points covered by the resident oct16x2 normal stream (0 = none)."""
        fn = getattr(self._dll, "nkv_get_point_normal_count", None)
        if not self._handle or fn is None:
            return 0
        try:
            return int(fn(ctypes.c_uint64(self._handle)))
        except Exception:
            return 0

    def get_point_normal_bytes(self) -> int:
        fn = getattr(self._dll, "nkv_get_point_normal_bytes", None)
        if not self._handle or fn is None:
            return 0
        try:
            return int(fn(ctypes.c_uint64(self._handle)))
        except Exception:
            return 0

    def get_point_normal_upload_count(self) -> int:
        fn = getattr(self._dll, "nkv_get_point_normal_upload_count", None)
        if not self._handle or fn is None:
            return 0
        try:
            return int(fn(ctypes.c_uint64(self._handle)))
        except Exception:
            return 0

    def set_point_normals(self, packed) -> bool:
        """Upload the packed oct16x2 stream (4 bytes/point, point count = len/4).

        Point i of this buffer MUST be the normal of point i of the resident
        position stream; that invariant is verified over the real cache by
        validate_stream_attribute_alignment.py. The bytes are uploaded verbatim -
        they are never expanded to float32x3 on the CPU."""
        arr = np.ascontiguousarray(packed, dtype=np.uint8).reshape(-1)
        if arr.size % 4 != 0:
            raise ValueError(f"oct16 stream must be a multiple of 4 bytes, "
                             f"got {arr.size}")
        points = arr.size // 4
        if points == 0:
            return False
        return bool(self._nkv("nkv_set_point_normals",
                              ctypes.c_uint64(self._handle),
                              ctypes.c_void_p(arr.ctypes.data),
                              ctypes.c_uint64(points)))

    def set_point_attributes(self, classification=None, intensity=None) -> bool:
        """PHASE 1: upload ONLY the class / intensity streams, never XYZ.

        This is what makes "Neutral -> Class" or "Neutral -> Intensity" a
        zero-XYZ-upload switch: the position buffer is already resident and
        identical, so it must not be re-sent. `nkv_get_point_position_upload_count`
        is the counter that proves it.

        `classification` is (N,) uint8, `intensity` is (N,) float32, and N MUST
        equal the resident point count - the streams are bound by point index, so
        a length disagreement would shift every attribute onto the wrong point.
        The native side refuses such a call; this wrapper refuses first so the
        caller gets a Python-level reason.

        Pass None for a stream this call should not touch; the other still
        uploads. Returns False when the DLL predates the entry point, so the
        caller can report the mode as PENDING rather than drawing a wrong colour.
        """
        if not self._handle or self._dll is None:
            return False
        if getattr(self._dll, "nkv_set_point_attributes", None) is None:
            return False
        cls_arr = (np.ascontiguousarray(classification, dtype=np.uint8).reshape(-1)
                   if classification is not None else None)
        int_arr = (np.ascontiguousarray(intensity, dtype=np.float32).reshape(-1)
                   if intensity is not None else None)
        if cls_arr is None and int_arr is None:
            return False
        count = int(cls_arr.size if cls_arr is not None else int_arr.size)
        if (cls_arr is not None and cls_arr.size != count) or \
                (int_arr is not None and int_arr.size != count):
            raise ValueError("classification and intensity must have the same length")
        if count == 0:
            return False
        cls_p = (ctypes.c_void_p(cls_arr.ctypes.data) if cls_arr is not None
                 else ctypes.c_void_p(0))
        int_p = (ctypes.c_void_p(int_arr.ctypes.data) if int_arr is not None
                 else ctypes.c_void_p(0))
        return bool(self._nkv("nkv_set_point_attributes",
                              ctypes.c_uint64(self._handle),
                              cls_p, int_p, ctypes.c_uint64(count)))

    def get_point_classification_count(self) -> int:
        """Points covered by the resident classification stream (0 = absent)."""
        value = self._nkv("nkv_get_point_classification_count",
                          ctypes.c_uint64(self._handle))
        return int(value) if value is not None else 0

    def get_point_intensity_count(self) -> int:
        """Points covered by the resident intensity stream (0 = absent).

        This is what separates "the dataset has no intensity channel" from
        "intensity is present but every value is zero", so a legitimately black
        intensity cloud is never mistaken for an unwired stream.
        """
        value = self._nkv("nkv_get_point_intensity_count",
                          ctypes.c_uint64(self._handle))
        return int(value) if value is not None else 0

    def set_instant_normal_source(self, screen_space: bool) -> bool:
        """0 = stored oct16 normals (production), 1 = screen-space (DEV).
        One float of push constant: no upload, no rebuild."""
        value = self._nkv("nkv_set_instant_normal_source",
                          ctypes.c_uint64(self._handle),
                          ctypes.c_int(1 if screen_space else 0))
        return bool(value) if value is not None else False

    def set_instant_normal_radius(self, radius_texels: int) -> bool:
        """Reconstruction neighbourhood radius in texels (clamped 1..8).

        Screen-space depth-normal reconstruction on a 1-3 px splat depth buffer
        is dominated by WHICH point won the depth test in each pixel: at radius
        1 that is per-pixel noise, which renders as coloured confetti rather
        than a shaded surface. A wider neighbourhood averages across the same
        planar surface. Push-constant only; touches no buffer."""
        value = self._nkv("nkv_set_instant_normal_radius",
                          ctypes.c_uint64(self._handle),
                          ctypes.c_int(int(radius_texels)))
        return bool(value) if value is not None else False

    def set_class_visibility(self, visible) -> bool:
        """Upload the 256-BYTE visibility table (1 = shown). It rides in the
        class palette's alpha, so a hidden class writes no pixel and no depth
        on the instant path. A Display Mode checkbox change is therefore a
        uniform push, never a point re-upload."""
        arr = np.ascontiguousarray(visible, dtype=np.uint8).reshape(-1)
        if arr.size != 256:
            raise ValueError(f"visibility must have 256 entries, got {arr.size}")
        value = self._nkv("nkv_set_class_visibility",
                          ctypes.c_uint64(self._handle),
                          ctypes.c_void_p(arr.ctypes.data))
        return bool(value) if value is not None else False

    def get_class_visibility_update_count(self) -> int:
        value = self._nkv("nkv_get_class_visibility_update_count",
                          ctypes.c_uint64(self._handle))
        return int(value) if value is not None else 0

    def get_point_position_upload_count(self) -> int:
        """How many times the position stream has been uploaded. Must NOT move
        for class palette / visibility / weight / display-mode changes - those
        are uniform-only (LUT + push constant) by contract."""
        value = self._nkv("nkv_get_point_position_upload_count", ctypes.c_uint64(self._handle))
        return int(value) if value is not None else -1

    def get_point_color_upload_count(self) -> int:
        """How many times the RGB colour stream has been uploaded."""
        value = self._nkv("nkv_get_point_color_upload_count",
                          ctypes.c_uint64(self._handle))
        return int(value) if value is not None else -1

    def get_point_classification_upload_count(self) -> int:
        """How many times the classification stream has been uploaded."""
        value = self._nkv("nkv_get_point_classification_upload_count",
                          ctypes.c_uint64(self._handle))
        return int(value) if value is not None else -1

    def set_point_cloud_visible(self, visible: bool) -> bool:
        """Show/hide the resident point cloud (no upload, no destroy).

        Shaded Classification and Surface present a mesh instead of the raw
        cloud, so the app hides its point actors in those modes. The native
        viewport needs the same, otherwise the points stay in front of the
        mesh. Returns False when the DLL predates this entry point.
        """
        return bool(self._nkv("nkv_set_point_cloud_visible",
                              ctypes.c_uint64(self._handle), ctypes.c_int(1 if visible else 0)))

    def get_point_cloud_visible(self) -> int:
        """1 visible / 0 hidden / -1 unknown (older DLL)."""
        value = self._nkv("nkv_get_point_cloud_visible", ctypes.c_uint64(self._handle))
        return int(value) if value is not None else -1

    # -- telemetry (diagnostics) -------------------------------------------
    def get_lut_update_count(self) -> int:
        """Colour-table replacements. Rises on PTC / palette / visibility /
        weight / display-mode changes. Pair with get_point_position_upload_count:
        LUT rising while positions stay flat == the GPU re-tint path."""
        value = self._nkv("nkv_get_lut_update_count", ctypes.c_uint64(self._handle))
        return int(value) if value is not None else -1

    def get_point_draw_call_count(self) -> int:
        value = self._nkv("nkv_get_point_draw_call_count", ctypes.c_uint64(self._handle))
        return int(value) if value is not None else -1

    def get_surface_draw_call_count(self) -> int:
        value = self._nkv("nkv_get_surface_draw_call_count", ctypes.c_uint64(self._handle))
        return int(value) if value is not None else -1

    def get_surface_overlay_draw_call_count(self) -> int:
        value = self._nkv("nkv_get_surface_overlay_draw_call_count",
                          ctypes.c_uint64(self._handle))
        return int(value) if value is not None else -1

    def get_max_point_size(self) -> float:
        """Device ceiling limits.pointSizeRange[1] (e.g. 2047.9). The shader
        clamps gl_PointSize to it so a requested footprint is never silently
        clamped by the driver. -1.0 when the DLL predates the query."""
        if not self._handle:
            return -1.0
        try:
            out = ctypes.c_float(0.0)
            if not self._dll.nkv_get_max_point_size(ctypes.c_uint64(self._handle),
                                                     ctypes.byref(out)):
                return -1.0
            return float(out.value)
        except Exception:
            return -1.0

    def get_render_origin(self) -> str:
        """Floating render origin (world - origin is what the vertex buffer
        holds). Empty when the DLL predates the query."""
        if not self._handle:
            return "unknown"
        # The engine signature is nkv_get_render_origin(handle, double* out3).
        # It MUST be called once, with a real out-pointer. An earlier version
        # first routed it through _nkv() with three c_double BY VALUE, which
        # makes the engine dereference whatever those 24 bytes of stack hold as
        # a double* - a wild write that hard-crashes the process.
        out = (ctypes.c_double * 3)()
        try:
            fn = getattr(self._dll, "nkv_get_render_origin", None)
            if fn is None:
                return "unknown"
            fn.restype = ctypes.c_int
            fn.argtypes = [ctypes.c_uint64, ctypes.POINTER(ctypes.c_double)]
            # Pass the array itself, NOT ctypes.byref(out): with a strict
            # POINTER(c_double) argtype, byref() is a byref object rather than
            # an LP_c_double and ctypes rejects it outright.
            if not fn(ctypes.c_uint64(self._handle), out):
                return "unknown"
            return f"({out[0]:.2f},{out[1]:.2f},{out[2]:.2f})"
        except Exception:
            _log.exception("nkv_get_render_origin failed")
            return "unknown"

    def set_camera_ortho(self, centre, view_dir, parallel_scale,
                         near_clip=0.1, far_clip=10000.0) -> bool:
        """TRUE orthographic (VTK parallel) camera.

        This is the Naksha main view: the state is centre + parallel scale +
        aspect + near/far and nothing else. Zoom changes parallel_scale, pan
        changes the centre, and there is NO eye distance for a zoom to move -
        which is exactly why the previous FOV simulation made zoom behave like
        a 3D viewer and stretched point clouds into rays.
        """
        if not self._handle:
            return False
        ext = getattr(self, "_last_extent", (1, 1)) or (1, 1)
        aspect = (float(ext[0]) / float(ext[1])) if ext[1] else 1.0
        c = [float(v) for v in centre]
        d = [float(v) for v in view_dir]
        return bool(self._dll.nkv_set_camera_ortho(
            ctypes.c_uint64(self._handle),
            ctypes.c_double(c[0]), ctypes.c_double(c[1]), ctypes.c_double(c[2]),
            ctypes.c_double(d[0]), ctypes.c_double(d[1]), ctypes.c_double(d[2]),
            ctypes.c_double(float(parallel_scale)),
            ctypes.c_double(aspect),
            ctypes.c_double(float(near_clip)), ctypes.c_double(float(far_clip))))

    def set_camera_perspective_mode(self) -> bool:
        """Leave orthographic (3D orbit mode)."""
        if not self._handle:
            return False
        return bool(self._nkv("nkv_set_camera_perspective_mode",
                              ctypes.c_uint64(self._handle)))

    def get_camera_projection(self):
        """(is_orthographic, parallel_scale) as the engine actually holds it."""
        if not self._handle:
            return (-1, float("nan"))
        try:
            is_ortho = ctypes.c_int(0)
            scale = ctypes.c_double(0.0)
            if not self._dll.nkv_get_camera_projection(
                    ctypes.c_uint64(self._handle), ctypes.byref(is_ortho),
                    ctypes.byref(scale)):
                return (-1, float("nan"))
            return (int(is_ortho.value), float(scale.value))
        except Exception:
            return (-1, float("nan"))

    def last_error(self) -> str:
        """Engine-side diagnostic text (last C API failure), or ''."""
        if not self._handle or self._dll is None:
            return ""
        try:
            return (self._dll.nkv_last_error() or b"").decode("utf-8", errors="replace")
        except Exception:
            return ""

    def request_render_safe(self) -> bool:
        """request_render() that reports failure instead of propagating.

        The C ABI can raise through ctypes when the engine throws a C++
        exception across the FFI boundary; a diagnostic-only frame request must
        never abort the caller, so the error is captured and returned."""
        try:
            self.request_render()
            return True
        except Exception as _e:
            _log.warning("Vulkan request_render failed: %r (%s)", _e, self.last_error())
            return False

    def get_point_intensity_upload_count(self) -> int:
        """How many intensity buffers this renderer has uploaded (proof that
        the 4-stream upload path ran, not just the RGB one)."""
        try:
            return int(self._nkv("nkv_get_point_intensity_upload_count",
                                 ctypes.c_uint64(self._handle)) or 0)
        except Exception:
            return 0

    def set_camera(self, view_matrix: np.ndarray, proj_matrix: np.ndarray) -> None:
        # RenderBackend's abstract signature is matrix-based for interface
        # parity with a future direct-matrix path, but the current native
        # Camera API (include/naksha/Camera.hpp) is look-at based, not
        # matrix-injection based. Callers that have a VTK vtkCamera should
        # use set_camera_lookat() below instead; this override intentionally
        # does nothing rather than silently misinterpreting matrices.
        _log.debug("VulkanRenderBackend.set_camera(matrix) is a no-op; use set_camera_lookat()")

    def set_camera_lookat(self, eye, target, fov_y_degrees=45.0, near_clip=0.1, far_clip=10000.0) -> bool:
        if not self._handle:
            return False
        return bool(self._dll.nkv_set_camera_lookat(
            ctypes.c_uint64(self._handle),
            ctypes.c_double(eye[0]), ctypes.c_double(eye[1]), ctypes.c_double(eye[2]),
            ctypes.c_double(target[0]), ctypes.c_double(target[1]), ctypes.c_double(target[2]),
            ctypes.c_double(fov_y_degrees), ctypes.c_double(near_clip), ctypes.c_double(far_clip),
        ))

    def request_render(self) -> None:
        if self._handle:
            # nkv_render() return contract (see naksha_vulkan_c_api.h):
            #   2 = a frame was drawn, submitted AND presented
            #   1 = frame skipped (minimized / swapchain out of date)
            #   0 = failure
            # VULKAN_PRESENT_COUNT only counts 2 - a skipped frame is NOT a
            # present. Before this distinction existed nkv_render() returned 1
            # for both cases, so a renderer that was skipping every single
            # frame still looked "ACTIVE". This is the honest signal the
            # status bar's strict ACTIVE wording is gated on.
            self.render_count += 1
            _t0 = time.perf_counter()
            code = int(self._dll.nkv_render(ctypes.c_uint64(self._handle)))
            _cpu_ms = (time.perf_counter() - _t0) * 1000.0
            if code == 2:
                self.present_count = _bump_present_count()
                # [VULKAN FRAME]: only a PRESENTED frame (code 2) counts, so a
                # skipped/failed frame cannot flatter the FPS. The interval is
                # measured between presents, which is the cadence a user feels.
                _now = time.perf_counter()
                _prev = getattr(self, "_last_present_perf", None)
                if _prev is not None:
                    _owner = getattr(self, "_owner_ref", lambda: None)()
                    if _owner is not None:
                        _owner.note_frame((_now - _prev) * 1000.0, _cpu_ms)
                self._last_present_perf = _now
            elif code == 1:
                self.skip_count += 1
            else:
                self.fail_count += 1

    def set_point_draw_ranges(self, firsts, counts) -> bool:
        """Set the visible draw windows (LOD path).

        Only the DRAW COMMANDS change - the point/colour/classification/
        intensity buffers stay exactly as uploaded. The caller must have
        uploaded the full cloud in tile-index order for a range to address a
        contiguous block of points.
        """
        if not self._handle:
            return False
        try:
            f = np.ascontiguousarray(np.asarray(firsts, dtype=np.uint32))
            c = np.ascontiguousarray(np.asarray(counts, dtype=np.uint32))
            n = int(f.size)
            ok = self._dll.nkv_set_point_draw_ranges(
                ctypes.c_uint64(self._handle),
                f.ctypes.data_as(ctypes.POINTER(ctypes.c_uint32)),
                c.ctypes.data_as(ctypes.POINTER(ctypes.c_uint32)),
                ctypes.c_uint32(n))
            return bool(ok)
        except Exception:
            return False

    # -- stable-offset arena residency ----------------------------------------
    def supports_point_arena(self) -> bool:
        """True when the loaded DLL has the arena entry points."""
        return bool(self._handle and self._dll is not None
                    and getattr(self._dll, "nkv_upload_point_tile", None) is not None
                    and getattr(self._dll, "nkv_reserve_point_capacity", None) is not None)

    def reserve_point_capacity(self, capacity_points: int) -> int:
        """0 = failed, 1 = ready, 2 = (re)allocated: every earlier tile is invalid."""
        if not self.supports_point_arena():
            return 0
        try:
            return int(self._dll.nkv_reserve_point_capacity(
                ctypes.c_uint64(self._handle), ctypes.c_uint64(int(capacity_points))))
        except Exception:
            return 0

    def upload_point_tile(self, first: int, xyz, classification=None,
                          intensity=None, normals_oct16=None) -> bool:
        """Write ONE tile at a stable point offset. Every provided stream uses the
        same offset, so XYZ i / class i / intensity i / normal i are one point."""
        if not self.supports_point_arena():
            return False
        try:
            # xyz=None -> ATTRIBUTE-ONLY upload for a tile whose positions are
            # already resident; the point count then comes from the streams.
            x = None if xyz is None else np.ascontiguousarray(
                np.asarray(xyz, dtype=np.float64).reshape(-1, 3))
            if x is not None:
                n = int(x.shape[0])
            elif classification is not None:
                n = int(np.asarray(classification).reshape(-1).size)
            elif intensity is not None:
                n = int(np.asarray(intensity).reshape(-1).size)
            elif normals_oct16 is not None:
                n = int(np.asarray(normals_oct16, dtype=np.int16).nbytes // 4)
            else:
                return False
            keep = [x]
            cptr = iptr = nptr = None
            if classification is not None:
                c = np.ascontiguousarray(np.asarray(classification, dtype=np.uint8).reshape(-1))
                if c.size != n:
                    return False                  # never draw a short stream against XYZ
                keep.append(c); cptr = c.ctypes.data_as(ctypes.c_void_p)
            if intensity is not None:
                i = np.ascontiguousarray(np.asarray(intensity, dtype=np.float32).reshape(-1))
                if i.size != n:
                    return False
                keep.append(i); iptr = i.ctypes.data_as(ctypes.c_void_p)
            if normals_oct16 is not None:
                m = np.ascontiguousarray(np.asarray(normals_oct16, dtype=np.int16))
                if m.nbytes != n * 4:
                    return False
                keep.append(m); nptr = m.ctypes.data_as(ctypes.c_void_p)
            return bool(self._dll.nkv_upload_point_tile(
                ctypes.c_uint64(self._handle), ctypes.c_uint64(int(first)),
                ctypes.c_uint64(n),
                None if x is None else x.ctypes.data_as(ctypes.c_void_p),
                cptr, iptr, nptr))
        except Exception:
            return False

    def get_point_capacity(self) -> int:
        fn = getattr(self._dll, "nkv_get_point_capacity", None) if self._handle else None
        return int(fn(ctypes.c_uint64(self._handle))) if fn is not None else 0

    def get_arena_uploaded_tiles(self) -> int:
        fn = getattr(self._dll, "nkv_get_arena_uploaded_tiles", None) if self._handle else None
        return int(fn(ctypes.c_uint64(self._handle))) if fn is not None else 0

    def clear_point_draw_ranges(self) -> bool:
        """Restore the single full-buffer draw (LOD off)."""
        if not self._handle:
            return False
        try:
            return bool(self._dll.nkv_clear_point_draw_ranges(
                ctypes.c_uint64(self._handle)))
        except Exception:
            return False

    def get_point_draw_range_count(self) -> int:
        if not self._handle:
            return 0
        try:
            return int(self._dll.nkv_get_point_draw_range_count(
                ctypes.c_uint64(self._handle)))
        except Exception:
            return 0

    def get_point_draw_range_points(self) -> int:
        if not self._handle:
            return 0
        try:
            return int(self._dll.nkv_get_point_draw_range_points(
                ctypes.c_uint64(self._handle)))
        except Exception:
            return 0

    def get_gpu_frame_time_ms(self) -> float:
        """Real GPU frame time in ms, or -1.0 when unavailable.

        Thin wrapper over nkv_get_gpu_frame_time_ms so callers get an honest
        "not available" sentinel rather than a 0.0 that looks like a
        measurement. Never returns nan.
        """
        if not self._handle:
            return -1.0
        try:
            v = float(self._dll.nkv_get_gpu_frame_time_ms(
                ctypes.c_uint64(self._handle)))
            return v if v == v else -1.0   # guard against a nan from the driver
        except Exception:
            return -1.0

    def get_frame_timing(self) -> dict:
        """Real per-frame timing straight from the engine.

        GPU values come from the VK_QUERY_TYPE_TIMESTAMP query pool and are
        corrected by the device timestampPeriod, so they are genuine GPU
        times - NOT the CPU submit time relabelled. CPU submit and present are
        measured inside the renderer around vkQueueSubmit / vkQueuePresentKHR.

        `valid` is 0 until at least one full frame's timestamps have been read
        back (the read happens at the START of the next frame, so a value is
        always one frame behind).
        """
        out = {"cpuSubmitMs": 0.0, "gpuRenderMs": 0.0, "pointGpuMs": 0.0,
               "surfaceGpuMs": 0.0, "presentMs": 0.0, "valid": False}
        if not self._handle:
            return out
        try:
            c = ctypes.c_double(); g = ctypes.c_double(); p = ctypes.c_double()
            s = ctypes.c_double(); pr = ctypes.c_double(); v = ctypes.c_int()
            ok = self._dll.nkv_get_frame_timing(
                ctypes.c_uint64(self._handle), ctypes.byref(c),
                ctypes.byref(g), ctypes.byref(p), ctypes.byref(s),
                ctypes.byref(pr), ctypes.byref(v))
            if not ok:
                return out
            return {"cpuSubmitMs": c.value, "gpuRenderMs": g.value,
                    "pointGpuMs": p.value, "surfaceGpuMs": s.value,
                    "presentMs": pr.value, "valid": bool(v.value)}
        except Exception:
            return out

    def _poll_first_presented_frame(self, app=None) -> bool:
        """True once the engine has presented at least one frame.

        Observation only. The engine counts a frame as rendered only when it
        reached vkQueuePresentKHR, which is exactly the FIRST FRAME RULE
        condition - the upload completing is NOT the same thing, because it
        only enqueues GPU work.
        """
        try:
            rendered = int(self.get_frame_stats()[0])
        except Exception:
            return False
        if rendered < 1:
            return False
        if app is not None:
            try:
                app._note_first_presented_frame()
            except Exception:
                pass
        return True

    def get_frame_stats(self) -> tuple:
        """(rendered, skipped, swapchain_recreates) straight from the engine.

        Independent of the Python-side counters above: the native renderer
        counts a frame as rendered only when it reached vkQueuePresentKHR.
        Returns (0, 0, 0) when the native call is unavailable.
        """
        if not self._handle:
            return (0, 0, 0)
        try:
            rendered = ctypes.c_uint64()
            skipped = ctypes.c_uint64()
            recreates = ctypes.c_uint64()
            ok = self._dll.nkv_get_frame_stats(
                ctypes.c_uint64(self._handle), ctypes.byref(rendered),
                ctypes.byref(skipped), ctypes.byref(recreates))
            if not ok:
                return (0, 0, 0)
            return (int(rendered.value), int(skipped.value), int(recreates.value))
        except Exception:
            return (0, 0, 0)

    def get_point_count(self) -> int:
        """Points currently resident in the GPU position buffer."""
        if not self._handle:
            return 0
        try:
            return int(self._dll.nkv_get_point_count(ctypes.c_uint64(self._handle)))
        except Exception:
            return 0

    def set_clear_color(self, r: float, g: float, b: float, a: float = 1.0) -> bool:
        """Viewport background colour (default opaque black)."""
        if not self._handle:
            return False
        try:
            return bool(self._dll.nkv_set_clear_color(
                ctypes.c_uint64(self._handle), ctypes.c_float(r), ctypes.c_float(g),
                ctypes.c_float(b), ctypes.c_float(a)))
        except Exception:
            return False

    def capture_frame(self) -> Optional[np.ndarray]:
        """Native OFFSCREEN readback of the current camera view: RGBA8 (h, w, 4)
        uint8, top-down rows, or None when unsupported/failed.

        nkv_capture_frame renders the same frame-slot UBO the live view uses
        (i.e. the camera last pushed through nkv_set_camera_lookat) into a CPU
        buffer. This is the ONLY honest pixel source on Windows: both
        QScreen.grabWindow and PrintWindow go through GDI, which reports a
        flat black image for a Vulkan surface (measured: std=0.00 no matter
        what the engine was presenting).

        BLOCKING - it waits for the GPU. Diagnostics / parity tests only,
        never call it per frame."""
        if not self._handle or self._dll is None:
            return None
        fn = getattr(self._dll, "nkv_capture_frame", None)
        if fn is None:
            _log.info("nkv_capture_frame missing (older DLL); offscreen capture unavailable")
            return None
        try:
            w, h = getattr(self, "_last_extent", (0, 0))
            buf = np.empty((max(1, int(h)), max(1, int(w)), 4), dtype=np.uint8)
            out_w = ctypes.c_uint32(0)
            out_h = ctypes.c_uint32(0)

            def _call():
                return bool(fn(
                    ctypes.c_uint64(self._handle),
                    buf.ctypes.data_as(ctypes.c_void_p),
                    ctypes.c_uint64(buf.nbytes),
                    ctypes.byref(out_w), ctypes.byref(out_h),
                ))

            ok = _call()
            if not ok and out_w.value and out_h.value:
                # Extent changed since the last resize(): native reported the
                # real size even though the buffer was short - retry once.
                buf = np.empty((int(out_h.value), int(out_w.value), 4), dtype=np.uint8)
                ok = _call()
            if not ok or out_w.value <= 0 or out_h.value <= 0:
                _log.info("nkv_capture_frame failed: %s",
                          (self._dll.nkv_last_error() or b"")[:200])
                return None
            return np.ascontiguousarray(buf[:out_h.value, :out_w.value])
        except Exception:
            _log.exception("VulkanRenderBackend.capture_frame failed (ignored)")
            return None

    def capture_depth(self) -> Optional[np.ndarray]:
        """Native OFFSCREEN depth readback of the current camera view: float32
        (h, w) in [0,1], top-down rows, or None when unsupported/failed.

        Renders the SAME frame (same frame-slot UBO, same Record() calls) as
        capture_frame(), so a depth read taken right after a colour read
        corresponds to that colour.

        Why the parity gate needs this: to compare a face's shading against the
        pixel it projects to, the test must first prove that face actually OWNS
        that pixel. A different, nearer face can legitimately own it and still
        produce a plausible colour - so without depth, "colour mismatch" and
        "wrong face sampled" are indistinguishable. Depth separates them.

        Requires a D32_SFLOAT depth attachment; returns None otherwise (an older
        DLL, or a driver that chose another depth format).

        BLOCKING - it waits for the GPU. Diagnostics / parity tests only.
        """
        if not self._handle or self._dll is None:
            return None
        fn = getattr(self._dll, "nkv_capture_depth", None)
        if fn is None:
            _log.info("nkv_capture_depth missing (older DLL); no depth readback")
            return None
        try:
            w, h = getattr(self, "_last_extent", (0, 0))
            buf = np.empty((max(1, int(h)), max(1, int(w))), dtype=np.float32)
            out_w = ctypes.c_uint32(0)
            out_h = ctypes.c_uint32(0)

            def _call():
                return bool(fn(
                    ctypes.c_uint64(self._handle),
                    buf.ctypes.data_as(ctypes.c_void_p),
                    ctypes.c_uint64(buf.nbytes),
                    ctypes.byref(out_w), ctypes.byref(out_h),
                ))

            ok = _call()
            if not ok and out_w.value and out_h.value:
                buf = np.empty((int(out_h.value), int(out_w.value)), dtype=np.float32)
                ok = _call()
            if not ok or out_w.value <= 0 or out_h.value <= 0:
                _log.info("nkv_capture_depth failed: %s",
                          (self._dll.nkv_last_error() or b"")[:200])
                return None
            return np.ascontiguousarray(buf[:out_h.value, :out_w.value])
        except Exception:
            _log.exception("VulkanRenderBackend.capture_depth failed (ignored)")
            return None

    # -- device introspection (real values, for status banner/GUI label) --
    def get_device_name(self) -> str:
        if not self._handle:
            return ""
        try:
            raw = self._dll.nkv_get_device_name(ctypes.c_uint64(self._handle))
            return raw.decode("utf-8", errors="replace") if raw else ""
        except Exception:
            return ""

    # -- authoritative VRAM (Phase 3) ------------------------------------
    # The streaming budget planner must consume THESE, not a second independent
    # probe. Returning 0 means UNKNOWN, never "zero memory" - hardware.py treats
    # 0 as "no answer" and falls back, which is the correct behaviour for a
    # handle that never opened a device.
    def gpu_total_bytes(self) -> int:
        if not self._handle:
            return 0
        try:
            return int(self._dll.nkv_get_vram_bytes(
                ctypes.c_uint64(self._handle)) or 0)
        except Exception:
            return 0

    def heap_budget_bytes(self) -> int:
        if not self._handle:
            return 0
        try:
            return int(self._dll.nkv_get_device_vram_budget(
                ctypes.c_uint64(self._handle)) or 0)
        except Exception:
            return 0

    # Aliases hardware.py probes for. Keeping both names costs nothing and means
    # the planner finds the value whichever accessor it reaches for first.
    total_vram_bytes = gpu_total_bytes
    vram_bytes = gpu_total_bytes
    memory_budget_bytes = heap_budget_bytes
    gpu_budget_bytes = heap_budget_bytes

    def get_api_version_string(self) -> str:
        if not self._handle:
            return ""
        try:
            v = int(self._dll.nkv_get_api_version(ctypes.c_uint64(self._handle)))
            if v == 0:
                return ""
            major = (v >> 22) & 0x7F
            minor = (v >> 12) & 0x3FF
            patch = v & 0xFFF
            return f"{major}.{minor}.{patch}"
        except Exception:
            return ""

    def get_vram_bytes(self) -> int:
        if not self._handle:
            return 0
        try:
            return int(self._dll.nkv_get_vram_bytes(ctypes.c_uint64(self._handle)))
        except Exception:
            return 0

    def get_surface_vertex_count(self) -> int:
        """Resident surface vertices, measured from the renderer (-1 if the
        loaded DLL predates the Phase 2 accessors)."""
        if not self._handle:
            return 0
        try:
            return int(self._dll.nkv_get_surface_vertex_count(
                ctypes.c_uint64(self._handle)))
        except Exception:
            return -1

    def get_surface_index_count(self) -> int:
        """Resident surface indices, measured from the renderer (-1 if the
        loaded DLL predates the Phase 2 accessors)."""
        if not self._handle:
            return 0
        try:
            return int(self._dll.nkv_get_surface_index_count(
                ctypes.c_uint64(self._handle)))
        except Exception:
            return -1


# PySide6 exposes enums as SCOPED members of the Qt class (e.g.
# Qt.MouseButton.MiddleButton), not as attributes of the QtCore module. The
# previous `from PySide6 import QtCore as _Qt` bound the MODULE here, so every
# `_Qt.MiddleButton` / `_Qt.LeftButton` / `_Qt.ShiftModifier` /
# `_Qt.MouseFocusReason` below raised
#   AttributeError: module 'PySide6.QtCore' has no attribute 'MiddleButton'
# the first time a mouse event reached mousePressEvent. Importing Qt itself
# keeps the same short name but yields the enum holder.
try:
    from PySide6.QtCore import Qt, QEvent as _QEvent, QObject as _QObject, QTimer
    from PySide6.QtWidgets import QWidget as _QWidget
except Exception:  # pragma: no cover - test/build environments may omit Qt.
    class _QEvent:  # type: ignore[no-redef]
        pass

    class _QObject:  # type: ignore[no-redef]
        pass

    class _QWidget:  # type: ignore[no-redef]
        pass

    class QTimer:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            pass

    class Qt:  # type: ignore[no-redef]
        LeftButton = 0
        RightButton = 0
        MiddleButton = 0
        ShiftModifier = 0
        ControlModifier = 0
        MouseFocusReason = 0


class BackendState:
    """Distinct render-backend lifecycle states - never conflate READY (the
    renderer exists and CAN draw) with ACTIVE (a real render call for a real
    workload has actually gone through it this session). See
    AppRenderBackendOwner for the state machine that sets these."""
    UNAVAILABLE = "UNAVAILABLE"    # Vulkan runtime/device not detected
    AVAILABLE = "AVAILABLE"        # detected, not yet initialized
    INITIALIZED = "INITIALIZED"    # instance/device/native renderer created
    READY = "READY"                # surface/swapchain/shaders ready
    ACTIVE = "ACTIVE"              # a real workload has actually rendered via Vulkan
    FALLBACK = "FALLBACK"          # unavailable/failed/not selected - VTK is rendering
    ERROR = "ERROR"                # failed, with a real reason string


_CAM_TRACE = [False, 0, None]


def _cam_trace_on() -> bool:
    return os.environ.get("NAKSHA_DEV_CAMERA_TRACE", "").strip() not in (
        "", "0", "false", "False")


def cam_trace_input(kind: str = "INPUT") -> str:
    """Assign ONE id to a physical input and carry it through the chain."""
    if not _cam_trace_on():
        return "-"
    _CAM_TRACE[1] += 1
    _CAM_TRACE[2] = f"{kind}-{_CAM_TRACE[1]:03d}"
    print(f"[LIVE CAMERA EVENT CHAIN] event_id: {_CAM_TRACE[2]}  "
          f"opened: {kind}", flush=True)
    return _CAM_TRACE[2]


def cam_current_event_id() -> str:
    return _CAM_TRACE[2] or "-"


def _trace_push(writer: str, kw: dict, rig) -> None:
    """STEP 3/9/10: exactly what the engine is handed, and its footprint."""
    if not _cam_trace_on():
        return
    try:
        import numpy as _np
        c = kw.get("centre")
        s = float(kw.get("parallel_scale") or 0.0)
        n = float(kw.get("near_clip") or 0.0)
        f = float(kw.get("far_clip") or 0.0)
        vd = kw.get("view_dir")
        print(f"\n[VULKAN CAMERA INPUT] event_id: {cam_current_event_id()}\n"
              f"  writer: {writer}\n"
              f"  center: ({float(c[0]):.4f}, {float(c[1]):.4f}, {float(c[2]):.4f})\n"
              f"  parallel_scale: {s:.6f}\n"
              f"  near: {n:.4f}   far: {f:.4f}   "
              f"(finite={_np.isfinite(n) and _np.isfinite(f)}, far>near={f > n})\n"
              f"  view_direction: ({float(vd[0]):.4f}, {float(vd[1]):.4f}, "
              f"{float(vd[2]):.4f})\n"
              f"  rig_distance: {float(rig.distance):.4f}   "
              f"rig_clip_source: {getattr(rig, 'clip_source_', '?')}", flush=True)
        sc = getattr(rig, "scene_center_", None)
        sr = getattr(rig, "scene_radius_", None)
        if sc is not None and sr and s > 0.0:
            span = max(float(sr) * 2.0, 1e-9)
            print(f"[CAMERA FOOTPRINT] event_id: {cam_current_event_id()}\n"
                  f"  parallel_scale: {s:.6f}\n"
                  f"  expected_extent_x: {span / (2.0 * s):.6f}  "
                  f"expected_extent_y: {span / (2.0 * s):.6f}  "
                  f"(1.0 == fills the viewport)", flush=True)
        else:
            print(f"[CAMERA FOOTPRINT] event_id: {cam_current_event_id()}\n"
                  f"  parallel_scale: {s:.6f}  dataset extent UNKNOWN "
                  f"(scene_center={sc} scene_radius={sr})", flush=True)
    except Exception:
        pass


class _VulkanCameraRig:
    """Turntable camera driving the native look-at API (nkv_set_camera_lookat).

    State is spherical around a target - the same parameterisation the native
    Camera::Orbit()/Dolly()/Pan() use (Camera.cpp:54-109) - so a mouse gesture
    produces exactly the eye an equivalent native orbit would. LiDAR world is
    Z-up: azimuth rotates about +Z, elevation is degrees above the XY plane.

    The C ABI has no up-vector argument on purpose: the engine keeps a fixed
    world up of +Y (Camera.hpp:58 `up_ = {0,1,0}` -> CoreCamera worldUp_), so
    the VTK mirror writes ViewUp=(0,1,0) too and both viewports frame the
    scene identically. Elevation is clamped to +-89.999 deg (native Orbit
    clamps to +-90-0.001) so f is never parallel to the up axis."""

    MIN_ELEV = -89.999
    MAX_ELEV = 89.999
    MIN_DIST = 1e-3
    MAX_DIST = 1e9
    DRAG_SPEED = 0.30      # degrees of orbit per pixel of drag
    DOLLY_FACTOR = 0.88    # zoom factor per wheel notch

    def __init__(self):
        self.target = np.zeros(3, dtype=np.float64)
        self.distance = 100.0
        self.azimuth = 45.0        # deg, atan2(dy, dx) of eye-target
        self.elevation = 35.0      # deg, asin(dz / |eye-target|)
        self.fov_y = 45.0          # vertical FOV, degrees
        self.near_clip = 0.1
        self.far_clip = 10000.0
        # Radius of the framed data, remembered so recompute_clip() can keep
        # far_clip clear of the cloud after a zoom (see recompute_clip).
        self.scene_radius_ = 0.0
        # Centre of that radius, so data_reach() can size the depth box from
        # wherever the camera is LOOKING rather than assuming the target sits
        # inside the data. None until an extent is known.
        self.scene_center_ = None
        # Which authority produced the planes currently pushed: "rig" (self
        # derived) or "vtk" (adopted after validation). Reported by
        # describe_camera() so a black viewport can be traced to its source.
        self.clip_source_ = "init"
        # When the app is in a PARALLEL (2D plan) view the rig is ORTHOGRAPHIC:
        # zoom scales parallel_scale, pan moves the centre, and the engine
        # builds a real ortho matrix. FOV is then never used - see dolly()/pan().
        # None = perspective mode (3D orbit).
        self.orthographic_ = False
        self.parallel_scale_ = 100.0
        self.MIN_PARALLEL_SCALE = 1e-3
        self.MAX_PARALLEL_SCALE = 1e9
        # Unit direction the camera looks along (plan view: straight down -Z).
        self.view_dir_ = np.array([0.0, 0.0, -1.0], dtype=np.float64)
        self.view_up = np.array([0.0, 1.0, 0.0])  # fixed +Y, engine rule

    # -- pose ---------------------------------------------------------------
    def eye(self) -> np.ndarray:
        az = math.radians(self.azimuth)
        el = math.radians(self.elevation)
        h = self.distance * math.cos(el)
        return self.target + np.array([
            h * math.cos(az), h * math.sin(az),
            self.distance * math.sin(el),
        ])

    def set_eye_target(self, eye, target, fov_y=None,
                       parallel_scale: Optional[float] = None) -> None:
        """Adopt an explicit eye/target pair (VTK camera, set_view preset).

        `parallel_scale` is passed when the source VTK camera is a PARALLEL
        (2D plan) camera. The rig then switches to ORTHOGRAPHIC mode: the
        centre is the VTK focal point and the parallel scale drives the
        visible window, with no FOV and no eye-distance zoom.
        """
        eye = np.asarray(eye, dtype=np.float64)
        target = np.asarray(target, dtype=np.float64)
        d = target - eye          # VIEW DIRECTION (eye -> target)
        r = float(np.linalg.norm(d))
        if r < 1e-12:
            return
        self.target = target.astype(np.float64)
        self.distance = min(max(r, self.MIN_DIST), self.MAX_DIST)
        # Spherical angles describe the EYE's offset FROM the target
        # (eye() = target + distance * unit(az, el)), so they must be taken
        # from eye - target. Deriving them from d = target - eye rotated the
        # azimuth by 180 deg and negated the elevation, which made eye()
        # return the point REFLECTION of the real camera through the target -
        # a mirrored view as soon as the rig pushed a perspective pose.
        off = -d
        self.azimuth = math.degrees(math.atan2(off[1], off[0]))
        self.elevation = math.degrees(math.asin(max(-1.0, min(1.0, off[2] / r))))
        if parallel_scale is not None:
            self.orthographic_ = True
            self.parallel_scale_ = max(float(parallel_scale), 1e-6)
            self.view_dir_ = d / r
        else:
            self.orthographic_ = False
            if fov_y:
                self.fov_y = float(fov_y)
        self.recompute_clip()

    def push_kwargs(self) -> dict:
        """Camera arguments for the native push.

        ORTHOGRAPHIC returns the ortho tuple (centre, view direction, parallel
        scale, near, far) - there is deliberately no eye, no target-distance
        and no FOV in it, because an orthographic camera does not have them.
        """
        if self.orthographic_:
            return {
                "ortho": True,
                "centre": self.target,
                "view_dir": self.view_dir_,
                "parallel_scale": float(self.parallel_scale_),
                "near_clip": float(self.near_clip),
                "far_clip": float(self.far_clip),
            }
        return {
            "eye": self.eye(),
            "target": self.target,
            "fov_y_degrees": float(self.fov_y),
            "near_clip": float(self.near_clip),
            "far_clip": float(self.far_clip),
        }

    def push_to_backend(self, backend) -> bool:
        """Send the rig pose using whichever projection the rig is in."""
        kw = self.push_kwargs()
        if kw.get("ortho"):
            _trace_push("rig.push_to_backend", kw, self)
            return bool(backend.set_camera_ortho(
                kw["centre"], kw["view_dir"], kw["parallel_scale"],
                near_clip=kw["near_clip"], far_clip=kw["far_clip"]))
        return bool(backend.set_camera_lookat(**kw))


    def fit_to_bounds(self, bounds, aspect: float = 1.0) -> None:
        """Frame [min,max] xyz like VTK ResetCamera.

        ORTHOGRAPHIC: reproduces VTK's parallel ResetCamera - centre on the
        bounds midpoint, and ParallelScale = the half-extent that makes the
        whole XY footprint fit, widened to the aspect ratio. That is the same
        computation VTK does, so fit view matches instead of merely
        approximating.
        """
        b = np.asarray(bounds, dtype=np.float64).reshape(2, 3)
        size = b[1] - b[0]
        self.target = b.mean(axis=0)
        # The bounding sphere is centred on the fitted bounds, so data_reach()
        # adds nothing here (centre == target) and the radius alone is exact.
        self.scene_center_ = self.target.astype(np.float64)
        half_x = max(abs(float(size[0])) * 0.5, 1e-6)
        half_y = max(abs(float(size[1])) * 0.5, 1e-6)
        if self.orthographic_:
            # VTK fits the larger of the two half-extents, then the aspect
            # ratio widens the box so nothing is cut off horizontally.
            self.parallel_scale_ = max(half_x / max(aspect, 1e-6), half_y, 1e-6)
            self.scene_radius_ = float(np.linalg.norm(size) * 0.5)
            # An ortho camera has no eye distance; park it far enough back that
            # the near/far box still contains the data.
            self.distance = max(self.parallel_scale_ * 4.0, self.scene_radius_ * 4.0, 1.0)
            self.view_dir_ = np.array([0.0, 0.0, -1.0], dtype=np.float64)
            self.recompute_clip()
            return
        radius = max(float(np.linalg.norm(size)) * 0.5, 1e-6)
        fov = max(self.fov_y, 5.0)
        dist = radius / math.tan(math.radians(fov) * 0.5)
        if aspect > 0:
            dist = max(dist, (radius / aspect) / math.tan(math.radians(fov) * 0.5))
        self.distance = min(max(dist, self.MIN_DIST), self.MAX_DIST)
        # Remember the framed radius: recompute_clip() uses it after a zoom so
        # far_clip still clears the data when the camera moves far away.
        self.scene_radius_ = radius
        self.recompute_clip(radius)

    def screen_basis(self):
        """(right, up) unit vectors of the view plane, world space.

        For a top-down -Z view with world up +Y this yields right=+X, up=+Y,
        which is the Naksha plan-view orientation. Shared by pan() and the
        ortho push so both agree on the screen axes."""
        f = np.asarray(self.view_dir_, dtype=np.float64)
        n = float(np.linalg.norm(f))
        if n < 1e-12:
            f = np.array([0.0, 0.0, -1.0])
        else:
            f = f / n
        right = np.cross(f, self.view_up)
        rl = float(np.linalg.norm(right))
        if rl < 1e-12:            # looking along up: fall back to screen +X
            right = np.array([1.0, 0.0, 0.0])
            rl = 1.0
        right = right / rl
        up = np.cross(right, f)
        return right, up

    def recompute_fov(self) -> None:
        """No-op in orthographic mode.

        An ortho projection has no FOV: the visible window is 2*parallelScale
        tall regardless of distance. Kept as a method so the perspective path
        and the shared call sites stay symmetric (and so nothing silently
        reintroduces a perspective approximation)."""
        return

    def recompute_clip(self, scene_radius: Optional[float] = None) -> None:
        """Near/far that TRACK the current distance.

        These planes used to be computed once, in fit_to_bounds(), and then
        never again. Every wheel notch changes `distance` but left near/far at
        their fit-time values, so:
          * zooming OUT past the fit distance put far_clip in front of the
            cloud and everything beyond it was clipped away, and
          * fitting a large scene (near = dist/1000) and then zooming IN
            below that near put the whole cloud in front of the near plane.
        Both look identical to the user - "the points disappear while
        zooming". Re-deriving on every distance change is the fix; the scene
        radius is remembered from the last fit so far still clears the data.

        This is also the ONLY box the rig trusts unconditionally: every other
        source (VTK's clipping range) has to pass adopt_vtk_clip() first.
        """
        radius = self.data_reach() if scene_radius is None else float(scene_radius)
        self.clip_source_ = "rig"
        if self.orthographic_:
            # Depth box for an ortho camera: the distance is nominal, so the
            # planes are derived from the data extent around the centre.
            #
            # Before set_data_extent() existed, the radius came only from
            # fit_to_bounds(), which the app never calls (it adopts VTK's
            # camera through set_eye_target instead) - so it stayed 0 and this
            # produced
            #   near == far == distance
            # a degenerate box: in perspective every vertex was clipped (an
            # all-black viewport), in ortho the projection divided by zero.
            # Both the measured data radius and the parallel scale (always a
            # valid floor: half the visible window) now feed it.
            radius = max(radius, float(self.parallel_scale_))
            self.near_clip = max(self.distance - radius * 4.0, 1e-3)
            self.far_clip = self.distance + radius * 4.0
            if self.far_clip <= self.near_clip:
                self.far_clip = self.near_clip + max(self.distance * 0.1, 1.0)
            return
        self.near_clip = max(self.distance / 1000.0, 1e-3)
        self.far_clip = max(self.distance * 10.0 + radius * 4.0,
                            self.near_clip + 10.0)

    # -- data extent ---------------------------------------------------------
    def set_data_extent(self, lo, hi) -> None:
        """Record the extent of the point cloud the engine actually holds.

        Called from AppRenderBackendOwner.upload_point_cloud() after a load,
        and from fit_to_bounds() for scripted fits. Without it the rig has no
        idea how deep the scene is - it can only guess from the parallel scale
        - and a thin guess is what clips a whole cloud away.
        """
        lo = np.asarray(lo, dtype=np.float64)
        hi = np.asarray(hi, dtype=np.float64)
        if lo.shape != (3,) or hi.shape != (3,) or not np.all(np.isfinite(hi - lo)):
            return
        self.scene_center_ = 0.5 * (lo + hi)
        # Bounding SPHERE, so it stays valid whichever way the camera looks.
        self.scene_radius_ = max(0.5 * float(np.linalg.norm(hi - lo)), 1e-6)
        self.recompute_clip()

    def data_reach(self) -> float:
        """Depth half-span of the data as seen from the current target.

        The bounding sphere is centred on the DATA; the camera turns about the
        TARGET. Using scene_radius_ alone would under-report the span whenever
        the two differ - panning away from the cloud would shrink the box that
        is supposed to contain it.
        """
        r = float(self.scene_radius_)
        centre = self.scene_center_
        if centre is not None:
            r += float(np.linalg.norm(np.asarray(centre, dtype=np.float64)
                                      - np.asarray(self.target, dtype=np.float64)))
        return r

    def adopt_vtk_clip(self, near, far) -> bool:
        """Take VTK's clipping range ONLY if it can actually be trusted.

        resync_camera() used to do `rig.near_clip, rig.far_clip = near, far`
        with no check at all, which silently undid everything recompute_clip()
        had just derived. VTK's range is computed from VTK's OWN renderer
        bounds - the actors in the hidden VTK viewport, not the buffers the
        Vulkan engine holds - and in the main viewport it is routinely useless:
        observed `near=5000.0000 far=5001.00` (a one-metre band) while the
        loaded cloud spanned `depth=[4946.59, 5075.90]`, i.e. 99.5% of the
        sampled points outside the frustum and an all-black 3D viewport. It
        also never recovered, because _mirror_rig_to_vtk() writes the rig's
        planes back into that same camera - source and sink of the bad box.

        So VTK is treated as a CANDIDATE: adopt its planes only when they are
        ordered, non-degenerate, and actually bracket the data. Otherwise keep
        the rig's own box. Rejecting a tighter VTK box only ever shows MORE of
        the scene, never less, so this cannot hide geometry - and VTK itself
        keeps rendering with whatever range it wants.
        """
        try:
            near, far = float(near), float(far)
        except (TypeError, ValueError):
            self.recompute_clip()
            return False
        reach = self.data_reach()
        span_lo = max(self.distance - reach, 1e-3)   # nearest data
        span_hi = self.distance + reach               # farthest data
        # A band thinner than 2% of the focal distance cannot be carrying a
        # LiDAR scene, however correct it looks against the bounds.
        min_width = max(self.distance * 0.02, 1.0)
        # PART 11: the band must also be BOUNDED. VTK re-derives its range on
        # every camera modification and pads it, and the app pads again, so an
        # unmoved camera's own far plane multiplies by ~1.1 per zoom/pan event
        # until it reaches astronomical values. Measured on 123.LAS: near 1e-6,
        # far 2.15e8 against a cloud whose true depth span is ~56 m - a 215,000,000 m
        # box destroys depth precision for the ortho projection and is what the
        # rig then pushed. The rig's own box (distance +- 4*radius) is a floor
        # for the radius, so anything far beyond a small multiple of it is a
        # padded accident, not a real constraint.
        self.recompute_clip()
        sane_far = max(self.far_clip * 4.0, span_hi + 1.0)
        ok = (near > 0.0 and far >= near + min_width
              and near <= span_lo and far >= span_hi
              and far <= sane_far)
        if ok:
            self.near_clip, self.far_clip = near, far
            self.clip_source_ = "vtk"
        else:
            self.recompute_clip()
        return ok

    # -- gestures -----------------------------------------------------------
    def orbit(self, dx_px: float, dy_px: float) -> None:
        """Left-drag: rotate about the target (native Camera::Orbit)."""
        self.azimuth = (self.azimuth + dx_px * self.DRAG_SPEED) % 360.0
        self.elevation = min(max(
            self.elevation - dy_px * self.DRAG_SPEED, self.MIN_ELEV), self.MAX_ELEV)
        # Tilting changes which part of the bounding sphere sits nearest, so
        # the depth box is re-derived just as VTK's ResetCameraClippingRange
        # does on every camera modification.
        self.recompute_clip()

    def dolly(self, notches: float) -> None:
        """Wheel zoom.

        ORTHOGRAPHIC (the Naksha main view): zoom changes ONLY the parallel
        scale, exactly as VTK's parallel camera does. The camera centre and
        the view direction do not move, there is no eye distance to dolly, and
        therefore no perspective distortion is introduced - the previous
        FOV-simulation approach moved the eye and shrank the world window,
        which is what made zoom behave like a 3D viewer and stretched the
        cloud into rays.

        PERSPECTIVE (3D orbit mode): the classic multiplicative dolly.
        """
        if self.orthographic_:
            # Per-notch multiplicative factor, matching VTK's parallel zoom.
            factor = (0.8 ** float(notches)) if notches > 0 else (1.25 ** -float(notches))
            self.parallel_scale_ = float(
                min(max(self.parallel_scale_ * factor, self.MIN_PARALLEL_SCALE),
                    self.MAX_PARALLEL_SCALE))
            self.recompute_clip()
            return
        factor = self.DOLLY_FACTOR ** float(notches)
        self.distance = min(max(self.distance * factor, self.MIN_DIST),
                            self.MAX_DIST)
        self.recompute_fov()
        self.recompute_clip()

    def pan(self, dx_px: float, dy_px: float, viewport_height: int) -> None:
        """Middle/Shift-drag: translate the view in the screen plane, 1 px of
        drag = 1 px at the focal plane (native Camera::Pan).

        ORTHOGRAPHIC: this moves the camera CENTRE only. The world-units-per-
        pixel is a constant 2*parallelScale/height, so a pan tracks the mouse
        1:1 at every zoom level - the behaviour VTK's parallel camera has and
        a perspective dolly cannot reproduce.
        """
        if viewport_height <= 0:
            return
        if self.orthographic_:
            units_per_px = (2.0 * self.parallel_scale_) / float(viewport_height)
            right, up = self.screen_basis()
            self.target = self.target + (-dx_px * right + dy_px * up) * units_per_px
            # The target moved away from the data centre, so the depth box has
            # to widen to keep the cloud inside near/far (see data_reach).
            self.recompute_clip()
            return
        units_per_px = (2.0 * self.distance
                        * math.tan(math.radians(self.fov_y) * 0.5)) / viewport_height
        eye = self.eye()
        f = self.target - eye
        flen = float(np.linalg.norm(f))
        if flen < 1e-12:
            return
        f = f / flen
        right = np.cross(f, self.view_up)
        rlen = float(np.linalg.norm(right))
        if rlen < 1e-12:            # looking along up: fall back to screen +X
            right, rlen = np.array([1.0, 0.0, 0.0]), 1.0
        right = right / rlen
        up = np.cross(right, f)     # same basis as CoreCamera::UpdateMatrices
        self.target = self.target + (-dx_px * right + dy_px * up) * units_per_px
        self.recompute_clip()      # see the ortho branch above


class _VulkanSurfaceWidget(_QWidget):
    """The Vulkan viewport child widget. Its only special behavior is
    forwarding Qt resize events to the owning AppRenderBackendOwner, which
    calls nkv_resize() (swapchain recreation only - never device/buffer
    recreation, per the native Renderer contract)."""

    def __init__(self, parent, owner: "AppRenderBackendOwner"):
        super().__init__(parent)
        self._owner = owner
        # Direct manipulation. The Vulkan widget IS the visible viewport now,
        # so it has to own navigation itself - the VTK interactor underneath
        # receives no input while hidden. Gestures update the shared
        # _VulkanCameraRig, which AppRenderBackendOwner._apply_rig_gesture()
        # pushes to nkv_set_camera_lookat AND mirrors into the VTK camera, so
        # both viewports (and VTK-based capture/parity scripts) stay in step.
        self._last_pos = None
        self._panning = False

    def _rig(self):
        rig = getattr(self._owner, "_camera_rig", None)
        return rig if rig is not None else None

    @staticmethod
    def _event_pos(event):
        return event.position() if hasattr(event, "position") else event.pos()

    def mousePressEvent(self, event):
        try:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
        except Exception:
            pass
        self._last_pos = self._event_pos(event)
        self._panning = event.button() == Qt.MouseButton.MiddleButton or (
            event.button() == Qt.MouseButton.LeftButton
            and bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        )

    def mouseMoveEvent(self, event):
        pos = self._event_pos(event)
        last, self._last_pos = self._last_pos, pos
        if last is None:
            return
        dx, dy = pos.x() - last.x(), pos.y() - last.y()
        if abs(dx) < 0.01 and abs(dy) < 0.01:
            return
        rig = self._rig()
        if rig is None:
            return
        _owns = getattr(self._owner, "owns_main_camera", None)
        if self._panning and _owns is not None and _owns():
            # STAGE D: MainCamera2D owns the pan (centre only, scale untouched).
            self._owner.apply_main_pan(dx, dy, max(1, self.height()))
            self._owner._note_surface_interaction(True)
            return
        if self._panning:
            # The canonical camera is changed by wheel zoom / fit WITHOUT going
            # through the rig, so the rig's pose is stale here. Panning the
            # stale rig and mirroring it back wrote the OLD parallel scale into
            # the camera (live trace on 123.las: 86.13 -> 94.74 on a pure pan),
            # i.e. every pan silently undid the zoom. Adopt the canonical
            # camera first so a pan moves the centre only and keeps the scale.
            try:
                self._owner.resync_camera(present=False)
            except Exception:
                logging.getLogger(__name__).exception(
                    "pan: canonical camera adopt failed; ignoring step")
                return
            rig.pan(dx, dy, max(1, self.height()))
        else:
            rig.orbit(dx, dy)
        self._owner._apply_rig_gesture()
        self._owner._note_surface_interaction(True)

    def mouseReleaseEvent(self, event):
        self._last_pos = None
        self._panning = False
        event.accept()

    def wheelEvent(self, event):
        # Zoom has exactly ONE owner: the VTK main camera.
        #
        # This surface used to dolly the shared rig itself and then write that
        # pose back into the VTK camera (_apply_rig_gesture -> _mirror_rig_to_vtk).
        # The VTK wheel observers on the widget underneath apply their own eased
        # zoom to the same camera, so ONE notch ran through two independent
        # multiplicative factors (0.8**n from rig.dolly and 1.10**n from the eased
        # handler). The product compounded every notch and drove ParallelScale
        # monotonically toward zero - measured on 123.LAS: 78.7448 -> 0.008692
        # (~9000x) after a short zoom/pan sequence, i.e. the cloud collapses to
        # a point. The surface now asks the canonical owner for exactly one zoom.
        notches = event.angleDelta().y() / 120.0
        if notches and self._owner is not None:
            try:
                _b4 = self._owner._camera_parallel_scale()
                # This surface is the SINGLE physical wheel receiver when it is
                # the visible main viewport, so it must not be debounced against
                # a phantom second sender.
                _ok = self._owner.apply_surface_wheel(notches)
                _af = self._owner._camera_parallel_scale()
                if os.environ.get("NAKSHA_DEV_CAMERA_TRACE", "").strip() not in (
                        "", "0", "false", "False"):
                    print(f"[REAL WHEEL] steps={notches:+.3f} "
                          f"scale_before={_b4:.6f} scale_after={_af:.6f} "
                          f"owner_result={_ok} mutation_count="
                          f"{0 if abs(_af - _b4) < 1e-9 else 1} "
                          f"event_accepted={'YES' if _ok else 'NO'}", flush=True)
                if _ok:
                    self._owner._note_surface_interaction(True)
                    event.accept()
                    return
                # PART D6: a valid notch must not be consumed silently. Let it
                # fall through to the other route rather than dying here.
                if abs(_af - _b4) < 1e-9 and abs(notches) > 1e-9:
                    print(f"[WHEEL CONTRACT FAILURE] steps={notches:+.3f} "
                          f"scale unchanged at {_b4:.6f} - event not consumed",
                          flush=True)
            except Exception:
                logging.getLogger(__name__).exception(
                    "Vulkan surface wheel zoom failed; ignoring event")
        event.ignore()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        try:
            size = event.size()
            w, h = max(1, size.width()), max(1, size.height())
            if (self._owner is not None and not getattr(self._owner, "_rebinding", False)
                    and self._owner.active and self._owner.vulkan_backend is not None):
                self._owner.vulkan_backend.resize(w, h)
                # Swapchain aspect changed: re-push the mirrored VTK camera
                # so nkv_set_camera_lookat recomputes the projection with the
                # new aspect BEFORE the fresh frame is presented.
                if hasattr(self._owner, "resync_camera"):
                    self._owner.resync_camera(present=False)
                self._owner.vulkan_backend.request_render()
        except Exception:
            logging.getLogger(__name__).exception("Vulkan surface resizeEvent handling failed (ignored)")


class _GeometryMirror(_QObject):
    """Keeps the Vulkan overlay exactly covering its host slot.

    Qt layouts only manage their own children, and the Vulkan widget is
    deliberately NOT added to the VTK widget's layout (it has to sit ON TOP
    of that slot, not share a grid cell), so this event filter mirrors the
    VTK widget's Move/Resize - and its parent's, for good measure - onto the
    overlay. Parenting it to `source` makes it die with that widget."""

    def __init__(self, source, target, host=None):
        super().__init__(source)
        self._target = target
        self._sources = [s for s in (source, host) if s is not None]
        for src in self._sources:
            try:
                src.installEventFilter(self)
            except Exception:
                pass

    def eventFilter(self, obj, event):
        try:
            if event.type() in (_QEvent.Resize, _QEvent.Move):
                geom = self._sources[0].geometry()
                if self._target.geometry() != geom:
                    self._target.setGeometry(geom)
        except Exception:
            pass
        return False


class AppRenderBackendOwner:
    """Single owner object for the app's render backend selection.

    Attached once as `app.render_backend` (see gui/app_window.py
    NakshaApp.__init__, right after self.vtk_widget is created). Owns:
    backend selection ("vtk" default / "vulkan" opt-in), one-time Vulkan
    probe+init, a dedicated child widget for the Vulkan surface (created
    but NOT shown unless Vulkan init actually succeeds - VTK's widget is
    never removed or replaced), and dataset upload. Every method here is
    wrapped so a Vulkan-side failure degrades to the VTK path silently
    (logged) rather than raising out of app_window.py.
    """

    def __init__(self, app: Any, vtk_widget: Any):
        self._app = app
        self._vtk_widget = vtk_widget
        self.backend_name = "vtk"           # kept for back-compat with existing call sites
        self.active = False                 # kept for back-compat: True once Vulkan is USABLE (>=READY)
        self.vulkan_backend: Optional[VulkanRenderBackend] = None
        self.vulkan_widget = None  # QWidget child, created lazily only on opt-in
        # NAKSHA_VULKAN_PREVIEW split view: VTK camera -> Vulkan camera sync.
        self._camera_sync_timer = None     # single-shot QTimer (coalescer)
        self._vtk_camera_observer = None   # vtkCamera ModifiedEvent tag
        # Shared turntable pose: mouse gestures update the rig, the rig pushes
        # to nkv_set_camera_lookat AND mirrors back into the VTK camera, and
        # programmatic VTK view changes (set_view/fit) adopt into the rig.
        self._camera_rig = _VulkanCameraRig()
        # Canonical main-camera state (see gui/main_camera.py). Stage A/B: seeded
        # from the working VTK camera; stages C-E move the writers onto it.
        from gui.main_camera import MainCamera2D
        self.main_camera = MainCamera2D()
        self.legacy_vtk_adoptions = {}      # source -> count of VTK -> MainCamera2D writes
        self._viewport_installed = False   # install_as_main_viewport() done
        self._geometry_mirror = None       # QEvent filter overlaying the VTK slot
        self._mirroring_camera = False     # suppress observer during rig->VTK writes
        # Single-owner camera state (see apply_surface_wheel / _apply_main_camera_state).
        # True while the canonical camera is being written, so the VTK ModifiedEvent
        # observer cannot start a second, independent camera change (PART 5).
        self._camera_sync_in_progress = False
        # (centre_x, centre_y, parallel_scale, viewport_w, viewport_h) of the last
        # pose actually pushed to Vulkan. An unchanged signature must not push,
        # re-render, or bump a generation (PART 6).
        self._last_pushed_camera_signature = None
        # The eye standoff an orthographic (2D plan) camera is parked at. An
        # ortho camera has no eye-distance, but near/far ARE derived from it, so
        # letting it drift on every pan moves the depth box under a view that
        # never moved. Captured once, then held (PART 10).
        self._ortho_standoff = None
        self._vulkan_hwnd = 0              # HWND the LIVE renderer is bound to (see _rebind_renderer_to_current_hwnd)
        self._rebinding = False            # True while the surface widget is being reparented
        self._log = logging.getLogger(__name__ + ".AppRenderBackendOwner")

        # ---- Real state machine (see BackendState) - single authoritative
        # source; other GUI code should read these, not invent its own flags.
        self.state = BackendState.UNAVAILABLE
        # Resolution rule (per explicit correction):
        #   env == "vulkan" -> explicit Vulkan request
        #   env == "vtk"    -> explicit VTK, no Vulkan attempt at all
        #   env == "auto" OR unset/empty/anything else -> attempt Vulkan,
        #     fall back to VTK silently-but-logged on any failure.
        # A plain `py main.py` with NO env var therefore now attempts Vulkan
        # automatically (requested_backend == "auto"), it does NOT skip
        # straight to vtk like the previous round's default did.
        _raw = os.environ.get("NAKSHA_RENDER_BACKEND", "").strip().lower()
        if _raw == "vulkan":
            self.requested_backend = "vulkan"
        elif _raw == "vtk":
            self.requested_backend = "vtk"
        else:
            self.requested_backend = "auto"  # covers "auto" explicitly AND unset/empty/anything else
        self.vulkan_available = False
        self.vulkan_initialized = False
        self.vulkan_ready = False
        self.active_backend = "vtk"
        self.active_render_mode: Optional[str] = None  # "point_cloud"/"surface"/"shaded_class"/None
        self.last_error: str = ""
        self.gpu_name: str = ""
        self.vulkan_api_version: str = ""
        self._ever_active = False  # ACTIVE is sticky once a real workload has rendered this session

        if self.requested_backend == "vtk":
            self.state = BackendState.FALLBACK
            self._log.info("Render backend: vtk (requested; set NAKSHA_RENDER_BACKEND=vulkan|auto to try Vulkan)")
            self._print_banner_fallback(
                reason="Vulkan not requested (NAKSHA_RENDER_BACKEND=vtk)", failed=False,
            )
            return

        print(f"[render_backend] NAKSHA_RENDER_BACKEND={self.requested_backend} requested - probing native engine...")
        try:
            self._try_activate_vulkan()
        except Exception as _e:
            self.last_error = repr(_e)
            print(f"[render_backend] Vulkan backend activation raised; staying on VTK: {_e!r}")
            self._log.exception("Vulkan backend activation raised; staying on VTK")
            self.active = False
            self.backend_name = "vtk"
            self.active_backend = "vtk"
            self.state = BackendState.ERROR
            self._print_banner_fallback(reason=self.last_error)

        if self.state not in (BackendState.READY, BackendState.ACTIVE):
            # _try_activate_vulkan returned without raising but without
            # reaching READY either (e.g. "auto" declined, device probe
            # failed) - make sure we still land in a real, printed FALLBACK
            # state rather than silently staying UNAVAILABLE.
            if self.state == BackendState.UNAVAILABLE:
                self._print_banner_fallback(reason=self.last_error or "Vulkan runtime/device not detected")
            self.state = BackendState.FALLBACK if self.state != BackendState.ERROR else self.state
            self.active_backend = "vtk"

    # -- terminal banners (state-change only, never per-frame) --------------
    def _print_banner_success(self, w: int, h: int) -> None:
        vb = self.vulkan_backend
        print("=" * 60)
        print("NAKSHA RENDER BACKEND")
        print("=" * 60)
        print(f"Requested backend : {self.requested_backend.capitalize()}")
        print("Native DLL        : LOADED")
        print("Vulkan runtime    : AVAILABLE")
        print(f"Vulkan device     : {self.gpu_name or 'unknown'}")
        print(f"Vulkan version    : {self.vulkan_api_version or 'unknown'}")
        print("Native renderer   : INITIALIZED")
        print("Qt/Win32 surface  : READY")
        print("Shaders           : READY")
        print("Fallback backend  : VTK/PyVista")
        print("Status            : VULKAN READY")
        print("=" * 60)

    def _print_banner_fallback(self, reason: str, failed: bool = True) -> None:
        print("=" * 60)
        print("NAKSHA RENDER BACKEND")
        print("=" * 60)
        print("Vulkan             : FAILED" if failed else "Vulkan             : NOT REQUESTED")
        print(f"Reason             : {reason or 'NAKSHA_RENDER_BACKEND is vtk (default)'}")
        print("Active renderer    : VTK/PyVista")
        print("Application status : CONTINUING NORMALLY")
        print("=" * 60)

    def _try_activate_vulkan(self) -> None:
        backend = VulkanRenderBackend()
        self.vulkan_available = backend.is_available()
        if not self.vulkan_available:
            self.last_error = "vulkan runtime/device probe failed (nkv_is_available()==0)"
            print("[render_backend] Vulkan requested but unavailable; staying on VTK")
            self._log.info("Vulkan requested but unavailable; staying on VTK")
            self.state = BackendState.UNAVAILABLE
            return
        self.state = BackendState.AVAILABLE
        print("[render_backend] Vulkan runtime AVAILABLE")

        # `Qt` comes from the module-level import at the top of this file.
        # Parent to the VTK widget's OWN real parent - app_window.py's
        # self.frame, the exact widget that has always held
        # vtk_widget.interactor and is (via self.splitter -> main_row ->
        # container, alongside top_bar/ribbon_container) properly confined to
        # the area under the ribbon/toolbar and above the status bar. Never
        # fall back to the QMainWindow itself: raising a widget there would
        # raise it above the ribbon/menu bar/toolbars/status bar too, since
        # they are all its siblings in the SAME parent's child stack.
        parent = self._vtk_widget.parentWidget()
        if parent is None:
            self.last_error = "VTK widget has no parent; refusing to parent Vulkan surface to the QMainWindow"
            self._log.warning(self.last_error)
            self.state = BackendState.ERROR
            return
        owner = self
        widget = _VulkanSurfaceWidget(parent, owner)
        widget.setAttribute(Qt.WA_NativeWindow)  # forces a real HWND to exist
        try:
            widget.winId()  # realize the native window before reading it
            hwnd = int(widget.winId())
        except Exception:
            self.last_error = "could not obtain a native HWND for the Vulkan widget"
            self._log.warning(self.last_error)
            widget.deleteLater()
            self.state = BackendState.ERROR
            return

        # Remember which HWND this renderer is bound to: Qt destroys and
        # re-creates a widget's native window whenever it is reparented, and
        # the VkSurfaceKHR does NOT follow it (see
        # _rebind_renderer_to_current_hwnd).
        self._vulkan_hwnd = hwnd
        w = max(1, self._vtk_widget.width() or 1280)
        h = max(1, self._vtk_widget.height() or 720)
        if not backend.initialize(hwnd, w, h):
            self.last_error = (
                backend._dll.nkv_last_error().decode("utf-8", errors="replace")
                if backend._dll else "nkv_create_renderer failed (no DLL bound)"
            )
            print(f"[render_backend] nkv_create_renderer failed; staying on VTK (last_error={self.last_error})")
            self._log.warning("nkv_create_renderer failed; staying on VTK")
            widget.deleteLater()
            self.state = BackendState.ERROR
            return

        # INITIALIZED: instance/device/native renderer created.
        self.vulkan_initialized = True
        self.state = BackendState.INITIALIZED
        self.gpu_name = backend.get_device_name() or "unknown"
        self.vulkan_api_version = backend.get_api_version_string() or "unknown"

        self.vulkan_backend = backend
        self.vulkan_widget = widget
        self.active = True
        self.backend_name = "vulkan"
        self.active_backend = "vulkan"
        self._log.info("Vulkan backend initialized (%dx%d) device=%s api=%s", w, h, self.gpu_name, self.vulkan_api_version)

        # Deliberately NOT added to parent.layout() here: that layout
        # (self.layout, a QVBoxLayout) already holds vtk_widget.interactor as
        # its only child, and adding a second widget to it would split
        # self.frame's space vertically between the two instead of stacking
        # them - a real bug an earlier round hit. The widget stays an
        # unmanaged, hidden, absolutely-positioned child of `parent`
        # (self.frame) instead; install_as_main_viewport() is what shows it
        # and syncs its geometry to vtk_widget.interactor's via
        # _GeometryMirror. Only its own actors are ever suppressed (see
        # set_vtk_lidar_rendering) - VTK's main render loop, overlay renderer
        # (SNT/digitizer/grid/measurements/text/vectors) and Qt's own UI
        # (ribbon/menus/toolbars/status bar) are never hidden by this.
        widget.setVisible(False)
        print("[render_backend] Vulkan widget created (hidden) as a child of the VTK "
              "widget's own parent - ribbon/menus/toolbars/status bar are separate "
              "widgets elsewhere in the window and are never covered")

        # READY: surface/swapchain/shaders/resources ready to accept a real
        # workload. NOT the same as ACTIVE - nothing has actually rendered a
        # real point cloud/surface/shaded-class through this yet.
        self.vulkan_ready = True
        self.state = BackendState.READY
        self._print_banner_success(w, h)

        # Explicit diagnostic opt-in: disable VTK's LiDAR actors independent
        # of whether Vulkan is installed as the main viewport. Scoped to the
        # same actor list set_vtk_lidar_rendering() already targets - the
        # main VTK render loop, overlays and Qt UI are never touched by this.
        if vtk_lidar_render_disable_requested():
            self.set_vtk_lidar_rendering(False)

    @property
    def is_displayed(self) -> bool:
        """True ONLY when the Vulkan widget is actually the visible/shown
        viewport the user is looking at (today: only when a preview/split
        view has explicitly made it visible - NOT just because a real
        workload succeeded internally). Distinct on purpose from
        active_backend/active_render_mode/state: those answer "did Vulkan
        do real work", this answers "is the user seeing Vulkan pixels right
        now". As of this round there is no default-viewport-swap and no
        preview panel wired up yet, so this is always False under plain
        `py main.py` regardless of requested_backend - VTK is what's on
        screen. A real Qt state, not a guess: reads vulkan_widget.isVisible()."""
        try:
            return self.vulkan_widget is not None and bool(self.vulkan_widget.isVisible())
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # STEP 1 (performance phase): stop VTK rendering the LiDAR when the
    # Vulkan surface is the visible viewport.
    #
    # Why this is needed at all: install_as_main_viewport() raises the opaque
    # Vulkan HWND over the VTK widget, so the user only SEES Vulkan - but VTK
    # keeps drawing the full point cloud and mesh underneath, every frame.
    # That is pure duplicated work: the exact cost the performance phase is
    # meant to remove.
    #
    # Scope is deliberately narrow - ONLY LiDAR actors are switched off:
    #   * the unified point cloud (+ its interaction-LOD partner)
    #   * the surface mesh
    #   * the shaded-class meshes
    # VTK KEEPS rendering every overlay: SNT, digitizer, measurements, text,
    # vector/CAD overlays, grids. Those are explicitly out of scope.
    #
    # Visibility is used rather than removal, so toggling back to VTK is exact
    # and lossless - no actor has to be rebuilt.
    # ------------------------------------------------------------------ #
    _VTK_LIDAR_ACTOR_NAMES = (
        "_naksha_unified_cloud",   # UNIFIED_ACTOR_NAME
        "_naksha_interaction_lod",
        "surface_mesh",            # surface_mode.SURFACE_ACTOR_NAME
        "shaded_mesh_live_multiclass_crisp_pure",
        "shaded_mesh_live_multiclass_color",
        "shaded_mesh_live_multiclass_tip",
    )
    # Prefixes for actors created per-class or per-live-region.
    _VTK_LIDAR_ACTOR_PREFIXES = (
        "class_", "border_", "border_layer", "color_layer",
        "shaded_mesh_static_multiclass_blend_", "shaded_mesh_live_",
        "surface_",
    )

    def _vtk_lidar_actor_list(self):
        """The VTK actors that draw LiDAR (point cloud / surface / shaded mesh).

        Collected defensively: an actor that has already been removed, or whose
        mapper is gone, must not be able to break the switch.
        """
        out = []
        try:
            vtk_widget = self._vtk_widget
            if vtk_widget is None:
                return out
            actors = getattr(vtk_widget, "actors", None)
            if not isinstance(actors, dict):
                return out
            for name, actor in list(actors.items()):
                n = str(name)
                if actor is None:
                    continue
                if (n in self._VTK_LIDAR_ACTOR_NAMES
                        or n.startswith(self._VTK_LIDAR_ACTOR_PREFIXES)):
                    out.append((n, actor))
            # Actors held on the widget rather than in the actors dict.
            for attr in ("_unified_actor", "_naksha_full_detail_actor",
                         "_naksha_interaction_actor"):
                a = getattr(vtk_widget, attr, None)
                if a is not None and all(a is not existing for _, existing in out):
                    out.append((attr, a))
        except Exception:
            self._log.exception("VTK LiDAR actor enumeration failed (ignored)")
        return out

    def set_vtk_lidar_rendering(self, enabled: bool) -> bool:
        """Enable/disable VTK's LiDAR actors, leaving all overlays alone.

        Returns True when the requested state is now in effect. Safe to call
        repeatedly (it is called after every load/mode change, because a new
        actor can appear at any time).
        """
        actors = self._vtk_lidar_actor_list()
        want = bool(enabled)
        applied = 0
        for _name, actor in actors:
            try:
                if bool(actor.GetVisibility()) != want:
                    actor.SetVisibility(1 if want else 0)
                applied += 1
            except Exception:
                self._log.debug("actor visibility toggle failed", exc_info=True)
        self._vtk_lidar_enabled = want
        if want != getattr(self, "_vtk_lidar_last_logged", None):
            self._vtk_lidar_last_logged = want
            print(f"[render_backend] VTK LiDAR rendering "
                  f"{'ON' if want else 'OFF'} ({applied} actors) - "
                  f"overlays (SNT/digitizer/measurements/text/vectors) unaffected")
        return True

    def vtk_lidar_rendering_enabled(self) -> bool:
        return bool(getattr(self, "_vtk_lidar_enabled", True))

    # Names of the VTK actor GROUPS, reported separately in the ownership block
    # so "point actors" and "surface actors" can be told apart.
    _VTK_POINT_ACTOR_NAMES = ("_naksha_unified_cloud", "_naksha_interaction_lod",
                              "color_layer", "border_layer")
    _VTK_POINT_ACTOR_PREFIXES = ("class_", "border_")
    _VTK_SURFACE_ACTOR_NAMES = ("surface_mesh",
                               "shaded_mesh_live_multiclass_crisp_pure",
                               "shaded_mesh_live_multiclass_color",
                               "shaded_mesh_live_multiclass_tip")
    _VTK_SURFACE_ACTOR_PREFIXES = ("surface_", "shaded_mesh_static_multiclass_blend_",
                                   "shaded_mesh_live_")

    def _group_visibility(self):
        """(point_on, surface_on, n_point, n_surface) for the VTK LiDAR actors.

        Read live from each actor rather than trusting the cached flag, so the
        ownership report reflects what VTK would actually draw.
        """
        n_pt = n_sf = 0
        pt_on = sf_on = 0
        pnames = set(self._VTK_POINT_ACTOR_NAMES)
        ppre = tuple(self._VTK_POINT_ACTOR_PREFIXES)
        snames = set(self._VTK_SURFACE_ACTOR_NAMES)
        spre = tuple(self._VTK_SURFACE_ACTOR_PREFIXES)
        for name, actor in self._vtk_lidar_actor_list():
            n = str(name)
            vis = _safe_actor_vis(actor)
            if n in pnames or n.startswith(ppre):
                n_pt += 1
                pt_on += int(vis)
            elif n in snames or n.startswith(spre):
                n_sf += 1
                sf_on += int(vis)
        return pt_on > 0, sf_on > 0, n_pt, n_sf

    def ownership_report(self) -> str:
        """[VULKAN OWNERSHIP] - who is actually drawing the LiDAR right now."""
        b = self.vulkan_backend
        lines = ["[VULKAN OWNERSHIP]"]
        viewport_active = False
        if b is not None and self.active:
            try:
                viewport_active = self.vulkan_viewport_is_visible()
            except Exception:
                viewport_active = False
        lines.append(f"  Vulkan viewport:        "
                     f"{'ACTIVE' if viewport_active else 'INACTIVE'}")
        try:
            pt_on, sf_on, n_pt, n_sf = self._group_visibility()
        except Exception:
            pt_on = sf_on = False
            n_pt = n_sf = 0
        try:
            n_points = int(b.get_point_count()) if b is not None else 0
        except Exception:
            n_points = 0
        try:
            uploads = int(b.get_point_position_upload_count()) if b is not None else 0
        except Exception:
            uploads = 0
        lines.append(f"  Vulkan point renderer:  "
                     f"{'ON' if n_points > 0 else 'OFF'}  ({n_points:,} points resident)")
        lines.append(f"  VTK point actors:       "
                     f"{'ON' if pt_on else 'OFF'}  ({n_pt} actor(s))")
        lines.append(f"  VTK surface actors:     "
                     f"{'ON' if sf_on else 'OFF'}  ({n_sf} actor(s))")
        lines.append(f"  OpenGL LiDAR uploads:   {uploads:,} position upload(s) via Vulkan; "
                     f"VTK LiDAR actor count = {n_pt + n_sf}")
        lines.append(f"  VTK overlays:           "
                     f"UNTOUCHED (SNT/digitizer/measurements/text/vectors/CAD)")
        return "\n".join(lines)

    def print_ownership_report(self) -> None:
        try:
            print(self.ownership_report(), flush=True)
        except Exception:
            self._log.exception("ownership report failed (ignored)")

    def vulkan_ownership_report(self) -> str:
        """Short [VULKAN OWNERSHIP] block: who owns what right now.

        Vulkan owns ONLY LiDAR content (point cloud / surface / shaded
        class). The VTK main render loop, its overlay renderer and Qt's own
        UI rendering are never disabled by Vulkan ownership - this block is
        the quick proof of that, complementing the fuller ownership_report()
        above.
        """
        vulkan_active = bool(self.active and self.vulkan_viewport_is_visible())
        try:
            pt_on, sf_on, n_pt, n_sf = self._group_visibility()
            lidar_actors_on = (n_pt if pt_on else 0) + (n_sf if sf_on else 0)
        except Exception:
            lidar_actors_on = 0

        app = self._app
        widget = getattr(app, "vtk_widget", None) if app is not None else None
        ren = getattr(widget, "renderer", None) if widget is not None else None
        overlays_active = False
        try:
            overlays_active = ren is not None and bool(ren.GetActors2D().GetNumberOfItems() > 0)
        except Exception:
            overlays_active = False

        render_loop_active = False
        try:
            rw = widget.GetRenderWindow() if widget is not None and hasattr(widget, "GetRenderWindow") else None
            render_loop_active = rw is not None
        except Exception:
            render_loop_active = False

        return "\n".join([
            "[VULKAN OWNERSHIP]",
            "",
            "Vulkan:",
            "ACTIVE" if vulkan_active else "INACTIVE",
            "",
            "VTK LiDAR actors:",
            str(lidar_actors_on),
            "",
            "VTK overlays:",
            "ACTIVE" if overlays_active else "INACTIVE",
            "",
            "VTK render loop:",
            "ACTIVE" if render_loop_active else "INACTIVE",
        ])

    def print_vulkan_ownership_report(self) -> None:
        try:
            print(self.vulkan_ownership_report(), flush=True)
        except Exception:
            self._log.exception("vulkan ownership report failed (ignored)")

    def _reassert_vtk_lidar_state(self) -> None:
        """Re-apply the VTK LiDAR on/off state after actors are (re)built.

        LiDAR actors are created by the loaders, not by the backend, so a new
        cloud or a new display mode brings new VTK actors with default
        visibility. Whenever Vulkan is the visible LiDAR renderer they must be
        switched off again, or VTK quietly starts drawing the cloud a second
        time underneath the opaque Vulkan surface.

        Deferred AND retried: the loaders build actors on a worker thread and
        rebuild the actor dict while this runs, so enumerating + touching every
        actor synchronously from inside upload_point_cloud() raced that and
        could take the process down. A single singleShot(0) was not enough
        either - it lands BEFORE the loaders have created the actors, so it
        toggles 0 actors and the freshly built cloud stays visible. This
        retries on the Qt thread until it has actually seen (and switched off)
        a non-empty actor set, or the retry budget runs out.
        """
        try:
            if not (self.active and self.vulkan_backend is not None):
                return
            if not self.vulkan_viewport_is_visible():
                return
            self._vtk_reassert_attempts = 0
            self._schedule_vtk_lidar_reassert()
        except Exception:
            self._log.debug("VTK LiDAR re-assert failed (ignored)",
                            exc_info=True)

        # [VULKAN FRAME] presented-frame telemetry. The engine's own
        # nkv_last_frame_ms returns 0, so the cadence a user actually feels is
        # measured here on the Python side instead.
        self._frame_timing = {"frames": 0, "sum_ms": 0.0, "sum_cpu_ms": 0.0}
        # Back-reference so the low-level VulkanRenderBackend can feed
        # presented-frame timings to [VULKAN FRAME] without owning a strong
        # ref to the owner (which would create a cycle).
        try:
            self.vulkan_backend._owner_ref = weakref.ref(self)
        except Exception:
            pass

    # How long to keep re-asserting after a load. The loaders create the VTK
    # actors asynchronously, and on a large file the last one can appear
    # several seconds in; a single pass keeps missing it.
    _VTK_REASSERT_MAX_ATTEMPTS = 40
    _VTK_REASSERT_DELAY_MS = 250

    def _schedule_vtk_lidar_reassert(self) -> None:
        QTimer.singleShot(self._VTK_REASSERT_DELAY_MS, self._apply_vtk_lidar_state_safe)

    def _apply_vtk_lidar_state_safe(self) -> None:
        """One Qt-thread pass: switch VTK LiDAR off, and keep retrying while
        the loaders are still creating actors (measured by seeing none yet)."""
        try:
            if not (self.active and self.vulkan_viewport_is_visible()):
                return
            had_actors = len(self._vtk_lidar_actor_list()) > 0
            self.set_vtk_lidar_rendering(False)
            attempts = int(getattr(self, "_vtk_reassert_attempts", 0)) + 1
            self._vtk_reassert_attempts = attempts
            if not had_actors and attempts < self._VTK_REASSERT_MAX_ATTEMPTS:
                # Actors not up yet - the load is still in flight.
                self._schedule_vtk_lidar_reassert()
        except Exception:
            self._log.debug("deferred VTK LiDAR apply failed (ignored)",
                            exc_info=True)

    # ---- Surface interaction state (Parts 5 and 8) -------------------------
    #
    # While the camera is moving the engine draws the interaction/coarse
    # surface level; this is the hard guarantee that an uncapped SLOW surface
    # (~53.8M triangles) is never the geometry being navigated. When the user
    # stops, the engine refines to the requested final quality by itself after
    # the idle debounce - this timer only makes the transition deterministic and
    # lets the reports observe it.
    _SURFACE_IDLE_MS = 400

    def _note_surface_interaction(self, moving: bool) -> None:
        b = getattr(self, "vulkan_backend", None)
        if b is None or not getattr(self, "active", False):
            return
        try:
            b.set_interacting(bool(moving), float(self._SURFACE_IDLE_MS))
        except Exception:
            self._log.debug("surface interaction flag failed (ignored)", exc_info=True)
            return
        try:
            if moving:
                t = getattr(self, "_surface_idle_timer", None)
                if t is None:
                    from PySide6.QtCore import QTimer
                    t = QTimer()
                    t.setSingleShot(True)
                    t.timeout.connect(self._note_surface_interaction_settled)
                    self._surface_idle_timer = t
                t.start(int(self._SURFACE_IDLE_MS))
        except Exception:
            self._log.debug("surface idle timer arm failed (ignored)", exc_info=True)

    def _note_surface_interaction_settled(self) -> None:
        b = getattr(self, "vulkan_backend", None)
        if b is None or not getattr(self, "active", False):
            return
        try:
            b.set_interacting(False, float(self._SURFACE_IDLE_MS))
        except Exception:
            self._log.debug("surface settle flag failed (ignored)", exc_info=True)

    # ---- [SURFACE CULLING] / [SURFACE LOD] / [SURFACE MEMORY] -------------
    # Pure reports over the engine's own counters - no estimation, no guessing.
    def surface_culling_report(self) -> str:
        b = getattr(self, "vulkan_backend", None)
        if b is None or not getattr(self, "active", False):
            return "[SURFACE CULLING]\n  backend: not active"
        c = b.get_surface_culling()
        if c.get("total_tiles", -1) < 0:
            return ("[SURFACE CULLING]\n  native tiled surface path not available "
                    "in this DLL")
        n = lambda v: f"{v:,}" if v >= 0 else "n/a"
        return "\n".join([
            "[SURFACE CULLING]", "",
            "Total tiles:", f"  {n(c['total_tiles'])}", "",
            "Visible tiles:", f"  {n(c['visible_tiles'])}", "",
            "Culled tiles:", f"  {n(c['culled_tiles'])}", "",
            "Total triangles:", f"  {n(c['total_triangles'])}", "",
            "Submitted triangles:", f"  {n(c['visible_triangles'])}", "",
            "Culled triangles:", f"  {n(c['culled_triangles'])}", "",
        ])

    def surface_lod_report(self) -> str:
        b = getattr(self, "vulkan_backend", None)
        if b is None or not getattr(self, "active", False):
            return "[SURFACE LOD]\n  backend: not active"
        d = b.get_surface_lod()
        if d.get("lod", -1) < 0:
            return "[SURFACE LOD]\n  native LOD path not available in this DLL"
        names = {0: "LOD0 (final)", 1: "LOD1 (medium)", 2: "LOD2 (interaction/coarse)"}
        counts = d.get("counts", [-1, -1, -1])
        lines = ["[SURFACE LOD]", "",
                 "State:", f"  {'MOVING' if d.get('moving') else 'IDLE'}", "",
                 "Selected LOD:", f"  {names.get(d.get('lod'), d.get('lod'))}", "",
                 "Resident triangles per level:"]
        for i, c in enumerate(counts):
            lines.append(f"  {names.get(i, i)}: {c:,}" if c >= 0 else f"  {i}: n/a")
        try:
            gms = b.get_gpu_frame_time_ms()
            lines += ["", f"GPU frame: {gms:,.2f} ms" if gms > 0
                      else "GPU frame: not available"]
        except Exception:
            pass
        return "\n".join(lines)

    def surface_memory_report(self) -> str:
        b = getattr(self, "vulkan_backend", None)
        if b is None or not getattr(self, "active", False):
            return "[SURFACE MEMORY]\n  backend: not active"
        m = b.get_surface_memory()
        if m.get("budget", -1) < 0:
            return "[SURFACE MEMORY]\n  native memory report not available in this DLL"
        MB = 1048576.0
        f = lambda v: f"{v / MB:,.1f} MB" if v >= 0 else "n/a"
        sw = b.get_surface_swaps()
        return "\n".join([
            "[SURFACE MEMORY]", "",
            f"Budget (70% of VRAM): {f(m['budget'])}", "",
            f"Active:   {f(m['active'])}",
            f"Pending:  {f(m['pending'])}",
            f"LOD cache:{f(m['lod_cache'])}", "",
            f"Peak during swap: {f(m['peak_during_swap'])}", "",
            f"Atomic swaps: {sw.get('swaps')}",
            f"Stale discards: {sw.get('stale_discards')}",
            f"Last upload refused by budget: {'YES' if m.get('rejected') else 'NO'}",
        ])

    def surface_state_report(self) -> str:
        b = getattr(self, "vulkan_backend", None)
        if b is None or not getattr(self, "active", False):
            return "[SURFACE STATE]\n  backend: not active"
        names = {0: "EMPTY", 1: "PREVIEW_ACTIVE", 2: "FINAL_PENDING", 3: "FINAL_ACTIVE"}
        s = b.get_surface_state()
        return f"[SURFACE STATE]\n  State: {names.get(s, s)}"

    def vulkan_viewport_is_visible(self) -> bool:
        """True when the Vulkan surface is the visible viewport."""
        try:
            return (self.vulkan_widget is not None
                    and bool(self.vulkan_widget.isVisible()))
        except Exception:
            return False

    def vulkan_owns_lidar_viewport(self) -> bool:
        """Single source of truth: is Vulkan the ONLY LiDAR renderer now?

        True only when BOTH hold:
          1. the Vulkan main viewport was actually installed (the env flag
             alone is not enough - the install can be skipped when the UI
             never settles), and
          2. the Vulkan surface is the visible viewport.

        When this is True, VTK must not build LiDAR actors at all: the point
        cloud / classification / intensity / elevation / shaded-class /
        surface geometry is already resident in Vulkan's own buffers, so a
        second copy in VTK is pure duplicated CPU work and duplicated GPU
        memory for pixels nobody sees.

        Overlays (SNT, digitizer, measurements, text, vectors, CAD) are NOT
        LiDAR and are deliberately unaffected by this flag.
        """
        try:
            if not (self.active and self.vulkan_backend is not None):
                return False
            if not bool(getattr(self, "_viewport_installed", False)):
                return False
            return self.vulkan_viewport_is_visible()
        except Exception:
            return False

    def _log_vulkan_lidar_ownership(self) -> None:
        """[VULKAN OWNERSHIP] - who is actually rendering LiDAR right now.

        Counts the VTK LiDAR actors that actually exist and are visible, and
        whether any VTK LiDAR mapper is holding GPU buffers, so "0" is a
        measured fact rather than an assumption.
        """
        owns = self.vulkan_owns_lidar_viewport()
        try:
            n_pt_on, n_sf_on, n_pt, n_sf = self._group_visibility()
        except Exception:
            n_pt_on = n_sf_on = n_pt = n_sf = 0
        vtk_lidar_total = int(n_pt) + int(n_sf)
        vtk_lidar_on = int(n_pt_on) + int(n_sf_on)
        # A mapper with a non-empty input is holding real GL buffers.
        try:
            vtk_gl_buffers = len(self._vtk_lidar_actor_list())
        except Exception:
            vtk_gl_buffers = 0
        overlays = "ACTIVE" if self._vtk_overlays_present() else "INACTIVE"
        print("\n".join([
            "[VULKAN OWNERSHIP]",
            "",
            "LiDAR renderer:",
            "Vulkan" if owns else "VTK",
            "",
            "VTK point actors:",
            f"{int(n_pt_on)}",
            "",
            "VTK surface actors:",
            f"{int(n_sf_on)}",
            "",
            "VTK LiDAR OpenGL buffers:",
            f"{vtk_gl_buffers if not owns else 0}",
            "",
            "VTK overlays:",
            overlays,
        ]))
        return vtk_lidar_total, vtk_lidar_on

    def _vtk_overlays_present(self) -> bool:
        """True when VTK still holds 2D overlay content (SNT/text/vector/
        digitizer/measurements). Those must keep working under Vulkan."""
        try:
            widget = self._vtk_widget
            ren = getattr(widget, "renderer", None) if widget is not None else None
            if ren is None:
                return False
            return bool(ren.GetActors2D().GetNumberOfItems() > 0)
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # STEP 4 (performance phase): the [VULKAN PERFORMANCE] block.
    #
    # Every number here is read from the engine's own counters - nothing is
    # estimated. The upload counters are the important ones for STEP 3: they
    # must NOT advance while the camera moves, because that would mean the point
    # and surface buffers are being re-sent to the GPU on every pan/zoom.
    # ------------------------------------------------------------------ #
    def performance_report(self) -> str:
        """The [VULKAN PERFORMANCE] block, built from live engine counters."""
        b = self.vulkan_backend
        lines = ["[VULKAN PERFORMANCE]"]
        if b is None or not self.active:
            lines.append("  backend: not active")
            return "\n".join(lines)
        try:
            n_points = int(b.get_point_count())
        except Exception:
            n_points = -1
        try:
            pos_up = int(b.get_point_position_upload_count())
        except Exception:
            pos_up = -1
        try:
            surf_up = int(b.get_surface_upload_count())
        except Exception:
            surf_up = -1
        try:
            lut_up = int(b.get_lut_update_count())
        except Exception:
            lut_up = -1
        try:
            pdraw = int(b.get_point_draw_call_count())
        except Exception:
            pdraw = -1
        try:
            sdraw = int(b.get_surface_draw_call_count())
        except Exception:
            sdraw = -1
        try:
            vram = int(b.get_vram_bytes())
        except Exception:
            vram = -1
        try:
            rendered, skipped, recreates = b.get_frame_stats()
        except Exception:
            rendered = skipped = recreates = -1
        try:
            frame_ms = float(b.get_last_frame_ms())
        except Exception:
            frame_ms = float("nan")
        fps = (1000.0 / frame_ms) if frame_ms and frame_ms > 0 else float("nan")

        lines.append(f"  Point buffer upload:  {pos_up:,} x  "
                     f"({n_points:,} points resident)")
        lines.append(f"  Surface buffer upload: {surf_up:,} x  "
                     f"({sdraw:,} surface draw calls)")
        lines.append(f"  Colour LUT uploads:  {lut_up:,} x")
        lines.append(f"  GPU memory: {vram/1048576.0:,.1f} MiB")
        lines.append(f"  Draw calls: point={pdraw:,} surface={sdraw:,}")
        lines.append(f"  Frame time: {frame_ms:.2f} ms")
        lines.append(f"  FPS: {fps:.1f}")
        lines.append(f"  Frames: rendered={int(rendered):,} skipped={int(skipped):,} "
                     f"swapchain_rebuilds={int(recreates):,}")
        lines.append(f"  VTK LiDAR rendering: "
                     f"{'ON' if self.vtk_lidar_rendering_enabled() else 'OFF'}")
        # PERFORMANCE PHASE 1: prove the duplicate VTK LiDAR pipeline is gone.
        try:
            from gui.unified_actor_manager import vtk_lidar_build_counters
            c = vtk_lidar_build_counters()
        except Exception:
            c = {"built": -1, "skipped": -1, "skipped_points": -1}
        lines.append(f"  Point uploads: {pos_up:,}   "
                     f"Surface uploads: {surf_up:,}")
        lines.append(f"  VTK LiDAR creation: "
                     f"{'NO (skipped)' if c['skipped'] else 'YES'}"
                     f"  built={c['built']} skipped={c['skipped']} "
                     f"skipped_points={c['skipped_points']:,}")
        lines.append(f"  GPU memory: Vulkan={vram/1048576.0:,.1f} MiB   "
                     f"VTK=0.0 MiB (no LiDAR actors)")
        lines.append(f"  Draw calls: point={pdraw:,} surface={sdraw:,}")
        lines.append(f"  LiDAR renderer: "
                     f"{'Vulkan' if self.vulkan_owns_lidar_viewport() else 'VTK'}")
        # CPU/GPU split. The engine measures CPU submit time
        # (nkv_last_frame_ms -> LastCpuFrameMs); there is no GPU timestamp
        # query yet, so GPU frame time is reported as unavailable rather
        # than guessed from the same number.
        lines.append(f"  CPU frame time: {frame_ms:.2f} ms")
        lines.append("  GPU frame time: not measured "
                     "(needs a VK_QUERY_TYPE_TIMESTAMP pool)")
        try:
            ov_t = int(b.get_overlay_triangle_count())
            so_d = int(b.get_surface_overlay_draw_call_count())
        except Exception:
            ov_t = so_d = -1
        lines.append(f"  Draw calls: point={pdraw:,} surface={sdraw:,} "
                     f"overlay_tris={ov_t:,} surface_overlay={so_d:,}")
        return "\n".join(lines)

    def gpu_memory_report(self) -> str:
        """[VULKAN GPU MEMORY] - how the VRAM is actually being spent.

        Every element count below is MEASURED from the renderer
        (nkv_get_point_count / nkv_get_surface_vertex_count /
        nkv_get_surface_index_count) and multiplied by the vertex/attribute
        formats the shaders actually bind:
          point     21 B/pt  = pos 3xf32(12) + rgba8(4) + class u8(1) +
                               intensity f32(4)
          surface   28 B/vtx = pos 3xf32(12) + normal 3xf32(12) + rgba8(4)
          index      4 B/idx = uint32
        TOTAL is the engine's own device-local allocation report
        (nkv_get_vram_bytes) and is therefore the authoritative figure; the
        per-buffer lines are a breakdown of it, and the sum can be lower
        than TOTAL because the total also covers swapchain images, the frame
        UBO (which carries the class/elevation/intensity LUTs) and
        descriptors.

        A -1 count means the loaded DLL predates the Phase 2 accessors.
        """
        b = self.vulkan_backend
        if b is None or not self.active:
            return "[VULKAN GPU MEMORY]\n  backend: not active"
        try:
            total = int(b.get_vram_bytes())
        except Exception:
            total = -1

        def _n(fn):
            try:
                return int(getattr(b, fn)())
            except Exception:
                return -1

        n_pts = _n("get_point_count")
        n_vtx = _n("get_surface_vertex_count")
        n_idx = _n("get_surface_index_count")
        POINT_STRIDE = 3 * 4 + 4 + 1 + 4          # 21 B
        SURFACE_VERTEX_STRIDE = 3 * 4 + 3 * 4 + 4  # 28 B
        INDEX_STRIDE = 4
        MB = 1048576.0

        def _mb(v, stride):
            return f"{(v * stride) / MB:,.1f} MB" if v >= 0 else "not available"

        return "\n".join([
            "[VULKAN GPU MEMORY]",
            "",
            "Point buffer:",
            f"  {_mb(n_pts, POINT_STRIDE)}   "
            f"({n_pts:,} pts x {POINT_STRIDE} B)",
            "",
            "Surface buffer:",
            f"  {_mb(n_vtx, SURFACE_VERTEX_STRIDE)}   "
            f"({n_vtx:,} vtx x {SURFACE_VERTEX_STRIDE} B)",
            "",
            "Index buffer:",
            f"  {_mb(n_idx, INDEX_STRIDE)}   "
            f"({n_idx:,} idx x {INDEX_STRIDE} B)",
            "",
            "LUT:",
            "  part of the per-frame UBO (not a separate buffer); "
            "included in Total",
            "",
            "Total:",
            f"  {(total / MB) if total >= 0 else float('nan'):,.1f} MB   "
            f"(nkv_get_vram_bytes - includes swapchain + UBO + descriptors)",
        ])

    def gpu_persistence_report(self) -> str:
        """[VULKAN GPU PERSISTENCE] - which uploads happened, and whether the
        geometry has stayed resident.

        Point / surface / index counts must stay FLAT across camera movement
        and display changes: that is the proof the buffers are persistent.
        LUT and uniform counts are the small updates that display changes
        are *supposed* to produce.
        """
        b = self.vulkan_backend
        if b is None or not self.active:
            return "[VULKAN GPU PERSISTENCE]\n  backend: not active"

        def _c(fn):
            try:
                return int(getattr(b, fn)())
            except Exception:
                return -1

        pos = _c("get_point_position_upload_count")
        col = _c("get_point_color_upload_count")
        cls = _c("get_point_classification_upload_count")
        inten = _c("get_point_intensity_upload_count")
        surf = _c("get_surface_upload_count")
        lut = _c("get_lut_update_count")
        shade = _c("get_shade_param_update_count")
        parity = _c("get_parity_param_update_count")
        uniform = max(0, shade) + max(0, parity)
        try:
            n_vtx = int(b.get_surface_vertex_count())
            n_idx = int(b.get_surface_index_count())
        except Exception:
            n_vtx = n_idx = -1
        return "\n".join([
            "[VULKAN GPU PERSISTENCE]",
            "",
            "Point uploads:",
            f"  {pos:,}   (position - must be 1 and stay 1)",
            "",
            "Surface uploads:",
            f"  {surf:,}   (vertex + index together, one UploadMesh call)",
            "",
            "Index uploads:",
            f"  {surf:,}   (indices are uploaded with the vertices, so they "
            f"share the counter)",
            "",
            "LUT uploads:",
            f"  {lut:,}   (class / elevation / intensity colour maps)",
            "",
            "Uniform updates:",
            f"  {uniform:,}   (shade={shade:,} + parity={parity:,})",
            "",
            f"Point attribute uploads: colour={col:,} class={cls:,} "
            f"intensity={inten:,}",
            f"Resident surface: {n_vtx:,} vtx / {n_idx:,} idx",
        ])

    def upload_counters_report(self) -> str:
        """[VULKAN UPLOAD COUNTERS] - proof that camera and display changes do
        NOT re-upload geometry.

        Position/surface counts must stay flat while panning, zooming and
        changing shading. The LUT and parameter counters are allowed to move:
        those are the small uniform/LUT updates that shading changes are
        supposed to produce.
        """
        b = self.vulkan_backend
        if b is None or not self.active:
            return "[VULKAN UPLOAD COUNTERS]\n  backend: not active"

        def _c(fn):
            try:
                return int(getattr(b, fn)())
            except Exception:
                return -1

        pos = _c("get_point_position_upload_count")
        inten = _c("get_point_intensity_upload_count")
        surf = _c("get_surface_upload_count")
        lut = _c("get_lut_update_count")
        shade = _c("get_shade_param_update_count")
        parity = _c("get_parity_param_update_count")
        # Uniform/parameter updates are the two push-constant style channels
        # the engine already counts separately from geometry uploads.
        uniform = (shade if shade >= 0 else 0) + (parity if parity >= 0 else 0)
        return "\n".join([
            "[VULKAN UPLOAD COUNTERS]",
            "",
            "Point uploads:",
            f"{pos:,}  (geometry - must stay flat across camera moves)",
            "",
            "Surface uploads:",
            f"{surf:,}  (geometry - must stay flat across camera moves)",
            "",
            "LUT uploads:",
            f"{lut:,}  (colour mapping - expected to move on display change)",
            "",
            "Uniform updates:",
            f"{uniform:,}  (shade={shade:,} + parity={parity:,})",
            "",
            f"Point intensity uploads: {inten:,}",
        ])

    def frame_report(self) -> str:
        """[VULKAN FRAME] - wall-clock frame telemetry measured in Python.

        The engine's own nkv_last_frame_ms currently returns 0 (so
        [VULKAN PERFORMANCE] prints "FPS: nan"). This block therefore
        measures the PRESENTED frame cadence on the Python side instead:
        the interval between consecutive request_render() calls that the
        event loop actually serviced, which is the number a user feels.

        Honest limits:
          * CPU update time is the time spent inside our own request_render
            (camera push + submit), not the whole engine frame.
          * GPU time is not measured (needs a VK_QUERY_TYPE_TIMESTAMP pool).
          * These are request-paced samples, not a synthetic GPU benchmark.
        """
        b = self.vulkan_backend
        if b is None or not self.active:
            return "[VULKAN FRAME]\n  backend: not active"
        f = self._frame_timing
        n = int(f.get("frames", 0))
        if n < 2:
            return ("[VULKAN FRAME]\n  frames: 0\n"
                    "  (no frames presented yet - pan/zoom the view first)")
        avg_ms = float(f.get("sum_ms", 0.0)) / n
        fps = 1000.0 / avg_ms if avg_ms > 0 else float("nan")
        cpu_avg = float(f.get("sum_cpu_ms", 0.0)) / n
        try:
            pdraw = int(b.get_point_draw_call_count())
            sdraw = int(b.get_surface_draw_call_count())
        except Exception:
            pdraw = sdraw = -1
        return "\n".join([
            "[VULKAN FRAME]",
            "",
            "FPS:",
            f"  {fps:,.1f}",
            "",
            "Frame time:",
            f"  {avg_ms:,.2f} ms  (mean over {n:,} presented frames)",
            "",
            "CPU update time:",
            f"  {cpu_avg:,.2f} ms  (our request_render: camera push + submit)",
            "",
            "GPU update time:",
            "  not measured (needs a VK_QUERY_TYPE_TIMESTAMP pool)",
            "",
            "Point draw:",
            f"  {pdraw:,}",
            "",
            "Surface draw:",
            f"  {sdraw:,}",
        ])

    def note_frame(self, wall_ms: float, cpu_ms: float) -> None:
        """Record one presented frame for [VULKAN FRAME]."""
        f = self._frame_timing
        f["frames"] = int(f.get("frames", 0)) + 1
        f["sum_ms"] = float(f.get("sum_ms", 0.0)) + float(wall_ms)
        f["sum_cpu_ms"] = float(f.get("sum_cpu_ms", 0.0)) + float(cpu_ms)
        f["last_wall"] = float(wall_ms)

    def frame_timing_report(self) -> str:
        """[VULKAN FRAME TIMING] - real GPU vs CPU split.

        GPU render / point / surface come from the VK_QUERY_TYPE_TIMESTAMP
        pool inside the engine, corrected by the device timestampPeriod. CPU
        submit and present are wall-clock around the individual driver calls.
        FPS is derived from the presented-frame cadence ([VULKAN FRAME]),
        never from the GPU number.

        When `valid` is 0 no full frame of timestamps has been read back yet
        (the read happens at the start of the NEXT frame), so the GPU fields
        honestly say so rather than printing 0.00 as if it were a measurement.
        """
        b = self.vulkan_backend
        if b is None or not self.active:
            return "[VULKAN FRAME TIMING]\n  backend: not active"
        t = b.get_frame_timing()
        f = self._frame_timing
        n = int(f.get("frames", 0))
        if n >= 2:
            avg = float(f.get("sum_ms", 0.0)) / n
            fps = 1000.0 / avg if avg > 0 else float("nan")
        else:
            fps = float("nan")
        if t["valid"]:
            gpu = (f"{t['gpuRenderMs']:,.2f} ms   (timestamp query pool)"
                   f"\n    point pass:  {t['pointGpuMs']:,.2f} ms"
                   f"\n    surface pass: {t['surfaceGpuMs']:,.2f} ms")
        else:
            gpu = "not available yet (no full frame of timestamps read back)"
        # Never print nan: a missing sample is reported as such, and a
        # genuinely-zero GPU time is printed as 0.00 (a real measurement).
        fps_txt = (f"{fps:,.1f}   (presented-frame cadence over {n:,} frames)"
                   if fps == fps and n >= 2
                   else "not available yet (fewer than 2 presented frames)")
        return "\n".join([
            "[VULKAN FRAME]",
            "",
            "CPU submit:",
            f"  {t['cpuSubmitMs']:,.3f} ms   (inside vkQueueSubmit)",
            "",
            "GPU render:",
            f"  {gpu}",
            "",
            "Present:",
            f"  {t['presentMs']:,.3f} ms   (inside vkQueuePresentKHR)",
            "",
            "FPS:",
            f"  {fps_txt}",
        ])

    def interaction_report(self) -> str:
        """[VULKAN INTERACTION] - what a camera move actually costs.

        The "Geometry rebuild" row is not a claim, it is read from the live
        upload counters: if any geometry had been re-uploaded during the
        interaction, the point/surface upload counts would have advanced past
        the single load-time upload and this would print YES.
        """
        b = self.vulkan_backend
        if b is None or not self.active:
            return "[VULKAN INTERACTION]\n  backend: not active"

        def _c(fn, d=0):
            try:
                return int(getattr(b, fn)())
            except Exception:
                return d

        pos = _c("get_point_position_upload_count", -1)
        surf = _c("get_surface_upload_count", -1)
        t = b.get_frame_timing()
        cpu = t.get("cpuSubmitMs", 0.0)
        n = int(getattr(self, "_frame_timing", {}).get("frames", 0))
        cpu_avg = (float(self._frame_timing.get("sum_cpu_ms", 0.0)) / n
                   if n else 0.0)
        # One load-time upload is the healthy steady state. Anything above that
        # means navigation re-uploaded geometry.
        rebuilt = "NO" if (pos in (0, 1) and surf in (0, 1)) else "YES"
        return "\n".join([
            "[VULKAN INTERACTION]",
            "",
            "Camera update:",
            f"  {cpu_avg:,.3f} ms   (mean per presented frame, CPU side)",
            f"  last frame: {cpu:,.3f} ms inside vkQueueSubmit",
            "",
            "Geometry rebuild:",
            f"  {rebuilt}",
            "",
            f"Point uploads: {pos:,}   Surface uploads: {surf:,}"
            f"   (1 each = loaded once, never re-uploaded)",
        ])

    @staticmethod
    def vulkan_lod_enabled() -> bool:
        """NAKSHA_VULKAN_LOD=1 opts into the screen-space LOD draw path.

        DEFAULT IS OFF. With it off the renderer issues exactly the single
        full-buffer vkCmdDraw it always did - no behaviour change at all.
        """
        import os
        return os.environ.get("NAKSHA_VULKAN_LOD", "").strip().lower() \
            in ("1", "true", "yes", "on")

    def lod_runtime_report(self) -> str:
        """[VULKAN LOD RUNTIME] - what the LOD draw path is currently doing."""
        b = self.vulkan_backend
        if b is None or not self.active:
            return "[VULKAN LOD RUNTIME]\n  backend: not active"
        enabled = self.vulkan_lod_enabled()
        try:
            n_ranges = int(b.get_point_draw_range_count())
            n_pts = int(b.get_point_draw_range_points())
        except Exception:
            n_ranges = n_pts = 0
        try:
            total = int(b.get_point_count())
        except Exception:
            total = 0
        try:
            gms = float(b.get_gpu_frame_time_ms())
        except Exception:
            gms = -1.0
        red = (100.0 * (1.0 - n_pts / total)) if (enabled and total > 0
                                                 and n_pts > 0) else 0.0
        def _c(fn):
            try:
                return int(getattr(b, fn)())
            except Exception:
                return -1
        return "\n".join([
            "[VULKAN LOD RUNTIME]",
            "",
            "Enabled:",
            "YES" if enabled else "NO",
            "",
            "Total points:",
            f"  {total:,}",
            "",
            "Visible points:",
            f"  {n_pts:,}" if enabled and n_ranges > 0
            else "  all (single full-buffer draw)",
            "",
            "Visible cells / draw ranges:",
            f"  {n_ranges:,}",
            "",
            "Reduction:",
            # 0 ranges means the fallback single full-buffer draw is in effect,
            # i.e. 0% reduction - NOT 100%. Reporting that as 100% would claim
            # an empty screen.
            (f"  {red:,.2f}%" if enabled and n_ranges > 0
             else "  0.00% (full-buffer draw fallback)"),
            "",
            "GPU uploads:",
            f"  Point: {_c('get_point_position_upload_count'):,}"
            f"   Color: {_c('get_point_color_upload_count'):,}"
            f"   Class: {_c('get_point_classification_upload_count'):,}",
            "",
            f"Frame time (GPU): {gms:,.2f} ms" if gms > 0
            else "Frame time (GPU): not available",
        ])

    def update_lod_ranges(self, cam_xy, radius_m: float) -> dict:
        """Recompute the visible draw ranges from the stored tile index.

        Only the DRAW COMMANDS change. The point/colour/classification/
        intensity buffers are untouched, so no upload counter moves - which is
        the whole point of doing the permutation once at load.

        Adjacent visible cells are merged into contiguous runs so the number of
        vkCmdDraw calls stays low (289 cells collapsed to 17 runs at 10% zoom).
        """
        info = {"enabled": self.vulkan_lod_enabled(), "ranges": 0, "points": 0,
                "cells": 0}
        b = self.vulkan_backend
        if b is None or not self.active:
            return info
        if not info["enabled"]:
            try:
                b.clear_point_draw_ranges()
            except Exception:
                pass
            return info
        idx = getattr(self, "_lod_index", None)
        if idx is None:
            try:
                b.clear_point_draw_ranges()
            except Exception:
                pass
            return info
        try:
            cells = idx.visible_cells(cam_xy, radius_m)
            runs = []
            for (cx, cy) in cells:
                s, e = idx.cell_ranges(cx, cy)
                if e <= s:
                    continue
                if runs and runs[-1][0] + runs[-1][1] == s:
                    runs[-1] = (runs[-1][0], runs[-1][1] + (e - s))
                else:
                    runs.append((s, e - s))
            if not runs:
                b.clear_point_draw_ranges()
                return info
            b.set_point_draw_ranges([r[0] for r in runs],
                                    [r[1] for r in runs])
            info["ranges"] = len(runs)
            info["points"] = int(sum(c for _f, c in runs))
            info["cells"] = len(cells)
        except Exception:
            self._log.debug("LOD range update failed (ignored)", exc_info=True)
        return info

    # Camera-idle debounce: while the user is dragging we use the coarse
    # selection; this many ms after the last move we re-run it at full
    # fidelity (smaller radius = more cells, denser display).
    _LOD_IDLE_MS = 500
    # Multiplier on the visible ground radius. Deliberately >1: the LOD must
    # over-select rather than under-select, because a missed cell is a visible
    # HOLE and an extra cell is invisible. A perspective view is driven by the
    # same number, so it over-selects much more - correct, not optimal.
    _LOD_SAFETY_MARGIN = 1.6

    def _render_origin_xyz(self):
        """(ox, oy, oz) the vertex buffer is shifted by, or (0,0,0).

        world = render + origin. nkv_set_render_origin is declared in the ctypes
        block but never actually CALLED anywhere in gui/, so this is expected to
        come back (0,0,0) - in which case render space already EQUALS world
        space and adding the origin is a no-op. Read it rather than assume.
        """
        try:
            fn = getattr(self.vulkan_backend._dll, "nkv_get_render_origin", None)
            if fn is None:
                return (0.0, 0.0, 0.0)
            fn.restype = ctypes.c_int
            fn.argtypes = [ctypes.c_uint64, ctypes.POINTER(ctypes.c_double)]
            out = (ctypes.c_double * 3)()
            if not fn(ctypes.c_uint64(self.vulkan_backend._handle), out):
                return (0.0, 0.0, 0.0)
            return (float(out[0]), float(out[1]), float(out[2]))
        except Exception:
            return (0.0, 0.0, 0.0)

    # Ã¢â€â‚¬Ã¢â€â‚¬ Interaction mode: MOVING vs IDLE Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬Ã¢â€â‚¬
    # set by _lod_on_camera_moved / _lod_idle_refine. Purely observational:
    # it records which LOD selection is currently displayed, it never changes
    # what is selected.
    _lod_state = "IDLE"
    _lod_prev_points = 0
    _lod_moves = 0
    _lod_refinements = 0
    _lod_last_refine_ms = 0.0
    _lod_last_refine_from = 0
    _lod_last_refine_to = 0

    def refinement_report(self) -> str:
        """[VULKAN REFINEMENT] - the last idle re-selection."""
        b = self.vulkan_backend
        if b is None or not self.active:
            return "[VULKAN REFINEMENT]\n  backend: not active"
        def _c(fn):
            try:
                return int(getattr(b, fn)())
            except Exception:
                return -1
        return "\n".join([
            "[VULKAN REFINEMENT]",
            "",
            "Previous points:",
            f"  {int(self._lod_last_refine_from):,}",
            "",
            "New points:",
            f"  {int(self._lod_last_refine_to):,}",
            "",
            "Refinement time:",
            f"  {float(self._lod_last_refine_ms):,.2f} ms",
            "",
            "Upload count:",
            f"  point: {_c('get_point_position_upload_count'):,}"
            f"   colour: {_c('get_point_color_upload_count'):,}"
            "   (refinement must not change these)",
            "",
            f"  refinements: {int(self._lod_refinements):,}"
            f"   debounce: {self._LOD_IDLE_MS} ms",
        ])

    def _lod_mark_moving(self, points: int) -> None:
        if self._lod_state != "MOVING":
            self._lod_state = "MOVING"
            self._lod_moves += 1

    def interaction_mode_report(self) -> str:
        """[VULKAN INTERACTION MODE] - MOVING vs IDLE and what is drawn."""
        b = self.vulkan_backend
        if b is None or not self.active:
            return "[VULKAN INTERACTION MODE]\n  backend: not active"
        enabled = self.vulkan_lod_enabled()
        try:
            n_ranges = int(b.get_point_draw_range_count())
            n_pts = int(b.get_point_draw_range_points())
        except Exception:
            n_ranges = n_pts = 0
        if not enabled:
            shown, dr = "all (LOD off)", 0
        elif n_ranges > 0:
            shown, dr = f"{n_pts:,}", n_ranges
        else:
            shown, dr = "all (full-buffer fallback)", 0

        def _c(fn):
            try:
                return int(getattr(b, fn)())
            except Exception:
                return -1
        return "\n".join([
            "[VULKAN INTERACTION MODE]",
            "",
            "State:",
            f"  {self._lod_state}",
            "",
            "Visible points:",
            f"  {shown}",
            "",
            "Draw ranges:",
            f"  {dr:,}",
            "",
            "GPU uploads:",
            f"  point: {_c('get_point_position_upload_count'):,}"
            f"   colour: {_c('get_point_color_upload_count'):,}",
            "",
            f"Frame time (GPU): "
            + (f"{b.get_gpu_frame_time_ms():,.2f} ms"
               if b.get_gpu_frame_time_ms() > 0 else "not available"),
            "",
            f"  camera moves observed: {int(self._lod_moves):,}",
        ])

    def memory_report(self) -> str:
        """[VULKAN MEMORY] - what is resident, against a safe budget.

        Point/surface sizes are MEASURED from the engine's resident element
        counts x the bound vertex formats. The LOD index is CPU-side memory
        (it is never uploaded), so it is accounted separately and does not
        consume VRAM. Draw ranges are a few KB of command data.
        """
        b = self.vulkan_backend
        if b is None or not self.active:
            return "[VULKAN MEMORY]\n  backend: not active"
        MB = 1048576.0
        try:
            vram = int(b.get_vram_bytes())
        except Exception:
            vram = -1
        try:
            n_pts = int(b.get_point_count())
        except Exception:
            n_pts = -1
        try:
            n_vtx = int(b.get_surface_vertex_count())
            n_idx = int(b.get_surface_index_count())
        except Exception:
            n_vtx = n_idx = -1
        point_mb = n_pts * 21 / MB if n_pts >= 0 else float("nan")
        surf_mb = (n_vtx * 28 + n_idx * 4) / MB if n_vtx >= 0 else float("nan")
        idx = getattr(self, "_lod_index", None)
        idx_mb = 0.0
        if idx is not None:
            # order (int64) + cell_start (int64) + per-cell z bounds (f32 x2)
            idx_mb = ((idx.order.nbytes + idx.cell_start.nbytes
                        + idx.cell_min_z.nbytes + idx.cell_max_z.nbytes) / MB)
        try:
            n_ranges = int(b.get_point_draw_range_count())
        except Exception:
            n_ranges = 0
        range_kb = n_ranges * 8 / 1024.0
        total_mb = vram / MB if vram >= 0 else float("nan")
        # NVIDIA T400 is 4 GB; keep a working margin so the compositor, the
        # staging buffer and overlays always have room.
        budget_mb = 2800.0
        pct = (100.0 * total_mb / budget_mb) if total_mb == total_mb else float("nan")
        def _mb(v):
            return f"{v:,.1f} MB" if v == v else "not available"
        return "\n".join([
            "[VULKAN MEMORY]",
            "",
            "Point buffer:",
            f"  {_mb(point_mb)}   ({n_pts:,} pts x 21 B)",
            "",
            "Surface buffer:",
            f"  {_mb(surf_mb)}   ({n_vtx:,} vtx / {n_idx:,} idx)",
            "",
            "LOD index:",
            f"  {_mb(idx_mb)}   CPU-side, never uploaded"
            + (f"   [{idx.nx}x{idx.ny} cells]" if idx is not None else ""),
            "",
            "Draw ranges:",
            f"  {range_kb:,.1f} KB   ({n_ranges:,} ranges)",
            "",
            "Total resident (VRAM):",
            f"  {_mb(total_mb)}   (nkv_get_vram_bytes)",
            "",
            "GPU budget:",
            f"  {budget_mb:,.0f} MB   ({pct:,.1f}% used)"
            if pct == pct else f"  {budget_mb:,.0f} MB",
        ])

    def gpu_pass_report(self) -> str:
        """[VULKAN GPU PASS TIMING] - per-pass GPU cost.

        Measured today (from the engine's timestamp pool):
          Point pass   - the whole vkCmdDraw for the point cloud
          Surface pass - the whole surface draw
          Present      - time inside vkQueuePresentKHR (CPU side)
          Total GPU    - frame-start to frame-end timestamp span

        NOT yet separated: vertex / fragment / depth / rasterisation. Those need
        extra vkCmdWriteTimestamp calls at the pipeline-barrier boundaries
        inside PointCloudRenderer::Record. Reported as "not separated" rather
        than estimated - a guessed vertex/fragment split is exactly the kind of
        number that sends optimisation work in the wrong direction.
        """
        b = self.vulkan_backend
        if b is None or not self.active:
            return "[VULKAN GPU PASS TIMING]\n  backend: not active"
        t = b.get_frame_timing()
        if not t["valid"]:
            return ("[VULKAN GPU PASS TIMING]\n  not available yet "
                    "(no full frame of timestamps read back)")
        share = (f"{(100.0 * t['pointGpuMs'] / t['gpuRenderMs']):,.1f}%"
                 if t["gpuRenderMs"] > 0 else "n/a")
        return "\n".join([
            "[VULKAN GPU PASS TIMING]",
            "",
            "Point pass:",
            f"  {t['pointGpuMs']:,.2f} ms   (timestamp pool)",
            "",
            "Surface pass:",
            f"  {t['surfaceGpuMs']:,.2f} ms   (timestamp pool)",
            "",
            "Point vertex:",
            "  not separated (needs a timestamp at the barrier)",
            "",
            "Point fragment:",
            "  not separated",
            "",
            "Depth:",
            "  not separated",
            "",
            "Raster:",
            "  not separated",
            "",
            "Present:",
            f"  {t['presentMs']:,.2f} ms   (CPU side, inside vkQueuePresentKHR)",
            "",
            "Total GPU:",
            f"  {t['gpuRenderMs']:,.2f} ms   (frame start -> frame end)",
            "",
            f"  point share of frame: {share}",
        ])

    def viewport_performance_report(self) -> str:
        """[VULKAN VIEWPORT PERFORMANCE] + [SURFACE RESIDENT MEMORY].

        Observation only. Every number is read from the engine's existing
        counters; nothing here measures, gates, or changes rendering. Counters
        that the native side does not expose print "n/a" rather than an
        estimate - a guessed draw-call or triangle total is worse than a gap,
        because it is exactly the number a bottleneck diagnosis hangs on.
        """
        b = self.vulkan_backend
        if b is None or not self.active:
            return "[VULKAN VIEWPORT PERFORMANCE]\n  backend: not active"

        def _i(fn, default=None):
            try:
                v = getattr(b, fn)()
                return None if v is None else int(v)
            except Exception:
                return default

        def _f(fn, default=None):
            try:
                return float(getattr(b, fn)())
            except Exception:
                return default

        t = b.get_frame_timing()
        if not t.get("valid"):
            return ("[VULKAN VIEWPORT PERFORMANCE]\n"
                    "  no full frame of GPU timestamps read back yet")

        surf_v = _i("get_surface_vertex_count")
        surf_i = _i("get_surface_index_count")
        pts = _i("get_point_count")
        pd_calls = _i("get_point_draw_call_count")
        sd_calls = _i("get_surface_draw_call_count")
        so_calls = _i("get_surface_overlay_draw_call_count")
        ranges = _i("get_point_draw_range_count")
        range_pts = _i("get_point_draw_range_points")
        overlay_tris = _i("get_overlay_triangle_count")
        vram = _f("get_vram_bytes")

        def _n(v):
            return "n/a" if v is None else f"{int(v):,}"

        def _m(v):
            return "n/a" if v is None else f"{float(v) / (1024.0 * 1024.0):,.1f}"

        # Visible triangles = the surface actually submitted this frame. With a
        # single resident surface mesh that is the whole index buffer; if the
        # engine ever culls, this tracks the real submitted count instead.
        gpu_ms = float(t["gpuRenderMs"])
        cpu_ms = float(t["cpuSubmitMs"])
        present_ms = float(t["presentMs"])
        surf_ms = float(t["surfaceGpuMs"])
        pt_ms = float(t["pointGpuMs"])
        # CPU render preparation is everything the GUI thread spent preparing
        # the frame that the GPU then consumed. The engine reports submit time,
        # not the whole prepare phase, so this is an upper bound on CPU work.
        prep_ms = max(cpu_ms - present_ms, 0.0)

        return "\n".join([
            "[VULKAN VIEWPORT PERFORMANCE]",
            "",
            "  Camera update:          n/a (engine has no camera-stage timer)",
            f"  CPU render preparation: {prep_ms:,.2f} ms   (submit - present)",
            f"  GPU frame:              {gpu_ms:,.2f} ms",
            f"  Present:                {present_ms:,.2f} ms",
            "",
            "  Visible points:         "
            f"{_n(range_pts if ranges else pts)}",
            f"  Visible triangles:      {_n((surf_i // 3) if surf_i is not None else None)}",
            f"  Submitted triangles:    {_n((surf_i // 3) if surf_i is not None else None)}",
            f"  Overlay triangles:      {_n(overlay_tris)}",
            f"  Draw calls:             {_n((pd_calls or 0) + (sd_calls or 0) + (so_calls or 0))}"
            f"   (point={_n(pd_calls)} surface={_n(sd_calls)} overlay={_n(so_calls)})",
            "",
            "  GPU split:",
            f"    point pass:           {pt_ms:,.2f} ms",
            f"    surface pass:         {surf_ms:,.2f} ms",
            "",
            "[SURFACE RESIDENT MEMORY]",
            "",
            "  Surface buffer:",
            f"    vertices:             {_n(surf_v)}",
            f"    indices:              {_n(surf_i)}",
            "",
            f"  Surface vertices:       {_m((surf_v * 24) if surf_v is not None else None)} MB"
            "   (xyz f64 + class u8 + shade f32)",
            f"  Surface indices:        {_m((surf_i * 4) if surf_i is not None else None)} MB   (u32)",
            f"  Surface colours:        n/a   (colours are a u8 class LUT, not a per-vertex array)",
            "",
            f"  Point buffer points:    {_n(pts)}",
            f"  Total device VRAM:      {_m(vram)} MB   (engine-reported, all resources)",
        ])

    def interaction_performance_report(self, label: str = "") -> str:
        """[VULKAN INTERACTION PERFORMANCE] + [VULKAN CAMERA STATE].

        Observation only - reads the engine's existing per-frame counters.

        NOTE ON WHAT THIS CAN AND CANNOT SEE: the engine exposes a GPU frame
        time, a CPU submit time and a present time, but it has NO camera-stage
        timer, so "Camera update" prints n/a rather than a guess. FPS is
        derived from the reported GPU frame time, NOT from wall-clock frame
        delivery - if the app is not presenting continuously, that derived FPS
        is the GPU's capability, not the user's observed frame rate.
        """
        b = self.vulkan_backend
        if b is None or not self.active:
            return "[VULKAN INTERACTION PERFORMANCE]\n  backend: not active"
        t = b.get_frame_timing()
        if not t.get("valid"):
            return ("[VULKAN INTERACTION PERFORMANCE]\n"
                    "  no full frame of GPU timestamps read back yet")

        def _i(fn):
            try:
                return int(getattr(b, fn)())
            except Exception:
                return None

        def _n(v):
            return "n/a" if v is None else f"{int(v):,}"

        gpu = float(t["gpuRenderMs"])
        cpu = float(t["cpuSubmitMs"])
        pres = float(t["presentMs"])
        fps = (1000.0 / gpu) if gpu > 0 else None
        surf_i = _i("get_surface_index_count")
        surf_v = _i("get_surface_vertex_count")
        ranges = _i("get_point_draw_range_count")
        range_pts = _i("get_point_draw_range_points")
        pts = _i("get_point_count")
        calls = None
        try:
            calls = (_i("get_point_draw_call_count") or 0) \
                + (_i("get_surface_draw_call_count") or 0) \
                + (_i("get_surface_overlay_draw_call_count") or 0)
        except Exception:
            pass

        # Uploads since process start - a rising value during pan/zoom would
        # mean geometry is being re-sent, which the FAST mode rules forbid.
        up = _i("nkv_point_position_upload_count")
        try:
            up = int(b._dll.nkv_get_point_position_upload_count(ctypes.c_uint64(b._handle)))
        except Exception:
            up = None
        try:
            su = int(b.get_surface_upload_count())
        except Exception:
            su = None

        return "\n".join([
            "[VULKAN INTERACTION PERFORMANCE]",
            f"  Step:                 {label or 'n/a'}",
            "",
            "  Camera update:        n/a (engine has no camera-stage timer)",
            f"  CPU submit:           {cpu:,.2f} ms",
            f"  GPU frame:            {gpu:,.2f} ms",
            f"  Present:              {pres:,.2f} ms",
            f"  Frame FPS:            {'n/a' if fps is None else f'{fps:,.1f} (derived from GPU frame)'}",
            "",
            f"  Visible points:       {_n(range_pts if ranges else pts)}",
            f"  Visible triangles:    {_n((surf_i // 3) if surf_i is not None else None)}",
            f"  Draw calls:           {_n(calls)}",
            "",
            "[VULKAN CAMERA STATE]",
            "  State:                IDLE (no interaction latch is active)",
            f"  Visible points:       {_n(range_pts if ranges else pts)}",
            f"  Visible triangles:    {_n((surf_i // 3) if surf_i is not None else None)}",
            f"  Point uploads:          {_n(up)}",
            f"  Surface uploads:        {_n(su)}",
            "",
            f"  Surface vertices:     {_n(surf_v)}",
            f"  Surface indices:      {_n(surf_i)}",
        ])

    def classify_bottleneck(self) -> str:
        """[VULKAN BOTTLENECK] - classify from the CURRENT frame's counters.

        Observation only. Thresholds are explicitly heuristics, not physics:
        the point is to rank candidates so the next measurement is aimed
        correctly, not to declare a verdict.
        """
        b = self.vulkan_backend
        if b is None or not self.active:
            return "[VULKAN BOTTLENECK]\n  backend: not active"
        t = b.get_frame_timing()
        if not t.get("valid"):
            return "[VULKAN BOTTLENECK]\n  no frame timing yet"

        gpu = float(t["gpuRenderMs"])
        cpu = float(t["cpuSubmitMs"])
        pres = float(t["presentMs"])
        pt = float(t["pointGpuMs"])
        sf = float(t["surfaceGpuMs"])

        def _i(fn):
            try:
                return int(getattr(b, fn)())
            except Exception:
                return 0

        tris = (_i("get_surface_index_count") // 3) or 0
        pts = _i("get_point_count")
        calls = (_i("get_point_draw_call_count") + _i("get_surface_draw_call_count")
                 + _i("get_surface_overlay_draw_call_count"))

        scores = {
            "GPU BOUND": gpu,
            "CPU BOUND": max(cpu - gpu, 0.0),
            "GEOMETRY BOUND": (tris / 1_000_000.0) * 10.0 if tris else 0.0,
            "DRAW CALL BOUND": (calls / 100.0) if calls > 200 else 0.0,
            "PRESENT BOUND": pres,
        }
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        primary = ranked[0][0] if ranked[0][1] > 0 else "UNKNOWN (all counters zero)"
        secondary = next((k for k, v in ranked[1:] if v > 0), "none")
        rec = {
            "GPU BOUND": "surface LOD + frustum culling; reduce triangles drawn per frame",
            "CPU BOUND": "interaction throttling; move per-frame work off the GUI thread",
            "GEOMETRY BOUND": "surface LOD; build multiple surface levels, pick by zoom",
            "DRAW CALL BOUND": "renderer optimization; batch/merge draws",
            "PRESENT BOUND": "present/swapchain path; check vsync and surface format",
        }.get(primary, "measure more frames before recommending anything")

        return "\n".join([
            "[VULKAN BOTTLENECK]", "",
            f"  Primary:   {primary}",
            f"  Secondary: {secondary}",
            f"  Recommended optimization: {rec}",
            "", "  Evidence:",
            f"    gpu frame    = {gpu:,.2f} ms",
            f"    cpu submit   = {cpu:,.2f} ms",
            f"    present      = {pres:,.2f} ms",
            f"    point pass   = {pt:,.2f} ms",
            f"    surface pass = {sf:,.2f} ms",
            f"    triangles    = {tris:,}",
            f"    points       = {pts:,}",
            f"    draw calls   = {calls:,}",
        ])

    def resource_monitor_report(self) -> str:
        """[NAKSHA RESOURCE MONITOR] + [SURFACE GPU MEMORY]. Observation only.

        Sizes come from the engine's live buffer counts using the same
        per-element widths the renderer uploads, so they track what is actually
        resident rather than what was requested.
        """
        b = self.vulkan_backend
        if b is None or not self.active:
            return "[NAKSHA RESOURCE MONITOR]\n  backend: not active"
        MB = 1024.0 ** 2

        def _i(fn):
            try:
                return int(getattr(b, fn)())
            except Exception:
                return 0

        sv, si = _i("get_surface_vertex_count"), _i("get_surface_index_count")
        pts = _i("get_point_count")
        # Uploaded widths: surface vertex xyz f64 + class u8 + shade f32 = 24 B,
        # u32 indices; points xyz f32 + class u8 + intensity i16 = 15 B.
        surf_mb = (sv * 24 + si * 4) / MB
        pt_mb = pts * 15 / MB
        try:
            vram = float(b.get_vram_bytes()) / MB
        except Exception:
            vram = None
        ram_used = ram_tot = None
        try:
            import psutil
            vm = psutil.virtual_memory()
            ram_used, ram_tot = vm.used / MB, vm.total / MB
        except Exception:
            pass

        def _m(v):
            return "n/a" if v is None else f"{v:,.1f} MB"

        ram = _m(ram_used) + (f" of {_m(ram_tot)}" if ram_tot else "")
        return "\n".join([
            "[NAKSHA RESOURCE MONITOR]", "",
            f"  RAM:            {ram}",
            f"  VRAM:           {_m(vram)}",
            f"  Surface memory: {_m(surf_mb)}",
            f"  Point memory:   {_m(pt_mb)}",
            f"  Active buffers: surface={'yes' if si else 'no'}  points={'yes' if pts else 'no'}",
            "", "[SURFACE GPU MEMORY]",
            f"  LOD0 (active):  {_m(surf_mb)}",
            "  LOD1:           n/a (no surface LOD built)",
            "  LOD2:           n/a (no surface LOD built)",
            f"  Resident:       {_m(surf_mb + pt_mb)}",
            "  Active:         LOD0",
        ])

    def _data_state_for_report(self) -> str:
        try:
            app = self._app
            return str(getattr(app, "_data_state", "UNKNOWN"))
        except Exception:
            return "UNKNOWN"

    def _loading_overlay_visible(self) -> bool:
        """True only if a loading overlay is still alive AND on screen."""
        try:
            dlg = getattr(self._app, "_load_progress", None)
            if dlg is None:
                return False
            return bool(dlg.isVisible())
        except Exception:
            return False

    def window_state_snapshot(self) -> dict:
        """Capture the counters that must NOT move across a window event.

        A minimize/restore/resize must never re-upload geometry, clear a
        buffer or push the app back into the loading state. The only thing
        allowed to change is the swapchain extent (and its recreation count).
        """
        b = self.vulkan_backend
        if b is None or not self.active:
            return {}

        def _c(fn, d=-1):
            try:
                return int(getattr(b, fn)())
            except Exception:
                return d
        try:
            _r, _s, rec = b.get_frame_stats()
        except Exception:
            _r = _s = rec = -1
        return {
            "point_upload": _c("get_point_position_upload_count"),
            "colour_upload": _c("get_point_color_upload_count"),
            "class_upload": _c("get_point_classification_upload_count"),
            "surface_upload": _c("get_surface_upload_count"),
            "point_count": _c("get_point_count"),
            "swapchain_recreates": rec,
            "extent": (self.vulkan_widget.width(), self.vulkan_widget.height())
            if self.vulkan_widget is not None else (0, 0),
        }

    def window_state_report(self, before: dict = None, event: str = "") -> str:
        """[VULKAN WINDOW STATE] - did the window event disturb any data?

        Point/surface upload counts and the resident point count MUST be
        identical to the pre-event snapshot. Only swapchain_recreates and the
        extent may move.
        """
        after = self.window_state_snapshot()
        if not after:
            return "[VULKAN WINDOW STATE]\n  backend: not active"
        if not before:
            return ("[VULKAN WINDOW STATE]\n  "
                    "(no pre-event snapshot captured)")

        def _delta(k):
            b0, a0 = before.get(k, -1), after.get(k, -1)
            if b0 < 0 or a0 < 0:
                return "n/a"
            return f"{b0} -> {a0}" + ("" if b0 == a0 else "   <-- CHANGED")
        stable = all(before.get(k) == after.get(k) for k in
                     ("point_upload", "colour_upload", "class_upload",
                      "surface_upload", "point_count"))
        return "\n".join([
            "[VULKAN WINDOW STATE]",
            "",
            f"Event: {event or 'unspecified'}",
            "",
            "Data state:",
            f"  {self._data_state_for_report()}",
            "",
            "Loading overlay:",
            f"  {'HIDDEN' if not self._loading_overlay_visible() else 'VISIBLE'}",
            "",
            "Minimize / Restore / Resize:",
            f"  data preserved: {'YES' if stable else 'NO'}",
            "",
            "Point upload:",
            f"  before: {before.get('point_upload')}",
            f"  after:  {after.get('point_upload')}   {_delta('point_upload')}",
            "",
            "Surface upload:",
            f"  before: {before.get('surface_upload')}",
            f"  after:  {after.get('surface_upload')}   {_delta('surface_upload')}",
            "",
            "Colour upload:",
            f"  before: {before.get('colour_upload')}",
            f"  after:  {after.get('colour_upload')}   {_delta('colour_upload')}",
            "",
            "Resident points:",
            f"  before: {before.get('point_count'):,}"
            if isinstance(before.get('point_count'), int)
            else f"  before: {before.get('point_count')}",
            f"  after:  {after.get('point_count'):,}"
            if isinstance(after.get('point_count'), int)
            else f"  after:  {after.get('point_count')}",
            "",
            "Swapchain recreated:",
            f"  {'YES' if after.get('swapchain_recreates', 0) != before.get('swapchain_recreates', 0) else 'NO'}"
            f"  ({before.get('swapchain_recreates')} -> "
            f"{after.get('swapchain_recreates')})",
            "",
            f"  extent: {before.get('extent')} -> {after.get('extent')}",
        ])

    def _lod_extent_report(self) -> None:
        """[VULKAN LOD VIEW EXTENT] - how the visible ground rectangle (and so
        the LOD radius) was derived from the camera."""
        try:
            d = getattr(self, "_lod_extent_debug", None)
            if not d:
                print("[VULKAN LOD VIEW EXTENT]\n  (not computed yet)",
                      flush=True)
                return
            print("\n".join([
                "[VULKAN LOD VIEW EXTENT]",
                "",
                "Camera mode:",
                f"  {d['mode']}",
                "",
                "parallel_scale:",
                f"  {d['parallel_scale']:,.3f}",
                "",
                "viewport:",
                f"  width:  {d['vw']}",
                f"  height: {d['vh']}",
                "",
                "visible width:",
                f"  {d['visible_w']:,.3f} m",
                "",
                "visible height:",
                f"  {d['visible_h']:,.3f} m",
                "",
                "radius before margin:",
                f"  {d['base']:,.3f} m",
                "",
                "radius after margin:",
                f"  {d['radius']:,.3f} m   (x{self._LOD_SAFETY_MARGIN})",
            ]), flush=True)
        except Exception:
            self._log.debug("LOD extent report failed (ignored)",
                            exc_info=True)

    def _lod_camera_state(self):
        """(centre_xy, radius_m) for the camera the VULKAN renderer is actually
        using, read from the shared rig.

        Reading the VTK camera here was wrong: resync_camera() treats VTK as
        the source of truth and pushes it INTO the rig, so a rig moved by a
        gesture (or a script) gets dragged back to wherever the VTK camera
        still is - typically the world origin. The LOD then tested (0,0)
        against a UTM grid at (390000, 685000), selected nothing, and silently
        fell back to the full-buffer draw.

        The rig is what push_to_backend() sends to the GPU, so it is the
        authoritative camera for LOD purposes.

        The rig target is in RENDER space; the render origin is added back here
        so the LOD radius test runs in the same WORLD frame the index was
        built in. See _render_origin_xyz for the measured origin.
        """
        try:
            rig = self._camera_rig
            t = np.asarray(rig.target, dtype=np.float64)
            # The rig target is in RENDER space (world minus the render
            # origin the engine subtracts for float32 precision). The LOD index
            # is built from data["xyz"] in WORLD space, so the origin has to be
            # added back before the radius test - otherwise the camera reads as
            # (-2.3, 0.0) against a UTM grid at (390000, 685000) and every
            # selection comes back empty.
            ox, oy, _oz = self._render_origin_xyz()
            cx, cy = float(t[0]) + ox, float(t[1]) + oy
            # Viewport aspect drives the width from the height.
            try:
                vw = max(1, int(self.vulkan_widget.width()))
                vh = max(1, int(self.vulkan_widget.height()))
            except Exception:
                vw, vh = 1400, 900
            aspect = vw / float(vh)
            # ORTHO: the rig's parallel_scale IS the HALF height, so the
            # visible height is 2x it. NOTE the trailing underscore - the
            # attribute is parallel_scale_; reading "parallel_scale" silently
            # yields 0.0, which fell through to the perspective branch and
            # produced a radius of a couple of metres on a 1 km dataset.
            #
            # PERSPECTIVE: frustum half-height at the focal distance.
            ps = float(getattr(rig, "parallel_scale_", 0.0) or 0.0)
            if ps > 0.0:
                mode = "ORTHO"
                visible_h = 2.0 * ps
            else:
                mode = "PERSPECTIVE"
                fov_y = math.radians(max(float(rig.fov_y), 1.0))
                visible_h = 2.0 * float(rig.distance) * math.tan(fov_y * 0.5)
            visible_w = visible_h * aspect
            # radius = distance to the far CORNER of the visible rectangle.
            base = math.hypot(visible_w * 0.5, visible_h * 0.5)
            radius = base * self._LOD_SAFETY_MARGIN
            self._lod_extent_debug = {
                "mode": mode, "parallel_scale": ps,
                "vw": vw, "vh": vh,
                "visible_w": visible_w, "visible_h": visible_h,
                "base": base, "radius": radius,
            }
            return (cx, cy), radius
        except Exception:
            self._log.debug("rig camera read failed (ignored)", exc_info=True)
            return None, float("inf")

    def _lod_visible_radius(self) -> float:
        """Kept for callers that only need the radius; see _lod_camera_state."""
        return self._lod_camera_state()[1]

    @staticmethod
    def _lod_debug_enabled() -> bool:
        """NAKSHA_VULKAN_LOD_DEBUG=1 prints the camera/LOD mapping on every
        camera move. Off by default: it would fire on every pan tick."""
        import os
        return os.environ.get("NAKSHA_VULKAN_LOD_DEBUG", "").strip().lower() \
            in ("1", "true", "yes", "on")

    def _lod_camera_debug(self, cam_xy, radius, info) -> None:
        """[VULKAN LOD CAMERA DEBUG] - print the camera/LOD coordinate mapping.

        The failure mode this guards against is a coordinate-system mismatch:
        the index is built from data["xyz"] in WORLD coordinates, and the
        selection is a plain XY radius test. If the camera centre falls outside
        the grid, every visible_cells() call returns [] and the LOD silently
        falls back to the full-buffer draw with no visible error.
        """
        try:
            idx = getattr(self, "_lod_index", None)
            ox, oy, oz = self._render_origin_xyz()
            # cam_xy is ALREADY world (the origin is added in
            # _lod_camera_state); show the render-space equivalent for context.
            _rcx, _rcy = float(cam_xy[0]) - ox, float(cam_xy[1]) - oy
            _wcx, _wcy = float(cam_xy[0]), float(cam_xy[1])
            print("\n".join([
                "[VULKAN LOD CAMERA DEBUG]",
                "",
                "Render camera:",
                f"  x: {_rcx:,.3f}",
                f"  y: {_rcy:,.3f}",
                "",
                "Render origin:",
                f"  x: {ox:,.3f}",
                f"  y: {oy:,.3f}",
                f"  z: {oz:,.3f}",
                "",
                "World camera:",
                f"  x: {_wcx:,.3f}",
                f"  y: {_wcy:,.3f}",
                "",
                "Camera bounds:",
                f"  xmin: {_wcx - radius:,.3f}",
                f"  xmax: {_wcx + radius:,.3f}",
                f"  ymin: {_wcy - radius:,.3f}",
                f"  ymax: {_wcy + radius:,.3f}",
                "",
                "LOD bounds:",
                f"  xmin: {float(idx.origin_xy[0]):,.3f}" if idx is not None
                else "  (no index)",
                f"  xmax: {float(idx.origin_xy[0]) + idx.meta['span_x']:,.3f}"
                if idx is not None else "  xmax: -",
                f"  ymin: {float(idx.origin_xy[1]):,.3f}" if idx is not None
                else "  ymin: -",
                f"  ymax: {float(idx.origin_xy[1]) + idx.meta['span_y']:,.3f}"
                if idx is not None else "  ymax: -",
                "",
                "Coordinate system:",
                "  world; render origin is added to the camera for display "
                "only (it does not change the selection frame)",
                "",
                "Center inside grid:",
                ("  YES" if idx is not None and
                 idx.origin_xy[0] <= _wcx <=
                 idx.origin_xy[0] + idx.meta["span_x"] and
                 idx.origin_xy[1] <= _wcy <=
                 idx.origin_xy[1] + idx.meta["span_y"] else "  NO"),
                "",
                "Selected cells:",
                f"  {info.get('cells', 0):,}",
                "",
                "Selected points:",
                f"  {info.get('points', 0):,}",
                "",
                f"  radius used: {radius:,.2f} m "
                f"(margin x{self._LOD_SAFETY_MARGIN})",
            ]), flush=True)
        except Exception:
            self._log.debug("LOD camera debug failed (ignored)", exc_info=True)

    def _lod_on_camera_moved(self) -> None:
        """Recompute LOD draw ranges for the current camera.

        Called from both camera paths (programmatic resync and mouse gesture).
        During a drag the selection runs with the safety margin only; the
        500 ms idle timer then re-runs it at full fidelity.
        """
        if not self.vulkan_lod_enabled():
            return
        try:
            (cx, cy), r = self._lod_camera_state()
            if cx is None:
                return
            _info = self.update_lod_ranges((cx, cy), r)
            self._lod_state = "MOVING"
            self._lod_moves += 1
            if self._lod_debug_enabled():
                self._lod_extent_report()
                self._lod_camera_debug((cx, cy), r, _info)
        except Exception:
            self._log.debug("LOD camera hook failed (ignored)", exc_info=True)
            return
        self._arm_lod_idle_refine()

    def _arm_lod_idle_refine(self) -> None:
        """(Re)start the 500 ms idle timer that refines the selection."""
        try:
            t = getattr(self, "_lod_idle_timer", None)
            if t is None:
                t = QTimer()
                t.setSingleShot(True)
                t.timeout.connect(self._lod_idle_refine)
                self._lod_idle_timer = t
            t.start(self._LOD_IDLE_MS)
        except Exception:
            self._log.debug("LOD idle timer arm failed (ignored)", exc_info=True)

    def _lod_idle_refine(self) -> None:
        """Camera has been still for _LOD_IDLE_MS: re-select at full fidelity.

        Same cell set, no extra buffer work - this exists so the display is
        correct immediately after a drag ends rather than staying on the
        coarser drag-time selection.
        """
        try:
            if not self.vulkan_lod_enabled():
                return
            (cx, cy), r = self._lod_camera_state()
            if cx is None:
                return
            # Measure the idle re-selection: how long it took and whether it
            # changed the point count. Draw ranges only - no upload.
            _t0 = time.perf_counter()
            _before = int(self.vulkan_backend.get_point_draw_range_points())
            _info = self.update_lod_ranges((cx, cy), r)
            self._lod_last_refine_ms = (time.perf_counter() - _t0) * 1000.0
            self._lod_last_refine_from = _before
            self._lod_last_refine_to = int(_info.get("points", 0))
            self._lod_state = "IDLE"
            self._lod_refinements += 1
        except Exception:
            self._log.debug("LOD idle refine failed (ignored)", exc_info=True)

    def camera_ownership_report(self) -> str:
        """[VULKAN CAMERA OWNERSHIP] - which camera is authoritative, and the
        frame the LOD selection runs in."""
        b = self.vulkan_backend
        if b is None or not self.active:
            return "[VULKAN CAMERA OWNERSHIP]\n  backend: not active"
        vulkan_owns = self.vulkan_owns_lidar_viewport()
        ox, oy, _oz = self._render_origin_xyz()
        (cx, cy), radius = self._lod_camera_state()
        idx = getattr(self, "_lod_index", None)
        if idx is not None:
            cells = len(idx.visible_cells((cx, cy), radius))
            pts = sum(idx.cell_ranges(*cl)[1] - idx.cell_ranges(*cl)[0]
                      for cl in cells)
        else:
            cells = pts = 0
        rcx, rcy = (cx - ox), (cy - oy)
        return "\n".join([
            "[VULKAN CAMERA OWNERSHIP]",
            "",
            "Mode:",
            "VULKAN" if vulkan_owns else "VTK",
            "",
            "Authoritative camera:",
            "rig" if vulkan_owns else "vtk",
            "",
            "World center:",
            f"  x: {cx:,.3f}",
            f"  y: {cy:,.3f}",
            "",
            "Render origin:",
            f"  x: {ox:,.3f}",
            f"  y: {oy:,.3f}",
            "",
            f"  (render-space centre: {rcx:,.3f}, {rcy:,.3f})",
            "",
            "LOD query bounds:",
            f"  xmin: {cx - radius:,.3f}",
            f"  xmax: {cx + radius:,.3f}",
            f"  ymin: {cy - radius:,.3f}",
            f"  ymax: {cy + radius:,.3f}",
            "",
            "Selected cells:",
            f"  {cells:,}",
            "",
            "Selected points:",
            f"  {pts:,}",
        ])

    def print_performance_report(self) -> None:
        try:
            print(self.performance_report(), flush=True)
        except Exception:
            self._log.exception("performance report failed (ignored)")

    def mark_active(self, render_mode: str) -> None:
        """Call this ONLY from a site that just pushed a REAL workload
        through the Vulkan path (set_point_cloud succeeded, set_surface
        succeeded, set_shaded_class_surface succeeded) - never speculatively.
        Transitions READY -> ACTIVE exactly once per state, prints a single
        state-change line (not per-frame). ACTIVE here means "Vulkan did a
        real render call for a real workload this session" - it does NOT by
        itself mean the user is seeing Vulkan pixels; see is_displayed for
        that separate, honest distinction."""
        if self.state not in (BackendState.READY, BackendState.ACTIVE):
            return
        was_active = self.state == BackendState.ACTIVE
        self.state = BackendState.ACTIVE
        self._ever_active = True
        self.active_render_mode = render_mode
        _display_note = "displayed" if self.is_displayed else "workload-only, VTK still displayed"
        if not was_active:
            print(f"[VULKAN] Renderer state: ACTIVE (mode={render_mode}, {_display_note})")
        else:
            print(f"[VULKAN] Active render mode: {render_mode}")
        # Preview mode: present a frame the moment a real workload lands, so
        # the strict "ACTIVE (Preview)" status (widget visible + swapchain
        # valid + VULKAN_PRESENT_COUNT > 0) becomes true without waiting for
        # a resize or camera event.
        if self.is_displayed and self.vulkan_backend is not None:
            self.vulkan_backend.request_render()

    def toggle_visible(self, vulkan_visible: bool) -> None:
        """Swap which of the two viewport widgets currently occupies the
        layout slot. Used by shutdown()/reactivation paths; does not touch
        surrounding ribbon/dock/panel widgets."""
        if self.vulkan_widget is None:
            return
        vtk_inner = getattr(self._vtk_widget, "interactor", self._vtk_widget)
        try:
            self.vulkan_widget.setVisible(bool(vulkan_visible))
            vtk_inner.setVisible(not vulkan_visible)
        except Exception:
            self._log.exception("toggle_visible failed (ignored)")

    # -- NAKSHA_VULKAN_PREVIEW split-view preview --------------------------------
    def _rebind_renderer_to_current_hwnd(self, width: int, height: int) -> bool:
        """Re-create the native renderer against the widget's CURRENT HWND.

        Qt destroys and re-creates a widget's native window whenever that
        widget is reparented, and the VkSurfaceKHR created by
        nkv_create_renderer() does NOT follow it. Presenting into the dead
        window is not reported as an error (nkv_render still returns success),
        so the only symptom is a permanently black pane - which is exactly
        what the first split-preview capture measured (698x841, every pixel
        0,0,0, after "present" had been counted 7 times).

        The C ABI has no surface-rebind call, so the renderer is destroyed and
        re-created for the live HWND (nkv_create_renderer refuses a second
        renderer in-process, hence destroy-first). GPU buffers do NOT survive
        this - callers must re-upload (see setup_split_preview).

        Returns True only when a renderer is bound to the live HWND.
        """
        try:
            self.vulkan_widget.winId()  # realize the new native window first
            hwnd = int(self.vulkan_widget.winId())
        except Exception:
            self._log.exception("could not obtain the preview widget's HWND")
            return False

        old = self.vulkan_backend
        if hwnd == int(self._vulkan_hwnd or 0) and old is not None and getattr(old, "_handle", 0):
            return True  # same native window: the existing surface is still valid

        if old is not None:
            old.shutdown()  # nkv_destroy_renderer - must happen before create
        new = VulkanRenderBackend()
        if not new.initialize(hwnd, max(1, width), max(1, height)):
            dll = getattr(new, "_dll", None)
            self.last_error = (
                dll.nkv_last_error().decode("utf-8", errors="replace")
                if dll is not None else "no DLL bound"
            )
            print(f"[render_backend] Vulkan renderer rebind failed ({self.last_error})")
            self.vulkan_backend = None
            self.vulkan_ready = False
            self.active = False
            self.backend_name = "vtk"
            self.active_backend = "vtk"
            self.state = BackendState.ERROR
            return False

        previous_hwnd = int(self._vulkan_hwnd or 0)
        self.vulkan_backend = new
        self._vulkan_hwnd = hwnd
        # New renderer, therefore new (empty) GPU buffers: nothing has been
        # rendered BY THIS RENDERER yet, so the honest state is READY, not
        # ACTIVE. The next real workload upload flips it via mark_active().
        self.vulkan_ready = True
        self.state = BackendState.READY
        self.active_render_mode = None
        self.active = True
        self.backend_name = "vulkan"
        self.active_backend = "vulkan"
        print(
            f"[render_backend] Vulkan surface REBOUND 0x{previous_hwnd:X} -> 0x{hwnd:X} "
            f"({max(1, width)}x{max(1, height)}); GPU buffers are empty again"
        )
        return True

    def setup_split_preview(self) -> bool:
        """Dock the Vulkan surface as the RIGHT half of the main-window
        splitter (NAKSHA_VULKAN_PREVIEW=1). VTK stays untouched on the left
        with all of its overlays; nothing is hidden or replaced. Also starts
        the throttled VTK -> Vulkan 2D-plan camera synchronization (matrix
        push only - never a geometry re-upload). Returns True when the
        preview pane is installed."""
        if not (self.active and self.vulkan_widget is not None):
            print("[render_backend] NAKSHA_VULKAN_PREVIEW set but Vulkan is not READY; preview skipped")
            return False
        app = self._app
        splitter = getattr(app, "splitter", None)
        if splitter is None:
            self._log.warning("app.splitter not found; cannot install Vulkan split preview")
            return False
        try:
            # Move the widget out of the (hidden) VTK frame layout into the
            # window-level horizontal splitter: pane 0 = VTK main frame,
            # pane 1 = section panel (0 wide), pane 2 = Vulkan preview.
            old_parent = self.vulkan_widget.parentWidget()
            old_layout = old_parent.layout() if old_parent is not None else None
            self._rebinding = True
            try:
                if old_layout is not None:
                    old_layout.removeWidget(self.vulkan_widget)
                splitter.addWidget(self.vulkan_widget)
                self.vulkan_widget.show()
            finally:
                self._rebinding = False

            total = sum(splitter.sizes()) or max(splitter.width(), 1200)
            half = max(total // 2, 200)
            if splitter.count() >= 3:
                splitter.setSizes([half, 0, half])

            # Apply the real pane size to the swapchain now (the widget's
            # resizeEvent also covers this; this makes install deterministic).
            w = max(1, self.vulkan_widget.width())
            h = max(1, self.vulkan_widget.height())

            # Reparenting gave the widget a BRAND NEW native window while the
            # renderer still presents into the discarded one. Re-bind it to
            # the live HWND before any frame is claimed as presented -
            # otherwise every "successful" present goes to a dead window and
            # the pane stays black forever (measured exactly that way).
            if not self._rebind_renderer_to_current_hwnd(w, h):
                print("[render_backend] Vulkan split preview ABORTED: renderer could not be "
                      "re-bound to the preview widget's HWND (VTK view unaffected)")
                return False

            self.vulkan_backend.resize(w, h)
            # The discarded renderer took its GPU buffers with it: restore
            # whatever the app already has loaded. A no-op before the first
            # load completes (the normal flow - install happens at startup).
            self.upload_point_cloud()
            self._install_camera_sync()
            self.resync_camera(present=True)
            print(
                f"[render_backend] Vulkan split preview INSTALLED "
                f"({w}x{h}, HWND 0x{self._vulkan_hwnd:X}, VTK left / Vulkan right, "
                f"camera sync on)"
            )
            return True
        except Exception:
            self._log.exception("Vulkan split preview install failed (VTK unaffected)")
            return False

    # -- the REAL viewer host (single source of truth) --------------------
    def vulkan_viewer_host(self):
        """The QWidget that must host the Vulkan surface.

        Resolved from the LIVE Qt hierarchy, not from any cached app
        attribute: the interactor's own parentWidget(). self.frame is only
        that widget by construction, and the app also exposes
        viewport_container/top_bar/ribbon_container/status - several of which
        are None at runtime, so none of them can be trusted as the target.

        self.frame is the right answer for the wrong reason if we name it
        directly: it is correct only because the interactor was added to its
        layout. Asking the interactor itself means the target follows the
        widget that actually shows the point cloud, even if the surrounding
        UI is later re-parented.
        """
        vtk_widget = self._vtk_widget
        if vtk_widget is None:
            return None
        inter = getattr(vtk_widget, "interactor", None)
        if inter is not None:
            host = inter.parentWidget()
            if host is not None:
                return host
        return vtk_widget.parentWidget()

    def vulkan_viewer_host_is_ready(self, min_size: int = 100) -> bool:
        """True once the viewer host is visible and bigger than min_size.

        Guards against installing into a not-yet-laid-out widget: before the
        first show/resize cycle a host still reports Qt's default 100x30, and
        parenting the surface there would create a swapchain at the wrong
        extent and pin it there.
        """
        host = self.vulkan_viewer_host()
        if host is None:
            return False
        try:
            if not host.isVisible():
                return False
            return int(host.width()) > min_size and int(host.height()) > min_size
        except Exception:
            return False

    # -- sole-viewport install (default path, replaces the split preview) ----
    def install_as_main_viewport(self) -> bool:
        """Make the native Vulkan surface THE main viewport, confined to the
        CENTRAL VIEWPORT CELL ONLY - never the whole application window.

        The Vulkan widget has been a hidden child of vtk_widget's OWN parent
        (app_window.py's self.frame - the exact widget that has always held
        vtk_widget.interactor) since _try_activate_vulkan(), and was never
        added to self.frame's layout (that already holds the interactor;
        adding a second widget to it would split the space between them
        instead of stacking them). This method makes it visible, raises it
        above the interactor, positions it to match the interactor's
        geometry, and installs a _GeometryMirror event filter to keep it
        pinned to that geometry on every resize/move - all within
        self.frame, never touching the ribbon, menu bar, toolbars, docks or
        status bar, which are separate widgets elsewhere in the QMainWindow's
        tree. The widget's parent never changes here, so its native HWND
        (and therefore its live Vulkan surface) is never destroyed/recreated
        - only the swapchain extent is resized to match.

        This is the default path taken by gui/app_window.py (replacing the
        NAKSHA_VULKAN_PREVIEW split preview). Opt out with
        NAKSHA_VULKAN_PREVIEW=0|off|no or NAKSHA_RENDER_BACKEND=vtk.

        VTK itself is never hidden: only its LiDAR actors (point cloud /
        surface / shaded-class mesh) are switched off via
        set_vtk_lidar_rendering(False), so every overlay (SNT, digitizer,
        grid labels, measurements, text, vectors) keeps rendering - Vulkan's
        opaque surface simply sits on top of where the point cloud would
        have been drawn. Returns True when Vulkan is the visible viewport."""
        if not (self.active and self.vulkan_widget is not None):
            return False
        if self._viewport_installed:
            # set_view() / loaders keep moving the VTK camera after install;
            # re-assert stacking and push whatever pose they left behind.
            self.vulkan_widget.raise_()
            self.resync_camera(present=True)
            return True
        try:
            vtk_widget = self._vtk_widget
            slot = getattr(vtk_widget, "interactor", None) or vtk_widget
            # Resolve the host from the live Qt hierarchy rather than trusting
            # app.frame / app.viewport_container: those are None in some
            # startup orders, and naming a widget that merely happens to be
            # the right one is how the surface ended up spanning the window.
            host = self.vulkan_viewer_host()
            if host is None:
                self._log.warning("VTK widget has no parent; cannot install main viewport")
                return False
            if not self.vulkan_viewer_host_is_ready():
                hw, hh = (host.width(), host.height()) if host is not None else (0, 0)
                self._log.warning(
                    "Viewer host not ready (visible=%s size=%dx%d); "
                    "refusing to install at a wrong geometry",
                    bool(getattr(host, "isVisible", lambda: False)()), hw, hh)
                return False
            if self.vulkan_widget.parentWidget() is not host:
                # Should already match (both created against the same
                # parent in _try_activate_vulkan) - reparent defensively if
                # it doesn't, but this is not the expected path.
                self.vulkan_widget.setParent(host)
            self.vulkan_widget.setGeometry(slot.geometry())
            self.vulkan_widget.raise_()
            self.vulkan_widget.show()
            self._geometry_mirror = _GeometryMirror(slot, self.vulkan_widget, host)

            w, h = max(1, slot.width()), max(1, slot.height())
            # The widget's parent never changed (it has lived inside
            # self.frame, just hidden, since _try_activate_vulkan), so its
            # HWND is still the one the live renderer is bound to - only the
            # swapchain extent needs to catch up to the real size.
            self.vulkan_backend.resize(w, h)
            self.upload_point_cloud()          # restores buffers + shading state
            self._install_camera_sync()        # VTK -> rig adoption (programmatic views)
            self.vulkan_widget.raise_()
            self._viewport_installed = True
            # Vulkan is now the visible LiDAR renderer, so stop VTK drawing
            # it. Only the LiDAR actors (point cloud / surface / shaded-class
            # mesh) are switched off - the main VTK render loop, its overlay
            # renderer (SNT, digitizer, measurements, text, vectors) and Qt's
            # own UI (ribbon/menus/toolbars/status bar) keep working exactly
            # as before.
            self.set_vtk_lidar_rendering(False)
            print(
                f"[render_backend] Vulkan MAIN VIEWPORT installed ({w}x{h}, "
                f"central viewport cell only; VTK render loop, overlays and "
                f"Qt UI unaffected)"
            )
            self._log_vulkan_ui_embedding()
            self._log_vulkan_target()
            self._log_vulkan_ui_geometry()
            self.resync_camera(present=True)
            return True
        except Exception:
            self._log.exception("Vulkan main-viewport install failed (VTK unaffected)")
            return False

    def _log_vulkan_ui_embedding(self) -> None:
        """[VULKAN UI EMBEDDING] - proof the Vulkan surface is confined to
        the LiDAR viewport cell and the window itself is still visible.

        Deliberately reads only the live hierarchy: the app's ribbon/top_bar/
        status attributes are None at runtime and previously reported
        "unknown", which is what hid the surface covering the whole window.
        """
        app = self._app
        try:
            main_visible = bool(app.isVisible())
        except Exception:
            main_visible = False
        host = self.vulkan_viewer_host()
        try:
            host_geo = f"{host.width()}x{host.height()}" if host is not None else "None"
        except Exception:
            host_geo = "?"
        try:
            vw, vh = (self.vulkan_widget.width(), self.vulkan_widget.height()) \
                if self.vulkan_widget is not None else (0, 0)
        except Exception:
            vw = vh = 0
        status = "ACTIVE" if self.vulkan_viewport_is_visible() else "INACTIVE"
        print("\n".join([
            "[VULKAN UI EMBEDDING]",
            "",
            "Main window:",
            "visible" if main_visible else "hidden",
            "",
            "LiDAR viewport cell:",
            host_geo,
            "",
            "Vulkan surface:",
            f"{vw}x{vh}",
            "",
            "Status:",
            status,
        ]))

    def _log_vulkan_target(self) -> None:
        """[VULKAN TARGET] - which widget the Vulkan surface is parented to,
        and proof it is the interactor's own parent rather than the
        QMainWindow / central widget / splitter / whole frame.

        This is the check that catches "Vulkan covers the complete
        application area": if the parent is anything other than the viewer
        host, the surface has escaped the LiDAR viewport.
        """
        vtk_widget = self._vtk_widget
        inter = getattr(vtk_widget, "interactor", None) if vtk_widget is not None else None
        host = self.vulkan_viewer_host()
        vw = self.vulkan_widget
        parent = vw.parentWidget() if vw is not None else None
        app = self._app

        def _geo(w):
            try:
                return f"{int(w.width())}x{int(w.height())}" if w is not None else "None"
            except Exception:
                return "?"

        def _cls(w):
            try:
                return type(w).__name__ if w is not None else "None"
            except Exception:
                return "?"

        parent_is_host = parent is not None and host is not None and parent is host
        escapes = []
        if parent is not None:
            if app is not None and parent is app:
                escapes.append("QMainWindow")
            try:
                cw = app.centralWidget() if app is not None else None
                if cw is not None and parent is cw:
                    escapes.append("centralWidget")
            except Exception:
                pass
        # The splitter (and its ancestors) must NOT be the direct parent: a
        # surface parented there would span the whole central area.
        splitter = getattr(app, "splitter", None)
        if splitter is not None and parent is splitter:
            escapes.append("QSplitter")
        frame = getattr(app, "frame", None)
        if frame is not None and parent is not host and parent is frame:
            escapes.append("app.frame (not the interactor parent)")

        print("\n".join([
            "[VULKAN TARGET]",
            "",
            "QtInteractor:",
            f"{_geo(inter)}  parent={_cls(inter.parentWidget() if inter is not None else None)}",
            "",
            "Parent widget:",
            f"{_geo(host)}  class={_cls(host)}",
            "",
            "Parent is viewer host:",
            "PASS" if parent_is_host else "FAIL",
            "",
            "Vulkan parent:",
            f"{_cls(parent)}  geo={_geo(vw)}",
            "",
            "Escapes viewer host:",
            (", ".join(escapes) if escapes else "none"),
        ]))

    def _log_vulkan_ui_geometry(self) -> None:
        """[VULKAN UI GEOMETRY] - the real widget sizes from the live Qt
        hierarchy, so a regression where Vulkan covers the WHOLE window
        instead of just the LiDAR viewport shows up immediately in the log
        rather than only as a black screen.

        Every widget below is discovered by walking parentWidget() from the
        interactor. The app's own ribbon_container / top_bar / status
        attributes are NOT consulted: they are None at runtime, so the old
        report printed 0x0 and hid the real problem.
        """
        app = self._app
        vtk_widget = self._vtk_widget
        inter = getattr(vtk_widget, "interactor", None) if vtk_widget is not None else None

        def _wh(w):
            try:
                return f"{int(w.width())}x{int(w.height())}" if w is not None else "None"
            except Exception:
                return "?"

        host = self.vulkan_viewer_host()
        # Walk up from the viewer host to find the enclosing splitter and the
        # main window, without assuming any app attribute is populated.
        splitter = None
        cur = host.parentWidget() if host is not None else None
        while cur is not None:
            if type(cur).__name__ == "QSplitter":
                splitter = cur
                break
            cur = cur.parentWidget()

        vw = self.vulkan_widget
        parent = vw.parentWidget() if vw is not None else None
        parent_ok = host is not None and parent is host

        print("\n".join([
            "[VULKAN UI GEOMETRY]",
            "",
            "Main window:",
            _wh(app),
            "",
            "Splitter:",
            _wh(splitter),
            "",
            "Viewer host:",
            f"{_wh(host)}  ({type(host).__name__ if host is not None else 'None'})",
            "",
            "QtInteractor:",
            _wh(inter),
            "",
            "Vulkan:",
            f"{_wh(vw)}  visible="
            f"{bool(vw.isVisible()) if vw is not None else False}",
            "",
            "Parent:",
            f"{type(parent).__name__ if parent is not None else 'None'}"
            f"{'  [OK - viewer host]' if parent_ok else '  [WRONG]'}",
        ]))

    # -- VTK -> Vulkan camera sync (2D plan parity) --------------------------------
    def _install_camera_sync(self) -> None:
        """Observe the main VTK camera and mirror pan/zoom/fit into the
        Vulkan look-at camera. Coalesced by a single-shot 33 ms QTimer so a
        drag produces at most ~30 Vulkan renders/sec. Only
        nkv_set_camera_lookat + nkv_render are called - the uploaded
        point cloud / surface / shaded-class buffers are never touched."""
        from PySide6.QtCore import QTimer
        try:
            cam = self._app.vtk_widget.renderer.GetActiveCamera()
        except Exception:
            self._log.warning("No main VTK camera to sync; preview camera stays static")
            return
        if self._camera_sync_timer is None:
            self._camera_sync_timer = QTimer()
            self._camera_sync_timer.setSingleShot(True)
            self._camera_sync_timer.setInterval(33)
            self._camera_sync_timer.timeout.connect(self.resync_camera)
        if self._vtk_camera_observer is None:
            self._vtk_camera_observer = cam.AddObserver(
                "ModifiedEvent", self._on_vtk_camera_modified
            )

    def _remove_camera_sync(self) -> None:
        timer = self._camera_sync_timer
        self._camera_sync_timer = None
        if timer is not None:
            try:
                timer.stop()
                timer.deleteLater()
            except Exception:
                pass
        tag = self._vtk_camera_observer
        self._vtk_camera_observer = None
        if tag is not None:
            try:
                cam = self._app.vtk_widget.renderer.GetActiveCamera()
                cam.RemoveObserver(tag)
            except Exception:
                pass


    def describe_camera(self, label: str = "") -> str:
        """Full camera + pipeline state as ONE report line.

        The diagnostic the "points disappear while zooming" report asked for.
        It reports OBSERVED values only: the pose about to be pushed, what the
        engine actually recorded (draw calls, uploads, LUT updates), and the
        two numbers that actually settle the question - how many points fall
        outside the near/far planes being pushed, and the gl_PointSize the
        adaptive model resolves to across the cloud. If clipping were the
        cause the out-of-range count jumps; if point size were the cause the
        minimum collapses below 1 px.

        Printed only when NAKSHA_VULKAN_CAMERA_DEBUG=1; the string is always
        returned so a test can assert on it.
        """
        rig = self._camera_rig
        b = self.vulkan_backend
        eye, target = rig.eye(), rig.target
        dist = float(np.linalg.norm(eye - target))
        # The direction the view matrix actually looks along. In perspective
        # mode the eye is rebuilt from azimuth/elevation, so rig.view_dir_ can
        # still hold the value the ortho path left in it - reporting the real
        # eye->target vector keeps the telemetry from misleading the reader.
        fwd = (target - eye) / dist if dist > 1e-9 else np.array([0.0, 0.0, -1.0])
        proj = "ORTHOGRAPHIC" if rig.orthographic_ else "PERSPECTIVE"
        parts = [
            f"[vulkan-cam] {label or 'camera'}",
            f"projection={proj}",
            f"centre=({rig.target[0]:.2f},{rig.target[1]:.2f},{rig.target[2]:.2f})",
            f"parallel_scale={rig.parallel_scale_:.4f}",
            f"view_dir=({fwd[0]:+.2f},{fwd[1]:+.2f},{fwd[2]:+.2f})",
            f"eye=({eye[0]:.2f},{eye[1]:.2f},{eye[2]:.2f})",
            f"target=({target[0]:.2f},{target[1]:.2f},{target[2]:.2f})",
            f"dist={dist:.3f}", f"fov_y={rig.fov_y:.2f}",
            f"near={rig.near_clip:.4f}", f"far={rig.far_clip:.2f}",
            # Which authority owns the planes right now, and how deep the
            # engine's actual point buffers are. clip_source=vtk with a near
            # inside the depth range, or data_radius=0.00 after a load, both
            # mean the clip box is wrong no matter how good the pose looks.
            f"clip_source={rig.clip_source_}",
            f"data_radius={rig.scene_radius_:.2f}",
        ]
        if b is not None:
            try:
                is_ortho, eng_scale = b.get_camera_projection()
                parts.append(f"engine_projection={'ORTHO' if is_ortho == 1 else 'PERSP'}")
                parts.append(f"engine_parallel_scale={eng_scale:.4f}")
            except Exception:
                pass
        try:
            cam = self._app.vtk_widget.renderer.GetActiveCamera()
            if cam is not None:
                n, f = cam.GetClippingRange()
                parts.append(f"vtk_parallel={int(bool(cam.GetParallelProjection()))}")
                parts.append(f"vtk_parallel_scale={float(cam.GetParallelScale()):.4f}")
                parts.append(f"vtk_near={float(n):.4f}")
                parts.append(f"vtk_far={float(f):.2f}")
        except Exception:
            pass
        if b is not None:
            try:
                parts.append(f"origin={b.get_render_origin()}")
                parts.append(f"points={b.get_point_count():,}")
                parts.append(f"visible={b.get_point_cloud_visible()}")
                parts.append(f"max_point_size={b.get_max_point_size():.1f}")
                parts.append(f"point_draws={b.get_point_draw_call_count()}")
                parts.append(f"surface_draws={b.get_surface_draw_call_count()}")
                parts.append(f"lut_updates={b.get_lut_update_count()}")
                parts.append(f"pos_uploads={b.get_point_position_upload_count()}")
            except Exception:
                pass
        data = getattr(self._app, "data", None)
        if data and data.get("xyz") is not None and dist > 1e-9:
            try:
                xyz = np.asarray(data["xyz"])
                idx = np.linspace(0, xyz.shape[0] - 1,
                                  min(200000, xyz.shape[0])).astype(np.int64)
                pts = xyz[idx].astype(np.float64)
                fwd = target - eye
                fwd = fwd / np.linalg.norm(fwd)
                depth = (pts - eye) @ fwd        # view-space depth == gl_Position.w
                # Two different things, deliberately counted separately:
                #  * behind the eye (depth <= 0) MUST be clipped - correct,
                #  * in front but nearer than near_clip is a genuine fault.
                behind_eye = depth <= 0.0
                parts.append(f"pts_behind_eye={int(behind_eye.sum())}")
                parts.append(f"pts_clipped_near={int(((depth > 0.0) & (depth <= rig.near_clip)).sum())}")
                parts.append(f"pts_clipped_far={int((depth >= rig.far_clip).sum())}")
                # How many points the three counts above are drawn from: the
                # sampler caps at 200k, so a clip count is meaningless without
                # its denominator.
                parts.append(f"depth_sample={int(depth.size)}")
                parts.append(f"depth=[{depth.min():.2f},{depth.max():.2f}]")
                u = shading_uniforms(self._app)
                # VTK PARITY: gl_PointSize is now a FIXED pixel size read from
                # pc.size.y, with no depth/footprint term. Reporting the old
                # footprint/depth formula here would describe a shader that no
                # longer exists, so report the real clamp band and the raw
                # pixel size instead.
                raw_px = float(u["min_px"])
                lo_px = max(1.0, min(raw_px, _POINTSIZE_HW_CAP))
                hi_px = lo_px  # fixed-pixel mode: min_px == max_px
                parts.append(f"gl_PointSize=[{lo_px:.2f},{hi_px:.2f}]")
                parts.append(f"point_size_px={raw_px:.2f}")
            except Exception as _e:
                parts.append(f"depth_analysis=unavailable({_e!r})")
        line = " ".join(parts)
        if camera_debug_enabled():
            print(line, flush=True)
        return line

    def _on_vtk_camera_modified(self, caller, event) -> None:
        # Fired for every pan/zoom/fit step; coalesce to one sync per 33 ms.
        if self.main_vtk_adoption_blocked():
            return  # VTK -> Vulkan writeback disabled for the main view
        if self._mirroring_camera:
            return  # our own rig->VTK write; nothing to adopt back
        if self._camera_sync_in_progress:
            return  # canonical camera is mid-write; no recursive second change
        try:
            if getattr(self._app, "_camera_mutation_in_progress", False):
                # PART 11: a Naksha pan/zoom handler owns this transaction and
                # performs the single Vulkan push at the end of it. The observer
                # is MIRROR-ONLY: it must never create a competing change here.
                return
        except Exception:
            pass
        if not (self.active and self.vulkan_widget is not None):
            return
        try:
            if not self.vulkan_widget.isVisible():
                return  # hidden preview costs nothing at all
        except Exception:
            return
        timer = self._camera_sync_timer
        if timer is not None and not timer.isActive():
            timer.start()

    def sync_main_camera_from_vtk(self, source: str = "vtk") -> bool:
        """STAGE A: seed MainCamera2D from the working VTK camera (plan view).

        Temporary direction VTK -> MainCamera2D; it disappears when the wheel /
        pan / fit writers own MainCamera2D (stages C-F). Returns True if the
        canonical camera changed."""
        if self.main_vtk_adoption_blocked():
            return False
        try:
            ren = self._app.vtk_widget.renderer
            cam = ren.GetActiveCamera()
            if cam is None or not cam.GetParallelProjection():
                return False
            fp = cam.GetFocalPoint()
            size = ren.GetRenderWindow().GetSize()
            changed = self.main_camera.set_state(
                fp[0], fp[1], cam.GetParallelScale(), size[0], size[1],
                cz=fp[2], source=source)
            if changed:
                # Attribution: every VTK -> MainCamera2D write that still happens
                # (legacy camera tools) is counted by source, never silent.
                tally = self.legacy_vtk_adoptions
                tally[source] = tally.get(source, 0) + 1
            return changed
        except Exception:
            return False

    def resync_camera(self, present: bool = True, adopt_vtk: bool = True) -> None:
        """Adopt the current VTK camera into the shared rig and push it to the
        Vulkan renderer right now (VTK stays the source of truth for
        PROGRAMMATIC view changes: set_view, fit-to-bounds, loaders).

        Parallel (2D plan) VTK cameras are mirrored as a perspective look-at
        whose vertical FOV reproduces the parallel half-height at the focal
        distance:  halfH = dist * tan(fov/2)  =>  fov = 2*atan(halfH/dist).
        The native camera uses a fixed world up vector of +Y (Z-up world);
        view directions parallel to +Y would degenerate, so those frames are
        skipped rather than pushing a broken matrix (the top/plan view looks
        down -Z and is always safe)."""
        if not (self.active and self.vulkan_backend is not None):
            return
        try:
            cam = self._app.vtk_widget.renderer.GetActiveCamera()
            if cam is None:
                return
            if adopt_vtk:
                self.sync_main_camera_from_vtk("resync_camera")  # no-op when blocked
            pos = cam.GetPosition()
            fp = cam.GetFocalPoint()
            dx, dy, dz = pos[0] - fp[0], pos[1] - fp[1], pos[2] - fp[2]
            dist = math.sqrt(dx * dx + dy * dy + dz * dz)
            if dist < 1e-9:
                return
            if abs(dy / dist) > 0.999:
                return  # degenerate for the native +Y up vector; keep last
            # STAGE B: the plan-view centre and scale come from the canonical
            # MainCamera2D, not from a fresh VTK read. (VTK still supplies the
            # view DIRECTION, eye standoff and clip range until later stages.)
            _mc = self.main_camera
            tgt = ((_mc.center_x, _mc.center_y, _mc.center_z)
                   if (cam.GetParallelProjection() and _mc.valid) else fp)
            if cam.GetParallelProjection():
                scale = max(float(_mc.parallel_scale if _mc.valid
                                  else cam.GetParallelScale()), 1e-9)
                fov_y = 2.0 * math.degrees(math.atan(scale / dist))
            else:
                fov_y = float(cam.GetViewAngle())
            near, far = cam.GetClippingRange()
            rig = self._camera_rig
            # A parallel camera is adopted AS parallel: the rig then keeps the
            # world-space window constant across zooms instead of shrinking it
            # (see _VulkanCameraRig.recompute_fov). fov_y above is only the
            # value in effect at this distance.
            if cam.GetParallelProjection():
                # The rig must know how DEEP the scene is before it can build a
                # near/far box. In the STREAMING path upload_point_cloud() never
                # reaches set_data_extent(), so scene_center_ stayed None and
                # data_reach() returned 0; near/far were then derived around the
                # eye standoff alone. That standoff is the VTK pre-Fit default
                # of 1 m, so the pushed box was near=0, far=110 while the cloud
                # spans Z 286.99..342.66 around a focal Z of 314.8 - the near
                # plane sat at Z~315 and cut away the whole cloud, which is why
                # the point cloud was invisible even though centre and
                # parallel_scale were both provably correct.
                # Seed the extent from the renderer's visible bounds (read-only).
                try:
                    if self._camera_rig.scene_center_ is None:
                        _lo = _hi = None
                        # STREAMING: the manager owns the dataset bounds. The
                        # VTK renderer's prop bounds are empty in this mode.
                        try:
                            _m = getattr(self._app, "naksha_stream", None)
                            if _m is not None:
                                _b = _m.dataset_bounds()
                                if _b is not None:
                                    _lo = np.asarray(_b[0], dtype=np.float64)
                                    _hi = np.asarray(_b[1], dtype=np.float64)
                        except Exception:
                            _lo = _hi = None
                        if _lo is None:
                            _b = renderer.ComputeVisiblePropBounds()
                            if _b and all(v is not None for v in _b) \
                                    and _b[1] > _b[0]:
                                _lo = np.array([_b[0], _b[2], _b[4]],
                                               dtype=np.float64)
                                _hi = np.array([_b[1], _b[3], _b[5]],
                                               dtype=np.float64)
                        if _lo is not None and np.all(np.isfinite(_hi - _lo)):
                            self._camera_rig.set_data_extent(_lo, _hi)
                except Exception:
                    pass
                # PART 10: park the eye at a FIXED standoff along the current
                # view direction. An orthographic camera has no eye-distance of
                # its own, so whatever VTK happened to leave in the camera was
                # being adopted as one and then re-derived into near/far on
                # every pan (measured on 123.LAS: 212.12 m -> 219.00 m over four
                # pans that never changed the zoom). Capture it once, hold it.
                # PART 10: hold a stable eye standoff so a 2D pan cannot ratchet
                # it (pan shifted VTK's |pos-fp| by ~1.6 m every time).
                # The latch must NOT be taken once per SESSION: the first value
                # seen is VTK's pre-Fit default of 1 m, which left the ortho
                # depth box built around a 1 m eye - near=0 bisected the cloud
                # (Z 286.99..342.66 around focal Z 314.8) and the point cloud was
                # invisible even though centre and parallel_scale were correct.
                # Adopt a genuinely new standoff (a Fit, or a large re-frame)
                # and hold only against small pan drift.
                if not (self._ortho_standoff and self._ortho_standoff > 1e-6):
                    self._ortho_standoff = dist
                else:
                    _held = float(self._ortho_standoff)
                    if dist > _held * 4.0 or dist < _held * 0.25:
                        self._ortho_standoff = dist
                _sd = float(self._ortho_standoff)
                # NOTE: an earlier attempt raised the standoff to
                # 4*data_reach() so the eye would sit outside the cloud. That
                # fed back through set_eye_target() -> distance -> data_reach()
                # and ran the standoff away to 1.9e7 m in a single session.
                # Reverted: the standoff is taken from VTK and only held.
                # Raising it safely needs a one-shot depth extent, not a
                # distance-derived multiplier.
                pos = (tgt[0] + dx / dist * _sd,
                       tgt[1] + dy / dist * _sd,
                       tgt[2] + dz / dist * _sd)
                rig.set_eye_target(pos, tgt, fov_y, parallel_scale=scale)
            else:
                self._ortho_standoff = None
                rig.set_eye_target(pos, fp, fov_y)
            # VTK's clipping range is a CANDIDATE, never an order (see
            # _VulkanCameraRig.adopt_vtk_clip). Blindly assigning it used to
            # undo the box set_eye_target() had just derived: VTK reported
            # near=5000.0000 far=5001.00 against a cloud spanning depth
            # [4946.59, 5075.90], which clipped 99.5% of the points and left an
            # all-black 3D viewport in BOTH the engine readback and the real
            # screen - std 0.25.
            adopted = rig.adopt_vtk_clip(near, far)
            if not adopted and camera_debug_enabled():
                reach = rig.data_reach()
                print(f"[vulkan-cam] REJECT vtk_clip=[{float(near):.4f},"
                      f"{float(far):.2f}] data_span=[{rig.distance - reach:.2f},"
                      f"{rig.distance + reach:.2f}] -> rig box=["
                      f"{rig.near_clip:.4f},{rig.far_clip:.2f}]", flush=True)
            # ONE push per signature: two describes the same view when the
            # canonical (centre, parallel scale, viewport) AND the depth box
            # are both unchanged, so the duplicate must not reach the engine
            # (PART 6). A clip-only change still has to be pushed.
            sig = self.camera_signature()
            token = (sig, round(float(rig.near_clip), 6),
                     round(float(rig.far_clip), 6)) if sig is not None else None
            if token is not None and token == self._last_pushed_camera_signature:
                return
            ok = rig.push_to_backend(self.vulkan_backend)
            if ok:
                if token is not None:
                    self._last_pushed_camera_signature = token
                if present:
                    self.vulkan_backend.request_render()
            # Screen-space LOD: the camera moved, so the visible cell set moved.
            # Only DRAW RANGES change - no upload, no rebuild, no reorder.
            if ok:
                self._lod_on_camera_moved()
        except Exception:
            self._log.exception("VTK -> Vulkan camera sync failed (ignored)")

    # -- MainCamera2D ownership (stage C+) -----------------------------------
    def _notify_stream_camera(self, source: str) -> None:
        """Tell the stream manager the canonical camera changed (it de-duplicates
        by signature). Direct call: VTK is not in this notification path."""
        try:
            fn = getattr(self._app, "_stream_camera_notify", None)
            if fn is not None:
                fn(source)
        except Exception:
            self._log.exception("stream camera notify failed (ignored)")

    def owns_main_camera(self) -> bool:
        """True when navigation must mutate MainCamera2D instead of the VTK camera.

        Only the Vulkan MAIN viewport qualifies. NAKSHA_MAIN_CAMERA_OWNER=vtk
        rolls back to the legacy VTK-owned path."""
        if os.environ.get("NAKSHA_MAIN_CAMERA_OWNER", "").strip().lower() == "vtk":
            return False
        app = self._app
        if getattr(app, "is_3d_mode", False) or getattr(app, "_main_view_3d_user_enabled", False):
            return False                       # 3D orbit stays on the VTK path
        return bool(self.active and self.vulkan_backend is not None
                    and self._viewport_installed)

    def main_vtk_adoption_blocked(self) -> bool:
        """NAKSHA_DISABLE_MAIN_VTK_CAMERA=1: the VTK main camera must NOT feed
        MainCamera2D / Vulkan (validation switch for VTK independence)."""
        return bool(main_vtk_camera_disabled() and self.owns_main_camera())

    def _mirror_main_camera_to_vtk(self) -> None:
        """MainCamera2D -> VTK, PASSIVE: keeps the VTK camera (overlays, legacy
        tools) in step. Never read back from here, never bumps a generation."""
        mc = self.main_camera
        if not mc.valid:
            return
        try:
            cam = self._app.vtk_widget.renderer.GetActiveCamera()
            if cam is None or not cam.GetParallelProjection():
                return
            pos, fp = cam.GetPosition(), cam.GetFocalPoint()
            dx, dy = mc.center_x - fp[0], mc.center_y - fp[1]
            self._mirroring_camera = True
            try:
                cam.SetFocalPoint(mc.center_x, mc.center_y, fp[2])
                cam.SetPosition(pos[0] + dx, pos[1] + dy, pos[2])
                cam.SetParallelScale(mc.parallel_scale)
            finally:
                self._mirroring_camera = False
        except Exception:
            self._log.exception("MainCamera2D -> VTK mirror failed (ignored)")

    def apply_main_pan(self, dx_px: float, dy_px: float, viewport_height: float) -> bool:
        """STAGE D: pan MainCamera2D by a drag of (dx, dy) surface pixels.

        Changes the centre ONLY; the scale cannot move because pan_pixels never
        touches it. VTK gets a passive mirror, the native camera one push. The
        rig is refreshed from MainCamera2D by that push, so it can no longer be
        stale here."""
        mc = self.main_camera
        if not mc.valid:
            self.sync_main_camera_from_vtk("seed_before_pan")
        if not mc.valid:
            return False
        scale_before, gen_before = mc.parallel_scale, mc.generation
        changed = mc.pan_pixels(dx_px, dy_px, source="pan", viewport_height=viewport_height)
        if os.environ.get("NAKSHA_DEV_CAMERA_TRACE", "").strip() not in ("", "0", "false", "False"):
            print(f"[PAN TRACE] pixel=({dx_px:+.1f},{dy_px:+.1f}) scale {scale_before:.6f} -> "
                  f"{mc.parallel_scale:.6f} generation {gen_before} -> {mc.generation}", flush=True)
        if changed:
            self._mirror_main_camera_to_vtk()
            self.resync_camera(present=True, adopt_vtk=False)
            self._notify_stream_camera("main_camera_pan")
        return changed

    def apply_main_fit(self, center_x: float, center_y: float, center_z: float,
                       scale: float, renderer=None, eye=None) -> bool:
        """STAGE E: Fit = dataset bounds -> MainCamera2D.

        The caller (fit_view) derives centre/scale from the dataset or stream-index
        bounds. They are applied HERE, to MainCamera2D; the VTK camera then gets
        the same top-view pose it always had, as a passive mirror, and the native
        camera is pushed once. VTK ResetCamera is not involved."""
        mc = self.main_camera
        try:
            ren = renderer or self._app.vtk_widget.renderer
            cam = ren.GetActiveCamera()
            size = ren.GetRenderWindow().GetSize()
        except Exception:
            return False
        if not mc.set_state(center_x, center_y, scale, size[0], size[1],
                            cz=center_z, source="fit") and not mc.valid:
            return False                       # invalid fit input: nothing mutated
        mc.center_z = float(center_z)
        self._mirroring_camera = True
        try:
            ex, ey, ez = eye if eye is not None else (center_x, center_y, center_z + 5000)
            cam.SetPosition(ex, ey, ez)
            cam.SetFocalPoint(center_x, center_y, center_z)
            cam.SetViewUp(0, 1, 0)
            cam.ParallelProjectionOn()
            cam.SetParallelScale(mc.parallel_scale)
            ren.ResetCameraClippingRange()
        finally:
            self._mirroring_camera = False
        self.resync_camera(present=True, adopt_vtk=False)
        self._notify_stream_camera("main_camera_fit")
        return True

    def apply_main_zoom(self, factor: float, *, display_position=None,
                        limits=(None, None), label: str = "wheel") -> bool:
        """STAGE C: ONE wheel zoom, applied to MainCamera2D.

        new scale = old scale / factor (the exact 1.10**steps the VTK path used),
        clamped into the dataset-derived window. With `display_position` the
        world point under the cursor stays under the cursor. VTK receives the
        result as a passive mirror; the native camera is pushed once."""
        mc = self.main_camera
        if not mc.valid:
            self.sync_main_camera_from_vtk("seed_before_zoom")
        if not mc.valid or not (math.isfinite(factor) and factor > 0.0):
            return False
        old_scale = mc.parallel_scale
        new_scale = old_scale / factor
        lo_s, hi_s = limits
        if lo_s is not None and hi_s is not None:
            clamped = min(max(new_scale, lo_s), hi_s)
            if clamped != new_scale:
                print(f"[ZOOM SCALE CLAMPED] {label}: {new_scale:.6g} in [{lo_s:.6g}, {hi_s:.6g}]",
                      flush=True)
                new_scale = clamped
        anchor = None
        if display_position is not None:
            anchor = mc.display_to_world(display_position[0], display_position[1])
        gen_before = mc.generation
        mc.zoom_to_scale(new_scale, anchor_world=anchor, source=f"wheel:{label}")
        if os.environ.get("NAKSHA_DEV_CAMERA_TRACE", "").strip() not in ("", "0", "false", "False"):
            print(f"[MAIN CAMERA ZOOM] {label} factor={factor:.6f} scale {old_scale:.6f} -> "
                  f"{mc.parallel_scale:.6f} generation {gen_before} -> {mc.generation}", flush=True)
        if mc.generation != gen_before:
            self._mirror_main_camera_to_vtk()
            self.resync_camera(present=True, adopt_vtk=False)
            self._notify_stream_camera("main_camera_zoom")
        return True

    # -- single camera owner -------------------------------------------------
    def apply_surface_wheel(self, notches: float) -> bool:
        """Apply ONE wheel notch from the Vulkan surface, through the owner.

        The surface does NOT own a pose: it no longer dollies the rig and no
        longer invents its own zoom factor. It normalises its raw notches and
        hands them to the single main-view zoom owner, which applies exactly
        one absolute 1.10**steps mutation and then pushes once (PART 2/8/12).

        3D (Shift+P) is routed to `apply_3d_wheel`, the canonical rig owner.
        That routing is the fix for dead 3D zoom: `apply_zoom_steps` refuses in
        3D by design, so before this the notch was consumed by two handlers that
        both declined to mutate anything. The 2D branch below is unchanged.
        """
        if not notches or not (self.active and self.vulkan_backend is not None):
            return False
        try:
            if bool(getattr(self._app, "is_3d_mode", False)):
                return self.apply_3d_wheel(float(notches))
            from gui.zoom_navigation import apply_zoom_steps
            changed = bool(apply_zoom_steps(self._app, float(notches),
                                            label="vulkan_surface",
                                            debounce=False))
            if changed:
                # apply_zoom_steps mutates only the canonical camera, and the
                # VTK ModifiedEvent observer is deliberately suppressed during
                # that transaction, so NOTHING pushed the new scale to the
                # native renderer: the wheel changed the camera but the
                # viewport stayed at the old scale until the next pan/resize
                # (live trace: vtk 86.13 vs native 94.74 after a notch). Push
                # exactly once, here, after the transaction has closed;
                # resync_camera() de-duplicates by camera signature.
                self.resync_camera(present=True)
            return changed
        except Exception:
            self._log.exception("surface wheel zoom failed (ignored)")
            return False

    # ------------------------------------------------------------------ #
    # 3D WHEEL ZOOM - canonical owner for Shift+P / perspective mode.      #
    # ------------------------------------------------------------------ #
    def _ensure_rig_projection(self) -> bool:
        """Keep the canonical rig's projection in step with the VTK camera's.

        WHY THIS EXISTS. Shift+P (`global_shortcuts._unlock_main_view`) flips the
        VTK camera to perspective, but the canonical `_VulkanCameraRig` stayed in
        its ORTHOGRAPHIC state, because nothing re-adopted the camera on the flip.
        That split is silent and lethal: `_VulkanCameraRig.dolly()` picks its
        branch from `orthographic_`, so a perspective dolly would have mutated
        `parallel_scale_` - a quantity the native camera does not read in
        perspective mode at all. The rig would move and the view would not.

        Re-adopting here makes the rig self-healing in BOTH directions, so it
        also covers the 3D -> 2D return trip and any programmatic projection
        change, without every caller having to remember to resync.
        """
        try:
            cam = self._app.vtk_widget.renderer.GetActiveCamera()
            if cam is None:
                return False
            vtk_parallel = bool(cam.GetParallelProjection())
        except Exception:
            return False
        rig = self._camera_rig
        if bool(rig.orthographic_) == vtk_parallel:
            return False
        self._log.info("canonical rig projection %s -> %s (adopting VTK camera)",
                       "ortho" if rig.orthographic_ else "persp",
                       "ortho" if vtk_parallel else "persp")
        self.resync_camera(present=True)
        return True

    def apply_3d_wheel(self, notches: float) -> bool:
        """Apply ONE wheel notch in 3D (Shift+P) mode, through the canonical rig.

        THE 3D ZOOM ROOT CAUSE, in one sentence: nothing owned the 3D wheel.
        `zoom_navigation.apply_zoom_steps` returns False the moment
        `is_3d_mode` is set ("native 3D wheel zoom is left untouched"), and the
        legacy VTK observer's 3D branch is `self._consume_vtk_event(obj)` - the
        same statement as its 2D branch, so it consumes WITHOUT mutating. The
        Vulkan surface then saw a refused zoom, printed
        `[WHEEL CONTRACT FAILURE]` and called `event.ignore()`. The rig already
        had a correct perspective `dolly()`; it was simply never reached.

        Contract for one physical notch: ONE `rig.dolly()` mutation, then ONE
        `_apply_rig_gesture()` - which pushes the native camera, mirrors VTK and
        requests exactly one render. There is no VTK round trip in the middle,
        so no feedback loop and no double zoom.

        VTK stays a PASSIVE MIRROR via `_mirror_rig_to_vtk`; it is never the
        authority here.
        """
        if not notches or not (self.active and self.vulkan_backend is not None):
            return False
        try:
            self._ensure_rig_projection()
            rig = self._camera_rig
            before = self._rig_trace_snapshot(rig)
            if rig.orthographic_:
                # A parallel camera reached the 3D wheel. dolly() still handles
                # it correctly (it scales parallel_scale_); say so rather than
                # silently pretending this was a perspective dolly.
                self._log.info("3D wheel arrived with an orthographic rig; "
                               "dolly scales the parallel scale instead")
            rig.dolly(float(notches))
            self._apply_rig_gesture()
            after = self._rig_trace_snapshot(rig)
            if os.environ.get("NAKSHA_DEV_CAMERA_TRACE", "").strip() not in (
                    "", "0", "false", "False"):
                self._print_3d_zoom_trace(notches, before, after)
            return True
        except Exception:
            self._log.exception("3D wheel zoom failed (ignored)")
            return False

    @staticmethod
    def _rig_trace_snapshot(rig) -> dict:
        return {
            "projection": "orthographic" if rig.orthographic_ else "perspective",
            "distance": float(rig.distance),
            "parallel_scale": float(rig.parallel_scale_),
            "fov": float(rig.fov_y),
            "azimuth": float(rig.azimuth),
            "elevation": float(rig.elevation),
            "target": np.asarray(rig.target, dtype=np.float64).copy(),
            "eye": np.asarray(rig.eye(), dtype=np.float64).copy(),
            "near_clip": float(rig.near_clip),
            "far_clip": float(rig.far_clip),
        }

    def _print_3d_zoom_trace(self, notches, before, after) -> None:
        """[3D ZOOM TRACE] - the PART A diagnostic, from the real native path."""
        gen = getattr(self.main_camera, "generation", None)
        eye_b = tuple(round(float(v), 6) for v in before["eye"])
        eye_a = tuple(round(float(v), 6) for v in after["eye"])
        tgt_b = tuple(round(float(v), 6) for v in before["target"])
        tgt_a = tuple(round(float(v), 6) for v in after["target"])
        print("[3D ZOOM TRACE]", flush=True)
        print(f"  receiver=VulkanSurface.wheelEvent->apply_surface_wheel"
              f"->apply_3d_wheel wheel_delta={float(notches):+.3f}", flush=True)
        print(f"  projection_mode={before['projection']}->{after['projection']}",
              flush=True)
        print("  camera_owner=_VulkanCameraRig (canonical; VTK is a mirror)",
              flush=True)
        print(f"  position_before={eye_b}\n  position_after={eye_a}", flush=True)
        print(f"  target_before={tgt_b}\n  target_after={tgt_a}", flush=True)
        print(f"  distance_before={before['distance']:.6f} "
              f"distance_after={after['distance']:.6f}", flush=True)
        print(f"  parallel_scale_before={before['parallel_scale']:.6f} "
              f"parallel_scale_after={after['parallel_scale']:.6f}", flush=True)
        print(f"  fov_before={before['fov']:.6f} fov_after={after['fov']:.6f}",
              flush=True)
        print(f"  yaw={after['azimuth']:.6f} pitch={after['elevation']:.6f}",
              flush=True)
        print(f"  clip_before=({before['near_clip']:.4f},{before['far_clip']:.4f}) "
              f"clip_after=({after['near_clip']:.4f},{after['far_clip']:.4f})",
              flush=True)
        moved = int(before["distance"] != after["distance"]
                    or before["parallel_scale"] != after["parallel_scale"])
        print(f"  mutation_count={moved} native_camera_pushed=YES "
              f"render_requested=YES event_consumed=YES "
              f"generation_before={gen}", flush=True)

    def _camera_parallel_scale(self) -> float:
        """Current main-camera ParallelScale, or -1.0 if unavailable."""
        try:
            cam = self._app.vtk_widget.renderer.GetActiveCamera()
            return float(cam.GetParallelScale())
        except Exception:
            return -1.0

    def camera_signature(self) -> Optional[tuple]:
        """(centre_x, centre_y, parallel_scale, viewport_w, viewport_h).

        Two pushes carrying the same signature describe the same view, so the
        second one must not reach the engine (PART 6).
        """
        try:
            mc = self.main_camera
            if mc.valid:
                return (round(float(mc.center_x), 9), round(float(mc.center_y), 9),
                        round(float(mc.parallel_scale), 9),
                        int(mc.viewport_width), int(mc.viewport_height))
            app = self._app
            widget = getattr(app, "vtk_widget", None)
            renderer = getattr(widget, "renderer", None)
            if renderer is None:
                return None
            cam = renderer.GetActiveCamera()
            if cam is None or not cam.GetParallelProjection():
                return None
            fp = cam.GetFocalPoint()
            size = renderer.GetRenderWindow().GetSize()
            return (round(float(fp[0]), 9), round(float(fp[1]), 9),
                    round(float(cam.GetParallelScale()), 9),
                    int(size[0]), int(size[1]))
        except Exception:
            return None

    def _apply_rig_gesture(self) -> None:
        """A mouse gesture on the Vulkan widget just moved the shared rig:
        push the new pose to the native camera AND mirror it back into the
        VTK camera (observer suppressed) so both viewports - and any
        VTK-capturing parity script - keep showing the same frame."""
        if not (self.active and self.vulkan_backend is not None):
            return
        try:
            self._camera_rig.push_to_backend(self.vulkan_backend)
        except Exception:
            self._log.exception("rig -> Vulkan camera push failed (ignored)")
            return
        self._mirror_rig_to_vtk()
        self.vulkan_backend.request_render()
        # Screen-space LOD after a mouse pan/zoom - draw ranges only.
        self._lod_on_camera_moved()

    def _mirror_rig_to_vtk(self) -> None:
        """Write the rig pose back into the VTK camera without re-triggering
        the VTK->Vulkan observer (_mirroring_camera guard). ViewUp is pinned
        to +Y because that is the engine's fixed world up (Camera.hpp)."""
        if self._mirroring_camera:
            return
        try:
            cam = self._app.vtk_widget.renderer.GetActiveCamera()
            if cam is None:
                return
        except Exception:
            return
        rig = self._camera_rig
        eye, target = rig.eye(), rig.target
        dist = max(float(np.linalg.norm(eye - target)), 1e-9)
        self._mirroring_camera = True
        try:
            if cam.GetParallelProjection() and rig.orthographic_:
                # Ortho rig -> a parallel VTK camera with the SAME centre,
                # view direction and parallel scale. No FOV conversion: an
                # ortho window is not a perspective cone.
                right, up = rig.screen_basis()
                centre = np.asarray(rig.target, dtype=np.float64)
                # Park the VTK eye back along -viewDir, as VTK does for a
                # parallel camera (its distance is irrelevant to the view).
                # Use the rig's OWN distance, not far_clip: this mirror's
                # output is what resync_camera() reads back, so parking the eye
                # on the far plane fed the clip box into the camera distance
                # and grew it by 4*radius on every programmatic resync.
                # PART 10: a 2D pan must NEVER change the eye standoff. The
                # rig's own `distance` is re-adopted from VTK by
                # resync_camera(), so parking the eye on it fed any VTK-side
                # distance drift straight back and ratcheted the standoff up on
                # every pan (measured on 123.LAS: 212.12 m -> 219.00 m over four
                # pans, with near/far re-derived from the wrong distance each
                # time). Preserve whatever standoff the VTK camera already has.
                # Preserve the standoff the VTK camera ALREADY has (the rig's
                # `distance` is only a stale re-adoption of it).
                try:
                    _p, _f = cam.GetPosition(), cam.GetFocalPoint()
                    _cur = math.sqrt((_p[0] - _f[0]) ** 2 + (_p[1] - _f[1]) ** 2
                                     + (_p[2] - _f[2]) ** 2)
                except Exception:
                    _cur = 0.0
                standoff = _cur if (_cur > 1e-6) else max(rig.distance, 1.0)
                eye_v = centre - np.asarray(rig.view_dir_, dtype=np.float64) * standoff
                cam.SetPosition(float(eye_v[0]), float(eye_v[1]), float(eye_v[2]))
                cam.SetFocalPoint(float(centre[0]), float(centre[1]), float(centre[2]))
                cam.SetViewUp(float(up[0]), float(up[1]), float(up[2]))
                cam.SetParallelScale(float(rig.parallel_scale_))
                cam.SetClippingRange(max(rig.near_clip, 1e-3),
                                     max(rig.far_clip, rig.near_clip + 1.0))
                return
            half_h = dist * math.tan(math.radians(max(rig.fov_y, 1e-3)) * 0.5)
            cam.SetPosition(float(eye[0]), float(eye[1]), float(eye[2]))
            cam.SetFocalPoint(float(target[0]), float(target[1]), float(target[2]))
            cam.SetViewUp(0.0, 1.0, 0.0)
            if cam.GetParallelProjection():
                cam.SetParallelScale(half_h)
            cam.SetClippingRange(max(rig.near_clip, 1e-3), max(rig.far_clip, rig.near_clip + 1.0))
        except Exception:
            self._log.exception("rig -> VTK camera mirror failed (ignored)")
        finally:
            self._mirroring_camera = False

    def sync_point_shading(self, present: bool = True, mode: Optional[str] = None) -> bool:
        """Push the VTK-parity shading state for the CURRENT display mode:
        the three 256-entry colour LUTs, the elevation/intensity
        normalisation ranges, the adaptive point-size band and the sprite
        parameters - then switch the native display mode.

        Uniform-only by contract (mode/palette/slider changes must never
        re-upload positions). The one exception is a mode GLSL has no path
        for (depth, surface, line, overlay, section, ...): there the exact
        VTK colours are baked on the CPU once and shown via NKV_MODE_RGB.

        Called from upload_point_cloud (after a real load) and from
        gui/pointcloud_display.update_pointcloud (mode/palette changes); a
        no-op until Vulkan actually holds a point cloud."""
        if not (self.active and self.vulkan_backend is not None):
            return False
        app = self._app
        data = getattr(app, "data", None)
        if not data or "xyz" not in data:
            return False
        mode = mode or getattr(app, "display_mode", "rgb") or "rgb"
        # Initialised before the try so the telemetry block below can never
        # reference an unbound local when the sync raised.
        instant = False
        nkv_mode = None
        try:
            b = self.vulkan_backend
            u = shading_uniforms(app)
            cls_lut, elev_lut, int_lut = build_point_luts(app)
            ok = True
            ok &= b.set_point_luts(cls_lut, elev_lut, int_lut)
            ok &= b.set_point_elevation_range(u["elev_lo"], u["elev_hi"], u["elev_gamma"])
            ok &= b.set_point_intensity_range(
                u["int_lo"], u["int_hi"], u["int_contrast"], u["int_gamma"])
            ok &= b.set_point_size_params(
                u["footprint_m"], u["min_px"], u["max_px"], u["class_mix"])
            ok &= b.set_point_sprite_params(u["softness"], u["brightness"])

            nkv_mode = _APP_MODE_TO_NKV.get(mode)

            # Display Mode -> Shaded Class uses the native INSTANT renderer by
            # default. Mode 4 is deliberately NOT NKV_MODE_CLASSIFICATION: it
            # is what routes the frame through the splat pass + fullscreen
            # depth-normal lighting pass. Selecting it writes one float - no
            # mesh is built and no buffer is re-uploaded.
            instant = (mode == "shaded_class" and instant_shaded_renderer_enabled())
            if instant and not (b.supports_instant_shaded()
                                and b.get_instant_shaded_available()):
                # Older DLL or the shaders did not build: fall back to plain
                # classification colouring rather than show an unlit cloud.
                # (The Delaunay path stays reachable via NAKSHA_SHADED_RENDERER.)
                print("[INSTANT SHADED] native pass unavailable -> "
                      "classification colours only")
                instant = False
            if instant:
                nkv_mode = NKV_MODE_SHADED_CLASS_INSTANT

            if nkv_mode is None:
                # No native shading path: bake exactly what compute_colors()
                # gives VTK and show it as RGB (this IS a re-upload, which is
                # why unsupported modes are the exception, not the rule).
                from gui.pointcloud_display import compute_colors
                colors = np.ascontiguousarray(compute_colors(app), dtype=np.uint8)
                ok = b.set_point_cloud(
                    data["xyz"], rgb=colors,
                    classification=data.get("classification"),
                    intensity=data.get("intensity"),
                )
                # Any point-based mode owns the viewport again, so re-show the
                # cloud (Surface / Shaded Class hid it to present their mesh).
                b.set_point_cloud_visible(True)
                if ok:
                    self.mark_active("point_cloud")
            else:
                if instant:
                    ok &= push_instant_splat_band(b, app)
                    ok &= b.set_point_sprite_params(u["softness"], 1.0)
                    ok &= b.set_class_visibility(build_class_visibility(app))
                ok &= b.set_display_mode(int(nkv_mode))
                b.set_point_cloud_visible(True)
                if instant:
                    self.mark_active("shaded_class")
            if ok and present:
                b.request_render()
            sync_ok = bool(ok)
        except Exception:
            sync_ok = False

        # ---- TELEMETRY, OUTSIDE the renderer try ----------------------------
        # Logging used to sit INSIDE that try, so a logging failure returned
        # False and turned a completed shading sync into a reported failure -
        # which the instant handoff then treated as "renderer failed" and sent
        # the user down the legacy fallback. Logging can no longer change this
        # function's result.
        try:
            if not sync_ok:
                self._log.warning(
                    "sync_point_shading(mode=%s) reported a failure", mode)
            _record_render_mode(app, mode, nkv_mode, instant)
        except Exception:
            pass
        return sync_ok

    # -- data path ----------------------------------------------------------
    def upload_point_cloud(self) -> bool:
        """Call once after a load completes (see app_window.py
        _on_load_finished, right after self.data is populated). Never
        called on camera-only interaction or display-mode switches - those
        must be uniform-only updates, not re-uploads, per the native
        PointCloudRenderer contract."""
        if not self.active or self.vulkan_backend is None:
            return False
        data = getattr(self._app, "data", None)
        if not data or "xyz" not in data:
            return False
        try:
            xyz = data["xyz"]
            rgb = data.get("rgb")
            cls = data.get("classification")
            inten = data.get("intensity")

            # Ã¢â€â‚¬Ã¢â€â‚¬ ONE-TIME upload-time permutation for the screen-space LOD Ã¢â€â‚¬Ã¢â€â‚¬
            # The LOD draw ranges index the GPU buffer directly, so cell c must
            # be a CONTIGUOUS block of vertices in that buffer. In LAS order
            # the points of a tile are scattered, so the whole cloud is sorted
            # by tile id ONCE here and every attribute is permuted with it.
            #
            # This happens exactly once per load, on the same call that already
            # performs the one and only geometry upload - it does not add an
            # upload, and it is never repeated per frame.
            #
            # Gated on NAKSHA_VULKAN_LOD so the DEFAULT path uploads the exact
            # original byte order it always did. Reordering is visually inert
            # for this pipeline (points are rasterised independently and alpha
            # blending is disabled), but there is no reason to perturb the
            # buffer for users who did not ask for LOD.
            self._lod_index = None
            if self.vulkan_lod_enabled():
                try:
                    t_perm = time.perf_counter()
                    from gui.lod_tile_index import build_tile_index
                    _idx = build_tile_index(np.asarray(xyz),
                                            target_cell_points=4096)
                    _order = _idx.order
                    xyz = np.ascontiguousarray(
                        np.asarray(xyz)[_order], dtype=np.float64)
                    if rgb is not None:
                        rgb = np.ascontiguousarray(
                            np.asarray(rgb)[_order])
                    if cls is not None:
                        cls = np.ascontiguousarray(
                            np.asarray(cls)[_order])
                    if inten is not None:
                        inten = np.ascontiguousarray(
                            np.asarray(inten)[_order], dtype=np.float32)
                    self._lod_index = _idx
                    print(f"[render_backend] LOD permutation: "
                          f"{_idx.summary()}; reordered in "
                          f"{(time.perf_counter() - t_perm) * 1000.0:,.0f} ms "
                          f"(one-time, same single upload)")
                except Exception as _pe:
                    self._lod_index = None
                    print(f"[render_backend] LOD permutation skipped "
                          f"(LOD draw path will stay off): {_pe!r}")
            else:
                # Keep the buffer in LAS order and make sure no stale ranges
                # survive from a previous dataset.
                try:
                    self.vulkan_backend.clear_point_draw_ranges()
                except Exception:
                    pass

            ok = self.vulkan_backend.set_point_cloud(
                xyz,
                rgb=rgb,
                classification=cls,
                intensity=inten,
            )
            print(f"[render_backend] Vulkan set_point_cloud({data['xyz'].shape[0]:,} points) -> {ok}")
            if ok:
                print(f"[VULKAN] Point cloud received: {data['xyz'].shape[0]:,} points")
                self.mark_active("point_cloud")
                # A fresh load builds fresh VTK actors, so re-assert the LiDAR
                # switch: otherwise the new cloud would be drawn by BOTH
                # renderers and the duplicated work would come straight back.
                self._reassert_vtk_lidar_state()
                # The engine now owns these coordinates, so the camera rig has
                # to learn how deep they are. Nothing else tells it: the app
                # never calls fit_to_bounds(), it adopts VTK's camera, and
                # VTK's clipping range describes VTK's OWN actors - an unknown
                # depth left recompute_clip() guessing from the parallel scale
                # alone, which is how a whole cloud ended up outside near/far.
                try:
                    with np.errstate(invalid="ignore"):
                        _xyz = np.asarray(data["xyz"], dtype=np.float64)
                    if _xyz.ndim == 2 and _xyz.shape[1] >= 3 and _xyz.shape[0]:
                        self._camera_rig.set_data_extent(
                            np.nanmin(_xyz[:, :3], axis=0),
                            np.nanmax(_xyz[:, :3], axis=0))
                except Exception:
                    self._log.exception("Vulkan data extent update failed (ignored)")
                # Positions/classes/intensity just landed: push the VTK-parity
                # LUTs + normalisation ranges + mode as well (uniform-only
                # afterwards; mode switches and palette edits call this too).
                self.sync_point_shading()
                # GRID-GAP/BLACK-VIEWPORT SAFETY RESYNC (found this round): if
                # install_as_main_viewport() ran before the window's real
                # layout settled (observed: "installed (100x30, ...)" at
                # __init__ time, before win.show()/layout), the native
                # swapchain and - more importantly - the projection matrix's
                # aspect ratio can be built from that stale 100x30 rect. A
                # later Qt resize event DOES reach nkv_resize() via the
                # geometry-mirror + _VulkanSurfaceWidget.resizeEvent hook, but
                # nothing forces a fresh CAMERA push afterwards unless VTK's
                # own camera happens to fire a ModifiedEvent again - by the
                # time the FIRST real load completes, the window has been
                # shown and laid out for real, so re-reading the live widget
                # size here and forcing both a resize and a camera resync is
                # a safe, idempotent no-op when nothing was stale, and a real
                # fix when it was.
                try:
                    if self._viewport_installed and self.vulkan_widget is not None:
                        w = max(1, self.vulkan_widget.width())
                        h = max(1, self.vulkan_widget.height())
                        self.vulkan_backend.resize(w, h)
                        self.resync_camera(present=True)
                        print(f"[render_backend] post-load geometry resync: {w}x{h}")
                except Exception:
                    self._log.exception("post-load geometry resync failed (ignored)")
            # DEV instrumentation (guarded; records only, decides nothing).
            if ok:
                try:
                    from gui import instant_shaded_telemetry as ist
                    if ist.get() is not None:
                        d = getattr(self._app, "data", {}) or {}
                        common = {
                            'dataset_path': (str(getattr(self._app, 'loaded_file', '')
                                                 or '') or None),
                            'point_count': ist.num(len(d['xyz'])) if 'xyz' in d else None,
                            'dataset_mode': 'IN_MEMORY',
                        }
                        ist.emit('application_ready', **common)
                        ist.emit('normal_cache_status', state='UNKNOWN', **common)
                        ist.emit('normal_gpu_ready', uploaded_once=1, reused=0,
                                 **common)
                        ist.emit('dataset_ready', **common)
                except Exception:
                    pass
            return ok
        except Exception as _e:
            print(f"[render_backend] Vulkan set_point_cloud failed (non-fatal, VTK view unaffected): {_e!r}")
            self._log.exception("Vulkan set_point_cloud failed (non-fatal, VTK view unaffected)")
            return False

    def resize(self, width: int, height: int) -> None:
        if self.active and self.vulkan_backend is not None:
            try:
                self.vulkan_backend.resize(width, height)
            except Exception:
                self._log.exception("Vulkan resize failed (ignored)")

    def shutdown(self) -> None:
        try:
            self._remove_camera_sync()
        except Exception:
            pass
        try:
            self.toggle_visible(False)
        except Exception:
            pass
        if self.vulkan_backend is not None:
            try:
                self.vulkan_backend.shutdown()
            except Exception:
                self._log.exception("Vulkan shutdown failed (ignored)")
        if self.vulkan_widget is not None:
            try:
                self.vulkan_widget.deleteLater()
            except Exception:
                pass
        self.active = False
        self.active_backend = "vtk"
        self.active_render_mode = None
        self.vulkan_ready = False
        self.state = BackendState.FALLBACK if self.vulkan_initialized else BackendState.UNAVAILABLE

    # -- status label helpers (for the GUI status-bar indicator) ------------
    @property
    def preview_frame_valid(self) -> bool:
        """True only when the preview swapchain is alive AND at least one
        frame has actually been presented (VULKAN_PRESENT_COUNT > 0) since
        this backend started. Never inferred from intent."""
        return bool(
            self.vulkan_ready
            and self.vulkan_backend is not None
            and self.vulkan_backend.present_count > 0
        )

    def status_label_text(self) -> str:
        if self.active_backend == "vulkan" and self.state == BackendState.ACTIVE:
            mode = (self.active_render_mode or "?").replace("_", " ").title()
            if self.is_displayed and self.preview_frame_valid:
                # Strict wording: only claimed when ALL of the following hold
                # together - widget visible + swapchain valid + a real frame
                # has been presented (VULKAN_PRESENT_COUNT > 0). "Main
                # viewport" when Vulkan is the sole view (VTK hidden beneath),
                # "Preview" on the legacy NAKSHA_VULKAN_PREVIEW split.
                scope = ("Main viewport" if getattr(self, "_viewport_installed", False)
                         else "Preview")
                return f"Vulkan Ã¢â‚¬Â¢ {mode} Ã¢â‚¬Â¢ ACTIVE ({scope})"
            if self.is_displayed:
                # Visible, workload rendered, but not a single frame has
                # been presented through the swapchain yet - warming up.
                return f"Vulkan Ã¢â‚¬Â¢ {mode} Ã¢â‚¬Â¢ PREVIEW (waiting for first frame)"
            # Vulkan did a real render for a real workload, but the user is
            # still LOOKING at VTK (no preview/split view has made the
            # Vulkan widget visible) - do not claim ACTIVE unqualified, that
            # would be a false claim about what's actually on screen.
            return f"Vulkan Ã¢â‚¬Â¢ {mode} Ã¢â‚¬Â¢ TEST/HEADLESS (Display: VTK)"
        if self.active_backend == "vulkan" and self.state == BackendState.READY:
            if self.is_displayed:
                return "Vulkan Ã¢â‚¬Â¢ READY (Preview)"
            return "Vulkan Ã¢â‚¬Â¢ READY (Display: VTK)"
        return "VTK Ã¢â‚¬Â¢ FALLBACK"

    def status_tooltip_text(self) -> str:
        lines = [
            f"Backend: {self.active_backend}",
            f"State: {self.state}",
            f"Displayed to user: {'YES (Vulkan pixels on screen)' if self.is_displayed else 'NO (VTK pixels on screen)'}",
        ]
        if self.active_backend == "vulkan":
            lines.append(f"GPU: {self.gpu_name or 'unknown'}")
            lines.append(f"Frames presented (VULKAN_PRESENT_COUNT): {get_present_count()}")
            lines.append(f"Vulkan API: {self.vulkan_api_version or 'unknown'}")
            lines.append(f"Mode: {self.active_render_mode or 'none'}")
        if self.last_error:
            lines.append(f"Last error: {self.last_error}")
        return "\n".join(lines)
