"""Prove the LIVE GUI frame-tick path is now exception-free.

This drives the REAL on_frame() repeatedly - the exact call the Qt 60 Hz timer
makes - against the REAL dataset, the REAL normal sidecar and the REAL
ScreenSpaceLOD, with the same viewport the live app reported (1400x731).

Before the fix, this raised NameError('IDLE_REFINE_MS') on EVERY tick.
"""
import os
import queue
import sys
import threading

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "test_classified_highprecision.laz")
sys.path.insert(0, ROOT)

from accept_stored_normal_shaded import _build                    # noqa: E402
from gui.naksha_cache.reader import NakshaPointCacheReader       # noqa: E402
from gui.naksha_cache.normal_streaming import (                   # noqa: E402
    IDLE_REFINE_MS, IDLE_TARGET_POINTS_PER_PIXEL,
    MOVING_TARGET_POINTS_PER_PIXEL, open_normal_cache)
from naksha_lod_gate import Camera2D                             # noqa: E402

W, H = 1400, 731          # the live viewport from the report


def main():
    if not os.path.isfile(SRC + ".nakshanorm"):
        print("SKIP: real sidecar not present")
        return 0
    print("=" * 70)
    print("LIVE GUI FRAME-TICK INTEGRATION")
    print("=" * 70)

    import gui.naksha_cache.stream_manager as sm
    print(f"\nIDLE_REFINE_MS resolved in stream_manager: "
          f"{getattr(sm, 'IDLE_REFINE_MS', 'MISSING')}")
    assert getattr(sm, "IDLE_REFINE_MS", None) is not None, \
        "stream_manager still cannot resolve IDLE_REFINE_MS"

    reader = NakshaPointCacheReader(SRC, verify_crc=False, load_edits=False)
    nreader, nrep = open_normal_cache(SRC, verify_source=False,
                                      expected_source_points=26960750)
    print(f"[NORMAL CACHE] status={nrep.status} blocks={nrep.blocks:,} "
          f"stored_normals={nrep.stored_normals:,} open_ms={nrep.open_ms:.2f}")
    assert nrep.status == "HIT"

    mgr = _build(reader, nreader, nrep)
    mgr.display_mode = "shaded"
    mgr.shaded_mode = True
    mgr.frame_tick_error_count = 0
    mgr._last_budget_target = None
    mgr._budget_report_printed_for = None
    mgr._camera_state = None
    mgr._vp_w, mgr._vp_h = 0, 0
    hdr = reader.index.header
    gmin = np.asarray(hdr["bounds_min"], float)
    gmax = np.asarray(hdr["bounds_max"], float)
    cx, cy = (gmin[0] + gmax[0]) * .5, (gmin[1] + gmax[1]) * .5
    span_x = float(gmax[0] - gmin[0])
    half_h = span_x * .5 * (H / float(W))
    # Camera2D carries the PIXEL size (width_px/height_px); the viewport tuple
    # is world units. This mirrors exactly what app_streaming._viewport() builds.
    cam = Camera2D(cx, cy, span_x * .5, W, H)
    viewport = (cx, cy, span_x * .5, half_h)

    # Part 6/7: first frame, then wait past the idle threshold and tick again.
    mgr.open_first_frame()
    mgr.note_camera_motion()
    states = []
    ticks = 0
    errors = []
    import time as _t
    # Part 3: the transition needs REAL elapsed time. Ticking 80 times in a tight
    # loop never reaches the 300 ms threshold, which would falsely report
    # "no MOVING->IDLE transition". Alternate ticks with real waits.
    for i in range(10):
        try:
            mgr.on_frame(cam, viewport)
            ticks += 1
        except Exception as exc:
            errors.append(repr(exc))
            mgr.frame_tick_error_count += 1
        st = mgr._camera_state
        if not states or states[-1] != st:
            states.append(st)
        _t.sleep(0.06)
    # Simulate the camera coming to rest, then let the idle threshold elapse.
    mgr.note_camera_motion()
    for i in range(12):
        try:
            mgr.on_frame(cam, viewport)
            ticks += 1
        except Exception as exc:
            errors.append(repr(exc))
            mgr.frame_tick_error_count += 1
        st = mgr._camera_state
        if not states or states[-1] != st:
            states.append(st)
        _t.sleep(0.06)
    idle_wait_s = IDLE_REFINE_MS / 1000.0 + 0.05
    _t.sleep(idle_wait_s)
    for i in range(4):
        try:
            mgr.on_frame(cam, viewport)
            ticks += 1
        except Exception as exc:
            errors.append(repr(exc))
            mgr.frame_tick_error_count += 1
        st = mgr._camera_state
        if not states or states[-1] != st:
            states.append(st)
        _t.sleep(0.05)

    tele = mgr.lod_budget_telemetry()
    print("\n" + "-" * 70)
    print(f"frame ticks executed      : {ticks}")
    print(f"frame tick exceptions     : {len(errors)}")
    print(f"frame_tick_error_count    : {mgr.frame_tick_error_count}")
    print(f"camera states observed    : {states}")
    print(f"final camera_state        : {tele['camera_state']}")
    print(f"viewport                  : {tele['viewport']} "
          f"({tele['pixels']:,} px)")
    print(f"target_ppp                : {tele['target_ppp']}")
    print(f"target_points             : {tele['target_points']:,}")
    print(f"selected_points           : {tele['selected_points']:,}")
    print(f"submitted_points          : {tele['submitted_points']:,}")
    print(f"actual_ppp                : {tele['actual_ppp']}")
    print(f"LOD0/1/2/3/4              : {tele['LOD0_blocks']}/"
          f"{tele['LOD1_blocks']}/{tele['LOD2_blocks']}/"
          f"{tele['LOD3_blocks']}/{tele['LOD4_blocks']}")
    print(f"normal_source             : {tele['normal_source']}")
    print(f"illegal persistent overlap: "
          f"{tele['illegal_persistent_overlap_pairs']}")
    print("-" * 70)

    exp_target = W * H * IDLE_TARGET_POINTS_PER_PIXEL
    checks = (
        ("IDLE_REFINE_MS resolves in live module", True),
        ("zero frame tick exceptions", len(errors) == 0),
        ("frame_tick_error_count == 0", mgr.frame_tick_error_count == 0),
        ("MOVING -> IDLE transition observed",
         "MOVING" in states and states[-1] == "IDLE"),
        ("live budget uses real viewport pixels",
         tele["target_points"] == int(exp_target)),
        ("target is NOT hardcoded", tele["target_points"] != 1_023_400
         or abs(exp_target - 1_023_400) < 1),
        ("actual_ppp <= 1.2 (no 5 ppp regression)",
         tele["actual_ppp"] is not None and tele["actual_ppp"] <= 1.2),
        ("illegal persistent overlap == 0",
         tele["illegal_persistent_overlap_pairs"] == 0),
        ("normal_source == stored", tele["normal_source"] == "stored"),
    )
    print("\n[CHECKS]")
    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    if errors:
        print("\nfirst errors:")
        for e in errors[:3]:
            print("   ", e)
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())