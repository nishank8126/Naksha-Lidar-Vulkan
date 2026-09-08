"""
SNT layer pick tool.

Provides a dedicated click mode in main view that identifies which SNT layer
the clicked feature belongs to, without reusing point-class identification.
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QLabel


class SNTLayerPickTool(QObject):
    """Dedicated SNT layer identifier for main-view left-click."""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.active = False
        self._main_observer = None
        self._prop_picker = None
        self._cell_picker = None
        self._click_popup_label = None
        self._click_popup_timer = QTimer(self)
        self._click_popup_timer.setSingleShot(True)
        self._click_popup_timer.timeout.connect(self._hide_click_popup)
        print("✅ SNTLayerPickTool initialized")

    def activate(self):
        if self.active:
            print("⚠️ SNT layer pick tool already active")
            return

        # Keep click behavior deterministic: only one footer click tool at a time.
        self._deactivate_conflicting_tools()

        self.active = True
        self._attach_main_observer()
        self._sync_cursor(True)
        self._sync_footer_button_state(True)
        self._set_footer_text(None, None, None)
        print("🎯 SNT layer pick tool ACTIVATED")

    def deactivate(self):
        if not self.active:
            return

        self.active = False
        self._detach_main_observer()
        self._hide_click_popup()
        self._sync_cursor(False)
        self._sync_footer_button_state(False)
        self._set_footer_text(None, None, None)
        print("🎯 SNT layer pick tool DEACTIVATED")

    def _deactivate_conflicting_tools(self):
        for tool_name in ("point_sync_tool", "coordinate_pick_tool"):
            tool = getattr(self.app, tool_name, None)
            if tool is not None and getattr(tool, "active", False):
                try:
                    tool.deactivate()
                except Exception:
                    pass

    def _attach_main_observer(self):
        if self._main_observer is not None:
            return

        vtk_widget = getattr(self.app, "vtk_widget", None)
        if vtk_widget is None or not hasattr(vtk_widget, "interactor"):
            return

        try:
            self._main_observer = vtk_widget.interactor.AddObserver(
                "LeftButtonPressEvent",
                self._on_main_click,
                -0.9,
            )
            print("   ✅ SNT layer pick observer attached to Main View")
        except Exception as e:
            print(f"   ⚠️ Failed to attach SNT layer pick observer: {e}")

    def _detach_main_observer(self):
        if self._main_observer is None:
            return

        vtk_widget = getattr(self.app, "vtk_widget", None)
        if vtk_widget is None or not hasattr(vtk_widget, "interactor"):
            self._main_observer = None
            return

        try:
            vtk_widget.interactor.RemoveObserver(self._main_observer)
        except Exception as e:
            print(f"   ⚠️ Failed to remove SNT layer pick observer: {e}")
        finally:
            self._main_observer = None

    def _on_main_click(self, obj, event):
        if not self.active:
            return

        actor = self._pick_main_actor()
        if actor is None:
            self._hide_click_popup()
            return

        layer_name, snt_name = self._resolve_actor_layer(actor)
        if not layer_name:
            self._hide_click_popup()
            return

        entity_count = self._resolve_layer_entity_count(snt_name, layer_name)
        self._set_footer_text(layer_name, snt_name, entity_count)
        self._show_click_popup(layer_name, snt_name, entity_count)

    def _pick_main_actor(self):
        vtk_widget = getattr(self.app, "vtk_widget", None)
        if vtk_widget is None or not hasattr(vtk_widget, "interactor"):
            return None

        # Get overlay renderer (where GIS vector layers live) and main renderer (where SNT/DXF layers live)
        overlay_renderer = None
        try:
            from gui.gis.gis_layers import _overlay_renderer
            overlay_renderer = _overlay_renderer(self.app)
        except Exception:
            pass
        main_renderer = getattr(vtk_widget, "renderer", None)

        renderers = [r for r in [overlay_renderer, main_renderer] if r is not None]
        if not renderers:
            return None

        try:
            x, y = vtk_widget.interactor.GetEventPosition()
        except Exception:
            return None

        try:
            import vtk

            if self._prop_picker is None:
                self._prop_picker = vtk.vtkPropPicker()

            if self._cell_picker is None:
                self._cell_picker = vtk.vtkCellPicker()
                self._cell_picker.SetTolerance(0.01)
        except Exception as e:
            print(f"   ⚠️ Failed to initialize SNT pickers: {e}")
            return None

        for ren in renderers:
            try:
                if self._prop_picker.Pick(x, y, 0, ren):
                    picked = self._prop_picker.GetViewProp()
                    if picked is not None:
                        return picked
            except Exception:
                pass

            try:
                if self._cell_picker.Pick(x, y, 0, ren):
                    picked = self._cell_picker.GetActor()
                    if picked is not None:
                        return picked
            except Exception:
                pass

        return None

    def _resolve_actor_layer(self, actor) -> Tuple[Optional[str], Optional[str]]:
        # Fast path: actor-level tags attached during SNT/DXF render.
        layer_name = str(getattr(actor, "_naksha_snt_layer", "") or "").strip()
        if layer_name:
            source_name = str(getattr(actor, "_naksha_snt_filename", "") or "").strip()
            return layer_name, source_name or None

        # Fallback path: resolve by object identity in attachment caches.
        for list_name in ["snt_attachments", "dxf_attachments"]:
            for attachment in getattr(self.app, list_name, []) or []:
                cache = attachment.get("actor_cache_map", {}) or {}
                source_name = attachment.get("filename") or attachment.get("full_path")
                for cached_layer, actors in cache.items():
                    if not actors:
                        continue
                    for cached_actor in actors:
                        if cached_actor is actor:
                            layer_value = str(cached_layer or "").strip()
                            if layer_value:
                                return layer_value, source_name

        # Fallback path 2: resolve by GIS layers registry (Shapefiles, GeoJSON, GDB, etc.)
        try:
            from gui.gis.gis_layers import _registry
            reg = _registry(self.app)
            for entry in reg:
                layer_actors = entry.get("actors", [])
                if actor in layer_actors:
                    layer_name = entry.get("name")
                    source_name = entry.get("path")
                    # Try to locate specific subtype/subclass if applicable
                    sub_layers = entry.get("sub_layers", {})
                    if sub_layers:
                        for sub_code, sub_info in sub_layers.items():
                            sub_actors = sub_info.get("actors", [])
                            if actor in sub_actors:
                                sub_label = sub_info.get("label")
                                if sub_label and sub_label != layer_name:
                                    return f"{layer_name} · {sub_label}", source_name
                    return layer_name, source_name
        except Exception:
            pass

        return None, None

    def _resolve_layer_entity_count(self, snt_name: Optional[str], layer_name: str) -> Optional[int]:
        lname = str(layer_name or "").strip()
        if not lname:
            return None

        snt_base = os.path.basename(str(snt_name)) if snt_name else None
        for list_name in ["snt_attachments", "dxf_attachments"]:
            for attachment in getattr(self.app, list_name, []) or []:
                att_name = attachment.get("filename") or attachment.get("full_path")
                if snt_base and att_name and os.path.basename(str(att_name)) != snt_base:
                    continue

                rows = None
                item = attachment.get("_snt_item") if isinstance(attachment, dict) else None
                if item is not None:
                    try:
                        refresh = getattr(item, "_refresh_live_layer_stats", None)
                        if callable(refresh):
                            rows = list(refresh())
                        else:
                            rows = getattr(item, "layer_stats_cache", None)
                    except Exception:
                        rows = getattr(item, "layer_stats_cache", None)
                if rows is None:
                    rows = attachment.get("layer_stats", []) or []

                for row in rows:
                    try:
                        row_layer = str(row[0] or "").strip()
                        row_count = int(row[1])
                    except Exception:
                        continue
                    if row_layer == lname:
                        return row_count
        return None

    def _set_footer_text(self, layer_name: Optional[str], snt_name: Optional[str], entity_count: Optional[int]):
        try:
            if hasattr(self.app, "_set_snt_layer_pick_footer_text"):
                self.app._set_snt_layer_pick_footer_text(layer_name, snt_name, entity_count)
        except Exception:
            pass

    def _sync_footer_button_state(self, enabled: bool):
        try:
            if hasattr(self.app, "_sync_snt_layer_pick_footer_button"):
                self.app._sync_snt_layer_pick_footer_button(bool(enabled))
        except Exception:
            pass

    def _sync_cursor(self, enabled: bool):
        try:
            if hasattr(self.app, "set_cross_cursor_active"):
                self.app.set_cross_cursor_active(bool(enabled), "snt_layer_pick")
        except Exception:
            pass

    def _show_click_popup(
        self,
        layer_name: str,
        snt_name: Optional[str],
        entity_count: Optional[int],
    ):
        layer_text = str(layer_name or "").strip()
        if not layer_text:
            return

        try:
            self._ensure_click_popup_label()
            label = self._click_popup_label
            if label is None:
                return

            label.setText(layer_text)
            label.adjustSize()

            global_pos = QCursor.pos()
            host = self.app
            local = host.mapFromGlobal(global_pos)
            x = int(local.x()) + 14
            y = int(local.y()) + 18

            margin = 8
            host_w = int(host.width())
            host_h = int(host.height())
            lbl_w = int(label.width())
            lbl_h = int(label.height())

            x = max(margin, min(x, host_w - lbl_w - margin))
            y = max(margin, min(y, host_h - lbl_h - margin))

            label.move(x, y)
            label.raise_()
            label.show()

            self._click_popup_timer.stop()
            self._click_popup_timer.start(2000)
        except Exception:
            pass

    def _hide_click_popup(self):
        try:
            if self._click_popup_timer.isActive():
                self._click_popup_timer.stop()
            if self._click_popup_label is not None:
                self._click_popup_label.hide()
        except Exception:
            pass

    def _ensure_click_popup_label(self):
        if self._click_popup_label is not None:
            return

        try:
            label = QLabel(self.app)
            label.setObjectName("sntLayerPickPopup")
            label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            label.setStyleSheet(
                """
                QLabel#sntLayerPickPopup {
                    background: rgba(20, 20, 24, 230);
                    color: #F6F7FB;
                    border: 1px solid rgba(120, 200, 255, 180);
                    border-radius: 6px;
                    padding: 4px 8px;
                    font-size: 11px;
                    font-weight: 600;
                }
                """
            )
            label.hide()
            self._click_popup_label = label
        except Exception:
            self._click_popup_label = None
