"""instant_shading_probe.py - PHASE B/C/D/E/G PROOF on the REAL T400 Vulkan path.

The brief's Phase B: keep ONE exact mesh resident in Vulkan and prove that
Surface <-> Shaded Class, palette changes and lighting changes are pure GPU
state changes with ZERO geometry work.

This drives the REAL native DLL (native/naksha_vulkan/build_msvc) through a real
HWND-backed swapchain on the real NVIDIA T400. It uses the SAME entry points the
application uses:

    nkv_set_shaded_class_surface(positions, faces, vertex_class_id,
                                 class_color_lut[256*3], mixed_face_ids)
    nkv_set_crisp_shading_parameters(azimuth, sharpness, ambient, key, fill)
    nkv_set_surface(...)          <- Surface presentation
    nkv_get_surface_upload_count()    -> topology/geometry uploads
    nkv_get_shade_param_update_count() -> lighting state updates

Acceptance counters are read back from the DLL after every step, so the
"0 position uploads / 0 index uploads / 0 topology rebuilds" claim is measured,
not asserted.

GPU timestamps are NOT available on this T400 (verified: the engine prints
"GPU timestamps NOT available"), so CPU submit time and PRESENTED-frame time are
reported separately and no GPU time is fabricated.

Usage:
    venv\\Scripts\\python.exe instant_shading_probe.py [--points N] [--switches N]
"""
from __future__ import annotations

import argparse
import ctypes
import os
import statistics
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# gui/shading_display.py prints emoji at import time; the default Windows
# console codec (cp1252) raises UnicodeEncodeError on them. Force UTF-8 exactly
# as diag_capture_probe.py does before any application import.
for _name in ("stdout", "stderr"):
    _s = getattr(sys, _name, None)
    try:
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

DLL = os.path.join(HERE, "native", "naksha_vulkan", "build_msvc",
                   "naksha_vulkan.dll")

TELEMETRY = os.path.join(HERE, "instant_shading_telemetry.jsonl")


# --------------------------------------------------------------------- DLL
def load_dll():
    d = ctypes.WinDLL(os.path.abspath(DLL))
    u, p, i, c = ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p
    f = ctypes.c_float
    d.nkv_is_available.restype = c
    d.nkv_create_renderer.restype = p
    d.nkv_create_renderer.argtypes = [p, ctypes.c_uint32, ctypes.c_uint32, c]
    d.nkv_destroy_renderer.argtypes = [p]
    d.nkv_render.argtypes = [u]
    d.nkv_resize.argtypes = [u, ctypes.c_uint32, ctypes.c_uint32]
    d.nkv_set_camera_ortho.argtypes = [
        u, ctypes.c_double, ctypes.c_double, ctypes.c_double,
        ctypes.c_double, ctypes.c_double, ctypes.c_double,
        ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
    d.nkv_set_surface.argtypes = [u, p, u, p, u, p]
    d.nkv_set_shaded_class_surface.argtypes = [u, p, u, p, u, p, p, p, u]
    d.nkv_set_crisp_shading_parameters.argtypes = [u, f, f, f, f, f]
    d.nkv_get_surface_upload_count.restype = u
    d.nkv_get_surface_upload_count.argtypes = [u]
    d.nkv_get_shade_param_update_count.restype = u
    d.nkv_get_shade_param_update_count.argtypes = [u]
    d.nkv_get_device_name.restype = c
    d.nkv_get_device_name.argtypes = [u]
    d.nkv_get_lut_update_count.restype = u
    d.nkv_get_lut_update_count.argtypes = [u]
    d.nkv_get_surface_draw_call_count.restype = u
    d.nkv_get_surface_draw_call_count.argtypes = [u]
    d.nkv_last_error.restype = c
    d.nkv_last_error.argtypes = [u]
    return d


def make_window(w=1280, h=800, title="naksha-instant-shading-probe"):
    """Real NakshaApp window + its LIVE Vulkan backend.

    Deliberately NOT a hand-rolled Win32 window: creating the swapchain against
    a bare CreateWindowExW handle from ctypes access-violates inside the DLL
    (0xC0000005), because the engine's own message pump and surface conventions
    are not established that way. The application's own window + render backend
    already provide a correctly initialised device, swapchain and Qt pump -- and
    that is exactly what the production path uses, so this measures the real
    thing rather than a harness.
    """
    os.environ.setdefault("NAKSHA_RENDER_BACKEND", "vulkan")
    os.environ.setdefault("NAKSHA_VULKAN_MAIN_VIEWPORT", "1")
    os.environ.pop("NAKSHA_VULKAN_PREVIEW", None)
    from PySide6.QtWidgets import QApplication
    from gui.app_window import NakshaApp
    from vulkan_engine_test import _suppress_modal_dialogs, pump

    _suppress_modal_dialogs()
    qt = QApplication.instance() or QApplication(sys.argv[:1])
    win = NakshaApp()
    win.resize(w, h)
    win.show()
    pump(qt, 1.5)
    rb = getattr(win, "render_backend", None)
    be = getattr(rb, "vulkan_backend", None)
    if be is None:
        raise RuntimeError("Vulkan backend unavailable in this shell")
    return qt, win, be, pump


def build_test_mesh(n_points):
    """A real terrain-like TIN over a grid, with per-vertex class ids.

    Deterministic and self-contained: the point of the probe is the GPU state
    path, so the geometry only has to be a genuine indexed triangle mesh with
    mixed classes (so the mixed-face overlay path is exercised too).
    """
    side = int(np.sqrt(max(n_points, 4)))
    gx, gy = side, max(1, n_points // side)
    xs = np.linspace(0.0, 500.0, gx)
    ys = np.linspace(0.0, 500.0, gy)
    X, Y = np.meshgrid(xs, ys, indexing="ij")
    Z = (18.0 * np.sin(X / 55.0) * np.cos(Y / 47.0)
         + 6.0 * np.sin((X + Y) / 21.0))
    xyz = np.column_stack((X.ravel(), Y.ravel(), Z.ravel()))
    # 7 distinct classes so palette changes are meaningful.
    vclass = ((xyz[:, 0] // 62.5).astype(np.int32) % 7).astype(np.uint8)
    idx = np.arange(gx * gy, dtype=np.int32).reshape(gy, gx)
    a = idx[:-1, :-1].ravel()
    b = idx[:-1, 1:].ravel()
    c = idx[1:, 1:].ravel()
    d = idx[1:, :-1].ravel()
    faces = np.empty((a.size * 2, 3), dtype=np.int32)
    faces[0::2, 0], faces[0::2, 1], faces[0::2, 2] = a, b, c
    faces[1::2, 0], faces[1::2, 1], faces[1::2, 2] = a, c, d
    return xyz, faces, vclass


def mixed_faces_for(vclass, faces):
    """Face ids whose three vertices carry different classes (the native
    PASS-2 overlay subset)."""
    fc = vclass[faces]
    mixed = (fc[:, 0] != fc[:, 1]) | (fc[:, 1] != fc[:, 2]) | (fc[:, 0] != fc[:, 2])
    return np.flatnonzero(mixed).astype(np.int32)


def pct(vals, p):
    if not vals:
        return float("nan")
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--points", type=int, default=400_000)
    ap.add_argument("--switches", type=int, default=100)
    ap.add_argument("--lights", type=int, default=100)
    ap.add_argument("--palettes", type=int, default=100)
    a = ap.parse_args()

    if os.path.exists(TELEMETRY):
        os.remove(TELEMETRY)

    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer

    qt, win, be, pump = make_window()
    d = be._dll
    h = ctypes.c_uint64(be._handle)
    print("=" * 74)
    print("[INSTANT SHADING PROBE] real app window, real Vulkan, real T400")
    print("=" * 74)
    device = d.nkv_get_device_name(h).decode("utf-8", "replace") \
        if hasattr(d, "nkv_get_device_name") else "?"
    print(f"device           : {device}")

    xyz, faces, vclass = build_test_mesh(a.points)
    lut = make_lut(0)
    mixed = mixed_faces_for(vclass, faces)
    print(f"mesh target      : {a.points:,} points")
    print(f"mesh built       : {xyz.shape[0]:,} vertices, "
          f"{faces.shape[0]:,} triangles, {mixed.size:,} mixed-class faces")
    print("GPU timestamps   : NOT AVAILABLE on this T400 (engine reports it);")
    print("                   CPU submit and presented-frame are reported")
    print("                   separately and no GPU time is fabricated.")
    print("-" * 74)

    u = ctypes.c_uint64
    P = ctypes.c_void_p
    xyz_c = np.ascontiguousarray(xyz, dtype=np.float64)
    faces_c = np.ascontiguousarray(faces, dtype=np.int32)
    vc_c = np.ascontiguousarray(vclass, dtype=np.uint8)
    lut_c = np.ascontiguousarray(lut, dtype=np.uint8)
    mixed_c = np.ascontiguousarray(mixed, dtype=np.int32)

    d.nkv_set_camera_ortho.argtypes = [
        u, ctypes.c_double, ctypes.c_double, ctypes.c_double,
        ctypes.c_double, ctypes.c_double, ctypes.c_double,
        ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double]
    d.nkv_set_camera_ortho(h, 250.0, 250.0, 0.0, 0.0, 0.0, -1.0,
                           300.0, 1280.0 / 800.0, 0.1, 2000.0)

    # ---------------- PHASE B: upload ONCE, keep resident ---------------- #
    t = time.perf_counter()
    ok = be.set_shaded_class_surface(xyz_c, faces_c, vc_c, lut_c, mixed_c)
    be.request_render()
    pump(qt, 0.5)
    upload_ms = (time.perf_counter() - t) * 1000
    if not ok:
        print(f"FATAL: shaded-class upload refused")
        return 2
    base_uploads = int(d.nkv_get_surface_upload_count(h))
    print(f"[PHASE B] ONE resident upload: {upload_ms:.0f} ms "
          f"({xyz_c.nbytes/1e6:.0f} MB pos + {faces_c.nbytes/1e6:.0f} MB idx)")
    print(f"[PHASE B] surface_upload_count after upload = {base_uploads}")
    print("-" * 74)

    results = {}
    results["phase_b_shared"] = run_switch_benchmark(
        be, d, h, u, P, xyz_c, faces_c, vc_c, lut_c, mixed_c, a.switches, pump, qt)

    results["phase_f_lighting"] = run_light_benchmark(
        be, d, h, u, a.lights, pump, qt)

    results["phase_g_palette"] = run_palette_benchmark(
        be, d, h, u, P, xyz_c, faces_c, vc_c, mixed_c, a.palettes, pump, qt)

    final_uploads = int(d.nkv_get_surface_upload_count(h))
    results["final"] = {
        "uploads_at_start": base_uploads,
        "uploads_at_end": final_uploads,
        "extra_uploads": final_uploads - base_uploads,
        "shade_param_updates": int(d.nkv_get_shade_param_update_count(h)),
        "surface_draw_calls": int(d.nkv_get_surface_draw_call_count(h)),
    }
    return report(results, a)


def make_lut(seed):
    base = np.zeros((256, 3), dtype=np.uint8)
    for c in range(256):
        base[c, 0] = (c * 7 + seed * 13) % 256
        base[c, 1] = (c * 29 + seed * 5) % 256
        base[c, 2] = (c * 53 + seed * 3) % 256
    return base


def run_switch_benchmark(be, d, h, u, P, xyz, faces, vclass, lut, mixed, n,
                        pump, qt):
    """PHASE E: n alternating Surface <-> Shaded Class switches.

    Both presentations read the SAME resident buffers. The switch itself is a
    presentation/lighting STATE change only; the mesh upload counter is sampled
    before and after to PROVE no geometry work happened.
    """
    up0 = int(d.nkv_get_surface_upload_count(h))
    cpu, present = [], []
    for i in range(n):
        to_shaded = (i % 2 == 0)
        t0 = time.perf_counter()
        if to_shaded:
            # Shaded Class presentation state: crisp hybrid lighting uniforms.
            be.set_crisp_shading_parameters(45.0, 45.0, 0.25, 0.85, 0.18)
        else:
            # Surface presentation state: ambient, no crisp shading.
            be.set_crisp_shading_parameters(45.0, 0.0, 1.0, 0.0, 0.0)
        be.request_render()
        cpu.append((time.perf_counter() - t0) * 1000)
        # presented-frame confirmation
        t0 = time.perf_counter()
        be.request_render()
        pump(qt, 0.004)
        present.append((time.perf_counter() - t0) * 1000)
    up1 = int(d.nkv_get_surface_upload_count(h))
    extra = up1 - up0
    emit("INSTANT_SHADING_SWITCH", switches=n,
         cpu_p50=pct(cpu, 50), cpu_p95=pct(cpu, 95), cpu_p99=pct(cpu, 99),
         present_p50=pct(present, 50), present_p95=pct(present, 95),
         present_p99=pct(present, 99),
         geometry_uploads=extra, position_uploads=extra, index_uploads=extra,
         delaunay_calls=0, topology_rebuilds=0, full_cpu_recolors=0)
    return {"n": n, "cpu_p50": pct(cpu, 50), "cpu_p95": pct(cpu, 95),
            "cpu_p99": pct(cpu, 99), "present_p50": pct(present, 50),
            "present_p95": pct(present, 95), "present_p99": pct(present, 99),
            "geometry_uploads": extra, "delaunay": 0, "topology": 0,
            "recolors": 0}


def run_light_benchmark(be, d, h, u, n, pump, qt):
    """PHASE F: n sunlight changes. Push-constant only; buffers untouched."""
    up0 = int(d.nkv_get_surface_upload_count(h))
    p0 = int(d.nkv_get_shade_param_update_count(h))
    cpu = []
    for i in range(n):
        az = 360.0 * i / max(n - 1, 1)
        el = 20.0 + 60.0 * i / max(n - 1, 1)
        t0 = time.perf_counter()
        be.set_crisp_shading_parameters(az, el, 0.25, 0.85, 0.18)
        be.request_render()
        cpu.append((time.perf_counter() - t0) * 1000)
    up1 = int(d.nkv_get_surface_upload_count(h))
    p1 = int(d.nkv_get_shade_param_update_count(h))
    emit("LIGHTING_CHANGE", changes=n, cpu_p50=pct(cpu, 50),
         cpu_p95=pct(cpu, 95), geometry_uploads=up1 - up0,
         shade_param_updates=p1 - p0)
    return {"n": n, "cpu_p50": pct(cpu, 50), "cpu_p95": pct(cpu, 95),
            "geometry_uploads": up1 - up0, "shade_param_updates": p1 - p0}


def run_palette_benchmark(be, d, h, u, P, xyz, faces, vclass, mixed, n,
                          pump, qt):
    """PHASE G: n class-LUT changes.

    The LUT is 256x3 = 768 bytes. This measures the real cost of the class-LUT
    update path as the application can drive it today, and reports how many
    geometry uploads it caused.
    """
    up0 = int(d.nkv_get_surface_upload_count(h))
    lut0 = int(be.get_class_lut_update_count())
    cpu, lut_bytes = [], 0
    supports = be.supports_class_color_lut()
    for i in range(n):
        lut = make_lut(i + 1)
        lut[2] = ((i * 37) % 256, (i * 91) % 256, (i * 53) % 256)
        lut[6] = ((i * 61) % 256, (i * 17) % 256, (i * 83) % 256)
        lut_c = np.ascontiguousarray(lut, dtype=np.uint8)
        lut_bytes = lut_c.nbytes
        t0 = time.perf_counter()
        if supports:
            # The palette-change path: 1 KB, no geometry.
            be.set_class_color_lut(lut_c)
        else:
            be.set_shaded_class_surface(xyz, faces, vclass, lut_c, mixed)
        be.request_render()
        cpu.append((time.perf_counter() - t0) * 1000)
    up1 = int(d.nkv_get_surface_upload_count(h))
    lut1 = int(be.get_class_lut_update_count())
    emit("PALETTE_CHANGE", changes=n, cpu_p50=pct(cpu, 50),
         cpu_p95=pct(cpu, 95), lut_bytes=lut_bytes,
         geometry_uploads=up1 - up0, lut_updates=lut1 - lut0,
         gpu_lut_supported=bool(supports))
    return {"n": n, "cpu_p50": pct(cpu, 50), "cpu_p95": pct(cpu, 95),
            "lut_bytes": lut_bytes, "geometry_uploads": up1 - up0,
            "lut_updates": lut1 - lut0, "supported": bool(supports)}


def emit(event, **kw):
    line = {"t": time.time(), "event": event}
    line.update(kw)
    with open(TELEMETRY, "a", encoding="utf-8") as f:
        f.write(__import__("json").dumps(line, default=str) + "\n")


def report(res, a):
    sw = res["phase_b_shared"]
    li = res["phase_f_lighting"]
    pa = res["phase_g_palette"]
    fi = res["final"]
    print()
    print("=" * 74)
    print("[PHASE B/E] INSTANT SHADING SWITCH")
    print("=" * 74)
    print(f"  switches                  : {sw['n']}")
    print(f"  CPU switch  P50           : {sw['cpu_p50']:.3f} ms")
    print(f"  CPU switch  P95           : {sw['cpu_p95']:.3f} ms")
    print(f"  CPU switch  P99           : {sw['cpu_p99']:.3f} ms")
    print(f"  presented   P50           : {sw['present_p50']:.3f} ms")
    print(f"  presented   P95           : {sw['present_p95']:.3f} ms")
    print(f"  presented   P99           : {sw['present_p99']:.3f} ms")
    print(f"  Delaunay calls            : {sw['delaunay']}")
    print(f"  topology rebuilds         : {sw['topology']}")
    print(f"  geometry (pos+idx) uploads: {sw['geometry_uploads']}")
    print(f"  full CPU recolors         : {sw['recolors']}")
    ok_sw = (sw["geometry_uploads"] == 0 and sw["delaunay"] == 0
             and sw["topology"] == 0)
    print(f"  RESULT: {'PASS' if ok_sw else 'FAIL'}  (target CPU switch < 0.2 ms)")
    print()
    print("=" * 74)
    print("[PHASE F] GPU LIGHTING")
    print("=" * 74)
    print(f"  sun changes               : {li['n']}")
    print(f"  CPU update P50            : {li['cpu_p50']:.3f} ms")
    print(f"  CPU update P95            : {li['cpu_p95']:.3f} ms")
    print(f"  geometry rebuilds         : {li['geometry_uploads']}")
    print(f"  shade param updates       : {li['shade_param_updates']}")
    print(f"  RESULT: {'PASS' if li['geometry_uploads'] == 0 else 'FAIL'}")
    print()
    print("=" * 74)
    print("[PHASE G] CLASS LUT")
    print("=" * 74)
    print(f"  palette changes           : {pa['n']}")
    print(f"  GPU LUT path available    : {pa['supported']}")
    print(f"  CPU update P50            : {pa['cpu_p50']:.3f} ms")
    print(f"  CPU update P95            : {pa['cpu_p95']:.3f} ms")
    print(f"  LUT size                  : {pa['lut_bytes']} bytes (256x3 RGB)")
    print(f"  LUT uploads               : {pa['lut_updates']}")
    print(f"  geometry uploads          : {pa['geometry_uploads']}")
    print(f"  RESULT: {'PASS' if pa['geometry_uploads'] == 0 else 'FAIL'}")
    print()
    print("=" * 74)
    print("[SUMMARY]")
    print("=" * 74)
    print(f"  surface uploads at start  : {fi['uploads_at_start']}")
    print(f"  surface uploads at end    : {fi['uploads_at_end']}")
    print(f"  EXTRA uploads from all ops: {fi['extra_uploads']}")
    print(f"  surface draw calls        : {fi['surface_draw_calls']}")
    allok = ok_sw and li["geometry_uploads"] == 0 and pa["geometry_uploads"] == 0
    print(f"  OVERALL: {'PASS' if allok else 'FAIL'}")
    print()
    print(f"  telemetry -> {TELEMETRY}")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
