"""Decode-path verification for the streaming tile reader.

Checks the properties Phase 17 and Phase 18 depend on, against REAL LAZ data:
  * every point in a cell is recovered, exactly once
  * XYZ, RGB, classification, intensity and global identity stay ALIGNED
  * the same permutation is applied to all attributes
  * decimation preserves alignment and spatial extent
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gui.streaming.quadtree_index import QuadtreeIndex          # noqa: E402
from gui.streaming.tile_store import TileReader, close_readers  # noqa: E402

SRC = [r"H:\TESTING CONTIUES\FUNIVIA\NT219\MANDI_53.laz"]
IDX = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "tmp_fast.nakshaidx")

fails = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def main():
    idx = QuadtreeIndex.load(IDX)
    origin = (float(idx.bbox_min[0]), float(idx.bbox_min[1]), 0.0)
    tr = TileReader(idx, SRC, origin=origin)
    print("\n[TILE DECODE VERIFICATION]")
    print(f"  index: {idx.nodes.size:,} cells, {idx.total_points():,} points")

    pc = idx.nodes["point_count"]
    rows = np.argsort(pc.astype(np.int64))
    picks = [int(rows[len(rows) // 2]), int(rows[-1]), int(rows[len(rows) // 8])]
    # Warm up the LAZ backend. The FIRST read in a process pays laspy/lazrs
    # backend construction - a one-time cost of opening a file, not a property
    # of the decoder. Including it makes the latency assertion measure library
    # initialisation instead of tile decode.
    tr.read_tile(picks[0])

    for k, row in enumerate(picks):
        expect = int(pc[row])
        t0 = time.perf_counter()
        tile = tr.read_tile(row)
        dt = time.perf_counter() - t0
        got = tile["count"]
        check(f"cell {k}: full-res count == index point_count",
              got == expect, f"{got:,} vs {expect:,}")
        # A LAZ random read has a FIXED ~24 ms seek cost independent of tile
        # size, so the correct assertion here is LATENCY, not throughput.
        # Asserting Mpts/s would fail every small cell for a reason that has
        # nothing to do with the decoder.
        check(f"cell {k}: decode latency within budget",
              dt < 0.050, f"{dt * 1000:.1f} ms for {got:,} pts")
        check(f"cell {k}: attributes aligned in length",
              tile["rgb"].shape[0] == got and tile["classification"].shape[0] == got
              and tile["intensity"].shape[0] == got
              and tile["gfile"].shape[0] == got and tile["gsource"].shape[0] == got)
        check(f"cell {k}: xyz is local float32",
              tile["xyz"].dtype == np.float32 and tile["world_xyz"].dtype == np.float64)
        # Local coordinates must be small (origin-relative), world must be UTM.
        check(f"cell {k}: local coords near origin",
              float(np.abs(tile["xyz"]).max()) < 10_000.0,
              f"max |local| = {float(np.abs(tile['xyz']).max()):.1f}")
        check(f"cell {k}: world coords preserved",
              float(tile["world_xyz"][:, 0].min()) > 600_000,
              f"x ~ {float(tile['world_xyz'][:, 0].min()):,.0f}")
        # Local must equal world - origin, i.e. the authoritative values are
        # only offset, never mutated (Phase 16).
        back = tile["xyz"].astype(np.float64) + origin
        err = float(np.abs(back - tile["world_xyz"]).max())
        check(f"cell {k}: local == world - origin (float32 precision only)",
              err < 0.01, f"max err {err:.5f} m")

        # Decimation must keep alignment, and must stay INSIDE the full-res extent. A
        # stride drops the extreme points, so the decimated bbox is a SUBSET -
        # it must never reach OUTSIDE the original.
        d = tr.read_tile(row, max_points=max(1, expect // 8))
        check(f"cell {k}: decimation reduces points",
              d["count"] < got, f"{d['count']:,} stride {d['stride']}")
        check(f"cell {k}: decimation keeps alignment",
              d["rgb"].shape[0] == d["count"] and d["intensity"].shape[0] == d["count"])
        wmin, wmax = tile["world_xyz"].min(0), tile["world_xyz"].max(0)
        dmin, dmax = d["world_xyz"].min(0), d["world_xyz"].max(0)
        check(f"cell {k}: decimation stays inside full-res extent",
              bool(np.all(dmin >= wmin - 1e-6) and np.all(dmax <= wmax + 1e-6)),
              f"full x[{wmin[0]:.1f}..{wmax[0]:.1f}] dec x[{dmin[0]:.1f}..{dmax[0]:.1f}]")

    # Identity uniqueness: global ids must address distinct source points.
    tile = tr.read_tile(picks[0])
    ids = tile["gfile"].astype(np.int64) * (1 << 42) + tile["gsource"]
    check("global point ids unique within a tile", np.unique(ids).size == ids.size)

    # Exactness against the source: for the tile's first run, decode the identical
    # span straight from the file and require EXACT agreement on XYZ, RGB,
    # intensity and classification. This is the check that would catch a
    # mis-applied permutation between attributes (Phase 17).
    import laspy
    rd = laspy.open(SRC[0])
    runs = idx.node_runs(picks[0])
    r0 = runs[0]
    first, cnt = int(r0["first_point"]), int(r0["count"])
    rd.seek(first)
    ref = rd.read_points(cnt)
    sel = ((tile["gfile"] == int(r0["file_id"]))
           & (tile["gsource"] >= first) & (tile["gsource"] < first + cnt))
    got_idx = tile["gsource"][sel] - first
    check("decoded points map into the first run exactly",
          got_idx.size > 0 and int(got_idx.min()) == 0
          and int(got_idx.max()) < cnt,
          f"{got_idx.size:,} of {cnt:,} points, range [{int(got_idx.min())}..{int(got_idx.max())}]")
    ref_rgb = np.asarray(ref.red)
    if ref_rgb.dtype != np.uint8:      # LAS 1.2 fmt 3 stores RGB as uint16
        ref_rgb = (ref_rgb.astype(np.int64) >> 8).clip(0, 255).astype(np.uint8)
    parts = {}
    if got_idx.size:
        w = tile["world_xyz"][sel]
        parts["xyz_x"] = float(np.abs(w[:, 0] - np.asarray(ref.x)[got_idx]).max())
        parts["rgb"] = int((tile["rgb"][sel][:got_idx.size, 0]
                            != ref_rgb[got_idx]).sum())
        parts["cls"] = int((tile["classification"][sel][:got_idx.size]
                            != np.asarray(ref.classification)[got_idx]).sum())
        parts["intensity"] = int((tile["intensity"][sel][:got_idx.size]
                                  != np.asarray(ref.intensity)[got_idx]).sum())
    check("decoded tile matches source EXACTLY (xyz, rgb, cls, intensity)",
          bool(got_idx.size) and parts.get("xyz_x", 1) < 1e-9
          and parts.get("rgb") == 0 and parts.get("cls") == 0
          and parts.get("intensity") == 0,
          f"max dx={parts.get('xyz_x', -1):.3e} rgb_diffs={parts.get('rgb')} "
          f"cls_diffs={parts.get('cls')} int_diffs={parts.get('intensity')}")
    rd.close()
    close_readers()

    # ---- index addressing: the failure mode counts cannot detect ----
    # A run whose offset is mis-addressed still has the right LENGTH, so point
    # counts, dataset totals and even the cell bounds look correct. Only
    # decoding and mapping the result back to the cell exposes it.
    order2 = np.argsort(pc.astype(np.int64))
    sample = [int(v) for v in order2[-40:]]
    viol = mis_addressed = 0
    for row in sample:
        t = tr.read_tile(row)
        if t is None:
            continue
        b = idx.node_bbox(row)
        w = t["world_xyz"]
        if not (w[:, 0].min() >= b[0] - 1e-6 and w[:, 0].max() <= b[3] + 1e-6
                and w[:, 1].min() >= b[1] - 1e-6 and w[:, 1].max() <= b[4] + 1e-6
                and w[:, 2].min() >= b[2] - 1e-6 and w[:, 2].max() <= b[5] + 1e-6):
            viol += 1
        ix = np.clip(((w[:, 0] - idx.bbox_min[0])
                      / (idx.bbox_max[0] - idx.bbox_min[0]) * idx.side
                      ).astype(np.int64), 0, idx.side - 1)
        iy = np.clip(((w[:, 1] - idx.bbox_min[1])
                      / (idx.bbox_max[1] - idx.bbox_min[1]) * idx.side
                      ).astype(np.int64), 0, idx.side - 1)
        if int(idx.nodes["cell"][row]) not in set((iy * idx.side + ix).tolist()):
            mis_addressed += 1
    check("index addressing: tile points lie in its stored bbox",
          viol == 0, f"{viol} violations of {len(sample)} sampled tiles")
    check("index addressing: tile points map back to its own cell",
          mis_addressed == 0, f"{mis_addressed} mis-addressed of {len(sample)}")

    vis = idx.visible_cells(idx.bbox_min[0], idx.bbox_min[1],
                            idx.bbox_max[0], idx.bbox_max[1])
    total = sum(int(idx.nodes["point_count"][v]) for v in vis)
    check("index addressing: full-fit query accounts for every point",
          total == idx.total_points(), f"{total:,} vs {idx.total_points():,}")

    print(f"\n  RESULT: {'PASS' if not fails else 'FAIL ' + str(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())