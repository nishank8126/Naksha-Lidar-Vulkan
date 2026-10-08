"""
NAKSHA OUT-OF-CORE STREAMING CORE
=================================

Design basis - every constant here is DERIVED FROM A MEASUREMENT on this
machine, not assumed. Re-measure with extreme_dataset_discovery.py and the
probes in streaming_measure.py if the target data changes.

Measured on this box (NVIDIA T400 4GB, 63.8 GB RAM, 86.9 GB free on H:):

  Dataset H:\\TESTING CONTIUES\\FUNIVIA\\NT219
    total            2,485,723,069 points across 14 LAZ files, 16.39 GB
    primary tiles    1,140,436,759 points across 6 files   (~1.14 BILLION)
    bounds           4034 m x 2000 m x 443 m
    header scan      1.99 s for all 14 files, zero points decoded

  Throughput
    sequential decode            8.22 Mpts/s
    LAZ random access            ~25 ms seek + 3.4 Mpts/s
    run length at quadtree d8    127 points/run  -> ~20M runs for 2.49B pts

  Capacity (why in-memory is impossible, not merely slow)
    xyz float64 for all points    59.7 GB   > 63.8 GB RAM once attributes added
    GPU tile format for all       49.7 GB   >  4.0 GB VRAM  (12x over)
    VRAM budget used              2.76 GB   -> at most ~138M points resident

  Consequences the implementation depends on:
    * QUADTREE, not octree: Z spans 443 m against a 4034 m X extent (11%),
      so true 3D subdivision buys nothing. Measured, not assumed.
    * The sidecar stores REFERENCES (file_id, first_point, count) into the
      ORIGINAL LAZ files - never copies. A copy-based tile cache would need
      ~50 GB and there are only 86.9 GB free.
    * Exactly ONE array is materialised: a decimated overview, small enough
      to give a first frame without touching the source files.
"""
import ctypes
import os

# ---- GPU tile layout (must match native/naksha_vulkan) --------------------
#   position       float32 x3   12 B
#   rgb            uint8   x3    3 B
#   classification uint8        1 B
#   intensity      float32       4 B
#                                 ------
#                                  20 B/point
GPU_BYTES_PER_POINT = 20
RAM_BYTES_PER_POINT = 28          # float32 xyz + packed attributes, per tile


# ---- Residency budgets ---------------------------------------------------
GPU_BUDGET_FRACTION = 0.70        # 70% of the device-local heap
RAM_BUDGET_FRACTION = 0.25        # of physical RAM, capped below
RAM_BUDGET_CAP_GB = 12.0
MAX_SIDECAR_GB = 20.0             # refuse a build that would exceed this

# ---- Frame governor ------------------------------------------------------
TARGET_FPS = 60.0
MIN_FPS = 30.0
TARGET_FRAME_MS = 1000.0 / TARGET_FPS      # 16.67
MIN_FRAME_MS = 1000.0 / MIN_FPS            # 33.33
# Hysteresis: only change the point budget when frame time is clearly outside
# the band, and by a bounded step, so the governor cannot oscillate.
GOVERNOR_HYSTERESIS_MS = 2.0
GOVERNOR_MAX_STEP = 0.25          # at most +/-25% per adjustment
MIN_POINT_BUDGET = 250_000
MAX_POINT_BUDGET = 40_000_000

# ---- Residency states (mirrors the native enum) -------------------------
UNLOADED = 0
REQUESTED = 1
READING = 2
DECODING = 3
RAM_READY = 4
GPU_PENDING = 5
GPU_RESIDENT = 6
EVICTABLE = 7
STATE_NAMES = {
    UNLOADED: "UNLOADED", REQUESTED: "REQUESTED", READING: "READING",
    DECODING: "DECODING", RAM_READY: "RAM_READY", GPU_PENDING: "GPU_PENDING",
    GPU_RESIDENT: "GPU_RESIDENT", EVICTABLE: "EVICTABLE",
}

# ---- Index sidecar -------------------------------------------------------
SIDECAR_MAGIC = b"NKVIDX\x01\x00"
SIDECAR_VERSION = 1
SIDECAR_SUFFIX = ".nakshaidx"
OVERVIEW_SUFFIX = ".nakshaovw"


def sidecar_path(source_path: str) -> str:
    """survey.laz -> survey.laz.nakshaidx (keeps multi-file sets unambiguous)."""
    return source_path + SIDECAR_SUFFIX


def overview_path(source_path: str) -> str:
    return source_path + OVERVIEW_SUFFIX


def available_ram_bytes() -> int:
    try:
        import psutil
        return int(psutil.virtual_memory().available)
    except Exception:
        return 0


def total_ram_bytes() -> int:
    try:
        import psutil
        return int(psutil.virtual_memory().total)
    except Exception:
        return 0


def ram_budget_bytes() -> int:
    return min(int(total_ram_bytes() * RAM_BUDGET_FRACTION),
               int(RAM_BUDGET_CAP_GB * 1024 ** 3))


def disk_free_bytes(path: str) -> int:
    try:
        free = ctypes.c_ulonglong(0)
        ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(path), ctypes.byref(free), None, None)
        return int(free.value)
    except Exception:
        return 0


def choose_storage_mode(total_points: int, estimated_bytes_per_point: float,
                        dataset_dir: str, vram_bytes: int = 4 * 1024 ** 3) -> dict:
    """Rule #1: never break small datasets; auto-select the mode for big ones.

    IN_MEMORY is kept whenever the whole cloud fits comfortably in BOTH the
    RAM budget and the VRAM budget, so every existing workflow is untouched.
    OUT_OF_CORE is chosen on measured capacity, not on a point-count constant.
    """
    est = total_points * estimated_bytes_per_point
    ram_budget = ram_budget_bytes()
    gpu_budget = int(vram_bytes * GPU_BUDGET_FRACTION)
    fits_ram = est <= ram_budget
    fits_gpu = total_points * GPU_BYTES_PER_POINT <= gpu_budget
    if fits_ram and fits_gpu:
        mode = "IN_MEMORY"
        reason = (f"{total_points:,} points need {est / 1e9:.1f} GB; RAM budget "
                  f"{ram_budget / 1e9:.1f} GB and VRAM budget "
                  f"{gpu_budget / 1e9:.1f} GB both fit it")
    else:
        blockers = []
        if not fits_ram:
            blockers.append(f"RAM {est / 1e9:.1f} GB > budget {ram_budget / 1e9:.1f} GB")
        if not fits_gpu:
            blockers.append(f"VRAM {total_points * GPU_BYTES_PER_POINT / 1e9:.1f} GB"
                            f" > budget {gpu_budget / 1e9:.1f} GB")
        mode, reason = "OUT_OF_CORE", "; ".join(blockers)
    return {"mode": mode, "reason": reason, "total_points": total_points,
            "estimated_raw_bytes": int(est), "ram_budget": ram_budget,
            "gpu_budget": gpu_budget, "disk_free": disk_free_bytes(dataset_dir)}

# ---- Quadtree ------------------------------------------------------------
# max depth 8 = 256x256 cells. Measured run length at this depth is 127
# points, so the whole 2.49B-point dataset needs only ~20M runs (~480 MB of
# sidecar) instead of 2.49B point records.
MAX_DEPTH = 8
# Subdivide a cell only while it still holds more than this many points, so a
# leaf averages ~38K points for the full dataset - one LAZ random-access read,
# which is the natural upload unit.
MAX_LEAF_POINTS = 120_000
MIN_LEAF_POINTS = 2_000
# The overview is a decimated sample of the WHOLE dataset, materialised so the
# first frame needs no LAZ decode at all. ~30 MB, renders instantly.
OVERVIEW_TARGET_POINTS = 1_500_000
