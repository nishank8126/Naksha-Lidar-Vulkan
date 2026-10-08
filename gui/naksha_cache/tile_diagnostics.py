"""tile_diagnostics.py - Part A1: tile-artifact DEV diagnostics overlay.

Produces a fixed 64x36 screen-quality map and a tile-artifact-region counter.
The quality map marks each screen-grid cell with the finest LOD present at that
screen position; a cell with no resident coverage is an artifact hole.  The
region count groups adjacent hole cells into connected regions, so a single
stray cell is not mistaken for the rectangular gaps that indicate a real
streaming defect.

Import-safe and GPU-free: it only reads the stream manager's resident/selection
state and the viewport geometry, so it can always run under
NAKSHA_DEV_DIAGNOSTICS without a Vulkan context.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

# Fixed screen-grid resolution (Part A1 constant). 64x36 is fine enough to
# catch tile-sized gaps on a 1920x848 viewport while staying cheap to scan.
GRID_W = 64
GRID_H = 36

# DEV_DIAG gate (Part A1 constant). Mirrors interaction_profile.DEV_DIAGNOSTICS
# but is duplicated here so this module has zero cross-module import cost on
# the production path - the gate is a single bool short-circuit.
_DEV_DIAG = os.environ.get("NAKSHA_DEV_DIAGNOSTICS", "") not in (
    "", "0", "false", "False", "off", "OFF",
)


# --------------------------------------------------------------------------- #
# Quality map cells                                                           #
# --------------------------------------------------------------------------- #
QUALITY_NO_DATA = 0   # no resident coverage in this cell = artifact hole
QUALITY_LOD0 = 1      # finest rung
QUALITY_LOD1 = 2
QUALITY_LOD2 = 3
QUALITY_LOD3 = 4
QUALITY_LOD4 = 5      # coarsest rung


def _dev_diag() -> bool:
    """Re-read the env var each call so tests can toggle it without reload."""
    return os.environ.get("NAKSHA_DEV_DIAGNOSTICS", "") not in (
        "", "0", "false", "False", "off", "OFF",
    )


@dataclass
class TileArtifactReport:
    """One frame's tile-artifact diagnostic snapshot."""

    frame_id: int = 0
    grid_w: int = GRID_W
    grid_h: int = GRID_H
    quality_map: np.ndarray = field(
        default_factory=lambda: np.zeros((GRID_H, GRID_W), np.uint8)
    )
    tile_artifact_region_count: int = 0
    hole_cells: int = 0
    total_source_cells: int = 0
    region_sizes: List[int] = field(default_factory=list)
    region_boxes: List[Tuple[int, int, int, int]] = field(
        default_factory=list)
    dev_diag: bool = False

    @property
    def hole_ratio(self) -> float:
        if self.total_source_cells <= 0:
            return 0.0
        return self.hole_cells / float(self.total_source_cells)

    def as_dict(self) -> dict:
        return {
            "frame_id": int(self.frame_id),
            "grid": f"{self.grid_w}x{self.grid_h}",
            "tile_artifact_region_count": int(self.tile_artifact_region_count),
            "hole_cells": int(self.hole_cells),
            "total_source_cells": int(self.total_source_cells),
            "hole_ratio": round(self.hole_ratio, 4),
            "region_sizes": list(self.region_sizes),
            "region_boxes": [tuple(v) for v in self.region_boxes],
            "dev_diag": bool(self.dev_diag),
        }


def connected_regions_4(grid: np.ndarray) -> List[List[Tuple[int, int]]]:
    """4-connected components of True cells in a boolean grid, largest first.

    Mirrors ``visible_parity.connected_regions`` but operates on the screen
    grid (64x36) instead of world-space cells, so it is O(GRID_W*GRID_H).
    """
    if grid is None or grid.size == 0:
        return []
    h, w = grid.shape
    visited = np.zeros((h, w), dtype=bool)
    regions: List[List[Tuple[int, int]]] = []
    for j in range(h):
        for i in range(w):
            if not grid[j, i] or visited[j, i]:
                continue
            stack = [(i, j)]
            visited[j, i] = True
            region: List[Tuple[int, int]] = []
            while stack:
                ci, cj = stack.pop()
                region.append((ci, cj))
                for ni, nj in ((ci + 1, cj), (ci - 1, cj),
                               (ci, cj + 1), (ci, cj - 1)):
                    if 0 <= ni < w and 0 <= nj < h:
                        if grid[nj, ni] and not visited[nj, ni]:
                            visited[nj, ni] = True
                            stack.append((ni, nj))
            if region:
                regions.append(region)
    regions.sort(key=len, reverse=True)
    return regions


class TileArtifactDiagnostics:
    """64x36 screen-quality map + tile-artifact-region counter (Part A1).

    Usage::

        diag = TileArtifactDiagnostics(manager)
        rep = diag.run(frame_id, viewport_w, viewport_h)

    The ``run`` method is guarded by ``_diag_guard("tile_artifact")`` on the
    manager so a failure here can never take down a frame.
    """

    def __init__(self, manager=None):
        self.manager = manager
        self._frame_id = 0
        self._last_report: Optional[TileArtifactReport] = None

    def run(self, frame_id: int, viewport_w: int = 0,
            viewport_h: int = 0) -> TileArtifactReport:
        """Build the quality map and count artifact regions for one frame."""
        self._frame_id = int(frame_id)
        m = self.manager
        rep = TileArtifactReport(frame_id=self._frame_id, dev_diag=_dev_diag())
        if m is None:
            self._last_report = rep
            return rep
        w = int(getattr(m, "_vp_w", viewport_w or 0) or 0)
        h = int(getattr(m, "_vp_h", viewport_h or 0) or 0)
        if w <= 0 or h <= 0:
            self._last_report = rep
            return rep
        idx = getattr(m, "idx", None)
        if idx is None:
            self._last_report = rep
            return rep
        quality = np.full((GRID_H, GRID_W), QUALITY_NO_DATA, np.uint8)
        src = self._source_cells(m, idx)
        rep.total_source_cells = len(src)
        for qi, qj, node_id, lod in src:
            if 0 <= qj < GRID_H and 0 <= qi < GRID_W:
                lvl = min(int(lod), 4)
                val = [QUALITY_LOD0, QUALITY_LOD1, QUALITY_LOD2,
                       QUALITY_LOD3, QUALITY_LOD4][lvl]
                if quality[qj, qi] < val:
                    quality[qj, qi] = val
        rep.quality_map = quality
        holes_mask = (quality == QUALITY_NO_DATA)
        rep.hole_cells = int(holes_mask.sum())
        regions = connected_regions_4(holes_mask)
        rep.tile_artifact_region_count = len(regions)
        rep.region_sizes = [len(r) for r in regions]
        rep.region_boxes = [self._bbox(r) for r in regions]
        self._last_report = rep
        if rep.dev_diag:
            self._print_dev_diag(rep)
        return rep

    @property
    def last_report(self) -> Optional[TileArtifactReport]:
        return self._last_report

    def _source_cells(self, manager, idx) -> List[Tuple[int, int, int, int]]:
        """Map every source-covered node to its screen-grid cell."""
        out: List[Tuple[int, int, int, int]] = []
        try:
            nodes = idx.nodes
            if nodes is None or nodes.size == 0:
                return out
            nmin = np.asarray(nodes["bounds_min"], dtype=np.float64)
            nmax = np.asarray(nodes["bounds_max"], dtype=np.float64)
            nids = np.asarray(nodes["node_id"], dtype=np.int64)
            lods = (np.asarray(nodes["lod"], dtype=np.int64)
                    if "lod" in nodes.dtype.names
                    else np.zeros(len(nids), np.int64))
        except Exception:
            return out
        vp = getattr(manager, "_viewport", None)
        if vp is None:
            return out
        try:
            cx, cy, hw, hh = (float(vp[0]), float(vp[1]),
                              float(vp[2]), float(vp[3]))
        except Exception:
            return out
        wv0, wv1 = cx - hw, cx + hw
        wv2, wv3 = cy - hh, cy + hh
        if wv1 <= wv0 or wv3 <= wv2:
            return out
        step_x = (wv1 - wv0) / float(GRID_W)
        step_y = (wv3 - wv2) / float(GRID_H)
        if step_x <= 0 or step_y <= 0:
            return out
        for r in range(nmin.shape[0]):
            nx0 = float(nmin[r, 0])
            ny0 = float(nmin[r, 1])
            nx1 = float(nmax[r, 0])
            ny1 = float(nmax[r, 1])
            gi0 = max(0, int(np.floor((nx0 - wv0) / step_x)))
            gi1 = min(GRID_W - 1, int(np.floor((nx1 - wv0) / step_x)))
            gj0 = max(0, int(np.floor((ny0 - wv2) / step_y)))
            gj1 = min(GRID_H - 1, int(np.floor((ny1 - wv2) / step_y)))
            if gi1 < gi0 or gj1 < gj0:
                continue
            nid = int(nids[r])
            lod = int(lods[r]) if r < lods.shape[0] else 0
            for gj in range(gj0, gj1 + 1):
                for gi in range(gi0, gi1 + 1):
                    out.append((gi, gj, nid, lod))
        return out

    @staticmethod
    def _bbox(region) -> Tuple[int, int, int, int]:
        if not region:
            return (0, 0, 0, 0)
        xs = [p[0] for p in region]
        ys = [p[1] for p in region]
        return (min(xs), min(ys), max(xs), max(ys))

    def _print_dev_diag(self, rep: TileArtifactReport) -> None:
        """DEV_DIAG reporting path (Part A1). One line, flushed."""
        try:
            print(
                f"[TILE ARTIFACT DIAG] frame={rep.frame_id} "
                f"grid={rep.grid_w}x{rep.grid_h} "
                f"regions={rep.tile_artifact_region_count} "
                f"holes={rep.hole_cells}/{rep.total_source_cells} "
                f"hole_ratio={rep.hole_ratio:.4f} "
                f"region_sizes={rep.region_sizes[:8]}",
                flush=True,
            )
        except Exception:
            pass


def dev_diagnostics_enabled() -> bool:
    """Cached single-source-of-truth for the DEV_DIAG gate."""
    v = os.environ.get("NAKSHA_DEV_DIAGNOSTICS", "")
    return v not in ("", "0", "false", "False", "off", "OFF")



