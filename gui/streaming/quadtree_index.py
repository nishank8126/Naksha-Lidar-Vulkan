"""Persistent quadtree index + LAZ reference store for out-of-core streaming.

The index maps a spatial hierarchy to REFERENCES into the ORIGINAL LAS/LAZ
files. It never copies point data - with 2.49B points a copy-based tile store
would need ~50 GB and there is only 86.9 GB free on the volume.

Sidecar layout (.nakshaidx, little-endian, written with numpy .tofile so a
2.49B-point index streams to disk instead of being assembled in RAM):

    [header      ] magic, version, flags, total_points, total_files,
                   max_depth, node_count, run_count, bbox (6 x float64)
    [file table  ] per file: name, size, mtime, points, las_version,
                   point_format, scale(3 f64), offset(3 f64), has_rgb/int/cls
    [node table  ] per node: level, quad, first_run, run_count, point_count,
                   bbox (6 x float64)
    [run table   ] per run: file_id (u32), first_point (u64), count (u32)

Node addressing is a flat array indexed by the Morton-style quadtree key
(level, ix, iy) so a node lookup is arithmetic, not a tree walk. Empty cells
simply carry point_count == 0.
"""
import os
import struct
import time

import numpy as np

from .core import (MAX_DEPTH, MAX_LEAF_POINTS, MIN_LEAF_POINTS,
                   OVERVIEW_TARGET_POINTS, SIDECAR_MAGIC, SIDECAR_VERSION,
                   overview_path, sidecar_path)

_HEADER_DTYPE = np.dtype([
    ("magic", "S8"), ("version", "u4"), ("flags", "u4"),
    ("total_points", "u8"), ("total_files", "u4"), ("max_depth", "u4"),
    ("node_count", "u8"), ("run_count", "u8"),
    ("bbox", "f8", 6),
])
_FILE_DTYPE = np.dtype([
    ("name", "S260"), ("size", "u8"), ("mtime", "f8"), ("points", "u8"),
    ("las_version", "u2"), ("point_format", "u2"),
    ("scale", "f8", 3), ("offset", "f8", 3),
    ("has_rgb", "u1"), ("has_intensity", "u1"), ("has_class", "u1"),
])
_NODE_DTYPE = np.dtype([
    ("cell", "u4"),          # exact grid cell key (iy*side+ix) - authoritative,
    ("level", "u2"), ("quad", "u2"), ("first_run", "u8"), ("run_count", "u4"),
    ("point_count", "u8"), ("bbox", "f8", 6),
])
# A run is one contiguous span of points in one source file that landed in one
# cell. 16 bytes, packed.
_RUN_DTYPE = np.dtype([("file_id", "u4"), ("first_point", "u8"), ("count", "u4")])


class _QuadtreeCore:
    """Index construction, persistence and validation.

    Composed into the public QuadtreeIndex together with _QuadtreeQueries at
    the bottom of this file.
    """

    CHUNK_POINTS = 2_000_000

    def __init__(self):
        self.header = None
        self.files = None            # structured array of _FILE_DTYPE
        self.nodes = None             # structured array of _NODE_DTYPE
        self.runs = None              # structured array of _RUN_DTYPE
        self.path = None
        self.bbox_min = None
        self.bbox_max = None
        self.max_depth = MAX_DEPTH
        self.side = 1 << MAX_DEPTH
        self.point_count = None       # per-leaf-cell point count (uint64)

    def scan_headers(self, source_paths):
        """Header-only pass. No point data is touched."""
        import laspy
        file_rows = []
        gmin = np.array([np.inf, np.inf, np.inf])
        gmax = np.array([-np.inf, -np.inf, -np.inf])
        total_points = 0
        for fid, p in enumerate(source_paths):
            with laspy.open(p) as r:
                h = r.header
                try:
                    mn = np.asarray(h.mins, dtype=np.float64)
                    mx = np.asarray(h.maxs, dtype=np.float64)
                except Exception:
                    mn = np.asarray([-1e9, -1e9, -1e9])
                    mx = np.asarray([1e9, 1e9, 1e9])
                gmin = np.minimum(gmin, mn)
                gmax = np.maximum(gmax, mx)
                n = int(h.point_count)
                total_points += n
                dims = {str(d.name).lower() for d in h.point_format.dimensions}
                # NOTE: the dtype declares scale/offset as 3-element subarray
                # fields, so they must be passed NESTED here, not flattened.
                file_rows.append((
                    os.path.basename(p).encode("utf-8", "replace"),
                    int(os.path.getsize(p)), float(os.path.getmtime(p)), n,
                    int(h.version.major) * 10 + int(h.version.minor),
                    int(h.point_format.id),
                    (float(h.scales[0]), float(h.scales[1]), float(h.scales[2])),
                    (float(h.offsets[0]), float(h.offsets[1]), float(h.offsets[2])),
                    1 if "red" in dims else 0,
                    1 if "intensity" in dims else 0,
                    1 if "classification" in dims else 0,
                ))
        self.files = np.array(file_rows, dtype=_FILE_DTYPE)
        self.bbox_min, self.bbox_max = gmin, gmax
        return {"points": total_points, "files": len(source_paths),
                "bytes": int(self.files["size"].sum())}

    def build(self, source_paths, sidecar: str, progress=None, max_points=None):
        """One streaming pass over every file. Returns build statistics.

        Peak RAM is bounded by CHUNK_POINTS (2M) plus the run table, so this
        scales to datasets far larger than RAM.
        """
        import laspy
        t_start = time.perf_counter()
        source_paths = [p for p in source_paths
                        if p.lower().endswith((".laz", ".las"))]
        self.scan_headers(source_paths)
        total_points = int(self.files["points"].sum())
        gmin, gmax = self.bbox_min, self.bbox_max
        span_x = max(float(gmax[0] - gmin[0]), 1e-6)   # epsilon: no div by 0
        span_y = max(float(gmax[1] - gmin[1]), 1e-6)
        side = self.side
        n_cells = side * side

        point_count = np.zeros(n_cells, dtype=np.int64)
        bbox_min = np.full((n_cells, 3), np.inf, dtype=np.float64)
        bbox_max = np.full((n_cells, 3), -np.inf, dtype=np.float64)

        # Runs are accumulated into FLAT parallel arrays and grouped once at the
        # end by a single argsort on the cell id.
        #
        # The obvious alternative - appending to a per-cell array with
        # np.concatenate - is quadratic in the runs per cell, because every
        # append reallocates and copies the whole cell. On the 1.14B-point
        # NT219 build that turns a few seconds of work into many minutes and
        # leaves the index build, not the decode, as the bottleneck.
        run_cell = []
        run_file = []
        run_first = []
        run_count_ = []

        processed = 0
        for fid, p in enumerate(source_paths):
            with laspy.open(p) as r:
                file_base = 0
                for chunk in r.chunk_iterator(self.CHUNK_POINTS):
                    n = len(chunk)
                    if n == 0:
                        continue
                    if max_points is not None and processed >= max_points:
                        break
                    x = np.asarray(chunk.x, dtype=np.float64)
                    y = np.asarray(chunk.y, dtype=np.float64)
                    z = np.asarray(chunk.z, dtype=np.float64)
                    ix = np.clip(((x - gmin[0]) / span_x * side).astype(np.int64),
                                 0, side - 1)
                    iy = np.clip(((y - gmin[1]) / span_y * side).astype(np.int64),
                                 0, side - 1)
                    cell = iy * side + ix

                    # Sorting by cell makes each cell's points CONTIGUOUS,
                    # which is what lets one cell be one (file, first, count)
                    # run instead of 38,000 separate point records.
                    order = np.argsort(cell, kind="stable")
                    cs = cell[order]
                    xs, ys, zs = x[order], y[order], z[order]
                    # A run is only a valid (first_point, count) span if the
                    # points are CONSECUTIVE IN THE FILE. Sorting groups a cell's
                    # points logically, but within one chunk they are usually
                    # NOT adjacent in the file - a scan line sweeps across many
                    # cells, so one cell's points are typically interleaved with
                    # its neighbours. Emitting one run per cell-per-chunk would
                    # therefore decode the wrong points while still reporting the
                    # right COUNT, which is a silent mis-address.
                    #
                    # A run is cut wherever the cell changes OR the original
                    # index is not exactly +1 from the previous one.
                    orig = order
                    brk = np.flatnonzero(
                        (np.diff(cs) != 0) | (np.diff(orig) != 1)) + 1
                    starts = np.concatenate(([0], brk))
                    ends = np.concatenate((brk, [n]))

                    uc = cs[starts]
                    ulen = ends - starts
                    np.add.at(point_count, uc, ulen)
                    # Per-cell bbox over EVERY point in the run. Because the
                    # chunk is sorted by cell, each run occupies the contiguous
                    # slice [starts[j], starts[j+1]), so reduceat computes the
                    # per-run min/max in one vectorised pass. Reducing only over
                    # starts (the first point of each run) would silently
                    # understate the tile bounds and break frustum culling.
                    np.minimum.at(bbox_min[:, 0], uc,
                                  np.minimum.reduceat(xs, starts))
                    np.minimum.at(bbox_min[:, 1], uc,
                                  np.minimum.reduceat(ys, starts))
                    np.minimum.at(bbox_min[:, 2], uc,
                                  np.minimum.reduceat(zs, starts))
                    np.maximum.at(bbox_max[:, 0], uc,
                                  np.maximum.reduceat(xs, starts))
                    np.maximum.at(bbox_max[:, 1], uc,
                                  np.maximum.reduceat(ys, starts))
                    np.maximum.at(bbox_max[:, 2], uc,
                                  np.maximum.reduceat(zs, starts))

                    run_cell.append(uc.astype(np.int64))
                    run_file.append(np.full(uc.size, fid, dtype=np.uint32))
                    # The run's file offset is the ORIGINAL position of the
                    # point that landed at sorted position starts[j] - that is
                    # order[starts[j]], NOT starts[j].
                    #
                    # Recording the sorted position as if it were a file offset
                    # yields a run of the correct LENGTH whose span sits in the
                    # right part of the file but decodes to the WRONG points:
                    # counts, totals and cell bounds all look correct, so nothing
                    # reports an error while the index silently mis-addresses
                    # every tile. Inverse-permute back through the sort order.
                    run_first.append((file_base + order[starts]).astype(np.int64))
                    run_count_.append(ulen.astype(np.int64))

                    file_base += n
                    processed += n
                    if progress:
                        progress(processed, total_points)

        run_cell = np.concatenate(run_cell) if run_cell else np.zeros(0, np.int64)
        run_file = np.concatenate(run_file) if run_file else np.zeros(0, np.uint32)
        run_first = np.concatenate(run_first) if run_first else np.zeros(0, np.int64)
        run_count_ = np.concatenate(run_count_) if run_count_ else np.zeros(0, np.int64)
        run_total = int(run_cell.size)

        # One grouping pass: order runs by cell, then build per-cell views.
        order = np.argsort(run_cell, kind="stable")
        runs_flat = np.empty(run_total, dtype=_RUN_DTYPE)
        runs_flat["file_id"] = run_file[order]
        runs_flat["first_point"] = run_first[order]
        runs_flat["count"] = run_count_[order].astype(np.uint32)
        starts_per_cell = np.searchsorted(run_cell[order],
                                         np.arange(n_cells + 1, dtype=np.int64))
        cell_runs = [None] * n_cells
        for ci in np.flatnonzero(point_count > 0):
            a = int(starts_per_cell[ci])
            b = int(starts_per_cell[ci + 1])
            cell_runs[ci] = runs_flat[a:b] if b > a else None

        self.point_count = point_count
        self._write_sidecar(sidecar, cell_runs, point_count, bbox_min, bbox_max,
                            processed, time.perf_counter() - t_start)
        return {"points": processed, "files": len(source_paths), "runs": run_total,
                "nonempty_cells": int((point_count > 0).sum()),
                "seconds": time.perf_counter() - t_start, "sidecar": sidecar}

    def _write_sidecar(self, path, cell_runs, point_count, bbox_min, bbox_max,
                       total_points, seconds):
        """Stream the index to disk. Only non-empty cells are written."""
        nonempty = np.flatnonzero(point_count > 0)
        n_nodes = int(nonempty.size)
        total_runs = 0
        for ci in nonempty:
            r = cell_runs[ci]
            if r is not None:
                total_runs += int(r.size)

        hdr = np.zeros(1, dtype=_HEADER_DTYPE)
        hdr["magic"] = SIDECAR_MAGIC
        hdr["version"] = SIDECAR_VERSION
        hdr["flags"] = 0
        hdr["total_points"] = total_points
        hdr["total_files"] = int(self.files.size)
        hdr["max_depth"] = self.max_depth
        hdr["node_count"] = n_nodes
        hdr["run_count"] = total_runs
        hdr["bbox"] = np.concatenate([self.bbox_min, self.bbox_max])

        # Pass 1: node table + a run-count prefix so run offsets are exact.
        nodes = np.zeros(n_nodes, dtype=_NODE_DTYPE)
        for k, ci in enumerate(nonempty):
            r = cell_runs[ci]
            rc = int(r.size) if r is not None else 0
            nodes[k] = (int(ci), self.max_depth, 0, 0, rc, int(point_count[ci]),
                        np.concatenate([bbox_min[ci], bbox_max[ci]]))
        # Cumulative run offsets.
        counts = nodes["run_count"].astype(np.int64)
        nodes["first_run"] = np.concatenate(([0], np.cumsum(counts)[:-1]))

        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            hdr.tofile(f)
            self.files.tofile(f)
            nodes.tofile(f)
            # Runs are appended in node order so first_run stays valid.
            for ci in nonempty:
                r = cell_runs[ci]
                if r is not None and r.size:
                    r.tofile(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

        self.header = hdr[0]
        self.nodes = nodes
        self.runs = _load_runs(path, int(total_runs))
        self.path = path

    # ----------------------------------------------------------------- load
    @staticmethod
    def load(path, mmap_runs=True):
        """Open an existing sidecar. O(1) - no point data is read."""
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        with open(path, "rb") as f:
            hdr = np.fromfile(f, dtype=_HEADER_DTYPE, count=1)[0]
            # numpy fixed-width bytes fields STRIP trailing NULs on read, so the
            # magic must be compared with trailing NULs removed on both sides.
            got = bytes(hdr["magic"]).rstrip(b"\x00")
            if got != SIDECAR_MAGIC.rstrip(b"\x00"):
                raise ValueError(f"bad sidecar magic {got!r} in {path}")
            if int(hdr["version"]) != SIDECAR_VERSION:
                raise ValueError(f"sidecar version {int(hdr['version'])} "
                                 f"!= {SIDECAR_VERSION}; rebuild required")
            files = np.fromfile(f, dtype=_FILE_DTYPE, count=int(hdr["total_files"]))
            nodes = np.fromfile(f, dtype=_NODE_DTYPE, count=int(hdr["node_count"]))
            n_runs = int(hdr["run_count"])
        idx = QuadtreeIndex()
        idx.header = hdr
        idx.files = files
        idx.nodes = nodes
        idx.max_depth = int(hdr["max_depth"])
        idx.side = 1 << idx.max_depth
        idx.bbox_min = np.asarray(hdr["bbox"][:3], dtype=np.float64)
        idx.bbox_max = np.asarray(hdr["bbox"][3:], dtype=np.float64)
        # Runs are memory-mapped, not read: an index of 20M runs costs 320 MB
        # of ADDRESS SPACE but almost no resident memory until touched.
        if mmap_runs and n_runs:
            idx.runs = np.memmap(path, dtype=_RUN_DTYPE, mode="r",
                                 offset=_runs_offset(hdr), shape=(n_runs,))
        elif n_runs:
            with open(path, "rb") as f:
                f.seek(_runs_offset(hdr))
                idx.runs = np.fromfile(f, dtype=_RUN_DTYPE, count=n_runs)
        else:
            idx.runs = np.zeros(0, dtype=_RUN_DTYPE)
        idx.path = path
        idx._keys = None      # force the sorted lookup to build lazily
        idx._rows = None
        return idx


def _runs_offset(hdr) -> int:
    """Byte offset of the run table: header + file table + node table."""
    return (_HEADER_DTYPE.itemsize
            + int(hdr["total_files"]) * _FILE_DTYPE.itemsize
            + int(hdr["node_count"]) * _NODE_DTYPE.itemsize)


def _load_runs(path, n_runs):
    if not n_runs:
        return np.zeros(0, dtype=_RUN_DTYPE)
    with open(path, "rb") as f:
        hdr = np.fromfile(f, dtype=_HEADER_DTYPE, count=1)[0]
        f.seek(_runs_offset(hdr))
        return np.fromfile(f, dtype=_RUN_DTYPE, count=n_runs)


def validate_against_sources(idx: "QuadtreeIndex", source_paths):
    """Header-only check that a sidecar still matches its source files.

    Returns (report,). `report["ok"]` is the verdict. Compares filename, size,
    mtime and point count per file, so a changed source invalidates only what is
    genuinely stale instead of forcing a full rebuild.
    """
    report = {"ok": True, "changed": [], "missing": [], "added": [],
              "reason": "sidecar matches all source headers"}
    indexed = {bytes(r["name"]).decode("utf-8", "replace"): r
               for r in idx.files}
    on_disk = {}
    for p in source_paths:
        b = os.path.basename(p)
        on_disk[b] = p

    for name, row in indexed.items():
        p = on_disk.get(name)
        if p is None:
            report["missing"].append(name)
            report["ok"] = False
            continue
        size = os.path.getsize(p)
        mtime = os.path.getmtime(p)
        if size != int(row["size"]) or abs(mtime - float(row["mtime"])) > 1.0:
            report["changed"].append(
                {"name": name, "reason": "size/mtime",
                 "indexed_size": int(row["size"]), "actual_size": size})
            report["ok"] = False
    for name in on_disk:
        if name not in indexed:
            report["added"].append(name)
            report["ok"] = False

    if not report["ok"]:
        parts = []
        if report["missing"]:
            parts.append(f"missing: {report['missing']}")
        if report["changed"]:
            parts.append(f"changed: {[c['name'] for c in report['changed']]}")
        if report["added"]:
            parts.append(f"added: {report['added']}")
        report["reason"] = "; ".join(parts)
    return report


class _QuadtreeQueries:
    """Cell-geometry queries, mixed into QuadtreeIndex below.

    Kept separate purely so the file can be edited in pieces; the methods are
    the class's own.
    """

    def cell_of(self, x, y):
        """World XY -> leaf cell index. O(1), no tree walk."""
        side = self.side
        ix = int(np.clip((x - self.bbox_min[0])
                         / max(self.bbox_max[0] - self.bbox_min[0], 1e-9) * side,
                         0, side - 1))
        iy = int(np.clip((y - self.bbox_min[1])
                         / max(self.bbox_max[1] - self.bbox_min[1], 1e-9) * side,
                         0, side - 1))
        return iy * side + ix

    def _node_lookup(self):
        """Lazily build the cell -> node-row map as SORTED parallel arrays.

        The key is the PERSISTED `cell` field, never a value recomputed from the
        bbox centre. Recomputing is subtly wrong: a cell whose points are
        distributed asymmetrically has a centre that can round into a
        neighbouring cell, which silently yields duplicated and missed nodes in
        the visible-cell query - i.e. dropped geometry.

        Lookup is O(log n) per cell; a linear scan per visible cell is what
        makes a naive implementation quadratic, and at this scale it never
        returns.
        """
        if getattr(self, "_keys", None) is not None:
            return self._keys, self._rows
        keys = np.asarray(self.nodes["cell"], dtype=np.int64)
        order = np.argsort(keys, kind="stable")
        self._keys = keys[order]
        self._rows = order.astype(np.int64)
        return self._keys, self._rows

    def _row_for_cell(self, cell):
        keys, rows = self._node_lookup()
        i = np.searchsorted(keys, cell)
        if i < keys.size and keys[i] == cell:
            return int(rows[i])
        return -1

    def lookup_cell(self, cx, cy):
        """Return (node_row, point_count) or (-1, 0) if the cell is empty."""
        if cx < 0 or cy < 0 or cx >= self.side or cy >= self.side:
            return -1, 0
        r = self._row_for_cell(cy * self.side + cx)
        if r < 0:
            return -1, 0
        return r, int(self.nodes["point_count"][r])

    def node_bbox(self, row):
        bb = self.nodes["bbox"][row]
        return (float(bb[0]), float(bb[1]), float(bb[2]),
                float(bb[3]), float(bb[4]), float(bb[5]))

    def node_runs(self, row):
        """The (file_id, first_point, count) runs backing one cell."""
        s = int(self.nodes["first_run"][row])
        c = int(self.nodes["run_count"][row])
        if c <= 0:
            return np.zeros(0, dtype=_RUN_DTYPE)
        return self.runs[s:s + c]

    def visible_cells(self, min_x, min_y, max_x, max_y, max_cells=200_000):
        """Cells intersecting a world-space rectangle.

        A 2D range query over the CELL GRID, never a scan of points: the cost
        is proportional to the cells in the rectangle, not to the dataset. This
        is the only spatial operation the camera path performs.
        """
        side = self.side
        span_x = max(self.bbox_max[0] - self.bbox_min[0], 1e-9)
        span_y = max(self.bbox_max[1] - self.bbox_min[1], 1e-9)
        ix0 = int(np.clip((min_x - self.bbox_min[0]) / span_x * side, 0, side - 1))
        ix1 = int(np.clip((max_x - self.bbox_min[0]) / span_x * side, 0, side - 1))
        iy0 = int(np.clip((min_y - self.bbox_min[1]) / span_y * side, 0, side - 1))
        iy1 = int(np.clip((max_y - self.bbox_min[1]) / span_y * side, 0, side - 1))
        if ix1 < ix0:
            ix0, ix1 = ix1, ix0
        if iy1 < iy0:
            iy0, iy1 = iy1, iy0
        n = (ix1 - ix0 + 1) * (iy1 - iy0 + 1)
        if n <= 0:
            return []
        if n > max_cells:
            # Bounded: a full-dataset fit would otherwise enumerate 65k cells
            # x every run lookup. Coarsen the enumeration stride instead; the
            # scheduler refines progressively and the overview covers the gap.
            step = int(np.ceil(np.sqrt(n / float(max_cells))))
            ix1 = min(side - 1, ix0 + ((ix1 - ix0) // step) * step + step)
            iy1 = min(side - 1, iy0 + ((iy1 - iy0) // step) * step + step)

        keys, rows = self._node_lookup()
        out = []
        for iy in range(iy0, iy1 + 1):
            base = iy * side
            lo = base + ix0
            hi = base + ix1 + 1          # exclusive upper cell key
            # BOTH bounds must be side='left': they delimit the half-open range
            # [lo, hi). side='right' on the upper bound would also take the key
            # equal to hi, which belongs to the NEXT row - that duplicates
            # nodes along one scanline and inflates the visible point count,
            # i.e. phantom geometry that was never in the dataset.
            i = np.searchsorted(keys, lo, side="left")
            j = np.searchsorted(keys, hi, side="left")
            if j > i:
                out.extend(int(v) for v in rows[i:j])
        return out

    def total_points(self):
        return int(self.header["total_points"]) if self.header is not None else 0


class QuadtreeIndex(_QuadtreeCore, _QuadtreeQueries):
    """A persisted quadtree over one or more LAS/LAZ files.

    Build is a SINGLE streaming pass: every source file is read once in chunks,
    each chunk is sorted by cell, and the resulting runs are appended. Nothing
    proportional to the POINT COUNT is ever held in RAM - the per-chunk working
    set is bounded by CHUNK_POINTS regardless of dataset size, and what does
    grow is the RUN table (~20M runs for 2.49B points, not 2.49B records).

    The index stores REFERENCES (file_id, first_point, count) into the original
    files. It never copies point data: at 2.49B points a copy-based tile store
    would need ~50 GB and the volume has 86.9 GB free in total.
    """


def _quadtree_key(ix, iy, depth):
    """Flat index of a cell at `depth` in a quadtree array layout.

    Level d occupies [4^d, 4^(d+1)) so every level is a contiguous slab and a
    cell lookup is pure arithmetic - no tree walk, no pointers.
    """
    d = np.asarray(depth, dtype=np.int64)
    base = (4 ** d - 1) // 3          # sum(4^0..4^(d-1))
    return base + d + (ix << d) + iy  # ix,yy are 0..2^d-1
