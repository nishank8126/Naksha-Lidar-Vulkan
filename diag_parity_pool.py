"""Diagnose the visible_face_pool returning 0 despite large projected faces.

Runs the same setup as the parity harness (LAS -> PTC -> shaded_class), then
projects the WHOLE mesh and reports the distribution of screen x, screen y and
area, plus what the capture actually contains. Answers: is the projection
wrong, is the pool filter wrong, or is the engine drawing something else?
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["NAKSHA_RENDER_BACKEND"] = "vulkan"
os.environ["NAKSHA_VULKAN_MAIN_VIEWPORT"] = "1"
os.environ["NAKSHA_VULKAN_PREVIEW"] = ""

import vulkan_shading_parity_test as T  # noqa: E402

from PySide6.QtWidgets import QApplication  # noqa: E402


def pct(a, ps=(0, 1, 5, 25, 50, 75, 95, 99, 100)):
    a = np.asarray(a, dtype=np.float64)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return "  (empty)"
    return "  ".join(f"p{p}={np.percentile(a, p):.1f}" for p in ps)


def main():
    from gui.app_window import NakshaApp
    from gui.dialogs.load_pointcloud_dialog import DEFAULT_IMPORT_OPTIONS
    from vulkan_engine_test import _suppress_modal_dialogs, pump, wait_until
    import vulkan_parity_check as par

    _suppress_modal_dialogs()
    app_qt = QApplication.instance() or QApplication(sys.argv[:1])
    win = NakshaApp()
    win.resize(1400, 900)
    win.show()
    pump(app_qt, 1.0)

    rb = getattr(win, "render_backend", None)
    backend = getattr(rb, "vulkan_backend", None)
    print("backend:", backend is not None, getattr(rb, "state", None), flush=True)
    if backend is None:
        return 1

    win.open_file(filenames=[T.LAS_PATH],
                  import_options=dict(DEFAULT_IMPORT_OPTIONS), prompt_import=False)
    wait_until(app_qt, lambda: (getattr(win, "data", None) is not None
                                and win.data.get("xyz") is not None
                                and getattr(win, "_file_loader_worker", None) is None),
               timeout=900.0)
    pump(app_qt, 1.0)
    win._load_single_ptc(T.PTC_PATH)
    wait_until(app_qt, lambda: bool(getattr(win, "class_palette", None)), timeout=60.0)
    win.set_display_mode("shaded_class")
    wait_until(app_qt, lambda: rb.active_render_mode == "shaded_class"
               and backend.get_surface_upload_count() > 0, timeout=900.0)

    from gui import shading_display as sd
    key = sd._get_rendered_cache_key(win)
    cache = sd._cache_store.get(key)
    print("faces:", len(cache.faces), flush=True)

    par.set_reference_camera(win, rb)
    backend.request_render()
    pump(app_qt, 0.5)

    rng = np.random.default_rng(1)
    W, H = 1400, 841
    mvp, origin, med, zoom = T.frame_for_sampling(win, rb, backend, cache, rng, W, H)
    print("mvp row0:", np.round(mvp[0], 6), flush=True)
    print("mvp row3:", np.round(mvp[3], 6), flush=True)
    print("origin:", origin, flush=True)
    print("median area:", med, " zoom:", zoom, flush=True)

    # ---- project EVERYTHING, report the distribution ----------------------
    faces = np.asarray(cache.faces)
    xyz = np.asarray(cache.xyz_final)
    print("xyz_final bbox:", np.round(xyz.min(axis=0), 2),
          np.round(xyz.max(axis=0), 2), flush=True)

    step = 40
    sub = faces[::step]
    tris = xyz[sub]
    sx, sy, w = T.project_points(tris.reshape(-1, 3), mvp, origin, W, H)
    sx = sx.reshape(-1, 3); sy = sy.reshape(-1, 3); w = w.reshape(-1, 3)
    x0, x1, x2 = sx[:, 0], sx[:, 1], sx[:, 2]
    y0, y1, y2 = sy[:, 0], sy[:, 1], sy[:, 2]
    area = 0.5 * np.abs((x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0))
    px = (x0 + x1 + x2) / 3.0
    py = (y0 + y1 + y2) / 3.0

    print(f"\nsampled {len(sub):,} of {len(faces):,} faces (every {step})", flush=True)
    print("w>0 frac      :", float(np.mean(np.all(w > 0, axis=1))), flush=True)
    print("finite frac   :", float(np.mean(np.all(np.isfinite(sx), axis=1))), flush=True)
    print("screen x      :", pct(px), flush=True)
    print("screen y      :", pct(py), flush=True)
    print("area px^2     :", pct(area), flush=True)
    in_box = ((px >= T.EDGE_MARGIN) & (px < W - T.EDGE_MARGIN) &
              (py >= T.EDGE_MARGIN) & (py < H - T.EDGE_MARGIN))
    print("in-box frac   :", float(np.mean(in_box)), flush=True)
    print("in-box&area>=40:", float(np.mean(in_box & (area >= T.MIN_AREA_PX))), flush=True)
    print("=> est visible pool:", int(np.sum(in_box & (area >= T.MIN_AREA_PX)) * step), flush=True)

    # ---- what is actually on screen? --------------------------------------
    imgs = T.capture_stages(backend, app_qt, pump)
    for stage in (0, 1, 3):
        a = imgs[stage]
        lit = a.max(axis=2) > 8
        ys, xs = np.nonzero(lit)
        print(f"\nstage {stage}: lit frac={lit.mean():.4f}", flush=True)
        if ys.size:
            print("   lit x range:", int(xs.min()), int(xs.max()),
                  " lit y range:", int(ys.min()), int(ys.max()), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
