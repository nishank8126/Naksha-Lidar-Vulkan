# gui/hatch_area_tool.py
# Hatch Area tool — Nakshatech-style parallel-line hatch fill for closed boundaries.

import os
import numpy as np

try:
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QLabel, QDoubleSpinBox,
        QCheckBox, QToolButton, QFrame, QButtonGroup
    )
    from PySide6.QtCore import Qt, QSettings, QSize, Signal
    from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QPen, QBrush, QPolygonF
    from PySide6.QtCore import QPointF
except ImportError:
    from PyQt5.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QLabel, QDoubleSpinBox,
        QCheckBox, QToolButton, QFrame, QButtonGroup
    )
    from PyQt5.QtCore import Qt, QSettings, QSize, QPointF
    from PyQt5.QtCore import pyqtSignal as Signal
    from PyQt5.QtGui import QIcon, QPixmap, QPainter, QColor, QPen, QBrush, QPolygonF

try:
    from gui.theme_manager import get_dialog_stylesheet, ThemeColors
    _HAS_THEME = True
except Exception:
    _HAS_THEME = False
    def get_dialog_stylesheet():
        return ""
    class ThemeColors:
        @staticmethod
        def get(k, d=""):
            return d

_ICONS_DIR = os.path.join(os.path.dirname(__file__), "icons")


# ---------------------------------------------------------------------------
# SVG icon loader
# ---------------------------------------------------------------------------

def _svg_icon(name, size=20):
    path = os.path.join(_ICONS_DIR, name)
    if not os.path.isfile(path):
        return None
    try:
        px = QPixmap(path)
        if not px.isNull():
            return QIcon(px.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    except Exception:
        pass
    return None


def _drawn_icon(draw_fn, size=20):
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    draw_fn(p, size)
    p.end()
    return QIcon(px)


# ---------------------------------------------------------------------------
# Fallback drawn icons (neutral grey — look fine on any theme background)
# ---------------------------------------------------------------------------

def _icon_element(size):
    def draw(p, s):
        clr = QColor("#888")
        p.setPen(QPen(clr, 1.5))
        p.setBrush(QBrush(clr))
        verts = [(3,2),(3,14),(6,11),(9,16),(11,15),(8,10),(12,10)]
        poly = QPolygonF([QPointF(x*s/18, y*s/18) for x,y in verts])
        p.drawPolygon(poly)
    return _drawn_icon(draw, size)

def _icon_fence(size):
    def draw(p, s):
        p.setPen(QPen(QColor("#888"), 1.5, Qt.DashLine))
        m = int(s*0.15)
        p.drawRect(m, m, s-2*m, s-2*m)
    return _drawn_icon(draw, size)

def _icon_points(size):
    def draw(p, s):
        p.setPen(QPen(QColor("#888"), 1.5))
        verts = [(0.5,0.1),(0.85,0.4),(0.75,0.85),(0.25,0.85),(0.15,0.4)]
        poly = QPolygonF([QPointF(x*s, y*s) for x,y in verts])
        p.drawPolygon(poly)
        p.setBrush(QBrush(QColor("#888")))
        r = max(2, int(s*0.08))
        for x, y in verts:
            p.drawEllipse(int(x*s-r), int(y*s-r), r*2, r*2)
    return _drawn_icon(draw, size)

def _icon_rect_select(size):
    def draw(p, s):
        m = int(s*0.12)
        p.setPen(QPen(QColor("#888"), 1.5))
        p.drawRect(m, m, s-2*m, s-2*m)
        p.setPen(QPen(QColor("#888"), 1.5, Qt.DashLine))
        p.drawLine(s//2, m, s//2, s-m)
        p.drawLine(m, s//2, s-m, s//2)
    return _drawn_icon(draw, size)

def _icon_freehand(size):
    def draw(p, s):
        p.setPen(QPen(QColor("#888"), 1.5))
        import math
        pts = []
        for i in range(10):
            t = i / 9.0
            x = s*0.1 + t*(s*0.8)
            y = s*0.5 + math.sin(t*math.pi*2)*s*0.25
            pts.append(QPointF(x, y))
        for i in range(len(pts)-1):
            p.drawLine(pts[i], pts[i+1])
    return _drawn_icon(draw, size)

def _icon_union(size):
    def draw(p, s):
        m = int(s*0.12)
        h = int(s*0.3)
        p.setPen(QPen(QColor("#888"), 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawRect(m, m, s-2*m-h, s-2*m-h)
        p.drawRect(m+h, m+h, s-2*m-h, s-2*m-h)
    return _drawn_icon(draw, size)

def _icon_block(size):
    def draw(p, s):
        p.setPen(QPen(QColor("#888"), 1.5))
        p.setBrush(Qt.NoBrush)
        m = int(s*0.12)
        half = (s - 2*m) / 2.0
        p.drawRect(m, m, s-2*m, s-2*m)
        p.drawLine(int(m+half), m, int(m+half), s-m)
        p.drawLine(m, int(m+half), s-m, int(m+half))
    return _drawn_icon(draw, size)


def _load_icon(svg_name, fallback_fn, size=20):
    """Try SVG first, fall back to drawn icon."""
    icon = _svg_icon(svg_name, size)
    return icon if icon is not None else fallback_fn(size)


# ---------------------------------------------------------------------------
# Toolbar button builder using theme-aware styling
# ---------------------------------------------------------------------------

def _make_method_button(icon, tooltip):
    """Create a checkable QToolButton styled via the theme's accent color."""
    accent = ThemeColors.get("accent", "#0077b6") if _HAS_THEME else "#0077b6"
    accent_hover = ThemeColors.get("accent_hover", "#0098ff") if _HAS_THEME else "#0098ff"
    bg_btn = ThemeColors.get("bg_button", "transparent") if _HAS_THEME else "transparent"
    bg_hover = ThemeColors.get("bg_button_hover", "rgba(0,0,0,0.08)") if _HAS_THEME else "rgba(0,0,0,0.08)"
    border = ThemeColors.get("border", "#ccc") if _HAS_THEME else "#ccc"

    btn = QToolButton()
    btn.setIcon(icon)
    btn.setIconSize(QSize(18, 18))
    btn.setFixedSize(28, 28)
    btn.setCheckable(True)
    btn.setToolTip(tooltip)
    btn.setStyleSheet(f"""
        QToolButton {{
            border: 1px solid {border};
            border-radius: 4px;
            background: {bg_btn};
            padding: 2px;
        }}
        QToolButton:hover {{
            background: {bg_hover};
            border-color: {accent};
        }}
        QToolButton:checked {{
            background: {accent};
            border-color: {accent_hover};
        }}
        QToolButton:pressed {{
            background: {accent_hover};
        }}
    """)
    return btn


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _point_in_polygon_2d(point, polygon):
    x, y = float(point[0]), float(point[1])
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = float(polygon[i][0]), float(polygon[i][1])
        xj, yj = float(polygon[j][0]), float(polygon[j][1])
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-15) + xi):
            inside = not inside
        j = i
    return inside


def _clip_line_to_polygon(p1, p2, polygon):
    pts = np.asarray(polygon, dtype=np.float64)
    d = p2 - p1
    if np.linalg.norm(d) < 1e-12:
        return []

    diag = np.max(pts, axis=0) - np.min(pts, axis=0)
    BIG = np.linalg.norm(diag) + 1.0

    t_vals = []
    n_pts = len(pts)
    for i in range(n_pts):
        a = pts[i]
        b = pts[(i + 1) % n_pts]
        edge = b - a
        denom = d[0] * edge[1] - d[1] * edge[0]
        if abs(denom) < 1e-12:
            continue
        diff = a - p1
        t = (diff[0] * edge[1] - diff[1] * edge[0]) / denom
        s = (diff[0] * d[1] - diff[1] * d[0]) / denom
        if -1e-9 <= s <= 1.0 + 1e-9:
            t_vals.append(t)

    if len(t_vals) < 2:
        return []

    t_vals = sorted(set(round(t, 10) for t in t_vals))

    segments = []
    for i in range(len(t_vals) - 1):
        t_mid = (t_vals[i] + t_vals[i + 1]) / 2.0
        mid = p1 + t_mid * d
        if _point_in_polygon_2d(mid, pts):
            segments.append((p1 + t_vals[i] * d, p1 + t_vals[i + 1] * d))

    return segments


def generate_hatch_lines(boundary_pts, spacing, angle_deg):
    """
    Generate hatch line segments that fill *boundary_pts* polygon.

    Args:
        boundary_pts : list of (x, y[, z]) tuples/arrays
        spacing      : distance between parallel hatch lines (world units)
        angle_deg    : angle of hatch lines in degrees (0 = horizontal)

    Returns:
        List of [(x1,y1,z), (x2,y2,z)] segment pairs.
    """
    if len(boundary_pts) < 3 or spacing <= 0:
        return []

    pts_2d = np.array([(float(p[0]), float(p[1])) for p in boundary_pts])
    z = float(boundary_pts[0][2]) if len(boundary_pts[0]) > 2 else 0.0

    angle_rad = np.radians(float(angle_deg))
    d = np.array([np.cos(angle_rad), np.sin(angle_rad)])
    n = np.array([-np.sin(angle_rad), np.cos(angle_rad)])

    projections = pts_2d @ n
    proj_min = projections.min()
    proj_max = projections.max()

    positions = []
    t = proj_min
    while t <= proj_max + 1e-9:
        positions.append(t)
        t += spacing

    diag_len = np.linalg.norm(pts_2d.max(axis=0) - pts_2d.min(axis=0)) + spacing * 2

    segments = []
    for t in positions:
        origin = n * t
        p1 = origin - d * diag_len
        p2 = origin + d * diag_len
        for seg in _clip_line_to_polygon(p1, p2, pts_2d):
            segments.append([
                (float(seg[0][0]), float(seg[0][1]), z),
                (float(seg[1][0]), float(seg[1][1]), z),
            ])

    return segments


# ---------------------------------------------------------------------------
# HatchAreaDialog — fully theme-aware (dark & light)
# ---------------------------------------------------------------------------

class HatchAreaDialog(QDialog):
    """
    Floating Hatch Area dialog — Nakshatech-style.
    Uses the app's theme manager for automatic dark/light theme adaptation.
    """

    settings_changed = Signal()
    method_changed = Signal(int)   # emitted when user clicks a method button (0-5)

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("Hatch Area")
        self.setFixedWidth(275)
        self.setModal(False)

        # Apply the app-wide theme (handles dark AND light automatically)
        ss = get_dialog_stylesheet()
        if ss:
            self.setStyleSheet(ss)

        self._load_settings()
        self._build_ui()

    # ------------------------------------------------------------------
    def _load_settings(self):
        s = QSettings("NakshaAI", "LidarApp")

        # Minimum hatch spacing should start from 0.1, not 0.0001.
        try:
            self._spacing = max(0.1, float(s.value("hatch/spacing", 0.1)))
        except Exception:
            self._spacing = 0.1

        self._angle = float(s.value("hatch/angle", 0.0))
        self._drop_pattern = s.value("hatch/drop_pattern", False, type=bool)
        self._assoc_boundary = s.value("hatch/assoc_boundary", True, type=bool)
        self._snappable = s.value("hatch/snappable", False, type=bool)

    def _save_settings(self):
        s = QSettings("NakshaAI", "LidarApp")
        s.setValue("hatch/spacing", self._spacing)
        s.setValue("hatch/angle", self._angle)
        s.setValue("hatch/drop_pattern", self._drop_pattern)
        s.setValue("hatch/assoc_boundary", self._assoc_boundary)
        s.setValue("hatch/snappable", self._snappable)

    # ------------------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # ── Method selector toolbar ──────────────────────────────────
        icon_defs = [
            (_load_icon("select.svg",          _icon_element,   18), "Select by Element"),
            (_load_icon("fence-conversion.svg", _icon_fence,    18), "Select by Fence"),
            (_load_icon("polygon.svg",          _icon_points,   18), "Select by Points"),
            (_load_icon("rectangle-select.svg", _icon_rect_select, 18), "Select by Rectangle"),
            (_load_icon("freehand-draw.svg",    _icon_freehand, 18), "Freehand / Flood"),
            (_load_icon("area-block.svg",       _icon_union,    18), "Union Areas"),
            (_load_icon("block-select.svg",     _icon_block,    18), "Select Block (SNT/DXF)"),
        ]

        toolbar_row = QHBoxLayout()
        toolbar_row.setSpacing(3)
        self._method_group = QButtonGroup(self)
        self._method_group.setExclusive(True)
        for i, (icon, tip) in enumerate(icon_defs):
            btn = _make_method_button(icon, tip)
            self._method_group.addButton(btn, i)
            toolbar_row.addWidget(btn)
            if i == 2:  # "Select by Points" — the active mode
                btn.setChecked(True)
        toolbar_row.addStretch()
        layout.addLayout(toolbar_row)
        # Re-activates hatch tool if user clicks a method button while another tool is active
        self._method_group.idClicked.connect(self.method_changed)

        sep1 = QFrame()
        sep1.setFrameShape(QFrame.HLine)
        sep1.setFrameShadow(QFrame.Sunken)
        layout.addWidget(sep1)

        # ── Spacing ───────────────────────────────────────────────────
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        lbl_sp = QLabel("Spacing:")
        lbl_sp.setFixedWidth(58)

        self.spacing_spin = QDoubleSpinBox()
        self.spacing_spin.setDecimals(1)
        self.spacing_spin.setRange(0.1, 1_000_000.0)
        self.spacing_spin.setSingleStep(0.1)
        self.spacing_spin.setValue(max(0.1, self._spacing))
        self.spacing_spin.setMinimumWidth(120)

        row1.addWidget(lbl_sp)
        row1.addWidget(self.spacing_spin)
        row1.addStretch()
        layout.addLayout(row1)

        # ── Angle ──────────────────────────────────────────────────────
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        lbl_ang = QLabel("Angle:")
        lbl_ang.setFixedWidth(58)

        self.angle_spin = QDoubleSpinBox()
        self.angle_spin.setDecimals(1)
        self.angle_spin.setRange(-360.0, 360.0)
        self.angle_spin.setSingleStep(5.0)
        self.angle_spin.setSuffix("°")
        self.angle_spin.setValue(self._angle)
        self.angle_spin.setMinimumWidth(120)

        row2.addWidget(lbl_ang)
        row2.addWidget(self.angle_spin)
        row2.addStretch()
        layout.addLayout(row2)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setFrameShadow(QFrame.Sunken)
        layout.addWidget(sep2)

        # ── Checkboxes ───────────────────────────────────────────────
        self.chk_drop = QCheckBox("Drop Pattern")
        self.chk_drop.setChecked(self._drop_pattern)
        layout.addWidget(self.chk_drop)

        self.chk_assoc = QCheckBox("Associative Boundary")
        self.chk_assoc.setChecked(self._assoc_boundary)
        layout.addWidget(self.chk_assoc)

        self.chk_snap = QCheckBox("Snappable")
        self.chk_snap.setChecked(self._snappable)
        layout.addWidget(self.chk_snap)

        # ── Status / instruction label ─────────────────────────────
        self.status_lbl = QLabel(
            "Click inside a shape to hatch it,\n"
            "or click empty space to add boundary\npoints (right-click ≥3 to apply)."
        )
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setObjectName("dialogHintLabel")   # picks up theme style
        layout.addWidget(self.status_lbl)

        # ── Signals ────────────────────────────────────────────────
        self.spacing_spin.valueChanged.connect(self._on_changed)
        self.angle_spin.valueChanged.connect(self._on_changed)
        self.chk_drop.toggled.connect(self._on_changed)
        self.chk_assoc.toggled.connect(self._on_changed)
        self.chk_snap.toggled.connect(self._on_changed)

    # ------------------------------------------------------------------
    def _on_changed(self):
        self._spacing = max(0.1, self.spacing_spin.value())
        self._angle = self.angle_spin.value()
        self._drop_pattern = self.chk_drop.isChecked()
        self._assoc_boundary = self.chk_assoc.isChecked()
        self._snappable = self.chk_snap.isChecked()
        self._save_settings()
        self.settings_changed.emit()

    # ------------------------------------------------------------------
    @property
    def spacing(self):
        return self.spacing_spin.value()

    @property
    def angle(self):
        return self.angle_spin.value()

    def set_status(self, text):
        try:
            self.status_lbl.setText(text)
        except Exception:
            pass

    def refresh_theme(self):
        """Call this when the app theme changes to re-apply the stylesheet."""
        ss = get_dialog_stylesheet()
        if ss:
            self.setStyleSheet(ss)
        # Rebuild toolbar button styles
        for btn in self._method_group.buttons():
            was_checked = btn.isChecked()
            accent = ThemeColors.get("accent", "#0077b6") if _HAS_THEME else "#0077b6"
            accent_hover = ThemeColors.get("accent_hover", "#0098ff") if _HAS_THEME else "#0098ff"
            bg_btn = ThemeColors.get("bg_button", "transparent") if _HAS_THEME else "transparent"
            bg_hover = ThemeColors.get("bg_button_hover", "rgba(0,0,0,0.08)") if _HAS_THEME else "rgba(0,0,0,0.08)"
            border = ThemeColors.get("border", "#ccc") if _HAS_THEME else "#ccc"
            btn.setStyleSheet(f"""
                QToolButton {{
                    border: 1px solid {border};
                    border-radius: 4px;
                    background: {bg_btn};
                    padding: 2px;
                }}
                QToolButton:hover {{
                    background: {bg_hover};
                    border-color: {accent};
                }}
                QToolButton:checked {{
                    background: {accent};
                    border-color: {accent_hover};
                }}
            """)

    def closeEvent(self, event):
        self._save_settings()
        super().closeEvent(event)
