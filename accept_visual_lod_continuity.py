"""accept_visual_lod_continuity.py - PHASE 5 / PART 0: measure tile-to-tile
visual discontinuity of the CURRENT frontier, numerically (not by screenshot).

For every node in the committed ACTIVE_DRAWN frontier it records

    node_id, lod, source_points, draw_points, projected_visible_px, PPP,
    representation fraction (= draw_points / source_points), parent id

then, for every pair of EDGE-ADJACENT frontier nodes:

    LOD difference, PPP ratio, representation-fraction ratio

PPP ratios use the part of the node that is actually VISIBLE (clipped to the
viewport) - an unclipped box over-reports area for nodes that are mostly off
screen. The representation fraction is SOURCE-NORMALISED: a naturally sparse
node (few source points) has the same fraction as a dense neighbour sampled the
same way, so genuine source-density boundaries are not reported as defects.

OFFLINE / DEV ONLY: the adjacency search is O(frontier^2). Never call it from
the GUI tick (see tests/test_streaming_visual_continuity.py guardrail).

    py accept_visual_lod_continuity.py            # real 27M cache
"""
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

# Metres. Leaf bounds are TIGHT boxes around each leaf's points, so edge-
# adjacent leaves are separated by the point-quantisation step (0.01 m on the
# real data), not by exactly 0. Measured on the corrected cache: every nearest
# edge gap is 0.01 m, and there are 0 overlapping pairs.
EDGE_TOL = 0.1


def frontier_records(mgr, viewport, width_px, height_px):
    """One dict per ACTIVE leaf node (the overview/pyramid root is excluded)."""
    idx = mgr.idx
    nodes = idx.nodes
    nid = np.asarray(nodes["node_id"]).astype(np.int64)
    row_of = {int(n): i for i, n in enumerate(nid)}
    bmin = np.asarray(nodes["bounds_min"], np.float64)
    bmax = np.asarray(nodes["bounds_max"], np.float64)
    cx, cy, hw, hh = viewport
    vx0, vx1, vy0, vy1 = cx - hw, cx + hw, cy - hh, cy + hh
    ppm_x = width_px / (2.0 * hw)
    ppm_y = height_px / (2.0 * hh)
    lod_counts = np.asarray(nodes["lod_point_count"]).astype(np.int64)
    recs = []
    ov_key = mgr._overview_key()
    for key in (mgr.active_draw_keys or ()):
        n, lod = int(key[0]), int(key[1])
        # The overview is stored as (node 0, coarsest LOD) and has NO node-table
        # row, while the first real leaf is ALSO node 0: exclude it by its full
        # (node, lod) key, never by node id alone.
        if ov_key is not None and (n, lod) == tuple(ov_key):
            continue
        r = row_of.get(n)
        v = mgr.resident.get(key)
        if r is None or v is None:
            continue
        ix0, ix1 = max(bmin[r, 0], vx0), min(bmax[r, 0], vx1)
        iy0, iy1 = max(bmin[r, 1], vy0), min(bmax[r, 1], vy1)
        vis_w, vis_h = max(ix1 - ix0, 0.0), max(iy1 - iy0, 0.0)
        px = vis_w * ppm_x * vis_h * ppm_y
        full_w = (bmax[r, 0] - bmin[r, 0]) * ppm_x
        full_h = (bmax[r, 1] - bmin[r, 1]) * ppm_y
        src = int(lod_counts[r].max())                # finest LOD == the source
        draw = int(v.count)
        # the fraction of the node on screen scales the visible point count
        vis_frac = (vis_w * vis_h) / max(
            (bmax[r, 0] - bmin[r, 0]) * (bmax[r, 1] - bmin[r, 1]), 1e-12)
        sp_all = np.asarray(nodes["lod_spacing"][r], dtype=np.float64)
        sp_m = float(sp_all[lod]) if 0 <= lod < sp_all.size else 0.0
        recs.append({
            # apparent point spacing on screen: SOURCE-AWARE (a naturally sparse
            # leaf has a wide measured spacing at LOD0), so equal spacing across
            # a boundary means no visible seam whatever the source density.
            "spacing_px": sp_m * ppm_x if sp_m > 0 else float("nan"),
            "node_id": n, "lod": lod, "source_points": src,
            "draw_points": draw, "visible_px": px,
            "unclipped_px": full_w * full_h,
            "ppp": (draw * vis_frac) / px if px > 1.0 else float("nan"),
            "rep_fraction": draw / max(src, 1),
            "resident_state": str(v.state), "active": True,
            "bounds": (bmin[r, 0], bmin[r, 1], bmax[r, 0], bmax[r, 1]),
            "row": r,
        })
    return recs


def adjacency(recs):
    """Edge-adjacent pairs (a, b) of frontier records. O(n^2), offline."""
    out = []
    for i in range(len(recs)):
        ax0, ay0, ax1, ay1 = recs[i]["bounds"]
        for j in range(i + 1, len(recs)):
            bx0, by0, bx1, by1 = recs[j]["bounds"]
            ox = min(ax1, bx1) - max(ax0, bx0)
            oy = min(ay1, by1) - max(ay0, by0)
            touch_x = (abs(ax1 - bx0) < EDGE_TOL or abs(bx1 - ax0) < EDGE_TOL) \
                and oy > EDGE_TOL
            touch_y = (abs(ay1 - by0) < EDGE_TOL or abs(by1 - ay0) < EDGE_TOL) \
                and ox > EDGE_TOL
            if touch_x or touch_y:
                out.append((i, j))
    return out


def seam_metrics(recs, pairs, min_px=400.0):
    """Neighbour discontinuity. Pairs where either side is < min_px visible
    pixels are ignored (a sliver at the viewport edge has no meaningful PPP)."""
    ppp_ratio, frac_ratio, lod_diff, worst = [], [], [], []
    spacing_ratio = []
    for i, j in pairs:
        a, b = recs[i], recs[j]
        if a["visible_px"] < min_px or b["visible_px"] < min_px:
            continue
        if not (np.isfinite(a["ppp"]) and np.isfinite(b["ppp"])):
            continue
        hi, lo = max(a["ppp"], b["ppp"]), max(min(a["ppp"], b["ppp"]), 1e-9)
        fa, fb = max(a["rep_fraction"], 1e-9), max(b["rep_fraction"], 1e-9)
        pr = hi / lo
        fr = max(fa, fb) / min(fa, fb)
        ld = abs(a["lod"] - b["lod"])
        sa, sb = a["spacing_px"], b["spacing_px"]
        sr = (max(sa, sb) / max(min(sa, sb), 1e-9)
              if np.isfinite(sa) and np.isfinite(sb) else float("nan"))
        ppp_ratio.append(pr)
        frac_ratio.append(fr)
        lod_diff.append(ld)
        spacing_ratio.append(sr)
        worst.append((sr if np.isfinite(sr) else 0.0, fr, pr, ld,
                      a["node_id"], a["lod"], a["draw_points"], a["ppp"], sa,
                      b["node_id"], b["lod"], b["draw_points"], b["ppp"], sb))
    worst.sort(reverse=True)
    pct = lambda v, q: float(np.nanpercentile(v, q)) if len(v) else float("nan")  # noqa: E731
    return {
        "spacing_ratio_p95": pct(spacing_ratio, 95),
        "spacing_ratio_max": (float(np.nanmax(spacing_ratio))
                              if len(spacing_ratio) else float("nan")),
        "pairs": len(ppp_ratio),
        "ppp_ratio_max": max(ppp_ratio) if ppp_ratio else float("nan"),
        "ppp_ratio_p95": pct(ppp_ratio, 95),
        "frac_ratio_max": max(frac_ratio) if frac_ratio else float("nan"),
        "frac_ratio_p95": pct(frac_ratio, 95),
        "lod_diff_max": max(lod_diff) if lod_diff else 0,
        "lod_diff_gt1": int(sum(1 for d in lod_diff if d > 1)),
        "lod_diff_hist": {int(k): int(sum(1 for d in lod_diff if d == k))
                          for k in sorted(set(lod_diff))},
        "worst": worst[:5],
    }


def main():
    import perf_camera_phase4 as P
    from naksha_lod_gate import Camera2D
    mgr, adapter, cx, cy, half = P.build()
    W, H = P.W, P.H
    print(f"\n[VISUAL LOD CONTINUITY]  real hierarchy, {W}x{H}")
    print(f"{'view':<10}{'state':<8}{'nodes':>6}{'pairs':>7}{'lod_max':>8}"
          f"{'lod>1':>7}{'frac_p95':>10}{'frac_max':>10}{'ppp_p95':>9}"
          f"{'ppp_max':>9}{'pts':>11}{'ppp_med':>8}   LOD mix")
    rows = []
    for label, f in (("fit", 1.0), ("x2", 0.5), ("x4", 0.25), ("x8", 0.125)):
        for state, moving in (("MOVING", True), ("IDLE", False)):
            cam = Camera2D(cx, cy, half * f, W, H)
            vp = (cx, cy, half * f, half * f * H / W)
            if moving:
                mgr.note_camera_motion()
                mgr.on_frame(cam, vp)
            else:
                for _ in range(40):
                    mgr.on_frame(cam, vp)
                    if getattr(mgr, "_camera_state", "") == "IDLE":
                        break
                    time.sleep(0.03)
                mgr.on_frame(cam, vp)
            recs = frontier_records(mgr, vp, W, H)
            pairs = adjacency(recs)
            m = seam_metrics(recs, pairs)
            mix = {}
            for r in recs:
                mix[r["lod"]] = mix.get(r["lod"], 0) + 1
            print(f"{label:<10}{state:<8}{len(recs):>6}{m['pairs']:>7}"
                  f"{m['lod_diff_max']:>8}{m['lod_diff_gt1']:>7}"
                  f"{m['frac_ratio_p95']:>10.2f}{m['frac_ratio_max']:>10.2f}"
                  f"{m['ppp_ratio_p95']:>9.2f}{m['ppp_ratio_max']:>9.2f}"
                  f"{sum(r['draw_points'] for r in recs):>11,}"
                  f"{np.nanmedian([r['ppp'] for r in recs]):>8.2f}   "
                  + " ".join(f"L{k}:{v}" for k, v in sorted(mix.items())))
            rows.append((label, state, m))
    print("\nspacing_px ratio across neighbours (source-aware apparent-density seam):")
    for label, state, m in rows:
        print(f"  {label:<4}{state:<7} spacing_ratio p95 x{m['spacing_ratio_p95']:.2f} "
              f"max x{m['spacing_ratio_max']:.2f}")
    print("\nworst seam per view (by spacing ratio):")
    for label, state, m in rows:
        if m["worst"]:
            w = m["worst"][0]
            print(f"  {label:<4}{state:<7} spacing x{w[0]:.1f} frac x{w[1]:.1f} "
                  f"ppp x{w[2]:.1f} dLOD {w[3]} | A node {w[4]}/L{w[5]} "
                  f"pts={w[6]:,} ppp={w[7]:.2f} sp={w[8]:.2f}px | B node "
                  f"{w[9]}/L{w[10]} pts={w[11]:,} ppp={w[12]:.2f} sp={w[13]:.2f}px")
    print("\nLOD-difference histogram across adjacent pairs (MOVING):")
    for label, state, m in rows:
        if state == "MOVING":
            print(f"  {label:<4}{m['lod_diff_hist']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
