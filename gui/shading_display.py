# # # /////////////////////////////phase 2 PATCH 8.2///////////////////////////////////////////////
# #||||||||||||||||||||||||||||||||||||||||||| WORKING_DIR ||||||||||||||||||||||||||||||||||||||||

import numpy as np
import pyvista as pv
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QDoubleSpinBox, QHBoxLayout, QPushButton,
    QProgressDialog, QApplication
)
from PySide6.QtCore import Qt, QTimer, QEvent, QEventLoop, QThread, Signal
from typing import Optional, Set
import time
from vtkmodules.util import numpy_support
import vtk
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum, auto

try:
    import triangle as tr
    HAS_TRIANGLE = True
except ImportError:
    HAS_TRIANGLE = False

try:
    from scipy.spatial import Delaunay
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

import multiprocessing
from concurrent.futures import ThreadPoolExecutor
import os
import weakref
import hashlib

_LARGE_MESH_THRESHOLD = 50_000_000
_MAX_STORED_SHADING_CACHES = 4
_MAX_STORED_REPRESENTATIVE_CACHES = 4
_ADAPTIVE_SHADING_VERSION = 1
_ADAPTIVE_DEFAULT_TARGET = 3_000_000
_SHADING_FAST_TARGET = 1_000_000
_FEATURE_AWARE_VERSION = 1

# Multi-class classification refresh must keep the enormous base shaded mesh
# immutable.  Local class edits are displayed through one tiny exact-face
# overlay, just like the existing single-class live-add path.
_MULTICLASS_COLOR_OVERLAY_NAME = "shaded_mesh_live_multiclass_color"
_MULTICLASS_CRISP_PURE_OVERLAY_NAME = "shaded_mesh_live_multiclass_crisp_pure"
_MULTICLASS_LIVE_TIP_NAME = "shaded_mesh_live_multiclass_tip"
_MULTICLASS_STATIC_BLEND_PREFIX = "shaded_mesh_static_multiclass_blend_"
_INCIDENT_FACE_CACHE_MAX_NEW_VERTICES = 4096

# Legacy class-detail point-layer names retained for cleanup compatibility.
# Current multi-class fidelity is facet-based for Fast / Normal / Slow: class
# RGB lives on mesh vertices and is barycentrically interpolated by the GPU.
# Single-class shading keeps its established topology/edit implementation.
_SHADING_CLASS_DETAIL_NAME = "shaded_mesh_class_detail"
_SHADING_LIVE_CLASS_DETAIL_NAME = "shaded_mesh_live_class_detail"


def normalize_shading_quality(value):
    """Return the persisted shading quality key used by UI and caches."""
    quality = str(value or "normal").strip().lower()
    return quality if quality in ("fast", "normal", "slow") else "normal"
_rebuild_timer = None
_rebuild_reason = ""
_rebuild_changed_indices = None
_active_app_ref = None
_representative_store = OrderedDict()


def _emit_shading_profile(scope, timings, **meta):
    """Print stable, machine-searchable shading stage timings.

    Instrumentation only: this helper must never affect rendering behaviour.
    """
    try:
        ordered = " ".join(
            f"{name}={float(value) * 1000.0:.1f}ms"
            for name, value in timings.items()
        )
        details = " ".join(f"{key}={value}" for key, value in meta.items())
        suffix = f" {details}" if details else ""
        print(f"SHADING_PROFILE scope={scope} {ordered}{suffix}")
    except Exception:
        # Profiling must never break shading.
        pass


class ShadingEditKind(Enum):
    NO_GEOMETRY_CHANGE = auto()
    LOCAL_REMOVE = auto()
    LOCAL_ADD = auto()
    FULL_REBUILD = auto()


@dataclass(frozen=True)
class ClassificationDelta:
    changed_indices: np.ndarray
    old_classes: np.ndarray
    new_classes: np.ndarray
    operation: str = "classification"
    origin_view: str = "unknown"
    is_final_commit: bool = True


def plan_single_class_shading_edit(app, changed_indices, old_classes,
                                   new_classes, visible_class):
    """Classify a committed edit from its real before/after values."""
    ci = np.asarray(changed_indices, dtype=np.int64).ravel()
    old = np.asarray(old_classes).ravel()
    new = np.asarray(new_classes).ravel()
    if ci.size == 0:
        return ShadingEditKind.NO_GEOMETRY_CHANGE
    if old.size != ci.size or new.size != ci.size:
        return ShadingEditKind.FULL_REBUILD

    entering = (old != visible_class) & (new == visible_class)
    leaving = (old == visible_class) & (new != visible_class)
    if not np.any(entering) and not np.any(leaving):
        return ShadingEditKind.NO_GEOMETRY_CHANGE
    if np.any(entering) and np.any(leaving):
        return ShadingEditKind.FULL_REBUILD

    try:
        from gui.optimization_config import (
            SINGLE_CLASS_PATCH_MAX_CHANGED_POINTS,
            SINGLE_CLASS_PATCH_MAX_CHANGED_RATIO,
        )
        cache = get_cache()
        n_cached = max(len(cache.unique_indices) if cache.unique_indices is not None else 0, 1)
        if ci.size > int(SINGLE_CLASS_PATCH_MAX_CHANGED_POINTS):
            return ShadingEditKind.FULL_REBUILD
        if ci.size / n_cached > float(SINGLE_CLASS_PATCH_MAX_CHANGED_RATIO):
            return ShadingEditKind.FULL_REBUILD
    except Exception:
        return ShadingEditKind.FULL_REBUILD
    return ShadingEditKind.LOCAL_ADD if np.any(entering) else ShadingEditKind.LOCAL_REMOVE


def _recover_classification_delta(app, changed_indices, operation="classification"):
    """Recover old/new values from the exact matching undo entry."""
    ci = np.asarray(changed_indices, dtype=np.int64).ravel()
    if ci.size == 0:
        return None
    for stack_name in ("undo_stack", "undostack"):
        stack = getattr(app, stack_name, None)
        if not stack:
            continue
        try:
            step = stack[-1]
            si = step.get("indices")
            if si is None:
                sm = step.get("mask")
                if isinstance(sm, np.ndarray) and sm.dtype == bool:
                    si = np.flatnonzero(sm)
            if si is None:
                continue
            si = np.asarray(si, dtype=np.int64).ravel()
            if not np.array_equal(si, ci):
                continue
            old = step.get("old_classes")
            if old is None:
                old = step.get("oldclasses")
            new = step.get("new_classes")
            if new is None:
                new = step.get("newclasses")
            if old is None or new is None:
                continue
            return ClassificationDelta(
                changed_indices=ci,
                old_classes=np.asarray(old),
                new_classes=np.asarray(new),
                operation=operation,
                origin_view="recovered_commit",
            )
        except Exception:
            continue
    return None


def _force_present_shaded_mesh(app):
    """Bypass classify-streak throttling now and once on the next Qt turn."""
    def present():
        try:
            actor = getattr(app, "_shaded_mesh_actor", None)
            mesh = getattr(app, "_shaded_mesh_polydata", None)
            if mesh is not None:
                mesh.GetPoints().Modified() if mesh.GetPoints() is not None else None
                mesh.GetPolys().Modified() if mesh.GetPolys() is not None else None
                mesh.GetPointData().Modified(); mesh.GetCellData().Modified()
                mesh.Modified()
            if actor is not None:
                mapper = actor.GetMapper()
                if mapper is not None:
                    mapper.Update(); mapper.Modified()
                actor.Modified()
            widget = getattr(app, "vtk_widget", None)
            rw = widget.GetRenderWindow() if widget is not None else None
            if rw is not None:
                rw.Render()
        except Exception as exc:
            print(f"SHADING_PRESENT failed={exc}")

    present()
    try:
        QTimer.singleShot(0, present)
    except Exception:
        pass


def _schedule_fast_shaded_present(
        app, delay_ms=0, restart=False, wait_while_preview=False):
    """Coalesce local patches and never interrupt a live classify preview."""
    timer = getattr(app, '_shading_present_timer', None)
    if timer is not None:
        try:
            if timer.isActive():
                if restart:
                    timer.start(max(0, int(delay_ms)))
                return
        except Exception:
            pass
    timer = QTimer(app if isinstance(app, QWidget) else None)
    timer.setSingleShot(True)
    def present_once():
        if (wait_while_preview
                and bool(getattr(app, '_classification_preview_active', False))):
            timer.start(max(16, int(delay_ms)))
            return
        app._shading_present_timer = None
        try:
            widget = getattr(app, 'vtk_widget', None)
            if widget is not None:
                reason = getattr(app, '_shading_present_reason', None)
                pt0 = time.perf_counter()
                widget.render()
                if reason:
                    print(
                        "SHADING_PRESENT "
                        f"reason={reason} render={(time.perf_counter()-pt0)*1000:.1f}ms"
                    )
                    app._shading_present_reason = None
        except Exception as exc:
            print(f"SHADING_PRESENT failed={exc}")
        try:
            timer.deleteLater()
        except Exception:
            pass
    timer.timeout.connect(present_once)
    app._shading_present_timer = timer
    timer.start(max(0, int(delay_ms)))

def _is_widget_deleted(widget):
    if widget is None:
        return True
    try:
        # accessing any attribute of a deleted C++ QObject raises RuntimeError
        _ = widget.objectName()
        return False
    except RuntimeError:
        return True


class _ShadingComputationWorker(QThread):
    finished_signal = Signal(dict)
    error_signal = Signal(str)

    def __init__(self, xyz_raw, classes_raw, visible_classes, azimuth, angle, ambient, max_edge_factor, single_class_max_edge, data_hash, representative_seed=None, boundary_flags=None, quality_mode="normal"):
        super().__init__()
        self.xyz_raw = xyz_raw
        self.classes_raw = classes_raw
        self.visible_classes = visible_classes
        self.azimuth = azimuth
        self.angle = angle
        self.ambient = ambient
        self.max_edge_factor = max_edge_factor
        self.single_class_max_edge = single_class_max_edge
        self.data_hash = data_hash
        self.representative_seed = representative_seed
        self.boundary_flags = boundary_flags
        self.quality_mode = quality_mode

    def run(self):
        try:
            res = _compute_shading_geometry_backend(
                self.xyz_raw, self.classes_raw, self.visible_classes,
                self.azimuth, self.angle, self.ambient,
                self.max_edge_factor, self.single_class_max_edge, self.data_hash,
                representative_seed=self.representative_seed,
                boundary_flags=self.boundary_flags,
                quality_mode=self.quality_mode
            )
            self.finished_signal.emit(res)
        except Exception:
            import traceback
            self.error_signal.emit(traceback.format_exc())


def _extract_boundary_flags_for_shading(app, expected_len):
    """Return a detached bool BoundaryFlag array for the worker thread.

    Reads application-owned data first, then the unified actor's point-data
    array. Failure is non-fatal: adaptive density falls back to the exact
    Patch-7 selector.
    """
    try:
        data = getattr(app, "data", None)
        if isinstance(data, dict):
            for key in ("boundary_flag", "BoundaryFlag", "boundary_flags"):
                arr = data.get(key)
                if arr is not None and len(arr) == expected_len:
                    return np.asarray(arr).reshape(-1).astype(bool, copy=True)
    except Exception:
        pass
    try:
        widget = getattr(app, "vtk_widget", None)
        actor = getattr(widget, "actors", {}).get("_naksha_unified_cloud")
        if actor is None:
            return None
        mapper = actor.GetMapper()
        poly = mapper.GetInput() if mapper is not None else None
        vtk_arr = poly.GetPointData().GetArray("BoundaryFlag") if poly is not None else None
        if vtk_arr is None or vtk_arr.GetNumberOfTuples() != expected_len:
            return None
        return numpy_support.vtk_to_numpy(vtk_arr).reshape(-1).astype(bool, copy=True)
    except Exception as exc:
        print(f"SHADING_ADAPTIVE status=fallback reason=boundary_unavailable detail={exc}")
        return None


def _adaptive_boundary_representatives(xyz, baseline_indices, boundary_mask,
                                       baseline_precision, target_max):
    """Add edge-focused representatives while preserving every baseline point.

    The baseline Patch-7 set is never reduced. Boundary points are selected on
    a finer grid and only new representatives are appended. If candidates
    exceed the quality budget, deterministic spatial-order thinning keeps the
    additions distributed across the complete tile.
    """
    t0 = time.perf_counter()
    baseline_indices = np.asarray(baseline_indices, dtype=np.int64)
    boundary_mask = np.asarray(boundary_mask, dtype=bool).reshape(-1)
    n_base = len(baseline_indices)
    budget = max(int(target_max), n_base)
    extra_budget = budget - n_base
    if extra_budget <= 0 or boundary_mask.size != len(xyz) or not np.any(boundary_mask):
        return baseline_indices, {
            "enabled": False, "reason": "no_budget_or_boundary", "base": n_base,
            "boundary_points": int(np.count_nonzero(boundary_mask)), "extra": 0,
            "target": budget, "elapsed": time.perf_counter() - t0,
        }

    boundary_local = np.flatnonzero(boundary_mask)
    # Phase 1 conservative fine grid. Environment override allows production
    # tuning without changing code. 0.55 gives ~3.3x potential cell density,
    # but the hard representative budget prevents runaway meshes.
    fine_ratio = float(os.environ.get("NAKSHA_SHADING_ADAPTIVE_FINE_RATIO", "0.55"))
    fine_ratio = min(max(fine_ratio, 0.30), 0.95)
    fine_precision = max(float(baseline_precision) * fine_ratio, 0.0025)
    fine_sub = _grid_dedup_at_precision(xyz[boundary_local], fine_precision)
    fine_local = boundary_local[np.asarray(fine_sub, dtype=np.int64)]

    selected = np.zeros(len(xyz), dtype=np.uint8)
    selected[baseline_indices] = 1
    extras = fine_local[selected[fine_local] == 0]
    candidate_extra = len(extras)
    if candidate_extra > extra_budget:
        # _grid_dedup_at_precision returns deterministic grid order. Evenly
        # sampling that order retains spatial coverage better than truncation.
        pick = np.linspace(0, candidate_extra - 1, extra_budget, dtype=np.int64)
        extras = extras[pick]

    result = np.concatenate((baseline_indices, extras.astype(np.int64, copy=False)))
    meta = {
        "enabled": True,
        "reason": "boundary_refinement",
        "base": n_base,
        "boundary_points": len(boundary_local),
        "fine_candidates": len(fine_local),
        "candidate_extra": candidate_extra,
        "extra": len(extras),
        "target": budget,
        "fine_ratio": fine_ratio,
        "fine_precision": fine_precision,
        "elapsed": time.perf_counter() - t0,
    }
    return result, meta



def _feature_aware_filter_faces(faces, xyz_unique, representative_boundary, spacing):
    """Conservatively reject long triangles that bridge strong boundary jumps.

    This is a post-Delaunay quality filter, not a topology generator. It only
    acts on mixed boundary/non-boundary triangles that are simultaneously long,
    vertically discontinuous, and steep. A removal-ratio guard preserves the
    original mesh when thresholds would remove too much topology.
    """
    t0 = time.perf_counter()
    enabled = os.environ.get("NAKSHA_SHADING_FEATURE_AWARE", "1").strip().lower() not in ("0", "false", "off", "no")
    meta = {
        "enabled": False, "reason": "disabled", "input_faces": int(len(faces)),
        "output_faces": int(len(faces)), "removed": 0, "elapsed": 0.0,
    }
    if not enabled:
        meta["elapsed"] = time.perf_counter() - t0
        return faces, meta
    if representative_boundary is None or len(representative_boundary) != len(xyz_unique):
        meta.update(reason="boundary_unavailable", elapsed=time.perf_counter() - t0)
        return faces, meta
    if len(faces) == 0 or not HAS_NUMBA:
        meta.update(reason="empty_or_numba_unavailable", elapsed=time.perf_counter() - t0)
        return faces, meta

    edge_factor = float(os.environ.get("NAKSHA_SHADING_FEATURE_EDGE_FACTOR", "3.0"))
    min_edge = float(os.environ.get("NAKSHA_SHADING_FEATURE_MIN_EDGE", "1.0"))
    z_factor = float(os.environ.get("NAKSHA_SHADING_FEATURE_Z_FACTOR", "0.75"))
    min_z_jump = float(os.environ.get("NAKSHA_SHADING_FEATURE_MIN_Z_JUMP", "0.35"))
    min_slope = float(os.environ.get("NAKSHA_SHADING_FEATURE_MIN_SLOPE", "0.75"))
    max_remove_ratio = float(os.environ.get("NAKSHA_SHADING_FEATURE_MAX_REMOVE_RATIO", "0.02"))

    min_edge_len = max(float(spacing) * max(edge_factor, 1.0), max(min_edge, 0.0))
    min_jump = max(float(spacing) * max(z_factor, 0.0), max(min_z_jump, 0.0))
    remove = _numba_feature_discontinuity_mask(
        np.asarray(faces, dtype=np.int32),
        np.asarray(xyz_unique, dtype=np.float64),
        np.asarray(representative_boundary, dtype=np.uint8),
        min_edge_len * min_edge_len,
        min_jump,
        max(min_slope, 0.0),
    )
    removed = int(np.count_nonzero(remove))
    ratio = removed / max(len(faces), 1)
    if removed == 0:
        meta.update(
            reason="no_crossing_discontinuities", candidate_removed=0,
            removal_ratio=0.0, elapsed=time.perf_counter() - t0,
        )
        return faces, meta
    if ratio > max_remove_ratio:
        meta.update(
            reason="safety_guard", candidate_removed=removed,
            removal_ratio=ratio, max_remove_ratio=max_remove_ratio,
            elapsed=time.perf_counter() - t0,
        )
        return faces, meta

    filtered = faces[~remove]
    meta.update(
        enabled=True, reason="mixed_boundary_discontinuity", removed=removed,
        candidate_removed=removed, removal_ratio=ratio,
        output_faces=int(len(filtered)), edge_factor=edge_factor,
        min_edge=min_edge_len, min_z_jump=min_jump, min_slope=min_slope,
        elapsed=time.perf_counter() - t0,
    )
    return filtered, meta

def _compute_shading_geometry_backend(xyz_raw, classes_raw, visible_classes, azimuth, angle, ambient, max_edge_factor, single_class_max_edge, data_hash, representative_seed=None, boundary_flags=None, quality_mode="normal"):
    profile_start = time.perf_counter()
    stage_start = profile_start
    timings = OrderedDict()

    def checkpoint(name):
        nonlocal stage_start
        now = time.perf_counter()
        timings[name] = now - stage_start
        stage_start = now

    nv = len(visible_classes)
    is_sc = (nv == 1)
    representative_cache_hit = bool(representative_seed)
    adaptive_meta = {"enabled": False, "reason": "representative_cache" if representative_cache_hit else "not_evaluated"}

    if representative_cache_hit:
        # PATCH 4A: reuse the exact final representative selection. The seed
        # is accepted only after geometry, visible-preset, and full
        # classification-content validation in the GUI thread. No point-count
        # reduction or alternate representative rule is used here.
        offset = representative_seed["offset"]
        unique_indices_global = representative_seed["unique_indices"]
        xyz_unique = representative_seed["xyz_unique"]
        spacing = float(representative_seed["spacing"])
        lod_factor = float(representative_seed.get("lod_factor", 1.0))
        base_grid_unique_count = int(representative_seed.get("base_grid_unique_count", len(xyz_unique)))
        # Shaded Class is faceted for every visibility combination, including
        # the all-classes preset.  Do not revive the former smooth policy from
        # a representative-selection cache created by an older build.
        smooth_all_classes = False
        precision = float(representative_seed.get("precision", 0.0))
        adaptive_meta = dict(representative_seed.get("adaptive_meta", {
            "enabled": False, "reason": "representative_cache"
        }))
        representative_boundary = representative_seed.get("representative_boundary")
        if representative_boundary is not None:
            representative_boundary = np.asarray(representative_boundary, dtype=bool).reshape(-1)
            if len(representative_boundary) != len(xyz_unique):
                representative_boundary = None

        # Keep the established profile field names stable. Cache lookup and
        # validation are profiled separately by the caller.
        for name in ("visibility_mask", "visible_indices", "visible_xyz_copy",
                     "finite_filter", "normalize_extent", "dedup_pass1",
                     "dedup_lod_extra", "adaptive_refinement"):
            timings[name] = 0.0
        stage_start = time.perf_counter()

        xr2 = xyz_unique[:, 0].max() - xyz_unique[:, 0].min()
        yr2 = xyz_unique[:, 1].max() - xyz_unique[:, 1].min()
        data_extent = max(xr2, yr2)
        xy = xyz_unique[:, :2]
        checkpoint("unique_materialize")
    else:
        classes = classes_raw.astype(np.int32)
        mc = int(np.max(classes)) + 1 if len(classes) > 0 else 256
        vl = np.zeros(mc, dtype=bool)
        for v in visible_classes:
            if v < mc:
                vl[v] = True
        vm = vl[classes]
        checkpoint("visibility_mask")

        vi = np.flatnonzero(vm)
        # Keep per-face lighting/class colour even when every loaded class is
        # visible. Previously this special-cased all classes to vertex-smoothed
        # shading, making the same mode change appearance when one class was
        # toggled off.
        smooth_all_classes = False
        checkpoint("visible_indices")
        if len(vi) < 3:
            timings["total_backend"] = time.perf_counter() - profile_start
            return {"empty": True, "profile_timings": timings}

        xv = xyz_raw[vi]
        checkpoint("visible_xyz_copy")

        finite_mask = np.isfinite(xv).all(axis=1)
        if not np.all(finite_mask):
            vi = vi[finite_mask]
            xv = xv[finite_mask]
        visible_boundary = None
        representative_boundary = None
        if boundary_flags is not None:
            try:
                bf = np.asarray(boundary_flags, dtype=bool).reshape(-1)
                if len(bf) == len(xyz_raw):
                    visible_boundary = bf[vi]
            except Exception:
                visible_boundary = None
        checkpoint("finite_filter")
        if len(vi) < 3:
            timings["total_backend"] = time.perf_counter() - profile_start
            return {"empty": True, "profile_timings": timings}

        offset = xv.min(axis=0)
        xyz = (xv - offset).astype(np.float64)
        xr = xyz[:, 0].max() - xyz[:, 0].min()
        yr = xyz[:, 1].max() - xyz[:, 1].min()
        area = max(xr * yr, 1.0)
        n_pts = len(xyz)
        natural_spacing = np.sqrt(area / n_pts)
        checkpoint("normalize_extent")

        quality_mode = str(quality_mode or "normal").strip().lower()
        if quality_mode not in ("fast", "normal", "slow"):
            quality_mode = "normal"
        adaptive_enabled = os.environ.get("NAKSHA_SHADING_ADAPTIVE", "1").strip().lower() not in ("0", "false", "off", "no")
        use_count_first = False
        precision = max(natural_spacing * 0.3, 0.005)
        lod_factor = 1.0

        if quality_mode == "slow":
            # MicroStation-style Slow: retain every eligible finite point.
            # The confirmation and memory warning live in Display Mode before
            # this deliberately expensive background operation starts.
            unique_local = np.arange(n_pts, dtype=np.int64)
            n_unique = n_pts
            base_grid_unique_count = n_pts
            checkpoint("dedup_pass1")
            checkpoint("dedup_lod_extra")
            adaptive_target = n_pts
            adaptive_meta = {
                "enabled": False, "reason": "slow_all_points",
                "base": n_pts, "extra": 0, "target": n_pts,
            }
            print(
                "SHADING_ADAPTIVE status=disabled reason=slow_all_points "
                f"base={n_pts} final={n_pts} target={n_pts}"
            )
        else:
            final_target = (
                _SHADING_FAST_TARGET if quality_mode == "fast"
                else int(os.environ.get(
                    "NAKSHA_SHADING_ADAPTIVE_TARGET", str(_ADAPTIVE_DEFAULT_TARGET)
                ))
            )
            TARGET_MAX = (
                max(3, final_target // 2) if HAS_TRIANGLE
                else min(final_target, 500_000)
            )

            # Count-first grid deduplication avoids materializing an oversized
            # representative array before the final precision is known.
            use_count_first = n_pts > int(TARGET_MAX * 1.30)
            if use_count_first:
                n_unique = _grid_unique_count_fast_at_precision(xyz, precision)
                unique_local = None
            else:
                unique_local = _grid_dedup_at_precision(xyz, precision)
                n_unique = len(unique_local)
            base_grid_unique_count = int(n_unique)
            checkpoint("dedup_pass1")

            if n_unique > TARGET_MAX:
                lod_factor = np.sqrt(n_unique / TARGET_MAX)
                lod_factor = min(max(lod_factor, 1.0), 5.0)
                precision2 = precision * lod_factor
                n_unique2 = _grid_unique_count_fast_at_precision(xyz, precision2)
                if n_unique2 > TARGET_MAX * 1.3:
                    lod_factor2 = np.sqrt(n_unique2 / TARGET_MAX)
                    precision3 = precision2 * lod_factor2
                    unique_local = _grid_dedup_at_precision(xyz, precision3)
                    n_unique = len(unique_local)
                    lod_factor *= lod_factor2
                    precision = precision3
                else:
                    unique_local = _grid_dedup_at_precision(xyz, precision2)
                    n_unique = len(unique_local)
                    precision = precision2
            elif unique_local is None:
                unique_local = _grid_dedup_at_precision(xyz, precision)
                n_unique = len(unique_local)
            checkpoint("dedup_lod_extra")

            adaptive_target = final_target
            if adaptive_enabled and HAS_TRIANGLE and visible_boundary is not None:
                unique_local, adaptive_meta = _adaptive_boundary_representatives(
                    xyz, unique_local, visible_boundary, precision, adaptive_target
                )
                print(
                    "SHADING_ADAPTIVE "
                    f"status={'enabled' if adaptive_meta.get('enabled') else 'fallback'} "
                    f"reason={adaptive_meta.get('reason')} "
                    f"base={adaptive_meta.get('base', len(unique_local))} "
                    f"boundary_points={adaptive_meta.get('boundary_points', 0)} "
                    f"fine_candidates={adaptive_meta.get('fine_candidates', 0)} "
                    f"extra={adaptive_meta.get('extra', 0)} "
                    f"final={len(unique_local)} target={adaptive_meta.get('target', adaptive_target)} "
                    f"elapsed={adaptive_meta.get('elapsed', 0.0)*1000.0:.1f}ms"
                )
            else:
                adaptive_meta = {
                    "enabled": False,
                    "reason": "disabled" if not adaptive_enabled else (
                        "triangle_unavailable" if not HAS_TRIANGLE else "boundary_unavailable"
                    ),
                    "base": len(unique_local), "extra": 0,
                    "target": adaptive_target,
                }
                print(
                    "SHADING_ADAPTIVE status=fallback "
                    f"reason={adaptive_meta['reason']} "
                    f"base={len(unique_local)} final={len(unique_local)}"
                )
        checkpoint("adaptive_refinement")

        xyz_unique = xyz[unique_local]
        unique_indices_global = vi[unique_local]
        if visible_boundary is not None:
            representative_boundary = np.asarray(visible_boundary[unique_local], dtype=bool)

        xr2 = xyz_unique[:, 0].max() - xyz_unique[:, 0].min()
        yr2 = xyz_unique[:, 1].max() - xyz_unique[:, 1].min()
        data_extent = max(xr2, yr2)
        spacing = np.sqrt((xr2 * yr2) / max(len(xyz_unique), 1))
        xy = xyz_unique[:, :2]
        checkpoint("unique_materialize")

    faces = _do_triangulate(xy)
    checkpoint("delaunay")

    if len(faces) > 0:
        # Slow means fidelity, not geometric simplification. The historical
        # spacing/aspect thresholds reject skinny but valid triangles and can
        # orphan LiDAR samples around poles, wires, roof edges and vertical
        # returns. Slow only rejects numerical zero-area triangles; Fast and
        # Normal retain the established thresholds unchanged.
        slow_numeric_only = str(quality_mode or "normal").lower() == "slow"
        if slow_numeric_only:
            min_area = 1e-12
            min_aspect = 1e-10
            print(
                "SHADING_DEGENERATE_POLICY mode=slow policy=numeric_only "
                f"min_area={min_area:.1e} min_aspect={min_aspect:.1e}"
            )
        else:
            min_area = (spacing * 0.1) ** 2
            min_aspect = 0.001
        if HAS_NUMBA:
            keep = _numba_degenerate_filter(
                faces, xy, float(min_area), float(min_aspect)
            )
            if np.any(~keep):
                faces = faces[keep]
        else:
            p0 = xy[faces[:, 0]]
            p1 = xy[faces[:, 1]]
            p2 = xy[faces[:, 2]]
            cz = ((p1[:, 0] - p0[:, 0]) * (p2[:, 1] - p0[:, 1]) -
                  (p1[:, 1] - p0[:, 1]) * (p2[:, 0] - p0[:, 0]))
            ta = np.abs(cz) * 0.5
            e0 = ((p1 - p0) ** 2).sum(1)
            e1 = ((p2 - p1) ** 2).sum(1)
            e2 = ((p0 - p2) ** 2).sum(1)
            me = np.maximum(np.maximum(e0, e1), e2)
            nd = (ta > min_area) & (ta / np.maximum(me, 1e-20) > min_aspect)
            if np.any(~nd):
                faces = faces[nd]
    checkpoint("degenerate_filter")

    if len(faces) > 0:
        if is_sc:
            me = single_class_max_edge if single_class_max_edge else data_extent * 0.2
            faces = _filter_edges_by_absolute(faces, xy, me)
        else:
            mea = data_extent * 0.10
            faces = _filter_edges_3d_abs(faces, xyz_unique, mea)
            max_edge_factor = mea / max(spacing, 1e-9)
    checkpoint("edge_filter")

    faces, feature_meta = _feature_aware_filter_faces(
        faces, xyz_unique, representative_boundary, spacing
    )
    print(
        "SHADING_FEATURE_AWARE "
        f"status={'enabled' if feature_meta.get('enabled') else 'fallback'} "
        f"reason={feature_meta.get('reason')} "
        f"input_faces={feature_meta.get('input_faces', len(faces))} "
        f"removed={feature_meta.get('removed', 0)} "
        f"output_faces={feature_meta.get('output_faces', len(faces))} "
        f"ratio={feature_meta.get('removal_ratio', 0.0):.6f} "
        f"elapsed={feature_meta.get('elapsed', 0.0)*1000.0:.1f}ms"
    )
    checkpoint("feature_filter")

    if str(quality_mode or "normal").lower() == "slow" and len(faces) > 0:
        try:
            used = np.zeros(len(xyz_unique), dtype=np.bool_)
            face_chunk = 2_000_000
            for begin in range(0, len(faces), face_chunk):
                fc = faces[begin:begin + face_chunk]
                used[fc[:, 0]] = True
                used[fc[:, 1]] = True
                used[fc[:, 2]] = True
            orphan_count = int(len(used) - np.count_nonzero(used))
            coverage = 100.0 * (len(used) - orphan_count) / max(len(used), 1)
            print(
                "SHADING_POINT_COVERAGE mode=slow "
                f"input_vertices={len(used):,} referenced_vertices={len(used)-orphan_count:,} "
                f"orphan_vertices={orphan_count:,} coverage={coverage:.6f}%"
            )
        except Exception as exc:
            print(f"SHADING_POINT_COVERAGE status=unavailable reason={exc}")

    face_normals = None
    vertex_normals = None
    vertex_shade = None
    shade = None

    if len(faces) > 0:
        face_normals = _compute_face_normals(xyz_unique, faces)
    else:
        face_normals = np.array([]).reshape(0, 3)
    checkpoint("face_normals")

    if len(faces) > 0:
        shade = _compute_face_shade(
            xyz_unique, faces, azimuth, angle, ambient,
            face_normals=face_normals,
        )
    else:
        shade = np.array([])
    checkpoint("face_shade")

    if len(faces) > 0 and smooth_all_classes:
        vertex_normals = _compute_vertex_normals(
            xyz_unique, faces, face_normals,
        )
        checkpoint("vertex_normals")
        vertex_shade = _compute_shading(
            vertex_normals, azimuth, angle, ambient,
            z_values=xyz_unique[:, 2],
        )
        checkpoint("vertex_shade")
    else:
        vertex_normals = np.array([]).reshape(0, 3) if len(faces) == 0 else None
        vertex_shade = np.array([]) if len(faces) == 0 else None
        timings["vertex_normals"] = 0.0
        timings["vertex_shade"] = 0.0

    timings["total_backend"] = time.perf_counter() - profile_start

    return {
        "empty": False,
        "offset": offset,
        "unique_indices": unique_indices_global,
        "xyz_unique": xyz_unique,
        "xyz_final": xyz_unique + offset,
        "spacing": spacing,
        "max_edge_factor": max_edge_factor,
        "visible_classes_hash": hash(frozenset(visible_classes)),
        "n_visible_classes": nv,
        "visible_classes_set": visible_classes.copy(),
        "single_class_id": list(visible_classes)[0] if is_sc else None,
        "smooth_all_classes": smooth_all_classes,
        "faces": faces,
        "face_normals": face_normals,
        "vertex_normals": vertex_normals,
        "vertex_shade": vertex_shade,
        "shade": shade,
        "last_azimuth": azimuth,
        "last_angle": angle,
        "last_ambient": ambient,
        "data_hash": data_hash,
        "lod_factor": lod_factor,
        "dedup_strategy": ("representative_cache" if representative_cache_hit
                           else ("count_then_materialize" if use_count_first else "legacy_direct")),
        "representative_cache_hit": representative_cache_hit,
        "precision": precision,
        "base_grid_unique_count": base_grid_unique_count,
        "adaptive_meta": adaptive_meta,
        "adaptive_version": _ADAPTIVE_SHADING_VERSION,
        "representative_boundary": representative_boundary,
        "feature_meta": feature_meta,
        "feature_version": _FEATURE_AWARE_VERSION,
        "profile_timings": timings,
    }

class _ShadingProgressDialog(QProgressDialog):
    def __init__(self, parent=None):
        super().__init__("Building surface...", None, 0, 0, parent)
        self._allow_close = False
        self.setWindowTitle("Building Surface")
        self.setCancelButton(None)
        self.setAutoClose(False)
        self.setAutoReset(False)
        self.setWindowModality(Qt.ApplicationModal)
        self.setMinimumDuration(0)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.CustomizeWindowHint)

    def event(self, event):
        if (
            not self._allow_close
            and event.type() in (QEvent.Close, QEvent.Hide, QEvent.HideToParent)
        ):
            if hasattr(event, "ignore"):
                event.ignore()
            return True
        return super().event(event)

    def closeEvent(self, event):
        if self._allow_close:
            super().closeEvent(event)
        else:
            event.ignore()

    def close(self):
        if self._allow_close:
            return super().close()
        return False

    def hide(self):
        if self._allow_close:
            super().hide()

    def setVisible(self, visible):
        if visible or self._allow_close:
            super().setVisible(visible)

    def reject(self):
        if self._allow_close:
            super().reject()

    def finish(self):
        self._allow_close = True
        self.close()

def _process_shading_progress_events():
    QApplication.processEvents(QEventLoop.ExcludeUserInputEvents)

try:
    from numba import njit, prange
    HAS_NUMBA = True
except ImportError:
    HAS_NUMBA = False

# -- DIAGNOSTIC: Print accelerator status at import time --------------
def _print_accel_status():
    import sys
    frozen = getattr(sys, 'frozen', False)
    print(f"\n{'='*60}")
    print(f"?? SHADING ACCELERATOR STATUS ({'FROZEN EXE' if frozen else 'DEV'})")
    print(f"{'='*60}")
    print(f"   numba    : {'? LOADED' if HAS_NUMBA else '? MISSING — normals/shading will be 3-5x slower'}")
    print(f"   triangle : {'? LOADED' if HAS_TRIANGLE else '? MISSING — Delaunay will use scipy (2x slower)'}")
    print(f"   scipy    : {'? LOADED' if HAS_SCIPY else '? MISSING — no triangulation possible'}")
    
    # Check numpy threading (MKL vs OpenBLAS)
    try:
        np_config = np.__config__
        blas_info = str(getattr(np_config, 'blas_opt_info', {}))
        if 'mkl' in blas_info.lower():
            print(f"   numpy BLAS: ? MKL (multi-threaded)")
        elif 'openblas' in blas_info.lower():
            print(f"   numpy BLAS: ?? OpenBLAS")
        else:
            print(f"   numpy BLAS: ?? unknown ({blas_info[:80]})")
    except Exception:
        try:
            cfg = np.show_config(mode='dicts')
            print(f"   numpy BLAS: {cfg}")
        except Exception:
            print(f"   numpy BLAS: ?? cannot determine")
    
    # Check thread counts
    import os
    for var in ('MKL_NUM_THREADS', 'OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'NUMBA_NUM_THREADS'):
        val = os.environ.get(var, 'NOT SET')
        print(f"   {var}: {val}")
    
    try:
        import multiprocessing
        print(f"   CPU cores: {multiprocessing.cpu_count()}")
    except Exception:
        pass
    print(f"{'='*60}\n")

_print_accel_status()

if HAS_NUMBA:
    @njit(parallel=True, fastmath=True)
    def _compute_face_normals_fast(xyz, faces):
        n_faces = faces.shape[0]
        fn = np.empty((n_faces, 3), dtype=np.float64)
        for i in prange(n_faces):
            v0x, v0y, v0z = xyz[faces[i, 0]]
            v1x, v1y, v1z = xyz[faces[i, 1]]
            v2x, v2y, v2z = xyz[faces[i, 2]]
            ax, ay, az = v1x - v0x, v1y - v0y, v1z - v0z
            bx, by, bz = v2x - v0x, v2y - v0y, v2z - v0z
            nx = ay * bz - az * by
            ny = az * bx - ax * bz
            nz = ax * by - ay * bx
            length = np.sqrt(nx * nx + ny * ny + nz * nz)
            if length > 1e-10:
                nx, ny, nz = nx / length, ny / length, nz / length
            if nz < 0 and abs(nz) > 0.3:
                nx, ny, nz = -nx, -ny, -nz
            fn[i, 0] = nx; fn[i, 1] = ny; fn[i, 2] = nz
        return fn

    @njit(fastmath=True)
    def _compute_vertex_normals_fast(xyz, faces, fn):
        n_verts = xyz.shape[0]
        vn = np.zeros((n_verts, 3), dtype=np.float64)
        n_faces = faces.shape[0]
        for i in range(n_faces):
            v0, v1, v2 = faces[i, 0], faces[i, 1], faces[i, 2]
            p0x, p0y, p0z = xyz[v0]; p1x, p1y, p1z = xyz[v1]; p2x, p2y, p2z = xyz[v2]
            axx, ay, az = p1x - p0x, p1y - p0y, p1z - p0z
            bx, by, bz = p2x - p0x, p2y - p0y, p2z - p0z
            cx = ay * bz - az * by; cy = az * bx - axx * bz; cz = axx * by - ay * bx
            area = 0.5 * np.sqrt(cx * cx + cy * cy + cz * cz)
            wx, wy, wz = fn[i, 0] * area, fn[i, 1] * area, fn[i, 2] * area
            vn[v0, 0] += wx; vn[v0, 1] += wy; vn[v0, 2] += wz
            vn[v1, 0] += wx; vn[v1, 1] += wy; vn[v1, 2] += wz
            vn[v2, 0] += wx; vn[v2, 1] += wy; vn[v2, 2] += wz
        for i in range(n_verts):
            nx, ny, nz = vn[i, 0], vn[i, 1], vn[i, 2]
            length = np.sqrt(nx * nx + ny * ny + nz * nz)
            if length < 1e-10:
                vn[i, 0] = 0.0; vn[i, 1] = 0.0; vn[i, 2] = 1.0
            else:
                vn[i, 0] = nx / length; vn[i, 1] = ny / length; vn[i, 2] = nz / length
        return vn

    @njit(parallel=True, fastmath=True)
    def _compute_shading_fast(normals, z_values, lx, ly, lz, ambient, z_lo, z_range):
        """Standard MicroStation/ArcGIS hillshade: N.L dot product only."""
        n = normals.shape[0]
        shade = np.empty(n, dtype=np.float32)
        for i in prange(n):
            nx, ny, nz = normals[i, 0], normals[i, 1], normals[i, 2]
            ndotl = nx * lx + ny * ly + nz * lz
            intensity = max(ndotl, 0.0)
            if intensity < ambient:
                intensity = ambient
            shade[i] = min(max(intensity, 0.0), 1.0)
        return shade

    @njit(parallel=True, fastmath=True)
    def _fast_centroids_z(xyz, faces):
        n = faces.shape[0]
        fz = np.empty(n, dtype=np.float64)
        for i in prange(n):
            fz[i] = (xyz[faces[i, 0], 2] + xyz[faces[i, 1], 2] + xyz[faces[i, 2], 2]) / 3.0
        return fz

    @njit(parallel=True, cache=True)
    def _numba_affected_face_mask(faces, changed_vertex):
        n = faces.shape[0]
        affected = np.empty(n, dtype=np.bool_)
        for i in prange(n):
            affected[i] = (
                changed_vertex[faces[i, 0]]
                or changed_vertex[faces[i, 1]]
                or changed_vertex[faces[i, 2]]
            )
        return affected

    @njit(parallel=True, cache=True)
    def _numba_fill_affected_face_mask(faces, changed_vertex, affected):
        """Fill a caller-owned mask to avoid a 26M bool allocation per edit."""
        n = faces.shape[0]
        for i in prange(n):
            affected[i] = (
                changed_vertex[faces[i, 0]]
                or changed_vertex[faces[i, 1]]
                or changed_vertex[faces[i, 2]]
            )

    @njit(parallel=True, fastmath=True)
    def _numba_edge_filter(faces, xy, max_edge_sq):
        n = faces.shape[0]
        keep = np.empty(n, dtype=np.bool_)
        for i in prange(n):
            x0, y0 = xy[faces[i, 0], 0], xy[faces[i, 0], 1]
            x1, y1 = xy[faces[i, 1], 0], xy[faces[i, 1], 1]
            x2, y2 = xy[faces[i, 2], 0], xy[faces[i, 2], 1]
            e0 = (x1-x0)*(x1-x0) + (y1-y0)*(y1-y0)
            e1 = (x2-x1)*(x2-x1) + (y2-y1)*(y2-y1)
            e2 = (x0-x2)*(x0-x2) + (y0-y2)*(y0-y2)
            mx = e0
            if e1 > mx: mx = e1
            if e2 > mx: mx = e2
            keep[i] = mx <= max_edge_sq
        return keep

    @njit(parallel=True, fastmath=True)
    def _numba_feature_discontinuity_mask(faces, xyz, boundary, min_edge_sq, min_z_jump, min_slope):
        n = faces.shape[0]
        remove = np.zeros(n, dtype=np.bool_)
        for i in prange(n):
            a = faces[i, 0]; b = faces[i, 1]; c = faces[i, 2]
            bc = int(boundary[a]) + int(boundary[b]) + int(boundary[c])
            # Only mixed feature/non-feature triangles can bridge a detected break.
            if bc == 0 or bc == 3:
                continue
            x0, y0, z0 = xyz[a, 0], xyz[a, 1], xyz[a, 2]
            x1, y1, z1 = xyz[b, 0], xyz[b, 1], xyz[b, 2]
            x2, y2, z2 = xyz[c, 0], xyz[c, 1], xyz[c, 2]
            e0 = (x1-x0)*(x1-x0) + (y1-y0)*(y1-y0)
            e1 = (x2-x1)*(x2-x1) + (y2-y1)*(y2-y1)
            e2 = (x0-x2)*(x0-x2) + (y0-y2)*(y0-y2)
            mx = e0
            if e1 > mx: mx = e1
            if e2 > mx: mx = e2
            if mx <= min_edge_sq:
                continue
            zmin = z0
            if z1 < zmin: zmin = z1
            if z2 < zmin: zmin = z2
            zmax = z0
            if z1 > zmax: zmax = z1
            if z2 > zmax: zmax = z2
            dz = zmax - zmin
            if dz <= min_z_jump:
                continue
            slope = dz / (np.sqrt(mx) + 1e-12)
            if slope >= min_slope:
                remove[i] = True
        return remove

    @njit(parallel=True, fastmath=True)
    def _numba_degenerate_filter(faces, xy, min_area, min_aspect):
        n = faces.shape[0]
        keep = np.empty(n, dtype=np.bool_)
        for i in prange(n):
            x0, y0 = xy[faces[i, 0], 0], xy[faces[i, 0], 1]
            x1, y1 = xy[faces[i, 1], 0], xy[faces[i, 1], 1]
            x2, y2 = xy[faces[i, 2], 0], xy[faces[i, 2], 1]
            d0x, d0y = x1 - x0, y1 - y0
            cross_z = d0x * (y2 - y0) - d0y * (x2 - x0)
            tri_area = abs(cross_z) * 0.5
            d1x, d1y = x2 - x1, y2 - y1
            d2x, d2y = x0 - x2, y0 - y2
            e0 = d0x*d0x + d0y*d0y; e1 = d1x*d1x + d1y*d1y; e2 = d2x*d2x + d2y*d2y
            mx = e0
            if e1 > mx: mx = e1
            if e2 > mx: mx = e2
            keep[i] = (tri_area > min_area) and (tri_area / (mx + 1e-10) > min_aspect)
        return keep


    @njit(cache=True)
    def _numba_dense_grid_highest_z(xyz, precision, min_gx, min_gy, gx_span, gy_span):
        """Exact highest-Z representative per dense XY grid cell.

        Output order is ascending packed grid key, matching the legacy
        lexsort result. Equal-Z ties use the lowest source index so results
        are deterministic.
        """
        n_cells = gx_span * gy_span
        best_z = np.empty(n_cells, dtype=np.float64)
        best_idx = np.empty(n_cells, dtype=np.int64)
        for c in range(n_cells):
            best_z[c] = -np.inf
            best_idx[c] = -1
        for i in range(xyz.shape[0]):
            gx = int(np.floor(xyz[i, 0] / precision)) - min_gx
            gy = int(np.floor(xyz[i, 1] / precision)) - min_gy
            key = gx * gy_span + gy
            z = xyz[i, 2]
            old = best_idx[key]
            if z > best_z[key] or (z == best_z[key] and (old < 0 or i < old)):
                best_z[key] = z
                best_idx[key] = i
        count = 0
        for c in range(n_cells):
            if best_idx[c] >= 0:
                count += 1
        out = np.empty(count, dtype=np.int64)
        j = 0
        for c in range(n_cells):
            idx = best_idx[c]
            if idx >= 0:
                out[j] = idx
                j += 1
        return out


    @njit(cache=True)
    def _numba_dense_grid_unique_count(xyz, precision, min_gx, min_gy, gx_span, gy_span):
        """Exact occupied-cell count without sorting or representatives.

        This is used for adaptive-LOD decision passes. It preserves the exact
        cell count used by the legacy selector while avoiding a global sort and
        avoiding construction of representative indices that would be discarded.
        """
        occupied = np.zeros(gx_span * gy_span, dtype=np.uint8)
        for i in range(xyz.shape[0]):
            gx = int(np.floor(xyz[i, 0] / precision)) - min_gx
            gy = int(np.floor(xyz[i, 1] / precision)) - min_gy
            occupied[gx * gy_span + gy] = 1
        count = 0
        for i in range(occupied.shape[0]):
            count += occupied[i]
        return count

if HAS_NUMBA:
    def _warmup_numba_jit():
        try:
            _xyz = np.array([[0,0,0],[1,0,0],[0,1,0],[1,1,1]], dtype=np.float64)
            _f = np.array([[0,1,2],[1,2,3]], dtype=np.int32)
            _fn = _compute_face_normals_fast(_xyz, _f)
            _compute_vertex_normals_fast(_xyz, _f, _fn)
            _z = np.array([0.0, 1.0], dtype=np.float64)
            _compute_shading_fast(_fn, _z, .5,.5,.7, .25, 0., 1.)
            _fast_centroids_z(_xyz, _f)
            _xy = np.array([[0.,0.],[1.,0.],[0.,1.]], dtype=np.float64)
            _gf = np.array([[0,1,2]], dtype=np.int32)
            _numba_edge_filter(_gf, _xy, 100.)
            _numba_degenerate_filter(_gf, _xy, 1e-6, 0.001)
            _cv = np.array([True, False, False])
            _numba_affected_face_mask(_gf, _cv)
            _af = np.empty(len(_gf), dtype=np.bool_)
            _numba_fill_affected_face_mask(_gf, _cv, _af)
            _numba_dense_grid_highest_z(_xyz, 0.5, 0, 0, 3, 3)
            _numba_dense_grid_unique_count(_xyz, 0.5, 0, 0, 3, 3)
        except Exception:
            pass
    import threading
    threading.Thread(target=_warmup_numba_jit, daemon=True).start()


# -- DIAGNOSTIC: Print accelerator status at import time ----------------------
def _print_accel_status():
    import sys as _sys
    frozen = getattr(_sys, 'frozen', False)
    print(f"\n{'='*60}")
    print(f"?? SHADING ACCELERATOR STATUS ({'FROZEN EXE' if frozen else 'DEV MODE'})")
    print(f"{'='*60}")
    print(f"   numba    : {'? LOADED' if HAS_NUMBA else '? MISSING — normals/shading 3-5x slower'}")
    print(f"   triangle : {'? LOADED' if HAS_TRIANGLE else '? MISSING — Delaunay via scipy (2x slower)'}")
    print(f"   scipy    : {'? LOADED' if HAS_SCIPY else '? MISSING — no triangulation available'}")
    for var in ('MKL_NUM_THREADS', 'OMP_NUM_THREADS',
                'OPENBLAS_NUM_THREADS', 'NUMBA_NUM_THREADS'):
        print(f"   {var}: {os.environ.get(var, 'NOT SET')}")
    print(f"   CPU cores: {os.cpu_count()}")
    print(f"{'='*60}\n")

_print_accel_status()

def triangulate_with_triangle(xy):
    if not HAS_TRIANGLE: raise ImportError("no triangle")
    return tr.triangulate({'vertices': xy.astype(np.float64)}, 'Qz')['triangles'].astype(np.int32)

def triangulate_scipy_direct(xy):
    return Delaunay(xy).simplices.astype(np.int32)

def _do_triangulate(xy):
    xy = np.asarray(xy, dtype=np.float64)
    if xy.ndim != 2 or xy.shape[1] != 2 or len(xy) < 3:
        return np.empty((0, 3), dtype=np.int32)
    # Guard native triangulators against NaN/Inf and degenerate lines.
    if not np.isfinite(xy).all():
        return np.empty((0, 3), dtype=np.int32)
    if np.ptp(xy[:, 0]) <= 1e-12 or np.ptp(xy[:, 1]) <= 1e-12:
        return np.empty((0, 3), dtype=np.int32)
    if HAS_TRIANGLE:
        try:
            return triangulate_with_triangle(xy)
        except Exception:
            pass
    try:
        return triangulate_scipy_direct(xy)
    except Exception:
        return np.empty((0, 3), dtype=np.int32)

def _filter_edges_by_absolute(faces, xy, max_edge_length):
    if len(faces) == 0: return faces
    if HAS_NUMBA:
        return faces[_numba_edge_filter(faces, xy, max_edge_length * max_edge_length)]
    v0, v1, v2 = xy[faces[:,0]], xy[faces[:,1]], xy[faces[:,2]]
    e0 = ((v1-v0)**2).sum(1); e1 = ((v2-v1)**2).sum(1); e2 = ((v0-v2)**2).sum(1)
    return faces[np.maximum(np.maximum(e0, e1), e2) <= max_edge_length**2]

def _filter_edges_3d_abs(faces, xyz, max_xy, max_slope_ratio=10.0):
    if len(faces) == 0: return faces
    xy = xyz[:, :2]
    if HAS_NUMBA:
        return faces[_numba_edge_filter(faces, xy, max_xy * max_xy)]
    v0, v1, v2 = xy[faces[:,0]], xy[faces[:,1]], xy[faces[:,2]]
    e0 = ((v1-v0)**2).sum(1); e1 = ((v2-v1)**2).sum(1); e2 = ((v0-v2)**2).sum(1)
    return faces[np.maximum(np.maximum(e0, e1), e2) <= max_xy**2]

def _compute_face_normals(xyz, faces):
    if len(faces) == 0: return np.array([]).reshape(0, 3)
    if HAS_NUMBA: return _compute_face_normals_fast(xyz, faces)
    p0, p1, p2 = xyz[faces[:,0]], xyz[faces[:,1]], xyz[faces[:,2]]
    fn = np.cross(p1-p0, p2-p0)
    l = np.linalg.norm(fn, axis=1, keepdims=True)
    fn = fn / np.maximum(l, 1e-10)
    m = (fn[:,2] < 0) & (np.abs(fn[:,2]) > 0.3); fn[m] *= -1
    return fn

def _compute_vertex_normals(xyz, faces, face_normals):
    if len(faces) == 0:
        vn = np.zeros((len(xyz), 3), dtype=np.float64); vn[:,2] = 1.0; return vn.astype(np.float32)
    if HAS_NUMBA:
        return _compute_vertex_normals_fast(xyz, faces, face_normals).astype(np.float32)
    n = len(xyz); vn = np.zeros((n, 3), dtype=np.float64)
    p0, p1, p2 = xyz[faces[:,0]], xyz[faces[:,1]], xyz[faces[:,2]]
    a = 0.5 * np.linalg.norm(np.cross(p1-p0, p2-p0), axis=1)
    w = face_normals * a[:, np.newaxis]
    np.add.at(vn, faces[:,0], w); np.add.at(vn, faces[:,1], w); np.add.at(vn, faces[:,2], w)
    l = np.linalg.norm(vn, axis=1, keepdims=True)
    vn = vn / np.maximum(l, 1e-10); vn[l.ravel() < 1e-10] = [0,0,1]
    return vn.astype(np.float32)

def _recompute_vertex_normals_partial(cache, patch_face_start_idx):
    if cache.face_normals is None or cache.faces is None or len(cache.faces) == 0: return
    if patch_face_start_idx >= len(cache.faces): return
    if cache.xyz_unique is None or len(cache.xyz_unique) == 0: return
    nv = len(cache.xyz_unique)
    if cache.vertex_normals is None or len(cache.vertex_normals) < nv:
        no = len(cache.vertex_normals) if cache.vertex_normals is not None else 0
        na = nv - no; nn = np.zeros((na, 3), dtype=np.float32); nn[:,2] = 1.0
        cache.vertex_normals = nn if cache.vertex_normals is None or no == 0 else np.vstack([cache.vertex_normals, nn])
    if cache.vertex_shade is None or len(cache.vertex_shade) < nv:
        no = len(cache.vertex_shade) if cache.vertex_shade is not None else 0
        na = nv - no; amb = cache.last_ambient if cache.last_ambient >= 0 else 0.25
        ns = np.full(na, float(amb), dtype=np.float32)
        cache.vertex_shade = ns if cache.vertex_shade is None or no == 0 else np.concatenate([cache.vertex_shade, ns])
    pv2 = np.unique(cache.faces[patch_face_start_idx:].ravel()); pv2 = pv2[pv2 < nv]
    if len(pv2) == 0: return
    am = np.isin(cache.faces[:,0], pv2) | np.isin(cache.faces[:,1], pv2) | np.isin(cache.faces[:,2], pv2)
    af = cache.faces[am]; afn = cache.face_normals[am]
    if len(af) == 0: return
    p0 = cache.xyz_unique[af[:,0]]; p1 = cache.xyz_unique[af[:,1]]; p2 = cache.xyz_unique[af[:,2]]
    a = 0.5 * np.linalg.norm(np.cross(p1-p0, p2-p0), axis=1); w = afn * a[:, None]
    cache.vertex_normals[pv2] = 0.0
    np.add.at(cache.vertex_normals, af[:,0], w); np.add.at(cache.vertex_normals, af[:,1], w); np.add.at(cache.vertex_normals, af[:,2], w)
    l = np.linalg.norm(cache.vertex_normals[pv2], axis=1, keepdims=True)
    cache.vertex_normals[pv2] /= np.maximum(l, 1e-10)
    if cache.vertex_shade is not None and len(cache.vertex_shade) >= nv and cache.last_azimuth >= 0:
        pn = cache.vertex_normals[pv2]; N = pn.astype(np.float64)
        zenith_rad = np.radians(90.0 - cache.last_angle)
        az_math_rad = np.radians(360.0 - cache.last_azimuth + 90.0)
        lx = np.sin(zenith_rad) * np.cos(az_math_rad)
        ly = np.sin(zenith_rad) * np.sin(az_math_rad)
        lz = np.cos(zenith_rad)
        amb = cache.last_ambient
        NdL = N[:,0]*lx + N[:,1]*ly + N[:,2]*lz
        ni = np.maximum(NdL, 0.0)
        ni = np.maximum(ni, amb)
        cache.vertex_shade[pv2] = np.clip(ni, 0., 1.).astype(np.float32)


def _compute_shading(normals, azimuth, angle, ambient, z_values=None):
    """Standard MicroStation/ArcGIS hillshade algorithm.
    
    Uses the standard formula:
      Hillshade = cos(zenith)*cos(slope) + sin(zenith)*sin(slope)*cos(azimuth_math - aspect)
    Which equals N . L where L is constructed with geographic-to-math azimuth conversion.
    """
    if len(normals) == 0: return np.array([])
    zenith_rad = np.radians(90.0 - angle)
    az_math_rad = np.radians(360.0 - azimuth + 90.0)
    lx = np.sin(zenith_rad) * np.cos(az_math_rad)
    ly = np.sin(zenith_rad) * np.sin(az_math_rad)
    lz = np.cos(zenith_rad)
    if HAS_NUMBA:
        return _compute_shading_fast(normals, z_values if z_values is not None else np.empty(0, dtype=np.float64),
                                     lx, ly, lz, ambient, 0., 0.)
    ld = np.array([lx, ly, lz], dtype=np.float64)
    NdL = (normals * ld).sum(1)
    ni = np.maximum(NdL, 0.0)
    ni = np.maximum(ni, ambient)
    return np.clip(ni, 0., 1.).astype(np.float32)

def _compute_face_shade(xyz, faces, azimuth, angle, ambient, face_normals=None, z_values=None):
    if xyz is None or faces is None or len(faces) == 0: return np.array([], dtype=np.float32)
    if face_normals is None or len(face_normals) != len(faces): face_normals = _compute_face_normals(xyz, faces)
    fz = _fast_centroids_z(xyz, faces) if HAS_NUMBA else xyz[faces, 2].mean(axis=1)
    return _compute_shading(face_normals, azimuth, angle, ambient, z_values=fz)

def _remove_shaded_edge_overlay(app):
    p = getattr(app, 'vtk_widget', None)
    if p:
        try: p.remove_actor("shaded_mesh_edges", render=False)
        except Exception:
            pass
    app._shaded_mesh_edge_actor = None; app._shaded_mesh_edge_polydata = None


def _remove_shading_class_detail_overlays(app, remove_static=True, remove_live=True):
    """Remove only the MicroStation-fidelity class-detail point layers."""
    plotter = getattr(app, 'vtk_widget', None)
    if plotter is not None:
        if remove_static:
            try:
                plotter.remove_actor(_SHADING_CLASS_DETAIL_NAME, render=False)
            except Exception:
                pass
        if remove_live:
            try:
                plotter.remove_actor(_SHADING_LIVE_CLASS_DETAIL_NAME, render=False)
            except Exception:
                pass
    if remove_static:
        app._shading_class_detail_actor = None
        app._shading_class_detail_buffers = None
    if remove_live:
        app._shading_live_class_detail_actor = None
        app._shading_live_class_detail_buffers = None


def _shading_class_detail_enabled(app, cache) -> bool:
    """Raw point-sprite fidelity overlay is retired; fidelity is facet-based."""
    return False


def _microstation_color_blend_enabled(app, cache) -> bool:
    """Enable TerraScan/MicroStation-style class-color interpolation safely.

    Geometry density remains controlled exclusively by the selected shading
    quality (Fast / Normal / Slow).  For every MULTI-CLASS quality, presentation
    is identical: canonical class RGB is stored per representative vertex, the
    GPU barycentrically interpolates those colors inside each triangle, and the
    triangle keeps flat/faceted slope lighting.

    Single-class shading intentionally keeps its established cell/facet path so
    all existing local add/remove, undo/redo and topology-edit behaviour remains
    untouched.  The environment switch is an emergency rollback without a code
    change.
    """
    flag = os.environ.get('NAKSHA_SHADING_MICROSTATION_BLEND', '1').strip().lower()
    if flag in ('0', 'false', 'off', 'no'):
        return False
    return int(getattr(cache, 'n_visible_classes', 0) or 0) > 1



def _crisp_multiclass_facets_enabled(app, cache) -> bool:
    """Use a crisp cell-shaded base plus a mixed-class blend overlay.

    This is a presentation-only policy for multi-class Shaded Classification.
    The canonical point set, Delaunay topology, class membership, cache keys and
    classification edit machinery are untouched.  Same-color/same-class terrain
    is rendered as one solid flat-shaded triangle per TIN face; only faces whose
    displayed vertex colours differ are overlaid with barycentric class blending.
    """
    flag = os.environ.get('NAKSHA_SHADING_CRISP_FACETS', '1').strip().lower()
    if flag in ('0', 'false', 'off', 'no'):
        return False
    return bool(
        _microstation_color_blend_enabled(app, cache)
        and int(getattr(cache, 'n_visible_classes', 0) or 0) > 1
    )


def _crisp_blend_ambient_floor(app) -> float:
    """Return the shared anti-black floor for crisp and GPU-lit shading.

    The crisp base is already rendered as solid CELL RGB with VTK lighting
    disabled, so it no longer needs the old 0.42 safety clamp that was used to
    hide GPU normal/scan-line chatter.  Keeping that legacy clamp made broad,
    low ground faces stay almost uniformly bright even when Sharpness was
    increased.  Use the same small floor as the mixed-class GPU overlays so
    Ambient remains the brightness control and Sharpness can expose real TIN
    facet contrast all the way down to the lowest terrain points.
    """
    try:
        floor = float(os.environ.get(
            'NAKSHA_SHADING_BLEND_AMBIENT_FLOOR', '0.08'
        ))
    except Exception:
        floor = 0.08
    return float(np.clip(floor, 0.0, 0.30))


def _crisp_sharpness_contrast_gain(sharpness_value) -> float:
    """Map the 0..999 Sharpness control to pure-facet contrast gain.

    Sharpness=45 is deliberately gain=1 so the established default look stays
    stable.  Values below 45 soften tiny ground-slope differences; values above
    45 progressively amplify them.  The 90..200 overdrive range is intentionally
    strong, then continues with the existing smaller tail through 999.

    This is a *presentation-only* multiplier around the intensity of a
    horizontal face.  It never changes vertices, triangles, classes or the
    user's Ambient value.
    """
    legacy, overdrive = _shading_sharpness_response(sharpness_value)
    # 0 -> 0.55, 45 -> 1.00, 90 -> 1.45.
    gain = 0.55 + 0.90 * legacy
    # Make 90..200 very visible on low/small-slope terrain while retaining a
    # progressive tail to 999.  At 200 gain is ~5.95; at 999 ~6.45.
    gain += 5.0 * overdrive
    return float(np.clip(gain, 0.35, 6.50))


def _crisp_shade_chunk(app, cache, face_ids):
    """Return Sharpness-aware CELL shade for the solid crisp TIN base.

    ``cache.shade`` contains the raw hillshade from the exact face normals.  The
    crisp base actor itself has lighting disabled, therefore every Sharpness
    change that should be visible on pure/ground faces has to be baked into
    these CELL RGB values.

    Contrast is amplified around a horizontal-face pivot instead of simply
    multiplying brightness.  This keeps a flat ground face at approximately the
    same brightness while making small slope/aspect differences between adjacent
    ground triangles increasingly clear.  Ambient remains only the shadow floor.
    """
    ids = np.asarray(face_ids, dtype=np.int64)
    if cache.shade is None or len(cache.shade) != len(cache.faces):
        return np.ones(len(ids), dtype=np.float32)
    raw = np.asarray(cache.shade[ids], dtype=np.float32)
    try:
        src_ambient = float(np.clip(
            getattr(cache, 'last_ambient', getattr(app, 'shade_ambient', 0.25)),
            0.0, 0.95,
        ))
    except Exception:
        src_ambient = 0.25
    shadow_floor = max(src_ambient, _crisp_blend_ambient_floor(app))

    sharpness_angle = _shading_sharpness_angle(app)
    gain = _crisp_sharpness_contrast_gain(sharpness_angle)

    # cache.last_angle is the *effective* light elevation used to generate raw
    # hillshade.  Above Sharpness 90 it becomes progressively more grazing.
    try:
        effective_elevation = float(np.clip(cache.last_angle, 5.0, 85.0))
    except Exception:
        effective_elevation = _shading_fixed_light_elevation(app)
    base_elevation = _shading_fixed_light_elevation(app)

    # For a horizontal normal N=(0,0,1), N.L == sin(light elevation).
    # Re-centre around that value so a perfectly flat face does not get darker
    # merely because Sharpness is raised.  Only deviations caused by actual TIN
    # slope/aspect are amplified.
    raw_horizontal = max(
        float(np.sin(np.radians(effective_elevation))), src_ambient
    )
    target_horizontal = max(
        float(np.sin(np.radians(base_elevation))), shadow_floor
    )

    sharpened = target_horizontal + (raw - raw_horizontal) * gain
    return np.asarray(
        np.clip(sharpened, shadow_floor, 1.0), dtype=np.float32
    )


def _build_crisp_base_face_colors(app, cache, vertex_classes, visible_classes,
                                  face_ids=None):
    """Build deterministic flat RGB for TIN faces in bounded-memory chunks.

    For pure faces, using vertex-0's class is exact because all three displayed
    colours are identical.  Mixed-colour faces are covered by the static blend
    overlay, so their base colour is only a hidden fallback.  This avoids the
    3x 69M-class temporary arrays previously required by face majority/peak
    materialisation and keeps Slow/all-points RAM bounded.
    """
    faces = cache.faces if face_ids is None else cache.faces[np.asarray(face_ids, dtype=np.int64)]
    n_faces = len(faces)
    if n_faces == 0:
        return np.empty((0, 3), dtype=np.uint8)

    vc = set(int(c) for c in (visible_classes or ()))
    classes = np.asarray(vertex_classes)
    palette_codes = [int(c) for c in getattr(app, 'class_palette', {}).keys()]
    class_max = int(classes.max()) if classes.size else 0
    max_code = max([255, class_max] + palette_codes)
    lut = np.zeros((max_code + 1, 3), dtype=np.float32)
    for code, entry in getattr(app, 'class_palette', {}).items():
        ci = int(code)
        if 0 <= ci <= max_code and ci in vc:
            lut[ci] = entry.get('color', (128, 128, 128))

    out = np.empty((n_faces, 3), dtype=np.uint8)
    chunk = max(50_000, int(os.environ.get(
        'NAKSHA_SHADING_CRISP_COLOR_CHUNK', '1000000'
    )))
    for begin in range(0, n_faces, chunk):
        end = min(begin + chunk, n_faces)
        fc = faces[begin:end]
        cid = np.asarray(classes[fc[:, 0]], dtype=np.int64)
        rgb = lut[np.clip(cid, 0, max_code)]
        if face_ids is None:
            shade_ids = np.arange(begin, end, dtype=np.int64)
        else:
            shade_ids = np.asarray(face_ids, dtype=np.int64)[begin:end]
        sh = _crisp_shade_chunk(app, cache, shade_ids)
        out[begin:end] = np.clip(rgb * sh[:, None], 0, 255).astype(np.uint8)
    return out


def _display_color_key_lut(app, vertex_classes, visible_classes):
    """Pack visible palette RGB into one integer per class for fast comparisons."""
    classes = np.asarray(vertex_classes)
    palette_codes = [int(c) for c in getattr(app, 'class_palette', {}).keys()]
    class_max = int(classes.max()) if classes.size else 0
    max_code = max([255, class_max] + palette_codes)
    lut = np.zeros(max_code + 1, dtype=np.int32)
    vc = set(int(c) for c in (visible_classes or ()))
    for code, entry in getattr(app, 'class_palette', {}).items():
        ci = int(code)
        if not (0 <= ci <= max_code and ci in vc):
            continue
        try:
            r, g, b = [int(v) & 255 for v in entry.get('color', (128, 128, 128))[:3]]
        except Exception:
            r, g, b = 128, 128, 128
        lut[ci] = (r << 16) | (g << 8) | b
    return lut


def _crisp_mixed_face_budget(total_faces):
    """Return the safe mixed-face budget for the crisp barycentric overlay.

    Large all-class tiles commonly have more than eight million mixed faces.
    Falling back to full-vertex blending at that point reintroduces visible
    scan-line hatching on otherwise pure ground facets. Scale the default with
    topology size while retaining a bounded upper limit and the existing
    environment override.
    """
    configured = os.environ.get('NAKSHA_SHADING_CRISP_MAX_MIXED_FACES')
    if configured is not None:
        try:
            return max(0, int(configured))
        except (TypeError, ValueError):
            pass
    try:
        face_count = max(0, int(total_faces))
    except (TypeError, ValueError):
        face_count = 0
    scaled = int(np.ceil(face_count * 0.25))
    return min(20_000_000, max(12_000_000, scaled))


def _collect_mixed_display_faces(app, cache, vertex_classes, visible_classes):
    """Return faces whose three displayed class colours are not identical.

    The scan is chunked, so the 69M-face Slow mesh never materialises three
    full-size class arrays.  A safety ceiling prevents a pathological dataset
    from creating an unreasonably large secondary blend actor; in that rare case
    the renderer falls back to the existing full-vertex blend path.
    """
    faces = cache.faces
    if faces is None or len(faces) == 0:
        return np.empty(0, dtype=np.int64), 0, True
    classes = np.asarray(vertex_classes)
    key_lut = _display_color_key_lut(app, classes, visible_classes)
    chunk = max(100_000, int(os.environ.get(
        'NAKSHA_SHADING_CRISP_SCAN_CHUNK', '2000000'
    )))
    max_mixed = _crisp_mixed_face_budget(len(faces))
    parts = []
    total = 0
    for begin in range(0, len(faces), chunk):
        fc = faces[begin:begin + chunk]
        c0 = np.asarray(classes[fc[:, 0]], dtype=np.int64)
        c1 = np.asarray(classes[fc[:, 1]], dtype=np.int64)
        c2 = np.asarray(classes[fc[:, 2]], dtype=np.int64)
        k0 = key_lut[np.clip(c0, 0, len(key_lut) - 1)]
        k1 = key_lut[np.clip(c1, 0, len(key_lut) - 1)]
        k2 = key_lut[np.clip(c2, 0, len(key_lut) - 1)]
        local = np.flatnonzero((k0 != k1) | (k0 != k2)).astype(np.int64, copy=False)
        if local.size:
            total += int(local.size)
            if max_mixed and total > max_mixed:
                return None, total, False
            parts.append(local + begin)
    if not parts:
        return np.empty(0, dtype=np.int64), 0, True
    return np.concatenate(parts), total, True


def _remove_static_multiclass_blend_overlays(app):
    """Remove presentation-only static mixed-class blend actors."""
    plotter = getattr(app, 'vtk_widget', None)
    entries = getattr(app, '_shading_static_blend_overlays', None) or []
    removed = 0
    if plotter is not None:
        names = set()
        for entry in entries:
            if isinstance(entry, dict) and entry.get('name'):
                names.add(str(entry['name']))
        try:
            names.update(
                str(name) for name in plotter.actors.keys()
                if str(name).startswith(_MULTICLASS_STATIC_BLEND_PREFIX)
            )
        except Exception:
            pass
        for name in names:
            try:
                plotter.remove_actor(name, render=False)
                removed += 1
            except Exception:
                pass
    app._shading_static_blend_overlays = []
    return removed


def _attach_explicit_cell_normals(poly, normals):
    """Attach exact face normals without creating point normals/smoothing."""
    if poly is None or normals is None or len(normals) == 0:
        return None
    buf = np.ascontiguousarray(np.asarray(normals), dtype=np.float32)
    vtk_normals = numpy_support.numpy_to_vtk(buf, deep=False)
    vtk_normals.SetName('Normals')
    poly.GetCellData().SetNormals(vtk_normals)
    poly.GetCellData().Modified()
    poly.Modified()
    return buf


def _sharp_multiclass_tip_enabled(app, cache) -> bool:
    """Presentation-only sharpened apex for isolated class vertices.

    The full Delaunay, all-point coverage and canonical barycentric blend remain
    unchanged.  This layer adds a small coplanar triangular core only when one
    displayed vertex colour differs from the other two.  It makes the actual
    LiDAR/class vertex read as a crisp apex instead of a soft blurred cone.
    """
    # Disabled by default: on dense multi-class clouds this presentation layer
    # creates millions of small solid-colour triangles which read as coloured
    # spots at point/class tips. Keep an explicit opt-in for diagnostics only.
    flag = os.environ.get('NAKSHA_SHADING_SHARP_TIPS', '0').strip().lower()
    if flag in ('0', 'false', 'off', 'no'):
        return False
    return bool(
        _crisp_multiclass_facets_enabled(app, cache)
        and int(getattr(cache, 'n_visible_classes', 0) or 0) > 1
    )


_SHADING_SHARPNESS_LEGACY_MAX = 90.0
_SHADING_SHARPNESS_MAX = 999.0
_SHADING_SHARPNESS_STRONG_MAX = 200.0
_SHADING_SHARPNESS_STRONG_RESPONSE = 0.90


def _shading_sharpness_angle(app, explicit=None) -> float:
    """Return the Shaded-Classification facet sharpness control (0..999).

    Values 0..90 preserve the previous response exactly.  Values above 90 are
    an *overdrive* range: they continue reducing opposite fill-light energy and
    lower the lighting incidence angle for visible facet contrast, while
    Ambient remains the brightness control.
    """
    if explicit is None:
        explicit = getattr(
            app, 'shading_sharpness_angle',
            getattr(app, 'last_shade_angle', 45.0),
        )
    try:
        return float(np.clip(float(explicit), 0.0, _SHADING_SHARPNESS_MAX))
    except Exception:
        return 45.0


def _shading_sharpness_response(sharpness_value):
    """Return ``(legacy, overdrive)`` normalized sharpness responses.

    ``legacy`` is 0..1 and is byte-for-byte compatible with the old 0..90
    behaviour. ``overdrive`` supplies a strong, linear 90..200 response, then
    retains a smaller progressive tail through 999. This makes every change up
    to 200 clearly visible without changing total directional light energy.
    """
    try:
        value = float(np.clip(
            float(sharpness_value), 0.0, _SHADING_SHARPNESS_MAX
        ))
    except Exception:
        value = 45.0
    legacy = float(np.clip(value / _SHADING_SHARPNESS_LEGACY_MAX, 0.0, 1.0))
    if value <= _SHADING_SHARPNESS_LEGACY_MAX:
        return legacy, 0.0
    strong_span = max(
        _SHADING_SHARPNESS_STRONG_MAX - _SHADING_SHARPNESS_LEGACY_MAX,
        1.0,
    )
    if value <= _SHADING_SHARPNESS_STRONG_MAX:
        strong_t = (value - _SHADING_SHARPNESS_LEGACY_MAX) / strong_span
        overdrive = _SHADING_SHARPNESS_STRONG_RESPONSE * strong_t
    else:
        tail_span = max(
            _SHADING_SHARPNESS_MAX - _SHADING_SHARPNESS_STRONG_MAX,
            1.0,
        )
        tail_t = (value - _SHADING_SHARPNESS_STRONG_MAX) / tail_span
        overdrive = (
            _SHADING_SHARPNESS_STRONG_RESPONSE
            + (1.0 - _SHADING_SHARPNESS_STRONG_RESPONSE) * tail_t
        )
    return legacy, float(np.clip(overdrive, 0.0, 1.0))


def _shading_fixed_light_elevation(app) -> float:
    """Stable internal sun elevation for multi-class shading."""
    value = getattr(app, 'shading_light_elevation', None)
    if value is None:
        value = os.environ.get('NAKSHA_SHADING_LIGHT_ELEVATION', '45.0')
    try:
        return float(np.clip(float(value), 5.0, 85.0))
    except Exception:
        return 45.0


def _shading_effective_light_elevation(app, sharpness_overdrive=0.0) -> float:
    """Return the render light elevation, with visible over-90 sharpening.

    The legacy 0..90 range keeps the configured 45-degree light exactly. Above
    90, lowering the grazing angle progressively through 200 increases actual
    facet-to-facet contrast. This avoids the old invisible 95/5 -> 100/0
    key/fill-only tail while leaving topology, colours and Ambient untouched.
    """
    base = _shading_fixed_light_elevation(app)
    overdrive = float(np.clip(sharpness_overdrive, 0.0, 1.0))
    minimum = 12.0
    return float(np.clip(base - (base - minimum) * overdrive, minimum, 85.0))



def _sharp_tip_parameters(app=None):
    """Derive isolated-class apex crispness from the Sharpness control.

    Higher sharpness => a smaller, more needle-like solid core and stronger
    retention of the apex class colour. Explicit environment overrides still
    take precedence for production tuning/rollback.
    """
    sharpness = _shading_sharpness_angle(app) if app is not None else 45.0
    t, overdrive = _shading_sharpness_response(sharpness)

    if 'NAKSHA_SHADING_SHARP_TIP_FRACTION' in os.environ:
        try:
            fraction = float(os.environ['NAKSHA_SHADING_SHARP_TIP_FRACTION'])
        except Exception:
            fraction = 0.30
    else:
        # Preserve the old 0..90 curve exactly, then continue tightening the
        # apex from 0.16 toward ~0.04 through the 90..999 overdrive range.
        fraction = 0.48 - 0.32 * (t ** 0.85)
        if overdrive > 0.0:
            fraction -= 0.12 * overdrive

    if 'NAKSHA_SHADING_SHARP_TIP_COLOR_RETENTION' in os.environ:
        try:
            edge_retention = float(os.environ['NAKSHA_SHADING_SHARP_TIP_COLOR_RETENTION'])
        except Exception:
            edge_retention = 0.86
    else:
        edge_retention = 0.70 + 0.28 * (t ** 0.75)
        if overdrive > 0.0:
            edge_retention += 0.02 * overdrive

    fraction = float(np.clip(fraction, 0.035, 0.60))
    edge_retention = float(np.clip(edge_retention, 0.55, 1.0))
    return fraction, edge_retention

def _build_sharp_multiclass_tip_actor(app, cache, *, name, xyz, faces,
                                      vertex_classes, visible_classes,
                                      face_normals=None, live=False):
    """Build a tiny crisp triangular core around unique-colour face vertices.

    No base vertex/face is moved or replaced.  The small overlay is constructed
    strictly inside the existing face plane, uses the exact original face normal
    for lighting, and keeps the surrounding barycentric blend fully visible.
    """
    plotter = getattr(app, 'vtk_widget', None)
    if plotter is None or not _sharp_multiclass_tip_enabled(app, cache):
        return None, None, 0

    faces = np.asarray(faces, dtype=np.int32)
    xyz = np.asarray(xyz)
    classes = np.asarray(vertex_classes)
    if len(faces) == 0 or len(xyz) == 0 or len(classes) != len(xyz):
        return None, None, 0

    vc = set(int(c) for c in (visible_classes or ()))
    vrgb = np.ascontiguousarray(_shading_palette_rgb(app, classes, vc), dtype=np.uint8)
    packed_key = (
        (vrgb[:, 0].astype(np.int32) << 16)
        | (vrgb[:, 1].astype(np.int32) << 8)
        | vrgb[:, 2].astype(np.int32)
    )

    k0 = packed_key[faces[:, 0]]
    k1 = packed_key[faces[:, 1]]
    k2 = packed_key[faces[:, 2]]
    m0 = (k0 != k1) & (k1 == k2)
    m1 = (k1 != k0) & (k0 == k2)
    m2 = (k2 != k0) & (k0 == k1)
    n_tip = int(np.count_nonzero(m0) + np.count_nonzero(m1) + np.count_nonzero(m2))
    if n_tip == 0:
        return None, None, 0

    max_faces = max(0, int(os.environ.get(
        'NAKSHA_SHADING_SHARP_TIP_MAX_FACES', '2000000'
    )))

    rows0 = np.flatnonzero(m0).astype(np.int64, copy=False)
    rows1 = np.flatnonzero(m1).astype(np.int64, copy=False)
    rows2 = np.flatnonzero(m2).astype(np.int64, copy=False)
    rows = np.concatenate((rows0, rows1, rows2))
    tip_ids = np.concatenate((
        faces[rows0, 0], faces[rows1, 1], faces[rows2, 2]
    )).astype(np.int64, copy=False)
    side_b = np.concatenate((
        faces[rows0, 1], faces[rows1, 0], faces[rows2, 0]
    )).astype(np.int64, copy=False)
    side_c = np.concatenate((
        faces[rows0, 2], faces[rows1, 2], faces[rows2, 1]
    )).astype(np.int64, copy=False)

    if max_faces and len(rows) > max_faces:
        # Deterministic thinning is a safety valve only.  Normal static chunks
        # are 250k faces, so the production path keeps every sharp tip.
        pick = np.linspace(0, len(rows) - 1, max_faces, dtype=np.int64)
        rows = rows[pick]
        tip_ids = tip_ids[pick]
        side_b = side_b[pick]
        side_c = side_c[pick]

    fraction, edge_retention = _sharp_tip_parameters(app)
    p0 = np.asarray(xyz[tip_ids], dtype=np.float64)
    pb = np.asarray(xyz[side_b], dtype=np.float64)
    pc = np.asarray(xyz[side_c], dtype=np.float64)
    p1 = p0 + fraction * (pb - p0)
    p2 = p0 + fraction * (pc - p0)

    out_xyz = np.empty((len(rows) * 3, 3), dtype=np.float64)
    out_xyz[0::3] = p0
    out_xyz[1::3] = p1
    out_xyz[2::3] = p2

    c0 = vrgb[tip_ids].astype(np.float32)
    cb = vrgb[side_b].astype(np.float32)
    cc = vrgb[side_c].astype(np.float32)
    c1 = edge_retention * c0 + (1.0 - edge_retention) * cb
    c2 = edge_retention * c0 + (1.0 - edge_retention) * cc
    out_rgb = np.empty((len(rows) * 3, 3), dtype=np.uint8)
    out_rgb[0::3] = np.clip(c0, 0, 255).astype(np.uint8)
    out_rgb[1::3] = np.clip(c1, 0, 255).astype(np.uint8)
    out_rgb[2::3] = np.clip(c2, 0, 255).astype(np.uint8)

    tri = np.arange(len(rows) * 3, dtype=np.int32).reshape(-1, 3)
    packed = np.empty(len(rows) * 4, dtype=np.int32)
    packed[0::4] = 3
    packed[1::4] = tri[:, 0]
    packed[2::4] = tri[:, 1]
    packed[3::4] = tri[:, 2]

    patch = pv.PolyData(np.ascontiguousarray(out_xyz), packed)
    patch.point_data['RGB'] = np.ascontiguousarray(out_rgb, dtype=np.uint8)

    normal_buf = None
    if face_normals is not None and len(face_normals) == len(faces):
        normal_buf = _attach_explicit_cell_normals(
            patch, np.asarray(face_normals)[rows]
        )

    try:
        plotter.remove_actor(name, render=False)
    except Exception:
        pass

    actor = plotter.add_mesh(
        patch,
        scalars='RGB',
        rgb=True,
        show_edges=False,
        lighting=True,
        smooth_shading=False,
        preference='point',
        name=name,
        render=False,
    )
    if actor is None:
        return None, None, 0

    try:
        setattr(actor, '_is_shading_mesh', True)
        setattr(actor, '_is_shading_sharp_tip', True)
        actor.PickableOff()
        _configure_microstation_color_blend_lighting(app, actor)
        mapper = actor.GetMapper()
        if mapper is not None:
            mapper.StaticOn()
            mapper.SetResolveCoincidentTopologyToPolygonOffset()
            mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(
                -12.0 if live else -7.0,
                -12.0 if live else -7.0,
            )
            mapper.InterpolateScalarsBeforeMappingOff()
    except Exception:
        pass

    buffers = (
        out_xyz, out_rgb, packed, tri, rows, tip_ids, side_b, side_c,
        normal_buf, vrgb,
    )
    actor._naksha_sharp_tip_buffers = buffers
    return actor, buffers, int(len(rows))


def _build_static_multiclass_blend_overlays(app, cache, vertex_classes,
                                            visible_classes, mixed_faces):
    """Overlay only mixed-colour faces with barycentric class RGB.

    The underlying complete TIN stays one crisp cell-shaded surface.  Mixed
    class boundaries are rendered in bounded chunks with point RGB plus explicit
    CELL normals, so class colours blend inside each triangle while each facet
    remains geometrically flat and sharply separated from its neighbours.
    """
    _remove_static_multiclass_blend_overlays(app)
    plotter = getattr(app, 'vtk_widget', None)
    if plotter is None:
        return 0, 0
    ids = np.asarray(mixed_faces, dtype=np.int64).ravel()
    if ids.size == 0:
        app._shading_static_blend_overlays = []
        return 0, 0

    classes = np.asarray(vertex_classes)
    vc = set(int(c) for c in (visible_classes or ()))
    chunk_faces = max(25_000, int(os.environ.get(
        'NAKSHA_SHADING_CRISP_OVERLAY_CHUNK', '250000'
    )))
    entries = []
    total_vertices = 0
    blend_actor_count = 0
    sharp_tip_faces = 0
    sharp_tip_actors = 0
    for chunk_no, begin in enumerate(range(0, len(ids), chunk_faces)):
        chunk_ids = ids[begin:begin + chunk_faces]
        base_faces = np.asarray(cache.faces[chunk_ids], dtype=np.int32)
        flat = base_faces.reshape(-1)
        local_ids, inverse = np.unique(flat, return_inverse=True)
        local_faces = inverse.reshape(-1, 3).astype(np.int32, copy=False)
        local_xyz = np.ascontiguousarray(cache.xyz_final[local_ids])
        rgb = _shading_palette_rgb(app, classes[local_ids], vc)

        packed = np.empty(len(local_faces) * 4, dtype=np.int32)
        packed[0::4] = 3
        packed[1::4] = local_faces[:, 0]
        packed[2::4] = local_faces[:, 1]
        packed[3::4] = local_faces[:, 2]
        patch = pv.PolyData(local_xyz, packed)
        patch.point_data['RGB'] = np.ascontiguousarray(rgb, dtype=np.uint8)

        normal_buf = None
        if cache.face_normals is not None and len(cache.face_normals) == len(cache.faces):
            normal_buf = _attach_explicit_cell_normals(
                patch, cache.face_normals[chunk_ids]
            )

        name = f'{_MULTICLASS_STATIC_BLEND_PREFIX}{chunk_no}'
        actor = plotter.add_mesh(
            patch,
            scalars='RGB',
            rgb=True,
            show_edges=False,
            lighting=True,
            smooth_shading=False,
            preference='point',
            name=name,
            render=False,
        )
        if actor is None:
            continue
        try:
            setattr(actor, '_is_shading_mesh', True)
            setattr(actor, '_is_shading_static_blend', True)
            actor.PickableOff()
            _configure_microstation_color_blend_lighting(app, actor)
            mapper = actor.GetMapper()
            if mapper is not None:
                mapper.StaticOn()
                mapper.SetResolveCoincidentTopologyToPolygonOffset()
                mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(-3.0, -3.0)
                mapper.InterpolateScalarsBeforeMappingOff()
        except Exception:
            pass
        # Keep every zero-copy/VTK-backed NumPy buffer alive with the actor.
        buffers = (local_xyz, packed, rgb, normal_buf, local_ids, local_faces, chunk_ids)
        actor._naksha_crisp_blend_buffers = buffers
        entries.append({'name': name, 'actor': actor, 'buffers': buffers})
        blend_actor_count += 1
        total_vertices += int(len(local_ids))

        # Sharpen only the actual isolated class apex.  The surrounding mixed
        # face still uses the established barycentric blend.
        local_classes = classes[local_ids]
        local_normals = (
            cache.face_normals[chunk_ids]
            if cache.face_normals is not None and len(cache.face_normals) == len(cache.faces)
            else None
        )
        tip_name = f'{_MULTICLASS_STATIC_BLEND_PREFIX}tip_{chunk_no}'
        tip_actor, tip_buffers, tip_count = _build_sharp_multiclass_tip_actor(
            app, cache, name=tip_name, xyz=local_xyz, faces=local_faces,
            vertex_classes=local_classes, visible_classes=vc,
            face_normals=local_normals, live=False,
        )
        if tip_actor is not None:
            entries.append({'name': tip_name, 'actor': tip_actor, 'buffers': tip_buffers})
            sharp_tip_actors += 1
            sharp_tip_faces += int(tip_count)

    app._shading_static_blend_overlays = entries
    if sharp_tip_faces:
        fraction, retention = _sharp_tip_parameters(app)
        print(
            'SHADING_SHARP_TIPS status=enabled kind=static '
            f'faces={sharp_tip_faces:,} actors={sharp_tip_actors} '
            f'fraction={fraction:.3f} color_retention={retention:.3f} '
            'topology_unchanged=1 base_blend_preserved=1'
        )
    return blend_actor_count, total_vertices


def _split_crisp_dirty_faces_by_display_color(app, cache, classes_raw,
                                               visible_classes, dirty_faces):
    """Split a SMALL dirty-face set into solid/pure and mixed-colour faces.

    This is the classification/undo/redo equivalent of the full-render crisp
    hybrid policy.  It intentionally examines only ``dirty_faces``; it never
    scans/rebuilds the complete 69M-face mesh and never materialises the full
    representative-class array.
    """
    ids = np.asarray(dirty_faces, dtype=np.int64).ravel()
    if ids.size == 0:
        return ids, ids
    ids = ids[(ids >= 0) & (ids < len(cache.faces))]
    if ids.size == 0:
        return ids, ids

    faces = np.asarray(cache.faces[ids], dtype=np.int32)
    classes = np.asarray(classes_raw)
    global_vertices = cache.unique_indices[faces]
    fvc = np.asarray(classes[global_vertices], dtype=np.int64)
    key_lut = _display_color_key_lut(app, fvc, visible_classes)
    k0 = key_lut[np.clip(fvc[:, 0], 0, len(key_lut) - 1)]
    k1 = key_lut[np.clip(fvc[:, 1], 0, len(key_lut) - 1)]
    k2 = key_lut[np.clip(fvc[:, 2], 0, len(key_lut) - 1)]
    mixed = (k0 != k1) | (k0 != k2)
    return ids[~mixed], ids[mixed]


def _build_crisp_live_pure_overlay(app, cache, classes_raw, visible_classes,
                                    face_ids):
    """Draw current PURE dirty faces with the exact crisp base presentation.

    Classification used to cover *every* dirty face with the point-RGB/GPU
    blend actor.  That is correct for mixed-class triangles, but wrong for
    same-colour ground triangles because the initial crisp renderer uses baked
    CELL RGB + Sharpness-aware face shade.  The mismatch made classified and
    undone regions look artificially smooth.

    This tiny actor restores the same solid-cell-facet rule only over touched
    pure faces.  It sits above both the immutable base and any stale static
    mixed overlay, so undo/redo can return a face to its exact current class
    presentation without touching global topology or 69M base RGB cells.
    """
    plotter = getattr(app, 'vtk_widget', None)
    ids = np.asarray(face_ids, dtype=np.int64).ravel()
    if plotter is None or ids.size == 0:
        return None, 0, 0

    faces = np.asarray(cache.faces[ids], dtype=np.int32)
    flat = faces.reshape(-1)
    local_ids, inverse = np.unique(flat, return_inverse=True)
    local_faces = inverse.reshape(-1, 3).astype(np.int32, copy=False)
    local_xyz = np.ascontiguousarray(cache.xyz_final[local_ids])

    packed = np.empty(len(local_faces) * 4, dtype=np.int32)
    packed[0::4] = 3
    packed[1::4] = local_faces[:, 0]
    packed[2::4] = local_faces[:, 1]
    packed[3::4] = local_faces[:, 2]
    patch = pv.PolyData(local_xyz, packed)

    classes = np.asarray(classes_raw)
    # Pure DISPLAY-colour faces have the same displayed RGB at all 3 vertices,
    # so vertex 0 is an exact source for the cell colour.
    face_classes = classes[cache.unique_indices[faces[:, 0]]]
    base_rgb = _shading_palette_rgb(app, face_classes, visible_classes).astype(
        np.float32, copy=False
    )
    shade = _crisp_shade_chunk(app, cache, ids)
    rgb = np.clip(base_rgb * shade[:, None], 0, 255).astype(np.uint8)
    patch.cell_data['RGB'] = np.ascontiguousarray(rgb, dtype=np.uint8)

    actor = plotter.add_mesh(
        patch,
        scalars='RGB',
        rgb=True,
        show_edges=False,
        lighting=False,
        smooth_shading=False,
        preference='cell',
        name=_MULTICLASS_CRISP_PURE_OVERLAY_NAME,
        render=False,
    )
    if actor is None:
        return None, 0, 0
    try:
        setattr(actor, '_is_shading_mesh', True)
        setattr(actor, '_is_shading_color_overlay', True)
        setattr(actor, '_is_shading_crisp_pure_overlay', True)
        actor.PickableOff()
        prop = actor.GetProperty()
        prop.SetLighting(False)
        prop.SetInterpolationToFlat()
        prop.SetAmbient(1.0)
        prop.SetDiffuse(0.0)
        prop.SetSpecular(0.0)
        prop.EdgeVisibilityOff()
        mapper = actor.GetMapper()
        if mapper is not None:
            mapper.StaticOn()
            mapper.SetResolveCoincidentTopologyToPolygonOffset()
            # Static mixed overlays use -3 and the live mixed layer uses -8.
            # Keep the current pure classification mask above both.
            mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(-10.0, -10.0)
            mapper.InterpolateScalarsBeforeMappingOff()
    except Exception:
        pass

    buffers = (local_xyz, packed, rgb, local_ids, local_faces, ids)
    actor._naksha_crisp_live_pure_buffers = buffers
    app._shading_multiclass_crisp_pure_overlay_actor = actor
    return actor, int(len(ids)), int(len(local_ids))


def _build_crisp_live_mixed_overlay(app, cache, classes_raw, visible_classes,
                                     face_ids):
    """Draw current MIXED dirty faces exactly like the static blend layer."""
    plotter = getattr(app, 'vtk_widget', None)
    ids = np.asarray(face_ids, dtype=np.int64).ravel()
    if plotter is None or ids.size == 0:
        return None, 0, 0

    base_faces = np.asarray(cache.faces[ids], dtype=np.int32)
    flat = base_faces.reshape(-1)
    local_ids, inverse = np.unique(flat, return_inverse=True)
    local_faces = inverse.reshape(-1, 3).astype(np.int32, copy=False)
    local_xyz = np.ascontiguousarray(cache.xyz_final[local_ids])

    packed = np.empty(len(local_faces) * 4, dtype=np.int32)
    packed[0::4] = 3
    packed[1::4] = local_faces[:, 0]
    packed[2::4] = local_faces[:, 1]
    packed[3::4] = local_faces[:, 2]
    patch = pv.PolyData(local_xyz, packed)

    classes = np.asarray(classes_raw)
    local_classes = classes[cache.unique_indices[local_ids]]
    rgb = _shading_palette_rgb(app, local_classes, visible_classes)
    patch.point_data['RGB'] = np.ascontiguousarray(rgb, dtype=np.uint8)

    normal_buf = None
    local_normals = None
    if cache.face_normals is not None and len(cache.face_normals) == len(cache.faces):
        local_normals = cache.face_normals[ids]
        normal_buf = _attach_explicit_cell_normals(patch, local_normals)

    actor = plotter.add_mesh(
        patch,
        scalars='RGB',
        rgb=True,
        show_edges=False,
        lighting=True,
        smooth_shading=False,
        preference='point',
        name=_MULTICLASS_COLOR_OVERLAY_NAME,
        render=False,
    )
    if actor is None:
        return None, 0, 0
    try:
        setattr(actor, '_is_shading_mesh', True)
        setattr(actor, '_is_shading_color_overlay', True)
        actor.PickableOff()
        _configure_microstation_color_blend_lighting(app, actor)
        mapper = actor.GetMapper()
        if mapper is not None:
            mapper.StaticOn()
            mapper.SetResolveCoincidentTopologyToPolygonOffset()
            mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(-8.0, -8.0)
            mapper.InterpolateScalarsBeforeMappingOff()
    except Exception:
        pass

    buffers = (local_xyz, packed, rgb, normal_buf, local_ids, local_faces, ids)
    actor._naksha_crisp_live_mixed_buffers = buffers
    app._shading_multiclass_color_overlay_actor = actor

    # Keep the optional point-tip refinement behavior scoped to current mixed
    # faces only.  It is disabled by default, so this has no normal overhead.
    tip_actor, tip_buffers, tip_count = _build_sharp_multiclass_tip_actor(
        app, cache, name=_MULTICLASS_LIVE_TIP_NAME,
        xyz=local_xyz, faces=local_faces, vertex_classes=local_classes,
        visible_classes=visible_classes, face_normals=local_normals, live=True,
    )
    app._shading_multiclass_tip_actor = tip_actor
    if tip_actor is not None:
        try:
            tip_actor._naksha_sharp_tip_buffers = tip_buffers
        except Exception:
            pass
        if tip_count:
            print(
                'SHADING_SHARP_TIPS status=enabled kind=live '
                f'faces={tip_count} dirty_faces={len(ids)}'
            )

    return actor, int(len(ids)), int(len(local_ids))


def _rebuild_crisp_live_edit_overlays(app, cache, classes_raw,
                                       visible_classes, dirty_faces):
    """Rebuild only the tiny dirty presentation mask using CURRENT settings.

    No representatives, Delaunay, face topology, base-mesh RGB, or full actor is
    rebuilt.  The current app/cache Azimuth, Sharpness and Ambient are consumed
    by ``_crisp_shade_chunk`` and the mixed-overlay GPU light configuration.
    Therefore classification, undo and redo cannot switch the edited region to
    a smoother/different shading model.
    """
    ids = np.asarray(dirty_faces, dtype=np.int64).ravel()
    _remove_multiclass_color_overlay(app, cache=cache, clear_dirty=False)
    if ids.size == 0:
        return 0, 0, 0

    pure_ids, mixed_ids = _split_crisp_dirty_faces_by_display_color(
        app, cache, classes_raw, visible_classes, ids
    )
    _, pure_count, pure_vertices = _build_crisp_live_pure_overlay(
        app, cache, classes_raw, visible_classes, pure_ids
    )
    _, mixed_count, mixed_vertices = _build_crisp_live_mixed_overlay(
        app, cache, classes_raw, visible_classes, mixed_ids
    )

    azimuth = float(getattr(app, 'last_shade_azimuth', 45.0))
    sharpness = _shading_sharpness_angle(app)
    ambient = float(getattr(app, 'shade_ambient', 0.25))
    print(
        'SHADING_CRISP_EDIT_OVERLAY status=updated '
        f'dirty_faces={len(ids)} pure_faces={pure_count} mixed_faces={mixed_count} '
        f'local_vertices={pure_vertices + mixed_vertices} '
        f'azimuth={azimuth:.2f} sharpness={sharpness:.2f} ambient={ambient:.3f} '
        'pure=cell_shaded mixed=barycentric_flat '
        'topology_rebuild=0 geometry_rebuild=0 base_rgb_untouched=True'
    )
    return pure_count, mixed_count, pure_vertices + mixed_vertices

def _configure_microstation_color_blend_lighting(app, actor=None):
    """Flat faceted lighting with Sharpness independent of brightness.

    Multi-class Shaded Classification controls now mean:
      Azimuth   -> direction around the terrain.
      Sharpness -> face/tip contrast, not sun elevation.
      Ambient   -> brightness / shadow floor.

    Values through 90 keep the legacy 45-degree elevation. Overdrive lowers the
    incidence angle for visible facet contrast. Geometry/topology/class RGB,
    Ambient control and barycentric colour blending are untouched.
    """
    plotter = getattr(app, 'vtk_widget', None)
    renderer = getattr(plotter, 'renderer', None) if plotter is not None else None
    if renderer is None:
        return False

    azimuth = float(getattr(app, 'last_shade_azimuth', 45.0))
    sharpness_angle = _shading_sharpness_angle(app)
    ambient = float(np.clip(getattr(app, 'shade_ambient', 0.25), 0.0, 1.0))
    sharpness, sharpness_overdrive = _shading_sharpness_response(sharpness_angle)

    # Ambient now owns brightness. Keep only a very small anti-black safety floor
    light_elevation = _shading_effective_light_elevation(
        app, sharpness_overdrive
    )
    # instead of the previous 0.42 clamp that made Ambient=0.25 ineffective.
    try:
        ambient_floor = float(os.environ.get(
            'NAKSHA_SHADING_BLEND_AMBIENT_FLOOR', '0.08'
        ))
    except Exception:
        ambient_floor = 0.08
    ambient_floor = float(np.clip(ambient_floor, 0.0, 0.30))
    effective_ambient = max(ambient, ambient_floor)

    # Keep directional energy roughly constant. Sharpness only redistributes it
    # between key and opposite fill, increasing face-to-face contrast without
    # acting like a brightness slider.
    if ('NAKSHA_SHADING_BLEND_KEY_INTENSITY' in os.environ or
            'NAKSHA_SHADING_BLEND_FILL_INTENSITY' in os.environ):
        try:
            key_intensity = float(os.environ.get(
                'NAKSHA_SHADING_BLEND_KEY_INTENSITY', '0.85'
            ))
        except Exception:
            key_intensity = 0.85
        try:
            fill_intensity = float(os.environ.get(
                'NAKSHA_SHADING_BLEND_FILL_INTENSITY', '0.18'
            ))
        except Exception:
            fill_intensity = 0.18
    else:
        try:
            total_directional = float(os.environ.get(
                'NAKSHA_SHADING_DIRECTIONAL_ENERGY', '1.05'
            ))
        except Exception:
            total_directional = 1.05
        total_directional = float(np.clip(total_directional, 0.25, 1.50))
        # 0..90 is unchanged: key_ratio runs 0.58 -> 0.95. Most overdrive
        # contrast is deliberately distributed across 90..200, with a smaller
        # tail through 999 instead of visually saturating immediately after 90.
        # Total directional energy stays constant, so Ambient remains the
        # brightness control while facet-to-facet contrast gets stronger.
        key_ratio = (
            0.58
            + 0.37 * sharpness
            + 0.049 * sharpness_overdrive
        )
        key_ratio = float(np.clip(key_ratio, 0.50, 0.999))
        key_intensity = total_directional * key_ratio
        fill_intensity = total_directional * (1.0 - key_ratio)

    key_intensity = float(np.clip(key_intensity, 0.0, 2.0))
    fill_intensity = float(np.clip(fill_intensity, 0.0, 1.0))

    key_light = getattr(app, '_shading_microstation_key_light', None)
    fill_light = getattr(app, '_shading_microstation_fill_light', None)
    lights_live = False
    if key_light is not None and fill_light is not None:
        try:
            lights = renderer.GetLights()
            lights_live = bool(
                lights and lights.IsItemPresent(key_light)
                and lights.IsItemPresent(fill_light)
            )
        except Exception:
            lights_live = False

    if not lights_live:
        try:
            renderer.RemoveAllLights()
        except Exception:
            pass
        key_light = vtk.vtkLight(); key_light.SetLightTypeToSceneLight()
        key_light.SetPositional(False); key_light.SetColor(1.0, 1.0, 1.0)
        renderer.AddLight(key_light); app._shading_microstation_key_light = key_light
        fill_light = vtk.vtkLight(); fill_light.SetLightTypeToSceneLight()
        fill_light.SetPositional(False); fill_light.SetColor(1.0, 1.0, 1.0)
        renderer.AddLight(fill_light); app._shading_microstation_fill_light = fill_light

    # The 0..90 legacy range keeps the configured elevation. Overdrive lowers
    # the incidence angle so 90..200 has a strong, visible facet response.
    zenith_rad = np.radians(90.0 - light_elevation)
    az_math_rad = np.radians(360.0 - azimuth + 90.0)
    lx = np.sin(zenith_rad) * np.cos(az_math_rad)
    ly = np.sin(zenith_rad) * np.sin(az_math_rad)
    lz = np.cos(zenith_rad)

    key_light.SetPosition(float(lx*100.0), float(ly*100.0), float(lz*100.0))
    key_light.SetFocalPoint(0.0, 0.0, 0.0); key_light.SetIntensity(key_intensity)
    fill_z = max(float(lz), 0.20)
    fill_light.SetPosition(float(-lx*100.0), float(-ly*100.0), fill_z*100.0)
    fill_light.SetFocalPoint(0.0, 0.0, 0.0); fill_light.SetIntensity(fill_intensity)

    for light in (key_light, fill_light):
        try: light.Modified()
        except Exception: pass
    try: renderer.SetAmbient(1.0, 1.0, 1.0)
    except Exception: pass

    actors = []
    if actor is not None:
        actors.append(actor)
    else:
        for candidate in (
            getattr(app, '_shaded_mesh_actor', None),
            getattr(app, '_shading_multiclass_color_overlay_actor', None),
        ):
            if candidate is not None: actors.append(candidate)

    for item in actors:
        try:
            prop = item.GetProperty()
            prop.SetLighting(True); prop.SetInterpolationToFlat()
            prop.SetAmbient(effective_ambient)
            prop.SetDiffuse(max(0.0, 1.0-effective_ambient))
            prop.SetSpecular(0.0); prop.EdgeVisibilityOff()
            try:
                prop.BackfaceCullingOff(); prop.FrontfaceCullingOff()
            except Exception:
                pass
            item.Modified()
        except Exception:
            pass

    signature = (
        round(azimuth,4), round(sharpness_angle,4),
        round(sharpness_overdrive,4), round(light_elevation,4),
        round(ambient,4), round(effective_ambient,4),
        round(key_intensity,4), round(fill_intensity,4),
    )
    if getattr(app, '_shading_microstation_light_signature', None) != signature:
        app._shading_microstation_light_signature = signature
        print(
            'SHADING_SHARPNESS_CONTROL '
            f'azimuth={azimuth:.2f} sharpness={sharpness_angle:.2f} '
            f'overdrive={sharpness_overdrive:.3f} '
            f'light_elevation={light_elevation:.2f} '
            f'ambient_brightness={ambient:.3f} effective_ambient={effective_ambient:.3f} '
            f'key={key_intensity:.3f} fill={fill_intensity:.3f} '
            'semantics=decoupled topology_rebuild=0 color_upload=0'
        )
    return True

def _shading_palette_rgb(app, class_values, visible_classes):
    """Map a small/sparse class array to exact Display Mode RGB values."""
    values = np.asarray(class_values).astype(np.int64, copy=False).ravel()
    if values.size == 0:
        return np.empty((0, 3), dtype=np.uint8)
    visible = set(int(c) for c in (visible_classes or ()))
    palette_codes = [int(c) for c in getattr(app, 'class_palette', {}).keys()]
    max_code = max([255, int(values.max(initial=0))] + palette_codes)
    lut = np.zeros((max_code + 1, 3), dtype=np.uint8)
    for code, entry in getattr(app, 'class_palette', {}).items():
        ci = int(code)
        if 0 <= ci <= max_code and ci in visible:
            try:
                lut[ci] = np.asarray(entry.get('color', (128, 128, 128))[:3], dtype=np.uint8)
            except Exception:
                lut[ci] = (128, 128, 128)
    clipped = np.clip(values, 0, max_code)
    return lut[clipped]


def _add_shading_class_detail_actor(app, name, xyz, rgb, *, live=False):
    """Create one GPU point actor above the faceted mesh without altering it."""
    plotter = getattr(app, 'vtk_widget', None)
    if plotter is None:
        return None
    xyz = np.ascontiguousarray(np.asarray(xyz, dtype=np.float64))
    rgb = np.ascontiguousarray(np.asarray(rgb, dtype=np.uint8))
    if xyz.ndim != 2 or xyz.shape[1] != 3 or len(xyz) == 0 or len(rgb) != len(xyz):
        return None

    try:
        plotter.remove_actor(name, render=False)
    except Exception:
        pass

    cloud = pv.PolyData(xyz)
    cloud.point_data['RGB'] = rgb
    point_size = float(os.environ.get('NAKSHA_SHADING_CLASS_DETAIL_SIZE', '3.0'))
    point_size = min(max(point_size, 1.0), 8.0)
    actor = plotter.add_mesh(
        cloud,
        scalars='RGB',
        rgb=True,
        style='points',
        point_size=point_size,
        render_points_as_spheres=False,
        lighting=False,
        preference='point',
        name=name,
        render=False,
    )
    if actor is None:
        return None
    try:
        setattr(actor, '_is_shading_mesh', True)
        setattr(actor, '_is_shading_class_detail', True)
        setattr(actor, '_is_shading_live_class_detail', bool(live))
        actor.PickableOff()
        if hasattr(actor, 'UseBoundsOff'):
            actor.UseBoundsOff()
        prop = actor.GetProperty()
        prop.SetLighting(False)
        prop.SetAmbient(1.0)
        prop.SetDiffuse(0.0)
        prop.SetSpecular(0.0)
        prop.SetPointSize(point_size)
        mapper = actor.GetMapper()
        if mapper is not None:
            mapper.StaticOn()
            # Prefer a depth bias instead of physically moving LiDAR XYZ. VTK
            # versions differ on point-offset support, so guard every call.
            mapper.SetResolveCoincidentTopologyToPolygonOffset()
            if hasattr(mapper, 'SetRelativeCoincidentTopologyPointOffsetParameter'):
                mapper.SetRelativeCoincidentTopologyPointOffsetParameter(-8.0 if live else -6.0)
            elif hasattr(mapper, 'SetResolveCoincidentTopologyPointOffsetParameter'):
                mapper.SetResolveCoincidentTopologyPointOffsetParameter(-8.0 if live else -6.0)
            mapper.InterpolateScalarsBeforeMappingOff()
    except Exception:
        pass

    buffers = (xyz, rgb, cloud)
    if live:
        app._shading_live_class_detail_actor = actor
        app._shading_live_class_detail_buffers = buffers
    else:
        app._shading_class_detail_actor = actor
        app._shading_class_detail_buffers = buffers
    return actor


def _build_faceted_class_detail_overlay(app, cache, classes_raw, face_classes=None, visible_classes=None):
    """Restore point-level class fidelity lost by majority-colored triangles.

    The existing triangulation and 68M-cell RGB mesh are untouched. For Slow
    multi-class shading only, mark representative vertices whose canonical class
    differs from the class painted on at least one incident triangle. Vertices
    not referenced by any surviving triangle are also retained as point detail.
    Work is chunked so a 30M+ point / 60M+ face tile does not materialize another
    full ``classes[faces]`` array.
    """
    if not _shading_class_detail_enabled(app, cache):
        _remove_shading_class_detail_overlays(app, remove_static=True, remove_live=False)
        return True
    if cache.faces is None or cache.unique_indices is None or cache.xyz_final is None:
        return False

    t0 = time.perf_counter()
    faces = np.asarray(cache.faces, dtype=np.int32)
    n_vertices = len(cache.unique_indices)
    if n_vertices == 0 or len(faces) == 0:
        return True
    classes = np.asarray(classes_raw)
    cm = classes[cache.unique_indices]
    detail = np.zeros(n_vertices, dtype=np.bool_)
    used = np.zeros(n_vertices, dtype=np.bool_)

    # face_classes is already present during the normal flat render. If a caller
    # does not have it, compute only one bounded chunk at a time.
    supplied_fc = (
        face_classes is not None and len(face_classes) == len(faces)
    )
    chunk = max(100_000, int(os.environ.get(
        'NAKSHA_SHADING_CLASS_DETAIL_FACE_CHUNK', '1500000'
    )))

    for begin in range(0, len(faces), chunk):
        end = min(begin + chunk, len(faces))
        f = faces[begin:end]
        c0 = cm[f[:, 0]]
        c1 = cm[f[:, 1]]
        c2 = cm[f[:, 2]]
        if supplied_fc:
            fc = np.asarray(face_classes[begin:end])
        else:
            fc = np.where(
                c0 == c1,
                c0,
                np.where(c0 == c2, c0, np.where(c1 == c2, c1, c0)),
            )
        used[f[:, 0]] = True
        used[f[:, 1]] = True
        used[f[:, 2]] = True
        m0 = c0 != fc
        m1 = c1 != fc
        m2 = c2 != fc
        if np.any(m0):
            detail[f[m0, 0]] = True
        if np.any(m1):
            detail[f[m1, 1]] = True
        if np.any(m2):
            detail[f[m2, 2]] = True

    orphaned = ~used
    if np.any(orphaned):
        detail |= orphaned

    detail_idx = np.flatnonzero(detail).astype(np.int64, copy=False)
    if detail_idx.size == 0:
        _remove_shading_class_detail_overlays(app, remove_static=True, remove_live=False)
        print(
            'SHADING_CLASS_DETAIL points=0 minority=0 orphaned=0 '
            f'elapsed={(time.perf_counter()-t0)*1000.0:.1f}ms'
        )
        return True

    vc = visible_classes if visible_classes is not None else _get_shading_visibility(app)
    rgb = _shading_palette_rgb(app, cm[detail_idx], vc)
    # Hidden/unknown palette entries map to black. They should not create black
    # speckles over the mesh; geometry visibility semantics remain unchanged.
    keep = np.any(rgb != 0, axis=1)
    detail_idx = detail_idx[keep]
    rgb = rgb[keep]
    if detail_idx.size == 0:
        _remove_shading_class_detail_overlays(app, remove_static=True, remove_live=False)
        return True

    actor = _add_shading_class_detail_actor(
        app, _SHADING_CLASS_DETAIL_NAME,
        cache.xyz_final[detail_idx], rgb, live=False,
    )
    if actor is None:
        return False

    print(
        'SHADING_CLASS_DETAIL status=enabled mode=slow '
        f'points={len(detail_idx):,} orphaned={int(np.count_nonzero(orphaned)):,} '
        f'vertices={n_vertices:,} faces={len(faces):,} '
        f'elapsed={(time.perf_counter()-t0)*1000.0:.1f}ms '
        'base_mesh_unchanged=True'
    )
    return True


def _update_live_class_detail_overlay(app, cache, dirty_faces, classes_raw, visible_classes=None):
    """Refresh only class-detail points belonging to locally edited faces."""
    if not _shading_class_detail_enabled(app, cache):
        _remove_shading_class_detail_overlays(app, remove_static=False, remove_live=True)
        return True
    dirty_faces = np.asarray(dirty_faces, dtype=np.int64).ravel()
    if dirty_faces.size == 0:
        return True
    faces = np.asarray(cache.faces[dirty_faces], dtype=np.int32)
    classes = np.asarray(classes_raw)
    fvc = classes[cache.unique_indices[faces]]
    c0, c1, c2 = fvc[:, 0], fvc[:, 1], fvc[:, 2]
    fc = np.where(
        c0 == c1,
        c0,
        np.where(c0 == c2, c0, np.where(c1 == c2, c1, c0)),
    )

    ids = []
    m0 = c0 != fc
    m1 = c1 != fc
    m2 = c2 != fc
    if np.any(m0): ids.append(faces[m0, 0])
    if np.any(m1): ids.append(faces[m1, 1])
    if np.any(m2): ids.append(faces[m2, 2])
    if not ids:
        _remove_shading_class_detail_overlays(app, remove_static=False, remove_live=True)
        return True

    unique_ids = np.unique(np.concatenate(ids)).astype(np.int64, copy=False)
    vc = visible_classes if visible_classes is not None else _get_shading_visibility(app)
    current_classes = classes[cache.unique_indices[unique_ids]]
    rgb = _shading_palette_rgb(app, current_classes, vc)
    keep = np.any(rgb != 0, axis=1)
    unique_ids = unique_ids[keep]
    rgb = rgb[keep]
    if unique_ids.size == 0:
        _remove_shading_class_detail_overlays(app, remove_static=False, remove_live=True)
        return True

    return _add_shading_class_detail_actor(
        app, _SHADING_LIVE_CLASS_DETAIL_NAME,
        cache.xyz_final[unique_ids], rgb, live=True,
    ) is not None


def _remove_multiclass_color_overlay(app, cache=None, clear_dirty=True):
    """Remove the tiny multi-class color overlay without touching base shading.

    A full shading render/recolor already bakes the current canonical
    classification into the base mesh, so any live color overlay is then
    redundant and must be discarded.
    """
    plotter = getattr(app, 'vtk_widget', None)
    if plotter is not None:
        try:
            plotter.remove_actor(_MULTICLASS_COLOR_OVERLAY_NAME, render=False)
        except Exception:
            pass
        try:
            plotter.remove_actor(_MULTICLASS_CRISP_PURE_OVERLAY_NAME, render=False)
        except Exception:
            pass
        try:
            plotter.remove_actor(_MULTICLASS_LIVE_TIP_NAME, render=False)
        except Exception:
            pass
    app._shading_multiclass_color_overlay_actor = None
    app._shading_multiclass_crisp_pure_overlay_actor = None
    app._shading_multiclass_tip_actor = None

    if clear_dirty:
        caches = []
        if cache is not None:
            caches = [cache]
        else:
            try:
                caches = list(_cache_store.values())
            except Exception:
                caches = []
        for item in caches:
            try:
                item._multiclass_dirty_faces.clear()
                item._multiclass_dirty_faces_faces_id = id(item.faces) if item.faces is not None else None
            except Exception:
                pass


def _remove_fast_shading_overlays(app):
    """Remove only temporary shading-owned actors; never touch class actors."""
    plotter = getattr(app, 'vtk_widget', None)
    if plotter is None:
        app._shading_add_overlays = []
        app._shading_remove_overlays = []
        app._shading_multiclass_color_overlay_actor = None
        app._shading_multiclass_crisp_pure_overlay_actor = None
        app._shading_static_blend_overlays = []
        try:
            for item in _cache_store.values():
                item._multiclass_dirty_faces.clear()
        except Exception:
            pass
        return 0
    names = {
        str(entry.get('name'))
        for entry in (getattr(app, '_shading_add_overlays', None) or [])
        if entry.get('name')
    }
    # Single-class visible -> hidden edits use tiny local eraser/fill actors
    # so the 50M+ cell base mesh stays immutable on the GPU during active
    # classification.  They are shading-owned and must be removed together
    # with the existing add overlays on mode switches/full rebuilds.
    for entry in (getattr(app, '_shading_remove_overlays', None) or []):
        if not isinstance(entry, dict):
            continue
        for key in ('erase_name', 'fill_name'):
            value = entry.get(key)
            if value:
                names.add(str(value))
    try:
        names.update(
            str(name) for name in list(plotter.actors.keys())
            if (
                str(name).startswith('shaded_mesh_live_add_')
                or str(name).startswith('shaded_mesh_live_remove_erase_')
                or str(name).startswith('shaded_mesh_live_remove_fill_')
            )
        )
    except Exception:
        pass
    removed = 0
    for name in names:
        try:
            plotter.remove_actor(name, render=False)
            removed += 1
        except Exception:
            pass
    app._shading_add_overlays = []
    app._shading_remove_overlays = []
    # Multi-class uses a separate exact-face color overlay.  It is shading-owned
    # too, but deliberately not mixed with the single-class add-overlay journal.
    try:
        if _MULTICLASS_COLOR_OVERLAY_NAME in plotter.actors:
            plotter.remove_actor(_MULTICLASS_COLOR_OVERLAY_NAME, render=False)
            removed += 1
    except Exception:
        pass
    app._shading_multiclass_color_overlay_actor = None
    try:
        if _MULTICLASS_CRISP_PURE_OVERLAY_NAME in plotter.actors:
            plotter.remove_actor(_MULTICLASS_CRISP_PURE_OVERLAY_NAME, render=False)
            removed += 1
    except Exception:
        pass
    app._shading_multiclass_crisp_pure_overlay_actor = None
    try:
        if _MULTICLASS_LIVE_TIP_NAME in plotter.actors:
            plotter.remove_actor(_MULTICLASS_LIVE_TIP_NAME, render=False)
            removed += 1
    except Exception:
        pass
    app._shading_multiclass_tip_actor = None
    removed += _remove_static_multiclass_blend_overlays(app)
    _remove_shading_class_detail_overlays(app, remove_static=True, remove_live=True)
    try:
        for item in _cache_store.values():
            item._multiclass_dirty_faces.clear()
            item._multiclass_dirty_faces_faces_id = id(item.faces) if item.faces is not None else None
    except Exception:
        pass
    if removed:
        print(f"SHADING_OVERLAY_CLEANUP actors={removed}")
    return removed


def detach_shading_before_non_shading_mode(app):
    """Detach shading-owned actors while preserving reusable geometry caches."""
    plotter = getattr(app, 'vtk_widget', None)
    if plotter is not None:
        for name in ('shaded_mesh', 'shaded_mesh_edges'):
            try:
                plotter.remove_actor(name, render=False)
            except Exception:
                pass
    _remove_fast_shading_overlays(app)
    _remove_shading_class_detail_overlays(app, remove_static=True, remove_live=True)
    app._shaded_mesh_actor = None
    app._shaded_mesh_polydata = None
    app._shaded_mesh_edge_actor = None
    app._shaded_mesh_edge_polydata = None
    _set_rendered_cache_key(app, cache_key=None)

def _setup_microstation_lighting(renderer, azimuth=45., angle=45.):
    renderer.RemoveAllLights()
    zenith_rad = np.radians(90.0 - angle)
    az_math_rad = np.radians(360.0 - azimuth + 90.0)
    kl = vtk.vtkLight(); kl.SetLightTypeToSceneLight()
    kl.SetPosition(np.sin(zenith_rad)*np.cos(az_math_rad)*100, np.sin(zenith_rad)*np.sin(az_math_rad)*100, np.cos(zenith_rad)*100)
    kl.SetFocalPoint(0,0,0); kl.SetIntensity(0.85); kl.SetColor(1.,1.,0.98); kl.SetPositional(False)
    renderer.AddLight(kl)
    fl = vtk.vtkLight(); fl.SetLightTypeToSceneLight()
    fl.SetPosition(-np.sin(zenith_rad)*np.cos(az_math_rad)*100, -np.sin(zenith_rad)*np.sin(az_math_rad)*100, np.cos(zenith_rad)*50)
    fl.SetFocalPoint(0,0,0); fl.SetIntensity(0.15); fl.SetColor(0.85,0.85,1.); fl.SetPositional(False)
    renderer.AddLight(fl); renderer.SetAmbient(0.20, 0.20, 0.20)


def _classification_fingerprint(classes_raw):
    """Return a fast full-content fingerprint for safe representative reuse."""
    arr = np.asarray(classes_raw)
    if not arr.flags.c_contiguous:
        arr = np.ascontiguousarray(arr)
    h = hashlib.blake2b(digest_size=16)
    h.update(memoryview(arr).cast("B"))
    h.update(str(arr.dtype).encode("ascii", "replace"))
    h.update(str(arr.shape).encode("ascii", "replace"))
    return h.hexdigest()


def _build_representative_key(cache_key, classification_revision, classification_fingerprint):
    return (cache_key, int(classification_revision or 0), classification_fingerprint)


def _get_representative_seed(rep_key):
    seed = _representative_store.get(rep_key)
    if seed is not None:
        _representative_store.move_to_end(rep_key)
    return seed


def _store_representative_seed(rep_key, result):
    if result.get("empty", False):
        return
    seed = {
        "offset": result["offset"],
        "unique_indices": result["unique_indices"],
        "xyz_unique": result["xyz_unique"],
        "spacing": result["spacing"],
        "lod_factor": result.get("lod_factor", 1.0),
        "base_grid_unique_count": result.get("base_grid_unique_count", 0),
        "smooth_all_classes": result.get("smooth_all_classes", False),
        "precision": result.get("precision", 0.0),
        "adaptive_meta": dict(result.get("adaptive_meta", {})),
        "adaptive_version": result.get("adaptive_version", _ADAPTIVE_SHADING_VERSION),
        "representative_boundary": result.get("representative_boundary"),
    }
    _representative_store[rep_key] = seed
    _representative_store.move_to_end(rep_key)
    while len(_representative_store) > _MAX_STORED_REPRESENTATIVE_CACHES:
        _representative_store.popitem(last=False)


def clear_shading_representative_cache(reason=""):
    if reason and _representative_store:
        print(f"   ??? Representative cache cleared: {reason}")
    _representative_store.clear()


class ShadingGeometryCache:
    def __init__(self):
        self.cache_key = None
        self._clear_internal()
    def _clear_internal(self):
        self.xyz_unique = None; self.xyz_final = None; self.faces = None
        self.face_normals = None; self.vertex_normals = None
        self.shade = None; self.vertex_shade = None; self.unique_indices = None
        self.offset = None; self.spacing = 0.0; self.max_edge_factor = 3.0
        self.last_azimuth = -1; self.last_angle = -1; self.last_ambient = -1
        # User-facing multi-class facet Sharpness is independent from the
        # internal physical light elevation stored in ``last_angle``.
        self.last_crisp_sharpness = -1.0
        self.visible_classes_hash = None; self.n_visible_classes = 0
        self.single_class_id = None; self.visible_classes_set = None
        self.smooth_all_classes = False
        self.data_hash = None; self._vtk_colors_ptr = None
        self._hidden_face_mask = None; self._global_to_unique = None
        self._cached_face_class = None; self._tri_lod_factor = 1.0
        # Reusable masks for local shading classification edits.
        self._changed_vertex_scratch = None
        self._affected_face_scratch = None
        # Sparse point->incident-face cache.  It is populated only for vertices
        # the user actually edits, so we avoid a 1GB+ global CSR adjacency for
        # 30M+ point meshes while making repeat edits/undo essentially O(k).
        self._incident_face_cache = {}
        self._incident_face_cache_faces_id = None
        # Faces whose displayed class color differs from the immutable base
        # mesh.  A tiny overlay is rebuilt from this sparse set only.
        self._multiclass_dirty_faces = set()
        self._multiclass_dirty_faces_faces_id = None
        self._xy_tree = None; self._xy_tree_size = 0
        self._xy_tree_xyz_id = None
        # Keep NumPy buffers alive when VTK uses zero-copy local updates.
        self._vtk_fast_buffers = None
        self._pending_fast_rgb = None
        self._vtk_mesh = None
        self._needs_compaction = False
    def clear(self, reason=""):
        if reason: print(f"   ??? Cache cleared: {reason}")
        self._vtk_colors_ptr = None
        self._clear_internal()
        import gc
        gc.collect()
    def get_visible_hash(self, vc): return hash(frozenset(vc))
    def is_valid(self, xyz, vc):
        if self.xyz_unique is None or self.faces is None: return False
        if self.get_visible_hash(vc) != self.visible_classes_hash: return False
        return _compute_xyz_hash(xyz) == self.data_hash
    def is_fully_current(self, xyz, vc, az, an, am, app):
        if not self.is_geometry_valid(xyz, vc): return False
        if self.needs_shading_update(az, an, am): return False
        if int(self.n_visible_classes or 0) > 1:
            current_sharpness = _shading_sharpness_angle(app)
            if abs(float(self.last_crisp_sharpness) - current_sharpness) > .001:
                return False
        return getattr(app, '_shaded_mesh_actor', None) is not None
    def is_geometry_valid(self, xyz, vc):
        if self.xyz_unique is None or self.faces is None or len(self.faces) == 0: return False
        if self.get_visible_hash(vc) != self.visible_classes_hash: return False
        return _compute_xyz_hash(xyz) == self.data_hash
    def is_cached_subset_of(self, nvc, xyz):
        if self.visible_classes_set is None or self.xyz_unique is None or self.faces is None or len(self.faces) == 0 or self.data_hash is None: return False
        if not self.visible_classes_set.issubset(nvc): return False
        return _compute_xyz_hash(xyz) == self.data_hash
    def needs_shading_update(self, az, an, am):
        return abs(self.last_azimuth-az) > .001 or abs(self.last_angle-an) > .001 or abs(self.last_ambient-am) > .001
    def get_gpu_color_pointer(self, app):
        if self._vtk_colors_ptr is not None: return self._vtk_colors_ptr
        m = getattr(app, '_shaded_mesh_polydata', None)
        if m is None: return None
        try:
            vc = m.GetPointData().GetScalars()
            if vc is None: vc = m.GetCellData().GetScalars()
            if vc: self._vtk_colors_ptr = numpy_support.vtk_to_numpy(vc); return self._vtk_colors_ptr
        except Exception:
            pass
        return None
    def build_global_to_unique(self, total):
        if self._global_to_unique is not None and len(self._global_to_unique) == total: return self._global_to_unique
        self._global_to_unique = np.full(total, -1, dtype=np.int32)
        if self.unique_indices is not None:
            self._global_to_unique[self.unique_indices] = np.arange(len(self.unique_indices))
        return self._global_to_unique

def _compact_cache_vertices(cache):
    if cache.faces is None or len(cache.faces) == 0:
        return
    if cache.xyz_unique is None or len(cache.xyz_unique) == 0:
        return
    active_indices = np.unique(cache.faces.ravel())
    n_total = len(cache.xyz_unique)
    n_active = len(active_indices)
    if n_active < n_total:
        remap = np.full(n_total, -1, dtype=np.int32)
        remap[active_indices] = np.arange(n_active, dtype=np.int32)
        cache.unique_indices = cache.unique_indices[active_indices]
        cache.xyz_unique = cache.xyz_unique[active_indices]
        cache.xyz_final = cache.xyz_final[active_indices]
        cache.faces = remap[cache.faces]
        if cache.vertex_normals is not None and len(cache.vertex_normals) == n_total:
            cache.vertex_normals = cache.vertex_normals[active_indices]
        if cache.vertex_shade is not None and len(cache.vertex_shade) == n_total:
            cache.vertex_shade = cache.vertex_shade[active_indices]
        cache._global_to_unique = None
        cache._vtk_colors_ptr = None
        cache._xy_tree = None; cache._xy_tree_size = 0
        cache._xy_tree_xyz_id = None
        cache._vtk_mesh = None
    cache._needs_compaction = False

def _compute_xyz_hash(xyz):
    try:
        return hash((len(xyz), float(xyz[0,0]), float(xyz[-1,2])))
    except Exception:
        return None

def _normalize_visible_classes(vc):
    return tuple(sorted(int(c) for c in vc)) if vc else tuple()

def _build_cache_key(xyz, visible_classes, single_class_max_edge=None, quality_mode="normal"):
    quality_mode = normalize_shading_quality(quality_mode)
    adaptive_enabled = os.environ.get("NAKSHA_SHADING_ADAPTIVE", "1").strip().lower() not in ("0", "false", "off", "no")
    if quality_mode == "fast":
        adaptive_target = _SHADING_FAST_TARGET
    elif quality_mode == "slow":
        adaptive_target = "all"
    else:
        adaptive_target = int(os.environ.get("NAKSHA_SHADING_ADAPTIVE_TARGET", str(_ADAPTIVE_DEFAULT_TARGET)))
    adaptive_ratio = round(float(os.environ.get("NAKSHA_SHADING_ADAPTIVE_FINE_RATIO", "0.55")), 4)
    feature_enabled = os.environ.get("NAKSHA_SHADING_FEATURE_AWARE", "1").strip().lower() not in ("0", "false", "off", "no")
    feature_key = (
        _FEATURE_AWARE_VERSION, int(feature_enabled),
        round(float(os.environ.get("NAKSHA_SHADING_FEATURE_EDGE_FACTOR", "3.0")), 4),
        round(float(os.environ.get("NAKSHA_SHADING_FEATURE_MIN_EDGE", "1.0")), 4),
        round(float(os.environ.get("NAKSHA_SHADING_FEATURE_Z_FACTOR", "0.75")), 4),
        round(float(os.environ.get("NAKSHA_SHADING_FEATURE_MIN_Z_JUMP", "0.35")), 4),
        round(float(os.environ.get("NAKSHA_SHADING_FEATURE_MIN_SLOPE", "0.75")), 4),
    )
    quality_key = (quality_mode, _ADAPTIVE_SHADING_VERSION, int(adaptive_enabled), adaptive_target, adaptive_ratio, feature_key)
    if len(visible_classes) == 1:
        edge_key = "auto" if single_class_max_edge is None else round(float(single_class_max_edge), 6)
        mode_key = ("single", edge_key, quality_key)
    else:
        mode_key = ("multi", quality_key)
    return (_compute_xyz_hash(xyz), _normalize_visible_classes(visible_classes), mode_key)

_cache_store = OrderedDict()
_active_cache_key = None

def _trim_cache_store():
    global _active_cache_key
    while len(_cache_store) > _MAX_STORED_SHADING_CACHES:
        key, cache = _cache_store.popitem(last=False)
        cache.clear(f"evicted (limit {_MAX_STORED_SHADING_CACHES})")
        if key == _active_cache_key:
            _active_cache_key = None

def get_cache(cache_key=None, activate=True):
    global _active_cache_key
    if cache_key is None:
        if _active_cache_key is None:
            return ShadingGeometryCache()
        cache = _cache_store.get(_active_cache_key)
        if cache is None:
            _active_cache_key = None
            return ShadingGeometryCache()
        return cache
    cache = _cache_store.get(cache_key)
    if cache is None:
        cache = ShadingGeometryCache()
        _cache_store[cache_key] = cache
    cache.cache_key = cache_key
    if activate:
        _cache_store.move_to_end(cache_key)
        _active_cache_key = cache_key
        _trim_cache_store()
    return cache

def _get_rendered_cache_key(app):
    return getattr(app, '_rendered_shading_cache_key', None)

def _set_rendered_cache_key(app, cache=None, cache_key=None):
    if cache_key is None and cache is not None:
        cache_key = getattr(cache, 'cache_key', None)
    setattr(app, '_rendered_shading_cache_key', cache_key)

def has_cached_geometry(xyz, visible_classes, single_class_max_edge=None, quality_mode="normal"):
    cache = _cache_store.get(_build_cache_key(xyz, visible_classes, single_class_max_edge, quality_mode))
    return bool(cache and cache.is_geometry_valid(xyz, visible_classes))

def clear_shading_cache(reason="", all_entries=True):
    global _active_cache_key, _active_app_ref
    _hard_reset_tokens = ("new file", "project_clear", "point_cloud_clear", "manual")
    if any(token in str(reason).lower() for token in _hard_reset_tokens):
        clear_shading_representative_cache(reason)
    if _active_app_ref is not None:
        app = _active_app_ref()
        if app is not None and not _is_widget_deleted(app):
            _remove_fast_shading_overlays(app)
            if getattr(app, '_shading_rebuild_timer', None) is not None:
                try:
                    app._shading_rebuild_timer.stop()
                    app._shading_rebuild_timer.deleteLater()
                except Exception:
                    pass
                app._shading_rebuild_timer = None
    if all_entries:
        for cache in _cache_store.values():
            cache.clear(reason)
        _cache_store.clear()
        _active_cache_key = None
        return
    if _active_cache_key is None:
        return
    cache = _cache_store.pop(_active_cache_key, None)
    if cache is not None:
        cache.clear(reason)
    _active_cache_key = None

def invalidate_cache_for_new_file(fp=""): clear_shading_cache("new file", all_entries=True)

def _get_shading_visibility(app):
    so = getattr(app, '_shading_visibility_override', None)
    if so is not None:
        print(f"   ?? Shading visibility from SHORTCUT OVERRIDE: {sorted(so)}"); return so
    d = getattr(app, 'display_mode_dialog', None) or getattr(app, 'display_dialog', None)
    if d:
        vp = getattr(d, 'view_palettes', None)
        if vp and 0 in vp:
            vc = {int(c) for c, e in vp[0].items() if e.get("show", True)}
            if vc: print(f"   ?? Shading visibility from Display Mode (Slot 0): {sorted(vc)}"); return vc
    vc = {int(c) for c, e in app.class_palette.items() if e.get("show", True)}
    if vc: print(f"   ?? Shading visibility from class_palette: {sorted(vc)}")
    return vc

def _save_camera(app):
    try:
        c = app.vtk_widget.renderer.GetActiveCamera()
        return {'pos': c.GetPosition(), 'fp': c.GetFocalPoint(), 'up': c.GetViewUp(),
                'parallel': c.GetParallelProjection(), 'scale': c.GetParallelScale()}
    except: return None

def _restore_camera(app, c):
    if c:
        try:
            cam = app.vtk_widget.renderer.GetActiveCamera()
            cam.SetPosition(c['pos']); cam.SetFocalPoint(c['fp']); cam.SetViewUp(c['up'])
            cam.SetParallelProjection(c['parallel']); cam.SetParallelScale(c['scale'])
        except Exception:
            pass
def _queue_deferred_rebuild(app, reason="", newly_visible_indices=None):
    global _rebuild_reason, _rebuild_changed_indices
    _rebuild_reason = reason; _rebuild_changed_indices = newly_visible_indices
    if getattr(app, '_shading_rebuild_timer', None) is not None:
        try:
            app._shading_rebuild_timer.stop()
            app._shading_rebuild_timer.deleteLater()
        except Exception:
            pass
        app._shading_rebuild_timer = None
    def do_rebuild():
        global _rebuild_changed_indices
        if _is_widget_deleted(app):
            return
        app._shading_rebuild_timer = None
        di = _rebuild_changed_indices; _rebuild_changed_indices = None
        if getattr(app, 'is_dragging', False) or (hasattr(app, 'interactor') and getattr(app.interactor, 'is_dragging', False)):
            _queue_deferred_rebuild(app, _rebuild_reason, di); return
        cache = get_cache(); vc = _get_shading_visibility(app)
        data = getattr(app, "data", None)
        if not isinstance(data, dict):
            return
        cls_raw = data.get("classification"); xyz = data.get("xyz")
        if cls_raw is None or xyz is None:
            return
        cls = cls_raw.astype(np.int32)
        cm = cls[cache.unique_indices]; va = np.array(sorted(vc), dtype=np.int32)
        nh = ~np.isin(cm, va)
        if np.any(nh):
            if not _incremental_visibility_patch(app, cache.unique_indices[nh], vc):
                clear_shading_cache("patch failed"); update_shaded_class(app, force_rebuild=True)
            return
        if di is not None and len(di) > 0:
            g2u = cache.build_global_to_unique(len(xyz))
            sv = np.isin(cls[di], va); vod = di[sv]
            mg = vod[g2u[vod] < 0] if len(vod) > 0 else np.array([], dtype=np.intp)
            if len(mg) > 0:
                if not _fast_incremental_add_points(app, mg):
                    cm2 = np.zeros(len(xyz), dtype=bool); cm2[mg] = True
                    if not _multi_class_region_undo_patch(app, cm2, vc):
                        clear_shading_cache("region failed"); update_shaded_class(app, force_rebuild=True)
    dl = 150 if (newly_visible_indices is not None and len(newly_visible_indices) > 0) else 1000
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(do_rebuild)
    app._shading_rebuild_timer = timer
    timer.start(dl)

def _queue_incremental_patch(app, sci):
    global _rebuild_reason
    _rebuild_reason = "single-class patch"
    if getattr(app, '_shading_rebuild_timer', None) is not None:
        try:
            app._shading_rebuild_timer.stop()
            app._shading_rebuild_timer.deleteLater()
        except Exception:
            pass
        app._shading_rebuild_timer = None
    def do_patch():
        if _is_widget_deleted(app):
            return
        app._shading_rebuild_timer = None
        if getattr(app, 'is_dragging', False) or (hasattr(app, 'interactor') and getattr(app.interactor, 'is_dragging', False)):
            _queue_incremental_patch(app, sci); return
        _rebuild_single_class(app, sci)
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(do_patch)
    app._shading_rebuild_timer = timer
    timer.start(1000)


def update_shaded_class(app, azimuth=45., angle=None, ambient=0.25,
                        max_edge_factor=3.0, force_rebuild=False,
                        single_class_max_edge=None, **kwargs):
    global _active_app_ref
    _active_app_ref = weakref.ref(app)
    data = getattr(app, "data", None)
    if not isinstance(data, dict):
        return
    xyz_raw = data.get("xyz"); classes_raw = data.get("classification")
    if xyz_raw is None or classes_raw is None: return

    # Flight-line selection is a global geometry filter.  Shading works on a
    # compacted input so unchecked source IDs cannot contribute vertices or
    # triangles. Force a rebuild because older cache keys predate this mask.
    from gui.flight_line_filter import flight_line_visibility_mask
    _line_mask = flight_line_visibility_mask(app, len(xyz_raw))
    if not np.any(_line_mask):
        try:
            app.vtk_widget.remove_actor("shaded_mesh", render=False)
            app._shaded_mesh_actor = None
            app._shaded_mesh_polydata = None
            app.vtk_widget.render()
        except Exception:
            pass
        print("?? All flight lines are off — shaded point cloud hidden")
        return
    if not np.all(_line_mask):
        xyz_raw = xyz_raw[_line_mask]
        classes_raw = classes_raw[_line_mask]
        force_rebuild = True

    # Surface -> Shading safety:
    # Shading can be called by shortcut, Display Mode, or cached geometry restore.
    # All paths must remove Surface mesh before any early-return/cache path.
    try:
        from gui.surface_mode import detach_surface_before_non_surface_mode
        detach_surface_before_non_surface_mode(app, requested_mode="shaded_class")
    except Exception as _surface_cleanup_err:
        print(f"  ?? Surface cleanup before shaded_class skipped: {_surface_cleanup_err}")

    azimuth = getattr(app, 'last_shade_azimuth', azimuth)
    ambient = getattr(app, 'shade_ambient', ambient)
    vc = _get_shading_visibility(app)

    # In multi-class shading the user-facing Angle is facet SHARPNESS.
    # 0..90 keeps the established 45-degree light elevation.  Above 90 the
    # existing overdrive progressively lowers the effective elevation to make
    # low/small-slope TIN facets respond strongly.  The crisp-base contrast
    # remap keeps the horizontal-ground brightness anchored, so Ambient remains
    # the brightness/shadow-floor control rather than Sharpness.
    requested_angle = angle
    if len(vc) > 1:
        sharpness_angle = _shading_sharpness_angle(app, requested_angle)
        app.shading_sharpness_angle = sharpness_angle
        _, sharpness_overdrive = _shading_sharpness_response(sharpness_angle)
        angle = _shading_effective_light_elevation(
            app, sharpness_overdrive
        )
        # ``last_shade_angle`` is the internal physical light elevation used by
        # local classification/undo refresh paths.  The popup value itself is
        # stored independently in ``shading_sharpness_angle``.
        app.last_shade_angle = angle
    else:
        angle = float(
            getattr(app, 'last_shade_angle', 45.0)
            if requested_angle is None else requested_angle
        )
        app.last_shade_angle = angle

    quality_mode = normalize_shading_quality(getattr(app, 'shading_quality', 'normal'))
    app.shading_quality = quality_mode
    requested_cache_key = _build_cache_key(
        xyz_raw, vc, single_class_max_edge, quality_mode
    ) if vc else None
    cache = get_cache(requested_cache_key) if vc else get_cache()
    rendered_cache_key = _get_rendered_cache_key(app)

    _mesh_actor = getattr(app, '_shaded_mesh_actor', None)
    _actor_still_live = (
        _mesh_actor is not None
        and getattr(app, 'vtk_widget', None) is not None
        and "shaded_mesh" in app.vtk_widget.actors
    )
    if rendered_cache_key == requested_cache_key and _actor_still_live and cache.is_fully_current(xyz_raw, vc, azimuth, angle, ambient, app):
        _hide_point_cloud_actors_for_shading(app)
        try:
            app.vtk_widget.render()
        except Exception:
            pass
        print(" ? Requested shading preset already current")
        return

    if not force_rebuild and cache.is_geometry_valid(xyz_raw, vc):
        if rendered_cache_key != requested_cache_key:
            print("   ?? Restoring requested shading preset from cache")
        _refresh_from_cache(app, cache, azimuth, angle, ambient); return
    if not force_rebuild and cache.is_cached_subset_of(vc, xyz_raw):
        try:
            ec = vc - cache.visible_classes_set
            ei = np.where(np.isin(classes_raw.astype(np.int32), list(ec)))[0]
            if len(ei) > 0 and _fast_incremental_add_points(app, ei):
                cache.visible_classes_hash = cache.get_visible_hash(vc)
                cache.visible_classes_set = vc.copy(); cache.n_visible_classes = len(vc)
                cache.single_class_id = list(vc)[0] if len(vc) == 1 else None
                app.last_shade_azimuth = azimuth; app.last_shade_angle = angle; app.shade_ambient = ambient
                return
        except Exception:
            pass
    for c in app.class_palette: app.class_palette[c]["show"] = (int(c) in vc)
    app._shading_visible_classes = vc.copy() if vc else set()
    if not vc:
        if hasattr(app, '_shaded_mesh_actor'): app.vtk_widget.remove_actor("shaded_mesh"); app._shaded_mesh_actor = None
        app._shaded_mesh_polydata = None
        _set_rendered_cache_key(app, cache_key=None)
        _remove_shaded_edge_overlay(app); app.vtk_widget.render(); return
    if cache.is_valid(xyz_raw, vc) and not force_rebuild:
        _refresh_from_cache(app, cache, azimuth, angle, ambient)
    else:
        _build_visible_geometry(app, xyz_raw, classes_raw, azimuth, angle, ambient, max_edge_factor, cache, vc, single_class_max_edge)


def _grid_unique_count_at_precision(xyz, precision):
    """Return the exact number of XY grid cells at ``precision``.

    This intentionally mirrors ``_grid_dedup_at_precision`` cell construction
    but does not sort by Z or build representative indices. It is used only
    when the first-pass representatives would be discarded by the existing
    adaptive LOD logic.
    """
    xy_grid = np.floor(xyz[:, :2] / precision).astype(np.int64)
    gx = xy_grid[:, 0] - xy_grid[:, 0].min()
    gy = xy_grid[:, 1] - xy_grid[:, 1].min()
    gy_span = int(gy.max()) + 1

    if gx.max() < 2**30 and gy_span < 2**30:
        grid_key = gx * gy_span + gy
        return int(np.unique(grid_key).size)

    # Extremely large coordinate spans cannot be packed safely into the same
    # scalar key. np.unique(axis=0) preserves the exact legacy cell count.
    return int(np.unique(xy_grid, axis=0).shape[0])


def _grid_unique_count_fast_at_precision(xyz, precision):
    """Return the exact XY-cell count using a dense Numba bitmap when safe.

    The result is identical to ``_grid_unique_count_at_precision``. The legacy
    NumPy unique implementation remains the fallback for sparse/extreme grids
    or accelerator failures. No representative points are selected here.
    """
    min_gx = int(np.floor(np.min(xyz[:, 0]) / precision))
    max_gx = int(np.floor(np.max(xyz[:, 0]) / precision))
    min_gy = int(np.floor(np.min(xyz[:, 1]) / precision))
    max_gy = int(np.floor(np.max(xyz[:, 1]) / precision))
    gx_span = max_gx - min_gx + 1
    gy_span = max_gy - min_gy + 1
    n_cells = gx_span * gy_span

    count_limit = int(os.environ.get(
        "NAKSHA_SHADING_DENSE_COUNT_MAX_CELLS", "50000000"
    ))
    # One uint8 per cell. The default limit bounds the temporary bitmap to
    # roughly 50 MB while covering the 17-32M-cell grids seen in production.
    if HAS_NUMBA and 0 < n_cells <= count_limit:
        try:
            t0 = time.perf_counter()
            count = int(_numba_dense_grid_unique_count(
                xyz, float(precision), min_gx, min_gy, gx_span, gy_span
            ))
            print(
                f"SHADING_DEDUP_COUNT strategy=numba_dense points={len(xyz)} "
                f"cells={n_cells} unique={count} "
                f"elapsed={(time.perf_counter()-t0)*1000:.1f}ms"
            )
            return count
        except Exception as exc:
            print(f"SHADING_DEDUP_COUNT fallback=numpy_unique reason={exc}")

    t0 = time.perf_counter()
    count = _grid_unique_count_at_precision(xyz, precision)
    print(
        f"SHADING_DEDUP_COUNT strategy=numpy_unique points={len(xyz)} "
        f"cells={n_cells} unique={count} "
        f"elapsed={(time.perf_counter()-t0)*1000:.1f}ms"
    )
    return count


def _grid_dedup_at_precision(xyz, precision):
    """Grid dedup at given precision. Returns local highest-Z indices.

    PATCH 6B uses an O(N) dense-cell Numba selector when the XY grid is
    compact enough. It preserves the same final cell count and highest-Z
    representative rule as the legacy global lexsort. The original path is
    retained as a fallback for sparse/extreme grids or any accelerator error.
    """
    xy_grid = np.floor(xyz[:, :2] / precision).astype(np.int64)
    min_gx = int(xy_grid[:, 0].min())
    min_gy = int(xy_grid[:, 1].min())
    gx = xy_grid[:, 0] - min_gx
    gy = xy_grid[:, 1] - min_gy
    gx_span = int(gx.max()) + 1
    gy_span = int(gy.max()) + 1

    # Dense terrain grids are normally only ~2-4M cells for the current
    # 1.5M representative target. Cap memory conservatively (~24 bytes/cell
    # including temporary/JIT arrays) and require reasonable occupancy.
    n_cells = gx_span * gy_span
    dense_limit = int(os.environ.get("NAKSHA_SHADING_DENSE_GRID_MAX_CELLS", "12000000"))
    occupancy_ok = n_cells <= max(len(xyz) * 2, 1)
    if HAS_NUMBA and 0 < n_cells <= dense_limit and occupancy_ok:
        try:
            t0 = time.perf_counter()
            result = _numba_dense_grid_highest_z(
                xyz, float(precision), min_gx, min_gy, gx_span, gy_span
            )
            print(
                f"SHADING_DEDUP_SELECTOR strategy=numba_dense points={len(xyz)} "
                f"cells={n_cells} selected={len(result)} "
                f"elapsed={(time.perf_counter()-t0)*1000:.1f}ms"
            )
            return result
        except Exception as exc:
            print(f"SHADING_DEDUP_SELECTOR fallback=legacy reason={exc}")

    if gx.max() < 2**30 and gy_span < 2**30:
        grid_key = gx * gy_span + gy
        sort_idx = np.lexsort((-xyz[:, 2], grid_key))
        sorted_keys = grid_key[sort_idx]
        unique_mask = np.empty(len(sorted_keys), dtype=bool)
        unique_mask[0] = True
        unique_mask[1:] = np.diff(sorted_keys) != 0
    else:
        sort_idx = np.lexsort((-xyz[:, 2], xy_grid[:, 1], xy_grid[:, 0]))
        xy_sorted = xy_grid[sort_idx]
        d = np.diff(xy_sorted, axis=0)
        unique_mask = np.concatenate([[True], (d[:, 0] != 0) | (d[:, 1] != 0)])
    print(
        f"SHADING_DEDUP_SELECTOR strategy=legacy_sort points={len(xyz)} "
        f"cells={n_cells} selected={int(np.count_nonzero(unique_mask))}"
    )
    return sort_idx[unique_mask]


def _build_visible_geometry(app, xyz_raw, classes_raw, azimuth, angle,
                            ambient, max_edge_factor, cache, visible_classes,
                            single_class_max_edge=None):
    nv = len(visible_classes)
    is_sc = (nv == 1)
    print(f"\n{'='*60}")
    quality_mode = normalize_shading_quality(getattr(app, "shading_quality", "normal"))
    print(f"?? {'SINGLE-CLASS' if is_sc else 'MULTI-CLASS'} SHADING (MicroStation mode, {quality_mode})")
    print(f"{'='*60}")
    t_total = time.time()

    # -- Ensure numba JIT is fully compiled before timing real work ----
    if HAS_NUMBA and not getattr(_build_visible_geometry, '_numba_warmed', False):
        t_w = time.time()
        _warmup_numba_jit()
        _build_visible_geometry._numba_warmed = True
        print(f"   ?? Numba JIT warmup: {(time.time()-t_w)*1000:.0f}ms")

    app.last_shade_azimuth = azimuth; app.last_shade_angle = angle
    app.shade_ambient = ambient; app.display_mode = "shaded_class"
    saved_camera = _save_camera(app)
    progress = _ShadingProgressDialog(app)
    progress.show(); _process_shading_progress_events()
    try:
        rep_lookup_start = time.perf_counter()
        classification_fingerprint = _classification_fingerprint(classes_raw)
        classification_revision = int(getattr(app, "classification_revision", 0) or 0)
        rep_key = _build_representative_key(
            cache.cache_key, classification_revision, classification_fingerprint
        )
        representative_seed = _get_representative_seed(rep_key)
        rep_lookup_elapsed = time.perf_counter() - rep_lookup_start
        print(
            "SHADING_REPRESENTATIVE_CACHE "
            f"status={'hit' if representative_seed is not None else 'miss'} "
            f"revision={classification_revision} "
            f"validation={rep_lookup_elapsed*1000.0:.1f}ms"
        )

        boundary_flags = None
        if representative_seed is None:
            boundary_flags = _extract_boundary_flags_for_shading(app, len(xyz_raw))

        worker = _ShadingComputationWorker(
            xyz_raw, classes_raw, visible_classes, azimuth, angle, ambient,
            max_edge_factor, single_class_max_edge, _compute_xyz_hash(xyz_raw),
            representative_seed=representative_seed,
            boundary_flags=boundary_flags,
            quality_mode=normalize_shading_quality(getattr(app, 'shading_quality', 'normal'))
        )
        
        loop = QEventLoop()
        result_container = {}

        def handle_finish(res):
            result_container["result"] = res
            loop.quit()

        def handle_error(err_msg):
            result_container["error"] = err_msg
            loop.quit()

        worker.finished_signal.connect(handle_finish)
        worker.error_signal.connect(handle_error)

        worker.start()
        loop.exec()
        worker.wait()

        if "error" in result_container:
            raise Exception(result_container["error"])

        res = result_container.get("result", {})
        if not res.get("empty", False) and representative_seed is None:
            _store_representative_seed(rep_key, res)
            print(
                "SHADING_REPRESENTATIVE_CACHE stored "
                f"points={len(res.get('xyz_unique', []))} "
                f"entries={len(_representative_store)}"
            )
        backend_profile = res.get("profile_timings", {})
        if backend_profile:
            _emit_shading_profile(
                "geometry_backend", backend_profile,
                quality=normalize_shading_quality(getattr(app, "shading_quality", "normal")),
                raw_points=len(xyz_raw),
                unique_points=len(res.get("xyz_unique", [])),
                faces=len(res.get("faces", [])),
                classes=len(visible_classes),
                triangulator="triangle" if HAS_TRIANGLE else "scipy",
                dedup_strategy=res.get("dedup_strategy", "unknown"),
                base_grid_unique=res.get("base_grid_unique_count", 0),
                representative_cache=int(bool(res.get("representative_cache_hit", False))),
                adaptive=int(bool(res.get("adaptive_meta", {}).get("enabled", False))),
                adaptive_extra=int(res.get("adaptive_meta", {}).get("extra", 0) or 0),
                adaptive_target=int(res.get("adaptive_meta", {}).get("target", 0) or 0),
                feature_aware=int(bool(res.get("feature_meta", {}).get("enabled", False))),
                feature_removed=int(res.get("feature_meta", {}).get("removed", 0) or 0),
            )
        if res.get("empty", False):
            if hasattr(app, '_shaded_mesh_actor') and app._shaded_mesh_actor:
                try: app.vtk_widget.remove_actor("shaded_mesh", render=False)
                except Exception: pass
                app._shaded_mesh_actor = None
            if hasattr(app, '_shaded_mesh_polydata'): app._shaded_mesh_polydata = None
            _set_rendered_cache_key(app, cache_key=None)
            _remove_shaded_edge_overlay(app)
            cache.n_visible_classes = nv; cache.visible_classes_set = visible_classes.copy()
            cache.single_class_id = list(visible_classes)[0] if is_sc else None
            cache.smooth_all_classes = False
            _restore_camera(app, saved_camera); app.vtk_widget.render()
            return

        cache.offset = res["offset"]
        cache.unique_indices = res["unique_indices"]
        cache.xyz_unique = res["xyz_unique"]
        cache.xyz_final = res["xyz_final"]
        cache.spacing = res["spacing"]
        cache.max_edge_factor = res["max_edge_factor"]
        cache.visible_classes_hash = res["visible_classes_hash"]
        cache.n_visible_classes = res["n_visible_classes"]
        cache.visible_classes_set = res["visible_classes_set"]
        cache.single_class_id = res["single_class_id"]
        cache.smooth_all_classes = res["smooth_all_classes"]
        cache.faces = res["faces"]
        cache.face_normals = res["face_normals"]
        cache.vertex_normals = res["vertex_normals"]
        cache.vertex_shade = res["vertex_shade"]
        cache.shade = res["shade"]
        cache.last_azimuth = res["last_azimuth"]
        cache.last_angle = res["last_angle"]
        cache.last_ambient = res["last_ambient"]
        cache.data_hash = res["data_hash"]
        cache._vtk_colors_ptr = None
        cache._global_to_unique = None
        cache._cached_face_class = None
        cache._tri_lod_factor = res["lod_factor"]

        # -- PHASE 5: Render --
        _render_mesh(app, cache, classes_raw, saved_camera)
        print(f"   ? COMPLETE: {time.time()-t_total:.1f}s")
        print(f"{'='*60}\n")
    except Exception as e:
        print(f"   ? Error: {e}"); import traceback; traceback.print_exc()
    finally:
        progress.finish()

def _refresh_from_cache(app, cache, azimuth, angle, ambient):
    app.last_shade_azimuth = azimuth; app.last_shade_angle = angle
    app.shade_ambient = ambient; app.display_mode = "shaded_class"
    sc = _save_camera(app)
    if cache.needs_shading_update(azimuth, angle, ambient):
        if cache.face_normals is not None and len(cache.face_normals) > 0:
            cache.shade = _compute_face_shade(cache.xyz_unique, cache.faces, azimuth, angle, ambient, face_normals=cache.face_normals)
            if getattr(cache, "smooth_all_classes", False):
                cache.vertex_normals = _compute_vertex_normals(
                    cache.xyz_unique, cache.faces, cache.face_normals,
                )
                cache.vertex_shade = _compute_shading(
                    cache.vertex_normals, azimuth, angle, ambient,
                    z_values=cache.xyz_unique[:, 2],
                )
        cache.last_azimuth = azimuth; cache.last_angle = angle; cache.last_ambient = ambient
        cache._vtk_colors_ptr = None
    _render_mesh(
        app, cache, app.data.get("classification"), sc,
        cached_restore=True,
    )

def _prioritize_overlay_actor(actor, renderer=None):
    """Keep CAD/grid overlays readable over the shaded triangulated surface."""
    if actor is None:
        return
    try:
        prop = actor.GetProperty()
        if prop:
            prop.SetLighting(False)
            prop.SetAmbient(1.0)
            prop.SetOpacity(0.999)
            if hasattr(prop, "SetRenderLinesAsTubes"):
                prop.SetRenderLinesAsTubes(True)
    except Exception:
        pass
    try:
        actor.SetAllocatedRenderTime(1000.0, renderer)
    except Exception:
        pass
    try:
        if hasattr(actor, "ForceTranslucentOn"):
            actor.ForceTranslucentOn()
    except Exception:
        pass
    try:
        mapper = actor.GetMapper()
        if mapper:
            mapper.SetResolveCoincidentTopologyToPolygonOffset()
            mapper.SetRelativeCoincidentTopologyLineOffsetParameters(-50000.0, -50000.0)
            mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(-50000.0, -50000.0)
    except Exception:
        pass

def _restore_overlay_actor(renderer, actor, visible=True):
    if actor is None:
        return False
    try:
        if renderer.HasViewProp(actor):
            renderer.RemoveActor(actor)
        renderer.AddActor(actor)
        actor.SetVisibility(bool(visible))
        _prioritize_overlay_actor(actor, renderer)
        return True
    except Exception:
        return False

def _attachment_overlay_actors(app):
    seen = set()
    for entry in getattr(app, "dxf_actors", []) + getattr(app, "snt_actors", []):
        for actor in entry.get("actors", []):
            marker = id(actor)
            if marker in seen:
                continue
            seen.add(marker)
            yield actor

def _overlay_labels_hidden(app) -> bool:
    """Global SNT label-visibility switch from the attachment dialog."""
    try:
        dlg = getattr(app, "snt_dialog", None)
        return bool(getattr(dlg, "_labels_hidden", False))
    except Exception:
        return False

def _is_overlay_label_actor(actor) -> bool:
    """Match the same actor-level label detection used by SNT attachment UI."""
    return bool(
        getattr(actor, "is_grid_label", False)
        or getattr(actor, "text_content", None) is not None
    )

def _hide_point_cloud_actors_for_shading(app):
    """Hide point/class actors so Shaded Classification owns the main view."""
    try:
        plotter = getattr(app, "vtk_widget", None)
        if plotter is None:
            return

        def _is_attachment_or_shading_actor(actor):
            # These are intentional overlays and must remain visible.  A generic
            # _naksha_preserve flag is NOT sufficient here because large point
            # actors may also carry it for normal display-mode bookkeeping.
            return bool(
                getattr(actor, "_is_dxf_actor", False)
                or getattr(actor, "_is_snt_actor", False)
                or getattr(actor, "_is_shading_mesh", False)
            )

        def _looks_like_large_point_cloud(actor):
            """Identify raw/LOD point renderers even when they are 'preserved'."""
            if actor is None or _is_attachment_or_shading_actor(actor):
                return False
            if actor is getattr(app, "_shaded_mesh_actor", None):
                return False
            try:
                mapper = actor.GetMapper()
                poly = mapper.GetInput() if mapper is not None else None
                if poly is None:
                    return False
                n_points = int(poly.GetNumberOfPoints())
                if n_points <= 1000:
                    return False
                n_cells = int(poly.GetNumberOfCells())
                n_polys = int(poly.GetNumberOfPolys()) if hasattr(poly, "GetNumberOfPolys") else 0
                n_strips = int(poly.GetNumberOfStrips()) if hasattr(poly, "GetNumberOfStrips") else 0
                n_lines = int(poly.GetNumberOfLines()) if hasattr(poly, "GetNumberOfLines") else 0
                n_verts = int(poly.GetNumberOfVerts()) if hasattr(poly, "GetNumberOfVerts") else 0

                # Raw clouds are vertex-only datasets.  This catches unified,
                # class, LOD and boundary point actors whose VTK cell counts are
                # not exactly n_points (a case the old detector could miss).
                geometry_is_points = (
                    n_polys == 0
                    and n_strips == 0
                    and n_lines == 0
                    and (n_verts > 0 or n_cells == 0 or n_cells == n_points)
                )
                if not geometry_is_points:
                    return False

                pd = poly.GetPointData() if hasattr(poly, "GetPointData") else None
                has_point_scalars = bool(pd and (pd.GetArray("RGB") or pd.GetScalars()))
                return bool(
                    getattr(actor, "_naksha_pyvista_points", False)
                    or has_point_scalars
                    or n_verts > 0
                )
            except Exception:
                return False

        def _is_large_point_cloud_actor(actor):
            # Preserve the established destructive-cleanup contract: actors
            # explicitly marked _naksha_preserve are not removed here.  They are
            # handled by the non-destructive visibility pass below instead.
            if getattr(actor, "_naksha_preserve", False):
                return False
            return _looks_like_large_point_cloud(actor)

        actors = getattr(plotter, "actors", {}) or {}
        names_to_remove = []
        for name, actor in list(actors.items()):
            ns = str(name).lower()
            if (
                ns.startswith("class_")
                or ns in (
                    "main_pc",
                    "main_pc_border",
                    "_naksha_unified_cloud",
                )
            ):
                try:
                    actor.SetVisibility(False)
                except Exception:
                    pass
            elif _is_large_point_cloud_actor(actor):
                if ns not in ("shaded_mesh", "shaded_mesh_edges"):
                    names_to_remove.append(name)

        for name in names_to_remove:
            try:
                plotter.remove_actor(name, render=False)
            except Exception:
                try:
                    actors[name].SetVisibility(False)
                except Exception:
                    pass

        unified_actor = getattr(app, "_unified_actor", None)
        if unified_actor is not None:
            try:
                unified_actor.SetVisibility(False)
            except Exception:
                pass

        for attr_name in ("main_pc_actor", "point_cloud_actor", "_main_point_actor"):
            actor = getattr(app, attr_name, None)
            if actor is not None:
                try:
                    actor.SetVisibility(False)
                except Exception:
                    pass

        # Some production point/LOD actors intentionally carry _naksha_preserve
        # so generic actor cleanup does not destroy them.  In shaded mode they
        # must still be invisible, otherwise their coincident vertices appear as
        # dark dotted/scan-line hatching over the triangulated surface.  Hide
        # them non-destructively so switching back to a point display can restore
        # the same actor without rebuilding it.
        suppressed_preserved = []
        for name, actor in list((getattr(plotter, "actors", {}) or {}).items()):
            try:
                if (
                    getattr(actor, "_naksha_preserve", False)
                    and _looks_like_large_point_cloud(actor)
                    and bool(actor.GetVisibility())
                ):
                    actor.SetVisibility(False)
                    suppressed_preserved.append(str(name))
            except Exception:
                pass
        if suppressed_preserved:
            preview = ",".join(suppressed_preserved[:4])
            suffix = "..." if len(suppressed_preserved) > 4 else ""
            print(
                "SHADING_POINT_NOISE_SUPPRESS "
                f"hidden={len(suppressed_preserved)} actors={preview}{suffix} "
                "mode=non_destructive"
            )

        renderer = getattr(plotter, "renderer", None)
        if renderer is not None:
            anonymous_to_remove = []
            try:
                actor_collection = renderer.GetActors()
                actor_collection.InitTraversal()
                known_ids = {id(actor) for actor in actors.values()}
                for _ in range(actor_collection.GetNumberOfItems()):
                    actor = actor_collection.GetNextActor()
                    if actor is None:
                        continue
                    if id(actor) in known_ids:
                        continue
                    if _is_large_point_cloud_actor(actor):
                        anonymous_to_remove.append(actor)
            except Exception:
                anonymous_to_remove = []

            for actor in anonymous_to_remove:
                try:
                    renderer.RemoveActor(actor)
                except Exception:
                    try:
                        actor.SetVisibility(False)
                    except Exception:
                        pass
    except Exception as e:
        print(f"   ?? Shading point actor hide skipped: {e}")

def _face_class_ids(vertex_classes, faces):
    """Legacy deterministic majority class per triangle."""
    c0 = vertex_classes[faces[:, 0]]
    c1 = vertex_classes[faces[:, 1]]
    c2 = vertex_classes[faces[:, 2]]
    return np.where(
        c0 == c1,
        c0,
        np.where(c0 == c2, c0, np.where(c1 == c2, c1, c0)),
    )


def _face_class_ids_shading(app, cache, vertex_classes, faces):
    """Choose the displayed class for a shaded triangle.

    Existing Fast/Normal/single-class behaviour is preserved exactly.  Only
    Slow multi-class shading uses peak-vertex ownership: the class attached to
    the highest-Z vertex owns the facet.  This keeps isolated/elevated class
    samples (for example Default points above Ground) represented as shaded
    triangles instead of being swallowed by a 2-vs-1 majority vote.
    """
    try:
        is_slow = normalize_shading_quality(
            getattr(app, 'shading_quality', 'normal')
        ) == 'slow'
        is_multi = int(getattr(cache, 'n_visible_classes', 0) or 0) > 1
        xyz = getattr(cache, 'xyz_unique', None)
        if is_slow and is_multi and xyz is not None and len(xyz) == len(vertex_classes):
            f = np.asarray(faces, dtype=np.int32)
            z0 = xyz[f[:, 0], 2]
            z1 = xyz[f[:, 1], 2]
            z2 = xyz[f[:, 2], 2]
            # Deterministic ties: v0, then v1, then v2.
            choose1 = z1 > z0
            best_z = np.where(choose1, z1, z0)
            best_i = np.where(choose1, f[:, 1], f[:, 0])
            choose2 = z2 > best_z
            best_i = np.where(choose2, f[:, 2], best_i)
            return np.asarray(vertex_classes)[best_i]
    except Exception as exc:
        print(f"SHADING_FACE_CLASS peak_fallback=majority reason={exc}")
    return _face_class_ids(vertex_classes, faces)


def _face_class_ids_sparse_shading(app, cache, face_vertex_classes, face_vertex_ids):
    """Return face classes for a sparse already-expanded Nx3 face set.

    Fast/Normal/single-class keep the established majority rule. Slow
    multi-class uses the highest-Z vertex, exactly matching the full renderer
    without materializing classifications for every 34M mesh vertex.
    """
    fvc = np.asarray(face_vertex_classes)
    fids = np.asarray(face_vertex_ids, dtype=np.int32)
    if fvc.ndim != 2 or fvc.shape[1] != 3 or fids.shape != fvc.shape:
        raise ValueError("face_vertex_classes/ids must both be Nx3")
    try:
        is_slow = normalize_shading_quality(
            getattr(app, 'shading_quality', 'normal')
        ) == 'slow'
        is_multi = int(getattr(cache, 'n_visible_classes', 0) or 0) > 1
        xyz = getattr(cache, 'xyz_unique', None)
        if is_slow and is_multi and xyz is not None:
            z0 = xyz[fids[:, 0], 2]
            z1 = xyz[fids[:, 1], 2]
            z2 = xyz[fids[:, 2], 2]
            choose1 = z1 > z0
            best_z = np.where(choose1, z1, z0)
            best_slot = np.where(choose1, 1, 0)
            choose2 = z2 > best_z
            best_slot = np.where(choose2, 2, best_slot)
            rows = np.arange(len(fvc), dtype=np.int64)
            return fvc[rows, best_slot].astype(np.int64, copy=False)
    except Exception as exc:
        print(f"SHADING_FACE_CLASS sparse_peak_fallback=majority reason={exc}")
    c0, c1, c2 = fvc[:, 0], fvc[:, 1], fvc[:, 2]
    return np.where(
        c0 == c1,
        c0,
        np.where(c0 == c2, c0, np.where(c1 == c2, c1, c0)),
    ).astype(np.int64, copy=False)


def _render_mesh(app, cache, classes_raw, saved_camera, cached_restore=False):
    if cache.faces is None or len(cache.faces) == 0: return
    # A full render/recolor bakes canonical classes into the base mesh.  Drop
    # any temporary multi-class color overlay before touching that base actor.
    _remove_multiclass_color_overlay(app, cache=cache, clear_dirty=True)
    _remove_static_multiclass_blend_overlays(app)
    profile_start = time.perf_counter()
    stage_start = profile_start
    render_timings = OrderedDict()

    def checkpoint(name):
        nonlocal stage_start
        now = time.perf_counter()
        render_timings[name] = now - stage_start
        stage_start = now

    if getattr(cache, '_needs_compaction', False):
        _compact_cache_vertices(cache)
    checkpoint("compaction")
    t0 = time.time()
    labels_hidden = _overlay_labels_hidden(app)
    _hide_point_cloud_actors_for_shading(app)
    classes = classes_raw.astype(np.int32); cm = classes[cache.unique_indices]
    vc = _get_shading_visibility(app); nv = len(cache.xyz_final); nf = len(cache.faces)
    az = getattr(app, 'last_shade_azimuth', 45.); an = getattr(app, 'last_shade_angle', 45.)
    mc = max(int(cm.max())+1, 256)
    lut = np.zeros((mc, 3), dtype=np.float32)
    for c, e in app.class_palette.items():
        ci = int(c)
        if ci < mc and ci in vc: lut[ci] = e.get("color", (128,128,128))
    amb = getattr(app, 'shade_ambient', 0.25)
    # Every multi-class quality uses the same MicroStation/TerraScan presentation:
    # class RGB lives on each representative vertex and is barycentrically blended
    # by the GPU while the triangle normal remains flat/faceted.  Fast / Normal /
    # Slow still differ ONLY in geometry density; single-class behaviour is intact.
    blend_class_colors = _microstation_color_blend_enabled(app, cache)
    crisp_hybrid = bool(blend_class_colors and _crisp_multiclass_facets_enabled(app, cache))
    mixed_faces = np.empty(0, dtype=np.int64)
    mixed_count = 0
    mixed_budget = _crisp_mixed_face_budget(nf)
    if crisp_hybrid:
        mixed_faces, mixed_count, crisp_ok = _collect_mixed_display_faces(
            app, cache, cm, vc
        )
        if not crisp_ok:
            crisp_hybrid = False
            mixed_faces = np.empty(0, dtype=np.int64)
            print(
                'SHADING_CRISP_FACETS status=fallback reason=mixed_face_safety_guard '
                f'candidate_mixed_faces={mixed_count:,} '
                f'mixed_face_budget={mixed_budget:,} '
                'fallback=full_vertex_blend'
            )
    app._shading_crisp_hybrid_active = bool(crisp_hybrid)
    # Enforce the current faceted-normal policy for newly built and cached meshes.
    # In blend mode the NORMAL is still flat; only class RGB is interpolated.
    smooth_all_classes = False
    cache.smooth_all_classes = False
    em = getattr(app, '_shaded_mesh_polydata', None)
    ea = getattr(app, '_shaded_mesh_actor', None)
    cached_mesh = getattr(cache, '_vtk_mesh', None)
    if (cached_mesh is not None and
            (cached_mesh.GetNumberOfPoints() != nv or cached_mesh.GetNumberOfCells() != nf)):
        cached_mesh = None
        cache._vtk_mesh = None
    checkpoint("class_map_lut_setup")

    if crisp_hybrid:
        # Crisp MicroStation-style presentation:
        #   1) every TIN face is a solid cell-shaded triangle (no sub-pixel
        #      point-colour lighting chatter on the broad/low ground surface),
        #   2) only mixed DISPLAY-colour faces receive the barycentric blend
        #      overlay built below.
        face_colors = _build_crisp_base_face_colors(app, cache, cm, vc)
        cache.last_crisp_sharpness = _shading_sharpness_angle(app)

        if (em is not None and ea is not None and
                _get_rendered_cache_key(app) == cache.cache_key and
                em.GetNumberOfPoints() == nv and em.GetNumberOfCells() == nf):
            try:
                vtk_colors = em.GetCellData().GetScalars()
                if vtk_colors is not None and vtk_colors.GetNumberOfTuples() == nf:
                    numpy_support.vtk_to_numpy(vtk_colors)[:] = face_colors
                    vtk_colors.Modified(); em.GetCellData().Modified(); em.Modified()
                    ea.GetMapper().Modified()
                    app._shaded_mesh_actor = ea
                    _enforce_crisp_base_actor_state(app)
                    actor_count, blend_vertices = _build_static_multiclass_blend_overlays(
                        app, cache, cm, vc, mixed_faces
                    )
                    _set_rendered_cache_key(app, cache)
                    for actor in _attachment_overlay_actors(app):
                        vis = bool(actor.GetVisibility())
                        if labels_hidden and _is_overlay_label_actor(actor):
                            vis = False
                        _restore_overlay_actor(app.vtk_widget.renderer, actor, vis)
                    _hide_point_cloud_actors_for_shading(app)
                    checkpoint('inplace_crisp_color_update_and_overlays')
                    _restore_camera(app, saved_camera); app.vtk_widget.render()
                    checkpoint('render_present')
                    render_timings['total_render_path'] = time.perf_counter() - profile_start
                    _emit_shading_profile(
                        'render_inplace_crisp_blend', render_timings,
                        points=nv, faces=nf, smooth=0, blend=1,
                        mixed_faces=mixed_count, blend_actors=actor_count,
                    )
                    print(
                        'SHADING_CRISP_FACETS status=enabled '
                        f'faces={nf:,} mixed_faces={mixed_count:,} '
                        f'mixed_face_budget={mixed_budget:,} '
                        f'pure_faces={nf-mixed_count:,} blend_actors={actor_count} '
                        f'blend_vertices={blend_vertices:,} all_points_preserved=1 '
                        'topology_unchanged=1 base=cell_shaded overlay=vertex_blend'
                    )
                    return
            except Exception as exc:
                print(f'SHADING_CRISP_FACETS inplace_fallback={exc}')
            cache._vtk_colors_ptr = None

        if cached_mesh is not None:
            mesh = cached_mesh
            try:
                mesh.GetPointData().RemoveArray('RGB')
            except Exception:
                pass
            mesh.cell_data['RGB'] = face_colors
        else:
            fv = np.empty(nf * 4, dtype=np.int32); fv[0::4] = 3
            fv[1::4] = cache.faces[:,0]; fv[2::4] = cache.faces[:,1]; fv[3::4] = cache.faces[:,2]
            mesh = pv.PolyData(cache.xyz_final, fv)
            mesh.cell_data['RGB'] = face_colors

    elif blend_class_colors:
        # IMPORTANT: do not bake one class into the whole triangle.  Each mesh
        # vertex keeps its own canonical class RGB.  OpenGL then performs the
        # barycentric color interpolation visible in MicroStation/TerraScan,
        # while VTK flat lighting uses the triangle slope for brightness.
        vertex_colors = _shading_palette_rgb(app, cm, vc)

        if (em is not None and ea is not None and
                _get_rendered_cache_key(app) == cache.cache_key and
                em.GetNumberOfPoints() == nv and em.GetNumberOfCells() == nf):
            try:
                vtk_colors = em.GetPointData().GetScalars()
                if vtk_colors is not None and vtk_colors.GetNumberOfTuples() == nv:
                    numpy_support.vtk_to_numpy(vtk_colors)[:] = vertex_colors
                    vtk_colors.Modified(); em.GetPointData().Modified(); em.Modified()
                    ea.GetMapper().Modified()
                    _configure_microstation_color_blend_lighting(app, ea)
                    _set_rendered_cache_key(app, cache)
                    for actor in _attachment_overlay_actors(app):
                        vis = bool(actor.GetVisibility())
                        if labels_hidden and _is_overlay_label_actor(actor):
                            vis = False
                        _restore_overlay_actor(app.vtk_widget.renderer, actor, vis)
                    _hide_point_cloud_actors_for_shading(app)
                    checkpoint('inplace_color_update_and_overlays')
                    _restore_camera(app, saved_camera); app.vtk_widget.render()
                    checkpoint('render_present')
                    render_timings['total_render_path'] = time.perf_counter() - profile_start
                    _emit_shading_profile(
                        'render_inplace_blended', render_timings,
                        points=nv, faces=nf, smooth=0, blend=1,
                    )
                    return
            except Exception:
                pass
            cache._vtk_colors_ptr = None

        if cached_mesh is not None:
            mesh = cached_mesh
            try:
                mesh.GetCellData().RemoveArray('RGB')
            except Exception:
                pass
            mesh.point_data['RGB'] = vertex_colors
        else:
            fv = np.empty(nf * 4, dtype=np.int32); fv[0::4] = 3
            fv[1::4] = cache.faces[:,0]; fv[2::4] = cache.faces[:,1]; fv[3::4] = cache.faces[:,2]
            mesh = pv.PolyData(cache.xyz_final, fv)
            mesh.point_data['RGB'] = vertex_colors

    elif smooth_all_classes:
        if cache.vertex_normals is None or len(cache.vertex_normals) != nv:
            face_normals = cache.face_normals
            if face_normals is None or len(face_normals) != nf:
                face_normals = _compute_face_normals(cache.xyz_unique, cache.faces)
                cache.face_normals = face_normals
            cache.vertex_normals = _compute_vertex_normals(
                cache.xyz_unique, cache.faces, face_normals,
            )
        if cache.vertex_shade is None or len(cache.vertex_shade) != nv:
            cache.vertex_shade = _compute_shading(
                cache.vertex_normals, az, an, amb,
                z_values=cache.xyz_unique[:, 2],
            )
        vertex_colors = np.clip(
            lut[np.clip(cm, 0, mc - 1)] * cache.vertex_shade[:, None],
            0,
            255,
        ).astype(np.uint8)

        if (em is not None and ea is not None and
                _get_rendered_cache_key(app) == cache.cache_key and
                em.GetNumberOfPoints() == nv and em.GetNumberOfCells() == nf):
            try:
                vtk_colors = em.GetPointData().GetScalars()
                if vtk_colors is not None and vtk_colors.GetNumberOfTuples() == nv:
                    numpy_support.vtk_to_numpy(vtk_colors)[:] = vertex_colors
                    vtk_colors.Modified(); em.Modified(); ea.GetMapper().Modified()
                    _set_rendered_cache_key(app, cache)
                    for actor in _attachment_overlay_actors(app):
                        vis = bool(actor.GetVisibility())
                        if labels_hidden and _is_overlay_label_actor(actor):
                            vis = False
                        _restore_overlay_actor(app.vtk_widget.renderer, actor, vis)
                    _hide_point_cloud_actors_for_shading(app)
                    checkpoint("inplace_color_update_and_overlays")
                    _restore_camera(app, saved_camera); app.vtk_widget.render()
                    checkpoint("render_present")
                    render_timings["total_render_path"] = time.perf_counter() - profile_start
                    _emit_shading_profile(
                        "render_inplace", render_timings,
                        points=nv, faces=nf, smooth=int(smooth_all_classes),
                    )
                    return
            except Exception:
                pass
            cache._vtk_colors_ptr = None

        if cached_mesh is not None:
            mesh = cached_mesh
            mesh.point_data["RGB"] = vertex_colors
        else:
            fv = np.empty(nf * 4, dtype=np.int32); fv[0::4] = 3
            fv[1::4] = cache.faces[:,0]; fv[2::4] = cache.faces[:,1]; fv[3::4] = cache.faces[:,2]
            mesh = pv.PolyData(cache.xyz_final, fv)
            mesh.point_data["RGB"] = vertex_colors
            vtk_normals = numpy_support.numpy_to_vtk(
                cache.vertex_normals.astype(np.float32), deep=True,
            )
            vtk_normals.SetName("Normals")
            mesh.GetPointData().SetNormals(vtk_normals)
    else:
        if cache.shade is None or len(cache.shade) != nf:
            face_normals = cache.face_normals
            if face_normals is None or len(face_normals) != nf:
                face_normals = _compute_face_normals(cache.xyz_unique, cache.faces)
                cache.face_normals = face_normals
            cache.shade = _compute_face_shade(
                cache.xyz_unique, cache.faces, az, an, amb,
                face_normals=face_normals,
            ).astype(np.float32)

        face_classes = _face_class_ids_shading(app, cache, cm, cache.faces)
        face_colors = np.clip(
            lut[np.clip(face_classes, 0, mc - 1)] * cache.shade[:, None],
            0,
            255,
        ).astype(np.uint8)

        if (em is not None and ea is not None and
                _get_rendered_cache_key(app) == cache.cache_key and
                em.GetNumberOfPoints() == nv and em.GetNumberOfCells() == nf):
            try:
                vtk_colors = em.GetCellData().GetScalars()
                if vtk_colors is not None and vtk_colors.GetNumberOfTuples() == nf:
                    numpy_support.vtk_to_numpy(vtk_colors)[:] = face_colors
                    vtk_colors.Modified(); em.Modified(); ea.GetMapper().Modified()
                    _set_rendered_cache_key(app, cache)
                    for actor in _attachment_overlay_actors(app):
                        vis = bool(actor.GetVisibility())
                        if labels_hidden and _is_overlay_label_actor(actor):
                            vis = False
                        _restore_overlay_actor(app.vtk_widget.renderer, actor, vis)
                    _hide_point_cloud_actors_for_shading(app)
                    checkpoint("inplace_color_update_and_overlays")
                    _restore_camera(app, saved_camera); app.vtk_widget.render()
                    checkpoint("render_present")
                    render_timings["total_render_path"] = time.perf_counter() - profile_start
                    _emit_shading_profile(
                        "render_inplace", render_timings,
                        points=nv, faces=nf, smooth=int(smooth_all_classes),
                    )
                    return
            except Exception:
                pass
            cache._vtk_colors_ptr = None

        if cached_mesh is not None:
            mesh = cached_mesh
            mesh.cell_data["RGB"] = face_colors
        else:
            fv = np.empty(nf * 4, dtype=np.int32); fv[0::4] = 3
            fv[1::4] = cache.faces[:,0]; fv[2::4] = cache.faces[:,1]; fv[3::4] = cache.faces[:,2]
            mesh = pv.PolyData(cache.xyz_final, fv)
            mesh.cell_data["RGB"] = face_colors

    checkpoint("shade_color_and_mesh_prepare")
    plotter = app.vtk_widget; DXF = ("dxf_", "snt_", "grid_", "guideline", "snap_", "axis")
    def _prot(ns, a):
        if any(ns.lower().startswith(p) for p in DXF): return True
        return getattr(a, '_is_dxf_actor', False)

    protected = {}
    fast_cached_cleanup = False
    # PATCH 6A: the point-cloud hiding helper has already isolated the shading
    # view. DXF/SNT/grid/drawing actors stay attached and preserve visibility,
    # so both first builds and cached restores can remove only shading-owned
    # actors. Any exception falls back to the original conservative scan.
    if True:
        try:
            # Point-cloud actors were already hidden by
            # _hide_point_cloud_actors_for_shading().  A cached preset restore
            # only needs to replace shading-owned actors.  DXF/SNT/grid actors
            # remain attached to the renderer with their existing visibility.
            for name in ("shaded_mesh", "shaded_mesh_edges"):
                if name in plotter.actors:
                    plotter.remove_actor(name, render=False)
            for name in tuple(getattr(app, "_shading_add_overlays", ()) or ()):
                try:
                    plotter.remove_actor(name, render=False)
                except Exception:
                    pass
            fast_cached_cleanup = True
        except Exception as exc:
            print(f"SHADING_CACHE_FAST_RESTORE fallback=actor_cleanup error={exc}")
            fast_cached_cleanup = False

    if not fast_cached_cleanup:
        # Original conservative path.  Retained unchanged as the fallback for
        # first builds and for any cached restore that fails safety checks.
        for name in list(plotter.actors.keys()):
            try:
                a = plotter.actors[name]
                if _prot(name, a): protected[name] = (a, bool(a.GetVisibility()))
            except Exception:
                pass
        for name in list(plotter.actors.keys()):
            if name in protected: continue
            ns = str(name).lower()
            if ns.startswith("class_") or ns in ("main_pc", "main_pc_border", "_naksha_unified_cloud"):
                plotter.actors[name].SetVisibility(False)
            elif any(ns.startswith(p) for p in ["border_", "shaded_mesh", "__lod_overlay_"]):
                plotter.remove_actor(name, render=False)
    checkpoint("actor_scan_cleanup")
    # A normal shading build supersedes every temporary single-class delta.
    app._shading_add_overlays = []
    app._shading_remove_overlays = []

    app._shaded_mesh_actor = plotter.add_mesh(
        mesh, scalars="RGB", rgb=True, show_edges=False,
        lighting=bool(blend_class_colors and not crisp_hybrid),
        smooth_shading=False if blend_class_colors else smooth_all_classes,
        preference=("cell" if crisp_hybrid else ("point" if (blend_class_colors or smooth_all_classes) else "cell")),
        name="shaded_mesh", render=False,
    )
    if app._shaded_mesh_actor:
        setattr(app._shaded_mesh_actor, "_is_shading_mesh", True)
        setattr(app._shaded_mesh_actor, "_is_shading_crisp_base", bool(crisp_hybrid))
        p2 = app._shaded_mesh_actor.GetProperty()
        if crisp_hybrid:
            _enforce_crisp_base_actor_state(app)
        elif blend_class_colors:
            _configure_microstation_color_blend_lighting(app, app._shaded_mesh_actor)
        else:
            if not smooth_all_classes:
                p2.SetInterpolationToFlat()
            p2.SetLighting(False)
            p2.SetAmbient(1.); p2.SetDiffuse(0.); p2.SetSpecular(0.); p2.EdgeVisibilityOff()
    app._shaded_mesh_polydata = mesh; cache._vtk_colors_ptr = None
    cache._vtk_mesh = mesh
    _set_rendered_cache_key(app, cache)
    checkpoint("vtk_actor_create")

    # Multi-class fidelity is expressed by shaded FACETS, not raw point sprites,
    # for every quality.  Fast / Normal / Slow keep their existing representative
    # counts and topology; only the RGB presentation is shared across qualities.
    _remove_shading_class_detail_overlays(app, remove_static=True, remove_live=True)
    static_blend_actors = 0
    static_blend_vertices = 0
    if crisp_hybrid:
        static_blend_actors, static_blend_vertices = _build_static_multiclass_blend_overlays(
            app, cache, cm, vc, mixed_faces
        )
        print(
            'SHADING_CRISP_FACETS status=enabled '
            f'faces={nf:,} mixed_faces={mixed_count:,} pure_faces={nf-mixed_count:,} '
            f'mixed_face_budget={mixed_budget:,} '
            f'blend_actors={static_blend_actors} blend_vertices={static_blend_vertices:,} '
            'all_points_preserved=1 topology_unchanged=1 '
            'base=cell_shaded overlay=vertex_blend explicit_cell_normals=1'
        )
    quality_mode = normalize_shading_quality(getattr(app, 'shading_quality', 'normal'))
    if blend_class_colors:
        print(
            f'SHADING_COLOR_BLEND mode={quality_mode} active=1 '
            'class_source=per_vertex interpolation=barycentric '
            'lighting=flat_triangle_slope '
            f'presentation={"crisp_hybrid" if crisp_hybrid else "full_vertex_blend"} '
            f'points={nv:,} faces={nf:,} raw_point_overlay=disabled'
        )
    elif quality_mode == 'slow' and int(getattr(cache, 'n_visible_classes', 0) or 0) > 1:
        print(
            "SHADING_FACE_CLASS policy=peak_vertex mode=slow active=1 "
            f"points={nv:,} faces={nf:,} raw_point_overlay=disabled"
        )

    renderer = plotter.renderer; nr = 0
    if not fast_cached_cleanup:
        for name, (a, wv) in protected.items():
            vis = bool(wv)
            if labels_hidden and _is_overlay_label_actor(a):
                vis = False
            if _restore_overlay_actor(renderer, a, vis):
                nr += 1
        restored_attachment_actor_ids = set()
        for sn in ("dxf_actors", "snt_actors"):
            att_name = sn.replace("_actors", "_attachments")
            attachments = getattr(app, att_name, [])
            for entry in getattr(app, sn, []):
                target = os.path.basename(entry.get("filename", ""))
                att = next((a for a in attachments if os.path.basename(a.get("filename", "")) == target), None)
                
                actor_layer_map = {}
                selected_layers = None
                if att:
                    selected_layers = att.get("selected_layers")
                    cache_map = att.get("actor_cache_map", {})
                    for layer_name, actors in cache_map.items():
                        for a in actors:
                            actor_layer_map[id(a)] = layer_name

                for a in entry.get("actors", []):
                    try:
                        actor_id = id(a)
                        if actor_id in restored_attachment_actor_ids:
                            continue
                        restored_attachment_actor_ids.add(actor_id)
                        if selected_layers is not None and id(a) in actor_layer_map:
                            layer_name = actor_layer_map[id(a)]
                            visible = layer_name in selected_layers
                        else:
                            visible = True
                        if labels_hidden and _is_overlay_label_actor(a):
                            visible = False
                        if _restore_overlay_actor(renderer, a, visible):
                            nr += 1
                    except Exception:
                        pass
        if nr > 0: print(f"   ? Restored {nr} DXF/SNT actors")
    else:
        # Fast shading builds intentionally keep SNT/DXF overlays alive instead
        # of rebuilding them.  Once the new shaded mesh actor is inserted,
        # however, those already-attached line actors can lose the effective
        # draw/depth priority they had before the mesh existed.  Re-queue only
        # the existing attachment actors here.  This does NOT parse/rebuild SNT
        # data, does NOT recreate VTK geometry, and preserves the actor objects,
        # layer visibility and caches; it only removes/re-adds each live overlay
        # prop and reapplies the existing coincident-topology priority helper.
        overlay_requeued = 0
        for actor in _attachment_overlay_actors(app):
            try:
                visible = bool(actor.GetVisibility())
                if labels_hidden and _is_overlay_label_actor(actor):
                    visible = False
                if _restore_overlay_actor(renderer, actor, visible):
                    overlay_requeued += 1
            except Exception:
                pass

        # Reassert point-cloud isolation after overlays are re-queued.  This is
        # cheap and prevents any class/unified point actor from becoming visible
        # because of another tool's actor bookkeeping while shading is active.
        _hide_point_cloud_actors_for_shading(app)

        print(
            f"SHADING_ACTOR_FAST_PATH cached_restore={int(bool(cached_restore))} "
            f"overlays=preserved actor_scan=skipped overlay_requeued={overlay_requeued}"
        )
    checkpoint("overlay_restore")
    _restore_camera(app, saved_camera); plotter.set_background("black")
    plotter.renderer.ResetCameraClippingRange()
    try:
        m = app._shaded_mesh_actor.GetMapper()
        if m: m.StaticOn(); m.SetResolveCoincidentTopologyToPolygonOffset(); m.InterpolateScalarsBeforeMappingOff()
    except Exception:
        pass
    checkpoint("camera_mapper_setup")
    # Pay the spatial-index cost once when the single-class mesh is created,
    # not on the user's first other-class -> ground brush stroke.
    if (getattr(cache, 'n_visible_classes', 0) == 1 and
            cache.xyz_unique is not None and len(cache.xyz_unique) > 0):
        try:
            xyz_id = id(cache.xyz_unique)
            tree_valid = (
                cache._xy_tree is not None
                and cache._xy_tree_size == len(cache.xyz_unique)
                and getattr(cache, '_xy_tree_xyz_id', None) == xyz_id
            )
            if tree_valid:
                print(f"SHADING_SPATIAL_INDEX reused points={cache._xy_tree_size}")
            else:
                from scipy.spatial import cKDTree
                ti = time.perf_counter()
                cache._xy_tree = cKDTree(cache.xyz_unique[:, :2])
                cache._xy_tree_size = len(cache.xyz_unique)
                cache._xy_tree_xyz_id = xyz_id
                print(f"SHADING_SPATIAL_INDEX points={cache._xy_tree_size} elapsed={(time.perf_counter()-ti)*1000:.1f}ms")
        except Exception as exc:
            cache._xy_tree = None; cache._xy_tree_size = 0
            cache._xy_tree_xyz_id = None
            print(f"SHADING_SPATIAL_INDEX skipped={exc}")
    checkpoint("spatial_index")
    plotter.render()
    _force_present_shaded_mesh(app)
    checkpoint("render_present")
    shading_kind = (
        "single-class"
        if getattr(cache, 'n_visible_classes', 0) == 1
        else "all/selected classes"
    )
    ms = f"FLAT/FACETED ({shading_kind})"
    render_timings["total_render_path"] = time.perf_counter() - profile_start
    _emit_shading_profile(
        "render_full", render_timings,
        points=nv, faces=nf, smooth=int(smooth_all_classes),
        restored_actors=nr, cached_mesh=int(cached_mesh is not None),
    )
    print(f"   ?? Shaded Mesh [{ms}]: {nf:,} faces in {(time.time()-t0)*1000:.0f}ms")
# ---------------------------------------------------------------
# ALL REMAINING FUNCTIONS — identical behavior, compressed
# ---------------------------------------------------------------
def refresh_shaded_after_classification_fast(app, changed_mask=None, delta=None):
    """Optimized classification update for single-class shading."""
    cache = get_cache()
    if cache.faces is None or len(cache.faces) == 0:
        update_shaded_class(app, force_rebuild=True); return True 
    
    # Hide point cloud actors
    if hasattr(app, 'vtk_widget'):
        for name in list(app.vtk_widget.actors.keys()):
            ns = str(name).lower()
            if ns.startswith("class_") or ns in ("main_pc", "main_pc_border"):
                app.vtk_widget.actors[name].SetVisibility(False)
    
    isc = getattr(cache, 'n_visible_classes', 0) == 1
    sci = getattr(cache, 'single_class_id', None)
    vc = _get_shading_visibility(app)
    va = np.array(sorted(vc), dtype=np.int32)
    
    if changed_mask is None or not np.any(changed_mask):
        if not isc or sci is None:
            return _update_colors_gpu_fast(app, cache, changed_mask=None, _visible_classes=vc)
        return True
    
    data = getattr(app, "data", None)
    if not isinstance(data, dict):
        return False
    cls_raw = data.get("classification")
    xyz_raw = data.get("xyz")
    if cls_raw is None or xyz_raw is None:
        return False
    # LAS classification is already integer data; do not copy 13M+ entries
    # to int32 just to inspect a few hundred changed points.
    cls = np.asarray(cls_raw)
    ci = np.flatnonzero(changed_mask)
    cc = cls[ci]
    nh = ~np.isin(cc, va)  # Points now hidden (classified away from visible class)
    nvis = np.isin(cc, va)  # Points now visible
    g2u = cache.build_global_to_unique(len(xyz_raw))

    if delta is None:
        delta = _recover_classification_delta(app, ci)

    # Prefer the explicit transition contract.  Legacy callers remain
    # supported by the existing current-state inference below.
    if delta is not None and isc and sci is not None:
        try:
            dci = np.asarray(delta.changed_indices, dtype=np.int64).ravel()
            dold = np.asarray(delta.old_classes).ravel()
            dnew = np.asarray(delta.new_classes).ravel()
            decision = plan_single_class_shading_edit(
                app, dci, dold, dnew, int(sci)
            )
            add_count = int(np.count_nonzero((dold != sci) & (dnew == sci)))
            remove_count = int(np.count_nonzero((dold == sci) & (dnew != sci)))
            print(
                "SHADING_EDIT_PLAN "
                f"operation={delta.operation} origin={delta.origin_view} "
                f"visible_class={int(sci)} changed={len(dci)} "
                f"add={add_count} remove={remove_count} decision={decision.name}"
            )
            if decision is ShadingEditKind.NO_GEOMETRY_CHANGE:
                return _update_colors_gpu_fast(
                    app, cache, changed_mask=changed_mask,
                    _visible_classes=vc, _defer_render=True,
                )
            if decision is ShadingEditKind.LOCAL_ADD:
                added = dci[(dold != sci) & (dnew == sci)]

                # Undo of a previous LOCAL_REMOVE must restore the exact faces
                # that were removed by that edit.  Re-triangulating the points
                # again (overlay/incremental add) creates a different Delaunay
                # surface and is the source of the post-undo terrain scars.
                # This inverse path touches only the recorded local patch and
                # leaves every unrelated face unchanged.
                if str(getattr(delta, "operation", "")).lower() == "undo":
                    restored = _undo_last_local_remove_patch(app, added)
                    if restored is True:
                        return True
                    if restored is False:
                        print(
                            "SHADING_UNDO_LOCAL_RESTORE status=miss "
                            "fallback=existing_local_add"
                        )

                if _fast_add_overlay(app, added):
                    return True
                if _fast_incremental_add_points(app, added):
                    return True
                print("SHADING_EDIT_PLAN decision=FULL_REBUILD reason=local_add_failed")
                update_shaded_class(app, force_rebuild=True)
                return True
            if decision is ShadingEditKind.LOCAL_REMOVE:
                removed = dci[(dold == sci) & (dnew != sci)]
                if _local_remove_single_class_points(app, removed):
                    return True
                print("SHADING_EDIT_PLAN decision=FULL_REBUILD reason=local_remove_failed")
                update_shaded_class(app, force_rebuild=True)
                return True
            print("SHADING_EDIT_PLAN decision=FULL_REBUILD reason=planner_threshold_or_mixed")
            update_shaded_class(app, force_rebuild=True)
            return True
        except Exception as exc:
            print(f"SHADING_EDIT_PLAN decision=FULL_REBUILD reason=planner_error:{exc}")
            update_shaded_class(app, force_rebuild=True)
            return True
    
    # Multi-class fast membership path.
    #
    # If every edited point stays on the same visibility side of the shaded
    # subset, geometry is unchanged. In the common all-classes-visible case
    # this is always true, so skip the expensive orphan checks that call
    # np.unique(cache.faces.ravel()) across tens of millions of face indices.
    if not isc or sci is None:
        try:
            membership_unchanged = False
            visible_after = True

            if delta is not None:
                mdci = np.asarray(delta.changed_indices, dtype=np.int64).ravel()
                mdold = np.asarray(delta.old_classes).ravel()
                mdnew = np.asarray(delta.new_classes).ravel()
                if mdci.size == mdold.size == mdnew.size:
                    old_vis = np.isin(mdold, va)
                    new_vis = np.isin(mdnew, va)
                    membership_unchanged = not np.any(old_vis != new_vis)
                    visible_after = bool(np.any(new_vis))
                    if membership_unchanged:
                        if not visible_after:
                            return True
                        if _fast_multiclass_color_overlay(
                            app,
                            cache,
                            changed_mask=changed_mask,
                            changed_indices=mdci,
                            visible_classes=vc,
                        ):
                            return True
                        return _update_colors_gpu_fast(
                            app,
                            cache,
                            changed_mask=changed_mask,
                            _visible_classes=vc,
                            _defer_render=True,
                            _changed_indices=mdci,
                        )

            # Safe fallback when explicit old/new classes are unavailable:
            # if every known palette class is visible, no classification can
            # change shading membership.
            known_classes = set(int(c) for c in app.class_palette.keys())
            if known_classes and known_classes.issubset(set(int(c) for c in vc)):
                if _fast_multiclass_color_overlay(
                    app,
                    cache,
                    changed_mask=changed_mask,
                    changed_indices=ci,
                    visible_classes=vc,
                ):
                    return True
                return _update_colors_gpu_fast(
                    app,
                    cache,
                    changed_mask=changed_mask,
                    _visible_classes=vc,
                    _defer_render=True,
                    _changed_indices=ci,
                )
        except Exception as exc:
            print(f"SHADING_MULTI_FAST_PATH fallback={exc}")

        # We are leaving the topology-stable overlay regime (for example a
        # visible class becomes hidden or vice versa).  Commit prior sparse
        # color edits before the existing topology-aware path mutates faces.
        if getattr(cache, '_multiclass_dirty_faces', None):
            _bake_multiclass_color_overlay_into_base(app, cache, vc)

    # ? FAST PATH for single-class: blacken affected faces immediately, queue rebuild
    if isc and sci is not None:
        mesh = getattr(app, '_shaded_mesh_polydata', None)
        if np.all(nh) and mesh:
            # All changed points are now hidden - fast blacken
            cellc = mesh.GetCellData().GetScalars()
            if cellc and cache.faces is not None and cellc.GetNumberOfTuples() == len(cache.faces):
                try:
                    vp = numpy_support.vtk_to_numpy(cellc)
                    cu = g2u[ci]; cu = cu[cu >= 0]
                    if len(cu) > 0:
                        cs = np.zeros(len(cache.unique_indices), dtype=bool)
                        cs[cu] = True
                        af = cs[cache.faces[:,0]] | cs[cache.faces[:,1]] | cs[cache.faces[:,2]]
                        vp[af] = [0,0,0]
                        cellc.Modified()
                        mesh.Modified()
                        a = getattr(app, '_shaded_mesh_actor', None)
                        if a: a.GetMapper().Modified()
                        app.vtk_widget.render()
                except Exception:
                    pass
                _queue_incremental_patch(app, sci)
                return True
    
    # Handle visibility changes
    if np.all(nvis):
        if _check_previous_classes_visible(app, ci, va):
            # ? FIX: Check for orphaned vertices before taking color-only fast path
            _has_orphaned_cls = False
            _uv_cls = np.zeros(len(cache.unique_indices), dtype=bool)
            if cache.faces is not None and len(cache.faces) > 0:
                _uv_cls[np.unique(cache.faces.ravel())] = True
            _cu_cls = g2u[ci]; _cu_valid = _cu_cls[_cu_cls >= 0]
            if len(_cu_valid) > 0:
                _has_orphaned_cls = bool(np.any(~_uv_cls[_cu_valid]))
            if not _has_orphaned_cls:
                if _update_colors_gpu_fast(app, cache, changed_mask=changed_mask, _visible_classes=vc, _defer_render=True):
                    return True
        nvg = ci[nvis]
        mg = nvg[g2u[nvg] < 0]
        # ? FIX: Also find orphaned vertices (in unique_indices but not in any face)
        if len(mg) == 0:
            _uv_cls2 = np.zeros(len(cache.unique_indices), dtype=bool)
            if cache.faces is not None and len(cache.faces) > 0:
                _uv_cls2[np.unique(cache.faces.ravel())] = True
            _in_unique = g2u[nvg] >= 0
            _orphaned_unique = nvg[_in_unique]
            if len(_orphaned_unique) > 0:
                _cu_orph = g2u[_orphaned_unique]
                _orph_mask = ~_uv_cls2[_cu_orph]
                if np.any(_orph_mask):
                    # Treat orphaned vertices as needing face rebuild
                    mg = _orphaned_unique[_orph_mask]
        if len(mg) > 0:
            if _fast_incremental_add_points(app, mg):
                return True
            _queue_deferred_rebuild(app, "cls new vis", newly_visible_indices=mg)
            _update_colors_gpu_fast(app, cache, changed_mask=changed_mask, _visible_classes=vc, _defer_render=True)
            return True
        if _update_colors_gpu_fast(app, cache, changed_mask=changed_mask, _visible_classes=vc, _defer_render=True):
            return True
    
    voided = None
    if np.any(nvis):
        nvg = ci[nvis]
        nim = int(np.sum(g2u[nvg] < 0))
        if nim > 0:
            pwh = True
            if getattr(app, '_shading_visibility_override', None) is None:
                for sa in ('undo_stack', 'undostack'):
                    stk = getattr(app, sa, None)
                    if stk:
                        try:
                            old = (stk[-1].get('old_classes') or stk[-1].get('oldclasses'))
                            if old is not None:
                                pwh = not set(int(x) for x in np.unique(np.asarray(old))).issubset(set(int(c) for c in vc))
                        except Exception:
                            pass
                        break
            if pwh:
                _queue_deferred_rebuild(app, "cls new vis", newly_visible_indices=nvg[g2u[nvg] < 0])
    
    if np.any(nh):
        voided = ci[nh]
        mesh = getattr(app, '_shaded_mesh_polydata', None)
        if mesh:
            pc = mesh.GetPointData().GetScalars()
            if pc and pc.GetNumberOfTuples() == len(cache.unique_indices):
                hu = g2u[voided]
                hu = hu[(hu >= 0) & (hu < len(cache.unique_indices))]
                if len(hu) > 0:
                    numpy_support.vtk_to_numpy(pc)[hu] = [0,0,0]
                    pc.Modified()
    
    if _update_colors_gpu_fast(app, cache, changed_mask, _visible_classes=vc, _defer_render=True):
        if voided is not None and len(voided) > 0:
            _queue_deferred_rebuild(app, "void cleanup")
        return True
    
    update_shaded_class(app, force_rebuild=True)
    return True

def _incremental_visibility_patch(app, cgi, vcs):
    cache = get_cache()
    if cache.faces is None or cache.xyz_unique is None or cache.xyz_final is None:
        return False
    xr = app.data.get("xyz"); cr = app.data.get("classification")
    if xr is None or cr is None: return False
    cls = cr.astype(np.int32); cm = cls[cache.unique_indices]
    va = np.array(sorted(vcs), dtype=np.int32); viv = np.isin(cm, va)
    g2u = cache.build_global_to_unique(len(xr)); cim = g2u[cgi]; cim = cim[cim >= 0]
    
    az = getattr(app, 'last_shade_azimuth', 45.)
    an = getattr(app, 'last_shade_angle', 45.)
    am = getattr(app, 'shade_ambient', .25)
    
    if len(cim) > 0 and np.all(viv[cim]):
        fwc = (np.isin(cache.faces[:,0], cim) | np.isin(cache.faces[:,1], cim) |
               np.isin(cache.faces[:,2], cim))
        if np.sum(fwc) > 0:
            km = ~fwc
            cache.faces = cache.faces[km]
            cache.face_normals = cache.face_normals[km] if cache.face_normals is not None else None
            # ? FIX: Recompute ALL kept face shading with global z-range
            cache.shade = _compute_face_shade_global_z(
                cache.xyz_unique, cache.faces, az, an, am,
                face_normals=cache.face_normals)
            if cache.face_normals is not None and len(cache.face_normals) > 0:
                cache.vertex_normals = _compute_vertex_normals(
                    cache.xyz_unique, cache.faces, cache.face_normals)
                cache.vertex_shade = _compute_shading(
                    cache.vertex_normals, az, an, am,
                    z_values=cache.xyz_unique[:, 2])
            cache._vtk_colors_ptr = None
            _render_mesh(app, cache, cr, _save_camera(app))
            return True
    
    vnh = ~viv
    if np.sum(vnh) == 0: return True
    ifm = vnh[cache.faces[:,0]] | vnh[cache.faces[:,1]] | vnh[cache.faces[:,2]]
    vfm = ~ifm
    if np.sum(vfm) == 0: return False
    
    vf = cache.faces[vfm]
    vn = cache.face_normals[vfm] if cache.face_normals is not None else None
    
    hvi = np.flatnonzero(vnh); ifa = cache.faces[ifm]
    ihf = np.zeros(len(cache.unique_indices), dtype=bool)
    if len(hvi) > 0: ihf[hvi] = True
    avi = ifa.ravel(); bv = np.unique(avi[~ihf[avi]]).astype(np.int32)
    
    if len(bv) < 3:
        cache.faces = vf
        cache.face_normals = vn
        # ? FIX: global z-range for kept faces
        cache.shade = _compute_face_shade_global_z(
            cache.xyz_unique, vf, az, an, am, face_normals=vn)
        if vn is not None and len(vn) > 0:
            cache.vertex_normals = _compute_vertex_normals(cache.xyz_unique, vf, vn)
            cache.vertex_shade = _compute_shading(
                cache.vertex_normals, az, an, am,
                z_values=cache.xyz_unique[:, 2])
        cache._vtk_colors_ptr = None
        _render_mesh(app, cache, cr, _save_camera(app))
        return True
    
    bxy = cache.xyz_unique[bv, :2]
    try:
        lf = _do_triangulate(bxy)
    except Exception:
        cache.faces = vf; cache.face_normals = vn
        cache.shade = _compute_face_shade_global_z(
            cache.xyz_unique, vf, az, an, am, face_normals=vn)
        cache._vtk_colors_ptr = None
        _render_mesh(app, cache, cr, _save_camera(app))
        return True
    
    if len(lf) == 0:
        cache.faces = vf; cache.face_normals = vn
        cache.shade = _compute_face_shade_global_z(
            cache.xyz_unique, vf, az, an, am, face_normals=vn)
        cache._vtk_colors_ptr = None
        _render_mesh(app, cache, cr, _save_camera(app))
        return True
    
    be = max(bxy[:,0].max()-bxy[:,0].min(), bxy[:,1].max()-bxy[:,1].min())
    lf = _filter_edges_by_absolute(
        lf, bxy, max(be * 0.5, cache.spacing * cache.max_edge_factor))
    
    if len(lf) == 0:
        cache.faces = vf; cache.face_normals = vn
        cache.shade = _compute_face_shade_global_z(
            cache.xyz_unique, vf, az, an, am, face_normals=vn)
        cache._vtk_colors_ptr = None
        _render_mesh(app, cache, cr, _save_camera(app))
        return True
    
    pf = bv[lf]
    pn = _compute_face_normals(cache.xyz_unique, pf)
    
    # Combine faces first, then shade ALL with global z-range
    cache.faces = np.vstack([vf, pf])
    cache.face_normals = pn if vn is None else np.vstack([vn, pn])
    
    # ? FIX: Single call with global z-range for ALL faces
    cache.shade = _compute_face_shade_global_z(
        cache.xyz_unique, cache.faces, az, an, am,
        face_normals=cache.face_normals)
    
    _recompute_vertex_normals_partial(cache, len(vf))
    cache._vtk_colors_ptr = None
    _render_mesh(app, cache, cr, _save_camera(app))
    return True

def refresh_shaded_after_visibility_change(app, cgi, vcs):
    cache = get_cache()
    if cache.faces is None or cache.xyz_unique is None:
        clear_shading_cache("no cache"); update_shaded_class(app, force_rebuild=True); return
    if not _incremental_visibility_patch(app, cgi, vcs):
        clear_shading_cache("patch failed"); update_shaded_class(app, force_rebuild=True)

def _compute_face_shade_global_z(xyz_unique, faces, azimuth, angle, ambient, face_normals=None):
    """Compute face shading using standard MicroStation hillshade with GLOBAL z-range."""
    if xyz_unique is None or faces is None or len(faces) == 0:
        return np.array([], dtype=np.float32)
    if face_normals is None or len(face_normals) != len(faces):
        face_normals = _compute_face_normals(xyz_unique, faces)
    
    # Standard hillshade light direction with geographic-to-math azimuth conversion
    zenith_rad = np.radians(90.0 - angle)
    az_math_rad = np.radians(360.0 - azimuth + 90.0)
    lx = np.sin(zenith_rad) * np.cos(az_math_rad)
    ly = np.sin(zenith_rad) * np.sin(az_math_rad)
    lz = np.cos(zenith_rad)
    
    if HAS_NUMBA:
        return _compute_shading_fast(face_normals, np.empty(0, dtype=np.float64),
                                     lx, ly, lz, ambient, 0., 0.)
    
    ld = np.array([lx, ly, lz], dtype=np.float64)
    NdL = (face_normals * ld).sum(1)
    ni = np.maximum(NdL, 0.0)
    ni = np.maximum(ni, ambient)
    return np.clip(ni, 0., 1.).astype(np.float32)


def _multi_class_region_undo_patch(app, changed_mask, vcs):
    cache = get_cache()
    if cache.faces is None or cache.xyz_unique is None: return False
    xyz = app.data.get("xyz"); cr = app.data.get("classification")
    if xyz is None or cr is None: return False
    cls = cr.astype(np.int32); va = np.array(sorted(vcs), dtype=np.int32)
    ci = np.flatnonzero(changed_mask); cx = xyz[ci]
    xn, yn = cx[:,0].min(), cx[:,1].min()
    xx, yx = cx[:,0].max(), cx[:,1].max()
    mg = max(cache.spacing * 5, 1.) if cache.spacing > 0 else 10.
    xn -= mg; yn -= mg; xx += mg; yx += mg
    
    cf = cache.xyz_final
    irm = ((cf[:,0] >= xn) & (cf[:,0] <= xx) &
           (cf[:,1] >= yn) & (cf[:,1] <= yx))
    fir = irm[cache.faces[:,0]] & irm[cache.faces[:,1]] & irm[cache.faces[:,2]]
    fo = cache.faces[~fir]
    no = cache.face_normals[~fir] if cache.face_normals is not None else None
    
    vm = np.isin(cls, va); vi = np.flatnonzero(vm); vx = xyz[vi]
    irv = ((vx[:,0] >= xn) & (vx[:,0] <= xx) &
           (vx[:,1] >= yn) & (vx[:,1] <= yx))
    lgi = vi[irv]; lx = vx[irv]; nl = len(lx)
    
    az = getattr(app, 'last_shade_azimuth', 45.)
    an = getattr(app, 'last_shade_angle', 45.)
    am = getattr(app, 'shade_ambient', .25)
    
    if nl < 3:
        cache.faces = fo; cache.face_normals = no
        cache.shade = _compute_face_shade_global_z(
            cache.xyz_unique, fo, az, an, am, face_normals=no)
        cache._vtk_colors_ptr = None
        if no is not None and len(no) > 0:
            cache.vertex_normals = _compute_vertex_normals(cache.xyz_unique, fo, no)
            cache.vertex_shade = _compute_shading(
                cache.vertex_normals, az, an, am,
                z_values=cache.xyz_unique[:, 2])
        _render_mesh(app, cache, cr, _save_camera(app))
        return True
    
    lo = lx.min(axis=0); lxo = lx - lo
    xe = lxo[:,0].max() - lxo[:,0].min()
    ye = lxo[:,1].max() - lxo[:,1].min()
    ns = np.sqrt(max(xe * ye, 1.) / max(nl, 1))
    pr = max(ns * 0.3, 0.005)
    pu = nl / max((pr / max(ns, 1e-9))**2, 1)
    if pu > 80000: pr = max(pr * np.sqrt(pu / 80000), 0.005)
    xyg = np.floor(lxo[:,:2] / pr).astype(np.int64)
    si = np.lexsort((-lxo[:,2], xyg[:,1], xyg[:,0])); xys = xyg[si]
    d = np.diff(xys, axis=0)
    um = np.concatenate([[True], (d[:,0] != 0) | (d[:,1] != 0)])
    ui = si[um]; ux = lxo[ui]; ug = lgi[ui]
    
    if len(ux) < 3:
        cache.faces = fo; cache.face_normals = no
        cache.shade = _compute_face_shade_global_z(
            cache.xyz_unique, fo, az, an, am, face_normals=no)
        cache._vtk_colors_ptr = None
        _render_mesh(app, cache, cr, _save_camera(app))
        return True
    
    xy = ux[:,:2]
    try:
        lf = _do_triangulate(xy)
    except Exception:
        cache.faces = fo; cache.face_normals = no
        cache.shade = _compute_face_shade_global_z(
            cache.xyz_unique, fo, az, an, am, face_normals=no)
        cache._vtk_colors_ptr = None
        _render_mesh(app, cache, cr, _save_camera(app))
        return True
    
    if len(lf) == 0:
        cache.faces = fo; cache.face_normals = no
        cache.shade = _compute_face_shade_global_z(
            cache.xyz_unique, fo, az, an, am, face_normals=no)
        cache._vtk_colors_ptr = None
        _render_mesh(app, cache, cr, _save_camera(app))
        return True
    
    ls = np.sqrt((xy[:,0].max()-xy[:,0].min()) *
                 (xy[:,1].max()-xy[:,1].min()) / max(len(xy), 1))
    lf = _filter_edges_by_absolute(
        lf, xy, max(ls * 100, cache.spacing * 100))
    
    if len(lf) == 0:
        cache.faces = fo; cache.face_normals = no
        cache.shade = _compute_face_shade_global_z(
            cache.xyz_unique, fo, az, an, am, face_normals=no)
        cache._vtk_colors_ptr = None
        _render_mesh(app, cache, cr, _save_camera(app))
        return True
    
    g2u = cache.build_global_to_unique(len(xyz)); ltc = g2u[ug]
    npm = ltc < 0
    _snap_ui = _snap_xu = _snap_xf = None
    if int(np.sum(npm)) > 0:
        ng = ug[npm]; nx2 = ux[npm] + lo - cache.offset
        _snap_ui = cache.unique_indices
        _snap_xu = cache.xyz_unique
        _snap_xf = cache.xyz_final
        cache.unique_indices = np.concatenate([cache.unique_indices, ng])
        cache.xyz_unique = np.vstack([cache.xyz_unique, nx2])
        cache.xyz_final = np.vstack([cache.xyz_final, nx2 + cache.offset])
        cache._global_to_unique = None
        g2u = cache.build_global_to_unique(len(xyz))
        ltc = g2u[ug]
        if np.any(ltc < 0):
            cache.unique_indices = _snap_ui
            cache.xyz_unique = _snap_xu
            cache.xyz_final = _snap_xf
            cache._global_to_unique = None
            return False
    
    npf = ltc[lf]
    if np.any(npf < 0):
        if _snap_ui is not None:
            cache.unique_indices = _snap_ui
            cache.xyz_unique = _snap_xu
            cache.xyz_final = _snap_xf
            cache._global_to_unique = None
        return False
    
    pn = _compute_face_normals(cache.xyz_unique, npf)
    
    # Combine all faces, then shade with global z-range
    nk = len(fo)
    cache.faces = np.vstack([fo, npf])
    cache.face_normals = pn if no is None else np.vstack([no, pn])
    
    # ? FIX: Single shade call with global z-range for ALL faces
    cache.shade = _compute_face_shade_global_z(
        cache.xyz_unique, cache.faces, az, an, am,
        face_normals=cache.face_normals)
    
    _recompute_vertex_normals_partial(cache, nk)
    cache._vtk_colors_ptr = None
    _render_mesh(app, cache, cr, _save_camera(app))
    return True

def _rebuild_single_class(app, sci):
    """Optimized single-class rebuild."""
    cache = get_cache()
    if cache.faces is None or len(cache.faces) == 0:
        cache.clear("no mesh")
        _do_full_rebuild(app, sci)
        return
    
    # Use cache values for shading parameters
    az = cache.last_azimuth if cache.last_azimuth >= 0 else getattr(app, 'last_shade_azimuth', 45.)
    an = cache.last_angle if cache.last_angle >= 0 else getattr(app, 'last_shade_angle', 45.)
    am = cache.last_ambient if cache.last_ambient >= 0 else getattr(app, 'shade_ambient', .25)
    
    data = getattr(app, "data", None)
    if not isinstance(data, dict):
        return
    cls_raw = data.get("classification")
    if cls_raw is None:
        return
    cls = cls_raw.astype(np.int32)
    cvl = cls[cache.unique_indices]
    vl = cvl != sci
    if np.sum(vl) == 0:
        return
    
    ifm = vl[cache.faces[:,0]] | vl[cache.faces[:,1]] | vl[cache.faces[:,2]]
    vfm = ~ifm
    if np.sum(vfm) == 0:
        cache.clear("all invalid")
        _do_full_rebuild(app, sci)
        return
    
    rva = np.flatnonzero(vl).astype(np.int32)
    ifa = cache.faces[ifm]
    irf = np.zeros(len(cache.unique_indices), dtype=bool)
    if len(rva) > 0:
        irf[rva] = True
    bv = np.unique(ifa.ravel()[~irf[ifa.ravel()]]).astype(np.int32)
    
    vf = cache.faces[vfm]
    vn = cache.face_normals[vfm] if cache.face_normals is not None else None
    vs = cache.shade[vfm] if cache.shade is not None and len(cache.shade) == len(cache.faces) else None
    npf = np.array([], dtype=np.int32).reshape(0, 3)
    
    if len(bv) >= 3:
        bxy = cache.xyz_unique[bv, :2]
        try:
            lf = _do_triangulate(bxy)
            if len(lf) > 0 and len(bv) > 10:
                xr = bxy[:,0].max() - bxy[:,0].min()
                yr = bxy[:,1].max() - bxy[:,1].min()
                lf = _filter_edges_by_absolute(
                    lf, bxy,
                    max(np.sqrt(xr * yr / len(bv)) * 5,
                        cache.spacing * 1000))
            if len(lf) > 0:
                npf = bv[lf]
        except Exception:
            pass
    
    if len(npf) > 0:
        pn = _compute_face_normals(cache.xyz_unique, npf)
        cache.faces = np.vstack([vf, npf])
        cache.face_normals = pn if vn is None else np.vstack([vn, pn])
        
        # Compute shade for new patch with global z-range
        ps = _compute_face_shade_global_z(cache.xyz_unique, npf, az, an, am, face_normals=pn)
        cache.shade = ps if vs is None else np.concatenate([vs, ps])
    else:
        cache.faces = vf
        cache.face_normals = vn
        cache.shade = vs
    
    cache._vtk_colors_ptr = None
    _render_mesh(app, cache, app.data.get("classification"), _save_camera(app))

def _do_full_rebuild(app, sci):
    sv = {c: app.class_palette[c].get("show", True) for c in app.class_palette}
    for c in app.class_palette: app.class_palette[c]["show"] = (int(c) == sci)
    try: update_shaded_class(app, force_rebuild=True)
    finally:
        for c, v in sv.items(): app.class_palette[c]["show"] = v

def _check_previous_classes_visible(app, ci, va):
    try:
        for attr, key in [('redostack', 'newclasses'), ('redostack', 'new_classes'), ('undostack', 'oldclasses'), ('undostack', 'old_classes')]:
            stk = getattr(app, attr, None)
            if stk:
                prev = stk[-1].get(key)
                if prev is not None:
                    if not hasattr(prev, '__iter__') or np.ndim(prev) == 0: return int(prev) in set(va.tolist())
                    return bool(np.all(np.isin(np.asarray(prev), va)))
        return set(int(c) for c in app.class_palette.keys()).issubset(set(va.tolist()))
    except: return False


def _normalize_history_classes(values, changed_count):
    """Return one class value per changed point for heterogeneous undo entries."""
    array = np.asarray(values).ravel()
    if array.size == changed_count:
        return array
    if array.size == 1 and changed_count > 0:
        return np.full(changed_count, array[0], dtype=array.dtype)
    return None


def refresh_shaded_after_history_fast(
        app, changed_mask, old_classes, new_classes, operation):
    """Patch shading for undo/redo without rebuilding stable multi-class topology.

    Classification tools store history in more than one valid representation:
    some keep one target class while others keep one class per changed point.
    Normalize both forms here and use the explicit before/after transition.
    """
    changed_indices = np.flatnonzero(changed_mask).astype(np.int64, copy=False)
    if changed_indices.size == 0:
        return True

    old_values = _normalize_history_classes(
        old_classes, changed_indices.size
    )
    new_values = _normalize_history_classes(
        new_classes, changed_indices.size
    )
    if old_values is None or new_values is None:
        print(
            "SHADING_HISTORY_FAST status=unsupported_history_shape "
            f"operation={operation} changed={changed_indices.size}"
        )
        return False

    cache = get_cache()
    visible_classes = _get_shading_visibility(app)
    is_single = getattr(cache, "n_visible_classes", 0) == 1

    if not is_single:
        visible_array = np.asarray(
            sorted(int(code) for code in visible_classes), dtype=np.int32
        )
        old_visible = np.isin(old_values, visible_array)
        new_visible = np.isin(new_values, visible_array)
        if not np.any(old_visible != new_visible):
            if not np.any(new_visible):
                return True
            if _fast_multiclass_color_overlay(
                app,
                cache,
                changed_mask=changed_mask,
                changed_indices=changed_indices,
                visible_classes=visible_classes,
            ):
                print(
                    "SHADING_HISTORY_FAST status=overlay "
                    f"operation={operation} changed={changed_indices.size}"
                )
                return True
            if _update_colors_gpu_fast(
                app,
                cache,
                changed_mask=changed_mask,
                _visible_classes=visible_classes,
                _defer_render=True,
                _changed_indices=changed_indices,
            ):
                print(
                    "SHADING_HISTORY_FAST status=color_patch "
                    f"operation={operation} changed={changed_indices.size}"
                )
                return True

    return refresh_shaded_after_classification_fast(
        app,
        changed_mask=changed_mask,
        delta=ClassificationDelta(
            changed_indices=changed_indices,
            old_classes=old_values,
            new_classes=new_values,
            operation=str(operation or "history"),
            origin_view="undo_redo_history",
        ),
    )


def refresh_shaded_after_undo_fast(app, changed_mask=None):
    """Optimized undo handling for single-class shading."""
    cache = get_cache()
    if cache.faces is None or len(cache.faces) == 0:
        return False
    
    isc = getattr(cache, 'n_visible_classes', 0) == 1
    sci = getattr(cache, 'single_class_id', None)
    vc = getattr(cache, 'visible_classes_set', None) or _get_shading_visibility(app)
    
    if changed_mask is None or not np.any(changed_mask):
        return _update_colors_gpu_fast(app, cache, changed_mask=None)
    
    cls = app.data.get("classification")
    xyz = app.data.get("xyz")
    if cls is None or xyz is None:
        return False
    # Classification is already integer LAS data.  Do not copy 34M entries on
    # every Ctrl+Z merely to inspect the changed subset.
    cls = np.asarray(cls)
    ci = np.flatnonzero(changed_mask)
    if len(ci) == 0:
        return True
    
    va = np.array(sorted(vc), dtype=np.int32) if vc else np.array([], dtype=np.int32)
    nv = np.isin(cls[ci], va) if len(va) > 0 else np.zeros(len(ci), dtype=bool)

    # Multi-class topology-stable undo must mirror the classification fast
    # path.  Do this BEFORE np.unique(cache.faces.ravel()), which is catastrophic
    # on a 68M-face mesh and is unnecessary when both old/new classes are visible.
    if not isc or sci is None:
        try:
            if np.all(nv) and _check_previous_classes_visible(app, ci, va):
                if _fast_multiclass_color_overlay(
                    app,
                    cache,
                    changed_mask=changed_mask,
                    changed_indices=ci,
                    visible_classes=vc,
                ):
                    return True
        except Exception as exc:
            print(f"SHADING_MULTI_UNDO_FAST_PATH fallback={exc}")
    
    # Existing topology-aware path remains unchanged for visibility membership
    # changes (visible<->hidden classes).  Bake any earlier sparse color overlay
    # before that path changes topology.
    if (not isc or sci is None) and getattr(cache, '_multiclass_dirty_faces', None):
        _bake_multiclass_color_overlay_into_base(app, cache, vc)
    g2u = cache.build_global_to_unique(len(xyz))
    cu = g2u[ci]
    ic = cu >= 0
    
    # Check which unique vertices are actually in mesh faces
    uv = np.zeros(len(cache.unique_indices), dtype=bool)
    if cache.faces is not None and len(cache.faces) > 0:
        uv[np.unique(cache.faces.ravel())] = True
    
    # Fast path: all changed points still visible AND all have faces
    if np.all(nv) and _check_previous_classes_visible(app, ci, va):
        # ? FIX: Check for orphaned vertices (in unique_indices but not in any face)
        has_orphaned = False
        vp_check = np.flatnonzero(ic)
        if len(vp_check) > 0:
            has_orphaned = bool(np.any(~uv[cu[vp_check]]))
        if not has_orphaned:
            if _update_colors_gpu_fast(app, cache, changed_mask=changed_mask):
                global _rebuild_timer
                if _rebuild_timer:
                    try: _rebuild_timer.stop()
                    except Exception:
                        pass
                return True
    
    aim = np.zeros(len(ci), dtype=bool)
    vp = np.flatnonzero(ic)
    if len(vp) > 0:
        aim[vp] = uv[cu[vp]]
    
    hm = aim & (~nv)  # Was in mesh, now hidden
    vm = nv & (~aim)  # Now visible, wasn't in mesh
    
    # ? FIX: Only suppress vm for vertices that are genuinely new (not in unique_indices).
    # Orphaned vertices (in unique_indices but not in faces) must NOT be suppressed.
    if np.any(vm) and _check_previous_classes_visible(app, ci, va):
        # Keep vm=True for orphaned vertices (in unique_indices but not in any face)
        _vm_indices = np.flatnonzero(vm)
        _vm_global = ci[_vm_indices]
        _vm_cu = cu[_vm_indices]
        _vm_in_unique = _vm_cu >= 0
        _vm_orphaned = np.zeros(len(_vm_indices), dtype=bool)
        _vm_orphaned[_vm_in_unique] = ~uv[_vm_cu[_vm_in_unique]]
        # Only suppress non-orphaned entries
        _suppress = ~_vm_orphaned
        vm[_vm_indices[_suppress]] = False
    
    pbh = ci[hm]
    pbv = ci[vm]
    
    if len(pbh) == 0 and len(pbv) == 0:
        return _update_colors_gpu_fast(app, cache, changed_mask=changed_mask) or False
    
    # Multi-class path
    if not isc or sci is None:
        if len(pbv) > 0:
            return _multi_class_region_undo_patch(app, changed_mask, vc)
        if len(pbh) > 0:
            return _incremental_visibility_patch(app, pbh, vc)
        return True
    
    # Single-class optimized path
    if len(pbv) > 0 and len(pbh) > 0:
        # Both add and remove - use optimized rebuild
        _rebuild_single_class_for_undo(app, sci, changed_mask)
        return True
    if len(pbv) > 0:
        _rebuild_single_class_for_undo(app, sci, changed_mask)
        return True
    if len(pbh) > 0:
        _rebuild_single_class(app, sci)
        return True
    return True

def _affected_faceted_faces(cache, changed_mask, total_points, changed_indices=None):
    """Return representative vertices and their exact incident facets.

    First touch of new vertices uses the existing Numba full-face scan, which is
    exact and already costs only ~35-55 ms even on a 68M-face mesh.  The result
    is then cached sparsely per edited representative vertex.  Undo/re-edit of
    those same points no longer scans the complete mesh.
    """
    if changed_indices is None:
        changed_indices = np.flatnonzero(changed_mask)
    else:
        changed_indices = np.asarray(changed_indices, dtype=np.int64).ravel()
    if changed_indices.size == 0:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)

    g2u = cache.build_global_to_unique(total_points)
    changed_unique = g2u[changed_indices]
    changed_unique = np.unique(
        changed_unique[
            (changed_unique >= 0)
            & (changed_unique < len(cache.unique_indices))
        ]
    )
    if changed_unique.size == 0:
        return changed_unique, np.empty(0, dtype=np.int64)

    faces_id = id(cache.faces)
    incident_cache = getattr(cache, '_incident_face_cache', None)
    if not isinstance(incident_cache, dict):
        incident_cache = {}
        cache._incident_face_cache = incident_cache
    if getattr(cache, '_incident_face_cache_faces_id', None) != faces_id:
        incident_cache.clear()
        cache._incident_face_cache_faces_id = faces_id

    cached_parts = []
    missing = []
    for vertex_id in changed_unique.tolist():
        hit = incident_cache.get(int(vertex_id))
        if hit is None:
            missing.append(int(vertex_id))
        elif len(hit):
            cached_parts.append(np.asarray(hit, dtype=np.int64))

    # Pure cache hit: no 68M-face scan at all.  This is particularly important
    # for Ctrl+Z, which edits exactly the same representatives in reverse.
    if not missing:
        if not cached_parts:
            return changed_unique, np.empty(0, dtype=np.int64)
        affected_faces = np.unique(np.concatenate(cached_parts))
        print(
            'SHADING_FACE_LOOKUP cache=hit '
            f'vertices={len(changed_unique)} faces={len(affected_faces)}'
        )
        return changed_unique, affected_faces

    missing_arr = np.asarray(missing, dtype=np.int64)
    changed_vertex = getattr(cache, '_changed_vertex_scratch', None)
    if (
        changed_vertex is None
        or changed_vertex.dtype != np.bool_
        or len(changed_vertex) != len(cache.unique_indices)
    ):
        changed_vertex = np.zeros(len(cache.unique_indices), dtype=np.bool_)
        cache._changed_vertex_scratch = changed_vertex

    changed_vertex[missing_arr] = True
    try:
        if HAS_NUMBA:
            affected_mask = getattr(cache, '_affected_face_scratch', None)
            if (
                affected_mask is None
                or affected_mask.dtype != np.bool_
                or len(affected_mask) != len(cache.faces)
            ):
                affected_mask = np.empty(len(cache.faces), dtype=np.bool_)
                cache._affected_face_scratch = affected_mask
            _numba_fill_affected_face_mask(
                cache.faces, changed_vertex, affected_mask
            )
            newly_affected = np.flatnonzero(affected_mask)
        else:
            newly_affected = np.flatnonzero(
                changed_vertex[cache.faces[:, 0]]
                | changed_vertex[cache.faces[:, 1]]
                | changed_vertex[cache.faces[:, 2]]
            )
    finally:
        changed_vertex[missing_arr] = False

    # Cache only normal interactive-sized batches.  For huge bulk edits, the
    # Python dict bookkeeping would cost more than it saves; exact behaviour is
    # unchanged because the scan result is still returned.
    if (
        len(missing_arr) <= _INCIDENT_FACE_CACHE_MAX_NEW_VERTICES
        and len(newly_affected) > 0
    ):
        local_faces = cache.faces[newly_affected]
        for vertex_id in missing_arr.tolist():
            member = np.any(local_faces == int(vertex_id), axis=1)
            incident_cache[int(vertex_id)] = np.asarray(
                newly_affected[member], dtype=np.int32
            )

    if cached_parts:
        affected_faces = np.unique(
            np.concatenate(cached_parts + [np.asarray(newly_affected, dtype=np.int64)])
        )
    else:
        affected_faces = np.asarray(newly_affected, dtype=np.int64)

    return changed_unique, affected_faces


def _bake_multiclass_color_overlay_into_base(app, cache, visible_classes=None):
    """Commit sparse overlay colors before a topology-changing edit.

    Slow blended shading commits only the touched VERTEX colors.  Legacy
    Fast/Normal shading commits the touched CELL colors exactly as before.
    """
    dirty = getattr(cache, '_multiclass_dirty_faces', None)
    if not dirty:
        _remove_multiclass_color_overlay(app, cache=cache, clear_dirty=True)
        return True

    mesh = getattr(app, '_shaded_mesh_polydata', None)
    if mesh is None or cache.faces is None or cache.unique_indices is None:
        return False

    data = getattr(app, 'data', None)
    classes = data.get('classification') if isinstance(data, dict) else None
    if classes is None:
        return False
    classes = np.asarray(classes)

    dirty_faces = np.fromiter(dirty, dtype=np.int64, count=len(dirty))
    dirty_faces = dirty_faces[(dirty_faces >= 0) & (dirty_faces < len(cache.faces))]
    if dirty_faces.size == 0:
        _remove_multiclass_color_overlay(app, cache=cache, clear_dirty=True)
        return True

    vc = visible_classes if visible_classes is not None else _get_shading_visibility(app)
    vc = set(int(c) for c in (vc or ()))

    if bool(getattr(app, '_shading_crisp_hybrid_active', False)):
        cell_colors = mesh.GetCellData().GetScalars()
        if cell_colors is None or cell_colors.GetNumberOfTuples() != len(cache.faces):
            return False
        cm = classes[cache.unique_indices]
        rgb = _build_crisp_base_face_colors(
            app, cache, cm, vc, face_ids=dirty_faces
        )
        numpy_support.vtk_to_numpy(cell_colors)[dirty_faces] = rgb
        cell_colors.Modified(); mesh.GetCellData().Modified(); mesh.Modified()
        actor = getattr(app, '_shaded_mesh_actor', None)
        if actor is not None and actor.GetMapper() is not None:
            actor.GetMapper().Modified()
        # The topology-aware operation that follows will rebuild presentation.
        # Drop the old static/dynamic boundary overlays rather than exposing stale
        # class colours during that short transition.
        _remove_static_multiclass_blend_overlays(app)
        print(
            'SHADING_CRISP_BLEND_BAKE '
            f'faces={len(dirty_faces)} reason=topology_membership_change'
        )
        _remove_multiclass_color_overlay(app, cache=cache, clear_dirty=True)
        return True

    if _microstation_color_blend_enabled(app, cache):
        point_colors = mesh.GetPointData().GetScalars()
        if point_colors is None or point_colors.GetNumberOfTuples() != len(cache.unique_indices):
            return False
        dirty_vertices = np.unique(cache.faces[dirty_faces].ravel()).astype(np.int64, copy=False)
        vertex_classes = classes[cache.unique_indices[dirty_vertices]]
        rgb = _shading_palette_rgb(app, vertex_classes, vc)
        numpy_support.vtk_to_numpy(point_colors)[dirty_vertices] = rgb
        point_colors.Modified()
        mesh.GetPointData().Modified(); mesh.Modified()
        actor = getattr(app, '_shaded_mesh_actor', None)
        if actor is not None and actor.GetMapper() is not None:
            actor.GetMapper().Modified()
            _configure_microstation_color_blend_lighting(app, actor)
        print(
            'SHADING_MULTI_BLEND_BAKE '
            f'faces={len(dirty_faces)} vertices={len(dirty_vertices)} '
            'reason=topology_membership_change'
        )
        _remove_multiclass_color_overlay(app, cache=cache, clear_dirty=True)
        return True

    cell_colors = mesh.GetCellData().GetScalars()
    if cell_colors is None or cell_colors.GetNumberOfTuples() != len(cache.faces):
        return False

    base_faces = cache.faces[dirty_faces]
    fvc = classes[cache.unique_indices[base_faces]]
    face_classes = _face_class_ids_sparse_shading(
        app, cache, fvc, base_faces
    )

    palette_codes = [int(c) for c in getattr(app, 'class_palette', {}).keys()]
    local_max = int(face_classes.max()) if face_classes.size else 0
    mc = max([255, local_max] + palette_codes) + 1
    lut = np.zeros((mc, 3), dtype=np.float32)
    for code, entry in getattr(app, 'class_palette', {}).items():
        ci = int(code)
        if 0 <= ci < mc and ci in vc:
            lut[ci] = entry.get('color', (128, 128, 128))

    shade = (
        cache.shade[dirty_faces]
        if cache.shade is not None and len(cache.shade) == len(cache.faces)
        else np.ones(len(dirty_faces), dtype=np.float32)
    )
    colors_np = numpy_support.vtk_to_numpy(cell_colors)
    colors_np[dirty_faces] = np.clip(
        lut[np.clip(face_classes, 0, mc - 1)] * shade[:, None], 0, 255
    ).astype(np.uint8)

    cell_colors.Modified()
    mesh.GetCellData().Modified()
    mesh.Modified()
    actor = getattr(app, '_shaded_mesh_actor', None)
    if actor is not None and actor.GetMapper() is not None:
        actor.GetMapper().Modified()

    print(
        'SHADING_MULTI_COLOR_BAKE '
        f'faces={len(dirty_faces)} reason=topology_membership_change'
    )
    _remove_multiclass_color_overlay(app, cache=cache, clear_dirty=True)
    return True

def _fast_multiclass_color_overlay(
        app, cache, changed_mask=None, changed_indices=None,
        visible_classes=None, schedule_render=True):
    """Refresh topology-stable multi-class shading without touching base RGB.

    Slow multi-class uses the same per-vertex RGB interpolation as the base
    mesh.  Fast/Normal retain the established exact-face cell-color overlay.
    In both cases the huge base geometry remains immutable during classification.
    """
    t0 = time.perf_counter()
    if getattr(cache, 'n_visible_classes', 0) <= 1:
        return False
    if (
        cache.faces is None
        or cache.xyz_final is None
        or cache.unique_indices is None
    ):
        return False

    blend_mode = _microstation_color_blend_enabled(app, cache)
    if not blend_mode and cache.shade is None:
        return False

    data = getattr(app, 'data', None)
    if not isinstance(data, dict):
        return False
    classes = data.get('classification')
    xyz_raw = data.get('xyz')
    if classes is None or xyz_raw is None:
        return False
    classes = np.asarray(classes)

    lookup_t0 = time.perf_counter()
    changed_unique, affected_faces = _affected_faceted_faces(
        cache,
        changed_mask,
        len(xyz_raw),
        changed_indices=changed_indices,
    )
    lookup_ms = (time.perf_counter() - lookup_t0) * 1000.0
    if changed_unique.size == 0 or affected_faces.size == 0:
        return True

    faces_id = id(cache.faces)
    dirty = getattr(cache, '_multiclass_dirty_faces', None)
    if not isinstance(dirty, set):
        dirty = set()
        cache._multiclass_dirty_faces = dirty
    if getattr(cache, '_multiclass_dirty_faces_faces_id', None) != faces_id:
        dirty.clear()
        cache._multiclass_dirty_faces_faces_id = faces_id

    dirty.update(int(v) for v in np.asarray(affected_faces, dtype=np.int64))
    if not dirty:
        return True

    dirty_faces = np.fromiter(dirty, dtype=np.int64, count=len(dirty))
    dirty_faces.sort()

    base_faces = np.asarray(cache.faces[dirty_faces], dtype=np.int32)
    flat_vertices = base_faces.reshape(-1)
    local_vertex_ids, inverse = np.unique(flat_vertices, return_inverse=True)
    local_faces = inverse.reshape(-1, 3).astype(np.int32, copy=False)
    local_xyz = np.ascontiguousarray(cache.xyz_final[local_vertex_ids])

    vc = visible_classes if visible_classes is not None else _get_shading_visibility(app)
    vc = set(int(c) for c in (vc or ()))

    # CRITICAL PRESENTATION MATCH:
    # The full crisp-hybrid mesh is NOT one globally GPU-lit vertex-colour
    # surface. Pure faces are baked CELL-shaded facets while only mixed faces
    # use the barycentric GPU blend layer. Classification/Undo/Redo must use
    # that exact same split, otherwise the touched region becomes visibly
    # smoother than the surrounding TIN even though topology is unchanged.
    if blend_mode and bool(getattr(app, '_shading_crisp_hybrid_active', False)):
        pure_count, mixed_count, local_count = _rebuild_crisp_live_edit_overlays(
            app, cache, classes, vc, dirty_faces
        )
        total_ms = (time.perf_counter() - t0) * 1000.0
        print(
            'SHADING_MULTI_BLEND_OVERLAY '
            f'changed_vertices={len(changed_unique)} '
            f'new_faces={len(affected_faces)} dirty_faces={len(dirty_faces)} '
            f'pure_faces={pure_count} mixed_faces={mixed_count} '
            f'local_vertices={local_count} lookup={lookup_ms:.1f}ms '
            f'total={total_ms:.1f}ms base_rgb_untouched=True '
            'presentation=crisp_hybrid_match'
        )
        if schedule_render:
            app._shading_present_reason = 'multiclass_crisp_edit_overlay'
            _schedule_fast_shaded_present(
                app, delay_ms=0, restart=True, wait_while_preview=True
            )
        return True

    packed = np.empty(len(local_faces) * 4, dtype=np.int32)
    packed[0::4] = 3
    packed[1::4] = local_faces[:, 0]
    packed[2::4] = local_faces[:, 1]
    packed[3::4] = local_faces[:, 2]
    patch = pv.PolyData(local_xyz, packed)

    if blend_mode:
        # Keep the canonical class RGB on each local vertex.  The GPU blends
        # those colors inside each triangle and applies one flat slope light.
        local_classes = classes[cache.unique_indices[local_vertex_ids]]
        rgb = _shading_palette_rgb(app, local_classes, vc)
        patch.point_data['RGB'] = rgb
        _dynamic_normal_buf = None
        if cache.face_normals is not None and len(cache.face_normals) == len(cache.faces):
            _dynamic_normal_buf = _attach_explicit_cell_normals(
                patch, cache.face_normals[dirty_faces]
            )
        preference = 'point'
        lighting = True
    else:
        global_face_indices = cache.unique_indices[base_faces]
        fvc = classes[global_face_indices]
        face_classes = _face_class_ids_sparse_shading(
            app, cache, fvc, base_faces
        )

        palette_codes = [int(c) for c in getattr(app, 'class_palette', {}).keys()]
        local_max = int(face_classes.max()) if face_classes.size else 0
        mc = max([255, local_max] + palette_codes) + 1
        lut = np.zeros((mc, 3), dtype=np.float32)
        for code, entry in getattr(app, 'class_palette', {}).items():
            code_int = int(code)
            if 0 <= code_int < mc and code_int in vc:
                lut[code_int] = entry.get('color', (128, 128, 128))

        local_shade = np.asarray(cache.shade[dirty_faces], dtype=np.float32)
        rgb = np.clip(
            lut[np.clip(face_classes, 0, mc - 1)] * local_shade[:, None],
            0,
            255,
        ).astype(np.uint8)
        patch.cell_data['RGB'] = rgb
        preference = 'cell'
        lighting = False

    plotter = getattr(app, 'vtk_widget', None)
    if plotter is None:
        return False
    try:
        plotter.remove_actor(_MULTICLASS_COLOR_OVERLAY_NAME, render=False)
    except Exception:
        pass

    actor = plotter.add_mesh(
        patch,
        scalars='RGB',
        rgb=True,
        show_edges=False,
        lighting=lighting,
        smooth_shading=False,
        preference=preference,
        name=_MULTICLASS_COLOR_OVERLAY_NAME,
        render=False,
    )
    if actor is None:
        return False

    try:
        setattr(actor, '_is_shading_mesh', True)
        setattr(actor, '_is_shading_color_overlay', True)
        actor.PickableOff()
        prop = actor.GetProperty()
        if blend_mode:
            _configure_microstation_color_blend_lighting(app, actor)
        else:
            prop.SetLighting(False)
            prop.SetInterpolationToFlat()
            prop.SetAmbient(1.0)
            prop.SetDiffuse(0.0)
            prop.SetSpecular(0.0)
            prop.EdgeVisibilityOff()
        mapper = actor.GetMapper()
        if mapper is not None:
            mapper.StaticOn()
            mapper.SetResolveCoincidentTopologyToPolygonOffset()
            mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(-8.0, -8.0)
            mapper.InterpolateScalarsBeforeMappingOff()
    except Exception:
        pass

    app._shading_multiclass_color_overlay_actor = actor
    if blend_mode:
        try:
            actor._naksha_crisp_dynamic_normal_buffer = _dynamic_normal_buf
        except Exception:
            pass

        # Keep classification/undo/redo visually identical to the full render:
        # dirty mixed faces receive the same small crisp apex core.
        dynamic_normals = (
            cache.face_normals[dirty_faces]
            if cache.face_normals is not None and len(cache.face_normals) == len(cache.faces)
            else None
        )
        tip_actor, tip_buffers, tip_count = _build_sharp_multiclass_tip_actor(
            app, cache, name=_MULTICLASS_LIVE_TIP_NAME,
            xyz=local_xyz, faces=local_faces, vertex_classes=local_classes,
            visible_classes=vc, face_normals=dynamic_normals, live=True,
        )
        app._shading_multiclass_tip_actor = tip_actor
        if tip_actor is not None:
            try:
                tip_actor._naksha_sharp_tip_buffers = tip_buffers
            except Exception:
                pass
            if tip_count:
                print(
                    'SHADING_SHARP_TIPS status=enabled kind=live '
                    f'faces={tip_count} dirty_faces={len(dirty_faces)}'
                )
    if not blend_mode:
        try:
            _update_live_class_detail_overlay(
                app, cache, dirty_faces, classes, visible_classes=vc
            )
        except Exception as _detail_exc:
            print(f"SHADING_CLASS_DETAIL live_update_fallback={_detail_exc}")

    total_ms = (time.perf_counter() - t0) * 1000.0
    if blend_mode:
        print(
            'SHADING_MULTI_BLEND_OVERLAY '
            f'changed_vertices={len(changed_unique)} '
            f'new_faces={len(affected_faces)} dirty_faces={len(dirty_faces)} '
            f'local_vertices={len(local_vertex_ids)} lookup={lookup_ms:.1f}ms '
            f'total={total_ms:.1f}ms base_rgb_untouched=True'
        )
    else:
        print(
            'SHADING_MULTI_COLOR_OVERLAY '
            f'changed_vertices={len(changed_unique)} '
            f'new_faces={len(affected_faces)} dirty_faces={len(dirty_faces)} '
            f'local_vertices={len(local_vertex_ids)} lookup={lookup_ms:.1f}ms '
            f'total={total_ms:.1f}ms base_rgb_untouched=True'
        )

    if schedule_render:
        app._shading_present_reason = (
            'multiclass_blend_overlay' if blend_mode else 'multiclass_color_overlay'
        )
        _schedule_fast_shaded_present(
            app, delay_ms=0, restart=True, wait_while_preview=True
        )
    return True

def _update_colors_gpu_fast(
        app, cache, changed_mask=None, _visible_classes=None, _defer_render=False,
        _changed_indices=None):
    """Patch shaded RGB without rebuilding geometry.

    For local classification edits, only the affected triangle vertices are
    mapped back to the canonical LAS classification array. The old path copied
    the complete classification array and materialized classes for every
    representative vertex even when only a few hundred points changed.
    """
    try:
        t0 = time.perf_counter()
        mesh = getattr(app, "_shaded_mesh_polydata", None)
        if mesh is None:
            return False

        vc = _visible_classes or _get_shading_visibility(app)
        data = getattr(app, "data", None)
        cls_raw = data.get("classification") if isinstance(data, dict) else None
        xyz_raw = data.get("xyz") if isinstance(data, dict) else None

        # Slow multi-class base meshes use POINT RGB.  Normal classification
        # normally takes the tiny blend-overlay path above; this branch is the
        # safe direct fallback and also handles full palette recolors.
        if (
            _microstation_color_blend_enabled(app, cache)
            and not bool(getattr(app, '_shading_crisp_hybrid_active', False))
            and cls_raw is not None
            and cache.unique_indices is not None
        ):
            point_colors = mesh.GetPointData().GetScalars()
            if point_colors is not None and point_colors.GetNumberOfTuples() == len(cache.unique_indices):
                classes = np.asarray(cls_raw)
                vp = numpy_support.vtk_to_numpy(point_colors)
                changed_unique = np.empty(0, dtype=np.int64)
                if changed_mask is not None and np.any(changed_mask) and xyz_raw is not None:
                    if _changed_indices is not None:
                        source_idx = np.asarray(_changed_indices, dtype=np.int64).ravel()
                    else:
                        source_idx = np.flatnonzero(changed_mask).astype(np.int64, copy=False)
                    g2u = cache.build_global_to_unique(len(xyz_raw))
                    changed_unique = g2u[source_idx]
                    changed_unique = np.unique(changed_unique[changed_unique >= 0]).astype(np.int64, copy=False)
                    if changed_unique.size == 0:
                        return True
                    current_classes = classes[cache.unique_indices[changed_unique]]
                    vp[changed_unique] = _shading_palette_rgb(app, current_classes, vc)
                else:
                    _remove_multiclass_color_overlay(app, cache=cache, clear_dirty=True)
                    current_classes = classes[cache.unique_indices]
                    vp[:] = _shading_palette_rgb(app, current_classes, vc)

                point_colors.Modified(); mesh.GetPointData().Modified(); mesh.Modified()
                actor = getattr(app, '_shaded_mesh_actor', None)
                if actor is not None:
                    mapper = actor.GetMapper()
                    if mapper is not None:
                        mapper.Modified()
                    _configure_microstation_color_blend_lighting(app, actor)

                total_ms = (time.perf_counter() - t0) * 1000.0
                print(
                    'SHADING_VERTEX_COLOR_PATCH '
                    f'changed_vertices={len(changed_unique)} total_vertices={len(cache.unique_indices)} '
                    f'total={total_ms:.1f}ms blend=1'
                )
                if _defer_render:
                    _schedule_fast_shaded_present(app, delay_ms=0)
                else:
                    app.vtk_widget.render()
                return True

        cell_colors = mesh.GetCellData().GetScalars()
        if (
            cell_colors is not None
            and cell_colors.GetNumberOfTuples() == len(cache.faces)
            and cls_raw is not None
            and cache.unique_indices is not None
        ):
            classes = np.asarray(cls_raw)
            colors_np = numpy_support.vtk_to_numpy(cell_colors)
            shade = (
                cache.shade
                if cache.shade is not None and len(cache.shade) == len(cache.faces)
                else np.ones(len(cache.faces), dtype=np.float32)
            )

            affected_faces = None
            changed_unique = np.empty(0, dtype=np.int64)
            lookup_ms = 0.0

            if changed_mask is not None and np.any(changed_mask) and xyz_raw is not None:
                ta = time.perf_counter()
                changed_unique, affected_faces = _affected_faceted_faces(
                    cache, changed_mask, len(xyz_raw), changed_indices=_changed_indices
                )
                lookup_ms = (time.perf_counter() - ta) * 1000.0
                if changed_unique.size == 0 or affected_faces.size == 0:
                    return True

            palette_codes = []
            try:
                palette_codes = [int(c) for c in app.class_palette.keys()]
            except Exception:
                palette_codes = []
            palette_max = max([255] + palette_codes)

            if affected_faces is not None:
                # True local path: gather classes only for vertices belonging to
                # the affected triangles. No O(all-points) class materialization.
                target_faces = cache.faces[affected_faces]
                global_face_indices = cache.unique_indices[target_faces]
                fvc = classes[global_face_indices]
                face_classes = _face_class_ids_sparse_shading(
                    app, cache, fvc, target_faces
                )

                local_max = int(face_classes.max()) if face_classes.size else 0
                mc = max(palette_max, local_max) + 1
                lut = np.zeros((mc, 3), dtype=np.float32)
                for code, entry in app.class_palette.items():
                    code_int = int(code)
                    if 0 <= code_int < mc and code_int in vc:
                        lut[code_int] = entry.get("color", (128, 128, 128))

                target_shade = (
                    _crisp_shade_chunk(app, cache, affected_faces)
                    if bool(getattr(app, '_shading_crisp_hybrid_active', False))
                    else shade[affected_faces]
                )
                colors_np[affected_faces] = np.clip(
                    lut[np.clip(face_classes, 0, mc - 1)]
                    * target_shade[:, None],
                    0,
                    255,
                ).astype(np.uint8)

                cell_colors.Modified()
                mesh.GetCellData().Modified()
                mesh.Modified()
                actor = getattr(app, "_shaded_mesh_actor", None)
                if actor is not None:
                    mapper = actor.GetMapper()
                    if mapper is not None:
                        mapper.Modified()

                total_ms = (time.perf_counter() - t0) * 1000.0
                print(
                    "SHADING_LOCAL_COLOR_PATCH "
                    f"changed_vertices={len(changed_unique)} "
                    f"affected_faces={len(affected_faces)} "
                    f"total_faces={len(cache.faces)} "
                    f"lookup={lookup_ms:.1f}ms total={total_ms:.1f}ms"
                )

                if _defer_render:
                    _schedule_fast_shaded_present(app, delay_ms=0)
                else:
                    app.vtk_widget.render()
                return True

            # Full recolor remains available for palette/visibility changes.
            # It bakes current canonical classes into the base RGB buffer, so
            # the sparse live overlay is no longer needed.
            _remove_multiclass_color_overlay(app, cache=cache, clear_dirty=True)
            _remove_shading_class_detail_overlays(app, remove_static=False, remove_live=True)
            cm = classes[cache.unique_indices]
            class_max = int(cm.max()) if cm.size else 0
            mc = max(palette_max, class_max) + 1
            lut = np.zeros((mc, 3), dtype=np.float32)
            for code, entry in app.class_palette.items():
                code_int = int(code)
                if 0 <= code_int < mc and code_int in vc:
                    lut[code_int] = entry.get("color", (128, 128, 128))

            if bool(getattr(app, '_shading_crisp_hybrid_active', False)):
                colors_np[:] = _build_crisp_base_face_colors(app, cache, cm, vc)
            else:
                face_classes = _face_class_ids_shading(app, cache, cm, cache.faces)
                colors_np[:] = np.clip(
                    lut[np.clip(face_classes, 0, mc - 1)] * shade[:, None],
                    0,
                    255,
                ).astype(np.uint8)

            cell_colors.Modified()
            mesh.GetCellData().Modified()
            mesh.Modified()
            actor = getattr(app, "_shaded_mesh_actor", None)
            if actor is not None:
                mapper = actor.GetMapper()
                if mapper is not None:
                    mapper.Modified()

            if bool(getattr(app, '_shading_crisp_hybrid_active', False)):
                mixed_faces, mixed_count, crisp_ok = _collect_mixed_display_faces(
                    app, cache, cm, vc
                )
                if crisp_ok:
                    _build_static_multiclass_blend_overlays(
                        app, cache, cm, vc, mixed_faces
                    )
                else:
                    _remove_static_multiclass_blend_overlays(app)
                    print(
                        'SHADING_CRISP_FACETS status=palette_recolor_fallback '
                        f'candidate_mixed_faces={mixed_count:,}'
                    )

            if _defer_render:
                _schedule_fast_shaded_present(app, delay_ms=0)
            else:
                app.vtk_widget.render()
            return True

        # Compatibility fallback for a legacy smooth/point-colored shaded mesh.
        pc = mesh.GetPointData().GetScalars()
        if pc is None or pc.GetNumberOfTuples() != len(cache.unique_indices):
            return False
        if cls_raw is None or xyz_raw is None:
            return False

        vp = numpy_support.vtk_to_numpy(pc)
        classes = np.asarray(cls_raw)
        cm = classes[cache.unique_indices]
        nv2 = len(cm)
        sh = (
            cache.vertex_shade
            if cache.vertex_shade is not None and len(cache.vertex_shade) == nv2
            else np.ones(nv2, dtype=np.float32)
        )
        class_max = int(cm.max()) if cm.size else 0
        palette_max = max(
            [255] + [int(c) for c in getattr(app, "class_palette", {}).keys()]
        )
        mc = max(class_max, palette_max) + 1
        lut = np.zeros((mc, 3), dtype=np.float32)
        for c, e in app.class_palette.items():
            ci = int(c)
            if 0 <= ci < mc and ci in vc:
                lut[ci] = e.get("color", (128, 128, 128))
        vcl = np.clip(cm.astype(np.int64, copy=False), 0, mc - 1)

        if changed_mask is not None and np.any(changed_mask):
            g2u = cache.build_global_to_unique(len(xyz_raw))
            cu = g2u[np.flatnonzero(changed_mask)]
            cu = cu[(cu >= 0) & (cu < nv2)]
            if len(cu) > 0:
                vp[cu] = np.clip(
                    lut[vcl[cu]] * sh[cu, None], 0, 255
                ).astype(np.uint8)
                pc.Modified()
                mesh.Modified()
                actor = getattr(app, "_shaded_mesh_actor", None)
                if actor is not None and actor.GetMapper() is not None:
                    actor.GetMapper().Modified()
                if _defer_render or len(cu) < 500:
                    _schedule_fast_shaded_present(app, delay_ms=0)
                else:
                    app.vtk_widget.render()
                return True

        vp[:] = np.clip(lut[vcl] * sh[:, None], 0, 255).astype(np.uint8)
        pc.Modified()
        mesh.Modified()
        actor = getattr(app, "_shaded_mesh_actor", None)
        if actor is not None and actor.GetMapper() is not None:
            actor.GetMapper().Modified()
        if _defer_render:
            _schedule_fast_shaded_present(app, delay_ms=0)
        else:
            app.vtk_widget.render()
        return True
    except Exception as exc:
        print(f"SHADING_LOCAL_COLOR_PATCH fallback_error={exc}")
        return False

def _rebuild_single_class_for_undo(app, sci, changed_mask):
    """Optimized single-class undo - only update affected region faces."""
    cache = get_cache()
    if cache.faces is None or cache.xyz_unique is None:
        _do_full_rebuild(app, sci); return

    data = getattr(app, "data", None)
    if not isinstance(data, dict):
        return
    cls_raw = data.get("classification")
    xyz = data.get("xyz")
    if cls_raw is None or xyz is None:
        return
    cls = cls_raw.astype(np.int32)

    ci = np.flatnonzero(changed_mask)
    rgi = ci[cls[ci] == sci]
    if len(rgi) == 0: return

    # ? FIX: Use cache values first for shading parameters
    az_ = cache.last_azimuth if cache.last_azimuth >= 0 else getattr(app, 'last_shade_azimuth', 45.)
    an_ = cache.last_angle if cache.last_angle >= 0 else getattr(app, 'last_shade_angle', 45.)
    am_ = cache.last_ambient if cache.last_ambient >= 0 else getattr(app, 'shade_ambient', .25)

    rx = xyz[rgi]
    xn, yn = rx[:,0].min(), rx[:,1].min()
    xx, yx = rx[:,0].max(), rx[:,1].max()
    mg = cache.spacing * 5 if cache.spacing > 0 else 1000.
    xn -= mg; yn -= mg; xx += mg; yx += mg

    avi = np.flatnonzero(cls == sci)
    avx = xyz[avi]
    ir = (avx[:,0] >= xn) & (avx[:,0] <= xx) & (avx[:,1] >= yn) & (avx[:,1] <= yx)
    lgi = avi[ir]; lx = avx[ir]
    if len(lx) < 3: return

    cf = cache.xyz_final
    irm = (cf[:,0] >= xn) & (cf[:,0] <= xx) & (cf[:,1] >= yn) & (cf[:,1] <= yx)

    # Step 1: identify stale unique vertices
    region_ui = np.flatnonzero(irm)
    stale_ui_set = np.array([], dtype=np.int32)
    if len(region_ui) > 0:
        region_gi = cache.unique_indices[region_ui]
        stale_mask = cls[region_gi] != sci
        if np.any(stale_mask):
            stale_ui_set = region_ui[stale_mask]

    # Step 2: identify faces to remove
    all_inside = irm[cache.faces[:,0]] & irm[cache.faces[:,1]] & irm[cache.faces[:,2]]
    if len(stale_ui_set) > 0:
        has_stale = np.zeros(len(cache.unique_indices), dtype=bool)
        has_stale[stale_ui_set] = True
        touches_stale = (has_stale[cache.faces[:,0]] |
                         has_stale[cache.faces[:,1]] |
                         has_stale[cache.faces[:,2]])
        fir = all_inside | touches_stale
    else:
        fir = all_inside

    n_removed = int(np.sum(fir))
    fo = cache.faces[~fir]
    no = cache.face_normals[~fir] if cache.face_normals is not None else None
    so = cache.shade[~fir] if cache.shade is not None and len(cache.shade) == len(cache.faces) else None

    # Step 3: purge stale unique vertices and remap
    if len(stale_ui_set) > 0:
        keep_ui = np.ones(len(cache.unique_indices), dtype=bool)
        keep_ui[stale_ui_set] = False
        remap = np.full(len(cache.unique_indices), -1, dtype=np.int32)
        remap[keep_ui] = np.arange(int(keep_ui.sum()), dtype=np.int32)
        if len(fo) > 0:
            fo_flat = remap[fo.ravel()]
            if np.any(fo_flat < 0):
                fo_mask = (fo_flat.reshape(-1,3) >= 0).all(axis=1)
                fo = fo_flat.reshape(-1,3)[fo_mask]
                if no is not None: no = no[fo_mask]
                if so is not None: so = so[fo_mask]
            else:
                fo = fo_flat.reshape(-1, 3)
        cache.unique_indices = cache.unique_indices[keep_ui]
        cache.xyz_unique = cache.xyz_unique[keep_ui]
        cache.xyz_final = cache.xyz_final[keep_ui]
        cache._global_to_unique = None

    # Triangulate new region - use faster parameters for small regions
    lo = lx.min(axis=0); lxo = lx - lo
    ns = np.sqrt(max((lxo[:,0].max()-lxo[:,0].min())*(lxo[:,1].max()-lxo[:,1].min()), 1.) / max(len(lx), 1))
    
    # ? OPTIMIZATION: Coarser precision for faster triangulation during undo
    pr = max(ns * 0.5, 0.01)  # Coarser than normal
    pu = len(lx) / max((pr/max(ns, 1e-9))**2, 1)
    if pu > 50000: pr = max(pr * np.sqrt(pu / 50000), 0.01)  # Lower threshold
    
    xyg = np.floor(lxo[:,:2]/pr).astype(np.int64)
    si = np.lexsort((-lxo[:,2], xyg[:,1], xyg[:,0])); xys = xyg[si]
    d = np.diff(xys, axis=0); um = np.concatenate([[True], (d[:,0]!=0)|(d[:,1]!=0)])
    uli = si[um]; ulx = lxo[uli]; ulg = lgi[uli]
    if len(ulx) < 3:
        cache.faces = fo; cache.face_normals = no; cache.shade = so
        cache._vtk_colors_ptr = None
        _render_mesh_fast_update(app, cache, n_removed, 0)
        return
        
    xy = ulx[:,:2]
    try: lf = _do_triangulate(xy)
    except Exception:
        cache.faces = fo; cache.face_normals = no; cache.shade = so
        cache._vtk_colors_ptr = None
        _render_mesh_fast_update(app, cache, n_removed, 0)
        return
    if len(lf) == 0:
        cache.faces = fo; cache.face_normals = no; cache.shade = so
        cache._vtk_colors_ptr = None
        _render_mesh_fast_update(app, cache, n_removed, 0)
        return

    _mef = getattr(cache, 'max_edge_factor', 3.0)
    ls = np.sqrt((xy[:,0].max()-xy[:,0].min())*(xy[:,1].max()-xy[:,1].min())/len(xy)) if len(xy) > 0 else cache.spacing
    lf = _filter_edges_by_absolute(lf, xy, min(max(ls*8, cache.spacing*_mef*5), cache.spacing*_mef*20))
    if len(lf) == 0:
        cache.faces = fo; cache.face_normals = no; cache.shade = so
        cache._vtk_colors_ptr = None
        _render_mesh_fast_update(app, cache, n_removed, 0)
        return

    g2u = cache.build_global_to_unique(len(xyz)); ltc = g2u[ulg]
    if int(np.sum(ltc < 0)) > 0:
        ngi = ulg[ltc < 0]; nx2 = ulx[ltc < 0] + lo - cache.offset
        cache.unique_indices = np.concatenate([cache.unique_indices, ngi])
        cache.xyz_unique = np.vstack([cache.xyz_unique, nx2])
        cache.xyz_final = np.vstack([cache.xyz_final, nx2+cache.offset])
        cache._global_to_unique = None; g2u = cache.build_global_to_unique(len(xyz)); ltc = g2u[ulg]
    npf = ltc[lf]
    if np.any(npf < 0):
        cache.faces = fo; cache.face_normals = no; cache.shade = so
        cache._vtk_colors_ptr = None
        _render_mesh_fast_update(app, cache, n_removed, 0)
        return

    fn = _compute_face_normals(cache.xyz_unique, npf)

    # Standard MicroStation hillshade for new patch faces
    N = fn.astype(np.float64)
    zenith_rad = np.radians(90.0 - an_)
    az_math_rad = np.radians(360.0 - az_ + 90.0)
    _lx = np.sin(zenith_rad) * np.cos(az_math_rad)
    _ly = np.sin(zenith_rad) * np.sin(az_math_rad)
    _lz = np.cos(zenith_rad)

    NdL = N[:,0]*_lx + N[:,1]*_ly + N[:,2]*_lz
    ni = np.maximum(NdL, 0.0)
    ni = np.maximum(ni, am_)
    nps = np.clip(ni, 0., 1.).astype(np.float32)
    # Combine
    cache.faces = np.vstack([fo, npf])
    cache.face_normals = fn if no is None else np.vstack([no, fn])
    cache.shade = nps if so is None else np.concatenate([so, nps])
    cache._vtk_colors_ptr = None   
    _render_mesh_fast_update(app, cache, n_removed, len(npf))

def _render_mesh_fast_update(app, cache, n_removed, n_added):
    """Update topology without deep-copying the complete multi-million-cell mesh."""
    mesh = getattr(app, '_shaded_mesh_polydata', None)
    actor = getattr(app, '_shaded_mesh_actor', None)
    if mesh is None or actor is None:
        _render_mesh(app, cache, app.data.get("classification"), _save_camera(app))
        return

    try:
        t_prepare = time.perf_counter()
        sci = getattr(cache, 'single_class_id', None)
        if sci is None:
            _render_mesh(app, cache, app.data.get("classification"), _save_camera(app))
            return

        xyz_buf = np.ascontiguousarray(cache.xyz_final)
        pts_array = numpy_support.numpy_to_vtk(xyz_buf, deep=False)
        points = vtk.vtkPoints(); points.SetData(pts_array)
        nf = len(cache.faces)
        # VTK's modern offsets/connectivity representation avoids constructing
        # the old [3,a,b,c,3,...] array and avoids a second deep copy.
        conn_buf = np.ascontiguousarray(cache.faces.reshape(-1), dtype=np.int32)
        offsets_buf = np.arange(0, 3 * (nf + 1), 3, dtype=np.int32)
        vtk_conn = numpy_support.numpy_to_vtk(conn_buf, deep=False)
        vtk_offsets = numpy_support.numpy_to_vtk(offsets_buf, deep=False)
        polys = vtk.vtkCellArray(); polys.SetData(vtk_offsets, vtk_conn)

        rgb = getattr(cache, '_pending_fast_rgb', None)
        if (
            rgb is None
            or np.asarray(rgb).ndim != 2
            or np.asarray(rgb).shape != (nf, 3)
        ):
            sh = cache.shade if cache.shade is not None and len(cache.shade) == nf else np.ones(nf, dtype=np.float32)
            bc = np.array(app.class_palette.get(sci, {}).get("color", (128,128,128)), dtype=np.float32)
            rgb = np.clip(bc * sh[:, None], 0, 255).astype(np.uint8)
        rgb_buf = np.ascontiguousarray(rgb, dtype=np.uint8)
        vtk_rgb = numpy_support.numpy_to_vtk(rgb_buf, deep=False)
        vtk_rgb.SetName("RGB")

        mesh.SetPoints(points)
        mesh.SetPolys(polys)
        mesh.GetCellData().SetScalars(vtk_rgb)
        points.Modified(); polys.Modified()
        mesh.GetPointData().Modified(); mesh.GetCellData().Modified()
        mesh.Modified()
        mapper = actor.GetMapper()
        mapper.SetInputData(mesh); mapper.Modified(); actor.Modified()
        cache._vtk_colors_ptr = None
        cache._pending_fast_rgb = None
        cache._vtk_fast_buffers = (xyz_buf, offsets_buf, conn_buf, rgb_buf)
        prepare_ms = (time.perf_counter() - t_prepare) * 1000.0
        # Do not synchronously render here. Classification commits already
        # perform the authoritative render; brush/undo-only callers receive a
        # single coalesced render on the next Qt turn.
        _schedule_fast_shaded_present(app)
        print(
            "SHADING_VTK_UPDATE "
            f"faces={nf} removed={n_removed} added={n_added} "
            f"prepare={prepare_ms:.1f}ms render=deferred"
        )
    except Exception as exc:
        print(f"SHADING_LOCAL_PATCH inplace_vtk_failed={exc}")
        _render_mesh(app, cache, app.data.get("classification"), _save_camera(app))


def _fast_add_overlay(app, added_global_indices):
    """Display newly-entering single-class points without rewriting base mesh."""
    t0 = time.perf_counter()
    cache = get_cache()
    xyz = app.data.get('xyz')
    if xyz is None or cache.xyz_final is None or cache._xy_tree is None:
        return False
    gi = np.unique(np.asarray(added_global_indices, dtype=np.int64).ravel())
    if gi.size == 0:
        return True
    new_xyz = np.asarray(xyz[gi], dtype=np.float64)
    keep = _grid_dedup_at_precision(new_xyz - cache.offset,
                                    max(cache.spacing * 0.3, 0.005))
    new_xyz = new_xyz[keep]
    if len(new_xyz) < 1:
        return True
    margin = max(cache.spacing * cache.max_edge_factor * 2.0, cache.spacing * 6.0, 0.5)
    lo = new_xyz[:, :2].min(axis=0) - margin
    hi = new_xyz[:, :2].max(axis=0) + margin
    center = (lo + hi) * 0.5
    radius = float(np.linalg.norm(hi - lo) * 0.5)
    tree_center = center - np.asarray(cache.offset[:2], dtype=np.float64)
    candidates = np.asarray(cache._xy_tree.query_ball_point(tree_center, radius), dtype=np.int64)
    if len(candidates):
        cxy = cache.xyz_final[candidates, :2]
        candidates = candidates[((cxy >= lo) & (cxy <= hi)).all(axis=1)]
    # Bound preview work; exact idle consolidation has no such approximation.
    if len(candidates) > 20_000:
        candidates = candidates[:20_000]
    old_xyz = cache.xyz_final[candidates] if len(candidates) else np.empty((0, 3))
    local_xyz = np.vstack((old_xyz, new_xyz))
    if len(local_xyz) < 3:
        return False
    faces = _do_triangulate(local_xyz[:, :2])
    if len(faces) == 0:
        return False
    first_new = len(old_xyz)
    faces = faces[np.any(faces >= first_new, axis=1)]
    faces = _filter_edges_by_absolute(
        faces, local_xyz[:, :2],
        max(cache.spacing * cache.max_edge_factor * 4.0, cache.spacing * 8.0),
    )
    if len(faces) == 0:
        return False
    normals = _compute_face_normals(local_xyz, faces)
    shade = _compute_face_shade_global_z(
        local_xyz, faces,
        getattr(app, 'last_shade_azimuth', 45.),
        getattr(app, 'last_shade_angle', 45.),
        getattr(app, 'shade_ambient', .25), face_normals=normals,
    )
    color = np.asarray(app.class_palette.get(cache.single_class_id, {}).get('color', (128,128,128)), dtype=np.float32)
    rgb = np.clip(color * shade[:, None], 0, 255).astype(np.uint8)
    packed = np.empty((len(faces), 4), dtype=np.int64)
    packed[:, 0] = 3; packed[:, 1:] = faces
    patch = pv.PolyData(local_xyz, packed.ravel())
    patch.cell_data['RGB'] = rgb
    generation = int(getattr(app, '_shading_overlay_generation', 0)) + 1
    app._shading_overlay_generation = generation
    actor = app.vtk_widget.add_mesh(
        patch, scalars='RGB', rgb=True, lighting=False, smooth_shading=False,
        name=f'shaded_mesh_live_add_{generation}', render=False,
    )
    try:
        actor.GetProperty().SetInterpolationToFlat()
        actor.GetMapper().SetResolveCoincidentTopologyToPolygonOffset()
    except Exception:
        pass
    overlays = getattr(app, '_shading_add_overlays', None)
    if not isinstance(overlays, list):
        overlays = []
        app._shading_add_overlays = overlays
    overlays.append({
        'name': f'shaded_mesh_live_add_{generation}',
        'indices': gi.copy(),
    })
    print(f"SHADING_ADD_OVERLAY points={len(new_xyz)} faces={len(faces)} elapsed={(time.perf_counter()-t0)*1000:.1f}ms")
    return True


def _remove_from_add_overlays(app, removed_global_indices):
    """Remove ground points owned by fast-add overlays, rebuilding only survivors."""
    overlays = getattr(app, '_shading_add_overlays', None)
    if not overlays:
        return 0
    removed = np.unique(np.asarray(removed_global_indices, dtype=np.int64).ravel())
    if removed.size == 0:
        return 0
    cls = app.data.get('classification')
    sci = getattr(get_cache(), 'single_class_id', None)
    survivors = []
    rebuild_groups = []
    removed_overlay_points = 0
    for entry in list(overlays):
        owned = np.asarray(entry.get('indices', []), dtype=np.int64).ravel()
        if owned.size == 0 or not np.any(np.isin(owned, removed, assume_unique=False)):
            survivors.append(entry)
            continue
        hit = np.isin(owned, removed, assume_unique=False)
        removed_overlay_points += int(np.count_nonzero(hit))
        try:
            app.vtk_widget.remove_actor(entry.get('name'), render=False)
        except Exception:
            pass
        remaining = owned[~hit]
        if cls is not None and sci is not None and remaining.size:
            remaining = remaining[np.asarray(cls[remaining], dtype=np.int32) == int(sci)]
        if remaining.size:
            rebuild_groups.append(remaining)
    app._shading_add_overlays = survivors
    for remaining in rebuild_groups:
        _fast_add_overlay(app, remaining)
    if removed_overlay_points:
        print(
            "SHADING_OVERLAY_REMOVE "
            f"points={removed_overlay_points} rebuilt_groups={len(rebuild_groups)}"
        )
    return removed_overlay_points


def _clip_fill_faces_to_removed_region(xy, candidate_faces, removed_faces,
                                       base_max_edge):
    """Keep an adaptive local fill inside the exact footprint being replaced."""
    candidates = np.asarray(candidate_faces, dtype=np.int32).reshape(-1, 3)
    removed_faces = np.asarray(removed_faces, dtype=np.int32).reshape(-1, 3)
    if len(candidates) == 0 or len(removed_faces) == 0:
        return np.empty((0, 3), dtype=np.int32)

    domain_vertices = np.unique(removed_faces.ravel())
    domain_xy = np.asarray(xy[domain_vertices], dtype=np.float64)
    extent = np.ptp(domain_xy, axis=0)
    region_diameter = float(np.hypot(extent[0], extent[1]))
    adaptive_max_edge = max(float(base_max_edge), region_diameter * 1.01)
    candidates = _filter_edges_by_absolute(
        candidates, np.asarray(xy), adaptive_max_edge
    )
    if len(candidates) == 0:
        return candidates

    try:
        import matplotlib.tri as mtri
        local_removed = np.searchsorted(domain_vertices, removed_faces)
        domain = mtri.Triangulation(
            domain_xy[:, 0], domain_xy[:, 1], triangles=local_removed
        )
        finder = domain.get_trifinder()

        tri_xy = np.asarray(xy[candidates], dtype=np.float64)
        centroid = tri_xy.mean(axis=1)
        edge_mid = np.stack((
            (tri_xy[:, 0] + tri_xy[:, 1]) * 0.5,
            (tri_xy[:, 1] + tri_xy[:, 2]) * 0.5,
            (tri_xy[:, 2] + tri_xy[:, 0]) * 0.5,
        ), axis=1)
        # Sample just inside each candidate edge. This rejects convex-hull
        # bridges across concave/disconnected regions without boundary-roundoff.
        samples = np.concatenate((
            centroid[:, None, :],
            centroid[:, None, :] * 0.10 + edge_mid * 0.90,
        ), axis=1)
        inside = np.asarray(
            finder(samples[..., 0].ravel(), samples[..., 1].ravel())
        ).reshape(len(candidates), 4) >= 0
        return candidates[np.all(inside, axis=1)]
    except Exception as exc:
        # The adaptive edge bound is still local to this removed component's
        # extent; retain it rather than reverting to the gap-producing limit.
        print(f"SHADING_LOCAL_FILL domain_clip_fallback={exc}")
        return candidates


def _canonical_face_rows(faces):
    """Return orientation-independent triangle rows for exact matching."""
    arr = np.asarray(faces, dtype=np.int32).reshape(-1, 3)
    if len(arr) == 0:
        return arr
    return np.sort(arr, axis=1)


def _face_membership_mask(candidate_faces, target_faces):
    """Match a small face set inside a bounded candidate set exactly."""
    cand = _canonical_face_rows(candidate_faces)
    target = _canonical_face_rows(target_faces)
    if len(cand) == 0 or len(target) == 0:
        return np.zeros(len(cand), dtype=bool)
    dtype = np.dtype([("a", np.int32), ("b", np.int32), ("c", np.int32)])
    cv = np.ascontiguousarray(cand).view(dtype).ravel()
    tv = np.ascontiguousarray(target).view(dtype).ravel()
    return np.isin(cv, tv, assume_unique=False)



def _build_single_class_delta_actor(
    app,
    cache,
    faces,
    rgb,
    name,
    polygon_offset,
):
    """Build one tiny flat-shaded local delta actor from cache face indices.

    The base single-class mesh remains completely immutable.  Only vertices
    referenced by ``faces`` are materialized into this small PolyData.
    """
    faces = np.asarray(faces, dtype=np.int32).reshape(-1, 3)
    if len(faces) == 0:
        return None
    if cache.xyz_final is None:
        return None
    if np.any(faces < 0) or int(np.max(faces)) >= len(cache.xyz_final):
        return None

    vertex_ids = np.unique(faces.ravel())
    if vertex_ids.size < 3:
        return None
    local_xyz = np.asarray(cache.xyz_final[vertex_ids], dtype=np.float64)
    local_faces = np.searchsorted(vertex_ids, faces).astype(np.int32, copy=False)

    packed = np.empty(len(local_faces) * 4, dtype=np.int32)
    packed[0::4] = 3
    packed[1::4] = local_faces[:, 0]
    packed[2::4] = local_faces[:, 1]
    packed[3::4] = local_faces[:, 2]

    rgb = np.asarray(rgb, dtype=np.uint8).reshape(-1, 3)
    if len(rgb) != len(local_faces):
        return None

    patch = pv.PolyData(local_xyz, packed)
    patch.cell_data['RGB'] = np.ascontiguousarray(rgb, dtype=np.uint8)

    actor = app.vtk_widget.add_mesh(
        patch,
        scalars='RGB',
        rgb=True,
        show_edges=False,
        lighting=False,
        smooth_shading=False,
        preference='cell',
        name=name,
        render=False,
    )
    if actor is None:
        return None

    try:
        setattr(actor, '_is_shading_mesh', True)
        setattr(actor, '_is_single_class_delta_overlay', True)
        actor.PickableOff()
        if hasattr(actor, 'UseBoundsOff'):
            actor.UseBoundsOff()
        prop = actor.GetProperty()
        prop.SetInterpolationToFlat()
        prop.SetAmbient(1.0)
        prop.SetDiffuse(0.0)
        prop.SetSpecular(0.0)
        prop.EdgeVisibilityOff()
        mapper = actor.GetMapper()
        if mapper is not None:
            mapper.StaticOn()
            mapper.SetResolveCoincidentTopologyToPolygonOffset()
            mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(
                float(polygon_offset), float(polygon_offset)
            )
            mapper.InterpolateScalarsBeforeMappingOff()
    except Exception:
        pass
    return actor


def _apply_single_class_remove_delta_overlay(
    app,
    cache,
    slot_indices,
    original_faces,
    replacement_faces,
    replacement_shade,
    new_faces,
    new_shade,
    replacement_normals=None,
):
    """Apply visible->hidden single-class topology as a replacement skin.

    IMPORTANT CORRECTNESS RULE:
      Do NOT paint the old triangles black.  A black erase actor sits at the
      original surface depth and can occlude the replacement triangulation,
      which produces the large black voids seen after Ground -> other-class
      classification.

    Instead the huge base mesh stays immutable and only the newly triangulated
    local replacement surface is drawn on top with a strong, but bounded,
    polygon-depth priority.  The CPU topology is still updated exactly, so
    subsequent classification/undo uses the current mesh rather than the old
    one.  SNT/DXF overlays keep a much stronger (-50000) priority, therefore
    vector overlays remain above this local shading skin.

    If no valid replacement surface can be produced, return None so the caller
    falls back to the existing exact fixed-slot VTK path.
    """
    t0 = time.perf_counter()
    slots = np.asarray(slot_indices, dtype=np.int64).ravel()
    old_faces = np.asarray(original_faces, dtype=np.int32).reshape(-1, 3)
    repl_faces = np.asarray(replacement_faces, dtype=np.int32).reshape(-1, 3)
    repl_shade = np.asarray(replacement_shade, dtype=np.float32).ravel()
    fill_faces = np.asarray(new_faces, dtype=np.int32).reshape(-1, 3)
    fill_shade = np.asarray(new_shade, dtype=np.float32).ravel()

    if (
        slots.size == 0
        or len(old_faces) != len(slots)
        or len(repl_faces) != len(slots)
        or len(repl_shade) != len(slots)
        or cache.faces is None
        or cache.shade is None
        or len(cache.shade) != len(cache.faces)
        or int(slots.min()) < 0
        or int(slots.max()) >= len(cache.faces)
    ):
        return None

    # A replacement-only visual skin cannot represent a true no-fill case.
    # Use the exact VTK slot mutation fallback in that rare situation.
    if len(fill_faces) == 0 or len(fill_shade) != len(fill_faces):
        return None

    plotter = getattr(app, 'vtk_widget', None)
    base_actor = getattr(app, '_shaded_mesh_actor', None)
    base_mesh = getattr(app, '_shaded_mesh_polydata', None)
    if plotter is None or base_actor is None or base_mesh is None:
        return None

    # This path is intentionally limited to the current flat/faceted renderer.
    if bool(getattr(cache, 'smooth_all_classes', False)):
        return None

    generation = int(getattr(app, '_shading_remove_overlay_generation', 0)) + 1
    app._shading_remove_overlay_generation = generation
    fill_name = f'shaded_mesh_live_remove_fill_{generation}'

    fill_actor = None
    try:
        class_color = np.asarray(
            app.class_palette.get(cache.single_class_id, {}).get(
                'color', (128, 128, 128)
            ),
            dtype=np.float32,
        )
        fill_rgb = np.clip(
            class_color[None, :] * fill_shade[:, None], 0, 255
        ).astype(np.uint8)

        # The base shading mesh uses no strong relative polygon priority.
        # Multi-class live colour overlays use about -4, while SNT/DXF
        # overlays are restored with about -50000.  -5000 is therefore
        # deliberately strong enough to keep the local replacement surface
        # above the stale base facets without stealing priority from vectors.
        fill_actor = _build_single_class_delta_actor(
            app, cache, fill_faces, fill_rgb, fill_name, -5000.0
        )
        if fill_actor is None:
            return None

        # Only after the replacement actor is ready do we advance the CPU
        # topology.  The giant base VTK connectivity/RGB arrays stay immutable.
        cache.faces[slots] = repl_faces
        cache.shade[slots] = repl_shade
        if (
            replacement_normals is not None
            and cache.face_normals is not None
            and len(cache.face_normals) == len(cache.faces)
        ):
            rn = np.asarray(
                replacement_normals, dtype=cache.face_normals.dtype
            ).reshape(-1, 3)
            if len(rn) == len(slots):
                cache.face_normals[slots] = rn

        cache._needs_compaction = False

        entry = {
            'erase_name': None,
            'fill_name': fill_name,
            'slot_indices': slots.copy(),
        }
        overlays = getattr(app, '_shading_remove_overlays', None)
        if not isinstance(overlays, list):
            overlays = []
            app._shading_remove_overlays = overlays
        overlays.append(entry)

        app._shading_present_reason = 'singleclass_remove_replacement_overlay'
        _schedule_fast_shaded_present(
            app, delay_ms=0, restart=True, wait_while_preview=True
        )

        print(
            'SHADING_REMOVE_REPLACEMENT_OVERLAY '
            f'old_faces={len(old_faces)} fill_faces={len(fill_faces)} '
            f'local_actors=1 elapsed={(time.perf_counter()-t0)*1000.0:.1f}ms '
            'black_eraser=False base_vbo_untouched=True'
        )
        return entry

    except Exception as exc:
        try:
            if fill_name:
                plotter.remove_actor(fill_name, render=False)
        except Exception:
            pass
        print(
            f'SHADING_REMOVE_REPLACEMENT_OVERLAY '
            f'fallback={type(exc).__name__}: {exc}'
        )
        return None

def _remove_single_class_delta_overlay_entry(app, record):
    """Remove the two tiny actors owned by one remove-delta journal record."""
    plotter = getattr(app, 'vtk_widget', None)
    if plotter is None:
        return
    for key in ('erase_name', 'fill_name'):
        name = record.get(key)
        if not name:
            continue
        try:
            plotter.remove_actor(str(name), render=False)
        except Exception:
            pass

    overlays = getattr(app, '_shading_remove_overlays', None)
    if isinstance(overlays, list):
        erase_name = record.get('erase_name')
        fill_name = record.get('fill_name')
        app._shading_remove_overlays = [
            e for e in overlays
            if not (
                isinstance(e, dict)
                and e.get('erase_name') == erase_name
                and e.get('fill_name') == fill_name
            )
        ]

def _patch_shaded_face_slots_in_place(
    app,
    cache,
    slot_indices,
    replacement_faces,
    replacement_shade,
    replacement_rgb,
):
    """Patch a fixed set of facet slots without copying the complete mesh.

    Used only by the flat/faceted single-class local-remove path. Cell count
    stays constant; unused removed slots become degenerate triangles. If the
    live VTK cell layout is not the expected modern connectivity layout, return
    False so the existing full-array fallback runs unchanged.
    """
    slots = np.asarray(slot_indices, dtype=np.int64).ravel()
    faces = np.asarray(replacement_faces, dtype=np.int32).reshape(-1, 3)
    shades = np.asarray(replacement_shade, dtype=np.float32).ravel()
    rgbs = np.asarray(replacement_rgb, dtype=np.uint8).reshape(-1, 3)

    if (
        slots.size == 0
        or len(faces) != len(slots)
        or len(shades) != len(slots)
        or len(rgbs) != len(slots)
        or cache.faces is None
        or cache.shade is None
        or len(cache.shade) != len(cache.faces)
        or int(slots.min()) < 0
        or int(slots.max()) >= len(cache.faces)
    ):
        return False

    mesh = getattr(app, "_shaded_mesh_polydata", None)
    actor = getattr(app, "_shaded_mesh_actor", None)
    if (
        mesh is None
        or actor is None
        or mesh.GetNumberOfCells() != len(cache.faces)
    ):
        return False

    try:
        polys = mesh.GetPolys()
        conn_arr = polys.GetConnectivityArray() if polys is not None else None
        cell_colors = mesh.GetCellData().GetScalars()
        if conn_arr is None or cell_colors is None:
            return False
        if conn_arr.GetNumberOfTuples() != len(cache.faces) * 3:
            return False
        if cell_colors.GetNumberOfTuples() != len(cache.faces):
            return False

        conn_np = numpy_support.vtk_to_numpy(conn_arr).reshape(-1, 3)
        rgb_np = numpy_support.vtk_to_numpy(cell_colors)
        if rgb_np.ndim != 2 or rgb_np.shape[1] < 3:
            return False

        old_faces = cache.faces[slots].copy()
        old_shade = cache.shade[slots].copy()
        old_conn = conn_np[slots].copy()
        old_rgb = rgb_np[slots, :3].copy()

        try:
            cache.faces[slots] = faces
            cache.shade[slots] = shades
            conn_np[slots] = faces
            rgb_np[slots, :3] = rgbs

            conn_arr.Modified()
            polys.Modified()
            cell_colors.Modified()
            mesh.GetCellData().Modified()
            mesh.Modified()
            mapper = actor.GetMapper()
            if mapper is not None:
                mapper.Modified()
            actor.Modified()

            cache._vtk_colors_ptr = None
            cache._needs_compaction = False
            app._shading_present_reason = 'singleclass_remove_vtk_slots'
            _schedule_fast_shaded_present(app, delay_ms=0)
            return True
        except Exception:
            # Restore local state before allowing the safe legacy fallback.
            cache.faces[slots] = old_faces
            cache.shade[slots] = old_shade
            conn_np[slots] = old_conn
            rgb_np[slots, :3] = old_rgb
            try:
                conn_arr.Modified()
                polys.Modified()
                cell_colors.Modified()
                mesh.Modified()
            except Exception:
                pass
            return False
    except Exception:
        return False


def _undo_last_local_remove_patch(app, added_global_indices):
    """Restore the exact topology removed by the latest LOCAL_REMOVE.

    The original faces and their shading values are restored exactly.
    Restored RGB is regenerated from the visible-class base color and the
    recorded original shade. It is never copied from the live VTK RGB array,
    because that array may already contain black classification colors.
    """
    t0 = time.perf_counter()
    cache = get_cache()
    stack = getattr(app, "_shading_local_remove_journal", None)
    if not isinstance(stack, list) or not stack:
        return False

    added = np.unique(np.asarray(added_global_indices, dtype=np.int64).ravel())
    record = stack[-1]
    recorded = np.unique(
        np.asarray(record.get("changed_global_indices", []), dtype=np.int64).ravel()
    )
    if added.size != recorded.size or not np.array_equal(added, recorded):
        return False

    current_single_class = getattr(cache, "single_class_id", None)
    recorded_single_class = record.get("single_class_id")
    if current_single_class is None or recorded_single_class is None:
        return False
    if int(recorded_single_class) != int(current_single_class):
        return False
    if cache.faces is None or cache.xyz_unique is None or cache.xyz_final is None:
        return False

    fill_faces = np.asarray(record.get("fill_faces", []), dtype=np.int32).reshape(-1, 3)
    original_faces = np.asarray(record.get("original_faces", []), dtype=np.int32).reshape(-1, 3)
    if len(original_faces) == 0:
        return False
    if np.any(original_faces < 0) or int(np.max(original_faces)) >= len(cache.xyz_unique):
        print("SHADING_UNDO_LOCAL_RESTORE status=stale reason=original_vertex_out_of_range")
        return False

    # Fastest journal format: the base GPU mesh was never touched.  Restore
    # CPU slots and remove only the tiny eraser/fill actors.  This is the exact
    # counterpart of SHADING_REMOVE_OVERLAY and therefore avoids any giant VTK
    # connectivity/color buffer invalidation on Ctrl+Z.
    if record.get('visual_delta_overlay'):
        slots = np.asarray(record.get('slot_indices', []), dtype=np.int64).ravel()
        slot_replacement = np.asarray(
            record.get('slot_replacement_faces', []), dtype=np.int32
        ).reshape(-1, 3)
        original_shade = np.asarray(
            record.get('original_shade', []), dtype=np.float32
        ).reshape(-1)
        original_normals = record.get('original_normals')
        if (
            len(slots) == len(original_faces)
            and len(slot_replacement) == len(slots)
            and len(original_shade) == len(slots)
            and len(slots) > 0
            and int(slots.min()) >= 0
            and int(slots.max()) < len(cache.faces)
            and np.array_equal(cache.faces[slots], slot_replacement)
        ):
            cache.faces[slots] = original_faces
            cache.shade[slots] = original_shade
            if (
                original_normals is not None
                and cache.face_normals is not None
                and len(cache.face_normals) == len(cache.faces)
            ):
                on = np.asarray(
                    original_normals, dtype=cache.face_normals.dtype
                ).reshape(-1, 3)
                if len(on) == len(slots):
                    cache.face_normals[slots] = on

            _remove_single_class_delta_overlay_entry(app, record)
            stack.pop()
            app._shading_present_reason = 'singleclass_remove_replacement_undo'
            _schedule_fast_shaded_present(
                app, delay_ms=0, restart=True, wait_while_preview=False
            )
            print(
                'SHADING_UNDO_LOCAL_RESTORE status=restored_replacement_overlay '
                f'changed_points={len(added)} slots={len(slots)} '
                f'elapsed={(time.perf_counter()-t0)*1000.0:.1f}ms '
                'base_vbo_untouched=True'
            )
            return True
        print(
            'SHADING_UNDO_LOCAL_RESTORE status=delta_overlay_miss '
            'fallback=slot_or_legacy_restore'
        )

    # Fast journal format: the local remove kept cell count fixed and rewrote
    # only these exact slots. Undo can therefore restore them directly without
    # scanning/copying the complete 11M+ face mesh.
    slot_indices_raw = record.get("slot_indices")
    slot_replacement_raw = record.get("slot_replacement_faces")
    if slot_indices_raw is not None and slot_replacement_raw is not None:
        slots = np.asarray(slot_indices_raw, dtype=np.int64).ravel()
        slot_replacement = np.asarray(
            slot_replacement_raw, dtype=np.int32
        ).reshape(-1, 3)
        if (
            len(slots) == len(original_faces)
            and len(slot_replacement) == len(slots)
            and len(slots) > 0
            and int(slots.min()) >= 0
            and int(slots.max()) < len(cache.faces)
            and int(record.get("face_count", len(cache.faces))) == len(cache.faces)
            and np.array_equal(cache.faces[slots], slot_replacement)
        ):
            original_shade = record.get("original_shade")
            if original_shade is None:
                original_normals = _compute_face_normals(
                    cache.xyz_unique, original_faces
                )
                original_shade = _compute_face_shade_global_z(
                    cache.xyz_unique,
                    original_faces,
                    getattr(app, "last_shade_azimuth", 45.0),
                    getattr(app, "last_shade_angle", 45.0),
                    getattr(app, "shade_ambient", 0.25),
                    face_normals=original_normals,
                )
            original_shade = np.asarray(
                original_shade, dtype=np.float32
            ).reshape(-1)
            if len(original_shade) == len(original_faces):
                class_color = np.asarray(
                    app.class_palette.get(int(current_single_class), {}).get(
                        "color", (128, 128, 128)
                    ),
                    dtype=np.float32,
                ).reshape(3)
                restored_rgb = np.clip(
                    class_color[None, :] * original_shade[:, None],
                    0,
                    255,
                ).astype(np.uint8)
                if _patch_shaded_face_slots_in_place(
                    app,
                    cache,
                    slots,
                    original_faces,
                    original_shade,
                    restored_rgb,
                ):
                    stack.pop()
                    print(
                        "SHADING_UNDO_LOCAL_RESTORE status=restored_fast_slots "
                        f"changed_points={len(added)} slots={len(slots)} "
                        f"elapsed={(time.perf_counter()-t0)*1000.0:.1f}ms "
                        "full_mesh_copy_avoided=True"
                    )
                    return True

        print(
            "SHADING_UNDO_LOCAL_RESTORE status=slot_fast_path_miss "
            "fallback=legacy_exact_restore"
        )

    if len(fill_faces):
        if np.any(fill_faces < 0) or int(np.max(fill_faces)) >= len(cache.xyz_unique):
            print("SHADING_UNDO_LOCAL_RESTORE status=stale reason=fill_vertex_out_of_range")
            return False
        patch_vertices = np.unique(fill_faces.ravel())
        vertex_mask = np.zeros(len(cache.xyz_unique), dtype=bool)
        vertex_mask[patch_vertices] = True
        candidate_mask = (
            vertex_mask[cache.faces[:, 0]]
            & vertex_mask[cache.faces[:, 1]]
            & vertex_mask[cache.faces[:, 2]]
        )
        candidate_idx = np.flatnonzero(candidate_mask)
        local_hit = _face_membership_mask(cache.faces[candidate_idx], fill_faces)
        fill_mask = np.zeros(len(cache.faces), dtype=bool)
        fill_mask[candidate_idx[local_hit]] = True
    else:
        fill_mask = np.zeros(len(cache.faces), dtype=bool)

    matched_fill_count = int(np.count_nonzero(fill_mask))
    if matched_fill_count != len(fill_faces):
        print(
            "SHADING_UNDO_LOCAL_RESTORE status=stale "
            f"expected_fill={len(fill_faces)} matched_fill={matched_fill_count}"
        )
        return False

    old_face_count = len(cache.faces)
    keep_mask = ~fill_mask
    kept_faces = cache.faces[keep_mask]
    cache.faces = np.vstack((kept_faces, original_faces)).astype(np.int32, copy=False)

    original_shade = record.get("original_shade")
    if original_shade is None:
        original_normals_for_shade = _compute_face_normals(cache.xyz_unique, original_faces)
        original_shade = _compute_face_shade_global_z(
            cache.xyz_unique,
            original_faces,
            getattr(app, "last_shade_azimuth", 45.0),
            getattr(app, "last_shade_angle", 45.0),
            getattr(app, "shade_ambient", 0.25),
            face_normals=original_normals_for_shade,
        )
    else:
        original_shade = np.asarray(original_shade, dtype=np.float32).reshape(-1)

    if len(original_shade) != len(original_faces):
        print(
            "SHADING_UNDO_LOCAL_RESTORE status=stale "
            f"reason=shade_count_mismatch faces={len(original_faces)} shade={len(original_shade)}"
        )
        return False

    kept_shade = None
    if cache.shade is not None and len(cache.shade) == old_face_count:
        kept_shade = np.asarray(cache.shade[keep_mask], dtype=np.float32)
    if kept_shade is None:
        kept_normals_for_shade = _compute_face_normals(cache.xyz_unique, kept_faces)
        kept_shade = _compute_face_shade_global_z(
            cache.xyz_unique,
            kept_faces,
            getattr(app, "last_shade_azimuth", 45.0),
            getattr(app, "last_shade_angle", 45.0),
            getattr(app, "shade_ambient", 0.25),
            face_normals=kept_normals_for_shade,
        )
    cache.shade = np.concatenate((kept_shade, original_shade)).astype(np.float32, copy=False)

    smooth_mode = bool(getattr(cache, "smooth_all_classes", False))
    if smooth_mode:
        original_normals = record.get("original_normals")
        if original_normals is None:
            original_normals = _compute_face_normals(cache.xyz_unique, original_faces)
        else:
            original_normals = np.asarray(original_normals, dtype=np.float64).reshape(-1, 3)

        kept_normals = None
        if cache.face_normals is not None and len(cache.face_normals) == old_face_count:
            kept_normals = cache.face_normals[keep_mask]
        if kept_normals is None:
            kept_normals = _compute_face_normals(cache.xyz_unique, kept_faces)

        cache.face_normals = np.vstack((kept_normals, original_normals))
        cache.vertex_normals = _compute_vertex_normals(
            cache.xyz_unique, cache.faces, cache.face_normals
        )
        cache.vertex_shade = _compute_shading(
            cache.vertex_normals,
            getattr(app, "last_shade_azimuth", 45.0),
            getattr(app, "last_shade_angle", 45.0),
            getattr(app, "shade_ambient", 0.25),
            z_values=cache.xyz_unique[:, 2],
        )
    else:
        cache.face_normals = None
        cache.vertex_normals = None
        cache.vertex_shade = None

    current_rgb = None
    try:
        mesh = getattr(app, "_shaded_mesh_polydata", None)
        if mesh is not None:
            scalars = mesh.GetCellData().GetScalars()
            if scalars is not None and scalars.GetNumberOfTuples() == old_face_count:
                current_rgb = np.asarray(
                    numpy_support.vtk_to_numpy(scalars), dtype=np.uint8
                )
    except Exception:
        current_rgb = None

    class_color = np.asarray(
        app.class_palette.get(int(current_single_class), {}).get(
            "color", (128, 128, 128)
        ),
        dtype=np.float32,
    ).reshape(3)
    restored_rgb = np.clip(
        class_color[None, :] * original_shade[:, None], 0, 255
    ).astype(np.uint8)

    if current_rgb is not None and len(current_rgb) == old_face_count:
        kept_rgb = np.ascontiguousarray(current_rgb[keep_mask], dtype=np.uint8)
    else:
        kept_rgb = np.clip(
            class_color[None, :] * kept_shade[:, None], 0, 255
        ).astype(np.uint8)

    cache._pending_fast_rgb = np.ascontiguousarray(
        np.vstack((kept_rgb, restored_rgb)), dtype=np.uint8
    )
    cache._vtk_colors_ptr = None
    cache._needs_compaction = False
    stack.pop()

    _render_mesh_fast_update(
        app, cache, matched_fill_count, len(original_faces)
    )
    print(
        "SHADING_UNDO_LOCAL_RESTORE status=restored "
        f"changed_points={len(added)} removed_fill_faces={matched_fill_count} "
        f"restored_faces={len(original_faces)} restored_rgb=recomputed "
        f"elapsed={(time.perf_counter() - t0) * 1000.0:.1f}ms "
        "full_rebuild_avoided=True"
    )
    return True


def _local_remove_single_class_points(app, removed_global_indices):
    """Remove visible-class points with a bounded local topology patch.

    Preferred path rewrites only the affected triangle slots in place. The
    previous full-array path is retained as a fallback for unsupported layouts.
    """
    t0 = time.perf_counter()
    cache = get_cache()
    xyz = app.data.get("xyz")
    cls = app.data.get("classification")
    if xyz is None or cls is None or cache.faces is None:
        return False

    gi = np.unique(np.asarray(removed_global_indices, dtype=np.int64).ravel())
    if gi.size == 0:
        return True

    overlay_removed = _remove_from_add_overlays(app, gi)
    g2u = cache.build_global_to_unique(len(xyz))
    valid_gi = gi[(gi >= 0) & (gi < len(g2u))]
    if valid_gi.size == 0:
        return True

    removed_ui = np.unique(
        g2u[valid_gi][g2u[valid_gi] >= 0]
    ).astype(np.int32)
    if removed_ui.size == 0:
        return True

    old_face_count = len(cache.faces)
    removed_vertex = np.zeros(len(cache.unique_indices), dtype=bool)
    removed_vertex[removed_ui] = True

    if HAS_NUMBA:
        affected = getattr(cache, "_affected_face_scratch", None)
        if (
            affected is None
            or affected.dtype != np.bool_
            or len(affected) != old_face_count
        ):
            affected = np.empty(old_face_count, dtype=np.bool_)
            cache._affected_face_scratch = affected
        _numba_fill_affected_face_mask(cache.faces, removed_vertex, affected)
    else:
        affected = (
            removed_vertex[cache.faces[:, 0]]
            | removed_vertex[cache.faces[:, 1]]
            | removed_vertex[cache.faces[:, 2]]
        )

    affected_idx = np.flatnonzero(affected).astype(np.int64, copy=False)
    n_removed = len(affected_idx)
    if n_removed == 0:
        print(
            "SHADING_LOCAL_PATCH operation=LOCAL_REMOVE "
            f"overlay_points={overlay_removed} base_faces=0 "
            "full_mesh_upload_avoided=True"
        )
        return True

    original_faces = np.asarray(
        cache.faces[affected_idx], dtype=np.int32
    ).copy()
    original_normals = (
        np.asarray(cache.face_normals[affected_idx]).copy()
        if cache.face_normals is not None
        and len(cache.face_normals) == old_face_count
        else _compute_face_normals(cache.xyz_unique, original_faces)
    )
    original_shade = (
        np.asarray(cache.shade[affected_idx], dtype=np.float32).copy()
        if cache.shade is not None and len(cache.shade) == old_face_count
        else _compute_face_shade_global_z(
            cache.xyz_unique,
            original_faces,
            getattr(app, "last_shade_azimuth", 45.0),
            getattr(app, "last_shade_angle", 45.0),
            getattr(app, "shade_ambient", 0.25),
            face_normals=original_normals,
        )
    )

    ring = np.unique(original_faces.ravel())
    ring = ring[~removed_vertex[ring]].astype(np.int32, copy=False)
    new_faces = np.empty((0, 3), dtype=np.int32)
    if ring.size >= 3:
        local_faces = _do_triangulate(cache.xyz_unique[ring, :2])
        if len(local_faces):
            candidate_faces = ring[local_faces]
            base_max_edge = max(
                cache.spacing * cache.max_edge_factor * 4.0,
                cache.spacing * 8.0,
            )
            new_faces = _clip_fill_faces_to_removed_region(
                cache.xyz_unique[:, :2],
                candidate_faces,
                original_faces,
                base_max_edge,
            )

    new_faces = np.asarray(new_faces, dtype=np.int32).reshape(-1, 3)
    new_normals = _compute_face_normals(cache.xyz_unique, new_faces)
    new_shade = _compute_face_shade_global_z(
        cache.xyz_unique,
        new_faces,
        getattr(app, "last_shade_azimuth", 45.0),
        getattr(app, "last_shade_angle", 45.0),
        getattr(app, "shade_ambient", 0.25),
        face_normals=new_normals,
    )

    flat_mode = not getattr(cache, "smooth_all_classes", False)

    # Fast fixed-slot path. Removing points normally reduces local triangle
    # count, so the replacement fill fits inside the invalidated slots.
    if flat_mode and len(new_faces) <= n_removed:
        anchor = None
        if ring.size:
            anchor = int(ring[0])
        else:
            for candidate in original_faces.ravel():
                if not removed_vertex[int(candidate)]:
                    anchor = int(candidate)
                    break

        if anchor is not None:
            replacement_faces = np.empty((n_removed, 3), dtype=np.int32)
            n_new = len(new_faces)
            if n_new:
                replacement_faces[:n_new] = new_faces
            if n_new < n_removed:
                replacement_faces[n_new:] = anchor

            replacement_shade = np.zeros(n_removed, dtype=np.float32)
            if n_new:
                replacement_shade[:n_new] = new_shade

            base_color = np.asarray(
                app.class_palette.get(cache.single_class_id, {}).get(
                    "color", (128, 128, 128)
                ),
                dtype=np.float32,
            )
            replacement_rgb = np.zeros((n_removed, 3), dtype=np.uint8)
            if n_new:
                replacement_rgb[:n_new] = np.clip(
                    base_color * new_shade[:, None], 0, 255
                ).astype(np.uint8)

            replacement_normals = np.zeros((n_removed, 3), dtype=np.float64)
            replacement_normals[:, 2] = 1.0
            if n_new:
                replacement_normals[:n_new] = new_normals

            # --------------------------------------------------------------
            # Preferred single-class visible -> hidden path:
            # keep the gigantic VTK base actor immutable and render only a
            # tiny local erase/fill delta.  This mirrors the already-smooth
            # any-class -> visible-class overlay architecture.
            # --------------------------------------------------------------
            delta_entry = _apply_single_class_remove_delta_overlay(
                app,
                cache,
                affected_idx,
                original_faces,
                replacement_faces,
                replacement_shade,
                new_faces,
                new_shade,
                replacement_normals=replacement_normals,
            )
            if delta_entry is not None:
                journal = getattr(app, "_shading_local_remove_journal", None)
                if not isinstance(journal, list):
                    journal = []
                    app._shading_local_remove_journal = journal
                journal.append(
                    {
                        "single_class_id": int(cache.single_class_id),
                        "changed_global_indices": valid_gi.copy(),
                        "original_faces": original_faces,
                        "fill_faces": new_faces.copy(),
                        "original_shade": np.asarray(
                            original_shade, dtype=np.float32
                        ).copy(),
                        "original_normals": np.asarray(original_normals).copy(),
                        "slot_indices": affected_idx.copy(),
                        "slot_replacement_faces": replacement_faces.copy(),
                        "face_count": int(old_face_count),
                        "visual_delta_overlay": True,
                        "erase_name": delta_entry.get("erase_name"),
                        "fill_name": delta_entry.get("fill_name"),
                    }
                )
                if len(journal) > 128:
                    del journal[:-128]

                print(
                    "SHADING_LOCAL_PATCH operation=LOCAL_REMOVE_REPLACEMENT_OVERLAY "
                    f"removed_points={len(removed_ui)} slots={n_removed} "
                    f"new_faces={len(new_faces)} "
                    f"elapsed={(time.perf_counter()-t0)*1000:.1f}ms "
                    "base_vbo_untouched=True"
                )
                return True

            # Safety fallback: original exact fixed-slot VTK patch.  This is
            # preserved unchanged for any renderer/layout where delta-overlay
            # creation is unavailable.
            if _patch_shaded_face_slots_in_place(
                app,
                cache,
                affected_idx,
                replacement_faces,
                replacement_shade,
                replacement_rgb,
            ):
                journal = getattr(app, "_shading_local_remove_journal", None)
                if not isinstance(journal, list):
                    journal = []
                    app._shading_local_remove_journal = journal
                journal.append(
                    {
                        "single_class_id": int(cache.single_class_id),
                        "changed_global_indices": valid_gi.copy(),
                        "original_faces": original_faces,
                        "fill_faces": new_faces.copy(),
                        "original_shade": np.asarray(
                            original_shade, dtype=np.float32
                        ).copy(),
                        "original_normals": np.asarray(original_normals).copy(),
                        "slot_indices": affected_idx.copy(),
                        "slot_replacement_faces": replacement_faces.copy(),
                        "face_count": int(old_face_count),
                    }
                )
                if len(journal) > 128:
                    del journal[:-128]

                print(
                    "SHADING_LOCAL_PATCH operation=LOCAL_REMOVE_FAST_SLOTS "
                    f"removed_points={len(removed_ui)} slots={n_removed} "
                    f"new_faces={len(new_faces)} "
                    f"elapsed={(time.perf_counter()-t0)*1000:.1f}ms "
                    "full_mesh_copy_avoided=True"
                )
                return True

    # ------------------------------------------------------------------
    # Existing exact fallback. Only reached if the in-place layout is not
    # supported or the replacement unexpectedly needs more cell slots.
    # ------------------------------------------------------------------
    keep_faces = cache.faces[~affected]
    keep_normals = (
        cache.face_normals[~affected]
        if cache.face_normals is not None and not flat_mode
        else None
    )
    keep_shade = (
        cache.shade[~affected]
        if cache.shade is not None and len(cache.shade) == old_face_count
        else None
    )

    if keep_shade is None:
        keep_face_normals = _compute_face_normals(cache.xyz_unique, keep_faces)
        keep_shade = _compute_face_shade_global_z(
            cache.xyz_unique,
            keep_faces,
            getattr(app, "last_shade_azimuth", 45.0),
            getattr(app, "last_shade_angle", 45.0),
            getattr(app, "shade_ambient", 0.25),
            face_normals=keep_face_normals,
        )

    journal = getattr(app, "_shading_local_remove_journal", None)
    if not isinstance(journal, list):
        journal = []
        app._shading_local_remove_journal = journal
    journal.append(
        {
            "single_class_id": int(cache.single_class_id),
            "changed_global_indices": valid_gi.copy(),
            "original_faces": original_faces,
            "fill_faces": new_faces.copy(),
            "original_shade": np.asarray(
                original_shade, dtype=np.float32
            ).copy(),
            "original_normals": np.asarray(original_normals).copy(),
        }
    )
    if len(journal) > 128:
        del journal[:-128]

    current_rgb = None
    try:
        scalars = app._shaded_mesh_polydata.GetCellData().GetScalars()
        if scalars is not None and scalars.GetNumberOfTuples() == old_face_count:
            current_rgb = np.asarray(
                numpy_support.vtk_to_numpy(scalars), dtype=np.uint8
            )
    except Exception:
        current_rgb = None

    base_color = np.asarray(
        app.class_palette.get(cache.single_class_id, {}).get(
            "color", (128, 128, 128)
        ),
        dtype=np.float32,
    )
    if current_rgb is not None and len(current_rgb) == old_face_count:
        kept_rgb = np.ascontiguousarray(
            current_rgb[~affected], dtype=np.uint8
        )
    else:
        kept_rgb = np.clip(
            base_color * keep_shade[:, None], 0, 255
        ).astype(np.uint8)
    new_rgb = np.clip(
        base_color * new_shade[:, None], 0, 255
    ).astype(np.uint8)
    cache._pending_fast_rgb = np.ascontiguousarray(
        np.vstack((kept_rgb, new_rgb)), dtype=np.uint8
    )

    cache.faces = np.vstack([keep_faces, new_faces])
    cache.face_normals = (
        None
        if flat_mode
        else (
            new_normals
            if keep_normals is None
            else np.vstack([keep_normals, new_normals])
        )
    )
    cache.shade = np.concatenate(
        [keep_shade, new_shade]
    ).astype(np.float32, copy=False)
    cache._vtk_colors_ptr = None
    cache._needs_compaction = False
    cache._changed_vertex_scratch = None
    cache._affected_face_scratch = None

    _render_mesh_fast_update(app, cache, n_removed, len(new_faces))
    print(
        "SHADING_LOCAL_PATCH operation=LOCAL_REMOVE "
        f"removed_points={len(removed_ui)} removed_faces={n_removed} "
        f"new_faces={len(new_faces)} "
        f"elapsed={(time.perf_counter()-t0)*1000:.1f}ms "
        "full_rebuild_avoided=True"
    )
    return True

def _fast_incremental_add_points(app, ngi):
    t0 = time.perf_counter()
    cache = get_cache()
    if cache.faces is None or cache.xyz_unique is None or cache.xyz_final is None:
        return False
    if len(ngi) == 0: return True
    xr = app.data.get("xyz"); cr = app.data.get("classification")
    if xr is None or cr is None: return False
    g2u = cache.build_global_to_unique(len(xr)); mg = ngi[g2u[ngi] < 0]
    if len(mg) == 0:
        return _update_colors_gpu_fast(app, cache, changed_mask=None)
    
    # Deduplicate new points using the same spatial scale as the main cache.
    nxr2 = xr[mg]
    nxl = (nxr2 - cache.offset).astype(np.float64)
    precision = max(cache.spacing * 0.3, 0.005)
    keep_new = _grid_dedup_at_precision(nxl, precision)
    mg = mg[keep_new]; nxr2 = nxr2[keep_new]; nxl = nxl[keep_new]
    if len(mg) == 0: return True
    nn = len(mg)
    xn = nxl[:,0].min(); yn = nxl[:,1].min()
    xx = nxl[:,0].max(); yx = nxl[:,1].max()
    from gui.optimization_config import (
        SINGLE_CLASS_PATCH_MARGIN_FACTOR,
        SINGLE_CLASS_PATCH_MAX_EXISTING_VERTICES,
        SINGLE_CLASS_PATCH_MAX_FACES,
        SINGLE_CLASS_PATCH_MAX_AREA_RATIO,
    )
    mrg = max(cache.spacing * float(SINGLE_CLASS_PATCH_MARGIN_FACTOR),
              cache.spacing * cache.max_edge_factor * 2.0, 0.5)
    xn -= mrg; yn -= mrg; xx += mrg; yx += mrg
    
    total_extent = np.ptp(cache.xyz_unique[:, :2], axis=0)
    total_area = max(float(total_extent[0] * total_extent[1]), 1e-9)
    patch_area = max(float((xx - xn) * (yx - yn)), 0.0)
    if patch_area / total_area > float(SINGLE_CLASS_PATCH_MAX_AREA_RATIO):
        return False

    # Build the expensive tree once. Appended vertices remain as a small tail
    # scanned directly until the next full rebuild; rebuilding a 1.6M-point
    # cKDTree for every ground-class stroke dominated LOCAL_ADD latency.
    try:
        from scipy.spatial import cKDTree
        if (cache._xy_tree is None or cache._xy_tree_size <= 0 or
                cache._xy_tree_size > len(cache.xyz_unique)):
            cache._xy_tree = cKDTree(cache.xyz_unique[:, :2])
            cache._xy_tree_size = len(cache.xyz_unique)
        center = np.array([(xn + xx) * 0.5, (yn + yx) * 0.5])
        radius = float(np.hypot(xx - xn, yx - yn) * 0.5)
        candidates = np.asarray(cache._xy_tree.query_ball_point(center, radius), dtype=np.int64)
        if cache._xy_tree_size < len(cache.xyz_unique):
            tail_idx = np.arange(cache._xy_tree_size, len(cache.xyz_unique), dtype=np.int64)
            tail_xy = cache.xyz_unique[tail_idx, :2]
            tail_inside = ((tail_xy[:, 0] >= xn) & (tail_xy[:, 0] <= xx) &
                           (tail_xy[:, 1] >= yn) & (tail_xy[:, 1] <= yx))
            if np.any(tail_inside):
                candidates = np.concatenate((candidates, tail_idx[tail_inside]))
        cp = cache.xyz_unique[candidates, :2]
        inside = ((cp[:, 0] >= xn) & (cp[:, 0] <= xx) &
                  (cp[:, 1] >= yn) & (cp[:, 1] <= yx))
        ei = candidates[inside]
    except Exception:
        eib = ((cache.xyz_unique[:,0] >= xn) & (cache.xyz_unique[:,0] <= xx) &
               (cache.xyz_unique[:,1] >= yn) & (cache.xyz_unique[:,1] <= yx))
        ei = np.flatnonzero(eib)
    if len(ei) > int(SINGLE_CLASS_PATCH_MAX_EXISTING_VERTICES):
        return False
    onv = len(cache.xyz_unique)
    
    snap_unique = cache.unique_indices
    snap_xyz_unique = cache.xyz_unique
    snap_xyz_final = cache.xyz_final
    cache.unique_indices = np.concatenate([cache.unique_indices, mg])
    cache.xyz_unique = np.vstack([cache.xyz_unique, nxl])
    cache.xyz_final = np.vstack([cache.xyz_final, nxr2])
    cache._vtk_colors_ptr = None
    
    nvi = np.arange(onv, onv + nn, dtype=np.int32)
    # Preserve the 38M-entry global lookup and patch only the new members.
    # Reallocating/filling that table on every stroke is unnecessary O(N).
    g2u[mg] = nvi
    cache._global_to_unique = g2u
    if len(ei) > 0:
        ib = np.zeros(len(cache.xyz_unique), dtype=bool)
        ib[ei] = True; ib[nvi] = True
        # Remove every face touching the patch and include all of its vertices
        # as the stitching ring.  This avoids retaining crossing triangles.
        fir = ib[cache.faces[:,0]] | ib[cache.faces[:,1]] | ib[cache.faces[:,2]]
        if int(np.count_nonzero(fir)) > int(SINGLE_CLASS_PATCH_MAX_FACES):
            cache.unique_indices = snap_unique
            cache.xyz_unique = snap_xyz_unique
            cache.xyz_final = snap_xyz_final
            cache._global_to_unique = None
            return False
        n_removed_faces = int(np.count_nonzero(fir))
        ring = np.unique(cache.faces[fir].ravel())
        ring = ring[ring < onv]
        ei = np.unique(np.concatenate([ei, ring])).astype(np.int64, copy=False)
        fo = cache.faces[~fir]
        no = (cache.face_normals[~fir]
              if cache.face_normals is not None and getattr(cache, 'smooth_all_classes', False)
              else None)
        so = cache.shade[~fir] if cache.shade is not None and len(cache.shade) == len(cache.faces) else None
    else:
        n_removed_faces = 0
        fo = cache.faces
        no = cache.face_normals
        so = cache.shade
    
    lai = np.concatenate([ei, nvi]); lxy = cache.xyz_unique[lai, :2]
    if len(lai) < 3:
        cache.unique_indices = snap_unique
        cache.xyz_unique = snap_xyz_unique
        cache.xyz_final = snap_xyz_final
        cache._global_to_unique = None
        return False
    
    try:
        lf = _do_triangulate(lxy)
    except Exception:
        cache.unique_indices = snap_unique
        cache.xyz_unique = snap_xyz_unique
        cache.xyz_final = snap_xyz_final
        cache._global_to_unique = None
        return False
    
    if len(lf) > 0:
        lf = _filter_edges_by_absolute(
            lf, lxy, max(cache.spacing * cache.max_edge_factor * 4.0,
                         cache.spacing * 8.0))
    
    if len(lf) == 0:
        cache.unique_indices = snap_unique
        cache.xyz_unique = snap_xyz_unique
        cache.xyz_final = snap_xyz_final
        cache._global_to_unique = None
        return False
    
    pf = lai[lf]
    pn = _compute_face_normals(cache.xyz_unique, pf)   
    az = getattr(app, 'last_shade_azimuth', 45.)
    an = getattr(app, 'last_shade_angle', 45.)
    am = getattr(app, 'shade_ambient', .25)
    
    nk = len(fo)
    cache.faces = np.vstack([fo, pf])
    cache.face_normals = ((pn if no is None else np.vstack([no, pn]))
                          if getattr(cache, 'smooth_all_classes', False) else None)
    
    ps = _compute_face_shade_global_z(
        cache.xyz_unique, pf, az, an, am, face_normals=pn)
    cache.shade = ps if so is None else np.concatenate([so, ps])
    try:
        scalars = app._shaded_mesh_polydata.GetCellData().GetScalars()
        if scalars is not None and scalars.GetNumberOfTuples() == len(fir):
            old_rgb = numpy_support.vtk_to_numpy(scalars)
            bc = np.array(app.class_palette.get(cache.single_class_id, {}).get("color", (128,128,128)), dtype=np.float32)
            patch_rgb = np.clip(bc * ps[:, None], 0, 255).astype(np.uint8)
            cache._pending_fast_rgb = np.vstack((old_rgb[~fir], patch_rgb))
    except Exception:
        cache._pending_fast_rgb = None
    
    # Single-class shading is cell/facet shaded. Recomputing vertex normals
    # used three full-mesh np.isin scans and had no visible effect.
    if getattr(cache, 'smooth_all_classes', False):
        _recompute_vertex_normals_partial(cache, nk)
    vc = _get_shading_visibility(app)
    cache.visible_classes_hash = cache.get_visible_hash(vc)
    cache.data_hash = _compute_xyz_hash(xr)
    _render_mesh_fast_update(app, cache, n_removed_faces, len(pf))
    print(
        "SHADING_LOCAL_PATCH operation=LOCAL_ADD "
        f"new_points={nn} nearby_vertices={len(ei)} "
        f"removed_faces={n_removed_faces} new_faces={len(pf)} "
        f"full_mesh_vertices={len(cache.xyz_unique)} "
        f"elapsed={(time.perf_counter()-t0)*1000:.1f}ms "
        "full_rebuild_avoided=True"
    )
    return True

def _rebuild_mesh_vtk(app, cache, cr, sc): _render_mesh(app, cache, cr, sc)

def refresh_shaded_colors_fast(app):
    if getattr(app, 'display_mode', None) != "shaded_class": return
    refresh_shaded_after_classification_fast(app, None)

def refresh_shaded_colors_only(app): refresh_shaded_colors_fast(app)

def on_class_visibility_changed(app):
    if getattr(app, 'display_mode', None) == "shaded_class":
        clear_shading_cache("visibility changed"); update_shaded_class(app, force_rebuild=True)

def handle_shaded_view_change(app, view_name):
    try:
        a = getattr(app, '_shaded_mesh_actor', None)
        if not a: return
        _m = a.GetMapper()
        _inp = _m.GetInput() if _m else None
        if _inp is None: return
        b = _inp.GetBounds()
        cx, cy, cz = (b[0]+b[1])/2, (b[2]+b[3])/2, (b[4]+b[5])/2
        ex, ey, ez = b[1]-b[0], b[3]-b[2], b[5]-b[4]; d = max(ex, ey, ez)*2
        cam = app.vtk_widget.renderer.GetActiveCamera()
        if view_name in ("plan", "top"):
            cam.SetPosition(cx, cy, cz+d); cam.SetFocalPoint(cx, cy, cz); cam.SetViewUp(0,1,0)
            cam.SetParallelProjection(True); cam.SetParallelScale(max(ex, ey)/2)
        elif view_name == "front":
            cam.SetPosition(cx, cy-d, cz); cam.SetFocalPoint(cx, cy, cz); cam.SetViewUp(0,0,1); cam.SetParallelProjection(True)
        elif view_name in ("side", "left"):
            cam.SetPosition(cx-d, cy, cz); cam.SetFocalPoint(cx, cy, cz); cam.SetViewUp(0,0,1); cam.SetParallelProjection(True)
        else:
            cam.SetPosition(cx-d*.7, cy-d*.7, cz+d*.7); cam.SetFocalPoint(cx, cy, cz); cam.SetViewUp(0,0,1); cam.SetParallelProjection(False)
        app.vtk_widget.renderer.ResetCameraClippingRange(); app.vtk_widget.render()
    except Exception:
        pass



def _enforce_crisp_base_actor_state(app):
    """Keep the crisp-hybrid base as solid, edge-free CELL-shaded facets.

    The initial crisp renderer intentionally disables VTK lighting on the base
    mesh because its face shade is already baked into CELL RGB.  Re-enabling
    actor lighting on that same mesh produces the dark scan-line/hatched look
    seen after using the Shading popup.  This helper restores the exact actor
    presentation used by ``_render_mesh`` without touching topology.
    """
    actor = getattr(app, '_shaded_mesh_actor', None)
    if actor is None:
        return False
    try:
        setattr(actor, '_is_shading_crisp_base', True)
        actor.SetVisibility(True)
        prop = actor.GetProperty()
        if prop is not None:
            prop.SetLighting(False)
            prop.SetInterpolationToFlat()
            prop.SetAmbient(1.0)
            prop.SetDiffuse(0.0)
            prop.SetSpecular(0.0)
            prop.EdgeVisibilityOff()
            prop.SetOpacity(1.0)
        mapper = actor.GetMapper()
        if mapper is not None:
            mapper.StaticOn()
            mapper.SetResolveCoincidentTopologyToPolygonOffset()
            mapper.InterpolateScalarsBeforeMappingOff()
            mapper.Modified()
        actor.Modified()
        return True
    except Exception as exc:
        print(f'SHADING_CRISP_BASE_STATE status=failed reason={exc}')
        return False


def _refresh_crisp_base_face_colors_in_place(app, cache, classes_raw):
    """Refresh crisp base CELL RGB on the existing TIN, without retriangulation.

    Azimuth/Ambient modify ``cache.shade`` in the lighting-only path.  The crisp
    multi-class base is *not* GPU lit; therefore those new face shades must be
    copied back to its existing cell-color buffer.  Work is chunked so a 69M
    face Slow mesh does not allocate a second full RGB array.
    """
    mesh = getattr(app, '_shaded_mesh_polydata', None)
    actor = getattr(app, '_shaded_mesh_actor', None)
    faces = getattr(cache, 'faces', None)
    unique_indices = getattr(cache, 'unique_indices', None)
    if mesh is None or actor is None or faces is None or unique_indices is None:
        return False
    n_faces = int(len(faces))
    if n_faces <= 0:
        return False

    try:
        vtk_colors = mesh.GetCellData().GetScalars()
        if vtk_colors is None or int(vtk_colors.GetNumberOfTuples()) != n_faces:
            return False
        colors_np = numpy_support.vtk_to_numpy(vtk_colors)
        if colors_np.ndim != 2 or colors_np.shape[0] != n_faces or colors_np.shape[1] < 3:
            return False

        classes = np.asarray(classes_raw)
        vc = set(int(c) for c in (_get_shading_visibility(app) or ()))
        palette_codes = [int(c) for c in getattr(app, 'class_palette', {}).keys()]
        try:
            class_max = int(classes.max()) if classes.size else 0
        except Exception:
            class_max = 0
        max_code = max([255, class_max] + palette_codes)
        lut = np.zeros((max_code + 1, 3), dtype=np.float32)
        for code, entry in getattr(app, 'class_palette', {}).items():
            ci = int(code)
            if 0 <= ci <= max_code and ci in vc:
                lut[ci] = entry.get('color', (128, 128, 128))

        chunk = max(50_000, int(os.environ.get(
            'NAKSHA_SHADING_CRISP_LIGHT_CHUNK', '1000000'
        )))
        t0 = time.perf_counter()
        for begin in range(0, n_faces, chunk):
            end = min(begin + chunk, n_faces)
            fc = faces[begin:end]
            # Crisp-base ownership is intentionally identical to the initial
            # renderer: vertex-0 supplies the hidden/base class RGB; mixed faces
            # keep their separate barycentric overlay above this actor.
            rep_ids = np.asarray(unique_indices[fc[:, 0]], dtype=np.int64)
            cid = np.asarray(classes[rep_ids], dtype=np.int64)
            rgb = lut[np.clip(cid, 0, max_code)]
            face_ids = np.arange(begin, end, dtype=np.int64)
            sh = _crisp_shade_chunk(app, cache, face_ids)
            colors_np[begin:end, :3] = np.clip(
                rgb * sh[:, None], 0, 255
            ).astype(np.uint8)

        vtk_colors.Modified()
        mesh.GetCellData().Modified()
        mesh.Modified()
        mapper = actor.GetMapper()
        if mapper is not None:
            mapper.Modified()
        _enforce_crisp_base_actor_state(app)
        print(
            'SHADING_CRISP_BASE_REFRESH status=updated '
            f'faces={n_faces:,} elapsed={(time.perf_counter()-t0)*1000:.1f}ms '
            'topology_rebuild=0 actor_rebuild=0 presentation=solid_cell_facets'
        )
        return True
    except Exception as exc:
        print(f'SHADING_CRISP_BASE_REFRESH status=failed reason={exc}')
        return False

def update_shading_lighting_only(app, azimuth=None, angle=None, ambient=None):
    """Update live shading without rebuilding representatives or triangles.

    Multi-class ``angle`` is the user-facing Sharpness control.  The important
    split is presentation-aware:

    * crisp-hybrid base = baked CELL RGB, lighting MUST stay OFF;
    * mixed-class overlays = point RGB + flat GPU lighting;
    * non-crisp blend = the base mesh itself may use GPU lighting.

    This keeps the exact TIN produced by Shaded Classification intact when the
    popup changes Azimuth/Sharpness/Ambient and prevents the hatched/line-based
    regression caused by enabling lighting on the crisp base actor.
    """
    data = getattr(app, "data", None)
    if not isinstance(data, dict):
        return False
    xyz_raw = data.get("xyz")
    classes_raw = data.get("classification")
    if xyz_raw is None or classes_raw is None:
        return False

    azimuth = float(
        getattr(app, 'last_shade_azimuth', 45.0)
        if azimuth is None else azimuth
    )
    ambient = float(
        getattr(app, 'shade_ambient', 0.25)
        if ambient is None else ambient
    )

    rendered_key = _get_rendered_cache_key(app)
    cache = _cache_store.get(rendered_key) if rendered_key is not None else None
    actor_live = (
        getattr(app, '_shaded_mesh_actor', None) is not None
        and getattr(app, 'vtk_widget', None) is not None
        and 'shaded_mesh' in getattr(app.vtk_widget, 'actors', {})
    )
    if (
        cache is None
        or not actor_live
        or cache.visible_classes_set is None
        or not cache.is_geometry_valid(xyz_raw, cache.visible_classes_set)
    ):
        print('SHADING_LIGHT_ONLY status=fallback reason=no_valid_live_geometry')
        update_shaded_class(app, azimuth, angle, ambient, force_rebuild=False)
        return False

    t0 = time.perf_counter()
    saved_camera = _save_camera(app)
    app.last_shade_azimuth = azimuth
    app.shade_ambient = ambient

    if _microstation_color_blend_enabled(app, cache):
        sharpness_angle = _shading_sharpness_angle(app, angle)
        _, sharpness_overdrive = _shading_sharpness_response(sharpness_angle)
        light_elevation = _shading_effective_light_elevation(
            app, sharpness_overdrive
        )
        app.shading_sharpness_angle = sharpness_angle
        app.last_shade_angle = light_elevation

        previous_crisp_sharpness = float(
            getattr(cache, 'last_crisp_sharpness', -1.0)
        )
        crisp_sharpness_changed = (
            abs(previous_crisp_sharpness - sharpness_angle) > 0.001
        )

        # Geometry stays immutable.  Recompute only the canonical per-face shade
        # used by the crisp CELL-RGB base when Azimuth/Ambient/effective light
        # elevation changed.  Sharpness 0..90 keeps the same 45-degree raw
        # hillshade, but still requires a CELL-RGB refresh because the crisp
        # contrast gain itself changes.
        raw_shade_changed = cache.needs_shading_update(
            azimuth, light_elevation, ambient
        )
        if raw_shade_changed:
            if cache.face_normals is None or len(cache.face_normals) != len(cache.faces):
                cache.face_normals = _compute_face_normals(
                    cache.xyz_unique, cache.faces
                )
            cache.shade = _compute_face_shade(
                cache.xyz_unique,
                cache.faces,
                azimuth,
                light_elevation,
                ambient,
                face_normals=cache.face_normals,
            )
            cache.last_azimuth = azimuth
            cache.last_angle = light_elevation
            cache.last_ambient = ambient

        base_color_changed = bool(
            raw_shade_changed or crisp_sharpness_changed
        )

        crisp_hybrid = bool(
            getattr(app, '_shading_crisp_hybrid_active', False)
            and _crisp_multiclass_facets_enabled(app, cache)
        )

        if crisp_hybrid:
            # CRITICAL: never call _configure_microstation_color_blend_lighting
            # on the crisp base.  The initial renderer deliberately keeps that
            # actor unlit because its shade is already baked into CELL RGB.
            # Turning VTK lighting back on is what creates the dark dotted/
            # scan-line hatching seen after popup Apply.
            refreshed = True
            if base_color_changed:
                refreshed = _refresh_crisp_base_face_colors_in_place(
                    app, cache, classes_raw
                )
            else:
                # No lighting parameter changed; just re-assert the solid
                # presentation state without touching the CELL RGB buffer.
                refreshed = _enforce_crisp_base_actor_state(app)
            if not refreshed:
                # Safe correctness fallback: reuse the same cached topology and
                # the exact initial crisp renderer.  This still does NOT run
                # Delaunay/representative generation.
                print(
                    'SHADING_LIGHT_ONLY crisp_fallback=render_cached_topology '
                    'topology_rebuild=0'
                )
                _render_mesh(
                    app, cache, classes_raw, saved_camera,
                    cached_restore=True,
                )
                return True
            _enforce_crisp_base_actor_state(app)
            cache.last_crisp_sharpness = sharpness_angle
        else:
            _configure_microstation_color_blend_lighting(
                app, getattr(app, '_shaded_mesh_actor', None)
            )

        # Only presentation overlays are GPU-lit in crisp-hybrid mode.
        overlay_actor = getattr(
            app, '_shading_multiclass_color_overlay_actor', None
        )
        if overlay_actor is not None:
            _configure_microstation_color_blend_lighting(app, overlay_actor)
        for entry in (getattr(app, '_shading_static_blend_overlays', None) or []):
            overlay = entry.get('actor') if isinstance(entry, dict) else None
            if overlay is not None:
                _configure_microstation_color_blend_lighting(app, overlay)

        # Classification/undo/redo may have a tiny presentation mask above the
        # immutable base. Rebuild ONLY that dirty mask when lighting controls
        # change so its solid pure facets and mixed blend faces consume the
        # exact current Azimuth/Sharpness/Ambient. No global topology work.
        if crisp_hybrid:
            dirty = getattr(cache, '_multiclass_dirty_faces', None)
            if dirty:
                dirty_faces = np.fromiter(
                    dirty, dtype=np.int64, count=len(dirty)
                )
                dirty_faces = dirty_faces[
                    (dirty_faces >= 0) & (dirty_faces < len(cache.faces))
                ]
                if dirty_faces.size:
                    _rebuild_crisp_live_edit_overlays(
                        app, cache, classes_raw,
                        _get_shading_visibility(app), dirty_faces
                    )

        # Re-assert shading ownership before presenting.  Cross-section,
        # classification and LOD workflows can legitimately touch point-actor
        # visibility; if one becomes visible above the TIN it appears as dark
        # dotted/scan-line bands, not as a true triangle edge.
        _hide_point_cloud_actors_for_shading(app)
        if crisp_hybrid:
            _remove_shaded_edge_overlay(app)
            _enforce_crisp_base_actor_state(app)

        _restore_camera(app, saved_camera)
        app.vtk_widget.renderer.ResetCameraClippingRange()
        app.vtk_widget.render()
        print(
            'SHADING_LIGHT_ONLY status=updated_gpu_sharpness '
            f'azimuth={azimuth:.2f} sharpness={sharpness_angle:.2f} '
            f'light_elevation={light_elevation:.2f} ambient={ambient:.3f} '
            f'elapsed={(time.perf_counter()-t0)*1000:.1f}ms '
            f'crisp_hybrid={int(crisp_hybrid)} '
            'topology_rebuild=0 geometry_rebuild=0 '
            f'color_upload={int(crisp_hybrid and base_color_changed)} '
            f'raw_shade_update={int(raw_shade_changed)} '
            f'sharpness_refresh={int(crisp_sharpness_changed)} '
            'brightness_source=ambient'
        )
        return True

    # Single-class keeps the established legacy path exactly as before.
    legacy_angle = float(
        getattr(app, 'last_shade_angle', 45.0)
        if angle is None else angle
    )
    app.last_shade_angle = legacy_angle
    if cache.needs_shading_update(azimuth, legacy_angle, ambient):
        if cache.face_normals is None or len(cache.face_normals) != len(cache.faces):
            cache.face_normals = _compute_face_normals(
                cache.xyz_unique, cache.faces
            )
        cache.shade = _compute_face_shade(
            cache.xyz_unique,
            cache.faces,
            azimuth,
            legacy_angle,
            ambient,
            face_normals=cache.face_normals,
        )
        if getattr(cache, 'smooth_all_classes', False):
            if cache.vertex_normals is None or len(cache.vertex_normals) != len(cache.xyz_unique):
                cache.vertex_normals = _compute_vertex_normals(
                    cache.xyz_unique, cache.faces, cache.face_normals
                )
            cache.vertex_shade = _compute_shading(
                cache.vertex_normals,
                azimuth,
                legacy_angle,
                ambient,
                z_values=cache.xyz_unique[:, 2],
            )
        cache.last_azimuth = azimuth
        cache.last_angle = legacy_angle
        cache.last_ambient = ambient
        cache._vtk_colors_ptr = None

    _render_mesh(app, cache, classes_raw, saved_camera, cached_restore=True)
    print(
        'SHADING_LIGHT_ONLY status=updated_single_class '
        f'azimuth={azimuth:.2f} angle={legacy_angle:.2f} ambient={ambient:.3f} '
        f'elapsed={(time.perf_counter()-t0)*1000:.1f}ms topology_rebuild=0'
    )
    return True

class ShadingControlPanel(QWidget):
    def __init__(self, app):
        super().__init__(); self.app=app; self.setWindowTitle('Shading')
        layout=QVBoxLayout()
        rows=[
            ('Max edge (m):','max_edge',(1,1000),100,10),
            ('Azimuth:','az',(0,360),45,5),
            ('Sharpness:','el',(0,999),45,5),
            ('Ambient (brightness):','amb',(0,1),0.25,0.05),
        ]
        for label,attr,rng,default,step in rows:
            h=QHBoxLayout(); h.addWidget(QLabel(label)); spin=QDoubleSpinBox()
            spin.setRange(*rng); spin.setValue(default); spin.setSingleStep(step)
            setattr(self,attr,spin); h.addWidget(spin); layout.addLayout(h)
        try:
            self.el.setToolTip('0..90 keeps the established response; 91..999 adds progressive sharpness overdrive. Ambient controls brightness.')
            self.amb.setToolTip('Controls brightness/shadow floor without changing facet sharpness.')
        except Exception:
            pass
        btn=QPushButton('Apply'); btn.clicked.connect(self._on_apply); layout.addWidget(btn)
        rb=QPushButton('Full Rebuild'); rb.clicked.connect(self._on_full_rebuild); layout.addWidget(rb)
        self.setLayout(layout); self._restore_from_app()

    def _restore_from_app(self):
        self.az.setValue(float(getattr(self.app,'last_shade_azimuth',45.0)))
        self.el.setValue(_shading_sharpness_angle(self.app))
        self.amb.setValue(float(getattr(self.app,'shade_ambient',0.25)))

    def refresh_from_app(self):
        vals=[
            ('az',float(getattr(self.app,'last_shade_azimuth',45.0))),
            ('el',_shading_sharpness_angle(self.app)),
            ('amb',float(getattr(self.app,'shade_ambient',0.25))),
        ]
        for name,value in vals:
            spin=getattr(self,name); spin.blockSignals(True); spin.setValue(value); spin.blockSignals(False)

    def _on_apply(self):
        self.app.last_shade_azimuth=self.az.value()
        self.app.shading_sharpness_angle=self.el.value()
        self.app.shade_ambient=self.amb.value()
        update_shading_lighting_only(self.app,self.az.value(),self.el.value(),self.amb.value())

    def _on_full_rebuild(self):
        self.app.last_shade_azimuth=self.az.value()
        self.app.shading_sharpness_angle=self.el.value()
        self.app.shade_ambient=self.amb.value()
        clear_shading_cache('manual')
        update_shaded_class(
            self.app,self.az.value(),self.el.value(),self.amb.value(),
            force_rebuild=True,single_class_max_edge=self.max_edge.value(),
        )

__all__ = ['update_shaded_class', 'refresh_shaded_colors_fast', 'refresh_shaded_colors_only',
    'refresh_shaded_after_classification_fast', 'refresh_shaded_after_undo_fast',
    'refresh_shaded_after_history_fast',
    'refresh_shaded_after_visibility_change', 'handle_shaded_view_change',
    '_multi_class_region_undo_patch', 'ShadingControlPanel', 'clear_shading_cache',
    'detach_shading_before_non_shading_mode',
    'get_cache', 'has_cached_geometry', 'invalidate_cache_for_new_file',
    'clear_shading_representative_cache', 'update_shading_lighting_only',
    'on_class_visibility_changed', '_get_shading_visibility']