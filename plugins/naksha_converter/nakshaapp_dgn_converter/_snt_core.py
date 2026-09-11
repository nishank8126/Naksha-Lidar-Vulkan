"""
dgn_to_snt.py
=============
DGN -> SNT binary converter  (NakshaApp overlay format v1.2)

2-D top-view output (matches DXF -> SNT)
-----------------------------------------
All line-like geometry (linestrings, shapes, complex strings/shapes, curves,
B-splines, etc.) is written as LWPOLYLINE (SNT entity 0x02), exactly matching
what the DXF -> SNT pipeline produces.  Per-vertex format is (x, y, bulge=0)
with a single shared elevation of 0.0 -- NakshaAI always displays in 2-D top
view so no Z information is lost in practice.

Usage
-----
    python dgn_to_snt.py input.dgn
    python dgn_to_snt.py input.dgn output.snt
    python dgn_to_snt.py input.dgn -d "Project description"
    python dgn_to_snt.py input.dgn --follow-refs

Requires:  dgn_reader  and  snt_core  wheels installed.
"""
from __future__ import annotations

import argparse
import ctypes
from contextlib import contextmanager
import io
import json
import logging
import math
import os
import re
import struct
import sys
import tempfile
import time
import unicodedata
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# -- logging -------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dgn_to_snt")

# -- Import dgn_reader C extension ---------------------------------------------
# The extension is installed as dgn_reader.dgn_reader (C ext lives inside the
# dgn_reader Python package that also ships OCR helpers).
# If unavailable, we fall through to the Bentley bridge (dgn_bridge) which is
# the primary engine now.
_dgn = None
try:
    import dgn_reader.dgn_reader as _dgn          # normal installed layout
except ImportError:
    try:
        import dgn_reader as _dgn                  # standalone .pyd on sys.path
    except ImportError:
        _dgn = None

# verify the extension is the right one (has .open)
if _dgn is not None and not hasattr(_dgn, "open"):
    _dgn = None

if _dgn is None:
    log.info("dgn_reader C extension not available -- using Bentley bridge only")

# -- SNT format constants (mirrors snt_core/snt_format.py exactly) -------------
SNT_MAGIC        = 0x20544E53           # "SNT " little-endian
VERSION_MAJOR    = 1
VERSION_MINOR    = 2
FLAG_HAS_INDEX   = 0x0001
FLAG_HAS_CRS     = 0x0002

HEADER_FMT       = "<IbbHQ4d6QI28s"
HEADER_SIZE      = struct.calcsize(HEADER_FMT)        # 128 bytes
HEADER_CRC_OFF   = struct.calcsize("<IbbHQ4d6Q")      # 96

LAYER_FMT        = "<IHHBBH"
LAYER_SIZE       = struct.calcsize(LAYER_FMT)         # 12

ENTITY_HDR_FMT   = "<BHBIIBI"
ENTITY_HDR_SIZE  = struct.calcsize(ENTITY_HDR_FMT)    # 17

INDEX_FMT        = "<I4fQ"
INDEX_SIZE       = struct.calcsize(INDEX_FMT)         # 28

META_SIZE_FMT    = "<I"

CREATOR     = "Sanket NakshaTech"
CREATOR_DESC= ("SNT -- NakshaApp high-performance binary CAD overlay format. "
               "Developed by Sanket Mane software team NakshaTech. "
               "Unauthorised use prohibited.")

_EMPTY_BODY = struct.pack("<I", 0)      # fallback for unmappable elements

# -- SNT EntityType codes -------------------------------------------------------
class ET:
    LINE        = 0x01
    LWPOLYLINE  = 0x02  # 2-D top-view polyline: elev(f) flags(B) count(I) [x y bulge] × N
    POLYLINE    = 0x03  # 3-D polyline (not used by this converter)
    ARC         = 0x04
    CIRCLE      = 0x05
    ELLIPSE     = 0x06
    SPLINE      = 0x07
    POINT       = 0x08
    TEXT        = 0x10
    MTEXT       = 0x11
    DIMENSION   = 0x12
    SOLID       = 0x21
    FACE3D      = 0x22
    MESH        = 0x50
    UNKNOWN     = 0xFF

# -- ColorMode codes ------------------------------------------------------------
class CM:
    BYLAYER = 0x00
    ACI     = 0x01
    RGB     = 0x02

# -- DGN color index -> ACI mapping -------------------------------------------
# DGN uses its own 256-entry RGB palette (DwgDisplayColors.tbl).
# The first 10 entries map cleanly to the well-known standard ACI colours.
# For indices > 10 the palette is a smooth colour wheel; we keep the DGN index
# as-is (many renderers treat 1-255 as valid ACI) but cap at 255.
_DGN_TO_ACI: Dict[int, int] = {
    0:  7,  # White / unset
    1:  7,  # White
    2:  1,  # Red
    3:  2,  # Yellow
    4:  3,  # Green
    5:  4,  # Cyan
    6:  5,  # Blue
    7:  6,  # Magenta
    8:  7,  # White
    9:  8,  # Dark Gray
    10: 9,  # Light Gray
}

# Visually distinct ACI cycle used when a DGN level reports color=0 (unset).
# OLE_V8 files don't expose per-level symbology, so all levels return color=0.
# Cycling Red→Green→Cyan→Blue→Magenta→Yellow ensures different levels are
# clearly distinct instead of everything rendering as white (ACI 7).
_LEVEL_COLOR_CYCLE = [1, 3, 4, 5, 6, 2]   # R, G, C, B, M, Y
_MAX_BRIDGE_TEXT_HEIGHT = 80.0
_FALLBACK_BRIDGE_TEXT_HEIGHT = 5.0
_FALLBACK_BRIDGE_BLOCK_TEXT_HEIGHT = 0.1

# -- DGN element type numbers (from g_typeNames in dgn_reader.c) ---------------
DGN_CELL_HDR        = 1
DGN_LINE            = 2
DGN_LINESTRING      = 3
DGN_GROUP_DATA      = 4
DGN_SHAPE_HOLE      = 5
DGN_SHAPE           = 6
DGN_TEXT_NODE       = 7
DGN_CURVE           = 11
DGN_CMPLX_STRING    = 12
DGN_CMPLX_SHAPE     = 14
DGN_POINT_STRING    = 15
DGN_ELLIPSE         = 16
DGN_ARC             = 17
DGN_TEXT            = 18
DGN_CONE            = 23
DGN_CELL            = 34
DGN_TAG             = 39
DGN_CMPLX_STR_HDR   = 66
DGN_CMPLX_SHP_HDR   = 67
DGN_DIMENSION       = 95
DGN_MESH            = 100
DGN_BSPLINE_CURVE   = 106

# Administrative / non-geometry types to skip entirely.
# NOTE: GroupData (type 4) is intentionally NOT listed here.
#       Real-world DGN files (e.g. Italian cadastral/topographic) store
#       drawable linestring geometry in GroupData elements.  We convert them
#       via the generic geo=="linestring" path in _to_snt_one().
_SKIP_TYPES = {
    1,   # CellHeader        (container -- geometry is in children)
    9,   # DesignFileHdr
    10,  # LevelSymb
    21,  # BSplinePole
    22,  # PointStringHeader
    26,  # BSplineKnot
    27,  # BSplineWeight
    35,  # LineStyleDef
    36,  # SharedCellDef     (definition, not instance)
    38,  # TagSetDef
    94,  # RasterReserved    (raster placeholder, no drawable geometry)
    98,  # AttributeLinkage
    99,  # RasterFrame
}

# Types that produce closed polylines
_CLOSED_TYPES = {DGN_SHAPE, DGN_SHAPE_HOLE, DGN_CMPLX_SHAPE, DGN_CMPLX_SHP_HDR}


# =============================================================================
#  String Pool
# =============================================================================
class _StringPool:
    """Deduplicated UTF-8 pool. Index 0 is always the empty string."""

    def __init__(self) -> None:
        self._idx: Dict[str, int] = {"": 0}
        self._ent: List[bytes]    = [b""]

    def get(self, s: str) -> int:
        idx = self._idx.get(s)
        if idx is not None:
            return idx
        enc = s.encode("utf-8")[:65535]
        idx = len(self._ent)
        self._ent.append(enc)
        self._idx[s] = idx
        return idx

    def serialize(self) -> bytes:
        parts = [struct.pack("<I", len(self._ent))]
        for enc in self._ent:
            parts.append(struct.pack("<H", len(enc)))
            parts.append(enc)
        return b"".join(parts)

    def __len__(self) -> int:
        return len(self._ent)


# =============================================================================
#  Layer record
# =============================================================================
@dataclass
class _Layer:
    name_idx:     int
    color_aci:    int
    linetype_idx: int
    flags:        int = 0

    def pack(self) -> bytes:
        return struct.pack(
            LAYER_FMT,
            self.name_idx,
            self.color_aci & 0xFFFF,
            self.linetype_idx & 0xFFFF,
            self.flags & 0xFF,
            0,   # reserved
            0,   # pad uint16
        )


# =============================================================================
#  Float helpers
# =============================================================================
_F32_MAX   = 3.3e38    # just below struct 'f' overflow limit
_COORD_MIN = 1e-10     # absolute minimum meaningful coordinate (filters denormals)

def _f32(val: float) -> float:
    """Clamp a float64 to the safe float32 range -- avoids struct pack overflow."""
    if val != val:          # NaN
        return 0.0
    if val > _F32_MAX:
        return _F32_MAX
    if val < -_F32_MAX:
        return -_F32_MAX
    return val


def _valid_vertex(v) -> bool:
    """
    Return True if vertex (x, y, z) contains real coordinate data.
    Filters out:
      - NaN / inf
      - Denormal floats (extremely small non-zero values like 4.78e-303)
        which indicate the DGN parser read uninitialised/garbage memory.
    """
    for coord in v:
        f = float(coord)
        if not math.isfinite(f):
            return False
        absf = abs(f)
        # Denormal: non-zero but absurdly small (< 1e-10)
        if 0.0 < absf < _COORD_MIN:
            return False
    return True


# =============================================================================
#  Ring splitter  (for DGN ComplexShape merged vertices)
# =============================================================================

_RING_CLOSE_EPS = 0.01   # 1 cm in projected coordinates — detects ring closures

def _split_rings(verts) -> List[List]:
    """
    Split a concatenated DGN ComplexShape vertex list back into individual
    closed rings.

    DGN ComplexShape merges all component shapes into one flat vertex list.
    Each component shape was decoded with closeShape=True, so its last vertex
    is a copy of its first vertex.  We detect ring boundaries by finding
    where vertex[i] is within _RING_CLOSE_EPS of vertex[ring_start].

    Returns a list of vertex sublists (one per ring).  Any trailing single
    vertex (the extra close added by build_complex_cached_vertices for the
    whole shape) is silently discarded.

    GUARD against false-positive splits:
    After splitting, if the "trailing" unsplit segment is larger than ALL the
    detected rings combined, we have almost certainly mis-split the shape (a
    self-touching polygon whose interior vertex coincides with the start).
    In that case we fall back to returning the whole vertex list as-is so
    NakshaAI draws the correct outline rather than 30+ spurious tiny rings.
    """
    if len(verts) < 3:
        return [verts] if verts else []

    rings: List[List] = []
    ring_start = 0
    i = ring_start + 2          # minimum 3 vertices for a valid ring

    while i < len(verts):
        v0 = verts[ring_start]
        vi = verts[i]
        dx = float(vi[0]) - float(v0[0])
        dy = float(vi[1]) - float(v0[1])
        if dx * dx + dy * dy <= _RING_CLOSE_EPS * _RING_CLOSE_EPS:
            # Ring closes at position i  →  [ring_start … i] inclusive
            ring = verts[ring_start : i + 1]
            if len(ring) >= 3:
                rings.append(ring)
            ring_start = i + 1
            i = ring_start + 2  # reset scan for next ring
        else:
            i += 1

    # Remaining vertices that didn't close (or last unclosed path)
    trailing: List = []
    if ring_start < len(verts):
        trailing = verts[ring_start:]
        if len(trailing) >= 2:   # single dangling vertex → discard
            rings.append(trailing)

    # False-positive guard: if the trailing segment is bigger than ALL
    # detected rings combined, the splits are bogus (self-touching polygon
    # whose interior vertex coincidentally equals the start vertex).
    # Fall back to the original full vertex list.
    if rings and trailing:
        ring_verts_total = sum(len(r) for r in rings) - len(trailing)
        if len(trailing) > ring_verts_total:
            return [verts]

    return rings if rings else [verts]


# =============================================================================
#  Body packers  (DGN element dict -> bytes)
# =============================================================================

def _pack_line(el: dict) -> Optional[bytes]:
    """LINE body: sx sy sz ex ey ez  (6xf32 = 24 bytes)"""
    s = el.get("start")
    e = el.get("end")
    if s and e:
        return struct.pack("<6f",
            _f32(float(s[0])), _f32(float(s[1])), _f32(float(s[2])),
            _f32(float(e[0])), _f32(float(e[1])), _f32(float(e[2])))
    verts = el.get("vertices")
    if verts and len(verts) >= 2:
        s, e = verts[0], verts[-1]
        return struct.pack("<6f",
            _f32(float(s[0])), _f32(float(s[1])), _f32(float(s[2])),
            _f32(float(e[0])), _f32(float(e[1])), _f32(float(e[2])))
    return None


def _pack_lwpolyline(el: dict, closed: bool = False) -> Optional[bytes]:
    """
    LWPOLYLINE body (SNT 0x02):
        elev(f)  flags(B)  count(I)  [x(f)  y(f)  bulge(f)] × N

    Matches the DXF->SNT pipeline exactly.  NakshaAI always renders in 2-D
    top view so only (x, y) matter; elevation=0 and bulge=0 throughout.
    """
    verts = el.get("vertices")
    if not verts:
        return None
    return _pack_lwpolyline_verts(verts, closed=closed)


def _pack_lwpolyline_verts(verts, closed: bool = False) -> Optional[bytes]:
    """
    LWPOLYLINE body from a raw vertex list.
    Used by the ring-splitter path for ComplexShape.
    """
    clean = [v for v in verts if _valid_vertex(v)]
    if not clean:
        return None
    flags = 0x01 if closed else 0x00
    buf   = io.BytesIO()
    buf.write(struct.pack("<fBI", 0.0, flags, len(clean)))   # elev=0, flags, count
    for v in clean:
        buf.write(struct.pack("<3f", _f32(float(v[0])), _f32(float(v[1])), 0.0))  # x, y, bulge=0
    return buf.getvalue()


def _pack_arc(el: dict) -> Optional[bytes]:
    """
    ARC body: cx cy cz  r  start_angle  end_angle  (6xf32 = 24 bytes)
    DGN gives start_angle + sweep_angle (degrees).
    SNT stores start_angle and end_angle = start + sweep.
    """
    cx = el.get("center")
    r  = el.get("radius")
    sa = el.get("start_angle")
    sw = el.get("sweep_angle")
    if None in (cx, r, sa, sw):
        return None
    return struct.pack(
        "<6f",
        _f32(float(cx[0])), _f32(float(cx[1])), _f32(float(cx[2])),
        _f32(float(r)),
        _f32(float(sa)),
        _f32(float(sa) + float(sw)),
    )


def _pack_circle(cx: tuple, r: float) -> bytes:
    """CIRCLE body: cx cy cz  r  (4xf32 = 16 bytes)"""
    return struct.pack("<4f",
        _f32(float(cx[0])), _f32(float(cx[1])), _f32(float(cx[2])), _f32(float(r)))


def _pack_ellipse(el: dict) -> Optional[bytes]:
    """
    ELLIPSE body: cx cy cz  maj_x maj_y maj_z  ratio  sp ep  (9xf32 = 36 bytes)
    DGN gives scalar primary_axis and secondary_axis lengths.
    We orient the major axis along X, ratio = secondary / primary.
    """
    cx = el.get("center")
    pa = el.get("primary_axis", 0.0) or 0.0
    sa = el.get("secondary_axis", 0.0) or 0.0
    if cx is None or pa == 0.0:
        return None
    return struct.pack(
        "<9f",
        _f32(float(cx[0])), _f32(float(cx[1])), _f32(float(cx[2])),
        _f32(float(pa)), 0.0, 0.0,      # major axis vector along X
        _f32(float(sa) / float(pa)),    # ratio
        0.0, float(math.tau),           # full ellipse (0 -> 2pi)
    )


def _pack_text(el: dict, pool: "_StringPool") -> Optional[bytes]:
    """TEXT body: ix iy iz  height  rotation  str_idx  style_idx  (5xf32 + 2xu32 = 28 bytes)"""
    origin = el.get("origin")
    if origin is None:
        return None
    text   = str(el.get("text", "") or "")
    height = float(el.get("height", 1.0) or 1.0)
    rot    = float(el.get("rotation", 0.0) or 0.0)
    return struct.pack(
        "<5fII",
        _f32(float(origin[0])), _f32(float(origin[1])), _f32(float(origin[2])),
        _f32(height), _f32(rot),
        pool.get(text),
        pool.get("STANDARD"),
    )


def _pack_point_body(el: dict) -> Optional[bytes]:
    """POINT body: x y z  (3xf32 = 12 bytes)"""
    pt = (el.get("origin")
          or (el.get("vertices") or [None])[0]
          or el.get("base_center"))
    if pt is None:
        return None
    return struct.pack("<3f", _f32(float(pt[0])), _f32(float(pt[1])), _f32(float(pt[2])))


def _pack_dim(el: dict, pool: "_StringPool") -> Optional[bytes]:
    """DIMENSION -- text body using dimension_text / embedded_text."""
    text   = el.get("dimension_text") or el.get("embedded_text") or ""
    origin = el.get("embedded_origin")
    if origin is None:
        verts = el.get("vertices")
        origin = verts[0] if verts else None
    if origin is None:
        return None
    return struct.pack(
        "<5fII",
        float(origin[0]), float(origin[1]), float(origin[2]),
        1.0, 0.0,
        pool.get(str(text)),
        pool.get("STANDARD"),
    )


# =============================================================================
#  Dispatch: one DGN element dict -> list of (snt_type_code, body_bytes)
#
#  Returns a LIST because some DGN element types (e.g. Cone with two radii)
#  naturally map to more than one SNT entity.  Most elements return a list
#  of length 1.
# =============================================================================

def _to_snt_one(el: dict, pool: "_StringPool", layer_name: str) -> Tuple[int, bytes]:
    """
    Inner dispatcher -- maps a single DGN element to ONE (type, body) pair.
    Called by _to_snt() for all types except Cone and PointString.
    """
    dgn_type = int(el.get("type", 0))
    geo      = el.get("geometry_type", "")

    # -- LINE ------------------------------------------------------------------
    if dgn_type == DGN_LINE or geo == "line":
        body = _pack_line(el)
        if body:
            return ET.LINE, body

    # -- ARC -------------------------------------------------------------------
    elif dgn_type == DGN_ARC or geo == "arc":
        # Full-circle arcs (sweep = 2π) → emit as CIRCLE entities.
        sw = el.get("sweep_angle")
        if sw is not None:
            try:
                if abs(float(sw) - math.tau) < 0.001:
                    cx = el.get("center")
                    r  = el.get("radius")
                    if cx and r:
                        return ET.CIRCLE, _pack_circle(cx, float(r))
            except (TypeError, ValueError):
                pass
        body = _pack_arc(el)
        if body:
            return ET.ARC, body
        # fall back: sampled arc vertices -> LWPOLYLINE
        if el.get("vertices"):
            body = _pack_lwpolyline(el, closed=False)
            if body:
                return ET.LWPOLYLINE, body

    # -- ELLIPSE ---------------------------------------------------------------
    elif dgn_type == DGN_ELLIPSE or geo == "ellipse":
        pa = float(el.get("primary_axis") or 0.0)
        sa = float(el.get("secondary_axis") or 0.0)
        # Full circle when axes are equal
        if pa > 0.0 and abs(pa - sa) / pa < 0.001:
            cx = el.get("center")
            if cx:
                return ET.CIRCLE, _pack_circle(cx, pa)
        body = _pack_ellipse(el)
        if body:
            return ET.ELLIPSE, body
        # fall back: sampled vertices
        if el.get("vertices"):
            body = _pack_lwpolyline(el, closed=True)
            if body:
                return ET.LWPOLYLINE, body

    # -- TEXT / TEXT NODE ------------------------------------------------------
    elif dgn_type in (DGN_TEXT, DGN_TEXT_NODE) or geo == "text":
        body = _pack_text(el, pool)
        if body:
            return ET.TEXT, body

    # -- DIMENSION -------------------------------------------------------------
    elif dgn_type == DGN_DIMENSION:
        body = _pack_dim(el, pool)
        if body:
            return ET.DIMENSION, body

    # -- All linestring / shape / complex string types -> LWPOLYLINE ----------
    #    Use LWPOLYLINE (SNT 0x02, 2-D top view) to match DXF->SNT exactly.
    #    NakshaAI renders everything in 2-D top view -- only (x, y) are used.
    #
    #    NOTE: DGN_POINT_STRING → handled in _to_snt() (one POINT per vertex)
    #    NOTE: DGN_CMPLX_SHAPE / DGN_CMPLX_SHP_HDR → handled in _to_snt()
    #           (ring-split: one LWPOLYLINE per sub-shape ring)
    elif dgn_type in (DGN_LINESTRING, DGN_SHAPE, DGN_SHAPE_HOLE, DGN_CURVE,
                      DGN_CMPLX_STRING, DGN_CMPLX_STR_HDR) or geo == "linestring":
        closed = (dgn_type in _CLOSED_TYPES)
        body   = _pack_lwpolyline(el, closed=closed)
        if body:
            return ET.LWPOLYLINE, body

    # -- B-spline curve -> approximate as LWPOLYLINE --------------------------
    elif dgn_type == DGN_BSPLINE_CURVE or geo == "bspline_curve":
        body = _pack_lwpolyline(el, closed=False)
        if body:
            return ET.LWPOLYLINE, body

    # -- Mesh ------------------------------------------------------------------
    elif dgn_type == DGN_MESH or geo == "mesh":
        body = _pack_lwpolyline(el, closed=False)
        if body:
            return ET.LWPOLYLINE, body

    # -- Generic fallback: any element with >=2 vertices -> LWPOLYLINE --------
    verts = el.get("vertices")
    if verts and len(verts) >= 2:
        body = _pack_lwpolyline(el, closed=False)
        if body:
            return ET.LWPOLYLINE, body

    # -- Single-point fallback ------------------------------------------------
    if verts and len(verts) == 1:
        body = _pack_point_body(el)
        if body:
            return ET.POINT, body

    return ET.UNKNOWN, _EMPTY_BODY


def _to_snt(el: dict, pool: "_StringPool", layer_name: str) -> List[Tuple[int, bytes]]:
    """
    Map a single DGN element to a LIST of (snt_type, body) tuples.

    Most elements produce exactly one item.  Special cases:

    CONE (type 23):
        DGN Cone represents a truncated cone / annular ring viewed in plan.
        It carries base_center+base_radius (outer circle) and
        apex_point+apex_radius (inner circle).  We emit up to 2 CIRCLE
        entities so both rings are visible in NakshaAI.

    PointString (type 15):
        DGN PointString is a series of isolated survey/control points with no
        connecting lines.  Each vertex becomes a separate POINT entity.
    """
    dgn_type = int(el.get("type", 0))
    geo      = el.get("geometry_type", "")

    # -- CONE: SKIP entirely ------------------------------------------------
    #
    # DGN Cone is a 3-D truncated cone.  MicroStation does NOT export it
    # to DXF in 2-D mode.  Global Mapper doesn't render it either.
    # Previously we converted to POINT (dot) but that clutters the map
    # with unnecessary markers that don't exist in the original view.
    if dgn_type == DGN_CONE or geo == "cone":
        return [(ET.UNKNOWN, _EMPTY_BODY)]

    # -- PointString: one POINT entity per vertex ----------------------------
    #    Trajectory points are rendered as points (batched into a single VTK actor
    #    per color/layer group for performance). Using CIRCLE would create 37-vertex
    #    polylines per point which is extremely expensive for thousands of points.
    if dgn_type == DGN_POINT_STRING:
        verts = el.get("vertices", [])
        clean = [v for v in verts if _valid_vertex(v)]
        if clean:
            return [(ET.POINT,
                     struct.pack("<3f",
                                 _f32(float(v[0])),
                                 _f32(float(v[1])),
                                 _f32(float(v[2]) if len(v) > 2 else 0.0)))
                    for v in clean]
        return [(ET.UNKNOWN, _EMPTY_BODY)]

    # -- ComplexShape / ComplexShapeHeader: split concatenated rings ----------
    #
    # The DGN C reader merges all component sub-shapes into one flat vertex
    # list (build_complex_cached_vertices, closeShape=True).  Each component
    # shape closes on itself (last vertex == first vertex of that ring).
    #
    # Drawing the whole list as ONE LWPOLYLINE creates diagonal "jump" lines
    # between the end of one ring and the start of the next — exactly the
    # spurious lines visible in NakshaAI.
    #
    # Fix: detect ring boundaries (vertex[i] ≈ vertex[ring_start]) and emit
    # one LWPOLYLINE per ring.  This matches what MicroStation's DGN→DXF
    # export does (each component → separate DXF LWPOLYLINE).
    if dgn_type in (DGN_CMPLX_SHAPE, DGN_CMPLX_SHP_HDR):
        verts = el.get("vertices") or []
        if not verts:
            return [(ET.UNKNOWN, _EMPTY_BODY)]
        rings = _split_rings(verts)
        result: List[Tuple[int, bytes]] = []
        for ring in rings:
            body = _pack_lwpolyline_verts(ring, closed=True)
            if body:
                result.append((ET.LWPOLYLINE, body))
        return result if result else [(ET.UNKNOWN, _EMPTY_BODY)]

    # -- All other types: single entity ---------------------------------------
    return [_to_snt_one(el, pool, layer_name)]


# =============================================================================
#  Bounding box helper
# =============================================================================

def _bbox(el: dict) -> Optional[Tuple[float, float, float, float]]:
    """
    2-D AABB (xmin, ymin, xmax, ymax) from the element dict, or None if no
    valid geometry can be found.

    Returning None (instead of (0,0,0,0)) is critical: callers must NOT use
    None to update the file-level global bbox, so the viewport in NakshaAI
    is not anchored to the origin when actual data is far from (0,0).
    """
    # --- 1. Try geometry-derived vertices ------------------------------------
    verts = el.get("vertices")
    if verts:
        clean = [v for v in verts if _valid_vertex(v)]
        if clean:
            xs = [float(v[0]) for v in clean]
            ys = [float(v[1]) for v in clean]
            box = (min(xs), min(ys), max(xs), max(ys))
            if any(value != 0.0 for value in box):
                return box

    # --- 2. Point-like elements ----------------------------------------------
    for key in ("origin", "start", "center", "base_center"):
        pt = el.get(key)
        if pt and _valid_vertex(pt):
            x, y = float(pt[0]), float(pt[1])
            if x != 0.0 or y != 0.0:
                return (x, y, x, y)

    # --- 3. Arc: center +/- radius ------------------------------------------
    if el.get("geometry_type") == "arc":
        cx = el.get("center")
        r  = float(el.get("radius") or 0.0)
        if cx and _valid_vertex(cx) and r > 0.0:
            x, y = float(cx[0]), float(cx[1])
            return (x - r, y - r, x + r, y + r)

    # --- 4. Cone: use the outer (base) circle --------------------------------
    if el.get("geometry_type") == "cone" or int(el.get("type", 0)) == DGN_CONE:
        bc = el.get("base_center")
        br = float(el.get("base_radius") or 0.0)
        ap = el.get("apex_point")
        ar = float(el.get("apex_radius") or 0.0)
        xs, ys = [], []
        if bc and _valid_vertex(bc) and br > 0.0:
            bx, by = float(bc[0]), float(bc[1])
            xs.extend([bx - br, bx + br])
            ys.extend([by - br, by + br])
        if ap and _valid_vertex(ap) and ar > 0.0:
            ax, ay = float(ap[0]), float(ap[1])
            xs.extend([ax - ar, ax + ar])
            ys.extend([ay - ar, ay + ar])
        if xs and ys:
            return (min(xs), min(ys), max(xs), max(ys))

    # --- 5. Fall back to DGN range only if it looks sane --------------------
    rng = el.get("range")
    if rng:
        lo = rng.get("low",  (0.0, 0.0, 0.0))
        hi = rng.get("high", (0.0, 0.0, 0.0))
        lx, ly = float(lo[0]), float(lo[1])
        hx, hy = float(hi[0]), float(hi[1])
        _BIG = 1e10
        if (abs(lx) < _BIG and abs(ly) < _BIG
                and abs(hx) < _BIG and abs(hy) < _BIG
                and hx >= lx and hy >= ly
                and (lx != 0.0 or ly != 0.0 or hx != 0.0 or hy != 0.0)):
            return (lx, ly, hx, hy)

    # No valid geometry found -- caller must not use this for global bbox
    return None


# =============================================================================
#  UTM hemisphere helper
# =============================================================================

def _hemisphere(ymin: float, ymax: float) -> Tuple[str, Optional[float]]:
    mid = (ymin + ymax) / 2.0
    if ymin >= 10_000_000:
        return "south", round((mid - 10_000_000) / 110_540.0, 4)
    if ymax < 10_000_000:
        return "north", round(mid / 110_540.0, 4)
    return "ambiguous", round(mid / 110_540.0, 4)


# =============================================================================
#  Result dataclass
# =============================================================================

@dataclass(frozen=True)
class ConvertResult:
    snt_path:          Path
    report_path:       Path
    entity_count:      int
    layer_count:       int
    string_pool_size:  int
    source_size_bytes: int
    output_size_bytes: int
    bbox:              Tuple[float, float, float, float]
    type_counts:       Dict[str, int]
    layer_names:       List[str]
    warnings:          List[str]
    is_3d:             bool


# =============================================================================
#  Bentley Bridge (32-bit dgnfileio.dll)
# =============================================================================

# Holds the real diagnostics (exit code + stderr) from the last bridge run so the
# failure surfaced to the UI names the actual missing DLL instead of a generic guess.
_LAST_BRIDGE_DIAG = ""
_SEM_FAILCRITICALERRORS = 0x0001
_SEM_NOGPFAULTERRORBOX = 0x0002
_SEM_NOOPENFILEERRORBOX = 0x8000


def _bridge_exe_candidates() -> List[Path]:
    """Return every supported bridge location for source, wheel, and PyInstaller."""
    here = Path(__file__).resolve().parent

    # A manually installed NakshaAI plugin must prefer its private bridge over
    # anything bundled with the host executable. The package-local bridge is
    # the converter build that preserves DGN text height in UOR units; selecting
    # another host bridge can make grid labels effectively invisible.
    roots = [here]
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            roots.append(Path(meipass))
        roots.append(Path(sys.executable).resolve().parent)

    roots.append(here.parent)

    candidates: List[Path] = []
    for root in roots:
        candidates.extend(
            [
                root / "dgn_bridge" / "bin" / "read_dgn.exe",
                root / "nakshaapp_dgn_converter" / "bin" / "read_dgn.exe",
                root / "bin" / "read_dgn.exe",
            ]
        )

    unique: List[Path] = []
    seen = set()
    for path in candidates:
        key = str(path).lower()
        if key not in seen:
            unique.append(path)
            seen.add(key)
    return unique


def _find_bridge_exe() -> Optional[Path]:
    for path in _bridge_exe_candidates():
        if path.exists():
            return path
    return None


def _bridge_lib_for(bridge_exe: Path) -> Path:
    if bridge_exe.parent.name.lower() == "bin":
        return bridge_exe.parent.parent / "lib"
    return bridge_exe.parent / "lib"


@contextmanager
def _bridge_run_lock(timeout: float = 300.0):
    """
    Serialize bridge launches across Python processes.

    The Bentley DLL stack behind read_dgn.exe is stable for sequential use in
    this workspace, but overlapping launches can trigger access violations or
    transient file-write failures. We therefore take a short-lived OS file lock
    before spawning the bridge process.
    """
    lock_path = Path(tempfile.gettempdir()) / "dgn_bridge.lock"
    lock_file = open(lock_path, "a+b")

    try:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"0")
                lock_file.flush()

            deadline = time.monotonic() + timeout
            while True:
                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Timed out waiting for dgn_bridge lock")
                    time.sleep(0.1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(str(os.getpid()).encode("ascii", errors="ignore"))
        lock_file.flush()
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


@contextmanager
def _suppress_windows_error_dialogs():
    """
    Prevent Windows from showing child-process crash popups while we probe the
    standalone bridge. This keeps bridge failures recoverable so we can fall
    back to dgn_reader cleanly.
    """
    if os.name != "nt":
        yield
        return

    mode = (
        _SEM_FAILCRITICALERRORS
        | _SEM_NOGPFAULTERRORBOX
        | _SEM_NOOPENFILEERRORBOX
    )
    kernel32 = ctypes.windll.kernel32
    old_mode = kernel32.SetErrorMode(mode)
    try:
        yield
    finally:
        kernel32.SetErrorMode(old_mode)


_STANDARD_UOR_SCALES = [1.0, 10.0, 100.0, 1000.0, 10000.0, 100000.0, 1000000.0]


def score_scale_candidate(scale: float, samples: list) -> float:
    """Score how plausible a UOR scale is given raw coordinate samples.

    Projection-neutral: accepts any coordinate system where scaled values
    fall between 0.1 (sub-millimetre engineering) and 1e9 (continental grid).
    No regional bonuses — the SDK-reported value is the authoritative source;
    this function only distinguishes 'plausible' from 'clearly wrong'.
    """
    score = 0.0
    huge_raw = max((abs(v) for v in samples), default=0.0) >= 1e8
    for i in range(0, len(samples) - 1, 2):
        x = abs(samples[i] / scale)
        y = abs(samples[i+1] / scale)
        for coord in (x, y):
            if 0.1 <= coord <= 1e9:
                score += 1.0
            else:
                score -= 6.0

        lo, hi = sorted((x, y))
        if 1e5 <= lo <= 9e5 and 1e6 <= hi <= 1e7:
            score += 6.0
        elif 2.5e4 <= lo <= 2e6 and 2.5e5 <= hi <= 2e7:
            score += 1.5

        if huge_raw and hi < 1e5:
            score -= 1.0
        if huge_raw and hi > 2e7:
            score -= 1.0
    return score

def detect_uor_scale(samples: list, reported_scale: Optional[float] = None) -> float:
    if len(samples) < 4:
        return reported_scale if reported_scale is not None and reported_scale > 0 else 1.0

    candidate_scales = list(_STANDARD_UOR_SCALES)
    if reported_scale is not None and reported_scale > 0 and reported_scale not in candidate_scales:
        candidate_scales.append(float(reported_scale))

    best_scale = reported_scale if reported_scale is not None and reported_scale > 0 else 1.0
    best_score = float("-inf")

    for scale in candidate_scales:
        score = score_scale_candidate(scale, samples)
        if reported_scale is not None and reported_scale > 0 and math.isclose(scale, reported_scale):
            score += 2.0
        if score > best_score:
            best_score = score
            best_scale = scale

    return best_scale


def _normalise_block_identifier(value: str) -> str:
    """Return a filename-independent Unicode key for a PRJ/SNT block name."""
    key = unicodedata.normalize("NFKC", str(value or "")).strip().strip("\"'")
    key = key.replace("\\", "/").rsplit("/", 1)[-1].strip()
    key = re.sub(r"(?i)\.(?:las|laz)\s*$", "", key)
    return key.casefold()


def _read_companion_prj_centers(dgn_path: Path) -> Dict[str, List[Tuple[float, float]]]:
    """Read block centres from every adjacent TerraScan PRJ, losslessly."""
    result: Dict[str, List[Tuple[float, float]]] = {}
    coord_re = re.compile(
        r"^\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)"
        r"\s+([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*$"
    )
    for prj_path in sorted(dgn_path.parent.glob("*.prj")):
        current_name = ""
        points: List[Tuple[float, float]] = []

        def store_current() -> None:
            if not current_name or not points:
                return
            key = _normalise_block_identifier(current_name)
            if not key:
                return
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            center = (0.5 * (min(xs) + max(xs)), 0.5 * (min(ys) + max(ys)))
            result.setdefault(key, []).append(center)

        try:
            raw = prj_path.read_bytes()
            if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
                text = raw.decode("utf-16")
            else:
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    text = raw.decode("cp1252")
            for line in text.splitlines():
                block_match = re.match(
                    r"^\s*Block\s+(.+?)\s*$", line, re.IGNORECASE
                )
                if block_match:
                    store_current()
                    current_name = block_match.group(1)
                    points = []
                    continue
                if not current_name:
                    continue
                coord_match = coord_re.match(line)
                if coord_match:
                    points.append((
                        float(coord_match.group(1)),
                        float(coord_match.group(2)),
                    ))
            store_current()
        except OSError as exc:
            log.warning("Could not read companion PRJ %s: %s", prj_path.name, exc)
    return result


def detect_scale_from_prj_blocks(
    elements: list,
    model_idx: int,
    dgn_path: Path,
) -> Optional[float]:
    """Return a PRJ-authoritative UOR scale when named blocks correspond."""
    block_centers = _read_companion_prj_centers(Path(dgn_path))
    if not block_centers:
        return None

    votes: Dict[float, int] = {}
    matched_labels = set()
    for element in elements:
        if element.get("model_idx") != model_idx or element.get("type") != DGN_TEXT:
            continue
        origin = element.get("origin")
        key = _normalise_block_identifier(element.get("text", ""))
        if not origin or not key or key not in block_centers:
            continue
        ox, oy = float(origin[0]), float(origin[1])
        for center_x, center_y in block_centers[key]:
            if abs(center_x) < 1e-12 or abs(center_y) < 1e-12:
                continue
            for candidate in _STANDARD_UOR_SCALES:
                x_ratio = abs(ox / center_x)
                y_ratio = abs(oy / center_y)
                if (
                    abs(x_ratio - candidate) / candidate < 0.02
                    and abs(y_ratio - candidate) / candidate < 0.02
                ):
                    votes[candidate] = votes.get(candidate, 0) + 1
                    matched_labels.add(key)

    if not votes:
        return None
    scale, _support = max(votes.items(), key=lambda item: item[1])
    log.info(
        "PRJ authority selected scale %.12g from %d matching block label(s)",
        scale,
        len(matched_labels),
    )
    return scale

def detect_scale_from_grid_labels(elements: list, model_idx: int, current_scale: float) -> float:
    """Use projected-coordinate text labels to validate the model UOR scale.

    Besides standalone ordinates, survey drawings often put the coordinate
    pair in a tile name such as ``51000_216000``. Such labels can correct a
    bridge-reported scale when the DGN uses customised working units.
    """
    votes: Dict[float, int] = {}

    def vote(raw_coord: float, labelled_coord: float) -> None:
        if abs(labelled_coord) < 10000.0:
            return
        ratio = abs(raw_coord / labelled_coord)
        for candidate in _STANDARD_UOR_SCALES:
            if abs(ratio - candidate) / candidate < 0.02:
                votes[candidate] = votes.get(candidate, 0) + 1
    for el in elements:
        if el.get('model_idx') != model_idx or el.get('type') != 18:  # Text type
            continue
        text = str(el.get('text', '') or '').strip()
        origin = el.get('origin')
        if not text or not origin:
            continue

        ox, oy = origin[0], origin[1]
        if re.fullmatch(r"\d{6,7}", text):
            val = float(text)
            if val >= 100000.0:
                vote(ox, val)
                vote(oy, val)
            continue

        # Extract every separated numeric field: project prefixes are often
        # numeric too (for example 8114_587500_5917000). Accept a scale clue
        # only when *both* insertion coordinates agree with the same scale.
        # This rejects accidental one-axis matches involving project IDs.
        fields = [
            float(match.group(0))
            for match in re.finditer(r"(?<!\d)\d{4,8}(?!\d)", text)
            if float(match.group(0)) >= 10000.0
        ]
        for first_idx, first in enumerate(fields):
            for second in fields[first_idx + 1:]:
                for x_label, y_label in ((first, second), (second, first)):
                    for candidate in _STANDARD_UOR_SCALES:
                        x_ratio = abs(ox / x_label)
                        y_ratio = abs(oy / y_label)
                        if (
                            abs(x_ratio - candidate) / candidate < 0.02
                            and abs(y_ratio - candidate) / candidate < 0.02
                        ):
                            votes[candidate] = votes.get(candidate, 0) + 2

    if not votes:
        return current_scale

    candidate, support = max(votes.items(), key=lambda item: (item[1], -abs(item[0] - current_scale)))
    if support >= 2:
        log.info(
            "Grid text label clues agreed on scale %.12g (%d matches). Overriding %.12g",
            candidate, support, current_scale,
        )
        return candidate

    return current_scale


def _scale_bridge_text_height(
    height: float,
    scale: float,
    layer_name: str = "",
) -> float:
    """Convert Bentley UOR text height to a plausible SNT world height.

    Some Bentley SDK text records report corrupt text heights (for example
    500,000,000 UOR at 10,000 UOR/m = 50,000 m). Clamping that value to the
    former 80 m ceiling still produced enormous glyph geometry and made a
    later 1 pt label edit remain visibly oversized. The desktop converter's
    established fallback for such invalid records is 5 world units. Preserve
    every valid source height and use that fallback only beyond the plausibility
    ceiling.
    """
    if not math.isfinite(height) or height <= 0.0:
        return 1.0
    if not math.isfinite(scale) or scale <= 0.0:
        scale = 1.0
    world_height = height / scale
    if not math.isfinite(world_height) or world_height <= 0.0:
        return 1.0
    if world_height > _MAX_BRIDGE_TEXT_HEIGHT:
        normalized_layer = "".join(
            ch for ch in str(layer_name or "").upper() if ch.isalnum()
        )
        if normalized_layer in {
            "BL", "BLOCKS", "BLOCKLABEL", "BLOCKLABELS",
            "FILENAMES", "FEATUREATTRIBS",
        }:
            return _FALLBACK_BRIDGE_BLOCK_TEXT_HEIGHT
        return _FALLBACK_BRIDGE_TEXT_HEIGHT
    return world_height


def _parse_dgn_nm_levels(dgn_path: Path) -> dict:
    """Parse level names from DGN V8 OLE Dgn^Nm/$1 and Dgn^Nm/$2 streams.

    Returns {level_id_or_code: level_name} using only stdlib (struct, zlib) + olefile.
    On any failure returns an empty dict — never raises.
    """
    DGN_NM_NAME_MARKER = 0x0001FEFF
    try:
        import struct as _st
        import zlib as _zl
        import olefile as _ole

        def _decompress(raw: bytes):
            for skip in (0, 2, 4, 8, 12, 16, 20, 24):
                try:
                    return _zl.decompress(raw[skip:])
                except Exception:
                    pass
            for skip in (0, 2, 4, 8):
                try:
                    return _zl.decompress(raw[skip:], -15)
                except Exception:
                    pass
            return None

        with open(str(dgn_path), "rb") as fh:
            magic = fh.read(8)
        if magic != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            return {}  # not OLE V8

        ole = _ole.OleFileIO(str(dgn_path))
        result: dict = {}

        # 1. Parse Dgn^Nm/$1 (standard levels)
        #
        # Two known binary formats within the $1 stream:
        #
        # Format B (late-block / V8 native — found in DGN files with level table):
        #   Sentinel 0xFFFFFFFF at pos - 0xD8, levelCode at pos - 0xDC.
        #   Immediately precedes the name marker; 32 bytes before BOM are zeros.
        #
        # Format A (early-block / DXF-import style — fallback for files with no Format B):
        #   Magic 0x4279XXXX at pos - 0x20, levelCode at pos - 0x1C.
        #   nameMarker 0x56D2100B or 0x56D2100F at pos - 0x0C.
        #
        # Strategy: prefer Format B; use Format A only when B yields nothing.
        # Later entries override earlier ones (last-wins) — MicroStation precedence.
        if ole.exists(["Dgn^Nm", "$1"]):
            nm_raw = ole.openstream(["Dgn^Nm", "$1"]).read()
            nm_data = _decompress(nm_raw)
            if nm_data is not None:
                r32 = lambda d, o: _st.unpack_from("<I", d, o)[0]  # noqa: E731

                # -- Pass 1: Format B (sentinel = 0xFFFFFFFF) ------------------
                fmt_b: dict = {}
                for pos in range(0xDC, len(nm_data) - 8):
                    if r32(nm_data, pos) != DGN_NM_NAME_MARKER:
                        continue
                    name_storage = r32(nm_data, pos - 4)
                    if name_storage == 0 or name_storage > 255:
                        continue
                    sentinel = r32(nm_data, pos - 0xD8)
                    if sentinel != 0xFFFFFFFF:
                        continue
                    level_code = r32(nm_data, pos - 0xDC)
                    if level_code == 0 or level_code >= 0xFFFFFFFE:
                        continue
                    sig_e0 = r32(nm_data, pos - 0xE0)
                    if (sig_e0 & 0xFFFF0000) != 0x42790000:
                        continue
                    name_start = pos + 4
                    null_pos = nm_data.find(b"\x00", name_start)
                    if null_pos < 0 or null_pos - name_start > 200:
                        continue
                    name = nm_data[name_start:null_pos].decode("ascii", errors="replace").strip()
                    if name and all(0x20 <= ord(c) <= 0x7E for c in name):
                        fmt_b[level_code] = name  # last-wins

                if fmt_b:
                    result.update(fmt_b)
                else:
                    # -- Pass 2: Format A fallback (magic at pos-0x20) ----------
                    for pos in range(0x20, len(nm_data) - 8):
                        if r32(nm_data, pos) != DGN_NM_NAME_MARKER:
                            continue
                        name_marker = r32(nm_data, pos - 0x0C)
                        if name_marker not in (0x56D2100B, 0x56D2100F):
                            continue
                        magic_val = r32(nm_data, pos - 0x20)
                        if (magic_val & 0xFFFF0000) != 0x42790000:
                            continue
                        level_code = r32(nm_data, pos - 0x1C)
                        if level_code == 0 or level_code >= 0xFFFFFFFE:
                            continue
                        name_start = pos + 4
                        null_pos = nm_data.find(b"\x00", name_start)
                        if null_pos < 0 or null_pos - name_start > 200:
                            continue
                        name = nm_data[name_start:null_pos].decode("ascii", errors="replace").strip()
                        if name and all(0x20 <= ord(c) <= 0x7E for c in name):
                            result[level_code] = name  # last-wins


        # 2. Parse Dgn^Nm/$2 (attachment/reference levels)
        if ole.exists(["Dgn^Nm", "$2"]):
            nm2_raw = ole.openstream(["Dgn^Nm", "$2"]).read()
            nm2_data = _decompress(nm2_raw)
            if nm2_data is not None:
                # Parse main records: XX 10 D2 56 (0x56D210XX)
                pos = 0
                main_records = []
                while True:
                    pos = nm2_data.find(b'\x10\xd2\x56', pos)
                    if pos < 0:
                        break
                    sig_start = pos - 1
                    if sig_start + 12 <= len(nm2_data):
                        name_len = _st.unpack_from('<I', nm2_data, sig_start + 8)[0]
                        if 0 < name_len < 256 and sig_start + 12 + name_len <= len(nm2_data):
                            name_bytes = nm2_data[sig_start + 12 : sig_start + 12 + name_len]
                            try:
                                name = name_bytes.decode('ascii', errors='replace').strip('\x00')
                            except Exception:
                                name = ""
                            if name and all(0x20 <= ord(c) <= 0x7E for c in name):
                                main_records.append((sig_start, name))
                    pos += 3

                # Parse property records: BD E9 79 42 (0x4279E9BD)
                pos = 0
                prop_records = []
                while True:
                    pos = nm2_data.find(b'\xbd\xe9\x79\x42', pos)
                    if pos < 0:
                        break
                    if pos + 8 <= len(nm2_data):
                        code = _st.unpack_from('<I', nm2_data, pos + 4)[0]
                        prop_records.append((pos, code))
                    pos += 4

                # Pair property records with the name record that immediately follows it
                # with wrap-around fallback to the first name record.
                if main_records and prop_records:
                    for p_pos, code in prop_records:
                        matched_name = None
                        for sig_start, name in main_records:
                            if sig_start > p_pos:
                                matched_name = name
                                break
                        if matched_name is None:
                            matched_name = main_records[0][1]
                        # Merge into result using the V7 code (level_id) only if not already defined
                        if code not in result:
                            result[code] = matched_name

        ole.close()
        return result

    except Exception as exc:  # pragma: no cover
        log.debug("_parse_dgn_nm_levels failed (%s): %s", dgn_path.name, exc)
        return {}


def _try_bridge_scan(dgn_path: Path) -> Optional[Tuple[List[dict], Dict[Tuple[int, int], str]]]:
    """
    Try to use the 32-bit Bentley bridge to read ALL elements.
    Returns (element dicts, level names), or None if bridge unavailable.
    
    The bridge outputs text lines to stdout:
      POLY model level N weight color elemType x1 y1 x2 y2 ... xN yN
      LINE model level weight color x1 y1 x2 y2
      ARC  model level weight color cx cy r startDeg sweepDeg
      CELL model level cx cy name
      TEXT model level x y content
      SKIP type model level
    
    Coordinates are parsed raw and dynamically scaled based on geometry range.
    """
    import subprocess
    global _LAST_BRIDGE_DIAG
    _LAST_BRIDGE_DIAG = ""

    bridge_exe = _find_bridge_exe()
    if bridge_exe is None:
        tried = "\n".join(f"  - {p}" for p in _bridge_exe_candidates())
        _LAST_BRIDGE_DIAG = "read_dgn.exe not found. Tried:\n" + tried
        log.warning("Bridge executable not found for %s", dgn_path.name)
        return None
    
    try:
        with _bridge_run_lock(timeout=310.0):
            with _suppress_windows_error_dialogs():
                bridge_env = os.environ.copy()
                # Always drive the bridge from OUR bundled lib/ so every machine —
                # dev or customer — uses the identical shipped DLL set. Previously we
                # preferred a full MicroStation install if one existed, which made the
                # dev box (which has the install) pass while clean customer PCs, which
                # fall back to the bundle, failed. Forcing the bundle makes the shipping
                # path the tested path. Callers may still override via MICROSTATION_DIR.
                _bundled_lib = _bridge_lib_for(bridge_exe)
                if "MICROSTATION_DIR" not in bridge_env and (_bundled_lib / "toolsubs.dll").exists():
                    bridge_env["MICROSTATION_DIR"] = str(_bundled_lib)
                # msconfig.cfg computes _USTN_BENTLEYROOT via the SDK's internal
                # ${_ROOTDIR}/${parentdevdir} macros, which only resolve correctly when
                # launched through a full MicroStation bootstrap. Loading dgnfileio.dll
                # standalone (LoadLibrary, no bootstrap) leaves them undefined on machines
                # without an install, so the SDK falls back to a compiled-in default
                # ("C:\Program Files (x86)\Bentley\Program\") and then fails to find
                # Workspace\users\untitled.ucf there -> fatal exit=2. msconfig.cfg defines
                # _USTN_BENTLEYROOT with Bentley's ":" (define-if-unset) operator, so
                # pre-setting it as a real OS env var makes the SDK use our bundled path
                # directly instead of its own broken auto-detection.
                if "_USTN_BENTLEYROOT" not in bridge_env and (_bundled_lib / "Workspace" / "users" / "untitled.ucf").exists():
                    bridge_env["_USTN_BENTLEYROOT"] = str(_bundled_lib) + "\\"
                _cflags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
                log.info("Bridge exe: %s", bridge_exe)
                log.info("Bridge lib: %s", _bundled_lib)
                result = subprocess.run(
                    [str(bridge_exe), str(dgn_path)],
                    capture_output=True, text=True, timeout=300,
                    env=bridge_env,
                    creationflags=_cflags,
                )
    except Exception as e:
        _LAST_BRIDGE_DIAG = f"Bridge launch failed: {type(e).__name__}: {e}"
        log.warning("Bridge failed: %s", e)
        return None

    if result.returncode != 0:
        _err_tail = (result.stderr or "").strip()
        if len(_err_tail) > 1500:
            _err_tail = "..." + _err_tail[-1500:]
        _LAST_BRIDGE_DIAG = f"read_dgn.exe exit={result.returncode}\n{_err_tail}"
        log.warning(
            "Bridge crashed or failed on %s (exit=%s); falling back to dgn_reader",
            dgn_path.name,
            result.returncode,
        )
        if _err_tail:
            log.warning("Bridge stderr:\n%s", _err_tail)
        return None
    if not result.stdout:
        _err_tail = (result.stderr or "").strip()
        if len(_err_tail) > 1500:
            _err_tail = "..." + _err_tail[-1500:]
        _LAST_BRIDGE_DIAG = (
            f"read_dgn.exe exit={result.returncode} but produced no stdout\n"
            f"exe={bridge_exe}\n"
            f"lib={_bridge_lib_for(bridge_exe)}\n"
            f"stderr={_err_tail or '(empty)'}"
        )
        log.warning("Bridge produced no stdout on %s", dgn_path.name)
        if _err_tail:
            log.warning("Bridge stderr:\n%s", _err_tail)
        return None
    
    # Parse text output
    uor_scales = {}
    elements = []
    level_names = {}   # (model_idx, level_code) -> name  — from SDK LEVEL records

    model_samples = {}

    def add_samples(model_idx, x, y):
        if model_idx not in model_samples:
            model_samples[model_idx] = []
        if len(model_samples[model_idx]) < 128:
            model_samples[model_idx].extend([x, y])

    for line in result.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue

        cmd = parts[0]

        if cmd == "LEVEL" and len(parts) >= 4:
            try:
                m    = int(parts[1])
                code = int(parts[2])
                name = " ".join(parts[3:]).strip()
                if name:
                    level_names[(m, code)] = name
            except (ValueError, IndexError):
                pass
            continue

        if cmd == "UOR" and len(parts) >= 3:
            try:
                model = int(parts[1])
                scale = float(parts[2])
                uor_scales[model] = scale
            except (ValueError, IndexError):
                pass
            continue
        
        if cmd == "POLY" and len(parts) >= 6:
            try:
                model = int(parts[1])
                level = int(parts[2])
                n = int(parts[3])
                wt = int(parts[4])
                col = int(parts[5])
                if len(parts) >= 7 + n * 2:
                    elem_type = int(parts[6])
                    if elem_type == 4:  # SDK LINE_STRING_ELM -> DGN_LINESTRING (3)
                        elem_type = 3
                    coord_offset = 7
                elif len(parts) < 6 + n * 2:
                    continue

                if len(parts) >= coord_offset + n * 2:
                    verts = []
                    for i in range(n):
                        x = float(parts[coord_offset + i*2])
                        y = float(parts[coord_offset + i*2 + 1])
                        verts.append((x, y, 0.0))
                        if i < 5:
                            add_samples(model, x, y)
                    if verts:
                        if elem_type is None:
                            closed = len(verts) >= 3 and verts[0][0] == verts[-1][0] and verts[0][1] == verts[-1][1]
                            elem_type = 6 if closed else 3
                        else:
                            closed = elem_type in _CLOSED_TYPES
                        elements.append({
                            'model_idx': model,
                            'type': elem_type,
                            'type_name': 'Shape' if closed else 'LineString',
                            'level': level,
                            'geometry_type': 'linestring',
                            'vertices': verts,
                            'symbology': {'weight': wt, 'color': col},
                        })
            except (ValueError, IndexError):
                pass
        
        elif cmd == "LINE" and len(parts) >= 9:
            try:
                model = int(parts[1])
                level = int(parts[2])
                wt = int(parts[3])
                col = int(parts[4])
                x1 = float(parts[5])
                y1 = float(parts[6])
                x2 = float(parts[7])
                y2 = float(parts[8])
                add_samples(model, x1, y1)
                elements.append({
                    'model_idx': model,
                    'type': 2,
                    'type_name': 'Line',
                    'level': level,
                    'geometry_type': 'line',
                    'start': (x1, y1, 0.0),
                    'end': (x2, y2, 0.0),
                    'vertices': [(x1, y1, 0.0), (x2, y2, 0.0)],
                    'symbology': {'weight': wt, 'color': col},
                })
            except (ValueError, IndexError):
                pass
        
        elif cmd == "TEXT" and len(parts) >= 6:
            try:
                model = int(parts[1])
                level = int(parts[2])
                x = float(parts[3])
                y = float(parts[4])
                
                has_height = False
                height = 1.0
                height_in_uor = True
                
                # Check for 8-part bridge output format (includes uor_height and user_height)
                # Format: TEXT model level x y uor_height user_height text
                if len(parts) >= 8:
                    try:
                        uor_h = float(parts[5])
                        user_h = float(parts[6])
                        text = " ".join(parts[7:])
                        height = uor_h if uor_h > 0 else user_h
                        has_height = True
                        height_in_uor = True
                    except ValueError:
                        pass
                
                # Fallback to older 7-part format: TEXT model level x y height text
                if not has_height and len(parts) >= 7:
                    try:
                        height = float(parts[5])
                        text = " ".join(parts[6:])
                        has_height = True
                        height_in_uor = True
                    except ValueError:
                        pass
                        
                if not has_height:
                    text = " ".join(parts[5:])
                    height = 1.0
                    height_in_uor = False
 
                add_samples(model, x, y)
                elements.append({
                    'model_idx': model,
                    'type': 18,
                    'type_name': 'Text',
                    'level': level,
                    'geometry_type': 'text',
                    'origin': (x, y, 0.0),
                    'text': text,
                    'height': height,
                    'height_in_uor': height_in_uor,
                })
            except (ValueError, IndexError):
                pass
        
        elif cmd == "ARC" and len(parts) >= 10:
            try:
                model = int(parts[1])
                level = int(parts[2])
                wt = int(parts[3])
                col = int(parts[4])
                cx = float(parts[5])
                cy = float(parts[6])
                r = float(parts[7])
                sa = float(parts[8])
                sw = float(parts[9])
                add_samples(model, cx, cy)
                elements.append({
                    'model_idx': model,
                    'type': 17,
                    'type_name': 'Arc',
                    'level': level,
                    'geometry_type': 'arc',
                    'center': (cx, cy, 0.0),
                    'radius': r,
                    'start_angle': sa * 3.14159265 / 180.0,
                    'sweep_angle': sw * 3.14159265 / 180.0,
                    'symbology': {'weight': wt, 'color': col},
                })
            except (ValueError, IndexError):
                pass
        
        elif cmd == "CELL" and len(parts) >= 5:
            # Skip generating redundant origin markers since the bridge already
            # recurses and outputs the cell's child components.
            try:
                model = int(parts[1])
                x = float(parts[3])
                y = float(parts[4])
                add_samples(model, x, y)
            except (ValueError, IndexError):
                pass
            continue
        
        elif cmd == "POINT" and len(parts) >= 8:
            try:
                model = int(parts[1])
                level = int(parts[2])
                n = int(parts[3])
                wt = int(parts[4])
                col = int(parts[5])
                if len(parts) >= 6 + n * 2:
                    verts = []
                    for i in range(n):
                        x = float(parts[6 + i*2])
                        y = float(parts[7 + i*2])
                        verts.append((x, y, 0.0))
                        if i < 5:
                            add_samples(model, x, y)
                    if verts:
                        elements.append({
                            'model_idx': model,
                            'type': 15,
                            'type_name': 'PointString',
                            'level': level,
                            'geometry_type': 'linestring',
                            'vertices': verts,
                            'symbology': {'weight': wt, 'color': col},
                        })
            except (ValueError, IndexError):
                pass
                
    # Resolve UOR scales and scale geometries
    detected_scales = {}
    for model_idx, samples in model_samples.items():
        reported_scale = uor_scales.get(model_idx)
        scale_val = detect_uor_scale(samples, reported_scale)
        # PRJ block coordinates are authoritative whenever named DGN labels
        # provide a verifiable correspondence. Heuristics are fallback-only.
        prj_scale = detect_scale_from_prj_blocks(elements, model_idx, dgn_path)
        if prj_scale is not None:
            scale_val = prj_scale
        else:
            scale_val = detect_scale_from_grid_labels(elements, model_idx, scale_val)
        detected_scales[model_idx] = scale_val
        if reported_scale is not None and reported_scale > 0:
            if math.isclose(scale_val, reported_scale):
                log.info("Model %d: using SDK UOR scale %.12g", model_idx, scale_val)
            else:
                log.info(
                    "Model %d: adjusted SDK UOR scale %.12g -> %.12g from coordinate analysis",
                    model_idx, reported_scale, scale_val,
                )
        else:
            log.warning("Model %d: bridge did not report UOR; using fallback scale %.12g", model_idx, detected_scales[model_idx])
        
    for elem in elements:
        model_idx = elem['model_idx']
        scale = detected_scales.get(model_idx, 1.0)
        # scale geometries
        if 'vertices' in elem:
            elem['vertices'] = [(v[0] / scale, v[1] / scale, v[2]) for v in elem['vertices']]
        if 'start' in elem:
            elem['start'] = (elem['start'][0] / scale, elem['start'][1] / scale, elem['start'][2])
        if 'end' in elem:
            elem['end'] = (elem['end'][0] / scale, elem['end'][1] / scale, elem['end'][2])
        if 'origin' in elem:
            elem['origin'] = (elem['origin'][0] / scale, elem['origin'][1] / scale, elem['origin'][2])
        if 'center' in elem:
            elem['center'] = (elem['center'][0] / scale, elem['center'][1] / scale, elem['center'][2])
        if 'radius' in elem:
            elem['radius'] = elem['radius'] / scale
        if 'height' in elem:
            if elem.get('height_in_uor', True):
                elem['height'] = _scale_bridge_text_height(
                    elem['height'],
                    scale,
                    level_names.get(
                        (elem.get('model_idx'), elem.get('level')),
                        '',
                    ),
                )
            
    return (elements, level_names) if elements else None


def _extract_dxf_texts(dxf_path: Path) -> List[dict]:
    """Extract TEXT entities from a DXF file (companion to DGN).
    
    Parses DXF TEXT entities to get grid/block labels like LAZ filenames.
    Returns list of element dicts compatible with the bridge output format.
    """
    texts = []
    try:
        with open(dxf_path, 'r', errors='replace') as f:
            lines = f.readlines()
    except Exception:
        return []
    
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # Look for TEXT entity start
        if line == "TEXT" and i > 0 and lines[i-1].strip() == "0":
            # Parse the TEXT entity
            x = y = 0.0
            text_val = ""
            layer = ""
            height = 5.0
            j = i + 1
            while j < len(lines) and not (lines[j].strip() == "0" and j + 1 < len(lines) and lines[j+1].strip() in ("TEXT", "MTEXT", "LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "ENDSEC", "EOF")):
                code = lines[j].strip()
                j += 1
                if j >= len(lines):
                    break
                val = lines[j].strip()
                j += 1
                if code == "8":      # layer
                    layer = val
                elif code == "10":   # X coordinate
                    try: x = float(val)
                    except: pass
                elif code == "20":   # Y coordinate
                    try: y = float(val)
                    except: pass
                elif code == "40":   # text height
                    try: height = float(val)
                    except: pass
                elif code == "1":    # text content
                    text_val = val
            
            if text_val and (abs(x) > 1.0 or abs(y) > 1.0):
                texts.append({
                    'type': 18,
                    'type_name': 'Text',
                    'level': 64,  # Use a high level number for DXF-sourced text
                    'geometry_type': 'text',
                    'origin': (x, y, 0.0),
                    'text': text_val,
                    'height': height,
                    'layer_name': layer,  # preserve DXF layer for mapping
                })
            i = j
        else:
            i += 1
    
    return texts

def _parse_rawdgn(raw_path: Path) -> List[dict]:
    """Parse a .rawdgn binary file into element dicts."""
    import struct as _st
    
    data = raw_path.read_bytes()
    if data[:8] != b"DGNRAW01":
        return []
    
    UOR_SCALE = 10000.0
    VERT_OFFSET = 112
    VERT_STRIDE = 24
    POINT_OFFSET = 152
    
    elements = []
    pos = 8
    
    while pos + 10 <= len(data):
        t = _st.unpack_from('<H', data, pos)[0]
        level = _st.unpack_from('<I', data, pos + 2)[0]
        size = _st.unpack_from('<I', data, pos + 6)[0]
        
        if t == 0xFFFF:
            break
        
        body = data[pos + 10:pos + 10 + size]
        pos += 10 + size
        
        elem = None
        
        if t in (3, 4):  # Line / LineString / GroupData
            verts = _extract_verts_from_body(body, UOR_SCALE, VERT_OFFSET, VERT_STRIDE)
            if verts:
                elem = {
                    'type': 3 if t == 3 else 3,  # Map to LineString
                    'type_name': 'LineString',
                    'level': level,
                    'geometry_type': 'linestring',
                    'vertices': verts,
                }
        
        elif t == 6:  # Shape (closed polygon)
            verts = _extract_verts_from_body(body, UOR_SCALE, VERT_OFFSET, VERT_STRIDE)
            if verts:
                elem = {
                    'type': 6,
                    'type_name': 'Shape',
                    'level': level,
                    'geometry_type': 'linestring',
                    'vertices': verts,
                }
        
        elif t == 15:  # PointString
            pt = _extract_point_from_body(body, UOR_SCALE, POINT_OFFSET)
            if pt:
                elem = {
                    'type': 15,
                    'type_name': 'PointString',
                    'level': level,
                    'geometry_type': 'linestring',
                    'vertices': [pt],
                }
        
        elif t == 17:  # Arc (skip full circles)
            # Full circles are skipped by _to_snt anyway
            # Just mark as arc with sweep=2pi so the skip logic handles it
            elem = {
                'type': 17,
                'type_name': 'Arc',
                'level': level,
                'geometry_type': 'arc',
                'center': (0, 0, 0),
                'radius': 0,
                'start_angle': 0,
                'sweep_angle': 6.283185,  # Full circle → will be skipped
            }
        
        if elem:
            elements.append(elem)
    
    return elements


def _extract_verts_from_body(body: bytes, scale: float, offset: int, stride: int) -> List[tuple]:
    """Extract float64 UOR vertex pairs from element body."""
    import struct as _st
    verts = []
    off = offset
    while off + 16 <= len(body):
        x_uor = _st.unpack_from('<d', body, off)[0]
        y_uor = _st.unpack_from('<d', body, off + 8)[0]
        x = x_uor / scale
        y = y_uor / scale
        if abs(x) > 100 and abs(x) < 100000000 and abs(y) > 100 and abs(y) < 100000000:
            verts.append((x, y, 0.0))
        else:
            break
        off += stride
    return verts


def _extract_point_from_body(body: bytes, scale: float, offset: int) -> Optional[tuple]:
    """Extract a single point from element body."""
    import struct as _st
    if offset + 16 > len(body):
        return None
    x_uor = _st.unpack_from('<d', body, offset)[0]
    y_uor = _st.unpack_from('<d', body, offset + 8)[0]
    x = x_uor / scale
    y = y_uor / scale
    if abs(x) > 100 and abs(x) < 100000000 and abs(y) > 100 and abs(y) < 100000000:
        return (x, y, 0.0)
    return None


# =============================================================================
#  Main converter
# =============================================================================

def convert(
    dgn_path:       Path,
    snt_path:       Optional[Path] = None,
    description:    str            = "",
    follow_refs:    bool           = False,
    max_depth:      int            = 5,
    skip_invisible: bool           = True,
    decimate:       int            = 1,
    exclude_trajectory: bool       = False,
    trajectory_keywords: Optional[Tuple[str, ...]] = None,
) -> ConvertResult:
    """
    Convert a DGN / .sanket file to SNT binary.

    Parameters
    ----------
    dgn_path            Input file (.dgn or .sanket extension).
    snt_path            Output path -- defaults to <stem>.snt next to the input.
    description         Free-text description embedded in the SNT metadata block.
    follow_refs         Follow external reference attachments (xrefs).
    max_depth           Maximum xref recursion depth (default 5).
    skip_invisible      Skip elements with properties.invisible == True.
    exclude_trajectory  If True, skips trajectory-like elements.
    trajectory_keywords Keywords used to identify trajectory elements.
    """
    dgn_path = Path(dgn_path)
    if not dgn_path.exists():
        raise FileNotFoundError(f"DGN file not found: {dgn_path}")

    if snt_path is None:
        snt_path = dgn_path.with_suffix(".snt")
    snt_path = Path(snt_path)

    source_size: int    = dgn_path.stat().st_size
    warn_log: List[str] = []

    log.info("Opening  : %s  (%.2f MB)", dgn_path.name, source_size / 1_048_576)

    # -- Open DGN -------------------------------------------------------------
    is_3d    = False
    fmt      = "OLE_V8"
    n_models = 1
    n_levels = 0
    crs_info = None
    dgn_file = None

    if _dgn is not None:
        try:
            dgn_file  = _dgn.open(str(dgn_path))
            is_3d     = bool(dgn_file.is_3d())
            fmt       = dgn_file.get_format()
            n_models  = dgn_file.get_model_count()
            n_levels  = dgn_file.get_level_count()
            crs_info  = dgn_file.get_coordinate_system()   # may be None
        except Exception as e:
            log.warning("dgn_reader failed to open file (falling back to bridge only): %s", e)
            dgn_file = None

    log.info("Format   : %-10s  3D=%-5s  models=%d  levels=%d",
             fmt, is_3d, n_models, n_levels)

    # -- Phase 1: Run bridge scan first so we have real level names -----------
    # Try the Bentley bridge first (gets ALL elements including cells)
    # Falls back to custom C extension if bridge unavailable
    _bridge_result   = _try_bridge_scan(dgn_path)
    bridge_elements  = None
    _bridge_lvl_names: Dict[Tuple[int, int], str] = {}   # (model_idx, level_code) -> real SDK name

    if _bridge_result is not None:
        bridge_elements, _lvl_raw = _bridge_result
        _bridge_lvl_names = _lvl_raw

    # Parse level names directly from DGN Nm binary stream (works without C ext or MicroStation session)
    _nm_lvl_names: Dict[int, str] = _parse_dgn_nm_levels(dgn_path)
    if _nm_lvl_names:
        log.info("Nm stream : %d level names parsed from DGN", len(_nm_lvl_names))

    # Pre-cache dgn_reader level definitions if C reader is used or as fallback
    _dgn_reader_lvl_names: Dict[int, str] = {}
    _dgn_reader_lvl_colors: Dict[int, int] = {}
    if dgn_file is not None:
        for li in range(n_levels):
            info = dgn_file.get_level(li)
            code = int(info["number"])
            _dgn_reader_lvl_names[code] = str(info.get("name") or f"Level{code}")
            _dgn_reader_lvl_colors[code] = int(info.get("color", 7))

    def _is_generic(n: str) -> bool:
        return not n or (n.startswith("Level") and n[5:].isdigit())

    def get_layer_name(model_idx: int, lnum: int) -> str:
        # Priority 1: bridge real name (non-generic)
        name = _bridge_lvl_names.get((model_idx, lnum))
        if _is_generic(name):
            # Priority 2: dgn_reader C extension names (only if non-generic)
            dgn_name = _dgn_reader_lvl_names.get(lnum)
            if dgn_name and not _is_generic(dgn_name):
                name = dgn_name
            else:
                # Priority 3: Nm binary stream fallback
                nm_name = _nm_lvl_names.get(lnum)
                if nm_name:
                    name = nm_name
        if not name:
            name = f"Level{lnum}"
        return name

    def get_layer_default_color(model_idx: int, lnum: int) -> int:
        return _dgn_reader_lvl_colors.get(lnum, 0)

    if bridge_elements is not None:
        all_models = [("#bridge", bridge_elements)]
        log.info("  Bridge  : %d elements (Bentley dgnfileio.dll)", len(bridge_elements))
    elif dgn_file is not None:
        if fmt == "OLE_V8":
            dgn_file.close()
            raise RuntimeError(
                "Bridge failed to read V8 DGN file.\n"
                + (_LAST_BRIDGE_DIAG or "(no bridge diagnostics captured)")
            )
        all_models = dgn_file.scan_all_models(
            follow_attachments=follow_refs,
            max_depth=max_depth,
        )
    else:
        if fmt == "OLE_V8":
            raise RuntimeError(
                "Bridge failed to read V8 DGN file (dgn_reader fallback unavailable).\n"
                + (_LAST_BRIDGE_DIAG or "(no bridge diagnostics captured)")
            )
        raise RuntimeError("Neither bridge nor dgn_reader available for: " + str(dgn_path))

    if dgn_file is not None:
        dgn_file.close()

    # -- Expand cell components for pure C dgn_reader mode ---------------------
    if bridge_elements is None and dgn_file is not None:
        expanded_models = []
        for model_name, elements in all_models:
            expanded_elements = []
            for el in elements:
                dgn_type = int(el.get("type", 0))
                # Expand cells (type 34, 36, 37, 96)
                if dgn_type in (34, 36, 37, 96) and "cell_components" in el:
                    for child in el["cell_components"]:
                        child_copy = dict(child)
                        if not child_copy.get("level") and el.get("level"):
                            child_copy["level"] = el["level"]
                        expanded_elements.append(child_copy)
                else:
                    expanded_elements.append(el)
            expanded_models.append((model_name, expanded_elements))
        all_models = expanded_models

    # Normalize trajectory keywords
    normalized_keywords = []
    if exclude_trajectory:
        keywords_to_use = trajectory_keywords or (
            "trajectory", "traj", "path", "track", "flight", "gps", "route"
        )
        normalized_keywords = [k.strip().lower() for k in keywords_to_use if k and k.strip()]

    def is_trajectory_like_element(el: dict, lvl_name: str) -> bool:
        if not normalized_keywords:
            return False
            
        def _has_keyword(text: str | None) -> bool:
            if not text:
                return False
            text_l = text.lower()
            return any(k in text_l for k in normalized_keywords)

        if _has_keyword(lvl_name):
            return True
        if _has_keyword(el.get("type_name")):
            return True
        if _has_keyword(el.get("text")):
            return True
        for key, value in el.get("properties", {}).items():
            if _has_keyword(str(key)) or _has_keyword(str(value)):
                return True
        return False

    # Collect all unique layer names to build a sorted alphabetical layer table
    all_layer_names_set = set()
    layer_default_colors = {"0": 7}

    # Pre-cache default colors keyed by resolved name (only used if an element references the layer)
    for code, name in _dgn_reader_lvl_names.items():
        resolved_name = get_layer_name(0, code)
        if resolved_name not in layer_default_colors:
            layer_default_colors[resolved_name] = _dgn_reader_lvl_colors.get(code, 7)

    # Only add a layer when an element actually uses it — do NOT pre-seed from the DGN
    # level table or bridge LEVEL records. DGN files often contain hundreds of levels
    # inherited from referenced/attached files that have zero entities in this file.
    # Pre-seeding those produces phantom empty layers that don't exist in the DXF output.
    for model_name, elements in all_models:
        for el in elements:
            lnum = int(el.get("level", 0))
            model_idx = int(el.get("model_idx", 0)) if bridge_elements is not None else 0
            name = get_layer_name(model_idx, lnum)
            all_layer_names_set.add(name)

    # Build sorted alphabetical layer table of actually used layers
    sorted_layer_names = sorted(list(all_layer_names_set))
    if not sorted_layer_names:
        sorted_layer_names = ["0"]
    pool    = _StringPool()
    layers: List[_Layer]      = []
    layer_names: List[str]    = []
    layer_name_to_idx: Dict[str, int] = {}

    for idx, name in enumerate(sorted_layer_names):
        layer_name_to_idx[name] = idx
        layer_names.append(name)
        
        # Determine color
        color = layer_default_colors.get(name, 0)
        if color == 0:
            # Generate deterministic color from index (matches DGN level index mapping logic)
            aci = _LEVEL_COLOR_CYCLE[idx % len(_LEVEL_COLOR_CYCLE)]
        else:
            aci = _DGN_TO_ACI.get(color & 0xFF, color & 0xFF) or 7
            
        layers.append(_Layer(pool.get(name), aci, pool.get("CONTINUOUS")))

    entity_count   = 0
    index_entries: List[bytes] = []
    entity_offset  = 0
    xmin_all = ymin_all =  1e38
    xmax_all = ymax_all = -1e38
    type_counts: Dict[str, int] = {}

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".dgnsnt_ent", dir=snt_path.parent)
    try:
        with os.fdopen(tmp_fd, "wb", buffering=1 << 20) as ent_file:

            for model_name, elements in all_models:
                log.info("  Model %-22s  %d raw elements", repr(model_name), len(elements))
                skipped = 0

                for el_idx, el in enumerate(elements):
                    if decimate > 1 and el_idx % decimate != 0:
                        skipped += 1
                        continue
                    dgn_type  = int(el.get("type", 0))
                    el_bb = _bbox(el)

                    # The legacy bridge can emit zero-length LINE placeholders
                    # at the origin. They are not drawing geometry and the
                    # standalone converter excludes them from its SNT output.
                    if dgn_type == DGN_LINE and el_bb is None:
                        skipped += 1
                        continue

                    # Skip admin / non-geometry types
                    if dgn_type in _SKIP_TYPES:
                        skipped += 1
                        continue

                    # Skip invisible
                    if skip_invisible:
                        if el.get("properties", {}).get("invisible", False):
                            skipped += 1
                            continue

                    type_name = str(el.get("type_name", f"Type{dgn_type}"))

                    # -- Layer -------------------------------------------------
                    lnum      = int(el.get("level", 0))
                    model_idx = int(el.get("model_idx", 0)) if bridge_elements is not None else 0
                    layer_name = get_layer_name(model_idx, lnum)

                    # -- Map to SNT entity list --------------------------------
                    snt_entities = _to_snt(el, pool, layer_name)

                    # Trajectory Filtering Check
                    if exclude_trajectory and is_trajectory_like_element(el, layer_name):
                        skipped += 1
                        continue

                    layer_idx = layer_name_to_idx[layer_name]

                    # -- Color -------------------------------------------------
                    # DGN color 0 = "use layer default" (same as BYLAYER).
                    # Values 1-255 are indices into DwgDisplayColors.tbl;
                    # map to nearest ACI using _DGN_TO_ACI, passing unknown
                    # indices through (ACI 1-255 all valid) so the layer cycle
                    # colour shows through for levels whose color=0.
                    symb  = el.get("symbology", {}) or {}
                    color = int(symb.get("color", 256))
                    if color <= 0 or color >= 256:
                        # 256 = not set / no symbology dict; 0 = layer default.
                        # Give each point *kind* a distinct colour for the
                        # black NakshaAI canvas:
                        #   CELL-origin markers  → cyan    (ACI 4) – cool, pairs well with label text.
                        #   Trajectory PointStr  → blue    (ACI 5) – classic GIS blue.
                        if el.get('_source') == 'cell':
                            cmode, cval = CM.ACI, 4   # Cyan – block markers
                        elif dgn_type == DGN_POINT_STRING:
                            cmode, cval = CM.ACI, 5   # Blue   – trajectory pts
                        else:
                            cmode, cval = CM.BYLAYER, 0
                    else:
                        # Translate DGN palette index to ACI
                        aci = _DGN_TO_ACI.get(color, color) or 7
                        cmode, cval = CM.ACI, aci

                    # -- Linetype / lineweight ---------------------------------
                    lt_idx = pool.get("BYLAYER")
                    lw     = max(0, min(255, int(symb.get("weight", 0) or 0)))

                    # -- Reuse element bbox for every generated sub-entity
                    if el_bb is not None:
                        bx0, by0, bx1, by1 = el_bb
                    else:
                        bx0 = by0 = bx1 = by1 = 0.0

                    # -- Write each SNT entity for this DGN element -----------
                    for snt_type, body in snt_entities:
                        # Skip placeholder / unmappable entities -- no geometry
                        # to render, writing them wastes space in the SNT file.
                        if snt_type == ET.UNKNOWN:
                            continue
                        hdr = struct.pack(
                            ENTITY_HDR_FMT,
                            snt_type,
                            layer_idx & 0xFFFF,
                            cmode,
                            cval,
                            lt_idx,
                            lw,
                            len(body),
                        )
                        assert len(hdr) == ENTITY_HDR_SIZE
                        ent_file.write(hdr)
                        ent_file.write(body)

                        # Index entry (per SNT entity)
                        index_entries.append(struct.pack(
                            INDEX_FMT,
                            entity_count,
                            float(bx0), float(by0),
                            float(bx1), float(by1),
                            entity_offset,
                        ))

                        # Global bbox -- only update from real geometry
                        # (el_bb is None for elements with no usable coords)
                        if el_bb is not None:
                            _BIG = 1e15
                            if (abs(bx0) < _BIG and abs(by0) < _BIG
                                    and abs(bx1) < _BIG and abs(by1) < _BIG):
                                xmin_all = min(xmin_all, bx0)
                                ymin_all = min(ymin_all, by0)
                                xmax_all = max(xmax_all, bx1)
                                ymax_all = max(ymax_all, by1)

                        entity_offset += ENTITY_HDR_SIZE + len(body)
                        entity_count  += 1

                    type_counts[type_name] = type_counts.get(type_name, 0) + 1

                    if entity_count % 10_000 == 0:
                        log.info("    ... %d entities processed", entity_count)

                log.info("    Mapped %d  skipped %d",
                         len(elements) - skipped, skipped)

        # -- Phase 3: Build SNT metadata block --------------------------------
        if xmin_all > xmax_all:
            xmin_all = ymin_all = xmax_all = ymax_all = 0.0

        bbox_tuple = (xmin_all, ymin_all, xmax_all, ymax_all)
        hem, est_lat = _hemisphere(ymin_all, ymax_all)

        meta: Dict[str, Any] = {
            "creator":             CREATOR,
            "creator_description": CREATOR_DESC,
            "description":         description.strip(),
            "source":              dgn_path.name,
            "dgn_format":          fmt,
            "dgn_is_3d":           is_3d,
            "snt_version":         f"{VERSION_MAJOR}.{VERSION_MINOR}",
            "crs": {
                "hemisphere":    hem,
                "estimated_lat": est_lat,
                "confidence":    "medium",
                "notes":         [],
            },
        }
        if crs_info:
            meta["dgn_crs"]           = {k: str(v) for k, v in crs_info.items()}
            meta["crs"]["confidence"] = "high"

        meta_json  = json.dumps(meta, ensure_ascii=False).encode("utf-8")
        meta_comp  = zlib.compress(meta_json, level=3)
        meta_block = struct.pack(META_SIZE_FMT, len(meta_comp)) + meta_comp

        strings_block = pool.serialize()
        layers_block  = b"".join(l.pack() for l in layers)
        blocks_block  = b""
        index_block   = b"".join(index_entries)

        # Block offsets
        off_meta     = HEADER_SIZE
        off_strings  = off_meta    + len(meta_block)
        off_layers   = off_strings + len(strings_block)
        off_blocks   = off_layers  + len(layers_block)
        off_entities = off_blocks  + len(blocks_block)
        off_index    = off_entities + entity_offset

        flags = FLAG_HAS_INDEX | FLAG_HAS_CRS

        # Build header (CRC placeholder = 0)
        header_body = struct.pack(
            HEADER_FMT,
            SNT_MAGIC, VERSION_MAJOR, VERSION_MINOR,
            flags, entity_count,
            xmin_all, ymin_all, xmax_all, ymax_all,
            off_meta, off_strings, off_layers, off_blocks, off_entities, off_index,
            0,              # CRC32 placeholder
            b"\x00" * 28,  # reserved
        )
        assert len(header_body) == HEADER_SIZE

        # Compute and patch CRC
        crc_val     = zlib.crc32(header_body[:HEADER_CRC_OFF]) & 0xFFFF_FFFF
        header_body = (
            header_body[:HEADER_CRC_OFF]
            + struct.pack("<I", crc_val)
            + header_body[HEADER_CRC_OFF + 4:]
        )

        # -- Atomic write: temp -> final --------------------------------------
        tmp2_fd, tmp2_path = tempfile.mkstemp(suffix=".snt_w", dir=snt_path.parent)
        try:
            with os.fdopen(tmp2_fd, "wb") as out:
                out.write(header_body)
                out.write(meta_block)
                out.write(strings_block)
                out.write(layers_block)
                out.write(blocks_block)
                with open(tmp_path, "rb") as ent_in:
                    while True:
                        chunk = ent_in.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                out.write(index_block)
            # On Windows os.replace() can fail if the target is locked
            # (antivirus, Explorer preview, leftover handles). Retry with
            # explicit delete first.
            for _attempt in range(5):
                try:
                    os.replace(tmp2_path, snt_path)
                    break
                except PermissionError:
                    if _attempt < 4:
                        try:
                            snt_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                        time.sleep(0.2)
                    else:
                        raise
        except Exception:
            if os.path.exists(tmp2_path):
                os.unlink(tmp2_path)
            raise

    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    out_size = snt_path.stat().st_size

    # -- Summary log ----------------------------------------------------------
    log.info("-" * 60)
    log.info("Written  : %s  (%.2f MB)", snt_path.name, out_size / 1_048_576)
    log.info("Entities : %d    Layers: %d    Pool: %d strings",
             entity_count, len(layers), len(pool))
    log.info("BBox     : X [%.1f .. %.1f]  Y [%.1f .. %.1f]",
             xmin_all, xmax_all, ymin_all, ymax_all)
    log.info("")
    log.info("Entity type breakdown:")
    for t, n in sorted(type_counts.items(), key=lambda x: -x[1]):
        log.info("  %-28s  %6d", t, n)

    # -- Report ---------------------------------------------------------------
    report_path = _write_report(
        snt_path, dgn_path, description, entity_count,
        len(layers), len(pool), source_size, out_size,
        bbox_tuple, hem, est_lat, type_counts, layer_names,
        is_3d, fmt, warn_log,
    )
    log.info("Report   : %s", report_path.name)

    return ConvertResult(
        snt_path          = snt_path,
        report_path       = report_path,
        entity_count      = entity_count,
        layer_count       = len(layers),
        string_pool_size  = len(pool),
        source_size_bytes = source_size,
        output_size_bytes = out_size,
        bbox              = bbox_tuple,
        type_counts       = type_counts,
        layer_names       = layer_names,
        warnings          = warn_log,
        is_3d             = is_3d,
    )


# =============================================================================
#  Report writer
# =============================================================================

def _write_report(
    snt_path:    Path,
    dgn_path:    Path,
    description: str,
    n_ent:       int,
    n_layers:    int,
    n_pool:      int,
    src_sz:      int,
    out_sz:      int,
    bbox:        Tuple[float, float, float, float],
    hemisphere:  str,
    est_lat:     Optional[float],
    type_counts: Dict[str, int],
    layer_names: List[str],
    is_3d:       bool,
    fmt:         str,
    warnings:    List[str],
) -> Path:
    import datetime
    rp  = snt_path.with_suffix(".report.txt")
    now = datetime.datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    xmin, ymin, xmax, ymax = bbox
    DIV = "=" * 66
    SEC = "-" * 66
    W   = 20

    def lbl(label: str, value: str) -> str:
        return f"  {label:<{W}}: {value}"

    L: List[str] = [
        DIV, "  DGN -> SNT Conversion Report",
        "  Sanket NakshaTech -- NakshaApp  v1.2", DIV, "",
        lbl("Generated",    now),
        lbl("Source DGN",   dgn_path.name),
        lbl("Output SNT",   snt_path.name),
        lbl("Description",  description or "(none)"),
        lbl("DGN format",   fmt),
        lbl("3-D drawing",  str(is_3d)),
        "",
        SEC, "  FILE STATISTICS", SEC,
        lbl("Source size",   f"{src_sz/1_048_576:.2f} MB  ({src_sz:,} bytes)"),
        lbl("Output size",   f"{out_sz/1_048_576:.2f} MB  ({out_sz:,} bytes)"),
        lbl("String pool",   f"{n_pool:,} unique strings"),
        "",
        SEC, "  ENTITY SUMMARY", SEC,
        lbl("Total entities",  f"{n_ent:,}"),
        lbl("Layers",          f"{n_layers}"),
        "",
        "  Entity breakdown (DGN type  ->  SNT entity):",
    ]
    col = max((len(k) for k in type_counts), default=12) + 2
    for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
        L.append(f"    {t:<{col}}  {c:>8,}")
    L += [
        "",
        "  Layer table:",
    ]
    for i, name in enumerate(layer_names):
        L.append(f"    [{i:>3}]  {name}")
    L += [
        "",
        SEC, "  CRS / COORDINATE REFERENCE", SEC,
        lbl("Hemisphere",    hemisphere),
        lbl("Est. latitude", f"{est_lat:.4f}deg" if est_lat is not None else "unknown"),
        lbl("BBox X",        f"{int(xmin):,}  ->  {int(xmax):,}   delta={int(xmax-xmin):,}"),
        lbl("BBox Y",        f"{int(ymin):,}  ->  {int(ymax):,}   delta={int(ymax-ymin):,}"),
        "",
        SEC, f"  WARNINGS  ({len(warnings)})", SEC,
        *([f"  !  {w}" for w in warnings] if warnings else ["  (none)"]),
        "", DIV, "  END OF REPORT", DIV, "",
    ]
    for _attempt in range(5):
        try:
            rp.write_text("\n".join(L), encoding="utf-8")
            break
        except PermissionError:
            if _attempt < 4:
                try:
                    rp.unlink(missing_ok=True)
                except OSError:
                    pass
                time.sleep(0.2)
            else:
                raise
    return rp


# =============================================================================
#  CLI
# =============================================================================

def main() -> None:
    ap = argparse.ArgumentParser(
        description="DGN -> SNT converter -- NakshaApp overlay format v1.2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "2-D top-view: all line-like geometry is written as LWPOLYLINE (x,y per\n"
            "              vertex, bulge=0), matching the DXF -> SNT pipeline exactly.\n"
        ),
    )
    ap.add_argument("dgn_path",            help="Input .dgn or .sanket file")
    ap.add_argument("snt_path", nargs="?", help="Output .snt  (default: <dgn_stem>.snt)")
    ap.add_argument("-d", "--description", default="",
                    help="One-line description embedded in SNT metadata")
    ap.add_argument("--follow-refs",       action="store_true",
                    help="Follow external reference (xref) attachments")
    ap.add_argument("--max-depth",         type=int, default=5,
                    help="Max xref recursion depth (default 5)")
    ap.add_argument("--include-invisible", action="store_true",
                    help="Include invisible elements (default: skip)")
    ap.add_argument("-v", "--verbose",     action="store_true",
                    help="Show DEBUG messages")
    ap.add_argument("--decimate",         type=int, default=1,
                    help="Decimation factor (e.g. 10 to keep only 10% of elements for testing)")

    args = ap.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)



    result = convert(
        dgn_path       = Path(args.dgn_path),
        snt_path       = Path(args.snt_path) if args.snt_path else None,
        description    = args.description,
        follow_refs    = args.follow_refs,
        max_depth      = args.max_depth,
        skip_invisible = not args.include_invisible,
        decimate       = args.decimate,
    )



    SEP = "=" * 56
    print()
    print(SEP)
    print("  DGN --> SNT  CONVERSION COMPLETE")
    print(SEP)
    print(f"  SNT file    : {result.snt_path}")
    print(f"  Report      : {result.report_path}")
    print(f"  Entities    : {result.entity_count:,}")
    print(f"  Layers      : {result.layer_count}")
    print(f"  3-D drawing : {result.is_3d}")
    print(f"  Output size : {result.output_size_bytes:,} bytes"
          f"  ({result.output_size_bytes/1_048_576:.2f} MB)")

    print(SEP)
    print(SEP)


if __name__ == "__main__":
    main()
