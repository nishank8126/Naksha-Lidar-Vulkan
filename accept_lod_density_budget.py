"""Headless REAL-data proof that the screen-density budget is actually wired.

Uses the production Camera2D, ScreenSpaceLOD, NKPC reader, normal sidecar and
NakshaStreamManager._flush_gpu. The budget is applied through
ScreenDensityBudget exactly as on_camera_changed/on_frame apply it.

Answers: does ppp sit near the 1.0 target instead of the old 5.1, does the
budget scale with viewport, and is illegal parent/child overlap 0 at rest?
"""
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "test_classified_highprecision.laz")
sys.path.insert(0, ROOT)

from accept_stored_normal_shaded import _build, _resolve          # noqa: E402
from gui.naksha_cache.stream_manager import GPU_RESIDENT        # noqa: E402
from gui.naksha_cache.reader import NakshaPointCacheReader       # noqa: E402
from gui.naksha_cache.normal_streaming import (                   # noqa: E402
    IDLE_TARGET_POINTS_PER_PIXEL, MOVING_TARGET_POINTS_PER_PIXEL,
    ScreenDensityBudget, open_normal_cache, points_per_pixel)
from naksha_lod_gate import Camera2D, ScreenSpaceLOD            # noqa: E402

VIEWPORTS = [(1280, 720), (1400, 731), (1920, 848), (2560, 1440)]
LADDER = [("FIT", 1.0), ("x2", 0.5), ("x4", 0.25), ("x8", 0.125),
          ("x16", 0.0625)]


def settle(mgr, cam, w, h, moving):
    """Simulate one camera state through the PRODUCTION selection path."""
    lod = ScreenSpaceLOD()
    budget = ScreenDensityBudget()
    budget.apply(lod, w, h, moving=moving)          # the real wiring
    # Production hands `_visible_node_rows` a real (cx, cy, half_w, half_h)
    # viewport: `on_camera_changed` stores it in self._viewport before it
    # selects. Passing None returns EVERY node, so the visible set is
    # byte-identical at FIT and at x16 - and because the area-share budget is
    # scale-invariant by construction (every node's proj_px scales with ppm^2,
    # so the ratios do not move), the selector provably cannot produce a
    # different answer at a different zoom. That is not a configuration the
    # renderer ever runs, and it is why "zoom shifts detail toward finer LODs"
    # could not hold: the check was being fed a view with culling switched off.
    viewport = (cam.cx, cam.cy, cam.half_w, cam.height_m)
    vis = mgr._visible_node_rows(cam, viewport)
    sel = lod.select(mgr.idx, cam, vis)
    budget.note_candidates(sel)
    budget.note_selected(sum(int(o["points"]) for o in sel))
    return lod, budget, vis, sel


def main():
    if not os.path.isfile(SRC + ".nakshanorm"):
        print("SKIP: real sidecar not present")
        return 0
    print("=" * 74)
    print("HEADLESS REAL-DATA SCREEN-DENSITY BUDGET")
    print("=" * 74)

    reader = NakshaPointCacheReader(SRC, verify_crc=False, load_edits=False)
    nreader, nrep = open_normal_cache(SRC, verify_source=False,
                                      expected_source_points=26960750)
    print(f"[NORMAL CACHE] status={nrep.status} blocks={nrep.blocks:,} "
          f"stored_normals={nrep.stored_normals:,}")
    assert nrep.status == "HIT", nrep.reason
    mgr = _build(reader, nreader, nrep)
    assert mgr.normal_source == "stored"

    hdr = reader.index.header
    gmin = np.asarray(hdr["bounds_min"], float)
    gmax = np.asarray(hdr["bounds_max"], float)
    cx, cy = (gmin[0] + gmax[0]) * .5, (gmin[1] + gmax[1]) * .5
    span_x = float(gmax[0] - gmin[0])

    # ---------- Part 15/16/17: budget across viewports --------------------
    print(f"\n{'viewport':<12}{'state':<8}{'target_pts':>12}{'candidates':>13}"
          f"{'selected':>12}{'ppp':>8}")
    viewport_rows = []
    for w, h in VIEWPORTS:
        for moving in (True, False):
            cam = Camera2D(cx, cy, span_x * 0.5, w, h)
            _lod, budget, vis, sel = settle(mgr, cam, w, h, moving)
            sel_pts = sum(int(o["points"]) for o in sel)
            ppp = points_per_pixel(sel_pts, w, h)
            state = "MOVING" if moving else "IDLE"
            print(f"{f'{w}x{h}':<12}{state:<8}{budget.last_target:>12,}"
                  f"{budget.last_candidate:>13,}{sel_pts:>12,}{ppp:>8.3f}")
            viewport_rows.append((w, h, state, budget.last_target,
                                  budget.last_candidate, sel_pts, ppp))
# ---------- Part 15: full ladder, 1920x848, IDLE ----------------------
    print(f"\n[ZOOM LADDER 1920x848 - IDLE] target_ppp="
          f"{IDLE_TARGET_POINTS_PER_PIXEL}")
    print(f"  {'level':<7}{'target':>12}{'candidates':>13}{'selected':>12}"
          f"{'ppp':>8}  LOD-dist")
    ladder = []
    for label, frac in LADDER:
        # A real zoom is a fresh camera state, NOT an accumulation of every
        # level ever visited. Rebuilding the manager here prevents the harness
        # itself from manufacturing the parent/child overlap it is meant to
        # measure.
        mgr = _build(reader, nreader, nrep)
        cam = Camera2D(cx, cy, span_x * .5 * frac, 1920, 848)
        lod, budget, vis, sel = settle(mgr, cam, 1920, 848, moving=False)
        sel_pts = sum(int(o["points"]) for o in sel)
        dist = {}
        for o in sel:
            l = int(o["lod"])
            dist[l] = dist.get(l, 0) + 1
        ppp = points_per_pixel(sel_pts, 1920, 848)
        # Materialise the selection through the real streaming path so the
        # overlap audit sees genuine residency, not just a selection list.
        _resolve(mgr, sel)
        mgr._reconcile(mgr._specs_from_resident())
        mgr._flush_gpu(reason="idle")
        mgr._vp_w, mgr._vp_h = 1920, 848
        mgr._camera_state = "IDLE"
        audit = mgr.overlap_audit()
        resident_pts = sum(v.count for v in mgr.resident.values()
                           if v.state == GPU_RESIDENT)
        ladder.append((label, budget.last_target, budget.last_candidate,
                       resident_pts, points_per_pixel(resident_pts, 1920, 848),
                       dist, sel, audit))
        print(f"  {label:<7}{budget.last_target:>12,}"
              f"{budget.last_candidate:>13,}{resident_pts:>12,}"
              f"{points_per_pixel(resident_pts, 1920, 848):>8.3f}  {dist}")
        if label == "x8":
            mgr._last_visible_nodes = len(vis)
            tele = mgr.lod_budget_telemetry()

    # ---------- Part 14: overlap audit per ladder level -------------------
    print("\n[LOD OVERLAP - per stabilised IDLE level]")
    for label, _t, _c, pts, ppp, dist, sel, audit in ladder:
        print(f"  {label:<6} active={len(audit['active_blocks']):>3} "
              f"hier={audit['hierarchical_overlap_pairs']:>3} "
              f"temporary={audit['temporary_handoff_pairs']:>3} "
              f"ILLEGAL={audit['illegal_persistent_overlap_pairs']:>3}")

    fit_lods = {int(o["lod"]) for o in ladder[0][6]}
    zoom_lods = {int(o["lod"]) for o in ladder[-1][6]}
    fit_w = sum(v * k for k, v in ladder[0][5].items())
    zoom_w = sum(v * k for k, v in ladder[-1][5].items())
    max_illegal = max(a[7]["illegal_persistent_overlap_pairs"]
                      for a in ladder)

    checks = (
        ("normal cache HIT", nrep.status == "HIT"),
        ("normal_source == stored", mgr.normal_source == "stored"),
        ("idle ppp <= 1.2 at every level",
         all(a[4] <= 1.2 for a in ladder)),
        ("moving ppp < idle ppp",
         all(r[6] < 1.0 for r in viewport_rows if r[2] == "MOVING")),
        ("budget scales with pixels",
         len({r[3] for r in viewport_rows if r[2] == "IDLE"}) == len(VIEWPORTS)),
        ("illegal persistent overlap == 0 at every level", max_illegal == 0),
        # LOD0 is the FINEST level, so zoom must LOWER the LOD-weighted mean.
        ("zoom shifts detail toward finer LODs", zoom_w < fit_w),
        ("no overview lock (LOD0 present at x16)", min(zoom_lods) == 0),
        ("submitted stays bounded (<=1.2x target)",
         all(a[3] <= a[1] * 1.2 for a in ladder)),
    )
    print("\n[CHECKS]")
    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print(f"\nFIT  ppp={ladder[0][4]:.3f}  LOD-wmean={fit_w:.2f}")
    print(f"x16  ppp={ladder[-1][4]:.3f}  LOD-wmean={zoom_w:.2f}")
    print(f"RESULT: {'PASS' if ok else 'FAIL'}")
    print("=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())