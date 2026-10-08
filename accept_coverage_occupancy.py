"""Source-supported occupancy proof: does a selected frame cover the ground?

Answers the bug report directly, on a REAL cache and a REAL viewport:

    * build a source occupancy grid from every leaf's LOD0 (what the data
      actually covers)
    * for each zoom level and interaction state, run the production selector
      with a real viewport, read exactly the tiles it selects, and rasterise
      what would reach the screen
    * report missing source cells and the largest connected missing region

Run it on a cache built with the fixed Morton key/shard prefix/splice to see
the "after" numbers, and on one built with the old code to see the defect.
"""
import os
import sys
import time
from collections import deque

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from gui.naksha_cache.reader import NakshaPointCacheReader        # noqa: E402
from gui.naksha_cache.normal_streaming import ScreenDensityBudget  # noqa: E402
from naksha_lod_gate import Camera2D, ScreenSpaceLOD               # noqa: E402

LADDER = [("FIT", 1.0), ("x2", 0.5), ("x4", 0.25), ("x8", 0.125),
          ("x16", 0.0625), ("x32", 0.03125)]
CELL = 10.0          # 10 m hard diagnostic (Part 2 of the coverage mission)


def grid_index(xy, gmin, shape):
    gx = np.floor((xy[:, 0] - gmin[0]) / CELL).astype(np.int64)
    gy = np.floor((xy[:, 1] - gmin[1]) / CELL).astype(np.int64)
    gx = np.clip(gx, 0, shape[0] - 1)
    gy = np.clip(gy, 0, shape[1] - 1)
    return gy * shape[0] + gx


def largest_component(mask, shape):
    """4-connected largest run of missing cells, in cells."""
    seen = np.zeros(mask.shape, dtype=bool)
    best = 0
    total = int(mask.sum())
    if total == 0:
        return 0, 0
    for start in np.flatnonzero(mask.ravel() & ~seen.ravel()):
        r0, c0 = divmod(int(start), shape[0])
        if seen[r0, c0]:
            continue
        q = deque([(r0, c0)])
        seen[r0, c0] = True
        size = 0
        while q:
            r, c = q.popleft()
            size += 1
            for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nr, nc = r + dr, c + dc
                if 0 <= nr < shape[1] and 0 <= nc < shape[0] \
                        and mask[nr, nc] and not seen[nr, nc]:
                    seen[nr, nc] = True
                    q.append((nr, nc))
        best = max(best, size)
    return total, best


def visible_rows(idx, cx, cy, hw, hh):
    n = idx.nodes
    bmin = np.asarray(n["bounds_min"], dtype=np.float64)
    bmax = np.asarray(n["bounds_max"], dtype=np.float64)
    m = ((bmax[:, 0] >= cx - hw) & (bmin[:, 0] <= cx + hw) &
         (bmax[:, 1] >= cy - hh) & (bmin[:, 1] <= cy + hh))
    return np.flatnonzero(m)


def main():
    if len(sys.argv) < 2:
        print("usage: accept_coverage_occupancy.py <dataset>")
        return 2
    src = sys.argv[1]
    w, h = 1400, 731                     # the viewport in the bug report
    reader = NakshaPointCacheReader(src, verify_crc=False, load_edits=False)
    idx = reader.index
    n = idx.nodes

    t0 = time.perf_counter()
    hdr = idx.header
    gmin = np.asarray(hdr["bounds_min"], float)
    gmax = np.asarray(hdr["bounds_max"], float)
    shape = (int(np.ceil((gmax[0] - gmin[0]) / CELL)) + 1,
             int(np.ceil((gmax[1] - gmin[1]) / CELL)) + 1)
    src_grid = np.zeros(shape[0] * shape[1], dtype=bool)
    src_count = np.zeros(shape[0] * shape[1], dtype=np.int64)
    lod0_total = 0
    for row in range(n.size):
        # render_space=False -> float64 WORLD coordinates. The render path's
        # float32 variant is tile-local, so rasterising with it collapses every
        # tile onto one cell.
        t = reader.read_tile(int(n["node_id"][row]), 0, render_space=False,
                             apply_edits=False, verify_crc=False)
        if not t or t.get("xyz") is None:
            continue
        xyz = np.asarray(t["xyz"])
        lod0_total += xyz.shape[0]
        flat = grid_index(xyz[:, :2], gmin, shape)
        src_grid[flat] = True
        np.add.at(src_count, flat, 1)
    src_ms = (time.perf_counter() - t0) * 1000

    # Zoom into DATA, not into the dataset centroid: a survey can have a real
    # void at its centre, and zooming into a void would report zero visible
    # nodes as if it were a culling defect.
    pc = np.asarray(n["point_count"], float)
    densest = int(np.argmax(pc))
    bmin = np.asarray(n["bounds_min"], float)
    bmax = np.asarray(n["bounds_max"], float)
    cx = float((bmin[densest][0] + bmax[densest][0]) * 0.5)
    cy = float((bmin[densest][1] + bmax[densest][1]) * 0.5)
    span_x = float(gmax[0] - gmin[0])
    print(f"  zoom centre = densest leaf {int(n['node_id'][densest])} at "
          f"({cx:.1f}, {cy:.1f})")
    wpx_all = bmax[:, 0] - bmin[:, 0]
    hpx_all = bmax[:, 1] - bmin[:, 1]
    node_area = float(np.sum(wpx_all * hpx_all))
    ds_area = float((gmax[0] - gmin[0]) * (gmax[1] - gmin[1]))

    print(f"\nDATASET {os.path.basename(src)}")
    print(f"  nodes {n.size}  lod0 points {lod0_total:,}  "
          f"source occupancy {int(src_grid.sum())} cells of "
          f"{shape[0] * shape[1]}  ({CELL:.0f} m)")
    print(f"  sum(leaf area)/dataset = {node_area / ds_area:.3f}x")
    print(f"  source grid built in {src_ms:.0f} ms\n")

    print(f"  {'zoom':<5}{'state':<8}{'vis':>4}{'sel_pts':>10}{'ppp':>7}"
          f"{'LOD dist':<22}{'exp':>5}{'got':>5}{'miss':>5}{'starved':>8}"
          f"{'gap_cells':>10}  {'sweep_ms':>8}")
    worst = 0
    worst_s = 0
    rows_out = []
    for label, frac in LADDER:
        for moving in (True, False):
            cam = Camera2D(cx, cy, span_x * .5 * frac, w, h)
            vis = visible_rows(idx, cam.cx, cam.cy, cam.half_w, cam.height_m)
            lod = ScreenSpaceLOD()
            ScreenDensityBudget().apply(lod, w, h, moving=moving)
            t1 = time.perf_counter()
            sel = lod.select(idx, cam, list(vis))
            sel_pts = sum(int(o["points"]) for o in sel)

            drawn = np.zeros(shape[0] * shape[1], dtype=np.int64)
            for o in sel:
                t = reader.read_tile(int(o["node_id"]), int(o["lod"]),
                                     render_space=False, apply_edits=False,
                                     verify_crc=False)
                if not t or t.get("xyz") is None:
                    continue
                xyz = np.asarray(t["xyz"])
                np.add.at(drawn, grid_index(xyz[:, :2], gmin, shape), 1)
            drawn_mask = drawn > 0
            sweep_ms = (time.perf_counter() - t1) * 1000

            # cells inside the view that the source covers
            inview = np.zeros(shape[0] * shape[1], dtype=bool)
            gx = np.arange(shape[0])
            gy = np.arange(shape[1])
            XX = gmin[0] + (gx + .5) * CELL
            YY = gmin[1] + (gy + .5) * CELL
            inview_grid = ((XX[None, :] >= cam.cx - cam.half_w) &
                           (XX[None, :] <= cam.cx + cam.half_w) &
                           (YY[:, None] >= cam.cy - cam.height_m) &
                           (YY[:, None] <= cam.cy + cam.height_m))
            inview[:] = inview_grid.ravel()
            expected = src_grid & inview
            missing = expected & ~drawn_mask
            # A cell can be "covered" by one lonely point while the region is
            # visually empty - that is the reported symptom. Starved = under 5%
            # of the source points that the cell actually holds.
            ratio = np.zeros(shape[0] * shape[1], dtype=np.float64)
            np.divide(drawn, np.maximum(src_count, 1), out=ratio,
                      where=expected)
            starved = expected & (ratio < 0.05)
            exp_n = int(expected.sum())
            miss_n = int(missing.sum())
            starv_n = int(starved.sum())
            _, big = largest_component(missing.reshape(shape[1], shape[0]),
                                       shape)
            _, big_s = largest_component(starved.reshape(shape[1], shape[0]),
                                         shape)
            worst = max(worst, big)
            worst_s = max(worst_s, big_s)

            dist = {}
            for o in sel:
                dist[o["lod"]] = dist.get(o["lod"], 0) + 1
            ds = " ".join(f"L{k}:{v}" for k, v in sorted(dist.items()))
            state = "MOVING" if moving else "IDLE"
            print(f"  {label:<5}{state:<8}{len(vis):>4}{sel_pts:>10,}"
                  f"{sel_pts / (w * h):>7.3f}  {ds:<22}{exp_n:>5}"
                  f"{exp_n - miss_n:>5}{miss_n:>5}{starv_n:>8}"
                  f"{big_s:>10}  {sweep_ms:>8.0f}")
            rows_out.append((label, state, len(vis), sel_pts, exp_n, miss_n,
                             starv_n, big_s))
    print(f"\n  largest connected EMPTY source region   : {worst} cells "
          f"({worst * CELL * CELL:,.0f} m^2)")
    print(f"  largest connected STARVED source region : {worst_s} cells "
          f"({worst_s * CELL * CELL:,.0f} m^2)  "
          f"(<5% of the cell's own source points)")
    reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
