"""
Coordinate pick tool.

Footer-driven click mode: a left-click in the main view resolves the exact
world coordinate at that point and shows it in a small on-canvas label
anchored to the clicked point, plus the existing layer-identify footer slot.
Ctrl+C copies it, Esc stops the tool.

The label is a real VTK vtkTextActor (world-coordinate anchored, like
PointSyncTool's target ring) rather than a floating Qt widget layered on top
of the VTK render surface - a Qt child widget composited over VTK's native/
foreign OpenGL window does not reliably repaint the area it vacates when
moved, which left visual "ghost" copies behind on every pan. Driving the
label through VTK's own render pipeline means it moves for free with the
camera and never leaves stale pixels.

When a LAS/LAZ point cloud is loaded, the click snaps to the nearest
recorded dataset point using PointSyncTool's own pick + nearest-point
lookup, and draws the same target-ring preview PointSyncTool uses - so the
coordinate is the real recorded point, not a raw ray/surface intersection.
Anything else (GIS/DXF/SNT only, no point cloud) falls back to the default
raw world-point pick with no ring.
"""

from __future__ import annotations

from typing import Optional, Tuple

import vtk
from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QLineEdit, QTextEdit, QPlainTextEdit


def format_coordinate(x: float, y: float, z: float, crs=None) -> str:
    """Render a picked world point, degrees for a geographic CRS else meters."""
    if crs is not None and getattr(crs, "is_geographic", False):
        return f"Lon: {x:.6f}  Lat: {y:.6f}  Z: {z:.2f}"
    if crs is None:
        # No canvas CRS resolved: these are the file's raw local/survey
        # coordinates, not lat/lon - flag it so it isn't pasted into a
        # map expecting WGS84 degrees.
        return f"X: {x:.3f}  Y: {y:.3f}  Z: {z:.3f}  (local, CRS unknown)"
    return f"X: {x:.3f}  Y: {y:.3f}  Z: {z:.3f}"


def _horizontal_crs(crs):
    """The 2D horizontal component of `crs` - itself if already 2D, else the
    horizontal member of a compound CRS (horizontal + vertical datum, e.g.
    "UTM 43N + EGM2008 height", common in real GDB deliveries)."""
    try:
        if crs.to_epsg():
            return crs
    except Exception:
        pass
    try:
        for sub in getattr(crs, "sub_crs_list", None) or []:
            if getattr(sub, "is_projected", False) or getattr(sub, "is_geographic", False):
                return sub
    except Exception:
        pass
    return crs


def to_wgs84_lonlat(x: float, y: float, crs) -> Optional[Tuple[float, float]]:
    """Project a canvas-CRS point to WGS84 (lon, lat), or None if not possible.

    A projected X/Y (e.g. UTM meters) is not something Google Maps or any
    lat/lon-based tool understands - it needs to be converted, not just
    formatted differently.
    """
    if crs is None:
        return None
    try:
        import math
        from pyproj import CRS, Transformer

        wgs84 = CRS.from_epsg(4326)
        horizontal = _horizontal_crs(crs)
        if horizontal.equals(wgs84):
            lon, lat = float(x), float(y)
        else:
            transformer = Transformer.from_crs(horizontal, wgs84, always_xy=True)
            lon, lat = transformer.transform(x, y)
        if not (math.isfinite(lon) and math.isfinite(lat)):
            return None
        if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
            return None
        return (float(lon), float(lat))
    except Exception:
        return None


class _CoordinateTextOverlay:
    """VTK-native text label pinned to a world point (offset a few pixels)."""

    def __init__(self, vtk_widget, world_point, text):
        self.vtk_widget = vtk_widget
        self.renderer = getattr(vtk_widget, "renderer", None)

        self._world_ref = vtk.vtkCoordinate()
        self._world_ref.SetCoordinateSystemToWorld()

        self.actor = vtk.vtkTextActor()
        self.actor.SetTextScaleModeToNone()
        pos_coord = self.actor.GetPositionCoordinate()
        pos_coord.SetCoordinateSystemToDisplay()
        pos_coord.SetReferenceCoordinate(self._world_ref)
        pos_coord.SetValue(14, 18)

        prop = self.actor.GetTextProperty()
        prop.SetFontSize(13)
        prop.SetColor(0.965, 0.968, 0.984)
        prop.SetBackgroundColor(0.078, 0.078, 0.094)
        prop.SetBackgroundOpacity(0.85)
        prop.SetFrame(True)
        prop.SetFrameColor(0.47, 0.78, 1.0)
        prop.SetFrameWidth(1)
        prop.SetJustificationToLeft()
        prop.SetVerticalJustificationToBottom()

        self.update_world_point(world_point)
        self.set_text(text)

        if self.renderer is not None:
            self.renderer.AddActor2D(self.actor)

    def update_world_point(self, world_point):
        self._world_ref.SetValue(
            float(world_point[0]), float(world_point[1]), float(world_point[2])
        )

    def set_text(self, text):
        self.actor.SetInput(text)

    def remove(self):
        try:
            if self.renderer is not None:
                self.renderer.RemoveActor2D(self.actor)
        except Exception:
            pass


class CoordinatePickTool(QObject):
    """Click-to-read world-coordinate tool for the main-view."""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.active = False
        self._click_observer = None
        self._text_overlay = None
        self._preview_ring = None
        self._copy_shortcut = None
        self._last_picked_text = None
        print("✅ CoordinatePickTool initialized")

    def activate(self):
        if self.active:
            print("⚠️ Coordinate pick tool already active")
            return

        self._deactivate_conflicting_tools()

        self.active = True
        self._attach_main_observer()
        self._install_copy_shortcut()
        self._sync_cursor(True)
        self._sync_footer_button_state(True)
        self._set_footer_text(None)
        print("🎯 Coordinate pick tool ACTIVATED")

    def deactivate(self):
        if not self.active:
            return

        self.active = False
        self._detach_main_observer()
        self._remove_copy_shortcut()
        self._clear_preview()
        self._sync_cursor(False)
        self._sync_footer_button_state(False)
        self._set_footer_text(None)
        self._last_picked_text = None
        print("🎯 Coordinate pick tool DEACTIVATED")

    def _deactivate_conflicting_tools(self):
        # Only one footer click tool owns the main-view left-click at a time.
        for tool_name in ("point_sync_tool", "snt_layer_pick_tool"):
            tool = getattr(self.app, tool_name, None)
            if tool is not None and getattr(tool, "active", False):
                try:
                    tool.deactivate()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # VTK observer (click only - no continuous hover tracking)
    # ------------------------------------------------------------------

    def _attach_main_observer(self):
        if self._click_observer is not None:
            return

        vtk_widget = getattr(self.app, "vtk_widget", None)
        if vtk_widget is None or not hasattr(vtk_widget, "interactor"):
            return

        try:
            self._click_observer = vtk_widget.interactor.AddObserver(
                "LeftButtonPressEvent", self._on_main_click, -0.9,
            )
            print("   ✅ Coordinate pick observer attached to Main View")
        except Exception as e:
            print(f"   ⚠️ Failed to attach coordinate pick observer: {e}")

    def _detach_main_observer(self):
        if self._click_observer is None:
            return

        vtk_widget = getattr(self.app, "vtk_widget", None)
        if vtk_widget is not None and hasattr(vtk_widget, "interactor"):
            try:
                vtk_widget.interactor.RemoveObserver(self._click_observer)
            except Exception as e:
                print(f"   ⚠️ Failed to remove coordinate pick observer: {e}")
        self._click_observer = None

    def _on_main_click(self, obj, event):
        if not self.active:
            return

        vtk_widget = getattr(self.app, "vtk_widget", None)
        picked = self._pick_exact_coord(vtk_widget)
        if picked is None:
            self._clear_preview()
            return

        x, y, z, is_exact_point = picked
        crs = self._resolve_canvas_crs()
        text = format_coordinate(x, y, z, crs)
        copy_text = text

        # A projected canvas CRS (UTM meters, etc.) can be converted to real
        # WGS84 lat/lon - that's the only thing Google Maps or any lat/lon
        # tool can actually locate, so Ctrl+C copies THAT, not the raw X/Y.
        lonlat = to_wgs84_lonlat(x, y, crs)
        if lonlat is not None:
            lon, lat = lonlat
            text = f"{text}  |  Maps: {lat:.6f},{lon:.6f}"
            copy_text = f"{lat:.6f},{lon:.6f}"

        self._last_picked_text = copy_text
        self._set_footer_text(text)
        self._update_preview(vtk_widget, (x, y, z), is_exact_point, text)

    def _pick_exact_coord(self, vtk_widget) -> Optional[Tuple[float, float, float, bool]]:
        if vtk_widget is None:
            return None

        # LAS/LAZ point cloud loaded: snap to the actual recorded point via
        # PointSyncTool's own pick + nearest-dataset-point lookup, so the
        # reported coordinate is the real point, not a mesh/ray intersection.
        point_sync_tool = getattr(self.app, "point_sync_tool", None)
        if point_sync_tool is not None:
            try:
                xyz = point_sync_tool._get_dataset_xyz()
            except Exception:
                xyz = None
            if xyz is not None and len(xyz) > 0:
                try:
                    picked_pos = point_sync_tool._pick_position(vtk_widget)
                    if picked_pos is not None:
                        index, _dist = point_sync_tool._nearest_global_index(picked_pos, view_index=0)
                        if index is not None:
                            point = xyz[int(index)]
                            return (float(point[0]), float(point[1]), float(point[2]), True)
                except Exception:
                    pass

        # Default behavior: no point cloud (GIS / DXF / SNT surfaces only).
        pick = getattr(self.app, "_pick_world_point", None)
        if not callable(pick):
            return None
        try:
            result = pick(vtk_widget)
        except Exception:
            result = None
        if result is None:
            return None
        return (float(result[0]), float(result[1]), float(result[2]), False)

    def _resolve_canvas_crs(self):
        try:
            from gui.crs_manager import get_canvas_crs
            return get_canvas_crs(self.app)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Copy to clipboard (Ctrl+C, only while this tool owns the mode)
    # ------------------------------------------------------------------

    def _install_copy_shortcut(self):
        if self._copy_shortcut is not None:
            return
        try:
            shortcut = QShortcut(QKeySequence.Copy, self.app)
            shortcut.setContext(Qt.ApplicationShortcut)
            shortcut.activated.connect(self._copy_last_picked)
            self._copy_shortcut = shortcut
        except Exception as e:
            print(f"   ⚠️ Failed to install coordinate copy shortcut: {e}")

    def _remove_copy_shortcut(self):
        if self._copy_shortcut is None:
            return
        try:
            self._copy_shortcut.setEnabled(False)
            self._copy_shortcut.deleteLater()
        except Exception:
            pass
        self._copy_shortcut = None

    def _copy_last_picked(self):
        # Don't steal Ctrl+C from an in-progress text edit elsewhere in the app.
        focus_widget = QApplication.focusWidget()
        if isinstance(focus_widget, (QLineEdit, QTextEdit, QPlainTextEdit)):
            return
        if not self._last_picked_text:
            return
        try:
            QApplication.clipboard().setText(self._last_picked_text)
            if hasattr(self.app, "statusBar"):
                self.app.statusBar().showMessage(
                    f"📋 Copied: {self._last_picked_text}", 2000
                )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Footer / cursor plumbing
    # ------------------------------------------------------------------

    def _set_footer_text(self, text: Optional[str]):
        try:
            if hasattr(self.app, "_set_coordinate_pick_footer_text"):
                self.app._set_coordinate_pick_footer_text(text)
        except Exception:
            pass

    def _sync_footer_button_state(self, enabled: bool):
        try:
            if hasattr(self.app, "_sync_coordinate_pick_footer_button"):
                self.app._sync_coordinate_pick_footer_button(bool(enabled))
        except Exception:
            pass

    def _sync_cursor(self, enabled: bool):
        try:
            if hasattr(self.app, "set_cross_cursor_active"):
                self.app.set_cross_cursor_active(bool(enabled), "coordinate_pick")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # On-canvas preview: VTK text actor tracks the point through pan/zoom
    # for free; the target-ring (reused from PointSyncTool) only appears
    # for an exact LAS/LAZ dataset point.
    # ------------------------------------------------------------------

    def _update_preview(self, vtk_widget, world_point, show_ring, text):
        if vtk_widget is None:
            return

        if show_ring:
            if self._preview_ring is not None and getattr(self._preview_ring, "vtk_widget", None) is vtk_widget:
                self._preview_ring.update_world_point(world_point)
            else:
                self._clear_ring()
                try:
                    from gui.point_sync_tool import _TargetRingOverlay
                    self._preview_ring = _TargetRingOverlay(vtk_widget, world_point)
                except Exception:
                    self._preview_ring = None
        else:
            self._clear_ring()

        if self._text_overlay is not None and self._text_overlay.vtk_widget is vtk_widget:
            self._text_overlay.update_world_point(world_point)
            self._text_overlay.set_text(text)
        else:
            self._clear_text_overlay()
            try:
                self._text_overlay = _CoordinateTextOverlay(vtk_widget, world_point, text)
            except Exception:
                self._text_overlay = None

        try:
            vtk_widget.render()
        except Exception:
            pass

    def _clear_preview(self):
        had_preview = self._preview_ring is not None or self._text_overlay is not None
        vtk_widget = self._preview_ring.vtk_widget if self._preview_ring is not None else (
            self._text_overlay.vtk_widget if self._text_overlay is not None else None
        )
        self._clear_ring()
        self._clear_text_overlay()
        if had_preview and vtk_widget is not None:
            try:
                vtk_widget.render()
            except Exception:
                pass

    def _clear_ring(self):
        if self._preview_ring is not None:
            try:
                self._preview_ring.remove()
            except Exception:
                pass
            self._preview_ring = None

    def _clear_text_overlay(self):
        if self._text_overlay is not None:
            try:
                self._text_overlay.remove()
            except Exception:
                pass
            self._text_overlay = None
