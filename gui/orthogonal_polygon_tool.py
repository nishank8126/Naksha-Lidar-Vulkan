"""
orthogonal_polygon_tool.py
--------------------------
Port of the QGIS OrthogonalPolygonTool to the NakshaAI-Lidar VTK/PySide6 pipeline.
All QGIS-specific classes (QgsMapTool, QgsRubberBand, QgsPointXY, QgsCircularString,
QgsCompoundCurve, iface …) have been replaced with VTK actors + pure-Python math.

DRAWING MODES  (identical behaviour to the QGIS original)
─────────────────────────────────────────────────────────
  🔴 ORTHO    (default)
        First two clicks define the baseline.  Every subsequent vertex is
        constrained to 90° relative to the previous segment.
        Right-click auto-computes a closing vertex for a perfect right
        angle at both the last vertex and P1.

  🔵 FREEHAND / STRAIGHT  (Space)
        No angle constraint.  Click freely; right-click closes with a
        straight line back to P1.

  🟣 CURVE  (Shift+Space)
        3-point circular-arc mode.
        Click 1 → arc mid-point;  Click 2 → arc end-point.
        The arc is densified into polyline vertices and appended to the ring.

KEYBOARD SHORTCUTS
──────────────────
  Space          – toggle ORTHO ↔ FREEHAND
  Shift+Space    – toggle CURVE mode on/off (returns to previous mode)
  Escape         – cancel and discard the current polygon

GEOMETRY MATH  (replaces QGIS classes)
───────────────────────────────────────
  Ortho projection  : project cursor onto locked axis via dot-product
  Auto-close (ortho): Cramer's-rule intersection of two parametric lines
  Circular arc      : circumscribed circle of 3 points → linspace of angles

═══════════════════════════════════════════════════════════════════════════
INTEGRATION INTO digitize_tools.py  (two small edits only)
═══════════════════════════════════════════════════════════════════════════

  1.  Inside DigitizeManager.set_tool(), add this block BEFORE the existing
      "if self.active_tool and self.active_tool not in ..." guard:

        # ── Ortho-Polygon tool ────────────────────────────────────────────
        if tool == 'orthopolygon':
            from gui.orthogonal_polygon_tool import OrthogonalPolygonTool
            # Tear down any previous instance
            old = getattr(self, '_ortho_polygon_tool', None)
            if old:
                old.deactivate()
            inst = OrthogonalPolygonTool(self)
            inst.activate()
            self._ortho_polygon_tool = inst
            return
        # ─────────────────────────────────────────────────────────────────

  2.  Inside DigitizeManager._deactivate_active_tool_keep_drawings(),
      add this elif branch (alongside the existing "elif tool == 'freehand'" etc.):

        elif tool == 'orthopolygon':
            inst = getattr(self, '_ortho_polygon_tool', None)
            if inst:
                inst._cancel()
                inst.deactivate()
                self._ortho_polygon_tool = None

  3.  Add a toolbar/ribbon button that calls:
        self.digitizer.set_tool('orthopolygon')

      To activate the tool programmatically from anywhere:
        from gui.orthogonal_polygon_tool import OrthogonalPolygonTool
        OrthogonalPolygonTool(self.digitizer).activate()
"""

from __future__ import annotations
import math
import numpy as np
import vtk


# ─────────────────────────────────────────────────────────────────────────────
# Mode constants  (same names as the QGIS original)
# ─────────────────────────────────────────────────────────────────────────────

MODE_ORTHO    = 'ortho'
MODE_FREEHAND = 'freehand'
MODE_CURVE    = 'curve'

# VTK RGB colours (0-1 range) — matches original mode indicator colours
_MODE_COLORS: dict[str, tuple[float, float, float]] = {
    MODE_ORTHO:    (1.00, 0.24, 0.00),   # red-orange
    MODE_FREEHAND: (0.00, 0.45, 1.00),   # blue
    MODE_CURVE:    (0.80, 0.00, 1.00),   # magenta
}

_MODE_LABELS: dict[str, str] = {
    MODE_ORTHO:    "🔴 Orthogonal mode  (right-angle constraint ON) | Space=Freehand | Shift+Space=Curve",
    MODE_FREEHAND: "🔵 Freehand / Straight mode  (constraint OFF) | Space=Ortho | Shift+Space=Curve",
    MODE_CURVE:    "🟣 Curve mode  (click mid-point then end-point) | Shift+Space=exit Curve",
}

# Preview actor attribute names stored on the digitizer instance
_ATTR_EDGE = '_ortho_edge_preview'    # dashed live-edge line
_ATTR_POLY = '_ortho_poly_preview'    # solid committed-ring outline


# ─────────────────────────────────────────────────────────────────────────────
# Pure-Python / numpy geometry helpers  (no QGIS dependency)
# ─────────────────────────────────────────────────────────────────────────────

def _unit_vec(angle_rad: float) -> tuple[float, float]:
    """(cos θ, sin θ) unit vector — replaces QGIS unit_vector()."""
    return math.cos(angle_rad), math.sin(angle_rad)


def _seg_angle(p1: tuple, p2: tuple) -> float:
    """Angle of directed segment p1→p2, radians CCW from +X — replaces segment_angle()."""
    return math.atan2(p2[1] - p1[1], p2[0] - p1[0])


def _project_ray(origin: tuple, angle_rad: float, point: tuple) -> tuple:
    """
    Project *point* onto the ray (origin, angle_rad) — replaces project_onto_ray().

    Math:
        d = (cos Î¸, sin Î¸)        axis unit vector
        v = point − origin
        t = dot(v, d)             scalar projection
        result = origin + t·d
    Z is kept constant (from origin) because the LiDAR canvas is 2-D plan view.
    """
    dx, dy = _unit_vec(angle_rad)
    vx = point[0] - origin[0]
    vy = point[1] - origin[1]
    t  = vx * dx + vy * dy
    z  = origin[2] if len(origin) > 2 else 0.0
    return (origin[0] + t * dx, origin[1] + t * dy, z)


def _line_intersect(p1: tuple, d1x: float, d1y: float,
                    p2: tuple, d2x: float, d2y: float) -> tuple | None:
    """
    Parametric line intersection via Cramer's rule — replaces line_intersection().

    Line A : p1 + s·(d1x, d1y)
    Line B : p2 + t·(d2x, d2y)

    Returns (x, y, z) intersection point, or None if lines are parallel.
    """
    denom = d1x * (-d2y) - d1y * (-d2x)
    if abs(denom) < 1e-12:
        return None
    dx = p2[0] - p1[0]
    dy = p2[1] - p1[1]
    s  = (dx * (-d2y) - dy * (-d2x)) / denom
    z  = p1[2] if len(p1) > 2 else 0.0
    return (p1[0] + s * d1x, p1[1] + s * d1y, z)


def _arc_to_polyline(start: tuple, mid: tuple, end: tuple,
                     segments_per_quarter: int = 12) -> list[tuple]:
    """
    Convert a 3-point circular arc (start, through-point mid, end) to a list
    of (x, y, z) tuples — replaces arc_to_polyline() / QgsCircularString.

    Algorithm
    ─────────
    1. Find the circumscribed circle of the three points (circumcenter + radius).
    2. Compute the angle from the circumcenter to each point.
    3. Determine arc direction (CW or CCW) by checking whether *mid* sits
       between *start* and *end* in the CCW direction.
    4. Linspace from a_start to a_end along the chosen direction.

    Falls back to a straight line for degenerate (collinear) input.
    """
    ax, ay = float(start[0]), float(start[1])
    bx, by = float(mid[0]),   float(mid[1])
    cx, cy = float(end[0]),   float(end[1])
    z = float(start[2]) if len(start) > 2 else 0.0

    # ── 1. Circumcenter ──────────────────────────────────────────────────────
    D = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(D) < 1e-12:
        return [tuple(start), tuple(end)]          # collinear — straight line

    ux = ((ax ** 2 + ay ** 2) * (by - cy) +
          (bx ** 2 + by ** 2) * (cy - ay) +
          (cx ** 2 + cy ** 2) * (ay - by)) / D
    uy = ((ax ** 2 + ay ** 2) * (cx - bx) +
          (bx ** 2 + by ** 2) * (ax - cx) +
          (cx ** 2 + cy ** 2) * (bx - ax)) / D

    radius = math.hypot(ax - ux, ay - uy)
    if radius < 1e-9:
        return [tuple(start), tuple(end)]

    # ── 2. Angles from circumcenter to each input point ──────────────────────
    a_start = math.atan2(ay - uy, ax - ux)
    a_mid   = math.atan2(by - uy, bx - ux)
    a_end   = math.atan2(cy - uy, cx - ux)

    # ── 3. Direction: CCW if mid falls between start and end going CCW ────────
    def _ccw_offset(a: float, ref: float) -> float:
        """Normalise *a* to [0, 2π) relative to *ref*."""
        return (a - ref) % (2.0 * math.pi)

    mid_off = _ccw_offset(a_mid, a_start)
    end_off = _ccw_offset(a_end, a_start)

    if mid_off < end_off:
        sweep = end_off                        # CCW (positive)
    else:
        sweep = end_off - 2.0 * math.pi       # CW  (negative)

    # ── 4. Densify ────────────────────────────────────────────────────────────
    # One segment per π/(2·spq) radians, matching QGIS curveToLine density.
    n_segs = max(8, int(abs(sweep) / (math.pi / (2.0 * segments_per_quarter))))
    n_segs = min(n_segs, 720)

    angles = np.linspace(a_start, a_start + sweep, n_segs + 1)
    return [(float(ux + radius * math.cos(a)),
             float(uy + radius * math.sin(a)),
             z) for a in angles]


# ─────────────────────────────────────────────────────────────────────────────
# Main tool class
# ─────────────────────────────────────────────────────────────────────────────

class OrthogonalPolygonTool:
    """
    Multi-mode polygon digitizing tool for the NakshaAI-Lidar DigitizeManager.

    Replaces the QGIS QgsMapTool subclass; uses VTK interactor observers and
    the digitizer's existing helper methods for rendering so it fits naturally
    into the existing tool pipeline.

    Internal state
    ──────────────
    self.points        – confirmed (x, y, z) vertices in world space
    self._mode         – current drawing mode (MODE_*)
    self._prev_mode    – mode to return to when Curve exits
    self._ortho_angle  – locked orthogonal axis (radians)

    Curve sub-state:
    self._curve_step   – 0 = waiting for mid-point, 1 = waiting for end-point
    self._curve_start  – arc start (x, y, z)
    self._curve_mid    – arc mid-point (x, y, z)
    """

    TOOL_NAME = 'orthopolygon'

    def __init__(self, digitizer):
        """
        Parameters
        ----------
        digitizer : DigitizeManager
            The application's DigitizeManager instance.
        """
        self.dig        = digitizer

        # ── Drawing state ─────────────────────────────────────────────────────
        self.points       : list[tuple] = []
        self._mode        : str = MODE_ORTHO
        self._prev_mode   : str = MODE_ORTHO
        self._ortho_angle : float | None = None

        # Curve sub-state
        self._curve_step  : int = 0
        self._curve_start : tuple | None = None
        self._curve_mid   : tuple | None = None

        # VTK vertex sphere markers (one per confirmed vertex)
        self._vertex_actors: list = []

        # VTK observer IDs (only the ones we add, so we can remove only ours)
        self._obs_ids: list[int] = []
        self._registered_interactor = None

        # In-progress Ctrl+Z / Ctrl+Y history for active Ortho editing
        self._undo_states: list[dict] = []
        self._redo_states: list[dict] = []

    @property
    def renderer(self):
        return self.dig.renderer

    @renderer.setter
    def renderer(self, val):
        pass

    @property
    def interactor(self):
        return self.dig.interactor

    @interactor.setter
    def interactor(self, val):
        pass

    @property
    def overlay_renderer(self):
        return self.dig.overlay_renderer

    @overlay_renderer.setter
    def overlay_renderer(self, val):
        pass

    # ─────────────────────────────────────────────────────────────────────────
    # Activation / Deactivation
    # ─────────────────────────────────────────────────────────────────────────

    def activate(self):
        """
        Register VTK observers and mark the tool active in the digitizer.
        Call once; call deactivate() to stop.
        """
        # Let the digitizer know we are the active tool so its generic
        self.dig.active_tool = self.TOOL_NAME
        self._sync_digitizer_temp_points()

        self._registered_interactor = self.interactor
        obs = self._registered_interactor.AddObserver
        self._obs_ids = [
            obs('LeftButtonPressEvent',    self._on_left_press,   2.0),
            obs('RightButtonPressEvent',   self._on_right_press,  2.0),
            obs('MouseMoveEvent',          self._on_mouse_move,   2.0),
            obs('KeyPressEvent',           self._on_key_press,    2.0),
            # Middle-mouse panning: delegate to the digitizer's handlers
            obs('MiddleButtonPressEvent',   self.dig._on_middle_press,   1.0),
            obs('MiddleButtonReleaseEvent', self.dig._on_middle_release, 1.0),
            obs('MouseWheelForwardEvent',   self.dig._on_zoom,           1.0),
            obs('MouseWheelBackwardEvent',  self.dig._on_zoom,           1.0),
        ]

        if hasattr(self.dig.app, 'set_cross_cursor_active'):
            self.dig.app.set_cross_cursor_active(True, 'draw')

        self._show_status(_MODE_LABELS[self._mode])
        print(f"✅ OrthogonalPolygonTool activated — mode: {self._mode}")

    def deactivate(self, clear_state=True):
        """Remove all our VTK observers and clean up preview actors."""
        if self._registered_interactor:
            for oid in self._obs_ids:
                try:
                    self._registered_interactor.RemoveObserver(oid)
                except Exception:
                    pass
            self._registered_interactor = None
        self._obs_ids = []

        if clear_state:
            self._clear_preview()
            self._clear_vertex_markers()
            self._clear_edit_history()
            self._reset_state()

        if self.dig.active_tool == self.TOOL_NAME:
            self.dig.active_tool = None

        if hasattr(self.dig.app, 'set_cross_cursor_active'):
            self.dig.app.set_cross_cursor_active(False, 'draw')

        print("⚪ OrthogonalPolygonTool deactivated")

    # ─────────────────────────────────────────────────────────────────────────
    # VTK Event Handlers
    # ─────────────────────────────────────────────────────────────────────────

    def _on_left_press(self, obj, evt):
        self.dig._consume_vtk_event(obj)
        cursor = self._pick_world()
        self._left_click(cursor)
        self.dig.app.vtk_widget.render()

    def _on_right_press(self, obj, evt):
        self.dig._consume_vtk_event(obj)
        self._finish_polygon()
        self.dig.app.vtk_widget.render()

    def _on_mouse_move(self, obj, evt):
        # Middle-mouse panning: let the interactor style move the camera
        if self.dig._is_panning:
            style = self.interactor.GetInteractorStyle()
            if style and hasattr(style, 'OnMouseMove'):
                style.OnMouseMove()
            # After pan, rebuild the preview at the new camera position
            if self.points:
                try:
                    from PySide6.QtCore import QTimer
                except ImportError:
                    from  PySide6.QtCore import QTimer
                QTimer.singleShot(0, self._deferred_preview_refresh)
            self.dig.app.vtk_widget.render()
            return

        self.dig._consume_vtk_event(obj)
        if not self.points:
            return
        cursor = self._pick_world()
        self._update_preview(cursor)
        self.dig.app.vtk_widget.render()

    def _on_key_press(self, obj, evt):
        """
        Keyboard handling — mirrors OrthogonalPolygonTool.keyPressEvent() exactly.

        Escape         → cancel current polygon (keep tool active)
        Space          → toggle ORTHO ↔ FREEHAND  (ignored in CURVE mode)
        Shift + Space  → toggle CURVE mode on/off
        """
        key   = self.interactor.GetKeySym().lower()
        shift = bool(self.interactor.GetShiftKey())

        if key == 'escape':
            # Only handle plain Escape (Shift+Escape is handled by the digitizer
            # via _deactivate_active_tool_keep_drawings to fully exit the tool).
            if not shift:
                self._cancel()
                self.dig._consume_vtk_event(obj)
            return

        if key == 'space':
            # Consume Space so VTK's default interactor style doesn't reset the camera.
            self.dig._consume_vtk_event(obj)
            if shift:
                # Shift+Space — toggle CURVE
                if self._mode == MODE_CURVE:
                    self._reset_curve_state()
                    self._set_mode(self._prev_mode)
                else:
                    self._prev_mode = self._mode
                    self._reset_curve_state()
                    self._set_mode(MODE_CURVE)
            else:
                # Space — toggle ORTHO ↔ FREEHAND  (ignored while in CURVE)
                if self._mode == MODE_CURVE:
                    return
                if self._mode == MODE_ORTHO:
                    self._set_mode(MODE_FREEHAND)
                else:
                    # Re-align ortho angle from the last drawn segment
                    if len(self.points) >= 2:
                        baseline = _seg_angle(self.points[-2], self.points[-1])
                        self._ortho_angle = baseline + math.pi / 2
                    self._set_mode(MODE_ORTHO)

    # ─────────────────────────────────────────────────────────────────────────
    # Mode management
    # ─────────────────────────────────────────────────────────────────────────

    def _set_mode(self, new_mode: str):
        self._mode = new_mode
        self._show_status(_MODE_LABELS[new_mode])
        self._refresh_preview_colors()
        print(f"🔄 Ortho-Polygon mode → {new_mode}")

    def _get_tool_style(self) -> dict:
        """Return the configured draw style for the Ortho tool."""
        style = self.dig._get_draw_style(self.TOOL_NAME)
        return {
            'color': tuple(style.get('color', (1.0, 0.0, 0.0))),
            'width': int(style.get('width', 2) or 2),
            'style': str(style.get('style', 'solid') or 'solid'),
        }

    def _refresh_preview_colors(self):
        """Update preview actors when draw settings change."""
        color = self._get_tool_style()['color']
        for attr in (_ATTR_EDGE, _ATTR_POLY):
            actor = getattr(self.dig, attr, None)
            if actor is not None:
                try:
                    actor.GetProperty().SetColor(*color)
                    actor.Modified()
                except Exception:
                    pass

    # ─────────────────────────────────────────────────────────────────────────
    # Vertex placement  (mirrors _left_click / _add_ortho_point / _add_curve_point)
    # ─────────────────────────────────────────────────────────────────────────

    def _left_click(self, cursor: tuple):
        self._push_undo_state()
        if self._mode == MODE_ORTHO:
            self._add_ortho_point(cursor)
        elif self._mode == MODE_FREEHAND:
            self.points.append(cursor)
            self._add_vertex_marker(cursor)
            self._update_poly_actor()
        else:  # CURVE
            self._add_curve_point(cursor)

    # ── Orthogonal mode ───────────────────────────────────────────────────────

    def _add_ortho_point(self, cursor: tuple):
        """
        Mirrors _add_ortho_point() from the QGIS original:
          P1, P2 → raw cursor position (baseline definition)
          P3+    → projected onto the current orthogonal axis;
                   axis is then rotated 90° for the next segment.
        """
        if len(self.points) < 2:
            self.points.append(cursor)
            self._add_vertex_marker(cursor)
            if len(self.points) == 2:
                baseline = _seg_angle(self.points[0], self.points[1])
                self._ortho_angle = baseline + math.pi / 2
        else:
            snapped = _project_ray(self.points[-1], self._ortho_angle, cursor)
            self.points.append(snapped)
            self._add_vertex_marker(snapped)
            self._ortho_angle += math.pi / 2   # 90° rotation for next segment

        self._update_poly_actor()

    # ── Curve mode ────────────────────────────────────────────────────────────

    def _add_curve_point(self, cursor: tuple):
        """
        Mirrors _add_curve_point() from the QGIS original.

        Step 0 → user clicks arc mid-point (saved but not yet committed)
        Step 1 → user clicks arc end-point → arc is densified and committed
        """
        if not self.points:
            self.points.append(cursor)
            self._add_vertex_marker(cursor)
            self._update_poly_actor()
            return

        if self._curve_step == 0:
            self._curve_start = self.points[-1]
            self._curve_mid   = cursor
            self._curve_step  = 1
            self._show_status("🟣 Curve: now click the arc END-POINT")

        elif self._curve_step == 1:
            arc_pts = _arc_to_polyline(self._curve_start, self._curve_mid, cursor)
            # First arc point == self.points[-1] (already stored) → skip it
            for pt in arc_pts[1:]:
                self.points.append(pt)
                self._add_vertex_marker(pt)
            self._reset_curve_state()
            self._update_poly_actor()
            self._show_status(_MODE_LABELS[MODE_CURVE])

    def _reset_curve_state(self):
        self._curve_step  = 0
        self._curve_start = None
        self._curve_mid   = None

    # ─────────────────────────────────────────────────────────────────────────
    # Live preview rendering
    # ─────────────────────────────────────────────────────────────────────────

    def _update_preview(self, cursor: tuple):
        """
        Mirrors _update_preview() from the QGIS original.

        Two preview actors (stored on the digitizer so _remove_preview_actor_2d
        can clean them up using the standard pipeline):
          _ortho_edge_preview – dashed line from last vertex to live cursor
          _ortho_poly_preview – solid outline of the growing polygon ring
        """
        style = self._get_tool_style()
        color = style['color']
        width = style['width']
        line_style = style['style']


        # ── Constrained live point ────────────────────────────────────────────
        if self._mode == MODE_ORTHO and self._ortho_angle is not None:
            live_pt = _project_ray(self.points[-1], self._ortho_angle, cursor)
        else:
            live_pt = cursor

        # ── Dashed live-edge segment (or arc preview in curve step 1) ─────────
        if self._mode == MODE_CURVE and self._curve_step == 1:
            try:
                edge_pts = _arc_to_polyline(self._curve_start, self._curve_mid, live_pt)
            except Exception:
                edge_pts = [self.points[-1], live_pt]
        else:
            edge_pts = ([self.points[-1], live_pt]
                        if self.points else [live_pt, live_pt])

        self.dig._update_continuous_line_world(
            _ATTR_EDGE,
            edge_pts,
            color=color,
            width=width,
            line_style='dashed',
        )

        ghost_pts = list(self.points) + [live_pt]
        if len(ghost_pts) >= 2:
            closed_ghost = ghost_pts + [ghost_pts[0]]
            self.dig._update_continuous_line_world(
                _ATTR_POLY,
                closed_ghost,
                color=color,
                width=width,
                line_style=line_style,
            )

    def _update_poly_actor(self):
        """Redraw the committed polygon outline after a new vertex is confirmed."""
        if len(self.points) < 2:
            return
        style = self._get_tool_style()
        color = style['color']
        width = style['width']
        line_style = style['style']
        closed = list(self.points) + [self.points[0]]
        self.dig._update_continuous_line_world(
            _ATTR_POLY,
            closed,
            color=color,
            width=width,
            line_style=line_style,
        )


    def _clear_preview(self):
        """Remove both transient preview actors from the overlay renderer."""
        self.dig._remove_preview_actor_2d(_ATTR_EDGE)
        self.dig._remove_preview_actor_2d(_ATTR_POLY)

    def _deferred_preview_refresh(self):
        """Called after a pan/zoom so the preview geometry stays aligned."""
        if self.points:
            cursor = self._pick_world()
            self._update_preview(cursor)
            self.dig.app.vtk_widget.render()

    # ─────────────────────────────────────────────────────────────────────────
    # Vertex sphere markers
    # ─────────────────────────────────────────────────────────────────────────

    def _add_vertex_marker(self, pt: tuple):
        """Add a coloured sphere at a confirmed vertex."""
        color    = self._get_tool_style()['color']
        is_first = len(self._vertex_actors) == 0
        # First vertex is green (matches digitize_tools convention), rest use the tool color
        m_color  = (0.0, 1.0, 0.0) if is_first else color
        marker   = self.dig._add_endpoint_sphere(pt, color=m_color, radius=0.05)
        try:
            marker.GetProperty().SetOpacity(0.85)
        except Exception:
            pass
        self._vertex_actors.append(marker)

    def _clear_vertex_markers(self):
        """Remove all vertex sphere markers from the overlay renderer."""
        for m in self._vertex_actors:
            try:
                self.dig._remove_actor_from_overlay(m)
            except Exception:
                pass
        self._vertex_actors = []

    # ─────────────────────────────────────────────────────────────────────────
    # Polygon completion  (mirrors _finish_polygon / _cancel from the original)
    # ─────────────────────────────────────────────────────────────────────────

    def _finish_polygon(self):
        """
        Right-click: close and commit the polygon.

        ORTHO mode  →  compute a closing vertex at the intersection of
                       - Line A : last_pt + s·(current ortho direction)
                       - Line B : P1      + t·(baseline P1→P2 direction)
                       guaranteeing a right angle at both the last vertex and P1.
                       Falls back to P1 if lines are parallel (degenerate case).

        FREEHAND / CURVE  →  straight line back to P1 (no special vertex).
        """
        if len(self.points) < 3:
            self._show_status("⚠️ Need at least 3 points to close the polygon")
            return

        self.dig._save_state()

        ring = list(self.points)   # mutable working copy

        # ── Ortho auto-close (Cramer's rule) ─────────────────────────────────
        if self._mode == MODE_ORTHO and self._ortho_angle is not None:
            last_pt      = self.points[-1]
            od_x, od_y   = _unit_vec(self._ortho_angle)
            baseline_ang = _seg_angle(self.points[0], self.points[1])
            bd_x, bd_y   = _unit_vec(baseline_ang)

            closing = _line_intersect(last_pt,       od_x, od_y,
                                      self.points[0], bd_x, bd_y)
            if closing is None:
                # Parallel lines (degenerate) — snap to P1 (same as QGIS original)
                closing = self.points[0]
                self._show_status("⚠️ Parallel closing lines — snapped to P1")
                print("⚠️ Ortho closing: parallel lines, snapping to P1")
            ring.append(closing)

        ring.append(ring[0])   # close the ring (last == first)

        style  = self._get_tool_style()
        color  = style['color']
        width  = style.get('width', 2)
        lstyle = style.get('style', 'solid')
        self._clear_preview()   # remove transient preview before adding final actor
        actor = self.dig._make_polyline_actor(ring, color=color,
                                              width=width, line_style=lstyle)
        actor.PickableOn()
        self.dig._add_actor_to_overlay(actor)

        # Vertex markers become permanent (adopted by the drawing entry)
        perm_markers     = list(self._vertex_actors)
        self._vertex_actors = []   # prevent deactivate() from removing them

        entry = {
            'type':           'polygon',
            'subtype':        f'ortho_{self._mode}',
            'coords':         ring,
            'actor':          actor,
            'bounds':         actor.GetBounds(),
            'vertex_markers': perm_markers,
            'original_color': color,
            'original_width': width,
            'original_style': lstyle,
        }
        self.dig.drawings.append(entry)

        # Hide vertex markers immediately on finalization.
        for m in perm_markers:
            try:
                m.VisibilityOff()
            except Exception:
                pass

        n_verts = len(ring) - 1
        msg     = f"✅ Polygon committed — {n_verts} vertices, mode: {self._mode}"
        self._show_status(msg)
        print(f"✅ OrthoPoly finalized: {n_verts} vertices, mode={self._mode}")

        # ── Reset for next polygon (permanent mode keeps tool active) ─────────
        self.dig.clear_coordinate_labels()
        self._clear_edit_history()
        self._reset_state()
        self._clear_vertex_markers()   # safety: clears anything left

        # Honour permanent-mode flag (default: True — stay active like other tools)
        if not getattr(self.dig, 'orthopolygon_permanent_mode', True):
            self.deactivate()

    def _cancel(self):
        """Escape: discard the in-progress polygon (mirrors _cancel())."""
        self._clear_preview()
        self._clear_vertex_markers()
        self._clear_edit_history()
        self._reset_state()
        self._show_status("❌ Ortho-polygon cancelled — polygon discarded")
        print("❌ OrthogonalPolygonTool: polygon cancelled")
        try:
            self.dig.app.vtk_widget.render()
        except Exception:
            pass

    def _reset_state(self):
        """Reset all mutable drawing state (mirrors _reset() from the original)."""
        self.points        = []
        self._ortho_angle  = None
        self._mode         = MODE_ORTHO
        self._prev_mode    = MODE_ORTHO
        self._reset_curve_state()
        self._sync_digitizer_temp_points()
        # Keep the tool active (observers remain), only state is cleared.

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _sync_digitizer_temp_points(self):
        """Keep the shared digitizer temp_points reference aligned with this tool."""
        self.dig.temp_points = self.points

    def _capture_edit_state(self) -> dict:
        """Capture the active Ortho tool edit state for Ctrl+Z / Ctrl+Y."""
        return {
            "points": list(self.points),
            "mode": self._mode,
            "prev_mode": self._prev_mode,
            "ortho_angle": self._ortho_angle,
            "curve_step": self._curve_step,
            "curve_start": self._curve_start,
            "curve_mid": self._curve_mid,
        }

    def _restore_edit_state(self, state: dict):
        """Restore an in-progress Ortho polygon after point-level undo/redo."""
        self.points = list(state.get("points", []))
        self._mode = state.get("mode", MODE_ORTHO)
        self._prev_mode = state.get("prev_mode", MODE_ORTHO)
        self._ortho_angle = state.get("ortho_angle")
        self._curve_step = state.get("curve_step", 0)
        self._curve_start = state.get("curve_start")
        self._curve_mid = state.get("curve_mid")
        self._sync_digitizer_temp_points()

        self._clear_preview()
        self._clear_vertex_markers()
        for pt in self.points:
            self._add_vertex_marker(pt)

        if self.points:
            self._update_preview(self._pick_world())

        self._show_status(self._current_status_text())
        try:
            self.dig.app.vtk_widget.render()
        except Exception:
            pass

    def _push_undo_state(self):
        """Store the current edit before a new click mutates the polygon."""
        self._undo_states.append(self._capture_edit_state())
        max_steps = max(1, int(getattr(self.dig, "max_undo_levels", 50) or 50))
        if len(self._undo_states) > max_steps:
            self._undo_states.pop(0)
        self._redo_states = []

    def _clear_edit_history(self):
        """Drop temporary point-level undo/redo history for the current polygon."""
        self._undo_states = []
        self._redo_states = []

    def _current_status_text(self) -> str:
        """Return the correct status message for the current sub-mode."""
        if self._mode == MODE_CURVE and self._curve_step == 1:
            return "Curve: now click the arc END-POINT"
        return _MODE_LABELS.get(self._mode, _MODE_LABELS[MODE_ORTHO])

    def undo(self) -> bool:
        """Undo the last active Ortho-tool edit. Returns True when handled."""
        if not self._undo_states:
            return False

        self._redo_states.append(self._capture_edit_state())
        max_steps = max(1, int(getattr(self.dig, "max_undo_levels", 50) or 50))
        if len(self._redo_states) > max_steps:
            self._redo_states.pop(0)

        self._restore_edit_state(self._undo_states.pop())
        print(f"Ortho undo ({len(self.points)} active points)")
        return True

    def redo(self) -> bool:
        """Redo the last active Ortho-tool edit. Returns True when handled."""
        if not self._redo_states:
            return False

        self._undo_states.append(self._capture_edit_state())
        max_steps = max(1, int(getattr(self.dig, "max_undo_levels", 50) or 50))
        if len(self._undo_states) > max_steps:
            self._undo_states.pop(0)

        self._restore_edit_state(self._redo_states.pop())
        print(f"Ortho redo ({len(self.points)} active points)")
        return True

    def _pick_world(self) -> tuple:
        """
        Convert current mouse screen position to world coordinates.
        Uses the digitizer's existing picker (vtkWorldPointPicker) with Z-consistency.
        """
        pos = self.dig._get_mouse_world()
        return (float(pos[0]), float(pos[1]), float(pos[2]))

    def _show_status(self, msg: str):
        """Push a message to the application status bar (safe if unavailable)."""
        try:
            self.dig.app.statusBar().showMessage(msg)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: activate directly without going through set_tool
# ─────────────────────────────────────────────────────────────────────────────

def activate_ortho_polygon_tool(digitizer) -> 'OrthogonalPolygonTool':
    """
    Convenience wrapper — equivalent to:
        digitizer.set_tool('orthopolygon')

    Usage from anywhere in the application:
        from gui.orthogonal_polygon_tool import activate_ortho_polygon_tool
        _tool_ref = activate_ortho_polygon_tool(self.digitizer)

    The returned reference keeps the tool alive; store it if needed.
    """
    # Deactivate any existing instance
    old = getattr(digitizer, '_ortho_polygon_tool', None)
    if old is not None:
        try:
            old.deactivate()
        except Exception:
            pass

    tool = OrthogonalPolygonTool(digitizer)
    tool.activate()
    digitizer._ortho_polygon_tool = tool
    return tool
