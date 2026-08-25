from __future__ import annotations
import math
import mmap
import os
import struct
import traceback
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import numpy as np

from PySide6.QtCore import (
    QCoreApplication, Qt, QThread, Signal, QEvent,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog,
    QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QRadioButton, QScrollArea,
    QVBoxLayout, QWidget, QProgressDialog,
)

from gui.theme_manager import (
    get_dialog_stylesheet, get_progress_dialog_stylesheet,
    get_title_banner_style, get_file_item_row_style,
    get_badge_style, get_icon_button_style, get_notice_banner_style, ThemeColors,
)

_LEGACY_MAGIC_V0: bytes = b"SNT\x00"
_LEGACY_MAGIC_V1: bytes = b"SNT\x01"

_LEGACY_ETYPE_POLYLINE: int = 0
_LEGACY_ETYPE_TEXT:     int = 1
_LEGACY_ETYPE_3DFACE:   int = 2
_DEFAULT_COLOR: Tuple[int, int, int] = (0, 255, 200)
_LAYER_COLOR_CYCLE: List[Tuple[int, int, int]] = [
    (  0, 255,  80),
    (  0, 220, 255),
    (255, 220,  50),
    (255,  80, 100),
    (200, 130, 255),
    (255, 160,  60),
    (100, 255, 150),
]
_ACI_RGB: Dict[int, Tuple[int, int, int]] = {
      1: (255,   0,   0),    2: (255, 255,   0),    3: (  0, 255,   0),
      4: (  0, 255, 255),    5: (  0,   0, 255),    6: (255,   0, 255),
      7: (255, 255, 255),    8: (128, 128, 128),    9: (192, 192, 192),
     10: (255,   0,   0),   20: (255, 127,   0),   30: (255, 191,   0),
     40: (255, 255,   0),   50: (127, 255,   0),   60: (  0, 255,   0),
     70: (  0, 255, 127),   80: (  0, 255, 255),   90: (  0, 127, 255),
    100: (  0,   0, 255),  110: (127,   0, 255),  120: (255,   0, 255),
    130: (255,   0, 127),  140: (255, 127, 127),  150: (255, 200, 127),
    160: (255, 255, 127),  170: (200, 255, 127),  180: (127, 255, 127),
    190: (127, 255, 200),  200: (127, 255, 255),  210: (127, 200, 255),
    220: (127, 127, 255),  230: (200, 127, 255),  240: (255, 127, 255),
    250: (  0,   0,   0),  251: ( 42,  42,  42),  252: ( 84,  84,  84),
    253: (127, 127, 127),  254: (170, 170, 170),  255: (255, 255, 255),
}

_INVISIBLE_ACI: Set[int] = {0, 7, 256}

_ARC_SEGMENTS:    int = 32
_CIRCLE_SEGMENTS: int = 36


def _snt_point_in_polygon_2d(px: float, py: float, polygon: list) -> bool:
    """Ray casting 2D point-in-polygon test (XY plane)."""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def build_snt_block_polygons(app, entities: list, snt_filename: str) -> None:
    """Extract BL-layer block polygons and associate each with a grid label for click-inside-block detection.

    Text labels that are file names reside on the 'FileNames' layer (promoted from FeatureAttribs).
    We prefer those over generic text so the block click uses the correct LAZ file name.
    """
    if not hasattr(app, 'snt_block_polygons'):
        app.snt_block_polygons = []

    bl_polys: list = []
    # Separate file-name labels (FileNames/BLOCKS layer) from generic text
    filename_text: list = []   # (x, y, text_value)  — highest priority
    other_text: list = []      # (x, y, text_value)  — fallback

    for e in entities:
        etype = e.get("type", "").lower()
        layer = e.get("layer", "")

        if etype in ("line", "polyline") and layer == "BL":
            pts = e.get("points", [])
            if len(pts) >= 3:
                bl_polys.append([(float(p[0]), float(p[1])) for p in pts])

        elif etype == "text":
            pos = e.get("position", (0.0, 0.0, 0.0))
            text_val = str(e.get("text", "")).strip()
            if text_val and len(pos) >= 2:
                entry = (float(pos[0]), float(pos[1]), text_val)
                if layer in ("FileNames", "BLOCKS"):
                    filename_text.append(entry)
                else:
                    other_text.append(entry)

    print(f"[SNT block polygons] {snt_filename}: {len(bl_polys)} BL polys, "
          f"{len(filename_text)} FileNames labels, {len(other_text)} other text")

    for poly_pts in bl_polys:
        grid_name = None
        # Prefer FileNames/BLOCKS layer labels (actual file names)
        for tx, ty, tval in filename_text:
            if _snt_point_in_polygon_2d(tx, ty, poly_pts):
                grid_name = tval
                break
        # Fall back to any text label inside the polygon
        if grid_name is None:
            for tx, ty, tval in other_text:
                if _snt_point_in_polygon_2d(tx, ty, poly_pts):
                    grid_name = tval
                    break
        app.snt_block_polygons.append({
            "points_2d": poly_pts,
            "grid_name": grid_name,
            "snt_filename": snt_filename,
        })

    print(f"[SNT block polygons] stored {len(bl_polys)} block entries "
          f"({sum(1 for b in app.snt_block_polygons[-len(bl_polys):] if b['grid_name'])} with grid_name)")

def _aci_to_rgb(aci: int, cycle_idx: int = 0) -> Tuple[int, int, int]:
    if aci in _INVISIBLE_ACI:
        return _LAYER_COLOR_CYCLE[cycle_idx % len(_LAYER_COLOR_CYCLE)]
    if aci in _ACI_RGB:
        return _ACI_RGB[aci]
    return _LAYER_COLOR_CYCLE[aci % len(_LAYER_COLOR_CYCLE)]


def _normalise_vtk_color(color: Tuple[int, int, int]) -> List[float]:
    return [c / 255.0 for c in color]

def _score_label(text: str) -> int:
    """
    Return a priority score for a text string.

    Score  100 : grid ID or block name  (separator + 4+ digits)
                 e.g. "DW2039017_000347", "GR_0012_005", "MASTROMAURO+SA2000001"
    Score   50 : separator + some digits  (e.g. "BLOCK_ABC_123")
    Score   10 : has any digit (generic label)
    Score    0 : skip - not a useful label

    FIX (block-name visibility): treat '+' as equivalent to '_' as a word
    separator so that DGN block names like "MASTROMAURO+SA2000001" score 100
    (HIGH priority) instead of 10 (LOW), ensuring they survive label decimation.
    """
    t = text.strip()
    if len(t) < 5:
        return 0
    has_digit     = any(c.isdigit() for c in t)
    # Treat both '_' and '+' as valid name separators (covers DGN block names
    # that export with '+' rather than '_', e.g. "MASTROMAURO+SA2000001")
    has_separator = '_' in t or '+' in t

    if not has_digit and not has_separator:
        return 10

    # HIGH: separator + 4+ digits -> grid IDs and DGN block names
    if has_separator and sum(c.isdigit() for c in t) >= 4:
        return 100
    # MEDIUM: separator + some digits
    if has_separator and has_digit:
        return 50
    # LOW: just has digits
    if has_digit:
        return 10
    return 0


def _label_color_and_height(
    text: str,
) -> Tuple[Tuple[int, int, int], float]:
    """
    Return (vtk_color, text_height) for a label — mirrors DXF smart colour
    selection in process_entity INSERT block.

    The DXF code uses:
      • Cyan   (0,255,255) + height 3.0  →  grid IDs  (underscore + many digits)
      • Yellow (255,255,0) + height 2.5  →  feature names  (everything else)

    We apply the same split so SNT labels look identical to DXF labels.
    """
    score = _score_label(text)
    if score >= 100:
        # High-confidence grid ID → cyan
        return (0, 255, 255), 3.0
    else:
        # Feature / generic label → yellow
        return (255, 255, 0), 2.5
    
@lru_cache(maxsize=262144)
def _score_label_cached(text: str) -> int:
    return _score_label(text)


@lru_cache(maxsize=262144)
def _label_color_and_height_cached(text: str) -> Tuple[Tuple[int, int, int], float]:
    return _label_color_and_height(text)


def _snt_enable_gl_point_size(app) -> bool:
    """
    Synchronously enable GL_PROGRAM_POINT_SIZE (0x8642) and install the
    persistent StartEvent observer so it stays enabled across future renders.
    """
    try:
        rw = app.vtk_widget.GetRenderWindow()
        if rw is None:
            return False
        state = rw.GetState() if hasattr(rw, 'GetState') else None
        if state and hasattr(state, 'vtkglEnable'):
            state.vtkglEnable(0x8642)

            try:
                from gui.unified_actor_manager import (
                    _install_program_point_size_observer,
                    _try_enable_program_point_size,
                )
                ua = getattr(app, '_unified_actor', None)
                if ua is not None:
                    _try_enable_program_point_size(rw)
                    _install_program_point_size_observer(ua, rw)
            except Exception:
                pass

            return True
    except Exception as e:
        print(f"  [warn] _snt_enable_gl_point_size: {e}")
    return False


def _snt_push_border_uniforms(app) -> None:
    """
    Push weight_lut / visibility_lut / border_ring_val uniforms to the
    unified point-cloud actor right now, before the impending render.
    """
    try:
        from gui.unified_actor_manager import _push_uniforms_direct
        ua = getattr(app, '_unified_actor', None)
        if ua is None:
            return
        ctx = getattr(ua, '_naksha_shader_ctx', None)
        if ctx is None:
            return
        _push_uniforms_direct(ua, ctx)
    except Exception as e:
        print(f"  [warn] _snt_push_border_uniforms: {e}")


def _get_snt_z_offset(app):

    try:
        if hasattr(app, 'data') and app.data is not None and 'xyz' in app.data:
            # Try to get from app cache first (populated during load)
            bounds = getattr(app, 'data_bounds', None)
            if bounds is not None:
                z_min, z_max = bounds[4], bounds[5]
            else:
                # One-time fallback if bounds missing
                z_vals = app.data['xyz'][:, 2]
                z_min, z_max = float(z_vals.min()), float(z_vals.max())
                app.data_bounds = [0,0,0,0, z_min, z_max] # Partial cache
                
            z_range = z_max - z_min
            offset = z_max + max(z_range * 0.5, 50.0)
            return offset
    except Exception:
        pass
    return 0.0


def _apply_z_offset_to_actor(actor, new_offset: float):
    old_offset = getattr(actor, '_snt_z_offset', 0.0)
    delta = new_offset - old_offset
    if abs(delta) > 0.001:
        actor.AddPosition(0, 0, delta)
        actor._snt_z_offset = new_offset

def _decode_point_body(body, layer, color):
    if len(body) < 12:
        return None
    try:
        x, y, z = struct.unpack_from("<3f", body)
        return {"type": "POINT", "layer": layer, "color": color,
                "position": (float(x), float(y), float(z))}
    except struct.error:
        return None


def _decode_line_body(body, layer, color):
    if len(body) < 24:
        return None
    try:
        sx, sy, sz, ex, ey, ez = struct.unpack_from("<6f", body)
        return {"type": "POLYLINE", "layer": layer, "color": color,
                "vertices": [(float(sx), float(sy), float(sz)),
                             (float(ex), float(ey), float(ez))],
                "closed": False}
    except struct.error:
        return None


def _decode_arc_body(body, layer, color):
    if len(body) < 24:
        return None
    try:
        cx, cy, cz, r, sa, ea = struct.unpack_from("<6f", body)
        sa_r = math.radians(float(sa))
        ea_r = math.radians(float(ea))
        if ea_r <= sa_r:
            ea_r += 2.0 * math.pi
        verts = [
            (float(cx) + float(r) * math.cos(sa_r + (ea_r - sa_r) * i / _ARC_SEGMENTS),
             float(cy) + float(r) * math.sin(sa_r + (ea_r - sa_r) * i / _ARC_SEGMENTS),
             float(cz))
            for i in range(_ARC_SEGMENTS + 1)
        ]
        return {"type": "POLYLINE", "layer": layer, "color": color,
                "vertices": verts, "closed": False}
    except struct.error:
        return None


def _decode_circle_body(body, layer, color):
    if len(body) < 16:
        return None
    try:
        cx, cy, cz, r = struct.unpack_from("<4f", body)
        verts = [
            (float(cx) + float(r) * math.cos(2.0 * math.pi * i / _CIRCLE_SEGMENTS),
             float(cy) + float(r) * math.sin(2.0 * math.pi * i / _CIRCLE_SEGMENTS),
             float(cz))
            for i in range(_CIRCLE_SEGMENTS + 1)
        ]
        return {"type": "POLYLINE", "layer": layer, "color": color,
                "vertices": verts, "closed": True}
    except struct.error:
        return None


def _decode_lwpolyline_body(body, layer, color):
    if len(body) < 9:
        return None
    try:
        elev, flags, count = struct.unpack_from("<fBI", body)
        if len(body) < 9 + count * 12:
            return None
        verts = []
        pos = 9
        for _ in range(count):
            x, y, _bulge = struct.unpack_from("<3f", body, pos)
            verts.append((float(x), float(y), float(elev)))
            pos += 12
        if not verts:
            return None
        return {"type": "POLYLINE", "layer": layer, "color": color,
                "vertices": verts, "closed": bool(flags & 0x01)}
    except struct.error:
        return None


def _decode_polyline_body(body, layer, color):
    if len(body) < 5:
        return None
    try:
        flags, count = struct.unpack_from("<BI", body)
        if len(body) < 5 + count * 12:
            return None
        verts = []
        pos = 5
        for _ in range(count):
            x, y, z = struct.unpack_from("<3f", body, pos)
            verts.append((float(x), float(y), float(z)))
            pos += 12
        if not verts:
            return None
        return {"type": "POLYLINE", "layer": layer, "color": color,
                "vertices": verts, "closed": bool(flags & 0x01)}
    except struct.error:
        return None


def _decode_text_body(body, layer, color, string_fn):
    if len(body) < 28:
        return None
    try:
        ix, iy, iz, height, _rot, str_idx, _style = struct.unpack_from("<5fII", body)
        text = string_fn(str_idx)
        if not text or not text.strip():
            return None
        return {"type": "TEXT", "layer": layer, "color": color,
                "text": text,
                "position": (float(ix), float(iy), float(iz)),
                "height": float(height) if height > 0.0 else 1.0}
    except struct.error:
        return None


def _decode_insert_body(body, layer, color, string_fn):
    """
    Decode INSERT body as a label anchor when full block geometry is unavailable.

    Observed v1.2 payload layout:
      <3f4fI> -> insert(x,y,z), scale(x,y,z), rotation, name_str_idx
    """
    if len(body) < 32:
        return None
    try:
        ix, iy, iz, sx, sy, _sz, _rot, name_idx = struct.unpack_from("<7fI", body)
        text = string_fn(name_idx)
        if not text or not str(text).strip():
            return None
        scale = max(abs(float(sx)), abs(float(sy)), 1.0)
        return {
            "type": "TEXT",
            "layer": layer,
            "color": color,
            "text": str(text).strip(),
            "position": (float(ix), float(iy), float(iz)),
            "height": max(1.0, 2.5 * scale),
        }
    except struct.error:
        return None


def _decode_face3d_body(body, layer, color):
    if len(body) < 48:
        return None
    try:
        verts = [tuple(struct.unpack_from("<3f", body, i * 12)) for i in range(4)]
        return {"type": "3DFACE", "layer": layer, "color": color,
                "vertices": [(float(v[0]), float(v[1]), float(v[2])) for v in verts]}
    except struct.error:
        return None


def _is_native_batch_enabled() -> bool:
    flag = os.getenv("NAKSHA_SNT_NATIVE_BATCH", "1").strip().lower()
    return flag not in {"0", "false", "no", "off"}


def _resolve_entity_color(hdr, layer_colors, color_mode_cls) -> Tuple[int, int, int]:
    if hdr.color_mode == color_mode_cls.BYLAYER:
        idx = hdr.layer_idx
        return layer_colors[idx] if 0 <= idx < len(layer_colors) else _DEFAULT_COLOR
    if hdr.color_mode == color_mode_cls.ACI:
        return _aci_to_rgb(hdr.color_value & 0xFF, hdr.layer_idx)
    r = (hdr.color_value >> 16) & 0xFF
    g = (hdr.color_value >> 8) & 0xFF
    b = hdr.color_value & 0xFF
    return (r, g, b)


def _decode_entity_from_body(etype, body: bytes, layer_name: str, color, entity_type_cls, string_fn):
    if etype == entity_type_cls.POINT:
        return _decode_point_body(body, layer_name, color)
    if etype == entity_type_cls.LINE:
        return _decode_line_body(body, layer_name, color)
    if etype == entity_type_cls.ARC:
        return _decode_arc_body(body, layer_name, color)
    if etype == entity_type_cls.CIRCLE:
        return _decode_circle_body(body, layer_name, color)
    if etype == entity_type_cls.LWPOLYLINE:
        return _decode_lwpolyline_body(body, layer_name, color)
    if etype == entity_type_cls.POLYLINE:
        return _decode_polyline_body(body, layer_name, color)
    if etype in (entity_type_cls.TEXT, entity_type_cls.MTEXT,
                 entity_type_cls.DIMENSION, entity_type_cls.LEADER):
        return _decode_text_body(body, layer_name, color, string_fn)
    if etype == entity_type_cls.INSERT:
        return _decode_insert_body(body, layer_name, color, string_fn)
    if etype in (entity_type_cls.SOLID, entity_type_cls.FACE3D):
        return _decode_face3d_body(body, layer_name, color)
    return None


def _decode_entity_from_mmap(etype, mm_obj, body_offset: int, body_size: int,
                             layer_name: str, color, entity_type_cls, string_fn):
    """
    Fast-path decode using mmap slices from offsets produced by native SntReader.
    Returns None if the entity is malformed or unsupported.
    """
    if body_offset < 0 or body_size <= 0:
        return None
    if body_offset + body_size > len(mm_obj):
        return None

    try:
        if etype == entity_type_cls.POINT:
            if body_size < 12:
                return None
            x, y, z = struct.unpack_from("<3f", mm_obj, body_offset)
            return {"type": "POINT", "layer": layer_name, "color": color,
                    "position": (float(x), float(y), float(z))}

        if etype == entity_type_cls.LINE:
            if body_size < 24:
                return None
            sx, sy, sz, ex, ey, ez = struct.unpack_from("<6f", mm_obj, body_offset)
            return {"type": "POLYLINE", "layer": layer_name, "color": color,
                    "vertices": [(float(sx), float(sy), float(sz)),
                                 (float(ex), float(ey), float(ez))],
                    "closed": False}

        if etype == entity_type_cls.ARC:
            if body_size < 24:
                return None
            cx, cy, cz, r, sa, ea = struct.unpack_from("<6f", mm_obj, body_offset)
            sa_r = math.radians(float(sa))
            ea_r = math.radians(float(ea))
            if ea_r <= sa_r:
                ea_r += 2.0 * math.pi
            verts = [
                (float(cx) + float(r) * math.cos(sa_r + (ea_r - sa_r) * i / _ARC_SEGMENTS),
                 float(cy) + float(r) * math.sin(sa_r + (ea_r - sa_r) * i / _ARC_SEGMENTS),
                 float(cz))
                for i in range(_ARC_SEGMENTS + 1)
            ]
            return {"type": "POLYLINE", "layer": layer_name, "color": color,
                    "vertices": verts, "closed": False}

        if etype == entity_type_cls.CIRCLE:
            if body_size < 16:
                return None
            cx, cy, cz, r = struct.unpack_from("<4f", mm_obj, body_offset)
            verts = [
                (float(cx) + float(r) * math.cos(2.0 * math.pi * i / _CIRCLE_SEGMENTS),
                 float(cy) + float(r) * math.sin(2.0 * math.pi * i / _CIRCLE_SEGMENTS),
                 float(cz))
                for i in range(_CIRCLE_SEGMENTS + 1)
            ]
            return {"type": "POLYLINE", "layer": layer_name, "color": color,
                    "vertices": verts, "closed": True}

        if etype == entity_type_cls.LWPOLYLINE:
            if body_size < 9:
                return None
            elev, flags, count = struct.unpack_from("<fBI", mm_obj, body_offset)
            if count <= 0:
                return None
            expected = 9 + count * 12
            if body_size < expected:
                return None
            arr = np.frombuffer(mm_obj, dtype=np.dtype("<f4"), count=count * 3, offset=body_offset + 9)
            arr = arr.reshape((-1, 3))
            verts = [(float(x), float(y), float(elev)) for x, y, _ in arr]
            return {"type": "POLYLINE", "layer": layer_name, "color": color,
                    "vertices": verts, "closed": bool(flags & 0x01)}

        if etype == entity_type_cls.POLYLINE:
            if body_size < 5:
                return None
            flags, count = struct.unpack_from("<BI", mm_obj, body_offset)
            if count <= 0:
                return None
            expected = 5 + count * 12
            if body_size < expected:
                return None
            arr = np.frombuffer(mm_obj, dtype=np.dtype("<f4"), count=count * 3, offset=body_offset + 5)
            arr = arr.reshape((-1, 3))
            verts = [(float(x), float(y), float(z)) for x, y, z in arr]
            return {"type": "POLYLINE", "layer": layer_name, "color": color,
                    "vertices": verts, "closed": bool(flags & 0x01)}

        if etype in (entity_type_cls.TEXT, entity_type_cls.MTEXT,
                     entity_type_cls.DIMENSION, entity_type_cls.LEADER):
            if body_size < 28:
                return None
            ix, iy, iz, height, _rot, str_idx, _style = struct.unpack_from(
                "<5fII", mm_obj, body_offset
            )
            text = string_fn(str_idx)
            if not text or not text.strip():
                return None
            return {"type": "TEXT", "layer": layer_name, "color": color,
                    "text": text,
                    "position": (float(ix), float(iy), float(iz)),
                    "height": float(height) if height > 0.0 else 1.0}

        if etype == entity_type_cls.INSERT:
            if body_size < 32:
                return None
            ix, iy, iz, sx, sy, _sz, _rot, name_idx = struct.unpack_from(
                "<7fI", mm_obj, body_offset
            )
            text = string_fn(name_idx)
            if not text or not str(text).strip():
                return None
            scale = max(abs(float(sx)), abs(float(sy)), 1.0)
            return {"type": "TEXT", "layer": layer_name, "color": color,
                    "text": str(text).strip(),
                    "position": (float(ix), float(iy), float(iz)),
                    "height": max(1.0, 2.5 * scale)}

        if etype in (entity_type_cls.SOLID, entity_type_cls.FACE3D):
            if body_size < 48:
                return None
            arr = np.frombuffer(mm_obj, dtype=np.dtype("<f4"), count=12, offset=body_offset).reshape((4, 3))
            verts = [(float(x), float(y), float(z)) for x, y, z in arr]
            return {"type": "3DFACE", "layer": layer_name, "color": color,
                    "vertices": verts}

    except (struct.error, ValueError, OverflowError):
        return None

    return None


def _promote_featureattrib_text_layer(ent: Dict, src_layer_name: str, src_color,
                                      layers_out: List[Dict], layer_name_set: Set[str]) -> None:
    """
    Keep existing split behavior:
    high-confidence labels from FeatureAttribs move to FileNames layer.
    """
    if src_layer_name != "FeatureAttribs":
        return
    text_val = ent.get("text")
    if not text_val:
        return

    is_yellow = (src_color == (255, 255, 0))
    is_cyan = (src_color == (0, 255, 255))
    if is_cyan or (not is_yellow and _score_label_cached(str(text_val)) >= 100):
        new_lname = "FileNames"
        ent["layer"] = new_lname
        if new_lname not in layer_name_set:
            layers_out.append({"name": new_lname, "color": (0, 255, 255)})
            layer_name_set.add(new_lname)


def _env_int(name: str, default: int, min_value: int = 0) -> int:
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < min_value:
        return min_value
    return value


def _select_text_entities_for_render(
    text_ents: List[Dict],
) -> Tuple[List[Dict], Dict[str, int]]:
    """
    Reduce label actor count for very large files while preserving important labels.

    Tuning:
      NAKSHA_SNT_MAX_TEXT_ACTORS      : max labels to render (0 => unlimited)
      NAKSHA_SNT_TEXT_CELL_MIN_SIZE   : minimum decimation grid size in XY units
    """
    total = len(text_ents)
    if total == 0:
        return text_ents, {"total": 0, "selected": 0, "limit": 0}

    max_labels = _env_int("NAKSHA_SNT_MAX_TEXT_ACTORS", 1500, min_value=0)
    if max_labels == 0 or total <= max_labels:
        return text_ents, {"total": total, "selected": total, "limit": max_labels}

    high: List[Dict] = []
    med: List[Dict] = []
    low: List[Dict] = []

    xmin = float("inf")
    ymin = float("inf")
    xmax = float("-inf")
    ymax = float("-inf")

    # FIX: Separate out BLOCKS-layer labels — always render them regardless of
    # decimation since they are the important block/file name labels.
    exempt: List[Dict] = []
    remaining: List[Dict] = []
    for e in text_ents:
        if e.get("layer", "") == "BLOCKS":
            exempt.append(e)
        else:
            remaining.append(e)

    # Re-run with only non-BLOCKS entities for decimation
    text_ents = remaining

    for e in text_ents:
        txt = str(e.get("text", "")).strip()
        score = _score_label_cached(txt)
        if score >= 100:
            high.append(e)
        elif score >= 50:
            med.append(e)
        else:
            low.append(e)

        pos = e.get("position", (0.0, 0.0, 0.0))
        try:
            x = float(pos[0])
            y = float(pos[1])
            if x < xmin:
                xmin = x
            if x > xmax:
                xmax = x
            if y < ymin:
                ymin = y
            if y > ymax:
                ymax = y
        except Exception:
            continue

    if xmin == float("inf") or ymin == float("inf"):
        selected = (high + med + low)[:max_labels]
        return selected, {"total": total, "selected": len(selected), "limit": max_labels}

    dx = max(xmax - xmin, 1.0)
    dy = max(ymax - ymin, 1.0)
    area = dx * dy
    cell_min_size = float(_env_int("NAKSHA_SNT_TEXT_CELL_MIN_SIZE", 1, min_value=1))
    cell_size = max(math.sqrt(area / max(1, max_labels)), cell_min_size)

    selected: List[Dict] = []
    selected_ids: Set[int] = set()
    seen_cells: Set[Tuple[int, int]] = set()

    for bucket in (high, med, low):
        for e in bucket:
            pos = e.get("position", (0.0, 0.0, 0.0))
            try:
                x = float(pos[0])
                y = float(pos[1])
            except Exception:
                continue
            cx = int((x - xmin) / cell_size)
            cy = int((y - ymin) / cell_size)
            key = (cx, cy)
            if key in seen_cells:
                continue
            seen_cells.add(key)
            selected.append(e)
            selected_ids.add(id(e))
            if len(selected) >= max_labels:
                return selected, {"total": total, "selected": len(selected), "limit": max_labels}

    if len(selected) < max_labels:
        for bucket in (high, med, low):
            for e in bucket:
                if id(e) in selected_ids:
                    continue
                selected.append(e)
                selected_ids.add(id(e))
                if len(selected) >= max_labels:
                    break
            if len(selected) >= max_labels:
                break

    # Merge exempt BLOCKS labels back — always shown, uncapped
    selected = exempt + selected
    return selected, {"total": total + len(exempt), "selected": len(selected), "limit": max_labels}


# ─────────────────────────────────────────────────────────────────────────────
# SNT BINARY FILE READER
# ─────────────────────────────────────────────────────────────────────────────

def _read_snt_file(filepath: str) -> Dict:
    result: Dict = {"version": (1, 0), "layers": [], "entities": []}

    try:
        from snt_core.snt_reader import SntReader
        from snt_core.snt_format import ColorMode, EntityType

        with SntReader(filepath) as reader:
            result["version"] = (reader.version_major, reader.version_minor)
            layers_out: List[Dict] = []
            layer_colors: List[Tuple[int, int, int]] = []
            for i, rec in enumerate(reader.layers):
                lname = reader.string(rec.name_idx)
                color = _aci_to_rgb(rec.color_aci, i)
                layers_out.append({"name": lname, "color": color})
                layer_colors.append(color)
            result["layers"] = layers_out

            layer_name_set: Set[str] = {layer["name"] for layer in layers_out}
            entities_out: List[Dict] = []
            native_batch_used = False

            if _is_native_batch_enabled():
                try:
                    with open(filepath, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm_obj:
                        for hdr in reader.iter_entities():
                            layer_name = reader.layer_name(hdr.layer_idx)
                            color = _resolve_entity_color(hdr, layer_colors, ColorMode)
                            etype = hdr.entity_type

                            ent = _decode_entity_from_mmap(
                                etype=etype,
                                mm_obj=mm_obj,
                                body_offset=hdr.body_offset,
                                body_size=hdr.body_size,
                                layer_name=layer_name,
                                color=color,
                                entity_type_cls=EntityType,
                                string_fn=reader.string,
                            )
                            if ent is None:
                                body = reader.read_body(hdr)
                                ent = _decode_entity_from_body(
                                    etype=etype,
                                    body=body,
                                    layer_name=layer_name,
                                    color=color,
                                    entity_type_cls=EntityType,
                                    string_fn=reader.string,
                                )
                            if ent is None:
                                continue

                            if etype in (EntityType.TEXT, EntityType.MTEXT,
                                         EntityType.DIMENSION, EntityType.LEADER):
                                _promote_featureattrib_text_layer(
                                    ent=ent,
                                    src_layer_name=layer_name,
                                    src_color=ent.get("color", _DEFAULT_COLOR),
                                    layers_out=layers_out,
                                    layer_name_set=layer_name_set,
                                )
                            entities_out.append(ent)

                    native_batch_used = True
                except Exception as batch_exc:
                    print(f"  [native-batch] fallback to classic decode ({type(batch_exc).__name__}: {batch_exc})")

            if not native_batch_used:
                for hdr in reader.iter_entities():
                    body = reader.read_body(hdr)
                    layer_name = reader.layer_name(hdr.layer_idx)
                    color = _resolve_entity_color(hdr, layer_colors, ColorMode)
                    etype = hdr.entity_type

                    ent = _decode_entity_from_body(
                        etype=etype,
                        body=body,
                        layer_name=layer_name,
                        color=color,
                        entity_type_cls=EntityType,
                        string_fn=reader.string,
                    )
                    if ent is None:
                        continue
                    if etype in (EntityType.TEXT, EntityType.MTEXT,
                                 EntityType.DIMENSION, EntityType.LEADER):
                        _promote_featureattrib_text_layer(
                            ent=ent,
                            src_layer_name=layer_name,
                            src_color=ent.get("color", _DEFAULT_COLOR),
                            layers_out=layers_out,
                            layer_name_set=layer_name_set,
                        )
                    entities_out.append(ent)

            result["entities"] = entities_out
            print(
                f"  SNT v{reader.version_major}.{reader.version_minor} | "
                f"layers={len(layers_out)} entities={len(entities_out)} "
                f"native_batch={'on' if native_batch_used else 'off'}"
            )
            return result

    except ImportError:
        print("  [warning] snt_core not on PYTHONPATH - using legacy inline reader")
    except Exception as exc:
        print(f"  [warning] SntReader failed ({type(exc).__name__}): {exc}")
        traceback.print_exc()
        return result
    # ── Legacy inline parser ────────────────────────────────
    try:
        with open(filepath, "rb") as f:
            raw: bytes = f.read()
    except OSError as exc:
        print(f"  [error] Cannot read SNT file: {exc}")
        return result

    if len(raw) < 24:
        print("  [error] File too small to be a valid SNT file")
        return result

    magic = raw[:4]
    if magic not in (_LEGACY_MAGIC_V0, _LEGACY_MAGIC_V1):
        print(f"  [error] Unrecognised magic {magic!r} - not an SNT file")
        return result

    pos = 4
    ver_major, ver_minor = struct.unpack_from("<HH", raw, pos); pos += 4
    result["version"] = (ver_major, ver_minor)
    pos += 4  # skip CRC
    num_strings, num_layers, num_entities = struct.unpack_from("<III", raw, pos)
    pos += 12
    print(f"  [legacy] SNT v{ver_major}.{ver_minor} | "
          f"strings={num_strings} layers={num_layers} entities={num_entities}")

    legacy_strings: List[str] = []
    for _ in range(num_strings):
        if pos + 4 > len(raw):
            break
        slen = struct.unpack_from("<I", raw, pos)[0]; pos += 4
        legacy_strings.append(raw[pos:pos + slen].decode("utf-8", errors="replace"))
        pos += slen

    legacy_layers: List[Dict] = []
    for i in range(num_layers):
        if pos + 7 > len(raw):
            break
        name_idx = struct.unpack_from("<I", raw, pos)[0]; pos += 4
        r, g, b  = struct.unpack_from("<BBB", raw, pos); pos += 3
        name  = (legacy_strings[name_idx] if name_idx < len(legacy_strings)
                 else f"Layer{i}")
        color = ((r, g, b) if (r + g + b) > 0
                 else _LAYER_COLOR_CYCLE[i % len(_LAYER_COLOR_CYCLE)])
        legacy_layers.append({"name": name, "color": color})
    result["layers"] = legacy_layers
    legacy_layer_name_set: Set[str] = {layer["name"] for layer in legacy_layers}

    legacy_entities: List[Dict] = []
    for _ in range(num_entities):
        if pos >= len(raw):
            break
        try:
            etype     = struct.unpack_from("<B", raw, pos)[0]; pos += 1
            layer_idx = struct.unpack_from("<I", raw, pos)[0]; pos += 4
            lname  = (legacy_layers[layer_idx]["name"]
                      if layer_idx < len(legacy_layers) else "0")
            lcolor = (legacy_layers[layer_idx]["color"]
                      if layer_idx < len(legacy_layers) else _DEFAULT_COLOR)

            if etype == _LEGACY_ETYPE_POLYLINE:
                nv = struct.unpack_from("<I", raw, pos)[0]; pos += 4
                verts = []
                for _ in range(nv):
                    x, y, z = struct.unpack_from("<ddd", raw, pos); pos += 24
                    verts.append((x, y, z))
                closed = (len(verts) >= 3 and
                          np.allclose(verts[0], verts[-1], atol=1e-6))
                legacy_entities.append({
                    "type": "POLYLINE", "layer": lname, "color": lcolor,
                    "vertices": verts, "closed": closed})
            elif etype == _LEGACY_ETYPE_TEXT:
                tlen = struct.unpack_from("<I", raw, pos)[0]; pos += 4
                text = raw[pos:pos + tlen].decode("utf-8", errors="replace")
                pos += tlen
                x, y, z = struct.unpack_from("<ddd", raw, pos); pos += 24
                height = (struct.unpack_from("<d", raw, pos)[0]
                          if pos + 8 <= len(raw) else 1.0)
                pos += 8
                
                ent = {
                    "type": "TEXT", "layer": lname, "color": lcolor,
                    "text": text, "position": (x, y, z), "height": height}
                
                # ✅ HIGH-PRIORITY SPLIT (Legacy)
                if lname == "FeatureAttribs":
                    is_yellow = (lcolor == (255, 255, 0))
                    is_cyan = (lcolor == (0, 255, 255))
                    if is_cyan or (not is_yellow and _score_label_cached(text) >= 100):
                        new_lname = "FileNames"
                        ent["layer"] = new_lname
                        if new_lname not in legacy_layer_name_set:
                            legacy_layers.append({"name": new_lname, "color": (0, 255, 255)})
                            legacy_layer_name_set.add(new_lname)
                
                legacy_entities.append(ent)
            elif etype == _LEGACY_ETYPE_3DFACE:
                verts = []
                for _ in range(4):
                    x, y, z = struct.unpack_from("<ddd", raw, pos); pos += 24
                    verts.append((x, y, z))
                legacy_entities.append({
                    "type": "3DFACE", "layer": lname, "color": lcolor,
                    "vertices": verts})
            else:
                break
        except struct.error:
            break

    result["entities"] = legacy_entities
    return result


# ─────────────────────────────────────────────────────────────────────────────
# BACKGROUND LOAD WORKER
# ─────────────────────────────────────────────────────────────────────────────

class SNTLoadWorker(QThread):
    progress    = Signal(int, str, bool)
    file_loaded = Signal(object, object)
    finished    = Signal()
    error       = Signal(str)

    def __init__(self, file_paths: List[str]) -> None:
        super().__init__()
        self.file_paths: List[str] = file_paths
        self._cancelled: bool      = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            total = len(self.file_paths)
            indeterminate = (total == 1)
            for idx, fp in enumerate(self.file_paths):
                if self._cancelled:
                    return
                
                QCoreApplication.processEvents()
                if self._cancelled:
                    return
                    
                snt_path = Path(fp)
                self.progress.emit(
                    0 if indeterminate else idx,
                    f"Reading {snt_path.name}...",
                    indeterminate,
                )
                item_data = {"snt_path": snt_path}
                try:
                    parsed = _read_snt_file(str(snt_path))
                    if self._cancelled:
                        return
                    item_data["parsed"] = parsed
                    item_data["entity_count"] = len(parsed["entities"])
                    item_data["layer_count"] = len(parsed["layers"])
                except Exception as exc:
                    if not self._cancelled:
                        item_data["error"] = str(exc)
                
                if not self._cancelled:
                    self.file_loaded.emit(item_data, None)
            
            if not self._cancelled:
                self.finished.emit()
        except Exception as exc:
            if not self._cancelled:
                self.error.emit(f"SNT loading failed: {exc}\n{traceback.format_exc()}")


# ─────────────────────────────────────────────────────────────────────────────
# DISPLAY OPTIONS DIALOG
# ─────────────────────────────────────────────────────────────────────────────

class SNTDisplayOptionsDialog(QDialog):
    _COLOR_PRESETS: List[Tuple[str, QColor]] = [
        ("Green",   QColor(  0, 255,  80)),
        ("Cyan",    QColor(  0, 255, 255)),
        ("Yellow",  QColor(255, 255,   0)),
        ("Red",     QColor(255,   0,   0)),
        ("White",   QColor(255, 255, 255)),
        ("Magenta", QColor(255,   0, 255)),
        ("Blue",    QColor(  0,   0, 255)),
    ]

    def __init__(self, parent=None, mode="overlay", override_enabled=False,
                 override_color=(0, 255, 80)):
        super().__init__(parent)
        self.setWindowTitle("SNT Display Options")
        self.setModal(True)
        self.resize(300, 180)
        self.setStyleSheet(get_dialog_stylesheet())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Display Mode:"))
        self.overlay_radio  = QRadioButton("Overlay (on top)")
        self.underlay_radio = QRadioButton("Underlay (below)")
        (self.underlay_radio if mode == "underlay" else self.overlay_radio).setChecked(True)
        mode_row.addWidget(self.overlay_radio)
        mode_row.addWidget(self.underlay_radio)
        layout.addLayout(mode_row)

        color_row = QHBoxLayout()
        self.color_check = QCheckBox("Override colour:")
        self.color_combo  = QComboBox()
        for name, qc in self._COLOR_PRESETS:
            self.color_combo.addItem(name, qc)
        self.color_check.setChecked(override_enabled)
        self.color_combo.setEnabled(override_enabled)
        self.color_check.toggled.connect(self.color_combo.setEnabled)
        for i in range(self.color_combo.count()):
            q = self.color_combo.itemData(i)
            if (q.red(), q.green(), q.blue()) == override_color:
                self.color_combo.setCurrentIndex(i)
                break
        color_row.addWidget(self.color_check)
        color_row.addWidget(self.color_combo)
        layout.addLayout(color_row)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn     = QPushButton("OK")
        cancel_btn = QPushButton("Cancel")
        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def get_values(self):
        mode    = "underlay" if self.underlay_radio.isChecked() else "overlay"
        enabled = self.color_check.isChecked()
        qc      = self.color_combo.currentData()
        return mode, enabled, (qc.red(), qc.green(), qc.blue())


# ─────────────────────────────────────────────────────────────────────────────
# LAYER SELECTION DIALOG
# ─────────────────────────────────────────────────────────────────────────────

class SNTLayerSelectionDialog(QDialog):
    def __init__(self, snt_path: Path, parent_item=None):
        super().__init__(parent_item)
        self.snt_path    = snt_path
        self.parent_item = parent_item
        self.setWindowTitle(f"Layer Display - {snt_path.name}")
        self.setModal(False)
        self.setWindowModality(Qt.NonModal)
        self.resize(400, 550)
        self._init_ui()
        self._load_layers()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.setStyleSheet(get_dialog_stylesheet())

        title = QLabel("Select Layers to Display")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)

        self.layer_list = QListWidget()
        self.layer_list.setAlternatingRowColors(True)
        layout.addWidget(self.layer_list)

        action_row = QHBoxLayout()
        for label, slot in [("All On", self._select_all),
                             ("All Off", self._deselect_all),
                             ("Invert", self._invert)]:
            btn = QPushButton(label)
            btn.setAutoDefault(False)
            btn.setDefault(False)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.clicked.connect(slot)
            action_row.addWidget(btn)
        layout.addLayout(action_row)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton("OK")
        ok_btn.setObjectName("primaryBtn")
        ok_btn.setAutoDefault(False)
        ok_btn.setDefault(False)
        ok_btn.setFocusPolicy(Qt.NoFocus)
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setAutoDefault(False)
        cancel_btn.setDefault(False)
        cancel_btn.setFocusPolicy(Qt.NoFocus)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    def _load_layers(self):
        cached_layers_info = None
        if self.parent_item is not None:
            cached_layers_info = getattr(self.parent_item, "layer_stats_cache", None)

        if cached_layers_info:
            layers_info = list(cached_layers_info)
        else:
            layers_info: List[Tuple[str, int, Tuple[int, int, int]]] = []
            parsed: Optional[Dict] = None
            if (self.parent_item is not None
                    and hasattr(self.parent_item, "cached_parsed")
                    and self.parent_item.cached_parsed):
                parsed = self.parent_item.cached_parsed
            else:
                try:
                    parsed = _read_snt_file(str(self.snt_path))
                    if self.parent_item is not None:
                        self.parent_item.cached_parsed = parsed
                except Exception:
                    pass

            if parsed:
                stats: Dict = {
                    layer["name"]: {"count": 0, "color": layer["color"]}
                    for layer in parsed.get("layers", [])
                }
                for ent in parsed.get("entities", []):
                    name = ent.get("layer", "0")
                    if name not in stats:
                        stats[name] = {"count": 0, "color": _DEFAULT_COLOR}
                    stats[name]["count"] += 1
                layers_info = [(name, s["count"], s["color"])
                               for name, s in stats.items()]

            if self.parent_item is not None:
                self.parent_item.layer_stats_cache = list(layers_info)

        current_sel = getattr(self.parent_item, "selected_layers", None)
        for name, count, color in sorted(layers_info, key=lambda x: x[0]):
            item = QListWidgetItem()
            item.setText(f"{name}  ({count} entities)")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            checked = (current_sel is None) or (name in current_sel)
            item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
            item.setData(Qt.UserRole, name)
            item.setForeground(QColor(*color))
            self.layer_list.addItem(item)

    def _select_all(self):
        for i in range(self.layer_list.count()):
            self.layer_list.item(i).setCheckState(Qt.Checked)

    def _deselect_all(self):
        for i in range(self.layer_list.count()):
            self.layer_list.item(i).setCheckState(Qt.Unchecked)

    def _invert(self):
        for i in range(self.layer_list.count()):
            it = self.layer_list.item(i)
            it.setCheckState(
                Qt.Unchecked if it.checkState() == Qt.Checked else Qt.Checked)

    def get_selected_layers(self) -> Set[str]:
        return {
            self.layer_list.item(i).data(Qt.UserRole)
            for i in range(self.layer_list.count())
            if self.layer_list.item(i).checkState() == Qt.Checked
        }


# ─────────────────────────────────────────────────────────────────────────────
# FILE ITEM WIDGET
# ─────────────────────────────────────────────────────────────────────────────

class SNTFileItem(QWidget):
    remove_requested = Signal(object)

    def __init__(self, snt_path: Path, parent=None):
        super().__init__(parent)
        self.snt_path:         Path               = Path(snt_path)
        self.cached_parsed:    Optional[Dict]     = None
        self.actor_cache:      Dict[str, List]    = {}  # Local cache, will be linked to app state
        self.entity_layers:    Dict[int, str]     = {}
        self.entity_count:     int                = 0
        self.display_mode:     str                = "overlay"
        self.override_enabled: bool               = False
        self.override_color:   Tuple[int,int,int] = (0, 255, 80)
        self.selected_layers:  Optional[Set[str]] = None
        self.layer_stats_cache: Optional[List[Tuple[str, int, Tuple[int, int, int]]]] = None
        self._layer_selection_dlg = None
        
        # ⚡ PERSISTENCE FIX: Link to existing attachment data in app if available
        self._link_to_app_state()
        
        self._init_ui()

    def _link_to_app_state(self):
        """Link this UI item to the persistent actor data in NakshaApp."""
        try:
            parent_dlg = self._find_parent_dialog()
            if not parent_dlg or not hasattr(parent_dlg.app, 'snt_attachments'):
                return
            
            fname = self.snt_path.name
            for attachment in parent_dlg.app.snt_attachments:
                if attachment.get("filename") == fname:
                    # Sync state from persistent storage
                    self.actor_cache = attachment.setdefault("actor_cache_map", {})
                    self.selected_layers = attachment.get("selected_layers")
                    self.layer_stats_cache = attachment.get("layer_stats")
                    self.display_mode = attachment.get("mode", "overlay")
                    self.override_enabled = attachment.get("override_enabled", False)
                    self.override_color = attachment.get("override_color", (0, 255, 80))
                    self.cached_parsed = attachment.get("parsed")
                    break
        except Exception:
            pass

    def _init_ui(self):
        self.setObjectName("sntFileItemRow")
        self.setFixedHeight(34)
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignVCenter)

        # 1. Checkbox
        self.checkbox = QCheckBox(f"{self.snt_path.name}")
        self.checkbox.setChecked(True)
        self.checkbox.setObjectName("sntItemCheckbox")
        self.checkbox.setFixedHeight(26)
        self.checkbox.stateChanged.connect(self._on_checkbox_changed)
        layout.addWidget(self.checkbox, 1)

        # 2. SNT Badge
        self.snt_badge = QLabel("SNT")
        self.snt_badge.setObjectName("sntBadge")
        self.snt_badge.setFixedSize(35, 20)
        self.snt_badge.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.snt_badge)

        # 3. Entity Count
        self.count_label = QLabel("...")
        self.count_label.setObjectName("sntCountBadge")
        self.count_label.setFixedSize(85, 20)
        self.count_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.count_label)

        # 4. Layers Button
        self.layers_btn = QPushButton("Layers")
        self.layers_btn.setObjectName("secondaryBtn")
        self.layers_btn.setFixedSize(75, 24)
        self.layers_btn.clicked.connect(self._open_layer_selection)
        layout.addWidget(self.layers_btn)

        # 5. Settings Button
        self.settings_btn = QPushButton("Settings")
        self.settings_btn.setObjectName("secondaryBtn")
        self.settings_btn.setFixedSize(85, 24)
        self.settings_btn.clicked.connect(self._open_display_options)
        layout.addWidget(self.settings_btn)

        # 6. Remove Button
        self.rm_btn = QPushButton("X")
        self.rm_btn.setObjectName("dangerBtn")
        self.rm_btn.setFixedSize(24, 24)
        self.rm_btn.clicked.connect(lambda: self.remove_requested.emit(self))
        layout.addWidget(self.rm_btn)

        self.refresh_theme()

    def refresh_theme(self):
        """Re-apply styles based on current theme."""
        c = ThemeColors
        accent = c.get('accent')
        
        self.setStyleSheet(f"""
            QWidget#sntFileItemRow {{
                background: {c.get('bg_secondary')};
                border: 1px solid {c.get('border_light')};
                border-radius: 8px;
            }}
            QWidget#sntFileItemRow:hover {{
                border-color: {accent};
            }}
            QCheckBox#sntItemCheckbox {{
                color: {accent};
                font-weight: bold;
                font-size: 10px;
                background: {c.get('bg_input')};
                border: 1px solid {c.get('border_light')};
                border-radius: 13px;
                padding: 0px 10px;
            }}
            QLabel#sntBadge {{
                background: {c.get('success')};
                color: white;
                border-radius: 4px;
                font-size: 9px;
                font-weight: bold;
            }}
            QLabel#sntCountBadge {{
                background: {c.get('bg_input')};
                color: {accent};
                border: 1px solid {c.get('border_light')};
                border-radius: 10px;
                font-size: 9px;
                font-weight: bold;
            }}
            QPushButton#secondaryBtn, QPushButton#dangerBtn {{
                font-size: 9px;
                font-weight: bold;
                padding: 0px;
            }}
        """)

    def update_entity_count(self, count: int):
        self.entity_count = count
        self.count_label.setText(f"{count:,} entities")

    def is_checked(self) -> bool:
        return self.checkbox.isChecked()

    def _on_checkbox_changed(self, state: int):
        try:
            is_visible = (state == Qt.Checked or state == Qt.CheckState.Checked or state == 2)
            parent_dlg = self._find_parent_dialog()
            if parent_dlg is None:
                return

            renderer = None
            try:
                renderer = parent_dlg.app.vtk_widget.renderer
            except Exception:
                pass

            if self.actor_cache:
                for layer_name, actors in self.actor_cache.items():
                    layer_ok = (self.selected_layers is None
                                or layer_name in self.selected_layers)
                    vis = is_visible and layer_ok
                    for actor in actors:
                        if is_visible and renderer is not None:
                            try:
                                renderer.AddActor(actor)
                            except Exception:
                                pass
                        actor.SetVisibility(1 if vis else 0)
            else:
                target = self.snt_path.name
                for store in ['snt_actors', 'dxf_actors']:
                    for snt_data in getattr(parent_dlg.app, store, []):
                        if os.path.basename(snt_data.get("filename", "")) == target:
                            for actor in snt_data.get("actors", []):
                                if is_visible and renderer is not None:
                                    try:
                                        renderer.AddActor(actor)
                                    except Exception:
                                        pass
                                actor.SetVisibility(1 if is_visible else 0)

            self._force_render(parent_dlg)
        except Exception as exc:
            print(f"  [warn] SNT checkbox toggle failed: {exc}")

    def _open_display_options(self):
        dlg = SNTDisplayOptionsDialog(
            self, mode=self.display_mode,
            override_enabled=self.override_enabled,
            override_color=self.override_color)
        if dlg.exec() != QDialog.Accepted:
            return
        old_mode = self.display_mode
        self.display_mode, self.override_enabled, self.override_color = dlg.get_values()
        for _layer, actors in self.actor_cache.items():
            for actor in actors:
                if self.override_enabled:
                    if hasattr(actor, "GetProperty"):
                        actor.GetProperty().SetColor(_normalise_vtk_color(self.override_color))
                    elif hasattr(actor, "GetTextProperty"):
                        actor.GetTextProperty().SetColor(_normalise_vtk_color(self.override_color))
                elif hasattr(actor, "_original_color"):
                    if hasattr(actor, "GetProperty"):
                        actor.GetProperty().SetColor(_normalise_vtk_color(actor._original_color))
                    elif hasattr(actor, "GetTextProperty"):
                        actor.GetTextProperty().SetColor(_normalise_vtk_color(actor._original_color))
                if old_mode != self.display_mode:
                    if hasattr(actor, "GetProperty"):
                        actor.GetProperty().SetOpacity(0.5 if self.display_mode == "underlay" else 1.0)
        parent_dlg = self._find_parent_dialog()
        if parent_dlg:
            self._force_render(parent_dlg)

    def _apply_layer_selection(self, selected: Set[str], total_layers: int):
        if len(selected) == 0:
            self.selected_layers = set()
            self.count_label.setText(f"{self.entity_count} entities (0 layers)")
            self.count_label.setStyleSheet(f"color:{ThemeColors.get('danger')}; font-size:7px; font-weight:bold;")
        elif len(selected) == total_layers:
            self.selected_layers = None
            self.count_label.setText(f"{self.entity_count} entities")
            self.count_label.setStyleSheet(f"color:{ThemeColors.get('accent')}; font-size:7px; font-weight:bold;")
        else:
            self.selected_layers = selected
            self.count_label.setText(
                f"{self.entity_count} entities ({len(selected)} layers)")
            self.count_label.setStyleSheet(f"color:{ThemeColors.get('text_secondary')}; font-size:7px; font-weight:bold;")

        # ⚡ PERSISTENCE: Save selection back to app state
        parent_dlg = self._find_parent_dialog()
        if parent_dlg:
            fname = self.snt_path.name
            for attachment in parent_dlg.app.snt_attachments:
                if attachment.get("filename") == fname:
                    attachment["selected_layers"] = self.selected_layers
                    break

        if self.actor_cache and self.checkbox.isChecked():
            for layer_name, actors in self.actor_cache.items():
                vis = (self.selected_layers is None
                       or layer_name in self.selected_layers)
                for actor in actors:
                    actor.SetVisibility(vis)

        if parent_dlg:
            self._force_render(parent_dlg)

    def _open_layer_selection(self):
        try:
            if self._layer_selection_dlg is not None and self._layer_selection_dlg.isVisible():
                self._layer_selection_dlg.raise_()
                self._layer_selection_dlg.activateWindow()
                return

            dlg = SNTLayerSelectionDialog(self.snt_path, self)
            dlg.setModal(False)
            dlg.setWindowModality(Qt.NonModal)
            self._layer_selection_dlg = dlg

            def _cleanup_dialog(*_args):
                if self._layer_selection_dlg is dlg:
                    self._layer_selection_dlg = None

            def _on_accept():
                selected = dlg.get_selected_layers()
                total_layers = dlg.layer_list.count()
                self._apply_layer_selection(selected, total_layers)

            dlg.accepted.connect(_on_accept)
            dlg.finished.connect(_cleanup_dialog)
            dlg.show()
        except Exception as exc:
            print(f"[error] Layer selection failed: {exc}")
            traceback.print_exc()

    def _find_parent_dialog(self):
        p = self.parent()
        while p is not None and not isinstance(p, MultiSNTAttachmentDialog):
            p = p.parent()
        return p

    @staticmethod
    def _force_render(parent_dlg):
        try:
            rw = parent_dlg.app.vtk_widget.GetRenderWindow()
            if rw:
                rw.Render()
                return
        except Exception:
            pass
        try:
            parent_dlg.app.vtk_widget.render()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ATTACHMENT DIALOG
# ─────────────────────────────────────────────────────────────────────────────

class MultiSNTAttachmentDialog(QDialog):
    """
    Dialog for attaching multiple SNT overlay files.
    """

    snt_attached = Signal(list)

    def __init__(self, app, parent=None):
        target_parent = None
        if isinstance(parent, QWidget):
            target_parent = parent
        elif isinstance(app, QWidget):
            target_parent = app
        elif hasattr(app, "window") and isinstance(app.window, QWidget):
            target_parent = app.window

        super().__init__(target_parent, Qt.Window)
        self.setWindowModality(Qt.NonModal)
        self.app             = app
        self.snt_items:      List[SNTFileItem]       = []
        self._load_worker:   Optional[SNTLoadWorker] = None
        self.setProperty("themeStyledDialog", True)

        self.setWindowTitle("Attach SNT Files - NakshaApp Native Format")
        self.setStyleSheet(get_dialog_stylesheet())
        self.setGeometry(150, 150, 720, 800)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(12)

        # 1. Main Title
        self.title_label = QLabel("Attach SNT Files (NakshaApp Native Format)")
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.setStyleSheet(get_title_banner_style())
        layout.addWidget(self.title_label)

        # 2. Info Banner
        self.info_label = QLabel(
            "SNT loads 10-50x faster than DXF  |  "
            "Colours & layers preserved  |  Grid pipeline fully compatible")
        self.info_label.setAlignment(Qt.AlignCenter)
        self.info_label.setStyleSheet(get_notice_banner_style("info"))
        layout.addWidget(self.info_label)

        # 3. File Selection Group
        file_group = QGroupBox("Select SNT Files")
        file_layout = QVBoxLayout()
        self.browse_btn  = QPushButton("Browse and Add SNT Files...")
        self.browse_btn.setObjectName("secondaryBtn")
        self.browse_btn.setMinimumHeight(45)
        self.browse_btn.setAutoDefault(False)
        self.browse_btn.setDefault(False)
        self.browse_btn.setFocusPolicy(Qt.NoFocus)
        self.browse_btn.clicked.connect(self._select_snt_files)
        file_layout.addWidget(self.browse_btn)
        file_group.setLayout(file_layout)
        layout.addWidget(file_group)

        # 4. List Group
        list_group = QGroupBox("Selected SNT Files (click X to remove)")
        list_layout = QVBoxLayout()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(200)
        scroll.setMaximumHeight(350)
        
        self.file_list_widget = QWidget()
        self.file_list_layout = QVBoxLayout(self.file_list_widget)
        self.file_list_layout.setSpacing(5)
        self.file_list_layout.setContentsMargins(5, 5, 5, 5)
        self.file_list_layout.addStretch()
        scroll.setWidget(self.file_list_widget)
        list_layout.addWidget(scroll)

        self.file_count_label = QLabel("No files selected")
        self.file_count_label.setStyleSheet(f"color: {ThemeColors.get('text_muted')}; font-size: 10px; padding: 5px;")
        list_layout.addWidget(self.file_count_label)
        list_group.setLayout(list_layout)
        layout.addWidget(list_group)

        btn_row = QHBoxLayout()
        clear_btn = QPushButton("Clear All")
        clear_btn.setObjectName("dangerBtn")
        clear_btn.setMinimumHeight(40)
        clear_btn.setAutoDefault(False)
        clear_btn.setDefault(False)
        clear_btn.setFocusPolicy(Qt.NoFocus)
        clear_btn.clicked.connect(self._clear_all)
        btn_row.addWidget(clear_btn)

        btn_row.addStretch()

        self.attach_btn = QPushButton("Attach All SNT Files")
        self.attach_btn.setObjectName("primaryBtn")
        self.attach_btn.setMinimumHeight(40)
        self.attach_btn.setAutoDefault(False)
        self.attach_btn.setDefault(False)
        self.attach_btn.setFocusPolicy(Qt.NoFocus)
        self.attach_btn.clicked.connect(self._attach_all)
        btn_row.addWidget(self.attach_btn)
        layout.addLayout(btn_row)

    def refresh_theme(self):
        """Re-apply styles to the entire dialog and all items."""
        self.setStyleSheet(get_dialog_stylesheet())
        self.title_label.setStyleSheet(get_title_banner_style())
        self.info_label.setStyleSheet(get_notice_banner_style("info"))
        self.browse_btn.setStyleSheet(f"""
            QPushButton {{
                background: {ThemeColors.get('bg_secondary')};
                color: {ThemeColors.get('accent')};
                border: 1px solid {ThemeColors.get('border_light')};
                border-radius: 8px;
                font-size: 13px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background: {ThemeColors.get('bg_input')};
                border: 1px solid {ThemeColors.get('accent')};
            }}
        """)
        for item in self.snt_items:
            item.refresh_theme()

    def changeEvent(self, event):
        """Detect theme property changes and refresh."""
        if event.type() == QEvent.DynamicPropertyChange:
            if event.propertyName() == "themeStyledDialog":
                self.refresh_theme()
        super().changeEvent(event)

    def _select_snt_files(self):
        file_paths, _ = QFileDialog.getOpenFileNames(
            self, "Select SNT Files", "", "SNT Files (*.snt);;All Files (*)")
        if not file_paths:
            return

        if self._load_worker is not None:
            if self._load_worker.isRunning():
                self._load_worker.cancel()
                self._load_worker.wait(5000)
                if self._load_worker.isRunning():
                    self._load_worker.terminate()
            self._load_worker = None
        
        QCoreApplication.processEvents()
        
        total = len(file_paths)
        indeterminate = (total == 1)

        progress = QProgressDialog(
            "Loading SNT file..." if indeterminate else "Loading SNT files...",
            "Cancel", 0, 0 if indeterminate else total, self)
        progress.setWindowTitle("Loading SNT Files")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.setStyleSheet(get_progress_dialog_stylesheet())
        progress.show()
        QCoreApplication.processEvents()

        self._load_worker = SNTLoadWorker(file_paths)

        def on_progress(value, message, is_indet):
            try:
                if progress is not None and not progress.wasCanceled():
                    progress.setLabelText(message)
                    if not is_indet:
                        progress.setValue(value)
            except RuntimeError:
                pass

        def on_file_loaded(item_data, _):
            try:
                snt_path = item_data["snt_path"]
                if any(it.snt_path == snt_path for it in self.snt_items):
                    return
                item = SNTFileItem(snt_path)
                item.remove_requested.connect(self._remove_item)
                self.file_list_layout.insertWidget(len(self.snt_items), item)
                self.snt_items.append(item)
                if "parsed" in item_data:
                    item.cached_parsed = item_data["parsed"]
                    item.update_entity_count(item_data["entity_count"])
                else:
                    item.count_label.setText("Error")
                    item.count_label.setStyleSheet(
                        f"color:{ThemeColors.get('danger')}; font-size:9px;")
            except RuntimeError:
                pass

        def on_finished():
            try:
                if progress is not None and not progress.wasCanceled():
                    if not indeterminate:
                        progress.setValue(total)
                    progress.close()
                self._update_file_count()
            except RuntimeError:
                pass

        def on_error(msg):
            try:
                if progress is not None:
                    progress.close()
                QMessageBox.critical(self, "Load Failed", msg)
            except RuntimeError:
                pass

        def on_canceled():
            if self._load_worker and self._load_worker.isRunning():
                self._load_worker.cancel()
                self._load_worker.wait(3000)
                if self._load_worker.isRunning():
                    self._load_worker.terminate()

        self._load_worker.progress.connect(on_progress)
        self._load_worker.file_loaded.connect(on_file_loaded)
        self._load_worker.finished.connect(on_finished)
        self._load_worker.error.connect(on_error)
        progress.canceled.connect(on_canceled)
        self._load_worker.start()

    def closeEvent(self, event):
        if self._load_worker is not None and self._load_worker.isRunning():
            self._load_worker.cancel()
            self._load_worker.wait(3000)
            if self._load_worker.isRunning():
                self._load_worker.terminate()
        event.accept()

    def _remove_item(self, item: SNTFileItem):
        if item not in self.snt_items:
            return
        try:
            filename = str(item.snt_path.name)
            self.snt_items.remove(item)
            self.file_list_layout.removeWidget(item)
            if hasattr(item, 'actor_cache'):
                item.actor_cache.clear()
            self._remove_from_vtk(filename)
            try:
                item.remove_requested.disconnect()
            except Exception:
                pass
            item.setParent(None)
            item.deleteLater()
            self._update_file_count()
        except Exception as exc:
            print(f"[warn] Error in _remove_item: {exc}")

    def _remove_from_vtk(self, filename: str):
        try:
            target = os.path.basename(filename)
            renderer = None
            try:
                if (hasattr(self.app, 'vtk_widget') and 
                    self.app.vtk_widget is not None and
                    hasattr(self.app.vtk_widget, 'renderer')):
                    renderer = self.app.vtk_widget.renderer
            except Exception:
                renderer = None
            
            for store_name in ['snt_actors', 'dxf_actors']:
                if not hasattr(self.app, store_name):
                    continue
                store = getattr(self.app, store_name, [])
                indices_to_remove = []
                for i, snt_data in enumerate(store):
                    if os.path.basename(snt_data.get("filename", "")) == target:
                        indices_to_remove.append(i)
                        if renderer is not None:
                            for actor in snt_data.get("actors", []):
                                try:
                                    if actor is not None:
                                        renderer.RemoveActor(actor)
                                except Exception:
                                    pass
                for i in reversed(indices_to_remove):
                    try:
                        store.pop(i)
                    except IndexError:
                        pass
            
            for list_name in ['snt_attachments', 'dxf_attachments']:
                if hasattr(self.app, list_name):
                    try:
                        current_list = getattr(self.app, list_name)
                        setattr(self.app, list_name, [
                            a for a in current_list
                            if os.path.basename(a.get("filename", "")) != target
                        ])
                    except Exception:
                        pass

            if hasattr(self.app, "update_total_points_label"):
                self.app.update_total_points_label()
            
            if renderer is not None:
                try:
                    rw = self.app.vtk_widget.GetRenderWindow()
                    if rw is not None:
                        rw.Render()
                except Exception:
                    pass
        except Exception as exc:
            print(f"  [warn] _remove_from_vtk failed: {exc}")

    def _clear_all(self):
        if self._load_worker is not None and self._load_worker.isRunning():
            self._load_worker.cancel()
            self._load_worker.wait(5000)
            if self._load_worker.isRunning():
                self._load_worker.terminate()
            self._load_worker = None
        
        QCoreApplication.processEvents()
        items_to_remove = list(self.snt_items)
        for item in items_to_remove:
            try:
                self._remove_item(item)
            except Exception:
                pass
        self._update_file_count()
        import gc
        gc.collect()

    def restore_snt_actors(self) -> None:
        try:
            renderer = self.app.vtk_widget.renderer
        except Exception:
            return

        z_offset = _get_snt_z_offset(self.app)

        restored = 0
        for item in self.snt_items:
            if not item.is_checked():
                continue
            if item.actor_cache:
                for layer_name, actors in item.actor_cache.items():
                    layer_ok = (item.selected_layers is None
                                or layer_name in item.selected_layers)
                    for actor in actors:
                        try:
                            _apply_z_offset_to_actor(actor, z_offset)
                            renderer.AddActor(actor)
                            actor.SetVisibility(1 if layer_ok else 0)
                            restored += 1
                        except Exception:
                            pass
            else:
                target = item.snt_path.name
                for store in ['snt_actors', 'dxf_actors']:
                    att_name = store.replace("_actors", "_attachments")
                    attachments = getattr(self.app, att_name, [])
                    for snt_data in getattr(self.app, store, []):
                        if os.path.basename(snt_data.get("filename", "")) == target:
                            att = next((a for a in attachments if os.path.basename(a.get("filename", "")) == target), None)
                            actor_layer_map = {}
                            selected_layers = None
                            if att:
                                selected_layers = att.get("selected_layers")
                                cache_map = att.get("actor_cache_map", {})
                                for layer_name, actors in cache_map.items():
                                    for a in actors:
                                        actor_layer_map[id(a)] = layer_name

                            for actor in snt_data.get("actors", []):
                                try:
                                    _apply_z_offset_to_actor(actor, z_offset)
                                    renderer.AddActor(actor)
                                    if selected_layers is not None and id(actor) in actor_layer_map:
                                        layer_name = actor_layer_map[id(actor)]
                                        actor.SetVisibility(1 if layer_name in selected_layers else 0)
                                    else:
                                        actor.SetVisibility(1)
                                    restored += 1
                                except Exception:
                                    pass

        if restored:
            renderer.ResetCameraClippingRange()
            _snt_enable_gl_point_size(self.app)
            _snt_push_border_uniforms(self.app)
            try:
                rw = self.app.vtk_widget.GetRenderWindow()
                if rw:
                    rw.Render()
            except Exception:
                pass
    def _update_file_count(self):
        count = len(self.snt_items)
        if count == 0:
            self.file_count_label.setText("No files selected")
            self.file_count_label.setStyleSheet(f"color:{ThemeColors.get('text_muted')}; font-size:10px; padding:5px;")
        else:
            total_ent = sum(it.entity_count for it in self.snt_items)
            self.file_count_label.setText(
                f"{count} file(s)  |  {total_ent:,} total entities")
            self.file_count_label.setStyleSheet(
                f"color:{ThemeColors.get('accent')}; font-size:10px; font-weight:bold; padding:5px;")

    def _attach_all(self):
        selected_items = [it for it in self.snt_items if it.is_checked()]
        if not selected_items:
            QMessageBox.warning(self, "No Files", "Please select SNT files first.")
            return

        if hasattr(self.app, 'data') and self.app.data is not None:
            save_path = (getattr(self.app, 'last_save_path', None)
                         or getattr(self.app, 'loaded_file', None))
            if save_path:
                try:
                    from gui.save_pointcloud import save_pointcloud_quick
                    save_pointcloud_quick(self.app, save_path)
                except Exception as e:
                    reply = QMessageBox.warning(
                        self, "Save Failed",
                        f"Failed to auto-save current file:\n\n{e}\n\n"
                        "Continue with SNT attachment anyway?",
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                    if reply == QMessageBox.No:
                        return

        msg = f"Attach {len(selected_items)} SNT file(s)?\n\n"
        if hasattr(self.app, "data") and self.app.data is not None:
            msg += "WARNING: Current point cloud will be CLEARED\n"
            msg += "You can reload LAZ files after SNT attachment\n"

        if QMessageBox.question(
                        self, "Confirm SNT Attachment", msg,
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.Yes,
                    ) != QMessageBox.Yes:
            return

        if hasattr(self.app, 'data') and self.app.data is not None:
            if hasattr(self.app, "vtk_widget") and self.app.vtk_widget:
                renderer = self.app.vtk_widget.renderer
                renderer.RemoveAllViewProps()
                for attr in ['actors', '_actors']:
                    if hasattr(self.app.vtk_widget, attr):
                        getattr(self.app.vtk_widget, attr).clear()
                self.app.vtk_widget.render()

            if hasattr(self.app, 'section_vtks'):
                for vtk_widget in self.app.section_vtks.values():
                    try:
                        vtk_widget.renderer.RemoveAllViewProps()
                        if hasattr(vtk_widget, 'actors'):
                            vtk_widget.actors.clear()
                        vtk_widget.render()
                    except Exception:
                        pass

            self.app.data           = None
            self.app.loaded_file    = None
            self.app.last_save_path = None
            
            # ✅ BUG-FIX: Preserve user settings across SNT attachments if they have been manually set.
            if not getattr(self.app, '_display_mode_locked', False):
                self.app.class_palette = {}
                if hasattr(self.app, "view_palettes"):
                    self.app.view_palettes.clear()
            else:
                print("Preserving existing Display Mode settings for SNT attachment")

            for attr in ['snt_actors', 'snt_attachments',
                         'dxf_actors', 'dxf_attachments', 'snt_block_polygons']:
                if hasattr(self.app, attr):
                    getattr(self.app, attr).clear()

            QCoreApplication.processEvents()

        progress = QProgressDialog(
            "Processing SNT files...", "Cancel",
            0, len(selected_items) * 2, self)
        progress.setWindowTitle("Attaching SNT Files")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.setStyleSheet(get_progress_dialog_stylesheet())
        progress.show()
        QCoreApplication.processEvents()

        for attr in ['snt_attachments', 'snt_actors',
                     'dxf_attachments', 'dxf_actors']:
            if not hasattr(self.app, attr):
                setattr(self.app, attr, [])

        all_attachments: List[Dict] = []
        try:
            for idx, item in enumerate(selected_items):
                if progress.wasCanceled():
                    return
                progress.setLabelText(f"Processing {item.snt_path.name}...")
                progress.setValue(idx)
                QCoreApplication.processEvents()

                attachment = self._process_snt_file(item)
                if attachment:
                    attachment["_snt_item"] = item
                    all_attachments.append(attachment)

            if not all_attachments:
                progress.close()
                QMessageBox.information(self, "No Data", "No entities found.")
                return

            self.app.snt_attachments.extend(all_attachments)
            self.app.dxf_attachments.extend(all_attachments)
            if hasattr(self.app, "update_total_points_label"):
                self.app.update_total_points_label()
            self.snt_attached.emit(all_attachments)

            for idx, attachment in enumerate(all_attachments):
                if progress.wasCanceled():
                    return
                progress.setLabelText(f"Rendering {attachment['filename']}...")
                progress.setValue(len(selected_items) + idx)
                QCoreApplication.processEvents()
                self._render_snt_in_vtk(attachment)

            progress.setValue(len(selected_items) * 2)
            progress.close()

            total_ent = sum(int(a.get("entity_count", len(a.get("entities", [])))) for a in all_attachments)
            QMessageBox.information(
                self, "SNT Attached",
                f"Attached {len(all_attachments)} SNT file(s)\n"
                f"Total entities: {total_ent:,}")
        except Exception as exc:
            progress.close()
            QMessageBox.critical(self, "Attachment Failed", str(exc))

    def _compute_2d_bounds(self, entities: List[Dict]) -> Optional[Tuple[float, float, float, float]]:
        """
        Compute stable XY bounds for fit-view.
        Geometry drives bounds; text anchors are fallback only.
        """
        if not entities:
            return None

        geom_min_x = float("inf")
        geom_min_y = float("inf")
        geom_max_x = float("-inf")
        geom_max_y = float("-inf")
        has_geom = False

        text_min_x = float("inf")
        text_min_y = float("inf")
        text_max_x = float("-inf")
        text_max_y = float("-inf")
        has_text = False

        def _update_xy(x_val, y_val, use_text_bucket=False):
            nonlocal geom_min_x, geom_min_y, geom_max_x, geom_max_y, has_geom
            nonlocal text_min_x, text_min_y, text_max_x, text_max_y, has_text
            try:
                x = float(x_val)
                y = float(y_val)
            except Exception:
                return
            if use_text_bucket:
                if x < text_min_x:
                    text_min_x = x
                if y < text_min_y:
                    text_min_y = y
                if x > text_max_x:
                    text_max_x = x
                if y > text_max_y:
                    text_max_y = y
                has_text = True
                return
            if x < geom_min_x:
                geom_min_x = x
            if y < geom_min_y:
                geom_min_y = y
            if x > geom_max_x:
                geom_max_x = x
            if y > geom_max_y:
                geom_max_y = y
            has_geom = True

        for ent in entities:
            etype = str(ent.get("type", "")).lower()
            if etype in ("polyline", "line"):
                pts = ent.get("points") or ent.get("vertices") or []
                for pt in pts:
                    if len(pt) >= 2:
                        _update_xy(pt[0], pt[1], use_text_bucket=False)
            elif etype == "3dface":
                for pt in ent.get("vertices", []) or []:
                    if len(pt) >= 2:
                        _update_xy(pt[0], pt[1], use_text_bucket=False)
            elif etype == "point":
                pos = ent.get("position", [])
                if len(pos) >= 2:
                    _update_xy(pos[0], pos[1], use_text_bucket=False)
            elif etype == "text":
                pos = ent.get("position", [])
                if len(pos) >= 2:
                    _update_xy(pos[0], pos[1], use_text_bucket=True)

        if has_geom:
            return (geom_min_x, geom_max_x, geom_min_y, geom_max_y)
        if has_text:
            return (text_min_x, text_max_x, text_min_y, text_max_y)
        return None

    def _process_snt_file(self, item: SNTFileItem) -> Optional[Dict]:
        try:
            if item.cached_parsed:
                parsed = item.cached_parsed
            else:
                parsed = _read_snt_file(str(item.snt_path))
                item.cached_parsed = parsed

            color_override = item.override_color if item.override_enabled else None
            raw_entities   = parsed.get("entities", [])
            entity_count = len(raw_entities)
            item.update_entity_count(entity_count)
            layers_meta = parsed.get("layers", [])
            stats_map: Dict[str, Dict[str, object]] = {}
            for layer in layers_meta:
                lname = layer.get("name", "0")
                lcolor = layer.get("color", _DEFAULT_COLOR)
                stats_map[lname] = {"count": 0, "color": lcolor}
            for ent in raw_entities:
                lname = ent.get("layer", "0")
                if lname not in stats_map:
                    stats_map[lname] = {"count": 0, "color": _DEFAULT_COLOR}
                stats_map[lname]["count"] = int(stats_map[lname]["count"]) + 1
            layer_stats = [
                (lname, int(meta.get("count", 0)), tuple(meta.get("color", _DEFAULT_COLOR)))
                for lname, meta in stats_map.items()
            ]
            item.layer_stats_cache = list(layer_stats)

            filtered: List[Dict] = []
            for ent in raw_entities:
                if (item.selected_layers is not None
                        and ent.get("layer", "0") not in item.selected_layers):
                    continue
                if color_override:
                    ent = {**ent, "color": color_override}
                filtered.append(ent)

            render_entities = self._convert_entities(filtered)
            bounds = self._compute_2d_bounds(render_entities)

            return {
                "filename":  item.snt_path.name,
                "full_path": str(item.snt_path.resolve()),
                "mode":      item.display_mode,
                "entities":  render_entities,
                "bounds": bounds,
                "entity_count": entity_count,
                "actor_cache_map": item.actor_cache,
                "selected_layers": item.selected_layers,
                "layer_stats": layer_stats,
                "override_enabled": item.override_enabled,
                "override_color": item.override_color,
                "parsed": item.cached_parsed
            }
        except Exception:
            return None

    def _convert_entities(self, entities: List[Dict]) -> List[Dict]:
        out: List[Dict] = []
        for ent in entities:
            etype = ent.get("type", "")
            color = ent.get("color", _DEFAULT_COLOR)
            layer = ent.get("layer", "0")

            if etype == "POLYLINE":
                verts = ent.get("vertices", [])
                if len(verts) < 2:
                    continue
                out.append({
                    "type":   "polyline",
                    "points": verts,
                    "closed": ent.get("closed", False),
                    "color":  color,
                    "layer":  layer,
                })
            elif etype == "3DFACE":
                verts = ent.get("vertices", [])
                if len(verts) < 3:
                    continue
                v = list(verts[:4])
                while len(v) < 4:
                    v.append(v[-1])
                p2 = v[2]
                p3 = v[3]
                is_tri = (
                    abs(float(p2[0]) - float(p3[0])) <= 1e-6
                    and abs(float(p2[1]) - float(p3[1])) <= 1e-6
                    and abs(float(p2[2]) - float(p3[2])) <= 1e-6
                )
                out.append({
                    "type":        "3dface",
                    "vertices":    v,
                    "is_triangle": is_tri,
                    "color":       color,
                    "layer":       layer,
                })
            elif etype == "TEXT":
                text = str(ent.get("text", "")).strip()
                score = _score_label_cached(text)
                # FIX: Never skip TEXT entities on the BLOCKS layer — these are
                # block/file names that must always be visible regardless of score.
                is_blocks_layer = (layer == "BLOCKS")
                if score == 0 and not is_blocks_layer:
                    continue
                label_color, label_height = _label_color_and_height_cached(text)
                if score < 100 and color != _DEFAULT_COLOR:
                    label_color = color
                pos = ent.get("position", (0.0, 0.0, 0.0))
                out.append({
                    "type":     "text",
                    "text":     text,
                    "position": pos,
                    "height":   label_height,
                    "rotation": 0.0,
                    "color":    label_color,
                    "layer":    layer,
                })
            elif etype == "POINT":
                pos = ent.get("position", (0.0, 0.0, 0.0))
                out.append({
                    "type":     "point",
                    "position": pos,
                    "color":    color,
                    "layer":    layer,
                })
        return out

    def _render_snt_in_vtk(self, attachment: Dict) -> None:
        try:
            import vtk
            from vtkmodules.util import numpy_support

            if not hasattr(self.app, "vtk_widget"):
                return

            renderer = self.app.vtk_widget.renderer
            actors: List = []
            z_offset = _get_snt_z_offset(self.app)
            item: Optional[SNTFileItem] = attachment.get("_snt_item")
            entities = attachment.get("entities", [])

            line_groups: Dict = defaultdict(list)
            text_ents: List = []
            point_groups: Dict = defaultdict(list)
            face_groups: Dict = defaultdict(list)

            for e in entities:
                etype = e.get("type", "").lower()
                if etype in ("line", "polyline"):
                    line_groups[(tuple(e["color"]), e["layer"])].append(e)
                elif etype == "text":
                    text_ents.append(e)
                elif etype == "point":
                    point_groups[(tuple(e["color"]), e["layer"])].append(e)
                elif etype == "3dface":
                    face_groups[(tuple(e["color"]), e["layer"])].append(e)

            def _cache(actor, layer_name: str) -> None:
                if item is not None:
                    item.actor_cache.setdefault(layer_name, []).append(actor)

            def _tag_snt(actor) -> None:
                try:
                    from gui.optimized_refresh import tag_actor_as_dxf
                    tag_actor_as_dxf(actor)
                except Exception:
                    setattr(actor, "_is_dxf_actor", True)

            bounds_valid = False
            xmin = float("inf")
            ymin = float("inf")
            zmin = float("inf")
            xmax = float("-inf")
            ymax = float("-inf")
            zmax = float("-inf")

            def _update_bounds_np(arr: np.ndarray) -> None:
                nonlocal bounds_valid, xmin, ymin, zmin, xmax, ymax, zmax
                if arr is None or arr.size == 0:
                    return
                try:
                    mins = arr.min(axis=0)
                    maxs = arr.max(axis=0)
                except Exception:
                    return
                x0, y0, z0 = float(mins[0]), float(mins[1]), float(mins[2])
                x1, y1, z1 = float(maxs[0]), float(maxs[1]), float(maxs[2])
                if not bounds_valid:
                    xmin, ymin, zmin = x0, y0, z0
                    xmax, ymax, zmax = x1, y1, z1
                    bounds_valid = True
                    return
                if x0 < xmin:
                    xmin = x0
                if y0 < ymin:
                    ymin = y0
                if z0 < zmin:
                    zmin = z0
                if x1 > xmax:
                    xmax = x1
                if y1 > ymax:
                    ymax = y1
                if z1 > zmax:
                    zmax = z1

            def _update_bounds_point(pos) -> None:
                nonlocal bounds_valid, xmin, ymin, zmin, xmax, ymax, zmax
                try:
                    x = float(pos[0])
                    y = float(pos[1])
                    z = float(pos[2]) if len(pos) > 2 else 0.0
                except Exception:
                    return
                if not bounds_valid:
                    xmin = xmax = x
                    ymin = ymax = y
                    zmin = zmax = z
                    bounds_valid = True
                    return
                if x < xmin:
                    xmin = x
                if y < ymin:
                    ymin = y
                if z < zmin:
                    zmin = z
                if x > xmax:
                    xmax = x
                if y > ymax:
                    ymax = y
                if z > zmax:
                    zmax = z

            def _build_line_actor(color, layer_name: str, chunk: List[Dict]) -> None:
                valid = [e for e in chunk if len(e.get("points", [])) >= 2]
                if not valid:
                    return

                # Preserve closed-polyline topology by explicitly repeating the
                # first vertex when needed. Without this, closed LWPOLYLINE
                # entities render with one missing edge.
                prepared_pts: List[np.ndarray] = []
                total_pts = 0
                for e in valid:
                    pts_np = np.asarray(e["points"], dtype=np.float32)
                    if bool(e.get("closed", False)) and len(pts_np) >= 3:
                        try:
                            is_already_closed = np.allclose(pts_np[0], pts_np[-1], atol=1e-6)
                        except Exception:
                            is_already_closed = False
                        if not is_already_closed:
                            pts_np = np.vstack((pts_np, pts_np[0]))
                    prepared_pts.append(pts_np)
                    total_pts += len(pts_np)

                total_cells_items = total_pts + len(valid)

                combined_pts = np.empty((total_pts, 3), dtype=np.float32)
                combined_cells = np.empty(total_cells_items, dtype=np.int64)

                p_off = 0
                c_off = 0
                for pts_np in prepared_pts:
                    n_p = len(pts_np)
                    combined_pts[p_off:p_off + n_p] = pts_np
                    combined_cells[c_off] = n_p
                    combined_cells[c_off + 1:c_off + 1 + n_p] = np.arange(
                        p_off, p_off + n_p, dtype=np.int64
                    )
                    p_off += n_p
                    c_off += (n_p + 1)

                _update_bounds_np(combined_pts)

                pts = vtk.vtkPoints()
                pts.SetData(numpy_support.numpy_to_vtk(combined_pts, deep=True))

                cells = vtk.vtkCellArray()
                cells.ImportLegacyFormat(numpy_support.numpy_to_vtkIdTypeArray(combined_cells))

                pd = vtk.vtkPolyData()
                pd.SetPoints(pts)
                pd.SetLines(cells)

                mapper = vtk.vtkPolyDataMapper()
                mapper.SetInputData(pd)
                mapper.SetResolveCoincidentTopologyToPolygonOffset()
                mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(-10000.0, -10000.0)

                actor = vtk.vtkActor()
                actor.SetMapper(mapper)
                actor.GetProperty().SetColor([c / 255.0 for c in color])
                actor.GetProperty().SetLineWidth(4.0)
                actor.GetProperty().SetLighting(False)
                actor.GetProperty().SetAmbient(1.0)
                actor.GetProperty().SetOpacity(1.0)

                actor._original_color = color
                _tag_snt(actor)
                _apply_z_offset_to_actor(actor, z_offset)
                renderer.AddActor(actor)
                actors.append(actor)
                _cache(actor, layer_name)

            def _build_point_actor(color, layer_name: str, chunk: List[Dict]) -> None:
                if not chunk:
                    return

                pts_data = np.asarray([e.get("position", (0.0, 0.0, 0.0)) for e in chunk], dtype=np.float32)
                if pts_data.size == 0:
                    return
                _update_bounds_np(pts_data)

                pts = vtk.vtkPoints()
                pts.SetData(numpy_support.numpy_to_vtk(pts_data, deep=True))

                n_pts = len(chunk)
                cells_data = np.empty(n_pts * 2, dtype=np.int64)
                cells_data[0::2] = 1
                cells_data[1::2] = np.arange(n_pts, dtype=np.int64)

                verts = vtk.vtkCellArray()
                verts.ImportLegacyFormat(numpy_support.numpy_to_vtkIdTypeArray(cells_data))

                pd = vtk.vtkPolyData()
                pd.SetPoints(pts)
                pd.SetVerts(verts)

                mapper = vtk.vtkPolyDataMapper()
                mapper.SetInputData(pd)

                actor = vtk.vtkActor()
                actor.SetMapper(mapper)
                actor.GetProperty().SetColor([c / 255.0 for c in color])
                actor.GetProperty().SetPointSize(5.0)
                actor.GetProperty().SetLighting(False)

                _tag_snt(actor)
                _apply_z_offset_to_actor(actor, z_offset)
                renderer.AddActor(actor)
                actors.append(actor)
                _cache(actor, layer_name)

            def _build_face_actor(color, layer_name: str, chunk: List[Dict]) -> None:
                valid = [e for e in chunk if len(e.get("vertices", [])) >= 3]
                if not valid:
                    return

                total_pts = 0
                total_poly_items = 0
                for e in valid:
                    n_v = 3 if e.get("is_triangle", False) else min(4, len(e.get("vertices", [])))
                    total_pts += n_v
                    total_poly_items += (n_v + 1)

                combined_pts = np.empty((total_pts, 3), dtype=np.float32)
                combined_polys = np.empty(total_poly_items, dtype=np.int64)

                p_off = 0
                c_off = 0
                for e in valid:
                    n_v = 3 if e.get("is_triangle", False) else min(4, len(e.get("vertices", [])))
                    verts_np = np.asarray(e.get("vertices", [])[:n_v], dtype=np.float32)
                    if len(verts_np) < 3:
                        continue
                    count = len(verts_np)
                    combined_pts[p_off:p_off + count] = verts_np
                    combined_polys[c_off] = count
                    combined_polys[c_off + 1:c_off + 1 + count] = np.arange(
                        p_off, p_off + count, dtype=np.int64
                    )
                    p_off += count
                    c_off += (count + 1)

                if p_off == 0 or c_off == 0:
                    return

                combined_pts = combined_pts[:p_off]
                combined_polys = combined_polys[:c_off]
                _update_bounds_np(combined_pts)

                pts = vtk.vtkPoints()
                pts.SetData(numpy_support.numpy_to_vtk(combined_pts, deep=True))

                polys = vtk.vtkCellArray()
                polys.ImportLegacyFormat(numpy_support.numpy_to_vtkIdTypeArray(combined_polys))

                pd = vtk.vtkPolyData()
                pd.SetPoints(pts)
                pd.SetPolys(polys)

                mapper = vtk.vtkPolyDataMapper()
                mapper.SetInputData(pd)

                actor = vtk.vtkActor()
                actor.SetMapper(mapper)
                prop = actor.GetProperty()
                prop.SetColor([c / 255.0 for c in color])
                prop.SetLighting(False)
                prop.SetOpacity(0.55)

                actor._original_color = color
                _tag_snt(actor)
                _apply_z_offset_to_actor(actor, z_offset)
                renderer.AddActor(actor)
                actors.append(actor)
                _cache(actor, layer_name)

            def _build_face_outline_actor(color, layer_name: str, chunk: List[Dict]) -> None:
                """
                Keep 3DFACE visible in 2D plan mode even when filled faces are disabled.
                Reuses the line batching path so we don't introduce a new rendering stack.
                """
                outline_entities: List[Dict] = []
                for e in chunk:
                    verts = e.get("vertices", [])
                    if len(verts) < 3:
                        continue
                    if e.get("is_triangle", False):
                        ring = list(verts[:3])
                    else:
                        ring = list(verts[:4])
                        if len(ring) == 4:
                            try:
                                if (
                                    abs(float(ring[2][0]) - float(ring[3][0])) <= 1e-6
                                    and abs(float(ring[2][1]) - float(ring[3][1])) <= 1e-6
                                    and abs(float(ring[2][2]) - float(ring[3][2])) <= 1e-6
                                ):
                                    ring = ring[:3]
                            except Exception:
                                pass

                    if len(ring) < 3:
                        continue

                    outline_entities.append({
                        "points": ring,
                        "closed": True,
                    })

                if outline_entities:
                    _build_line_actor(color, layer_name, outline_entities)

            line_chunk_points = _env_int("NAKSHA_SNT_LINE_CHUNK_POINTS", 2000000, min_value=1000)
            point_chunk_size = _env_int("NAKSHA_SNT_POINT_CHUNK_SIZE", 2000000, min_value=1000)
            face_chunk_faces = _env_int("NAKSHA_SNT_FACE_CHUNK_FACES", 150000, min_value=1000)
            render_faces = _env_int("NAKSHA_SNT_RENDER_3DFACE", 0, min_value=0) > 0
            render_face_outlines = _env_int("NAKSHA_SNT_RENDER_3DFACE_OUTLINE", 1, min_value=0) > 0

            for (color, layer_name), group in line_groups.items():
                chunk: List[Dict] = []
                chunk_pts = 0
                for e in group:
                    n_p = len(e.get("points", []))
                    if n_p < 2:
                        continue
                    if chunk and (chunk_pts + n_p > line_chunk_points):
                        _build_line_actor(color, layer_name, chunk)
                        chunk = []
                        chunk_pts = 0
                    chunk.append(e)
                    chunk_pts += n_p
                if chunk:
                    _build_line_actor(color, layer_name, chunk)

            for (color, layer_name), group in point_groups.items():
                n = len(group)
                if n <= point_chunk_size:
                    _build_point_actor(color, layer_name, group)
                else:
                    for start in range(0, n, point_chunk_size):
                        _build_point_actor(color, layer_name, group[start:start + point_chunk_size])

            if render_faces:
                for (color, layer_name), group in face_groups.items():
                    n = len(group)
                    if n <= face_chunk_faces:
                        _build_face_actor(color, layer_name, group)
                    else:
                        for start in range(0, n, face_chunk_faces):
                            _build_face_actor(color, layer_name, group[start:start + face_chunk_faces])
            elif render_face_outlines:
                for (color, layer_name), group in face_groups.items():
                    n = len(group)
                    if n <= face_chunk_faces:
                        _build_face_outline_actor(color, layer_name, group)
                    else:
                        for start in range(0, n, face_chunk_faces):
                            _build_face_outline_actor(color, layer_name, group[start:start + face_chunk_faces])

            selected_text_ents, text_stats = _select_text_entities_for_render(text_ents)
            for e in selected_text_ents:
                a = self._create_text_actor(e)
                if a:
                    _update_bounds_point(e.get("position", (0.0, 0.0, 0.0)))
                    if a.GetMapper():
                        a.GetMapper().SetResolveCoincidentTopologyToPolygonOffset()
                        a.GetMapper().SetRelativeCoincidentTopologyPolygonOffsetParameters(-20000.0, -20000.0)

                    if "color" in e:
                        a._original_color = e["color"]
                    _tag_snt(a)
                    _apply_z_offset_to_actor(a, z_offset)
                    renderer.AddActor(a)
                    actors.append(a)
                    _cache(a, e.get("layer", "0"))

            fpath = str(Path(attachment.get("full_path", attachment["filename"])).resolve())
            actor_entry = {
                "filename": attachment["filename"],
                "full_path": fpath,
                "actors": actors,
                "bounds": attachment.get("bounds") if attachment.get("bounds") is not None else (
                    (xmin, xmax, ymin, ymax) if bounds_valid else None
                ),
            }
            self.app.snt_actors.append(actor_entry)
            self.app.dxf_actors.append(actor_entry)

            build_snt_block_polygons(self.app, entities, attachment["filename"])

            n_total = len(actors)
            print(
                f"SNT LOADED: {n_total} actors "
                f"(labels rendered: {text_stats['selected']}/{text_stats['total']}, "
                f"line groups: {len(line_groups)}, point groups: {len(point_groups)}, "
                f"face groups: {len(face_groups)}"
                f"{' [faces off]' if not render_faces else ''}"
                f"{' [face outlines on]' if (not render_faces and render_face_outlines and face_groups) else ''})"
            )
            if text_stats["selected"] < text_stats["total"]:
                print(
                    f"  label decimation active: rendered {text_stats['selected']} of {text_stats['total']} "
                    f"(limit={text_stats['limit']})"
                )
            if n_total > 500:
                print(f"  high actor count: {n_total}")

            try:
                from gui.optimized_refresh import invalidate_dxf_actor_cache
                invalidate_dxf_actor_cache()
            except Exception:
                pass

            attachment["actors"] = actors

            if bounds_valid:
                renderer.ResetCamera((xmin, xmax, ymin, ymax, zmin, zmax))
            renderer.ResetCameraClippingRange()
            self.app.vtk_widget.render()
        except Exception as e:
            print(f"Render Error: {e}")

    def _create_text_actor(self, entity: Dict):
        """
        REVERTED: Using vtkFollower to ensure 'No Loss' in visual look and scaling.
        We keep the Point Batching for performance, but restore VectorText for labels.
        """
        import vtk
        text_content = str(entity.get("text", "")).strip()
        if not text_content:
            return None
        
        cache_limit = _env_int("NAKSHA_SNT_TEXT_GEOM_CACHE", 4096, min_value=0)
        text_cache = getattr(self, "_snt_text_poly_cache", None)
        if text_cache is None:
            text_cache = {}
            self._snt_text_poly_cache = text_cache

        text_poly = text_cache.get(text_content)
        if text_poly is None:
            text_source = vtk.vtkVectorText()
            text_source.SetText(text_content)
            text_source.Update()
            text_poly = vtk.vtkPolyData()
            text_poly.DeepCopy(text_source.GetOutput())
            if cache_limit > 0 and len(text_cache) < cache_limit:
                text_cache[text_content] = text_poly

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(text_poly)
        
        actor = vtk.vtkFollower()
        actor.SetMapper(mapper)
        actor.text_content = text_content
        actor.is_grid_label = True
        actor.grid_name     = text_content
        actor.PickableOn()
        
        # Original scaling logic
        bounds      = text_poly.GetBounds()
        text_width  = bounds[1] - bounds[0]
        text_height = bounds[3] - bounds[2]
        pos = entity["position"]
        mag = abs(pos[0]) + abs(pos[1])
        
        if   mag > 100_000: desired_w, desired_h = 80.0, 20.0
        elif mag > 10_000:  desired_w, desired_h = 40.0, 10.0
        elif mag > 1_000:   desired_w, desired_h = 16.0,  4.0
        else:               desired_w, desired_h =  4.0,  1.0
        
        scale = (min(desired_w / text_width, desired_h / text_height)
                 if text_width > 0 and text_height > 0 else 1.0)
        actor.SetScale(scale, scale, scale)
        
        prop = actor.GetProperty()
        prop.SetColor(_normalise_vtk_color(entity["color"]))
        prop.SetLineWidth(3.0)
        prop.SetOpacity(1.0)
        prop.SetAmbient(0.6)
        prop.SetDiffuse(0.9)
        prop.SetLighting(False)
        
        try:
            actor.SetCamera(self.app.vtk_widget.renderer.GetActiveCamera())
        except Exception:
            pass
            
        half_w = (text_width  * scale) / 2.0
        half_h = (text_height * scale) / 2.0
        actor.SetPosition(pos[0] - half_w, pos[1] - half_h, pos[2] if len(pos) > 2 else 0.0)
        
        return actor

    def _create_point_actor(self, entity: Dict):
        import vtk
        pts = vtk.vtkPoints()
        pts.InsertNextPoint(entity["position"])
        verts = vtk.vtkCellArray()
        verts.InsertNextCell(1)
        verts.InsertCellPoint(0)
        pd = vtk.vtkPolyData()
        pd.SetPoints(pts)
        pd.SetVerts(verts)
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(pd)
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(_normalise_vtk_color(entity["color"]))
        actor.GetProperty().SetPointSize(5.0)
        return actor

def show_snt_attachment_dialog(app) -> MultiSNTAttachmentDialog:
    if hasattr(app, "snt_dialog") and app.snt_dialog is not None:
        try:
            dlg = app.snt_dialog
            if dlg.isVisible():
                dlg.setWindowState(dlg.windowState() & ~Qt.WindowMinimized | Qt.WindowActive)
                dlg.raise_()
                dlg.activateWindow()
            else:
                dlg.show()
                dlg.raise_()
                dlg.activateWindow()
            return dlg
        except RuntimeError:
            app.snt_dialog = None

    dlg = MultiSNTAttachmentDialog(app, parent=app)
    dlg.setModal(False)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    app.snt_dialog = dlg

    def _on_close(event):
        event.ignore()
        dlg.hide()

    dlg.closeEvent = _on_close
    return dlg