#!/usr/bin/env python3
"""Windows-spawn-safe persistent PCA worker for V4 Phase-15.

Keeps worker processes alive across tile changes. For every new tile generation,
workers rebuild cKDTrees from updated shared-memory context slices, then call the
unchanged frozen V3.3 reference PCA function.
"""
from __future__ import annotations

import importlib.util
import multiprocessing as mp
import sys
from multiprocessing import shared_memory
from pathlib import Path

import numpy as np
import torch
from scipy.spatial import cKDTree

FROZEN_PCA_BATCH = 512

_W_V33 = None
_W_SHMS = []
_W_QUERY = None
_W_RAW = None
_W_VOX15 = None
_W_VOX30 = None
_W_CONTEXTS = {}
_W_TREES = {}
_W_QUERY_WORKERS = 1
_W_GENERATION = -1


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _attach_array(spec: dict):
    shm = shared_memory.SharedMemory(name=spec["name"])
    arr = np.ndarray(
        tuple(spec["shape"]),
        dtype=np.dtype(spec["dtype"]),
        buffer=shm.buf,
    )
    return shm, arr


def worker_init(v33_path: str, specs: dict, query_workers: int):
    global _W_V33, _W_SHMS, _W_QUERY, _W_RAW, _W_VOX15, _W_VOX30
    global _W_CONTEXTS, _W_TREES, _W_QUERY_WORKERS, _W_GENERATION

    try:
        torch.set_num_threads(1)
    except Exception:
        pass

    _W_QUERY_WORKERS = max(1, int(query_workers))
    _W_V33 = _load_module(
        Path(v33_path),
        f"premium_v33_v415_persistent_worker_{mp.current_process().pid}",
    )
    _W_SHMS = []

    shm, _W_QUERY = _attach_array(specs["query"])
    _W_SHMS.append(shm)
    shm, _W_RAW = _attach_array(specs["raw"])
    _W_SHMS.append(shm)
    shm, _W_VOX15 = _attach_array(specs["vox15"])
    _W_SHMS.append(shm)
    shm, _W_VOX30 = _attach_array(specs["vox30"])
    _W_SHMS.append(shm)

    _W_CONTEXTS = {}
    _W_TREES = {}
    _W_GENERATION = -1


def _ensure_generation(generation: int, raw_len: int, vox15_len: int, vox30_len: int):
    global _W_GENERATION, _W_CONTEXTS, _W_TREES
    if int(generation) == _W_GENERATION:
        return

    raw = _W_RAW[: int(raw_len)]
    vox15 = _W_VOX15[: int(vox15_len)]
    vox30 = _W_VOX30[: int(vox30_len)]

    _W_CONTEXTS = {
        "raw": raw,
        "vox15": vox15,
        "vox30": vox30,
    }
    _W_TREES = {
        "raw": cKDTree(raw) if len(raw) >= 3 else None,
        "vox15": cKDTree(vox15) if len(vox15) >= 3 else None,
        "vox30": cKDTree(vox30) if len(vox30) >= 3 else None,
    }
    _W_GENERATION = int(generation)


def pca_task(task: tuple[int, int, int, int, str, float, int, int]):
    generation, raw_len, vox15_len, vox30_len, key, radius, start, end = task
    _ensure_generation(generation, raw_len, vox15_len, vox30_len)

    context = _W_CONTEXTS[key]
    tree = _W_TREES[key]
    out = _W_V33.radius_pca_features_fast(
        query_xyz=_W_QUERY[start:end],
        context_xyz=context,
        tree=tree,
        radius=float(radius),
        pca_batch_size=FROZEN_PCA_BATCH,
        workers=_W_QUERY_WORKERS,
        pca_engine="reference",
    )
    return key, float(radius), int(start), int(end), out
