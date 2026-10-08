# [SURFACE PIPELINE] telemetry smoke driver (read-only observation).
# Usage: python surface_pipeline_report.py <las-or-laz> [quality]
import os, sys, time
os.environ.setdefault("NAKSHA_RENDER_BACKEND", "vulkan")
os.environ.setdefault("NAKSHA_VULKAN_DISABLE_ALL_VTK_RENDER", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtWidgets import QApplication
import gui.surface_mode as sm

def main():
    path = sys.argv[1]
    quality = sys.argv[2] if len(sys.argv) > 2 else "normal"
    app = QApplication.instance() or QApplication([])

    import laspy
    las = laspy.read(path)
    xyz = np.column_stack([np.asarray(las.x, dtype=np.float64),
                           np.asarray(las.y, dtype=np.float64),
                           np.asarray(las.z, dtype=np.float64)])
    print(f"DRIVER loaded points={len(xyz):,}", flush=True)

    t0 = time.perf_counter()
    res = sm._compute_surface_geometry_backend(
        xyz, np.arange(len(xyz), dtype=np.int64), 0.0,
        sm._surface_quality_target(quality, len(xyz)),
        0.0, 45.0, 45.0, 0.22, None, quality_mode=quality)
    # SECONDS, matching profile_timings and the note functions.
    wall = time.perf_counter() - t0

    sm._surface_pipeline_note(input_points=len(xyz))
    if os.environ.get("SP_PREVIEW") == "1":
        # Exercise the REAL stage-1 path: same subsample helper, same target
        # math, same backend, so the measured time is the shipped cost.
        from PySide6.QtCore import QCoreApplication
        _final_target = sm._surface_quality_target(quality, len(xyz))
        _pt = sm._surface_preview_target(_final_target)
        _sub_xyz, _sub_idx = sm._surface_preview_subsample(
            xyz, np.arange(len(xyz), dtype=np.int64), _pt)
        _pt = sm._surface_quality_target(quality, len(_sub_idx))
        sm._surface_pipeline_note(preview_subsampled=len(_sub_idx))
        _pv = sm._compute_surface_geometry_backend(
            _sub_xyz, np.arange(len(_sub_idx), dtype=np.int64), 0.0, _pt, 0.0,
            45.0, 45.0, 0.22, None, quality_mode=quality)
        QCoreApplication.processEvents()
        if not _pv.get("empty"):
            _pt_tm = _pv.get("profile_timings") or {}
            sm._surface_pipeline_note(
                preview_points=len(_pv["points"]),
                preview_triangles=len(_pv["faces"]),
                preview_s=float(_pt_tm.get("total_backend", 0.0)))
            print("\n--- SURFACE PREVIEW STAGE TIMINGS ---")
            for k, v in sorted(_pt_tm.items(), key=lambda kv: -kv[1])[:10]:
                print(f"   {k:<28} {v * 1000.0:>9.1f} ms")
            print(f"DRIVER preview_target={_pt:,} final_target={_final_target:,} "
                  f"subsampled={len(_sub_idx):,}")

    sm._surface_pipeline_note_result(None, res, wall_s=wall)
    sm.print_surface_pipeline_report()

    if not res.get("empty"):
        f = res.get("faces")
        print(f"DRIVER verify faces={0 if f is None else len(f):,} "
              f"points={len(res.get('points')):,}")
    print("DRIVER DONE")

if __name__ == "__main__":
    main()
