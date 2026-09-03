from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import laspy
import numpy as np
from scipy.spatial import cKDTree, ConvexHull
from sklearn.cluster import DBSCAN


ELIGIBLE_CLASSES = np.array([3, 4, 5, 6], dtype=np.uint8)


def robust_obb(xy: np.ndarray):
    center = np.median(xy, axis=0)
    q = xy - center
    cov = np.cov(q.T)
    vals, vecs = np.linalg.eigh(cov)
    order = np.argsort(vals)[::-1]
    axes = vecs[:, order]
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

    return {
        "center_x": float(world_center[0]),
        "center_y": float(world_center[1]),
        "length": length,
        "width": width,
        "yaw_rad": yaw,
        "axes": axes,
        "proj_lo": lo,
        "proj_hi": hi,
        "origin": center,
    }


def hull_rectangularity(xy: np.ndarray, box_area: float) -> float:
    if len(xy) < 4 or box_area <= 1e-9:
        return 0.0
    try:
        hull = ConvexHull(xy)
        # For a 2-D ConvexHull, `volume` is polygon area.
        area = float(hull.volume)
        return float(np.clip(area / box_area, 0.0, 1.0))
    except Exception:
        return 0.0


def plane_residual(xyz: np.ndarray) -> float:
    if len(xyz) < 6:
        return 999.0
    x0 = np.median(xyz[:, 0])
    y0 = np.median(xyz[:, 1])
    A = np.column_stack(
        (xyz[:, 0] - x0, xyz[:, 1] - y0, np.ones(len(xyz)))
    )
    try:
        coef, *_ = np.linalg.lstsq(A, xyz[:, 2], rcond=None)
        residual = xyz[:, 2] - A @ coef
        return float(np.median(np.abs(residual - np.median(residual))))
    except Exception:
        return 999.0


def interval_score(v, ideal_lo, ideal_hi, hard_lo, hard_hi):
    if v < hard_lo or v > hard_hi:
        return 0.0
    if ideal_lo <= v <= ideal_hi:
        return 1.0
    if v < ideal_lo:
        return max(0.0, (v - hard_lo) / max(ideal_lo - hard_lo, 1e-9))
    return max(0.0, (hard_hi - v) / max(hard_hi - ideal_hi, 1e-9))


def score_component(xyz, hag, ground_dist):
    box = robust_obb(xyz[:, :2])
    length = max(box["length"], box["width"])
    width = min(box["length"], box["width"])
    area = max(length * width, 1e-6)
    aspect = length / max(width, 1e-6)

    h10, h50, h90, h95 = np.percentile(hag, [10, 50, 90, 95])
    z_span = float(np.percentile(xyz[:, 2], 95) - np.percentile(xyz[:, 2], 5))
    rough = plane_residual(xyz)
    rect = hull_rectangularity(xyz[:, :2], area)
    gdist = float(np.median(ground_dist))
    density = float(len(xyz) / area)

    # Two families: passenger/light vehicle and long vehicle.
    car_dim = (
        0.45 * interval_score(length, 2.2, 6.2, 1.6, 8.0)
        + 0.35 * interval_score(width, 1.3, 2.7, 0.9, 3.4)
        + 0.20 * interval_score(aspect, 1.25, 4.5, 1.0, 6.5)
    )
    long_dim = (
        0.50 * interval_score(length, 5.0, 13.5, 3.5, 16.0)
        + 0.30 * interval_score(width, 1.7, 3.2, 1.2, 3.7)
        + 0.20 * interval_score(aspect, 1.6, 6.0, 1.1, 8.0)
    )
    dim_score = max(car_dim, long_dim)

    height_score = interval_score(h90, 0.75, 3.8, 0.35, 4.8)
    area_score = interval_score(area, 3.0, 35.0, 1.5, 55.0)
    rough_score = interval_score(rough, 0.0, 0.22, 0.0, 0.65)
    rect_score = interval_score(rect, 0.50, 1.0, 0.22, 1.0)
    ground_score = interval_score(gdist, 0.0, 1.2, 0.0, 3.0)
    density_score = interval_score(density, 1.2, 200.0, 0.35, 400.0)
    span_score = interval_score(z_span, 0.10, 2.5, 0.05, 4.0)

    score = (
        0.24 * dim_score
        + 0.16 * height_score
        + 0.10 * area_score
        + 0.18 * rough_score
        + 0.14 * rect_score
        + 0.08 * ground_score
        + 0.05 * density_score
        + 0.05 * span_score
    )

    metrics = {
        "length_m": float(length),
        "width_m": float(width),
        "footprint_area_m2": float(area),
        "aspect_ratio": float(aspect),
        "hag_p10_m": float(h10),
        "hag_median_m": float(h50),
        "hag_p90_m": float(h90),
        "hag_p95_m": float(h95),
        "z_span_p95_p05_m": z_span,
        "plane_mad_m": float(rough),
        "rectangularity": float(rect),
        "median_ground_distance_m": gdist,
        "point_density_per_m2": density,
        "score": float(score),
        "center_x": box["center_x"],
        "center_y": box["center_y"],
        "yaw_rad": box["yaw_rad"],
    }
    return float(score), metrics


def write_candidates_only(las, candidate_idx, out_path: Path, debug_class: int):
    hdr = las.header.copy()
    out = laspy.LasData(hdr)

    if len(candidate_idx) == 0:
        # Create an empty point record compatible with the source format.
        out.points = las.points[:0].copy()
    else:
        out.points = las.points[candidate_idx].copy()
        out.classification = np.full(len(candidate_idx), debug_class, dtype=np.uint8)

    out.write(str(out_path))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--mode", choices=["strict", "balanced"], default="strict")
    ap.add_argument("--debug-class", type=int, default=64)
    ap.add_argument("--eps", type=float, default=0.60)
    ap.add_argument("--min-samples", type=int, default=6)
    args = ap.parse_args()

    src = Path(args.input)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("============================================================")
    print("NAKSHA LIDAR-ONLY VEHICLE CANDIDATE DETECTOR")
    print("DRY RUN: source classifications will NOT be modified")
    print("============================================================")
    print("Input:", src)

    las = laspy.read(str(src))
    x = np.asarray(las.x, dtype=np.float64)
    y = np.asarray(las.y, dtype=np.float64)
    z = np.asarray(las.z, dtype=np.float64)
    cls = np.asarray(las.classification, dtype=np.uint8)

    n = len(x)
    print("Points:", f"{n:,}")

    ground_idx = np.flatnonzero(cls == 2)
    eligible_idx = np.flatnonzero(np.isin(cls, ELIGIBLE_CLASSES))

    print("Ground LAS2:", f"{len(ground_idx):,}")
    print("Eligible LAS3/4/5/6:", f"{len(eligible_idx):,}")

    if len(ground_idx) < 20:
        raise RuntimeError("Not enough LAS2 Ground points for safe local-ground normalization.")

    if len(eligible_idx) == 0:
        raise RuntimeError("No eligible LAS3/4/5/6 points found.")

    ground_xy = np.column_stack((x[ground_idx], y[ground_idx]))
    ground_z = z[ground_idx]
    tree = cKDTree(ground_xy)

    e_xy = np.column_stack((x[eligible_idx], y[eligible_idx]))
    # k=4 helps smooth isolated ground noise.
    dist, nn = tree.query(e_xy, k=min(4, len(ground_idx)), workers=-1)

    if np.ndim(nn) == 1:
        local_ground = ground_z[nn]
        near_dist = np.asarray(dist, dtype=np.float64)
    else:
        local_ground = np.median(ground_z[nn], axis=1)
        near_dist = np.asarray(dist[:, 0], dtype=np.float64)

    hag = z[eligible_idx] - local_ground

    # Conservative prefilter:
    # - close to an actual LAS2 ground support
    # - above ground, but not building/tree-top scale
    pre = (
        (near_dist <= 3.0)
        & (hag >= 0.35)
        & (hag <= 4.8)
    )

    pre_local = np.flatnonzero(pre)
    pre_global = eligible_idx[pre_local]

    print("Vehicle-height/ground-supported prefilter:", f"{len(pre_global):,}")

    if len(pre_global) == 0:
        candidate_idx = np.empty(0, dtype=np.int64)
        candidates = []
    else:
        pxy = np.column_stack((x[pre_global], y[pre_global]))
        print("Clustering with DBSCAN...")
        labels = DBSCAN(
            eps=float(args.eps),
            min_samples=int(args.min_samples),
            algorithm="kd_tree",
            n_jobs=-1,
        ).fit_predict(pxy)

        unique = [int(v) for v in np.unique(labels) if v >= 0]
        print("Raw clusters:", len(unique))

        threshold = 0.78 if args.mode == "strict" else 0.66

        candidates = []
        accepted_parts = []

        pre_hag = hag[pre_local]
        pre_gdist = near_dist[pre_local]

        for lab in unique:
            loc = np.flatnonzero(labels == lab)
            if len(loc) < max(args.min_samples, 8):
                continue

            gidx = pre_global[loc]
            xyz = np.column_stack((x[gidx], y[gidx], z[gidx]))
            chag = pre_hag[loc]
            cgdist = pre_gdist[loc]

            score, metrics = score_component(xyz, chag, cgdist)

            # Hard safety gates before score is allowed to pass.
            L = metrics["length_m"]
            W = metrics["width_m"]
            H90 = metrics["hag_p90_m"]
            area = metrics["footprint_area_m2"]
            rough = metrics["plane_mad_m"]
            rect = metrics["rectangularity"]

            hard_ok = (
                1.6 <= L <= 16.0
                and 0.9 <= W <= 3.7
                and 0.35 <= H90 <= 4.8
                and 1.5 <= area <= 55.0
                and rough <= 0.65
                and rect >= 0.22
            )

            if hard_ok and score >= threshold:
                accepted_parts.append(gidx)
                candidates.append({
                    "cluster_id": lab,
                    "points": int(len(gidx)),
                    **metrics,
                })

        if accepted_parts:
            candidate_idx = np.unique(np.concatenate(accepted_parts)).astype(np.int64)
        else:
            candidate_idx = np.empty(0, dtype=np.int64)

    # Absolute protection assertion: no LAS2 Ground may be in candidates.
    if len(candidate_idx):
        if np.any(cls[candidate_idx] == 2):
            raise RuntimeError("SAFETY ASSERTION FAILED: LAS2 Ground entered vehicle candidates.")

    stem = src.stem
    json_path = out_dir / f"{stem}_vehicle_candidates.json"
    laz_path = out_dir / f"{stem}_vehicle_candidates_only.laz"

    report = {
        "source": str(src),
        "mode": args.mode,
        "dry_run": True,
        "source_modified": False,
        "eligible_classes": [3, 4, 5, 6],
        "protected_classes": [2],
        "debug_output_class": int(args.debug_class),
        "total_points": int(n),
        "ground_points": int(len(ground_idx)),
        "eligible_points": int(len(eligible_idx)),
        "prefilter_points": int(len(pre_global)),
        "accepted_vehicle_clusters": int(len(candidates)),
        "candidate_points": int(len(candidate_idx)),
        "candidates": sorted(candidates, key=lambda c: c["score"], reverse=True),
    }
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    write_candidates_only(las, candidate_idx, laz_path, args.debug_class)

    print("")
    print("Accepted vehicle clusters:", len(candidates))
    print("Candidate points:", f"{len(candidate_idx):,}")
    print("JSON:", json_path)
    print("Candidate-only LAZ:", laz_path)
    print("")
    print("LIDAR_VEHICLE_DRY_RUN_PASS")
    print("SOURCE MODIFIED: NO")
    print("LAS2 GROUND CHANGED: NO")


if __name__ == "__main__":
    main()
