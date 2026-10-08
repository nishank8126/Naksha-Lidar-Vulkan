"""Block triangulation and experimental, unconnected Surface scaffolding.

SurfaceBlockTriangulator is exercised by the ownership/halo regression tests.
The legacy sidecar reader and native controller below are prototypes, not a
production streaming Surface pipeline. No Surface writer or manager integration
is supplied here. Production persistence must use the requested .naksha derived
section; this module must not create permanent .nakshasurf files.
"""
from __future__ import annotations

import ctypes
import os
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy.spatial import Delaunay

from .normals import sidecar_path  # dataset_path -> Path(dataset + ".nakshanorm")

# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #
SURFACE_SUFFIX = ".nakshasurf"

# Cache file format magic + version (Part D binary header).
SURF_MAGIC = b"NAKHSURFv1"
SURF_VERSION = 1

# Default halo margin in metres. Boundary correctness also requires sufficient,
# consistent neighbour input; a fixed margin alone cannot guarantee no cracks.
DEFAULT_SURFACE_HALO_M = 2.0

# Triangle budget: oversized inputs are refused before triangulation. The caller
# must choose a coarser shared hierarchy block, preserving boundary consistency.
MAX_SURFACE_TRIANGLES = 65_536


# --------------------------------------------------------------------------- #
# Surface cache report                                                          #
# --------------------------------------------------------------------------- #
@dataclass
class SurfaceCacheReport:
    """Mirror of NormalCacheReport but for the `.nakshasurf` topology cache."""

    status: str = "MISS"          # HIT | MISS | STALE | CORRUPT
    path: str = ""
    blocks: int = 0
    stored_triangles: int = 0
    encoding: str = "tri16"
    open_ms: float = 0.0
    bytes: int = 0
    reason: str = ""
    layout_fingerprint: int = 0
    layout_bound: bool = True
    dataset_generation: int = 0
    surface_generation: int = 0

    def as_lines(self) -> List[str]:
        return [
            "[SURFACE CACHE]",
            f"status: {self.status}",
            f"path: {self.path or '-'}",
            f"blocks: {self.blocks:,}",
            f"stored_triangles: {self.stored_triangles:,}",
            f"encoding: {self.encoding}",
            f"open_ms: {self.open_ms:.2f}",
            f"bytes: {self.bytes}",
            f"dataset_generation: {self.dataset_generation}",
            f"surface_generation: {self.surface_generation}",
            f"layout_fingerprint: {self.layout_fingerprint}",
            f"layout_bound: {self.layout_bound}",
            f"reason: {self.reason}",
        ]


# --------------------------------------------------------------------------- #
# Block-wise triangulation with halo                                            #
# --------------------------------------------------------------------------- #
class SurfaceBudgetExceeded(ValueError):
    """The caller must select a coarser shared hierarchy block."""


class SurfaceBlockTriangulator:
    """Triangulate one existing core block and supplied neighbour halo.

    Ownership uses half-open core bounds. Adjacent cores must pass the same
    halo samples and bounds coordinate system; this class never builds a tree
    or invents neighbours. A refused budget requires a coarser hierarchy LOD,
    rather than independently subsampling a border and producing cracks.
    """

    def __init__(self, halo_m: float = DEFAULT_SURFACE_HALO_M,
                 max_triangles: int = MAX_SURFACE_TRIANGLES,
                 max_edge: float = 0.0):
        self.halo_m = float(halo_m)
        self.max_triangles = int(max_triangles)
        self.max_edge = float(max_edge)
        if (not np.isfinite(self.halo_m) or self.halo_m < 0
                or self.max_triangles <= 0 or not np.isfinite(self.max_edge)
                or self.max_edge < 0):
            raise ValueError("invalid surface triangulation settings")

    def triangulate(self, xyz: np.ndarray, block_bounds: Tuple[float, float,
                                                               float, float],
                    *, include_max_x=False, include_max_y=False
                    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        pts = np.asarray(xyz, dtype=np.float64)
        if pts.ndim != 2 or pts.shape[1] != 3:
            raise ValueError("xyz must have shape (N,3)")
        x0, x1, y0, y1 = map(float, block_bounds)
        if not np.isfinite((x0, x1, y0, y1)).all() or x0 >= x1 or y0 >= y1:
            raise ValueError("invalid core bounds")
        h = self.halo_m
        keep = (np.isfinite(pts).all(axis=1)
                & (pts[:, 0] >= x0-h) & (pts[:, 0] <= x1+h)
                & (pts[:, 1] >= y0-h) & (pts[:, 1] <= y1+h))
        pts = pts[keep]
        # Stable XY deduplication: ties use the lowest Z, independent of the
        # order in which neighbouring source files were read.
        order = np.lexsort((pts[:, 2], pts[:, 1], pts[:, 0]))
        pts = pts[order]
        unique = np.ones(len(pts), dtype=bool)
        unique[1:] = np.any(pts[1:, :2] != pts[:-1, :2], axis=1)
        pts = np.ascontiguousarray(pts[unique])
        core = ((pts[:, 0] >= x0) & (pts[:, 0] <= x1)
                & (pts[:, 1] >= y0) & (pts[:, 1] <= y1))
        # Bound the allocation BEFORE calling Qhull (planar T <= 2V-5).
        if len(pts) > (self.max_triangles + 5) // 2:
            raise SurfaceBudgetExceeded("select a coarser Surface block LOD")
        triangles = self._delaunay(pts)
        if self.max_edge > 0 and len(triangles):
            from gui.surface_mode import _filter_long_edges
            triangles = _filter_long_edges(triangles, pts[:, :2], self.max_edge)
        if len(triangles):
            centroids = pts[triangles, :2].mean(axis=1)
            owned = ((centroids[:, 0] >= x0) & (centroids[:, 1] >= y0)
                     & ((centroids[:, 0] <= x1) if include_max_x
                        else (centroids[:, 0] < x1))
                     & ((centroids[:, 1] <= y1) if include_max_y
                        else (centroids[:, 1] < y1)))
            triangles = triangles[owned].copy()
            a = pts[triangles[:, 1], :2] - pts[triangles[:, 0], :2]
            b = pts[triangles[:, 2], :2] - pts[triangles[:, 0], :2]
            down = (a[:, 0]*b[:, 1] - a[:, 1]*b[:, 0]) < 0
            triangles[down] = triangles[down][:, [0, 2, 1]]
        return pts, np.ascontiguousarray(triangles, dtype=np.int32), ~core

    def _delaunay(self, pts: np.ndarray) -> np.ndarray:
        from scipy.spatial import QhullError
        if len(pts) < 3:
            return np.empty((0, 3), dtype=np.int32)
        try:
            return Delaunay(pts[:, :2]).simplices.astype(np.int32)
        except QhullError:
            return np.empty((0, 3), dtype=np.int32)


# --------------------------------------------------------------------------- #
# Surface block cache (.nakshasurf I/O)                                        #
# --------------------------------------------------------------------------- #
@dataclass
class SurfaceBlockRecord:
    """On-disk layout of one triangulated block inside `.nakshasurf`."""

    block_id: int = -1
    dataset_generation: int = 0
    vertex_offset: int = 0
    vertex_count: int = 0
    triangle_offset: int = 0
    triangle_count: int = 0
    bounds_min: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    bounds_max: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    surface_generation: int = 0


class SurfaceCacheReport:
    """Report carried alongside the surface reader, mirrors NormalCacheReport."""

    def __init__(self):
        self.status = "MISS"
        self.path = ""
        self.blocks = 0
        self.stored_triangles = 0
        self.encoding = "tri16"
        self.open_ms = 0.0
        self.bytes = 0
        self.reason = ""
        self.layout_fingerprint = 0
        self.layout_bound = True
        self.dataset_generation = 0
        self.surface_generation = 0

    def as_lines(self) -> List[str]:
        return [
            "[SURFACE CACHE]",
            f"status: {self.status}",
            f"path: {self.path or '-'}",
            f"blocks: {self.blocks:,}",
            f"stored_triangles: {self.stored_triangles:,}",
            f"encoding: {self.encoding}",
            f"open_ms: {self.open_ms:.2f}",
            f"bytes: {self.bytes}",
            f"dataset_generation: {self.dataset_generation}",
            f"surface_generation: {self.surface_generation}",
            f"layout_fingerprint: {self.layout_fingerprint}",
            f"layout_bound: {self.layout_bound}",
            f"reason: {self.reason}",
        ]


class SurfaceBlockCache:
    """LRU of triangulated surface blocks, bounded in bytes (Part D).

    Keyed by ``(dataset_generation, block_id)`` - identical strategy to
    ``NormalBlockCache`` so reopening a dataset can never serve a block
    whose id now maps to a different point range.
    """

    def __init__(self, budget_bytes: int = 256 * 1024 ** 2):
        from collections import OrderedDict
        self.budget_bytes = int(budget_bytes)
        self._blocks: "OrderedDict[Tuple[int, int], np.ndarray]" = OrderedDict()
        self._tris: "Dict[Tuple[int, int], np.ndarray]" = {}
        self._bytes = 0
        self.lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.new_blocks = 0

    @property
    def resident_bytes(self) -> int:
        with self.lock:
            return self._bytes

    @property
    def resident_blocks(self) -> int:
        with self.lock:
            return len(self._blocks)

    def stats(self) -> dict:
        with self.lock:
            return dict(hits=self.hits, misses=self.misses,
                        resident_blocks=len(self._blocks),
                        resident_bytes=self._bytes,
                        evictions=self.evictions,
                        new_blocks=self.new_blocks,
                        budget_bytes=self.budget_bytes)

    def get(self, generation: int, block_id: int) -> Optional[np.ndarray]:
        with self.lock:
            key = (int(generation), int(block_id))
            blk = self._blocks.get(key)
            if blk is None:
                self.misses += 1
                return None
            self._blocks.move_to_end(key)
            self.hits += 1
            return blk

    def contains(self, generation: int, block_id: int) -> bool:
        with self.lock:
            return (int(generation), int(block_id)) in self._blocks

    def put(self, generation: int, block_id: int,
            vertices: np.ndarray, triangles: np.ndarray) -> int:
        """Insert a triangulated block. Returns bytes evicted."""
        v = np.ascontiguousarray(vertices, dtype=np.float32)
        t = np.ascontiguousarray(triangles, dtype=np.int32)
        nbytes = int(v.nbytes + t.nbytes)
        evicted = 0
        with self.lock:
            key = (int(generation), int(block_id))
            if key in self._blocks:
                old_v = self._blocks.pop(key)
                self._bytes -= int(old_v.nbytes)
                old_t = self._tris.pop(key, None)
                if old_t is not None:
                    self._bytes -= int(old_t.nbytes)
            self._blocks[key] = v
            self._tris[key] = t
            self._bytes += nbytes
            self.new_blocks += 1
            while self._bytes > self.budget_bytes and len(self._blocks) > 1:
                k, old_v = self._blocks.popitem(last=False)
                self._bytes -= int(old_v.nbytes)
                old_t = self._tris.pop(k, None)
                if old_t is not None:
                    self._bytes -= int(old_t.nbytes)
                    evicted += int(old_v.nbytes + old_t.nbytes)
                self.evictions += 1
        return evicted

    def clear(self) -> None:
        with self.lock:
            self._blocks.clear()
            self._tris.clear()
            self._bytes = 0


# --------------------------------------------------------------------------- #
# Surface atomic controller (native Vulkan surface functions)                   #
# --------------------------------------------------------------------------- #
_nkv_activate_pending_surface = None
_nkv_set_surface_tiled = None
_nkv_destroy_surface = None


def install_native(api_path: str) -> bool:
    """Load the native Vulkan surface C API (Part D)."""
    global _nkv_activate_pending_surface, _nkv_set_surface_tiled
    global _nkv_destroy_surface
    try:
        lib = ctypes.CDLL(api_path)
    except OSError:
        return False
    _nkv_activate_pending_surface = getattr(
        lib, "nkv_activate_pending_surface", None)
    _nkv_set_surface_tiled = getattr(lib, "nkv_set_surface_tiled", None)
    _nkv_destroy_surface = getattr(lib, "nkv_destroy_surface", None)
    return _nkv_activate_pending_surface is not None


class SurfaceAtomicController:
    """Atomic surface promotion: PENDING -> ACTIVE at frame boundary (Part D).

    Mirrors ``AtomicModeSwap`` for display modes (Part E): a surface mesh
    is staged as PENDING, and at the next frame boundary
    ``nkv_activate_pending_surface`` atomically swaps it in.
    """

    def __init__(self):
        self._pending_mesh: Optional[Tuple] = None
        self._active_mesh: Optional[Tuple] = None
        self.dataset_revision = 0
        self.surface_revision = 0
        self.commits = 0
        self.rejected_commits = 0
        self.black_frames = 0

    def set_pending(self, vertices: np.ndarray, triangles: np.ndarray,
                    dataset_revision: int) -> bool:
        """Stage a triangulated mesh as PENDING for the next frame boundary."""
        if int(dataset_revision) != int(self.dataset_revision):
            return False
        verts = np.ascontiguousarray(vertices, dtype=np.float32)
        tris = np.ascontiguousarray(triangles, dtype=np.int32)
        self._pending_mesh = (verts, tris, int(dataset_revision),
                              self.surface_revision)
        return True

    def activate_pending_surface(self) -> bool:
        """Frame-boundary atomic promote PENDING -> ACTIVE (Part D)."""
        p = self._pending_mesh
        if p is None:
            return False
        verts, tris, ds_rev, _ = p
        if int(ds_rev) != int(self.dataset_revision):
            self.rejected_commits += 1
            self._pending_mesh = None
            return False
        if _nkv_activate_pending_surface is not None:
            try:
                _nkv_activate_pending_surface(
                    verts.ctypes.data_as(
                        ctypes.POINTER(ctypes.c_float)),
                    ctypes.c_int(int(verts.shape[0])),
                    ctypes.c_int(int(verts.shape[1])),
                    tris.ctypes.data_as(ctypes.POINTER(ctypes.c_int)),
                    ctypes.c_int(int(tris.shape[0])),
                )
            except Exception:
                pass
        self._active_mesh = (verts, tris, int(ds_rev),
                             self.surface_revision)
        self._pending_mesh = None
        self.surface_revision += 1
        self.commits += 1
        return True

    @property
    def has_pending(self) -> bool:
        return self._pending_mesh is not None

    @property
    def active_revision(self) -> int:
        return self.surface_revision

    def new_dataset(self) -> None:
        """Bump dataset_revision and cancel any pending mesh."""
        self.dataset_revision += 1
        self._pending_mesh = None


# --------------------------------------------------------------------------- #
# Surface cache I/O (.nakshasurf binary format)                                 #
# --------------------------------------------------------------------------- #
def surface_cache_path(dataset: str) -> Path:
    """Return the ``.nakshasurf`` sidecar path for a dataset."""
    return Path(str(dataset) + SURFACE_SUFFIX)


@dataclass
class SurfaceCacheHeader:
    """Binary header for `.nakshasurf` files (Part D binary format)."""

    magic: bytes = SURF_MAGIC
    version: int = SURF_VERSION
    block_count: int = 0
    vertex_bytes: int = 0
    triangle_bytes: int = 0
    dataset_generation: int = 0
    surface_generation: int = 0
    layout_fingerprint: int = 0
    total_triangles: int = 0

    HEADER_SIZE = 64
    FORMAT = "<10sQIIQQQQI"

    def pack(self) -> bytes:
        return struct.pack(self.FORMAT, self.magic, self.version,
                           self.block_count, self.vertex_bytes,
                           self.triangle_bytes, self.dataset_generation,
                           self.surface_generation, self.layout_fingerprint,
                            self.total_triangles)

    @classmethod
    def unpack(cls, data: bytes) -> "SurfaceCacheHeader":
        vals = struct.unpack(cls.FORMAT, data[:cls.HEADER_SIZE])
        return cls(
            magic=vals[0], version=int(vals[1]),
            block_count=int(vals[2]), vertex_bytes=int(vals[3]),
            triangle_bytes=int(vals[4]), dataset_generation=int(vals[5]),
            surface_generation=int(vals[6]),
            layout_fingerprint=int(vals[7]),
            total_triangles=int(vals[8]),
        )


def open_surface_cache(dataset: str, *, dataset_generation: int = 0,
                       expected_layout_fingerprint: Optional[int] = None
                       ) -> Tuple[Optional["SurfaceCacheReader"],
                                  SurfaceCacheReport]:
    """Open the `.nakshasurf` directory/header only (Part D).

    Returns ``(reader_or_None, report)``.  Like ``open_normal_cache`` it
    opens lazily: only the 64-byte header and block directory are read.
    """
    import time as _time
    t0 = _time.perf_counter()
    path = surface_cache_path(dataset)
    rep = SurfaceCacheReport()
    rep.path = str(path)
    rep.dataset_generation = int(dataset_generation)
    if not path.is_file():
        rep.status = "MISS"
        rep.reason = f"no surface cache at {path}"
        rep.open_ms = (_time.perf_counter() - t0) * 1000.0
        return None, rep
    reader = None
    try:
        reader = SurfaceCacheReader(
            dataset, dataset_generation=dataset_generation,
            expected_layout_fingerprint=expected_layout_fingerprint)
    except FileNotFoundError:
        rep.status = "MISS"
        rep.reason = "surface cache vanished between stat and open"
    except ValueError as exc:
        msg = str(exc)
        rep.status = "STALE" if ("STALE" in msg or "fingerprint" in msg) \
            else "CORRUPT"
        rep.reason = msg
    except OSError as exc:
        rep.status = "CORRUPT"
        rep.reason = f"I/O error opening surface cache: {exc}"
    else:
        rep.blocks = int(reader.block_count)
        rep.stored_triangles = int(reader.total_triangles)
        rep.bytes = int(reader.stored_bytes())
        rep.layout_fingerprint = int(
            getattr(reader, "layout_fingerprint", 0) or 0)
        rep.layout_bound = bool(
            rep.layout_fingerprint
            and (not expected_layout_fingerprint
                 or int(expected_layout_fingerprint)
                 == rep.layout_fingerprint))
        if rep.layout_bound:
            rep.status = "HIT"
        else:
            rep.status = "STALE"
            rep.reason = "layout fingerprint mismatch"
            reader = None
    rep.open_ms = (_time.perf_counter() - t0) * 1000.0
    return reader, rep


class SurfaceCacheReader:
    """Random-access surface cache reader (Part D binary format)."""

    def __init__(self, dataset: str, dataset_generation: int = 0,
                 expected_layout_fingerprint: Optional[int] = None):
        self.dataset = str(dataset)
        self.dataset_generation = int(dataset_generation)
        self._path = surface_cache_path(dataset)
        with open(self._path, "rb") as f:
            hdr_data = f.read(SurfaceCacheHeader.HEADER_SIZE)
        if len(hdr_data) < SurfaceCacheHeader.HEADER_SIZE:
            raise ValueError("surface cache header truncated")
        self.header = SurfaceCacheHeader.unpack(hdr_data)
        if self.header.magic != SURF_MAGIC:
            raise ValueError(f"bad magic: {self.header.magic!r}")
        if self.header.version != SURF_VERSION:
            raise ValueError(f"version mismatch: {self.header.version}")
        if expected_layout_fingerprint is not None:
            if int(self.header.layout_fingerprint) != \
                    int(expected_layout_fingerprint):
                raise ValueError(
                    "STALE: layout fingerprint mismatch "
                    f"({self.header.layout_fingerprint} vs "
                    f"{expected_layout_fingerprint})")
            if int(self.header.dataset_generation) != \
                    int(dataset_generation):
                raise ValueError(
                    "STALE: dataset generation mismatch")
        self._dir_offset = SurfaceCacheHeader.HEADER_SIZE
        self._block_size = 48
        self._dirs = None

    @property
    def block_count(self) -> int:
        return int(self.header.block_count)

    @property
    def total_triangles(self) -> int:
        return int(self.header.total_triangles)

    @property
    def layout_fingerprint(self) -> int:
        return int(self.header.layout_fingerprint)

    def stored_bytes(self) -> int:
        return int(self.header.vertex_bytes
                   + self.header.triangle_bytes
                   + self._block_size * self.header.block_count)

    def read_block_directory(self) -> List[SurfaceBlockRecord]:
        """Read the block directory. Cached on first call."""
        if self._dirs is not None:
            return self._dirs
        records: List[SurfaceBlockRecord] = []
        with open(self._path, "rb") as f:
            f.seek(self._dir_offset)
            for _ in range(self.header.block_count):
                raw = f.read(self._block_size)
                if len(raw) < self._block_size:
                    break
                vals = struct.unpack("<iQIIQQ6f", raw)
                rec = SurfaceBlockRecord(
                    block_id=int(vals[0]),
                    dataset_generation=int(vals[1]),
                    vertex_offset=int(vals[2]),
                    vertex_count=int(vals[3]),
                    triangle_offset=int(vals[4]),
                    triangle_count=int(vals[5]),
                    bounds_min=(float(vals[6]), float(vals[7]),
                                float(vals[8])),
                    bounds_max=(float(vals[9]), float(vals[10]),
                                float(vals[11])),
                    surface_generation=int(vals[12]),
                )
                records.append(rec)
        self._dirs = records
        return records

    def read_block_mesh(self, record: SurfaceBlockRecord
                        ) -> Tuple[np.ndarray, np.ndarray]:
        """Read a single block's vertex + triangle arrays from the cache."""
        with open(self._path, "rb") as f:
            v_off = int(record.vertex_offset)
            v_count = int(record.vertex_count)
            t_off = int(record.triangle_offset)
            t_count = int(record.triangle_count)
            if v_count > 0:
                f.seek(v_off)
                vraw = f.read(v_count * 12)
                verts = np.frombuffer(vraw, dtype=np.float32).reshape(-1, 3)
            else:
                verts = np.zeros((0, 3), dtype=np.float32)
            if t_count > 0:
                f.seek(t_off)
                traw = f.read(t_count * 12)
                tris = np.frombuffer(traw, dtype=np.int32).reshape(-1, 3)
            else:
                tris = np.zeros((0, 3), dtype=np.int32)
        return verts, tris





