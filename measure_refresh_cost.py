"""measure_refresh_cost.py - PHASE 4B / PART 44: what does the 0.5 s safety
re-check (a full `_flush_gpu`) cost, and how does it compare to fast frames?
Also times the diagnostics inside it."""
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import perf_camera_phase4 as P                                   # noqa: E402
from naksha_lod_gate import Camera2D                             # noqa: E402


def main():
    mgr, adapter, cx, cy, half = P.build()
    cam = Camera2D(cx, cy, half * 0.5, P.W, P.H)
    vp = (cx, cy, half * 0.5, half * 0.5 * P.H / P.W)
    for _ in range(20):
        mgr.on_frame(cam, vp)
    fast, slow = [], []
    for _ in range(400):
        why = mgr._fast_flush_reason()
        t = time.perf_counter()
        mgr.on_frame(cam, vp)
        ms = (time.perf_counter() - t) * 1000.0
        (fast if why is None else slow).append((why, ms))
        time.sleep(0.004)
    f = np.array([m for _w, m in fast])
    s = np.array([m for _w, m in slow])
    print(f"\nfast frames: n={f.size} p50={np.median(f):.3f} p95="
          f"{np.percentile(f, 95):.3f} max={f.max():.3f} ms")
    print(f"slow frames: n={s.size} reasons="
          f"{sorted({w for w, _ in slow})} p50={np.median(s):.3f} "
          f"max={s.max():.3f} ms")
    # diagnostics inside a full flush
    t = time.perf_counter()
    for _ in range(50):
        mgr._overview_diag("measure")
    d_ms = (time.perf_counter() - t) * 1000.0 / 50
    t = time.perf_counter()
    for _ in range(50):
        mgr._overview_key()
    k_ms = (time.perf_counter() - t) * 1000.0 / 50
    keys = mgr._ff["keys"]
    rng = [(int(mgr.resident[k].first), int(mgr.resident[k].count))
           for k in keys[:200]]
    t = time.perf_counter()
    for _ in range(50):
        mgr._record_draw_telemetry(keys, rng)
    r_ms = (time.perf_counter() - t) * 1000.0 / 50
    t = time.perf_counter()
    for _ in range(50):
        mgr._sample(reason="measure")
    s_ms = (time.perf_counter() - t) * 1000.0 / 50
    print(f"diagnostic_ms: overview_diag={d_ms:.3f} overview_key={k_ms:.4f} "
          f"draw_telemetry={r_ms:.3f} sample={s_ms:.3f}")
    print(f"_diag_enabled={mgr._diag_enabled}  stats={mgr._fs()}")


if __name__ == "__main__":
    main()
