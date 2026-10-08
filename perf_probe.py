"""PHASE 1/2 - profile the REAL production frame path before optimizing.

Runs the real NakshaApp on the real 27M-point dataset, drives real Qt input
(pan / zoom / orbit / 3D zoom) and reports measured counters.

This does NOT optimize anything. It measures, so the optimization targets the
bottleneck that actually exists rather than the one that seems likely.
"""
import os, sys, time, json, statistics

os.environ["NAKSHA_RENDER_BACKEND"] = "vulkan"
os.environ["NAKSHA_VULKAN_MAIN_VIEWPORT"] = "1"
os.environ["NAKSHA_VULKAN_PREVIEW"] = ""      # never split preview
os.environ["NAKSHA_DEV_PERF"] = "1"           # the mission's profiling switch

ROOT = r"H:\naksha-lidar 2"
DIAG = os.path.join(ROOT, "diagnostics")
DATA = os.path.join(ROOT, os.environ.get("PERF_DATASET",
                                         "test_classified_highprecision.laz"))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
for n in ("stdout", "stderr"):
    try:
        getattr(sys, n).reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np
from PySide6.QtCore import QPoint, Qt, QEvent
from PySide6.QtGui import QWheelEvent, QMouseEvent
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication
from gui.app_window import NakshaApp
from vulkan_engine_test import _suppress_modal_dialogs, pump


def send_key(win, key, modifier=Qt.NoModifier):
    """Real Qt key delivery to the main window (Shift+P for the 3D toggle)."""
    from PySide6.QtTest import QTest
    QTest.keyClick(win, key, modifier)

M = "@@"
STAGES = {}


def mgr_of(win):
    return getattr(win, "naksha_stream", None)


def adapter_of(win):
    m = mgr_of(win)
    return getattr(m, "adapter", None) if m is not None else None


def record(stage, ms):
    STAGES.setdefault(stage, []).append(float(ms))


def _lod_mix(m, keys):
    out = {}
    for k in keys:
        try:
            lod = int(k[1])
        except Exception:
            lod = -1
        out[lod] = out.get(lod, 0) + 1
    return out


def snapshot(win):
    """One measurement of the real counters."""
    m = mgr_of(win)
    a = adapter_of(win)
    st = {}
    try:
        st.update(a.stats())
    except Exception:
        pass
    keys = [k for k, v in (m.resident.items() if m is not None else [])
            if getattr(v, "state", None) == "GPU_RESIDENT"]
    resident_points = sum(int(m.resident[k].count) for k in keys) if keys else 0
    ranges = 0
    try:
        ranges = len(m._build_ranges(keys))
    except Exception:
        pass
    return {
        "resident_points": resident_points,
        "resident_blocks": len(keys),
        "draw_ranges": ranges,
        "lod_mix": _lod_mix(m, keys),
        "uploads": int(st.get("uploads", 0) or 0),
        "attribute_uploads": int(st.get("attribute_uploads", 0) or 0),
        "camera_generation": int(getattr(m, "_gen", 0) or 0),
        "camera_only_frames": int(getattr(m, "camera_only_frames", 0) or 0),
        "render_mode": getattr(m, "render_mode", None),
        "hot_ms_last": round(float(getattr(m, "hot_ms_last", 0.0) or 0.0), 3),
        "bg_ms_last": round(float(getattr(m, "bg_ms_last", 0.0) or 0.0), 3),
        "is_3d": bool(getattr(win, "is_3d_mode", False)),
        "perf_last_flush": dict(getattr(m, "_perf_last_flush", {}) or {}),
    }


def bytes_read_delta(win, fn):
    """Wall ms for `fn` PLUS bytes actually read from the cache during it."""
    before = 0
    m = mgr_of(win)
    try:
        before = int(m.reader.stats.get("bytes_read", 0) or 0)
    except Exception:
        pass
    t0 = time.perf_counter()
    fn()
    pump(app_qt, 0.4)
    dt = (time.perf_counter() - t0) * 1000.0
    after = 0
    try:
        after = int(m.reader.stats.get("bytes_read", 0) or 0)
    except Exception:
        pass
    return dt, after - before


def drive_pan(win, seconds=5.0):
    """Real mouse-motion events delivered to the real viewport widget."""
    w = win.vtk_widget
    x, y = w.width() // 2, w.height() // 2
    t0 = time.perf_counter()
    n = 0
    while time.perf_counter() - t0 < seconds:
        pump(app_qt, 0.008)
        x += 4 if n % 2 == 0 else -4
        app_qt.postEvent(w, QMouseEvent(
            QEvent.MouseMove, QPointF(x, y), QPointF(x, y),
            Qt.NoButton, Qt.NoButton, Qt.NoModifier))
        n += 1
    pump(app_qt, 0.3)
    return n


def drive_wheel(win, notches=14, pause=0.05):
    w = win.vtk_widget
    c = QPoint(w.width() // 2, w.height() // 2)
    for _ in range(notches):
        app_qt.postEvent(w, QWheelEvent(
            c, c, QPoint(0, 0), QPoint(0, 120),
            Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False))
        pump(app_qt, pause)


def _gpu_frame_ms(win):
    """Native frame cost for the currently submitted draw set."""
    rb = getattr(win, "render_backend", None)
    be = getattr(rb, "vulkan_backend", None) if rb is not None else None
    if be is None:
        return None
    for name in ("last_frame_ms", "get_last_frame_ms", "frame_ms"):
        fn = getattr(be, name, None)
        if callable(fn):
            try:
                v = fn()
                if v:
                    return round(float(v), 3)
            except Exception:
                pass
    return None


def _draw_budget_experiment(win):
    """THE DECISIVE MEASUREMENT (Phase 2).

    Compare the native frame cost at the FULL draw set against the SAME camera
    with progressively fewer draw ranges. If frame time scales with the number
    of submitted POINTS and not with the number of ranges, the bottleneck is
    vertex/raster throughput (GPU), and the fix is interaction LOD.

    This changes only which pre-existing resident ranges are submitted. It
    re-uploads nothing, reads nothing, and restores the full set afterwards.
    """
    m = mgr_of(win)
    if m is None:
        return {}
    keys = [k for k, v in m.resident.items()
            if getattr(v, "state", None) == "GPU_RESIDENT"]
    full = m._build_ranges(keys)
    total_points = sum(int(c) for _f, c in full)
    out = {"full_ranges": len(full), "full_points": total_points, "levels": []}
    pump(app_qt, 0.5)
    out["levels"].append({
        "label": "full", "ranges": len(full), "points": total_points,
        "gpu_frame_ms": _gpu_frame_ms(win)})
    # Halve the SUBMITTED ranges (still resident, still uploaded) and re-measure.
    for frac in (0.5, 0.25, 0.125):
        subset = full[: max(1, int(len(full) * frac))]
        pts = sum(int(c) for _f, c in subset)
        try:
            m.adapter.set_draw_ranges(subset)
            win.render_backend.resync_camera(present=True)
        except Exception as exc:
            out.setdefault("error", repr(exc))
            break
        pump(app_qt, 0.5)
        out["levels"].append({
            "label": f"{int(frac*100)}%", "ranges": len(subset),
            "points": pts, "gpu_frame_ms": _gpu_frame_ms(win)})
    # Restore.
    try:
        m.adapter.set_draw_ranges(full)
        win.render_backend.resync_camera(present=True)
    except Exception:
        pass
    pump(app_qt, 0.3)
    return out


app_qt = QApplication.instance() or QApplication(sys.argv[:1])
_suppress_modal_dialogs()
print(f"{M} dataset={DATA}", flush=True)
win = NakshaApp()
win.resize(1400, 900)
win.show()
pump(app_qt, 2.0)

win.open_file(filenames=[DATA], prompt_import=False)
t0 = time.time()
while time.time() - t0 < 300:
    pump(app_qt, 1.5)
    m = mgr_of(win)
    if m is not None and getattr(m, "full_resident_ready", False):
        break
m = mgr_of(win)
print(f"{M} loaded resident={getattr(m, '_points_resident', 0):,} "
      f"render_mode={getattr(m, 'render_mode', None)} "
      f"full_resident_ready={getattr(m, 'full_resident_ready', None)} "
      f"blocks={len(getattr(m, 'resident', {}) or {})}", flush=True)

RESULTS = {}

idle_after = snapshot(win)
t0 = time.perf_counter()
pump(app_qt, 3.0)
idle_after = snapshot(win)
RESULTS["idle"] = {"after": idle_after,
                   "wall_ms": round((time.perf_counter() - t0) * 1000.0, 1)}
print(f"{M} IDLE {idle_after}", flush=True)

gen0 = idle_after["camera_generation"]
up0 = idle_after["uploads"]
pan_ms, pan_bytes = bytes_read_delta(win, lambda: drive_pan(win, 5.0))
pan_after = snapshot(win)
RESULTS["pan"] = {"ms": round(pan_ms, 1), "disk_bytes": pan_bytes,
                  "before": idle_after, "after": pan_after,
                  "gen_delta": pan_after["camera_generation"] - gen0,
                  "xyz_uploads_delta": pan_after["uploads"] - up0}
print(f"{M} PAN {RESULTS['pan']}", flush=True)

gen0 = pan_after["camera_generation"]
up0 = pan_after["uploads"]
zoom_ms, zoom_bytes = bytes_read_delta(win, lambda: drive_wheel(win))
zoom_after = snapshot(win)
RESULTS["zoom"] = {"ms": round(zoom_ms, 1), "disk_bytes": zoom_bytes,
                   "before": pan_after, "after": zoom_after,
                   "gen_delta": zoom_after["camera_generation"] - gen0,
                   "xyz_uploads_delta": zoom_after["uploads"] - up0}
print(f"{M} ZOOM {RESULTS['zoom']}", flush=True)

send_key(win, Qt.Key.Key_P, Qt.KeyboardModifier.ShiftModifier)
pump(app_qt, 2.0)
three_d = snapshot(win)
rig = getattr(win.render_backend, "_camera_rig", None)
print(f"{M} 3D is_3d={getattr(win, 'is_3d_mode', None)} "
      f"rig_ortho={getattr(rig, 'orthographic_', None)} "
      f"dist={getattr(rig, 'distance', None)}", flush=True)

gen0 = three_d["camera_generation"]
up0 = three_d["uploads"]
orbit_ms, orbit_bytes = bytes_read_delta(win, lambda: drive_pan(win, 5.0))
orbit_after = snapshot(win)
RESULTS["orbit3d"] = {"ms": round(orbit_ms, 1), "disk_bytes": orbit_bytes,
                      "before": three_d, "after": orbit_after,
                      "gen_delta": orbit_after["camera_generation"] - gen0,
                      "xyz_uploads_delta": orbit_after["uploads"] - up0}
print(f"{M} ORBIT3D {RESULTS['orbit3d']}", flush=True)

gen0 = orbit_after["camera_generation"]
up0 = orbit_after["uploads"]
dolly_ms, dolly_bytes = bytes_read_delta(win, lambda: drive_wheel(win))
dolly_after = snapshot(win)
RESULTS["dolly3d"] = {"ms": round(dolly_ms, 1), "disk_bytes": dolly_bytes,
                      "before": orbit_after, "after": dolly_after,
                      "gen_delta": dolly_after["camera_generation"] - gen0,
                      "xyz_uploads_delta": dolly_after["uploads"] - up0}
print(f"{M} DOLLY3D {RESULTS['dolly3d']}", flush=True)

send_key(win, Qt.Key.Key_P, Qt.KeyboardModifier.ShiftModifier)
pump(app_qt, 2.0)
RESULTS["back2d"] = snapshot(win)
print(f"{M} back2d is_3d={getattr(win, 'is_3d_mode', None)}", flush=True)

# ---- 6. THE DECISIVE MEASUREMENT (Phase 2) ----------------------------
RESULTS["draw_budget"] = _draw_budget_experiment(win)
print(f"{M} DRAW_BUDGET {json.dumps(RESULTS['draw_budget'], default=str)}",
      flush=True)

RESULTS["stage_timings_ms"] = {
    k: {"n": len(v), "p50": round(statistics.median(v), 3),
        "p95": round(sorted(v)[max(0, int(len(v) * 0.95) - 1)], 3),
        "max": round(max(v), 3)}
    for k, v in STAGES.items() if v
}
out = os.path.join(DIAG, "phase1", "perf_profile.json")
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w", encoding="utf-8") as fh:
    json.dump(RESULTS, fh, indent=1, default=str)
print(f"{M} STAGES {json.dumps(RESULTS['stage_timings_ms'], indent=1)}", flush=True)
print(f"{M} wrote {out}", flush=True)
try:
    win.close()
except Exception:
    pass