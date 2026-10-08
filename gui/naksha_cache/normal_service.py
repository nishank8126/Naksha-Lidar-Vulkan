"""normal_service.py - PHASE 6D: normal generation as a background service.

THE PROBLEM THIS SOLVES. Shading needs a normal stream. When the sidecar exists
everything is already instant (Phase 6C). When it does NOT exist, readiness
reported the derived `normal` stream as PENDING for ever: `normal_builder` was
an OFFLINE script nothing under `gui/` ever called, so Shading could only ever
appear after the user ran a CLI by hand. The display-mode lifecycle was fine -
the missing piece was a PRODUCER.

WHAT THIS IS
============
One bounded worker that runs the EXISTING builder (`normal_builder`) off the GUI
thread, visible-first, with real progress, resumable and cancel-safe. It adds no
new display-mode path and no new file format: it drives `build_normal_cache` and
hands the finished sidecar back through the same `open_normal_cache` ->
`attach_normal_cache` path a stored cache already uses.

CONTRACTS
=========
* NO GUI BLOCKING. `start()` returns immediately; all work happens off-thread.
* VISIBLE-FIRST. LOD0 tiles covering the current view are built before the rest,
  so Shading can become visible long before the whole dataset is finished.
* COHERENT. "Enough" is decided by the caller (the manager), which keeps the
  previous representation on screen until the VISIBLE set is complete - no
  rectangular mosaics of shaded and unshaded blocks.
* BOUNDED. One worker. The builder is memory-bounded by construction (one tile
  at a time), so this never becomes a second streaming engine.
* CANCEL-SAFE. Cancel stops between tiles and KEEPS the resumable work files, so
  a restart continues instead of starting over.
* ATOMIC PUBLISH. The builder writes `.tmp` and renames on commit, so an
  interrupted session can never leave a half-valid sidecar behind.
* HONEST PROGRESS. Percentages come from tiles actually finished, never from a
  timer.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Dict, List, Optional, Sequence

# States a build can be in. "READY" is the only one that promises usable
# normals; everything else must be reported as-is to the user.
PENDING = "PENDING"
RUNNING = "RUNNING"
READY = "READY"
CANCELLED = "CANCELLED"
FAILED = "FAILED"
MISS = "MISS"

# Progress is reported at most this often to the UI. A per-tile callback on a
# 393-tile build would repaint the status bar 393 times for no information gain.
PROGRESS_MIN_INTERVAL_S = 0.25


# --------------------------------------------------------------------------- #
# PART C3 - BLOCK-LEVEL STATE                                                 #
# --------------------------------------------------------------------------- #
class NormalBlockState(IntEnum):
    """O(1) per-tile lifecycle inside a single build run (Phase 6D.3)."""
    NOT_REQUESTED = 0
    QUEUED = 1
    BUILDING = 2
    READY_RUNTIME = 3
    PERSISTED = 4
    FAILED = 5


class BlockStateTracker:
    """O(1) block state lookup, no duplicate requests (Part C3)."""

    __slots__ = ("_states", "_counts", "_ordered")

    def __init__(self, n_tiles: int = 0):
        self._states: Dict[int, int] = {}
        self._counts: Dict[int, int] = {s: 0 for s in range(
            int(NormalBlockState.NOT_REQUESTED),
            int(NormalBlockState.FAILED) + 1)}
        self._ordered: List[int] = []
        self._resize(n_tiles)

    def _resize(self, n: int) -> None:
        for i in range(n):
            if i not in self._states:
                self._states[i] = int(NormalBlockState.NOT_REQUESTED)
                self._ordered.append(i)
                self._counts[int(NormalBlockState.NOT_REQUESTED)] += 1

    def transition(self, tile_id: int, to_state) -> int:
        """Move tile_id to to_state. Returns previous state. No-op if same."""
        to = int(to_state)
        if tile_id not in self._states:
            self._states[tile_id] = int(NormalBlockState.NOT_REQUESTED)
            self._counts[int(NormalBlockState.NOT_REQUESTED)] += 1
            self._ordered.append(tile_id)
        prev = int(self._states.get(
            tile_id, int(NormalBlockState.NOT_REQUESTED)))
        if prev == to:
            return prev
        self._states[tile_id] = to
        self._counts[prev] = max(0, self._counts.get(prev, 0) - 1)
        self._counts[to] = self._counts.get(to, 0) + 1
        return prev

    def request(self, tile_id: int) -> bool:
        """Idempotent: True if tile was NOT_REQUESTED (now QUEUED)."""
        return self.transition(tile_id, NormalBlockState.QUEUED) \
            == int(NormalBlockState.NOT_REQUESTED)

    def is_building(self, tile_id: int) -> bool:
        return self._states.get(tile_id, 0) == int(
            NormalBlockState.BUILDING)

    def counts(self) -> dict:
        return dict(self._counts)

    def ready_runtime(self) -> int:
        return self._counts.get(int(NormalBlockState.READY_RUNTIME), 0)

    def persisted(self) -> int:
        return self._counts.get(int(NormalBlockState.PERSISTED), 0)

    def failed(self) -> int:
        return self._counts.get(int(NormalBlockState.FAILED), 0)

    def clear(self) -> None:
        self._states.clear()
        self._counts = {s: 0 for s in range(
            int(NormalBlockState.NOT_REQUESTED),
            int(NormalBlockState.FAILED) + 1)}
        self._ordered.clear()


@dataclass
class ShadingTimestamps:
    """T0..T8 acceptance evidence for the shading pipeline (Part C11)."""
    t0_shading_click: float = 0.0
    t1_service_start: float = 0.0
    t2_first_normal_calc: float = 0.0
    t3_first_normal_runtime: float = 0.0
    t4_visible_normals_coherent: float = 0.0
    t5_pending_ready: float = 0.0
    t6_atomic_swap: float = 0.0
    t7_first_shaded: float = 0.0
    t8_sidecar_commit: float = 0.0

    def elapsed(self, name: str) -> Optional[float]:
        t = getattr(self, name, 0.0)
        if t > 0.0 and self.t0_shading_click > 0.0:
            return round(t - self.t0_shading_click, 3)
        return None

    def report(self) -> dict:
        names = ["t0_shading_click", "t1_service_start",
                 "t2_first_normal_calc", "t3_first_normal_runtime",
                 "t4_visible_normals_coherent", "t5_pending_ready",
                 "t6_atomic_swap", "t7_first_shaded",
                 "t8_sidecar_commit"]
        e = {}
        for i, n in enumerate(names):
            e[f"T{i}"] = self.elapsed(n)
        e["T7_lt_T8"] = (self.t7_first_shaded > 0 and self.t8_sidecar_commit > 0
                         and self.t7_first_shaded < self.t8_sidecar_commit)
        return e

    def mark(self, name: str, t: Optional[float] = None) -> float:
        """Record timestamp. First-writer wins."""
        if t is None:
            t = time.perf_counter()
        if getattr(self, name, 0.0) == 0.0:
            setattr(self, name, t)
        return t


@dataclass
class NormalBuildStatus:
    """What the UI may show. Every field is measured, never assumed."""
    state: str = MISS
    block_counts: dict = field(default_factory=dict)
    dataset: str = ""
    reason: str = ""
    tiles_total: int = 0
    tiles_done: int = 0
    percent: int = 0
    elapsed_s: float = 0.0
    eta_s: float = 0.0
    message: str = ""
    finished: bool = False
    resumed: bool = False

    def as_line(self) -> str:
        if self.state == RUNNING:
            return (f"Generating shading normals... {self.percent}% "
                    f"({self.tiles_done}/{self.tiles_total} tiles)")
        if self.state == READY:
            return "Shading normals ready"
        if self.state == CANCELLED:
            return "Shading normal generation cancelled (resumable)"
        if self.state == FAILED:
            return f"Shading normal generation failed: {self.reason}"
        return "Shading normals: not generated"


class NormalBuildService:
    """Runs ONE normal build in the background. Thread-safe, cancel-safe."""

    def __init__(self, *, builder: Optional[Callable] = None,
                 on_progress: Optional[Callable[[NormalBuildStatus], None]] = None,
                 on_finished: Optional[Callable[[NormalBuildStatus], None]] = None):
        self._builder = builder
        self._on_progress = on_progress
        self._on_finished = on_finished
        self._on_block_ready = None
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._status = NormalBuildStatus()
        self._last_emit = 0.0
        self.runs_started = 0
        self.runs_finished = 0
        self.block_tracker = BlockStateTracker()
        self.timestamps = ShadingTimestamps()

    def set_callbacks(self, on_progress=None, on_finished=None) -> None:
        """Attach/append the UI listeners. The manager installs its own here.

        Kept as a setter (rather than constructor-only) so a caller can build the
        service first and wire it to the manager that will consume the result -
        which is the order the application uses.
        """
        if on_progress is not None:
            self._on_progress = on_progress
        if on_finished is not None:
            self._on_finished = on_finished

    def set_block_callback(self, callback) -> None:
        """Attach a bounded, thread-safe consumer for completed normal blocks."""
        self._on_block_ready = callback

    # ---- introspection (read at any time, from any thread) ----------------
    @property
    def status(self) -> NormalBuildStatus:
        with self._lock:
            s = self._status
            return NormalBuildStatus(**vars(s))

    @property
    def running(self) -> bool:
        t = self._thread
        return bool(t is not None and t.is_alive())

    # ---- control ---------------------------------------------------------
    def start(self, dataset: str, *, priority_nodes: Optional[Sequence[int]] = None,
              resume: bool = True) -> NormalBuildStatus:
        """Begin a background build. Returns the status immediately.

        Calling `start` while a build is running for the SAME dataset is a no-op
        (idempotent), so a repeated Shading click cannot spawn a second worker
        over the same work files.
        """
        ds = str(dataset or "")
        now = time.perf_counter()
        with self._lock:
            if self.running and self._status.dataset == ds:
                return NormalBuildStatus(**vars(self._status))
            if self.running:
                raise RuntimeError("stop the current normal build before changing dataset")
            self._cancel = threading.Event()
            cancel = self._cancel
            self.timestamps.mark("t0_shading_click", now)
            self._status = NormalBuildStatus(state=RUNNING, dataset=ds,
                                             message="starting")
            self._last_emit = 0.0
        self.runs_started += 1
        thread = threading.Thread(target=self._run, args=(ds, list(priority_nodes or ()),
                                                          bool(resume), cancel),
                                  name="naksha-normal-build", daemon=True)
        with self._lock:
            self._thread = thread
        thread.start()
        self.timestamps.mark("t1_service_start")
        return self.status

    def cancel(self, wait: bool = False, timeout: float = 5.0) -> bool:
        """Ask the worker to stop between tiles. Work files are KEPT."""
        self._cancel.set()
        if wait and self._thread is not None:
            self._thread.join(timeout=timeout)
        with self._lock:
            if self._status.state == RUNNING:
                self._status.state = CANCELLED
                self._status.finished = True
                self._status.message = "cancelled"
        return True

    def wait(self, timeout: Optional[float] = None) -> bool:
        t = self._thread
        if t is None:
            return True
        t.join(timeout=timeout)
        return not t.is_alive()

    def shutdown(self, timeout: float = 5.0) -> bool:
        """C2: graceful shutdown of the background normal builder.

        Cancels any running build, joins the worker thread within *timeout*,
        and flushes/closes work resources. Resumable work files are preserved.
        Returns True if the worker stopped cleanly within the timeout.
        """
        self._cancel.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
            alive = t.is_alive()
            if alive:
                print(f"[NORMAL SERVICE] shutdown: worker did not stop "
                      f"within {timeout}s", flush=True)
            with self._lock:
                if self._status.state == RUNNING:
                    self._status.state = CANCELLED
                    self._status.finished = True
                    self._status.message = "shutdown cancelled"
            return not alive
        return True

    # ---- worker ----------------------------------------------------------
    def _emit(self, force: bool = False) -> None:
        now = time.perf_counter()
        if not force and (now - self._last_emit) < PROGRESS_MIN_INTERVAL_S:
            return
        self._last_emit = now
        cb = self._on_progress
        if cb is not None:
            try:
                cb(self.status)
            except Exception:                                     # noqa: BLE001
                pass

    def _run(self, dataset: str, priority_nodes: List[int], resume: bool,
             cancel: threading.Event) -> None:
        t0 = time.perf_counter()
        builder = self._builder
        if builder is None:
            from .normal_builder import build_normal_cache as builder

        def _progress(tiles_done: int, tiles_total: int,
                      message: str) -> None:
            with self._lock:
                self._status.tiles_done = int(tiles_done)
                self._status.tiles_total = int(tiles_total)
                self._status.percent = int(100 * tiles_done / tiles_total) \
                    if tiles_total else 0
                self._status.elapsed_s = time.perf_counter() - t0
                if tiles_done:
                    per = self._status.elapsed_s / max(tiles_done, 1)
                    self._status.eta_s = per * max(tiles_total - tiles_done, 0)
                self._status.message = message
            self._emit()

        try:
            kwargs = dict(resume=bool(resume), priority_nodes=priority_nodes or None,
                          cancel=cancel, progress=_progress)
            if self._on_block_ready is not None:
                import inspect
                parameters = inspect.signature(builder).parameters
                if "on_block_ready" in parameters or any(
                        p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
                    kwargs["on_block_ready"] = self._on_block_ready
            result = builder(dataset, **kwargs)
        except Exception as exc:                                  # noqa: BLE001
            with self._lock:
                self._status.state = FAILED
                self._status.reason = repr(exc)
                self._status.finished = True
                self._status.elapsed_s = time.perf_counter() - t0
            self._emit(force=True)
            self._finish()
            return
        with self._lock:
            self._status.elapsed_s = time.perf_counter() - t0
            self._status.finished = True
            self._status.resumed = bool(getattr(result, "get", lambda *_: False)("resumed"))
            sidecar = ""
            try:
                sidecar = str(result.get("sidecar") or "")
            except Exception:
                sidecar = ""
            if cancel.is_set():
                self._status.state = CANCELLED
                self._status.message = "cancelled; work files kept for resume"
            elif sidecar and os.path.isfile(sidecar):
                self._status.state = READY
                self._status.percent = 100
                self._status.message = "ready"
                self.timestamps.mark("t8_sidecar_commit")
            else:
                self._status.state = FAILED
                self._status.reason = str(result.get("reason") or "build produced no sidecar")
        self._emit(force=True)
        self._finish()

    def _finish(self) -> None:
        self.runs_finished += 1
        cb = self._on_finished
        if cb is not None:
            try:
                cb(self.status)
            except Exception:                                     # noqa: BLE001
                pass
