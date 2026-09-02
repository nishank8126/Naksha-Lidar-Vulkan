from __future__ import annotations

from typing import Any, Mapping, Optional

import numpy as np
from scipy.spatial import cKDTree


def _scope_indices(n: int, active_indices=None) -> np.ndarray:
    if active_indices is None:
        return np.arange(n, dtype=np.int64)
    idx = np.asarray(active_indices, dtype=np.int64).ravel()
    idx = idx[(idx >= 0) & (idx < n)]
    return np.unique(idx)


def _safe_code(mapping: Mapping[str, Any], key: str):
    raw = mapping.get(key)
    if raw is None:
        return None
    try:
        value = int(raw)
    except Exception:
        return None
    if 0 <= value <= 255:
        return value
    return None


def _choose_vegetation_destination(
    rows: np.ndarray,
    before: Optional[np.ndarray],
    neighbour_before: Optional[np.ndarray],
    neighbour_current: np.ndarray,
    semantic: Mapping[str, Any],
    local_height: np.ndarray,
) -> np.ndarray:
    low = _safe_code(semantic, "low_vegetation")
    mid = _safe_code(semantic, "medium_vegetation")
    high = _safe_code(semantic, "high_vegetation")
    ground = _safe_code(semantic, "ground")
    veg_codes = [c for c in (low, mid, high) if c is not None]

    out = np.full(len(rows), -1, dtype=np.int16)

    # First preference: the pre-AI semantic class at exactly this point.
    if before is not None:
        b = before[rows]
        for code in veg_codes:
            out[(out < 0) & (b == code)] = code
        if ground is not None:
            out[(out < 0) & (b == ground) & (local_height <= 0.45)] = ground

    # Second preference: local pre-AI vegetation majority. This is valuable when
    # the model turns the whole crown into Building, because the current labels
    # can no longer reveal what the object used to be.
    if neighbour_before is not None and veg_codes:
        unresolved = np.flatnonzero(out < 0)
        if unresolved.size:
            nb = neighbour_before[unresolved]
            counts = np.column_stack([(nb == c).sum(axis=1) for c in veg_codes])
            best = np.argmax(counts, axis=1)
            best_count = counts[np.arange(len(unresolved)), best]
            enough = best_count >= np.maximum(3, np.ceil(nb.shape[1] * 0.28)).astype(int)
            if np.any(enough):
                out[unresolved[enough]] = np.asarray(veg_codes, dtype=np.int16)[best[enough]]

    # Third preference: current non-Building vegetation neighbours.
    if veg_codes:
        unresolved = np.flatnonzero(out < 0)
        if unresolved.size:
            nb = neighbour_current[unresolved]
            counts = np.column_stack([(nb == c).sum(axis=1) for c in veg_codes])
            best = np.argmax(counts, axis=1)
            best_count = counts[np.arange(len(unresolved)), best]
            enough = best_count >= np.maximum(3, np.ceil(nb.shape[1] * 0.22)).astype(int)
            if np.any(enough):
                out[unresolved[enough]] = np.asarray(veg_codes, dtype=np.int16)[best[enough]]

    # Final semantic fallback uses local height, never Uncategorized.
    unresolved = out < 0
    if np.any(unresolved):
        h = local_height[unresolved]
        dest = np.empty(len(h), dtype=np.int16)
        if low is None and mid is None and high is None:
            dest[:] = ground if ground is not None else 0
        else:
            low_f = low if low is not None else (mid if mid is not None else high)
            mid_f = mid if mid is not None else (high if high is not None else low_f)
            high_f = high if high is not None else mid_f
            dest[:] = low_f
            dest[h >= 2.0] = mid_f
            dest[h >= 5.0] = high_f
        out[unresolved] = dest

    return out.astype(np.uint8, copy=False)


def apply_building_vegetation_guard(
    xyz,
    classes,
    semantic_mapping: Mapping[str, Any],
    *,
    active_indices=None,
    before_classes=None,
    protected_mask=None,
    config: Optional[Mapping[str, Any]] = None,
):
    """Conservative Building -> vegetation/ground rollback.

    This stage is intentionally external to the trained models. It targets only
    Building points that look like volumetric canopy / shrub structure and lack
    strong roof/wall geometry. Real planar roofs and facades are protected.

    It never sends rejected Building points to Uncategorized. When available,
    the exact pre-AI class is used as evidence and as the preferred rollback
    destination. Otherwise a local vegetation semantic is chosen.
    """
    cfg = dict(config or {})
    xyz = np.asarray(xyz, dtype=np.float64)
    cls = np.asarray(classes, dtype=np.uint8).copy()
    n = len(cls)
    if xyz.ndim != 2 or xyz.shape[1] < 3 or len(xyz) != n:
        raise ValueError("Building guard array-length mismatch")

    building = _safe_code(semantic_mapping, "building")
    if building is None:
        return cls, np.zeros(n, dtype=bool), {
            "status": "SKIPPED_NO_BUILDING_SEMANTIC",
            "building_points_checked": 0,
            "rolled_back": 0,
        }

    scope = _scope_indices(n, active_indices)
    if scope.size == 0:
        return cls, np.zeros(n, dtype=bool), {
            "status": "SKIPPED_EMPTY_SCOPE",
            "building_points_checked": 0,
            "rolled_back": 0,
        }

    protected = np.zeros(n, dtype=bool)
    if protected_mask is not None:
        p = np.asarray(protected_mask, dtype=bool).ravel()
        if len(p) == n:
            protected = p

    candidate = scope[(cls[scope] == np.uint8(building)) & (~protected[scope])]
    if candidate.size == 0:
        return cls, np.zeros(n, dtype=bool), {
            "status": "NO_BUILDING_CANDIDATES",
            "building_points_checked": 0,
            "rolled_back": 0,
        }

    before = None
    if before_classes is not None:
        b = np.asarray(before_classes).ravel()
        if len(b) == n:
            before = b.astype(np.uint8, copy=False)

    # Limit the support tree to the selected/fence neighbourhood rather than
    # building a multi-million point KD-tree for the whole loaded file.
    margin = float(cfg.get("support_margin_m", 3.0))
    txyz = xyz[scope]
    minx, miny = np.nanmin(txyz[:, :2], axis=0) - margin
    maxx, maxy = np.nanmax(txyz[:, :2], axis=0) + margin
    support_mask = (
        (xyz[:, 0] >= minx) & (xyz[:, 0] <= maxx)
        & (xyz[:, 1] >= miny) & (xyz[:, 1] <= maxy)
    )
    support_idx = np.flatnonzero(support_mask)
    if support_idx.size < 16:
        return cls, np.zeros(n, dtype=bool), {
            "status": "SKIPPED_TOO_LITTLE_SUPPORT",
            "building_points_checked": int(candidate.size),
            "rolled_back": 0,
        }

    k3 = int(cfg.get("geometry_k", 18))
    kxy = int(cfg.get("class_k", 28))
    k3 = max(6, min(k3, len(support_idx)))
    kxy = max(8, min(kxy, len(support_idx)))

    support_xyz = xyz[support_idx]
    tree3 = cKDTree(support_xyz)
    _, nn3 = tree3.query(xyz[candidate], k=k3, workers=-1)
    nn3 = np.asarray(nn3, dtype=np.int64)
    if nn3.ndim == 1:
        nn3 = nn3[:, None]
    pts = support_xyz[nn3]
    centered = pts - pts.mean(axis=1, keepdims=True)
    cov = np.einsum("nki,nkj->nij", centered, centered) / max(k3 - 1, 1)
    eigvals, eigvecs = np.linalg.eigh(cov)
    eigvals = np.maximum(eigvals, 0.0)[:, ::-1]
    eigvecs = eigvecs[:, :, ::-1]
    l1, l2, l3 = eigvals[:, 0], eigvals[:, 1], eigvals[:, 2]
    eps = 1e-9
    linearity = (l1 - l2) / (l1 + eps)
    planarity = (l2 - l3) / (l1 + eps)
    sphericity = l3 / (l1 + eps)
    # Smallest eigenvector is the local surface normal after descending reorder.
    normal_z = np.abs(eigvecs[:, 2, 2])
    verticality = 1.0 - normal_z
    zstd3 = np.std(pts[:, :, 2], axis=1)

    treexy = cKDTree(support_xyz[:, :2])
    _, nnxy = treexy.query(xyz[candidate, :2], k=kxy, workers=-1)
    nnxy = np.asarray(nnxy, dtype=np.int64)
    if nnxy.ndim == 1:
        nnxy = nnxy[:, None]
    nb_global = support_idx[nnxy]
    nb_current = cls[nb_global]
    nb_before = before[nb_global] if before is not None else None

    b_ratio = np.mean(nb_current == np.uint8(building), axis=1)
    veg_codes = [
        _safe_code(semantic_mapping, "low_vegetation"),
        _safe_code(semantic_mapping, "medium_vegetation"),
        _safe_code(semantic_mapping, "high_vegetation"),
    ]
    veg_codes = [c for c in veg_codes if c is not None]
    current_veg_ratio = (
        np.mean(np.isin(nb_current, np.asarray(veg_codes, dtype=np.uint8)), axis=1)
        if veg_codes else np.zeros(len(candidate), dtype=np.float32)
    )
    before_veg_ratio = (
        np.mean(np.isin(nb_before, np.asarray(veg_codes, dtype=np.uint8)), axis=1)
        if (nb_before is not None and veg_codes) else np.zeros(len(candidate), dtype=np.float32)
    )

    z_local_low = np.percentile(support_xyz[nnxy, 2], 10.0, axis=1)
    local_height = xyz[candidate, 2] - z_local_low

    roof_like = (
        (planarity >= float(cfg.get("roof_planarity_min", 0.42)))
        & (verticality <= float(cfg.get("roof_verticality_max", 0.38)))
        & (sphericity <= float(cfg.get("roof_sphericity_max", 0.24)))
    )
    wall_like = (
        (planarity >= float(cfg.get("wall_planarity_min", 0.30)))
        & (verticality >= float(cfg.get("wall_verticality_min", 0.75)))
        & (sphericity <= float(cfg.get("wall_sphericity_max", 0.16)))
        & (zstd3 >= float(cfg.get("wall_zstd_min", 0.14)))
    )

    # Leaf patches can look planar point-by-point. When the pre-AI neighbourhood
    # is strongly vegetation, require the roof/wall geometry to be supported by
    # neighbouring structural Building candidates rather than one isolated leaf.
    seed = roof_like | wall_like
    if len(candidate) >= 4 and np.any(seed):
        ctree = cKDTree(xyz[candidate])
        kk = min(int(cfg.get("structural_support_k", 10)), len(candidate))
        _, cnn = ctree.query(xyz[candidate], k=max(2, kk), workers=-1)
        cnn = np.asarray(cnn, dtype=np.int64)
        if cnn.ndim == 1:
            cnn = cnn[:, None]
        seed_ratio = np.mean(seed[cnn], axis=1)
        preveg_strong = before_veg_ratio >= float(cfg.get("structural_preveg_ratio", 0.48))
        min_seed_ratio = float(cfg.get("structural_seed_ratio_min", 0.55))
        roof_like = roof_like & ((~preveg_strong) | (seed_ratio >= min_seed_ratio))
        wall_like = wall_like & ((~preveg_strong) | (seed_ratio >= min_seed_ratio))
    else:
        seed_ratio = np.zeros(len(candidate), dtype=np.float32)

    dense_building = (
        (b_ratio >= float(cfg.get("dense_building_ratio_min", 0.74)))
        & (planarity >= float(cfg.get("dense_building_planarity_min", 0.26)))
        & (sphericity <= float(cfg.get("dense_building_sphericity_max", 0.31)))
    )
    # A whole tree crown can become Building in one model pass, making the
    # current local Building ratio artificially high. Pre-AI vegetation evidence
    # therefore vetoes the weak dense-cluster protection unless the geometry is
    # itself strongly planar. Roof/wall tests remain independent and protected.
    dense_building_safe = dense_building & (
        before_veg_ratio < float(cfg.get("dense_building_before_veg_max", 0.38))
    )
    structural = roof_like | wall_like | dense_building_safe

    # V3.6 production surface-completion guard. Real roof/facade edges can be
    # locally noisy even when they belong to a coherent structural surface.
    # Grow only from the already vegetation-vetoed structural seeds. This is
    # deliberately different from growing from raw planar candidates: tree
    # crowns can contain isolated planar leaves, but they do not form a dense
    # neighbourhood of accepted roof/wall seeds.
    if len(candidate) >= 8 and np.any(structural):
        ctree2 = cKDTree(xyz[candidate])
        kk2 = min(int(cfg.get("structural_expand_k", 14)), len(candidate))
        _, cnn2 = ctree2.query(xyz[candidate], k=max(2, kk2), workers=-1)
        cnn2 = np.asarray(cnn2, dtype=np.int64)
        if cnn2.ndim == 1:
            cnn2 = cnn2[:, None]
        accepted_seed_ratio = np.mean(structural[cnn2], axis=1)
        structural |= accepted_seed_ratio >= float(cfg.get("structural_expand_ratio_min", 0.20))
    else:
        accepted_seed_ratio = np.zeros(len(candidate), dtype=np.float32)

    original_veg = np.zeros(len(candidate), dtype=bool)
    original_ground = np.zeros(len(candidate), dtype=bool)
    ground_code = _safe_code(semantic_mapping, "ground")
    if before is not None:
        original_veg = np.isin(before[candidate], np.asarray(veg_codes, dtype=np.uint8)) if veg_codes else original_veg
        if ground_code is not None:
            original_ground = before[candidate] == np.uint8(ground_code)

    canopy_geometry = (
        (sphericity >= float(cfg.get("canopy_sphericity_min", 0.20)))
        | (planarity <= float(cfg.get("canopy_planarity_max", 0.28)))
        | ((zstd3 >= float(cfg.get("canopy_zstd_min", 0.28))) & (planarity < 0.36))
    )
    vegetation_evidence = (
        original_veg
        | (before_veg_ratio >= float(cfg.get("before_veg_ratio_min", 0.48)))
        | (current_veg_ratio >= float(cfg.get("current_veg_ratio_min", 0.38)))
    )

    # Ground/apron leakage is handled only when pre-AI Ground evidence exists;
    # this avoids converting a genuine low building to Ground by height alone.
    ground_leak = (
        original_ground
        & (local_height <= float(cfg.get("ground_leak_height_max", 0.55)))
    )
    # If the exact pre-AI point was vegetation, that is stronger evidence than
    # a single noisy local covariance estimate. Roll it back unless it has been
    # admitted to a coherent roof/wall surface above. This closes the failure
    # where an entire tree crown became Building and then looked locally dense.
    pre_ai_vegetation_leak = original_veg & (~structural)
    vegetation_leak = (
        pre_ai_vegetation_leak
        | (vegetation_evidence & canopy_geometry & (~structural))
    )
    rollback_local = vegetation_leak | ground_leak

    rollback_idx = candidate[rollback_local]
    changed_mask = np.zeros(n, dtype=bool)
    if rollback_idx.size:
        dest = _choose_vegetation_destination(
            rollback_idx,
            before,
            nb_before[rollback_local] if nb_before is not None else None,
            nb_current[rollback_local],
            semantic_mapping,
            local_height[rollback_local],
        )
        cls[rollback_idx] = dest
        changed_mask[rollback_idx] = True

    report = {
        "status": "APPLIED" if rollback_idx.size else "NO_CHANGE",
        "version": "BUILDING_VEG_GUARD_V3_6",
        "building_points_checked": int(candidate.size),
        "roof_protected": int(np.count_nonzero(roof_like)),
        "wall_protected": int(np.count_nonzero(wall_like)),
        "dense_building_protected": int(np.count_nonzero(dense_building_safe)),
        "vegetation_leak_candidates": int(np.count_nonzero(vegetation_leak)),
        "ground_leak_candidates": int(np.count_nonzero(ground_leak)),
        "rolled_back": int(rollback_idx.size),
    }
    return cls, changed_mask, report
