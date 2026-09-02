from __future__ import annotations

from typing import Any, Mapping, Optional

import numpy as np


LAYERED_GROUND_VEGETATION_VERSION = "NAKSHA_LAYERED_GROUND_VEGETATION_V3_18"


def _valid_scope(n: int, active_indices=None) -> np.ndarray:
    if active_indices is None:
        return np.arange(n, dtype=np.int64)
    idx = np.asarray(active_indices, dtype=np.int64).ravel()
    idx = idx[(idx >= 0) & (idx < n)]
    return np.unique(idx)


def _nearest_ground_z_ckdtree(
    xyz: np.ndarray,
    query_idx: np.ndarray,
    ground_idx: np.ndarray,
    *,
    k: int,
    max_ground_distance: float,
    chunk_size: int,
):
    from scipy.spatial import cKDTree

    ground_xy = np.asarray(xyz[ground_idx, :2], dtype=np.float64)
    ground_z = np.asarray(xyz[ground_idx, 2], dtype=np.float64)
    tree = cKDTree(ground_xy)

    out_z = np.full(query_idx.size, np.nan, dtype=np.float64)
    out_d = np.full(query_idx.size, np.inf, dtype=np.float64)
    kk = max(1, min(int(k), int(ground_idx.size)))

    for start in range(0, query_idx.size, max(1, int(chunk_size))):
        stop = min(query_idx.size, start + max(1, int(chunk_size)))
        qxy = np.asarray(xyz[query_idx[start:stop], :2], dtype=np.float64)
        d, j = tree.query(qxy, k=kk, workers=-1)
        if kk == 1:
            d = np.asarray(d, dtype=np.float64)[:, None]
            j = np.asarray(j, dtype=np.int64)[:, None]
        else:
            d = np.asarray(d, dtype=np.float64)
            j = np.asarray(j, dtype=np.int64)

        valid = np.isfinite(d) & (d <= float(max_ground_distance))
        zvals = np.full(d.shape, np.nan, dtype=np.float64)
        good_j = (j >= 0) & (j < ground_z.size) & valid
        zvals[good_j] = ground_z[j[good_j]]

        # Lower-quartile of nearby Ground-labelled seeds suppresses the most
        # common contamination we are correcting here: low vegetation that the
        # AI accidentally labelled Ground. It is less aggressive than a raw
        # minimum and still follows the local terrain surface.
        with np.errstate(all="ignore"):
            local_z = np.nanpercentile(zvals, 25.0, axis=1)
        nearest = np.min(np.where(valid, d, np.inf), axis=1)
        local_z[~np.isfinite(nearest)] = np.nan
        out_z[start:stop] = local_z
        out_d[start:stop] = nearest

    return out_z, out_d, "scipy_ckdtree"


def _nearest_ground_z_grid(
    xyz: np.ndarray,
    query_idx: np.ndarray,
    ground_idx: np.ndarray,
    *,
    cell_size: float,
    max_ground_distance: float,
):
    """Numpy-only fallback used if SciPy is unavailable.

    A robust median Ground elevation is stored per XY cell. Query points first
    use their own cell and then an expanding 3x3/5x5 neighborhood up to the
    requested search radius. This path is slower than cKDTree but keeps the
    production module dependency-safe.
    """
    cs = max(float(cell_size), 0.25)
    gx = np.asarray(xyz[ground_idx, 0], dtype=np.float64)
    gy = np.asarray(xyz[ground_idx, 1], dtype=np.float64)
    gz = np.asarray(xyz[ground_idx, 2], dtype=np.float64)
    x0 = float(np.nanmin(gx))
    y0 = float(np.nanmin(gy))
    gix = np.floor((gx - x0) / cs).astype(np.int64)
    giy = np.floor((gy - y0) / cs).astype(np.int64)

    # Sort by packed cell key and reduce each cell to median Z.
    key = (gix.astype(np.int64) << np.int64(32)) ^ (giy & np.int64(0xFFFFFFFF))
    order = np.argsort(key, kind="mergesort")
    key_s = key[order]
    z_s = gz[order]
    starts = np.r_[0, 1 + np.flatnonzero(key_s[1:] != key_s[:-1])]
    ends = np.r_[starts[1:], len(key_s)]
    keys_u = key_s[starts]
    med_z = np.empty(len(starts), dtype=np.float64)
    for i, (a, b) in enumerate(zip(starts, ends)):
        med_z[i] = float(np.median(z_s[a:b]))

    qx = np.asarray(xyz[query_idx, 0], dtype=np.float64)
    qy = np.asarray(xyz[query_idx, 1], dtype=np.float64)
    qix = np.floor((qx - x0) / cs).astype(np.int64)
    qiy = np.floor((qy - y0) / cs).astype(np.int64)
    out_z = np.full(query_idx.size, np.nan, dtype=np.float64)
    out_d = np.full(query_idx.size, np.inf, dtype=np.float64)

    max_cells = max(0, int(np.ceil(float(max_ground_distance) / cs)))
    unresolved = np.ones(query_idx.size, dtype=bool)
    for r in range(max_cells + 1):
        if not np.any(unresolved):
            break
        ui = np.flatnonzero(unresolved)
        best_d = np.full(ui.size, np.inf, dtype=np.float64)
        best_z = np.full(ui.size, np.nan, dtype=np.float64)
        offsets = [(dx, dy) for dx in range(-r, r + 1) for dy in range(-r, r + 1)
                   if max(abs(dx), abs(dy)) == r]
        for dx, dy in offsets:
            kk = ((qix[ui] + dx).astype(np.int64) << np.int64(32)) ^ ((qiy[ui] + dy) & np.int64(0xFFFFFFFF))
            pos = np.searchsorted(keys_u, kk)
            ok = (pos < len(keys_u))
            ok[ok] &= keys_u[pos[ok]] == kk[ok]
            if not np.any(ok):
                continue
            approx_d = np.hypot(float(dx) * cs, float(dy) * cs)
            take = ok & (approx_d < best_d)
            if np.any(take):
                best_d[take] = approx_d
                best_z[take] = med_z[pos[take]]
        good = np.isfinite(best_z) & (best_d <= float(max_ground_distance))
        if np.any(good):
            global_i = ui[good]
            out_z[global_i] = best_z[good]
            out_d[global_i] = best_d[good]
            unresolved[global_i] = False

    return out_z, out_d, "numpy_grid_fallback"


def apply_layered_ground_vegetation_postprocess(
    xyz: np.ndarray,
    classes: np.ndarray,
    semantic_mapping: Mapping[str, int],
    *,
    ai_mode: str,
    active_indices=None,
    protected_mask=None,
    config: Optional[Mapping[str, Any]] = None,
):
    """Create human-style vertical Ground/Vegetation layers using local HAG.

    Scope and safety contract:
      * Advanced + Premium only; Basic is unchanged.
      * Only Ground / LowVeg / MediumVeg / HighVeg are eligible for writes.
      * Building / Wire / Pole / LowPoint / Uncategorized are never rewritten.
      * Uses local Ground support from the full cloud as context while writing
        only exact active/fence indices.
      * Active PTC numeric codes are resolved dynamically by the caller.
      * Ground 70/30 policy is intentionally NOT implemented here; it runs
        later so only the final layered Ground pool is sampled.

    Default vertical bands (height above local Ground):
      <= 0.15 m           Ground
      0.15 .. 0.50 m     Low Vegetation
      0.50 .. 2.00 m     Medium Vegetation
      > 2.00 m            High Vegetation

    Points without reliable nearby Ground support are left unchanged.
    """
    cfg = dict(config or {})
    ground_max = float(cfg.get("ground_max_hag", 0.15))
    low_max = float(cfg.get("low_vegetation_max_hag", 0.50))
    medium_max = float(cfg.get("medium_vegetation_max_hag", 2.00))
    max_ground_distance = float(cfg.get("max_ground_support_distance", 5.0))
    k_ground = int(cfg.get("ground_neighbors", 6))
    chunk_size = int(cfg.get("query_chunk_size", 100_000))
    fallback_cell_size = float(cfg.get("fallback_grid_cell_size", 0.75))
    min_hag = float(cfg.get("minimum_valid_hag", -0.50))
    max_hag = float(cfg.get("maximum_valid_hag", 80.0))

    if not (ground_max < low_max < medium_max):
        raise ValueError("layer thresholds must satisfy ground_max < low_max < medium_max")

    xyz = np.asarray(xyz)
    arr = np.asarray(classes)
    if xyz.ndim != 2 or xyz.shape[1] < 3 or arr.ndim != 1 or len(xyz) != len(arr):
        raise ValueError("xyz/classes shape mismatch")

    n = len(arr)
    scope_idx = _valid_scope(n, active_indices)
    mode = str(ai_mode or "basic").strip().lower()
    work = arr.copy()
    changed_mask = np.zeros(n, dtype=bool)

    report = {
        "version": LAYERED_GROUND_VEGETATION_VERSION,
        "status": "RUNNING",
        "ai_mode": mode,
        "scope_points": int(scope_idx.size),
        "exact_scope_only": True,
        "dynamic_ptc": True,
        "eligible_semantics": ["ground", "low_vegetation", "medium_vegetation", "high_vegetation"],
        "protected_semantics": ["uncategorized", "building", "wire", "pole", "low_point"],
        "thresholds_m": {
            "ground_max": ground_max,
            "low_vegetation_max": low_max,
            "medium_vegetation_max": medium_max,
        },
        "eligible_points": 0,
        "ground_support_points": 0,
        "reliable_hag_points": 0,
        "changed_points": 0,
        "transition_counts": {},
        "backend": None,
    }

    if mode not in ("advanced", "premium"):
        report["status"] = "SKIPPED_MODE_NOT_ADVANCED_OR_PREMIUM"
        return work, changed_mask, report

    names = ("ground", "low_vegetation", "medium_vegetation", "high_vegetation")
    if any(semantic_mapping.get(name) is None for name in names):
        report["status"] = "SKIPPED_MISSING_PTC_SEMANTIC"
        return work, changed_mask, report

    codes = {name: int(semantic_mapping[name]) for name in names}
    if len(set(codes.values())) != 4:
        report["status"] = "SKIPPED_SEMANTIC_CODE_COLLISION"
        return work, changed_mask, report
    if any(code < 0 or code > 255 for code in codes.values()):
        report["status"] = "SKIPPED_CODE_OUT_OF_UINT8_RANGE"
        return work, changed_mask, report

    report["codes"] = dict(codes)
    eligible_codes = np.asarray(list(codes.values()), dtype=arr.dtype)
    eligible = np.isin(work[scope_idx], eligible_codes)
    eligible_idx = scope_idx[eligible]

    if protected_mask is not None:
        p = np.asarray(protected_mask, dtype=bool)
        if len(p) != n:
            raise ValueError("protected_mask length mismatch")
        eligible_idx = eligible_idx[~p[eligible_idx]]

    report["eligible_points"] = int(eligible_idx.size)
    if eligible_idx.size == 0:
        report["status"] = "NO_ELIGIBLE_POINTS"
        return work, changed_mask, report

    # Full-cloud Ground is support/context only. The write scope remains exact.
    ground_idx = np.flatnonzero(work == codes["ground"]).astype(np.int64)
    report["ground_support_points"] = int(ground_idx.size)
    if ground_idx.size < 8:
        report["status"] = "SKIPPED_INSUFFICIENT_GROUND_SUPPORT"
        return work, changed_mask, report

    try:
        local_ground_z, ground_distance, backend = _nearest_ground_z_ckdtree(
            xyz,
            eligible_idx,
            ground_idx,
            k=k_ground,
            max_ground_distance=max_ground_distance,
            chunk_size=chunk_size,
        )
    except Exception:
        local_ground_z, ground_distance, backend = _nearest_ground_z_grid(
            xyz,
            eligible_idx,
            ground_idx,
            cell_size=fallback_cell_size,
            max_ground_distance=max_ground_distance,
        )
    report["backend"] = backend

    hag = np.asarray(xyz[eligible_idx, 2], dtype=np.float64) - local_ground_z
    reliable = (
        np.isfinite(local_ground_z)
        & np.isfinite(hag)
        & np.isfinite(ground_distance)
        & (ground_distance <= max_ground_distance)
        & (hag >= min_hag)
        & (hag <= max_hag)
    )
    reliable_idx = eligible_idx[reliable]
    reliable_hag = hag[reliable]
    report["reliable_hag_points"] = int(reliable_idx.size)
    if reliable_idx.size == 0:
        report["status"] = "NO_RELIABLE_LOCAL_GROUND"
        return work, changed_mask, report

    target = np.empty(reliable_idx.size, dtype=arr.dtype)
    target[:] = np.asarray(codes["high_vegetation"], dtype=arr.dtype)
    target[reliable_hag <= medium_max] = np.asarray(codes["medium_vegetation"], dtype=arr.dtype)
    target[reliable_hag <= low_max] = np.asarray(codes["low_vegetation"], dtype=arr.dtype)
    target[reliable_hag <= ground_max] = np.asarray(codes["ground"], dtype=arr.dtype)

    before = work[reliable_idx].copy()
    diff = before != target
    if np.any(diff):
        changed_idx = reliable_idx[diff]
        work[changed_idx] = target[diff]
        changed_mask[changed_idx] = True

    # Human-readable transition audit, e.g. medium_vegetation->low_vegetation.
    code_to_name = {v: k for k, v in codes.items()}
    transitions = {}
    if np.any(diff):
        b = before[diff].astype(np.int64)
        t = target[diff].astype(np.int64)
        pairs = np.stack([b, t], axis=1)
        uniq, counts = np.unique(pairs, axis=0, return_counts=True)
        for (src, dst), count in zip(uniq, counts):
            key = f"{code_to_name.get(int(src), str(int(src)))}->{code_to_name.get(int(dst), str(int(dst)))}"
            transitions[key] = int(count)

    report["transition_counts"] = transitions
    report["changed_points"] = int(np.count_nonzero(changed_mask))
    report["status"] = "APPLIED" if report["changed_points"] else "NO_CHANGE"
    report["hag_stats_m"] = {
        "p02": float(np.percentile(reliable_hag, 2)),
        "p50": float(np.percentile(reliable_hag, 50)),
        "p98": float(np.percentile(reliable_hag, 98)),
    }

    changed_mask.setflags(write=False)
    return work, changed_mask, report
