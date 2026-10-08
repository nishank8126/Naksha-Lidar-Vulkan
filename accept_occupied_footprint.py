import json
from pathlib import Path
import numpy as np
import perf_camera_phase4 as P
from naksha_lod_gate import Camera2D
from gui.naksha_cache.visual_quality import quality_map
from gui.naksha_cache.occupancy_validation import validate_occupancy


def main():
    manager, _, cx, cy, half = P.build()
    # Replay prior zoom-in, then reverse into extreme zoom-out. Preserve the
    # residency/hysteresis history that produced the accepted failing frame.
    worst = None
    for name, scales in (("zoom_in", [half*.965**i for i in range(100)]),
                         ("zoom_out", [half*.03*1.04**i for i in range(100)])):
        for frame, hw in enumerate(scales):
            cam = Camera2D(cx, cy, hw, P.W, P.H)
            viewport = (cx, cy, hw, hw*P.H/P.W)
            manager.note_camera_motion()
            manager.on_frame(cam, viewport)
            diagnostic = quality_map(manager, viewport, P.W, P.H)
            if name == "zoom_out" and (worst is None or diagnostic["starved_cells"] > worst[0]):
                worst = (diagnostic["starved_cells"], frame, viewport,
                         tuple(manager.active_draw_keys), diagnostic, manager.density.last_target)
    _, frame, viewport, keys, diagnostic, target = worst
    manager.active_draw_keys = keys
    manager.density.last_target = target
    report = validate_occupancy(manager, viewport, P.W, P.H, diagnostic)
    report["frame"] = frame
    report["frontier"] = dict(manager.last_screen_space_diag)
    report["budget"] = int(manager.lod.budget)
    report["pass"] = report["occupied_starved_cells"] == 0
    path = Path("diagnostics/visual_continuity/occupied_footprint.json")
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k not in ("source_counts", "drawn_counts", "occupied_area_pixels")}), flush=True)
    manager.close()
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
