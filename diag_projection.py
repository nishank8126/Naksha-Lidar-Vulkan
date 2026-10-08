"""Find where a projected face ACTUALLY lands in the captured frame.

The harness projects face centroids with the engine's own MVP and reads that
pixel. The gate misses show background black there, so either the projection
is systematically misaligned or the faces are occluded. This brute-forces the
answer: for a handful of large faces it searches the WHOLE captured normal
stage for pixels whose value equals that face's expected normal encoding, and
reports the offset between predicted and actual screen position.
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
    if backend is None:
        print("no vulkan backend")
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
    cache = sd._cache_store.get(sd._get_rendered_cache_key(win))
    classes = np.asarray(win.data["classification"]).astype(np.int64)

    # Default whole-cloud camera: this is the pose that renders 23% lit.
    par.set_reference_camera(win, rb)
    backend.request_render()
    pump(app_qt, 0.5)

    mvp = T.get_last_mvp(backend)
    origin = T.get_render_origin(backend)
    print("mvp:\n", np.round(mvp, 6), flush=True)
    print("origin:", origin, flush=True)

    W, H = 1400, 841
    rng = np.random.default_rng(3)
    n_faces = int(len(np.asarray(cache.faces)))

    # Faces that are LARGE on screen (relax MIN_AREA_PX so we get candidates).
    probe = rng.choice(n_faces, size=20000, replace=False)
    areas = T.projected_face_areas(cache, probe, mvp, origin, W, H)
    order = np.argsort(-np.where(np.isfinite(areas), areas, -1))
    big = probe[order[:400]]

    ctx = T.build_context(win, sd, cache, classes)
    img1 = T.capture_stages(backend, app_qt, pump)[1]
    lit = img1.max(axis=2) > 8
    print("lit fraction:", float(lit.mean()), flush=True)

    found = 0
    for face in big[:60]:
        s = {"face": int(face)}
        tris = np.asarray(cache.xyz_final)[np.asarray(cache.faces)[face]]
        sx, sy, w = T.project_points(tris, mvp, origin, W, H)
        if not (np.all(np.isfinite(sx)) and np.all(w > 0)):
            continue
        px = int(np.floor(float(sx.mean())))
        py = int(np.floor(float(sy.mean())))
        if not (0 <= px < W and 0 <= py < H):
            continue
        area = 0.5 * abs((sx[1] - sx[0]) * (sy[2] - sy[0])
                         - (sx[2] - sx[0]) * (sy[1] - sy[0]))
        if area < 200:
            continue

        n = np.asarray(cache.face_normals[face], dtype=np.float64)
        exp = np.floor(np.clip(n * 0.5 + 0.5, 0.0, 1.0) * 255.0 + 1e-4)
        exp_i = exp.astype(np.int64)

        # brute-force search for this exact normal anywhere in the frame
        match = (np.abs(img1[..., 0].astype(np.int64) - exp_i[0]) <= 2) & \
                (np.abs(img1[..., 1].astype(np.int64) - exp_i[1]) <= 2) & \
                (np.abs(img1[..., 2].astype(np.int64) - exp_i[2]) <= 2)
        ys, xs = np.nonzero(match)
        print(f"\nface {face}: pred=({px},{py}) area={area:.0f}px^2 "
              f"exp={exp.astype(int).tolist()} gpu_at_pred={img1[py, px].tolist()} "
              f"matches_found={ys.size}", flush=True)
        if ys.size:
            mx, my = int(xs.mean()), int(ys.mean())
            print(f"   centroid of matches=({mx},{my}) "
                  f"delta=({mx - px:+d},{my - py:+d})", flush=True)
            found += 1
        if found >= 6:
            break
    print("\nfaces located:", found, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
