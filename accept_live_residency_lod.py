"""End-to-end proof on the LIVE on_frame path (1920x848), Shaded from click.

Uses a REAL ThreadPoolExecutor so the async request/completion loop actually
runs. The earlier harness used None, making _request_missing a no-op and hiding
the live LOD defect entirely.
"""
import concurrent.futures
import os
import queue
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "test_classified_highprecision.laz")
sys.path.insert(0, ROOT)

from accept_stored_normal_shaded import _build                  # noqa: E402
from gui.naksha_cache.reader import NakshaPointCacheReader       # noqa: E402
from gui.naksha_cache.stream_manager import GPU_RESIDENT   # noqa: E402
from gui.naksha_cache.normal_streaming import open_normal_cache  # noqa: E402
from naksha_lod_gate import Camera2D                             # noqa: E402

W, H = 1920, 848


def main():
    if not os.path.isfile(SRC + ".nakshanorm"):
        print("SKIP: real sidecar not present")
        return 0
    print("=" * 72)
    print("LIVE STORED-NORMAL RESIDENCY + LOD ACTIVATION")
    print("=" * 72)

    reader = NakshaPointCacheReader(SRC, verify_crc=False, load_edits=False)
    nreader, nrep = open_normal_cache(SRC, verify_source=False,
                                      expected_source_points=26960750)
    mgr = _build(reader, nreader, nrep)
    mgr._executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=4, thread_name_prefix="naksha-live")
    mgr._pending = {}
    mgr._done = queue.Queue()
    mgr.frame_tick_error_count = 0
    mgr._last_budget_target = None
    mgr._budget_report_printed_for = None
    mgr._camera_state = None
    mgr._vp_w, mgr._vp_h = 0, 0
    mgr._last_visible_nodes = 0

    print(f"[NORMAL CACHE] status={nrep.status} blocks={nrep.blocks:,}")
    print(f"[LIVE MANAGER] normal_reader={id(mgr.normal_reader)} "
          f"normal_cache={id(mgr.normal_cache)} mgr={id(mgr)} "
          f"adapter={id(mgr.adapter)}")
    print(f"  reader attached to manager: {mgr.normal_reader is not None}")

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
    ov_key = [k for k in mgr.resident][0]
    ovr = mgr.resident[ov_key]
    print(f"\n[OVERVIEW PREFETCH]")
    print(f"  node_id={ovr.node_id} lod={ovr.lod} block_id={ovr.block_id}")
    print(f"  points={ovr.count:,}")
    print(f"  normal in RAM cache: {ovr.normal is not None} "
          f"({0 if ovr.normal is None else ovr.normal.nbytes:,} bytes)")
    print(f"  resident_normal_points before click: {mgr.normal_resident_points}")

    print("\n[SHADED CLICK] -> mgr.set_display_mode('shaded')")
    mgr.set_display_mode("shaded")
    print(f"  display_mode={mgr.display_mode} shaded_mode={mgr.shaded_mode}")
    print(f"  required attrs={mgr._required_attrs()}")
    print(f"  resident_normal_points after click: {mgr.normal_resident_points:,}")
    print(f"  normal upload count: {mgr.adapter.normal_uploads}")
    print(f"  screen fallback: "
          f"{'ACTIVE' if mgr.normal_source == 'screen' else 'NOT used'}")

    mgr.note_camera_motion()
    errs = []
    t_end = time.time() + 26.0
    printed = {"tb": False}
    while time.time() < t_end:
        try:
            mgr.on_frame(cam, viewport)
        except Exception as exc:
            errs.append(repr(exc))
            mgr.frame_tick_error_count += 1
            if not printed["tb"]:
                printed["tb"] = True
                print("\n!!! FIRST FRAME-TICK EXCEPTION - FULL TRACEBACK !!!")
                import traceback
                traceback.print_exc()
                tb = exc.__traceback__
                fr = tb
                while fr is not None:
                    if fr.tb_frame.f_code.co_filename.endswith(
                            "stream_manager.py"):
                        print(f"\n  FAILING LINE in stream_manager.py:"
                              f"{fr.tb_lineno}")
                        import linecache
                        print("  SOURCE: " + linecache.getline(
                            fr.tb_frame.f_code.co_filename,
                            fr.tb_lineno).rstrip())
                    fr = fr.tb_next
                print(f"  exc type   : {type(exc).__name__}")
                print(f"  exc args   : {exc.args!r}")
                for a in exc.args:
                    print(f"    arg={a!r} type={type(a)}")
                print("!!! END TRACEBACK !!!\n")
        time.sleep(0.02)

    keys = [k for k, v in mgr.resident.items() if v.state == GPU_RESIDENT]
    dist = {}
    for k in keys:
        lod = int(mgr.resident[k].lod)
        dist[lod] = dist.get(lod, 0) + 1
    submitted = int(sum(mgr.resident[k].count for k in keys))
    ppp = submitted / float(W * H)
    tele = mgr.lod_budget_telemetry()

    print("\n" + "-" * 72)
    print(f"frame tick exceptions      : {len(errs)}")
    print(f"frame_tick_error_count     : {mgr.frame_tick_error_count}")
    print(f"camera_state               : {tele['camera_state']}")
    print(f"viewport                   : {tele['viewport']} "
          f"({tele['pixels']:,} px)")
    print(f"target_ppp                 : {tele['target_ppp']}")
    print(f"target_points              : {tele['target_points']:,}")
    print(f"selected_points            : {tele['selected_points']:,}")
    print(f"submitted_points           : {submitted:,}")
    print(f"actual_ppp                 : {ppp:.3f}")
    print(f"LOD distribution           : {dist}")
    print(f"resident normal points     : {mgr.normal_resident_points:,}")
    print(f"normal GPU bytes           : {mgr.normal_gpu_bytes:,}")
    print(f"normal RAM bytes           : {mgr.normal_cache.resident_bytes:,}")
    print(f"illegal persistent overlap : "
          f"{tele['illegal_persistent_overlap_pairs']}")
    print(f"normal_source              : {mgr.normal_source}")
    print("-" * 72)

    all_complete = all(mgr.resident[k].complete for k in keys)
    checks = (
        ("overview normal prefetched (RAM)",
         mgr.normal_cache.contains(mgr.gate.dataset_generation, ovr.block_id)),
        ("normal reader attached to live manager", mgr.normal_reader is not None),
        ("shaded mode requires NORMAL", "normal" in mgr._required_attrs()),
        ("resident_normal_points > 0 after shaded",
         mgr.normal_resident_points > 0),
        ("normal GPU upload happened", mgr.adapter.normal_uploads >= 1),
        ("screen fallback NOT used", mgr.normal_source == "stored"),
        ("zero frame tick exceptions", not errs),
        ("refined BEYOND overview (>299,676)", submitted > 299_676),
        ("actual_ppp <= 1.2", ppp <= 1.2),
        ("illegal persistent overlap == 0",
         tele["illegal_persistent_overlap_pairs"] == 0),
        ("every active block COMPLETE", all_complete),
        ("normal points == submitted points",
         mgr.normal_resident_points == submitted),
    )
    print("\n[CHECKS]")
    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    for e in errs[:3]:
        print("   ", e)
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

    print("\n" + "-" * 72)
    print(f"frame tick exceptions      : {len(errs)}")
    print(f"frame_tick_error_count     : {mgr.frame_tick_error_count}")
    print(f"camera_state               : {tele['camera_state']}")
    print(f"viewport                   : {tele['viewport']} "
          f"({tele['pixels']:,} px)")
    print(f"target_ppp                 : {tele['target_ppp']}")
    print(f"target_points              : {tele['target_points']:,}")
    print(f"selected_points            : {tele['selected_points']:,}")
    print(f"submitted_points           : {submitted:,}")
    print(f"actual_ppp                 : {ppp:.3f}")
    print(f"LOD distribution           : {dist}")
    print(f"resident normal points     : {mgr.normal_resident_points:,}")
    print(f"normal GPU bytes           : {mgr.normal_gpu_bytes:,}")
    print(f"normal RAM bytes           : {mgr.normal_cache.resident_bytes:,}")
    print(f"illegal persistent overlap : "
          f"{tele['illegal_persistent_overlap_pairs']}")
    print(f"normal_source              : {mgr.normal_source}")
    print("-" * 72)
    print(f"ram_cache blocks           : {len(mgr.ram_cache)}")
    print(f"pending blocks             : {len(mgr._pending)}")
    print(f"requests/completed         : {mgr.ram_misses}/{mgr.completed}")
    print(f"resident dict              : {len(mgr.resident)}")
    print(f"last_specs len             : {len(getattr(mgr, '_last_specs', None) or [])}")
    states = {}
    for kk, vv in mgr.resident.items():
        states[vv.state] = states.get(vv.state, 0) + 1
    print(f"resident states            : {states}")
    inc = [kk for kk, vv in mgr.resident.items() if not vv.complete]
    print(f"incomplete blocks          : {len(inc)}")
    for kk in inc[:3]:
        rd = mgr.resident[kk].readiness
        print(f"    {kk} missing={rd.missing() if rd is not None else '?'} err={mgr.resident[kk].normal_error[:70]}")
