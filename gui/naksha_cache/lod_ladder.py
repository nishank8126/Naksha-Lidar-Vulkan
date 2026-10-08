"""lod_ladder.py - geometric, nested LOD ladder for one leaf (LOD_ALGO_VERSION 2).

THE DEFECT THIS REPLACES (measured on the corrected 27M cache)
--------------------------------------------------------------
The builder sized LOD voxels as ``leaf_cell * 2**lod`` (a function of the leaf's
EXTENT). A dense leaf therefore went 62 036 -> ~2 700 points in one step (a 23x
cliff), with nothing between. At a ~1 point-per-pixel target the only available
rungs were ~10 PPP or ~0.5 PPP, so each leaf snapped to whichever side its own
density favoured and neighbours landed on different sides: a mosaic of dense and
sparse tiles (PPP x58 across one boundary).

THE FIX
-------
Choose each rung's voxel size so the rung's POINT COUNT is a fixed fraction of
the previous rung (``ratio``): N_l ~= N_0 * ratio**l. In an area-like (2D / 2.5D)
cloud, count ~ 1/spacing**2, so a count ratio of 0.25 is a spacing step of 2x -
the apparent-density step between two neighbouring rungs is bounded and
UNIFORM across leaves of any density. Every rung is chosen from the previous
rung's representatives, so the ladder is NESTED (LOD_k+1 is a subset of LOD_k)
and every representative is an actual source point with its canonical id.

Deterministic: the result depends only on the leaf's points and the parameters
(``voxel_representative_ids`` breaks ties by index through ``np.lexsort``).
"""
from __future__ import annotations

import numpy as np

from .format import voxel_representative_ids

# Each rung keeps this fraction of the previous rung's points (0.4 == 2.5x fewer,
# 1.6x wider spacing). MEASURED on the real 27M cache (accept_visual_continuity_5a):
#   ratio  levels  cache    worst adjacent PPP step   starved cells (worst path)
#   0.25     5      911 MB        x4.98                    1,560
#   0.40     7     1133 MB        x3.23                       72
# 0.25 is a 4x density step per rung: a uniform allocator then cannot land between
# rungs, so a frame leaves most of its budget unused (22-44 %) and neighbouring
# tiles that straddle a threshold differ by up to 4x. 0.4 needs MORE rungs at the
# coarse end (levels 7): with only 3 rungs its coarsest rung is 6.4 % of the leaf,
# which exceeds a wide view's budget (the never-blank floor then over-draws).
DEFAULT_RATIO = 0.4
DEFAULT_LOD_LEVELS = 7        # LOD0 + 5 rungs + the overview block
MIN_RUNG_POINTS = 1000        # a rung below this is not worth a block
MAX_SEARCH_ITERS = 6
TOLERANCE = 0.25              # accept a rung within +-25 % of its target count


def occupied_area(xyz, grid: int = 32) -> float:
    """Ground area (m^2) a leaf's points actually OCCUPY: the number of occupied
    cells of a `grid` x `grid` lattice over its bounding box times the cell
    area. Unlike the bounding box it does not count water / gaps / the empty
    corners of an elongated flight-line leaf."""
    xyz = np.asarray(xyz)
    if xyz.shape[0] == 0:
        return 1e-9
    x0, y0 = float(xyz[:, 0].min()), float(xyz[:, 1].min())
    w = max(float(xyz[:, 0].max()) - x0, 1e-6)
    h = max(float(xyz[:, 1].max()) - y0, 1e-6)
    ix = np.minimum(((xyz[:, 0] - x0) / w * grid).astype(np.int64), grid - 1)
    iy = np.minimum(((xyz[:, 1] - y0) / h * grid).astype(np.int64), grid - 1)
    occ = int(np.unique(iy * grid + ix).size)
    return max(occ * (w / grid) * (h / grid), 1e-9)


def area_spacing(area_m2: float, n_points: int) -> float:
    """Area-equivalent sampling spacing (m) of `n_points` over `area_m2`.

    ONE physical definition for every rung of every leaf: mean spacing =
    sqrt(area / count). Stored per rung (NODE_ENTRY.lod_spacing) so that
    "apparent spacing in pixels" is comparable ACROSS nodes and rungs, and a
    uniform apparent spacing is a uniform points-per-pixel. The previous
    metadata mixed a measured Morton-neighbour distance (LOD0) with the voxel
    cell size (coarser rungs, inflated by a 3-D voxel's z-thickness): two rungs
    with the same point count could differ ~10x in stored spacing."""
    return float(np.sqrt(max(float(area_m2), 1e-9) / max(int(n_points), 1)))


def _search_cell(xyz, cls, target: int, cell0: float):
    """Voxel size whose representative count lands near ``target``.

    count ~ 1/cell**2, so the update is cell *= sqrt(count / target). Keeps the
    closest result seen (never returns an un-reduced set silently)."""
    n = xyz.shape[0]
    cell = max(float(cell0), 1e-4)
    best = None
    for _ in range(MAX_SEARCH_ITERS):
        rep = voxel_representative_ids(xyz, cell, cls)
        cnt = int(rep.size)
        if best is None or abs(cnt - target) < abs(best[1].size - target):
            best = (cell, rep)
        if abs(cnt / float(target) - 1.0) <= TOLERANCE:
            break
        # damped so a heavily vertical (z-stacked) leaf cannot oscillate
        step = (cnt / float(target)) ** 0.5
        cell *= min(max(step, 0.5), 2.0) if cnt > 0 else 2.0
    return best


def build_ladder(xyz, cls, spacing0: float, *, max_rungs: int = 3,
                 ratio: float = DEFAULT_RATIO,
                 min_points: int = MIN_RUNG_POINTS):
    """Nested rungs below LOD0.

    Returns a list of ``(indices_into_PREVIOUS_rung, cell_m)``, one per rung, in
    order LOD1, LOD2, ... Each index array selects from the rung before it, so
    composing them recovers the LOD0 ids. Empty list when the leaf is too small
    to need any coarser representation.
    """
    rungs = []
    cur_xyz, cur_cls = np.asarray(xyz), np.asarray(cls)
    cell = max(float(spacing0), 1e-3)
    for _ in range(max_rungs):
        n = cur_xyz.shape[0]
        target = int(n * ratio)
        if n <= min_points or target < min_points // 2:
            break
        # start one nominal step (1/sqrt(ratio) wider) from the previous cell
        found = _search_cell(cur_xyz, cur_cls, target,
                             cell / np.sqrt(ratio))
        if found is None:
            break
        cell, rep = found
        if rep.size == 0 or rep.size >= n * 0.9:       # no real reduction
            break
        rungs.append((rep, float(cell)))
        cur_xyz, cur_cls = cur_xyz[rep], cur_cls[rep]
    return rungs
