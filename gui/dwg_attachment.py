
"""
dwg_attachment.py
─────────────────────────────────────────────────────────────────────────────
Direct DWG loading system for NakshaAI.

NO conversion. NO temp files. NO DXF intermediate.
Reads DWG binary directly via ezdxf (supports AC1015–AC1032 / R2000–R2018+).

Architecture
────────────
  DWGLoadWorker          – QThread: reads DWG, counts layers, detects PRJ
  DWGFileItem            – Row widget per file (checkbox, PRJ badge, count,
                           layer picker, display options, remove)
  DWGLayerDialog         – Checkbox list of layers with All/None/Invert
  DWGDisplayDialog       – Overlay/Underlay + colour override
  MultiDWGDialog         – Main dialog (Browse → list → Attach All)

Integration
────────────
  • app.vtk_widget.renderer   – renderer used directly
  • app.vtk_widget.renderer.GetActiveCamera() – for follower actors
  • app.crs                   – project CRS (pyproj)
  • app.dwg_actors            – list[dict] stored on app (mirrors dxf_actors)
  • app.dwg_attachments       – list[dict] stored on app

Coordinates
────────────
  If a .PRJ sits next to the .DWG and app.crs is set, every vertex is
  reprojected with pyproj Transformer (always_xy=True) in the worker thread,
  so the render is already in project coordinates by the time the main thread
  touches it.  z_offset of +0.1 m places DWG above the point cloud.
"""

from __future__ import annotations

import os
import struct
import traceback
import numpy as np
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFileDialog, QMessageBox, QCheckBox, QGroupBox, QRadioButton,
    QScrollArea, QWidget, QComboBox, QListWidget, QListWidgetItem,
    QProgressDialog, QSizePolicy,
)
from PySide6.QtCore import Qt, Signal, QThread, QCoreApplication, QTimer
from PySide6.QtGui import QColor
from gui.theme_manager import (
    get_dialog_stylesheet, get_progress_dialog_stylesheet,
    get_title_banner_style, get_file_item_row_style,
    get_badge_style, get_icon_button_style, get_notice_banner_style, ThemeColors,
)
from gui.minimize_chip import MinimizableDialogMixin

# ── optional deps (always available in the target env) ──────────────────────
try:
    import ezdxf
    from ezdxf.document import Drawing
    from ezdxf.addons import odafc as _odafc
    _EZDXF = True
except ImportError:
    _EZDXF = False

try:
    from pyproj import CRS, Transformer
    _PYPROJ = True
except ImportError:
    _PYPROJ = False

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# Full AutoCAD Color Index (ACI) 1-255 → normalised RGB
# Generated from the official ACI palette
_ACI: dict[int, tuple[float, float, float]] = {
    1:  (1.000, 0.000, 0.000),  # red
    2:  (1.000, 1.000, 0.000),  # yellow
    3:  (0.000, 1.000, 0.000),  # green
    4:  (0.000, 1.000, 1.000),  # cyan
    5:  (0.000, 0.000, 1.000),  # blue
    6:  (1.000, 0.000, 1.000),  # magenta
    7:  (1.000, 1.000, 1.000),  # white
    8:  (0.420, 0.420, 0.420),
    9:  (0.749, 0.749, 0.749),
    10: (1.000, 0.000, 0.000),
    11: (1.000, 0.702, 0.702),
    12: (0.651, 0.000, 0.000),
    13: (0.651, 0.455, 0.455),
    14: (0.502, 0.000, 0.000),
    15: (0.502, 0.353, 0.353),
    16: (1.000, 0.302, 0.000),
    17: (1.000, 0.800, 0.702),
    18: (0.651, 0.196, 0.000),
    19: (0.651, 0.522, 0.455),
    20: (0.502, 0.153, 0.000),
    21: (0.502, 0.400, 0.353),
    22: (1.000, 0.502, 0.000),
    23: (1.000, 0.851, 0.702),
    24: (0.651, 0.325, 0.000),
    25: (0.651, 0.553, 0.455),
    26: (0.502, 0.251, 0.000),
    27: (0.502, 0.427, 0.353),
    28: (1.000, 0.702, 0.000),
    29: (1.000, 0.902, 0.702),
    30: (0.651, 0.455, 0.000),
    31: (0.651, 0.588, 0.455),
    32: (0.502, 0.353, 0.000),
    33: (0.502, 0.451, 0.353),
    34: (1.000, 0.851, 0.000),
    35: (1.000, 0.949, 0.702),
    36: (0.651, 0.553, 0.000),
    37: (0.651, 0.620, 0.455),
    38: (0.502, 0.427, 0.000),
    39: (0.502, 0.475, 0.353),
    40: (1.000, 1.000, 0.000),
    41: (1.000, 1.000, 0.702),
    42: (0.651, 0.651, 0.000),
    43: (0.651, 0.651, 0.455),
    44: (0.502, 0.502, 0.000),
    45: (0.502, 0.502, 0.353),
    46: (0.702, 1.000, 0.000),
    47: (0.902, 1.000, 0.702),
    48: (0.455, 0.651, 0.000),
    49: (0.588, 0.651, 0.455),
    50: (0.353, 0.502, 0.000),
    51: (0.451, 0.502, 0.353),
    52: (0.502, 1.000, 0.000),
    53: (0.800, 1.000, 0.702),
    54: (0.325, 0.651, 0.000),
    55: (0.522, 0.651, 0.455),
    56: (0.251, 0.502, 0.000),
    57: (0.400, 0.502, 0.353),
    58: (0.302, 1.000, 0.000),
    59: (0.702, 1.000, 0.702),
    60: (0.196, 0.651, 0.000),
    61: (0.455, 0.651, 0.455),
    62: (0.153, 0.502, 0.000),
    63: (0.353, 0.502, 0.353),
    64: (0.000, 1.000, 0.000),
    65: (0.702, 1.000, 0.702),
    66: (0.000, 0.651, 0.000),
    67: (0.455, 0.651, 0.455),
    68: (0.000, 0.502, 0.000),
    69: (0.353, 0.502, 0.353),
    70: (0.000, 1.000, 0.302),
    71: (0.702, 1.000, 0.800),
    72: (0.000, 0.651, 0.196),
    73: (0.455, 0.651, 0.522),
    74: (0.000, 0.502, 0.153),
    75: (0.353, 0.502, 0.400),
    76: (0.000, 1.000, 0.502),
    77: (0.702, 1.000, 0.851),
    78: (0.000, 0.651, 0.325),
    79: (0.455, 0.651, 0.553),
    80: (0.000, 0.502, 0.251),
    81: (0.353, 0.502, 0.427),
    82: (0.000, 1.000, 0.702),
    83: (0.702, 1.000, 0.902),
    84: (0.000, 0.651, 0.455),
    85: (0.455, 0.651, 0.588),
    86: (0.000, 0.502, 0.353),
    87: (0.353, 0.502, 0.451),
    88: (0.000, 1.000, 0.851),
    89: (0.702, 1.000, 0.949),
    90: (0.000, 0.651, 0.553),
    91: (0.455, 0.651, 0.620),
    92: (0.000, 0.502, 0.427),
    93: (0.353, 0.502, 0.475),
    94: (0.000, 1.000, 1.000),
    95: (0.702, 1.000, 1.000),
    96: (0.000, 0.651, 0.651),
    97: (0.455, 0.651, 0.651),
    98: (0.000, 0.502, 0.502),
    99: (0.353, 0.502, 0.502),
    100:(0.000, 0.851, 1.000),
    101:(0.702, 0.949, 1.000),
    102:(0.000, 0.553, 0.651),
    103:(0.455, 0.620, 0.651),
    104:(0.000, 0.427, 0.502),
    105:(0.353, 0.475, 0.502),
    106:(0.000, 0.702, 1.000),
    107:(0.702, 0.902, 1.000),
    108:(0.000, 0.455, 0.651),
    109:(0.455, 0.588, 0.651),
    110:(0.000, 0.353, 0.502),
    111:(0.353, 0.451, 0.502),
    112:(0.000, 0.502, 1.000),
    113:(0.702, 0.800, 1.000),
    114:(0.000, 0.325, 0.651),
    115:(0.455, 0.522, 0.651),
    116:(0.000, 0.251, 0.502),
    117:(0.353, 0.400, 0.502),
    118:(0.000, 0.302, 1.000),
    119:(0.702, 0.800, 1.000),
    120:(0.000, 0.196, 0.651),
    121:(0.455, 0.455, 0.651),
    122:(0.000, 0.153, 0.502),
    123:(0.353, 0.353, 0.502),
    124:(0.000, 0.000, 1.000),
    125:(0.702, 0.702, 1.000),
    126:(0.000, 0.000, 0.651),
    127:(0.455, 0.455, 0.651),
    128:(0.000, 0.000, 0.502),
    129:(0.353, 0.353, 0.502),
    130:(0.302, 0.000, 1.000),
    131:(0.800, 0.702, 1.000),
    132:(0.196, 0.000, 0.651),
    133:(0.522, 0.455, 0.651),
    134:(0.153, 0.000, 0.502),
    135:(0.400, 0.353, 0.502),
    136:(0.502, 0.000, 1.000),
    137:(0.851, 0.702, 1.000),
    138:(0.325, 0.000, 0.651),
    139:(0.553, 0.455, 0.651),
    140:(0.251, 0.000, 0.502),
    141:(0.427, 0.353, 0.502),
    142:(0.702, 0.000, 1.000),
    143:(0.902, 0.702, 1.000),
    144:(0.455, 0.000, 0.651),
    145:(0.588, 0.455, 0.651),
    146:(0.353, 0.000, 0.502),
    147:(0.451, 0.353, 0.502),
    148:(0.851, 0.000, 1.000),
    149:(0.949, 0.702, 1.000),
    150:(0.553, 0.000, 0.651),
    151:(0.620, 0.455, 0.651),
    152:(0.427, 0.000, 0.502),
    153:(0.475, 0.353, 0.502),
    154:(1.000, 0.000, 1.000),
    155:(1.000, 0.702, 1.000),
    156:(0.651, 0.000, 0.651),
    157:(0.651, 0.455, 0.651),
    158:(0.502, 0.000, 0.502),
    159:(0.502, 0.353, 0.502),
    160:(1.000, 0.000, 0.702),
    161:(1.000, 0.702, 0.902),
    162:(0.651, 0.000, 0.455),
    163:(0.651, 0.455, 0.588),
    164:(0.502, 0.000, 0.353),
    165:(0.502, 0.353, 0.451),
    166:(1.000, 0.000, 0.502),
    167:(1.000, 0.702, 0.851),
    168:(0.651, 0.000, 0.325),
    169:(0.651, 0.455, 0.553),
    170:(0.502, 0.000, 0.251),
    171:(0.502, 0.353, 0.427),
    172:(1.000, 0.000, 0.302),
    173:(1.000, 0.702, 0.800),
    174:(0.651, 0.000, 0.196),
    175:(0.651, 0.455, 0.522),
    176:(0.502, 0.000, 0.153),
    177:(0.502, 0.353, 0.400),
    178:(1.000, 0.000, 0.000),
    179:(1.000, 0.702, 0.702),
    180:(0.651, 0.000, 0.000),
    181:(0.651, 0.455, 0.455),
    182:(0.502, 0.000, 0.000),
    183:(0.502, 0.353, 0.353),
    184:(0.333, 0.333, 0.333),
    185:(0.467, 0.467, 0.467),
    186:(0.600, 0.600, 0.600),
    187:(0.733, 0.733, 0.733),
    188:(0.867, 0.867, 0.867),
    189:(1.000, 1.000, 1.000),
    250:(0.063, 0.063, 0.063),
    251:(0.188, 0.188, 0.188),
    252:(0.314, 0.314, 0.314),
    253:(0.502, 0.502, 0.502),
    254:(0.753, 0.753, 0.753),
    255:(1.000, 1.000, 1.000),
}
_WHITE = (1.0, 1.0, 1.0)

# Sentinel for "entity was skipped"
_SKIP = None

# How far above point cloud Z-max to render DWG (metres)
_Z_LIFT = 0.10


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _layer_rgb(doc, layer_name: str) -> tuple[float, float, float]:
    """Look up a layer's own ACI color from the document layer table."""
    try:
        layer = doc.layers.get(layer_name)
        if layer is not None:
            idx = layer.dxf.color
            if idx and idx > 0:
                return _ACI.get(abs(idx), _WHITE)
    except Exception:
        pass
    return _WHITE


def _aci_rgb(entity, doc=None) -> tuple[float, float, float]:
    """
    Resolve entity colour with full BYLAYER fallback.
    color=256 means BYLAYER → look up the layer's color in the layer table.
    color=0   means BYBLOCK → default white.
    """
    try:
        idx = entity.dxf.color
        if idx == 256:  # BYLAYER
            if doc is not None:
                return _layer_rgb(doc, entity.dxf.layer)
        elif idx == 0:  # BYBLOCK
            return _WHITE
        elif 1 <= idx <= 255:
            return _ACI.get(idx, _WHITE)
    except Exception:
        pass
    return _WHITE


def _override_rgb(rgb_tuple: tuple[int, int, int]) -> tuple[float, float, float]:
    """Convert 0-255 override colour to 0-1 normalised."""
    return tuple(c / 255.0 for c in rgb_tuple)


def _read_prj(prj_path: Path):
    """Parse a .PRJ file and return a CRS or None."""
    if not _PYPROJ:
        return None
    try:
        return CRS.from_wkt(prj_path.read_text(encoding="utf-8", errors="ignore").strip())
    except Exception:
        return None


def _make_transformer(src_crs, dst_crs):
    """Return a Transformer or None."""
    if not (_PYPROJ and src_crs and dst_crs):
        return None
    try:
        return Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    except Exception:
        return None


def _xform(pt, transformer, z_lift: float, origin=None) -> tuple[float, float, float]:
    """Transform a point and subtract point-cloud origin to align coordinate spaces."""
    x, y, z = float(pt[0]), float(pt[1]), (float(pt[2]) if len(pt) > 2 else 0.0)
    if transformer:
        try:
            x, y = transformer.transform(x, y)
        except Exception:
            pass
    if origin:
        x -= origin[0]
        y -= origin[1]
        z -= origin[2]
    return x, y, z + z_lift


def _configure_odafc():
    if not _EZDXF:
        return
    import sys, glob
    if sys.platform != "win32":
        return
    current = getattr(_odafc, 'win_exec_path', '')
    if current and os.path.isfile(current):
        return
    for root in [r"C:\Program Files\ODA", r"C:\Program Files (x86)\ODA"]:
        hits = sorted(glob.glob(os.path.join(root, "ODAFileConverter*", "ODAFileConverter.exe")), reverse=True)
        if hits:
            try:
                ezdxf.options.set("odafc-addon", "win_exec_path", hits[0])
            except Exception:
                _odafc.win_exec_path = hits[0]
            return

_configure_odafc()


def _load_doc(path: Path) -> "Drawing":
    if path.suffix.lower() == ".dwg":
        return _odafc.readfile(str(path))
    return ezdxf.readfile(str(path))


def _get_point_cloud_origin(app):
    """
    NakshaAI renders point clouds at their RAW UTM coordinates (no centroid
    subtraction happens anywhere in class_display.py or app_window.py).
    DWG files are also in raw UTM.  Therefore NO coordinate offset should
    be subtracted — both datasets already share the same coordinate space.

    Returns None so _xform() skips the subtraction entirely.
    """
    return None


# ─────────────────────────────────────────────────────────────────────────────
# GEOMETRY EXTRACTION  (no VTK — pure data, so it's safe in worker thread)
# ─────────────────────────────────────────────────────────────────────────────

def _extract_geometry(dxf_doc: "Drawing",
                      transformer,
                      z_lift: float,
                      selected_layers,
                      color_override=None,
                      origin=None,
                      ) -> dict[str, list]:
    """
    Walk modelspace, extract geometry into plain Python/numpy arrays.
    Returns dict: layer_name → list of geometry dicts
    """
    result: dict[str, list] = {}

    def layer_ok(name: str) -> bool:
        if selected_layers is None:
            return True
        return name in selected_layers

    def colour(entity) -> tuple[float, float, float]:
        if color_override:
            return _override_rgb(color_override)
        return _aci_rgb(entity, dxf_doc)   # pass doc for BYLAYER lookup

    def ensure(name: str):
        if name not in result:
            result[name] = []

    for entity in dxf_doc.modelspace():
        try:
            layer = entity.dxf.layer
            if not layer_ok(layer):
                continue
            ensure(layer)
            etype = entity.dxftype()

            # ── LINE ────────────────────────────────────────────────
            if etype == "LINE":
                s = entity.dxf.start
                e = entity.dxf.end
                p0 = _xform((s.x, s.y, getattr(s, 'z', 0.0)), transformer, z_lift, origin)
                p1 = _xform((e.x, e.y, getattr(e, 'z', 0.0)), transformer, z_lift, origin)
                pts  = np.array([p0, p1], dtype=np.float64)
                segs = np.array([[0, 1]], dtype=np.int32)
                result[layer].append({'type': 'lines', 'pts': pts,
                                      'segs': segs, 'color': colour(entity)})

            # ── LWPOLYLINE ───────────────────────────────────────────
            elif etype == "LWPOLYLINE":
                raw = list(entity.get_points())
                if len(raw) < 2:
                    continue
                pts = np.array([
                    _xform((p[0], p[1], p[2] if len(p) > 2 else 0.0), transformer, z_lift, origin)
                    for p in raw
                ], dtype=np.float64)
                n = len(pts)
                closed = getattr(entity, 'is_closed', False)
                segs = np.array([[i, i+1] for i in range(n-1)] +
                                ([[n-1, 0]] if closed and n > 2 else []),
                                dtype=np.int32)
                result[layer].append({'type': 'lines', 'pts': pts,
                                      'segs': segs, 'color': colour(entity)})

            # ── POLYLINE ─────────────────────────────────────────────
            elif etype == "POLYLINE":
                verts = list(entity.vertices)
                if len(verts) < 2:
                    continue
                pts_list = []
                for v in verts:
                    loc = v.dxf.location
                    pts_list.append(_xform((loc.x, loc.y, loc.z), transformer, z_lift, origin))
                pts  = np.array(pts_list, dtype=np.float64)
                n    = len(pts)
                segs = np.array([[i, i+1] for i in range(n-1)], dtype=np.int32)
                result[layer].append({'type': 'lines', 'pts': pts,
                                      'segs': segs, 'color': colour(entity)})

            # ── CIRCLE ───────────────────────────────────────────────
            elif etype == "CIRCLE":
                c  = entity.dxf.center
                r  = float(entity.dxf.radius)
                N  = 64
                angles = np.linspace(0, 2*np.pi, N, endpoint=False)
                xs = c.x + r * np.cos(angles)
                ys = c.y + r * np.sin(angles)
                zs = np.full(N, getattr(c, 'z', 0.0))
                raw_pts = np.column_stack([xs, ys, zs])
                # transform each point
                pts = np.array([
                    _xform((raw_pts[i,0], raw_pts[i,1], raw_pts[i,2]), transformer, z_lift, origin)
                    for i in range(N)
                ], dtype=np.float64)
                segs = np.array([[i, (i+1) % N] for i in range(N)], dtype=np.int32)
                result[layer].append({'type': 'lines', 'pts': pts,
                                      'segs': segs, 'color': colour(entity)})

            # ── ARC ──────────────────────────────────────────────────
            elif etype == "ARC":
                c        = entity.dxf.center
                r        = float(entity.dxf.radius)
                start_a  = float(entity.dxf.start_angle)
                end_a    = float(entity.dxf.end_angle)
                if end_a < start_a:
                    end_a += 360.0
                N      = max(32, int((end_a - start_a) / 5))
                angles = np.linspace(np.radians(start_a), np.radians(end_a), N)
                xs = c.x + r * np.cos(angles)
                ys = c.y + r * np.sin(angles)
                zs = np.full(N, getattr(c, 'z', 0.0))
                pts = np.array([
                    _xform((xs[i], ys[i], zs[i]), transformer, z_lift, origin)
                    for i in range(N)
                ], dtype=np.float64)
                segs = np.array([[i, i+1] for i in range(N-1)], dtype=np.int32)
                result[layer].append({'type': 'lines', 'pts': pts,
                                      'segs': segs, 'color': colour(entity)})

            # ── SPLINE ───────────────────────────────────────────────
            elif etype == "SPLINE":
                try:
                    pts_raw = list(entity.control_points)
                    if len(pts_raw) < 2:
                        continue
                    pts = np.array([
                        _xform((p[0], p[1], p[2] if len(p) > 2 else 0.0), transformer, z_lift, origin)
                        for p in pts_raw
                    ], dtype=np.float64)
                    n    = len(pts)
                    segs = np.array([[i, i+1] for i in range(n-1)], dtype=np.int32)
                    result[layer].append({'type': 'lines', 'pts': pts,
                                          'segs': segs, 'color': colour(entity)})
                except Exception:
                    pass

            # ── POINT ────────────────────────────────────────────────
            elif etype == "POINT":
                loc = entity.dxf.location
                pt  = _xform((loc.x, loc.y, getattr(loc, 'z', 0.0)), transformer, z_lift, origin)
                pts = np.array([pt], dtype=np.float64)
                result[layer].append({'type': 'points', 'pts': pts,
                                      'color': colour(entity)})

            # ── INSERT (block reference) ─────────────────────────────
            elif etype == "INSERT":
                # Expand attribs as text points
                try:
                    for attrib in entity.attribs:
                        ins = attrib.dxf.insert
                        pt  = _xform((ins.x, ins.y, getattr(ins, 'z', 0.0)), transformer, z_lift, origin)
                        pts = np.array([pt], dtype=np.float64)
                        result[layer].append({'type': 'points', 'pts': pts,
                                              'color': colour(entity),
                                              'label': str(attrib.dxf.text)[:32]})
                except Exception:
                    pass

        except Exception:
            pass   # skip bad entities silently

    return result


# ─────────────────────────────────────────────────────────────────────────────
# VTK ACTOR FACTORY  (main thread only)
# ─────────────────────────────────────────────────────────────────────────────

def _geom_to_actors(renderer, layer_geom: dict[str, list]) -> dict[str, list]:
    """Build VTK actors — one merged actor per (layer, color, type) group."""
    import vtk
    from collections import defaultdict

    actor_cache: dict[str, list] = {}
    total_actors = 0

    for layer_name, geoms in layer_geom.items():
        actor_cache.setdefault(layer_name, [])

        groups = defaultdict(lambda: {'pts': [], 'segs': [], 'offset': 0})
        for geom in geoms:
            key = (geom['color'], geom['type'])
            g = groups[key]
            pts = geom['pts']
            g['pts'].append(pts)
            if geom['type'] == 'lines':
                g['segs'].append(geom['segs'] + g['offset'])
            g['offset'] += len(pts)

        for (color, gtype), g in groups.items():
            try:
                if not g['pts']:
                    continue
                all_pts = np.vstack(g['pts'])

                vtk_pts = vtk.vtkPoints()
                for p in all_pts:
                    vtk_pts.InsertNextPoint(float(p[0]), float(p[1]), float(p[2]))

                pd = vtk.vtkPolyData()
                pd.SetPoints(vtk_pts)

                if gtype == 'lines' and g['segs']:
                    all_segs = np.vstack(g['segs'])
                    cells = vtk.vtkCellArray()
                    for seg in all_segs:
                        line = vtk.vtkLine()
                        line.GetPointIds().SetId(0, int(seg[0]))
                        line.GetPointIds().SetId(1, int(seg[1]))
                        cells.InsertNextCell(line)
                    pd.SetLines(cells)
                    mapper = vtk.vtkPolyDataMapper()
                    mapper.SetInputData(pd)
                    actor = vtk.vtkActor()
                    actor.SetMapper(mapper)
                    actor.GetProperty().SetColor(*color)
                    actor.GetProperty().SetLineWidth(1.5)

                elif gtype == 'points':
                    verts = vtk.vtkCellArray()
                    for i in range(vtk_pts.GetNumberOfPoints()):
                        verts.InsertNextCell(1)
                        verts.InsertCellPoint(i)
                    pd.SetVerts(verts)
                    mapper = vtk.vtkPolyDataMapper()
                    mapper.SetInputData(pd)
                    actor = vtk.vtkActor()
                    actor.SetMapper(mapper)
                    actor.GetProperty().SetColor(*color)
                    actor.GetProperty().SetPointSize(4)
                else:
                    continue

                renderer.AddActor(actor)
                actor_cache[layer_name].append(actor)
                total_actors += 1

            except Exception as ex:
                print(f"  ⚠️  Actor build failed [{layer_name}]: {ex}")

    print(f"  🎨 _geom_to_actors: {total_actors} actors across {len(actor_cache)} layers")
    return actor_cache


# ─────────────────────────────────────────────────────────────────────────────
# BACKGROUND LOAD WORKER
# ─────────────────────────────────────────────────────────────────────────────

class DWGLoadWorker(QThread):
    """
    Reads DWG file directly with ezdxf in a background thread.
    No conversion, no temp files.

    Signals
    ───────
    progress(value:int, message:str, indeterminate:bool)
    file_ready(item_data:dict)   – emitted once per file when fully read
    finished()
    error(message:str)
    """
    progress   = Signal(int, str, bool)
    file_ready = Signal(dict)
    finished   = Signal()
    error      = Signal(str)

    def __init__(self, file_paths: list[str], project_crs=None):
        super().__init__()
        self.file_paths  = file_paths
        self.project_crs = project_crs
        self._cancelled  = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            if not _EZDXF:
                self.error.emit(
                    "ezdxf is not installed.\n\n"
                    "Install it with:  pip install ezdxf\n\n"
                    "ezdxf reads DWG files directly with no conversion."
                )
                return

            total = len(self.file_paths)

            for idx, fp in enumerate(self.file_paths):
                if self._cancelled:
                    return

                dwg_path = Path(fp)
                fname    = dwg_path.name
                single   = (total == 1)

                self.progress.emit(
                    idx,
                    f"📐 Reading {fname}..." if single else f"📐 {fname} ({idx+1}/{total})",
                    single
                )

                item: dict = {
                    'dwg_path':    dwg_path,
                    'prj_exists':  False,
                    'prj_path':    None,
                    'crs':         None,
                    'doc':         None,
                    'layers':      [],
                    'entity_count': 0,
                    'error':       None,
                }

                # ── PRJ detection ────────────────────────────────────
                for sfx in ('.prj', '.PRJ'):
                    pp = dwg_path.with_suffix(sfx)
                    if pp.exists():
                        item['prj_exists'] = True
                        item['prj_path']   = pp
                        break

                # ── Read DWG directly ────────────────────────────────
                try:
                    if single:
                        self.progress.emit(0, f"📖 Loading {fname}...", True)

                    doc = _load_doc(dwg_path)
                    item['doc'] = doc

                    if single:
                        self.progress.emit(0, f"🔢 Counting entities...", True)

                    ms = doc.modelspace()
                    count = sum(1 for _ in ms)
                    item['entity_count'] = count

                    # ── Layer names ──────────────────────────────────
                    item['layers'] = sorted(
                        layer.dxf.name for layer in doc.layers
                    )

                    # ── Parse PRJ ────────────────────────────────────
                    if item['prj_exists']:
                        if single:
                            self.progress.emit(0, "🗺️  Parsing coordinate system...", True)
                        item['crs'] = _read_prj(item['prj_path'])

                    print(f"  ✅ DWG read: {fname} — {count} entities, "
                          f"{len(item['layers'])} layers")

                except Exception as e:
                    item['error'] = str(e)
                    print(f"  ❌ DWG read failed: {fname} — {e}")

                self.file_ready.emit(item)

            self.finished.emit()

        except Exception as e:
            self.error.emit(f"Worker failed:\n{e}\n\n{traceback.format_exc()}")


# ─────────────────────────────────────────────────────────────────────────────
# LAYER SELECTION DIALOG
# ─────────────────────────────────────────────────────────────────────────────

class DWGLayerDialog(QDialog):
    """Checkbox list of layers — All / None / Invert helpers."""

    def __init__(self, layer_names: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Layers")
        self.setModal(True)
        self.resize(340, 460)
        self.setProperty("themeStyledDialog", True)
        self.setStyleSheet(get_dialog_stylesheet())

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(6)

        title = QLabel("Layers")
        title.setObjectName("dialogTitle")
        root.addWidget(title)

        self.lw = QListWidget()
        self.lw.setAlternatingRowColors(True)
        for name in layer_names:
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, name)
            item.setCheckState(Qt.Checked)
            self.lw.addItem(item)
        root.addWidget(self.lw)

        # All / None / Invert
        btn_row = QHBoxLayout()
        for label, fn in [("All", self._all), ("None", self._none), ("Invert", self._invert)]:
            b = QPushButton(label)
            b.setAutoDefault(False)
            b.setDefault(False)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(fn)
            btn_row.addWidget(b)
        root.addLayout(btn_row)

        # OK / Cancel
        ok_row = QHBoxLayout(); ok_row.addStretch()
        ok  = QPushButton("OK")
        ok.setObjectName("primaryBtn")
        ok.setAutoDefault(False)
        ok.setDefault(False)
        ok.setFocusPolicy(Qt.NoFocus)
        ok.clicked.connect(self.accept)
        can = QPushButton("Cancel")
        can.setAutoDefault(False)
        can.setDefault(False)
        can.setFocusPolicy(Qt.NoFocus)
        can.clicked.connect(self.reject)
        ok_row.addWidget(ok); ok_row.addWidget(can)
        root.addLayout(ok_row)

    def _all(self):
        for i in range(self.lw.count()):
            self.lw.item(i).setCheckState(Qt.Checked)

    def _none(self):
        for i in range(self.lw.count()):
            self.lw.item(i).setCheckState(Qt.Unchecked)

    def _invert(self):
        for i in range(self.lw.count()):
            it = self.lw.item(i)
            it.setCheckState(Qt.Unchecked if it.checkState() == Qt.Checked else Qt.Checked)

    def selected_layers(self) -> set[str] | None:
        """Return selected set, or None if ALL selected (no filtering needed)."""
        total    = self.lw.count()
        selected = set()
        for i in range(total):
            it = self.lw.item(i)
            if it.checkState() == Qt.Checked:
                selected.add(it.data(Qt.UserRole))
        if len(selected) == total:
            return None   # all = no filter
        return selected


# ─────────────────────────────────────────────────────────────────────────────
# DISPLAY OPTIONS DIALOG
# ─────────────────────────────────────────────────────────────────────────────

class DWGDisplayDialog(QDialog):
    """Overlay / Underlay mode + optional colour override."""

    _COLOURS = [
        ("Orange",  (255, 165,   0)),
        ("Red",     (255,   0,   0)),
        ("Green",   (  0, 255,   0)),
        ("Blue",    (  0,   0, 255)),
        ("Yellow",  (255, 255,   0)),
        ("Cyan",    (  0, 255, 255)),
        ("Magenta", (255,   0, 255)),
        ("White",   (255, 255, 255)),
    ]

    def __init__(self, parent=None, mode="overlay",
                 override_enabled=False, override_color=(255, 165, 0)):
        super().__init__(parent)
        self.setWindowTitle("DWG Display Options")
        self.setModal(True)
        self.resize(280, 190)
        self.setProperty("themeStyledDialog", True)
        self.setStyleSheet(get_dialog_stylesheet())

        lay = QVBoxLayout(self); lay.setContentsMargins(12, 12, 12, 12); lay.setSpacing(8)

        # Mode row
        mr = QHBoxLayout()
        mr.addWidget(QLabel("Mode:"))
        self.over  = QRadioButton("Overlay (on top)")
        self.under = QRadioButton("Underlay (below)")
        (self.under if mode == "underlay" else self.over).setChecked(True)
        mr.addWidget(self.over); mr.addWidget(self.under)
        lay.addLayout(mr)

        # Colour override
        cr = QHBoxLayout()
        self.ov_chk  = QCheckBox("Override colour:")
        self.ov_cmb  = QComboBox()
        for name, rgb in self._COLOURS:
            self.ov_cmb.addItem(name, rgb)
        self.ov_chk.setChecked(override_enabled)
        self.ov_cmb.setEnabled(override_enabled)
        self.ov_chk.toggled.connect(self.ov_cmb.setEnabled)
        # Pre-select current colour
        for i in range(self.ov_cmb.count()):
            if self.ov_cmb.itemData(i) == override_color:
                self.ov_cmb.setCurrentIndex(i)
                break
        cr.addWidget(self.ov_chk); cr.addWidget(self.ov_cmb)
        lay.addLayout(cr)

        # OK/Cancel
        br = QHBoxLayout(); br.addStretch()
        ok = QPushButton("OK")
        ok.setObjectName("primaryBtn")
        ok.setAutoDefault(False)
        ok.setDefault(False)
        ok.setFocusPolicy(Qt.NoFocus)
        ok.clicked.connect(self.accept)
        can = QPushButton("Cancel")
        can.setAutoDefault(False)
        can.setDefault(False)
        can.setFocusPolicy(Qt.NoFocus)
        can.clicked.connect(self.reject)
        br.addWidget(ok); br.addWidget(can)
        lay.addLayout(br)

    def values(self):
        mode  = "underlay" if self.under.isChecked() else "overlay"
        color = self.ov_cmb.currentData() if self.ov_chk.isChecked() else None
        return mode, self.ov_chk.isChecked(), color


# ─────────────────────────────────────────────────────────────────────────────
# FILE ROW WIDGET
# ─────────────────────────────────────────────────────────────────────────────

class DWGFileItem(QWidget):
    """
    One row in the file list.
    Stores the loaded ezdxf Document directly — no copy, no conversion.
    """

    remove_requested = Signal(object)  # self

    def __init__(self, dwg_path: Path, prj_exists: bool, parent=None):
        super().__init__(parent)

        # ── State ─────────────────────────────────────────
        self.dwg_path      = Path(dwg_path)
        self.prj_exists    = prj_exists

        self.doc           = None          # ezdxf Drawing (set after worker done)
        self.crs           = None          # pyproj CRS from PRJ
        self.layers: list[str] = []
        self.entity_count  = 0

        self.display_mode      = "overlay"
        self.override_enabled  = False
        self.override_color    = (255, 165, 0)   # orange default
        self.selected_layers   = None             # None = all

        self.actor_cache: dict[str, list] = {}   # layer → [vtkActor]

        self._build_ui()

    # ── UI ────────────────────────────────────────────────

    def _build_ui(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(5, 5, 5, 5)
        lay.setSpacing(6)

        # Checkbox + filename
        self.chk = QCheckBox(f"{self.dwg_path.name}")
        self.chk.setChecked(True)
        self.chk.setStyleSheet(f"color:{ThemeColors.get('accent')};font-weight:bold;font-size:10px;")
        self.chk.stateChanged.connect(self._on_check)
        lay.addWidget(self.chk, 1)

        # PRJ badge
        if self.prj_exists:
            badge = QLabel("PRJ")
            badge.setStyleSheet(get_badge_style("success"))
        else:
            badge = QLabel("No PRJ")
            badge.setStyleSheet(get_badge_style("warning"))
        lay.addWidget(badge)

        # Count label
        self.cnt = QLabel("reading...")
        self.cnt.setStyleSheet(f"color:{ThemeColors.get('text_muted')};font-size:9px;")
        lay.addWidget(self.cnt)

        # Layer picker
        lb = self._icon_btn("L", "default", "Select layers")
        lb.clicked.connect(self._pick_layers)
        lay.addWidget(lb)

        # Display options
        ob = self._icon_btn("S", "settings", "Display options")
        ob.clicked.connect(self._display_opts)
        lay.addWidget(ob)

        # Remove
        rb = self._icon_btn("X", "danger", "Remove")
        rb.clicked.connect(lambda: self.remove_requested.emit(self))
        lay.addWidget(rb)

        self.setStyleSheet(get_file_item_row_style())

    @staticmethod
    def _icon_btn(icon, role, tip):
        b = QPushButton(icon)
        b.setFixedSize(26, 26)
        b.setToolTip(tip)
        b.setStyleSheet(get_icon_button_style(role))
        return b

    # ── Slots ─────────────────────────────────────────────

    def set_loaded(self, doc, entity_count: int, layers: list[str], crs=None):
        """Called from main thread after worker emits file_ready."""
        self.doc          = doc
        self.entity_count = entity_count
        self.layers       = layers
        self.crs          = crs
        self.cnt.setText(f"{entity_count} ent · {len(layers)} layers")
        self.cnt.setStyleSheet(f"color:{ThemeColors.get('accent')};font-size:9px;font-weight:bold;")

    def set_error(self, msg: str):
        self.cnt.setText(msg[:38])
        self.cnt.setStyleSheet(f"color:{ThemeColors.get('danger')};font-size:9px;")

    def _on_check(self, state):
        vis = (state == 2)  # Qt.Checked == 2
        for actors in self.actor_cache.values():
            for a in actors:
                a.SetVisibility(vis)
        self._render()

    def _pick_layers(self):
        if not self.layers:
            QMessageBox.information(self, "No layers", "File not loaded yet.")
            return

        dlg = DWGLayerDialog(self.layers, self)
        # Pre-check based on current selection
        if self.selected_layers is not None:
            for i in range(dlg.lw.count()):
                it = dlg.lw.item(i)
                it.setCheckState(
                    Qt.Checked if it.data(Qt.UserRole) in self.selected_layers
                    else Qt.Unchecked
                )

        if dlg.exec() != QDialog.Accepted:
            return

        self.selected_layers = dlg.selected_layers()

        # Update count label
        if self.selected_layers is None:
            self.cnt.setText(f"{self.entity_count} ent · all layers")
            self.cnt.setStyleSheet(f"color:{ThemeColors.get('accent')};font-size:9px;font-weight:bold;")
        else:
            n = len(self.selected_layers)
            self.cnt.setText(f"{self.entity_count} ent · {n}/{len(self.layers)} layers")
            self.cnt.setStyleSheet(f"color:{ThemeColors.get('text_secondary')};font-size:9px;font-weight:bold;")

        # Instant visibility update on existing actors
        if self.actor_cache:
            for layer, actors in self.actor_cache.items():
                vis = (self.selected_layers is None) or (layer in self.selected_layers)
                for a in actors:
                    a.SetVisibility(vis)
            self._render()

    def _display_opts(self):
        dlg = DWGDisplayDialog(
            self, self.display_mode, self.override_enabled, self.override_color
        )
        if dlg.exec() == QDialog.Accepted:
            mode, ov_en, ov_col = dlg.values()
            self.display_mode     = mode
            self.override_enabled = ov_en
            if ov_col:
                self.override_color = ov_col

    def _render(self):
        """Walk up to the dialog and trigger a VTK render."""
        p = self.parent()
        while p and not isinstance(p, MultiDWGDialog):
            p = p.parent()
        if p:
            p._do_render()


# ─────────────────────────────────────────────────────────────────────────────
# MAIN DIALOG
# ─────────────────────────────────────────────────────────────────────────────

class MultiDWGDialog(MinimizableDialogMixin, QDialog):
    """
    Main DWG attachment dialog.

    Browse → worker reads files → list items appear → Attach All builds VTK actors.
    Point cloud and DWG share the same renderer; coordinates are matched via CRS.
    """

    dwg_attached = Signal(list)   # list of dicts with metadata

    def __init__(self, app, parent=None):
        # Find a valid Qt parent
        from PySide6.QtWidgets import QWidget as _QWidget
        qparent = None
        for candidate in (parent, app,
                          getattr(app, 'window', None)):
            if isinstance(candidate, _QWidget):
                qparent = candidate
                break

        # Parentless top-level window keeps native taskbar minimize behavior.
        super().__init__(None, Qt.Window)
        self._init_minimize_state()
        self.setAttribute(Qt.WA_QuitOnClose, False)
        self.setWindowModality(Qt.NonModal)

        self.app   = app
        self.items: list[DWGFileItem] = []
        self._worker = None
        self._close_requested_while_loading = False
        self.setProperty("themeStyledDialog", True)

        self.setWindowTitle("DWG Attachment")
        self.setStyleSheet(get_dialog_stylesheet())
        self.setGeometry(170, 170, 700, 740)

        self._build_ui()
        self._detect_crs()

        # Ensure storage lists exist on app
        if not hasattr(app, 'dwg_actors'):
            app.dwg_actors = []
        if not hasattr(app, 'dwg_attachments'):
            app.dwg_attachments = []
        self._guard_installed = False
        self._guard_renderer = None
        self._guard_observer_id = None
        self._guard_last_z = None

    # ── UI Build ──────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        # Title
        title = QLabel("DWG Attachment")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(get_title_banner_style())
        root.addWidget(title)

        intro = QLabel(
            "Review DWG overlays before attaching them to the project. If matching PRJ data "
            "is available, the dialog will align coordinates automatically."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(get_notice_banner_style("info"))
        root.addWidget(intro)

        # ── dep warning if ezdxf missing ──
        if not _EZDXF:
            warn = QLabel(
                "ezdxf is not installed.\n"
                "Run:  pip install ezdxf\n"
                "ezdxf reads DWG/DXF files natively — no conversion needed."
            )
            warn.setWordWrap(True)
            warn.setStyleSheet(get_notice_banner_style("danger"))
            root.addWidget(warn)

        # ── CRS info ──
        self.crs_lbl = QLabel("Detecting project CRS...")
        self.crs_lbl.setStyleSheet(get_notice_banner_style("info"))
        self.crs_lbl.setWordWrap(True)
        root.addWidget(self.crs_lbl)

        # ── Browse ──
        browse_grp = QGroupBox("Select DWG Files")
        bl = QVBoxLayout()
        browse_btn = QPushButton("Browse and Add DWG Files...")
        browse_btn.setObjectName("secondaryBtn")
        browse_btn.setAutoDefault(False)
        browse_btn.setDefault(False)
        browse_btn.setFocusPolicy(Qt.NoFocus)
        browse_btn.clicked.connect(self._browse)
        bl.addWidget(browse_btn)
        browse_grp.setLayout(bl)
        root.addWidget(browse_grp)

        # ── File list ──
        list_grp = QGroupBox("Loaded DWG Files  (X to remove)")
        ll = QVBoxLayout()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(180)
        scroll.setMaximumHeight(320)
        self._list_body   = QWidget()
        self._list_layout = QVBoxLayout(self._list_body)
        self._list_layout.setSpacing(4)
        self._list_layout.setContentsMargins(4, 4, 4, 4)
        self._list_layout.addStretch()
        scroll.setWidget(self._list_body)
        ll.addWidget(scroll)

        self.count_lbl = QLabel("No files loaded")
        self.count_lbl.setStyleSheet(f"color:{ThemeColors.get('text_muted')};font-size:10px;padding:4px;")
        ll.addWidget(self.count_lbl)
        list_grp.setLayout(ll)
        root.addWidget(list_grp)

        # ── Coordinate note ──
        coord_note = QLabel(
            "DWG geometry is rendered directly into the same VTK scene as the "
            "point cloud.  If a .PRJ file sits next to the .DWG and the project "
            "CRS is known, all coordinates are reprojected automatically so they "
            "align with your point cloud data."
        )
        coord_note.setWordWrap(True)
        coord_note.setStyleSheet(get_notice_banner_style("info"))
        root.addWidget(coord_note)

        # ── Action buttons ──
        btn_row = QHBoxLayout()

        clear_btn = QPushButton("Clear All")
        clear_btn.setObjectName("dangerBtn")
        clear_btn.setAutoDefault(False)
        clear_btn.setDefault(False)
        clear_btn.setFocusPolicy(Qt.NoFocus)
        clear_btn.clicked.connect(self._clear_all)
        btn_row.addWidget(clear_btn)

        btn_row.addStretch()

        attach_btn = QPushButton("Attach All DWG Files")
        attach_btn.setObjectName("primaryBtn")
        attach_btn.setAutoDefault(False)
        attach_btn.setDefault(False)
        attach_btn.setFocusPolicy(Qt.NoFocus)
        attach_btn.clicked.connect(self._attach_all)
        btn_row.addWidget(attach_btn)

        root.addLayout(btn_row)

        # Status bar
        self.status = QLabel("Ready")
        self.status.setStyleSheet(f"color:{ThemeColors.get('text_muted')};font-size:9px;padding:2px;")
        root.addWidget(self.status)

    # ── CRS Detection ─────────────────────────────────────

    def _detect_crs(self):
        app = self.app
        crs = None
        # Authoritative source first: the canvas CRS established by whichever
        # georeferenced dataset was loaded first.
        try:
            from gui.crs_manager import get_canvas_crs
            crs = get_canvas_crs(app)
        except Exception:
            crs = None
        if crs is None:
            # Legacy fallback for older app objects without canvas_crs.
            for attr in ('crs', 'project_crs', 'point_cloud_crs'):
                c = getattr(app, attr, None)
                if c is not None:
                    crs = c
                    break
        self._project_crs = crs
        if crs:
            self.crs_lbl.setText(f"Project CRS: {crs.name}")
            self.crs_lbl.setStyleSheet(get_notice_banner_style("success"))
        else:
            self.crs_lbl.setText(
                "No project CRS detected — DWG will be displayed in its own coordinates."
            )
            self.crs_lbl.setStyleSheet(get_notice_banner_style("warning"))

    # ── Browse ─────────────────────────────────────────────

    def _browse(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select DWG Files", "",
            "DWG Files (*.dwg *.DWG);;All Files (*)"
        )
        if not paths:
            return
        self._load_files(paths)

    def _load_files(self, paths: list[str]):
        total      = len(paths)
        single     = (total == 1)

        prog = QProgressDialog(
            "Reading DWG file…" if single else "Reading DWG files…",
            "Cancel",
            0, 0 if single else total,
            self
        )
        prog.setWindowTitle("Loading DWG")
        prog.setWindowModality(Qt.WindowModal)
        prog.setMinimumDuration(0)
        prog.setValue(0)
        prog.setStyleSheet(get_progress_dialog_stylesheet())
        prog.show()
        QCoreApplication.processEvents()

        worker = DWGLoadWorker(paths, self._project_crs)
        self._worker = worker   # keep reference

        def on_progress(val, msg, indet):
            prog.setLabelText(msg)
            if not indet:
                prog.setValue(val)

        def on_ready(item_data: dict):
            dwg_path  = item_data['dwg_path']
            # Duplicate guard
            for ex in self.items:
                if ex.dwg_path == dwg_path:
                    return

            row = DWGFileItem(dwg_path, item_data['prj_exists'])
            row.remove_requested.connect(self._remove_item)

            # Insert before the stretch
            self._list_layout.insertWidget(len(self.items), row)
            self.items.append(row)

            if item_data['error']:
                row.set_error(item_data['error'])
            else:
                row.set_loaded(
                    item_data['doc'],
                    item_data['entity_count'],
                    item_data['layers'],
                    item_data['crs'],
                )
            self._update_count()

        def on_finished():
            self._worker = None
            prog.setValue(total if not single else 0)
            prog.close()
            self._prompt_missing_crs_for_rows()
            self._set_status(f"{total} file(s) ready")
            if self._close_requested_while_loading:
                self._close_requested_while_loading = False
                QTimer.singleShot(0, self.close)

        def on_error(msg):
            self._worker = None
            prog.close()
            QMessageBox.critical(self, "Load Error", msg)
            if self._close_requested_while_loading:
                self._close_requested_while_loading = False
                QTimer.singleShot(0, self.close)

        def on_cancel():
            worker.cancel()
            worker.wait(800)
            if worker.isRunning():
                prog.setLabelText("Cancelling DWG load...")
                prog.setCancelButton(None)
                print("⏳ Waiting for DWG load worker to stop cooperatively")
            self._set_status("❌ Cancelled")

        worker.progress.connect(on_progress)
        worker.file_ready.connect(on_ready)
        worker.finished.connect(on_finished)
        worker.error.connect(on_error)
        prog.canceled.connect(on_cancel)

        worker.start()

    def _prompt_missing_crs_for_rows(self):
        """Ask once for a CRS to apply to every loaded DWG that has no .prj.

        Runs on the main thread after the reader worker finishes, so the modal
        dialog is safe. Rows whose CRS stays unresolved are simply left in raw
        coordinates (and will be reported as 'raw coords' at attach time).
        """
        canvas_crs = getattr(self, "_project_crs", None)
        if canvas_crs is None:
            try:
                from gui.crs_manager import get_canvas_crs
                canvas_crs = get_canvas_crs(self.app)
            except Exception:
                canvas_crs = None
        if canvas_crs is None:
            return

        missing = [r for r in self.items if r.doc is not None and r.crs is None]
        if not missing:
            return

        try:
            from gui.crs_selector_dialog import prompt_missing_source_crs
            picked = prompt_missing_source_crs(
                self,
                f"{len(missing)} DWG file(s) without a .prj",
                "DWG",
                canvas_crs,
            )
        except Exception as exc:
            print(f"  ⚠️ DWG CRS prompt failed: {exc}")
            return

        if picked is None:
            print("ℹ️ DWG: source CRS left unresolved for CRS-less file(s)")
            return

        for row in missing:
            row.crs = picked
        print(f"  ✅ Assigned {picked.name} to {len(missing)} DWG file(s)")

    # ── File Management ────────────────────────────────────

    def _remove_item(self, row: DWGFileItem):
        """Remove one DWG row — clears its VTK actors too."""
        renderer = self._get_renderer()
        if renderer:
            for actors in row.actor_cache.values():
                for a in actors:
                    renderer.RemoveActor(a)
            self._do_render()

        # Remove from app storage
        fname = row.dwg_path.name
        if hasattr(self.app, 'dwg_actors'):
            self.app.dwg_actors = [
                d for d in self.app.dwg_actors if d.get('filename') != fname
            ]
        if hasattr(self.app, 'dwg_attachments'):
            self.app.dwg_attachments = [
                d for d in self.app.dwg_attachments if d.get('filename') != fname
            ]
        if not getattr(self.app, 'dwg_actors', []):
            self._remove_render_guard()

        self._list_layout.removeWidget(row)
        row.setParent(None)
        row.deleteLater()
        self.items.remove(row)
        self._update_count()
        try:
            from gui.crs_manager import reconcile_canvas_crs_after_content_change
            reconcile_canvas_crs_after_content_change(
                self.app, reason="DWG attachment removed"
            )
        except Exception as exc:
            print(f"[CRS] reconciliation after DWG removal failed: {exc}")

    def _clear_all(self):
        for row in list(self.items):
            self._remove_item(row)
        self._remove_render_guard()

    def _update_count(self):
        n = len(self.items)
        if n == 0:
            self.count_lbl.setText("No files loaded")
            self.count_lbl.setStyleSheet(f"color:{ThemeColors.get('text_muted')};font-size:10px;padding:4px;")
        else:
            total_e  = sum(r.entity_count for r in self.items)
            prj_cnt  = sum(1 for r in self.items if r.prj_exists)
            self.count_lbl.setText(
                f"{n} file(s) · {total_e:,} entities · {prj_cnt} with PRJ"
            )
            self.count_lbl.setStyleSheet(
                f"color:{ThemeColors.get('accent')};font-size:10px;font-weight:bold;padding:4px;"
            )

    # ── Attach All ────────────────────────────────────────

    def _attach_all(self):
        checked = [r for r in self.items if r.chk.isChecked() and r.doc is not None]
        if not checked:
            QMessageBox.information(self, "Nothing to attach",
                                    "No loaded DWG files are checked.")
            return

        renderer = self._get_renderer()
        if renderer is None:
            QMessageBox.critical(self, "No Renderer",
                                 "Cannot access the VTK renderer.\n"
                                 "Make sure a point cloud is loaded first.")
            return

        # Establish the canvas CRS from the first checked DWG that carries a
        # .prj, then read it back so every row reprojects into the same CRS.
        try:
            from gui.crs_manager import ensure_canvas_crs, get_canvas_crs, log_dataset_crs
            for _row in checked:
                if _row.crs is not None:
                    ensure_canvas_crs(self.app, _row.crs,
                                      source="DWG adjacent WKT .prj",
                                      dataset=str(_row.dwg_path))
                    break
            self._project_crs = get_canvas_crs(self.app)
        except Exception as _crs_exc:
            print(f"  ⚠️ DWG canvas CRS update failed: {_crs_exc}")

        origin  = _get_point_cloud_origin(self.app)  # always None — no offset needed
        cloud_z = self._cloud_z_max()
        z_lift  = cloud_z + _Z_LIFT
        print(f"  📍 DWG z_lift: {z_lift:.3f}m (cloud_z={cloud_z:.3f})")

        prog = QProgressDialog(
            "Attaching DWG files…", "Cancel",
            0, len(checked), self
        )
        prog.setWindowTitle("Attaching DWG")
        prog.setWindowModality(Qt.WindowModal)
        prog.setMinimumDuration(0)
        prog.setStyleSheet(get_progress_dialog_stylesheet())
        prog.show()
        QCoreApplication.processEvents()

        attached = []
        for idx, row in enumerate(checked):
            if prog.wasCanceled():
                break

            prog.setLabelText(f"Attaching {row.dwg_path.name}…")
            prog.setValue(idx)
            QCoreApplication.processEvents()

            # Build transformer
            transformer = _make_transformer(row.crs, self._project_crs)
            try:
                from gui.crs_manager import log_dataset_crs
                log_dataset_crs(row.dwg_path.name, "DWG", row.crs,
                                "DWG adjacent WKT .prj",
                                canvas_crs=self._project_crs)
            except Exception:
                pass

            # Extract geometry (pure data — fast)
            self._set_status(f"⏳ Extracting {row.dwg_path.name}…")
            QCoreApplication.processEvents()

            layer_geom = _extract_geometry(
                dxf_doc        = row.doc,
                transformer    = transformer,
                z_lift         = z_lift,
                selected_layers= row.selected_layers,
                color_override = row.override_color if row.override_enabled else None,
                origin         = origin,
            )

            # Build VTK actors (main thread)
            self._set_status(f"🎨 Building actors for {row.dwg_path.name}…")
            QCoreApplication.processEvents()

            actor_cache = _geom_to_actors(renderer, layer_geom)
            row.actor_cache = actor_cache

            total_actors = sum(len(v) for v in actor_cache.values())
            print(f"  ✅ {row.dwg_path.name}: {total_actors} actors · "
                  f"{'reprojected' if transformer else 'raw coords'}")

            meta = {
                'filename':    row.dwg_path.name,
                'full_path':   str(row.dwg_path),
                'mode':        row.display_mode,
                'layers':      row.layers,
                'transformed': transformer is not None,
                'entities':    row.entity_count,
            }
            attached.append(meta)
            self.app.dwg_actors.append({
                'filename': row.dwg_path.name,
                'actors':   [a for actors in actor_cache.values() for a in actors],
            })
            self.app.dwg_attachments.append(meta)

        prog.setValue(len(checked))
        prog.close()

        if attached:
            self._install_render_guard(renderer)
            self._reset_camera_to_dwg(renderer)
            self.dwg_attached.emit(attached)
            self._set_status(f"✅ {len(attached)} DWG file(s) attached")
            QMessageBox.information(
                self, "DWG Attached",
                f"✅ {len(attached)} DWG file(s) attached successfully.\n\n"
                f"Geometry is rendered directly in the point cloud scene\n"
                f"{'with coordinate reprojection.' if any(a['transformed'] for a in attached) else 'in original coordinates (no PRJ found).'}"
            )

    # ── Render Guard ──────────────────────────────────────

    def _install_render_guard(self, renderer):
        """Install observer that re-adds DWG actors if they are removed by LAS reload."""
        try:
            rw = renderer.GetRenderWindow()
            if rw is None:
                return
            if getattr(self, '_guard_installed', False):
                return
            self._guard_observer_id = rw.AddObserver("StartEvent", self._render_guard_callback)
            self._guard_renderer  = renderer
            self._guard_installed = True
            self._guard_last_z    = None
            print("  🔒 DWG render guard installed")
        except Exception as e:
            print(f"  ⚠️  Render guard install failed: {e}")

    def _remove_render_guard(self):
        """Remove render guard observer when no DWG overlays remain."""
        if not getattr(self, "_guard_installed", False):
            return
        try:
            renderer = getattr(self, "_guard_renderer", None)
            rw = renderer.GetRenderWindow() if renderer is not None else None
            oid = getattr(self, "_guard_observer_id", None)
            if rw is not None and oid is not None:
                rw.RemoveObserver(oid)
        except Exception:
            pass
        self._guard_installed = False
        self._guard_renderer = None
        self._guard_observer_id = None
        self._guard_last_z = None

    def _render_guard_callback(self, caller, event):
        """Before every render: re-add missing DWG actors; re-attach if cloud changed."""
        try:
            renderer = getattr(self, '_guard_renderer', None)
            if renderer is None:
                return

            # Detect new point cloud loaded (Z max changes)
            new_z = self._cloud_z_max()
            last_z = getattr(self, '_guard_last_z', None)
            if last_z is not None and abs(new_z - last_z) > 0.5:
                self._guard_last_z = new_z
                from PySide6.QtCore import QTimer
                QTimer.singleShot(200, self._reattach_after_reload)
                return
            if last_z is None:
                self._guard_last_z = new_z

            # Re-add any missing actors
            existing = set()
            col = renderer.GetActors()
            col.InitTraversal()
            a = col.GetNextActor()
            while a is not None:
                existing.add(id(a))
                a = col.GetNextActor()

            restored = 0
            for entry in getattr(self.app, 'dwg_actors', []):
                for actor in entry.get('actors', []):
                    if id(actor) not in existing:
                        renderer.AddActor(actor)
                        restored += 1
            if restored:
                print(f"  🔁 DWG guard restored {restored} actor(s)")

        except Exception as e:
            print(f"  ⚠️  Render guard error: {e}")

    def _reattach_after_reload(self):
        """Rebuild DWG actors after a new LAS is loaded (new Z range)."""
        try:
            checked = [r for r in self.items if r.chk.isChecked() and r.doc is not None]
            if not checked:
                return
            renderer = self._get_renderer()
            if renderer is None:
                return

            # Clear old DWG actors
            for entry in getattr(self.app, 'dwg_actors', []):
                for actor in entry.get('actors', []):
                    try:
                        renderer.RemoveActor(actor)
                    except Exception:
                        pass
            self.app.dwg_actors      = []
            self.app.dwg_attachments = []

            # Re-attach — raw UTM, no origin offset
            cloud_z = self._cloud_z_max()
            z_lift  = cloud_z + _Z_LIFT
            print(f"  🔄 Re-attaching DWG after reload (z_lift={z_lift:.3f}m)")

            for row in checked:
                transformer = _make_transformer(row.crs, self._project_crs)
                layer_geom  = _extract_geometry(
                    dxf_doc        = row.doc,
                    transformer    = transformer,
                    z_lift         = z_lift,
                    selected_layers= row.selected_layers,
                    color_override = row.override_color if row.override_enabled else None,
                    origin         = None,   # NO offset — raw UTM coords match point cloud
                )
                actor_cache = _geom_to_actors(renderer, layer_geom)
                row.actor_cache = actor_cache
                self.app.dwg_actors.append({
                    'filename': row.dwg_path.name,
                    'actors':   [a for acts in actor_cache.values() for a in acts],
                })
                self.app.dwg_attachments.append({
                    'filename':    row.dwg_path.name,
                    'full_path':   str(row.dwg_path),
                    'mode':        row.display_mode,
                    'layers':      row.layers,
                    'transformed': transformer is not None,
                    'entities':    row.entity_count,
                })

            self._do_render()
            total = sum(len(e['actors']) for e in self.app.dwg_actors)
            print(f"  ✅ DWG re-attached: {total} actors")

        except Exception as e:
            print(f"  ⚠️  Re-attach failed: {e}")

    def _get_renderer(self):
        app = self.app
        # Primary path used by NakshaApp
        if hasattr(app, 'vtk_widget') and app.vtk_widget:
            r = getattr(app.vtk_widget, 'renderer', None)
            if r:
                return r
            try:
                rw = app.vtk_widget.GetRenderWindow()
                if rw:
                    rens = rw.GetRenderers()
                    rens.InitTraversal()
                    return rens.GetNextItem()
            except Exception:
                pass
        # Fallback
        for attr in ('renderer', 'vtk_renderer'):
            r = getattr(app, attr, None)
            if r:
                return r
        return None

    def _reset_camera_to_dwg(self, renderer):
        try:
            renderer.ResetCamera()
            renderer.ResetCameraClippingRange()
        except Exception as e:
            print(f"⚠️  Camera reset failed: {e}")
        self._do_render()

    def _do_render(self):
        try:
            app = self.app
            if hasattr(app, 'vtk_widget') and app.vtk_widget:
                rw = app.vtk_widget.GetRenderWindow()
                if rw:
                    rw.Render()
                    return
            # Fallback pyvistaqt
            if hasattr(app, 'vtk_widget') and hasattr(app.vtk_widget, 'render'):
                app.vtk_widget.render()
        except Exception as e:
            print(f"⚠️ Render failed: {e}")

    def _cloud_z_max(self) -> float:
        """Return max Z of the loaded point cloud from app.data['xyz']."""
        try:
            data = getattr(self.app, 'data', None)
            if data is not None and hasattr(data, 'get'):
                xyz = data.get('xyz')
                if xyz is not None and len(xyz) > 0:
                    return float(np.asarray(xyz)[:, 2].max())
        except Exception:
            pass
        return 0.0

    # ── Status ────────────────────────────────────────────

    def _set_status(self, msg: str):
        self.status.setText(msg)
        QCoreApplication.processEvents()

    def closeEvent(self, event):
        """Ensure guard observers are removed when dialog closes."""
        try:
            worker = getattr(self, "_worker", None)
            if worker is not None and worker.isRunning():
                self._close_requested_while_loading = True
                worker.cancel()
                worker.wait(3000)
                if worker.isRunning():
                    print("⏳ DWG dialog close deferred until load worker stops")
                    event.ignore()
                    return
                self._worker = None
            self._remove_render_guard()
            if hasattr(self.app, "dwg_dialog") and self.app.dwg_dialog is self:
                self.app.dwg_dialog = None
        except Exception:
            pass
        super().closeEvent(event)


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC ENTRY POINT  (called from menu_sidebar_system.py)
# ─────────────────────────────────────────────────────────────────────────────

def show_dwg_attachment_dialog(app) -> MultiDWGDialog:
    """Create or restore the DWG attachment dialog (persistent)."""
    if hasattr(app, 'dwg_dialog') and app.dwg_dialog is not None:
        try:
            dlg = app.dwg_dialog
            if dlg._is_minimized_to_chip:
                dlg._do_restore_from_chip()
            else:
                if dlg.windowState() & Qt.WindowMinimized:
                    dlg.showNormal()
                dlg.show()
                dlg.raise_()
                dlg.activateWindow()
            return dlg
        except RuntimeError:
            app.dwg_dialog = None

    dlg = MultiDWGDialog(app, parent=app)
    dlg.setModal(False)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    app.dwg_dialog = dlg

    def _on_close(event):
        event.ignore()
        dlg.hide()
    dlg.closeEvent = _on_close

    return dlg
