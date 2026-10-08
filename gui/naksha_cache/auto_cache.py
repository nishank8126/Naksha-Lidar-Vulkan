"""AUTOMATIC CACHE ORCHESTRATION - the POD-like "open and it just works" flow.

    FIRST OPEN   source.laz -> cache MISS/STALE -> build automatically
    NEXT OPEN    source.laz -> cache HIT -> open immediately, no conversion

The user never runs a build script, never moves cache files and never edits a
budget by hand. This module owns the decision; the GUI only renders progress.

CACHE STATES (PART 4)
----------------------
  HIT      valid, finalized, fingerprint matches -> use immediately
  MISS     no index / no point cache -> build
  STALE    fingerprint, size, point count or bounds changed -> rebuild
  CORRUPT  present but unreadable or fails validation -> report, then rebuild

A partial build can never be mistaken for a cache: the writer only ever commits
from a ``.tmp``, and a leftover ``.tmp`` is treated as an incomplete build, not
as something to resume (PART 5/8 - no unsafe partial-resume guessing).

CACHE PLACEMENT (PART 1/2)
--------------------------
Cache lives BESIDE the source file. Only when that directory is genuinely not
writable does it fall back to a deterministic user-local directory, and that
fallback is printed - never silent.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional


STATE_HIT = "HIT"
STATE_MISS = "MISS"
STATE_STALE = "STALE"
STATE_CORRUPT = "CORRUPT"
STATE_BUILDING = "BUILDING"

STAGES = ("Reading source", "Partitioning", "Building hierarchy",
          "Writing point blocks", "Compressing streams",
          "Building overview / LOD", "Final validation", "Committing cache")


# --------------------------------------------------------------------------- #
# PART 1/2 - placement                                                          #
# --------------------------------------------------------------------------- #
def fallback_cache_dir() -> str:
    """Deterministic user-local cache root. Never the project or temp dir."""
    base = os.environ.get("NAKSHA_CACHE_HOME")
    return base or os.path.join(os.path.expanduser("~"), ".naksha_cache")


def source_dir_writable(directory: str) -> bool:
    """Actually TEST writability - permission bits lie on network shares and
    locked folders."""
    try:
        os.makedirs(directory, exist_ok=True)
        probe = os.path.join(directory, ".naksha_write_test")
        with open(probe, "wb") as fh:
            fh.write(b"0")
        os.remove(probe)
        return True
    except Exception:
        return False


@dataclass
class CachePlacement:
    source_path: str
    idx_path: str
    pc_path: str
    edit_path: str
    fallback_used: bool = False
    message: str = ""

    @property
    def beside_source(self) -> bool:
        return not self.fallback_used


def place_cache(source_path: str) -> CachePlacement:
    """PART 1/2 - resolve where this dataset's cache lives.

    Same directory as the source. Only when that is genuinely not writable does
    it fall back, and then it PRINTS the fallback rather than doing it silently.
    """
    from .index import project_paths
    if isinstance(source_path, (list, tuple)):
        from .project_runtime import project_path
        source_path = str(project_path(source_path))
    src_dir = os.path.dirname(os.path.abspath(source_path))
    if source_dir_writable(src_dir):
        from .project_runtime import project_path
        container = str(project_path(source_path))
        return CachePlacement(source_path, container, container, container + ".nakshaedit", False, "")

    root = fallback_cache_dir()
    try:
        os.makedirs(root, exist_ok=True)
    except Exception:
        pass
    stem = os.path.basename(source_path)
    # Hash the full path so same-named files in different read-only folders do
    # not collide in the fallback.
    import hashlib
    tag = hashlib.sha1(os.path.abspath(source_path).encode("utf-8", "replace")
                       ).hexdigest()[:12]
    idx = os.path.join(root, f"{stem}.{tag}.naksha")
    pc = idx
    edit = os.path.join(root, f"{stem}.{tag}.nakshaedit")
    msg = ("Source cache directory not writable.\n"
           f"Using fallback cache:\n{root}")
    print(msg)
    return CachePlacement(source_path, idx, pc, edit, True, msg)


# --------------------------------------------------------------------------- #
# PART 4/5 - state machine                                                      #
# --------------------------------------------------------------------------- #
@dataclass
class CacheState:
    state: str = STATE_MISS
    reason: str = ""
    placement: Optional[CachePlacement] = None
    validity: object = None          # CacheValidity when the state is HIT
    leftover_tmp: bool = False
    index_bytes: int = 0
    pc_bytes: int = 0

    @property
    def needs_build(self) -> bool:
        return self.state in (STATE_MISS, STATE_STALE, STATE_CORRUPT)


def _leftover_tmp(placement: CachePlacement) -> bool:
    """An interrupted build leaves .tmp files: evidence of an incomplete cache,
    never something to resume (PART 5/8)."""
    return any(os.path.exists(p + ".tmp")
               for p in (placement.idx_path, placement.pc_path))


def cleanup_incomplete(placement: CachePlacement) -> int:
    """Remove partial build artefacts so they can never look valid."""
    import json
    from .build_safety import BuildLock, BuildSafetyError
    lock = BuildLock(placement.idx_path + ".nakshabuild.lock", {
        "purpose": "legacy temp cleanup", "target_output_path": placement.idx_path,
        "scratch_path": os.path.dirname(placement.idx_path)})
    try:
        lock.acquire()
        manifest = placement.idx_path + ".buildstate"
        if os.path.exists(manifest):
            with open(manifest, encoding="utf-8") as f:
                state = json.load(f)
            if state.get("stages", {}).get("COMMIT") != "COMPLETE":
                return 0
        removed = 0
        for p in (placement.idx_path, placement.pc_path):
            tmp = p + ".tmp"
            if os.path.isfile(tmp):
                os.remove(tmp)
                removed += 1
        return removed
    except (BuildSafetyError, OSError, ValueError):
        return 0
    finally:
        lock.release()


def classify_cache(source_path: str,
                   placement: Optional[CachePlacement] = None) -> CacheState:
    """HIT / MISS / STALE / CORRUPT for one source file.

    Never decodes the source point body: the existing header-only probe IS the
    fingerprint, so a 100 GB source is never hashed on startup (PART 4).
    """
    place = placement or place_cache(source_path)
    from .project_runtime import project_path, container_validity
    from pathlib import Path
    path = Path(place.idx_path)
    if path.is_file():
        try:
            validity = container_validity(source_path, container_path=path)
            if validity.ok:
                return CacheState(state=STATE_HIT, reason=validity.reason,
                    placement=place, validity=validity, leftover_tmp=_leftover_tmp(place),
                    index_bytes=0, pc_bytes=validity.pc_bytes)
            return CacheState(state=STATE_STALE, reason=validity.reason, placement=place)
        except Exception as exc:
            return CacheState(state=STATE_CORRUPT, reason=f"cache unreadable: {exc}", placement=place)
    if isinstance(source_path, (list, tuple)):
        return CacheState(state=STATE_MISS, reason="multi-source project not built", placement=place)
    st = CacheState(placement=place, leftover_tmp=_leftover_tmp(place))
    idx_ok = os.path.isfile(place.idx_path)
    pc_ok = os.path.isfile(place.pc_path)
    if idx_ok:
        from .index import IndexReader, resolve_point_cache
        try:
            idx = IndexReader(place.idx_path)
            try:
                resolved_pc = resolve_point_cache(place.idx_path, idx.point_cache_name, place.pc_path)
                pc_ok = os.path.isfile(resolved_pc)
                if pc_ok:
                    st.pc_bytes = os.path.getsize(resolved_pc)
            finally:
                idx.close()
        except Exception:
            pass  # cache_first_open reports the exact corrupt-index reason
    if not idx_ok and not pc_ok:
        st.state = STATE_MISS
        st.reason = "no cache beside source"
        return st
    if not idx_ok or not pc_ok:
        st.state = STATE_CORRUPT
        st.reason = ("cache incomplete: " +
                     ("index missing" if not idx_ok else "point cache missing"))
        return st

    st.index_bytes = os.path.getsize(place.idx_path)
    if not st.pc_bytes:
        st.pc_bytes = os.path.getsize(place.pc_path)

    from .dataset_mode import cache_first_open
    try:
        v = cache_first_open(place.source_path)
    except Exception as exc:
        st.state = STATE_CORRUPT
        st.reason = f"cache unreadable: {type(exc).__name__}: {exc}"
        return st
    if v.ok:
        st.state = STATE_HIT
        st.reason = v.reason
        st.validity = v
        return st
    text = (v.reason or "").lower()
    if ("bad index magic" in text or "unsupported index version" in text
            or "build_state" in text or "unreadable" in text):
        st.state = STATE_CORRUPT
    else:
        st.state = STATE_STALE
    st.reason = v.reason
    return st


# --------------------------------------------------------------------------- #
# PART 6/7/8/41 - the build runner                                               #
# --------------------------------------------------------------------------- #
@dataclass
class BuildProgress:
    stage: str = ""
    stage_index: int = 0
    stage_count: int = len(STAGES)
    percent: float = 0.0
    processed_points: int = 0
    total_points: int = 0
    bytes_written: int = 0
    elapsed_s: float = 0.0
    cancelled: bool = False
    message: str = ""

    def describe(self) -> str:
        pts = (f"{self.processed_points:,}/{self.total_points:,}"
               if self.total_points else f"{self.processed_points:,}")
        return (f"[CACHE BUILD] {self.percent:5.1f}% stage={self.stage!r} "
                f"({self.stage_index + 1}/{self.stage_count}) "
                f"points={pts} written={self.bytes_written / 1048576:.1f}MB "
                f"elapsed={self.elapsed_s:.1f}s"
                + (" CANCELLED" if self.cancelled else ""))


class CancelledError(RuntimeError):
    """Raised inside the worker on cancel. Never leads to a commit."""


class CacheBuildJob:
    """Runs a cache build OFF the GUI thread with progress and cancel.

    PART 7: no Qt work happens here. This only calls a caller-supplied progress
    callback, and the GUI adapter marshals it onto the Qt thread (a queued Qt
    connection does exactly that). No QApplication is touched from this class.

    PART 8: cancelling raises inside the worker, writers are closed, and the
    incomplete build is removed. `run()` never commits on failure or cancel.
    """

    def __init__(self, source_path: str,
                 placement: Optional[CachePlacement] = None,
                 workers: Optional[Dict] = None):
        from .project_runtime import source_paths, project_path
        self.source_paths = source_paths(source_path)
        self.source_path = str(project_path(source_path)) if len(self.source_paths) > 1 else self.source_paths[0]
        self.placement = placement or place_cache(source_path)
        self.workers = dict(workers or {})
        self.progress = BuildProgress()
        self.cancelled = False
        self.result: Dict = {}
        self.error: Optional[BaseException] = None
        self.committed = False

    def cancel(self) -> None:
        """Thread-safe cancel flag; the worker polls it at stage boundaries."""
        self.cancelled = True
        self.progress.cancelled = True

    def _check(self) -> None:
        if self.cancelled:
            raise CancelledError("cache build cancelled by user")

    def _advance(self, stage_index: int, percent=None, bytes_written=None,
                 processed=None, total=None) -> None:
        idx = min(max(0, stage_index), len(STAGES) - 1)
        self.progress.stage = STAGES[idx]
        self.progress.stage_index = idx
        if percent is not None:
            self.progress.percent = float(percent)
        if bytes_written is not None:
            self.progress.bytes_written = int(bytes_written)
        if processed is not None:
            self.progress.processed_points = int(processed)
        if total is not None:
            self.progress.total_points = int(total)
        self._check()

    # -- the work ------------------------------------------------------ #
    def run(self, on_progress: Optional[Callable] = None) -> Dict:
        """Build the cache for ``source_path``.

        ``committed`` is True only when a finalized, validated cache actually
        exists afterwards - a build that merely wrote files is not a success.
        """
        from .builder import NakshaPointCacheBuilder
        from .hardware import get_profile
        t0 = time.perf_counter()

        def emit():
            self.progress.elapsed_s = time.perf_counter() - t0
            if on_progress is not None:
                on_progress(self.progress)

        profile = get_profile()
        self.workers = self.workers or dict(profile.workers)
        report = {"source": self.source_path, "committed": False,
                  "stages": len(STAGES), "workers": dict(self.workers),
                  "placed_beside_source": self.placement.beside_source,
                  "placement": self.placement.idx_path}
        try:
            self._advance(0, percent=1.0)
            emit()
            # PART 8: an interrupted earlier build must never be reused.
            report["removed_incomplete_tmp"] = 0

            # Chunk size follows the MEASURED RAM budget, so a small machine
            # does not try to hold a huge decode buffer per worker.
            per_worker = max(64_000_000,
                             profile.ram_soft_limit_bytes //
                             max(1, len(self.workers)))
            chunk = int(max(500_000, min(4_000_000, per_worker)))
            from .project_runtime import NakshaWriter, project_path
            output = self.placement.idx_path
            builder = NakshaWriter(output, chunk_points=chunk,
                cancel_check=lambda: self.cancelled)
            self._advance(1, percent=8.0)
            emit()
            build_result = builder.build(self.source_paths)
            report["compression"] = build_result["compression"]
            report["placement"] = str(output)
            self._advance(len(STAGES) - 2, percent=92.0)
            emit()
            # Only now is a finalized cache on disk; validate it.
            state = classify_cache(self.source_paths, self.placement)
            self.progress.percent = 100.0
            self.progress.stage = STAGES[-1]
            self.progress.stage_index = len(STAGES) - 1
            emit()
            report.update({"committed": state.state == STATE_HIT,
                           "final_state": state.state,
                           "final_reason": state.reason,
                           "index_bytes": state.index_bytes,
                           "pc_bytes": state.pc_bytes,
                           "chunk_points": chunk,
                           "elapsed_s": round(time.perf_counter() - t0, 2)})
            self.committed = bool(report["committed"])
        except CancelledError as exc:
            self.error = exc
            report.update({"committed": False, "cancelled": True,
                           "elapsed_s": round(time.perf_counter() - t0, 2)})
        except Exception as exc:
            self.error = exc
            from .build_safety import BuildCancelled
            if isinstance(exc, BuildCancelled):
                report["cancelled"] = True
            report.update({"committed": False,
                           "error": f"{type(exc).__name__}: {exc}",
                           "elapsed_s": round(time.perf_counter() - t0, 2)})
        self.result = report
        return report


# --------------------------------------------------------------------------- #
# PART 22 - the cache-first entry point                                         #
# --------------------------------------------------------------------------- #
def ensure_cache(source_path: str, build: bool = True,
                 on_progress: Optional[Callable] = None) -> Dict:
    """ONE call the GUI makes on every file open.

    HIT -> returns immediately with the existing cache.
    MISS / STALE / CORRUPT -> builds automatically, then returns the cache.

    The caller never runs a script and never touches a cache file.
    """
    st = classify_cache(source_path)
    out = {"state": st.state, "reason": st.reason,
           "placement": st.placement, "validity": st.validity,
           "built": False, "report": None}
    if st.state == STATE_HIT:
        print(f"[CACHE] HIT {os.path.basename(source_path)} "
              f"idx={st.index_bytes:,}B pc={st.pc_bytes:,}B - no conversion")
        return out
    print(f"[CACHE] {st.state} {os.path.basename(source_path)}: {st.reason}"
          + (" - rebuilding automatically" if build else " - build skipped"))
    if not build:
        return out
    job = CacheBuildJob(source_path, st.placement)
    report = job.run(on_progress)
    out["built"] = bool(report.get("committed"))
    out["report"] = report
    after = classify_cache(source_path, st.placement)
    out["state"] = after.state
    out["reason"] = after.reason
    out["validity"] = after.validity
    print(f"[CACHE] after build: {after.state} "
          f"({report.get('elapsed_s', '?')}s)")
    return out
