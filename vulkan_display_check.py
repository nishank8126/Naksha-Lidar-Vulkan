"""vulkan_display_check.py - production display validation for the Vulkan
viewport: PTC ordering, GPU LUT re-tint, zoom survival, point sizing, shaded
visibility and load states.

Everything is measured through the production code paths and the engine's own
counters. The zoom investigation in particular does NOT guess a cause: at every
camera step it captures the real frame (nkv_capture_frame) AND reads back how
many points the clip planes about to be pushed would discard, plus the
gl_PointSize range the adaptive model resolves to. Whichever of those breaks
identifies the cause.

Run:  python vulkan_display_check.py
Exit 0 = all passed, 1 = a failure, 3 = harness error.
"""
from __future__ import annotations

import os
import sys
import time
import traceback

os.environ.setdefault("NAKSHA_RENDER_BACKEND", "vulkan")
os.environ.setdefault("NAKSHA_VULKAN_PREVIEW", "")
os.environ.setdefault("NAKSHA_VULKAN_CAMERA_DEBUG", "1")

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
PTC_PATH = r"H:\TESTING CONTIUES\Class_ENEL_2025_connect 1.ptc"
OUT_DIR = os.path.join(PROJECT_ROOT, "vulkan_display_output")

_CHECKS: list = []
R: dict = {}


def check(name: str, ok: bool, detail: str = "") -> bool:
    ok = bool(ok)
    _CHECKS.append((name, ok, detail))
    print(f"[disp] {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""),
          flush=True)
    return ok


def coverage(img) -> float:
    """Fraction of non-background pixels in a native capture."""
    if img is None:
        return 0.0
    a = np.asarray(img)[..., :3]
    return float((a.sum(axis=2) > 20).mean())


def _num(line, key, default=0.0):
    for tok in line.split():
        if tok.startswith(key + "="):
            try:
                return float(tok.split("=", 1)[1].strip("[]"))
            except ValueError:
                return default
    return default


def main() -> int:
    from PySide6.QtWidgets import QApplication
    from gui.app_window import NakshaApp
    from vulkan_engine_test import _suppress_modal_dialogs, pump, wait_until

    os.makedirs(OUT_DIR, exist_ok=True)
    print("=" * 72)
    print("VULKAN DISPLAY VALIDATION")
    print(f"  LAS: {LAS_PATH}")
    print(f"  PTC: {PTC_PATH}")
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
    check("Vulkan backend active", True,
          f"device={b.get_device_name()} api={b.get_api_version_string()} "
          f"max_point_size={b.get_max_point_size():.1f}")
    R["device"] = b.get_device_name()
    R["max_point_size"] = b.get_max_point_size()

    # ---- load (PTC must be applied before the first frame) ---------------
    print("\n[disp] loading 123.las ...", flush=True)
    win.open_file(filenames=[LAS_PATH], prompt_import=False)
    if not wait_until(app_qt, lambda: bool(getattr(win, "data", None)), timeout=300.0):
        check("LAS loaded", False, "timeout")
        return _report()
    pump(app_qt, 1.5)
    n_points = int(win.data["xyz"].shape[0])
    R["points"] = n_points
    check("LAS loaded", True, f"points={n_points:,}")
    R["startup_ptc"] = getattr(win, "_last_startup_ptc", "")
    R["loading_state"] = getattr(win, "_vulkan_loading_state", "?")
    R["palette_classes"] = len(win.class_palette or {})
    check("PTC applied before first frame",
          bool(R["startup_ptc"]) and R["palette_classes"] > 0,
          f"startup_ptc={os.path.basename(R['startup_ptc']) or 'NONE'} "
          f"palette={R['palette_classes']} classes, state={R['loading_state']}")

    # ---- 2. GPU LUT re-tint without a point re-upload ---------------------
    pos0, lut0 = b.get_point_position_upload_count(), b.get_lut_update_count()
    win.class_palette.setdefault(0, {"color": (200, 10, 10), "show": True})
    rb.sync_point_shading(mode="class")
    b.request_render()
    pump(app_qt, 0.4)
    pos1, lut1 = b.get_point_position_upload_count(), b.get_lut_update_count()
    R["lut_before"], R["lut_after"] = lut0, lut1
    R["pos_before"], R["pos_after"] = pos0, pos1
    check("GPU LUT updates on palette change", lut1 > lut0,
          f"lutUpdateCount {lut0} -> {lut1}")
    check("point buffer NOT re-uploaded after LUT change", pos1 == pos0,
          f"positionUploads {pos0} -> {pos1}")
    # ---- 3/4. zoom survival: measured, not guessed ------------------------
    print("\n[disp] --- zoom / pan investigation (measured) ---", flush=True)
    rig = rb._camera_rig
    xyz = np.asarray(win.data["xyz"])
    bounds = np.stack([xyz.min(axis=0), xyz.max(axis=0)])
    rig.fit_to_bounds(bounds, aspect=1.6)
    rb.resync_camera(present=False)
    pump(app_qt, 0.3)

    def probe(label, notches=None, pan_px=None):
        if notches is not None:
            rig.dolly(notches)
        if pan_px is not None:
            rig.pan(pan_px, 0, 841)
        b.set_camera_lookat(**rig.push_kwargs())
        b.request_render()
        pump(app_qt, 0.25)
        line = rb.describe_camera(label)
        img = b.capture_frame()
        if img is not None:
            np.save(os.path.join(OUT_DIR, f"zoom_{label}.npy"), img)
        seg = line.split("gl_PointSize=[")[-1].split("]")[0].split(",") \
            if "gl_PointSize=[" in line else ["0", "0"]
        return {"label": label, "coverage": coverage(img), "dist": rig.distance,
                "near": rig.near_clip, "far": rig.far_clip,
                "behind_eye": int(_num(line, "pts_behind_eye")),
                "clipped_near": int(_num(line, "pts_clipped_near")),
                "clipped_far": int(_num(line, "pts_clipped_far")),
                "psize_min": float(seg[0]), "psize_max": float(seg[-1])}

    # +notches zooms IN (factor 0.88^N < 1 shrinks distance), -notches zooms OUT.
    steps = [probe("fit"), probe("zoom_in_5", notches=5.0),
             probe("zoom_in_12", notches=12.0), probe("zoom_in_25", notches=25.0),
             probe("zoom_out_5", notches=-5.0), probe("pan", pan_px=120.0)]
    R["zoom"] = steps
    for s in steps:
        print(f"  {s['label']:<14} cov={s['coverage']*100:5.1f}%  dist={s['dist']:8.2f}  "
              f"near={s['near']:.4f} far={s['far']:9.2f}  "
              f"behind_eye={s['behind_eye']:<6} clipped[near={s['clipped_near']} "
              f"far={s['clipped_far']}]  "
              f"gl_PointSize=[{s['psize_min']:.2f},{s['psize_max']:.2f}]", flush=True)

    visible = [s for s in steps if s["coverage"] > 0.01]
    check("points remain visible at every zoom level", len(visible) == len(steps),
          f"{len(visible)}/{len(steps)} steps rendered points")
    # Points BEHIND the eye must be clipped (that is correct); what must not
    # happen is an in-front point being discarded by a stale plane.
    check("no in-front points clipped by near/far at any zoom",
          all(s["clipped_near"] == 0 and s["clipped_far"] == 0 for s in steps),
          "measured against the planes actually pushed; points behind the eye "
          "are expected to be clipped")
    dev_max = max(float(R["max_point_size"]), 1.0)
    check("gl_PointSize stays >=1px and within the device limit",
          all(s["psize_min"] >= 1.0 and s["psize_max"] <= dev_max for s in steps),
          f"device max={dev_max:.1f}")

    # ---- 5. Shaded Class visibility --------------------------------------
    print("\n[disp] --- Shaded Class ---", flush=True)
    from gui import shading_display as sd
    sd.update_shaded_class(win, azimuth=45.0, ambient=0.25)
    pump(app_qt, 1.0)
    b.set_camera_lookat(**rig.push_kwargs())
    b.request_render()
    pump(app_qt, 0.4)
    sd_up = b.get_surface_upload_count()
    sd_draw0 = b.get_surface_draw_call_count()
    ov_draw0 = b.get_surface_overlay_draw_call_count()
    b.request_render()
    pump(app_qt, 0.3)
    sd_draw1 = b.get_surface_draw_call_count()
    ov_draw1 = b.get_surface_overlay_draw_call_count()
    img_shaded = b.capture_frame()
    if img_shaded is not None:
        np.save(os.path.join(OUT_DIR, "shaded_native.npy"), img_shaded)
    R["surface_uploads"] = sd_up
    R["surface_draws"] = sd_draw1 - sd_draw0
    R["overlay_draws"] = ov_draw1 - ov_draw0
    R["shaded_coverage"] = coverage(img_shaded)
    key = sd._get_rendered_cache_key(win)
    cache = sd._cache_store.get(key) if key is not None else None
    R["shaded_faces"] = 0 if cache is None else int(len(cache.faces))
    R["shaded_vertices"] = 0 if cache is None else int(len(cache.xyz_final))
    check("Shaded Class: surface uploaded", sd_up >= 1,
          f"surface_upload_count={sd_up} faces={R['shaded_faces']:,} "
          f"verts={R['shaded_vertices']:,}")
    check("Shaded Class: draw calls submitted each frame",
          (sd_draw1 - sd_draw0) >= 1,
          f"surface draw calls/frame={sd_draw1 - sd_draw0} "
          f"overlay={ov_draw1 - ov_draw0}")
    check("Shaded Class: surface visible", coverage(img_shaded) > 0.01,
          f"coverage={coverage(img_shaded)*100:.1f}%")

    # ---- 6. Surface mode --------------------------------------------------
    print("\n[disp] --- Surface ---", flush=True)
    from gui import surface_mode as sm
    up_before = b.get_surface_upload_count()
    sm.render_surface_mode(win)
    pump(app_qt, 1.0)
    b.set_camera_lookat(**rig.push_kwargs())
    b.request_render()
    pump(app_qt, 0.5)
    img_surf = b.capture_frame()
    if img_surf is not None:
        np.save(os.path.join(OUT_DIR, "surface_native.npy"), img_surf)
    pts = getattr(win, "_surface_points", None)
    fcs = getattr(win, "_surface_faces", None)
    R["surface_vertices"] = 0 if pts is None else int(len(pts))
    R["surface_faces"] = 0 if fcs is None else int(len(fcs))
    R["surface_uploads_delta"] = b.get_surface_upload_count() - up_before
    R["surface_coverage"] = coverage(img_surf)
    check("Surface: uploaded once", R["surface_uploads_delta"] == 1,
          f"delta={R['surface_uploads_delta']} verts={R['surface_vertices']:,} "
          f"faces={R['surface_faces']:,}")
    check("Surface: first frame has pixels", R["surface_coverage"] > 0.01,
          f"coverage={R['surface_coverage']*100:.1f}%")

    return _report()


def _report() -> int:
    import display_report
    return display_report.print_report(R, _CHECKS, OUT_DIR)


if __name__ == "__main__":
    try:
        _CODE = main()
    except BaseException:
        traceback.print_exc()
        _CODE = 3
    finally:
        sys.stdout.flush()
    raise SystemExit(_CODE)
