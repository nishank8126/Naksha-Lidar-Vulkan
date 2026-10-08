"""Sparse classification edit overlay for streaming datasets (Phase 19).

Rewriting a 2 GB LAZ to change one classification is not viable, and is not
necessary: a classification edit is a SPARSE MAP from a stable global point id
to a new class, applied whenever the owning tile enters RAM.

    global point id = (file_id, source_point_index)

Those two values are produced by the decoder for every point, so an edit
resolves without loading the dataset. Undo/redo operates on this map, so it
costs nothing regardless of dataset size.
"""
import json
import os
import threading

import numpy as np


class EditLayer:
    """Sparse (global point id -> new class) with undo/redo."""

    def __init__(self, path=None):
        self.path = path
        self._lock = threading.RLock()
        # key: (file_id << 42) | source_point_index   -> new class
        self._edits = {}
        self._undo = []
        self._redo = []
        self.applied_count = 0

    @staticmethod
    def key(file_id, source_index):
        # 42 bits of source index covers 4.4 trillion points in one file, which
        # is far beyond any real LAS, and keeps the key a single int64.
        return (int(file_id) << 42) | (int(source_index) & ((1 << 42) - 1))

    def set_class(self, file_id, source_index, new_class):
        k = self.key(file_id, source_index)
        with self._lock:
            prev = self._edits.get(k)
            if prev == new_class:
                return False
            self._undo.append((k, prev))
            self._redo.clear()
            self._edits[k] = int(new_class)
        return True

    def bulk_set(self, keys, new_class):
        """Apply one class to many ids at once (a brush stroke)."""
        changed = 0
        with self._lock:
            for k in keys:
                prev = self._edits.get(k)
                if prev == new_class:
                    continue
                self._undo.append((k, prev))
                self._edits[k] = int(new_class)
                changed += 1
            if changed:
                self._redo.clear()
        return changed

    def undo(self):
        """Revert the most recent edit.

        The stack stores (key, previous_value). Restoring it and pushing the
        now-current value onto the redo stack keeps undo and redo symmetric
        without copying the dataset.
        """
        with self._lock:
            if not self._undo:
                return 0
            k, prev = self._undo.pop()
            cur = self._edits.get(k)
            self._redo.append((k, cur))
            if prev is None:
                self._edits.pop(k, None)
            else:
                self._edits[k] = prev
            return 1

    def redo(self):
        with self._lock:
            if not self._redo:
                return 0
            k, val = self._redo.pop()
            prev = self._edits.get(k)
            self._undo.append((k, prev))
            if val is None:
                self._edits.pop(k, None)
            else:
                self._edits[k] = val
            return 1

    def apply_to_tile(self, tile):
        """Overlay pending edits onto a freshly decoded tile, in place.

        Returns the number of points actually changed. Cost is O(points in THIS
        TILE), never O(dataset) - which is the whole point of the overlay.
        """
        if not self._edits:
            return 0
        with self._lock:
            if not self._edits:
                return 0
            keys = ((tile["gfile"].astype(np.int64) << 42)
                    | (tile["gsource"] & ((1 << 42) - 1)))
            uniq, inv = np.unique(keys, return_inverse=True)
            lut = np.full(uniq.size, -1, dtype=np.int16)
            for i, k in enumerate(uniq):
                v = self._edits.get(int(k))
                if v is not None:
                    lut[i] = v
            hit = lut[inv]
            mask = hit >= 0
            n = int(mask.sum())
            if n:
                tile["classification"][mask] = hit[mask].astype(np.uint8)
                self.applied_count += n
            return n

    def size(self):
        with self._lock:
            return len(self._edits)

    def save(self, path=None):
        p = path or self.path
        if not p:
            return None
        tmp = p + ".tmp"
        with self._lock:
            data = {str(k): v for k, v in self._edits.items()}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, p)
        return p

    def load(self, path=None):
        p = path or self.path
        if not p or not os.path.isfile(p):
            return 0
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        with self._lock:
            self._edits = {int(k): int(v) for k, v in data.items()}
            self._undo.clear()
            self._redo.clear()
        return len(self._edits)