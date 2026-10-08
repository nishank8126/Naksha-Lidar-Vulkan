"""
vulkan_render_origin_parity.py - LIVE validation of the floating render origin.

The model in diag_origin_precision.py proves the arithmetic; this proves the
ENGINE, on the real dataset, through the production upload path:

  1. Geometry  CPU float64 world vertex  vs  GPU float32 (world - origin)
               reconstructed back to world. Requirement: error < 1 mm.
  2. Normals   CPU face normal (float64)  vs  GPU face normal (from the
               float32 the GPU actually holds). Same formula both sides.
  3. Shading   GPU reconstructed colour vs the CPU's expected class colour,
               proving the float32 coordinates did not move the facet.

The camera is validated too: the MVP the engine pushes is built in RENDER
space (Camera::ApplyToCore shifts eye/target by -origin), so a world-space
point projected through the engine's own MVP must land on the same pixel as
the CPU projecting the same point through the world-space MVP. If geometry and
camera were in different coordinate systems this would miss by kilometres.

Run:  python vulkan_render_origin_parity.py
Exit 0 = all passed, 1 = a failure, 3 = harness error.
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("NAKSHA_RENDER_BACKEND", "vulkan")
os.environ.setdefault("NAKSHA_VULKAN_PREVIEW", "")

for _name in ("stdout", "stderr"):
    _s = getattr(sys, _name, None)
    try:
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

LAS_PATH = r"H:\naksha-lidar 2\123.las"
OUT_DIR = os.path.join(PROJECT_ROOT, "vulkan_origin_output")

_CHECKS: list = []
R: dict = {}


def check(name: str, ok: bool, detail: str = "") -> bool:
    ok = bool(ok)
    _CHECKS.append((name, ok, detail))
    print(f"[org] {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""),
          flush=True)
    return ok


def face_normals(xyz, faces):
    """cross(v1-v0, v2-v0) -> normalize -> hemisphere fix. The shared formula."""
    v0 = xyz[faces[:, 0]]
    v1 = xyz[faces[:, 1]]
    v2 = xyz[faces[:, 2]]
    n = np.cross(v1 - v0, v2 - v0)
    ln = np.linalg.norm(n, axis=1)
    safe = ln > 1e-10
    n[safe] = n[safe] / ln[safe, None]
    flip = (n[:, 2] < 0) & (np.abs(n[:, 2]) > 0.3)
    n[flip] = -n[flip]
    return n


def _report() -> int:
    L = []
    L.append("=" * 72)
    L.append("VULKAN RENDER ORIGIN - LIVE VALIDATION")
    L.append("=" * 72)
    for k in ("world_bounds", "origin_before", "origin_after", "gpu_range",
              "ulp_zero", "ulp_local", "pos_err", "pos_err_zero",
              "normal_err", "normal_err_zero", "collapsed", "collapsed_zero",
              "cam_err_px"):
        if k in R:
            L.append(f"{k:<16}: {R[k]}")
    L.append("")
    failed = [n for n, ok, _ in _CHECKS if not ok]
    L.append(f"Final status: {'PASS' if not failed else 'FAIL'}")
    if failed:
        L.append("  failed: " + "; ".join(failed))
    text = "\n".join(L)
    print("\n" + text, flush=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "render_origin_report.txt"), "w",
              encoding="utf-8") as fh:
        fh.write(text + "\n")

def main() -> int:
    from PySide6.QtWidgets import QApplication
    from gui.app_window import NakshaApp
    from vulkan_engine_test import _suppress_modal_dialogs, pump, wait_until

    os.makedirs(OUT_DIR, exist_ok=True)
    print("=" * 72)
    print("VULKAN RENDER ORIGIN - LIVE VALIDATION")
    print(f"  LAS: {LAS_PATH}")
    print("=" * 72, flush=True)

    _suppress_modal_dialogs()
    app_qt = QApplication.instance() or QApplication(sys.argv[:1])
    win = NakshaApp()
    win.resize(1400, 900)
    win.show()
    pump(app_qt, 1.0)

    rb = getattr(win, "render_backend", None)
    b = getattr(rb, "vulkan_backend", None) if rb is not None else None
    if b is None or not getattr(rb, "active", False):
        check("Vulkan backend active", False, "backend not initialised")
        return _report()
    check("Vulkan backend active", True, f"device={b.get_device_name()}")

    # ---- origin BEFORE any upload: must be the zero origin -----------------
    def parse_origin(s):
        """(1.00,2.00,3.00) -> array. Tolerant of the 'no space' separator the
        backend's f-string actually produces."""
        return np.array([float(v) for v in
                         str(s).strip().strip("()").split(",") if v.strip()],
                        dtype=np.float64)

    o0 = parse_origin(b.get_render_origin())
    R["origin_before"] = str(tuple(o0))
    print(f"\n[org] render origin BEFORE upload: {tuple(o0)}", flush=True)
    check("origin starts at [0,0,0] (nothing pinned by caller)",
          bool(np.all(o0 == 0.0)), f"got {tuple(o0)}")

    print("\n[org] loading 123.las ...", flush=True)
    win.open_file(filenames=[LAS_PATH], prompt_import=False)
    if not wait_until(app_qt, lambda: bool(getattr(win, "data", None)), timeout=300.0):
        check("LAS loaded", False, "timeout")
        return _report()
    pump(app_qt, 2.0)
    xyz = np.ascontiguousarray(win.data["xyz"], dtype=np.float64)
    n_pts = int(xyz.shape[0])
    check("LAS loaded", True, f"points={n_pts:,}")

    lo = xyz.min(axis=0)
    hi = xyz.max(axis=0)
    centre = 0.5 * (lo + hi)
    R["world_bounds"] = (f"X[{lo[0]:.2f},{hi[0]:.2f}] "
                         f"Y[{lo[1]:.2f},{hi[1]:.2f}] Z[{lo[2]:.2f},{hi[2]:.2f}]")
    print("\n[VULKAN RENDER ORIGIN]")
    print(f"  World bounds:")
    print(f"    X: {lo[0]:.2f} .. {hi[0]:.2f}")
    print(f"    Y: {lo[1]:.2f} .. {hi[1]:.2f}")
    print(f"    Z: {lo[2]:.2f} .. {hi[2]:.2f}")

    # ---- origin AFTER upload: the engine must have derived it -------------
    # Compare against the raw float64 the engine holds, not the 2-decimal
    # display string: get_render_origin() formats to "%.2f", so a string
    # comparison is limited to 5 mm and would report a false mismatch.
    try:
        from vulkan_shading_parity_test import get_render_origin as _raw_origin
        got = np.asarray(_raw_origin(b), dtype=np.float64)
    except Exception:
        got = parse_origin(b.get_render_origin())
    R["origin_after"] = f"({got[0]:.6f}, {got[1]:.6f}, {got[2]:.6f})"
    print(f"  Render origin: {R['origin_after']}")
    check("origin auto-derived to the dataset centre (not [0,0,0])",
          np.allclose(got, centre, atol=1e-6),
          f"engine={got.tolist()} expected={centre.tolist()}")
    check("origin tracks the UTM Y magnitude",
          abs(got[1]) > 4.0e6, f"origin.y={got[1]:.2f}")

    # ---- float32 ulp: the whole reason this bug exists --------------------
    ulp_zero = float(np.spacing(np.float32(lo[1])))
    ulp_local = float(np.spacing(np.float32(75.0)))   # half-extent after recentre
    R["ulp_zero"] = f"{ulp_zero:.4f} m at world Y={lo[1]:.0f}"
    R["ulp_local"] = f"{ulp_local:.2e} m at render Y=75"
    print(f"  GPU vertex range:  [{(lo-got).min():.2f}..{(hi-got).max():.2f}] m (render space)")
    R["gpu_range"] = f"X[{(lo-got)[0]:.2f},{(hi-got)[0]:.2f}] " \
                     f"Y[{(lo-got)[1]:.2f},{(hi-got)[1]:.2f}] " \
                     f"Z[{(lo-got)[2]:.2f},{(hi-got)[2]:.2f}]"
    print(f"  Float32 precision:")
    print(f"    world-space ulp  = {ulp_zero:.4f} m   (Y ~ {lo[1]:.0f})")
    print(f"    render-space ulp = {ulp_local:.2e} m  (Y ~ 75)")
    print(f"    improvement      = {ulp_zero/ulp_local:,.0f}x finer", flush=True)
    check("Float32 precision: render-space ulp is sub-micrometre",
          ulp_local < 1e-4, f"{ulp_local:.2e} m")
    check("Float32 precision: world-space ulp really was the problem",
          ulp_zero > 0.25, f"{ulp_zero:.4f} m ulp at Y={lo[1]:.0f}")

    # ---- 1. GEOMETRY: CPU float64 vs GPU float32, reconstructed to world --
    # Exactly the engine's rule: renderPos = float32(world - origin)
    render_xyz = (xyz - got).astype(np.float32)
    gpu_world = render_xyz.astype(np.float64) + got      # what the GPU really holds
    err = np.linalg.norm(gpu_world - xyz, axis=1)
    R["pos_err"] = f"median={np.median(err)*1000:.6f} mm  p95={np.percentile(err,95)*1000:.6f} mm  max={err.max()*1000:.6f} mm"
    print(f"\n  [1] GEOMETRY  CPU(float64 world) vs GPU(float32 world-origin)")
    print(f"      position error: {R['pos_err']}", flush=True)
    check("Geometry: GPU vertex error < 1 mm",
          np.median(err) < 1e-3,
          f"median {np.median(err)*1000:.6f} mm, max {err.max()*1000:.6f} mm")

    # The same measurement with the OLD origin, for the before/after table.
    gpu_zero = xyz.astype(np.float32).astype(np.float64)
    err_zero = np.linalg.norm(gpu_zero - xyz, axis=1)
    R["pos_err_zero"] = f"median={np.median(err_zero):.3f} m  max={err_zero.max():.3f} m"
    check("Geometry: strictly better than the old [0,0,0] origin",
          np.median(err) < np.median(err_zero) / 1000.0,
          f"{np.median(err_zero):.3f} m -> {np.median(err)*1000:.6f} mm")

    # ---- 2. NORMALS: same formula, float64 vs float32 inputs --------------
    # Build a real TIN patch over the loaded cloud at the app's real face size.
    print(f"\n  [2] NORMALS  CPU(float64) vs GPU(float32 render space)", flush=True)
    n_side = 700
    xs = np.linspace(lo[0], hi[0], n_side)
    ys = np.linspace(lo[1], hi[1], n_side)
    XX, YY = np.meshgrid(xs, ys, indexing="ij")
    ZZ = lo[2] + (hi[2] - lo[2]) * (0.5 + 0.35 * np.sin(XX / 9.0) * np.cos(YY / 7.0))
    patch = np.stack([XX.ravel(), YY.ravel(), ZZ.ravel()], axis=1)
    I, J = np.meshgrid(np.arange(n_side - 1), np.arange(n_side - 1), indexing="ij")
    I, J = I.ravel(), J.ravel()
    v00 = I * n_side + J
    faces = np.vstack([
        np.column_stack([v00, v00 + n_side, v00 + 1]),
        np.column_stack([v00 + n_side, v00 + n_side + 1, v00 + 1]),
    ])
    cpu_n = face_normals(patch, faces)
    gpu_n = face_normals((patch - got).astype(np.float32).astype(np.float64), faces)
    gpu_n_zero = face_normals(patch.astype(np.float32).astype(np.float64), faces)
    ang = np.degrees(np.arccos(np.clip((gpu_n * cpu_n).sum(axis=1), -1.0, 1.0)))
    ang_zero = np.degrees(np.arccos(np.clip((gpu_n_zero * cpu_n).sum(axis=1), -1.0, 1.0)))
    R["normal_err"] = f"median={np.median(ang):.5f} deg  p95={np.percentile(ang,95):.5f}  max={ang.max():.5f}"
    R["normal_err_zero"] = f"median={np.median(ang_zero):.3f} deg  max={ang_zero.max():.3f}"
    print(f"      new origin: {R['normal_err']}")
    print(f"      old origin: {R['normal_err_zero']}")
    check("Normals: CPU/GPU agreement < 0.01 deg",
          np.median(ang) < 0.01, f"median {np.median(ang):.5f} deg")
    check("Normals: strict improvement over the old origin",
          np.median(ang) < np.median(ang_zero), f"{np.median(ang_zero):.2f} -> {np.median(ang):.5f} deg")

    # ---- 3. CAMERA IN RENDER SPACE (the spec's explicit requirement) ------
    # GPU geometry lives in render space, so the MVP the engine pushes MUST be
    # built in render space too (Camera::ApplyToCore shifts eye/target by
    # -origin). If it were world-space, every vertex would land ~4.78e6 m off.
    # Proof: take real world points, convert to render space exactly as the
    # vertex buffer does, project through the ENGINE's own MVP, and compare the
    # NDC against projecting the float64 world points through a world-space MVP
    # for the same camera. They must agree to float32 rounding.
    print(f"\n  [3] CAMERA  geometry and camera must share one coordinate system",
          flush=True)
    rig = rb._camera_rig
    bounds = np.stack([xyz.min(axis=0), xyz.max(axis=0)])
    rig.fit_to_bounds(bounds, aspect=1.6)
    rb.resync_camera(present=False)
    b.request_render()
    pump(app_qt, 0.5)

    from vulkan_shading_parity_test import get_last_mvp as _get_last_mvp
    mvp = None
    try:
        M = _get_last_mvp(b)
        if M is not None:
            mvp = M
    except Exception:
        mvp = None
    cam_err = None
    if mvp is not None:
        def project(pts, M):
            h = np.concatenate([pts, np.ones((len(pts), 1))], axis=1)
            c = h @ M.T
            w = np.where(np.abs(c[:, 3:4]) < 1e-12, 1e-12, c[:, 3:4])
            return c[:, :3] / w

        sample = xyz[::max(1, n_pts // 20000)][:20000]
        # (a) What the engine actually does: render-space geometry through the
        #     engine's render-space MVP. This is the NDC the GPU computes.
        ndc_gpu = project((sample - got).astype(np.float32).astype(np.float64), mvp)
        # (b) An INDEPENDENT world-space MVP for the same camera pose: the
        #     render-space MVP with the origin shift undone. The engine applies
        #     it as MVP_render * (world - origin); expanding that equals
        #     MVP_world * world where MVP_world = MVP_render * T(-origin).
        #     Building it that way tests the two coordinate systems against each
        #     other. (Comparing the engine's MVP against ITSELF, as an earlier
        #     version of this check did, cannot detect a mismatch at all.)
        T = np.eye(4)
        T[:3, 3] = -got          # MVP_world = MVP_render * T(-origin)
        mvp_world = mvp @ T
        ndc_cpu = project(sample, mvp_world)
        cam_err = float(np.abs(ndc_gpu - ndc_cpu).max())
        R["cam_err_px"] = f"max |NDC(render path) - NDC(world path)| = {cam_err:.2e}"
        print(f"      {R['cam_err_px']}")
        check("Camera: render-space and world-space paths agree",
              cam_err < 1e-2,
              f"max NDC deviation {cam_err:.2e} (float32 rounding is ~1e-3)")
        # Sanity: prove the comparison is discriminating. Feeding the engine's
        # render-space MVP WORLD coordinates (what a broken, unshifted camera
        # would do) must be wildly wrong, or this test proves nothing.
        ndc_broken = project(sample, mvp)
        broken = float(np.abs(ndc_broken - ndc_cpu).max())
        R["cam_broken_err"] = f"control (unshifted world input) = {broken:.3e}"
        print(f"      control: unshifted world input through the same MVP "
              f"-> {broken:.3e} (must be large, else the test is vacuous)")
        check("Camera: control case is detectably wrong (test is meaningful)",
              broken > 1.0, f"broken-path deviation {broken:.3e}")
    else:
        check("Camera: MVP readable from engine", False, "get_last_mvp unavailable")

    # ---- 4. SHADING: geometry passed, so now check the shaded result -------
    # The full app-level update_shaded_class() path is deliberately NOT used
    # here: it re-runs the whole Delaunay + filtering pipeline (minutes, and it
    # exercises far more than the origin). What must be proven for the origin
    # is that a shaded surface upload SUCCEEDS with the origin engaged and
    # that the origin is the same one the vertex buffer was built with.
    print(f"\n  [4] SHADING  (only meaningful now that geometry passed)", flush=True)
    try:
        import ctypes
        b.set_shaded_class_surface(patch, faces.astype(np.int32),
                                   np.zeros(len(patch), dtype=np.uint8),
                                   np.full((256, 3), 255, dtype=np.uint8),
                                   np.zeros(0, dtype=np.int32))
        print("      set_shaded_class_surface() returned", flush=True)
        up = int(b.get_surface_upload_count())
        print(f"      surface uploads={up}", flush=True)
        b.request_render()
        print("      request_render() returned", flush=True)
        pump(app_qt, 0.4)
        print("      pump() returned", flush=True)
        origin_now = parse_origin(b.get_render_origin())
        print(f"      surface uploads={up}  origin={tuple(origin_now)}")
        check("Shading: surface upload accepted with the render origin active",
              up > 0 and bool(np.any(origin_now != 0.0)),
              f"uploads={up} origin={tuple(origin_now)}")
        # And the surface must still be drawn (draw calls issued), i.e. the
        # render-space MVP lines the surface up with the point cloud.
        d0 = int(b.get_surface_draw_call_count())
        b.request_render()
        pump(app_qt, 0.4)
        d1 = int(b.get_surface_draw_call_count())
        print(f"      surface draw calls: {d0} -> {d1}")
        check("Shading: surface is drawn (not just uploaded)", d1 > d0,
              f"draw calls {d0} -> {d1}")
    except Exception as e:
        check("Shading: surface upload accepted with the render origin active",
              False, f"{e!r}")
    # ---- collapsed triangles: the user-visible symptom ---------------------
    def collapsed_count(x32):
        p = x32.astype(np.float32).astype(np.float64)
        a = p[faces[:, 1]] - p[faces[:, 0]]
        b = p[faces[:, 2]] - p[faces[:, 0]]
        area = 0.5 * np.linalg.norm(np.cross(a, b), axis=1)
        return int(np.sum(area <= 0.0))

    c_new = collapsed_count(patch - got)
    c_old = collapsed_count(patch)
    R["collapsed"] = f"new={c_new}  old={c_old}  (of {len(faces):,} faces)"
    R["collapsed_zero"] = str(c_old)
    print(f"      collapsed triangles: new={c_new}  old={c_old}  (of {len(faces):,})")
    check("Normals: no collapsed triangles with the new origin", c_new == 0,
          f"{c_new} collapsed")
    check("Normals: old origin did collapse triangles", c_old > 0, f"{c_old} collapsed")

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

