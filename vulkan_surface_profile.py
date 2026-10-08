"""[VULKAN PROFILE] Real, visible-window profiler for the native Vulkan path.

Fixes the two harness defects that made earlier runs meaningless:

  A. Dataset path - the loader input is normalized so a bare
     "test_classified_highprecision.laz" is ONE file, not 33 characters.
  B. Data-ready wait - the run does not give up because the state machine
     starts at EMPTY. It waits for the real sequence
         EMPTY -> LOADING -> FIRST_FRAME_READY / READY
     and additionally requires all three of:
         len(app.data["xyz"]) > 0
         Vulkan resident point count > 0
         present count advanced

The window is a REAL, VISIBLE Qt window. Nothing here is headless or
offscreen: an offscreen run cannot produce the presentation-paced frame times
this task is asking about.
"""
import ctypes
import os
import time

os.environ.setdefault("NAKSHA_RENDER_BACKEND", "vulkan")
os.environ.setdefault("NAKSHA_VULKAN_DISABLE_ALL_VTK_RENDER", "1")

DATASET = os.environ.get("NAKSHA_TEST_LAS", "test_classified_highprecision.laz")


def normalize_paths(filenames):
    """str -> [str]; list/tuple -> unchanged (as a list of str).

    A bare string is a SINGLE filename. Iterating it directly produced one
    "file" per character (t, e, s, t, ...) which then failed as 33 bogus
    paths. This is the harness-side mirror of the same normalization in
    NakshaApp.open_file, kept here so the profiler is correct even against a
    future loader that lacks it.
    """
    if isinstance(filenames, (str, os.PathLike)):
        return [str(filenames)]
    return [str(p) for p in filenames]


def pump(app, seconds):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.processEvents()
        time.sleep(0.005)


def backend_of(win):
    rb = getattr(win, "render_backend", None)
    if rb is None or not getattr(rb, "active", False):
        return None, None
    return rb, getattr(rb, "vulkan_backend", None)


def n(fn, default=0):
    try:
        v = fn()
        return default if v is None else v
    except Exception:
        return default


def wait_for_data(app, win, timeout_s=900.0):
    """Block until the data is genuinely loaded and presenting frames.

    Returns (ok, reason). Every condition is MEASURED, not inferred from the
    state label alone - a READY label with zero resident points is a failure,
    not a pass.
    """
    t0 = time.perf_counter()
    seen_states = []
    last_state = None
    rb, vb = backend_of(win)
    presents0 = n(lambda: vb.get_frame_stats()[0]) if vb else 0
    while time.perf_counter() - t0 < timeout_s:
        app.processEvents()
        state = str(getattr(win, "_data_state", "") or "")
        if state != last_state:
            seen_states.append(state)
            last_state = state
        pts = 0
        try:
            xyz = (win.data or {}).get("xyz")
            pts = 0 if xyz is None else len(xyz)
        except Exception:
            pts = 0
        resident = n(lambda: vb.get_point_count()) if vb else 0
        presents = n(lambda: vb.get_frame_stats()[0]) if vb else 0
        if (state in ("FIRST_FRAME_READY", "READY", "POINTS_READY")
                and pts > 0 and resident > 0 and presents > presents0):
            return True, (f"state={state} points={pts:,} resident={resident:,} "
                          f"presents +{presents - presents0} "
                          f"states={seen_states}")
        time.sleep(0.1)
    return False, (f"timeout after {timeout_s:.0f}s; last state={last_state!r}, "
                   f"state sequence={seen_states}")


def report_dataset(win, vb):
    print("\n[PROFILE DATASET]", flush=True)
    print(f"  Path:            {DATASET}")
    print(f"  Exists:          {os.path.isfile(DATASET)}")
    pts = 0
    try:
        xyz = (win.data or {}).get("xyz")
        pts = 0 if xyz is None else len(xyz)
    except Exception:
        pass
    print(f"  Point count:     {pts:,}")
    print(f"  Data state:      {getattr(win, '_data_state', '?')}")
    print(f"  Resident points: {n(lambda: vb.get_point_count()):,}")
    print(f"  Present count:   {n(lambda: vb.get_frame_stats()[0]):,}")


def measure(app, rb, vb, seconds=2.0):
    """Sample frame timing for `seconds` and return a stats dict.

    Qt events MUST be pumped inside this loop. A tight request_render() loop
    with no event processing never lets the compositor present, so the frame
    fence is not signalled in time and the NEXT AcquireNextImage blocks
    forever - the window has to stay a real, presented window while we
    measure, which is exactly what the task requires anyway.

    request_render lives on the VulkanRenderBackend (the C ABI seam), not on
    the AppRenderBackendOwner that owns it; request_render_safe() is used
    because a diagnostic frame request must never abort the run.
    """
    r0 = n(lambda: vb.get_frame_stats()[0])
    s0 = n(lambda: vb.get_frame_stats()[1])
    t0 = time.perf_counter()
    cpu = gpu = point_ms = surf_ms = present = 0.0
    valid = 0
    fn = getattr(vb._dll, "nkv_get_frame_timing", None)
    while time.perf_counter() - t0 < seconds:
        app.processEvents()
        vb.request_render_safe()
        if fn is not None:
            a, b, c, d, e, v = (ctypes.c_double(), ctypes.c_double(), ctypes.c_double(),
                                ctypes.c_double(), ctypes.c_double(), ctypes.c_int())
            try:
                fn(ctypes.c_uint64(vb._handle), ctypes.byref(a), ctypes.byref(b),
                   ctypes.byref(c), ctypes.byref(d), ctypes.byref(e),
                   ctypes.byref(v))
            except Exception:
                v = ctypes.c_int(0)
            if v.value:
                valid += 1
                cpu += a.value
                gpu += b.value
                point_ms += c.value
                surf_ms += d.value
                present += e.value
        time.sleep(0.002)
    wall = time.perf_counter() - t0
    rendered = max(0, n(lambda: vb.get_frame_stats()[0]) - r0)
    skipped = max(0, n(lambda: vb.get_frame_stats()[1]) - s0)
    k = valid if valid else 0
    return {"wall_s": wall, "rendered": rendered, "skipped": skipped,
            "fps": rendered / wall if wall > 0 else 0.0,
            "cpu_submit_ms": cpu / k if k else -1.0,
            "gpu_frame_ms": gpu / k if k else -1.0,
            "gpu_point_ms": point_ms / k if k else -1.0,
            "gpu_surface_ms": surf_ms / k if k else -1.0,
            "present_ms": present / k if k else -1.0}


def print_baseline(vb, mode_label, d, quality=None):
    tiled = vb.supports_tiled_surface()
    cull = vb.get_surface_culling() if tiled else {}
    lod = vb.get_surface_lod() if tiled else {}
    mem = vb.get_surface_memory() if tiled else {}
    # This T400 reports timestampValidBits == 0, so the GPU timestamp pool is
    # UNUSABLE and every GPU figure below is honestly "not available" rather
    # than a fake 0.0. Frame cost is therefore reported from the presentation
    # side (wall clock per presented frame), which is what a user actually
    # experiences.
    gpu_ok = d["gpu_frame_ms"] >= 0
    f = lambda v: (f"{v:.2f}" if isinstance(v, (int, float)) and v >= 0 else "n/a")
    print(f"\n[VULKAN BASELINE]\n\nMode: {mode_label}", flush=True)
    rng = n(lambda: vb.get_point_draw_range_count())
    pts = n(lambda: vb.get_point_draw_range_points())
    print(f"  Visible points:      {pts:,}" if rng > 0
          else "  Visible points:      all (single full-buffer draw)")
    print(f"  Surface triangles:   {n(lambda: vb.get_surface_index_count()) // 3:,}")
    print(f"  Overlay triangles:   {n(lambda: vb.get_overlay_triangle_count()):,}")
    print(f"  GPU frame:           {f(d['gpu_frame_ms'])} ms"
          + ("" if gpu_ok else "   (GPU timestamps unavailable on this device)"))
    print(f"  Point pass:          {f(d['gpu_point_ms'])} ms")
    print(f"  Surface pass:        {f(d['gpu_surface_ms'])} ms")
    print(f"  CPU submit:          {f(d['cpu_submit_ms'])} ms")
    print(f"  Present:             {f(d['present_ms'])} ms")
    ms = (d["wall_s"] / d["rendered"] * 1000.0) if d["rendered"] else -1.0
    print(f"  Frame (wall/frame):  {f(ms)} ms")
    print(f"  FPS:                 {d['fps']:.1f}")
    print(f"  Point uploads:       {n(lambda: vb.get_point_position_upload_count()):,}")
    print(f"  Surface uploads:     {n(lambda: vb.get_surface_upload_count()):,}")
    if quality:
        print(f"  surface quality mode: {quality}")
    print(f"  surface selected pts: {n(lambda: vb.get_surface_vertex_count()):,}")
    print(f"  surface faces:        {n(lambda: vb.get_surface_index_count()) // 3:,}")
    if cull and cull.get("total_tiles", -1) >= 0:
        print(f"  tiles vis/total:      {cull['visible_tiles']:,} / "
              f"{cull['total_tiles']:,}  (culled {cull['culled_tiles']:,})")
        print(f"  tris submitted/total: {cull['visible_triangles']:,} / "
              f"{cull['total_triangles']:,}  (culled {cull['culled_triangles']:,})")
    if lod and lod.get("lod", -1) >= 0:
        print(f"  surface LOD:          {lod['lod']} "
              f"({'MOVING' if lod['moving'] else 'IDLE'})")
        print(f"  LOD triangles:        {lod['counts']}")
    if mem and mem.get("budget", -1) >= 0:
        MB = 1048576.0
        print(f"  VRAM budget/active:   {mem['budget'] / MB:,.0f} / "
              f"{mem['active'] / MB:,.0f} MB")


def cam_action(win, what):
    """Drive the real camera through the shared rig (the same path a gesture takes)."""
    owner = getattr(win, "render_backend", None)
    rig = getattr(owner, "_camera_rig", None)
    if rig is None or owner is None:
        return False
    try:
        if what == "zoom_in":
            rig.dolly(3.0)
        elif what == "zoom_out":
            rig.dolly(-3.0)
        elif what == "pan":
            rig.pan(60.0, 40.0, max(1, win.height()))
        owner._apply_rig_gesture()
        owner._note_surface_interaction(True)
        return True
    except Exception as exc:
        print(f"  cam({what}) failed: {exc!r}")
        return False


app_global = None


def run_case(app, win, rb, vb, mode, quality=None, seconds=2.0):
    """Switch mode, exercise FIT / ZOOM / PAN, and report each."""
    print(f"\n{'=' * 70}\nMODE: {mode}"
          + (f"  (quality={quality})" if quality else ""), flush=True)
    if quality is not None:
        try:
            win.surface_quality = quality
        except Exception:
            pass
    try:
        win.set_display_mode(mode)
    except Exception as exc:
        print(f"  set_display_mode({mode}) FAILED: {exc!r}")
        return
    # Let the surface build + upload + atomic swap settle. Point-cloud mode
    # never produces a surface, so its state is legitimately EMPTY (0) and
    # waiting for a surface state there would just burn the whole timeout.
    if mode != "pointcloud" and vb.supports_tiled_surface():
        deadline = time.perf_counter() + 240.0
        while time.perf_counter() < deadline:
            pump(app, 0.5)
            if vb.get_surface_state() in (1, 3):
                break
    pump(app, 1.5)
    print("  " + win.render_backend.surface_state_report(), flush=True)
    for action in ("fit", "zoom_in", "pan", "zoom_out"):
        cam_action(win, action)
        pump(app, 0.8)
        d = measure(app, rb, vb, seconds)
        print(f"  [{action:9s}] FPS={d['fps']:5.1f}  GPU={d['gpu_frame_ms']:7.2f}ms  "
              f"surface={d['gpu_surface_ms']:7.2f}ms  "
              f"presented={d['rendered']:4d}  skipped={d['skipped']}", flush=True)
    # Settle: let the idle debounce refine to the final level, then measure idle.
    pump(app, 1.5)
    d = measure(app, rb, vb, seconds)
    print(f"  [{'idle':9s}] FPS={d['fps']:5.1f}  GPU={d['gpu_frame_ms']:7.2f}ms  "
          f"surface={d['gpu_surface_ms']:7.2f}ms  presented={d['rendered']:4d}",
          flush=True)
    print_baseline(vb, mode, d, quality)
    if vb.supports_tiled_surface():
        for rep in (rb.surface_culling_report(), rb.surface_lod_report(),
                    rb.surface_memory_report()):
            print("\n" + rep, flush=True)


def main():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from gui.app_window import NakshaApp
    win = NakshaApp()
    win.show()                      # REAL, VISIBLE window
    win.resize(1600, 900)
    pump(app, 2.0)

    paths = normalize_paths(DATASET)
    print(f"[PROFILE] loading {paths}", flush=True)
    t_load = time.perf_counter()
    win.open_file(paths, prompt_import=False)
    ok, reason = wait_for_data(app, win)
    print(f"[PROFILE] load {'OK' if ok else 'FAILED'} in "
          f"{time.perf_counter() - t_load:,.1f}s: {reason}", flush=True)
    rb, vb = backend_of(win)
    if rb is None or vb is None:
        print("[PROFILE] Vulkan backend NOT ACTIVE - cannot profile")
        return 1
    report_dataset(win, vb)
    print(f"[PROFILE] native tiled surface path: "
          f"{'AVAILABLE' if vb.supports_tiled_surface() else 'NOT AVAILABLE'}")

    quality = os.environ.get("NAKSHA_SURFACE_QUALITY", "normal")
    # NAKSHA_PROFILE_MODES lets a verification run skip the (slow) modes it has
    # already measured, e.g. NAKSHA_PROFILE_MODES=surface
    modes = os.environ.get("NAKSHA_PROFILE_MODES", "pointcloud,surface,shaded_class")
    for mode in [m.strip() for m in modes.split(",") if m.strip()]:
        run_case(app, win, rb, vb, mode,
                 quality if mode == "surface" else None)
    print("\n[PROFILE] done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
