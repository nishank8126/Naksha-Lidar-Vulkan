# ─────────────────────────────────────────────────────────────────────────────
# gis_layers.py — "Overlay Control Center" for imported GIS data
#
# A Global-Mapper-style, slide-in/out dock that manages ONLY the imported
# geospatial overlays:
#
#     • GeoTIFF (.tif / .tiff)  → raster renderer (layer 0)
#     • Shapefile (.shp)        → vector overlay renderer (layer 2)
#     • GeoJSON (.geojson)      → vector overlay renderer (layer 2)
#
# It does NOT touch DXF / DWG / SNT attachments — those keep their own systems.
#
# Per layer you get: show/hide, opacity, reorder (top↔bottom), zoom-to, remove.
#
# Ordering model (this is a 3D scene, not a 2D layer stack):
#   - Vectors (layer 2) ALWAYS render above rasters (layer 0) — the renderer-layer
#     architecture guarantees it, which is the sensible GIS default.
#   - Among RASTERS: order is applied with a tiny Z offset (higher = on top) since
#     layer-0 has depth testing on.
#   - Among VECTORS: overlay-renderer actors have depth-testing OFF, so order is
#     applied by re-adding actors in list order (last added draws on top).
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import colorsys
import os
import random
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QCheckBox, QToolButton
from PySide6.QtGui import QPixmap, QColor

# Registry attribute name on the app object.
_REGISTRY_ATTR = "gis_layers"
_DOCK_ATTR = "_gis_layers_dock"   # legacy — kept for any external refs
_PANEL_ATTR = "_gis_layers_panel"

# Z step (world units) used to stack rasters among themselves on layer 0.
_RASTER_Z_STEP = 1e-3


# ─────────────────────────────────────────────────────────────────────────────
# COLOR GENERATION — golden-angle hue rotation (same as QGIS)
# ─────────────────────────────────────────────────────────────────────────────
def _generate_unique_color(index: int) -> tuple[int, int, int]:
    """Return (r, g, b) for layer *index* using the golden angle."""
    golden_angle = 137.508
    hue = (index * golden_angle) % 360.0
    saturation = 0.72 + 0.13 * ((index % 3) - 1)
    lightness = 0.48 + 0.07 * ((index % 2) * 2 - 1)
    r, g, b = colorsys.hls_to_rgb(hue / 360.0, lightness, saturation)
    return (int(r * 255), int(g * 255), int(b * 255))


def _default_color_for(index: int, used_colors: set = None) -> QColor:
    """Pick a distinct QColor for a new layer, avoiding collisions and visual similarity."""
    if used_colors is None:
        used_colors = set()
    clean_used = {c.lower() for c in used_colors if c}
    idx = len(clean_used)
    
    for attempt in range(100):
        r, g, b = _generate_unique_color(idx)
        candidate = QColor(r, g, b)
        
        is_too_close = False
        for used in clean_used:
            try:
                uc = QColor(used)
                if uc.isValid():
                    dist = ((candidate.red() - uc.red())**2 + 
                            (candidate.green() - uc.green())**2 + 
                            (candidate.blue() - uc.blue())**2)**0.5
                    if dist < 45:  # Ensure distinct visual threshold
                        is_too_close = True
                        break
            except Exception:
                pass
        if not is_too_close:
            return candidate
        idx += 1
        
    r, g, b = _generate_unique_color(len(clean_used))
    return QColor(r, g, b)


def _colored_square_pixmap(color_hex: str, size: int = 16) -> QPixmap:
    """16×16 solid-color square pixmap — QGIS layer panel icon style."""
    from PySide6.QtGui import QPainter, QPen
    from PySide6.QtCore import QRectF
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, False)
    col = QColor(color_hex)
    p.setPen(QPen(col.darker(130), 1.0))
    p.setBrush(col)
    p.drawRect(QRectF(0.5, 0.5, size - 1.5, size - 1.5))
    p.end()
    return pm


# ─────────────────────────────────────────────────────────────────────────────
# LAYER ROW WIDGET — colored square + name + visibility checkbox
# (Copied from Ring app's LayerItemWidget, adapted to PySide6)
# ─────────────────────────────────────────────────────────────────────────────
class LayerItemWidget(QWidget):
    """A compact layer row: [■ colored square] [name] [👁 checkbox]."""
    toggled = Signal(bool)

    def __init__(self, name: str, pixmap: QPixmap, checked: bool = True, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("background: transparent; border: none;")
        self.setMinimumHeight(24)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 0, 4, 0)
        lay.setSpacing(6)
        lay.setAlignment(Qt.AlignVCenter)

        self.icon_lbl = QLabel()
        if not pixmap.isNull():
            self.icon_lbl.setPixmap(pixmap)
        self.icon_lbl.setFixedSize(14, 14)
        self.icon_lbl.setScaledContents(True)
        lay.addWidget(self.icon_lbl)

        self.name_lbl = QLabel(name)
        self.name_lbl.setStyleSheet("background: transparent;")
        lay.addWidget(self.name_lbl)
        lay.addStretch()

        self.cb = QCheckBox()
        self.cb.setFixedWidth(20)
        self.cb.setChecked(checked)
        self.cb.setCursor(Qt.PointingHandCursor)
        self.cb.toggled.connect(self.toggled.emit)
        lay.addWidget(self.cb)


# ─────────────────────────────────────────────────────────────────────────────
# REGISTRY
# ─────────────────────────────────────────────────────────────────────────────
def _registry(app) -> list:
    reg = getattr(app, _REGISTRY_ATTR, None)
    if not isinstance(reg, list):
        reg = []
        setattr(app, _REGISTRY_ATTR, reg)
    return reg


def _next_layer_id(app) -> int:
    nid = int(getattr(app, "_gis_layer_seq", 0)) + 1
    app._gis_layer_seq = nid
    return nid


def _main_renderer(app):
    """Layer-1 data renderer (point cloud and terrain meshes)."""
    try:
        return app.vtk_widget.renderer
    except Exception:
        try:
            return app.vtk_widget.GetRenderWindow().GetRenderers().GetFirstRenderer()
        except Exception:
            return None


def _raster_renderer(app):
    """Layer-0 renderer dedicated to GeoTIFF/basemap underlays."""
    try:
        from gui.scene_render_pipeline import ROLE_RASTER, renderer_for_role
        return renderer_for_role(app, ROLE_RASTER)
    except Exception:
        return None


def _overlay_renderer(app):
    """Layer-2 renderer (vector linework)."""
    dz = getattr(app, "digitizer", None)
    return getattr(dz, "overlay_renderer", None) if dz is not None else None


def _ensure_overlay_on_top(app):
    """
    Repair the central four-pass compositor. Raster, LiDAR, vector, and text
    ownership is enforced together so no subsystem can silently reset another.
    """
    try:
        from gui.scene_render_pipeline import ensure_scene_render_pipeline
        ensure_scene_render_pipeline(app)
    except Exception:
        pass


def _render(app):
    try:
        app.vtk_widget.render()
    except Exception:
        try:
            app.vtk_widget.GetRenderWindow().Render()
        except Exception:
            pass


def _sub_layer_actors(sub_info: dict) -> list:
    """Return every render prop that belongs to one subtype entry."""
    if not isinstance(sub_info, dict):
        return []
    actors = [a for a in (sub_info.get("actors") or []) if a is not None]
    if actors:
        return actors
    actor = sub_info.get("actor")
    return [actor] if actor is not None else []


def get_layer_epsg(path: str, layer_name: str = None) -> str | None:
    if not path:
        return None
    try:
        from osgeo import ogr, osr, gdal
        ext = os.path.splitext(path)[1].lower()
        if ext in (".tif", ".tiff"):
            ds = gdal.Open(path, gdal.GA_ReadOnly)
            if ds:
                proj = ds.GetProjection()
                if proj:
                    srs = osr.SpatialReference()
                    srs.ImportFromWkt(proj)
                    srs.AutoIdentifyEPSG()
                    code = srs.GetAuthorityCode(None)
                    if code:
                        return f"EPSG:{code}"
                ds = None
        else:
            ds = ogr.Open(path, 0)
            if ds:
                lyr = None
                if layer_name:
                    lyr = ds.GetLayerByName(layer_name)
                if lyr is None:
                    if layer_name:
                        for i in range(ds.GetLayerCount()):
                            cand = ds.GetLayerByIndex(i)
                            if cand and cand.GetName().lower() == layer_name.lower():
                                lyr = cand
                                break
                if lyr is None:
                    lyr = ds.GetLayer(0)
                if lyr:
                    srs = lyr.GetSpatialRef()
                    if srs:
                        srs.AutoIdentifyEPSG()
                        code = srs.GetAuthorityCode(None)
                        if code:
                            return f"EPSG:{code}"
                ds = None
    except Exception as e:
        print(f"Error extracting EPSG from {path} (layer {layer_name}): {e}")
    return None


def register_gis_layer(app, name: str, path: str, kind: str, fmt: str,
                       actors: list, opacity: float = None,
                       allow_empty: bool = False) -> dict:
    """
    Register an imported overlay so the panel can manage it.

    kind : "raster" | "vector"
    fmt  : "tif" | "shp" | "geojson"
    actors: the VTK actors that make up this layer (already added to a renderer).
    """
    actors = [a for a in (actors or []) if a is not None]
    if not actors and not allow_empty:
        return None

    if opacity is None:
        # Use the first actor's current opacity if it exposes one.
        opacity = 1.0
        if actors:
            try:
                opacity = float(actors[0].GetProperty().GetOpacity())
            except Exception:
                opacity = 1.0

    normalized_kind = kind if kind in ("raster", "vector", "table") else "vector"

    entry = {
        "id": _next_layer_id(app),
        "name": name or Path(str(path)).name or "layer",
        "path": str(path or ""),
        "kind": normalized_kind,
        "fmt": fmt,
        "actors": actors,
        "visible": True,
        "opacity": float(opacity),
        "epsg": get_layer_epsg(path),
    }
    # Sensible default stacking (top of list = drawn on top): vectors go to the
    # TOP (drawn over imagery), rasters default to the BOTTOM (background) — like
    # QGIS/Global Mapper. The user can still reorder freely afterwards.
    reg = _registry(app)
    if entry["kind"] == "raster":
        reg.append(entry)
    else:
        reg.insert(0, entry)
    _apply_order(app)

    dock = getattr(app, _PANEL_ATTR, None)
    if dock is not None and not getattr(dock, "_refresh_suspended", 0):
        try:
            dock.refresh()
        except Exception:
            import traceback
            traceback.print_exc()
    if hasattr(app, "_sync_activity_bar"):
        try:
            app._sync_activity_bar()
        except Exception:
            pass
    if hasattr(app, "update_epsg_display"):
        try:
            app.update_epsg_display()
        except Exception:
            pass
    return entry


def suspend_layer_panel_refresh(app):
    """Pause the Overlay Control Center's per-layer refresh() during a batch
    import. Without this, importing N layers rebuilds the whole QTreeWidget
    (with a fresh QIcon per row) N times - O(n^2) instead of O(n). Call
    resume_layer_panel_refresh() in a finally block to refresh once at the end.
    """
    dock = getattr(app, _PANEL_ATTR, None)
    if dock is not None:
        dock._refresh_suspended = getattr(dock, "_refresh_suspended", 0) + 1


def resume_layer_panel_refresh(app):
    dock = getattr(app, _PANEL_ATTR, None)
    if dock is None:
        return
    dock._refresh_suspended = max(0, getattr(dock, "_refresh_suspended", 0) - 1)
    if not dock._refresh_suspended:
        try:
            dock.refresh()
        except Exception:
            import traceback
            traceback.print_exc()


# ─────────────────────────────────────────────────────────────────────────────
# LAYER OPERATIONS
# ─────────────────────────────────────────────────────────────────────────────
def _set_layer_visible(app, entry: dict, visible: bool):
    entry["visible"] = bool(visible)
    for a in entry["actors"]:
        try:
            a.SetVisibility(1 if visible else 0)
        except Exception:
            pass
            
    # Toggle sub-layers actors visibility as well
    if "sub_layers" in entry:
        for sub_code, sub_info in entry["sub_layers"].items():
            for a in _sub_layer_actors(sub_info):
                try:
                    a.SetVisibility(1 if visible else 0)
                except Exception:
                    pass
                    
    _render(app)


def _set_layer_opacity(app, entry: dict, opacity: float):
    opacity = max(0.0, min(1.0, float(opacity)))
    entry["opacity"] = opacity
    for a in entry["actors"]:
        try:
            a.GetProperty().SetOpacity(opacity)
        except Exception:
            pass
    _render(app)


def _layer_bounds(entry: dict):
    xmin = ymin = zmin = float("inf")
    xmax = ymax = zmax = float("-inf")
    found = False
    for a in entry["actors"]:
        try:
            b = a.GetBounds()
        except Exception:
            b = None
        if not b or len(b) < 6:
            continue
        if b[0] > b[1]:  # invalid / empty
            continue
        found = True
        xmin, xmax = min(xmin, b[0]), max(xmax, b[1])
        ymin, ymax = min(ymin, b[2]), max(ymax, b[3])
        zmin, zmax = min(zmin, b[4]), max(zmax, b[5])
    if not found:
        return None
    return (xmin, xmax, ymin, ymax, zmin, zmax)


def _zoom_to_layer(app, entry: dict):
    bounds = _layer_bounds(entry)
    ren = _main_renderer(app)
    if bounds is None or ren is None:
        return
    try:
        ren.ResetCamera(bounds)
        ren.ResetCameraClippingRange()
    except Exception:
        pass
    _render(app)


def _merge_bounds(bounds_list):
    """Combine multiple VTK bounds tuples into one overall extent."""
    valid = [b for b in (bounds_list or []) if b and len(b) >= 6 and b[0] <= b[1] and b[2] <= b[3] and b[4] <= b[5]]
    if not valid:
        return None
    xmin = min(b[0] for b in valid)
    xmax = max(b[1] for b in valid)
    ymin = min(b[2] for b in valid)
    ymax = max(b[3] for b in valid)
    zmin = min(b[4] for b in valid)
    zmax = max(b[5] for b in valid)
    return (xmin, xmax, ymin, ymax, zmin, zmax)


def zoom_to_gis_entries(app, entries):
    """Frame one or more GIS layer registry entries in the main camera."""
    ren = _main_renderer(app)
    if ren is None:
        return
    bounds = _merge_bounds([_layer_bounds(entry) for entry in (entries or []) if entry])
    if bounds is None:
        return
    try:
        ren.ResetCamera(bounds)
        ren.ResetCameraClippingRange()
    except Exception:
        return
    _render(app)


def _remove_layer(app, entry: dict):
    """Detach actors from their renderer and drop the layer from all stores."""
    main_ren = _main_renderer(app)
    raster_ren = _raster_renderer(app)
    ovl_ren = _overlay_renderer(app)
    for a in entry["actors"]:
        for ren in (raster_ren, main_ren, ovl_ren):
            if ren is None:
                continue
            try:
                if ren.HasViewProp(a):
                    ren.RemoveActor(a)
            except Exception:
                try:
                    ren.RemoveActor(a)
                except Exception:
                    pass
        # Free heavy GPU resources held by raster textures.
        try:
            tex = a.GetTexture()
            if tex is not None:
                tex.SetInputData(None)
                a.SetTexture(None)
        except Exception:
            pass

    # Remove from the registry.
    reg = _registry(app)
    if entry in reg:
        reg.remove(entry)

    # Keep legacy stores consistent so other code (memory cleanup, etc.) agrees.
    if entry["kind"] == "raster":
        ga = getattr(app, "geotiff_actors", None)
        if isinstance(ga, list):
            for a in entry["actors"]:
                if a in ga:
                    ga.remove(a)
    else:
        dz = getattr(app, "digitizer", None)
        drawings = getattr(dz, "drawings", None) if dz is not None else None
        if isinstance(drawings, list):
            actor_set = set(id(a) for a in entry["actors"])
            kept = [d for d in drawings
                    if id(d.get("actor")) not in actor_set]
            try:
                drawings[:] = kept
            except Exception:
                pass

    _apply_order(app)
    _render(app)
    if hasattr(app, "_sync_activity_bar"):
        try:
            app._sync_activity_bar()
        except Exception:
            pass
    if hasattr(app, "update_epsg_display"):
        try:
            app.update_epsg_display()
        except Exception:
            pass


def _move_layer(app, entry: dict, delta: int):
    """Reorder within the list. delta = -1 moves up (toward top/on-top)."""
    reg = _registry(app)
    if entry not in reg:
        return
    i = reg.index(entry)
    j = i + delta
    if j < 0 or j >= len(reg):
        return
    reg[i], reg[j] = reg[j], reg[i]
    _apply_order(app)
    _render(app)


def _move_layer_to(app, entry: dict, to_top: bool):
    """Move a layer to the very top or bottom of the list."""
    reg = _registry(app)
    if entry not in reg:
        return
    reg.remove(entry)
    reg.insert(0 if to_top else len(reg), entry)
    _apply_order(app)
    _render(app)


def _reorder_registry(app, ordered_ids):
    """Reorder the registry to match a list of layer ids (top-to-bottom)."""
    reg = _registry(app)
    by_id = {e["id"]: e for e in reg}
    new = [by_id[i] for i in ordered_ids if i in by_id]
    # keep any not mentioned (safety) appended in their old order
    for e in reg:
        if e not in new:
            new.append(e)
    reg[:] = new
    _apply_order(app)
    _render(app)


def _apply_order(app):
    """
    QGIS / Global-Mapper ordering: the TOP row of the panel draws on top — for
    ANY layer, raster OR vector.
    """
    reg = _registry(app)
    from gui.scene_render_pipeline import (
        ROLE_DATA, ROLE_OVERLAY, ROLE_RASTER, ROLE_TEXT,
        ensure_scene_render_pipeline,
    )
    pipeline = ensure_scene_render_pipeline(app)
    raster_renderer = pipeline.get(ROLE_RASTER)
    main = pipeline.get(ROLE_DATA)
    ovl = pipeline.get(ROLE_OVERLAY)
    text = pipeline.get(ROLE_TEXT)

    rasters = [e for e in reg if e.get("kind") == "raster"]
    vectors = [e for e in reg if e.get("kind") == "vector"]

    # 1. Stack rasters only inside the dedicated layer-0 renderer.
    for rank, e in enumerate(reversed(rasters)):
        actors = list(e.get("actors", []))
        if "sub_layers" in e:
            for sub_info in e["sub_layers"].values():
                actors.extend(_sub_layer_actors(sub_info))
        for a in actors:
            try:
                for other in (main, ovl, text):
                    if other is not None and other.HasViewProp(a):
                        other.RemoveViewProp(a)
                if raster_renderer is not None and not raster_renderer.HasViewProp(a):
                    raster_renderer.AddActor(a)
                a._naksha_scene_role = ROLE_RASTER
                try:
                    a.GetProperty().SetDepthTestingEnabled(True)
                except Exception:
                    pass
                p = a.GetPosition()
                a.SetPosition(p[0], p[1], rank * _RASTER_Z_STEP)
                a.SetVisibility(1 if e.get("visible", True) else 0)
            except Exception:
                pass

    # 2. Stack vectors on the overlay renderer (or main if ovl is None)
    target_ren = ovl if ovl is not None else main
    for e in reversed(vectors):
        actors = list(e.get("actors", []))
        if "sub_layers" in e:
            for sub_info in e["sub_layers"].values():
                actors.extend(_sub_layer_actors(sub_info))
        for a in actors:
            try:
                for other_ren in (raster_renderer, main, text):
                    if other_ren is not None and other_ren is not target_ren and other_ren.HasViewProp(a):
                        other_ren.RemoveViewProp(a)
                if target_ren is not None and not target_ren.HasViewProp(a):
                    target_ren.AddActor(a)
                a._naksha_scene_role = ROLE_OVERLAY if target_ren is ovl else ROLE_DATA
                
                # Depth testing: disabled on overlay, enabled on main
                if target_ren is ovl:
                    try:
                        a.GetProperty().SetDepthTestingEnabled(False)
                    except Exception:
                        pass
                else:
                    try:
                        a.GetProperty().SetDepthTestingEnabled(True)
                    except Exception:
                        pass
                a.SetVisibility(1 if e.get("visible", True) else 0)
            except Exception:
                pass

    if ovl is not None:
        _ensure_overlay_on_top(app)


# ─────────────────────────────────────────────────────────────────────────────
# THE DOCK
# ─────────────────────────────────────────────────────────────────────────────
ROW_HEIGHT = 40  # fixed list-row height (prevents label overlap)


def _occ_colors():
    """Pull a theme-aware color set (works in both light and dark themes)."""
    try:
        from gui.theme_manager import ThemeColors as C

        def g(key, fallback):
            try:
                val = C.get(key)
                return val or fallback
            except Exception:
                return fallback

        light = False
        try:
            light = bool(C.is_light())
        except Exception:
            light = False

        return {
            "panel":   g("bg_secondary", "#1b1d23"),
            "surface": g("bg_primary",   "#16181d"),
            "card_hover": g("bg_button", "#21242c"),
            "btn":     g("bg_button",       "#262a33"),
            "btn_hover": g("bg_button_hover", "#313641"),
            "text":    g("text_primary",   "#e8eaed"),
            "muted":   g("text_muted",     "#7f8794"),
            "secondary": g("text_secondary", "#aeb4bf"),
            "border":  g("border_light",   "#2a2d35"),
            "accent":  g("accent",         "#3b5bdb"),
            "accent_hover": g("accent_hover", "#4c6ef5"),
            "danger":  g("danger",         "#d32f2f"),
            "danger_hover": g("danger_hover", "#f44336"),
            "on_accent": "#ffffff",
            "raster":  "#2f7fd6" if not light else "#1971c2",
            "vector":  "#37b24d" if not light else "#2f9e44",
            # Soft selection tint that keeps the default text readable in both themes.
            "sel":     "#d6e4ff" if light else "#2b3a5e",
            "is_light": light,
        }
    except Exception:
        # Hard fallback (dark) if the theme module is unavailable.
        return {
            "panel": "#1b1d23", "surface": "#16181d", "card_hover": "#21242c",
            "btn": "#262a33", "btn_hover": "#313641", "text": "#e8eaed",
            "muted": "#7f8794", "secondary": "#aeb4bf", "border": "#2a2d35",
            "accent": "#3b5bdb", "accent_hover": "#4c6ef5", "danger": "#d32f2f",
            "danger_hover": "#f44336", "on_accent": "#ffffff", "raster": "#2f7fd6",
            "vector": "#37b24d", "sel": "#2b3a5e", "is_light": False,
        }


# Preferred UI font (professional, falls back gracefully across platforms).
_UI_FONT = '"Segoe UI", "Inter", "Roboto", Arial, sans-serif'


def _glyph(name: str, color_hex: str, size: int = 16):
    """
    Draw a crisp, theme-colored icon in the Lucide line style and return a
    QPixmap (hi-DPI aware): uniform stroke, rounded caps/joins, stroke-only.
    """
    from PySide6.QtGui import QPixmap, QPainter, QColor, QPen, QPainterPath
    from PySide6.QtCore import Qt, QPointF, QRectF

    dpr = 2  # render at 2x for sharp edges, then display-scale down
    pm = QPixmap(size * dpr, size * dpr)
    pm.fill(Qt.transparent)
    pm.setDevicePixelRatio(dpr)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.scale(dpr, dpr)  # paint in logical coordinates
    col = QColor(color_hex)
    s = size

    # Lucide: 2px stroke on a 24px grid → ~0.083 ratio, rounded caps + joins.
    sw = max(1.3, s * 0.083)
    pen = QPen(col, sw)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)

    def line(x1, y1, x2, y2):
        p.drawLine(QPointF(s * x1, s * y1), QPointF(s * x2, s * y2))

    def poly(pts):
        path = QPainterPath()
        path.moveTo(s * pts[0][0], s * pts[0][1])
        for x, y in pts[1:]:
            path.lineTo(s * x, s * y)
        p.drawPath(path)

    if name == "raster":
        # Lucide "image": rounded frame + sun circle + mountain polyline (stroke).
        p.drawRoundedRect(QRectF(s * 0.16, s * 0.16, s * 0.68, s * 0.68),
                          s * 0.14, s * 0.14)
        p.drawEllipse(QPointF(s * 0.37, s * 0.37), s * 0.06, s * 0.06)  # sun
        poly([(0.20, 0.78), (0.42, 0.54), (0.56, 0.66), (0.80, 0.40)])  # mountains

    elif name == "vector":
        # Lucide "spline"/waypoints: a path with hollow square vertex nodes.
        poly([(0.20, 0.74), (0.44, 0.42), (0.60, 0.58), (0.82, 0.26)])
        r = s * 0.075
        for x, y in ((0.20, 0.74), (0.60, 0.58), (0.82, 0.26)):
            p.drawRect(QRectF(s * x - r, s * y - r, 2 * r, 2 * r))

    elif name in ("eye", "eye_off"):
        # Lucide "eye": almond outline + pupil circle (stroke).
        path = QPainterPath()
        path.moveTo(s * 0.10, s * 0.50)
        path.quadTo(s * 0.50, s * 0.16, s * 0.90, s * 0.50)
        path.quadTo(s * 0.50, s * 0.84, s * 0.10, s * 0.50)
        p.drawPath(path)
        p.drawEllipse(QPointF(s * 0.50, s * 0.50), s * 0.12, s * 0.12)
        if name == "eye_off":
            line(0.14, 0.14, 0.86, 0.86)

    elif name == "zoom":
        # Lucide "search": circle + handle.
        p.drawEllipse(QRectF(s * 0.16, s * 0.16, s * 0.44, s * 0.44))
        line(0.60, 0.60, 0.84, 0.84)

    elif name == "up":          # Lucide "chevron-up"
        poly([(0.26, 0.60), (0.50, 0.38), (0.74, 0.60)])
    elif name == "down":        # Lucide "chevron-down"
        poly([(0.26, 0.42), (0.50, 0.64), (0.74, 0.42)])

    elif name == "trash":
        # Lucide "trash-2": lid + handle + can body + two inner lines.
        line(0.18, 0.30, 0.82, 0.30)                 # lid
        poly([(0.40, 0.30), (0.42, 0.20), (0.58, 0.20), (0.60, 0.30)])  # handle
        poly([(0.26, 0.30), (0.30, 0.82), (0.70, 0.82), (0.74, 0.30)])  # body
        line(0.43, 0.42, 0.45, 0.70)                 # inner line 1
        line(0.57, 0.42, 0.55, 0.70)                 # inner line 2

    elif name == "plus":
        line(0.50, 0.22, 0.50, 0.78)
        line(0.22, 0.50, 0.78, 0.50)

    elif name == "sliders":     # Lucide "sliders-horizontal" (raster properties)
        line(0.18, 0.32, 0.82, 0.32); line(0.18, 0.68, 0.82, 0.68)
        p.drawEllipse(QPointF(s * 0.62, s * 0.32), s * 0.07, s * 0.07)
        p.drawEllipse(QPointF(s * 0.38, s * 0.68), s * 0.07, s * 0.07)

    elif name == "table":
        p.drawRoundedRect(QRectF(s * 0.16, s * 0.16, s * 0.68, s * 0.68), s * 0.08, s * 0.08)
        line(0.40, 0.16, 0.40, 0.84)
        line(0.68, 0.16, 0.68, 0.84)
        line(0.16, 0.40, 0.84, 0.40)
        line(0.16, 0.64, 0.84, 0.64)

    p.end()
    return pm


def _icon(name: str, color_hex: str, size: int = 16):
    from PySide6.QtGui import QIcon
    return QIcon(_glyph(name, color_hex, size))


def _symbol_pixmap(geom: str, color_hex: str, size: int = 14):
    """Premium QGIS-style legend symbol reflecting geometry + colour."""
    from PySide6.QtGui import QPixmap, QPainter, QColor, QPen, QBrush
    from PySide6.QtCore import Qt, QPointF, QRectF

    # Render at the exact icon size (no 2x supersample). Downscaling a 2x image
    # washes out thin features like the line symbol → render 1:1 so it stays bold.
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    s = size
    col = QColor(color_hex)
    outline = QColor(col).darker(150)

    if geom == "point":
        # Solid disc with a crisp ring — a clear marker.
        p.setPen(QPen(outline, 1.2))
        p.setBrush(QBrush(col))
        p.drawEllipse(QPointF(s * 0.5, s * 0.5), s * 0.32, s * 0.32)

    elif geom == "multipoint":
        # Draw three small dots offset from each other
        p.setPen(QPen(outline, 0.8))
        p.setBrush(QBrush(col))
        r_size = s * 0.16
        p.drawEllipse(QPointF(s * 0.35, s * 0.35), r_size, r_size)
        p.drawEllipse(QPointF(s * 0.68, s * 0.45), r_size, r_size)
        p.drawEllipse(QPointF(s * 0.48, s * 0.68), r_size, r_size)

    elif geom == "line":
        # BOLD diagonal line with square vertex nodes (clearly visible at 16px).
        pen = QPen(col, max(3.2, s * 0.21))
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        a, b = QPointF(s * 0.16, s * 0.80), QPointF(s * 0.84, s * 0.20)
        p.drawLine(a, b)
        # vertex nodes
        p.setPen(QPen(outline, 1.0))
        p.setBrush(QBrush(col))
        ns = s * 0.13
        for pt in (a, b):
            p.drawRect(QRectF(pt.x() - ns, pt.y() - ns, 2 * ns, 2 * ns))

    elif geom == "multiline":
        # Draw two thin diagonal lines
        pen = QPen(col, max(2.0, s * 0.14))
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.drawLine(QPointF(s * 0.15, s * 0.60), QPointF(s * 0.65, s * 0.10))
        p.drawLine(QPointF(s * 0.35, s * 0.85), QPointF(s * 0.85, s * 0.35))

    elif geom in ("raster", "multiraster"):
        # Distinct raster icon: a 3×3 grid
        p.setPen(Qt.NoPen)
        m = s * 0.12
        cell = (s - 2 * m) / 3.0
        shades = [1.0, 0.78, 0.62]
        for r in range(3):
            for cc in range(3):
                base = QColor(col)
                f = shades[(r + cc) % 3]
                base.setRed(int(base.red() * f)); base.setGreen(int(base.green() * f))
                base.setBlue(int(base.blue() * f))
                p.setBrush(QBrush(base))
                p.drawRect(QRectF(m + cc * cell, m + r * cell, cell + 0.5, cell + 0.5))
        p.setPen(QPen(outline, 1.0)); p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(m, m, 3 * cell, 3 * cell))

    elif geom == "multipolygon":
        # Draw two overlapping rectangles/polygons
        p.setPen(QPen(outline, 1.0))
        fill = QColor(col); fill.setAlpha(150)
        p.setBrush(QBrush(fill))
        p.drawRect(QRectF(s * 0.14, s * 0.34, s * 0.46, s * 0.40))
        p.drawRect(QRectF(s * 0.40, s * 0.18, s * 0.46, s * 0.40))

    else:  # polygon
        p.setPen(QPen(outline, 1.4))
        fill = QColor(col); fill.setAlpha(165)
        p.setBrush(QBrush(fill))
        p.drawRect(QRectF(s * 0.20, s * 0.24, s * 0.60, s * 0.52))
    p.end()
    return pm


def _colored_square_pixmap(color_hex: str, size: int = 16):
    """16×16 solid-color square pixmap — the QGIS layer panel icon style."""
    from PySide6.QtGui import QPixmap, QPainter, QColor, QPen
    from PySide6.QtCore import Qt, QRectF
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, False)
    col = QColor(color_hex)
    p.setPen(QPen(col.darker(130), 1.0))
    p.setBrush(col)
    p.drawRect(QRectF(0.5, 0.5, size - 1.5, size - 1.5))
    p.end()
    return pm


def _swatch_pixmap(color_hex: str, size: int = 12):
    """
    Flat solid colour swatch for raster band R/G/B chips — exactly like QGIS:
    a crisp, square-edged filled rectangle with a thin neutral border.
    """
    from PySide6.QtGui import QPixmap, QPainter, QColor, QPen, QBrush
    from PySide6.QtCore import Qt, QRectF

    dpr = 2
    pm = QPixmap(size * dpr, size * dpr)
    pm.fill(Qt.transparent)
    pm.setDevicePixelRatio(dpr)
    p = QPainter(pm)
    # No antialiasing → crisp square edges, like QGIS legend swatches.
    p.scale(dpr, dpr)
    col = QColor(color_hex)
    # Thin neutral grey border (consistent regardless of fill colour).
    p.setPen(QPen(QColor(90, 90, 90), 1.0))
    p.setBrush(QBrush(col))
    p.drawRect(QRectF(0.5, 0.5, size - 1.5, size - 1.5))
    p.end()
    return pm


_CHECK_CACHE = {}


def _checkbox_image_paths(c: dict) -> dict:
    """
    Bake the FULL checkbox into two PNGs (box + check together) so the tick is
    guaranteed to show. Classic look: a simple crisp border and fill; checked
    adds a clean dark checkmark on a white background in light mode.
    """
    key = (c["accent"], c["surface"], c["is_light"])
    if key in _CHECK_CACHE:
        return _CHECK_CACHE[key]
    from PySide6.QtGui import QPixmap, QPainter, QColor, QPen, QPainterPath
    from PySide6.QtCore import Qt, QRectF

    sz = 16

    def make(checked: bool) -> str:
        pm = QPixmap(sz, sz)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, False)
        
        if c.get("is_light"):
            border_color = QColor(0, 0, 0)
            bg_color = QColor(255, 255, 255)
            check_color = QColor(0, 0, 0)
        else:
            border_color = QColor(160, 160, 160)
            bg_color = QColor(c["surface"])
            check_color = QColor(255, 255, 255)
            
        p.setPen(QPen(border_color, 1.0))
        p.setBrush(bg_color)
        p.drawRect(QRectF(1.0, 1.0, 14, 14))
        
        if checked:
            p.setRenderHint(QPainter.Antialiasing, True)
            pen = QPen(check_color, 1.8)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            tick = QPainterPath()
            tick.moveTo(3.6, 8.2)
            tick.lineTo(6.6, 11.2)
            tick.lineTo(12.2, 4.3)
            p.drawPath(tick)
        p.end()
        import hashlib
        key_str = str(key)
        h = hashlib.md5(key_str.encode("utf-8")).hexdigest()[:8]
        fname = os.path.join(
            tempfile.gettempdir(),
            f"occ_chk_{'on' if checked else 'off'}_{h}.png")
        pm.save(fname, "PNG")
        return fname.replace("\\", "/")

    paths = {"on": make(True), "off": make(False)}
    _CHECK_CACHE[key] = paths
    return paths


_DOCKBTN_CACHE = {}


def _dock_button_image_paths(c: dict) -> dict:
    """
    Crisp, theme-tinted icons for the docked title-bar buttons (the default
    QDockWidget float/close glyphs are tiny and faint). Written as temp PNGs
    referenced from the stylesheet.
    """
    key = (c["text"], c["is_light"])
    if key in _DOCKBTN_CACHE:
        return _DOCKBTN_CACHE[key]
    from PySide6.QtGui import QPixmap, QPainter, QColor, QPen
    from PySide6.QtCore import Qt, QPointF, QRectF

    sz, dpr = 18, 2

    import hashlib
    key_str = str(key)
    h = hashlib.md5(key_str.encode("utf-8")).hexdigest()[:8]
    def _save(pm, kind):
        fname = os.path.join(tempfile.gettempdir(),
                             f"occ_db_{kind}_{h}.png")
        pm.save(fname, "PNG")
        return fname.replace("\\", "/")

    def _canvas():
        pm = QPixmap(sz * dpr, sz * dpr)
        pm.fill(Qt.transparent)
        pm.setDevicePixelRatio(dpr)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.scale(dpr, dpr)
        pen = QPen(QColor(c["text"]), 2.3)        # bolder so it reads clearly
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        return pm, p

    # close (×) — large, bold
    pm, p = _canvas()
    p.drawLine(QPointF(4.5, 4.5), QPointF(13.5, 13.5))
    p.drawLine(QPointF(13.5, 4.5), QPointF(4.5, 13.5))
    p.end()
    close_path = _save(pm, "close")

    # float / undock (two overlapping squares — the standard restore glyph)
    pm, p = _canvas()
    p.drawRect(QRectF(7, 3.5, 7.5, 7.5))
    p.setBrush(QColor(c["surface"]))
    p.drawRect(QRectF(3.5, 7, 7.5, 7.5))
    p.end()
    float_path = _save(pm, "float")

    paths = {"close": close_path, "float": float_path}
    _DOCKBTN_CACHE[key] = paths
    return paths


def _build_occ_style() -> str:
    """Build the panel stylesheet from the current theme palette (light or dark)."""
    c = _occ_colors()
    try:
        chk = _checkbox_image_paths(c)
    except Exception:
        chk = {"on": "", "off": ""}
    try:
        dbtn = _dock_button_image_paths(c)
    except Exception:
        dbtn = {"close": "", "float": ""}
    sel_text = c["on_accent"]
    # NOTE: font sizes are in *pt* (not px). A px font-size makes QFont.pointSize()
    # return -1, which floods the console with "QFont::setPointSize: Point size <= 0"
    # when the app's theme event-filter touches the widgets.
    return f"""
QDockWidget#GisLayersDock {{ color:{c['text']}; font-family:{_UI_FONT}; font-size:10pt; }}
QDockWidget#GisLayersDock::title {{
    background:{c['surface']}; color:{c['text']}; padding:9px 12px;
    text-align:left; border-bottom:1px solid {c['border']};
}}
QDockWidget#GisLayersDock::close-button {{
    background:transparent; border:none; border-radius:5px; padding:2px;
    width:26px; height:26px; icon-size:18px; image:url("{dbtn['close']}");
    subcontrol-position: top right; right:6px; top:5px;
}}
QDockWidget#GisLayersDock::float-button {{
    background:transparent; border:none; border-radius:5px; padding:2px;
    width:26px; height:26px; icon-size:18px; image:url("{dbtn['float']}");
    subcontrol-position: top right; right:38px; top:5px;
}}
QDockWidget#GisLayersDock::float-button:hover {{ background:{c['card_hover']}; }}
QDockWidget#GisLayersDock::close-button:hover {{ background:{c['danger']}; }}
#GisOCC {{ background:{c['panel']}; font-family:{_UI_FONT}; font-size:9pt; }}
#GisOCC QLabel {{ background:transparent; font-family:{_UI_FONT}; }}
#GisOCC QLabel#occTitle {{ color:{c['text']}; font-size:10pt; font-weight:600; }}
#GisOCC QLabel#occCount {{
    color:{c['secondary']}; background:{c['btn']}; border-radius:9px;
    padding:1px 9px; font-size:8pt; font-weight:600;
}}
#GisOCC QPushButton#occImport {{
    background:{c['accent']}; color:{c['on_accent']}; border:none; border-radius:6px;
    padding:7px 14px; font-size:9pt; font-weight:600;
}}
#GisOCC QPushButton#occImport:hover {{ background:{c['accent_hover']}; }}
#GisOCC QListWidget {{
    background:{c['surface']}; border:1px solid {c['border']}; border-radius:8px;
    padding:1px; outline:0;
    border: none;
}}
#GisOCC QListWidget::item {{ border-radius:4px; margin:0px 1px; padding: 0px; }}
#GisOCC QListWidget::item:hover {{ background:{c['card_hover']}; }}
#GisOCC QListWidget::item:selected {{ background:{c['sel']}; }}
#GisOCC QListWidget:focus {{
    outline: none;
    border: none;
}}
#GisOCC QTreeWidget#occTree {{
    background:{c['surface']}; border:1px solid {c['border']}; border-radius:8px;
    padding:3px; outline:0;
}}
#GisOCC QTreeWidget#occTree::item {{
    color:{c['text']}; padding:1px 0px; border-radius:0px; min-height:16px;
}}
#GisOCC QTreeWidget#occTree::item:hover {{ background:{c['card_hover']}; }}
#GisOCC QTreeWidget#occTree::item:selected {{ background:{c['sel']}; color:{c['text']}; }}
#GisOCC QScrollBar:vertical {{
    background: transparent;
    width: 6px;
    margin: 0px;
}}
#GisOCC QScrollBar::handle:vertical {{
    background: {c['muted']};
    min-height: 20px;
    border-radius: 3px;
}}
#GisOCC QScrollBar::handle:vertical:hover {{
    background: {c['accent']};
}}
#GisOCC QScrollBar::add-line:vertical, #GisOCC QScrollBar::sub-line:vertical {{
    border: none;
    background: none;
    height: 0px;
}}
#GisOCC QScrollBar::add-page:vertical, #GisOCC QScrollBar::sub-page:vertical {{
    background: none;
}}
#GisOCC QScrollBar:horizontal {{
    background: transparent;
    height: 6px;
    margin: 0px;
}}
#GisOCC QScrollBar::handle:horizontal {{
    background: {c['muted']};
    min-width: 20px;
    border-radius: 3px;
}}
#GisOCC QScrollBar::handle:horizontal:hover {{
    background: {c['accent']};
}}
#GisOCC QScrollBar::add-line:horizontal, #GisOCC QScrollBar::sub-line:horizontal {{
    border: none;
    background: none;
    width: 0px;
}}
#GisOCC QScrollBar::add-page:horizontal, #GisOCC QScrollBar::sub-page:horizontal {{
    background: none;
}}
#GisOCC QToolButton#occTool {{
    background:transparent; border:none; border-radius:4px; padding:4px;
}}
#GisOCC QToolButton#occTool:hover {{ background:{c['card_hover']}; }}
#GisOCC QToolButton#occTool:checked {{ background:{c['sel']}; }}
#GisOCC QLineEdit#occFilter {{
    background:{c['surface']}; color:{c['text']}; border:1px solid {c['border']};
    border-radius:6px; padding:4px 8px; font-size:9pt;
}}
#GisOCC QLineEdit#occFilter:focus {{ border:1px solid {c['accent']}; }}
/* QGIS-style visibility checkbox: a light box; click to add the tick, click to
   remove it. Box+check are baked into one image each so the tick always shows. */
#GisOCC QTreeWidget#occTree::indicator {{ width:16px; height:16px; }}
#GisOCC QTreeWidget#occTree::indicator:unchecked {{ image:url("{chk['off']}"); }}
#GisOCC QTreeWidget#occTree::indicator:checked {{ image:url("{chk['on']}"); }}
#GisOCC QLabel#occName {{ color:{c['text']}; font-size:9.5pt; font-weight:normal; }}
#GisOCC QToolButton#occEye {{ border:none; background:transparent; padding:2px; }}
#GisOCC QToolButton#occEye:hover {{ background:{c['card_hover']}; border-radius:5px; }}
#GisOCC QWidget#occDetail {{
    background:{c['surface']}; border:1px solid {c['border']}; border-radius:8px;
}}
#GisOCC QLabel#occDetailTitle {{
    color:{c['muted']}; font-size:8pt; font-weight:700; letter-spacing:0.6px;
}}
#GisOCC QLabel#occCaption {{ color:{c['secondary']}; font-size:9pt; font-weight:500; }}
#GisOCC QToolButton#occAct, #GisOCC QToolButton#occDanger {{
    background:{c['btn']}; color:{c['text']}; border:none; border-radius:6px;
    padding:6px 9px; font-size:9pt; font-weight:500;
}}
#GisOCC QToolButton#occAct:hover {{ background:{c['btn_hover']}; }}
#GisOCC QToolButton#occDanger:hover {{ background:{c['danger']}; color:{c['on_accent']}; }}
#GisOCC QToolButton#occAct:disabled, #GisOCC QToolButton#occDanger:disabled {{
    color:{c['muted']}; background:{c['surface']};
}}
#GisOCC QLabel#occEmptyTitle {{ color:{c['secondary']}; font-size:11pt; font-weight:600; }}
#GisOCC QLabel#occEmptyHint  {{ color:{c['muted']}; font-size:8pt; }}
#GisOCC QLabel#occHint {{ color:{c['muted']}; font-size:8pt; }}
#GisOCC QSlider::groove:horizontal {{ height:4px; background:{c['btn']}; border-radius:2px; }}
#GisOCC QSlider::sub-page:horizontal {{ background:{c['accent']}; border-radius:2px; }}
#GisOCC QSlider::handle:horizontal {{
    background:{c['accent']}; width:14px; height:14px; margin:-6px 0; border-radius:7px;
}}
"""


class GisLayersDock:
    """Factory for the Overlay Control Center panel (QTreeWidget + QGIS style tree hierarchy)."""

    @staticmethod
    def create(app):
        from PySide6.QtWidgets import (
            QWidget, QVBoxLayout, QHBoxLayout, QStackedWidget,
            QTreeWidget, QTreeWidgetItem, QPushButton, QSlider, QLabel,
            QToolButton, QSizePolicy, QLineEdit, QAbstractItemView, QHeaderView,
        )
        from PySide6.QtCore import Qt, QSize
        from PySide6.QtGui import QColor, QIcon
        from gui.gis import icons as gicons

        class DeselectableTreeWidget(QTreeWidget):
            def mousePressEvent(self, event):
                pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
                item = self.itemAt(pos)
                
                # Intercept right-clicks to preserve multi-selection
                if event.button() == Qt.RightButton:
                    if item is not None:
                        if item.isSelected():
                            # Right-clicked on an already selected item: keep multi-selection intact!
                            event.accept()
                            return
                        else:
                            # Right-clicked on an unselected item: select only it
                            self.clearSelection()
                            item.setSelected(True)
                            self.setCurrentItem(item)
                            event.accept()
                            return
                    else:
                        # Right-clicked on empty space: clear selection
                        self.clearSelection()
                        self.setCurrentItem(None)
                        event.accept()
                        return
                
                # Left-click deselect/reselect logic
                if item is None:
                    self.clearSelection()
                    self.setCurrentItem(None)
                    super().mousePressEvent(event)
                    return
                
                # If clicked on name/icon area (x > 30) of an already selected item, deselect it
                if item.isSelected() and pos.x() > 30:
                    self.clearSelection()
                    self.setCurrentItem(None)
                    event.accept()
                    return
                    
                super().mousePressEvent(event)

            def dropEvent(self, event):
                dragged_items = self.selectedItems()
                for item in dragged_items:
                    if item.parent() is not None:
                        event.ignore()
                        return
                super().dropEvent(event)

        cols = _occ_colors()

        panel = QWidget()
        panel.setObjectName("GisOCC")
        panel.setMinimumWidth(240)
        panel.setStyleSheet(_build_occ_style())
        v = QVBoxLayout(panel)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(4)

        # ── header ──────────────────────────────────────────────────────────
        header = QHBoxLayout()
        header.setSpacing(6)
        title = QLabel("Layers")
        title.setObjectName("occTitle")
        header.addWidget(title)
        header.addStretch()
        v.addLayout(header)

        # ── toolbar ─────────────────────────────────────────────────────────
        def _tool(name, tip, size=16):
            b = QToolButton()
            b.setObjectName("occTool")
            b.setIcon(gicons.icon(name, cols["text"], size))
            b.setIconSize(QSize(size, size))
            b.setFixedSize(QSize(26, 24))
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            return b

        toolbar = QHBoxLayout()
        toolbar.setSpacing(2)
        tb_import = _tool("plus", "Create new layer…", 16)
        tb_eye = _tool("eye", "Show / hide all layers")
        tb_filter = _tool("filter", "Filter layers by name")
        tb_filter.setCheckable(True)
        tb_table = _tool("table", "View Selected Layer Attribute Table", 16)
        tb_table.setEnabled(False)
        tb_domains = _tool("database", "View GDB Workspace Domains", 16)
        tb_domains.setEnabled(False)
        tb_remove = _tool("delete-layer", "Remove selected layer")
        for b in (tb_import, tb_eye, tb_filter, tb_table, tb_domains):
            toolbar.addWidget(b)
        toolbar.addStretch()
        toolbar.addWidget(tb_remove)
        v.addLayout(toolbar)

        # ── filter ──────────────────────────────────────────────────────────
        filter_box = QLineEdit()
        filter_box.setObjectName("occFilter")
        filter_box.setPlaceholderText("Filter layers…")
        filter_box.setClearButtonEnabled(True)
        filter_box.addAction(gicons.icon("search", cols["muted"], 14),
                             QLineEdit.LeadingPosition)
        filter_box.hide()
        v.addWidget(filter_box)

        # ── stack: [0] list, [1] empty ──────────────────────────────────────
        stack = QStackedWidget()

        layer_list = DeselectableTreeWidget()
        layer_list.setObjectName("occTree")
        layer_list.setHeaderHidden(True)
        layer_list.setColumnCount(2)
        layer_list.header().setStretchLastSection(False)
        layer_list.header().setSectionResizeMode(0, QHeaderView.Stretch)
        layer_list.header().setSectionResizeMode(1, QHeaderView.Fixed)
        layer_list.header().resizeSection(1, 60)
        layer_list.setIndentation(14)
        layer_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        layer_list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        layer_list.setContextMenuPolicy(Qt.CustomContextMenu)
        layer_list.setDragDropMode(QAbstractItemView.InternalMove)
        layer_list.setDropIndicatorShown(True)
        stack.addWidget(layer_list)

        empty = QWidget()
        ev = QVBoxLayout(empty)
        ev.addStretch()
        ic = QLabel()
        ic.setAlignment(Qt.AlignCenter)
        ic.setPixmap(_glyph("vector", cols["muted"], 44))
        et = QLabel("No overlays yet")
        et.setObjectName("occEmptyTitle")
        et.setAlignment(Qt.AlignCenter)
        eh = QLabel("Drag & drop a .shp, .tif, .geojson\nor right-click to Import")
        eh.setObjectName("occEmptyHint")
        eh.setAlignment(Qt.AlignCenter)
        ev.addWidget(ic)
        ev.addWidget(et)
        ev.addWidget(eh)
        ev.addStretch()
        stack.addWidget(empty)
        v.addWidget(stack, 1)

        hint = QLabel("Top of the list draws on top")
        hint.setObjectName("occHint")
        hint.setWordWrap(True)
        v.addWidget(hint)

        # ── palette for auto-coloring layers ────────────────────────────────
        _PALETTE = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
                    "#008080", "#9a6324", "#46c2cb", "#bcbd22", "#e377c2"]
        _used_colors: set = set()

        def _layer_color(entry) -> QColor:
            if entry.get("kind") == "raster":
                # Rasters (GeoTIFF/orthophoto) render their own pixel colors via a
                # texture, not a flat fill - never assign/mutate a per-layer color
                # here. The block below auto-tints any actor whose property color
                # is still default white, which is exactly the texture actor's
                # untouched default, so without this guard the orthophoto image
                # itself gets tinted by the "unique per-layer color" logic meant
                # for vector features.
                return QColor(_occ_colors()["raster"])
            color = entry.get("color") or ""
            try:
                qc = QColor(color)
                if qc.isValid() and qc.lightnessF() < 0.85 and qc.lightnessF() > 0.12:
                    return qc
            except Exception:
                pass
            c = _default_color_for(int(entry.get("id", 0)), _used_colors)
            _used_colors.add(c.name())
            entry["color"] = c.name()
            # Also apply color to actors if they exist and are default white/yellow
            if "actors" in entry:
                vtk_color = (c.redF(), c.greenF(), c.blueF())
                for a in entry["actors"]:
                    try:
                        curr_col = a.GetProperty().GetColor()
                        is_default = (curr_col[0] > 0.99 and curr_col[1] > 0.99 and curr_col[2] > 0.99) or \
                                     (curr_col[0] > 0.99 and curr_col[1] > 0.99 and curr_col[2] < 0.01)
                        if is_default:
                            a.GetProperty().SetColor(*vtk_color)
                            if hasattr(a.GetProperty(), "GetEdgeVisibility") and a.GetProperty().GetEdgeVisibility():
                                a.GetProperty().SetEdgeColor(vtk_color[0] * 0.6, vtk_color[1] * 0.6, vtk_color[2] * 0.6)
                    except Exception:
                        pass
            return c

        # ── helpers ──────────────────────────────────────────────────────────
        def _entry_by_item(item):
            if item is None:
                return None
            lid = item.data(0, Qt.UserRole)
            for e in _registry(app):
                if e["id"] == lid:
                    return e
            return None

        def _selected_entries():
            entries = []
            for item in layer_list.selectedItems():
                if item.parent() is not None:
                    continue
                lid = item.data(0, Qt.UserRole)
                if lid is not None:
                    for e in _registry(app):
                        if e["id"] == lid and e not in entries:
                            entries.append(e)
                            break
            return entries

        def _current_entry():
            item = layer_list.currentItem()
            return _entry_by_item(item)

        def _sync_table_btn():
            selections = _selected_entries()
            has_tabular = len(selections) > 0 and any(
                e.get("kind") in ("vector", "table") for e in selections
            )
            tb_table.setEnabled(has_tabular)
            has_gdb = len(selections) > 0 and any(e.get("gdb_path") for e in selections)
            tb_domains.setEnabled(has_gdb)
            
            # Sync tb_remove styling and tooltip based on selection
            if not selections:
                tb_remove.setToolTip("Clear all layers")
                tb_remove.setStyleSheet(f"QToolButton#occTool:hover {{ background: {cols['danger']}; color: white; }}")
            else:
                tb_remove.setToolTip("Remove selected layer")
                tb_remove.setStyleSheet("")

        def _select_id(lid):
            for i in range(layer_list.topLevelItemCount()):
                item = layer_list.topLevelItem(i)
                if item.data(0, Qt.UserRole) == lid:
                    layer_list.setCurrentItem(item)
                    break

        def _feature_count(entry_or_sub) -> int:
            if "_cached_count" in entry_or_sub:
                return entry_or_sub["_cached_count"]
            total = 0
            actors = []
            if "actors" in entry_or_sub:
                actors = entry_or_sub["actors"]
            elif "actor" in entry_or_sub and entry_or_sub["actor"] is not None:
                actors = [entry_or_sub["actor"]]
                
            for a in actors:
                try:
                    mapper = a.GetMapper()
                    if mapper:
                        pd = mapper.GetInput()
                        if pd:
                            total += pd.GetNumberOfCells()
                except Exception:
                    pass
            entry_or_sub["_cached_count"] = total
            return total

        def refresh():
            reg = _registry(app)
            cur = _current_entry()
            prev = cur["id"] if cur else None
            
            layer_list.blockSignals(True)
            model = layer_list.model()
            if model is not None:
                model.blockSignals(True)
            layer_list.clear()
            
            cols_theme = _occ_colors()
            
            for e in reg:
                # Create root item for the layer
                item = QTreeWidgetItem()
                item.setData(0, Qt.UserRole, e["id"])
                # Set drag and drop flags for top-level item (reordering only, no merging as child)
                item.setFlags((item.flags() | Qt.ItemIsDragEnabled) & ~Qt.ItemIsDropEnabled)
                
                # Checkbox
                item.setCheckState(0, Qt.Checked if e.get("visible", True) else Qt.Unchecked)
                
                # Determine geometry type
                geom_type = e.get("geom")
                if not geom_type:
                    if e.get("kind") == "raster":
                        geom_type = "raster"
                    else:
                        name_lower = e.get("name", "").lower()
                        if "_pnt" in name_lower or "point" in name_lower:
                            geom_type = "point"
                        elif "_line" in name_lower or "line" in name_lower:
                            geom_type = "line"
                        else:
                            geom_type = "polygon"

                # Check if layer has multiple sub-layers
                has_multiple_subs = "sub_layers" in e and len(e["sub_layers"]) > 1

                # Icon
                if has_multiple_subs:
                    multi_geom = f"multi{geom_type}" if geom_type in ("point", "line", "polygon") else geom_type
                    symbol_color = _layer_color(e).name()
                    item.setIcon(0, QIcon(_symbol_pixmap(multi_geom, symbol_color, 14)))
                else:
                    if "sub_layers" in e and len(e["sub_layers"]) == 1:
                        sub_info = list(e["sub_layers"].values())[0]
                        symbol_color = sub_info.get("color") or _layer_color(e).name()
                    else:
                        symbol_color = _layer_color(e).name()
                    item.setIcon(0, QIcon(_symbol_pixmap(geom_type, symbol_color, 14)))
                
                # Text & count
                item.setText(0, e["name"])
                if e.get("kind") != "raster":
                    count = _feature_count(e)
                    item.setText(1, str(count))
                    item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
                    item.setForeground(1, QColor(cols_theme["muted"]))
                else:
                    item.setText(1, "-")
                    item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
                    item.setForeground(1, QColor(cols_theme["muted"]))
                
                # Font and colors
                font = layer_list.font()
                font.setPointSize(9.0)
                item.setFont(0, font)
                item.setFont(1, font)
                item.setForeground(0, QColor(cols_theme["text"]))
                
                layer_list.addTopLevelItem(item)
                
                # If layer has multiple sub-layers, add them as child items!
                if has_multiple_subs:
                    for sub_code, sub_info in e["sub_layers"].items():
                        child = QTreeWidgetItem()
                        child.setData(0, Qt.UserRole, e["id"])
                        child.setData(0, Qt.UserRole + 1, sub_code)
                        # Sub-layers cannot be dragged and do not accept drops
                        child.setFlags(child.flags() & ~Qt.ItemIsDragEnabled & ~Qt.ItemIsDropEnabled)
                        
                        # Sub-layer visibility
                        sub_visible = True
                        sub_actors = _sub_layer_actors(sub_info)
                        if sub_actors:
                            try:
                                sub_visible = any(bool(actor.GetVisibility()) for actor in sub_actors)
                            except Exception:
                                pass
                        child.setCheckState(0, Qt.Checked if sub_visible else Qt.Unchecked)
                        
                        # Sub-layer colored geometry symbol
                        sub_color = sub_info.get("color", "#ffffff")
                        child.setIcon(0, QIcon(_symbol_pixmap(geom_type, sub_color, 12)))
                        
                        # Sub-layer text & count
                        child.setText(0, sub_info.get("label", "Subtype"))
                        sub_count = _feature_count(sub_info)
                        child.setText(1, str(sub_count))
                        child.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
                        child.setForeground(1, QColor(cols_theme["muted"]))
                        
                        sub_font = layer_list.font()
                        sub_font.setPointSize(8.5)
                        child.setFont(0, sub_font)
                        child.setFont(1, sub_font)
                        child.setForeground(0, QColor(cols_theme["text"]))
                        
                        item.addChild(child)
                    
                    # Expand the item by default to show subtypes
                    item.setExpanded(True)
                    
            model = layer_list.model()
            if model is not None:
                model.blockSignals(False)
            layer_list.blockSignals(False)
            stack.setCurrentIndex(0 if reg else 1)
            if prev is not None:
                _select_id(prev)
            _sync_table_btn()

        panel.refresh = refresh

        def _reorder_from_list():
            ids = []
            for i in range(layer_list.topLevelItemCount()):
                item = layer_list.topLevelItem(i)
                lid = item.data(0, Qt.UserRole)
                if lid is not None:
                    ids.append(lid)
            if ids:
                _reorder_registry(app, ids)
            refresh()

        layer_list.model().rowsMoved.connect(_reorder_from_list)
        layer_list.model().layoutChanged.connect(_reorder_from_list)

        # Checkbox toggling handler
        def _on_item_changed(item, column):
            lid = item.data(0, Qt.UserRole)
            if lid is None:
                return
                
            parent_item = item.parent()
            checked = item.checkState(0) == Qt.Checked
            
            if parent_item:
                # Sub-layer child item checkbox toggled
                parent_lid = parent_item.data(0, Qt.UserRole)
                entry = None
                for e in _registry(app):
                    if e["id"] == parent_lid:
                        entry = e
                        break
                if entry and "sub_layers" in entry:
                    sub_code = item.data(0, Qt.UserRole + 1)
                    sub_info = entry["sub_layers"].get(sub_code)
                    if sub_info:
                        for actor in _sub_layer_actors(sub_info):
                            try:
                                actor.SetVisibility(1 if checked else 0)
                            except Exception:
                                pass
                    
                    # Sync parent checkbox based on children states
                    any_checked = False
                    for idx in range(parent_item.childCount()):
                        if parent_item.child(idx).checkState(0) == Qt.Checked:
                            any_checked = True
                            break
                    
                    layer_list.blockSignals(True)
                    parent_item.setCheckState(0, Qt.Checked if any_checked else Qt.Unchecked)
                    entry["visible"] = any_checked
                    layer_list.blockSignals(False)
                _render(app)
            else:
                # Main layer root item checkbox toggled
                entry = None
                for e in _registry(app):
                    if e["id"] == lid:
                        entry = e
                        break
                if entry:
                    entry["visible"] = checked
                    
                    # Toggle all sub-layers
                    if "sub_layers" in entry:
                        layer_list.blockSignals(True)
                        for idx in range(item.childCount()):
                            child = item.child(idx)
                            child.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
                        layer_list.blockSignals(False)
                        
                        for sub_info in entry["sub_layers"].values():
                            for actor in _sub_layer_actors(sub_info):
                                try:
                                    actor.SetVisibility(1 if checked else 0)
                                except Exception:
                                    pass
                    else:
                        for a in entry["actors"]:
                            try:
                                a.SetVisibility(1 if checked else 0)
                            except Exception:
                                pass
                _render(app)

        layer_list.itemChanged.connect(_on_item_changed)

        def _apply_filter(text):
            t = (text or "").strip().lower()
            for i in range(layer_list.topLevelItemCount()):
                item = layer_list.topLevelItem(i)
                lid = item.data(0, Qt.UserRole)
                parent_entry = None
                for e in _registry(app):
                    if e["id"] == lid:
                        parent_entry = e
                        break
                
                if not parent_entry:
                    continue
                    
                parent_name = parent_entry.get("name", "").lower()
                parent_matches = not t or (t in parent_name)
                
                # Check children
                any_child_matches = False
                child_matches_mask = []
                for c_idx in range(item.childCount()):
                    child = item.child(c_idx)
                    child_label = child.text(0).lower()
                    matches = not t or (t in child_label)
                    if matches:
                        any_child_matches = True
                    child_matches_mask.append(matches)
                
                # Show/hide logic
                if parent_matches or any_child_matches:
                    item.setHidden(False)
                    for c_idx in range(item.childCount()):
                        child = item.child(c_idx)
                        child.setHidden(not parent_matches and not child_matches_mask[c_idx])
                else:
                    item.setHidden(True)

        def _toggle_filter(on):
            filter_box.setVisible(on)
            if on:
                filter_box.setFocus()
            else:
                filter_box.clear()
                _apply_filter("")

        # ── actions ──────────────────────────────────────────────────────────
        def _on_opacity(val):
            op_value.setText(f"{val}%")
            for e in _selected_entries():
                _set_layer_opacity(app, e, val / 100.0)

        def _do_import():
            unified_import_overlay(app)

        def _do_zoom():
            for e in _selected_entries():
                _zoom_to_layer(app, e)

        def _do_remove():
            selections = _selected_entries()
            from PySide6.QtWidgets import QMessageBox
            if not selections:
                reg = _registry(app)
                if not reg:
                    return
                if QMessageBox.question(
                    app, "Clear all layers", "Remove ALL layers from the scene?"
                ) == QMessageBox.Yes:
                    for e in list(reg):
                        # Clean up open attribute tables for this layer (including sub-layers)
                        sid = e["id"]
                        if hasattr(app, "_attribute_tables"):
                            for k in list(app._attribute_tables.keys()):
                                if (isinstance(k, tuple) and k[0] == sid) or k == sid:
                                    try:
                                        app._attribute_tables[k].close()
                                    except Exception:
                                        pass
                        _remove_layer(app, e)
                    refresh()
                return

            names_str = ", ".join(f'"{e["name"]}"' for e in selections)
            if QMessageBox.question(
                app, "Remove layer", f"Remove selected layer(s) {names_str} from the scene?"
            ) == QMessageBox.Yes:
                for e in selections:
                    # Clean up open attribute tables for this layer (including sub-layers)
                    sid = e["id"]
                    if hasattr(app, "_attribute_tables"):
                        for k in list(app._attribute_tables.keys()):
                            if (isinstance(k, tuple) and k[0] == sid) or k == sid:
                                try:
                                    app._attribute_tables[k].close()
                                except Exception:
                                    pass
                    _remove_layer(app, e)
                refresh()

        def _do_rename():
            e = _current_entry()
            if e is None:
                return
            from PySide6.QtWidgets import QInputDialog
            new, ok = QInputDialog.getText(app, "Rename layer", "Name:", text=e["name"])
            if ok and new.strip():
                e["name"] = new.strip()
                refresh()
                _select_id(e["id"])

        def _do_merge_layers():
            selections = _selected_entries()
            if len(selections) <= 1:
                return

            from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QPushButton
            
            dlg = QDialog(app)
            dlg.setWindowTitle("Merge Layers")
            dlg.setMinimumWidth(320)
            dlg.setWindowFlags(dlg.windowFlags() | Qt.Tool)
            
            v = QVBoxLayout(dlg)
            v.setContentsMargins(12, 12, 12, 12)
            v.setSpacing(10)
            
            v.addWidget(QLabel("Select target layer to merge other selected layers into:"))
            
            cmb_target = QComboBox()
            for entry in selections:
                cmb_target.addItem(entry["name"], entry["id"])
            v.addWidget(cmb_target)
            
            # Show which layers will be merged
            lbl_info = QLabel("The other selected layers will be merged into the target.")
            lbl_info.setStyleSheet("color: #888888; font-size: 11px;")
            v.addWidget(lbl_info)
            
            # Buttons
            h = QHBoxLayout()
            h.addStretch()
            btn_merge = QPushButton("Merge")
            btn_merge.setStyleSheet("font-weight: bold;")
            btn_cancel = QPushButton("Cancel")
            h.addWidget(btn_cancel)
            h.addWidget(btn_merge)
            v.addLayout(h)
            
            btn_merge.clicked.connect(dlg.accept)
            btn_cancel.clicked.connect(dlg.reject)
            
            if dlg.exec() == QDialog.Accepted:
                target_id = cmb_target.currentData()
                target_entry = None
                for entry in selections:
                    if entry["id"] == target_id:
                        target_entry = entry
                        break
                
                if target_entry:
                    # Perform the merge!
                    sources = [entry for entry in selections if entry["id"] != target_id]
                    reg = _registry(app)
                    
                    for source_entry in sources:
                        # 1. Merge actors
                        target_entry["actors"].extend(source_entry.get("actors", []))
                        
                        # 2. Merge sub_layers
                        if "sub_layers" in source_entry:
                            if "sub_layers" not in target_entry:
                                target_entry["sub_layers"] = {}
                            
                            for sub_code, sub_info in source_entry["sub_layers"].items():
                                if sub_code in target_entry["sub_layers"]:
                                    # Merge subtype actors
                                    target_sub = target_entry["sub_layers"][sub_code]
                                    target_sub["actors"] = list(target_sub.get("actors", [])) + list(sub_info.get("actors", []))
                                    if not target_sub.get("actor") and sub_info.get("actor"):
                                        target_sub["actor"] = sub_info["actor"]
                                    target_sub.pop("_cached_count", None)
                                    target_entry.pop("_cached_count", None)
                                else:
                                    # Copy subtype info over safely (avoid sharing actors list reference)
                                    new_sub = sub_info.copy()
                                    if "actors" in sub_info:
                                        new_sub["actors"] = list(sub_info["actors"])
                                    target_entry["sub_layers"][sub_code] = new_sub
                                    target_entry.pop("_cached_count", None)
                        
                        # 3. Clean up any open attribute table dialogs for the source layer
                        sid = source_entry["id"]
                        if sid in getattr(app, "_attribute_tables", {}):
                            try:
                                app._attribute_tables[sid].close()
                            except Exception:
                                pass
                        
                        # 4. Remove from registry directly without removing actors from scene
                        if source_entry in reg:
                            reg.remove(source_entry)
                    
                    # Refresh panel & render viewport to apply changes
                    refresh()
                    _render(app)

        def _selected_sub_layers():
            subs = []
            for item in layer_list.selectedItems():
                if item.parent() is not None:
                    parent_id = item.data(0, Qt.UserRole)
                    sub_code = item.data(0, Qt.UserRole + 1)
                    if parent_id is not None and sub_code is not None:
                        for e in _registry(app):
                            if e["id"] == parent_id:
                                subs.append((e, sub_code, item))
                                break
            return subs

        def _do_merge_sub_layers():
            selected_subs = _selected_sub_layers()
            if len(selected_subs) <= 1:
                return
                
            from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QPushButton
            
            dlg = QDialog(app)
            dlg.setWindowTitle("Merge Sub-layers")
            dlg.setMinimumWidth(360)
            dlg.setWindowFlags(dlg.windowFlags() | Qt.Tool)
            
            v = QVBoxLayout(dlg)
            v.setContentsMargins(12, 12, 12, 12)
            v.setSpacing(10)
            
            v.addWidget(QLabel("Select target sub-layer to merge other selected sub-layers into:"))
            
            cmb_target = QComboBox()
            for idx, (parent_entry, sub_code, item) in enumerate(selected_subs):
                sub_layer_data = parent_entry.get("sub_layers", {}).get(sub_code, {})
                label = f"{parent_entry['name']} - {sub_layer_data.get('label', 'Subtype')}"
                cmb_target.addItem(label, idx)
            v.addWidget(cmb_target)
            
            lbl_info = QLabel("The other selected sub-layers will be merged into the target.")
            lbl_info.setStyleSheet("color: #888888; font-size: 11px;")
            v.addWidget(lbl_info)
            
            h = QHBoxLayout()
            h.addStretch()
            btn_merge = QPushButton("Merge")
            btn_merge.setStyleSheet("font-weight: bold;")
            btn_cancel = QPushButton("Cancel")
            h.addWidget(btn_cancel)
            h.addWidget(btn_merge)
            v.addLayout(h)
            
            btn_merge.clicked.connect(dlg.accept)
            btn_cancel.clicked.connect(dlg.reject)
            
            if dlg.exec() == QDialog.Accepted:
                target_idx = cmb_target.currentData()
                target_parent, target_code, _ = selected_subs[target_idx]
                target_sub = target_parent["sub_layers"][target_code]
                
                # Convert target color to VTK color format
                target_color_str = target_sub.get("color", "#ffffff")
                from PySide6.QtGui import QColor
                qcol = QColor(target_color_str)
                vtk_color = (qcol.red() / 255.0, qcol.green() / 255.0, qcol.blue() / 255.0)
                
                # Perform the merge!
                for idx, (source_parent, source_code, _) in enumerate(selected_subs):
                    if idx == target_idx:
                        continue
                        
                    source_sub = source_parent["sub_layers"][source_code]
                    
                    # 1. Merge actors/actor of the sub-layer
                    target_sub["actors"] = list(target_sub.get("actors", [])) + list(source_sub.get("actors", []))
                    if not target_sub.get("actor") and source_sub.get("actor"):
                        target_sub["actor"] = source_sub["actor"]
                    target_sub.pop("_cached_count", None)
                    target_parent.pop("_cached_count", None)
                        
                    # Recolor source actors safely
                    source_actors = []
                    for a in source_sub.get("actors", []):
                        if a is not None and a not in source_actors:
                            source_actors.append(a)
                    s_actor = source_sub.get("actor")
                    if s_actor is not None and s_actor not in source_actors:
                        source_actors.append(s_actor)
                        
                    for a in source_actors:
                        try:
                            prop = getattr(a, "GetProperty", None)
                            if prop:
                                p_obj = prop()
                                if p_obj:
                                    p_obj.SetColor(*vtk_color)
                                    if hasattr(p_obj, "GetEdgeVisibility") and p_obj.GetEdgeVisibility():
                                        p_obj.SetEdgeColor(vtk_color[0] * 0.6, vtk_color[1] * 0.6, vtk_color[2] * 0.6)
                        except Exception:
                            pass
                            
                    # 2. Clean up any open attribute table dialogs for this specific sub-layer
                    spid = (source_parent["id"], source_code)
                    if hasattr(app, "_attribute_tables"):
                        if spid in app._attribute_tables:
                            try:
                                app._attribute_tables[spid].close()
                            except Exception:
                                pass
                                
                    # 3. Remove the source sub-layer from its parent's registry entry!
                    if source_code in source_parent["sub_layers"]:
                        del source_parent["sub_layers"][source_code]
                        
                # Refresh panel & render viewport
                refresh()
                _render(app)

        def _do_remove_sub_layer(parent_entry, sub_code):
            from PySide6.QtWidgets import QMessageBox
            sub_info = parent_entry.get("sub_layers", {}).get(sub_code, {})
            sub_label = sub_info.get("label", "Sub-layer")
            if QMessageBox.question(
                app, "Remove sub-layer", f"Remove sub-layer '{sub_label}' from the scene?"
            ) == QMessageBox.Yes:
                # Close any open attribute table for this sub-layer
                spid = (parent_entry["id"], sub_code)
                if hasattr(app, "_attribute_tables"):
                    if spid in app._attribute_tables:
                        try:
                            app._attribute_tables[spid].close()
                        except Exception:
                            pass
                
                # Delete sub-layer info
                if sub_code in parent_entry.get("sub_layers", {}):
                    del parent_entry["sub_layers"][sub_code]
                refresh()
                _render(app)

        def _do_add_new_layer():
            from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox, QPushButton
            
            dlg = QDialog(app)
            dlg.setWindowTitle("Create New Layer")
            dlg.setMinimumWidth(300)
            dlg.setWindowFlags(dlg.windowFlags() | Qt.Tool)
            
            v = QVBoxLayout(dlg)
            v.setContentsMargins(12, 12, 12, 12)
            v.setSpacing(10)
            
            v.addWidget(QLabel("Layer Name:"))
            txt_name = QLineEdit("New Layer")
            v.addWidget(txt_name)
            
            v.addWidget(QLabel("Geometry Type:"))
            cmb_geom = QComboBox()
            cmb_geom.addItems(["Point", "Line", "Polygon"])
            v.addWidget(cmb_geom)
            
            h = QHBoxLayout()
            h.addStretch()
            btn_ok = QPushButton("OK")
            btn_ok.setStyleSheet("font-weight: bold;")
            btn_cancel = QPushButton("Cancel")
            h.addWidget(btn_cancel)
            h.addWidget(btn_ok)
            v.addLayout(h)
            
            btn_ok.clicked.connect(dlg.accept)
            btn_cancel.clicked.connect(dlg.reject)
            
            if dlg.exec() == QDialog.Accepted:
                name = txt_name.text().strip() or "New Layer"
                geom = cmb_geom.currentText().lower()
                
                entry = register_gis_layer(app, name, "", "vector", "geojson", [], allow_empty=True)
                if entry:
                    entry["geom"] = geom
                    used_colors = set(e.get("color") for e in _registry(app) if e.get("color"))
                    color_q = _default_color_for(entry.get("id", 0), used_colors)
                    entry["color"] = color_q.name()
                    entry["sub_layers"] = {}
                    
                    refresh()
                    _select_id(entry["id"])

        def _do_create_sub_layer(e):
            if e is None:
                return
                
            from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton
            
            dlg = QDialog(app)
            dlg.setWindowTitle("Create Sub-layer (Subtype)")
            dlg.setMinimumWidth(300)
            dlg.setWindowFlags(dlg.windowFlags() | Qt.Tool)
            
            v = QVBoxLayout(dlg)
            v.setContentsMargins(12, 12, 12, 12)
            v.setSpacing(10)
            
            v.addWidget(QLabel("Subtype Name (Label):"))
            txt_label = QLineEdit("New Subtype")
            v.addWidget(txt_label)
            
            v.addWidget(QLabel("Subtype Code (Key):"))
            txt_code = QLineEdit("new_subtype")
            v.addWidget(txt_code)
            
            h = QHBoxLayout()
            h.addStretch()
            btn_ok = QPushButton("OK")
            btn_ok.setStyleSheet("font-weight: bold;")
            btn_cancel = QPushButton("Cancel")
            h.addWidget(btn_cancel)
            h.addWidget(btn_ok)
            v.addLayout(h)
            
            btn_ok.clicked.connect(dlg.accept)
            btn_cancel.clicked.connect(dlg.reject)
            
            if dlg.exec() == QDialog.Accepted:
                label = txt_label.text().strip() or "New Subtype"
                code = txt_code.text().strip() or "new_subtype"
                
                if "sub_layers" not in e:
                    e["sub_layers"] = {}
                
                used_colors = set(x.get("color") for x in _registry(app) if x.get("color"))
                for sub_item in e.get("sub_layers", {}).values():
                    if sub_item.get("color"):
                        used_colors.add(sub_item["color"])
                        
                sub_color = _default_color_for(len(e["sub_layers"]), used_colors).name()
                
                e["sub_layers"][code] = {
                    "actor": None,
                    "actors": [],
                    "color": sub_color,
                    "label": label
                }
                
                refresh()
                _select_id(e["id"])

        def _do_open_table():
            current_item = layer_list.currentItem()
            sub_code = None
            if current_item and current_item.parent() is not None:
                sub_code = current_item.data(0, Qt.UserRole + 1)

            selections = _selected_entries()
            table_entries = [e for e in selections if e.get("kind") in ("vector", "table")]
            if not table_entries:
                return
            if not hasattr(app, "_attribute_tables"):
                app._attribute_tables = {}
            for e in table_entries:
                lid = e["id"]
                # Use a combined key if opening a specific subclass table
                table_key = (lid, sub_code)
                if table_key in app._attribute_tables:
                    existing = app._attribute_tables[table_key]
                    existing.show()
                    existing.raise_()
                    existing.activateWindow()
                    continue
                dlg = AttributeTableDialog(app, e, sub_code)
                app._attribute_tables[table_key] = dlg
                def make_cleanup(key):
                    def _cleanup(*args):
                        if key in app._attribute_tables:
                            del app._attribute_tables[key]
                    return _cleanup
                dlg.finished.connect(make_cleanup(table_key))
                dlg.show()
                dlg.raise_()
                dlg.activateWindow()

        def _do_open_domains():
            selections = _selected_entries()
            gdb_entries = [e for e in selections if e.get("gdb_path")]
            if not gdb_entries:
                return
            from gui.gis.gdb.domains_dialog import WorkspaceDomainsDialog
            from gui.gis.gdb.engine import get_engine
            
            # Find if a specific sub-layer is currently selected
            current_item = layer_list.currentItem()
            sub_label = None
            if current_item and current_item.parent() is not None:
                sub_label = current_item.text(0)

            if not hasattr(app, "_gdb_domains_dialogs"):
                app._gdb_domains_dialogs = {}
            for e in gdb_entries:
                gdb_path = e["gdb_path"]
                normalized_path = os.path.normpath(gdb_path).lower()
                
                # Get the domain names to find the best match
                engine = get_engine(gdb_path)
                if not engine:
                    continue
                    
                best_domain = None
                try:
                    domains_meta = engine.export_domains_meta()
                    domain_names = [d["name"] for d in domains_meta]
                    
                    # Heuristic matching
                    parent_clean = e["name"].split("·")[-1].split("—")[-1].strip().lower()
                    for suffix in ("_poly", "_line", "_pnt"):
                        if parent_clean.endswith(suffix):
                            parent_clean = parent_clean[:-len(suffix)]
                            
                    best_match = None
                    best_score = -1
                    
                    for dname in domain_names:
                        dname_lower = dname.lower()
                        score = 0
                        
                        if sub_label:
                            sub_lower = sub_label.lower()
                            if sub_lower in dname_lower:
                                score += 10
                                if dname_lower == sub_lower or f"_{sub_lower}" in dname_lower or f"{sub_lower}_" in dname_lower:
                                    score += 5
                            elif dname_lower in sub_lower:
                                score += 2
                        else:
                            if parent_clean in dname_lower:
                                score += 5
                            elif dname_lower in parent_clean:
                                score += 1
                                
                        if parent_clean and parent_clean in dname_lower:
                            score += 3
                            
                        if score > best_score and score > 0:
                            best_score = score
                            best_match = dname
                            
                    best_domain = best_match
                except Exception as exc:
                    print(f"Error resolving best domain match: {exc}")

                if normalized_path in app._gdb_domains_dialogs:
                    existing = app._gdb_domains_dialogs[normalized_path]
                    if best_domain:
                        existing.select_domain(best_domain)
                    existing.show()
                    existing.raise_()
                    existing.activateWindow()
                    continue

                dlg = WorkspaceDomainsDialog(engine, app, best_domain)
                app._gdb_domains_dialogs[normalized_path] = dlg
                def make_cleanup(gdb_key):
                    def _cleanup(*args):
                        if gdb_key in app._gdb_domains_dialogs:
                            del app._gdb_domains_dialogs[gdb_key]
                    return _cleanup
                dlg.finished.connect(make_cleanup(normalized_path))
                dlg.show()
                dlg.raise_()
                dlg.activateWindow()

        def _toggle_all_visibility():
            reg = _registry(app)
            target = not any(e.get("visible") for e in reg)
            for e in reg:
                _set_layer_visible(app, e, target)
            refresh()

        def _context_menu(pos):
            item = layer_list.itemAt(pos)
            from PySide6.QtWidgets import QMenu
            menu = QMenu(layer_list)
            
            if item is not None:
                if not item.isSelected():
                    layer_list.clearSelection()
                    item.setSelected(True)
                    layer_list.setCurrentItem(item)
                e = _entry_by_item(item)
                if e is not None:
                    is_child = item.parent() is not None
                    sub_code = item.data(0, Qt.UserRole + 1) if is_child else None
                    
                    def _do_change_color():
                        from PySide6.QtWidgets import QColorDialog
                        if is_child:
                            current_color_str = e.get("sub_layers", {}).get(sub_code, {}).get("color", "#ffffff")
                        else:
                            current_color_str = e.get("color", "#ffffff")
                            
                        initial_color = QColor(current_color_str)
                        new_color = QColorDialog.getColor(initial_color, app, "Select Layer Color")
                        if not new_color.isValid():
                            return
                            
                        new_color_hex = new_color.name()
                        vtk_color = (new_color.redF(), new_color.greenF(), new_color.blueF())
                        
                        if is_child:
                            sub_info = e.get("sub_layers", {}).get(sub_code)
                            if sub_info:
                                sub_info["color"] = new_color_hex
                                sub_info.pop("_cached_count", None)
                                # Recolor sub-layer actors safely
                                actors = []
                                for a in sub_info.get("actors", []):
                                    if a is not None and a not in actors:
                                        actors.append(a)
                                s_actor = sub_info.get("actor")
                                if s_actor is not None and s_actor not in actors:
                                    actors.append(s_actor)
                                    
                                for a in actors:
                                    try:
                                        prop = getattr(a, "GetProperty", None)
                                        if prop:
                                            p_obj = prop()
                                            if p_obj:
                                                p_obj.SetColor(*vtk_color)
                                                if hasattr(p_obj, "GetEdgeVisibility") and p_obj.GetEdgeVisibility():
                                                    p_obj.SetEdgeColor(vtk_color[0] * 0.6, vtk_color[1] * 0.6, vtk_color[2] * 0.6)
                                    except Exception:
                                        pass
                        else:
                            e["color"] = new_color_hex
                            e.pop("_cached_count", None)
                            # Recolor layer actors safely
                            actors = []
                            for a in e.get("actors", []):
                                if a is not None and a not in actors:
                                    actors.append(a)
                                    
                            for a in actors:
                                try:
                                    prop = getattr(a, "GetProperty", None)
                                    if prop:
                                        p_obj = prop()
                                        if p_obj:
                                            p_obj.SetColor(*vtk_color)
                                            if hasattr(p_obj, "GetEdgeVisibility") and p_obj.GetEdgeVisibility():
                                                p_obj.SetEdgeColor(vtk_color[0] * 0.6, vtk_color[1] * 0.6, vtk_color[2] * 0.6)
                                except Exception:
                                    pass
                                    
                        # Defer refresh and render using QTimer.singleShot to avoid Qt deletion-during-event crashes
                        from PySide6.QtCore import QTimer
                        QTimer.singleShot(0, refresh)
                        QTimer.singleShot(0, lambda: _render(app))

                    if is_child:
                        menu.addAction("Zoom to layer", _do_zoom)
                        menu.addAction("Open attribute table", _do_open_table)
                        menu.addAction("Change Color…", _do_change_color)
                        if e.get("gdb_path"):
                            menu.addAction("Workspace Domains…", _do_open_domains)
                        
                        # Merging sub-layers option: only if multiple sub-layers are selected
                        selected_subs = _selected_sub_layers()
                        if len(selected_subs) > 1:
                            menu.addSeparator()
                            menu.addAction("Merge selected sub-layers…", _do_merge_sub_layers)
                            
                        menu.addSeparator()
                        menu.addAction("Remove sub-layer", lambda: _do_remove_sub_layer(e, sub_code))
                    else:
                        menu.addAction("Zoom to layer", _do_zoom)
                        menu.addAction("Rename…", _do_rename)
                        if e.get("kind") in ("vector", "table"):
                            menu.addAction("Open attribute table", _do_open_table)
                            menu.addAction("Change Color…", _do_change_color)
                        if e.get("gdb_path"):
                            menu.addAction("Workspace Domains…", _do_open_domains)
                        
                        # Merging option: only if multiple layers are selected
                        selections = _selected_entries()
                        if len(selections) > 1:
                            menu.addSeparator()
                            menu.addAction("Merge selected layers…", _do_merge_layers)
                        
                        # Sub-layer creation option
                        menu.addSeparator()
                        menu.addAction("Create sub-layer (subtype)…", lambda: _do_create_sub_layer(e))
                        
                        menu.addSeparator()
                        menu.addAction("Remove", _do_remove)
            else:
                # Clicked on empty area: global panel actions
                menu.addAction("Create new layer…", _do_add_new_layer)
                menu.addAction("Import GIS overlay…", _do_import)
                
            menu.exec(layer_list.viewport().mapToGlobal(pos))

        layer_list.customContextMenuRequested.connect(_context_menu)
        layer_list.itemSelectionChanged.connect(_sync_table_btn)
        tb_import.clicked.connect(_do_add_new_layer)
        tb_eye.clicked.connect(_toggle_all_visibility)
        tb_filter.toggled.connect(_toggle_filter)
        filter_box.textChanged.connect(_apply_filter)
        tb_remove.clicked.connect(_do_remove)
        tb_table.clicked.connect(_do_open_table)
        tb_domains.clicked.connect(_do_open_domains)

        refresh()
        return panel


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC: show / toggle the panel (sits inside the splitter, not a dock)
# ─────────────────────────────────────────────────────────────────────────────
def _current_theme_name():
    try:
        from gui.theme_manager import ThemeColors
        return ThemeColors.current_theme()
    except Exception:
        return "dark"


def show_gis_layers_panel(app):
    panel = getattr(app, _PANEL_ATTR, None)

    if panel is None:
        panel = GisLayersDock.create(app)
        setattr(app, _PANEL_ATTR, panel)
        try:
            splitter = app.splitter
            splitter.insertWidget(0, panel)
            sizes = splitter.sizes()
            if len(sizes) >= 2:
                splitter.setSizes([280] + sizes[1:])
        except Exception:
            pass
    else:
        try:
            panel.refresh()
        except Exception:
            pass
    try:
        panel.setStyleSheet(_build_occ_style())
    except Exception:
        pass
    panel.show()
    if hasattr(app, "_sync_activity_bar"):
        try:
            app._sync_activity_bar()
        except Exception:
            pass
    return panel


def toggle_gis_layers_panel(app):
    panel = getattr(app, _PANEL_ATTR, None)
    if panel is not None and panel.isVisible():
        panel.hide()
        if hasattr(app, "_sync_activity_bar"):
            try:
                app._sync_activity_bar()
            except Exception:
                pass
        return panel
    return show_gis_layers_panel(app)


# ─────────────────────────────────────────────────────────────────────────────
# UNIFIED IMPORT — auto-detect format, import, register, open the panel
# ─────────────────────────────────────────────────────────────────────────────
def unified_import_overlay(app):
    """
    One entry point that imports .shp / .tif / .tiff / .geojson, registers each
    as a managed layer, and opens the Overlay Control Center.
    """
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    filt = (
        "All supported (*.shp *.tif *.tiff *.geojson *.json *.gdb);;"
        "Shapefile (*.shp);;"
        "GeoTIFF (*.tif *.tiff);;"
        "GeoJSON (*.geojson *.json);;"
        "ESRI File Geodatabase (*.gdb)"
    )
    paths, _ = QFileDialog.getOpenFileNames(
        app, "Import GIS overlay (multiple allowed)", str(Path.home()), filt
    )
    if not paths:
        return

    ok, fail = 0, 0
    for p in sort_imports_vectors_first(paths):
        try:
            if _import_one(app, p):
                ok += 1
            else:
                fail += 1
        except Exception as exc:
            fail += 1
            print(f"   ❌ Import error for {p}: {exc}")
            import traceback
            traceback.print_exc()

    show_gis_layers_panel(app)

    if fail and not ok:
        QMessageBox.warning(app, "Import",
                            "None of the selected files could be imported. "
                            "See the console for details.")


_RASTER_EXTS = (".tif", ".tiff")


def sort_imports_vectors_first(paths):
    """
    Order a batch so vectors import BEFORE rasters. A non-georeferenced raster
    fits itself to whatever scene data already exists; importing vectors first
    means the raster lands on the data instead of at raw pixel coordinates.
    """
    vectors = [p for p in paths if not str(p).lower().endswith(_RASTER_EXTS)]
    rasters = [p for p in paths if str(p).lower().endswith(_RASTER_EXTS)]
    return vectors + rasters


_GM_EXE = r"C:\Program Files\GlobalMapper23.0_64bit\global_mapper.exe"


def _ecw_driver_available() -> bool:
    """Return True if the active GDAL build has the ECW raster driver."""
    try:
        from osgeo import gdal
        return gdal.GetDriverByName("ECW") is not None
    except Exception:
        return False


def _show_ecw_dialog(app, path: str) -> None:
    import os, subprocess
    from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton, QHBoxLayout
    from PySide6.QtCore import Qt

    dlg = QDialog(app)
    dlg.setWindowTitle("ECW — Conversion Required")
    dlg.setMinimumWidth(480)

    layout = QVBoxLayout(dlg)
    layout.setSpacing(12)

    msg = QLabel(
        "<b>ECW files cannot be loaded directly.</b><br><br>"
        "The GDAL library in this application does not include the proprietary "
        "Hexagon ECW driver.<br><br>"
        "<b>Quickest fix:</b> Open in Global Mapper → <i>File → Export → "
        "Raster/Image Format</i> → choose <b>GeoTIFF</b>, save the TIF, "
        "then drag the <b>.tif</b> file into this window."
    )
    msg.setWordWrap(True)
    msg.setTextFormat(Qt.RichText)
    layout.addWidget(msg)

    btn_row = QHBoxLayout()

    import os.path as osp
    gm_available = osp.isfile(_GM_EXE)

    if gm_available:
        btn_gm = QPushButton("Open in Global Mapper")
        btn_gm.setDefault(True)
        def _open_gm():
            subprocess.Popen([_GM_EXE, path])
            dlg.accept()
        btn_gm.clicked.connect(_open_gm)
        btn_row.addWidget(btn_gm)

    btn_explorer = QPushButton("Show File in Explorer")
    def _show_explorer():
        subprocess.Popen(["explorer", "/select,", path.replace("/", "\\")])
        dlg.accept()
    btn_explorer.clicked.connect(_show_explorer)
    btn_row.addWidget(btn_explorer)

    btn_cancel = QPushButton("Cancel")
    btn_cancel.clicked.connect(dlg.reject)
    btn_row.addWidget(btn_cancel)

    layout.addLayout(btn_row)
    dlg.exec()


def _import_one(app, path: str) -> bool:
    ext = Path(path).suffix.lower()
    name = Path(path).name

    if ext == ".ecw":
        if not _ecw_driver_available():
            _show_ecw_dialog(app, path)
            return False
        return _import_raster_layer(app, path, name)

    if ext in (".tif", ".tiff"):
        return _import_raster_layer(app, path, name)
    if ext == ".shp":
        return _import_vector_layer(app, path, name, fmt="shp")
    if ext in (".geojson", ".json"):
        return _import_vector_layer(app, path, name, fmt="geojson")
    if ext == ".gdb":
        return _import_gdb(app, path)

    print(f"   ⚠️ Unsupported overlay type: {ext}")
    return False


def _import_gdb(app, path: str) -> bool:
    """Open the GDB layer picker, then import selected layers into the panel."""
    try:
        from gui.gis.gdb.gdb_picker import pick_and_import_gdb
        return pick_and_import_gdb(app, path)
    except Exception as exc:
        print(f"   ❌ GDB import failed: {exc}")
        import traceback
        traceback.print_exc()
        return False


def _import_raster_layer(app, path: str, name: str, world_bounds=None,
                         gcp_corners=None) -> bool:
    """Import a GeoTIFF and register the new texture actor(s) as one layer."""
    from gui.vector_export import import_geotiff_as_texture

    before = list(getattr(app, "geotiff_actors", []) or [])
    ok = import_geotiff_as_texture(app, path, world_bounds=world_bounds,
                                   gcp_corners=gcp_corners)
    if not ok:
        return False
    after = list(getattr(app, "geotiff_actors", []) or [])
    new_actors = [a for a in after if a not in before]
    if not new_actors:
        # Fallback: assume the last actor is the one we just added.
        new_actors = after[-1:] if after else []
    entry = register_gis_layer(app, name, path, "raster", "tif", new_actors)
    if entry is not None:
        # How the importer placed it: georef/worldfile/gcp/fit/pixel.
        # "pixel" means it landed at raw pixel coords (no data to fit to yet) and
        # must be re-fitted once vectors/point-cloud exist — otherwise it sits
        # ~hundreds of km from the real data and looks "not visible".
        entry["_placement"] = getattr(app, "_last_raster_placement", "fit")
    return True


def _refit_pixel_rasters(app):
    """
    Re-fit any raster that was stranded at raw pixel coordinates (imported
    before any positioned data existed) now that scene data is available.
    Reimports it so the importer's fit-to-extent kicks in. Runs at most once
    per raster (placement flips to 'fit').
    """
    from gui.vector_export import _infer_scene_xy_bounds
    if _infer_scene_xy_bounds(app) is None:
        return  # still nothing to fit to
    for e in list(_registry(app)):
        if e.get("kind") == "raster" and e.get("_placement") == "pixel":
            path, name = e.get("path"), e.get("name", "raster")
            if not path:
                continue
            print(f"   🔄 Re-fitting stranded raster to data extent: {name}")
            _remove_layer(app, e)
            _import_raster_layer(app, path, name)


def georeference_raster_layer(app, entry: dict):
    """
    Re-place a raster layer at user-specified world bounds (manual
    georeferencing), or snapped to another layer's extent. Reimports the
    GeoTIFF at the chosen bounds so it lines up with the rest of the data.
    """
    if entry is None or entry.get("kind") != "raster":
        return
    path = entry.get("path")
    if not path:
        return

    cur = _layer_bounds(entry)            # (xmin,xmax,ymin,ymax,zmin,zmax) or None
    prefill = (cur[0], cur[2], cur[1], cur[3]) if cur else (0.0, 0.0, 0.0, 0.0)

    bounds = _ask_georeference(app, entry, prefill)
    if bounds is None:
        return

    name = entry.get("name", "raster")
    _remove_layer(app, entry)
    _import_raster_layer(app, path, name, world_bounds=bounds)

    dock = getattr(app, _PANEL_ATTR, None)
    if dock is not None:
        try:
            dock.refresh()
        except Exception:
            pass


def _ask_georeference(app, entry, prefill):
    """
    Dialog returning (west, south, east, north) or None.
    Offers manual entry and a 'match to another layer's extent' helper.
    """
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QFormLayout, QLineEdit, QComboBox, QLabel,
        QDialogButtonBox, QHBoxLayout, QPushButton, QFileDialog, QMessageBox,
    )
    from PySide6.QtGui import QDoubleValidator
    from pathlib import Path
    try:
        from gui.theme_manager import get_dialog_stylesheet
    except Exception:
        def get_dialog_stylesheet():
            return ""

    w0, s0, e0, n0 = prefill
    dlg = QDialog(app)
    dlg.setWindowTitle(f"Georeference — {entry.get('name', '')}")
    dlg.setModal(True)
    dlg.setStyleSheet(get_dialog_stylesheet())
    v = QVBoxLayout(dlg)

    info = QLabel("Place this image at its true coordinates so it lines up with "
                  "your data.\n• Best: load a world file (.tfw) from the survey.\n"
                  "• Or match another layer's extent, or type the corners below.")
    info.setWordWrap(True)
    v.addWidget(info)

    # World-file loader (the correct, GCP-derived georeference)
    wf_row = QHBoxLayout()
    btn_wf = QPushButton("📄 Load world file (.tfw)…")
    wf_row.addWidget(btn_wf)
    v.addLayout(wf_row)

    # Match-to-layer helper
    match_row = QHBoxLayout()
    match_row.addWidget(QLabel("Match extent to:"))
    combo = QComboBox()
    combo.addItem("— choose a layer —", None)
    for e in _registry(app):
        if e["id"] != entry["id"]:
            combo.addItem(e["name"], e["id"])
    match_row.addWidget(combo, 1)
    v.addLayout(match_row)

    form = QFormLayout()
    fields = {}
    validator = QDoubleValidator()
    for key, label, val in (("w", "West (min X)", w0), ("south", "South (min Y)", s0),
                            ("e", "East (max X)", e0), ("n", "North (max Y)", n0)):
        le = QLineEdit(f"{val:.4f}")
        le.setValidator(validator)
        form.addRow(label, le)
        fields[key] = le
    v.addLayout(form)

    def _on_match(idx):
        lid = combo.itemData(idx)
        if lid is None:
            return
        for e in _registry(app):
            if e["id"] == lid:
                b = _layer_bounds(e)
                if b:
                    fields["w"].setText(f"{b[0]:.4f}")
                    fields["e"].setText(f"{b[1]:.4f}")
                    fields["south"].setText(f"{b[2]:.4f}")
                    fields["n"].setText(f"{b[3]:.4f}")
                break
    combo.currentIndexChanged.connect(_on_match)

    def _load_world_file():
        start = str(Path(entry.get("path", "")).parent or Path.home())
        wf, _ = QFileDialog.getOpenFileName(
            dlg, "Select world file", start,
            "World files (*.tfw *.tifw *.wld *.jgw *.pgw);;All files (*.*)")
        if not wf:
            return
        # Need the raster pixel size to turn the world file into bounds.
        width = height = None
        try:
            import rasterio
            with rasterio.open(entry.get("path")) as src:
                width, height = src.width, src.height
        except Exception:
            pass
        if not width or not height:
            QMessageBox.warning(dlg, "World file",
                                "Could not read the image size to apply the world file.")
            return
        try:
            from gui.vector_export import read_world_file_bounds
            # Parse the chosen world file directly by faking the path's sidecar:
            # read_world_file_bounds derives candidates from the image path, so
            # parse the selected file here instead.
            nums = []
            for line in Path(wf).read_text().splitlines():
                line = line.strip()
                if line:
                    nums.append(float(line))
                if len(nums) >= 6:
                    break
            if len(nums) < 6:
                raise ValueError("world file needs 6 numbers")
            A, D, B, E, C, F = nums[:6]
            left = C - A / 2.0
            top = F - E / 2.0
            right = left + A * float(width)
            bottom = top + E * float(height)
            fields["w"].setText(f"{min(left, right):.4f}")
            fields["e"].setText(f"{max(left, right):.4f}")
            fields["south"].setText(f"{min(bottom, top):.4f}")
            fields["n"].setText(f"{max(bottom, top):.4f}")
            if abs(D) > 1e-9 or abs(B) > 1e-9:
                QMessageBox.information(
                    dlg, "World file",
                    "This world file contains rotation; only the axis-aligned "
                    "extent is applied (no rotation).")
        except Exception as exc:
            QMessageBox.warning(dlg, "World file", f"Could not read world file:\n{exc}")
    btn_wf.clicked.connect(_load_world_file)

    btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    btns.accepted.connect(dlg.accept)
    btns.rejected.connect(dlg.reject)
    v.addWidget(btns)

    if dlg.exec() != QDialog.Accepted:
        return None
    try:
        w = float(fields["w"].text()); s = float(fields["south"].text())
        e = float(fields["e"].text()); n = float(fields["n"].text())
    except (ValueError, TypeError):
        return None
    if e == w or n == s:
        return None
    return (w, s, e, n)


def _import_vector_layer(app, path: str, name: str, fmt: str) -> bool:
    """
    Import a shapefile/GeoJSON via the digitizer, then group the newly created
    drawing actors into a single managed layer.
    """
    dz = getattr(app, "digitizer", None)
    drawings = getattr(dz, "drawings", None) if dz is not None else None
    before_ids = set(id(d.get("actor")) for d in drawings) if isinstance(drawings, list) else set()

    if fmt == "shp":
        from gui.vector_export import import_drawings_from_shapefile_with_rendering
        ok = import_drawings_from_shapefile_with_rendering(app, path)
    else:
        ok = _import_geojson_via_digitizer(app, path)

    if not ok:
        return False

    after = getattr(dz, "drawings", None) if dz is not None else None
    new_actors = []
    new_drawings = []
    if isinstance(after, list):
        for d in after:
            a = d.get("actor")
            if a is not None and id(a) not in before_ids:
                new_actors.append(a)
                new_drawings.append(d)

    if not new_actors:
        print(f"   ⚠️ {name}: imported but no new actors were captured")
        return False

    entry = register_gis_layer(app, name, path, "vector", fmt, new_actors)

    # Record geometry kind + colour so the panel can show a QGIS-style symbol.
    if entry is not None:
        _point_types = {"circle", "text"}
        _line_types = {"smartline", "line", "line_segment", "polyline", "freehand"}
        counts = {"point": 0, "line": 0, "polygon": 0}
        for d in new_drawings:
            t = str(d.get("type", "")).lower()
            if t in _point_types:
                counts["point"] += 1
            elif t in _line_types:
                counts["line"] += 1
            else:
                counts["polygon"] += 1
        entry["geom"] = max(counts, key=counts.get) if any(counts.values()) else "polygon"
        # Colour from the first actor's line/fill colour (0..1 → hex).
        try:
            c = new_actors[0].GetProperty().GetColor()
            is_default = (c[0] > 0.99 and c[1] > 0.99 and c[2] > 0.99) or (c[0] > 0.99 and c[1] > 0.99 and c[2] < 0.01)
            
            # Retrieve a distinct random color for the layer
            used_colors = set(e.get("color") for e in _registry(app) if e.get("color"))
            color_q = _default_color_for(entry.get("id", 0), used_colors)
            
            if is_default:
                vtk_color = (color_q.redF(), color_q.greenF(), color_q.blueF())
                # Update VTK actors to match this new layer color
                for a in new_actors:
                    try:
                        a.GetProperty().SetColor(*vtk_color)
                        # If it is a polygon, set edge color too
                        if hasattr(a.GetProperty(), "GetEdgeVisibility") and a.GetProperty().GetEdgeVisibility():
                            a.GetProperty().SetEdgeColor(vtk_color[0] * 0.6, vtk_color[1] * 0.6, vtk_color[2] * 0.6)
                    except Exception:
                        pass
                # Update the digitizer drawings list as well, so that line editing / saves keep the correct color
                for d in new_drawings:
                    d["color"] = (color_q.red(), color_q.green(), color_q.blue())
                    if "original_color" in d:
                        d["original_color"] = vtk_color
                
                entry["color"] = color_q.name()
            else:
                entry["color"] = "#%02x%02x%02x" % (
                    int(c[0] * 255), int(c[1] * 255), int(c[2] * 255))
        except Exception:
            entry["color"] = "#ffd43b"

    # A raster imported before this vector may be stranded at pixel coords —
    # now that positioned vector data exists, pull it onto the data extent.
    _refit_pixel_rasters(app)
    return True


def _import_geojson_via_digitizer(app, path: str) -> bool:
    """
    Read a GeoJSON with geopandas and push each feature through the digitizer,
    mirroring import_drawings_from_shapefile_with_rendering's geometry handling.
    """
    try:
        import geopandas as gpd
    except ImportError:
        print("   ⚠️ geopandas not installed — cannot import GeoJSON")
        return False

    from gui.vector_export import _coords_to_scene_z, _infer_import_scene_z

    try:
        gdf = gpd.read_file(path)
    except Exception as exc:
        print(f"   ❌ Could not read GeoJSON: {exc}")
        return False

    # Establish/confirm the ONE authoritative canvas CRS from this GeoJSON's
    # own CRS (first trustworthy dataset wins), then reproject into whatever
    # canvas CRS is authoritative. Reading app.project_crs_epsg directly (the
    # old behaviour) missed the case where THIS import is the first
    # georeferenced dataset: the canvas CRS would never get established, and
    # every later SNT/LAZ/basemap load would have nothing to align against.
    target_crs = None
    try:
        from gui.crs_manager import ensure_canvas_crs, get_canvas_crs
        if gdf.crs is not None:
            ensure_canvas_crs(app, gdf.crs, source="GeoJSON CRS", dataset=path)
        target_crs = get_canvas_crs(app)
    except Exception as e:
        print(f"   ⚠️ canvas CRS unavailable: {e}")
    if target_crs is not None and gdf.crs is not None:
        try:
            if not gdf.crs.equals(target_crs):
                print(f"   🔄 Reprojecting GeoJSON from {gdf.crs} to canvas CRS {target_crs}")
                gdf = gdf.to_crs(target_crs)
        except Exception as e:
            print(f"   ⚠️ Could not reproject GeoJSON: {e}")

    scene_z = _infer_import_scene_z(app)
    dz = getattr(app, "digitizer", None)
    if dz is None:
        print("   ❌ Digitizer is not initialized — cannot import GeoJSON")
        return False
    imported = 0

    def _push(coords, dtype, color=(255, 255, 0)):
        nonlocal imported
        data = {"type": dtype, "coordinates": coords, "color": color}
        if dz.add_drawing_from_data(data):
            imported += 1

    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        gt = geom.geom_type
        try:
            if gt == "Point":
                has_z = getattr(geom, "has_z", False)
                raw = [(geom.x, geom.y, geom.z)] if has_z else [(geom.x, geom.y)]
                _push(_coords_to_scene_z(raw, scene_z), "circle")
            elif gt == "LineString":
                _push(_coords_to_scene_z(list(geom.coords), scene_z), "smartline")
            elif gt == "Polygon":
                _push(_coords_to_scene_z(list(geom.exterior.coords), scene_z), "polygon")
            elif gt in ("MultiLineString", "MultiPolygon", "MultiPoint"):
                for part in geom.geoms:
                    if part.is_empty:
                        continue
                    if part.geom_type == "LineString":
                        _push(_coords_to_scene_z(list(part.coords), scene_z), "smartline")
                    elif part.geom_type == "Polygon":
                        _push(_coords_to_scene_z(list(part.exterior.coords), scene_z), "polygon")
                    elif part.geom_type == "Point":
                        has_z = getattr(part, "has_z", False)
                        raw = [(part.x, part.y, part.z)] if has_z else [(part.x, part.y)]
                        _push(_coords_to_scene_z(raw, scene_z), "circle")
            else:
                print(f"   ⚠️ Unsupported geometry: {gt}")
        except Exception as exc:
            print(f"   ⚠️ Feature skipped ({gt}): {exc}")

    print(f"   ✅ GeoJSON: imported {imported} feature(s)")
    return imported > 0


# ─────────────────────────────────────────────────────────────────────────────
# Attribute Table Dialog
# ─────────────────────────────────────────────────────────────────────────────
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QTableWidget, QTableWidgetItem, QAbstractItemView, QHeaderView,
    QPushButton
)
from PySide6.QtCore import Qt

class AttributeTableDialog(QDialog):
    def __init__(self, parent, entry, sub_code=None):
        super().__init__(parent)
        self.entry = entry
        self.sub_code = sub_code
        self.layer_name = entry.get("name", "Layer")
        
        # Get subtype label if sub_code is set
        self.sub_label = None
        if sub_code and "sub_layers" in entry:
            self.sub_label = entry["sub_layers"].get(sub_code, {}).get("label")
            
        if self.sub_label:
            self.setWindowTitle(f"Attribute Table — {self.layer_name} ({self.sub_label})")
        else:
            self.setWindowTitle(f"Attribute Table — {self.layer_name}")
            
        self.setMinimumSize(750, 500)
        self.setWindowFlags(self.windowFlags() | Qt.WindowMinMaxButtonsHint | Qt.Tool)
        
        # Load theme stylesheet
        try:
            from gui.theme_manager import get_dialog_stylesheet
            self.setStyleSheet(get_dialog_stylesheet())
        except Exception:
            pass
            
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        
        # Header layout
        header_layout = QHBoxLayout()
        lbl_text = f"Attribute Table: {self.layer_name}"
        if self.sub_label:
            lbl_text += f" ({self.sub_label})"
        title_lbl = QLabel(lbl_text)
        title_lbl.setObjectName("dialogTitle")
        # Keep title clean and same size as dialogSubtitle/caption
        title_lbl.setStyleSheet("font-size: 11pt; font-weight: bold; margin: 2px 0px;")
        header_layout.addWidget(title_lbl)
        header_layout.addStretch()
        layout.addLayout(header_layout)
        
        # Search and Export bar
        search_layout = QHBoxLayout()
        search_layout.setSpacing(6)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search attributes...")
        self.search_box.setClearButtonEnabled(True)
        try:
            from gui.gis.icons import icon as gicon
            from gui.theme_manager import ThemeColors
            cols = {
                "muted": ThemeColors.get("text_muted") or "#888888"
            }
            self.search_box.addAction(gicon("search", cols["muted"], 14), QLineEdit.LeadingPosition)
        except Exception:
            pass
        self.search_box.setStyleSheet("""
            QLineEdit {
                min-height: 24px;
                max-height: 24px;
                padding: 1px 4px 1px 22px;
                font-size: 9.5pt;
                border-radius: 4px;
            }
        """)
        search_layout.addWidget(self.search_box)
        
        self.export_btn = QPushButton("Export CSV")
        self.export_btn.setToolTip("Export attribute table to CSV")
        self.export_btn.setCursor(Qt.PointingHandCursor)
        self.export_btn.setStyleSheet("""
            QPushButton {
                min-height: 24px;
                max-height: 24px;
                padding: 1px 12px;
                font-size: 9.5pt;
                border-radius: 4px;
                font-weight: normal;
            }
        """)
        search_layout.addWidget(self.export_btn)
        
        layout.addLayout(search_layout)
        
        # Table Grid
        self.table = QTableWidget()
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        
        # Compact Spreadsheet style
        try:
            from gui.theme_manager import ThemeColors
            bg_sec = ThemeColors.get("bg_secondary") or "#ffffff"
            bg_input = ThemeColors.get("bg_input") or "#f7f8fa"
            text_prim = ThemeColors.get("text_primary") or "#1b2838"
            border_col = ThemeColors.get("border") or "#d0d5dd"
        except Exception:
            bg_sec, bg_input, text_prim, border_col = "#ffffff", "#f7f8fa", "#1b2838", "#d0d5dd"
            
        self.table.setStyleSheet(f"""
            QTableWidget {{
                background-color: {bg_sec};
                alternate-background-color: {bg_input};
                color: {text_prim};
                gridline-color: {border_col};
                font-size: 9pt;
                border: 1px solid {border_col};
                border-radius: 4px;
            }}
            QTableWidget::item {{
                padding: 1px 4px;
            }}
            QHeaderView::section {{
                background-color: {bg_input};
                color: {text_prim};
                border: 1px solid {border_col};
                padding: 2px 4px;
                font-size: 9pt;
                font-weight: bold;
            }}
        """)
        layout.addWidget(self.table)
        
        # Status Label
        self.status_lbl = QLabel("Loading attributes...")
        self.status_lbl.setStyleSheet("font-size: 9pt; color: gray; padding: 2px 0px;")
        layout.addWidget(self.status_lbl)
        
        self.headers = []
        self.all_rows = []
        
        # Wire events
        self.search_box.textChanged.connect(self.filter_table)
        self.export_btn.clicked.connect(self.export_to_csv)
        
        # Load and populate data
        if not self.load_data():
            self.status_lbl.setText("Failed to load attributes or layer is empty.")
            self.table.setEnabled(False)
            self.search_box.setEnabled(False)
            self.export_btn.setEnabled(False)
        else:
            self.populate_table()

    def load_data(self) -> bool:
        from osgeo import ogr
        path = self.entry.get("path")
        if not path:
            return False
            
        try:
            ds = ogr.Open(path, 0)
        except Exception as e:
            print(f"Error opening OGR datasource at {path}: {e}")
            return False
            
        if ds is None:
            return False
            
        lyr = None
        gdb_layer_name = self.entry.get("gdb_layer_name")
        if gdb_layer_name:
            try:
                lyr = ds.GetLayerByName(gdb_layer_name)
            except Exception:
                pass
            if lyr is None:
                for i in range(ds.GetLayerCount()):
                    cand = ds.GetLayerByIndex(i)
                    if cand and cand.GetName().lower() == gdb_layer_name.lower():
                        lyr = cand
                        break
        else:
            try:
                lyr = ds.GetLayer(0)
            except Exception:
                pass
                
        if lyr is None:
            ds = None
            return False
            
        try:
            # Get subtype_field and GDB engine
            subtype_field = None
            engine = None
            gdb_path = self.entry.get("gdb_path")
            if self.sub_code:
                if gdb_path and gdb_layer_name:
                    try:
                        from gui.gis.gdb.engine import get_engine
                        engine = get_engine(gdb_path)
                        cascade = engine.get_cascade_data(gdb_layer_name) if engine else None
                        if cascade and cascade.get("subtype_field"):
                            subtype_field = cascade["subtype_field"]
                    except Exception as e:
                        print(f"Error getting subtype field for attribute table: {e}")
            elif gdb_path and gdb_layer_name:
                try:
                    from gui.gis.gdb.engine import get_engine
                    engine = get_engine(gdb_path)
                except Exception:
                    pass

            # Fields introspection
            defn = lyr.GetLayerDefn()
            field_names = []
            if defn:
                for i in range(defn.GetFieldCount()):
                    field_names.append(defn.GetFieldDefn(i).GetName())
                    
            # Retrieve domain mapping from GDB Engine
            field_domain_maps = {}
            if gdb_path and gdb_layer_name and engine:
                try:
                    layer_key = engine._layer_key(gdb_layer_name)
                    field_domains = engine._field_domains.get(layer_key, {})
                    for fname in field_names:
                        dname = field_domains.get(fname)
                        if dname and dname in engine.all_domains:
                            field_domain_maps[fname] = engine.all_domains[dname]
                except Exception as e:
                    print(f"Error reading field domain mapping: {e}")

            self.headers = ["FID"] + field_names
            self.table.setColumnCount(len(self.headers))
            self.table.setHorizontalHeaderLabels(self.headers)
            self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
            self.table.horizontalHeader().setStretchLastSection(False)
            self.table.verticalHeader().setDefaultSectionSize(20)
            self.table.verticalHeader().setMinimumSectionSize(16)
            
            # Apply server-side OGR attribute filter if subtype is selected & not default
            if self.sub_code and subtype_field and self.sub_code != "default":
                try:
                    try:
                        int_code = int(self.sub_code)
                        filter_str = f"{subtype_field} = {int_code}"
                    except ValueError:
                        filter_str = f"{subtype_field} = '{self.sub_code}'"
                    lyr.SetAttributeFilter(filter_str)
                except Exception as e:
                    print(f"Error applying OGR attribute filter: {e}")

            # Extract features
            lyr.ResetReading()
            feat = lyr.GetNextFeature()
            self.all_rows = []
            while feat is not None:
                # Fallback subclass filtering (in case driver filter is partially supported)
                if self.sub_code and subtype_field:
                    try:
                        val = feat.GetField(subtype_field)
                        feat_sub_code = str(val).strip() if val is not None else "default"
                        try:
                            feat_sub_code = str(int(float(feat_sub_code)))
                        except Exception:
                            pass
                    except Exception:
                        feat_sub_code = "default"
                    
                    sub_layers = self.entry.get("sub_layers", {})
                    if feat_sub_code not in sub_layers:
                        feat_sub_code = "default"
                        
                    if feat_sub_code != self.sub_code:
                        feat = lyr.GetNextFeature()
                        continue

                fid = feat.GetFID()
                row = [str(fid)]
                for fname in field_names:
                    val = feat.GetField(fname)
                    if val is None:
                        row.append("")
                    else:
                        sval = str(val).strip()
                        # Strip leading colons or semicolons
                        if sval.startswith(":") or sval.startswith(";"):
                            sval = sval[1:].lstrip()
                        
                        # Decoded domain matching
                        if fname in field_domain_maps:
                            domain_map = field_domain_maps[fname]
                            decoded_val = domain_map.get(sval)
                            if decoded_val is None:
                                try:
                                    int_val = int(float(sval))
                                    decoded_val = domain_map.get(int_val) or domain_map.get(str(int_val))
                                except Exception:
                                    pass
                            if decoded_val is not None:
                                sval = f"{sval} ({decoded_val})"
                                
                        row.append(sval)
                self.all_rows.append(row)
                feat = lyr.GetNextFeature()
        finally:
            try:
                lyr.SetAttributeFilter(None)
            except Exception:
                pass
            ds = None
            import gc
            gc.collect()
            
        return True

    def populate_table(self):
        self.filter_table(self.search_box.text())

    def filter_table(self, query: str):
        query = query.strip().lower()
        filtered_rows = []
        for row in self.all_rows:
            if not query:
                filtered_rows.append(row)
            else:
                match = False
                for val in row:
                    if query in val.lower():
                        match = True
                        break
                if match:
                    filtered_rows.append(row)
                    
        # Performance protection threshold
        MAX_ROWS = 2000
        shown_count = min(len(filtered_rows), MAX_ROWS)
        
        self.table.setRowCount(shown_count)
        for r in range(shown_count):
            row_vals = filtered_rows[r]
            for col, val in enumerate(row_vals):
                item = QTableWidgetItem(val)
                if len(val) > 30:
                    item.setToolTip(val)
                self.table.setItem(r, col, item)
                
        total = len(self.all_rows)
        filtered = len(filtered_rows)
        if filtered > MAX_ROWS:
            self.status_lbl.setText(f"Showing first {MAX_ROWS} of {filtered} matching features (Total: {total})")
        elif query:
            self.status_lbl.setText(f"Showing {filtered} of {total} features (Filter: '{query}')")
        else:
            self.status_lbl.setText(f"Total features: {total}")
            
        self.table.resizeColumnsToContents()

    def export_to_csv(self):
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        import csv
        
        if not self.all_rows:
            QMessageBox.information(self, "Export CSV", "No attributes to export.")
            return
            
        default_name = f"{self.layer_name}_attributes.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Attribute Table to CSV", default_name, "CSV Files (*.csv)"
        )
        if not path:
            return
            
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(self.headers)
                writer.writerows(self.all_rows)
            QMessageBox.information(self, "Export CSV", f"Successfully exported attributes to:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Export CSV", f"Failed to export CSV:\n{e}")
