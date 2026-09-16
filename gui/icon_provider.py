"""
gui/icon_provider.py
────────────────────
Centralized SVG icon provider for NakshaAI-Lidar ribbon toolbar.
All icons are embedded as SVG strings for zero-dependency, theme-aware rendering.
"""

from PySide6.QtGui import QIcon, QPixmap, QPainter
from PySide6.QtCore import QByteArray, QSize, Qt, QRectF
from PySide6.QtSvg import QSvgRenderer
from pathlib import Path
import re


_ICON_DIR = Path(__file__).resolve().parent / "icons"
_BUILTIN_SVG_SCALE = 0.92
_EXTERNAL_SVG_SCALE = 1.12
_LARGE_CANVAS_EXTERNAL_SVGS = {
    "dwg.svg",
    "dxf.svg",
    "snt.svg",
    "prj.svg",
}


def _normalize_lookup_text(value: str | None) -> str:
    """Normalize ribbon labels for robust icon lookup."""
    if value is None:
        return ""

    value = (
        value.replace("\n", " ")
        .replace("\u2026", "...")
        .replace("\u00e2\u20ac\u00a6", "...")
        .replace("\u00c3\u00a2\u201a\u00ac\u00a6", "...")
    )
    value = re.sub(r"\s+", " ", value)
    return value.strip().lower()


def _resolve_icon_color(color: str | None) -> str:
    """Resolve the theme-aware icon color when none is provided."""
    if color:
        return color

    from gui.theme_manager import ThemeColors
    return ThemeColors.get("icon_primary")


def _replace_svg_color_attributes(svg_text: str, color: str) -> str:
    """
    Force external SVGs to follow the requested icon color.
    Replaces currentColor, stroke/fill hex/rgb values, but preserves none/transparent.
    """
    svg_text = re.sub(r"currentColor", color, svg_text, flags=re.IGNORECASE)

    def _replace_attr(match):
        attr = match.group(1)
        quote = match.group(2)
        value = match.group(3).strip()

        if value.lower() in {"none", "transparent"}:
            return match.group(0)

        if value.lower().startswith("url("):
            return match.group(0)

        return f'{attr}={quote}{color}{quote}'

    svg_text = re.sub(
        r'(fill|stroke)\s*=\s*(["\'])(.*?)\2',
        _replace_attr,
        svg_text,
        flags=re.IGNORECASE,
    )

    return svg_text


def _apply_svg_current_color(svg_data: bytes, color: str) -> bytes:
    """Replace SVG color tokens with a concrete theme color."""
    try:
        svg_text = svg_data.decode("utf-8")
    except UnicodeDecodeError:
        return svg_data

    svg_text = _replace_svg_color_attributes(svg_text, color)
    return svg_text.encode("utf-8")


def _resolve_ribbon_icon_color(
    button_text: str | None = None,
    section_title: str | None = None,
    ribbon_scope: str | None = None,
    icon_name: str | None = None,
) -> str:
    """
    Resolve icon color using full ribbon context first:
    1) ribbon_scope + section_title + button_text
    2) button_text only
    3) fallback to icon_primary
    """
    from gui.theme_manager import ThemeColors

    normalized_scope = _normalize_lookup_text(ribbon_scope)
    normalized_section = _normalize_lookup_text(section_title)
    normalized_button = _normalize_lookup_text(button_text)

    contextual_map = {
        # File
        ("fileribbon", "file", "open"): "icon_open",
        ("fileribbon", "file", "save"): "icon_save",
        ("fileribbon", "file", "save as..."): "icon_save_as",
        ("fileribbon", "vectors", "export"): "icon_export",
        ("fileribbon", "vectors", "import"): "icon_import",

        # Edit - elevation products
        ("editribbon", "surface models", "create dtm"): "icon_ground",
        ("editribbon", "surface models", "create dsm"): "icon_surface",
        ("editribbon", "surface models", "export dtm"): "icon_export",
        ("editribbon", "surface models", "export dsm"): "icon_export",

        # Attachments - short names + old names
        ("fileribbon", "attachments", "dxf"): "icon_attach_dxf",
        ("fileribbon", "attachments", "attach dxf"): "icon_attach_dxf",

        ("fileribbon", "attachments", "prj"): "icon_prj_loader",
        ("fileribbon", "attachments", "prj loader"): "icon_prj_loader",
        ("fileribbon", "attachments", "define"): "icon_prj_loader",

        ("fileribbon", "attachments", "verify"): "icon_verify_labels",
        ("fileribbon", "attachments", "verify labels"): "icon_verify_labels",

        ("fileribbon", "attachments", "dwg"): "icon_attach_dwg",
        ("fileribbon", "attachments", "attach dwg"): "icon_attach_dwg",

        ("fileribbon", "attachments", "snt"): "icon_attach_snt",
        ("fileribbon", "attachments", "attach snt"): "icon_attach_snt",

        ("fileribbon", "project", "clear"): "icon_clear",
        ("fileribbon", "project", "clear data"): "icon_clear_data",

        # Edit
        ("editribbon", "history", "undo"): "icon_undo",
        ("editribbon", "history", "redo"): "icon_redo",
        ("editribbon", "clipboard", "cut"): "icon_cut",
        ("editribbon", "clipboard", "copy"): "icon_copy",
        ("editribbon", "clipboard", "paste"): "icon_paste",
        ("editribbon", "delete", "delete"): "icon_delete",

        # View
        ("viewribbon", "views", "top"): "icon_top",
        ("viewribbon", "views", "front"): "icon_front",
        ("viewribbon", "views", "side"): "icon_side",
        ("viewribbon", "views", "3d"): "icon_3d",
        ("viewribbon", "display", "depth"): "icon_depth",
        ("viewribbon", "display", "rgb"): "icon_rgb",
        ("viewribbon", "display", "intensity"): "icon_intensity",
        ("viewribbon", "display", "elevation"): "icon_elevation",
        ("viewribbon", "display", "class"): "icon_class",
        ("viewribbon", "display", "shading"): "icon_shading",
        ("viewribbon", "navigate", "fit"): "icon_fit",

        # Tools
        ("toolsribbon", "sections", "cross"): "icon_cross",
        ("toolsribbon", "sections", "cut section"): "icon_cut_section",
        ("toolsribbon", "sections", "width"): "icon_width",
        ("toolsribbon", "sync", "views"): "icon_views",
        ("toolsribbon", "selection", "config"): "icon_config",
        ("toolsribbon", "settings", "backup"): "icon_backup",
        ("toolsribbon", "settings", "preference"): "icon_preferences",
        ("toolsribbon", "settings", "preferences"): "icon_preferences",

        # Classify
        ("classifyribbon", "lines", "above"): "icon_above",
        ("classifyribbon", "lines", "below"): "icon_below",
        ("classifyribbon", "shapes", "rect"): "icon_classify_rect",
        ("classifyribbon", "shapes", "circle"): "icon_classify_circle",
        ("classifyribbon", "shapes", "polygon"): "icon_classify_free",
        ("classifyribbon", "shapes", "free"): "icon_classify_free",
        ("classifyribbon", "points", "brush"): "icon_brush",
        ("classifyribbon", "points", "point"): "icon_point",

        # Display
        ("displayribbon", "config", "display"): "icon_display",

        # Measure
        ("measurementribbon", "distance", "measure line"): "icon_measure_line",
        ("measurementribbon", "distance", "measure path"): "icon_measure_path",
        ("measurementribbon", "actions", "clear"): "icon_measure_clear",
        ("measurementribbon", "area", "block"): "icon_area_block",

        # Identify
        ("identifyribbon", "identify", "identify"): "icon_identify",
        ("identifyribbon", "identify", "zoom"): "icon_zoom",
        ("identifyribbon", "identify", "select"): "icon_select",

        # By Class
        ("byclassribbon", "by class", "convert"): "icon_convert",
        ("byclassribbon", "by class", "close"): "icon_close",
        ("byclassribbon", "by class", "height"): "icon_height",
        ("byclassribbon", "by class", "fence"): "icon_fence",
        ("byclassribbon", "algorithms", "low"): "icon_low_points",
        ("byclassribbon", "algorithms", "low points"): "icon_low_points",
        ("byclassribbon", "algorithms", "isolated"): "icon_isolated",
        ("byclassribbon", "algorithms", "ground"): "icon_ground",
        ("byclassribbon", "algorithms", "surface"): "icon_surface",

        # Draw
        ("drawribbon", "tools", "smart"): "icon_smart_line",
        ("drawribbon", "tools", "smart line"): "icon_smart_line",
        ("drawribbon", "tools", "line"): "icon_line",
        ("drawribbon", "tools", "rect"): "icon_draw_rect",
        ("drawribbon", "tools", "circle"): "icon_draw_circle",
        ("drawribbon", "tools", "free"): "icon_draw_free",
        ("drawribbon", "tools", "text"): "icon_text",
        ("drawribbon", "tools", "move v"): "icon_move_vertex",
        ("drawribbon", "tools", "move vertex"): "icon_move_vertex",
        ("drawribbon", "tools", "vertex"): "icon_vertex",
        ("drawribbon", "tools", "grid"): "icon_grid",
        ("drawribbon", "select", "select drawing"): "icon_select_drawing",
        ("drawribbon", "select", "deselect all"): "icon_deselect_all",
        ("drawribbon", "transform", "rotate"): "icon_move_vertex",
        ("drawribbon", "transform", "mirror"): "icon_vertex",
        ("drawribbon", "parallel", "parallel"): "icon_line",
        ("drawribbon", "controls", "centerline"): "icon_line",
        ("drawribbon", "actions", "clear"): "icon_draw_clear",
        ("drawribbon", "utilities", "clear"): "icon_draw_clear",
        ("drawribbon", "settings", "draw settings"): "icon_draw_settings",
        ("drawribbon", "curve", "curve"): "icon_curve_point",
        ("drawribbon", "curve", "clear"): "icon_curve_clear",
        ("drawribbon", "curve", "settings"): "icon_draw_settings",

        # Curve
        ("curveribbon", "curve tools", "curve"): "icon_curve",
        ("curveribbon", "actions", "clear"): "icon_curve_clear",

        # AI
        ("airibbon", "ai", "start"): "icon_start",

        # Block
        ("blockribbon", "block", "create block"): "icon_create_block",
    }

    color_key = contextual_map.get((normalized_scope, normalized_section, normalized_button))
    if color_key:
        return ThemeColors.get(color_key)

    button_only_map = {
        "open": "icon_open",
        "save": "icon_save",
        "save as...": "icon_save_as",
        "save as": "icon_save_as",
        "export": "icon_export",
        "import": "icon_import",

        "dxf": "icon_attach_dxf",
        "attach dxf": "icon_attach_dxf",

        "prj": "icon_prj_loader",
        "prj loader": "icon_prj_loader",
        "define": "icon_prj_loader",

        "verify": "icon_verify_labels",
        "verify labels": "icon_verify_labels",

        "dwg": "icon_attach_dwg",
        "attach dwg": "icon_attach_dwg",

        "snt": "icon_attach_snt",
        "attach snt": "icon_attach_snt",

        "clear": "icon_clear",
        "clear data": "icon_clear_data",
        "undo": "icon_undo",
        "redo": "icon_redo",
        "cut": "icon_cut",
        "copy": "icon_copy",
        "paste": "icon_paste",
        "delete": "icon_delete",
        "top": "icon_top",
        "front": "icon_front",
        "side": "icon_side",
        "3d": "icon_3d",
        "depth": "icon_depth",
        "rgb": "icon_rgb",
        "intensity": "icon_intensity",
        "elevation": "icon_elevation",
        "class": "icon_class",
        "shading": "icon_shading",
        "fit": "icon_fit",
        "cross": "icon_cross",
        "cut section": "icon_cut_section",
        "width": "icon_width",
        "views": "icon_views",
        "config": "icon_config",
        "backup": "icon_backup",
        "preference": "icon_preferences",
        "preferences": "icon_preferences",
        "above": "icon_above",
        "below": "icon_below",
        "brush": "icon_brush",
        "point": "icon_point",
        "display": "icon_display",
        "measure line": "icon_measure_line",
        "measure path": "icon_measure_path",
        "block": "icon_area_block",
        "identify": "icon_identify",
        "zoom": "icon_zoom",
        "select": "icon_select",
        "convert": "icon_convert",
        "close": "icon_close",
        "height": "icon_height",
        "fence": "icon_fence",
        "low": "icon_low_points",
        "low points": "icon_low_points",
        "isolated": "icon_isolated",
        "ground": "icon_ground",
        "surface": "icon_surface",
        "smart": "icon_smart_line",
        "smart line": "icon_smart_line",
        "line": "icon_line",
        "ortho": "icon_draw_rect",
        "text": "icon_text",
        "move v": "icon_move_vertex",
        "move vertex": "icon_move_vertex",
        "vertex": "icon_vertex",
        "grid": "icon_grid",
        "select drawing": "icon_select_drawing",
        "deselect all": "icon_deselect_all",
        "rotate": "icon_move_vertex",
        "mirror": "icon_vertex",
        "draw settings": "icon_draw_settings",
        "parallel": "icon_line",
        "centerline": "icon_line",
        "curve": "icon_curve",
        "start": "icon_start",
        "create block": "icon_create_block",
    }

    color_key = button_only_map.get(normalized_button)
    if color_key:
        return ThemeColors.get(color_key)

    return ThemeColors.get("icon_primary")


# ═══════════════════════════════════════════════════════════════════════════
#  SVG ICON DEFINITIONS  (24x24 viewBox, stroke-based)
# ═══════════════════════════════════════════════════════════════════════════

_SVG_HEADER = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
_SVG_FOOTER = '</svg>'

_ICON_DATA = {
    "folder_open": '<path d="M3 18V6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v2"/><path d="M3 18l2.4-7.2A2 2 0 0 1 7.3 9.5H21l-2.4 7.2a2 2 0 0 1-1.9 1.3H3z"/>',
    "save": '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/>',
    "save_as": '<path d="M17 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h9l5 5v5"/><polyline points="14 21 14 13 7 13 7 21"/><polyline points="7 3 7 8 13 8"/><path d="M18.5 15.5l3 3-3 3"/><line x1="16" y1="18.5" x2="21.5" y2="18.5"/>',
    "export": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>',
    "import": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
    "attach": '<path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>',
    "clipboard_list": '<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1"/><line x1="8" y1="12" x2="16" y2="12"/><line x1="8" y1="16" x2="13" y2="16"/>',
    "search": '<circle cx="11" cy="11" r="7"/><line x1="16.5" y1="16.5" x2="21" y2="21"/>',
    "file_dwg": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><text x="12" y="16" text-anchor="middle" font-size="8" font-weight="bold" fill="{color}" stroke="none">DWG</text>',
    "file_snt": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><text x="12" y="16" text-anchor="middle" font-size="8" font-weight="bold" fill="{color}" stroke="none">SNT</text>',
    "broom": '<path d="M12 3v9"/><path d="M8 12c0 4 1.5 9 4 9s4-5 4-9"/><line x1="7" y1="12" x2="17" y2="12"/>',
    "undo": '<polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/>',
    "redo": '<polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.13-9.36L23 10"/>',
    "cut": '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><line x1="20" y1="4" x2="8.12" y2="15.88"/><line x1="14.47" y1="14.48" x2="20" y2="20"/><line x1="8.12" y1="8.12" x2="12" y2="12"/>',
    "copy": '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "paste": '<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1"/>',
    "trash": '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/>',
    "view_top": '<rect x="4" y="8" width="16" height="12" rx="1"/><path d="M12 3v5"/><polyline points="9 5 12 8 15 5"/>',
    "view_front": '<rect x="4" y="4" width="16" height="16" rx="1"/><path d="M4 12h16"/><circle cx="12" cy="12" r="2"/>',
    "view_side": '<path d="M4 4h16v16H4z"/><path d="M4 4l6 6"/><path d="M20 4l-6 6"/><path d="M10 10h4v4h-4z"/>',
    "view_3d": '<path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/>',
    "depth": '<rect x="3" y="3" width="14" height="10" rx="1"/><rect x="7" y="7" width="14" height="10" rx="1"/><rect x="5" y="5" width="14" height="10" rx="1" opacity="0.5"/>',
    "rgb": '<circle cx="9" cy="9" r="5" opacity="0.7"/><circle cx="15" cy="9" r="5" opacity="0.7"/><circle cx="12" cy="14" r="5" opacity="0.7"/>',
    "intensity": '<circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="4"/><line x1="12" y1="20" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="6.34" y2="6.34"/><line x1="17.66" y1="17.66" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="4" y2="12"/><line x1="20" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="6.34" y2="17.66"/><line x1="17.66" y1="6.34" x2="19.78" y2="4.22"/>',
    "elevation": '<rect x="4" y="14" width="4" height="7"/><rect x="10" y="8" width="4" height="13"/><rect x="16" y="3" width="4" height="18"/>',
    "class_tag": '<path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><circle cx="7" cy="7" r="1.5" fill="{color}"/>',
    "shading": '<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" fill="{color}"/>',
    "fit_view": '<path d="M3 3h6v2H5v4H3V3z"/><path d="M21 3h-6v2h4v4h2V3z"/><path d="M3 21h6v-2H5v-4H3v6z"/><path d="M21 21h-6v-2h4v-4h2v6z"/><circle cx="12" cy="12" r="3"/>',
    "reset": '<polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.13-9.36L23 10"/>',
    "cross_section": '<line x1="3" y1="12" x2="21" y2="12"/><line x1="12" y1="3" x2="12" y2="21"/><circle cx="12" cy="12" r="2"/>',
    "cut_section": '<path d="M3 12h7"/><path d="M14 12h7"/><path d="M10 6l4 12"/><polyline points="8 4 10 6 8 8"/><polyline points="16 16 14 18 16 20"/>',
    "ruler": '<path d="M3 21L21 3"/><path d="M6 18l1-1"/><path d="M9 15l2-2"/><path d="M12 12l1-1"/><path d="M15 9l2-2"/><path d="M18 6l1-1"/>',
    "sync": '<polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/>',
    "keyboard": '<rect x="2" y="5" width="20" height="14" rx="2"/><line x1="6" y1="9" x2="6.01" y2="9"/><line x1="10" y1="9" x2="10.01" y2="9"/><line x1="14" y1="9" x2="14.01" y2="9"/><line x1="18" y1="9" x2="18.01" y2="9"/><line x1="7" y1="15" x2="17" y2="15"/>',
    "backup": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><polyline points="9 12 11 14 15 10"/>',
    "preferences": '<line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="18" x2="20" y2="18"/><circle cx="8" cy="6" r="2" fill="{color}"/><circle cx="16" cy="12" r="2" fill="{color}"/><circle cx="10" cy="18" r="2" fill="{color}"/>',
    "arrow_up": '<line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/>',
    "arrow_down": '<line x1="12" y1="5" x2="12" y2="19"/><polyline points="19 12 12 19 5 12"/>',
    "rectangle": '<rect x="4" y="4" width="16" height="16" rx="2"/>',
    "circle": '<circle cx="12" cy="12" r="9"/>',
    "freehand": '<path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/>',
    "brush": '<path d="M20 4L8.5 15.5"/><path d="M7 17c-2 2-4 2-5 1s-1-3 1-5"/><path d="M8.5 15.5l-1.5 1.5c-1 1-1 2.5 0 3.5s2.5 1 3.5 0l1.5-1.5"/><path d="M15 7l2-2"/>',
    "point_marker": '<path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7z"/><circle cx="12" cy="9" r="2.5" fill="{color}"/>',
    "measure_line": '<line x1="4" y1="20" x2="20" y2="4"/><polyline points="4 16 4 20 8 20"/><polyline points="20 8 20 4 16 4"/><line x1="10" y1="10" x2="14" y2="14" stroke-dasharray="2,2"/>',
    "measure_path": '<polyline points="4 18 8 10 14 14 20 6"/><circle cx="4" cy="18" r="1.5" fill="{color}"/><circle cx="8" cy="10" r="1.5" fill="{color}"/><circle cx="14" cy="14" r="1.5" fill="{color}"/><circle cx="20" cy="6" r="1.5" fill="{color}"/>',
    "identify": '<circle cx="12" cy="12" r="9"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>',
    "zoom_rect": '<circle cx="10" cy="10" r="6"/><line x1="14.5" y1="14.5" x2="20" y2="20"/><rect x="7" y="7" width="6" height="6" rx="0.5" stroke-dasharray="2,1"/>',
    "select_check": '<rect x="3" y="3" width="18" height="18" rx="2"/><polyline points="9 12 11 14 16 9"/>',
    "convert": '<polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/>',
    "pin_close": '<path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7z"/><circle cx="12" cy="9" r="2" fill="{color}"/>',
    "height_ruler": '<line x1="12" y1="2" x2="12" y2="22"/><polyline points="8 6 12 2 16 6"/><polyline points="8 18 12 22 16 18"/><line x1="8" y1="8" x2="12" y2="8"/><line x1="8" y1="12" x2="12" y2="12"/><line x1="8" y1="16" x2="12" y2="16"/>',
    "fence": '<path d="M12 2l8 5v10l-8 5-8-5V7z"/>',
    "isolated": '<circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r="8" stroke-dasharray="3,2"/>',
    "mountain": '<path d="M3 20L8 10l3 4 5-10 5 16H3z"/>',
    "surface": '<path d="M3 16h18"/><path d="M6 16V8"/><path d="M18 16V8"/><path d="M12 16V10"/><polyline points="9 12 12 10 15 12"/>',
    "smartline": '<path d="M15 4V2a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v2"/><path d="M3 10l3-6h12l3 6"/><line x1="12" y1="10" x2="12" y2="22"/><line x1="8" y1="10" x2="16" y2="10"/><path d="M9 22h6"/>',
    "line": '<line x1="5" y1="19" x2="19" y2="5"/><circle cx="5" cy="19" r="1.5" fill="{color}"/><circle cx="19" cy="5" r="1.5" fill="{color}"/>',
    "polyline": '<polyline points="4 18 8 8 14 14 20 6"/><circle cx="4" cy="18" r="1.5" fill="{color}"/><circle cx="8" cy="8" r="1.5" fill="{color}"/><circle cx="14" cy="14" r="1.5" fill="{color}"/><circle cx="20" cy="6" r="1.5" fill="{color}"/>',
    "text": '<polyline points="4 7 12 7 20 7"/><line x1="12" y1="7" x2="12" y2="21"/>',
    "move_vertex": '<polyline points="5 9 2 12 5 15"/><polyline points="9 5 12 2 15 5"/><polyline points="19 9 22 12 19 15"/><polyline points="9 19 12 22 15 19"/><line x1="2" y1="12" x2="22" y2="12"/><line x1="12" y1="2" x2="12" y2="22"/>',
    "vertex": '<circle cx="12" cy="12" r="3" fill="{color}"/><line x1="4" y1="18" x2="12" y2="12"/><line x1="12" y1="12" x2="20" y2="6"/>',
    "grid": '<line x1="3" y1="3" x2="3" y2="21"/><line x1="9" y1="3" x2="9" y2="21"/><line x1="15" y1="3" x2="15" y2="21"/><line x1="21" y1="3" x2="21" y2="21"/><line x1="3" y1="3" x2="21" y2="3"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="3" y1="15" x2="21" y2="15"/><line x1="3" y1="21" x2="21" y2="21"/>',
    "select_target": '<circle cx="12" cy="12" r="9"/><line x1="12" y1="3" x2="12" y2="7"/><line x1="12" y1="17" x2="12" y2="21"/><line x1="3" y1="12" x2="7" y2="12"/><line x1="17" y1="12" x2="21" y2="12"/><circle cx="12" cy="12" r="2" fill="{color}"/>',
    "deselect": '<circle cx="12" cy="12" r="9"/><line x1="8" y1="8" x2="16" y2="16"/><line x1="16" y1="8" x2="8" y2="16"/>',
    "settings_gear": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
    "curve_point": '<path d="M4 20C4 20 8 4 12 4s8 16 8 16"/><circle cx="4" cy="20" r="1.5" fill="{color}"/><circle cx="12" cy="4" r="1.5" fill="{color}"/><circle cx="20" cy="20" r="1.5" fill="{color}"/>',
    "ai_brain": '<path d="M12 2a7 7 0 0 0-7 7c0 2.5 1.3 4.7 3.3 5.9"/><path d="M12 2a7 7 0 0 1 7 7c0 2.5-1.3 4.7-3.3 5.9"/><path d="M8.7 14.9A5.5 5.5 0 0 0 12 22a5.5 5.5 0 0 0 3.3-7.1"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/>',
    "rocket": '<path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/><path d="M12 15l-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0"/><path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/>',
    "config_display": '<rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/><circle cx="12" cy="10" r="2"/><path d="M9.5 7.5l-1-1"/><path d="M14.5 7.5l1-1"/><path d="M14.5 12.5l1 1"/>',
    "fields": '<rect x="3" y="4" width="18" height="16" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="9" x2="9" y2="20"/><line x1="15" y1="9" x2="15" y2="20"/>',
    "section_width": '<path d="M4 4v16"/><path d="M20 4v16"/><line x1="4" y1="12" x2="20" y2="12"/><polyline points="7 9 4 12 7 15"/><polyline points="17 9 20 12 17 15"/>',
    "neon_block": '<rect x="5" y="5" width="14" height="14" rx="2" opacity="0.24" fill="{color}"/><rect x="4.5" y="4.5" width="15" height="15" rx="2.5" stroke-width="1.4"/><path d="M8 8h8v8H8z" stroke-width="1.4"/><path d="M4.5 9.5H2.5M4.5 14.5H2.5M19.5 9.5h2M19.5 14.5h2M9.5 4.5v-2M14.5 4.5v-2M9.5 19.5v2M14.5 19.5v2"/><circle cx="12" cy="12" r="2" fill="{color}" opacity="0.9"/><path d="M8 12h8M12 8v8" opacity="0.75"/>',
    "parallel": '<line x1="4" y1="19" x2="18" y2="5"/><line x1="8" y1="19" x2="22" y2="5"/><circle cx="4" cy="19" r="1.5" fill="{color}"/><circle cx="18" cy="5" r="1.5" fill="{color}"/><circle cx="8" cy="19" r="1.5" fill="{color}"/><circle cx="22" cy="5" r="1.5" fill="{color}"/>',
    "centerline": '<path d="M4 7c4-3 8-3 16 0"/><path d="M4 17c4-3 8-3 16 0"/><path d="M4 12c4-3 8-3 16 0" stroke-dasharray="2.4,2.4"/><circle cx="4" cy="7" r="1.2" fill="{color}"/><circle cx="4" cy="17" r="1.2" fill="{color}"/><circle cx="20" cy="7" r="1.2" fill="{color}"/><circle cx="20" cy="17" r="1.2" fill="{color}"/>',
    "rotate_transform": '<path d="M19 8a7.5 7.5 0 1 0 1.2 6.1"/><polyline points="19 3.8 19 8 14.8 8"/>',
    "mirror_transform": '<line x1="12" y1="4" x2="12" y2="20" stroke-dasharray="2.2,2.2"/><polyline points="9.2 6.5 4.5 12 9.2 17.5 9.2 6.5"/><polyline points="14.8 6.5 19.5 12 14.8 17.5 14.8 6.5"/>',
}


def _build_svg(icon_name: str, color: str = "#cccccc") -> bytes:
    """Build a complete SVG string for the given icon name and color."""
    body = _ICON_DATA.get(icon_name, "")
    header = _SVG_HEADER.replace("{color}", color)
    body = body.replace("{color}", color)
    return (header + body + _SVG_FOOTER).encode("utf-8")


def _render_svg_bytes(svg_data: bytes, size: int, scale: float = 1.0) -> QIcon:
    """Render SVG bytes into a QIcon at the requested size."""
    renderer = QSvgRenderer(QByteArray(svg_data))
    pixmap = QPixmap(QSize(size, size))
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)

    render_size = float(size) * float(scale)
    offset = (float(size) - render_size) / 2.0
    renderer.render(painter, QRectF(offset, offset, render_size, render_size))
    painter.end()

    return QIcon(pixmap)


def _load_icon_from_file(
    icon_path: Path,
    size: int,
    color: str = None,
    button_text: str = None,
    section_title: str = None,
    ribbon_scope: str = None,
) -> QIcon:
    """Load a disk-backed icon, rendering SVGs to a consistent size."""
    if not icon_path.exists():
        return QIcon()

    if color is None:
        color = _resolve_ribbon_icon_color(
            button_text=button_text,
            section_title=section_title,
            ribbon_scope=ribbon_scope,
        )

    if icon_path.suffix.lower() == ".svg":
        try:
            scale = _EXTERNAL_SVG_SCALE
            if icon_path.name in _LARGE_CANVAS_EXTERNAL_SVGS:
                scale = 1.18
            svg_data = _apply_svg_current_color(icon_path.read_bytes(), color)
            return _render_svg_bytes(svg_data, size, scale=scale)
        except Exception:
            return QIcon(str(icon_path))

    return QIcon(str(icon_path))


def get_icon(name: str, color: str = None, size: int = 24) -> QIcon:
    """
    Get a QIcon for the given icon name.

    Args:
        name:  Icon identifier (e.g. 'folder_open', 'save', 'trash')
        color: Hex color string. If None, uses theme-appropriate default.
        size:  Icon pixel size (default 24).

    Returns:
        QIcon with the rendered SVG icon.
    """
    if name not in _ICON_DATA:
        return QIcon()

    if color is None:
        color = _resolve_ribbon_icon_color(icon_name=name)

    return _render_svg_bytes(_build_svg(name, color), size, scale=_BUILTIN_SVG_SCALE)


def get_icon_pixmap(name: str, color: str = None, size: int = 24) -> QPixmap:
    """Get a QPixmap for the given icon name."""
    if name not in _ICON_DATA:
        return QPixmap()

    if color is None:
        color = _resolve_ribbon_icon_color(icon_name=name)

    svg_data = _build_svg(name, color)
    icon = _render_svg_bytes(svg_data, size, scale=_BUILTIN_SVG_SCALE)
    return icon.pixmap(size, size)


_SCOPED_EXTERNAL_RIBBON_ICON_FILES = {
    ("measurementribbon", "actions", "clear"): "clear-measurements.svg",
    ("measurementribbon", "area", "block"): "area-block.svg",
    ("drawribbon", "actions", "clear"): "clear-drawing-line.svg",
    ("drawribbon", "utilities", "clear"): "clear-drawing-line.svg",
    ("curveribbon", "actions", "clear"): "clear-curve.svg",
    ("drawribbon", "tools", "rect"): "rectangle-draw.svg",
    ("drawribbon", "tools", "circle"): "circle-draw.svg",
    ("drawribbon", "tools", "free"): "freehand-draw.svg",
    ("drawribbon", "tools", "ortho"): "ortho-polygon.svg",
    ("drawribbon", "curve", "curve"): "curve.svg",
    ("drawribbon", "curve", "clear"): "clear-curve.svg",
    ("classifyribbon", "shapes", "rect"): "rectangle-select.svg",
    ("classifyribbon", "shapes", "circle"): "circle.svg",
    ("classifyribbon", "shapes", "polygon"): "polygon.svg",
    ("classifyribbon", "shapes", "free"): "freehand.svg",
}


_EXTERNAL_RIBBON_ICON_FILES = {
    ("file", "open"): "open-file.svg",
    ("file", "save"): "save.svg",
    ("file", "save as..."): "save-as.svg",

    ("gis data", "layers"): "layers.svg",
    ("gis data", "catalog"): "catalog.svg",
    ("gis data", "import"): "import.svg",
    ("gis data", "export"): "export.svg",
    ("gis data", "convert"): "convert-card.svg",

    ("vectors", "export"): "export.svg",
    ("vectors", "import"): "import.svg",

    # Attachments - short names + old names
    ("attachments", "dxf"): "dxf.svg",
    ("attachments", "attach dxf"): "dxf.svg",

    ("attachments", "prj"): "prj.svg",
    ("attachments", "prj loader"): "prj.svg",
    ("attachments", "define"): "prj.svg",

    ("attachments", "verify"): "verify-grid-labels.svg",
    ("attachments", "verify labels"): "verify-grid-labels.svg",

    ("attachments", "dwg"): "dwg.svg",
    ("attachments", "attach dwg"): "dwg.svg",

    ("attachments", "snt"): "snt.svg",
    ("attachments", "attach snt"): "snt.svg",

    ("dxf", "attach"): "dxf.svg",
    ("dxf", "define"): "prj.svg",
    ("dxf", "verify labels"): "verify-grid-labels.svg",

    ("dwg", "attach"): "dwg.svg",
    ("snt", "attach"): "snt.svg",

    ("project", "clear"): "clear-project.svg",
    ("project", "clear data"): "clear-data.svg",
    ("history", "undo"): "undo.svg",
    ("history", "redo"): "redo.svg",
    ("clipboard", "cut"): "cut.svg",
    ("clipboard", "copy"): "copy.svg",
    ("clipboard", "paste"): "paste.svg",
    ("delete", "delete"): "delete.svg",
    ("views", "top"): "top-view.svg",
    ("views", "front"): "front-view.svg",
    ("views", "side"): "side-view.svg",
    ("views", "3d"): "3d-view.svg",
    ("display", "rgb"): "icon-view-rgb.svg",
    ("display", "intensity"): "icon-view-intensity.svg",
    ("display", "elevation"): "icon-view-elevation.svg",
    ("display", "class"): "icon-view-class.svg",
    ("display", "shading"): "icon-view-shading.svg",
    ("navigate", "fit"): "fit-view.svg",
    ("sections", "cross"): "cross-section.svg",
    ("sections", "cut section"): "cut-section.svg",
    ("sections", "width"): "width.svg",
    ("sync", "views"): "sync-view.svg",
    ("selection", "config"): "shortcut-config.svg",
    ("settings", "backup"): "backup-settings.svg",
    ("settings", "preference"): "cross-section-pref.svg",
    ("settings", "preferences"): "cross-section-pref.svg",
    ("lines", "above"): "above.svg",
    ("lines", "below"): "below.svg",
    ("points", "brush"): "brush.svg",
    ("points", "point"): "point.svg",
    ("config", "display"): "display-mode.svg",
    ("distance", "measure line"): "measure-line.svg",
    ("distance", "measure path"): "measure-path.svg",
    ("identify", "identify"): "identify-point.svg",
    ("identify", "zoom"): "zoom-rectangle.svg",
    ("identify", "select"): "select.svg",
    ("by class", "convert"): "by-class-conversion.svg",
    ("by class", "close"): "closed-feature-conversion.svg",
    ("by class", "height"): "height-conversion.svg",
    ("by class", "fence"): "fence-conversion.svg",
    ("algorithms", "low"): "low-points.svg",
    ("algorithms", "low points"): "low-points.svg",
    ("algorithms", "isolated"): "isolated-points.svg",
    ("algorithms", "ground"): "ground-classification.svg",
    ("algorithms", "surface"): "below-surface.svg",
    ("tools", "smart"): "smart-line.svg",
    ("tools", "smart line"): "smart-line.svg",
    ("tools", "line"): "line.svg",
    ("tools", "polyline"): "polygon.svg",
    ("tools", "text"): "text.svg",
    ("tools", "move v"): "move-vertex.svg",
    ("tools", "move vertex"): "move-vertex.svg",
    ("tools", "vertex"): "vertex.svg",
    ("tools", "grid"): "grid.svg",
    ("select", "select drawing"): "select.svg",
    ("select", "deselect all"): "deselect.svg",
    ("settings", "draw settings"): "draw-settings.svg",
    ("curve tools", "curve"): "curve.svg",
}

def get_external_ribbon_icon_path(button_text: str, section_title: str = None,
                                  ribbon_scope: str = None) -> Path | None:
    """Return a user-provided ribbon icon path when one exists."""
    normalized_scope = _normalize_lookup_text(ribbon_scope)
    normalized_section = _normalize_lookup_text(section_title)
    normalized_button = _normalize_lookup_text(button_text)

    filename = _SCOPED_EXTERNAL_RIBBON_ICON_FILES.get(
        (normalized_scope, normalized_section, normalized_button)
    )
    if not filename:
        filename = _EXTERNAL_RIBBON_ICON_FILES.get(
            (normalized_section, normalized_button)
        )
    if not filename:
        return None

    icon_path = _ICON_DIR / filename
    if icon_path.exists():
        return icon_path
    return None


RIBBON_ICONS = {
    "Open": "folder_open",
    "Save": "save",
    "Save As…": "save_as",
    "Export": "export",
    "Import": "import",
    "Create DTM": "mountain",
    "Create DSM": "surface",
    "Export DTM": "export",
    "Export DSM": "export",
    "Load Tool": "import",
    "Manage": "settings_gear",

    "Attach": "attach",
    "Attach DXF": "attach",
    "Attach DWG": "file_dwg",
    "Attach SNT": "file_snt",
    "PRJ Loader": "clipboard_list",
    "Define": "clipboard_list",
    "Verify Labels": "search",

    # short labels
    "DXF": "attach",
    "PRJ": "clipboard_list",
    "VERIFY": "search",
    "DWG": "file_dwg",
    "SNT": "file_snt",

    "Clear": "broom",
    "Clear Data": "trash",
    "Undo": "undo",
    "Redo": "redo",
    "Cut": "cut",
    "Copy": "copy",
    "Paste": "paste",
    "Delete": "trash",
    "Top": "view_top",
    "Front": "view_front",
    "Side": "view_side",
    "3D": "view_3d",
    "Depth": "depth",
    "RGB": "rgb",
    "Intensity": "intensity",
    "Elevation": "elevation",
    "Class": "class_tag",
    "Shading": "shading",
    "Fit": "fit_view",
    "Cross": "cross_section",
    "Cut Section": "cut_section",
    "Width": "section_width",
    "Views": "sync",
    "Config": "keyboard",
    "Backup": "backup",
    "Preference": "preferences",
    "Preferences": "preferences",
    "Above": "arrow_up",
    "Below": "arrow_down",
    "Parallel": "parallel",
    "Centerline": "centerline",
    "Rect": "rectangle",
    "Circle": "circle",
    "Polygon": "polyline",
    "Free": "freehand",
    "Brush": "brush",
    "Point": "point_marker",
    "Line": "line",
    "Measure Line": "measure_line",
    "Path": "measure_path",
    "Measure Path": "measure_path",
    "Identify": "identify",
    "Zoom": "zoom_rect",
    "Select": "select_check",
    "Convert": "convert",
    "Close": "pin_close",
    "Height": "height_ruler",
    "Fence": "fence",
    "Low": "arrow_down",
    "Low Points": "arrow_down",
    "Smart": "smartline",
    "Isolated": "isolated",
    "Ground": "mountain",
    "Surface": "surface",
    "Smart\nLine": "smartline",
    "Polyline": "polyline",
    "Text": "text",
    "Move V": "move_vertex",
    "Move\nVertex": "move_vertex",
    "Vertex": "vertex",
    "Snap": "select_target",
    "Grid": "grid",
    "Select\nDrawing": "select_target",
    "Deselect\nAll": "deselect",
    "Draw\nSettings": "settings_gear",
    "Settings": "settings_gear",
    "Curve": "curve_point",
    "Start": "rocket",
    "Display": "config_display",
    "Fields": "fields",
    "Create Block": "neon_block",
    "Rotate": "rotate_transform",
    "Mirror": "mirror_transform",
    "Trace": "polyline",
}

_NORMALIZED_RIBBON_ICONS = {
    _normalize_lookup_text(label): icon_name
    for label, icon_name in RIBBON_ICONS.items()
}


def get_button_icon(button_text: str, section_title: str = None,
                    ribbon_scope: str = None,
                    color: str = None, size: int = 24) -> QIcon:
    """
    Resolve a ribbon button icon.
    Prefers user-provided files from gui/icons, then falls back to embedded SVGs.
    """
    external_icon_path = get_external_ribbon_icon_path(
        button_text,
        section_title=section_title,
        ribbon_scope=ribbon_scope,
    )
    if external_icon_path is not None:
        return _load_icon_from_file(
            external_icon_path,
            size,
            color=color,
            button_text=button_text,
            section_title=section_title,
            ribbon_scope=ribbon_scope,
        )

    icon_name = RIBBON_ICONS.get(button_text)
    if not icon_name:
        icon_name = _NORMALIZED_RIBBON_ICONS.get(_normalize_lookup_text(button_text))

    if icon_name:
        resolved_color = color or _resolve_ribbon_icon_color(
            button_text=button_text,
            section_title=section_title,
            ribbon_scope=ribbon_scope,
            icon_name=icon_name,
        )
        return get_icon(icon_name, resolved_color, size)

    return QIcon()


def get_ribbon_icon(tooltip_text: str, color: str = None, size: int = 24,
                    section_title: str = None, ribbon_scope: str = None) -> QIcon:
    """
    Get a QIcon by ribbon button tooltip/label text.
    Falls back to empty icon if not found.
    """
    return get_button_icon(
        tooltip_text,
        section_title=section_title,
        ribbon_scope=ribbon_scope,
        color=color,
        size=size,
    )


def list_available_icons():
    """Return list of all available icon names."""
    return list(_ICON_DATA.keys())
