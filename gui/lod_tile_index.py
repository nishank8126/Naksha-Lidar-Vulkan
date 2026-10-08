"""
Screen-space LOD groundwork: a spatial tile index over a LiDAR point cloud.

DESIGN / SCOPE NOTE
-------------------
This module builds the hierarchy and the visibility selection. It does NOT
change how the renderer draws. The draw path still issues a single full-buffer
vkCmdDraw over every resident point; point shader, point size, colours and
classification appearance are untouched.

That split is deliberate. A screen-space LOD changes WHICH points reach the
GPU, so its only real acceptance criterion is visual parity - and parity
cannot be established without comparing rendered output. Building and
measuring the index first gives evidence for whether LOD is worth wiring in,
without putting a working, parity-passing renderer at risk.

Why a uniform XY grid rather than an octree
-------------------------------------------
LiDAR is a single sweeping pass: density is near-uniform in XY and extremely
sparse in Z (tens of metres across a kilometres-wide footprint). An octree
would split Z until nearly every leaf is one point - a huge node count for no
selection benefit. A uniform XY grid yields spatially CONTIGUOUS point
ranges, which is what an indexed/multi-draw actually needs to exploit the
selection.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np


@dataclass
class TileIndex:
    """Uniform XY grid over a point cloud.

    CSR-style: points of cell c occupy perm[cell_start[c]:cell_start[c+1]],
    and `order` indexes back into the original arrays. Selection therefore
    yields CONTIGUOUS RANGES, not scattered indices.
    """

    origin_xy: np.ndarray
    cell_size: float
    nx: int
    ny: int
    order: np.ndarray
    cell_start: np.ndarray
    cell_min_z: np.ndarray
    cell_max_z: np.ndarray
    total_points: int
    build_ms: float = 0.0
    meta: dict = field(default_factory=dict)

    @property
    def n_cells(self) -> int:
        return int(self.nx * self.ny)

    def cell_bounds(self, cx: int, cy: int):
        x0 = float(self.origin_xy[0]) + cx * self.cell_size
        y0 = float(self.origin_xy[1]) + cy * self.cell_size
        return x0, y0, x0 + self.cell_size, y0 + self.cell_size

    def cell_indices(self, cx: int, cy: int) -> np.ndarray:
        if cx < 0 or cy < 0 or cx >= self.nx or cy >= self.ny:
            return np.empty(0, dtype=np.int64)
        c = cy * self.nx + cx
        s = int(self.cell_start[c]); e = int(self.cell_start[c + 1])
        return self.order[s:e]

    def cell_ranges(self, cx: int, cy: int):
        if cx < 0 or cy < 0 or cx >= self.nx or cy >= self.ny:
            return 0, 0
        c = cy * self.nx + cx
        return int(self.cell_start[c]), int(self.cell_start[c + 1])

    def summary(self) -> str:
        per = self.total_points / max(1, self.n_cells)
        return (f"{self.total_points:,} pts / {self.n_cells} cells "
                f"({self.nx}x{self.ny}, cell={self.cell_size:.2f} m, "
                f"~{per:,.0f} pts/cell, built in {self.build_ms:.0f} ms)")

    def visible_cells(self, cam_xy, radius_m: float):
        """Cells whose XY rect lies within radius_m of a camera XY point.

        Screen-space proxy: keep a cell when its ground footprint can still
        cover at least one pixel. Deliberately CONSERVATIVE - it
        over-selects, so it can never punch a hole in the display.
        """
        cx0, cy0 = float(cam_xy[0]), float(cam_xy[1])
        r = float(radius_m)
        gx0 = int(np.floor((cx0 - r - self.origin_xy[0]) / self.cell_size))
        gx1 = int(np.floor((cx0 + r - self.origin_xy[0]) / self.cell_size))
        gy0 = int(np.floor((cy0 - r - self.origin_xy[1]) / self.cell_size))
        gy1 = int(np.floor((cy0 + r - self.origin_xy[1]) / self.cell_size))
        gx0, gx1 = max(0, gx0), min(self.nx - 1, gx1)
        gy0, gy1 = max(0, gy0), min(self.ny - 1, gy1)
        if gx1 < gx0 or gy1 < gy0:
            return []
        return [(cx, cy) for cy in range(gy0, gy1 + 1)
                for cx in range(gx0, gx1 + 1)]

    def select(self, cam_xy, radius_m: float):
        """(visible_point_indices, visible_cell_count, visible_point_count).

        Indices refer to the ORIGINAL arrays. Because cells are contiguous
        in `order`, a renderer can draw one range per visible cell.
        """
        cells = self.visible_cells(cam_xy, radius_m)
        chunks = [self.order[s:e] for (s, e) in
                  (self.cell_ranges(cx, cy) for (cx, cy) in cells)
                  if e > s]
        if not chunks:
            return np.empty(0, dtype=np.int64), 0, 0
        idx = chunks[0] if len(chunks) == 1 else np.concatenate(chunks)
        return idx, len(cells), int(idx.size)


def build_tile_index(xyz: np.ndarray, target_cell_points: int = 4096,
                     max_cells: int = 262_144) -> TileIndex:
    """Build a uniform XY tile index. Vectorised: one stable sort, no per-point loop."""
    t0 = time.perf_counter()
    xyz = np.asarray(xyz)
    n = int(xyz.shape[0])
    if n == 0:
        return TileIndex(np.zeros(2, np.float32), 1.0, 1, 1,
                         np.empty(0, np.int64), np.zeros(1, np.int64),
                         np.zeros(1, np.float32), np.zeros(1, np.float32), 0)

    x0 = float(xyz[:, 0].min()); x1 = float(xyz[:, 0].max())
    y0 = float(xyz[:, 1].min()); y1 = float(xyz[:, 1].max())
    span_x = max(x1 - x0, 1e-6)
    span_y = max(y1 - y0, 1e-6)

    cells = int(np.clip(round(n / max(1, target_cell_points)), 1, max_cells))
    aspect = span_x / span_y
    ny = int(np.clip(round(np.sqrt(cells / max(aspect, 1e-6))), 1, max_cells))
    nx = int(np.clip(round(cells / ny), 1, max_cells))
    cell = max(span_x / nx, span_y / ny, 1e-6)

    ix = np.clip(((xyz[:, 0] - x0) / cell).astype(np.int64), 0, nx - 1)
    iy = np.clip(((xyz[:, 1] - y0) / cell).astype(np.int64), 0, ny - 1)
    key = iy * nx + ix

    # Stable sort by cell id -> contiguous ranges, original order preserved
    # inside each cell (deterministic, so a selection is reproducible).
    order = np.argsort(key, kind="stable").astype(np.int64)

    counts = np.bincount(key, minlength=nx * ny).astype(np.int64)
    cell_start = np.zeros(nx * ny + 1, dtype=np.int64)
    np.cumsum(counts, out=cell_start[1:])

    z = np.asarray(xyz[:, 2], dtype=np.float32)
    cell_min_z = np.full(nx * ny, np.inf, dtype=np.float32)
    cell_max_z = np.full(nx * ny, -np.inf, dtype=np.float32)
    idx = np.flatnonzero(counts > 0)
    if idx.size:
        zs = z[order]
        cell_min_z[idx] = np.minimum.reduceat(zs, cell_start[idx])
        cell_max_z[idx] = np.maximum.reduceat(zs, cell_start[idx])

    return TileIndex(
        origin_xy=np.array([x0, y0], dtype=np.float32),
        cell_size=float(cell), nx=int(nx), ny=int(ny),
        order=order, cell_start=cell_start,
        cell_min_z=cell_min_z, cell_max_z=cell_max_z,
        total_points=n, build_ms=(time.perf_counter() - t0) * 1000.0,
        meta={"span_x": span_x, "span_y": span_y,
              "z_range": float(z.max() - z.min()) if n else 0.0},
    )
