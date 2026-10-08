"""CAMERA / LOD / VISIBLE-POINT PARITY GATE.

Three-way coverage proof for every camera view (INITIAL / PAN / ZOOM / FIT):

    A. EXPECTED  - source-covered cells visible through the canonical camera
    B. SELECTED  - coverage the current desired TileSpecs represent
    C. ACTIVE    - coverage actually drawing (fallback parents included)

Import-safe: no VTK, no GPU, no LAZ decode. Pure geometry + set algebra, so it
can be unit tested headlessly and cannot itself break a render frame.

THE ORACLE (PHASE 3)
--------------------
`SourceOccupancy` is built ONCE from the NKIDX node table. That table IS the
proven source/LOD0 occupancy: every node's XY box is a region the SOURCE
actually covers, and the union of node boxes is the source footprint. Decoding
the source LAZ per frame is forbidden and unnecessary.

RESOLUTION HONESTY
------------------
The 10 m level is the HARD gross-coverage gate and is derived exactly from the
node boxes. 5 m and 1 m are obtained by SUBDIVIDING the proven 10 m truth, so a
fine cell is "expected" exactly when its 10 m parent holds source data. Fine
levels are therefore conservative-and-informational: they can flag a fine gap,
but they can never invent a hole outside genuine source coverage.
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, Optional, Sequence, Tuple

import numpy as np

# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #
CELL_SIZES_M = (10.0, 5.0, 1.0)
HARD_CELL_M = 10.0        # the gross-coverage gate
DIAG_CELL_M = 5.0         # the stronger diagnostic
INFO_CELL_M = 1.0         # informational only

EPSILON_M = 1e-9
SIGNATURE_QUANTUM = 1e-4
MAX_CELLS_PER_AUDIT = 40000

# PHASE 18 - exactly ONE label per failure.
ROOT_SOURCE = "SOURCE"
ROOT_CACHE = "CACHE"
ROOT_CAMERA = "CAMERA"
ROOT_SELECTOR = "SELECTOR"
ROOT_READINESS = "READINESS"
ROOT_ACTIVATION = "ACTIVATION"
ROOT_HANDOFF = "HANDOFF"
ROOT_ASYNC_GENERATION = "ASYNC_GENERATION"
ROOT_GPU_DRAW_RANGE = "GPU_DRAW_RANGE"
ROOT_RASTERIZATION = "RASTERIZATION"

ROOT_SELECTED_FAIL = ROOT_SELECTOR
ROOT_ACTIVE_FAIL = f"{ROOT_ACTIVATION} / {ROOT_HANDOFF}"
ROOT_SCREEN_FAIL = f"{ROOT_GPU_DRAW_RANGE} / {ROOT_RASTERIZATION}"


def _finite(value) -> Optional[float]:
    """float(value) or None for None / NaN / Inf / non-numeric."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# --------------------------------------------------------------------------- #
# PHASE 2 - canonical camera sample + validity                                  #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CameraSample:
    """A canonical, validated camera observation.

    `ok=False` samples must never become a streaming generation: they are
    rejected BEFORE any frontier, service generation or camera state is
    touched (PHASE 2).
    """

    ok: bool
    source: str
    reason: str
    signature: Optional[tuple] = None
    center: Optional[tuple] = None
    world_size: Optional[tuple] = None
    bounds: Optional[tuple] = None
    width_px: int = 0
    height_px: int = 0

    def __bool__(self) -> bool:      # `if sample:` reads as "usable sample"
        return bool(self.ok)


def quantise(value: float, quantum: float = SIGNATURE_QUANTUM) -> float:
    """Stable quantisation so float noise is never a camera change."""
    return round(float(value) / float(quantum)) * float(quantum)


def camera_signature(center, world_size, width_px: int, height_px: int) -> tuple:
    """(cx, cy, world_width, world_height, width_px, height_px).

    Deliberately byte-for-byte the same tuple the stream manager already used,
    so adopting the canonical sample cannot change what counts as motion.
    """
    return (round(quantise(center[0]), 6),
            round(quantise(center[1]), 6),
            round(quantise(world_size[0]), 6),
            round(quantise(world_size[1]), 6),
            int(width_px), int(height_px))

def read_camera_sample(camera=None, viewport=None, source: str = "unknown"
                       ) -> CameraSample:
    """Validate + canonicalise one camera observation.

    The precedence mirrors the stream manager's own signature capture: an
    explicit ``viewport`` tuple wins, and the pixel size comes from the camera
    object (where the VTK widget size is already applied).

    Rejection reasons are explicit so `[CAMERA SAMPLE REJECTED]` can report
    which rule fired rather than just "it was invalid".
    """
    cx = cy = half_w = half_h = None
    if viewport is not None and len(viewport) >= 4:
        cx, cy = _finite(viewport[0]), _finite(viewport[1])
        half_w, half_h = _finite(viewport[2]), _finite(viewport[3])
        if len(viewport) >= 6:
            width_px = _finite(viewport[4])
            height_px = _finite(viewport[5])
        else:
            # A 4-tuple viewport carries no pixel size, so it must come from the
            # camera object - rejecting here would reject every real VTK sample.
            width_px = _finite(getattr(camera, "width_px", None))
            height_px = _finite(getattr(camera, "height_px", None))
    else:
        cx = _finite(getattr(camera, "cx",
                             getattr(camera, "center_x", 0.0)))
        cy = _finite(getattr(camera, "cy",
                             getattr(camera, "center_y", 0.0)))
        half_w = _finite(getattr(camera, "half_w",
                                 getattr(camera, "half_width", 0.0)))
        half_h = _finite(getattr(camera, "half_h",
                                 getattr(camera, "height_m", 0.0)))
        width_px = _finite(getattr(camera, "width_px", 0.0))
        height_px = _finite(getattr(camera, "height_px", 0.0))
        if (half_h is not None and half_w is not None
                and half_h == 0.0 and half_w > 0.0
                and (width_px or 0) > 0.0 and (height_px or 0) > 0.0):
            half_h = half_w * float(height_px) / float(width_px)

    for name, value in (("center_x", cx), ("center_y", cy),
                        ("half_width", half_w), ("half_height", half_h)):
        if value is None or not math.isfinite(value):
            return CameraSample(False, source, f"non_finite_{name}")

    world_w = 2.0 * float(half_w)
    world_h = 2.0 * float(half_h)
    if world_w <= EPSILON_M:
        return CameraSample(False, source, "world_width<=epsilon")
    if world_h <= EPSILON_M:
        return CameraSample(False, source, "world_height<=epsilon")

    w_px = int(width_px) if width_px else 0
    h_px = int(height_px) if height_px else 0
    if w_px <= 0 or h_px <= 0:
        return CameraSample(False, source, "non_positive_viewport")

    center = (float(cx), float(cy))
    world_size = (world_w, world_h)
    bounds = (center[0] - half_w, center[1] - half_h,
              center[0] + half_w, center[1] + half_h)
    return CameraSample(
        True, source, "ok",
        signature=camera_signature(center, world_size, w_px, h_px),
        center=center, world_size=world_size, bounds=bounds,
        width_px=w_px, height_px=h_px)


# --------------------------------------------------------------------------- #
# Grid geometry                                                                #
# --------------------------------------------------------------------------- #
# Cells are anchored to the GLOBAL origin, never to the camera. A camera-anchored
# grid shifts its own coordinates under the data on every pan, which fabricates
# "missing" cells out of pure sub-cell motion.
def cell_span(bounds, cell_m: float):
    """Inclusive (i0, i1, j0, j1) index range whose CENTRES can lie in bounds."""
    x0, y0, x1, y1 = (float(bounds[0]), float(bounds[1]),
                      float(bounds[2]), float(bounds[3]))
    step = float(cell_m)
    i0 = int(math.floor(x0 / step))
    i1 = int(math.floor(x1 / step))
    j0 = int(math.floor(y0 / step))
    j1 = int(math.floor(y1 / step))
    return i0, max(i0, i1), j0, max(j0, j1)


def cells_in_bounds(bounds, cell_m: float,
                    max_cells: int = MAX_CELLS_PER_AUDIT) -> set:
    """Cells whose CENTRE lies inside bounds (the proven hole-metric rule)."""
    step = float(cell_m)
    i0, i1, j0, j1 = cell_span(bounds, step)
    nx, ny = (i1 - i0 + 1), (j1 - j0 + 1)
    if nx * ny > max_cells:
        # Audit cost is bounded by COARSENING, never by dropping cells: a
        # silently truncated audit would understate the hole count.
        step *= math.sqrt((nx * ny) / float(max_cells))
        i0, i1, j0, j1 = cell_span(bounds, step)
    x0, y0, x1, y1 = (float(bounds[0]), float(bounds[1]),
                      float(bounds[2]), float(bounds[3]))
    out = set()
    for i in range(i0, i1 + 1):
        sx = (i + 0.5) * step
        if not (x0 <= sx <= x1):
            continue
        for j in range(j0, j1 + 1):
            sy = (j + 0.5) * step
            if y0 <= sy <= y1:
                out.add((i, j))
    return out


def as_box(box):
    """Normalise an XY box to nested ``((x0,y0),(x1,y1))``.

    Node bounds arrive nested (from `_node_bounds_xy`) while view rectangles are
    flat `(x0,y0,x1,y1)`. Accepting both here means a caller cannot silently pass
    the wrong shape and get a nonsensical grid.
    """
    if box is None:
        return None
    seq = list(box)
    if len(seq) == 2 and not isinstance(seq[0], (int, float, np.floating,
                                                  np.integer)):
        lo, hi = seq
        return ((float(lo[0]), float(lo[1])), (float(hi[0]), float(hi[1])))
    if len(seq) == 4:
        return ((float(seq[0]), float(seq[1])), (float(seq[2]), float(seq[3])))
    return None


def cells_for_boxes(boxes, bounds, cell_m: float) -> set:
    """Cells covered by the UNION of XY boxes and lying inside bounds.

    Overlapping boxes are legal during a handoff, so this is a UNION and never a
    per-box test.
    """
    step = float(cell_m)
    x0, y0, x1, y1 = (float(bounds[0]), float(bounds[1]),
                      float(bounds[2]), float(bounds[3]))
    out = set()
    for raw in boxes or ():
        box = as_box(raw)
        if box is None:
            continue
        (bx0, by0), (bx1, by1) = box
        if bx1 < x0 or bx0 > x1 or by1 < y0 or by0 > y1:
            continue
        i0 = int(math.floor(max(bx0, x0) / step))
        i1 = int(math.floor(min(bx1, x1) / step))
        j0 = int(math.floor(max(by0, y0) / step))
        j1 = int(math.floor(min(by1, y1) / step))
        for i in range(i0, i1 + 1):
            sx = (i + 0.5) * step
            if not (bx0 <= sx <= bx1 and x0 <= sx <= x1):
                continue
            for j in range(j0, j1 + 1):
                sy = (j + 0.5) * step
                if by0 <= sy <= by1 and y0 <= sy <= y1:
                    out.add((i, j))
    return out


def connected_regions(cells) -> list:
    """4-connected components of a cell set, largest first.

    Region COUNT is what distinguishes a real hole from a single lost cell: one
    stray cell is a rounding artefact, many separated regions are rectangles.
    """
    remaining = set(cells or ())
    regions = []
    while remaining:
        seed = remaining.pop()
        region = {seed}
        stack = [seed]
        while stack:
            i, j = stack.pop()
            for nb in ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)):
                if nb in remaining:
                    remaining.discard(nb)
                    region.add(nb)
                    stack.append(nb)
        regions.append(region)
    regions.sort(key=len, reverse=True)
    return regions


def region_bounds(cells, cell_m: float):
    """World XY bounds of a cell set."""
    if not cells:
        return None
    step = float(cell_m)
    i = [int(c[0]) for c in cells]
    j = [int(c[1]) for c in cells]
    return ((min(i) * step, min(j) * step),
            ((max(i) + 1) * step, (max(j) + 1) * step))


def largest_gap_m(cells, cell_m: float) -> float:
    """Longest side of the largest connected missing region, in metres."""
    regions = connected_regions(cells)
    if not regions:
        return 0.0
    box = region_bounds(regions[0], cell_m)
    return round(max(box[1][0] - box[0][0], box[1][1] - box[0][1]), 3)


# --------------------------------------------------------------------------- #
# PHASE 3 - trusted SOURCE / LOD0 occupancy oracle                              #
# --------------------------------------------------------------------------- #
class SourceOccupancy:
    """Trusted source occupancy, materialised ONCE and never re-decoded.

    Built from the NKIDX node table, whose XY boxes are exactly the regions the
    SOURCE covers (they partition the LOD0 footprint). Cells outside the union
    are GENUINE source no-data, never counted as a hole.

    Built at ``base_cell_m`` (10 m) and subdivided for finer levels, so the hard
    gate is exact and the finer diagnostics inherit that truth rather than
    guessing their own.
    """

    def __init__(self, boxes, dataset_bounds=None, source_point_count: int = 0,
                 base_cell_m: float = HARD_CELL_M):
        self.boxes = tuple(boxes or ())
        self.dataset_bounds = dataset_bounds
        self.source_point_count = int(source_point_count or 0)
        self.base_cell_m = float(base_cell_m)
        if dataset_bounds:
            b = self.dataset_bounds
            frame = (float(b[0][0]), float(b[0][1]),
                     float(b[1][0]), float(b[1][1]))
        elif self.boxes:
            frame = self._union_box()
        else:
            frame = (0.0, 0.0, 0.0, 0.0)
        # FLAT (x0, y0, x1, y1) everywhere: the grid helpers all take flat rects,
        # and a nested box here silently produced a float(tuple) TypeError.
        self.bounds = frame
        self._base = cells_for_boxes(self.boxes, frame, self.base_cell_m)

    def _union_box(self):
        xs0 = min(float(b[0][0]) for b in self.boxes)
        ys0 = min(float(b[0][1]) for b in self.boxes)
        xs1 = max(float(b[1][0]) for b in self.boxes)
        ys1 = max(float(b[1][1]) for b in self.boxes)
        return (xs0, ys0, xs1, ys1)

    @classmethod
    def from_index(cls, idx, base_cell_m: float = HARD_CELL_M):
        """Build the oracle from a live NKIDX index. No LAZ decode."""
        boxes = []
        try:
            nodes = idx.nodes
            if nodes is not None and nodes.size:
                nmin = np.asarray(nodes["bounds_min"], dtype=np.float64)
                nmax = np.asarray(nodes["bounds_max"], dtype=np.float64)
                for i in range(nmin.shape[0]):
                    boxes.append(((float(nmin[i, 0]), float(nmin[i, 1])),
                                  (float(nmax[i, 0]), float(nmax[i, 1]))))
        except Exception:
            boxes = []
        ds = None
        try:
            hdr = idx.header
            ds = ((float(hdr["bounds_min"][0]), float(hdr["bounds_min"][1])),
                  (float(hdr["bounds_max"][0]), float(hdr["bounds_max"][1])))
        except Exception:
            ds = None
        count = 0
        try:
            count = int(idx.header["source_point_count"])
        except Exception:
            count = 0
        return cls(boxes, dataset_bounds=ds, source_point_count=count,
                   base_cell_m=base_cell_m)

    @property
    def known(self) -> bool:
        return bool(self._base)

    def _ratio(self, cell_m: float) -> int:
        return max(1, int(round(float(cell_m) / self.base_cell_m)))

    def expected_cells(self, bounds, cell_m: float) -> set:
        """EXPECTED_VISIBLE_CELLS: source-covered cells inside the view."""
        ratio = self._ratio(cell_m)
        out = set()
        for cell in cells_in_bounds(bounds, cell_m):
            if (int(cell[0]) // ratio, int(cell[1]) // ratio) in self._base:
                out.add(cell)
        return out


# --------------------------------------------------------------------------- #
# PHASE 4/5 - three-way coverage at 1 m / 5 m / 10 m                           #
# --------------------------------------------------------------------------- #
@dataclass
class LevelParity:
    cell_m: float
    expected: set = field(default_factory=set)
    selected: set = field(default_factory=set)
    active: set = field(default_factory=set)

    @property
    def selected_missing(self) -> set:
        return self.expected - self.selected

    @property
    def active_missing(self) -> set:
        return self.expected - self.active

    @property
    def selected_extra(self) -> set:
        return self.selected - self.expected

    @property
    def active_extra(self) -> set:
        return self.active - self.expected

    @property
    def selected_missing_regions(self) -> int:
        return len(connected_regions(self.selected_missing))

    @property
    def active_missing_regions(self) -> int:
        return len(connected_regions(self.active_missing))

    @property
    def largest_selected_gap_m(self) -> float:
        return largest_gap_m(self.selected_missing, self.cell_m)

    @property
    def largest_active_gap_m(self) -> float:
        return largest_gap_m(self.active_missing, self.cell_m)

    @property
    def selected_missing_bounds(self):
        return region_bounds(self.selected_missing, self.cell_m)

    @property
    def active_missing_bounds(self):
        return region_bounds(self.active_missing, self.cell_m)

    def summary(self) -> dict:
        return {
            "cell_m": self.cell_m,
            "expected_cells": len(self.expected),
            "selected_cells": len(self.selected),
            "active_cells": len(self.active),
            "selected_missing": len(self.selected_missing),
            "active_missing": len(self.active_missing),
            "selected_extra": len(self.selected_extra),
            "active_extra": len(self.active_extra),
            "selected_missing_regions": self.selected_missing_regions,
            "active_missing_regions": self.active_missing_regions,
            "largest_selected_gap_m": self.largest_selected_gap_m,
            "largest_active_gap_m": self.largest_active_gap_m,
            "selected_missing_bounds": self.selected_missing_bounds,
            "active_missing_bounds": self.active_missing_bounds,
        }


def clip_bounds(bounds, dataset_bounds):
    """Viewport rect clipped to the dataset footprint.

    ``dataset_bounds`` is FLAT (x0, y0, x1, y1) - the same shape the grid helpers
    use, so a nested box can never be half-applied.
    """
    x0, y0, x1, y1 = (float(bounds[0]), float(bounds[1]),
                      float(bounds[2]), float(bounds[3]))
    if dataset_bounds:
        x0 = max(x0, float(dataset_bounds[0]))
        y0 = max(y0, float(dataset_bounds[1]))
        x1 = min(x1, float(dataset_bounds[2]))
        y1 = min(y1, float(dataset_bounds[3]))
    return (x0, y0, x1, y1)


def three_way_parity(oracle: SourceOccupancy, bounds,
                     selected_boxes, active_boxes,
                     cell_sizes=CELL_SIZES_M) -> dict:
    """A / B / C coverage at every resolution.

    ``active_boxes`` MUST already include legitimate fallback parents: the ACTIVE
    side answers "what is drawing", not "what is finest".
    """
    view = clip_bounds(bounds, oracle.bounds)
    out = {}
    for cell_m in cell_sizes:
        expected = oracle.expected_cells(view, cell_m) if oracle.known else set()
        level = LevelParity(cell_m, expected,
                            cells_for_boxes(selected_boxes, view, cell_m),
                            cells_for_boxes(active_boxes, view, cell_m))
        out[float(cell_m)] = level
    return out


# --------------------------------------------------------------------------- #
# PHASE 18 - exactly ONE automatic root-cause label                             #
# --------------------------------------------------------------------------- #
def classify_root_cause(expected_known: bool, selected_missing_10m: int,
                        active_missing_10m: int, expected_5m_missing: int = 0,
                        expected_1m_missing: int = 0) -> str:
    """One label per failure, derived only from which stage broke.

    EXPECTED PASS + SELECTED FAIL           -> SELECTOR
    EXPECTED PASS + SELECTED PASS + ACTIVE  -> ACTIVATION / HANDOFF
    EXPECTED itself unknown                  -> CAMERA (view not provable)
    """
    if not expected_known:
        return ROOT_CAMERA
    if selected_missing_10m > 0 or expected_5m_missing > 0:
        return ROOT_SELECTOR
    if active_missing_10m > 0:
        return ROOT_ACTIVE_FAIL
    # Fine-level loss with a clean 10 m gate is informational (coarse LODs are
    # sparse by construction), so it is NOT a stage failure.
    return ""


def root_cause_all_three_pass() -> str:
    """All three coverage gates PASS and the screen is still wrong."""
    return ROOT_SCREEN_FAIL


# --------------------------------------------------------------------------- #
# PHASE 16/19 - the per-step report block                                      #
# --------------------------------------------------------------------------- #
@dataclass
class ParityReport:
    step: str = "UNNAMED"
    frame_id: int = 0
    generation_id: int = 0
    timestamp: float = field(default_factory=time.time)
    camera_center: tuple = (0.0, 0.0)
    camera_world_size: tuple = (0.0, 0.0)
    levels: dict = field(default_factory=dict)
    selected_points: int = 0
    submitted_points: int = 0
    lod_distribution: dict = field(default_factory=dict)
    fallback_blocks: int = 0
    frame_errors: int = 0
    expected_known: bool = False

    @property
    def hard(self):
        return self.levels.get(HARD_CELL_M)

    @property
    def diag(self):
        return self.levels.get(DIAG_CELL_M)

    @property
    def info(self):
        return self.levels.get(INFO_CELL_M)

    @property
    def selected_missing_10m(self) -> int:
        return len(self.hard.selected_missing) if self.hard else 0

    @property
    def active_missing_10m(self) -> int:
        return len(self.hard.active_missing) if self.hard else 0

    @property
    def root_cause(self) -> str:
        return classify_root_cause(
            self.expected_known, self.selected_missing_10m,
            self.active_missing_10m,
            expected_5m_missing=(len(self.diag.selected_missing)
                                 if self.diag else 0))

    def verdict(self) -> str:
        """PARITY: PASS / FAIL - judged on the 10 m hard gate."""
        if not self.expected_known:
            return "FAIL"
        if self.selected_missing_10m or self.active_missing_10m:
            return "FAIL"
        return "PASS"

    def as_dict(self) -> dict:
        return {
            "step": self.step,
            "frame_id": self.frame_id,
            "generation_id": self.generation_id,
            "timestamp": round(self.timestamp, 3),
            "camera_center": [round(float(v), 4) for v in self.camera_center],
            "camera_world_size": [round(float(v), 4)
                                  for v in self.camera_world_size],
            "expected_known": self.expected_known,
            "levels": {f"{k:g}m": v.summary()
                       for k, v in sorted(self.levels.items(),
                                          key=lambda kv: -kv[0])},
            "selected_points": self.selected_points,
            "submitted_points": self.submitted_points,
            "lod_distribution": dict(self.lod_distribution),
            "fallback_blocks": self.fallback_blocks,
            "frame_errors": self.frame_errors,
            "root_cause": self.root_cause,
            "verdict": self.verdict(),
        }

    def render(self) -> str:
        """The exact compact PHASE 16 block, one per stable step."""
        h = self.hard
        return "\n".join([
            "[VISIBLE PARITY]",
            f"step: {self.step}",
            f"generation: {self.generation_id}",
            f"frame_id: {self.frame_id}",
            f"timestamp: {round(self.timestamp, 3)}",
            f"camera_center: ({self.camera_center[0]:.4f}, "
            f"{self.camera_center[1]:.4f})",
            f"camera_world_size: ({self.camera_world_size[0]:.4f} x "
            f"{self.camera_world_size[1]:.4f})",
            f"expected_10m_cells: {len(h.expected) if h else 0}",
            f"selected_10m_cells: {len(h.selected) if h else 0}",
            f"active_10m_cells: {len(h.active) if h else 0}",
            f"selected_missing_10m: {self.selected_missing_10m}",
            f"active_missing_10m: {self.active_missing_10m}",
            f"largest_selected_gap_m: {h.largest_selected_gap_m if h else 0.0}",
            f"largest_active_gap_m: {h.largest_active_gap_m if h else 0.0}",
            f"selected_points: {self.selected_points}",
            f"submitted_points: {self.submitted_points}",
            f"LOD_distribution: {self.lod_distribution}",
            f"fallback_blocks: {self.fallback_blocks}",
            f"frame_errors: {self.frame_errors}",
            f"ROOT_CAUSE: {self.root_cause or 'none'}",
            f"PARITY: {self.verdict()}",
            "[/VISIBLE PARITY]",
        ])


# --------------------------------------------------------------------------- #
# PHASE 10 - camera -> LOD response parity                                      #
# --------------------------------------------------------------------------- #
ZOOM_RATIO_EPS = 1.2      # a zoom above this MUST be geometrically verified


def lod_distribution(keys) -> dict:
    """{lod: block_count} for a set of (node_id, lod) keys."""
    out = {}
    for key in keys or ():
        lod = int(key[1])
        out[lod] = out.get(lod, 0) + 1
    return dict(sorted(out.items()))


def camera_lod_parity(old_signature, new_signature, old_keys, new_keys,
                     zoom_ratio_eps: float = ZOOM_RATIO_EPS) -> dict:
    """Decide whether an unchanged selection after a real zoom is legitimate.

    An identical selection is NOT automatically a failure: the same key set can
    legitimately cover a new viewport. This returns the geometric evidence and a
    verdict so the CALLER can prove spatial correctness instead of assuming it.
    """
    old_keys = frozenset(old_keys or ())
    new_keys = frozenset(new_keys or ())
    old_sig = tuple(old_signature or ())
    new_sig = tuple(new_signature or ())
    selection_changed = bool(old_keys != new_keys)
    bounds_changed = bool(len(old_sig) >= 4 and len(new_sig) >= 4
                          and old_sig[:4] != new_sig[:4])
    pan_delta = None
    zoom_ratio = None
    if len(old_sig) >= 4 and len(new_sig) >= 4:
        pan_delta = (round(new_sig[0] - old_sig[0], 6),
                     round(new_sig[1] - old_sig[1], 6))
        old_w, old_h = float(old_sig[2]), float(old_sig[3])
        new_w, new_h = float(new_sig[2]), float(new_sig[3])
        if old_w > 0.0 and old_h > 0.0:
            zoom_ratio = round(max(new_w / old_w, new_h / old_h, 0.01), 6)
    material_zoom = bool(zoom_ratio is not None
                         and (zoom_ratio >= zoom_ratio_eps
                              or (1.0 / zoom_ratio) >= zoom_ratio_eps))
    if selection_changed:
        status = "PASS"
        reason = "selection responded to the camera change"
    elif material_zoom:
        status = "VERIFY"
        reason = (f"zoom ratio {zoom_ratio}x left the selection bit-identical; "
                  f"spatial correctness must be proven, not assumed")
    else:
        status = "PASS"
        reason = "camera change was below the material-zoom threshold"
    return {
        "old_signature": old_sig,
        "new_signature": new_sig,
        "pan_delta": pan_delta,
        "zoom_ratio": zoom_ratio,
        "old_selected_hash": hash(old_keys),
        "new_selected_hash": hash(new_keys),
        "old_LOD_distribution": lod_distribution(old_keys),
        "new_LOD_distribution": lod_distribution(new_keys),
        "old_selected_count": len(old_keys),
        "new_selected_count": len(new_keys),
        "visible_bounds_changed": bounds_changed,
        "selection_changed": selection_changed,
        "material_zoom": material_zoom,
        "status": status,
        "reason": reason,
    }


_CAMERA_LOD_FIELDS = ("pan_delta", "zoom_ratio", "old_selected_hash",
                      "new_selected_hash", "old_selected_count",
                      "new_selected_count", "old_LOD_distribution",
                      "new_LOD_distribution", "visible_bounds_changed",
                      "selection_changed", "material_zoom", "status", "reason")


def render_camera_lod_parity(result: dict, generation: int) -> str:
    lines = ["[CAMERA LOD PARITY]", f"generation: {generation}"]
    lines += [f"{name}: {result.get(name)}" for name in _CAMERA_LOD_FIELDS]
    lines.append("[/CAMERA LOD PARITY]")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# PHASE 11 - selected tile / camera intersection                                #
# --------------------------------------------------------------------------- #
def tile_camera_intersection(keys, boxes, visible_bounds,
                             expected_cells_in_view=None, overview_key=None):
    """Which selected tiles fall outside the view, and which visible regions
    have no selected representation.

    A tile outside the view is legal ONLY when it is a PARENT FALLBACK whose box
    still covers the visible region. Anything else means the selector is using
    stale camera bounds.
    """
    x0, y0, x1, y1 = (float(visible_bounds[0]), float(visible_bounds[1]),
                      float(visible_bounds[2]), float(visible_bounds[3]))
    bounds = boxes or {}
    view = (x0, y0, x1, y1)
    outside, fallback, inside = [], [], []
    for key in keys or ():
        key = tuple(key)
        box = as_box(bounds.get(key))
        if box is None:
            outside.append(key)
            continue
        bx0, by0 = box[0]
        bx1, by1 = box[1]
        overlaps = not (bx1 <= x0 or bx0 >= x1 or by1 <= y0 or by0 >= y1)
        if overlaps:
            inside.append(key)
        elif key == tuple(overview_key or ()):
            # The coarse floor spans the whole dataset by construction, so it is
            # outside a zoomed viewport while still covering it. Legal.
            fallback.append(key)
        else:
            outside.append(key)
    unrepresented = []
    if expected_cells_in_view:
        covered = cells_for_boxes([bounds[k] for k in inside
                                   if k in bounds], view, HARD_CELL_M)
        # Fallback parents also keep coverage alive during a handoff.
        covered |= cells_for_boxes([bounds[k] for k in fallback
                                    if k in bounds], view, HARD_CELL_M)
        unrepresented = sorted(set(expected_cells_in_view) - covered)
    return {
        "selected_total": len(keys or ()),
        "selected_inside_view": len(inside),
        "selected_outside_view": outside,
        "legitimate_parent_fallback": fallback,
        "visible_cells_without_selected_representation": len(unrepresented),
        "largest_unrepresented_bounds":
            region_bounds(unrepresented, HARD_CELL_M),
    }


# --------------------------------------------------------------------------- #
# PHASE 12 - generation / async parity ledger                                   #
# --------------------------------------------------------------------------- #
STATES = ("NOT_REQUESTED", "QUEUED", "READING", "RAM_READY", "GPU_READY",
          "ACTIVE", "BLOCKED")


def runtime_state_of(key, manager) -> str:
    """Canonical runtime state name for a key, for the ACTIVATION report."""
    key = tuple(key)
    resident = getattr(manager, "resident", {}) or {}
    ram = getattr(manager, "ram_cache", {}) or {}
    pending = getattr(manager, "_pending", {}) or {}
    tile = resident.get(key)
    if tile is not None:
        snap = None
        try:
            snap = manager._tile_readiness_snapshot(key)
        except Exception:
            snap = None
        blocked = (snap or {}).get("blocking_attribute")
        if blocked:
            return "BLOCKED"
        state = str(getattr(tile, "state", "") or "")
        if state == "GPU_RESIDENT":
            return "ACTIVE"
        if state in ("GPU_PENDING", "RAM_READY"):
            return "RAM_READY"
        return "BLOCKED" if state == "EVICTABLE" else "GPU_READY"
    if key in ram:
        return "RAM_READY"
    if key in pending:
        return "READING"
    return "NOT_REQUESTED"


def activation_report(keys, manager, missing_bounds=None):
    """The `[PARITY FAIL - ACTIVATION]` payload: who is responsible and why."""
    rows = []
    for key in sorted({tuple(k) for k in (keys or ())}):
        state = runtime_state_of(key, manager)
        blocking = None
        if state == "BLOCKED":
            try:
                blocking = manager._tile_readiness_snapshot(key).get(
                    "blocking_attribute")
            except Exception:
                blocking = "unknown"
        rows.append({"key": list(key), "state": state,
                     "blocking_attribute": blocking})
    return {"responsible_keys": rows,
            "missing_bounds": missing_bounds,
            "root_cause": ROOT_ACTIVE_FAIL}


def async_parity_report(manager) -> dict:
    """PHASE 12 counters. `incorrectly_activated` MUST be 0, always."""
    stale = int(getattr(manager, "stale_completions", 0) or 0)
    accepted = int(getattr(manager, "stale_accepted_into_ram", 0) or 0)
    bad = int(getattr(manager, "stale_incorrectly_activated", 0) or 0)
    return {
        "stale_completions": stale,
        "accepted_into_ram": accepted,
        "incorrectly_activated": bad,
        "PASS": bad == 0,
        "root_cause": "" if bad == 0 else ROOT_ASYNC_GENERATION,
    }


# --------------------------------------------------------------------------- #
# PHASE 0 - runtime-type-agnostic tile access                                   #
# --------------------------------------------------------------------------- #
# THE DEFECT
# ----------
# `NakshaStreamManager._tile_readiness_snapshot` read `sprite.xyz` off whatever
# object it was handed. `self.ram_cache` holds PACKED DICTS (what `_pack_block`
# returns), so any replacement tile decoded but not yet GPU-admitted raised
# AttributeError("'dict' object has no attribute 'xyz'"). The chain is
# _reconcile -> _retire_superseded -> _emit_pending_replacement_diag ->
# _tile_readiness_snapshot: a DIAGNOSTIC aborted retirement, request scheduling
# and the GPU flush. Fixed by reading each runtime type through its own canonical
# accessors - NOT by blanket-converting one type into the other, which would
# have hidden the difference instead of handling it.
PACKED_XYZ_KEYS = ("xyz",)
PACKED_RGB_KEYS = ("rgb",)
PACKED_CLS_KEYS = ("cls",)
PACKED_INTEN_KEYS = ("inten", "intensity")
PACKED_NORMAL_KEYS = ("normal",)
PACKED_COUNT_KEYS = ("pts", "point_count", "count")
PACKED_READINESS_KEYS = ("readiness",)


def tile_is_mapping(tile) -> bool:
    """True for a packed block DICT, False for a ResidentTile-like object."""
    return isinstance(tile, dict) or hasattr(tile, "keys")


def tile_field(tile, attr: str, packed_keys: Sequence[str] = ()):
    """Read one field from EITHER runtime form.

    ResidentTile exposes attributes; a packed block dict exposes canonical keys.
    Returning None for "absent" means the caller never needs to know which form
    it received - which is exactly what the old code got wrong.
    """
    if tile is None:
        return None
    if tile_is_mapping(tile):
        for key in (packed_keys or (attr,)):
            try:
                if key in tile:
                    return tile[key]
            except TypeError:
                return None
        return None
    return getattr(tile, attr, None)


def tile_count(tile) -> int:
    value = tile_field(tile, "count", PACKED_COUNT_KEYS)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def tile_xyz(tile):
    return tile_field(tile, "xyz", PACKED_XYZ_KEYS)


def tile_normal_ready(tile) -> bool:
    """Stored-normal availability, with the same answer for both runtime forms.

    Mirrors ResidentTile.normal_ready: a readiness object answers it when a
    NORMAL pass ran, otherwise the presence of a normal payload does.
    """
    if tile is None:
        return False
    readiness = tile_field(tile, "readiness", PACKED_READINESS_KEYS)
    if readiness is not None:
        return bool(getattr(readiness, "normal_ready", False))
    if tile_is_mapping(tile):
        return tile_field(tile, "normal", PACKED_NORMAL_KEYS) is not None
    normal_ready = getattr(tile, "normal_ready", None)
    if normal_ready is not None:
        return bool(normal_ready)
    return getattr(tile, "normal", None) is not None


def tile_complete(tile) -> bool:
    """Mode-completeness, with the same answer for both runtime forms."""
    if tile is None:
        return False
    if not tile_is_mapping(tile):
        complete = getattr(tile, "complete", None)
        if complete is not None:
            return bool(complete)
    readiness = tile_field(tile, "readiness", PACKED_READINESS_KEYS)
    if readiness is not None:
        return bool(getattr(readiness, "complete", False))
    return bool(tile_count(tile) > 0 and tile_xyz(tile) is not None)


# --------------------------------------------------------------------------- #
# PHASE 0 - diagnostics must NEVER abort the production path                     #
# --------------------------------------------------------------------------- #
class DiagnosticGuard:
    """Runs a diagnostic, prints ONE warning on failure, never propagates.

    Deliberately scoped: it wraps TELEMETRY/PARITY/DIAGNOSTIC work only. Renderer
    and streaming exceptions are never hidden behind it, because a swallowed
    renderer bug is worse than a visible one.
    """

    def __init__(self, name: str, owner=None):
        self.name = str(name)
        self.owner = owner
        self.warnings = 0
        self.last_error = ""

    def _record(self, exc: BaseException) -> None:
        self.warnings += 1
        self.last_error = f"{type(exc).__name__}: {exc}"
        if self.owner is not None:
            try:
                self.owner.diagnostic_error_count = int(
                    getattr(self.owner, "diagnostic_error_count", 0) or 0) + 1
            except Exception:
                pass
        # ONE warning for the first occurrence, then a periodic heartbeat. A
        # 60 Hz stack trace buries everything else in the console.
        if self.warnings == 1:
            print(f"WARNING: [parity] {self.name} diagnostic suppressed: "
                  f"{self.last_error}")
        elif self.warnings % 300 == 0:
            print(f"WARNING: [parity] {self.name} diagnostic still failing "
                  f"({self.warnings}x): {self.last_error}")

    def run(self, fn, *args, **kwargs):
        """Execute the diagnostic. Returns ``None`` if it failed."""
        try:
            return fn(*args, **kwargs)
        except Exception as exc:          # diagnostics only - never the renderer
            self._record(exc)
            return None

    def report(self) -> str:
        return (f"{self.name}: {self.warnings} suppressed failure(s)"
                + (f" last={self.last_error}" if self.last_error else ""))


# --------------------------------------------------------------------------- #
# PART A1 - tile diagnostics gate + guard factory                              #
# --------------------------------------------------------------------------- #
# Cross-talk anchor: the tile-artifact DEV overlay (Part A1, tile_diagnostics.py)
# and the shading-state validator (Part E, RenderStateValidator) both run on
# the per-frame path, and both must pay ZERO cost when the DEV switch is off.
# ``dev_diagnostics_enabled`` is the single shared gate, and ``_diag_guard``
# is the zero-allocation accessor that returns a cached DiagnosticGuard per
# name so one failing reporter cannot take down the frame.

_DEV_DIAG_CACHE = None  # cached bool, re-read each call via _dev_diag_env()
_DEV_GUARDS: Dict[str, DiagnosticGuard] = {}


def _dev_diag_env() -> bool:
    """Re-read the NAKSHA_DEV_DIAGNOSTICS env var every call (test-togglable)."""
    return os.environ.get("NAKSHA_DEV_DIAGNOSTICS", "") not in (
        "", "0", "false", "False", "off", "OFF",
    )


def dev_diagnostics_enabled() -> bool:
    """Cached single-source-of-truth for the DEV_DIAG gate.

    Mirrors ``tile_diagnostics.dev_diagnostics_enabled`` so callers can use
    either module without an import cycle. Re-reads the env var each call so
    tests can toggle it without reloading.
    """
    global _DEV_DIAG_CACHE
    val = _dev_diag_env()
    _DEV_DIAG_CACHE = val
    return val


def _diag_guard(name: str, owner=None) -> DiagnosticGuard:
    """Return a lazily-created, cached DiagnosticGuard by name.

    Production callers: ``manager._diag_guard("parity")`` inside
    ``visible_parity_uncached`` and the tile-artifact overlay.

    Cost when diagnostics OFF: one dict lookup (cache hit) — no guard
    ``run`` is ever invoked on the hot path. The guard's ``run`` method is
    the only place that executes wrapped code, and it is gated by
    ``dev_diagnostics_enabled()`` at the call site.
    """
    g = _DEV_GUARDS.get(name)
    if g is None:
        g = DiagnosticGuard(name, owner=owner)
        _DEV_GUARDS[name] = g
    elif owner is not None and g.owner is None:
        g.owner = owner
    return g
