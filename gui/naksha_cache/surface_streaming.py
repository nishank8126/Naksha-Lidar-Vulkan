"""One asynchronous Surface producer using the existing point-cache hierarchy.

Only the frame owner consumes results and stages GPU resources. The worker owns
its file handles, triangulation, colour calculation and append transactions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import OrderedDict
import hashlib
import io
import json
import queue
import os
import sqlite3
import tempfile
import threading
import time

import numpy as np

from .derived_container import DerivedContainer, project_container_path
from .format import ATTR_XYZ, ATTR_SOURCE_ID, ATTR_CLASSIFICATION
from .reader import NakshaPointCacheReader
from .surface_cache import SurfaceBlockTriangulator, SurfaceBudgetExceeded

ALGORITHM_VERSION = 1


@dataclass(frozen=True)
class SurfaceRequest:
    token: tuple
    viewport: tuple
    pixels: tuple
    settings: tuple
    style: tuple


@dataclass
class SurfaceResult:
    request: SurfaceRequest
    positions: np.ndarray
    indices: np.ndarray
    colors: np.ndarray
    counts: np.ndarray
    blocks: tuple
    cache_hits: int
    triangulations: int
    revision: int
    error: str = ""


def triangulate_identity_block(core, contexts, max_edge, max_vertices=160000):
    """Canonical minimum vertex ID owns a face; halo vertices are references.

    Adjacent nodes' measured point AABBs are not cell partitions. Canonical
    ownership avoids discarding the sliver between two measured AABBs.
    """
    arrays = [core, *contexts]
    positions = np.concatenate([p["xyz"] for p in arrays])
    ids = np.concatenate([p["source_id"] for p in arrays]).astype(np.uint64)
    references = np.concatenate([p["refs"] for p in arrays])
    finite = np.isfinite(positions).all(axis=1)
    positions, ids, references = positions[finite], ids[finite], references[finite]
    order = np.lexsort((ids, positions[:, 2], positions[:, 1], positions[:, 0]))
    positions, ids, references = positions[order], ids[order], references[order]
    unique = np.ones(len(positions), bool)
    unique[1:] = np.any(positions[1:, :2] != positions[:-1, :2], axis=1)
    positions, ids, references = positions[unique], ids[unique], references[unique]
    if len(positions) > max_vertices:
        raise SurfaceBudgetExceeded("Surface context exceeds block RAM budget; use a coarser common LOD")
    triangulator = SurfaceBlockTriangulator(max_edge=max_edge, max_triangles=max_vertices * 2)
    faces = triangulator._delaunay(positions)
    if len(faces):
        from gui.surface_mode import _filter_long_edges
        faces = _filter_long_edges(faces, positions[:, :2], max_edge)
        owned = np.isin(ids[faces].min(axis=1), core["source_id"])
        faces = faces[owned].copy()
        cross = np.cross(positions[faces[:, 1]] - positions[faces[:, 0]],
                         positions[faces[:, 2]] - positions[faces[:, 0]])
        down = cross[:, 2] < 0
        faces[down] = faces[down][:, [0, 2, 1]]
    used, inverse = np.unique(faces.reshape(-1), return_inverse=True)
    return {"refs": references[used], "ids": ids[used],
            "faces": inverse.reshape(-1, 3).astype(np.uint32), "xyz": positions[used]}


class SurfaceBuildService:
    def __init__(self, dataset, *, max_points=1500000, max_ram=256 << 20):
        self.dataset = str(dataset)
        self.max_points = int(max_points)
        self.max_ram = int(max_ram)
        self.results = queue.Queue(maxsize=1)
        self._condition = threading.Condition()
        self._request = None
        self._stop = threading.Event()
        self._thread = None
        self.status = "IDLE"
        self.error = ""
        self.timestamps = {}
        self.triangulation_calls = 0
        self.cache_hits = 0
        self.elevation_bounds = None
        self._point_blocks = OrderedDict()
        self._point_block_bytes = 0

    def request(self, request):
        with self._condition:
            if request == self._request:
                return
            self._request = request
            self.timestamps = {"T0": time.perf_counter()}
            self.status = "QUEUED"
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="naksha-surface", daemon=True)
                self._thread.start()
            self._condition.notify()

    def cancel_request(self):
        with self._condition:
            self._request = None
            self._condition.notify()

    def shutdown(self, timeout=5):
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        if self._thread:
            self._thread.join(timeout)
        return self._thread is None or not self._thread.is_alive()

    def consume(self):
        try:
            return self.results.get_nowait()
        except queue.Empty:
            return None

    def _publish(self, result):
        try:
            self.results.get_nowait()
        except queue.Empty:
            pass
        self.results.put_nowait(result)

    def _current(self, request):
        return not self._stop.is_set() and request == self._request

    def _run(self):
        reader = None
        try:
            reader = NakshaPointCacheReader(self.dataset, load_edits=False)
            idx = reader.index
            identity = {"layout_fingerprint": int(idx.layout_fingerprint),
                        "source_set_fingerprint": hashlib.sha256(idx.sources.tobytes()).hexdigest()}
            container = DerivedContainer.import_base(project_container_path(self.dataset),
                idx.path, reader.pc_path, identity, self._stop)
            last = None
            while not self._stop.is_set():
                with self._condition:
                    self._condition.wait_for(lambda: self._stop.is_set() or self._request != last)
                    request = self._request
                if self._stop.is_set():
                    break
                last = request
                if request is None:
                    self.status = "CANCELLED"
                    continue
                try:
                    if self.elevation_bounds is None:
                        self.elevation_bounds = self._elevation_percentiles(reader, container, identity)
                    self._build(reader, container, identity, request)
                except InterruptedError:
                    continue
                except Exception as exc:
                    self.status, self.error = "FAILED", repr(exc)
                    self._publish(SurfaceResult(request, np.empty((0, 3)), np.empty((0, 3), np.uint32),
                        np.empty((0, 3), np.uint8), np.empty(0, np.uint32), (), 0, 0, 0, self.error))
        except Exception as exc:
            self.status, self.error = "FAILED", repr(exc)
        finally:
            if reader is not None:
                reader.close()

    def _build(self, reader, container, identity, request):
        self.status = "BUILDING"
        self.timestamps["T1"] = time.perf_counter()
        idx = reader.index
        bounds_min = np.asarray(idx.nodes["bounds_min"], float)
        bounds_max = np.asarray(idx.nodes["bounds_max"], float)
        cx, cy, hw, hh = request.viewport
        # Margin provides Surface coverage while the next camera set prepares.
        lo, hi = np.array([cx-hw*1.3, cy-hh*1.3]), np.array([cx+hw*1.3, cy+hh*1.3])
        visible = np.flatnonzero(np.all(bounds_max[:, :2] >= lo, axis=1)
                                  & np.all(bounds_min[:, :2] <= hi, axis=1))
        if not len(visible):
            raise ValueError("no source coverage in Surface viewport")
        # One common rung for this Surface set, selected from the SAME hierarchy.
        # No point-frontier selector or camera callback is modified.
        maximum_lod = min(max(lod for lod in range(int(idx.header["lod_count"]))
            if idx.find_block(int(nid), lod) is not None) for nid in visible)
        target_spacing = request.viewport[3]*2 / max(request.pixels[1], 1)
        lod = 0
        while lod < maximum_lod:
            spacing = np.median(idx.nodes["lod_spacing"][visible, lod+1])
            if spacing > target_spacing * 1.5:
                break
            lod += 1
        while lod < maximum_lod and sum(int(idx.find_block(int(n), lod)["point_count"]) for n in visible) > self.max_points:
            lod += 1
        if sum(int(idx.find_block(int(n), lod)["point_count"]) for n in visible) > self.max_points:
            raise SurfaceBudgetExceeded("visible Surface cannot fit the bounded working set")
        max_edge, support = request.settings
        if max_edge <= 0:
            extent = float(np.max(bounds_max[:, :2].max(axis=0)-bounds_min[:, :2].min(axis=0)))
            # A camera/LOD change must not invalidate every committed block.
            spacing = float(np.median(idx.nodes["lod_spacing"][:, 0]))
            max_edge = max(extent * .10, spacing * 100)
        neighbours_by_node = {int(nid): np.flatnonzero(
            np.all(bounds_max[:, :2] >= bounds_min[nid, :2]-max_edge, axis=1)
            & np.all(bounds_min[:, :2] <= bounds_max[nid, :2]+max_edge, axis=1))
            for nid in visible}
        # Include halos in the preflight, before decoding or invoking Qhull.
        # Coarsen the entire set together so neighbouring blocks share a rung.
        def halo_count(neighbours):
            return sum(int(idx.find_block(int(n), lod)["point_count"])
                       for n in neighbours if idx.find_block(int(n), lod) is not None)
        while lod < maximum_lod and max(map(halo_count, neighbours_by_node.values())) > 160000:
            lod += 1
        if max(map(halo_count, neighbours_by_node.values())) > 160000:
            raise SurfaceBudgetExceeded("coarsest common Surface halo exceeds RAM budget")
        metadata = dict(identity, algorithm_version=ALGORITHM_VERSION,
                        settings_fingerprint=hashlib.sha256(json.dumps((max_edge, support)).encode()).hexdigest())
        section = container.directory["sections"].get("SURFACE_DATA", {})
        stored = section.get("blocks", {}) if section.get("metadata") == metadata else {}
        blocks, hits, builds = [], 0, 0
        # Newly triangulated blocks are staged here and published in ONE append.
        # Appending per block re-wrote the whole directory JSON on every commit:
        # 394 visible blocks produced 394 generations and tens of MB of dead
        # directory/generation payload in the container. Batching keeps the
        # append-only contract (one bounded, checksummed commit) while making
        # the file grow with the BLOCKS, not with the square of their count.
        staged = []
        for nid in visible:
            if not self._current(request):
                raise InterruptedError("Surface request superseded")
            key = f"{int(nid)}:{lod}"
            if key in stored:
                payload, _ = container.read_block("SURFACE_DATA", key)
                with np.load(io.BytesIO(payload), allow_pickle=False) as data:
                    block = {name: data[name] for name in ("refs", "ids", "faces")}
                block["xyz"] = self._resolve(reader, block["refs"])
                hits += 1
            else:
                core = self._read(reader, int(nid), lod, support)
                neighbours = neighbours_by_node[int(nid)]
                contexts = []
                for other in neighbours:
                    if other == nid:
                        continue
                    part = self._read(reader, int(other), lod, support)
                    keep = np.all(part["xyz"][:, :2] >= bounds_min[nid, :2]-max_edge, axis=1) & np.all(part["xyz"][:, :2] <= bounds_max[nid, :2]+max_edge, axis=1)
                    contexts.append({k: v[keep] for k, v in part.items()})
                    if sum(len(p["xyz"]) for p in contexts) > 160000:
                        raise SurfaceBudgetExceeded("Surface halo exceeds bounded context")
                block = triangulate_identity_block(core, contexts, max_edge)
                self.triangulation_calls += 1
                builds += 1
                self.timestamps.setdefault("T2", time.perf_counter())
                buffer = io.BytesIO()
                np.savez(buffer, refs=block["refs"], ids=block["ids"], faces=block["faces"])
                staged.append((key, buffer.getvalue(), {
                    "node_id": int(nid), "lod": lod, "triangle_count": len(block["faces"]),
                    "bounds": [bounds_min[nid].tolist(), bounds_max[nid].tolist()],
                    "index_encoding": "uint32", "ownership": "minimum_canonical_vertex_id"}))
                if len(staged) >= 64:
                    # Bounded batch: progress still survives a camera move (the
                    # completed blocks are committed), without one directory
                    # rewrite per block.
                    container.append("SURFACE_DATA", metadata, staged)
                    staged.clear()
                    self.timestamps["T8"] = time.perf_counter()
            blocks.append((int(nid), block))
            if sum(b["xyz"].nbytes+b["faces"].nbytes+b["ids"].nbytes+b["refs"].nbytes for _, b in blocks) > self.max_ram:
                raise SurfaceBudgetExceeded("Surface working set exceeds RAM budget")
        if staged:
            container.append("SURFACE_DATA", metadata, staged)
            staged.clear()
            self.timestamps["T8"] = time.perf_counter()
        # Deduplicate halo vertices across the bounded visible set by canonical ID.
        ids = np.concatenate([b["ids"] for _, b in blocks])
        xyz = np.concatenate([b["xyz"] for _, b in blocks])
        unique, first, inverse = np.unique(ids, return_index=True, return_inverse=True)
        positions = np.ascontiguousarray(xyz[first])
        faces, counts, offset = [], [], 0
        for _, block in blocks:
            local = inverse[offset:offset+len(block["ids"])]
            faces.append(local[block["faces"]])
            counts.append(block["faces"].size)
            offset += len(block["ids"])
        indices = np.ascontiguousarray(np.concatenate(faces), np.uint32)
        if not len(indices):
            raise ValueError("Surface selection contains no supported triangles")
        from gui.surface_mode import _compute_surface_face_colors
        azimuth, angle, ambient, z_lo, z_hi, ramp = request.style
        if self.elevation_bounds is not None:
            z_lo, z_hi = self.elevation_bounds
        ramp = json.loads(ramp)
        colors = _compute_surface_face_colors(positions, indices, azimuth, angle, ambient, z_lo, z_hi, ramp)
        if self._current(request):
            self.status = "READY"
            self.cache_hits += hits
            self.timestamps["T4"] = time.perf_counter()
            result = SurfaceResult(request, positions, indices, colors, np.asarray(counts, np.uint32),
                                   tuple((n, lod) for n in visible), hits, builds, container.generation)
            self._publish(result)
        self.timestamps["T7"] = time.perf_counter()

    def _elevation_percentiles(self, reader, container, identity):
        """Exact legacy 1/99 percentiles, using a disk-backed frequency table.

        No full XYZ/Z array or approximate sampling enters this calculation.
        The SQLite index bounds RAM even for non-quantized, distinct heights.
        The resulting two scalars are embedded once and reused on cache HIT.
        """
        section = container.directory['sections'].get('SURFACE_STATS', {})
        if section.get('metadata') == identity and 'z_percentiles' in section.get('blocks', {}):
            return tuple(json.loads(container.read_block('SURFACE_STATS', 'z_percentiles')[0]))
        descriptor, path = tempfile.mkstemp(prefix='naksha-surface-z-', suffix='.tmp',
                                            dir=container.path.parent)
        os.close(descriptor)
        connection = None
        try:
            connection = sqlite3.connect(path)
            connection.execute('PRAGMA journal_mode=OFF')
            connection.execute('PRAGMA synchronous=OFF')
            connection.execute('PRAGMA cache_size=-8192')
            connection.execute('CREATE TABLE heights(z REAL PRIMARY KEY, n INTEGER NOT NULL) WITHOUT ROWID')
            total = 0
            for nid in range(len(reader.index.nodes)):
                if self._stop.is_set():
                    raise InterruptedError('Surface elevation scan cancelled')
                data = reader.read_tile(nid, 0, only_attrs=(ATTR_XYZ,), verify_crc=True)
                z = data['xyz'][:, 2]
                z = z[np.isfinite(z)]
                values, counts = np.unique(z, return_counts=True)
                connection.executemany('INSERT INTO heights(z,n) VALUES (?,?) ON CONFLICT(z) DO UPDATE SET n=n+excluded.n',
                                       ((float(v), int(n)) for v, n in zip(values, counts)))
                total += len(z)
                connection.commit()
            if not total:
                raise ValueError('Surface has no finite elevation samples')
            ranks = np.asarray([.01, .99]) * (total-1)
            targets = sorted(set(np.floor(ranks).astype(int)) | set(np.ceil(ranks).astype(int)))
            found, cumulative, cursor = {}, 0, 0
            for value, count in connection.execute('SELECT z,n FROM heights ORDER BY z'):
                cumulative += count
                while cursor < len(targets) and targets[cursor] < cumulative:
                    found[targets[cursor]] = value
                    cursor += 1
                if cursor == len(targets):
                    break
            result = tuple(float(found[int(np.floor(r))] + (found[int(np.ceil(r))]-found[int(np.floor(r))])*(r-np.floor(r))) for r in ranks)
            container.append('SURFACE_STATS', identity, [('z_percentiles', json.dumps(result).encode(),
                              {'method': 'numpy_linear_percentile_1_99', 'sample_count': total})])
            return result
        finally:
            if connection is not None:
                connection.close()
            if os.path.exists(path):
                os.unlink(path)

    def _read(self, reader, nid, lod, support):
        key = (int(nid), int(lod), tuple(support))
        cached = self._point_blocks.get(key)
        if cached is not None:
            self._point_blocks.move_to_end(key)
            return cached
        data = reader.read_tile(nid, lod, only_attrs=(ATTR_XYZ, ATTR_SOURCE_ID, ATTR_CLASSIFICATION), verify_crc=True)
        if data is None:
            raise ValueError(f"Surface neighbour {nid}:{lod} is absent")
        xyz = np.asarray(data["xyz"], np.float64)
        ids = np.asarray(data["source_id"], np.uint64)
        refs = np.column_stack((np.full(len(xyz), nid, np.uint32), np.full(len(xyz), lod, np.uint32), np.arange(len(xyz), dtype=np.uint32)))
        keep = np.isfinite(xyz).all(axis=1)
        if support:
            keep &= np.isin(data.get("cls", data.get("classification")), support)
        result = {"xyz": xyz[keep], "source_id": ids[keep], "refs": refs[keep]}
        size = sum(v.nbytes for v in result.values())
        limit = self.max_ram // 4
        while self._point_blocks and self._point_block_bytes + size > limit:
            _, old = self._point_blocks.popitem(last=False)
            self._point_block_bytes -= sum(v.nbytes for v in old.values())
        if size <= limit:
            self._point_blocks[key] = result
            self._point_block_bytes += size
        return result

    def _resolve(self, reader, references):
        xyz = np.empty((len(references), 3), np.float64)
        for nid, lod in np.unique(references[:, :2], axis=0):
            mask = np.all(references[:, :2] == (nid, lod), axis=1)
            data = self._read(reader, int(nid), int(lod), ())
            local = np.searchsorted(data['refs'][:, 2], references[mask, 2])
            if np.any(local >= len(data['refs'])) or not np.array_equal(data['refs'][local, 2], references[mask, 2]):
                raise ValueError('Surface reference no longer resolves in its source block')
            xyz[mask] = data["xyz"][local]
        return xyz
