"""NakshaStreamManager: real-time tile streaming for a committed NAKSHA cache.
Stage 3C streaming core. Implements:
  Cache-First Open / Streaming Metadata / Stream Manager
  Latest-Camera-Wins / Persistent GPU Residency / No Monolithic Upload
  RAM/GPU Budget / Never-Blank Fallback / Position-Reupload Hard Gate
Import-safe. Rendering is abstracted behind StreamingRendererAdapter so the
DECISION pipeline is validated headlessly (st3c_headless_smoke.py) without GPU.
"""
from __future__ import annotations
import os, sys, time, queue, threading
import concurrent.futures
from collections import OrderedDict
from dataclasses import dataclass
import numpy as np
from .dataset_mode import DatasetMode, is_streaming_dataset
from .lod_kernel import FastLodSelector, LodReuseWindow, kernel_mode
from .capabilities import DatasetCapabilities, DEFAULT_DISPLAY_MODE
from .display_modes import MODES, canonical_mode, build_luts, mode_requires
from .gpu_memory import (
    RENDER_FULL_RESIDENT, RENDER_STREAMING_LOD, estimate_from_capabilities,
    force_render_mode, probe_safe_gpu_budget,
)
from .gpu_arena import GpuArena
from .format import ATTR_XYZ, ATTR_CLASSIFICATION, ATTR_INTENSITY, ATTR_RGB
from .normal_streaming import (
    ATTR_NORMAL, BlockReadiness, DEFAULT_NORMAL_RAM_BUDGET,
    GenerationGate, IDLE_REFINE_MS, IDLE_TARGET_POINTS_PER_PIXEL,
    MOVING_TARGET_POINTS_PER_PIXEL,
    NormalBlockCache, NormalPrefetcher, ShadedActivationPolicy,
    BYTES_PER_NORMAL, ScreenDensityBudget, audit_lod_overlap,
    concat_normals_in_key_order, draw_telemetry, load_normal_block,
    nkpc_attrs, points_per_pixel, requires_normal, required_attributes,
    switch_telemetry, target_point_budget, tile_load_telemetry,
)
from .normal_streaming import open_normal_cache  # PHASE 6D handoff
from .normal_streaming import _node_bounds_xy, _overlap_frac, _boxes_overlap
from .normal_streaming import union_coverage_ratio, COVERAGE_TO_RELEASE_BLOCK
# CAMERA / LOD / VISIBLE-POINT PARITY GATE (phases 0-19). Import-safe and
# GPU-free, so the parity proof cannot itself break a frame.
from .visible_parity import (
    CELL_SIZES_M, HARD_CELL_M, DIAG_CELL_M, INFO_CELL_M,
    CameraSample, DiagnosticGuard, ParityReport, SourceOccupancy,
    PACKED_CLS_KEYS, PACKED_COUNT_KEYS, PACKED_INTEN_KEYS, PACKED_NORMAL_KEYS,
    PACKED_RGB_KEYS, PACKED_READINESS_KEYS, PACKED_XYZ_KEYS,
    activation_report, async_parity_report, camera_lod_parity, cell_span,
    cells_for_boxes, cells_in_bounds, classify_root_cause,
    clip_bounds, connected_regions, largest_gap_m, lod_distribution,
    read_camera_sample, region_bounds, render_camera_lod_parity,
    three_way_parity, tile_camera_intersection, tile_complete as tile_complete_fn,
    tile_count, tile_field, tile_is_mapping, tile_normal_ready, tile_xyz,
)
# Fail loudly at import time rather than as a NameError inside the live frame
# tick. The live GUI calls is_idle() every tick, so an unresolved constant here
# throws continuously and silently kills moving->idle refinement: the stream
# looks alive but never promotes to IDLE, and the symptom appears far away from
# the cause. Asserting the canonical value keeps normal_streaming.py the single
# source of truth while making a missing import impossible to miss.
assert isinstance(IDLE_REFINE_MS, (int, float)) and IDLE_REFINE_MS > 0, \
    "IDLE_REFINE_MS must come from normal_streaming (one source of truth)"
DEFAULT_RAM_BUDGET_BYTES = 8 * 1024 ** 3
DEFAULT_GPU_BUDGET_BYTES = 1.5 * 1024 ** 3
# PART 7 - camera telemetry is a STATE CHANNEL, not a per-event stream. One
# write per CAMERA_TELEMETRY_MS keeps the diagnostics without putting a file
# write on the mouse-event hot path.
CAMERA_TELEMETRY_MS = 250.0
# PHASE 4: longest a clean-residency camera frame may skip the full
# `_flush_gpu` reconcile (safety net behind the resident-generation check).
FAST_FLUSH_REFRESH_S = 0.5
# PHASE 4B: optional latency-probe reporting (gui/phase4b_latency_probe.py).
# `_pm()` is one list read + one attribute test when the probe is off or not
# capturing - no allocation, no clock read - so the hooks below are safe on the
# per-frame path. The probe module is imported lazily, once.
_PM = [None, False]

def _pm():
    if not _PM[1]:
        _PM[1] = True
        try:
            from gui import phase4b_latency_probe as _m
            _PM[0] = _m
        except Exception:
            _PM[0] = None
    m = _PM[0]
    if m is None:
        return None
    p = m._PROBE
    return m if (p is not None and p.capturing) else None

def auto_budgets(adapter=None) -> dict:
    """PART 11/12/13 - budgets from the MEASURED hardware profile.
    These are the defaults; a caller may still pass explicit values, which is
    what the DEV memory-pressure test (PART 50) uses to force eviction.
    """
    try:
        from .hardware import get_profile
        p = get_profile(adapter)
        return {"ram_budget": int(p.ram_soft_limit_bytes),
                "gpu_budget": int(p.gpu_usable_bytes),
                "workers": dict(p.workers), "profile": p}
    except Exception:
        return {"ram_budget": DEFAULT_RAM_BUDGET_BYTES,
                "gpu_budget": DEFAULT_GPU_BUDGET_BYTES,
                "workers": {}, "profile": None}
# Part 17: splat COVERAGE. Kept independent of LOD selection on purpose - a
# huge splat hides missing detail instead of fixing it, so the LOD ladder
# would look fine while rendering 299,676 points.
SPLAT_PX_MIN, SPLAT_PX_MAX = 1.0, 3.0
# PART 13 - console flood budget. The pending-replacement diagnostic used to
# print every replacement key and every readiness field on every reconcile pass,
# i.e. hundreds of lines per second on the UI thread. That printing is itself a
# performance defect: it starves the GUI and makes the renderer look slow. State
# CHANGES are always printed; a persisting condition only heartbeats at this
# interval.
PENDING_DIAG_HEARTBEAT_S = 5.0
# Attribute requirements come from ONE central function (Part 2) so a mode can
# never silently disagree with what the stream manager actually loads.
# PART 1/2: `neutral` is the default on open and requires XYZ ONLY, so any
# LAS/LAZ - including an XYZ-only file with no RGB - opens as a gray cloud.
DISPLAY_ATTRS = {
    "neutral":        required_attributes("neutral"),
    "depth":          required_attributes("depth"),
    "elevation":      required_attributes("elevation"),
    "classification": required_attributes("classification"),
    "class":          required_attributes("classification"),
    "intensity":      required_attributes("intensity"),
    "rgb":            required_attributes("rgb"),
    "shaded":         required_attributes("shaded"),
    "shaded_class":   required_attributes("shaded"),
    "surface":        required_attributes("surface"),
}
REQUESTED, RAM_READY, GPU_PENDING = "REQUESTED", "RAM_READY", "GPU_PENDING"
GPU_RESIDENT, EVICTABLE = "GPU_RESIDENT", "EVICTABLE"
# ---- STABLE GPU SLOTS (Parts 11/12/14) ------------------------------------ #
# `ResidentTile.first` is now an ARENA SLOT, not a position in a repacked
# concatenation. -1 means "no slot": the tile has no GPU geometry at all.
ARENA_NONE = -1
# Worst-case bytes for one point's CANONICAL position allocation (float64 world
# XYZ, which is what the native `nkv_upload_point_tile` takes). Attributes live
# in their own streams and are NOT charged here. This is used only to SIZE the
# native pool once; admission is still governed by `gpu_bytes`/`gpu_budget`.
ARENA_POSITION_BYTES_PER_POINT = 24.0
# Fraction of the GPU budget the POSITION arena may reserve. Attributes,
# normals, the surface, staging and the swapchain need the rest (Part 20/21).
ARENA_RESERVE_FRACTION = 0.55
# Bumped whenever the pan/zoom frontier-retirement behaviour changes, and
# printed at import time by app_streaming.py. Without it, "I edited
# stream_manager.py" and "main.py imported stream_manager.py" are
# indistinguishable from the console output alone.
PAN_ZOOM_FIX_VERSION = "PAN_ZOOM_FIX_V4_OVERVIEW_RETIRE_RGB_COMPLETE"

class _ResidentIndexView:
    """The only attribute `ScreenSpaceLOD.select()` reads off its `index`.
    Passing this instead of the real index is how the existing gate is reused
    for ACTIVE_DRAWN selection without copying or restructuring it: the caller
    supplies an already-masked node table (see `_resident_lod_nodes`).
    """
    __slots__ = ("nodes",)
    def __init__(self, nodes):
        self.nodes = nodes

@dataclass
class TileSpec:
    node_id: int
    lod: int
    priority: float
    points: int
    px_err: float
    row: int

# PHASE 4 / PART 39 - RESIDENT GENERATION. One monotonically increasing counter
# that moves whenever ANY tile's residency-relevant field changes or a tile is
# added/removed from a `_GenDict`. The camera path compares one integer instead
# of rebuilding and hashing the whole resident key set every frame. It is
# deliberately process-global: a spurious bump costs one recompute, a missed
# bump would be a stale selection, so over-notification is the safe direction.
_RES_GEN = [0]
# PHASE 4B: geometry AND attribute residency. Any tile field that changes what
# the GPU holds or must hold for the current mode bumps the generation - the
# geometry fields (state/count/first/lod/slot) and the attribute payloads
# (cls/inten/rgb/xyz/normal/attrs). Over-notification costs one full flush;
# a missed notification would be a stale frame, so this errs toward bumping.
# PHASE 6C: streams that are DERIVED (built into a sidecar) rather than
# stored as NKPC attribute bits. They are reported through derived_pending,
# never mixed into the int-bit resident_attrs set.
DERIVED_ATTR_NAMES = frozenset(("normal",))
_GEN_FIELDS = frozenset(("state", "count", "first", "lod", "node_id",
                         "arena_slot_valid", "cls", "inten", "rgb", "xyz",
                         "normal", "attrs"))
# PHASE 4B: display-mode / LUT (PTC) / palette / class-visibility / intensity
# range state. These reach the renderer through `push_display_state` (uniform
# pushes), never through the flush, but the camera-only reuse state must still
# be dropped synchronously when any of them moves.
_MODE_GEN = [0]

def resident_generation() -> int:
    return _RES_GEN[0]

def mode_generation() -> int:
    return _MODE_GEN[0]

def render_state_generation() -> tuple:
    """(residency+attribute generation, mode/LUT generation)."""
    return (_RES_GEN[0], _MODE_GEN[0])

class _GenDict(dict):
    """`dict` that bumps the resident generation on every structural change."""
    __slots__ = ()
    def __setitem__(self, k, v):
        _RES_GEN[0] += 1
        dict.__setitem__(self, k, v)
    def __delitem__(self, k):
        _RES_GEN[0] += 1
        dict.__delitem__(self, k)
    def pop(self, *a):
        _RES_GEN[0] += 1
        return dict.pop(self, *a)
    def popitem(self):
        _RES_GEN[0] += 1
        return dict.popitem(self)
    def clear(self):
        _RES_GEN[0] += 1
        dict.clear(self)
    def update(self, *a, **k):
        _RES_GEN[0] += 1
        dict.update(self, *a, **k)
    def setdefault(self, k, d=None):
        _RES_GEN[0] += 1
        return dict.setdefault(self, k, d)

@dataclass
class ResidentTile:
    node_id: int
    lod: int
    # ARENA SLOT (Parts 11/12/14). Stable for as long as this tile holds GPU
    # geometry; a camera move, a LOD change or the arrival of ANOTHER tile must
    # never alter it. -1 = no slot, i.e. nothing of this tile is on the GPU.
    first: int = ARENA_NONE
    count: int = 0
    bytes: int = 0
    priority: float = 0.0
    last_used: float = 0.0
    state: str = GPU_RESIDENT
    # The slot the GPU ACTUALLY holds for this tile, and which attribute
    # streams were written alongside it. `gpu_first != first` therefore means
    # "this tile needs a position upload"; a matching slot with a missing
    # attribute means "attributes only" - the position buffer is not touched.
    # This is what makes a pure LOD change a draw-range edit (Part 10) and keeps
    # an ordinary append at `existing_points_reuploaded == 0` (Part 14).
    gpu_first: int = ARENA_NONE
    gpu_attrs: object = None         # frozenset[str] of streams on the GPU
    # True only when `first` was handed out BY THE ARENA. The legacy
    # whole-buffer path packs offsets into the same field, so without this flag
    # a packed offset could be mistaken for a pool allocation and handed back to
    # the allocator - corrupting its free list. Ownership is explicit, never
    # inferred from the value.
    arena_slot_valid: bool = False
    xyz: object = None
    rgb: object = None
    cls: object = None
    inten: object = None
    # ---- stored-normal streaming (Parts 3/5/7) -----------------------------
    block_id: int = -1
    normal: object = None            # packed (N,2) int16, NOT float32x3
    normal_from_cache: bool = False  # True when the normal was RAM-cache reused
    readiness: object = None         # BlockReadiness
    normal_error: str = ""
    # PHASE 1: which NKPC attribute streams this tile actually HOLDS, as a
    # frozenset of ATTR_* bits. Distinct from `requires_normal`, which says
    # what the CURRENT mode wants. Without this the streamer cannot tell
    # "this block was read in Neutral and carries no class data" from "this
    # block carries class data that simply has not been uploaded yet", which is
    # exactly the ambiguity that left Class rendering one flat colour: a
    # Neutral -> Class switch found no missing attribute to load and therefore
    # uploaded nothing.
    attrs: object = None             # frozenset[int], None => unknown/legacy
    # Whether THIS tile's mode needs the stored-normal stream. Set at
    # construction so `complete` can distinguish "no normals attached because
    # this mode never asked" from "normals requested but not ready yet".
    requires_normal: bool = False
    # PART 47 - PINNED COARSE COVERAGE. A coarse representation is the cheap,
    # always-complete fallback that makes zoom-out, Fit and a large camera jump
    # instant instead of a re-read. Pinned tiles are never retired and never
    # evicted to make room, because trading them away buys a little memory and
    # costs the one guarantee that keeps the viewport alive.
    pinned: bool = False
    def __setattr__(self, name, value):
        if name in _GEN_FIELDS:
            _RES_GEN[0] += 1
        object.__setattr__(self, name, value)
    @property
    def normal_bytes(self) -> int:
        return 0 if self.normal is None else int(self.normal.nbytes)
    @property
    def complete(self) -> bool:
        """Is this block a legal draw-set member in the CURRENT mode?
        ROOT CAUSE of "overview never retires in the real RGB GUI".
        This used to be `readiness.complete`, i.e. XYZ + CLASS + NORMAL.
        But `_read_tile` only attaches a BlockReadiness when the mode requires
        NORMAL, so in rgb / classification / intensity / elevation every block
        had `readiness is None` and therefore `complete is False` FOREVER.
        `_retire_superseded` skips any tile that is not complete, so nothing
        could ever be retired in those four modes: the overview stayed resident
        and blocks only accumulated (44 blocks / LOD4=1 / 1,326,359 submitted /
        1.296 ppp) while the Shaded harness passed because it did attach
        normals.
        The fix separates the two questions that were conflated here:
          * "are the geometry attributes this mode needs present?" (complete)
          * "is the stored-normal stream present?"            (normal_ready)
        Completeness must depend on the MODE. In a non-normal mode a block is
        complete when it has its points and the class attribute the mode
        requires; normals are only required by Shaded, and their absence is
        reported separately by `normal_ready` so Shaded can still refuse to
        draw a half-shaded block.
        """
        if self.readiness is not None:
            # A readiness object only ever exists because a NORMAL pass ran:
            # `_read_tile` calls `_attach_normal` exactly when the mode
            # requires normals, and `_attach_normal` always sets one. Its
            # presence therefore means normals were REQUESTED for this block,
            # so XYZ+CLASS+NORMAL remains the bar. This preserves Shaded's
            # never-draw-a-partially-shaded-block guarantee exactly.
            return bool(self.readiness.complete)
        # No readiness object at all => no normal pass ran => a non-Shaded mode.
        # Geometry completeness is then the only legal bar, and normals are
        # irrelevant to what this mode draws.
        return bool(self.count > 0 and self.xyz is not None)
    @property
    def normal_ready(self) -> bool:
        """Stored-normal availability, independent of geometry completeness."""
        if self.readiness is not None:
            return bool(self.readiness.normal_ready)
        return self.normal is not None

class FrameBudgetController:
    """Adaptive interaction draw budget (Phases 3, 26, 28).
    The contract is NOT "the frame rate never drops" - no controller can promise
    that. The contract is GRACEFUL QUALITY REDUCTION: when frames run long, cut
    the interaction draw density BEFORE interaction becomes unresponsive, and
    restore it gradually once there is headroom again.
    Two design rules that matter more than the numbers:
    * ASYMMETRIC RESPONSE. Density drops fast (a gesture must not stutter) and
      recovers slowly (otherwise every pan flicks between densities and the
      result looks worse than either level).
    * HYSTERESIS + A DEAD BAND. Without both, a controller sitting exactly on
      target oscillates forever, which reads as flicker.
    Cost is tracked PER DISPLAY MODE (Phase 8): Shading is far more expensive
    per point than Neutral, so one shared point budget is wrong for both.
    """
    # Density as a fraction of the full resident set. `moving` is what an
    # interaction frame may draw; an idle frame always draws everything.
    QUALITY_STEPS = (1.0, 0.5, 0.25, 0.12, 0.06, 0.03)
    def __init__(self, target_frame_ms: float = 16.67):
        self.target_frame_ms = float(target_frame_ms)
        self._level = 1
        self.recent_frame_ms = 0.0
        self.samples = []
        self.last_adjustment = "init"
        self.quality_level = self.QUALITY_STEPS[self._level]
        self.mode_cost = {}
        self._good_frames = 0
        self._bad_frames = 0
        self.updates = 0
        # COOLDOWN. Without it a single burst of slow frames CASCADES: the drop
        # clears the window, the next slow frames refill it, and each refill
        # drives ANOTHER drop before the new density has had any chance to
        # help. Measured in a unit test: 20 slow frames produced a second drop
        # during the recovery that followed, so quality fell twice for one
        # event. One adjustment per cooldown is enough.
        self._cooldown = 0
        self.COOLDOWN_FRAMES = 16
    def observe(self, frame_ms: float, mode: str = "neutral") -> None:
        f = float(frame_ms)
        if not (f == f) or f <= 0.0:       # NaN / non-positive
            return
        self.recent_frame_ms = f
        self.samples.append(f)
        if len(self.samples) > 240:
            del self.samples[:120]
        # Learn per-mode cost from frames actually seen (Phase 8).
        cost = self.mode_cost.setdefault(str(mode), {"n": 0, "ms": 0.0})
        cost["n"] += 1
        cost["ms"] = f
    def budget_points(self, resident_points: int) -> int:
        """How many points an INTERACTION frame may submit right now."""
        if resident_points <= 0:
            return 0
        return max(1, int(resident_points * self.QUALITY_STEPS[self._level]))
    def update(self) -> bool:
        """Feed one observed frame time in. True when quality moved."""
        f = self.recent_frame_ms
        if f <= 0.0 or len(self.samples) < 8:
            return False
        # Trailing percentile, not the last sample: one slow frame (a mode
        # switch, a GC pause) must not collapse the quality level.
        if self._cooldown > 0:
            # A recent adjustment is still settling; acting again now would
            # cascade (see the note on _cooldown).
            self._cooldown -= 1
            return False
        ordered = sorted(self.samples)
        p90 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.90))]
        if p90 > self.target_frame_ms * 1.25:
            self._bad_frames += 1
            self._good_frames = 0
            if self._bad_frames >= 4 and self._level < len(self.QUALITY_STEPS) - 1:
                self._level += 1
                self._bad_frames = 0
                self.last_adjustment = f"down->{self._level}"
                self.quality_level = self.QUALITY_STEPS[self._level]
                self.samples.clear()
                self._cooldown = self.COOLDOWN_FRAMES
                return True
        elif p90 < self.target_frame_ms * 0.75:
            self._good_frames += 1
            self._bad_frames = 0
            # ASYMMETRIC, and NOT on the first fast frames after a drop.
            # `samples.clear()` on a drop leaves a SHORT window, so a p90 from a
            # handful of fast frames looks "comfortably below target" and undoes
            # the drop on the very next tick - exactly the oscillation this
            # controller exists to prevent. Require a full window of evidence.
            if (self._good_frames >= 32
                    and len(self.samples) >= 32
                    and self._level > 0):
                self._level -= 1
                self._good_frames = 0
                self.last_adjustment = f"up->{self._level}"
                self.updates += 1
                self.quality_level = self.QUALITY_STEPS[self._level]
                self.samples.clear()
                self._cooldown = self.COOLDOWN_FRAMES
                return True
        else:
            # Inside the band: decay both counters so the controller settles
            # instead of accumulating credit indefinitely.
            self._good_frames = max(0, self._good_frames - 1)
            self._bad_frames = max(0, self._bad_frames - 1)
        return False
    def describe(self) -> str:
        return (f"level={self._level} "
                f"density={self.QUALITY_STEPS[self._level]:.3f} "
                f"recent_ms={self.recent_frame_ms:.2f} "
                f"target_ms={self.target_frame_ms:.2f} "
                f"adjustments={self.updates}")

# =========================================================================== #
# PHASE 24/25/26 - ATOMIC RENDER STATE.                                      #
# =========================================================================== #
# A mode switch must NEVER blank the viewport (Phase 26). Two states: the ACTIVE
# one keeps drawing, a PENDING one is prepared in the background. Only when the
# pending state satisfies EVERY readiness predicate does it commit, and it
# commits at a FRAME BOUNDARY - so the swap is atomic from the viewer's point
# of view: one frame Neutral, next frame Shading, never a frame with a buffer
# unbound.
# Phase 25 request states.
REQ_REQUESTED = "REQUESTED"
REQ_PREPARING = "PREPARING"
REQ_READY_TO_COMMIT = "READY_TO_COMMIT"
REQ_ACTIVE = "ACTIVE"
REQ_FAILED = "FAILED"

@dataclass
class RenderState:
    """One display mode plus everything required to draw it (Phase 24)."""
    mode: str = "neutral"
    generation: int = 0
    # A mode is committable only when every resource it REQUIRES is True; one it
    # does not use is ignored. That is why Elevation and Depth never wait for
    # Class or Intensity - a real defect was Elevation reporting
    # required_attrs=(1,2,4) and blocking on Class.
    position_ready: bool = True
    class_ready: bool = False
    rgb_ready: bool = False
    intensity_ready: bool = False
    normal_ready: bool = False
    surface_ready: bool = False
    pipeline_ready: bool = False
    coverage_ready: bool = False
    request_state: str = REQ_ACTIVE
    reason: str = ""
    # Phases 7/11/15: what this mode actually consumes.
    REQUIRES = {
        "neutral": (),
        "class": ("class",),
        "classification": ("class",),
        "intensity": ("intensity",),
        "elevation": (),          # shader-derived from PositionBuffer.z
        "depth": (),              # shader-derived from the camera
        "rgb": ("rgb",),
        "shaded": ("class", "normal"),
        "surface": ("surface",),
    }
    def required_resources(self) -> tuple:
        return self.REQUIRES.get(str(self.mode), ())
    def missing_resources(self) -> list:
        return [n for n in self.required_resources()
                if not getattr(self, f"{n}_ready", False)]
    def committable(self) -> bool:
        """True only when this state may become the visible mode (Phase 24)."""
        return (self.position_ready and not self.missing_resources()
                and self.pipeline_ready)
    def describe(self) -> str:
        miss = self.missing_resources()
        return (f"{self.mode}[{self.request_state}] gen={self.generation} "
                f"missing={miss or '-'} pipeline={self.pipeline_ready} "
                f"coverage={self.coverage_ready}")

class AtomicModeSwap:
    """Owns `active_render_state` and `pending_render_state` (Phases 24, 26).
    THE NEVER-BLACK CONTRACT: a request NEVER replaces what is being drawn. It
    becomes a PENDING state, the previous mode keeps rendering every frame until
    the pending state is fully committable, and only then does one commit swap
    the visible mode. No code path here can leave the viewport with nothing to
    draw.
    """
    def __init__(self, initial_mode: str = "neutral"):
        self.active = RenderState(mode=initial_mode, request_state=REQ_ACTIVE,
                                  pipeline_ready=True)
        self.pending = None
        # Phase 35 telemetry. black_frames / partial_frames MUST stay 0; they
        # are counted here precisely so a regression becomes visible.
        self.commits = 0
        self.rejected_commits = 0
        self.black_frames = 0
        self.partial_frames = 0
        self.history = []
    def request(self, mode: str, generation: int = 0) -> "RenderState":
        """Ask for `mode`. The ACTIVE state is deliberately left untouched."""
        mode = str(mode)
        if mode == self.active.mode and self.pending is None:
            return self.active
        if self.pending is not None and self.pending.mode == mode:
            return self.pending
        st = RenderState(mode=mode, generation=generation,
                         request_state=REQ_PREPARING)
        self.pending = st
        # Phase 35 preparation telemetry, completed by `mark_ready`.
        self.pending_preparation_ms = 0.0
        self.pending_requested_at = time.perf_counter()
        self.pending_cache_hit = None
        return st
    def mark_ready(self, state: "RenderState", **flags) -> None:
        """Record resource readiness for a pending state."""
        if state is not self.pending:
            return
        for k, v in flags.items():
            if hasattr(state, k):
                setattr(state, k, bool(v))
        state.request_state = (REQ_READY_TO_COMMIT
                               if state.committable() else REQ_PREPARING)
        if state.committable() and not self.pending_preparation_ms:
            # Phase 35: preparation time is measured from the REQUEST, not from
            # the last mark_ready call.
            self.pending_preparation_ms = (
                time.perf_counter() - self.pending_requested_at) * 1000.0
    def cancel(self, reason: str = "superseded") -> None:
        """Drop a pending request. The active mode keeps drawing throughout."""
        if self.pending is not None:
            self.pending.request_state = REQ_FAILED
            self.pending.reason = reason
            self.history.append({"dropped": self.pending.mode, "reason": reason})
            self.pending = None
    def commit_if_ready(self) -> bool:
        """Frame-boundary commit (Phase 12). True if the visible mode changed."""
        p = self.pending
        if p is None:
            return False
        if not p.committable():
            # NOT an error and NOT a black frame: the old mode is still up.
            self.rejected_commits += 1
            return False
        p.request_state = REQ_ACTIVE
        self.active = p
        self.pending = None
        self.commits += 1
        self.history.append({"committed": p.mode, "generation": p.generation})
        return True
    def status_line(self) -> str:
        """User-facing status; rendering continues regardless (Phase 26)."""
        if self.pending is None:
            return f"{self.active.mode} (ready)"
        miss = self.pending.missing_resources()
        what = ", ".join(miss) if miss else "finishing"
        return (f"{self.active.mode} active - Preparing "
                f"{self.pending.mode}... ({what})")
    def describe(self) -> str:
        return (f"active={self.active.describe()} "
                f"pending={self.pending.describe() if self.pending else 'none'} "
                f"commits={self.commits} rejected={self.rejected_commits} "
                f"black_frames={self.black_frames} "
                f"partial_frames={self.partial_frames}")

# --------------------------------------------------------------------------- #
# PART E - UNIFIED ATOMIC MODE CONTROLLER.                                      #
# --------------------------------------------------------------------------- #
# Phase 8E: 5-generation tracking + PENDING/ACTIVE shading state machine.
ACTIVE_SHADING_STATE = "ACTIVE_SHADING_STATE"
PENDING_SHADING_STATE = "PENDING_SHADING_STATE"

@dataclass
class ShadingStateSnapshot:
    """Immutable capture of the 5 generation clocks at a commit decision."""
    dataset_generation: int = 0
    camera_generation: int = 0
    mode_generation: int = 0
    resource_generation: int = 0
    layout_fingerprint: int = 0
    def matches(self, other: "ShadingStateSnapshot") -> bool:
        return (
            int(self.dataset_generation) == int(other.dataset_generation)
            and int(self.camera_generation) == int(other.camera_generation)
            and int(self.mode_generation) == int(other.mode_generation)
            and int(self.resource_generation) == int(other.resource_generation)
            and int(self.layout_fingerprint) == int(other.layout_fingerprint))
    def as_dict(self) -> dict:
        return {
            "dataset_generation": int(self.dataset_generation),
            "camera_generation": int(self.camera_generation),
            "mode_generation": int(self.mode_generation),
            "resource_generation": int(self.resource_generation),
            "layout_fingerprint": int(self.layout_fingerprint),
        }

class RenderStateValidator:
    """5-generation tracking + PENDING/ACTIVE shading state machine (Phase 8E).
    Tracks the five clocks that can invalidate a pending mode switch:
    1. ``dataset_generation``  - bumps on dataset open / reopen.
    2. ``camera_generation``   - bumps on camera motion.
    3. ``mode_generation``     - bumps when display mode changes.
    4. ``resource_generation`` - bumps when GPU resources change.
    5. ``layout_fingerprint``   - hash of the point-cache layout.
    A PENDING state captures a snapshot of all five clocks at request time.
    At the frame boundary ``validate_commit`` re-reads the current clocks and
    rejects the swap if dataset_generation or layout_fingerprint drifted,
    so a stale mesh built for an old layout can never draw.
    """
    def __init__(self):
        self.dataset_generation = 0
        self.camera_generation = 0
        self.mode_generation = 0
        self.resource_generation = 0
        self.layout_fingerprint = 0
        self._pending_snapshot = None
        self._active_snapshot = None
        self.pending_state = ACTIVE_SHADING_STATE
        self.commits = 0
        self.rejected_commits = 0
        self.rejected_reasons = []
    # -- clock mutators ------------------------------------------------------- #
    def bump_dataset(self) -> int:
        self.dataset_generation += 1
        self._pending_snapshot = None
        self.pending_state = ACTIVE_SHADING_STATE
        return self.dataset_generation
    def bump_camera(self) -> int:
        self.camera_generation += 1
        return self.camera_generation
    def bump_mode(self) -> int:
        self.mode_generation += 1
        return self.mode_generation
    def bump_resource(self) -> int:
        self.resource_generation += 1
        return self.resource_generation
    def set_layout_fingerprint(self, fp: int) -> None:
        self.layout_fingerprint = int(fp)
    def snapshot(self) -> "ShadingStateSnapshot":
        return ShadingStateSnapshot(
            dataset_generation=self.dataset_generation,
            camera_generation=self.camera_generation,
            mode_generation=self.mode_generation,
            resource_generation=self.resource_generation,
            layout_fingerprint=self.layout_fingerprint,
        )
    # -- PENDING / ACTIVE state machine -------------------------------------- #
    def request_pending(self, mode: str) -> "ShadingStateSnapshot":
        """Mark a mode as PENDING and capture the clock snapshot."""
        self.bump_mode()
        snap = self.snapshot()
        self._pending_snapshot = snap
        self.pending_state = PENDING_SHADING_STATE
        return snap
    def validate_commit(self) -> bool:
        """Frame-boundary gate: may the PENDING state commit to ACTIVE?
        Returns True only when dataset_generation and layout_fingerprint
        have not changed since the PENDING snapshot was taken. Camera and
        resource drift are acceptable (the pending mesh is still valid);
        only dataset/layout changes are fatal because they change the
        meaning of block ids and point offsets.
        """
        if self._pending_snapshot is None:
            self.pending_state = ACTIVE_SHADING_STATE
            return False
        p = self._pending_snapshot
        now = self.snapshot()
        if int(p.mode_generation) != int(now.mode_generation):
            self.rejected_commits += 1
            self.rejected_reasons.append("mode_generation drifted")
            self.cancel_pending("superseded mode")
            return False
        if int(p.dataset_generation) != int(now.dataset_generation):
            self.rejected_commits += 1
            self.rejected_reasons.append(
                f"dataset_generation drifted: {p.dataset_generation}"
                f" -> {now.dataset_generation}")
            self._pending_snapshot = None
            self.pending_state = ACTIVE_SHADING_STATE
            return False
        if int(p.layout_fingerprint) != int(now.layout_fingerprint):
            self.rejected_commits += 1
            self.rejected_reasons.append(
                f"layout_fingerprint drifted: {p.layout_fingerprint}"
                f" -> {now.layout_fingerprint}")
            self._pending_snapshot = None
            self.pending_state = ACTIVE_SHADING_STATE
            return False
        return True
    def commit(self) -> None:
        """Accept the PENDING state as ACTIVE (frame boundary)."""
        if self._pending_snapshot is not None:
            self._active_snapshot = self._pending_snapshot
            self._pending_snapshot = None
            self.commits += 1
        self.pending_state = ACTIVE_SHADING_STATE
    def cancel_pending(self, reason: str = "superseded") -> None:
        """Drop the PENDING state (superseded, dataset closed)."""
        self._pending_snapshot = None
        self.pending_state = ACTIVE_SHADING_STATE
    @property
    def has_pending(self) -> bool:
        return self._pending_snapshot is not None
    @property
    def active_snapshot(self) -> Optional["ShadingStateSnapshot"]:
        return self._active_snapshot
    def describe(self) -> str:
        p = self._pending_snapshot.as_dict() if self._pending_snapshot else {}
        a = self._active_snapshot.as_dict() if self._active_snapshot else {}
        return (f"shading={self.pending_state} "
                f"dataset_gen={self.dataset_generation} "
                f"camera_gen={self.camera_generation} "
                f"mode_gen={self.mode_generation} "
                f"resource_gen={self.resource_generation} "
                f"layout_fp={self.layout_fingerprint} "
                f"commits={self.commits} "
                f"rejected={self.rejected_commits} "
                f"pending={p} active={a}")

class NakshaStreamManager:
    """Camera -> hierarchy query -> screen-space LOD -> priority ->
    NKPC read -> selective SoA decode -> RAM cache -> GPU upload ->
    resident tile -> draw ranges.
    The stored-normal attributes below are declared at CLASS level on purpose.
    Several callers (the alignment validator, headless harnesses, tests) build a
    manager via ``__new__`` and assign only the fields they exercise; an
    instance attribute set solely in ``__init__`` would raise AttributeError
    there. Class-level defaults keep those partial constructions working.
    """
    # ---- stored-normal streaming defaults (see normal_streaming.py) -------
    normal_reader = None
    normal_report = None
    display_mode = "rgb"
    total_points = 0
    # Screen-density budget state (Part 1/2). Class-level so partially
    # constructed managers (the alignment validator, headless harnesses) still
    # satisfy telemetry reads.
    density = None
    _vp_w = 0
    _vp_h = 0
    _camera_state = "MOVING"
    _last_budget_target = None
    _last_visible_nodes = 0
    last_overlap_audit = {}
    frame_tick_error_count = 0
    _budget_report_printed_for = None
    normal_gpu_bytes = 0
    normal_upload_count = 0
    normal_resident_points = 0
    new_normal_blocks = 0
    reused_normal_blocks = 0
    stale_normal_requests_dropped = 0
    shaded_mode = False
    splat_px = 0.0
    last_moving = True
    _idle_refine_pending = False
    _normal_source = "screen"
    normal_build_status = None
    normal_service = None
    _normal_reupload_needed = False
    _normal_sig = None
    _points_resident = 0
    last_draw_telemetry = {}
    last_switch_telemetry = {}
    # Camera/selection change-detection state. Class-level, like the other
    # telemetry fields above, so managers built via __new__ in harnesses and
    # tests resolve them instead of raising AttributeError mid-tick.
    _last_cam_sig = None
    _select_cam_sig = None
    _diag_cam_sig = None
    _diag_sel_hash = None
    _last_specs = None
    _last_coverage_report = {}
    _retire_sig = None
    tile_load_events = []
    # ---- CAMERA / LOD / VISIBLE-POINT PARITY GATE state -------------------
    # Class-level like the telemetry fields above, so managers built via
    # ``__new__`` in harnesses and tests still resolve these instead of raising
    # AttributeError mid-tick.
    _diag_guards = None
    diagnostic_error_count = 0
    invalid_camera_sample_count = 0
    rejected_camera_samples = []
    source_oracle = None
    _last_parity_report = None
    _last_parity_signature = None
    _last_selected_keys = frozenset()
    _frame_id = 0
    _last_camera_source = "none"
    # PHASE 12 async-generation ledger. `incorrectly_activated` MUST stay 0.
    stale_completions = 0
    stale_accepted_into_ram = 0
    stale_incorrectly_activated = 0
    _pending_stale_log = None
    _last_selected_generation = 0
    # ---- PART 5/6/7 rendering mode ---------------------------------------
    render_mode = RENDER_STREAMING_LOD
    gpu_estimate = None
    full_resident_specs = None
    full_resident_total_points = 0
    full_resident_ready = False
    full_resident_loading = False
    full_resident_started = 0.0
    camera_only_frames = 0
    # ---- PART E - render state validator (5-generation tracking) -------------
    _render_validator = None
    _active_shading_state = ACTIVE_SHADING_STATE
    def _pending_stale_keys(self):
        """Keys currently carrying a stale-generation marker (PHASE 12)."""
        log = getattr(self, "_pending_stale_log", None)
        if log is None:
            self._pending_stale_log = log = []
        cur = int(getattr(self, "camera_generation", 0) or 0)
        return [tuple(e["key"]) for e in log
                if 0 <= int(e.get("requested_generation", -1)) < cur]
    def _diag_guard(self, name: str) -> DiagnosticGuard:
        """Lazily-created named diagnostic guard.
        Diagnostics are wrapped per-name so one failing reporter cannot take
        down the frame, and so the console reports WHICH reporter failed.
        Renderer and streaming code is never wrapped by this.
        """
        guards = getattr(self, "_diag_guards", None)
        if guards is None:
            guards = {}
            self._diag_guards = guards
        guard = guards.get(name)
        if guard is None:
            guard = DiagnosticGuard(name, owner=self)
            guards[name] = guard
        return guard
    def __init__(self, app, reader, adapter, te, *,
                 ram_budget=None, gpu_budget=None):
        self.app = app
        self.reader = reader
        self.idx = reader.index
        self.adapter = adapter
        self.te = te
        # PART 11/12: budgets come from the MEASURED machine unless the caller
        # passes explicit ones. No 2.7 GB / 4 GB constant is baked in.
        auto = auto_budgets(adapter)
        self.hardware_profile = auto.get("profile")
        self.worker_plan = auto.get("workers", {})
        self.ram_budget = int(ram_budget if ram_budget is not None
                              else auto["ram_budget"])
        self.gpu_budget = int(gpu_budget if gpu_budget is not None
                              else auto["gpu_budget"])
        if self.hardware_profile is not None:
            print(self.hardware_profile.describe())
        self.total_points = int(self.idx.total_points)
        # PART 2: read what the file ACTUALLY contains, once, before any mode is
        # chosen or any attribute is requested.
        self.capabilities = DatasetCapabilities.from_index(
            self.idx, has_normals=bool(getattr(self, "normal_report", None)
                                       is not None
                                       and getattr(self.normal_report,
                                                   "status", "") == "HIT"))
        sys.path.insert(0, os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))))
        from naksha_lod_gate import ScreenSpaceLOD
        self.lod = ScreenSpaceLOD()
        self.ram_cache = OrderedDict()
        self.ram_bytes = 0
        self.ram_hits = 0
        self.ram_misses = 0
        self.resident = _GenDict()
        self.gpu_bytes = 0
        # ---- PHASE 4: compiled SoA selector + LOD reuse window ---------------
        # Built ONCE at open so no JIT compile can ever land on the first pan.
        self._init_fast_lod()
        # ---- STABLE-OFFSET GPU ARENA (Parts 11/12/13/14/15) -----------------
        # `self.arena` hands out [first, first+count) slots in ONE canonical
        # position pool that every display mode shares. It is created lazily, on
        # the first flush, and only when the adapter can host it
        # (`supports_arena()` -> the native `nkv_upload_point_tile` pair).
        #
        # When it is absent the engine falls back to the whole-buffer path it
        # had before: concatenate every resident tile and re-send it. That path
        # is O(all resident points) per residency change, which is exactly what
        # the billion-point target cannot afford - hence the arena.
        self.arena = None
        self._arena_capacity = 0
        # Bumped whenever the native pool is (re)allocated. Every slot in it
        # becomes stale at that moment, so a tile whose `gpu_first` predates the
        # current epoch is re-uploaded even though its slot number is unchanged.
        self._arena_epoch = 0
        self._arena_refused_logged = False
        # PART 14 TELEMETRY. For an ordinary append the last one MUST be 0.
        self.new_blocks_uploaded = 0
        self.new_points_uploaded = 0
        self.existing_points_reuploaded = 0
        self.whole_buffer_rebuilds = 0
        # PART 10. ACTIVE_DRAWN is the frame's submission set. It is a SUBSET of
        # the resident set and is recomputed from the screen-space selection on
        # every flush; residency is NOT touched by a LOD change.
        self.active_draw_keys = ()
        self.active_draw_points = 0
        self.resident_points = 0
        # ---- PART 16: SCREEN-SPACE ACTIVE_DRAWN -----------------------------
        # In FULL_RESIDENT the data stays in VRAM, but the frame must still be
        # built from the SCREEN, not from residency. These hold the latest camera
        # so the frame can re-select without re-running the frontier, plus the
        # cache that makes a settled camera cost nothing.
        self._last_camera = None
        self._last_vp = None
        self._screen_nodes_cache = (None, None)
        self._active_screen_sig = None
        self._last_draw_sig = None
        self.screen_space_selections = 0
        self.draw_range_pushes = 0
        self.screen_space_uncovered = 0
        self.last_screen_space_diag = {}
        # WHY the last selection came from the screen-space gate or from the
        # legacy rule. "screen_space_applies() is True" alone is not evidence
        # that the gate answered - it only means residency is complete.
        self.last_screen_space_reason = "not_requested"
        self.screen_space_fallbacks = 0
        self.screen_space_cached_hits = 0
        self._active_screen_keys = None
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="naksha-stream")
        self._pending = {}
        self._done = queue.Queue()
        self._ram_lock = threading.Lock()
        self._gen = 0
        self._service_gen = 0
        self._resident_sig = None
        self._display_reupload_needed = False
        self._normal_reupload_needed = False
        self._normal_sig = None
        self._points_resident = 0
        # ---- PHASE 1: attribute-stream queues/state ------------------------
        # PER-INSTANCE, not class-level: these are queues and mutable dicts, so a
        # class-level default would be shared by every manager in the process and
        # attribute reads from one dataset could land in another's.
        # `_attr_done` carries completed attribute-only reads; `_attr_errors`
        # records why a block could not be widened (never silently dropped);
        # `_attr_dirty` marks blocks whose attribute streams changed even though
        # the resident KEY set - and therefore the position buffer - did not.
        self._attr_done = queue.Queue()
        self._attr_errors = {}
        self._attr_dirty = set()
        self._attr_backfill_pending = frozenset()
        self._attr_class_sig = None
        self._attr_intensity_sig = None
        self._attr_class_points = 0
        self._attr_intensity_points = 0
        self._last_display_state_push = {}
        self._last_mode_readiness = {}
        self.display_mode = self.capabilities.default_display_mode()
        # PART 1 - GRAY FIRST. A freshly opened LAS/LAZ is a uniform neutral
        # gray cloud. Colour modes are only entered when the user asks, so an
        # RGB-bearing file does NOT open in RGB and a classified file does NOT
        # open rainbow. Neutral needs XYZ only, so this always works.
        print(f"[DATASET CAPABILITIES] {self.capabilities.describe()}")
        print(f"[DISPLAY MODE] default=neutral (GRAY FIRST) - "
              f"requested_preference="
              f"{self.capabilities.preferred_display_mode()!r} is NOT applied "
              f"on open; colour modes are user-selected")
        self._last_mode_substitution = None
        self.streaming_full_point_upload_attempts = 0
        self.position_reuploads = 0
        self.requests = 0
        self.completed = 0
        self.cancelled_stale = 0
        self.queue_depth = 0
        self.evictions = 0
        self.overview_tile = None
        self.camera_generation = 0
        self._viewport = None
        # PAN/ZOOM CONTINUITY DIAGNOSTICS. These exist so a live main.py run
        # can prove WHICH module and WHICH manager object are executing, and
        # whether the overview was actually retired - the failure that made a
        # harness pass while the real GUI never released the overview.
        self._panzoom_fix_version = PAN_ZOOM_FIX_VERSION
        self._diag_enabled = True
        self._ov_diag_state = None
        self._ov_seen_in_draw = False
        print(f"\n[LIVE STREAM MANAGER CREATED]\n"
              f"id: {hex(id(self))}\n"
              f"class: {type(self).__name__}\n"
              f"module: {type(self).__module__}\n"
              f"source file: {os.path.abspath(__file__)}\n"
              f"dataset: {getattr(self.idx, 'path', '?')}\n"
              f"fix_version: {PAN_ZOOM_FIX_VERSION}\n"
              f"[LIVE STREAM MANAGER CREATED]\n")
        self._print_overview_identity()
        # Stage 2 instrumentation: the legacy FileLoaderWorker is never invoked
        # on the cache-first path, so this stays False and is REPORTED, not
        # assumed. The camera-thread vs background-stream split is measured
        # separately so the two can never be conflated again.
        self.legacy_loader_called = False
        self.timing = {"hot_ms": [], "bg_ms": []}
        self.hot_ms_last = 0.0
        self.bg_ms_last = 0.0
        self._pending_ms = []
        # ---- PART 2/9: sealed FULL_RESIDENT camera state -------------------
        # Set when the full-residency load completes. While sealed, the camera
        # path performs NO frontier/signature/residency work at all.
        self._camera_only_sealed = False
        self._sealed_sig = None
        self._sealed_resident_points = 0
        self.camera_seal_count = 0
        self._last_camera_telemetry = 0.0
        # ---- stored-normal streaming state ---------------------------------
        # Normals get their OWN bounded RAM cache. Keeping them out of the
        # generic ram_cache is deliberate: normal blocks are 4 B/point and must
        # never be evicted because an XYZ block displaced them (or vice versa).
        self.normal_reader = None
        self.normal_report = None
        # NOTE: uses self.ram_budget (the RESOLVED budget), never the raw
        # keyword, which is None when the caller relies on auto-detection.
        self.normal_cache = NormalBlockCache(
            budget_bytes=min(DEFAULT_NORMAL_RAM_BUDGET,
                             max(64 * 1024 ** 2, self.ram_budget // 8)))
        self.normal_gpu_bytes = 0
        self.normal_upload_count = 0
        self.normal_resident_points = 0
        self.gate = GenerationGate()
        self.prefetch = NormalPrefetcher()
        self.policy = ShadedActivationPolicy()
        density = ScreenDensityBudget()
        self.density = density
        self._vp_w = 0
        self._vp_h = 0
        self._camera_state = "MOVING"
        self.last_overlap_audit = {}
        self.new_normal_blocks = 0
        self.reused_normal_blocks = 0
        self.stale_normal_requests_dropped = 0
        self.shaded_mode = False
        self.last_moving = True
        self._last_camera_change = time.perf_counter()
        self._idle_refine_pending = False
        self._normal_source = "screen"
        self.splat_px = 0.0
        self.last_draw_telemetry = {}
        self.last_switch_telemetry = {}
        self.tile_load_events = []
        self._last_camera_signature = None
        self._diag_camera_signature = None
        self._render_validator = RenderStateValidator()
        self._active_shading_state = ACTIVE_SHADING_STATE
    # ------------------------------------------------------------------ #
    # PART 1 - attach the opened sidecar.                                 #
    # ------------------------------------------------------------------ #
    def dataset_bounds(self):
        """(min_xyz, max_xyz) from the NKIDX header - authoritative, and the
        ONLY valid source in STREAMING (app.data is empty by design)."""
        try:
            hdr = self.idx.header
            bmin = tuple(float(v) for v in hdr["bounds_min"])
            bmax = tuple(float(v) for v in hdr["bounds_max"])
            return bmin, bmax
        except Exception:
            return None
    @property
    def source_point_count(self) -> int:
        try:
            return int(self.idx.header["source_point_count"])
        except Exception:
            return 0
    @property
    def dataset_center(self):
        b = self.dataset_bounds()
        if b is None:
            return (0.0, 0.0, 0.0)
        bmin, bmax = b
        return tuple((bmin[i] + bmax[i]) * 0.5 for i in range(3))
    def attach_normal_cache(self, reader, report) -> None:
        """Install the real sidecar reader + its measured status.
        Called once at dataset open. Only DIRECTORY/reader state is kept; the
        payload is never materialised here.
        PHASE 6C.2 - WHY THE CAPABILITY IS REFRESHED HERE. `capabilities` is
        built in __init__ from the INDEX, and this method runs AFTER that: the
        sidecar is attached by the caller once the manager exists. So a
        PERFECT stored-normal HIT still left `capabilities.has_normals` False,
        `DatasetCapabilities.intersect()` then dropped ATTR_NORMAL from every
        required-attribute set, readiness reported `normal` as an UNSUPPORTED
        attribute, and Shading could never reach READY. The switch was accepted
        and then silently stuck on the previous mode - with a valid 27M-point
        normal cache sitting on disk, untouched (measured: normal_uploads=0,
        normal_resident_points=0).
        The capability now follows what was ACTUALLY attached. A MISS keeps a
        normal SOURCE (the flat +Z placeholder below, or the async builder in
        Phase 6D), so `normal` stays a PENDING requirement rather than becoming
        an unsupported one.
        """
        first_attachment = self.normal_report is None
        self.normal_reader = reader
        self.normal_report = report
        # Publishing a derived stream does not replace the point dataset.
        # Preserve the identity carried by in-flight blocks and mode requests.
        if first_attachment:
            self.gate.new_dataset()
        status = getattr(report, "status", "MISS")
        stored = bool(status == "HIT" and reader is not None)
        self._normal_source = "stored" if stored else "screen"
        # PHASE 6C.8 - HONEST LABEL. "screen" is NOT a screen-space normal
        # reconstruction: the point Shading shader (native instant.frag) reads
        # the STORED oct16 stream from normalTex and nothing else, and only
        # surface.frag uses dFdx/dFdy. With no normal stream the splat pass
        # writes no normal, `octDecode` returns +Z, and the cloud is lit as a
        # FLAT ground plane: plausible-looking and wrong. It is a placeholder
        # while real normals are built (Phase 6D), never a parity path.
        self.normal_fallback_mode = "" if stored else "flat_up"
        self._refresh_normal_capability()
        if not stored:
            print(f"[SHADED NORMALS] stored normals UNAVAILABLE "
                  f"(normal_cache={status}: "
                  f"{getattr(report, 'reason', '') or 'no reader'}) "
                  f"-> FLAT +Z placeholder lighting (not a normal "
                  f"reconstruction); Phase 6D build required for real Shading")
    def _refresh_normal_capability(self) -> bool:
        """Make `capabilities.has_normals` describe the ATTACHED normal source.
        One place, called from attach_normal_cache and from the async builder
        when its first blocks land, so the capability can never drift from the
        state that actually feeds the shader.
        """
        caps = getattr(self, "capabilities", None)
        if caps is None:
            return False
        # DatasetCapabilities is FROZEN (it is a statement about the file), so
        # it is rebuilt through dataclasses.replace rather than mutated.
        import dataclasses
        # A normal SOURCE exists even on a MISS (flat +Z placeholder now, the
        # Phase 6D async builder next), which is what keeps `normal` a PENDING
        # requirement - not an unsupported one - instead of refusing the mode
        # outright and leaving the user with a mode that can never become READY.
        self.capabilities = dataclasses.replace(caps, has_normals=True)
        return True
    @property
    def normal_source(self) -> str:
        return self._normal_source
    def _backfill_resident_normals(self) -> int:
        """Attach STORED normals to blocks that are already resident (6C.2).
        The worker reads a block's normals only in the pass that CREATES it
        (`_read_block` -> `_attach_normal`). A block already resident from
        Neutral is never re-read, so on entering Shading every resident block
        kept `normal = None`, `_flush_gpu` had nothing to concatenate,
        `normal_resident_points` stayed 0 and readiness stayed PENDING no matter
        how good the sidecar was.
        Each fix is an O(1) seek into the sidecar directory (the block id is the
        join key), bounded by the RESIDENT set - which is bounded by the GPU
        budget - not by the dataset. Anything already attached with the right
        length is left alone, so re-entering Shading costs nothing.
        """
        reader = getattr(self, "normal_reader", None)
        if reader is None or not getattr(self, "resident", None):
            return 0
        attached = 0
        for key, v in list(self.resident.items()):
            pts = int(getattr(v, "count", 0) or 0)
            if pts <= 0:
                continue
            have = getattr(v, "normal", None)
            if have is not None and int(getattr(have, "shape", (0,))[0]) == pts:
                continue
            bid = int(getattr(v, "block_id", -1) or -1)
            if bid < 0:
                try:
                    entry = self.idx.find_block(key[0], key[1])
                    bid = int(entry["block_id"]) if entry is not None else -1
                except Exception:
                    bid = -1
            if bid < 0:
                continue
            blob, reason = load_normal_block(
                reader, self.normal_cache, self.gate.dataset_generation, bid,
                verify_crc=True)
            if blob is None:
                v.normal_error = reason
                continue
            if int(blob.shape[0]) != pts:
                # Refuse rather than upload a misaligned stream: an off-by-N
                # shifts every normal and shades a plausible but wrong scene.
                v.normal_error = (f"block {bid}: normal count "
                                  f"{int(blob.shape[0]):,} != points {pts:,}")
                continue
            v.normal = blob
            v.normal_error = ""
            attached += 1
        if attached:
            self._normal_reupload_needed = True
            _RES_GEN[0] += 1
            print(f"[SHADED NORMALS] backfilled {attached} resident block(s) "
                  f"from the stored sidecar ({self._normal_source})", flush=True)
        return attached

    def _needs_normal(self) -> bool:
        """True when the CURRENT mode needs normals (Part 2, one source)."""
        return bool(self.shaded_mode) or requires_normal(self.display_mode)
    def open_first_frame(self) -> dict:
        """First paint: read the overview block, make it the never-blank floor."""
        t0 = time.perf_counter()
        # PART 6/7: decide the rendering mode BEFORE the first paint, so a
        # dataset that fits gets the one-time full upload instead of a slow LOD
        # climb that would visibly thin out first and then swap tiles.
        if getattr(self, "render_mode", None) in (None, RENDER_STREAMING_LOD) \
                and getattr(self, "gpu_estimate", None) is None:
            self.select_render_mode(needs_normal=bool(self._needs_normal()))
        ov = self.reader.read_overview()
        if ov is not None:
            # The overview block is stored with node_id 0 and lod = lod_count-1,
            # while header['overview_block_id'] is its BLOCK id (1338), not its
            # node id. The key must therefore be built from the BLOCK's own row,
            # not from overview_block_id - otherwise find_block() misses and the
            # sidecar lookup (and the RAM cache key) silently uses a node that
            # does not exist. The node id is what ScreenSpaceLOD selects on.
            ov_bid = int(self.idx.overview_block_id)
            ov_lod = int(self.idx.header["lod_count"]) - 1
            entry = self.idx.find_block(ov_bid, ov_lod)
            if entry is None:
                for row in self.idx.blocks:
                    if int(row["block_id"]) == ov_bid:
                        entry = row
                        break
            if entry is None:
                ov_key = (ov_bid, ov_lod)
            else:
                ov_key = (int(entry["node_id"]), int(entry["lod"]))
            packed = self._pack_block(ov, ov_key)
            # The overview is the FLOOR for shaded. If its normals are absent it
            # is not a complete block, and every "keep coarse until fine is
            # ready" decision downstream would be keeping an unusable block.
            spec = TileSpec(node_id=ov_key[0], lod=ov_key[1], priority=0.0,
                            points=int(packed.get("pts", 0)), px_err=0.0, row=0)
            self._attach_normal(packed, spec, self._required_attrs())
            nb = int(packed.get("normal_bytes", 0) or 0)
            self.overview_tile = packed
            with self._ram_lock:
                self.ram_cache[ov_key] = packed
                self.ram_bytes += packed["bytes"]
            self.resident[ov_key] = ResidentTile(
                node_id=ov_key[0], lod=ov_key[1], count=packed["pts"],
                bytes=packed["bytes"] + nb, priority=0.0,
                last_used=time.perf_counter(), state=GPU_RESIDENT,
                xyz=packed["xyz"], rgb=packed.get("rgb"),
                cls=packed.get("cls"), inten=packed.get("inten"),
                block_id=int(packed.get("block_id", -1)),
                normal=packed.get("normal"),
                normal_from_cache=bool(packed.get("normal_from_cache", False)),
                readiness=packed.get("readiness"),
                normal_error=str(packed.get("normal_error", "") or ""),
                requires_normal=ATTR_NORMAL in self._required_attrs())
            self.gpu_bytes = packed["bytes"] + nb
            # NOTE: do NOT pre-set _resident_sig here; _flush_gpu must detect
            # the change from None and perform the first overview upload.
        self._flush_gpu(reason="open_first_frame")
        # Part 2: prefetch the OVERVIEW normal block immediately, without
        # waiting for the user to click Shaded. The overview is the only block
        # guaranteed resident at open, so its normal (299,676 x 4 = 1,198,704 B)
        # is what makes the first Shaded click able to use normal_source=stored.
        # This runs AFTER the first paint, so it never delays the first frame.
        try:
            ov_bid = int(packed.get("block_id", -1)) if ov is not None else -1
            if ov_bid >= 0:
                blob, why = load_normal_block(
                    self.normal_reader, self.normal_cache,
                    self.gate.dataset_generation, ov_bid, verify_crc=True)
                if blob is not None:
                    print(f"[NORMAL PREFETCH] overview block_id={ov_bid} "
                          f"points={int(blob.shape[0]):,} "
                          f"normal_bytes={int(blob.nbytes):,} "
                          f"source={why}")
                else:
                    print(f"[NORMAL PREFETCH] overview block_id={ov_bid} "
                          f"FAILED: {why}")
        except Exception as exc:
            print(f"[NORMAL PREFETCH] error: {exc!r}")
        self.te.event("first_frame",
                      overview_points=int(ov["point_count"]) if ov else 0,
                      overview_block=self.idx.overview_block_id,
                      ms=(time.perf_counter() - t0) * 1000)
        # PART 7: if the whole dataset fits, start the ONE full upload now. The
        # overview first paint above already guarantees never-blank, so the
        # handover to full resolution cannot open a hole.
        if getattr(self, "render_mode", RENDER_STREAMING_LOD) == \
                RENDER_FULL_RESIDENT:
            self.load_full_resident(reason="open_first_frame")
        self._sample(viewport=None, reason="first_frame")
        return {"overview_block": self.idx.overview_block_id,
                "overview_points": int(ov["point_count"]) if ov else 0,
                "render_mode": getattr(self, "render_mode", None),
                "gpu_estimate": (self.gpu_estimate.as_dict()
                                 if getattr(self, "gpu_estimate", None)
                                 else None)}
    # ------------------------------------------------------------------ #
    # PHASE 1/2 - canonical camera generation + sample validity.            #
    # ------------------------------------------------------------------ #
    def _reject_camera_sample(self, sample: CameraSample) -> None:
        """PHASE 2. Announce a rejected sample and mutate NOTHING.
        A degenerate sample (world size 0, NaN, non-positive viewport) must not
        become a streaming generation and must not touch the desired frontier,
        the service generation, the active frontier or the camera state.
        """
        self.invalid_camera_sample_count = int(
            getattr(self, "invalid_camera_sample_count", 0) or 0) + 1
        print("[CAMERA SAMPLE REJECTED]")
        print(f"source: {sample.source}")
        print(f"reason: {sample.reason}")
        print(f"signature: {sample.signature}")
        log = getattr(self, "rejected_camera_samples", None)
        if log is None:
            log = []
            self.rejected_camera_samples = log
        log.append({"source": sample.source, "reason": sample.reason,
                    "signature": sample.signature,
                    "generation": int(getattr(self, "camera_generation", 0)
                                      or 0)})
        del log[:-64]     # bounded: a rejection storm must not grow unbounded
    def _canonical_camera(self, camera, viewport, source: str):
        """THE single camera-generation authority (PHASE 1).
        Returns ``(sample, changed)``. The generation increments ONLY when the
        canonical signature differs from the previous one, so three raw events
        (`camera_modified`, `render_start`, `RenderEvent`) describing the SAME
        camera all map to ONE generation. An invalid sample is rejected here and
        never reaches any caller that could mutate streaming state.
        """
        sample = read_camera_sample(camera, viewport, source=source)
        if not sample.ok:
            self._reject_camera_sample(sample)
            return sample, False
        previous = getattr(self, "_last_camera_signature", None)
        changed = bool(sample.signature != previous)
        if changed:
            self.camera_generation = int(
                getattr(self, "camera_generation", 0) or 0) + 1
            self._last_camera_signature = sample.signature
        self._last_camera_source = source
        return sample, changed
    def on_camera_changed(self, camera, viewport, generation=None,
                          source: str = "camera_change") -> dict:
        """Latest-camera-wins entry.
        PHASE 1: ``generation`` is no longer an externally-minted ticket that
        increments per RAW EVENT. The canonical generation is derived here from
        the camera signature alone, so identical camera states never produce
        200 / 201 / 202. ``generation`` is accepted for backwards compatibility
        and reported, but it is never used to advance the streaming generation.
        HARD CAMERA-HOT-PATH RULE: this runs on the UI thread and may only do
        camera capture, a cheap hierarchy query, the cheap LOD request
        calculation, the generation update and queue submission. It never
        touches disk, never CRCs, never decodes, never waits on a worker.
        ``_drain_done`` below is a NON-BLOCKING poll of an already-finished
        future queue -- it performs no I/O and no wait, so the camera thread
        still returns immediately.
        """
        _t0 = time.perf_counter()
        _st = getattr(self, "lod_stats", None)
        if _st is not None:
            _st["raw_callbacks"] += 1          # PART 52: raw events seen
        sample, changed = self._canonical_camera(camera, viewport, source)
        if not sample.ok:
            # No frontier, service generation, active frontier or camera state
            # was touched: the sample never became a streaming generation.
            return {"dropped": True, "reason": "invalid_camera_sample",
                    "invalid_samples": self.invalid_camera_sample_count}
        generation = int(getattr(self, "camera_generation", 0) or 0)
        if not changed:
            # Identical camera signature -> SAME generation. Re-running selection
            # would be pure duplicated work and would spam the frontier.
            return {"dropped": True, "reason": "unchanged_camera",
                    "generation": generation}
        # PART 7: in FULL_RESIDENT the entire dataset is already selected and
        # resident. The camera path must not re-run LOD selection here, or it
        # would add EXTRA blocks on top of the full set - which is how a
        # "complete" dataset ends up over-resident and over-drawn.
        #
        # PART 9: it must also skip the frontier/signature/residency work. In
        # FULL_RESIDENT the resident SET CANNOT CHANGE during camera movement,
        # so recomputing the key set, the point sum and the telemetry sample
        # per mouse event is O(N_blocks) work whose answer is already known.
        # That is the O(N) cost that made pan/zoom feel heavy on big datasets.
        if getattr(self, "render_mode", RENDER_STREAMING_LOD) == \
                RENDER_FULL_RESIDENT:
            return self._camera_only_frame(camera, viewport, sample,
                                           generation, _t0)
        self.requests += 1
        # Part 8: any camera change resets the idle clock, so the FIRST pass
        # after motion is MOVING. on_frame() below promotes it to IDLE.
        self.note_camera_motion()
        self._vp_w, self._vp_h = self._measure_viewport(viewport, camera)
        # Part 2: push the density budget into the LOD gate BEFORE selecting.
        # This is the line that changes which blocks become active.
        self._density(self).apply(self.lod, self._vp_w, self._vp_h,
                           moving=not self.is_idle())
        if generation < self._service_gen:
            self.cancelled_stale += 1
            self.te.event("camera_stale", generation=generation,
                          served_gen=self._service_gen)
            return {"dropped": True, "reason": "stale_generation"}
        self._gen = generation
        self._viewport = viewport
        self._vp_w, self._vp_h = self._measure_viewport(viewport, camera)
        visible_rows = self._visible_node_rows(camera, viewport)
        self._last_visible_nodes = len(visible_rows)
        self._drain_done()
        selections = self.lod.select(self.idx, camera, visible_rows)
        self.density.note_candidates(selections)
        specs = [TileSpec(node_id=int(o["node_id"]), lod=int(o["lod"]),
                          priority=float(o.get("priority", o["centre_dist"])),
                          points=int(o["points"]), px_err=float(o["px_err"]),
                          row=int(o["row"])) for o in selections]
        specs.sort(key=lambda s: -s.priority)
        self.density.note_selected(sum(s.points for s in specs))
        requested = sum(s.points for s in specs)
        new_tiles = 0
        for s in specs:
            key = (s.node_id, s.lod)
            if key in self.resident and \
               self.resident[key].state == GPU_RESIDENT:
                self.resident[key].last_used = time.perf_counter()
                self.ram_hits += 1
                continue
            if key in self._pending or key in self.ram_cache:
                continue
            self.ram_misses += 1
            self._pending[key] = self._submit_read(
                s, int(getattr(self, "camera_generation", 0) or 0))
            self._pending[key].add_done_callback(self._read_tile_done)
            new_tiles += 1
        self.queue_depth = len(self._pending)
        self._reconcile(specs)
        gpu_info = self._flush_gpu(reason="camera", gen=generation,
                                   requested=requested, specs=specs)
        self._service_gen = generation
        self.hot_ms_last = (time.perf_counter() - _t0) * 1000.0
        self.timing["hot_ms"].append(self.hot_ms_last)
        # Background stream time is whatever the WORKER threads already spent
        # decoding since the last drain. It is reported separately and is NOT
        # charged to the camera thread.
        self.bg_ms_last = self._collect_bg_ms()
        self._sample(camera=camera, viewport=viewport, specs=specs,
                     requested_points=requested, reason="camera_change",
                     generation=generation,
                     camera_hot_ms=round(self.hot_ms_last, 3),
                     background_ms=round(self.bg_ms_last, 3), **gpu_info)
        return {"dropped": False, "visible_nodes": len(visible_rows),
                "selections": len(specs), "requested_points": requested,
                "new_tiles": new_tiles,
                "camera_hot_ms": self.hot_ms_last,
                "background_ms": self.bg_ms_last, **gpu_info}
    def _collect_bg_ms(self) -> float:
        """Total worker decode time completed since the previous call."""
        total = 0.0
        for d in getattr(self, "_pending_ms", []):
            total += d
        self._pending_ms = []
        return total
    # ------------------------------------------------------------------ #
    # PART 2/9 - the FULL_RESIDENT camera frame                            #
    # ------------------------------------------------------------------ #
    def _camera_only_frame(self, camera, viewport, sample, generation, t0):
        """A camera frame in FULL_RESIDENT costs O(1), not O(N_blocks).
        Every point is already on the GPU, so the only thing a pan/zoom/orbit/
        Fit may legitimately do here is update the camera. This method
        deliberately performs:
            NO  source or NKPC reread
            NO  decompression
            NO  TileSpec rebuild / LOD selection / frontier reconciliation
            NO  point permutation, concat or NumPy copy
            NO  point-buffer rebuild, reallocation or upload
            NO  vkDeviceWaitIdle / vkQueueWaitIdle
            NO  per-event telemetry file write (PART 7)
        The resident set is SEALED once full residency completes, so the key set,
        the point count and the draw ranges are already known and are reused
        instead of being recomputed for every mouse event.
        """
        from . import interaction_profile as _prof
        self.requests += 1
        self.note_camera_motion()
        # PART 26: this runs for EVERY input event, so it must stay cheap. It
        # only RECORDS the camera; the selection it feeds runs at frame cadence
        # in `on_frame`, so 300 mouse events do not mean 300 selections.
        self._note_camera(camera, viewport)
        self._vp_w, self._vp_h = self._measure_viewport(viewport, camera)
        self._viewport = (sample.center[0], sample.center[1],
                          sample.world_size[0] * 0.5, sample.world_size[1] * 0.5)
        self._gen = generation
        self._service_gen = generation
        self.camera_only_frames = int(
            getattr(self, "camera_only_frames", 0) or 0) + 1
        # Non-blocking drain of ALREADY-FINISHED read futures. In FULL_RESIDENT
        # this is normally empty; it performs no I/O and no wait.
        if self._pending:
            self._drain_done()
        # Draw ranges only change when the sealed resident set changes, which
        # camera movement cannot cause. Re-pushing them would be a redundant
        # draw-call configuration update (PART 19/20).
        if not getattr(self, "_camera_only_sealed", False):
            self._seal_camera_only()
        self.hot_ms_last = (time.perf_counter() - t0) * 1000.0
        # getattr: a partial fixture (built via __new__) may have no timing
        # deque; the hot-path number is a diagnostic and must never be the
        # reason a camera frame raises.
        timing = getattr(self, "timing", None)
        if isinstance(timing, dict) and "hot_ms" in timing:
            try:
                timing["hot_ms"].append(self.hot_ms_last)
            except Exception:
                pass
        _prof.bump("camera_events")
        _prof.bump("camera_uniform_updates")
        # PART 7/44: telemetry is THROTTLED, not per-event. A file write per
        # mouse event is real disk I/O on the hot path and was a measured cost.
        self._camera_only_telemetry(camera, viewport, generation)
        return {"camera_only": True, "generation": generation,
                "uploads_this_frame": 0, "position_reuploads": 0,
                "disk_reads": 0, "decompressions": 0, "draw_ranges": 0,
                "resident_points": int(
                    getattr(self, "_sealed_resident_points", 0) or 0)}
    def _seal_camera_only(self) -> None:
        """Freeze the resident set + draw ranges ONCE (first camera frame)."""
        from . import interaction_profile as _prof
        keys = [k for k, v in self.resident.items() if v.state == GPU_RESIDENT]
        with _prof.stage("frontier"):
            self._sealed_resident_points = int(
                sum(self.resident[k].count for k in keys)) if keys else 0
            self._sealed_sig = frozenset(keys)
            # One draw-range configuration for the whole sealed set.
            # getattr, not self.adapter: several callers build a manager via
            # __new__ with only the fields under test, so a hard attribute
            # access here made a partial fixture crash.
            adapter = getattr(self, "adapter", None)
            if adapter is not None:
                # PART 16: the FIRST configuration is already the screen-space
                # one, so a FULL_RESIDENT open does not briefly submit every
                # resident point and then correct itself a frame later.
                ranges = self._build_ranges(
                    keys, screen_space=self._screen_space_applies(),
                    camera=getattr(self, "_last_camera", None),
                    viewport=getattr(self, "_last_vp", None))
                self._last_draw_sig = tuple(ranges)
                adapter.set_draw_ranges(ranges)
        self._resident_sig = self._sealed_sig
        self._camera_only_sealed = True
        self.camera_seal_count = int(
            getattr(self, "camera_seal_count", 0) or 0) + 1
    def invalidate_camera_seal(self) -> None:
        """Un-seal when residency ACTUALLY changes.
        The seal is an optimisation, so it must never be able to hide a real
        change. Any path that adds/removes a resident block, changes a display
        mode, or retires a tile calls this, and the next camera frame re-seals.
        """
        if getattr(self, "_camera_only_sealed", False):
            self._camera_only_sealed = False
            self.camera_seal_invalidations = int(
                getattr(self, "camera_seal_invalidations", 0) or 0) + 1
    def _camera_only_telemetry(self, camera, viewport, generation):
        """Write a camera telemetry sample at most every CAMERA_TELEMETRY_MS."""
        now = time.perf_counter()
        last = float(getattr(self, "_last_camera_telemetry", 0.0) or 0.0)
        if (now - last) < CAMERA_TELEMETRY_MS:
            return
        self._last_camera_telemetry = now
        try:
            self._sample(camera=camera, viewport=viewport,
                         reason="camera_only", generation=generation,
                         camera_hot_ms=round(self.hot_ms_last, 3))
        except Exception:
            pass
    def _perf_record_frame(self, *, stage_camera_ms, stage_flush_ms, total_ms,
                           idle: bool) -> None:
        """[FRAME PERFORMANCE] + [INTERACTION PERFORMANCE] (Phases 1 and 28).
        Collects a bounded ring of samples and prints a SUMMARY once every
        `PERF_REPORT_EVERY` frames - never per frame. Printing at 60 Hz would
        cost more than the path being measured and would drown every other line
        in the log.
        Everything here is READ from live objects; nothing is inferred.
        """
        P = getattr(self, "_perf", None)
        if P is None:
            P = self._perf = {"frames": [], "n": 0, "t0": time.perf_counter(),
                              "gen": None, "uploads": None}
        P["n"] += 1
        keys = [k for k, v in self.resident.items()
                if getattr(v, "state", None) == GPU_RESIDENT]
        submitted = sum(int(self.resident[k].count) for k in keys)
        try:
            st = self.adapter.stats()
            uploads = int(st.get("uploads", 0) or 0)
        except Exception:
            uploads = -1
        P["frames"].append({
            "camera_ms": stage_camera_ms, "flush_ms": stage_flush_ms,
            "total_ms": total_ms, "idle": bool(idle),
            "submitted": submitted, "blocks": len(keys), "uploads": uploads,
        })
        if len(P["frames"]) > 4096:
            del P["frames"][:2048]
        every = int(os.environ.get("NAKSHA_PERF_REPORT_EVERY", "600") or 600)
        if P["n"] % every:
            return
        fr = P["frames"]
        ms = sorted(f["total_ms"] for f in fr)
        cm = sorted(f["camera_ms"] for f in fr)
        fm = sorted(f["flush_ms"] for f in fr)
        sp = sorted(f["submitted"] for f in fr)
        elapsed = (time.perf_counter() - P["t0"]) * 1000.0
        fps = (len(fr) / (elapsed / 1000.0)) if elapsed > 0 else 0.0
        print("[INTERACTION PERFORMANCE] "
              f"mode={getattr(self, 'display_mode', '?')} "
              f"camera_state={getattr(self, '_camera_state', '?')}", flush=True)
        print(f"[INTERACTION PERFORMANCE] fps={fps:.1f} "
              f"frame_ms_p50={ms[len(ms)//2]:.2f} "
              f"frame_ms_p95={ms[min(len(ms)-1, int(len(ms)*0.95))]:.2f} "
              f"frame_ms_max={ms[-1]:.2f}", flush=True)
        print(f"[INTERACTION PERFORMANCE] "
              f"camera_ms_p50={cm[len(cm)//2]:.2f} "
              f"flush_ms_p50={fm[len(fm)//2]:.2f} "
              f"flush_ms_p95={fm[min(len(fm)-1, int(len(fm)*0.95))]:.2f} "
              f"uploads={fr[-1]['uploads']} "
              f"draw_ranges={fr[-1]['blocks']}", flush=True)
        print(f"[INTERACTION PERFORMANCE] "
              f"submitted_points_p50={sp[len(sp)//2]:,} "
              f"submitted_points_p95={sp[min(len(sp)-1, int(len(sp)*0.95))]:,} "
              f"resident_points={getattr(self, '_points_resident', 0):,}", flush=True)
        P["frames"].clear()
        P["t0"] = time.perf_counter()
    def _atomic_swap(self) -> "AtomicModeSwap":
        """The manager's single atomic render-state machine (Phases 24/25/26).
        One per manager, created lazily: a class-level instance would let one
        dataset's pending mode leak into another's viewport.
        """
        s = getattr(self, "_swap", None)
        if s is None:
            s = self._swap = AtomicModeSwap(
                initial_mode=str(getattr(self, "display_mode", "neutral")))
        return s
    def _commit_atomic_mode_swap(self) -> bool:
        """Frame-boundary commit + Phase 35 `[ATOMIC MODE SWAP]` telemetry.
        Every number printed here is READ: the XYZ reupload count is the
        difference in the live adapter counter across the commit, not an
        assumption.
        """
        s = self._atomic_swap()
        if s.pending is None:
            return False
        # Readiness is evaluated against the current resident group, even if
        # a camera move or eviction happened after the completion callback.
        self._maybe_complete_pending_mode()
        if not s.pending.committable() or not s.pending.coverage_ready:
            return False
        xyz_before = self._xyz_upload_count()
        prev_mode = s.active.mode
        # Phase 8: validate atomic swap before committing
        if getattr(self, "_render_validator", None) is not None:
            self._render_validator.dataset_generation = self.gate.dataset_generation
            self._render_validator.layout_fingerprint = int(self.idx.layout_fingerprint)
            if not self._render_validator.validate_commit():
                s.cancel("validator_reject")
                self._pending_display_mode = None
                return False
        # A refused native transition must leave both ACTIVE states intact.
        if not self.adapter.set_display_mode(s.pending.mode):
            s.pending.pipeline_ready = False
            s.pending.reason = "native mode transition refused"
            return False
        committed = s.commit_if_ready()
        if not committed:
            return False
        self.display_mode = s.active.mode
        self.shaded_mode = requires_normal(self.display_mode)
        self._pending_display_mode = None
        self._last_rendered_mode = s.active.mode
        if self.display_mode == "surface" and getattr(self, "surface_service", None) is not None:
            self.surface_service.timestamps["T5"] = time.perf_counter()
            self.surface_service.timestamps["T6"] = time.perf_counter()
        if self.shaded_mode and getattr(self, "normal_service", None) is not None:
            self.normal_service.timestamps.mark("t6_atomic_swap")
            self.normal_service.timestamps.mark("t7_first_shaded")
        self._adapter_mode = self.display_mode
        for tile in self.resident.values():
            tile.requires_normal = self.shaded_mode
        self.invalidate_camera_seal()
        # An atomic mode commit changes what the NEXT presented frame must draw.
        # Without an explicit render request the swapchain can sit showing the
        # PREVIOUS mode until the next incidental repaint, so a caller that
        # inspects the draw record immediately after the commit sees the old
        # mode's counters (points) rather than the new mode's (indexed Surface
        # triangles). Requesting the frame here makes the commit visible at the
        # very next frame boundary, which is what "atomic" has to mean.
        try:
            self.adapter.request_render()
        except Exception:
            pass
        prep = float(getattr(s, "pending_preparation_ms", 0.0) or 0.0)
        hit = getattr(s, "pending_cache_hit", None)
        print(f"[ATOMIC MODE SWAP] from={prev_mode} to={s.active.mode} "
              f"cache_hit={hit} preparation_ms={prep:.1f} upload_ms=0.000 "
              f"ready_to_commit=YES commit_frame=YES "
              f"black_frames={s.black_frames} "
              f"partial_frames={s.partial_frames} "
              f"xyz_reuploads={max(0, self._xyz_upload_count() - xyz_before)}",
              flush=True)
        # Phase 8: commit validator clock after successful swap
        if getattr(self, "_render_validator", None) is not None:
            self._render_validator.commit()
        return True
    def on_frame(self, camera, viewport) -> dict:
        """Per-frame tick. Camera motion for POSITION only must NOT re-upload
        geometry -- only draw-range reordering + render origin."""
        _t0 = time.perf_counter()
        # PHASE 12 - FRAME-BOUNDARY COMMIT. A pending mode is promoted only
        # here, once per frame, and only when every resource it needs is ready.
        # Until then the ACTIVE mode keeps drawing, so a switch can never blank
        # the viewport (Phase 26).
        # NAKSHA_DEV_PERF (Phase 1): high-resolution CPU timers around the REAL
        # production path. Off by default - on_frame runs at 60 Hz and the
        # instrumentation must not become the bottleneck it measures.
        _perf = os.environ.get("NAKSHA_DEV_PERF", "").strip() not in (
            "", "0", "false", "False")
        # PART E - Phase 8E: capture the 5-generation ShadingStateSnapshot
        # BEFORE mode evaluation so the validator can detect drift (dataset
        # layout, camera move, mode change, resource shift) at the commit
        # boundary. This snapshot is compared against the PENDING snapshot
        # captured in request_pending() at the frame boundary commit.
        if getattr(self, "_render_validator", None) is not None:
            try:
                self._active_shading_state = self._render_validator.snapshot()
            except Exception:
                pass
        self._drain_done()
        self._drain_normal_delivery()
        self._vp_w, self._vp_h = self._measure_viewport(viewport, camera)
        # Part 8: on_frame runs continuously, so THIS is where MOVING becomes
        # IDLE (after IDLE_REFINE_MS of no camera motion) and the budget is
        # raised for refinement. No geometry rebuild, no blocking.
        idle = self.is_idle()
        new_state = "IDLE" if idle else "MOVING"
        prev = getattr(self, "_camera_state", None)
        # Part 3: announce the transition ONCE, never every tick. Printing each
        # frame floods the console at 60 Hz and hides everything else.
        if prev != new_state:
            self._camera_state = new_state
            self.frame_tick_error_count = int(
                getattr(self, "frame_tick_error_count", 0) or 0)
            dt_ms = (time.perf_counter()
                     - self._last_camera_change) * 1000.0
            print(f"[STREAM CAMERA STATE]\n{prev or 'NONE'} -> {new_state}\n"
                  f"idle_ms: {dt_ms:.0f}\n[/STREAM CAMERA STATE]")
            try:
                self.te.event("camera_state", **{"from": prev, "to": new_state,
                                                 "idle_ms": round(dt_ms, 1)})
            except Exception:
                pass
        # `specs` is the budget-aware SELECTION, recomputed only when the target
        # changes. It MUST be persisted between ticks: recomputing it only on a
        # budget change and defaulting to [] otherwise would silently collapse
        # the frontier back to whatever is already resident (the overview),
        # which is precisely the live 299,676-point bug.
        specs = list(getattr(self, "_last_specs", None) or [])
        camera_diag_selections = [
            {"node_id": int(s.node_id), "lod": int(s.lod), "points": int(s.points)}
            for s in specs
        ]
        # PHASE 1/2: on_frame is the SECOND camera entry point, so it must ask the
        # SAME canonical authority whether the camera moved. It previously
        # compared signatures on its own and bumped the generation itself, which
        # is how one camera move produced two or three generation increments.
        sample, camera_changed = self._canonical_camera(
            camera, viewport, "on_frame")
        if not sample.ok:
            # Invalid sample: return without touching the frontier, the service
            # generation or the active frontier.
            self.hot_ms_last = (time.perf_counter() - _t0) * 1000.0
            return {"dropped": True, "reason": "invalid_camera_sample",
                    "invalid_samples": self.invalid_camera_sample_count}
        self._viewport = (sample.center[0], sample.center[1],
                          sample.world_size[0] * 0.5, sample.world_size[1] * 0.5)
        cam_sig = sample.signature
        self._surface_tick()
        self._commit_atomic_mode_swap()
        # ---------------------------------------------------------------- #
        # PART 7 - FULL_RESIDENT: the whole dataset is already on the GPU,  #
        # so a pan/zoom/orbit/fit is CAMERA-ONLY. Returning here is what    #
        # guarantees no LOD re-selection, no replacement group, no          #
        # retirement and no disk read during interaction. During the ONE    #
        # initial upload the pump must still run.                           #
        # ---------------------------------------------------------------- #
        if getattr(self, "render_mode", RENDER_STREAMING_LOD) == \
                RENDER_FULL_RESIDENT and getattr(self, "full_resident_ready", False):
            self.camera_only_frames = int(
                getattr(self, "camera_only_frames", 0) or 0) + 1
            # PHASE 1: split the camera-only path into its real stages so the
            # bottleneck is measurable rather than guessed.
            _t_cam = time.perf_counter()
            self.hot_ms_last = (time.perf_counter() - _t0) * 1000.0
            self.bg_ms_last = self._collect_bg_ms()
            # PHASE 4/5: `interaction` decides WHICH resident blocks are drawn.
            # Moving -> the coarse part of the hierarchy (spatially complete,
            # ~24x fewer points). Idle -> everything. No upload either way.
            # PART 16: hand the frame's camera to the selector. `on_frame` runs
            # at FRAME cadence, so this is where the screen-space selection is
            # allowed to run - never in the per-event camera callback (Part 26).
            self._note_camera(camera, viewport)
            # PHASE 4: when residency is provably unchanged (one integer
            # compare, PART 39) the frame only re-derives the screen-space
            # selection and re-pushes ranges if they changed. The O(resident)
            # work in `_flush_gpu` (key set, per-tile upload scan, draw
            # telemetry, overview diag) runs on a residency change and as a
            # bounded safety net, never per camera frame.
            _st = getattr(self, "lod_stats", None)
            if _st is not None:
                _st["frames"] += 1
            _why = self._fast_flush_reason()
            if _why is None:
                self._camera_fast_flush(interaction=not idle)
            else:
                _m = _pm()
                if _m is not None:
                    _ad = self.adapter
                    _u0 = (int(getattr(_ad, "arena_xyz_bytes", 0) or 0),
                           int(getattr(_ad, "arena_attr_bytes", 0) or 0),
                           int(getattr(_ad, "normal_uploads", 0) or 0))
                _info = self._flush_gpu(reason="camera_only", gen=self._gen,
                                        interaction=not idle)
                if _m is not None:
                    _g = int(getattr(self, "camera_generation", -1) or -1)
                    _m.hook_extra(_m.RecordType.SLOW_PATH, _g, a0=str(_why))
                    for _kind, _now, _was in (
                            (_m.UPLOAD_XYZ_BYTES,
                             int(getattr(_ad, "arena_xyz_bytes", 0) or 0),
                             _u0[0]),
                            (_m.UPLOAD_ATTR_BYTES,
                             int(getattr(_ad, "arena_attr_bytes", 0) or 0),
                             _u0[1]),
                            (_m.UPLOAD_NORMAL_COUNT,
                             int(getattr(_ad, "normal_uploads", 0) or 0),
                             _u0[2])):
                        if _now > _was:
                            _m.hook_extra(_m.RecordType.GPU_UPLOAD, _g,
                                          a0=_kind, a1=_now - _was)
                self._record_fast_flush_state(_info, _why)
            _t_flush = time.perf_counter()
            # PHASE 3: feed the observed frame cost to the budget controller.
            # Measured END-TO-END (Python + native submit + present), not just
            # the CPU portion, because the cost that matters is the one the user
            # feels.
            _fb = self._frame_budget()
            _fb.observe((_t_flush - _t0) * 1000.0,
                        mode=str(getattr(self, "display_mode", "neutral")))
            _fb.update()
            if _perf:
                self._perf_record_frame(
                    stage_camera_ms=(_t_cam - _t0) * 1000.0,
                    stage_flush_ms=(_t_flush - _t_cam) * 1000.0,
                    total_ms=(time.perf_counter() - _t0) * 1000.0,
                    idle=idle)
            # PART 46: the O(resident) telemetry sample is throttled, not
            # per-frame.
            if getattr(self, "_camera_fastpath", True):
                self._camera_only_telemetry(camera, viewport, self._gen)
            else:
                self._sample(camera=camera, viewport=viewport,
                             reason="camera_only", generation=self._gen,
                             camera_hot_ms=round(self.hot_ms_last, 3),
                             background_ms=round(self.bg_ms_last, 3))
            return {"camera_only": True, "uploads_this_frame": 0,
                    "draw_ranges": len(self.resident),
                    "position_reuploads": 0,
                    "resident_points": self._points_resident}
        if getattr(self, "full_resident_loading", False):
            # Still completing the single upload: keep pumping, skip LOD logic.
            self._full_resident_pump()
            self.hot_ms_last = (time.perf_counter() - _t0) * 1000.0
            self.bg_ms_last = self._collect_bg_ms()
            return {"full_resident_loading": True,
                    "uploads_this_frame": 0, "draw_ranges": len(self.resident),
                    "position_reuploads": 0,
                    "resident_points": self._points_resident}
        target = self._density(self).apply(self.lod, self._vp_w, self._vp_h,
                                           moving=not idle)
        # ROOT CAUSE (real GUI pan/zoom breaks): re-selection was gated on the
        # point BUDGET alone -
        #     if target != self._last_budget_target:
        # but `target` is derived from PIXEL COUNT and the MOVING/IDLE flag
        # only (ScreenDensityBudget.apply -> target_point_budget(px, moving)).
        # The camera CENTRE and the WORLD SCALE are not inputs to it.
        #
        # A pan changes the centre but neither the pixel count nor the moving
        # flag, so `target` was bit-identical, the guard was False, and
        # `self.lod.select()` was never called again. The frontier stayed
        # pinned to the region chosen at the last budget change, so every newly
        # revealed strip had no block at all - the tile-shaped black
        # rectangles. A 3x zoom failed identically, measured on the real
        # dataset: identical selected key set and identical hash.
        #
        # The guard must therefore include the camera itself. Selection is a
        # pure function of (viewport, camera, budget), so any change in any of
        # them requires a re-select. `note_camera_motion()` is called here as
        # well because a pan/zoom that never reaches VTK's InteractionEvent
        # still has to promote MOVING -> IDLE.
        cam_sig = sample.signature
        if cam_sig != getattr(self, "_last_cam_sig", None):
            self._last_cam_sig = cam_sig
            self.note_camera_motion()
        _frontier_on = self._streaming_frontier_enabled()
        # The frame's camera must reach the screen-space gate in STREAMING too.
        # Only the FULL_RESIDENT branch used to call this, so `_last_camera` stayed
        # unset, the gate answered "no_camera_yet" forever and every frame fell
        # back to the legacy "LOD0 only when idle" rule (holes + a frozen draw
        # set). Cheap: it only stores two references.
        if _frontier_on:
            self._note_camera(camera, viewport)
        _res_changed = bool(_frontier_on and _RES_GEN[0]
                            != getattr(self, "_select_res_gen", None))
        if target != getattr(self, "_last_budget_target", None) \
                or cam_sig != getattr(self, "_select_cam_sig", None) \
                or _res_changed:
            self._last_budget_target = target
            self._select_cam_sig = cam_sig
            self._select_res_gen = _RES_GEN[0]
            _fr = (self._frontier_specs(camera, viewport, not idle)
                   if _frontier_on else None)
            if _fr is not None:
                # PHASE 5: the DESIRED frontier comes from the compiled uniform
                # allocator over ALL rungs, plus a top-priority coarse request
                # for every visible node with nothing resident. It is re-derived
                # when the camera / budget moves AND when residency changes (the
                # coverage floor depends on what is resident).
                specs, _nvis, _selpts = _fr
                self._last_visible_nodes = _nvis
                self._last_specs = specs
                try:
                    self.density.last_candidate = int(_selpts)
                    self.density.candidate_points = int(_selpts)
                    self.density.note_selected(int(_selpts))
                except Exception:
                    pass
                camera_diag_selections = [
                    {"node_id": s.node_id, "lod": s.lod, "points": s.points}
                    for s in specs]
                sig = (self._vp_w, self._vp_h, self._camera_state)
                if sig != self._budget_report_printed_for:
                    self._budget_report_printed_for = sig
            else:
                visible_rows = self._visible_node_rows(camera, viewport)
                self._last_visible_nodes = len(visible_rows)
                selections = self.lod.select(self.idx, camera, visible_rows)
                self.density.note_candidates(selections)
                self.density.note_selected(sum(int(o["points"])
                                                for o in selections))
                # ROOT CAUSE (live refinement never activated): the dict->
                # TileSpec conversion was missing here, so `self._last_specs`
                # was assigned the stale PRE-BLOCK list (always empty).
                # candidate_points and selected_points looked correct because
                # they come from `selections`, which is why the bug was
                # invisible in the budget telemetry - but the request loop
                # received an empty frontier and submitted nothing, leaving the
                # LOD4 overview active forever.
                specs = [TileSpec(node_id=int(o["node_id"]), lod=int(o["lod"]),
                                  priority=float(o.get("priority",
                                                      o["centre_dist"])),
                                  points=int(o["points"]),
                                  px_err=float(o["px_err"]), row=int(o["row"]))
                         for o in selections]
                specs.sort(key=lambda s: -s.priority)
                self._last_specs = specs
                camera_diag_selections = [
                    {"node_id": int(o["node_id"]), "lod": int(o["lod"]),
                     "points": int(o["points"])}
                    for o in selections
                ]
                # Part 4/7: report once per stabilised state, not every tick.
                sig = (self._vp_w, self._vp_h, self._camera_state)
                if sig != self._budget_report_printed_for:
                    self._budget_report_printed_for = sig
        # Part 2/6: REQUEST the selected frontier. on_frame previously used
        # _specs_from_resident() - specs built from what is ALREADY resident -
        # so the idle path computed a selection it then discarded. With only the
        # overview resident, that is the overview forever: the live app sat at
        # 299,676 points / 0.18 ppp while the headless path (which drove
        # on_camera_changed) reached ~1.0 ppp. The request loop must run on the
        # idle path too, not only on camera motion.
        frontier = specs
        if not frontier:
            frontier = self._specs_from_resident()
        if camera_changed:
            self._diag_guard("camera_diag").run(
                self._diag_camera, "on_frame_camera_change",
                camera_diag_selections)
        requested = sum(s.points for s in frontier)
        self._request_missing(frontier)
        self._reconcile(frontier)
        gpu_info = self._flush_gpu(reason="frame", gen=self._gen,
                                   requested=requested, specs=frontier)
        if self._budget_report_printed_for is not None:
            # Report AFTER the flush so submitted points are real, not predicted.
            if self._budget_report_printed_for == (
                    self._vp_w, self._vp_h, self._camera_state):
                self._budget_report_printed_for = None
                if self._needs_normal():
                    self.print_lod_budget_report()
        self.hot_ms_last = (time.perf_counter() - _t0) * 1000.0
        self.timing["hot_ms"].append(self.hot_ms_last)
        self.bg_ms_last = self._collect_bg_ms()
        self._sample(camera=camera, viewport=viewport, reason="frame",
                     generation=self._gen,
                     camera_hot_ms=round(self.hot_ms_last, 3),
                     background_ms=round(self.bg_ms_last, 3), **gpu_info)
        return gpu_info
    def set_display_mode(self, mode: str) -> bool:
        """Prepare a requested mode while the active representation keeps drawing.

        Resource reads and uploads may complete here or on later frames. Only
        on_frame may publish the new native mode, after checking GPU coverage.
        """
        mode = canonical_mode(mode)
        caps = getattr(self, "capabilities", None)
        spec = MODES.get(mode)
        if caps is not None and spec is not None and not spec.available(caps):
            self._last_mode_substitution = (mode, self.display_mode)
            print(f"[DISPLAY MODE UNAVAILABLE] requested={mode}: "
                  f"{spec.requirement_reason(caps)}. Keeping {self.display_mode}.")
            return True
        swap = self._atomic_swap()
        # Returning to the active mode cancels any older asynchronous request.
        if mode == canonical_mode(swap.active.mode):
            swap.cancel("returned to active mode")
            self._pending_display_mode = None
            validator = getattr(self, "_render_validator", None)
            if validator is not None:
                validator.cancel_pending("returned to active mode")
            return True
        if swap.pending is not None and swap.pending.mode == mode:
            return True
        _MODE_GEN[0] += 1
        self.invalidate_camera_seal()
        self._pending_display_mode = mode
        validator = getattr(self, "_render_validator", None)
        if validator is not None:
            validator.dataset_generation = int(getattr(
                getattr(self, "gate", None), "dataset_generation",
                validator.dataset_generation))
            validator.set_layout_fingerprint(int(getattr(
                getattr(self, "idx", None), "layout_fingerprint",
                validator.layout_fingerprint)))
            snap = validator.request_pending(mode)
            generation = snap.mode_generation
        else:
            generation = _MODE_GEN[0]
        swap.request(mode, generation=generation)
        xyz_before = self._xyz_upload_count()
        nrm_before = int(getattr(self, "normal_upload_count", 0))
        t0 = time.perf_counter()
        self._drain_attr_done()
        self._backfill_missing_attrs()
        if requires_normal(mode):
            self._normal_reupload_needed = True
            if self._normal_source == "stored":
                self._backfill_resident_normals()
            else:
                self.start_normal_generation(reason="shaded_miss")
        self._flush_gpu(reason="display_mode")
        self.push_display_state(reason=f"prepare:{mode}")
        self._maybe_complete_pending_mode()
        self.last_switch_telemetry = switch_telemetry(
            normal_source=self._normal_source,
            xyz_upload_due_to_switch=int(self._xyz_upload_count() > xyz_before),
            normal_upload_due_to_switch=int(
                int(getattr(self, "normal_upload_count", 0)) > nrm_before),
            delaunay_calls=int(getattr(self, "delaunay_calls", 0)),
            surface_rebuilds=int(getattr(self, "surface_rebuilds", 0)),
            full_recolors=int(getattr(self, "full_recolors", 0)),
            cpu_ms=(time.perf_counter() - t0) * 1000.0,
            ptc_label=self.active_ptc_label())
        return True

    def _preparation_attrs(self) -> tuple:
        """Streams to prepare, without changing active tile admission rules."""
        active = tuple(self._required_attrs())
        want = getattr(self, "_pending_display_mode", None)
        if not want:
            return active
        extra = required_attributes(want)
        caps = getattr(self, "capabilities", None)
        if caps is not None:
            extra = caps.intersect(extra)
        return tuple(dict.fromkeys(active + tuple(extra)))

    def _preparing_normals(self) -> bool:
        return self._needs_normal() or requires_normal(
            getattr(self, "_pending_display_mode", None) or self.display_mode)

    def _xyz_upload_count(self) -> int:
        try:
            return int(self.adapter.stats().get("uploads", 0))
        except Exception:
            return 0
    # ------------------------------------------------------------------ #
    # PART 9/14 - prefetch + moving-vs-idle.                              #
    # ------------------------------------------------------------------ #
    def _measure_viewport(self, viewport=None, camera=None) -> tuple:
        """(width, height) in PIXELS.
        Part 16: the budget must scale with real pixels. The `viewport` tuple is
        (cx, cy, half_w, half_h) in WORLD UNITS - reading viewport[2]/[3] as
        pixels yields a nonsense budget (a ~500 m half-extent became a "499x499
        viewport"). The pixel size lives on the camera object
        (Camera2D.width_px/height_px), which is where VTK's widget size is
        already applied, so read it from there.
        """
        for obj in (camera, viewport):
            if obj is None:
                continue
            try:
                w = int(getattr(obj, "width_px", 0) or 0)
                h = int(getattr(obj, "height_px", 0) or 0)
                if w > 0 and h > 0:
                    return w, h
            except Exception:
                continue
        return int(getattr(self, "_vp_w", 0) or 0), int(
            getattr(self, "_vp_h", 0) or 0)
    def overlap_audit(self) -> dict:
        """Part 14 diagnostic: parent/child active coverage."""
        keys = [k for k, v in self.resident.items()
                if v.state == GPU_RESIDENT and v.complete]
        audit = audit_lod_overlap(
            self.idx, keys,
            complete_fn=lambda k: bool(self.resident[k].complete)
            if k in self.resident else False)
        self.last_overlap_audit = audit
        return audit
    def _coverage_holes(self, cell_m: float = 10.0):
        """Viewport cells with NO active representation, ignoring empty source.
        A pan/zoom transition must never leave a source-covered cell empty, and
        the verdict has to be a NUMBER rather than a screenshot. Two rules keep
        this honest:
        * a cell only counts as a hole when the SOURCE hierarchy says data
          exists there (no node covers it => the cell is genuinely empty, e.g.
          the edge of the surveyed block);
        * a cell counts as covered when ANY resident block's footprint contains
          it, because drawing a parent and its child together is legal during
          a handoff and must not be reported as a gap.
        Returns (holes, cells, sample) where `cells` counts only cells the
        source actually covers.
        """
        vp = getattr(self, "_viewport", None)
        if vp is None:
            return 0, 0, None
        try:
            cx, cy, hw, hh = (float(vp[0]), float(vp[1]),
                              float(vp[2]), float(vp[3]))
        except Exception:
            return 0, 0, None
        ds = self._overview_true_bounds() or self.dataset_bounds()
        x0 = max(cx - hw, float(ds[0][0]))
        x1 = min(cx + hw, float(ds[1][0]))
        y0 = max(cy - hh, float(ds[0][1]))
        y1 = min(cy + hh, float(ds[1][1]))
        if x1 <= x0 or y1 <= y0:
            return 0, 0, None
        # SOURCE footprint: every node box in the index, at any LOD.
        src = []
        try:
            n = self.idx.nodes
            if n is not None and n.size:
                nmin = np.asarray(n["bounds_min"], dtype=np.float64)
                nmax = np.asarray(n["bounds_max"], dtype=np.float64)
                for i in range(nmin.shape[0]):
                    src.append(((float(nmin[i, 0]), float(nmin[i, 1])),
                                (float(nmax[i, 0]), float(nmax[i, 1]))))
        except Exception:
            pass
        act = []
        for k, v in self.resident.items():
            if v.state != GPU_RESIDENT:
                continue
            bb = self._block_bounds_for_key(k)
            if bb is not None:
                act.append(bb)
        if not act:
            return 0, 0, None
        step = float(cell_m)
        nx = max(1, int(round((x1 - x0) / step)))
        ny = max(1, int(round((y1 - y0) / step)))
        # Bound the audit cost: a very wide view is sampled, never exhaustive.
        if nx * ny > 40000:
            step *= float(np.sqrt((nx * ny) / 40000.0))
            nx = max(1, int(round((x1 - x0) / step)))
            ny = max(1, int(round((y1 - y0) / step)))
        holes = 0
        total = 0
        sample = None
        for i in range(nx):
            sx = x0 + (i + 0.5) * step
            for j in range(ny):
                sy = y0 + (j + 0.5) * step
                if sx > x1 or sy > y1:
                    continue
                # Does the source claim this cell at all?
                have_source = any(b[0][0] <= sx <= b[1][0]
                                  and b[0][1] <= sy <= b[1][1] for b in src)
                if not have_source:
                    continue          # genuinely empty; not a streaming hole
                total += 1
                if any(b[0][0] <= sx <= b[1][0]
                       and b[0][1] <= sy <= b[1][1] for b in act):
                    continue
                holes += 1
                if sample is None:
                    sample = (round(sx, 2), round(sy, 2))
        return holes, total, sample
    def lod_budget_telemetry(self, detail: bool = True) -> dict:
        """Part 13 [SHADED LOD BUDGET] block for the stabilised camera state.
        `detail=False` is the PRODUCTION per-tick form: cheap counters only, no
        pairwise overlap audit and no coverage-grid scan (see below)."""
        keys = [k for k, v in self.resident.items() if v.state == GPU_RESIDENT]
        resident_points = int(sum(int(self.resident[k].count) for k in keys))
        # PART 10/16 - "submitted" must mean DRAWN, not RESIDENT. These are now
        # genuinely different sets, and reporting residency as the submission
        # count is exactly how a 27M-point cloud kept looking like it was only
        # drawing 300K. The ACTIVE set is what the renderer was told to draw.
        active_keys = [k for k in (getattr(self, "active_draw_keys", ()) or ())
                       if k in self.resident
                       and self.resident[k].state == GPU_RESIDENT]
        if not active_keys:
            active_keys = keys           # nothing selected yet: residency is the floor
        lod_dist = {}
        for k in active_keys:
            lod = int(self.resident[k].lod)
            lod_dist[lod] = lod_dist.get(lod, 0) + 1
        submitted = int(sum(int(self.resident[k].count) for k in active_keys))
        px = int(self._vp_w) * int(self._vp_h)
        ppp = points_per_pixel(submitted, self._vp_w, self._vp_h)
        target_ppp = (MOVING_TARGET_POINTS_PER_PIXEL
                      if self._camera_state == "MOVING"
                      else IDLE_TARGET_POINTS_PER_PIXEL)
        if detail:
            audit = self._diag_guard("overlap_audit").run(
                self.overlap_audit) or {}
            holes, cells, _sample = (self._diag_guard("coverage_holes").run(
                self._coverage_holes) or (0, 0, None))
        else:
            # PHASE 4B / PART 44. `overlap_audit` is a PAIRWISE audit over every
            # resident tile (all 1,339 pyramid tiles of the 27M set) and took
            # 34-44 SECONDS per call on the real cache; `_coverage_holes`
            # walks the viewport grid. The GUI tick called this on every 16 ms
            # tick. Production telemetry keeps the cheap counters and skips
            # the audits; they run on demand (detail=True) and under
            # NAKSHA_DEV_DIAGNOSTICS.
            audit = {"temporary_handoff_pairs": 0,
                     "illegal_persistent_overlap_pairs": 0}
            holes, cells = 0, 0
        return {
            "viewport": f"{int(self._vp_w)}x{int(self._vp_h)}",
            "pixels": px,
            "camera_state": self._camera_state,
            "target_ppp": target_ppp,
            "target_points": int(self._density(self).last_target),
            "candidate_points": int(self._density(self).last_candidate),
            "selected_points": int(self._density(self).last_selected),
            "submitted_points": submitted,
            "active_points": submitted,
            "active_blocks": len(active_keys),
            "resident_points": resident_points,
            "resident_blocks": len(keys),
            "screen_space_active": bool(getattr(
                self, "last_screen_space_diag", None)),
            "screen_space_selections": int(getattr(
                self, "screen_space_selections", 0) or 0),
            "screen_space_reason": str(getattr(
                self, "last_screen_space_reason", "not_requested")),
            "screen_space_fallbacks": int(getattr(
                self, "screen_space_fallbacks", 0) or 0),
            "screen_space_cached_hits": int(getattr(
                self, "screen_space_cached_hits", 0) or 0),
            "draw_range_pushes": int(getattr(self, "draw_range_pushes", 0) or 0),
            "actual_ppp": (round(ppp, 3) if ppp is not None else None),
            "visible_nodes": int(getattr(self, "_last_visible_nodes", 0) or 0),
            "selected_blocks": len(active_keys),
            "LOD0_blocks": int(lod_dist.get(0, 0)),
            "LOD1_blocks": int(lod_dist.get(1, 0)),
            "LOD2_blocks": int(lod_dist.get(2, 0)),
            "LOD3_blocks": int(lod_dist.get(3, 0)),
            "LOD4_blocks": int(lod_dist.get(4, 0)),
            "parent_child_overlap_regions":
                int(audit["temporary_handoff_pairs"]
                    + audit["illegal_persistent_overlap_pairs"]),
            "illegal_persistent_overlap_pairs":
                int(audit["illegal_persistent_overlap_pairs"]),
            "temporary_handoff_pairs": int(audit["temporary_handoff_pairs"]),
            "normal_source": str(getattr(self, "_normal_source", "screen")),
            # Canonical residency counter. It MUST live here as well as in
            # draw_telemetry(): the GUI tick reads it from THIS dict, and
            # omitting it raised KeyError('resident_normal_points') on every
            # real Qt frame tick, aborting on_frame() before refinement.
            "resident_normal_points": int(getattr(
                self, "normal_resident_points", 0) or 0),
            "normal_RAM_bytes": int(getattr(
                getattr(self, "normal_cache", None), "resident_bytes", 0) or 0),
            "normal_GPU_bytes": int(getattr(self, "normal_gpu_bytes", 0) or 0),
            "xyz_GPU_bytes": int(getattr(self, "gpu_bytes", 0) or 0)
            - int(getattr(self, "normal_gpu_bytes", 0) or 0),
            # ---- PAN / ZOOM ACCEPTANCE FIELDS -------------------------
            # These make the visual verdict numeric so the console, not a
            # screenshot, decides whether a camera move broke coverage.
            "camera_generation": int(getattr(self, "_gen", 0) or 0),
            "overview_active": (self._overview_key() in active_keys),
            "viewport_hole_cells": int(holes),
            "viewport_cells": int(cells),
            "detail": bool(detail),
        }
    def print_lod_budget_report(self, detail: bool = False) -> dict:
        # A mode promotion runs on the frame thread. Pairwise overlap and
        # coverage scans are explicit development checks, not activation work.
        t = self.lod_budget_telemetry(detail=detail)
        print("\n[SHADED LOD BUDGET]")
        for k in ("viewport", "pixels", "camera_state", "target_ppp",
                  "target_points", "candidate_points", "selected_points",
                  "submitted_points", "actual_ppp", "visible_nodes",
                  "selected_blocks", "LOD0_blocks", "LOD1_blocks",
                  "LOD2_blocks", "LOD3_blocks", "LOD4_blocks",
                  "parent_child_overlap_regions",
                  "illegal_persistent_overlap_pairs",
                  "temporary_handoff_pairs", "normal_source",
                  "normal_RAM_bytes", "normal_GPU_bytes", "xyz_GPU_bytes",
                  "camera_generation", "overview_active",
                  "viewport_hole_cells", "viewport_cells"):
            if not detail and k in (
                    "parent_child_overlap_regions", "illegal_persistent_overlap_pairs",
                    "temporary_handoff_pairs", "viewport_hole_cells", "viewport_cells"):
                continue
            print(f"{k}: {t[k]}")
        print("[/SHADED LOD BUDGET]\n")
        if not detail:
            print("[LOD OVERLAP] UNVERIFIED: detailed audit not requested")
            return t
        if int(t.get("viewport_hole_cells", 0) or 0) > 0:
            print(f"[COVERAGE WARNING] {t['viewport_hole_cells']} of "
                  f"{t['viewport_cells']} viewport cells have NO active "
                  f"representation - this renders as black rectangles.")
        a = self.last_overlap_audit
        print("[LOD OVERLAP]")
        print(f"active_blocks: {len(a.get('active_blocks', []))}")
        print(f"hierarchical_overlap_pairs: {a.get('hierarchical_overlap_pairs')}")
        print(f"temporary_handoff_pairs: {a.get('temporary_handoff_pairs')}")
        print(f"illegal_persistent_overlap_pairs: "
              f"{a.get('illegal_persistent_overlap_pairs')}")
        print("[/LOD OVERLAP]\n")
        return t
    def _ensure_source_oracle(self) -> SourceOccupancy:
        """PHASE 3. Build the trusted SOURCE/LOD0 oracle ONCE, lazily.
        The oracle is derived from the NKIDX node table, never from a per-frame
        source decode, so the parity proof costs one index pass at first use and
        then nothing.
        """
        oracle = getattr(self, "source_oracle", None)
        if oracle is not None:
            return oracle
        oracle = SourceOccupancy.from_index(self.idx, base_cell_m=HARD_CELL_M)
        self.source_oracle = oracle
        return oracle
    def _parity_bounds(self, sample=None):
        """Visible world bounds for the parity audit (FLAT x0,y0,x1,y1)."""
        if sample is not None and sample.bounds:
            return tuple(sample.bounds)
        vp = getattr(self, "_viewport", None)
        if vp is None:
            return None
        cx, cy, hw, hh = (float(vp[0]), float(vp[1]),
                          float(vp[2]), float(vp[3]))
        return (cx - hw, cy - hh, cx + hw, cy + hh)
    def _selected_and_active_boxes(self):
        """(selected_boxes, active_boxes) as XY boxes.
        ACTIVE deliberately includes EVERY GPU-resident block, coarse fallback
        parents included: it answers "what is drawing", not "what is finest".
        """
        selected_boxes, active_boxes, boxes = [], [], {}
        specs = getattr(self, "_last_specs", None) or []
        for s in specs:
            box = self._block_bounds_for_key((int(s.node_id), int(s.lod)))
            if box is not None:
                selected_boxes.append(box)
                boxes[(int(s.node_id), int(s.lod))] = box
        fallback = 0
        for k, v in (getattr(self, "resident", {}) or {}).items():
            if getattr(v, "state", None) != GPU_RESIDENT:
                continue
            box = self._block_bounds_for_key(k)
            if box is None:
                continue
            active_boxes.append(box)
            boxes[tuple(k)] = box
        # A fallback parent is an ACTIVE coarse block that still overlaps a finer
        # block: it is keeping a region covered while the replacement arrives.
        for k in boxes:
            if self._is_fallback_parent(k):
                fallback += 1
        return selected_boxes, active_boxes, boxes, fallback
    def _is_fallback_parent(self, key) -> bool:
        """A resident coarse block kept alive only to cover a finer gap."""
        for other in (getattr(self, "resident", {}) or {}):
            if tuple(other) == tuple(key):
                continue
            if int(other[1]) >= int(key[1]):
                continue
            a = self._block_bounds_for_key(tuple(other))
            b = self._block_bounds_for_key(tuple(key))
            if a is not None and b is not None and _boxes_overlap(a, b):
                return True
        return False
    def note_camera_motion(self) -> None:
        self._last_camera_change = time.perf_counter()
        self.last_moving = True
    def visible_parity(self, step: str = "IDLE", sample=None,
                       print_report: bool = True):
        """PHASE 4/5/6/16/18/19 - the three-way coverage proof for ONE step.
        Computes EXPECTED (source oracle clipped to the view), SELECTED (current
        desired TileSpecs) and ACTIVE (what is actually drawing, fallback parents
        included) at 10 m / 5 m / 1 m, then emits exactly one compact
        `[VISIBLE PARITY]` block plus the stage classification.
        Read-only and guarded: a parity failure must never abort streaming.
        """
        def _run():
            return self._visible_parity_uncached(step, sample)
        report = self._diag_guard("visible_parity").run(_run)
        if report is None:
            return None
        if print_report:
            print(report.render())
            self._diag_guard("parity_failures").run(self._print_parity_failures,
                                                    report)
        self._last_parity_report = report
        return report
    def _visible_parity_uncached(self, step, sample):
        oracle = self._ensure_source_oracle()
        bounds = self._parity_bounds(sample)
        self._frame_id = int(getattr(self, "_frame_id", 0) or 0) + 1
        center = sample.center if sample else (0.0, 0.0)
        world = sample.world_size if sample else (0.0, 0.0)
        resident = getattr(self, "resident", {}) or {}
        keys = [k for k, v in resident.items()
                if getattr(v, "state", None) == GPU_RESIDENT]
        selected_points = int(getattr(self._density(self), "last_selected", 0)
                              or 0)
        submitted = sum(int(getattr(resident[k], "count", 0) or 0) for k in keys)
        sel_boxes, act_boxes, _boxes, fallback = self._selected_and_active_boxes()
        levels = {}
        if bounds is not None:
            levels = three_way_parity(oracle, bounds, sel_boxes, act_boxes,
                                      cell_sizes=CELL_SIZES_M)
        return ParityReport(
            step=str(step),
            frame_id=self._frame_id,
            generation_id=int(getattr(self, "camera_generation", 0) or 0),
            camera_center=(float(center[0]), float(center[1])),
            camera_world_size=(float(world[0]), float(world[1])),
            levels=levels,
            selected_points=selected_points,
            submitted_points=submitted,
            lod_distribution=lod_distribution(keys),
            fallback_blocks=fallback,
            frame_errors=int(getattr(self, "frame_tick_error_count", 0) or 0),
            expected_known=bool(oracle.known and bounds is not None),
        )
    def _print_parity_failures(self, report):
        """PHASE 7/8/9 - name the failing stage, and only that stage.
        SELECTED missing -> LOD SELECTION. Investigate the selector first.
        ACTIVE missing   -> ACTIVATION / READINESS / HANDOFF.
        All three pass   -> the fault is downstream (GPU_DRAW_RANGE /
                           RASTERIZATION); splat size must not be touched.
        """
        hard = report.hard
        if hard is None:
            return
        gen = report.generation_id
        sig = getattr(self, "_last_camera_signature", None)
        specs = getattr(self, "_last_specs", None) or []
        selected_keys = [(int(s.node_id), int(s.lod)) for s in specs]
        if hard.selected_missing:
            print("[PARITY FAIL - SELECTION]")
            print(f"generation: {gen}")
            print(f"camera: {tuple(sig) if sig else None}")
            print(f"missing cell count: {len(hard.selected_missing)}")
            print(f"missing bounds: {hard.selected_missing_bounds}")
            print("nearest hierarchy nodes: "
                  f"{self._nearest_hierarchy_nodes(hard.selected_missing)}")
            print(f"selected keys: {selected_keys}")
            print("[/PARITY FAIL - SELECTION]")
        elif hard.active_missing:
            print("[PARITY FAIL - ACTIVATION]")
            print(f"generation: {gen}")
            print(f"missing cell count: {len(hard.active_missing)}")
            print(f"missing bounds: {hard.active_missing_bounds}")
            payload = activation_report(selected_keys, self,
                                        hard.active_missing_bounds)
            for row in payload["responsible_keys"]:
                print(f"selected key: {row['key']} "
                      f"runtime state: {row['state']} "
                      f"blocking attribute: {row['blocking_attribute']}")
            print("[/PARITY FAIL - ACTIVATION]")
        elif hard.selected_extra or hard.active_extra:
            print(f"[PARITY NOTE] coverage exceeds source truth "
                  f"(selected_extra={len(hard.selected_extra)} "
                  f"active_extra={len(hard.active_extra)}) - usually a coarse "
                  f"parent wider than its data, not a hole.")
    def _nearest_hierarchy_nodes(self, missing_cells, limit: int = 6):
        """PHASE 7. Which hierarchy nodes sit nearest the missing region."""
        box = region_bounds(missing_cells, HARD_CELL_M)
        if box is None:
            return []
        cx = (box[0][0] + box[1][0]) * 0.5
        cy = (box[0][1] + box[1][1]) * 0.5
        ranked = []
        try:
            n = self.idx.nodes
            bmin = np.asarray(n["bounds_min"], dtype=np.float64)
            bmax = np.asarray(n["bounds_max"], dtype=np.float64)
            for i in range(bmin.shape[0]):
                mx = (float(bmin[i, 0]) + float(bmax[i, 0])) * 0.5
                my = (float(bmin[i, 1]) + float(bmax[i, 1])) * 0.5
                ranked.append((float(np.hypot(mx - cx, my - cy)),
                               int(n["node_id"][i])))
            ranked.sort()
            return [nid for _d, nid in ranked[:limit]]
        except Exception:
            return []
    def camera_lod_parity_report(self, sample=None, print_report: bool = True):
        """PHASE 10/11 - prove the SELECTOR responded to the camera.
        An unchanged key set is not a failure on its own: the same keys can
        legitimately cover a new viewport. When a material zoom (>1.2x) leaves the
        selection bit-identical, this runs the explicit geometric verification
        (PHASE 11) and reports PASS-with-reason, or FAIL = SELECTOR STALE /
        WRONG CAMERA.
        """
        def _run():
            return self._camera_lod_parity_uncached(sample)
        result = self._diag_guard("camera_lod_parity").run(_run)
        if result is None:
            return None
        if print_report:
            print(render_camera_lod_parity(
                result, int(getattr(self, "camera_generation", 0) or 0)))
            print("\n[CAMERA LOD PARITY - SELECTED TILE INTERSECTION]")
            for name, value in result["tile_intersection"].items():
                print(f"{name}: {value}")
            print("[/CAMERA LOD PARITY - SELECTED TILE INTERSECTION]")
        return result
    def _camera_lod_parity_uncached(self, sample):
        oracle = self._ensure_source_oracle()
        bounds = self._parity_bounds(sample)
        _sel_boxes, _act, boxes, _fb = self._selected_and_active_boxes()
        specs = getattr(self, "_last_specs", None) or []
        new_keys = frozenset((int(s.node_id), int(s.lod)) for s in specs)
        old_keys = frozenset(getattr(self, "_last_selected_keys", frozenset()))
        old_sig = getattr(self, "_last_parity_signature", None)
        new_sig = sample.signature if sample else None
        result = camera_lod_parity(old_sig, new_sig, old_keys, new_keys)
        expected_cells = None
        if bounds is not None and oracle.known:
            expected_cells = oracle.expected_cells(
                clip_bounds(bounds, oracle.bounds), HARD_CELL_M)
        result["tile_intersection"] = tile_camera_intersection(
            sorted(new_keys), boxes, bounds or (0.0, 0.0, 0.0, 0.0),
            expected_cells_in_view=expected_cells,
            overview_key=self._overview_key())
        if result["status"] == "VERIFY":
            inter = result["tile_intersection"]
            # A tile outside the view is REPORTED, not failed on by itself: a
            # selection legitimately persists across a drag, and over-selection
            # is wasteful rather than a hole. The failure condition is strictly
            # a visible source-covered region with NO selected representation,
            # which is the signature of a selector using stale camera bounds.
            covered = inter["visible_cells_without_selected_representation"] == 0
            result["status"] = "PASS" if covered else "FAIL"
            result["geometrically_proven"] = bool(covered)
            result["reason"] = (
                "unchanged selection still covers the new viewport: "
                f"{inter['selected_inside_view']} selected tiles intersect the "
                "visible bounds, 0 visible cells unrepresented"
                + (f" ({len(inter['selected_outside_view'])} selected tiles now "
                   "fall outside the view but the view remains covered)"
                   if inter["selected_outside_view"] else "")
                if covered else
                "SELECTOR STALE / WRONG CAMERA: unchanged selection does not "
                f"cover the new viewport "
                f"({inter['visible_cells_without_selected_representation']} "
                f"visible source-covered cells unrepresented, bounds "
                f"{inter['largest_unrepresented_bounds']})")
        self._last_selected_keys = new_keys
        self._last_parity_signature = new_sig
        return result
    def async_parity(self) -> dict:
        """PHASE 12 ledger. `incorrectly_activated` MUST be 0."""
        return async_parity_report(self)
    def diagnostic_health(self) -> dict:
        """PHASE 0 proof: diagnostics must never have aborted streaming."""
        guards = getattr(self, "_diag_guards", None) or {}
        return {
            "diagnostic_error_count": int(
                getattr(self, "diagnostic_error_count", 0) or 0),
            "frame_tick_error_count": int(
                getattr(self, "frame_tick_error_count", 0) or 0),
            "invalid_camera_sample_count": int(
                getattr(self, "invalid_camera_sample_count", 0) or 0),
            "guards": {name: g.report() for name, g in guards.items()},
        }
    # ------------------------------------------------------------------ #
    # PART 5/6/7 - FULL_RESIDENT vs STREAMING_LOD.                           #
    # ------------------------------------------------------------------ #
    def select_render_mode(self, *, needs_normal: bool = False) -> dict:
        """PART 6 - choose by MEASURED MEMORY, never a point-count threshold.
        Prints the estimate so the decision is auditable rather than guessed
        (PART 11/24).
        """
        forced = force_render_mode()
        est = estimate_from_capabilities(
            int(getattr(self, "total_points", 0) or 0),
            getattr(self, "capabilities", None),
            needs_normal=bool(needs_normal),
            adapter=getattr(self, "adapter", None))
        mode = forced or est.mode
        self.gpu_estimate = est
        self.render_mode = mode
        print(f"[RENDER MODE] {est.describe()}")
        if forced:
            print(f"[RENDER MODE] FORCED by operator to {mode} "
                  f"(estimate would choose {est.mode})")
        elif mode == RENDER_FULL_RESIDENT:
            print(f"[RENDER MODE] FULL_RESIDENT: every source point is uploaded "
                  f"once. Pan/zoom/orbit/fit are camera-only - no tile "
                  f"swapping, no LOD replacement, no disk reads during "
                  f"interaction.")
        else:
            print(f"[RENDER MODE] STREAMING_LOD: dataset does not fit safely "
                  f"(needs {est.total_bytes:,} B, budget {est.safe_budget_bytes:,} B)")
        return est.as_dict()
    def _lod0_specs(self) -> list:
        """Every LOD0 TileSpec: the complete source point set, exactly once.
        `lod_point_count[:, 0]` sums to the source point count, so enumerating
        LOD0 blocks is the FULL_RESIDENT definition of "all points". The BLOCK
        table has no "row" field (its fields are block_id/node_id/lod/codec/...),
        so the row index is taken from the enumeration position.
        """
        specs = []
        try:
            blocks = self.idx.blocks
            sel = np.flatnonzero(blocks["lod"] == 0)
            for i in sel:
                row = blocks[int(i)]
                specs.append(TileSpec(node_id=int(row["node_id"]), lod=0,
                                      priority=0.0,
                                      points=int(row["point_count"]),
                                      px_err=0.0, row=int(i)))
        except Exception as exc:
            print(f"[FULL_RESIDENT] cannot enumerate LOD0 blocks: {exc!r}")
            return []
        specs.sort(key=lambda s: (-s.points, s.node_id))
        return specs
    def _resident_pyramid_enabled(self) -> bool:
        """PART 16/47 - keep the COARSE LODs resident alongside LOD0.
        FULL_RESIDENT used to upload LOD0 only, which made screen-space selection
        impossible: with no coarser resident representation to fall back to, the
        gate's never-blank floor is forced to LOD0 for every visible node. A FIT
        frame then submitted ~15M of 27M points at 9.3 points/pixel against a
        1.6M target.
        Preloading the coarse blocks was previously tried and DELIBERATELY
        REJECTED, and the reason is recorded in `_keep_overview_for_interaction`:
        re-laying the buffer MOVED the LOD0 offsets, which misaligned the
        index-bound class and intensity streams.
        STABLE SLOTS REMOVED THAT OBSTACLE. Adding a tile cannot move another
        tile's offset, and every attribute stream is written at the tile's own
        slot, so the pyramid is now safe. It costs ~7% more resident points and
        is what makes ACTIVE_DRAWN a screen-space set rather than a residency
        report.
        """
        if os.environ.get("NAKSHA_RESIDENT_PYRAMID", "").strip().lower() in (
                "0", "off", "false", "no"):
            return False
        return (getattr(self, "render_mode", RENDER_STREAMING_LOD)
                == RENDER_FULL_RESIDENT)
    def _coarse_specs(self) -> list:
        """Every non-LOD0 block: the coarse half of the resident pyramid."""
        specs = []
        try:
            blocks = self.idx.blocks
            sel = np.flatnonzero(blocks["lod"] > 0)
            for i in sel:
                row = blocks[int(i)]
                specs.append(TileSpec(node_id=int(row["node_id"]),
                                      lod=int(row["lod"]), priority=0.0,
                                      points=int(row["point_count"]),
                                      px_err=0.0, row=int(i)))
        except Exception as exc:                                    # noqa: BLE001
            print(f"[FULL_RESIDENT] cannot enumerate coarse blocks: {exc!r}")
            return []
        # Coarsest first: the CHEAPEST containment lands first, so the viewport
        # has a complete coarse floor long before the finest detail arrives.
        specs.sort(key=lambda s: (s.lod, s.node_id))
        return specs
    def load_full_resident(self, reason: str = "open") -> dict:
        """PART 7 - upload every source point ONCE, plus the coarse pyramid.
        Thereafter pan / zoom / orbit / fit touch the CAMERA ONLY: no LOD
        re-selection, no replacement groups, no retirement, no disk reads. The
        coarse LODs are resident too, so the FRAME can choose a coarse
        representation per node without touching disk or VRAM.
        """
        t0 = time.perf_counter()
        specs = self._lod0_specs()
        total = sum(s.points for s in specs)
        coarse = self._coarse_specs() if self._resident_pyramid_enabled() else []
        all_specs = specs + coarse
        self._last_specs = all_specs
        added = self._request_missing(all_specs)
        self.full_resident_specs = all_specs
        # `total` stays the SOURCE point count: it is the coverage contract, and
        # it must not be inflated by the pyramid's decimated copies.
        self.full_resident_total_points = total
        self.full_resident_pyramid_points = total + sum(s.points for s in coarse)
        self.full_resident_ready = False
        self.full_resident_loading = True
        self.full_resident_started = time.perf_counter()
        print(f"[FULL_RESIDENT] reason={reason} lod0_blocks={len(specs)} "
              f"points={total:,} coarse_blocks={len(coarse)} "
              f"pyramid_points={self.full_resident_pyramid_points:,} "
              f"submitted_now={added} pending={len(self._pending)}")
        return {"blocks": len(all_specs), "points": total,
                "coarse_blocks": len(coarse),
                "pyramid_points": self.full_resident_pyramid_points,
                "submitted": added,
                "ms": (time.perf_counter() - t0) * 1000.0}
    def _keep_overview_for_interaction(self) -> None:
        """Keep ONE cheap, complete-coverage block resident for gestures.
        PHASE 4/5/6 - the interaction draw set needs SOMETHING cheaper than
        26.9M points, and the overview block is the ideal candidate: it already
        exists in the cache, it covers the whole dataset (so it cannot leave a
        hole), and it is a single draw call of ~300K points - roughly a 90x
        reduction in submitted points AND draw calls during a gesture.
        WHY IT IS NOT RETIRED ANY MORE. `_retire_full_resident_overview` used to
        drop it as soon as LOD0 was complete, on the assumption that nothing
        would ever want it again. That assumption is what left every gesture
        submitting the entire dataset.
        DELIBERATELY NOT DONE (measured, and rejected):
        pre-loading all 945 coarse blocks into the position buffer. It reduced
        the interaction draw set correctly (1.6M points), but re-laying the
        buffer MOVED the LOD0 offsets, which silently misaligns the class and
        intensity arrays that are already bound by point index - and it made the
        idle frame draw MORE (28.5M points over 1338 calls), turning a 9.8 s
        zoom into 206 s. One extra resident block achieves most of the benefit
        with none of that risk.
        """
        ov = getattr(self.idx, "overview_block_id", -1) if self.idx is not None else -1
        if ov is None or int(ov) < 0:
            return
        key = (0, int(self.idx.header["lod_count"]) - 1)
        v = self.resident.get(key)
        if v is None:
            return
        # Resident state only. No re-layout, no re-upload, no offset change.
        v.state = GPU_RESIDENT
        self._interaction_overview_key = key
    def _retire_full_resident_overview(self) -> int:
        """Drop the coarse overview once the FULL detail set is resident.
        The overview is a DECIMATED COPY of the same data, so keeping it live
        after the LOD0 set has uploaded double-draws every region and makes a
        2,958,460-point dataset report ~3.26M resident points. It exists purely
        as the never-blank first paint, and that job is finished.
        """
        ov_key = self._overview_key()
        if ov_key is None:
            return 0
        tile = self.resident.get(ov_key)
        if tile is None or tile.state != GPU_RESIDENT:
            return 0
        # The overview (LOD = lod_count-1) is a DECIMATED COPY OF THE WHOLE
        # DATASET - its true footprint is the dataset extent, not node 0's small
        # box. Once every LOD0 block is resident it is therefore 100% redundant,
        # and keeping it double-draws every region: a 2,958,460-point dataset
        # would report ~3.26M resident points. It existed only as the never-blank
        # first paint, and that job is finished.
        tile.state = EVICTABLE
        self.gpu_bytes -= tile.bytes
        self.evictions += 1
        self._flush_gpu(reason="full_resident_retire_overview")
        return 1
    def _full_resident_pump(self) -> None:
        """Drive the one-time FULL_RESIDENT upload to completion.
        Called from the frame tick. Once complete, the resident set is the whole
        dataset and stays that way, so `_flush_gpu` never sees a structural
        change again and camera frames upload nothing.
        """
        if not getattr(self, "full_resident_loading", False):
            return
        self._reconcile(list(getattr(self, "_last_specs", None) or []))
        self._flush_gpu(reason="full_resident_load", gen=self._gen,
                        specs=getattr(self, "_last_specs", None))
        if self._pending:
            return
        keys = [k for k, v in self.resident.items()
                if v.state == GPU_RESIDENT]
        pts = sum(int(self.resident[k].count) for k in keys)
        target = int(getattr(self, "full_resident_total_points", 0) or 0)
        self.full_resident_ready = bool(target and pts >= target)
        if self.full_resident_ready:
            self.full_resident_loading = False
            retired = self._retire_full_resident_overview()
            keys = [k for k, v in self.resident.items()
                    if v.state == GPU_RESIDENT]
            pts = sum(int(self.resident[k].count) for k in keys)
            print(f"[FULL_RESIDENT] COMPLETE - {pts:,} points resident in "
                  f"{len(keys)} blocks "
                  f"({time.perf_counter() - self.full_resident_started:.1f}s, "
                  f"overview retired={retired}). Pan/zoom/orbit/fit are now "
                  f"camera-only: no LOD replacement, no retirement, no disk "
                  f"reads.")
            # PHASE 4/5: keep ONE cheap, complete-coverage block resident so a
            # gesture has something better than 26.9M points to draw.
            self._keep_overview_for_interaction()
            return
        # PART 13: progress is a heartbeat, not a per-frame print.
        now = time.perf_counter()
        if (now - float(getattr(self, "_full_resident_logged", 0.0) or 0.0)) \
                < PENDING_DIAG_HEARTBEAT_S:
            return
        self._full_resident_logged = now
        print(f"[FULL_RESIDENT] progress {pts:,} / {target:,} points "
              f"({len(keys)} blocks, pending={len(self._pending)}) "
              f"({now - self.full_resident_started:.1f}s)")
    def display_luts(self) -> dict:
        """Every colour table for the CURRENT palette/PTC state.
        PART 20: built the same way in FULL_RESIDENT and STREAMING_LOD, so the
        backend can never change the colour the user sees for a point.
        """
        app = getattr(self, "app", None)
        return build_luts(app)
    def active_lut(self) -> np.ndarray:
        """The 256x3 table for the CURRENT display mode."""
        luts = self.display_luts()
        key = canonical_mode(self.display_mode)
        return luts.get(key, luts["neutral"])
    def active_ptc_label(self) -> str:
        """B4: return the human-readable PTC label for the status bar.
        Returns the basename of the active .ptc file, or
        'Naksha Default' when the built-in palette is active. Never reads
        disk; the label is whatever the app currently holds.
        """
        app = getattr(self, "app", None)
        path = str(getattr(app, "current_ptc_path", None) or "")
        if path and os.path.isfile(path):
            return os.path.basename(path)
        return "Naksha Default"
    def apply_palette(self, palette=None, weight=None,
                      reason: str = "palette_update") -> dict:
        """PART 8/9/28 - update class colours / visibility as a LUT-ONLY change.
        A PTC load, a colour edit or a class-visibility toggle updates the small
        256x3 table. It must NOT re-upload XYZ, must NOT re-upload all points,
        must NOT rebuild the cache, must NOT reorder points and must NOT restart
        Vulkan - so this method deliberately touches only the table.
        Returns a report so the caller can PROVE the geometry was not touched.
        """
        _MODE_GEN[0] += 1          # PHASE 4B: PTC / class colour / visibility
        t0 = time.perf_counter()
        before_xyz = self._xyz_upload_count()
        before_pts = int(getattr(self, "_points_resident", 0) or 0)
        # Geometry-rebuilding counters, read (not assumed) so the report can
        # prove a PTC load touched none of them.
        before_normals = int(getattr(self, "normal_upload_count", 0) or 0)
        before_surf = int(getattr(self, "surface_rebuilds", 0) or 0)
        before_delaunay = int(getattr(self, "delaunay_calls", 0) or 0)
        before_normal_gen = int(getattr(self, "normal_generations", 0) or 0)
        luts = build_luts(getattr(self, "app", None), palette, weight)
        class_table = luts["class"]
        self._last_class_lut = class_table
        self._class_lut_generation = int(
            getattr(self, "_class_lut_generation", 0) or 0) + 1
        t_parse = (time.perf_counter() - t0) * 1000.0
        # Push the table to the renderer if it exposes the native LUT setter.
        pushed = False
        setter = getattr(self.adapter, "set_class_lut", None)
        if callable(setter):
            try:
                pushed = bool(setter(class_table))
            except Exception:
                pushed = False
        t_lut = (time.perf_counter() - t0) * 1000.0 - t_parse
        after_xyz = self._xyz_upload_count()
        # The visibility table travels with the palette (it is PTC metadata), so
        # it is pushed here too rather than waiting for a separate checkbox edit.
        vis_pushed = False
        try:
            vis_fn = getattr(self.adapter, "set_class_visibility", None)
            if vis_fn is not None:
                from gui.render_backend import build_class_visibility
                vis_pushed = bool(vis_fn(build_class_visibility(self.app)))
        except Exception:
            vis_pushed = False
        t_lut = (time.perf_counter() - t0) * 1000.0 - t_parse
        # PHASE 1: a PTC load must actually REDRAW. Pushing the table is only
        # half the job - without a render request the new colours sit in the
        # renderer's uniform buffer until some unrelated event happens to repaint,
        # which is why a PTC load could report success and change nothing on
        # screen. This asks for a redraw ONLY; it uploads no geometry.
        redraw = False
        try:
            fn = getattr(self, "request_redraw", None) or getattr(
                self.adapter, "request_render", None)
            if callable(fn):
                fn()
                redraw = True
        except Exception:
            redraw = False
        t_redraw = (time.perf_counter() - t0) * 1000.0 - t_parse - t_lut
        # [PTC PERFORMANCE] - the acceptance evidence for 1B. Every geometry
        # counter is READ here, so the numbers cannot flatter the implementation.
        xyz_uploads = int(after_xyz - before_xyz)
        geometry_rebuilds = (
            int(max(0, int(getattr(self, "normal_upload_count", 0) or 0)
                    - before_normals))
            + int(max(0, int(getattr(self, "surface_rebuilds", 0) or 0)
                      - before_surf))
            + int(max(0, int(getattr(self, "delaunay_calls", 0) or 0)
                      - before_delaunay))
            + int(max(0, int(getattr(self, "normal_generations", 0) or 0)
                      - before_normal_gen)))
        perf = {
            "parse_ms": round(t_parse, 3),
            "lut_upload_ms": round(t_lut, 3),
            "redraw_ms": round(t_redraw, 3),
            "xyz_uploads": xyz_uploads,
            "geometry_rebuilds": geometry_rebuilds,
        }
        if os.environ.get("NAKSHA_DEV_PTC_TRACE", "").strip() not in (
                "", "0", "false", "False"):
            print(f"[PTC PERFORMANCE] parse_ms={perf['parse_ms']:.3f} "
                  f"lut_upload_ms={perf['lut_upload_ms']:.3f} "
                  f"redraw_ms={perf['redraw_ms']:.3f} "
                  f"xyz_uploads={perf['xyz_uploads']} "
                  f"geometry_rebuilds={perf['geometry_rebuilds']}", flush=True)
        return {
            "reason": reason,
            "redraw_requested": redraw,
            "visibility_pushed": vis_pushed,
            "performance": perf,
            "lut_generation": self._class_lut_generation,
            "entries": int(class_table.shape[0]),
            "pushed_to_renderer": pushed,
            "xyz_uploads_before": before_xyz,
            "xyz_uploads_after": after_xyz,
            "xyz_reuploaded": bool(after_xyz > before_xyz),
            "resident_points_before": before_pts,
            "resident_points_after": int(getattr(self, "_points_resident", 0)
                                         or 0),
            "geometry_unchanged": bool(after_xyz == before_xyz),
        }
    def is_idle(self, now: Optional[float] = None) -> bool:
        t = time.perf_counter() if now is None else now
        return bool((t - self._last_camera_change) * 1000.0 >= IDLE_REFINE_MS)
    def request_normal_prefetch(self, reason: str = "initial_normal_prefetch"
                                ) -> int:
        """Queue normals for every currently-resident block. Never blocks.
        Called once the first point frame is up, so the common case is that the
        first Shaded click finds the coarse normal set already resident and
        uploads nothing (Part 22 CASE A).
        """
        bids = [int(self.resident[k].block_id) for k in self.resident
                if int(self.resident[k].block_id) >= 0]
        if not bids:
            return 0
        return self.prefetch.request(bids, self.gate.dataset_generation,
                                     priority=0.0)
    def drain_normal_prefetch(self, limit: int = 64) -> int:
        """Pop prefetch work, drop stale generations, load normals into RAM."""
        work = self.prefetch.take(limit, generation=self.gate.dataset_generation)
        loaded = 0
        for _gen, bid in work:
            blob, _reason = load_normal_block(
                self.normal_reader, self.normal_cache,
                self.gate.dataset_generation, int(bid), verify_crc=True)
            if blob is not None:
                loaded += 1
        return loaded
    # ===================== implementation ==================================== #
    def _visible_node_rows(self, camera, viewport):
        n = self.idx.nodes
        if n.size == 0:
            return []
        rows = np.arange(n.size)
        if viewport is None:
            return rows
        bmin = np.asarray(n["bounds_min"], dtype=np.float64)
        bmax = np.asarray(n["bounds_max"], dtype=np.float64)
        cx, cy, hw, hh = viewport
        m = ((bmax[:, 0] >= cx - hw) & (bmin[:, 0] <= cx + hw) &
             (bmax[:, 1] >= cy - hh) & (bmin[:, 1] <= cy + hh))
        return rows[m]
    def _submit_read(self, spec: TileSpec, requested_generation: int):
        """Submit one worker read and stamp the camera generation it was
        requested for (PHASE 12).
        The stamp must travel with the RESULT, not with the submission, because
        `_drain_done` decides staleness from the completion alone.
        """
        def _work():
            out = self._read_tile(spec)
            if isinstance(out, dict):
                out["requested_generation"] = int(requested_generation)
            return out
        return self._executor.submit(_work)
    def _request_missing(self, specs) -> int:
        """Submit async reads for every selected block not already held.
        Shared by the camera path and the idle path. Skips blocks already
        GPU-resident, already in RAM, or already queued, so repeated ticks cost
        nothing.
        """
        added = 0
        exec_ = getattr(self, "_executor", None)
        if exec_ is None:
            return 0
        gen = int(getattr(self, "camera_generation", 0) or 0)
        # PHASE 5 / PARTS 19-20: BOUNDED, RE-RANKED requests. `specs` arrives
        # sorted by priority (missing coverage first), re-derived from the LATEST
        # view every frame, so anything not yet submitted for an old view is
        # simply never submitted: the queue cannot grow with camera movement and
        # the newest view always outranks the old. In-flight reads finish into
        # the RAM cache (useful, never auto-activated).
        _cap = (self._max_inflight() if self._streaming_frontier_enabled()
                else None)
        for s in specs:
            key = (s.node_id, s.lod)
            v = self.resident.get(key)
            if v is not None and v.state == GPU_RESIDENT:
                v.last_used = time.perf_counter()
                self.ram_hits += 1
                continue
            if key in self._pending or key in self.ram_cache:
                continue
            if _cap is not None and len(self._pending) >= _cap:
                self.requests_deferred = int(
                    getattr(self, "requests_deferred", 0) or 0) + 1
                break
            self.ram_misses += 1
            fut = self._submit_read(s, gen)
            self._pending[key] = fut
            fut.add_done_callback(self._read_tile_done)
            added += 1
        self.queue_depth = len(self._pending)
        return added
    def _specs_from_resident(self):
        specs = []
        for k, v in self.resident.items():
            specs.append(TileSpec(node_id=v.node_id, lod=v.lod,
                                priority=v.priority, points=v.count,
                                px_err=0.0, row=0))
        specs.sort(key=lambda s: -s.priority)
        return specs
    def _read_tile(self, spec: TileSpec) -> dict:
        attrs = self._preparation_attrs()
        t0 = time.perf_counter()
        t = self.reader.read_tile(spec.node_id, spec.lod,
                                  only_attrs=nkpc_attrs(attrs),
                                  apply_edits=False,
                                  verify_crc=False, render_space=True)
        ms = (time.perf_counter() - t0) * 1000
        if t is None:
            return {"tile_key": (spec.node_id, spec.lod), "error": "no block",
                    "ms": ms, "pts": 0}
        packed = self._pack_block(t, (spec.node_id, spec.lod), ms, spec)
        # Part 3: if this mode needs NORMAL, load it in the SAME worker pass so
        # a tile reaches RAM already complete. Doing it here (not on the UI
        # thread) is what keeps the main thread free during a camera drag.
        if ATTR_NORMAL in attrs:
            self._attach_normal(packed, spec, attrs)
        return packed
    # ---- PART 5/6/7 rendering mode ---------------------------------------
    render_mode = RENDER_STREAMING_LOD
    # ---- PHASE 1: attribute-stream + display-state state ------------------
    # The GPU-resident coverage of each optional attribute stream, in POINTS.
    # Distinct from the per-tile `attrs` frozensets: this describes what has
    # actually reached the renderer for the CURRENT resident key set.
    _attr_class_points = 0
    _attr_intensity_points = 0
    # Signatures of what the renderer currently holds, so a warm mode switch
    # costs nothing and a residency change re-uploads exactly once.
    _attr_class_sig = None
    _attr_intensity_sig = None
    # Blocks re-read purely to widen their attribute coverage. Tracked so the
    # next flush knows an attribute upload is owed even though the resident KEY
    # set - and therefore the position buffer - has not changed.
    _attr_backfill_pending = frozenset()
    _last_display_state_push = {}
    _last_mode_readiness = {}
    mode_readiness_status = "READY"
    # The mode whose bytes are actually on the GPU right now. A requested mode
    # whose attribute stream is still loading is NOT published here: the
    # previous mode keeps rendering, so a partially-loaded mode can never black
    # the viewport (Part 11 never-black contract).
    _last_rendered_mode = None
    # ------------------------------------------------------------------ #
    # PHASE 1 - required-attribute awareness (1G) and mode readiness (1H).  #
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # PHASE 6D - NORMAL GENERATION AS A BACKGROUND SERVICE.               #
    # ------------------------------------------------------------------ #
    def attach_normal_service(self, service) -> None:
        """Install the background normal builder (optional; None = no service).
        Kept as an injectable dependency so the manager never constructs threads
        itself and a caller (or a test) can supply its own.
        """
        self.normal_service = service
        self.normal_build_status = None
        self._normal_delivery = queue.Queue(maxsize=16)
        self._normal_finished = queue.Queue()
        # The manager is the consumer of the result, so it owns these listeners:
        # progress reaches the UI, and a READY sidecar is handed back through the
        # STORED-normal path (6D.6) instead of waiting for a user action.
        try:
            service.set_callbacks(on_progress=self._on_normal_build_progress,
                                  on_finished=self._on_normal_build_finished)
        except Exception:
            pass
        setter = getattr(service, "set_block_callback", None)
        if setter is not None:
            setter(self._queue_normal_block)

    def _queue_normal_block(self, dataset, fingerprint, node_id, lod, block_id, payload):
        """Worker callback: bounded delivery, no manager/GPU mutation.

        If the consumer is paused, discard the oldest optional runtime copy.
        Canonical work remains persisted and the final cache can recover it.
        Shutdown and a caller waiting for a worker can never deadlock on UI work.
        """
        service = getattr(self, "normal_service", None)
        if service is None or service._cancel.is_set():
            return
        event = (dataset, fingerprint, (node_id, lod), block_id, payload)
        try:
            self._normal_delivery.put_nowait(event)
        except queue.Full:
            try:
                self._normal_delivery.get_nowait()
            except queue.Empty:
                pass
            self._normal_delivery.put_nowait(event)

    def _drain_normal_delivery(self):
        """Apply completed blocks on the frame thread, under current identity."""
        deliveries = getattr(self, "_normal_delivery", None)
        if deliveries is None:
            return
        expected_dataset = str(self.idx.path).removesuffix(".nakshaidx")
        for _ in range(16):
            try:
                dataset, fingerprint, key, bid, payload = deliveries.get_nowait()
            except queue.Empty:
                break
            if (os.path.normcase(os.path.abspath(dataset)) !=
                    os.path.normcase(os.path.abspath(expected_dataset))
                    or fingerprint != int(self.idx.layout_fingerprint)):
                continue
            block = self.idx.find_block(*key)
            if block is None or int(block["block_id"]) != bid:
                continue
            if payload.shape != (int(block["point_count"]), 2):
                continue
            self.normal_cache.put(self.gate.dataset_generation, bid, payload)
            tile = self.resident.get(key)
            if tile is not None and tile.count == len(payload):
                tile.normal = payload
                tile.normal_error = ""
                if tile.readiness is not None:
                    tile.readiness.normal_ready = True
                if getattr(tile, "gpu_attrs", None) is not None:
                    tile.gpu_attrs = frozenset(set(tile.gpu_attrs) - {"normal"})
                self._normal_sig = None
                self._normal_reupload_needed = True
                self.invalidate_camera_seal()
                _RES_GEN[0] += 1
                self._normal_source = "runtime"
                self.normal_service.timestamps.mark("t3_first_normal_runtime")
                from .normal_service import NormalBlockState
                self.normal_service.block_tracker.transition(bid, NormalBlockState.READY_RUNTIME)
        finished = getattr(self, "_normal_finished", None)
        if finished is not None:
            try:
                status = finished.get_nowait()
            except queue.Empty:
                return
            if os.path.normcase(os.path.abspath(status.dataset)) == os.path.normcase(
                    os.path.abspath(expected_dataset)):
                self._apply_normal_build_finished(status)
    @property
    def normal_build_progress(self) -> str:
        """Phase 6D.5: the non-blocking status line, or "" when idle."""
        st = getattr(self, "normal_build_status", None)
        if st is None:
            return ""
        try:
            return str(st.as_line())
        except Exception:
            return ""
    def _visible_lod0_nodes(self, limit: int = 64):
        """LOD0 node ids that cover what is ON SCREEN right now (6D.3).
        Derived from the resident tiles' bounds, so "visible-first" means the
        tiles the user is actually looking at - not a guess from a camera that
        the manager does not own.
        """
        try:
            from gui.naksha_cache.index import IndexReader  # noqa: F401
            blocks = self.idx.blocks
            lod0 = np.asarray(blocks["node_id"])[np.asarray(blocks["lod"]) == 0]
            node_tbl = getattr(self.idx, "nodes", None)
            if node_tbl is None or node_tbl.size == 0:
                return [int(n) for n in lod0[:limit]]
            ids = np.asarray(node_tbl["node_id"], dtype=np.int64)
            pos = {int(n): i for i, n in enumerate(ids)}
            nod = np.asarray(node_tbl["bounds_min"], dtype=np.float64)
            nox = np.asarray(node_tbl["bounds_max"], dtype=np.float64)
            keep = [int(n) for n in lod0 if int(n) in pos]
            if not keep:
                return []
            lpos = np.asarray([pos[n] for n in keep], dtype=np.int64)
            bmin, bmax = nod[lpos], nox[lpos]
            out, seen = [], set()
            for key, _v in list(self.resident.items()):
                j = pos.get(int(key[0]))
                if j is None:
                    continue
                lo, hi = nod[j], nox[j]
                hit = np.flatnonzero(np.all((bmin <= hi) & (bmax >= lo), axis=1))
                for k in hit:
                    nid = int(keep[int(k)])
                    if nid not in seen:
                        seen.add(nid)
                        out.append(nid)
                    if len(out) >= limit:
                        return out
            return out if out else keep[:limit]
        except Exception:
            return []
    def start_normal_generation(self, reason: str = "shading_miss") -> bool:
        """Start the background build for this dataset, visible-first (6D.2).
        Returns True when a build is running (now or already). Deliberately
        quiet when no service is installed: the MISS path must still work
        exactly as it did before, just without a producer.
        """
        service = getattr(self, "normal_service", None)
        if service is None:
            return False
        if getattr(self, "normal_source", "") == "stored":
            return False
        dataset = getattr(self, "dataset_path", None)
        if not dataset:
            try:
                dataset = str(self.idx.path).replace(".nakshaidx", "")
            except Exception:
                return False
        prio = self._visible_lod0_nodes()
        print(f"[NORMAL SERVICE] start reason={reason} visible_tiles={len(prio)} "
              f"dataset={os.path.basename(str(dataset))}", flush=True)
        service.start(str(dataset), priority_nodes=prio,
                      resume=bool(getattr(self, "_normal_resume", True)))
        return True
    def _on_normal_build_progress(self, status) -> None:
        self.normal_build_status = status
    def _on_normal_build_finished(self, status) -> None:
        """Worker completion queues publication; the frame thread owns resources."""
        self._normal_finished.put(status)

    def _apply_normal_build_finished(self, status) -> None:
        """Hand a finished sidecar back through the STORED-normal path (6D.6).
        No second format, no second activation route: the file the builder just
        published is opened with the same reader a pre-existing cache uses, the
        capability is refreshed, and the deferred mode completes on the next
        flush (6D.9).
        """
        self.normal_build_status = status
        if getattr(status, "state", "") != "READY":
            print(f"[NORMAL SERVICE] finished without a usable sidecar: "
                  f"{getattr(status, 'state', '?')} "
                  f"{getattr(status, 'reason', '')}", flush=True)
            return
        try:
            reader, report = open_normal_cache(
                str(status.dataset), verify_source=False,
                expected_source_points=self.source_point_count or None,
                expected_layout_fingerprint=int(
                    getattr(self.idx, "layout_fingerprint", 0) or 0))
        except Exception as exc:                                  # noqa: BLE001
            print(f"[NORMAL SERVICE] generated sidecar could not be opened: "
                  f"{exc!r}", flush=True)
            return
        if getattr(report, "status", "") != "HIT" or reader is None:
            print(f"[NORMAL SERVICE] generated sidecar rejected: "
                  f"{getattr(report, 'status', '')} "
                  f"{getattr(report, 'reason', '')}", flush=True)
            return
        self.attach_normal_cache(reader, report)
        self.normal_generations = int(
            getattr(self, "normal_generations", 0) or 0) + 1
        # The visible set now has a normal source: complete it immediately and
        # invalidate the frame so the change is visible on the NEXT frame, not
        # on the 0.5 s safety re-check.
        self._backfill_resident_normals()
        self._normal_reupload_needed = True
        _RES_GEN[0] += 1
        try:
            print(f"[NORMAL SERVICE] sidecar READY ({report.blocks} blocks, "
                  f"{report.stored_normals:,} normals) in "
                  f"{getattr(status, 'elapsed_s', 0.0):.1f}s -> re-checking "
                  f"Shading readiness", flush=True)
        except Exception:
            pass
        self._maybe_complete_pending_mode()
    def mode_readiness(self, mode=None) -> dict:
        """Canonical per-mode readiness - the ONLY answer to "can it draw?".
        The NEVER-BLACK rule (1H) is this function. A mode is READY, PENDING
        REQUIRED ATTRIBUTE, or UNSUPPORTED - it may never silently present an
        all-black viewport because a required buffer is absent. Callers and the
        acceptance harness read THIS rather than inferring from pixel darkness,
        which cannot tell "unsupported" from "still loading".
        """
        mode = canonical_mode(mode or getattr(self, "_pending_display_mode", None)
                              or self.display_mode)
        nominal = required_attributes(mode)
        # PHASE 6C: "normal" is a DERIVED stream (it lives in the .nakshanorm
        # sidecar), while ATTR_XYZ/CLASSIFICATION/INTENSITY are NKPC attribute
        # BITS. Keeping the two in one set mixes str and int, so sorted() used
        # to raise TypeError the moment a normal capability existed - and it
        # also modelled normal readiness as stored-attribute coverage, which it
        # is not. Derived streams are reported through derived_pending.
        derived_nominal = tuple(a for a in nominal if a in DERIVED_ATTR_NAMES)
        stored_nominal = tuple(a for a in nominal if a not in DERIVED_ATTR_NAMES)
        caps = getattr(self, "capabilities", None)
        available, dropped = set(stored_nominal), set()
        if caps is not None:
            available = set(caps.intersect(stored_nominal))
            dropped = set(caps.dropped(stored_nominal))
        if caps is not None:
            dropped |= set(caps.dropped(derived_nominal))
        keys = [k for k, v in self.resident.items() if v.state == GPU_RESIDENT]
        resident_points = int(sum(self.resident[k].count for k in keys)) if keys else 0
        resident_attrs = {ATTR_XYZ} if resident_points else set()
        if int(getattr(self, "_attr_class_points", 0) or 0) >= resident_points > 0:
            resident_attrs.add(ATTR_CLASSIFICATION)
        if int(getattr(self, "_attr_intensity_points", 0) or 0) >= resident_points > 0:
            resident_attrs.add(ATTR_INTENSITY)
        # 6C.2: normal coverage is MEASURED (normal_resident_points, written by
        # _flush_gpu from the bytes actually handed to the renderer) and is
        # reported as a derived stream. It never enters the int-bit set.
        if keys and all(getattr(self.resident[k], "rgb", None) is not None
                        for k in keys):
            resident_attrs.add(ATTR_RGB)
        derived_pending = []
        surface_ready = False
        if mode == "surface":
            expected = getattr(self, "_surface_request", None)
            surface_ready = bool(expected and getattr(self, "_surface_ready_token", None) == expected.token)
            if not surface_ready:
                derived_pending.append("surface")
        if ATTR_NORMAL in derived_nominal and (
                int(getattr(self, "normal_resident_points", 0) or 0)
                < resident_points > 0):
            derived_pending.append("normal")
        missing = sorted(available - resident_attrs)
        if dropped:
            status = "UNSUPPORTED"
        elif resident_points <= 0 or missing or derived_pending:
            status = "PENDING"
        else:
            status = "READY"
        report = {
            "mode": mode,
            "required_attrs": tuple(nominal),
            "source_available": bool(available),
            "cache_available": bool(available),
            "gpu_resident": bool(resident_points),
            "surface_ready": surface_ready,
            "derived_pending": tuple(derived_pending),
            "missing_attrs": tuple(missing),
            "dropped_attrs": tuple(sorted(dropped)),
            "resident_points": resident_points,
            "resident_attrs": tuple(sorted(resident_attrs)),
            "status": status,
            "ptc_label": self.active_ptc_label(),
        }
        self._last_mode_readiness = report
        self.mode_readiness_status = status
        return report
    def print_mode_readiness(self, reason: str = "") -> dict:
        """[MODE READINESS] - the 1H report, printed once per reason."""
        r = self.mode_readiness()
        print(f"[MODE READINESS] reason={reason or '-'} "
              f"mode={r['mode']} "
              f"required_attrs={r['required_attrs']} "
              f"available_attrs={r['required_attrs'] and tuple(a for a in r['required_attrs'] if a not in r['dropped_attrs'])} "
              f"resident_attrs={r['resident_attrs']} "
              f"missing_attrs={r['missing_attrs']} "
              f"resident_points={r['resident_points']:,} "
              f"derived_pending={r['derived_pending']} "
              f"status={r['status']}", flush=True)
        return r
    def _surface_tick(self):
        """Frame-thread orchestration only; all Surface I/O/CPU work is async."""
        wanted = (self.display_mode == "surface" or getattr(self, "_pending_display_mode", None) == "surface")
        service = getattr(self, "surface_service", None)
        if not wanted:
            if service is not None:
                service.cancel_request()
            self._surface_request = None
            self._surface_ready_token = None
            return
        from .surface_streaming import SurfaceBuildService, SurfaceRequest
        import json
        if service is None:
            dataset = str(self.idx.path).removesuffix(".nakshaidx")
            service = self.surface_service = SurfaceBuildService(dataset,
                max_ram=min(self.ram_budget // 8, 256 << 20))
        app = self.app
        bounds = self.dataset_bounds()
        ramp = json.dumps(getattr(app, "surface_color_ramp", None), sort_keys=True)
        style = (float(getattr(app, "last_shade_azimuth", 45)),
                 float(getattr(app, "last_shade_angle", 45)),
                 float(getattr(app, "shade_ambient", .22)), float(bounds[0][2]), float(bounds[1][2]), ramp)
        settings = (float(getattr(app, "surface_max_edge", 0) or 0), ())
        token = (self.gate.dataset_generation, int(self.idx.layout_fingerprint),
                 self.camera_generation, _MODE_GEN[0], style, settings)
        request = SurfaceRequest(token, tuple(self._viewport), (self._vp_w, self._vp_h), settings, style)
        self._surface_request = request
        service.request(request)
        result = service.consume()
        if result is None or result.request != request:
            return
        if result.error:
            self.surface_error = result.error
            print(f"[SURFACE BUILD FAILED] {result.error}", flush=True)
            return
        revision = int(getattr(self, "_surface_revision", 0)) + 1
        stage = getattr(self.adapter, "stage_surface", None)
        if stage is None or not stage(result, self.gate.dataset_generation, revision):
            self.surface_error = "native indexed Surface resource refused"
            return
        self._surface_revision = revision
        self._surface_ready_token = token
        self.surface_result = result
        service.timestamps["T3"] = time.perf_counter()
        if self.display_mode == "surface":
            activate = getattr(getattr(self.adapter, "be", None), "activate_pending_surface", None)
            if activate is not None:
                activate()
        self.invalidate_camera_seal()

    def _needed_attr_reads(self, keys) -> int:
        """Re-read resident blocks missing an attribute the CURRENT mode needs.
        PHASE 1 (1G). THE ROOT CAUSE THIS FIXES: `_read_tile` decodes only the
        CURRENT mode's streams, so a block read in Neutral holds no class and no
        intensity. Switching to Class therefore found nothing missing and
        uploaded nothing, and the renderer kept drawing whatever it already had -
        one flat colour.
        These are ordinary NKPC block reads (seek + decode of streams this block
        already stores). They never touch the LAS source, never rebuild
        .nakshaidx / .nakshapc, and never re-upload the position buffer.
        """
        required = set(nkpc_attrs(self._preparation_attrs()))
        required.discard(ATTR_XYZ)               # always present
        caps = getattr(self, "capabilities", None)
        if caps is not None:
            required &= set(caps.intersect(required))
        resident = getattr(self, "resident", None) or {}
        if not required or not keys or getattr(self, "_executor", None) is None:
            return 0
        added = 0
        queued = set(getattr(self, "_attr_backfill_pending", frozenset()) or ())
        gen = int(getattr(self, "camera_generation", 0) or 0)
        pending = getattr(self, "_pending", {}) or {}
        for k in keys:
            v = resident.get(k)
            if v is None or k in queued or k in pending:
                continue
            held = set(v.attrs or ())
            missing = required - held
            if not missing:
                continue
            spec = TileSpec(node_id=k[0], lod=k[1], priority=v.priority,
                            points=int(v.count), px_err=0.0, row=0)
            if self._submit_attr_read(spec, frozenset(missing), gen) is None:
                continue
            queued.add(k)
            added += 1
        if added:
            self._attr_backfill_pending = frozenset(queued)
            self._attr_dirty = set(getattr(self, "_attr_dirty", set())) | queued
        if os.environ.get("NAKSHA_DEV_ATTRIBUTE_TRACE", "").strip() not in (
                "", "0", "false", "False"):
            print(f"[ATTR BACKFILL] required={sorted(required)} "
                  f"queued_now={added} total_pending={len(queued)} "
                  f"mode={getattr(self, 'display_mode', None)} "
                  f"resident={len(resident)}", flush=True)
        return added
    def _backfill_missing_attrs(self) -> int:
        """Queue attribute-only reads for the blocks that need widening NOW.
        The entry point used by `set_display_mode` and `_flush_gpu`. It resolves
        the current GPU-resident key set itself, so a caller cannot hand
        `_needed_attr_reads` a stale or partial key list.
        Returns the number of reads QUEUED (not completed): the switch returns
        immediately, the cloud keeps rendering its current colours, and the read
        lands on a later `_flush_gpu` via `_drain_attr_done`. That is what keeps
        a mode switch from ever blocking on I/O or re-uploading geometry.
        """
        resident = getattr(self, "resident", None) or {}
        keys = [k for k, v in resident.items()
                if getattr(v, "state", None) == GPU_RESIDENT]
        return self._needed_attr_reads(keys)
    def _submit_attr_read(self, spec: TileSpec, missing: frozenset, gen: int):
        """One worker read that fetches ONLY the missing attribute streams."""
        def _work():
            try:
                t = self.reader.read_tile(
                    spec.node_id, spec.lod,
                    only_attrs=tuple(int(m) for m in missing),
                    apply_edits=False, verify_crc=False, render_space=True)
            except Exception as exc:                                # noqa: BLE001
                return {"tile_key": (spec.node_id, spec.lod),
                        "attr_error": repr(exc)}
            if t is None:
                return {"tile_key": (spec.node_id, spec.lod),
                        "attr_error": "block absent"}
            return {"tile_key": (spec.node_id, spec.lod), "attrs_only": True,
                    "requested_generation": int(gen),
                    "cls": t.get("classification"), "inten": t.get("intensity"),
                    "pts": int(t.get("point_count", 0) or 0)}
        try:
            fut = self._executor.submit(_work)
        except Exception:
            return None
        fut.add_done_callback(self._attr_read_done)
        return fut
    def _attr_read_done(self, fut):
        """Worker-completion callback.
        THIS RUNS ON AN EXECUTOR THREAD, so an exception raised here is swallowed
        by the futures machinery and the attribute simply never arrives - the
        block stays un-classed and the mode silently renders Neutral forever.
        The original error path re-put into `self._attr_done`, so a manager
        missing that queue raised INSIDE this handler and lost the result twice.
        The queue is therefore resolved defensively, and a genuinely
        undeliverable result is reported rather than dropped in silence.
        """
        try:
            result = fut.result()
        except Exception as exc:                                    # noqa: BLE001
            result = {"tile_key": None, "attr_error": repr(exc)}
        q = getattr(self, "_attr_done", None)
        if q is None:
            # No queue means this manager was never set up for backfill. Say so
            # rather than losing a read that already cost a disk hit.
            print(f"[ATTR BACKFILL] dropped result - manager has no attribute "
                  f"queue: {result.get('attr_error') or result.get('tile_key')}",
                  flush=True)
            return
        try:
            q.put(result)
        except Exception as exc:                                    # noqa: BLE001
            print(f"[ATTR BACKFILL] result undeliverable: {exc!r}", flush=True)
    def _drain_attr_done(self) -> int:
        """Fold completed attribute-only reads into the resident tiles.
        DEFENSIVE ON `_attr_done`. Several callers (and several tests) build the
        manager via `__new__` and set only the fields they exercise, so this must
        not assume the full `__init__` ran. A manager with no attribute queue was
        never asked to backfill anything, so there is provably nothing to fold in
        and returning 0 is correct - not a silent skip.
        """
        q = getattr(self, "_attr_done", None)
        if q is None:
            return 0
        applied = 0
        while True:
            try:
                r = q.get_nowait()
            except queue.Empty:
                break
            key = r.get("tile_key")
            if not key:
                continue
            self._attr_backfill_pending = frozenset(
                k for k in self._attr_backfill_pending if k != key)
            if r.get("attr_error"):
                self._set_attr_error(key, str(r["attr_error"]))
                continue
            v = self.resident.get(key)
            pts = int(r.get("pts", 0) or 0)
            # A short read would attribute-shift every point past its end, so it
            # is refused rather than concatenated.
            if v is None:
                continue
            if pts <= 0 or pts != int(v.count):
                self._set_attr_error(
                    key, f"attribute read length {pts} != resident count {int(v.count)}")
                continue
            gained = set()
            for name, attr, cast in (("cls", ATTR_CLASSIFICATION, None),
                                     ("inten", ATTR_INTENSITY, np.float32)):
                raw = r.get(name)
                if raw is None:
                    continue
                arr = np.asarray(raw).reshape(-1)
                if arr.size != int(v.count):
                    continue
                setattr(v, name, arr.astype(cast, copy=False) if cast else arr)
                gained.add(attr)
            if not gained:
                self._set_attr_error(key, "attribute read returned no usable stream")
                continue
            v.attrs = frozenset(set(v.attrs or ()) | gained)
            self._attr_dirty = set(getattr(self, "_attr_dirty", set())) | {key}
            applied += 1
        return applied
    def _set_attr_error(self, key, message: str) -> None:
        """Record why a block could not be widened, without assuming __init__ ran.
        A missing attribute must NEVER become silent black output (Part 5): the
        reason is kept so mode readiness can report PENDING/UNSUPPORTED with a
        cause instead of leaving the previous mode on screen unexplained.
        """
        errors = getattr(self, "_attr_errors", None)
        if errors is None:
            errors = {}
            try:
                self._attr_errors = errors
            except Exception:
                return
        errors[key] = str(message)
    def _concat_attr_in_key_order(self, keys, name, expected):
        """Concatenate one attribute stream across `keys`, in the SAME order the
        position buffer used. Returns None when any block lacks it.
        Ordering is not cosmetic: attribute streams are bound by point INDEX, so
        an order differing from the one `_flush_gpu` used for XYZ would colour
        every point with another point's attribute. All-or-nothing is the only
        safe answer - a partial concatenation silently mis-aligns.
        """
        parts = []
        for k in keys:
            v = self.resident.get(k)
            if v is None:
                return None
            arr = getattr(v, name, None)
            if arr is None:
                return None
            arr = np.asarray(arr).reshape(-1)
            if arr.size != int(v.count):
                return None
            parts.append(arr)
        if not parts:
            return None
        out = np.concatenate(parts)
        if expected and int(out.size) != int(expected):
            return None
        return out
    def _upload_missing_attributes(self, keys, new_sig) -> dict:
        """Push whichever attribute streams the current mode needs and the GPU
        does not yet hold. NEVER re-uploads positions.
        This is the whole of PHASE 1's "0 XYZ uploads during mode switches": the
        position buffer is untouched and only the extra vertex stream moves.
        """
        required = set(nkpc_attrs(self._preparation_attrs()))
        caps = getattr(self, "capabilities", None)
        if caps is not None:
            required &= set(caps.intersect(required))
        expected = int(getattr(self, "_points_resident", 0) or 0)
        if expected <= 0:
            expected = sum(int(self.resident[k].count) for k in keys)
        out = {"classification": 0, "intensity": 0, "bytes": 0, "pushed": False,
               "reason": ""}
        if expected <= 0:
            out["reason"] = "no resident points"
            return out
        cls_arr = int_arr = None
        if ATTR_CLASSIFICATION in required and self._attr_class_sig != new_sig:
            cls_arr = self._concat_attr_in_key_order(keys, "cls", expected)
            if cls_arr is None:
                out["reason"] = "classification stream incomplete"
        if ATTR_INTENSITY in required and self._attr_intensity_sig != new_sig:
            int_arr = self._concat_attr_in_key_order(keys, "inten", expected)
            if int_arr is None:
                out["reason"] = (out["reason"] + "; " if out["reason"] else "") \
                    + "intensity stream incomplete"
        if cls_arr is None and int_arr is None:
            if os.environ.get("NAKSHA_DEV_ATTRIBUTE_TRACE", "").strip() not in (
                    "", "0", "false", "False"):
                have_cls = sum(1 for k in keys
                               if getattr(self.resident.get(k), "cls", None) is not None)
                have_int = sum(1 for k in keys
                               if getattr(self.resident.get(k), "inten", None) is not None)
                print(f"[ATTR UPLOAD] nothing sent: "
                      f"class_arr={cls_arr is not None} "
                      f"intensity_arr={int_arr is not None} "
                      f"required={sorted(required)} "
                      f"blocks_with_class={have_cls}/{len(keys)} "
                      f"blocks_with_intensity={have_int}/{len(keys)} "
                      f"reason={out['reason']}", flush=True)
            return out
        fn = getattr(self.adapter, "upload_resident_attributes", None)
        if fn is None:
            out["reason"] = "adapter has no upload_resident_attributes"
            return out
        try:
            ok = bool(fn(classification=cls_arr, intensity=int_arr))
        except Exception as exc:                                    # noqa: BLE001
            out["reason"] = f"upload raised: {exc!r}"
            return out
        if not ok:
            out["reason"] = out["reason"] or "native refused the attribute upload"
            return out
        if cls_arr is not None:
            cls_arr = np.asarray(cls_arr)
            out["classification"] = int(cls_arr.size)
            out["bytes"] += int(cls_arr.nbytes)
            self._attr_class_sig = new_sig
            self._attr_class_points = int(cls_arr.size)
        if int_arr is not None:
            int_arr = np.asarray(int_arr)
            out["intensity"] = int(int_arr.size)
            out["bytes"] += int(int_arr.nbytes)
            self._attr_intensity_sig = new_sig
            self._attr_intensity_points = int(int_arr.size)
            # The stretch range is derived from the RESIDENT intensity values. If
            # the display state was pushed BEFORE this stream arrived, the native
            # side still holds the (0, 1) fallback - every value clamps to white.
            # Re-push now that the data the range describes is on the GPU.
            try:
                self._intensity_span_cache = None
                self.push_display_state(reason="intensity_stream_resident")
            except Exception:                           # noqa: BLE001
                pass
        out["pushed"] = True
        self._attr_dirty = set()
        return out
    def _elevation_range(self):
        """(lo, hi, gamma) for the elevation ramp, from DATASET bounds.
        PHASE 1 (1F). Elevation derives colour from PositionBuffer.z, so no
        second geometry stream is involved - only this range plus the ramp LUT.
        While streaming `app.data` is empty, so the range cannot come from
        there; the NKIDX header bounds are authoritative and already resident.
        This is the fix for the flat-elevation symptom: the native default is
        lo=0/hi=1, and against a real Z span of 0..55.31 every point clamped to
        t=1 and therefore to elevationLut[255] - one flat colour.
        """
        gamma = 1.0
        app = getattr(self, "app", None)
        if app is not None:
            try:
                gamma = float(getattr(app, "elevation_gamma", 1.0) or 1.0)
            except Exception:
                gamma = 1.0
        bounds = self.dataset_bounds()
        if bounds is None:
            return (0.0, 1.0, gamma)
        bmin, bmax = bounds
        lo, hi = float(bmin[2]), float(bmax[2])
        if not (hi > lo):
            hi = lo + 1.0          # never hand the shader a zero span
        return (lo, hi, gamma)
    def _depth_range(self):
        """(lo, hi, gamma) for the DEPTH ramp, from the DATASET extent.
        Depth is distance from the eye, so unlike elevation (world Z) and
        intensity (a stored per-point value) this range is inherently
        VIEW-dependent. The shader computes the true eye-relative distance; this
        only decides which window of it the ramp spans.
        Using the dataset's diagonal as the span keeps the normalisation stable
        and meaningful in BOTH projections: an orthographic plan view and a
        perspective orbit see the same cloud at similar eye distances, so the
        ramp does not need re-deriving on every orbit step - and a ramp that
        tracked the eye exactly would flatten the moment the camera moved.
        The native side clamps a zero span, and the legacy default gamma is 1.0
        (gui/depth_settings_dialog.py: depth_gamma=1.0, 'grayscale').
        """
        gamma = 1.0
        app = getattr(self, "app", None)
        if app is not None:
            try:
                gamma = float(getattr(app, "depth_gamma", 1.0) or 1.0)
            except Exception:
                gamma = 1.0
        bounds = self.dataset_bounds()
        if bounds is None:
            return (0.0, 1.0, gamma)
        bmin, bmax = bounds
        span = float(np.linalg.norm(np.asarray(bmax, dtype=np.float64)
                                    - np.asarray(bmin, dtype=np.float64)))
        if not (span > 0.0) or not np.isfinite(span):
            return (0.0, 1.0, gamma)
        # The eye is never inside the data, so distances run over [0, span];
        # clamping at the span means an extreme outlier cannot wash the ramp out.
        return (0.0, span, gamma)
    def _intensity_range(self):
        """(lo, hi, contrast, gamma) from the RESIDENT intensity stream.
        PHASE 1 (1E). NEVER assumes 0..255: 123.las carries uint16 intensity up
        to 59309, so a hard-coded 0..255 maps almost every return to the top of
        the ramp and flattens the mode to one bright grey. Zeros are ignored,
        matching legacy `_nakshatech_auto_normalize(..., ignore_zero=True)`,
        because a zero-intensity return carries no signal to display.
        """
        app = getattr(self, "app", None)
        contrast, gamma = 1.0, 1.0
        if app is not None:
            try:
                contrast = float(getattr(app, "intensity_contrast", 1.0) or 1.0)
                gamma = float(getattr(app, "intensity_gamma", 1.0) or 1.0)
            except Exception:
                contrast, gamma = 1.0, 1.0
        lo, hi = self._resident_intensity_span()
        if lo is None:
            return (0.0, 1.0, contrast, gamma)
        if not (hi > lo):
            hi = lo + 1.0          # a constant-intensity dataset must not divide by 0
        return (float(lo), float(hi), contrast, gamma)
    def intensity_readiness(self, nonzero_frame_pixels=None) -> dict:
        """[INTENSITY READY] - the honest answer to "can Intensity draw?".
        Intensity is the mode most likely to fail SILENTLY: a missing or
        zero-filled stream still renders, it just renders BLACK, and black is
        indistinguishable from "the cloud is out of view". This report names
        every stage of the chain separately - source, cache, GPU, dtype, range,
        coverage - so a black Intensity viewport can be diagnosed without
        guessing.
        `nonzero_frame_pixels` is optional and supplied by the acceptance
        harness from an actual pixel readback; without it the report says so
        rather than claiming a frame was verified.
        """
        required = set(nkpc_attrs(self._required_attrs()))
        caps = getattr(self, "capabilities", None)
        source_present = bool(
            caps is None or ATTR_INTENSITY in set(caps.intersect({ATTR_INTENSITY})))
        keys = [k for k, v in self.resident.items() if v.state == GPU_RESIDENT]
        resident_points = int(sum(self.resident[k].count for k in keys)) if keys else 0
        held, dtypes, nonzero, mismatches = 0, set(), 0, {}
        for k in keys:
            v = self.resident[k]
            arr = getattr(v, "inten", None)
            if arr is None:
                continue
            a = np.asarray(arr).reshape(-1)
            held += int(a.size)
            dtypes.add(str(a.dtype))
            nonzero += int(np.count_nonzero(a))
            if int(a.size) != int(v.count):
                mismatches[k] = int(a.size) - int(v.count)
        gpu_present = (resident_points > 0
                       and int(getattr(self, "_attr_intensity_points", 0) or 0)
                       >= resident_points)
        lo, hi = self._resident_intensity_span()
        app = getattr(self, "app", None)
        try:
            gamma = float(getattr(app, "intensity_gamma", 1.0) or 1.0)
        except Exception:
            gamma = 1.0
        report = {
            "source_present": source_present,
            "cache_present": bool(held),
            "gpu_present": bool(gpu_present),
            "dtype": ",".join(sorted(dtypes)) or "-",
            "min": lo,
            "max": hi,
            "gamma": gamma,
            "active_blocks": len(keys),
            "resident_points": resident_points,
            "attribute_points": held,
            "nonzero_values": nonzero,
            "attribute_mismatches": len(mismatches),
            "mode_requires_intensity": ATTR_INTENSITY in required,
            "nonzero_frame_pixels": nonzero_frame_pixels,
            "frame_verified": nonzero_frame_pixels is not None,
        }
        if os.environ.get("NAKSHA_DEV_INTENSITY_TRACE", "").strip() not in (
                "", "0", "false", "False"):
            print(f"[INTENSITY READY] source_present={report['source_present']} "
                  f"cache_present={report['cache_present']} "
                  f"gpu_present={report['gpu_present']} "
                  f"dtype={report['dtype']} min={report['min']} "
                  f"max={report['max']} gamma={report['gamma']} "
                  f"active_blocks={report['active_blocks']} "
                  f"attribute_mismatches={report['attribute_mismatches']} "
                  f"nonzero_frame_pixels={report['nonzero_frame_pixels']}",
                  flush=True)
        return report
    def _resident_intensity_span(self):
        """(lo, hi) of the intensity display stretch - the VTK contract.
        Legacy VTK (`pointcloud_display._nakshatech_intensity_rgb`) stretches the
        NON-ZERO intensity between its `intensity_clip_low/high` PERCENTILES
        (0.5 / 99.8), not between min and max. Using min/max let a few hot
        returns set the top of the ramp, so the bulk of the cloud rendered ~45%
        darker than VTK (live 123.las: median grey 84 vs 138) and, with gamma 3,
        crushed it further.
        Percentiles are taken over a bounded, EVENLY STRIDED sample of every
        resident block (not a prefix of each block, which biased the sample),
        and cached per resident set + clip settings, so a mode switch stays O(1)
        after the first computation.
        """
        app = getattr(self, "app", None)
        try:
            clip_lo = float(getattr(app, "intensity_clip_low", 0.5))
            clip_hi = float(getattr(app, "intensity_clip_high", 99.8))
        except Exception:
            clip_lo, clip_hi = 0.5, 99.8
        keys = [k for k, v in self.resident.items() if v.state == GPU_RESIDENT]
        sig = (frozenset(keys), clip_lo, clip_hi)
        cached = getattr(self, "_intensity_span_cache", None)
        if cached is not None and cached[0] == sig:
            return cached[1]
        arrays = []
        total = 0
        for k in keys:
            arr = getattr(self.resident[k], "inten", None)
            if arr is None:
                continue
            a = np.asarray(arr).reshape(-1)
            arrays.append(a)
            total += int(a.size)
        if total == 0:
            return (None, None)
        cap = 4_000_000                              # exact up to 4M non-sampled points
        stride = max(1, -(-total // cap))            # ceil: even sampling across ALL blocks
        parts = []
        for a in arrays:
            s = np.asarray(a[::stride], dtype=np.float64)
            s = s[s > 0]
            if s.size:
                parts.append(s)
        if not parts:
            return (None, None)
        work = np.concatenate(parts)
        lo = float(np.percentile(work, clip_lo))
        hi = float(np.percentile(work, clip_hi))
        if hi <= lo:                                   # same fallback as the VTK helper
            lo, hi = float(work.min()), float(work.max())
        result = (lo, hi)
        self._intensity_span_cache = (sig, result)
        return result
    def _ensure_default_class_palette(self) -> int:
        """Seed `app.class_palette` from the RESIDENT class codes.
        WHY THIS IS NEEDED. In STREAMING mode nothing ever populates
        `app.class_palette` - `gui/naksha_cache/app_streaming.py` does not touch
        it, and the legacy builder `pointcloud_display.compute_colors` returns
        early when `app.data` is empty, which it always is while streaming. The
        LUT therefore came out FLAT: 256 identical grey entries, so Class mode
        rendered exactly like Neutral no matter how perfectly the attribute
        stream was aligned. Measured live: `canonical_class_lut_unique: 1`.
        The default here is deliberately the application's OWN default, taken
        from `gui/pointcloud_display.py`:
            app.class_palette = {int(code): {"color": (160, 160, 160),
                                             "show": True} for code in classes}
        Same shape, same colour, same `show` flag - this is NOT a second palette
        system. It exists so that class VISIBILITY and a subsequent PTC load have
        real entries to act on; supplying the actual class colours remains the
        PTC's job, exactly as in the legacy path.
        Returns the number of classes seeded.
        """
        app = getattr(self, "app", None)
        if app is None:
            return 0
        existing = getattr(app, "class_palette", None)
        if existing:
            return len(existing)
        codes = set()
        budget = 400_000
        used = 0
        for k, v in self.resident.items():
            arr = getattr(v, "cls", None)
            if arr is None:
                continue
            a = np.asarray(arr).reshape(-1)
            if used and used + a.size > budget:
                a = a[: max(1, budget - used)]
            used += int(a.size)
            codes.update(int(c) for c in np.unique(a))
            if used >= budget:
                break
        if not codes:
            return 0
        try:
            app.class_palette = {
                int(code): {"color": (160, 160, 160), "show": True}
                for code in sorted(codes)}
        except Exception:
            return 0
        print(f"[CLASS PALETTE] seeded {len(app.class_palette)} classes from "
              f"resident stream (application default colour; PTC supplies the "
              f"real palette)", flush=True)
        return len(app.class_palette)
    def push_display_state(self, reason: str = "display_state") -> dict:
        """PHASE 1 (1C): push the canonical colour tables and ranges to Vulkan.
        THE MISSING LINK. The legacy route pushed these through
        `render_backend.sync_point_shading`, which returns immediately when
        `app.data` is empty - i.e. it NEVER ran while streaming. The Vulkan
        renderer therefore kept its BUILT-IN DEFAULT tables: a fixed class
        palette, and an elevation range of 0..1 against a cloud whose real Z span
        is 0..55.31. That is precisely why Class showed one flat colour and
        Elevation collapsed onto a single ramp entry.
        Uniform-only by contract, so it is safe on every mode switch, palette
        edit and slider tick, and it never moves a position-upload counter.
        """
        _MODE_GEN[0] += 1          # PHASE 4B: LUT / visibility / range moved
        app = getattr(self, "app", None)
        fn = getattr(getattr(self, "adapter", None), "push_display_state", None)
        out = {"reason": reason, "ok": False, "error": ""}
        if fn is None:
            out["error"] = "adapter has no push_display_state"
            self._last_display_state_push = out
            return out
        try:
            luts = build_luts(app)
        except Exception as exc:                                    # noqa: BLE001
            # A palette build failure must not take the whole switch down: the
            # renderer's previous (valid) tables stay in place and the reason is
            # reported, rather than the viewport going black.
            out["error"] = f"build_luts failed: {exc!r}"
            self._last_display_state_push = out
            return out
        # A FLAT class LUT means Class renders exactly like Neutral, so make
        # sure a palette exists before the tables are built from it.
        if luts["class"] is not None and len(np.unique(
                np.asarray(luts["class"]), axis=0)) <= 1:
            self._ensure_default_class_palette()
            try:
                luts = build_luts(app)
            except Exception:
                pass
        vis = None
        try:
            from gui.render_backend import build_class_visibility
            if app is not None:
                vis = build_class_visibility(app)
        except Exception as exc:                                    # noqa: BLE001
            out["error"] = f"visibility build failed: {exc!r}"
        elev = self._elevation_range()
        inten = self._intensity_range()
        # DEPTH needs a RANGE like every other ramp mode, but its range is
        # camera-derived, not data-derived: depth is distance from the eye, so
        # the same (lo, hi) is wrong as soon as the camera moves. Taking it from
        # the dataset's visible extent keeps the ramp meaningful in 2D and 3D
        # alike, and the value is cheap enough to re-derive on every push.
        depth = self._depth_range()
        try:
            out["ok"] = bool(fn(
                class_lut=luts.get("class"),
                elevation_lut=luts.get("elevation"),
                intensity_lut=luts.get("intensity"),
                visibility=vis, elevation_range=elev, intensity_range=inten,
                depth_range=depth))
        except Exception as exc:                                    # noqa: BLE001
            out["error"] = repr(exc)
        out["elevation_range"] = elev
        out["intensity_range"] = inten
        out["depth_range"] = depth
        out["lut_entries"] = int(luts["class"].shape[0])
        try:
            st = self.adapter.stats()
            out["lut_pushes"] = int(st.get("lut_pushes", 0) or 0)
            out["visibility_pushes"] = int(st.get("visibility_pushes", 0) or 0)
            out["xyz_uploads"] = int(st.get("uploads", 0) or 0)
        except Exception:
            pass
        self._last_display_state_push = out
        return out
    # --- PHASE 1 METHODS (appended below) ---
    def _required_attrs(self) -> tuple:
        """The requirement tuple for the CURRENT mode (Part 2).
        PART 2/4: the nominal mode mapping is INTERSECTED with what the dataset
        actually contains. This is what guarantees an absent optional attribute
        can never hold a block in a permanent pending state - on `123.LAS`, which
        has no RGB, RGB is simply never requested, so nothing waits for it.
        """
        base = required_attributes(self.display_mode)
        if self.shaded_mode and ATTR_NORMAL not in base:
            base = tuple(base) + (ATTR_NORMAL,)
        caps = getattr(self, "capabilities", None)
        if caps is None:
            return base
        return caps.intersect(base)
    def _mode_requirements_report(self) -> str:
        """One line explaining the live requirement decision. Never silent."""
        caps = getattr(self, "capabilities", None)
        nominal = required_attributes(self.display_mode)
        if caps is None:
            return f"required_attrs={nominal} (capabilities unknown)"
        dropped = caps.dropped(nominal)
        line = f"required_attrs={self._required_attrs()} nominal={nominal}"
        if dropped:
            line += f" DROPPED_NOT_IN_DATASET={dropped}"
        return line
    def _attach_normal(self, packed: dict, spec: TileSpec, attrs: tuple) -> None:
        """Seek the sidecar for this tile's block and attach packed normals."""
        key = packed["tile_key"]
        bid = int(packed.get("block_id", -1))
        rd = BlockReadiness(block_id=bid, key=key)
        rd.mark(xyz=packed.get("xyz") is not None,
                cls=packed.get("cls") is not None)
        packed["readiness"] = rd
        if bid < 0:
            rd.normal_ready = False
            packed["normal_error"] = f"tile {key} has no NKPC block_id"
            return
        blob, reason = load_normal_block(
            self.normal_reader, self.normal_cache,
            self.gate.dataset_generation, bid, verify_crc=True)
        if blob is None:
            rd.normal_ready = False
            packed["normal_error"] = reason
            packed["normal_from_cache"] = False
            return
        pts = int(packed.get("pts", 0))
        if int(blob.shape[0]) != pts:
            # Refuse rather than upload a misaligned stream: an off-by-N here
            # shifts every normal and shades a plausible but wrong scene.
            rd.normal_ready = False
            packed["normal_error"] = (
                f"tile {key}: normal count {int(blob.shape[0]):,} != "
                f"point count {pts:,}")
            packed["normal_from_cache"] = False
            return
        rd.normal_ready = True
        packed["normal"] = blob
        packed["normal_from_cache"] = (reason == "ram_hit")
        packed["normal_bytes"] = int(blob.nbytes)
    def _pack_block(self, t, key, ms=0.0, spec=None) -> dict:
        xyz = np.asarray(t.get("xyz"), dtype=np.float64)
        origin = np.asarray(t.get("origin", (0.0, 0.0, 0.0)), dtype=np.float64)
        if xyz.size:
            xyz = xyz + origin
        if xyz.ndim == 1:
            xyz = xyz.reshape(-1, 3)
        rgb = t.get("rgb")
        rgb = np.asarray(rgb).reshape(-1, 3) if rgb is not None else None
        cls = t.get("classification")
        cls = np.asarray(cls) if cls is not None else None
        inten = t.get("intensity")
        inten = np.asarray(inten) if inten is not None else None
        pts = int(xyz.shape[0]) if xyz.size else int(t.get("point_count", 0))
        b = xyz.nbytes + (rgb.nbytes if rgb is not None else 0) \
            + (cls.nbytes if cls is not None else 0) \
            + (inten.nbytes if inten is not None else 0)
        # block_id is the NKPC<->sidecar join key. Resolving it here (from the
        # index, not from disk) is what lets the worker seek straight to the
        # normal payload for this exact block.
        try:
            entry = self.idx.find_block(key[0], key[1])
            block_id = int(entry["block_id"]) if entry is not None else -1
        except Exception:
            block_id = -1
        # PHASE 1: record WHICH streams this block actually holds, measured from
        # the decoded payload rather than from what was requested. A read can
        # legitimately come back short (the cache masks off an absent channel),
        # and recording the request instead of the result is what previously made
        # the streamer believe a Neutral-read block had class data ready.
        attrs = {ATTR_XYZ}
        if rgb is not None:
            attrs.add(ATTR_RGB)
        if cls is not None:
            attrs.add(ATTR_CLASSIFICATION)
        if inten is not None:
            attrs.add(ATTR_INTENSITY)
        return {"tile_key": key, "pts": pts, "bytes": int(b),
                "ms": ms, "xyz": xyz, "rgb": rgb, "cls": cls, "inten": inten,
                "priority": spec.priority if spec else 0.0,
                "lod": key[1], "page_bytes": int(t.get("bytes_read", b)),
                "block_id": block_id, "attrs": frozenset(attrs)}
    def _drain_done(self):
        while True:
            try:
                r = self._done.get_nowait()
            except queue.Empty:
                break
            self.completed += 1
            key = r.get("tile_key")
            # Worker decode time. Accumulated here so the camera thread can
            # report BACKGROUND stream cost separately from its own hot path.
            self._pending_ms.append(float(r.get("ms", 0.0) or 0.0))
            if key in self._pending:
                self._pending.pop(key, None)
            if not key or "error" in r or r.get("pts", 0) == 0:
                self.te.event("tile_read_error", tile=str(key),
                              error=r.get("error", "empty"))
                continue
            # PHASE 12. A completion issued for an OLDER camera generation may
            # enter the RAM cache (that is free, useful work) but MUST NOT be
            # allowed to overwrite the current desired frontier. Only the
            # frontier is generation-scoped; the RAM cache is not.
            rgen = int(r.get("requested_generation", -1))
            cur_gen = int(getattr(self, "camera_generation", 0) or 0)
            if rgen >= 0 and rgen < cur_gen:
                self.stale_completions = int(
                    getattr(self, "stale_completions", 0) or 0) + 1
                r["stale_generation"] = rgen
            with self._ram_lock:
                if key in self.ram_cache:
                    self.ram_bytes -= self.ram_cache[key]["bytes"]
                self.ram_cache[key] = r
                self.ram_bytes += r["bytes"]
                self._trim_ram()
            if rgen >= 0 and rgen < cur_gen:
                self.stale_accepted_into_ram = int(
                    getattr(self, "stale_accepted_into_ram", 0) or 0) + 1
    def _read_tile_done(self, fut):
        """add_done_callback target: push completed tile result to the
        main-thread-serviced _done queue."""
        try:
            self._done.put(fut.result())
        except Exception as e:
            self._done.put({"tile_key": None, "error": repr(e)})
    def _trim_ram(self):
        while self.ram_bytes > self.ram_budget and self.ram_cache:
            k, v = self.ram_cache.popitem(last=False)
            self.ram_bytes -= v["bytes"]
            self.evictions += 1
            rt = self.resident.pop(k, None)
            if rt is not None:
                self.gpu_bytes -= rt.bytes
                # Dropping the entry is a TRUE eviction: the stable slot has to
                # go back to the pool or the arena leaks it forever.
                self._arena_release_slot(rt)
    def _retire_mode_specific(self, keep_normal: bool = True) -> int:
        """Release resident blocks the CURRENT mode can no longer draw.
        In non-Shaded modes NORMAL is not required, so any block whose
        readiness was carried solely by a normal load is no longer a legal
        member of the active set for this mode. They are set EVICTABLE (not
        deleted) so ram_cache still holds the arrays and a later Shaded return
        costs no disk read.
        """
        if keep_normal:
            return 0
        retired = 0
        for k, v in list(self.resident.items()):
            if v.state != GPU_RESIDENT:
                continue
            rd = v.readiness
            if rd is not None and not rd.normal_ready:
                v.state = EVICTABLE
                self.gpu_bytes -= v.bytes
                self.evictions += 1
                retired += 1
        return retired
    # ------------------------------------------------------------------ #
    def _camera_signature(self, camera=None, viewport=None):
        """Minimal, stable identity of the visible streaming view.
        The tuple intentionally contains only the values that define the visible
        world view and the output viewport. It excludes object ids, timestamps,
        and transient state. The output is round-trippable and remains identical
        when the camera is genuinely static.
        """
        try:
            if viewport is not None and len(viewport) >= 4:
                cx = float(viewport[0])
                cy = float(viewport[1])
                half_w = float(viewport[2])
                half_h = float(viewport[3])
            else:
                cx = float(getattr(camera, "cx",
                                   getattr(camera, "center_x", 0.0)) or 0.0)
                cy = float(getattr(camera, "cy",
                                   getattr(camera, "center_y", 0.0)) or 0.0)
                half_w = float(getattr(camera, "half_w",
                                       getattr(camera, "half_width", 0.0)) or 0.0)
                half_h = float(getattr(camera, "half_h",
                                       getattr(camera, "height_m", 0.0)) or 0.0)
                if half_h == 0.0 and half_w > 0.0:
                    width_px = float(getattr(camera, "width_px", 0.0) or 0.0)
                    height_px = float(getattr(camera, "height_px", 0.0) or 0.0)
                    if width_px > 0.0 and height_px > 0.0:
                        half_h = half_w * height_px / width_px
            width_px = int(getattr(camera, "width_px", 0) or 0)
            height_px = int(getattr(camera, "height_px", 0) or 0)
            if width_px <= 0 and viewport is not None and len(viewport) >= 6:
                width_px = int(viewport[4])
                height_px = int(viewport[5])
            q = 1e-4
            cx = round(float(cx) / q) * q
            cy = round(float(cy) / q) * q
            world_width = round((2.0 * float(half_w)) / q) * q
            world_height = round((2.0 * float(half_h)) / q) * q
            return (
                round(float(cx), 6),
                round(float(cy), 6),
                round(float(world_width), 6),
                round(float(world_height), 6),
                int(width_px),
                int(height_px),
            )
        except Exception:
            return None
    def _diag_camera(self, reason, selections=None):
        """Print a camera-change report only when the visible view changes."""
        if not getattr(self, "_diag_enabled", False):
            return
        sig = getattr(self, "_last_camera_signature", None)
        if sig is None or sig == getattr(self, "_diag_camera_signature", None):
            return
        prev = getattr(self, "_diag_camera_signature", None)
        self._diag_camera_signature = sig
        center_x, center_y, world_width, world_height, width_px, height_px = sig
        keys = frozenset((int(o["node_id"]), int(o["lod"]))
                         for o in (selections or []))
        selected_points = sum(int(o["points"]) for o in (selections or []))
        print("[STREAM CAMERA CHANGE]")
        print(f"manager={hex(id(self))}")
        print(f"generation={int(getattr(self, 'camera_generation', 0))}")
        print()
        print("old_signature:")
        print(prev if prev is not None else "None")
        print()
        print("new_signature:")
        print(sig)
        print()
        print("center:")
        print(f"({center_x:.6f}, {center_y:.6f})")
        print()
        print("world_size:")
        print(f"({world_width:.6f}, {world_height:.6f})")
        print()
        print("viewport:")
        print(f"({int(width_px)}x{int(height_px)})")
        print()
        print("camera_state:")
        print(str(getattr(self, "_camera_state", "IDLE") or "IDLE"))
        print()
        print("[CAMERA -> LOD]")
        print(f"generation: {int(getattr(self, 'camera_generation', 0))}")
        print("camera_changed: YES")
        print(f"selected_key_count: {len(keys)}")
        print(f"selected_points: {selected_points}")
        print(f"selected_keys_hash: {hash(keys)}")
        print("[/CAMERA -> LOD]")

    # PAN / ZOOM COVERAGE-CONTINUITY DIAGNOSTICS (read-only).            #
    # ------------------------------------------------------------------ #
    def _print_overview_identity(self):
        """Print the overview's REAL identity once, so a wrong key is obvious.
        The runtime key must be (node_id, lod) == (0, 4) and must NOT be the
        block id 1338. Retirement compares against `_overview_key()`, so a key
        mismatch leaves the overview permanently unrecognisable - which is how
        a stale build can look "fixed in the harness, still live in the GUI".
        """
        try:
            key = self._overview_key()
            true_b = self._overview_true_bounds()
            node_b = _node_bounds_xy(self.idx, key[0]) if key else None
            print("[OVERVIEW IDENTITY]")
            print(f"block_id: {int(self.idx.overview_block_id)}")
            print(f"runtime_key: {key}  "
                  f"{'OK' if key == (0, 4) else 'FAIL - expected (0, 4)'}")
            print(f"node_id: {key[0] if key else '?'}  "
                  f"lod: {key[1] if key else '?'}")
            print(f"dataset_bounds: {true_b}")
            print(f"node{key[0] if key else '?'}_bounds: {node_b}")
            print(f"retirement_bounds_source: "
                  f"{'DATASET_BOUNDS' if true_b is not None else 'NODE_BOUNDS'}")
            print("[/OVERVIEW IDENTITY]")
        except Exception as e:
            print(f"[OVERVIEW IDENTITY] unavailable: {e!r}")
    def _overview_diag(self, reason):
        """Report overview residency only when its state CHANGES.
        Covers the full lifetime - startup, first paint, refinement, retirement
        and any re-admission - without printing once per frame at 60 Hz.
        """
        if not getattr(self, "_diag_enabled", False):
            return
        key = self._overview_key()
        if key is None:
            return
        tile = self.resident.get(key)
        ram = key in getattr(self, "ram_cache", {})
        gpu = tile is not None and tile.state == GPU_RESIDENT
        complete = bool(tile is not None and tile.complete)
        sig = (ram, gpu, complete)
        if sig == getattr(self, "_ov_diag_state", None):
            return
        was_gone = (getattr(self, "_ov_diag_state", None) is not None
                    and not getattr(self, "_ov_diag_state", (True,))[1])
        self._ov_diag_state = sig
        print(f"[OVERVIEW STATE] reason={reason} key={key} "
              f"RAM_resident={ram} GPU_resident={gpu} complete={complete} "
              f"manager={hex(id(self))}")
        if gpu and was_gone:
            # A silent re-admission is a coverage regression risk: something
            # put the coarse floor back without an explicit reason.
            print(f"[OVERVIEW RE-ADMITTED] reason={reason} "
                  f"camera_generation={int(getattr(self, '_gen', 0))} "
                  f"requested_generation="
                  f"{int(getattr(self, '_service_gen', 0))} "
                  f"manager={hex(id(self))}")
    def _retire_report(self, retired):
        """Explain the retirement decision, but only when it is informative.
        PART 13 - RATE LIMITED. This runs inside `_retire_superseded`, i.e. twice
        per reconcile, i.e. ~120 times per second. It printed on every IDLE tick,
        which is hundreds of lines per second on the UI thread; the printing was
        itself a responsiveness defect.
        Now: an unchanged decision prints at most once per heartbeat, and a
        decision that actually CHANGES prints immediately.
        """
        if not getattr(self, "_diag_enabled", False):
            return
        key = self._overview_key()
        tile = self.resident.get(key) if key is not None else None
        active = [k for k, v in self.resident.items()
                  if v.state == GPU_RESIDENT]
        rep = getattr(self, "_last_coverage_report", {}).get(key)
        sig = (key in active if key is not None else None,
               rep[0] if rep else None,
               round(float(rep[1]), 4) if rep else None)
        changed = sig != getattr(self, "_retire_sig", None)
        self._retire_sig = sig
        now = time.perf_counter()
        last = float(getattr(self, "_retire_report_last", 0.0) or 0.0)
        if not changed and (now - last) < PENDING_DIAG_HEARTBEAT_S:
            return
        self._retire_report_last = now
        if not (changed or retired > 0
                or getattr(self, "_camera_state", "") == "IDLE"):
            return
        true_b = self._overview_true_bounds()
        print(f"[RETIRE SUPERSEDED] called=YES manager={hex(id(self))} "
              f"retired_this_pass={retired} active_blocks={len(active)} "
              f"overview_key={key} "
              f"decision={'KEEP' if (key in active if key is not None else False) else 'RETIRE'}"
              f" reason={rep[0] if rep else 'n/a'} "
              f"union_coverage={round(float(rep[1]), 4) if rep else 'n/a'} "
              f"threshold={COVERAGE_TO_RELEASE_BLOCK} "
              f"overview_true_bounds={true_b}")

    def _overview_key(self):
        """RUNTIME key of the overview block, or None.
        header['overview_block_id'] is the BLOCK id (1338); the runtime/hierarchy
        key uses the NODE id, which for the overview is 0. Using the block id
        here produced a key that never matches the resident entry, so the
        overview could never be recognised - let alone retired.
        """
        # PHASE 4B: the answer is a pure function of the (immutable) index, but
        # the scan walks every block through the memmap on EVERY diagnostic
        # call. Computed once per index object.
        cache = getattr(self, "_overview_key_cache", None)
        if cache is not None and cache[0] is getattr(self, "idx", None):
            return cache[1]
        try:
            bid = int(self.idx.overview_block_id)
            blocks = self.idx.blocks
            hit = np.flatnonzero(np.asarray(blocks["block_id"]) == bid)
            key = (None if hit.size == 0 else
                   (int(blocks["node_id"][hit[0]]), int(blocks["lod"][hit[0]])))
        except Exception:
            return None
        self._overview_key_cache = (self.idx, key)
        return key
    def _overview_true_bounds(self):
        """TRUE XY footprint of the overview block's DATA.
        The overview is stored under node_id 0, whose NODE box is only
        848x856 m, yet its points cover the whole 1000x1000 m dataset.
        Retirement uses node boxes, so comparing a 848 m node box against the
        finer tiles can never mark the overview superseded - it stayed active
        forever alongside refined children, adding its full 299,676 points to
        the submitted total (1,023,399 selected -> 1,326,359 submitted).
        The footprint is measured ONCE from the decoded points and cached.
        """
        key = self._overview_key()
        if key is None:
            return None
        cached = getattr(self, "_ov_true_bounds_cache", None)
        if cached is not None:
            return cached
        try:
            # The overview's TRUE world footprint is the dataset extent: it is
            # a decimation of the WHOLE scene. read_overview() returns tile-local
            # coordinates, so measuring it there yields a box at the origin,
            # which would never overlap any world-space tile. The NKIDX header
            # bounds ARE the world footprint.
            hdr = self.idx.header
            b = ((float(hdr["bounds_min"][0]), float(hdr["bounds_min"][1])),
                 (float(hdr["bounds_max"][0]), float(hdr["bounds_max"][1])))
            self._ov_true_bounds_cache = b
            return b
        except Exception:
            return None
    def _block_bounds_for_key(self, key):
        """Spatial footprint for a resident or selected node key."""
        ov_key = self._overview_key()
        ov_true = self._overview_true_bounds()
        if ov_true is not None and key == ov_key:
            return ov_true
        return _node_bounds_xy(self.idx, key[0])
    def _tile_readiness_snapshot(self, key, tile=None):
        """Mode-aware readiness for a candidate replacement tile.
        PHASE 0 FIX. `tile` may be a ResidentTile OR a packed RAM-cache dict
        (what `_pack_block` returns), and the old code read `sprite.xyz` off
        whichever it got - raising AttributeError("'dict' object has no
        attribute 'xyz'") for every decoded-but-not-yet-admitted replacement.
        That diagnostic aborted `_reconcile` -> retirement, request scheduling
        and the GPU flush. Each field is now read through its own canonical
        accessor, so both runtime forms answer identically. Purely diagnostic and
        additionally wrapped by the caller, so it can never abort a frame again.
        """
        required = tuple(self._required_attrs())
        normal_required = bool(requires_normal(self.display_mode))
        # A mode can never BLOCK on an attribute the dataset does not contain.
        # Without this the snapshot asked for RGB on an RGB-less file and
        # reported `blocking_attribute: rgb` forever, which is what kept the
        # coarse parents alive and produced the island/gap rendering.
        if not normal_required and self.shaded_mode:
            normal_required = ATTR_NORMAL in required
        sprite = tile
        if sprite is None:
            sprite = self.resident.get(key)
        if sprite is None:
            sprite = getattr(self, "ram_cache", {}).get(key)
        count = tile_count(sprite)
        xyz_ready = bool(sprite is not None and tile_xyz(sprite) is not None
                         and count > 0)
        rgb_ready = (ATTR_RGB not in required) or bool(
            sprite is not None
            and tile_field(sprite, "rgb", PACKED_RGB_KEYS) is not None)
        class_ready = (ATTR_CLASSIFICATION not in required) or bool(
            sprite is not None
            and tile_field(sprite, "cls", PACKED_CLS_KEYS) is not None)
        intensity_ready = (ATTR_INTENSITY not in required) or bool(
            sprite is not None
            and tile_field(sprite, "inten", PACKED_INTEN_KEYS) is not None)
        normal_present = tile_normal_ready(sprite)
        # NORMAL_ready reports RAW availability, exactly like
        # `ResidentTile.normal_ready`. Whether it is REQUIRED is a separate
        # field; conflating the two is what made a Classification block look
        # "blocked" on a normal it was never going to ask for.
        normal_ready = normal_present
        complete = tile_complete_fn(sprite)
        if normal_required and not normal_present:
            # Never draw a half-shaded block: completeness in Shaded is
            # XYZ+CLASS+NORMAL, so a missing normal is never "complete".
            complete = False
        blocking = []
        if not xyz_ready:
            blocking.append("xyz")
        if ATTR_RGB in required and not rgb_ready:
            blocking.append("rgb")
        if ATTR_CLASSIFICATION in required and not class_ready:
            blocking.append("class")
        if ATTR_INTENSITY in required and not intensity_ready:
            blocking.append("intensity")
        if normal_required and not normal_present:
            blocking.append("normal")
        return {
            "XYZ_ready": xyz_ready,
            "RGB_ready": rgb_ready,
            "CLASS_ready": class_ready,
            "INTENSITY_ready": intensity_ready,
            "NORMAL_required": normal_required,
            "NORMAL_ready": normal_ready,
            "tile_complete": complete,
            "blocking_attribute": blocking[0] if blocking else None,
            "runtime_form": ("none" if sprite is None else
                             ("packed_dict" if tile_is_mapping(sprite)
                              else type(sprite).__name__)),
        }
    def _tile_readiness(self, key, tile=None):
        """`_tile_readiness_snapshot`, guaranteed not to raise.
        Diagnostics must never abort on_frame, camera update, request scheduling,
        GPU flush or retirement. A failure here degrades to "blocked by unknown"
        instead of killing the streaming tick.
        """
        def _call():
            return self._tile_readiness_snapshot(key, tile)
        snap = self._diag_guard("readiness").run(_call)
        if snap is None:
            return {"XYZ_ready": False, "RGB_ready": False,
                    "CLASS_ready": False, "INTENSITY_ready": False,
                    "NORMAL_required": False, "NORMAL_ready": False,
                    "tile_complete": False, "blocking_attribute": "unknown",
                    "runtime_form": "unavailable"}
        return snap
    def _emit_pending_replacement_diag(self, parent_key, replacement_keys):
        """PART 13 - print state CHANGES, never a per-frame key dump.
        This used to print every replacement key and every readiness field on
        every reconcile pass. At 60 Hz with a large frontier that is hundreds of
        lines per second on the UI thread, which itself starves the GUI and makes
        the renderer look slow.
        It is now throttled to: the first occurrence per distinct signature, a
        periodic heartbeat while the condition persists, and any CHANGE (a new
        blocking attribute is exactly the signal that matters).
        """
        if not replacement_keys:
            return
        ordered = tuple(sorted((int(k[0]), int(k[1])) for k in replacement_keys))
        required = tuple(self._required_attrs())
        signature = (str(self.display_mode), required, len(ordered))
        blocking_summary = None
        per_key = []
        for key in ordered:
            snap = self._tile_readiness(key)
            per_key.append((key, snap.get("blocking_attribute")))
            if blocking_summary is None:
                blocking_summary = snap.get("blocking_attribute")
        sig = (signature, blocking_summary)
        now = time.perf_counter()
        # Rate limit FIRST and unconditionally. A diagnostic that runs inside
        # _reconcile cannot be allowed to print per frame at any volume: at 60 Hz
        # even a single line is 60 lines/second and the console itself becomes the
        # performance problem.
        last = float(getattr(self, "_pending_diag_last", 0.0) or 0.0)
        if (now - last) < PENDING_DIAG_HEARTBEAT_S:
            self._pending_diag_sig = sig
            return
        # Beyond the rate limit, print only when something NEW is true: a
        # blocking attribute never seen before, or a different requirement set.
        seen = getattr(self, "_pending_diag_seen", None)
        if seen is None:
            seen = set()
            self._pending_diag_seen = seen
        novel = bool(blocking_summary) and blocking_summary not in seen
        if blocking_summary is not None:
            seen.add(blocking_summary)
        prev = getattr(self, "_pending_diag_sig", None)
        req_changed = prev is not None and prev[0][1] != required
        if not (novel or req_changed):
            self._pending_diag_sig = sig
            self._pending_diag_last = now
            return
        self._pending_diag_sig = sig
        self._pending_diag_last = now
        counts = {}
        for _k, blk in per_key:
            counts[blk] = counts.get(blk, 0) + 1
        print(f"[PENDING REPLACEMENT] mode={self.display_mode} "
              f"required_attrs={required} parent_key="
              f"{tuple(int(v) for v in parent_key)} "
              f"pending_tiles={len(ordered)} "
              f"blocked_by={counts} "
              f"first_keys={[list(k) for k, _b in per_key[:4]]}")
        # A block blocked on an attribute the dataset does not contain is a
        # PERMANENT condition, not a transient one. Say so once, loudly,
        # because it is the signature that silently produces visual gaps.
        caps = getattr(self, "capabilities", None)
        if caps is not None:
            impossible = [a for a in required if not caps.supports(a)]
            if impossible:
                print(f"[PENDING REPLACEMENT] WARNING: required but absent "
                      f"from this dataset: {impossible} - these blocks can "
                      f"never become complete and their parents can never "
                      f"retire. {caps.describe()}")
    def _retire_superseded(self, specs=None) -> int:
        """PART 5/11 - retire a coarse block only when its replacement COVERS it.
        The never-blank invariant, stated exactly:
            retire C  <=>  union of READY finer blocks covers C
                          AND no queued finer replacement is still outstanding
        Coverage is a UNION property. The previous gate asked whether ONE finer
        block covered >=80% of C, which is a containment test wearing a coverage
        test's clothes. For the LOD4 overview - whose footprint is the entire
        1000x1000 m dataset while every finer tile is a small part of it - no
        single child can ever reach that threshold, so C stayed protected
        forever and blocks only accumulated (44 blocks / LOD4=1 /
        1,326,359 submitted / 1.296 ppp).
        Any queued-but-unready finer block that overlaps C keeps C alive: that
        is the coverage-atomic handoff, so a region is never left empty while
        its replacement streams.
        """
        pending_keys = set()
        desired_keys = set()
        if specs is not None:
            desired_keys.update((int(s.node_id), int(s.lod)) for s in specs)
            pending_keys.update(desired_keys)
        pending_keys.update(getattr(self, "_pending", {}).keys())
        _cur_gen = int(getattr(self, "camera_generation", 0) or 0)
        for _k, _r in getattr(self, "ram_cache", {}).items():
            # A stale completion the frontier no longer wants is not an
            # "outstanding replacement": letting it protect its parents forever
            # is what made unselected blocks accumulate.
            _sg = _r.get("stale_generation") if isinstance(_r, dict) else None
            if (specs is not None and _sg is not None
                    and 0 <= int(_sg) < _cur_gen and tuple(_k) not in desired_keys):
                continue
            pending_keys.add(_k)
        # Bounds for every key we may need, computed once per pass. The
        # resolution loop below is O(n^2) on a handful of resident blocks, so
        # resolving bounds lazily inside it would repeat real work per pair.
        resident = [(k, v) for k, v in self.resident.items()
                    if v.state == GPU_RESIDENT and v.complete
                    and not getattr(v, "pinned", False)]
        bb_of = {}
        for k, _v in resident:
            b = self._block_bounds_for_key(k)
            if b is not None:
                bb_of[k] = b
        pending_bb = {}
        for k in pending_keys:
            if k in bb_of:
                continue
            b = self._block_bounds_for_key(k)
            if b is not None:
                pending_bb[k] = b
        protected = set()
        coverage_report = {}
        for coarse_key, _coarse in resident:
            coarse_bb = bb_of.get(coarse_key)
            if coarse_bb is None:
                continue
            # The frontier SELECTED this block for the current view (zoom-out /
            # pan back): finer leftovers from an earlier view must not retire it.
            if coarse_key in desired_keys:
                protected.add(coarse_key)
                coverage_report[coarse_key] = ("DESIRED", 1.0)
                continue
            # 1. An outstanding finer replacement overlapping this coarse block
            #    keeps it alive. This is the never-blank / coverage-atomic rule.
            waiting = False
            pending_overlap = []
            for sel_key, sel_bb in pending_bb.items():
                if int(sel_key[1]) >= int(coarse_key[1]):
                    continue
                if _boxes_overlap(coarse_bb, sel_bb):
                    waiting = True
                    pending_overlap.append(sel_key)
            if waiting:
                protected.add(coarse_key)
                coverage_report[coarse_key] = ("PENDING", 0.0)
                # PHASE 0: this diagnostic runs INSIDE _reconcile, i.e. inside the
                # production path. It must never be able to abort retirement,
                # request scheduling or the GPU flush - which is exactly what the
                # `sprite.xyz` AttributeError did.
                self._diag_guard("pending_replacement").run(
                    self._emit_pending_replacement_diag,
                    coarse_key, pending_overlap)
                continue
            # 2. Otherwise require the UNION of ready finer blocks to cover it.
            finer_boxes = [bb for k, bb in bb_of.items()
                           if k != coarse_key
                           and int(k[1]) < int(coarse_key[1])
                           and _boxes_overlap(coarse_bb, bb)]
            if not finer_boxes:
                # Nothing finer exists here: this block is the floor.
                protected.add(coarse_key)
                coverage_report[coarse_key] = ("NO_FINER", 0.0)
                continue
            ratio = union_coverage_ratio(coarse_bb, finer_boxes)
            coverage_report[coarse_key] = ("COVERED" if
                                           ratio >= COVERAGE_TO_RELEASE_BLOCK
                                           else "PARTIAL", ratio)
            if ratio < COVERAGE_TO_RELEASE_BLOCK:
                protected.add(coarse_key)
        retired = 0
        for k, v in list(self.resident.items()):
            if v.state != GPU_RESIDENT:
                continue
            if not v.complete:
                continue
            if k in protected:
                continue
            if getattr(v, "pinned", False):
                # PART 47: coarse coverage is a contract, not a cache entry.
                continue
            v.state = EVICTABLE
            self.gpu_bytes -= v.bytes
            self.evictions += 1
            retired += 1
        self._last_coverage_report = coverage_report
        self._diag_guard("retire_report").run(self._retire_report, retired)
        self._diag_guard("overview_diag").run(self._overview_diag,
                                              "retire_superseded")
        return retired
    def _reconcile(self, specs):
        # PART 11/12: settle the ARENA QUESTION FIRST. The packed-offset fallback
        # at the end of this method must only run when there is genuinely no
        # stable-slot pool, and `_arena_ensure()` is the one place that decides.
        # Doing it here (not only in `_flush_gpu`) keeps "does this tile own a
        # pooled slot?" a single answer for the whole frame.
        self._arena_ensure()
        # PHASE 5: in frontier mode ONE representation per node is drawn, so
        # "retire the superseded parent" is neither needed (the parent is simply
        # not selected; it stays GPU-resident as a free fallback) nor affordable
        # (it is an O(resident x pending) Python pass). Admission instead evicts
        # COLD tiles to make room, and RAM is trimmed to its budget.
        _frontier = self._streaming_frontier_enabled()
        self._evict_cands = None
        if not _frontier:
            # Part 5/11: retire superseded coarse blocks BEFORE admitting new
            # ones, so refinement replaces its parent instead of stacking on it.
            self._retire_superseded(specs=specs)
        desired = {(int(s.node_id), int(s.lod)) for s in (specs or ())}
        self._frontier_protect = (
            (set(getattr(self, "active_draw_keys", ()) or ()) | desired)
            if _frontier else None)
        cur_gen = int(getattr(self, "camera_generation", 0) or 0)
        activated_now = set()
        for k, r in list(self.ram_cache.items()):
            rt = self.resident.get(k)
            if rt is not None:
                # REVIVAL. A block retired to EVICTABLE keeps its arrays and its
                # ram_cache entry, so when the frontier selects it again (zoom
                # out, pan back) it must become drawable again. Before this, a
                # selected EVICTABLE tile fell through BOTH `_request_missing`
                # (it is in ram_cache) and this loop (it is in resident): it was
                # never re-requested and never re-activated, so the region stayed
                # empty for good - the hole left behind after a pan.
                if rt.state != EVICTABLE or tuple(k) not in desired:
                    continue
                if rt.count == 0 or rt.xyz is None or not rt.complete:
                    continue
                if self.gpu_bytes + rt.bytes > self.gpu_budget \
                        and not self._evict_cold_for_bytes(rt.bytes):
                    continue
                rt.state = GPU_RESIDENT
                rt.last_used = time.perf_counter()
                self.gpu_bytes += rt.bytes
                activated_now.add(tuple(k))
                self.revived_tiles = int(getattr(self, "revived_tiles", 0) or 0) + 1
                continue
            # PHASE 12. A completion issued for an OLDER camera generation may
            # sit in the RAM cache, but is only ADMITTED to the draw set while
            # the current frontier still wants it.
            _rgen = int(r.get("stale_generation", -1) if r.get("stale_generation") is not None else -1)
            if 0 <= _rgen < cur_gen and tuple(k) not in desired:
                continue
            need = r["bytes"] + int(r.get("normal_bytes", 0) or 0)
            if self.gpu_bytes + need > self.gpu_budget \
                    and not self._evict_cold_for_bytes(need):
                continue
            # Part 18: normal bytes participate in the budget, so a tile with
            # normals can legitimately fail to be admitted. That is correct -
            # it must be rejected as a WHOLE, never half-uploaded.
            self.resident[k] = ResidentTile(
                node_id=k[0], lod=k[1], count=r["pts"], bytes=need,
                priority=r["priority"], last_used=time.perf_counter(),
                state=GPU_PENDING, xyz=r["xyz"], rgb=r.get("rgb"),
                cls=r.get("cls"), inten=r.get("inten"),
                block_id=int(r.get("block_id", -1)), normal=r.get("normal"),
                normal_from_cache=bool(r.get("normal_from_cache", False)),
                readiness=r.get("readiness"), normal_error=str(r.get(
                    "normal_error", "") or ""),
                requires_normal=ATTR_NORMAL in self._required_attrs(),
                # PART 47: with the resident pyramid, a coarse block is the
                # always-complete fallback the frame selects from. Retiring it
                # would take that choice away and force the finer block for its
                # whole footprint.
                #
                # PHASE 5: NOT in frontier mode. Pinning every coarse rung ever
                # visited fills the arena with the coarse pyramid of the whole
                # dataset (measured: finer tiles arrived and were immediately
                # demoted because nothing was evictable) and at a billion points
                # it would pin the entire pyramid. The coarsest rung of each node
                # is instead evicted LAST by `_evict_cold_for_bytes`, and the
                # coarse floor of every visible node is requested first.
                pinned=bool(self._resident_pyramid_enabled()
                            and int(k[1]) > 0 and not _frontier))
            self.gpu_bytes += need
        for v in self.resident.values():
            if v.state in (GPU_PENDING, RAM_READY):
                if v.count == 0 or v.xyz is None:
                    v.state = EVICTABLE
                elif self._needs_normal() and not v.complete:
                    # Part 5/13: XYZ+CLASS without NORMAL is not a legal
                    # STORED shaded block. It stays out of the draw set and the
                    # coarse floor keeps covering its region.
                    v.state = EVICTABLE
                else:
                    v.state = GPU_RESIDENT
                    activated_now.add((int(v.node_id), int(v.lod)))
        # PHASE 12. A block that completed for an OLDER camera generation must
        # not become part of the CURRENT desired frontier. It may sit in the RAM
        # cache for free; promoting it into the draw set for a view it was not
        # selected for is exactly the "incorrectly activated" failure. Counted,
        # never silently allowed. The marker is read from ram_cache itself, not
        # from a side log, so the check cannot depend on a previous check.
        # Stale blocks are now REFUSED at admission (above), so none can become
        # active here. This is the guard that proves it: only a block activated
        # in THIS pass can be "incorrectly activated". The old post-hoc loop
        # re-evaluated every ram_cache entry on every pass against a permanent
        # marker, evicting valid resident coverage (fallback parents included)
        # and re-counting the same tile each time.
        for k in activated_now:
            r = (getattr(self, "ram_cache", {}) or {}).get(k)
            if r is None:
                continue
            _rg = r.get("stale_generation")
            if _rg is not None and 0 <= int(_rg) < cur_gen and k not in desired:
                self.stale_incorrectly_activated += 1
        # Part 5/11: retire AGAIN, now that this pass's finer blocks have been
        # admitted and promoted to GPU_RESIDENT. The pre-admission pass cannot
        # see them, so without this the coarse parent would survive one extra
        # frame and register as illegal persistent overlap.
        if not _frontier:
            self._retire_superseded(specs=specs)
        else:
            self._trim_evictable()
        order = sorted(self.resident.items(), key=lambda kv: kv[1].priority)
        while self.gpu_bytes > self.gpu_budget and order:
            k, v = order.pop(0)
            if k == (self.idx.overview_block_id,
                     int(self.idx.header["lod_count"]) - 1):
                continue
            if getattr(v, "pinned", False):
                continue                     # PART 47: pinned coarse coverage stays
            v.state = EVICTABLE
            self.gpu_bytes -= v.bytes
            self.evictions += 1
        if not self._arena_active():
            # ---- WHOLE-BUFFER FALLBACK --------------------------------------
            # Without a native stable-slot pool the only addressable layout is a
            # single packed concatenation, so the offsets are recomputed here and
            # `_flush_gpu` re-sends the whole buffer. That is O(all resident
            # points) per residency change - the cost the arena exists to remove -
            # but it is kept so the engine still runs on a DLL that predates the
            # arena entry points.
            live = [v for v in self.resident.values()
                    if v.state == GPU_RESIDENT]
            live.sort(key=lambda v: -v.priority)
            off = 0
            for v in live:
                v.first = off
                v.arena_slot_valid = False
                off += v.count
        # ---- WITH THE ARENA: NOTHING IS TOUCHED HERE ------------------------
        # A slot is assigned once, in `_arena_flush`, and never moves. That is the
        # entire point of a stable allocation: recomputing `first` on every
        # reconcile is what forced a whole-buffer re-upload.
    def _block_attribute_trace(self, keys, required):
        """[BLOCK ATTRIBUTE TRACE] - prove block ownership, one block at a time.
        DEV-only (`NAKSHA_DEV_ATTRIBUTE_TRACE=1`). This exists because the
        AGGREGATE form cannot localise a fault:
            [CLASSIFICATION STREAM MISMATCH] xyz_count=24569032 shape=(299676,)
        says only "somewhere, 299676 of 24569032 arrived". With 289 blocks there
        are 289 candidate causes and no way to choose between them. This walks the
        real submission order and prints, per block, the node/lod identity, every
        stream's length, and every stream's OFFSET in the concatenated buffer.
        The offsets are the part that matters. Two streams can both be the right
        LENGTH and still be mis-bound if one was assembled in a different block
        order than the other. Lengths alone cannot detect that; offsets can.
        Pure observation - it mutates nothing.
        """
        want_cls = ATTR_CLASSIFICATION in required
        want_int = ATTR_INTENSITY in required
        want_nrm = ATTR_NORMAL in required
        # Running offsets: where each stream's NEXT block would land.
        off_xyz = off_cls = off_int = off_nrm = 0
        gen = int(getattr(self, "_gen", 0) or 0)
        for k in keys:
            v = self.resident.get(k)
            if v is None:
                continue
            xyz_n = int(np.asarray(v.xyz).reshape(-1, 3).shape[0]) \
                if v.xyz is not None else 0
            cls_n = 0 if getattr(v, "cls", None) is None else int(
                np.asarray(v.cls).reshape(-1).size)
            int_n = 0 if getattr(v, "inten", None) is None else int(
                np.asarray(v.inten).reshape(-1).size)
            nrm = getattr(v, "normal", None)
            nrm_n = 0 if nrm is None else int(np.asarray(nrm).shape[0])
            active = bool(getattr(v, "state", None) == GPU_RESIDENT)
            # `submitted` = the renderer has actually been told to draw it.
            submitted = bool(active and xyz_n > 0 and int(v.count) > 0)
            bad = []
            if want_cls and cls_n != xyz_n:
                bad.append(f"class {cls_n}!={xyz_n}")
            if want_int and int_n != xyz_n:
                bad.append(f"intensity {int_n}!={xyz_n}")
            if want_nrm and nrm_n != xyz_n:
                bad.append(f"normal {nrm_n}!={xyz_n}")
            print(f"[BLOCK ATTRIBUTE TRACE] key={k} "
                  f"block_id={int(getattr(v, 'block_id', -1))} "
                  f"lod={int(getattr(v, 'lod', k[1] if len(k) > 1 else -1))} "
                  f"xyz_count={xyz_n} class_count={cls_n} "
                  f"intensity_count={int_n} normal_count={nrm_n} "
                  f"xyz_offset={off_xyz} class_offset={off_cls} "
                  f"intensity_offset={off_int} normal_offset={off_nrm} "
                  f"source_generation={gen} resident_state={v.state} "
                  f"active={active} submitted={submitted} "
                  f"{'FAIL:' + ','.join(bad) if bad else 'PASS'}", flush=True)
            off_xyz += xyz_n
            off_cls += cls_n
            off_int += int_n
            off_nrm += nrm_n
        print(f"[BLOCK ATTRIBUTE TRACE] TOTAL blocks={len(keys)} "
              f"xyz={off_xyz} class={off_cls} intensity={off_int} "
              f"normal={off_nrm} "
              f"aligned={off_xyz == off_cls if want_cls else True}", flush=True)
    def _report_attribute_alignment(self, keys, xyz, cls, int_required, inten,
                                   cls_required, nrm_required) -> dict:
        """[ATTRIBUTE ALIGNMENT] - prove, per submitted block, that every stream
        is the SAME length as XYZ and belongs to the SAME block.
        DEV mode only (`NAKSHA_DEV_ATTRIBUTE_TRACE=1`), because it is per-block
        and a FULL_RESIDENT frame can carry ~290 blocks.
        This is the diagnostic that makes a mismatch LOCAL: it names the exact
        key that lacks the stream, instead of reporting an aggregate
        `xyz_count=24569032 shape=(299676,)` that can only say "something is
        wrong somewhere".
        """
        n = int(getattr(xyz, "shape", (0,))[0])
        result = {"xyz": n, "class": 0, "intensity": 0, "normal": 0,
                  "failures": [], "ok": True}
        resident_normal_points = int(getattr(self, "normal_resident_points", 0) or 0)
        # Normal coverage is tracked for the WHOLE resident set, not per block,
        # so it is reported as a set-level number rather than per key.
        for k in keys:
            v = self.resident.get(k)
            if v is None:
                continue
            want = int(v.count)
            cls_n = 0 if getattr(v, "cls", None) is None else int(
                np.asarray(v.cls).reshape(-1).size)
            int_n = 0 if getattr(v, "inten", None) is None else int(
                np.asarray(v.inten).reshape(-1).size)
            bad = []
            if cls_required and cls_n != want:
                bad.append(f"class {cls_n} != {want}")
            if int_required and int_n != want:
                bad.append(f"intensity {int_n} != {want}")
            if bad:
                result["ok"] = False
                result["failures"].append((k, "; ".join(bad)))
        result["class"] = 0 if cls is None else int(np.asarray(cls).reshape(-1).size)
        result["intensity"] = 0 if inten is None else int(
            np.asarray(inten).reshape(-1).size)
        result["normal"] = resident_normal_points
        if os.environ.get("NAKSHA_DEV_ATTRIBUTE_TRACE", "").strip() not in (
                "", "0", "false", "False"):
            print(f"[ATTRIBUTE ALIGNMENT] key=<{len(keys)} blocks> "
                  f"xyz_count={n} class_count={result['class']} "
                  f"intensity_count={result['intensity']} "
                  f"normal_count={result['normal']} "
                  f"class_required={cls_required} "
                  f"intensity_required={int_required} "
                  f"normal_required={nrm_required} "
                  f"{'PASS' if result['ok'] else 'FAIL'}", flush=True)
            for k, why in result["failures"][:8]:
                self._set_attr_error(k, why)
                print(f"[ATTRIBUTE ALIGNMENT]   key={k} -> {why}", flush=True)
        elif not result["ok"]:
            # Even without the verbose trace, a genuine mismatch must be visible:
            # it is the reason a mode silently renders Neutral.
            print(f"[ATTRIBUTE MISMATCH] xyz={n} class={result['class']} "
                  f"intensity={result['intensity']} "
                  f"blocks_failing={len(result['failures'])} "
                  f"(set NAKSHA_DEV_ATTRIBUTE_TRACE=1 for per-block detail)",
                  flush=True)
            for k, why in result["failures"][:4]:
                self._set_attr_error(k, why)
        return result
    def _interaction_lod_keys(self, keys):
        """The blocks an INTERACTION frame should submit.
        PHASE 4/5/6 - the fix for "FULL_RESIDENT drew all 26.9M points on every
        camera update".
        FULL_RESIDENT means "the data stays in VRAM". It does NOT mean "draw all
        of it every frame". Measured on this machine: ~700 ms per wheel notch
        with 26.9M points submitted, against 0.27 ms of CPU work - the cost is
        entirely in submitting and rasterising the points.
        PREFERENCE ORDER (cheapest and safest first):
        1. THE OVERVIEW BLOCK. It covers the entire dataset, so it cannot leave
           a hole, and it is ONE draw call of ~300K points - a ~90x reduction
           in both submitted points and draw calls. It is already resident, so
           selecting it costs nothing: no upload, no layout change, and
           critically NO re-indexing of the LOD0 blocks, which would misalign
           the class/intensity arrays that are already bound by point index.
        2. ANY OTHER resident coarse (lod > 0) block, if one exists.
        Returns None when neither is available, and the caller then keeps the
        previous behaviour rather than drawing nothing.
        """
        coarse = [k for k in keys if int(k[1]) > 0]
        if not coarse:
            return None
        coarsest = max(int(k[1]) for k in coarse)
        # One block if the hierarchy offers a single coarsest node (the
        # overview); otherwise every coarsest node, which still tiles the space
        # far more cheaply than LOD0.
        return [k for k in coarse if int(k[1]) == coarsest]
    # ==================================================================== #
    # STABLE-OFFSET GPU ARENA (Parts 11/12/13/14/15).                        #
    #                                                                      #
    # One canonical position pool, shared by EVERY display mode. A tile is  #
    # written once, at a slot it keeps until it is genuinely evicted, so:    #
    #                                                                      #
    #   * a LOD change is a DRAW-RANGE edit (Part 10),                      #
    #   * one arriving block uploads ONE block (Part 14),                   #
    #   * class / intensity / normal address the same points as XYZ         #
    #     because they are written at the same slot (Part 13/53).           #
    # ==================================================================== #
    def _arena_active(self) -> bool:
        """True when this manager has a live stable-slot pool."""
        if getattr(self, "arena", None) is None:
            return False
        try:
            return bool(self.adapter.supports_arena())
        except Exception:
            return False
    def _arena_target_capacity(self) -> int:
        """Points the position pool may hold, from the MEASURED GPU budget.
        Deliberately NOT mode-dependent. The position allocation is canonical
        and shared (Part 13), so sizing it per mode would force a re-reserve - and
        therefore a full re-upload - on every mode switch, breaking the
        "basic mode switch uploads 0 XYZ" guarantee. Per-mode cost is handled by
        the admission byte budget instead, which is where it belongs.
        """
        budget = int(getattr(self, "gpu_budget", 0) or 0)
        if budget <= 0:
            return 0
        pts = int((budget * ARENA_RESERVE_FRACTION)
                  // ARENA_POSITION_BYTES_PER_POINT)
        return pts if pts >= 1024 else 0
    def _arena_ensure(self) -> bool:
        """Create the pool once, and only when the adapter can host it.
        Returns False when there is no arena, and the caller then uses the
        whole-buffer path. The pool is never REGROWN: admission is already
        byte-budgeted, so a pool sized from that budget is a ceiling, and growing
        it mid-session would invalidate every live slot.
        """
        if getattr(self, "arena", None) is not None:
            return True
        adapter = getattr(self, "adapter", None)
        if adapter is None:
            return False
        try:
            if not adapter.supports_arena():
                return False
        except Exception:
            return False
        cap = self._arena_target_capacity()
        if cap <= 0:
            return False
        try:
            rc = int(adapter.reserve_arena(cap))
        except Exception:
            rc = 0
        if rc <= 0:
            if not getattr(self, "_arena_refused_logged", False):
                self._arena_refused_logged = True
                print(f"[GPU ARENA] native reserve refused capacity={cap:,} "
                      f"points; staying on the whole-buffer path", flush=True)
            return False
        self.arena = GpuArena(cap)
        self._arena_capacity = cap
        self._arena_epoch = int(getattr(self, "_arena_epoch", 0) or 0) + 1
        print(f"[GPU ARENA] capacity={cap:,} points "
              f"({ARENA_POSITION_BYTES_PER_POINT:.0f} B/point, "
              f"gpu_budget={int(self.gpu_budget) / 1e6:.0f} MB, reserve={rc}) "
              f"epoch={self._arena_epoch}", flush=True)
        return True
    def _arena_alloc(self, count: int) -> int:
        """Best-fit a stable slot, or ARENA_NONE. Never moves a live tile."""
        arena = getattr(self, "arena", None)
        if arena is None or int(count) <= 0:
            return ARENA_NONE
        slot = arena.alloc(int(count))
        return ARENA_NONE if slot is None else int(slot)
    def _arena_release_slot(self, v) -> None:
        """TRUE GPU eviction for one tile: its slot goes back to the pool.
        This is NOT draw retirement (Part 45). A tile that is merely no longer
        submitted keeps `GPU_RESIDENT` and keeps its slot; this runs only when
        the tile leaves the resident set, so a zoom-out re-activates a slot that
        is still exactly where it was.
        A tile the ARENA never allocated (the legacy packed path) is only marked
        un-resident: its offset is not ours to return.
        """
        if v is None:
            return
        owned = bool(getattr(v, "arena_slot_valid", False))
        arena = getattr(self, "arena", None)
        if owned and arena is not None \
                and int(getattr(v, "first", ARENA_NONE)) != ARENA_NONE:
            try:
                arena.free(int(v.first), int(v.count))
                self.gpu_evictions = int(
                    getattr(self, "gpu_evictions", 0) or 0) + 1
            except Exception:
                pass
            # Tell the ADAPTER too. The manager's pool and the adapter's own
            # tile table are two views of one allocation; freeing only ours left
            # the adapter holding a live record for the freed range, so it
            # rejected every later upload that reused it ("overlaps a live
            # tile") and the manager demoted the new tile straight away.
            try:
                self.adapter.arena_release(int(v.first), int(v.count))
            except Exception:
                pass
        v.first = ARENA_NONE
        v.arena_slot_valid = False
        v.gpu_first = ARENA_NONE
        v.gpu_attrs = None
    def _arena_evict_for(self, count: int) -> bool:
        """Free a contiguous slot by demoting LOW-VALUE residents only.
        Fragmentation is the one case where a bounded pool cannot host a tile it
        has room for in total. The answer is never "compact the pool" - that
        would move live slots and is exactly the repack this architecture
        removes. It is "drop the cheapest non-drawing tiles until the run fits".
        Anything in ACTIVE_DRAWN is protected, so this can never blank what the
        current frame is showing (Part 43/46).
        """
        arena = getattr(self, "arena", None)
        if arena is None:
            return False
        if arena.can_alloc(count):
            return True
        protect = set(getattr(self, "active_draw_keys", ()) or ())
        if self._streaming_frontier_enabled():
            # PHASE 5: the DESIRED frontier is protected too. Protecting only the
            # active set let a fragmented arena evict a desired tile that had
            # just been uploaded in order to place the next desired tile, which
            # is then evicted for the one after: thousands of evictions per
            # second and a frontier that never refined. Among the rest, a node's
            # coarsest rung (its fallback) goes last, then least recently used.
            protect |= set(getattr(self, "_frontier_protect", None) or ())
            coarsest = {}
            for k, v in self.resident.items():
                if bool(getattr(v, "arena_slot_valid", False)):
                    c = coarsest.get(k[0])
                    if c is None or k[1] > c:
                        coarsest[k[0]] = k[1]
            cands = [(1 if coarsest.get(k[0]) == k[1] else 0,
                      float(v.last_used), v)
                     for k, v in list(self.resident.items())
                     if bool(getattr(v, "arena_slot_valid", False))
                     and k not in protect and not getattr(v, "pinned", False)]
            cands.sort(key=lambda t: (t[0], t[1]))
            cands = [t[2] for t in cands]
        else:
            cands = [v for k, v in list(self.resident.items())
                     if bool(getattr(v, "arena_slot_valid", False))
                     and k not in protect
                     and not getattr(v, "pinned", False)]
            cands.sort(key=lambda v: (float(v.priority), float(v.last_used)))
        for v in cands:
            self._arena_release_slot(v)
            v.state = EVICTABLE
            self._release_gpu_bytes(v)
            self.evictions = int(getattr(self, "evictions", 0) or 0) + 1
            if arena.can_alloc(count):
                return True
        return arena.can_alloc(count)
    def _release_gpu_bytes(self, v) -> None:
        """Give a demoted tile's bytes back. Defensive: `gpu_bytes` may not exist
        on a manager built by `__new__` for a narrower test."""
        if v is None:
            return
        self.gpu_bytes = max(0, int(getattr(self, "gpu_bytes", 0) or 0)
                             - int(getattr(v, "bytes", 0) or 0))
    def _account_arena_attribute_coverage(self, keys) -> None:
        """Record how many ARENA-resident points have class/intensity on the GPU.
        Measured, never assumed: a tile only counts when its payload is present
        AND its length matches its point count (a short stream is not coverage),
        and only when the tile really holds an arena slot. Without this the
        arena path left _attr_class_points at 0, mode readiness could never
        report class as resident, and every class-dependent mode stayed PENDING
        on top of data that had already been uploaded (Phase 6C).
        """
        cls_pts = 0
        int_pts = 0
        for k in keys:
            v = self.resident.get(k)
            if v is None or v.state != GPU_RESIDENT:
                continue
            if not bool(getattr(v, "arena_slot_valid", False)):
                continue
            n = int(getattr(v, "count", 0) or 0)
            if n <= 0:
                continue
            cls = getattr(v, "cls", None)
            if cls is not None and int(np.asarray(cls).reshape(-1).size) == n:
                cls_pts += n
            inten = getattr(v, "inten", None)
            if inten is not None and int(np.asarray(inten).reshape(-1).size) == n:
                int_pts += n
        self._attr_class_points = int(cls_pts)
        self._attr_intensity_points = int(int_pts)
    def _arena_flush(self, keys) -> dict:
        """INCREMENTAL upload: only NEW or NEWLY-ATTRIBUTED tiles move.
        For an ordinary append - one block arriving while the rest stays resident
        - `existing_points_reuploaded` is 0 BY CONSTRUCTION: nothing here
        iterates over another tile's positions, concatenates anything, or
        re-sends a buffer. Each tile is addressed by the slot it already owns.
        """
        required = set(nkpc_attrs(self._preparation_attrs()))
        caps = getattr(self, "capabilities", None)
        if caps is not None:
            required &= set(caps.intersect(required))
        want = set()
        if ATTR_CLASSIFICATION in required:
            want.add("cls")
        if ATTR_INTENSITY in required:
            want.add("inten")
        if self._preparing_normals():
            want.add("normal")
        out = {"uploads_this_frame": 0, "arena": True,
               "new_blocks_uploaded": 0, "new_points_uploaded": 0,
               "existing_points_reuploaded": 0, "draw_ranges": 0,
               "resident_points": 0, "position_reuploads": 0}
        # ---- 1. EVICT what left the resident set, and only that ------------
        for v in list(self.resident.values()):
            if (v.state != GPU_RESIDENT
                    and bool(getattr(v, "arena_slot_valid", False))):
                self._arena_release_slot(v)
        # ---- 2. ALLOCATE + UPLOAD, one tile at a time ---------------------
        # PHASE 5 / PART 23 - GPU UPLOAD BUDGET (streaming frontier only). A burst
        # of arrivals must not turn into one frame that uploads them all: past the
        # budget the remaining tiles WAIT for the next frame, which always draws
        # the current frontier first. The first tile of a frame always goes
        # through (so progress is guaranteed), and a deferred tile owns no slot,
        # so the resident mask (`_sync_fast_mask`) does not offer it to the
        # selector - a deferral can delay detail but never create a hole.
        _ub = (self._upload_budget_points()
               if self._streaming_frontier_enabled() else None)
        _up_pts = 0
        out["deferred_uploads"] = 0
        for k in keys:
            v = self.resident.get(k)
            if v is None or int(v.count) <= 0 or v.xyz is None:
                continue
            if v.state != GPU_RESIDENT:
                continue
            if not bool(getattr(v, "arena_slot_valid", False)):
                if _ub is not None and _up_pts > 0 \
                        and _up_pts + int(v.count) > _ub:
                    out["deferred_uploads"] += 1
                    self.uploads_deferred = int(
                        getattr(self, "uploads_deferred", 0) or 0) + 1
                    continue
                _up_pts += int(v.count)
                slot = self._arena_alloc(int(v.count))
                if slot == ARENA_NONE and self._arena_evict_for(int(v.count)):
                    slot = self._arena_alloc(int(v.count))
                if slot == ARENA_NONE:
                    # No stable home for this tile. Keep it OUT of ACTIVE_DRAWN
                    # rather than advertise a slot the GPU never received; the
                    # coarse parent still covers its footprint (Part 43).
                    v.state = EVICTABLE
                    self._release_gpu_bytes(v)
                    continue
                v.first = int(slot)
                v.arena_slot_valid = True
                v.gpu_first = ARENA_NONE
                v.gpu_attrs = None
            held = set(getattr(v, "gpu_attrs", None) or ())
            fresh_position = int(getattr(v, "gpu_first", ARENA_NONE)) != int(v.first)
            missing = want - held
            if not fresh_position and not missing:
                continue
            # A stream is sent only when this tile actually HOLDS it. A short
            # stream is never sent: attributes are bound by point index, so a
            # partial one mis-colours everything after the gap.
            send_cls = ("cls" in want) and getattr(v, "cls", None) is not None
            send_int = ("inten" in want) and getattr(v, "inten", None) is not None
            send_nrm = ("normal" in want) and getattr(v, "normal", None) is not None
            try:
                ok = bool(self.adapter.upload_arena_tile(
                    int(v.first),
                    None if not fresh_position else v.xyz,
                    cls=(v.cls if send_cls else None),
                    inten=(v.inten if send_int else None),
                    normal=(v.normal if send_nrm else None)))
            except Exception as exc:                                    # noqa: BLE001
                ok = False
                self._set_attr_error(k, f"arena upload raised: {exc!r}")
            if not ok:
                if fresh_position:
                    # The slot holds nothing. Drop it back and keep the tile out
                    # of the draw set so nothing reads unwritten memory.
                    self._arena_release_slot(v)
                    v.state = EVICTABLE
                    self._release_gpu_bytes(v)
                continue
            if fresh_position:
                v.gpu_first = int(v.first)
                out["new_blocks_uploaded"] += 1
                out["new_points_uploaded"] += int(v.count)
                # getattr, not a bare +=: several callers (and several tests)
                # build the manager via __new__ with only the fields they
                # exercise, and a counter must never be the reason a frame throws.
                self.new_blocks_uploaded = int(
                    getattr(self, "new_blocks_uploaded", 0) or 0) + 1
                self.new_points_uploaded = int(
                    getattr(self, "new_points_uploaded", 0) or 0) + int(v.count)
            if send_cls:
                held.add("cls")
            if send_int:
                held.add("inten")
            if send_nrm:
                # PART 54: normals are streamed on demand, tile by tile, at the
                # SAME slot as the positions - so normal i IS point i by
                # construction (Part 53), with no second ordering step to drift.
                held.add("normal")
                self.normal_upload_count = int(
                    getattr(self, "normal_upload_count", 0) or 0) + 1
                if getattr(v, "normal_from_cache", False):
                    self.reused_normal_blocks = int(
                        getattr(self, "reused_normal_blocks", 0) or 0) + 1
                else:
                    self.new_normal_blocks = int(
                        getattr(self, "new_normal_blocks", 0) or 0) + 1
            v.gpu_attrs = frozenset(held)
            v.last_used = time.perf_counter()
            out["uploads_this_frame"] += 1
        out["resident_points"] = sum(int(self.resident[k].count) for k in keys)
        self._points_resident = int(out["resident_points"])
        # Normal coverage is derived from what each tile's slot actually HOLDS,
        # not accumulated: on this path a re-upload must be able to leave the
        # total unchanged. Keeping `normal_gpu_bytes == normal_resident_points *
        # 4` (oct16x2 is 4 B/point, never expanded) is the contract the shading
        # path and its telemetry rely on.
        norm_pts = sum(int(self.resident[k].count) for k in keys
                       if "normal" in (getattr(self.resident[k], "gpu_attrs",
                                               None) or ()))
        self.normal_resident_points = int(norm_pts)
        self.normal_gpu_bytes = int(norm_pts) * 4
        # GPU_RESIDENT total, published separately from `active_draw_points` so
        # the two can never be conflated again (Part 10).
        self.resident_points = int(out["resident_points"])
        # The legacy concatenated-stream signatures describe a buffer layout that
        # does not exist on this path. Clearing them keeps the two paths from
        # ever believing the other's upload is already resident.
        self._attr_class_sig = None
        self._attr_intensity_sig = None
        self._normal_sig = frozenset(keys)
        self._normal_reupload_needed = False
        self._attr_dirty = set()
        return out
    # ==================================================================== #
    # PART 16 - SCREEN-SPACE ACTIVE_DRAWN (FULL_RESIDENT).                  #
    #                                                                      #
    # FULL_RESIDENT means "the data STAYS in VRAM". It does NOT mean "draw   #
    # all of it". A 27M-point resident set seen from the top contains ~18    #
    # points per pixel; submitting every one of them costs frame time and     #
    # changes nothing on screen.                                                #
    #                                                                      #
    # This chooses ACTIVE_DRAWN from the SCREEN - bounded by VISIBLE          #
    # HIERARCHY NODES, never by the resident point count (Part 27) - and it   #
    # REUSES the existing ScreenSpaceLOD gate instead of inventing a second   #
    # hierarchy (Part 5). No upload happens on this path by construction:     #
    # every candidate is already GPU-resident.                                #
    # ==================================================================== #
    def _screen_space_authoritative(self) -> bool:
        """Whether the gate's answer IS the submission (default) or is advisory.
        The gate emits exactly one representative per visible node and already
        applies the interaction density budget, so it cannot overdraw a node
        against itself; re-filtering its answer by LOD only drops coverage it
        deliberately chose. The escape hatch exists to measure that claim rather
        than assert it.
        """
        return os.environ.get("NAKSHA_SCREEN_SPACE_GATE_AUTHORITATIVE",
                              "1").strip() not in ("0", "false", "False", "no")
    def _screen_space_applies(self) -> bool:
        """True when the frame's submission set comes from the SCREEN: either
        residency is complete (FULL_RESIDENT, every candidate LOD is hot) or the
        STREAMING frontier is active (one representation per node from whatever
        is resident, with a coarse fallback for what is not)."""
        return ((getattr(self, "render_mode", RENDER_STREAMING_LOD)
                 == RENDER_FULL_RESIDENT
                 and bool(getattr(self, "full_resident_ready", False)))
                or self._streaming_frontier_enabled())
    def _note_camera(self, camera, viewport) -> None:
        """Remember the latest camera. Cheap, and safe on every input event.
        Part 26: input events only STORE the camera. The expensive work runs at
        FRAME cadence in `on_frame`, not 300 times because Windows produced 300
        mouse events.
        """
        self._last_camera = camera
        self._last_vp = viewport
    def _resident_lod_nodes(self):
        """`idx.nodes` with every NON-RESIDENT LOD masked to point_count 0.
        `ScreenSpaceLOD.select()` treats a zero `lod_point_count` as "this LOD
        does not exist", so masking is all that is needed to make the EXISTING
        gate choose among representations that are ALREADY on the GPU. Nothing
        is reimplemented: the gate keeps its projected-box math, its
        importance ordering, its global point budget and its never-blank floor -
        only the candidate set shrinks to what is hot.
        Cached on the resident key set, which is exactly when it can change:
        refining, eviction or a mode switch rebuilds it; a camera move cannot.
        """
        keys = [(int(v.node_id), int(v.lod)) for v in self.resident.values()
                if v.state == GPU_RESIDENT]
        sig = frozenset(keys)
        cached_sig, cached = getattr(self, "_screen_nodes_cache", (None, None))
        if cached is not None and cached_sig == sig:
            return cached
        idx = getattr(self, "idx", None)
        if idx is None or not hasattr(idx, "nodes"):
            return None
        nodes = idx.nodes
        counts = np.asarray(nodes["lod_point_count"])
        if counts.ndim != 2 or counts.size == 0:
            self._screen_nodes_cache = (sig, nodes)
            return nodes
        n_lod = int(counts.shape[1])
        row_of = {int(n): i for i, n in enumerate(np.asarray(nodes["node_id"]))}
        mask = np.zeros(counts.shape, dtype=bool)
        for nid, lod in keys:
            i = row_of.get(nid)
            if i is not None and 0 <= lod < n_lod:
                mask[i, lod] = True
        out = nodes.copy()
        out["lod_point_count"] = np.where(mask, counts, 0).astype(counts.dtype)
        self._screen_nodes_cache = (sig, out)
        return out
    # ------------------------------------------------------------------ #
    # PHASE 4 - compiled screen-space selection (2D orthographic).        #
    # ------------------------------------------------------------------ #
    def _init_fast_lod(self) -> None:
        self._lod_fast = None
        self._lod_window = LodReuseWindow()
        self.lod_stats = {"selector_runs": 0, "exact_hits": 0,
                          "window_hits": 0, "kernel_ms_sum": 0.0,
                          "verify_mismatches": 0, "raw_callbacks": 0,
                          "frames": 0, "warmup": None}
        # DEV A/B: NAKSHA_CAMERA_FASTPATH=0 restores the legacy per-frame
        # `_flush_gpu` + per-frame telemetry sample (baseline measurements).
        self._camera_fastpath = os.environ.get(
            "NAKSHA_CAMERA_FASTPATH", "1").strip() not in ("0", "false", "no")
        mode = kernel_mode()
        self._lod_kernel_mode = mode
        self.cache_layout = {}          # set by _report_cache_layout (metadata)
        self.cache_layout_notice = None  # user-facing text, or None when current
        if mode == "python":
            return
        try:
            fs = FastLodSelector(self.idx.nodes)
            self.lod_stats["warmup"] = fs.warmup()
            self._lod_fast = fs
            self._report_cache_layout(fs)
        except Exception as exc:                                    # noqa: BLE001
            print(f"[LOD KERNEL] unavailable ({exc!r}); python reference used")
            self._lod_fast = None
    # ------------------------------------------------------------------ #
    # PHASE 5 - STREAMING FRONTIER (the STREAMING_LOD path, i.e. any       #
    # dataset too large to be FULL_RESIDENT: the billion-point case).       #
    #                                                                      #
    #   DESIRED frontier : uniform allocator over ALL rungs. What to       #
    #                      request. Independent of what is resident.       #
    #   ACTIVE  frontier : the same allocator over RESIDENT rungs (+        #
    #                      coherence hold-back). What to draw.             #
    #   One representation per node is drawn, so a replaced coarse rung     #
    #   stays GPU-resident as a free fallback but is never double-drawn.    #
    # ------------------------------------------------------------------ #
    def _streaming_frontier_enabled(self) -> bool:
        if getattr(self, "render_mode", None) != RENDER_STREAMING_LOD:
            return False
        if getattr(self, "_lod_fast", None) is None:
            return False
        if type(getattr(self, "resident", None)) is not _GenDict:
            return False
        return os.environ.get("NAKSHA_STREAMING_FRONTIER", "1").strip().lower() \
            not in ("0", "false", "no")
    def _sync_fast_mask(self) -> None:
        """Rebuild the resident-rung mask when residency changed.
        In ARENA mode a tile counts as available only once it OWNS a slot (its
        positions are on the GPU). A GPU_RESIDENT tile whose upload was deferred
        by the per-frame upload budget has no slot yet: selecting it would put a
        key in ACTIVE that `_build_ranges` must then skip - a hole."""
        fs = self._lod_fast
        gen = _RES_GEN[0]
        if fs.mask_generation != gen:
            need_slot = bool(self._arena_active())
            fs.set_resident(
                [(int(v.node_id), int(v.lod)) for v in self.resident.values()
                 if v.state == GPU_RESIDENT
                 and (not need_slot or bool(getattr(v, "arena_slot_valid",
                                                    False)))], gen)
            self._lod_window.invalidate()
    def _frontier_specs(self, camera, viewport, interaction: bool):
        """-> (specs, n_visible, selected_points) or None if not applicable.
        Request priority classes (Part 15), highest first:
          4e9  coarsest rung of a VISIBLE node that has NOTHING resident
               (missing coverage - never delayed by any refinement)
          3e9  the desired rung of a visible node (replacement / refinement)
          1e9  the same for the margin ring around the viewport
        each plus up to 1e6 for closeness to the view centre.
        """
        try:
            from naksha_lod_gate import Camera2D
        except Exception:
            return None
        if not isinstance(camera, Camera2D) or viewport is None:
            return None
        fs = self._lod_fast
        self._sync_fast_mask()
        lod = self.lod
        budget = self._frontier_budget(int(getattr(lod, "budget", 0) or 0))
        err_t = float(getattr(lod, "error_px", 0.0) or 0.0)
        vcx, vcy, hw, hh = (float(viewport[0]), float(viewport[1]),
                            float(viewport[2]), float(viewport[3]))
        ppm = float(camera.px_per_m())
        view = (vcx - hw, vcx + hw, vcy - hh, vcy + hh)
        m = self._fast_pan_margin(interaction)
        sel = (view[0] - 2.0 * hw * m, view[1] + 2.0 * hw * m,
               view[2] - 2.0 * hh * m, view[3] + 2.0 * hh * m)
        n_out, n_vis, _lim, arrs = fs.select_uniform(
            sel, vcx, vcy, ppm, err_t, budget, masked=fs.full_counts,
            hysteresis=self._uniform_hysteresis(), state=1)
        if n_out == 0:
            return [], int(n_vis), 0
        row = arrs[0].copy()
        lodv = arrs[1].copy()
        pts = arrs[2].copy()
        perr = arrs[4].copy()
        dist = arrs[5].copy()
        soa = fs.soa
        nid = soa.node_id[row]
        in_view = ((soa.bmax_x[row] >= view[0]) & (soa.bmin_x[row] <= view[1])
                   & (soa.bmax_y[row] >= view[2]) & (soa.bmin_y[row] <= view[3]))
        covered = (fs._masked[row] > 0).any(axis=1)
        close = 1.0 - np.minimum(dist / max(float(dist.max()), 1.0), 1.0)
        specs = []
        for i in range(n_out):
            specs.append(TileSpec(
                node_id=int(nid[i]), lod=int(lodv[i]),
                priority=float((3e9 if in_view[i] else 1e9) + 1e6 * close[i]),
                points=int(pts[i]), px_err=float(perr[i]), row=int(row[i])))
        unc = np.flatnonzero((~covered) & in_view)
        if unc.size:
            full = soa.lod_count[row[unc]]
            coarsest = (full.shape[1] - 1) - np.argmax(
                (full > 0)[:, ::-1], axis=1)
            for q, l in zip(unc.tolist(), coarsest.tolist()):
                specs.append(TileSpec(
                    node_id=int(nid[q]), lod=int(l),
                    priority=float(4e9 + 1e6 * close[q]),
                    points=int(soa.lod_count[row[q], l]), px_err=0.0,
                    row=int(row[q])))
        specs.sort(key=lambda s: -s.priority)
        return specs, int(n_vis), int(pts.sum())
    def _upload_budget_points(self) -> int:
        """Position points one frame may upload (streaming frontier).
        MOVING keeps it small so camera frames stay cheap; IDLE allows more so
        refinement completes quickly. Env: NAKSHA_UPLOAD_BUDGET_MOVING /
        NAKSHA_UPLOAD_BUDGET_IDLE (points; defaults 262,144 / 1,048,576)."""
        moving = getattr(self, "_camera_state", "MOVING") != "IDLE"
        name = ("NAKSHA_UPLOAD_BUDGET_MOVING" if moving
                else "NAKSHA_UPLOAD_BUDGET_IDLE")
        default = 262_144 if moving else 1_048_576
        try:
            return max(1, int(os.environ.get(name, str(default))))
        except ValueError:
            return default
    def _frontier_budget(self, density_budget: int) -> int:
        """Point budget for the streaming frontier: the screen-density target,
        CAPPED by what the GPU arena can hold with headroom.
        A frontier larger than the arena can never be fully resident, so every
        frame would evict tiles the next frame re-requests (thrash) - measured:
        a 1 M-point idle target against a 144 K-point arena demoted 103 tiles in
        14 frames and ended with 2 resident. Half the arena is left for the
        coarse fallbacks, hysteresis and margin tiles."""
        arena = getattr(self, "arena", None)
        cap_pts = getattr(arena, "capacity", None)
        if not cap_pts:
            return int(density_budget)
        return int(min(int(density_budget), max(1, int(cap_pts) // 2)))
    def _max_inflight(self) -> int:
        """Reads allowed in flight at once (default 16; NAKSHA_STREAM_MAX_INFLIGHT)."""
        try:
            return max(1, int(os.environ.get("NAKSHA_STREAM_MAX_INFLIGHT", "16")))
        except ValueError:
            return 16
    def _uncovered_in_view(self, view) -> int:
        """Visible leaf nodes (real viewport, no margin) with NO resident rung.
        One vector pass over the SoA; only runs when a selection is derived."""
        fs = self._lod_fast
        soa = fs.soa
        vis = ((soa.bmax_x >= view[0]) & (soa.bmin_x <= view[1])
               & (soa.bmax_y >= view[2]) & (soa.bmin_y <= view[3]))
        has_any = (soa.lod_count > 0).any(axis=1)
        has_res = (fs._masked > 0).any(axis=1)
        return int((vis & has_any & ~has_res).sum())
    def _evict_cold_for_bytes(self, need: int) -> bool:
        """Make `need` GPU bytes of room by demoting COLD tiles (Part 12).
        Never evicted: anything in ACTIVE_DRAWN or in the current DESIRED
        frontier, pinned tiles, the overview. Among the rest, a tile that is
        NOT its node's coarsest resident rung goes first (the coarse rung is the
        node's fallback, kept longest - the zoom-out / pan-back reactivation
        costs 0 disk reads), then least recently used. Demoted tiles keep their
        arrays for revival and are bounded by `_trim_evictable`.
        """
        if not self._streaming_frontier_enabled():
            return False
        free = int(self.gpu_budget) - int(self.gpu_bytes)
        if free >= need:
            return True
        cands = getattr(self, "_evict_cands", None)
        if cands is None:
            protect = getattr(self, "_frontier_protect", None) or set()
            ov = self._overview_key()
            coarsest = {}
            for k, v in self.resident.items():
                if v.state == GPU_RESIDENT:
                    c = coarsest.get(k[0])
                    if c is None or k[1] > c:
                        coarsest[k[0]] = k[1]
            cands = []
            for k, v in self.resident.items():
                if (v.state != GPU_RESIDENT or k in protect or k == ov
                        or getattr(v, "pinned", False)):
                    continue
                cands.append((1 if coarsest.get(k[0]) == k[1] else 0,
                              float(v.last_used), k))
            cands.sort()
            self._evict_cands = cands
        while cands and free < need:
            _fb, _lu, k = cands.pop(0)
            v = self.resident.get(k)
            if v is None or v.state != GPU_RESIDENT:
                continue
            v.state = EVICTABLE
            self.gpu_bytes -= v.bytes
            self.evictions = int(getattr(self, "evictions", 0) or 0) + 1
            free += int(v.bytes)
        return free >= need
    def _trim_evictable(self) -> int:
        """Bound the RAM held by demoted tiles (they keep their arrays so a
        zoom-out / pan-back can revive them with no disk read). Beyond half the
        RAM budget the least recently used are DROPPED from `resident`; their
        `ram_cache` entry (itself LRU-bounded) can still revive them, otherwise
        they are simply re-read. Without this, a long pan over a huge dataset
        would grow memory with everything ever loaded."""
        cap = int(self.ram_budget) // 2
        ev = [(float(v.last_used), k, int(v.bytes))
              for k, v in self.resident.items() if v.state == EVICTABLE]
        total = sum(b for _t, _k, b in ev)
        if total <= cap:
            return 0
        ev.sort()
        dropped = 0
        for _t, k, b in ev:
            if total <= cap:
                break
            v = self.resident.pop(k, None)
            if v is not None:
                if getattr(v, "arena_slot_valid", False):
                    self._arena_release_slot(v)
                total -= b
                dropped += 1
        self.trimmed_tiles = int(getattr(self, "trimmed_tiles", 0) or 0) + dropped
        return dropped
    def _hold_rungs(self) -> int:
        """Most rungs a node may be HELD BACK behind its lagging neighbours
        (streaming). Unbounded hold-back cascades across the whole view; see
        `_coherence_kernel`. Env: NAKSHA_STREAMING_HOLD_RUNGS (default 2)."""
        try:
            return int(os.environ.get("NAKSHA_STREAMING_HOLD_RUNGS", "2"))
        except ValueError:
            return 2
    def _uniform_hysteresis(self) -> float:
        """Refine/coarsen dead-band of the uniform allocator (fraction of S).
        0 disables. Env: NAKSHA_LOD_UNIFORM_HYST (default 0.15)."""
        try:
            return max(0.0, float(os.environ.get("NAKSHA_LOD_UNIFORM_HYST",
                                                 "0.15")))
        except ValueError:
            return 0.15
    def _lod_allocator(self) -> str:
        """`share` = the accepted per-node area-share allocator (reference
        parity, kept recoverable); `uniform` = one global apparent-spacing
        threshold for the whole frame. NAKSHA_LOD_ALLOCATOR selects."""
        v = os.environ.get("NAKSHA_LOD_ALLOCATOR", "uniform").strip().lower()
        return "share" if v == "share" else "uniform"
    def _coherence_limit(self, interaction: bool) -> float:
        """Screen-space density coherence limit (apparent-spacing ratio).
        MOVING favours a stable, uniform frontier (default 2.0 = at most a 2x
        apparent-density step across a boundary); IDLE keeps full detail
        (default off). 0 disables. Env: NAKSHA_LOD_COHERENCE_MOVING / _IDLE."""
        name = ("NAKSHA_LOD_COHERENCE_MOVING" if interaction
                else "NAKSHA_LOD_COHERENCE_IDLE")
        try:
            return max(0.0, float(os.environ.get(
                name, "2.0" if interaction else "0")))
        except ValueError:
            return 0.0
    def _report_cache_layout(self, fs) -> None:
        """Classify the cache's SPATIAL layout once at open (metadata only).
        Two ways a cache can be the WRONG cache for the current selector:
          * a legacy layout (index v1 / pre-Morton-fix) has overlapping leaves of
            very different sizes. Tile seams then come from the DATA LAYOUT, not
            from the selector, and only a rebuild with the current builder
            removes them.
          * a cache built with a coarser rung ladder cannot satisfy the
            configured screen-space coherence limit at all, so the frontier at
            any moment is a mosaic of rungs. The limit was tuned on the current
            ladder, so it is not doing what it says on that cache.
        Either way it is said plainly and once - in the status bar (the shipped
        GUI has no console) as well as on stdout - instead of rendering badly in
        silence. Never raises: a diagnostic must not be able to fail an open.
        """
        from .lod_kernel import leaf_layout_report
        rep = leaf_layout_report(fs.soa, coherence_limit=self._coherence_limit(True))
        rep["lod_algo_version"] = int(getattr(self.idx, "lod_algo_version", 0)
                                      or 0)
        self.cache_layout = rep
        self.cache_layout_notice = None
        why = []
        if rep["legacy_layout"]:
            why.append(f"overlapping leaves (overlap_ratio="
                       f"{rep['overlap_ratio']:.2f}, "
                       f"{rep['wide_fraction'] * 100:.0f}% of leaves >3x the "
                       f"median width)")
        if rep["lod_algo_version"] < 2:
            why.append(f"old LOD ladder (algo v{rep['lod_algo_version']}: "
                       f"~23x LOD0->LOD1 step)")
        elif rep["stale_ladder"]:
            why.append(f"coarse LOD ladder (rung spacing step "
                       f"{rep['ladder_spacing_step']:.2f}x > the coherence "
                       f"limit {rep['coherence_limit']:.2f}x)")
        if not why:
            return
        print("[CACHE LAYOUT] this cache is out of date for the current "
              "selector - rectangular density patches while panning/zooming "
              "come from the CACHE, not the renderer: " + "; ".join(why) +
              ". Rebuild this dataset with the current builder.", flush=True)
        self.cache_layout_notice = (
            "CACHE OUT OF DATE: " + "; ".join(why) +
            ". Density patches while panning/zooming come from the cache "
            "itself, not the renderer - rebuild this dataset.")
        self._notify_open(self.cache_layout_notice)
    def _notify_open(self, message: str, ms: int = 15000) -> None:
        """One-off open-time notice in the SHIPPED GUI, through the status bar
        the rest of the app already uses (a `print` reaches nobody when the
        packaged build has no console). Silent in headless harnesses, and never
        raises."""
        try:
            app = getattr(self, "app", None)
            bar = app.statusBar() if app is not None else None
            if bar is not None:
                bar.showMessage(str(message), int(ms))
        except Exception:                                            # noqa: BLE001
            pass
    def _fast_pan_margin(self, interaction: bool) -> float:
        """Fraction of the view added on each side while MOVING (Part 22)."""
        if not interaction:
            return 0.0          # IDLE keeps the exact, accepted Phase-3 view
        try:
            return max(0.0, min(0.25, float(os.environ.get(
                "NAKSHA_LOD_PAN_MARGIN", "0.06"))))
        except ValueError:
            return 0.06
    def _fast_screen_space_keys(self, interaction, cam, vp):
        """(keys, diag) | (None, reason) | NotImplemented (not applicable)."""
        fs = getattr(self, "_lod_fast", None)
        if fs is None or type(self.resident) is not _GenDict:
            return NotImplemented
        try:
            from naksha_lod_gate import Camera2D
        except Exception:
            return NotImplemented
        if not isinstance(cam, Camera2D):
            return NotImplemented               # 3D keeps its own projection
        gen = _RES_GEN[0]
        if fs.mask_generation != gen:
            self._sync_fast_mask()
        lod = self.lod
        streaming = self._streaming_frontier_enabled()
        budget = int(getattr(lod, "budget", 0) or 0)
        if streaming:
            budget = self._frontier_budget(budget)
        err_t = float(getattr(lod, "error_px", 0.0) or 0.0)
        vcx, vcy, hw, hh = (float(vp[0]), float(vp[1]), float(vp[2]),
                            float(vp[3]))
        ppm = float(cam.px_per_m())
        st = self.lod_stats
        coh = self._coherence_limit(interaction)
        if streaming:
            # Hold back an early-arriving fine tile whose neighbours are still
            # coarse. The limit MUST sit above the ladder's own rung step: the
            # uniform allocator already leaves neighbours up to ~2.3-2.6x apart
            # in apparent spacing (one 4x-count rung = 2x spacing), so a limit of
            # 2.0 fired everywhere and cascaded the whole view to the coarsest
            # rung. 3.0 triggers only on genuine residency lag.
            try:
                coh = max(coh, float(os.environ.get(
                    "NAKSHA_STREAMING_COHERENCE", "2.3")))
            except ValueError:
                coh = max(coh, 2.3)
        alloc = self._lod_allocator()
        key = (bool(interaction), int(self._vp_w), int(self._vp_h), gen,
               budget, round(err_t, 6), round(coh, 4), alloc)
        view = (vcx - hw, vcx + hw, vcy - hh, vcy + hh)
        win = self._lod_window
        if (self._active_screen_keys_ok()
                and win.covers(key, view, ppm)):
            st["window_hits"] += 1
            self.screen_space_cached_hits = int(
                getattr(self, "screen_space_cached_hits", 0) or 0) + 1
            return (list(self._active_screen_keys),
                    dict(getattr(self, "last_screen_space_diag", None) or {}))
        m = self._fast_pan_margin(interaction)
        sel = (view[0] - 2.0 * hw * m, view[1] + 2.0 * hw * m,
               view[2] - 2.0 * hh * m, view[3] + 2.0 * hh * m)
        t = time.perf_counter()
        if alloc == "uniform":
            _des = None
            if streaming:
                # The DESIRED rung of each node (all rungs available): hold-back
                # only reacts to neighbours that are LAGGING behind it.
                _dn, _dv, _dl, _da = fs.select_uniform(
                    sel, vcx, vcy, ppm, err_t, budget, masked=fs.full_counts,
                    hysteresis=self._uniform_hysteresis(), state=1)
                _des = (_da[0].copy(), _da[1].copy())
            n_out, n_vis, _over, arrs = fs.select_uniform(
                sel, vcx, vcy, ppm, err_t, budget,
                coherence=(coh if streaming else 0.0), desired=_des,
                hysteresis=self._uniform_hysteresis(), state=0,
                max_hold=self._hold_rungs())
        else:
            n_out, n_vis, _over, arrs = fs.select(
                sel, vcx, vcy, ppm, err_t, float(lod.hysteresis), budget,
                coherence=coh)
        _k_ms = (time.perf_counter() - t) * 1000.0
        st["kernel_ms_sum"] += _k_ms
        st["selector_runs"] += 1
        _m = _pm()
        if _m is not None:
            _m.hook_extra(_m.RecordType.SELECTOR,
                          int(getattr(self, "camera_generation", -1) or -1),
                          a0=_k_ms, a1=1)
        if n_vis == 0:
            return None, "no_visible_nodes"
        if self._lod_kernel_mode == "verify" and alloc == "share" and coh <= 1.0:
            self._verify_fast_select(cam, sel, n_out, arrs)
        nid = fs.soa.node_id
        row, lod_a, pts = arrs[0], arrs[1], arrs[2]
        keys = []
        res = self.resident
        for i in range(n_out):
            k = (int(nid[row[i]]), int(lod_a[i]))
            v = res.get(k)
            if v is None or v.state != GPU_RESIDENT or int(v.count) <= 0:
                continue
            keys.append(k)
        if len(keys) != n_vis:
            if not streaming:
                self.screen_space_uncovered = int(n_vis - len(keys))
                return None, f"uncovered_visible_nodes={n_vis - len(keys)}"
            # STREAMING: a visible node with NOTHING resident is expected while
            # its coarse rung is in flight. It is requested first (top priority)
            # and, until it lands, the overview covers it - drawn ONLY while
            # such a node exists, so a fully covered view never double-draws it.
            n_unc = self._uncovered_in_view(view)
            self.screen_space_uncovered = int(n_unc)
            ov = self._overview_key()
            ovt = res.get(ov) if ov is not None else None
            if n_unc > 0 and ovt is not None and ovt.state == GPU_RESIDENT \
                    and int(ovt.count) > 0 and ov not in keys:
                keys.append(ov)
            if not keys:
                return None, "streaming_nothing_resident"
        else:
            self.screen_space_uncovered = 0
        if streaming:
            st["streaming_selections"] = st.get("streaming_selections", 0) + 1
        self.screen_space_uncovered = int(getattr(
            self, "screen_space_uncovered", 0) or 0)
        self.screen_space_selections = int(
            getattr(self, "screen_space_selections", 0) or 0) + 1
        total_pts = int(pts.sum()) if n_out else 0
        try:
            d = self.density
            d.last_candidate = d.candidate_points = total_pts
            d.note_selected(total_pts)
        except Exception:
            pass
        # The reference path's exact-signature cache must never serve an answer
        # this path produced (different resident key), so it is cleared here.
        self._active_screen_sig = None
        self._active_screen_keys = list(keys)
        win.store(key, sel, ppm)
        diag = {"visible_nodes": int(n_vis), "gate_blocks": len(keys),
                "gate_points": int(sum(int(res[k].count) for k in keys)),
                "target_points": int(getattr(self.density, "last_target", 0)
                                     or 0),
                "interaction": bool(interaction)}
        self.last_screen_space_diag = diag
        return keys, diag
    def _fs(self) -> dict:
        """Fast-path counters (created lazily: partial fixtures skip __init__)."""
        d = getattr(self, "fast_stats", None)
        if d is None:
            d = self.fast_stats = {
                "fast_frames": 0, "slow_frames": 0, "slow_reasons": {},
                "safety_recheck_corrections": 0, "camera_range_builds": 0,
                "membership_pushes": 0, "unnecessary_pushes": 0,
                "active_generation": 0}
        return d
    def _render_sig(self) -> tuple:
        """Everything outside the tile table that decides what a flush would do
        for the CURRENT mode: the mode, whether normals are wanted, the dataset
        capabilities that gate attribute requirements."""
        return (self.display_mode, bool(getattr(self, "shaded_mode", False)),
                id(getattr(self, "capabilities", None)))
    def _fast_flush_reason(self):
        """None when a camera-only frame is provably equivalent to a full
        flush, else the FIRST reason it is not (for telemetry and tests).
        Every item is a synchronous check: nothing here waits for the 0.5 s
        safety re-check, which is a backstop that must find nothing."""
        ff = getattr(self, "_ff", None)
        if ff is None:
            return "no_snapshot"
        if type(getattr(self, "resident", None)) is not _GenDict:
            return "untracked_resident"
        if not getattr(self, "_camera_fastpath", True):
            return "disabled"
        try:
            if ff["gen"] != _RES_GEN[0]:
                return "residency_or_attribute_generation"
            if ff["mgen"] != _MODE_GEN[0]:
                return "mode_or_lut_generation"
            if ff["rsig"] != self._render_sig():
                return "render_signature"
            if self._display_reupload_needed:
                return "display_reupload_needed"
            if self._normal_reupload_needed:
                return "normal_reupload_needed"
            if self._attr_dirty:
                return "attribute_dirty"
            if self._pending:
                return "reads_pending"
            if getattr(self, "_adapter_mode", None) != self.display_mode:
                return "adapter_mode_unsynced"
            if self._last_draw_sig is None:
                return "no_draw_sig"
            q = getattr(self, "_attr_done", None)
            if q is not None and not q.empty():
                return "attribute_arrived"
            if (time.perf_counter() - ff["t"]) >= FAST_FLUSH_REFRESH_S:
                return "refresh"
        except AttributeError:
            return "partial_fixture"
        return None
    def _fast_flush_eligible(self) -> bool:
        return self._fast_flush_reason() is None
    def _record_fast_flush_state(self, info=None, reason=None) -> None:
        """Called right after a FULL `_flush_gpu`: residency is now settled.
        `reason` is why the full flush ran. When it was ONLY the periodic
        refresh and that flush still had to upload something, an event that
        should have invalidated the fast path earlier was missed: that is a
        `safety_recheck_correction` and it must stay 0."""
        st = self._fs()
        st["slow_frames"] += 1
        if reason:
            st["slow_reasons"][reason] = st["slow_reasons"].get(reason, 0) + 1
        if reason == "refresh" and info:
            if (int(info.get("uploads_this_frame", 0) or 0) > 0
                    or int(info.get("new_blocks_uploaded", 0) or 0) > 0):
                st["safety_recheck_corrections"] += 1
                _m = _pm()
                if _m is not None:
                    _m.hook_extra(_m.RecordType.SAFETY_CORRECTION,
                                  int(getattr(self, "camera_generation", -1)
                                      or -1))
                print(f"[FAST PATH] safety re-check CORRECTED a missed "
                      f"invalidation: {info}")
        if type(getattr(self, "resident", None)) is not _GenDict:
            self._ff = None
            return
        self._ff = {"gen": _RES_GEN[0], "mgen": _MODE_GEN[0],
                    "rsig": self._render_sig(), "mode": self.display_mode,
                    "t": time.perf_counter(),
                    "keys": [k for k, v in self.resident.items()
                             if v.state == GPU_RESIDENT]}
    def _camera_fast_flush(self, interaction: bool) -> None:
        st = self._fs()
        st["fast_frames"] += 1
        st["camera_range_builds"] += 1
        _m = _pm()
        if _m is not None:
            _m.hook_extra(_m.RecordType.FAST_PATH,
                          int(getattr(self, "camera_generation", -1) or -1))
        before = tuple(getattr(self, "active_draw_keys", ()) or ())
        ranges = self._build_ranges(
            self._ff["keys"], interaction=interaction, screen_space=True,
            camera=getattr(self, "_last_camera", None),
            viewport=getattr(self, "_last_vp", None))
        sig = tuple(ranges)
        if sig != getattr(self, "_last_draw_sig", None):
            # PART 38: ranges are re-submitted only when ACTIVE membership
            # changed; an exact-camera change alone is a camera update only.
            if tuple(self.active_draw_keys or ()) == before:
                st["unnecessary_pushes"] += 1     # same members, new ranges
            st["membership_pushes"] += 1
            st["active_generation"] += 1
            self._last_draw_sig = sig
            self.adapter.set_draw_ranges(ranges)
            self.draw_range_pushes = int(
                getattr(self, "draw_range_pushes", 0) or 0) + 1
            if _m is not None:
                _m.hook_extra(_m.RecordType.RANGE_PUSH,
                              int(getattr(self, "camera_generation", -1) or -1),
                              a0=len(ranges))
        self.fast_flush_frames = int(
            getattr(self, "fast_flush_frames", 0) or 0) + 1
    def _active_screen_keys_ok(self) -> bool:
        return getattr(self, "_active_screen_keys", None) is not None
    def _verify_fast_select(self, cam, sel, n_out, arrs) -> None:
        """DEV A/B (NAKSHA_LOD_KERNEL=verify): reference vs compiled."""
        nodes = self._resident_lod_nodes()
        if nodes is None:
            return
        n = self.idx.nodes
        bmin = np.asarray(n["bounds_min"], dtype=np.float64)
        bmax = np.asarray(n["bounds_max"], dtype=np.float64)
        m = ((bmax[:, 0] >= sel[0]) & (bmin[:, 0] <= sel[1]) &
             (bmax[:, 1] >= sel[2]) & (bmin[:, 1] <= sel[3]))
        ref = self._ref_lod.select(_ResidentIndexView(nodes), cam,
                                   np.arange(n.size)[m])
        fast = self._lod_fast
        got = {int(fast.soa.node_id[arrs[0][i]]): (int(arrs[1][i]),
                                                   int(arrs[2][i]))
               for i in range(n_out)}
        exp = {int(o["node_id"]): (int(o["lod"]), int(o["points"]))
               for o in ref}
        if got != exp:
            self.lod_stats["verify_mismatches"] += 1
    @property
    def _ref_lod(self):
        r = getattr(self, "_ref_lod_obj", None)
        if r is None:
            from naksha_lod_gate import ScreenSpaceLOD
            r = self._ref_lod_obj = ScreenSpaceLOD()
        r.budget = self.lod.budget
        r.moving = self.lod.moving
        return r
    def _screen_space_active_keys(self, interaction: bool, camera=None,
                                  viewport=None):
        """(keys, diag) for a screen-space ACTIVE_DRAWN, or (None, reason).
        Returns None whenever the answer cannot be shown to cover the screen: no
        camera yet, no visible nodes, or a visible node with NO resident
        representation. The caller then keeps the previous behaviour, so this can
        never introduce a hole (Part 43).
        The camera is an ARGUMENT, not hidden state read from `_last_camera`. A
        caller holding a definite camera must be able to say which view it is
        selecting for; otherwise a call made between `_note_camera` and the next
        frame silently degrades to the legacy "draw the residency" rule while
        still reporting the screen-space path as active - the exact confusion
        Part 87 exists to catch.
        """
        cam = camera if camera is not None else getattr(self, "_last_camera", None)
        vp = viewport if viewport is not None else getattr(self, "_last_vp", None)
        if cam is None or vp is None:
            return None, "no_camera_yet"
        # Push the ONE density budget into the gate BEFORE selecting, so the
        # gate's own budget pass is what limits the submission (Part 6/31).
        try:
            self.density.apply(self.lod, self._vp_w, self._vp_h,
                               moving=bool(interaction))
        except Exception:
            pass
        # PHASE 4: compiled SoA selector for the orthographic plan view. Returns
        # NotImplemented when it does not apply (3D, python mode, partial
        # fixture) so the reference path below stays the single fallback.
        fast = self._fast_screen_space_keys(interaction, cam, vp)
        if fast is not NotImplemented:
            return fast
        nodes = self._resident_lod_nodes()
        if nodes is None:
            return None, "no_index"
        # ---- PART 26/27 - THE SELECTION IS A PURE FUNCTION OF THE VIEW --------
        # It depends on the camera, the resident LOD set and the interaction
        # state - and on nothing else. A settled camera therefore re-derived a
        # byte-identical answer on every frame at 5-11 ms per call, which is
        # spent ON the camera hot path. `_camera_signature` is the existing
        # rounded, stable view identity, so caching on it cannot miss a real
        # camera change; the resident set comes straight out of
        # `_resident_lod_nodes`'s own cache key rather than being rebuilt.
        resident_sig = getattr(self, "_screen_nodes_cache", (None, None))[0]
        # The BUDGET and the ERROR TARGET are part of the answer, not just the
        # view: an adaptive controller (Part 30/31) lowers the budget without the
        # camera moving, and a cached selection would then keep submitting the
        # old set until the user happened to pan. `apply()` above has already
        # pushed this frame's values, so those are the ones being keyed on.
        sig = (self._camera_signature(cam, vp), bool(interaction),
               int(self._vp_w), int(self._vp_h), resident_sig,
               int(getattr(self.lod, "budget", 0) or 0),
               round(float(getattr(self.lod, "error_px", 0.0) or 0.0), 6))
        if (sig == getattr(self, "_active_screen_sig", None)
                and getattr(self, "_active_screen_keys", None) is not None):
            self.screen_space_cached_hits = int(
                getattr(self, "screen_space_cached_hits", 0) or 0) + 1
            return (list(self._active_screen_keys),
                    dict(getattr(self, "last_screen_space_diag", None) or {}))
        rows = self._visible_node_rows(cam, vp)
        if len(rows) == 0:
            return None, "no_visible_nodes"
        _t_ref = time.perf_counter()
        try:
            sel = self.lod.select(_ResidentIndexView(nodes), cam, rows)
        except Exception as exc:                                    # noqa: BLE001
            return None, f"select_failed:{exc!r}"
        _m = _pm()
        if _m is not None:
            _m.hook_extra(_m.RecordType.SELECTOR,
                          int(getattr(self, "camera_generation", -1) or -1),
                          a0=(time.perf_counter() - _t_ref) * 1000.0, a1=0)
        keys = []
        for o in sel:
            k = (int(o["node_id"]), int(o["lod"]))
            v = self.resident.get(k)
            if v is None or v.state != GPU_RESIDENT or int(v.count) <= 0:
                continue
            keys.append(k)
        # COVERAGE CHECK. The gate emits one entry per visible node that has any
        # resident LOD, so an unrepresented visible node means that node has
        # nothing on the GPU - the one case where this selection is refused.
        chosen = {k[0] for k in keys}
        uncoverable = [int(nodes["node_id"][r]) for r in rows
                       if int(nodes["node_id"][r]) not in chosen]
        if uncoverable:
            self.screen_space_uncovered = len(uncoverable)
            return None, f"uncovered_visible_nodes={len(uncoverable)}"
        self.screen_space_uncovered = 0
        self.screen_space_selections = int(
            getattr(self, "screen_space_selections", 0) or 0) + 1
        # PART 12 - SELECTED vs SUBMITTED. The density controller's counters are
        # what the GUI tick prints, and on the FULL_RESIDENT screen-space path
        # nothing was feeding them, so every production line read
        # `selected_points=0` while `submitted_points` was correct. Reported as
        # "no selection happened" when a selection HAD happened: exactly the
        # confusion Part 12 exists to remove.
        try:
            self.density.note_candidates(sel)
            self.density.note_selected(int(sum(int(o["points"]) for o in sel)))
        except Exception:
            pass
        self._active_screen_sig = sig
        self._active_screen_keys = list(keys)
        # `gate_*` is what the gate PROPOSED. It is deliberately not called
        # `active_*`: `_build_ranges` may still apply a legacy rule on top, and
        # reporting a proposal as the submission is how a 230-block selection can
        # be recorded as a 35-block frame.
        diag = {"visible_nodes": len(rows), "gate_blocks": len(keys),
                "gate_points": int(sum(int(self.resident[k].count)
                                      for k in keys)),
                "target_points": int(getattr(self.density, "last_target", 0) or 0),
                "interaction": bool(interaction)}
        self.last_screen_space_diag = diag
        return keys, diag
    def _build_ranges(self, keys, interaction: bool = False, *,
                      screen_space: bool = False, camera=None, viewport=None):
        gate_keys = None
        """(first, count) draw ranges for the resident set.
        `screen_space=True` (Part 16) asks the EXISTING screen-space gate for the
        frame's submission set, choosing ONE resident representative per visible
        node. It falls back to the rules below whenever the gate cannot answer.
        """
        self.last_screen_space_reason = "not_requested"
        if screen_space:
            sel_keys, _why = self._screen_space_active_keys(
                interaction, camera=camera, viewport=viewport)
            if sel_keys is None:
                # Distinguishable from "screen_space was never asked": a caller
                # must be able to tell a real gate selection from the legacy
                # fallback without inspecting a private attribute.
                self.last_screen_space_reason = str(_why)
                self.screen_space_fallbacks = int(
                    getattr(self, "screen_space_fallbacks", 0) or 0) + 1
            else:
                self.last_screen_space_reason = "selected"
                gate_keys = list(sel_keys)
                keys = sel_keys
        # ---- PHASE 5 - what remains is the NON-screen-space rule -------------
        # `interaction=True` submits the coarse overview instead of LOD0: full
        # spatial coverage at ~1/90th of the points and 1 draw call instead of
        # 393. `interaction=False` submits LOD0 ONLY.
        #
        # The `lod > 0` filter on the IDLE path is essential, not cosmetic: the
        # overview block's points OCCUPY THE SAME SCREEN SPACE as the LOD0 points
        # beneath them, so drawing both rasterises every pixel twice. That is
        # strictly worse than the original behaviour - it is exactly what made a
        # first attempt 21x SLOWER rather than faster.
        #
        # This branch is only reached when `screen_space` is off, or when the
        # screen-space gate could not answer (no camera, no visible nodes, or a
        # visible node with nothing resident).
        if gate_keys is None or not self._screen_space_authoritative():
            if interaction:
                coarse = self._interaction_lod_keys(keys)
                if coarse:
                    keys = coarse
            else:
                fine = [k for k in keys if int(k[1]) <= 0]
                # Only drop the coarse blocks when LOD0 is actually present; a
                # coarse-only resident set must still draw something.
                if fine:
                    keys = fine
        # Record how much of the gate's proposal survived, so "the gate answered"
        # and "the gate's answer was drawn" cannot be confused again. With the
        # gate authoritative (the default) nothing is dropped here and the field
        # stays False; NAKSHA_SCREEN_SPACE_GATE_AUTHORITATIVE=0 restores the old
        # behaviour so the two can be A/B'd on the same camera.
        if gate_keys is not None:
            _d = dict(getattr(self, "last_screen_space_diag", None) or {})
            _d["submitted_blocks"] = len(keys)
            _d["submitted_points"] = int(sum(
                int(self.resident[k].count) for k in keys if k in self.resident))
            _d["overridden_by_legacy_filter"] = (
                len(keys) != len(gate_keys)
                or {k for k in keys} != {k for k in gate_keys})
            self.last_screen_space_diag = _d
        ranges = []
        active = []
        for k in keys:
            v = self.resident.get(k)
            if v is None or v.count == 0 or v.state != GPU_RESIDENT:
                continue
            # A tile with no slot has no GPU geometry: submitting it would read
            # memory the GPU never wrote. It is not in the draw set.
            if int(getattr(v, "first", ARENA_NONE)) == ARENA_NONE:
                continue
            ranges.append((int(v.first), int(v.count)))
            active.append(k)
        # PART 10. ACTIVE_DRAWN is recorded HERE, and this is the only writer of
        # it. It is the frame's submission set - a SUBSET of the resident blocks -
        # and changing it edits draw ranges only. It never evicts, decompresses,
        # concatenates or re-uploads anything.
        self.active_draw_keys = tuple(active)
        self.active_draw_points = int(sum(c for _f, c in ranges))
        return ranges
    def _frame_budget(self) -> "FrameBudgetController":
        """The one adaptive frame-budget controller for this manager (Phase 3).
        Created lazily and stored per-instance: several callers build a manager
        via `__new__`, and a class-level controller would let one dataset's
        measured frame times drive another's density.
        """
        fb = getattr(self, "_fb", None)
        if fb is None:
            target = float(os.environ.get("NAKSHA_FRAME_BUDGET_MS", "16.67")
                           or 16.67)
            fb = self._fb = FrameBudgetController(target_frame_ms=target)
        return fb
    def _maybe_complete_pending_mode(self) -> str:
        """Update pending readiness; never activate from a completion callback."""
        swap = self._atomic_swap()
        pending = swap.pending
        if pending is None:
            return ""
        report = self.mode_readiness(pending.mode)
        attrs = set(report["resident_attrs"])
        ready = report["status"] == "READY"
        swap.mark_ready(
            pending, position_ready=report["gpu_resident"],
            class_ready=ATTR_CLASSIFICATION in attrs,
            intensity_ready=ATTR_INTENSITY in attrs,
            rgb_ready=ATTR_RGB in attrs,
            normal_ready="normal" not in report["derived_pending"],
            surface_ready=bool(report.get("surface_ready", False)),
            coverage_ready=ready, pipeline_ready=ready)
        return pending.mode if ready else "not_ready"

    def _flush_gpu(self, *, reason, gen=None, requested=None, specs=None,
                   interaction: bool = False):
        # PHASE 1: timing boundaries inside the flush, so the profile can say
        # whether the cost is PYTHON (range building, attribute prep) or the
        # native call that actually submits the draws. Only armed under
        # NAKSHA_DEV_PERF; the env read is one small dict lookup.
        _perf = os.environ.get("NAKSHA_DEV_PERF", "").strip() not in (
            "", "0", "false", "False")
        _pt0 = time.perf_counter()
        # PHASE 1: fold in any attribute-only reads that completed since the last
        # flush. Doing it HERE (not only on a mode switch) is what lets a backfill
        # started by an earlier switch finish and reach the GPU: the switch queues
        # the read, this flush uploads it, and the cloud recolours without the
        # user having to leave and re-enter the mode.
        try:
            if self._drain_attr_done():
                self._backfill_missing_attrs()
        except Exception:
            pass
        # The renderer only learns the display mode through an explicit push, and
        # nothing pushed the ON-OPEN default: a no-RGB cloud (123.las) opened in
        # "neutral" while the native renderer stayed in its default RGB mode, so
        # it read uninitialised colour memory - multicolour noise plus
        # black (invisible) tiles. Sync whenever the manager's mode differs from
        # what was last pushed.
        if getattr(self, "_adapter_mode", None) != self.display_mode:
            try:
                self.adapter.set_display_mode(self.display_mode)
            finally:
                self._adapter_mode = self.display_mode
                # PHASE 6C: COMPLETE A DEFERRED SWITCH. 
                # leaves the previous mode on screen when a required stream
                # is not resident yet (never-black) and forces a re-push
                # here once it lands. That re-push used to leave
                #  behind for ever, so a switch that
                # HAD completed was still reported as never rendered - and
                # the next switch would restore a stale "previous mode".
                # Only a genuinely READY mode is claimed, so this can never
                # fabricate a rendered mode.
                try:
                    if self.mode_readiness().get("status") == "READY":
                        self._last_rendered_mode = self.display_mode
                        self._last_mode_substitution = None
                except Exception:
                    pass
        keys = [k for k, v in self.resident.items() if v.state == GPU_RESIDENT]
        new_sig = frozenset(keys)
        structural = (new_sig != self._resident_sig) or \
                     self._display_reupload_needed
        info = {"uploads_this_frame": 0, "draw_ranges": 0,
                "resident_points": sum(self.resident[k].count for k in keys)
                                 if keys else 0, "position_reuploads": 0}
        if not keys:
            self.adapter.set_draw_ranges([])
            self._last_draw_sig = None       # ranges were cleared: dedup must not hide a re-push
            self.active_draw_keys = ()
            self.active_draw_points = 0
            self._resident_sig = new_sig
            self.invalidate_camera_seal()
            return info
        # PART 11/12/14 - STABLE-SLOT PATH. With a native pool, a residency
        # change is applied PER TILE: new tiles get a slot and are written,
        # everything already resident is left exactly where it is. There is no
        # concatenation, no repack and no re-upload of a byte another tile owns.
        # The whole-buffer path below is kept for a DLL without the arena.
        arena_mode = self._arena_ensure()
        if arena_mode:
            if structural:
                self.invalidate_camera_seal()
            try:
                self.adapter.full_upload_allowed = False
            except Exception:
                pass
            info.update(self._arena_flush(keys))
            self._resident_sig = new_sig
            if info.get("new_blocks_uploaded"):
                self.invalidate_camera_seal()
            self._display_reupload_needed = False
            self._record_arena_telemetry(keys, info)
            # PHASE 6C - ATTRIBUTE COVERAGE BOOKKEEPING FOR THE ARENA PATH.
            # The whole-buffer path records how many resident points have a
            # class/intensity stream ON THE GPU (_attr_class_points), and
            # mode readiness reads exactly that. The arena path wrote each
            # tile's streams at its own slot and recorded NOTHING, so readiness
            # reported Class as a MISSING attribute for ever - measured on the
            # streaming rig, every mode that needs class (Class, Line, Shaded)
            # stayed PENDING with its data already uploaded, and Shading never
            # appeared even after its normals landed. Coverage is now MEASURED
            # from the resident tiles whose arena slot is real.
            self._account_arena_attribute_coverage(keys)
        elif structural:
            self.whole_buffer_rebuilds = int(
                getattr(self, "whole_buffer_rebuilds", 0) or 0) + 1
            self.invalidate_camera_seal()   # residency genuinely changed
            # ---- ATTRIBUTE ALIGNMENT (the [CLASSIFICATION STREAM MISMATCH]
            # root cause) -----------------------------------------------
            # The previous code did `if v.cls is not None: cls_p.append(v.cls)`.
            # That SILENTLY SKIPS any block lacking the stream, so the result is
            # SHORTER than XYZ and every subsequent point is attributed to the
            # WRONG point. On 123.las / test_classified_highprecision.laz it
            # produced shape=(299676,) against xyz_count=24569032 - one block
            # out of 289 - and the renderer correctly refused it, leaving the
            # cloud stuck in Neutral.
            #
            # Attribute streams are bound by point INDEX, so the only two
            # correct answers are "every block, same order, same length" or
            # "do not submit the stream at all". There is no partial answer.
            # `_concat_attr_in_key_order` enforces exactly that.
            xyz_p = []
            for k in keys:
                xyz_p.append(self.resident[k].xyz)
            xyz = np.concatenate(xyz_p) if xyz_p else np.empty((0, 3))
            rgb = self._concat_attr_in_key_order(keys, "rgb", int(xyz.shape[0]))
            required = set(nkpc_attrs(self._required_attrs()))
            cls_required = ATTR_CLASSIFICATION in required
            int_required = ATTR_INTENSITY in required
            nrm_required = ATTR_NORMAL in required
            cls = self._concat_attr_in_key_order(keys, "cls", int(xyz.shape[0])) \
                if cls_required else None
            inten = self._concat_attr_in_key_order(keys, "inten", int(xyz.shape[0])) \
                if int_required else None
            # A stream the mode needs but that cannot be completed is NOT sent.
            # Silently sending a short one would mis-colour the whole cloud, and
            # the renderer dropping it later hid the real cause. Report it here
            # with the block that is missing it.
            self._report_attribute_alignment(keys, xyz, cls, int_required,
                                             inten, cls_required, nrm_required)
            if os.environ.get("NAKSHA_DEV_ATTRIBUTE_TRACE", "").strip() not in (
                    "", "0", "false", "False"):
                self._block_attribute_trace(keys, required)
            try:
                self.adapter.full_upload_allowed = (
                    getattr(self, "render_mode", RENDER_STREAMING_LOD)
                    == RENDER_FULL_RESIDENT)
            except Exception:
                pass
            r = self.adapter.upload_resident(xyz, rgb, cls, inten,
                                             self._build_ranges(keys))
            if r.get("full_attempt"):
                self.streaming_full_point_upload_attempts += 1
                # REFUSED: the GPU still holds the PREVIOUS buffer. Publishing
                # draw ranges for the new resident set would index past it
                # (a spatial hole, not an error). Keep the old ranges, and say so.
                _n = int(xyz.shape[0])
                if getattr(self, "_refused_upload_logged", None) != _n:
                    self._refused_upload_logged = _n
                    print(f"[GPU UPLOAD REFUSED] points={_n:,} mode="
                          f"{getattr(self, 'render_mode', None)} - draw ranges NOT "
                          f"updated (would reference data the GPU does not hold)")
                return info
            self.gpu_bytes = int(xyz.nbytes + (rgb.nbytes if rgb is not None else 0)
                                 + (cls.nbytes if cls is not None else 0)
                                 + (inten.nbytes if inten is not None else 0))
            self._resident_sig = new_sig
            info["uploads_this_frame"] = 1
            self._points_resident = int(xyz.shape[0])
        # ---- PART 6/7: normals, uploaded SEPARATELY from geometry ----------
        # Entering Shaded must upload normals WITHOUT re-uploading XYZ: the
        # geometry is already resident and identical (Part 8/23). That is why
        # this is gated on its own flag instead of `structural`.
        if ((not arena_mode) and self._preparing_normals()
                and new_sig != self._normal_sig):
            # The ONLY reason to re-upload normals is that the resident key set
            # changed, which is exactly when the concatenated order (and length)
            # changes. Comparing signatures - rather than a mode-change flag - is
            # what makes the warm cycle cost nothing: same keys, same bytes,
            # already resident, so nothing is sent.
            if self._upload_normals(keys):
                self._normal_sig = new_sig
        self._normal_reupload_needed = False
        # ---- PHASE 1: attribute streams, uploaded AFTER ordered XYZ ---------
        # Ordering is the contract, not a preference: the native attribute API
        # rejects any count that disagrees with the resident position count, and
        # the class/intensity arrays are INDEX-ALIGNED with the concatenated XYZ.
        # So attributes can only be uploaded once the ordered resident set above
        # is the one actually on the GPU.
        # The arena path already wrote each tile's class/intensity stream at that
        # tile's own slot, so a concatenated re-send would be both redundant and
        # wrong (there is no single contiguous buffer to send it to).
        if not arena_mode:
            info.update(self._upload_missing_attributes(keys, new_sig))
        if _perf:
            _t_attr = time.perf_counter()
        # PART 16: when residency is complete, the frame's submission set comes
        # from the SCREEN. `_screen_space_applies()` is False for streaming, so
        # the streaming rule below is untouched.
        _screen = self._screen_space_applies()
        ranges = self._build_ranges(keys, interaction=interaction,
                                    screen_space=_screen,
                                    camera=getattr(self, "_last_camera", None),
                                    viewport=getattr(self, "_last_vp", None))
        if _perf:
            _t_ranges = time.perf_counter()
        _sig = tuple(ranges)
        if (not _screen) or _sig != getattr(self, "_last_draw_sig", None):
            # A settled camera produces a byte-identical selection, so re-pushing
            # the same ranges 60x/second is pure overhead. The dedup is confined
            # to the screen-space path on purpose: for streaming the range list
            # is also the residency contract and must keep being re-asserted.
            self._last_draw_sig = _sig
            self.adapter.set_draw_ranges(ranges)
            self.draw_range_pushes = int(
                getattr(self, "draw_range_pushes", 0) or 0) + 1
            _m = _pm()
            if _m is not None:
                _m.hook_extra(_m.RecordType.RANGE_PUSH,
                              int(getattr(self, "camera_generation", -1) or -1),
                              a0=len(ranges))
        if _perf:
            _t_draw = time.perf_counter()
            self._perf_last_flush = {
                "attr_ms": (_t_attr - _pt0) * 1000.0,
                "ranges_ms": (_t_ranges - _t_attr) * 1000.0,
                "submit_ms": (_t_draw - _t_ranges) * 1000.0,
                "reason": reason, "ranges": len(ranges),
                "interaction": bool(interaction),
                "submitted_points": sum(int(c) for _f, c in ranges),
            }
        info["draw_ranges"] = len(ranges)
        self._display_reupload_needed = False
        self._record_draw_telemetry(keys, ranges)
        self._diag_guard("overview_diag").run(self._overview_diag, "flush_gpu")
        # PHASE 6C/6D: complete a mode the never-black contract DEFERRED, on the
        # flush that makes it READY. This is the existing PENDING -> READY ->
        # re-push machinery (set_display_mode is still the only entry point);
        # what was missing is that the deferred REQUEST was discarded, so a
        # Shading click on a dataset whose normals were still arriving never came
        # back even after the normals landed.
        self._maybe_complete_pending_mode()
        return info
    # ------------------------------------------------------------------ #
    # PART 20/21 - telemetry.                                             #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _density(mgr):
        """The screen-density budget, tolerating a partial construction."""
        d = getattr(mgr, "density", None)
        if d is None:
            d = ScreenDensityBudget()
            try:
                mgr.density = d
            except Exception:
                pass
        return d
    @staticmethod
    def _stale_dropped(mgr) -> int:
        """Total stale normal requests dropped, from whichever counters exist."""
        gate = getattr(mgr, "gate", None)
        pf = getattr(mgr, "prefetch", None)
        return (int(getattr(gate, "stale_requests_dropped", 0) or 0)
                + int(getattr(pf, "dropped", 0) or 0))
    def _record_arena_telemetry(self, keys, info) -> None:
        """PARTS 14/83 - the counters that prove an append is INCREMENTAL.
        `existing_points_reuploaded` is the one that matters: for an ordinary
        append it must be 0, because the whole-buffer path is the thing this
        architecture removes.
        """
        arena = getattr(self, "arena", None)
        self.last_arena_telemetry = {
            "resident_blocks": len(keys),
            "resident_points": int(info.get("resident_points", 0) or 0),
            "active_blocks": len(getattr(self, "active_draw_keys", ()) or ()),
            "active_points": int(getattr(self, "active_draw_points", 0) or 0),
            "new_blocks_uploaded": int(info.get("new_blocks_uploaded", 0) or 0),
            "new_points_uploaded": int(info.get("new_points_uploaded", 0) or 0),
            "existing_points_reuploaded": int(
                info.get("existing_points_reuploaded", 0) or 0),
            "whole_buffer_rebuilds": int(
                getattr(self, "whole_buffer_rebuilds", 0) or 0),
            "arena_capacity": int(getattr(self, "_arena_capacity", 0) or 0),
            "arena_used": int(getattr(arena, "used", 0) or 0),
            "arena_free_runs": len(getattr(arena, "_free", ()) or ()),
            "arena_epoch": int(getattr(self, "_arena_epoch", 0) or 0),
        }
    def active_draw_report(self) -> dict:
        """PART 10/99 - GPU_RESIDENT vs ACTIVE_DRAWN, read from live state."""
        res_blocks = [k for k, v in self.resident.items()
                      if v.state == GPU_RESIDENT]
        return {
            "resident_blocks": len(res_blocks),
            "resident_points": int(sum(self.resident[k].count
                                      for k in res_blocks)),
            "active_blocks": len(getattr(self, "active_draw_keys", ()) or ()),
            "active_points": int(getattr(self, "active_draw_points", 0) or 0),
            "separated": bool(len(getattr(self, "active_draw_keys", ()) or ())
                              != len(res_blocks)
                              or not getattr(self, "arena", None)),
            "arena": bool(getattr(self, "arena", None) is not None),
            "whole_buffer_rebuilds": int(
                getattr(self, "whole_buffer_rebuilds", 0) or 0),
            "new_blocks_uploaded": int(
                getattr(self, "new_blocks_uploaded", 0) or 0),
            "new_points_uploaded": int(
                getattr(self, "new_points_uploaded", 0) or 0),
            "existing_points_reuploaded": int(
                getattr(self, "existing_points_reuploaded", 0) or 0),
        }
    def _active_draw_trace(self, keys, ranges) -> None:
        """PART 25 - one rate-limited [ACTIVE DRAW] line.
        Off unless NAKSHA_ACTIVE_DRAW_TRACE is set, and at most one line per
        NAKSHA_ACTIVE_DRAW_EVERY frames (default 30): this is a diagnostic, and
        a diagnostic that costs a line of output per frame is a performance bug
        in its own right.
        The point of the line is Part 12/26 - to make it visible WHERE overdraw
        enters, by reporting the selector's target and proposal, what was ready,
        what covered a gap, and what was actually submitted, as separate numbers
        rather than one "points" figure.
        """
        if os.environ.get("NAKSHA_ACTIVE_DRAW_TRACE", "").strip() in (
                "", "0", "false", "False"):
            return
        try:
            every = max(1, int(os.environ.get("NAKSHA_ACTIVE_DRAW_EVERY", "30")))
            self._active_draw_frame = int(
                getattr(self, "_active_draw_frame", 0) or 0) + 1
            if self._active_draw_frame % every:
                return
            diag = dict(getattr(self, "last_screen_space_diag", None) or {})
            area = dict(getattr(self, "active_draw_report", lambda: {})() or {})
            submitted = int(sum(int(c) for _f, c in (ranges or [])))
            resident_pts = int(area.get("resident_points", 0) or 0)
            gate_pts = int(diag.get("gate_points", 0) or 0)
            px = max(int(getattr(self, "_vp_w", 0) or 0)
                     * int(getattr(self, "_vp_h", 0) or 0), 1)
            print(
                "[ACTIVE DRAW]"
                f" camera_state={getattr(self, '_camera_state', None)}"
                f" mode={getattr(self, 'display_mode', None)}"
                f" viewport={getattr(self, '_vp_w', 0)}x{getattr(self, '_vp_h', 0)}"
                f" screen_pixels={px}"
                f" resident_blocks={int(area.get('resident_blocks', 0) or 0)}"
                f" resident_points={resident_pts}"
                f" selector_budget={int(getattr(self.density, 'last_target', 0) or 0)}"
                f" selector_blocks={int(diag.get('gate_blocks', 0) or 0)}"
                f" selector_points={gate_pts}"
                f" ready_selected_blocks={int(diag.get('submitted_blocks', 0) or 0)}"
                f" ready_selected_points="
                f"{int(diag.get('submitted_points', 0) or 0)}"
                f" fallback_blocks={int(area.get('fallback_blocks', 0) or 0)}"
                f" fallback_points={int(area.get('fallback_points', 0) or 0)}"
                # ACTIVE_DRAWN, not the resident set: `keys` handed in here is the
                # RESIDENT list, and printing its length as the active block count
                # reported 1339 blocks for a 230-block frame.
                f" active_draw_blocks={len(getattr(self, 'active_draw_keys', ()) or ())}"
                f" submitted_points={submitted}"
                f" actual_points_per_pixel={submitted / px:.3f}"
                f" XYZ_upload_bytes={int(getattr(self, 'last_xyz_upload_bytes', 0) or 0)}"
                f" attribute_upload_bytes="
                f"{int(getattr(self, 'last_attr_upload_bytes', 0) or 0)}"
                f" whole_buffer_rebuilds="
                f"{int(getattr(self, 'whole_buffer_rebuilds', 0) or 0)}"
                f" draw_ratio={submitted / max(resident_pts, 1):.4f}"
                f" selected_to_submitted="
                f"{submitted / max(gate_pts, 1):.3f}",
                flush=True)
        except Exception:
            # A diagnostic must never take down the path it observes.
            pass
    def _record_draw_telemetry(self, keys, ranges) -> None:
        """PART 20 telemetry. READ-ONLY, and must never raise.
        Several callers construct the manager via ``__new__`` with only the
        fields they exercise. Telemetry that throws on a missing counter would
        take down the very code path it is meant to observe, so every read here
        is defensive.
        """
        lod_dist = {}
        for k in keys:
            lod = int(self.resident[k].lod)
            lod_dist[lod] = lod_dist.get(lod, 0) + 1
        submitted = int(sum(int(c) for _f, c in (ranges or [])))
        self._active_draw_trace(keys, ranges)
        res_xyz = sum(int(self.resident[k].count) for k in keys)
        res_cls = sum(int(self.resident[k].count) for k in keys
                      if self.resident[k].cls is not None)
        try:
            self.last_draw_telemetry = draw_telemetry(
                normal_source=str(getattr(self, "_normal_source", "screen")),
                source_points=int(getattr(self, "total_points", 0) or 0),
                visible_tiles=len(keys), lod_distribution=lod_dist,
                resident_xyz_points=res_xyz, resident_class_points=res_cls,
                resident_normal_points=int(getattr(
                    self, "normal_resident_points", 0) or 0),
                submitted_points=submitted,
                width=int(getattr(self, "_vp_w", 0) or 0),
                height=int(getattr(self, "_vp_h", 0) or 0),
                splat_px=getattr(self, "splat_px", None),
                normal_ram_bytes=int(getattr(
                    getattr(self, "normal_cache", None),
                    "resident_bytes", 0) or 0),
                normal_gpu_bytes=int(getattr(self, "normal_gpu_bytes", 0) or 0),
                new_normal_blocks=int(getattr(self, "new_normal_blocks", 0) or 0),
                reused_normal_blocks=int(getattr(
                    self, "reused_normal_blocks", 0) or 0),
                stale_requests_dropped=int(self._stale_dropped(self)),
                xyz_gpu_bytes=int(getattr(self, "gpu_bytes", 0) or 0)
                - int(getattr(self, "normal_gpu_bytes", 0) or 0),
                class_gpu_bytes=int(res_cls), other_attr_bytes=0)
        except Exception:
            # A telemetry failure is never allowed to break rendering.
            pass
    def tile_load_event(self, *, reason: str, block_id: int, lod: int,
                        xyz_bytes: int, class_bytes: int, normal_bytes: int,
                        reused_normal: bool = False) -> dict:
        ev = tile_load_telemetry(reason=reason, block_id=block_id, lod=lod,
                                 xyz_bytes=xyz_bytes, class_bytes=class_bytes,
                                 normal_bytes=normal_bytes,
                                 reused_normal=reused_normal)
        self.tile_load_events.append(ev)
        if len(self.tile_load_events) > 512:
            del self.tile_load_events[:-512]
        return ev
    def _upload_normals(self, keys) -> bool:
        """PART 6/7 - concatenate normals in EXACTLY the _flush_gpu key order
        and upload them as packed oct16. Returns True on a completed upload.
        ``keys`` is the very list _flush_gpu used for XYZ, walked again in that
        same order. There is deliberately no second, normal-specific sort: that
        is how normals get desynchronised from geometry while every per-block
        test still passes.
        """
        self.new_normal_blocks = 0
        self.reused_normal_blocks = 0
        expected = int(getattr(self, "_points_resident", 0) or 0)
        if expected <= 0:
            expected = sum(int(self.resident[k].count) for k in keys)
        normal, err = concat_normals_in_key_order(
            keys, lambda k: self.resident[k].normal,
            expected_points=expected)
        if normal is None:
            # Never upload a half-shaded cloud. Draw ranges are left untouched,
            # so the previous (coarse, complete) frame stays on screen and the
            # next flush completes with no teardown and no black frame.
            print(f"[STREAM] normal concat refused: {err}; keeping previous "
                  f"draw ranges")
            return False
        for k in keys:
            v = self.resident[k]
            if v.normal_from_cache:
                self.reused_normal_blocks += 1
            else:
                self.new_normal_blocks += 1
        ok = bool(self.adapter.upload_resident_normals(normal))
        if ok:
            self.normal_upload_count += 1
            self.normal_gpu_bytes = int(normal.nbytes)
            self.normal_resident_points = int(normal.shape[0])
        else:
            print("[STREAM] normal upload REFUSED by the native backend")
            self.normal_gpu_bytes = 0
        return ok
    def _sample(self, **extra):
        lod_dist = {}
        for v in self.resident.values():
            if v.state == GPU_RESIDENT:
                lod_dist[v.lod] = lod_dist.get(v.lod, 0) + 1
        ram_pts = sum(v["pts"] for v in self.ram_cache.values())
        gpu_pts = sum(v.count for v in self.resident.values()
                      if v.state == GPU_RESIDENT)
        # Avoid kwarg collisions: callers push requested_points /
        # position_reuploads / streaming_full_point_upload_attempts via
        # **extra (from _flush_gpu). Pop them so we can set authoritative values.
        extra.pop("requested_points", None)
        extra.pop("position_reuploads", None)
        extra.pop("streaming_full_point_upload_attempts", None)
        self.te.sample(
            dataset_mode="streaming", camera_generation=self._gen,
            visible_pages=int(sum(1 for v in self.resident.values()
                                  if v.state == GPU_RESIDENT)),
            visible_microcells=len(self.ram_cache),
            lod_distribution=lod_dist,
            decoded_points=ram_pts, drawn_points=gpu_pts,
            ram_resident_points=ram_pts, ram_resident_bytes=self.ram_bytes,
            gpu_resident_points=gpu_pts, gpu_resident_bytes=self.gpu_bytes,
            request_queue_depth=self.queue_depth,
            ram_hits=self.ram_hits, ram_misses=self.ram_misses,
            uploads=self.adapter.stats().get("uploads", 0),
            evictions=self.evictions, stale_cancellations=self.cancelled_stale,
            position_reuploads=self.position_reuploads,
            streaming_full_point_upload_attempts=
                self.streaming_full_point_upload_attempts,
            requests=self.requests, completed=self.completed,
            queue_depth=self.queue_depth,
            event=extra.pop("reason", "sample"), **extra)
    def request_render(self):
        self.adapter.request_render()
    def close(self):
        surface_service = getattr(self, "surface_service", None)
        if surface_service is not None:
            surface_service.shutdown(timeout=5)
        # C2: graceful shutdown of the background normal builder.
        service = getattr(self, "normal_service", None)
        if service is not None:
            try:
                service.shutdown(timeout=5.0)
            except Exception:
                pass
        # Cancel reads that have not started and WAIT for those that have.
        try:
            self._executor.shutdown(wait=True, cancel_futures=True)
        except TypeError:
            try:
                self._executor.shutdown(wait=True)
            except Exception:
                pass
        except Exception:
            pass
