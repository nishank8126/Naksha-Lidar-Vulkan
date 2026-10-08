"""accept_visual_continuity_5a.py - PHASE 5A acceptance on the REAL 27M dataset.

Per view (Fit, x2, x4, x8, x16) and state (MOVING / IDLE) it prints the quality-
map metrics of the committed frontier, then walks slow-pan / fast-pan / zoom
paths reporting the WORST frame, and writes diagnostic PNGs.

    NAKSHA_BENCH_SRC=<project path>  py accept_visual_continuity_5a.py
    NAKSHA_LOD_ALLOCATOR=share        (A/B against the accepted allocator)

DEV ONLY: quality_map is O(nodes x grid); never run from the GUI tick.
"""
import os
import json
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from gui.naksha_cache import visual_quality as VQ                  # noqa: E402
import accept_visual_lod_continuity as V                           # noqa: E402

OUT = os.path.join(ROOT, "diagnostics", "visual_continuity")


def row(label, state, qm, sm):
    return (f"{label:<9}{state:<7}"
            f"empty={qm['empty_cells']:>3} starved={qm['starved_cells']:>3} "
            f"over={qm['overdense_cells']:>3} dup={qm['duplicate_cover_cells']:>3} "
            f"| comp e/s/o={qm['largest_empty_component']}/"
            f"{qm['largest_starved_component']}/{qm['largest_overdense_component']} "
            f"| adjPPP max={qm['max_adjacent_ppp_ratio']:.2f} "
            f"p95={qm['p95_adjacent_ppp_ratio']:.2f} "
            f"rep max={qm['max_adjacent_representation_ratio']:.2f} "
            f"| LODbdry={qm['lod_boundary_count']} seams={qm['visible_tile_seam_count']} "
            f"| node dLOD max={sm['lod_diff_max']} spacing p95={sm['spacing_ratio_p95']:.2f}")


def main():
    frame_reports = []
    timings = {}
    import perf_camera_phase4 as P
    from naksha_lod_gate import Camera2D
    os.makedirs(OUT, exist_ok=True)
    mgr, adapter, cx, cy, half = P.build()
    W, H = P.W, P.H
    try:
        from gui import phase4b_latency_probe as PR
        det = PR.LatencyProbe().detect()
        print(f"[VISIBLE_BACKEND] headless harness; Vulkan main viewport active: "
              f"{'YES' if det.get('vulkan_main_viewport_active') else 'NO'} "
              f"(this run measures the stream manager's frontier, NOT pixels)")
    except Exception:
        print("[VISIBLE_BACKEND] headless harness (no GUI)")
    lay = getattr(mgr, "cache_layout", {})
    print(f"[CACHE LAYOUT] overlap_ratio={lay.get('overlap_ratio', float('nan')):.2f} "
          f"wide={lay.get('wide_fraction', float('nan')) * 100:.0f}% "
          f"legacy={lay.get('legacy_layout')} "
          f"ladder_step={lay.get('ladder_spacing_step', float('nan')):.2f}x "
          f"stale={lay.get('stale_ladder')}  allocator={mgr._lod_allocator()} "
          f"hyst={mgr._uniform_hysteresis()}")
    print("\n[STATIC VIEWS]")
    for label, f in (("fit", 1.0), ("x2", 0.5), ("x4", 0.25), ("x8", 0.125),
                     ("x16", 0.0625)):
        for state, moving in (("MOVING", True), ("IDLE", False)):
            cam = Camera2D(cx, cy, half * f, W, H)
            vp = (cx, cy, half * f, half * f * H / W)
            if moving:
                mgr.note_camera_motion()
                mgr.on_frame(cam, vp)
            else:
                for _ in range(40):
                    mgr.on_frame(cam, vp)
                    if getattr(mgr, "_camera_state", "") == "IDLE":
                        break
                    time.sleep(0.03)
                mgr.on_frame(cam, vp)
            qm = VQ.quality_map(mgr, vp, W, H)
            recs = V.frontier_records(mgr, vp, W, H)
            sm = V.seam_metrics(recs, V.adjacency(recs))
            print(row(label, state, qm, sm))
            VQ.render_png(qm, os.path.join(OUT, f"{label}_{state}.png"),
                          f"{label} {state}")
    print("\n[PATHS - worst frame over the path]")
    paths = {
        "slow pan": [(cx - half * 0.3 + i * half * 0.004, cy, half * 0.5)
                     for i in range(100)],
        "fast pan": [(cx - half * 0.5 + i * half * 0.01, cy + i * half * 0.003,
                      half * 0.4) for i in range(100)],
        "zoom in": [(cx, cy, half * (1.0 * (0.965 ** i))) for i in range(100)],
        "zoom out": [(cx, cy, half * (0.03 * (1.04 ** i))) for i in range(100)],
        "pan+zoom": [(cx - half * 0.3 + i * half * 0.006, cy,
                      half * (0.7 * (0.985 ** i))) for i in range(100)],
        "reversal": [(cx + half * 0.25 * np.sin(i / 3.0), cy, half * 0.4)
                     for i in range(100)],
    }
    for name, cams in paths.items():
        worst = None
        stats = {"starved": 0, "over": 0, "empty": 0, "seams": 0, "dup": 0,
                 "p95": 0.0, "mx": 0.0}
        for (px_, py_, hw) in cams:
            cam = Camera2D(px_, py_, hw, W, H)
            vp = (px_, py_, hw, hw * H / W)
            mgr.note_camera_motion()
            start = time.perf_counter()
            mgr.on_frame(cam, vp)
            timings.setdefault(name, []).append((time.perf_counter() - start) * 1000)
            qm = VQ.quality_map(mgr, vp, W, H)
            recs = V.frontier_records(mgr, vp, W, H)
            sm = V.seam_metrics(recs, V.adjacency(recs))
            frame_reports.append(dict(path=name, frame=len(timings[name]) - 1,
                visible_nodes=int(mgr.last_screen_space_diag.get("visible_nodes", 0)),
                drawn_nodes=len(mgr.active_draw_keys),
                adjacent_lod_difference=int(sm["lod_diff_max"]),
                adjacent_spacing_p95=float(sm["spacing_ratio_p95"]),
                adjacent_spacing_max=float(sm["spacing_ratio_max"]),
                duplicate_draw_count=len(mgr.active_draw_keys)-len(set(mgr.active_draw_keys)),
                overview_beside_finer=bool(mgr._overview_key() in mgr.active_draw_keys and len(mgr.active_draw_keys)>1),
                adjacent_ppp_max=float(qm["max_adjacent_ppp_ratio"]),
                seam_candidates=int(qm["visible_tile_seam_count"]),
                largest_starved_region=int(qm["largest_starved_component"]),
                coverage_holes=int(qm["empty_cells"]),
                duplicate_cover_cells=int(qm["duplicate_cover_cells"]),
                handoff_waiting=bool(mgr.last_screen_space_diag.get("continuity_handoff_waiting", False))))
            stats["starved"] = max(stats["starved"], qm["starved_cells"])
            stats["over"] = max(stats["over"], qm["overdense_cells"])
            stats["empty"] = max(stats["empty"], qm["empty_cells"])
            stats["dup"] = max(stats["dup"], qm["duplicate_cover_cells"])
            stats["seams"] = max(stats["seams"], qm["visible_tile_seam_count"])
            if np.isfinite(qm["p95_adjacent_ppp_ratio"]):
                stats["p95"] = max(stats["p95"], qm["p95_adjacent_ppp_ratio"])
            if np.isfinite(qm["max_adjacent_ppp_ratio"]):
                stats["mx"] = max(stats["mx"], qm["max_adjacent_ppp_ratio"])
        print(f"  {name:<9} worst: empty={stats['empty']} starved={stats['starved']} "
              f"overdense={stats['over']} dup={stats['dup']} seams={stats['seams']} "
              f"adjPPP p95={stats['p95']:.2f} max={stats['mx']:.2f}")
    fs = mgr._fs()
    print(f"\nrange pushes={mgr.draw_range_pushes}  selector_runs="
          f"{mgr.lod_stats['selector_runs']}  fast/slow frames="
          f"{fs['fast_frames']}/{fs['slow_frames']}")
    report = dict(backend="headless stream-manager; Vulkan pixels unverified",
        frames=frame_reports,
        timings_ms={k: dict(p50=float(np.percentile(v, 50)),
                            p95=float(np.percentile(v, 95))) for k, v in timings.items()})
    report["numeric_pass"] = all(f["adjacent_lod_difference"] <= 1
        and (not np.isfinite(f["adjacent_spacing_max"]) or f["adjacent_spacing_max"] <= 2.3)
        and not f["overview_beside_finer"] and f["duplicate_draw_count"] == 0
        and f["adjacent_ppp_max"] <= 2.5 and f["seam_candidates"] == 0
        and f["coverage_holes"] == 0 and f["duplicate_cover_cells"] == 0
        and f["largest_starved_region"] == 0 for f in frame_reports)
    with open(os.path.join(OUT, "strict_acceptance.json"), "w", encoding="utf-8") as fp:
        json.dump(report, fp, indent=2)
    print("STRICT NUMERIC:", "PASS" if report["numeric_pass"] else "FAIL")
    return 0 if report["numeric_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
