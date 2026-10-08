"""visual_quality.py - DEV-ONLY screen-space quality map and continuity metrics.

NEVER imported by the GUI tick / camera callback / on_frame (it is O(nodes x
grid)); it is for acceptance harnesses and tests. It answers, in numbers, the
question a screenshot answers by eye: "can you see the tile boundaries?"

For a committed ACTIVE_DRAWN frontier it builds a GRID_X x GRID_Y grid over the
viewport. Per cell:

    support_pts   source points that exist under the cell (LOD0 counts of every
                  node overlapping it, area-weighted)
    active_pts    points actually drawn there (area-weighted active rungs)
    ppp           active_pts / cell pixels
    support_ppp   support_pts / cell pixels  (what the SOURCE could supply)
    rep_fraction  active_pts / support_pts   (source-normalised)
    multiplicity  AREA of the cell covered by active nodes, summed (> 1 ==
                  duplicate cover: parent + child, or the overview over a
                  refined region; a cell straddling a tile edge is still <= 1)
    dominant_lod  rung of the node covering most of the cell

Classification (target = the frame's points-per-pixel budget):
    NO_SOURCE  nothing exists here (outside the survey): ignored everywhere
    EMPTY      source exists, nothing drawn
    STARVED    ppp < 0.15 x target AND the source could supply >= target: the
               cell is poor because of LOD / loading, not because the data is
    LOW        ppp < 0.5 x target (or poor but the source itself is sparse)
    TARGET     0.5 .. 3 x target
    OVERDENSE  ppp > 3 x target, or covered more than once

A naturally sparse region therefore stays "LOW", never "STARVED": the rule is
"same source density -> similar representation", not "equal raw counts".
"""
from __future__ import annotations

from collections import deque

import numpy as np

GRID_X, GRID_Y = 64, 36
# covered area per cell above this == drawn more than once. Tight leaf boxes
# are separated by ~0.01 m, so a clean partition stays at <= 1.0.
DUP_COVER = 1.25
NO_SOURCE, EMPTY, STARVED, LOW, TARGET, OVERDENSE = 0, 1, 2, 3, 4, 5
NAMES = {0: "NO_SOURCE", 1: "EMPTY", 2: "STARVED", 3: "LOW", 4: "TARGET",
         5: "OVERDENSE"}


def _axis_overlap(lo, hi, v0, v1, n):
    """Overlap length of [lo,hi] with each of n equal cells spanning [v0,v1]."""
    edges = np.linspace(v0, v1, n + 1)
    return np.clip(np.minimum(edges[1:], hi) - np.maximum(edges[:-1], lo),
                   0.0, None)


def _components(mask):
    """4-connected components of a boolean grid -> list of sizes (cells)."""
    seen = np.zeros_like(mask, bool)
    sizes = []
    h, w = mask.shape
    for y in range(h):
        for x in range(w):
            if mask[y, x] and not seen[y, x]:
                q = deque([(y, x)])
                seen[y, x] = True
                n = 0
                while q:
                    cy, cx = q.popleft()
                    n += 1
                    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        ny, nx = cy + dy, cx + dx
                        if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] \
                                and not seen[ny, nx]:
                            seen[ny, nx] = True
                            q.append((ny, nx))
                sizes.append(n)
    return sizes


def quality_map(mgr, viewport, width_px, height_px, target_ppp=None,
                grid=(GRID_X, GRID_Y)):
    """Build the quality map of the manager's CURRENT active frontier."""
    gx, gy = grid
    cx, cy, hw, hh = (float(v) for v in viewport[:4])
    x0, x1, y0, y1 = cx - hw, cx + hw, cy - hh, cy + hh
    cell_px = (width_px / gx) * (height_px / gy)
    nodes = mgr.idx.nodes
    nid = np.asarray(nodes["node_id"]).astype(np.int64)
    row_of = {int(n): i for i, n in enumerate(nid)}
    bmin = np.asarray(nodes["bounds_min"], np.float64)
    bmax = np.asarray(nodes["bounds_max"], np.float64)
    lod_counts = np.asarray(nodes["lod_point_count"]).astype(np.int64)

    support = np.zeros((gy, gx))
    active = np.zeros((gy, gx))
    mult = np.zeros((gy, gx))
    best_cover = np.zeros((gy, gx))
    dom_lod = np.full((gy, gx), -1, np.int64)
    dom_node = np.full((gy, gx), -1, np.int64)

    def spread(b0, b1, pts):
        ox = _axis_overlap(b0[0], b1[0], x0, x1, gx)
        oy = _axis_overlap(b0[1], b1[1], y0, y1, gy)
        area = max((b1[0] - b0[0]) * (b1[1] - b0[1]), 1e-9)
        cover = np.outer(oy, ox) / ((x1 - x0) / gx * (y1 - y0) / gy)
        return np.outer(oy, ox) / area * pts, cover

    # SOURCE support: every node's LOD0 points, area-weighted into the cells
    for r in range(nodes.size):
        if bmax[r, 0] < x0 or bmin[r, 0] > x1 or bmax[r, 1] < y0 \
                or bmin[r, 1] > y1:
            continue
        s, _c = spread(bmin[r], bmax[r], float(lod_counts[r].max()))
        support += s

    ov = mgr._overview_key()
    hdr = mgr.idx.header
    for key in (mgr.active_draw_keys or ()):
        v = mgr.resident.get(key)
        if v is None:
            continue
        if ov is not None and tuple(key) == tuple(ov):
            b0 = np.asarray(hdr["bounds_min"], float)
            b1 = np.asarray(hdr["bounds_max"], float)
            n_id, lod = -1, int(key[1])
        else:
            r = row_of.get(int(key[0]))
            if r is None:
                continue
            b0, b1 = bmin[r], bmax[r]
            n_id, lod = int(key[0]), int(key[1])
        a, cover = spread(b0, b1, float(v.count))
        active += a
        # AREA covered, not node count: a cell straddling a tile boundary is
        # touched by two nodes yet covered exactly once (sum <= 1). Only real
        # duplicate cover (parent+child, overview over a refined region) pushes
        # the area sum above 1.
        mult += cover
        better = cover > best_cover
        best_cover = np.where(better, cover, best_cover)
        dom_lod = np.where(better, lod, dom_lod)
        dom_node = np.where(better, n_id, dom_node)

    if target_ppp is None:
        target_ppp = max(float(mgr.density.last_target), 1.0) / float(
            width_px * height_px)
    ppp = active / cell_px
    sup_ppp = support / cell_px
    frac = np.where(support > 0, active / np.maximum(support, 1e-9), np.nan)

    cls = np.full((gy, gx), TARGET, np.int8)
    has_src = support >= 1.0
    cls[~has_src] = NO_SOURCE
    cls[has_src & (active < 1.0)] = EMPTY
    live = has_src & (active >= 1.0)
    cls[live & (ppp < 0.5 * target_ppp)] = LOW
    cls[live & (ppp < 0.15 * target_ppp) & (sup_ppp >= target_ppp)] = STARVED
    dup = live & (mult > DUP_COVER)
    cls[live & ((ppp > 3.0 * target_ppp) | dup)] = OVERDENSE

    # neighbour discontinuities ACROSS A TILE BOUNDARY (the dominant node differs)
    ratios_ppp, ratios_frac, lod_b, seams = [], [], 0, 0
    for (ay, ax, by, bx) in (
            [(y, x, y, x + 1) for y in range(gy) for x in range(gx - 1)] +
            [(y, x, y + 1, x) for y in range(gy - 1) for x in range(gx)]):
        if dom_node[ay, ax] == dom_node[by, bx] or dom_node[ay, ax] < -1 \
                or not (live[ay, ax] and live[by, bx]):
            continue
        pa, pb = ppp[ay, ax], ppp[by, bx]
        ratios_ppp.append(max(pa, pb) / max(min(pa, pb), 1e-9))
        fa, fb = frac[ay, ax], frac[by, bx]
        ratios_frac.append(max(fa, fb) / max(min(fa, fb), 1e-9))
        if dom_lod[ay, ax] != dom_lod[by, bx]:
            lod_b += 1
        if ratios_ppp[-1] > 2.5 * 1.0 and ratios_frac[-1] > 2.5:
            seams += 1
    pct = lambda a, q: float(np.percentile(a, q)) if len(a) else float("nan")  # noqa: E731
    comp = lambda c: max(_components(cls == c), default=0)               # noqa: E731
    counts = {NAMES[k]: int((cls == k).sum()) for k in NAMES}
    return {
        "grid": (gx, gy), "target_ppp": float(target_ppp),
        "cls": cls, "ppp": ppp, "support_ppp": sup_ppp, "frac": frac,
        "mult": mult, "dom_lod": dom_lod, "dom_node": dom_node,
        "counts": counts,
        "empty_cells": counts["EMPTY"], "starved_cells": counts["STARVED"],
        "overdense_cells": counts["OVERDENSE"],
        "duplicate_cover_cells": int(dup.sum()),
        "largest_empty_component": comp(EMPTY),
        "largest_starved_component": comp(STARVED),
        "largest_overdense_component": comp(OVERDENSE),
        "max_adjacent_ppp_ratio": max(ratios_ppp) if ratios_ppp else float("nan"),
        "p95_adjacent_ppp_ratio": pct(ratios_ppp, 95),
        "max_adjacent_representation_ratio":
            max(ratios_frac) if ratios_frac else float("nan"),
        "lod_boundary_count": int(lod_b),
        "visible_tile_seam_count": int(seams),
        "boundary_pairs": len(ratios_ppp),
    }


def render_png(qm, path, title=""):
    """DEV diagnostic image: cell classes (colour) with PPP, to SEE whether
    a bright/dark boundary coincides with a tile boundary. Optional (needs
    matplotlib); returns False when unavailable."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.colors import ListedColormap
    except Exception:
        return False
    cmap = ListedColormap(["#202020", "#000000", "#d32f2f", "#fbc02d",
                           "#388e3c", "#1976d2"])
    gx, gy = qm["grid"]
    fig, ax = plt.subplots(1, 2, figsize=(14, 4.2))
    ax[0].imshow(qm["cls"], origin="lower", cmap=cmap, vmin=0, vmax=5,
                 interpolation="nearest")
    ax[0].set_title(f"{title}\nclass (red=starved, yellow=low, green=target, "
                    f"blue=overdense, black=empty)", fontsize=8)
    ppp = np.log10(np.maximum(qm["ppp"], 1e-4))
    im = ax[1].imshow(ppp, origin="lower", cmap="magma", vmin=-2.5, vmax=1.0,
                      interpolation="nearest")
    # tile boundaries: where the dominant node changes
    dn = qm["dom_node"]
    by, bx = np.nonzero(dn[:, 1:] != dn[:, :-1])
    ax[1].plot(bx + 0.5, by, "c|", ms=4, alpha=0.6)
    by, bx = np.nonzero(dn[1:, :] != dn[:-1, :])
    ax[1].plot(bx, by + 0.5, "c_", ms=4, alpha=0.6)
    ax[1].set_title("log10 PPP (cyan ticks = tile boundaries)", fontsize=8)
    fig.colorbar(im, ax=ax[1], fraction=0.025)
    fig.tight_layout()
    fig.savefig(path, dpi=90)
    plt.close(fig)
    return True
