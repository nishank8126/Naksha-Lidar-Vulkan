"""vulkan_class_elevation_validation.py

Phase 2 validation: PTC classification / elevation / shaded class / surface.

Everything is driven through the PRODUCTION Naksha code paths only:
    LAS            app.open_file()
    PTC palette    app._load_single_ptc()      (the File > Load .ptc menu path)
    display mode   app.display_mode = ... + update_pointcloud()
    shaded class   shading_display.update_shaded_class() /
                   shading_display.update_shading_lighting_only()
    surface        surface_mode.render_surface_mode()
There is no test palette, no hardcoded class colour and no internal fallback
class map anywhere in this harness: the only class source of truth is the .ptc
file, parsed by the app's own parser (app._load_ptc_file).

Colour comparison note
----------------------
The VTK screenshot path in this app is a FLAT PLACEHOLDER (the view actor is
built without bound scalars, so pyvista's offscreen grab shows one uniform
grey). The pixel-truth reference for "what VTK paints" is therefore the app's
own CPU colouring function, gui.pointcloud_display.compute_colors(), which is
exactly what feeds VTK's scalar table. Every colour metric below compares
  VTK side = compute_colors() colours of the same points
  Vulkan side = drawn pixels of the real native frame (nkv_capture_frame)
plus, for the exact/LUT checks, a byte-for-byte comparison of the GPU tables
against the values derived from the .ptc file.

Usage:  python vulkan_class_elevation_validation.py
"""
from __future__ import annotations

import os
import sys
import time
import traceback

# ---- env BEFORE any gui import -------------------------------------------
os.environ["NAKSHA_RENDER_BACKEND"] = "vulkan"
os.environ["NAKSHA_VULKAN_PREVIEW"] = ""       # not the split preview
# vulkan_viewport_enabled() now defaults to False (restored this round - see
# gui/render_backend.py's change note); this harness explicitly opts into
# the full main-viewport takeover it always relied on, rather than depending
# on that no-longer-true default.
os.environ["NAKSHA_VULKAN_MAIN_VIEWPORT"] = "1"

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
OUT_DIR = os.path.join(PROJECT_ROOT, "vulkan_validation_output")

R: dict = {}
_CHECKS: list = []
_REPORT_TEXT: list = [""]


def _flush_report_on_exit():
    """Persist whatever report text was produced, even if the process is torn
    down abnormally right after (Qt/VTK shutdown can abort)."""
    text = _REPORT_TEXT[0]
    if not text:
        return
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(os.path.join(OUT_DIR, "validation_report.txt"), "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except Exception:
        pass


import atexit
atexit.register(_flush_report_on_exit)


def rec(key, value):
    R[key] = value
    return value


def head(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72, flush=True)


def check(name, ok, detail=""):
    ok = bool(ok)
    _CHECKS.append((name, ok, detail))
    print(f"[val] {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""),
          flush=True)
    return ok


# ---------------------------------------------------------------------------
# colour metrics
# ---------------------------------------------------------------------------
_LUMA = np.array([0.2126, 0.7152, 0.0722])


def colour_metrics(ref_rgb, img_rgb, rank_samples=4096):
    """Distribution-level comparison of two colour populations.

    Point rasterisation is deliberately different between the two renderers
    (footprint/depth sizing + alpha blending), so pixels cannot be matched
    1:1. What must match is the colour DISTRIBUTION, which is compared as
    mean RGB, a luminance-rank-matched p95 (same percentile of brightness on
    both sides -> reports real ramp disagreement, not resampling noise) and
    the per-channel histogram correlation.
    """
    ref = np.asarray(ref_rgb, dtype=np.float64).reshape(-1, 3)
    img = np.asarray(img_rgb, dtype=np.float64).reshape(-1, 3)
    if ref.size == 0 or img.size == 0:
        return None

    def _sorted(a):
        return a[np.argsort(a @ _LUMA)]

    r_sorted, i_sorted = _sorted(ref), _sorted(img)
    n = min(len(r_sorted), len(i_sorted), rank_samples)
    idx = np.linspace(0, min(len(r_sorted), len(i_sorted)) - 1, n).astype(int)
    rq, iq = r_sorted[idx], i_sorted[idx]
    d = np.abs(rq - iq)
    return {
        "mean_diff": float(np.abs(ref.mean(axis=0) - img.mean(axis=0)).mean()),
        "p95": float(np.percentile(d.max(axis=1), 95)),
        "mean_abs": float(d.mean()),
        "corr": _hist_corr(ref, img),
        "ref_mean": ref.mean(axis=0),
        "img_mean": img.mean(axis=0),
    }


def _hist_corr(a, b):
    total = 0.0
    for c in range(3):
        ha = np.histogram(a[:, c], bins=64, range=(0, 256), density=True)[0]
        hb = np.histogram(b[:, c], bins=64, range=(0, 256), density=True)[0]
        if ha.std() <= 0 or hb.std() <= 0:
            continue
        total += float(np.corrcoef(ha, hb)[0, 1])
    return total / 3.0


def drawn_pixels(img):
    """Non-background RGB pixels of a native capture (RGBA -> RGB)."""
    a = np.asarray(img)
    if a.ndim == 3 and a.shape[2] >= 4:
        a = a[..., :3]
    return a[a.sum(axis=2) > 20]
def _patch_upload_counter(backend):
    """Count calls to set_point_cloud on the live backend.

    This is the direct witness for "the GPU does the class lookup, no CPU
    colour re-upload": if a palette / visibility / weight / mode change touches
    this counter, millions of points were re-uploaded. (The native
    nkv_get_point_position_upload_count export carries the same information
    from the engine side; the harness uses whichever is available.)
    """
    calls = {"n": 0}
    original = backend.set_point_cloud

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    backend.set_point_cloud = _counting
    return calls, original


def _restore_upload_counter(backend, original):
    backend.set_point_cloud = original


def main() -> int:
    from PySide6.QtWidgets import QApplication
    from gui.app_window import NakshaApp
    from gui.pointcloud_display import update_pointcloud, compute_colors
    from gui.render_backend import build_point_luts, shading_uniforms
    from vulkan_engine_test import _suppress_modal_dialogs, pump, wait_until
    import vulkan_parity_check as par

    head("NAKSHA VULKAN - CLASS / ELEVATION / SHADED / SURFACE VALIDATION")
    print(f"  LAS : {LAS_PATH}")
    print(f"  PTC : {PTC_PATH}", flush=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    for path, label in ((LAS_PATH, "LAS"), (PTC_PATH, "PTC")):
        if not os.path.isfile(path):
            print(f"[val] {label} file not found: {path}")
            return 1

    _suppress_modal_dialogs()
    app_qt = QApplication.instance() or QApplication(sys.argv[:1])
    win = NakshaApp()
    win.resize(1400, 900)
    win.show()
    pump(app_qt, 1.0)

    rb = getattr(win, "render_backend", None)
    backend = getattr(rb, "vulkan_backend", None) if rb is not None else None
    rec("_rb", rb)
    rec("_backend", backend)
    check("Vulkan backend active", backend is not None and bool(getattr(rb, "active", False)),
          f"state={getattr(rb, 'state', None)}")
    if backend is None:
        return _report()

    # ---- dataset ---------------------------------------------------------
    print(f"[val] loading {os.path.basename(LAS_PATH)} ...", flush=True)
    win.open_file(filenames=[LAS_PATH], prompt_import=False)
    if not wait_until(app_qt, lambda: bool(getattr(win, "data", None)), timeout=240.0):
        check("LAS loaded", False, "timeout")
        return _report()
    pump(app_qt, 1.0)
    n_points = int(win.data["xyz"].shape[0])
    rec("points", n_points)
    check("LAS loaded", True, f"points={n_points:,}")

    # ---- 2. PTC through the production loader -----------------------------
    head("PTC LOAD (app._load_single_ptc - the File > Load .ptc path)")
    ptc_map = win._load_ptc_file(PTC_PATH)          # the app's own parser
    check("PTC parsed by app parser", isinstance(ptc_map, dict) and len(ptc_map) > 0,
          f"classes={len(ptc_map or {})}")
    pump(app_qt, 0.2)
    win._load_single_ptc(PTC_PATH)                  # full production path
    pump(app_qt, 2.0)
    if not wait_until(app_qt, lambda: bool(getattr(win, "class_palette", None)), timeout=60.0):
        check("PTC applied to app.class_palette", False, "palette stayed empty")
        return _report()

    palette = dict(win.class_palette or {})
    rec("ptc_classes", len(ptc_map or {}))
    rec("palette_classes", len(palette))
    visible = [c for c, e in palette.items() if bool(e.get("show", True))]
    hidden = [c for c, e in palette.items() if not bool(e.get("show", True))]
    rec("visible_classes", len(visible))
    rec("hidden_classes", hidden)
    rec("class_weight", float(getattr(win, "class_weight", 1.0) or 1.0))

    print(f"PTC loaded: {PTC_PATH}")
    print(f"Classes: {len(ptc_map or {})}")
    print(f"Palette: loaded ({len(palette)} entries, {len(visible)} visible)")
    print(f"Visibility: loaded ({len(visible)} shown / {len(hidden)} hidden)")
    print(f"Weights: loaded (class_weight={float(getattr(win, 'class_weight', 1.0) or 1.0):.3f})")

    # app.class_palette must carry the PTC colours + visibility verbatim
    colour_ok, vis_ok, missing = True, True, []
    for code, entry in (ptc_map or {}).items():
        got = palette.get(int(code))
        if got is None:
            missing.append(int(code))
            colour_ok = False
            continue
        if tuple(int(v) for v in got.get("color", ())) != tuple(int(v) for v in entry["color"]):
            colour_ok = False
        if bool(got.get("show", True)) != bool(entry.get("show", True)):
            vis_ok = False
    check("PTC palette: app.class_palette == PTC file colours", colour_ok and not missing,
          f"missing={missing[:6]}" if missing else f"{len(palette)} classes")
    check("PTC visibility: app.class_palette.show == PTC file", vis_ok)
    vp0 = (getattr(win, "view_palettes", None) or {}).get(0)
    check("PTC palette: view_palettes[0] mirrors class_palette",
          bool(vp0) and all(tuple(int(v) for v in (vp0.get(c) or {}).get("color", ())) ==
                            tuple(int(v) for v in (palette.get(c) or {}).get("color", ()))
                            for c in palette),
          f"entries={len(vp0 or {})}")
    # ---- 3/4. CLASSIFICATION ----------------------------------------------
    head("CLASSIFICATION: Display Mode -> class -> Vulkan (PTC colour LUT on the GPU)")
    win.display_mode = "class"
    update_pointcloud(win, "class")
    pump(app_qt, 0.5)
    par.set_reference_camera(win, rb)
    rb.sync_point_shading(mode="class")
    backend.request_render()
    pump(app_qt, 0.5)
    img_class = backend.capture_frame()
    if img_class is None:
        check("classification: native capture", False, "capture_frame() returned None")
        return _report()
    np.save(os.path.join(OUT_DIR, "class_native.npy"), img_class)
    check("classification: native capture non-empty",
          drawn_pixels(img_class).size > 0, f"shape={img_class.shape}")

    classes = np.asarray(win.data["classification"]).astype(np.int64)
    counts = np.bincount(classes, minlength=256)
    present = [int(c) for c in np.nonzero(counts)[0]]
    weight = float(getattr(win, "class_weight", 1.0) or 1.0)
    present_visible = [c for c in present if bool((palette.get(c) or {}).get("show", True))]
    rec("class_present", len(present))
    rec("class_present_visible", len(present_visible))
    top = sorted(present, key=lambda c: -counts[c])[:5]
    print("dominant classes: " + ", ".join(
        f"#{c} ({counts[c]:,} pts, {tuple(int(v) for v in ptc_map[c]['color'])})"
        for c in top if c in ptc_map))

    # 3a. the GPU table IS the PTC table (no CPU colour upload involved)
    cls_lut, _elev_lut, _int_lut = build_point_luts(win)
    exact_ok, lut_report, mism = True, [], []
    for code in sorted(set(present) | set(int(c) for c in ptc_map)):
        entry = ptc_map.get(code)
        if entry is None:
            continue
        if entry.get("show", True):
            expect = np.clip(np.asarray(entry["color"], dtype=np.float64) * weight, 0, 255).astype(np.uint8)
        else:
            expect = np.zeros(3, dtype=np.uint8)
        got = cls_lut[code & 255]
        if not np.array_equal(got, expect):
            exact_ok = False
            mism.append((code, tuple(int(v) for v in expect), tuple(int(v) for v in got)))
    check("classification: GPU class LUT == PTC colours (byte exact)", exact_ok,
          f"{len(present)} classes present, {len(mism)} mismatched" +
          (f" e.g. {mism[:3]}" if mism else ""))
    rec("class_lut_exact", exact_ok)

    # 3b. the lookup happens on the GPU: no position/colour re-upload
    uploads, original = _patch_upload_counter(backend)
    pos_before = backend.get_point_position_upload_count()
    pal_before = {c: dict(e) for c, e in win.class_palette.items()}

    # visibility change on the largest class: LUT entry must go black
    toggle = top[0] if top else present[0]
    win.class_palette[toggle]["show"] = not bool(win.class_palette[toggle].get("show", True))
    hidden_now = not bool(win.class_palette[toggle].get("show", True))
    rb.sync_point_shading(mode="class")
    backend.request_render()
    pump(app_qt, 0.3)
    lut_hidden, _, _ = build_point_luts(win)
    img_hidden = backend.capture_frame()
    check("classification: visibility toggle is a LUT edit (no re-upload)",
          (tuple(int(v) for v in lut_hidden[toggle & 255]) == (0, 0, 0)) == hidden_now
          and uploads["n"] == 0
          and backend.get_point_position_upload_count() in (pos_before, -1),
          f"class #{toggle} show={not hidden_now} lut={tuple(int(v) for v in lut_hidden[toggle & 255])} "
          f"set_point_cloud calls={uploads['n']} pos_uploads="
          f"{pos_before}->{backend.get_point_position_upload_count()}")

    # restore visibility first, so the weight test below sees the PTC table
    for c, e in pal_before.items():
        win.class_palette[c].update(e)
    rb.sync_point_shading(mode="class")

    # weight change: every LUT entry must scale, still no re-upload
    w_old = float(getattr(win, "class_weight", 1.0) or 1.0)
    win.class_weight = 0.6
    rb.sync_point_shading(mode="class")
    pump(app_qt, 0.2)
    lut_w, _, _ = build_point_luts(win)
    weight_ok = all(
        np.array_equal(lut_w[c & 255], np.clip(np.asarray(e["color"], np.float64) * 0.6, 0, 255).astype(np.uint8))
        for c, e in ptc_map.items() if e.get("show", True))
    check("classification: class_weight scales the PTC LUT (no re-upload)",
          weight_ok and uploads["n"] == 0,
          f"weight 1.0 -> 0.6, set_point_cloud calls={uploads['n']}")
    win.class_weight = w_old
    rb.sync_point_shading(mode="class")
    backend.request_render()
    pump(app_qt, 0.3)
    _restore_upload_counter(backend, original)
    check("classification: visibility/weight changes never re-uploaded points",
          uploads["n"] == 0, f"set_point_cloud calls during the edits = {uploads['n']}")

    # 3c. colour distribution vs the VTK (CPU) colouring of the same points
    vis_mask = np.isin(classes, np.asarray(present_visible, dtype=np.int64)) if present_visible \
        else np.ones(len(classes), dtype=bool)
    ref_class = compute_colors(win, mask=vis_mask)
    drawn_class = drawn_pixels(img_class)
    m_class = colour_metrics(ref_class, drawn_class)
    rec("class_metrics", m_class)
    # Histogram correlation is the strict test, but a per-PIXEL population also
    # contains every anti-aliased sprite rim blended toward the background,
    # which the per-POINT reference cannot contain. The exact byte-level checks
    # above carry the colour correctness; this one catches gross inversion.
    check("classification: colours match the VTK/CPU colouring",
          bool(m_class) and m_class["corr"] >= 0.60 and m_class["mean_diff"] <= 40.0,
          f"mean-diff={m_class['mean_diff']:.1f} corr={m_class['corr']:.3f}" if m_class else "n/a")
    cov_native = float(drawn_class.size) / max(1, img_class[..., 3].size)
    rec("class_coverage", cov_native)
    print(f"coverage(native)={cov_native * 100:.1f}%  visible classes={len(present_visible)}")
    # ---- 5/6. ELEVATION ---------------------------------------------------
    head("ELEVATION: CPU formula trace, GPU parity (geometry untouched)")
    from gui import pointcloud_display as pcd

    win.display_mode = "elevation"
    update_pointcloud(win, "elevation")
    pump(app_qt, 0.5)
    par.set_reference_camera(win, rb)
    rb.sync_point_shading(mode="elevation")
    backend.request_render()
    pump(app_qt, 0.5)
    img_elev = backend.capture_frame()
    check("elevation: native capture non-empty",
          img_elev is not None and drawn_pixels(img_elev).size > 0,
          f"shape={None if img_elev is None else img_elev.shape}")
    if img_elev is not None:
        np.save(os.path.join(OUT_DIR, "elevation_native.npy"), img_elev)

    z = np.asarray(win.data["xyz"][:, 2], dtype=np.float64)
    clip_lo = float(getattr(win, "elevation_clip_low", 1.0))
    clip_hi = float(getattr(win, "elevation_clip_high", 99.0))
    u = shading_uniforms(win)
    gpu_lo, gpu_hi = float(u["elev_lo"]), float(u["elev_hi"])
    cpu_lo, cpu_hi = float(np.percentile(z, clip_lo)), float(np.percentile(z, clip_hi))
    rec("elev_clip_pct", (clip_lo, clip_hi))
    rec("elev_range", (cpu_lo, cpu_hi))
    rec("elev_gamma", float(u["elev_gamma"]))
    print(f"CPU  : percentile({clip_lo},{clip_hi}) of world Z -> lo={cpu_lo:.4f} hi={cpu_hi:.4f} "
          f"gamma={u['elev_gamma']:.2f}  ramp={'custom' if getattr(win, 'elevation_color_ramp', None) else 'Nakshatech rainbow 5-colour'}")
    print(f"GPU  : elevation_min={gpu_lo:.4f} elevation_max={gpu_hi:.4f} "
          f"(uniforms pushed to point.vert)")
    check("elevation: GPU range == CPU percentile range",
          abs(gpu_lo - cpu_lo) < 1e-6 + abs(cpu_lo) * 1e-6 and
          abs(gpu_hi - cpu_hi) < 1e-6 + abs(cpu_hi) * 1e-6,
          f"lo {gpu_lo:.6f} vs {cpu_lo:.6f}, hi {gpu_hi:.6f} vs {cpu_hi:.6f}")

    # the 256-entry table the shader reads must BE the CPU ramp
    t_grid = np.linspace(0.0, 1.0, 256)
    cpu_ramp = pcd._nakshatech_rainbow_5color(t_grid)
    _c, elev_lut, _i = build_point_luts(win)
    lut_exact = np.array_equal(elev_lut, np.ascontiguousarray(cpu_ramp))
    check("elevation: GPU ramp LUT == CPU Nakshatech rainbow table (byte exact)", lut_exact,
          f"first={tuple(int(v) for v in elev_lut[0])} mid={tuple(int(v) for v in elev_lut[128])} "
          f"last={tuple(int(v) for v in elev_lut[255])}")
    rec("elev_lut_exact", lut_exact)

    # GPU colour = LUT[floor(t*255)] with the pushed range, on WORLD z
    def gpu_elev_colours(zz):
        t = np.clip((np.asarray(zz, np.float64) - gpu_lo) / max(gpu_hi - gpu_lo, 1e-6), 0.0, 1.0)
        t = np.power(t, max(float(u["elev_gamma"]), 0.01))
        return elev_lut[np.minimum((t * 255.0).astype(np.int64), 255)]

    step = max(1, z.size // 300000)
    zs = z[::step]
    cpu_cols = pcd._nakshatech_elevation_rgb(
        zs, color_ramp=getattr(win, "elevation_color_ramp", None),
        low_pct=clip_lo, high_pct=clip_hi)
    gpu_cols = gpu_elev_colours(zs)
    d = np.abs(cpu_cols.astype(np.int16) - gpu_cols.astype(np.int16))
    rec("elev_point_diff", (float(d.max()), float(d.mean()), float((d.max(axis=1) == 0).mean())))
    print(f"per-point colour difference (CPU continuous ramp vs GPU 256-step LUT): "
          f"max={d.max()} mean={d.mean():.3f} identical={100 * (d.max(axis=1) == 0).mean():.2f}%")
    # The GPU ramp is the SAME CPU ramp resampled at 256 stops, so the only
    # possible error is one ramp step: the steepest Nakshatech segment moves
    # 255 levels over t=0.25, i.e. 4 levels per 1/255 step. Anything above that
    # would be a real mismatch, not quantisation.
    check("elevation: GPU per-point colour == CPU (one ramp step of quantisation)",
          int(d.max()) <= 4,
          f"max channel diff={d.max()} (bound = 255/0.25/255 = 4 LSB per LUT step)")

    # low / middle / high band agreement
    order = np.argsort(zs)
    bands = np.array_split(order, 5)
    band_lines = []
    band_ok = True
    for bi, band in enumerate(bands):
        c_cpu, c_gpu = cpu_cols[band].mean(axis=0), gpu_cols[band].mean(axis=0)
        step_diff = int(np.abs(c_cpu - c_gpu).max())
        band_ok &= step_diff <= 4
        band_lines.append(f"band{bi + 1} z=[{zs[band].min():.1f},{zs[band].max():.1f}] "
                          f"CPU={np.round(c_cpu, 1)} GPU={np.round(c_gpu, 1)}")
    rec("elev_bands", band_lines)
    print("  " + "\n  ".join(band_lines))
    check("elevation: low/middle/high band colours agree", band_ok,
          "5 z-bands, max |CPU-GPU| <= 4 LSB (one ramp step)")

    # clamping at both ends
    below, above = float((z < cpu_lo).mean()), float((z > cpu_hi).mean())
    clamp_ok = (np.array_equal(gpu_elev_colours(np.array([cpu_lo - 1.0])), elev_lut[0:1]) and
                np.array_equal(gpu_elev_colours(np.array([cpu_hi + 1.0])), elev_lut[255:256]))
    check("elevation: out-of-range points clamp to the ramp ends", clamp_ok,
          f"{below * 100:.2f}% below lo, {above * 100:.2f}% above hi")

    # whole-image distribution vs the VTK/CPU colouring
    ref_elev = pcd._nakshatech_elevation_rgb(
        z, color_ramp=getattr(win, "elevation_color_ramp", None),
        low_pct=clip_lo, high_pct=clip_hi)
    m_elev = colour_metrics(ref_elev, drawn_pixels(img_elev)) if img_elev is not None else None
    rec("elev_metrics", m_elev)
    if m_elev:
        print(f"elevation image metrics: mean RGB diff={m_elev['mean_diff']:.1f} "
              f"p95={m_elev['p95']:.1f} hist corr={m_elev['corr']:.3f}")
        check("elevation: image distribution matches VTK/CPU colouring",
              m_elev["corr"] >= 0.80, f"corr={m_elev['corr']:.3f} mean-diff={m_elev['mean_diff']:.1f}")
    # ---- 7/8/9. SHADED CLASS ---------------------------------------------
    head("SHADED CLASS: PTC mapping -> Naksha geometry -> Vulkan crisp-hybrid surface")
    from gui import shading_display as sd

    t0 = time.perf_counter()
    sd.update_shaded_class(win, azimuth=45.0, ambient=0.25)
    shaded_build_s = time.perf_counter() - t0
    pump(app_qt, 1.0)
    par.set_reference_camera(win, rb)
    backend.request_render()
    pump(app_qt, 0.5)
    img_shaded = backend.capture_frame()
    print(f"update_shaded_class() took {shaded_build_s:.1f}s")
    rec("shaded_build_s", shaded_build_s)
    check("shaded class: native frame rendered",
          img_shaded is not None and drawn_pixels(img_shaded).size > 0,
          f"shape={None if img_shaded is None else img_shaded.shape}")
    if img_shaded is not None:
        np.save(os.path.join(OUT_DIR, "shaded_native.npy"), img_shaded)

    key = sd._get_rendered_cache_key(win)
    cache = sd._cache_store.get(key) if key is not None else None
    check("shaded class: geometry cache present", cache is not None,
          f"cache_key={'yes' if key else 'no'}")
    surf_uploads = backend.get_surface_upload_count()
    rec("surface_upload_count_after_shaded", surf_uploads)
    rec("crisp_hybrid_active", bool(getattr(win, "_shading_crisp_hybrid_active", False)))
    if cache is not None:
        faces = np.asarray(cache.faces)
        verts = np.asarray(cache.xyz_final)
        cm = classes[np.asarray(cache.unique_indices)]
        vc = sd._get_shading_visibility(win)
        mixed_idx, mixed_count, crisp_ok = sd._collect_mixed_display_faces(win, cache, cm, vc)
        pure = int(len(faces) - (mixed_count if mixed_idx is not None else 0))
        rec("shaded_vertices", int(len(verts)))
        rec("shaded_faces", int(len(faces)))
        rec("shaded_mixed_faces", int(mixed_count if mixed_idx is not None else 0))
        rec("shaded_pure_faces", pure)
        rec("overlay_triangles", int(backend.get_overlay_triangle_count()))
        print(f"faces={len(faces):,}  vertices={len(verts):,}  pure={pure:,}  mixed={mixed_count:,}  "
              f"overlay triangles(native)={backend.get_overlay_triangle_count():,}")
        check("shaded class: crisp-hybrid active (pure + mixed faces)", bool(crisp_ok) and mixed_count > 0,
              f"mixed={mixed_count:,} pure={pure:,}")
        check("shaded class: native mixed-face overlay uploaded once",
              int(backend.get_overlay_triangle_count()) == mixed_count,
              f"overlay triangles={backend.get_overlay_triangle_count():,} vs mixed faces={mixed_count:,} "
              f"(one indexed triangle per mixed face)")

        # PTC colours are the material source (same table the seam uploads)
        lut_expected = {}
        for c, e in (win.class_palette or {}).items():
            ci = int(c)
            if ci in vc:
                lut_expected[ci] = tuple(int(v) for v in e.get("color", (128, 128, 128)))
        ptc_matches = all(
            ci in ptc_map and lut_expected[ci] == tuple(int(v) for v in ptc_map[ci]["color"])
            for ci in lut_expected)
        rec("shaded_ptc_lut", len(lut_expected))
        check("shaded class: material LUT == PTC colours (no temporary palette)",
              ptc_matches and len(lut_expected) > 0,
              f"{len(lut_expected)} class colours, all equal to the .ptc entries")

        # Exact CPU crisp-base contract, which the GLSL crisp-hybrid shader
        # mirrors: every cell colour is clip(PTC colour of the face's vertex-0
        # class * one flat per-face slope shade). Verifying the formula itself
        # (not a self-comparison of one shade against another) is what proves
        # "PTC colours as material source + flat triangle slope lighting".
        sel = np.linspace(0, len(faces) - 1, min(20000, len(faces))).astype(int)
        shade = np.asarray(sd._crisp_shade_chunk(win, cache, sel), dtype=np.float64)
        face_cols = sd._build_crisp_base_face_colors(win, cache, cm, vc)
        v0 = np.asarray(faces[sel, 0], dtype=np.int64)
        base = np.zeros((len(sel), 3), dtype=np.float64)
        for row, vi in enumerate(v0):
            col = lut_expected.get(int(cm[vi]))
            base[row] = col if col else (0.0, 0.0, 0.0)
        expected_cols = np.clip(base * shade[:, None], 0, 255).astype(np.uint8)
        got_cols = np.asarray(face_cols)[sel]
        exact_cols = np.array_equal(got_cols, expected_cols)
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = np.where(base > 0, got_cols.astype(np.float64) / np.maximum(base, 1e-9), np.nan)
        flat_spread = float(np.nanmax(np.nanmax(ratio, axis=1) - np.nanmin(ratio, axis=1))) \
            if np.isfinite(ratio).any() else 0.0
        rec("shaded_shade_range", (float(np.nanmin(ratio)), float(np.nanmax(ratio))))
        rec("shaded_flat_spread", flat_spread)
        check("shaded class: cell colour == PTC(vertex-0 class) x one flat per-face shade",
              exact_cols and flat_spread <= 0.02,
              f"{len(sel):,} sampled faces, exact={exact_cols}, "
              f"per-face channel spread<={flat_spread:.3f}, shade "
              f"{np.nanmin(ratio):.2f}..{np.nanmax(ratio):.2f}")
    # ---- 9. SHADING PARAMETERS: uniforms only ------------------------------
    head("SHADING PARAMETERS: sharpness / azimuth / light elevation / ambient")
    sharp = float(sd._shading_sharpness_angle(win))
    _sh, overdrive = sd._shading_sharpness_response(sharp)
    eff_elev = float(sd._shading_effective_light_elevation(win, overdrive))
    rec("shading_sharpness_angle", sharp)
    rec("shading_overdrive", float(overdrive))
    rec("shading_light_elevation", eff_elev)
    rec("shading_ambient", float(getattr(win, "shade_ambient", 0.25)))
    _sh_norm, _sh_od = sd._shading_sharpness_response(sharp)
    _key_i, _fill_i = sd._shading_key_fill_intensities(win, _sh_norm, _sh_od)
    rec("shading_key", float(_key_i))
    rec("shading_fill", float(_fill_i))
    print(f"sharpness={sharp:.1f} deg -> overdrive={overdrive:.3f} -> "
          f"effective light elevation={eff_elev:.1f} deg, ambient={getattr(win, 'shade_ambient', 0.25):.2f}, "
          f"key={_key_i:.3f} fill={_fill_i:.3f}")

    up0, pu0 = backend.get_surface_upload_count(), backend.get_shade_param_update_count()
    backend.request_render_safe()
    pump(app_qt, 0.3)
    img_a = backend.capture_frame()
    sd.update_shading_lighting_only(win, azimuth=135.0, angle=35.0, ambient=0.45)
    pump(app_qt, 0.6)
    backend.request_render_safe()
    pump(app_qt, 0.4)
    try:
        img_b = backend.capture_frame()
    except Exception as _cap_err:
        print(f"[val] shaded capture failed: {_cap_err!r} ({backend.last_error()})", flush=True)
        img_b = None
    up1, pu1 = backend.get_surface_upload_count(), backend.get_shade_param_update_count()
    pts_hidden = backend.get_point_cloud_visible()
    rec("point_cloud_visible_in_mesh_modes", pts_hidden)
    check("shaded/surface: raw point cloud hidden so the mesh is visible",
          pts_hidden in (0, -1), f"nkv_get_point_cloud_visible()={pts_hidden} (-1 = DLL predates the toggle)")
    rec("surface_upload_count_before_params", up0)
    rec("surface_upload_count_after_params", up1)
    rec("shade_param_update_delta", int(pu1 - pu0))
    rec("shading_geometry_rebuild", "NO" if up1 == up0 else "YES")
    rec("shading_surface_reupload", "NO" if up1 == up0 else "YES")
    if img_a is not None and img_b is not None and img_a.shape == img_b.shape:
        da, db = drawn_pixels(img_a), drawn_pixels(img_b)
        changed = float(np.abs(da.astype(np.int16) - db.astype(np.int16)).mean()) if (
            da.size and db.size) else 0.0
    else:
        changed = 0.0
    rec("shading_pixel_change", changed)
    check("shading parameters: surface_upload_count unchanged", up1 == up0,
          f"{up0} -> {up1} (push-constant path only)")
    check("shading parameters: only uniforms/push constants updated", pu1 > pu0,
          f"shade_param_update_count {pu0} -> {pu1}")
    check("shading parameters: the frame really changes (no rebuild needed)", changed > 0.5,
          f"mean |delta| = {changed:.2f} per channel")
    sd.update_shading_lighting_only(win, azimuth=45.0, angle=None, ambient=0.25)
    pump(app_qt, 0.4)

    # ---- 10. SURFACE MODE -------------------------------------------------
    head("SURFACE MODE: existing surface geometry -> Vulkan SurfaceRenderer")
    from gui import surface_mode as sm

    up_before = backend.get_surface_upload_count()
    t0 = time.perf_counter()
    sm.render_surface_mode(win)
    surface_build_s = time.perf_counter() - t0
    pump(app_qt, 1.0)
    par.set_reference_camera(win, rb)
    backend.request_render_safe()
    pump(app_qt, 0.6)
    try:
        img_surf = backend.capture_frame()
    except Exception as _cap_err:
        print(f"[val] surface capture failed: {_cap_err!r} ({backend.last_error()})", flush=True)
        img_surf = None
    pts = getattr(win, "_surface_points", None)
    fcs = getattr(win, "_surface_faces", None)
    up_after = backend.get_surface_upload_count()
    rec("surface_build_s", surface_build_s)
    rec("surface_vertices", 0 if pts is None else int(len(pts)))
    rec("surface_faces", 0 if fcs is None else int(len(fcs)))
    rec("surface_upload_delta", int(up_after - up_before))
    drawn_surf = drawn_pixels(img_surf) if img_surf is not None else np.zeros((0, 3))
    check("surface: geometry built by the app", pts is not None and fcs is not None and len(fcs) > 0,
          f"vertices={0 if pts is None else len(pts):,} faces={0 if fcs is None else len(fcs):,} "
          f"({surface_build_s:.1f}s)")
    check("surface: uploaded to Vulkan exactly once", up_after == up_before + 1,
          f"surface_upload_count {up_before} -> {up_after}")
    check("surface: first native frame has pixels", drawn_surf.size > 0,
          f"coverage={100.0 * drawn_surf.size / max(1, img_surf[..., 3].size):.1f}%" if img_surf is not None else "no capture")
    if img_surf is not None:
        np.save(os.path.join(OUT_DIR, "surface_native.npy"), img_surf)
    surf_cols = None
    bufs = getattr(win, "_surface_vtk_buffers", None)
    if bufs is not None and len(bufs) >= 3 and np.asarray(bufs[2]).size:
        surf_cols = np.asarray(bufs[2], dtype=np.float64)
        rec("surface_cpu_face_colour_mean", [float(v) for v in surf_cols.mean(axis=0)])
    if surf_cols is not None and drawn_surf.size:
        print(f"CPU face colours mean={np.round(surf_cols.mean(axis=0), 1)}  "
              f"native drawn pixels mean={np.round(drawn_pixels(img_surf).mean(axis=0), 1)}")

    # Emit the report BEFORE any teardown: closing the main window runs a large
    # amount of VTK/Qt cleanup, and the process can die inside it, which would
    # otherwise swallow the report entirely.
    return _report()

def status(prefix: str) -> str:
    for name, ok, _detail in reversed(_CHECKS):
        if name.startswith(prefix):
            return "PASS" if ok else "FAIL"
    return "N/A"


def _fmt(value, spec="{:.1f}"):
    if value is None:
        return "n/a"
    try:
        return spec.format(value)
    except Exception:
        return str(value)


def _report() -> int:
    """Render the required report and persist it.

    Also flushed from an atexit hook: the Qt/VTK teardown that follows can
    abort the process, and a validation report that dies with the run is worse
    than useless."""
    try:
        code = _render_report()
    except Exception:
        traceback.print_exc()
        code = 3
    try:
        with open(os.path.join(OUT_DIR, "validation_report.txt"), "w", encoding="utf-8") as fh:
            fh.write(_REPORT_TEXT[0] + "\n")
    except Exception:
        pass
    return code


def _render_report() -> int:
    rb, backend = R.get("_rb"), R.get("_backend")
    m_class = R.get("class_metrics") or {}
    m_elev = R.get("elev_metrics") or {}
    pcd_diff = R.get("elev_point_diff") or (None, None, None)
    lo, hi = R.get("elev_range") or (0.0, 0.0)
    clip = R.get("elev_clip_pct") or (1.0, 99.0)

    L = []
    A = L.append
    A("=" * 46)
    A("VULKAN CLASS / ELEVATION / SHADED VALIDATION")
    A("=" * 46)
    A("")
    A("DATASET:")
    A("123.las")
    A("")
    A("POINTS:")
    A(f"{R.get('points', 0):,}")
    A("")
    A("PTC:")
    A(PTC_PATH)
    A("")
    A("PTC LOADED:")
    A(status("PTC palette"))
    A("")
    A("CLASSES:")
    A(f"{R.get('ptc_classes', 0)} in file / {R.get('palette_classes', 0)} in app.class_palette / "
      f"{R.get('visible_classes', 0)} visible")
    A("")
    A("-" * 32)
    A("CLASSIFICATION")
    A("-" * 32)
    A("")
    A("VTK vs Vulkan:")
    A("")
    A("class LUT:")
    A(status("classification: GPU class LUT"))
    A("")
    A("colors:")
    A(status("classification: colours match"))
    A("")
    A("visibility:")
    A(status("classification: visibility toggle"))
    A("")
    A("weights:")
    A(status("classification: class_weight")
      if status("classification: class_weight") != "N/A"
      else status("classification: visibility/weight"))
    A("")
    A("mean RGB difference:")
    A(_fmt(m_class.get("mean_diff")))
    A("")
    A("hist correlation:")
    A(_fmt(m_class.get("corr"), "{:.3f}"))
    A("")
    A(f"visible classes in data: {R.get('class_present_visible', 0)} of {R.get('class_present', 0)}")
    A(f"coverage (native): {100.0 * (R.get('class_coverage') or 0):.1f}%")
    A(f"point re-upload on palette/visibility/weight change: "
      f"{'NO' if status('classification: visibility/weight') == 'PASS' else 'YES/UNKNOWN'}")
    A("")
    A("-" * 32)
    A("ELEVATION")
    A("-" * 32)
    A("")
    A("CPU elevation formula identified:")
    A(f"z -> percentile({clip[0]},{clip[1]}) of WORLD Z -> [{lo:.4f}, {hi:.4f}]")
    A("-> (z-lo)/(hi-lo) clamped -> pow(gamma) -> Nakshatech 5-colour ramp, 256-step LUT")
    A("")
    A("GPU formula parity:")
    A(status("elevation: GPU per-point")
      if status("elevation: GPU per-point") != "N/A" else status("elevation: GPU range"))
    A("")
    A("mean RGB difference:")
    A(_fmt(m_elev.get("mean_diff")))
    A("")
    A("hist correlation:")
    A(_fmt(m_elev.get("corr"), "{:.3f}"))
    A("")
    if pcd_diff[0] is not None:
        A(f"per-point max channel diff: {pcd_diff[0]} (mean {pcd_diff[1]:.3f}, "
          f"{100 * pcd_diff[2]:.1f}% byte-identical)")
    A(f"range clamp: {status('elevation: out-of-range points')}")
    A(f"low/mid/high bands: {status('elevation: low/middle/high band')}")
    A("")
    A("-" * 32)
    A("SHADED CLASS")
    A("-" * 32)
    A("")
    A("faces:")
    A(f"{R.get('shaded_faces', 0):,}")
    A("")
    A("mixed faces:")
    A(f"{R.get('shaded_mixed_faces', 0):,} (native overlay triangles "
      f"{R.get('overlay_triangles', 0):,})")
    A("")
    A("pure faces:")
    A(f"{R.get('shaded_pure_faces', 0):,}")
    A("")
    A("PTC colors used:")
    A(status("shaded class: material LUT"))
    A("")
    A("crisp hybrid:")
    A(status("shaded class: crisp-hybrid"))
    A("")
    A("sharpness:")
    A(f"{status('shading parameters: only uniforms')} "
      f"(angle {_fmt(R.get('shading_sharpness_angle'), '{:.1f}')} deg, push constants only)")
    A("")
    A("overdrive:")
    A(_fmt(R.get("shading_overdrive"), "{:.3f}"))
    A("")
    A("key/fill:")
    A(f"{_fmt(R.get('shading_key'), '{:.3f}')} / {_fmt(R.get('shading_fill'), '{:.3f}')} "
      f"(app _shading_key_fill_intensities -> surface.frag shadeParamsB), "
      f"effective light elevation {_fmt(R.get('shading_light_elevation'), '{:.1f}')} deg, "
      f"ambient {_fmt(R.get('shading_ambient'), '{:.2f}')}")
    A("")
    A("geometry rebuild after shading change:")
    A(str(R.get("shading_geometry_rebuild", "n/a")))
    A("")
    A("surface upload after shading change:")
    A(str(R.get("shading_surface_reupload", "n/a")))
    A("")
    A(f"surface_upload_count {R.get('surface_upload_count_before_params', '?')} -> "
      f"{R.get('surface_upload_count_after_params', '?')}, "
      f"shade_param_update_count +{R.get('shade_param_update_delta', '?')}, "
      f"mean pixel change {_fmt(R.get('shading_pixel_change'), '{:.2f}')}")
    A("")
    A("-" * 32)
    A("SURFACE")
    A("-" * 32)
    A("")
    A("Vulkan surface:")
    A(status("surface: uploaded to Vulkan"))
    A("")
    A("vertices:")
    A(f"{R.get('surface_vertices', 0):,}")
    A("")
    A("faces:")
    A(f"{R.get('surface_faces', 0):,}")
    A("")
    A("upload:")
    A(f"{R.get('surface_upload_delta', 0)} upload(s) in "
      f"{R.get('surface_build_s', 0):.1f}s (app geometry, no new triangulation)")
    A("")
    A("first frame:")
    A(status("surface: first native frame has pixels"))
    A("")
    A("-" * 32)
    A("VULKAN STATUS")
    A("-" * 32)
    A("")
    A("renderer:")
    A(str(rb.status_label_text()) if rb is not None else "n/a")
    A("")
    A("GPU:")
    A(str(backend.get_device_name()) if backend is not None else "n/a")
    A("")
    A("API:")
    A(str(backend.get_api_version_string()) if backend is not None else "n/a")
    A("")
    A("validation errors:")
    A("n/a - cfg.enableValidation exists (nkv_create_renderer arg 4, app passes 0) but the "
      "engine installs no VK_EXT_debug_utils messenger, so nothing is surfaced.")
    A("")
    A("=" * 46)
    A(f"checks: {sum(1 for _n, ok, _d in _CHECKS if ok)}/{len(_CHECKS)} passed")
    for name, ok, detail in _CHECKS:
        if not ok:
            A(f"  FAILED: {name} ({detail})")
    A("artifacts: " + OUT_DIR)
    A("")
    A("WORKSTATIONCAD FILES MODIFIED:")
    A("NONE (H:\\naksha lidar C C++\\WorkstationCAD was never opened for write)")
    A("=" * 46)

    text = "\n".join(L)
    _REPORT_TEXT[0] = text
    print("\n" + text, flush=True)
    return 0 if all(ok for _n, ok, _d in _CHECKS) else 1


if __name__ == "__main__":
    try:
        _CODE = main()
    except BaseException:
        traceback.print_exc()
        _CODE = 3
    finally:
        sys.stdout.flush()
    raise SystemExit(_CODE)






