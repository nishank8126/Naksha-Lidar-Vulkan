from __future__ import annotations

from typing import Any, Mapping, Optional

import numpy as np

from .ptc_semantics import (
    SemanticResolution,
    format_resolution,
    resolve_semantic_mapping_from_app,
)
from .vehicle_postprocess import apply_vehicle_postprocess
from .low_point_postprocess import apply_low_point_postprocess
from .building_postprocess import apply_building_vegetation_guard
from .ground_uncategorized_postprocess import apply_ground_uncategorized_ratio
from .layered_ground_vegetation_postprocess import apply_layered_ground_vegetation_postprocess


def _log(message: str) -> None:
    print(message, flush=True)


def _valid_scope(n: int, target_indices=None):
    if target_indices is None:
        return None
    idx = np.asarray(target_indices, dtype=np.int64).ravel()
    idx = idx[(idx >= 0) & (idx < n)]
    return np.unique(idx)


def _semantic_collisions(mapping: Mapping[str, int]):
    by_code = {}
    for semantic, raw in mapping.items():
        try:
            code = int(raw)
        except Exception:
            continue
        by_code.setdefault(code, []).append(str(semantic))
    return {
        int(code): tuple(names)
        for code, names in by_code.items()
        if len(names) > 1
    }


def _mask_for_codes(classes: np.ndarray, codes) -> np.ndarray:
    valid = []
    for c in codes:
        if c is None:
            continue
        try:
            valid.append(int(c))
        except Exception:
            pass
    if not valid:
        return np.zeros(len(classes), dtype=bool)
    return np.isin(classes, np.asarray(sorted(set(valid)), dtype=np.uint8))


def apply_common_semantic_postprocess(
    app: Any,
    *,
    ai_mode: str,
    class_mapping: Optional[Mapping[Any, Any]] = None,
    power_mapping: Optional[Mapping[Any, Any]] = None,
    target_indices=None,
    semantic_resolution: Optional[SemanticResolution] = None,
    vehicle_config: Optional[Mapping[str, Any]] = None,
    low_point_config: Optional[Mapping[str, Any]] = None,
    building_config: Optional[Mapping[str, Any]] = None,
    ground_uncategorized_config: Optional[Mapping[str, Any]] = None,
    layered_ground_vegetation_config: Optional[Mapping[str, Any]] = None,
    before_classes=None,
):
    """Production common stage shared by Basic, Advanced and Premium.

    Responsibilities are intentionally outside the trained networks:
      * active-PTC semantic destinations;
      * vehicle -> Uncategorized for all three models;
      * strict below-terrain outlier -> Low Point; above-scene outlier -> High Noise;
      * exact fence write safety;
      * power/vehicle/uncategorized protection;
      * Advanced/Premium human-style local-HAG Ground/Vegetation layering;
      * Advanced/Premium final Ground policy: user-selected Ground -> Uncategorized ratio.

    The function operates transactionally on a copy and commits to app.data only
    after the stages complete. Model weights and frozen Premium engine files are
    never touched.
    """
    data = getattr(app, "data", None)
    if not isinstance(data, Mapping):
        return {"status": "SKIPPED_NO_APP_DATA", "changed_points": 0}

    xyz = data.get("xyz")
    classes = data.get("classification")
    if xyz is None or classes is None:
        return {"status": "SKIPPED_MISSING_XYZ_OR_CLASSIFICATION", "changed_points": 0}

    xyz = np.asarray(xyz)
    live = np.asarray(classes)
    if xyz.ndim != 2 or xyz.shape[1] < 3 or len(xyz) != len(live):
        return {"status": "SKIPPED_BAD_ARRAY_SHAPE", "changed_points": 0}

    # LAS classification is one byte. Work on a private copy so an unexpected
    # post-process exception can never partially corrupt the live project.
    original = live.astype(np.uint8, copy=True)
    work = original.copy()
    n = len(work)
    scope = _valid_scope(n, target_indices)
    scope_count = n if scope is None else int(len(scope))

    resolution = semantic_resolution
    # V3.23 semantic recovery: a session-restored palette can carry the correct
    # class numbers while losing description/lvl text. Even if a caller supplied
    # an earlier partial snapshot, refresh from the active PTC/table and merge
    # missing semantics before any geometry stage runs.
    fresh_resolution = resolve_semantic_mapping_from_app(
        app,
        class_mapping=class_mapping,
        power_mapping=power_mapping,
    )
    if resolution is None:
        resolution = fresh_resolution
    elif any(resolution.mapping.get(k) is None for k in ("low_point", "high_noise")):
        merged_mapping = dict(resolution.mapping)
        merged_matched = dict(resolution.matched)
        merged_ambiguous = dict(resolution.ambiguous)
        for key, value in fresh_resolution.mapping.items():
            if merged_mapping.get(key) is None:
                merged_mapping[key] = int(value)
                if key in fresh_resolution.matched:
                    merged_matched[key] = fresh_resolution.matched[key]
        for key, value in fresh_resolution.ambiguous.items():
            merged_ambiguous.setdefault(key, value)
        resolution = SemanticResolution(
            mapping=merged_mapping,
            source=f"{resolution.source}+V3.23-refresh:{fresh_resolution.source}",
            ptc_path=resolution.ptc_path or fresh_resolution.ptc_path,
            matched=merged_matched,
            ambiguous=merged_ambiguous,
        )
    semantic = dict(resolution.mapping)

    report = {
        "status": "RUNNING",
        "version": "NAKSHA_COMMON_SEMANTICS_V3_23_STRICT_LOWPOINT_HIGHNOISE",
        "ai_mode": str(ai_mode or "basic").lower(),
        "scope_points": int(scope_count),
        "ptc_source": resolution.source,
        "ptc_path": resolution.ptc_path,
        "semantic_mapping": {str(k): int(v) for k, v in semantic.items()},
        "ambiguous_semantics": {
            str(k): [int(x) for x in v] for k, v in resolution.ambiguous.items()
        },
        "changed_points": 0,
    }

    collisions = _semantic_collisions(semantic)
    report["semantic_code_collisions"] = {
        str(k): list(v) for k, v in collisions.items()
    }
    if collisions:
        report["status"] = "SKIPPED_PTC_CODE_COLLISION"
        _log(f"[COMMON AI] PTC semantic collision; common stage skipped: {collisions}")
        return report

    _log("=" * 68)
    _log("NAKSHA COMMON PRODUCTION POST-PROCESS V3.23")
    _log(f"  AI mode : {report['ai_mode'].upper()}")
    _log(f"  scope   : {scope_count:,}/{n:,} points")
    _log(format_resolution(resolution))
    _log(
        "[COMMON PTC V3.23] "
        f"Low Point -> class {semantic.get('low_point')} | "
        f"High Noise -> class {semantic.get('high_noise')} | "
        f"source={resolution.source}"
    )
    _log("=" * 68)

    if scope is None:
        scope_idx = np.arange(n, dtype=np.int64)
    else:
        scope_idx = scope

    # ------------------------------------------------------------------
    # PHASE 4 COMMON FINALIZATION SNAPSHOT
    # ------------------------------------------------------------------
    # Worker-level Phase 4 has already resolved physical Wire/Pole precedence.
    # Snapshot the active-PTC power classes exactly as they enter the shared
    # common stage. Building / Vehicle / LowPoint logic may use them as nearby
    # context, but these already-confirmed assets must never be reinterpreted.
    phase4_wire_code = semantic.get("wire")
    phase4_pole_code = semantic.get("pole")
    phase4_wire_mask = np.zeros(n, dtype=bool)
    phase4_pole_mask = np.zeros(n, dtype=bool)
    if phase4_wire_code is not None:
        phase4_wire_mask[scope_idx] = work[scope_idx] == np.uint8(int(phase4_wire_code))
    if phase4_pole_code is not None:
        phase4_pole_mask[scope_idx] = work[scope_idx] == np.uint8(int(phase4_pole_code))
    # Pole/Pylon is the more specific structural semantic at any accidental
    # overlap. In a normal class array the masks are mutually exclusive, but
    # keep the precedence contract explicit.
    phase4_wire_mask[phase4_pole_mask] = False
    phase4_wire_mask.setflags(write=False)
    phase4_pole_mask.setflags(write=False)
    report["phase4_power_entry"] = {
        "version": "NAKSHA_POWER_PHASE4_COMMON_REASSERT_V3_10",
        "wire_points": int(np.count_nonzero(phase4_wire_mask)),
        "pole_points": int(np.count_nonzero(phase4_pole_mask)),
        "immutable": True,
    }

    # ------------------------------------------------------------------
    # Premium frozen-engine legacy Low Point / High Noise compatibility.
    # ------------------------------------------------------------------
    # The frozen Premium V4.2 engine emits canonical LAS 7 (Low Point) and
    # LAS 18 (High Noise). V3.23 preserves those two semantics separately:
    # BELOW-terrain -> active PTC Low Point, ABOVE-scene -> active PTC High Noise.
    # No Premium engine/model file is modified.
    legacy_low_applied = 0
    legacy_high_applied = 0
    if report["ai_mode"] == "premium":
        claimed = {int(v) for v in semantic.values()}
        low_code = semantic.get("low_point")
        high_code = semantic.get("high_noise")
        if low_code is not None and int(low_code) != 7 and (7 not in claimed):
            idx = scope_idx[work[scope_idx] == np.uint8(7)]
            if idx.size:
                work[idx] = np.uint8(int(low_code))
                legacy_low_applied = int(idx.size)
                _log(f"[COMMON NoiseGuard V3.23] Premium LAS7 -> Low Point {int(low_code)}: {idx.size:,}")
        if high_code is not None and int(high_code) != 18 and (18 not in claimed):
            idx = scope_idx[work[scope_idx] == np.uint8(18)]
            if idx.size:
                work[idx] = np.uint8(int(high_code))
                legacy_high_applied = int(idx.size)
                _log(f"[COMMON NoiseGuard V3.23] Premium LAS18 -> High Noise {int(high_code)}: {idx.size:,}")
    report["premium_legacy_lowpoint_remap"] = int(legacy_low_applied)
    report["premium_legacy_highnoise_remap"] = int(legacy_high_applied)

    # Protect semantic assets that must not be reinterpreted by downstream
    # geometry. Existing active-PTC Uncategorized is also protected.
    # Keep incoming Low Point protected through Building and Vehicle stages.
    # Advanced revalidation is enabled only at the dedicated Low Point stage
    # below, so no other semantic detector can consume provisional class-7 data.
    protected_codes = [
        semantic.get("wire"),
        semantic.get("pole"),
        semantic.get("uncategorized"),
        semantic.get("low_point"),
        semantic.get("high_noise"),
    ]
    protected = _mask_for_codes(work, protected_codes)

    # ------------------------------------------------------------------
    # COMMON BUILDING GUARD: reject vegetation/ground leakage while
    # protecting planar roofs and vertical facades. This runs after the
    # model/Power stage so confirmed Wire/Pole assets are already protected.
    # ------------------------------------------------------------------
    building_report = {"status": "SKIPPED", "rolled_back": 0}
    building_mask = np.zeros(n, dtype=bool)
    try:
        work, building_mask, building_report = apply_building_vegetation_guard(
            xyz,
            work,
            semantic,
            active_indices=scope,
            before_classes=before_classes,
            protected_mask=protected,
            config=building_config,
        )
        if int(building_report.get("rolled_back", 0) or 0):
            _log(
                "[COMMON Building V3.6] "
                f"checked={building_report.get('building_points_checked', 0):,} "
                f"rolled_back={building_report.get('rolled_back', 0):,} "
                f"roof_protected={building_report.get('roof_protected', 0):,} "
                f"wall_protected={building_report.get('wall_protected', 0):,}"
            )
    except Exception as exc:
        building_report = {
            "status": "ERROR_FAIL_OPEN",
            "rolled_back": 0,
            "error": str(exc),
        }
        _log(f"[COMMON Building V3.6] WARNING fail-open: {exc}")
    report["building_guard"] = building_report

    protected |= _mask_for_codes(
        work,
        [semantic.get("wire"), semantic.get("pole"), semantic.get("uncategorized"), semantic.get("low_point"), semantic.get("high_noise")],
    )

    # ------------------------------------------------------------------
    # COMMON VEHICLE: Basic + Advanced + Premium -> Uncategorized.
    # ------------------------------------------------------------------
    vehicle_report = {"status": "SKIPPED"}
    vehicle_mask = np.zeros(n, dtype=bool)
    try:
        legacy_vehicle_inputs = []
        uncat = semantic.get("uncategorized")

        # V3.5 PREMIUM DOUBLE-DETECTION GUARD. Premium already runs the
        # dedicated LiDAR vehicle detector before this common stage. When the
        # active PTC Uncategorized code is canonical 0, running a second
        # geometry detector here only creates an opportunity to erase shrubs or
        # other vegetation into class 0. Preserve Premium's dedicated result.
        premium_vehicle_already_final = (
            report["ai_mode"] == "premium"
            and uncat is not None
            and int(uncat) == 0
        )

        if premium_vehicle_already_final:
            vehicle_report = {
                "status": "SKIPPED_PREMIUM_DEDICATED_VEHICLE_ALREADY_FINAL",
                "vehicle_points_applied": 0,
                "version": "VEHICLE_V3_5_PREMIUM_DOUBLE_PASS_GUARD",
            }
            _log("[COMMON Vehicle] Premium dedicated vehicle result already class 0; second common detection skipped.")
        else:
            # If a future PTC assigns Uncategorized a different code, the
            # common geometry stage is still allowed to recognize legacy
            # Premium class-0 vehicle shapes and rewrite only strongly confirmed
            # vehicles to the active PTC code. Never blanket-remap class 0.
            if report["ai_mode"] == "premium":
                base_power_semantics = (
                    "ground", "low_vegetation", "medium_vegetation",
                    "high_vegetation", "building", "wire", "pole", "low_point", "high_noise",
                )
                claimed_without_uncat = {
                    int(semantic[s]) for s in base_power_semantics if semantic.get(s) is not None
                }
                if uncat is not None and int(uncat) != 0 and 0 not in claimed_without_uncat:
                    legacy_vehicle_inputs = [0]

            work, vehicle_mask, vehicle_report = apply_vehicle_postprocess(
                xyz,
                work,
                semantic,
                active_indices=scope,
                protected_mask=protected,
                legacy_input_codes=legacy_vehicle_inputs,
                config=vehicle_config,
            )
    except Exception as exc:
        vehicle_report = {
            "status": "ERROR_FAIL_OPEN",
            "vehicle_points_applied": 0,
            "error": str(exc),
        }
        _log(f"[COMMON Vehicle] WARNING fail-open: {exc}")
    report["vehicle"] = vehicle_report

    protected |= vehicle_mask
    protected |= _mask_for_codes(
        work,
        [semantic.get("wire"), semantic.get("pole"), semantic.get("uncategorized")],
    )

    # ------------------------------------------------------------------
    # COMMON NOISE GUARD: below terrain -> Low Point; above scene -> High Noise.
    # ------------------------------------------------------------------
    low_report = {"status": "SKIPPED"}
    try:
        _effective_low_point_config = dict(low_point_config or {})
        _lowpoint_stage_protected = protected
        if report["ai_mode"] == "advanced":
            # Root fix for Advanced QC class-7 explosions: every incoming
            # Advanced Low Point in the writable scope must pass the strict
            # full-scene geometry test before it remains Low Point. Keep those
            # points protected through Building/Vehicle, then unprotect ONLY
            # class Low Point here for the geometry revalidation stage.
            _effective_low_point_config["revalidate_existing"] = True
            _lowpoint_stage_protected = protected.copy()
            _low_code = semantic.get("low_point")
            if _low_code is not None:
                _provisional_low = scope_idx[
                    work[scope_idx] == np.uint8(int(_low_code))
                ]
                if _provisional_low.size:
                    _lowpoint_stage_protected[_provisional_low] = False
            # Critical non-LowPoint assets stay immutable.
            _lowpoint_stage_protected |= phase4_wire_mask | phase4_pole_mask | vehicle_mask
            _lowpoint_stage_protected |= _mask_for_codes(
                work,
                [semantic.get("wire"), semantic.get("pole"), semantic.get("uncategorized"), semantic.get("building"), semantic.get("high_noise")],
            )
        work, low_mask, low_report = apply_low_point_postprocess(
            xyz,
            work,
            semantic,
            active_indices=scope,
            protected_mask=_lowpoint_stage_protected,
            config=_effective_low_point_config,
        )
    except Exception as exc:
        low_mask = np.zeros(n, dtype=bool)
        low_report = {
            "status": "ERROR_FAIL_OPEN",
            "low_point_points_applied": 0,
            "error": str(exc),
        }
        _log(f"[COMMON LowPoint] WARNING fail-open: {exc}")
    report["low_point"] = low_report
    if report["ai_mode"] == "advanced" and low_report.get("revalidate_existing"):
        _log(
            "[COMMON Advanced NoiseGuard V3.23] "
            f"incoming_low={low_report.get('existing_lowpoint_before', 0):,} "
            f"kept_below={low_report.get('existing_lowpoint_retained', 0):,} "
            f"released_to_GV={low_report.get('existing_lowpoint_released_to_ground', 0):,} "
            f"to_high_noise={low_report.get('existing_lowpoint_to_high_noise', 0):,} "
            f"new_low={low_report.get('new_lowpoint_detected', 0):,} "
            f"new_high_noise={low_report.get('new_high_noise_detected', 0):,}"
        )

    # ------------------------------------------------------------------
    # PHASE 4 FINAL RE-ASSERTION
    # ------------------------------------------------------------------
    # This is deliberately last, after Building / Vehicle / Low Point. If any
    # future common stage accidentally writes across a confirmed power point,
    # restore the active-PTC Wire/Pole semantic before the transactional commit.
    phase4_repairs = 0
    if phase4_wire_code is not None and np.any(phase4_wire_mask):
        wcode = np.uint8(int(phase4_wire_code))
        phase4_repairs += int(np.count_nonzero(work[phase4_wire_mask] != wcode))
        work[phase4_wire_mask] = wcode
    if phase4_pole_code is not None and np.any(phase4_pole_mask):
        pcode = np.uint8(int(phase4_pole_code))
        phase4_repairs += int(np.count_nonzero(work[phase4_pole_mask] != pcode))
        work[phase4_pole_mask] = pcode
    report["phase4_power_finalization"] = {
        "status": "PROTECTED",
        "wire_points": int(np.count_nonzero(phase4_wire_mask)),
        "pole_points": int(np.count_nonzero(phase4_pole_mask)),
        "reasserted_points": int(phase4_repairs),
        "exact_scope_only": True,
    }
    _log(
        "[COMMON Power Phase4] "
        f"wire={int(np.count_nonzero(phase4_wire_mask)):,} "
        f"pole={int(np.count_nonzero(phase4_pole_mask)):,} "
        f"reasserted={phase4_repairs:,}"
    )

    # ------------------------------------------------------------------
    # ADVANCED + PREMIUM HUMAN-STYLE LAYERED GROUND / VEGETATION
    # ------------------------------------------------------------------
    # This stage is intentionally narrow: only active-PTC Ground / LowVeg /
    # MidVeg / HighVeg points may be rewritten. Building / Wire / Pole /
    # LowPoint / HighNoise / Uncategorized are never eligible. It runs BEFORE the user-ratio
    # Ground policy so the selected sample is drawn only from the final layered
    # Ground pool.
    layered_report = {"status": "SKIPPED"}
    layered_mask = np.zeros(n, dtype=bool)
    try:
        layered_protected = (
            phase4_wire_mask
            | phase4_pole_mask
            | vehicle_mask
            | low_mask
            | _mask_for_codes(
                work,
                [semantic.get("uncategorized"), semantic.get("building"), semantic.get("wire"), semantic.get("pole"), semantic.get("low_point"), semantic.get("high_noise")],
            )
        )
        work, layered_mask, layered_report = apply_layered_ground_vegetation_postprocess(
            xyz,
            work,
            semantic,
            ai_mode=report["ai_mode"],
            active_indices=scope,
            protected_mask=layered_protected,
            config=layered_ground_vegetation_config,
        )
        if layered_report.get("status") in ("APPLIED", "NO_CHANGE"):
            _log(
                "[COMMON LayeredGV V3.18] "
                f"mode={report['ai_mode'].upper()} "
                f"eligible={layered_report.get('eligible_points', 0):,} "
                f"reliable_hag={layered_report.get('reliable_hag_points', 0):,} "
                f"changed={layered_report.get('changed_points', 0):,} "
                f"bands=G<=0.15 L<=0.50 M<=2.00 H>2.00m "
                f"backend={layered_report.get('backend')}"
            )
    except Exception as exc:
        layered_report = {
            "status": "ERROR_FAIL_OPEN",
            "changed_points": 0,
            "error": str(exc),
        }
        _log(f"[COMMON LayeredGV V3.18] WARNING fail-open: {exc}")
    report["layered_ground_vegetation"] = layered_report

    # Power is not eligible for LayeredGV, but keep the production invariant
    # explicit: reassert the immutable entry masks again before the Ground user-ratio stage.
    if phase4_wire_code is not None and np.any(phase4_wire_mask):
        work[phase4_wire_mask] = np.uint8(int(phase4_wire_code))
    if phase4_pole_code is not None and np.any(phase4_pole_mask):
        work[phase4_pole_mask] = np.uint8(int(phase4_pole_code))

    # ------------------------------------------------------------------
    # ADVANCED + PREMIUM USER-CONTROLLED GROUND / UNCATEGORIZED RATIO
    # ------------------------------------------------------------------
    # Apply only after all semantic cleanup and the final Phase-4 power
    # reassertion. Eligibility is strictly the active-PTC Ground class, so
    # Vegetation / Building / Wire / Pole / Low Point are never sampled.
    # The configured 0-100% share of the final Ground pool becomes active-PTC
    # Uncategorized. Every non-Ground semantic is protected by source eligibility.
    # Existing Uncategorized (e.g. confirmed vehicles) remains untouched.
    ground_uncat_report = {"status": "SKIPPED"}
    ground_uncat_mask = np.zeros(n, dtype=bool)
    try:
        work, ground_uncat_mask, ground_uncat_report = apply_ground_uncategorized_ratio(
            work,
            semantic,
            ai_mode=report["ai_mode"],
            active_indices=scope,
            config=ground_uncategorized_config,
        )
        if ground_uncat_report.get("status") in ("APPLIED", "NO_CHANGE"):
            _log(
                "[COMMON GroundRatio V3.21] "
                f"mode={report['ai_mode'].upper()} "
                f"pool={ground_uncat_report.get('ground_pool_before', 0):,} "
                f"ground={ground_uncat_report.get('remaining_ground', 0):,} "
                f"to_uncategorized={ground_uncat_report.get('converted_from_ground', 0):,} "
                f"ratio={ground_uncat_report.get('uncategorized_from_ground_percent', 0.0):.2f}% "
                f"-> class {ground_uncat_report.get('uncategorized_code')}"
            )
    except Exception as exc:
        ground_uncat_report = {
            "status": "ERROR_FAIL_OPEN",
            "converted_from_ground": 0,
            "error": str(exc),
        }
        _log(f"[COMMON GroundRatio V3.21] WARNING fail-open: {exc}")
    report["ground_uncategorized_ratio"] = ground_uncat_report
    # Backward-compatible report alias for consumers introduced with V3.19.
    report["ground_uncategorized_50_50"] = ground_uncat_report

    # Exact fence safety assertion before commit.
    if scope is not None:
        outside = np.ones(n, dtype=bool)
        outside[scope] = False
        if np.any(work[outside] != original[outside]):
            report["status"] = "ABORTED_FENCE_SAFETY_ASSERTION"
            _log("[COMMON AI] SAFETY ASSERTION FAILED: outside-fence change detected; no commit.")
            return report

    changed = work != original
    changed_count = int(np.count_nonzero(changed))

    # Commit while preserving the original array object when possible so VTK/
    # GUI references remain valid.
    live_now = data.get("classification")
    if live_now is not None:
        live_arr = np.asarray(live_now)
        if len(live_arr) == n:
            live_arr[:] = work.astype(live_arr.dtype, copy=False)
        else:
            data["classification"] = work
    else:
        data["classification"] = work

    report["status"] = "APPLIED" if changed_count else "NO_CHANGE"
    report["changed_points"] = changed_count
    report["final_low_point_code"] = (
        int(semantic["low_point"]) if semantic.get("low_point") is not None else None
    )
    report["final_high_noise_code"] = (
        int(semantic["high_noise"]) if semantic.get("high_noise") is not None else None
    )
    report["final_uncategorized_code"] = (
        int(semantic["uncategorized"]) if semantic.get("uncategorized") is not None else None
    )

    try:
        data["_ai_common_postprocess_report"] = report
    except Exception:
        pass

    _log(
        "[COMMON AI] COMPLETE "
        f"mode={report['ai_mode'].upper()} changed={changed_count:,} "
        f"building_rollback={building_report.get('rolled_back', 0):,} "
        f"vehicle={vehicle_report.get('vehicle_points_applied', 0):,} "
        f"low_point={low_report.get('low_point_points_applied', 0):,} "
        f"high_noise={low_report.get('high_noise_points_applied', 0):,} "
        f"layered_gv={layered_report.get('changed_points', 0):,} "
        f"ground_to_uncat={ground_uncat_report.get('converted_from_ground', 0):,}"
    )
    return report
