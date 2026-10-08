"""profile_camera_hotpath.py - PHASE 4 / PART 1: where do the changing-frame ms go?

Runs the REAL NakshaStreamManager.on_frame on the real 27M cache (headless
adapter, no GPU) and attributes wall time to stages by wrapping the production
methods with perf_counter timers. Wrapped timers are INCLUSIVE, so nested stages
are also reported with their parent subtracted ("self" column) where relevant.

    py profile_camera_hotpath.py            # pan / zoom / settled, p50 p95 max
    py profile_camera_hotpath.py --cprofile # plus a cProfile top-25 of pan frames
"""
import cProfile
import io
import os
import pstats
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

SRC = os.path.join(ROOT, "test_classified_highprecision.laz")
W, H = 1920, 848
N = 100

# (label, owner-resolver, method name). Order = reporting order.
STAGES = [
    ("commit_mode_swap", "mgr", "_commit_atomic_mode_swap"),
    ("drain_done", "mgr", "_drain_done"),
    ("measure_viewport", "mgr", "_measure_viewport"),
    ("is_idle", "mgr", "is_idle"),
    ("camera_consume+sig", "mgr", "_canonical_camera"),
    ("note_camera", "mgr", "_note_camera"),
    ("flush_gpu(total)", "mgr", "_flush_gpu"),
    ("build_ranges", "mgr", "_build_ranges"),
    ("ss_active_keys", "mgr", "_screen_space_active_keys"),
    ("density.apply", "density", "apply"),
    ("resident_lod_nodes", "mgr", "_resident_lod_nodes"),
    ("camera_signature", "mgr", "_camera_signature"),
    ("visible_rows", "mgr", "_visible_node_rows"),
    ("lod.select", "lod", "select"),
    ("frame_budget.observe", "fb", "observe"),
    ("frame_budget.update", "fb", "update"),
    ("telemetry(_sample)", "mgr", "_sample"),
    ("adapter.*", "adapter", None),
]


class Prof:
    def __init__(self):
        self.acc = {}
        self.cur = {}

    def wrap(self, label, obj, name):
        fn = getattr(obj, name, None)
        if fn is None:
            return
        acc = self.acc.setdefault(label, [])

        def w(*a, **k):
            t = time.perf_counter()
            try:
                return fn(*a, **k)
            finally:
                self.cur[label] = self.cur.get(label, 0.0) + (
                    time.perf_counter() - t) * 1000.0
        setattr(obj, name, w)

    def frame_done(self):
        for lab in self.acc:
            self.acc[lab].append(self.cur.get(lab, 0.0))
        self.cur = {}


def pct(a, q):
    return float(np.percentile(a, q)) if len(a) else float("nan")


def main():
    do_cprof = "--cprofile" in sys.argv
    if not os.path.isfile(SRC + ".nakshaidx"):
        print("SKIP: no cache beside", SRC)
        return 1
    reader = NakshaPointCacheReader(SRC, verify_crc=False, load_edits=False)
    hdr = reader.index.header
    gmin = np.asarray(hdr["bounds_min"], float)
    gmax = np.asarray(hdr["bounds_max"], float)
    cx, cy = (gmin[0] + gmax[0]) * 0.5, (gmin[1] + gmax[1]) * 0.5
    half = (gmax[0] - gmin[0]) * 0.5

    class _App:
        pass

    adapter = HeadlessTileRenderer(total_points=int(reader.index.total_points))
    te = StreamTelemetry(os.path.join(ROOT, "diagnostics", "cam_profile.jsonl"))
    mgr = NakshaStreamManager(_App(), reader, adapter, te,
                              ram_budget=4 * 1024 ** 3, gpu_budget=2 * 1024 ** 3)

    def cam_at(ccx, ccy, hw):
        return (Camera2D(ccx, ccy, hw, W, H), (ccx, ccy, hw, hw * H / W))

    cam, vp = cam_at(cx, cy, half)
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
    print(f"render_mode={mgr.render_mode} full_resident_ready="
          f"{mgr.full_resident_ready} resident={len(mgr.resident)} blocks")

    prof = Prof()
    fb = mgr._frame_budget()
    owners = {"mgr": mgr, "density": mgr.density, "lod": mgr.lod, "fb": fb,
              "adapter": adapter}
    for label, own, meth in STAGES:
        if meth is None:
            for n in ("set_draw_ranges", "set_point_draw_ranges", "submit",
                      "set_camera", "set_render_origin", "draw"):
                if hasattr(adapter, n):
                    prof.wrap(label, adapter, n)
            continue
        prof.wrap(label, owners[own], meth)

    def run(name, cams, moving=True):
        for lab in prof.acc:
            prof.acc[lab] = []
        tot = []
        sel_nodes = []
        for ccx, ccy, hw in cams:
            c, v = cam_at(ccx, ccy, hw)
            if moving:
                mgr.note_camera_motion()
            t = time.perf_counter()
            mgr.on_frame(c, v)
            tot.append((time.perf_counter() - t) * 1000.0)
            prof.frame_done()
            d = getattr(mgr, "last_screen_space_diag", None) or {}
            sel_nodes.append((d.get("visible_nodes", 0), d.get("gate_blocks", 0),
                              d.get("submitted_points", 0)))
        print(f"\n[CAMERA HOT PATH PROFILE] {name}  frames={len(tot)}")
        print(f"  {'stage':<24}{'p50':>9}{'p95':>9}{'max':>9}   (ms, inclusive)")
        print(f"  {'TOTAL on_frame':<24}{pct(tot, 50):9.3f}{pct(tot, 95):9.3f}"
              f"{max(tot):9.3f}")
        for lab in prof.acc:
            a = prof.acc[lab]
            if not a or max(a) == 0.0:
                continue
            print(f"  {lab:<24}{pct(a, 50):9.3f}{pct(a, 95):9.3f}{max(a):9.3f}")
        v = np.asarray(sel_nodes)
        print(f"  visible_nodes p50={np.median(v[:, 0]):.0f}  selected_nodes "
              f"p50={np.median(v[:, 1]):.0f}  submitted_points p50="
              f"{np.median(v[:, 2]):,.0f}")
        return tot

    # A: settled (identical camera, IDLE)
    settled = [(cx, cy, half)] * N
    run("SETTLED (100 identical frames)", settled, moving=False)

    # B: pan - meaningful steps of 0.4 % of the view width, bouncing
    pan = []
    for i in range(N):
        off = ((i % 50) - 25) * half * 0.008
        pan.append((cx + off, cy + off * 0.3, half * 0.5))
    def _pan():
        return run("PAN (100 meaningful pan frames)", pan)
    if do_cprof:
        pr = cProfile.Profile()
        pr.enable()
        _pan()
        pr.disable()
        s = io.StringIO()
        pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(28)
        print(s.getvalue())
    else:
        _pan()

    # C: zoom - alternating in/out in 2 % steps
    zoom = []
    for i in range(N):
        f = 1.0 - 0.02 * ((i % 25) + 1) if (i // 25) % 2 == 0 \
            else 0.5 + 0.02 * ((i % 25) + 1)
        zoom.append((cx, cy, half * max(f, 0.05)))
    run("ZOOM (100 zoom frames)", zoom)
    return 0


if __name__ == "__main__":
    sys.exit(main())
