#!/usr/bin/env python3
r"""
PREMIUM BEST_GUARDED 68F INFERENCE V4.2 - PERSISTENT EXACT CANDIDATE
====================================================================

Speed-only production candidate built on the frozen V3.3 Accuracy-Final core.

Proven ingredients integrated here:
- Phase-4: base-feature work overlaps frozen PCA work.
- Phase-5: CPU feature block N+1 overlaps GPU inference block N.
- Phase-9: 8 PCA worker processes x 2 cKDTree query workers.
- Phase-10: TorchScript farthest-point-sampling loop (strict exact benchmark pass).
- Phase-15: one persistent Windows worker pool across tiles; per-tile cKDTrees are
  still rebuilt from the same tile context and frozen V3.3 NumPy PCA arithmetic.

This script does NOT modify V3.3, V4.0, model.py, best_guarded.pth, feature_stats,
_core_hybrid_v2.py, or _core_dtm_v3.py.
"""
from __future__ import annotations

import concurrent.futures as cf
import gc
import hashlib
import importlib
import importlib.util
import json
import math
import multiprocessing as mp
from multiprocessing import shared_memory
import os
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import torch
from scipy.spatial import cKDTree

SCRIPT_DIR = Path(__file__).resolve().parent
V33_PATH = SCRIPT_DIR / "premium_best_guarded_inference_v3_3_accuracy_final.py"
P15_WORKER_PATH = SCRIPT_DIR / "v4_phase15_persistent_worker.py"
FPS_PATH = SCRIPT_DIR / "v4_phase10_scripted_fps.py"

EXPECTED_V33_SHA256 = "45b107b59f014992449dd815b7baef4dd66fecd654817cf7170ecc2569b54ed9"
EXPECTED_P15_WORKER_SHA256 = "4d456982f856900f4a84f02c247d8f1acc4f9d6c201effa31f0c5e020b868750"
EXPECTED_FPS_SHA256 = "a25b39f7c8c371bf76edd33b8e366c7681575b1c018bdc6176e48369abe6f0c4"

V42_VERSION = "4.2.0-persistent-p8w2-scripted-fps-exact-candidate"
FEATURE_BLOCK = 28_000
PCA_PROCESSES = 8
QUERY_WORKERS = 2
PCA_TASK_POINTS = 1_024
PCA_BATCH = 512
BASE_WORKERS = 1
GPU_BATCH = 1
VOTE_PASSES = 2
CAPACITY_GROWTH = 1.50


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def load_private_module(path: Path, name: str):
    if not path.is_file():
        raise FileNotFoundError(path)
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_real_module(path: Path):
    parent = str(path.resolve().parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    importlib.invalidate_caches()
    name = path.stem
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    return importlib.import_module(name)


class SharedArray:
    def __init__(self, *, shape, dtype):
        dtype = np.dtype(dtype)
        nbytes = max(int(np.prod(shape)) * dtype.itemsize, 1)
        self.shm = shared_memory.SharedMemory(create=True, size=nbytes)
        self.arr = np.ndarray(shape, dtype=dtype, buffer=self.shm.buf)
        self.spec = {
            "name": self.shm.name,
            "shape": list(self.arr.shape),
            "dtype": self.arr.dtype.str,
        }

    def close_unlink(self):
        try:
            self.shm.close()
        finally:
            try:
                self.shm.unlink()
            except FileNotFoundError:
                pass


class PersistentState:
    def __init__(
        self,
        *,
        worker,
        raw_capacity: int,
        vox15_capacity: int,
        vox30_capacity: int,
        dtype,
    ):
        self.worker = worker
        self.closed = False
        self.generation = 0
        self.lengths = (0, 0, 0)
        self.raw_capacity = int(raw_capacity)
        self.vox15_capacity = int(vox15_capacity)
        self.vox30_capacity = int(vox30_capacity)

        self.query = SharedArray(shape=(FEATURE_BLOCK, 3), dtype=dtype)
        self.raw = SharedArray(shape=(self.raw_capacity, 3), dtype=dtype)
        self.vox15 = SharedArray(shape=(self.vox15_capacity, 3), dtype=dtype)
        self.vox30 = SharedArray(shape=(self.vox30_capacity, 3), dtype=dtype)
        self.shared = [self.query, self.raw, self.vox15, self.vox30]

        specs = {
            "query": self.query.spec,
            "raw": self.raw.spec,
            "vox15": self.vox15.spec,
            "vox30": self.vox30.spec,
        }
        ctx = mp.get_context("spawn")
        self.executor = cf.ProcessPoolExecutor(
            max_workers=PCA_PROCESSES,
            mp_context=ctx,
            initializer=worker.worker_init,
            initargs=(str(V33_PATH), specs, QUERY_WORKERS),
        )

    def fits(self, cache) -> bool:
        return (
            len(cache.raw_xyz) <= self.raw_capacity
            and len(cache.voxel_015) <= self.vox15_capacity
            and len(cache.voxel_030) <= self.vox30_capacity
        )

    def load_cache(self, cache) -> float:
        if not self.fits(cache):
            raise RuntimeError("cache exceeds persistent shared capacity")
        t0 = time.perf_counter()
        raw_len = len(cache.raw_xyz)
        vox15_len = len(cache.voxel_015)
        vox30_len = len(cache.voxel_030)
        np.copyto(self.raw.arr[:raw_len], cache.raw_xyz, casting="no")
        np.copyto(self.vox15.arr[:vox15_len], cache.voxel_015, casting="no")
        np.copyto(self.vox30.arr[:vox30_len], cache.voxel_030, casting="no")
        self.generation += 1
        self.lengths = (int(raw_len), int(vox15_len), int(vox30_len))
        return float(time.perf_counter() - t0)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.executor.shutdown(wait=True, cancel_futures=True)
        finally:
            for item in self.shared:
                item.close_unlink()
        gc.collect()


def _capacity(current_len: int, previous: int = 0) -> int:
    base = max(int(current_len), 1)
    grown = int(math.ceil(base * CAPACITY_GROWTH))
    if previous > 0:
        grown = max(grown, int(math.ceil(previous * CAPACITY_GROWTH)))
    return max(grown, base)


def ensure_persistent_state(state, worker, cache):
    if state is not None and state.fits(cache):
        return state, False

    prev_raw = state.raw_capacity if state is not None else 0
    prev_v15 = state.vox15_capacity if state is not None else 0
    prev_v30 = state.vox30_capacity if state is not None else 0
    if state is not None:
        state.close()

    state = PersistentState(
        worker=worker,
        raw_capacity=_capacity(len(cache.raw_xyz), prev_raw),
        vox15_capacity=_capacity(len(cache.voxel_015), prev_v15),
        vox30_capacity=_capacity(len(cache.voxel_030), prev_v30),
        dtype=cache.raw_xyz.dtype,
    )
    return state, True


def build_persistent_features(
    *, state, v33, query_xyz, qidx, center_x, center_y,
    cache, loaded, terrain, raster_report, dtm, feat_mean, feat_std,
):
    n = len(query_xyz)
    if n > FEATURE_BLOCK:
        raise RuntimeError(f"query block {n:,} exceeds {FEATURE_BLOCK:,}")

    t0 = time.perf_counter()
    np.copyto(state.query.arr[:n], query_xyz, casting="no")
    features = np.empty((n, v33.NUM_FEATURES), dtype=np.float32)

    generation = int(state.generation)
    raw_len, vox15_len, vox30_len = state.lengths
    geometry = (
        (57, "vox30", 10.0, cache.tree_030 is not None, len(cache.voxel_030)),
        (46, "vox30", 5.0, cache.tree_030 is not None, len(cache.voxel_030)),
        (35, "vox15", 2.0, cache.tree_015 is not None, len(cache.voxel_015)),
        (24, "raw", 1.0, cache.raw_tree is not None, len(cache.raw_xyz)),
        (13, "raw", 0.5, cache.raw_tree is not None, len(cache.raw_xyz)),
    )
    col_map = {(key, float(radius)): col for col, key, radius, _, _ in geometry}
    futures = []
    for col, key, radius, has_tree, context_len in geometry:
        if not has_tree or context_len < 3:
            features[:, col:col + 11] = 0.0
            continue
        for start in range(0, n, PCA_TASK_POINTS):
            end = min(start + PCA_TASK_POINTS, n)
            futures.append(
                state.executor.submit(
                    state.worker.pca_task,
                    (
                        generation,
                        raw_len,
                        vox15_len,
                        vox30_len,
                        key,
                        float(radius),
                        start,
                        end,
                    ),
                )
            )

    # Phase-4 exact winner: cheap/base work concurrently with PCA.
    features[:, 0] = (query_xyz[:, 0] - center_x).astype(np.float32)
    features[:, 1] = (query_xyz[:, 1] - center_y).astype(np.float32)
    features[:, 2] = query_xyz[:, 2].astype(np.float32)
    features[:, 3] = v33.strong_hag(query_xyz, terrain, raster_report, dtm)
    features[:, 4] = loaded["intensity_raw"][qidx].astype(np.float32) / np.float32(65535.0)
    rn = loaded["return_number"][qidx].astype(np.float32)
    nr = loaded["num_returns"][qidx].astype(np.float32)
    features[:, 5] = rn
    features[:, 6] = nr
    features[:, 7] = rn / np.maximum(nr, 1.0)
    features[:, 8] = (nr == 1.0).astype(np.float32)
    features[:, 9] = (rn == 1.0).astype(np.float32)
    features[:, 10] = (rn == nr).astype(np.float32)
    features[:, 11] = v33.density_counts_fast(query_xyz, cache.raw_tree, BASE_WORKERS)
    features[:, 12] = v33.roughness_zstd_fast(
        query_xyz, cache.raw_xyz, cache.raw_tree, BASE_WORKERS
    )

    for fut in cf.as_completed(futures):
        key, radius, start, end, out = fut.result()
        col = col_map[(key, float(radius))]
        features[start:end, col:col + 11] = out

    invalid_q = loaded["invalid_return_mask"][qidx]
    if np.any(invalid_q):
        features[invalid_q, 5:11] = feat_mean[5:11]

    if not np.isfinite(features).all():
        raise RuntimeError("V4.2 feature matrix contains NaN or infinity")
    if np.max(np.abs(features[:, 0])) > v33.TILE_SIZE / 2.0 + 0.01:
        raise RuntimeError("Local X exceeds tile limits")
    if np.max(np.abs(features[:, 1])) > v33.TILE_SIZE / 2.0 + 0.01:
        raise RuntimeError("Local Y exceeds tile limits")

    normalized = (features - feat_mean) / feat_std
    np.clip(normalized, -10.0, 10.0, out=normalized)
    normalized = np.nan_to_num(
        normalized, nan=0.0, posinf=10.0, neginf=-10.0
    ).astype(np.float32, copy=False)
    return normalized, float(time.perf_counter() - t0)


def install_scripted_fps(model, fps_mod):
    model_module_name = model.__class__.__module__
    model_mod = sys.modules.get(model_module_name)
    if model_mod is None or not hasattr(model_mod, "farthest_point_sample"):
        raise RuntimeError(f"Cannot resolve frozen FPS global in {model_module_name}")
    model_mod.farthest_point_sample = fps_mod.farthest_point_sample_scripted


def classify_file_v42(
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
    if vote_passes != VOTE_PASSES:
        raise RuntimeError(f"V4.2 exact candidate requires --vote-passes {VOTE_PASSES}")
    if batch_size != GPU_BATCH:
        raise RuntimeError(f"V4.2 exact candidate requires --batch-size {GPU_BATCH}")
    if feature_block != FEATURE_BLOCK:
        raise RuntimeError(f"V4.2 exact candidate requires --feature-block {FEATURE_BLOCK}")
    if pca_batch_size != PCA_BATCH:
        raise RuntimeError(f"V4.2 exact candidate requires --pca-batch-size {PCA_BATCH}")
    if pca_engine != "reference":
        raise RuntimeError("V4.2 exact candidate requires --pca-engine reference")

    started = time.time()
    model, feat_mean, feat_std, hybrid, dtm, checkpoint, frozen_hashes = v33.load_runtime(
        model_path, stats_path, device
    )
    install_scripted_fps(model, fps_mod)

    print("=" * 100)
    print("PREMIUM BEST_GUARDED 68F INFERENCE V4.2 - PERSISTENT EXACT CANDIDATE")
    print("=" * 100)
    print(f"Input : {input_path}")
    print(f"Output: {output_path}")
    print(f"Model : {model_path}")
    print(f"Device: {device}")
    print(f"Model checkpoint epoch index: {checkpoint.get('epoch', 'unknown')}")
    print(f"Best mIoU recorded: {checkpoint.get('best_miou', 'unknown')}")
    print("LAS mapping: model [0,1,2,3,4] -> LAS [2,3,4,5,6]")
    print(
        "Speed engine: Phase4 base/PCA overlap + Phase5 CPU/GPU prefetch + "
        "Phase9 p8_w2 + Phase10 scripted FPS + Phase15 persistent tile workers"
    )
    print(
        f"Exact settings: feature_block={FEATURE_BLOCK:,}, pca_processes={PCA_PROCESSES}, "
        f"query_workers={QUERY_WORKERS}, task={PCA_TASK_POINTS}, pca_batch={PCA_BATCH}, "
        f"gpu_batch={GPU_BATCH}, vote_passes={VOTE_PASSES}"
    )
    if qc_enabled:
        print("QC post-processing: ON (frozen V3.3 LAS 18 -> 7 -> Ground-only 0 policy)")
    else:
        print("QC post-processing: OFF")

    terrain, raster_report, dtm_stages = v33.build_strong_dtm(input_path, dtm)

    print("[2/5] Streaming LAZ into compact arrays and building spatial index...")
    t0 = time.time()
    loaded = v33.read_laz_inference_safe_streaming(input_path, io_chunk)
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
    tiles = hybrid.tile_grid(xyz, v33.TILE_SIZE)
    print(f"      Points={n_total:,} | tiles={len(tiles):,} | ready in {time.time() - t0:.1f}s")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    settings = {
        "script_version": V42_VERSION,
        "torch_fps_seed_mode": v33.TORCH_FPS_SEED_MODE,
        "tile_size": v33.TILE_SIZE,
        "halo": v33.HALO,
        "feature_block": FEATURE_BLOCK,
        "reference_infer_block": int(v33.REFERENCE_INFER_BLOCK),
        "pca_batch_size": PCA_BATCH,
        "pca_engine": "reference",
        "pca_processes": PCA_PROCESSES,
        "pca_task_points": PCA_TASK_POINTS,
        "pca_query_workers": QUERY_WORKERS,
        "base_query_workers": BASE_WORKERS,
        "persistent_worker_pool": True,
        "persistent_capacity_growth": CAPACITY_GROWTH,
        "scripted_fps": True,
        "cpu_gpu_feature_prefetch": True,
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
    predictions, owner, confidence_store, completed, work_paths, identity = v33.init_or_resume_work(
        output_path=output_path,
        n_total=n_total,
        tile_count=len(tiles),
        input_path=input_path,
        frozen_hashes=frozen_hashes,
        settings=settings,
        resume=resume,
        need_confidence=bool(qc_enabled),
    )

    print("[3/5] Persistent exact 68F extraction + overlapped Premium inference...")
    stage3_start = time.time()
    initial_completed = len(completed)
    persistent_state = None
    pool_restarts = 0
    pool_peak_capacities = {"raw": 0, "vox15": 0, "vox30": 0}
    context_copy_seconds_total = 0.0

    try:
        for tile_no, (tile_x, tile_y, center_x, center_y) in enumerate(tiles, start=1):
            if tile_no in completed:
                continue

            tile_start = time.time()
            core_indices = hybrid.square_query(xy_tree, center_x, center_y, v33.TILE_SIZE / 2.0)
            if core_indices.size == 0:
                v33.mark_tile_complete(
                    tile_no, completed, work_paths, identity,
                    predictions, owner, confidence_store,
                )
                continue

            core_indices = core_indices[np.asarray(predictions[core_indices]) == 255]
            if core_indices.size == 0:
                v33.mark_tile_complete(
                    tile_no, completed, work_paths, identity,
                    predictions, owner, confidence_store,
                )
                continue

            context_indices = hybrid.square_query(
                xy_tree, center_x, center_y, v33.TILE_SIZE / 2.0 + v33.HALO
            )
            if context_indices.size < 3:
                v33.mark_tile_complete(
                    tile_no, completed, work_paths, identity,
                    predictions, owner, confidence_store,
                )
                continue

            context_xyz = xyz[context_indices]
            cache_t0 = time.time()
            cache = v33.make_tile_cache(context_xyz, hybrid)
            cache_seconds = time.time() - cache_t0

            persistent_state, restarted = ensure_persistent_state(
                persistent_state, p15_worker, cache
            )
            if restarted:
                pool_restarts += 1
                print(
                    f"            persistent pool start/restart #{pool_restarts}: "
                    f"capacity raw/vox15/vox30="
                    f"{persistent_state.raw_capacity:,}/"
                    f"{persistent_state.vox15_capacity:,}/"
                    f"{persistent_state.vox30_capacity:,}"
                )
            copy_seconds = persistent_state.load_cache(cache)
            context_copy_seconds_total += copy_seconds
            pool_peak_capacities["raw"] = max(pool_peak_capacities["raw"], persistent_state.raw_capacity)
            pool_peak_capacities["vox15"] = max(pool_peak_capacities["vox15"], persistent_state.vox15_capacity)
            pool_peak_capacities["vox30"] = max(pool_peak_capacities["vox30"], persistent_state.vox30_capacity)

            print(
                f"      Tile {tile_no:>4}/{len(tiles)} | core={len(core_indices):,} "
                f"context={len(context_indices):,} | vox15={len(cache.voxel_015):,} "
                f"vox30={len(cache.voxel_030):,} | cache={cache_seconds:.1f}s | copy={copy_seconds:.3f}s"
            )

            specs = [
                (start, min(start + FEATURE_BLOCK, len(core_indices)))
                for start in range(0, len(core_indices), FEATURE_BLOCK)
            ]
            total_blocks = len(specs)

            def submit_build(executor, start, end):
                qidx_local = core_indices[start:end]
                query_local = xyz[qidx_local]
                return executor.submit(
                    build_persistent_features,
                    state=persistent_state,
                    v33=v33,
                    query_xyz=query_local,
                    qidx=qidx_local,
                    center_x=center_x,
                    center_y=center_y,
                    cache=cache,
                    loaded=loaded,
                    terrain=terrain,
                    raster_report=raster_report,
                    dtm=dtm,
                    feat_mean=feat_mean,
                    feat_std=feat_std,
                )

            with cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="v42_feature_prefetch") as ex:
                current = submit_build(ex, *specs[0])
                for block_index, (qstart, qend) in enumerate(specs):
                    block_start = time.time()
                    qidx = core_indices[qstart:qend]
                    query_xyz = xyz[qidx]
                    normalized, feature_s = current.result()

                    if block_index + 1 < total_blocks:
                        next_future = submit_build(ex, *specs[block_index + 1])
                    else:
                        next_future = None

                    gpu_t0 = time.perf_counter()
                    for suboffset in range(0, len(qidx), v33.REFERENCE_INFER_BLOCK):
                        global_qstart = qstart + suboffset
                        sub_end = min(suboffset + v33.REFERENCE_INFER_BLOCK, len(qidx))
                        sub_qidx = qidx[suboffset:sub_end]
                        sub_xyz = query_xyz[suboffset:sub_end]
                        sub_norm = normalized[suboffset:sub_end]
                        seed_base = hybrid.stable_seed(
                            f"{input_path.resolve()}::{tile_x}::{tile_y}::{global_qstart}::premium_inference",
                            v33.SEED,
                        )
                        pred, conf = v33.infer_model_blocks_fast(
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
                    if device.type == "cuda":
                        torch.cuda.synchronize(device)
                    gpu_s = time.perf_counter() - gpu_t0

                    if total_blocks > 1:
                        done = min(qend, len(core_indices))
                        print(
                            f"            block {block_index + 1:>2}/{total_blocks} | "
                            f"{done:,}/{len(core_indices):,} | feature={feature_s:.1f}s "
                            f"gpu={gpu_s:.1f}s | wall={time.time() - block_start:.1f}s"
                        )

                    del normalized, query_xyz
                    current = next_future

            v33.mark_tile_complete(
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
    finally:
        if persistent_state is not None:
            persistent_state.close()
            persistent_state = None

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
    if np.any(pred_array >= v33.NUM_CLASSES):
        raise RuntimeError("Internal predictions contain invalid class indices")
    premium_las = v33.MODEL_TO_LAS[pred_array]

    qc_report = {
        "enabled": bool(qc_enabled),
        "safety_invariant_non_qc_exact": True,
        "class1_generated": False,
    }
    if qc_enabled:
        if confidence_store is None:
            raise RuntimeError("Confidence storage missing for Ground-only Uncategorized QC")
        print("[4/5] Applying global boundary-safe QC post-processing...")
        print("      Computing RAW SIGNED HAG for QC only (model HAG remains unchanged/clipped)...")
        raw_hag = v33.raw_signed_hag_all(xyz, terrain, raster_report, dtm)
        del model
        if device.type == "cuda":
            torch.cuda.empty_cache()
        las_classes, qc_detail = v33.apply_qc_postprocessing(
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
        raise RuntimeError("V4.2 safety check failed: LAS class 1 must not be generated")

    print("[5/5] Streaming classified LAS/LAZ write (all non-class fields preserved)...")
    del xy_tree, terrain
    gc.collect()
    v33.write_classified_streaming(input_path, output_path, las_classes, io_chunk)

    binc = np.bincount(las_classes.astype(np.int64), minlength=19)
    class_distribution = {}
    for cls in v33.FINAL_CLASS_ORDER:
        cnt = int(binc[cls])
        if cnt:
            class_distribution[str(cls)] = {
                "name": v33.CLASS_NAMES[cls],
                "points": cnt,
                "percent": float(100.0 * cnt / n_total),
            }

    dependency_hashes = {
        "v33": sha256_file(V33_PATH),
        "phase15_worker": sha256_file(P15_WORKER_PATH),
        "scripted_fps": sha256_file(FPS_PATH),
        **frozen_hashes,
    }
    report = {
        "status": "COMPLETED",
        "engine": "premium_best_guarded_inference_v4_2_persistent_exact_candidate",
        "input": str(input_path.resolve()),
        "output": str(output_path.resolve()),
        "points": int(n_total),
        "checkpoint_epoch_index": checkpoint.get("epoch"),
        "checkpoint_best_miou": checkpoint.get("best_miou"),
        "checkpoint_best_guarded_score": checkpoint.get("best_guarded_score"),
        "feature_contract": "accepted_68f_hybrid_v2_dtm_v3_strong_production",
        "optimization_contract": "phase4_base_pca_overlap_phase5_cpu_gpu_prefetch_phase9_p8w2_phase10_scripted_fps_phase15_persistent_workers",
        "settings": settings,
        "dependency_hashes": dependency_hashes,
        "persistent_pool_restarts": int(pool_restarts),
        "persistent_peak_capacities": pool_peak_capacities,
        "persistent_context_copy_seconds": float(context_copy_seconds_total),
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
            "config": v33.STRONG_DTM,
            "raster": raster_report,
            "stages": dtm_stages,
        },
        "seconds": float(time.time() - started),
    }
    report_path = output_path.with_suffix(output_path.suffix + ".premium_v4_2_persistent_exact_report.json")
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\nClassification result:")
    for cls in sorted(class_distribution, key=lambda x: int(x)):
        row = class_distribution[cls]
        print(f"  LAS {cls}: {row['name']:<40} {row['points']:>12,}  ({row['percent']:.2f}%)")
    print(f"\nOutput : {output_path}")
    print(f"Report : {report_path}")
    print(f"Elapsed: {report['seconds']:.1f}s ({report['seconds'] / 3600.0:.2f}h)")

    del predictions, owner, confidence_store
    gc.collect()
    if not keep_work:
        shutil.rmtree(work_paths["work"], ignore_errors=True)
    else:
        print(f"Work state kept: {work_paths['work']}")
    return output_path


def verify_dependencies():
    checks = {
        V33_PATH: EXPECTED_V33_SHA256,
        P15_WORKER_PATH: EXPECTED_P15_WORKER_SHA256,
        FPS_PATH: EXPECTED_FPS_SHA256,
    }
    for path, expected in checks.items():
        if not path.is_file():
            raise SystemExit(f"Required file missing: {path}")
        actual = sha256_file(path)
        if actual != expected:
            raise SystemExit(
                f"Dependency SHA mismatch for {path.name}\nExpected: {expected}\nActual:   {actual}"
            )


def main() -> int:
    global v33, p15_worker, fps_mod
    verify_dependencies()
    v33 = load_private_module(V33_PATH, "premium_v33_frozen_for_v42_persistent")
    p15_worker = load_real_module(P15_WORKER_PATH)
    fps_mod = load_real_module(FPS_PATH)

    # Patch only the entry used by V3.3 main(). Frozen source on disk is unchanged.
    v33.classify_file = classify_file_v42
    v33.FAST_SCRIPT_VERSION = V42_VERSION

    # V4.2 strict settings are already V3.3 defaults; explicit user changes are
    # validated inside classify_file_v42 before any production work starts.
    return int(v33.main())


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
