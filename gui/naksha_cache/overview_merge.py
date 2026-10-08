"""overview_merge.py - bounded, deterministic overview accumulation.

The legacy `build_overview` kept ~400 K points per source chunk in Python lists
and `np.concatenate`d them at the end. At 1.14 B points / 2 M-point chunks that
is ~570 chunks x 400 K = ~228 M points (~9 GB with attributes) - peak RAM
proportional to the dataset, which the out-of-core design forbids.

`BoundedOverviewReducer` keeps the pool under a FIXED cap:

  * each chunk contributes a deterministic strided subsample (<= per_chunk),
  * whenever the pool exceeds `cap`, it is reduced with the SAME voxel
    representative rule the LOD builder uses (`voxel_representative_ids`,
    highest-z per voxel, class mixed into the key) at a cell size that only ever
    GROWS, so every retained point is an actual source point,
  * `finish()` bisects the cell on the (bounded) pool to land near `target`.

Every retained row keeps its CANONICAL global point id (and class / intensity /
RGB), so overview points are real, addressable source points - the legacy
overview stored id 0 for all of them, colliding with the real point 0.

Determinism: the result is a pure function of (chunk order, parameters). No
randomness; ties inside a voxel are broken by `np.lexsort` order (index).
"""
from __future__ import annotations

import numpy as np

from .format import voxel_representative_ids


class BoundedOverviewReducer:
    def __init__(self, extent_xy, target: int = 300_000, cap: int = None,
                 per_chunk: int = 60_000, have_rgb: bool = False):
        self.target = int(target)
        self.cap = int(cap) if cap else max(4 * self.target, 1_000_000)
        self.per_chunk = int(per_chunk)
        self.have_rgb = bool(have_rgb)
        w, h = max(float(extent_xy[0]), 1e-3), max(float(extent_xy[1]), 1e-3)
        # Start fine enough that a uniform cloud would NOT be thinned below the
        # target, then only coarsen when the pool actually overflows.
        self.cell = float(np.sqrt(w * h / max(self.target * 8, 1)))
        self._parts = []         # list of tuples (xyz, cls, rgb, gid, inten)
        self._n = 0
        self.peak_points = 0
        self.reductions = 0
        self.points_seen = 0

    # ------------------------------------------------------------------ add
    def add(self, xyz, cls, rgb, gid, inten) -> None:
        n = int(xyz.shape[0])
        if n == 0:
            return
        self.points_seen += n
        stride = max(1, -(-n // self.per_chunk))
        sl = slice(0, n, stride)
        self._parts.append((np.array(xyz[sl], np.float64),
                            np.array(cls[sl]),
                            np.array(rgb[sl]) if (self.have_rgb
                                                  and rgb is not None) else None,
                            np.array(gid[sl], np.uint64),
                            np.array(inten[sl])))
        self._n += self._parts[-1][0].shape[0]
        self.peak_points = max(self.peak_points, self._n)
        if self._n > self.cap:
            self._reduce(self.cap // 2)

    # ------------------------------------------------------------- internals
    def _cat(self):
        xyz = np.concatenate([p[0] for p in self._parts])
        cls = np.concatenate([p[1] for p in self._parts])
        gid = np.concatenate([p[3] for p in self._parts])
        inten = np.concatenate([p[4] for p in self._parts])
        rgb = (np.concatenate([p[2] for p in self._parts])
               if self.have_rgb and self._parts[0][2] is not None else None)
        return xyz, cls, rgb, gid, inten

    def _store(self, xyz, cls, rgb, gid, inten, idx):
        self._parts = [(xyz[idx], cls[idx],
                        rgb[idx] if rgb is not None else None,
                        gid[idx], inten[idx])]
        self._n = int(idx.size)

    def _reduce(self, limit: int) -> None:
        xyz, cls, rgb, gid, inten = self._cat()
        cell = self.cell
        idx = voxel_representative_ids(xyz, cell, cls)
        while idx.size > limit:
            cell *= 1.4
            idx = voxel_representative_ids(xyz, cell, cls)
        self.cell = cell                       # monotonically non-decreasing
        self.reductions += 1
        self._store(xyz, cls, rgb, gid, inten, idx)

    # ---------------------------------------------------------------- finish
    def finish(self):
        """-> (xyz, cls, rgb|None, gid, inten) with ~`target` points."""
        if not self._parts:
            return None
        xyz, cls, rgb, gid, inten = self._cat()
        if xyz.shape[0] > self.target:
            lo, hi, best = 1e-3, 1e6, None
            for _ in range(24):
                mid = float(np.sqrt(lo * hi))
                idx = voxel_representative_ids(xyz, mid, cls)
                if idx.size > self.target:
                    lo = mid
                else:
                    hi = mid
                    best = idx
            if best is None:
                best = voxel_representative_ids(xyz, hi, cls)
            xyz, cls, gid, inten = xyz[best], cls[best], gid[best], inten[best]
            rgb = rgb[best] if rgb is not None else None
        order = np.argsort(gid, kind="stable")      # canonical, reproducible
        return (xyz[order], cls[order],
                rgb[order] if rgb is not None else None,
                gid[order], inten[order])
