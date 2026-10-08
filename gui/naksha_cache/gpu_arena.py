"""gpu_arena.py - stable-offset allocator for the native point arena.

The native renderer keeps ONE fixed-capacity point buffer per stream and a tile
is written at a caller-chosen offset (`nkv_upload_point_tile`). This module is
the caller's side of that contract: it hands out `[first, first+count)` slots,
takes them back when a tile is evicted, and coalesces neighbours so a long
pan/zoom session does not fragment the arena into unusable slivers.

A slot's offset NEVER changes while the tile is resident. That stability is the
whole point: draw ranges and every attribute stream (class / intensity / normal)
address the same canonical points, and a LOD change is a draw-range edit rather
than a re-pack.

Pure Python, no GPU access, so it is fully unit-testable.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple


class GpuArena:
    def __init__(self, capacity: int):
        self.capacity = int(capacity)
        self._free: List[Tuple[int, int]] = [(0, self.capacity)] if capacity > 0 else []
        self._used = 0
        self.alloc_count = 0
        self.free_count = 0

    # ---------------------------------------------------------------- stats
    @property
    def used(self) -> int:
        return self._used

    @property
    def free_points(self) -> int:
        return self.capacity - self._used

    @property
    def largest_free(self) -> int:
        return max((s for _f, s in self._free), default=0)

    @property
    def extent(self) -> int:
        """High-water: one past the last used point (what the GPU has written)."""
        if not self._free:
            return self.capacity
        last_first, last_size = self._free[-1]
        if last_first + last_size == self.capacity:
            return last_first
        return self.capacity

    def stats(self) -> Dict[str, int]:
        return {"capacity": self.capacity, "used": self._used,
                "free": self.free_points, "largest_free": self.largest_free,
                "free_runs": len(self._free), "extent": self.extent,
                "allocs": self.alloc_count, "frees": self.free_count}

    # ------------------------------------------------------------- allocate
    def can_alloc(self, count: int) -> bool:
        return int(count) > 0 and self.largest_free >= int(count)

    def alloc(self, count: int) -> Optional[int]:
        """Best-fit: the smallest free run that holds `count` (least waste)."""
        count = int(count)
        if count <= 0:
            return None
        best = None
        for i, (f, s) in enumerate(self._free):
            if s >= count and (best is None or s < self._free[best][1]):
                best = i
                if s == count:
                    break
        if best is None:
            return None
        f, s = self._free[best]
        if s == count:
            del self._free[best]
        else:
            self._free[best] = (f + count, s - count)
        self._used += count
        self.alloc_count += 1
        return f

    # ----------------------------------------------------------------- free
    def free(self, first: int, count: int) -> None:
        first, count = int(first), int(count)
        if count <= 0:
            return
        if first < 0 or first + count > self.capacity:
            raise ValueError(f"slot [{first},{first + count}) outside arena {self.capacity}")
        # insert keeping the list sorted, then coalesce with both neighbours
        runs = self._free
        i = 0
        while i < len(runs) and runs[i][0] < first:
            i += 1
        if i > 0 and runs[i - 1][0] + runs[i - 1][1] > first:
            raise ValueError("double free (overlaps the previous free run)")
        if i < len(runs) and first + count > runs[i][0]:
            raise ValueError("double free (overlaps the next free run)")
        runs.insert(i, (first, count))
        # merge with next
        if i + 1 < len(runs) and runs[i][0] + runs[i][1] == runs[i + 1][0]:
            runs[i] = (runs[i][0], runs[i][1] + runs[i + 1][1])
            del runs[i + 1]
        # merge with previous
        if i > 0 and runs[i - 1][0] + runs[i - 1][1] == runs[i][0]:
            runs[i - 1] = (runs[i - 1][0], runs[i - 1][1] + runs[i][1])
            del runs[i]
        self._used -= count
        self.free_count += 1

    def reset(self, capacity: Optional[int] = None) -> None:
        if capacity is not None:
            self.capacity = int(capacity)
        self._free = [(0, self.capacity)] if self.capacity > 0 else []
        self._used = 0
