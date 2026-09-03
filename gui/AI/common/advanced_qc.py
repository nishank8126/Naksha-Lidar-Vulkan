from __future__ import annotations

import numpy as np

# Internal-only sentinels used inside Advanced inference after the normal
# 5-class model and post-processing have completely finished.
# They are converted to LAS/GUI codes only at final output time.
ADV_QC_UNCATEGORIZED = np.uint8(250)
ADV_QC_LOW_POINT = np.uint8(251)
ADV_QC_HIGH_NOISE = np.uint8(252)


def _log(log, level: str, message: str) -> None:
    try:
        fn = getattr(log, level)
        fn(message)
    except Exception:
        print(message, flush=True)


def _active_indices(n_points: int, active_indices=None) -> np.ndarray:
    if active_indices is None:
        return np.arange(n_points, dtype=np.int64)

    idx = np.asarray(active_indices, dtype=np.int64).ravel()
    idx = idx[(idx >= 0) & (idx < n_points)]
    if idx.size == 0:
        return np.empty(0, dtype=np.int64)
    return np.unique(idx)


def apply_advanced_qc(
    predictions,
    confidence,
    xyz,
    hag,
    *,
    active_indices=None,
    config=None,
    log=None,
):
    """
    Conservative Premium-style QC post-pass for the Advanced 5-class model.

    IMPORTANT SAFETY RULES
    ----------------------
    * The trained Advanced model remains untouched.
    * QC runs only AFTER all existing Advanced Building/Vegetation corrections.
    * Uncategorized is Ground-only.
    * Low Point is restricted to Ground/LowVeg-like rows below the terrain model.
    * High Noise is restricted to very high, low-confidence, non-Building rows.
    * In fence mode, support/context points are inspected but only active target
      indices can be changed.
    * The Uncategorized fraction is a MAXIMUM CAP, not a forced percentage.

    Returns
    -------
    (updated_predictions, report)
    """
    cfg = dict(config or {})

    if not bool(cfg.get("advanced_qc_enabled", True)):
        return np.asarray(predictions, dtype=np.uint8).copy(), {
            "enabled": False,
            "uncategorized": 0,
            "low_point": 0,
            "high_noise": 0,
        }

    pred = np.asarray(predictions, dtype=np.uint8).copy()
    conf = np.asarray(confidence, dtype=np.float32).reshape(-1)
    xyz = np.asarray(xyz, dtype=np.float64)
    hag = np.asarray(hag, dtype=np.float32).reshape(-1)

    n = len(pred)
    if len(conf) != n or len(hag) != n or len(xyz) != n:
        raise ValueError("Advanced QC array-length mismatch")

    active = _active_indices(n, active_indices)
    if active.size == 0:
        return pred, {
            "enabled": True,
            "active_points": 0,
            "uncategorized": 0,
            "low_point": 0,
            "high_noise": 0,
        }

    # Conservative defaults. These can be overridden through advanced_config.
    uncat_conf_max = float(cfg.get("advanced_uncat_confidence_max", 0.58))
    uncat_abs_hag_max = float(cfg.get("advanced_uncat_abs_hag_max", 0.35))
    uncat_max_ratio = float(cfg.get("advanced_uncat_max_ratio", 0.10))
    uncat_max_ratio = min(max(uncat_max_ratio, 0.0), 0.25)

    low_point_hag_max = float(cfg.get("advanced_low_point_hag_max", -0.55))
    high_noise_hag_min = float(cfg.get("advanced_high_noise_hag_min", 45.0))
    high_noise_conf_max = float(cfg.get("advanced_high_noise_confidence_max", 0.72))

    # Internal model classes: 0 Ground, 1 LowVeg, 2 MidVeg, 3 HighVeg, 4 Building.
    active_pred = pred[active]
    active_conf = conf[active]
    active_hag = hag[active]

    # ------------------------------------------------------------------
    # 1) HIGH NOISE
    # ------------------------------------------------------------------
    # Robust Z sanity check adds a second condition so tall but valid objects are
    # less likely to be marked High Noise. Building is explicitly protected.
    z = xyz[active, 2].astype(np.float64, copy=False)
    z_med = float(np.nanmedian(z)) if z.size else 0.0
    z_mad = float(np.nanmedian(np.abs(z - z_med))) if z.size else 0.0
    robust_sigma = max(1.4826 * z_mad, 1.0)
    robust_high = z > (z_med + 12.0 * robust_sigma)

    high_mask_local = (
        (active_pred != 4)
        & (active_hag >= high_noise_hag_min)
        & (active_conf <= high_noise_conf_max)
        & robust_high
    )
    high_idx = active[high_mask_local]
    if high_idx.size:
        pred[high_idx] = ADV_QC_HIGH_NOISE

    # ------------------------------------------------------------------
    # 2) LOW POINT
    # ------------------------------------------------------------------
    # Only Ground/LowVeg-like model rows can become Low Point. This protects
    # Building and normal vegetation classification.
    low_mask_local = (
        np.isin(active_pred, [0, 1])
        & (active_hag <= low_point_hag_max)
        & (~high_mask_local)
    )
    low_idx = active[low_mask_local]
    if low_idx.size:
        pred[low_idx] = ADV_QC_LOW_POINT

    # ------------------------------------------------------------------
    # 3) GROUND-ONLY UNCATEGORIZED
    # ------------------------------------------------------------------
    # This mirrors Premium's philosophy: only uncertain Ground candidates are
    # eligible. It never deliberately converts Building/Vegetation to class 0.
    uncat_mask_local = (
        (active_pred == 0)
        & (active_conf <= uncat_conf_max)
        & (np.abs(active_hag) <= uncat_abs_hag_max)
        & (~high_mask_local)
        & (~low_mask_local)
    )
    uncat_candidates = active[uncat_mask_local]

    max_uncat = int(np.floor(active.size * uncat_max_ratio))
    if max_uncat <= 0:
        uncat_idx = np.empty(0, dtype=np.int64)
    elif uncat_candidates.size <= max_uncat:
        uncat_idx = uncat_candidates
    else:
        # Keep only the least-confident eligible Ground rows.
        cand_conf = conf[uncat_candidates]
        keep = np.argpartition(cand_conf, max_uncat - 1)[:max_uncat]
        uncat_idx = uncat_candidates[keep]

    if uncat_idx.size:
        pred[uncat_idx] = ADV_QC_UNCATEGORIZED

    report = {
        "enabled": True,
        "active_points": int(active.size),
        "uncategorized": int(uncat_idx.size),
        "low_point": int(low_idx.size),
        "high_noise": int(high_idx.size),
        "uncategorized_max_ratio": float(uncat_max_ratio),
        "uncategorized_confidence_max": float(uncat_conf_max),
        "low_point_hag_max": float(low_point_hag_max),
        "high_noise_hag_min": float(high_noise_hag_min),
    }

    _log(log, "info", "")
    _log(log, "info", "  ADVANCED QC POST-PASS")
    _log(log, "info", f"    Active points       : {active.size:,}")
    _log(log, "info", f"    Uncategorized       : {uncat_idx.size:,}")
    _log(log, "info", f"    Low Point           : {low_idx.size:,}")
    _log(log, "info", f"    High Noise          : {high_idx.size:,}")
    _log(log, "info", f"    Uncategorized cap   : {100.0 * uncat_max_ratio:.1f}% (maximum, never forced)")

    return pred, report
