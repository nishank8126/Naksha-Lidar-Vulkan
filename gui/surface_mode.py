"""
Surface display mode integration.

This module is intentionally isolated so the finalized GUI code only needs small,
low-risk hooks in pointcloud_display.py, app_window.py, classification_tools.py,
classification_fast.py, menu_sidebar_system.py and display_mode.py.
"""

from __future__ import annotations

import time
from typing import Optional

import numpy as np
import pyvista as pv
from scipy.spatial import Delaunay, cKDTree

try:
    from PySide6.QtWidgets import QApplication, QProgressDialog, QWidget
    from PySide6.QtCore import QObject, QEvent, QEventLoop, Qt, QThread, Signal
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


SURFACE_ACTOR_NAME = "surface_mesh"


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
                )
                self.finished_signal.emit(res)
            except Exception:
                import traceback
                self.error_signal.emit(traceback.format_exc())
else:
    _SurfaceComputationWorker = None


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
    if faces is None or len(faces) == 0:
        return np.zeros((0, 3), dtype=np.uint8)

    tri = points[faces]
    v1 = tri[:, 1] - tri[:, 0]
    v2 = tri[:, 2] - tri[:, 0]
    normals = np.cross(v1, v2)
    normals /= np.linalg.norm(normals, axis=1, keepdims=True) + 1e-12

    az = np.deg2rad(float(azimuth))
    el = np.deg2rad(float(angle))
    light = np.array([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)], dtype=np.float64)
    light /= np.linalg.norm(light) + 1e-12
    shade = float(ambient) + (1.0 - float(ambient)) * np.clip(normals @ light, 0.0, 1.0)

    z = points[:, 2]
    lo = float(z_lo)
    hi = float(z_hi)
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        lo = float(np.percentile(z, 1.0))
        hi = float(np.percentile(z, 99.0))
        if hi <= lo:
            lo, hi = float(np.min(z)), float(np.max(z))
    if hi <= lo:
        hi = lo + 1.0

    z_face = np.mean(z[faces], axis=1)
    norm = np.clip((z_face - lo) / (hi - lo), 0.0, 1.0)

    if ramp:
        try:
            stops = np.array([float(pos) for pos, _ in ramp], dtype=np.float64)
            colors = np.array([list(color[:3]) for _, color in ramp], dtype=np.float64)
            if len(stops) >= 2:
                rgb = np.column_stack([
                    np.interp(norm, stops, colors[:, 0]),
                    np.interp(norm, stops, colors[:, 1]),
                    np.interp(norm, stops, colors[:, 2]),
                ])
            else:
                raise ValueError("surface ramp needs at least two stops")
        except Exception:
            ramp = None

    if not ramp:
        stops = np.array([0.00, 0.25, 0.50, 0.75, 1.00], dtype=np.float64)
        r = np.interp(norm, stops, np.array([0, 0, 0, 255, 255], dtype=np.float64))
        g = np.interp(norm, stops, np.array([0, 200, 255, 255, 80], dtype=np.float64))
        b = np.interp(norm, stops, np.array([255, 255, 0, 0, 0], dtype=np.float64))
        rgb = np.column_stack([r, g, b])

    rgb *= shade[:, None]
    return np.clip(rgb, 0, 255).astype(np.uint8)


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
) -> dict:
    def _emit(value, message):
        if callable(progress_callback):
            try:
                progress_callback(int(value), str(message))
            except Exception:
                pass

    if global_idx is None or int(getattr(global_idx, "size", 0)) < 3:
        return {"empty": True}

    xyz = xyz_all[global_idx].astype(np.float64, copy=False)
    if len(xyz) < 3:
        return {"empty": True}

    _emit(20, "Filtering Surface points...")

    offset = xyz.min(axis=0)
    local = xyz - offset
    xy_range = np.ptp(local[:, :2], axis=0)
    area = max(float(xy_range[0] * xy_range[1]), 1.0)
    spacing = float(np.sqrt(area / max(len(local), 1)))

    precision = float(precision or 0.0)
    if precision <= 0.0:
        precision = max(spacing * 0.35, 0.005)

    _emit(35, "Reducing duplicate Surface points...")

    unique_local_idx = _grid_dedup_indices(local, precision)
    target_max = int(target_max or 1_500_000)
    if len(unique_local_idx) > target_max:
        stride = int(np.ceil(len(unique_local_idx) / target_max))
        unique_local_idx = unique_local_idx[::max(stride, 1)]

    if len(unique_local_idx) < 3:
        return {"empty": True}

    pts = xyz[unique_local_idx].copy()
    unique_global_idx = global_idx[unique_local_idx].astype(np.int64, copy=False)
    local_pts = pts - offset
    xy = local_pts[:, :2]

    _emit(55, "Triangulating Surface mesh...")

    tri = Delaunay(xy)
    faces = tri.simplices.astype(np.int32, copy=False)

    data_extent = max(float(np.ptp(xy[:, 0])), float(np.ptp(xy[:, 1])), 1.0)
    max_edge = float(max_edge or 0.0)
    if max_edge <= 0.0:
        max_edge = max(data_extent * 0.10, spacing * 100.0)

    _emit(75, "Cleaning long Surface triangles...")

    faces = _filter_long_edges(faces, xy, max_edge)
    if len(faces) == 0:
        return {"empty": True}

    z_lo = float(np.percentile(xyz_all[:, 2], 1.0))
    z_hi = float(np.percentile(xyz_all[:, 2], 99.0))

    _emit(88, "Applying Surface elevation shading...")

    colors = _compute_surface_face_colors(
        pts,
        faces,
        azimuth,
        angle,
        ambient,
        z_lo,
        z_hi,
        ramp=ramp,
    )

    return {
        "empty": False,
        "points": pts,
        "faces": faces,
        "colors": colors,
        "unique_global_idx": unique_global_idx,
        "z_lo": z_lo,
        "z_hi": z_hi,
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
    """
    Decide whether a class should participate in terrain Surface generation.
    Default rule: checked/visible classes participate, but obvious noise classes do not.
    """
    if not entry.get("show", True):
        return False
    if entry.get("surface", None) is not None:
        return bool(entry.get("surface"))
    if entry.get("surface_support", None) is not None:
        return bool(entry.get("surface_support"))

    # LAS/common non-terrain classes that should not drive the surface.
    non_surface = {7, 18}  # low/noise/high-noise
    return int(code) not in non_surface


def surface_visible_mask(app) -> np.ndarray:
    xyz = app.data.get("xyz") if getattr(app, "data", None) else None
    if xyz is None:
        return np.zeros(0, dtype=bool)

    classes = app.data.get("classification")
    from gui.flight_line_filter import flight_line_visibility_mask
    line_mask = flight_line_visibility_mask(app, len(xyz))
    if classes is None:
        return line_mask

    palette = surface_palette(app)
    if not palette:
        return line_mask

    mask = np.zeros(len(xyz), dtype=bool)
    for code in np.unique(classes):
        entry = palette.get(int(code), {"show": True})
        if _surface_support_entry(int(code), entry):
            mask |= classes == int(code)
    return mask & line_mask

def _class_is_surface_support(app, code: int) -> bool:
    """
    True if this class currently participates in Surface mesh generation.
    Used to avoid unnecessary Surface rebuilds after classification.
    """
    try:
        palette = surface_palette(app)
        entry = palette.get(int(code), {"show": True})
        return _surface_support_entry(int(code), entry)
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


def surface_visible_class_signature(app):
    """
    Return the class visibility signature used for Surface generation.

    Used to detect Display Mode changes like:
    All Classes Surface -> Ground Only Surface.
    """
    try:
        xyz = app.data.get("xyz") if getattr(app, "data", None) else None
        classes = app.data.get("classification") if getattr(app, "data", None) else None

        if xyz is None:
            return ("NO_DATA",)

        if classes is None:
            return ("NO_CLASSIFICATION", int(len(xyz)))

        palette = surface_palette(app)
        visible_codes = []

        for code in np.unique(classes):
            code_int = int(code)
            entry = palette.get(code_int, {"show": True})
            if _surface_support_entry(code_int, entry):
                visible_codes.append(code_int)

        return tuple(sorted(visible_codes))

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
    if str(requested_mode or "").lower() == "surface":
        return

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
            app._surface_mesh_cache_dirty = True
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
            )

        ramp = getattr(app, "surface_color_ramp", None) or getattr(app, "elevation_color_ramp", None) or []
        ramp_sig = tuple(
            (round(float(pos), 6), tuple(int(c) for c in (color[:3] if isinstance(color, (list, tuple)) else color)))
            for pos, color in ramp
        )

        return (
            _surface_data_signature(app),
            preset_sig,
            ramp_sig,
            int(getattr(app, "classification_revision", 0) or 0),
            int(getattr(app, "_surface_cache_revision", 0) or 0),
        )
    except Exception:
        return ("SURFACE_CACHE_SIGNATURE_ERROR",)


def _store_surface_mesh_cache(app) -> bool:
    """
    Store only the latest Surface mesh cache.

    Important:
    - This does not keep old Surface actors alive.
    - It stores the mesh data so Surface can be restored instantly later.
    - One cache only, so memory does not grow with every Surface shortcut.
    """
    try:
        mesh = getattr(app, "_surface_mesh_polydata", None)
        points = getattr(app, "_surface_points", None)
        faces = getattr(app, "_surface_faces", None)

        if mesh is None or points is None or faces is None:
            return False

        signature = surface_mesh_cache_signature(app)

        app._surface_mesh_cache = {
            "signature": signature,
            "mesh": mesh,
            "points": points,
            "faces": faces,
            "unique_global_indices": getattr(app, "_surface_unique_global_indices", None),
            "global_to_unique": getattr(app, "_surface_global_to_unique", None),
        }

        app._surface_mesh_cache_dirty = False
        app._surface_filter_dirty = False
        app._surface_needs_rebuild_after_classification = False
        print("💾 Surface mesh cached")
        return True

    except Exception as e:
        print(f"⚠️ Surface mesh cache store skipped: {e}")
        return False


def restore_cached_surface_mesh(app, signature=None) -> bool:
    """
    Restore cached Surface mesh instantly.

    Safe behavior:
    - Only restores in Surface path.
    - Removes Shading mesh first.
    - Hides unified/class point actor.
    - Does NOT restore Surface under ByClass/Shading/RGB.
    """
    try:
        if bool(getattr(app, "_surface_mesh_cache_dirty", False)):
            return False

        cache = getattr(app, "_surface_mesh_cache", None)
        if not isinstance(cache, dict):
            return False

        expected = signature or surface_mesh_cache_signature(app)
        if cache.get("signature") != expected:
            return False

        mesh = cache.get("mesh")
        if mesh is None:
            return False

        vtk_widget = getattr(app, "vtk_widget", None)
        if vtk_widget is None:
            return False

        # Remove Shading mesh before restoring Surface.
        try:
            remove_shaded_class_mesh_actors(app)
        except Exception:
            pass

        # Remove old live Surface actor if any, but keep cache.
        try:
            vtk_widget.remove_actor(SURFACE_ACTOR_NAME, render=False)
        except Exception:
            pass

        actor = vtk_widget.add_mesh(
            mesh,
            scalars="RGB",
            rgb=True,
            show_edges=False,
            name=SURFACE_ACTOR_NAME,
            render=False,
        )

        setattr(actor, "_is_surface_mesh", True)
        setattr(actor, "_naksha_display_mode", "surface")

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
        app._surface_global_to_unique = cache.get("global_to_unique")

        app.display_mode = "surface"
        app.current_display_mode = "surface"
        app._surface_shortcut_signature = expected
        _remember_surface_signature(app)

        try:
            _restore_snt_grid_above_surface(app)
        except Exception as e:
            print(f"⚠️ Cached Surface SNT/grid restore skipped: {e}")

        try:
            vtk_widget.render()
        except Exception:
            pass

        print("⚡ Surface mesh restored from cache")
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
    if faces is None or len(faces) == 0:
        return np.empty((0, 3), dtype=np.int32)
    if max_edge <= 0:
        return faces.astype(np.int32, copy=False)
    p0 = xy[faces[:, 0]]
    p1 = xy[faces[:, 1]]
    p2 = xy[faces[:, 2]]
    e01 = np.linalg.norm(p0 - p1, axis=1)
    e12 = np.linalg.norm(p1 - p2, axis=1)
    e20 = np.linalg.norm(p2 - p0, axis=1)
    keep = (e01 <= max_edge) & (e12 <= max_edge) & (e20 <= max_edge)
    return faces[keep].astype(np.int32, copy=False)


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


def _set_mesh_actor(app, points: np.ndarray, faces: np.ndarray, colors: np.ndarray) -> bool:
    try:
        faces_pv = np.hstack([np.full((len(faces), 1), 3, dtype=np.int32), faces.astype(np.int32)]).ravel()
        mesh = pv.PolyData(points.astype(np.float64, copy=False), faces_pv)
        mesh.cell_data["RGB"] = colors.astype(np.uint8, copy=False)

        detach_surface_before_non_surface_mode(app, requested_mode="surface")
        remove_shaded_class_mesh_actors(app)

        try:
            app.vtk_widget.remove_actor(SURFACE_ACTOR_NAME, render=False)
        except Exception:
            pass

        actor = app.vtk_widget.add_mesh(
            mesh,
            scalars="RGB",
            rgb=True,
            show_edges=False,
            name=SURFACE_ACTOR_NAME,
            render=False,
        )
        setattr(actor, "_is_surface_mesh", True)
        setattr(actor, "_naksha_display_mode", "surface")

        # Surface mode should show the terrain mesh, not the class/RGB point actor.
        try:
            unified_actor = getattr(app, "_unified_actor", None)
            if unified_actor is not None:
                unified_actor.VisibilityOff()
        except Exception:
            pass

        app._surface_mesh_actor = actor
        app._surface_mesh_polydata = mesh
        app._surface_points = points
        app._surface_faces = faces.astype(np.int32, copy=False)

        try:
            _restore_snt_grid_above_surface(app)
        except Exception as e:
            print(f"⚠️ Surface SNT/grid restore skipped: {e}")

        app.vtk_widget.render()
        return True
    except Exception as e:
        print(f"⚠️ Surface actor build failed: {e}")
        return False

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
        if global_idx.size < 3:
            detach_surface_before_non_surface_mode(app, requested_mode="surface")
            return False

        precision = float(getattr(app, "surface_dedup_precision", 0.0) or 0.0)
        target_max = int(getattr(app, "surface_target_max_points", 1_500_000) or 1_500_000)
        max_edge = float(getattr(app, "surface_max_edge", 0.0) or 0.0)
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
            )

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

        ok = _set_mesh_actor(app, pts, faces, colors)

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
            app._surface_global_to_unique = {
                int(g): int(i) for i, g in enumerate(unique_global_idx)
            }

            try:
                _store_surface_mesh_cache(app)
            except Exception as _surface_cache_err:
                print(f"⚠️ Surface mesh cache store skipped: {_surface_cache_err}")
            if not silent:
                print(
                    f"🏔️ Surface mode applied: "
                    f"{len(pts):,} vertices, {len(faces):,} faces "
                    f"in {(time.perf_counter() - t0) * 1000:.1f}ms"
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


def refresh_surface_after_classification(app, changed_mask=None, operation="classification", delay_ms=300):
    """
    Refresh Surface after classification without creating flat patch artifacts.

    IMPORTANT:
    Do not use local plane/deformation patching here.
    Cross-section classification can change terrain-support classes, so the
    correct MicroStation-style result is to rebuild the Surface mesh from the
    updated classification data after the user pauses editing.
    """
    if str(getattr(app, "display_mode", "") or "").lower() != "surface":
        return False

    try:
        app._surface_needs_rebuild_after_classification = True
    except Exception:
        pass

    operation_lc = str(operation or "").lower()

    # Immediate fast path: update the existing mesh locally when the surface
    # geometry itself did not change. This is much faster than a full
    # triangulation rebuild and is the preferred path for live classification.
    if int(delay_ms or 0) <= 0 and operation_lc not in {"undo", "redo"}:
        try:
            ok = _apply_surface_bridge_update(app, changed_mask=changed_mask, operation=operation)
            if ok:
                app._surface_needs_rebuild_after_classification = False
                print("⚡ Surface classification changed — updated Surface mesh in-place")
            else:
                print("🔁 Surface in-place update unavailable — rebuilding Surface mesh")
                ok = render_surface_mode(app, silent=True)
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
            print(f"🔁 Surface {operation_lc} — rebuilding Surface mesh")
            ok = render_surface_mode(app, silent=True)
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
            return render_surface_mode(app, silent=True)
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

                # IMPORTANT: do NOT clear the flag before calling
                # render_surface_mode(). render_surface_mode()'s own cache-
                # restore fast path checks this exact flag — if it's already
                # False when render_surface_mode() runs, it thinks nothing
                # changed and just restores the stale cached mesh instead of
                # re-triangulating. Clear it only AFTER a real rebuild
                # succeeds (or on failure, so we don't loop forever).
                # vis_mask is also passed explicitly as a belt-and-braces
                # measure to force render_surface_mode() past its own
                # "vis_mask is None" cache-restore guard.
                print("🔁 Surface classification changed — rebuilding Surface mesh")
                try:
                    from gui.surface_mode import surface_visible_mask
                    fresh_vis_mask = surface_visible_mask(app)
                except Exception:
                    fresh_vis_mask = None

                ok = render_surface_mode(app, vis_mask=fresh_vis_mask, silent=True)

                app._surface_needs_rebuild_after_classification = False

                if ok:
                    print("✅ Surface rebuilt after classification")
                else:
                    print("⚠️ Surface rebuild after classification returned False")

            except Exception as e:
                print(f"⚠️ Surface rebuild after classification failed: {e}")

        timer.timeout.connect(_run_surface_rebuild)

    # Restart timer on every brush/classification update.
    # This prevents rebuilding 10 times while user is still dragging.
    timer.start(int(delay_ms))
    print("⏳ Surface classification changed — rebuild scheduled")
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
