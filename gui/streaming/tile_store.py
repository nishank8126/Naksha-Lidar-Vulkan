"""Tile decoding: quadtree runs -> GPU-ready point tiles.

This is the ONLY place that touches LAZ, and it runs on worker threads, never
on the render thread.

Measured on NT219 (NVIDIA T400 box):
  * LAZ random access = seek() + read_points(); a 38,000-point tile costs
    ~25 ms / 1.52 Mpts/s. That is the tile decode budget the scheduler is
    sized against - it is why tiles are ~38K points and not 5M.
  * Cost is proportional to the TILE, never to the dataset.

Attributes stay aligned with XYZ because every attribute is read from the same
structured slice and the same permutation is applied to all of them (Phase 17).
"""
import threading

import numpy as np

from .core import GPU_BYTES_PER_POINT

# Thread-local open readers. laspy readers are NOT thread-safe, and reopening a
# 2 GB LAZ per tile would cost more than the decode itself, so each worker keeps
# one reader per file it touches.
_local = threading.local()


def _readers():
    r = getattr(_local, "readers", None)
    if r is None:
        r = {}
        _local.readers = r
    return r


def close_readers():
    """Release per-thread LAZ handles (called on worker shutdown)."""
    for rd in list(_readers().values()):
        try:
            rd.close()
        except Exception:
            pass
    _readers().clear()


class TileReader:
    """Decodes index cells into packed, GPU-uploadable tiles."""

    def __init__(self, index, source_paths, origin=(0.0, 0.0, 0.0)):
        self.index = index
        self.source_paths = list(source_paths)
        # Local float32 coordinates are emitted relative to this origin. The
        # authoritative float64 coordinates are never mutated (Phase 16) - the
        # origin is a render-time offset, exactly like the existing floating
        # origin contract.
        self.origin = np.asarray(origin, dtype=np.float64)

    def file_for_id(self, fid):
        if fid < 0 or fid >= len(self.source_paths):
            raise IndexError(f"file_id {fid} out of range")
        return self.source_paths[fid]

    def _reader(self, path):
        readers = _readers()
        rd = readers.get(path)
        if rd is None:
            import laspy
            rd = laspy.open(path)
            readers[path] = rd
        return rd

    @staticmethod
    def _rgb8(values):
        """Normalise LAS RGB to 8 bits WITHOUT truncating.

        LAS 1.2 / point format 3 (which NT219 uses - measured) stores RGB as
        uint16. A naive astype(uint8) takes the value MODULO 256, so 49920
        becomes 0: every colour is destroyed and the cloud renders BLACK,
        silently, on a billion-point dataset. The correct 16->8 conversion is
        the high byte, which is what a full-range channel downscale means.
        """
        v = np.asarray(values)
        if v.dtype == np.uint8:
            return v
        return np.clip(v.astype(np.int64) >> 8, 0, 255).astype(np.uint8)

    def read_tile(self, node_row, max_points=None):
        """Decode one index cell into packed arrays.

        Returns None for an empty cell, otherwise a dict with:
          xyz            float32 Nx3, LOCAL to self.origin
          rgb            uint8   Nx3
          classification uint8   N
          intensity      float32 N
          gfile/gsource  uint32 / int64 - STABLE GLOBAL POINT IDENTITY
          world_xyz      float64 Nx3 (authoritative, un-mutated)
        """
        runs = self.index.node_runs(node_row)
        total = int(self.index.nodes["point_count"][node_row])
        if total <= 0:
            return None
        # A FIXED stride decimates for coarse LOD. Random subsampling clumps
        # and opens visible holes; a stride stays spatially uniform. It is
        # applied identically to every attribute below, preserving alignment.
        stride = 1
        if max_points is not None and total > max_points:
            stride = int(np.ceil(total / float(max_points)))

        X, Y, Z = [], [], []
        R, G, B = [], [], []
        C, I = [], []
        GF, GS = [], []
        seen = 0
        for run in runs:
            fid = int(run["file_id"])
            first = int(run["first_point"])
            count = int(run["count"])
            if count <= 0:
                continue
            rd = self._reader(self.file_for_id(fid))
            rd.seek(first)
            sub = rd.read_points(count)
            if stride > 1:
                sub = sub[::stride]
            m = len(sub)
            if m == 0:
                continue
            X.append(np.asarray(sub.x, dtype=np.float64))
            Y.append(np.asarray(sub.y, dtype=np.float64))
            Z.append(np.asarray(sub.z, dtype=np.float64))
            n_rgb = count // stride + (1 if first % stride else 0)
            for name, sink in (("red", R), ("green", G), ("blue", B)):
                v = getattr(sub, name, None)
                sink.append(np.zeros(m, dtype=np.uint8) if v is None
                            else self._rgb8(v))
            C.append(np.asarray(getattr(sub, "classification", np.zeros(m)),
                                dtype=np.uint8))
            I.append(np.asarray(getattr(sub, "intensity", np.zeros(m)),
                                dtype=np.float32))
            # Stable identity = file_id + source point index. A classification
            # edit resolves against this without loading anything else.
            GF.append(np.full(m, fid, dtype=np.uint32))
            GS.append(first + np.arange(0, count, stride, dtype=np.int64)[:m])
            seen += m

        if not X:
            return None
        wx = np.concatenate(X)
        wy = np.concatenate(Y)
        wz = np.concatenate(Z)
        world = np.column_stack((wx, wy, wz))
        local = (world - self.origin).astype(np.float32)
        return {
            "xyz": np.ascontiguousarray(local),
            "rgb": np.ascontiguousarray(
                np.column_stack((np.concatenate(R), np.concatenate(G),
                                 np.concatenate(B)))),
            "classification": np.concatenate(C),
            "intensity": np.concatenate(I),
            "gfile": np.concatenate(GF),
            "gsource": np.concatenate(GS),
            "world_xyz": world,
            "stride": stride,
            "count": seen,
            "gpu_bytes": seen * GPU_BYTES_PER_POINT,
        }