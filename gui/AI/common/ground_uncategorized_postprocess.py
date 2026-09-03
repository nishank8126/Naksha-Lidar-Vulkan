from __future__ import annotations

from typing import Any, Mapping, Optional

import numpy as np


GROUND_UNCATEGORIZED_VERSION = "NAKSHA_GROUND_UNCATEGORIZED_USER_RATIO_V3_21"


def _valid_scope(n: int, active_indices=None) -> np.ndarray:
    if active_indices is None:
        return np.arange(n, dtype=np.int64)
    idx = np.asarray(active_indices, dtype=np.int64).ravel()
    idx = idx[(idx >= 0) & (idx < n)]
    return np.unique(idx)


def _stable_keys(indices: np.ndarray) -> np.ndarray:
    """Deterministic 64-bit mixing of global point indices.

    This produces a stable pseudo-random spatial-looking subset without using
    RNG state. Re-running the same file/fence selects the same source Ground
    points, which is important for reproducible classification and undo/redo.
    """
    x = np.asarray(indices, dtype=np.uint64).copy()
    x ^= x >> np.uint64(30)
    x *= np.uint64(0xBF58476D1CE4E5B9)
    x ^= x >> np.uint64(27)
    x *= np.uint64(0x94D049BB133111EB)
    x ^= x >> np.uint64(31)
    return x


def apply_ground_uncategorized_ratio(
    classes: np.ndarray,
    semantic_mapping: Mapping[str, int],
    *,
    ai_mode: str,
    active_indices=None,
    config: Optional[Mapping[str, Any]] = None,
):
    """Convert a user-selected percentage of final Ground points to Uncategorized.

    User-ratio policy for Advanced + Premium only:
      * eligible source semantic is Ground only;
      * the user selects 0-100% of the eligible Ground pool to become Uncategorized;
      * the remaining Ground points stay Ground;
      * Vegetation / Building / Wire / Pole / Low Point are never sampled;
      * target/fence scope is honored exactly;
      * selection is deterministic across repeat runs;
      * source eligibility is strictly final Ground only.

    Existing Uncategorized points (for example confirmed vehicle outputs) are
    preserved and are never used as source points for this Ground-only policy.

    The destination numeric code comes from the active PTC semantic mapping.
    For NT932 this resolves to class 0; it is intentionally not hard-coded so
    customer PTC mappings remain authoritative.
    """
    cfg = dict(config or {})
    # NAKSHA_GROUND_RATIO_SHARED_UI_V3_24_SAFE_DEFAULT_ZERO
    ratio = float(cfg.get("ground_to_uncategorized_ratio", 0.0))
    ratio = min(max(ratio, 0.0), 1.0)

    arr = np.asarray(classes)
    if arr.ndim != 1:
        raise ValueError("classes must be a 1D classification array")

    n = len(arr)
    scope_idx = _valid_scope(n, active_indices)
    mode = str(ai_mode or "basic").strip().lower()

    report = {
        "version": GROUND_UNCATEGORIZED_VERSION,
        "status": "RUNNING",
        "ai_mode": mode,
        "target_ratio": float(ratio),
        "target_percent": float(ratio * 100.0),
        "ratio_source": ("explicit_config" if "ground_to_uncategorized_ratio" in cfg else "safe_default_zero"),
        "scope_points": int(scope_idx.size),
        "source_semantic": "ground",
        "destination_semantic": "uncategorized",
        "ground_pool_before": 0,
        "converted_from_ground": 0,
        "remaining_ground": 0,
        "existing_uncategorized_before": 0,
        "final_uncategorized_in_scope": 0,
        "ground_keep_percent": 100.0,
        "uncategorized_from_ground_percent": 0.0,
        "ground_only_source": True,
        "ground_only_source_verified": False,
        "deterministic": True,
        "exact_scope_only": True,
    }

    if mode not in ("advanced", "premium"):
        report["status"] = "SKIPPED_MODE_NOT_ADVANCED_OR_PREMIUM"
        return arr, np.zeros(n, dtype=bool), report

    ground_code = semantic_mapping.get("ground")
    uncat_code = semantic_mapping.get("uncategorized")
    if ground_code is None or uncat_code is None:
        report["status"] = "SKIPPED_MISSING_PTC_SEMANTIC"
        return arr, np.zeros(n, dtype=bool), report

    ground_code = int(ground_code)
    uncat_code = int(uncat_code)
    report["ground_code"] = ground_code
    report["uncategorized_code"] = uncat_code

    if ground_code == uncat_code:
        report["status"] = "SKIPPED_SEMANTIC_CODE_COLLISION"
        return arr, np.zeros(n, dtype=bool), report
    if not (0 <= ground_code <= 255 and 0 <= uncat_code <= 255):
        report["status"] = "SKIPPED_CODE_OUT_OF_UINT8_RANGE"
        return arr, np.zeros(n, dtype=bool), report

    work = arr.copy()
    uncat_before = int(np.count_nonzero(work[scope_idx] == uncat_code))
    ground_idx = scope_idx[work[scope_idx] == ground_code]
    ground_n = int(ground_idx.size)
    target_n = int(np.floor((ground_n * ratio) + 0.5))
    target_n = min(max(target_n, 0), ground_n)

    report["existing_uncategorized_before"] = uncat_before
    report["ground_pool_before"] = ground_n

    converted_mask = np.zeros(n, dtype=bool)
    if target_n:
        if target_n == ground_n:
            selected = ground_idx
        else:
            keys = _stable_keys(ground_idx)
            pick = np.argpartition(keys, target_n - 1)[:target_n]
            selected = ground_idx[pick]
        work[selected] = np.asarray(uncat_code, dtype=work.dtype)
        converted_mask[selected] = True

    # Safety invariant: every point changed by this stage must have been Ground
    # immediately before this stage. No vegetation/building/power/noise class is
    # ever an eligible source.
    changed_idx = np.flatnonzero(converted_mask)
    source_verified = bool(
        changed_idx.size == 0 or np.all(arr[changed_idx] == ground_code)
    )
    if not source_verified:
        raise RuntimeError("Ground-only source invariant failed")

    remaining = int(np.count_nonzero(work[scope_idx] == ground_code))
    final_uncat = int(np.count_nonzero(work[scope_idx] == uncat_code))
    converted = int(np.count_nonzero(converted_mask))

    report.update({
        "status": "APPLIED" if converted else "NO_CHANGE",
        "converted_from_ground": converted,
        "remaining_ground": remaining,
        "final_uncategorized_in_scope": final_uncat,
        "ground_keep_percent": (100.0 * remaining / ground_n) if ground_n else 100.0,
        "uncategorized_from_ground_percent": (100.0 * converted / ground_n) if ground_n else 0.0,
        "ground_only_source_verified": source_verified,
    })

    converted_mask.setflags(write=False)
    return work, converted_mask, report
