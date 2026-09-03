from __future__ import annotations

import argparse
import json
import math
import os
import uuid
from pathlib import Path

import laspy
import numpy as np
from scipy.spatial import cKDTree
from sklearn.cluster import DBSCAN

from lidar_vehicle_detector import ELIGIBLE_CLASSES, score_component


PROTECTED_GROUND = np.uint8(2)
WORK_CLASSES = np.array([2, 3, 4, 5, 6], dtype=np.uint8)


def world_to_raw_bounds(lo, hi, scale, offset):
    raw_lo = math.floor((lo - offset) / scale)
    raw_hi = math.ceil((hi - offset) / scale)
    return raw_lo, raw_hi


def hard_vehicle_ok(metrics) -> bool:
    L = metrics["length_m"]
    W = metrics["width_m"]
    H90 = metrics["hag_p90_m"]
    area = metrics["footprint_area_m2"]
    rough = metrics["plane_mad_m"]
    rect = metrics["rectangularity"]

    return (
        1.6 <= L <= 16.0
        and 0.9 <= W <= 3.7
        and 0.35 <= H90 <= 4.8
        and 1.5 <= area <= 55.0
        and rough <= 0.65
        and rect >= 0.22
    )


def process_core(
    *,
    X, Y, Z, cls,
    scales, offsets,
    core,
    overlap,
    mode,
    eps,
    min_samples,
    max_prefilter,
    min_core_size,
    depth=0,
):
    x0, x1, y0, y1 = core
    sx, sy, sz = scales
    ox, oy, oz = offsets

    cx0, cx1 = x0 - overlap, x1 + overlap
    cy0, cy1 = y0 - overlap, y1 + overlap

    rx0, rx1 = world_to_raw_bounds(cx0, cx1, sx, ox)
    ry0, ry1 = world_to_raw_bounds(cy0, cy1, sy, oy)

    work_mask = (
        (X >= rx0) & (X <= rx1)
        & (Y >= ry0) & (Y <= ry1)
        & np.isin(cls, WORK_CLASSES)
    )
    context_global = np.flatnonzero(work_mask)

    result = {
        "candidates": [],
        "candidate_indices": np.empty(0, dtype=np.int64),
        "raw_clusters": 0,
        "prefilter_points": 0,
        "subtiles": 0,
    }

    if context_global.size == 0:
        return result

    ccls = cls[context_global]
    ground_local = np.flatnonzero(ccls == 2)
    eligible_local = np.flatnonzero(np.isin(ccls, ELIGIBLE_CLASSES))

    if ground_local.size < 20 or eligible_local.size == 0:
        return result

    # Convert only the current context to world coordinates.
    cx = X[context_global].astype(np.float64) * sx + ox
    cy = Y[context_global].astype(np.float64) * sy + oy
    cz = Z[context_global].astype(np.float64) * sz + oz

    ground_xy = np.column_stack((cx[ground_local], cy[ground_local]))
    ground_z = cz[ground_local]
    tree = cKDTree(ground_xy)

    e_xy = np.column_stack((cx[eligible_local], cy[eligible_local]))
    dist, nn = tree.query(e_xy, k=min(4, len(ground_local)), workers=-1)

    if np.ndim(nn) == 1:
        local_ground = ground_z[nn]
        near_dist = np.asarray(dist, dtype=np.float64)
    else:
        local_ground = np.median(ground_z[nn], axis=1)
        near_dist = np.asarray(dist[:, 0], dtype=np.float64)

    hag = cz[eligible_local] - local_ground

    pre = (
        (near_dist <= 3.0)
        & (hag >= 0.35)
        & (hag <= 4.8)
    )

    pre_local = np.flatnonzero(pre)
    result["prefilter_points"] = int(len(pre_local))

    # Dense low-vegetation areas can make DBSCAN expensive.
    # Split the core recursively before clustering when needed.
    core_w = x1 - x0
    core_h = y1 - y0
    if (
        len(pre_local) > max_prefilter
        and max(core_w, core_h) > min_core_size
        and depth < 4
    ):
        mx = (x0 + x1) / 2.0
        my = (y0 + y1) / 2.0
        subcores = [
            (x0, mx, y0, my),
            (mx, x1, y0, my),
            (x0, mx, my, y1),
            (mx, x1, my, y1),
        ]

        parts = []
        candidates = []
        raw_clusters = 0
        prefilter_points = 0
        subtiles = 0

        for sub in subcores:
            subres = process_core(
                X=X, Y=Y, Z=Z, cls=cls,
                scales=scales, offsets=offsets,
                core=sub,
                overlap=overlap,
                mode=mode,
                eps=eps,
                min_samples=min_samples,
                max_prefilter=max_prefilter,
                min_core_size=min_core_size,
                depth=depth + 1,
            )
            if len(subres["candidate_indices"]):
                parts.append(subres["candidate_indices"])
            candidates.extend(subres["candidates"])
            raw_clusters += subres["raw_clusters"]
            prefilter_points += subres["prefilter_points"]
            subtiles += 1 + subres["subtiles"]

        result["candidate_indices"] = (
            np.unique(np.concatenate(parts)).astype(np.int64)
            if parts else np.empty(0, dtype=np.int64)
        )
        result["candidates"] = candidates
        result["raw_clusters"] = raw_clusters
        result["prefilter_points"] = prefilter_points
        result["subtiles"] = subtiles
        return result

    if len(pre_local) == 0:
        return result

    pre_context_local = eligible_local[pre_local]
    pre_global = context_global[pre_context_local]
    pxy = np.column_stack((cx[pre_context_local], cy[pre_context_local]))

    labels = DBSCAN(
        eps=float(eps),
        min_samples=int(min_samples),
        algorithm="kd_tree",
        n_jobs=-1,
    ).fit_predict(pxy)

    unique = [int(v) for v in np.unique(labels) if v >= 0]
    result["raw_clusters"] = len(unique)
    threshold = 0.78 if mode == "strict" else 0.66

    accepted_parts = []
    accepted = []

    pre_hag = hag[pre_local]
    pre_gdist = near_dist[pre_local]

    for lab in unique:
        loc = np.flatnonzero(labels == lab)
        if len(loc) < max(min_samples, 8):
            continue

        cluster_global = pre_global[loc]
        xyz = np.column_stack((
            X[cluster_global].astype(np.float64) * sx + ox,
            Y[cluster_global].astype(np.float64) * sy + oy,
            Z[cluster_global].astype(np.float64) * sz + oz,
        ))

        score, metrics = score_component(
            xyz,
            pre_hag[loc],
            pre_gdist[loc],
        )

        # Assign an overlapping cluster to one core based on its center.
        center_in_core = (
            x0 <= metrics["center_x"] < x1
            and y0 <= metrics["center_y"] < y1
        )

        if center_in_core and hard_vehicle_ok(metrics) and score >= threshold:
            accepted_parts.append(cluster_global)
            accepted.append({
                "tile_core": [float(x0), float(x1), float(y0), float(y1)],
                "cluster_id": int(lab),
                "points": int(len(cluster_global)),
                **metrics,
            })

    result["candidate_indices"] = (
        np.unique(np.concatenate(accepted_parts)).astype(np.int64)
        if accepted_parts else np.empty(0, dtype=np.int64)
    )
    result["candidates"] = accepted
    return result


def write_transactional_class0(classified_path: Path, candidate_idx: np.ndarray):
    candidate_idx = np.unique(np.asarray(candidate_idx, dtype=np.int64))
    tmp = classified_path.with_name(
        classified_path.stem
        + f".vehicle_full_tmp_{uuid.uuid4().hex[:8]}"
        + classified_path.suffix
    )

    changed = 0
    start = 0

    try:
        with laspy.open(str(classified_path), mode="r") as src:
            header = src.header.copy()
            with laspy.open(
                str(tmp),
                mode="w",
                header=header,
                do_compress=(classified_path.suffix.lower() == ".laz"),
            ) as dst:
                for points in src.chunk_iterator(1_000_000):
                    count = len(points)
                    end = start + count
                    cls = np.asarray(points.classification, dtype=np.uint8).copy()

                    lo = int(np.searchsorted(candidate_idx, start, side="left"))
                    hi = int(np.searchsorted(candidate_idx, end, side="left"))

                    if hi > lo:
                        local = candidate_idx[lo:hi] - start
                        before = cls[local]

                        if np.any(before == 2):
                            raise RuntimeError(
                                "SAFETY ASSERTION FAILED: LAS2 Ground entered full-file vehicle write-back."
                            )
                        if not np.all(np.isin(before, ELIGIBLE_CLASSES)):
                            bad = np.unique(before[~np.isin(before, ELIGIBLE_CLASSES)])
                            raise RuntimeError(
                                "SAFETY ASSERTION FAILED: non-eligible classes entered vehicle write-back: "
                                + str(bad.tolist())
                            )

                        cls[local] = np.uint8(0)
                        changed += int(len(local))

                    points.classification = cls
                    dst.write_points(points)
                    start = end

        # Atomic replacement of generated Premium output only.
        os.replace(str(tmp), str(classified_path))
        return changed

    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--classified", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--mode", choices=["strict", "balanced"], default="strict")
    ap.add_argument("--tile-size", type=float, default=80.0)
    ap.add_argument("--overlap", type=float, default=20.0)
    ap.add_argument("--eps", type=float, default=0.60)
    ap.add_argument("--min-samples", type=int, default=6)
    ap.add_argument("--max-prefilter", type=int, default=250_000)
    ap.add_argument("--min-core-size", type=float, default=20.0)
    args = ap.parse_args()

    path = Path(args.classified)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    with laspy.open(str(path), mode="r") as reader:
        header = reader.header
        n = int(header.point_count)
        scales = tuple(float(v) for v in header.scales)
        offsets = tuple(float(v) for v in header.offsets)
        mins = tuple(float(v) for v in header.mins)
        maxs = tuple(float(v) for v in header.maxs)

    # Compact full-cloud representation: raw integer XYZ + 1-byte class.
    X = np.empty(n, dtype=np.int32)
    Y = np.empty(n, dtype=np.int32)
    Z = np.empty(n, dtype=np.int32)
    cls = np.empty(n, dtype=np.uint8)

    pos = 0
    with laspy.open(str(path), mode="r") as reader:
        for points in reader.chunk_iterator(1_000_000):
            count = len(points)
            X[pos:pos+count] = np.asarray(points.X, dtype=np.int32)
            Y[pos:pos+count] = np.asarray(points.Y, dtype=np.int32)
            Z[pos:pos+count] = np.asarray(points.Z, dtype=np.int32)
            cls[pos:pos+count] = np.asarray(points.classification, dtype=np.uint8)
            pos += count

    if pos != n:
        raise RuntimeError(f"Full-file input read mismatch: {pos}/{n}")

    eligible_count = int(np.count_nonzero(np.isin(cls, ELIGIBLE_CLASSES)))
    ground_count = int(np.count_nonzero(cls == 2))

    if ground_count < 20 or eligible_count == 0:
        report = {
            "enabled": True,
            "engine": "Naksha LiDAR-only tiled full-file vehicle geometry",
            "status": "NO_SAFE_INPUT",
            "total_points": n,
            "ground_points": ground_count,
            "eligible_points": eligible_count,
            "vehicle_points_detected": 0,
            "vehicle_points_applied": 0,
            "protected_las2_changed": 0,
        }
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
        return

    min_x, min_y = mins[0], mins[1]
    max_x, max_y = maxs[0], maxs[1]
    tile = max(float(args.tile_size), 20.0)
    overlap = max(float(args.overlap), 16.0)

    nx = max(1, int(math.ceil((max_x - min_x) / tile)))
    ny = max(1, int(math.ceil((max_y - min_y) / tile)))

    all_parts = []
    candidates = []
    total_raw_clusters = 0
    total_prefilter = 0
    subtiles = 0
    tiles_processed = 0

    for iy in range(ny):
        y0 = min_y + iy * tile
        y1 = max_y + 1e-9 if iy == ny - 1 else min(y0 + tile, max_y)

        for ix in range(nx):
            x0 = min_x + ix * tile
            x1 = max_x + 1e-9 if ix == nx - 1 else min(x0 + tile, max_x)

            res = process_core(
                X=X, Y=Y, Z=Z, cls=cls,
                scales=scales, offsets=offsets,
                core=(x0, x1, y0, y1),
                overlap=overlap,
                mode=args.mode,
                eps=args.eps,
                min_samples=args.min_samples,
                max_prefilter=args.max_prefilter,
                min_core_size=args.min_core_size,
            )

            tiles_processed += 1
            total_raw_clusters += res["raw_clusters"]
            total_prefilter += res["prefilter_points"]
            subtiles += res["subtiles"]
            candidates.extend(res["candidates"])

            if len(res["candidate_indices"]):
                all_parts.append(res["candidate_indices"])

            print(
                f"[Vehicle FULL] tile {tiles_processed}/{nx*ny} "
                f"accepted={len(res['candidates'])} "
                f"candidate_points={len(res['candidate_indices'])}",
                flush=True,
            )

    detected = (
        np.unique(np.concatenate(all_parts)).astype(np.int64)
        if all_parts else np.empty(0, dtype=np.int64)
    )

    # Final pre-write safety against the untouched in-memory classification snapshot.
    if len(detected):
        if np.any(cls[detected] == 2):
            raise RuntimeError(
                "SAFETY ASSERTION FAILED: LAS2 Ground entered full-file vehicle candidates."
            )
        if not np.all(np.isin(cls[detected], ELIGIBLE_CLASSES)):
            raise RuntimeError(
                "SAFETY ASSERTION FAILED: non-eligible class entered full-file vehicle candidates."
            )

    changed = write_transactional_class0(path, detected) if len(detected) else 0

    report = {
        "enabled": True,
        "engine": "Naksha LiDAR-only tiled full-file vehicle geometry",
        "status": "APPLIED_CLASS0" if changed else "NO_VEHICLE",
        "mode": args.mode,
        "classified_path": str(path),
        "total_points": int(n),
        "ground_points": ground_count,
        "eligible_points": eligible_count,
        "tile_size_m": tile,
        "overlap_m": overlap,
        "base_tiles": int(nx * ny),
        "tiles_processed": int(tiles_processed),
        "recursive_subtiles": int(subtiles),
        "raw_clusters": int(total_raw_clusters),
        "prefilter_points_accumulated": int(total_prefilter),
        "accepted_vehicle_clusters": int(len(candidates)),
        "vehicle_points_detected": int(len(detected)),
        "vehicle_points_applied": int(changed),
        "protected_las2_changed": 0,
        "candidates": sorted(candidates, key=lambda c: c["score"], reverse=True),
    }

    try:
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[Vehicle FULL] report write warning: {exc}", flush=True)

    print(json.dumps({
        "status": report["status"],
        "tiles_processed": report["tiles_processed"],
        "accepted_vehicle_clusters": report["accepted_vehicle_clusters"],
        "vehicle_points_detected": report["vehicle_points_detected"],
        "vehicle_points_applied": report["vehicle_points_applied"],
        "protected_las2_changed": 0,
    }), flush=True)


if __name__ == "__main__":
    main()
