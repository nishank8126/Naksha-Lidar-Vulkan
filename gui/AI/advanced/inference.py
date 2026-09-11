# OM DUM DURGAYE NAMAHA
import laspy
import numpy as np
import torch
import gc
import json
import sys
import time
import shutil
import logging
import hashlib
import traceback
import pickle
import importlib.util
import threading
import re
import os
import tempfile

# NAKSHA_GPU_PHASE11_V3_20_ADVANCED IMPORTS
from gui.AI.common.gpu_engine.runtime import (
    configure_torch_for_accuracy as naksha_configure_torch_for_accuracy,
    gpu_report_lines as naksha_gpu_report_lines,
    recommended_advanced_batch_size as naksha_recommended_advanced_batch_size,
    select_torch_device as naksha_select_torch_device,
    should_use_amp as naksha_should_use_amp,
)

try:
    from numba import njit
    HAS_NUMBA = True
except Exception:
    HAS_NUMBA = False
    njit = None

from datetime import datetime
from pathlib import Path
from scipy.spatial import cKDTree
from PySide6.QtCore import QThread, Signal

from gui.AI.common.advanced_qc import (
    ADV_QC_UNCATEGORIZED,
    ADV_QC_LOW_POINT,
    ADV_QC_HIGH_NOISE,
    apply_advanced_qc,
)
from gui.AI.common.powerline_postprocess import apply_power_asset_postprocess
from gui.AI.common.ptc_mapping import remap_classes, remap_las_file_in_place

ADVANCED_DIR = Path(__file__).resolve().parent
ADVANCE_MODEL_DIR = ADVANCED_DIR / "models"
MODEL_PATH = ADVANCE_MODEL_DIR / "best_building.pth"
STATS_PATH = ADVANCE_MODEL_DIR / "feature_stats.json"
ADVANCE_MODEL_PY = ADVANCE_MODEL_DIR / "model.py"

ADVANCED_OUTPUT_FOLDER_NAME = "Advanced_AI_Classified"
FENCE_CONTEXT_MARGIN = 8.0
FENCE_TEMP_FOLDER_NAME = "_fence_ai_temp"

def load_advanced_model(device):
    if not MODEL_PATH.exists():
        raise FileNotFoundError(f"Advanced model not found: {MODEL_PATH}")

    if not STATS_PATH.exists():
        raise FileNotFoundError(f"Advanced feature_stats.json not found: {STATS_PATH}")

    if not ADVANCE_MODEL_PY.exists():
        raise FileNotFoundError(f"Advanced models/model.py not found: {ADVANCE_MODEL_PY}")

    spec = importlib.util.spec_from_file_location(
        "advance_model",
        str(ADVANCE_MODEL_PY)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    if not hasattr(mod, "PointNet2SSGPro"):
        raise RuntimeError("Advanced models/model.py must contain class PointNet2SSGPro")

    ckpt = torch.load(str(MODEL_PATH), map_location=device, weights_only=False)

    num_features = ckpt.get("num_features", 68)
    num_classes = ckpt.get("num_classes", 5)

    if num_features != 68:
        raise ValueError(
            f"Advanced model expects {num_features} features, "
            f"but advanced pipeline must generate 68 features"
        )

    model = mod.PointNet2SSGPro(
        num_features=num_features,
        num_classes=num_classes
    ).to(device)

    model.load_state_dict(ckpt["model_state_dict"], strict=True)
    model.eval()

    # Safe speed optimization.
    # If torch.compile is not supported, inference continues normally.
    # if hasattr(torch, "compile"):
    #     try:
    #         model = torch.compile(
    #             model,
    #             mode="reduce-overhead",
    #             fullgraph=False
    #         )
    #         print("torch.compile enabled for Advanced AI model")
    #     except Exception as e:
    #         print(f"torch.compile skipped: {e}")

    with open(STATS_PATH, "r", encoding="utf-8") as f:
        stats = json.load(f)

    feat_mean = np.array(stats["mean"], dtype=np.float32)
    feat_std = np.array(stats["std"], dtype=np.float32)

    if len(feat_mean) != 68 or len(feat_std) != 68:
        raise ValueError(
            f"Advanced stats must have 68 values. "
            f"Got mean={len(feat_mean)}, std={len(feat_std)}"
        )

    return model, feat_mean, feat_std


# =====================================================================
# LOGGING SETUP
# =====================================================================

def setup_logging(output_dir: Path) -> logging.Logger:
    """Create a logger that does not break classification if the network log file drops."""
    logging.raiseExceptions = False

    output_dir = Path(output_dir)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fmt = "%(asctime)s | %(levelname)-8s | %(message)s"
    datefmt = "%H:%M:%S"

    handlers = []
    log_file = None

    # Prefer project log folder, but never let network/log permission issues stop AI.
    try:
        log_dir = output_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"inference_{ts}.log"
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    except Exception:
        fallback_dir = Path(tempfile.gettempdir()) / "NakshaAdvancedAI" / "logs"
        fallback_dir.mkdir(parents=True, exist_ok=True)
        log_file = fallback_dir / f"inference_{ts}.log"
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    handlers.append(logging.StreamHandler(sys.stdout))

    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt=datefmt,
        handlers=handlers,
        force=True,
    )

    log = logging.getLogger("inference")
    log.info(f"Log file: {log_file}")
    log.info(f"PROCESS ID: {os.getpid()}")
    log.info(f"THREAD ID : {threading.get_ident()}")

    return log

# =====================================================================
# CONFIGURATION
# =====================================================================

TILE_SIZE    = 50.0
TILE_OVERLAP = 15.0
TILE_MARGIN  = 15.0

NUM_POINTS            = 8192
VOTE_PASSES           = 1
CHUNK_STEP            = NUM_POINTS // 2      # 50% chunk overlap, better accuracy, slower
INFERENCE_BATCH_SIZE  = 8
EXPECTED_NUM_FEATURES = 68

FEATURE_SCALES  = [0.5, 1.0, 2.0, 5.0, 10.0]
GEOM_VOXEL_SIZE = 0.25

GROUND   = 0
LOWVEG   = 1
MIDVEG   = 2
HIGHVEG  = 3
BUILDING = 4

ASPRS_MAP = np.array([1, 2, 3, 4, 5], dtype=np.uint8)

ADV_POWER_WIRE_INTERNAL = 5
ADV_POWER_POLE_INTERNAL = 6

ASPRS_NAMES = {
    0: "Uncategorized",
    1: "Ground",
    2: "LowVeg",
    3: "MidVeg",
    4: "HighVeg",
    5: "Building",
    7: "Low Point",
    14: "Wire / Bare Conductor",
    15: "Pole",
    18: "High Noise",
}

ROOF_MAX_PASSES       = 6
ROOF_CANDIDATE_DIST   = 6.0
ROOF_FAST_RADIUS      = 3.0
ROOF_HAG_FAST         = 1.5
ROOF_BLDG_RATIO_FAST  = 0.20
ROOF_BLDG_RATIO_STD   = 0.30
ROOF_HEIGHT_STD       = 1.8
ROOF_HEIGHT_LO        = 1.4
ROOF_BLDG_RATIO_LO    = 0.60
TERRACE_HAG_MIN       = 2.8
TERRACE_PLANARITY_MIN = 0.40
TERRACE_BLDG_SEARCH_R = 12.0

WALL_CANOPY_RADIUS    = 2.0
WALL_CANOPY_VEG_RATIO = 0.65

MIN_POINTS_INPUT       = 1_000
MAX_POINTS_INPUT       = 80_000_000
MIN_GROUND_PCT         = 2.0
MAX_BUILDING_PCT       = 60.0
CONFIDENCE_FLAG_THRESH = 0.60
MAX_ROOF_CANDIDATES    = 150_000
MAX_SALT_CANDIDATES    = 200_000

# Production fast post-processing mode
# Step 0-8 still run, but heavy vegetation/building cleanup checks suspicious candidates first.
FAST_SAFE_FILTERS = True
FAST_STEP3_FILTER = True

# KDTree speedup: uses all CPU cores where SciPy supports it.
KD_TREE_WORKERS        = -1

# Less logging delay
LOG_EVERY_CHUNKS       = 25

# Faster post-processing chunk size.
# If RAM crash happens, reduce this back to 10_000.
CHUNK_SIZE             = 150_000

# Step 3 candidate cap for speed.
# Increase to 600_000 for stronger but slower cleanup.
MAX_STEP3_BUILDING_CANDIDATES = 80_000

# ---------------------------------------------------------------------
# V13 SAFETY SWITCH
# ---------------------------------------------------------------------
# True  = run the trained model and write raw model output immediately.
#         No V11/V12 post-processing, so Step 6J cannot crash the app.
# False = run post-processing after raw model output is verified.
MODEL_ONLY_ADVANCED_AI = False

# V14 production mode.
# True = apply only small, targeted geometry corrections after model inference.
# This fixes roof holes, roof vegetation patches, and tree/building noise
# without running the old heavy V11/V12 post-processing chain.
LIGHT_GEOMETRY_CORRECTION = True

# Keep this False. The old V11 final false-building cleanup caused very
# high RAM usage and hard app exits on fence subsets.
ENABLE_HEAVY_V11_ROLLBACK = False

# ---------------------------------------------------------------------
# V19 CRASH-FREE TARGET-ONLY WALL/ROOF/VEG CLEANUP SAFETY
# ---------------------------------------------------------------------
# Never run geometry correction across the full context cloud.
# Correction is restricted to the actual selected fence points only.
# This prevents the V14 issue where a 16k-point fence expanded to >1.5M
# context points and crashed in roof-hole correction.
TARGET_ONLY_LIGHT_CORRECTION = True

# Core V19 safety cap per batch.
# Do not increase this too high on 4GB GPU / normal RAM systems.
MAX_TARGET_CORRECTION_POINTS = 80_000

# Batched V19 settings.
# Large fences are processed in batches instead of skipping correction.
V19_BATCHED_CORRECTION = True
V19_BATCH_SIZE = 60_000

MAX_FENCE_RUN_POINTS = 450_000
MIN_FENCE_CONTEXT_MARGIN = 3.0

CHECKPOINT_EVERY_TILES = 100
try:
    import jakteristics
    HAS_JAKTERISTICS = True
except ImportError:
    HAS_JAKTERISTICS = False

try:
    import CSF
    HAS_CSF = True
except ImportError:
    HAS_CSF = False


# =====================================================================
# CACHE SYSTEM
# =====================================================================

def get_cache_key(input_path: Path, model_path: str) -> str:
    stat = input_path.stat()
    key_str = (
        f"{input_path.name}"
        f"_size{stat.st_size}"
        f"_mtime{int(stat.st_mtime)}"
        f"_model{Path(model_path).stat().st_size}"
        f"_tiles{TILE_SIZE}_{TILE_OVERLAP}"
        f"_margin{TILE_MARGIN}"
        f"_fencemargin{FENCE_CONTEXT_MARGIN}"
        f"_pts{NUM_POINTS}"
        f"_votes{VOTE_PASSES}"
        f"_chunk{CHUNK_STEP}"
        f"_scales{'_'.join(str(s) for s in FEATURE_SCALES)}"
        f"_vox{GEOM_VOXEL_SIZE}"
        f"_feat{EXPECTED_NUM_FEATURES}"
        f"_preproc_localxy_v18_wall_roof_veg_cleanup_safe_gui_map"
        f"_modelonly{int(MODEL_ONLY_ADVANCED_AI)}"
        f"_lightgeom{int(LIGHT_GEOMETRY_CORRECTION)}"
    )
    return hashlib.md5(key_str.encode()).hexdigest()[:12]


def get_cache_dir(output_dir: Path) -> Path:
    cache_dir = output_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def save_cache(cache_dir: Path, stem: str, key: str,
               data: dict, log: logging.Logger):
    try:
        for name, arr in data.items():
            fpath    = cache_dir / f"{stem}_{key}_{name}.npy"
            tmp_path = cache_dir / f"{stem}_{key}_{name}.tmp.npy"
            np.save(str(tmp_path), arr)
            shutil.move(str(tmp_path), str(fpath))
            size_mb = fpath.stat().st_size / 1e6
            log.info(f"    Cache saved : {fpath.name}  ({size_mb:.1f} MB)")
    except Exception as e:
        log.warning(f"    Cache save FAILED: {e}")


def load_cache(cache_dir: Path, stem: str, key: str,
               names: list, log: logging.Logger, mmap_names=None):
    result = {}
    mmap_names = set(mmap_names) if mmap_names else set()
    for name in names:
        fpath = cache_dir / f"{stem}_{key}_{name}.npy"
        if not fpath.exists():
            log.info(f"    Cache MISS  : {fpath.name}")
            return None
        try:
            mmap_mode = "r" if name in mmap_names else None
            result[name] = np.load(str(fpath), mmap_mode=mmap_mode,
                                   allow_pickle=False)
            size_mb  = fpath.stat().st_size / 1e6
            mode_txt = " mmap" if mmap_mode else ""
            log.info(f"    Cache HIT   : {fpath.name}  ({size_mb:.1f} MB){mode_txt}")
        except Exception as e:
            log.warning(f"    Cache load FAILED: {fpath.name} - {e}")
            return None
    return result


def save_vote_checkpoint(cache_dir, stem, key, vote_counts, tile_id, log):
    try:
        save_cache(cache_dir, stem, key, {
            "vote_counts_ckpt":      vote_counts,
            "vote_counts_ckpt_tile": np.array([tile_id], dtype=np.int32),
        }, log)
        log.info(f"    CHECKPOINT saved at tile {tile_id}")
    except Exception as e:
        log.warning(f"    CHECKPOINT failed at tile {tile_id}: {e}")


def load_tile_index_cache(cache_dir, stem, key, log):
    path = cache_dir / f"{stem}_{key}_tile_index_data.pkl"
    if not path.exists():
        log.info(f"    Tile index cache MISS: {path.name}")
        return None
    try:
        with open(path, "rb") as f:
            data = pickle.load(f)
        log.info(f"    Tile index cache HIT : {path.name}")
        return data
    except Exception as e:
        log.warning(f"    Tile index cache load FAILED: {e}")
        return None


def save_tile_index_cache(cache_dir, stem, key, tile_index_data, log):
    path     = cache_dir / f"{stem}_{key}_tile_index_data.pkl"
    tmp_path = cache_dir / f"{stem}_{key}_tile_index_data.tmp"
    try:
        with open(tmp_path, "wb") as f:
            pickle.dump(tile_index_data, f, protocol=pickle.HIGHEST_PROTOCOL)
        shutil.move(str(tmp_path), str(path))
        log.info(f"    Tile index cache saved: {path.name}")
    except Exception as e:
        log.warning(f"    Tile index cache save FAILED: {e}")


# =====================================================================
# INPUT VALIDATION
# =====================================================================

def validate_input_file(path: Path, log: logging.Logger) -> dict:
    result = {"path": str(path), "warnings": [], "fatal": None}
    if not path.exists():
        result["fatal"] = f"File not found: {path}"
        raise ValueError(result["fatal"])
    size_mb = path.stat().st_size / 1e6
    result["size_mb"] = round(size_mb, 1)
    if size_mb < 0.01:
        result["fatal"] = f"File too small ({size_mb:.2f} MB) - likely corrupt"
        raise ValueError(result["fatal"])
    try:
        las = laspy.read(str(path))
    except Exception as e:
        result["fatal"] = f"Cannot read file: {e}"
        raise ValueError(result["fatal"])
    n = las.header.point_count
    result["point_count"] = n
    if n < MIN_POINTS_INPUT:
        result["fatal"] = f"Only {n:,} points - below minimum {MIN_POINTS_INPUT:,}"
        raise ValueError(result["fatal"])
    if n > MAX_POINTS_INPUT:
        result["warnings"].append(f"Very large file: {n:,} points")
        log.warning(f"  Large file: {n:,} points")
    xyz = np.column_stack([np.array(las.x), np.array(las.y), np.array(las.z)])
    xr = xyz[:, 0].max() - xyz[:, 0].min()
    yr = xyz[:, 1].max() - xyz[:, 1].min()
    zr = xyz[:, 2].max() - xyz[:, 2].min()
    result["extent_xy_m"] = [round(xr, 1), round(yr, 1)]
    result["z_range_m"]   = round(zr, 1)
    if xr < 1.0 or yr < 1.0:
        result["warnings"].append(f"Very small XY extent ({xr:.1f} x {yr:.1f} m)")
    if zr > 5000:
        result["warnings"].append(f"Z range is {zr:.0f}m - check CRS / units")
    if zr == 0:
        result["fatal"] = "All points have the same Z - file may be 2D only"
        raise ValueError(result["fatal"])
    result["has_intensity"] = hasattr(las, "intensity")
    result["has_returns"]   = (hasattr(las, "return_number") and
                                hasattr(las, "number_of_returns"))
    if not result["has_intensity"]:
        result["warnings"].append("No intensity field - will be zero")
    if not result["has_returns"]:
        result["warnings"].append("No return fields - will be zero")
    for w in result["warnings"]:
        log.warning(f"  VALIDATION: {w}")
    log.info(f"  Validated: {n:,} pts | {xr:.0f}x{yr:.0f}m | Z range {zr:.1f}m | "
             f"intensity={'yes' if result['has_intensity'] else 'no'} | "
             f"returns={'yes' if result['has_returns'] else 'no'}")
    del las, xyz
    gc.collect()
    return result


# =====================================================================
# GPU CHECK
# =====================================================================

def check_gpu(device: torch.device, log: logging.Logger):
    if device.type != "cuda":
        log.warning("Running on CPU - inference will be very slow")
        return
    total    = torch.cuda.get_device_properties(device).total_memory / 1e9
    reserved = torch.cuda.memory_reserved(device) / 1e9
    log.info(f"GPU: {torch.cuda.get_device_name(device)} | "
             f"VRAM {total:.1f}GB total | {reserved:.1f}GB reserved")
    if total < 3.5:
        log.warning("VRAM < 3.5GB - if OOM occurs reduce INFERENCE_BATCH_SIZE to 4")


# =====================================================================
# HELPERS
# =====================================================================

def safe_query(tree, pts, k=1):
    try:
        return tree.query(pts, k=k, workers=KD_TREE_WORKERS)
    except TypeError:
        return tree.query(pts, k=k)


def safe_query_ball_point(tree, pts, r, return_length=False):
    try:
        return tree.query_ball_point(
            pts,
            r=r,
            workers=KD_TREE_WORKERS,
            return_length=return_length,
        )
    except TypeError:
        try:
            return tree.query_ball_point(
                pts,
                r=r,
                return_length=return_length,
            )
        except TypeError:
            result = tree.query_ball_point(pts, r=r)
            if return_length:
                return np.array([len(x) for x in result], dtype=np.int64)
            return result


def batched_neighbor_class_ratios(tree_all, pts_idx, xyz, predictions, radius,
                                   min_neighbors, building_code, highveg_code,
                                   veg_codes):
    """
    Vectorized replacement for the "for pt, nb in zip(pts_idx, nlist): ..."
    Python loops used by the fence roof/wall-rescue and building-footprint-
    completion post-processing passes. For every point in `pts_idx`, finds
    its neighbors within `radius` (via `tree_all`) and computes, among those
    neighbors, the fraction currently classified as Building, as HighVeg,
    and as any vegetation class.

    This reads `predictions` exactly once, up front — matching the original
    per-point loop's semantics exactly, since that loop only ever *read*
    predictions while iterating and applied all resulting label changes in
    one batch afterward (`predictions[to_building] = BUILDING`), so there is
    no ordering dependency between points to preserve. The per-point Python
    loop (with several numpy calls made 10,000-45,000+ times, once per
    candidate point) was the dominant cost of these two post-processing
    passes; this replaces it with a handful of whole-array numpy operations.

    Returns:
        valid_pts             : pts_idx filtered to points with >= min_neighbors
                                 neighbors (points below that are dropped, same
                                 as the original loop's `continue`)
        b_ratio, high_ratio, veg_ratio : float32 arrays aligned with valid_pts
    """
    nlist = safe_query_ball_point(tree_all, xyz[pts_idx], r=radius)
    counts = np.fromiter((len(nb) for nb in nlist), dtype=np.int64, count=len(nlist))

    keep = counts >= min_neighbors
    if not np.any(keep):
        empty = np.empty(0, dtype=np.float32)
        return pts_idx[:0], empty, empty, empty

    valid_pts = pts_idx[keep]
    keep_positions = np.flatnonzero(keep)
    valid_nlist = [nlist[i] for i in keep_positions]
    valid_counts = counts[keep].astype(np.float32)

    neighbor_counts = np.fromiter((len(nb) for nb in valid_nlist), dtype=np.int64,
                                   count=len(valid_nlist))
    owner = np.repeat(np.arange(len(valid_nlist)), neighbor_counts)
    flat_nb = np.concatenate(valid_nlist).astype(np.int64)
    nb_pred = predictions[flat_nb]

    n_valid = len(valid_pts)
    b_counts = np.bincount(owner, weights=(nb_pred == building_code), minlength=n_valid)
    high_counts = np.bincount(owner, weights=(nb_pred == highveg_code), minlength=n_valid)
    veg_counts = np.bincount(owner, weights=np.isin(nb_pred, veg_codes), minlength=n_valid)

    b_ratio = (b_counts / valid_counts).astype(np.float32)
    high_ratio = (high_counts / valid_counts).astype(np.float32)
    veg_ratio = (veg_counts / valid_counts).astype(np.float32)

    return valid_pts, b_ratio, high_ratio, veg_ratio


def safe_clear(device: torch.device):
    gc.collect()
    if device.type == "cuda":
        torch.cuda.empty_cache()


def _cuda_vram_snapshot(device):
    """Return CUDA memory state without ever breaking inference."""
    if getattr(device, "type", None) != "cuda":
        return None
    try:
        free_b, total_b = torch.cuda.mem_get_info(device)
        return {
            "free_gb": float(free_b) / (1024.0 ** 3),
            "total_gb": float(total_b) / (1024.0 ** 3),
            "allocated_gb": float(torch.cuda.memory_allocated(device)) / (1024.0 ** 3),
            "reserved_gb": float(torch.cuda.memory_reserved(device)) / (1024.0 ** 3),
        }
    except Exception:
        return None


def _effective_inference_batch_size(device, requested, *, fence_mode, log):
    """
    Pick a batch from *current free VRAM*, not total VRAM.

    The desktop renderer (VTK/OpenGL, especially Shading/Surface) shares the same
    GPU with PyTorch. Total VRAM is therefore not a safe proxy for AI headroom.
    Fence runs are small enough that a conservative batch is preferable to a
    driver reset / native process exit.
    """
    requested = max(1, int(requested))
    if getattr(device, "type", None) != "cuda":
        return requested

    try:
        torch.cuda.empty_cache()
    except Exception:
        pass

    snap = _cuda_vram_snapshot(device)
    if snap is None:
        return min(requested, 2 if fence_mode else requested)

    free_gb = snap["free_gb"]
    if fence_mode:
        # Fence inference runs inside the live VTK desktop process.  Give the
        # renderer/driver a wide safety margin and never use the old batch=4
        # path that triggered the observed hard exit.
        if free_gb < 4.50:
            effective = 1
        else:
            effective = min(requested, 2)
    else:
        if free_gb < 2.75:
            effective = 1
        elif free_gb < 4.25:
            effective = min(requested, 2)
        elif free_gb < 6.50:
            effective = min(requested, 4)
        else:
            effective = requested

    log.info(
        "  AI GPU HEADROOM: "
        f"free={snap['free_gb']:.2f}GB / total={snap['total_gb']:.2f}GB | "
        f"torch_alloc={snap['allocated_gb']:.2f}GB "
        f"torch_reserved={snap['reserved_gb']:.2f}GB | "
        f"requested_batch={requested} effective_batch={effective} "
        f"fence_mode={int(bool(fence_mode))}"
    )
    return max(1, int(effective))


def to_device_tensor(arr: np.ndarray, device: torch.device) -> torch.Tensor:
    """
    Safe CPU -> GPU transfer.
    Use normal transfer to avoid pinned-memory issues on small GPUs/Windows.
    """
    return torch.from_numpy(arr).to(device)


# =====================================================================
# BATCH FLUSH — KEY SPEED IMPROVEMENT
# =====================================================================

def flush_gpu_batch(gpu_batch, batch_meta, vote_counts,
                    model, device, use_amp, autocast_device, log):
    """Send a batch to the model with CUDA OOM backoff."""
    B = len(gpu_batch)
    if B == 0:
        return

    coords_batch = feats_batch = None
    c_t = f_t = logits = preds = None

    try:
        coords_batch = np.stack([t[0] for t in gpu_batch])
        feats_batch = np.stack([t[1] for t in gpu_batch])
        c_t = to_device_tensor(coords_batch, device)
        f_t = to_device_tensor(feats_batch, device)

        with torch.no_grad():
            with torch.amp.autocast(device_type=autocast_device, enabled=use_amp):
                logits = model(c_t, f_t)
            preds = logits.argmax(dim=-1).detach().cpu().numpy()

        for i, (global_real_idx, real_count) in enumerate(batch_meta):
            real_preds = preds[i, :real_count]
            valid = real_preds < 5
            np.add.at(vote_counts, (global_real_idx[valid], real_preds[valid]), 1)

    except RuntimeError as e:
        err = str(e).lower()
        is_cuda_oom = (
            "out of memory" in err
            or "cuda error" in err
            or "cuda out of memory" in err
        )

        if is_cuda_oom and getattr(device, "type", None) == "cuda" and B > 1:
            snap = _cuda_vram_snapshot(device)
            suffix = "" if snap is None else (
                f" | free={snap['free_gb']:.2f}GB/{snap['total_gb']:.2f}GB"
            )
            log.warning(
                f"    CUDA batch {B} failed; retrying as smaller batches{suffix}: {e}"
            )
            c_t = f_t = logits = preds = coords_batch = feats_batch = None
            safe_clear(device)
            mid = max(1, B // 2)
            flush_gpu_batch(
                gpu_batch[:mid], batch_meta[:mid], vote_counts,
                model, device, use_amp, autocast_device, log
            )
            flush_gpu_batch(
                gpu_batch[mid:], batch_meta[mid:], vote_counts,
                model, device, use_amp, autocast_device, log
            )
            return

        if is_cuda_oom:
            snap = _cuda_vram_snapshot(device)
            suffix = "" if snap is None else (
                f" | free={snap['free_gb']:.2f}GB/{snap['total_gb']:.2f}GB"
            )
            log.error(f"    CUDA OOM at minimum batch {B}{suffix}: {e}")
            safe_clear(device)
        raise

    finally:
        c_t = f_t = logits = preds = coords_batch = feats_batch = None


# =====================================================================
# HAG
# =====================================================================

def compute_hag_csf(points_xyz: np.ndarray, log: logging.Logger) -> np.ndarray:
    """
    Compute HAG safely for Advanced AI.

    Production fix:
    Fence subsets sometimes contain extreme low/high Z outliers or too little usable
    ground. CSF then returns almost no ground points. The old fallback used the
    lowest 10 percent of raw Z values, which can put the ground model hundreds of
    metres too low and makes the model classify most points as elevated vegetation.
    """
    pts = np.asarray(points_xyz, dtype=np.float64)
    n = len(pts)

    if n == 0:
        return np.zeros(0, dtype=np.float32)

    z = pts[:, 2].astype(np.float64, copy=False)

    def _robust_fallback_hag(reason: str) -> np.ndarray:
        log.warning(f"    Robust HAG fallback active: {reason}")

        finite = np.isfinite(z)
        if finite.sum() < 3:
            return np.zeros(n, dtype=np.float32)

        z_med = float(np.nanmedian(z[finite]))
        z_p02 = float(np.nanpercentile(z[finite], 2.0))
        z_p98 = float(np.nanpercentile(z[finite], 98.0))
        z_rng = z_p98 - z_p02

        if z_rng > 120.0:
            lo = max(z_p02, z_med - 60.0)
            hi = min(z_p98, z_med + 80.0)
        else:
            lo = z_p02
            hi = z_p98

        valid = finite & (z >= lo) & (z <= hi)
        if valid.sum() < 64:
            valid = finite

        xy = pts[:, :2]
        xy_valid = xy[valid]
        z_valid = z[valid]
        valid_idx = np.where(valid)[0]

        if len(valid_idx) < 3:
            return np.clip(z - z_med, -2.0, 80.0).astype(np.float32)

        grid = 1.5
        xy_min = xy_valid.min(axis=0)
        cell = np.floor((xy_valid - xy_min) / grid).astype(np.int64)
        key = cell[:, 0] * 10_000_000 + cell[:, 1]

        order = np.lexsort((z_valid, key))
        key_sorted = key[order]
        idx_sorted = valid_idx[order]

        ground_candidates = []
        start = 0
        while start < len(order):
            end = start + 1
            while end < len(order) and key_sorted[end] == key_sorted[start]:
                end += 1

            count = end - start
            if count >= 3:
                take = start + max(0, min(count - 1, int(count * 0.15)))
                ground_candidates.append(idx_sorted[take])
            else:
                ground_candidates.append(idx_sorted[start])

            start = end

        ground_candidates = np.asarray(ground_candidates, dtype=np.int64)

        if len(ground_candidates) < 32:
            z_cut = np.nanpercentile(z_valid, 35.0)
            ground_candidates = valid_idx[z_valid <= z_cut]

        if len(ground_candidates) < 3:
            baseline = float(np.nanpercentile(z_valid, 25.0))
            return np.clip(z - baseline, -2.0, 80.0).astype(np.float32)

        ground_pts = pts[ground_candidates]

        if len(ground_pts) > 500_000:
            rng = np.random.default_rng(42)
            keep = rng.choice(len(ground_pts), 500_000, replace=False)
            ground_pts = ground_pts[keep]

        tree = cKDTree(ground_pts[:, :2])
        k = min(8, len(ground_pts))
        d, ii = safe_query(tree, pts[:, :2], k=k)

        if k == 1:
            d = d.reshape(-1, 1)
            ii = ii.reshape(-1, 1)

        w = 1.0 / (d + 0.25)
        w /= w.sum(axis=1, keepdims=True)
        gz = np.sum(ground_pts[ii, 2] * w, axis=1)

        hag = z - gz
        hag = np.nan_to_num(hag, nan=0.0, posinf=80.0, neginf=-2.0)
        hag = np.clip(hag, -2.0, 80.0).astype(np.float32)

        log.info(
            f"    Robust HAG fallback ground candidates: {len(ground_pts):,} | "
            f"z_med={z_med:.2f} z_p02={z_p02:.2f} z_p98={z_p98:.2f} "
            f"trim=[{lo:.2f},{hi:.2f}]"
        )
        return hag

    if not HAS_CSF:
        return _robust_fallback_hag("CSF package is not installed")

    try:
        csf = CSF.CSF()
        csf.params.bSloopSmooth = False
        csf.params.cloth_resolution = 0.5
        csf.params.rigidness = 3
        csf.params.time_step = 0.65
        csf.params.class_threshold = 0.5

        if hasattr(csf.params, "interations"):
            csf.params.interations = 500
        elif hasattr(csf.params, "iterations"):
            csf.params.iterations = 500

        csf.setPointCloud(pts)
        ground_idx = CSF.VecInt()
        non_ground_idx = CSF.VecInt()
        csf.do_filtering(ground_idx, non_ground_idx)
        ground_idx = np.asarray(ground_idx, dtype=np.int64)
    except Exception as e:
        return _robust_fallback_hag(f"CSF failed: {e}")

    min_ground = max(32, int(0.002 * n))
    if len(ground_idx) < min_ground:
        return _robust_fallback_hag(
            f"CSF returned too few ground points ({len(ground_idx):,}/{n:,})"
        )

    ground_pts = pts[ground_idx]

    gz_med = float(np.nanmedian(ground_pts[:, 2]))
    all_med = float(np.nanmedian(z))
    all_p02 = float(np.nanpercentile(z, 2.0))
    all_p98 = float(np.nanpercentile(z, 98.0))

    if (all_p98 - all_p02) > 120.0 and gz_med < (all_med - 80.0):
        return _robust_fallback_hag(
            f"CSF ground median is an extreme low outlier ({gz_med:.2f} vs {all_med:.2f})"
        )

    log.info(f"    CSF ground points: {len(ground_pts):,} / {n:,}")

    if len(ground_pts) > 500_000:
        rng = np.random.default_rng(42)
        keep = rng.choice(len(ground_pts), 500_000, replace=False)
        ground_pts = ground_pts[keep]

    tree = cKDTree(ground_pts[:, :2])
    k = min(5, len(ground_pts))
    dists, idxs = safe_query(tree, pts[:, :2], k=k)

    if k == 1:
        dists = dists.reshape(-1, 1)
        idxs = idxs.reshape(-1, 1)

    w = 1.0 / (dists + 1e-6)
    w /= w.sum(axis=1, keepdims=True)
    gz = np.sum(ground_pts[idxs, 2] * w, axis=1)

    hag = pts[:, 2] - gz
    hag = np.nan_to_num(hag, nan=0.0, posinf=80.0, neginf=-2.0)
    return np.clip(hag, -2.0, 80.0).astype(np.float32)


# =====================================================================
# VOXEL DOWNSAMPLE
# =====================================================================

def voxel_downsample(xyz: np.ndarray, voxel_size: float):
    shifted = xyz - xyz.min(axis=0)
    vc      = np.floor(shifted / voxel_size).astype(np.int64)
    dims    = vc.max(axis=0) + 1
    max_key = int(dims[0]) * int(dims[1]) * int(dims[2])
    if max_key > 2**62:
        raise ValueError(f"Voxel grid too large: {dims} - reduce GEOM_VOXEL_SIZE")
    keys = (vc[:, 0].astype(np.int64) * int(dims[1]) * int(dims[2])
            + vc[:, 1].astype(np.int64) * int(dims[2])
            + vc[:, 2].astype(np.int64))
    unique_keys, inverse, counts = np.unique(keys, return_inverse=True,
                                              return_counts=True)
    n_vox     = len(unique_keys)
    centroids = np.zeros((n_vox, 3), dtype=np.float64)
    np.add.at(centroids, inverse, xyz)
    centroids /= counts[:, None]
    return centroids, inverse.astype(np.int32), n_vox


# =====================================================================
# GEOMETRIC FEATURES
# =====================================================================
def compute_geometric_features_full(xyz, scales, voxel_size, log):
    if not HAS_JAKTERISTICS:
        raise RuntimeError("Install: pip install jakteristics")

    feat_names = [
        "eigenvalue1", "eigenvalue2", "eigenvalue3",
        "linearity", "planarity", "sphericity",
        "omnivariance", "anisotropy", "eigenentropy",
        "surface_variation", "verticality",
    ]

    ds_pts, vox_map, n_vox = voxel_downsample(xyz, voxel_size)
    log.info(f"    Voxel downsample: {len(xyz):,} -> {n_vox:,} points")
    log.info(f"    FEATURE_SCALES order: {list(scales)}")

    cols_per_scale = len(feat_names)
    ds_all = np.empty(
        (n_vox, cols_per_scale * len(scales)),
        dtype=np.float32
    )

    col_start = 0

    for scale_i, radius in enumerate(scales):
        t0 = time.time()

        log.info(
            f"      START Scale index={scale_i + 1}/{len(scales)} "
            f"radius={radius}m | col_start={col_start}"
        )

        f = jakteristics.compute_features(
            ds_pts.astype(np.float64, copy=False),
            search_radius=radius,
            feature_names=feat_names,
            num_threads=-1,
        )

        f = np.nan_to_num(
            f,
            nan=0.0,
            posinf=0.0,
            neginf=0.0
        ).astype(np.float32, copy=False)

        ds_all[:, col_start:col_start + cols_per_scale] = f
        col_start += cols_per_scale

        del f
        gc.collect()

        log.info(
            f"      DONE  Scale index={scale_i + 1}/{len(scales)} "
            f"radius={radius}m: {time.time() - t0:.1f}s"
        )

    return ds_all, vox_map


# =====================================================================
# DENSITY
# =====================================================================

def compute_density_full(xyz, radius=1.0, chunk_size=200_000):
    tree = cKDTree(xyz)
    n    = len(xyz)
    out  = np.zeros(n, dtype=np.float32)
    for i in range(0, n, chunk_size):
        j        = min(i + chunk_size, n)
        counts   = safe_query_ball_point(tree, xyz[i:j], r=radius, return_length=True)
        out[i:j] = counts.astype(np.float32)
    return out


# =====================================================================
# ROUGHNESS
# =====================================================================

def compute_roughness_full(xyz, k=10, chunk_size=200_000):
    tree = cKDTree(xyz)
    n    = len(xyz)
    out  = np.zeros(n, dtype=np.float32)
    kk   = min(k, max(2, len(xyz) - 1))
    for i in range(0, n, chunk_size):
        j           = min(i + chunk_size, n)
        pts         = xyz[i:j]
        _, idxs     = safe_query(tree, pts, k=kk)
        if kk == 1:
            idxs    = idxs.reshape(-1, 1)
        z_neighbors = xyz[idxs][:, :, 2]
        z_center    = pts[:, 2].reshape(-1, 1)
        out[i:j]    = np.std(z_neighbors - z_center, axis=1).astype(np.float32)
    return out


# =====================================================================
# FEATURE ASSEMBLY
# =====================================================================

def assemble_tile_features(
    tile_points: np.ndarray,
    tile_hag: np.ndarray,
    tile_geom: np.ndarray,
    tile_density: np.ndarray,
    tile_roughness: np.ndarray,
    has_intensity: bool,
    has_returns: bool,
    xy_origin: np.ndarray | None = None,
    z_feature_override: np.ndarray | None = None,
) -> np.ndarray:
    """
    Assemble the exact 68 features expected by the trained model.

    CRITICAL V13 FIX:
    The PointNet coord branch is centered separately before inference.
    The feature branch also needs local X/Y values because feature_stats.json
    was trained with local tile X/Y, not absolute UTM X/Y.

    Columns:
      0,1 = local tile X/Y
      2   = original Z elevation
      3   = HAG
      4   = normalized intensity
      5:10 return features
      11  = density
      12  = roughness
      13: = geometric features
    """
    n = len(tile_points)
    xyz = tile_points[:, :3].astype(np.float32, copy=False)

    features = np.zeros((n, EXPECTED_NUM_FEATURES), dtype=np.float32)

    if xy_origin is None:
        xy_origin = xyz[:, :2].mean(axis=0).astype(np.float32)
    else:
        xy_origin = np.asarray(xy_origin, dtype=np.float32).reshape(2)

    # local X/Y only.
    features[:, 0] = xyz[:, 0] - xy_origin[0]
    features[:, 1] = xyz[:, 1] - xy_origin[1]

    # Direct LAZ keeps original absolute Z.
    # Fence/SNT mode can override this to avoid out-of-training Z distribution.
    if z_feature_override is not None:
        z_feature_override = np.asarray(z_feature_override, dtype=np.float32)
        if len(z_feature_override) != n:
            raise ValueError(
                f"z_feature_override length mismatch: "
                f"{len(z_feature_override):,} != {n:,}"
            )
        features[:, 2] = z_feature_override
    else:
        features[:, 2] = xyz[:, 2]

    features[:, 3] = tile_hag.astype(np.float32, copy=False)

    if has_intensity:
        intensity = tile_points[:, 3].astype(np.float32, copy=False)
        mx = float(np.nanmax(intensity)) if len(intensity) else 0.0
        if mx > 0.0:
            intensity = intensity / mx
        features[:, 4] = intensity

    if has_returns:
        col_rn = 4 if has_intensity else 3
        col_nr = col_rn + 1

        rn = tile_points[:, col_rn].astype(np.float32, copy=False)
        nr = tile_points[:, col_nr].astype(np.float32, copy=False)

        features[:, 5] = rn
        features[:, 6] = nr
        features[:, 7] = np.divide(
            rn,
            nr,
            out=np.zeros_like(rn, dtype=np.float32),
            where=(nr > 0) & np.isfinite(nr) & np.isfinite(rn),
        )
        features[:, 8] = (nr == 1).astype(np.float32)
        features[:, 9] = (rn == 1).astype(np.float32)
        features[:, 10] = (rn == nr).astype(np.float32)

    features[:, 11] = tile_density.astype(np.float32, copy=False)
    features[:, 12] = tile_roughness.astype(np.float32, copy=False)
    features[:, 13:13 + 11 * len(FEATURE_SCALES)] = tile_geom.astype(
        np.float32,
        copy=False,
    )

    return np.nan_to_num(
        features,
        nan=0.0,
        posinf=0.0,
        neginf=0.0,
    ).astype(np.float32, copy=False)


# =====================================================================
# POST-PROCESSING STEP 0
# =====================================================================

def fix_elevated_ground_as_roof(xyz, predictions, hag, planarity_05_raw, log):
    fixes      = 0
    ground_idx = np.where(predictions == GROUND)[0]
    if len(ground_idx) == 0:
        return predictions, fixes
    g_hag      = hag[ground_idx]
    g_plan     = planarity_05_raw[ground_idx]
    elev_flat  = (g_hag > TERRACE_HAG_MIN) & (g_plan > TERRACE_PLANARITY_MIN)
    candidates = ground_idx[elev_flat]
    if len(candidates) == 0:
        return predictions, fixes
    building_idx = np.where(predictions == BUILDING)[0]
    if len(building_idx) == 0:
        return predictions, fixes
    tree_bldg = cKDTree(xyz[building_idx][:, :2])
    dists, _  = safe_query(tree_bldg, xyz[candidates][:, :2], k=1)
    to_fix    = candidates[dists < TERRACE_BLDG_SEARCH_R]
    predictions[to_fix] = BUILDING
    fixes = len(to_fix)
    log.info(f"      Elevated-ground to roof: {fixes:,}")
    return predictions, fixes


# =====================================================================
# POST-PROCESSING STEP 1
# =====================================================================

def fix_ground_on_roofs(xyz, predictions, hag, log, tree_all=None):
    fixes_total = 0

    if tree_all is None:
        tree_all = cKDTree(xyz)
    for pass_num in range(ROOF_MAX_PASSES):
        ground_mask   = predictions == GROUND
        building_mask = predictions == BUILDING
        if ground_mask.sum() == 0 or building_mask.sum() == 0:
            break
        ground_indices = np.where(ground_mask)[0]
        building_xyz   = xyz[building_mask]
        tree_bldg_2d   = cKDTree(building_xyz[:, :2])
        ground_xyz     = xyz[ground_indices]
        ground_hag     = hag[ground_indices]
        dists_to_bldg, _ = safe_query(tree_bldg_2d, ground_xyz[:, :2], k=1)
        cand_mask  = (dists_to_bldg < ROOF_CANDIDATE_DIST) & (ground_hag > 0.8)
        candidates = ground_indices[cand_mask]
        if len(candidates) == 0:
            break
        if len(candidates) > MAX_ROOF_CANDIDATES:
            candidates = candidates[np.random.choice(len(candidates),
                                                      MAX_ROOF_CANDIDATES, replace=False)]
        all_ground_xyz = xyz[ground_indices]
        tree_ground_2d = cKDTree(all_ground_xyz[:, :2])
        k_ground       = min(10, len(all_ground_xyz))
        k_nn           = min(32, len(xyz))
        fixes_this     = 0
        n_chunks       = (len(candidates) + CHUNK_SIZE - 1) // CHUNK_SIZE
        for ci, i in enumerate(range(0, len(candidates), CHUNK_SIZE)):
            chunk     = candidates[i:i + CHUNK_SIZE]
            chunk_xyz = xyz[chunk]
            chunk_hag = hag[chunk]
            d_nn, idx_nn = safe_query(tree_all, chunk_xyz, k=k_nn)
            if k_nn == 1:
                d_nn   = d_nn.reshape(-1, 1)
                idx_nn = idx_nn.reshape(-1, 1)
            d_ground, idx_ground = safe_query(tree_ground_2d, chunk_xyz[:, :2], k=k_ground)
            if k_ground == 1:
                d_ground   = d_ground.reshape(-1, 1)
                idx_ground = idx_ground.reshape(-1, 1)
            for row, pt_idx in enumerate(chunk):
                keep = d_nn[row] < ROOF_FAST_RADIUS
                nb   = idx_nn[row][keep]
                if len(nb) < 5:
                    continue
                bldg_ratio = (predictions[nb] == BUILDING).sum() / len(nb)
                if chunk_hag[row] > ROOF_HAG_FAST and bldg_ratio >= ROOF_BLDG_RATIO_FAST:
                    if predictions[pt_idx] != BUILDING:
                        predictions[pt_idx] = BUILDING
                        fixes_this += 1
                    continue
                if bldg_ratio < ROOF_BLDG_RATIO_STD:
                    continue
                near_mask = d_ground[row] < 20.0
                nearby_z  = all_ground_xyz[idx_ground[row][near_mask], 2]
                if len(nearby_z) == 0:
                    continue
                h_above = xyz[pt_idx, 2] - np.median(nearby_z)
                if ((h_above > ROOF_HEIGHT_STD and hag[pt_idx] > 1.0) or
                        (h_above > ROOF_HEIGHT_LO and bldg_ratio > ROOF_BLDG_RATIO_LO)):
                    if predictions[pt_idx] != BUILDING:
                        predictions[pt_idx] = BUILDING
                        fixes_this += 1
            if n_chunks > 3 and ((ci + 1) % LOG_EVERY_CHUNKS == 0 or (ci + 1) == n_chunks):
                log.info(f"      Roof pass {pass_num+1}: chunk {ci+1}/{n_chunks} | fixes: {fixes_this:,}")
        fixes_total += fixes_this
        log.info(f"      Roof pass {pass_num + 1}: {fixes_this:,} fixed")
        if fixes_this == 0:
            break
    return predictions, fixes_total


# =====================================================================
# POST-PROCESSING STEP 2
# =====================================================================

def fix_building_walls(xyz, predictions, hag,
                       verticality_05_raw, planarity_05_raw, log,
                       tree_all=None):
    fixes         = 0
    building_mask = predictions == BUILDING
    cand_mask     = np.isin(predictions, [LOWVEG, MIDVEG, HIGHVEG])
    if building_mask.sum() == 0 or cand_mask.sum() == 0:
        return predictions, fixes
    building_xyz      = xyz[building_mask]
    building_hag_vals = hag[building_mask]
    tree_bldg_2d      = cKDTree(building_xyz[:, :2])
    if tree_all is None:
        tree_all = cKDTree(xyz)
    cand_indices  = np.where(cand_mask)[0]
    cand_xyz      = xyz[cand_indices]
    cand_hag_vals = hag[cand_indices]
    verticality   = verticality_05_raw[cand_indices]
    planarity     = planarity_05_raw[cand_indices]
    dists, nn_idx = safe_query(tree_bldg_2d, cand_xyz[:, :2], k=1)
    nearby_hag    = building_hag_vals[nn_idx]
    # Balanced wall candidate gate:
    # wide enough to recover sparse facades, strict enough to avoid vegetation flooding.
    geom_ok = (
        (dists < 1.85) &
        (verticality > 0.18) &
        (planarity > 0.045) &
        (cand_hag_vals > 0.28) &
        (cand_hag_vals < nearby_hag + 1.70)
    )
    geom_candidates = cand_indices[geom_ok]
    if len(geom_candidates) > 0:
        log.info(f"      Wall geometry candidates: {len(geom_candidates):,}")
        n_chunks = (len(geom_candidates) + CHUNK_SIZE - 1) // CHUNK_SIZE
        for ci, i in enumerate(range(0, len(geom_candidates), CHUNK_SIZE)):
            chunk   = geom_candidates[i:i + CHUNK_SIZE]
            far_nbs = safe_query_ball_point(tree_all, xyz[chunk], r=WALL_CANOPY_RADIUS)
            for pt, nb in zip(chunk, far_nbs):
                nb = np.asarray(nb, dtype=np.int64)
                if len(nb) < 5:
                    continue
                nb_pred       = predictions[nb]
                veg_ratio     = np.isin(nb_pred, [LOWVEG, MIDVEG, HIGHVEG]).sum() / len(nb)
                highveg_ratio = (nb_pred == HIGHVEG).sum() / len(nb)
                bldg_ratio    = (nb_pred == BUILDING).sum() / len(nb)
                pt_hag = hag[pt]
                pt_vert = verticality_05_raw[pt]
                pt_plan = planarity_05_raw[pt]

                strong_building_geometry = (
                    ((pt_hag > 0.30) and (pt_vert > 0.18) and (pt_plan > 0.040)) or
                    ((pt_hag > 1.20) and (pt_plan > 0.12))
                )

                # Do not block real wall/facade points just because nearby points are HighVeg.
                # The model is currently labeling many building walls as HighVeg.
                if highveg_ratio > 0.55 and bldg_ratio < 0.22 and not strong_building_geometry:
                    continue

                if veg_ratio > 0.82 and bldg_ratio < 0.18 and not strong_building_geometry:
                    continue

                if bldg_ratio < 0.10 and not strong_building_geometry:
                    continue

                predictions[pt] = BUILDING
                fixes += 1
            if n_chunks > 3 and ((ci + 1) % LOG_EVERY_CHUNKS == 0 or (ci + 1) == n_chunks):
                log.info(f"      Wall pass A: chunk {ci+1}/{n_chunks} | fixes: {fixes:,}")
    remaining_mask    = np.isin(predictions, [LOWVEG, MIDVEG, HIGHVEG])
    remaining_indices = np.where(remaining_mask)[0]
    if len(remaining_indices) > 0:
        rem_xyz  = xyz[remaining_indices]
        rem_hag  = hag[remaining_indices]
        rem_vert = verticality_05_raw[remaining_indices]
        d2, nn2   = safe_query(tree_bldg_2d, rem_xyz[:, :2], k=1)
        nearby2   = building_hag_vals[nn2]
        # Conservative facade strip fill. Prevents large vegetation patches becoming building.
        buffer_mask = (
            (d2 < 0.75) &
            (rem_hag > 0.22) &
            (rem_hag < nearby2 + 1.45) &
            (rem_vert > 0.12)
        )
        buffer_idx = remaining_indices[buffer_mask]
        predictions[buffer_idx] = BUILDING
        fixes += len(buffer_idx)
        if len(buffer_idx) > 0:
            log.info(f"      Facade strip fill: {len(buffer_idx):,}")
    return predictions, fixes


# =====================================================================
# POST-PROCESSING STEP 3
# =====================================================================

def fix_salt_pepper(xyz, predictions, confidence,
                    hag, verticality_05_raw, planarity_05_raw, log,
                    tree_all=None):
    fixes = 0

    if tree_all is None:
        tree_all = cKDTree(xyz)
    building_indices_all = np.where(predictions == BUILDING)[0]

    if len(building_indices_all) > 0 and FAST_STEP3_FILTER:
        b_conf = confidence[building_indices_all]
        b_hag  = hag[building_indices_all]
        b_vert = verticality_05_raw[building_indices_all]
        b_plan = planarity_05_raw[building_indices_all]

        obvious_roof = (
            (b_hag > 1.8) &
            (b_plan > 0.55) &
            (b_conf > 0.90)
        )

        obvious_wall = (
            (b_hag > 0.45) &
            (b_vert > 0.45) &
            (b_plan > 0.05) &
            (b_conf > 0.90)
        )

        suspicious = (
            (b_conf < 0.96) |
            ((b_hag < 1.4) & (~obvious_wall)) |
            (~(obvious_roof | obvious_wall))
        )

        building_indices = building_indices_all[suspicious]

        if len(building_indices) > MAX_STEP3_BUILDING_CANDIDATES:
            order = np.argsort(confidence[building_indices])
            building_indices = building_indices[order[:MAX_STEP3_BUILDING_CANDIDATES]]

        log.info(
            f"      Step 3 FAST candidates: "
            f"{len(building_indices):,} / {len(building_indices_all):,}"
        )
    else:
        building_indices = building_indices_all

    if len(building_indices) > 0:
        n_chunks = (len(building_indices) + CHUNK_SIZE - 1) // CHUNK_SIZE
        for ci, i in enumerate(range(0, len(building_indices), CHUNK_SIZE)):
            chunk = building_indices[i:i + CHUNK_SIZE]
            if len(chunk) == 0:
                continue
            nlist     = safe_query_ball_point(tree_all, xyz[chunk], r=2.2)
            _, nn_ids = safe_query(tree_all, xyz[chunk], k=min(32, len(xyz)))
            for row, pt in enumerate(chunk):
                nb = np.asarray(nlist[row], dtype=np.int64)
                if len(nb) < 6:
                    continue
                nb_pred       = predictions[nb]
                b_count       = int((nb_pred == BUILDING).sum())
                b_ratio       = b_count / len(nb)
                veg_ratio     = np.isin(nb_pred, [LOWVEG, MIDVEG, HIGHVEG]).sum() / len(nb)
                highveg_ratio = (nb_pred == HIGHVEG).sum() / len(nb)
                should_remove = (
                    len(nb) < 10 or
                    (b_ratio < 0.50 and b_count < 5) or
                    (confidence[pt] < 0.85 and b_ratio < 0.65 and b_count < 7) or
                    (highveg_ratio > 0.15 and b_count < 18 and b_ratio < 0.82) or
                    (veg_ratio > 0.65 and b_count < 20 and confidence[pt] < 0.97) or
                    (veg_ratio > 0.82 and b_count < 24)
                )
                if should_remove:
                    nn       = np.asarray(nn_ids[row]).flatten()
                    nn_other = predictions[nn[1:]]
                    nn_other = nn_other[nn_other != BUILDING]
                    if len(nn_other) > 0:
                        vals, cnts = np.unique(nn_other, return_counts=True)
                        predictions[pt] = vals[cnts.argmax()]
                    else:
                        predictions[pt] = GROUND
                    fixes += 1
            if n_chunks > 3 and ((ci + 1) % LOG_EVERY_CHUNKS == 0 or (ci + 1) == n_chunks):
                log.info(f"      Salt-pepper remove: chunk {ci+1}/{n_chunks} | fixes: {fixes:,}")
    low_conf   = confidence < 0.55
    candidates = np.where(low_conf & np.isin(predictions, [LOWVEG, MIDVEG, HIGHVEG]))[0]
    if len(candidates) > MAX_SALT_CANDIDATES:
        candidates = candidates[np.random.choice(len(candidates),
                                                   MAX_SALT_CANDIDATES, replace=False)]
    n_chunks = (len(candidates) + CHUNK_SIZE - 1) // CHUNK_SIZE
    for ci, i in enumerate(range(0, len(candidates), CHUNK_SIZE)):
        chunk = candidates[i:i + CHUNK_SIZE]
        if len(chunk) == 0:
            continue
        _, nn_ids = safe_query(tree_all, xyz[chunk], k=min(15, len(xyz)))
        for row, idx in enumerate(chunk):
            nn_c = confidence[nn_ids[row][1:]]
            nn_p = predictions[nn_ids[row][1:]]
            hc   = nn_c > 0.8
            if hc.sum() >= 5:
                vals, cnts = np.unique(nn_p[hc], return_counts=True)
                new_pred   = vals[cnts.argmax()]
                if new_pred != BUILDING and new_pred != predictions[idx]:
                    predictions[idx] = new_pred
                    fixes += 1
        if n_chunks > 3 and ((ci + 1) % LOG_EVERY_CHUNKS == 0 or (ci + 1) == n_chunks):
            log.info(f"      Salt-pepper veg: chunk {ci+1}/{n_chunks} | fixes: {fixes:,}")
    return predictions, fixes


# =====================================================================
# POST-PROCESSING STEP 4
# =====================================================================

def clean_false_buildings_in_vegetation(xyz, predictions, hag,
                                         verticality_05_raw, planarity_05_raw, log,
                                         tree_all=None):
    # Disabled: this cleanup was stripping valid walls/facades back to vegetation.
    fixes = 0
    return predictions, fixes

    if tree_all is None:
        tree_all = cKDTree(xyz)
    building_idx = np.where(predictions == BUILDING)[0]
    if len(building_idx) == 0:
        return predictions, fixes
    if FAST_SAFE_FILTERS:
        b_hag  = hag[building_idx]
        b_vert = verticality_05_raw[building_idx]
        b_plan = planarity_05_raw[building_idx]

        obvious_roof = (b_hag > 1.8) & (b_plan > 0.58)
        obvious_wall = (b_hag > 0.45) & (b_vert > 0.55) & (b_plan > 0.08)

        keep_check = ~(obvious_roof | obvious_wall)
        building_idx_all = building_idx
        building_idx = building_idx[keep_check]

        log.info(
            f"      Step 4 FAST candidates: "
            f"{len(building_idx):,} / {len(building_idx_all):,}"
        )
    n_chunks = (len(building_idx) + CHUNK_SIZE - 1) // CHUNK_SIZE
    for ci, i in enumerate(range(0, len(building_idx), CHUNK_SIZE)):
        chunk = building_idx[i:i + CHUNK_SIZE]
        if len(chunk) == 0:
            continue
        nlist      = safe_query_ball_point(tree_all, xyz[chunk], r=4.0)
        chunk_hag  = hag[chunk]
        chunk_vert = verticality_05_raw[chunk]
        chunk_plan = planarity_05_raw[chunk]
        for row, (pt, nb) in enumerate(zip(chunk, nlist)):
            nb = np.asarray(nb, dtype=np.int64)
            if len(nb) < 6:
                continue
            nb_pred       = predictions[nb]
            veg_mask      = np.isin(nb_pred, [LOWVEG, MIDVEG, HIGHVEG])
            veg_ratio     = veg_mask.sum() / len(nb)
            b_ratio       = (nb_pred == BUILDING).sum() / len(nb)
            highveg_ratio = (nb_pred == HIGHVEG).sum() / len(nb)
            roof_like = (chunk_plan[row] > 0.58 and chunk_hag[row] > 2.5 and b_ratio > 0.35)
            wall_like = ((chunk_vert[row] > 0.82 and chunk_hag[row] > 2.2 and b_ratio > 0.35) or
                         (chunk_vert[row] > 0.68 and chunk_hag[row] > 4.0 and b_ratio > 0.40))
            strong_building_cluster = (b_ratio > 0.82 and chunk_hag[row] > 2.0 and veg_ratio < 0.45)
            if roof_like or wall_like or strong_building_cluster:
                continue
            # should_reclass = ((highveg_ratio > 0.12 and b_ratio < 0.68) or
            #                   (veg_ratio > 0.36 and b_ratio < 0.78) or
            #                   (veg_ratio > 0.50))
            should_reclass = (
                (highveg_ratio > 0.22 and b_ratio < 0.55) or
                (veg_ratio > 0.55 and b_ratio < 0.60) or
                (veg_ratio > 0.70)
            )
            if should_reclass:
                veg_classes = nb_pred[veg_mask]
                if len(veg_classes) == 0:
                    continue
                vals, cnts = np.unique(veg_classes, return_counts=True)
                new_pred   = vals[cnts.argmax()]
                if highveg_ratio > 0.30:
                    new_pred = HIGHVEG
                predictions[pt] = new_pred
                fixes += 1
        if n_chunks > 3 and ((ci + 1) % LOG_EVERY_CHUNKS == 0 or (ci + 1) == n_chunks):
            log.info(f"      Veg-building cleanup: chunk {ci+1}/{n_chunks} | fixes: {fixes:,}")
    return predictions, fixes


# =====================================================================
# POST-PROCESSING STEP 5
# =====================================================================

def fix_building_walls_final_strict(xyz, predictions, hag,
                                     verticality_05_raw, planarity_05_raw, log,
                                     tree_all=None):
    fixes        = 0
    building_idx = np.where(predictions == BUILDING)[0]
    cand_idx     = np.where(np.isin(predictions, [LOWVEG, MIDVEG, HIGHVEG]))[0]
    if len(building_idx) == 0 or len(cand_idx) == 0:
        return predictions, fixes
    building_xyz = xyz[building_idx]
    building_hag = hag[building_idx]
    tree_bldg_2d = cKDTree(building_xyz[:, :2])
    if tree_all is None:
        tree_all = cKDTree(xyz)
    cand_xyz  = xyz[cand_idx]
    cand_hag  = hag[cand_idx]
    cand_vert = verticality_05_raw[cand_idx]
    cand_plan = planarity_05_raw[cand_idx]
    dists, nn_idx = safe_query(tree_bldg_2d, cand_xyz[:, :2], k=1)
    nearby_hag    = building_hag[nn_idx]
    cand_mask = (
        (dists < 0.70) &
        (cand_hag > 0.40) &
        (cand_hag < nearby_hag + 1.20) &
        (cand_vert > 0.24) &
        (cand_plan > 0.05)
    )
    wall_candidates = cand_idx[cand_mask]
    if len(wall_candidates) == 0:
        return predictions, fixes
    log.info(f"      Final wall candidates STRICT: {len(wall_candidates):,}")
    n_chunks = (len(wall_candidates) + CHUNK_SIZE - 1) // CHUNK_SIZE
    for ci, i in enumerate(range(0, len(wall_candidates), CHUNK_SIZE)):
        chunk   = wall_candidates[i:i + CHUNK_SIZE]
        if len(chunk) == 0:
            continue
        nlist   = safe_query_ball_point(tree_all, xyz[chunk], r=1.4)
        d2, nn2 = safe_query(tree_bldg_2d, xyz[chunk][:, :2], k=1)
        nearby2 = building_hag[nn2]
        for row, (pt, nb) in enumerate(zip(chunk, nlist)):
            nb = np.asarray(nb, dtype=np.int64)
            if len(nb) < 8:
                continue
            nb_pred       = predictions[nb]
            veg_ratio     = np.isin(nb_pred, [LOWVEG, MIDVEG, HIGHVEG]).sum() / len(nb)
            highveg_ratio = (nb_pred == HIGHVEG).sum() / len(nb)
            b_ratio       = (nb_pred == BUILDING).sum() / len(nb)
            if highveg_ratio > 0.14:
                continue
            if veg_ratio > 0.52 and b_ratio < 0.42:
                continue
            wall_like = ((d2[row] < 0.70) and (hag[pt] > 0.40) and
                         (hag[pt] < nearby2[row] + 1.20) and
                         (verticality_05_raw[pt] > 0.28) and
                         (planarity_05_raw[pt] > 0.06) and (b_ratio > 0.42))
            if wall_like:
                predictions[pt] = BUILDING
                fixes += 1
        if n_chunks > 3 and ((ci + 1) % LOG_EVERY_CHUNKS == 0 or (ci + 1) == n_chunks):
            log.info(f"      Final wall STRICT: chunk {ci+1}/{n_chunks} | fixes: {fixes:,}")
    return predictions, fixes


# =====================================================================
# POST-PROCESSING STEP 6
# =====================================================================

def recover_wall_base_strip(xyz, predictions, hag,
                             verticality_05_raw, planarity_05_raw, log,
                             tree_all=None):
    fixes         = 0
    building_mask = predictions == BUILDING
    cand_mask     = (predictions == LOWVEG) | (predictions == MIDVEG)
    if building_mask.sum() == 0 or cand_mask.sum() == 0:
        return predictions, fixes
    building_xyz  = xyz[building_mask]
    building_hag  = hag[building_mask]
    tree_bldg_2d  = cKDTree(building_xyz[:, :2])
    if tree_all is None:
        tree_all = cKDTree(xyz)
    cand_indices  = np.where(cand_mask)[0]
    cand_xyz      = xyz[cand_indices]
    cand_hag      = hag[cand_indices]
    cand_vert     = verticality_05_raw[cand_indices]
    cand_plan     = planarity_05_raw[cand_indices]
    dists, nn_idx = safe_query(tree_bldg_2d, cand_xyz[:, :2], k=1)
    near_bldg_hag = building_hag[nn_idx]
    base_mask = (
        (dists < 0.52) &
        (cand_hag > 0.08) &
        (cand_hag < 0.75) &
        (cand_hag < near_bldg_hag + 0.90) &
        (cand_vert > 0.14) &
        (cand_plan > 0.025)
    )
    base_candidates = cand_indices[base_mask]
    if len(base_candidates) == 0:
        return predictions, fixes
    log.info(f"      Wall-base candidates STRICT: {len(base_candidates):,}")
    n_chunks = (len(base_candidates) + CHUNK_SIZE - 1) // CHUNK_SIZE
    for ci, i in enumerate(range(0, len(base_candidates), CHUNK_SIZE)):
        chunk = base_candidates[i:i + CHUNK_SIZE]
        nlist = safe_query_ball_point(tree_all, xyz[chunk], r=1.0)
        for pt, nb in zip(chunk, nlist):
            nb = np.asarray(nb, dtype=np.int64)
            if len(nb) < 8:
                continue
            nb_pred       = predictions[nb]
            bldg_ratio    = (nb_pred == BUILDING).sum() / len(nb)
            veg_ratio     = np.isin(nb_pred, [LOWVEG, MIDVEG, HIGHVEG]).sum() / len(nb)
            highveg_ratio = (nb_pred == HIGHVEG).sum() / len(nb)
            if highveg_ratio > 0.12:
                continue
            if veg_ratio > 0.50 and bldg_ratio < 0.42:
                continue
            if bldg_ratio >= 0.42:
                predictions[pt] = BUILDING
                fixes += 1
        if n_chunks > 3 and ((ci + 1) % LOG_EVERY_CHUNKS == 0 or (ci + 1) == n_chunks):
            log.info(f"      Wall-base STRICT: chunk {ci+1}/{n_chunks} | fixes: {fixes:,}")
    return predictions, fixes

def recover_attached_building_base_from_vegetation(
    xyz,
    predictions,
    hag,
    verticality_05_raw,
    planarity_05_raw,
    log,
    tree_all=None,
):
    """
    Recover lower wall / building-base points that remain vegetation.

    This is for cases where roof is correctly building but lower facade/base
    points are sparse and get classified as vegetation.
    """
    fixes = 0

    building_idx = np.where(predictions == BUILDING)[0]
    cand_idx = np.where(np.isin(predictions, [LOWVEG, MIDVEG, HIGHVEG]))[0]

    if len(building_idx) == 0 or len(cand_idx) == 0:
        return predictions, fixes

    building_xyz = xyz[building_idx]
    building_hag = hag[building_idx]

    tree_bldg_2d = cKDTree(building_xyz[:, :2])

    if tree_all is None:
        tree_all = cKDTree(xyz)

    cand_xyz = xyz[cand_idx]
    cand_hag = hag[cand_idx]
    cand_vert = verticality_05_raw[cand_idx]
    cand_plan = planarity_05_raw[cand_idx]

    d2, nn = safe_query(tree_bldg_2d, cand_xyz[:, :2], k=1)
    near_bldg_hag = building_hag[nn]

    # Slightly wider than old wall-base recovery.
    # This catches sparse facade/base points under roof edges.
    base_mask = (
        (d2 < 0.85) &
        (cand_hag > 0.10) &
        (cand_hag < 1.50) &
        (cand_hag < near_bldg_hag + 1.50) &
        (
            (cand_vert > 0.12) |
            (cand_plan > 0.025)
        )
    )

    base_candidates = cand_idx[base_mask]

    if len(base_candidates) == 0:
        return predictions, fixes

    log.info(f"      Attached building-base candidates: {len(base_candidates):,}")

    n_chunks = (len(base_candidates) + CHUNK_SIZE - 1) // CHUNK_SIZE

    for ci, i in enumerate(range(0, len(base_candidates), CHUNK_SIZE)):
        chunk = base_candidates[i:i + CHUNK_SIZE]
        nlist = safe_query_ball_point(tree_all, xyz[chunk], r=1.40)

        for pt, nb in zip(chunk, nlist):
            nb = np.asarray(nb, dtype=np.int64)

            if len(nb) < 8:
                continue

            nb_pred = predictions[nb]

            b_ratio = (nb_pred == BUILDING).sum() / len(nb)
            highveg_ratio = (nb_pred == HIGHVEG).sum() / len(nb)
            veg_ratio = np.isin(nb_pred, [LOWVEG, MIDVEG, HIGHVEG]).sum() / len(nb)

            # Avoid converting real tree/canopy mass.
            if highveg_ratio > 0.45 and b_ratio < 0.35:
                continue

            # Enough building support nearby means it is facade/base, not random veg.
            if b_ratio >= 0.38 or (veg_ratio < 0.62 and b_ratio >= 0.28):
                predictions[pt] = BUILDING
                fixes += 1

        if n_chunks > 3 and ((ci + 1) % LOG_EVERY_CHUNKS == 0 or (ci + 1) == n_chunks):
            log.info(
                f"      Attached building-base recovery: "
                f"chunk {ci + 1}/{n_chunks} | fixes: {fixes:,}"
            )

    return predictions, fixes




def recover_highveg_building_facade_roof(
    xyz,
    predictions,
    hag,
    verticality_05_raw,
    planarity_05_raw,
    log,
    tree_all=None,
):
    """
    V11 roof/facade rescue.

    The previous v10 rescue was still too weak for tiled roofs because it rejected
    roof areas that were already mostly HighVeg. This version:
    - only rescues MID/HIGH vegetation, never LowVeg shrubs/ground;
    - lets roof-like HighVeg convert with lower local building ratio;
    - keeps facade recovery strict.
    """
    fixes = 0

    building_idx = np.where(predictions == BUILDING)[0]
    cand_idx = np.where(
        (predictions == HIGHVEG) |
        ((predictions == MIDVEG) & (hag > 0.95))
    )[0]

    if len(building_idx) == 0 or len(cand_idx) == 0:
        return predictions, fixes

    building_xyz = xyz[building_idx]
    building_hag = hag[building_idx]
    tree_bldg_2d = cKDTree(building_xyz[:, :2])

    if tree_all is None:
        tree_all = cKDTree(xyz)

    cand_xyz = xyz[cand_idx]
    cand_hag = hag[cand_idx]
    cand_vert = verticality_05_raw[cand_idx]
    cand_plan = planarity_05_raw[cand_idx]

    dists, nn_idx = safe_query(tree_bldg_2d, cand_xyz[:, :2], k=1)
    near_bldg_hag = building_hag[nn_idx]

    roof_seed = (
        (dists < 1.55) &
        (cand_hag > 1.05) &
        (cand_hag < near_bldg_hag + 1.70) &
        (cand_plan > 0.070) &
        (cand_vert < 0.78)
    )

    facade_seed = (
        (dists < 0.90) &
        (cand_hag > 0.40) &
        (cand_hag < near_bldg_hag + 1.30) &
        (cand_vert > 0.30) &
        (cand_plan > 0.060)
    )

    candidates = cand_idx[roof_seed | facade_seed]

    if len(candidates) == 0:
        return predictions, fixes

    if len(candidates) > 90_000:
        # Prefer points closest to existing building support and more planar/vertical.
        d = dists[roof_seed | facade_seed]
        score = (1.0 / (d + 0.05)) + planarity_05_raw[candidates] + 0.3 * verticality_05_raw[candidates]
        keep = np.argsort(-score)[:90_000]
        candidates = candidates[keep]

    log.info(f"      V11 roof/facade rescue candidates: {len(candidates):,}")

    n_chunks = (len(candidates) + CHUNK_SIZE - 1) // CHUNK_SIZE

    for ci, i in enumerate(range(0, len(candidates), CHUNK_SIZE)):
        chunk = candidates[i:i + CHUNK_SIZE]
        nlist = safe_query_ball_point(tree_all, xyz[chunk], r=1.35)

        for pt, nb in zip(chunk, nlist):
            nb = np.asarray(nb, dtype=np.int64)
            if len(nb) < 8:
                continue

            nb_pred = predictions[nb]
            b_ratio = (nb_pred == BUILDING).sum() / len(nb)
            low_ratio = (nb_pred == LOWVEG).sum() / len(nb)
            mid_ratio = (nb_pred == MIDVEG).sum() / len(nb)
            high_ratio = (nb_pred == HIGHVEG).sum() / len(nb)
            veg_ratio = low_ratio + mid_ratio + high_ratio

            h = hag[pt]
            v = verticality_05_raw[pt]
            p = planarity_05_raw[pt]

            roof_like = (
                h > 1.05 and
                p > 0.070 and
                v < 0.78
            )

            facade_like = (
                h > 0.40 and
                v > 0.30 and
                p > 0.060
            )

            # Roofs in your screenshots are often HighVeg-dominant, so do not
            # reject them only because high_ratio is high. Require building support
            # and planar roof geometry instead.
            if roof_like and b_ratio >= 0.20 and veg_ratio < 0.92:
                predictions[pt] = BUILDING
                fixes += 1
                continue

            # Facades/walls must stay stricter to avoid vegetation columns.
            if facade_like and b_ratio >= 0.38 and high_ratio < 0.36 and veg_ratio < 0.62:
                predictions[pt] = BUILDING
                fixes += 1

        if n_chunks > 3 and ((ci + 1) % LOG_EVERY_CHUNKS == 0 or (ci + 1) == n_chunks):
            log.info(
                f"      V11 roof/facade rescue: "
                f"chunk {ci + 1}/{n_chunks} | fixes: {fixes:,}"
            )

    return predictions, fixes


def expand_building_roof_surface_region_grow(
    xyz,
    predictions,
    hag,
    verticality_05_raw,
    planarity_05_raw,
    log,
    tree_all=None,
    max_passes=2,
):
    """
    V11 narrow roof-only grow.

    This grows roof surfaces inward, but it does not touch LowVeg/ground and
    it does not do broad facade/edge fill. That avoids the earlier vegetation
    blobs becoming Building.
    """
    fixes_total = 0

    if tree_all is None:
        tree_all = cKDTree(xyz)

    for pass_no in range(1, max_passes + 1):
        building_idx = np.where(predictions == BUILDING)[0]
        cand_idx = np.where(
            (predictions == HIGHVEG) |
            ((predictions == MIDVEG) & (hag > 1.05))
        )[0]

        if len(building_idx) == 0 or len(cand_idx) == 0:
            break

        building_xyz = xyz[building_idx]
        building_hag = hag[building_idx]
        tree_bldg_2d = cKDTree(building_xyz[:, :2])

        cand_xyz = xyz[cand_idx]
        cand_hag = hag[cand_idx]
        cand_vert = verticality_05_raw[cand_idx]
        cand_plan = planarity_05_raw[cand_idx]

        d2, nn = safe_query(tree_bldg_2d, cand_xyz[:, :2], k=1)
        near_hag = building_hag[nn]

        roof_like = (
            (cand_hag > 1.05) &
            (cand_plan > 0.075) &
            (cand_vert < 0.78)
        )
        height_ok = (cand_hag > near_hag - 1.25) & (cand_hag < near_hag + 1.45)

        grow_mask = (
            (d2 < 1.35) &
            height_ok &
            roof_like
        )

        candidates = cand_idx[grow_mask]
        if len(candidates) == 0:
            break

        if len(candidates) > 70_000:
            candidate_d = d2[grow_mask]
            score = (1.0 / (candidate_d + 0.05)) + planarity_05_raw[candidates]
            keep = np.argsort(-score)[:70_000]
            candidates = candidates[keep]

        log.info(
            f"      V11 roof-only grow pass {pass_no}: "
            f"candidates={len(candidates):,}"
        )

        fixes_this = 0
        n_chunks = (len(candidates) + CHUNK_SIZE - 1) // CHUNK_SIZE

        for ci, i in enumerate(range(0, len(candidates), CHUNK_SIZE)):
            chunk = candidates[i:i + CHUNK_SIZE]
            nlist = safe_query_ball_point(tree_all, xyz[chunk], r=1.25)

            for pt, nb in zip(chunk, nlist):
                nb = np.asarray(nb, dtype=np.int64)
                if len(nb) < 8:
                    continue

                nb_pred = predictions[nb]
                b_ratio = (nb_pred == BUILDING).sum() / len(nb)
                veg_ratio = np.isin(nb_pred, [LOWVEG, MIDVEG, HIGHVEG]).sum() / len(nb)

                h = hag[pt]
                v = verticality_05_raw[pt]
                p = planarity_05_raw[pt]

                if h > 1.05 and p > 0.075 and v < 0.78 and b_ratio >= 0.24 and veg_ratio < 0.92:
                    predictions[pt] = BUILDING
                    fixes_this += 1

            if n_chunks > 3 and ((ci + 1) % LOG_EVERY_CHUNKS == 0 or (ci + 1) == n_chunks):
                log.info(
                    f"      V11 roof-only grow pass {pass_no}: "
                    f"chunk {ci + 1}/{n_chunks} | fixes={fixes_this:,}"
                )

        fixes_total += fixes_this
        log.info(f"      V11 roof-only grow pass {pass_no}: {fixes_this:,} fixed")

        if fixes_this < 80:
            break

    return predictions, fixes_total

def rollback_canopy_false_buildings_from_raw(
    xyz,
    predictions,
    raw_predictions,
    hag,
    verticality_05_raw,
    planarity_05_raw,
    log,
    tree_all=None,
):
    """
    Remove false Building points created by aggressive rescue/grow steps.

    Key idea:
    If a point was raw vegetation before post-processing and became Building,
    it must keep clear roof/facade geometry. Otherwise, rollback to the raw class.
    This prevents tree/canopy blobs near buildings from becoming Building.
    """
    fixes = 0

    if raw_predictions is None:
        return predictions, fixes

    raw_predictions = np.asarray(raw_predictions)
    if len(raw_predictions) != len(predictions):
        log.warning("      Canopy rollback skipped: raw/current length mismatch")
        return predictions, fixes

    if tree_all is None:
        tree_all = cKDTree(xyz)

    changed_idx = np.where(
        (predictions == BUILDING) &
        (raw_predictions != BUILDING) &
        np.isin(raw_predictions, [LOWVEG, MIDVEG, HIGHVEG])
    )[0]

    if len(changed_idx) == 0:
        return predictions, fixes

    # Only inspect suspicious points. Strong roof/wall geometry is kept.
    pt_hag = hag[changed_idx]
    pt_vert = verticality_05_raw[changed_idx]
    pt_plan = planarity_05_raw[changed_idx]

    strong_roof = (pt_hag > 1.25) & (pt_plan > 0.16)
    roof_like = (pt_hag > 0.95) & (pt_plan > 0.12)
    wall_like = (pt_hag > 0.35) & (pt_vert > 0.18) & (pt_plan > 0.040)

    suspicious_idx = changed_idx[~(strong_roof | roof_like | wall_like)]

    if len(suspicious_idx) == 0:
        return predictions, fixes

    log.info(f"      Canopy rollback candidates: {len(suspicious_idx):,}")

    n_chunks = (len(suspicious_idx) + CHUNK_SIZE - 1) // CHUNK_SIZE

    for ci, i in enumerate(range(0, len(suspicious_idx), CHUNK_SIZE)):
        chunk = suspicious_idx[i:i + CHUNK_SIZE]
        nlist = safe_query_ball_point(tree_all, xyz[chunk], r=2.20)

        for pt, nb in zip(chunk, nlist):
            nb = np.asarray(nb, dtype=np.int64)
            if len(nb) < 10:
                continue

            cur_nb = predictions[nb]
            raw_nb = raw_predictions[nb]

            cur_b_ratio = (cur_nb == BUILDING).sum() / len(nb)
            raw_veg_ratio = np.isin(raw_nb, [LOWVEG, MIDVEG, HIGHVEG]).sum() / len(nb)
            raw_high_ratio = (raw_nb == HIGHVEG).sum() / len(nb)

            h = hag[pt]
            v = verticality_05_raw[pt]
            p = planarity_05_raw[pt]

            tree_like = (
                (h > 0.35) and
                (p < 0.085) and
                ((v < 0.22) or (raw_high_ratio > 0.45))
            )

            weak_structure = (
                (p < 0.110) and
                not ((h > 0.35 and v > 0.20 and p > 0.045) or (h > 1.10 and p > 0.140))
            )

            rollback = (
                (tree_like and raw_veg_ratio > 0.45 and cur_b_ratio < 0.88) or
                (raw_high_ratio > 0.60 and p < 0.10 and cur_b_ratio < 0.82) or
                (raw_veg_ratio > 0.82 and weak_structure and cur_b_ratio < 0.86)
            )

            if rollback:
                predictions[pt] = raw_predictions[pt]
                fixes += 1

        if n_chunks > 3 and ((ci + 1) % LOG_EVERY_CHUNKS == 0 or (ci + 1) == n_chunks):
            log.info(
                f"      Canopy rollback: chunk {ci + 1}/{n_chunks} | fixes: {fixes:,}"
            )

    return predictions, fixes


def fill_remaining_building_roof_holes(
    xyz,
    predictions,
    hag,
    verticality_05_raw,
    planarity_05_raw,
    log,
    tree_all=None,
):
    """
    Final small roof/facade hole fill.

    This converts only non-building points that sit inside/next to a dense
    Building cluster and have clear roof/facade geometry. It is safer than a
    wide region-grow because it requires strong local Building support.
    """
    fixes = 0

    if tree_all is None:
        tree_all = cKDTree(xyz)

    cand_idx = np.where(np.isin(predictions, [LOWVEG, MIDVEG, HIGHVEG, GROUND]))[0]
    if len(cand_idx) == 0:
        return predictions, fixes

    cand_hag = hag[cand_idx]
    cand_vert = verticality_05_raw[cand_idx]
    cand_plan = planarity_05_raw[cand_idx]

    geom_mask = (
        ((cand_hag > 0.95) & (cand_plan > 0.120)) |
        ((cand_hag > 0.35) & (cand_vert > 0.20) & (cand_plan > 0.045))
    )

    candidates = cand_idx[geom_mask]
    if len(candidates) == 0:
        return predictions, fixes

    if len(candidates) > 30_000:
        # v10: optional hole fill must stay narrow.
        score = planarity_05_raw[candidates] + 0.25 * verticality_05_raw[candidates]
        keep = np.argsort(-score)[:30_000]
        candidates = candidates[keep]

    log.info(f"      Remaining roof/facade hole-fill candidates: {len(candidates):,}")

    n_chunks = (len(candidates) + CHUNK_SIZE - 1) // CHUNK_SIZE

    for ci, i in enumerate(range(0, len(candidates), CHUNK_SIZE)):
        chunk = candidates[i:i + CHUNK_SIZE]
        nlist = safe_query_ball_point(tree_all, xyz[chunk], r=1.80)

        for pt, nb in zip(chunk, nlist):
            nb = np.asarray(nb, dtype=np.int64)
            if len(nb) < 12:
                continue

            nb_pred = predictions[nb]
            b_ratio = (nb_pred == BUILDING).sum() / len(nb)
            highveg_ratio = (nb_pred == HIGHVEG).sum() / len(nb)

            h = hag[pt]
            v = verticality_05_raw[pt]
            p = planarity_05_raw[pt]

            roof_hole = (
                h > 1.10 and
                p > 0.155 and
                b_ratio >= 0.36 and
                highveg_ratio < 0.45
            )
            wall_hole = (
                h > 0.45 and
                v > 0.24 and
                p > 0.060 and
                b_ratio >= 0.40 and
                highveg_ratio < 0.38
            )

            if roof_hole or wall_hole:
                predictions[pt] = BUILDING
                fixes += 1

        if n_chunks > 3 and ((ci + 1) % LOG_EVERY_CHUNKS == 0 or (ci + 1) == n_chunks):
            log.info(
                f"      Remaining roof/facade hole fill: "
                f"chunk {ci + 1}/{n_chunks} | fixes: {fixes:,}"
            )

    return predictions, fixes


def fill_building_edge_seams(
    xyz,
    predictions,
    hag,
    verticality_05_raw,
    planarity_05_raw,
    log,
    tree_all=None,
):
    """
    Final narrow edge seam fill.

    Purpose:
    - Fill small roof/eave/wall-edge gaps still left as vegetation/ground.
    - Avoid large canopy/tree growth by requiring close Building support.
    - Runs after Step 6F and before Step 7.
    """
    fixes = 0

    building_idx = np.where(predictions == BUILDING)[0]
    cand_idx = np.where(np.isin(predictions, [GROUND, LOWVEG, MIDVEG, HIGHVEG]))[0]

    if len(building_idx) == 0 or len(cand_idx) == 0:
        return predictions, fixes

    if tree_all is None:
        tree_all = cKDTree(xyz)

    building_xyz = xyz[building_idx]
    building_hag = hag[building_idx]
    tree_bldg_2d = cKDTree(building_xyz[:, :2])

    cand_xyz = xyz[cand_idx]
    cand_hag = hag[cand_idx]
    cand_vert = verticality_05_raw[cand_idx]
    cand_plan = planarity_05_raw[cand_idx]

    dists, nn_idx = safe_query(tree_bldg_2d, cand_xyz[:, :2], k=1)
    near_bldg_hag = building_hag[nn_idx]

    # Narrow gate: only points very close to existing Building edges.
    edge_mask = (
        (dists < 0.70) &
        (cand_hag > 0.35) &
        (cand_hag < near_bldg_hag + 1.10) &
        (
            ((cand_plan > 0.075) & (cand_hag > 0.70)) |
            ((cand_vert > 0.16) & (cand_plan > 0.030))
        )
    )

    edge_candidates = cand_idx[edge_mask]

    if len(edge_candidates) == 0:
        return predictions, fixes

    # Keep this pass bounded. Prefer strongest structural/near-edge candidates.
    if len(edge_candidates) > 45_000:
        score = (
            1.20 * planarity_05_raw[edge_candidates] +
            0.55 * verticality_05_raw[edge_candidates] +
            0.20 * np.clip(hag[edge_candidates], 0.0, 5.0)
        )
        keep = np.argsort(-score)[:45_000]
        edge_candidates = edge_candidates[keep]

    log.info(f"      Building edge seam candidates: {len(edge_candidates):,}")

    n_chunks = (len(edge_candidates) + CHUNK_SIZE - 1) // CHUNK_SIZE

    for ci, i in enumerate(range(0, len(edge_candidates), CHUNK_SIZE)):
        chunk = edge_candidates[i:i + CHUNK_SIZE]
        nlist = safe_query_ball_point(tree_all, xyz[chunk], r=1.35)

        for pt, nb in zip(chunk, nlist):
            nb = np.asarray(nb, dtype=np.int64)

            if len(nb) < 10:
                continue

            nb_pred = predictions[nb]

            b_ratio = (nb_pred == BUILDING).sum() / len(nb)
            highveg_ratio = (nb_pred == HIGHVEG).sum() / len(nb)
            veg_ratio = np.isin(nb_pred, [LOWVEG, MIDVEG, HIGHVEG]).sum() / len(nb)

            pt_hag = hag[pt]
            pt_vert = verticality_05_raw[pt]
            pt_plan = planarity_05_raw[pt]

            roof_edge_like = (
                (pt_hag > 0.70) and
                (pt_plan > 0.075)
            )

            wall_edge_like = (
                (pt_hag > 0.35) and
                (pt_vert > 0.16) and
                (pt_plan > 0.030)
            )

            # Strong local building support required.
            # Prevents trees/canopy from being pulled into Building.
            if b_ratio >= 0.42 and (roof_edge_like or wall_edge_like):
                if highveg_ratio < 0.54 or b_ratio >= 0.56:
                    predictions[pt] = BUILDING
                    fixes += 1
                    continue

            # Extra strict fallback for roof/eave edges only.
            if (
                b_ratio >= 0.34 and
                roof_edge_like and
                veg_ratio < 0.58 and
                highveg_ratio < 0.45
            ):
                predictions[pt] = BUILDING
                fixes += 1

        if n_chunks > 3 and ((ci + 1) % LOG_EVERY_CHUNKS == 0 or (ci + 1) == n_chunks):
            log.info(
                f"      Building edge seam fill: "
                f"chunk {ci + 1}/{n_chunks} | fixes: {fixes:,}"
            )

    return predictions, fixes


def final_rollback_false_vegetation_buildings(
    xyz,
    predictions,
    raw_predictions,
    hag,
    verticality_05_raw,
    planarity_05_raw,
    log,
    tree_all=None,
):
    """
    Final rollback after hole-fill / edge-seam passes.

    This is intentionally stricter than Step 6E because Step 6F and Step 6G
    can convert nearby vegetation into Building after the first rollback pass.

    It only checks points that:
    - were vegetation in the raw model output, and
    - became Building during post-processing.

    True roof/wall points are preserved when they have both structural geometry
    and local Building support.
    """
    fixes = 0

    if raw_predictions is None:
        return predictions, fixes

    raw_predictions = np.asarray(raw_predictions)
    if len(raw_predictions) != len(predictions):
        log.warning("      Final rollback skipped: raw/current length mismatch")
        return predictions, fixes

    if tree_all is None:
        tree_all = cKDTree(xyz)

    changed_idx = np.where(
        (predictions == BUILDING) &
        (raw_predictions != BUILDING) &
        np.isin(raw_predictions, [LOWVEG, MIDVEG, HIGHVEG])
    )[0]

    if len(changed_idx) == 0:
        return predictions, fixes

    # Fast pre-filter: only ambiguous recovered building points.
    h = hag[changed_idx]
    v = verticality_05_raw[changed_idx]
    p = planarity_05_raw[changed_idx]

    strong_structural = (
        ((h > 1.20) & (p > 0.20)) |
        ((h > 0.45) & (v > 0.28) & (p > 0.070))
    )

    inspect_idx = changed_idx[~strong_structural]

    if len(inspect_idx) == 0:
        return predictions, fixes

    log.info(f"      Final false-veg rollback candidates: {len(inspect_idx):,}")

    n_chunks = (len(inspect_idx) + CHUNK_SIZE - 1) // CHUNK_SIZE

    for ci, i in enumerate(range(0, len(inspect_idx), CHUNK_SIZE)):
        chunk = inspect_idx[i:i + CHUNK_SIZE]
        nlist = safe_query_ball_point(tree_all, xyz[chunk], r=2.10)

        for pt, nb in zip(chunk, nlist):
            nb = np.asarray(nb, dtype=np.int64)
            if len(nb) < 10:
                continue

            cur_nb = predictions[nb]
            raw_nb = raw_predictions[nb]

            cur_b_ratio = (cur_nb == BUILDING).sum() / len(nb)
            raw_b_ratio = (raw_nb == BUILDING).sum() / len(nb)
            raw_veg_ratio = np.isin(raw_nb, [LOWVEG, MIDVEG, HIGHVEG]).sum() / len(nb)
            raw_high_ratio = (raw_nb == HIGHVEG).sum() / len(nb)

            pt_hag = hag[pt]
            pt_vert = verticality_05_raw[pt]
            pt_plan = planarity_05_raw[pt]

            real_roof = (
                (pt_hag > 1.00) and
                (pt_plan > 0.160) and
                (cur_b_ratio >= 0.34) and
                (raw_high_ratio < 0.72)
            )

            real_wall = (
                (pt_hag > 0.40) and
                (pt_vert > 0.22) and
                (pt_plan > 0.055) and
                (cur_b_ratio >= 0.36) and
                (raw_high_ratio < 0.66)
            )

            dense_supported_building = (
                (cur_b_ratio >= 0.62) and
                (raw_b_ratio >= 0.08) and
                (pt_plan > 0.090 or pt_vert > 0.24)
            )

            if real_roof or real_wall or dense_supported_building:
                continue

            weak_or_tree_like = (
                (pt_plan < 0.090) or
                ((pt_vert < 0.20) and (pt_plan < 0.125)) or
                (pt_hag < 0.45)
            )

            rollback = (
                (raw_high_ratio > 0.52 and weak_or_tree_like and cur_b_ratio < 0.74) or
                (raw_veg_ratio > 0.78 and pt_plan < 0.125 and cur_b_ratio < 0.68) or
                (raw_b_ratio < 0.04 and raw_veg_ratio > 0.70 and cur_b_ratio < 0.58) or
                (raw_high_ratio > 0.70 and cur_b_ratio < 0.82 and pt_plan < 0.150)
            )

            if rollback:
                predictions[pt] = raw_predictions[pt]
                fixes += 1

        if n_chunks > 3 and ((ci + 1) % LOG_EVERY_CHUNKS == 0 or (ci + 1) == n_chunks):
            log.info(
                f"      Final false-veg rollback: "
                f"chunk {ci + 1}/{n_chunks} | fixes: {fixes:,}"
            )

    return predictions, fixes


# =====================================================================
# POST-PROCESSING STEP 7
# =====================================================================

def clean_final_building_speckles_only(xyz, predictions, hag,
                                        verticality_05_raw, planarity_05_raw, log,
                                        tree_all=None):
    fixes = 0

    if tree_all is None:
        tree_all = cKDTree(xyz)
    building_idx = np.where(predictions == BUILDING)[0]
    if len(building_idx) == 0:
        return predictions, fixes
    if FAST_SAFE_FILTERS:
        b_hag  = hag[building_idx]
        b_vert = verticality_05_raw[building_idx]
        b_plan = planarity_05_raw[building_idx]

        obvious_roof = (b_hag > 1.8) & (b_plan > 0.48)
        obvious_wall = (b_hag > 0.25) & (b_vert > 0.45) & (b_plan > 0.05)

        keep_check = ~(obvious_roof | obvious_wall)
        building_idx_all = building_idx
        building_idx = building_idx[keep_check]

        log.info(
            f"      Step 7 FAST candidates: "
            f"{len(building_idx):,} / {len(building_idx_all):,}"
        )
    n_chunks = (len(building_idx) + CHUNK_SIZE - 1) // CHUNK_SIZE
    for ci, i in enumerate(range(0, len(building_idx), CHUNK_SIZE)):
        chunk = building_idx[i:i + CHUNK_SIZE]
        if len(chunk) == 0:
            continue
        nlist = safe_query_ball_point(tree_all, xyz[chunk], r=3.0)
        for row, (pt, nb) in enumerate(zip(chunk, nlist)):
            nb = np.asarray(nb, dtype=np.int64)
            if len(nb) < 8:
                continue
            nb_pred       = predictions[nb]
            b_ratio       = (nb_pred == BUILDING).sum() / len(nb)
            veg_mask      = np.isin(nb_pred, [LOWVEG, MIDVEG, HIGHVEG])
            veg_ratio     = veg_mask.sum() / len(nb)
            highveg_ratio = (nb_pred == HIGHVEG).sum() / len(nb)
            roof_like = (hag[pt] > 1.8 and planarity_05_raw[pt] > 0.48 and b_ratio > 0.30)
            wall_like = (hag[pt] > 0.25 and verticality_05_raw[pt] > 0.32 and
                         planarity_05_raw[pt] > 0.05 and b_ratio > 0.25 and highveg_ratio < 0.18)
            dense_building_cluster = b_ratio > 0.58
            if roof_like or wall_like or dense_building_cluster:
                continue
            should_remove = ((highveg_ratio > 0.35 and veg_ratio > 0.70 and b_ratio < 0.30) or
                             (highveg_ratio > 0.50 and b_ratio < 0.40))
            if not should_remove:
                continue
            veg_classes = nb_pred[veg_mask]
            if len(veg_classes) == 0:
                continue
            vals, cnts = np.unique(veg_classes, return_counts=True)
            new_pred   = vals[cnts.argmax()]
            if highveg_ratio > 0.35:
                new_pred = HIGHVEG
            predictions[pt] = new_pred
            fixes += 1
        if n_chunks > 3 and ((ci + 1) % LOG_EVERY_CHUNKS == 0 or (ci + 1) == n_chunks):
            log.info(f"      Final safe veg cleanup: chunk {ci+1}/{n_chunks} | fixes: {fixes:,}")
    return predictions, fixes


# =====================================================================
# POST-PROCESSING STEP 8
# =====================================================================

def final_remove_buildings_inside_vegetation_strict(xyz, predictions, confidence,
                                                     hag, verticality_05_raw,
                                                     planarity_05_raw, log,
                                                     tree_all=None):
    # Disabled: final vegetation cleanup was removing real building wall/facade points.
    fixes = 0
    return predictions, fixes

    if tree_all is None:
        tree_all = cKDTree(xyz)
    building_idx = np.where(predictions == BUILDING)[0]
    if len(building_idx) == 0:
        return predictions, fixes
    if FAST_SAFE_FILTERS:
        b_conf = confidence[building_idx]
        b_hag  = hag[building_idx]
        b_vert = verticality_05_raw[building_idx]
        b_plan = planarity_05_raw[building_idx]

        obvious_roof = (
            (b_hag > 1.8) &
            (b_plan > 0.58) &
            (b_conf > 0.90)
        )

        obvious_wall = (
            (b_hag > 0.45) &
            (b_vert > 0.50) &
            (b_plan > 0.08) &
            (b_conf > 0.90)
        )

        keep_check = ~(obvious_roof | obvious_wall)
        building_idx_all = building_idx
        building_idx = building_idx[keep_check]

        log.info(
            f"      Step 8 FAST candidates: "
            f"{len(building_idx):,} / {len(building_idx_all):,}"
        )
    k_support = min(48, len(xyz))
    n_chunks  = (len(building_idx) + CHUNK_SIZE - 1) // CHUNK_SIZE
    for ci, i in enumerate(range(0, len(building_idx), CHUNK_SIZE)):
        chunk = building_idx[i:i + CHUNK_SIZE]
        if len(chunk) == 0:
            continue
        nlist    = safe_query_ball_point(tree_all, xyz[chunk], r=3.5)
        _, nn_ids = safe_query(tree_all, xyz[chunk], k=k_support)
        if k_support == 1:
            nn_ids = nn_ids.reshape(-1, 1)
        for row, (pt, nb) in enumerate(zip(chunk, nlist)):
            nb = np.asarray(nb, dtype=np.int64)
            if len(nb) < 12:
                continue
            nb_pred   = predictions[nb]
            b_ratio   = (nb_pred == BUILDING).sum() / len(nb)
            low_ratio = (nb_pred == LOWVEG).sum()   / len(nb)
            mid_ratio = (nb_pred == MIDVEG).sum()   / len(nb)
            high_ratio= (nb_pred == HIGHVEG).sum()  / len(nb)
            veg_ratio = low_ratio + mid_ratio + high_ratio
            support_ids     = np.asarray(nn_ids[row], dtype=np.int64)
            support_pred    = predictions[support_ids]
            support_b_ratio = (support_pred == BUILDING).sum() / len(support_pred)
            roof_like = (hag[pt] > 1.8 and planarity_05_raw[pt] > 0.58 and
                         (b_ratio > 0.42 or support_b_ratio > 0.50) and high_ratio < 0.22)
            wall_like = (hag[pt] > 0.45 and verticality_05_raw[pt] > 0.50 and
                         planarity_05_raw[pt] > 0.08 and
                         (b_ratio > 0.42 or support_b_ratio > 0.52) and
                         high_ratio < 0.14 and veg_ratio < 0.55)
            strong_building_cluster = ((b_ratio > 0.72 and veg_ratio < 0.40) or
                                       (support_b_ratio > 0.65 and confidence[pt] > 0.80))
            if roof_like or wall_like or strong_building_cluster:
                continue
            remove_as_vegetation = (
                (high_ratio > 0.28 and veg_ratio > 0.55 and b_ratio < 0.58) or
                (veg_ratio > 0.68 and b_ratio < 0.55) or
                (confidence[pt] < 0.72 and veg_ratio > 0.42 and b_ratio < 0.62) or
                (confidence[pt] < 0.85 and high_ratio > 0.22 and b_ratio < 0.45)
            )
            if not remove_as_vegetation:
                continue
            if high_ratio >= mid_ratio and high_ratio >= low_ratio:
                new_pred = HIGHVEG
            elif mid_ratio >= low_ratio:
                new_pred = MIDVEG
            else:
                new_pred = LOWVEG
            predictions[pt] = new_pred
            fixes += 1
        if n_chunks > 3 and ((ci + 1) % LOG_EVERY_CHUNKS == 0 or (ci + 1) == n_chunks):
            log.info(f"      Final veg-bldg STRICT: chunk {ci+1}/{n_chunks} | fixes: {fixes:,}")
    return predictions, fixes

# =====================================================================
# NUMBA ACCELERATION FOR POST-PROCESSING STEP 4, STEP 7, STEP 8
# Same rules, same thresholds, same radius queries.
# If Numba is missing/fails, original Python functions are used.
# =====================================================================

_step3_python_original = fix_salt_pepper
_step4_python_original = clean_false_buildings_in_vegetation
_step7_python_original = clean_final_building_speckles_only
_step8_python_original = final_remove_buildings_inside_vegetation_strict


def _flatten_neighbor_lists(nlist):
    """
    Convert cKDTree query_ball_point ragged neighbor lists into flat arrays.

    For row i:
        neighbors = flat_nb[offsets[i]:offsets[i + 1]]

    This keeps exact radius-neighbor behavior.
    """
    n = len(nlist)
    lengths = np.empty(n, dtype=np.int64)

    total = 0
    for i, nb in enumerate(nlist):
        ln = len(nb)
        lengths[i] = ln
        total += ln

    offsets = np.empty(n + 1, dtype=np.int64)
    offsets[0] = 0
    np.cumsum(lengths, out=offsets[1:])

    flat_nb = np.empty(total, dtype=np.int64)

    pos = 0
    for nb in nlist:
        arr = np.asarray(nb, dtype=np.int64)
        ln = len(arr)
        if ln > 0:
            flat_nb[pos:pos + ln] = arr
            pos += ln

    return flat_nb, offsets


if HAS_NUMBA:
    @njit(cache=False)
    def _step3_building_numba_apply(
            chunk,
            flat_nb,
            offsets,
            nn_ids,
            predictions,
            confidence):
        fixes = 0
        k_nn = nn_ids.shape[1]

        for row in range(chunk.shape[0]):
            pt = chunk[row]

            s = offsets[row]
            e = offsets[row + 1]
            nb_len = e - s

            if nb_len < 6:
                continue

            b_count = 0
            low_count = 0
            mid_count = 0
            high_count = 0

            for p in range(s, e):
                cls = predictions[flat_nb[p]]
                if cls == 4:
                    b_count += 1
                elif cls == 1:
                    low_count += 1
                elif cls == 2:
                    mid_count += 1
                elif cls == 3:
                    high_count += 1

            veg_count = low_count + mid_count + high_count
            b_ratio = b_count / nb_len
            veg_ratio = veg_count / nb_len
            highveg_ratio = high_count / nb_len

            should_remove = (
                nb_len < 10 or
                (b_ratio < 0.50 and b_count < 5) or
                (confidence[pt] < 0.85 and b_ratio < 0.65 and b_count < 7) or
                (highveg_ratio > 0.15 and b_count < 18 and b_ratio < 0.82) or
                (veg_ratio > 0.65 and b_count < 20 and confidence[pt] < 0.97) or
                (veg_ratio > 0.82 and b_count < 24)
            )
            if not should_remove:
                continue

            # Same fallback behavior as original:
            # majority of non-building in kNN[1:]
            other_low = 0
            other_mid = 0
            other_high = 0
            other_ground = 0

            for k in range(1, k_nn):
                cls = predictions[nn_ids[row, k]]
                if cls == 4:
                    continue
                if cls == 0:
                    other_ground += 1
                elif cls == 1:
                    other_low += 1
                elif cls == 2:
                    other_mid += 1
                elif cls == 3:
                    other_high += 1

            total_other = other_ground + other_low + other_mid + other_high
            if total_other == 0:
                predictions[pt] = 0
                fixes += 1
                continue

            new_pred = 0
            best = other_ground
            if other_low > best:
                new_pred = 1
                best = other_low
            if other_mid > best:
                new_pred = 2
                best = other_mid
            if other_high > best:
                new_pred = 3

            predictions[pt] = new_pred
            fixes += 1

        return fixes

    @njit(cache=False)
    def _step3_veg_numba_apply(
            chunk,
            nn_ids,
            predictions,
            confidence):
        fixes = 0
        k_nn = nn_ids.shape[1]

        for row in range(chunk.shape[0]):
            idx = chunk[row]

            high_low = 0
            high_mid = 0
            high_high = 0
            high_ground = 0
            high_building = 0

            for k in range(1, k_nn):
                nid = nn_ids[row, k]
                if confidence[nid] <= 0.8:
                    continue
                cls = predictions[nid]
                if cls == 0:
                    high_ground += 1
                elif cls == 1:
                    high_low += 1
                elif cls == 2:
                    high_mid += 1
                elif cls == 3:
                    high_high += 1
                elif cls == 4:
                    high_building += 1

            hc = high_low + high_mid + high_high + high_ground + high_building
            if hc < 5:
                continue

            new_pred = 0
            best = high_ground
            if high_low > best:
                new_pred = 1
                best = high_low
            if high_mid > best:
                new_pred = 2
                best = high_mid
            if high_high > best:
                new_pred = 3
                best = high_high
            if high_building > best:
                new_pred = 4

            if new_pred != 4 and new_pred != predictions[idx]:
                predictions[idx] = new_pred
                fixes += 1

        return fixes

    @njit(cache=False)
    def _step4_numba_apply(
            chunk,
            flat_nb,
            offsets,
            predictions,
            hag,
            verticality_05_raw,
            planarity_05_raw):
        fixes = 0

        for row in range(chunk.shape[0]):
            pt = chunk[row]

            s = offsets[row]
            e = offsets[row + 1]
            nb_len = e - s

            if nb_len < 6:
                continue

            b_count = 0
            low_count = 0
            mid_count = 0
            high_count = 0

            for p in range(s, e):
                cls = predictions[flat_nb[p]]

                if cls == 4:
                    b_count += 1
                elif cls == 1:
                    low_count += 1
                elif cls == 2:
                    mid_count += 1
                elif cls == 3:
                    high_count += 1

            veg_count = low_count + mid_count + high_count

            b_ratio = b_count / nb_len
            veg_ratio = veg_count / nb_len
            highveg_ratio = high_count / nb_len

            pt_hag = hag[pt]
            pt_vert = verticality_05_raw[pt]
            pt_plan = planarity_05_raw[pt]

            roof_like = (
                pt_plan > 0.58 and
                pt_hag > 2.5 and
                b_ratio > 0.35
            )

            wall_like = (
                (pt_vert > 0.82 and pt_hag > 2.2 and b_ratio > 0.35) or
                (pt_vert > 0.68 and pt_hag > 4.0 and b_ratio > 0.40)
            )

            strong_building_cluster = (
                b_ratio > 0.82 and
                pt_hag > 2.0 and
                veg_ratio < 0.45
            )

            if roof_like or wall_like or strong_building_cluster:
                continue

            should_reclass = (
                (highveg_ratio > 0.22 and b_ratio < 0.55) or
                (veg_ratio > 0.55 and b_ratio < 0.60) or
                (veg_ratio > 0.70)
            )

            if not should_reclass:
                continue

            if veg_count == 0:
                continue

            # Same behavior as np.unique(...).argmax() for classes 1,2,3:
            # LowVeg wins ties before MidVeg, MidVeg before HighVeg.
            new_pred = 1
            best_count = low_count

            if mid_count > best_count:
                new_pred = 2
                best_count = mid_count

            if high_count > best_count:
                new_pred = 3

            if highveg_ratio > 0.30:
                new_pred = 3

            predictions[pt] = new_pred
            fixes += 1

        return fixes


    @njit(cache=False)
    def _step7_numba_apply(
            chunk,
            flat_nb,
            offsets,
            predictions,
            hag,
            verticality_05_raw,
            planarity_05_raw):
        fixes = 0

        for row in range(chunk.shape[0]):
            pt = chunk[row]

            s = offsets[row]
            e = offsets[row + 1]
            nb_len = e - s

            if nb_len < 8:
                continue

            b_count = 0
            low_count = 0
            mid_count = 0
            high_count = 0

            for p in range(s, e):
                cls = predictions[flat_nb[p]]

                if cls == 4:
                    b_count += 1
                elif cls == 1:
                    low_count += 1
                elif cls == 2:
                    mid_count += 1
                elif cls == 3:
                    high_count += 1

            veg_count = low_count + mid_count + high_count

            b_ratio = b_count / nb_len
            veg_ratio = veg_count / nb_len
            highveg_ratio = high_count / nb_len

            pt_hag = hag[pt]
            pt_vert = verticality_05_raw[pt]
            pt_plan = planarity_05_raw[pt]

            roof_like = (
                pt_hag > 1.8 and
                pt_plan > 0.48 and
                b_ratio > 0.30
            )

            wall_like = (
                pt_hag > 0.25 and
                pt_vert > 0.32 and
                pt_plan > 0.05 and
                b_ratio > 0.25 and
                highveg_ratio < 0.18
            )

            dense_building_cluster = b_ratio > 0.58

            if roof_like or wall_like or dense_building_cluster:
                continue

            should_remove = (
                (highveg_ratio > 0.35 and veg_ratio > 0.70 and b_ratio < 0.30) or
                (highveg_ratio > 0.50 and b_ratio < 0.40)
            )

            if not should_remove:
                continue

            if veg_count == 0:
                continue

            # Same np.unique tie behavior: LowVeg, then MidVeg, then HighVeg.
            new_pred = 1
            best_count = low_count

            if mid_count > best_count:
                new_pred = 2
                best_count = mid_count

            if high_count > best_count:
                new_pred = 3

            if highveg_ratio > 0.35:
                new_pred = 3

            predictions[pt] = new_pred
            fixes += 1

        return fixes


    @njit(cache=False)
    def _step8_numba_apply(
            chunk,
            flat_nb,
            offsets,
            support_ids,
            predictions,
            confidence,
            hag,
            verticality_05_raw,
            planarity_05_raw):
        fixes = 0

        support_k = support_ids.shape[1]

        for row in range(chunk.shape[0]):
            pt = chunk[row]

            s = offsets[row]
            e = offsets[row + 1]
            nb_len = e - s

            if nb_len < 12:
                continue

            b_count = 0
            low_count = 0
            mid_count = 0
            high_count = 0

            for p in range(s, e):
                cls = predictions[flat_nb[p]]

                if cls == 4:
                    b_count += 1
                elif cls == 1:
                    low_count += 1
                elif cls == 2:
                    mid_count += 1
                elif cls == 3:
                    high_count += 1

            b_ratio = b_count / nb_len
            low_ratio = low_count / nb_len
            mid_ratio = mid_count / nb_len
            high_ratio = high_count / nb_len
            veg_ratio = low_ratio + mid_ratio + high_ratio

            support_b_count = 0
            for k in range(support_k):
                sid = support_ids[row, k]
                if predictions[sid] == 4:
                    support_b_count += 1

            support_b_ratio = support_b_count / support_k

            pt_hag = hag[pt]
            pt_vert = verticality_05_raw[pt]
            pt_plan = planarity_05_raw[pt]
            pt_conf = confidence[pt]

            roof_like = (
                pt_hag > 1.8 and
                pt_plan > 0.58 and
                (b_ratio > 0.42 or support_b_ratio > 0.50) and
                high_ratio < 0.22
            )

            wall_like = (
                pt_hag > 0.45 and
                pt_vert > 0.50 and
                pt_plan > 0.08 and
                (b_ratio > 0.42 or support_b_ratio > 0.52) and
                high_ratio < 0.14 and
                veg_ratio < 0.55
            )

            strong_building_cluster = (
                (b_ratio > 0.72 and veg_ratio < 0.40) or
                (support_b_ratio > 0.65 and pt_conf > 0.80)
            )

            if roof_like or wall_like or strong_building_cluster:
                continue

            remove_as_vegetation = (
                (high_ratio > 0.28 and veg_ratio > 0.55 and b_ratio < 0.58) or
                (veg_ratio > 0.68 and b_ratio < 0.55) or
                (pt_conf < 0.72 and veg_ratio > 0.42 and b_ratio < 0.62) or
                (pt_conf < 0.85 and high_ratio > 0.22 and b_ratio < 0.45)
            )

            if not remove_as_vegetation:
                continue

            # Same Step 8 tie rule:
            # HighVeg wins tie first, then MidVeg, then LowVeg.
            if high_ratio >= mid_ratio and high_ratio >= low_ratio:
                new_pred = 3
            elif mid_ratio >= low_ratio:
                new_pred = 2
            else:
                new_pred = 1

            predictions[pt] = new_pred
            fixes += 1

        return fixes

else:
    def _step3_building_numba_apply(*args, **kwargs):
        raise RuntimeError("Numba is not installed")

    def _step3_veg_numba_apply(*args, **kwargs):
        raise RuntimeError("Numba is not installed")

    def _step4_numba_apply(*args, **kwargs):
        raise RuntimeError("Numba is not installed")

    def _step7_numba_apply(*args, **kwargs):
        raise RuntimeError("Numba is not installed")

    def _step8_numba_apply(*args, **kwargs):
        raise RuntimeError("Numba is not installed")


def fix_salt_pepper(
        xyz,
        predictions,
        confidence,
        hag,
        verticality_05_raw,
        planarity_05_raw,
        log,
        tree_all=None):
    """
    Step 3 Numba wrapper (accuracy-preserving).
    Uses the exact same decision rules/thresholds as the original Python Step 3.
    Falls back to original implementation when Numba is unavailable.
    """
    if not HAS_NUMBA:
        log.warning("      Step 3 NUMBA disabled - using original Python Step 3")
        return _step3_python_original(
            xyz,
            predictions,
            confidence,
            hag,
            verticality_05_raw,
            planarity_05_raw,
            log,
            tree_all=tree_all
        )

    fixes = 0

    if tree_all is None:
        tree_all = cKDTree(xyz)

    predictions = np.ascontiguousarray(predictions)
    confidence = np.ascontiguousarray(confidence)

    building_indices_all = np.where(predictions == BUILDING)[0]

    if len(building_indices_all) > 0 and FAST_STEP3_FILTER:
        b_conf = confidence[building_indices_all]
        b_hag = hag[building_indices_all]
        b_vert = verticality_05_raw[building_indices_all]
        b_plan = planarity_05_raw[building_indices_all]

        obvious_roof = (
            (b_hag > 1.8) &
            (b_plan > 0.55) &
            (b_conf > 0.90)
        )

        obvious_wall = (
            (b_hag > 0.45) &
            (b_vert > 0.45) &
            (b_plan > 0.05) &
            (b_conf > 0.90)
        )

        suspicious = (
            (b_conf < 0.96) |
            ((b_hag < 1.4) & (~obvious_wall)) |
            (~(obvious_roof | obvious_wall))
        )

        building_indices = building_indices_all[suspicious]

        if len(building_indices) > MAX_STEP3_BUILDING_CANDIDATES:
            order = np.argsort(confidence[building_indices])
            building_indices = building_indices[order[:MAX_STEP3_BUILDING_CANDIDATES]]

        log.info(
            f"      Step 3 FAST candidates: "
            f"{len(building_indices):,} / {len(building_indices_all):,}"
        )
    else:
        building_indices = building_indices_all

    if len(building_indices) > 0:
        n_chunks = (len(building_indices) + CHUNK_SIZE - 1) // CHUNK_SIZE
        log.info(
            f"      Step 3 NUMBA enabled: "
            f"{len(building_indices):,} building candidates | chunks={n_chunks}"
        )

        k_build = min(32, len(xyz))
        for ci, i in enumerate(range(0, len(building_indices), CHUNK_SIZE)):
            chunk = building_indices[i:i + CHUNK_SIZE]
            if len(chunk) == 0:
                continue

            nlist = safe_query_ball_point(tree_all, xyz[chunk], r=2.2)
            flat_nb, offsets = _flatten_neighbor_lists(nlist)
            if flat_nb.size == 0:
                del nlist, flat_nb, offsets
                continue

            _, nn_ids = safe_query(tree_all, xyz[chunk], k=k_build)
            if k_build == 1:
                nn_ids = nn_ids.reshape(-1, 1)
            nn_ids = np.ascontiguousarray(np.asarray(nn_ids, dtype=np.int64))

            chunk_arr = np.ascontiguousarray(chunk, dtype=np.int64)
            fixes_this = _step3_building_numba_apply(
                chunk_arr,
                flat_nb,
                offsets,
                nn_ids,
                predictions,
                confidence
            )
            fixes += int(fixes_this)

            if n_chunks > 3 and ((ci + 1) % LOG_EVERY_CHUNKS == 0 or (ci + 1) == n_chunks):
                log.info(f"      Salt-pepper remove NUMBA: chunk {ci+1}/{n_chunks} | fixes: {fixes:,}")

            del nlist, flat_nb, offsets, nn_ids, chunk_arr

    low_conf = confidence < 0.55
    candidates = np.where(low_conf & np.isin(predictions, [LOWVEG, MIDVEG, HIGHVEG]))[0]
    if len(candidates) > MAX_SALT_CANDIDATES:
        candidates = candidates[np.random.choice(len(candidates), MAX_SALT_CANDIDATES, replace=False)]

    n_chunks = (len(candidates) + CHUNK_SIZE - 1) // CHUNK_SIZE
    k_veg = min(15, len(xyz))
    for ci, i in enumerate(range(0, len(candidates), CHUNK_SIZE)):
        chunk = candidates[i:i + CHUNK_SIZE]
        if len(chunk) == 0:
            continue

        _, nn_ids = safe_query(tree_all, xyz[chunk], k=k_veg)
        if k_veg == 1:
            nn_ids = nn_ids.reshape(-1, 1)
        nn_ids = np.ascontiguousarray(np.asarray(nn_ids, dtype=np.int64))
        chunk_arr = np.ascontiguousarray(chunk, dtype=np.int64)

        fixes_this = _step3_veg_numba_apply(
            chunk_arr,
            nn_ids,
            predictions,
            confidence
        )
        fixes += int(fixes_this)

        if n_chunks > 3 and ((ci + 1) % LOG_EVERY_CHUNKS == 0 or (ci + 1) == n_chunks):
            log.info(f"      Salt-pepper veg NUMBA: chunk {ci+1}/{n_chunks} | fixes: {fixes:,}")

        del nn_ids, chunk_arr

    return predictions, fixes


def clean_false_buildings_in_vegetation(
        xyz,
        predictions,
        hag,
        verticality_05_raw,
        planarity_05_raw,
        log,
        tree_all=None):
    """
    Step 4 disabled for building-preservation.
    The previous cleanup removed valid building walls/facades as vegetation.
    """
    log.info("      Step 4 disabled: preserving building walls/facades")
    return predictions, 0

    # Original Step 4 kept below unreachable for reference.
    if not HAS_NUMBA:
        log.warning("      Step 4 NUMBA disabled - using original Python Step 4")
        return _step4_python_original(
            xyz,
            predictions,
            hag,
            verticality_05_raw,
            planarity_05_raw,
            log,
            tree_all=tree_all
        )

    fixes = 0

    if tree_all is None:
        tree_all = cKDTree(xyz)

    predictions = np.ascontiguousarray(predictions)
    hag = np.ascontiguousarray(hag)
    verticality_05_raw = np.ascontiguousarray(verticality_05_raw)
    planarity_05_raw = np.ascontiguousarray(planarity_05_raw)

    building_idx = np.where(predictions == BUILDING)[0]

    if len(building_idx) == 0:
        return predictions, fixes

    if FAST_SAFE_FILTERS:
        b_hag = hag[building_idx]
        b_vert = verticality_05_raw[building_idx]
        b_plan = planarity_05_raw[building_idx]

        obvious_roof = (b_hag > 1.8) & (b_plan > 0.58)
        obvious_wall = (b_hag > 0.45) & (b_vert > 0.55) & (b_plan > 0.08)

        keep_check = ~(obvious_roof | obvious_wall)
        building_idx_all = building_idx
        building_idx = building_idx[keep_check]

        log.info(
            f"      Step 4 FAST candidates: "
            f"{len(building_idx):,} / {len(building_idx_all):,}"
        )

    n_chunks = (len(building_idx) + CHUNK_SIZE - 1) // CHUNK_SIZE

    log.info(
        f"      Step 4 NUMBA enabled: "
        f"{len(building_idx):,} building candidates | chunks={n_chunks}"
    )

    for ci, i in enumerate(range(0, len(building_idx), CHUNK_SIZE)):
        chunk = building_idx[i:i + CHUNK_SIZE]

        if len(chunk) == 0:
            continue

        nlist = safe_query_ball_point(tree_all, xyz[chunk], r=4.0)
        flat_nb, offsets = _flatten_neighbor_lists(nlist)

        if flat_nb.size == 0:
            del nlist, flat_nb, offsets
            continue

        chunk_arr = np.ascontiguousarray(chunk, dtype=np.int64)

        fixes_this = _step4_numba_apply(
            chunk_arr,
            flat_nb,
            offsets,
            predictions,
            hag,
            verticality_05_raw,
            planarity_05_raw
        )

        fixes += int(fixes_this)

        if n_chunks > 3 and ((ci + 1) % LOG_EVERY_CHUNKS == 0 or (ci + 1) == n_chunks):
            log.info(
                f"      Veg-building cleanup NUMBA: "
                f"chunk {ci + 1}/{n_chunks} | fixes: {fixes:,}"
            )

        del nlist, flat_nb, offsets, chunk_arr

    return predictions, fixes


def clean_final_building_speckles_only(
        xyz,
        predictions,
        hag,
        verticality_05_raw,
        planarity_05_raw,
        log,
        tree_all=None):
    """
    Step 7 Numba wrapper.
    Same strict logic as original Step 7.
    """
    if not HAS_NUMBA:
        log.warning("      Step 7 NUMBA disabled - using original Python Step 7")
        return _step7_python_original(
            xyz,
            predictions,
            hag,
            verticality_05_raw,
            planarity_05_raw,
            log,
            tree_all=tree_all
        )

    fixes = 0

    if tree_all is None:
        tree_all = cKDTree(xyz)

    predictions = np.ascontiguousarray(predictions)
    hag = np.ascontiguousarray(hag)
    verticality_05_raw = np.ascontiguousarray(verticality_05_raw)
    planarity_05_raw = np.ascontiguousarray(planarity_05_raw)

    building_idx = np.where(predictions == BUILDING)[0]

    if len(building_idx) == 0:
        return predictions, fixes

    if FAST_SAFE_FILTERS:
        b_hag = hag[building_idx]
        b_vert = verticality_05_raw[building_idx]
        b_plan = planarity_05_raw[building_idx]

        obvious_roof = (b_hag > 1.8) & (b_plan > 0.48)
        obvious_wall = (b_hag > 0.25) & (b_vert > 0.45) & (b_plan > 0.05)

        keep_check = ~(obvious_roof | obvious_wall)
        building_idx_all = building_idx
        building_idx = building_idx[keep_check]

        log.info(
            f"      Step 7 FAST candidates: "
            f"{len(building_idx):,} / {len(building_idx_all):,}"
        )

    n_chunks = (len(building_idx) + CHUNK_SIZE - 1) // CHUNK_SIZE

    log.info(
        f"      Step 7 NUMBA enabled: "
        f"{len(building_idx):,} building candidates | chunks={n_chunks}"
    )

    for ci, i in enumerate(range(0, len(building_idx), CHUNK_SIZE)):
        chunk = building_idx[i:i + CHUNK_SIZE]

        if len(chunk) == 0:
            continue

        nlist = safe_query_ball_point(tree_all, xyz[chunk], r=3.0)
        flat_nb, offsets = _flatten_neighbor_lists(nlist)

        if flat_nb.size == 0:
            del nlist, flat_nb, offsets
            continue

        chunk_arr = np.ascontiguousarray(chunk, dtype=np.int64)

        fixes_this = _step7_numba_apply(
            chunk_arr,
            flat_nb,
            offsets,
            predictions,
            hag,
            verticality_05_raw,
            planarity_05_raw
        )

        fixes += int(fixes_this)

        if n_chunks > 3 and ((ci + 1) % LOG_EVERY_CHUNKS == 0 or (ci + 1) == n_chunks):
            log.info(
                f"      Final safe veg cleanup NUMBA: "
                f"chunk {ci + 1}/{n_chunks} | fixes: {fixes:,}"
            )

        del nlist, flat_nb, offsets, chunk_arr

    return predictions, fixes


def final_remove_buildings_inside_vegetation_strict(
        xyz,
        predictions,
        confidence,
        hag,
        verticality_05_raw,
        planarity_05_raw,
        log,
        tree_all=None):
    """
    Step 8 disabled for building-preservation.
    The previous final cleanup removed valid building wall/facade points.
    """
    log.info("      Step 8 disabled: preserving building walls/facades")
    return predictions, 0

    # Original Step 8 kept below unreachable for reference.
    if not HAS_NUMBA:
        log.warning("      Step 8 NUMBA disabled - using original Python Step 8")
        return _step8_python_original(
            xyz,
            predictions,
            confidence,
            hag,
            verticality_05_raw,
            planarity_05_raw,
            log,
            tree_all=tree_all
        )

    fixes = 0

    if tree_all is None:
        tree_all = cKDTree(xyz)

    predictions = np.ascontiguousarray(predictions)
    confidence = np.ascontiguousarray(confidence)
    hag = np.ascontiguousarray(hag)
    verticality_05_raw = np.ascontiguousarray(verticality_05_raw)
    planarity_05_raw = np.ascontiguousarray(planarity_05_raw)

    building_idx = np.where(predictions == BUILDING)[0]

    if len(building_idx) == 0:
        return predictions, fixes

    if FAST_SAFE_FILTERS:
        b_conf = confidence[building_idx]
        b_hag = hag[building_idx]
        b_vert = verticality_05_raw[building_idx]
        b_plan = planarity_05_raw[building_idx]

        obvious_roof = (
            (b_hag > 1.8) &
            (b_plan > 0.58) &
            (b_conf > 0.90)
        )

        obvious_wall = (
            (b_hag > 0.45) &
            (b_vert > 0.50) &
            (b_plan > 0.08) &
            (b_conf > 0.90)
        )

        keep_check = ~(obvious_roof | obvious_wall)
        building_idx_all = building_idx
        building_idx = building_idx[keep_check]

        log.info(
            f"      Step 8 FAST candidates: "
            f"{len(building_idx):,} / {len(building_idx_all):,}"
        )

    k_support = min(48, len(xyz))
    n_chunks = (len(building_idx) + CHUNK_SIZE - 1) // CHUNK_SIZE

    log.info(
        f"      Step 8 NUMBA enabled: "
        f"{len(building_idx):,} building candidates | chunks={n_chunks}"
    )

    for ci, i in enumerate(range(0, len(building_idx), CHUNK_SIZE)):
        chunk = building_idx[i:i + CHUNK_SIZE]

        if len(chunk) == 0:
            continue

        nlist = safe_query_ball_point(tree_all, xyz[chunk], r=3.5)
        _, nn_ids = safe_query(tree_all, xyz[chunk], k=k_support)

        if k_support == 1:
            nn_ids = nn_ids.reshape(-1, 1)

        flat_nb, offsets = _flatten_neighbor_lists(nlist)

        if flat_nb.size == 0:
            del nlist, flat_nb, offsets, nn_ids
            continue

        chunk_arr = np.ascontiguousarray(chunk, dtype=np.int64)
        support_ids = np.ascontiguousarray(nn_ids, dtype=np.int64)

        fixes_this = _step8_numba_apply(
            chunk_arr,
            flat_nb,
            offsets,
            support_ids,
            predictions,
            confidence,
            hag,
            verticality_05_raw,
            planarity_05_raw
        )

        fixes += int(fixes_this)

        if n_chunks > 3 and ((ci + 1) % LOG_EVERY_CHUNKS == 0 or (ci + 1) == n_chunks):
            log.info(
                f"      Final veg-bldg STRICT NUMBA: "
                f"chunk {ci + 1}/{n_chunks} | fixes: {fixes:,}"
            )

        del nlist, flat_nb, offsets, nn_ids, chunk_arr, support_ids

    return predictions, fixes


def remove_detached_vegetation_building_noise(
    xyz,
    predictions,
    raw_predictions,
    hag,
    verticality_05_raw,
    planarity_05_raw,
    confidence,
    log,
    tree_all=None,
):
    """
    v8 STRICT vegetation/building cleanup.

    Purpose:
    - remove Building points sitting inside vegetation/canopy clusters
    - keep real roofs/walls only when geometry + local Building support are strong

    This is stricter than v7 because v7 removed only a small part of the pink
    vegetation noise. It still protects real structural roof/wall points.
    """
    fixes = 0

    if tree_all is None:
        tree_all = cKDTree(xyz)

    building_idx = np.where(predictions == BUILDING)[0]
    if len(building_idx) == 0:
        return predictions, fixes

    if raw_predictions is None or len(raw_predictions) != len(predictions):
        raw_predictions = predictions.copy()

    raw_predictions = np.asarray(raw_predictions)

    h = hag[building_idx]
    v = verticality_05_raw[building_idx]
    p = planarity_05_raw[building_idx]
    c = confidence[building_idx]

    raw_was_not_building = raw_predictions[building_idx] != BUILDING

    # Strong structural points are inspected only if raw was vegetation.
    strong_roof = (h > 1.30) & (p > 0.215) & (c > 0.68)
    strong_wall = (h > 0.45) & (v > 0.27) & (p > 0.070) & (c > 0.68)

    risky_mask = raw_was_not_building | (~strong_roof & ~strong_wall) | (c < 0.78)
    candidates = building_idx[risky_mask]

    if len(candidates) == 0:
        return predictions, fixes

    log.info(f"      Detached vegetation-building noise candidates: {len(candidates):,}")

    n_chunks = (len(candidates) + CHUNK_SIZE - 1) // CHUNK_SIZE

    for ci, i in enumerate(range(0, len(candidates), CHUNK_SIZE)):
        chunk = candidates[i:i + CHUNK_SIZE]
        nlist = safe_query_ball_point(tree_all, xyz[chunk], r=2.60)

        for pt, nb in zip(chunk, nlist):
            nb = np.asarray(nb, dtype=np.int64)
            if len(nb) < 10:
                continue

            nb_pred = predictions[nb]
            nb_raw = raw_predictions[nb]

            b_ratio = (nb_pred == BUILDING).sum() / len(nb)
            low_ratio = (nb_pred == LOWVEG).sum() / len(nb)
            mid_ratio = (nb_pred == MIDVEG).sum() / len(nb)
            high_ratio = (nb_pred == HIGHVEG).sum() / len(nb)
            veg_ratio = low_ratio + mid_ratio + high_ratio

            raw_b_ratio = (nb_raw == BUILDING).sum() / len(nb)
            raw_low_ratio = (nb_raw == LOWVEG).sum() / len(nb)
            raw_mid_ratio = (nb_raw == MIDVEG).sum() / len(nb)
            raw_high_ratio = (nb_raw == HIGHVEG).sum() / len(nb)
            raw_veg_ratio = raw_low_ratio + raw_mid_ratio + raw_high_ratio

            pt_h = hag[pt]
            pt_v = verticality_05_raw[pt]
            pt_p = planarity_05_raw[pt]
            pt_c = confidence[pt]
            raw_cls = raw_predictions[pt]

            # Keep real building only when it is both structural and supported.
            roof_like = (
                pt_h > 1.25 and
                pt_p > 0.220 and
                b_ratio >= 0.42 and
                high_ratio < 0.28 and
                raw_high_ratio < 0.42
            )

            wall_like = (
                pt_h > 0.45 and
                pt_v > 0.30 and
                pt_p > 0.080 and
                b_ratio >= 0.40 and
                high_ratio < 0.24 and
                raw_high_ratio < 0.38
            )

            dense_real_building = (
                b_ratio >= 0.78 and
                raw_b_ratio >= 0.08 and
                (pt_p > 0.145 or (pt_v > 0.28 and pt_p > 0.065)) and
                raw_veg_ratio < 0.68
            )

            if roof_like or wall_like or dense_real_building:
                continue

            weak_geometry = (
                pt_p < 0.160 or
                (pt_v < 0.24 and pt_p < 0.200) or
                pt_h < 0.45
            )

            remove_as_veg = (
                # Raw model and raw neighborhood say vegetation, current became Building.
                (raw_cls != BUILDING and raw_veg_ratio > 0.52 and raw_b_ratio < 0.14 and weak_geometry) or
                # Current neighborhood is still vegetation-heavy.
                (raw_cls != BUILDING and veg_ratio > 0.36 and b_ratio < 0.76 and pt_c < 0.96) or
                # High vegetation/canopy around the point.
                (raw_high_ratio > 0.34 and high_ratio > 0.12 and b_ratio < 0.86 and pt_p < 0.220) or
                # Detached recovered blob with poor raw Building support.
                (raw_cls != BUILDING and raw_b_ratio < 0.06 and raw_veg_ratio > 0.62 and b_ratio < 0.88) or
                # Low/mid vegetation patch converted by region-grow/hole-fill.
                (raw_cls in (LOWVEG, MIDVEG, HIGHVEG) and veg_ratio > 0.48 and b_ratio < 0.70)
            )

            if not remove_as_veg:
                continue

            # Prefer original raw vegetation class when it is vegetation.
            if raw_cls in (LOWVEG, MIDVEG, HIGHVEG):
                predictions[pt] = raw_cls
            elif raw_high_ratio >= raw_mid_ratio and raw_high_ratio >= raw_low_ratio:
                predictions[pt] = HIGHVEG
            elif raw_mid_ratio >= raw_low_ratio:
                predictions[pt] = MIDVEG
            else:
                predictions[pt] = LOWVEG

            fixes += 1

        if n_chunks > 3 and ((ci + 1) % LOG_EVERY_CHUNKS == 0 or (ci + 1) == n_chunks):
            log.info(
                f"      Detached vegetation-building noise cleanup: "
                f"chunk {ci + 1}/{n_chunks} | fixes: {fixes:,}"
            )

    return predictions, fixes


# =====================================================================
# POST-PROCESSING STEP 6J - final canopy/island rollback
# =====================================================================


def strict_canopy_island_building_rollback(
    xyz,
    predictions,
    raw_predictions,
    hag,
    verticality_05_raw,
    planarity_05_raw,
    confidence,
    log,
    tree_all=None,
):
    """
    V11 final false-building cleanup.

    Important change:
    The old 6J checked only points where raw_predictions != BUILDING. That misses
    cases where the model itself predicted tree/canopy as Building. This version
    checks risky BUILDING points from both sources, while protecting structural
    roof/wall geometry.
    """
    fixes = 0

    if raw_predictions is None or len(raw_predictions) != len(predictions):
        raw_predictions = predictions.copy()
    else:
        raw_predictions = np.asarray(raw_predictions)

    if tree_all is None:
        tree_all = cKDTree(xyz)

    building_idx = np.where(predictions == BUILDING)[0]
    if len(building_idx) == 0:
        return predictions, fixes

    h = hag[building_idx]
    v = verticality_05_raw[building_idx]
    p = planarity_05_raw[building_idx]
    c = confidence[building_idx]
    raw = raw_predictions[building_idx]

    obvious_roof = (
        (h > 1.20) &
        (p > 0.120) &
        (v < 0.82) &
        (c > 0.70)
    )

    obvious_wall = (
        (h > 0.35) &
        (v > 0.30) &
        (p > 0.055) &
        (c > 0.65)
    )

    risky = (
        (raw != BUILDING) |
        (c < 0.90) |
        (~(obvious_roof | obvious_wall)) |
        (p < 0.080)
    )

    candidates = building_idx[risky]

    if len(candidates) == 0:
        return predictions, fixes

    if len(candidates) > 110_000:
        # Check weakest geometry/confidence first. Keeps runtime controlled.
        score = (
            confidence[candidates] +
            0.7 * planarity_05_raw[candidates] +
            0.3 * verticality_05_raw[candidates]
        )
        keep = np.argsort(score)[:110_000]
        candidates = candidates[keep]

    log.info(f"      V11 final false-building cleanup candidates: {len(candidates):,}")

    n_chunks = (len(candidates) + CHUNK_SIZE - 1) // CHUNK_SIZE

    for ci, i in enumerate(range(0, len(candidates), CHUNK_SIZE)):
        chunk = candidates[i:i + CHUNK_SIZE]
        nlist = safe_query_ball_point(tree_all, xyz[chunk], r=2.40)

        for pt, nb in zip(chunk, nlist):
            nb = np.asarray(nb, dtype=np.int64)
            if len(nb) < 10:
                continue

            nb_pred = predictions[nb]
            nb_raw = raw_predictions[nb]

            cur_b_ratio = (nb_pred == BUILDING).sum() / len(nb)
            cur_low_ratio = (nb_pred == LOWVEG).sum() / len(nb)
            cur_mid_ratio = (nb_pred == MIDVEG).sum() / len(nb)
            cur_high_ratio = (nb_pred == HIGHVEG).sum() / len(nb)
            cur_veg_ratio = cur_low_ratio + cur_mid_ratio + cur_high_ratio

            raw_b_ratio = (nb_raw == BUILDING).sum() / len(nb)
            raw_low_ratio = (nb_raw == LOWVEG).sum() / len(nb)
            raw_mid_ratio = (nb_raw == MIDVEG).sum() / len(nb)
            raw_high_ratio = (nb_raw == HIGHVEG).sum() / len(nb)
            raw_veg_ratio = raw_low_ratio + raw_mid_ratio + raw_high_ratio

            pt_h = hag[pt]
            pt_v = verticality_05_raw[pt]
            pt_p = planarity_05_raw[pt]
            pt_c = confidence[pt]
            raw_cls = raw_predictions[pt]

            keep_roof = (
                pt_h > 1.15 and
                pt_p > 0.095 and
                pt_v < 0.82 and
                cur_b_ratio > 0.20 and
                cur_high_ratio < 0.86
            )

            keep_wall = (
                pt_h > 0.35 and
                pt_v > 0.30 and
                pt_p > 0.055 and
                cur_b_ratio > 0.22 and
                cur_high_ratio < 0.58
            )

            keep_dense_building = (
                cur_b_ratio > 0.78 and
                cur_veg_ratio < 0.42 and
                (pt_p > 0.080 or pt_v > 0.32)
            )

            if keep_roof or keep_wall or keep_dense_building:
                continue

            remove_as_veg = (
                (raw_veg_ratio > 0.55 and raw_b_ratio < 0.18 and cur_b_ratio < 0.48 and pt_p < 0.115) or
                (cur_high_ratio > 0.42 and cur_b_ratio < 0.38 and pt_p < 0.130) or
                (raw_high_ratio > 0.50 and pt_p < 0.120 and pt_v < 0.36) or
                (pt_c < 0.72 and cur_veg_ratio > 0.48 and cur_b_ratio < 0.55) or
                (pt_p < 0.045 and pt_v < 0.22 and cur_veg_ratio > 0.30)
            )

            if not remove_as_veg:
                continue

            if raw_cls in (LOWVEG, MIDVEG, HIGHVEG):
                predictions[pt] = raw_cls
            elif raw_high_ratio >= raw_mid_ratio and raw_high_ratio >= raw_low_ratio:
                predictions[pt] = HIGHVEG
            elif raw_mid_ratio >= raw_low_ratio:
                predictions[pt] = MIDVEG
            else:
                predictions[pt] = LOWVEG

            fixes += 1

        if n_chunks > 3 and ((ci + 1) % LOG_EVERY_CHUNKS == 0 or (ci + 1) == n_chunks):
            log.info(
                f"      V11 final false-building cleanup: "
                f"chunk {ci + 1}/{n_chunks} | fixes: {fixes:,}"
            )

    return predictions, fixes

def _build_structural_building_guard(predictions, hag, verticality_05_raw, planarity_05_raw):
    """
    Protect real structural building points from later vegetation cleanup.

    v3 change:
    The guard is intentionally stricter than v2. v2 protected too many points
    created by rescue/region-grow, so false vegetation-to-building conversions
    could not be rolled back later.
    """

    building_mask = predictions == BUILDING

    roof_like = (
        building_mask &
        (hag > 1.10) &
        (planarity_05_raw > 0.34)
    )

    wall_like = (
        building_mask &
        (hag > 0.30) &
        (verticality_05_raw > 0.18) &
        (planarity_05_raw > 0.035)
    )

    strong_roof = (
        building_mask &
        (hag > 1.70) &
        (planarity_05_raw > 0.26)
    )

    protected = roof_like | wall_like | strong_roof

    return protected



# =====================================================================
# V14 LIGHT GEOMETRY CORRECTION
# =====================================================================

def _majority_veg_class(predictions: np.ndarray, ids: np.ndarray) -> int:
    """Return majority vegetation class among ids, defaulting to HIGHVEG."""
    if len(ids) == 0:
        return HIGHVEG

    vals = predictions[ids]
    low = int((vals == LOWVEG).sum())
    mid = int((vals == MIDVEG).sum())
    high = int((vals == HIGHVEG).sum())

    if high >= mid and high >= low:
        return HIGHVEG
    if mid >= low:
        return MIDVEG
    return LOWVEG


def _get_plan_vert_from_geom(geom_ds, geom_vox_map):
    planarity_05_raw = (
        geom_ds[geom_vox_map, 4]
        .astype(np.float32, copy=False)
        .copy()
    )
    verticality_05_raw = (
        geom_ds[geom_vox_map, 10]
        .astype(np.float32, copy=False)
        .copy()
    )
    return planarity_05_raw, verticality_05_raw


def _get_power_geom_from_geom(geom_ds, geom_vox_map):
    """Keep only the small geometry arrays required by the power post-pass.

    Feature order at each scale is:
    eigen1,eigen2,eigen3,linearity,planarity,sphericity,omnivariance,
    anisotropy,eigenentropy,surface_variation,verticality.
    """
    m = np.asarray(geom_vox_map, dtype=np.int64)
    linearity_05_raw = geom_ds[m, 3].astype(np.float32, copy=False).copy()
    planarity_05_raw = geom_ds[m, 4].astype(np.float32, copy=False).copy()
    verticality_05_raw = geom_ds[m, 10].astype(np.float32, copy=False).copy()

    # Second scale = 1.0 m, so add 11 columns.
    linearity_10_raw = geom_ds[m, 14].astype(np.float32, copy=False).copy()
    verticality_10_raw = geom_ds[m, 21].astype(np.float32, copy=False).copy()

    return (
        linearity_05_raw, planarity_05_raw, verticality_05_raw,
        linearity_10_raw, verticality_10_raw,
    )


def light_fix_ground_holes_inside_building_roof(
        xyz,
        predictions,
        hag,
        planarity_05_raw,
        log,
        tree_all=None):
    """
    Convert small Ground/LowVeg/MidVeg holes inside an existing building roof
    back to Building.

    Strong local Building support is mandatory, so normal ground beside a
    building does not become Building.
    """
    fixes = 0

    building_idx = np.where(predictions == BUILDING)[0]
    if len(building_idx) < 30:
        log.info("      V14 roof-hole fix skipped: not enough building points")
        return predictions, fixes

    if tree_all is None:
        tree_all = cKDTree(xyz)

    btree_2d = cKDTree(xyz[building_idx, :2])

    candidate_idx = np.where(
        np.isin(predictions, [GROUND, LOWVEG, MIDVEG]) &
        ((hag > 0.85) | (planarity_05_raw > 0.08))
    )[0]

    if len(candidate_idx) == 0:
        return predictions, fixes

    d2, _ = safe_query(btree_2d, xyz[candidate_idx, :2], k=1)
    near = d2 < 2.80
    candidate_idx = candidate_idx[near]
    d2 = d2[near]

    if len(candidate_idx) == 0:
        return predictions, fixes

    if len(candidate_idx) > 120_000:
        order = np.argsort(d2)
        candidate_idx = candidate_idx[order[:120_000]]

    log.info(f"      V14 roof-hole candidates: {len(candidate_idx):,}")

    n_chunks = (len(candidate_idx) + CHUNK_SIZE - 1) // CHUNK_SIZE
    for ci, start in enumerate(range(0, len(candidate_idx), CHUNK_SIZE)):
        chunk = candidate_idx[start:start + CHUNK_SIZE]
        nlist = safe_query_ball_point(tree_all, xyz[chunk], r=2.25)

        for pt, nb in zip(chunk, nlist):
            nb = np.asarray(nb, dtype=np.int64)
            if len(nb) < 12:
                continue

            nb_pred = predictions[nb]
            b_ratio = float((nb_pred == BUILDING).sum()) / float(len(nb))
            high_ratio = float((nb_pred == HIGHVEG).sum()) / float(len(nb))
            veg_ratio = float(np.isin(nb_pred, [LOWVEG, MIDVEG, HIGHVEG]).sum()) / float(len(nb))

            if b_ratio < 0.48:
                continue

            if high_ratio > 0.35 and veg_ratio > 0.55:
                continue

            roof_surface_like = (
                (hag[pt] > 0.95 and planarity_05_raw[pt] > 0.055) or
                (planarity_05_raw[pt] > 0.12)
            )

            if roof_surface_like:
                predictions[pt] = BUILDING
                fixes += 1

        if n_chunks > 3 and ((ci + 1) % LOG_EVERY_CHUNKS == 0 or (ci + 1) == n_chunks):
            log.info(f"      V14 roof-hole fix: chunk {ci + 1}/{n_chunks} | fixes={fixes:,}")

    log.info(f"      V14 roof holes fixed: {fixes:,}")
    return predictions, fixes


def light_recover_roof_vegetation_to_building(
        xyz,
        predictions,
        hag,
        planarity_05_raw,
        verticality_05_raw,
        log,
        tree_all=None):
    """
    Recover green vegetation patches that are actually roof/wall points.
    A vegetation point becomes Building only when it is close to existing
    Building and has roof/wall geometry support.
    """
    fixes = 0

    building_idx = np.where(predictions == BUILDING)[0]
    if len(building_idx) < 30:
        log.info("      V14 roof vegetation recovery skipped: not enough building points")
        return predictions, fixes

    if tree_all is None:
        tree_all = cKDTree(xyz)

    btree_2d = cKDTree(xyz[building_idx, :2])

    candidate_idx = np.where(np.isin(predictions, [LOWVEG, MIDVEG, HIGHVEG]))[0]
    if len(candidate_idx) == 0:
        return predictions, fixes

    d2, _ = safe_query(btree_2d, xyz[candidate_idx, :2], k=1)
    near_mask = d2 < 2.40
    candidate_idx = candidate_idx[near_mask]
    d2 = d2[near_mask]

    if len(candidate_idx) == 0:
        return predictions, fixes

    geom_candidate = (
        ((hag[candidate_idx] > 0.95) & (planarity_05_raw[candidate_idx] > 0.070)) |
        ((hag[candidate_idx] > 0.30) &
         (verticality_05_raw[candidate_idx] > 0.18) &
         (planarity_05_raw[candidate_idx] > 0.030))
    )

    candidate_idx = candidate_idx[geom_candidate]
    d2 = d2[geom_candidate]

    if len(candidate_idx) == 0:
        return predictions, fixes

    if len(candidate_idx) > 160_000:
        order = np.argsort(d2)
        candidate_idx = candidate_idx[order[:160_000]]

    log.info(f"      V14 roof/wall vegetation recovery candidates: {len(candidate_idx):,}")

    n_chunks = (len(candidate_idx) + CHUNK_SIZE - 1) // CHUNK_SIZE
    for ci, start in enumerate(range(0, len(candidate_idx), CHUNK_SIZE)):
        chunk = candidate_idx[start:start + CHUNK_SIZE]
        nlist = safe_query_ball_point(tree_all, xyz[chunk], r=2.10)

        for pt, nb in zip(chunk, nlist):
            nb = np.asarray(nb, dtype=np.int64)
            if len(nb) < 10:
                continue

            nb_pred = predictions[nb]
            b_ratio = float((nb_pred == BUILDING).sum()) / float(len(nb))
            veg_ratio = float(np.isin(nb_pred, [LOWVEG, MIDVEG, HIGHVEG]).sum()) / float(len(nb))
            high_ratio = float((nb_pred == HIGHVEG).sum()) / float(len(nb))

            roof_like = (
                hag[pt] > 0.95 and
                planarity_05_raw[pt] > 0.070 and
                verticality_05_raw[pt] < 0.86
            )

            strong_roof_like = (
                hag[pt] > 1.20 and
                planarity_05_raw[pt] > 0.115
            )

            wall_like = (
                hag[pt] > 0.30 and
                verticality_05_raw[pt] > 0.20 and
                planarity_05_raw[pt] > 0.035
            )

            has_building_support = (
                b_ratio >= 0.34 or
                (b_ratio >= 0.24 and strong_roof_like) or
                (b_ratio >= 0.26 and wall_like and high_ratio < 0.22)
            )

            if not has_building_support:
                continue

            if high_ratio > 0.62 and veg_ratio > 0.78 and not strong_roof_like:
                continue

            if roof_like or strong_roof_like or wall_like:
                predictions[pt] = BUILDING
                fixes += 1

        if n_chunks > 3 and ((ci + 1) % LOG_EVERY_CHUNKS == 0 or (ci + 1) == n_chunks):
            log.info(f"      V14 roof/wall veg recovery: chunk {ci + 1}/{n_chunks} | fixes={fixes:,}")

    log.info(f"      V14 roof/wall vegetation recovered: {fixes:,}")
    return predictions, fixes


def light_remove_building_noise_inside_vegetation(
        xyz,
        predictions,
        hag,
        planarity_05_raw,
        verticality_05_raw,
        log,
        tree_all=None):
    """
    Remove isolated Building speckles inside tree/vegetation clusters.
    True roofs/walls are protected by geometry and local Building support.
    """
    fixes = 0

    building_idx = np.where(predictions == BUILDING)[0]
    if len(building_idx) == 0:
        return predictions, fixes

    if tree_all is None:
        tree_all = cKDTree(xyz)

    b_hag = hag[building_idx]
    b_plan = planarity_05_raw[building_idx]
    b_vert = verticality_05_raw[building_idx]

    obvious_roof = (b_hag > 1.10) & (b_plan > 0.120)
    obvious_wall = (b_hag > 0.30) & (b_vert > 0.24) & (b_plan > 0.035)
    suspicious_idx = building_idx[~(obvious_roof | obvious_wall)]

    extra_idx = building_idx[(b_plan < 0.090) & (b_vert < 0.22)]
    if len(extra_idx) > 0:
        suspicious_idx = np.unique(np.concatenate([suspicious_idx, extra_idx]))

    if len(suspicious_idx) == 0:
        return predictions, fixes

    if len(suspicious_idx) > 180_000:
        score = planarity_05_raw[suspicious_idx] + verticality_05_raw[suspicious_idx]
        order = np.argsort(score)
        suspicious_idx = suspicious_idx[order[:180_000]]

    log.info(f"      V14 building-in-vegetation cleanup candidates: {len(suspicious_idx):,}")

    n_chunks = (len(suspicious_idx) + CHUNK_SIZE - 1) // CHUNK_SIZE
    for ci, start in enumerate(range(0, len(suspicious_idx), CHUNK_SIZE)):
        chunk = suspicious_idx[start:start + CHUNK_SIZE]
        nlist = safe_query_ball_point(tree_all, xyz[chunk], r=2.60)

        for pt, nb in zip(chunk, nlist):
            nb = np.asarray(nb, dtype=np.int64)
            if len(nb) < 12:
                continue

            nb_pred = predictions[nb]
            b_ratio = float((nb_pred == BUILDING).sum()) / float(len(nb))
            veg_mask = np.isin(nb_pred, [LOWVEG, MIDVEG, HIGHVEG])
            veg_ratio = float(veg_mask.sum()) / float(len(nb))
            high_ratio = float((nb_pred == HIGHVEG).sum()) / float(len(nb))

            protect_roof = (
                hag[pt] > 1.00 and
                planarity_05_raw[pt] > 0.095 and
                b_ratio > 0.25
            )

            protect_wall = (
                hag[pt] > 0.25 and
                verticality_05_raw[pt] > 0.20 and
                planarity_05_raw[pt] > 0.030 and
                b_ratio > 0.24 and
                high_ratio < 0.28
            )

            protect_dense_building = b_ratio >= 0.54

            if protect_roof or protect_wall or protect_dense_building:
                continue

            remove_as_veg = (
                (veg_ratio > 0.72 and b_ratio < 0.38) or
                (high_ratio > 0.48 and veg_ratio > 0.58 and b_ratio < 0.46) or
                (veg_ratio > 0.62 and b_ratio < 0.28)
            )

            if not remove_as_veg:
                continue

            veg_ids = nb[veg_mask]
            predictions[pt] = _majority_veg_class(predictions, veg_ids)
            fixes += 1

        if n_chunks > 3 and ((ci + 1) % LOG_EVERY_CHUNKS == 0 or (ci + 1) == n_chunks):
            log.info(f"      V14 building noise cleanup: chunk {ci + 1}/{n_chunks} | fixes={fixes:,}")

    log.info(f"      V14 building noise removed: {fixes:,}")
    return predictions, fixes



def _majority_veg_from_counts(low_count, mid_count, high_count):
    if high_count >= mid_count and high_count >= low_count:
        return HIGHVEG
    if mid_count >= low_count:
        return MIDVEG
    return LOWVEG


def _majority_non_building_from_array(predictions, ids, fallback=GROUND):
    if ids is None or len(ids) == 0:
        return np.uint8(fallback)
    vals = predictions[np.asarray(ids, dtype=np.int64)]
    vals = vals[vals != BUILDING]
    if len(vals) == 0:
        return np.uint8(fallback)
    classes, counts = np.unique(vals, return_counts=True)
    return np.uint8(classes[counts.argmax()])



def _post_process_v19_core_target_only(
        xyz,
        predictions,
        confidence,
        hag,
        verticality_05_raw,
        planarity_05_raw,
        log,
        active_indices=None):
    """
    V19 crash-free target-only correction.

    What V19 changes compared with V18:
    - roof holes can be fixed even when HAG is weak/wrong, if the point is inside a
      strong 2D building/roof neighbourhood
    - sparse facade/wall bands below roof edges are recovered more strongly
    - Building speckles inside vegetation are cleaned by local vegetation support
      and by small detached 2D component cleanup

    Safety rule stays unchanged:
    - only active_indices are changed
    - context points are support only
    - correction is capped to avoid the previous crash/hang behaviour
    """
    fix_report = {}
    t0 = time.time()

    n_total = len(predictions)
    if active_indices is None:
        active_indices = np.arange(n_total, dtype=np.int64)
        log.info("  V19 target-only correction: no target mask supplied; using full current file.")
    else:
        active_indices = np.asarray(active_indices, dtype=np.int64)
        active_indices = active_indices[(active_indices >= 0) & (active_indices < n_total)]
        active_indices = np.unique(active_indices)

    n_active = int(len(active_indices))
    log.info("  V19 crash-free target-only wall/roof/vegetation cleanup selected.")
    log.info(f"    Active correction points : {n_active:,} / {n_total:,}")
    log.info("    Context is used only for support; context points are not globally corrected.")

    if n_active == 0:
        return predictions, {"V19 skipped empty target": 0}

    if n_active > MAX_TARGET_CORRECTION_POINTS:
        log.warning(
            f"    Target correction skipped: {n_active:,} active points exceeds "
            f"safe cap {MAX_TARGET_CORRECTION_POINTS:,}. Raw model output will be written."
        )
        return predictions, {"V19 skipped by safe cap": 0}

    if int((predictions == BUILDING).sum()) == 0:
        log.info("    V19 skipped: no building support in model output.")
        return predictions, {"V19 skipped no building": 0}

    tree_all = cKDTree(xyz)
    xy_all = xyz[:, :2]

    # ------------------------------------------------------------------
    # Build stable 2D roof/building support.
    # Important: V19 does not trust every Building point as roof support.
    # It favours planar roof-like or vertical wall-like building points.
    # ------------------------------------------------------------------
    building_idx_all = np.where(predictions == BUILDING)[0]
    bldg_hag_all = hag[building_idx_all]
    bldg_plan_all = planarity_05_raw[building_idx_all]
    bldg_vert_all = verticality_05_raw[building_idx_all]

    roof_seed_mask = (
        (bldg_hag_all > 0.35) &
        (
            (bldg_plan_all > 0.045) |
            ((bldg_hag_all > 0.90) & (bldg_vert_all < 0.75)) |
            ((bldg_hag_all > 0.30) & (bldg_vert_all > 0.14) & (bldg_plan_all > 0.020))
        )
    )
    roof_support_idx = building_idx_all[roof_seed_mask]
    if len(roof_support_idx) < 16:
        roof_support_idx = building_idx_all

    tree_bldg_2d = cKDTree(xy_all[building_idx_all])
    tree_roof_2d = cKDTree(xy_all[roof_support_idx])

    active_xyz = xyz[active_indices]
    active_xy = active_xyz[:, :2]
    active_z = active_xyz[:, 2]

    d_bldg, nn_bldg = safe_query(tree_bldg_2d, active_xy, k=1)
    nn_bldg = np.asarray(nn_bldg).reshape(-1)
    near_bldg_ids = building_idx_all[nn_bldg]
    near_bldg_hag = hag[near_bldg_ids]
    near_bldg_z = xyz[near_bldg_ids, 2]

    d_roof, nn_roof = safe_query(tree_roof_2d, active_xy, k=1)
    nn_roof = np.asarray(nn_roof).reshape(-1)
    near_roof_ids = roof_support_idx[nn_roof]
    near_roof_hag = hag[near_roof_ids]
    near_roof_z = xyz[near_roof_ids, 2]


    def _count2d(tree, pts, radius):
        try:
            return safe_query_ball_point(tree, pts, r=radius, return_length=True).astype(np.int32, copy=False)
        except Exception:
            return np.zeros(len(pts), dtype=np.int32)

    bldg_count_12 = _count2d(tree_bldg_2d, active_xy, 1.20)
    bldg_count_20 = _count2d(tree_bldg_2d, active_xy, 2.00)
    roof_count_12 = _count2d(tree_roof_2d, active_xy, 1.20)
    roof_count_22 = _count2d(tree_roof_2d, active_xy, 2.20)

    active_pred = predictions[active_indices]
    active_hag = hag[active_indices]
    active_plan = planarity_05_raw[active_indices]
    active_vert = verticality_05_raw[active_indices]

    def _cap_candidates(mask, cap, score=None):
        ids = active_indices[mask]
        if len(ids) <= cap:
            return ids
        local_pos = np.where(mask)[0]
        if score is None:
            s = (
                (1.0 / (d_roof[local_pos] + 0.05)) +
                0.020 * np.minimum(roof_count_22[local_pos], 100) +
                0.012 * np.minimum(bldg_count_20[local_pos], 100) +
                0.35 * active_plan[local_pos] +
                0.25 * active_vert[local_pos]
            )
        else:
            s = score(local_pos)
        keep = np.argsort(-s)[:cap]
        return ids[keep]

    def _pos_for_ids(ids):
        return np.searchsorted(active_indices, ids)

    def _local_lists(ids, radius):
        return safe_query_ball_point(tree_all, xyz[ids], r=radius)

    # ------------------------------------------------------------------
    # A0. Roof interior / terrace holes -> Building.
    # This specifically targets orange/green roof holes inside a pink roof.
    # HAG is intentionally relaxed because small fence HAG can be unreliable.
    # ------------------------------------------------------------------
    roof_hole_mask = (
        np.isin(active_pred, [GROUND, LOWVEG, MIDVEG, HIGHVEG]) &
        (d_roof < 1.35) &
        (roof_count_12 >= 4) &
        (roof_count_22 >= 12) &
        (bldg_count_20 >= 12) &
        (active_hag > -0.35) &
        (active_z > near_roof_z - 1.80) &
        (active_z < near_roof_z + 1.60) &
        (
            (active_plan > 0.012) |
            (roof_count_22 >= 20) |
            ((active_hag > 0.20) & (active_vert < 0.92))
        )
    )
    roof_hole_candidates = _cap_candidates(roof_hole_mask, 40_000)

    fixes_roof_holes = 0
    if len(roof_hole_candidates) > 0:
        nlist = _local_lists(roof_hole_candidates, 1.55)
        for pt, nb in zip(roof_hole_candidates, nlist):
            nb = np.asarray(nb, dtype=np.int64)
            if len(nb) < 8:
                continue

            nb_pred = predictions[nb]
            b_ratio = float((nb_pred == BUILDING).sum()) / float(len(nb))
            high_ratio = float((nb_pred == HIGHVEG).sum()) / float(len(nb))
            veg_ratio = float(np.isin(nb_pred, [LOWVEG, MIDVEG, HIGHVEG]).sum()) / float(len(nb))

            pos = int(np.searchsorted(active_indices, pt))
            p = float(planarity_05_raw[pt])
            v = float(verticality_05_raw[pt])
            h = float(hag[pt])
            rc = int(roof_count_22[pos])
            bc = int(bldg_count_20[pos])

            # Dense tree / flat-ground guard.
            if (high_ratio > 0.70 and veg_ratio > 0.80 and b_ratio < 0.15) or (high_ratio > 0.84 and b_ratio < 0.08):
                continue
            # Flat-ground / courtyard guard: majority vegetation → not a roof hole.
            if veg_ratio >= 0.65 and b_ratio < 0.20:
                continue

            inside_roof_support = (rc >= 14 and bc >= 12 and d_roof[pos] < 1.35)
            roof_shape_ok = (p > 0.012 or v < 0.92 or h > 0.20)
            # Require stronger local building support to prevent leaking into
            # flat ground, courtyards, and paved areas near buildings.
            if inside_roof_support and roof_shape_ok and (b_ratio >= 0.25 or (rc >= 20 and b_ratio >= 0.12)):
                predictions[pt] = BUILDING
                fixes_roof_holes += 1

    fix_report["V19 roof interior holes -> Building"] = int(fixes_roof_holes)
    log.info(f"    V19 roof interior holes fixed: {fixes_roof_holes:,}")

    # Refresh active state after roof interior fix.
    active_pred = predictions[active_indices]
    active_hag = hag[active_indices]
    active_plan = planarity_05_raw[active_indices]
    active_vert = verticality_05_raw[active_indices]

    # ------------------------------------------------------------------
    # A1. Roof edge/patch recovery -> Building.
    # Slightly stronger than V18, but still requires 2D support.
    # ------------------------------------------------------------------
    roof_patch_mask = (
        np.isin(active_pred, [GROUND, LOWVEG, MIDVEG, HIGHVEG]) &
        (d_roof < 1.85) &
        (roof_count_22 >= 10) &
        (active_hag > -0.15) &
        (active_z > near_roof_z - 2.20) &
        (active_z < near_roof_z + 1.90) &
        (
            ((active_hag > 0.45) & (active_plan > 0.018)) |
            ((active_hag > 0.85) & (active_vert < 0.92)) |
            ((roof_count_22 >= 18) & (active_plan > 0.010)) |
            ((bldg_count_20 >= 18) & (active_hag > 0.15))
        )
    )
    roof_patch_candidates = _cap_candidates(roof_patch_mask, 45_000)

    fixes_roof_patches = 0
    if len(roof_patch_candidates) > 0:
        nlist = _local_lists(roof_patch_candidates, 1.75)
        for pt, nb in zip(roof_patch_candidates, nlist):
            nb = np.asarray(nb, dtype=np.int64)
            if len(nb) < 8:
                continue

            nb_pred = predictions[nb]
            b_ratio = float((nb_pred == BUILDING).sum()) / float(len(nb))
            high_ratio = float((nb_pred == HIGHVEG).sum()) / float(len(nb))
            veg_ratio = float(np.isin(nb_pred, [LOWVEG, MIDVEG, HIGHVEG]).sum()) / float(len(nb))

            pos = int(np.searchsorted(active_indices, pt))
            h = float(hag[pt])
            p = float(planarity_05_raw[pt])
            v = float(verticality_05_raw[pt])
            rc = int(roof_count_22[pos])
            bc = int(bldg_count_20[pos])

            if (high_ratio > 0.68 and veg_ratio > 0.78 and b_ratio < 0.18) or (veg_ratio > 0.90 and b_ratio < 0.10):
                continue
            # Flat-ground / courtyard guard: majority vegetation → not a roof patch.
            if veg_ratio >= 0.70 and b_ratio < 0.18:
                continue

            roof_like = (
                (h > 0.45 and p > 0.018) or
                (h > 0.85 and v < 0.92) or
                (rc >= 18 and p > 0.010) or
                (bc >= 20 and h > 0.15)
            )

            if roof_like and (b_ratio >= 0.22 or (rc >= 16 and b_ratio >= 0.12) or (bc >= 20 and b_ratio >= 0.12)):
                predictions[pt] = BUILDING
                fixes_roof_patches += 1

    fix_report["V19 roof patches -> Building"] = int(fixes_roof_patches)
    log.info(f"    V19 roof patches fixed: {fixes_roof_patches:,}")

    # Refresh after roof recovery.
    active_pred = predictions[active_indices]
    active_hag = hag[active_indices]
    active_plan = planarity_05_raw[active_indices]
    active_vert = verticality_05_raw[active_indices]

    # ------------------------------------------------------------------
    # B. Wall/facade recovery below/near roof footprint.
    # This fixes sparse facade points that the model labels vegetation.
    # ------------------------------------------------------------------
    wall_band_mask = (
        np.isin(active_pred, [GROUND, LOWVEG, MIDVEG, HIGHVEG]) &
        (d_roof < 2.20) &
        ((roof_count_22 >= 6) | (bldg_count_20 >= 8)) &
        (active_hag > -0.10) &
        (active_z < near_roof_z + 1.25) &
        (active_z > near_roof_z - 6.00) &
        (
            ((active_vert > 0.070) & (active_plan > 0.006)) |
            ((active_vert > 0.14) & (active_hag > 0.05)) |
            ((roof_count_22 >= 16) & (active_hag > -0.05)) |
            ((bldg_count_20 >= 15) & (active_hag > -0.05))
        )
    )
    wall_band_candidates = _cap_candidates(wall_band_mask, 55_000)

    fixes_wall_band = 0
    if len(wall_band_candidates) > 0:
        nlist = _local_lists(wall_band_candidates, 1.95)
        for pt, nb in zip(wall_band_candidates, nlist):
            nb = np.asarray(nb, dtype=np.int64)
            if len(nb) < 8:
                continue

            nb_pred = predictions[nb]
            b_count = int((nb_pred == BUILDING).sum())
            low_count = int((nb_pred == LOWVEG).sum())
            mid_count = int((nb_pred == MIDVEG).sum())
            high_count = int((nb_pred == HIGHVEG).sum())
            veg_count = low_count + mid_count + high_count

            b_ratio = b_count / float(len(nb))
            veg_ratio = veg_count / float(len(nb))
            high_ratio = high_count / float(len(nb))

            pos = int(np.searchsorted(active_indices, pt))
            h = float(hag[pt])
            v = float(verticality_05_raw[pt])
            p = float(planarity_05_raw[pt])
            rc = int(roof_count_22[pos])
            bc = int(bldg_count_20[pos])
            dr = float(d_roof[pos])
            db = float(d_bldg[pos])

            # Tree/canopy guard.  A wall can have vegetation nearby, but a nearly
            # pure high-vegetation ball should stay vegetation.
            if (high_ratio > 0.72 and b_ratio < 0.12 and rc < 20) or (veg_ratio > 0.85 and b_ratio < 0.08 and rc < 22 and bc < 18):
                continue

            wall_shape = (
                (v > 0.07 and p > 0.006) or
                (v > 0.14 and h > -0.05) or
                (rc >= 18 and dr < 2.40) or
                (bc >= 18 and db < 2.40)
            )
            wall_support = (
                (b_ratio >= 0.22) or
                (b_ratio >= 0.16 and rc >= 16) or
                (rc >= 28 and b_ratio >= 0.12 and bc >= 12)
            )

            if wall_shape and wall_support and high_ratio < 0.68 and veg_ratio < 0.78:
                predictions[pt] = BUILDING
                fixes_wall_band += 1
                continue

            # Very close to a building/wall strip; allow weaker shape only if not vegetation/ground blob.
            if (
                db < 0.58 and
                h > -0.05 and
                (b_ratio >= 0.24 or bc >= 24) and
                high_ratio < 0.58 and
                veg_ratio < 0.68
            ):
                predictions[pt] = BUILDING
                fixes_wall_band += 1

    fix_report["V19 wall/facade vegetation -> Building"] = int(fixes_wall_band)
    log.info(f"    V19 wall/facade vegetation fixed: {fixes_wall_band:,}")

    # ------------------------------------------------------------------
    # C. Building speckles inside vegetation -> Vegetation.
    # ------------------------------------------------------------------

        # ------------------------------------------------------------------
    # B2. V21 disabled.
    #
    # Reason:
    # V21 was still leaking Building into vegetation/ground-side points.
    # It is too risky for mixed ground/vegetation fences.
    # ------------------------------------------------------------------
    fixes_bottom_wall = 0
    fix_report["V21 bottom wall vegetation -> Building"] = 0
    log.info("    V21 bottom wall vegetation fixed: 0 (disabled - leakage guard)")


    # ------------------------------------------------------------------
    # C. Building speckles inside vegetation -> Vegetation.
    # V18 was too protective and often removed 0. V19 first removes local
    # vegetation-dominated building noise, then removes small detached 2D blobs.
    # ------------------------------------------------------------------
    active_pred = predictions[active_indices]
    building_active = active_indices[active_pred == BUILDING]

    if len(building_active) > 70_000:
        pos = _pos_for_ids(building_active)
        score = (
            (1.0 - confidence[building_active]) +
            0.18 * np.minimum(d_roof[pos], 5.0) -
            0.012 * np.minimum(roof_count_22[pos], 100) -
            0.010 * np.minimum(bldg_count_20[pos], 100)
        )
        keep = np.argsort(-score)[:70_000]
        building_active = building_active[keep]

    fixes_noise_local = 0
    local_remove_mask = np.zeros(n_total, dtype=bool)

    if len(building_active) > 0:
        nlist = _local_lists(building_active, 2.25)
        for pt, nb in zip(building_active, nlist):
            nb = np.asarray(nb, dtype=np.int64)
            if len(nb) < 10:
                continue

            nb_pred = predictions[nb]
            b_count = int((nb_pred == BUILDING).sum())
            low_count = int((nb_pred == LOWVEG).sum())
            mid_count = int((nb_pred == MIDVEG).sum())
            high_count = int((nb_pred == HIGHVEG).sum())
            ground_count = int((nb_pred == GROUND).sum())
            veg_count = low_count + mid_count + high_count

            b_ratio = b_count / float(len(nb))
            veg_ratio = veg_count / float(len(nb))
            high_ratio = high_count / float(len(nb))
            ground_ratio = ground_count / float(len(nb))
            non_bldg_ratio = veg_ratio + ground_ratio

            pos = int(np.searchsorted(active_indices, pt))
            h = float(hag[pt])
            v = float(verticality_05_raw[pt])
            p = float(planarity_05_raw[pt])
            rc = int(roof_count_22[pos])
            bc = int(bldg_count_20[pos])
            dr = float(d_roof[pos])

            protect_roof = (
                (dr < 1.20) and
                (h > -0.15) and
                (p > 0.025 or (rc >= 12 and bc >= 10)) and
                (high_ratio < 0.70) and
                (ground_ratio < 0.60)
            )
            protect_wall = (
                (dr < 1.50) and
                (h > -0.20) and
                ((v > 0.10 and p > 0.010) or (v > 0.18)) and
                (rc >= 14 or bc >= 16) and
                (high_ratio < 0.65) and
                (ground_ratio < 0.50)
            )
            protect_dense_building = (
                (b_ratio >= 0.65) and
                (p > 0.020 or v > 0.12) and
                (dr < 1.50) and
                (high_ratio < 0.55) and
                (ground_ratio < 0.40)
            )

            if protect_roof or protect_wall or protect_dense_building:
                continue

            remove_as_veg = (
                (dr > 1.30 and veg_ratio > 0.35 and b_ratio < 0.55 and rc < 12 and bc < 14) or
                (high_ratio > 0.40 and veg_ratio > 0.48 and b_ratio < 0.60 and rc < 12) or
                (veg_ratio > 0.60 and b_ratio < 0.58 and rc < 14 and bc < 14) or
                (confidence[pt] < 0.88 and veg_ratio > 0.42 and b_ratio < 0.62 and rc < 16) or
                (ground_ratio > 0.55 and b_ratio < 0.40 and dr > 1.50 and rc < 10) or
                (non_bldg_ratio > 0.72 and b_ratio < 0.45 and rc < 14 and dr > 1.20)
            )
            if not remove_as_veg:
                continue

            predictions[pt] = np.uint8(_majority_veg_from_counts(low_count, mid_count, high_count))
            local_remove_mask[pt] = True
            fixes_noise_local += 1

    # ------------------------------------------------------------------
    # C2. Detached/small 2D Building component cleanup.
    # This catches pink islands inside vegetation that local ratio rules protect
    # because the whole small island was mislabelled Building.
    # ------------------------------------------------------------------
    fixes_noise_components = 0
    active_pred = predictions[active_indices]
    building_active_after = active_indices[active_pred == BUILDING]

    if len(building_active_after) > 0:
        xy_b = xy_all[building_active_after]
        grid = 0.55
        min_xy = active_xy.min(axis=0)
        cells = np.floor((xy_b - min_xy) / grid).astype(np.int64)
        dims_y = int(cells[:, 1].max() - cells[:, 1].min() + 5) + 1
        # Shift Y cells positive to avoid negative key collisions.
        cells[:, 1] -= cells[:, 1].min() - 2
        keys = cells[:, 0] * dims_y + cells[:, 1]

        order = np.argsort(keys)
        keys_sorted = keys[order]
        ids_sorted = building_active_after[order]
        unique_keys, starts, counts = np.unique(keys_sorted, return_index=True, return_counts=True)
        key_to_group = {int(k): i for i, k in enumerate(unique_keys)}

        visited = set()
        components = []
        key_set = set(int(k) for k in unique_keys)
        for k0 in unique_keys:
            k0 = int(k0)
            if k0 in visited:
                continue
            stack = [k0]
            visited.add(k0)
            comp_groups = []
            while stack:
                k = stack.pop()
                comp_groups.append(key_to_group[k])
                cx = k // dims_y
                cy = k - cx * dims_y
                for dx in (-1, 0, 1):
                    nx = cx + dx
                    for dy in (-1, 0, 1):
                        if dx == 0 and dy == 0:
                            continue
                        ny = cy + dy
                        if ny < 0:
                            continue
                        nk = int(nx * dims_y + ny)
                        if nk in key_set and nk not in visited:
                            visited.add(nk)
                            stack.append(nk)
            components.append(comp_groups)

        remove_component_ids = []
        for comp_groups in components:
            # Collect point ids for this component.
            parts = []
            for gi in comp_groups:
                s = starts[gi]
                e = s + counts[gi]
                parts.append(ids_sorted[s:e])
            comp_ids = np.concatenate(parts) if len(parts) > 1 else parts[0]
            comp_n = int(len(comp_ids))
            if comp_n == 0:
                continue

            pos = _pos_for_ids(comp_ids)
            comp_rc = roof_count_22[pos]
            comp_bc = bldg_count_20[pos]
            comp_dr = d_roof[pos]
            comp_h = hag[comp_ids]
            comp_p = planarity_05_raw[comp_ids]
            comp_v = verticality_05_raw[comp_ids]
            comp_conf = confidence[comp_ids]

            roof_geom = ((comp_h > -0.10) & (comp_p > 0.030) & (comp_dr < 1.60))
            wall_geom = ((comp_h > -0.20) & (comp_v > 0.12) & (comp_p > 0.010) & (comp_dr < 2.20))
            support_good = ((comp_rc >= 14) | (comp_bc >= 16) | (comp_dr < 1.30))

            good_ratio = float((roof_geom | wall_geom | support_good).sum()) / float(comp_n)
            median_rc = float(np.median(comp_rc))
            median_bc = float(np.median(comp_bc))
            median_dr = float(np.median(comp_dr))
            mean_conf = float(np.mean(comp_conf))
            mean_plan = float(np.mean(comp_p))
            mean_hag = float(np.mean(comp_h))

            # Keep main building body and roof/wall-connected strips.
            keep_component = (
                comp_n >= 80 or
                (good_ratio >= 0.40 and median_dr < 1.80) or
                (median_rc >= 14 and median_dr < 1.60) or
                (median_bc >= 20 and median_dr < 1.60) or
                (median_dr < 1.20 and mean_plan > 0.020)
            )
            if keep_component:
                continue

            remove_component = (
                comp_n < 120 or
                (comp_n < 350 and median_dr > 1.60 and median_rc < 12 and median_bc < 14) or
                (comp_n < 300 and good_ratio < 0.25 and mean_conf < 0.96) or
                (comp_n < 400 and median_dr > 2.00 and mean_plan < 0.025 and mean_hag < 15.0)
            )
            if remove_component:
                remove_component_ids.append(comp_ids)

        if remove_component_ids:
            remove_component_ids = np.concatenate(remove_component_ids)
            # Reclass each removed component point using nearest non-building neighbours.
            k = min(18, len(xyz))
            _, nn_ids = safe_query(tree_all, xyz[remove_component_ids], k=k)
            if k == 1:
                nn_ids = nn_ids.reshape(-1, 1)
            for pt, nn in zip(remove_component_ids, nn_ids):
                predictions[pt] = _majority_non_building_from_array(predictions, nn[1:], fallback=HIGHVEG)
                fixes_noise_components += 1

    # ------------------------------------------------------------------
    # C3. V22 final hard Building leakage rollback.
    #
    # Crash-safe chunked rollback:
    # - Removes pink Building points inside ground/vegetation-dominant neighborhoods.
    # - Protects real roof/wall/building clusters.
    # - Runs only on active fence points.
    # ------------------------------------------------------------------
    active_pred = predictions[active_indices]
    building_active = active_indices[active_pred == BUILDING]

    fixes_hard_leakage = 0

    if len(building_active) > 0:
        # Cap worst-looking building candidates first to avoid memory spikes.
        if len(building_active) > 120_000:
            pos_all = _pos_for_ids(building_active)
            score = (
                (1.0 - confidence[building_active]) +
                0.30 * np.minimum(d_roof[pos_all], 6.0) -
                0.020 * np.minimum(roof_count_22[pos_all], 120) -
                0.018 * np.minimum(bldg_count_20[pos_all], 120)
            )
            keep = np.argsort(-score)[:120_000]
            building_active = building_active[keep]

        V22_CHUNK_SIZE = 15_000

        for c0 in range(0, len(building_active), V22_CHUNK_SIZE):
            chunk_ids = building_active[c0:c0 + V22_CHUNK_SIZE]
            nlist = _local_lists(chunk_ids, 2.75)

            for pt, nb in zip(chunk_ids, nlist):
                nb = np.asarray(nb, dtype=np.int64)

                if len(nb) < 12:
                    continue

                pos = int(np.searchsorted(active_indices, pt))

                nb_pred = predictions[nb]

                b_count = int((nb_pred == BUILDING).sum())
                ground_count = int((nb_pred == GROUND).sum())
                low_count = int((nb_pred == LOWVEG).sum())
                mid_count = int((nb_pred == MIDVEG).sum())
                high_count = int((nb_pred == HIGHVEG).sum())

                veg_count = low_count + mid_count + high_count
                non_build_count = ground_count + veg_count

                b_ratio = b_count / float(len(nb))
                ground_ratio = ground_count / float(len(nb))
                veg_ratio = veg_count / float(len(nb))
                high_ratio = high_count / float(len(nb))
                non_build_ratio = non_build_count / float(len(nb))

                h = float(hag[pt])
                v = float(verticality_05_raw[pt])
                p = float(planarity_05_raw[pt])
                dr = float(d_roof[pos])
                db = float(d_bldg[pos])
                rc = int(roof_count_22[pos])
                bc = int(bldg_count_20[pos])

                # Protect true roof.
                true_roof = (
                    (dr < 1.25) and
                    (rc >= 18 or bc >= 20) and
                    (h > 0.20) and
                    (p > 0.030 or rc >= 28) and
                    (high_ratio < 0.45)
                )

                # Protect true wall/facade.
                true_wall = (
                    (db < 0.65 or dr < 1.50) and
                    (bc >= 18 or rc >= 20 or b_ratio >= 0.42) and
                    (h > -0.05) and
                    (
                        (v > 0.12 and p > 0.008) or
                        (v > 0.20) or
                        (bc >= 28)
                    ) and
                    (veg_ratio < 0.62) and
                    (high_ratio < 0.38)
                )

                true_building_cluster = (
                    (b_ratio >= 0.68) and
                    (bc >= 22 or rc >= 22 or dr < 1.10 or db < 0.55) and
                    (veg_ratio < 0.55) and
                    (ground_ratio < 0.42) and
                    (
                        (h > 0.25) or
                        (v > 0.16) or
                        (p > 0.030) or
                        (rc >= 30 and bc >= 24)
                    )
                )

                if true_roof or true_wall or true_building_cluster:
                    continue

                leakage = (
                    (
                        (ground_ratio > 0.55 and b_ratio < 0.42) or
                        (veg_ratio > 0.56 and b_ratio < 0.44) or
                        (high_ratio > 0.24 and b_ratio < 0.50) or
                        (non_build_ratio > 0.72 and b_ratio < 0.50)
                    ) and
                    (rc < 22) and
                    (bc < 26) and
                    (dr > 1.10) and
                    (db > 0.45)
                )

                if not leakage:
                    continue

                if veg_count > ground_count:
                    predictions[pt] = np.uint8(
                        _majority_veg_from_counts(low_count, mid_count, high_count)
                    )
                else:
                    predictions[pt] = GROUND

                fixes_hard_leakage += 1

            del nlist

    # ------------------------------------------------------------------
    # C4. V23 ground-skirt Building leakage rollback.
    #
    # Purpose:
    # - Removes pink Building leakage on ground/road/yard near buildings.
    # - This is stricter than V22 for low-height, non-vertical points.
    # - Protects real roofs and vertical walls.
    # - Runs only on selected active fence points.
    # ------------------------------------------------------------------
    active_pred = predictions[active_indices]
    building_active = active_indices[active_pred == BUILDING]

    fixes_ground_skirt = 0

    if len(building_active) > 0:
        # Focus on points most likely to be ground leakage.
        if len(building_active) > 140_000:
            pos_all = _pos_for_ids(building_active)
            score = (
                0.55 * np.minimum(np.maximum(0.65 - hag[building_active], 0.0), 2.0) +
                0.35 * np.minimum(d_roof[pos_all], 5.0) +
                0.20 * (1.0 - confidence[building_active]) -
                0.018 * np.minimum(roof_count_22[pos_all], 120) -
                0.016 * np.minimum(bldg_count_20[pos_all], 120)
            )
            keep = np.argsort(-score)[:140_000]
            building_active = building_active[keep]

        V23_CHUNK_SIZE = 15_000

        for c0 in range(0, len(building_active), V23_CHUNK_SIZE):
            chunk_ids = building_active[c0:c0 + V23_CHUNK_SIZE]
            nlist = _local_lists(chunk_ids, 1.95)

            for pt, nb in zip(chunk_ids, nlist):
                nb = np.asarray(nb, dtype=np.int64)

                if len(nb) < 10:
                    continue

                pos = int(np.searchsorted(active_indices, pt))
                nb_pred = predictions[nb]

                b_count = int((nb_pred == BUILDING).sum())
                ground_count = int((nb_pred == GROUND).sum())
                low_count = int((nb_pred == LOWVEG).sum())
                mid_count = int((nb_pred == MIDVEG).sum())
                high_count = int((nb_pred == HIGHVEG).sum())

                veg_count = low_count + mid_count + high_count
                non_build_count = ground_count + veg_count

                b_ratio = b_count / float(len(nb))
                ground_ratio = ground_count / float(len(nb))
                veg_ratio = veg_count / float(len(nb))
                high_ratio = high_count / float(len(nb))
                non_build_ratio = non_build_count / float(len(nb))

                h = float(hag[pt])
                v = float(verticality_05_raw[pt])
                p = float(planarity_05_raw[pt])
                dr = float(d_roof[pos])
                db = float(d_bldg[pos])
                rc = int(roof_count_22[pos])
                bc = int(bldg_count_20[pos])

                # Protect true roof points.
                true_roof = (
                    (dr < 1.25) and
                    (rc >= 20 or bc >= 22) and
                    (h > 0.35) and
                    (p > 0.030 or rc >= 30) and
                    (ground_ratio < 0.34) and
                    (high_ratio < 0.45)
                )

                # Protect real wall/base: includes lower wall-base also.
                # This prevents V23 ground rollback from eating valid bottom facade points.
                true_wall = (
                    (h > 0.28) and
                    (db < 0.85 or dr < 1.55) and
                    (
                        (v > 0.14 and p > 0.005) or
                        (v > 0.22)
                    ) and
                    (
                        (b_ratio >= 0.22) or
                        (bc >= 16) or
                        (rc >= 18)
                    ) and
                    (ground_ratio < 0.68) and
                    (veg_ratio < 0.72)
                )

                if true_roof or true_wall:
                    continue

                # Main case shown in your screenshot:
                # Building class bleeding across low ground/yard near the building.
                ground_skirt_leakage = (
                    (
                        (ground_ratio >= 0.38 and b_ratio < 0.70) or
                        (ground_ratio >= 0.30 and non_build_ratio >= 0.58 and b_ratio < 0.76) or
                        (h < 0.30 and ground_ratio >= 0.24 and non_build_ratio >= 0.48 and v < 0.12)
                    ) and
                    (h < 0.75) and
                    (v < 0.18) and
                    (rc < 26) and
                    (bc < 34)
                )

                # Even if support counters are high because the point is near the roof,
                # low-HAG non-vertical points surrounded by ground should not remain Building.
                close_ground_halo = (
                    (h < 0.42) and
                    (v < 0.10) and
                    (ground_ratio >= 0.32) and
                    (non_build_ratio >= 0.54) and
                    (b_ratio < 0.82) and
                    (high_ratio < 0.30)
                )

                if not (ground_skirt_leakage or close_ground_halo):
                    continue

                # Roll back to the local dominant non-building class.
                if ground_count >= veg_count:
                    predictions[pt] = GROUND
                else:
                    predictions[pt] = np.uint8(
                        _majority_veg_from_counts(low_count, mid_count, high_count)
                    )

                fixes_ground_skirt += 1

            del nlist

    fixes_hard_leakage += fixes_ground_skirt
    fix_report["V23 ground-skirt Building rollback"] = int(fixes_ground_skirt)
    log.info(f"    V23 ground-skirt Building rollback: {fixes_ground_skirt:,}")

    base_noise = int(fixes_noise_local + fixes_noise_components)
    total_noise = int(base_noise + fixes_hard_leakage)

    fix_report["V19 Building noise -> Vegetation/non-building"] = base_noise
    fix_report["V22 hard Building leakage rollback"] = int(fixes_hard_leakage)

    log.info(f"    V22 hard Building leakage rollback: {fixes_hard_leakage:,}")
    log.info(
        f"    V19 building noise removed: {total_noise:,} "
        f"(local={fixes_noise_local:,}, components={fixes_noise_components:,}, hard={fixes_hard_leakage:,})"
    )

    log.info(f"  V19 target-only correction done: {sum(fix_report.values()):,} fixes ({time.time() - t0:.1f}s)")
    return predictions, fix_report


def post_process_light_v16_target_only(
        xyz,
        predictions,
        confidence,
        hag,
        verticality_05_raw,
        planarity_05_raw,
        log,
        active_indices=None):
    """
    V20 crash-free wrapper around V19 target-only correction.

    Important:
    - Only active_indices are corrected.
    - Context points are support only.
    - Large fences are split into safe batches, so V19 does not skip and does not crash.
    """
    t0 = time.time()
    n_total = len(predictions)

    if active_indices is None:
        active_indices = np.arange(n_total, dtype=np.int64)
        log.info("  V20 batched V19: no target mask supplied; using full current file.")
    else:
        active_indices = np.asarray(active_indices, dtype=np.int64)
        active_indices = active_indices[
            (active_indices >= 0) & (active_indices < n_total)
        ]
        active_indices = np.unique(active_indices)

    n_active = int(len(active_indices))

    log.info("")
    log.info("  V20 BATCHED V19 TARGET-ONLY CORRECTION ENABLED")
    log.info(f"    Active correction points : {n_active:,} / {n_total:,}")
    log.info("    Context points are support only; only active fence points are changed.")

    if n_active == 0:
        return predictions, {"V20 skipped empty target": 0}

    if not globals().get("V19_BATCHED_CORRECTION", True):
        log.info("    V19_BATCHED_CORRECTION is False. Running V19 core directly.")
        return _post_process_v19_core_target_only(
            xyz,
            predictions,
            confidence,
            hag,
            verticality_05_raw,
            planarity_05_raw,
            log,
            active_indices=active_indices,
        )

    max_points = int(globals().get("MAX_TARGET_CORRECTION_POINTS", 80_000))
    requested_batch_size = int(globals().get("V19_BATCH_SIZE", 60_000))
    batch_size = max(1_000, min(requested_batch_size, max_points))

    log.info(f"    Safe cap per V19 core    : {max_points:,}")
    log.info(f"    Batch size               : {batch_size:,}")

    if n_active <= max_points:
        log.info("    Small target set detected. Running V19 core directly.")
        return _post_process_v19_core_target_only(
            xyz,
            predictions,
            confidence,
            hag,
            verticality_05_raw,
            planarity_05_raw,
            log,
            active_indices=active_indices,
        )

    total_report = {}
    total_fixes = 0
    n_batches = int(np.ceil(n_active / float(batch_size)))

    log.info(f"    Large target set detected. Running {n_batches} V19 safe batches.")

    for batch_id, start in enumerate(range(0, n_active, batch_size), start=1):
        end = min(start + batch_size, n_active)
        batch_indices = active_indices[start:end]

        log.info("")
        log.info(
            f"    V20 batch {batch_id}/{n_batches}: "
            f"{len(batch_indices):,} target points"
        )

        t_batch = time.time()

        try:
            predictions, batch_report = _post_process_v19_core_target_only(
                xyz,
                predictions,
                confidence,
                hag,
                verticality_05_raw,
                planarity_05_raw,
                log,
                active_indices=batch_indices,
            )
        except Exception as e:
            log.error(f"    V20 batch {batch_id}/{n_batches} failed: {e}")
            log.debug(traceback.format_exc())
            batch_report = {f"V20 batch {batch_id} failed": 0}

        batch_fixes = int(sum(batch_report.values()))
        total_fixes += batch_fixes

        for key, value in batch_report.items():
            total_report[key] = int(total_report.get(key, 0)) + int(value)

        log.info(
            f"    V20 batch {batch_id}/{n_batches} done: "
            f"{batch_fixes:,} fixes in {time.time() - t_batch:.1f}s"
        )

        gc.collect()

    total_report["V20 batched target points processed"] = n_active

    log.info("")
    log.info(
        f"  V20 batched V19 correction done: "
        f"{total_fixes:,} fixes | {n_batches} batches | "
        f"{time.time() - t0:.1f}s"
    )

    return predictions, total_report



# =====================================================================
# POST-PROCESSING ORCHESTRATOR
# =====================================================================




# =====================================================================
# V12 BUILDING-ONLY COMPONENT PIPELINE
# =====================================================================

def _majority_non_building_from_knn(predictions, nn_ids, fallback_cls=GROUND):
    counts = {GROUND: 0, LOWVEG: 0, MIDVEG: 0, HIGHVEG: 0}
    for idx in np.asarray(nn_ids, dtype=np.int64).ravel():
        cls = int(predictions[idx])
        if cls != BUILDING and cls in counts:
            counts[cls] += 1
    best_cls = fallback_cls
    best_count = -1
    for cls in (GROUND, LOWVEG, MIDVEG, HIGHVEG):
        if counts[cls] > best_count:
            best_cls = cls
            best_count = counts[cls]
    return np.uint8(best_cls)


def _component_keys_from_seed_cells(keys_unique, dims_y):
    """
    8-neighbour connected components on occupied XY grid cells.
    Returns list[list[int_key]]. Pure Python here is fine because it runs on
    occupied cells, not every point.
    """
    key_set = set(int(k) for k in keys_unique)
    visited = set()
    components = []

    for start_key in list(key_set):
        if start_key in visited:
            continue
        stack = [start_key]
        visited.add(start_key)
        comp = []

        while stack:
            key = stack.pop()
            comp.append(key)
            cx = key // dims_y
            cy = key - cx * dims_y

            for dx in (-1, 0, 1):
                nx = cx + dx
                if nx < 0:
                    continue
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    ny = cy + dy
                    if ny < 0 or ny >= dims_y:
                        continue
                    nk = int(nx * dims_y + ny)
                    if nk in key_set and nk not in visited:
                        visited.add(nk)
                        stack.append(nk)

        components.append(comp)

    return components


def _dilate_cell_keys(keys, dims_x, dims_y, radius_cells=2):
    out = set()
    for key in keys:
        key = int(key)
        cx = key // dims_y
        cy = key - cx * dims_y
        for dx in range(-radius_cells, radius_cells + 1):
            nx = cx + dx
            if nx < 0 or nx >= dims_x:
                continue
            for dy in range(-radius_cells, radius_cells + 1):
                ny = cy + dy
                if ny < 0 or ny >= dims_y:
                    continue
                out.add(int(nx * dims_y + ny))
    return np.fromiter(out, dtype=np.int64)


def post_process_building_only_component(
        xyz,
        predictions,
        confidence,
        hag,
        vert_raw,
        plan_raw,
        log,
        raw_predictions=None):
    """
    V12 building-only pipeline.

    Purpose:
      - Stop local rescue/rollback war.
      - Keep only real connected building body/components.
      - Rescue roof only inside/near kept building footprint.
      - Remove pink building noise inside vegetation/canopy islands.

    Use this ONLY when Advanced Target Classes == Building.
    """
    fix_report = {}

    n = len(predictions)
    if n == 0:
        return predictions, fix_report

    raw_predictions = predictions.copy() if raw_predictions is None else raw_predictions
    tree_all = cKDTree(xyz)

    original_building = predictions == BUILDING
    if int(original_building.sum()) == 0:
        log.info("    V12 building-only: no Building seeds found.")
        fix_report["v12_no_building_seed"] = 0
        return predictions, fix_report

    # Strong seeds: these are the points allowed to define the building footprint.
    # Do NOT let every pink point become a seed; vegetation noise is often pink too.
    roof_seed = (
        original_building &
        (hag > 0.90) &
        (plan_raw > 0.085)
    )
    wall_seed = (
        original_building &
        (hag > 0.25) &
        (vert_raw > 0.16) &
        (plan_raw > 0.025)
    )
    seed_mask = roof_seed | wall_seed
    seed_idx = np.where(seed_mask)[0]

    log.info(
        f"    V12 building-only seeds: {len(seed_idx):,} / "
        f"{int(original_building.sum()):,} initial Building"
    )

    if len(seed_idx) < 20:
        log.warning("    V12 building-only: too few strong seeds, using safe cleanup only.")
        predictions, nfix = clean_final_building_speckles_only(
            xyz, predictions, hag, vert_raw, plan_raw, log, tree_all=tree_all
        )
        fix_report["v12_safe_cleanup_only"] = int(nfix)
        return predictions, fix_report

    xy = xyz[:, :2]
    xy_min = xy.min(axis=0)
    grid = 0.75

    all_cells = np.floor((xy - xy_min) / grid).astype(np.int64)
    dims_x = int(all_cells[:, 0].max()) + 1
    dims_y = int(all_cells[:, 1].max()) + 1
    if dims_x <= 0 or dims_y <= 0:
        fix_report["v12_bad_grid"] = 0
        return predictions, fix_report

    all_keys = all_cells[:, 0] * dims_y + all_cells[:, 1]
    seed_keys = all_keys[seed_idx]
    keys_unique, inv, key_counts = np.unique(seed_keys, return_inverse=True, return_counts=True)

    # Connected components of strong building seed cells.
    comps = _component_keys_from_seed_cells(keys_unique, dims_y)
    count_by_key = {int(k): int(c) for k, c in zip(keys_unique, key_counts)}

    kept_keys = []
    rejected_keys = []
    kept_components = 0

    for comp in comps:
        comp_arr = np.asarray(comp, dtype=np.int64)
        comp_count = sum(count_by_key.get(int(k), 0) for k in comp)
        xs = comp_arr // dims_y
        ys = comp_arr - xs * dims_y
        width_m = (int(xs.max()) - int(xs.min()) + 1) * grid
        height_m = (int(ys.max()) - int(ys.min()) + 1) * grid
        max_dim = max(width_m, height_m)
        min_dim = min(width_m, height_m)

        # Keep real building bodies/roofs. Reject small canopy/speckle islands.
        keep = (
            (comp_count >= 90 and len(comp) >= 8 and max_dim >= 3.0) or
            (comp_count >= 180 and max_dim >= 2.2 and min_dim >= 1.2) or
            (comp_count >= 350)
        )

        if keep:
            kept_components += 1
            kept_keys.extend(comp)
        else:
            rejected_keys.extend(comp)

    kept_keys = np.asarray(kept_keys, dtype=np.int64)
    rejected_keys = np.asarray(rejected_keys, dtype=np.int64)

    log.info(
        f"    V12 building components: total={len(comps):,}, "
        f"kept={kept_components:,}, kept_cells={len(kept_keys):,}"
    )

    if len(kept_keys) == 0:
        log.warning("    V12 building-only: no component passed size test; keeping original predictions.")
        fix_report["v12_no_kept_component"] = 0
        return predictions, fix_report

    # Footprint = kept strong seed cells dilated slightly. Roof rescue is allowed
    # only inside this footprint, not anywhere near vegetation.
    footprint_keys = _dilate_cell_keys(kept_keys, dims_x, dims_y, radius_cells=2)
    footprint_mask = np.isin(all_keys, footprint_keys)

    kept_seed_mask = seed_mask & np.isin(all_keys, kept_keys)
    kept_seed_idx = np.where(kept_seed_mask)[0]

    if len(kept_seed_idx) == 0:
        fix_report["v12_no_kept_seed"] = 0
        return predictions, fix_report

    tree_seed_2d = cKDTree(xyz[kept_seed_idx][:, :2])

    # -----------------------------------------------------------------
    # A) Remove existing Building points that are not connected to a kept
    #    building component / footprint.
    # -----------------------------------------------------------------
    building_idx = np.where(original_building)[0]
    d_seed, _ = safe_query(tree_seed_2d, xyz[building_idx][:, :2], k=1)

    b_hag = hag[building_idx]
    b_vert = vert_raw[building_idx]
    b_plan = plan_raw[building_idx]
    b_inside = footprint_mask[building_idx]

    existing_roof_like = (b_hag > 0.90) & (b_plan > 0.050)
    existing_wall_like = (b_hag > 0.22) & (b_vert > 0.12) & (b_plan > 0.018)

    keep_existing = (
        b_inside &
        (d_seed < 2.20) &
        (existing_roof_like | existing_wall_like | (d_seed < 0.80))
    )

    remove_idx = building_idx[~keep_existing]

    if len(remove_idx) > 0:
        k = min(24, n)
        _, nn = safe_query(tree_all, xyz[remove_idx], k=k)
        if k == 1:
            nn = nn.reshape(-1, 1)

        for row, pt in enumerate(remove_idx):
            # Prefer the raw/model non-building class if available; otherwise use
            # neighbour majority. This avoids forcing everything to HighVeg.
            raw_cls = int(raw_predictions[pt])
            if raw_cls != BUILDING and raw_cls in (GROUND, LOWVEG, MIDVEG, HIGHVEG):
                predictions[pt] = np.uint8(raw_cls)
            else:
                fallback = HIGHVEG if hag[pt] > 1.2 else GROUND
                predictions[pt] = _majority_non_building_from_knn(
                    predictions, nn[row], fallback_cls=fallback
                )

    fix_report["v12_removed_detached_building"] = int(len(remove_idx))
    log.info(f"    V12 removed detached/noisy Building: {len(remove_idx):,}")

    # -----------------------------------------------------------------
    # B) Conservative roof/facade rescue: only inside kept footprint and
    #    close to kept structural seed points.
    # -----------------------------------------------------------------
    non_bldg = predictions != BUILDING
    candidate_mask = (
        non_bldg &
        footprint_mask &
        np.isin(predictions, [GROUND, LOWVEG, MIDVEG, HIGHVEG]) &
        (hag > 0.65) &
        (
            ((hag > 0.95) & (plan_raw > 0.040)) |
            ((hag > 0.25) & (vert_raw > 0.10) & (plan_raw > 0.018))
        )
    )

    cand_idx = np.where(candidate_mask)[0]
    if len(cand_idx) > 90_000:
        # Keep strongest geometry first for speed and safety.
        score = (
            np.clip(plan_raw[cand_idx], 0.0, 1.0) * 2.0 +
            np.clip(vert_raw[cand_idx], 0.0, 1.0) +
            np.clip(hag[cand_idx] / 4.0, 0.0, 1.0)
        )
        keep_order = np.argsort(score)[-90_000:]
        cand_idx = cand_idx[keep_order]

    if len(cand_idx) > 0:
        d_cand, _ = safe_query(tree_seed_2d, xyz[cand_idx][:, :2], k=1)
        cand_idx = cand_idx[d_cand < 2.20]

    rescued = []
    if len(cand_idx) > 0:
        nlist = safe_query_ball_point(tree_all, xyz[cand_idx], r=1.60)
        for pt, nb in zip(cand_idx, nlist):
            nb = np.asarray(nb, dtype=np.int64)
            if len(nb) < 8:
                continue

            nb_pred = predictions[nb]
            b_ratio = float((nb_pred == BUILDING).sum()) / float(len(nb))
            high_ratio = float((nb_pred == HIGHVEG).sum()) / float(len(nb))
            veg_ratio = float(np.isin(nb_pred, [LOWVEG, MIDVEG, HIGHVEG]).sum()) / float(len(nb))

            roof_like = (hag[pt] > 0.95 and plan_raw[pt] > 0.040 and b_ratio >= 0.10)
            wall_like = (
                hag[pt] > 0.25 and
                vert_raw[pt] > 0.12 and
                plan_raw[pt] > 0.018 and
                b_ratio >= 0.14
            )

            # Tree/canopy guard: if it is overwhelmingly vegetation and has weak
            # plane/wall geometry, do not convert it even inside the footprint.
            canopy_like = (
                high_ratio > 0.72 and
                veg_ratio > 0.82 and
                plan_raw[pt] < 0.075 and
                vert_raw[pt] < 0.22 and
                b_ratio < 0.22
            )

            if canopy_like:
                continue

            if roof_like or wall_like:
                rescued.append(int(pt))

    if rescued:
        rescued = np.asarray(rescued, dtype=np.int64)
        predictions[rescued] = BUILDING
        rescued_count = int(len(rescued))
    else:
        rescued_count = 0

    fix_report["v12_rescued_component_roof_wall"] = rescued_count
    log.info(f"    V12 rescued component roof/wall: {rescued_count:,}")

    # -----------------------------------------------------------------
    # C) One final local cleanup after rescue. This is intentionally light;
    #    component filtering already did the heavy work.
    # -----------------------------------------------------------------
    predictions, nfix = clean_final_building_speckles_only(
        xyz, predictions, hag, vert_raw, plan_raw, log, tree_all=tree_all
    )
    fix_report["v12_final_speckle_cleanup"] = int(nfix)

    return predictions, fix_report

def post_process_all(xyz, predictions, confidence, hag, vert_raw, plan_raw, log, raw_predictions=None):
    fix_report = {}

    tree_all = cKDTree(xyz)

    protected_building_mask = None

    steps = [
        ("Step 0: Elevated-ground to roof", "elevated_ground_terrace",
         lambda: fix_elevated_ground_as_roof(xyz, predictions, hag, plan_raw, log)),

        ("Step 1: Ground-on-roof propagation", "ground_on_roof",
         lambda: fix_ground_on_roofs(xyz, predictions, hag, log, tree_all=tree_all)),

        ("Step 2: Wall recovery main", "wall_recovery",
         lambda: fix_building_walls(
             xyz, predictions, hag, vert_raw, plan_raw, log, tree_all=tree_all
         )),

        ("Step 3: Salt-pepper cleanup", "salt_pepper",
         lambda: fix_salt_pepper(
             xyz, predictions, confidence, hag, vert_raw, plan_raw, log, tree_all=tree_all
         )),

        ("Step 5: Final strict wall recovery", "wall_recovery_final_strict",
         lambda: fix_building_walls_final_strict(
             xyz, predictions, hag, vert_raw, plan_raw, log, tree_all=tree_all
         )),

        ("Step 6: Wall-base strip recovery", "wall_base_recovery",
         lambda: recover_wall_base_strip(
             xyz, predictions, hag, vert_raw, plan_raw, log, tree_all=tree_all
         )),

        ("Step 6B: Attached building-base recovery", "attached_building_base_recovery",
         lambda: recover_attached_building_base_from_vegetation(
             xyz, predictions, hag, vert_raw, plan_raw, log, tree_all=tree_all
         )),

        ("Step 6C: V11 roof/facade rescue", "highveg_facade_roof_rescue",
         lambda: recover_highveg_building_facade_roof(
             xyz, predictions, hag, vert_raw, plan_raw, log, tree_all=tree_all
         )),

        ("Step 6D: V11 roof-only grow", "roof_surface_region_grow",
         lambda: expand_building_roof_surface_region_grow(
             xyz, predictions, hag, vert_raw, plan_raw, log, tree_all=tree_all
         )),

        ("Step 7: Final safe vegetation cleanup", "final_safe_veg_cleanup",
         lambda: clean_final_building_speckles_only(
             xyz, predictions, hag, vert_raw, plan_raw, log, tree_all=tree_all
         )),
    ]

    if ENABLE_HEAVY_V11_ROLLBACK:
        steps.insert(9, (
            "Step 6J: V11 final false-building cleanup",
            "strict_canopy_island_rollback",
            lambda: strict_canopy_island_building_rollback(
                xyz, predictions, raw_predictions, hag, vert_raw, plan_raw, confidence, log, tree_all=tree_all
            ),
        ))
    else:
        log.info("    Step 6J disabled in V13: avoids high-RAM crash before output write.")

    log.info("    POST-PROCESSING STEPS LOADED:")
    for i, (label, key, _) in enumerate(steps):
        log.info(f"      {i}: {label} | key={key}")

    for label, key, fn in steps:
        log.info(f"    {label}...")
        t0 = time.time()

        try:
            predictions, n = fn()
        except Exception as e:
            log.error(f"      SKIPPED - {label} failed: {e}")
            log.debug(traceback.format_exc())
            n = 0

        fix_report[key] = int(n)
        log.info(f"      -> {n:,} fixed ({time.time() - t0:.1f}s)")

        # Lock only high-confidence structural building points.
        # Do NOT lock broad rescue/region-grow outputs before final cleanup,
        # otherwise false vegetation-to-building conversions cannot be corrected.
        if key in (
            "wall_recovery",
            "wall_recovery_final_strict",
            "wall_base_recovery",
            "attached_building_base_recovery",
        ):
            new_guard = _build_structural_building_guard(
                predictions,
                hag,
                vert_raw,
                plan_raw
            )

            if protected_building_mask is None:
                protected_building_mask = new_guard
            else:
                protected_building_mask |= new_guard

            log.info(
                f"      Structural building guard updated after {key}: "
                f"{int(protected_building_mask.sum()):,} protected building points"
            )

    # Final safety restore.
    # Only strict structural points are restored. Broad roof grow outputs are
    # intentionally not protected because they may include canopy.
    if protected_building_mask is not None:
        restore_mask = protected_building_mask & (predictions != BUILDING)
        restore_count = int(restore_mask.sum())

        if restore_count > 0:
            predictions[restore_mask] = BUILDING

        fix_report["structural_building_guard_restore"] = restore_count
        log.info(
            f"    Structural building guard restored: "
            f"{restore_count:,} building points"
        )

    return predictions, fix_report


# =====================================================================
# OUTPUT QUALITY CHECK
# =====================================================================

def check_output_quality(asprs_classes, confidence, n_total, log):
    report = {"warnings": [], "pass": True}
    unique, counts = np.unique(asprs_classes, return_counts=True)
    pcts = {int(cls): float(cnt / n_total * 100) for cls, cnt in zip(unique, counts)}
    # Current Naksha GUI standard output before PTC remap: Ground=1, Building=5.
    ground_pct = pcts.get(1, 0.0)
    building_pct = pcts.get(5, 0.0)
    low_conf_pct = float((confidence < CONFIDENCE_FLAG_THRESH).sum() / n_total * 100)
    report["class_pct"]       = pcts
    report["low_conf_pct"]    = round(low_conf_pct, 2)
    report["mean_confidence"] = round(float(confidence.mean()), 4)
    if ground_pct < MIN_GROUND_PCT:
        msg = f"Ground only {ground_pct:.1f}% - CSF may have failed"
        report["warnings"].append(msg)
        report["pass"] = False
        log.warning(f"  QC: {msg}")
    if building_pct > MAX_BUILDING_PCT:
        msg = f"Building {building_pct:.1f}% - possible over-prediction"
        report["warnings"].append(msg)
        report["pass"] = False
        log.warning(f"  QC: {msg}")
    if low_conf_pct > 15.0:
        msg = f"{low_conf_pct:.1f}% points below confidence {CONFIDENCE_FLAG_THRESH}"
        report["warnings"].append(msg)
        log.warning(f"  QC: {msg}")
    if report["pass"]:
        log.info("  QC: PASSED")
    else:
        log.warning("  QC: FAILED - review warnings above")
    return report


# =====================================================================
# JSON REPORT WRITER
# =====================================================================

def write_json_report(output_dir, filename, validation, fix_report,
                      quality, elapsed, n_total, log):
    report = {
        "file":       filename,
        "timestamp":  datetime.now().isoformat(),
        "runtime_s":  round(elapsed, 1),
        "pts_per_sec": round(n_total / max(elapsed, 1), 0),
        "validation": validation,
        "post_processing_fixes": fix_report,
        "total_fixes": int(sum(fix_report.values())),
        "quality": quality,
    }
    report_dir  = output_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / (Path(filename).stem + "_report.json")
    tmp_path    = report_dir / (Path(filename).stem + "_report.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
        shutil.move(str(tmp_path), str(report_path))
        log.info(f"  Report: {report_path}")
    except Exception as e:
        log.warning(f"  Report write failed: {e}")


# =====================================================================
# TILE INDEX CACHE
# =====================================================================

def build_or_load_tile_indices(xyz, tile_specs, cache_dir, stem, cache_key, log, use_cache=True):
    if use_cache:
        tile_index_data = load_tile_index_cache(cache_dir, stem, cache_key, log)
        if tile_index_data is not None:
            log.info("    Tile index precompute SKIPPED - loaded from cache.")
            return tile_index_data
    else:
        log.info("    FENCE FRESH MODE: tile index cache disabled; rebuilding tile indices.")

    log.info("    Precomputing tile indices...")
    t0 = time.time()
    tile_index_data = []
    for xs, ys in tile_specs:
        mask_expand = ((xyz[:, 0] >= xs - TILE_MARGIN) &
                       (xyz[:, 0] <  xs + TILE_SIZE + TILE_MARGIN) &
                       (xyz[:, 1] >= ys - TILE_MARGIN) &
                       (xyz[:, 1] <  ys + TILE_SIZE + TILE_MARGIN))
        idx_expand = np.where(mask_expand)[0]
        if len(idx_expand) < 200:
            tile_index_data.append(None)
            continue
        tile_xyz_local = xyz[idx_expand]
        mask_core = ((tile_xyz_local[:, 0] >= xs) & (tile_xyz_local[:, 0] < xs + TILE_SIZE) &
                     (tile_xyz_local[:, 1] >= ys) & (tile_xyz_local[:, 1] < ys + TILE_SIZE))
        core_local_idx = np.where(mask_core)[0]
        if len(core_local_idx) == 0:
            tile_index_data.append(None)
            continue
        tile_index_data.append((idx_expand, core_local_idx))
    log.info(f"    Done: {time.time() - t0:.1f}s")
    if use_cache:
        save_tile_index_cache(cache_dir, stem, cache_key, tile_index_data, log)
    return tile_index_data


# =====================================================================
# MAIN CLASSIFICATION FUNCTION
# =====================================================================

# =====================================================================
# FENCE MODE HELPER
# Runs Advanced AI on fence + context subset, then writes back only
# exact inside-fence target points into the full original LAZ.
# =====================================================================



def _write_full_laz_with_updated_classes_streaming(
    input_path,
    output_path,
    target_indices,
    replacement_classes,
    log,
    chunk_size=1_000_000,
):
    """
    Safely write the final full LAZ/LAS after fence classification.

    Why this exists:
    - full_las.write(...) can spike RAM at the final step, especially for LAZ.
    - On large files this can close the app without a Python traceback.
    - This function streams the original file chunk-by-chunk and changes only
      the requested fence target point classifications.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)

    target_indices = np.asarray(target_indices, dtype=np.int64)
    replacement_classes = np.asarray(replacement_classes, dtype=np.uint8)

    if len(target_indices) != len(replacement_classes):
        raise ValueError(
            f"target/replacement size mismatch: "
            f"{len(target_indices)} indices vs {len(replacement_classes)} classes"
        )

    if len(target_indices) == 0:
        raise ValueError("No target indices supplied for streaming write.")

    order = np.argsort(target_indices)
    target_indices = target_indices[order]
    replacement_classes = replacement_classes[order]

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Write final full file to local temp first. This avoids network/NAS handle
    # drops and avoids corrupting the requested output path if the process stops.
    local_final_dir = Path(tempfile.gettempdir()) / "NakshaAdvancedAI" / "final_write"
    local_final_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = local_final_dir / (
        f"{output_path.stem}.{os.getpid()}.{threading.get_ident()}.tmp{output_path.suffix}"
    )

    if tmp_path.exists():
        try:
            tmp_path.unlink()
        except Exception:
            pass

    changed = 0
    point_start = 0
    ptr = 0
    n_targets = int(len(target_indices))

    log.info("  FENCE MODE: streaming full-file write-back enabled.")
    log.info(f"    Streaming chunk size : {int(chunk_size):,}")
    log.info(f"    Local temp output    : {tmp_path}")

    with laspy.open(str(input_path), mode="r") as reader:

        source_header = reader.header
        total_points = int(source_header.point_count)

        max_replacement_class = (
            int(np.max(replacement_classes))
            if len(replacement_classes) > 0
            else 0
        )

        source_pf = int(source_header.point_format.id)

        needs_extended = (
            source_pf <= 5
            and max_replacement_class > 31
        )

        if needs_extended:
            from gui.AI.common.ptc_mapping import _make_extended_header

            header = _make_extended_header(
                source_header
            )

            log.info(
                f"  PTC LAS 1.4 UPGRADE: "
                f"point format {source_pf} -> "
                f"{header.point_format.id}"
            )
        else:
            header = source_header.copy()

        with laspy.open(
            str(tmp_path),
            mode="w",
            header=header,
            do_compress=(tmp_path.suffix.lower() == ".laz"),
        ) as writer:

            for points in reader.chunk_iterator(int(chunk_size)):

                n_chunk = len(points)
                point_end = point_start + n_chunk

                # Convert the point record BEFORE writing PTC codes >31.
                if needs_extended:
                    out_points = laspy.PackedPointRecord.from_point_record(
                        points,
                        header.point_format,
                    )
                else:
                    out_points = points

                left = np.searchsorted(
                    target_indices,
                    point_start,
                    side="left"
                )

                right = np.searchsorted(
                    target_indices,
                    point_end,
                    side="left"
                )

                if right > left:
                    local_pos = (
                        target_indices[left:right]
                        - point_start
                    )

                    out_points.classification[local_pos] = (
                        replacement_classes[left:right]
                    )

                    changed += int(right - left)
                    ptr = right

                writer.write_points(out_points)
                point_start = point_end

    if changed != n_targets:
        raise RuntimeError(
            f"Streaming write changed {changed:,}/{n_targets:,} target points only."
        )

    if not tmp_path.exists() or tmp_path.stat().st_size <= 0:
        raise FileNotFoundError(f"Streaming temp output was not created: {tmp_path}")

    # Copy/move from local temp to the requested output path. shutil.move across
    # drives uses copy+delete internally, without loading the whole file in RAM.
    if output_path.exists():
        try:
            output_path.unlink()
        except Exception:
            pass

    shutil.move(str(tmp_path), str(output_path))

    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise FileNotFoundError(f"Final output missing after streaming move: {output_path}")

    log.info("  FENCE MODE: streaming write-back finished.")
    log.info(f"    Changed target points: {changed:,}")
    log.info(f"    Final output file    : {output_path}")

    return True


def compute_fence_parent_context_hag(
    xyz_full: np.ndarray,
    full_classes: np.ndarray,
    run_indices: np.ndarray,
    base_min_x: float,
    base_max_x: float,
    base_min_y: float,
    base_max_y: float,
    selected_margin: float,
    log: logging.Logger,
) -> np.ndarray:
    """
    Fence/SNT HAG fix.

    Do not estimate ground only from the temporary fence subset. Some fence boxes
    contain roofs/vegetation but little real ground, so CSF can return zero ground
    points and fallback HAG becomes unstable. This helper builds a wider parent
    ground support surface from the original LAZ around the fence, then computes
    HAG for the exact run subset.

    Direct LAZ mode is untouched. This is used only by _classify_laz_fence_subset.
    """
    run_indices = np.asarray(run_indices, dtype=np.int64)
    run_xyz = xyz_full[run_indices]

    n_full = len(xyz_full)
    if n_full == 0 or len(run_indices) == 0:
        return np.zeros(len(run_indices), dtype=np.float32)

    # Wider than the AI context. This is the key fix: ground support must come
    # from the parent file neighbourhood, not only from the small fence temp LAZ.
    width = max(1.0, float(base_max_x - base_min_x))
    height = max(1.0, float(base_max_y - base_min_y))
    parent_margin = max(30.0, float(selected_margin) + 25.0, 0.75 * max(width, height))

    px_min = base_min_x - parent_margin
    px_max = base_max_x + parent_margin
    py_min = base_min_y - parent_margin
    py_max = base_max_y + parent_margin

    parent_mask = (
        (xyz_full[:, 0] >= px_min) &
        (xyz_full[:, 0] <= px_max) &
        (xyz_full[:, 1] >= py_min) &
        (xyz_full[:, 1] <= py_max)
    )
    parent_idx = np.where(parent_mask)[0].astype(np.int64)

    if len(parent_idx) < max(1024, len(run_indices) // 20):
        # If the widened bbox is unexpectedly sparse, fall back to run subset.
        parent_idx = run_indices

    parent_xyz = xyz_full[parent_idx]
    parent_cls = np.asarray(full_classes[parent_idx], dtype=np.uint8)

    log.info(
        "    Fence parent-HAG support bbox: "
        f"X[{px_min:.2f}, {px_max:.2f}] Y[{py_min:.2f}, {py_max:.2f}] | "
        f"parent support points={len(parent_idx):,} | margin={parent_margin:.1f}m"
    )

    # First preference: use existing parent-file Ground class as ground support.
    # In Naksha GUI output mapping, ASPRS/GUI class 1 is Ground. Inside the fence
    # may have been reset to class 0, but parent context still provides stable
    # ground outside/around the selected fence.
    ground_mask = parent_cls == 1
    ground_pts = parent_xyz[ground_mask]
    ground_source = "parent class-1 ground"

    # If class-1 support is insufficient, try CSF on the wider parent context.
    if len(ground_pts) < 512 and HAS_CSF:
        try:
            csf_xyz = parent_xyz
            csf_local_idx = None
            if len(csf_xyz) > 900_000:
                rng = np.random.default_rng(12345)
                csf_local_idx = rng.choice(len(csf_xyz), 900_000, replace=False)
                csf_xyz = csf_xyz[csf_local_idx]

            csf = CSF.CSF()
            csf.params.bSloopSmooth = False
            csf.params.cloth_resolution = 0.5
            csf.params.rigidness = 3
            csf.params.time_step = 0.65
            csf.params.class_threshold = 0.5
            if hasattr(csf.params, "interations"):
                csf.params.interations = 500
            elif hasattr(csf.params, "iterations"):
                csf.params.iterations = 500

            csf.setPointCloud(np.asarray(csf_xyz, dtype=np.float64))
            ground_idx = CSF.VecInt()
            non_ground_idx = CSF.VecInt()
            csf.do_filtering(ground_idx, non_ground_idx)
            ground_idx = np.asarray(ground_idx, dtype=np.int64)

            if csf_local_idx is not None and len(ground_idx) > 0:
                ground_pts_csf = csf_xyz[ground_idx]
            else:
                ground_pts_csf = csf_xyz[ground_idx]

            if len(ground_pts_csf) >= 512:
                ground_pts = ground_pts_csf
                ground_source = "parent-context CSF ground"
        except Exception as e:
            log.warning(f"    Fence parent-HAG CSF support failed: {e}")

    # Last fallback: grid-low surface from the wider parent context.
    # This is much safer than using only the fence temp subset because the parent
    # context includes road/yard/open ground around the structure.
    if len(ground_pts) < 512:
        try:
            finite = np.isfinite(parent_xyz).all(axis=1)
            px = parent_xyz[finite]
            if len(px) < 3:
                raise RuntimeError("not enough finite parent points")

            grid = 1.5
            xy_min = px[:, :2].min(axis=0)
            cell = np.floor((px[:, :2] - xy_min) / grid).astype(np.int64)
            key = cell[:, 0] * 10_000_000 + cell[:, 1]
            order = np.lexsort((px[:, 2], key))
            key_sorted = key[order]
            idx_sorted = order

            selected = []
            start = 0
            while start < len(order):
                end = start + 1
                while end < len(order) and key_sorted[end] == key_sorted[start]:
                    end += 1
                count = end - start
                take = start + max(0, min(count - 1, int(count * 0.10)))
                selected.append(idx_sorted[take])
                start = end

            selected = np.asarray(selected, dtype=np.int64)
            if len(selected) < 512:
                z_cut = np.nanpercentile(px[:, 2], 35.0)
                selected = np.where(px[:, 2] <= z_cut)[0]

            ground_pts = px[selected]
            ground_source = "parent grid-low fallback ground"
        except Exception as e:
            log.warning(f"    Fence parent-HAG grid-low fallback failed: {e}")
            baseline = float(np.nanpercentile(run_xyz[:, 2], 10.0))
            return np.clip(run_xyz[:, 2] - baseline, -2.0, 80.0).astype(np.float32)

    if len(ground_pts) > 700_000:
        rng = np.random.default_rng(12345)
        keep = rng.choice(len(ground_pts), 700_000, replace=False)
        ground_pts = ground_pts[keep]

    tree = cKDTree(ground_pts[:, :2])
    k = min(8, len(ground_pts))
    d, ii = safe_query(tree, run_xyz[:, :2], k=k)

    if k == 1:
        d = d.reshape(-1, 1)
        ii = ii.reshape(-1, 1)

    w = 1.0 / (d + 0.25)
    w_sum = w.sum(axis=1, keepdims=True)
    w = np.divide(w, w_sum, out=np.zeros_like(w), where=w_sum > 0)

    ground_z = np.sum(ground_pts[ii, 2] * w, axis=1)
    hag = run_xyz[:, 2] - ground_z
    hag = np.nan_to_num(hag, nan=0.0, posinf=80.0, neginf=-2.0)
    hag = np.clip(hag, -2.0, 80.0).astype(np.float32)

    log.info(
        "    Fence parent-HAG enabled: "
        f"source={ground_source} | ground_points={len(ground_pts):,} | "
        f"run_points={len(run_indices):,} | "
        f"hag p02/p50/p98="
        f"{np.percentile(hag, 2):.2f}/{np.percentile(hag, 50):.2f}/{np.percentile(hag, 98):.2f}"
    )

    return hag

def _safe_fence_rollback_classes(original_classes):
    """Return GUI-safe rollback classes for Advanced fence post-processing.

    Fence inference writes GUI classes 1..5. Original class 0 means
    unclassified/unknown and must never be restored into the classified fence
    output. Likewise, if a guard decides to rollback an original Building row,
    Ground is the conservative fallback rather than leaving Building in place.

    Known original Ground/Vegetation GUI classes 1..4 are preserved. All other
    values fall back to GUI Ground (1), keeping the fence result in one class
    space and preventing class-0 leakage.
    """
    src = np.asarray(original_classes, dtype=np.uint8)
    out = np.ones(src.shape, dtype=np.uint8)
    known_non_building = np.isin(src, [1, 2, 3, 4])
    out[known_non_building] = src[known_non_building]
    return out


def _classify_laz_fence_subset(input_path, output_path, output_dir,
                               model, device, feat_mean, feat_std, log,
                               class_mapping, target_indices, target_classes=None,
                               power_mapping=None, enable_power_lines=False,
                               advanced_config=None):
    input_path = Path(input_path)
    output_path = Path(output_path)
    output_dir = Path(output_dir)

    target_indices = np.asarray(target_indices, dtype=np.int64)
    target_indices = np.unique(target_indices)

    try:
        full_las = laspy.read(str(input_path))
    except Exception as e:
        log.error(f"  FENCE MODE: failed to read full input file: {e}")
        return False

    n_total = int(full_las.header.point_count)

    target_indices = target_indices[
        (target_indices >= 0) & (target_indices < n_total)
    ]

    if len(target_indices) == 0:
        log.error("  FENCE MODE: target_indices is empty after validation.")
        return False

    xyz_full = np.column_stack([
        np.array(full_las.x, dtype=np.float64),
        np.array(full_las.y, dtype=np.float64),
        np.array(full_las.z, dtype=np.float64),
    ])

    target_xyz = xyz_full[target_indices]

    base_min_x = float(np.min(target_xyz[:, 0]))
    base_max_x = float(np.max(target_xyz[:, 0]))
    base_min_y = float(np.min(target_xyz[:, 1]))
    base_max_y = float(np.max(target_xyz[:, 1]))

    selected_margin = float(FENCE_CONTEXT_MARGIN)
    candidate_margins = []
    for m in (FENCE_CONTEXT_MARGIN, 6.0, 4.0, MIN_FENCE_CONTEXT_MARGIN):
        m = float(max(MIN_FENCE_CONTEXT_MARGIN, min(float(m), float(FENCE_CONTEXT_MARGIN))))
        if m not in candidate_margins:
            candidate_margins.append(m)

    run_indices = None
    min_x = max_x = min_y = max_y = None
    for margin in candidate_margins:
        tx_min = base_min_x - margin
        tx_max = base_max_x + margin
        ty_min = base_min_y - margin
        ty_max = base_max_y + margin
        run_mask = (
            (xyz_full[:, 0] >= tx_min) &
            (xyz_full[:, 0] <= tx_max) &
            (xyz_full[:, 1] >= ty_min) &
            (xyz_full[:, 1] <= ty_max)
        )
        idx = np.where(run_mask)[0].astype(np.int64)
        if run_indices is None or len(idx) < len(run_indices):
            run_indices = idx
            selected_margin = margin
            min_x, max_x, min_y, max_y = tx_min, tx_max, ty_min, ty_max
        if len(idx) <= MAX_FENCE_RUN_POINTS or margin <= MIN_FENCE_CONTEXT_MARGIN:
            run_indices = idx
            selected_margin = margin
            min_x, max_x, min_y, max_y = tx_min, tx_max, ty_min, ty_max
            break

    if run_indices is None or len(run_indices) == 0:
        log.error("  FENCE MODE: no run points found in context area.")
        return False

    if len(run_indices) > MAX_FENCE_RUN_POINTS:
        log.warning(
            f"  FENCE MODE: run subset is still large ({len(run_indices):,}) even after "
            f"adaptive margin {selected_margin:.1f}m. Post-processing remains target-only/safe."
        )
    elif selected_margin < FENCE_CONTEXT_MARGIN:
        log.info(
            f"  FENCE MODE: adaptive context margin reduced from "
            f"{FENCE_CONTEXT_MARGIN:.1f}m to {selected_margin:.1f}m for speed/safety."
        )

    # target_indices must be inside run_indices because bbox was made from target_xyz
    target_pos_in_run = np.searchsorted(run_indices, target_indices)

    valid_pos = (
        (target_pos_in_run >= 0) &
        (target_pos_in_run < len(run_indices)) &
        (run_indices[target_pos_in_run] == target_indices)
    )

    if not np.all(valid_pos):
        log.error("  FENCE MODE: target/run index mapping failed.")
        return False

    log.info("")
    log.info("  FENCE MODE ENABLED")
    log.info(f"    Exact target points : {len(target_indices):,} / {n_total:,}")
    log.info(f"    Context margin      : {selected_margin:.1f} m")
    log.info(f"    Run subset points   : {len(run_indices):,} / {n_total:,}")
    log.info(
        f"    Context bbox        : "
        f"X[{min_x:.2f}, {max_x:.2f}] Y[{min_y:.2f}, {max_y:.2f}]"
    )
    bbox_key = (
        f"{input_path.stem}_"
        f"{len(target_indices)}_"
        f"{min_x:.2f}_{max_x:.2f}_"
        f"{min_y:.2f}_{max_y:.2f}_"
        f"m{selected_margin:.1f}"
    )

    run_token = hashlib.md5(bbox_key.encode("utf-8")).hexdigest()[:16]

    # Keep heavy fence temp files local. Writing the temp LAZ on the NAS path was
    # failing with FileNotFoundError when the network folder/handle dropped mid-run.
    local_temp_base = Path(tempfile.gettempdir()) / "NakshaAdvancedAI" / FENCE_TEMP_FOLDER_NAME
    tmp_root = local_temp_base / run_token
    tmp_root.mkdir(parents=True, exist_ok=True)

    tmp_input = tmp_root / f"{input_path.stem}_fence_work{input_path.suffix}"
    tmp_output_dir = tmp_root / "classified"
    tmp_output_dir.mkdir(parents=True, exist_ok=True)
    tmp_output = tmp_output_dir / f"{input_path.stem}_fence_work_classified{input_path.suffix}"

    try:
        sub_header = full_las.header.copy()
        sub_las = laspy.LasData(sub_header)
        sub_las.points = full_las.points[run_indices]
        sub_las.update_header()
        sub_las.write(str(tmp_input))
    except Exception as e:
        log.error(f"  FENCE MODE: failed to write temporary fence input: {e}")
        return False

    log.info(f"    Temporary fence input written: {tmp_input}")

    original_target_classes_parent = np.array(
        full_las.classification[target_indices], dtype=np.uint8, copy=True
    )

    try:
        full_classes_for_hag = np.array(full_las.classification, dtype=np.uint8)
        precomputed_hag_for_run = compute_fence_parent_context_hag(
            xyz_full=xyz_full,
            full_classes=full_classes_for_hag,
            run_indices=run_indices,
            base_min_x=base_min_x,
            base_max_x=base_max_x,
            base_min_y=base_min_y,
            base_max_y=base_max_y,
            selected_margin=selected_margin,
            log=log,
        )
    except Exception as e:
        log.warning(
            f"    Fence parent-HAG precompute failed; falling back to normal HAG: {e}"
        )
        precomputed_hag_for_run = None

    # Release the parent 10M-point LasData/XYZ before PointNet++ starts.
    try:
        del sub_las
    except Exception:
        pass
    try:
        del full_classes_for_hag
    except Exception:
        pass
    del full_las, xyz_full, target_xyz
    gc.collect()
    log.info(
        "    Fence parent cloud released before model inference; "
        "only exact target classes + subset indices retained."
    )

    try:
        log.info(
            f"    Fence post-processing restricted to exact target points: "
            f"{len(target_pos_in_run):,}"
        )

        ok = classify_laz(
            input_path=tmp_input,
            output_path=tmp_output,
            output_dir=tmp_output_dir,
            model=model,
            device=device,
            feat_mean=feat_mean,
            feat_std=feat_std,
            log=log,
            class_mapping=class_mapping,
            target_indices=None,
            target_classes=target_classes,
            postprocess_indices=target_pos_in_run,
            precomputed_hag=precomputed_hag_for_run,
            power_mapping=power_mapping,
            enable_power_lines=enable_power_lines,
            advanced_config=advanced_config,
        )

    except Exception as e:
        log.error(f"  FENCE MODE: subset classification failed: {e}")
        return False
    if not tmp_output.exists():
        log.error(f"  FENCE MODE: temporary classified output missing: {tmp_output}")
        return False

    try:
        sub_result = laspy.read(str(tmp_output))
        sub_classes = np.array(sub_result.classification, dtype=np.uint8)

        if len(sub_classes) != len(run_indices):
            log.error(
                f"  FENCE MODE: subset output point mismatch. "
                f"Expected {len(run_indices):,}, got {len(sub_classes):,}"
            )
            return False
        # ------------------------------------------------------------
        # FENCE-ONLY FINAL LEAKAGE GUARD V2
        # Removes road/ground/linear/vegetation leakage classified as Building.
        # This runs only in fence workflow, so direct LAZ output is untouched.
        # ------------------------------------------------------------
        try:
            sub_xyz = np.column_stack([
                np.array(sub_result.x, dtype=np.float64),
                np.array(sub_result.y, dtype=np.float64),
                np.array(sub_result.z, dtype=np.float64),
            ])

            original_target_classes = original_target_classes_parent.copy()

            local_target_pos = np.asarray(target_pos_in_run, dtype=np.int64)
            pred_target_classes = sub_classes[local_target_pos]

            building_target_mask = pred_target_classes == 5
            building_local_pos = local_target_pos[building_target_mask]

            if len(building_local_pos) > 0:
                tree_2d = cKDTree(sub_xyz[:, :2])

                rollback_local = []

                for lp in building_local_pos:
                    nb = tree_2d.query_ball_point(sub_xyz[lp, :2], r=2.20)
                    nb = np.asarray(nb, dtype=np.int64)

                    if len(nb) < 10:
                        rollback_local.append(lp)
                        continue

                    nb_cls = sub_classes[nb]

                    b_count = int((nb_cls == 5).sum())
                    g_count = int((nb_cls == 1).sum())
                    low_count = int((nb_cls == 2).sum())
                    mid_count = int((nb_cls == 3).sum())
                    high_count = int((nb_cls == 4).sum())

                    non_build_count = len(nb) - b_count
                    veg_count = low_count + mid_count + high_count

                    b_ratio = b_count / float(len(nb))
                    ground_ratio = g_count / float(len(nb))
                    veg_ratio = veg_count / float(len(nb))
                    non_build_ratio = non_build_count / float(len(nb))

                    nb_xy = sub_xyz[nb, :2]
                    nb_z = sub_xyz[nb, 2]

                    z_span = float(np.max(nb_z) - np.min(nb_z))

                    # Height of this point vs the building cluster in the neighbourhood.
                    # Used to detect ground-level / courtyard surfaces inside dense
                    # building areas where b_ratio alone would otherwise grant protection.
                    pt_z = float(sub_xyz[lp, 2])
                    bldg_nb_z = nb_z[nb_cls == 5]
                    mean_bldg_z = float(np.mean(bldg_nb_z)) if len(bldg_nb_z) >= 5 else pt_z
                    z_below_buildings = max(0.0, mean_bldg_z - pt_z)

                    # Early rollback: flat surface clearly below the building cluster.
                    # A roof or facade cannot be flat AND multiple metres below the
                    # surrounding building elevation. Courtyards, paved aprons, and
                    # ground-level roads inside building complexes are caught here
                    # before the density-protection check can shield them.
                    if z_below_buildings > 2.5 and z_span < 1.00:
                        rollback_local.append(lp)
                        continue

                    xy_centered = nb_xy - np.mean(nb_xy, axis=0)

                    try:
                        cov = np.cov(xy_centered.T)
                        eigvals = np.linalg.eigvalsh(cov)
                        eigvals = np.sort(eigvals)[::-1]
                        line_ratio = float(eigvals[1] / (eigvals[0] + 1e-9))
                    except Exception:
                        line_ratio = 1.0

                    # Very strong roof/wall protection.
                    # Do not rollback actual building body.
                    is_dense_building = (
                        (b_ratio >= 0.72) and
                        (b_count >= 35) and
                        (z_span >= 0.35)
                    )

                    is_supported_building = (
                        (b_ratio >= 0.58) and
                        (b_count >= 28) and
                        (non_build_ratio < 0.42)
                    )

                    if is_dense_building or is_supported_building:
                        continue

                    # Road / ground / flat apron leakage.
                    is_flat_ground_like = (
                        (z_span < 0.70) and
                        (
                            (ground_ratio >= 0.22) or
                            (non_build_ratio >= 0.50)
                        ) and
                        (b_ratio < 0.62)
                    )

                    # Thin road edge / powerline / long strip leakage.
                    is_thin_linear_leakage = (
                        (line_ratio < 0.120) and
                        (z_span < 1.20) and
                        (b_ratio < 0.70)
                    )

                    # Weak isolated building fragments.
                    is_weak_building = (
                        (b_count < 18) or
                        (b_ratio < 0.34)
                    )

                    # Vegetation/canopy leakage near building.
                    is_veg_leakage = (
                        (veg_ratio >= 0.30) and
                        (b_ratio < 0.58) and
                        (z_span < 2.20)
                    )

                    # Mixed class noise around building edge.
                    is_mixed_edge_leakage = (
                        (non_build_ratio >= 0.46) and
                        (b_ratio < 0.60) and
                        (z_span < 1.40)
                    )

                    # Courtyard / paved surface at moderate depth below buildings.
                    # Catches cases where b_ratio is not high enough for early rollback
                    # but the point is still clearly below roof level and flat.
                    is_courtyard_flat = (
                        (z_below_buildings > 1.5) and
                        (z_span < 0.80) and
                        (b_ratio < 0.75)
                    )

                    if (
                        is_flat_ground_like or
                        is_thin_linear_leakage or
                        is_weak_building or
                        is_veg_leakage or
                        is_mixed_edge_leakage or
                        is_courtyard_flat
                    ):
                        rollback_local.append(lp)

                if rollback_local:
                    rollback_local = np.asarray(rollback_local, dtype=np.int64)

                    local_to_target = {
                        int(local_pos): i
                        for i, local_pos in enumerate(local_target_pos)
                    }

                    rollback_target_rows = [
                        local_to_target[int(lp)]
                        for lp in rollback_local
                        if int(lp) in local_to_target
                    ]

                    if rollback_target_rows:
                        rollback_target_rows = np.asarray(
                            rollback_target_rows,
                            dtype=np.int64,
                        )

                        safe_original = _safe_fence_rollback_classes(
                            original_target_classes[rollback_target_rows]
                        )

                        sub_classes[
                            local_target_pos[rollback_target_rows]
                        ] = safe_original

                        log.info(
                            "  FENCE-ONLY FINAL LEAKAGE GUARD V2: "
                            f"rolled back {len(rollback_target_rows):,} road/ground/linear/veg/courtyard Building leaks"
                        )

        except Exception as guard_exc:
            log.warning(f"  FENCE-ONLY FINAL LEAKAGE GUARD V2 skipped: {guard_exc}")

        # ------------------------------------------------------------
        # FENCE-ONLY DETACHED / LINEAR BUILDING COMPONENT GUARD
        # ------------------------------------------------------------
        # The human-finish pass can recover roofs/walls well, but if the fence
        # polygon contains long road/vegetation strips it may also create thin
        # magenta Building streaks.  This final guard works only on exact
        # selected fence rows and rolls back detached/linear Building components
        # while keeping compact roof/body components.  Direct LAZ is untouched.
        try:
            local_target_pos = np.asarray(target_pos_in_run, dtype=np.int64)
            target_classes_now = sub_classes[local_target_pos]
            target_building_rows = np.where(target_classes_now == 5)[0]

            component_rollback_rows = []

            if len(target_building_rows) > 0:
                building_local_pos = local_target_pos[target_building_rows]
                building_xy = sub_xyz[building_local_pos, :2]
                building_z = sub_xyz[building_local_pos, 2]

                tree_building = cKDTree(building_xy)
                pairs = tree_building.query_pairs(r=1.45, output_type='ndarray')

                parent = np.arange(len(building_local_pos), dtype=np.int32)
                size = np.ones(len(building_local_pos), dtype=np.int32)

                def _find(a):
                    while parent[a] != a:
                        parent[a] = parent[parent[a]]
                        a = parent[a]
                    return a

                def _union(a, b):
                    ra = _find(int(a))
                    rb = _find(int(b))
                    if ra == rb:
                        return
                    if size[ra] < size[rb]:
                        ra, rb = rb, ra
                    parent[rb] = ra
                    size[ra] += size[rb]

                for a, b in pairs:
                    _union(a, b)

                roots = np.array([_find(i) for i in range(len(parent))], dtype=np.int32)
                uniq_roots = np.unique(roots)

                for root in uniq_roots:
                    comp_local_rows = np.where(roots == root)[0]
                    comp_target_rows = target_building_rows[comp_local_rows]
                    n_comp = int(len(comp_local_rows))

                    pts_xy = building_xy[comp_local_rows]
                    pts_z = building_z[comp_local_rows]

                    x_span = float(np.max(pts_xy[:, 0]) - np.min(pts_xy[:, 0])) if n_comp else 0.0
                    y_span = float(np.max(pts_xy[:, 1]) - np.min(pts_xy[:, 1])) if n_comp else 0.0
                    max_span = max(x_span, y_span)
                    min_span = min(x_span, y_span)
                    z_span = float(np.max(pts_z) - np.min(pts_z)) if n_comp else 0.0

                    if n_comp >= 3:
                        centered = pts_xy - np.mean(pts_xy, axis=0)
                        try:
                            cov = np.cov(centered.T)
                            eig = np.sort(np.linalg.eigvalsh(cov))[::-1]
                            line_ratio = float(eig[1] / (eig[0] + 1e-9))
                        except Exception:
                            line_ratio = 1.0
                    else:
                        line_ratio = 0.0

                    bbox_area = max(0.25, x_span * y_span)
                    density_2d = n_comp / bbox_area

                    # True buildings/roofs form compact 2D masses.
                    compact_roof_body = (
                        (n_comp >= 240) and
                        (max_span >= 1.50) and
                        (min_span >= 0.85) and
                        (line_ratio >= 0.030)
                    )

                    # Tall facade/wall components can be narrower, but they must be
                    # reasonably large and vertically expressive.
                    tall_wall_body = (
                        (n_comp >= 180) and
                        (max_span >= 1.20) and
                        (z_span >= 1.20) and
                        (line_ratio >= 0.018)
                    )

                    dense_small_roof = (
                        (n_comp >= 120) and
                        (max_span >= 1.00) and
                        (min_span >= 0.55) and
                        (density_2d >= 12.0) and
                        (line_ratio >= 0.025)
                    )

                    keep_component = compact_roof_body or tall_wall_body or dense_small_roof

                    # Rollback rules for the false magenta streaks seen after
                    # human-finish mode: tiny islands, thin linear strips, and
                    # low-height flat components.
                    thin_linear_strip = (
                        (max_span >= 2.80) and
                        (min_span < 0.70) and
                        (line_ratio < 0.020) and
                        (z_span < 2.00)
                    )

                    tiny_or_weak = (
                        (n_comp < 90) or
                        ((n_comp < 180) and (line_ratio < 0.020))
                    )

                    flat_ground_like_component = (
                        (z_span < 0.55) and
                        (line_ratio < 0.045) and
                        (n_comp < 450)
                    )

                    if (not keep_component) or thin_linear_strip or tiny_or_weak or flat_ground_like_component:
                        component_rollback_rows.extend(comp_target_rows.tolist())

            if component_rollback_rows:
                component_rollback_rows = np.asarray(component_rollback_rows, dtype=np.int64)
                safe_original = _safe_fence_rollback_classes(
                    original_target_classes[component_rollback_rows]
                )
                sub_classes[local_target_pos[component_rollback_rows]] = safe_original
                log.info(
                    "  FENCE COMPONENT GUARD: "
                    f"rolled back {len(component_rollback_rows):,} detached/linear Building component points"
                )
            else:
                log.info("  FENCE COMPONENT GUARD: no detached/linear Building components found")

        except Exception as comp_guard_exc:
            log.warning(f"  FENCE COMPONENT GUARD skipped: {comp_guard_exc}")

        # ------------------------------------------------------------
        # FENCE-ONLY ATTACHED CANOPY / TAIL GUARD
        # ------------------------------------------------------------
        # Component guard alone cannot remove leakage when a false Building tail
        # is physically connected to a real roof/body component.  That is exactly
        # what happens near trees, power-line corridors, and sloped ground strips:
        # the component is not detached, so the old component guard keeps it.
        #
        # This pass works point-wise on exact fence target rows only:
        #   - preserve original Building rows;
        #   - preserve points close to a dense Building core;
        #   - rollback weak attached Building points that are still surrounded by
        #     original vegetation/ground and have poor local Building support.
        # Direct LAZ is untouched because this code is inside fence write-back only.
        try:
            local_target_pos = np.asarray(target_pos_in_run, dtype=np.int64)
            local_target_pos = local_target_pos[
                (local_target_pos >= 0) & (local_target_pos < len(sub_classes))
            ]

            if len(local_target_pos) > 0:
                target_classes_now = sub_classes[local_target_pos]

                original_target_classes = np.asarray(original_target_classes, dtype=np.uint8)
                if len(original_target_classes) != len(local_target_pos):
                    original_target_classes = original_target_classes_parent.copy()

                building_rows = np.where(target_classes_now == 5)[0]
                rollback_rows = []

                if len(building_rows) > 0:
                    building_local_pos = local_target_pos[building_rows]
                    building_xy = sub_xyz[building_local_pos, :2]

                    bldg_tree = cKDTree(building_xy)

                    # Local Building density.  Dense core = roof/body anchor.
                    try:
                        bldg_neighbor_count = bldg_tree.query_ball_point(
                            building_xy,
                            r=0.95,
                            return_length=True,
                        ).astype(np.int32)
                    except TypeError:
                        bldg_neighbor_count = np.array(
                            [len(x) for x in bldg_tree.query_ball_point(building_xy, r=0.95)],
                            dtype=np.int32,
                        )

                    orig_building_rows = original_target_classes[building_rows] == 5
                    dense_core_mask = (bldg_neighbor_count >= 12) | orig_building_rows

                    # If a sparse small roof has no very dense core, use the strongest
                    # local Building rows as a fallback core instead of rolling back all.
                    if not np.any(dense_core_mask):
                        cutoff = int(max(6, np.percentile(bldg_neighbor_count, 70)))
                        dense_core_mask = bldg_neighbor_count >= cutoff

                    if np.any(dense_core_mask):
                        core_xy = building_xy[dense_core_mask]
                        core_tree = cKDTree(core_xy)
                        d_core, _ = safe_query(core_tree, building_xy, k=1)
                        d_core = np.asarray(d_core, dtype=np.float32).reshape(-1)
                    else:
                        d_core = np.zeros(len(building_rows), dtype=np.float32)

                    target_tree = cKDTree(sub_xyz[local_target_pos, :2])

                    # Inspect only rows with a KNOWN original Ground/Vegetation class.
                    # Original class 0 is unclassified/unknown, so it must be neutral:
                    # it is not evidence that an AI-created Building is wrong. Detached
                    # false Building strings are still handled by the geometry/component
                    # guards above.
                    created_mask = np.isin(
                        original_target_classes[building_rows],
                        [1, 2, 3, 4],
                    )

                    inspect_mask = (
                        created_mask &
                        (
                            (d_core > 0.70) |
                            (bldg_neighbor_count < 10) |
                            np.isin(original_target_classes[building_rows], [2, 3, 4])
                        )
                    )

                    inspect_building_rows = building_rows[inspect_mask]
                    inspect_d_core = d_core[inspect_mask]
                    inspect_counts = bldg_neighbor_count[inspect_mask]

                    if len(inspect_building_rows) > 0:
                        inspect_local_pos = local_target_pos[inspect_building_rows]
                        nlists = target_tree.query_ball_point(
                            sub_xyz[inspect_local_pos, :2],
                            r=1.45,
                        )

                        for row_i, nb_rows in enumerate(nlists):
                            if len(nb_rows) < 8:
                                continue

                            nb_rows = np.asarray(nb_rows, dtype=np.int64)
                            nb_cur = target_classes_now[nb_rows]
                            nb_org = original_target_classes[nb_rows]

                            b_ratio = float(np.count_nonzero(nb_cur == 5)) / float(len(nb_rows))
                            org_ground_ratio = float(np.count_nonzero(nb_org == 1)) / float(len(nb_rows))
                            org_low_ratio = float(np.count_nonzero(nb_org == 2)) / float(len(nb_rows))
                            org_mid_ratio = float(np.count_nonzero(nb_org == 3)) / float(len(nb_rows))
                            org_high_ratio = float(np.count_nonzero(nb_org == 4)) / float(len(nb_rows))
                            org_veg_ratio = org_low_ratio + org_mid_ratio + org_high_ratio

                            target_row = int(inspect_building_rows[row_i])
                            lp = int(local_target_pos[target_row])
                            old_cls = int(original_target_classes[target_row])

                            dc = float(inspect_d_core[row_i])
                            bc = int(inspect_counts[row_i])

                            # Keep true building body: close to dense roof/body core
                            # and supported by current Building neighbors.
                            strong_attached_building = (dc <= 0.70 and b_ratio >= 0.34 and bc >= 8)

                            # Tree/canopy attached to roof: original HighVeg/vegetation
                            # dominates around the point, and Building support is not strong.
                            attached_canopy = (
                                (old_cls in (3, 4)) and
                                (org_veg_ratio >= 0.46) and
                                (org_high_ratio >= 0.24) and
                                (dc > 0.55) and
                                (b_ratio < 0.72)
                            )

                            # Ground/road/courtyard tail attached to building edge.
                            attached_ground_tail = (
                                (old_cls in (1, 2)) and
                                (org_ground_ratio + org_low_ratio >= 0.55) and
                                (dc > 0.85) and
                                (b_ratio < 0.64)
                            )

                            # Sparse magenta line/string connected to a real component.
                            weak_string = (
                                (bc < 7) and
                                (dc > 0.55) and
                                (b_ratio < 0.58)
                            )

                            # Larger vegetation pocket connected to roof by a thin bridge.
                            vegetation_pocket = (
                                (org_veg_ratio >= 0.64) and
                                (dc > 0.95) and
                                (b_ratio < 0.82)
                            )

                            if strong_attached_building:
                                continue

                            if attached_canopy or attached_ground_tail or weak_string or vegetation_pocket:
                                rollback_rows.append(target_row)

                if rollback_rows:
                    rollback_rows = np.unique(np.asarray(rollback_rows, dtype=np.int64))
                    safe_original = _safe_fence_rollback_classes(
                        original_target_classes[rollback_rows]
                    )
                    sub_classes[local_target_pos[rollback_rows]] = safe_original
                    log.info(
                        "  FENCE ATTACHED TAIL GUARD: "
                        f"rolled back {len(rollback_rows):,} attached canopy/ground/tail Building points"
                    )
                else:
                    log.info("  FENCE ATTACHED TAIL GUARD: no attached weak Building tails found")

        except Exception as tail_guard_exc:
            log.warning(f"  FENCE ATTACHED TAIL GUARD skipped: {tail_guard_exc}")

        # ------------------------------------------------------------
        # FENCE STRICT ORIGINAL-CLASS GUARD
        # ------------------------------------------------------------
        # Last safety layer before write-back.  If a point became Building but
        # came from road/ground/vegetation originally, keep it only when it is
        # strongly supported by nearby Building inside the exact fence target.
        # This is deliberately conservative for production SNT fence runs.
        try:
            local_target_pos = np.asarray(target_pos_in_run, dtype=np.int64)
            local_target_pos = local_target_pos[
                (local_target_pos >= 0) & (local_target_pos < len(sub_classes))
            ]
            if len(local_target_pos) > 0:
                original_target_classes = np.asarray(original_target_classes, dtype=np.uint8)
                if len(original_target_classes) != len(local_target_pos):
                    original_target_classes = original_target_classes_parent.copy()

                cur_target_classes = sub_classes[local_target_pos]

                # IMPORTANT CLASS-SPACE FIX:
                # original class 0 means unclassified/unknown. It must not be used
                # as negative evidence against a valid AI Building prediction. Only
                # known GUI Ground/Vegetation classes 1..4 participate in this final
                # original-class rollback guard.
                known_original_nonbuilding = np.isin(
                    original_target_classes,
                    [1, 2, 3, 4],
                )
                created_building_rows = np.where(
                    (cur_target_classes == 5) & known_original_nonbuilding
                )[0]

                strict_rollback_rows = []
                if len(created_building_rows) > 0:
                    target_xy = sub_xyz[local_target_pos, :2]
                    target_tree = cKDTree(target_xy)

                    b_rows = np.where(cur_target_classes == 5)[0]
                    if len(b_rows) > 0:
                        b_xy = target_xy[b_rows]
                        b_tree = cKDTree(b_xy)
                        try:
                            b_count_all = b_tree.query_ball_point(
                                b_xy,
                                r=0.90,
                                return_length=True,
                            ).astype(np.int32)
                        except TypeError:
                            b_count_all = np.array(
                                [len(x) for x in b_tree.query_ball_point(b_xy, r=0.90)],
                                dtype=np.int32,
                            )
                        dense_b_rows = b_rows[b_count_all >= 16]
                        if len(dense_b_rows) == 0:
                            dense_b_rows = b_rows
                        dense_tree = cKDTree(target_xy[dense_b_rows])

                        inspect_pos = local_target_pos[created_building_rows]
                        d_dense, _ = safe_query(dense_tree, sub_xyz[inspect_pos, :2], k=1)
                        d_dense = np.asarray(d_dense, dtype=np.float32).reshape(-1)
                        nlists = target_tree.query_ball_point(sub_xyz[inspect_pos, :2], r=1.35)

                        for i, row in enumerate(created_building_rows):
                            nb = np.asarray(nlists[i], dtype=np.int64)
                            if len(nb) < 8:
                                strict_rollback_rows.append(row)
                                continue

                            nb_cur = cur_target_classes[nb]
                            nb_org = original_target_classes[nb]

                            b_ratio = float(np.count_nonzero(nb_cur == 5)) / float(len(nb))
                            org_high = float(np.count_nonzero(nb_org == 4)) / float(len(nb))
                            org_veg = float(np.count_nonzero(np.isin(nb_org, [2, 3, 4]))) / float(len(nb))
                            org_ground_low = float(np.count_nonzero(np.isin(nb_org, [1, 2]))) / float(len(nb))

                            old_cls = int(original_target_classes[row])
                            dc = float(d_dense[i])

                            # Final vegetation-protection gate.
                            # Created Building from original vegetation is accepted only when
                            # it is glued to a Building cluster.  v2 adds a dense roof/wall
                            # allowance so true roofs/facades do not stay green after parent-HAG.
                            keep_as_building = (
                                (old_cls == 1 and dc <= 0.35 and b_ratio >= 0.78 and org_veg < 0.40) or
                                (old_cls == 2 and dc <= 0.42 and b_ratio >= 0.82 and org_high < 0.18) or
                                (old_cls == 3 and dc <= 0.36 and b_ratio >= 0.88 and org_high < 0.16 and org_veg < 0.52) or
                                (old_cls == 4 and dc <= 0.30 and b_ratio >= 0.92 and org_high < 0.12 and org_veg < 0.45)
                            )

                            dense_roof_wall_keep = (
                                (old_cls in (2, 3, 4) and dc <= 0.58 and b_ratio >= 0.76 and org_high < 0.58) or
                                (old_cls in (1, 2, 3, 4) and dc <= 0.42 and b_ratio >= 0.86 and org_veg < 0.68) or
                                (old_cls in (3, 4) and dc <= 0.72 and b_ratio >= 0.88 and org_high < 0.42)
                            )

                            keep_as_building = keep_as_building or dense_roof_wall_keep

                            weak_tree_or_tail = (
                                (old_cls == 4 and not dense_roof_wall_keep) or
                                (old_cls == 3 and (org_high >= 0.12 or org_veg >= 0.50 or b_ratio < 0.90) and not dense_roof_wall_keep) or
                                (old_cls == 2 and (org_high >= 0.18 or org_veg >= 0.58 or b_ratio < 0.84) and not dense_roof_wall_keep) or
                                (old_cls == 1 and org_ground_low >= 0.55 and b_ratio < 0.82) or
                                (dc > 0.70 and b_ratio < 0.90)
                            )

                            if (not keep_as_building) or weak_tree_or_tail:
                                strict_rollback_rows.append(row)

                if strict_rollback_rows:
                    strict_rollback_rows = np.unique(
                        np.asarray(strict_rollback_rows, dtype=np.int64)
                    )
                    safe_original = _safe_fence_rollback_classes(
                        original_target_classes[strict_rollback_rows]
                    )
                    sub_classes[local_target_pos[strict_rollback_rows]] = safe_original
                    log.info(
                        "  FENCE VEGETATION-PROTECT ORIGINAL-CLASS GUARD: "
                        f"rolled back {len(strict_rollback_rows):,} weak created Building points"
                    )
                else:
                    log.info("  FENCE VEGETATION-PROTECT ORIGINAL-CLASS GUARD: no weak created Building points found")
        except Exception as strict_guard_exc:
            log.warning(f"  FENCE VEGETATION-PROTECT ORIGINAL-CLASS GUARD skipped: {strict_guard_exc}")

        replacement_classes = sub_classes[target_pos_in_run].astype(np.uint8, copy=True)

        # Final fence class-space invariant. Base output is GUI 1..5; optional
        # Advanced QC adds 0/7/18 and Power adds standard 14/15.
        valid_fence_codes = [0, 1, 2, 3, 4, 5, 7, 14, 15, 18]
        invalid_gui = ~np.isin(replacement_classes, valid_fence_codes)
        if np.any(invalid_gui):
            invalid_count = int(np.count_nonzero(invalid_gui))
            replacement_classes[invalid_gui] = 1
            log.warning(
                f"  FENCE CLASS-SPACE GUARD: converted {invalid_count:,} "
                "invalid/non-GUI rollback classes to Ground (1)."
            )

        # ============================================================
        # ADVANCED FENCE -> PTC / CUSTOM OUTPUT CODES
        # ============================================================
        combined_mapping = {}
        source_to_internal = {}

        # Core 5-class PTC mapping is supplied only when PTC is active.
        if class_mapping:
            combined_mapping.update(dict(class_mapping))
            source_to_internal.update({
                1: 0, 2: 1, 3: 2, 4: 3, 5: 4,
            })

            optional_qc = dict(
                (advanced_config or {}).get("_ptc_optional_semantic_codes", {}) or {}
            )
            combined_mapping.update(optional_qc)
            source_to_internal.update({
                0: "uncategorized",
                7: "low_point",
                18: "high_noise",
            })

        # Wire/Pole output mapping is user/PTC configurable even without a PTC.
        if enable_power_lines and power_mapping:
            combined_mapping.update(dict(power_mapping))
            source_to_internal.update({
                14: ADV_POWER_WIRE_INTERNAL,
                15: ADV_POWER_POLE_INTERNAL,
            })

        if combined_mapping and source_to_internal:
            replacement_classes = remap_classes(
                replacement_classes,
                source_to_internal=source_to_internal,
                class_mapping=combined_mapping,
            )
            log.info(
                f"  ADVANCED FENCE FINAL REMAP: {combined_mapping}"
            )

        # Do NOT call full_las.write(...) here.
        # On large LAZ files that final write can spike RAM and hard-close the app.
        # Stream the original full file and replace only fence target classifications.
        _write_full_laz_with_updated_classes_streaming(
            input_path=input_path,
            output_path=output_path,
            target_indices=target_indices,
            replacement_classes=replacement_classes,
            log=log,
            chunk_size=750_000,
        )

        unique, counts = np.unique(replacement_classes, return_counts=True)

        log.info("")
        log.info("  FENCE MODE OUTPUT WRITTEN")
        log.info(f"    Full output file       : {output_path}")
        log.info(f"    Updated fence points   : {len(target_indices):,}")
        log.info(f"    Outside fence          : unchanged")
        log.info("    Inside-fence classes:")
        for cls, cnt in zip(unique, counts):
            log.info(f"      class {int(cls)}: {int(cnt):,}")

        return True

    except Exception as e:
        log.error(f"  FENCE MODE: final write-back failed: {e}")
        return False
    
def classify_laz(input_path, output_path, output_dir,
                 model, device, feat_mean, feat_std, log,
                 class_mapping=None, target_indices=None, target_classes=None,
                 postprocess_indices=None, precomputed_hag=None,
                 power_mapping=None, enable_power_lines=False,
                 advanced_config=None):
    input_path  = Path(input_path)
    output_path = Path(output_path)
    output_dir  = Path(output_dir)
    advanced_config = dict(advanced_config or {})
    power_mapping = dict(power_mapping or {})
    enable_power_lines = bool(enable_power_lines)
    t_start     = time.time()
    if target_indices is not None:
        return _classify_laz_fence_subset(
            input_path=input_path,
            output_path=output_path,
            output_dir=output_dir,
            model=model,
            device=device,
            feat_mean=feat_mean,
            feat_std=feat_std,
            log=log,
            class_mapping=class_mapping,
            target_indices=target_indices,
            target_classes=target_classes,
            power_mapping=power_mapping,
            enable_power_lines=enable_power_lines,
            advanced_config=advanced_config,
        )

    log.info(f"{'-' * 60}")
    log.info(f"FILE: {input_path.name}")
    log.info(f"{'-' * 60}")

    try:
        validation = validate_input_file(input_path, log)
    except ValueError as e:
        log.error(f"  VALIDATION FAILED: {e}")
        return False

    n_total = validation["point_count"]

    try:
        las = laspy.read(str(input_path))
    except Exception as e:
        log.error(f"  FAILED to read: {e}")
        return False

    xyz = np.column_stack([np.array(las.x, dtype=np.float64),
                           np.array(las.y, dtype=np.float64),
                           np.array(las.z, dtype=np.float64)])

    has_intensity = validation["has_intensity"]
    has_returns   = validation["has_returns"]

    cols = [xyz.astype(np.float32, copy=False)]
    if has_intensity:
        cols.append(np.array(las.intensity, dtype=np.float32).reshape(-1, 1))
    if has_returns:
        cols.append(np.array(las.return_number,     dtype=np.float32).reshape(-1, 1))
        cols.append(np.array(las.number_of_returns, dtype=np.float32).reshape(-1, 1))
    points = np.hstack(cols).astype(np.float32, copy=False)
    del cols
    gc.collect()

    stem      = input_path.stem
    cache_dir = get_cache_dir(output_dir)
    cache_key = get_cache_key(input_path, MODEL_PATH)
    log.info(f"  Cache key : {cache_key}")
    log.info(f"  Advanced target classes passed to inference: {sorted(set(int(x) for x in (target_classes or [0, 1, 2, 3, 4])))}")
    log.info(f"  Cache dir : {cache_dir}")

    # Fence/SNT must always run from fresh computed features.
    # This prevents stale HAG/geometry/density/roughness/tile-index/vote caches
    # from reusing results after threshold or post-processing changes.
    fence_fresh_mode = postprocess_indices is not None
    if fence_fresh_mode:
        log.info("  FENCE FRESH MODE ENABLED: all caches disabled for this fence run.")

    effective_inference_batch_size = _effective_inference_batch_size(
        device, INFERENCE_BATCH_SIZE, fence_mode=fence_fresh_mode, log=log
    )

    # ── [1/5] HAG ────────────────────────────────────────────────────
    log.info("")
    log.info("  [1/5] HAG (CSF cloth simulation)...")

    if precomputed_hag is not None:
        try:
            hag = np.asarray(precomputed_hag, dtype=np.float32)
            if len(hag) != n_total:
                log.warning(
                    f"    Precomputed HAG length mismatch: {len(hag):,} != {n_total:,}; "
                    "falling back to normal HAG."
                )
                hag = None
            else:
                log.info(
                    "    Fence parent-HAG supplied - CSF/fallback skipped for temp subset. "
                    f"hag p02/p50/p98="
                    f"{np.percentile(hag, 2):.2f}/{np.percentile(hag, 50):.2f}/{np.percentile(hag, 98):.2f}"
                )
        except Exception as e:
            log.warning(f"    Precomputed HAG invalid; falling back to normal HAG: {e}")
            hag = None
    else:
        hag = None

    if hag is None:
        cached = None if fence_fresh_mode else load_cache(cache_dir, stem, cache_key, ["hag"], log, mmap_names={"hag"})
        if cached:
            hag = cached["hag"]
            log.info("    Loaded from cache - SKIPPED.")
        else:
            try:
                t0  = time.time()
                hag = compute_hag_csf(xyz, log)
                log.info(f"    Done: {time.time() - t0:.1f}s")
                if not fence_fresh_mode:
                    save_cache(cache_dir, stem, cache_key, {"hag": hag}, log)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log.error(f"    HAG failed: {e}")
                return False

    # ── [2/5] Geometric features ──────────────────────────────────────
    log.info("")
    log.info("  [2/5] Geometric features (full cloud, once)...")
    cached = None if fence_fresh_mode else load_cache(
        cache_dir,
        stem,
        cache_key,
        ["geom_ds", "geom_vox_map"],
        log,
        mmap_names={"geom_ds", "geom_vox_map"},
    )
    if cached:
        geom_ds      = cached["geom_ds"]
        geom_vox_map = cached["geom_vox_map"].astype(np.int32, copy=False)
        log.info("    Loaded from cache - SKIPPED.")
    else:
        try:
            t0 = time.time()
            geom_ds, geom_vox_map = compute_geometric_features_full(
                xyz, FEATURE_SCALES, GEOM_VOXEL_SIZE, log)
            geom_vox_map = geom_vox_map.astype(np.int32, copy=False)
            gc.collect()
            log.info(f"    Done: {time.time() - t0:.1f}s shape={geom_ds.shape}")
            if not fence_fresh_mode:
                save_cache(cache_dir, stem, cache_key,
                           {"geom_ds": geom_ds, "geom_vox_map": geom_vox_map}, log)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.error(f"    Geom features failed: {e}")
            return False

    # ── [3/5] Density ─────────────────────────────────────────────────
    log.info("")
    log.info("  [3/5] Density (full cloud, once)...")
    cached = None if fence_fresh_mode else load_cache(cache_dir, stem, cache_key, ["density"], log, mmap_names={"density"})
    if cached:
        density_full = cached["density"]
        log.info("    Loaded from cache - SKIPPED.")
    else:
        try:
            t0           = time.time()
            density_full = compute_density_full(xyz.astype(np.float64, copy=False), radius=1.0)
            log.info(f"    Done: {time.time() - t0:.1f}s")
            if not fence_fresh_mode:
                save_cache(cache_dir, stem, cache_key, {"density": density_full}, log)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.error(f"    Density failed: {e}")
            return False

    # ── [4/5] Roughness ───────────────────────────────────────────────
    log.info("")
    log.info("  [4/5] Roughness (full cloud, once)...")
    cached = None if fence_fresh_mode else load_cache(cache_dir, stem, cache_key, ["roughness"], log, mmap_names={"roughness"})
    if cached:
        roughness_full = cached["roughness"]
        log.info("    Loaded from cache - SKIPPED.")
    else:
        try:
            t0             = time.time()
            roughness_full = compute_roughness_full(xyz.astype(np.float64, copy=False), k=10)
            log.info(f"    Done: {time.time() - t0:.1f}s")
            if not fence_fresh_mode:
                save_cache(cache_dir, stem, cache_key, {"roughness": roughness_full}, log)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.error(f"    Roughness failed: {e}")
            return False

    # ── [5/5] Tile inference ──────────────────────────────────────────
    log.info("")
    log.info("  [5/5] Tile inference (BATCHED)...")

    stride = TILE_SIZE - TILE_OVERLAP
    if stride <= 0:
        stride = TILE_SIZE * 0.5

    x_min, y_min = xyz[:, 0].min(), xyz[:, 1].min()
    x_max, y_max = xyz[:, 0].max(), xyz[:, 1].max()

    xs_list = np.arange(x_min, x_max, stride)
    ys_list = np.arange(y_min, y_max, stride)
    if len(xs_list) == 0 or xs_list[-1] + TILE_SIZE < x_max:
        xs_list = np.append(xs_list, x_max - TILE_SIZE)
    if len(ys_list) == 0 or ys_list[-1] + TILE_SIZE < y_max:
        ys_list = np.append(ys_list, y_max - TILE_SIZE)

    tile_specs = [(xs, ys) for xs in xs_list for ys in ys_list]
    n_tiles    = len(tile_specs)
    log.info(f"    Tiles: {n_tiles}  stride={stride}m  "
             f"batch_size={effective_inference_batch_size}")

    try:
        tile_index_data = build_or_load_tile_indices(
            xyz,
            tile_specs,
            cache_dir,
            stem,
            cache_key,
            log,
            use_cache=not fence_fresh_mode,
        )
    except KeyboardInterrupt:
        raise
    except Exception as e:
        log.error(f"    Tile index failed: {e}")
        return False

    safe_std      = np.where(feat_std < 1e-6, 1.0, feat_std).astype(np.float32)

    fence_safe_feature_mode = postprocess_indices is not None
    fence_balanced_building_recovery = fence_safe_feature_mode

    if fence_safe_feature_mode:
        log.info(
            "  FENCE SAFE FEATURE MODE ENABLED: "
            "absolute Z neutralized for fence/SNT inference; direct LAZ unchanged."
        )

    if fence_balanced_building_recovery:
        log.info(
            "  FENCE BALANCED BUILDING RECOVERY ENABLED: "
            "V19-created Building is kept using roof/wall/body support."
        )
        log.info(
            "  FENCE STRICT FINISH MODE ENABLED: "
            "fresh inference; conservative roof/wall recovery; no broad gap/body growth."
        )
    if fence_safe_feature_mode:
        # Do not reuse old fence vote caches because this mode intentionally
        # changes feature column 2 (absolute Z). Direct LAZ cache behavior is unchanged.
        cached_votes = None
        log.info("    Fence safe mode: vote cache bypassed for fresh inference.")
    else:
        cached_votes = load_cache(cache_dir, stem, cache_key, ["vote_counts"], log)

    if cached_votes:
        vote_counts = cached_votes["vote_counts"].astype(np.uint16, copy=False)
        log.info("    Inference SKIPPED - loaded vote_counts from cache.")
    else:
        vote_counts = np.zeros((n_total, 5), dtype=np.uint16)
        start_tile  = 0

        if fence_safe_feature_mode:
            ckpt = None
        else:
            ckpt = load_cache(cache_dir, stem, cache_key,
                              ["vote_counts_ckpt", "vote_counts_ckpt_tile"], log)
        if ckpt:
            vote_counts = ckpt["vote_counts_ckpt"].astype(np.uint16, copy=False)
            start_tile  = int(ckpt["vote_counts_ckpt_tile"][0])
            if start_tile < 0 or start_tile > n_tiles:
                log.warning(f"    Invalid checkpoint tile {start_tile}; restarting.")
                vote_counts = np.zeros((n_total, 5), dtype=np.uint16)
                start_tile  = 0
            else:
                log.info(f"    Resuming from checkpoint tile {start_tile}/{n_tiles}")

        log.info("")
        log.info(f"  Running inference: {VOTE_PASSES} pass x {n_tiles} tiles "
                 f"[batch={effective_inference_batch_size}]...")
        t_inf         = time.time()
        use_amp       = naksha_should_use_amp(device)
        autocast_dev  = "cuda" if device.type == "cuda" else "cpu"

        for vote in range(VOTE_PASSES):
            t_pass     = time.time()
            tiles_done = 0
            gpu_batch  = []   # list of (coords, feats)
            batch_meta = []   # list of (global_real_idx, real_count)

            for ti, data in enumerate(tile_index_data):
                if ti < start_tile:
                    tiles_done += 1
                    continue

                try:
                    if data is None:
                        tiles_done += 1
                        if ((ti + 1) % CHECKPOINT_EVERY_TILES == 0) and (not fence_fresh_mode):
                            save_vote_checkpoint(cache_dir, stem, cache_key,
                                                 vote_counts, ti + 1, log)
                        continue

                    idx_expand, core_local_idx = data
                    tile_xyz       = xyz[idx_expand]
                    tile_points    = points[idx_expand]
                    tile_hag       = hag[idx_expand]
                    tile_geom      = geom_ds[geom_vox_map[idx_expand]]
                    tile_density   = density_full[idx_expand]
                    tile_roughness = roughness_full[idx_expand]

                    tile_xy_origin = tile_xyz[:, :2].mean(axis=0).astype(np.float32)

                    z_feature_override = None

                    # Fence/SNT Advanced AI safety:
                    # postprocess_indices is supplied only by fence workflow.
                    # The trained stats use a limited absolute-Z range, but
                    # current production files can be far outside it.
                    # Neutralizing Z in fence mode prevents false Building leakage
                    # caused by elevation distribution mismatch.
                    if postprocess_indices is not None:
                        z_feature_override = np.full(
                            len(tile_points),
                            float(feat_mean[2]),
                            dtype=np.float32,
                        )

                    tile_features = assemble_tile_features(
                        tile_points,
                        tile_hag,
                        tile_geom,
                        tile_density,
                        tile_roughness,
                        has_intensity,
                        has_returns,
                        xy_origin=tile_xy_origin,
                        z_feature_override=z_feature_override,
                    )
                    tile_features -= feat_mean.reshape(1, -1)
                    tile_features /= safe_std.reshape(1, -1)
                    np.clip(tile_features, -10.0, 10.0, out=tile_features)

                    core_idx = core_local_idx.copy()
                    np.random.shuffle(core_idx)

                    for start in range(0, len(core_idx), CHUNK_STEP):
                        end        = min(start + NUM_POINTS, len(core_idx))
                        chunk      = core_idx[start:end]
                        real_count = end - start
                        if real_count <= 0:
                            continue
                        if len(chunk) < NUM_POINTS:
                            extra = np.random.choice(chunk, NUM_POINTS - len(chunk),
                                                     replace=True)
                            chunk = np.concatenate([chunk, extra])

                        coords = np.ascontiguousarray(tile_xyz[chunk], dtype=np.float32)
                        feats  = np.ascontiguousarray(tile_features[chunk], dtype=np.float32)
                        coords -= coords.mean(axis=0, keepdims=True)

                        global_real_idx = idx_expand[chunk[:real_count]]
                        gpu_batch.append((coords, feats))
                        batch_meta.append((global_real_idx, real_count))

                        # ── FLUSH BATCH when full ────────────────────────
                        if len(gpu_batch) >= effective_inference_batch_size:
                            flush_gpu_batch(gpu_batch, batch_meta, vote_counts,
                                            model, device, use_amp, autocast_dev, log)
                            gpu_batch  = []
                            batch_meta = []

                        if "extra" in locals():
                            del extra

                    tiles_done += 1
                    del tile_xyz, tile_points, tile_hag, tile_geom
                    del tile_density, tile_roughness, tile_features
                    del idx_expand, core_local_idx, core_idx

                    if (ti + 1) % 10 == 0:
                        safe_clear(device)

                    if (ti + 1) % CHECKPOINT_EVERY_TILES == 0 or (ti + 1) == n_tiles:
                        # Flush pending batch before checkpoint/end-of-pass.
                        # In fence fresh mode do not save checkpoint files.
                        if gpu_batch:
                            flush_gpu_batch(gpu_batch, batch_meta, vote_counts,
                                            model, device, use_amp, autocast_dev, log)
                            gpu_batch  = []
                            batch_meta = []
                        if not fence_fresh_mode:
                            save_vote_checkpoint(cache_dir, stem, cache_key,
                                                 vote_counts, ti + 1, log)

                    if (ti + 1) % 10 == 0 or (ti + 1) == n_tiles:
                        elapsed_t = time.time() - t_pass
                        rate      = tiles_done / max(elapsed_t, 1e-6)
                        remaining = (n_tiles - ti - 1) / max(rate, 1e-6)
                        log.info(f"    Pass {vote+1}: tile {ti+1}/{n_tiles} "
                                 f"| {elapsed_t:.0f}s | ~{remaining/60:.1f}min left")

                except KeyboardInterrupt:
                    if gpu_batch:
                        flush_gpu_batch(gpu_batch, batch_meta, vote_counts,
                                        model, device, use_amp, autocast_dev, log)
                    if not fence_fresh_mode:
                        save_vote_checkpoint(cache_dir, stem, cache_key,
                                             vote_counts, ti, log)
                        log.warning(f"    Interrupted at tile {ti}. Checkpoint saved.")
                    else:
                        log.warning(f"    Interrupted at tile {ti}. Fence fresh mode: checkpoint not saved.")
                    safe_clear(device)
                    raise

                except RuntimeError as e:
                    if "out of memory" in str(e).lower():
                        log.error(f"    OOM at tile {ti+1}. "
                                  f"Reduce effective batch from {effective_inference_batch_size}.")
                        if not fence_fresh_mode:
                            save_vote_checkpoint(cache_dir, stem, cache_key,
                                                 vote_counts, ti, log)
                        safe_clear(device)
                        return False
                    raise

                except Exception as e:
                    log.error(f"    Failed at tile {ti+1}: {e}")
                    if not fence_fresh_mode:
                        save_vote_checkpoint(cache_dir, stem, cache_key,
                                             vote_counts, ti, log)
                    safe_clear(device)
                    return False

            # Flush any remaining tiles in batch
            if gpu_batch:
                flush_gpu_batch(gpu_batch, batch_meta, vote_counts,
                                model, device, use_amp, autocast_dev, log)
                gpu_batch  = []
                batch_meta = []

            log.info(f"    Pass {vote+1}/{VOTE_PASSES}: {time.time()-t_pass:.1f}s")

        log.info(f"  Inference total: {time.time()-t_inf:.1f}s")

        if not fence_fresh_mode:
            save_cache(cache_dir, stem, cache_key, {"vote_counts": vote_counts}, log)
        else:
            log.info("    FENCE FRESH MODE: vote_counts cache not saved.")

        for name in ["vote_counts_ckpt", "vote_counts_ckpt_tile"]:
            ckpt_path = cache_dir / f"{stem}_{cache_key}_{name}.npy"
            if ckpt_path.exists():
                try:
                    ckpt_path.unlink()
                    log.info(f"    Checkpoint cleaned: {ckpt_path.name}")
                except Exception as e:
                    log.warning(f"    Checkpoint clean failed: {e}")

    del tile_index_data
    safe_clear(device)

    # ── Resolve votes ─────────────────────────────────────────────────
    has_votes  = vote_counts.sum(axis=1) > 0
    n_voted    = int(has_votes.sum())
    n_no_votes = int((~has_votes).sum())

    if n_voted == 0:
        log.error("FATAL: No points received votes.")
        return False

    predictions = np.zeros(n_total, dtype=np.uint8)
    predictions[has_votes] = vote_counts[has_votes].argmax(axis=1).astype(np.uint8)

    if n_no_votes > 0:
        pct = 100.0 * n_no_votes / n_total
        log.info(f"    {n_no_votes:,} unvoted points ({pct:.2f}%) - NN fill")
        if pct > 5.0:
            log.warning("  >5% unvoted - consider increasing TILE_OVERLAP")
        voted_xyz   = xyz[has_votes]
        voted_preds = predictions[has_votes]
        try:
            tree    = cKDTree(voted_xyz)
            _, nn_idx = safe_query(tree, xyz[~has_votes], k=1)
            predictions[~has_votes] = voted_preds[np.asarray(nn_idx).reshape(-1)]
        except Exception as e:
            log.error(f"    NN fill failed: {e}")
            return False

    total_votes = vote_counts.sum(axis=1, keepdims=True).astype(np.float32)
    total_votes[total_votes == 0] = 1.0
    confidence  = vote_counts.max(axis=1).astype(np.float32) / total_votes.squeeze()
    confidence  = np.nan_to_num(confidence, nan=0.0, posinf=0.0, neginf=0.0)
    del vote_counts
    gc.collect()

    pre_post_predictions = predictions.copy()

    fix_report = {}

    if MODEL_ONLY_ADVANCED_AI:
        log.info("")
        log.info("  MODEL-ONLY MODE ENABLED")
        log.info("  Skipping all post-processing steps.")
        log.info("  Output will be written from raw model prediction only.")

        # Free post-processing geometry memory before writing output.
        try:
            del geom_ds, geom_vox_map
        except Exception:
            pass
        gc.collect()
        safe_clear(device)
    else:
        # Light geometry helper extraction.
        log.info("")
        log.info("  Preparing V14 geometry helpers...")
        try:
            if enable_power_lines:
                (
                    linearity_05_raw,
                    planarity_05_raw,
                    verticality_05_raw,
                    linearity_10_raw,
                    verticality_10_raw,
                ) = _get_power_geom_from_geom(geom_ds, geom_vox_map)
            else:
                planarity_05_raw, verticality_05_raw = _get_plan_vert_from_geom(
                    geom_ds,
                    geom_vox_map,
                )
                linearity_05_raw = None
                linearity_10_raw = None
                verticality_10_raw = None

            del geom_ds, geom_vox_map
            gc.collect()
            log.info("  Freed geom_ds / geom_vox_map before correction")

        except Exception as e:
            log.error(f"  Failed to extract geometry helpers: {e}")
            return False

        log.info("")
        if LIGHT_GEOMETRY_CORRECTION and TARGET_ONLY_LIGHT_CORRECTION:
            log.info("  V19 TARGET-ONLY WALL/ROOF/VEG CLEANUP ENABLED")
            t_pp = time.time()

            # Fence/SNT balanced mode:
            # Snapshot before V19 so we can inspect only non-Building -> Building growth.
            # Unlike the previous strict rollback, this keeps real roof/wall recovery
            # when geometry is strong and there is nearby raw Building support.
            if fence_balanced_building_recovery:
                raw_before_v19 = predictions.copy()
            else:
                raw_before_v19 = None

            predictions, fix_report = post_process_light_v16_target_only(
                xyz,
                predictions,
                confidence,
                hag,
                verticality_05_raw,
                planarity_05_raw,
                log,
                active_indices=postprocess_indices,
            )

            if fence_balanced_building_recovery and raw_before_v19 is not None:
                added_mask = (
                    (predictions == BUILDING) &
                    (raw_before_v19 != BUILDING)
                )
                added_idx = np.where(added_mask)[0]

                keep_added = np.zeros(len(added_idx), dtype=bool)

                if len(added_idx) > 0:
                    raw_building_idx = np.where(raw_before_v19 == BUILDING)[0]

                    if len(raw_building_idx) > 0:
                        raw_bldg_tree = cKDTree(xyz[raw_building_idx][:, :2])
                        d_raw_bldg, _ = safe_query(
                            raw_bldg_tree,
                            xyz[added_idx][:, :2],
                            k=1,
                        )
                        d_raw_bldg = np.asarray(d_raw_bldg, dtype=np.float32).reshape(-1)
                    else:
                        d_raw_bldg = np.full(len(added_idx), np.inf, dtype=np.float32)

                    h = hag[added_idx]
                    p = planarity_05_raw[added_idx]
                    v = verticality_05_raw[added_idx]
                    c = confidence[added_idx]

                    # Roof recovery: keep planar elevated roof/eave holes that are close
                    # to model/raw Building seed. This restores real roofs without
                    # allowing isolated trees/canopies to become Building.
                    # VEGETATION-PROTECT MODE:
                    # Earlier thresholds still allowed tree/vegetation points near the roof
                    # to survive as Building. Keep only V19-created Building points that are
                    # very close to raw/model Building support AND have strong roof/wall shape.
                    roof_like = (
                        (h > 1.15) &
                        (p > 0.180) &
                        (v < 0.78) &
                        (d_raw_bldg < 1.35) &
                        (c > 0.72)
                    )

                    strong_roof_like = (
                        (h > 1.70) &
                        (p > 0.240) &
                        (v < 0.82) &
                        (d_raw_bldg < 1.75) &
                        (c > 0.68)
                    )

                    # Very strict facade recovery. Vegetation beside the building can be
                    # vertical too, so require close raw Building support and stronger planarity.
                    wall_like = (
                        (h > 0.45) &
                        (h < 3.60) &
                        (v > 0.24) &
                        (p > 0.070) &
                        (d_raw_bldg < 0.52) &
                        (c > 0.70)
                    )

                    base_wall_like = (
                        (h > 0.12) &
                        (h < 1.35) &
                        (v > 0.24) &
                        (p > 0.060) &
                        (d_raw_bldg < 0.38) &
                        (c > 0.72)
                    )

                    keep_added = roof_like | strong_roof_like | wall_like | base_wall_like

                rollback_idx = added_idx[~keep_added]
                keep_count = int(np.count_nonzero(keep_added))
                rollback_count = int(len(rollback_idx))

                if rollback_count > 0:
                    predictions[rollback_idx] = raw_before_v19[rollback_idx]

                fix_report["FENCE BALANCED kept V19 Building recovery"] = keep_count
                fix_report["FENCE BALANCED weak V19 Building rollback"] = rollback_count
                log.info(
                    f"  FENCE BALANCED MODE: kept {keep_count:,} "
                    f"roof/wall-supported V19 Building points; rolled back "
                    f"{rollback_count:,} weak/tree-like V19 Building points"
                )

                # Fence/SNT roof/wall rescue after vegetation protection.
                # Parent-HAG gives a stable height reference, but the vegetation
                # protection guard can now under-recover real roof/wall points.
                # This pass is narrow: it only converts points that are already
                # touching a current Building cluster AND have planar roof or
                # vertical facade geometry. It does not perform broad body/gap growth.
                try:
                    roof_wall_rescue_count = 0
                    active_rw_idx = np.asarray(postprocess_indices, dtype=np.int64)
                    active_rw_idx = active_rw_idx[
                        (active_rw_idx >= 0) & (active_rw_idx < len(predictions))
                    ]

                    cur_building_idx = np.where(predictions == BUILDING)[0]
                    if len(active_rw_idx) > 0 and len(cur_building_idx) > 0:
                        # Candidate classes only. Do not touch already-Building rows.
                        cand_mask = (
                            (predictions[active_rw_idx] != BUILDING) &
                            np.isin(predictions[active_rw_idx], [GROUND, LOWVEG, MIDVEG, HIGHVEG])
                        )
                        cand_idx = active_rw_idx[cand_mask]

                        if len(cand_idx) > 0:
                            bldg_tree_2d = cKDTree(xyz[cur_building_idx][:, :2])
                            d_bldg, _ = safe_query(
                                bldg_tree_2d,
                                xyz[cand_idx][:, :2],
                                k=1,
                            )
                            d_bldg = np.asarray(d_bldg, dtype=np.float32).reshape(-1)

                            ch = hag[cand_idx]
                            cp = planarity_05_raw[cand_idx]
                            cv = verticality_05_raw[cand_idx]
                            cc = confidence[cand_idx]
                            ccls = predictions[cand_idx]

                            roof_like = (
                                (d_bldg < 1.35) &
                                (ch > 0.45) &
                                (cp > 0.105) &
                                (cv < 0.88) &
                                (cc > 0.35) &
                                np.isin(ccls, [GROUND, MIDVEG, HIGHVEG])
                            )

                            # Low/deck roofs can sit close to terrain on hillside data.
                            # Allow them only when very close to current Building support
                            # and clearly planar.
                            low_deck_roof_like = (
                                (d_bldg < 0.80) &
                                (ch > -0.20) &
                                (ch < 1.55) &
                                (cp > 0.165) &
                                (cv < 0.72) &
                                (cc > 0.30) &
                                np.isin(ccls, [GROUND, MIDVEG, HIGHVEG])
                            )

                            wall_like = (
                                (d_bldg < 0.72) &
                                (ch > 0.12) &
                                (ch < 4.60) &
                                (cv > 0.22) &
                                (cp > 0.040) &
                                (cc > 0.35) &
                                np.isin(ccls, [LOWVEG, MIDVEG, HIGHVEG])
                            )

                            rescue_idx = cand_idx[roof_like | low_deck_roof_like | wall_like]

                            if len(rescue_idx) > 0:
                                if len(rescue_idx) > 35_000:
                                    score = (
                                        1.50 * planarity_05_raw[rescue_idx] +
                                        0.80 * verticality_05_raw[rescue_idx] +
                                        0.25 * np.clip(hag[rescue_idx], 0.0, 5.0)
                                    )
                                    rescue_idx = rescue_idx[np.argsort(-score)[:35_000]]

                                if 'tree_all' not in locals() or tree_all is None:
                                    tree_all = cKDTree(xyz)

                                # Vectorized (was a per-point Python loop with several
                                # numpy calls per iteration — the dominant cost of this
                                # pass for large rescue_idx sets). Same thresholds, same
                                # resulting point set: each of the three branches below
                                # independently qualifies a point (the original if/elif
                                # chain only short-circuited redundant checks, it did not
                                # change which points end up added).
                                valid_pts, b_ratio, high_ratio, veg_ratio = batched_neighbor_class_ratios(
                                    tree_all, rescue_idx, xyz, predictions, radius=1.45,
                                    min_neighbors=10, building_code=BUILDING,
                                    highveg_code=HIGHVEG, veg_codes=[LOWVEG, MIDVEG, HIGHVEG],
                                )

                                if len(valid_pts) > 0:
                                    h_pt = hag[valid_pts]
                                    p_pt = planarity_05_raw[valid_pts]
                                    v_pt = verticality_05_raw[valid_pts]

                                    is_roof = (h_pt > 0.45) & (p_pt > 0.105) & (v_pt < 0.88)
                                    is_low_deck_roof = (h_pt > -0.20) & (h_pt < 1.55) & (p_pt > 0.165) & (v_pt < 0.72)
                                    is_wall = (h_pt > 0.12) & (h_pt < 4.60) & (v_pt > 0.22) & (p_pt > 0.040)

                                    # Need real Building support, otherwise this becomes tree growth.
                                    to_building_mask = (
                                        (is_roof & (b_ratio >= 0.28) & ((high_ratio < 0.68) | (b_ratio >= 0.48))) |
                                        (is_low_deck_roof & (b_ratio >= 0.36) & (veg_ratio < 0.72)) |
                                        (is_wall & (b_ratio >= 0.42) & (high_ratio < 0.44))
                                    )
                                    to_building = valid_pts[to_building_mask]
                                else:
                                    to_building = valid_pts

                                if len(to_building) > 0:
                                    predictions[to_building] = BUILDING
                                    roof_wall_rescue_count = int(len(to_building))

                    fix_report["FENCE roof/wall geometry rescue"] = roof_wall_rescue_count
                    log.info(
                        f"  FENCE ROOF/WALL GEOMETRY RESCUE: recovered "
                        f"{roof_wall_rescue_count:,} roof/wall points from Ground/Vegetation"
                    )
                except Exception as roof_wall_rescue_exc:
                    fix_report["FENCE roof/wall geometry rescue"] = 0
                    log.warning(
                        f"  FENCE ROOF/WALL GEOMETRY RESCUE skipped: {roof_wall_rescue_exc}"
                    )

                # Fence/SNT footprint-based building completion.
                # This is the safe replacement for the old broad gap recovery:
                # - uses parent-HAG and current Building seeds
                # - grows only inside/next to the compact Building footprint
                # - requires roof/wall geometry and local Building support
                # - still rejects tree/canopy dominated neighborhoods
                try:
                    footprint_fix_count = 0
                    active_fp_idx = np.asarray(postprocess_indices, dtype=np.int64)
                    active_fp_idx = active_fp_idx[
                        (active_fp_idx >= 0) & (active_fp_idx < len(predictions))
                    ]

                    if len(active_fp_idx) > 0:
                        # Restrict seeds to exact fence target rows only. Context points
                        # are support for features, never seeds for fence completion.
                        active_building_idx = active_fp_idx[predictions[active_fp_idx] == BUILDING]

                        # Need a real building seed before growing. This avoids converting
                        # isolated vegetation/tree blobs when the model has no building.
                        if len(active_building_idx) >= 40:
                            if 'tree_all' not in locals() or tree_all is None:
                                tree_all = cKDTree(xyz)

                            for grow_pass in range(1, 3):
                                active_building_idx = active_fp_idx[predictions[active_fp_idx] == BUILDING]
                                if len(active_building_idx) < 40:
                                    break

                                bldg_tree_2d = cKDTree(xyz[active_building_idx][:, :2])

                                cand_mask = (
                                    (predictions[active_fp_idx] != BUILDING) &
                                    np.isin(predictions[active_fp_idx], [GROUND, LOWVEG, MIDVEG, HIGHVEG])
                                )
                                cand_idx = active_fp_idx[cand_mask]
                                if len(cand_idx) == 0:
                                    break

                                d_seed, nn_seed = safe_query(
                                    bldg_tree_2d,
                                    xyz[cand_idx][:, :2],
                                    k=1,
                                )
                                d_seed = np.asarray(d_seed, dtype=np.float32).reshape(-1)
                                nn_seed = np.asarray(nn_seed, dtype=np.int64).reshape(-1)
                                near_seed_idx = active_building_idx[nn_seed]
                                near_seed_hag = hag[near_seed_idx]

                                ch = hag[cand_idx]
                                cp = planarity_05_raw[cand_idx]
                                cv = verticality_05_raw[cand_idx]
                                cc = confidence[cand_idx]
                                ccls = predictions[cand_idx]

                                # Pass 1 is tight. Pass 2 can cross a small roof gap,
                                # but still needs stronger shape/support checks below.
                                max_d = 1.65 if grow_pass == 1 else 2.35

                                roof_body = (
                                    (d_seed < max_d) &
                                    (ch > 0.28) &
                                    (ch < near_seed_hag + 1.65) &
                                    (ch > near_seed_hag - 1.85) &
                                    (cp > 0.080) &
                                    (cv < 0.88) &
                                    (cc > 0.24) &
                                    np.isin(ccls, [GROUND, MIDVEG, HIGHVEG])
                                )

                                low_roof_deck = (
                                    (d_seed < min(max_d, 1.45)) &
                                    (ch > -0.25) &
                                    (ch < 1.65) &
                                    (cp > 0.150) &
                                    (cv < 0.72) &
                                    (cc > 0.24) &
                                    np.isin(ccls, [GROUND, MIDVEG, HIGHVEG])
                                )

                                wall_body = (
                                    (d_seed < 1.10) &
                                    (ch > 0.10) &
                                    (ch < near_seed_hag + 1.35) &
                                    (cv > 0.18) &
                                    (cp > 0.035) &
                                    (cc > 0.25) &
                                    np.isin(ccls, [LOWVEG, MIDVEG, HIGHVEG])
                                )

                                fp_candidates = cand_idx[roof_body | low_roof_deck | wall_body]
                                if len(fp_candidates) == 0:
                                    break

                                if len(fp_candidates) > 45_000:
                                    score = (
                                        1.60 * planarity_05_raw[fp_candidates] +
                                        0.55 * verticality_05_raw[fp_candidates] +
                                        0.20 * np.clip(hag[fp_candidates], 0.0, 6.0)
                                    )
                                    fp_candidates = fp_candidates[np.argsort(-score)[:45_000]]

                                # Vectorized (was a per-point Python loop with several
                                # numpy calls per iteration — this was the single largest
                                # cost in fence post-processing, since fp_candidates can
                                # reach 45,000 points per pass). Same thresholds, same
                                # resulting point set: the original's "continue" calls
                                # only skipped now-redundant checks once a point already
                                # qualified, they never changed which points end up
                                # added, so this is the union of the same three branches
                                # gated by the same tree-rejection check.
                                valid_pts, b_ratio, high_ratio, veg_ratio = batched_neighbor_class_ratios(
                                    tree_all, fp_candidates, xyz, predictions, radius=2.10,
                                    min_neighbors=12, building_code=BUILDING,
                                    highveg_code=HIGHVEG, veg_codes=[LOWVEG, MIDVEG, HIGHVEG],
                                )

                                if len(valid_pts) > 0:
                                    h_pt = hag[valid_pts]
                                    p_pt = planarity_05_raw[valid_pts]
                                    v_pt = verticality_05_raw[valid_pts]
                                    cls_pt = predictions[valid_pts]

                                    roof_like = (h_pt > 0.28) & (p_pt > 0.080) & (v_pt < 0.88)
                                    low_deck_like = (h_pt > -0.25) & (h_pt < 1.65) & (p_pt > 0.150) & (v_pt < 0.72)
                                    wall_like = (h_pt > 0.10) & (v_pt > 0.18) & (p_pt > 0.035)

                                    # Tree/canopy rejection. HighVeg can be a roof in bad raw output,
                                    # but only keep it if it is planar or has strong building support.
                                    tree_like = (
                                        ((high_ratio > 0.62) & (b_ratio < 0.32) & (p_pt < 0.145)) |
                                        ((veg_ratio > 0.82) & (b_ratio < 0.24) & (p_pt < 0.130))
                                    )

                                    roof_full = (
                                        roof_like & (b_ratio >= 0.18) &
                                        ((cls_pt != HIGHVEG) | (p_pt > 0.120) | (b_ratio >= 0.30))
                                    )
                                    low_deck_full = low_deck_like & (b_ratio >= 0.30) & (veg_ratio < 0.76)
                                    wall_full = wall_like & (b_ratio >= 0.30) & (high_ratio < 0.55)

                                    to_building_mask = (~tree_like) & (roof_full | low_deck_full | wall_full)
                                    to_building = valid_pts[to_building_mask]
                                else:
                                    to_building = valid_pts

                                if len(to_building) == 0:
                                    break

                                predictions[to_building] = BUILDING
                                footprint_fix_count += int(len(to_building))

                                log.info(
                                    f"  FENCE BUILDING FOOTPRINT COMPLETION pass {grow_pass}: "
                                    f"recovered {len(to_building):,} roof/body/wall points"
                                )

                                # If pass is no longer adding meaningful structure, stop.
                                if len(to_building) < 80:
                                    break

                    fix_report["FENCE building footprint completion"] = footprint_fix_count
                    log.info(
                        f"  FENCE BUILDING FOOTPRINT COMPLETION: recovered "
                        f"{footprint_fix_count:,} total roof/body/wall points"
                    )
                except Exception as footprint_completion_exc:
                    fix_report["FENCE building footprint completion"] = 0
                    log.warning(
                        f"  FENCE BUILDING FOOTPRINT COMPLETION skipped: {footprint_completion_exc}"
                    )

                # Fence/SNT final structural gap recovery.
                # STRICT FINISH: previous human-finish body growth caused Building
                # leakage into trees/road/courtyard. Keep this pass disabled until
                # training/data support is improved.
                try:
                    gap_fix_count = 0
                    fix_report["FENCE final roof/wall/base gap recovery"] = gap_fix_count
                    log.info(
                        "  FENCE FINAL GAP RECOVERY: disabled in strict fresh mode "
                        "to prevent Building leakage"
                    )
                    raise StopIteration("strict finish gap recovery intentionally disabled")
                    active_gap_idx = np.asarray(postprocess_indices, dtype=np.int64)
                    active_gap_idx = active_gap_idx[
                        (active_gap_idx >= 0) & (active_gap_idx < len(predictions))
                    ]

                    current_building_idx = np.where(predictions == BUILDING)[0]
                    gap_candidates = active_gap_idx[predictions[active_gap_idx] != BUILDING]

                    gap_fix_count = 0

                    if len(current_building_idx) > 0 and len(gap_candidates) > 0:
                        bldg_tree_2d = cKDTree(xyz[current_building_idx][:, :2])
                        d_cur_bldg, _ = safe_query(
                            bldg_tree_2d,
                            xyz[gap_candidates][:, :2],
                            k=1,
                        )
                        d_cur_bldg = np.asarray(d_cur_bldg, dtype=np.float32).reshape(-1)

                        gh = hag[gap_candidates]
                        gp = planarity_05_raw[gap_candidates]
                        gv = verticality_05_raw[gap_candidates]
                        gc_conf = confidence[gap_candidates]
                        gcls = predictions[gap_candidates]

                        # First-stage geometry gate. Keep it intentionally narrow.
                        roof_gap_seed = (
                            (d_cur_bldg < 3.35) &
                            (gh > 0.45) &
                            (gp > 0.040) &
                            (gv < 0.95) &
                            (gc_conf > 0.25) &
                            np.isin(gcls, [GROUND, MIDVEG, HIGHVEG])
                        )

                        wall_gap_seed = (
                            (d_cur_bldg < 1.95) &
                            (gh > -0.10) &
                            (gh < 5.80) &
                            ((gv > 0.075) | (gp > 0.012)) &
                            (gc_conf > 0.20) &
                            np.isin(gcls, [GROUND, LOWVEG, MIDVEG, HIGHVEG])
                        )

                        base_gap_seed = (
                            (d_cur_bldg < 1.35) &
                            (gh > -0.45) &
                            (gh < 2.60) &
                            ((gv > 0.055) | (gp > 0.008)) &
                            np.isin(gcls, [GROUND, LOWVEG, MIDVEG])
                        )

                        gap_idx = gap_candidates[roof_gap_seed | wall_gap_seed | base_gap_seed]

                        if len(gap_idx) > 0:
                            # Cap by structural score so this pass cannot grow into trees.
                            if len(gap_idx) > 40_000:
                                score = (
                                    1.35 * planarity_05_raw[gap_idx] +
                                    0.90 * verticality_05_raw[gap_idx] +
                                    0.25 * np.clip(hag[gap_idx], 0.0, 5.0)
                                )
                                keep = np.argsort(-score)[:40_000]
                                gap_idx = gap_idx[keep]

                            if 'tree_all' not in locals() or tree_all is None:
                                tree_all = cKDTree(xyz)

                            nlist = safe_query_ball_point(tree_all, xyz[gap_idx], r=1.55)
                            to_building = []

                            for pt, nb in zip(gap_idx, nlist):
                                nb = np.asarray(nb, dtype=np.int64)
                                if len(nb) < 8:
                                    continue

                                nb_pred = predictions[nb]
                                b_ratio = (nb_pred == BUILDING).sum() / len(nb)
                                veg_ratio = np.isin(nb_pred, [LOWVEG, MIDVEG, HIGHVEG]).sum() / len(nb)
                                high_ratio = (nb_pred == HIGHVEG).sum() / len(nb)

                                h_pt = hag[pt]
                                p_pt = planarity_05_raw[pt]
                                v_pt = verticality_05_raw[pt]
                                cls_pt = predictions[pt]

                                roof_gap = (
                                    h_pt > 0.50 and
                                    p_pt > 0.045 and
                                    v_pt < 0.96 and
                                    b_ratio >= 0.14 and
                                    high_ratio < 0.82
                                )

                                wall_gap = (
                                    h_pt > -0.05 and
                                    h_pt < 5.80 and
                                    ((v_pt > 0.080 and p_pt > 0.010) or (p_pt > 0.028) or (v_pt > 0.18)) and
                                    b_ratio >= 0.18 and
                                    high_ratio < 0.64
                                )

                                base_gap = (
                                    h_pt > -0.45 and
                                    h_pt < 2.60 and
                                    cls_pt in (GROUND, LOWVEG, MIDVEG) and
                                    b_ratio >= 0.22 and
                                    veg_ratio < 0.86
                                )

                                if roof_gap or wall_gap or base_gap:
                                    to_building.append(pt)

                            if to_building:
                                to_building = np.asarray(to_building, dtype=np.int64)
                                predictions[to_building] = BUILDING
                                gap_fix_count = int(len(to_building))

                        # Second pass: human-like building body finish.
                        # This fills coherent facade/body strips between recovered roof and base,
                        # but still uses only exact fence target points and local Building support.
                        current_building_idx = np.where(predictions == BUILDING)[0]
                        body_candidates = active_gap_idx[predictions[active_gap_idx] != BUILDING]

                        if len(current_building_idx) > 0 and len(body_candidates) > 0:
                            bldg_tree_2d = cKDTree(xyz[current_building_idx][:, :2])
                            d_body, _ = safe_query(
                                bldg_tree_2d,
                                xyz[body_candidates][:, :2],
                                k=1,
                            )
                            d_body = np.asarray(d_body, dtype=np.float32).reshape(-1)

                            bh = hag[body_candidates]
                            bp = planarity_05_raw[body_candidates]
                            bv = verticality_05_raw[body_candidates]
                            bcls = predictions[body_candidates]

                            body_seed = (
                                (d_body < 2.35) &
                                (bh > -0.35) &
                                (bh < 6.25) &
                                np.isin(bcls, [GROUND, LOWVEG, MIDVEG, HIGHVEG]) &
                                (
                                    ((bh > 0.35) & (bp > 0.020)) |
                                    ((bh > -0.10) & (bv > 0.075)) |
                                    ((d_body < 1.15) & (bh < 2.75))
                                )
                            )

                            body_idx = body_candidates[body_seed]

                            if len(body_idx) > 0:
                                if len(body_idx) > 35_000:
                                    score = (
                                        1.10 * planarity_05_raw[body_idx] +
                                        1.00 * verticality_05_raw[body_idx] +
                                        0.20 * np.clip(hag[body_idx], 0.0, 4.0)
                                    )
                                    keep = np.argsort(-score)[:35_000]
                                    body_idx = body_idx[keep]

                                nlist_body = safe_query_ball_point(tree_all, xyz[body_idx], r=2.10)
                                body_to_building = []

                                for pt, nb in zip(body_idx, nlist_body):
                                    nb = np.asarray(nb, dtype=np.int64)
                                    if len(nb) < 10:
                                        continue

                                    nb_pred = predictions[nb]
                                    b_ratio = (nb_pred == BUILDING).sum() / len(nb)
                                    high_ratio = (nb_pred == HIGHVEG).sum() / len(nb)
                                    veg_ratio = np.isin(nb_pred, [LOWVEG, MIDVEG, HIGHVEG]).sum() / len(nb)
                                    ground_ratio = (nb_pred == GROUND).sum() / len(nb)

                                    h_pt = hag[pt]
                                    p_pt = planarity_05_raw[pt]
                                    v_pt = verticality_05_raw[pt]
                                    cls_pt = predictions[pt]

                                    # Main facade/body acceptance. This recovers the visible wall bands
                                    # seen in cross-section while preventing large tree/canopy blobs.
                                    supported_body = (
                                        b_ratio >= 0.20 and
                                        high_ratio < 0.70 and
                                        (
                                            (h_pt > 0.25 and (v_pt > 0.085 or p_pt > 0.018)) or
                                            (h_pt > 0.85 and p_pt > 0.040) or
                                            (cls_pt == GROUND and ground_ratio < 0.72 and v_pt > 0.060)
                                        )
                                    )

                                    close_base_or_wall = (
                                        b_ratio >= 0.28 and
                                        h_pt > -0.35 and
                                        h_pt < 2.85 and
                                        veg_ratio < 0.88 and
                                        (v_pt > 0.055 or p_pt > 0.010)
                                    )

                                    if supported_body or close_base_or_wall:
                                        body_to_building.append(pt)

                                if body_to_building:
                                    body_to_building = np.asarray(body_to_building, dtype=np.int64)
                                    predictions[body_to_building] = BUILDING
                                    gap_fix_count += int(len(body_to_building))

                    fix_report["FENCE final roof/wall/base gap recovery"] = gap_fix_count
                    log.info(
                        f"  FENCE FINAL GAP RECOVERY: recovered {gap_fix_count:,} "
                        f"roof/wall/base points from Ground/Vegetation"
                    )

                except StopIteration:
                    pass
                except Exception as gap_e:
                    fix_report["FENCE final roof/wall/base gap recovery"] = 0
                    log.warning(f"  FENCE FINAL GAP RECOVERY skipped: {gap_e}")

            log.info(f"    Total V19 target-only fixes: {sum(fix_report.values()):,} ({time.time()-t_pp:.1f}s)")
        else:
            log.info("  LIGHT GEOMETRY CORRECTION DISABLED")
            log.info("  Raw model predictions will be written.")
            fix_report = {}

        # Keep the lightweight geometry arrays until optional QC/Power post-passes
        # finish. The large geom_ds matrix was already freed above.

        # Safety guard: if any correction accidentally collapses the class
        # distribution, revert to raw model predictions instead of writing bad data.
        try:
            pre_u, pre_c = np.unique(pre_post_predictions, return_counts=True)
            post_u, post_c = np.unique(predictions, return_counts=True)
            pre_dist = dict(zip(pre_u.tolist(), pre_c.tolist()))
            post_dist = dict(zip(post_u.tolist(), post_c.tolist()))
            pre_build = float(pre_dist.get(BUILDING, 0)) / float(n_total)
            post_build = float(post_dist.get(BUILDING, 0)) / float(n_total)
            pre_high = float(pre_dist.get(HIGHVEG, 0)) / float(n_total)
            post_high = float(post_dist.get(HIGHVEG, 0)) / float(n_total)

            suspicious_collapse = (
                len(pre_u) >= 3 and
                (
                    (post_high >= 0.97 and pre_high <= 0.85) or
                    (post_build >= 0.85 and pre_build <= 0.55) or
                    (post_build <= 0.005 and pre_build >= 0.05)
                )
            )
            if suspicious_collapse:
                log.warning("  V18 target-only correction collapse detected.")
                log.warning(
                    f"    pre_build={pre_build*100:.1f}% post_build={post_build*100:.1f}% "
                    f"pre_highveg={pre_high*100:.1f}% post_highveg={post_high*100:.1f}%"
                )
                log.warning("    Reverting to raw model predictions.")
                predictions = pre_post_predictions
                fix_report = {"V18 correction reverted by collapse guard": 1}
        except Exception as collapse_e:
            log.warning(f"  Collapse guard skipped: {collapse_e}")

    # =================================================================
    # ADVANCED QC POST-PASS (Premium-style philosophy, model untouched)
    # =================================================================
    try:
        predictions, qc_post_report = apply_advanced_qc(
            predictions,
            confidence,
            xyz,
            hag,
            active_indices=postprocess_indices,
            config=advanced_config,
            log=log,
        )
        fix_report["Advanced QC Uncategorized"] = int(qc_post_report.get("uncategorized", 0))
        fix_report["Advanced QC Low Point"] = int(qc_post_report.get("low_point", 0))
        fix_report["Advanced QC High Noise"] = int(qc_post_report.get("high_noise", 0))
    except Exception as qc_exc:
        # Fail-open: keep the successful Advanced model result.
        log.warning(f"  Advanced QC post-pass skipped after error: {qc_exc}")

    # =================================================================
    # POWER ASSET POST-PASS
    # =================================================================
    if enable_power_lines:
        try:
            predictions, power_report = apply_power_asset_postprocess(
                xyz,
                predictions,
                hag,
                wire_candidate_codes=(LOWVEG, MIDVEG, HIGHVEG),
                pole_candidate_codes=(MIDVEG, HIGHVEG, BUILDING),
                protected_codes=(
                    int(ADV_QC_UNCATEGORIZED),
                    int(ADV_QC_LOW_POINT),
                    int(ADV_QC_HIGH_NOISE),
                ),
                wire_output_code=ADV_POWER_WIRE_INTERNAL,
                pole_output_code=ADV_POWER_POLE_INTERNAL,
                active_indices=postprocess_indices,
                config=advanced_config,
                cl_coords=advanced_config.get("cl_corridor_coords"),
                cl_width=float(advanced_config.get(
                    "cl_corridor_width",
                    advanced_config.get("power_corridor_width", 0.0),
                ) or 0.0),
                linearity_05=locals().get("linearity_05_raw"),
                planarity_05=locals().get("planarity_05_raw"),
                verticality_05=locals().get("verticality_05_raw"),
                linearity_10=locals().get("linearity_10_raw"),
                verticality_10=locals().get("verticality_10_raw"),
                log=log,
            )
            fix_report["Advanced Wire/Bare Conductor"] = int(power_report.get("wires_final", 0))
            fix_report["Advanced Pole"] = int(power_report.get("poles_final", 0))
        except Exception as power_exc:
            # Fail-open: never destroy successful base classifications.
            log.warning(f"  Advanced power post-pass skipped after error: {power_exc}")

    # Free retained lightweight geometry arrays now.
    for _name in (
        "linearity_05_raw", "planarity_05_raw", "verticality_05_raw",
        "linearity_10_raw", "verticality_10_raw",
    ):
        try:
            del locals()[_name]
        except Exception:
            pass
    gc.collect()

    # ── Write output ──────────────────────────────────────────────────
    try:
        # Base model remains 0..4 internally. Optional post-passes add:
        #   5   Wire/Bare Conductor
        #   6   Pole
        #   250 Uncategorized (Ground-only uncertainty)
        #   251 Low Point
        #   252 High Noise
        # Standard <=31 codes are written first. PTC/custom codes are applied
        # later by the worker so legacy LAS point formats cannot overflow here.
        asprs_classes = np.empty(len(predictions), dtype=np.uint8)
        base_mask = predictions <= BUILDING
        output_map = np.array([1, 2, 3, 4, 5], dtype=np.uint8)
        asprs_classes[base_mask] = output_map[predictions[base_mask]]

        asprs_classes[predictions == ADV_POWER_WIRE_INTERNAL] = 14
        asprs_classes[predictions == ADV_POWER_POLE_INTERNAL] = 15
        asprs_classes[predictions == ADV_QC_UNCATEGORIZED] = 0
        asprs_classes[predictions == ADV_QC_LOW_POINT] = 7
        asprs_classes[predictions == ADV_QC_HIGH_NOISE] = 18

        valid_internal = (
            base_mask
            | (predictions == ADV_POWER_WIRE_INTERNAL)
            | (predictions == ADV_POWER_POLE_INTERNAL)
            | (predictions == ADV_QC_UNCATEGORIZED)
            | (predictions == ADV_QC_LOW_POINT)
            | (predictions == ADV_QC_HIGH_NOISE)
        )
        if not np.all(valid_internal):
            bad = np.unique(predictions[~valid_internal]).tolist()
            raise RuntimeError(f"Unexpected Advanced internal classes before output: {bad}")

        log.info("  NAKSHA ADVANCED FINAL STANDARD MAP:")
        log.info("    Ground/Vegetation/Building -> GUI 1..5")
        log.info("    Uncategorized -> 0 | Low Point -> 7 | High Noise -> 18")
        if enable_power_lines:
            log.info("    Wire/Bare Conductor -> 14 | Pole -> 15 (PTC/custom remap follows)")
    except Exception as e:
        log.error(f"  GUI output mapping failed: {e}")
        return False

    quality = check_output_quality(asprs_classes, confidence, n_total, log)

    if not quality.get("pass", False):
        log.warning(
            "  QC failed, but output write will continue. "
            "Review the JSON/log report if classification looks suspicious."
        )

    log.info("  Writing output.")

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        las.classification = asprs_classes
        tmp_path = output_path.with_name(
            f"{output_path.stem}.{os.getpid()}.{threading.get_ident()}.tmp{output_path.suffix}"
        )
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        las.write(str(tmp_path))
        shutil.move(str(tmp_path), str(output_path))

        if not output_path.exists():
            raise FileNotFoundError(f"Output still missing after write: {output_path}")
    except KeyboardInterrupt:
        raise
    except Exception as e:
        log.error(f"  Output write failed: {e}")
        return False

    elapsed = time.time() - t_start

    log.info("")
    log.info("  -- Classification results ----------------------")
    unique, counts = np.unique(asprs_classes, return_counts=True)
    for cls, cnt in zip(unique, counts):
        log.info(f"    {ASPRS_NAMES.get(int(cls),'?'):>10}: "
                 f"{cnt:>12,} ({100*cnt/n_total:.1f}%)")

    log.info("")
    log.info("  -- Post-processing fixes -----------------------")
    for k, v in fix_report.items():
        log.info(f"    {k}: {v:,}")

    voted_conf = confidence[has_votes]
    log.info("")
    log.info("  -- Confidence ----------------------------------")
    log.info(f"    mean={voted_conf.mean():.3f} "
             f">=0.9: {(voted_conf>=0.9).sum():,} "
             f"<{CONFIDENCE_FLAG_THRESH}: {(confidence<CONFIDENCE_FLAG_THRESH).sum():,}")
    log.info(f"\n  Output : {output_path}")
    log.info(f"  Runtime: {elapsed:.1f}s ({n_total/max(elapsed,1):.0f} pts/sec)")

    write_json_report(output_dir, input_path.name, validation, fix_report,
                      quality, elapsed, n_total, log)

    # IMPORTANT:
    # Quality check is only a warning/report gate, not a loader gate.
    # The output was already written successfully above. Returning False here
    # makes the GUI throw "Advanced AI QC failed" and prevents loading a valid
    # classified file into the software.
    if not bool(quality.get("pass", True)):
        log.warning(
            "  QC did not pass, but output was written successfully. "
            "Loading will continue; check the JSON/log report for details."
        )

    return True


# =====================================================================
# ENTRY POINT
# =====================================================================
# =====================================================================
# QT PROGRESS HANDLER FOR ADVANCED AI
# Reads existing log messages and moves progress bar step-by-step.
# No accuracy change. Only UI progress update.
# =====================================================================

class _AdvancedQtProgressHandler(logging.Handler):
    def __init__(self, progress_signal):
        super().__init__(logging.INFO)
        self.progress_signal = progress_signal
        self._last_percent = 0
        self._last_emit_time = 0.0

    def _safe_emit(self, percent, message):
        try:
            percent = int(max(0, min(94, percent)))

            if percent < self._last_percent:
                percent = self._last_percent

            now = time.time()

            if percent != self._last_percent or (now - self._last_emit_time) > 1.0:
                self._last_percent = percent
                self._last_emit_time = now
                self.progress_signal.emit(percent, message)
        except Exception:
            pass

    def emit(self, record):
        try:
            msg = record.getMessage()
            low = msg.lower()

            if "validated:" in low:
                self._safe_emit(8, "Validated input point cloud...")
                return

            if "[1/5] hag" in low or "hag (csf" in low:
                self._safe_emit(10, "Step 1/8: Computing HAG ground model...")
                return

            if "csf ground points" in low:
                self._safe_emit(16, "Step 1/8: HAG ground points extracted...")
                return

            if "[2/5] geometric" in low or "geometric features" in low:
                self._safe_emit(20, "Step 2/8: Computing geometric features...")
                return

            m = re.search(
                r"START\s+Scale\s+index\s*=\s*(\d+)\s*/\s*(\d+)",
                msg,
                re.IGNORECASE
            )
            if m:
                cur = int(m.group(1))
                total = max(1, int(m.group(2)))
                pct = 20 + int(20 * (cur - 1) / total)
                self._safe_emit(
                    pct,
                    f"Step 2/8: Geometric features scale {cur}/{total}..."
                )
                return

            m = re.search(
                r"DONE\s+Scale\s+index\s*=\s*(\d+)\s*/\s*(\d+)",
                msg,
                re.IGNORECASE
            )
            if m:
                cur = int(m.group(1))
                total = max(1, int(m.group(2)))
                pct = 20 + int(25 * cur / total)
                self._safe_emit(
                    pct,
                    f"Step 2/8: Geometric features scale {cur}/{total} done..."
                )
                return

            if "[3/5] density" in low or "density" in low:
                self._safe_emit(47, "Step 3/8: Computing density...")
                return

            if "[4/5] roughness" in low or "roughness" in low:
                self._safe_emit(53, "Step 4/8: Computing roughness...")
                return

            if "tile index" in low or "precomputing tile" in low:
                self._safe_emit(58, "Step 5/8: Building tile index...")
                return

            if "[5/5] tile inference" in low or "running inference" in low:
                self._safe_emit(62, "Step 6/8: Running tile inference...")
                return

            m = re.search(r"tile\s+(\d+)\s*/\s*(\d+)", msg, re.IGNORECASE)
            if m:
                cur = int(m.group(1))
                total = max(1, int(m.group(2)))
                pct = 62 + int(18 * cur / total)
                self._safe_emit(
                    pct,
                    f"Step 6/8: Tile inference {cur}/{total}..."
                )
                return

            if "preparing post-processing" in low:
                self._safe_emit(80, "Preparing post-processing...")
                return

            if "post-processing..." in low:
                self._safe_emit(82, "Step 7/8: Post-processing started...")
                return

            m = re.search(r"Step\s+(\d+)\s*:", msg, re.IGNORECASE)
            if m:
                step_no = int(m.group(1))
                pct = 82 + int(min(10, step_no))
                self._safe_emit(
                    pct,
                    f"Step 7/8: Post-processing step {step_no}/8..."
                )
                return

            if "writing output" in low:
                self._safe_emit(94, "Step 8/8: Writing classified output...")
                return

            if "output :" in low or "runtime:" in low or "report:" in low:
                self._safe_emit(94, "Step 8/8: Finalizing classified output...")
                return

        except Exception:
            pass

class AdvancedInferenceWorker(QThread):
    progress = Signal(int, str)
    finished = Signal()
    error = Signal(str)

    def __init__(self, data_dict, class_mapping, power_mapping=None,
             advanced_config=None, enable_power_lines=False,
             target_indices=None):
        super().__init__()
        self._data_dict_ref = data_dict
        self.class_mapping = dict(class_mapping or {})
        self.power_mapping = dict(power_mapping or {})
        self.advanced_config = advanced_config or {}
        self.enable_power_lines = bool(enable_power_lines)
        self.target_indices = (
            None if target_indices is None
            else np.asarray(target_indices, dtype=np.int64)
        )
        self._cancel_requested = threading.Event()
        self.model = None
        self.device = None

    def cancel(self):
        self._cancel_requested.set()

    def _check_cancel(self):
        if self._cancel_requested.is_set():
            raise InterruptedError("Cancelled by user")

    def run(self):
        try:
            self.progress.emit(1, "Loading Advanced AI model...")
            if not HAS_JAKTERISTICS:
                raise RuntimeError("jakteristics is required for Advanced AI.")
            if not HAS_CSF:
                raise RuntimeError("CSF is required for Advanced AI.")

            # NAKSHA_GPU_PHASE11_V3_20_ADVANCED DEVICE BEGIN
            global INFERENCE_BATCH_SIZE
            self.device, _naksha_gpu_report = naksha_select_torch_device(prefer_cuda=True)
            naksha_configure_torch_for_accuracy(self.device)
            INFERENCE_BATCH_SIZE = naksha_recommended_advanced_batch_size(
                self.device, default=INFERENCE_BATCH_SIZE
            )
            _naksha_gpu_log = logging.getLogger("inference")
            for _naksha_line in naksha_gpu_report_lines(
                _naksha_gpu_report, prefix="Advanced GPU Phase11"
            ):
                _naksha_gpu_log.info(_naksha_line)
            _naksha_gpu_log.info(
                f"Advanced GPU Phase11 tile batch: {INFERENCE_BATCH_SIZE} | "
                f"AMP={naksha_should_use_amp(self.device)}"
            )
            # NAKSHA_GPU_PHASE11_V3_20_ADVANCED DEVICE END
            check_gpu(self.device, logging.getLogger("inference"))
            self.model, feat_mean, feat_std = load_advanced_model(self.device)
            self._check_cancel()

            source_file = self._data_dict_ref.get("_source_file_path")
            if not source_file:
                raise RuntimeError(
                    "Source LAZ/LAS path missing. Advanced AI needs original file path to save classified output."
                )

            input_path = Path(source_file)
            if not input_path.exists():
                raise FileNotFoundError(f"Source file not found: {input_path}")

            output_dir = input_path.parent / ADVANCED_OUTPUT_FOLDER_NAME
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / f"{input_path.stem}_advanced_classified{input_path.suffix}"

            log = setup_logging(output_dir)

            qt_progress_handler = _AdvancedQtProgressHandler(self.progress)
            log.addHandler(qt_progress_handler)

            log.info("ADVANCED AI SOFTWARE WORKER START")
            log.info(f"Input : {input_path}")
            log.info(f"Output: {output_path}")
            log.info(f"Model : {MODEL_PATH}")
            for _naksha_line in naksha_gpu_report_lines(
                _naksha_gpu_report, prefix="Advanced GPU Phase11"
            ):
                log.info(_naksha_line)
            check_gpu(self.device, log)
            log.info(
                f"Advanced GPU Phase11 requested tile batch: {INFERENCE_BATCH_SIZE} | "
                f"AMP={naksha_should_use_amp(self.device)}"
            )

            self.progress.emit(5, "Starting Advanced AI classification...")

            # Never load stale output from a previous failed/partial run.
            try:
                if output_path.exists():
                    output_path.unlink()
            except Exception as e:
                log.warning(f"Could not remove previous Advanced AI output before run: {e}")

            run_target_indices = self.target_indices

            try:
                source_point_indices = self._data_dict_ref.get("_fence_source_point_indices")
                xyz_viewer = self._data_dict_ref.get("xyz")

                if (
                    self.target_indices is not None
                    and source_point_indices is not None
                    and xyz_viewer is not None
                ):
                    source_point_indices = np.asarray(source_point_indices, dtype=np.int64)
                    viewer_target = np.asarray(self.target_indices, dtype=np.int64)

                    if (
                        len(source_point_indices) == len(xyz_viewer)
                        and viewer_target.size > 0
                        and np.all(viewer_target >= 0)
                        and np.all(viewer_target < len(source_point_indices))
                    ):
                        run_target_indices = source_point_indices[viewer_target]

                        log.info(
                            "  SNT/FENCE SOURCE INDEX MAP ACTIVE: "
                            f"viewer targets={len(viewer_target):,}, "
                            f"source targets={len(run_target_indices):,}"
                        )

            except Exception as map_exc:
                log.warning(f"  SNT/FENCE source-index mapping skipped: {map_exc}")
                run_target_indices = self.target_indices

            ptc_active = bool(
                self.advanced_config.get(
                    "_ptc_active",
                    False,
                )
            )

            print(
                f"[PTC] Advanced active state: {ptc_active}",
                flush=True,
            )

            ok = classify_laz(
                input_path=input_path,
                output_path=output_path,
                output_dir=output_dir,
                model=self.model,
                device=self.device,
                feat_mean=feat_mean,
                feat_std=feat_std,
                log=log,

                # Only expose the PTC mapping to the fence helper when a PTC
                # was explicitly activated through Display Mode -> Apply.
                class_mapping=(
                    self.class_mapping
                    if ptc_active
                    else None
                ),

                target_indices=run_target_indices,
                target_classes=self.advanced_config.get(
                    "advanced_target_classes",
                    [0, 1, 2, 3, 4],
                ),
                power_mapping=self.power_mapping,
                enable_power_lines=self.enable_power_lines,
                advanced_config=self.advanced_config,
            )
            self._check_cancel()

            try:
                log.removeHandler(qt_progress_handler)
            except Exception:
                pass

            if not ok:
                # classify_laz returns False only for hard failures in old builds.
                # Do not stop here if a classified output was written; load it below.
                print(
                    "Advanced AI warning: QC/log check returned False. "
                    "Will load output if the file exists."
                )

            if not output_path.exists():
                # Fence mode writes final output inside _fence_ai_temp/classified.
                # Try to find latest classified output before failing.
                try:
                    candidates = sorted(
                        output_dir.rglob(f"{input_path.stem}*classified{input_path.suffix}"),
                        key=lambda p: p.stat().st_mtime,
                        reverse=True,
                    )
                    if candidates:
                        print(f"Advanced AI output path corrected: {candidates[0]}")
                        output_path = candidates[0]
                except Exception as e:
                    print(f"Advanced AI output search failed: {e}")

            if not output_path.exists():
                raise RuntimeError(
                    f"Advanced AI finished but output file was not created: {output_path}"
                )

            # ============================================================
            # FINAL OUTPUT REMAP: PTC CORE/QC + USER/PTC POWER CODES
            # ============================================================
            if self.target_indices is None:
                combined_mapping = {}
                source_to_internal = {}

                if ptc_active:
                    combined_mapping.update(dict(self.class_mapping or {}))
                    combined_mapping.update(dict(
                        self.advanced_config.get("_ptc_optional_semantic_codes", {}) or {}
                    ))
                    source_to_internal.update({
                        1: 0, 2: 1, 3: 2, 4: 3, 5: 4,
                        0: "uncategorized",
                        7: "low_point",
                        18: "high_noise",
                    })

                if self.enable_power_lines:
                    combined_mapping.update(dict(self.power_mapping or {}))
                    source_to_internal.update({
                        14: ADV_POWER_WIRE_INTERNAL,
                        15: ADV_POWER_POLE_INTERNAL,
                    })

                if combined_mapping and source_to_internal:
                    self.progress.emit(
                        94,
                        "Applying final PTC / Power output mapping..."
                    )
                    remap_las_file_in_place(
                        output_path,
                        source_to_internal=source_to_internal,
                        class_mapping=combined_mapping,
                        chunk_size=1_000_000,
                    )
            else:
                # Fence mode already performs target-only remapping inside the
                # fence helper. Outside-fence classes are never remapped.
                print(
                    "[PTC] Advanced fence mode: target-only final mapping complete; "
                    "outside-fence classes unchanged.",
                    flush=True,
                )

            self.progress.emit(
                95,
                "Loading classified result into software..."
            )

            las = laspy.read(str(output_path))
            classified = np.array(
                las.classification,
                dtype=np.uint8
            )

            xyz = self._data_dict_ref.get("xyz")

            if xyz is not None and len(classified) != len(xyz):
                try:
                    source_point_indices = self._data_dict_ref.get("_fence_source_point_indices")

                    if source_point_indices is None:
                        raise RuntimeError("Missing _fence_source_point_indices")

                    source_point_indices = np.asarray(source_point_indices, dtype=np.int64)

                    if len(source_point_indices) != len(xyz):
                        raise RuntimeError(
                            f"Source index length mismatch: "
                            f"indices={len(source_point_indices):,}, xyz={len(xyz):,}"
                        )

                    if source_point_indices.size > 0:
                        if np.min(source_point_indices) < 0 or np.max(source_point_indices) >= len(classified):
                            raise RuntimeError(
                                f"Source indices out of classified range: "
                                f"max_index={int(np.max(source_point_indices))}, "
                                f"classified_len={len(classified):,}"
                            )

                    classified = classified[source_point_indices]

                    print(
                        "Advanced AI result remapped from source LAZ to SNT/fence viewer subset: "
                        f"{len(classified):,} points"
                    )

                except Exception as remap_exc:
                    raise RuntimeError(
                        f"Output point count mismatch. "
                        f"Software has {len(xyz):,} points, "
                        f"classified file has {len(classified):,} points. "
                        f"Viewer remap failed: {remap_exc}"
                    )

            self._data_dict_ref["classification"] = classified

            if self.target_indices is not None:
                self._data_dict_ref["_ai_last_target_indices"] = np.asarray(
                    self.target_indices,
                    dtype=np.int64,
                ).copy()
            else:
                self._data_dict_ref["_ai_last_target_indices"] = None

            self._data_dict_ref["_advanced_ai_output_path"] = str(output_path)
            self._data_dict_ref["_advanced_ai_quality_pass"] = bool(ok)

            self.progress.emit(100, "Advanced AI classification complete!")
            self.finished.emit()

        except InterruptedError:
            self.error.emit("Advanced AI classification cancelled by user")
        except Exception as e:
            self.error.emit(
                f"Advanced AI classification failed: {str(e)}\n\n{traceback.format_exc()}"
            )
        finally:
            try:
                if self.model is not None:
                    del self.model
                    self.model = None
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                gc.collect()
            except Exception:
                pass


def main():
    INPUT_PATH = r"D:\model_data\Raw_files_to_test\DX5036474_000110.laz"
    OUTPUT_DIR = r"D:\model_data\DX5036474_000110_to_test_5_11_26_FINAL_TIME_TEST"

    input_path = Path(INPUT_PATH)
    output_dir = Path(OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)

    log = setup_logging(output_dir)
    log.info("=" * 60)
    log.info("PRODUCTION INFERENCE START")
    log.info("FAST_BATCH_STEP8_CACHE VERSION ACTIVE")
    log.info(f"  INFERENCE_BATCH_SIZE : {INFERENCE_BATCH_SIZE}  (was 1)")
    log.info(f"  TILE_OVERLAP         : {TILE_OVERLAP}m  (was 15.0m)")
    log.info(f"  GEOM_VOXEL_SIZE      : {GEOM_VOXEL_SIZE}  (was 0.35)")
    log.info(f"  CHUNK_STEP           : {CHUNK_STEP}  (was {NUM_POINTS//2})")
    log.info(f"  Model : {MODEL_PATH}")
    log.info(f"  Input : {INPUT_PATH}")
    log.info(f"  Output: {OUTPUT_DIR}")
    log.info("=" * 60)

    if not HAS_JAKTERISTICS:
        log.error("FATAL: pip install jakteristics")
        sys.exit(1)
    if not HAS_CSF:
        log.error("FATAL: pip install cloth-simulation-filter")
        sys.exit(1)
    if not Path(MODEL_PATH).exists():
        log.error(f"FATAL: Model not found: {MODEL_PATH}")
        sys.exit(1)
    if not Path(STATS_PATH).exists():
        log.error(f"FATAL: Stats not found: {STATS_PATH}")
        sys.exit(1)

    log.info("Loading model...")
    # NAKSHA_GPU_PHASE11_V3_20_ADVANCED MAIN DEVICE BEGIN
    device, _naksha_gpu_report = naksha_select_torch_device(prefer_cuda=True)
    naksha_configure_torch_for_accuracy(device)
    for _naksha_line in naksha_gpu_report_lines(
        _naksha_gpu_report, prefix="Advanced GPU Phase11"
    ):
        log.info(_naksha_line)
    log.info(
        f"Advanced GPU Phase11 standalone batch: {INFERENCE_BATCH_SIZE} | "
        f"AMP={naksha_should_use_amp(device)}"
    )
    # NAKSHA_GPU_PHASE11_V3_20_ADVANCED MAIN DEVICE END
    check_gpu(device, log)

    try:
        model, feat_mean, feat_std = load_advanced_model(device)
    except Exception as e:
        log.error(f"FATAL: Cannot load advanced model - {e}")
        log.debug(traceback.format_exc())
        sys.exit(1)

    log.info(f"  Features: {EXPECTED_NUM_FEATURES}  Classes: 5  Device: {device}")

    try:
        if input_path.is_file():
            classify_laz(input_path, output_dir / input_path.name, output_dir,
                         model, device, feat_mean, feat_std, log)

        elif input_path.is_dir():
            laz_files = sorted(list(input_path.glob("*.laz")) +
                               list(input_path.glob("*.las")))
            n_files   = len(laz_files)
            log.info(f"\nBatch mode: {n_files} files")
            passed = failed = skipped = 0
            failed_files = []
            t_batch = time.time()

            for i, laz_file in enumerate(laz_files):
                out_file = output_dir / laz_file.name
                if out_file.exists():
                    log.info(f"  [{i+1}/{n_files}] SKIP: {laz_file.name}")
                    skipped += 1
                    continue
                log.info(f"\n  [{i+1}/{n_files}] Processing: {laz_file.name}")
                try:
                    ok = classify_laz(laz_file, out_file, output_dir,
                                      model, device, feat_mean, feat_std, log)
                    if ok:
                        passed += 1
                    else:
                        failed += 1
                        failed_files.append(laz_file.name + " (QC/error)")
                except KeyboardInterrupt:
                    log.warning("Batch interrupted.")
                    raise
                except Exception as e:
                    log.error(f"  FAILED: {laz_file.name} - {e}")
                    failed += 1
                    failed_files.append(laz_file.name)

                done    = passed + failed
                elapsed = time.time() - t_batch
                rate    = done / max(elapsed, 1e-6)
                rem     = (n_files - skipped - done) / max(rate, 1e-6)
                log.info(f"  Batch: {passed} passed | {failed} failed | "
                         f"{skipped} skipped | ~{rem/60:.1f}min remaining")

            log.info(f"\nBatch complete: {passed} passed | {failed} failed | {skipped} skipped")
            if failed_files:
                log.warning("Failed files:")
                for fn in failed_files:
                    log.warning(f"  {fn}")
        else:
            log.error(f"FATAL: path not found - {input_path}")
            sys.exit(1)

    except KeyboardInterrupt:
        log.warning("\nSTOPPED BY USER. Checkpoint saved. Run again to resume.")
        sys.exit(130)
    except Exception as e:
        log.error(f"FATAL unexpected error: {e}")
        log.debug(traceback.format_exc())
        sys.exit(1)
    finally:
        safe_clear(device)

    log.info("")
    log.info("=" * 60)
    log.info("INFERENCE COMPLETE")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
