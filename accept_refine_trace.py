"""Stage-by-stage trace of ONE refinement block, selected -> GPU.

Instruments every boundary the brief lists and captures the tile_read_error
telemetry that earlier harnesses silently swallowed - worker failures were
being discarded there, which is exactly where a selected block can vanish.
"""
import concurrent.futures
import os
import queue
import sys
import threading
import time

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "test_classified_highprecision.laz")
sys.path.insert(0, ROOT)

from accept_stored_normal_shaded import _build                  # noqa: E402
from gui.naksha_cache.reader import NakshaPointCacheReader       # noqa: E402
from gui.naksha_cache.stream_manager import GPU_RESIDENT          # noqa: E402
from gui.naksha_cache.normal_streaming import open_normal_cache   # noqa: E402
from naksha_lod_gate import Camera2D                              # noqa: E402

W, H = 1920, 848
TRACE = {"key": None}


class Tracer:
    """Replaces the telemetry sink so NOTHING is silently dropped."""

    def __init__(self):
        self.errors = []
        self.events = 0

    def event(self, name, **kw):
        self.events += 1
        if name == "tile_read_error":
            self.errors.append(dict(kw))
            if len(self.errors) <= 5:
                print(f"  [TILE READ ERROR] tile={kw.get('tile')} "
                      f"error={kw.get('error')}")

    def sample(self, **kw):
        pass


def instrument(mgr):
    """Wrap the stages so we can watch the traced block."""
    orig_read = mgr._read_tile
    orig_drain = mgr._drain_done
    orig_reconcile = mgr._reconcile
    orig_retire = mgr._retire_superseded

    def read_tile(spec):
        k = (spec.node_id, spec.lod)
        if k == TRACE["key"]:
            print(f"[REFINE TRACE WORKER START] block={k} "
                  f"gen={mgr.gate.camera_generation} "
                  f"attrs={mgr._required_attrs()}")
        r = orig_read(spec)
        if k == TRACE["key"]:
            if "error" in r:
                print(f"[REFINE TRACE WORKER DONE] block={k} "
                      f"ERROR={r['error']}")
            else:
                print(f"[REFINE TRACE WORKER DONE] block={k} "
                      f"pts={r['pts']:,} xyz={r['xyz'].shape} "
                      f"cls={'yes' if r.get('cls') is not None else 'NO'} "
                      f"normal={'yes' if r.get('normal') is not None else 'NO'} "
                      f"nbytes={r.get('normal_bytes', 0):,} "
                      f"complete={r['readiness'].complete}")
        return r

    def drain_done():
        before = len(mgr._pending)
        orig_drain()
        after = len(mgr._pending)
        if before != after:
            print(f"[REFINE TRACE QUEUE] pending {before}->{after} "
                  f"drained={before - after}")

    def reconcile(specs):
        sel_keys = {(s.node_id, s.lod) for s in (specs or [])}
        k = TRACE["key"]
        if k is not None:
            print(f"[REFINE TRACE RECONCILE PRE] block={k} "
                  f"selected={k in sel_keys} "
                  f"in_ram={k in mgr.ram_cache} "
                  f"in_resident={k in mgr.resident} "
                  f"complete={(mgr.ram_cache[k]['readiness'].complete if k in mgr.ram_cache else 'n/a')} "
                  f"gpu_bytes={mgr.gpu_bytes:,} budget={mgr.gpu_budget:,}")
        r = orig_reconcile(specs)
        if k is not None:
            v = mgr.resident.get(k)
            print(f"[REFINE TRACE ADMIT] block={k} "
                  f"in_resident={v is not None} "
                  f"state={v.state if v is not None else 'ABSENT'} "
                  f"gpu_bytes={mgr.gpu_bytes:,}")
        return r

    n = {"i": 0}

    def retire():
        n["i"] += 1
        k = TRACE["key"]
        before_active = [kk for kk, vv in mgr.resident.items()
                         if vv.state == GPU_RESIDENT]
        traced_before = (mgr.resident[k].state == GPU_RESIDENT
                         if k in mgr.resident else False)
        out = orig_retire()
        after_active = [kk for kk, vv in mgr.resident.items()
                        if vv.state == GPU_RESIDENT]
        traced_after = (mgr.resident[k].state == GPU_RESIDENT
                        if k in mgr.resident else False)
        print(f"[RETIRE PASS] pass={n['i']} active_before={len(before_active)} "
              f"retired={out} active_after={len(after_active)} "
              f"traced_before={'YES' if traced_before else 'NO'} "
              f"traced_after={'YES' if traced_after else 'NO'}")
        return out

    mgr._read_tile = read_tile
    mgr._drain_done = drain_done
    mgr._reconcile = reconcile
    mgr._retire_superseded = retire
    return orig_retire


def main():
    if not os.path.isfile(SRC + ".nakshanorm"):
        print("SKIP: real sidecar not present")
        return 0
    print("=" * 72)
    print("LIVE REFINEMENT PIPELINE TRACE")
    print("=" * 72)

    reader = NakshaPointCacheReader(SRC, verify_crc=False, load_edits=False)
    nreader, nrep = open_normal_cache(SRC, verify_source=False,
                                      expected_source_points=26960750)
    mgr = _build(reader, nreader, nrep)
    te = Tracer()
    mgr.te = te
    mgr._executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=4, thread_name_prefix="naksha-trace")
    mgr._pending = {}
    mgr._done = queue.Queue()
    mgr._ram_lock = threading.Lock()
    mgr.frame_tick_error_count = 0
    mgr._last_budget_target = None
    mgr._budget_report_printed_for = None
    mgr._camera_state = None
    mgr._last_specs = None
    mgr._vp_w, mgr._vp_h = 0, 0
    mgr._last_visible_nodes = 0

    hdr = reader.index.header
    gmin = np.asarray(hdr["bounds_min"], float)
    gmax = np.asarray(hdr["bounds_max"], float)
    cx, cy = (gmin[0] + gmax[0]) * .5, (gmin[1] + gmax[1]) * .5
    span_x = float(gmax[0] - gmin[0])
    cam = Camera2D(cx, cy, span_x * .5, W, H)
    viewport = (cx, cy, span_x * .5, span_x * .5 * (H / float(W)))

    mgr.display_mode = "classification"
    mgr.shaded_mode = False
    mgr.open_first_frame()
    ov_keys = set(mgr.resident)
    print(f"\noverview resident keys: {ov_keys}")

    mgr.set_display_mode("shaded")
    print(f"after shaded click: resident={sorted(mgr.resident)} "
          f"pending={len(mgr._pending)} "
          f"resident_normal_points={mgr.normal_resident_points:,}")

    instrument(mgr)

    # ---- Part 1: pick ONE real refinement block ------------------------
    mgr.note_camera_motion()
    time.sleep(0.45)                      # reach IDLE
    for _ in range(3):
        mgr.on_frame(cam, viewport)
        time.sleep(0.02)
    sel = list(getattr(mgr, "_last_specs", None) or [])
    print(f"\n[SELECTION] {len(sel)} blocks, "
          f"{sum(int(s.points) for s in sel):,} points")
    target = None
    for s in sorted(sel, key=lambda x: -x.points):
        if (s.node_id, s.lod) not in ov_keys:
            target = s
            break
    if target is None:
        print("NO refinement block selected beyond the overview!")
        return 1
    TRACE["key"] = (target.node_id, target.lod)
    try:
        entry = mgr.idx.find_block(target.node_id, target.lod)
        bid = int(entry["block_id"]) if entry is not None else -1
    except Exception:
        bid = -1
    print(f"\n[REFINE TRACE TARGET]\nnode_id: {target.node_id}\n"
          f"block_id: {bid}\nLOD: {target.lod}\n"
          f"point_count: {target.points:,}\n"
          f"required_attrs: {mgr._required_attrs()}\n"
          f"already_resident: "
          f"{'YES' if TRACE['key'] in mgr.resident else 'NO'}")

    # ---- Part 15: hold stationary 6 s ----------------------------------
    print("\n--- holding camera stationary 6 s ---")
    t0 = time.time()
    req_seen = []
    orig_req = mgr._request_missing

    def req(specs):
        n = orig_req(specs)
        if n:
            req_seen.append((round(time.time() - t0, 2), n))
        return n
    mgr._request_missing = req
    errs = []
    while time.time() - t0 < 6.0:
        try:
            mgr.on_frame(cam, viewport)
        except Exception as exc:
            errs.append(repr(exc))
        time.sleep(0.02)

    keys = [k for k, v in mgr.resident.items() if v.state == GPU_RESIDENT]
    dist = {}
    for k in keys:
        lod = int(mgr.resident[k].lod)
        dist[lod] = dist.get(lod, 0) + 1
    submitted = int(sum(mgr.resident[k].count for k in keys))

    print("\n" + "=" * 72)
    print(f"requests submitted (new tiles): {sum(n for _t, n in req_seen)}")
    print(f"pending at end     : {len(mgr._pending)}")
    print(f"ram_cache blocks   : {len(mgr.ram_cache)}")
    print(f"completed          : {mgr.completed}")
    print(f"TILE READ ERRORS   : {len(te.errors)}")
    for e in te.errors[:5]:
        print(f"    tile={e.get('tile')} error={e.get('error')}")
    print(f"frame exceptions   : {len(errs)}")
    for e in errs[:3]:
        print("    ", e)
    print(f"active blocks      : {len(keys)}  dist={dist}")
    print(f"submitted points   : {submitted:,}")
    print(f"resident normal pts: {mgr.normal_resident_points:,}")
    print(f"normal GPU bytes   : {mgr.normal_gpu_bytes:,}")
    tk = TRACE["key"]
    v = mgr.resident.get(tk)
    print(f"TRACED {tk}: resident={v is not None} "
          f"state={v.state if v is not None else 'ABSENT'} "
          f"complete={v.complete if v is not None else 'n/a'} "
          f"in_ram={tk in mgr.ram_cache}")
    if tk in mgr.ram_cache:
        rr = mgr.ram_cache[tk]
        print(f"  ram pts={rr['pts']:,} err={rr.get('error', 'none')} "
              f"readiness_missing={rr['readiness'].missing()}")
    print(f"ram_hits/misses    : {mgr.ram_hits}/{mgr.ram_misses}")
    print(f"evictions          : {mgr.evictions}")
    print(f"gpu_bytes/budget   : {mgr.gpu_bytes:,}/{mgr.gpu_budget:,}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
