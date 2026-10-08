"""lod_kernel.py - PHASE 4: compiled, SoA screen-space LOD selection (2D ortho).

`naksha_lod_gate.ScreenSpaceLOD.select()` is the ACCEPTED (Phase 3) selection
semantics and stays as the DEV reference. This module reproduces exactly the same
result for the orthographic plan view on contiguous numeric arrays, in one
Numba `@njit(nogil=True, cache=True)` function, because the reference spends its
~16-20 ms per changing frame in per-node `numpy.memmap.__getitem__` calls
(~286 000 per 100 frames), `dict`/`list` allocation and Python `_pick` calls -
none of which depends on the camera.

What is precomputed ONCE (camera independent): node bounds, width, height,
centre, per-node LOD point counts and per-LOD representative spacing, as
contiguous float64/int64 arrays (`HierarchySoA`).

What is rebuilt only when RESIDENCY changes (`resident_generation`): the masked
LOD point counts (a non-resident LOD has count 0 == "does not exist").

Selected via `NAKSHA_LOD_KERNEL`:
    numba   (default)  compiled kernel
    python             reference ScreenSpaceLOD.select
    verify             run both, keep the reference answer, count mismatches
"""
from __future__ import annotations

import math
import os
import time

import numpy as np

try:                                                    # pragma: no cover
    from numba import njit
    HAVE_NUMBA = True
except Exception:                                       # pragma: no cover
    HAVE_NUMBA = False

    def njit(*a, **k):                                   # type: ignore
        if len(a) == 1 and callable(a[0]) and not k:
            return a[0]
        return lambda f: f


def kernel_mode() -> str:
    m = os.environ.get("NAKSHA_LOD_KERNEL", "numba").strip().lower()
    if m not in ("numba", "python", "verify"):
        m = "numba"
    if m != "python" and not HAVE_NUMBA:
        return "python"
    return m


# --------------------------------------------------------------------------- #
# Structure of arrays                                                          #
# --------------------------------------------------------------------------- #
class HierarchySoA:
    """Immutable, contiguous, camera-independent view of `idx.nodes`."""

    __slots__ = ("n", "n_lod", "node_id", "bmin_x", "bmin_y", "bmax_x",
                 "bmax_y", "width", "height", "centre_x", "centre_y",
                 "lod_count", "lod_spacing", "node_point_count", "level")

    def __init__(self, nodes):
        n = int(nodes.size)
        self.n = n
        names = nodes.dtype.names
        bmin = np.ascontiguousarray(np.asarray(nodes["bounds_min"],
                                               dtype=np.float64))
        bmax = np.ascontiguousarray(np.asarray(nodes["bounds_max"],
                                               dtype=np.float64))
        self.node_id = np.ascontiguousarray(
            np.asarray(nodes["node_id"]).astype(np.int64))
        self.bmin_x = np.ascontiguousarray(bmin[:, 0])
        self.bmin_y = np.ascontiguousarray(bmin[:, 1])
        self.bmax_x = np.ascontiguousarray(bmax[:, 0])
        self.bmax_y = np.ascontiguousarray(bmax[:, 1])
        # Same float operations the reference performs per call, done once.
        self.width = np.ascontiguousarray(self.bmax_x - self.bmin_x)
        self.height = np.ascontiguousarray(self.bmax_y - self.bmin_y)
        self.centre_x = np.ascontiguousarray(
            (self.bmin_x + self.bmax_x) * 0.5)
        self.centre_y = np.ascontiguousarray(
            (self.bmin_y + self.bmax_y) * 0.5)
        counts = np.asarray(nodes["lod_point_count"])
        if counts.ndim != 2:
            counts = counts.reshape(n, -1)
        self.n_lod = int(counts.shape[1])
        self.lod_count = np.ascontiguousarray(counts.astype(np.int64))
        if "lod_spacing" in names:
            sp = np.asarray(nodes["lod_spacing"], dtype=np.float64)
        else:
            sp = np.zeros((n, self.n_lod), np.float64)
        self.lod_spacing = np.ascontiguousarray(sp)
        self.node_point_count = np.ascontiguousarray(
            np.asarray(nodes["point_count"]).astype(np.int64)) \
            if "point_count" in names else np.zeros(n, np.int64)
        self.level = np.ascontiguousarray(
            np.asarray(nodes["level"]).astype(np.int32)) \
            if "level" in names else np.zeros(n, np.int32)

    def nbytes(self) -> int:
        return int(sum(getattr(self, s).nbytes for s in self.__slots__
                       if isinstance(getattr(self, s, None), np.ndarray)))

    def arrays(self) -> dict:
        return {s: getattr(self, s).dtype.str + str(getattr(self, s).shape)
                for s in self.__slots__
                if isinstance(getattr(self, s, None), np.ndarray)}


# --------------------------------------------------------------------------- #
# The kernel                                                                   #
# --------------------------------------------------------------------------- #
@njit(cache=True, nogil=True)
def _select_kernel(bmin_x, bmin_y, bmax_x, bmax_y, width, height,
                   centre_x, centre_y, counts, spacing, cur,
                   vx0, vx1, vy0, vy1, ppm, cam_cx, cam_cy, err_t, hyst,
                   budget,
                   o_row, o_lod, o_pts, o_proj, o_err, o_dist, o_ideal,
                   s_prio):
    """Returns (n_out, n_visible, over_budget). Mirrors ScreenSpaceLOD.select."""
    n = bmin_x.shape[0]
    n_lod = counts.shape[1]
    lo_t = err_t * (1.0 - hyst)
    n_out = 0
    n_vis = 0
    total = 0
    for r in range(n):
        # ---- orthographic visibility (no matrix, no Python call) ----------
        if not (bmax_x[r] >= vx0 and bmin_x[r] <= vx1
                and bmax_y[r] >= vy0 and bmin_y[r] <= vy1):
            continue
        n_vis += 1
        any_lod = False
        for l in range(n_lod):
            if counts[r, l] > 0:
                any_lod = True
                break
        if not any_lod:
            continue
        # ---- projected box (orthographic: scalar multiply) -----------------
        wpx = width[r] * ppm
        hpx = height[r] * ppm
        proj = wpx * hpx
        w = width[r]
        if w < 1e-9:
            w = 1e-9
        nppm = wpx / w
        # ---- _pick: coarsest LOD under the error target --------------------
        cur_l = cur[r]
        best_l = -1
        best_px = 0.0
        for l in range(n_lod - 1, -1, -1):
            if counts[r, l] <= 0:
                continue
            sp = spacing[r, l]
            if sp > 0.0 and nppm > 0.0:
                px = sp * nppm
            else:
                px = 1.0
            if cur_l < 0 or l <= cur_l:
                thr = err_t
            else:
                thr = lo_t
            if px <= thr:
                best_l = l
                best_px = px
                break
        if best_l < 0:
            # finest available == the LOD with the MOST points (first on ties)
            bc = -1
            for l in range(n_lod):
                if counts[r, l] > bc and counts[r, l] > 0:
                    bc = counts[r, l]
                    best_l = l
            best_px = 0.0
        cur[r] = best_l
        o_row[n_out] = r
        o_lod[n_out] = best_l
        o_pts[n_out] = counts[r, best_l]
        o_proj[n_out] = proj
        o_err[n_out] = best_px
        dx = centre_x[r] - cam_cx
        dy = centre_y[r] - cam_cy
        o_dist[n_out] = math.hypot(dx, dy)
        o_ideal[n_out] = best_l
        total += counts[r, best_l]
        n_out += 1
    over = False
    if total > budget and n_out > 0:
        over = True
        tot_area = 0.0
        for i in range(n_out):
            a = o_proj[i]
            if a < 1.0:
                a = 1.0
            tot_area += a
        if tot_area == 0.0:
            tot_area = 1.0
        for i in range(n_out):
            r = o_row[i]
            a = o_proj[i]
            if a < 1.0:
                a = 1.0
            share = budget * a / tot_area
            ideal = o_ideal[i]
            fit_l = -1
            coarse_ok = -1
            coarse_any = -1
            for l in range(n_lod):
                c = counts[r, l]
                if c <= 0:
                    continue
                coarse_any = l
                if l >= ideal:
                    coarse_ok = l
                    if fit_l < 0 and c <= share:
                        fit_l = l          # ascending l => smallest = finest
            if fit_l >= 0:
                o_lod[i] = fit_l
                o_pts[i] = counts[r, fit_l]
            elif coarse_ok >= 0:
                o_lod[i] = coarse_ok
                o_pts[i] = counts[r, coarse_ok]
            elif coarse_any >= 0:
                o_lod[i] = coarse_any
                o_pts[i] = counts[r, coarse_any]
        used = 0
        for i in range(n_out):
            used += o_pts[i]
        left = budget - used
        if left > 0:
            diag = 0.0
            for i in range(n_out):
                if o_dist[i] > diag:
                    diag = o_dist[i]
            if diag == 0.0:
                diag = 1.0
            for i in range(n_out):
                f = o_dist[i] / diag
                if f > 1.0:
                    f = 1.0
                s_prio[i] = -((1.0 - f) * math.log1p(o_proj[i]))
            order = np.argsort(s_prio[:n_out], kind="mergesort")
            for k in range(n_out):
                i = order[k]
                r = o_row[i]
                fin = -1
                for l in range(n_lod):
                    if counts[r, l] > 0 and l < o_lod[i] and l >= o_ideal[i]:
                        fin = l
                        break              # ascending l => finest first
                if fin < 0:
                    continue
                npts = counts[r, fin]
                if npts - o_pts[i] <= left:
                    left -= (npts - o_pts[i])
                    o_lod[i] = fin
                    o_pts[i] = npts
    return n_out, n_vis, over


@njit(cache=True, nogil=True)
def _uniform_total(o_row, n_out, counts, spacing, ppm, s_px):
    """Points if every selected node takes the coarsest resident rung whose
    apparent spacing is <= s_px (its finest resident rung if none qualifies)."""
    n_lod = counts.shape[1]
    total = 0
    for i in range(n_out):
        r = o_row[i]
        pick = -1
        finest = -1
        for l in range(n_lod - 1, -1, -1):
            if counts[r, l] <= 0:
                continue
            finest = l                   # ends as the smallest resident l
            if pick < 0:
                sp = spacing[r, l]
                px = sp * ppm if sp > 0.0 else 1.0
                if px <= s_px:
                    pick = l
        if pick < 0:
            pick = finest
        total += counts[r, pick]
    return total


@njit(cache=True, nogil=True)
def _select_uniform_kernel(bmin_x, bmin_y, bmax_x, bmax_y, width, height,
                           centre_x, centre_y, counts, spacing,
                           vx0, vx1, vy0, vy1, ppm, cam_cx, cam_cy, err_t,
                           budget,
                           o_row, o_lod, o_pts, o_proj, o_err, o_dist, o_ideal,
                           cur, hyst):
    """UNIFORM APPARENT-SPACING ALLOCATOR.

    HYSTERESIS (`hyst` > 0, state in `cur`): a node keeps its PREVIOUS rung while
    that rung is still within `hyst` of what S asks for - a rung coarser than
    needed is kept while its spacing <= S*(1+hyst); a rung finer than needed is
    kept while its spacing*(1+hyst) >= S. A node therefore changes rung only when
    S has moved clearly past the boundary, in either direction, which removes the
    tile popping / range pushes of a view hovering on a threshold. The budget
    stays a soft target (a kept finer rung may overshoot it by ~hyst).

    One scalar S (pixels) decides the whole frame: node i draws the coarsest
    resident rung with spacing_px <= S, or its own finest rung when the SOURCE
    is already sparser than S. S is the smallest value >= err_t whose total
    fits `budget`. Consequences, all by construction rather than by patching:

      * neighbouring nodes differ in apparent spacing by at most one rung step
        (the geometric ladder's 2x), whatever their source density;
      * a naturally sparse leaf stays at its own (wide) spacing - it is never
        "starved" and never coarsened to match a neighbour;
      * a node is never finer than the screen error target needs (S >= err_t);
      * when even all-coarsest exceeds the budget, S = that floor: coverage
        wins over the budget (never-blank), exactly as the reference does.

    Returns (n_out, n_visible, budget_limited, S).
    """
    n = bmin_x.shape[0]
    n_lod = counts.shape[1]
    n_out = 0
    n_vis = 0
    s_max = 0.0
    for r in range(n):
        if not (bmax_x[r] >= vx0 and bmin_x[r] <= vx1
                and bmax_y[r] >= vy0 and bmin_y[r] <= vy1):
            continue
        n_vis += 1
        any_lod = False
        for l in range(n_lod):
            if counts[r, l] > 0:
                any_lod = True
                break
        if not any_lod:
            continue
        o_row[n_out] = r
        n_out += 1
        # coarsest resident rung's apparent spacing bounds S from above
        for l in range(n_lod - 1, -1, -1):
            if counts[r, l] > 0:
                sp = spacing[r, l]
                px = sp * ppm if sp > 0.0 else 1.0
                if px > s_max:
                    s_max = px
                break
    if n_out == 0:
        return 0, n_vis, False, err_t
    s_lo = err_t
    limited = False
    if _uniform_total(o_row, n_out, counts, spacing, ppm, s_lo) <= budget:
        s = s_lo
    else:
        limited = True
        s_hi = s_max if s_max > s_lo else s_lo
        if _uniform_total(o_row, n_out, counts, spacing, ppm, s_hi) > budget:
            s = s_hi                       # coverage floor: exceed the budget
        else:
            lo = s_lo
            hi = s_hi
            for _it in range(40):
                mid = 0.5 * (lo + hi)
                if _uniform_total(o_row, n_out, counts, spacing, ppm,
                                  mid) <= budget:
                    hi = mid
                else:
                    lo = mid
            s = hi
    for i in range(n_out):
        r = o_row[i]
        pick = -1
        finest = -1
        ideal = -1
        for l in range(n_lod - 1, -1, -1):
            if counts[r, l] <= 0:
                continue
            finest = l
            sp = spacing[r, l]
            px = sp * ppm if sp > 0.0 else 1.0
            if pick < 0 and px <= s:
                pick = l
            if ideal < 0 and px <= s_lo:
                ideal = l
        if pick < 0:
            pick = finest
        if ideal < 0:
            ideal = finest
        if hyst > 0.0:
            p = cur[r]
            if p >= 0 and p != pick and counts[r, p] > 0:
                spp = spacing[r, p]
                pxp = spp * ppm if spp > 0.0 else 1.0
                if p > pick:
                    if pxp <= s * (1.0 + hyst):
                        pick = p
                else:
                    if pxp * (1.0 + hyst) >= s:
                        pick = p
            cur[r] = pick
        o_lod[i] = pick
        o_pts[i] = counts[r, pick]
        o_ideal[i] = ideal
        wpx = width[r] * ppm
        o_proj[i] = wpx * (height[r] * ppm)
        sp = spacing[r, pick]
        o_err[i] = sp * ppm if sp > 0.0 else 1.0
        o_dist[i] = math.hypot(centre_x[r] - cam_cx, centre_y[r] - cam_cy)
    return n_out, n_vis, limited, s


def uniform_reference(rows, counts, spacing, ppm, err_t, budget):
    """Pure-Python statement of the uniform allocator (parity tests).

    Returns (lods list aligned with `rows`, S)."""
    n_lod = counts.shape[1]

    def pick(r, s):
        fin = None
        for l in range(n_lod - 1, -1, -1):
            if counts[r, l] <= 0:
                continue
            fin = l
            sp = spacing[r, l]
            px = sp * ppm if sp > 0.0 else 1.0
            if px <= s:
                return l
        return fin

    def total(s):
        return sum(int(counts[r, pick(r, s)]) for r in rows)

    s_max = 0.0
    for r in rows:
        for l in range(n_lod - 1, -1, -1):
            if counts[r, l] > 0:
                sp = spacing[r, l]
                s_max = max(s_max, sp * ppm if sp > 0.0 else 1.0)
                break
    s_lo = err_t
    if total(s_lo) <= budget:
        s = s_lo
    else:
        s_hi = max(s_max, s_lo)
        if total(s_hi) > budget:
            s = s_hi
        else:
            lo, hi = s_lo, s_hi
            for _ in range(40):
                mid = 0.5 * (lo + hi)
                if total(mid) <= budget:
                    hi = mid
                else:
                    lo = mid
            s = hi
    return [pick(r, s) for r in rows], s


@njit(cache=True, nogil=True)
def _coherence_kernel(o_row, o_lod, o_pts, n_out, counts, spacing, ppm,
                      nb_ptr, nb_idx, row_to_out, limit, max_iters,
                      desired_lod, use_desired, o_orig, max_hold):
    """SCREEN-SPACE DENSITY COHERENCE (Phase 5, Parts 3-5).

    Apparent point spacing of a selected node in PIXELS is
    ``spacing[row, lod] * ppm``. It is SOURCE-AWARE: a naturally sparse leaf has
    a wide measured spacing even at its finest LOD, so it is never "fixed".
    A node whose apparent spacing is finer than its coarsest edge-neighbour's by
    more than ``limit`` is OVER-REFINED relative to that neighbour (a dense,
    bright tile beside a sparse one) and is coarsened to the finest rung whose
    spacing reaches ``neighbour / limit``. Rules that make this safe:

      * only ever COARSENS (LOD index and spacing grow) => never adds points,
        never drops a region (a node keeps >= 1 resident rung);
      * never touches a node that is already coarser than its neighbours;
      * monotone, so it terminates; bounded by ``max_iters`` passes.

    BOUNDED HOLD-BACK (``max_hold`` >= 0): a node is never coarsened more than
    ``max_hold`` rungs past the rung it would show without hold-back. Without the
    bound the pass CASCADES: a freshly entering edge tile that only has its
    coarsest rung drags its neighbours coarser, which drag theirs, until the
    whole view collapses to the coarsest rung (measured: 12.7 K points of a 240 K
    budget, 48 % of the screen empty, at a 0 % pan margin). Bounded, the contrast
    at the lagging tile is softened by a couple of rungs and the rest of the view
    is untouched.

    With ``use_desired`` (STREAMING) a neighbour only counts if it is LAGGING -
    drawn coarser than ITS OWN desired rung because that rung is not resident
    yet. A neighbour that is simply sparse in the source (already at its desired
    rung) is a genuine source-density boundary and is never "normalised away":
    hold-back is for residency lag, not for real differences in the data.

    Returns the number of nodes changed.
    """
    n = row_to_out.shape[0]
    n_lod = counts.shape[1]
    for i in range(n):
        row_to_out[i] = -1
    for i in range(n_out):
        row_to_out[o_row[i]] = i
        o_orig[i] = o_lod[i]          # the rung it would show WITHOUT hold-back
    changed_total = 0
    for _it in range(max_iters):
        changed = 0
        for i in range(n_out):
            r = o_row[i]
            a_i = spacing[r, o_lod[i]] * ppm
            if not (a_i > 0.0):
                continue
            need = 0.0
            for k in range(nb_ptr[r], nb_ptr[r + 1]):
                j = row_to_out[nb_idx[k]]
                if j < 0:
                    continue
                if use_desired and o_lod[j] <= desired_lod[o_row[j]]:
                    continue          # at/finer than its desired rung: not lagging
                a_j = spacing[o_row[j], o_lod[j]] * ppm
                if a_j > need:
                    need = a_j
            if need > 0.0 and a_i * limit < need:
                goal = need / limit
                best = -1
                for l in range(o_lod[i] + 1, n_lod):
                    if max_hold >= 0 and l > o_orig[i] + max_hold:
                        break         # BOUNDED hold-back (see below)
                    if counts[r, l] > 0:
                        best = l
                        if spacing[r, l] * ppm >= goal:
                            break
                if best >= 0:
                    o_lod[i] = best
                    o_pts[i] = counts[r, best]
                    changed += 1
        changed_total += changed
        if changed == 0:
            break
    return changed_total


def _ladder_report(counts, coherence_limit: float,
                   min_leaves: int = 20) -> dict:
    """Which LOD rung ladder was this cache BUILT with?

    The index header records the ladder's algorithm version, not its parameters,
    so a cache built with a coarser ladder is accepted and then drawn with
    selector defaults calibrated for the current one. The rung SIZES are already
    in the metadata: the median ratio of a node's second rung to its first is the
    ladder's count ratio, independent of leaf density. Measured on the same 27M
    file: 0.043 (legacy algo v1), 0.217 (ladder ratio 0.25), 0.333 (current
    ratio 0.40), 0.406 (ratio 0.50).

    The number that matters is the SPACING step between neighbouring rungs,
    ``1/sqrt(ratio)``: that is the finest contrast the ladder can offer, and the
    selector's coherence limit caps the contrast it will draw across a boundary.
    When one rung step exceeds the limit, no rung choice can satisfy continuity
    and the frontier falls back to a mosaic of too-fine and too-coarse tiles -
    the setting that removed the mosaic was measured on a finer ladder, so it is
    not doing what it says here.
    """
    out = {"ladder_ratio": float("nan"), "ladder_spacing_step": float("nan"),
           "ladder_rungs": 0, "coherence_limit": float(coherence_limit),
           "stale_ladder": False}
    if counts is None or getattr(counts, "ndim", 0) != 2 \
            or counts.shape[1] < 2:
        return out
    n0 = counts[:, 0].astype(np.float64)
    n1 = counts[:, 1].astype(np.float64)
    usable = (n0 >= 200.0) & (n1 > 0.0)
    if int(usable.sum()) < int(min_leaves):
        return out                      # too few leaves to infer a ladder
    ratio = float(np.median(n1[usable] / n0[usable]))
    if not np.isfinite(ratio) or ratio <= 0.0:
        return out
    step = float(1.0 / np.sqrt(ratio))
    out["ladder_ratio"] = ratio
    out["ladder_spacing_step"] = step
    out["ladder_rungs"] = int(np.median((counts > 0).sum(axis=1)))
    out["stale_ladder"] = bool(coherence_limit > 0.0 and step > coherence_limit)
    return out


def leaf_layout_report(soa, coherence_limit: float = 2.0) -> dict:
    """Is the leaf set a TILE PARTITION, or the overlapping layout of a legacy
    (pre-Morton-fix) cache? O(N), numeric, no point read.

    overlap_ratio = sum(leaf box areas) / area of the dataset's bounding box.
    A partition cannot exceed 1.0 (it is below 1.0 when the survey has gaps);
    measured on the same 27M file: corrected cache 1.00, legacy cache 30.4.
    `wide_fraction` is the share of leaves wider than 3x the median (corrected
    0 %, legacy 16 %, with leaves spanning the whole survey). Screen-space
    selection projects each node's
    bounds, so an overlapping layout feeds it inflated, shared areas and no
    allocator can make the result uniform.

    Also reports the rung ladder the cache was built with (see `_ladder_report`),
    judged against the coherence limit the caller will actually apply."""
    n = soa.n
    if n == 0:
        return {"nodes": 0, "overlap_ratio": 0.0, "wide_fraction": 0.0,
                "median_width_m": 0.0, "legacy_layout": False,
                **_ladder_report(None, coherence_limit)}
    area = float((soa.width * soa.height).sum())
    span = float((soa.bmax_x.max() - soa.bmin_x.min())
                 * (soa.bmax_y.max() - soa.bmin_y.min()))
    ratio = area / max(span, 1e-9)
    med = float(np.median(soa.width))
    wide = float((soa.width > 3.0 * max(med, 1e-9)).mean())
    return {"nodes": int(n), "overlap_ratio": ratio, "wide_fraction": wide,
            "median_width_m": med,
            "legacy_layout": bool(ratio > 1.3 or wide > 0.05),
            **_ladder_report(getattr(soa, "lod_count", None),
                             coherence_limit)}


def build_adjacency(soa, tol: float = 0.1, max_candidates: int = 64):
    """CSR edge-adjacency of the leaf boxes: (nb_ptr int32[N+1], nb_idx int32).

    Two leaves are neighbours when their boxes touch along an edge: the gap (or
    slight overlap) across one axis is within ``tol`` metres AND they overlap
    by more than ``tol`` along the other. Leaf bounds are TIGHT boxes around
    each leaf's points, so true neighbours are separated by the point-
    quantisation step (0.01 m measured), not exactly 0 - hence the tolerance.
    Heavily overlapping boxes (legacy, pre-Morton-fix caches) never qualify.

    Built ONCE per index (metadata-scale: bucketed, ~O(N)); never per frame.
    """
    n = soa.n
    if n == 0:
        return np.zeros(1, np.int32), np.zeros(0, np.int32)
    size = float(max(np.median(soa.width), np.median(soa.height), 1e-3))
    bx = np.floor((soa.centre_x - soa.centre_x.min()) / size).astype(np.int64)
    by = np.floor((soa.centre_y - soa.centre_y.min()) / size).astype(np.int64)
    buckets = {}
    for i in range(n):
        buckets.setdefault((int(bx[i]), int(by[i])), []).append(i)
    nb = [[] for _ in range(n)]
    for i in range(n):
        cand = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                cand.extend(buckets.get((int(bx[i]) + dx, int(by[i]) + dy), ()))
        for j in cand[:max_candidates * 9]:
            if j <= i:
                continue
            gx = max(soa.bmin_x[j] - soa.bmax_x[i], soa.bmin_x[i] - soa.bmax_x[j])
            gy = max(soa.bmin_y[j] - soa.bmax_y[i], soa.bmin_y[i] - soa.bmax_y[j])
            touch_x = abs(gx) <= tol and -gy > tol      # share a vertical edge
            touch_y = abs(gy) <= tol and -gx > tol      # share a horizontal edge
            if touch_x or touch_y:
                nb[i].append(j)
                nb[j].append(i)
    ptr = np.zeros(n + 1, np.int32)
    for i in range(n):
        ptr[i + 1] = ptr[i] + len(nb[i])
    idx = np.fromiter((j for lst in nb for j in lst), np.int32, int(ptr[-1]))
    return ptr, idx


def coherence_reference(o_row, o_lod, counts, spacing, ppm, nb, limit,
                        max_iters=8, desired=None, max_hold=-1):
    """Pure-Python statement of `_coherence_kernel` (parity tests).
    `desired` = {row: desired lod}; None => every neighbour counts.
    `max_hold` < 0 => unbounded (legacy)."""
    lod = list(o_lod)
    orig = list(o_lod)
    where = {int(r): i for i, r in enumerate(o_row)}
    n_lod = counts.shape[1]
    for _ in range(max_iters):
        changed = 0
        for i, r in enumerate(o_row):
            a_i = spacing[r, lod[i]] * ppm
            if not a_i > 0.0:
                continue
            need = 0.0
            for q in nb.get(int(r), ()):
                j = where.get(int(q))
                if j is None:
                    continue
                if desired is not None and lod[j] <= desired[int(o_row[j])]:
                    continue
                need = max(need, spacing[o_row[j], lod[j]] * ppm)
            if need > 0.0 and a_i * limit < need:
                goal = need / limit
                best = -1
                for l in range(lod[i] + 1, n_lod):
                    if max_hold >= 0 and l > orig[i] + max_hold:
                        break
                    if counts[r, l] > 0:
                        best = l
                        if spacing[r, l] * ppm >= goal:
                            break
                if best >= 0:
                    lod[i] = best
                    changed += 1
        if changed == 0:
            break
    return lod


class FastLodSelector:
    """Owns the SoA, the masked counts and the reusable output buffers."""

    def __init__(self, nodes):
        self.soa = HierarchySoA(nodes)
        n = self.soa.n
        self.cur = np.full(n, -1, np.int64)            # hysteresis state / node
        self._o = [np.empty(n, np.int64), np.empty(n, np.int64),
                   np.empty(n, np.int64), np.empty(n, np.float64),
                   np.empty(n, np.float64), np.empty(n, np.float64),
                   np.empty(n, np.int64)]
        self._prio = np.empty(n, np.float64)
        # neighbour adjacency: built lazily, once per index (see build_adjacency)
        self._nb = None
        self._row_to_out = np.empty(n, np.int64)
        self._desired_lod = np.full(n, -1, np.int64)
        self._orig_lod = np.empty(n, np.int64)       # pre-hold-back rung scratch
        # per-node previous rung for the uniform allocator's hysteresis:
        # [0] ACTIVE frontier, [1] DESIRED frontier (kept apart so one call does
        # not bias the other)
        self._ucur = [np.full(n, -1, np.int64), np.full(n, -1, np.int64)]
        self.coherence_changes = 0
        self._masked = None
        self._mask_gen = None
        self._row_of = None
        self.compile_ms = 0.0
        self.first_call_ms = 0.0
        self.calls = 0

    # -- residency ---------------------------------------------------------
    def set_resident(self, keys, generation) -> None:
        """Rebuild the masked counts. Called only when residency changed."""
        soa = self.soa
        if self._row_of is None:
            self._row_of = {int(v): i for i, v in enumerate(soa.node_id)}
        mask = np.zeros(soa.lod_count.shape, dtype=bool)
        row_of = self._row_of
        for nid, lod in keys:
            r = row_of.get(int(nid))
            if r is not None and 0 <= int(lod) < soa.n_lod:
                mask[r, int(lod)] = True
        self._masked = np.where(mask, soa.lod_count, 0).astype(np.int64)
        self._mask_gen = generation

    @property
    def mask_generation(self):
        return self._mask_gen

    # -- selection ---------------------------------------------------------
    def ensure_adjacency(self):
        if self._nb is None:
            self._nb = build_adjacency(self.soa)
        return self._nb

    @property
    def full_counts(self):
        """Every rung counted as available: the DESIRED (ideal) frontier."""
        return self.soa.lod_count

    def select_uniform(self, view, cam_cx, cam_cy, ppm, err_t, budget,
                       masked=None, coherence: float = 0.0, desired=None,
                       hysteresis: float = 0.0, state: int = 0,
                       max_hold: int = -1):
        """Uniform apparent-spacing allocation (see `_select_uniform_kernel`).

        Same return shape as `select`: (n_out, n_visible, budget_limited,
        arrays) with arrays ordered by row (the allocator has no reordering
        pass). Also records ``last_spacing_px`` (the frame's S).

        `masked` overrides the availability matrix: the resident-masked counts
        (default) give the ACTIVE frontier; `full_counts` gives the DESIRED one.
        `desired` = (rows, lods) of the DESIRED frontier: with `coherence` it
        restricts hold-back to neighbours lagging behind their own desired rung
        (residency lag), leaving genuine source-density boundaries alone.

        NOTE the returned arrays are views of shared buffers - copy them before
        the next call."""
        s = self.soa
        o = self._o
        t = time.perf_counter()
        n_out, n_vis, limited, s_px = _select_uniform_kernel(
            s.bmin_x, s.bmin_y, s.bmax_x, s.bmax_y, s.width, s.height,
            s.centre_x, s.centre_y,
            self._masked if masked is None else masked, s.lod_spacing,
            float(view[0]), float(view[1]), float(view[2]), float(view[3]),
            float(ppm), float(cam_cx), float(cam_cy), float(err_t),
            np.int64(budget), o[0], o[1], o[2], o[3], o[4], o[5], o[6],
            self._ucur[int(state)], float(hysteresis))
        if self.calls == 0:
            self.first_call_ms = (time.perf_counter() - t) * 1000.0
        self.calls += 1
        self.last_spacing_px = float(s_px)
        n_out = int(n_out)
        if coherence > 1.0 and n_out > 1:
            # Streaming: the RESIDENT-limited frontier can leave one node on a
            # coarse rung while its neighbour already has a fine one. Hold the
            # fine one back (coarsen it) until the neighbour catches up, so a
            # tile that loaded first never reads as a bright patch.
            ptr, nbi = self.ensure_adjacency()
            cnt = self._masked if masked is None else masked
            use_d = desired is not None
            if use_d:
                self._desired_lod[:] = -1
                self._desired_lod[desired[0]] = desired[1]
            self.coherence_changes += int(_coherence_kernel(
                o[0], o[1], o[2], n_out, cnt, s.lod_spacing, float(ppm),
                ptr, nbi, self._row_to_out, float(coherence), 8,
                self._desired_lod, use_d, self._orig_lod, int(max_hold)))
        return (n_out, int(n_vis), bool(limited),
                tuple(a[:n_out] for a in o))

    def select(self, view, cam_cx, cam_cy, ppm, err_t, hyst, budget,
               coherence: float = 0.0):
        """-> (n_out, n_visible, over_budget, arrays) for a 2D ortho camera.

        `view` = (x0, x1, y0, y1) visibility rectangle (already margin-expanded
        by the caller when it wants a pan margin). `coherence` > 1 enables the
        screen-space density coherence pass (0 = off: byte-identical to the
        reference selector)."""
        s = self.soa
        o = self._o
        t = time.perf_counter()
        n_out, n_vis, over = _select_kernel(
            s.bmin_x, s.bmin_y, s.bmax_x, s.bmax_y, s.width, s.height,
            s.centre_x, s.centre_y, self._masked, s.lod_spacing, self.cur,
            float(view[0]), float(view[1]), float(view[2]), float(view[3]),
            float(ppm), float(cam_cx), float(cam_cy), float(err_t),
            float(hyst), np.int64(budget),
            o[0], o[1], o[2], o[3], o[4], o[5], o[6], self._prio)
        if self.calls == 0:
            self.first_call_ms = (time.perf_counter() - t) * 1000.0
        self.calls += 1
        n_out = int(n_out)
        if coherence > 1.0 and n_out > 1:
            ptr, nbi = self.ensure_adjacency()
            self.coherence_changes += int(_coherence_kernel(
                o[0], o[1], o[2], n_out, self._masked, s.lod_spacing,
                float(ppm), ptr, nbi, self._row_to_out, float(coherence), 8,
                self._desired_lod, False, self._orig_lod, -1))
        if over and n_out > 1:
            order = np.argsort(s.node_id[o[0][:n_out]], kind="stable")
            arrs = tuple(a[:n_out][order] for a in o)
        else:
            arrs = tuple(a[:n_out] for a in o)
        return n_out, int(n_vis), bool(over), arrs

    def to_dicts(self, n_out, arrs):
        """Reference-shaped dicts (parity tests / the non-hot callers)."""
        row, lod, pts, proj, err, dist, ideal = arrs
        nid = self.soa.node_id
        return [{"node_id": int(nid[row[i]]), "lod": int(lod[i]),
                 "points": int(pts[i]), "proj_px": float(proj[i]),
                 "px_err": float(err[i]), "centre_dist": float(dist[i]),
                 "row": int(row[i]), "ideal": int(ideal[i])}
                for i in range(n_out)]

    def warmup(self) -> dict:
        """Compile (or load the on-disk cache) BEFORE the user's first pan."""
        if self._masked is None:
            self._masked = self.soa.lod_count.copy()
        saved = self.cur.copy()
        t = time.perf_counter()
        v = (self.soa.bmin_x.min() if self.soa.n else 0.0,
             self.soa.bmax_x.max() if self.soa.n else 1.0,
             self.soa.bmin_y.min() if self.soa.n else 0.0,
             self.soa.bmax_y.max() if self.soa.n else 1.0)
        self.select(v, 0.0, 0.0, 1.0, 1.0, 0.15, 1)
        self.compile_ms = (time.perf_counter() - t) * 1000.0
        t = time.perf_counter()
        self.select(v, 0.0, 0.0, 1.0, 1.0, 0.15, 1)
        second = (time.perf_counter() - t) * 1000.0
        # compile the coherence pass (and build the adjacency) here too, so the
        # first MOVING frame never pays a JIT compile
        t = time.perf_counter()
        self.select(v, 0.0, 0.0, 1.0, 1.0, 0.15, 1, coherence=2.0)
        self.compile_ms += (time.perf_counter() - t) * 1000.0
        t = time.perf_counter()
        _, _, _, arrays = self.select_uniform(v, 0.0, 0.0, 1.0, 1.0, 1)
        from .frontier_continuity import constrain
        constrain(self, arrays, self._masked, budget=1)
        self.compile_ms += (time.perf_counter() - t) * 1000.0
        self.coherence_changes = 0
        self.cur[:] = saved                      # warm-up must not bias state
        self._masked = None
        self.calls = 0
        return {"compile_ms": round(self.compile_ms, 2),
                "first_warm_call_ms": round(self.compile_ms, 2),
                "subsequent_call_ms": round(second, 4)}

    # -- PART A4: fractional LOD with ordered-prefix sub-rung sampling ----------- #
    def select_with_fractional(self, view, cam_cx, cam_cy, ppm, err_t, hyst,
                               budget, coherence: float = 0.0):
        """Same as ``select`` but returns fractional-LOD metadata (A4).

        Returns ``(n_out, n_visible, over_budget, arrs, frac_lod, extra_pts)``
        where for each output entry:

        - ``frac_lod[i]``  : fractional position in [0, 1] between the current
          rung ``lod[i]`` and the next *finer* rung ``lod[i]-1``. 0 means the
          coarse rung alone is sufficient; 1 means the finer rung is needed.
        - ``extra_pts[i]`` : integer count of *additional* points from the finer
          rung, computed by ORDERED-PREFIX sampling (deterministic Morton order).

        The ordered-prefix rule: from the finer rung's point superset, take the
        first ``int(frac * (count_{l-1} - count_l))`` points in Morton-code
        order. This is GPU-deterministic: every frame that lands on the same
        frac gets the same sub-rung subset, so dithering is stable and free of
        temporal sparkle.
        """
        s = self.soa
        n_out, n_vis, over, arrs = self.select(
            view, cam_cx, cam_cy, ppm, err_t, hyst, budget, coherence)
        if n_out == 0:
            return (0, n_vis, over,
                    arrs, np.empty(0, np.float32), np.empty(0, np.int64))
        row, lod, pts, proj, err, dist, ideal = arrs
        n = int(n_out)
        frac_lod = np.zeros(n, dtype=np.float32)
        extra_pts = np.zeros(n, dtype=np.int64)
        sp = s.lod_spacing
        ct = s.lod_count
        for i in range(n):
            r = int(row[i])
            l = int(lod[i])
            e_l = float(err[i])  # pixel error at selected rung
            if l <= 0:
                # Already at finest rung; no finer rung to sample from.
                frac_lod[i] = 0.0
                extra_pts[i] = 0
                continue
            if sp[r, l] <= 0.0 or sp[r, l - 1] <= 0.0:
                frac_lod[i] = 0.0
                extra_pts[i] = 0
                continue
            # Pixel error at the next finer rung (l-1): smaller spacing.
            err_fin = float(sp[r, l - 1]) * float(ppm)
            err_cur = float(sp[r, l]) * float(ppm)
            denom = err_cur - err_fin
            if denom <= 1e-12:
                frac = 0.0
            else:
                # err_t is the target threshold. When err_cur == err_t we are
                # right at the switch point -> frac=1 (include all finer).
                # When err_cur == err_fin (degenerate) frac=0.
                frac = (float(err_t) - err_cur) / denom
                frac = max(0.0, min(1.0, frac))
            frac_lod[i] = frac
            # Ordered-prefix: the finer rung has MORE points. Take a prefix.
            count_cur = int(ct[r, l])
            count_fin = int(ct[r, l - 1])
            extra = max(0, count_fin - count_cur)
            extra_pts[i] = int(frac * extra)
        return n_out, n_vis, over, arrs, frac_lod, extra_pts


# --------------------------------------------------------------------------- #
# LOD camera signature / reuse window                                          #
# --------------------------------------------------------------------------- #
class LodReuseWindow:
    """Decides whether the CURRENT selection is still valid for a new camera.

    EXACT camera (what Vulkan draws) is never touched by this: it only answers
    "does ScreenSpaceLOD need to be recomputed?". The selection was computed for
    a rectangle `sel` (the view, expanded by a pan margin while MOVING). It stays
    valid while

      * the non-camera inputs are identical (`key`: interaction state, viewport
        pixels, resident generation, budget, SSE target, display-mode cost...),
      * the new view rectangle is inside `sel` (within `tol_px` pixels), and
      * the scale moved by less than `zoom_eps` in log space.
    """

    __slots__ = ("key", "sel", "ppm", "valid")

    def __init__(self):
        self.valid = False
        self.key = None
        self.sel = None
        self.ppm = 1.0

    def store(self, key, sel, ppm) -> None:
        self.key, self.sel, self.ppm, self.valid = key, sel, float(ppm), True

    def invalidate(self) -> None:
        self.valid = False

    def covers(self, key, view, ppm, tol_px=0.25, zoom_eps=0.01) -> bool:
        if not self.valid or key != self.key:
            return False
        if ppm <= 0.0 or self.ppm <= 0.0:
            return False
        if abs(math.log(ppm / self.ppm)) > zoom_eps:
            return False
        tol = tol_px / ppm
        sx0, sx1, sy0, sy1 = self.sel
        return (view[0] >= sx0 - tol and view[1] <= sx1 + tol
                and view[2] >= sy0 - tol and view[3] <= sy1 + tol)
