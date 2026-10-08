"""Stage 3C cache-first streaming wiring, attached to the live AppWindow.

Keeps the app_window.py edit surface to two small hooks:
  (1) cache-first detection in open_file() -> install_streaming(app, path, v)
  (2) a hard gate on the monolithic upload_point_cloud() path

All streaming logic lives here; app_window.py only imports and calls.
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np

from .dataset_mode import DatasetMode, is_streaming_dataset
from .dataset_mode import cache_first_open, CacheValidity
from .stream_manager import (
    NakshaStreamManager, DEFAULT_RAM_BUDGET_BYTES, DEFAULT_GPU_BUDGET_BYTES,
    PAN_ZOOM_FIX_VERSION,
)
from .stream_telemetry import StreamTelemetry
from .stream_renderer_adapter import (
    VulkanTileRendererAdapter, HeadlessTileRenderer,
)

# Import-time provenance banner. Two copies of this code can otherwise produce
# identical-looking consoles, and "which file is main.py actually running?" is
# then unanswerable from the log. Both absolute paths plus an explicit version
# constant make an edit-vs-import mismatch visible immediately.
import os as _os

from . import stream_manager as _stream_manager_mod

print("[STREAM CODE VERSION]")
print(f"fix_version: {PAN_ZOOM_FIX_VERSION}")
print(f"stream_manager_file: {_os.path.abspath(_stream_manager_mod.__file__)}")
print(f"app_streaming_file: {_os.path.abspath(__file__)}")
print("[/STREAM CODE VERSION]\n")

# APP.DATA AUDIT marks (spec)
STREAMING_SAFE = [
    "pan / zoom / orbit (camera-only)",
    "classification display (SoA)",
    "intensity display (SoA)",
    "elevation display (SoA)",
    "RGB display (SoA)",
    "bounds / total point count / CRS",
    "LODs 0-4 via cache",
    # Part 5: stored-normal Shaded Class is implemented and streaming-safe.
    # It renders XYZ + CLASS + NORMAL from the resident GPU buffers via the
    # Vulkan stored-normal splat pass. It needs NO in-memory app.data and calls
    # NO Delaunay, so it belongs here rather than under TEMPORARILY_GATED.
    "stored-normal Shaded Class (XYZ + CLASS + NORMAL streamed)",
]
VISIBLE_SET_ONLY = [
    "identification / picking (visible subset)",
    "cross-section (visible subset)",
    "accudraw digitizing",
    "sparse classification edit (per-point)",
    "export of visible set",
]
TEMPORARILY_GATED = [
    # The stored-normal Shaded path is NOT gated. What remains gated is only the
    # legacy Delaunay/TIN mesh, which genuinely requires the full in-memory
    # app.data. Naming that legacy path precisely avoids implying that Shaded
    # Class itself is unavailable while streaming.
    "legacy Delaunay/TIN shaded mesh (needs full in-memory app.data)",
    "global statistics requiring full decode",
    "full-LAZ save/export of the entire dataset",
]


def install_streaming(app, source_path, validity: CacheValidity):
    """Attach the streaming path to ``app`` and return the StreamManager.

    Called from AppWindow.open_file() when a committed cache is valid.
    Sets DatasetMode.STREAMING, opens the cache reader, builds the telemetry
    file + resident-tile renderer adapter, performs the overview first paint,
    and installs a 60 Hz frame tick + a VTK camera-observer.

    No full source LAZ decode occurs; self.data stays EMPTY (no materialization).
    """
    from PySide6.QtCore import QTimer

    app.dataset_mode = DatasetMode.STREAMING
    app.data = {}  # STREAMING: never allocate the full point dataset
    app.data_bounds = (tuple(float(v) for v in validity.bounds_min),
                       tuple(float(v) for v in validity.bounds_max))
    app.total_points = validity.total_points  # metadata only
    app._streaming_full_point_upload_attempts = 0
    app.loaded_file = source_path
    app.loaded_filename = source_path

    from gui.naksha_cache.reader import NakshaPointCacheReader
    reader = NakshaPointCacheReader(source_path, verify_crc=False,
                                    load_edits=False)

    rb = getattr(app, "render_backend", None)
    if rb is not None and getattr(rb, "active", False) and \
       getattr(rb, "vulkan_backend", None) is not None:
        adapter = VulkanTileRendererAdapter(rb.vulkan_backend,
                                            total_points=validity.total_points)
        backend_label = "VulkanTileRendererAdapter"
    else:
        adapter = HeadlessTileRenderer(validity.total_points)
        backend_label = "HeadlessTileRenderer"

    te_path = os.path.join(os.path.dirname(source_path),
                           "stage3c_viewport_telemetry.jsonl")
    te = StreamTelemetry(te_path)
    app._stream_telemetry_path = te_path

    # PART 11/12/13: pass NO budget so NakshaStreamManager resolves them from
    # the MEASURED hardware profile. Passing the constants here would silently
    # reintroduce fixed-size machine assumptions on every real machine.
    mgr = NakshaStreamManager(app, reader, adapter, te)
    app.naksha_stream = mgr

    # ---- PART 1: open the real stored-normal sidecar ----------------------
    # This REPLACED a block that emitted normal_cache_status state='HIT' and
    # normal_gpu_ready uploaded_once=1 unconditionally. That was fabricated
    # telemetry: it reported a normal cache that had never been opened, so a
    # total absence of normals still looked like a success. The status below is
    # whatever the sidecar on disk actually says.
    from gui.naksha_cache.normal_streaming import open_normal_cache
    nreader, nreport = open_normal_cache(
        source_path, verify_source=False,
        expected_source_points=int(validity.total_points),
        # Phase 6C.4: a sidecar built against a DIFFERENT point-cache layout
        # (block order, depth, LOD ladder, attribute mask, bounds) is refused
        # here, so a rebuilt cache can never silently attach stale normals.
        expected_layout_fingerprint=int(
            getattr(getattr(mgr, "idx", None), "layout_fingerprint", 0) or 0))
    mgr.attach_normal_cache(nreader, nreport)
    # PHASE 6D: wire the background normal builder into the production bootstrap.
    # Without this, start_normal_generation() finds service=None and returns False -
    # a MISS dataset never produces normals off-thread. See PHASE6D report.
    from gui.naksha_cache.normal_service import NormalBuildService
    service = NormalBuildService()
    mgr.attach_normal_service(service)
    print()
    for line in nreport.as_lines():
        print(line)
    if nreport.reason:
        print(f"reason: {nreport.reason}")
    print()

    # ---- DEV instrumentation (guarded; no return value is consumed) -------
    # Records the dataset/cache/GPU-residency evidence the acceptance analyzer
    # reads AFTER the session. It records state; it never decides anything.
    try:
        from gui import instant_shaded_telemetry as ist
        if ist.get() is not None:
            common = {
                'dataset_path': str(source_path),
                'point_count': int(validity.total_points),
                'dataset_mode': 'STREAMING',
                'renderer': str(backend_label),
            }
            ist.emit('dataset_open_requested', **common)
            # Measured, never asserted: the status and counts below come from
            # the opened sidecar, not from what we hoped was there.
            ist.emit('normal_cache_status', state=str(nreport.status),
                     blocks=int(nreport.blocks),
                     stored_normals=int(nreport.stored_normals),
                     encoding=str(nreport.encoding),
                     open_ms=round(float(nreport.open_ms), 3),
                     bytes=int(nreport.bytes),
                     reason=str(nreport.reason), **common)
            ist.emit('dataset_ready', **common)
    except Exception:
        pass
    _t_fp0 = time.perf_counter()
    info = mgr.open_first_frame()
    first_frame_ms = (time.perf_counter() - _t_fp0) * 1000.0
    print(f"   stream telemetry -> {te_path}")
    print(f"   DatasetMode.STREAMING; renderer={backend_label}; "
          f"has_native_append={adapter.has_native_append()}")
    print(f"   first paint: overview block #{validity.overview_block_id} "
          f"({info['overview_points']:,} pts)")
    _audit()
    _install_frame_tick(app, mgr)
    _install_camera_hook(app, mgr)
    # PART 5: fit the MAIN view from cache metadata. Without this the camera
    # stays at its default origin while the dataset sits near (390000, 685000),
    # which is the black viewport.
    fit_streaming_bounds(app, mgr)
    try:
        app.update_total_points_label()
    except Exception:
        pass
    _print_stream_open_report(app, source_path, validity, adapter, mgr,
                              backend_label, first_frame_ms, info)
    return mgr


def streaming_display_mode(ui_mode: str) -> str:
    """PART 1 - the ONE translation table at the GUI/streaming boundary.

    The UI names and the stream manager's canonical names are DIFFERENT
    vocabularies. The manager accepts exactly:
        classification, elevation, intensity, rgb, shaded
    while the UI buttons send:
        class, shaded_class, rgb, intensity, elevation

    'class' and 'shaded_class' are both REJECTED by
    NakshaStreamManager.set_display_mode, which returns False for any name
    outside DISPLAY_ATTRS. That is why the live log showed
    "[STREAMING DISPLAY] mode=shaded_class ok=False" - the click never reached
    the manager as a valid mode.

    Every GUI -> streaming conversion goes through this function so the
    mapping cannot drift between call sites.
    """
    return _UI_TO_STREAM_MODE.get(str(ui_mode or "").strip().lower(),
                                  str(ui_mode or "").strip().lower())


def streaming_ui_mode(stream_mode: str) -> str:
    """PART 28: the INVERSE translation, manager name -> UI name.

    `app.display_mode` is the name the UI shows. When the manager is the
    authority (STREAMING) and it moved to a mode on its own - it does so when
    the dataset has no RGB and falls back to neutral - the UI name has to
    follow, otherwise the log shows the impossible
    `ui_mode=rgb / stream_mode=neutral` pair.
    """
    return _STREAM_TO_UI_MODE.get(str(stream_mode or "").strip().lower(),
                                  str(stream_mode or "").strip().lower())


# PART 28: these modes need an RGB stream the file does not have.
MODES_REQUIRING_RGB = frozenset({"rgb"})


def dataset_has_rgb(mgr=None) -> bool:
    """True when the resident dataset actually carries RGB."""
    if mgr is None:
        return False
    for attr in ("has_rgb", "dataset_has_rgb"):
        fn = getattr(mgr, attr, None)
        if fn is None:
            continue
        try:
            return bool(fn() if callable(fn) else fn)
        except Exception:
            continue
    caps = getattr(mgr, "capabilities", None)
    if caps is not None:
        for attr in ("has_rgb", "rgb"):
            v = getattr(caps, attr, None)
            if isinstance(v, bool):
                return v
    return False


# Canonical streaming manager mode names, in the UI vocabulary.
_UI_TO_STREAM_MODE = {
    # PART 1: gray-first. The UI "default"/Neutral button is the on-open mode.
    "neutral": "neutral",
    "default": "neutral",
    "depth": "depth",
    "class": "classification",
    "classification": "classification",
    "shaded_class": "shaded",
    "shaded": "shaded",
    "rgb": "rgb",
    "intensity": "intensity",
    "elevation": "elevation",
    "surface": "surface",
}

# PART 28: the inverse table, so app.display_mode can be re-derived from the
# manager instead of going stale at "rgb" while the stream says "neutral".
_STREAM_TO_UI_MODE = {
    "neutral": "neutral",
    "depth": "depth",
    "classification": "class",
    "shaded": "shaded_class",
    "rgb": "rgb",
    "intensity": "intensity",
    "elevation": "elevation",
    "surface": "surface",
}


def fit_streaming_bounds(app, mgr=None):
    """PART 5 - position the MAIN view camera from CACHE metadata.

    In STREAMING there is no app.data and no VTK LiDAR actor, so the legacy
    fit (actor.GetBounds / app.data bounds) cannot run and leaves the camera at
    its default. The dataset sits near (390000, 685000), so a camera left at
    the origin renders a correct point buffer into empty space: 299,676 points
    resident, nothing on screen.

    Sets ParallelScale so the whole XY extent fits the viewport, centres the
    focal point on the dataset, keeps the top-down plan orientation, and pushes
    the render origin so the GPU's float32 local coordinates stay precise.
    """
    import numpy as np
    from vtkmodules.vtkRenderingCore import vtkRenderer
    mgr = mgr or getattr(app, "naksha_stream", None)
    if mgr is None:
        return False
    bounds = mgr.dataset_bounds()
    if bounds is None:
        return False
    bmin, bmax = np.asarray(bounds[0], float), np.asarray(bounds[1], float)
    cx, cy = float((bmin[0] + bmax[0]) * .5), float((bmin[1] + bmax[1]) * .5)
    span_x = max(1.0, float(bmax[0] - bmin[0]))
    span_y = max(1.0, float(bmax[1] - bmin[1]))
    try:
        ren = app.vtk_widget.GetRenderWindow().GetRenderers().GetFirstRenderer()
        cam = ren.GetActiveCamera()
        sz = app.vtk_widget.size()
        w, h = max(1, int(sz.width())), max(1, int(sz.height()))
        aspect = float(w) / float(h)
        # ParallelScale is the HALF VERTICAL extent, so it is driven by the
        # vertical span, then widened if the horizontal span demands more.
        half_h = span_y * 0.5 * 1.05
        half_h = max(half_h, (span_x * 0.5 * 1.05) / aspect)
        cz = float((bmin[2] + bmax[2]) * .5)
        _owner = getattr(app, "render_backend", None)
        _framed = False
        try:
            # MainCamera2D owns the initial framing (stage E/F): same centre,
            # scale and eye as the VTK writes below, applied to the canonical
            # camera first and mirrored into VTK.
            if _owner is not None and _owner.owns_main_camera():
                _framed = bool(_owner.apply_main_fit(
                    cx, cy, cz, float(half_h), ren, eye=(cx, cy - span_y, cz + span_y)))
        except Exception as exc:
            print(f"[FIT STREAMING] MainCamera2D framing failed ({exc!r}); VTK path")
        if not _framed:
            cam.SetFocalPoint(cx, cy, cz)
            cam.SetPosition(cx, cy - span_y, cz + span_y)
            cam.SetViewUp(0.0, 1.0, 0.0)
            cam.ParallelProjectionOn()
            cam.SetParallelScale(float(half_h))
            ren.ResetCameraClippingRange()
        # Render origin = dataset centre, so local float32 coords stay small.
        try:
            app.naksha_stream.adapter.set_render_origin(cx, cy,
                                                        float((bmin[2] + bmax[2]) * .5))
        except Exception as exc:
            print(f"[FIT STREAMING] render origin not set: {exc!r}")
        app.vtk_widget.GetRenderWindow().Render()
        print(f"[FIT STREAMING] centre=({cx:.1f}, {cy:.1f}) "
              f"span={span_x:.0f}x{span_y:.0f} m "
              f"viewport={w}x{h} parallel_scale={half_h:.1f}")
        return True
    except Exception as exc:
        print(f"[FIT STREAMING] failed: {exc!r}")
        return False


def _print_stream_open_report(app, source_path, validity, adapter, mgr,
                              backend_label, first_frame_ms, info):
    """The mandatory [NAKSHA STREAM OPEN] block (spec Part C).

    Every counter is read from the LIVE objects, not asserted:
      legacy_loader_called            - the legacy FileLoaderWorker never ran
      full_*_allocated                - app.data holds no point arrays
      monolithic_set_point_cloud_calls- the streaming hard-gate counter
    """
    d = getattr(app, "data", None) or {}
    full_xyz = sum(1 for k in ("xyz",) if k in d)
    full_cls = sum(1 for k in ("classification",) if k in d)
    full_rgb = sum(1 for k in ("rgb",) if k in d)
    full_int = sum(1 for k in ("intensity",) if k in d)
    app_points = 0
    for k in ("xyz",):
        v = d.get(k)
        if v is not None:
            try:
                app_points = max(app_points, int(len(v)))
            except Exception:
                pass
    print()
    print("[NAKSHA STREAM OPEN]")
    print(f"mode: STREAMING")
    print(f"source_points: {validity.total_points:,}")
    print(f"cache: VALID")
    print(f"legacy_loader_called: {int(not getattr(mgr, 'legacy_loader_called', False))}")
    print(f"full_xyz_allocated: {int(full_xyz == 0)}")
    print(f"full_classification_allocated: {int(full_cls == 0)}")
    print(f"full_rgb_allocated: {int(full_rgb == 0)}")
    print(f"full_intensity_allocated: {int(full_int == 0)}")
    print(f"monolithic_set_point_cloud_calls: "
          f"{int(getattr(app, '_streaming_full_point_upload_attempts', 0))}")
    print(f"app_data_point_count: {app_points:,}  (must be 0 in STREAMING)")
    print(f"persistent_nkidx: {os.path.basename(validity.index_path)} "
          f"({os.path.getsize(validity.index_path):,} bytes)")
    print(f"nkpc: {os.path.basename(validity.pc_path)} "
          f"({validity.pc_bytes / 1024 ** 3:.2f} GiB)")
    print(f"renderer: {backend_label}")
    print(f"overview_points: {info['overview_points']:,}")
    print(f"first_frame_cpu_ms: {first_frame_ms:.1f}")
    print("[/NAKSHA STREAM OPEN]")
    print()


def _audit():
    print("   STREAMING feature audit:")
    for label, feats in (("STREAMING_SAFE", STREAMING_SAFE),
                         ("VISIBLE_SET_ONLY", VISIBLE_SET_ONLY),
                         ("TEMPORARILY_GATED", TEMPORARILY_GATED)):
                        print("      %s: %s" % (label, ", ".join(feats)))


def _install_frame_tick(app, mgr):
    """60 Hz tick: pumps the stream manager (drains async reads, re-orders
    draw ranges) and emits telemetry. NEVER blocks the UI thread on disk."""
    from PySide6.QtCore import QTimer
    t = QTimer(app)
    t.timeout.connect(_make_tick(app, mgr))
    t.start(16)  # ~60 Hz
    app._stream_timer = t


def _make_tick(app, mgr):
    state = {"n": 0, "last_submitted": None, "printed_idle": False}

    def tick():
        if not is_streaming_dataset(app):
            return
        cam, vp = _viewport(app)
        if vp is None:
            return
        try:
            mgr.on_frame(cam, vp)
        except Exception as e:  # worker exceptions must never crash the UI
            # COUNTED, never swallowed. A tick that raises every frame leaves
            # the stream looking alive while no refinement ever happens, which
            # is exactly how a missing constant in the refinement path went
            # unnoticed until a live GUI run.
            mgr.frame_tick_error_count = int(
                getattr(mgr, "frame_tick_error_count", 0) or 0) + 1
            n = mgr.frame_tick_error_count
            if n <= 3 or n % 60 == 0:
                print(f"WARNING: [stream] frame tick error #{n}: {e!r}")
            return
        # PART 5: DEV telemetry is isolated BELOW the render call and is
        # strictly non-fatal. on_frame() has already completed at this point,
        # so a missing diagnostic field can no longer abort refinement - which
        # is exactly what KeyError('resident_normal_points') did. Only this
        # reporting block is guarded; renderer bugs stay visible.
        try:
            state["n"] += 1
            n = state["n"]
            # PHASE 4B: this runs every 16 ms. The decision to print needs only
            # two cheap numbers; the full telemetry (and, under
            # NAKSHA_DEV_DIAGNOSTICS, the pairwise overlap audit that took
            # 34-44 s on the 27M set) is built ONLY when a line is printed.
            sub = int(getattr(mgr, "active_draw_points", 0) or 0)
            idle_now = str(getattr(mgr, "_camera_state", "")) == "IDLE"
            if (n == 1 or sub != state["last_submitted"]
                    or (idle_now and not state["printed_idle"])
                    or n % 120 == 0):
                tele = mgr.lod_budget_telemetry(
                    detail=os.environ.get("NAKSHA_DEV_DIAGNOSTICS", "").strip()
                    not in ("", "0", "false", "False"))
                sub = int(tele["submitted_points"])
                if idle_now:
                    state["printed_idle"] = True
                state["last_submitted"] = sub
                lod = tele.get("LOD0_blocks", 0), tele.get("LOD1_blocks", 0), \
                    tele.get("LOD2_blocks", 0), tele.get("LOD3_blocks", 0), \
                    tele.get("LOD4_blocks", 0)
                # .get() with an explicit sentinel: telemetry must never
                # fabricate 0, because 0 would read as "GPU has no normals".
                rn = tele.get("resident_normal_points", "unavailable")
                rn_txt = (f"{int(rn):,}" if isinstance(rn, (int, float))
                          else str(rn))
                # PART 28: the manager is authoritative for the display mode.
                # app.display_mode used to sit at its stale "rgb" default while
                # the manager had already fallen back to neutral (the file has
                # no RGB), which is the impossible
                # ui_mode=rgb / stream_mode=neutral pair in this very log line.
                try:
                    _sm = str(getattr(mgr, "display_mode", "") or "")
                    if _sm:
                        _want = streaming_ui_mode(_sm)
                        if _sm in MODES_REQUIRING_RGB and not dataset_has_rgb(mgr):
                            _want = "neutral"   # rgb is not available here
                        if str(getattr(app, "display_mode", "")) != _want:
                            app.display_mode = _want
                            print(f"[MODE RECONCILE] ui_mode -> {_want} "
                                  f"(manager={_sm}, "
                                  f"rgb_available={dataset_has_rgb(mgr)})",
                                  flush=True)
                except Exception:
                    pass
                print(f"[GUI STREAM TICK] tick={n} manager=0x{id(mgr):x} "
                      f"camera_state={tele.get('camera_state')} "
                      f"ui_mode={getattr(app, 'display_mode', '?')} "
                      f"stream_mode={getattr(mgr, 'display_mode', '?')} "
                      f"viewport={tele.get('viewport')} "
                      f"target_points={tele.get('target_points', 0):,} "
                      f"selected_points={tele.get('selected_points', 0):,} "
                      f"active_blocks={tele.get('selected_blocks', 0)} "
                      f"submitted_points={sub:,} "
                      f"resident_normal_points={rn_txt} "
                      f"LOD0/1/2/3/4={lod} "
                      f"actual_ppp={tele.get('actual_ppp')}")
        except Exception as _tele_exc:
            # Dev telemetry must NEVER abort a frame tick. Report once and
            # keep streaming - the render path already completed above.
            if not state.get("tele_warned"):
                state["tele_warned"] = True
                print(f"WARNING: [stream] telemetry suppressed: "
                      f"{type(_tele_exc).__name__}: {_tele_exc}")
    return tick


def _install_camera_hook(app, mgr):
    """Connect interactive camera motion to the stream manager with
    latest-camera-wins generation tickets.

    THREE sources are observed, because a real session moves the camera in
    three different ways and only ONE of them is an interactor drag:

      * InteractionEvent / EndInteractionEvent - VTK's own mouse drag,
      * the active camera's ModifiedEvent - programmatic moves: the Fit button,
        a keyboard shortcut, ResetCamera(), an ortho-2D policy reset,
      * a StartEvent render-window observer - a catch-all for anything that
        mutates the camera between frames.

    Previously only the first was observed, so a Fit button click or a
    programmatic reset produced NO generation, NO note_camera_motion() and NO
    MOVING transition - exactly the log signature where the stream looks
    frozen after startup.
    """
    vtk_widget = getattr(app, "vtk_widget", None)
    if vtk_widget is None:
        print("WARNING: [stream] no vtk_widget: camera hook NOT installed")
        return
    try:
        renderer = vtk_widget.GetRenderWindow().GetRenderers().GetFirstRenderer()
        interactor = renderer.GetRenderWindow().GetInteractor()
        vcam = renderer.GetActiveCamera()
    except Exception:
        renderer = None
        interactor = getattr(vtk_widget, "interactor", None) or \
            getattr(vtk_widget, "_vtk_interactor", None)
        vcam = None
    if interactor is None:
        print("WARNING: [stream] no VTK interactor: camera hook NOT installed")
        return

    # PHASE 1: there is exactly ONE camera-generation source - the manager,
    # which derives the generation from the canonical camera signature. This
    # hook used to mint its own counter incremented on EVERY raw event
    # (interaction / end / mousemove / camera_modified / render_start), so five
    # events describing the SAME camera produced five "generations". The local
    # counter now tracks only the SOURCE LABEL, never the streaming generation.
    _counter = {"events": 0}
    _last = {"sig": None}

    def _classify(cam_obj, vp):
        """PAN / ZOOM / FIT-ish, from the change in the camera signature."""
        sig = mgr._camera_signature(cam_obj, vp)
        prev = _last["sig"]
        _last["sig"] = sig
        if prev is None or sig is None:
            return "INIT"
        dcx = abs(sig[0] - prev[0]) * 1e-4
        dcy = abs(sig[1] - prev[1]) * 1e-4
        dw = abs(sig[2] - prev[2]) * 1e-4
        dh = abs(sig[3] - prev[3]) * 1e-4
        moved = dcx > 1e-3 or dcy > 1e-3
        scaled = dw > 1e-3 or dh > 1e-3
        if scaled and not moved:
            return "ZOOM"
        if moved and scaled:
            return "ZOOM+PAN"
        if moved:
            return "PAN"
        return "OTHER"

    def on_interaction(source="interactor"):
        if not is_streaming_dataset(app):
            return
        cam, vp = _viewport(app)
        if vp is None:
            return
        # PART 1/2: log at the REAL interaction callback, before the manager
        # is involved, so GUI-side and stream-side motion can be compared.
        kind = _classify(cam, vp)
        _counter["events"] += 1
        # PHASE 2: a degenerate sample (world size 0, NaN, non-positive
        # viewport) is rejected by the manager BEFORE any frontier, service
        # generation or camera state is touched. `render_start` fires on every
        # frame, so this is exactly where a zero-size sample arrives.
        gen = int(getattr(mgr, "camera_generation", 0) or 0)
        try:
            if vp[2] > 0.0 and vp[3] > 0.0:
                _print_real_camera_event(source, kind, cam, vp, gen, mgr)
            else:
                print(f"[REAL CAMERA EVENT] event={kind} source={source} "
                      f"center=({float(vp[0]):.2f}, {float(vp[1]):.2f}) "
                      f"world_width={2.0 * float(vp[2]):.2f} "
                      f"world_height={2.0 * float(vp[3]):.2f} "
                      f"REJECTED=zero_size_camera")
        except Exception as exc:
            print(f"WARNING: [stream] camera event logging failed: {exc!r}")
        try:
            mgr.on_camera_changed(cam, vp, source=source)
        except Exception as e:
            print(f"WARNING: [stream] camera-change error: {e!r}")

    # Direct (VTK-free) entry for MainCamera2D-driven navigation.
    app._stream_camera_notify = on_interaction

    try:
        interactor.AddObserver("InteractionEvent",
                               lambda *a: on_interaction("interaction"))
        interactor.AddObserver("EndInteractionEvent",
                               lambda *a: on_interaction("end_interaction"))
        interactor.AddObserver("MouseMoveEvent",
                               lambda *a: on_interaction("mouse_move"))
    except Exception as e:
        print(f"WARNING: [stream] could not install interactor observers: {e!r}")

    # Programmatic camera changes (Fit button, shortcuts, ResetCamera).
    if vcam is not None:
        try:
            vcam.AddObserver("ModifiedEvent",
                             lambda *a: on_interaction("camera_modified"))
        except Exception as e:
            print(f"WARNING: [stream] camera ModifiedEvent observer: {e!r}")

    # Last-resort catch-all: any frame that rendered with a changed camera.
    try:
        ren_win = vtk_widget.GetRenderWindow()
        ren_win.AddObserver("StartEvent",
                            lambda *a: on_interaction("render_start"))
    except Exception as e:
        print(f"WARNING: [stream] render StartEvent observer: {e!r}")
    print("[STREAM CAMERA HOOK] installed sources=interaction,end,mousemove,"
          "camera_modified,render_start "
          f"manager=0x{id(mgr):x}")


def _print_real_camera_event(source, kind, cam, vp, gen, mgr):
    """PART 1/2 - GUI camera and stream camera, printed side by side."""
    import time as _t
    hw = float(vp[2]) if vp and len(vp) > 2 else 0.0
    hh = float(vp[3]) if vp and len(vp) > 3 else 0.0
    w_px = int(getattr(cam, "width_px", 0) or 0)
    h_px = int(getattr(cam, "height_px", 0) or 0)
    print(f"[REAL CAMERA EVENT] event={kind} source={source} "
          f"center=({float(vp[0]):.2f}, {float(vp[1]):.2f}) "
          f"world_width={2.0 * hw:.2f} world_height={2.0 * hh:.2f} "
          f"viewport={w_px}x{h_px} "
          f"generation={gen} manager=0x{id(mgr):x} "
          f"timestamp={_t.time():.3f}")
    print(f"[STREAM CAMERA UPDATE] manager_id={hex(id(mgr))} "
          f"center_x={float(vp[0]):.2f} center_y={float(vp[1]):.2f} "
          f"width_world={2.0 * hw:.2f} height_world={2.0 * hh:.2f} "
          f"width_px={w_px} height_px={h_px} "
          f"camera_generation={gen} "
          f"parallel_scale_equiv={hh:.4f}")


def _viewport(app):
    """Build the LOD gate's Camera2D/Camera3D + a world rectangle from the
    active VTK camera. Returns (camera, viewport) or (None, None).

    The LOD gate expects its OWN camera objects (Camera2D = orthographic
    top-down with exact world-units-per-pixel, Camera3D = perspective), NOT a
    raw vtkCamera. Passing a vtkCamera to ScreenSpaceLOD.select() would fail
    because the gate calls cam.project_box() / cam.px_per_m().

    Orthographic -> Camera2D when the view is top-down (view dir along -Z);
    otherwise an oblique/ortho camera is treated as perspective using the
    parallel scale, which errs toward FINER detail (never toward holes).
    """
    try:
        import numpy as _np
        from naksha_lod_gate import Camera2D, Camera3D
        sz = app.vtk_widget.size()
        w = max(int(sz.width()), 1)
        h = max(int(sz.height()), 1)
        # MainCamera2D is the source of truth when the Vulkan main viewport owns
        # the camera: the selector's visible bounds come from IT, not from VTK.
        _owner = getattr(app, "render_backend", None)
        if _owner is not None and _owner.owns_main_camera() and _owner.main_camera.valid:
            _mc = _owner.main_camera
            _hh = float(_mc.parallel_scale)
            _hw = _hh * (w / h)
            return (Camera2D(float(_mc.center_x), float(_mc.center_y), _hw, w, h),
                    (float(_mc.center_x), float(_mc.center_y), _hw, _hh))
        ren = app.vtk_widget.GetRenderWindow().GetRenderers().GetFirstRenderer()
        vcam = ren.GetActiveCamera()
        parallel = bool(vcam.GetParallelProjection())
        pos = _np.asarray(vcam.GetPosition(), float)
        fp = _np.asarray(vcam.GetFocalPoint(), float)
        up = _np.asarray(vcam.GetViewUp(), float)

        if parallel:
            half_h = float(vcam.GetParallelScale())   # VTK: half VERTICAL extent
            half_w = half_h * (w / h)
            top_down = abs(abs(float(vcam.GetViewPlaneNormal()[2])) - 1.0) < 1e-3
            if top_down:
                # Exact orthographic top-down -> Camera2D.
                return (Camera2D(float(fp[0]), float(fp[1]), half_w, w, h),
                        (float(fp[0]), float(fp[1]), half_w, half_h))
            # Oblique orthographic: approximate as perspective at the focal
            # distance so the LOD gate still gets a valid camera object.
            dist = float(_np.linalg.norm(pos - fp)) or 1.0
            fov = 2.0 * _np.degrees(_np.arctan(half_h / dist))
            return (Camera3D(pos, fp, max(fov, 1e-3), w, h),
                    (float(fp[0]), float(fp[1]), half_w, half_h))

        # Perspective.
        fov = float(vcam.GetViewAngle())
        dist = float(_np.linalg.norm(pos - fp))
        cam = Camera3D(pos, fp, fov, w, h)
        # World rectangle at the FOCAL PLANE (where LOD detail matters most).
        half_h = dist * _np.tan(_np.radians(fov * 0.5))
        half_w = half_h * (w / h)
        return (cam, (float(fp[0]), float(fp[1]), half_w, half_h))
    except Exception as e:
        print(f"WARNING: [stream] viewport/camera capture failed: {e!r}")
        return (None, None)


def assert_not_monolithic_upload(app):
    """Stage 3C hard gate: must never be called in STREAMING mode. Increments
    the counter analyze_stage3c.py requires to be 0."""
    if is_streaming_dataset(app):
        app._streaming_full_point_upload_attempts = \
            getattr(app, "_streaming_full_point_upload_attempts", 0) + 1
        print("WARNING: [STAGE 3C HARD GATE] monolithic upload_point_cloud() "
              "called in STREAMING mode -- this must be 0!")
