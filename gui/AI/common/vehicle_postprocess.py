from __future__ import annotations

import math
from typing import Any, Mapping, Optional, Sequence, Tuple

import numpy as np
from scipy.spatial import cKDTree, ConvexHull

try:
    from sklearn.cluster import DBSCAN
    HAS_SKLEARN_DBSCAN = True
except Exception:
    DBSCAN = None
    HAS_SKLEARN_DBSCAN = False

try:
    # Reuse the detector geometry already validated in Naksha Premium instead
    # of creating a second, contradictory definition of a vehicle.
    from gui.AI.vehicle.lidar_vehicle_detector import score_component as _premium_score_component
except Exception:
    _premium_score_component = None




def _query_ball_point(tree, pts, radius, *, return_length=False):
    """SciPy compatibility wrapper (workers/return_length vary by version)."""
    kwargs = {"r": float(radius)}
    if return_length:
        kwargs["return_length"] = True
    try:
        return tree.query_ball_point(pts, workers=-1, **kwargs)
    except TypeError:
        try:
            return tree.query_ball_point(pts, **kwargs)
        except TypeError:
            # Older SciPy without return_length support.
            if return_length:
                neigh = tree.query_ball_point(pts, r=float(radius))
                return np.asarray([len(v) for v in neigh], dtype=np.int32)
            return tree.query_ball_point(pts, r=float(radius))


def _query_nearest(tree, pts, radius):
    try:
        return tree.query(pts, k=1, distance_upper_bound=float(radius), workers=-1)
    except TypeError:
        return tree.query(pts, k=1, distance_upper_bound=float(radius))


def _dbscan_labels_scipy(xy: np.ndarray, eps: float, min_samples: int) -> np.ndarray:
    """Memory-bounded DBSCAN-compatible fallback using only SciPy cKDTree.

    Core-point connectivity follows the DBSCAN definition. Border points are
    attached to the nearest core point within eps. This avoids introducing a
    mandatory scikit-learn dependency into the desktop production environment.
    """
    xy = np.asarray(xy, dtype=np.float64)
    n = len(xy)
    labels = np.full(n, -1, dtype=np.int32)
    if n == 0:
        return labels

    eps = max(float(eps), 1e-6)
    min_samples = max(1, int(min_samples))
    tree = cKDTree(xy)

    # Compute core status without materializing every neighborhood at once.
    counts = np.empty(n, dtype=np.int32)
    count_chunk = 50_000
    for start in range(0, n, count_chunk):
        end = min(start + count_chunk, n)
        counts[start:end] = np.asarray(
            _query_ball_point(tree, xy[start:end], eps, return_length=True),
            dtype=np.int32,
        )
    core_idx = np.flatnonzero(counts >= min_samples).astype(np.int64)
    if core_idx.size == 0:
        return labels

    m = int(core_idx.size)
    core_pos = np.full(n, -1, dtype=np.int64)
    core_pos[core_idx] = np.arange(m, dtype=np.int64)
    parent = np.arange(m, dtype=np.int64)
    rank = np.zeros(m, dtype=np.uint8)

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = int(parent[a])
        return a

    def union(a: int, b: int) -> None:
        ra = find(a)
        rb = find(b)
        if ra == rb:
            return
        if rank[ra] < rank[rb]:
            parent[ra] = rb
        elif rank[ra] > rank[rb]:
            parent[rb] = ra
        else:
            parent[rb] = ra
            rank[ra] += 1

    # Chunked neighborhood expansion keeps temporary Python lists bounded.
    query_chunk = 4_000
    for start in range(0, m, query_chunk):
        end = min(start + query_chunk, m)
        neighborhoods = _query_ball_point(tree, xy[core_idx[start:end]], eps)
        for off, neigh in enumerate(neighborhoods):
            ci = start + off
            neigh = np.asarray(neigh, dtype=np.int64)
            if neigh.size == 0:
                continue
            cj = core_pos[neigh]
            cj = cj[cj > ci]  # avoid duplicate/self unions
            for other in cj.tolist():
                union(ci, int(other))

    roots = np.empty(m, dtype=np.int64)
    for i in range(m):
        roots[i] = find(i)
    _, compact = np.unique(roots, return_inverse=True)
    core_labels = compact.astype(np.int32, copy=False)
    labels[core_idx] = core_labels

    # DBSCAN border points: attach to nearest core point within eps.
    noncore_idx = np.flatnonzero(counts < min_samples).astype(np.int64)
    if noncore_idx.size:
        core_tree = cKDTree(xy[core_idx])
        near_chunk = 100_000
        for start in range(0, len(noncore_idx), near_chunk):
            part = noncore_idx[start:start + near_chunk]
            dist, near = _query_nearest(core_tree, xy[part], eps)
            dist = np.asarray(dist)
            near = np.asarray(near, dtype=np.int64)
            valid = np.isfinite(dist) & (near >= 0) & (near < m)
            if np.any(valid):
                labels[part[valid]] = core_labels[near[valid]]

    return labels


def _dbscan_labels(xy: np.ndarray, eps: float, min_samples: int, backend: str = "auto"):
    backend = str(backend or "auto").strip().lower()
    if backend not in {"auto", "sklearn", "scipy"}:
        backend = "auto"

    if backend in {"auto", "sklearn"} and HAS_SKLEARN_DBSCAN:
        labels = DBSCAN(
            eps=float(eps),
            min_samples=int(min_samples),
            algorithm="kd_tree",
            n_jobs=-1,
        ).fit_predict(xy)
        return np.asarray(labels, dtype=np.int32), "sklearn"

    # Production fallback: SciPy is already required by Naksha AI.
    return _dbscan_labels_scipy(xy, eps, min_samples), "scipy"


def _interval_score(v, ideal_lo, ideal_hi, hard_lo, hard_hi):
    if v < hard_lo or v > hard_hi:
        return 0.0
    if ideal_lo <= v <= ideal_hi:
        return 1.0
    if v < ideal_lo:
        return max(0.0, (v - hard_lo) / max(ideal_lo - hard_lo, 1e-9))
    return max(0.0, (hard_hi - v) / max(hard_hi - ideal_hi, 1e-9))


def _fallback_score_component(xyz, hag, ground_dist):
    """Embedded copy of Naksha's validated Premium vehicle geometry score.

    Keeping this tiny fallback here means Basic/Advanced vehicle cleanup does
    not fail just because the Premium CLI module has an optional import issue.
    """
    xy = np.asarray(xyz[:, :2], dtype=np.float64)
    center = np.median(xy, axis=0)
    q = xy - center
    cov = np.cov(q.T)
    vals, vecs = np.linalg.eigh(cov)
    axes = vecs[:, np.argsort(vals)[::-1]]
    proj = q @ axes
    lo = np.percentile(proj, 2.0, axis=0)
    hi = np.percentile(proj, 98.0, axis=0)
    dims = hi - lo
    major = int(np.argmax(dims))
    minor = 1 - major
    length = float(dims[major])
    width = float(dims[minor])
    major_axis = axes[:, major]
    yaw = float(math.atan2(major_axis[1], major_axis[0]))
    local_center = (lo + hi) / 2.0
    world_center = center + axes @ local_center

    area = max(length * width, 1e-6)
    aspect = length / max(width, 1e-6)
    h10, h50, h90, h95 = np.percentile(hag, [10, 50, 90, 95])
    z_span = float(np.percentile(xyz[:, 2], 95) - np.percentile(xyz[:, 2], 5))

    x0 = np.median(xyz[:, 0])
    y0 = np.median(xyz[:, 1])
    A = np.column_stack((xyz[:, 0] - x0, xyz[:, 1] - y0, np.ones(len(xyz))))
    try:
        coef, *_ = np.linalg.lstsq(A, xyz[:, 2], rcond=None)
        residual = xyz[:, 2] - A @ coef
        rough = float(np.median(np.abs(residual - np.median(residual))))
    except Exception:
        rough = 999.0

    try:
        rect = float(np.clip(float(ConvexHull(xy).volume) / area, 0.0, 1.0)) if len(xy) >= 4 else 0.0
    except Exception:
        rect = 0.0
    gdist = float(np.median(ground_dist))
    density = float(len(xyz) / area)

    car_dim = (
        0.45 * _interval_score(length, 2.2, 6.2, 1.6, 8.0)
        + 0.35 * _interval_score(width, 1.3, 2.7, 0.9, 3.4)
        + 0.20 * _interval_score(aspect, 1.25, 4.5, 1.0, 6.5)
    )
    long_dim = (
        0.50 * _interval_score(length, 5.0, 13.5, 3.5, 16.0)
        + 0.30 * _interval_score(width, 1.7, 3.2, 1.2, 3.7)
        + 0.20 * _interval_score(aspect, 1.6, 6.0, 1.1, 8.0)
    )
    dim_score = max(car_dim, long_dim)
    score = (
        0.24 * dim_score
        + 0.16 * _interval_score(h90, 0.75, 3.8, 0.35, 4.8)
        + 0.10 * _interval_score(area, 3.0, 35.0, 1.5, 55.0)
        + 0.18 * _interval_score(rough, 0.0, 0.22, 0.0, 0.65)
        + 0.14 * _interval_score(rect, 0.50, 1.0, 0.22, 1.0)
        + 0.08 * _interval_score(gdist, 0.0, 1.2, 0.0, 3.0)
        + 0.05 * _interval_score(density, 1.2, 200.0, 0.35, 400.0)
        + 0.05 * _interval_score(z_span, 0.10, 2.5, 0.05, 4.0)
    )
    metrics = {
        "length_m": length, "width_m": width, "footprint_area_m2": area,
        "aspect_ratio": aspect, "hag_p10_m": float(h10),
        "hag_median_m": float(h50), "hag_p90_m": float(h90),
        "hag_p95_m": float(h95), "z_span_p95_p05_m": z_span,
        "plane_mad_m": rough, "rectangularity": rect,
        "median_ground_distance_m": gdist, "point_density_per_m2": density,
        "score": float(score), "center_x": float(world_center[0]),
        "center_y": float(world_center[1]), "yaw_rad": yaw,
    }
    return float(score), metrics


def _score_component(xyz, hag, ground_dist):
    if _premium_score_component is not None:
        score, metrics = _premium_score_component(xyz, hag, ground_dist)
    else:
        score, metrics = _fallback_score_component(xyz, hag, ground_dist)

    # V3.5 vegetation veto: a real vehicle normally has a coherent roof/body
    # surface.  Shrubs can accidentally match vehicle XY dimensions, but their
    # upper half remains rough and non-planar.  Compute this independently so
    # the guard works with both the Premium scorer and the embedded fallback.
    pts = np.asarray(xyz, dtype=np.float64)
    hh = np.asarray(hag, dtype=np.float64)
    try:
        zcut = float(np.percentile(pts[:, 2], 55.0))
        top = pts[:, 2] >= zcut
        top_pts = pts[top]
        if len(top_pts) >= 8:
            x0 = float(np.median(top_pts[:, 0]))
            y0 = float(np.median(top_pts[:, 1]))
            A = np.column_stack((top_pts[:, 0] - x0, top_pts[:, 1] - y0, np.ones(len(top_pts))))
            coef, *_ = np.linalg.lstsq(A, top_pts[:, 2], rcond=None)
            r = top_pts[:, 2] - A @ coef
            top_plane_mad = float(np.median(np.abs(r - np.median(r))))
        else:
            top_plane_mad = 999.0
    except Exception:
        top_plane_mad = 999.0
    try:
        h10, h25, h50, h75, h90 = np.percentile(hh, [10, 25, 50, 75, 90])
        hag_iqr = float(h75 - h25)
        hag_core_span = float(h90 - h10)
    except Exception:
        h10 = h50 = h90 = -999.0
        hag_iqr = hag_core_span = 999.0
    metrics = dict(metrics or {})
    metrics.update({
        "top_plane_mad_m": float(top_plane_mad),
        "hag_iqr_m": float(hag_iqr),
        "hag_core_span_m": float(hag_core_span),
        "hag_p10_m": float(metrics.get("hag_p10_m", h10)),
        "hag_median_m": float(metrics.get("hag_median_m", h50)),
        "hag_p90_m": float(metrics.get("hag_p90_m", h90)),
    })
    return float(score), metrics


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
    """Deterministic memory guard while preserving broad scan coverage."""
    idx = np.asarray(idx, dtype=np.int64)
    max_points = max(20, int(max_points))
    if idx.size <= max_points:
        return idx
    step = int(math.ceil(idx.size / float(max_points)))
    return idx[::step][:max_points]


def _hard_vehicle_ok(metrics: Mapping[str, Any]) -> bool:
    """V3.5 hard acceptance: vehicle-like, not merely compact vegetation."""
    L = float(metrics.get("length_m", 0.0))
    W = float(metrics.get("width_m", 0.0))
    H10 = float(metrics.get("hag_p10_m", -999.0))
    H90 = float(metrics.get("hag_p90_m", -999.0))
    area = float(metrics.get("footprint_area_m2", 0.0))
    rough = float(metrics.get("plane_mad_m", 999.0))
    rect = float(metrics.get("rectangularity", 0.0))
    zspan = float(metrics.get("z_span_p95_p05_m", 999.0))
    top_mad = float(metrics.get("top_plane_mad_m", 999.0))
    hag_iqr = float(metrics.get("hag_iqr_m", 999.0))
    hag_core = float(metrics.get("hag_core_span_m", 999.0))
    aspect = float(metrics.get("aspect_ratio", 0.0))
    return (
        1.9 <= L <= 15.0
        and 1.05 <= W <= 3.5
        and 1.10 <= aspect <= 7.0
        and 0.45 <= H90 <= 4.3
        and H10 <= 1.75
        and 2.5 <= area <= 48.0
        and rough <= 0.42
        and top_mad <= 0.26
        and rect >= 0.38
        and zspan <= 3.6
        and hag_iqr <= 1.55
        and hag_core <= 3.3
    )


def _cluster_components(
    candidate_idx: np.ndarray,
    xyz: np.ndarray,
    hag: np.ndarray,
    ground_dist: np.ndarray,
    *,
    eps: float,
    min_samples: int,
    threshold: float,
    cluster_backend: str = "auto",
):
    """Return exact accepted cluster point arrays plus JSON-safe metadata."""
    if candidate_idx.size < max(8, min_samples):
        return [], "none"

    labels, used_backend = _dbscan_labels(
        xyz[candidate_idx, :2],
        eps=float(eps),
        min_samples=int(min_samples),
        backend=cluster_backend,
    )

    accepted = []
    for lab in np.unique(labels):
        if int(lab) < 0:
            continue
        local = np.flatnonzero(labels == lab)
        if local.size < max(8, int(min_samples)):
            continue
        gidx = candidate_idx[local]
        score, metrics = _score_component(
            xyz[gidx],
            hag[local],
            ground_dist[local],
        )
        if _hard_vehicle_ok(metrics) and float(score) >= float(threshold):
            meta = {"cluster_id": int(lab), "points": int(len(gidx)), **metrics}
            accepted.append((gidx.astype(np.int64, copy=False), meta))
    return accepted, used_backend


def _cluster_global(
    candidate_idx: np.ndarray,
    xyz: np.ndarray,
    hag: np.ndarray,
    ground_dist: np.ndarray,
    *,
    eps: float,
    min_samples: int,
    threshold: float,
    cluster_backend: str = "auto",
) -> Tuple[np.ndarray, list, str]:
    parts, used_backend = _cluster_components(
        candidate_idx,
        xyz,
        hag,
        ground_dist,
        eps=eps,
        min_samples=min_samples,
        threshold=threshold,
        cluster_backend=cluster_backend,
    )
    if not parts:
        return np.empty(0, dtype=np.int64), [], used_backend
    return (
        np.unique(np.concatenate([p[0] for p in parts])).astype(np.int64),
        [p[1] for p in parts],
        used_backend,
    )


def _cluster_tiled(
    candidate_idx: np.ndarray,
    xyz: np.ndarray,
    candidate_hag: np.ndarray,
    candidate_gdist: np.ndarray,
    *,
    tile_size: float,
    overlap: float,
    eps: float,
    min_samples: int,
    threshold: float,
    max_context: int,
    min_core: float,
    cluster_backend: str = "auto",
) -> Tuple[np.ndarray, list, int, str]:
    """Memory-bounded DBSCAN with exact overlap de-duplication by cluster center."""
    if candidate_idx.size == 0:
        return np.empty(0, dtype=np.int64), [], 0, "none"

    pxy = xyz[candidate_idx, :2]
    xmin, ymin = np.min(pxy, axis=0)
    xmax, ymax = np.max(pxy, axis=0)
    tile_size = max(float(tile_size), float(min_core))
    overlap = max(float(overlap), 2.0)

    accepted_parts = []
    accepted_meta = []
    processed = 0
    backends_used = set()

    def process_core(x0, x1, y0, y1, depth=0):
        nonlocal processed
        context_mask = (
            (pxy[:, 0] >= x0 - overlap) & (pxy[:, 0] <= x1 + overlap)
            & (pxy[:, 1] >= y0 - overlap) & (pxy[:, 1] <= y1 + overlap)
        )
        pos = np.flatnonzero(context_mask)
        if pos.size < max(8, min_samples):
            return

        width = max(x1 - x0, y1 - y0)
        if pos.size > int(max_context) and width > float(min_core) * 1.5 and depth < 5:
            xm = (x0 + x1) * 0.5
            ym = (y0 + y1) * 0.5
            process_core(x0, xm, y0, ym, depth + 1)
            process_core(xm, x1, y0, ym, depth + 1)
            process_core(x0, xm, ym, y1, depth + 1)
            process_core(xm, x1, ym, y1, depth + 1)
            return
        if pos.size > int(max_context):
            # Production fail-open: never run an unbounded DBSCAN that could
            # freeze the GUI or exhaust RAM. The model output is preserved.
            return

        processed += 1
        gidx = candidate_idx[pos]
        parts, used_backend = _cluster_components(
            gidx,
            xyz,
            candidate_hag[pos],
            candidate_gdist[pos],
            eps=eps,
            min_samples=min_samples,
            threshold=threshold,
            cluster_backend=cluster_backend,
        )
        backends_used.add(used_backend)
        for cluster_idx, meta in parts:
            cx = float(meta.get("center_x", math.inf))
            cy = float(meta.get("center_y", math.inf))
            # Each overlap cluster belongs to exactly one core tile.  Unlike the
            # older prototype, append only this exact cluster's indices.
            if x0 <= cx < x1 + 1e-9 and y0 <= cy < y1 + 1e-9:
                accepted_parts.append(cluster_idx)
                accepted_meta.append(meta)

    nx = max(1, int(math.ceil(max(xmax - xmin, 1e-9) / tile_size)))
    ny = max(1, int(math.ceil(max(ymax - ymin, 1e-9) / tile_size)))
    for iy in range(ny):
        y0 = ymin + iy * tile_size
        y1 = ymax + 1e-9 if iy == ny - 1 else min(y0 + tile_size, ymax + 1e-9)
        for ix in range(nx):
            x0 = xmin + ix * tile_size
            x1 = xmax + 1e-9 if ix == nx - 1 else min(x0 + tile_size, xmax + 1e-9)
            process_core(x0, x1, y0, y1, 0)

    backend_used = "+".join(sorted(b for b in backends_used if b and b != "none")) or "none"
    if accepted_parts:
        return np.unique(np.concatenate(accepted_parts)).astype(np.int64), accepted_meta, processed, backend_used
    return np.empty(0, dtype=np.int64), accepted_meta, processed, backend_used


def _vehicle_prefilter_chunked(
    xyz: np.ndarray,
    eligible_idx: np.ndarray,
    ground_idx: np.ndarray,
    *,
    ground_k: int,
    ground_distance_max: float,
    hag_min: float,
    hag_max: float,
    query_chunk: int,
):
    ground_xy = xyz[ground_idx, :2]
    ground_z = xyz[ground_idx, 2]
    tree = cKDTree(ground_xy)
    idx_parts = []
    hag_parts = []
    dist_parts = []
    try:
        kk = max(1, min(int(ground_k), len(ground_idx)))
        qchunk = max(10_000, int(query_chunk))
        for start in range(0, len(eligible_idx), qchunk):
            part = eligible_idx[start:start + qchunk]
            dist, nn = tree.query(xyz[part, :2], k=kk, workers=-1)
            if kk == 1:
                local_ground = ground_z[np.asarray(nn, dtype=np.int64)]
                near_dist = np.asarray(dist, dtype=np.float64)
            else:
                local_ground = np.median(
                    ground_z[np.asarray(nn, dtype=np.int64)], axis=1
                )
                near_dist = np.asarray(dist[:, 0], dtype=np.float64)
            hag = xyz[part, 2] - local_ground
            keep = (
                (near_dist <= float(ground_distance_max))
                & (hag >= float(hag_min))
                & (hag <= float(hag_max))
            )
            if np.any(keep):
                idx_parts.append(part[keep])
                hag_parts.append(hag[keep].astype(np.float32, copy=False))
                dist_parts.append(near_dist[keep].astype(np.float32, copy=False))
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
        np.concatenate(dist_parts).astype(np.float32, copy=False),
    )


def apply_vehicle_postprocess(
    xyz,
    classes,
    semantic_mapping: Mapping[str, int],
    *,
    active_indices=None,
    protected_mask=None,
    legacy_input_codes: Sequence[int] = (),
    config: Optional[Mapping[str, Any]] = None,
    log=None,
):
    """Detect vehicle-shaped LiDAR clusters and assign PTC Uncategorized.

    V3.5 adds a strict upper-surface planarity/roughness veto so vegetation is
    not erased into Uncategorized merely because a bush has car-like XY size.

    It is deliberately post-model and semantic-code driven, so Basic, Advanced
    and Premium can share the same behavior without changing any model weights.
    Ground is never eligible.  Any failure is fail-open and preserves the model
    classifications.
    """
    cfg = dict(config or {})
    xyz = np.asarray(xyz, dtype=np.float64)
    src = np.asarray(classes)
    out = src.astype(np.uint8, copy=True)
    n = len(out)
    report = {
        "enabled": True,
        "version": "VEHICLE_V3_5_VEG_SAFE",
        "status": "NOT_RUN",
        "vehicle_points_applied": 0,
        "accepted_vehicle_clusters": 0,
    }

    if xyz.ndim != 2 or xyz.shape[0] != n or xyz.shape[1] < 3:
        report["status"] = "SKIPPED_BAD_XYZ"
        return out, np.zeros(n, dtype=bool), report

    ground_code = semantic_mapping.get("ground")
    uncat_code = semantic_mapping.get("uncategorized")
    eligible_codes = [
        semantic_mapping.get("low_vegetation"),
        semantic_mapping.get("medium_vegetation"),
        semantic_mapping.get("high_vegetation"),
        semantic_mapping.get("building"),
    ]
    eligible_codes = {int(v) for v in eligible_codes if v is not None}
    for code in legacy_input_codes or ():
        try:
            eligible_codes.add(int(code))
        except Exception:
            pass
    if ground_code is not None:
        eligible_codes.discard(int(ground_code))

    if ground_code is None or uncat_code is None or not eligible_codes:
        report["status"] = "SKIPPED_PTC_SEMANTIC_MISSING"
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

    # Ground context may come from outside a fence, but writes are exact-scope.
    ground_idx_scope = active[out[active] == np.uint8(int(ground_code))]
    if ground_idx_scope.size >= 20:
        ground_idx = ground_idx_scope
    else:
        ground_idx = np.flatnonzero(out == np.uint8(int(ground_code)))
    ground_idx = _sample_indices_evenly(
        ground_idx,
        int(cfg.get("max_ground_points", 750_000)),
    )

    active_classes = out[active]
    eligible_local = np.isin(
        active_classes,
        np.asarray(sorted(eligible_codes), dtype=np.uint8),
    ) & (~protected[active])
    eligible_idx = active[eligible_local]

    report.update({
        "ground_code": int(ground_code),
        "uncategorized_code": int(uncat_code),
        "ground_points_used": int(ground_idx.size),
        "eligible_points": int(eligible_idx.size),
    })
    if ground_idx.size < 20 or eligible_idx.size == 0:
        report["status"] = "NO_SAFE_INPUT"
        return out, np.zeros(n, dtype=bool), report

    candidate_idx, candidate_hag, candidate_gdist = _vehicle_prefilter_chunked(
        xyz,
        eligible_idx,
        ground_idx,
        ground_k=int(cfg.get("ground_k", 4)),
        ground_distance_max=float(cfg.get("ground_distance_max", 3.0)),
        hag_min=float(cfg.get("hag_min", 0.35)),
        hag_max=float(cfg.get("hag_max", 4.8)),
        query_chunk=int(cfg.get("query_chunk", 250_000)),
    )
    report["prefilter_points"] = int(candidate_idx.size)
    if candidate_idx.size == 0:
        report["status"] = "NO_VEHICLE"
        return out, np.zeros(n, dtype=bool), report

    threshold = float(cfg.get("score_threshold", 0.84))
    max_global = int(cfg.get("global_dbscan_max", 180_000))
    if candidate_idx.size <= max_global:
        detected, meta, cluster_backend = _cluster_global(
            candidate_idx,
            xyz,
            candidate_hag,
            candidate_gdist,
            eps=float(cfg.get("eps", 0.60)),
            min_samples=int(cfg.get("min_samples", 6)),
            threshold=threshold,
            cluster_backend=str(cfg.get("cluster_backend", "auto")),
        )
        tiles_processed = 1
    else:
        detected, meta, tiles_processed, cluster_backend = _cluster_tiled(
            candidate_idx,
            xyz,
            candidate_hag,
            candidate_gdist,
            tile_size=float(cfg.get("tile_size", 80.0)),
            overlap=float(cfg.get("overlap", 18.0)),
            eps=float(cfg.get("eps", 0.60)),
            min_samples=int(cfg.get("min_samples", 6)),
            threshold=threshold,
            max_context=int(cfg.get("max_tile_candidates", 180_000)),
            min_core=float(cfg.get("min_core_size", 20.0)),
            cluster_backend=str(cfg.get("cluster_backend", "auto")),
        )

    vehicle_mask = np.zeros(n, dtype=bool)
    if detected.size:
        # Final write guard: only exact scope + eligible semantic inputs.
        pos = np.searchsorted(active, detected)
        in_scope = (pos < len(active)) & (active[np.minimum(pos, len(active) - 1)] == detected)
        safe = in_scope & (~protected[detected]) & np.isin(
            out[detected], np.asarray(sorted(eligible_codes), dtype=np.uint8)
        )
        detected = detected[safe]
        vehicle_mask[detected] = True
        out[detected] = np.uint8(int(uncat_code))

    report.update({
        "status": "APPLIED_UNCATEGORIZED" if detected.size else "NO_VEHICLE",
        "accepted_vehicle_clusters": int(len(meta)),
        "vehicle_points_detected": int(detected.size),
        "vehicle_points_applied": int(detected.size),
        "tiles_processed": int(tiles_processed),
        "cluster_backend": str(cluster_backend),
        "sklearn_available": bool(HAS_SKLEARN_DBSCAN),
        "candidates": sorted(meta, key=lambda row: float(row.get("score", 0.0)), reverse=True)[:200],
    })
    _log(
        log,
        "[COMMON Vehicle] "
        f"status={report['status']} backend={report.get('cluster_backend', 'n/a')} "
        f"clusters={report['accepted_vehicle_clusters']} "
        f"points={report['vehicle_points_applied']} -> class {int(uncat_code)}",
    )
    return out, vehicle_mask, report
