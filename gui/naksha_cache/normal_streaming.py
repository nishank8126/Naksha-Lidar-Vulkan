"""normal_streaming.py - selective STORED-NORMAL streaming for Shaded Class.

WHY THIS MODULE EXISTS
======================
The stored ``.nakshanorm`` sidecar is byte-aligned with the NKPC blocks, and the
alignment validator proved that against the real 27M-point dataset. What it
could NOT prove is that the RUNTIME uses it, and it does not:

* nothing opened the sidecar at dataset open,
* ``NakshaStreamManager`` had no normal stream, no readiness concept and no
  normal bytes in its GPU budget,
* Shaded therefore drew only what was already resident - which after the first
  frame is the LOD4 overview (299,676 points). That is the whole "stuck at the
  overview" bug: not a LOD-selection failure, but the absence of any second
  attribute that a finer block would need to become worth activating.

DESIGN RULES THAT ARE NOT NEGOTIABLE
====================================
* XYZ/CLASS/NORMAL point i must be the same point. Normals are concatenated in
  the SAME key order ``_flush_gpu`` already uses; there is deliberately no
  second, normal-specific sort. A separate order is how a shading bug that no
  per-block test can see gets shipped.
* An incomplete FINE block never replaces a complete COARSE block. That is the
  difference between "detail appears a moment later" and "a black frame".
* Normals count against the GPU budget at 4 bytes/resident point. Evicting them
  is allowed; uploading all 28.86M at once is not.
* The oct16 payload is NEVER expanded to float32x3 on the CPU. The GPU decodes.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from .format import ATTR_XYZ, ATTR_CLASSIFICATION, ATTR_INTENSITY, ATTR_RGB

# NORMAL is a pseudo-attribute, not an NKPC stream. It is a string so it can
# never be silently OR-ed into an `only_attrs` bitmask and sent to the NKPC
# reader, which would reject it.
ATTR_NORMAL = "normal"

BYTES_PER_NORMAL = 4
DEFAULT_NORMAL_RAM_BUDGET = 512 * 1024 ** 2   # bounded; never a 110 MiB blob
NORMAL_GPU_BUDGET_FRACTION = 0.25            # of the total GPU budget
IDLE_REFINE_MS = 900.0                       # Part 14
# PHASE 4 (performance): raised from 300 ms after MEASUREMENT on
# test_classified_highprecision.laz with real Qt input: the camera flipped
# MOVING -> IDLE after 383 ms, so the cheap interaction draw path was SKIPPED for
# most of a wheel gesture while the user was still turning the wheel - and the
# manager re-submitted all 26.9M points.
#
# 300 ms is shorter than a single deliberate gesture step on a real mouse, so
# ordinary interaction was misclassified as idle. 900 ms covers a gap between
# gesture steps and still restores full detail promptly once input stops.
MOVING_TARGET_POINTS_PER_PIXEL = 0.35   # Part 1: 0.25-0.50 band
IDLE_TARGET_POINTS_PER_PIXEL = 1.0     # Part 1: 0.75-1.00 band
TARGET_MIN_POINTS_PER_PIXEL = 0.5
COVERAGE_TO_RELEASE_OVERVIEW = 0.90          # Part 13
# Threshold for actually RELEASING a coarse block from the draw set.
#
# This is deliberately far stricter than COVERAGE_TO_RELEASE_OVERVIEW. A coarse
# block may only be dropped when its replacement covers essentially ALL of it:
# every percentage point left uncovered becomes a region with no active
# representation, which renders as a black rectangle. Measured on the real
# 1000x1000 m dataset, the finer frontier reaches 0.9951 union coverage and
# stops - the remaining ~0.5% is a peripheral strip (Y ~685885-685995) that
# node bounding boxes claim but no selected block covers. Releasing the
# overview at 0.9951 would open a visible gap along the whole edge to save one
# block of 299,676 points, so the coarse floor is kept until that is genuinely
# fixed. Visual continuity outranks the point budget.
COVERAGE_TO_RELEASE_BLOCK = 0.999


# --------------------------------------------------------------------------- #
# PART 2 - ONE central attribute-requirement function.                         #
# --------------------------------------------------------------------------- #
def required_attributes(display_mode: str) -> tuple:
    """Attributes a display mode needs. The ONLY place this mapping lives.

    PART 4 - MINIMAL per mode. The previous RGB entry demanded
    (XYZ, RGB, CLASSIFICATION, INTENSITY): to draw RGB it also demanded CLASS
    and INTENSITY. On any dataset missing one of those, every block then blocked
    on an attribute the mode does not actually draw, and its coarse parent could
    never retire - the "disconnected islands with black gaps" failure. A mode now
    requests ONLY what it draws.

    This mapping is NOMINAL. It is intersected with what the dataset really
    contains by ``DatasetCapabilities.intersect`` before anything is read or
    blocked on, so an absent optional attribute can never wedge the pipeline.

    Elevation reads position Z and a uniform LUT; it needs no class or intensity
    stream. Keep this contract aligned with display_modes.MODE_ELEVATION.
    """
    mode = str(display_mode or "").strip().lower()
    if mode in ("neutral", "default", "gray", "grey"):
        # PART 2: XYZ ONLY. The default view must never depend on RGB,
        # classification, intensity, normals or a PTC.
        return (ATTR_XYZ,)
    if mode == "depth":
        # PART 10: legacy depth is a camera-space grayscale shading of the same
        # XYZ; it reads no stored attribute.
        return (ATTR_XYZ,)
    if mode in ("shaded", "shaded_class", "shaded_class_instant"):
        return (ATTR_XYZ, ATTR_CLASSIFICATION, ATTR_NORMAL)
    if mode in ("classification", "class"):
        return (ATTR_XYZ, ATTR_CLASSIFICATION)
    if mode == "rgb":
        # RGB is RGB. CLASS and INTENSITY belong to their own modes.
        return (ATTR_XYZ, ATTR_RGB)
    if mode == "intensity":
        return (ATTR_XYZ, ATTR_INTENSITY)
    if mode == "elevation":
        return (ATTR_XYZ,)
    return (ATTR_XYZ,)


def requires_normal(display_mode: str) -> bool:
    return ATTR_NORMAL in required_attributes(display_mode)


def nkpc_attrs(attrs: Iterable) -> tuple:
    """Filter a requirement tuple down to real NKPC streams."""
    return tuple(a for a in attrs if isinstance(a, int))
# --------------------------------------------------------------------------- #
# PART 1 - NORMAL CACHE OPEN.                                                   #
# --------------------------------------------------------------------------- #
@dataclass
class NormalCacheReport:
    status: str = "MISS"          # HIT | MISS | STALE | CORRUPT
    path: str = ""
    blocks: int = 0
    stored_normals: int = 0
    encoding: str = ""
    open_ms: float = 0.0
    bytes: int = 0
    reason: str = ""
    # PHASE 6C.4 - POINT-CACHE LAYOUT BINDING. `layout_bound` is True only when
    # this sidecar RECORDS a point-cache layout fingerprint and it matches the
    # one the open dataset reports. False means "readable, but it cannot prove
    # it belongs to this layout" (a pre-6C.4 sidecar) - reported, never
    # hard-rejected, because the project rule is that a new field may not make
    # an existing cache unusable. An actual MISMATCH is rejected earlier, as a
    # STALE open.
    layout_fingerprint: int = 0
    layout_bound: bool = True

    def as_lines(self) -> List[str]:
        return ["[NORMAL CACHE]", f"status: {self.status}",
                f"path: {self.path or '-'}", f"blocks: {self.blocks:,}",
                f"stored_normals: {self.stored_normals:,}",
                f"encoding: {self.encoding}", f"open_ms: {self.open_ms:.2f}",
f"bytes: {self.bytes:,}",
                f"layout_fingerprint: 0x{int(self.layout_fingerprint):016x}",
                f"layout_bound: {bool(self.layout_bound)}"]


_ENCODING_NAMES = {1: "OCT16_X2 (R16G16_SNORM, 4 B/point)"}


def assert_report_invariant(reader, rep: "NormalCacheReport") -> None:
    """STEP 2/5 - the state contract, enforced at runtime.

    The invariant that matters: a report can NEVER claim MISS/STALE/CORRUPT
    while carrying populated metadata or a live reader. That exact
    contradiction is what produced the false MISS (blocks=1339,
    stored_normals=28,860,837, open_ms measured - but status MISS), because
    the dataclass default is "MISS" and a success path that forgot to overwrite
    it still looked complete.

    Raises AssertionError on violation. This is cheap, runs once per open, and
    turns a silently-wrong runtime state into an immediate, obvious failure.
    """
    st = str(rep.status)
    if st == "MISS":
        assert reader is None, f"MISS must have no reader (got {reader!r})"
        assert int(rep.blocks) == 0, \
            f"MISS must report 0 blocks, got {int(rep.blocks)}"
        assert int(rep.stored_normals) == 0, \
            f"MISS must report 0 stored_normals, got {int(rep.stored_normals)}"
    elif st == "HIT":
        assert reader is not None, "HIT must have a live reader"
        assert int(rep.blocks) > 0, "HIT must report a non-zero block count"
        assert int(rep.stored_normals) > 0, \
            "HIT must report a non-zero stored normal count"
        assert not str(rep.reason), \
            f"HIT must carry an empty reason, got {rep.reason!r}"
    else:  # STALE / CORRUPT
        assert st in ("STALE", "CORRUPT"), f"unknown status {st!r}"
        assert reader is None, \
            f"{st} must not hand a reader to the renderer (got {reader!r})"
        assert int(rep.blocks) == 0, \
            f"{st} must zero blocks so no stale offsets can be used"
        assert int(rep.stored_normals) == 0, \
            f"{st} must zero stored_normals"
        assert str(rep.reason), f"{st} must explain itself"
    return None


def open_normal_cache(dataset: str, *, verify_source: bool = True,
                      expected_source_points: Optional[int] = None,
                      expected_layout_fingerprint: Optional[int] = None):
    """Open the sidecar DIRECTORY only. Returns ``(reader_or_None, report)``.

    The payload is never read here - only the 160-byte header and the block
    directory. That is what keeps a 110 MiB sidecar viable now and a multi-GB
    one viable later.
    """
    t0 = time.perf_counter()
    from .normals import NormalCacheReader, sidecar_path
    path = sidecar_path(dataset)
    rep = NormalCacheReport(path=str(path))
    if not path.is_file():
        rep.status = "MISS"
        rep.reason = f"no sidecar at {path}"
        rep.open_ms = (time.perf_counter() - t0) * 1000.0
        assert_report_invariant(None, rep)
        return None, rep
    reader = None
    try:
        reader = NormalCacheReader(
            dataset, verify_source=verify_source,
            expected_layout_fingerprint=expected_layout_fingerprint)
    except FileNotFoundError:
        rep.status = "MISS"
        rep.reason = "sidecar vanished between stat and open"
    except ValueError as exc:
        # A changed fingerprint is the expected, benign outcome after a
        # re-classification; a truncated/CRC-bad header is not. They must not
        # collapse together, because the remedy differs.
        msg = str(exc)
        rep.status = "STALE" if ("STALE" in msg or "fingerprint" in msg) \
            else "CORRUPT"
        rep.reason = msg
    except OSError as exc:
        rep.status = "CORRUPT"
        rep.reason = f"I/O error opening sidecar: {exc}"
    else:
        rep.blocks = int(reader.block_count)
        rep.stored_normals = int(reader.stored_normal_point_count)
        rep.encoding = _ENCODING_NAMES.get(int(reader.encoding),
                                           f"unknown({reader.encoding})")
        rep.bytes = int(reader.stored_bytes())
        if (expected_source_points is not None
                and int(reader.source_point_count) != int(expected_source_points)):
            # Built for a different point layout: its block offsets would point
            # at the wrong points, so refuse rather than shade nonsense.
            rep.status = "STALE"
            rep.reason = (f"sidecar built for {int(reader.source_point_count):,} "
                          f"source points, cache has "
                          f"{int(expected_source_points):,}")
            rep.blocks = rep.stored_normals = 0
            reader = None
        elif False:
            pass
        else:
            # PHASE 6C.4: the layout evidence, on the SUCCESS path only. 0 (or
            # `layout_bound False` when the point cache states a fingerprint the
            # sidecar cannot confirm) means "readable, but it cannot prove it
            # belongs to this point-cache layout" - reported, never silently
            # upgraded to production-valid.
            rep.layout_fingerprint = int(
                getattr(reader, "layout_fingerprint", 0) or 0)
            rep.layout_bound = bool(
                rep.layout_fingerprint
                and (not expected_layout_fingerprint
                     or int(expected_layout_fingerprint)
                     == rep.layout_fingerprint))
            # Only the SUCCESS path declares HIT. Defaulting the dataclass to
            # HIT and letting a later branch downgrade it is how a valid cache
            # ends up reported MISS - which would silently disable stored
            # normals while every other field still looks perfect.
            rep.status = "HIT"
    rep.open_ms = (time.perf_counter() - t0) * 1000.0
    # STEP 5: refuse to return a self-contradictory report. Every field below
    # is derived from the reader, so a status that disagrees with them is a bug
    # in this function - and it must surface here, not as a silently disabled
    # normal stream three layers downstream.
    assert_report_invariant(reader, rep)
    return reader, rep
# PART 4 - BYTE-BOUNDED NORMAL RAM CACHE.                                       #
# --------------------------------------------------------------------------- #
class NormalBlockCache:
    """LRU of packed oct16 blocks, bounded in BYTES.

    Keyed by ``(dataset_generation, block_id)`` so reopening a dataset can never
    serve a block whose id now means a different point range.
    """

    def __init__(self, budget_bytes: int = DEFAULT_NORMAL_RAM_BUDGET):
        self.budget_bytes = int(budget_bytes)
        self._blocks: "OrderedDict[Tuple[int, int], np.ndarray]" = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.new_blocks = 0

    @property
    def resident_bytes(self) -> int:
        return self._bytes

    @property
    def resident_blocks(self) -> int:
        return len(self._blocks)

    @property
    def resident_points(self) -> int:
        return sum(int(b.shape[0]) for b in self._blocks.values())

    def stats(self) -> dict:
        return {"hits": self.hits, "misses": self.misses,
                "resident_blocks": self.resident_blocks,
                "resident_bytes": self.resident_bytes,
                "resident_points": self.resident_points,
                "evictions": self.evictions, "new_blocks": self.new_blocks,
                "budget_bytes": self.budget_bytes}

    def get(self, generation: int, block_id: int) -> Optional[np.ndarray]:
        with self._lock:
            key = (int(generation), int(block_id))
            blk = self._blocks.get(key)
            if blk is None:
                self.misses += 1
                return None
            self._blocks.move_to_end(key)
            self.hits += 1
            return blk

    def contains(self, generation: int, block_id: int) -> bool:
        with self._lock:
            return (int(generation), int(block_id)) in self._blocks

    def put(self, generation: int, block_id: int, packed: np.ndarray) -> int:
        """Insert packed (N,2) int16. Returns bytes evicted to make room."""
        arr = np.ascontiguousarray(packed, dtype=np.int16)
        nbytes = int(arr.nbytes)
        evicted = 0
        with self._lock:
            key = (int(generation), int(block_id))
            if key in self._blocks:
                self._bytes -= int(self._blocks.pop(key).nbytes)
            self._blocks[key] = arr
            self._bytes += nbytes
            self.new_blocks += 1
            while self._bytes > self.budget_bytes and len(self._blocks) > 1:
                _, old = self._blocks.popitem(last=False)
                self._bytes -= int(old.nbytes)
                self.evictions += 1
                evicted += int(old.nbytes)
        return evicted

    def clear(self) -> None:
        with self._lock:
            self._blocks.clear()
            self._bytes = 0


# --------------------------------------------------------------------------- #
# PART 5 - PER-BLOCK ATTRIBUTE READINESS.                                       #
# --------------------------------------------------------------------------- #
@dataclass
class BlockReadiness:
    """Per-block attribute readiness for STORED shaded rendering."""
    block_id: int = -1
    key: Tuple[int, int] = (0, 0)
    xyz_ready: bool = False
    class_ready: bool = False
    normal_ready: bool = False

    @property
    def complete(self) -> bool:
        """A STORED shaded block is usable only when all three are ready."""
        return bool(self.xyz_ready and self.class_ready and self.normal_ready)

    def mark(self, *, xyz=False, cls=False, normal=False) -> "BlockReadiness":
        if xyz:
            self.xyz_ready = True
        if cls:
            self.class_ready = True
        if normal:
            self.normal_ready = True
        return self

    def missing(self) -> tuple:
        out = []
        if not self.xyz_ready:
            out.append("xyz")
        if not self.class_ready:
            out.append("class")
        if not self.normal_ready:
            out.append("normal")
        return tuple(out)
# --------------------------------------------------------------------------- #
# PART 3 - NORMAL BLOCK LOADING.                                                 #
# --------------------------------------------------------------------------- #
def load_normal_block(reader, cache: NormalBlockCache, generation: int,
                      block_id: int, *, verify_crc: bool = True
                      ) -> Tuple[Optional[np.ndarray], str]:
    """RAM hit, else sidecar seek + CRC -> packed int16 (N,2).

    Returns ``(packed, reason)``; reason is "" on success. A missing or corrupt
    normal must be REPORTED - silently rendering it as +Z produces a flat,
    plausible-looking ground plane instead of an error.
    """
    hit = cache.get(generation, block_id)
    if hit is not None:
        return hit, "ram_hit"
    if reader is None:
        return None, "no normal reader"
    try:
        if not reader.has_block(block_id):
            return None, f"sidecar has no block {block_id}"
        packed = reader.read_block_normals(block_id, verify_crc=verify_crc)
    except Exception as exc:                       # CRC / short read / I/O
        return None, f"block {block_id}: {exc}"
    cache.put(generation, block_id, packed)
    return packed, "sidecar_read"


# --------------------------------------------------------------------------- #
# PART 6 - CONCATENATION IN THE EXACT _flush_gpu KEY ORDER.                      #
# --------------------------------------------------------------------------- #
def concat_normals_in_key_order(
        keys: Sequence[Tuple[int, int]],
        fetch: Callable[[Tuple[int, int]], Optional[np.ndarray]],
        expected_points: int) -> Tuple[Optional[np.ndarray], str]:
    """Concatenate packed normals in ``keys`` order - the order _flush_gpu used.

    ``fetch(key)`` returns that same tile's (N,2) int16 block. The total is
    checked against ``expected_points`` BEFORE anyone uploads it, because a
    silent length mismatch shifts every normal by an unknown offset and renders
    as subtly-wrong lighting rather than as a crash.
    """
    parts: List[np.ndarray] = []
    total = 0
    for key in keys:
        blk = fetch(key)
        if blk is None:
            return None, f"missing normal block for tile {key}"
        blk = np.asarray(blk, dtype=np.int16)
        if blk.ndim != 2 or blk.shape[1] != 2:
            return None, f"tile {key}: normal block is {blk.shape}, expected (N,2)"
        parts.append(blk)
        total += int(blk.shape[0])
    if total != int(expected_points):
        return None, (f"normal/point count mismatch: normals={total:,} "
                      f"points={int(expected_points):,}")
    if not parts:
        return np.empty((0, 2), dtype=np.int16), ""
    return np.ascontiguousarray(np.concatenate(parts, axis=0)), ""
# --------------------------------------------------------------------------- #
# PART 13 - NEVER-BLANK REFINEMENT.                                             #
# --------------------------------------------------------------------------- #
class ShadedActivationPolicy:
    """Chooses which blocks STREAMING shaded may draw.

    The rule is one-directional: a complete FINE block may replace a complete
    COARSE one; an INCOMPLETE fine block may not replace anything. The coarse
    block stays ACTIVE until the finer one is genuinely ready, so refinement is
    additive in coverage and never a black frame.
    """

    def __init__(self, coverage_to_release: float = COVERAGE_TO_RELEASE_OVERVIEW):
        self.coverage_to_release = float(coverage_to_release)
        self.active: "OrderedDict[Tuple[int, int], float]" = OrderedDict()
        self.blank_frames = 0
        self.coarse_held_frames = 0

    def reset(self) -> None:
        self.active.clear()

    def select(self, desired: Sequence[Tuple[Tuple[int, int], float, bool]]
               ) -> List[Tuple[int, int]]:
        """``desired`` = ``[(key, priority, complete), ...]``, best first.

        The floor is chosen by LOD, not by completeness: the highest-LOD block
        is the coarse overview that always covers the frame, and it stays drawn
        until the complete FINER blocks cover enough of the screen to replace it.
        Completeness decides only what is allowed to be drawn at all.
        """
        if not desired:
            self.blank_frames += 1
            return []
        max_lod = max(int(k[1]) for k, _p, _c in desired)
        fine_total = sum(1 for k, _p, _c in desired if int(k[1]) < max_lod)
        fine_complete = [(k, p) for k, p, c in desired
                         if c and int(k[1]) < max_lod]
        coarse_complete = [(k, p) for k, p, c in desired
                           if c and int(k[1]) == max_lod]
        fine_complete.sort(key=lambda kv: -kv[1])
        coarse_complete.sort(key=lambda kv: -kv[1])

        keys = [k for k, _ in fine_complete]
        coverage = (len(fine_complete) / float(fine_total)) if fine_total else 0.0
        if coverage < self.coverage_to_release and coarse_complete:
            keys = [coarse_complete[0][0]] + keys
            self.coarse_held_frames += 1
        if not keys:
            self.blank_frames += 1
        self.active.clear()
        for k in keys:
            self.active[k] = 0.0
        return keys

    def stats(self) -> dict:
        return {"blank_frames": self.blank_frames,
                "coarse_held_frames": self.coarse_held_frames,
                "active_tiles": len(self.active)}


# --------------------------------------------------------------------------- #
# PART 9 - LOW-PRIORITY NORMAL PREFETCH.                                        #
# --------------------------------------------------------------------------- #
class NormalPrefetcher:
    """Requests normals for already-resident blocks WITHOUT delaying a frame.

    The first Shaded click should usually find the coarse normal set already
    resident. ``request`` only appends to a bounded queue; draining happens on
    the stream manager's own worker thread, never on the UI thread.
    """

    def __init__(self, capacity: int = 512):
        self.capacity = int(capacity)
        self._q: List[Tuple[float, int, int]] = []   # (priority, gen, block_id)
        self._dropped = 0

    def request(self, block_ids: Iterable[int], generation: int,
                priority: float = 0.0) -> int:
        """Queue normal loads. Returns how many were accepted."""
        added = 0
        for bid in block_ids:
            if len(self._q) >= self.capacity:
                self._dropped += 1
                break
            self._q.append((float(priority), int(generation), int(bid)))
            added += 1
        self._q.sort(key=lambda t: -t[0])
        return added

    def take(self, limit: int = 64, generation: Optional[int] = None
             ) -> List[Tuple[int, int]]:
        """Pop up to ``limit`` ``(generation, block_id)``, dropping stale work."""
        out: List[Tuple[int, int]] = []
        keep: List[Tuple[float, int, int]] = []
        for prio, gen, bid in self._q:
            if generation is not None and gen != generation:
                self._dropped += 1              # Part 15: latest camera wins
                continue
            if len(out) < limit:
                out.append((gen, bid))
            else:
                keep.append((prio, gen, bid))
        self._q = keep
        return out

    @property
    def depth(self) -> int:
        return len(self._q)

    @property
    def dropped(self) -> int:
        return self._dropped
# --------------------------------------------------------------------------- #
# PART 15 - GENERATION GATING.                                                   #
# --------------------------------------------------------------------------- #
class GenerationGate:
    """``dataset_generation`` + ``camera_generation`` -> staleness decisions."""

    def __init__(self):
        self.dataset_generation = 0
        self.camera_generation = 0
        self.stale_requests_dropped = 0

    def new_dataset(self) -> int:
        self.dataset_generation += 1
        self.camera_generation = 0
        return self.dataset_generation

    def new_camera(self, generation: Optional[int] = None) -> int:
        self.camera_generation = (int(generation) if generation is not None
                                  else self.camera_generation + 1)
        return self.camera_generation

    def is_stale(self, camera_generation: int, dataset_generation: int) -> bool:
        stale = (int(camera_generation) != int(self.camera_generation)
                 or int(dataset_generation) != int(self.dataset_generation))
        if stale:
            self.stale_requests_dropped += 1
        return stale


# --------------------------------------------------------------------------- #
# PART 16 - SCREEN DENSITY TARGET.                                               #
# --------------------------------------------------------------------------- #
def target_point_budget(pixels: int, moving: bool) -> int:
    """Points to submit for a viewport, derived from measured points/pixel.

    Deliberately NOT "as many points as exist": the LOD4 overview already gives
    ~0.18 points/pixel at 1920x848, so blindly rendering maximum detail buys
    nothing on screen while costing frame time.

    This is the SINGLE budget authority. ScreenSpaceLOD keeps its own absolute
    point_budget_moving/idle (4M/8M); when that is left in force it is what
    produced ~5.1 points/pixel at 1920x848. ``ScreenDensityBudget.apply()``
    overwrites it with this value so there is exactly ONE number in play.
    """
    ppp = MOVING_TARGET_POINTS_PER_PIXEL if moving else IDLE_TARGET_POINTS_PER_PIXEL
    return int(max(0, int(pixels)) * float(ppp))


class ScreenDensityBudget:
    """Binds the viewport's screen density to the REAL LOD selection.

    Part 2: this must change WHICH BLOCKS BECOME ACTIVE, not merely print a
    number. The way to do that without inventing a second LOD hierarchy is to
    push the density budget into the existing ``ScreenSpaceLOD`` instance,
    whose second pass already downgrades nodes by importance until the budget
    is met (naksha_lod_gate.py lines 165-188). We supply the budget; the gate
    does the selection.
    """

    def __init__(self):
        self.width = 0
        self.height = 0
        self.moving = True
        self.last_target = 0
        self.last_candidate = 0
        self.last_selected = 0
        self.last_applied = 0
        self.candidate_points = 0

    @property
    def pixels(self) -> int:
        return int(self.width) * int(self.height)

    def apply(self, lod, width: int, height: int, moving: bool) -> int:
        """Push the density budget into ``lod`` and return the target count."""
        self.width, self.height = int(width), int(height)
        self.moving = bool(moving)
        px = self.pixels
        target = target_point_budget(px, self.moving) if px > 0 else 0
        if px > 0:
            # This is the line that actually changes LOD selection.
            lod.budget = int(target)
            lod.moving = bool(moving)
        self.last_target = int(target)
        return int(target)

    def note_candidates(self, selections) -> None:
        self.last_candidate = int(sum(int(o["points"])
                                      for o in (selections or [])))
        self.candidate_points = self.last_candidate

    def note_selected(self, points: int) -> None:
        self.last_selected = int(points)
        self.last_applied = int(points)


# --------------------------------------------------------------------------- #
# PART 5/6/14 - PARENT / CHILD OVERLAP AUDIT.                                   #
# --------------------------------------------------------------------------- #
def _node_bounds(idx, node_id: int):
    """(min_xy, max_xy) for a node, or None."""
    try:
        nodes = idx.nodes
        if nodes is None or nodes.size == 0:
            return None
        sel = np.flatnonzero(nodes["node_id"] == int(node_id))
        if sel.size == 0:
            return None
        r = int(sel[0])
        return (np.asarray(nodes["bounds_min"][r][:2], dtype=np.float64),
                np.asarray(nodes["bounds_max"][r][:2], dtype=np.float64))
    except Exception:
        return None


def _overlap_area(a_min, a_max, b_min, b_max) -> float:
    ox = min(a_max[0], b_max[0]) - max(a_min[0], b_min[0])
    oy = min(a_max[1], b_max[1]) - max(a_min[1], b_min[1])
    if ox <= 0.0 or oy <= 0.0:
        return 0.0
    return float(ox) * float(oy)


def audit_lod_overlap(idx, active_keys, complete_fn=None,
                      tol: float = 1e-6):
    """DEV diagnostic (Part 14): find parent/child pairs drawing the SAME area.

    Returns a dict with:
      active_blocks                     - every active (node_id, lod)
      hierarchical_overlap_pairs        - all overlapping ancestor pairs
      temporary_handoff_pairs           - overlapping, but the finer block is
                                          still INCOMPLETE (legal, never-blank)
      illegal_persistent_overlap_pairs  - overlapping AND both complete, i.e.
                                          the refined child is active while its
                                          coarse parent still covers it

    The last count MUST be 0 once the camera has stabilised. Part 6 is respected:
    LOD0 in one region and LOD2 in another is NOT overlap, because the audit
    only pairs blocks whose spatial bounds genuinely intersect.
    """
    pairs = []
    active = list(active_keys)
    for i in range(len(active)):
        for j in range(i + 1, len(active)):
            ka, kb = active[i], active[j]
            if int(ka[1]) == int(kb[1]):
                continue                      # same level: siblings, legal
            coarse, fine = (ka, kb) if int(ka[1]) > int(kb[1]) else (kb, ka)
            ba, bb = _node_bounds(idx, coarse[0]), _node_bounds(idx, fine[0])
            if ba is None or bb is None:
                continue
            area = _overlap_area(ba[0], ba[1], bb[0], bb[1])
            if area <= tol:
                continue
            # CONTAINMENT, not mere intersection. The node grid tiles the
            # dataset, so neighbouring tiles share edges; counting that as
            # overlap flagged every adjacent pair. Real parent/child overlap is
            # the FINE block's footprint sitting (mostly) inside the coarse one.
            coarse_area = max(1e-9, (float(ba[1][0] - ba[0][0])
                                     * float(ba[1][1] - ba[0][1])))
            fine_area = max(1e-9, (float(bb[1][0] - bb[0][0])
                                   * float(bb[1][1] - bb[0][1])))
            contained = (area / fine_area) >= 0.80
            if not contained:
                continue
            fine_ok = True
            if complete_fn is not None:
                try:
                    fine_ok = bool(complete_fn(fine))
                except Exception:
                    fine_ok = False
            pairs.append({"coarse": tuple(int(v) for v in coarse),
                          "fine": tuple(int(v) for v in fine),
                          "overlap_area_m2": round(area, 3),
                          "fine_complete": fine_ok,
                          "temporary": not fine_ok})
    illegal = [p for p in pairs if p["fine_complete"]]
    temporary = [p for p in pairs if not p["fine_complete"]]
    return {"active_blocks": [tuple(int(v) for v in k) for k in active],
            "hierarchical_overlap_pairs": len(pairs),
            "temporary_handoff_pairs": len(temporary),
            "illegal_persistent_overlap_pairs": len(illegal),
            "illegal_pairs": illegal,
            "temporary_pairs": temporary}


def _node_bounds_xy(idx, node_id: int):
    """Public alias of ``_node_bounds`` for the stream manager's retire pass."""
    return _node_bounds(idx, node_id)


def _overlap_frac(a, b) -> float:
    """Fraction of box ``a`` covered by box ``b`` (0.0..1.0)."""
    ax0, ay0 = float(a[0][0]), float(a[0][1])
    ax1, ay1 = float(a[1][0]), float(a[1][1])
    area = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    if area <= 0.0:
        return 0.0
    return _overlap_area(a[0], a[1], b[0], b[1]) / area


def _boxes_overlap(a, b) -> bool:
    """True when two boxes share ANY area (not a containment threshold)."""
    return _overlap_area(a[0], a[1], b[0], b[1]) > 0.0


def union_coverage_ratio(target, boxes, max_cells: int = 4096) -> float:
    """Fraction of ``target`` covered by the UNION of ``boxes`` (0.0..1.0).

    WHY THIS EXISTS. The retirement gate used to ask whether a SINGLE finer
    block covered >=80% of a coarse block. That is a containment test being
    used as a coverage test, and it is unanswerable for the overview: its
    footprint is the WHOLE 1000x1000 m dataset, while each finer tile covers a
    small part of it, so no single child ever reaches 0.80 and the coarse floor
    is re-protected forever (the live 44 blocks / LOD4=1 / 1,326,359 points).

    Coverage is a property of the UNION of the replacement set, so it must be
    measured that way. The grid is bounded by ``max_cells`` so this can never
    become an unbounded per-frame cost; it is only evaluated for tiles that are
    genuine retirement candidates.
    """
    tx0, ty0 = float(target[0][0]), float(target[0][1])
    tx1, ty1 = float(target[1][0]), float(target[1][1])
    tw = max(0.0, tx1 - tx0)
    th = max(0.0, ty1 - ty0)
    total_area = tw * th
    if total_area <= 0.0:
        return 0.0

    keep = []
    for b in boxes:
        if b is None:
            continue
        # Only boxes that actually intersect the target can contribute.
        if _overlap_area((tx0, ty0), (tx1, ty1), b[0], b[1]) <= 0.0:
            continue
        keep.append(b)
    if not keep:
        return 0.0

    nx = ny = 1
    step = max(tw, th)
    if step > 0.0:
        # Choose a grid with at most max_cells samples.
        n = int(np.ceil(np.sqrt(float(max_cells))))
        n = max(1, int(n))
        nx = ny = n
        step = max(tw, th) / float(n)
    if step <= 0.0:
        return 0.0

    bx = np.asarray([[float(b[0][0]), float(b[0][1]),
                      float(b[1][0]), float(b[1][1])] for b in keep],
                    dtype=np.float64)
    cx = tx0 + (np.arange(nx, dtype=np.float64) + 0.5) * step
    cy = ty0 + (np.arange(ny, dtype=np.float64) + 0.5) * step
    # The mask must clip against the TARGET's far edges (tx1 / ty1), not against
    # `t0 + n*step`. A square sample grid spans max(tw, th) on BOTH axes, so for a
    # non-square target the extra rows/columns fall outside the block - and
    # counting them made full coverage mathematically unreachable (400x225 with
    # two exact children measured 0.5625 instead of 1.0), so the coarse parent
    # could never be released regardless of how completely it was replaced.
    inside = ((cx[:, None] >= tx0) & (cx[:, None] <= tx1)
              & (cy[None, :] >= ty0) & (cy[None, :] <= ty1))
    covered = np.zeros((nx, ny), dtype=bool)
    for i in range(bx.shape[0]):
        x0, y0, x1, y1 = bx[i]
        covered |= ((cx[:, None] >= x0) & (cx[:, None] <= x1)
                    & (cy[None, :] >= y0) & (cy[None, :] <= y1))
    n_cells = int(inside.sum())
    if n_cells <= 0:
        return 0.0
    return float((covered & inside).sum()) / float(n_cells)


def points_per_pixel(submitted_points: int, width: int,
                     height: int) -> Optional[float]:
    px = int(width) * int(height)
    if px <= 0:
        return None
    return float(submitted_points) / float(px)


# --------------------------------------------------------------------------- #
# PART 18 - GPU BUDGET INCLUDING NORMALS.                                       #
# --------------------------------------------------------------------------- #
@dataclass
class GpuBudget:
    """XYZ + CLASS + INTENSITY + RGB + NORMAL bytes, with a normal share cap."""
    total_bytes: int = 1 << 30
    normal_share: float = NORMAL_GPU_BUDGET_FRACTION

    def __post_init__(self):
        self.normal_limit = int(self.total_bytes) * float(self.normal_share)

    @staticmethod
    def attribute_bytes(pts: int, *, xyz: bool = True, cls: bool = True,
                        rgb: bool = False, inten: bool = False,
                        normal: bool = False) -> dict:
        p = int(pts)
        return {"xyz": p * 24 if xyz else 0,
                "class": p * 1 if cls else 0,
                "rgb": p * 3 if rgb else 0,
                "intensity": p * 4 if inten else 0,
                "normal": p * BYTES_PER_NORMAL if normal else 0}

    @staticmethod
    def total(attrs: dict) -> int:
        return int(sum(int(v) for v in attrs.values()))

    def normal_fits(self, points: int) -> bool:
        return int(points) * BYTES_PER_NORMAL <= self.normal_limit
# --------------------------------------------------------------------------- #
# PART 20/21 - TELEMETRY PAYLOADS.                                              #
# --------------------------------------------------------------------------- #
def draw_telemetry(*, normal_source: str, source_points: Optional[int],
                   visible_tiles: int, lod_distribution: dict,
                   resident_xyz_points: int, resident_class_points: int,
                   resident_normal_points: int, submitted_points: int,
                   width: int, height: int, splat_px: Optional[float],
                   normal_ram_bytes: int, normal_gpu_bytes: int,
                   new_normal_blocks: int, reused_normal_blocks: int,
                   stale_requests_dropped: int, xyz_gpu_bytes: int = 0,
                   class_gpu_bytes: int = 0, other_attr_bytes: int = 0) -> dict:
    """The Part 20 [INSTANT SHADED DRAW] record. Read-only; decides nothing."""
    ppp = points_per_pixel(submitted_points, width, height)
    return {
        "normal_source": str(normal_source),
        "source_points": source_points,
        "visible_tiles": int(visible_tiles),
        "LOD_distribution": {int(k): int(v)
                             for k, v in dict(lod_distribution).items()},
        "resident_xyz_points": int(resident_xyz_points),
        "resident_class_points": int(resident_class_points),
        "resident_normal_points": int(resident_normal_points),
        "submitted_points": int(submitted_points),
        "points_per_pixel": (round(ppp, 3) if ppp is not None else None),
        "splat_px": (float(splat_px) if splat_px else None),
        "normal_RAM_bytes": int(normal_ram_bytes),
        "normal_GPU_bytes": int(normal_gpu_bytes),
        "new_normal_blocks": int(new_normal_blocks),
        "reused_normal_blocks": int(reused_normal_blocks),
        "stale_requests_dropped": int(stale_requests_dropped),
        "xyz_gpu_bytes": int(xyz_gpu_bytes),
        "class_gpu_bytes": int(class_gpu_bytes),
        "other_attr_gpu_bytes": int(other_attr_bytes),
        "total_point_gpu_bytes": (int(xyz_gpu_bytes) + int(class_gpu_bytes)
                                  + int(other_attr_bytes)
                                  + int(normal_gpu_bytes)),
    }


def switch_telemetry(*, normal_source: str, xyz_upload_due_to_switch: int,
                     normal_upload_due_to_switch: int, delaunay_calls: int = 0,
                     surface_rebuilds: int = 0, full_recolors: int = 0,
                     cpu_ms: Optional[float] = None,
                     ptc_label: str = "") -> dict:
    """Part 21 [INSTANT SHADED SWITCH] - mode-switch cost ONLY.

    Deliberately separate from streaming cost: mixing them makes a cheap style
    switch look like it paid for tile streaming.
    """
    return {"normal_source": str(normal_source),
            "XYZ_upload_due_to_switch": int(xyz_upload_due_to_switch),
            "normal_upload_due_to_switch": int(normal_upload_due_to_switch),
            "Delaunay": int(delaunay_calls),
            "surface_rebuild": int(surface_rebuilds),
            "full_recolor": int(full_recolors),
            "switch_cpu_ms": (round(float(cpu_ms), 3) if cpu_ms is not None
            else None),
            "ptc_label": str(ptc_label or "")}


def tile_load_telemetry(*, reason: str, block_id: int, lod: int,
                        xyz_bytes: int, class_bytes: int, normal_bytes: int,
                        reused_normal: bool = False) -> dict:
    """Part 21 [STREAM TILE LOAD] - streaming cost ONLY."""
    return {"reason": str(reason), "block_id": int(block_id), "LOD": int(lod),
            "XYZ_bytes": int(xyz_bytes), "CLASS_bytes": int(class_bytes),
            "NORMAL_bytes": int(normal_bytes),
            "normal_reused": bool(reused_normal)}


def format_block(lines: Iterable[str], title: str) -> str:
    return "\n".join(list(lines) + [f"[/{title}]"])
