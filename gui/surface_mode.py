"""
Surface display mode integration.

This module is intentionally isolated so the finalized GUI code only needs small,
low-risk hooks in pointcloud_display.py, app_window.py, classification_tools.py,
classification_fast.py, menu_sidebar_system.py and display_mode.py.
"""

from __future__ import annotations

import time
import os
from collections import OrderedDict
from typing import Optional

import numpy as np
import pyvista as pv
from scipy.spatial import Delaunay, cKDTree

try:
    from PySide6.QtWidgets import QApplication, QProgressDialog, QWidget
    from PySide6.QtCore import QObject, QEvent, QEventLoop, Qt, QThread, Signal, Slot
except Exception:
    QApplication = None
    QProgressDialog = None
    QWidget = None
    QObject = object
    QEvent = None
    QEventLoop = None
    Qt = None
    QThread = None
    Signal = None
    Slot = None


SURFACE_ACTOR_NAME = "surface_mesh"


# Surface density controls mirror Shaded Classification.
_SURFACE_FAST_TARGET = 1_000_000
_SURFACE_NORMAL_TARGET = 3_000_000
_SURFACE_CACHE_MAX_ENTRIES = 2
_SURFACE_SLOW_RETAIN_MAX_POINTS = 5_000_000

try:
    import triangle as _surface_triangle
    _SURFACE_HAS_TRIANGLE = True
except Exception:
    _surface_triangle = None
    _SURFACE_HAS_TRIANGLE = False

try:
    from numba import njit, prange
    _SURFACE_HAS_NUMBA = True
except Exception:
    njit = None
    prange = range
    _SURFACE_HAS_NUMBA = False


def normalize_surface_quality(value):
    """Return persisted Surface quality key used by UI and caches."""
    quality = str(value or "normal").strip().lower()
    return quality if quality in ("fast", "normal", "slow") else "normal"


def _surface_quality_target(quality_mode: str, eligible_count: int) -> int:
    quality_mode = normalize_surface_quality(quality_mode)
    if quality_mode == "slow":
        return max(int(eligible_count), 3)
    if quality_mode == "fast":
        return min(max(int(eligible_count), 3), _SURFACE_FAST_TARGET)
    return min(max(int(eligible_count), 3), _SURFACE_NORMAL_TARGET)


if _SURFACE_HAS_NUMBA:
    @njit(cache=True)
    def _surface_dense_grid_count(points, precision, min_gx, min_gy, gx_span, gy_span):
        occupied = np.zeros(gx_span * gy_span, dtype=np.uint8)
        for i in range(points.shape[0]):
            gx = int(np.floor(points[i, 0] / precision)) - min_gx
            gy = int(np.floor(points[i, 1] / precision)) - min_gy
            occupied[gx * gy_span + gy] = 1
        count = 0
        for i in range(occupied.shape[0]):
            count += occupied[i]
        return count


    @njit(cache=True)
    def _surface_dense_grid_first(points, precision, min_gx, min_gy, gx_span, gy_span):
        """Select the first source point in every occupied XY grid cell."""
        best_idx = np.empty(gx_span * gy_span, dtype=np.int64)
        for c in range(best_idx.shape[0]):
            best_idx[c] = -1
        for i in range(points.shape[0]):
            gx = int(np.floor(points[i, 0] / precision)) - min_gx
            gy = int(np.floor(points[i, 1] / precision)) - min_gy
            key = gx * gy_span + gy
            if best_idx[key] < 0:
                best_idx[key] = i
        count = 0
        for c in range(best_idx.shape[0]):
            if best_idx[c] >= 0:
                count += 1
        out = np.empty(count, dtype=np.int64)
        j = 0
        for c in range(best_idx.shape[0]):
            idx = best_idx[c]
            if idx >= 0:
                out[j] = idx
                j += 1
        return out


def _surface_grid_shape(points: np.ndarray, precision: float):
    precision = max(float(precision), 1e-9)
    min_gx = int(np.floor(float(np.min(points[:, 0])) / precision))
    max_gx = int(np.floor(float(np.max(points[:, 0])) / precision))
    min_gy = int(np.floor(float(np.min(points[:, 1])) / precision))
    max_gy = int(np.floor(float(np.max(points[:, 1])) / precision))
    gx_span = max_gx - min_gx + 1
    gy_span = max_gy - min_gy + 1
    return min_gx, min_gy, gx_span, gy_span


def _surface_grid_count(points: np.ndarray, precision: float) -> int:
    min_gx, min_gy, gx_span, gy_span = _surface_grid_shape(points, precision)
    n_cells = int(gx_span) * int(gy_span)
    dense_limit = int(os.environ.get("NAKSHA_SURFACE_DENSE_COUNT_MAX_CELLS", "50000000"))
    if _SURFACE_HAS_NUMBA and 0 < n_cells <= dense_limit:
        try:
            return int(_surface_dense_grid_count(
                points, float(precision), min_gx, min_gy, gx_span, gy_span
            ))
        except Exception:
            pass

    grid = np.floor(points[:, :2] / float(precision)).astype(np.int64, copy=False)
    gx = grid[:, 0] - grid[:, 0].min()
    gy = grid[:, 1] - grid[:, 1].min()
    gy_span2 = int(gy.max()) + 1
    if gx.max() < 2**30 and gy_span2 < 2**30:
        return int(np.unique(gx * gy_span2 + gy).size)
    return int(np.unique(grid, axis=0).shape[0])


def _surface_grid_select(points: np.ndarray, precision: float) -> np.ndarray:
    min_gx, min_gy, gx_span, gy_span = _surface_grid_shape(points, precision)
    n_cells = int(gx_span) * int(gy_span)
    dense_limit = int(os.environ.get("NAKSHA_SURFACE_DENSE_GRID_MAX_CELLS", "12000000"))
    occupancy_ok = n_cells <= max(int(len(points) * 2), 1)
    if _SURFACE_HAS_NUMBA and 0 < n_cells <= dense_limit and occupancy_ok:
        try:
            result = _surface_dense_grid_first(
                points, float(precision), min_gx, min_gy, gx_span, gy_span
            )
            # Preserve the old source-order semantics used by _grid_dedup_indices.
            return np.sort(result.astype(np.int64, copy=False))
        except Exception:
            pass

    grid = np.floor(points[:, :2] / float(precision)).astype(np.int64, copy=False)
    gx = grid[:, 0] - grid[:, 0].min()
    gy = grid[:, 1] - grid[:, 1].min()
    gy_span2 = int(gy.max()) + 1
    if gx.max() < 2**30 and gy_span2 < 2**30:
        _, idx = np.unique(gx * gy_span2 + gy, return_index=True)
    else:
        _, idx = np.unique(grid, axis=0, return_index=True)
    return np.sort(idx.astype(np.int64, copy=False))


def _select_surface_representatives(
    local_points: np.ndarray,
    target_max: int,
    user_precision: float = 0.0,
):
    """Count-first Surface LOD selection, analogous to shading representatives."""
    n = int(len(local_points))
    target_max = max(3, min(int(target_max), n))
    if n <= target_max:
        return np.arange(n, dtype=np.int64), 0.0, {
            "strategy": "all_eligible", "selected": n, "target": target_max,
        }

    xr = max(float(np.ptp(local_points[:, 0])), 1e-9)
    yr = max(float(np.ptp(local_points[:, 1])), 1e-9)
    area = max(xr * yr, 1.0)

    # A slightly denser first estimate prevents irregular project outlines from
    # undershooting the requested representative count.
    precision = max(float(user_precision or 0.0), np.sqrt(area / target_max) * 0.82, 1e-6)
    count = _surface_grid_count(local_points, precision)

    # Two exact count-first corrections are enough in production and avoid the
    # old "dedup millions first, then stride them away" cost.
    for _ in range(2):
        if count <= 0:
            break
        ratio = float(count) / float(target_max)
        if 0.82 <= ratio <= 1.08:
            break
        precision = max(float(user_precision or 0.0), precision * np.sqrt(max(ratio, 1e-9)))
        count = _surface_grid_count(local_points, precision)

    idx = _surface_grid_select(local_points, precision)
    if len(idx) > target_max:
        # Deterministic, low-cost final cap. The grid step has already made the
        # candidates spatially representative; this only clips small overshoot.
        pick = np.linspace(0, len(idx) - 1, target_max, dtype=np.int64)
        idx = idx[pick]

    return idx.astype(np.int64, copy=False), float(precision), {
        "strategy": "grid_count_first",
        "selected": int(len(idx)),
        "target": int(target_max),
        "grid_count": int(count),
    }



if _SURFACE_HAS_NUMBA:
    @njit(cache=True, parallel=True)
    def _surface_edge_keep_mask_numba(faces, xy, max_edge_sq):
        """Exact long-edge predicate without allocating three Nx2 edge arrays."""
        n = faces.shape[0]
        keep = np.empty(n, dtype=np.bool_)
        for i in prange(n):
            i0 = int(faces[i, 0])
            i1 = int(faces[i, 1])
            i2 = int(faces[i, 2])

            dx01 = xy[i0, 0] - xy[i1, 0]
            dy01 = xy[i0, 1] - xy[i1, 1]
            dx12 = xy[i1, 0] - xy[i2, 0]
            dy12 = xy[i1, 1] - xy[i2, 1]
            dx20 = xy[i2, 0] - xy[i0, 0]
            dy20 = xy[i2, 1] - xy[i0, 1]

            e01 = dx01 * dx01 + dy01 * dy01
            e12 = dx12 * dx12 + dy12 * dy12
            e20 = dx20 * dx20 + dy20 * dy20
            keep[i] = (
                e01 <= max_edge_sq
                and e12 <= max_edge_sq
                and e20 <= max_edge_sq
            )
        return keep


    @njit(cache=True, parallel=True)
    def _surface_face_colors_numba(
        points,
        faces,
        light_x,
        light_y,
        light_z,
        ambient,
        z_lo,
        z_hi,
        stops,
        ramp_colors,
    ):
        """Compute the exact elevation-ramp + Lambert color in one streaming pass."""
        n = faces.shape[0]
        out = np.empty((n, 3), dtype=np.uint8)
        denom_z = z_hi - z_lo
        nstops = stops.shape[0]

        for i in prange(n):
            ia = int(faces[i, 0])
            ib = int(faces[i, 1])
            ic = int(faces[i, 2])

            ax = points[ia, 0]
            ay = points[ia, 1]
            az = points[ia, 2]
            bx = points[ib, 0]
            by = points[ib, 1]
            bz = points[ib, 2]
            cx = points[ic, 0]
            cy = points[ic, 1]
            cz = points[ic, 2]

            v1x = bx - ax
            v1y = by - ay
            v1z = bz - az
            v2x = cx - ax
            v2y = cy - ay
            v2z = cz - az

            nx = v1y * v2z - v1z * v2y
            ny = v1z * v2x - v1x * v2z
            nz = v1x * v2y - v1y * v2x
            nn = np.sqrt(nx * nx + ny * ny + nz * nz) + 1e-12
            dot = (nx * light_x + ny * light_y + nz * light_z) / nn
            if dot < 0.0:
                dot = 0.0
            elif dot > 1.0:
                dot = 1.0
            shade = ambient + (1.0 - ambient) * dot

            zn = ((az + bz + cz) / 3.0 - z_lo) / denom_z
            if zn < 0.0:
                zn = 0.0
            elif zn > 1.0:
                zn = 1.0

            # np.interp-equivalent linear interpolation over a tiny ramp.
            if zn <= stops[0]:
                r = ramp_colors[0, 0]
                g = ramp_colors[0, 1]
                b = ramp_colors[0, 2]
            elif zn >= stops[nstops - 1]:
                r = ramp_colors[nstops - 1, 0]
                g = ramp_colors[nstops - 1, 1]
                b = ramp_colors[nstops - 1, 2]
            else:
                hi = 1
                while hi < nstops and zn > stops[hi]:
                    hi += 1
                lo = hi - 1
                span = stops[hi] - stops[lo]
                if span <= 1e-15:
                    t = 0.0
                else:
                    t = (zn - stops[lo]) / span
                r = ramp_colors[lo, 0] + (ramp_colors[hi, 0] - ramp_colors[lo, 0]) * t
                g = ramp_colors[lo, 1] + (ramp_colors[hi, 1] - ramp_colors[lo, 1]) * t
                b = ramp_colors[lo, 2] + (ramp_colors[hi, 2] - ramp_colors[lo, 2]) * t

            r *= shade
            g *= shade
            b *= shade
            if r < 0.0:
                r = 0.0
            elif r > 255.0:
                r = 255.0
            if g < 0.0:
                g = 0.0
            elif g > 255.0:
                g = 255.0
            if b < 0.0:
                b = 0.0
            elif b > 255.0:
                b = 255.0

            out[i, 0] = np.uint8(r)
            out[i, 1] = np.uint8(g)
            out[i, 2] = np.uint8(b)

        return out

def _triangulate_surface_xy(xy: np.ndarray):
    xy = np.asarray(xy, dtype=np.float64)
    if xy.ndim != 2 or xy.shape[1] != 2 or len(xy) < 3:
        return np.empty((0, 3), dtype=np.int32), "none"
    if not np.isfinite(xy).all():
        return np.empty((0, 3), dtype=np.int32), "none"
    if np.ptp(xy[:, 0]) <= 1e-12 or np.ptp(xy[:, 1]) <= 1e-12:
        return np.empty((0, 3), dtype=np.int32), "none"

    if _SURFACE_HAS_TRIANGLE:
        try:
            result = _surface_triangle.triangulate(
                {"vertices": np.ascontiguousarray(xy, dtype=np.float64)}, "Qz"
            )
            faces = result.get("triangles") if isinstance(result, dict) else None
            if faces is not None and len(faces):
                return np.asarray(faces, dtype=np.int32), "triangle"
        except Exception as exc:
            print(f"SURFACE_TRIANGULATOR fallback=scipy reason={exc}")

    return Delaunay(xy).simplices.astype(np.int32, copy=False), "scipy"


if QEvent is not None:
    _SURFACE_BLOCKED_EVENT_TYPES = {
        QEvent.MouseButtonPress,
        QEvent.MouseButtonRelease,
        QEvent.MouseButtonDblClick,
        QEvent.MouseMove,
        QEvent.Wheel,
        QEvent.KeyPress,
        QEvent.KeyRelease,
        QEvent.Shortcut,
        QEvent.ShortcutOverride,
        QEvent.ContextMenu,
        QEvent.TabletPress,
        QEvent.TabletRelease,
        QEvent.TabletMove,
    }
else:
    _SURFACE_BLOCKED_EVENT_TYPES = set()


class _SurfaceInputBlocker(QObject):
    """
    Temporary application-wide input blocker used while Surface is building.

    Paint/timer/progress events are allowed, but mouse/keyboard/wheel events are
    swallowed. This prevents repeated Surface clicks or canvas clicks from being
    delivered while a heavy triangulation is in progress.
    """

    def __init__(self, app):
        try:
            super().__init__(app)
        except Exception:
            try:
                super().__init__()
            except Exception:
                pass
        self.app = app

    def eventFilter(self, obj, event):
        try:
            if not bool(getattr(self.app, "_surface_building", False)):
                return False
            if event is not None and event.type() in _SURFACE_BLOCKED_EVENT_TYPES:
                if hasattr(event, "ignore"):
                    event.ignore()
                return True
        except Exception:
            return False
        return False


if QProgressDialog is not None:
    class _SurfaceProgressDialog(QProgressDialog):
        """Non-closable Surface progress popup."""

        def __init__(self, parent=None):
            super().__init__("Building Surface mesh...\nPlease wait.", None, 0, 100, parent)
            self._allow_close = False
            self.setWindowTitle("Building Surface")
            if Qt is not None:
                self.setWindowModality(Qt.ApplicationModal)
                self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.CustomizeWindowHint)
            self.setCancelButton(None)
            self.setMinimumDuration(0)
            self.setAutoClose(False)
            self.setAutoReset(False)

        def event(self, event):
            if (
                not self._allow_close
                and event is not None
                and QEvent is not None
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
else:
    _SurfaceProgressDialog = None


if QThread is not None and Signal is not None:
    class _SurfaceComputationWorker(QThread):
        finished_signal = Signal(dict)
        error_signal = Signal(str)
        progress_signal = Signal(int, str)

        def __init__(
            self,
            xyz_all,
            global_idx,
            precision,
            target_max,
            max_edge,
            azimuth,
            angle,
            ambient,
            ramp,
            quality_mode="normal",
            z_bounds=None,
        ):
            super().__init__()
            self.xyz_all = xyz_all
            self.global_idx = global_idx
            self.precision = precision
            self.target_max = target_max
            self.max_edge = max_edge
            self.azimuth = azimuth
            self.angle = angle
            self.ambient = ambient
            self.ramp = ramp
            self.quality_mode = normalize_surface_quality(quality_mode)
            self.z_bounds = z_bounds

        def run(self):
            try:
                res = _compute_surface_geometry_backend(
                    self.xyz_all,
                    self.global_idx,
                    self.precision,
                    self.target_max,
                    self.max_edge,
                    self.azimuth,
                    self.angle,
                    self.ambient,
                    self.ramp,
                    progress_callback=self.progress_signal.emit,
                    quality_mode=self.quality_mode,
                    z_bounds=self.z_bounds,
                )
                self.finished_signal.emit(res)
            except Exception:
                import traceback
                self.error_signal.emit(traceback.format_exc())
else:
    _SurfaceComputationWorker = None


if QObject is not object and Slot is not None:
    class _SurfaceAsyncResultRelay(QObject):
        """Deliver worker results on the GUI thread before touching VTK/Qt."""

        def __init__(self, on_finish, on_error, on_progress, parent=None):
            super().__init__(parent)
            self._on_finish = on_finish
            self._on_error = on_error
            self._on_progress = on_progress

        @Slot(dict)
        def handle_finished(self, result):
            self._on_finish(result)

        @Slot(str)
        def handle_error(self, error_text):
            self._on_error(error_text)

        @Slot(int, str)
        def handle_progress(self, value, message):
            self._on_progress(value, message)
else:
    _SurfaceAsyncResultRelay = None


def _surface_process_progress_events():
    """Keep progress painting alive without accepting user input."""
    try:
        if QApplication is None:
            return
        app_qt = QApplication.instance()
        if app_qt is None:
            return
        if QEventLoop is not None:
            app_qt.processEvents(QEventLoop.ExcludeUserInputEvents)
        else:
            app_qt.processEvents()
    except Exception:
        pass


def _normalize_surface_ramp(ramp):
    normalized = []
    if not ramp:
        return normalized

    for pos, color in ramp:
        try:
            rgb = _as_palette_color(color)
            normalized.append((float(pos), rgb))
        except Exception:
            continue
    return normalized


def _compute_surface_face_colors(
    points: np.ndarray,
    faces: np.ndarray,
    azimuth: float,
    angle: float,
    ambient: float,
    z_lo: float,
    z_hi: float,
    ramp=None,
) -> np.ndarray:
    """Compute Surface cell colors without multi-GB temporary triangle arrays.

    The mathematical result is unchanged: elevation ramp * Lambert shade.
    Numba is used when available; otherwise the same NumPy math is executed in
    bounded chunks so Slow/all-point Surface does not allocate ``points[faces]``
    for tens of millions of triangles at once.
    """
    if faces is None or len(faces) == 0:
        return np.zeros((0, 3), dtype=np.uint8)

    pts = np.asarray(points, dtype=np.float64)
    f = np.asarray(faces, dtype=np.int32)

    az = np.deg2rad(float(azimuth))
    el = np.deg2rad(float(angle))
    light = np.array(
        [np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)],
        dtype=np.float64,
    )
    light /= np.linalg.norm(light) + 1e-12

    z = pts[:, 2]
    lo = float(z_lo)
    hi = float(z_hi)
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        lo = float(np.percentile(z, 1.0))
        hi = float(np.percentile(z, 99.0))
        if hi <= lo:
            lo, hi = float(np.min(z)), float(np.max(z))
    if hi <= lo:
        hi = lo + 1.0

    valid_ramp = None
    if ramp:
        try:
            stops = np.asarray([float(pos) for pos, _ in ramp], dtype=np.float64)
            colors = np.asarray([list(color[:3]) for _, color in ramp], dtype=np.float64)
            if len(stops) >= 2 and len(colors) == len(stops):
                valid_ramp = (stops, colors)
        except Exception:
            valid_ramp = None

    if valid_ramp is None:
        stops = np.array([0.00, 0.25, 0.50, 0.75, 1.00], dtype=np.float64)
        colors = np.array(
            [
                [0, 0, 255],
                [0, 200, 255],
                [0, 255, 0],
                [255, 255, 0],
                [255, 80, 0],
            ],
            dtype=np.float64,
        )
    else:
        stops, colors = valid_ramp

    # Fast exact streaming kernel. This avoids ~5-7 GB of temporary arrays on
    # a 60-70M-face Slow Surface.
    if _SURFACE_HAS_NUMBA:
        try:
            return _surface_face_colors_numba(
                np.ascontiguousarray(pts),
                np.ascontiguousarray(f),
                float(light[0]),
                float(light[1]),
                float(light[2]),
                float(ambient),
                float(lo),
                float(hi),
                np.ascontiguousarray(stops),
                np.ascontiguousarray(colors),
            )
        except Exception as exc:
            print(f"SURFACE_ACCEL face_colors=fallback reason={type(exc).__name__}: {exc}")

    # Memory-bounded NumPy fallback. Keep the chunk modest because each chunk
    # still materializes three triangle point arrays and cross-product vectors.
    chunk = max(50_000, int(os.environ.get("NAKSHA_SURFACE_COLOR_CHUNK", "750000")))
    out = np.empty((len(f), 3), dtype=np.uint8)
    ambient_f = float(ambient)
    z_span = float(hi - lo)

    for begin in range(0, len(f), chunk):
        finish = min(begin + chunk, len(f))
        fc = f[begin:finish]
        p0 = pts[fc[:, 0]]
        p1 = pts[fc[:, 1]]
        p2 = pts[fc[:, 2]]
        v1 = p1 - p0
        v2 = p2 - p0
        normals = np.cross(v1, v2)
        normals /= np.linalg.norm(normals, axis=1, keepdims=True) + 1e-12
        shade = ambient_f + (1.0 - ambient_f) * np.clip(normals @ light, 0.0, 1.0)

        z_face = (p0[:, 2] + p1[:, 2] + p2[:, 2]) / 3.0
        norm = np.clip((z_face - lo) / z_span, 0.0, 1.0)
        rgb = np.column_stack(
            [
                np.interp(norm, stops, colors[:, 0]),
                np.interp(norm, stops, colors[:, 1]),
                np.interp(norm, stops, colors[:, 2]),
            ]
        )
        rgb *= shade[:, None]
        out[begin:finish] = np.clip(rgb, 0, 255).astype(np.uint8)

    return out


def _compute_surface_geometry_backend(
    xyz_all: np.ndarray,
    global_idx: np.ndarray,
    precision: float,
    target_max: int,
    max_edge: float,
    azimuth: float,
    angle: float,
    ambient: float,
    ramp,
    progress_callback=None,
    quality_mode="normal",
    z_bounds=None,
) -> dict:
    profile_start = time.perf_counter()
    timings = {}
    last = profile_start

    def _checkpoint(name):
        nonlocal last
        now = time.perf_counter()
        timings[name] = now - last
        last = now

    def _emit(value, message):
        if callable(progress_callback):
            try:
                progress_callback(int(value), str(message))
            except Exception:
                pass

    if global_idx is None or int(getattr(global_idx, "size", 0)) < 3:
        return {"empty": True, "profile_timings": timings}

    quality_mode = normalize_surface_quality(quality_mode)
    xyz = xyz_all[global_idx].astype(np.float64, copy=False)
    if len(xyz) < 3:
        return {"empty": True, "profile_timings": timings}
    _checkpoint("visible_xyz_copy")

    _emit(20, f"Filtering Surface points ({quality_mode})...")
    finite = np.isfinite(xyz).all(axis=1)
    if not np.all(finite):
        xyz = xyz[finite]
        global_idx = global_idx[finite]
    if len(xyz) < 3:
        return {"empty": True, "profile_timings": timings}
    _checkpoint("finite_filter")

    # Only XY needs normalization for representative selection/triangulation.
    # The previous Slow path materialized two separate Nx3 translated arrays
    # (``local`` and ``local_pts``) before making the Nx2 triangulation buffer.
    # At 34M points that was ~2.2 GB of avoidable memory traffic.  Preserve the
    # exact same local XY coordinates while keeping global XYZ only once.
    offset = xyz.min(axis=0)
    max_xyz = xyz.max(axis=0)
    xy_range = max_xyz[:2] - offset[:2]
    area = max(float(xy_range[0] * xy_range[1]), 1.0)
    spacing = float(np.sqrt(area / max(len(xyz), 1)))
    local_xy = np.ascontiguousarray(xyz[:, :2] - offset[:2], dtype=np.float64)
    _checkpoint("normalize_extent")

    target_max = _surface_quality_target(quality_mode, len(xyz))
    user_precision = max(float(precision or 0.0), 0.0)

    _emit(35, f"Selecting {quality_mode.title()} Surface representatives...")
    if quality_mode == "slow":
        # Do not allocate a 26M-entry arange just to select every point.
        unique_local_idx = None
        selected_precision = 0.0
        rep_meta = {
            "strategy": "all_points",
            "selected": int(len(xyz)),
            "target": int(target_max),
        }
    elif len(xyz) <= target_max:
        unique_local_idx = np.arange(len(xyz), dtype=np.int64)
        selected_precision = 0.0
        rep_meta = {
            "strategy": "under_target",
            "selected": int(len(xyz)),
            "target": int(target_max),
        }
    else:
        # _select_surface_representatives only reads columns 0/1, so an Nx2
        # local array is behavior-identical to the previous Nx3 local array.
        unique_local_idx, selected_precision, rep_meta = _select_surface_representatives(
            local_xy, target_max, user_precision
        )
    _checkpoint("representative_select")

    if unique_local_idx is not None and len(unique_local_idx) < 3:
        return {"empty": True, "profile_timings": timings}

    if unique_local_idx is None or len(unique_local_idx) == len(xyz):
        # Avoid a second full copy in Slow/all-points mode.
        pts = xyz
        xy = local_xy
        unique_global_idx = global_idx.astype(np.int64, copy=False)
    else:
        pts = xyz[unique_local_idx].copy()
        xy = np.ascontiguousarray(local_xy[unique_local_idx], dtype=np.float64)
        unique_global_idx = global_idx[unique_local_idx].astype(np.int64, copy=False)
        del local_xy
    _checkpoint("representative_materialize")

    _emit(55, f"Triangulating Surface mesh ({quality_mode})...")
    faces, triangulator = _triangulate_surface_xy(xy)
    _checkpoint("delaunay")
    if len(faces) == 0:
        return {"empty": True, "profile_timings": timings}

    data_extent = max(float(np.ptp(xy[:, 0])), float(np.ptp(xy[:, 1])), 1.0)
    max_edge = float(max_edge or 0.0)
    if max_edge <= 0.0:
        max_edge = max(data_extent * 0.10, spacing * 100.0)

    _emit(75, "Cleaning long Surface triangles...")
    faces = _filter_long_edges(faces, xy, max_edge)
    _checkpoint("edge_filter")
    if len(faces) == 0:
        return {"empty": True, "profile_timings": timings}

    if z_bounds is not None and len(z_bounds) >= 2:
        z_lo, z_hi = float(z_bounds[0]), float(z_bounds[1])
    else:
        z_lo = float(np.percentile(xyz_all[:, 2], 1.0))
        z_hi = float(np.percentile(xyz_all[:, 2], 99.0))
    _checkpoint("z_bounds")

    _emit(88, "Applying Surface elevation shading...")
    colors = _compute_surface_face_colors(
        pts, faces, azimuth, angle, ambient, z_lo, z_hi, ramp=ramp
    )
    _checkpoint("face_colors")
    timings["total_backend"] = time.perf_counter() - profile_start

    return {
        "empty": False,
        "points": pts,
        "faces": faces,
        "colors": colors,
        "unique_global_idx": unique_global_idx,
        "z_lo": z_lo,
        "z_hi": z_hi,
        "quality_mode": quality_mode,
        "triangulator": triangulator,
        "representative_meta": rep_meta,
        "selected_precision": selected_precision,
        "profile_timings": timings,
    }


def _surface_disable_interaction_widgets(app):
    """
    Temporarily disable the main window's interactive child widgets.

    The event filter still swallows queued mouse/key events, but disabling the
    central widgets and VTK interactor adds a second safety net for very fast
    repeated clicks while the UI thread is busy triangulating.
    """
    disabled = []
    if QWidget is None or app is None:
        return disabled

    seen = set()

    def _remember(widget):
        if widget is None:
            return
        marker = id(widget)
        if marker in seen:
            return
        seen.add(marker)
        try:
            was_enabled = bool(widget.isEnabled())
        except Exception:
            return
        if not was_enabled:
            return
        try:
            widget.setEnabled(False)
            disabled.append((widget, was_enabled))
        except Exception:
            pass

    try:
        _remember(getattr(app, "centralWidget", lambda: None)())
    except Exception:
        pass

    try:
        _remember(getattr(app, "menuBar", lambda: None)())
    except Exception:
        pass

    try:
        _remember(getattr(app, "statusBar", lambda: None)())
    except Exception:
        pass

    try:
        vtk_widget = getattr(app, "vtk_widget", None)
        _remember(vtk_widget)
        _remember(getattr(vtk_widget, "interactor", None) if vtk_widget is not None else None)
    except Exception:
        pass

    try:
        _remember(getattr(app, "display_mode_dialog", None))
    except Exception:
        pass

    try:
        _remember(getattr(app, "display_dialog", None))
    except Exception:
        pass

    return disabled


def _surface_restore_interaction_widgets(app):
    disabled = getattr(app, "_surface_disabled_widgets", None)
    if not disabled:
        return

    for widget, was_enabled in reversed(disabled):
        if not was_enabled:
            continue
        try:
            widget.setEnabled(True)
        except Exception:
            pass

    try:
        app._surface_disabled_widgets = []
    except Exception:
        pass


def _surface_begin_build_guard(app, block_input=True):
    """Mark Surface busy and optionally install the temporary input blocker."""
    blocker = None
    try:
        app._surface_building = True
        app._surface_busy = True
        app._surface_input_blocked = bool(block_input)
        app._surface_disabled_widgets = []
        app._surface_wait_cursor_active = False
    except Exception:
        pass

    if block_input and QApplication is not None:
        try:
            app_qt = QApplication.instance()
            if app_qt is not None:
                blocker = _SurfaceInputBlocker(app)
                app_qt.installEventFilter(blocker)
                app._surface_input_blocker = blocker
        except Exception:
            blocker = None

        try:
            app._surface_disabled_widgets = _surface_disable_interaction_widgets(app)
        except Exception:
            app._surface_disabled_widgets = []

        try:
            if Qt is not None and app_qt is not None:
                app_qt.setOverrideCursor(Qt.WaitCursor)
                app._surface_wait_cursor_active = True
        except Exception:
            try:
                app._surface_wait_cursor_active = False
            except Exception:
                pass

    return blocker


def _surface_finish_build_guard(app, blocker=None):
    """
    Remove Surface busy state safely.

    Before removing the filter, process pending input once while the blocker is
    still installed so repeated clicks made during the build are swallowed rather
    than delivered after the progress popup closes.
    """
    try:
        app_qt = QApplication.instance() if QApplication is not None else None
        if app_qt is not None and blocker is not None:
            try:
                app_qt.processEvents()
            except Exception:
                pass
            try:
                app_qt.removeEventFilter(blocker)
            except Exception:
                pass
    except Exception:
        pass

    try:
        if getattr(app, "_surface_input_blocker", None) is blocker:
            app._surface_input_blocker = None
    except Exception:
        pass

    try:
        _surface_restore_interaction_widgets(app)
    except Exception:
        pass

    try:
        if bool(getattr(app, "_surface_wait_cursor_active", False)) and QApplication is not None:
            app_qt = QApplication.instance()
            if app_qt is not None:
                app_qt.restoreOverrideCursor()
    except Exception:
        pass

    try:
        app._surface_building = False
        app._surface_busy = False
        app._surface_input_blocked = False
        app._surface_wait_cursor_active = False
    except Exception:
        pass

def _as_palette_color(color):
    try:
        if hasattr(color, "red"):
            return (int(color.red()), int(color.green()), int(color.blue()))
        return tuple(int(c) for c in color[:3])
    except Exception:
        return (128, 128, 128)


def surface_palette(app) -> dict:
    dlg = getattr(app, "display_mode_dialog", None) or getattr(app, "display_dialog", None)
    if dlg is not None:
        try:
            vp = getattr(dlg, "view_palettes", None)
            if isinstance(vp, dict) and vp.get(0):
                return vp[0]
        except Exception:
            pass
    return getattr(app, "class_palette", {}) or {}


def _surface_support_entry(code: int, entry: dict) -> bool:
    """Return whether one class participates in the current Surface.

    Display Mode checkboxes are authoritative for Surface membership. This makes
    single-class, multi-class and all-class Surface use the exact same semantics.
    Optional ``surface`` / ``surface_support`` keys remain valid explicit project
    overrides, but no LAS class is silently removed merely because of its code.
    """
    if not entry.get("show", True):
        return False
    if entry.get("surface", None) is not None:
        return bool(entry.get("surface"))
    if entry.get("surface_support", None) is not None:
        return bool(entry.get("surface_support"))
    return True


def surface_visible_mask(app) -> np.ndarray:
    xyz = app.data.get("xyz") if getattr(app, "data", None) else None
    if xyz is None:
        return np.zeros(0, dtype=bool)

    classes = app.data.get("classification")
    from gui.flight_line_filter import flight_line_visibility_mask
    line_mask = flight_line_visibility_mask(app, len(xyz))
    if classes is None:
        return line_mask

    classes_arr = np.asarray(classes)
    palette = surface_palette(app)
    if not palette:
        # Before the Display Mode palette exists, preserve the historical
        # all-class Surface behavior (flight-line visibility still applies).
        return line_mask

    # An explicit palette is an exact membership request. Start hidden and turn
    # on only checked classes, otherwise an unknown/unlisted class can leak into
    # a requested single-class Surface.
    if classes_arr.dtype == np.uint8:
        lut = np.zeros(256, dtype=bool)
        for code, entry in palette.items():
            try:
                code_int = int(code)
                if 0 <= code_int < 256:
                    lut[code_int] = _surface_support_entry(code_int, entry)
            except Exception:
                continue
        return lut[classes_arr] & line_mask

    supported = []
    for code, entry in palette.items():
        try:
            code_int = int(code)
        except Exception:
            continue
        if _surface_support_entry(code_int, entry):
            supported.append(code_int)
    if not supported:
        return np.zeros(len(xyz), dtype=bool)
    return np.isin(classes_arr, np.asarray(supported, dtype=classes_arr.dtype)) & line_mask

def _class_is_surface_support(app, code: int) -> bool:
    """
    True if this class currently participates in Surface mesh generation.
    Used to avoid unnecessary Surface rebuilds after classification.
    """
    try:
        palette = surface_palette(app)
        if not palette:
            return True
        code_int = int(code)
        entry = palette.get(code_int)
        if entry is None:
            return False
        return _surface_support_entry(code_int, entry)
    except Exception:
        return True


def surface_class_change_needs_rebuild(app, old_classes, new_class) -> bool:
    """
    Rebuild Surface only when classification changes Surface membership.
    """
    try:
        old_classes = np.asarray(old_classes)
        if old_classes.size == 0:
            return False

        new_support = _class_is_surface_support(app, int(new_class))

        for old_code in np.unique(old_classes):
            old_support = _class_is_surface_support(app, int(old_code))
            if old_support != new_support:
                return True

        return False
    except Exception:
        return True



def _surface_support_flags_for_values(app, values: np.ndarray) -> np.ndarray:
    """Return Surface-membership flags for a small classification value array.

    This is intentionally O(K), where K is the number of edited points.  It
    must never scan the complete point cloud during a classification refresh.
    """
    arr = np.asarray(values).ravel()
    if arr.size == 0:
        return np.zeros(0, dtype=bool)

    palette = surface_palette(app)
    if not palette:
        return np.ones(arr.size, dtype=bool)

    # Use the exact same checked-class membership LUT as the full Surface. This
    # keeps classification, Undo and Redo consistent with the active filter in
    # O(K), where K is only the edited points.
    if np.issubdtype(arr.dtype, np.integer):
        try:
            amin = int(arr.min())
            amax = int(arr.max())
        except Exception:
            amin, amax = -1, 256
        if amin >= 0 and amax < 256:
            lut = np.zeros(256, dtype=bool)
            for code, entry in palette.items():
                try:
                    code_int = int(code)
                    if 0 <= code_int < 256:
                        lut[code_int] = _surface_support_entry(code_int, entry)
                except Exception:
                    continue
            return lut[arr.astype(np.uint8, copy=False)]

    out = np.zeros(arr.size, dtype=bool)
    for code in np.unique(arr):
        code_int = int(code)
        entry = palette.get(code_int)
        if entry is not None:
            out[arr == code] = _surface_support_entry(code_int, entry)
    return out


def _surface_indices_from_step(step, total_points: int):
    """Return the point indices represented by an undo/redo step."""
    if not isinstance(step, dict):
        return None

    indices = step.get("indices")
    if indices is not None:
        try:
            idx = np.asarray(indices, dtype=np.int64).ravel()
            if idx.size:
                return idx
        except Exception:
            pass

    mask = step.get("mask")
    if isinstance(mask, np.ndarray) and mask.dtype == bool and mask.ndim == 1:
        if len(mask) == int(total_points):
            return np.flatnonzero(mask).astype(np.int64, copy=False)
    return None


def _surface_align_step_values(step_indices, values, changed_indices):
    """Align step values to the sorted indices from changed_mask."""
    if step_indices is None or values is None:
        return None
    step_indices = np.asarray(step_indices, dtype=np.int64).ravel()
    changed_indices = np.asarray(changed_indices, dtype=np.int64).ravel()
    vals = np.asarray(values).ravel()

    if vals.size == 1 and step_indices.size > 1:
        vals = np.full(step_indices.size, vals[0], dtype=vals.dtype)
    if vals.size != step_indices.size or step_indices.size != changed_indices.size:
        return None

    if np.array_equal(step_indices, changed_indices):
        return vals

    order = np.argsort(step_indices)
    sorted_idx = step_indices[order]
    pos = np.searchsorted(sorted_idx, changed_indices)
    if np.any(pos >= sorted_idx.size):
        return None
    if not np.array_equal(sorted_idx[pos], changed_indices):
        return None
    return vals[order[pos]]


def _surface_resolve_classification_transition(app, changed_mask, operation="classification"):
    """Recover old/new classes for the current edit without scanning all points.

    The hot path uses the exact sparse indices already stored by Naksha's
    classification transaction.  ``np.flatnonzero(changed_mask)`` is only a
    last-resort fallback, so 50M/300M-point projects do not pay an O(N) mask
    scan merely to decide that Surface topology did not change.
    """
    try:
        classes = app.data.get("classification") if getattr(app, "data", None) else None
    except Exception:
        classes = None
    if classes is None:
        return None

    total = len(classes)
    changed_mask = np.asarray(changed_mask) if changed_mask is not None else None
    if (
        changed_mask is None
        or changed_mask.dtype != bool
        or changed_mask.ndim != 1
        or len(changed_mask) != total
    ):
        return None

    op = str(operation or "classification").lower()

    step = None
    reverse = False
    if "undo" in op:
        stack = getattr(app, "redo_stack", None)
        if isinstance(stack, list) and stack:
            step = stack[-1]
            reverse = True
    elif "redo" in op:
        stack = getattr(app, "undo_stack", None)
        if isinstance(stack, list) and stack:
            step = stack[-1]
    else:
        stack = getattr(app, "undo_stack", None)
        if isinstance(stack, list) and stack:
            step = stack[-1]

    # Prefer sparse transaction indices.  These are authoritative and were
    # already computed by the classifier/undo system.
    changed_idx = None
    pending = None
    if "undo" not in op and "redo" not in op:
        pending = getattr(app, "_pending_surface_delta", None)
        if isinstance(pending, dict):
            pidx = np.asarray(pending.get("indices", []), dtype=np.int64).ravel()
            if pidx.size:
                try:
                    if np.all(changed_mask[pidx]):
                        changed_idx = pidx
                except Exception:
                    changed_idx = None

    if changed_idx is None and isinstance(step, dict):
        sidx = _surface_indices_from_step(step, total)
        if sidx is not None and sidx.size:
            try:
                if np.all(changed_mask[sidx]):
                    changed_idx = np.asarray(sidx, dtype=np.int64).ravel()
            except Exception:
                changed_idx = None

    if changed_idx is None and "undo" not in op and "redo" not in op:
        cached = getattr(app, "_last_changed_indices", None)
        if isinstance(cached, np.ndarray) and cached.size:
            cidx = np.asarray(cached, dtype=np.int64).ravel()
            try:
                if np.all(changed_mask[cidx]):
                    changed_idx = cidx
            except Exception:
                changed_idx = None

    if changed_idx is None:
        changed_idx = np.flatnonzero(changed_mask).astype(np.int64, copy=False)

    if changed_idx.size == 0:
        return {
            "indices": changed_idx,
            "old_classes": np.empty(0, dtype=np.asarray(classes).dtype),
            "new_classes": np.empty(0, dtype=np.asarray(classes).dtype),
            "source": "empty",
            "transaction_token": None,
        }

    # Explicit/pending transaction is primarily for signal-based callers that
    # apply classification before the Surface callback executes.
    if isinstance(pending, dict):
        pidx = np.asarray(pending.get("indices", []), dtype=np.int64).ravel()
        if pidx.size == changed_idx.size:
            oldv = _surface_align_step_values(pidx, pending.get("old_classes"), changed_idx)
            newv = _surface_align_step_values(pidx, pending.get("new_classes"), changed_idx)
            if oldv is not None and newv is not None:
                try:
                    current = np.asarray(classes)[changed_idx]
                    if np.array_equal(current, np.asarray(newv, dtype=current.dtype)):
                        return {
                            "indices": changed_idx,
                            "old_classes": np.asarray(oldv),
                            "new_classes": np.asarray(newv),
                            "source": "pending",
                            "transaction_token": ("pending", id(pending)),
                        }
                except Exception:
                    pass

    if not isinstance(step, dict):
        return None

    step_idx = _surface_indices_from_step(step, total)
    if step_idx is None:
        return None

    if reverse:
        old_values = step.get("new_classes")
        new_values = step.get("old_classes")
    else:
        old_values = step.get("old_classes")
        new_values = step.get("new_classes")

    oldv = _surface_align_step_values(step_idx, old_values, changed_idx)
    newv = _surface_align_step_values(step_idx, new_values, changed_idx)
    if oldv is None or newv is None:
        return None

    try:
        current = np.asarray(classes)[changed_idx]
        if not np.array_equal(current, np.asarray(newv, dtype=current.dtype)):
            return None
    except Exception:
        return None

    return {
        "indices": changed_idx,
        "old_classes": np.asarray(oldv),
        "new_classes": np.asarray(newv),
        "source": "redo_stack" if reverse else "undo_stack",
        "transaction_token": (
            "step", id(step), "reverse" if reverse else "forward"
        ),
    }


def _surface_transition_membership_summary(app, transition):
    """Return Surface support add/remove counts for an O(K) edit transition."""
    if not isinstance(transition, dict):
        return None
    oldv = np.asarray(transition.get("old_classes", [])).ravel()
    newv = np.asarray(transition.get("new_classes", [])).ravel()
    if oldv.size != newv.size:
        return None
    old_support = _surface_support_flags_for_values(app, oldv)
    new_support = _surface_support_flags_for_values(app, newv)
    add = (~old_support) & new_support
    remove = old_support & (~new_support)
    return {
        "add_count": int(np.count_nonzero(add)),
        "remove_count": int(np.count_nonzero(remove)),
        "same_count": int(oldv.size - np.count_nonzero(add) - np.count_nonzero(remove)),
        "topology_changed": bool(np.any(add) or np.any(remove)),
    }


def _retag_surface_cache_after_classification_no_topology(app) -> bool:
    """Retag the resident Surface cache after a metadata-only class edit.

    Surface colors and geometry depend on XYZ/support membership, not the class
    code itself.  When support membership is unchanged the existing mesh is
    still exact.  Re-keying the cache prevents the next Surface mode switch from
    rebuilding only because classification_revision advanced.
    """
    try:
        old_sig = getattr(app, "_surface_resident_signature", None)
        revision = int(getattr(app, "classification_revision", 0) or 0)
        cache_revision = int(getattr(app, "_surface_cache_revision", 0) or 0)

        # O(1) signature update: preserve the geometry/filter portion that made
        # this mesh valid and advance only revision fields.
        if isinstance(old_sig, tuple) and len(old_sig) >= 6:
            new_sig = tuple(old_sig[:4]) + (revision, cache_revision) + tuple(old_sig[6:])
        else:
            # Rare compatibility fallback.  Avoid this in the normal hot path.
            new_sig = surface_mesh_cache_signature(app)

        store = _surface_cache_store_for_app(app)
        entry = store.pop(old_sig, None) if old_sig is not None else None
        if entry is None:
            legacy = getattr(app, "_surface_mesh_cache", None)
            if isinstance(legacy, dict):
                entry = legacy

        if isinstance(entry, dict):
            entry["signature"] = new_sig
            store[new_sig] = entry
            store.move_to_end(new_sig)
            while len(store) > _SURFACE_CACHE_MAX_ENTRIES:
                store.popitem(last=False)
            app._surface_mesh_cache = entry

        app._surface_resident_signature = new_sig
        app._surface_shortcut_signature = new_sig
        app._surface_mesh_cache_dirty = False
        app._surface_filter_dirty = False
        app._surface_needs_rebuild_after_classification = False
        try:
            _remember_surface_signature(app)
        except Exception:
            pass
        return True
    except Exception as exc:
        print(f"⚠️ Surface cache retag skipped: {exc}")
        return False


def surface_visible_class_signature(app):
    """Return the Surface-support policy signature without scanning point data.

    Surface geometry depends on whether a class participates in the Surface,
    not on whether that class happens to be present in the current LAS array.
    Using the palette/support policy here removes an expensive ``np.unique``
    over millions of classifications from cache checks and live refreshes.
    """
    try:
        palette = surface_palette(app)
        if not palette:
            return ("ELEVATION_SURFACE_ALL_CLASSES",)

        supported = []
        for code, entry in palette.items():
            try:
                code_int = int(code)
            except Exception:
                continue
            if _surface_support_entry(code_int, entry):
                supported.append(code_int)

        return tuple(sorted(set(supported)))
    except Exception:
        return ("SURFACE_SIGNATURE_ERROR",)


def _remember_surface_signature(app) -> None:
    """
    Remember the class-visibility signature that produced the active Surface.

    This lets duplicate Surface clicks be ignored safely after a successful
    build or cache restore instead of being misread as a filter change.
    """
    try:
        app._surface_visible_class_signature = surface_visible_class_signature(app)
        app._surface_quality_signature = normalize_surface_quality(
            getattr(app, "surface_quality", "normal")
        )
        app._surface_last_completed_at = time.perf_counter()
    except Exception:
        pass


def mark_surface_filter_dirty(app, reason="classification/undo"):
    """
    Force next Surface call to rebuild, even if Surface is already active.

    Also invalidates Surface mesh cache safely.
    This is required after classification, undo, redo, and Display Mode
    class-filter changes.
    """
    try:
        app._surface_visible_class_signature = None
        app._surface_needs_rebuild_after_classification = True
        app._surface_filter_dirty = True
        app._surface_mesh_cache_dirty = True
        app._surface_dirty_reason = reason
        app._surface_cache_revision = int(getattr(app, "_surface_cache_revision", 0) or 0) + 1
    except Exception:
        pass


def detach_surface_before_non_surface_mode(app, requested_mode=None):
    requested = str(requested_mode or "").lower().strip()
    if requested == "surface":
        return

    hard_clear = requested in {
        "clear", "clear_data", "grid_clear", "grid_load",
        "clear_project", "project_clear", "point_cloud_clear", "new_file",
    }

    # Normal display-mode switches park the Surface actor instead of destroying
    # its VTK/GPU state. Returning to the same Surface signature is then a true
    # visibility toggle rather than a multi-million-face GPU re-upload.
    if not hard_clear:
        try:
            resident_sig = getattr(app, "_surface_resident_signature", None)
            resident_cache = getattr(app, "_surface_mesh_cache", None)
            transient_resident = bool(
                isinstance(resident_cache, dict)
                and resident_cache.get("signature") == resident_sig
                and resident_cache.get("transient", False)
            )
            actor = getattr(app, "_surface_mesh_actor", None)
            if actor is not None and not transient_resident:
                try:
                    actor_visible = bool(actor.GetVisibility())
                except Exception:
                    actor_visible = True
                if not actor_visible:
                    # Already parked by this or another authoritative display
                    # transition. Keep the operation silent and idempotent,
                    # while preserving the established mode-state restoration.
                    target_mode = requested or "rgb"
                    if str(getattr(app, "display_mode", "") or "").lower() == "surface":
                        app.display_mode = target_mode
                    if str(getattr(app, "current_display_mode", "") or "").lower() == "surface":
                        app.current_display_mode = target_mode
                    try:
                        app._suspend_grid_clicks = False
                    except Exception:
                        pass
                    try:
                        unified_actor = getattr(app, "_unified_actor", None)
                        if unified_actor is not None:
                            unified_actor.VisibilityOn()
                    except Exception:
                        pass
                    return
                try:
                    actor.VisibilityOff()
                except Exception:
                    pass
                target_mode = requested or "rgb"
                if str(getattr(app, "display_mode", "") or "").lower() == "surface":
                    app.display_mode = target_mode
                if str(getattr(app, "current_display_mode", "") or "").lower() == "surface":
                    app.current_display_mode = target_mode
                try:
                    app._suspend_grid_clicks = False
                except Exception:
                    pass
                try:
                    unified_actor = getattr(app, "_unified_actor", None)
                    if unified_actor is not None:
                        unified_actor.VisibilityOn()
                except Exception:
                    pass
                print(f"  ⚡ Surface actor parked for {target_mode}; cache/GPU state preserved")
                return
            elif transient_resident:
                # Huge Slow/all-point meshes are deliberately not parked after
                # leaving Surface; keeping tens of millions of triangles resident
                # would pin multiple GB of CPU/GPU memory. Fast/Normal caches stay.
                try:
                    store = getattr(app, "_surface_mesh_cache_store", None)
                    if isinstance(store, dict):
                        store.pop(resident_sig, None)
                    if getattr(app, "_surface_mesh_cache", None) is resident_cache:
                        app._surface_mesh_cache = None
                except Exception:
                    pass
                print("  🧹 Large Slow Surface is transient — releasing resident mesh on mode switch")
        except Exception as e:
            print(f"  ⚠️ Surface park skipped; using full detach: {e}")

    try:
        vtk_widget = getattr(app, "vtk_widget", None)
        renderer = getattr(vtk_widget, "renderer", None) if vtk_widget is not None else None
        actors_to_remove = []

        # 1. Remove by stored app references.
        for attr_name in (
            "_surface_mesh_actor",
            "_surface_preview_actor",
            "_surface_instant_preview_actor",
        ):
            actor = getattr(app, attr_name, None)
            if actor is not None:
                actors_to_remove.append(actor)

        # 2. Remove PyVista named actors that contain surface.
        try:
            actors_dict = getattr(vtk_widget, "actors", {}) if vtk_widget is not None else {}
            for actor_name, actor in list(actors_dict.items()):
                if "surface" in str(actor_name).lower():
                    try:
                        vtk_widget.remove_actor(actor_name, render=False)
                        print(f"  🧹 Removed Surface actor by name: {actor_name}")
                    except Exception:
                        if actor is not None:
                            actors_to_remove.append(actor)
        except Exception:
            pass

        # 3. Remove VTK actors by tag/name/reference.
        if renderer is not None:
            try:
                actor_collection = renderer.GetActors()
                actor_collection.InitTraversal()

                stored_surface_actor = getattr(app, "_surface_mesh_actor", None)

                for _ in range(actor_collection.GetNumberOfItems()):
                    actor = actor_collection.GetNextActor()
                    if actor is None:
                        continue

                    is_surface = bool(getattr(actor, "_is_surface_mesh", False))
                    is_surface_mode = (
                        str(getattr(actor, "_naksha_display_mode", "") or "").lower() == "surface"
                    )
                    is_stored_surface = actor is stored_surface_actor

                    if is_surface or is_surface_mode or is_stored_surface:
                        actors_to_remove.append(actor)
            except Exception:
                pass

        removed = 0
        if renderer is not None:
            seen = set()
            for actor in actors_to_remove:
                if actor is None:
                    continue
                marker = id(actor)
                if marker in seen:
                    continue
                seen.add(marker)

                try:
                    actor.VisibilityOff()
                except Exception:
                    pass

                try:
                    renderer.RemoveActor(actor)
                    removed += 1
                except Exception:
                    pass

        # 4. Remove known names again as safety.
        for name in (
            SURFACE_ACTOR_NAME,
            "surface_mesh",
            "surface_preview",
            "surface_instant_preview",
        ):
            try:
                if vtk_widget is not None:
                    vtk_widget.remove_actor(name, render=False)
            except Exception:
                pass

        # 5. Clear Surface state.
        app._surface_mesh_actor = None
        app._surface_preview_actor = None
        app._surface_instant_preview_actor = None
        app._surface_mesh_polydata = None
        app._surface_points = None
        app._surface_faces = None
        app._surface_global_to_unique = None
        app._surface_unique_global_indices = None
        app._surface_vtk_buffers = None

        # Reset stale Surface ownership state when leaving/clearing Surface.
        target_mode = str(requested_mode or "rgb").lower().strip()
        if target_mode in ("", "none", "clear", "clear_data", "grid_clear", "grid_load", "clear_project"):
            target_mode = "rgb"

        try:
            if str(getattr(app, "display_mode", "") or "").lower() == "surface":
                app.display_mode = target_mode
        except Exception:
            pass

        try:
            if str(getattr(app, "current_display_mode", "") or "").lower() == "surface":
                app.current_display_mode = target_mode
        except Exception:
            pass

        try:
            app._suspend_grid_clicks = False
        except Exception:
            pass

        try:
            app._surface_building = False
            app._surface_busy = False
            app._surface_input_blocked = False
            app._surface_needs_rebuild_after_classification = False
            app._surface_filter_dirty = False
            if hard_clear:
                app._surface_mesh_cache_dirty = True
                try:
                    store = getattr(app, "_surface_mesh_cache_store", None)
                    if isinstance(store, dict):
                        store.clear()
                    app._surface_mesh_cache = None
                    app._surface_z_bounds_cache = None
                except Exception:
                    pass
            # A normal full detach (used only as a fallback or for a huge
            # transient Slow mesh) must not invalidate other Fast/Normal caches.
            app._surface_resident_signature = None
        except Exception:
            pass

        try:
            dlg = getattr(app, "_surface_settings_dialog", None)
            if dlg is not None:
                try:
                    dlg.close()
                except Exception:
                    dlg.hide()
            app._surface_settings_dialog = None
        except Exception:
            pass

        # Restore main unified point actor when leaving Surface mode.
        try:
            unified_actor = getattr(app, "_unified_actor", None)
            if unified_actor is not None:
                unified_actor.VisibilityOn()
        except Exception:
            pass

        if removed:
            print(f"  🧹 Surface cleanup before {requested_mode}: removed {removed} actor(s)")

        try:
            if vtk_widget is not None:
                vtk_widget.render()
        except Exception:
            pass

    except Exception as e:
        print(f"  ⚠️ Surface cleanup skipped: {e}")


def remove_shaded_class_mesh_actors(app):
    """Remove shaded-class mesh actors before entering Surface mode."""
    try:
        renderer = getattr(getattr(app, "vtk_widget", None), "renderer", None)
        if renderer is None:
            return
        actors_to_remove = []
        actor_collection = renderer.GetActors()
        actor_collection.InitTraversal()
        for _ in range(actor_collection.GetNumberOfItems()):
            actor = actor_collection.GetNextActor()
            if actor is None:
                continue
            is_surface = bool(getattr(actor, "_is_surface_mesh", False))
            is_shading = bool(getattr(actor, "_is_shading_mesh", False))
            if is_shading and not is_surface:
                actors_to_remove.append(actor)
        for actor in actors_to_remove:
            try:
                renderer.RemoveActor(actor)
            except Exception:
                pass
        for name in ("shaded_mesh", "shaded_class_mesh", "class_shading_mesh", "multi_class_shading_mesh"):
            try:
                app.vtk_widget.remove_actor(name, render=False)
            except Exception:
                pass
        app._shaded_mesh_actor = None
    except Exception as e:
        print(f"  ⚠️ Shaded mesh cleanup before Surface skipped: {e}")

def _restore_snt_grid_above_surface(app) -> int:
    """
    Keep SNT / DXF / CL / grid actors visible above Surface mesh.

    Surface mesh writes depth and can visually cover SNT grid/block/CL actors.
    We do NOT move drawing actors here. This only re-adds existing SNT/grid
    actors into the digitizer overlay renderers so they stay visible in Surface mode.

    Important:
        - Surface actor stays in main renderer.
        - SNT/grid/CL line actors are also added to overlay_renderer.
        - SNT/grid/CL text actors are also added to text_overlay_renderer.
        - We do not remove SNT actors from the main renderer, so existing systems
          and picking state remain safer.
    """
    try:
        vtk_widget = getattr(app, "vtk_widget", None)
        main_renderer = getattr(vtk_widget, "renderer", None) if vtk_widget is not None else None

        digitizer = getattr(app, "digitizer", None)
        overlay = getattr(digitizer, "overlay_renderer", None) if digitizer is not None else None
        text_overlay = getattr(digitizer, "text_overlay_renderer", None) if digitizer is not None else None

        if main_renderer is None or overlay is None:
            return 0

        try:
            overlay.SetLayer(1)
            overlay.SetErase(0)
            overlay.SetInteractive(0)
            overlay.SetActiveCamera(main_renderer.GetActiveCamera())
        except Exception:
            pass

        try:
            if text_overlay is not None:
                text_overlay.SetLayer(2)
                text_overlay.SetErase(1)
                text_overlay.SetInteractive(0)
                try:
                    text_overlay.SetPreserveColorBuffer(True)
                except Exception:
                    pass
                text_overlay.SetActiveCamera(main_renderer.GetActiveCamera())
        except Exception:
            pass

        surface_actor = getattr(app, "_surface_mesh_actor", None)
        unified_actor = getattr(app, "_unified_actor", None)

        def _is_surface_or_cloud_actor(actor):
            if actor is None:
                return True

            if actor is surface_actor or actor is unified_actor:
                return True

            try:
                if bool(getattr(actor, "_is_surface_mesh", False)):
                    return True
            except Exception:
                pass

            try:
                if bool(getattr(actor, "_is_shading_mesh", False)):
                    return True
            except Exception:
                pass

            try:
                mode = str(getattr(actor, "_naksha_display_mode", "") or "").lower()
                if mode in ("surface", "class", "rgb", "intensity", "elevation", "depth", "shading"):
                    return True
            except Exception:
                pass

            return False

        def _is_text_actor(actor):
            try:
                return bool(
                    actor.IsA("vtkTextActor3D")
                    or actor.IsA("vtkTextActor")
                    or actor.IsA("vtkBillboardTextActor3D")
                    or actor.IsA("vtkFollower")
                )
            except Exception:
                return False

        def _is_actor2d(actor):
            try:
                return bool(actor.IsA("vtkActor2D") or actor.IsA("vtkTextActor"))
            except Exception:
                return False

        def _flatten(value):
            if value is None:
                return

            if hasattr(value, "IsA"):
                yield value
                return

            if isinstance(value, dict):
                for v in value.values():
                    yield from _flatten(v)
                return

            if isinstance(value, (list, tuple, set)):
                for v in value:
                    yield from _flatten(v)
                return

        def _collect_known_actors():
            # Common app-level SNT/DXF/grid containers.
            for owner in (
                app,
                getattr(app, "grid_label_manager", None),
                getattr(app, "snt_layer_pick_tool", None),
                getattr(app, "dxf_attachment", None),
            ):
                if owner is None:
                    continue

                for attr in (
                    "snt_actors",
                    "dxf_actors",
                    "snt_label_actors",
                    "snt_grid_actors",
                    "snt_block_actors",
                    "grid_label_actors",
                    "block_actors",
                    "label_actors",
                    "line_actors",
                    "text_actors",
                    "outline_actors",
                    "face_outline_actors",
                    "filename_label_actors",
                ):
                    try:
                        yield from _flatten(getattr(owner, attr, None))
                    except Exception:
                        pass

            # SNT attachment dictionaries.
            for att in list(getattr(app, "snt_attachments", []) or []):
                if not isinstance(att, dict):
                    continue

                for key in (
                    "actors",
                    "vtk_actors",
                    "line_actors",
                    "label_actors",
                    "block_actors",
                    "grid_actors",
                    "text_actors",
                    "outline_actors",
                    "face_outline_actors",
                    "filename_label_actors",
                ):
                    try:
                        yield from _flatten(att.get(key))
                    except Exception:
                        pass

        def _collect_main_renderer_candidates():
            # Fallback: collect visible non-surface/non-cloud actors from main renderer.
            # This catches SNT grid actors even if they are not stored in a known list.
            try:
                actors = main_renderer.GetActors()
                actors.InitTraversal()
                for _ in range(actors.GetNumberOfItems()):
                    actor = actors.GetNextActor()
                    if actor is not None:
                        yield actor
            except Exception:
                pass

            try:
                actors2d = main_renderer.GetActors2D()
                actors2d.InitTraversal()
                for _ in range(actors2d.GetNumberOfItems()):
                    actor = actors2d.GetNextActor2D()
                    if actor is not None:
                        yield actor
            except Exception:
                pass

        seen = set()
        count = 0

        for actor in list(_collect_known_actors()) + list(_collect_main_renderer_candidates()):
            if actor is None:
                continue

            marker = id(actor)
            if marker in seen:
                continue
            seen.add(marker)

            if _is_surface_or_cloud_actor(actor):
                continue

            # Do not push classification preview actors above Surface.
            # These are temporary yellow rectangle/circle/line previews.
            try:
                if bool(getattr(actor, "_is_classification_preview", False)):
                    continue
                if str(getattr(actor, "_naksha_preview_owner", "") or "").lower() == "classification":
                    continue
            except Exception:
                pass

            # Do not revive actors that were intentionally hidden.
            # Earlier code forced SetVisibility(1), which could bring back
            # hidden classification rectangle previews during Surface rebuild.
            try:
                if not actor.GetVisibility():
                    continue
            except Exception:
                pass

            try:
                actor.SetVisibility(1)
            except Exception:
                pass

            # Make overlay/grid line actors ignore Surface depth where supported.
            try:
                prop = actor.GetProperty()
                try:
                    prop.SetDepthTestingEnabled(False)
                except Exception:
                    pass
            except Exception:
                pass

            try:
                mapper = actor.GetMapper()
                if mapper is not None:
                    mapper.SetResolveCoincidentTopologyToPolygonOffset()
                    mapper.SetResolveCoincidentTopologyPolygonOffsetParameters(-100000, -100000)
                    try:
                        mapper.SetResolveCoincidentTopologyLineOffsetParameters(-100000, -100000)
                    except Exception:
                        pass
            except Exception:
                pass

            target = text_overlay if (_is_text_actor(actor) and text_overlay is not None) else overlay

            try:
                target.RemoveActor(actor)
            except Exception:
                pass
            try:
                target.RemoveActor2D(actor)
            except Exception:
                pass
            try:
                target.RemoveViewProp(actor)
            except Exception:
                pass

            try:
                if _is_actor2d(actor):
                    target.AddActor2D(actor)
                else:
                    target.AddActor(actor)
                count += 1
            except Exception:
                try:
                    target.AddActor(actor)
                    count += 1
                except Exception:
                    pass

        try:
            overlay.Modified()
            if text_overlay is not None:
                text_overlay.Modified()
            main_renderer.Modified()
        except Exception:
            pass

        if count:
            print(f"✅ SNT/grid actors pushed above Surface: {count}")

        return count

    except Exception as e:
        print(f"⚠️ SNT/grid Surface overlay restore skipped: {e}")
        return 0

def _surface_data_signature(app) -> tuple:
    """
    Lightweight signature for the currently loaded point cloud.

    This prevents restoring a cached Surface mesh after a new file/load.
    """
    try:
        xyz = app.data.get("xyz") if getattr(app, "data", None) else None
        if xyz is None or len(xyz) == 0:
            return ("NO_DATA",)

        file_id = (
            getattr(app, "current_file_path", None)
            or getattr(app, "loaded_file_path", None)
            or getattr(app, "current_laz_path", None)
            or getattr(app, "filename", None)
            or ""
        )

        mid = len(xyz) // 2
        return (
            str(file_id),
            int(len(xyz)),
            round(float(xyz[0, 0]), 4),
            round(float(xyz[mid, 1]), 4),
            round(float(xyz[-1, 2]), 4),
        )
    except Exception:
        return ("SURFACE_DATA_SIGNATURE_ERROR",)


def surface_mesh_cache_signature(app) -> tuple:
    """
    Full cache signature.

    Cache is valid only for:
    - same loaded data
    - same Surface visible classes
    - same Surface parameters
    - same Surface revision
    """
    try:
        try:
            preset_sig = current_surface_preset_signature(app)
        except Exception:
            preset_sig = (
                surface_visible_class_signature(app),
                round(float(getattr(app, "surface_max_edge", 0.0) or 0.0), 6),
                round(float(getattr(app, "surface_azimuth", getattr(app, "last_shade_azimuth", 45.0)) or 45.0), 6),
                round(float(getattr(app, "surface_angle", getattr(app, "last_shade_angle", 45.0)) or 45.0), 6),
                round(float(getattr(app, "surface_ambient", getattr(app, "shade_ambient", 0.22)) or 0.22), 6),
                normalize_surface_quality(getattr(app, "surface_quality", "normal")),
            )

        ramp = getattr(app, "surface_color_ramp", None) or getattr(app, "elevation_color_ramp", None) or []
        ramp_sig = tuple(
            (round(float(pos), 6), tuple(int(c) for c in (color[:3] if isinstance(color, (list, tuple)) else color)))
            for pos, color in ramp
        )

        return (
            _surface_data_signature(app),
            preset_sig,
            normalize_surface_quality(getattr(app, "surface_quality", "normal")),
            ramp_sig,
            int(getattr(app, "classification_revision", 0) or 0),
            int(getattr(app, "_surface_cache_revision", 0) or 0),
        )
    except Exception:
        return ("SURFACE_CACHE_SIGNATURE_ERROR",)


def _surface_cache_store_for_app(app):
    store = getattr(app, "_surface_mesh_cache_store", None)
    if not isinstance(store, OrderedDict):
        store = OrderedDict()
        app._surface_mesh_cache_store = store
    return store


def _store_surface_mesh_cache(app) -> bool:
    """Store a small quality-aware LRU of Surface meshes."""
    try:
        mesh = getattr(app, "_surface_mesh_polydata", None)
        points = getattr(app, "_surface_points", None)
        faces = getattr(app, "_surface_faces", None)
        if mesh is None or points is None or faces is None:
            return False

        signature = surface_mesh_cache_signature(app)
        quality = normalize_surface_quality(getattr(app, "surface_quality", "normal"))
        transient_slow = bool(
            quality == "slow" and len(points) > _SURFACE_SLOW_RETAIN_MAX_POINTS
        )
        entry = {
            "signature": signature,
            "quality": quality,
            "mesh": mesh,
            "points": points,
            "faces": faces,
            "unique_global_indices": getattr(app, "_surface_unique_global_indices", None),
            # Kept for compatibility only. The previous Slow-mode Python dict
            # contained one entry per Surface vertex (tens of millions of
            # Python objects) and was not consumed anywhere in the project.
            "global_to_unique": None,
            "vtk_buffers": getattr(app, "_surface_vtk_buffers", None),
            "transient": transient_slow,
        }

        store = _surface_cache_store_for_app(app)
        if transient_slow:
            # Never retain two 30M+ Slow meshes after consecutive
            # classifications.  The previous LRU limit of 2 was harmless for
            # Fast/Normal meshes but could pin several extra GB because a Slow
            # entry owns the old VTK mesh + NumPy connectivity buffers.
            for old_key, old_entry in list(store.items()):
                if isinstance(old_entry, dict) and old_entry.get("transient", False):
                    store.pop(old_key, None)
        store[signature] = entry
        store.move_to_end(signature)
        while len(store) > _SURFACE_CACHE_MAX_ENTRIES:
            store.popitem(last=False)

        # Compatibility for existing code that reads the old one-cache field.
        app._surface_mesh_cache = entry
        app._surface_mesh_cache_dirty = False
        app._surface_filter_dirty = False
        app._surface_needs_rebuild_after_classification = False
        app._surface_resident_signature = signature
        print(
            f"💾 Surface mesh cached quality={quality} entries={len(store)} "
            f"transient={int(transient_slow)}"
        )
        return True
    except Exception as e:
        print(f"⚠️ Surface mesh cache store skipped: {e}")
        return False


def restore_cached_surface_mesh(app, signature=None) -> bool:
    """Restore the exact quality/filter Surface cache, reusing resident actor when possible."""
    t0 = time.perf_counter()
    try:
        expected = signature or surface_mesh_cache_signature(app)
        quality = normalize_surface_quality(getattr(app, "surface_quality", "normal"))

        # Fastest path: the last Surface actor is still GPU-resident but hidden.
        actor = getattr(app, "_surface_mesh_actor", None)
        if (
            actor is not None
            and getattr(app, "_surface_resident_signature", None) == expected
            and getattr(actor, "GetMapper", lambda: None)() is not None
        ):
            try:
                remove_shaded_class_mesh_actors(app)
            except Exception:
                pass
            try:
                _configure_surface_base_actor(actor)
                actor.VisibilityOn()
            except Exception:
                pass
            try:
                unified_actor = getattr(app, "_unified_actor", None)
                if unified_actor is not None:
                    unified_actor.VisibilityOff()
            except Exception:
                pass
            app.display_mode = "surface"
            app.current_display_mode = "surface"
            _remember_surface_signature(app)
            try:
                app.vtk_widget.render()
            except Exception:
                pass
            print(
                f"⚡ Surface resident actor restored quality={quality} "
                f"in {(time.perf_counter()-t0)*1000:.1f}ms"
            )
            return True

        store = _surface_cache_store_for_app(app)
        cache = store.get(expected)
        if cache is None:
            legacy = getattr(app, "_surface_mesh_cache", None)
            if isinstance(legacy, dict) and legacy.get("signature") == expected:
                cache = legacy
        if not isinstance(cache, dict):
            return False

        mesh = cache.get("mesh")
        if mesh is None:
            return False
        vtk_widget = getattr(app, "vtk_widget", None)
        if vtk_widget is None:
            return False

        try:
            remove_shaded_class_mesh_actors(app)
        except Exception:
            pass
        try:
            vtk_widget.remove_actor(SURFACE_ACTOR_NAME, render=False)
        except Exception:
            pass

        actor = vtk_widget.add_mesh(
            mesh,
            scalars="RGB",
            rgb=True,
            show_edges=False,
            lighting=False,
            smooth_shading=False,
            preference="cell",
            name=SURFACE_ACTOR_NAME,
            render=False,
        )
        setattr(actor, "_is_surface_mesh", True)
        setattr(actor, "_naksha_display_mode", "surface")
        _configure_surface_base_actor(actor)

        try:
            unified_actor = getattr(app, "_unified_actor", None)
            if unified_actor is not None:
                unified_actor.VisibilityOff()
        except Exception:
            pass

        app._surface_mesh_actor = actor
        app._surface_mesh_polydata = mesh
        app._surface_points = cache.get("points")
        app._surface_faces = cache.get("faces")
        app._surface_unique_global_indices = cache.get("unique_global_indices")
        app._surface_global_to_unique = None
        app._surface_vtk_buffers = cache.get("vtk_buffers")
        app._surface_resident_signature = expected
        app.display_mode = "surface"
        app.current_display_mode = "surface"
        app._surface_shortcut_signature = expected
        _remember_surface_signature(app)
        store.move_to_end(expected)

        _hide_point_cloud_actors_for_surface(app)

        try:
            _restore_snt_grid_above_surface(app)
        except Exception as e:
            print(f"⚠️ Cached Surface SNT/grid restore skipped: {e}")
        try:
            vtk_widget.render()
        except Exception:
            pass

        print(
            f"⚡ Surface mesh restored from cache quality={quality} "
            f"in {(time.perf_counter()-t0)*1000:.1f}ms"
        )
        return True
    except Exception as e:
        print(f"⚠️ Surface mesh cache restore failed: {e}")
        return False


def _grid_dedup_indices(points: np.ndarray, precision: float) -> np.ndarray:
    if len(points) == 0:
        return np.empty(0, dtype=np.int64)
    precision = max(float(precision), 1e-6)
    grid = np.floor(points[:, :2] / precision).astype(np.int64, copy=False)
    _, idx = np.unique(grid, axis=0, return_index=True)
    return np.sort(idx.astype(np.int64, copy=False))


def _filter_long_edges(faces: np.ndarray, xy: np.ndarray, max_edge: float) -> np.ndarray:
    """Filter triangles by absolute edge length without giant edge temporaries."""
    if faces is None or len(faces) == 0:
        return np.empty((0, 3), dtype=np.int32)
    if max_edge <= 0:
        return np.asarray(faces, dtype=np.int32)

    f = np.asarray(faces, dtype=np.int32)
    xy_arr = np.asarray(xy, dtype=np.float64)
    max_edge_sq = float(max_edge) * float(max_edge)

    if _SURFACE_HAS_NUMBA:
        try:
            keep = _surface_edge_keep_mask_numba(
                np.ascontiguousarray(f),
                np.ascontiguousarray(xy_arr),
                max_edge_sq,
            )
            return f[keep].astype(np.int32, copy=False)
        except Exception as exc:
            print(f"SURFACE_ACCEL edge_filter=fallback reason={type(exc).__name__}: {exc}")

    # Chunked NumPy fallback: exact squared-distance predicate, no sqrt needed.
    chunk = max(100_000, int(os.environ.get("NAKSHA_SURFACE_EDGE_CHUNK", "1500000")))
    keep = np.empty(len(f), dtype=bool)
    for begin in range(0, len(f), chunk):
        finish = min(begin + chunk, len(f))
        fc = f[begin:finish]
        p0 = xy_arr[fc[:, 0]]
        p1 = xy_arr[fc[:, 1]]
        p2 = xy_arr[fc[:, 2]]
        d01 = np.sum((p0 - p1) * (p0 - p1), axis=1)
        d12 = np.sum((p1 - p2) * (p1 - p2), axis=1)
        d20 = np.sum((p2 - p0) * (p2 - p0), axis=1)
        keep[begin:finish] = (
            (d01 <= max_edge_sq)
            & (d12 <= max_edge_sq)
            & (d20 <= max_edge_sq)
        )
    return f[keep].astype(np.int32, copy=False)


def _face_colors(points: np.ndarray, faces: np.ndarray, app) -> np.ndarray:
    ramp = _normalize_surface_ramp(
        getattr(app, "surface_color_ramp", None) or getattr(app, "elevation_color_ramp", None)
    )
    return _compute_surface_face_colors(
        points,
        faces,
        float(getattr(app, "last_shade_azimuth", 45.0)),
        float(getattr(app, "last_shade_angle", 45.0)),
        float(getattr(app, "shade_ambient", 0.22)),
        float(getattr(app, "_surface_z_lo", np.nan)),
        float(getattr(app, "_surface_z_hi", np.nan)),
        ramp=ramp,
    )


def _build_surface_polydata_native(points: np.ndarray, faces: np.ndarray, colors: np.ndarray):
    """Build vtkPolyData from NumPy buffers without legacy packed-face conversion.

    PyVista's legacy ``[3, i0, i1, i2, ...]`` construction allocates another
    ~4 integers per face and then VTK converts that representation again.  On a
    60-70M triangle Slow Surface that is >1 GB of avoidable memory traffic.
    """
    import vtk
    from vtk.util import numpy_support

    pts = np.ascontiguousarray(points, dtype=np.float64)
    f = np.ascontiguousarray(faces, dtype=np.int32)
    rgb = np.ascontiguousarray(colors, dtype=np.uint8)
    nf = int(len(f))

    vtk_pts_arr = numpy_support.numpy_to_vtk(pts, deep=False)
    vtk_points = vtk.vtkPoints()
    vtk_points.SetData(vtk_pts_arr)

    max_conn = nf * 3
    use_i32 = (
        max_conn <= np.iinfo(np.int32).max
        and (f.size == 0 or int(f.max(initial=0)) <= np.iinfo(np.int32).max)
    )
    if use_i32:
        offsets = np.arange(0, max_conn + 1, 3, dtype=np.int32)
        conn = f.reshape(-1)
        vtk_offsets = numpy_support.numpy_to_vtk(
            offsets, deep=False, array_type=vtk.VTK_INT
        )
        vtk_conn = numpy_support.numpy_to_vtk(
            conn, deep=False, array_type=vtk.VTK_INT
        )
    else:
        # Future-proof fallback for >2^31 connectivity entries.
        offsets = np.arange(0, max_conn + 1, 3, dtype=np.int64)
        conn = np.ascontiguousarray(f.reshape(-1), dtype=np.int64)
        vtk_offsets = numpy_support.numpy_to_vtkIdTypeArray(offsets, deep=False)
        vtk_conn = numpy_support.numpy_to_vtkIdTypeArray(conn, deep=False)

    cells = vtk.vtkCellArray()
    cells.SetData(vtk_offsets, vtk_conn)

    vtk_rgb = numpy_support.numpy_to_vtk(
        rgb, deep=False, array_type=vtk.VTK_UNSIGNED_CHAR
    )
    vtk_rgb.SetName("RGB")

    poly = vtk.vtkPolyData()
    poly.SetPoints(vtk_points)
    poly.SetPolys(cells)
    poly.GetCellData().SetScalars(vtk_rgb)
    poly.Modified()

    # Keep Python-side buffers explicitly alive. vtk numpy arrays also retain a
    # reference, but this makes cache ownership obvious and robust.
    buffers = (pts, f, rgb, offsets, conn)
    return pv.wrap(poly), buffers


def _configure_surface_base_actor(actor) -> None:
    """Force a clean solid-cell Surface presentation with no hatch/edge pass.

    Surface RGB already contains the elevation-ramp Lambert lighting. A second
    VTK lighting/interpolation pass is both unnecessary and a source of dark
    striping when the mesh is viewed close up.
    """
    if actor is None:
        return
    try:
        prop = actor.GetProperty()
        if prop is not None:
            try:
                prop.SetRepresentationToSurface()
            except Exception:
                pass
            prop.SetLighting(False)
            prop.SetInterpolationToFlat()
            prop.SetAmbient(1.0)
            prop.SetDiffuse(0.0)
            prop.SetSpecular(0.0)
            prop.EdgeVisibilityOff()
            prop.SetOpacity(1.0)
    except Exception:
        pass
    try:
        mapper = actor.GetMapper()
        if mapper is not None:
            try:
                mapper.StaticOn()
            except Exception:
                pass
            try:
                mapper.InterpolateScalarsBeforeMappingOff()
            except Exception:
                pass
            try:
                if hasattr(mapper, "SetResolveCoincidentTopologyToOff"):
                    mapper.SetResolveCoincidentTopologyToOff()
            except Exception:
                pass
    except Exception:
        pass


def _hide_point_cloud_actors_for_surface(app) -> int:
    """Hide raw/class/LOD point actors while the Surface mesh owns the canvas.

    Actors are hidden rather than destroyed, preserving the existing RGB/Class
    mode caches. DXF/SNT/drawing geometry is deliberately not touched.
    """
    hidden = 0
    try:
        plotter = getattr(app, "vtk_widget", None)
        if plotter is None:
            return 0
        surface_actor = getattr(app, "_surface_mesh_actor", None)

        def _is_overlay(actor):
            return bool(
                actor is surface_actor
                or getattr(actor, "_is_surface_mesh", False)
                or getattr(actor, "_is_dxf_actor", False)
                or getattr(actor, "_is_snt_actor", False)
            )

        def _looks_like_point_cloud(actor):
            if actor is None or _is_overlay(actor):
                return False
            try:
                mapper = actor.GetMapper()
                poly = mapper.GetInput() if mapper is not None else None
                if poly is None or int(poly.GetNumberOfPoints()) <= 1000:
                    return False
                n_polys = int(poly.GetNumberOfPolys()) if hasattr(poly, "GetNumberOfPolys") else 0
                n_strips = int(poly.GetNumberOfStrips()) if hasattr(poly, "GetNumberOfStrips") else 0
                n_lines = int(poly.GetNumberOfLines()) if hasattr(poly, "GetNumberOfLines") else 0
                n_verts = int(poly.GetNumberOfVerts()) if hasattr(poly, "GetNumberOfVerts") else 0
                n_cells = int(poly.GetNumberOfCells())
                return (
                    n_polys == 0 and n_strips == 0 and n_lines == 0
                    and (n_verts > 0 or n_cells == 0 or n_cells == int(poly.GetNumberOfPoints()))
                )
            except Exception:
                return False

        for name, actor in list((getattr(plotter, "actors", {}) or {}).items()):
            ns = str(name).lower()
            should_hide = (
                ns.startswith("class_")
                or ns in ("main_pc", "main_pc_border", "_naksha_unified_cloud")
                or bool(getattr(actor, "_naksha_pyvista_points", False))
                or _looks_like_point_cloud(actor)
            )
            if should_hide and not _is_overlay(actor):
                try:
                    if actor.GetVisibility():
                        actor.VisibilityOff()
                        hidden += 1
                except Exception:
                    pass

        for attr_name in (
            "_unified_actor", "main_pc_actor", "point_cloud_actor",
            "_main_point_actor", "_lod_actor", "_boundary_point_actor",
        ):
            actor = getattr(app, attr_name, None)
            if actor is None or _is_overlay(actor):
                continue
            try:
                if actor.GetVisibility():
                    actor.VisibilityOff()
                    hidden += 1
            except Exception:
                pass
    except Exception as exc:
        print(f"SURFACE_POINT_ISOLATION status=warning error={exc}")
    return hidden


def _set_mesh_actor(app, points: np.ndarray, faces: np.ndarray, colors: np.ndarray) -> bool:
    try:
        actor_prepare_t0 = time.perf_counter()
        try:
            mesh, vtk_buffers = _build_surface_polydata_native(points, faces, colors)
            native_layout = True
        except Exception as native_exc:
            # Conservative compatibility fallback: preserve the old constructor
            # on systems with an older VTK/PyVista ABI.
            print(
                "SURFACE_VTK_LAYOUT fallback=legacy "
                f"reason={type(native_exc).__name__}: {native_exc}"
            )
            faces_pv = np.hstack(
                [
                    np.full((len(faces), 1), 3, dtype=np.int32),
                    np.asarray(faces, dtype=np.int32),
                ]
            ).ravel()
            mesh = pv.PolyData(np.asarray(points, dtype=np.float64), faces_pv)
            mesh.cell_data["RGB"] = np.asarray(colors, dtype=np.uint8)
            vtk_buffers = (points, faces, colors, faces_pv)
            native_layout = False

        prepare_ms = (time.perf_counter() - actor_prepare_t0) * 1000.0

        detach_surface_before_non_surface_mode(app, requested_mode="surface")
        remove_shaded_class_mesh_actors(app)
        # A new exact base Surface supersedes every classification replacement
        # skin accumulated by the fast local-update path.
        _surface_remove_local_patch_actors(app)

        try:
            app.vtk_widget.remove_actor(SURFACE_ACTOR_NAME, render=False)
        except Exception:
            pass

        upload_t0 = time.perf_counter()
        actor = app.vtk_widget.add_mesh(
            mesh,
            scalars="RGB",
            rgb=True,
            show_edges=False,
            lighting=False,
            smooth_shading=False,
            preference="cell",
            name=SURFACE_ACTOR_NAME,
            render=False,
        )
        upload_ms = (time.perf_counter() - upload_t0) * 1000.0
        setattr(actor, "_is_surface_mesh", True)
        setattr(actor, "_naksha_display_mode", "surface")
        _configure_surface_base_actor(actor)

        # Surface mode should show the terrain mesh, not the class/RGB point actor.
        try:
            unified_actor = getattr(app, "_unified_actor", None)
            if unified_actor is not None:
                unified_actor.VisibilityOff()
        except Exception:
            pass

        app._surface_mesh_actor = actor
        app._surface_mesh_polydata = mesh
        app._surface_points = vtk_buffers[0]
        app._surface_faces = vtk_buffers[1]
        app._surface_vtk_buffers = vtk_buffers

        hidden_points = _hide_point_cloud_actors_for_surface(app)
        if hidden_points:
            print(f"SURFACE_POINT_ISOLATION hidden={hidden_points} zfight_removed=1")

        try:
            _restore_snt_grid_above_surface(app)
        except Exception as e:
            print(f"⚠️ Surface SNT/grid restore skipped: {e}")

        render_t0 = time.perf_counter()
        app.vtk_widget.render()
        render_ms = (time.perf_counter() - render_t0) * 1000.0
        print(
            "SURFACE_PROFILE scope=vtk_actor "
            f"layout={'native' if native_layout else 'legacy'} "
            f"prepare={prepare_ms:.1f}ms add_mesh={upload_ms:.1f}ms "
            f"render={render_ms:.1f}ms points={len(app._surface_points)} "
            f"faces={len(app._surface_faces)}"
        )
        return True
    except Exception as e:
        print(f"⚠️ Surface actor build failed: {e}")
        return False


def _surface_z_bounds_cached(app, xyz_all):
    """Cache expensive global Z percentiles per loaded point cloud."""
    try:
        sig = _surface_data_signature(app)
        cache = getattr(app, "_surface_z_bounds_cache", None)
        if isinstance(cache, tuple) and len(cache) == 3 and cache[0] == sig:
            return float(cache[1]), float(cache[2])
        z = np.asarray(xyz_all[:, 2])
        lo = float(np.percentile(z, 1.0))
        hi = float(np.percentile(z, 99.0))
        app._surface_z_bounds_cache = (sig, lo, hi)
        return lo, hi
    except Exception:
        return None


def render_surface_mode(app, vis_mask: Optional[np.ndarray] = None, silent: bool = False) -> bool:
    """Build/replace Surface mode as elevation + shading mesh.

    Safety behavior:
    - repeated Surface clicks are ignored while a build is already running
    - user mouse/keyboard input is blocked during user-triggered builds
    - progress/render events still update, like Shading Display
    """
    if getattr(app, "data", None) is None or "xyz" not in app.data:
        return False

    if bool(getattr(app, "_surface_building", False)):
        if not silent:
            try:
                app.statusBar().showMessage("Surface is already building. Please wait...", 2500)
            except Exception:
                pass
            print("🏔️ Surface click ignored — build already in progress")
        return True

    # Fast path:
    # If the same Surface mesh was already built before and nothing is dirty,
    # restore it instantly before showing the Building Surface popup.
    try:
        if (
            vis_mask is None
            and not bool(getattr(app, "_surface_needs_rebuild_after_classification", False))
            and not bool(getattr(app, "_surface_filter_dirty", False))
            and not bool(getattr(app, "_surface_mesh_cache_dirty", False))
        ):
            if restore_cached_surface_mesh(app):
                return True
    except Exception as _cache_restore_err:
        print(f"⚠️ Surface cache restore check skipped: {_cache_restore_err}")

    t0 = time.perf_counter()
    progress = None
    blocker = None

    def _surface_progress(value, message):
        try:
            if progress is not None:
                progress.setLabelText(message)
                progress.setValue(int(value))
                _surface_process_progress_events()
        except Exception:
            pass

    def _close_surface_progress(value=100):
        try:
            if progress is not None:
                progress.setValue(int(value))
                if hasattr(progress, "finish"):
                    progress.finish()
                else:
                    progress.close()
        except Exception:
            pass

    try:
        # User-triggered Surface builds get the same safe behavior as Shading:
        # keep the progress popup alive but do not accept mouse/keyboard input.
        blocker = _surface_begin_build_guard(app, block_input=True)

        # Show popup only for user-triggered Surface builds.
        # Classification / Undo / Redo refresh calls render_surface_mode(..., silent=True),
        # so this popup will NOT appear during classification.
        if not silent and _SurfaceProgressDialog is not None:
            progress = _SurfaceProgressDialog(app)
            progress.setValue(5)
            progress.show()
            _surface_process_progress_events()

        xyz_all = app.data["xyz"]
        _surface_progress(10, "Preparing visible Surface classes...")

        if vis_mask is None:
            vis_mask = surface_visible_mask(app)

        if vis_mask is None or len(vis_mask) != len(xyz_all):
            vis_mask = np.ones(len(xyz_all), dtype=bool)

        global_idx = np.flatnonzero(vis_mask)
        try:
            print(
                "SURFACE_CLASS_FILTER "
                f"classes={surface_visible_class_signature(app)} "
                f"eligible={global_idx.size:,} total={len(xyz_all):,} "
                "exact_checkbox_membership=1"
            )
        except Exception:
            pass
        if global_idx.size < 3:
            detach_surface_before_non_surface_mode(app, requested_mode="surface")
            return False

        quality_mode = normalize_surface_quality(getattr(app, "surface_quality", "normal"))
        app.surface_quality = quality_mode
        precision = float(getattr(app, "surface_dedup_precision", 0.0) or 0.0)
        target_max = _surface_quality_target(quality_mode, int(global_idx.size))
        max_edge = float(getattr(app, "surface_max_edge", 0.0) or 0.0)
        z_bounds = _surface_z_bounds_cached(app, xyz_all)
        if not silent:
            print(
                f"🏔️ SURFACE ({quality_mode}) eligible={global_idx.size:,} "
                f"target={target_max:,} triangulator={'triangle' if _SURFACE_HAS_TRIANGLE else 'scipy'}"
            )
        azimuth = float(getattr(app, "last_shade_azimuth", 45.0))
        angle = float(getattr(app, "last_shade_angle", 45.0))
        ambient = float(getattr(app, "shade_ambient", 0.22))
        ramp = _normalize_surface_ramp(
            getattr(app, "surface_color_ramp", None) or getattr(app, "elevation_color_ramp", None)
        )

        if _SurfaceComputationWorker is not None and QEventLoop is not None:
            worker = _SurfaceComputationWorker(
                xyz_all,
                global_idx,
                precision,
                target_max,
                max_edge,
                azimuth,
                angle,
                ambient,
                ramp,
                quality_mode=quality_mode,
                z_bounds=z_bounds,
            )
            loop = QEventLoop()
            result_container = {}

            def _handle_surface_progress(value, message):
                _surface_progress(value, message)

            def _handle_surface_finish(result):
                result_container["result"] = result
                loop.quit()

            def _handle_surface_error(error_text):
                result_container["error"] = error_text
                loop.quit()

            worker.progress_signal.connect(_handle_surface_progress)
            worker.finished_signal.connect(_handle_surface_finish)
            worker.error_signal.connect(_handle_surface_error)
            worker.start()
            loop.exec()
            worker.wait()

            if "error" in result_container:
                raise RuntimeError(result_container["error"])
            result = result_container.get("result", {})
        else:
            result = _compute_surface_geometry_backend(
                xyz_all,
                global_idx,
                precision,
                target_max,
                max_edge,
                azimuth,
                angle,
                ambient,
                ramp,
                progress_callback=_surface_progress,
                quality_mode=quality_mode,
                z_bounds=z_bounds,
            )

        profile_timings = result.get("profile_timings", {}) or {}
        if profile_timings:
            try:
                stages = " ".join(
                    f"{name}={float(value)*1000.0:.1f}ms"
                    for name, value in profile_timings.items()
                )
                rep_meta = result.get("representative_meta", {}) or {}
                print(
                    f"SURFACE_PROFILE scope=geometry_backend quality={quality_mode} "
                    f"{stages} raw={global_idx.size} "
                    f"selected={len(result.get('points', []))} "
                    f"faces={len(result.get('faces', []))} "
                    f"triangulator={result.get('triangulator', 'unknown')} "
                    f"rep={rep_meta.get('strategy', 'unknown')}"
                )
            except Exception:
                pass

        if result.get("empty", False):
            detach_surface_before_non_surface_mode(app, requested_mode="surface")
            return False

        pts = result["points"]
        faces = result["faces"]
        colors = result["colors"]
        unique_global_idx = result["unique_global_idx"]
        app._surface_z_lo = float(result.get("z_lo", np.nan))
        app._surface_z_hi = float(result.get("z_hi", np.nan))

        _surface_progress(95, "Drawing Surface in main view...")

        _surface_actor_t0 = time.perf_counter()
        ok = _set_mesh_actor(app, pts, faces, colors)
        _surface_actor_ms = (time.perf_counter() - _surface_actor_t0) * 1000.0

        if ok:
            app.display_mode = "surface"
            app.current_display_mode = "surface"
            app.surface_color_ramp = getattr(app, "surface_color_ramp", None) or getattr(app, "elevation_color_ramp", None)
            _remember_surface_signature(app)

            # Surface mode owns right-click.
            # Grid/block context menus are still available again after leaving Surface.
            try:
                app._suspend_grid_clicks = True
            except Exception:
                pass

            app._surface_unique_global_indices = unique_global_idx
            # unique_global_idx is already sorted because it originates from
            # flatnonzero / sorted representative indices.  Do NOT materialize
            # a Python {global: local} dict here: on a 34M-point Slow Surface
            # it consumes multiple GB and several seconds, and no live caller
            # uses it.  Local lookup code uses searchsorted on this array.
            app._surface_global_to_unique = None
            print(
                "SURFACE_INDEX_MAP strategy=sorted_array "
                f"count={len(unique_global_idx):,} python_dict=skipped"
            )

            try:
                _store_surface_mesh_cache(app)
            except Exception as _surface_cache_err:
                print(f"⚠️ Surface mesh cache store skipped: {_surface_cache_err}")
            if not silent:
                print(
                    f"🏔️ Surface mode applied [{quality_mode}]: "
                    f"{len(pts):,} vertices, {len(faces):,} faces "
                    f"actor={_surface_actor_ms:.1f}ms "
                    f"total={(time.perf_counter() - t0) * 1000:.1f}ms"
                )

        return ok

    finally:
        _close_surface_progress(100)
        _surface_finish_build_guard(app, blocker)

def _update_existing_surface_polydata(app, points: np.ndarray, faces: np.ndarray) -> bool:
    try:
        mesh = getattr(app, "_surface_mesh_polydata", None)
        if mesh is None:
            return False
        colors = _face_colors(points, faces, app)
        new_mesh = pv.PolyData(
            points.astype(np.float64, copy=False),
            np.hstack([np.full((len(faces), 1), 3, dtype=np.int32), faces.astype(np.int32)]).ravel(),
        )
        new_mesh.cell_data["RGB"] = colors.astype(np.uint8, copy=False)
        actor = getattr(app, "_surface_mesh_actor", None)
        if actor is None or actor.GetMapper() is None:
            return _set_mesh_actor(app, points, faces, colors)
        actor.GetMapper().SetInputData(new_mesh)
        actor.GetMapper().Modified()
        app._surface_mesh_polydata = new_mesh
        app._surface_points = points
        app._surface_faces = faces

        try:
            _restore_snt_grid_above_surface(app)
        except Exception as e:
            print(f"⚠️ Updated Surface SNT/grid restore skipped: {e}")

        try:
            _store_surface_mesh_cache(app)
        except Exception:
            pass

        app.vtk_widget.render()
        return True
    except Exception as e:
        print(f"⚠️ Surface in-place update failed: {e}")
        return False


def _apply_surface_bridge_update(app, changed_mask=None, operation="classification") -> bool:
    """
    Local visual Surface correction after classification.
    It does not change app.data['xyz']; it only updates the Surface mesh so edited
    obstacle patches stay in normal elevation+shading form instead of class colors.
    """
    if str(getattr(app, "display_mode", "") or "").lower() != "surface":
        return False

    pts = getattr(app, "_surface_points", None)
    faces = getattr(app, "_surface_faces", None)
    unique_global = getattr(app, "_surface_unique_global_indices", None)
    if pts is None or faces is None or unique_global is None or len(pts) == 0 or len(faces) == 0:
        return render_surface_mode(app, silent=True)

    if changed_mask is None:
        changed_mask = getattr(app, "_last_changed_mask", None)
    if changed_mask is None or not isinstance(changed_mask, np.ndarray) or not np.any(changed_mask):
        return True

    changed_idx = np.flatnonzero(changed_mask).astype(np.int64, copy=False)
    if changed_idx.size == 0:
        return True

    # Find exact mesh vertices belonging to changed points.
    sorter = np.argsort(unique_global)
    sorted_global = unique_global[sorter]
    pos = np.searchsorted(sorted_global, changed_idx)
    valid = pos < len(sorted_global)
    pos_valid = pos[valid]
    changed_valid = changed_idx[valid]
    if pos_valid.size:
        hit = sorted_global[pos_valid] == changed_valid
        target_vertices = np.unique(sorter[pos_valid[hit]])
    else:
        target_vertices = np.empty(0, dtype=np.int64)

    # Fallback: nearest XY vertices around changed points.
    if target_vertices.size == 0:
        try:
            xyz_all = app.data["xyz"]
            ch_xy = xyz_all[changed_idx, :2]
            tree = cKDTree(pts[:, :2])
            spacing = np.sqrt(max(np.ptp(pts[:, 0]) * np.ptp(pts[:, 1]), 1.0) / max(len(pts), 1))
            radius = max(float(spacing) * 4.0, 0.25)
            near_lists = tree.query_ball_point(ch_xy, r=radius)
            target_vertices = np.unique(np.fromiter((i for sub in near_lists for i in sub), dtype=np.int64))
        except Exception:
            target_vertices = np.empty(0, dtype=np.int64)

    if target_vertices.size == 0:
        return render_surface_mode(app, silent=True)

    # Expand to adjacent faces/vertices so the patch blends naturally.
    face_hit = np.isin(faces, target_vertices).any(axis=1)
    affected_faces = np.flatnonzero(face_hit)
    affected_vertices = np.unique(faces[affected_faces].ravel()) if affected_faces.size else target_vertices

    points2 = pts.copy()
    xy = points2[:, :2]
    target_xy = xy[affected_vertices]
    min_xy = target_xy.min(axis=0)
    max_xy = target_xy.max(axis=0)
    diag = float(np.linalg.norm(max_xy - min_xy))
    margin = max(diag * 1.5, 1.0)

    bbox = (
        (xy[:, 0] >= min_xy[0] - margin) & (xy[:, 0] <= max_xy[0] + margin) &
        (xy[:, 1] >= min_xy[1] - margin) & (xy[:, 1] <= max_xy[1] + margin)
    )
    ring = np.flatnonzero(bbox)
    ring = np.setdiff1d(ring, affected_vertices, assume_unique=False)

    if ring.size >= 6:
        # Fit a local plane to surrounding Surface and project patch vertices onto it.
        A = np.c_[points2[ring, 0], points2[ring, 1], np.ones(ring.size)]
        z = points2[ring, 2]
        try:
            coef, *_ = np.linalg.lstsq(A, z, rcond=None)
            Az = np.c_[points2[affected_vertices, 0], points2[affected_vertices, 1], np.ones(len(affected_vertices))]
            new_z = Az @ coef
        except Exception:
            new_z = np.full(len(affected_vertices), float(np.median(z)))
        # Keep update conservative to avoid a new visible flat rectangle.
        points2[affected_vertices, 2] = new_z
    else:
        return render_surface_mode(app, silent=True)

    ok = _update_existing_surface_polydata(app, points2, faces)
    if ok:
        print(f"⚡ Surface {operation}: local elevation/shading patch updated ({len(affected_vertices):,} vertices)")
    return ok


def _surface_async_settings_signature(app):
    """Return the geometry/settings signature for an in-flight Surface rebuild.

    This intentionally excludes ``classification_revision``.  A support ->
    support class edit does not change Surface topology, so it must not make a
    perfectly valid background triangulation stale.  True support-membership
    edits are tracked separately by ``_surface_topology_generation``.
    """
    try:
        ramp = _normalize_surface_ramp(
            getattr(app, "surface_color_ramp", None)
            or getattr(app, "elevation_color_ramp", None)
        )
        ramp_sig = tuple(
            (round(float(pos), 8), tuple(int(c) for c in color[:3]))
            for pos, color in ramp
        )
        try:
            from gui.flight_line_filter import flight_line_visibility_signature
            flight_line_sig = flight_line_visibility_signature(app, 0)
        except Exception:
            flight_line_sig = ()
        return (
            _surface_data_signature(app),
            surface_visible_class_signature(app),
            flight_line_sig,
            normalize_surface_quality(getattr(app, "surface_quality", "normal")),
            round(float(getattr(app, "surface_dedup_precision", 0.0) or 0.0), 9),
            round(float(getattr(app, "surface_max_edge", 0.0) or 0.0), 9),
            round(float(getattr(app, "last_shade_azimuth", 45.0) or 45.0), 7),
            round(float(getattr(app, "last_shade_angle", 45.0) or 45.0), 7),
            round(float(getattr(app, "shade_ambient", 0.22) or 0.22), 7),
            ramp_sig,
        )
    except Exception:
        return ("SURFACE_ASYNC_SETTINGS_ERROR",)


def _surface_print_backend_profile(result, quality_mode, raw_count, prefix="SURFACE_PROFILE"):
    """Emit the same stage profile for synchronous and asynchronous builds."""
    try:
        profile_timings = result.get("profile_timings", {}) or {}
        if not profile_timings:
            return
        stages = " ".join(
            f"{name}={float(value)*1000.0:.1f}ms"
            for name, value in profile_timings.items()
        )
        rep_meta = result.get("representative_meta", {}) or {}
        print(
            f"{prefix} scope=geometry_backend quality={quality_mode} "
            f"{stages} raw={int(raw_count)} "
            f"selected={len(result.get('points', []))} "
            f"faces={len(result.get('faces', []))} "
            f"triangulator={result.get('triangulator', 'unknown')} "
            f"rep={rep_meta.get('strategy', 'unknown')}"
        )
    except Exception:
        pass


def _surface_async_cleanup_worker(app, worker):
    """Release one background worker without touching a newer replacement."""
    try:
        if getattr(app, "_surface_async_worker", None) is worker:
            app._surface_async_worker = None
    except Exception:
        pass
    try:
        # Async classification refresh never installs the input blocker, so do
        # not call _surface_finish_build_guard() here (it can restore widgets
        # belonging to a user-triggered build).  Only clear our own busy state.
        if getattr(app, "_surface_async_worker", None) is None:
            app._surface_building = False
            app._surface_busy = False
            app._surface_input_blocked = False
    except Exception:
        pass


def _surface_schedule_latest_async_rebuild(app, delay_ms=50):
    """Start one latest-state rebuild after an obsolete worker has finished."""
    try:
        if QThread is None or _SurfaceComputationWorker is None:
            return False
        if str(getattr(app, "display_mode", "") or "").lower() != "surface":
            return False
        from PySide6.QtCore import QTimer
        QTimer.singleShot(
            max(0, int(delay_ms)),
            lambda: _start_surface_async_exact_rebuild(app),
        )
        return True
    except Exception:
        return False


def _start_surface_async_exact_rebuild(app) -> bool:
    """Rebuild exact Surface topology in the background after classification.

    The currently displayed Surface stays resident and interactive while the
    expensive triangulation is computed.  VTK actor replacement still happens
    on the GUI thread, but only after the geometry is ready.  If another real
    topology edit occurs during the build, this result is discarded and one
    rebuild of the latest state is scheduled.  No classification result is
    dropped and no approximate topology is committed.
    """
    if _SurfaceComputationWorker is None or QThread is None:
        return False
    if getattr(app, "data", None) is None or "xyz" not in app.data:
        return False
    if str(getattr(app, "display_mode", "") or "").lower() != "surface":
        return False

    existing = getattr(app, "_surface_async_worker", None)
    try:
        existing_running = existing is not None and bool(existing.isRunning())
    except Exception:
        existing_running = existing is not None
    if existing_running:
        app._surface_async_rebuild_pending = True
        print("SURFACE_ASYNC_REBUILD coalesced=1 reason=worker_already_running")
        return True

    t0 = time.perf_counter()
    xyz_all = app.data["xyz"]
    try:
        vis_mask = surface_visible_mask(app)
    except Exception:
        vis_mask = None
    if vis_mask is None or len(vis_mask) != len(xyz_all):
        vis_mask = np.ones(len(xyz_all), dtype=bool)

    global_idx = np.flatnonzero(vis_mask).astype(np.int64, copy=False)
    if global_idx.size < 3:
        print("⚠️ Surface async rebuild skipped — fewer than 3 eligible points")
        return False

    quality_mode = normalize_surface_quality(getattr(app, "surface_quality", "normal"))
    app.surface_quality = quality_mode
    precision = float(getattr(app, "surface_dedup_precision", 0.0) or 0.0)
    target_max = _surface_quality_target(quality_mode, int(global_idx.size))
    max_edge = float(getattr(app, "surface_max_edge", 0.0) or 0.0)
    azimuth = float(getattr(app, "last_shade_azimuth", 45.0))
    angle = float(getattr(app, "last_shade_angle", 45.0))
    ambient = float(getattr(app, "shade_ambient", 0.22))
    ramp = _normalize_surface_ramp(
        getattr(app, "surface_color_ramp", None)
        or getattr(app, "elevation_color_ramp", None)
    )
    z_bounds = _surface_z_bounds_cached(app, xyz_all)

    generation = int(getattr(app, "_surface_topology_generation", 0) or 0)
    settings_sig = _surface_async_settings_signature(app)
    data_object_id = id(xyz_all)

    worker = _SurfaceComputationWorker(
        xyz_all,
        global_idx,
        precision,
        target_max,
        max_edge,
        azimuth,
        angle,
        ambient,
        ramp,
        quality_mode=quality_mode,
        z_bounds=z_bounds,
    )
    worker._naksha_surface_generation = generation
    worker._naksha_surface_settings_signature = settings_sig
    worker._naksha_surface_data_object_id = data_object_id
    worker._naksha_surface_raw_count = int(global_idx.size)
    worker._naksha_surface_started_at = t0

    app._surface_async_worker = worker
    app._surface_async_rebuild_pending = False
    app._surface_building = True
    app._surface_busy = True
    app._surface_input_blocked = False

    def _progress(value, message):
        # Background refresh must never become modal.  A short status message is
        # enough to show progress while classification/navigation remains usable.
        try:
            app.statusBar().showMessage(
                f"Surface updating in background... {int(value)}% - {message}"
            )
        except Exception:
            pass

    def _finish(result):
        worker_t0 = float(getattr(worker, "_naksha_surface_started_at", t0))
        try:
            mode_is_surface = str(getattr(app, "display_mode", "") or "").lower() == "surface"
            current_xyz = None
            try:
                current_xyz = app.data.get("xyz") if getattr(app, "data", None) else None
            except Exception:
                current_xyz = None
            same_data = current_xyz is not None and id(current_xyz) == data_object_id
            same_generation = (
                int(getattr(app, "_surface_topology_generation", 0) or 0) == generation
            )
            same_settings = _surface_async_settings_signature(app) == settings_sig

            if not (mode_is_surface and same_data and same_generation and same_settings):
                reasons = []
                if not mode_is_surface:
                    reasons.append("mode_changed")
                if not same_data:
                    reasons.append("data_changed")
                if not same_generation:
                    reasons.append("newer_topology_edit")
                if not same_settings:
                    reasons.append("settings_changed")
                print(
                    "SURFACE_ASYNC_REBUILD stale=1 discarded=1 reason="
                    + ",".join(reasons or ["unknown"])
                )
                _surface_async_cleanup_worker(app, worker)
                if mode_is_surface and same_data and bool(
                    getattr(app, "_surface_needs_rebuild_after_classification", False)
                ):
                    _surface_schedule_latest_async_rebuild(app, 50)
                return

            _surface_print_backend_profile(
                result,
                quality_mode,
                int(getattr(worker, "_naksha_surface_raw_count", global_idx.size)),
            )

            if result.get("empty", False):
                print("⚠️ Surface async rebuild returned an empty mesh; keeping current Surface")
                app._surface_needs_rebuild_after_classification = True
                _surface_async_cleanup_worker(app, worker)
                return

            pts = result["points"]
            faces = result["faces"]
            colors = result["colors"]
            unique_global_idx = result["unique_global_idx"]
            app._surface_z_lo = float(result.get("z_lo", np.nan))
            app._surface_z_hi = float(result.get("z_hi", np.nan))

            actor_t0 = time.perf_counter()
            ok = _set_mesh_actor(app, pts, faces, colors)
            actor_ms = (time.perf_counter() - actor_t0) * 1000.0
            if not ok:
                app._surface_needs_rebuild_after_classification = True
                app._surface_last_refresh_visual_change = False
                app._surface_last_refresh_presented = False
                print("⚠️ Surface async actor swap failed; keeping rebuild dirty")
                _surface_async_cleanup_worker(app, worker)
                return

            app.display_mode = "surface"
            app.current_display_mode = "surface"
            app.surface_color_ramp = (
                getattr(app, "surface_color_ramp", None)
                or getattr(app, "elevation_color_ramp", None)
            )
            try:
                app._suspend_grid_clicks = True
            except Exception:
                pass

            app._surface_unique_global_indices = unique_global_idx
            app._surface_global_to_unique = None
            print(
                "SURFACE_INDEX_MAP strategy=sorted_array "
                f"count={len(unique_global_idx):,} python_dict=skipped"
            )
            _remember_surface_signature(app)
            try:
                _store_surface_mesh_cache(app)
            except Exception as cache_exc:
                print(f"⚠️ Surface async cache store skipped: {cache_exc}")

            app._surface_needs_rebuild_after_classification = False
            app._surface_filter_dirty = False
            app._surface_mesh_cache_dirty = False
            app._surface_async_rebuild_pending = False
            app._surface_last_refresh_visual_change = True
            app._surface_last_refresh_presented = True
            total_ms = (time.perf_counter() - worker_t0) * 1000.0
            print(
                "SURFACE_ASYNC_REBUILD completed=1 exact=1 "
                f"quality={quality_mode} raw={len(global_idx):,} "
                f"faces={len(faces):,} actor_ms={actor_ms:.1f} total_ms={total_ms:.1f}"
            )
            try:
                app.statusBar().showMessage(
                    f"Surface updated ({len(faces):,} faces)", 2500
                )
            except Exception:
                pass
            _surface_async_cleanup_worker(app, worker)

            # A duplicate caller may have marked a pending request while this
            # exact generation was finishing.  Only start another build when a
            # genuinely newer topology generation is dirty.
            if bool(getattr(app, "_surface_async_rebuild_pending", False)) and bool(
                getattr(app, "_surface_needs_rebuild_after_classification", False)
            ):
                _surface_schedule_latest_async_rebuild(app, 50)

        except Exception as exc:
            app._surface_needs_rebuild_after_classification = True
            app._surface_last_refresh_visual_change = False
            app._surface_last_refresh_presented = False
            print(f"⚠️ Surface async finalize failed: {exc}")
            _surface_async_cleanup_worker(app, worker)

    def _error(error_text):
        try:
            app._surface_needs_rebuild_after_classification = True
            app._surface_last_refresh_visual_change = False
            app._surface_last_refresh_presented = False
            print("⚠️ Surface async rebuild failed:\n" + str(error_text))
            try:
                app.statusBar().showMessage(
                    "Surface background update failed; current Surface kept", 4000
                )
            except Exception:
                pass
        finally:
            _surface_async_cleanup_worker(app, worker)

    # Route callbacks through a QObject created on the GUI thread.  Directly
    # connecting a QThread signal to a plain Python closure can execute the
    # closure in an implementation-dependent thread; VTK actor replacement
    # must never run from the worker thread.
    if _SurfaceAsyncResultRelay is not None:
        relay = _SurfaceAsyncResultRelay(_finish, _error, _progress, parent=worker)
        worker._naksha_surface_relay = relay
        worker.progress_signal.connect(relay.handle_progress)
        worker.finished_signal.connect(relay.handle_finished)
        worker.error_signal.connect(relay.handle_error)
    else:
        worker.progress_signal.connect(_progress)
        worker.finished_signal.connect(_finish)
        worker.error_signal.connect(_error)
    try:
        # Delete the QThread QObject only after run() has actually returned.
        # The custom finished_signal is emitted *inside* run(), so deleting from
        # its handler risks "QThread destroyed while thread is still running".
        worker.finished.connect(worker.deleteLater)
    except Exception:
        pass
    worker.start()
    print(
        "SURFACE_ASYNC_REBUILD started=1 exact=1 ui_blocked=0 "
        f"generation={generation} quality={quality_mode} eligible={global_idx.size:,}"
    )
    return True



def _surface_remove_local_patch_actors(app) -> None:
    """Remove transient classification patch actors without touching the base Surface."""
    patches = getattr(app, "_surface_local_patch_actors", None)
    if not isinstance(patches, list) or not patches:
        app._surface_local_patch_actors = []
        return
    plotter = getattr(app, "vtk_widget", None)
    if plotter is not None:
        for entry in patches:
            actor = entry.get("actor") if isinstance(entry, dict) else entry
            name = entry.get("name") if isinstance(entry, dict) else None
            try:
                if name:
                    plotter.remove_actor(name, render=False)
                elif actor is not None:
                    plotter.remove_actor(actor, render=False)
            except Exception:
                pass
    app._surface_local_patch_actors = []


def _surface_build_local_patch_actor(app, result: dict, name: str, priority: float):
    """Render a small exact local Surface replacement skin above the resident mesh.

    The 30M+ point base mesh is deliberately left immutable.  This is the same
    architectural idea used by the Shading local-update path: classification
    commits create only a tiny replacement actor and therefore avoid a global
    Delaunay + 60M-face VTK upload.
    """
    if not isinstance(result, dict) or result.get("empty", False):
        return None
    pts = np.asarray(result.get("points"), dtype=np.float64)
    faces = np.asarray(result.get("faces"), dtype=np.int32).reshape(-1, 3)
    colors = np.asarray(result.get("colors"), dtype=np.uint8).reshape(-1, 3)
    if len(pts) < 3 or len(faces) == 0 or len(colors) != len(faces):
        return None

    try:
        patch, buffers = _build_surface_polydata_native(pts, faces, colors)
    except Exception:
        packed = np.empty(len(faces) * 4, dtype=np.int32)
        packed[0::4] = 3
        packed[1::4] = faces[:, 0]
        packed[2::4] = faces[:, 1]
        packed[3::4] = faces[:, 2]
        patch = pv.PolyData(pts, packed)
        patch.cell_data["RGB"] = np.ascontiguousarray(colors, dtype=np.uint8)
        buffers = (pts, faces, colors, packed)

    actor = app.vtk_widget.add_mesh(
        patch,
        scalars="RGB",
        rgb=True,
        show_edges=False,
        lighting=False,
        smooth_shading=False,
        preference="cell",
        name=name,
        render=False,
    )
    if actor is None:
        return None

    try:
        setattr(actor, "_is_surface_mesh", True)
        setattr(actor, "_is_surface_local_patch", True)
        setattr(actor, "_naksha_display_mode", "surface")
        actor.PickableOff()
        if hasattr(actor, "UseBoundsOff"):
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
                float(priority), float(priority)
            )
            mapper.InterpolateScalarsBeforeMappingOff()
    except Exception:
        pass

    # Keep all NumPy buffers alive for deep=False VTK arrays.
    actor._naksha_surface_patch_buffers = buffers
    return actor


def _surface_local_membership_indices(app, transition: dict):
    """Return only points whose edit actually changes Surface membership."""
    if not isinstance(transition, dict):
        return np.empty(0, dtype=np.int64)
    idx = np.asarray(transition.get("indices", []), dtype=np.int64).ravel()
    oldv = np.asarray(transition.get("old_classes", [])).ravel()
    newv = np.asarray(transition.get("new_classes", [])).ravel()
    if idx.size == 0 or oldv.size != idx.size or newv.size != idx.size:
        return np.empty(0, dtype=np.int64)
    old_support = _surface_support_flags_for_values(app, oldv)
    new_support = _surface_support_flags_for_values(app, newv)
    return idx[old_support != new_support].astype(np.int64, copy=False)


def _surface_query_local_candidate_indices(app, changed_global_idx: np.ndarray):
    """Find a bounded neighbourhood around topology-changing points.

    One vectorized AABB pass over XYZ is intentionally preferred to building a
    new 34M-point KDTree during classification.  The resulting local candidate
    set is then spatially trimmed with a small KDTree.  On large tiles this is
    memory-light and typically hundreds of milliseconds rather than tens of
    seconds of global triangulation.
    """
    xyz = app.data.get("xyz") if getattr(app, "data", None) else None
    if xyz is None:
        return None, None
    changed_global_idx = np.unique(np.asarray(changed_global_idx, dtype=np.int64).ravel())
    changed_global_idx = changed_global_idx[(changed_global_idx >= 0) & (changed_global_idx < len(xyz))]
    if changed_global_idx.size == 0:
        return np.empty(0, dtype=np.int64), 0.0

    changed_xy = np.asarray(xyz[changed_global_idx, :2], dtype=np.float64)
    # First pass is deliberately generous enough to estimate local density.
    seed_margin = float(getattr(app, "surface_local_patch_seed_margin", 4.0) or 4.0)
    seed_margin = min(max(seed_margin, 1.5), 12.0)
    lo = changed_xy.min(axis=0) - seed_margin
    hi = changed_xy.max(axis=0) + seed_margin

    x = xyz[:, 0]
    y = xyz[:, 1]
    broad_mask = ((x >= lo[0]) & (x <= hi[0]) & (y >= lo[1]) & (y <= hi[1]))
    broad_idx = np.flatnonzero(broad_mask).astype(np.int64, copy=False)
    if broad_idx.size < 3:
        return broad_idx, seed_margin

    # Estimate spacing from this bounded region and use it to choose a stitching
    # radius.  Dense aerial LiDAR normally resolves to the 1.5-4 m floor.
    area = max(float((hi[0] - lo[0]) * (hi[1] - lo[1])), 1.0)
    spacing = float(np.sqrt(area / max(int(broad_idx.size), 1)))
    radius = max(seed_margin * 0.5, spacing * 20.0, 1.5)
    radius = min(radius, float(getattr(app, "surface_local_patch_max_radius", 8.0) or 8.0))

    # The broad set may be elongated because a cross section edited a few
    # separated points.  Keep only neighbourhoods around actual membership
    # changes instead of triangulating the entire cross-section rectangle.
    if broad_idx.size > 5_000:
        try:
            from scipy.spatial import cKDTree
            broad_xy = np.asarray(xyz[broad_idx, :2], dtype=np.float64)
            tree = cKDTree(broad_xy, leafsize=32)
            hits = tree.query_ball_point(changed_xy, r=radius, workers=-1)
            parts = [np.asarray(h, dtype=np.int64) for h in hits if len(h)]
            if parts:
                local_pos = np.unique(np.concatenate(parts))
                broad_idx = broad_idx[local_pos]
        except Exception as exc:
            print(f"SURFACE_LOCAL_PATCH spatial_trim=fallback reason={type(exc).__name__}: {exc}")

    return broad_idx, radius


def _apply_surface_local_classification_patch(app, transition: dict, operation="classification") -> bool:
    """Shading-style Surface refresh for bounded support-membership edits.

    This path updates only the edited neighbourhood and leaves the giant base
    Surface actor untouched.  It is intentionally used for interactive
    classification/undo/redo.  A complete rebuild is still used when the edit
    is too spatially large for a safe local patch or when Surface settings/data
    are changed outside classification.
    """
    t0 = time.perf_counter()
    if str(getattr(app, "display_mode", "") or "").lower() != "surface":
        return False
    if getattr(app, "_surface_mesh_actor", None) is None:
        return False

    topology_idx = _surface_local_membership_indices(app, transition)
    if topology_idx.size == 0:
        return True

    # Large membership changes are not an interactive local-edit case.  Keep
    # the existing exact/background fallback for those operations.
    max_changed = int(getattr(app, "surface_local_patch_max_changed", 50_000) or 50_000)
    if topology_idx.size > max_changed:
        print(
            "SURFACE_LOCAL_PATCH decision=FALLBACK_EXACT "
            f"reason=changed_limit topology_changed={topology_idx.size:,} limit={max_changed:,}"
        )
        return False

    candidate_idx, radius = _surface_query_local_candidate_indices(app, topology_idx)
    if candidate_idx is None or candidate_idx.size < 3:
        return False

    max_candidates = int(getattr(app, "surface_local_patch_max_candidates", 300_000) or 300_000)
    if candidate_idx.size > max_candidates:
        print(
            "SURFACE_LOCAL_PATCH decision=FALLBACK_EXACT "
            f"reason=candidate_limit candidates={candidate_idx.size:,} limit={max_candidates:,}"
        )
        return False

    classes = np.asarray(app.data.get("classification"))
    cand_classes = classes[candidate_idx]
    support = _surface_support_flags_for_values(app, cand_classes)
    local_idx = candidate_idx[support]
    if local_idx.size < 3:
        return False

    quality_mode = normalize_surface_quality(getattr(app, "surface_quality", "normal"))
    precision = float(getattr(app, "surface_dedup_precision", 0.0) or 0.0)
    max_edge = float(getattr(app, "surface_max_edge", 0.0) or 0.0)
    azimuth = float(getattr(app, "last_shade_azimuth", 45.0))
    angle = float(getattr(app, "last_shade_angle", 45.0))
    ambient = float(getattr(app, "shade_ambient", 0.22))
    ramp = _normalize_surface_ramp(
        getattr(app, "surface_color_ramp", None)
        or getattr(app, "elevation_color_ramp", None)
    )
    z_bounds = (
        float(getattr(app, "_surface_z_lo", np.nan)),
        float(getattr(app, "_surface_z_hi", np.nan)),
    )
    if not np.isfinite(z_bounds).all():
        z_bounds = _surface_z_bounds_cached(app, app.data["xyz"])

    result = _compute_surface_geometry_backend(
        app.data["xyz"],
        local_idx,
        precision,
        _surface_quality_target(quality_mode, int(local_idx.size)),
        max_edge,
        azimuth,
        angle,
        ambient,
        ramp,
        progress_callback=None,
        quality_mode=quality_mode,
        z_bounds=z_bounds,
    )
    if result.get("empty", False):
        return False

    generation = int(getattr(app, "_surface_local_patch_generation", 0) or 0) + 1
    app._surface_local_patch_generation = generation
    name = f"surface_live_classification_patch_{generation}"
    # Stay above the stale base Surface but below SNT/DXF overlays (-50000).
    priority = -5000.0 - float(min(generation, 5000)) * 0.5
    actor = _surface_build_local_patch_actor(app, result, name, priority)
    if actor is None:
        return False

    patches = getattr(app, "_surface_local_patch_actors", None)
    if not isinstance(patches, list):
        patches = []
        app._surface_local_patch_actors = patches
    patches.append({
        "name": name,
        "actor": actor,
        "transaction_token": transition.get("transaction_token"),
        "topology_indices": topology_idx.copy(),
        "candidate_count": int(candidate_idx.size),
    })

    # Do NOT retag/store this as a globally exact mesh.  The base cache remains
    # dirty so a later explicit Surface rebuild/mode re-entry can consolidate
    # patches, while interactive classification stays immediate.
    app._surface_mesh_cache_dirty = True
    app._surface_filter_dirty = True
    app._surface_needs_rebuild_after_classification = False
    app._surface_last_refresh_visual_change = True
    app._surface_last_refresh_presented = True
    app._surface_last_local_token = transition.get("transaction_token")

    try:
        _restore_snt_grid_above_surface(app)
    except Exception:
        pass
    try:
        # app.vtk_widget.render() is GPURenderManager's wrapped/throttled
        # version. This runs synchronously right after a classify commit's
        # own render (which just stamped _last_classify_ts / last_render_time),
        # so it can re-enter the classify-streak or general debounce throttle
        # and just reschedule a deferred timer instead of actually painting --
        # the same bug found and fixed for the Shaded-mode overlay presenter
        # (gui/shading_display.py's _schedule_fast_shaded_present). Bypass it
        # the same way.
        mgr = getattr(app, "gpu_render_manager", None)
        if mgr is not None:
            mgr.force_render()
        else:
            app.vtk_widget.render()
        try:
            app.vtk_widget.update()
            from PySide6.QtCore import QEventLoop
            from PySide6.QtWidgets import QApplication
            QApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
        except Exception:
            pass
    except Exception:
        pass

    elapsed = (time.perf_counter() - t0) * 1000.0
    print(
        "SURFACE_LOCAL_PATCH decision=LOCAL_REFRESH "
        f"operation={str(operation or 'classification')} "
        f"topology_changed={topology_idx.size:,} candidates={candidate_idx.size:,} "
        f"eligible={local_idx.size:,} faces={len(result.get('faces', [])):,} "
        f"radius={radius:.2f}m elapsed={elapsed:.1f}ms global_rebuild_avoided=True"
    )
    return True

def refresh_surface_after_classification(app, changed_mask=None, operation="classification", delay_ms=300):
    """Refresh Surface after classification with Shading-style edit planning.

    Fast invariant:
      * support -> support classification does NOT change Surface geometry
      * non-support -> non-support classification does NOT change Surface geometry

    Those edits now complete in O(K) using the existing undo transaction and do
    not touch the multi-million-triangle Surface VTK object at all.

    A true support-membership change first uses the bounded local topology
    replacement patch. Only spatially huge/unsupported edits fall back to the
    exact asynchronous full Surface rebuild.
    """
    if str(getattr(app, "display_mode", "") or "").lower() != "surface":
        return False

    t_plan = time.perf_counter()
    app._surface_last_refresh_visual_change = False
    app._surface_last_refresh_presented = False

    if changed_mask is None:
        changed_mask = getattr(app, "_last_changed_mask", None)

    transition = _surface_resolve_classification_transition(
        app, changed_mask, operation=operation
    )
    summary = _surface_transition_membership_summary(app, transition)

    if summary is not None:
        changed_count = int(len(transition.get("indices", [])))
        add_count = int(summary["add_count"])
        remove_count = int(summary["remove_count"])

        if not summary["topology_changed"]:
            # If an older, genuinely topology-changing edit is already waiting
            # on the debounce timer, do not retag that stale geometry as valid.
            timer = getattr(app, "_surface_rebuild_after_classification_timer", None)
            timer_active = bool(
                timer is not None and getattr(timer, "isActive", lambda: False)()
            )
            async_worker = getattr(app, "_surface_async_worker", None)
            try:
                async_active = async_worker is not None and bool(async_worker.isRunning())
            except Exception:
                async_active = async_worker is not None
            prior_topology_pending = bool(
                getattr(app, "_surface_needs_rebuild_after_classification", False)
                and (timer_active or async_active)
            )

            if not prior_topology_pending:
                _retag_surface_cache_after_classification_no_topology(app)

            app._surface_last_refresh_visual_change = False
            app._surface_last_refresh_presented = False
            print(
                "SURFACE_FAST_REFRESH decision=NO_TOPOLOGY_CHANGE "
                f"operation={str(operation or 'classification')} "
                f"changed={changed_count} support_add=0 support_remove=0 "
                f"source={transition.get('source', 'unknown')} "
                f"pending_prior_topology={int(prior_topology_pending)} "
                f"elapsed={(time.perf_counter()-t_plan)*1000.0:.2f}ms"
            )
            return True

        # The same classification transaction can reach Surface twice
        # (classifier + cross-section commit).  Local patches are transaction
        # scoped, so the second hook must become a no-op.
        transaction_token = transition.get("transaction_token")
        if (
            transaction_token is not None
            and transaction_token == getattr(app, "_surface_last_local_token", None)
        ):
            print(
                "SURFACE_EDIT_PLAN decision=TOPOLOGY_CHANGE duplicate=coalesced "
                f"operation={str(operation or 'classification')} "
                f"changed={changed_count} support_add={add_count} "
                f"support_remove={remove_count} source={transition.get('source', 'unknown')} "
                "path=local_patch"
            )
            return True

        # Interactive classification now mirrors Shading: first try an exact
        # bounded replacement skin.  This avoids touching the 34M-point base
        # Surface or launching a 50-second full Delaunay for a handful of
        # support-membership changes.
        try:
            if _apply_surface_local_classification_patch(
                app, transition, operation=operation
            ):
                print(
                    "SURFACE_EDIT_PLAN decision=TOPOLOGY_CHANGE "
                    f"operation={str(operation or 'classification')} "
                    f"changed={changed_count} support_add={add_count} "
                    f"support_remove={remove_count} source={transition.get('source', 'unknown')} "
                    "path=local_patch full_rebuild=0"
                )
                return True
        except Exception as local_exc:
            print(
                "SURFACE_LOCAL_PATCH decision=FALLBACK_EXACT "
                f"reason={type(local_exc).__name__}: {local_exc}"
            )

        # Fallback for spatially huge/unsupported edits: preserve the existing
        # exact asynchronous global rebuild path.
        timer = getattr(app, "_surface_rebuild_after_classification_timer", None)
        timer_active = bool(
            timer is not None and getattr(timer, "isActive", lambda: False)()
        )
        async_worker = getattr(app, "_surface_async_worker", None)
        try:
            async_active = async_worker is not None and bool(async_worker.isRunning())
        except Exception:
            async_active = async_worker is not None
        topology_pending = bool(
            getattr(app, "_surface_needs_rebuild_after_classification", False)
            or timer_active
            or async_active
        )

        if (
            transaction_token is not None
            and transaction_token == getattr(app, "_surface_last_topology_token", None)
            and topology_pending
        ):
            print(
                "SURFACE_EDIT_PLAN decision=TOPOLOGY_CHANGE duplicate=coalesced "
                f"operation={str(operation or 'classification')} "
                f"changed={changed_count} support_add={add_count} "
                f"support_remove={remove_count} source={transition.get('source', 'unknown')}"
            )
            return True

        try:
            app._surface_last_topology_token = transaction_token
            app._surface_topology_generation = int(
                getattr(app, "_surface_topology_generation", 0) or 0
            ) + 1
        except Exception:
            pass

        print(
            "SURFACE_EDIT_PLAN decision=TOPOLOGY_CHANGE "
            f"operation={str(operation or 'classification')} "
            f"changed={changed_count} support_add={add_count} "
            f"support_remove={remove_count} source={transition.get('source', 'unknown')} "
            f"generation={int(getattr(app, '_surface_topology_generation', 0) or 0)}"
        )
    else:
        # Unknown transitions stay on the safe exact path.  Use the bool-mask
        # object identity as a fallback transaction token so two integration
        # hooks carrying the very same edit do not queue duplicate global work.
        fallback_token = ("mask", id(changed_mask)) if changed_mask is not None else None
        timer = getattr(app, "_surface_rebuild_after_classification_timer", None)
        timer_active = bool(
            timer is not None and getattr(timer, "isActive", lambda: False)()
        )
        async_worker = getattr(app, "_surface_async_worker", None)
        try:
            async_active = async_worker is not None and bool(async_worker.isRunning())
        except Exception:
            async_active = async_worker is not None
        topology_pending = bool(
            getattr(app, "_surface_needs_rebuild_after_classification", False)
            or timer_active
            or async_active
        )
        if (
            fallback_token is not None
            and fallback_token == getattr(app, "_surface_last_topology_token", None)
            and topology_pending
        ):
            print(
                "SURFACE_EDIT_PLAN decision=UNKNOWN duplicate=coalesced "
                f"operation={str(operation or 'classification')}"
            )
            return True
        try:
            app._surface_last_topology_token = fallback_token
            app._surface_topology_generation = int(
                getattr(app, "_surface_topology_generation", 0) or 0
            ) + 1
        except Exception:
            pass

    # From this point onward the existing exact Surface behavior is preserved.
    # Unknown transitions also stay on the safe rebuild path.
    try:
        app._surface_needs_rebuild_after_classification = True
    except Exception:
        pass

    operation_lc = str(operation or "").lower()

    # Keep the historical direct local-bridge route only when no transaction
    # semantics are available.  For a KNOWN support-membership change that
    # bridge would be topologically stale, so use the exact rebuild path.
    if int(delay_ms or 0) <= 0 and operation_lc not in {"undo", "redo"} and summary is None:
        try:
            ok = _apply_surface_bridge_update(app, changed_mask=changed_mask, operation=operation)
            if ok:
                app._surface_needs_rebuild_after_classification = False
                app._surface_last_refresh_visual_change = True
                app._surface_last_refresh_presented = True
                print("⚡ Surface classification changed — updated Surface mesh in-place")
            else:
                print("🔁 Surface in-place update unavailable — rebuilding Surface mesh")
                ok = render_surface_mode(app, silent=True)
                app._surface_last_refresh_visual_change = bool(ok)
                app._surface_last_refresh_presented = bool(ok)
                if ok:
                    print("✅ Surface rebuilt after classification")
                else:
                    print("⚠️ Surface rebuild after classification returned False")
            return ok
        except Exception as e:
            print(f"⚠️ Surface rebuild after classification failed: {e}")
            return False

    if operation_lc in {"undo", "redo"}:
        try:
            from gui.surface_mode import mark_surface_filter_dirty
            mark_surface_filter_dirty(app, reason=operation_lc)
        except Exception:
            pass
        try:
            # Undo/redo can change Surface membership just like a live
            # classification.  Use the same exact non-modal rebuild so Ctrl+Z
            # never freezes the GUI behind a 30M-point triangulation.
            print(f"🔁 Surface {operation_lc} — exact background rebuild")
            ok = _start_surface_async_exact_rebuild(app)
            if ok:
                app._surface_last_refresh_visual_change = False
                app._surface_last_refresh_presented = False
                return True

            print(
                f"⚠️ Surface async worker unavailable for {operation_lc} — "
                "using synchronous exact rebuild"
            )
            ok = render_surface_mode(app, silent=True)
            app._surface_needs_rebuild_after_classification = not bool(ok)
            app._surface_last_refresh_visual_change = bool(ok)
            app._surface_last_refresh_presented = bool(ok)
            if ok:
                print(f"✅ Surface rebuilt after {operation_lc}")
            else:
                print(f"⚠️ Surface rebuild after {operation_lc} returned False")
            return ok
        except Exception as e:
            print(f"⚠️ Surface rebuild after {operation_lc} failed: {e}")
            return False

    try:
        from PySide6.QtCore import QTimer
    except Exception:
        try:
            print("🔁 Surface classification changed — rebuilding Surface mesh now")
            ok = render_surface_mode(app, silent=True)
            app._surface_last_refresh_visual_change = bool(ok)
            app._surface_last_refresh_presented = bool(ok)
            return ok
        except Exception as e:
            print(f"⚠️ Surface rebuild after classification failed: {e}")
            return False

    timer = getattr(app, "_surface_rebuild_after_classification_timer", None)

    if timer is None:
        timer = QTimer(app)
        timer.setSingleShot(True)
        app._surface_rebuild_after_classification_timer = timer

        def _run_surface_rebuild():
            try:
                if str(getattr(app, "display_mode", "") or "").lower() != "surface":
                    return

                if not bool(getattr(app, "_surface_needs_rebuild_after_classification", False)):
                    return

                # Classification refresh is deliberately non-modal.  Keep the
                # exact resident Surface visible, compute the replacement on a
                # QThread, and atomically swap it when ready.  This removes the
                # 20-50+ second GUI stall seen on 30M+ Slow surfaces.
                print("🔁 Surface classification changed — exact background rebuild")
                ok = _start_surface_async_exact_rebuild(app)
                if not ok:
                    # Compatibility fallback for environments where QThread is
                    # unavailable.  Preserve the historical exact behavior.
                    print("⚠️ Surface async worker unavailable — using synchronous exact rebuild")
                    try:
                        fresh_vis_mask = surface_visible_mask(app)
                    except Exception:
                        fresh_vis_mask = None
                    ok = render_surface_mode(app, vis_mask=fresh_vis_mask, silent=True)
                    app._surface_needs_rebuild_after_classification = not bool(ok)
                    app._surface_last_refresh_visual_change = bool(ok)
                    app._surface_last_refresh_presented = bool(ok)
                    if ok:
                        print("✅ Surface rebuilt after classification")
                    else:
                        print("⚠️ Surface rebuild after classification returned False")

            except Exception as e:
                app._surface_last_refresh_visual_change = False
                app._surface_last_refresh_presented = False
                print(f"⚠️ Surface rebuild after classification failed: {e}")

        timer.timeout.connect(_run_surface_rebuild)

    timer.start(int(delay_ms))
    app._surface_last_refresh_visual_change = False
    app._surface_last_refresh_presented = False
    print("⏳ Surface topology changed — exact rebuild scheduled")
    return True


# ============================================================================
# Surface right-click settings popup
# ============================================================================

def _surface_float(value, fallback):
    try:
        return float(value)
    except Exception:
        return float(fallback)


def _surface_settings_allowed(app) -> bool:
    """
    Allow Surface Settings only when real LAZ/LAS point data and
    a valid visible Surface mesh still exist.
    Prevents stale Surface mode from opening settings after grid/LAZ clear.
    """
    try:
        data = getattr(app, "data", None)
        xyz = data.get("xyz") if isinstance(data, dict) else None

        if xyz is None or len(xyz) == 0:
            return False

        display_mode = str(getattr(app, "display_mode", "") or "").lower()
        current_mode = str(getattr(app, "current_display_mode", "") or "").lower()

        if display_mode != "surface" and current_mode != "surface":
            return False

        surface_actor = getattr(app, "_surface_mesh_actor", None)
        surface_mesh = getattr(app, "_surface_mesh_polydata", None)
        surface_points = getattr(app, "_surface_points", None)
        surface_faces = getattr(app, "_surface_faces", None)

        if surface_actor is None:
            return False

        if surface_mesh is None:
            return False

        if surface_points is None or surface_faces is None:
            return False

        if len(surface_points) == 0 or len(surface_faces) == 0:
            return False

        try:
            if hasattr(surface_actor, "GetVisibility") and not surface_actor.GetVisibility():
                return False
        except Exception:
            pass

        return True

    except Exception:
        return False


def _apply_surface_settings_from_popup(app, max_edge, azimuth, angle, ambient, full_rebuild=False):
    """
    Apply Surface display settings without affecting RGB/Class/Shading/Depth/etc.

    - Azimuth / Angle / Ambient can update the existing Surface mesh colors.
    - Max edge changes require Surface rebuild because triangle filtering changes.
    """
    old_max_edge = _surface_float(getattr(app, "surface_max_edge", 0.0), 0.0)

    app.surface_max_edge = float(max_edge)
    app.last_shade_max_edge = float(max_edge)
    app.last_shade_azimuth = float(azimuth)
    app.last_shade_angle = float(angle)
    app.shade_ambient = float(ambient)

    if str(getattr(app, "display_mode", "") or "").lower() != "surface":
        return False

    edge_changed = abs(float(max_edge) - float(old_max_edge)) > 1e-9

    try:
        points = getattr(app, "_surface_points", None)
        faces = getattr(app, "_surface_faces", None)

        if full_rebuild or edge_changed or points is None or faces is None:
            print("🔁 Surface settings changed — rebuilding Surface mesh")
            return render_surface_mode(app, silent=False)

        ok = _update_existing_surface_polydata(app, points, faces)
        if ok:
            print("✅ Surface settings applied")
        return ok

    except Exception as e:
        print(f"⚠️ Surface settings apply failed: {e}")
        
def show_surface_gradient_controls(app):
    """
    Surface Color Gradient settings.

    Use this only for Shift + Surface button.
    Do NOT use this for canvas right-click.
    """
    try:
        from PySide6.QtWidgets import QDialog, QMessageBox
        from PySide6.QtCore import QSettings
        from gui.elevation_settings_dialog import ElevationSettingsDialog
    except Exception as e:
        print(f"⚠️ Surface gradient controls unavailable: {e}")
        return False

    try:
        data = getattr(app, "data", None)
        xyz = data.get("xyz") if isinstance(data, dict) else None

        if xyz is None or len(xyz) == 0:
            QMessageBox.warning(
                app,
                "No Data",
                "Please load a LAZ/LAS point cloud first."
            )
            return False
    except Exception:
        return False

    dlg = ElevationSettingsDialog(
        app,
        app=app,
        ramp_attr="surface_color_ramp",
        title="Surface",
        subtitle="Customize how surface terrain is colored.",
        default_label="Default: Elevation-style terrain gradient",
    )

    if dlg.exec() == QDialog.Accepted:
        app.surface_color_ramp = dlg.get_color_ramp()

        try:
            settings = QSettings("NakshaAI", "LidarApp")
            settings.setValue(
                "surface_color_ramp",
                [(float(pos), list(color)) for pos, color in app.surface_color_ramp],
            )
            settings.sync()
        except Exception as e:
            print(f"⚠️ Failed to save surface ramp: {e}")

        try:
            app._surface_mesh_cache_dirty = True
            app._surface_cache_revision = int(getattr(app, "_surface_cache_revision", 0) or 0) + 1
        except Exception:
            pass

        try:
            if str(getattr(app, "display_mode", "") or "").lower() == "surface":
                render_surface_mode(app, silent=True)
        except Exception as e:
            print(f"⚠️ Surface gradient apply failed: {e}")

        return True

    return False


def show_surface_controls(app):
    """
    Show Surface settings popup on right-click.

    This is intentionally separate from Shading controls.
    It only appears when app.display_mode/current_display_mode == 'surface'.
    """
    try:
        from PySide6.QtWidgets import (
            QDialog,
            QVBoxLayout,
            QHBoxLayout,
            QLabel,
            QDoubleSpinBox,
            QPushButton,
        )
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QCursor
    except Exception as e:
        print(f"Surface controls unavailable: {e}")
        return False

    # existing Surface guard code continues below...

    # ✅ AccuDraw guard:
    # AccuDraw uses right-click to finish geometry.
    # So Surface settings must NOT open from the same canvas right-click.
    try:
        guard = getattr(app, "_accudraw_canvas_right_click_in_progress", None)
        if callable(guard) and guard():
            print("📐 AccuDraw active/right-click finish — Surface settings blocked")
            return False

        digitizer = getattr(app, "digitizer", None)
        accudraw_tool = getattr(digitizer, "accudraw_tool", None) if digitizer else None

        if accudraw_tool is not None and getattr(accudraw_tool, "active", False):
            print("📐 AccuDraw active — Surface settings blocked")
            return False

        if digitizer is not None and getattr(digitizer, "active_tool", None) == "accudraw":
            print("📐 AccuDraw active_tool — Surface settings blocked")
            return False

        consumed_until = max(
            float(getattr(app, "_accudraw_right_click_consumed_until", 0.0) or 0.0),
            float(getattr(digitizer, "_accudraw_right_click_consumed_until", 0.0) or 0.0) if digitizer else 0.0,
        )

        if time.monotonic() < consumed_until:
            print("📐 AccuDraw consumed this right-click — Surface settings blocked")
            return False

    except Exception:
        pass

    if not _surface_settings_allowed(app):
        try:
            dlg = getattr(app, "_surface_settings_dialog", None)
            if dlg is not None:
                try:
                    dlg.close()
                except Exception:
                    try:
                        dlg.hide()
                    except Exception:
                        pass
            app._surface_settings_dialog = None
        except Exception:
            pass

        print("🏔️ Surface settings blocked — real Surface mesh is not active")
        return False

    dlg = getattr(app, "_surface_settings_dialog", None)

    try:
        if dlg is not None and dlg.isVisible():
            dlg.raise_()
            dlg.activateWindow()
            return True
    except Exception:
        dlg = None

    dlg = QDialog(app)
    dlg.setWindowTitle("Surface")
    dlg.setWindowFlags(
        Qt.Tool
        | Qt.WindowTitleHint
        | Qt.WindowCloseButtonHint
        | Qt.CustomizeWindowHint
    )
    dlg.setAttribute(Qt.WA_DeleteOnClose, False)

    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(10, 10, 10, 10)
    layout.setSpacing(8)

    def _row(label_text, value, minimum, maximum, decimals, step):
        row = QHBoxLayout()

        label = QLabel(label_text)
        spin = QDoubleSpinBox()
        spin.setRange(float(minimum), float(maximum))
        spin.setDecimals(int(decimals))
        spin.setSingleStep(float(step))
        spin.setValue(float(value))
        spin.setMinimumWidth(100)

        row.addWidget(label)
        row.addWidget(spin)
        layout.addLayout(row)

        return spin

    max_edge_spin = _row(
        "Max edge:",
        _surface_float(getattr(app, "surface_max_edge", getattr(app, "last_shade_max_edge", 0.0)), 0.0),
        0.0,
        999999.0,
        2,
        1.0,
    )

    azimuth_spin = _row(
        "Azimuth:",
        _surface_float(getattr(app, "surface_azimuth", getattr(app, "last_shade_azimuth", 45.0)), 45.0),
        0.0,
        360.0,
        2,
        1.0,
    )

    angle_spin = _row(
        "Angle:",
        _surface_float(getattr(app, "surface_angle", getattr(app, "last_shade_angle", 45.0)), 45.0),
        -90.0,
        90.0,
        2,
        1.0,
    )

    ambient_spin = _row(
        "Ambient:",
        _surface_float(getattr(app, "surface_ambient", getattr(app, "shade_ambient", 0.10)), 0.10),
        0.0,
        1.0,
        2,
        0.05,
    )

    apply_btn = QPushButton("Apply")
    rebuild_btn = QPushButton("Full Rebuild")

    layout.addWidget(apply_btn)
    layout.addWidget(rebuild_btn)

    def _read_values():
        return (
            float(max_edge_spin.value()),
            float(azimuth_spin.value()),
            float(angle_spin.value()),
            float(ambient_spin.value()),
        )

    def _apply_only():
        max_edge, azimuth, angle, ambient = _read_values()
        _apply_surface_settings_from_popup(
            app,
            max_edge,
            azimuth,
            angle,
            ambient,
            False,
        )

    def _full_rebuild():
        max_edge, azimuth, angle, ambient = _read_values()

        try:
            app._surface_mesh_cache_dirty = True
            app._surface_cache_revision = int(getattr(app, "_surface_cache_revision", 0) or 0) + 1
        except Exception:
            pass

        _apply_surface_settings_from_popup(
            app,
            max_edge,
            azimuth,
            angle,
            ambient,
            True,
        )

    apply_btn.clicked.connect(_apply_only)
    rebuild_btn.clicked.connect(_full_rebuild)

    app._surface_settings_dialog = dlg

    try:
        dlg.move(QCursor.pos())
    except Exception:
        pass

    dlg.show()
    dlg.raise_()

    try:
        dlg.activateWindow()
    except Exception:
        pass

    return True
