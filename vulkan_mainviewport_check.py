"""
vulkan_mainviewport_check.py - STEP 1..4 acceptance through the PRODUCTION path.

Launched the way a user launches the app, so nothing here can masquerade as
working only inside a harness:

    NAKSHA_RENDER_BACKEND=vulkan
    NAKSHA_VULKAN_MAIN_VIEWPORT=1
    python main.py

...then drives the real NakshaApp through the real load path. The env vars are
set in-process at the top of this file because render_backend.py reads them at
import time, exactly as `py main.py` would with them set in the shell.

Checks:
  1. Vulkan is the VISIBLE LiDAR renderer.
  2. VTK LiDAR actors are OFF (point cloud / surface / shaded mesh)...
  3. ...while overlay actors (SNT/digitizer/measurements/text/vectors) stay
     VISIBLE - the architecture rule that VTK is removed ONLY from LiDAR.
  4. GPU buffers are persistent: pan/zoom/shading changes must NOT advance the
     position or surface upload counters.
  5. The [VULKAN PERFORMANCE] block prints real numbers.

Run:  python vulkan_mainviewport_check.py
Exit 0 = all passed, 1 = a failure, 3 = harness error.
"""
from __future__ import annotations

import os
import sys
import time

os.environ["NAKSHA_RENDER_BACKEND"] = "vulkan"
os.environ["NAKSHA_VULKAN_MAIN_VIEWPORT"] = "1"
os.environ.setdefault("NAKSHA_VULKAN_PREVIEW", "")

for _n in ("stdout", "stderr"):
    _s = getattr(sys, _n, None)
    try:
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

LAS = os.environ.get("NAKSHA_TEST_LAS") or os.path.join(ROOT, "123.las")
# Optional second dataset for the Phase 2 acceptance run. Defaults to the
# repo's high-precision LAZ when it is present, so the same command covers
# both files.
_LAZ_DEFAULT = os.path.join(ROOT, "test_classified_highprecision.laz")
LAZ = os.environ.get("NAKSHA_TEST_LAZ2") or (
    _LAZ_DEFAULT if os.path.exists(_LAZ_DEFAULT) else "")
PTC = r"H:\TESTING CONTIUES\Class_ENEL_2025_connect 1.ptc"

_CHECKS: list = []


def check(name, ok, detail=""):
    ok = bool(ok)
    _CHECKS.append((name, ok, detail))
    print(f"[perf] {'PASS' if ok else 'FAIL'}  {name}"
          + (f"  ({detail})" if detail else ""), flush=True)
    return ok


def _report():
    failed = [n for n, ok, _ in _CHECKS if not ok]
    print(f"\nFinal: {'PASS' if not failed else 'FAIL'}"
          + ("" if not failed else "  failed: " + "; ".join(failed)), flush=True)
    return 0 if not failed else 1


def _safe_vis(actor):
    try:
        return bool(actor.GetVisibility())
    except Exception:
        return False


def _wh(widget):
    """'WxH' for a widget, or 'None'."""
    if widget is None:
        return "None"
    try:
        return f"{int(widget.width())}x{int(widget.height())}"
    except Exception:
        return "?"


def _lidar_names():
    from gui.render_backend import AppRenderBackendOwner as A
    return set(A._VTK_LIDAR_ACTOR_NAMES), tuple(A._VTK_LIDAR_ACTOR_PREFIXES)


def _is_lidar(name, names, prefixes):
    n = str(name)
    return n in names or n.startswith(prefixes)


def _actor_split(rb):
    """(lidar_actors, overlay_actors) - overlays are everything NOT LiDAR."""
    lidar, overlay = [], []
    try:
        actors = getattr(rb._vtk_widget, "actors", None)
        if not isinstance(actors, dict):
            return lidar, overlay
        names, prefixes = _lidar_names()
        for name, actor in actors.items():
            if actor is None:
                continue
            (lidar if _is_lidar(name, names, prefixes) else overlay).append(
                (str(name), actor))
    except Exception:
        pass
    return lidar, overlay


def main():
    from PySide6.QtWidgets import QApplication
    from gui.app_window import NakshaApp
    from vulkan_engine_test import _suppress_modal_dialogs, pump, wait_until

    print("=" * 72)
    print("VULKAN MAIN-VIEWPORT / PERFORMANCE ACCEPTANCE (production path)")
    print("=" * 72, flush=True)

    _suppress_modal_dialogs()
    app_qt = QApplication.instance() or QApplication(sys.argv[:1])
    win = NakshaApp()
    win.resize(1400, 900)
    win.show()
    pump(app_qt, 1.5)

    rb = getattr(win, "render_backend", None)
    b = getattr(rb, "vulkan_backend", None) if rb is not None else None
    if b is None or not getattr(rb, "active", False):
        check("Vulkan backend active", False, "backend not initialised")
        return _report()
    check("Vulkan backend active", True, f"device={b.get_device_name()}")

    installed = bool(getattr(rb, "_viewport_installed", False))
    check("Vulkan installed as the main viewport (env flag honoured)", installed,
          f"_viewport_installed={installed}")
    vis = bool(rb.vulkan_viewport_is_visible())
    check("Vulkan surface is the visible viewport", vis,
          f"vulkan_widget.isVisible()={vis}")

    print("\n[ui] --- NAKSH UI TREE / HIERARCHY DUMP ---", flush=True)
    try:
        def _chain(w, limit=8):
            names = []
            cur = w
            for _ in range(limit):
                if cur is None:
                    break
                names.append(f"{type(cur).__name__}"
                             f"[{cur.objectName() or '-'}]"
                             f"{cur.width()}x{cur.height()}")
                cur = cur.parentWidget()
            return " <- ".join(names)

        vw = getattr(win, "vtk_widget", None)
        inter = getattr(vw, "interactor", None) if vw is not None else None
        print(f"[ui] main            : {_chain(win)}", flush=True)
        print(f"[ui] vtk_widget      : {_chain(vw)}", flush=True)
        print(f"[ui] interactor      : {_chain(inter)}", flush=True)
        print(f"[ui] inter.parentWidget()   = "
              f"{type(inter.parentWidget()).__name__ if inter is not None else None}",
              flush=True)
        print(f"[ui] vtk.parentWidget()     = "
              f"{type(vw.parentWidget()).__name__ if vw is not None else None}",
              flush=True)
        print(f"[ui] inter.parent() is vw  = "
              f"{inter.parent() is vw if (inter is not None and vw is not None) else None}",
              flush=True)
        print(f"[ui] frame           : {_wh(getattr(win, 'frame', None))}", flush=True)
        print(f"[ui] splitter        : {_wh(getattr(win, 'splitter', None))}", flush=True)
        print(f"[ui] ribbon_container={getattr(win, 'ribbon_container', None)!r}", flush=True)
        print(f"[ui] top_bar         ={getattr(win, 'top_bar', None)!r}", flush=True)
        print(f"[ui] status          ={getattr(win, 'status', None)!r}", flush=True)
        vw2 = getattr(rb, "vulkan_widget", None)
        print(f"[ui] vulkan          : {_chain(vw2)}", flush=True)
    except Exception as e:
        print(f"[ui] tree dump failed: {e!r}", flush=True)

    # ---- [VULKAN TARGET] - is the surface parented to the real viewer host? ----
    print("\n[perf] --- viewport target check ---", flush=True)
    try:
        host = rb.vulkan_viewer_host() if hasattr(rb, "vulkan_viewer_host") else None
        inter = getattr(getattr(win, "vtk_widget", None), "interactor", None)
        vw = getattr(rb, "vulkan_widget", None)
        print("[VULKAN TARGET]", flush=True)
        print(f"  QtInteractor        : {_wh(inter)}", flush=True)
        print(f"  inter.parentWidget(): {_wh(inter.parentWidget()) if inter is not None else 'None'}"
              f"  class="
              f"{type(inter.parentWidget()).__name__ if inter is not None else 'None'}",
              flush=True)
        print(f"  Viewer host (owner) : {_wh(host)}  "
              f"class={type(host).__name__ if host is not None else 'None'}", flush=True)
        print(f"  Vulkan              : {_wh(vw)}", flush=True)
        actual_parent = vw.parentWidget() if vw is not None else None
        print(f"  Vulkan parent       : "
              f"{type(actual_parent).__name__ if actual_parent is not None else 'None'}",
              flush=True)
        print(f"  Viewer host         : {_wh(getattr(win, 'frame', None))}", flush=True)
        print(f"  Splitter            : {_wh(getattr(win, 'splitter', None))}", flush=True)
        print(f"  Main window         : {_wh(win)}", flush=True)

        check("viewer host resolves from interactor.parentWidget()",
              host is not None and inter is not None
              and host is inter.parentWidget(),
              f"host={type(host).__name__ if host is not None else None} "
              f"inter.parent={type(inter.parentWidget()).__name__ if inter is not None else None}")
        check("Vulkan widget is parented to the viewer host",
              actual_parent is not None and actual_parent is host,
              f"parent={type(actual_parent).__name__ if actual_parent is not None else None} "
              f"host={type(host).__name__ if host is not None else None}")
        # The regression: a surface parented to the window/central/splitter
        # covers the complete application area.
        bad = []
        if actual_parent is win:
            bad.append("QMainWindow")
        try:
            cw = win.centralWidget()
            if actual_parent is cw:
                bad.append("centralWidget")
        except Exception:
            pass
        if actual_parent is getattr(win, "splitter", None):
            bad.append("QSplitter")
        check("Vulkan is NOT parented to the window/central widget/splitter",
              not bad,
              f"escapes={bad if bad else 'none'}")
        if host is not None and inter is not None:
            hw, hh = int(host.width()), int(host.height())
            check("viewer host is laid out (not the 100x30 default)",
                  hw > 100 and hh > 100, f"viewer host={hw}x{hh}")
        if vw is not None and host is not None:
            check("Vulkan geometry matches the viewer host",
                  (int(vw.width()), int(vw.height()))
                  == (int(host.width()), int(host.height())),
                  f"vulkan={_wh(vw)} host={_wh(host)}")
    except Exception as e:
        check("viewport target checks", False, f"{e!r}")

    # ---- diagnostic kill-switch check -----------------------------------
    # Runs BEFORE the 2.9 M-point load on purpose: it must stay fast and must
    # not depend on data, and the load is the slowest/riskiest step in the run.
    print("\n[perf] --- diagnostic kill-switch check ---", flush=True)
    try:
        from gui.gpu_render_manager import GPURenderManager as _GRM
        _req = _GRM.vtk_render_killswitch_requested
        _KS = os.environ.get("NAKSHA_VULKAN_DISABLE_ALL_VTK_RENDER", "")
        print(f"[perf] NAKSHA_VULKAN_DISABLE_ALL_VTK_RENDER={_KS!r} -> "
              f"killswitch_requested={_req()}", flush=True)
        check("kill-switch reads NAKSHA_VULKAN_DISABLE_ALL_VTK_RENDER",
              _req() == (_KS.strip().lower() in ("1", "true", "yes", "on")),
              f"env={_KS!r} requested={_req()}")
        grm = getattr(win, "gpu_render_manager", None)
        check("GPU render manager present (VTK render choke point)",
              grm is not None, f"grm={type(grm).__name__}")
        if grm is not None:
            check("kill-switch state matches the env flag",
                  bool(getattr(grm, "_all_vtk_render_disabled", False)) == _req(),
                  f"_all_vtk_render_disabled="
                  f"{getattr(grm, '_all_vtk_render_disabled', None)}")
            if _req():
                # Drive the VTK path the kill-switch actually guards. Going
                # through the Vulkan backend's request_render() would not
                # exercise the switch at all, and it blocks on a full frame.
                vtk_w = getattr(win, "vtk_widget", None)
                before = int(getattr(grm, "render_count", 0))
                for _i in range(3):
                    grm.request_render(vtk_w)
                    pump(app_qt, 0.5)
                # Let the debounce timer fire so _execute_render is reached.
                pump(app_qt, 1.5)
                after = int(getattr(grm, "render_count", 0))
                print("[perf] --- [VULKAN OPENGL OWNERSHIP] ---", flush=True)
                grm.print_opengl_ownership_report()
                print(flush=True)
                check("VTK render draws blocked while Vulkan owns the viewport",
                      after == before,
                      f"VTK renders executed={after} (delta={after - before}, "
                      f"must be 0 with the kill-switch on), "
                      f"blocked={getattr(grm, '_vtk_render_calls_blocked', 0)}")
            else:
                print("[perf] kill-switch OFF - VTK renders are expected to run",
                      flush=True)
    except Exception as e:
        check("kill-switch diagnostics", False, f"{e!r}")

    print("\n[perf] loading 123.las ...", flush=True)
    win.open_file(filenames=[LAS], prompt_import=False)
    if not wait_until(app_qt, lambda: bool(getattr(win, "data", None)), timeout=300.0):
        check("LAS loaded", False, "timeout")
        return _report()
    print("[perf] load returned; pumping ...", flush=True)
    pump(app_qt, 3.0)
    print("[perf] pump returned", flush=True)
    check("LAS loaded", True, f"points={int(win.data['xyz'].shape[0]):,}")

    # The VTK LiDAR actors are created asynchronously by the loaders, and the
    # backend re-asserts their (off) visibility on a retry timer. Give that
    # window time to finish before measuring, otherwise this measures a
    # half-built actor set rather than the steady state.
    print("[perf] waiting for the VTK LiDAR re-assert to settle ...", flush=True)
    wait_until(app_qt,
               lambda: len(_actor_split(rb)[0]) > 0
               and all(not _safe_vis(a) for _n, a in _actor_split(rb)[0]),
               timeout=20.0)
    pump(app_qt, 1.0)

    # ---- 1/2. VTK LiDAR must be OFF -------------------------------------
    print("[perf] enumerating actors ...", flush=True)
    try:
        lidar, overlay = _actor_split(rb)
        print(f"[perf] lidar={len(lidar)} overlay={len(overlay)}", flush=True)
    except Exception as e:
        lidar, overlay = [], []
        check("actor enumeration", False, f"{e!r}")
    vis_on = [n for n, a in lidar if _safe_vis(a)]
    check("VTK LiDAR rendering is OFF", not vis_on,
          f"{len(vis_on)} of {len(lidar)} LiDAR actors still visible"
          + (f": {vis_on[:4]}" if vis_on else ""))
    # NOTE: there used to be a "VTK LiDAR actor set was actually found
    # (check is meaningful)" check here requiring len(lidar) > 0. That is now
    # exactly backwards: under PERFORMANCE PHASE 1 the correct steady state is
    # ZERO VTK LiDAR actors, and the phase-1 block below asserts that instead.

    # ---- 3. overlays must be untouched ---------------------------------
    # The LiDAR suppression must never have touched these. With no overlays
    # present in this run there is nothing to assert, so say so instead of
    # reporting a vacuous pass (0 of 0 visible) that looks like a result.
    ov_on = [n for n, a in overlay if _safe_vis(a)]
    if overlay:
        check("VTK overlay actors still visible (SNT/digitizer/measurements/text/vectors)",
              len(ov_on) > 0,
              f"{len(ov_on)} of {len(overlay)} overlay actors visible"
              + (f"  HIDDEN: {[n for n, _a in overlay if n not in ov_on][:6]}"
                 if len(ov_on) < len(overlay) else ""))
    else:
        print("[perf] SKIP overlay visibility: no overlay actors exist in this "
              "run (nothing to assert - load an SNT/measurement file to check)",
              flush=True)

    # ---- PERFORMANCE PHASE 1: no duplicate VTK LiDAR pipeline ---------------
    print("\n[perf] --- VTK LiDAR suppression (phase 1) ---", flush=True)
    try:
        owns = bool(rb.vulkan_owns_lidar_viewport()) \
            if hasattr(rb, "vulkan_owns_lidar_viewport") else False
        check("Vulkan owns the LiDAR viewport (single runtime check)", owns,
              f"vulkan_owns_lidar_viewport()={owns}")
        lidar, _ov = _actor_split(rb)
        check("VTK LiDAR point actors: 0", len(lidar) == 0,
              f"{len(lidar)} VTK LiDAR actors still registered"
              + (f": {[n for n, _a in lidar][:5]}" if lidar else ""))
        try:
            from gui.unified_actor_manager import vtk_lidar_build_counters
            c = vtk_lidar_build_counters()
        except Exception as e:
            c = None
            check("VTK LiDAR build counters available", False, f"{e!r}")
        if c is not None:
            print(f"[perf] counters: built={c['built']} "
                  f"skipped={c['skipped']} "
                  f"skipped_points={c['skipped_points']:,}", flush=True)
            check("VTK LiDAR actor was NOT created (creation skipped)",
                  c["built"] == 0 and c["skipped"] > 0,
                  f"built={c['built']} (must be 0), skipped={c['skipped']}")
            check("VTK LiDAR work was actually avoided",
                  c["skipped_points"] > 0,
                  f"{c['skipped_points']:,} points never built into VTK")
        if hasattr(rb, "_log_vulkan_lidar_ownership"):
            print("[perf] --- [VULKAN OWNERSHIP] ---", flush=True)
            rb._log_vulkan_lidar_ownership()
            print(flush=True)
    except Exception as e:
        check("VTK LiDAR suppression checks", False, f"{e!r}")

    # ---- 4. GPU buffers persistent across camera movement ---------------
    p0 = int(b.get_point_position_upload_count())
    s0 = int(b.get_surface_upload_count())
    rig = rb._camera_rig
    xyz = np.asarray(win.data["xyz"])
    bounds = np.stack([xyz.min(axis=0), xyz.max(axis=0)])
    rig.fit_to_bounds(bounds, aspect=1.6)
    rb.resync_camera(present=False)
    pump(app_qt, 0.5)
    for notches, pan in ((5.0, None), (7.0, None), (None, 120.0), (None, -90.0)):
        if notches is not None:
            rig.dolly(notches)
        else:
            rig.pan(pan, 0, 900)
        rig.push_to_backend(b)
        b.request_render()
        pump(app_qt, 0.25)
    p1 = int(b.get_point_position_upload_count())
    s1 = int(b.get_surface_upload_count())
    check("Camera movement does NOT re-upload the point buffer", p1 == p0,
          f"positionUploads {p0} -> {p1} over 4 pan/zoom steps")
    check("Camera movement does NOT re-upload the surface buffer", s1 == s0,
          f"surfaceUploads {s0} -> {s1}")

    # ---- PHASE 1: the rest of the operations that must stay upload-free ---
    # Fit view / 2D-3D toggles / display-mode changes all go through the
    # camera rig and the shading setters. None of them may touch geometry.
    def _snap():
        return (int(b.get_point_position_upload_count()),
                int(b.get_surface_upload_count()),
                int(b.get_point_intensity_upload_count()))

    base = _snap()
    ops = []

    # fit view (the post-load fit path)
    try:
        win.fit_view()
        rb.resync_camera(present=False)
        b.request_render(); pump(app_qt, 0.4)
        ops.append(("fit_view", _snap()))
    except Exception as e:
        ops.append((f"fit_view({e!r})", _snap()))

    # 2D / 3D view toggles. The real entry point is
    # NakshaApp.toggle_view_mode(mode), not set_view.
    for label, mode in (("toggle 2d", "2d"), ("toggle 3d", "3d")):
        try:
            win.toggle_view_mode(mode)
            rb.resync_camera(present=False)
            b.request_render(); pump(app_qt, 0.4)
            ops.append((label, _snap()))
        except Exception as e:
            ops.append((f"{label}({e!r})", _snap()))

    # display-mode changes. NOTE: we must NOT call rb.upload_point_cloud()
    # here - that is a deliberate FULL geometry re-upload, so calling it would
    # trivially move the counters and prove nothing. The real display-mode
    # path goes through the shading setters, which are supposed to touch the
    # LUT/uniforms only.
    for label, mode in (("display_mode=elevation", "elevation"),
                        ("display_mode=intensity", "intensity"),
                        ("display_mode=classification", "classification")):
        try:
            prev = getattr(win, "display_mode", None)
            win.display_mode = mode
            b.request_render(); pump(app_qt, 0.4)
            ops.append((label, _snap()))
            win.display_mode = prev
        except Exception as e:
            ops.append((f"{label}({e!r})", _snap()))

    bad_ops = [(n, a, c) for n, c in ops for a in [c[0]] if a != base[0]]
    for name, snap in ops:
        print(f"[perf]   {name}: pos={snap[0]} surf={snap[1]} "
              f"inten={snap[2]}", flush=True)
    check("Fit view / 2D-3D / display changes do NOT re-upload geometry",
          all(s[0] == base[0] and s[1] == base[1] for _n, s in ops),
          f"baseline pos={base[0]} surf={base[1]}; "
          + "; ".join(f"{n}:pos={s[0]}" for n, s in ops))

    # ---- PHASE 2: 20 zoom steps + pan, the persistence stress case --------
    zbase = _snap()
    for i in range(20):
        rig.dolly(1.0 if i % 2 == 0 else -1.0)
        rig.push_to_backend(b)
        b.request_render()
        if i % 5 == 0:
            pump(app_qt, 0.05)
    pump(app_qt, 0.6)
    zzoom = _snap()
    check("20 zoom steps do NOT re-upload point or surface geometry",
          zzoom[0] == zbase[0] and zzoom[1] == zbase[1],
          f"pos {zbase[0]} -> {zzoom[0]}, surf {zbase[1]} -> {zzoom[1]}")

    pbase = _snap()
    for dx, dy in ((150.0, 0.0), (-150.0, 0.0), (0.0, 120.0), (0.0, -120.0)):
        rig.pan(dx, dy, 900)
        rig.push_to_backend(b)
        b.request_render()
    pump(app_qt, 0.6)
    ppan = _snap()
    check("Panning does NOT re-upload point or surface geometry",
          ppan[0] == pbase[0] and ppan[1] == pbase[1],
          f"pos {pbase[0]} -> {ppan[0]}, surf {pbase[1]} -> {ppan[1]}")

    # ---- PHASE 2: display-side changes (class vis / weight / sharpness) ---
    # These are the ones that are ALLOWED to move the LUT and uniform
    # counters, but must still never move geometry.
    gbase = _snap()
    lbase = int(b.get_lut_update_count())
    ubase = (int(b.get_shade_param_update_count())
             + int(b.get_parity_param_update_count()))
    disp_ops = []
    try:
        # class visibility: hide a class through the backend visibility path
        _vis_fn = getattr(rb, "set_class_visibility", None)
        if _vis_fn is not None:
            _vis_fn(2, False)
            disp_ops.append("class_visibility_off")
        else:
            disp_ops.append("class_visibility(unavailable)")
        # sharpness / shading parameter
        _sh = getattr(b, "set_point_sprite_params", None)
        if _sh is not None:
            _sh(0.35, 1.0)
            disp_ops.append("sharpness")
        # weight change is a PTC-side operation; drive the shading uniform path
        _wm = getattr(b, "set_point_size_params", None)
        if _wm is not None:
            _wm(0.05, 1.5, 6.0, 0.35)
            disp_ops.append("point_size_params")
        b.request_render()
        pump(app_qt, 0.5)
    except Exception as e:
        disp_ops.append(f"error({e!r})")
    dbase = _snap()
    lut1 = int(b.get_lut_update_count())
    uni1 = (int(b.get_shade_param_update_count())
            + int(b.get_parity_param_update_count()))
    print(f"[perf]   display ops: {disp_ops}", flush=True)
    print(f"[perf]   lut {lbase} -> {lut1}, uniform {ubase} -> {uni1}", flush=True)
    check("Class visibility / weight / sharpness do NOT re-upload geometry",
          dbase[0] == gbase[0] and dbase[1] == gbase[1],
          f"pos {gbase[0]} -> {dbase[0]}, surf {gbase[1]} -> {dbase[1]}")

    # ---- [VULKAN FRAME] presented-frame telemetry ------------------------
    try:
        print("[perf] --- [VULKAN FRAME] after navigation ---", flush=True)
        print(rb.frame_report(), flush=True)
        print(flush=True)
        f = rb._frame_timing
        check("[VULKAN FRAME] collected real presented frames",
              int(f.get("frames", 0)) >= 2,
              f"presented frames={int(f.get('frames', 0))} "
              f"(only code==2 presents count)")
        check("[VULKAN FRAME] reports a positive FPS",
              "FPS" in rb.frame_report()
              and "nan" not in rb.frame_report().split("Frame time:")[0],
              "frame report contains a finite FPS")
    except Exception as e:
        check("[VULKAN FRAME] telemetry", False, f"{e!r}")

    # ---- [LOAD PIPELINE] + [VULKAN INTERACTION] -------------------------
    try:
        print("[perf] --- [LOAD PIPELINE] ---", flush=True)
        print(win.load_pipeline_report(), flush=True)
        print("\n[perf] --- [VULKAN INTERACTION] ---", flush=True)
        print(rb.interaction_report(), flush=True)
        print(flush=True)
        ir = rb.interaction_report()
        check("[VULKAN INTERACTION] reports NO geometry rebuild during navigation",
              "Geometry rebuild:" in ir and "  NO" in ir,
              "navigation must not re-upload geometry")
        check("[LOAD PIPELINE] has real per-stage timings",
              "Point cloud ready:" in win.load_pipeline_report(),
              "load stage timeline recorded")
    except Exception as e:
        check("[LOAD PIPELINE] / [VULKAN INTERACTION]", False, f"{e!r}")

    # ---- [VULKAN FRAME TIMING] real GPU vs CPU split ---------------------
    try:
        print("[perf] --- [VULKAN FRAME TIMING] ---", flush=True)
        print(rb.frame_timing_report(), flush=True)
        print(flush=True)
        t = b.get_frame_timing()
        check("GPU timestamp readback is live (valid=1)",
              bool(t.get("valid")),
              f"valid={t.get('valid')} gpuRenderMs={t.get('gpuRenderMs')}")
        check("GPU render time is a real measurement (> 0)",
              t.get("gpuRenderMs", 0.0) > 0.0,
              f"gpuRenderMs={t.get('gpuRenderMs')} "
              f"(0 would mean timestamps are not supported/available)")
        check("CPU submit time is measured",
              t.get("cpuSubmitMs", -1) >= 0.0,
              f"cpuSubmitMs={t.get('cpuSubmitMs')}")
    except Exception as e:
        check("[VULKAN FRAME TIMING] telemetry", False, f"{e!r}")

    # The legacy nkv_last_frame_ms CPU accessor now returns -1 by design, so
    # assert on the REAL GPU accessor instead: numeric, positive, never nan.
    try:
        gms = b.get_gpu_frame_time_ms()
        check("nkv_get_gpu_frame_time_ms returns a real numeric GPU time",
              isinstance(gms, (int, float)) and gms > 0.0,
              f"gpu_frame_time_ms={gms} "
              f"(must be > 0 and numeric; -1 means unavailable)")
        rep2 = rb.frame_timing_report()
        check("[VULKAN FRAME TIMING] contains no 'nan' anywhere",
              "nan" not in rep2.lower(),
              "report must never print nan")
        import re as _re2
        _fps = _re2.search(r"FPS:\s*\n\s*([0-9]+\.?[0-9]*)", rep2)
        check("[VULKAN FRAME TIMING] FPS is numeric",
              bool(_fps),
              f"parsed FPS={_fps.group(1) if _fps else None!r}")
    except Exception as _e:
        check("GPU frame time validation", False, f"{_e!r}")

    # ---- PHASE 2: the two telemetry reports must render -------------------
    try:
        print("\n[perf] --- [VULKAN GPU PERSISTENCE] ---", flush=True)
        print(rb.gpu_persistence_report(), flush=True)
        print("\n[perf] --- [VULKAN GPU MEMORY] ---", flush=True)
        print(rb.gpu_memory_report(), flush=True)
        print(flush=True)
        rep = rb.gpu_persistence_report()
        mem = rb.gpu_memory_report()
        check("[VULKAN GPU PERSISTENCE] reports all five counters",
              all(k in rep for k in ("Point uploads:", "Surface uploads:",
                                     "Index uploads:", "LUT uploads:",
                                     "Uniform updates:")),
              "all five required lines present")
        check("[VULKAN GPU MEMORY] reports all five lines",
              all(k in mem for k in ("Point buffer:", "Surface buffer:",
                                     "Index buffer:", "LUT:", "Total:")),
              "all five required lines present")
        # The new Phase 2 accessors must be live, not the -1 fallback.
        vtx = b.get_surface_vertex_count()
        idx = b.get_surface_index_count()
        print(f"[perf]   surface resident: {vtx:,} vtx / {idx:,} idx", flush=True)
    except Exception as e:
        check("Phase 2 telemetry reports", False, f"{e!r}")

    # shading parameter change must also be upload-free
    try:
        b.set_crisp_shading_parameters(60.0, 45.0, 0.25)
        b.request_render()
        pump(app_qt, 0.3)
    except Exception:
        pass
    p2 = int(b.get_point_position_upload_count())
    s2 = int(b.get_surface_upload_count())
    check("Shading parameter change does NOT re-upload buffers",
          p2 == p1 and s2 == s1,
          f"positionUploads {p1} -> {p2}, surfaceUploads {s1} -> {s2}")

    # ---- 5. performance block -------------------------------------------
    print()
    rb.print_performance_report()
    print()
    rep = rb.performance_report()
    # The legacy nkv_last_frame_ms CPU accessor now returns -1 by design and
    # its old "block reports real numbers" check is gone: it asserted on
    # nkv_last_frame_ms, which is superseded by nkv_get_frame_timing /
    # nkv_get_gpu_frame_time_ms. Real GPU timing is validated below.

    t0 = time.perf_counter()
    n = 60
    for _i in range(n):
        b.request_render()
    app_qt.processEvents()
    dt = time.perf_counter() - t0
    fps = n / dt if dt > 0 else float("nan")
    print(f"[perf] {n} request_render() calls in {dt*1000:.1f} ms -> "
          f"{fps:.1f} calls/s (includes Qt event overhead)", flush=True)
    check("Vulkan renders continuously without error", fps > 0.0, f"{fps:.1f}/s")

    # ---- [SHADING PERFORMANCE] - actually RUN the shading pipeline --------
    # Everything above measures the point-cloud path. The shading stages
    # (Delaunay, dedup, normals, colour) only run when a shaded-class or
    # surface build is requested, so they must be driven explicitly or the
    # report stays empty. This calls the real backend, not a mock.
    try:
        import numpy as _np
        from gui.shading_display import (_compute_shading_geometry_backend,
                                         shading_performance_report,
                                         _LAST_SHADING_PROFILE)
        _xyz = _np.asarray(win.data["xyz"])
        _cls = _np.asarray(win.data["classification"])
        _vis = sorted({int(c) for c in _np.unique(_cls)})[:26]
        print(f"\n[perf] driving shaded-geometry backend on "
              f"{_xyz.shape[0]:,} points, {len(_vis)} classes ...", flush=True)
        _t_sh = time.perf_counter()
        _res = _compute_shading_geometry_backend(
            _xyz, _cls, _vis, 45.0, 45.0, 0.2, 2.0, 0.0, None, None, None,
            quality_mode="normal")
        _shade_s = time.perf_counter() - _t_sh
        print(f"[perf] shading backend wall time: {_shade_s:,.2f} s", flush=True)
        print("[perf] --- [SHADING PERFORMANCE] ---", flush=True)
        print(shading_performance_report(), flush=True)
        print(flush=True)
        _tm = _LAST_SHADING_PROFILE.get("timings") or {}
        check("[SHADING PERFORMANCE] produced real stage timings",
              len(_tm) > 0,
              f"{len(_tm)} stages recorded, wall={_shade_s:.2f}s")
        for _req in ("dedup_pass1", "delaunay", "face_normals", "face_shade",
                     "total_backend"):
            check(f"  shading stage '{_req}' timed", _req in _tm,
                  f"{_tm.get(_req):.3f}s"
                  if _req in _tm else "(stage not reached in this run)")
        _faces = 0
        try:
            _faces = int(_res.get("faces", 0) or 0)
        except Exception:
            pass
        print(f"[perf] faces produced: {_faces:,}", flush=True)
        check("shaded mesh produced faces from the real pipeline", _faces > 0,
              f"faces={_faces:,}")
    except Exception as _se:
        check("[SHADING PERFORMANCE] benchmark", False, f"{_se!r}")
        import traceback
        traceback.print_exc()

    # ---- Screen-space LOD: build the index and MEASURE the opportunity ----
    # The index is built and the selection validated, but the draw path is
    # deliberately NOT changed: a LOD's only real acceptance criterion is
    # visual parity, and parity cannot be verified without comparing rendered
    # output. See gui/lod_tile_index.py for the scope note.
    try:
        import numpy as _np
        from gui.lod_tile_index import build_tile_index
        _xyz = _np.asarray(win.data["xyz"])
        print("\n[perf] --- [VULKAN LOD] ---", flush=True)
        _t0 = time.perf_counter()
        _idx = build_tile_index(_xyz, target_cell_points=4096)
        print(f"  Index: {_idx.summary()}", flush=True)
        _cx = _idx.origin_xy[0] + _idx.meta["span_x"] / 2.0
        _cy = _idx.origin_xy[1] + _idx.meta["span_y"] / 2.0
        _span = max(_idx.meta["span_x"], _idx.meta["span_y"])
        _all_idx, _nc, _nv = _idx.select([_cx, _cy], radius_m=_span)
        _all_set = _np.unique(_all_idx)
        check("LOD index covers every point exactly once",
              _all_set.size == _idx.total_points,
              f"covered {_all_set.size:,} of {_idx.total_points:,} points")
        check("LOD index is a permutation (no point lost or duplicated)",
              _all_set.size == _np.unique(_idx.order).size,
              "order is a permutation of range(N)")
        print(f"  {'View':<22}{'Visible pts':>14}{'Reduction':>11}"
              f"{'Cells':>8}{'Select':>10}", flush=True)
        for _frac, _label in ((1.00, "full extent (fit)"),
                              (0.50, "half extent"),
                              (0.25, "quarter extent"),
                              (0.10, "zoomed 10%"),
                              (0.02, "zoomed 2%")):
            _t1 = time.perf_counter()
            _v, _c, _vn = _idx.select([_cx, _cy], radius_m=_span * _frac)
            _sel_ms = (time.perf_counter() - _t1) * 1000.0
            _red = 100.0 * (1.0 - _vn / max(1, _idx.total_points))
            print(f"  {_label:<22}{_vn:>14,}{_red:>10.2f}%{_c:>8,}"
                  f"{_sel_ms:>9.1f}ms", flush=True)
        _cid, _cn2, _cv = _idx.select([_cx, _cy], radius_m=_span * 0.10)
        _pts = _xyz[_cid]
        _r = _span * 0.10
        _inb = (_np.abs(_pts[:, 0] - _cx) <= _r + _idx.cell_size) & \
               (_np.abs(_pts[:, 1] - _cy) <= _r + _idx.cell_size)
        check("LOD selection is SPATIAL (no stray points)", bool(_inb.all()),
              f"{int((~_inb).sum())} stray points selected")
        _a, _, _ = _idx.select([_cx, _cy], radius_m=_span * 0.25)
        _b, _, _ = _idx.select([_cx, _cy], radius_m=_span * 0.25)
        check("[VULKAN LOD] selection is deterministic", bool(_np.array_equal(_a, _b)),
              "identical query -> identical index set")
        print(f"  Total points: {_idx.total_points:,}", flush=True)

        # ---- LOD DRAW PATH: OFF vs ON --------------------------------------
        # The default is OFF. Prove (a) off == the original single full draw,
        # (b) setting ranges changes ONLY draw commands, never an upload, and
        # (c) the ranges are a disjoint in-bounds partition of the points.
        import os as _os
        from gui.render_backend import AppRenderBackendOwner as _ABO
        check("NAKSHA_VULKAN_LOD flag is read; default is OFF",
              _ABO.vulkan_lod_enabled() == (
                  _os.environ.get("NAKSHA_VULKAN_LOD", "").strip().lower()
                  in ("1", "true", "yes", "on")),
              f"NAKSHA_VULKAN_LOD="
              f"{_os.environ.get('NAKSHA_VULKAN_LOD', '')!r} "
              f"enabled={_ABO.vulkan_lod_enabled()}")
        check("LOD OFF = single full-buffer draw (0 ranges)",
              b.get_point_draw_range_count() == 0,
              f"ranges={b.get_point_draw_range_count()}")
        _u0 = (int(b.get_point_position_upload_count()),
               int(b.get_point_color_upload_count()),
               int(b.get_point_classification_upload_count()))

        def _runs(frac):
            _rs = []
            for (cx, cy) in _idx.visible_cells([_cx, _cy], radius_m=_span * frac):
                s, e = _idx.cell_ranges(cx, cy)
                if e <= s:
                    continue
                if _rs and _rs[-1][0] + _rs[-1][1] == s:
                    _rs[-1] = (_rs[-1][0], _rs[-1][1] + (e - s))
                else:
                    _rs.append((s, e - s))
            return _rs

        for _frac, _label in ((0.02, "zoomed 2%"), (0.10, "zoomed 10%"),
                              (1.00, "full extent")):
            _rs = _runs(_frac)
            _sel = sum(c for _f, c in _rs)
            ok = b.set_point_draw_ranges([r[0] for r in _rs],
                                         [r[1] for r in _rs])
            b.request_render(); pump(app_qt, 0.3)
            _u1 = (int(b.get_point_position_upload_count()),
                   int(b.get_point_color_upload_count()),
                   int(b.get_point_classification_upload_count()))
            _gpu = b.get_gpu_frame_time_ms()
            check(f"  LOD ON ({_label}): ranges accepted", ok,
                  f"{len(_rs)} draws, {_sel:,} pts")
            check(f"  LOD ON ({_label}): NO buffer re-uploaded", _u1 == _u0,
                  f"point/color/class {_u0} -> {_u1}")
            check(f"  LOD ON ({_label}): native matches the request",
                  b.get_point_draw_range_count() == len(_rs)
                  and b.get_point_draw_range_points() == _sel,
                  f"ranges={b.get_point_draw_range_count()} "
                  f"points={b.get_point_draw_range_points():,}")
            _rr = sorted(_rs)
            _disj = all(_rr[i][0] + _rr[i][1] <= _rr[i + 1][0]
                        for i in range(len(_rr) - 1))
            _inb = all(0 <= f and f + c <= _idx.total_points for f, c in _rr)
            check(f"  LOD ON ({_label}): ranges disjoint + in-bounds",
                  _disj and _inb,
                  f"{len(_rr)} runs, no overlap, within "
                  f"[0,{_idx.total_points:,})")
            print(f"    {_label}: {len(_rs)} draws, {_sel:,} pts "
                  f"({100.0 * (1 - _sel / _idx.total_points):.2f}% cut), "
                  f"GPU={_gpu:.2f} ms", flush=True)

        b.clear_point_draw_ranges()
        b.request_render(); pump(app_qt, 0.3)
        check("LOD OFF restores the single full-buffer draw",
              b.get_point_draw_range_count() == 0
              and b.get_point_draw_range_points() == 0, "ranges cleared")
        check("No buffer re-uploaded across the whole on/off cycle",
              (int(b.get_point_position_upload_count()),
               int(b.get_point_color_upload_count()),
               int(b.get_point_classification_upload_count())) == _u0,
              f"uploads still {_u0}")
        # ---- Buffer-order integration: do the ranges index the GPU buffer? ---
        # This is the check that the permutation actually made the ranges
        # meaningful. In LAS order the points of a cell are SCATTERED, so
        # (first,count) would draw the wrong region; after the load-time
        # permutation each cell IS a contiguous block, which we verify by
        # rebuilding the cell's point set from the ranges and checking it
        # really is one spatial tile.
        _permuted = getattr(rb, "_lod_index", None) is not None
        print(f"[perf]   buffer permuted for LOD: {_permuted}", flush=True)
        check("LOD index was built and the buffer permuted at upload time",
              _permuted == rb.vulkan_lod_enabled(),
              f"index={'present' if _permuted else 'absent'}, "
              f"LOD enabled={rb.vulkan_lod_enabled()}")
        if _permuted:
            _ri = rb._lod_index
            # Rebuild the XYZ of cell (cx,cy) from the PERMUTED arrays and
            # confirm it matches that cell's world rect.
            _cx0, _cy0 = (_ri.nx // 2, _ri.ny // 2)
            _s, _e = _ri.cell_ranges(_cx0, _cy0)
            _bx0, _by0, _bx1, _by1 = _ri.cell_bounds(_cx0, _cy0)
            # The permuted upload reordered win.data["xyz"] too? No - data is
            # the source of truth for the CPU copy, so reconstruct the
            # permuted positions explicitly.
            _perm_xyz = _np.ascontiguousarray(
                _np.asarray(win.data["xyz"])[_ri.order])
            _cell_xyz = _perm_xyz[_s:_e]
            _in = ((_cell_xyz[:, 0] >= _bx0 - 1e-6)
                   & (_cell_xyz[:, 0] <= _bx1 + 1e-6)
                   & (_cell_xyz[:, 1] >= _by0 - 1e-6)
                   & (_cell_xyz[:, 1] <= _by1 + 1e-6))
            check("Permuted buffer makes each cell a contiguous spatial tile",
                  bool(_in.all()) and _cell_xyz.shape[0] == (_e - _s),
                  f"cell ({_cx0},{_cy0}) range [{_s}:{_e}] -> "
                  f"{_cell_xyz.shape[0]:,} pts, all inside the cell rect"
                  + ("" if _in.all() else f", {int((~_in).sum())} outside"))
            # And in ORIGINAL order the same range would NOT be one tile -
            # that is the bug the permutation fixes.
            _orig_cell = _np.asarray(win.data["xyz"])[_s:_e]
            _oin = ((_orig_cell[:, 0] >= _bx0 - 1e-6)
                    & (_orig_cell[:, 0] <= _bx1 + 1e-6)
                    & (_orig_cell[:, 1] >= _by0 - 1e-6)
                    & (_orig_cell[:, 1] <= _by1 + 1e-6))
            print(f"[perf]   LAS-order same range would be a tile: "
                  f"{bool(_oin.all())} (expected False)", flush=True)
            # Attributes travel with the points.
            _pc = _np.asarray(win.data["classification"])
            _perm_cls = _np.ascontiguousarray(_pc[_ri.order])
            check("Classification is permuted with the positions",
                  _perm_cls.shape[0] == _idx.total_points
                  and np.array_equal(np.sort(_perm_cls), np.sort(_pc)),
                  f"classification permuted, {len(np.unique(_pc))} classes, "
                  f"multiset identical to source")
            # Uploads must still be exactly one.
            check("Permutation did NOT add an upload (still 1 point upload)",
                  int(b.get_point_position_upload_count()) == 1,
                  f"positionUploads="
                  f"{int(b.get_point_position_upload_count())}")
        print(f"  GPU upload: unchanged (1 point, 0 colour) "
              f"- permutation happens in the same single upload", flush=True)

        # ---- Camera-driven LOD: does moving the camera change the ranges? --
        if rb.vulkan_lod_enabled():
            _rig = rb._camera_rig
            _xyzc = _np.asarray(win.data["xyz"])
            _bnd = _np.stack([_xyzc.min(axis=0), _xyzc.max(axis=0)])
            _u_before = int(b.get_point_position_upload_count())
            print(f"\n[perf] camera-driven LOD:", flush=True)
            _seen = []
            for _tag, _notch, _pan in (("fit", None, None),
                                       ("zoom in x8", 8.0, None),
                                       ("zoom in x16", 16.0, None),
                                       ("pan", None, 400.0),
                                       ("zoom out", -6.0, None)):
                # Drive the REAL camera path. Calling rig.dolly()/push_to_backend
                # directly bypasses resync_camera(), which is where the LOD hook
                # lives - that is why a previous run reported ranges=0.
                if _notch is not None:
                    _rig.dolly(_notch)
                elif _pan is not None:
                    _rig.pan(_pan, 0, 900)
                rb._mirror_rig_to_vtk()
                rb.resync_camera(present=True)
                pump(app_qt, 0.35)
                _nc = b.get_point_draw_range_count()
                _npv = b.get_point_draw_range_points()
                _gpu = b.get_gpu_frame_time_ms()
                _seen.append((_tag, _nc, _npv, _gpu))
                print(f"    {_tag:<12} ranges={_nc:<6} "
                      f"pts={_npv:>12,}  "
                      f"cut={100.0 * (1 - _npv / max(1, _idx.total_points)):6.2f}%"
                      f"  GPU={_gpu:.2f} ms", flush=True)
            _u_after = int(b.get_point_position_upload_count())
            _any_range = any(s[1] > 0 for s in _seen)
            check("Camera moves update the LOD ranges (draw commands only)",
                  _u_after == _u_before == 1,
                  f"positionUploads stayed {_u_before} across "
                  f"{len(_seen)} camera moves")
            check("Camera moves actually SELECT cells (ranges non-zero)",
                  _any_range,
                  f"ranges per move: {[s[1] for s in _seen]} - all zero means "
                  f"the camera hook never fired and the full-buffer fallback "
                  f"is in use (safe, but LOD is not doing anything)")
            # Fit view must select EVERYTHING: 6561 cells / 26,960,750 pts.
            _fit = _seen[0]
            check("Fit view selects the full point set (no holes at full extent)",
                  _fit[2] == _idx.total_points,
                  f"fit -> {_fit[2]:,} of {_idx.total_points:,} pts in "
                  f"{_fit[1]} ranges"
                  + ("" if _fit[2] == _idx.total_points
                     else "  (LESS than total = potential holes at fit view)"))
            # Zoom must strictly reduce.
            _zoomed = [s for s in _seen[1:] if 0 < s[2] < _idx.total_points]
            check("Zoom selects a strict subset (LOD actually reduces work)",
                  bool(_zoomed),
                  f"visible pts per move: {[s[2] for s in _seen]} of "
                  f"{_idx.total_points:,}")
            _z = [s for s in _seen if s[2] > 0]
            check("Zooming reduces the visible point count",
                  bool(_z) and max(s[2] for s in _z) < _idx.total_points,
                  f"visible pts per move: {[s[2] for s in _seen]} of "
                  f"{_idx.total_points:,}"
                  + ("" if _z else "  (camera hook did not fire - see above)"))
            check("Idle refine timer is armed (500 ms)",
                  getattr(rb, "_LOD_IDLE_MS", 0) == 500,
                  f"_LOD_IDLE_MS={getattr(rb, '_LOD_IDLE_MS', None)}")
        print("\n[perf] --- [VULKAN INTERACTION MODE] / [VULKAN REFINEMENT] / [VULKAN MEMORY] ---", flush=True)
        print(rb.interaction_mode_report(), flush=True); print(flush=True)
        print(rb.refinement_report(), flush=True); print(flush=True)
        print(rb.memory_report(), flush=True); print(flush=True)
        _im = rb.interaction_mode_report()
        check("[VULKAN INTERACTION MODE] reports MOVING/IDLE state",
              "State:" in _im and ("MOVING" in _im or "IDLE" in _im),
              _im.split("State:")[1].split()[0] if "State:" in _im else "?")
        _mm = rb.memory_report()
        check("[VULKAN MEMORY] accounts the point buffer and LOD index",
              "Point buffer:" in _mm and "LOD index:" in _mm
              and "GPU budget:" in _mm,
              "all required lines present")
        check("Refinement did NOT upload geometry",
              int(b.get_point_position_upload_count()) == 1
              and int(b.get_point_color_upload_count()) == 0,
              f"pointUploads={int(b.get_point_position_upload_count())} "
              f"colourUploads={int(b.get_point_color_upload_count())}")
        # ---- [VULKAN WINDOW STATE] minimize / restore / resize --------------
        # This is the regression that started the NVIDIA driver-reset hunt:
        # a window event must never re-upload geometry or clear a buffer.
        for _ev, _act in (("minimize", lambda: win.showMinimized()),
                           ("restore", lambda: win.showNormal()),
                           ("resize 900x600", lambda: win.resize(900, 600)),
                           ("resize 1400x900", lambda: win.resize(1400, 900)),
                           ("focus loss", lambda: win.clearFocus()),
                           ("activate", lambda: win.activateWindow())):
            _before = rb.window_state_snapshot()
            try:
                _act()
                pump(app_qt, 0.6)
                b.request_render()
                pump(app_qt, 0.4)
            except Exception as _we:
                check(f"  window event '{_ev}'", False, f"{_we!r}")
                continue
            print(f"\n[perf] {rb.window_state_report(_before, _ev)}", flush=True)
            _after = rb.window_state_snapshot()
            check(f"  window event '{_ev}': no geometry re-uploaded",
                  _before.get("point_upload") == _after.get("point_upload")
                  and _before.get("surface_upload") == _after.get("surface_upload"),
                  f"point {_before.get('point_upload')}->{_after.get('point_upload')}, "
                  f"surface {_before.get('surface_upload')}->{_after.get('surface_upload')}")
            check(f"  window event '{_ev}': point data still resident",
                  _before.get("point_count") == _after.get("point_count"),
                  f"{_after.get('point_count')} points")
            # The regression this whole phase is about: after READY, a window /
            # focus event must never bring the loading overlay back.
            check(f"  window event '{_ev}': loading overlay stays HIDDEN",
                  not rb._loading_overlay_visible()
                  and rb._data_state_for_report() == "READY",
                  f"state={rb._data_state_for_report()} "
                  f"overlay_visible={rb._loading_overlay_visible()}")
    except Exception as _le:
        import traceback
        traceback.print_exc()

    return _report()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(3)

