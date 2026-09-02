from __future__ import annotations

import math
from typing import Any, Mapping, Optional

import numpy as np
from scipy.spatial import cKDTree


def _log(log, message: str) -> None:
    try:
        if log is None:
            print(message, flush=True)
        elif hasattr(log, "info"):
            log.info(message)
        else:
            log(message)
    except Exception:
        print(message, flush=True)


def _active_indices(n: int, active_indices=None) -> np.ndarray:
    if active_indices is None:
        return np.arange(n, dtype=np.int64)
    idx = np.asarray(active_indices, dtype=np.int64).ravel()
    idx = idx[(idx >= 0) & (idx < n)]
    return np.unique(idx)


def _sample_indices_evenly(idx: np.ndarray, max_points: int) -> np.ndarray:
    idx = np.asarray(idx, dtype=np.int64)
    max_points = max(20, int(max_points))
    if idx.size <= max_points:
        return idx
    step = int(math.ceil(idx.size / float(max_points)))
    return idx[::step][:max_points]


def _hag_candidates_chunked(
    xyz: np.ndarray,
    query_idx: np.ndarray,
    ground_idx: np.ndarray,
    *,
    ground_k: int,
    ground_distance_max: float,
    low_candidate_hag: float,
    high_candidate_hag: float,
    query_chunk: int,
):
    """Return only vertical outlier candidates; never materialize HAG for all points."""
    gxy = xyz[ground_idx, :2]
    gz = xyz[ground_idx, 2]
    tree = cKDTree(gxy)
    idx_parts = []
    hag_parts = []
    gdist_parts = []
    try:
        kk = max(1, min(int(ground_k), len(ground_idx)))
        chunk = max(10_000, int(query_chunk))
        for start in range(0, len(query_idx), chunk):
            part = query_idx[start:start + chunk]
            finite = np.all(np.isfinite(xyz[part, :3]), axis=1)
            if not np.any(finite):
                continue
            q = part[finite]
            dist, nn = tree.query(xyz[q, :2], k=kk, workers=-1)
            if kk == 1:
                local_ground = gz[np.asarray(nn, dtype=np.int64)]
                near_dist = np.asarray(dist, dtype=np.float64)
            else:
                local_ground = np.median(gz[np.asarray(nn, dtype=np.int64)], axis=1)
                near_dist = np.asarray(dist[:, 0], dtype=np.float64)
            hag = xyz[q, 2] - local_ground
            keep = (
                (near_dist <= float(ground_distance_max))
                & ((hag <= float(low_candidate_hag)) | (hag >= float(high_candidate_hag)))
            )
            if np.any(keep):
                idx_parts.append(q[keep])
                hag_parts.append(hag[keep].astype(np.float32, copy=False))
                gdist_parts.append(near_dist[keep].astype(np.float32, copy=False))
    finally:
        del tree

    if not idx_parts:
        return (
            np.empty(0, dtype=np.int64),
            np.empty(0, dtype=np.float32),
            np.empty(0, dtype=np.float32),
        )
    return (
        np.concatenate(idx_parts).astype(np.int64, copy=False),
        np.concatenate(hag_parts).astype(np.float32, copy=False),
        np.concatenate(gdist_parts).astype(np.float32, copy=False),
    )


def _build_scene_support_indices(
    xyz: np.ndarray,
    candidate_idx: np.ndarray,
    *,
    max_points: int,
) -> np.ndarray:
    """Build deterministic read-only scene context and always include candidates.

    This intentionally samples the *full local scene*, not only other outlier
    candidates. Dense vegetation/roofs therefore provide real support and are
    not mistaken for isolated sky noise.
    """
    finite = np.all(np.isfinite(xyz[:, :3]), axis=1)
    idx = np.flatnonzero(finite)
    idx = _sample_indices_evenly(idx, int(max_points))
    if candidate_idx.size:
        idx = np.unique(np.concatenate([idx, np.asarray(candidate_idx, dtype=np.int64)]))
    return idx.astype(np.int64, copy=False)


def _scene_support_metrics(
    xyz: np.ndarray,
    candidate_idx: np.ndarray,
    support_idx: np.ndarray,
    *,
    support_k: int,
    envelope_k: int,
    envelope_radius: float,
    density_radius: float,
):
    """Full-scene 3-D isolation + local XY scene-top envelope metrics."""
    m = int(candidate_idx.size)
    if m == 0 or support_idx.size == 0:
        inf = np.full(m, np.inf, dtype=np.float32)
        nan = np.full(m, np.nan, dtype=np.float32)
        zeros = np.zeros(m, dtype=np.int32)
        return inf, inf, zeros, nan, nan

    support_xyz = xyz[support_idx, :3]
    cand_xyz = xyz[candidate_idx, :3]

    # 3-D real-scene support. Query more than four neighbours because the
    # candidate itself can be present in support_idx (distance == 0).
    tree3 = cKDTree(support_xyz)
    k3 = max(6, min(int(support_k), len(support_idx)))
    try:
        dist3, _ = tree3.query(cand_xyz, k=k3, workers=-1)
        if k3 == 1:
            dist3 = np.asarray(dist3, dtype=np.float64)[:, None]
        else:
            dist3 = np.asarray(dist3, dtype=np.float64)
        positive = np.where(dist3 > 1.0e-6, dist3, np.inf)
        positive.sort(axis=1)
        d1 = positive[:, 0].astype(np.float32)
        d4_col = min(3, positive.shape[1] - 1)
        d4 = positive[:, d4_col].astype(np.float32)
        density = np.asarray(
            tree3.query_ball_point(
                cand_xyz,
                r=float(density_radius),
                workers=-1,
                return_length=True,
            ),
            dtype=np.int32,
        )
        # If the candidate itself is present, remove that self-hit.
        density = np.maximum(density - 1, 0)
    finally:
        del tree3

    # Local scene envelope in XY. P90 rather than max ignores a few other
    # outliers while still following real vegetation/building tops.
    tree2 = cKDTree(support_xyz[:, :2])
    k2 = max(8, min(int(envelope_k), len(support_idx)))
    try:
        dist2, nn2 = tree2.query(cand_xyz[:, :2], k=k2, workers=-1)
    finally:
        del tree2

    if k2 == 1:
        dist2 = np.asarray(dist2, dtype=np.float64)[:, None]
        nn2 = np.asarray(nn2, dtype=np.int64)[:, None]
    else:
        dist2 = np.asarray(dist2, dtype=np.float64)
        nn2 = np.asarray(nn2, dtype=np.int64)

    z_neigh = support_xyz[nn2, 2].astype(np.float64, copy=False)
    valid = dist2 <= float(envelope_radius)
    z_masked = np.where(valid, z_neigh, np.nan)
    with np.errstate(all="ignore"):
        scene_p90 = np.nanpercentile(z_masked, 90.0, axis=1)
        scene_p50 = np.nanpercentile(z_masked, 50.0, axis=1)
    scene_gap = cand_xyz[:, 2] - scene_p90
    return (
        d1,
        d4,
        density,
        scene_p90.astype(np.float32),
        scene_gap.astype(np.float32),
    )


def apply_low_point_postprocess(
    xyz,
    classes,
    semantic_mapping: Mapping[str, int],
    *,
    active_indices=None,
    protected_mask=None,
    config: Optional[Mapping[str, Any]] = None,
    log=None,
):
    """Strictly separate below-terrain Low Point from above-scene High Noise.

    V3.23 production policy:
      * Low Point is BELOW the local terrain only; ordinary ground is never Low Point;
      * High Noise is ABOVE the local building/vegetation scene envelope;
      * Advanced worker Low Point output is provisional and must be revalidated;
      * rejected provisional Low Point points are released to Ground so LayeredGV
        can recover Ground / LowVeg / MidVeg / HighVeg;
      * numeric destinations always come from the active PTC.

    Any failure is fail-open and leaves model classifications unchanged.
    """
    cfg = dict(config or {})
    xyz = np.asarray(xyz, dtype=np.float64)
    out = np.asarray(classes).astype(np.uint8, copy=True)
    n = len(out)
    report = {
        "enabled": True,
        "version": "LOW_POINT_NOISE_GUARD_V3_23",
        "status": "NOT_RUN",
        "low_point_points_applied": 0,
        "high_noise_points_applied": 0,
        "below_ground_applied": 0,
        "above_ground_applied": 0,
    }

    if xyz.ndim != 2 or xyz.shape[0] != n or xyz.shape[1] < 3:
        report["status"] = "SKIPPED_BAD_XYZ"
        return out, np.zeros(n, dtype=bool), report

    ground_code = semantic_mapping.get("ground")
    low_point_code = semantic_mapping.get("low_point")
    high_noise_code = semantic_mapping.get("high_noise")
    if ground_code is None or low_point_code is None:
        report["status"] = "SKIPPED_PTC_SEMANTIC_MISSING"
        report["missing_semantics"] = [
            name for name, value in (("ground", ground_code), ("low_point", low_point_code))
            if value is None
        ]
        return out, np.zeros(n, dtype=bool), report

    active = _active_indices(n, active_indices)
    if active.size == 0:
        report["status"] = "SKIPPED_EMPTY_SCOPE"
        return out, np.zeros(n, dtype=bool), report

    protected = np.zeros(n, dtype=bool)
    if protected_mask is not None:
        pm = np.asarray(protected_mask, dtype=bool).reshape(-1)
        if len(pm) == n:
            protected = pm.copy()

    # V3.22: Advanced worker Low Point output is provisional until the strict
    # full-scene geometry detector validates it. Basic/Premium keep the legacy
    # authoritative behavior unless the caller explicitly opts in.
    revalidate_existing = bool(cfg.get("revalidate_existing", False))
    existing_low = active[out[active] == np.uint8(int(low_point_code))]
    if not revalidate_existing:
        protected |= out == np.uint8(int(low_point_code))

    # Only unprotected incoming Low Point points are eligible for revalidation.
    # This keeps the function compatible with an explicit caller protection mask.
    if existing_low.size:
        revalidatable_existing = existing_low[~protected[existing_low]]
        protected_existing = existing_low[protected[existing_low]]
    else:
        revalidatable_existing = np.empty(0, dtype=np.int64)
        protected_existing = np.empty(0, dtype=np.int64)

    report.update({
        "revalidate_existing": bool(revalidate_existing),
        "existing_lowpoint_before": int(existing_low.size),
        "existing_lowpoint_revalidated": int(revalidatable_existing.size),
        "existing_lowpoint_protected": int(protected_existing.size),
        "existing_lowpoint_retained": int(existing_low.size if not revalidate_existing else protected_existing.size),
        "existing_lowpoint_released_to_ground": 0,
        "new_lowpoint_detected": 0,
    })

    # Build the terrain reference BEFORE releasing provisional Low Point points.
    # This prevents a large bad class-7 block from contaminating the Ground surface.
    ground_scope = active[out[active] == np.uint8(int(ground_code))]
    if ground_scope.size >= 20:
        ground_idx = ground_scope
    else:
        ground_idx = np.flatnonzero(out == np.uint8(int(ground_code)))
    if ground_idx.size:
        finite_ground = np.all(np.isfinite(xyz[ground_idx, :3]), axis=1)
        ground_idx = ground_idx[finite_ground]
    ground_idx = _sample_indices_evenly(
        ground_idx,
        int(cfg.get("max_ground_points", 750_000)),
    )

    query_idx = active[~protected[active]]
    report.update({
        "ground_code": int(ground_code),
        "low_point_code": int(low_point_code),
        "high_noise_code": None if high_noise_code is None else int(high_noise_code),
        "ground_points_used": int(ground_idx.size),
        "scope_points": int(active.size),
        "query_points": int(query_idx.size),
    })
    if ground_idx.size < 20 or query_idx.size == 0:
        report["status"] = "NO_SAFE_INPUT"
        return out, np.zeros(n, dtype=bool), report

    # Deliberate dead-zone: the common detector does not create Low Point from
    # ordinary vegetation / near-surface structure. High candidates begin much
    # higher than V3.1 (5 m), because trees commonly exceed 5 m.
    candidate_idx, candidate_hag, candidate_gdist = _hag_candidates_chunked(
        xyz,
        query_idx,
        ground_idx,
        ground_k=int(cfg.get("ground_k", 5)),
        ground_distance_max=float(cfg.get("ground_distance_max", 14.0)),
        low_candidate_hag=float(cfg.get("low_candidate_hag", -0.90)),
        high_candidate_hag=float(cfg.get("high_candidate_hag", 10.0)),
        query_chunk=int(cfg.get("query_chunk", 250_000)),
    )
    report["vertical_outlier_candidates"] = int(candidate_idx.size)
    if candidate_idx.size == 0:
        # We had a valid Ground reference and no incoming Low Point survives even
        # the broad HAG candidate gate. Release only the provisional Advanced
        # Low Point points to Ground; LayeredGV will subsequently split them into
        # Ground / LowVeg / MidVeg / HighVeg from local HAG.
        if revalidate_existing and revalidatable_existing.size:
            out[revalidatable_existing] = np.uint8(int(ground_code))
            report["existing_lowpoint_retained"] = int(protected_existing.size)
            report["existing_lowpoint_released_to_ground"] = int(revalidatable_existing.size)
            report["status"] = "REVALIDATED_EXISTING_LOW_POINT"
            _log(
                log,
                "[COMMON NoiseGuard V3.23] "
                f"incoming_low={existing_low.size:,} revalidated={revalidatable_existing.size:,} "
                f"kept_below={protected_existing.size:,} released_to_GV={revalidatable_existing.size:,} "
                "high_noise=0",
            )
            return out, np.zeros(n, dtype=bool), report
        report["status"] = "NO_LOW_POINT"
        return out, np.zeros(n, dtype=bool), report

    support_idx = _build_scene_support_indices(
        xyz,
        candidate_idx,
        max_points=int(cfg.get("max_scene_support_points", 1_750_000)),
    )
    d1, d4, local_density, scene_p90, scene_gap = _scene_support_metrics(
        xyz,
        candidate_idx,
        support_idx,
        support_k=int(cfg.get("scene_support_k", 10)),
        envelope_k=int(cfg.get("scene_envelope_k", 32)),
        envelope_radius=float(cfg.get("scene_envelope_radius", 8.0)),
        density_radius=float(cfg.get("scene_density_radius", 1.25)),
    )

    h = candidate_hag.astype(np.float32, copy=False)
    gap = scene_gap.astype(np.float32, copy=False)

    # ---------------------------- BELOW TERRAIN ----------------------------
    # Moderate below-ground noise must have very little real 3-D support.
    below_moderate = (
        (h <= float(cfg.get("below_hag", -1.00)))
        & (d1 >= float(cfg.get("below_d1", 0.45)))
        & (d4 >= float(cfg.get("below_d4", 1.20)))
        & (local_density <= int(cfg.get("below_density_max", 2)))
    )
    below_severe = (
        (h <= float(cfg.get("below_severe_hag", -2.50)))
        & (d1 >= float(cfg.get("below_severe_d1", 0.25)))
        & (d4 >= float(cfg.get("below_severe_d4", 0.75)))
        & (local_density <= int(cfg.get("below_severe_density_max", 4)))
    )

    # ------------------------------- SKY ----------------------------------
    # A point must be above both terrain and the *local scene top*. This is the
    # key vegetation safeguard missing in V3.1.
    finite_gap = np.isfinite(gap)
    above_moderate = (
        (h >= float(cfg.get("above_hag", 10.0)))
        & finite_gap
        & (gap >= float(cfg.get("above_scene_gap", 4.0)))
        & (d1 >= float(cfg.get("above_d1", 0.55)))
        & (d4 >= float(cfg.get("above_d4", 1.60)))
        & (local_density <= int(cfg.get("above_density_max", 2)))
    )
    above_severe = (
        (h >= float(cfg.get("above_severe_hag", 18.0)))
        & finite_gap
        & (gap >= float(cfg.get("above_severe_scene_gap", 3.0)))
        & (d1 >= float(cfg.get("above_severe_d1", 0.30)))
        & (d4 >= float(cfg.get("above_severe_d4", 1.00)))
        & (local_density <= int(cfg.get("above_severe_density_max", 4)))
    )
    above_extreme = (
        (h >= float(cfg.get("above_extreme_hag", 35.0)))
        & finite_gap
        & (gap >= float(cfg.get("above_extreme_scene_gap", 2.0)))
        & (d4 >= float(cfg.get("above_extreme_d4", 0.70)))
    )

    below = below_moderate | below_severe
    above = above_moderate | above_severe | above_extreme

    below_detected = candidate_idx[below]
    above_detected = candidate_idx[above]

    def _filter_detected(idx: np.ndarray) -> np.ndarray:
        if idx.size == 0:
            return np.empty(0, dtype=np.int64)
        pos = np.searchsorted(active, idx)
        clipped = np.minimum(pos, max(len(active) - 1, 0))
        in_scope = (pos < len(active)) & (active[clipped] == idx)
        return idx[in_scope & (~protected[idx])]

    below_detected = _filter_detected(below_detected)
    above_detected = _filter_detected(above_detected)

    # Release provisional Advanced Low Point BEFORE re-applying only strict
    # below-terrain Low Point.  Surface/vegetation failures enter LayeredGV.
    if revalidate_existing and revalidatable_existing.size:
        out[revalidatable_existing] = np.uint8(int(ground_code))

    low_mask = np.zeros(n, dtype=bool)
    if below_detected.size:
        low_mask[below_detected] = True
        out[below_detected] = np.uint8(int(low_point_code))

    high_noise_written = np.empty(0, dtype=np.int64)
    if high_noise_code is not None and above_detected.size:
        high_noise_written = above_detected
        out[high_noise_written] = np.uint8(int(high_noise_code))

    if revalidate_existing:
        retained_revalidated = int(
            np.intersect1d(revalidatable_existing, below_detected, assume_unique=True).size
        ) if (revalidatable_existing.size and below_detected.size) else 0
        existing_to_high = int(
            np.intersect1d(revalidatable_existing, high_noise_written, assume_unique=True).size
        ) if (revalidatable_existing.size and high_noise_written.size) else 0
        released_existing = int(revalidatable_existing.size) - retained_revalidated - existing_to_high
        released_existing = max(0, released_existing)
        new_low = int(
            below_detected.size - np.intersect1d(existing_low, below_detected, assume_unique=True).size
        ) if (below_detected.size and existing_low.size) else int(below_detected.size)
        new_high = int(
            high_noise_written.size - np.intersect1d(existing_low, high_noise_written, assume_unique=True).size
        ) if (high_noise_written.size and existing_low.size) else int(high_noise_written.size)
        report["existing_lowpoint_retained"] = int(protected_existing.size) + retained_revalidated
        report["existing_lowpoint_released_to_ground"] = released_existing
        report["existing_lowpoint_to_high_noise"] = existing_to_high
        report["new_lowpoint_detected"] = new_low
        report["new_high_noise_detected"] = new_high

    below_written = int(below_detected.size)
    above_written = int(high_noise_written.size)
    status = "NO_OUTLIER"
    if below_written or above_written:
        status = "APPLIED_LOWPOINT_HIGHNOISE"
    elif revalidate_existing and revalidatable_existing.size:
        status = "REVALIDATED_EXISTING_LOW_POINT"

    report.update({
        "status": status,
        "low_point_points_applied": below_written,
        "high_noise_points_applied": above_written,
        "below_ground_applied": below_written,
        "above_ground_applied": above_written,
        "high_noise_unmapped_candidates": int(above_detected.size if high_noise_code is None else 0),
        "scene_support_points": int(support_idx.size),
        "candidate_hag_min": float(np.min(h)) if h.size else None,
        "candidate_hag_max": float(np.max(h)) if h.size else None,
        "candidate_scene_gap_p50": float(np.nanmedian(gap)) if gap.size else None,
        "median_candidate_ground_distance_m": float(np.median(candidate_gdist)) if candidate_gdist.size else None,
    })

    if revalidate_existing:
        _log(
            log,
            "[COMMON NoiseGuard V3.23] "
            f"status={report['status']} incoming_low={report['existing_lowpoint_before']:,} "
            f"revalidated={report['existing_lowpoint_revalidated']:,} "
            f"kept_below={report['existing_lowpoint_retained']:,} "
            f"released_to_GV={report['existing_lowpoint_released_to_ground']:,} "
            f"to_high_noise={report.get('existing_lowpoint_to_high_noise', 0):,} "
            f"new_low={report.get('new_lowpoint_detected', 0):,} "
            f"new_high_noise={report.get('new_high_noise_detected', 0):,} "
            f"support={support_idx.size:,} -> LowPoint {int(low_point_code)} / "
            f"HighNoise {None if high_noise_code is None else int(high_noise_code)}",
        )
    else:
        _log(
            log,
            "[COMMON NoiseGuard V3.23] "
            f"below_lowpoint={below_written:,} high_noise={above_written:,} "
            f"support={support_idx.size:,} -> LowPoint {int(low_point_code)} / "
            f"HighNoise {None if high_noise_code is None else int(high_noise_code)}",
        )
    return out, low_mask, report
