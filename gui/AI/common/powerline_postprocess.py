from __future__ import annotations

import math
from typing import Iterable

import numpy as np
from scipy.spatial import cKDTree

from .power_phase1_state import build_power_phase1_state
from .power_phase2_candidates import build_power_phase2_candidates
from .power_phase3_tracks import build_power_phase3_tracks
from .power_phase4_protection import (
    apply_power_phase4_protection,
    build_power_phase4_protection,
)
from .power_phase5_supports import build_power_phase5_supports
from .power_phase6_recovery import build_power_phase6_recovery
from .power_phase7_validation import build_power_phase7_validation
from .power_phase8_consistency import build_power_phase8_consistency
from .power_phase9_attachment_boundary import build_power_phase9_attachment_boundary
from .power_phase10_densification import build_power_phase10_densification
# PHASE10_CONDUCTOR_DENSIFICATION_V3_17

try:
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import connected_components
    HAS_SCIPY_GRAPH = True
except Exception:
    csr_matrix = None
    connected_components = None
    HAS_SCIPY_GRAPH = False

try:
    import jakteristics
    HAS_JAKTERISTICS = True
except Exception:
    jakteristics = None
    HAS_JAKTERISTICS = False


_GEOM_NAMES = [
    "eigenvalue1", "eigenvalue2", "eigenvalue3",
    "linearity", "planarity", "sphericity",
    "omnivariance", "anisotropy", "eigenentropy",
    "surface_variation", "verticality",
]


def _log(log, level: str, message: str) -> None:
    try:
        getattr(log, level)(message)
    except Exception:
        print(message, flush=True)


def _voxel_downsample(xyz: np.ndarray, voxel_size: float = 0.30):
    xyz = np.asarray(xyz, dtype=np.float64)
    if len(xyz) == 0:
        return xyz.copy(), np.empty(0, dtype=np.int32)

    shifted = xyz - np.nanmin(xyz, axis=0)
    vc = np.floor(shifted / float(voxel_size)).astype(np.int64)
    dims = vc.max(axis=0) + 1
    max_key = int(dims[0]) * int(dims[1]) * int(dims[2])
    if max_key > 2**62:
        raise ValueError(f"Voxel grid too large: {dims}")

    keys = vc[:, 0] * dims[1] * dims[2] + vc[:, 1] * dims[2] + vc[:, 2]
    _, inverse, counts = np.unique(keys, return_inverse=True, return_counts=True)
    n_vox = int(counts.size)
    centroids = np.zeros((n_vox, 3), dtype=np.float64)
    np.add.at(centroids, inverse, xyz)
    centroids /= counts[:, None]
    return centroids, inverse.astype(np.int32, copy=False)


def _compute_power_geometry(xyz: np.ndarray, log=None):
    """
    Compute only the two small-scale geometry blocks used by the power detector.
    This path is primarily for Premium, where the frozen engine does not expose
    its internal 68F feature matrix to the GUI worker.
    """
    if not HAS_JAKTERISTICS:
        _log(log, "warning", "  Power post-pass skipped: jakteristics is unavailable.")
        return None

    ds, inv = _voxel_downsample(xyz, voxel_size=0.30)
    if len(ds) < 4:
        return None

    blocks = {}
    for radius in (0.5, 1.0):
        _log(log, "info", f"    Power geometry {radius:.1f}m on {len(ds):,} voxels...")
        feat = jakteristics.compute_features(
            ds.astype(np.float64, copy=False),
            search_radius=float(radius),
            feature_names=_GEOM_NAMES,
            num_threads=-1,
        )
        feat = np.nan_to_num(feat, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)
        blocks[radius] = feat

    f05 = blocks[0.5][inv]
    f10 = blocks[1.0][inv]
    return {
        "linearity_05": f05[:, 3].astype(np.float32, copy=False),
        "planarity_05": f05[:, 4].astype(np.float32, copy=False),
        "verticality_05": f05[:, 10].astype(np.float32, copy=False),
        "linearity_10": f10[:, 3].astype(np.float32, copy=False),
        "verticality_10": f10[:, 10].astype(np.float32, copy=False),
    }


def estimate_hag_from_ground(
    xyz,
    classes,
    *,
    ground_codes: Iterable[int] = (2,),
    k: int = 3,
    max_ground_points: int = 1_500_000,
    log=None,
):
    """Estimate HAG for an already-classified cloud without touching the frozen model."""
    xyz = np.asarray(xyz, dtype=np.float64)
    cls = np.asarray(classes).reshape(-1)
    if len(xyz) != len(cls):
        raise ValueError("HAG estimate array-length mismatch")

    ground_mask = np.isin(cls, list(ground_codes))
    ground_idx = np.flatnonzero(ground_mask)

    if ground_idx.size < 20:
        # Conservative fallback: global low Z percentile.
        z0 = float(np.nanpercentile(xyz[:, 2], 5.0)) if len(xyz) else 0.0
        _log(log, "warning", "  Power HAG fallback: too few Ground points; using global Z p05.")
        return (xyz[:, 2] - z0).astype(np.float32)

    if ground_idx.size > int(max_ground_points):
        step = int(math.ceil(ground_idx.size / float(max_ground_points)))
        ground_idx = ground_idx[::step]

    ground_xy = xyz[ground_idx, :2]
    ground_z = xyz[ground_idx, 2]
    tree = cKDTree(ground_xy)
    kk = max(1, min(int(k), len(ground_idx)))
    _, nn = tree.query(xyz[:, :2], k=kk, workers=-1)
    if kk == 1:
        ground_est = ground_z[np.asarray(nn, dtype=np.int64)]
    else:
        ground_est = np.median(ground_z[np.asarray(nn, dtype=np.int64)], axis=1)
    del tree
    return (xyz[:, 2] - ground_est).astype(np.float32)


def _polyline_distance_station_xy(points_xyz, coords):
    """Fast approximate XY distance/station using a 1 m sampled CL KD-tree."""
    points_xy = np.asarray(points_xyz, dtype=np.float64)[:, :2]
    line = np.asarray(coords, dtype=np.float64)[:, :2]
    if line.ndim != 2 or len(line) < 2:
        return np.full(len(points_xy), np.inf), np.zeros(len(points_xy), dtype=np.float64)

    samples = []
    stations = []
    cumulative = 0.0
    for a, b in zip(line[:-1], line[1:]):
        d = b - a
        seg_len = float(np.linalg.norm(d))
        if seg_len <= 1e-9:
            continue
        n_steps = max(1, int(math.ceil(seg_len / 1.0)))
        t = np.linspace(0.0, 1.0, n_steps + 1, endpoint=True)
        pts = a[None, :] + t[:, None] * d[None, :]
        samples.append(pts)
        stations.append(cumulative + t * seg_len)
        cumulative += seg_len

    if not samples:
        return np.full(len(points_xy), np.inf), np.zeros(len(points_xy), dtype=np.float64)

    sample_xy = np.vstack(samples)
    sample_station = np.concatenate(stations)
    tree = cKDTree(sample_xy)
    dist, nn = tree.query(points_xy, k=1, workers=-1)
    del tree
    return np.asarray(dist, dtype=np.float64), sample_station[np.asarray(nn, dtype=np.int64)]


def _long_station_run_mask(station, *, bin_size=2.0, min_run_m=10.0, max_gap_bins=3):
    station = np.asarray(station, dtype=np.float64)
    if station.size == 0:
        return np.zeros(0, dtype=bool)
    s0 = float(np.nanmin(station))
    bins = np.floor((station - s0) / float(bin_size)).astype(np.int64)
    occupied = np.unique(bins)
    if occupied.size == 0:
        return np.zeros(len(station), dtype=bool)

    keep_bins = set()
    run = [int(occupied[0])]
    for b in occupied[1:]:
        b = int(b)
        if b - run[-1] <= int(max_gap_bins) + 1:
            run.append(b)
        else:
            if (run[-1] - run[0] + 1) * float(bin_size) >= float(min_run_m):
                keep_bins.update(run)
            run = [b]
    if (run[-1] - run[0] + 1) * float(bin_size) >= float(min_run_m):
        keep_bins.update(run)

    if not keep_bins:
        return np.zeros(len(station), dtype=bool)
    return np.isin(bins, np.fromiter(keep_bins, dtype=np.int64))



def _wire_track_continuity_mask(
    xyz,
    candidate_idx,
    station_all,
    cl_dist_all,
    *,
    lin05=None,
    lin10=None,
    plan05=None,
    config=None,
):
    """V3.5 vegetation-safe conductor continuity filter.

    The older CL guard only required *some* points to occupy a long sequence of
    station bins. A hedge/tree row parallel to the corridor could therefore
    bridge those bins and survive as Wire.  V3.5 instead builds coherent 3-D
    conductor tracks: successive points must remain close in station, lateral
    offset and elevation. Only components with real longitudinal span, thin
    local Z thickness and strong line geometry survive.

    This is intentionally a final filter on already line-like candidates; it
    does not create new wire points and therefore cannot broaden classification.
    """
    cfg = dict(config or {})
    idx = np.asarray(candidate_idx, dtype=np.int64)
    if idx.size == 0:
        return np.zeros(0, dtype=bool)
    if idx.size < int(cfg.get("wire_track_min_points", 18)):
        return np.zeros(idx.size, dtype=bool)

    xyz = np.asarray(xyz, dtype=np.float64)
    st = np.asarray(station_all, dtype=np.float64)[idx]
    lat = np.asarray(cl_dist_all, dtype=np.float64)[idx]
    z = xyz[idx, 2]
    good = np.isfinite(st) & np.isfinite(lat) & np.isfinite(z)
    if not np.any(good):
        return np.zeros(idx.size, dtype=bool)

    # Candidate-pair search in scaled coordinates, followed by exact axis-wise
    # limits.  Absolute CL distance is sufficient here because Z continuity
    # prevents opposite-side vegetation from becoming one conductor track.
    ds_max = float(cfg.get("wire_track_station_step", 3.2))
    dl_max = float(cfg.get("wire_track_lateral_step", 0.90))
    dz_max = float(cfg.get("wire_track_z_step", 0.85))
    span_min = float(cfg.get("wire_track_min_span", 12.0))
    min_pts = int(cfg.get("wire_track_min_points", 18))
    max_local_z_iqr = float(cfg.get("wire_track_max_local_z_iqr", 0.32))
    min_bin_coverage = float(cfg.get("wire_track_min_bin_coverage", 0.42))
    min_med_linearity = float(cfg.get("wire_track_min_median_linearity", 0.66))
    max_med_planarity = float(cfg.get("wire_track_max_median_planarity", 0.30))

    valid_pos = np.flatnonzero(good)
    scaled = np.column_stack((
        st[valid_pos] / max(ds_max, 1e-6),
        lat[valid_pos] / max(dl_max, 1e-6),
        z[valid_pos] / max(dz_max, 1e-6),
    ))
    tree = cKDTree(scaled)
    pairs = tree.query_pairs(1.75, output_type="ndarray")
    del tree

    m = len(valid_pos)
    parent = np.arange(m, dtype=np.int32)
    rank = np.zeros(m, dtype=np.uint8)

    def find(a):
        a = int(a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = int(parent[a])
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rank[ra] < rank[rb]:
            parent[ra] = rb
        elif rank[ra] > rank[rb]:
            parent[rb] = ra
        else:
            parent[rb] = ra
            rank[ra] += 1

    if pairs.size:
        a = pairs[:, 0]
        b = pairs[:, 1]
        pa = valid_pos[a]
        pb = valid_pos[b]
        exact = (
            (np.abs(st[pa] - st[pb]) <= ds_max)
            & (np.abs(lat[pa] - lat[pb]) <= dl_max)
            & (np.abs(z[pa] - z[pb]) <= dz_max)
        )
        for aa, bb in pairs[exact]:
            union(int(aa), int(bb))

    roots = np.asarray([find(i) for i in range(m)], dtype=np.int32)
    keep = np.zeros(idx.size, dtype=bool)
    l05 = None if lin05 is None else np.asarray(lin05, dtype=np.float32)[idx]
    l10 = None if lin10 is None else np.asarray(lin10, dtype=np.float32)[idx]
    p05 = None if plan05 is None else np.asarray(plan05, dtype=np.float32)[idx]

    for root in np.unique(roots):
        locv = np.flatnonzero(roots == root)
        loc = valid_pos[locv]
        if loc.size < min_pts:
            continue
        svals = st[loc]
        span = float(np.percentile(svals, 98) - np.percentile(svals, 2))
        if span < span_min:
            continue

        # Require sustained occupancy along the conductor, not a few isolated
        # branch tips spread over a long corridor.
        bin_size = float(cfg.get("wire_track_quality_bin", 2.0))
        s0 = float(np.min(svals))
        bins = np.floor((svals - s0) / max(bin_size, 1e-6)).astype(np.int64)
        ub = np.unique(bins)
        total_bins = max(1, int(np.floor(span / max(bin_size, 1e-6))) + 1)
        coverage = float(len(ub)) / float(total_bins)
        if coverage < min_bin_coverage:
            continue

        # A conductor is locally very thin in Z.  Tree/hedge returns are
        # volumetric even if individual branches are locally linear.
        local_iqr = []
        for b in ub:
            q = z[loc[bins == b]]
            if q.size >= 3:
                local_iqr.append(float(np.percentile(q, 75) - np.percentile(q, 25)))
        if local_iqr and float(np.median(local_iqr)) > max_local_z_iqr:
            continue

        if l05 is not None and l10 is not None:
            med_lin = float(np.median(np.maximum(l05[loc], l10[loc])))
            if med_lin < min_med_linearity:
                continue
        if p05 is not None:
            med_plan = float(np.median(p05[loc]))
            if med_plan > max_med_planarity:
                continue

        keep[loc] = True

    return keep


def _wire_profile_continuity_mask(
    xyz,
    candidate_idx,
    station_all,
    cl_dist_all,
    *,
    lin05=None,
    lin10=None,
    plan05=None,
    config=None,
):
    """V3.6 sparse-LiDAR conductor profile tracker.

    Real conductors can be too sparse for point-to-point graph connectivity.
    This tracker works on station-bin Z clusters instead. Accepted tracks must
    extend longitudinally and follow a smooth linear/quadratic sag profile.
    Volumetric tree/hedge returns normally fail the profile residual test.
    """
    cfg = dict(config or {})
    idx = np.asarray(candidate_idx, dtype=np.int64)
    keep = np.zeros(len(idx), dtype=bool)
    if idx.size == 0:
        return keep, {"tracks": 0, "accepted_tracks": 0, "kept": 0}

    xyz = np.asarray(xyz, dtype=np.float64)
    st = np.asarray(station_all, dtype=np.float64)[idx]
    lat = np.asarray(cl_dist_all, dtype=np.float64)[idx]
    z = xyz[idx, 2]
    good = np.isfinite(st) & np.isfinite(lat) & np.isfinite(z)
    if np.count_nonzero(good) < 8:
        return keep, {"tracks": 0, "accepted_tracks": 0, "kept": 0}

    bin_m = float(cfg.get("wire_profile_bin_m", 2.0))
    z_gap = float(cfg.get("wire_profile_z_cluster_gap", 0.70))
    max_bin_gap = int(cfg.get("wire_profile_max_bin_gap", 2))
    max_dz_per_bin = float(cfg.get("wire_profile_max_dz_per_bin", 1.20))
    max_dlat = float(cfg.get("wire_profile_max_dlat", 1.35))
    min_span = float(cfg.get("wire_profile_min_span_m", 10.0))
    min_bins = int(cfg.get("wire_profile_min_bins", 5))
    min_points = int(cfg.get("wire_profile_min_points", 12))
    max_z_resid = float(cfg.get("wire_profile_max_z_residual", 0.55))
    max_lat_resid = float(cfg.get("wire_profile_max_lat_residual", 0.95))
    min_linearity = float(cfg.get("wire_profile_min_linearity", 0.62))
    max_planarity = float(cfg.get("wire_profile_max_planarity", 0.34))

    pos = np.flatnonzero(good)
    s0 = float(np.nanmin(st[pos]))
    bnum = np.floor((st[pos] - s0) / max(bin_m, 1e-6)).astype(np.int64)

    clusters = []
    for b in np.unique(bnum):
        lp = pos[bnum == b]
        if lp.size == 0:
            continue
        order = lp[np.argsort(z[lp])]
        split = np.flatnonzero(np.diff(z[order]) > z_gap) + 1
        groups = np.split(order, split)
        for g in groups:
            if g.size == 0:
                continue
            clusters.append({
                "bin": int(b),
                "station": float(np.median(st[g])),
                "z": float(np.median(z[g])),
                "lat": float(np.median(lat[g])),
                "loc": g,
            })

    if not clusters:
        return keep, {"tracks": 0, "accepted_tracks": 0, "kept": 0}

    clusters.sort(key=lambda c: (c["bin"], c["z"], c["lat"]))
    tracks = []
    for ci, c in enumerate(clusters):
        best_t = None
        best_cost = float("inf")
        for ti, tr in enumerate(tracks):
            last = clusters[tr[-1]]
            db = c["bin"] - last["bin"]
            if db <= 0 or db > max_bin_gap + 1:
                continue
            dz_lim = max_dz_per_bin * max(db, 1)
            dz = abs(c["z"] - last["z"])
            dl = abs(c["lat"] - last["lat"])
            if dz > dz_lim or dl > max_dlat:
                continue
            cost = dz / max(dz_lim, 1e-6) + dl / max(max_dlat, 1e-6)
            if cost < best_cost:
                best_cost = cost
                best_t = ti
        if best_t is None:
            tracks.append([ci])
        else:
            tracks[best_t].append(ci)

    l05 = None if lin05 is None else np.asarray(lin05, dtype=np.float32)[idx]
    l10 = None if lin10 is None else np.asarray(lin10, dtype=np.float32)[idx]
    p05 = None if plan05 is None else np.asarray(plan05, dtype=np.float32)[idx]
    accepted = 0

    for tr in tracks:
        if len(tr) < min_bins:
            continue
        cs = [clusters[j] for j in tr]
        stations = np.asarray([c["station"] for c in cs], dtype=np.float64)
        zs = np.asarray([c["z"] for c in cs], dtype=np.float64)
        lats = np.asarray([c["lat"] for c in cs], dtype=np.float64)
        loc = np.unique(np.concatenate([c["loc"] for c in cs]))
        if loc.size < min_points:
            continue
        span = float(np.percentile(stations, 98) - np.percentile(stations, 2))
        if span < min_span:
            continue

        x = stations - float(np.mean(stations))
        deg = 2 if len(stations) >= 5 and np.ptp(stations) >= 8.0 else 1
        try:
            coef = np.polyfit(x, zs, deg)
            zfit = np.polyval(coef, x)
            zres = np.abs(zs - zfit)
            zres90 = float(np.percentile(zres, 90))
        except Exception:
            zres90 = float("inf")
        if zres90 > max_z_resid:
            continue

        # Conductors keep a stable offset from CL. Absolute distance is used by
        # the existing CL projection, so allow a generous residual for curves.
        lat_med = float(np.median(lats))
        lat_res90 = float(np.percentile(np.abs(lats - lat_med), 90))
        if lat_res90 > max_lat_resid:
            continue

        if l05 is not None and l10 is not None:
            med_lin = float(np.median(np.maximum(l05[loc], l10[loc])))
            if med_lin < min_linearity:
                continue
        if p05 is not None:
            med_plan = float(np.median(p05[loc]))
            if med_plan > max_planarity:
                continue

        keep[loc] = True
        accepted += 1

    return keep, {"tracks": int(len(tracks)), "accepted_tracks": int(accepted), "kept": int(np.count_nonzero(keep))}

def _active_mask(n: int, active_indices=None):
    if active_indices is None:
        return np.ones(n, dtype=bool)
    idx = np.asarray(active_indices, dtype=np.int64).ravel()
    idx = idx[(idx >= 0) & (idx < n)]
    mask = np.zeros(n, dtype=bool)
    if idx.size:
        mask[np.unique(idx)] = True
    return mask


def _connected_components_radius(xy, radius: float, max_edges: int = 2_000_000):
    n = len(xy)
    if n == 0:
        return np.empty(0, dtype=np.int32), 0
    if n == 1 or not HAS_SCIPY_GRAPH:
        return np.zeros(n, dtype=np.int32), 1

    tree = cKDTree(np.asarray(xy, dtype=np.float64))
    pairs = tree.query_pairs(float(radius), output_type="ndarray")
    del tree
    if pairs.size == 0:
        return np.arange(n, dtype=np.int32), n
    if len(pairs) > max_edges:
        # Too dense to be a thin wire/pole candidate set; treat as one noisy
        # component and let later geometry/span rules reject it.
        return np.zeros(n, dtype=np.int32), 1

    row = np.concatenate([pairs[:, 0], pairs[:, 1]])
    col = np.concatenate([pairs[:, 1], pairs[:, 0]])
    data = np.ones(len(row), dtype=np.uint8)
    graph = csr_matrix((data, (row, col)), shape=(n, n))
    n_comp, labels = connected_components(graph, directed=False, return_labels=True)
    return labels.astype(np.int32, copy=False), int(n_comp)



def _grid_cluster_labels_xy(xy, cell_size=2.0):
    """Memory-safe 2D grid connected components for pole/pylon structures."""
    xy = np.asarray(xy, dtype=np.float64)
    n = len(xy)
    if n == 0:
        return np.empty(0, dtype=np.int32), 0

    cell_size = max(float(cell_size), 0.25)
    cells = np.floor(xy / cell_size).astype(np.int64)
    unique_cells, inverse = np.unique(cells, axis=0, return_inverse=True)
    n_cells = len(unique_cells)
    parent = np.arange(n_cells, dtype=np.int32)

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra = find(a)
        rb = find(b)
        if ra != rb:
            parent[rb] = ra

    lookup = {(int(c[0]), int(c[1])): i for i, c in enumerate(unique_cells)}
    for i, c in enumerate(unique_cells):
        cx, cy = int(c[0]), int(c[1])
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                j = lookup.get((cx + dx, cy + dy))
                if j is not None:
                    union(i, j)

    roots = np.array([find(i) for i in range(n_cells)], dtype=np.int32)
    _, compact = np.unique(roots, return_inverse=True)
    labels = compact[inverse].astype(np.int32)
    return labels, int(compact.max() + 1) if len(compact) else 0


def _classify_structural_core_metrics(
    *,
    height_span: float,
    xy_span: float,
    occupied_bins: int,
    vert_support: float,
    linear_support: float,
    top_link_count: int,
    top_link_fraction: float,
    anchor_cells: int,
    core_cells: int,
    point_count: int,
    base_reach: bool,
    top_reach: bool,
    aspect: float,
    station_span: float,
    config=None,
):
    """Classify one compact structural support core.

    V3.3 keeps the conservative V3.2 acceptance path, but adds a
    high-evidence lattice-pylon rescue for real towers that are slightly
    wider in CL station/core-cell count.  The rescue deliberately requires
    very strong vertical structure and a meaningful fraction of top points
    physically linked to detected conductors, which vegetation normally
    cannot satisfy.
    """
    cfg = dict(config or {})

    height_span = float(height_span)
    xy_span = float(xy_span)
    occupied_bins = int(occupied_bins)
    vert_support = float(vert_support)
    linear_support = float(linear_support)
    top_link_count = int(top_link_count)
    top_link_fraction = float(top_link_fraction)
    anchor_cells = int(anchor_cells)
    core_cells = int(core_cells)
    point_count = int(point_count)
    aspect = float(aspect)
    station_span = float(station_span)

    height_ok = height_span <= float(cfg.get("pole_max_height_span", 80.0))
    standard_compact = (
        height_ok
        and xy_span <= float(cfg.get("pylon_max_xy", 12.0))
        and core_cells <= int(cfg.get("pylon_max_core_cells", 36))
        and point_count <= int(cfg.get("pylon_max_core_points", 8500))
    )

    narrow = (
        standard_compact
        and height_span >= float(cfg.get("pole_min_height", 4.0))
        and xy_span <= float(cfg.get("pole_max_xy", 3.8))
        and occupied_bins >= int(cfg.get("pole_min_bins", 4))
        and bool(base_reach) and bool(top_reach)
        and top_link_count >= 1
        and (
            vert_support >= 0.26
            or (vert_support >= 0.18 and linear_support >= 0.30)
        )
        and core_cells <= int(cfg.get("pole_max_core_cells", 10))
        and point_count <= int(cfg.get("pole_max_core_points", 4500))
    )

    lattice_standard = (
        standard_compact
        and height_span >= float(cfg.get("pylon_min_height", 6.0))
        and 2.0 < xy_span <= float(cfg.get("pylon_max_xy", 12.0))
        and occupied_bins >= int(cfg.get("pylon_min_bins", 5))
        and bool(base_reach) and bool(top_reach)
        and top_link_count >= int(cfg.get("pylon_min_top_links", 2))
        and anchor_cells >= int(cfg.get("pylon_min_anchor_cells", 2))
        and vert_support >= float(cfg.get("pylon_min_vert_fraction", 0.22))
        and aspect >= float(cfg.get("pylon_min_aspect", 0.55))
        and station_span <= float(cfg.get("pylon_max_station_span", 7.0))
    )

    # V3.3 REAL-TOWER RESCUE
    # The GRUPPIGNANO real towers produced cores around:
    #   6.02m high / 7.57m wide / 35 cells / station 7.99m / vert 0.92
    #  10.32m high / 7.39m wide / 48 cells / station 7.97m / vert 0.93
    # V3.2 rejected both only because the 7.0m station / 36-cell limits were
    # too strict.  We do NOT simply widen those limits globally.  A rescue
    # core must show exceptionally strong verticality plus many conductor
    # attachments in its upper structure.
    rescue_compact = (
        height_ok
        and height_span >= float(cfg.get("pylon_rescue_min_height", 5.5))
        and 2.0 < xy_span <= float(cfg.get("pylon_rescue_max_xy", 9.5))
        and occupied_bins >= int(cfg.get("pylon_rescue_min_bins", 5))
        and core_cells <= int(cfg.get("pylon_rescue_max_core_cells", 64))
        and point_count <= int(cfg.get("pylon_rescue_max_core_points", 9000))
        and station_span <= float(cfg.get("pylon_rescue_max_station_span", 9.5))
    )
    rescue_evidence = (
        bool(base_reach) and bool(top_reach)
        and anchor_cells >= int(cfg.get("pylon_rescue_min_anchor_cells", 2))
        and vert_support >= float(cfg.get("pylon_rescue_min_vert_fraction", 0.70))
        and linear_support >= float(cfg.get("pylon_rescue_min_linear_fraction", 0.25))
        and aspect >= float(cfg.get("pylon_rescue_min_aspect", 0.65))
        and top_link_count >= int(cfg.get("pylon_rescue_min_top_links", 20))
        and top_link_fraction >= float(cfg.get("pylon_rescue_min_top_link_fraction", 0.08))
    )
    lattice_rescue = rescue_compact and rescue_evidence

    if narrow:
        return "POLE", "standard_narrow"
    if lattice_standard:
        return "PYLON", "standard_lattice"
    if lattice_rescue:
        return "PYLON", "strong_evidence_rescue"
    return "REJECT", "none"


def _extract_structural_pole_cores(
    xyz,
    hag,
    pole_support_idx,
    wire_support_idx,
    *,
    vert05,
    vert10,
    lin05,
    lin10,
    building_like,
    cl_station=None,
    cl_active=False,
    config=None,
    log=None,
):
    """V3.2 pole/pylon structural-core extractor.

    The old algorithm accepted/rejected one broad connected XY component and,
    when accepted, wrote the *entire* component as Pole. That can merge a real
    tower with vegetation/noise or turn a vegetation patch into a pylon.

    V3.2 instead:
      1) finds small vertical column cells;
      2) requires a real top-of-structure wire attachment;
      3) groups only nearby attachment columns (same CL station when available);
      4) validates a compact pole/tower footprint; and
      5) returns only the structural core points, never the whole broad cluster.
    """
    cfg = dict(config or {})
    pole_support_idx = np.asarray(pole_support_idx, dtype=np.int64)
    wire_support_idx = np.asarray(wire_support_idx, dtype=np.int64)
    if pole_support_idx.size < 4 or wire_support_idx.size < 2:
        return np.empty(0, dtype=np.int64), []

    vstrength = np.maximum(np.asarray(vert05), np.asarray(vert10))
    lstrength = np.maximum(np.asarray(lin05), np.asarray(lin10))

    # Per-point 3-D wire attachment evidence. A single arbitrary point no
    # longer makes a whole 20-80 m component "wire_link=True".
    wt = cKDTree(xyz[wire_support_idx, :2])
    try:
        dxy, nn = wt.query(xyz[pole_support_idx, :2], k=1, workers=-1)
    finally:
        del wt
    nearest_wire_idx = wire_support_idx[np.asarray(nn, dtype=np.int64)]
    dz = np.abs(xyz[pole_support_idx, 2] - xyz[nearest_wire_idx, 2])
    point_wire_link = (
        (np.asarray(dxy) <= float(cfg.get("pole_top_wire_xy", 5.0)))
        & (dz <= float(cfg.get("pole_top_wire_dz", 4.5)))
    )

    # Build compact 1 m vertical-column cells. This deliberately prevents a
    # vegetation carpet from bridging two towers into one giant component.
    cell_size = float(cfg.get("pole_core_cell_size", 1.0))
    cell_size = min(max(cell_size, 0.6), 1.25)
    xy = xyz[pole_support_idx, :2]
    cells = np.floor(xy / cell_size).astype(np.int64)
    unique_cells, inverse = np.unique(cells, axis=0, return_inverse=True)
    n_cells = len(unique_cells)
    if n_cells == 0:
        return np.empty(0, dtype=np.int64), []

    order = np.argsort(inverse, kind="stable")
    counts = np.bincount(inverse, minlength=n_cells)
    starts = np.cumsum(np.r_[0, counts[:-1]])

    cell_is_core = np.zeros(n_cells, dtype=bool)
    cell_is_anchor = np.zeros(n_cells, dtype=bool)
    cell_xy = np.zeros((n_cells, 2), dtype=np.float64)
    cell_station = np.full(n_cells, np.nan, dtype=np.float64)
    cell_point_positions = [None] * n_cells

    for cid in range(n_cells):
        cnt = int(counts[cid])
        if cnt < int(cfg.get("pole_cell_min_points", 4)):
            continue
        loc = order[starts[cid]:starts[cid] + cnt]
        gi = pole_support_idx[loc]
        pts = xyz[gi]
        cell_point_positions[cid] = loc
        cell_xy[cid] = np.median(pts[:, :2], axis=0)

        hspan = float(max(np.ptp(pts[:, 2]), np.ptp(hag[gi])))
        if hspan < float(cfg.get("pole_cell_min_height", 2.8)):
            continue
        z0 = float(np.min(pts[:, 2]))
        bins = np.floor((pts[:, 2] - z0) / float(cfg.get("pole_cell_z_bin", 1.0))).astype(np.int32)
        nbins = int(len(np.unique(bins)))
        if nbins < int(cfg.get("pole_cell_min_bins", 3)):
            continue

        vfrac = float(np.mean(vstrength[gi] >= float(cfg.get("pole_cell_vert", 0.24))))
        lfrac = float(np.mean(lstrength[gi] >= float(cfg.get("pole_cell_lin", 0.45))))
        bfrac = float(np.mean(building_like[gi]))
        structural = (
            vfrac >= float(cfg.get("pole_cell_vert_fraction", 0.22))
            or (vfrac >= 0.12 and lfrac >= float(cfg.get("pole_cell_lin_fraction", 0.28)))
            or bfrac >= float(cfg.get("pole_cell_build_fraction", 0.20))
        )
        if not structural:
            continue

        cell_is_core[cid] = True
        if cl_active and cl_station is not None:
            try:
                cell_station[cid] = float(np.median(np.asarray(cl_station)[gi]))
            except Exception:
                pass

        # Attachment must occur in the upper part of this vertical column.
        z_cut = float(np.percentile(pts[:, 2], 65.0))
        top = pts[:, 2] >= z_cut
        top_links = int(np.count_nonzero(point_wire_link[loc] & top))
        if top_links >= int(cfg.get("pole_anchor_min_top_links", 1)):
            cell_is_anchor[cid] = True

    core_cells = np.flatnonzero(cell_is_core)
    anchor_cells = np.flatnonzero(cell_is_anchor)
    if core_cells.size == 0 or anchor_cells.size == 0:
        return np.empty(0, dtype=np.int64), []

    # Group attachment columns into one physical support. With a CL prior,
    # towers are expected to occupy a narrow station interval even when their
    # crossarms/legs are wide perpendicular to the line.
    axy = cell_xy[anchor_cells]
    parent = np.arange(len(anchor_cells), dtype=np.int32)

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    atree = cKDTree(axy)
    try:
        pairs = atree.query_pairs(float(cfg.get("pole_anchor_group_radius", 9.0)), output_type="ndarray")
    finally:
        del atree
    for a, b in np.asarray(pairs, dtype=np.int64).reshape(-1, 2) if np.asarray(pairs).size else []:
        ca = anchor_cells[int(a)]
        cb = anchor_cells[int(b)]
        if cl_active and np.isfinite(cell_station[ca]) and np.isfinite(cell_station[cb]):
            if abs(cell_station[ca] - cell_station[cb]) > float(cfg.get("pole_anchor_station_gap", 5.0)):
                continue
        union(int(a), int(b))

    roots = np.array([find(i) for i in range(len(anchor_cells))], dtype=np.int32)
    _, group_labels = np.unique(roots, return_inverse=True)
    diagnostics = []
    accepted_cores = []

    # Global wire-link lookup for candidate metrics.
    wire_link_global = np.zeros(len(xyz), dtype=bool)
    wire_link_global[pole_support_idx] = point_wire_link
    nearest_wire_z_global = np.full(len(xyz), np.nan, dtype=np.float32)
    nearest_wire_z_global[pole_support_idx] = xyz[nearest_wire_idx, 2].astype(np.float32, copy=False)

    for gid in range(int(group_labels.max()) + 1 if group_labels.size else 0):
        group_anchor_cells = anchor_cells[group_labels == gid]
        if group_anchor_cells.size == 0:
            continue
        group_anchor_xy = cell_xy[group_anchor_cells]
        center = np.median(group_anchor_xy, axis=0)
        group_station = np.nan
        if cl_active:
            vals = cell_station[group_anchor_cells]
            vals = vals[np.isfinite(vals)]
            if vals.size:
                group_station = float(np.median(vals))

        # Grow only through pre-qualified vertical cells near the attachment,
        # not through every occupied XY cell.
        cxy = cell_xy[core_cells]
        dist_to_center = np.linalg.norm(cxy - center[None, :], axis=1)
        keep = dist_to_center <= float(cfg.get("pole_core_radius", 6.5))
        if cl_active and np.isfinite(group_station):
            st = cell_station[core_cells]
            st_ok = (~np.isfinite(st)) | (np.abs(st - group_station) <= float(cfg.get("pole_core_station_halfwidth", 4.5)))
            keep &= st_ok
        selected_cells = core_cells[keep]
        if selected_cells.size == 0:
            continue

        point_pos = [cell_point_positions[c] for c in selected_cells if cell_point_positions[c] is not None]
        if not point_pos:
            continue
        loc = np.unique(np.concatenate(point_pos)).astype(np.int64)
        gi = pole_support_idx[loc]

        # Structural-core only. This is the vegetation protection: even after a
        # tower is accepted, nearby foliage is not written as Pole/Pylon.
        strong = (
            (vstrength[gi] >= float(cfg.get("pole_core_point_vert", 0.20)))
            | ((vstrength[gi] >= 0.10) & (lstrength[gi] >= float(cfg.get("pole_core_point_lin", 0.45))))
            | building_like[gi]
        )
        # A support structure can extend below the conductor, but isolated sky
        # points far above the nearest conductor must never be absorbed into a
        # pole core merely because they share an XY cell.
        nwz = nearest_wire_z_global[gi]
        strong &= (~np.isfinite(nwz)) | (
            xyz[gi, 2] <= nwz + float(cfg.get("pole_core_above_wire_margin", 6.0))
        )
        gi = gi[strong]
        if gi.size < int(cfg.get("pole_core_min_points", 12)):
            continue

        pts = xyz[gi]
        qx = np.percentile(pts[:, 0], [5.0, 95.0])
        qy = np.percentile(pts[:, 1], [5.0, 95.0])
        qz = np.percentile(pts[:, 2], [2.0, 98.0])
        qh = np.percentile(hag[gi], [10.0, 90.0])
        xy_span = float(max(qx[1] - qx[0], qy[1] - qy[0]))
        height_span = float(max(qz[1] - qz[0], qh[1] - qh[0]))
        z0 = float(np.min(pts[:, 2]))
        zbins = np.floor((pts[:, 2] - z0) / 1.0).astype(np.int32)
        occupied_bins = int(len(np.unique(zbins)))
        vert_support = float(np.mean(vstrength[gi] >= 0.24))
        linear_support = float(np.mean(lstrength[gi] >= 0.45))
        building_fraction = float(np.mean(building_like[gi]))
        top_link_count = int(np.count_nonzero(wire_link_global[gi] & (pts[:, 2] >= np.percentile(pts[:, 2], 65.0))))
        aspect = height_span / max(xy_span, 0.25)
        station_span = 0.0
        if cl_active and cl_station is not None:
            try:
                sv = np.asarray(cl_station)[gi]
                station_span = float(np.percentile(sv, 95.0) - np.percentile(sv, 5.0))
            except Exception:
                station_span = 0.0

        base_reach = float(qh[0]) <= float(cfg.get("pole_base_hag_max", 4.5))
        top_reach = float(qh[1]) >= float(cfg.get("pole_top_hag_min", 5.0))
        top_link_fraction = float(top_link_count) / float(max(1, gi.size))

        kind, accept_reason = _classify_structural_core_metrics(
            height_span=height_span,
            xy_span=xy_span,
            occupied_bins=occupied_bins,
            vert_support=vert_support,
            linear_support=linear_support,
            top_link_count=top_link_count,
            top_link_fraction=top_link_fraction,
            anchor_cells=int(group_anchor_cells.size),
            core_cells=int(selected_cells.size),
            point_count=int(gi.size),
            base_reach=base_reach,
            top_reach=top_reach,
            aspect=aspect,
            station_span=station_span,
            config=cfg,
        )
        accepted = kind != "REJECT"
        diagnostics.append({
            "group": int(gid),
            "kind": kind,
            "accepted": bool(accepted),
            "anchors": int(group_anchor_cells.size),
            "core_cells": int(selected_cells.size),
            "points": int(gi.size),
            "height": height_span,
            "xy": xy_span,
            "bins": occupied_bins,
            "vert": vert_support,
            "linear": linear_support,
            "building": building_fraction,
            "top_links": top_link_count,
            "top_link_fraction": top_link_fraction,
            "accept_reason": accept_reason,
            "low_hag_p10": float(qh[0]),
            "high_hag_p90": float(qh[1]),
            "station_span": station_span,
            "aspect": aspect,
        })
        if accepted:
            accepted_cores.append(gi)

    if not accepted_cores:
        return np.empty(0, dtype=np.int64), diagnostics
    return np.unique(np.concatenate(accepted_cores)).astype(np.int64), diagnostics


def _find_conductor_attachment_stations(
    xyz,
    wire_support_idx,
    cl_station,
    *,
    config=None,
):
    """Find likely physical support stations from conductor sag geometry.

    This V3.4 helper uses the already-detected conductor bundle as the anchor.
    A real pole/pylon is normally located where several sagging conductors rise
    to a local support high point.  We compare each local conductor elevation
    against a linear interpolation of the wire profile on both sides, which is
    substantially safer than widening generic pole geometry thresholds.

    Returns a list of dictionaries with station, anchor XY, wire Z/HAG-like
    profile information, local wire indices and a prominence score.
    """
    cfg = dict(config or {})
    wire_support_idx = np.asarray(wire_support_idx, dtype=np.int64)
    if wire_support_idx.size < 20 or cl_station is None:
        return []

    station_all = np.asarray(cl_station, dtype=np.float64)
    st = station_all[wire_support_idx]
    good = np.isfinite(st) & np.isfinite(xyz[wire_support_idx, 2])
    if np.count_nonzero(good) < 20:
        return []
    widx = wire_support_idx[good]
    st = st[good]
    z = np.asarray(xyz[widx, 2], dtype=np.float64)

    bin_size = float(cfg.get('pole_anchor_profile_bin', 1.0))
    bin_size = min(max(bin_size, 0.5), 2.0)
    s0 = float(np.min(st))
    bins = np.floor((st - s0) / bin_size).astype(np.int64)
    max_bin = int(np.max(bins)) if bins.size else -1
    if max_bin < 2:
        return []

    nbin = max_bin + 1
    count = np.bincount(bins, minlength=nbin)
    prof_z = np.full(nbin, np.nan, dtype=np.float64)
    prof_s = s0 + (np.arange(nbin, dtype=np.float64) + 0.5) * bin_size
    min_bin_pts = int(cfg.get('pole_anchor_profile_min_wire_points', 6))

    order = np.argsort(bins, kind='stable')
    starts = np.cumsum(np.r_[0, count[:-1]])
    for b in np.flatnonzero(count >= min_bin_pts):
        loc = order[starts[b]:starts[b] + count[b]]
        # Median is robust when several conductors run at slightly different Z.
        prof_z[b] = float(np.median(z[loc]))

    valid = np.isfinite(prof_z)
    if np.count_nonzero(valid) < 8:
        return []

    # Small median smoothing suppresses individual noisy wire points without
    # flattening the real sag peak around a support.
    smooth = prof_z.copy()
    smooth_half = int(cfg.get('pole_anchor_profile_smooth_bins', 1))
    for i in np.flatnonzero(valid):
        a=max(0, i-smooth_half); b=min(nbin, i+smooth_half+1)
        v=prof_z[a:b]; v=v[np.isfinite(v)]
        if v.size:
            smooth[i]=float(np.median(v))

    side_near_m = float(cfg.get('pole_anchor_side_near_m', 6.0))
    side_far_m = float(cfg.get('pole_anchor_side_far_m', 18.0))
    min_prom = float(cfg.get('pole_anchor_min_prominence', 0.18))
    min_local_wire = int(cfg.get('pole_anchor_min_local_wire_points', 12))
    local_half = float(cfg.get('pole_anchor_wire_station_halfwidth', 1.75))

    candidates=[]
    valid_bins=np.flatnonzero(np.isfinite(smooth))
    for i in valid_bins:
        si=prof_s[i]
        left=(prof_s <= si-side_near_m) & (prof_s >= si-side_far_m) & np.isfinite(smooth)
        right=(prof_s >= si+side_near_m) & (prof_s <= si+side_far_m) & np.isfinite(smooth)
        if not np.any(left) or not np.any(right):
            # Interior support detection is intentionally conservative.  The
            # standard V3.3 core detector still handles edge supports.
            continue
        sl=prof_s[left]; zl=smooth[left]
        sr=prof_s[right]; zr=smooth[right]
        # Use the closest-side medians then interpolate for local corridor slope.
        lmask = sl >= np.max(sl) - max(2.0, 0.25*(side_far_m-side_near_m))
        rmask = sr <= np.min(sr) + max(2.0, 0.25*(side_far_m-side_near_m))
        ls=float(np.median(sl[lmask])); lz=float(np.median(zl[lmask]))
        rs=float(np.median(sr[rmask])); rz=float(np.median(zr[rmask]))
        if rs <= ls + 1e-6:
            continue
        t=(si-ls)/(rs-ls)
        expected=lz + t*(rz-lz)
        prominence=float(smooth[i]-expected)
        if prominence < min_prom:
            continue
        # A physical support should be a local conductor high-point, not just
        # part of a gently curved/sloped wire profile.
        peak_half=max(2, int(round(float(cfg.get('pole_anchor_peak_halfwidth_m', 3.0))/bin_size)))
        pa=max(0,i-peak_half); pb=min(nbin,i+peak_half+1)
        pv=smooth[pa:pb]; pv=pv[np.isfinite(pv)]
        if pv.size and float(smooth[i]) < float(np.max(pv)) - float(cfg.get('pole_anchor_peak_tolerance', 0.04)):
            continue

        local_mask=np.abs(st-si) <= local_half
        local_idx=widx[local_mask]
        if local_idx.size < min_local_wire:
            continue
        anchor_xy=np.median(xyz[local_idx,:2], axis=0)
        wire_z=float(np.percentile(xyz[local_idx,2], 75.0))
        candidates.append({
            'station': float(si),
            'anchor_xy': np.asarray(anchor_xy,dtype=np.float64),
            'wire_z': wire_z,
            'prominence': prominence,
            'wire_points': int(local_idx.size),
            'wire_indices': np.asarray(local_idx,dtype=np.int64),
        })

    if not candidates:
        return []

    # Non-maximum suppression in station space: one physical support should
    # produce one anchor, not several adjacent 1 m profile bins.
    sep=float(cfg.get('pole_anchor_min_station_separation', 6.0))
    candidates=sorted(candidates, key=lambda d: d['prominence'], reverse=True)
    kept=[]
    for c in candidates:
        if all(abs(c['station']-k['station']) >= sep for k in kept):
            kept.append(c)
    kept.sort(key=lambda d:d['station'])
    return kept


def _extract_attachment_driven_pole_cores(
    xyz,
    hag,
    pole_support_idx,
    wire_support_idx,
    *,
    vert05,
    vert10,
    lin05,
    lin10,
    building_like,
    cl_station=None,
    cl_active=False,
    already_accepted=None,
    config=None,
    log=None,
):
    """V3.4 conductor-anchor downward support rescue.

    V3.3 remains the primary detector.  This rescue is used only with a CL
    prior and only around conductor sag high-points.  It searches downward for
    compact vertical structural columns, so a pole that the semantic model
    called vegetation can still be recovered without globally loosening the
    vegetation guard.
    """
    cfg=dict(config or {})
    pole_support_idx=np.asarray(pole_support_idx,dtype=np.int64)
    wire_support_idx=np.asarray(wire_support_idx,dtype=np.int64)
    if (not cl_active) or cl_station is None or pole_support_idx.size < 4 or wire_support_idx.size < 20:
        return np.empty(0,dtype=np.int64), []

    anchors=_find_conductor_attachment_stations(xyz, wire_support_idx, cl_station, config=cfg)
    if not anchors:
        return np.empty(0,dtype=np.int64), []

    vstrength=np.maximum(np.asarray(vert05), np.asarray(vert10))
    lstrength=np.maximum(np.asarray(lin05), np.asarray(lin10))
    station=np.asarray(cl_station,dtype=np.float64)
    accepted_existing=np.asarray(already_accepted if already_accepted is not None else [],dtype=np.int64)
    existing_tree=cKDTree(xyz[accepted_existing,:2]) if accepted_existing.size else None

    diagnostics=[]
    rescued=[]
    search_station=float(cfg.get('pole_anchor_rescue_station_halfwidth', 4.5))
    search_radius=float(cfg.get('pole_anchor_rescue_xy_radius', 6.0))
    cell_size=float(cfg.get('pole_anchor_rescue_cell_size', 0.8))
    cell_size=min(max(cell_size,0.6),1.1)

    try:
        for aid,a in enumerate(anchors):
            axy=np.asarray(a['anchor_xy'],dtype=np.float64)
            # Skip anchors already represented by a V3.3 accepted core.
            if existing_tree is not None:
                d0,_=existing_tree.query(axy,k=1)
                if float(d0) <= float(cfg.get('pole_anchor_existing_core_radius',3.0)):
                    diagnostics.append({**a,'anchor':aid,'accepted':False,'reason':'already_covered','points':0,'cells':0})
                    continue

            st_ok=np.isfinite(station[pole_support_idx]) & (np.abs(station[pole_support_idx]-a['station']) <= search_station)
            dxy=np.linalg.norm(xyz[pole_support_idx,:2]-axy[None,:],axis=1)
            local=pole_support_idx[st_ok & (dxy <= search_radius)]
            if local.size < int(cfg.get('pole_anchor_rescue_min_candidates',12)):
                diagnostics.append({**a,'anchor':aid,'accepted':False,'reason':'too_few_candidates','points':int(local.size),'cells':0})
                continue

            # Split the local support search into sub-metre vertical XY columns.
            cells=np.floor((xyz[local,:2]-axy[None,:])/cell_size).astype(np.int64)
            uc,inv=np.unique(cells,axis=0,return_inverse=True)
            counts=np.bincount(inv,minlength=len(uc))
            order=np.argsort(inv,kind='stable')
            starts=np.cumsum(np.r_[0,counts[:-1]])
            seed_cells=[]
            seed_points=[]
            cell_meta=[]
            for cid in range(len(uc)):
                cnt=int(counts[cid])
                if cnt < int(cfg.get('pole_anchor_rescue_cell_min_points',4)):
                    continue
                locpos=order[starts[cid]:starts[cid]+cnt]
                gi=local[locpos]
                pts=xyz[gi]
                qh=np.percentile(hag[gi],[10.0,90.0])
                qz=np.percentile(pts[:,2],[5.0,95.0])
                hspan=float(max(qh[1]-qh[0],qz[1]-qz[0]))
                if hspan < float(cfg.get('pole_anchor_rescue_cell_min_height',3.0)):
                    continue
                z0=float(np.min(pts[:,2]))
                zb=np.floor((pts[:,2]-z0)/1.0).astype(np.int32)
                bins_n=int(len(np.unique(zb)))
                if bins_n < int(cfg.get('pole_anchor_rescue_cell_min_bins',4)):
                    continue
                vf=float(np.mean(vstrength[gi] >= float(cfg.get('pole_anchor_rescue_point_vert',0.24))))
                lf=float(np.mean(lstrength[gi] >= float(cfg.get('pole_anchor_rescue_point_lin',0.32))))
                bf=float(np.mean(building_like[gi]))
                # Anchor rescue is intentionally stricter than generic
                # verticality: vegetation can also look vertical at small
                # scales.  Require a meaningful linear structural fraction.
                structural=(
                    (vf >= float(cfg.get('pole_anchor_rescue_cell_vert_fraction',0.32))
                     and lf >= float(cfg.get('pole_anchor_rescue_cell_lin_fraction',0.22)))
                    or (lf >= float(cfg.get('pole_anchor_rescue_cell_strong_lin_fraction',0.42))
                        and vf >= 0.18)
                    or (bf >= 0.45 and vf >= 0.20 and lf >= 0.12)
                )
                if not structural:
                    continue
                center=np.median(pts[:,:2],axis=0)
                cdist=float(np.linalg.norm(center-axy))
                if cdist > float(cfg.get('pole_anchor_rescue_seed_radius',4.2)):
                    continue
                # A support seed must reach both downward toward terrain and
                # upward close to the local conductor attachment elevation.
                base_ok=float(qh[0]) <= float(cfg.get('pole_anchor_rescue_base_hag_max',5.0))
                top_z=float(qz[1])
                top_ok=top_z >= float(a['wire_z']) - float(cfg.get('pole_anchor_rescue_top_z_gap',4.0))
                if not (base_ok and top_ok):
                    continue
                # Direct top attachment evidence: at least a few points in the
                # upper cell must sit close to the detected conductor bundle.
                wlocal=np.asarray(a['wire_indices'],dtype=np.int64)
                wtree=cKDTree(xyz[wlocal,:2])
                try:
                    topmask=pts[:,2] >= np.percentile(pts[:,2],70.0)
                    toppts=pts[topmask]
                    if len(toppts):
                        dd,nn=wtree.query(toppts[:,:2],k=1,workers=-1)
                        wz=xyz[wlocal[np.asarray(nn,dtype=np.int64)],2]
                        attach=int(np.count_nonzero((np.asarray(dd)<=4.2) & (np.abs(toppts[:,2]-wz)<=4.0)))
                    else:
                        attach=0
                finally:
                    del wtree
                if attach < int(cfg.get('pole_anchor_rescue_cell_min_top_links',2)):
                    continue
                score=(1.6*vf + 1.8*lf + min(hspan,15.0)/15.0 + min(attach,20)/20.0 - 0.12*cdist)
                seed_cells.append(cid)
                seed_points.append(gi)
                cell_meta.append((cid,center,vf,lf,bf,hspan,cdist,score,attach))

            if not seed_points:
                diagnostics.append({**a,'anchor':aid,'accepted':False,'reason':'no_vertical_seed','points':int(local.size),'cells':0})
                continue

            # Keep the best structural cells while preserving multi-leg pylons.
            cell_meta.sort(key=lambda x:x[-2], reverse=True)
            max_cells=int(cfg.get('pole_anchor_rescue_max_seed_cells',10))
            # Keep only high-scoring support columns near the wire-bundle
            # center.  This is the main foliage containment guard.
            chosen_meta=cell_meta[:max_cells]
            chosen_ids={m[0] for m in chosen_meta}
            chosen=[g for cid,g in zip(seed_cells,seed_points) if cid in chosen_ids]
            gi=np.unique(np.concatenate(chosen)).astype(np.int64)

            # Write structural points only.  Pure verticality is insufficient
            # because stems/branches can be vertical; require line-like support
            # as well, except for strong building/pole priors.
            structural=(
                ((vstrength[gi] >= float(cfg.get('pole_anchor_rescue_write_vert',0.22)))
                 & (lstrength[gi] >= float(cfg.get('pole_anchor_rescue_write_lin',0.34))))
                | (lstrength[gi] >= float(cfg.get('pole_anchor_rescue_write_strong_lin',0.58)))
                | (building_like[gi] & (vstrength[gi] >= 0.20) & (lstrength[gi] >= 0.20))
            )
            gi=gi[structural]
            if gi.size < int(cfg.get('pole_anchor_rescue_min_points',20)):
                diagnostics.append({**a,'anchor':aid,'accepted':False,'reason':'too_few_structural_points','points':int(gi.size),'cells':len(chosen_ids)})
                continue

            pts=xyz[gi]
            qh=np.percentile(hag[gi],[10.0,90.0])
            qx=np.percentile(pts[:,0],[5.0,95.0]); qy=np.percentile(pts[:,1],[5.0,95.0])
            xyspan=float(max(qx[1]-qx[0],qy[1]-qy[0]))
            height=float(max(np.percentile(pts[:,2],95)-np.percentile(pts[:,2],5), qh[1]-qh[0]))
            vf=float(np.mean(vstrength[gi] >= 0.24))
            lf=float(np.mean(lstrength[gi] >= float(cfg.get('pole_anchor_rescue_group_point_lin',0.32))))
            center=np.median(pts[:,:2],axis=0)
            center_dist=float(np.linalg.norm(center-axy))
            base_ok=float(qh[0]) <= float(cfg.get('pole_anchor_rescue_group_base_hag_max',5.0))
            top_ok=float(np.percentile(pts[:,2],90.0)) >= float(a['wire_z']) - float(cfg.get('pole_anchor_rescue_group_top_z_gap',4.5))
            compact=xyspan <= float(cfg.get('pole_anchor_rescue_group_max_xy',8.5))
            near_anchor=center_dist <= float(cfg.get('pole_anchor_rescue_group_center_radius',3.5))
            vertical_ok=vf >= float(cfg.get('pole_anchor_rescue_group_min_vert',0.40))
            linear_ok=lf >= float(cfg.get('pole_anchor_rescue_group_min_linear',0.35))
            height_ok=height >= float(cfg.get('pole_anchor_rescue_group_min_height',4.5))
            prom_ok=float(a['prominence']) >= float(cfg.get('pole_anchor_min_prominence',0.18))
            accepted=bool(base_ok and top_ok and compact and near_anchor and vertical_ok and linear_ok and height_ok and prom_ok)
            reason='conductor_anchor_downward' if accepted else 'group_guard_reject'
            diagnostics.append({
                **a,'anchor':aid,'accepted':accepted,'reason':reason,'points':int(gi.size),
                'cells':len(chosen_ids),'height':height,'xy':xyspan,'vert':vf,'linear':lf,
                'low_hag_p10':float(qh[0]),'center_dist':center_dist,
            })
            if accepted:
                rescued.append(gi)
    finally:
        if existing_tree is not None:
            del existing_tree

    if not rescued:
        return np.empty(0,dtype=np.int64), diagnostics
    return np.unique(np.concatenate(rescued)).astype(np.int64), diagnostics

def apply_power_asset_postprocess(
    xyz,
    classes,
    hag,
    *,
    wire_candidate_codes,
    pole_candidate_codes,
    protected_codes=(),
    wire_output_code=14,
    pole_output_code=15,
    active_indices=None,
    config=None,
    cl_coords=None,
    cl_width=0.0,
    linearity_05=None,
    planarity_05=None,
    verticality_05=None,
    linearity_10=None,
    verticality_10=None,
    log=None,
):
    """
    Production-oriented external Wire/Bare-Conductor + Pole/Pylon postprocessor.

    It never changes the trained model.  CL is a candidate prior only.  Fence
    context may support continuity, but only active_indices are written.
    """
    cfg = dict(config or {})
    xyz = np.asarray(xyz, dtype=np.float64)
    cls = np.asarray(classes, dtype=np.uint8).copy()
    hag = np.asarray(hag, dtype=np.float32).reshape(-1)
    n = len(cls)
    if len(xyz) != n or len(hag) != n:
        raise ValueError("Power postprocess array-length mismatch")
    if n == 0:
        return cls, {"enabled": True, "wires_final": 0, "poles_final": 0}

    # ------------------------------------------------------------------
    # PHASE 1 - IMMUTABLE BASE-AI SNAPSHOT / WRITE-PROTECTION FOUNDATION
    # ------------------------------------------------------------------
    # Capture the classification exactly as it enters the common Power
    # engine.  Later phases may use this snapshot as evidence, but may never
    # mutate it.  This is deliberately PTC-agnostic: the caller already
    # supplied the active numeric semantic codes.
    phase1 = build_power_phase1_state(
        cls,
        active_indices=active_indices,
        protected_codes=protected_codes,
    )
    active_write = np.asarray(phase1.active_mask, dtype=bool)
    protected = np.asarray(phase1.protected_mask, dtype=bool)
    base_classes = phase1.base_classes
    _log(log, "info", "")
    _log(log, "info", "  POWER ARCHITECTURE PHASE 1 - BASELINE LOCK")
    _log(log, "info", f"    Version               : {phase1.version}")
    _log(log, "info", f"    Base snapshot points  : {phase1.point_count:,}")
    _log(log, "info", f"    Exact writable points : {phase1.active_points:,}")
    _log(log, "info", f"    Protected base points : {phase1.protected_points:,}")
    _log(log, "info", f"    Protected codes       : {list(phase1.protected_codes)}")
    _log(log, "info", "    Snapshot immutable    : True")

    wire_codes = tuple(int(x) for x in wire_candidate_codes)
    pole_codes = tuple(int(x) for x in pole_candidate_codes)
    building_like_codes = tuple(sorted(set(pole_codes) - set(wire_codes)))

    wire_hag_min = float(cfg.get("wire_hag_min", 3.0))
    wire_hag_max = float(cfg.get("wire_hag_max", 80.0))
    wire_chain_radius = float(cfg.get("wire_chain_radius", 2.5))
    wire_density_max = int(cfg.get("wire_density_max", 12))
    wire_min_segment_pts = int(cfg.get("wire_min_segment_pts", 50))
    wire_linearity_min = float(cfg.get("wire_linearity_min", 0.72))
    wire_planarity_max = float(cfg.get("wire_planarity_max", 0.25))

    # ---------------------------------------------------------------
    # CL PRIOR
    # ---------------------------------------------------------------
    cl_active = False
    cl_mask = None
    cl_dist = None
    cl_station = None
    if cl_coords is not None and float(cl_width or 0.0) > 0.0:
        try:
            line = np.asarray(cl_coords, dtype=np.float64)
            if line.ndim == 2 and len(line) >= 2 and line.shape[1] >= 2:
                cl_dist, cl_station = _polyline_distance_station_xy(xyz, line)
                cl_mask = cl_dist <= float(cl_width)
                cl_active = bool(np.any(cl_mask))
        except Exception as exc:
            _log(log, "warning", f"  Power CL prior ignored: {exc}")

    # ---------------------------------------------------------------
    # GEOMETRY
    # ---------------------------------------------------------------
    supplied = all(
        x is not None for x in (
            linearity_05, planarity_05, verticality_05,
            linearity_10, verticality_10,
        )
    )
    if supplied:
        geom = {
            "linearity_05": np.asarray(linearity_05, dtype=np.float32),
            "planarity_05": np.asarray(planarity_05, dtype=np.float32),
            "verticality_05": np.asarray(verticality_05, dtype=np.float32),
            "linearity_10": np.asarray(linearity_10, dtype=np.float32),
            "verticality_10": np.asarray(verticality_10, dtype=np.float32),
        }
    else:
        if cl_active and cl_dist is not None:
            support_idx = np.flatnonzero(cl_dist <= (float(cl_width) + 3.0))
            if support_idx.size < 4:
                return cls, {"enabled": True, "status": "SKIPPED_EMPTY_CL_SUPPORT", "wires_final": 0, "poles_final": 0}
            geom_sub = _compute_power_geometry(xyz[support_idx], log=log)
            if geom_sub is None:
                return cls, {"enabled": True, "status": "SKIPPED_NO_GEOMETRY", "wires_final": 0, "poles_final": 0}
            geom = {}
            for key, arr in geom_sub.items():
                full = np.zeros(n, dtype=np.float32)
                full[support_idx] = np.asarray(arr, dtype=np.float32)
                geom[key] = full
        else:
            geom = _compute_power_geometry(xyz, log=log)
            if geom is None:
                return cls, {"enabled": True, "status": "SKIPPED_NO_GEOMETRY", "wires_final": 0, "poles_final": 0}

    for key, arr in geom.items():
        if len(arr) != n:
            raise ValueError(f"Power geometry length mismatch for {key}")

    lin05 = geom["linearity_05"]
    plan05 = geom["planarity_05"]
    vert05 = geom["verticality_05"]
    lin10 = geom["linearity_10"]
    vert10 = geom["verticality_10"]

    if cl_active:
        # V3.6: retain V3.5 strong geometry; real-data continuity is handled by station runs.
        # The old 0.50-linearity / 0.43-planarity gate admitted branch/hedge
        # returns simply because they were inside the corridor.
        effective_lin = max(0.62, wire_linearity_min - 0.10)
        effective_plan = min(0.33, max(wire_planarity_max, 0.30))
        effective_density = max(wire_density_max, 18)
        effective_chain = max(wire_chain_radius, 3.0)
        effective_min_seg = max(12, min(wire_min_segment_pts, 24))
        effective_hag_min = max(2.5, wire_hag_min - 0.5)
    else:
        effective_lin = wire_linearity_min
        effective_plan = wire_planarity_max
        effective_density = wire_density_max
        effective_chain = wire_chain_radius
        effective_min_seg = wire_min_segment_pts
        effective_hag_min = wire_hag_min

    # ------------------------------------------------------------------
    # PHASE 2 - RAW POWER CANDIDATE GENERATION (NO CLASSIFICATION WRITES)
    # ------------------------------------------------------------------
    # Phase 2 intentionally ignores the base Ground/Vegetation/Building
    # semantic as a hard gate.  The immutable Phase-1 classes are evidence
    # only.  This allows a real conductor/pole that the base AI called
    # Vegetation or Building to remain available for Phase 3 track fitting.
    # Context outside the exact fence is retained as support, but Phase 2
    # itself never writes classification.
    phase2 = build_power_phase2_candidates(
        xyz,
        hag,
        phase1,
        linearity_05=lin05,
        planarity_05=plan05,
        verticality_05=vert05,
        linearity_10=lin10,
        verticality_10=vert10,
        cl_mask=cl_mask,
        cl_dist=cl_dist,
        cl_station=cl_station,
        config=cfg,
    )
    phase2_report = phase2.report()
    _log(log, "info", "")
    _log(log, "info", "  POWER ARCHITECTURE PHASE 2 - RAW CANDIDATES")
    _log(log, "info", f"    Version                 : {phase2.version}")
    _log(log, "info", f"    Semantic class gate     : False")
    _log(log, "info", f"    Classification writes   : False")
    _log(log, "info", f"    Support corridor points : {phase2_report['support_points']:,}")
    _log(log, "info", f"    Raw wire candidates     : {phase2_report['raw_wire_candidates']:,}")
    _log(log, "info", f"      active/write scope    : {phase2_report['active_wire_candidates']:,}")
    _log(log, "info", f"      context-only support  : {phase2_report['context_only_wire_candidates']:,}")
    _log(log, "info", f"    Raw pole candidates     : {phase2_report['raw_pole_candidates']:,}")
    _log(log, "info", f"      active/write scope    : {phase2_report['active_pole_candidates']:,}")
    _log(log, "info", f"      context-only support  : {phase2_report['context_only_pole_candidates']:,}")
    _log(log, "info", f"    Wire base-class mix     : {phase2_report['wire_base_class_distribution']}")
    _log(log, "info", f"    Pole base-class mix     : {phase2_report['pole_base_class_distribution']}")
    _log(log, "info", f"    Candidate state immutable: {phase2_report['immutable']}")

    # ------------------------------------------------------------------
    # PHASE 3 - CONTEXT CONDUCTOR TRACK FIT + EXACT-FENCE PROJECTION
    # ------------------------------------------------------------------
    # Phase 2 intentionally returns a broad semantic-agnostic candidate cloud.
    # Phase 3 turns that cloud into physical conductor tracks using the FULL
    # run context, then projects each accepted track back onto the exact Phase-1
    # writable mask. This is the architectural fix for the previous failure
    # where context support existed but zero support points happened to lie
    # inside the exact fence.
    phase3 = build_power_phase3_tracks(
        xyz,
        hag,
        phase1,
        phase2,
        cl_coords=cl_coords,
        cl_width=float(cl_width or 0.0),
        linearity_05=lin05,
        planarity_05=plan05,
        verticality_05=vert05,
        linearity_10=lin10,
        verticality_10=vert10,
        cl_station=cl_station,
        cl_dist=cl_dist,
        config=cfg,
    )
    phase3_report = phase3.report()
    _log(log, "info", "")
    _log(log, "info", "  POWER ARCHITECTURE PHASE 3 - CONDUCTOR TRACKS")
    _log(log, "info", f"    Version                    : {phase3.version}")
    _log(log, "info", f"    Status                     : {phase3.status}")
    _log(log, "info", f"    Semantic class gate        : False")
    _log(log, "info", f"    Context track fitting      : True")
    _log(log, "info", f"    Exact-fence projection     : {phase3_report['exact_fence_projection']}")
    _log(log, "info", f"    Accepted conductor tracks  : {phase3_report['tracks']}")
    _log(log, "info", f"    Confirmed context points   : {phase3_report['confirmed_context_points']:,}")
    _log(log, "info", f"    Projected active points    : {phase3_report['projected_active_points']:,}")
    _log(log, "info", f"    Projection rescue points   : {phase3_report['projection_rescue_points']:,}")
    _log(log, "info", f"    Candidate state immutable  : {phase3_report['immutable']}")
    for tr in phase3.tracks[:12]:
        _log(
            log,
            "info",
            f"      Track {tr.track_id:02d}: station={tr.station_min:.1f}..{tr.station_max:.1f}m "
            f"span={tr.span:.1f}m nodes={tr.nodes} pts={tr.candidate_points} "
            f"coverage={tr.coverage:.2f} score={tr.median_score:.2f} "
            f"lat_p90={tr.lateral_residual_p90:.2f}m z_p90={tr.z_residual_p90:.2f}m",
        )

    # ------------------------------------------------------------------
    # PHASE 6 - CROSS-MODE POWER RECOVERY / PHASE-3 FAILURE FALLBACK
    # ------------------------------------------------------------------
    # The normal Phase-3 path remains authoritative whenever it succeeds.
    # If a model/fence produces raw Phase-2 conductor evidence but Phase 3
    # accepts zero tracks, Phase 6 retries only that failed case with a
    # sparse/short-span profile and requires a multi-track parallel conductor
    # bundle before any fallback is trusted. This keeps Premium's successful
    # path untouched and prevents a single tree branch from becoming Wire.
    phase6 = build_power_phase6_recovery(
        xyz,
        hag,
        phase1,
        phase2,
        phase3,
        cl_coords=cl_coords,
        cl_width=float(cl_width or 0.0),
        linearity_05=lin05,
        planarity_05=plan05,
        verticality_05=vert05,
        linearity_10=lin10,
        verticality_10=vert10,
        cl_station=cl_station,
        cl_dist=cl_dist,
        config=cfg,
    )
    phase6_report = phase6.report()
    effective_phase3 = phase6.effective_phase3
    effective_phase3_report = effective_phase3.report()

    _log(log, "info", "")
    _log(log, "info", "  POWER ARCHITECTURE PHASE 6 - CROSS-MODE POWER RECOVERY")
    _log(log, "info", f"    Version                    : {phase6.version}")
    _log(log, "info", f"    Status                     : {phase6.status}")
    _log(log, "info", f"    Activated                  : {phase6_report['activated']}")
    _log(log, "info", f"    Normal Phase3 tracks       : {phase6_report['original_tracks']}")
    _log(log, "info", f"    Fallback Phase3 tracks     : {phase6_report['fallback_tracks']}")
    _log(log, "info", f"    Effective conductor tracks : {phase6_report['effective_tracks']}")
    _log(log, "info", f"    Parallel bundle tracks     : {phase6_report['bundle_tracks']}")
    _log(log, "info", f"    Bundle compatible pairs    : {phase6_report['bundle_pair_count']}")
    _log(log, "info", f"    Bundle span                : {phase6_report['bundle_common_span_m']:.2f} m")
    _log(log, "info", f"    Recovered context Wire     : {phase6_report['recovered_context_wire_points']:,}")
    _log(log, "info", f"    Recovered active Wire      : {phase6_report['recovered_active_wire_points']:,}")
    _log(log, "info", f"    Exact-fence projection     : {phase6_report['exact_fence_projection']}")
    _log(log, "info", f"    Semantic class gate        : False")
    for d in phase6.diagnostics[:12]:
        _log(log, "info", f"      Phase6 diagnostic: {d}")

    # ------------------------------------------------------------------
    # PHASE 10 - FULL CONDUCTOR POINT DENSIFICATION
    # ------------------------------------------------------------------
    # Phase 3/6 already proved the physical conductor routes.  The previous
    # projection was intentionally conservative and could leave many genuine
    # wire returns carrying their base Ground/Vegetation label.  Phase 10 does
    # not discover a new route: it builds a tight local 3D tube around each
    # confirmed track and completes the actual LiDAR points on that wire before
    # support recovery / validation / attachment-boundary resolution.
    phase10 = build_power_phase10_densification(
        xyz,
        hag,
        phase1,
        phase2,
        effective_phase3,
        cl_coords=cl_coords,
        cl_width=float(cl_width or 0.0),
        linearity_05=lin05,
        planarity_05=plan05,
        verticality_05=vert05,
        linearity_10=lin10,
        verticality_10=vert10,
        cl_station=cl_station,
        cl_dist=cl_dist,
        config=cfg,
    )
    phase10_report = phase10.report()
    effective_phase3 = phase10.effective_phase3
    effective_phase3_report = effective_phase3.report()

    _log(log, "info", "")
    _log(log, "info", "  POWER ARCHITECTURE PHASE 10 - CONDUCTOR POINT DENSIFICATION")
    _log(log, "info", f"    Version                    : {phase10.version}")
    _log(log, "info", f"    Status                     : {phase10.status}")
    _log(log, "info", f"    Input context Wire         : {phase10_report['input_context_wire_points']:,}")
    _log(log, "info", f"    Input active Wire          : {phase10_report['input_active_wire_points']:,}")
    _log(log, "info", f"    Added context Wire         : {phase10_report['added_context_wire_points']:,}")
    _log(log, "info", f"    Added active Wire          : {phase10_report['added_active_wire_points']:,}")
    _log(log, "info", f"    Final context Wire         : {phase10_report['final_context_wire_points']:,}")
    _log(log, "info", f"    Final active Wire          : {phase10_report['final_active_wire_points']:,}")
    _log(log, "info", f"    Tracks densified           : {phase10_report['tracks_densified']}/{phase10_report['tracks_available']}")
    _log(log, "info", f"    Added base-class mix       : {phase10_report['added_base_class_distribution']}")
    _log(log, "info", f"    Semantic class gate        : False")
    _log(log, "info", f"    Mode independent           : {phase10_report['mode_independent']}")
    _log(log, "info", f"    Exact-fence only           : {phase10_report['exact_fence_only']}")
    _log(log, "info", f"    Densification immutable    : {phase10_report['immutable']}")
    for d in phase10.diagnostics[:16]:
        _log(
            log,
            "info",
            f"      Phase10 track {int(d.get('track_id', -1)):02d}: "
            f"span={float(d.get('span_m', 0.0)):.1f}m cov={float(d.get('coverage', 0.0)):.2f} "
            f"seed={int(d.get('seed_points', 0)):,} bins={int(d.get('profile_bins', 0))} "
            f"added_ctx={int(d.get('added_context', 0)):,} added_active={int(d.get('added_active', 0)):,} "
            f"-> {'DENSIFY' if d.get('densified') else 'KEEP'} [{d.get('reason', 'n/a')}]",
        )

    wire_candidates = int(phase2_report.get("raw_wire_candidates", 0))
    wire_support_idx = np.empty(0, dtype=np.int64)
    wire_write_idx = np.empty(0, dtype=np.int64)
    wire_profile_report = {
        "mode": "PHASE3_TRACK_FIT",
        "tracks": int(effective_phase3_report.get("tracks", 0)),
        "accepted_tracks": int(effective_phase3_report.get("tracks", 0)),
        "kept": int(effective_phase3_report.get("wire_support_points", 0)),
        "projection_rescue_points": int(effective_phase3_report.get("projection_rescue_points", 0)),
        "phase6_fallback": bool(phase6_report.get("fallback_accepted", False)),
    }

    if cl_active:
        # With a valid CL, Phase 3 is authoritative for conductors. Do NOT fall
        # back to the old semantic-gated wire selector: that would reintroduce
        # the vegetation/building dependency that Phase 2/3 were built to remove.
        wire_support_idx = np.flatnonzero(np.asarray(effective_phase3.wire_support_mask, dtype=bool))
        wire_write_idx = np.flatnonzero(np.asarray(effective_phase3.projected_active_mask, dtype=bool))
        if wire_write_idx.size:
            cls[wire_write_idx] = np.uint8(wire_output_code)
    else:
        # No CL: preserve the established legacy wire path. Phase 3 needs a CL
        # station/lateral frame and therefore intentionally does not replace
        # non-CL behaviour.
        regular_wire_class = np.isin(cls, list(wire_codes))
        # The 5-class base model can call a conductor Building.  Only allow these
        # extra structural classes through with a much stronger geometry gate.
        extra_wire_class = (
            np.isin(cls, list(building_like_codes))
            if building_like_codes else np.zeros(n, dtype=bool)
        )

        wire_pool = (
            (regular_wire_class | extra_wire_class)
            & (~protected)
            & (hag >= effective_hag_min)
            & (hag <= wire_hag_max)
        )
        if cl_active and cl_mask is not None:
            wire_pool &= cl_mask

        pool_idx = np.flatnonzero(wire_pool)
        wire_candidates = 0
        wire_support_idx = np.empty(0, dtype=np.int64)
        wire_profile_report = {"mode": "NOT_USED", "tracks": 0, "accepted_tracks": 0, "kept": 0}

        if pool_idx.size:
            regular = regular_wire_class[pool_idx]
            normal_geom = (
                ((lin05[pool_idx] >= effective_lin) & (plan05[pool_idx] <= effective_plan))
                | (lin10[pool_idx] >= effective_lin)
            )
            building_geom = (
                ((lin05[pool_idx] >= 0.62) | (lin10[pool_idx] >= 0.68))
                & (plan05[pool_idx] <= 0.28)
            )
            geom_ok = np.where(regular, normal_geom, building_geom)
            wire_support_idx = pool_idx[geom_ok]
            wire_candidates = int(wire_support_idx.size)

        if wire_support_idx.size:
            try:
                t = cKDTree(xyz[wire_support_idx])
                density = np.asarray(
                    t.query_ball_point(xyz[wire_support_idx], r=0.5, workers=-1, return_length=True),
                    dtype=np.int32,
                ) - 1
                del t
                is_extra = extra_wire_class[wire_support_idx]
                lim = np.where(is_extra, min(20, effective_density), effective_density)
                wire_support_idx = wire_support_idx[density <= lim]
            except Exception as exc:
                _log(log, "warning", f"  Power wire density filter skipped: {exc}")

        if wire_support_idx.size:
            try:
                t = cKDTree(xyz[wire_support_idx])
                chain_count = np.asarray(
                    t.query_ball_point(xyz[wire_support_idx], r=effective_chain, workers=-1, return_length=True),
                    dtype=np.int32,
                ) - 1
                del t
                wire_support_idx = wire_support_idx[chain_count >= 1]
            except Exception as exc:
                _log(log, "warning", f"  Power wire chain filter skipped: {exc}")

        if wire_support_idx.size and cl_active and cl_dist is not None and cl_station is not None:
            # Preserve parallel conductors across the ROW. +/-9 m becomes about
            # +/-5.9 m instead of the old +/-2.8 m clamp.
            max_wire_width = max(3.5, min(6.0, float(cl_width) * 0.65))
            max_wire_width = min(max_wire_width, float(cl_width))
            wire_support_idx = wire_support_idx[cl_dist[wire_support_idx] <= max_wire_width]

            if wire_support_idx.size:
                station = cl_station[wire_support_idx]
                score = (
                    np.clip(lin05[wire_support_idx], 0.0, 1.0)
                    + np.clip(lin10[wire_support_idx], 0.0, 1.0)
                    + np.clip(1.0 - plan05[wire_support_idx], 0.0, 1.0)
                    + 1.0 / (cl_dist[wire_support_idx].astype(np.float32) + 1.0)
                )
                bins = np.floor((station - float(np.nanmin(station))) / 2.0).astype(np.int64)
                keep_local = []
                max_per_bin = 100
                for b in np.unique(bins):
                    loc = np.flatnonzero(bins == b)
                    if len(loc) <= max_per_bin:
                        keep_local.append(loc)
                    else:
                        keep_local.append(loc[np.argpartition(score[loc], -max_per_bin)[-max_per_bin:]])
                if keep_local:
                    keep_local = np.concatenate(keep_local)
                    wire_support_idx = wire_support_idx[keep_local]
                    station = station[keep_local]
                    # V3.6 REAL-DATA FIX. Track conductor profiles by station-bin
                    # Z clusters instead of V3.5's point-to-point graph. This is
                    # tolerant of sparse/sagging wires while still requiring a
                    # smooth longitudinal profile that branch/hedge noise lacks.
                    profile_keep, profile_diag = _wire_profile_continuity_mask(
                        xyz,
                        wire_support_idx,
                        cl_station,
                        cl_dist,
                        lin05=lin05,
                        lin10=lin10,
                        plan05=plan05,
                        config=cfg,
                    )
                    profile_count = int(np.count_nonzero(profile_keep))
                    wire_profile_report = dict(profile_diag)
                    wire_profile_report["mode"] = "PROFILE"

                    if profile_count >= effective_min_seg:
                        wire_support_idx = wire_support_idx[profile_keep]
                    else:
                        # Fail-safe for very sparse real conductors: retain only
                        # sustained station runs with strong line geometry. This
                        # prevents the V3.5 regression where 4k+ real candidates
                        # became zero, without reverting to V3.4's loose gates.
                        run_keep = _long_station_run_mask(
                            station,
                            bin_size=float(cfg.get("wire_run_bin_m", 2.0)),
                            min_run_m=float(cfg.get("wire_run_min_m", 10.0)),
                            max_gap_bins=int(cfg.get("wire_run_max_gap_bins", 3)),
                        )
                        strong = (
                            (np.maximum(lin05[wire_support_idx], lin10[wire_support_idx]) >= float(cfg.get("wire_fallback_min_linearity", 0.66)))
                            & (plan05[wire_support_idx] <= float(cfg.get("wire_fallback_max_planarity", 0.32)))
                        )
                        fallback_keep = run_keep & strong
                        fallback_count = int(np.count_nonzero(fallback_keep))
                        wire_profile_report.update({
                            "mode": "STRONG_STATION_FALLBACK",
                            "profile_kept": profile_count,
                            "fallback_kept": fallback_count,
                        })
                        wire_support_idx = wire_support_idx[fallback_keep]

        if wire_support_idx.size < effective_min_seg:
            wire_support_idx = np.empty(0, dtype=np.int64)

        wire_write_idx = wire_support_idx[active_write[wire_support_idx]] if wire_support_idx.size else np.empty(0, dtype=np.int64)
        if wire_write_idx.size:
            cls[wire_write_idx] = np.uint8(wire_output_code)


    # ---------------------------------------------------------------
    # POLE / PYLON / TOWER
    # ---------------------------------------------------------------
    # Use the immutable Phase-1 base classes for pole eligibility. Phase-3 Wire
    # projection has already written ``cls`` for exact-fence conductor points;
    # it must not erase the original evidence needed by the existing structural
    # pole detector at conductor attachment/crossarm locations.
    building_like = (
        np.isin(base_classes, list(building_like_codes))
        if building_like_codes else np.zeros(n, dtype=bool)
    )
    vertical_structural = (vert05 >= 0.16) | (vert10 >= 0.18)
    pole_pool = (
        np.isin(base_classes, list(pole_codes))
        & (~protected)
        & (hag >= 1.0)
        & (hag <= 80.0)
        & (building_like | vertical_structural)
    )
    if cl_active and cl_mask is not None:
        pole_pool &= cl_mask

    pole_support_idx = np.flatnonzero(pole_pool)

    if pole_support_idx.size and wire_support_idx.size:
        try:
            wt = cKDTree(xyz[wire_support_idx, :2])
            d, _ = wt.query(xyz[pole_support_idx, :2], k=1, workers=-1)
            del wt
            near_limit = max(6.0, min(10.0, float(cl_width) * 0.95)) if cl_active else 8.0
            pole_support_idx = pole_support_idx[np.asarray(d) <= near_limit]
        except Exception as exc:
            _log(log, "warning", f"  Pole/Pylon near-wire prefilter skipped: {exc}")
    elif pole_support_idx.size and not cl_active:
        # No wire + no CL is too risky for an external power-asset override.
        pole_support_idx = np.empty(0, dtype=np.int64)

    # V3.2 STRUCTURAL-CORE POLE/PYLON EXTRACTION.
    # Do not accept a broad connected vegetation/tower/noise component as one
    # object. Detect compact vertical columns tied to the conductor top and
    # write only the structural core.
    pole_support_final, pole_diagnostics = _extract_structural_pole_cores(
        xyz,
        hag,
        pole_support_idx,
        wire_support_idx,
        vert05=vert05,
        vert10=vert10,
        lin05=lin05,
        lin10=lin10,
        building_like=building_like,
        cl_station=cl_station,
        cl_active=cl_active,
        config=cfg,
        log=log,
    )

    # V3.4 CONDUCTOR-ANCHOR DOWNWARD RESCUE.  This does not loosen the
    # generic pole thresholds.  It only searches beneath real conductor sag
    # high-points and writes compact structural columns, allowing a true pole
    # that the base semantic model called vegetation to be recovered safely.
    anchor_rescue_idx, anchor_diagnostics = _extract_attachment_driven_pole_cores(
        xyz,
        hag,
        pole_support_idx,
        wire_support_idx,
        vert05=vert05,
        vert10=vert10,
        lin05=lin05,
        lin10=lin10,
        building_like=building_like,
        cl_station=cl_station,
        cl_active=cl_active,
        already_accepted=pole_support_final,
        config=cfg,
        log=log,
    )
    if anchor_rescue_idx.size:
        pole_support_final = np.unique(np.concatenate([pole_support_final, anchor_rescue_idx])).astype(np.int64)

    # ------------------------------------------------------------------
    # PHASE 5 - CONDUCTOR-ANCHORED FULL SUPPORT RECOVERY
    # ------------------------------------------------------------------
    # Phase 3 has already proven the conductor bundle.  Phase 5 uses common
    # conductor endpoints / sag support stations to grow the physical support
    # downward through raw geometry, regardless of the base AI semantic label.
    # It is a rescue-only phase: already-good V3.3/V3.4 pole cores are kept as
    # authoritative and Phase 5 does not broaden them.
    phase5 = build_power_phase5_supports(
        xyz,
        hag,
        phase1,
        phase2,
        effective_phase3,
        linearity_05=lin05,
        planarity_05=plan05,
        verticality_05=vert05,
        linearity_10=lin10,
        verticality_10=vert10,
        cl_station=cl_station,
        cl_dist=cl_dist,
        existing_pole_indices=pole_support_final,
        config=cfg,
    )
    phase5_report = phase5.report()
    phase5_idx = np.flatnonzero(np.asarray(phase5.support_context_mask, dtype=bool))
    if phase5_idx.size:
        pole_support_final = np.unique(np.concatenate([pole_support_final, phase5_idx])).astype(np.int64)

    for d in anchor_diagnostics[:30]:
        _log(
            log,
            "info",
            f"    Pole anchor rescue {d.get('anchor', -1):03d}: "
            f"station={d.get('station', float('nan')):.2f}m "
            f"prom={d.get('prominence', 0.0):.2f}m "
            f"wire_pts={d.get('wire_points', 0)} cells={d.get('cells', 0)} "
            f"pts={d.get('points', 0):,} "
            f"height={d.get('height', 0.0):.2f}m xy={d.get('xy', 0.0):.2f}m "
            f"vert={d.get('vert', 0.0):.2f} lowHAG={d.get('low_hag_p10', float('nan')):.2f} "
            f"center={d.get('center_dist', float('nan')):.2f}m "
            f"-> {'ACCEPT' if d.get('accepted') else 'REJECT'} [{d.get('reason', 'n/a')}]",
        )

    for d in pole_diagnostics[:30]:
        _log(
            log,
            "info",
            f"    Pole/Pylon core {d['group']:03d}: anchors={d['anchors']} "
            f"cells={d['core_cells']} pts={d['points']:,} "
            f"height={d['height']:.2f}m xy={d['xy']:.2f}m bins={d['bins']} "
            f"vert={d['vert']:.2f} lin={d['linear']:.2f} "
            f"top_links={d['top_links']} ({d.get('top_link_fraction', 0.0):.1%}) "
            f"lowHAG={d['low_hag_p10']:.2f} station={d['station_span']:.2f}m "
            f"aspect={d['aspect']:.2f} -> {d['kind']} [{d.get('accept_reason', 'n/a')}]",
        )

    _log(log, "info", "")
    _log(log, "info", "  POWER ARCHITECTURE PHASE 5 - CONDUCTOR-ANCHORED SUPPORT RECOVERY")
    _log(log, "info", f"    Version                    : {phase5.version}")
    _log(log, "info", f"    Status                     : {phase5.status}")
    _log(log, "info", f"    Support anchors            : {phase5_report['anchors']}")
    _log(log, "info", f"    Accepted supports          : {phase5_report['accepted_supports']}")
    _log(log, "info", f"    Support context points     : {phase5_report['support_context_points']:,}")
    _log(log, "info", f"    Support active points      : {phase5_report['support_active_points']:,}")
    _log(log, "info", f"    Conductor anchored         : {phase5_report['conductor_anchored']}")
    _log(log, "info", f"    Exact-fence only           : {phase5_report['exact_fence_only']}")
    _log(log, "info", f"    Support state immutable    : {phase5_report['immutable']}")
    for d in phase5.diagnostics[:30]:
        _log(
            log,
            "info",
            f"      Phase5 support {int(d.get('anchor', -1)):03d}: "
            f"station={float(d.get('station', float('nan'))):.2f}m "
            f"source={d.get('source', 'n/a')} tracks={int(d.get('track_count', 0))} "
            f"wire_pts={int(d.get('wire_points', 0))} cand={int(d.get('candidates', 0)):,} "
            f"cells={int(d.get('selected_cells', d.get('cells', 0)))} pts={int(d.get('points', 0)):,} "
            f"height={float(d.get('height', 0.0)):.2f}m xy={float(d.get('xy', 0.0)):.2f}m "
            f"station_span={float(d.get('station_span', 0.0)):.2f}m "
            f"vert={float(d.get('vert', 0.0)):.2f} lin={float(d.get('linear', 0.0)):.2f} "
            f"top_links={int(d.get('top_links', 0))} lowHAG={float(d.get('low_hag_p10', float('nan'))):.2f} "
            f"-> {'ACCEPT' if d.get('accepted') else 'REJECT'} [{d.get('reason', 'n/a')}]",
        )

    # ------------------------------------------------------------------
    # PHASE 7 - PRODUCTION VALIDATION / FALSE-POSITIVE GUARD
    # ------------------------------------------------------------------
    # Phases 3/6 recover conductors and Phases 5 / structural-core logic recover
    # supports. Phase 7 is intentionally not another detector. It validates the
    # already-confirmed assets before Phase 4 freezes them, rejecting clear
    # vegetation/tree-shaped support leakage while preserving coherent narrow
    # poles and lattice pylons attached to the confirmed conductor bundle.
    phase7 = build_power_phase7_validation(
        xyz,
        hag,
        phase1,
        phase2,
        effective_phase3,
        wire_indices=wire_write_idx,
        pole_context_indices=pole_support_final,
        linearity_05=lin05,
        verticality_05=vert05,
        linearity_10=lin10,
        verticality_10=vert10,
        cl_station=cl_station,
        cl_dist=cl_dist,
        cl_width=float(cl_width or 0.0),
        config=cfg,
    )
    phase7_report = phase7.report()

    phase7_wire_idx = np.flatnonzero(np.asarray(phase7.validated_wire_mask, dtype=bool))
    if wire_write_idx.size:
        removed_wire_idx = np.setdiff1d(wire_write_idx, phase7_wire_idx, assume_unique=False)
        if removed_wire_idx.size:
            # Roll back only a Phase-7-rejected Wire point to the immutable
            # pre-Power baseline. Outside-fence points are never present here.
            cls[removed_wire_idx] = base_classes[removed_wire_idx]
    wire_write_idx = phase7_wire_idx.astype(np.int64, copy=False)
    pole_support_final = np.flatnonzero(
        np.asarray(phase7.validated_pole_context_mask, dtype=bool)
    ).astype(np.int64, copy=False)

    _log(log, "info", "")
    _log(log, "info", "  POWER ARCHITECTURE PHASE 7 - PRODUCTION VALIDATION / FALSE-POSITIVE GUARD")
    _log(log, "info", f"    Version                    : {phase7.version}")
    _log(log, "info", f"    Status                     : {phase7.status}")
    _log(log, "info", f"    Input Wire points          : {phase7_report['input_wire_points']:,}")
    _log(log, "info", f"    Validated Wire points      : {phase7_report['validated_wire_points']:,}")
    _log(log, "info", f"    Input Pole context points  : {phase7_report['input_pole_context_points']:,}")
    _log(log, "info", f"    Validated Pole context     : {phase7_report['validated_pole_context_points']:,}")
    _log(log, "info", f"    Validated Pole active      : {phase7_report['validated_pole_active_points']:,}")
    _log(log, "info", f"    Rejected Pole leakage      : {phase7_report['rejected_pole_context_points']:,}")
    _log(log, "info", f"    Accepted support components: {phase7_report['accepted_support_components']}")
    _log(log, "info", f"    Rejected support components: {phase7_report['rejected_support_components']}")
    _log(log, "info", f"    False-positive guard       : {phase7_report['false_positive_guard']}")
    _log(log, "info", f"    Exact-fence only           : {phase7_report['exact_fence_only']}")
    _log(log, "info", f"    Validation immutable       : {phase7_report['immutable']}")
    for d in phase7.diagnostics[:30]:
        _log(
            log,
            "info",
            f"      Phase7 support {int(d.get('component', -1)):03d}: "
            f"input={int(d.get('input_points', 0)):,} structural={int(d.get('structural_points', 0)):,} "
            f"height={float(d.get('height', 0.0)):.2f}m xy={float(d.get('xy_span', 0.0)):.2f}m "
            f"station={float(d.get('station_span', 0.0)):.2f}m lowHAG={float(d.get('low_hag_p10', float('nan'))):.2f} "
            f"vert={float(d.get('median_verticality', 0.0)):.2f} lin={float(d.get('median_linearity', 0.0)):.2f} "
            f"struct={float(d.get('structural_fraction', 0.0)):.2f} score={float(d.get('pole_score', 0.0)):.2f} "
            f"top_links={int(d.get('top_links', 0))} "
            f"-> {'ACCEPT' if d.get('accepted') else 'REJECT'} "
            f"[{d.get('kind', 'n/a')}:{d.get('reason', 'n/a')}]",
        )

    # ------------------------------------------------------------------
    # PHASE 8 - PRODUCTION CONSISTENCY / MULTI-SCENE FIREWALL
    # ------------------------------------------------------------------
    # Phase 7 validates physical support geometry. Phase 8 does not redetect
    # anything: it checks the validated result as a production scene, preserves
    # legitimate wire-only / support-only / fence-clipped cases, and blocks
    # impossible orphan assets before Phase 4 freezes the final masks.
    phase8 = build_power_phase8_consistency(
        xyz,
        phase1,
        effective_phase3,
        phase7,
        recovery_used=bool(phase6_report.get("fallback_accepted", False)),
        cl_active=bool(cl_active),
        config=cfg,
    )
    phase8_report = phase8.report()

    phase8_wire_idx = np.flatnonzero(np.asarray(phase8.final_wire_mask, dtype=bool))
    if wire_write_idx.size:
        removed_wire_idx = np.setdiff1d(wire_write_idx, phase8_wire_idx, assume_unique=False)
        if removed_wire_idx.size:
            cls[removed_wire_idx] = base_classes[removed_wire_idx]
    wire_write_idx = phase8_wire_idx.astype(np.int64, copy=False)
    pole_support_final = np.flatnonzero(
        np.asarray(phase8.final_pole_context_mask, dtype=bool)
    ).astype(np.int64, copy=False)

    _log(log, "info", "")
    _log(log, "info", "  POWER ARCHITECTURE PHASE 8 - PRODUCTION CONSISTENCY / MULTI-SCENE FIREWALL")
    _log(log, "info", f"    Version                    : {phase8.version}")
    _log(log, "info", f"    Status                     : {phase8.status}")
    _log(log, "info", f"    Scenario                   : {phase8_report['scenario']}")
    _log(log, "info", f"    Route                      : {phase8_report['route']}")
    _log(log, "info", f"    Input Wire points          : {phase8_report['input_wire_points']:,}")
    _log(log, "info", f"    Final consistent Wire      : {phase8_report['final_wire_points']:,}")
    _log(log, "info", f"    Input Pole context         : {phase8_report['input_pole_context_points']:,}")
    _log(log, "info", f"    Final Pole context         : {phase8_report['final_pole_context_points']:,}")
    _log(log, "info", f"    Final Pole active          : {phase8_report['final_pole_active_points']:,}")
    _log(log, "info", f"    Orphan Wire rejected       : {phase8_report['orphan_wire_rejected']:,}")
    _log(log, "info", f"    Orphan Pole rejected       : {phase8_report['orphan_pole_rejected']:,}")
    _log(log, "info", f"    Support components         : {phase8_report['support_components']}")
    _log(log, "info", f"    Boundary-clipped supports  : {phase8_report['boundary_clipped_supports']}")
    _log(log, "info", f"    Context-only supports      : {phase8_report['context_only_supports']}")
    _log(log, "info", f"    Production consistency     : {phase8_report['production_consistency']}")
    _log(log, "info", f"    Mode independent           : {phase8_report['mode_independent']}")
    _log(log, "info", f"    Exact-fence only           : {phase8_report['exact_fence_only']}")
    _log(log, "info", f"    Consistency immutable      : {phase8_report['immutable']}")
    tq = phase8_report.get('track_quality', {})
    _log(log, "info", f"    Track quality              : count={int(tq.get('track_count', 0))} median_span={float(tq.get('median_span_m', 0.0)):.2f}m min_coverage={float(tq.get('min_coverage', 0.0)):.2f}")
    for d in phase8.diagnostics[:30]:
        _log(
            log,
            "info",
            f"      Phase8 support {int(d.get('component', -1)):03d}: "
            f"context={int(d.get('context_points', 0)):,} active={int(d.get('active_points', 0)):,} "
            f"active_fraction={float(d.get('active_fraction', 0.0)):.2f} "
            f"boundary_clipped={bool(d.get('boundary_clipped', False))} "
            f"context_only={bool(d.get('context_only', False))} "
            f"-> PRESERVE [{d.get('reason', 'n/a')}]",
        )

    # ------------------------------------------------------------------
    # PHASE 9 - WIRE / POLE ATTACHMENT-BOUNDARY RESOLUTION
    # ------------------------------------------------------------------
    # Phase 8 has already confirmed a production-consistent scene. On lattice
    # supports the validated Pole mask can still overlap several metres of a
    # fitted conductor near a crossarm. Phase 9 resolves only that overlap:
    # Pole keeps a compact structural attachment zone and the remaining
    # already-confirmed track points stay Wire. No new asset is detected here.
    phase9 = build_power_phase9_attachment_boundary(
        xyz,
        phase1,
        effective_phase3,
        phase8,
        linearity_05=lin05,
        verticality_05=vert05,
        linearity_10=lin10,
        verticality_10=vert10,
        cl_station=cl_station,
        config=cfg,
    )
    phase9_report = phase9.report()
    wire_write_idx = np.flatnonzero(np.asarray(phase9.final_wire_mask, dtype=bool)).astype(np.int64, copy=False)
    pole_support_final = np.flatnonzero(np.asarray(phase9.final_pole_context_mask, dtype=bool)).astype(np.int64, copy=False)

    _log(log, "info", "")
    _log(log, "info", "  POWER ARCHITECTURE PHASE 9 - WIRE / POLE ATTACHMENT BOUNDARY")
    _log(log, "info", f"    Version                    : {phase9.version}")
    _log(log, "info", f"    Status                     : {phase9.status}")
    _log(log, "info", f"    Input Wire points          : {phase9_report['input_wire_points']:,}")
    _log(log, "info", f"    Input Pole context         : {phase9_report['input_pole_context_points']:,}")
    _log(log, "info", f"    Input Wire/Pole overlap    : {phase9_report['input_overlap_points']:,}")
    _log(log, "info", f"    Reclaimed conductor Wire   : {phase9_report['wire_reclaimed_from_pole']:,}")
    _log(log, "info", f"    Attachment overlap retained: {phase9_report['attachment_overlap_retained']:,}")
    _log(log, "info", f"    Final Wire before Phase4   : {phase9_report['final_wire_points']:,}")
    _log(log, "info", f"    Final Pole active          : {phase9_report['final_pole_active_points']:,}")
    _log(log, "info", f"    Creates new assets         : {phase9_report['creates_new_assets']}")
    _log(log, "info", f"    Mode independent           : {phase9_report['mode_independent']}")
    _log(log, "info", f"    Exact-fence only           : {phase9_report['exact_fence_only']}")
    _log(log, "info", f"    Resolution immutable       : {phase9_report['immutable']}")
    for d in phase9.diagnostics[:30]:
        _log(
            log,
            "info",
            f"      Phase9 support {int(d.get('component', -1)):03d}: "
            f"overlap={int(d.get('input_overlap', 0)):,} "
            f"reclaimed_wire={int(d.get('reclaimed_wire', 0)):,} "
            f"attachment={int(d.get('retained_attachment', 0)):,} "
            f"core_station={float(d.get('core_station', float('nan'))):.2f}m "
            f"core_span={float(d.get('core_station_span', 0.0)):.2f}m "
            f"halfwidth={float(d.get('attachment_halfwidth', 0.0)):.2f}m "
            f"-> {d.get('reason', 'n/a')}",
        )

    if pole_support_final.size:
        # Structural Pole/Pylon confirmation outranks the conductor tube at an
        # attachment. A fitted conductor necessarily crosses the support, so a
        # few tower/crossarm points can lie inside the Phase-3 wire tube. If the
        # dedicated structural-core detector confirms those same points as the
        # support, Pole/Pylon is the more specific semantic and wins.
        pole_write_idx = pole_support_final[active_write[pole_support_final]]
        if pole_write_idx.size:
            cls[pole_write_idx] = np.uint8(pole_output_code)
    else:
        pole_write_idx = np.empty(0, dtype=np.int64)

    # ------------------------------------------------------------------
    # PHASE 4 - FINAL POWER-ASSET PROTECTION / PRECEDENCE RESOLUTION
    # ------------------------------------------------------------------
    # Phase 9 has already returned fitted conductor overlap outside the compact
    # attachment zone to Wire. Any residual overlap is the physical support /
    # crossarm attachment itself, where Pole/Pylon remains authoritative. Phase
    # 4 freezes those final exact-fence masks and re-asserts their output codes.
    phase4 = build_power_phase4_protection(
        phase1,
        wire_indices=wire_write_idx,
        pole_indices=pole_write_idx,
    )
    phase4_report = phase4.report()
    cls, phase4_repaired = apply_power_phase4_protection(
        cls,
        phase4,
        wire_output_code=wire_output_code,
        pole_output_code=pole_output_code,
    )
    wire_write_idx = np.flatnonzero(np.asarray(phase4.wire_mask, dtype=bool))
    pole_write_idx = np.flatnonzero(np.asarray(phase4.pole_mask, dtype=bool))

    _log(log, "info", "")
    _log(log, "info", "  POWER ARCHITECTURE PHASE 4 - FINAL ASSET PROTECTION")
    _log(log, "info", f"    Version                    : {phase4.version}")
    _log(log, "info", f"    Input Wire points          : {phase4_report['input_wire_points']:,}")
    _log(log, "info", f"    Input Pole/Pylon points    : {phase4_report['input_pole_points']:,}")
    _log(log, "info", f"    Wire/Pole overlap          : {phase4_report['wire_pole_overlap_points']:,}")
    _log(log, "info", f"    Final protected Wire       : {phase4_report['final_wire_points']:,}")
    _log(log, "info", f"    Final protected Pole       : {phase4_report['final_pole_points']:,}")
    _log(log, "info", f"    Protected power total      : {phase4_report['protected_power_points']:,}")
    _log(log, "info", f"    Pole precedence            : {phase4_report['pole_precedence']}")
    _log(log, "info", f"    Exact-fence only           : {phase4_report['exact_fence_only']}")
    _log(log, "info", f"    Final reassert repairs     : {phase4_repaired:,}")
    _log(log, "info", f"    Protection state immutable : {phase4_report['immutable']}")

    # Phase-1 invariant: the frozen baseline must still be byte-for-byte the
    # classification that entered this function.  ``cls`` is the mutable
    # working output; ``base_classes`` remains untouched for Phase 2+.
    phase1_baseline_intact = bool(not base_classes.flags.writeable)
    # PHASE2_RAW_CANDIDATES_V3_8: observation-only candidate layer active.
    # PHASE3_CONDUCTOR_TRACK_FIT_V3_9: context-fit + exact-fence projection active.
    # PHASE4_FINAL_ASSET_PROTECTION_V3_10: immutable final Wire/Pole masks active.
    # PHASE5_CONDUCTOR_ANCHORED_SUPPORT_RECOVERY_V3_11: full support rescue active.
    # PHASE6_CROSS_MODE_POWER_RECOVERY_V3_12: Phase-3 failure fallback active.
    # PHASE7_PRODUCTION_VALIDATION_V3_13: final false-positive QC guard active.
    # PHASE8_PRODUCTION_CONSISTENCY_V3_14: multi-scene consistency firewall active.

    report = {
        "enabled": True,
        "status": "OK",
        "algorithm": "PHASE9_ATTACHMENT_BOUNDARY_V3_15",
        "architecture_phase": "PHASE9_ATTACHMENT_BOUNDARY_V3_15",
        "phase1": phase1.report(),
        "phase2": phase2_report,
        "phase3": phase3_report,
        "phase6": phase6_report,
        "effective_phase3": effective_phase3_report,
        "phase5": phase5_report,
        "phase7": phase7_report,
        "phase8": phase8_report,
        "phase9": phase9_report,
        "phase10": phase10_report,
        "phase4": phase4_report,
        "phase4_reassert_repairs": int(phase4_repaired),
        "phase1_baseline_intact": phase1_baseline_intact,
        "cl_prior_active": bool(cl_active),
        "cl_prior_points": int(cl_mask.sum()) if cl_mask is not None else 0,
        "wire_candidates": int(wire_candidates),
        "wire_support_final": int(wire_support_idx.size),
        "wire_profile": wire_profile_report,
        "wires_final": int(wire_write_idx.size),
        "pole_support_final": int(pole_support_final.size),
        "anchor_rescue_support": int(anchor_rescue_idx.size),
        "anchor_candidates": int(len(anchor_diagnostics)),
        "phase6_fallback_used": bool(phase6_report.get("fallback_accepted", False)),
        "phase6_recovered_wire": int(phase6_report.get("recovered_active_wire_points", 0)),
        "phase5_support": int(phase5_idx.size),
        "phase5_accepted_supports": int(phase5_report.get("accepted_supports", 0)),
        "phase7_rejected_pole_leakage": int(phase7_report.get("rejected_pole_context_points", 0)),
        "phase7_validated_pole_context": int(phase7_report.get("validated_pole_context_points", 0)),
        "phase7_status": str(phase7_report.get("status", "UNKNOWN")),
        "phase8_status": str(phase8_report.get("status", "UNKNOWN")),
        "phase8_scenario": str(phase8_report.get("scenario", "UNKNOWN")),
        "phase8_orphan_wire_rejected": int(phase8_report.get("orphan_wire_rejected", 0)),
        "phase8_orphan_pole_rejected": int(phase8_report.get("orphan_pole_rejected", 0)),
        "phase8_boundary_clipped_supports": int(phase8_report.get("boundary_clipped_supports", 0)),
        "phase9_status": str(phase9_report.get("status", "UNKNOWN")),
        "phase9_input_overlap": int(phase9_report.get("input_overlap_points", 0)),
        "phase9_wire_reclaimed": int(phase9_report.get("wire_reclaimed_from_pole", 0)),
        "phase9_attachment_overlap": int(phase9_report.get("attachment_overlap_retained", 0)),
        "phase10_added_active_wire": int(phase10_report.get("added_active_wire_points", 0)),
        "phase10_final_active_wire": int(phase10_report.get("final_active_wire_points", 0)),
        "poles_final": int(pole_write_idx.size),
    }

    _log(log, "info", "")
    _log(log, "info", "  POWER ASSET POST-PASS - PHASE 10 DENSIFIED / PHASE 9 RESOLVED / PHASE 8 CONSISTENT / PHASE 4 PROTECTED")
    _log(log, "info", f"    CL prior active       : {bool(cl_active)}")
    _log(log, "info", f"    Wire candidates       : {wire_candidates:,}")
    _log(log, "info", f"    Wire profile mode     : {wire_profile_report.get('mode', 'NOT_USED')} | tracks={wire_profile_report.get('accepted_tracks', 0)}/{wire_profile_report.get('tracks', 0)} | kept={wire_profile_report.get('kept', wire_profile_report.get('fallback_kept', 0))} | phase6={wire_profile_report.get('phase6_fallback', False)}")
    _log(log, "info", f"    Wire support final    : {wire_support_idx.size:,}")
    _log(log, "info", f"    Wire/Bare conductor   : {wire_write_idx.size:,}")
    _log(log, "info", f"    Pole/Pylon support    : {pole_support_final.size:,}")
    _log(log, "info", f"    Anchor rescue support : {anchor_rescue_idx.size:,}")
    _log(log, "info", f"    Phase6 recovery used  : {bool(phase6_report.get('fallback_accepted', False))}")
    _log(log, "info", f"    Phase6 recovered Wire : {int(phase6_report.get('recovered_active_wire_points', 0)):,}")
    _log(log, "info", f"    Phase5 support rescue : {phase5_idx.size:,}")
    _log(log, "info", f"    Phase7 validation     : {phase7_report.get('status', 'UNKNOWN')}")
    _log(log, "info", f"    Phase7 pole rejected  : {int(phase7_report.get('rejected_pole_context_points', 0)):,}")
    _log(log, "info", f"    Phase8 consistency    : {phase8_report.get('status', 'UNKNOWN')} | {phase8_report.get('scenario', 'UNKNOWN')}")
    _log(log, "info", f"    Phase8 orphan rejected: wire={int(phase8_report.get('orphan_wire_rejected', 0)):,} pole={int(phase8_report.get('orphan_pole_rejected', 0)):,}")
    _log(log, "info", f"    Phase9 attachment     : {phase9_report.get('status', 'UNKNOWN')} | reclaimed_wire={int(phase9_report.get('wire_reclaimed_from_pole', 0)):,} retained_attachment={int(phase9_report.get('attachment_overlap_retained', 0)):,}")
    _log(log, "info", f"    Phase10 densification : {phase10_report.get('status', 'UNKNOWN')} | added_active={int(phase10_report.get('added_active_wire_points', 0)):,} final_active={int(phase10_report.get('final_active_wire_points', 0)):,}")
    _log(log, "info", f"    Poles/Pylons written  : {pole_write_idx.size:,}")

    return cls, report

