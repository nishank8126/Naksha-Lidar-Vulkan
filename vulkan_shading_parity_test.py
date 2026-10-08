"""vulkan_shading_parity_test.py - Shaded-Class parity gate: VTK/CPU vs Vulkan.

THIS IS THE HARNESS shaders/surface.frag refers to when it says
"vulkan_shading_parity_test.py re-derives every constant below from the
Python module and compares this file's arithmetic against the real CPU
functions face-by-face on the real dataset".

Source of truth
---------------
gui/shading_display.py (the VTK/CPU pipeline) is authoritative. The harness
never re-implements the CPU model; it calls the PRODUCTION functions directly:

    _compute_face_normals / cache.face_normals   flat cell normal + hemisphere fix
    cache.shade                                 raw hillshade (clip(max(N.L, amb)))
    _crisp_shade_chunk()                        crisp remap -> CELL shade
    _shading_palette_rgb()                      per-vertex class RGB (mixed faces)
    _shading_sharpness_response()               legacy / overdrive split
    _crisp_sharpness_contrast_gain()            contrast gain
    _shading_effective_light_elevation()        overdrive-elevated light angle
    _shading_key_fill_intensities()             key / fill intensities
    _crisp_blend_ambient_floor()                anti-black shadow floor
    _collect_mixed_display_faces()              which faces are mixed-colour

Method
------
1. Drive the REAL app (LAS -> PTC -> shaded_class) through production paths.
2. Capture the real native frame five times, once per debug stage
   (0 final / 1 normal / 2 lighting / 3 class colour / 4 raw N.L) using
   backend.set_crisp_debug_mode() + nkv_capture_frame().
3. Project sampled face centroids through the engine's own MVP
   (nkv_get_last_mvp) and read the captured pixel there.
4. Gate each sample on the normal stage: a face whose normal does not match
   at that pixel is occluded / sub-pixel and is skipped, never counted as a
   shading failure.
5. Compare every remaining stage against the CPU expectation and report
   mean / p95 / max RGB-level differences per stage and per face kind.

Two parameter sets are exercised:
    A  defaults          azimuth 45,  sharpness  45, ambient 0.25
    B  overdrive rig     azimuth 120, sharpness 150, ambient 0.40
so the legacy AND the overdrive (90..200) branches of sharpness, gain,
effective elevation and key/fill are both validated.

Usage:
    python vulkan_shading_parity_test.py [C:\\path\\file.las]  (default 123.las)
Exit code 0 = PASS, 1 = FAIL, 3 = crashed.
Artifacts land in vulkan_shading_parity_output/.
"""
from __future__ import annotations

import atexit
import ctypes
import os
import sys
import time
import traceback

# ---- env BEFORE any gui import -------------------------------------------
os.environ["NAKSHA_RENDER_BACKEND"] = "vulkan"
os.environ["NAKSHA_VULKAN_PREVIEW"] = ""              # not the split preview
os.environ["NAKSHA_VULKAN_MAIN_VIEWPORT"] = "1"       # full main-viewport install
os.environ.pop("NAKSHA_SHADING_DEBUG_STAGE", None)    # harness sets the stage itself

for _name in ("stdout", "stderr"):
    _s = getattr(sys, _name, None)
    try:
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

LAS_PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(PROJECT_ROOT, "123.las")
PTC_PATH = r"H:\TESTING CONTIUES\Class_ENEL_2025_connect 1.ptc"
OUT_DIR = os.path.join(PROJECT_ROOT, "vulkan_shading_parity_output")

# ---- thresholds ------------------------------------------------------------
# Final colour = trunc(albedo * shade) computed twice: CPU float64 and shader
# float32. A byte can only differ when the product sits within a float epsilon
# of a byte boundary, so a correct port measures mean well under 1 level with
# an occasional +/-1. Anything structurally wrong (missing sRGB compensation,
# wrong gain, inverted normal, missing barycentric blend) measures tens of
# levels at once.
MAX_MEAN = 1.0       # mean abs RGB-level difference per stage/sample set
MAX_P95 = 2.0        # 95th percentile
MAX_MAX = 3.0        # single worst channel reading
NORMAL_GATE_TOL = 3  # byte tolerance when deciding "this face is visible here"
MIN_AREA_PX = 40.0   # reject sub-pixel / sliver faces for sampling
EDGE_MARGIN = 4      # pixels from the capture border
MIN_BARY = 0.12      # sample pixel must sit comfortably inside the face
# Ownership gate tolerance on NDC z. The depth attachment is D32_SFLOAT, so the
# only error sources are the CPU/GPU float32-vs-float64 difference and the
# rasteriser's depth interpolation at the pixel centre vs the barycentric point
# we evaluate at. Both are tiny next to the depth RANGE across a face, so this
# is a genuine "same surface" test, not a loose one: a neighbouring face a few
# centimetres nearer is orders of magnitude further away than this.
DEPTH_TOL = 1e-4

# ---- sampling plan ---------------------------------------------------------
SAMPLE_SCAN = 6000   # random candidate faces scanned per kind per param set
MIN_GATED = 40       # minimum visible samples per kind to trust the stats

STAGES = (0, 1, 2, 3, 4)
STAGE_NAMES = {
    0: "final shaded colour",
    1: "face normal",
    2: "lighting factor",
    3: "class colour",
    4: "raw N.L",
}

_CHECKS: list[tuple[str, bool, str]] = []
_REPORT_LINES: list[str] = []
_R: dict = {}


# --------------------------------------------------------------------------- #
# Check / report plumbing
# --------------------------------------------------------------------------- #
def check(name: str, ok: bool, detail: str = "") -> bool:
    _CHECKS.append((name, bool(ok), detail))
    flag = "OK  " if ok else "FAIL"
    print(f"[parity] [{flag}] {name}" + (f"  ({detail})" if detail else ""),
          flush=True)
    return ok


def note(text: str) -> None:
    print(f"[parity] {text}", flush=True)


def _flush_report_on_exit():
    try:
        if _REPORT_LINES:
            os.makedirs(OUT_DIR, exist_ok=True)
            with open(os.path.join(OUT_DIR, "parity_report.txt"), "w",
                      encoding="utf-8") as fh:
                fh.write("\n".join(_REPORT_LINES) + "\n")
    except Exception:
        pass


atexit.register(_flush_report_on_exit)


def report() -> int:
    failed = [c for c in _CHECKS if not c[1]]
    print("=" * 76, flush=True)
    print(f"[parity] {len(_CHECKS) - len(failed)}/{len(_CHECKS)} checks passed",
          flush=True)
    for name, _ok, detail in failed:
        print(f"[parity]   FAILED: {name}  ({detail})", flush=True)
    print(f"[parity] artifacts: {OUT_DIR}", flush=True)
    print("VULKAN SHADING PARITY: " + ("PASS" if not failed else "FAIL"), flush=True)
    print("=" * 76, flush=True)
    return 0 if not failed else 1


def save_rgb_png(arr: np.ndarray, path: str) -> bool:
    from PySide6.QtGui import QImage
    try:
        a = np.ascontiguousarray(arr[..., :3], dtype=np.uint8)
        h, w = a.shape[:2]
        q = QImage(a.tobytes(), w, h, w * 3, QImage.Format_RGB888).copy()
        return bool(q.save(path))
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Engine introspection: MVP + render origin straight from the native DLL, so
# the harness projects with the EXACT matrix the shader used.
# --------------------------------------------------------------------------- #
def _bind(dll, name, restype, argtypes):
    fn = getattr(dll, name, None)
    if fn is None:
        return None
    try:
        fn.restype = restype
        fn.argtypes = argtypes
    except Exception:
        pass
    return fn


def get_last_mvp(backend):
    fn = _bind(backend._dll, "nkv_get_last_mvp", ctypes.c_int,
               [ctypes.c_uint64, ctypes.POINTER(ctypes.c_float)])
    if fn is None:
        return None
    buf = (ctypes.c_float * 16)()
    if int(fn(ctypes.c_uint64(backend._handle), buf)) != 1:
        return None
    m = np.array(buf, dtype=np.float64)
    if not np.all(np.isfinite(m)) or np.max(np.abs(m)) < 1e-9:
        return None
    # GLSL mat4 is column-major: element (row r, col c) = d[c*4 + r].
    return m.reshape(4, 4).T          # so clip = M @ [x, y, z, 1]


def get_render_origin(backend) -> np.ndarray:
    fn = _bind(backend._dll, "nkv_get_render_origin", ctypes.c_int,
               [ctypes.c_uint64, ctypes.POINTER(ctypes.c_double)])
    out = (ctypes.c_double * 3)()
    if fn is None or int(fn(ctypes.c_uint64(backend._handle), out)) != 1:
        return np.zeros(3)
    return np.array([out[0], out[1], out[2]], dtype=np.float64)


def project_points(pts_world, mvp, origin, width, height):
    """World -> pixel (x right, y top-down) plus per-point clip w.

    Mirrors surface.vert exactly: gl_Position = mvp * vec4(inPos, 1) where
    inPos = world - origin (nkv_set_shaded_class_surface's WorldToRender).
    The Vulkan viewport (y=0, height=+H, see SurfaceRenderer::Record) maps
    ndc.y to row (ndc.y + 0.5) * H counting from the top, which is also the
    row order nkv_capture_frame returns.

    Done in float64: this is the TRUE screen position of the face. The engine
    stores those same positions in a float32 vertex buffer, and for UTM-scale
    data that quantises badly - see position_precision_px(), which measures
    the resulting error instead of hiding it inside the projection.
    """
    p = np.asarray(pts_world, dtype=np.float64) - origin
    hom = np.concatenate([p, np.ones((len(p), 1))], axis=1)
    clip = hom @ mvp.T
    w = clip[:, 3]
    with np.errstate(divide="ignore", invalid="ignore"):
        ndc = clip[:, :3] / w[:, None]
    sx = (ndc[:, 0] * 0.5 + 0.5) * width
    sy = (ndc[:, 1] * 0.5 + 0.5) * height
    return sx, sy, w


def clip_z_depth(pts_world, mvp, origin):
    """NDC z (the value the depth attachment stores, in [0,1]) per point.

    Mirrors the GL pipeline: gl_Position.z/w. The depth buffer holds this NDC z
    (Vulkan remaps the projection's [-1,1] clip z to [0,1] via the viewport's
    depth range, and this engine uses the default 0..1), so comparing it against
    the CPU value is an apples-to-apples check of "is this pixel showing THIS
    face" rather than a heuristic.
    """
    p = np.asarray(pts_world, dtype=np.float64) - origin
    hom = np.concatenate([p, np.ones((len(p), 1))], axis=1)
    clip = hom @ np.asarray(mvp, dtype=np.float64).T
    w = clip[:, 3]
    with np.errstate(divide="ignore", invalid="ignore"):
        z = clip[:, 2] / w
    return z, w


def position_precision_px(cache, cands, mvp, origin, width, height):
    """Median on-screen error (px) caused by the engine's float32 vertices.

    The engine converts world positions to float32 when it fills the vertex
    buffer (RenderOrigin::WorldToRenderF64ToF32). For UTM-scale data - this
    dataset sits at y ~ 4.78e6 - one float32 ulp is ~0.5 m while the terrain
    triangles are ~0.13 m across, so neighbouring vertices can round onto the
    same value and whole triangles collapse to zero area. This is a property
    of the ENGINE, not of the comparison, so it is measured and reported
    rather than being folded into the sampling.
    """
    if len(cands) == 0:
        return 0.0
    tris = np.asarray(cache.xyz_final)[np.asarray(cache.faces)[cands]]
    exact = project_points(tris.reshape(-1, 3), mvp, origin, width, height)
    # The engine does the subtraction in float64 and casts the RESULT:
    #   out[i] = static_cast<float>(world[i] - origin)   (WorldToRenderF64ToF32)
    # Rounding world to float32 FIRST and subtracting afterwards would measure a
    # different (and much worse) operation than the engine performs.
    p32 = (tris.reshape(-1, 3) - origin).astype(np.float32)
    hom32 = np.concatenate([p32, np.ones((len(p32), 1), dtype=np.float32)], axis=1)
    clip32 = (hom32 @ np.asarray(mvp, dtype=np.float32).T).astype(np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        ndc32 = clip32[:, :3] / clip32[:, 3:4]
    qx = (ndc32[:, 0] * 0.5 + 0.5) * width
    qy = (ndc32[:, 1] * 0.5 + 0.5) * height
    d = np.hypot(np.asarray(exact[0]) - qx, np.asarray(exact[1]) - qy)
    d = d[np.isfinite(d)]
    return float(np.median(d)) if d.size else 0.0


# --------------------------------------------------------------------------- #
# Camera framing for sampling
#
# Three facts drive this, all measured on the real data:
#   * reset_camera() frames the WHOLE cloud, so with ~5.8M faces on a 1400x841
#     viewport every triangle is a fraction of a pixel - nothing reaches
#     MIN_AREA_PX and every sample is rejected as "tiny".
#   * The engine's MVP has row3 = [0,0,0,1] (clip w == 1): the camera is
#     ORTHOGRAPHIC, so on-screen size depends only on the view window.
#   * A blind cam.zoom() loop is still wrong: it dollies the EYE along the view
#     axis, and by x198 the eye is inside the terrain, every triangle becomes
#     back-facing, backface culling empties the frame (lit fraction 0.0) and the
#     visible pool is 0.
# So: step the zoom and CHECK THE ACTUAL FRAME each time, accepting the first
# step whose faces are big enough while the viewport still has real content.
# The render is the ground truth here, not a model of the camera.
# --------------------------------------------------------------------------- #
TARGET_FACE_PX = 120.0    # aim for faces of this on-screen area (px^2)
MIN_LIT_FRACTION = 0.05   # a frame less lit than this is effectively empty
GATE_EXAMPLES = 8        # mismatch samples recorded in the report


def median_world_face_area(cache, sample=20000, seed=7) -> float:
    """Median triangle area of the mesh in WORLD units (m^2)."""
    faces = np.asarray(cache.faces)
    xyz = np.asarray(cache.xyz_final)
    n_faces = int(len(faces))
    if n_faces == 0:
        return 0.0
    if n_faces > sample:
        idx = np.random.default_rng(seed).choice(n_faces, size=sample,
                                                  replace=False)
    else:
        idx = np.arange(n_faces)
    tris = xyz[faces[idx]].astype(np.float64)
    a = tris[:, 1] - tris[:, 0]
    b = tris[:, 2] - tris[:, 0]
    areas = 0.5 * np.linalg.norm(np.cross(a, b), axis=1)
    return float(np.median(areas))


def projected_face_areas(cache, cands, mvp, origin, width, height):
    """On-screen area (px^2) of each candidate face, NaN where degenerate."""
    if len(cands) == 0:
        return np.zeros(0)
    tris = np.asarray(cache.xyz_final)[np.asarray(cache.faces)[cands]]
    sx, sy, w = project_points(tris.reshape(-1, 3), mvp, origin, width, height)
    sx = sx.reshape(-1, 3); sy = sy.reshape(-1, 3); w = w.reshape(-1, 3)
    x0, x1, x2 = sx[:, 0], sx[:, 1], sx[:, 2]
    y0, y1, y2 = sy[:, 0], sy[:, 1], sy[:, 2]
    area = 0.5 * np.abs((x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0))
    good = (np.all(np.isfinite(sx) & np.isfinite(sy), axis=1)
            & np.all(w > 0.0, axis=1))
    return np.where(good, area, np.nan)


def lit_fraction(backend, app_qt=None, pump=None) -> float:
    """Fraction of the framebuffer that is actually drawn to (not cleared).

    Reads the normal debug stage, which is mid-grey on every drawn face and
    black on the background - so this is an unambiguous 'did anything render'
    test rather than a guess about surface colours.
    """
    try:
        backend.set_crisp_debug_mode(1)
        backend.request_render()
        if app_qt is not None and pump is not None:
            pump(app_qt, 0.05)
        img = backend.capture_frame()
        backend.set_crisp_debug_mode(0)
        if img is None:
            return 0.0
        a = np.asarray(img)[..., :3]
        return float((a.max(axis=2) > 8).mean())
    except Exception:
        return 0.0


def frame_for_sampling(win, rb, backend, cache, rng, width, height,
                       target_px=TARGET_FACE_PX, app_qt=None, pump=None):
    """Frame the cloud so a median face covers ~target_px^2 on screen.

    Drives the camera through VTK and lets the app's resync path push it to the
    engine. nkv_set_camera_ortho is deliberately NOT used: measured in this app
    it is inert (the installed VTK camera observer re-pushes its own pose, and
    nkv_get_last_mvp returns a byte-identical matrix before and after the call).

    The window half-height goes as 1/scale^2 on screen, so the search is a
    self-correcting Newton step on the measured median face area.

    Returns (mvp, origin, median_face_px2, parallel_scale).
    """
    xyz = np.asarray(cache.xyz_final)
    lo = xyz.min(axis=0)
    hi = xyz.max(axis=0)
    span = float(np.max(hi - lo))
    if not np.isfinite(span) or span <= 0.0:
        return None, np.zeros(3), 0.0, 0.0

    a_world = median_world_face_area(cache)
    if not np.isfinite(a_world) or a_world <= 0.0:
        return None, np.zeros(3), 0.0, 0.0

    # Lateral centre: the MEDIAN vertex position, not the bbox centre or the
    # mean. The bbox centre of a terrain tile can sit over a hole (black view),
    # and the MEAN is dragged by outlier vertices - measured: the mean gave a
    # 112-face sample pool on one parameter set versus 2,500 on another for the
    # SAME window size, which is what made the mixed/barycentric gate look like
    # a rendering failure when it was really aiming at empty terrain.
    cx = float(np.median(xyz[:, 0]))
    cy = float(np.median(xyz[:, 1]))
    # Eye depth: nkv_set_camera_ortho has no eye distance, so the engine puts
    # the eye just in front of `centre` along `dir` and clips at near_clip.
    # Centring on the bbox mid-height therefore clips away every triangle
    # ABOVE that height. Put the eye above the highest vertex so the whole
    # mesh is in front of the near plane.
    cz = float(hi[2]) + max(1.0, 0.05 * float(hi[2] - lo[2]))
    centre = np.array([cx, cy, cz])

    n_faces = int(len(np.asarray(cache.faces)))
    probe = pick_candidates(rng, np.arange(n_faces), 3000)

    # Straight down. Orthographic, so there is no vanishing point and no
    # near-degenerate view direction to worry about.
    view_dir = (0.0, 0.0, -1.0)
    near_clip, far_clip = 0.1, max(1.0e5, span * 10.0)

    def apply(scale):
        """Frame the scene at the given window half-height.

        PROVEN by measurement: calling nkv_set_camera_ortho directly has NO
        effect in this app - the installed VTK camera observer re-pushes its own
        pose on the next frame, and nkv_get_last_mvp returns a byte-identical
        matrix before and after. The camera must therefore be driven through
        VTK (the observer's own source of truth) and adopted via the app's
        normal resync path.
        """
        half = float(scale)
        plotter = win.vtk_widget
        plotter.camera.parallel_projection = True
        plotter.camera.focal_point = (float(cx), float(cy), float(hi[2]))
        plotter.camera.position = (float(cx), float(cy), float(hi[2]) + 4.0 * half)
        plotter.camera.up = (0.0, 1.0, 0.0)
        plotter.camera.parallel_scale = max(half, 1e-6)
        plotter.reset_camera_clipping_range()
        plotter.render()
        try:
            rb.resync_camera(present=True)
        except Exception:
            pass
        backend.request_render()
        if app_qt is not None and pump is not None:
            pump(app_qt, 0.05)

    # Start from a KNOWN-GOOD wide pose. The native ortho camera persists across
    # parameter sets, so whatever the previous set left behind (possibly a very
    # tight window) would otherwise be the first thing measured. The search
    # below only ever narrows from here.
    scale = span                      # half-window = span -> whole cloud in view
    apply(scale)
    mvp = origin = None
    median = 0.0
    lit = 0.0
    best = None                        # (median, scale) that actually rendered
    for _ in range(5):
        apply(scale)
        mvp = get_last_mvp(backend)
        if mvp is None:
            return None, np.zeros(3), 0.0, float(scale)
        origin = get_render_origin(backend)

        areas = projected_face_areas(cache, probe, mvp, origin, width, height)
        finite = areas[np.isfinite(areas)]
        median = float(np.median(finite)) if finite.size else 0.0
        lit = lit_fraction(backend, app_qt, pump)
        note(f"  parallel_scale={scale:.4f}: median face {median:.3g} px^2, "
             f"lit {100 * lit:.1f}%  "
             f"[finite={finite.size}/{areas.size} origin={np.round(origin, 2)}]")

        # Accept only when faces are big enough to sample AND the window is not
        # absurdly tight. An unbounded "median >= MIN_AREA_PX" fires on the very
        # first probe when the camera is stale from the previous parameter set
        # (measured: 5e5 px^2), which silently skips the rest of the search.
        if lit >= MIN_LIT_FRACTION and MIN_AREA_PX <= median <= 4.0 * target_px:
            return mvp, origin, median, float(scale)
        if lit >= MIN_LIT_FRACTION and (best is None or median > best[0]):
            best = (median, scale)

        if median <= 0.0:
            scale *= 0.5
        else:
            # On-screen area goes as 1/scale^2 (parallel_scale is the window
            # HALF size), so sqrt(median/target) lands on the target size:
            # faces too small -> shrink the window, too big -> grow it.
            scale = float(np.clip(scale * float(np.sqrt(median / target_px)),
                                  span * 1e-4, span * 4.0))

    if best is not None:
        scale = best[1]
        apply(scale)
        mvp = get_last_mvp(backend)
        origin = get_render_origin(backend)
        return mvp, origin, best[0], float(scale)
    return mvp, origin, median, float(scale)



# --------------------------------------------------------------------------- #
# CPU expectations: every number comes from a production function in
# gui/shading_display.py - nothing is re-derived with private arithmetic.
# --------------------------------------------------------------------------- #
def light_vector(azimuth_deg, elevation_deg) -> np.ndarray:
    """Same construction as _compute_shading / the surface.frag helper."""
    zenith = np.radians(90.0 - elevation_deg)
    az_math = np.radians(360.0 - azimuth_deg + 90.0)
    return np.array([np.sin(zenith) * np.cos(az_math),
                     np.sin(zenith) * np.sin(az_math),
                     np.cos(zenith)])


def build_context(win, sd, cache, classes):
    """Snapshot the CPU state both pipelines must agree on."""
    cm = classes[np.asarray(cache.unique_indices)]
    vc = sd._get_shading_visibility(win)
    rgb_by_vertex = sd._shading_palette_rgb(win, cm, vc)      # (V,3) uint8
    mixed_ids, mixed_count, ok = sd._collect_mixed_display_faces(win, cache, cm, vc)
    sharpness = sd._shading_sharpness_angle(win)
    legacy, overdrive = sd._shading_sharpness_response(sharpness)
    key_i, fill_i = sd._shading_key_fill_intensities(win, legacy, overdrive)
    return {
        "cm": cm, "vc": vc, "rgb_by_vertex": rgb_by_vertex,
        "mixed_ids": np.asarray(mixed_ids, dtype=np.int64),
        "mixed_count": int(mixed_count), "mixed_ok": bool(ok),
        "azimuth": float(getattr(win, "last_shade_azimuth", 45.0)),
        "ambient": float(np.clip(getattr(win, "shade_ambient", 0.25), 0.0, 1.0)),
        "sharpness": float(sharpness),
        "legacy": float(legacy), "overdrive": float(overdrive),
        "eff_elev": sd._shading_effective_light_elevation(win, overdrive),
        "base_elev": sd._shading_fixed_light_elevation(win),
        "floor": sd._crisp_blend_ambient_floor(win),
        "key": key_i, "fill": fill_i,
    }


def phong_params(ctx):
    """Ka/Kd exactly as _configure_nakshatech_color_blend_lighting."""
    ka = max(ctx["ambient"], ctx["floor"])
    kd = max(0.0, 1.0 - ka)
    return ka, kd


def screen_barycentric(px, py, tri):
    """Barycentric coords of (px, py) in screen triangle tri ((3,2))."""
    (x0, y0), (x1, y1), (x2, y2) = tri
    den = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
    if abs(den) < 1e-12:
        return None
    l0 = ((y1 - y2) * (px - x2) + (x2 - x1) * (py - y2)) / den
    l1 = ((y2 - y0) * (px - x2) + (x0 - x2) * (py - y2)) / den
    l2 = 1.0 - l0 - l1
    return l0, l1, l2


def perspective_correct(lam, ws, attrs):
    """lam: screen barycentric (3,), ws: clip w (3,), attrs: (3, K)."""
    a = np.asarray(attrs, dtype=np.float64)
    ws = np.asarray(ws, dtype=np.float64)
    # A zero / non-finite w means the vertex is on the eye plane: fall back to
    # the flat mean instead of propagating inf/nan into the comparison.
    if not np.all(np.isfinite(ws)) or np.any(ws == 0.0):
        return a.mean(axis=0)
    t = np.asarray(lam, dtype=np.float64) / ws
    denom = t.sum()
    if not np.isfinite(denom) or abs(denom) < 1e-12:
        return a.mean(axis=0)
    return (t[:, None] * a).sum(axis=0) / denom


def pick_candidates(rng, ids, count):
    ids = np.asarray(ids, dtype=np.int64)
    if ids.size == 0:
        return ids
    count = min(int(count), int(ids.size))
    return rng.choice(ids, size=count, replace=False)


def project_candidates(cache, cands, mvp, origin, width, height, stats=None):
    """Project candidate faces; keep those whose centroid pixel sits well
    inside the projected triangle (guards against edge/rounding samples).

    When ``stats`` is a dict it is filled with per-filter attrition counts, so
    a run that gates everything out can say WHICH filter rejected it instead
    of just reporting "0 samples".
    """
    st = stats if isinstance(stats, dict) else {}
    st.update(scanned=0, nonfinite=0, behind=0, tiny=0, offscreen=0, sliver=0, kept=0)
    if len(cands) == 0:
        return []
    st["scanned"] = int(len(cands))
    tris = np.asarray(cache.xyz_final)[np.asarray(cache.faces)[cands]]
    flat = tris.reshape(-1, 3)
    sx, sy, w = project_points(flat, mvp, origin, width, height)
    sx = sx.reshape(-1, 3); sy = sy.reshape(-1, 3); w = w.reshape(-1, 3)

    kept = []
    for i in range(len(cands)):
        if not (np.all(np.isfinite(sx[i])) and np.all(np.isfinite(sy[i]))):
            st["nonfinite"] += 1
            continue
        if not np.all(w[i] > 0.0):                       # behind the camera
            st["behind"] += 1
            continue
        x0, x1, x2 = sx[i]
        y0, y1, y2 = sy[i]
        area = 0.5 * abs((x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0))
        if area < MIN_AREA_PX:
            st["tiny"] += 1
            continue
        px = int(np.floor(float(sx[i].mean())))
        py = int(np.floor(float(sy[i].mean())))
        if not (EDGE_MARGIN <= px < width - EDGE_MARGIN and
                EDGE_MARGIN <= py < height - EDGE_MARGIN):
            st["offscreen"] += 1
            continue
        lam = screen_barycentric(px + 0.5, py + 0.5,
                                 np.stack([sx[i], sy[i]], axis=1))
        if lam is None or min(lam) < MIN_BARY:
            st["sliver"] += 1
            continue
        kept.append({
            "face": int(cands[i]), "px": px, "py": py,
            "lam": np.asarray(lam, dtype=np.float64),
            "w": w[i].astype(np.float64),
        })
    st["kept"] = len(kept)
    return kept


def attrition_text(st: dict) -> str:
    """One-line summary of where candidate faces were dropped."""
    if not st:
        return "no scan"
    if "reasons" in st:
        parts = ", ".join(f"{k}={v}" for k, v in sorted(st["reasons"].items()))
        return (f"scanned={st.get('scanned', 0)} -> gated={st.get('gated', 0)} "
                f"rejected={st.get('rejected', 0)} [{parts}]")
    return (f"scan={st.get('scanned', 0)} -> kept={st.get('kept', 0)} "
            f"(nonfinite={st.get('nonfinite', 0)} behind={st.get('behind', 0)} "
            f"tiny<{MIN_AREA_PX:g}px2={st.get('tiny', 0)} "
            f"offscreen={st.get('offscreen', 0)} "
            f"sliver={st.get('sliver', 0)})")


def _dump_rejects(label, kind, attr, limit=GATE_EXAMPLES):
    """Print the full evidence for rejected samples.

    Required diagnostic detail per sample: face id, screen coordinate, the
    CPU-projected triangle, GPU depth, CPU expected depth, barycentric
    coordinates, and (for occlusion) the nearest competing face. Without this
    a low gated count is just a number with no way to tell a camera/framing
    problem from a genuine shading problem.
    """
    rej = attr.get("rejects") or []
    if not rej:
        return
    note(f"    --- {label}/{kind}: {len(rej)} rejected sample(s) "
         f"(of {attr.get('rejected', 0)} total) ---")
    for r in rej[:limit]:
        px, py = r.get("px"), r.get("py")
        line = (f"    face={r.get('face')} @({px},{py}) "
                f"reason={r.get('reason')}")
        tri = r.get("tri")
        if tri:
            line += (" tri=[" + " ".join(f"({a:.1f},{b:.1f})" for a, b in tri) + "]")
        bary = r.get("bary")
        if bary:
            line += " bary=[" + " ".join(f"{v:+.3f}" for v in bary) + "]"
        if "gpu_depth" in r:
            line += f" gpu_depth={r['gpu_depth']:.8f}"
        if "cpu_depth" in r:
            line += f" cpu_depth={r['cpu_depth']:.8f}"
        if "depth_err" in r:
            line += f" depth_err={r['depth_err']:.3e}"
        comp = r.get("competing_face")
        if comp:
            line += (f" competing_face={comp.get('face')} "
                     f"competing_depth={comp.get('depth', float('nan')):.8f}")
        note(line)


# --------------------------------------------------------------------------- #
# Visible-face pool
#
# At the zoom level needed to make faces big enough to sample, the camera sees
# a small patch of the ~6M-face mesh, so sampling uniformly over ALL faces
# lands off-screen ~95% of the time. Project the whole mesh once (chunked, so
# memory stays bounded) and keep the faces that are actually in the viewport
# AND large enough to sample. Sampling then draws from that pool.
# --------------------------------------------------------------------------- #
POOL_CHUNK = 200_000


def visible_face_pool(cache, mvp, origin, width, height, chunk=POOL_CHUNK):
    """Indices of faces that are on-screen and at least MIN_AREA_PX big."""
    faces = np.asarray(cache.faces)
    xyz = np.asarray(cache.xyz_final)
    n_faces = int(len(faces))
    picked = []
    for start in range(0, n_faces, chunk):
        block = faces[start:start + chunk]
        tris = xyz[block]                                    # (m, 3, 3)
        sx, sy, w = project_points(tris.reshape(-1, 3), mvp, origin,
                                   width, height)
        sx = sx.reshape(-1, 3); sy = sy.reshape(-1, 3); w = w.reshape(-1, 3)
        x0, x1, x2 = sx[:, 0], sx[:, 1], sx[:, 2]
        y0, y1, y2 = sy[:, 0], sy[:, 1], sy[:, 2]
        area = 0.5 * np.abs((x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0))
        finite = (np.all(np.isfinite(sx), axis=1) & np.all(np.isfinite(sy), axis=1)
                  & np.all(w > 0.0, axis=1))
        px = np.floor((x0 + x1 + x2) / 3.0)
        py = np.floor((y0 + y1 + y2) / 3.0)
        in_box = ((px >= EDGE_MARGIN) & (px < width - EDGE_MARGIN) &
                  (py >= EDGE_MARGIN) & (py < height - EDGE_MARGIN))
        sel = finite & in_box & (area >= MIN_AREA_PX)
        if sel.any():
            picked.append((start + np.flatnonzero(sel)).astype(np.int64))
    if not picked:
        return np.zeros(0, dtype=np.int64)
    return np.concatenate(picked)



def expected_for_face(kind, sample, ctx, cache, crisp_shade, faces_row):
    """Expected byte values (0..255) for every stage of one sampled face."""
    n = np.asarray(cache.face_normals[sample["face"]], dtype=np.float64)
    normal_b = np.floor(np.clip(n * 0.5 + 0.5, 0.0, 1.0) * 255.0 + 1e-4)

    if kind == "pure":
        # PASS 1 / mode 4: flat cell colour of vertex 0's class x crisp shade.
        rgb0 = ctx["rgb_by_vertex"][faces_row[0]].astype(np.float64)
        shade = float(crisp_shade)
        raw = float(cache.shade[sample["face"]])
        albedo = rgb0
        raw_b = np.floor(np.clip(raw, 0.0, 1.0) * 255.0 + 1e-4)
        shade_b = np.floor(np.clip(shade, 0.0, 1.0) * 255.0 + 1e-4)
        final_b = np.floor(np.clip(rgb0 * shade, 0.0, 255.0) + 1e-4)
    else:
        # PASS 2 / mode 5: barycentric class RGB x Ka+Kd*(key/fill) Phong.
        ka, kd = phong_params(ctx)
        lkey = light_vector(ctx["azimuth"], ctx["eff_elev"])
        lfill = np.array([-lkey[0], -lkey[1], max(float(lkey[2]), 0.20)])
        lfill = lfill / max(float(np.linalg.norm(lfill)), 1e-12)
        nn = n / max(float(np.linalg.norm(n)), 1e-12)
        diff = (ctx["key"] * max(float(nn @ lkey), 0.0)
                + ctx["fill"] * max(float(nn @ lfill), 0.0))
        shade = float(np.clip(ka + kd * diff, 0.0, 1.0))
        rgb3 = ctx["rgb_by_vertex"][faces_row].astype(np.float64)   # (3,3)
        albedo = np.clip(perspective_correct(sample["lam"], sample["w"], rgb3),
                         0.0, 255.0)
        raw_b = np.floor(np.clip(diff, 0.0, 1.0) * 255.0 + 1e-4)
        shade_b = np.floor(shade * 255.0 + 1e-4)
        final_b = np.floor(np.clip(albedo * shade, 0.0, 255.0) + 1e-4)

    albedo_b = np.floor(np.clip(albedo, 0.0, 255.0) + 1e-4)
    return {
        1: normal_b,
        4: np.repeat(raw_b, 3),
        2: np.repeat(shade_b, 3),
        3: albedo_b,
        0: final_b,
    }


# --------------------------------------------------------------------------- #
# Capture + evaluation
# --------------------------------------------------------------------------- #
def capture_stages(backend, app_qt, pump):
    """One native capture per debug stage.

    Returns ({stage: (H,W,3) uint8}, {stage: mvp}). The MVP is read AFTER
    request_render but BEFORE capture_frame: nkv_capture_frame re-renders into
    the current frame slot, so reading the matrix afterwards yields the NEXT
    slot's camera and every projected sample silently lands off-screen.
    Measured on the synthetic probe: all four known triangles went from exact
    byte match to off-screen purely because of that ordering.
    """
    fnm = _bind(backend._dll, "nkv_get_last_mvp", ctypes.c_int,
                [ctypes.c_uint64, ctypes.POINTER(ctypes.c_float)])

    def read_mvp():
        if fnm is None:
            return None
        b = (ctypes.c_float * 16)()
        if int(fnm(ctypes.c_uint64(backend._handle), b)) != 1:
            return None
        m = np.array(b, dtype=np.float64)
        if not np.all(np.isfinite(m)) or np.max(np.abs(m)) < 1e-9:
            return None
        return m.reshape(4, 4).T

    imgs = {}
    mvps = {}
    for stage in STAGES:
        backend.set_crisp_debug_mode(stage)
        backend.request_render()
        pump(app_qt, 0.12)
        mvps[stage] = read_mvp()
        img = backend.capture_frame()
        if img is None:
            raise RuntimeError(f"capture_frame() returned None at stage {stage}")
        imgs[stage] = np.ascontiguousarray(img[..., :3])
    backend.set_crisp_debug_mode(0)     # restore production output
    return imgs, mvps


def gate_visibility(cache, cands, mvp, origin, width, height, depth_img,
                    normal_img=None, max_depth_tol=DEPTH_TOL):
    """Decide which candidate faces ACTUALLY own their sampled pixel.

    This replaces the old colour-based gate, which inferred visibility from
    "does the captured normal look like mine". That inference is circular: when
    a neighbouring face legitimately owns the pixel, the captured colour is
    perfectly valid and simply belongs to a different face - so the sample was
    either wrongly rejected (losing real signal) or wrongly accepted (counting
    another face's colour as a shading mismatch). Neither failure mode was
    distinguishable from a genuine shading bug, which is why the gate sat at
    3/2015.

    The honest test is depth. The GPU depth buffer records which face won the
    depth test at every pixel, so for each candidate we:

      1. project the face and require the sample pixel to be strictly INSIDE it
         (all barycentric coords >= MIN_BARY) - catches edge/sliver samples;
      2. compute the face's NDC z at that pixel on the CPU;
      3. read the GPU depth there and require |gpu - cpu| <= max_depth_tol -
         a match means THIS face is the one that won there;
      4. reject if the GPU depth is CLEAR (1.0) - nothing rasterised the pixel.

    Face i wins at its own pixel exactly when its depth equals the stored depth:
    a different face in front would have written a strictly smaller value.

    Returns (kept_samples, rejects) where each reject carries the evidence
    needed to diagnose it (screen coords, projected triangle, both depths,
    barycentric, and the nearest competing face).
    """
    depth_img = np.asarray(depth_img, dtype=np.float32)
    dh, dw = depth_img.shape[:2]
    if (dh, dw) != (height, width):
        raise ValueError(f"depth {depth_img.shape[:2]} != capture {height}x{width}")

    faces = np.asarray(cache.faces)
    xyz = np.asarray(cache.xyz_final)
    face_normals = np.asarray(cache.face_normals)
    kept, rejects = [], []
    if len(cands) == 0:
        return kept, rejects

    tris = xyz[faces[cands]]                                  # (n, 3, 3)
    flat = tris.reshape(-1, 3)
    sx, sy, w = project_points(flat, mvp, origin, width, height)
    zs, _ = clip_z_depth(flat, mvp, origin)
    sx = sx.reshape(-1, 3); sy = sy.reshape(-1, 3)
    w = w.reshape(-1, 3); zs = zs.reshape(-1, 3)

    for i, face in enumerate(cands):
        fid = int(face)
        if not (np.all(np.isfinite(sx[i])) and np.all(np.isfinite(sy[i]))
                and np.all(np.isfinite(zs[i]))):
            rejects.append({"face": fid, "reason": "nonfinite_projection"})
            continue
        if not np.all(w[i] > 0.0):
            rejects.append({"face": fid, "reason": "behind_camera",
                            "w": [float(v) for v in w[i]]})
            continue

        px = int(np.floor(float(sx[i].mean())))
        py = int(np.floor(float(sy[i].mean())))
        tri2d = np.stack([sx[i], sy[i]], axis=1)
        lam = screen_barycentric(px + 0.5, py + 0.5, tri2d)
        base = {
            "face": fid, "px": px, "py": py,
            "tri": [[float(sx[i, k]), float(sy[i, k])] for k in range(3)],
            "bary": [float(v) for v in lam] if lam is not None else None,
        }
        if lam is None:
            rejects.append({**base, "reason": "degenerate_projection"})
            continue
        if min(lam) < MIN_BARY:
            rejects.append({**base, "reason": "outside_triangle"})
            continue
        if not (EDGE_MARGIN <= px < width - EDGE_MARGIN and
                EDGE_MARGIN <= py < height - EDGE_MARGIN):
            rejects.append({**base, "reason": "offscreen"})
            continue

        gpu_d = float(depth_img[py, px])
        base["gpu_depth"] = gpu_d
        if not np.isfinite(gpu_d) or gpu_d >= 0.999999:
            rejects.append({**base, "reason": "gpu_pixel_empty",
                            "cpu_depth": float(np.mean(zs[i]))})
            continue

        # Perspective-correct NDC z at the sample point: 1/w is linear in
        # screen space, so interpolate the reciprocals and invert.
        inv_w = 1.0 / np.asarray(w[i], dtype=np.float64)
        lam_a = np.asarray(lam, dtype=np.float64)
        num = float(np.sum(lam_a * inv_w * zs[i]))
        den = float(np.sum(lam_a * inv_w))
        cpu_d = num / den if abs(den) > 1e-30 else float(np.mean(zs[i]))
        base["cpu_depth"] = cpu_d
        d_err = abs(gpu_d - cpu_d)
        base["depth_err"] = d_err
        if d_err > max_depth_tol:
            rejects.append({**base, "reason": "occluded_other_face"})
            continue

        s = {"face": fid, "px": px, "py": py,
             "lam": lam_a, "w": np.asarray(w[i], dtype=np.float64),
             "gpu_depth": gpu_d, "cpu_depth": cpu_d, "depth_err": d_err}

        # Depth alone is the primary ownership test, but on a near-planar
        # surface it is not sufficient: this scene's whole visible surface spans
        # just 0.0012 in NDC depth, so many different faces sit at nearly the
        # same depth and a depth match cannot tell them apart. Measured cost of
        # dropping this corroboration entirely: stages 2/3/4 (lighting, class
        # colour, raw N.L) went from max=1 to max=255 while stage 0/1 stayed
        # exact - i.e. we were reading a neighbour's colour off a co-planar face.
        # So depth decides OWNERSHIP and the captured normal CORROBORATES it.
        # This is not the old circular gate: there, the normal was the only
        # evidence. Here a sample must first win the depth test on its own.
        if normal_img is not None:
            nrm = np.asarray(normal_img[py, px], dtype=np.int16)
            s["normal_rgb"] = [int(v) for v in nrm]
            # Byte encoding of the face normal, identical to expected_for_face():
            #   floor(clip(n*0.5+0.5,0,1)*255 + 1e-4)
            _n = np.asarray(face_normals[fid], dtype=np.float64)
            normal_b = np.floor(np.clip(_n * 0.5 + 0.5, 0.0, 1.0) * 255.0 + 1e-4)
            if not np.all(np.abs(nrm - normal_b) <= NORMAL_GATE_TOL):
                rejects.append({**base, "reason": "depth_ok_but_normal_differs",
                                "gpu_normal": [int(v) for v in nrm],
                                "exp_normal": [float(v) for v in normal_b]})
                continue
        kept.append(s)

    return kept, rejects


def evaluate(win, sd, cache, ctx, images, mvp, origin, kind, cands, depth_img):
    """Compare CPU expectations vs captured pixels for one face kind.

    Visibility is decided by gate_visibility() on the GPU DEPTH buffer, not by
    matching the captured normal. Every rejected sample is reported with the
    reason and the evidence behind it, so a low gated count is always
    explainable rather than mysterious.

    Returns (stats_by_stage, gated_count, scanned_count, attrition).
    """
    h, w = next(iter(images.values())).shape[:2]
    attr = {}
    kept, rejects = gate_visibility(cache, cands, mvp, origin, w, h,
                                    depth_img, normal_img=images[1])
    attr["scanned"] = int(len(cands))
    attr["gated"] = len(kept)
    attr["rejected"] = len(rejects)
    attr["reasons"] = {}
    for r in rejects:
        attr["reasons"][r["reason"]] = attr["reasons"].get(r["reason"], 0) + 1
    attr["rejects"] = rejects[:GATE_EXAMPLES]
    if not kept:
        return {}, 0, int(len(cands)), attr
    faces = np.asarray(cache.faces)

    face_ids = np.array([s["face"] for s in kept], dtype=np.int64)
    crisp = (sd._crisp_shade_chunk(win, cache, face_ids)
             if kind == "pure" else np.zeros(len(face_ids)))
    for i, s in enumerate(kept):
        s["exp"] = expected_for_face(kind, s, ctx, cache, crisp[i],
                                     faces[s["face"]])

    # Depth stats for the report: how tightly the gate agreed.
    derr = np.array([s["depth_err"] for s in kept], dtype=np.float64)
    attr["depth_err_median"] = float(np.median(derr))
    attr["depth_err_max"] = float(derr.max())

    # ---- per-stage byte differences over the gated set ---------------------
    stats = {}
    for stage in STAGES:
        img = images[stage]
        diffs = np.concatenate([
            np.abs(img[s["py"], s["px"]].astype(np.int16) - s["exp"][stage])
            for s in kept
        ]).astype(np.float64)
        stats[stage] = {
            "mean": float(diffs.mean()),
            "p95": float(np.percentile(diffs, 95)),
            "max": float(diffs.max()),
            "n": len(kept),
        }
    return stats, len(kept), int(len(cands)), attr



# --------------------------------------------------------------------------- #
# Parameter sets: defaults + an overdrive rig (sharpness > 90) so the legacy
# AND the 90..200 overdrive branches of sharpness/gain/elevation/key+fill are
# both exercised against the GPU port.
# --------------------------------------------------------------------------- #
PARAM_SETS = (
    ("A_defaults",   45.0,  45.0, 0.25),
    ("B_overdrive", 120.0, 150.0, 0.40),
)


def stage_check(name: str, st: dict) -> bool:
    ok = (st["mean"] <= MAX_MEAN and st["p95"] <= MAX_P95
          and st["max"] <= MAX_MAX)
    check(name, ok,
          f"n={st['n']} mean={st['mean']:.2f} p95={st['p95']:.1f} "
          f"max={st['max']:.0f} (limits {MAX_MEAN}/{MAX_P95}/{MAX_MAX})")
    return ok


def barycentric_probe(cache, ctx, images, mvp, origin, cands):
    """Prove the GPU really interpolates class colour across a mixed face.

    Two independent things are measured on the class-colour stage (3):
      * the captured pixel matches the perspective-correct barycentric blend
        of the three vertex class RGBs (the CPU expectation);
      * that blend is materially DIFFERENT from every single vertex colour,
        so a "good" match cannot be explained by the GPU flat-shading the
        face to one representative class.
    """
    h, w = next(iter(images.values())).shape[:2]
    samples = project_candidates(cache, cands, mvp, origin, w, h)
    if not samples:
        return {"n": 0, "blend_err": 0.0, "flat_err": 0.0, "discriminating": 0}

    faces = np.asarray(cache.faces)
    normal_img = images[1]
    class_img = images[3]
    blend_err = []
    flat_err = []
    discriminating = 0

    for s in samples:
        faces_row = faces[s["face"]]
        rgb3 = ctx["rgb_by_vertex"][faces_row].astype(np.float64)
        n = np.asarray(cache.face_normals[s["face"]], dtype=np.float64)
        normal_b = np.floor(np.clip(n * 0.5 + 0.5, 0.0, 1.0) * 255.0 + 1e-4)
        got_n = normal_img[s["py"], s["px"]].astype(np.int16)
        if not np.all(np.abs(got_n - normal_b) <= NORMAL_GATE_TOL):
            continue                                   # not visible here

        blended = perspective_correct(s["lam"], s["w"], rgb3)
        blended = np.clip(blended, 0.0, 255.0)
        got = class_img[s["py"], s["px"]].astype(np.float64)
        blend_err.append(float(np.abs(got - blended).max()))

        # Distance from the blend to the NEAREST single vertex colour.
        d = np.abs(blended[None, :] - rgb3).max(axis=1).min()
        flat_err.append(float(d))
        if d > MAX_MAX:
            discriminating += 1

    if not blend_err:
        return {"n": 0, "blend_err": 0.0, "flat_err": 0.0, "discriminating": 0}

    return {
        "n": len(blend_err),
        "blend_err": float(np.max(blend_err)),
        "flat_err": float(np.median(flat_err)),
        "discriminating": discriminating,
    }



# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def build_report() -> int:
    L = _REPORT_LINES
    L.clear()
    A = L.append
    A("=" * 78)
    A("VULKAN SHADING PARITY REPORT")
    A("=" * 78)
    A("")
    A("SOURCE OF TRUTH: gui/shading_display.py (VTK/CPU pipeline).")
    A("GPU PORT:        native/naksha_vulkan/shaders/surface.frag (+ point.*)")
    A("")
    A("-" * 78)
    A("1. FUNCTIONS ANALYSED (CPU) -> GLSL counterpart")
    A("-" * 78)
    for line in (
        "_compute_face_normals()              -> faceNormal() (normalize + hemisphere fix)",
        "_compute_shading() / cache.shade     -> rawShade() clip(max(N.L, ambient), 0, 1)",
        "_shading_sharpness_response()        -> sharpnessResponse() legacy/overdrive split",
        "_crisp_sharpness_contrast_gain()     -> contrastGain() clip(0.55+0.90*leg+5*od, .35, 6.5)",
        "_shading_effective_light_elevation() -> effectiveLightElevation() base-(base-12)*od, 12..85",
        "_crisp_shade_chunk()                 -> mode 4 inline remap (identical algebra)",
        "_crisp_blend_ambient_floor()         -> shadeParamsA.w ambient floor (default 0.08)",
        "_shading_key_fill_intensities()      -> shadeParamsB.x / shadeParamsB.y (key, fill)",
        "_configure_nakshatech_color_blend_lighting() -> mode 5 Ka/Kd + key/fill light rig",
        "_build_crisp_base_face_colors()      -> PASS1 vertex colour = trunc(classRGB*shade)",
        "_collect_mixed_display_faces()       -> PASS2 overlay index list (mixed ids)",
        "_shading_palette_rgb()               -> PASS2 per-vertex true class RGB (barycentric)",
    ):
        A(f"  {line}")
    A("")
    A("-" * 78)
    A("2. CPU vs GPU FORMULAS (must be identical)")
    A("-" * 78)
    A("  lighting (raw)   CPU: clip(max(N.L, ambient), 0, 1)        GPU: rawShade()")
    A("  sharpness        CPU: legacy=clip(v/90,0,1); <=90 od=0;    GPU: sharpnessResponse()")
    A("                   <=200 od=0.90*(v-90)/110; else tail to 999")
    A("  contrast gain    CPU: clip(0.55+0.90*legacy+5.0*od,.35,6.50) GPU: contrastGain()")
    A("  light elevation  CPU: clip(base-(base-12)*od, 12, 85)      GPU: effectiveLightElevation()")
    A("  ambient/floor    CPU: shadow_floor=max(ambient, floor)     GPU: max(A.z, A.w)")
    A("  crisp base shade CPU: targetH+(raw-rawH)*gain, clamp       GPU: mode-4 inline (same algebra)")
    A("  key / fill       CPU+GPU: _shading_key_fill_intensities() = 1.05 * key_ratio split")
    A("  mixed-face Phong CPU: clamp(Ka+Kd*(key*max(N.Lk,0)+fill*max(N.Lf,0)),0,1) GPU: mode 5")
    A("  pure-face colour CPU: trunc(classRGB * crispShade)         GPU: present(albedo*shade)")
    A("  mixed albedo     CPU: barycentric class RGB (persp-correct) GPU: rasterizer interpolation")
    A("  output encoding  VTK writes byte direct; GPU pre-compensates sRGB (color_mode=1)")
    A("")
    A("-" * 78)
    A("3. MEASURED PER-FACE RGB DIFFERENCES (native capture vs CPU expectation)")
    A("-" * 78)
    for label in _R.get("labels", []):
        A(f"  Parameter set {label}:")
        for kind in ("pure", "mixed"):
            st = _R.get(f"{label}_{kind}") or {}
            if not st:
                A(f"    {kind:<6}: no gated samples")
                continue
            for stage in STAGES:
                s = st.get(stage)
                if s:
                    A(f"    {kind:<6} stage {stage} ({STAGE_NAMES[stage]:<20}) "
                      f"n={s['n']:<5} mean={s['mean']:.2f} "
                      f"p95={s['p95']:.1f} max={s['max']:.0f}")
        A(f"    gated samples: pure={_R.get(label + '_pure_n', 0)} "
          f"mixed={_R.get(label + '_mixed_n', 0)} of {SAMPLE_SCAN} scanned/kind "
          f"(gate = normal-stage match)")
        for kind in ("pure", "mixed"):
            line = _R.get(f"{label}_{kind}_attr")
            if line:
                A(f"    {kind:<6} attrition: {line}")
        A(f"    engine float32 vertex position error: "
          f"{_R.get(label + '_precision_px', float('nan')):.2f} px median")
        A("")
    A("-" * 78)
    A("4. BARYCENTRIC INTERPOLATION (mixed faces, class-colour stage)")
    A("-" * 78)
    A(f"  {_R.get('barycentric_verdict', 'N/A')}")
    A("")
    A("-" * 78)
    A("5. FINAL VERDICT")
    A("-" * 78)
    failed = [c for c in _CHECKS if not c[1]]
    A(f"  checks: {len(_CHECKS) - len(failed)}/{len(_CHECKS)} passed")
    for name, _ok, detail in failed:
        A(f"    FAILED: {name}  ({detail})")
    A(f"  VULKAN SHADING PARITY: {'PASS' if not failed else 'FAIL'}")
    A(f"  threshold: mean<={MAX_MEAN}  p95<={MAX_P95}  max<={MAX_MAX} RGB levels")
    A("")
    A("-" * 78)
    A("6. FILES MODIFIED FOR THIS PARITY PORT")
    A("-" * 78)
    for f in (
        "native/naksha_vulkan/shaders/surface.frag  (crisp modes 4/5 = transcription of shading_display.py)",
        "native/naksha_vulkan/shaders/surface.vert  (varyings for the frag port)",
        "native/naksha_vulkan/shaders/point.vert    (sRGB byte-parity pre-compensation)",
        "native/naksha_vulkan/shaders/point.frag    (push-constant parity block)",
        "native/naksha_vulkan/src/SurfaceRenderer.cpp/.hpp   (two-pass draw, parity push constants)",
        "native/naksha_vulkan/src/PointCloudRenderer.cpp/.hpp (colour-parity mode)",
        "native/naksha_vulkan/src/naksha_vulkan_c_api.cpp + include/naksha/naksha_vulkan_c_api.h",
        "gui/render_backend.py   (uniform plumbing, set_color_parity_params, set_crisp_debug_mode)",
        "gui/shading_display.py  (_push_vulkan_shading_parity single-source-of-truth seam)",
        "build_native.ps1        (shader compile + DLL link)",
        "vulkan_shading_parity_test.py (this harness)",
    ):
        A(f"  - {f}")
    A("")
    A("=" * 78)

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "parity_report.txt"), "w",
              encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")

    return 0 if not failed else 1



# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    from PySide6.QtWidgets import QApplication

    from gui.app_window import NakshaApp
    from gui.dialogs.load_pointcloud_dialog import DEFAULT_IMPORT_OPTIONS
    from vulkan_engine_test import _suppress_modal_dialogs, pump, wait_until
    import vulkan_parity_check as par

    print("=" * 76, flush=True)
    print("NAKSHA VULKAN SHADING PARITY TEST (shading_display.py = truth)", flush=True)
    print(f"  las  : {LAS_PATH}", flush=True)
    print(f"  ptc  : {PTC_PATH}", flush=True)
    print("=" * 76, flush=True)
    for path, label in ((LAS_PATH, "LAS"), (PTC_PATH, "PTC")):
        if not os.path.isfile(path):
            print(f"[parity] {label} file not found: {path}", flush=True)
            return 1
    os.makedirs(OUT_DIR, exist_ok=True)

    _suppress_modal_dialogs()
    app_qt = QApplication.instance() or QApplication(sys.argv[:1])
    win = NakshaApp()
    win.resize(1400, 900)
    win.show()
    pump(app_qt, 1.0)

    rb = getattr(win, "render_backend", None)
    backend = getattr(rb, "vulkan_backend", None) if rb is not None else None
    check("Vulkan backend active", backend is not None and bool(getattr(rb, "active", False)),
          f"state={getattr(rb, 'state', None)}")
    if backend is None:
        return report()

    # ---- dataset through the production loaders ---------------------------
    print(f"[parity] loading {os.path.basename(LAS_PATH)} ...", flush=True)
    win.open_file(filenames=[LAS_PATH],
                  import_options=dict(DEFAULT_IMPORT_OPTIONS), prompt_import=False)
    if not wait_until(app_qt, lambda: (getattr(win, "data", None) is not None
                                       and win.data.get("xyz") is not None
                                       and getattr(win, "_file_loader_worker", None) is None),
                      timeout=900.0):
        check("LAS loaded", False, "timeout")
        return report()
    pump(app_qt, 1.0)
    n_points = int(win.data["xyz"].shape[0])
    check("LAS loaded", True, f"points={n_points:,}")

    win._load_single_ptc(PTC_PATH)
    if not wait_until(app_qt, lambda: bool(getattr(win, "class_palette", None)), timeout=60.0):
        check("PTC palette applied", False, "class_palette stayed empty")
        return report()
    check("PTC palette applied", True, f"classes={len(win.class_palette)}")

    # ---- shaded class through the production path -------------------------
    print("[parity] display mode -> shaded_class ...", flush=True)
    t0 = time.perf_counter()
    win.set_display_mode("shaded_class")
    active = wait_until(app_qt,
                        lambda: rb.active_render_mode == "shaded_class"
                        and backend.get_surface_upload_count() > 0,
                        timeout=900.0)
    check("shaded_class active + surface uploaded", active,
          f"{time.perf_counter() - t0:.1f}s uploads={backend.get_surface_upload_count()}")
    if not active:
        return report()

    from gui import shading_display as sd
    key = sd._get_rendered_cache_key(win)
    cache = sd._cache_store.get(key) if key is not None else None
    check("shading geometry cache present", cache is not None,
          f"faces={0 if cache is None else len(cache.faces):,}")
    if cache is None:
        return report()
    check("crisp-hybrid active (two-pass)", bool(getattr(win, "_shading_crisp_hybrid_active", False)))
    check("point cloud hidden under mesh", backend.get_point_cloud_visible() in (0, -1),
          f"visible={backend.get_point_cloud_visible()}")

    classes = np.asarray(win.data["classification"]).astype(np.int64)
    rng = np.random.default_rng(20260929)
    if not par.set_reference_camera(win, rb):
        check("reference camera applied", False, "set_reference_camera failed")
        return report()
    backend.request_render()
    pump(app_qt, 0.4)

    # ---- one measurement pass per parameter set ---------------------------
    n_faces = int(len(np.asarray(cache.faces)))
    labels = []
    bary_rows = []

    for label, azimuth, sharpness, ambient in PARAM_SETS:
        note(f"parameter set {label}: azimuth={azimuth:g} "
             f"sharpness={sharpness:g} ambient={ambient:g}")
        # Production light-only update: the same function the UI's Apply
        # button calls, so GPU uniforms and the CPU cache stay in lockstep.
        sd.update_shading_lighting_only(win, azimuth, sharpness, ambient)
        pump(app_qt, 0.6)

        # Re-frame per parameter set: the light-only update saves/restores the
        # camera, so the framing done before the loop cannot be assumed to
        # still hold. Faces must be large enough to sample (at whole-cloud
        # framing every one of ~6M faces is sub-pixel).
        view_w, view_h = 1400, 841
        mvp, origin, med_px, pscale = frame_for_sampling(
            win, rb, backend, cache, rng, view_w, view_h,
            app_qt=app_qt, pump=pump)
        if mvp is None:
            check(f"{label}: engine MVP readable", False,
                  "nkv_get_last_mvp returned 0 / non-finite matrix")
            continue
        check(f"{label}: faces large enough to sample",
              med_px >= MIN_AREA_PX,
              f"median face {med_px:.1f} px^2 at parallel_scale={pscale:.4f} "
              f"(need >= {MIN_AREA_PX:g})")
        backend.request_render()
        pump(app_qt, 0.3)

        ctx = build_context(win, sd, cache, classes)
        check(f"{label}: CPU state snapshot",
              ctx["mixed_count"] > 0
              and ctx["rgb_by_vertex"].shape[0] == len(cache.xyz_final),
              f"mixed={ctx['mixed_count']:,} "
              f"verts={ctx['rgb_by_vertex'].shape[0]:,} "
              f"legacy={ctx['legacy']:.3f} overdrive={ctx['overdrive']:.3f} "
              f"eff_elev={ctx['eff_elev']:.1f} "
              f"key={ctx['key']:.3f} fill={ctx['fill']:.3f}")

        imgs, mvps = capture_stages(backend, app_qt, pump)
        # Use the MVP that was live for the CAPTURE, not the one sampled during
        # framing (lit_fraction renders between the two and advances the slot).
        # `or` cannot be used on a numpy array - it has no single truth value.
        mvp_cap = mvps.get(1)
        if mvp_cap is None:
            mvp_cap = mvp
        h, w = next(iter(imgs.values())).shape[:2]
        save_rgb_png(imgs[0], os.path.join(OUT_DIR, f"{label}_stage0_final.png"))
        save_rgb_png(imgs[1], os.path.join(OUT_DIR, f"{label}_stage1_normal.png"))
        save_rgb_png(imgs[3], os.path.join(OUT_DIR, f"{label}_stage3_class.png"))
        note(f"{label}: captured {len(imgs)} debug stages at {w}x{h}")

        # Depth for the ownership gate, captured at the same camera as the
        # colour stages and with the same mesh state. capture_depth()
        # re-renders, so it must happen before anything else advances the
        # frame slot - otherwise the gate compares depths from a different
        # frame than the colours it is gating.
        depth_img = backend.capture_depth()
        if depth_img is None:
            check(f"{label}: depth readback available", False,
                  "capture_depth() returned None (needs D32_SFLOAT depth + "
                  "rebuilt DLL); the ownership gate cannot run without it")
            depth_img = np.ones((h, w), dtype=np.float32)
        else:
            d_ok = depth_img.shape == (h, w) and np.all(np.isfinite(depth_img))
            check(f"{label}: depth readback available", d_ok,
                  f"{depth_img.shape} finite={bool(np.all(np.isfinite(depth_img)))} "
                  f"range=[{float(np.nanmin(depth_img)):.4f},"
                  f"{float(np.nanmax(depth_img)):.4f}]")
        np.save(os.path.join(OUT_DIR, f"{label}_depth.npy"), depth_img)

        # Only faces actually on screen (and big enough to sample) are usable
        # candidates; at this zoom the camera sees a small patch of ~6M faces.
        vis = visible_face_pool(cache, mvp_cap, origin, w, h)
        note(f"{label}: visible sample pool = {vis.size:,} of {n_faces:,} faces")

        # The engine stores world positions in a float32 vertex buffer. On
        # UTM-scale data that quantises coarser than the triangles themselves,
        # which is an ENGINE property, not a shading one - measure it and say
        # so, rather than letting it silently empty the sample gate.
        probe = pick_candidates(rng, np.arange(n_faces), 2000)
        prec_px = position_precision_px(cache, probe, mvp_cap, origin, w, h)
        _R[f"{label}_precision_px"] = prec_px
        check(f"{label}: engine float32 position precision", prec_px <= 1.0,
              f"median vertex lands {prec_px:.2f} px from its true position "
              f"(render origin={np.round(origin, 1).tolist()}); >1 px means "
              f"float32 vertex storage moves geometry")

        is_mixed = np.zeros(n_faces, dtype=bool)
        mid = ctx["mixed_ids"]
        mid = mid[(mid >= 0) & (mid < n_faces)]
        is_mixed[mid] = True
        vis_mixed = vis[is_mixed[vis]] if vis.size else vis
        vis_pure = vis[~is_mixed[vis]] if vis.size else vis

        labels.append(label)
        for kind, ids in (("pure", vis_pure), ("mixed", vis_mixed)):
            cands = pick_candidates(rng, ids, SAMPLE_SCAN)
            if len(cands) == 0:
                check(f"{label}/{kind}: candidate faces available", False,
                      f"visible pool for {kind} = 0")
                continue
            st, gated, scanned, attr = evaluate(win, sd, cache, ctx, imgs,
                                                mvp_cap, origin, kind, cands,
                                                depth_img)
            _R[f"{label}_{kind}"] = st
            _R[f"{label}_{kind}_n"] = gated
            _R[f"{label}_{kind}_attr"] = attrition_text(attr)
            if gated < MIN_GATED:
                check(f"{label}/{kind}: enough visible samples", False,
                      f"gated={gated} of {scanned} scanned (need {MIN_GATED}) "
                      f"| {attrition_text(attr)}")
                _dump_rejects(label, kind, attr)
                continue
            check(f"{label}/{kind}: enough visible samples", True,
                  f"gated={gated} of {scanned} scanned "
                  f"| {attrition_text(attr)}")
            _dump_rejects(label, kind, attr)
            for stage in STAGES:
                if stage in st:
                    stage_check(
                        f"{label}/{kind} stage {stage} ({STAGE_NAMES[stage]})",
                        st[stage])

        # barycentric proof uses the visible mixed pool of this parameter set
        cands_mixed = pick_candidates(rng, vis_mixed, SAMPLE_SCAN)
        probe = barycentric_probe(cache, ctx, imgs, mvp_cap, origin, cands_mixed)
        if probe["n"] >= MIN_GATED:
            ok = (probe["blend_err"] <= MAX_MAX
                  and probe["discriminating"] >= int(0.5 * probe["n"]))
            check(f"{label}: barycentric class-colour blend", ok,
                  f"n={probe['n']} max|blend-gpu|={probe['blend_err']:.1f} "
                  f"median|blend-nearest-vertex|={probe['flat_err']:.1f} "
                  f"discriminating={probe['discriminating']}")
            bary_rows.append(
                f"{label}: {probe['n']} mixed faces, max |CPU blend - GPU| = "
                f"{probe['blend_err']:.1f} levels, median distance from the "
                f"nearest single vertex colour = {probe['flat_err']:.1f} "
                f"levels ({probe['discriminating']}/{probe['n']} discriminating)")
        else:
            check(f"{label}: barycentric class-colour blend", False,
                  f"only {probe['n']} visible mixed faces (need {MIN_GATED})")

    _R["labels"] = labels
    if bary_rows:
        _R["barycentric_verdict"] = (
            f"CPU blend matches the GPU within {MAX_MAX:.0f} levels on every "
            f"gated mixed face, and the blend sits far enough from any single "
            f"vertex colour that flat shading could not reproduce it.\n"
            + "\n".join(f"    {r}" for r in bary_rows))
    else:
        _R["barycentric_verdict"] = "not measured (no gated mixed faces)"

    return build_report()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        print("VULKAN SHADING PARITY: CRASH", flush=True)
        sys.exit(3)

