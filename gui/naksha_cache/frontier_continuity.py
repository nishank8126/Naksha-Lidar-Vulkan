"""Bounded, monotone neighbour constraints for submitted point frontiers."""
import numpy as np
from numba import njit


@njit(cache=True, nogil=True)
def clamp_frontier(rows, lods, points, counts, spacing, area, ptr, neighbours,
                   lookup, spacing_limit=2.3, ppp_limit=2.5):
    lookup[:] = -1
    for i in range(len(rows)):
        lookup[rows[i]] = i
    # Every successful step removes points. No refinement, upload or budget
    # expansion is possible. The finite ladder bounds convergence.
    for iteration in range(counts.shape[1] * 2 + 1):
        changed = False
        for i in range(len(rows)):
            r = rows[i]
            required_lod = lods[i]
            required_spacing = 0.0
            max_density = np.inf
            for k in range(ptr[r], ptr[r + 1]):
                j = lookup[neighbours[k]]
                if j < 0:
                    continue
                q = rows[j]
                required_lod = max(required_lod, lods[j] - 1)
                required_spacing = max(required_spacing,
                                       spacing[q, lods[j]] / spacing_limit)
                max_density = min(max_density,
                                  points[j] / max(area[q], 1e-12) * ppp_limit)
            if (lods[i] >= required_lod
                    and spacing[r, lods[i]] >= required_spacing
                    and points[i] / max(area[r], 1e-12) <= max_density):
                continue
            for l in range(lods[i] + 1, counts.shape[1]):
                if counts[r, l] <= 0:
                    continue
                lods[i] = l
                points[i] = counts[r, l]
                changed = True
                if (l >= required_lod and spacing[r, l] >= required_spacing
                        and points[i] / max(area[r], 1e-12) <= max_density):
                    break
        if not changed:
            break
    violations = 0
    for i in range(len(rows)):
        r = rows[i]
        for k in range(ptr[r], ptr[r + 1]):
            j = lookup[neighbours[k]]
            if j <= i:
                continue
            q = rows[j]
            a, b = spacing[r, lods[i]], spacing[q, lods[j]]
            da = points[i] / max(area[r], 1e-12)
            db = points[j] / max(area[q], 1e-12)
            if (abs(lods[i] - lods[j]) > 1
                    or max(a, b) > spacing_limit * max(min(a, b), 1e-12)
                    or max(da, db) > ppp_limit * max(min(da, db), 1e-12)):
                violations += 1
    return violations


@njit(cache=True, nogil=True)
def refill_frontier(rows, lods, points, counts, spacing, area, ptr,
                    neighbours, lookup, budget):
    """Spend clamp savings on the globally sparsest admissible neighbours.

    Every upgrade must preserve the same final continuity envelope. This
    restores useful density after hold-back instead of leaving the budget idle.
    """
    remaining = budget - points.sum()
    for iteration in range(counts.shape[1]):
        density = np.empty(len(rows))
        for i in range(len(rows)):
            density[i] = points[i] / max(area[rows[i]], 1e-12)
        order = np.argsort(density)
        changed = False
        for i in order:
            r = rows[i]
            finer = -1
            for l in range(lods[i]-1, -1, -1):
                if counts[r, l] > 0:
                    finer = l
                    break
            if finer < 0:
                continue
            extra = counts[r, finer] - points[i]
            if extra <= 0 or extra > remaining:
                continue
            safe = True
            a = spacing[r, finer]
            da = counts[r, finer] / max(area[r], 1e-12)
            for k in range(ptr[r], ptr[r+1]):
                j = lookup[neighbours[k]]
                if j < 0:
                    continue
                q = rows[j]
                b = spacing[q, lods[j]]
                db = points[j] / max(area[q], 1e-12)
                if (abs(finer-lods[j]) > 1
                        or max(a,b) > 2.3 * max(min(a,b), 1e-12)
                        or max(da,db) > 2.5 * max(min(da,db), 1e-12)):
                    safe = False
                    break
            if safe:
                points[i] = counts[r, finer]
                lods[i] = finer
                remaining -= extra
                changed = True
        if not changed:
            break


def constrain(selector, arrays, counts, budget=None):
    ptr, neighbours = selector.ensure_adjacency()
    soa = selector.soa
    violations = int(clamp_frontier(arrays[0], arrays[1], arrays[2], counts,
                             soa.lod_spacing, soa.width * soa.height,
                             ptr, neighbours, selector._row_to_out))
    if violations == 0 and budget is not None:
        refill_frontier(arrays[0], arrays[1], arrays[2], counts,
                        soa.lod_spacing, soa.width * soa.height, ptr,
                        neighbours, selector._row_to_out, int(budget))
    return violations
