#!/usr/bin/env python3
"""
extract_3file_hybrid_v2_68f.py
==============================

Second controlled 3-file diagnostic for the accepted 68-feature PointNet++ model.

Locked from diagnostic V1:
- exact 68-feature names and order
- global XYZ float64
- tile-local X/Y float32
- absolute Z feature
- intensity = raw uint16 / 65535
- classes 2..6 -> model labels 0..4
- all physical points retained as geometry context
- 60 m tile and 10 m halo
- roughness = Z standard deviation of raw K=10 neighbours
- label-independent lowest-grid HAG

Corrections in V2:
- density radius changed from 1.0 m to 0.5 m
- nearest-K256 PCA removed
- r0.5 and r1.0 use all raw true-radius neighbours
- r2.0 uses all 0.15 m 3-D voxel centroids inside the true radius
- r5.0 and r10.0 use all 0.30 m 3-D voxel centroids inside the true radius
- geometry is computed separately at each query point
- source points within the outer 10 m file border are skipped by default

This is diagnostic-only. It never auto-approves the full 200-file extraction.
"""

from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import json
import math
import os
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import h5py
import laspy
import numpy as np
from scipy.spatial import cKDTree
from tqdm import tqdm


NUM_FEATURES = 68
NUM_CLASSES = 5
CLASS_NAMES = ["Ground", "LowVeg", "MediumVeg", "HighVeg", "Building"]

SOURCE_TO_MODEL = {
    2: 0,
    3: 1,
    4: 2,
    5: 3,
    6: 4,
}

FEATURE_SCALES = [0.5, 1.0, 2.0, 5.0, 10.0]

BASE_FEATURE_NAMES = [
    "x",
    "y",
    "z",
    "hag",
    "intensity",
    "return_number",
    "num_returns",
    "return_ratio",
    "is_single_return",
    "is_first_return",
    "is_last_return",
    "density",
    "roughness",
]

GEOMETRY_NAMES = [
    "eigenvalue1",
    "eigenvalue2",
    "eigenvalue3",
    "linearity",
    "planarity",
    "sphericity",
    "omnivariance",
    "anisotropy",
    "eigenentropy",
    "surface_variation",
    "verticality",
]

FEATURE_NAMES = BASE_FEATURE_NAMES + [
    f"{name}_r{radius:.1f}"
    for radius in FEATURE_SCALES
    for name in GEOMETRY_NAMES
]

if len(FEATURE_NAMES) != NUM_FEATURES:
    raise RuntimeError("The accepted feature contract must contain exactly 68 columns.")

DEFAULT_FILES = [
    "600000_5668500.laz",
    "601500_5674000.laz",
    "604500_5652500.laz",
]

EPS = 1e-12


@dataclass
class RunningVectorStats:
    width: int

    def __post_init__(self) -> None:
        self.count = 0
        self.total = np.zeros(self.width, dtype=np.float64)
        self.total_sq = np.zeros(self.width, dtype=np.float64)
        self.minimum = np.full(self.width, np.inf, dtype=np.float64)
        self.maximum = np.full(self.width, -np.inf, dtype=np.float64)

    def update(self, values: np.ndarray) -> None:
        arr = np.asarray(values, dtype=np.float64)
        if arr.ndim != 2 or arr.shape[1] != self.width:
            raise ValueError(f"Expected (*,{self.width}) matrix, got {arr.shape}.")
        if arr.shape[0] == 0:
            return
        if not np.isfinite(arr).all():
            raise ValueError("Feature matrix contains NaN or infinity.")
        self.count += int(arr.shape[0])
        self.total += arr.sum(axis=0)
        self.total_sq += np.square(arr).sum(axis=0)
        self.minimum = np.minimum(self.minimum, arr.min(axis=0))
        self.maximum = np.maximum(self.maximum, arr.max(axis=0))

    def finalize(self) -> Dict[str, Any]:
        if self.count <= 0:
            raise ValueError("No feature rows accumulated.")
        mean = self.total / self.count
        variance = np.maximum(self.total_sq / self.count - np.square(mean), 0.0)
        return {
            "count": int(self.count),
            "mean": mean,
            "std": np.sqrt(variance),
            "min": self.minimum,
            "max": self.maximum,
        }


@dataclass
class RunningScalarStats:
    def __post_init__(self) -> None:
        self.count = 0
        self.total = 0.0
        self.total_sq = 0.0
        self.minimum = float("inf")
        self.maximum = float("-inf")

    def update(self, values: np.ndarray) -> None:
        arr = np.asarray(values, dtype=np.float64).reshape(-1)
        arr = arr[np.isfinite(arr)]
        if arr.size == 0:
            return
        self.count += int(arr.size)
        self.total += float(arr.sum())
        self.total_sq += float(np.square(arr).sum())
        self.minimum = min(self.minimum, float(arr.min()))
        self.maximum = max(self.maximum, float(arr.max()))

    def finalize(self) -> Dict[str, Any]:
        if self.count <= 0:
            return {"count": 0, "mean": None, "std": None, "min": None, "max": None}
        mean = self.total / self.count
        variance = max(self.total_sq / self.count - mean * mean, 0.0)
        return {
            "count": int(self.count),
            "mean": float(mean),
            "std": float(math.sqrt(variance)),
            "min": float(self.minimum),
            "max": float(self.maximum),
        }


def stable_seed(text: str, base_seed: int) -> int:
    digest = hashlib.sha256(text.encode("utf-8", errors="ignore")).digest()
    return int((int.from_bytes(digest[:8], "little") + base_seed) % (2**32 - 1))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def atomic_replace(temp_path: Path, final_path: Path) -> None:
    if final_path.exists():
        final_path.unlink()
    os.replace(temp_path, final_path)


def load_base_stats(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        stats = json.load(handle)

    names = list(stats.get("feature_names", []))
    if names != FEATURE_NAMES:
        if len(names) != NUM_FEATURES:
            raise ValueError(f"Base stats contains {len(names)} names, expected 68.")
        mismatch = next(
            (i for i, (actual, expected) in enumerate(zip(names, FEATURE_NAMES)) if actual != expected),
            None,
        )
        raise ValueError(
            f"Base feature order mismatch at index {mismatch}: "
            f"{names[mismatch]!r} != {FEATURE_NAMES[mismatch]!r}"
        )

    for key in ("mean", "std", "min", "max"):
        values = np.asarray(stats.get(key), dtype=np.float64)
        if values.shape != (NUM_FEATURES,):
            raise ValueError(f"Base stats {key} shape is {values.shape}, expected (68,).")
        stats[f"{key}_np"] = values

    stats["std_np"] = np.where(stats["std_np"] < 1e-6, 1.0, stats["std_np"])
    return stats


def map_labels(raw_class: np.ndarray) -> np.ndarray:
    labels = np.full(raw_class.shape, -1, dtype=np.int16)
    for source_class, model_class in SOURCE_TO_MODEL.items():
        labels[raw_class == source_class] = model_class
    return labels


def prepare_returns(
    return_number: np.ndarray,
    number_of_returns: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray, Dict[str, int]]:
    rn = np.asarray(return_number, dtype=np.float32)
    nr = np.asarray(number_of_returns, dtype=np.float32)

    invalid = (
        ~np.isfinite(rn)
        | ~np.isfinite(nr)
        | (rn < 1)
        | (nr < 1)
        | (rn > nr)
    )
    invalid_count = int(np.count_nonzero(invalid))
    if invalid_count:
        raise ValueError(
            f"Selected diagnostic file contains {invalid_count:,} invalid return records."
        )

    return rn, nr, {
        "total_records": int(rn.size),
        "invalid_records": invalid_count,
    }


def read_laz(path: Path) -> Dict[str, Any]:
    started = time.time()
    las = laspy.read(str(path))
    n = int(las.header.point_count)

    xyz = np.column_stack(
        [
            np.asarray(las.x, dtype=np.float64),
            np.asarray(las.y, dtype=np.float64),
            np.asarray(las.z, dtype=np.float64),
        ]
    )
    if xyz.shape != (n, 3):
        raise ValueError(f"XYZ shape {xyz.shape} does not match header count {n}.")
    if not np.isfinite(xyz).all():
        raise ValueError("XYZ contains NaN or infinity.")

    raw_class = np.asarray(las.classification, dtype=np.int16)
    labels = map_labels(raw_class)
    intensity = np.clip(
        np.asarray(las.intensity, dtype=np.float32) / 65535.0,
        0.0,
        1.0,
    ).astype(np.float32)

    rn, nr, return_report = prepare_returns(
        np.asarray(las.return_number),
        np.asarray(las.number_of_returns),
    )

    crs_text = ""
    epsg = ""
    try:
        crs = las.header.parse_crs()
        if crs is not None:
            crs_text = crs.to_string()
            authority = crs.to_authority()
            if authority:
                epsg = f"{authority[0]}:{authority[1]}"
    except Exception:
        pass

    header = {
        "point_count": n,
        "point_format": int(las.header.point_format.id),
        "las_version": str(las.header.version),
        "scales": np.asarray(las.header.scales, dtype=float).tolist(),
        "offsets": np.asarray(las.header.offsets, dtype=float).tolist(),
        "mins": np.asarray(las.header.mins, dtype=float).tolist(),
        "maxs": np.asarray(las.header.maxs, dtype=float).tolist(),
        "generating_software": str(las.header.generating_software).strip(),
        "crs": crs_text,
        "epsg": epsg,
        "read_seconds": float(time.time() - started),
    }

    del las
    gc.collect()

    return {
        "xyz": xyz,
        "raw_class": raw_class,
        "labels": labels,
        "intensity": intensity,
        "return_number": rn,
        "num_returns": nr,
        "return_report": return_report,
        "header": header,
    }


def lowest_grid_ground(
    context_xyz: np.ndarray,
    grid_size: float,
    percentile: float,
) -> np.ndarray:
    xy_min = context_xyz[:, :2].min(axis=0)
    ix = np.floor((context_xyz[:, 0] - xy_min[0]) / grid_size).astype(np.int32)
    iy = np.floor((context_xyz[:, 1] - xy_min[1]) / grid_size).astype(np.int32)

    ny = int(iy.max()) + 1
    key = ix.astype(np.int64) * max(ny, 1) + iy.astype(np.int64)
    order = np.argsort(key, kind="mergesort")
    ordered_key = key[order]

    starts = np.flatnonzero(np.r_[True, ordered_key[1:] != ordered_key[:-1]])
    ends = np.r_[starts[1:], len(order)]

    selected: List[int] = []
    for start, end in zip(starts, ends):
        group = order[start:end]
        z = context_xyz[group, 2]
        target_z = np.percentile(z, percentile)
        selected.append(int(group[np.argmin(np.abs(z - target_z))]))

    return context_xyz[np.asarray(selected, dtype=np.int64)]


def interpolate_hag(
    query_xyz: np.ndarray,
    ground_xyz: np.ndarray,
    k: int = 5,
) -> np.ndarray:
    if ground_xyz.shape[0] < 3:
        return np.clip(
            query_xyz[:, 2] - float(np.min(query_xyz[:, 2])),
            -2.0,
            None,
        ).astype(np.float32)

    tree = cKDTree(ground_xyz[:, :2])
    k_use = min(k, ground_xyz.shape[0])
    distances, indices = tree.query(query_xyz[:, :2], k=k_use, workers=1)

    if distances.ndim == 1:
        distances = distances[:, None]
        indices = indices[:, None]

    weights = 1.0 / (distances.astype(np.float64) + 1e-6)
    weights /= weights.sum(axis=1, keepdims=True)
    ground_z = np.sum(ground_xyz[indices, 2] * weights, axis=1)

    return np.nan_to_num(
        np.clip(query_xyz[:, 2] - ground_z, -2.0, None),
        nan=0.0,
        posinf=0.0,
        neginf=-2.0,
    ).astype(np.float32)


def voxel_centroids(points_xyz: np.ndarray, voxel_size: float) -> np.ndarray:
    """
    Deterministic 3-D voxel centroid calculation without Python dictionaries.
    """
    if points_xyz.shape[0] == 0:
        return np.empty((0, 3), dtype=np.float64)

    origin = points_xyz.min(axis=0)
    indices = np.floor((points_xyz - origin) / voxel_size).astype(np.int32)

    max_index = indices.max(axis=0).astype(np.int64) + 1
    nx = max(int(max_index[0]), 1)
    ny = max(int(max_index[1]), 1)

    key = (
        indices[:, 0].astype(np.int64)
        + nx
        * (
            indices[:, 1].astype(np.int64)
            + ny * indices[:, 2].astype(np.int64)
        )
    )

    order = np.argsort(key, kind="mergesort")
    sorted_key = key[order]
    starts = np.flatnonzero(np.r_[True, sorted_key[1:] != sorted_key[:-1]])
    counts = np.diff(np.r_[starts, len(order)]).astype(np.float64)

    sorted_points = points_xyz[order]
    sums = np.add.reduceat(sorted_points, starts, axis=0)
    centroids = sums / counts[:, None]

    return centroids.astype(np.float64, copy=False)


def pca_descriptor(neighbour_xyz: np.ndarray) -> np.ndarray:
    if neighbour_xyz.shape[0] < 3:
        return np.zeros(len(GEOMETRY_NAMES), dtype=np.float32)

    centred = neighbour_xyz.astype(np.float64) - neighbour_xyz.mean(axis=0, keepdims=True)
    covariance = (centred.T @ centred) / max(neighbour_xyz.shape[0] - 1, 1)

    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    eigenvalues = np.maximum(eigenvalues, 0.0)[::-1]
    eigenvectors = eigenvectors[:, ::-1]

    l1, l2, l3 = eigenvalues
    total = l1 + l2 + l3 + EPS

    linearity = (l1 - l2) / (l1 + EPS)
    planarity = (l2 - l3) / (l1 + EPS)
    sphericity = l3 / (l1 + EPS)
    omnivariance = np.cbrt(max(l1 * l2 * l3, 0.0))
    anisotropy = (l1 - l3) / (l1 + EPS)

    eigenentropy = -(
        l1 * np.log(l1 + EPS)
        + l2 * np.log(l2 + EPS)
        + l3 * np.log(l3 + EPS)
    )

    surface_variation = l3 / total
    normal = eigenvectors[:, 2]
    verticality = 1.0 - abs(normal[2])

    return np.nan_to_num(
        np.asarray(
            [
                l1,
                l2,
                l3,
                linearity,
                planarity,
                sphericity,
                omnivariance,
                anisotropy,
                eigenentropy,
                surface_variation,
                verticality,
            ],
            dtype=np.float64,
        ),
        nan=0.0,
        posinf=0.0,
        neginf=0.0,
    ).astype(np.float32)


def radius_pca_features(
    query_xyz: np.ndarray,
    context_xyz: np.ndarray,
    radius: float,
    query_chunk_size: int,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    """
    Compute PCA using every context point/centroid inside the requested radius.
    """
    tree = cKDTree(context_xyz)
    output = np.zeros(
        (query_xyz.shape[0], len(GEOMETRY_NAMES)),
        dtype=np.float32,
    )

    neighbour_stats = RunningScalarStats()
    fewer_than_three = 0

    for start in range(0, query_xyz.shape[0], query_chunk_size):
        end = min(start + query_chunk_size, query_xyz.shape[0])
        lists = tree.query_ball_point(
            query_xyz[start:end],
            r=float(radius),
            workers=1,
        )

        for local_index, neighbour_indices in enumerate(lists):
            count = len(neighbour_indices)
            neighbour_stats.update(np.asarray([count], dtype=np.float64))

            if count < 3:
                fewer_than_three += 1
                distances, fallback_indices = tree.query(
                    query_xyz[start + local_index],
                    k=min(3, context_xyz.shape[0]),
                    workers=1,
                )
                fallback_indices = np.atleast_1d(fallback_indices)
                neighbour_xyz = context_xyz[fallback_indices]
            else:
                neighbour_xyz = context_xyz[
                    np.asarray(neighbour_indices, dtype=np.int64)
                ]

            output[start + local_index] = pca_descriptor(neighbour_xyz)

    report = neighbour_stats.finalize()
    report["radius"] = float(radius)
    report["context_points"] = int(context_xyz.shape[0])
    report["fewer_than_three"] = int(fewer_than_three)
    report["fewer_than_three_fraction"] = float(
        fewer_than_three / max(query_xyz.shape[0], 1)
    )

    return output, report


def density_counts(
    query_xyz: np.ndarray,
    raw_context_tree: cKDTree,
    radius: float,
) -> np.ndarray:
    try:
        counts = raw_context_tree.query_ball_point(
            query_xyz,
            r=float(radius),
            return_length=True,
            workers=1,
        )
        return np.asarray(counts, dtype=np.float32)
    except TypeError:
        lists = raw_context_tree.query_ball_point(query_xyz, r=float(radius))
        return np.asarray([len(values) for values in lists], dtype=np.float32)


def roughness_zstd(
    query_xyz: np.ndarray,
    context_xyz: np.ndarray,
    context_tree: cKDTree,
    k: int,
) -> np.ndarray:
    k_use = min(max(int(k), 2), context_xyz.shape[0])
    _, indices = context_tree.query(query_xyz, k=k_use, workers=1)

    if indices.ndim == 1:
        indices = indices[:, None]

    neighbours = context_xyz[indices]
    roughness = np.std(
        neighbours[:, :, 2] - query_xyz[:, None, 2],
        axis=1,
    )
    return np.nan_to_num(
        roughness,
        nan=0.0,
        posinf=0.0,
        neginf=0.0,
    ).astype(np.float32)


def build_features(
    query_xyz: np.ndarray,
    raw_context_xyz: np.ndarray,
    raw_context_class: np.ndarray,
    query_intensity: np.ndarray,
    query_return_number: np.ndarray,
    query_num_returns: np.ndarray,
    tile_center_xy: Tuple[float, float],
    args: argparse.Namespace,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    center_x, center_y = tile_center_xy

    local_x = (query_xyz[:, 0] - center_x).astype(np.float32)
    local_y = (query_xyz[:, 1] - center_y).astype(np.float32)
    absolute_z = query_xyz[:, 2].astype(np.float32)

    ground_proxy = lowest_grid_ground(
        raw_context_xyz,
        grid_size=args.hag_grid_size,
        percentile=args.hag_percentile,
    )
    hag = interpolate_hag(query_xyz, ground_proxy, k=5)

    raw_tree = cKDTree(raw_context_xyz)
    density = density_counts(
        query_xyz=query_xyz,
        raw_context_tree=raw_tree,
        radius=args.density_radius,
    )
    roughness = roughness_zstd(
        query_xyz=query_xyz,
        context_xyz=raw_context_xyz,
        context_tree=raw_tree,
        k=args.roughness_k,
    )

    rn = query_return_number.astype(np.float32)
    nr = query_num_returns.astype(np.float32)
    return_ratio = rn / np.maximum(nr, 1.0)
    is_single = (nr == 1.0).astype(np.float32)
    is_first = (rn == 1.0).astype(np.float32)
    is_last = (rn == nr).astype(np.float32)

    base = np.column_stack(
        [
            local_x,
            local_y,
            absolute_z,
            hag,
            query_intensity.astype(np.float32),
            rn,
            nr,
            return_ratio.astype(np.float32),
            is_single,
            is_first,
            is_last,
            density,
            roughness,
        ]
    ).astype(np.float32)

    voxel_015 = voxel_centroids(raw_context_xyz, args.voxel_size_r2)
    voxel_030 = voxel_centroids(raw_context_xyz, args.voxel_size_large)

    geometry_context = {
        0.5: ("raw", raw_context_xyz),
        1.0: ("raw", raw_context_xyz),
        2.0: (f"voxel_{args.voxel_size_r2}", voxel_015),
        5.0: (f"voxel_{args.voxel_size_large}", voxel_030),
        10.0: (f"voxel_{args.voxel_size_large}", voxel_030),
    }

    geometry_parts: List[np.ndarray] = []
    radius_reports: Dict[str, Any] = {}

    for radius in FEATURE_SCALES:
        context_name, context = geometry_context[radius]
        values, report = radius_pca_features(
            query_xyz=query_xyz,
            context_xyz=context,
            radius=radius,
            query_chunk_size=args.geometry_query_chunk,
        )
        report["context_type"] = context_name
        radius_reports[f"r{radius:.1f}"] = report
        geometry_parts.append(values)

    features = np.concatenate([base] + geometry_parts, axis=1).astype(np.float32)

    if features.shape != (query_xyz.shape[0], NUM_FEATURES):
        raise RuntimeError(f"Feature shape is {features.shape}, expected (*,68).")
    if not np.isfinite(features).all():
        raise RuntimeError("Feature matrix contains NaN or infinity.")
    if np.max(np.abs(features[:, 0])) > args.tile_size / 2.0 + 0.01:
        raise RuntimeError("Local X exceeds tile limits.")
    if np.max(np.abs(features[:, 1])) > args.tile_size / 2.0 + 0.01:
        raise RuntimeError("Local Y exceeds tile limits.")
    if np.min(features[:, 4]) < -1e-6 or np.max(features[:, 4]) > 1.000001:
        raise RuntimeError("Intensity is outside 0..1.")
    if not np.allclose(features[:, 7], rn / np.maximum(nr, 1.0), atol=1e-5):
        raise RuntimeError("return_ratio mismatch.")
    if not np.allclose(features[:, 8], (nr == 1.0).astype(np.float32), atol=1e-5):
        raise RuntimeError("is_single_return mismatch.")
    if not np.allclose(features[:, 9], (rn == 1.0).astype(np.float32), atol=1e-5):
        raise RuntimeError("is_first_return mismatch.")
    if not np.allclose(features[:, 10], (rn == nr).astype(np.float32), atol=1e-5):
        raise RuntimeError("is_last_return mismatch.")

    report = {
        "density": {
            "radius": float(args.density_radius),
            "mean": float(np.mean(density)),
            "std": float(np.std(density)),
            "min": float(np.min(density)),
            "max": float(np.max(density)),
        },
        "roughness": {
            "method": "zstd_knn_raw",
            "k": int(args.roughness_k),
        },
        "hag": {
            "method": "lowest_grid_label_independent",
            "ground_proxy_points": int(ground_proxy.shape[0]),
        },
        "voxel_context": {
            "raw_points": int(raw_context_xyz.shape[0]),
            "voxel_015_points": int(voxel_015.shape[0]),
            "voxel_030_points": int(voxel_030.shape[0]),
        },
        "geometry": radius_reports,
    }

    return features, report


def square_query(
    xy_tree: cKDTree,
    center_x: float,
    center_y: float,
    half_width: float,
) -> np.ndarray:
    values = xy_tree.query_ball_point(
        [center_x, center_y],
        r=float(half_width),
        p=np.inf,
        workers=1,
    )
    return np.asarray(values, dtype=np.int64)


def tile_grid(
    target_xyz: np.ndarray,
    tile_size: float,
) -> List[Tuple[int, int, float, float]]:
    min_x = float(np.min(target_xyz[:, 0]))
    min_y = float(np.min(target_xyz[:, 1]))
    max_x = float(np.max(target_xyz[:, 0]))
    max_y = float(np.max(target_xyz[:, 1]))

    count_x = max(int(math.ceil((max_x - min_x) / tile_size)), 1)
    count_y = max(int(math.ceil((max_y - min_y) / tile_size)), 1)

    output: List[Tuple[int, int, float, float]] = []
    for tile_x in range(count_x):
        for tile_y in range(count_y):
            output.append(
                (
                    tile_x,
                    tile_y,
                    min_x + (tile_x + 0.5) * tile_size,
                    min_y + (tile_y + 0.5) * tile_size,
                )
            )
    return output


def extract_file(
    path: Path,
    output_dir: Path,
    base_stats: Mapping[str, Any],
    global_stats: RunningVectorStats,
    global_clip_counts: np.ndarray,
    global_class_counts: np.ndarray,
    global_hag_stats: RunningScalarStats,
    args: argparse.Namespace,
) -> Dict[str, Any]:
    started = time.time()
    loaded = read_laz(path)

    xyz = loaded["xyz"]
    raw_class = loaded["raw_class"]
    labels = loaded["labels"]
    target_mask = labels >= 0

    if not np.any(target_mask):
        raise ValueError("No target classes 2..6 found.")

    source_min = xyz.min(axis=0)
    source_max = xyz.max(axis=0)

    print("\n" + "=" * 100)
    print(f"FILE: {path.name}")
    print(f"Points: {xyz.shape[0]:,}")
    print(f"Target: {int(np.count_nonzero(target_mask)):,}")
    print(f"Returns invalid: {loaded['return_report']['invalid_records']:,}")
    print("=" * 100)

    xy_tree_started = time.time()
    xy_tree = cKDTree(xyz[:, :2])
    xy_tree_seconds = time.time() - xy_tree_started

    tiles = tile_grid(xyz[target_mask], args.tile_size)

    final_h5 = output_dir / "per_file_h5" / f"{path.stem}_hybrid_v2.h5"
    temp_h5 = final_h5.with_suffix(".partial.h5")
    final_h5.parent.mkdir(parents=True, exist_ok=True)

    if final_h5.exists() and not args.overwrite:
        raise FileExistsError(f"Output exists: {final_h5}. Use --overwrite.")
    if temp_h5.exists():
        temp_h5.unlink()

    file_stats = RunningVectorStats(NUM_FEATURES)
    file_clip_counts = np.zeros(NUM_FEATURES, dtype=np.int64)
    file_class_counts = np.zeros(NUM_CLASSES, dtype=np.int64)
    file_hag_stats = RunningScalarStats()

    tile_reports: List[Dict[str, Any]] = []
    written_tiles = 0
    saved_points = 0
    skipped_border_points = 0

    with h5py.File(temp_h5, "w") as h5:
        meta = h5.create_group("metadata")
        tiles_group = h5.create_group("tiles")

        for tile_x, tile_y, center_x, center_y in tqdm(
            tiles,
            desc=f"hybrid-v2 {path.name}",
        ):
            core_indices = square_query(
                xy_tree,
                center_x,
                center_y,
                args.tile_size / 2.0,
            )
            if core_indices.size == 0:
                continue

            candidates = core_indices[labels[core_indices] >= 0]
            if candidates.size < args.min_points_per_tile:
                continue

            # Exclude query points near the source-file edge because adjacent
            # LAZ context is not loaded in this diagnostic.
            candidate_xyz = xyz[candidates]
            border = (
                (candidate_xyz[:, 0] < source_min[0] + args.halo)
                | (candidate_xyz[:, 0] > source_max[0] - args.halo)
                | (candidate_xyz[:, 1] < source_min[1] + args.halo)
                | (candidate_xyz[:, 1] > source_max[1] - args.halo)
            )
            skipped_border_points += int(np.count_nonzero(border))
            candidates = candidates[~border]

            if candidates.size < args.min_points_per_tile:
                continue

            rng = np.random.default_rng(
                stable_seed(
                    f"{path.resolve()}::{tile_x}::{tile_y}::hybrid_v2",
                    args.seed,
                )
            )

            if candidates.size > args.max_points_per_tile:
                query_indices = rng.choice(
                    candidates,
                    size=args.max_points_per_tile,
                    replace=False,
                )
            else:
                query_indices = candidates

            context_indices = square_query(
                xy_tree,
                center_x,
                center_y,
                args.tile_size / 2.0 + args.halo,
            )
            if context_indices.size < 3:
                continue

            query_xyz = xyz[query_indices]
            context_xyz = xyz[context_indices]

            features, diagnostic = build_features(
                query_xyz=query_xyz,
                raw_context_xyz=context_xyz,
                raw_context_class=raw_class[context_indices],
                query_intensity=loaded["intensity"][query_indices],
                query_return_number=loaded["return_number"][query_indices],
                query_num_returns=loaded["num_returns"][query_indices],
                tile_center_xy=(center_x, center_y),
                args=args,
            )

            tile_labels = labels[query_indices].astype(np.int16)

            coords = query_xyz.copy()
            coords[:, 0] -= center_x
            coords[:, 1] -= center_y
            coords[:, 2] -= np.mean(coords[:, 2])
            coords = coords.astype(np.float32)

            group = tiles_group.create_group(f"tile_{written_tiles:06d}")
            group.create_dataset("coords", data=coords, compression="lzf")
            group.create_dataset("features", data=features, compression="lzf")
            group.create_dataset("labels", data=tile_labels, compression="lzf")

            counts = np.bincount(
                tile_labels.astype(np.int64),
                minlength=NUM_CLASSES,
            )[:NUM_CLASSES].astype(np.int64)

            group.attrs["class_counts"] = counts
            group.attrs["n_points"] = int(features.shape[0])
            group.attrs["tile_x"] = int(tile_x)
            group.attrs["tile_y"] = int(tile_y)
            group.attrs["tile_center_x"] = float(center_x)
            group.attrs["tile_center_y"] = float(center_y)
            group.attrs["context_points"] = int(context_indices.size)
            group.attrs["diagnostic_json"] = json.dumps(diagnostic, sort_keys=True)

            file_stats.update(features)
            global_stats.update(features)

            normalized = (
                features.astype(np.float64)
                - np.asarray(base_stats["mean_np"], dtype=np.float64)
            ) / np.asarray(base_stats["std_np"], dtype=np.float64)

            clip_counts = np.sum(np.abs(normalized) >= 10.0, axis=0).astype(np.int64)
            file_clip_counts += clip_counts
            global_clip_counts += clip_counts

            file_class_counts += counts
            global_class_counts += counts
            file_hag_stats.update(features[:, 3])
            global_hag_stats.update(features[:, 3])

            tile_reports.append(
                {
                    "tile_index": int(written_tiles),
                    "tile_x": int(tile_x),
                    "tile_y": int(tile_y),
                    "saved_points": int(features.shape[0]),
                    "context_points": int(context_indices.size),
                    "diagnostic": diagnostic,
                }
            )

            saved_points += int(features.shape[0])
            written_tiles += 1

            del (
                core_indices,
                candidates,
                query_indices,
                context_indices,
                query_xyz,
                context_xyz,
                features,
                coords,
                tile_labels,
            )
            gc.collect()

        if written_tiles == 0:
            raise ValueError("No tiles written.")

        meta.attrs["feature_names"] = json.dumps(FEATURE_NAMES)
        meta.attrs["class_names"] = json.dumps(CLASS_NAMES)
        meta.attrs["num_features"] = NUM_FEATURES
        meta.attrs["num_classes"] = NUM_CLASSES
        meta.attrs["total_tiles"] = int(written_tiles)
        meta.attrs["total_points"] = int(saved_points)
        meta.attrs["class_counts"] = file_class_counts
        meta.attrs["source_file"] = str(path.resolve())
        meta.attrs["source_file_sha256"] = sha256_file(path)
        meta.attrs["base_stats"] = str(args.base_stats.resolve())
        meta.attrs["base_stats_sha256"] = sha256_file(args.base_stats)
        meta.attrs["feature_contract"] = "accepted_68f_hybrid_true_radius_v2"
        meta.attrs["diagnostic_only"] = True
        meta.attrs["extractor_config"] = json.dumps(
            {
                "tile_size": args.tile_size,
                "halo": args.halo,
                "max_points_per_tile": args.max_points_per_tile,
                "density_radius": args.density_radius,
                "roughness_k": args.roughness_k,
                "hag_grid_size": args.hag_grid_size,
                "hag_percentile": args.hag_percentile,
                "voxel_size_r2": args.voxel_size_r2,
                "voxel_size_large": args.voxel_size_large,
                "geometry": {
                    "r0.5": "raw_true_radius",
                    "r1.0": "raw_true_radius",
                    "r2.0": "0.15m_voxel_true_radius",
                    "r5.0": "0.30m_voxel_true_radius",
                    "r10.0": "0.30m_voxel_true_radius",
                },
                "source_border": "outer_10m_query_points_excluded",
            },
            sort_keys=True,
        )

    atomic_replace(temp_h5, final_h5)

    stats = file_stats.finalize()
    report = {
        "source_file": str(path.resolve()),
        "output_h5": str(final_h5.resolve()),
        "header": loaded["header"],
        "return_report": loaded["return_report"],
        "source_points": int(xyz.shape[0]),
        "target_points": int(np.count_nonzero(target_mask)),
        "written_tiles": int(written_tiles),
        "saved_points": int(saved_points),
        "skipped_source_border_points": int(skipped_border_points),
        "global_xy_tree_seconds": float(xy_tree_seconds),
        "elapsed_seconds": float(time.time() - started),
        "class_counts": file_class_counts.astype(int).tolist(),
        "feature_stats": {
            "sample_size": int(stats["count"]),
            "mean": stats["mean"].astype(float).tolist(),
            "std": stats["std"].astype(float).tolist(),
            "min": stats["min"].astype(float).tolist(),
            "max": stats["max"].astype(float).tolist(),
            "base_clip_fraction_per_feature": (
                file_clip_counts / max(file_stats.count, 1)
            ).astype(float).tolist(),
        },
        "hag": file_hag_stats.finalize(),
        "tile_reports": tile_reports,
    }

    report_path = output_dir / "reports" / f"{path.stem}_hybrid_v2_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"WROTE: {final_h5}")
    print(f"TILES: {written_tiles}")
    print(f"POINTS: {saved_points:,}")
    print(f"TIME: {report['elapsed_seconds'] / 60.0:.2f} minutes")

    del loaded, xyz, raw_class, labels, xy_tree
    gc.collect()

    return report


def write_drift_csv(
    path: Path,
    extracted: Mapping[str, Any],
    base_stats: Mapping[str, Any],
    clip_counts: np.ndarray,
) -> List[Dict[str, Any]]:
    mean_new = np.asarray(extracted["mean"], dtype=np.float64)
    std_new = np.asarray(extracted["std"], dtype=np.float64)
    min_new = np.asarray(extracted["min"], dtype=np.float64)
    max_new = np.asarray(extracted["max"], dtype=np.float64)

    mean_base = np.asarray(base_stats["mean_np"], dtype=np.float64)
    std_base = np.asarray(base_stats["std_np"], dtype=np.float64)
    min_base = np.asarray(base_stats["min_np"], dtype=np.float64)
    max_base = np.asarray(base_stats["max_np"], dtype=np.float64)

    sample_size = max(int(extracted["count"]), 1)
    rows: List[Dict[str, Any]] = []

    for index, name in enumerate(FEATURE_NAMES):
        drift = abs(mean_new[index] - mean_base[index]) / max(std_base[index], 1e-6)
        std_ratio = std_new[index] / max(std_base[index], 1e-6)
        clip_fraction = float(clip_counts[index] / sample_size)

        if drift > 8.0 or clip_fraction > 0.10:
            severity = "FAIL_CANDIDATE"
        elif drift > 4.0 or clip_fraction > 0.05:
            severity = "HIGH"
        elif drift > 2.0 or clip_fraction > 0.01:
            severity = "REVIEW"
        else:
            severity = "OK"

        rows.append(
            {
                "index": index,
                "feature": name,
                "base_mean": mean_base[index],
                "new_mean": mean_new[index],
                "base_std": std_base[index],
                "new_std": std_new[index],
                "standardized_mean_drift_sigma": drift,
                "std_ratio_new_over_base": std_ratio,
                "base_min": min_base[index],
                "new_min": min_new[index],
                "base_max": max_base[index],
                "new_max": max_new[index],
                "base_normalization_clip_fraction": clip_fraction,
                "severity": severity,
            }
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    return rows


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Second three-file diagnostic using hybrid true-radius PCA."
    )
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--base-stats", type=Path, required=True)
    parser.add_argument("--files", nargs="+", default=DEFAULT_FILES)
    parser.add_argument("--tile-size", type=float, default=60.0)
    parser.add_argument("--halo", type=float, default=10.0)
    parser.add_argument("--max-points-per-tile", type=int, default=1000)
    parser.add_argument("--min-points-per-tile", type=int, default=500)
    parser.add_argument("--density-radius", type=float, default=0.5)
    parser.add_argument("--roughness-k", type=int, default=10)
    parser.add_argument("--hag-grid-size", type=float, default=1.0)
    parser.add_argument("--hag-percentile", type=float, default=5.0)
    parser.add_argument("--voxel-size-r2", type=float, default=0.15)
    parser.add_argument("--voxel-size-large", type=float, default=0.30)
    parser.add_argument("--geometry-query-chunk", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    if len(args.files) != 3:
        raise SystemExit(f"Provide exactly three files, got {len(args.files)}.")

    args.input_root = args.input_root.resolve()
    args.output_dir = args.output_dir.resolve()
    args.base_stats = args.base_stats.resolve()

    paths = [(args.input_root / name).resolve() for name in args.files]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise SystemExit("Missing selected files:\n" + "\n".join(missing))

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "per_file_h5").mkdir(parents=True, exist_ok=True)
    (args.output_dir / "reports").mkdir(parents=True, exist_ok=True)

    base_stats = load_base_stats(args.base_stats)

    manifest = {
        "selected_files": [str(path) for path in paths],
        "base_stats": str(args.base_stats),
        "base_stats_sha256": sha256_file(args.base_stats),
        "configuration": {
            "tile_size": args.tile_size,
            "halo": args.halo,
            "max_points_per_tile": args.max_points_per_tile,
            "density_radius": args.density_radius,
            "roughness_k": args.roughness_k,
            "voxel_size_r2": args.voxel_size_r2,
            "voxel_size_large": args.voxel_size_large,
            "geometry": {
                "r0.5": "raw_true_radius",
                "r1.0": "raw_true_radius",
                "r2.0": "voxel_true_radius_0.15m",
                "r5.0": "voxel_true_radius_0.30m",
                "r10.0": "voxel_true_radius_0.30m",
            },
            "source_border": "outer_10m_excluded",
            "seed": args.seed,
        },
    }
    (args.output_dir / "selected_files_hybrid_v2.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )

    print("=" * 100)
    print("ACCEPTED 68F HYBRID TRUE-RADIUS DIAGNOSTIC V2")
    print("=" * 100)
    print(f"Input:       {args.input_root}")
    print(f"Output:      {args.output_dir}")
    print(f"Base stats:  {args.base_stats}")
    print(f"Density:     raw count at {args.density_radius} m")
    print(f"r0.5/r1:     raw true-radius PCA")
    print(f"r2:          {args.voxel_size_r2} m voxel true-radius PCA")
    print(f"r5/r10:      {args.voxel_size_large} m voxel true-radius PCA")
    print(f"Saved/tile:  {args.max_points_per_tile}")
    print("=" * 100)

    global_stats = RunningVectorStats(NUM_FEATURES)
    global_clip_counts = np.zeros(NUM_FEATURES, dtype=np.int64)
    global_class_counts = np.zeros(NUM_CLASSES, dtype=np.int64)
    global_hag = RunningScalarStats()

    file_reports: List[Dict[str, Any]] = []
    failures: List[Dict[str, str]] = []

    for path in paths:
        try:
            file_reports.append(
                extract_file(
                    path=path,
                    output_dir=args.output_dir,
                    base_stats=base_stats,
                    global_stats=global_stats,
                    global_clip_counts=global_clip_counts,
                    global_class_counts=global_class_counts,
                    global_hag_stats=global_hag,
                    args=args,
                )
            )
        except Exception as exc:
            traceback.print_exc()
            failures.append(
                {
                    "file": str(path),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            break

    if global_stats.count <= 0:
        summary = {"status": "FAILED", "failures": failures}
        (args.output_dir / "diagnostic_summary_hybrid_v2.json").write_text(
            json.dumps(summary, indent=2),
            encoding="utf-8",
        )
        return 2

    finalized = global_stats.finalize()

    stats_json = {
        "num_features": NUM_FEATURES,
        "sample_size": int(finalized["count"]),
        "feature_names": FEATURE_NAMES,
        "class_names": CLASS_NAMES,
        "class_counts": global_class_counts.astype(int).tolist(),
        "mean": finalized["mean"].astype(float).tolist(),
        "std": np.maximum(finalized["std"], 1e-6).astype(float).tolist(),
        "min": finalized["min"].astype(float).tolist(),
        "max": finalized["max"].astype(float).tolist(),
    }

    stats_path = args.output_dir / "diagnostic_feature_stats_hybrid_v2.json"
    stats_path.write_text(json.dumps(stats_json, indent=2), encoding="utf-8")

    drift_rows = write_drift_csv(
        path=args.output_dir / "feature_drift_hybrid_v2.csv",
        extracted=finalized,
        base_stats=base_stats,
        clip_counts=global_clip_counts,
    )

    severity_counts: Dict[str, int] = {}
    for row in drift_rows:
        severity_counts[row["severity"]] = severity_counts.get(row["severity"], 0) + 1

    most_drifted = sorted(
        drift_rows,
        key=lambda row: row["standardized_mean_drift_sigma"],
        reverse=True,
    )[:15]

    structural_pass = len(file_reports) == 3 and not failures

    summary = {
        "status": "STRUCTURAL_PASS" if structural_pass else "FAILED",
        "approved_for_200_file_extraction": False,
        "approval_note": (
            "Review density, r2/r5/r10 drift and clipping before approving production."
        ),
        "three_files_completed": len(file_reports),
        "failures": failures,
        "sample_size": int(finalized["count"]),
        "class_names": CLASS_NAMES,
        "class_counts": global_class_counts.astype(int).tolist(),
        "severity_counts": severity_counts,
        "most_drifted_features": most_drifted,
        "hag": global_hag.finalize(),
        "file_reports": [
            {
                "source_file": report["source_file"],
                "output_h5": report["output_h5"],
                "written_tiles": report["written_tiles"],
                "saved_points": report["saved_points"],
                "elapsed_seconds": report["elapsed_seconds"],
                "class_counts": report["class_counts"],
            }
            for report in file_reports
        ],
        "generated_files": {
            "summary": str(args.output_dir / "diagnostic_summary_hybrid_v2.json"),
            "feature_drift": str(args.output_dir / "feature_drift_hybrid_v2.csv"),
            "feature_stats": str(stats_path),
            "per_file_h5": str(args.output_dir / "per_file_h5"),
            "reports": str(args.output_dir / "reports"),
        },
    }

    summary_path = args.output_dir / "diagnostic_summary_hybrid_v2.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print("\n" + "=" * 100)
    print("HYBRID V2 DIAGNOSTIC COMPLETE")
    print("=" * 100)
    print(f"Files completed: {len(file_reports)}/3")
    print(f"Saved points:    {finalized['count']:,}")
    print(f"Status:          {summary['status']}")
    print(f"Severities:      {severity_counts}")
    print("\nReports:")
    print(f"  {summary_path}")
    print(f"  {args.output_dir / 'feature_drift_hybrid_v2.csv'}")
    print(f"  {stats_path}")
    print("\nFULL 200-FILE EXTRACTION IS NOT AUTO-APPROVED.")
    print("=" * 100)

    return 0 if structural_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
