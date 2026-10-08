"""diag_frontier_uniformity.py - why source-covered regions look EMPTY.

The user's screenshots show: a few dense tile-like rectangles surviving while
large surrounding source-covered regions go almost empty, and the sparse regions
MOVE with the camera.

That is a signature of UNEVEN BUDGET ALLOCATION, not of missing coverage: if the
point budget is spent by keeping a handful of nodes at their FINEST level and
collapsing every other visible node to its COARSEST level, then no region is
actually dropped - but most regions are represented by so few points that they
look empty, while the lucky few look dense. Which ones are lucky changes with the
camera, so the "holes" move.

This measures that directly, per visible node, for the REAL 123.las cache:

  * the LOD each visible node was given
  * the points that node contributes
  * the node's own points-per-pixel  <-- the number that decides dense vs empty
  * the spread of that number across nodes (min / p25 / median / p75 / max)
  * how many nodes are below a "visibly empty" threshold

A uniform frontier has a TIGHT per-node ppp spread. A patchwork has a huge one.

Run:  venv\\Scripts\\python.exe diag_frontier_uniformity.py
"""
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from gui.naksha_cache.reader import NakshaPointCacheReader            # noqa: E402
from gui.naksha_cache.stream_manager import (                         # noqa: E402
    GPU_RESIDENT, RENDER_STREAMING_LOD, NakshaStreamManager,
    _ResidentIndexView,
)
from gui.naksha_cache.stream_renderer_adapter import HeadlessTileRenderer  # noqa: E402
from gui.naksha_cache.stream_telemetry import StreamTelemetry          # noqa: E402
from naksha_lod_gate import Camera2D                                  # noqa: E402

SRC = os.path.join(ROOT, "diagnostics", "data", "123.las")
TE_PATH = os.path.join(ROOT, "diagnostics", "frontier_uniformity.jsonl")
W, H = 1400, 731          # the user's reported viewport

# Below this per-node points-per-pixel a node's footprint reads as "empty" on
# screen: a few hundred points scattered across a whole tile-sized area. It is a
# VISUAL threshold, deliberately not tied to any internal constant.
EMPTY_PPP = 0.02


class _App:
    pass


def _settle(mgr, cam, vp, frames=40):
    for _ in range(frames):
        mgr.on_frame(cam, vp)
        if getattr(mgr, "_camera_state", None) == "IDLE":
            return True
        time.sleep(0.02)
    return False


def _report(mgr, cam, vp, label, interaction, width, height):
    """Per-node density audit for one camera/state."""
    nodes = mgr._resident_lod_nodes()
    if nodes is None:
        print("  no index nodes")
        return None
    view = _ResidentIndexView(nodes)
    mgr.density.apply(mgr.lod, mgr._vp_w, mgr._vp_h, moving=bool(interaction))
    target = int(getattr(mgr.density, "last_target", 0) or 0)
    rows = mgr._visible_node_rows(cam, vp)
    sel = mgr.lod.select(view, cam, rows)
    if not sel:
        print(f"  {label}: EMPTY selection")
        return None
    ppp, lods, pts = [], {}, 0
    for o in sel:
        n = int(o["points"])
        area = max(float(o["proj_px"]), 1.0)
        ppp.append(n / area)
        pts += n
        lods[int(o["lod"])] = lods.get(int(o["lod"]), 0) + 1
    ppp = np.asarray(ppp, dtype=float)
    med = float(np.median(ppp))
    empty = int((ppp < EMPTY_PPP).sum())
    print(f"\n  {label}  ({'MOVING' if interaction else 'IDLE'})")
    print(f"    budget target        : {target:,}")
    print(f"    visible nodes        : {len(sel)}")
    print(f"    selected points      : {pts:,}   ({pts / max(target,1):.3f}x target)")
    print(f"    LOD distribution     : "
          f"{' '.join(f'L{k}:{v}' for k, v in sorted(lods.items()))}")
    print(f"    per-node ppp         : min={ppp.min():.4f} p25="
          f"{np.percentile(ppp, 25):.4f} median={med:.4f} p75="
          f"{np.percentile(ppp, 75):.4f} max={ppp.max():.4f}")
    print(f"    max/median ratio     : {ppp.max() / max(med, 1e-9):.1f}x")
    print(f"    nodes below {EMPTY_PPP} ppp : {empty} of {len(ppp)} "
          f"({100.0 * empty / len(ppp):.1f}%)  <- these read as EMPTY on screen")
    total_area = float(sum(max(float(o["proj_px"]), 1.0) for o in sel))
    starved_area = float(sum(max(float(o["proj_px"]), 1.0)
                             for o, p in zip(sel, ppp) if p < EMPTY_PPP))
    print(f"    starved screen area  : {100.0 * starved_area / max(total_area, 1.0):.1f}%"
          f" of all visible node area")
    # how LARGE is a starved node on screen? a starved tile filling many
    # thousands of pixels is exactly the reported "large empty region".
    big = [(int(o["node_id"]), int(o["lod"]), int(o["points"]),
            float(o["proj_px"]), float(p))
           for o, p in zip(sel, ppp) if p < EMPTY_PPP]
    big.sort(key=lambda t: -t[3])
    for nid, lod, n, proj, p in big[:5]:
        print(f"      STARVED node={nid:<5} lod={lod} pts={n:>7,} "
              f"proj={proj:>10,.0f}px ppp={p:.5f}")
    return {"label": label, "target": target, "nodes": len(sel), "points": pts,
            "lods": lods, "empty": empty, "empty_pct": 100.0 * empty / len(ppp),
            "max_over_median": float(ppp.max() / max(med, 1e-9)),
            "starved_area_pct": 100.0 * starved_area / max(total_area, 1.0)}


def main():
    if not os.path.isfile(SRC + ".nakshaidx"):
        print(f"SKIP: no cache beside {SRC}")
        return 0
    print("=" * 78)
    print("FRONTIER DENSITY UNIFORMITY - why source-covered regions look empty")
    print("=" * 78)
    print(f"dataset : {SRC}")
    print(f"viewport: {W}x{H}  (the user's reported viewport)")

    reader = NakshaPointCacheReader(SRC, verify_crc=False, load_edits=False)
    hdr = reader.index.header
    gmin = np.asarray(hdr["bounds_min"], float)
    gmax = np.asarray(hdr["bounds_max"], float)
    cx, cy = (gmin[0] + gmax[0]) * 0.5, (gmin[1] + gmax[1]) * 0.5
    span_x = float(gmax[0] - gmin[0])
    half = span_x * 0.5

    def cam_at(frac):
        hw = half * frac
        return (Camera2D(cx, cy, hw, W, H), (cx, cy, hw, hw * H / float(W)))

    adapter = HeadlessTileRenderer(total_points=int(reader.index.total_points))
    te = StreamTelemetry(TE_PATH)
    mgr = NakshaStreamManager(_App(), reader, adapter, te,
                              ram_budget=4 * 1024 ** 3,
                              gpu_budget=2 * 1024 ** 3)
    cam, vp = cam_at(1.0)
    t0 = time.perf_counter()
    mgr.open_first_frame()
    print(f"open_first_frame : {(time.perf_counter() - t0) * 1000.0:.0f} ms")
    t0 = time.perf_counter()
    for _ in range(600):
        mgr.on_frame(cam, vp)
        if getattr(mgr, "full_resident_ready", False):
            break
    print(f"full_resident    : {getattr(mgr, 'full_resident_ready', False)} "
          f"in {(time.perf_counter() - t0) * 1000.0:.0f} ms")
    keys = [k for k, v in mgr.resident.items() if v.state == GPU_RESIDENT]
    print(f"GPU_RESIDENT     : {len(keys)} blocks / "
          f"{sum(int(mgr.resident[k].count) for k in keys):,} points")
    print(f"screen_space_applies: {mgr._screen_space_applies()}")

    out = []
    for frac, label in ((1.0, "FIT"), (0.5, "x2"), (0.25, "x4"), (0.125, "x8")):
        cam, vp = cam_at(frac)
        _settle(mgr, cam, vp)
        r = _report(mgr, cam, vp, label, False, W, H)
        if r:
            out.append(r)
        # the same view while the camera is MOVING
        mgr.note_camera_motion()
        mgr.on_frame(cam, vp)
        r = _report(mgr, cam, vp, label, True, W, H)
        if r:
            out.append(r)
        time.sleep(0.05)

    print("\n" + "=" * 78)
    worst = max(out, key=lambda r: r["empty_pct"]) if out else None
    if worst:
        print(f"WORST CASE : {worst['label']} "
              f"{'MOVING' if worst['target'] < 600000 else 'IDLE'} -> "
              f"{worst['empty']} of {worst['nodes']} visible nodes "
              f"({worst['empty_pct']:.1f}%) are below {EMPTY_PPP} ppp, "
              f"covering {worst['starved_area_pct']:.1f}% of visible node area")
        print(f"max/median per-node ppp ratio: {worst['max_over_median']:.1f}x "
              f"(1.0x would be a perfectly uniform frontier)")
    reader.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
