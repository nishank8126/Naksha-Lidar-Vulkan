#!/usr/bin/env python3
"""
Premium LiDAR 68F inference V3.3.0 Accuracy-Final candidate.

Goal
----
Preserve the accepted Premium model/feature contract while removing repeated work
that made V2 take hours on large point clouds.

Accuracy contract retained:
- exact Premium best_guarded checkpoint (5 classes, 68 features)
- exact accepted feature_stats.json normalization
- 60 m tiles + 10 m halo
- DTM V3 Strong HAG
- intensity / 65535
- return features, with invalid returns neutralized to accepted means
- density radius 0.5 m
- roughness raw K=10 Z standard deviation
- PCA geometry at 0.5, 1, 2, 5 and 10 m
- raw context at 0.5/1 m
- 0.15 m voxel context at 2 m
- 0.30 m voxel context at 5/10 m
- PointNet blocks of 6,144 points
- model classes [0..4] -> LAS [2..6]

Speed changes that do NOT intentionally change the feature definition:
- build raw/voxel KD trees once per tile, not once per 7k query block
- build voxel centroids once per tile, not once per query block
- skip the temporary lowest-grid HAG because final V2 always overwrote it with
  DTM V3 Strong HAG before normalization/model inference
- vectorize/batch the 3x3 covariance/eigen work for PCA descriptors
- use SciPy KD-tree worker threads for neighbour lookup
- larger feature blocks with bounded PCA micro-batches
- stream model blocks instead of storing all model batches in RAM
- no torch.cuda.empty_cache() on every model batch
- stream LAZ reading/writing to reduce peak RAM
- resumable tile checkpoints via memory-mapped prediction files

IMPORTANT
---------
Use --equivalence-check on the already validated reference LAZ before trusting
the frozen Premium core on a new large file. V3.3.0 keeps the approved conservative post-processing
only after Premium inference: High Noise (18), Low Point (7), and precision-first
Ground-only Uncategorized (0). Class 0 now requires low Premium Ground confidence,
a tight RAW signed-HAG terrain band, and local lower-surface support. The check compares
V3 features and predictions against
the original recovered hybrid.build_features path on real points.
"""

from __future__ import annotations

import argparse
import copy
import gc
import hashlib
import importlib.util
import json
import math
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import laspy
import numpy as np
import torch
from scipy.ndimage import median_filter
from scipy.spatial import cKDTree


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_STATS = SCRIPT_DIR / "feature_stats.json"
DEFAULT_MODEL_PY = SCRIPT_DIR / "model.py"
DEFAULT_HYBRID_PY = SCRIPT_DIR / "_core_hybrid_v2.py"
DEFAULT_DTM_PY = SCRIPT_DIR / "_core_dtm_v3.py"

MODEL_NUM_POINTS = 6144
NUM_FEATURES = 68
NUM_CLASSES = 5

TILE_SIZE = 60.0
HALO = 10.0
DENSITY_RADIUS = 0.5
ROUGHNESS_K = 10
VOXEL_SIZE_R2 = 0.15
VOXEL_SIZE_LARGE = 0.30
SEED = 42
FAST_SCRIPT_VERSION = "3.3.0-accuracy-final-candidate"
TORCH_FPS_SEED_MODE = "stable_seed_per_reference_7000_block"

DEFAULT_FEATURE_BLOCK = 28000
REFERENCE_INFER_BLOCK = 7000
DEFAULT_PCA_BATCH = 512
DEFAULT_IO_CHUNK = 1_000_000

# V3.2 conservative QC / abstention contract.
QC_HIGH_CLASS = np.uint8(18)
QC_LOW_CLASS = np.uint8(7)
QC_UNCATEGORIZED_CLASS = np.uint8(0)
QC_GROUND_LAS_CLASS = np.uint8(2)
QC_LOCAL_K = 128
DEFAULT_QC_BATCH = 5000
DEFAULT_GROUND_UNCAT_THRESHOLD = 0.70
# Precision-first Class-0 geometry gate. False negatives remain Premium Ground (LAS 2);
# elevated/vegetation-like points are not allowed to become Uncategorized.
DEFAULT_GROUND_UNCAT_MAX_ABS_HAG = 0.25
DEFAULT_GROUND_UNCAT_MAX_ABOVE_LOCAL_P10 = 0.25

HIGH_HAG_MIN = 10.0
HIGH_NN3D_MIN = 0.50
HIGH_ABOVE_LOCAL_P90_MIN = 2.0
HIGH_SUPPORT_2M_MAX = 3

LOW_HAG_MAX = -1.00
LOW_BELOW_LOCAL_P10_MAX = -1.20
LOW_BELOW_LOCAL_MEDIAN_MAX = -1.50
LOW_BELOW_LOCAL_P90_MAX = -1.50

STRONG_DTM = {
    "grid_size": 1.0,
    "cell_quantile": 0.05,
    "windows_m": [3, 7, 15, 31, 61],
    "thresholds_m": [0.35, 0.70, 1.40, 2.80, 5.60],
    "median_filter_cells": 3,
    "hag_min_clip": -2.0,
}

MODEL_TO_LAS = np.asarray([2, 3, 4, 5, 6], dtype=np.uint8)
CLASS_NAMES = {
    0: "Uncategorized / uncertain Ground",
    2: "Ground",
    3: "Low vegetation",
    4: "Medium vegetation",
    5: "High vegetation",
    6: "Building",
    7: "Low Point",
    18: "High Noise",
}
FINAL_CLASS_ORDER = (0, 2, 3, 4, 5, 6, 7, 18)

# Frozen identities from the verified Premium package.
EXPECTED_MODEL_SHA256 = "ed36e6eba1802fa7a50c69792e2d212c522bdac3e2cb42ac4e8d3991945fbcbe"
EXPECTED_STATS_SHA256 = "c395874c3dcb7a1f4bb319f882e0d52295a04b40657632803690d6054f317e6f"
EXPECTED_MODEL_PY_SHA256 = "8e76e094c5225cddba588affbc5edccb26db2ca17786bf3892356c0a6e61ff91"
EXPECTED_HYBRID_SHA256 = "ff3d380f6c96d4cdc880f1fb02b31352ef5c342c3fd692e68c568440e51ffd17"
EXPECTED_DTM_SHA256 = "2d2a461331654768d2a5024bfe24369b6020d3aa6ca87d426ac359ed68061a9e"

EPS = 1e-12


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_module(path: Path, name: str):
    if not path.is_file():
        raise FileNotFoundError(f"Required Python file not found: {path}")
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot import {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def find_default_model() -> Path | None:
    for path in (
        SCRIPT_DIR / "best_guarded.pth",
        SCRIPT_DIR / "best_guarded(1).pth",
        SCRIPT_DIR / "premium_best_guarded.pth",
    ):
        if path.is_file():
            return path
    return None


def verify_frozen_files(model_path: Path, stats_path: Path) -> dict[str, str]:
    paths = {
        "model": model_path,
        "stats": stats_path,
        "model_py": DEFAULT_MODEL_PY,
        "hybrid": DEFAULT_HYBRID_PY,
        "dtm": DEFAULT_DTM_PY,
    }
    expected = {
        "model": EXPECTED_MODEL_SHA256,
        "stats": EXPECTED_STATS_SHA256,
        "model_py": EXPECTED_MODEL_PY_SHA256,
        "hybrid": EXPECTED_HYBRID_SHA256,
        "dtm": EXPECTED_DTM_SHA256,
    }
    hashes: dict[str, str] = {}
    for key, path in paths.items():
        if not path.is_file():
            raise FileNotFoundError(f"Required Premium file missing: {path}")
        actual = sha256_file(path).lower()
        hashes[key] = actual
        if actual != expected[key]:
            raise RuntimeError(
                f"Frozen Premium contract mismatch for {key}: {path}\n"
                f"expected: {expected[key]}\n"
                f"actual:   {actual}\n"
                "Do not run V3 with a mixed model/extractor package."
            )
    return hashes


def resolve_workers(value: int) -> int:
    # CLI 0 means all logical CPU cores, which SciPy expresses as -1.
    if value == 0:
        return -1
    if value < -1:
        raise ValueError("--workers must be 0 (all cores), -1, or a positive integer")
    return value


def load_runtime(model_path: Path, stats_path: Path, device: torch.device):
    frozen_hashes = verify_frozen_files(model_path, stats_path)

    model_mod = load_module(DEFAULT_MODEL_PY, "premium_model_arch_v3")
    hybrid = load_module(DEFAULT_HYBRID_PY, "premium_hybrid_68f_v3")
    dtm = load_module(DEFAULT_DTM_PY, "premium_dtm_v3_fast")

    if int(hybrid.NUM_FEATURES) != NUM_FEATURES:
        raise RuntimeError(f"Hybrid extractor has {hybrid.NUM_FEATURES} features, expected 68")

    with stats_path.open("r", encoding="utf-8") as handle:
        stats = json.load(handle)

    if int(stats.get("num_features", -1)) != NUM_FEATURES:
        raise RuntimeError("feature_stats.json is not the accepted 68-feature statistics file")
    if list(stats.get("feature_names", [])) != list(hybrid.FEATURE_NAMES):
        raise RuntimeError("feature_stats feature_names do not exactly match recovered 68F order")

    feat_mean = np.asarray(stats["mean"], dtype=np.float32)
    feat_std = np.asarray(stats["std"], dtype=np.float32)
    if feat_mean.shape != (68,) or feat_std.shape != (68,):
        raise RuntimeError("feature_stats mean/std must each contain exactly 68 values")
    feat_std = np.where(feat_std < 1e-6, 1.0, feat_std).astype(np.float32)

    checkpoint = torch.load(str(model_path), map_location=device, weights_only=False)
    nf = int(checkpoint.get("num_features", -1))
    nc = int(checkpoint.get("num_classes", -1))
    if nf != NUM_FEATURES or nc != NUM_CLASSES:
        raise RuntimeError(f"Checkpoint mismatch: num_features={nf}, num_classes={nc}")

    expected_stats_hash = str(checkpoint.get("accepted_stats_sha256", "")).lower()
    if expected_stats_hash and expected_stats_hash != frozen_hashes["stats"]:
        raise RuntimeError("Checkpoint accepted_stats_sha256 does not match frozen stats")

    model = model_mod.PointNet2SSGPro(num_features=nf, num_classes=nc).to(device)
    model.load_state_dict(checkpoint["model_state_dict"], strict=True)
    model.eval()

    return model, feat_mean, feat_std, hybrid, dtm, checkpoint, frozen_hashes


def build_strong_dtm(input_path: Path, dtm):
    print("[1/5] Building label-independent DTM V3 Strong HAG surface...")
    t0 = time.time()
    seed, raster_report = dtm.build_low_quantile_raster(
        input_path,
        STRONG_DTM["grid_size"],
        STRONG_DTM["cell_quantile"],
    )
    filled = dtm.fill_nearest(seed)
    filled = median_filter(
        filled,
        size=STRONG_DTM["median_filter_cells"],
        mode="nearest",
    ).astype(np.float32)
    terrain, stages = dtm.make_progressive_dtm(
        filled,
        STRONG_DTM["grid_size"],
        STRONG_DTM["windows_m"],
        STRONG_DTM["thresholds_m"],
    )
    del seed, filled
    gc.collect()
    print(f"      DTM ready in {time.time() - t0:.1f}s | raster={terrain.shape}")
    return terrain, raster_report, stages


def read_laz_inference_safe_streaming(path: Path, io_chunk: int) -> dict[str, Any]:
    """Stream LAS/LAZ into compact inference arrays.

    XYZ stays float64 because that is part of the recovered production contract.
    Intensity/returns stay in their integer storage types and are converted to
    float32 only for each query block.  This lowers RAM for 20M+ point files.
    """
    started = time.time()
    with laspy.open(str(path), mode="r") as reader:
        header_obj = reader.header
        n = int(header_obj.point_count)
        if n <= 0:
            raise RuntimeError("Input contains zero points")

        xyz = np.empty((n, 3), dtype=np.float64)
        intensity_raw = np.empty(n, dtype=np.uint16)
        return_number = np.empty(n, dtype=np.uint8)
        num_returns = np.empty(n, dtype=np.uint8)
        invalid_returns = np.empty(n, dtype=np.bool_)

        pos = 0
        for points in reader.chunk_iterator(int(io_chunk)):
            m = len(points)
            sl = slice(pos, pos + m)
            xyz[sl, 0] = np.asarray(points.x, dtype=np.float64)
            xyz[sl, 1] = np.asarray(points.y, dtype=np.float64)
            xyz[sl, 2] = np.asarray(points.z, dtype=np.float64)
            intensity_raw[sl] = np.asarray(points.intensity, dtype=np.uint16)

            rn = np.asarray(points.return_number, dtype=np.uint8)
            nr = np.asarray(points.number_of_returns, dtype=np.uint8)
            invalid = (rn < 1) | (nr < 1) | (rn > nr)
            rn_safe = rn.copy()
            nr_safe = nr.copy()
            rn_safe[invalid] = 1
            nr_safe[invalid] = 1
            return_number[sl] = rn_safe
            num_returns[sl] = nr_safe
            invalid_returns[sl] = invalid
            pos += m

        if pos != n:
            raise RuntimeError(f"Streamed {pos:,} points but header declares {n:,}")

        header = {
            "point_count": n,
            "point_format": int(header_obj.point_format.id),
            "las_version": str(header_obj.version),
            "scales": np.asarray(header_obj.scales, dtype=float).tolist(),
            "offsets": np.asarray(header_obj.offsets, dtype=float).tolist(),
            "mins": np.asarray(header_obj.mins, dtype=float).tolist(),
            "maxs": np.asarray(header_obj.maxs, dtype=float).tolist(),
            "generating_software": str(header_obj.generating_software).strip(),
            "read_seconds": float(time.time() - started),
        }

    if not np.isfinite(xyz).all():
        raise ValueError("XYZ contains NaN or infinity")

    return {
        "xyz": xyz,
        "intensity_raw": intensity_raw,
        "return_number": return_number,
        "num_returns": num_returns,
        "invalid_return_mask": invalid_returns,
        "invalid_return_count": int(np.count_nonzero(invalid_returns)),
        "header": header,
    }


@dataclass
class TileFeatureCache:
    raw_xyz: np.ndarray
    raw_tree: cKDTree
    voxel_015: np.ndarray
    tree_015: cKDTree | None
    voxel_030: np.ndarray
    tree_030: cKDTree | None


def make_tile_cache(raw_context_xyz: np.ndarray, hybrid) -> TileFeatureCache:
    """Build all deterministic context structures once for a tile."""
    raw_tree = cKDTree(raw_context_xyz)
    voxel_015 = hybrid.voxel_centroids(raw_context_xyz, VOXEL_SIZE_R2)
    voxel_030 = hybrid.voxel_centroids(raw_context_xyz, VOXEL_SIZE_LARGE)
    tree_015 = cKDTree(voxel_015) if len(voxel_015) else None
    tree_030 = cKDTree(voxel_030) if len(voxel_030) else None
    return TileFeatureCache(
        raw_xyz=raw_context_xyz,
        raw_tree=raw_tree,
        voxel_015=voxel_015,
        tree_015=tree_015,
        voxel_030=voxel_030,
        tree_030=tree_030,
    )


def strong_hag(
    query_xyz: np.ndarray,
    terrain: np.ndarray,
    raster_report: dict[str, Any],
    dtm,
) -> np.ndarray:
    terrain_z = dtm.bilinear(
        terrain,
        query_xyz[:, 0],
        query_xyz[:, 1],
        float(raster_report["x_min"]),
        float(raster_report["y_min"]),
        STRONG_DTM["grid_size"],
    ).astype(np.float64)
    return np.clip(
        query_xyz[:, 2].astype(np.float64) - terrain_z,
        STRONG_DTM["hag_min_clip"],
        None,
    ).astype(np.float32)


def raw_signed_hag_all(
    xyz: np.ndarray,
    terrain: np.ndarray,
    raster_report: dict[str, Any],
    dtm,
    chunk_size: int = 500_000,
) -> np.ndarray:
    """Return the un-clipped signed terrain residual used only by V3.2 QC.

    IMPORTANT: this must never replace strong_hag() in the 68F model feature
    path. The Premium model keeps its accepted -2 m HAG clipping exactly.
    """
    n = len(xyz)
    out = np.empty(n, dtype=np.float32)
    x_min = float(raster_report["x_min"])
    y_min = float(raster_report["y_min"])
    for start in range(0, n, int(chunk_size)):
        end = min(start + int(chunk_size), n)
        terrain_z = dtm.bilinear(
            terrain,
            xyz[start:end, 0],
            xyz[start:end, 1],
            x_min,
            y_min,
            STRONG_DTM["grid_size"],
        ).astype(np.float64)
        out[start:end] = (
            xyz[start:end, 2].astype(np.float64) - terrain_z
        ).astype(np.float32)
    return out


def density_counts_fast(query_xyz: np.ndarray, tree: cKDTree, workers: int) -> np.ndarray:
    try:
        counts = tree.query_ball_point(
            query_xyz,
            r=DENSITY_RADIUS,
            return_length=True,
            workers=workers,
        )
        return np.asarray(counts, dtype=np.float32)
    except TypeError:
        lists = tree.query_ball_point(query_xyz, r=DENSITY_RADIUS, workers=workers)
        return np.asarray([len(v) for v in lists], dtype=np.float32)


def roughness_zstd_fast(
    query_xyz: np.ndarray,
    context_xyz: np.ndarray,
    tree: cKDTree,
    workers: int,
) -> np.ndarray:
    k_use = min(max(int(ROUGHNESS_K), 2), context_xyz.shape[0])
    _, indices = tree.query(query_xyz, k=k_use, workers=workers)
    if indices.ndim == 1:
        indices = indices[:, None]
    neighbours = context_xyz[indices]
    # Keep V2's exact expression, including subtracting the query Z first.
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


def descriptors_from_covariances(cov: np.ndarray) -> np.ndarray:
    if cov.shape[0] == 0:
        return np.empty((0, 11), dtype=np.float32)

    eigenvalues, eigenvectors = np.linalg.eigh(cov)
    eigenvalues = np.maximum(eigenvalues, 0.0)[:, ::-1]
    eigenvectors = eigenvectors[:, :, ::-1]

    l1 = eigenvalues[:, 0]
    l2 = eigenvalues[:, 1]
    l3 = eigenvalues[:, 2]
    total = l1 + l2 + l3 + EPS

    linearity = (l1 - l2) / (l1 + EPS)
    planarity = (l2 - l3) / (l1 + EPS)
    sphericity = l3 / (l1 + EPS)
    omnivariance = np.cbrt(np.maximum(l1 * l2 * l3, 0.0))
    anisotropy = (l1 - l3) / (l1 + EPS)
    eigenentropy = -(
        l1 * np.log(l1 + EPS)
        + l2 * np.log(l2 + EPS)
        + l3 * np.log(l3 + EPS)
    )
    surface_variation = l3 / total
    # After reversing eigenvector columns, column 2 is the smallest-eigenvalue normal.
    normal_z = eigenvectors[:, 2, 2]
    verticality = 1.0 - np.abs(normal_z)

    out = np.column_stack(
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
        ]
    )
    return np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)


def covariances_reference_loop(
    query_xyz: np.ndarray,
    context_xyz: np.ndarray,
    tree: cKDTree,
    radius: float,
    workers: int,
) -> np.ndarray:
    """Same per-neighbour covariance expression as V2, batched only at eigh."""
    lists = tree.query_ball_point(query_xyz, r=float(radius), workers=workers)
    cov = np.empty((len(query_xyz), 3, 3), dtype=np.float64)

    small_positions = [i for i, idx in enumerate(lists) if len(idx) < 3]
    fallback_map: dict[int, np.ndarray] = {}
    if small_positions:
        if len(context_xyz) < 3:
            cov.fill(0.0)
            return cov
        _, fb = tree.query(
            query_xyz[np.asarray(small_positions, dtype=np.int64)],
            k=min(3, len(context_xyz)),
            workers=workers,
        )
        fb = np.asarray(fb)
        if fb.ndim == 1:
            fb = fb[:, None]
        for row, pos in enumerate(small_positions):
            fallback_map[pos] = np.atleast_1d(fb[row]).astype(np.int64, copy=False)

    for i, neighbour_indices in enumerate(lists):
        if len(neighbour_indices) < 3:
            idx = fallback_map.get(i)
            if idx is None or len(idx) < 3:
                cov[i] = 0.0
                continue
        else:
            idx = np.asarray(neighbour_indices, dtype=np.int64)
        pts = context_xyz[idx]
        centred = pts.astype(np.float64) - pts.mean(axis=0, keepdims=True)
        cov[i] = (centred.T @ centred) / max(len(pts) - 1, 1)
    return cov


def covariances_vectorized(
    query_xyz: np.ndarray,
    context_xyz: np.ndarray,
    tree: cKDTree,
    radius: float,
    workers: int,
) -> np.ndarray:
    """Vectorized two-pass centered covariance for variable-radius neighbours."""
    b = len(query_xyz)
    if b == 0:
        return np.empty((0, 3, 3), dtype=np.float64)
    if len(context_xyz) < 3:
        return np.zeros((b, 3, 3), dtype=np.float64)

    lists = tree.query_ball_point(query_xyz, r=float(radius), workers=workers)
    counts = np.fromiter((len(idx) for idx in lists), dtype=np.int64, count=b)
    small = np.flatnonzero(counts < 3)
    if small.size:
        _, fb = tree.query(query_xyz[small], k=3, workers=workers)
        fb = np.asarray(fb, dtype=np.int64)
        if fb.ndim == 1:
            fb = fb[:, None]
        for row, pos in enumerate(small):
            lists[int(pos)] = np.atleast_1d(fb[row]).tolist()
        counts[small] = 3

    total_neighbours = int(counts.sum())
    if total_neighbours == 0:
        return np.zeros((b, 3, 3), dtype=np.float64)

    flat = np.concatenate(
        [np.asarray(idx, dtype=np.int64) for idx in lists],
        axis=0,
    )
    starts = np.empty(b, dtype=np.int64)
    starts[0] = 0
    if b > 1:
        np.cumsum(counts[:-1], out=starts[1:])

    pts = context_xyz[flat].astype(np.float64, copy=False)
    sums = np.add.reduceat(pts, starts, axis=0)
    means = sums / counts[:, None]
    group_ids = np.repeat(np.arange(b, dtype=np.int64), counts)
    centered = pts - means[group_ids]

    x = centered[:, 0]
    y = centered[:, 1]
    z = centered[:, 2]
    products = np.column_stack((x * x, y * y, z * z, x * y, x * z, y * z))
    sums2 = np.add.reduceat(products, starts, axis=0)
    denom = np.maximum(counts - 1, 1).astype(np.float64)

    cov = np.empty((b, 3, 3), dtype=np.float64)
    cov[:, 0, 0] = sums2[:, 0] / denom
    cov[:, 1, 1] = sums2[:, 1] / denom
    cov[:, 2, 2] = sums2[:, 2] / denom
    cov[:, 0, 1] = cov[:, 1, 0] = sums2[:, 3] / denom
    cov[:, 0, 2] = cov[:, 2, 0] = sums2[:, 4] / denom
    cov[:, 1, 2] = cov[:, 2, 1] = sums2[:, 5] / denom
    return cov


def radius_pca_features_fast(
    query_xyz: np.ndarray,
    context_xyz: np.ndarray,
    tree: cKDTree | None,
    radius: float,
    pca_batch_size: int,
    workers: int,
    pca_engine: str,
) -> np.ndarray:
    out = np.zeros((len(query_xyz), 11), dtype=np.float32)
    if len(query_xyz) == 0 or tree is None or len(context_xyz) < 3:
        return out

    cov_fn = covariances_vectorized if pca_engine == "vectorized" else covariances_reference_loop

    for start in range(0, len(query_xyz), pca_batch_size):
        end = min(start + pca_batch_size, len(query_xyz))
        cov = cov_fn(
            query_xyz[start:end],
            context_xyz,
            tree,
            radius,
            workers,
        )
        out[start:end] = descriptors_from_covariances(cov)
    return out


def build_features_fast(
    query_xyz: np.ndarray,
    qidx: np.ndarray,
    center_x: float,
    center_y: float,
    cache: TileFeatureCache,
    loaded: dict[str, Any],
    terrain: np.ndarray,
    raster_report: dict[str, Any],
    dtm,
    feat_mean: np.ndarray,
    feat_std: np.ndarray,
    workers: int,
    pca_batch_size: int,
    pca_engine: str,
    return_raw: bool = False,
):
    n = len(query_xyz)
    features = np.empty((n, NUM_FEATURES), dtype=np.float32)

    # Base 13 features.
    features[:, 0] = (query_xyz[:, 0] - center_x).astype(np.float32)
    features[:, 1] = (query_xyz[:, 1] - center_y).astype(np.float32)
    features[:, 2] = query_xyz[:, 2].astype(np.float32)
    features[:, 3] = strong_hag(query_xyz, terrain, raster_report, dtm)
    features[:, 4] = (
        loaded["intensity_raw"][qidx].astype(np.float32) / np.float32(65535.0)
    )

    rn = loaded["return_number"][qidx].astype(np.float32)
    nr = loaded["num_returns"][qidx].astype(np.float32)
    features[:, 5] = rn
    features[:, 6] = nr
    features[:, 7] = rn / np.maximum(nr, 1.0)
    features[:, 8] = (nr == 1.0).astype(np.float32)
    features[:, 9] = (rn == 1.0).astype(np.float32)
    features[:, 10] = (rn == nr).astype(np.float32)
    features[:, 11] = density_counts_fast(query_xyz, cache.raw_tree, workers)
    features[:, 12] = roughness_zstd_fast(query_xyz, cache.raw_xyz, cache.raw_tree, workers)

    geometry = (
        (0.5, cache.raw_xyz, cache.raw_tree),
        (1.0, cache.raw_xyz, cache.raw_tree),
        (2.0, cache.voxel_015, cache.tree_015),
        (5.0, cache.voxel_030, cache.tree_030),
        (10.0, cache.voxel_030, cache.tree_030),
    )
    col = 13
    for radius, context, tree in geometry:
        features[:, col:col + 11] = radius_pca_features_fast(
            query_xyz=query_xyz,
            context_xyz=context,
            tree=tree,
            radius=radius,
            pca_batch_size=pca_batch_size,
            workers=workers,
            pca_engine=pca_engine,
        )
        col += 11

    invalid_q = loaded["invalid_return_mask"][qidx]
    if np.any(invalid_q):
        features[invalid_q, 5:11] = feat_mean[5:11]

    if not np.isfinite(features).all():
        raise RuntimeError("Fast feature matrix contains NaN or infinity")
    if np.max(np.abs(features[:, 0])) > TILE_SIZE / 2.0 + 0.01:
        raise RuntimeError("Local X exceeds tile limits")
    if np.max(np.abs(features[:, 1])) > TILE_SIZE / 2.0 + 0.01:
        raise RuntimeError("Local Y exceeds tile limits")

    normalized = (features - feat_mean) / feat_std
    np.clip(normalized, -10.0, 10.0, out=normalized)
    normalized = np.nan_to_num(
        normalized,
        nan=0.0,
        posinf=10.0,
        neginf=-10.0,
    ).astype(np.float32, copy=False)

    if return_raw:
        return features, normalized
    return normalized


def seed_torch_for_inference(seed: int, device: torch.device) -> None:
    """Make PointNet++ FPS reproducible without modifying the frozen model.py.

    The frozen architecture intentionally starts farthest-point sampling from a
    random point via torch.randint().  Without resetting the Torch RNG, two
    forwards on byte-identical 68F tensors can disagree even though the feature
    pipeline is exactly equivalent.  We derive the seed from the same stable
    per-tile/per-7k-block seed already used by the recovered V2 inference path.
    """
    safe_seed = int(seed) % (2**63 - 1)
    torch.manual_seed(safe_seed)
    if device.type == "cuda":
        torch.cuda.manual_seed_all(safe_seed)



def infer_model_blocks_fast(
    query_xyz: np.ndarray,
    normalized_features: np.ndarray,
    model,
    device: torch.device,
    vote_passes: int,
    batch_size: int,
    seed_base: int,
):
    # Critical V3.1 fix: synchronize the Torch RNG used by PointNet++
    # farthest-point sampling.  NumPy block order was already deterministic.
    seed_torch_for_inference(seed_base, device)

    n = len(query_xyz)
    prob_sum = np.zeros((n, NUM_CLASSES), dtype=np.float32)
    hit_count = np.zeros(n, dtype=np.uint16)
    use_amp = device.type == "cuda"

    for vote_pass in range(vote_passes):
        rng = np.random.default_rng(seed_base + vote_pass * 1000003)
        order = rng.permutation(n)

        starts = list(range(0, n, MODEL_NUM_POINTS))
        for batch_start in range(0, len(starts), batch_size):
            specs = []
            coords_list = []
            feats_list = []

            for start in starts[batch_start:batch_start + batch_size]:
                real_idx = order[start:start + MODEL_NUM_POINTS]
                real_count = len(real_idx)
                if real_count == 0:
                    continue
                if real_count < MODEL_NUM_POINTS:
                    extra = rng.choice(real_idx, MODEL_NUM_POINTS - real_count, replace=True)
                    model_idx = np.concatenate([real_idx, extra])
                else:
                    model_idx = real_idx

                coords = query_xyz[model_idx].astype(np.float32, copy=True)
                coords -= coords.mean(axis=0, keepdims=True)
                feats = normalized_features[model_idx].astype(np.float32, copy=False)
                specs.append((real_idx, real_count))
                coords_list.append(coords)
                feats_list.append(feats)

            if not specs:
                continue

            coords_np = np.stack(coords_list, axis=0)
            feats_np = np.stack(feats_list, axis=0)
            coords_t = torch.from_numpy(coords_np).to(device, non_blocking=True)
            feats_t = torch.from_numpy(feats_np).to(device, non_blocking=True)

            with torch.inference_mode():
                if use_amp:
                    with torch.amp.autocast("cuda"):
                        logits = model(coords_t, feats_t)
                else:
                    logits = model(coords_t, feats_t)
                probs = torch.softmax(logits, dim=-1).float().cpu().numpy()

            for row, (real_idx, real_count) in enumerate(specs):
                prob_sum[real_idx] += probs[row, :real_count]
                hit_count[real_idx] += 1

            del coords_t, feats_t, logits, probs, coords_np, feats_np

    safe_hits = np.maximum(hit_count.astype(np.float32), 1.0)[:, None]
    mean_probs = prob_sum / safe_hits
    pred = mean_probs.argmax(axis=1).astype(np.uint8)
    confidence = mean_probs.max(axis=1).astype(np.float32)
    return pred, confidence


def _qc_query_k(xyz: np.ndarray) -> int:
    return min(int(QC_LOCAL_K), int(len(xyz)))


def detect_high_noise_indices(
    xyz: np.ndarray,
    xy_tree: cKDTree,
    raw_hag: np.ndarray,
    workers: int,
    batch_size: int,
) -> np.ndarray:
    """Validated High Noise V1 rule -> LAS 18.

    Validation contract reproduced:
    raw HAG > 10 m; nearest 3D neighbour > 0.50 m;
    Z - local XY-neighbour P90 > 2.0 m; <=3 other 3D points within 2 m.
    """
    prefilter = np.flatnonzero(raw_hag > HIGH_HAG_MIN).astype(np.int64)
    print(f"      High HAG prefilter (> {HIGH_HAG_MIN:.2f} m): {len(prefilter):,}")
    if len(prefilter) == 0 or len(xyz) < 2:
        return np.empty(0, dtype=np.int64)

    print("      Building temporary global 3D tree for exact High Noise isolation...")
    tree3 = cKDTree(xyz)

    isolated_parts: list[np.ndarray] = []
    nn_batch = max(int(batch_size) * 20, 50_000)
    for start in range(0, len(prefilter), nn_batch):
        qidx = prefilter[start:start + nn_batch]
        dist3, _ = tree3.query(xyz[qidx], k=2, workers=workers)
        isolated_parts.append(qidx[np.asarray(dist3)[:, 1] > HIGH_NN3D_MIN])
    high_iso = (
        np.concatenate(isolated_parts)
        if isolated_parts
        else np.empty(0, dtype=np.int64)
    )
    print(f"      After nearest-3D isolation (> {HIGH_NN3D_MIN:.2f} m): {len(high_iso):,}")
    if len(high_iso) == 0:
        del tree3
        return np.empty(0, dtype=np.int64)

    k_use = _qc_query_k(xyz)
    if k_use < 2:
        del tree3
        return np.empty(0, dtype=np.int64)

    z = xyz[:, 2]
    xy = xyz[:, :2]
    keep_parts: list[np.ndarray] = []
    for start in range(0, len(high_iso), int(batch_size)):
        qidx = high_iso[start:start + int(batch_size)]
        _, nbr = xy_tree.query(xy[qidx], k=k_use, workers=workers)
        nbr = np.asarray(nbr)
        if nbr.ndim == 1:
            nbr = nbr[:, None]
        neighbour_z = z[nbr[:, 1:]]
        local_p90 = np.percentile(neighbour_z, 90, axis=1)
        z_above_p90 = z[qidx] - local_p90

        support_2m = np.asarray(
            tree3.query_ball_point(
                xyz[qidx],
                r=2.0,
                workers=workers,
                return_length=True,
            ),
            dtype=np.int64,
        ) - 1  # subtract self

        keep = (
            (z_above_p90 > HIGH_ABOVE_LOCAL_P90_MIN)
            & (support_2m <= HIGH_SUPPORT_2M_MAX)
        )
        keep_parts.append(qidx[keep])

    out = (
        np.sort(np.concatenate(keep_parts)).astype(np.int64, copy=False)
        if keep_parts
        else np.empty(0, dtype=np.int64)
    )
    del tree3
    print(f"      High Noise final (LAS 18): {len(out):,}")
    return out


def detect_low_point_indices(
    xyz: np.ndarray,
    xy_tree: cKDTree,
    raw_hag: np.ndarray,
    workers: int,
    batch_size: int,
) -> np.ndarray:
    """Validated conservative STRICT_B Low Point rule -> LAS 7."""
    prefilter = np.flatnonzero(raw_hag < LOW_HAG_MAX).astype(np.int64)
    print(f"      Low HAG prefilter (< {LOW_HAG_MAX:.2f} m): {len(prefilter):,}")
    if len(prefilter) == 0:
        return np.empty(0, dtype=np.int64)

    k_use = _qc_query_k(xyz)
    if k_use < 2:
        return np.empty(0, dtype=np.int64)

    z = xyz[:, 2]
    xy = xyz[:, :2]
    keep_parts: list[np.ndarray] = []
    for start in range(0, len(prefilter), int(batch_size)):
        qidx = prefilter[start:start + int(batch_size)]
        _, nbr = xy_tree.query(xy[qidx], k=k_use, workers=workers)
        nbr = np.asarray(nbr)
        if nbr.ndim == 1:
            nbr = nbr[:, None]
        neighbour_z = z[nbr[:, 1:]]
        local_p10 = np.percentile(neighbour_z, 10, axis=1)
        local_median = np.median(neighbour_z, axis=1)
        local_p90 = np.percentile(neighbour_z, 90, axis=1)

        keep = (
            ((z[qidx] - local_p10) < LOW_BELOW_LOCAL_P10_MAX)
            & ((z[qidx] - local_median) < LOW_BELOW_LOCAL_MEDIAN_MAX)
            & ((z[qidx] - local_p90) < LOW_BELOW_LOCAL_P90_MAX)
        )
        keep_parts.append(qidx[keep])

    out = (
        np.sort(np.concatenate(keep_parts)).astype(np.int64, copy=False)
        if keep_parts
        else np.empty(0, dtype=np.int64)
    )
    print(f"      Low Point STRICT_B final (LAS 7): {len(out):,}")
    return out


def detect_ground_uncategorized_indices(
    premium_las: np.ndarray,
    final_las: np.ndarray,
    confidence: np.ndarray,
    xyz: np.ndarray,
    xy_tree: cKDTree,
    raw_hag: np.ndarray,
    workers: int,
    batch_size: int,
    confidence_threshold: float,
    max_abs_hag: float,
    max_above_local_p10: float,
) -> tuple[np.ndarray, dict[str, int]]:
    """Precision-first Ground-only Uncategorized rule -> LAS 0.

    A point is eligible only when the frozen Premium core predicted LAS 2 Ground,
    the point has not already become Low Point / High Noise, model confidence is
    below the calibrated threshold, RAW signed HAG is tightly near the DTM, and
    the point lies close to the local lower XY-neighbour surface.

    This deliberately prefers false negatives (remain LAS 2) over false-positive
    Class 0 points on roofs or vegetation.
    """
    base_mask = (
        (premium_las == QC_GROUND_LAS_CLASS)
        & (final_las == QC_GROUND_LAS_CLASS)
        & (confidence < float(confidence_threshold))
    )
    base_idx = np.flatnonzero(base_mask).astype(np.int64)
    if len(base_idx) == 0:
        return np.empty(0, dtype=np.int64), {
            "low_confidence_premium_ground": 0,
            "after_raw_hag_band": 0,
            "after_local_p10_gate": 0,
        }

    hag_keep = np.abs(raw_hag[base_idx]) <= float(max_abs_hag)
    hag_idx = base_idx[hag_keep]
    if len(hag_idx) == 0:
        return np.empty(0, dtype=np.int64), {
            "low_confidence_premium_ground": int(len(base_idx)),
            "after_raw_hag_band": 0,
            "after_local_p10_gate": 0,
        }

    k_use = _qc_query_k(xyz)
    if k_use < 2:
        return np.empty(0, dtype=np.int64), {
            "low_confidence_premium_ground": int(len(base_idx)),
            "after_raw_hag_band": int(len(hag_idx)),
            "after_local_p10_gate": 0,
        }

    z = xyz[:, 2]
    xy = xyz[:, :2]
    keep_parts: list[np.ndarray] = []
    for start in range(0, len(hag_idx), int(batch_size)):
        qidx = hag_idx[start:start + int(batch_size)]
        _, nbr = xy_tree.query(xy[qidx], k=k_use, workers=workers)
        nbr = np.asarray(nbr)
        if nbr.ndim == 1:
            nbr = nbr[:, None]
        neighbour_z = z[nbr[:, 1:]]
        local_p10 = np.percentile(neighbour_z, 10, axis=1)
        above_local_p10 = z[qidx] - local_p10
        keep = above_local_p10 <= float(max_above_local_p10)
        keep_parts.append(qidx[keep])

    out = (
        np.sort(np.concatenate(keep_parts)).astype(np.int64, copy=False)
        if keep_parts
        else np.empty(0, dtype=np.int64)
    )
    return out, {
        "low_confidence_premium_ground": int(len(base_idx)),
        "after_raw_hag_band": int(len(hag_idx)),
        "after_local_p10_gate": int(len(out)),
    }


def _transition_report(premium_las: np.ndarray, final_las: np.ndarray) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for src in (2, 3, 4, 5, 6):
        src_mask = premium_las == src
        src_count = int(np.count_nonzero(src_mask))
        destinations: dict[str, int] = {}
        if src_count:
            vals, counts = np.unique(final_las[src_mask], return_counts=True)
            for dst, count in zip(vals, counts):
                destinations[str(int(dst))] = int(count)
        report[str(src)] = {
            "source_name": CLASS_NAMES[int(src)],
            "source_points": src_count,
            "final_destinations": destinations,
        }
    return report


def apply_qc_postprocessing(
    premium_las: np.ndarray,
    confidence: np.ndarray,
    xyz: np.ndarray,
    xy_tree: cKDTree,
    raw_hag: np.ndarray,
    workers: int,
    batch_size: int,
    ground_uncat_threshold: float,
    ground_uncat_max_abs_hag: float = DEFAULT_GROUND_UNCAT_MAX_ABS_HAG,
    ground_uncat_max_above_local_p10: float = DEFAULT_GROUND_UNCAT_MAX_ABOVE_LOCAL_P10,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Apply conservative QC in the locked order: 18 -> 7 -> Ground-only 0.

    Safety invariant: every point not changed to 0/7/18 must remain byte-identical
    to the Premium LAS class produced by the V3.1 core.
    """
    premium_las = np.asarray(premium_las, dtype=np.uint8)
    confidence = np.asarray(confidence, dtype=np.float32)
    if premium_las.shape != confidence.shape or premium_las.shape[0] != len(xyz):
        raise RuntimeError("QC input arrays have inconsistent lengths")
    if raw_hag.shape != premium_las.shape:
        raise RuntimeError("QC raw HAG length mismatch")

    final_las = premium_las.copy()

    high_idx = detect_high_noise_indices(
        xyz, xy_tree, raw_hag, workers, batch_size
    )
    if len(high_idx):
        final_las[high_idx] = QC_HIGH_CLASS

    low_idx = detect_low_point_indices(
        xyz, xy_tree, raw_hag, workers, batch_size
    )
    if len(low_idx):
        # HAG gates are mutually exclusive, but keep explicit precedence anyway.
        low_idx = low_idx[final_las[low_idx] != QC_HIGH_CLASS]
        final_las[low_idx] = QC_LOW_CLASS

    uncat_idx, uncat_stage_counts = detect_ground_uncategorized_indices(
        premium_las=premium_las,
        final_las=final_las,
        confidence=confidence,
        xyz=xyz,
        xy_tree=xy_tree,
        raw_hag=raw_hag,
        workers=workers,
        batch_size=batch_size,
        confidence_threshold=ground_uncat_threshold,
        max_abs_hag=ground_uncat_max_abs_hag,
        max_above_local_p10=ground_uncat_max_above_local_p10,
    )
    if len(uncat_idx):
        final_las[uncat_idx] = QC_UNCATEGORIZED_CLASS

    qc_mask = np.isin(final_las, np.asarray([0, 7, 18], dtype=np.uint8))
    unchanged_mask = ~qc_mask
    if not np.array_equal(final_las[unchanged_mask], premium_las[unchanged_mask]):
        raise RuntimeError("QC safety invariant failed: a non-QC Premium class changed")
    if len(uncat_idx) and np.any(premium_las[uncat_idx] != QC_GROUND_LAS_CLASS):
        raise RuntimeError("QC safety invariant failed: Uncategorized contains non-Ground source points")
    if len(uncat_idx) and np.any(
        np.abs(raw_hag[uncat_idx]) > float(ground_uncat_max_abs_hag) + 1e-6
    ):
        raise RuntimeError("QC safety invariant failed: Uncategorized escaped RAW HAG ground band")
    if np.any(final_las == 1):
        raise RuntimeError("V3.2 must never generate LAS class 1")

    report = {
        "high_noise_points": int(len(high_idx)),
        "low_point_points": int(len(low_idx)),
        "uncategorized_ground_points": int(len(uncat_idx)),
        "uncategorized_ground_percent_of_premium_ground": float(
            100.0 * len(uncat_idx) / max(int(np.count_nonzero(premium_las == 2)), 1)
        ),
        "uncategorized_ground_stage_counts": uncat_stage_counts,
        "thresholds": {
            "high_hag_gt_m": HIGH_HAG_MIN,
            "high_nearest_3d_gt_m": HIGH_NN3D_MIN,
            "high_z_minus_local_p90_gt_m": HIGH_ABOVE_LOCAL_P90_MIN,
            "high_3d_support_within_2m_max": HIGH_SUPPORT_2M_MAX,
            "low_hag_lt_m": LOW_HAG_MAX,
            "low_z_minus_local_p10_lt_m": LOW_BELOW_LOCAL_P10_MAX,
            "low_z_minus_local_median_lt_m": LOW_BELOW_LOCAL_MEDIAN_MAX,
            "low_z_minus_local_p90_lt_m": LOW_BELOW_LOCAL_P90_MAX,
            "ground_uncategorized_confidence_lt": float(ground_uncat_threshold),
            "ground_uncategorized_abs_raw_hag_lte_m": float(ground_uncat_max_abs_hag),
            "ground_uncategorized_z_minus_local_p10_lte_m": float(ground_uncat_max_above_local_p10),
            "local_xy_k": int(QC_LOCAL_K),
        },
        "transition_report": _transition_report(premium_las, final_las),
        "safety_invariant_non_qc_exact": True,
        "class1_generated": False,
    }
    print(
        "      Ground-only Uncategorized gate: "
        f"low-conf Premium Ground={uncat_stage_counts['low_confidence_premium_ground']:,} "
        f"-> |RAW HAG|<={ground_uncat_max_abs_hag:.2f}m "
        f"{uncat_stage_counts['after_raw_hag_band']:,} "
        f"-> Z-localP10<={ground_uncat_max_above_local_p10:.2f}m "
        f"LAS0={len(uncat_idx):,}"
    )
    return final_las, report


def progress_paths(output_path: Path) -> dict[str, Path]:
    work = output_path.parent / (output_path.name + ".v321qc_work")
    return {
        "work": work,
        "pred": work / "predictions.uint8.mmap",
        "owner": work / "owner.int32.mmap",
        "conf": work / "confidence.float32.mmap",
        "progress": work / "progress.json",
    }


def init_or_resume_work(
    output_path: Path,
    n_total: int,
    tile_count: int,
    input_path: Path,
    frozen_hashes: dict[str, str],
    settings: dict[str, Any],
    resume: bool,
    need_confidence: bool,
):
    p = progress_paths(output_path)
    work = p["work"]

    identity = {
        "input": str(input_path.resolve()),
        "point_count": int(n_total),
        "tile_count": int(tile_count),
        "model_sha256": frozen_hashes["model"],
        "stats_sha256": frozen_hashes["stats"],
        "settings": settings,
    }

    if work.exists():
        if not resume:
            raise RuntimeError(
                f"V3 work directory already exists: {work}\n"
                "Use --resume to continue it, or delete the work directory to start over."
            )
        if not p["progress"].is_file():
            raise RuntimeError("Resume requested but progress.json is missing")
        saved = json.loads(p["progress"].read_text(encoding="utf-8"))
        for key in ("input", "point_count", "tile_count", "model_sha256", "stats_sha256", "settings"):
            if saved.get(key) != identity.get(key):
                raise RuntimeError(f"Resume state mismatch for {key}; refusing unsafe resume")
        completed = set(int(x) for x in saved.get("completed_tiles", []))
        predictions = np.memmap(p["pred"], dtype=np.uint8, mode="r+", shape=(n_total,))
        owner = np.memmap(p["owner"], dtype=np.int32, mode="r+", shape=(n_total,))
        confidence = None
        if need_confidence:
            confidence = np.memmap(p["conf"], dtype=np.float32, mode="r+", shape=(n_total,))

        # A crash can leave writes from the first incomplete tile.  Remove only
        # points owned by that tile, preserving boundary points owned by earlier tiles.
        first_incomplete = next((i for i in range(1, tile_count + 1) if i not in completed), None)
        if first_incomplete is not None:
            partial = owner == first_incomplete
            if np.any(partial):
                predictions[partial] = 255
                owner[partial] = 0
                if confidence is not None:
                    confidence[partial] = 0.0
                predictions.flush()
                owner.flush()
                if confidence is not None:
                    confidence.flush()
        print(f"      RESUME: {len(completed)}/{tile_count} tiles already complete")
        return predictions, owner, confidence, completed, p, identity

    work.mkdir(parents=True, exist_ok=False)
    predictions = np.memmap(p["pred"], dtype=np.uint8, mode="w+", shape=(n_total,))
    predictions[:] = 255
    predictions.flush()
    owner = np.memmap(p["owner"], dtype=np.int32, mode="w+", shape=(n_total,))
    owner[:] = 0
    owner.flush()
    confidence = None
    if need_confidence:
        confidence = np.memmap(p["conf"], dtype=np.float32, mode="w+", shape=(n_total,))
        confidence[:] = 0.0
        confidence.flush()
    completed: set[int] = set()
    state = dict(identity)
    state["completed_tiles"] = []
    p["progress"].write_text(json.dumps(state, indent=2), encoding="utf-8")
    return predictions, owner, confidence, completed, p, identity


def save_progress(
    completed: set[int],
    p: dict[str, Path],
    identity: dict[str, Any],
    predictions: np.memmap,
    owner: np.memmap,
    confidence: np.memmap | None,
):
    predictions.flush()
    owner.flush()
    if confidence is not None:
        confidence.flush()
    state = dict(identity)
    state["completed_tiles"] = sorted(completed)
    temp = p["progress"].with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temp, p["progress"])


def mark_tile_complete(
    tile_no: int,
    completed: set[int],
    p: dict[str, Path],
    identity: dict[str, Any],
    predictions: np.memmap,
    owner: np.memmap,
    confidence: np.memmap | None,
):
    completed.add(int(tile_no))
    save_progress(completed, p, identity, predictions, owner, confidence)


def write_classified_streaming(
    input_path: Path,
    output_path: Path,
    las_classes: np.ndarray,
    io_chunk: int,
):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(output_path.stem + ".partial" + output_path.suffix)
    if tmp_path.exists():
        tmp_path.unlink()

    try:
        with laspy.open(str(input_path), mode="r") as reader:
            if int(reader.header.point_count) != len(las_classes):
                raise RuntimeError("Input point count changed between read and write")
            header = copy.deepcopy(reader.header)
            do_compress = output_path.suffix.lower() == ".laz"
            with laspy.open(
                str(tmp_path),
                mode="w",
                header=header,
                do_compress=do_compress,
            ) as writer:
                offset = 0
                for points in reader.chunk_iterator(int(io_chunk)):
                    m = len(points)
                    points["classification"] = np.asarray(
                        las_classes[offset:offset + m],
                        dtype=np.uint8,
                    )
                    writer.write_points(points)
                    offset += m
                if offset != len(las_classes):
                    raise RuntimeError("Output writer point count mismatch")
        os.replace(tmp_path, output_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def reference_features_for_check(
    qidx: np.ndarray,
    query_xyz: np.ndarray,
    context_xyz: np.ndarray,
    center_x: float,
    center_y: float,
    loaded: dict[str, Any],
    hybrid,
    dtm,
    terrain: np.ndarray,
    raster_report: dict[str, Any],
    feat_mean: np.ndarray,
    feat_std: np.ndarray,
):
    from types import SimpleNamespace

    args = SimpleNamespace(
        tile_size=TILE_SIZE,
        halo=HALO,
        density_radius=DENSITY_RADIUS,
        roughness_k=ROUGHNESS_K,
        hag_grid_size=1.0,
        hag_percentile=5.0,
        voxel_size_r2=VOXEL_SIZE_R2,
        voxel_size_large=VOXEL_SIZE_LARGE,
        geometry_query_chunk=100,
        seed=SEED,
    )
    ref, _ = hybrid.build_features(
        query_xyz=query_xyz,
        raw_context_xyz=context_xyz,
        raw_context_class=np.zeros(len(context_xyz), dtype=np.int16),
        query_intensity=(loaded["intensity_raw"][qidx].astype(np.float32) / np.float32(65535.0)),
        query_return_number=loaded["return_number"][qidx].astype(np.float32),
        query_num_returns=loaded["num_returns"][qidx].astype(np.float32),
        tile_center_xy=(center_x, center_y),
        args=args,
    )
    ref[:, 3] = strong_hag(query_xyz, terrain, raster_report, dtm)
    invalid_q = loaded["invalid_return_mask"][qidx]
    if np.any(invalid_q):
        ref[invalid_q, 5:11] = feat_mean[5:11]
    norm = (ref - feat_mean) / feat_std
    np.clip(norm, -10.0, 10.0, out=norm)
    norm = np.nan_to_num(norm, nan=0.0, posinf=10.0, neginf=-10.0).astype(np.float32)
    return ref, norm


def equivalence_check(
    input_path: Path,
    model_path: Path,
    stats_path: Path,
    device: torch.device,
    workers: int,
    pca_batch_size: int,
    pca_engine: str,
    sample_points: int,
    io_chunk: int,
) -> int:
    print("=" * 88)
    print("V3.2 CORE EQUIVALENCE CHECK AGAINST RECOVERED V2 FEATURE PATH")
    print("=" * 88)
    model, feat_mean, feat_std, hybrid, dtm, checkpoint, _ = load_runtime(
        model_path, stats_path, device
    )
    terrain, raster_report, _ = build_strong_dtm(input_path, dtm)
    loaded = read_laz_inference_safe_streaming(input_path, io_chunk)
    xyz = loaded["xyz"]
    xy_tree = cKDTree(xyz[:, :2])
    tiles = hybrid.tile_grid(xyz, TILE_SIZE)

    selected = None
    for tile_x, tile_y, center_x, center_y in tiles:
        core = hybrid.square_query(xy_tree, center_x, center_y, TILE_SIZE / 2.0)
        context = hybrid.square_query(xy_tree, center_x, center_y, TILE_SIZE / 2.0 + HALO)
        if len(core) >= max(32, min(sample_points, 128)) and len(context) >= 3:
            selected = (tile_x, tile_y, center_x, center_y, core, context)
            break
    if selected is None:
        raise RuntimeError("Could not find a suitable tile for equivalence check")

    tile_x, tile_y, center_x, center_y, core, context = selected
    rng = np.random.default_rng(hybrid.stable_seed(f"{input_path.resolve()}::v3_equivalence", SEED))
    n_sample = min(int(sample_points), len(core))
    qidx = np.sort(rng.choice(core, size=n_sample, replace=False).astype(np.int64))
    query_xyz = xyz[qidx]
    context_xyz = xyz[context]

    print(
        f"Tile ({tile_x},{tile_y}) | sample={n_sample:,} | context={len(context):,} | "
        f"engine={pca_engine} | workers={workers}"
    )

    t0 = time.time()
    ref_raw, ref_norm = reference_features_for_check(
        qidx, query_xyz, context_xyz, center_x, center_y, loaded,
        hybrid, dtm, terrain, raster_report, feat_mean, feat_std,
    )
    ref_seconds = time.time() - t0

    t0 = time.time()
    cache = make_tile_cache(context_xyz, hybrid)
    fast_raw, fast_norm = build_features_fast(
        query_xyz=query_xyz,
        qidx=qidx,
        center_x=center_x,
        center_y=center_y,
        cache=cache,
        loaded=loaded,
        terrain=terrain,
        raster_report=raster_report,
        dtm=dtm,
        feat_mean=feat_mean,
        feat_std=feat_std,
        workers=workers,
        pca_batch_size=pca_batch_size,
        pca_engine=pca_engine,
        return_raw=True,
    )
    fast_seconds = time.time() - t0

    raw_abs = np.abs(ref_raw.astype(np.float64) - fast_raw.astype(np.float64))
    norm_abs = np.abs(ref_norm.astype(np.float64) - fast_norm.astype(np.float64))
    raw_max = float(raw_abs.max())
    norm_max = float(norm_abs.max())
    raw_mean = float(raw_abs.mean())
    norm_mean = float(norm_abs.mean())

    seed_base = hybrid.stable_seed(
        f"{input_path.resolve()}::{tile_x}::{tile_y}::equivalence::premium_inference",
        SEED,
    )
    ref_pred, ref_conf = infer_model_blocks_fast(
        query_xyz, ref_norm, model, device, 1, 1, seed_base
    )
    fast_pred, fast_conf = infer_model_blocks_fast(
        query_xyz, fast_norm, model, device, 1, 1, seed_base
    )
    pred_match = float(np.mean(ref_pred == fast_pred))
    conf_max = float(np.max(np.abs(ref_conf.astype(np.float64) - fast_conf.astype(np.float64))))

    print("\nEquivalence metrics:")
    print(f"  Raw feature max abs diff       : {raw_max:.9g}")
    print(f"  Raw feature mean abs diff      : {raw_mean:.9g}")
    print(f"  Normalized max abs diff        : {norm_max:.9g}")
    print(f"  Normalized mean abs diff       : {norm_mean:.9g}")
    print(f"  Model prediction exact match   : {pred_match * 100.0:.4f}%")
    print(f"  Confidence max abs diff        : {conf_max:.9g}")
    print(f"  Torch FPS synchronized seed    : ON ({seed_base})")
    print(f"  Reference feature time         : {ref_seconds:.2f}s")
    print(f"  V3 cached feature time         : {fast_seconds:.2f}s")
    if fast_seconds > 0:
        print(f"  Sample feature speedup         : {ref_seconds / fast_seconds:.2f}x")

    # Prediction identity is the highest priority.  The normalized numerical
    # tolerance is deliberately tight enough to catch a changed feature contract.
    passed = pred_match == 1.0 and norm_max <= 5e-5 and conf_max <= 1e-5
    if passed:
        print("\nEQUIVALENCE CHECK PASS")
        print("V3.3.0 frozen core preserved the exact 68F features and synchronized PointNet++ FPS decisions on the sampled real points.")
        return 0

    print("\nEQUIVALENCE CHECK FAILED")
    if pca_engine == "vectorized":
        print("Retry with: --pca-engine reference")
    print("Do NOT run the 26M-point production test until this passes.")
    return 2


def classify_file(
    input_path: Path,
    output_path: Path,
    model_path: Path,
    stats_path: Path,
    device: torch.device,
    vote_passes: int,
    batch_size: int,
    ground_uncat_threshold: float,
    ground_uncat_max_abs_hag: float,
    ground_uncat_max_above_local_p10: float,
    qc_enabled: bool,
    qc_batch_size: int,
    workers: int,
    feature_block: int,
    pca_batch_size: int,
    pca_engine: str,
    io_chunk: int,
    resume: bool,
    keep_work: bool,
):
    started = time.time()
    model, feat_mean, feat_std, hybrid, dtm, checkpoint, frozen_hashes = load_runtime(
        model_path, stats_path, device
    )

    print("=" * 88)
    print("PREMIUM BEST_GUARDED 68F INFERENCE V3.3.0 - ACCURACY-FINAL CANDIDATE")
    print("=" * 88)
    print(f"Input : {input_path}")
    print(f"Output: {output_path}")
    print(f"Model : {model_path}")
    print(f"Device: {device}")
    print(f"Model checkpoint epoch index: {checkpoint.get('epoch', 'unknown')}")
    print(f"Best mIoU recorded: {checkpoint.get('best_miou', 'unknown')}")
    print("LAS mapping: model [0,1,2,3,4] -> LAS [2,3,4,5,6]")
    print(
        f"Fast settings: workers={workers}, feature_block={feature_block:,}, "
        f"pca_batch={pca_batch_size:,}, pca_engine={pca_engine}"
    )
    if qc_enabled:
        print("QC post-processing: ON")
        print("  High Noise -> LAS 18 | Low Point STRICT_B -> LAS 7")
        print(
            f"  Ground-only Uncategorized -> LAS 0 only when confidence < "
            f"{ground_uncat_threshold:.3f}, |RAW HAG| <= {ground_uncat_max_abs_hag:.2f} m, "
            f"and Z-localP10 <= {ground_uncat_max_above_local_p10:.2f} m"
        )
        print("  LAS 1 generation: DISABLED")
    else:
        print("QC post-processing: OFF (pure Premium V3.1-equivalent 5-class output)")

    terrain, raster_report, dtm_stages = build_strong_dtm(input_path, dtm)

    print("[2/5] Streaming LAZ into compact arrays and building spatial index...")
    t0 = time.time()
    loaded = read_laz_inference_safe_streaming(input_path, io_chunk)
    xyz = loaded["xyz"]
    n_total = len(xyz)
    invalid_return_count = loaded["invalid_return_count"]
    if invalid_return_count:
        pct = 100.0 * invalid_return_count / max(n_total, 1)
        print(
            f"      Return metadata warning: {invalid_return_count:,}/{n_total:,} invalid "
            f"({pct:.2f}%). Six return-derived features are neutralized only for those points."
        )
    xy_tree = cKDTree(xyz[:, :2])
    tiles = hybrid.tile_grid(xyz, TILE_SIZE)
    print(f"      Points={n_total:,} | tiles={len(tiles):,} | ready in {time.time() - t0:.1f}s")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    settings = {
        "script_version": FAST_SCRIPT_VERSION,
        "torch_fps_seed_mode": TORCH_FPS_SEED_MODE,
        "tile_size": TILE_SIZE,
        "halo": HALO,
        "feature_block": int(feature_block),
        "reference_infer_block": int(REFERENCE_INFER_BLOCK),
        "pca_batch_size": int(pca_batch_size),
        "pca_engine": pca_engine,
        "workers": int(workers),
        "vote_passes": int(vote_passes),
        "batch_size": int(batch_size),
        "qc_enabled": bool(qc_enabled),
        "ground_uncat_threshold": float(ground_uncat_threshold),
        "ground_uncat_max_abs_hag": float(ground_uncat_max_abs_hag),
        "ground_uncat_max_above_local_p10": float(ground_uncat_max_above_local_p10),
        "qc_batch_size": int(qc_batch_size),
        "qc_contract": "high18_low7_strictB_ground0_conf_absHAG_localP10_precision_first",
    }
    predictions, owner, confidence_store, completed, work_paths, identity = init_or_resume_work(
        output_path=output_path,
        n_total=n_total,
        tile_count=len(tiles),
        input_path=input_path,
        frozen_hashes=frozen_hashes,
        settings=settings,
        resume=resume,
        need_confidence=bool(qc_enabled),
    )

    print("[3/5] Cached exact-contract 68F extraction + Premium inference...")
    stage3_start = time.time()
    initial_completed = len(completed)

    for tile_no, (tile_x, tile_y, center_x, center_y) in enumerate(tiles, start=1):
        if tile_no in completed:
            continue

        tile_start = time.time()
        core_indices = hybrid.square_query(xy_tree, center_x, center_y, TILE_SIZE / 2.0)
        if core_indices.size == 0:
            mark_tile_complete(
                tile_no, completed, work_paths, identity,
                predictions, owner, confidence_store,
            )
            continue

        # Preserve V2 boundary ownership: an exact-boundary point is owned by the
        # first tile in deterministic tile_grid order.
        core_indices = core_indices[np.asarray(predictions[core_indices]) == 255]
        if core_indices.size == 0:
            mark_tile_complete(
                tile_no, completed, work_paths, identity,
                predictions, owner, confidence_store,
            )
            continue

        context_indices = hybrid.square_query(
            xy_tree, center_x, center_y, TILE_SIZE / 2.0 + HALO
        )
        if context_indices.size < 3:
            mark_tile_complete(
                tile_no, completed, work_paths, identity,
                predictions, owner, confidence_store,
            )
            continue

        context_xyz = xyz[context_indices]
        cache_t0 = time.time()
        cache = make_tile_cache(context_xyz, hybrid)
        cache_seconds = time.time() - cache_t0

        print(
            f"      Tile {tile_no:>4}/{len(tiles)} | core={len(core_indices):,} "
            f"context={len(context_indices):,} | vox15={len(cache.voxel_015):,} "
            f"vox30={len(cache.voxel_030):,} | cache={cache_seconds:.1f}s"
        )

        total_blocks = int(math.ceil(len(core_indices) / feature_block))
        for block_no, qstart in enumerate(range(0, len(core_indices), feature_block), start=1):
            block_start = time.time()
            qidx = core_indices[qstart:qstart + feature_block]
            query_xyz = xyz[qidx]

            normalized = build_features_fast(
                query_xyz=query_xyz,
                qidx=qidx,
                center_x=center_x,
                center_y=center_y,
                cache=cache,
                loaded=loaded,
                terrain=terrain,
                raster_report=raster_report,
                dtm=dtm,
                feat_mean=feat_mean,
                feat_std=feat_std,
                workers=workers,
                pca_batch_size=pca_batch_size,
                pca_engine=pca_engine,
            )

            # Preserve V2 model grouping exactly.  V2 inferred each 7,000-point
            # feature query block independently and included qstart in the RNG seed.
            # V3 may EXTRACT several such blocks together, but model inference is
            # still split on the original 7,000-point boundaries.
            for suboffset in range(0, len(qidx), REFERENCE_INFER_BLOCK):
                global_qstart = qstart + suboffset
                sub_end = min(suboffset + REFERENCE_INFER_BLOCK, len(qidx))
                sub_qidx = qidx[suboffset:sub_end]
                sub_xyz = query_xyz[suboffset:sub_end]
                sub_norm = normalized[suboffset:sub_end]
                seed_base = hybrid.stable_seed(
                    f"{input_path.resolve()}::{tile_x}::{tile_y}::{global_qstart}::premium_inference",
                    SEED,
                )
                pred, conf = infer_model_blocks_fast(
                    query_xyz=sub_xyz,
                    normalized_features=sub_norm,
                    model=model,
                    device=device,
                    vote_passes=vote_passes,
                    batch_size=batch_size,
                    seed_base=seed_base,
                )
                predictions[sub_qidx] = pred
                owner[sub_qidx] = tile_no
                if confidence_store is not None:
                    confidence_store[sub_qidx] = conf
                del pred, conf

            if total_blocks > 1:
                done = min(qstart + len(qidx), len(core_indices))
                print(
                    f"            block {block_no:>2}/{total_blocks} | "
                    f"{done:,}/{len(core_indices):,} | {time.time() - block_start:.1f}s"
                )

            del normalized, query_xyz

        mark_tile_complete(
            tile_no, completed, work_paths, identity,
            predictions, owner, confidence_store,
        )
        tile_seconds = time.time() - tile_start
        stage_elapsed = time.time() - stage3_start
        newly_done = max(len(completed) - initial_completed, 1)
        rate = stage_elapsed / newly_done
        remaining_tiles = len(tiles) - len(completed)
        eta_seconds = rate * remaining_tiles
        print(
            f"            tile done in {tile_seconds:.1f}s | progress={len(completed)}/{len(tiles)} "
            f"| rough ETA={eta_seconds / 3600.0:.2f}h"
        )

        del cache, context_xyz, core_indices, context_indices
        gc.collect()

    missing = np.asarray(predictions) == 255
    missing_count = int(np.count_nonzero(missing))
    if missing_count:
        print(f"      WARNING: {missing_count:,} points received no tile prediction. NN filling.")
        good = ~missing
        if not np.any(good):
            raise RuntimeError("No points received predictions")
        fill_tree = cKDTree(xyz[good])
        _, nn = fill_tree.query(xyz[missing], k=1, workers=workers)
        predictions[missing] = np.asarray(predictions[good])[nn]
        if confidence_store is not None:
            confidence_store[missing] = np.asarray(confidence_store[good])[nn]
        del fill_tree, nn
        predictions.flush()
        if confidence_store is not None:
            confidence_store.flush()

    pred_array = np.asarray(predictions)
    if np.any(pred_array >= NUM_CLASSES):
        raise RuntimeError("Internal predictions contain invalid class indices")
    premium_las = MODEL_TO_LAS[pred_array]

    qc_report: dict[str, Any] = {
        "enabled": bool(qc_enabled),
        "safety_invariant_non_qc_exact": True,
        "class1_generated": False,
    }
    if qc_enabled:
        if confidence_store is None:
            raise RuntimeError("Confidence storage missing for Ground-only Uncategorized QC")
        print("[4/5] Applying global boundary-safe QC post-processing...")
        print("      Computing RAW SIGNED HAG for QC only (model HAG remains unchanged/clipped)...")
        raw_hag = raw_signed_hag_all(xyz, terrain, raster_report, dtm)

        # Neural inference is finished; release GPU model memory before CPU-heavy QC.
        del model
        if device.type == "cuda":
            torch.cuda.empty_cache()

        las_classes, qc_detail = apply_qc_postprocessing(
            premium_las=premium_las,
            confidence=np.asarray(confidence_store),
            xyz=xyz,
            xy_tree=xy_tree,
            raw_hag=raw_hag,
            workers=workers,
            batch_size=qc_batch_size,
            ground_uncat_threshold=ground_uncat_threshold,
            ground_uncat_max_abs_hag=ground_uncat_max_abs_hag,
            ground_uncat_max_above_local_p10=ground_uncat_max_above_local_p10,
        )
        qc_report.update(qc_detail)
        del raw_hag
    else:
        las_classes = premium_las.copy()
        del model
        if device.type == "cuda":
            torch.cuda.empty_cache()

    if np.any(las_classes == 1):
        raise RuntimeError("V3.2 safety check failed: LAS class 1 must not be generated")

    print("[5/5] Streaming classified LAS/LAZ write (all non-class fields preserved)...")
    del xy_tree, terrain
    gc.collect()
    write_classified_streaming(input_path, output_path, las_classes, io_chunk)

    binc = np.bincount(las_classes.astype(np.int64), minlength=19)
    class_distribution = {}
    for cls in FINAL_CLASS_ORDER:
        cnt = int(binc[cls])
        if cnt:
            class_distribution[str(cls)] = {
                "name": CLASS_NAMES[cls],
                "points": cnt,
                "percent": float(100.0 * cnt / n_total),
            }

    report = {
        "status": "COMPLETED",
        "engine": "premium_best_guarded_inference_v3_3_accuracy_final",
        "input": str(input_path.resolve()),
        "output": str(output_path.resolve()),
        "points": int(n_total),
        "model": str(model_path.resolve()),
        "model_sha256": frozen_hashes["model"],
        "feature_stats": str(stats_path.resolve()),
        "feature_stats_sha256": frozen_hashes["stats"],
        "model_py_sha256": frozen_hashes["model_py"],
        "hybrid_sha256": frozen_hashes["hybrid"],
        "dtm_sha256": frozen_hashes["dtm"],
        "checkpoint_epoch_index": checkpoint.get("epoch"),
        "checkpoint_best_miou": checkpoint.get("best_miou"),
        "checkpoint_best_guarded_score": checkpoint.get("best_guarded_score"),
        "feature_contract": "accepted_68f_hybrid_v2_dtm_v3_strong_production",
        "optimization_contract": "v3_1_exact_core_plus_ground_safe_global_qc_postprocessing",
        "settings": settings,
        "ptc_compatible_las_mapping": {
            "0": "Uncategorized / uncertain Ground",
            "2": "Ground",
            "3": "Low vegetation",
            "4": "Medium vegetation",
            "5": "High vegetation",
            "6": "Building",
            "7": "Low Point",
            "18": "High Noise",
        },
        "qc": qc_report,
        "invalid_return_count": int(invalid_return_count),
        "class_distribution": class_distribution,
        "missing_points_filled": int(missing_count),
        "dtm": {
            "config": STRONG_DTM,
            "raster": raster_report,
            "stages": dtm_stages,
        },
        "seconds": float(time.time() - started),
    }
    report_path = output_path.with_suffix(output_path.suffix + ".premium_v3_2_1_qc_report.json")
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\nClassification result:")
    for cls in sorted(class_distribution, key=lambda x: int(x)):
        row = class_distribution[cls]
        print(f"  LAS {cls}: {row['name']:<18} {row['points']:>12,}  ({row['percent']:.2f}%)")
    print(f"\nOutput : {output_path}")
    print(f"Report : {report_path}")
    print(f"Elapsed: {report['seconds']:.1f}s ({report['seconds'] / 3600.0:.2f}h)")

    # Close memmaps before optional cleanup.
    del predictions, owner, confidence_store
    gc.collect()
    if not keep_work:
        shutil.rmtree(work_paths["work"], ignore_errors=True)
    else:
        print(f"Work state kept: {work_paths['work']}")
    return output_path


def self_check(model_path: Path, stats_path: Path, device: torch.device) -> int:
    model, feat_mean, feat_std, hybrid, _, checkpoint, hashes = load_runtime(
        model_path, stats_path, device
    )
    print("SELF-CHECK PASS - V3.3.0 ACCURACY-FINAL CANDIDATE WITH FROZEN V3.1 PREMIUM CORE")
    print(f"Model SHA256 : {hashes['model']}")
    print(f"Stats SHA256 : {hashes['stats']}")
    print(f"model.py SHA : {hashes['model_py']}")
    print(f"Hybrid SHA   : {hashes['hybrid']}")
    print(f"DTM SHA      : {hashes['dtm']}")
    print(f"Features     : {checkpoint.get('num_features')}")
    print(f"Classes      : {checkpoint.get('num_classes')}")
    print(f"Epoch index  : {checkpoint.get('epoch')}")
    print(f"Best mIoU    : {checkpoint.get('best_miou')}")
    print(f"Feature names: {len(hybrid.FEATURE_NAMES)} exact names verified")
    print(f"Stats arrays : mean={feat_mean.shape}, std={feat_std.shape}")
    print("LAS mapping  : Uncategorized=0, Ground=2, LowVeg=3, MediumVeg=4, HighVeg=5, Building=6, LowPoint=7, HighNoise=18")
    print(f"Torch FPS    : deterministic ({TORCH_FPS_SEED_MODE})")
    print(f"Script       : V{FAST_SCRIPT_VERSION}")
    del model
    return 0


def resolve_output(input_path: Path, requested: str | None) -> Path:
    if requested:
        out = Path(requested)
        if out.exists() and out.is_dir():
            return out / f"{input_path.stem}_premium_v3_2_1_qc_classified{input_path.suffix}"
        if not out.suffix:
            out.mkdir(parents=True, exist_ok=True)
            return out / f"{input_path.stem}_premium_v3_2_1_qc_classified{input_path.suffix}"
        return out
    return input_path.with_name(f"{input_path.stem}_premium_v3_2_1_qc_classified{input_path.suffix}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Premium best_guarded 68F V3.3.0 Accuracy-Final candidate LAS/LAZ inference"
    )
    parser.add_argument("--input", help="Raw/unclassified .las or .laz")
    parser.add_argument("--output", help="Output .las/.laz or output directory")
    parser.add_argument("--model", help="Exact Premium best_guarded.pth")
    parser.add_argument("--stats", default=str(DEFAULT_STATS), help="Exact accepted feature_stats.json")
    parser.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    parser.add_argument("--vote-passes", type=int, default=2, help="Keep 2 for Premium accuracy")
    parser.add_argument("--batch-size", type=int, default=1, help="Keep 1 on NVIDIA T400 4GB")
    parser.add_argument("--workers", type=int, default=0, help="0=all CPU cores; 1=strict single-thread KD queries")
    parser.add_argument("--feature-block", type=int, default=DEFAULT_FEATURE_BLOCK)
    parser.add_argument("--pca-batch-size", type=int, default=DEFAULT_PCA_BATCH)
    parser.add_argument(
        "--pca-engine",
        choices=["vectorized", "reference"],
        default="reference",
        help="reference=exact V2 PCA arithmetic with cached/batched execution; vectorized=experimental faster mode",
    )
    parser.add_argument("--io-chunk", type=int, default=DEFAULT_IO_CHUNK)
    parser.add_argument(
        "--ground-uncat-threshold",
        type=float,
        default=DEFAULT_GROUND_UNCAT_THRESHOLD,
        help="Ground-only confidence threshold to LAS 0 Uncategorized (default: 0.70)",
    )
    parser.add_argument(
        "--ground-uncat-max-abs-hag",
        type=float,
        default=DEFAULT_GROUND_UNCAT_MAX_ABS_HAG,
        help="Precision-first LAS0 gate: require |RAW signed HAG| <= this many metres (default: 0.25)",
    )
    parser.add_argument(
        "--ground-uncat-max-above-local-p10",
        type=float,
        default=DEFAULT_GROUND_UNCAT_MAX_ABOVE_LOCAL_P10,
        help="Precision-first LAS0 gate: require Z-local XY K128 P10 <= this many metres (default: 0.25)",
    )
    parser.add_argument(
        "--qc-batch-size",
        type=int,
        default=DEFAULT_QC_BATCH,
        help="CPU QC neighbour-analysis batch size (default: 5000)",
    )
    parser.add_argument(
        "--disable-qc",
        action="store_true",
        help="Disable LAS 0/7/18 post-processing and produce pure V3.1-equivalent classes 2-6",
    )
    parser.add_argument("--resume", action="store_true", help="Resume a matching interrupted V3.3.0 run")
    parser.add_argument("--keep-work", action="store_true", help="Keep V3.3.0 mmap work state after success")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--equivalence-check", action="store_true")
    parser.add_argument("--sample-points", type=int, default=256, help="Real points used by equivalence check")
    args = parser.parse_args()

    if args.vote_passes < 1:
        raise SystemExit("--vote-passes must be >= 1")
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be >= 1")
    if args.feature_block < REFERENCE_INFER_BLOCK or args.feature_block % REFERENCE_INFER_BLOCK != 0:
        raise SystemExit(f"--feature-block must be a positive multiple of {REFERENCE_INFER_BLOCK} to preserve V2 model grouping")
    if args.pca_batch_size < 16:
        raise SystemExit("--pca-batch-size must be >= 16")
    if args.io_chunk < 10000:
        raise SystemExit("--io-chunk must be >= 10000")
    if args.sample_points < 32:
        raise SystemExit("--sample-points must be >= 32")
    if not (0.0 <= args.ground_uncat_threshold < 1.0):
        raise SystemExit("--ground-uncat-threshold must be in [0,1)")
    if args.ground_uncat_max_abs_hag <= 0.0:
        raise SystemExit("--ground-uncat-max-abs-hag must be > 0")
    if args.ground_uncat_max_above_local_p10 <= 0.0:
        raise SystemExit("--ground-uncat-max-above-local-p10 must be > 0")
    if args.qc_batch_size < 128:
        raise SystemExit("--qc-batch-size must be >= 128")

    workers = resolve_workers(args.workers)
    model_path = Path(args.model) if args.model else find_default_model()
    if model_path is None:
        raise SystemExit("Premium best_guarded.pth not found beside script and --model was not supplied")
    stats_path = Path(args.stats)

    if args.device == "cuda":
        if not torch.cuda.is_available():
            raise SystemExit("CUDA requested but PyTorch cannot see a CUDA GPU")
        device = torch.device("cuda:0")
    elif args.device == "cpu":
        device = torch.device("cpu")
    else:
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    if args.self_check:
        return self_check(model_path, stats_path, device)

    if not args.input:
        raise SystemExit("--input is required unless --self-check is used")
    input_path = Path(args.input)
    if not input_path.is_file():
        raise SystemExit(f"Input file not found: {input_path}")
    if input_path.suffix.lower() not in {".las", ".laz"}:
        raise SystemExit("Input must be .las or .laz")

    if args.equivalence_check:
        return equivalence_check(
            input_path=input_path,
            model_path=model_path,
            stats_path=stats_path,
            device=device,
            workers=workers,
            pca_batch_size=args.pca_batch_size,
            pca_engine=args.pca_engine,
            sample_points=args.sample_points,
            io_chunk=args.io_chunk,
        )

    output_path = resolve_output(input_path, args.output)
    if output_path.resolve() == input_path.resolve():
        raise SystemExit("Output must not overwrite raw input")
    if output_path.exists() and not args.resume:
        raise SystemExit(f"Output already exists: {output_path}. Choose another output path.")

    classify_file(
        input_path=input_path,
        output_path=output_path,
        model_path=model_path,
        stats_path=stats_path,
        device=device,
        vote_passes=args.vote_passes,
        batch_size=args.batch_size,
        ground_uncat_threshold=args.ground_uncat_threshold,
        ground_uncat_max_abs_hag=args.ground_uncat_max_abs_hag,
        ground_uncat_max_above_local_p10=args.ground_uncat_max_above_local_p10,
        qc_enabled=not args.disable_qc,
        qc_batch_size=args.qc_batch_size,
        workers=workers,
        feature_block=args.feature_block,
        pca_batch_size=args.pca_batch_size,
        pca_engine=args.pca_engine,
        io_chunk=args.io_chunk,
        resume=args.resume,
        keep_work=args.keep_work,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
