"""streaming_probe_rig.py - a small, FORCED-STREAMING manager for behaviour
experiments and tests (no GPU, no GUI, ~seconds).

A non-uniform synthetic survey (dense clusters over sparse background, like a
real corridor/urban LiDAR flight) is built with the CURRENT builder, then opened
with a GPU budget far below the dataset size so the manager takes the
STREAMING_LOD path - the path a 1B-point dataset must use.
"""
import os
import sys
import tempfile
import time

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


def build_dataset(n=1_500_000, seed=3, leaf_target=20_000, min_leaf_points=5_000,
                  span=1000.0, directory=None):
    import laspy
    from gui.naksha_cache.builder import NakshaPointCacheBuilder
    # The dataset is a pure function of its parameters, so it lives in ONE fixed
    # folder on the project drive and is reused across runs (the system temp
    # drive can be full; leaving a new 100 MB dataset per run filled it).
    from gui.naksha_cache.lod_ladder import DEFAULT_LOD_LEVELS, DEFAULT_RATIO
    key = (f"u_{n}_{seed}_{leaf_target}_{min_leaf_points}_{int(span)}"
           f"_r{DEFAULT_RATIO}_l{DEFAULT_LOD_LEVELS}")
    d = directory or os.path.join(ROOT, "diagnostics", "_stream_rig", key)
    os.makedirs(d, exist_ok=True)
    existing = os.path.join(d, "u.las")
    if os.path.isfile(existing + ".nakshaidx") and os.path.isfile(
            existing + ".nakshapc"):
        return existing
    rng = np.random.default_rng(seed)
    h = laspy.LasHeader(point_format=3, version="1.2")
    h.scales = (0.001,) * 3
    h.offsets = (0, 0, 0)
    las = laspy.LasData(h)
    x = np.concatenate([rng.random(n // 2) * span, rng.normal(300, 40, n // 4),
                        rng.normal(750, 60, n // 4)])
    y = np.concatenate([rng.random(n // 2) * span, rng.normal(400, 40, n // 4),
                        rng.normal(650, 60, n // 4)])
    las.x = np.clip(x, 0, span)
    las.y = np.clip(y, 0, span)
    las.z = rng.random(n) * 30
    las.classification = rng.integers(0, 6, n).astype(np.uint8)
    las.intensity = rng.integers(0, 2000, n).astype(np.uint16)
    p = os.path.join(d, "u.las")
    las.write(p)
    NakshaPointCacheBuilder(p, chunk_points=500_000, leaf_target=leaf_target,
                            min_leaf_points=min_leaf_points,
                            max_leaf_extent=125.0, verbose=False).build([p])
    return p


def open_manager(path, gpu_budget=6 * 1024 * 1024, ram_budget=512 * 1024 * 1024):
    from gui.naksha_cache.reader import NakshaPointCacheReader
    from gui.naksha_cache.stream_manager import NakshaStreamManager
    from gui.naksha_cache.stream_renderer_adapter import HeadlessTileRenderer
    from gui.naksha_cache.stream_telemetry import StreamTelemetry
    r = NakshaPointCacheReader(path, verify_crc=False, load_edits=False)

    class A:
        pass
    ad = HeadlessTileRenderer(total_points=int(r.index.total_points))
    mgr = NakshaStreamManager(A(), r, ad, StreamTelemetry(path + ".t.jsonl"),
                              ram_budget=ram_budget, gpu_budget=gpu_budget)
    return mgr, ad, r


def pump(mgr, cam, vp, frames=40, sleep=0.01):
    for _ in range(frames):
        mgr.on_frame(cam, vp)
        time.sleep(sleep)
