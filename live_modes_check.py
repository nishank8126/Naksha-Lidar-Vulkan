"""LIVE basic-modes acceptance against the REAL production application.

This is NOT a mock viewport and NOT split-preview mode. It constructs the real
`NakshaApp`, opens the real dataset through the real `open_file` path, and drives
the real Qt event loop - exactly like `py main.py`, only scripted.

What it proves, per mode:
  * the native mode actually changed (no silent fallback to Neutral)
  * XYZ/Class and XYZ/Intensity are per-block ALIGNED
  * the XYZ upload counter does NOT move on a mode switch
  * mode readiness reports READY / PENDING honestly
  * a rendered frame is non-black and varies (readback when available)

Readback is attempted through the render backend; if the platform refuses a
readback the harness says UNVERIFIED rather than claiming a pass.
"""
import os, sys, time, json

os.environ["NAKSHA_RENDER_BACKEND"] = "vulkan"
os.environ["NAKSHA_VULKAN_MAIN_VIEWPORT"] = "1"
os.environ["NAKSHA_VULKAN_PREVIEW"] = ""      # never the split preview
os.environ.setdefault("NAKSHA_DEV_ATTRIBUTE_TRACE", "1")

ROOT = r"H:\naksha-lidar 2"
DIAG = os.path.join(ROOT, "diagnostics")
DATA = os.path.join(ROOT, "test_classified_highprecision.laz")
if not os.path.exists(DATA):
    DATA = os.path.join(ROOT, "123.las")
sys.path.insert(0, ROOT)
os.chdir(ROOT)
for n in ("stdout", "stderr"):
    try:
        getattr(sys, n).reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np
from PySide6.QtWidgets import QApplication
from gui.app_window import NakshaApp
from vulkan_engine_test import _suppress_modal_dialogs, pump

M = "##"
RESULTS = []


def adapter_of(win):
    m = getattr(win, "naksha_stream", None)
    return getattr(m, "adapter", None) if m is not None else None


def mgr_of(win):
    return getattr(win, "naksha_stream", None)


def xyz_uploads(win):
    a = adapter_of(win)
    if a is None:
        return -1
    try:
        return int(a.stats().get("uploads", -1))
    except Exception:
        return -1


def attr_uploads(win):
    a = adapter_of(win)
    if a is None:
        return -1
    try:
        return int(a.stats().get("attribute_uploads", -1))
    except Exception:
        return -1
def alignment(win):
    """Per-block XYZ/Class/Intensity alignment, read from the LIVE manager."""
    m = mgr_of(win)
    if m is None:
        return {"ok": False, "reason": "no manager"}
    keys = [k for k, v in m.resident.items() if v.state == "GPU_RESIDENT"]
    xyz_n = sum(int(m.resident[k].count) for k in keys)
    cls_n = sum(int(np.asarray(m.resident[k].cls).reshape(-1).size)
                for k in keys if getattr(m.resident[k], "cls", None) is not None)
    int_n = sum(int(np.asarray(m.resident[k].inten).reshape(-1).size)
                for k in keys if getattr(m.resident[k], "inten", None) is not None)
    return {
        "blocks": len(keys), "xyz": xyz_n, "class": cls_n, "intensity": int_n,
        "blocks_with_class": sum(1 for k in keys
                                 if getattr(m.resident[k], "cls", None) is not None),
        "blocks_with_intensity": sum(1 for k in keys
                                     if getattr(m.resident[k], "inten", None) is not None),
        "class_aligned": (cls_n == xyz_n) if cls_n else None,
        "intensity_aligned": (int_n == xyz_n) if int_n else None,
    }


def native_mode(win):
    """The native enum the adapter last pushed (None = not pushed)."""
    a = adapter_of(win)
    try:
        return getattr(a, "mode", None)
    except Exception:
        return None


def readback(win):
    """Attempt a real pixel readback. Returns (verified, stats)."""
    rb = getattr(win, "render_backend", None)
    be = getattr(rb, "vulkan_backend", None) if rb is not None else None
    for name in ("read_pixels", "readback_frame", "capture_frame",
                 "read_frame_pixels", "screenshot_pixels"):
        fn = getattr(be, name, None) if be is not None else None
        if callable(fn):
            try:
                arr = fn()
                if arr is not None:
                    a = np.asarray(arr)
                    if a.ndim == 3 and a.size:
                        flat = a.reshape(-1, a.shape[-1])[:, :3]
                        return True, {
                            "nonzero_px": int(np.count_nonzero(flat.any(axis=1))),
                            "unique_colours": int(len(np.unique(flat, axis=0))),
                            "shape": list(a.shape)}
            except Exception as exc:
                return False, {"error": repr(exc)}
    return False, {"reason": "no readback entry point on this backend"}


def settle(win, secs=25.0):
    t0 = time.time()
    while time.time() - t0 < secs:
        pump(app_qt, 1.0)
        m = mgr_of(win)
        if m is not None and len(getattr(m, "_pending", {}) or {}) == 0:
            pump(app_qt, 1.0)
            if len(getattr(mgr_of(win), "_pending", {}) or {}) == 0:
                return True
def switch(win, mode):
    """Switch through the REAL application API, then settle."""
    before = xyz_uploads(win)
    fn = getattr(win, "set_display_mode", None) or getattr(
        win, "_set_display_mode", None)
    ok = False
    if callable(fn):
        try:
            ok = bool(fn(mode))
        except Exception as exc:
            print(f"{M} switch({mode}) raised {exc!r}", flush=True)
    pump(app_qt, 1.5)
    settle(win, 30.0)
    return ok, before, xyz_uploads(win)
    return False
def lut_stats(win):
    """What colour tables the RENDERER currently holds."""
    out = {}
    m = mgr_of(win)
    a = adapter_of(win)
    for name in ("last_class_lut", "last_class_visibility",
                 "last_elevation_range", "last_intensity_range",
                 "last_depth_range"):
        v = getattr(a, name, None)
        out[name] = None if v is None else (
            [int(v[0]), int(v[1]), int(v[2])] if isinstance(v, tuple)
            else np.asarray(v).shape)
    # Uniqueness of the pushed class LUT is the real test: a flat table means
    # Class is rendering Neutral regardless of how well the stream is aligned.
    try:
        from gui.naksha_cache.display_modes import build_luts
        lut = build_luts(getattr(win, "app", win))["class"]
        out["canonical_class_lut_unique"] = int(
            len(np.unique(np.asarray(lut), axis=0)))
        out["app_class_palette_size"] = len(
            getattr(win, "class_palette", None) or {})
    except Exception as exc:
        out["lut_error"] = repr(exc)
    return out


app_qt = QApplication.instance() or QApplication(sys.argv[:1])
_suppress_modal_dialogs()
print(f"{M} dataset={DATA}", flush=True)
win = NakshaApp()
win.resize(1400, 900)
win.show()
pump(app_qt, 2.0)

rb = win.render_backend
print(f"{M} backend_active={getattr(rb, 'active', None)} "
      f"owns_main_camera={rb.owns_main_camera() if hasattr(rb, 'owns_main_camera') else '?'}",
      flush=True)

win.open_file(filenames=[DATA], prompt_import=False)
print(f"{M} opening...", flush=True)
ok_open = False
t0 = time.time()
while time.time() - t0 < 240:
    pump(app_qt, 1.5)
    m = mgr_of(win)
    if m is not None and getattr(m, "_points_resident", 0) > 0:
        ok_open = True
        break
print(f"{M} opened={ok_open} points_resident="
      f"{getattr(mgr_of(win), '_points_resident', 0)} "
      f"render_mode={getattr(mgr_of(win), 'render_mode', None)}", flush=True)

for mode in ("neutral", "class", "class", "intensity", "elevation", "depth",
             "neutral"):
    ok, before, after = switch(win, mode)
    a = alignment(win)
    try:
        ready = mgr_of(win).mode_readiness()
    except Exception:
        ready = {}
    verified, rb_stats = readback(win)
    row = {
        "mode": mode, "switch_ok": ok, "native_mode": native_mode(win),
        "ui_mode": getattr(win, "display_mode", None),
        "stream_mode": getattr(mgr_of(win), "display_mode", None),
        "xyz_uploads_before": before, "xyz_uploads_after": after,
        "xyz_reuploaded": (after > before) if (before >= 0 and after >= 0) else None,
        "attribute_uploads": attr_uploads(win), "alignment": a,
        "readiness": ready, "frame_verified": verified, "readback": rb_stats,
    }
    RESULTS.append(row)
    print(f"{M} === {mode.upper()} ===", flush=True)
    print(f"{M} native={row['native_mode']} ui={row['ui_mode']} "
          f"stream={row['stream_mode']}", flush=True)
    print(f"{M} xyz_uploads {before} -> {after} "
          f"reuploaded={row['xyz_reuploaded']} "
          f"attr_uploads={row['attribute_uploads']}", flush=True)
    print(f"{M} blocks={a.get('blocks')} xyz={a.get('xyz')} "
          f"class={a.get('class')}(aligned={a.get('class_aligned')}) "
          f"intensity={a.get('intensity')}(aligned={a.get('intensity_aligned')})",
          flush=True)
    print(f"{M} readiness={ready.get('status')} missing={ready.get('missing_attrs')}",
          flush=True)
    print(f"{M} frame_verified={verified} {rb_stats}", flush=True)
    ls = lut_stats(win)
    print(f"{M} luts={ls}", flush=True)

out = os.path.join(DIAG, "phase1", "live_modes.json")
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w", encoding="utf-8") as fh:
    json.dump({"dataset": DATA, "results": RESULTS}, fh, indent=1)
print(f"{M} wrote {out}", flush=True)
try:
    win.close()
except Exception:
    pass


def switch(win, mode):
    """Switch through the REAL application API, then settle."""
    before = xyz_uploads(win)
    fn = getattr(win, "set_display_mode", None) or getattr(
        win, "_set_display_mode", None)
    ok = False
    if callable(fn):
        try:
            ok = bool(fn(mode))
        except Exception as exc:
            print(f"{M} switch({mode}) raised {exc!r}", flush=True)
    pump(app_qt, 1.5)
    settle(win, 30.0)
    return ok, before, xyz_uploads(win)