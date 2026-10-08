"""PHASE 4B / PART 44: cost of the GUI tick's per-frame telemetry.

`_make_tick.tick()` (gui/naksha_cache/app_streaming.py) calls
`mgr.lod_budget_telemetry()` EVERY 16 ms tick - the headless harnesses call
on_frame only, so that cost was never in the Phase 4A numbers."""
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import perf_camera_phase4 as P                                   # noqa: E402
from naksha_lod_gate import Camera2D                             # noqa: E402


def timeit(fn, n=5):
    # Time ONE call first: if a diagnostic is pathological, report it instead
    # of spending minutes in a loop.
    t = time.perf_counter()
    fn()
    first = (time.perf_counter() - t) * 1000.0
    if first > 500.0:
        return f"single call {first:.0f} ms (loop skipped)"
    ts = [first]
    for _ in range(n - 1):
        t = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t) * 1000.0)
    a = np.array(ts)
    return f"p50 {np.median(a):.3f}  max {a.max():.3f} ms (n={len(a)})"


def main():
    mgr, adapter, cx, cy, half = P.build()
    cam = Camera2D(cx, cy, half * 0.5, P.W, P.H)
    vp = (cx, cy, half * 0.5, half * 0.5 * P.H / P.W)
    for _ in range(20):
        mgr.on_frame(cam, vp)
    print("\n[TICK COST] settled camera, real 27M hierarchy")
    print("  on_frame                     :", timeit(lambda: mgr.on_frame(cam, vp)))
    print("  lod_budget_telemetry (full)  :", timeit(mgr.lod_budget_telemetry))
    print("  overlap_audit                :", timeit(mgr.overlap_audit))
    print("  _coverage_holes              :", timeit(mgr._coverage_holes))

    def tick_old():
        mgr.on_frame(cam, vp)
        mgr.lod_budget_telemetry()
    print("  TICK as shipped (frame+tele) :", timeit(tick_old))
    cheap = lambda: (mgr.active_draw_points, mgr._camera_state)       # noqa: E731
    print("  cheap (active_pts, state)    :", timeit(cheap))


if __name__ == "__main__":
    main()
