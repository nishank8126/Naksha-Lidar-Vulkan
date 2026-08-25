# ─────────────────────────────────────────────────────────────────────────────
# icons.py — open-source (Lucide, ISC license) SVG icon set for the GIS panel.
#
# Design goals:
#   • Real SVG assets (the markup lives here and is written to
#     assets/gui/icons/*.svg so the repo has clean standalone files), BUT
#   • rendering reads from the embedded markup directly — so it works in dev
#     AND in a frozen/PyInstaller build even if the files aren't bundled, and
#   • a code-drawn fallback covers any environment where QtSvg is unavailable.
#
# Icons are tinted to the active theme colour at render time (currentColor).
# This module is the single place that maps icon NAMES → asset markup, keeping
# the panel layout code free of icon-path concerns.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import os
import sys

# Lucide icon paths (https://lucide.dev, ISC license). 24×24 grid, stroke-based.
_LUCIDE = {
    # toolbar
    "filter":        '<path d="M22 3H2l8 9.46V19l4 2v-8.54L22 3z"/>',
    "eye":           '<path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/>',
    "eye-off":       '<path d="M9.88 9.88a3 3 0 1 0 4.24 4.24"/><path d="M10.73 5.08A10.43 10.43 0 0 1 12 5c7 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68"/><path d="M6.61 6.61A13.526 13.526 0 0 0 2 12s3 7 10 7a9.74 9.74 0 0 0 5.39-1.61"/><line x1="2" x2="22" y1="2" y2="22"/>',
    "expand-all":    '<path d="m7 15 5 5 5-5"/><path d="m7 9 5-5 5 5"/>',
    "collapse-all":  '<path d="m7 20 5-5 5 5"/><path d="m7 4 5 5 5-5"/>',
    "add-group":     '<path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2z"/><line x1="12" x2="12" y1="10" y2="16"/><line x1="9" x2="15" y1="13" y2="13"/>',
    "folder":        '<path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2z"/>',
    "delete-layer":  '<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" x2="10" y1="11" y2="17"/><line x1="14" x2="14" y1="11" y2="17"/>',
    "import":        '<path d="M12 3v12"/><path d="m8 11 4 4 4-4"/><path d="M8 5H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2"/>',
    "plus":          '<path d="M5 12h14"/><path d="M12 5v14"/>',
    "chevron-up":    '<path d="m18 15-6-6-6 6"/>',
    "chevron-down":  '<path d="m6 9 6 6 6-6"/>',
    "search":        '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    "sliders":       '<line x1="21" x2="14" y1="4" y2="4"/><line x1="10" x2="3" y1="4" y2="4"/><line x1="21" x2="12" y1="12" y2="12"/><line x1="8" x2="3" y1="12" y2="12"/><line x1="21" x2="16" y1="20" y2="20"/><line x1="12" x2="3" y1="20" y2="20"/><line x1="14" x2="14" y1="2" y2="6"/><line x1="8" x2="8" y1="10" y2="14"/><line x1="16" x2="16" y1="18" y2="22"/>',
    "target":        '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
    "table":         '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/><path d="M15 3v18"/><path d="M3 9h18"/><path d="M3 15h18"/>',
    "database":      '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4 3 9 3s9-1.34 9-3"/>',
    # layer-type fallbacks (filled symbology is drawn separately per-layer)
    "globe":         '<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>',
    "raster-layer":  '<rect width="18" height="18" x="3" y="3" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/>',
    "vector-point":  '<circle cx="12" cy="12" r="4"/>',
    "vector-line":   '<path d="M5 19 19 5"/><circle cx="5" cy="19" r="2"/><circle cx="19" cy="5" r="2"/>',
    "vector-polygon":'<path d="M12 2 4 7v10l8 5 8-5V7z"/>',
}

# Map our internal/legacy names → Lucide names (and to code-draw fallback names).
_ALIAS = {
    "raster": "raster-layer",
    "vector": "vector-polygon",
    "trash": "delete-layer",
    "up": "chevron-up",
    "down": "chevron-down",
    "zoom": "search",
    "eye_off": "eye-off",
}

# Fallback to gui.gis.gis_layers._glyph names when SVG/QtSvg is unavailable.
_GLYPH_FALLBACK = {
    "raster-layer": "raster", "vector-point": "vector", "vector-line": "vector",
    "vector-polygon": "vector", "delete-layer": "trash", "chevron-up": "up",
    "chevron-down": "down", "expand-all": "down", "collapse-all": "up",
    "search":        "zoom", "eye-off": "eye_off", "eye": "eye", "plus": "plus",
    "sliders":       "sliders", "add-group": "folder", "folder": "folder",
    "filter":        "filter", "import": "import", "target": "zoom",
    "table":         "table",
    "database":      "table",
    "globe":         "zoom",
}

_SVG_HEADER = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="{color}" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round">{body}</svg>'
)


def _resolve(name: str) -> str:
    return _ALIAS.get(name, name)


def assets_dir() -> str:
    """Where the standalone .svg files live (dev: project; frozen: _MEIPASS)."""
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        # gui/gis/icons.py → project root is two levels up from gui/
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "assets", "gui", "icons")


def export_svg_files():
    """Best-effort: write each icon as a clean standalone SVG into assets/gui/icons/."""
    try:
        d = assets_dir()
        os.makedirs(d, exist_ok=True)
        for name, body in _LUCIDE.items():
            fp = os.path.join(d, f"{name}.svg")
            if not os.path.exists(fp):
                with open(fp, "w", encoding="utf-8") as fh:
                    fh.write(_SVG_HEADER.format(color="currentColor", body=body))
    except Exception:
        pass  # purely cosmetic; rendering never depends on these files


def pixmap(name: str, color_hex: str, size: int = 16):
    """Render the icon, tinted to color_hex, as a hi-DPI QPixmap."""
    from PySide6.QtGui import QPixmap
    from PySide6.QtCore import Qt

    lucide = _resolve(name)
    body = _LUCIDE.get(lucide)
    if body is not None:
        try:
            from PySide6.QtSvg import QSvgRenderer
            from PySide6.QtGui import QPainter
            from PySide6.QtCore import QByteArray, QRectF
            svg = _SVG_HEADER.format(color=color_hex, body=body)
            renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
            dpr = 2
            pm = QPixmap(size * dpr, size * dpr)
            pm.fill(Qt.transparent)
            pm.setDevicePixelRatio(dpr)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing, True)
            # Render into the LOGICAL rect (0,0,size,size). Without an explicit
            # rect, QSvgRenderer fills the device viewport (size*dpr) on top of
            # the dpr scaling → the icon draws double-size and crops/zooms.
            renderer.render(p, QRectF(0, 0, size, size))
            p.end()
            return pm
        except Exception:
            pass

    # Fallback: code-drawn glyph from the panel module.
    try:
        from gui.gis.gis_layers import _glyph
        return _glyph(_GLYPH_FALLBACK.get(lucide, "plus"), color_hex, size)
    except Exception:
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        return pm


def icon(name: str, color_hex: str, size: int = 16):
    from PySide6.QtGui import QIcon
    return QIcon(pixmap(name, color_hex, size))


# Write the standalone files once at import (best-effort).
export_svg_files()
