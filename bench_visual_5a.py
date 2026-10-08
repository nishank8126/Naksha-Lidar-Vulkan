"""bench_visual_5a.py - PHASE 5A measurements that decide defaults.

  1. HYSTERESIS (Part 13): rung changes + range pushes along a slowly
     oscillating zoom and a wiggling pan on the real 27M cache, with the uniform
     allocator's dead-band at 0 / 0.08 / 0.15 / 0.25.
  2. PAN MARGIN (Part 19): 0 / 3 / 6 / 10 % on the STREAMING path (where an
     unready edge can actually show): visible nodes bare at the edge, requests,
     selector derivations, range pushes, adjacent-PPP continuity.

    py bench_visual_5a.py hyst | margin
"""
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))


def hysteresis():
    os.environ.setdefault("NAKSHA_BENCH_SRC", os.path.join(
        ROOT, "v2cache_r04_l7", "test_classified_highprecision.laz"))
    import perf_camera_phase4 as P
    from naksha_lod_gate import Camera2D
    print("HYSTERESIS on", os.path.basename(os.path.dirname(os.environ["NAKSHA_BENCH_SRC"])))
    print(f"{'dead-band':>10}{'rung changes':>14}{'per frame':>11}{'range pushes':>14}")
    for h in ("0", "0.08", "0.15", "0.25"):
        os.environ["NAKSHA_LOD_UNIFORM_HYST"] = h
        mgr, ad, cx, cy, half = P.build()
        W, H = P.W, P.H
        prev = {}
        changes = 0
        frames = 0
        pushes0 = mgr.draw_range_pushes
        for path in (
                [(cx, cy, half * (0.25 * (1.0 + 0.12 * np.sin(i / 4.0))))
                 for i in range(160)],                       # slow zoom wobble
                [(cx + half * 0.15 * np.sin(i / 5.0),
                  cy + half * 0.05 * np.cos(i / 7.0), half * 0.3)
                 for i in range(160)]):                      # pan wiggle
            for (px, py, hw) in path:
                cam = Camera2D(px, py, hw, W, H)
                mgr.note_camera_motion()
                mgr.on_frame(cam, (px, py, hw, hw * H / W))
                cur = {k[0]: k[1] for k in mgr.active_draw_keys}
                for n, l in cur.items():
                    if n in prev and prev[n] != l:
                        changes += 1
                prev = cur
                frames += 1
        print(f"{h:>10}{changes:>14}{changes / frames:>11.2f}"
              f"{mgr.draw_range_pushes - pushes0:>14}")


def margin():
    import _streaming_helpers as S
    import accept_visual_lod_continuity as V
    from gui.naksha_cache import visual_quality as VQ
    print("PAN MARGIN on the STREAMING rig (1.5M pts, 20 MB GPU)")
    print(f"{'margin':>7}{'bare edge frames':>18}{'new reads':>11}{'selector runs':>15}"
          f"{'range pushes':>14}{'adjPPP p95':>12}{'holes':>7}")
    for m in ("0", "0.03", "0.06", "0.10"):
        os.environ["NAKSHA_LOD_PAN_MARGIN"] = m
        mgr, ad, r = S.open_streaming(gpu_mb=20)
        cam, vp = S.view(500, 500, 500)
        S.pump(mgr, cam, vp, frames=140)
        cam, vp = S.view(300, 400, 125)
        S.pump(mgr, cam, vp, frames=120)
        reads0 = mgr.reader.stats["blocks_read"]
        runs0 = mgr.lod_stats["selector_runs"]
        push0 = mgr.draw_range_pushes
        bare, p95s, holes = 0, [], 0
        for i in range(160):                      # steady pan across the survey
            cx, cy = 300 + i * 2.2, 400 + i * 1.2
            cam, vp = S.view(cx, cy, 125)
            mgr.note_camera_motion()
            mgr.on_frame(cam, vp)
            time.sleep(0.004)
            if mgr.screen_space_uncovered > 0:
                bare += 1
            qm = VQ.quality_map(mgr, vp, S.W, S.H)
            holes = max(holes, qm["empty_cells"])
            if np.isfinite(qm["p95_adjacent_ppp_ratio"]):
                p95s.append(qm["p95_adjacent_ppp_ratio"])
        print(f"{float(m) * 100:>6.0f}%{bare:>18}"
              f"{mgr.reader.stats['blocks_read'] - reads0:>11}"
              f"{mgr.lod_stats['selector_runs'] - runs0:>15}"
              f"{mgr.draw_range_pushes - push0:>14}"
              f"{(np.mean(p95s) if p95s else float('nan')):>12.2f}{holes:>7}")
        S.close_streaming(mgr, r)


if __name__ == "__main__":
    {"hyst": hysteresis, "margin": margin}[sys.argv[1] if len(sys.argv) > 1 else "hyst"]()
