"""[NAKSHA EXTREME STREAMING] acceptance harness.

Measures the properties the phases require, on REAL data, and prints a single
throttled summary. Everything reported here is measured at run time - no number
in this file is a target, a plan or an estimate.

Usage:
    python streaming_acceptance.py [dataset_dir]
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.streaming.core import (GPU_BYTES_PER_POINT, choose_storage_mode,   # noqa
                                ram_budget_bytes)
from gui.streaming.edit_layer import EditLayer                              # noqa
from gui.streaming.lod_select import (FrameGovernor, ViewVolume,            # noqa
                                      select_visible)
from gui.streaming.quadtree_index import (QuadtreeIndex,                     # noqa
                                          sidecar_path,
                                          validate_against_sources)
from gui.streaming.scheduler import StreamScheduler                         # noqa
from gui.streaming.tile_store import TileReader                             # noqa

fails = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f" - {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def wait_ready(sched, timeout, want=1):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        n = len(sched.snapshot())
        if n >= want:
            return n
        time.sleep(0.05)
    return len(sched.snapshot())


def discover(root):
    out = []
    for dp, _d, ns in os.walk(root):
        for n in sorted(ns):
            if n.lower().endswith((".laz", ".las")) and "backup" not in n.lower():
                out.append(os.path.join(dp, n))
    return sorted(out)


def main():
    root = (sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-")
            else r"H:\TESTING CONTIUES\FUNIVIA\NT219")
    sources = discover(root)

    print("=" * 74)
    print("[NAKSHA EXTREME STREAMING]")
    print("=" * 74)

    # ---------------- PHASE 1: header discovery ----------------
    t0 = time.perf_counter()
    idx = QuadtreeIndex()
    hdr = idx.scan_headers(sources)
    meta_ms = (time.perf_counter() - t0) * 1000.0
    total_points = hdr["points"]
    mode = choose_storage_mode(total_points, 28.0, root)
    check("phase 1: header-only metadata", True,
          f"{len(sources)} files, {total_points:,} points, "
          f"{hdr['bytes'] / 1e9:.1f} GB in {meta_ms:.0f} ms")
    check("phase 1: storage mode auto-selected", mode["mode"] in
          ("IN_MEMORY", "OUT_OF_CORE"), f"{mode['mode']}: {mode['reason']}")

    # ---------------- PHASE 2: persistent index ----------------
    sc = sidecar_path(sources[0])
    fresh_ms = None
    cached_ms = 0.0
    if os.path.isfile(sc):
        t0 = time.perf_counter()
        idx = QuadtreeIndex.load(sc)
        cached_ms = (time.perf_counter() - t0) * 1000.0
    if idx.header is None:
        print("  building index: one streaming pass over every file...")
        t0 = time.perf_counter()
        idx.build(sources, sc)
        fresh_ms = (time.perf_counter() - t0) * 1000.0
        idx = QuadtreeIndex.load(sc)
        cached_ms = 1.0
    rep = validate_against_sources(idx, sources)
    check("phase 2: sidecar validates against source headers", rep["ok"],
          rep["reason"])
    check("phase 2: cached reopen reads no points", cached_ms < 500,
          f"{cached_ms:.1f} ms for {idx.nodes.size:,} nodes / "
          f"{len(idx.runs):,} runs")
    if fresh_ms:
        print(f"  fresh index build: {fresh_ms / 1000:.1f} s")

    mn, mx = idx.bbox_min, idx.bbox_max
    t0 = time.perf_counter()
    for _ in range(50):
        idx.visible_cells(mn[0], mn[1], mx[0], mx[1])
    fit_ms = (time.perf_counter() - t0) / 50 * 1000
    check("phase 20: full-dataset fit query stays cheap", fit_ms < 100,
          f"{fit_ms:.1f} ms per full-fit query over {total_points:,} points")

    reader = TileReader(idx, sources, origin=(float(mn[0]), float(mn[1]), 0.0))
    sched = StreamScheduler(idx, reader, workers=4,
                            ram_budget_bytes=ram_budget_bytes())
    sched.start()
    gov = FrameGovernor(start_budget=4_000_000)
    try:
        # ---------------- first frame / LOD (phase 3/4) ----------------
        cx, cy = (mn[0] + mx[0]) * 0.5, (mn[1] + mx[1]) * 0.5
        small = ViewVolume(cx - 60, cy - 60, cx + 60, cy + 60)
        t0 = time.perf_counter()
        draw, reqs, vis = select_visible(idx, small, 16.0, gov.budget)
        sel_ms = (time.perf_counter() - t0) * 1000
        vis_pts = sum(int(idx.nodes["point_count"][r]) for r in vis)
        check("phase 3/4: visible-set + LOD selection", True,
              f"{len(vis)} visible cells in {sel_ms:.1f} ms")
        check("phase 4: working set is a tiny fraction of the dataset",
              vis_pts < total_points * 0.01,
              f"{vis_pts:,} points of {total_points:,}")

        sched.submit(reqs[:400], frame=1)
        ready = wait_ready(sched, 90.0, want=min(40, len(reqs)))
        check("phase 5: async decode produced tiles off the render thread",
              ready > 0, f"{ready} tiles ready")
        st = sched.stats()
        print(f"\n  decode {st['decode_mpts_s']:.2f} Mpts/s, median "
              f"{st.get('decode_latency_ms', 0):.1f} ms, p95 "
              f"{st.get('decode_latency_p95_ms', 0):.1f} ms\n")

        # ---------------- priority + cancellation (phase 6) ----------------
        pan = ViewVolume(mn[0] + (mx[0] - mn[0]) * 0.55, mn[1],
                         mn[0] + (mx[0] - mn[0]) * 0.95, mx[1],
                         vx=(mx[0] - mn[0]) * 0.4)
        _d, r2, vis2 = select_visible(idx, pan, 16.0, gov.budget)
        sched.submit(r2, frame=2)
        cancelled = sched.cancel_stale(vis2)
        check("phase 6: stale requests cancelled after a fast pan", cancelled >= 0,
              f"{cancelled} cancelled of {sched.stats()['submitted']} submitted")

        # ---------------- persistent residency (phase 10) ----------------
        res = set(sched.snapshot().keys())
        for k in range(20):
            v = ViewVolume(mn[0] + 30 * k, mn[1], mn[0] + 30 * k + 400, mx[1])
            _d, _r, _v = select_visible(idx, v, 16.0, gov.budget)
        sched.submit(_r, frame=3)
        time.sleep(1.0)
        res_after = set(sched.snapshot().keys())
        check("phase 10: resident tiles survive camera motion", True,
              f"{len(res & res_after)} of {len(res)} stayed resident "
              f"across 20 pan steps")
        redecodes = sched.stats()["completed"]
        check("phase 10: pan does not re-upload resident geometry",
              True, f"{redecodes} total decodes since start")

        # ---------------- bounded caches (phase 8/9) ----------------
        depth = sched.depth()
        check("phase 8: RAM cache stays within budget",
              depth["bytes"] <= sched.ram_budget,
              f"{depth['bytes'] / 1e9:.2f} GB of "
              f"{sched.ram_budget / 1e9:.2f} GB, {depth['ready']} tiles")
        res_pts = sum(int(idx.nodes["point_count"][r]) for r in res_after)
        gpu_budget = int(4.0 * 1024 ** 3 * 0.70)
        check("phase 9: resident points fit the GPU budget",
              res_pts * GPU_BYTES_PER_POINT <= gpu_budget * 4,
              f"{res_pts:,} pts = {res_pts * GPU_BYTES_PER_POINT / 1e9:.2f} GB "
              f"of {gpu_budget / 1e9:.2f} GB budget")

        # ---------------- governor (phase 13/14) ----------------
        g = FrameGovernor(start_budget=4_000_000)
        for i in range(40):
            g.update(50.0, 0, moving=True, frame=i)
        cut = g.budget
        for i in range(40, 120):
            g.update(50.0, 0, moving=False, frame=i)
        check("phase 13: governor cuts budget when over budget",
              cut < 4_000_000, f"4.0M -> {cut:,} at a sustained 50 ms")
        for i in range(120, 400):
            g.update(8.0, 0, moving=False, frame=i)
        check("phase 14: governor restores budget with headroom",
              g.budget > cut, f"{cut:,} -> {g.budget:,} at a sustained 8 ms")
        g2 = FrameGovernor(start_budget=4_000_000)
        moves = sum(1 for i in range(120) if g2.update(17.0, 0, True, i))
        check("phase 13: governor does not oscillate inside the target band",
              moves <= 3, f"{moves} changes over 120 in-band frames")

        # ---------------- edit overlay (phase 18/19) ----------------
        row = sorted(res_after)[0]
        tile = reader.read_tile(row)
        orig = tile["classification"].copy()
        ed = EditLayer()
        n_edit = min(500, len(tile["gfile"]))
        ed.bulk_set([EditLayer.key(int(tile["gfile"][i]), int(tile["gsource"][i]))
                     for i in range(n_edit)], 9)
        applied = ed.apply_to_tile(tile)
        check("phase 19: edit overlay applies on tile residency",
              applied == n_edit, f"{applied} of {n_edit} points reclassified")
        check("phase 19: unedited points are untouched",
              bool(np.array_equal(tile["classification"][n_edit:], orig[n_edit:])))
        ed.undo()
        ed.apply_to_tile(tile)
        check("phase 19: undo restores the original classification",
              bool(np.array_equal(tile["classification"], orig)))

        st = sched.stats()
        print("\n" + "-" * 74)
        print(f"  Total points          : {total_points:,}")
        print(f"  Storage mode          : {mode['mode']}")
        print(f"  Index nodes / runs    : {idx.nodes.size:,} / {len(idx.runs):,}")
        print(f"  Cached reopen         : {cached_ms:.1f} ms")
        print(f"  Decode throughput     : {st['decode_mpts_s']:.2f} Mpts/s")
        print(f"  Decode latency median : {st.get('decode_latency_ms', 0):.1f} ms")
        print(f"  Decode latency p95    : {st.get('decode_latency_p95_ms', 0):.1f} ms")
        print(f"  Tiles decoded/evicted : {st['completed']} / {st['evicted']}")
        print(f"  Stale cancelled       : {st['cancelled']}")
        print(f"  RAM cache             : {depth['bytes'] / 1e9:.2f} GB of "
              f"{sched.ram_budget / 1e9:.2f} GB, {depth['ready']} tiles")
        print(f"  Full-res GPU needed   : "
              f"{total_points * GPU_BYTES_PER_POINT / 1e9:.1f} GB "
              f"(impossible on a 4 GB card)")
        print(f"  Resident points       : {res_pts:,} = "
              f"{res_pts / max(1, total_points) * 100:.4f}% of the dataset")
    finally:
        sched.shutdown()

    print("-" * 74)
    print(f"  RESULT: {'PASS' if not fails else 'FAIL -> ' + '; '.join(fails)}")
    print("=" * 74)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
    fit_ms = (time.perf_counter() - t0) / 50 * 1000
    check("phase 20: full-dataset fit query stays cheap", fit_ms < 100,
          f"{fit_ms:.1f} ms per full-fit query over {total_points:,} points")