from __future__ import annotations

import argparse
import json
from pathlib import Path

import laspy
import numpy as np
from scipy.spatial import cKDTree
from sklearn.cluster import DBSCAN

from lidar_vehicle_detector import ELIGIBLE_CLASSES, score_component


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--classified", required=True)
    ap.add_argument("--restrict-indices", required=True)
    ap.add_argument("--output-indices", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--mode", choices=["strict", "balanced"], default="strict")
    ap.add_argument("--eps", type=float, default=0.60)
    ap.add_argument("--min-samples", type=int, default=6)
    args = ap.parse_args()

    classified_path = Path(args.classified)
    restrict_path = Path(args.restrict_indices)
    output_path = Path(args.output_indices)
    report_path = Path(args.report)

    restrict = np.asarray(np.load(restrict_path), dtype=np.int64).ravel()
    restrict = np.unique(restrict)

    las = laspy.read(str(classified_path))
    x = np.asarray(las.x, dtype=np.float64)
    y = np.asarray(las.y, dtype=np.float64)
    z = np.asarray(las.z, dtype=np.float64)
    cls = np.asarray(las.classification, dtype=np.uint8)

    n = len(x)
    if restrict.size:
        restrict = restrict[(restrict >= 0) & (restrict < n)]

    ground_idx = np.flatnonzero(cls == 2)
    eligible_idx = np.flatnonzero(np.isin(cls, ELIGIBLE_CLASSES))

    report = {
        "enabled": True,
        "engine": "Naksha LiDAR-only strict vehicle geometry",
        "mode": args.mode,
        "classified_path": str(classified_path),
        "input_points": int(n),
        "restrict_points": int(len(restrict)),
        "ground_points": int(len(ground_idx)),
        "eligible_points": int(len(eligible_idx)),
        "raw_clusters": 0,
        "accepted_vehicle_clusters": 0,
        "vehicle_points_detected": 0,
        "vehicle_points_in_fence": 0,
        "vehicle_points_applied": 0,
        "status": "STARTED",
        "candidates": [],
    }

    if len(ground_idx) < 20 or len(eligible_idx) == 0 or len(restrict) == 0:
        np.save(output_path, np.empty(0, dtype=np.int64))
        report["status"] = "NO_SAFE_INPUT"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
        return

    ground_xy = np.column_stack((x[ground_idx], y[ground_idx]))
    ground_z = z[ground_idx]
    tree = cKDTree(ground_xy)

    e_xy = np.column_stack((x[eligible_idx], y[eligible_idx]))
    dist, nn = tree.query(e_xy, k=min(4, len(ground_idx)), workers=-1)

    if np.ndim(nn) == 1:
        local_ground = ground_z[nn]
        near_dist = np.asarray(dist, dtype=np.float64)
    else:
        local_ground = np.median(ground_z[nn], axis=1)
        near_dist = np.asarray(dist[:, 0], dtype=np.float64)

    hag = z[eligible_idx] - local_ground

    pre = (
        (near_dist <= 3.0)
        & (hag >= 0.35)
        & (hag <= 4.8)
    )

    pre_local = np.flatnonzero(pre)
    pre_global = eligible_idx[pre_local]
    report["prefilter_points"] = int(len(pre_global))

    accepted = []
    accepted_parts = []

    if len(pre_global):
        pxy = np.column_stack((x[pre_global], y[pre_global]))
        labels = DBSCAN(
            eps=float(args.eps),
            min_samples=int(args.min_samples),
            algorithm="kd_tree",
            n_jobs=-1,
        ).fit_predict(pxy)

        unique = [int(v) for v in np.unique(labels) if v >= 0]
        report["raw_clusters"] = len(unique)
        threshold = 0.78 if args.mode == "strict" else 0.66

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
                accepted.append({
                    "cluster_id": lab,
                    "points": int(len(gidx)),
                    **metrics,
                })

    if accepted_parts:
        detected = np.unique(np.concatenate(accepted_parts)).astype(np.int64)
    else:
        detected = np.empty(0, dtype=np.int64)

    # Fence restriction is applied by exact local indices.
    in_fence = np.intersect1d(detected, restrict, assume_unique=False)

    # Absolute safety: output must not contain LAS2 points.
    if len(in_fence) and np.any(cls[in_fence] == 2):
        raise RuntimeError("SAFETY ASSERTION FAILED: LAS2 Ground entered vehicle output.")

    # Absolute safety: output may only originate from Premium classes 3/4/5/6.
    if len(in_fence) and not np.all(np.isin(cls[in_fence], ELIGIBLE_CLASSES)):
        raise RuntimeError("SAFETY ASSERTION FAILED: non-eligible class entered vehicle output.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    np.save(output_path, in_fence)

    report["accepted_vehicle_clusters"] = len(accepted)
    report["vehicle_points_detected"] = int(len(detected))
    report["vehicle_points_in_fence"] = int(len(in_fence))
    report["status"] = "DETECTED" if len(in_fence) else "NO_VEHICLE"
    report["candidates"] = sorted(accepted, key=lambda c: c["score"], reverse=True)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps({
        "status": report["status"],
        "raw_clusters": report["raw_clusters"],
        "accepted_vehicle_clusters": report["accepted_vehicle_clusters"],
        "vehicle_points_detected": report["vehicle_points_detected"],
        "vehicle_points_in_fence": report["vehicle_points_in_fence"],
    }))


if __name__ == "__main__":
    main()
