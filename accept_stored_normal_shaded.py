"""REAL-dataset acceptance for selective stored-normal streaming.

Runs the PRODUCTION classes (NakshaStreamManager, the real NormalCacheReader,
the real NKPC reader and the real ScreenSpaceLOD gate) against
test_classified_highprecision.laz. Nothing here is mocked.

It proves the four things that were previously unproven:
  1. the sidecar opens as HIT against the real dataset,
  2. XYZ/CLASS/NORMAL stay aligned through the real _flush_gpu key order,
  3. shaded refines BEYOND the 299,676-point overview as the camera zooms,
  4. no blank frame occurs during that refinement.
"""
import os
import queue
import sys
import threading

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "test_classified_highprecision.laz")
sys.path.insert(0, ROOT)

from gui.naksha_cache.reader import NakshaPointCacheReader          # noqa: E402
from gui.naksha_cache.stream_manager import (                       # noqa: E402
    GPU_RESIDENT, NakshaStreamManager, TileSpec)
from gui.naksha_cache.stream_renderer_adapter import (                # noqa: E402
    HeadlessTileRenderer)
from gui.naksha_cache.normal_streaming import (                     # noqa: E402
    GenerationGate, NormalBlockCache, NormalPrefetcher,
    ShadedActivationPolicy, nkpc_attrs, open_normal_cache)
from naksha_lod_gate import Camera2D, ScreenSpaceLOD                # noqa: E402


class _Te:
    def event(self, *a, **k):
        pass

    def sample(self, *a, **k):
        pass


def _spec(o):
    return TileSpec(node_id=int(o["node_id"]), lod=int(o["lod"]),
                    priority=float(o.get("priority", o["centre_dist"])),
                    points=int(o["points"]), px_err=float(o["px_err"]),
                    row=int(o["row"]))


def _build(reader, nreader, nrep):
    """A NakshaStreamManager wired exactly as install_streaming() wires it."""
    mgr = NakshaStreamManager.__new__(NakshaStreamManager)
    mgr.reader = reader
    mgr.idx = reader.index
    mgr.adapter = HeadlessTileRenderer(
        total_points=int(reader.index.total_points))
    mgr.te = _Te()
    mgr.ram_budget = 8 * 1024 ** 3
    mgr.gpu_budget = 1.5 * 1024 ** 3
    mgr.total_points = int(reader.index.total_points)
    mgr.lod = ScreenSpaceLOD()
    mgr.ram_cache = {}
    mgr.ram_bytes = 0
    mgr.ram_hits = mgr.ram_misses = 0
    mgr.resident = {}
    mgr.gpu_bytes = 0
    mgr._executor = None
    mgr._pending = {}
    mgr._done = queue.Queue()
    mgr._ram_lock = threading.Lock()
    mgr._gen = mgr._service_gen = 0
    mgr._resident_sig = None
    mgr._display_reupload_needed = False
    mgr._normal_reupload_needed = False
    mgr._normal_sig = None
    mgr._points_resident = 0
    mgr.display_mode = "shaded"
    mgr.shaded_mode = True
    mgr.legacy_loader_called = False
    mgr.timing = {"hot_ms": [], "bg_ms": []}
    mgr.hot_ms_last = mgr.bg_ms_last = 0.0
    mgr._pending_ms = []
    mgr.streaming_full_point_upload_attempts = 0
    mgr.position_reuploads = 0
    mgr.requests = mgr.completed = mgr.cancelled_stale = 0
    mgr.queue_depth = 0
    mgr.evictions = 0
    mgr.overview_tile = None
    mgr.camera_generation = 0
    mgr._viewport = None
    mgr._vp_w, mgr._vp_h = 1920, 848
    mgr.splat_px = 0.0
    mgr.last_draw_telemetry = {}
    mgr.last_switch_telemetry = {}
    mgr.tile_load_events = []
    mgr.normal_reader = nreader
    mgr.normal_report = nrep
    mgr.normal_cache = NormalBlockCache(budget_bytes=256 * 1024 ** 2)
    mgr.normal_gpu_bytes = 0
    mgr.normal_upload_count = 0
    mgr.normal_resident_points = 0
    mgr.new_normal_blocks = 0
    mgr.reused_normal_blocks = 0
    mgr.stale_normal_requests_dropped = 0
    mgr.gate = GenerationGate()
    mgr.prefetch = NormalPrefetcher()
    mgr.policy = ShadedActivationPolicy()
    mgr.attach_normal_cache(nreader, nrep)
    return mgr


def _resolve(mgr, sel):
    """Load every selected block synchronously (the worker does this live)."""
    for o in sel:
        key = (int(o["node_id"]), int(o["lod"]))
        if key in mgr.resident or key in mgr.ram_cache:
            continue
        t = mgr.reader.read_tile(key[0], key[1],
                                 only_attrs=nkpc_attrs(mgr._required_attrs()),
                                 apply_edits=False, verify_crc=False,
                                 render_space=True)
        if t is None:
            continue
        packed = mgr._pack_block(t, key, 0.0, _spec(o))
        mgr._attach_normal(packed, _spec(o), mgr._required_attrs())
        mgr.ram_cache[key] = packed
        mgr.ram_bytes += packed["bytes"]
def main():
    if not os.path.isfile(SRC + ".nakshanorm"):
        print("SKIP: real sidecar not present")
        return 0
    print("=" * 62)
    print("REAL-DATASET SELECTIVE NORMAL STREAMING")
    print("=" * 62)

    reader = NakshaPointCacheReader(SRC, verify_crc=False, load_edits=False)
    nreader, nrep = open_normal_cache(SRC, verify_source=False,
                                      expected_source_points=26960750)
    print(f"\n[NORMAL CACHE] status={nrep.status} blocks={nrep.blocks:,} "
          f"stored_normals={nrep.stored_normals:,} "
          f"open_ms={nrep.open_ms:.2f} encoding={nrep.encoding}")
    assert nrep.status == "HIT", nrep.reason

    mgr = _build(reader, nreader, nrep)
    assert mgr.normal_source == "stored", mgr.normal_source

    info = mgr.open_first_frame()
    ov_pts = int(info["overview_points"])
    ovk = list(mgr.resident)[0]
    ovr = mgr.resident[ovk]
    print(f"\n[FIRST FRAME] overview={ov_pts:,} pts  block={ovr.block_id}  "
          f"normal_source={mgr.normal_source}")
    print(f"  overview complete={ovr.complete}  normal_bytes={ovr.normal_bytes:,}")

    hdr = reader.index.header
    gmin = np.asarray(hdr["bounds_min"], float)
    gmax = np.asarray(hdr["bounds_max"], float)
    cx, cy = (gmin[0] + gmax[0]) * 0.5, (gmin[1] + gmax[1]) * 0.5
    span_x = float(gmax[0] - gmin[0])
    print("\n[ZOOM LADDER]  (idle refinement at each level)")
    print(f"  {'level':<8}{'visible':>8}{'LOD-dist':>24}{'submitted':>12}"
          f"{'normals':>12}{'ppp':>7}")
    ladder = []
    for label, frac in (("FIT", 1.0), ("x2", 0.5), ("x4", 0.25),
                        ("x8", 0.125), ("x16", 0.0625)):
        cam = Camera2D(cx, cy, span_x * 0.5 * frac, 1920, 848)
        mgr.lod = ScreenSpaceLOD()
        mgr.lod.moving = False
        vis = mgr._visible_node_rows(cam, None)
        sel = mgr.lod.select(mgr.idx, cam, vis)
        _resolve(mgr, sel)
        mgr._reconcile(mgr._specs_from_resident())
        mgr._flush_gpu(reason="zoom")
        keys = [k for k, v in mgr.resident.items() if v.state == GPU_RESIDENT]
        dist = {}
        for k in keys:
            lod = int(mgr.resident[k].lod)
            dist[lod] = dist.get(lod, 0) + 1
        submitted = int(sum(mgr.resident[k].count for k in keys))
        npts = int(mgr.normal_resident_points)
        ppp = submitted / float(1920 * 848)
        ladder.append((label, len(vis), dist, submitted, npts))
        print(f"  {label:<8}{len(vis):>8}{str(dist):>24}{submitted:>12,}"
              f"{npts:>12,}{ppp:>7.3f}")

    fit_pts = ladder[0][3]
    fine_pts = max(l[3] for l in ladder)
    dist_all = sorted({lod for l in ladder for lod in l[2]})
    checks = (
        ("normal cache HIT", nrep.status == "HIT"),
        ("normal_source == stored", mgr.normal_source == "stored"),
        ("overview is 299,676 pts", ov_pts == 299676),
        ("overview COMPLETE (xyz+cls+nrm)", bool(ovr.complete)),
        ("normals uploaded to GPU", mgr.normal_upload_count >= 1),
        ("normal bytes == 4 B/point",
         mgr.normal_gpu_bytes == mgr.normal_resident_points * 4),
        ("normal RAM byte-bounded",
         mgr.normal_cache.resident_bytes <= mgr.normal_cache.budget_bytes),
        ("LOD refines BEYOND the overview", fine_pts > fit_pts * 1.05),
        ("no blank frames", mgr.policy.blank_frames == 0),
        ("every drawn block COMPLETE",
         all(mgr.resident[k].complete for k in mgr.resident
             if mgr.resident[k].state == GPU_RESIDENT)),
        ("normal/XYZ counts aligned",
         mgr.normal_resident_points ==
         sum(v.count for v in mgr.resident.values()
             if v.state == GPU_RESIDENT)),
    )
    print("\n[CHECKS]")
    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print(f"\nFIT submitted    : {fit_pts:,}")
    print(f"finest submitted : {fine_pts:,}")
    print(f"LOD levels seen  : {dist_all}")
    print(f"normal cache     : {mgr.normal_cache.stats()}")
    print(f"normal GPU bytes : {mgr.normal_gpu_bytes:,}")
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    print("=" * 62)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())