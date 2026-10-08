"""perf_camera_phase4.py - PHASE 4 performance harness (real 27M hierarchy).

Scenarios (each 100 frames; every frame emits 3 RAW camera callbacks - the
camera_modified / main_camera_pan / mousemove triple seen in production logs -
then ONE on_frame, which is the only place a selection may run):

  A identical frames          B sub-pixel pans (0.1 px/frame)
  C normal pans (6 px/frame)  D rapid pan reversals (40 px, flip every 8 frames)
  E zoom (1.5 %/frame)        F alternating zoom in/out
  G same camera, varying budget

    py perf_camera_phase4.py            # numba kernel (production default)
    NAKSHA_LOD_KERNEL=python py perf_camera_phase4.py   # reference A/B
"""
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from gui.naksha_cache.reader import NakshaPointCacheReader            # noqa: E402
from gui.naksha_cache.stream_manager import NakshaStreamManager       # noqa: E402
from gui.naksha_cache.stream_renderer_adapter import HeadlessTileRenderer  # noqa: E402
from gui.naksha_cache.stream_telemetry import StreamTelemetry         # noqa: E402
from naksha_lod_gate import Camera2D                                  # noqa: E402

# NAKSHA_BENCH_SRC selects the benchmark cache (project path whose
# .nakshaidx/.nakshapc to open), e.g. the corrected v2cache/ build.
SRC = os.environ.get("NAKSHA_BENCH_SRC") or os.path.join(
    ROOT, "test_classified_highprecision.laz")
W, H = 1920, 848
N = 100


def pc(a, q):
    return float(np.percentile(a, q)) if len(a) else float("nan")


def build():
    reader = NakshaPointCacheReader(SRC, verify_crc=False, load_edits=False)
    hdr = reader.index.header
    gmin = np.asarray(hdr["bounds_min"], float)
    gmax = np.asarray(hdr["bounds_max"], float)
    cx, cy = (gmin[0] + gmax[0]) / 2, (gmin[1] + gmax[1]) / 2
    half = (gmax[0] - gmin[0]) / 2

    class _App:
        pass
    adapter = HeadlessTileRenderer(total_points=int(reader.index.total_points))
    te = StreamTelemetry(os.path.join(ROOT, "diagnostics", "perf4.jsonl"))
    mgr = NakshaStreamManager(_App(), reader, adapter, te,
                              ram_budget=4 * 1024 ** 3, gpu_budget=2 * 1024 ** 3)
    cam = Camera2D(cx, cy, half, W, H)
    vp = (cx, cy, half, half * H / W)
    mgr.open_first_frame()
    for _ in range(600):
        mgr.on_frame(cam, vp)
        if getattr(mgr, "full_resident_ready", False):
            break
    for _ in range(40):
        mgr.on_frame(cam, vp)
        if getattr(mgr, "_camera_state", None) == "IDLE":
            break
        time.sleep(0.03)
    return mgr, adapter, cx, cy, half


def scenario(mgr, adapter, name, cams, moving=True, budgets=None):
    st = mgr.lod_stats
    s0 = dict(st)
    pushes0 = int(getattr(mgr, "draw_range_pushes", 0) or 0)
    up0 = int(getattr(adapter, "arena_xyz_points", 0) or 0)
    wb0 = int(getattr(mgr, "whole_buffer_rebuilds", 0) or 0)
    uniq = set()
    tot, sel_ms = [], []
    runs_before = st["selector_runs"]
    ms_before = st["kernel_ms_sum"]
    for i, (cx, cy, hw) in enumerate(cams):
        cam = Camera2D(cx, cy, hw, W, H)
        vp = (cx, cy, hw, hw * H / W)
        if budgets is not None:
            b = budgets[i]
            mgr.density.apply = (lambda lod, w, h, moving, _b=b:
                                 (setattr(lod, "budget", _b),
                                  setattr(lod, "moving", moving), _b)[2])
        if moving:
            mgr.note_camera_motion()
        for _src in ("camera_modified", "main_camera_pan", "mousemove"):
            mgr.on_camera_changed(cam, vp, source=_src)
        uniq.add((round(cx, 4), round(cy, 4), round(hw, 4)))
        t = time.perf_counter()
        mgr.on_frame(cam, vp)
        tot.append((time.perf_counter() - t) * 1000.0)
    runs = st["selector_runs"] - s0["selector_runs"]
    raw = st["raw_callbacks"] - s0["raw_callbacks"]
    hits = (st["window_hits"] - s0["window_hits"])
    k_ms = st["kernel_ms_sum"] - ms_before
    holes = 0
    try:
        # the coverage-grid scan only: the full telemetry also runs the
        # pairwise overlap audit (34-44 s on this cache)
        holes = int(mgr._coverage_holes()[0])
    except Exception:
        pass
    print(f"\n[{name}]")
    print(f"  raw callbacks        : {raw}")
    print(f"  unique camera states : {len(uniq)}")
    print(f"  selector derivations : {runs}   (cache/window hits {hits})")
    print(f"  derivations / raw    : {runs / max(raw, 1):.3f}")
    if runs:
        print(f"  selector mean        : {k_ms / runs:.4f} ms per derivation")
    print(f"  on_frame ms          : p50 {pc(tot, 50):.3f}  p95 {pc(tot, 95):.3f}"
          f"  max {max(tot):.3f}")
    print(f"  range pushes         : "
          f"{int(getattr(mgr, 'draw_range_pushes', 0) or 0) - pushes0}"
          f"   XYZ uploads {int(getattr(adapter, 'arena_xyz_points', 0) or 0) - up0}"
          f"   whole-buffer {int(getattr(mgr, 'whole_buffer_rebuilds', 0) or 0) - wb0}"
          f"   holes {holes}")
    print(f"  active               : {len(mgr.active_draw_keys or ())} blocks / "
          f"{mgr.active_draw_points:,} pts")
    fs = mgr._fs()
    print(f"  fast path            : fast {fs['fast_frames']} slow "
          f"{fs['slow_frames']} reasons {fs['slow_reasons']} "
          f"corrections {fs['safety_recheck_corrections']} "
          f"unnecessary_pushes {fs['unnecessary_pushes']}")
    return tot


def main():
    mgr, adapter, cx, cy, half = build()
    print(f"kernel mode: {mgr._lod_kernel_mode}   warmup: "
          f"{mgr.lod_stats['warmup']}")
    pxm = 2 * (half * 0.5) / W          # world metres per px at zoom 0.5

    def A():
        return [(cx, cy, half * 0.5)] * N

    def B():
        return [(cx + i * 0.1 * pxm, cy, half * 0.5) for i in range(N)]

    def C():
        return [(cx + i * 6 * pxm, cy + i * 2 * pxm, half * 0.5)
                for i in range(N)]

    def D():
        out, x = [], 0.0
        for i in range(N):
            x += 40 * pxm * (1 if (i // 8) % 2 == 0 else -1)
            out.append((cx + x, cy, half * 0.5))
        return out

    def E():
        return [(cx, cy, half * (0.8 * (0.985 ** i))) for i in range(N)]

    def F():
        return [(cx, cy, half * 0.4 * (1.015 ** (i if (i // 25) % 2 == 0
                                                 else 25 - i % 25)))
                for i in range(N)]

    for name, fn, mv in (("A identical x100", A, False),
                         ("B sub-pixel pans", B, True),
                         ("C normal pans", C, True),
                         ("D rapid pan reversals", D, True),
                         ("E zoom", E, True),
                         ("F alternating zoom", F, True)):
        scenario(mgr, adapter, name, fn(), moving=mv)
    G = [(cx, cy, half * 0.5)] * N
    scenario(mgr, adapter, "G same camera, varying budget", G, moving=False,
             budgets=[400_000 + 25_000 * (i % 10) for i in range(N)])
    print(f"\nverify_mismatches: {mgr.lod_stats['verify_mismatches']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
