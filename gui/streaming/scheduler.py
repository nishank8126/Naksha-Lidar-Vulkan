"""Bounded async streaming pipeline + camera-priority tile scheduler.

Pipeline (Phase 5), all stages bounded:
    REQUEST -> IO READY -> DECODE READY -> RAM READY -> GPU UPLOAD READY

Rules enforced here rather than hoped for:
  * The render thread NEVER touches LAZ, never decodes, never waits on a
    worker. It only reads the ready-queue snapshot and swaps residency.
  * Every queue has a hard capacity. A 2.49B-point dataset must never be able
    to enqueue unbounded work just because the camera moved fast.
  * Requests are PRIORITISED and CANCELLED. When the camera pans east, queued
    west work is dropped instead of consuming decode bandwidth on data that has
    left the screen.
"""
import heapq
import itertools
import threading
import time
from collections import OrderedDict

from .core import EVICTABLE, GPU_RESIDENT, RAM_READY, READING, REQUESTED, \
    UNLOADED

# Priority tiers (Phase 6). Lower sorts first.
PRIO_MISSING_COARSE = 0      # visible, no resident data at all
PRIO_SCREEN_CENTRE = 1       # visible, nearest the middle of the screen
PRIO_VISIBLE_DETAIL = 2      # visible, refinement of an existing tile
PRIO_PREDICTED = 3           # halo ahead of the camera's motion
PRIO_NEIGHBOUR = 4           # frustum-adjacent, for panning without holes
PRIO_BACKGROUND = 5          # non-visible refinement


class TileRequest:
    __slots__ = ("row", "priority", "seq", "canceled")

    def __init__(self, row, priority, seq):
        self.row = row
        self.priority = priority
        self.seq = seq
        self.canceled = False

    def __lt__(self, other):
        return (self.priority, self.seq) < (other.priority, other.seq)


class StreamScheduler:
    """Priority queue + decode workers + bounded RAM cache.

    Threading contract:
      render thread  -> submit()/snapshot()/stats()  (never blocks on decode)
      workers        -> pull a request, decode, publish to the ready queue
    """

    def __init__(self, index, reader, workers=3, ram_budget_bytes=0,
                 queue_capacity=512, ready_capacity=256):
        self.index = index
        self.reader = reader
        self.workers = max(1, int(workers))
        self.ram_budget = int(ram_budget_bytes) or (1 << 30)
        self.queue_capacity = queue_capacity
        self.ready_capacity = ready_capacity

        self._lock = threading.RLock()
        self._pending = []                 # heap of TileRequest
# ------------------------------------------------------------ lifecycle
    def start(self):
        for i in range(self.workers):
            t = threading.Thread(target=self._worker, name=f"naksha-decode-{i}",
                                 daemon=True)
            t.start()
            self._threads.append(t)

    def shutdown(self):
        """Stop workers and release LAZ handles. Safe to call twice."""
        self._stop.set()
        with self._new_ready:
            self._new_ready.notify_all()
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads = []
        from .tile_store import close_readers
        close_readers()

    # -------------------------------------------------------------- submit
    def submit(self, rows_with_priority, frame=None):
        """Enqueue work. Bounded: excess is dropped, never queued forever."""
        dropped = 0
        with self._lock:
            if frame is not None:
                self._frame = frame
            for row, prio in rows_with_priority:
                if row in self._ready or row in self._inflight:
                    continue
                if row in self._pending_rows:
                    # Already queued: promote only if the new priority is
                    # better. A stale low-priority entry must not shadow a
                    # tile that has just become visible.
                    old = self._pending_rows[row]
                    if prio < old.priority:
                        old.priority = prio
                        heapq.heapify(self._pending)
                    continue
                if len(self._pending) >= self.queue_capacity:
                    dropped += 1
                    self.stats_counters["oversize_drops"] += 1
                    continue
                req = TileRequest(row, prio, next(self._counter))
                heapq.heappush(self._pending, req)
                self._pending_rows[row] = req
                self._state[row] = REQUESTED
                self.stats_counters["submitted"] += 1
        return dropped

    def cancel_stale(self, visible_rows, prefetch_rows=()):
        """Drop queued work that is neither visible nor a prefetch target.

        This is the Phase 6 requirement: after a fast pan, requests for tiles
        that left the screen are CANCELLED rather than left to consume decode
        bandwidth on data nobody can see.
        """
        keep = set(visible_rows) | set(prefetch_rows)
        cancelled = 0
        with self._lock:
            if not self._pending:
                return 0
            keep_pending = []
            for req in self._pending:
                if req.row in keep or req.row in self._inflight:
                    keep_pending.append(req)
                else:
                    req.canceled = True
# --------------------------------------------------------------- worker
    def _worker(self):
        while not self._stop.is_set():
            with self._lock:
                while not self._pending and not self._stop.is_set():
                    self._new_ready.wait(timeout=0.05)
                if not self._pending:
                    if self._stop.is_set():
                        return
                    continue
                req = heapq.heappop(self._pending)
                self._pending_rows.pop(req.row, None)
                # Never re-decode a tile that is already warm.
                if req.row in self._ready or req.canceled:
                    continue
                self._inflight[req.row] = req
                self._state[req.row] = READING

            row = req.row
            t0 = time.perf_counter()
            try:
                tile = self.reader.read_tile(row)
            except Exception:
                tile = None
            dt = time.perf_counter() - t0

            with self._lock:
                self._inflight.pop(row, None)
                if self._stop.is_set():
                    continue
                if tile is None:
                    self._state[row] = UNLOADED
                    continue
                nb = tile["gpu_bytes"]
                # Enforce the RAM budget BEFORE publishing, so a tile that does
                # not fit is refused instead of silently overrunning the cache.
                if nb > self.ram_budget:
                    self._state[row] = UNLOADED
                    self.stats_counters["oversize_drops"] += 1
                    continue
                self._evict_for(nb, protect=row)
                if self._ready_bytes + nb > self.ram_budget:
                    self._state[row] = UNLOADED
                    self.stats_counters["oversize_drops"] += 1
                    continue
                if len(self._ready) >= self.ready_capacity:
                    self._evict_for(0, protect=row, max_evict=1)
                self._ready[row] = tile
                self._ready_bytes += nb
                self._state[row] = RAM_READY
                c = self.stats_counters
                c["completed"] += 1
                c["decode_points"] += tile["count"]
                c["decode_seconds"] += dt
                self._recent_latency.append(dt)
                if len(self._recent_latency) > 256:
                    del self._recent_latency[:-256]
                self._new_ready.notify()

    def _evict_for(self, incoming_bytes, protect=None, max_evict=None):
        """Evict least-recently-used tiles until `incoming` fits."""
        evicted = 0
        while self._ready_bytes + incoming_bytes > self.ram_budget:
            if max_evict is not None and evicted >= max_evict:
                return
            victim = None
            for row in self._ready:
                if row != protect:
                    victim = row
                    break
            if victim is None:
                return
            t = self._ready.pop(victim)
            self._ready_bytes -= t["gpu_bytes"]
            self._state[victim] = EVICTABLE
            self.stats_counters["evicted"] += 1
            evicted += 1

    # --------------------------------------------------------------- render
    def snapshot(self, rows=None):
        """Non-blocking view of ready tiles for the render thread.

        Returns {row: tile}. Takes the lock only long enough to copy the
        OrderedDict - it never waits for a decode to finish.
        """
        with self._lock:
            if rows is None:
                return dict(self._ready)
            return {r: self._ready[r] for r in rows if r in self._ready}

    def promote_to_gpu(self, rows):
        """Mark tiles as GPU-resident; they then stay resident (Phase 10)."""
        with self._lock:
            for r in rows:
                if r in self._state:
                    self._state[r] = GPU_RESIDENT

    def release_gpu(self, rows):
        with self._lock:
            for r in rows:
                if r in self._state and self._state[r] == GPU_RESIDENT:
                    self._state[r] = EVICTABLE

    def state_of(self, row):
        with self._lock:
            return self._state.get(row, UNLOADED)

    def states(self):
        with self._lock:
            return dict(self._state)

    def depth(self):
        with self._lock:
            return {"queued": len(self._pending), "inflight": len(self._inflight),
                    "ready": len(self._ready), "bytes": self._ready_bytes}

    def stats(self):
        with self._lock:
            c = dict(self.stats_counters)
            lat = list(self._recent_latency)
            ready = len(self._ready)
            resident = sum(1 for v in self._state.values() if v == GPU_RESIDENT)
            pts = sum(t["count"] for t in self._ready.values())
        lat.sort()
        out = dict(c)
        out["resident_tiles"] = ready
        out["gpu_resident_tiles"] = resident
        out["resident_points"] = pts
        out["ram_used"] = c.get("_ram", 0)
        out["decode_mpts_s"] = (c["decode_points"] / 1e6 / c["decode_seconds"]
                                if c["decode_seconds"] > 1e-9 else 0.0)
        if lat:
            out["decode_latency_ms"] = lat[len(lat) // 2] * 1000.0
            out["decode_latency_p95_ms"] = lat[int(len(lat) * 0.95)] * 1000.0
        out["hit_rate"] = (c["completed"] / max(1, c["submitted"]))
        return out