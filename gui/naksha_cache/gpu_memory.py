"""PART 6 - GPU MEMORY ESTIMATOR and RENDER-MODE SELECTION.

Two point-rendering modes (PART 5):

  A. FULL_RESIDENT   - upload every source point ONCE; pan / zoom / orbit / fit
                       are then camera-only. No tile swapping, no LOD
                       replacement, no retirement, no disk reads during
                       interaction. This is the fastest AND the most visually
                       faithful mode, because it is the old VTK point set
                       verbatim.
  B. STREAMING_LOD   - the NKIDX/NKPC hierarchy path, for datasets that do not
                       fit in VRAM. Coverage-atomic parent/child replacement,
                       old parent retained until the replacement is ready.

SELECTION IS BY MEMORY, NOT BY A POINT-COUNT MAGIC NUMBER (PART 6). A
hardcoded threshold is wrong because the cost depends on WHICH attributes exist:
XYZ-only and XYZ+CLASS+RGB+INTENSITY+NORMAL differ several-fold per point, and
the budget belongs to the actual GPU, not to a constant in this file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


def _env_float(name: str, default: float) -> float:
    """Env override for a fraction; a malformed value falls back to default
    rather than crashing the render path."""
    try:
        v = os.environ.get(name)
        return float(v) if v else default
    except (TypeError, ValueError):
        return default

RENDER_FULL_RESIDENT = "FULL_RESIDENT"
RENDER_STREAMING_LOD = "STREAMING_LOD"

# Per-point GPU stream costs. XYZ is float32 in the render path (the reader
# decodes to float32 for rendering), which is the number that reaches VRAM.
BYTES_XYZ_F32 = 3 * 4          # 12
BYTES_CLASS = 1
BYTES_INTENSITY = 2           # uint16
BYTES_RGB = 3 * 2             # uint16 triplet, as NKPC stores it
BYTES_NORMAL = 2 * 2          # packed oct16 x2, int16
BYTES_SOURCE_ID = 8           # uint64, only when editing/export needs it

# Draw/index/state overhead. Vertex pulls, index ranges and driver scratch are
# real VRAM consumers even with a single non-indexed draw, so the estimate
# carries headroom rather than pretending the buffers are the whole cost.
DRAW_OVERHEAD_RATIO = 0.10

# Used only when real VRAM cannot be probed. Deliberately conservative:
# under-estimating would pick FULL_RESIDENT and then overflow the GPU, which is
# far worse than unnecessarily streaming.
DEFAULT_SAFE_GPU_BUDGET_BYTES = 2 * 1024 ** 3

ENV_BUDGET = "NAKSHA_SAFE_GPU_BUDGET_BYTES"
ENV_MODE = "NAKSHA_RENDER_MODE"


@dataclass
class GpuEstimate:
    """A measured, printable account of why a mode was chosen."""
    point_count: int = 0
    bytes_per_point: int = 0
    stream_bytes: int = 0
    overhead_bytes: int = 0
    total_bytes: int = 0
    safe_budget_bytes: int = DEFAULT_SAFE_GPU_BUDGET_BYTES
    budget_source: str = "default-conservative"
    mode: str = RENDER_STREAMING_LOD

    @property
    def fits(self) -> bool:
        return self.total_bytes <= self.safe_budget_bytes

    @property
    def headroom_bytes(self) -> int:
        return self.safe_budget_bytes - self.total_bytes

    def describe(self) -> str:
        return (f"points={self.point_count:,} "
                f"bytes_per_point={self.bytes_per_point} "
                f"streams={self.stream_bytes:,} "
                f"overhead={self.overhead_bytes:,} "
                f"estimated_gpu_bytes={self.total_bytes:,} "
                f"safe_gpu_budget={self.safe_budget_bytes:,} ({self.budget_source}) "
                f"mode={self.mode} headroom={self.headroom_bytes:,}")

    def as_dict(self) -> dict:
        return {
            "point_count": self.point_count, "bytes_per_point":
            self.bytes_per_point, "stream_bytes": self.stream_bytes,
            "overhead_bytes": self.overhead_bytes,
            "total_bytes": self.total_bytes,
            "safe_budget_bytes": self.safe_budget_bytes,
            "budget_source": self.budget_source, "mode": self.mode,
            "headroom_bytes": self.headroom_bytes, "fits": self.fits,
        }


def probe_safe_gpu_budget(adapter=None) -> tuple:
    """(budget_bytes, source). Real device memory when it can be MEASURED.

    An explicit env override always wins. Otherwise the MEASURED hardware
    profile decides - there is no card-specific or fixed-size constant here, so
    the same code adapts to whatever machine it runs on (PART 10/11).

    NOTE ON WHAT IS MEASURABLE TODAY: the native backend exports
    ``nkv_get_vram_bytes``, which is USED VRAM, not the device total or a
    ``VK_EXT_memory_budget`` heap budget. Reading used-VRAM as if it were a
    budget would be wrong, so it is deliberately NOT used. Until the renderer
    exposes total device-local memory, this falls back to the hardware
    profile's software-renderer default, which is labelled as such rather than
    presented as a real card measurement.
    """
    raw = os.environ.get(ENV_BUDGET)
    if raw:
        try:
            return int(raw), "env-override"
        except (TypeError, ValueError):
            pass
    try:
        from .hardware import GIB, GPU_SAFE_FRACTION, detect_gpu
        gpu = detect_gpu(adapter)
        budget, source = gpu["budget_bytes"], gpu["source"]
        fraction = _env_float("NAKSHA_GPU_SAFE_FRACTION",
                              GPU_SAFE_FRACTION)
        # Prefer the Vulkan BUDGET when the driver reports one: it already
        # accounts for other clients. Otherwise a conservative share of total.
        if budget > 0:
            return int(budget * fraction), f"{source}*fraction"
        return int(gpu["total_bytes"] * fraction), f"{source}*fraction"
    except Exception:
        # A fallback must never be silent about being a fallback: the source
        # string says so, and the value is clearly a conservative default.
        return DEFAULT_SAFE_GPU_BUDGET_BYTES, "default-conservative"


def estimate_resident_gpu_bytes(point_count: int, *, has_class: bool = False,
                                has_intensity: bool = False,
                                has_rgb: bool = False,
                                needs_normal: bool = False,
                                needs_source_id: bool = False,
                                safe_budget_bytes: Optional[int] = None,
                                budget_source: str = "supplied") -> GpuEstimate:
    """Bytes the WHOLE dataset needs on the GPU for the CURRENT mode.

    Only attributes that ACTUALLY EXIST / are NEEDED are counted. Nothing is
    fabricated: an absent RGB stream contributes zero and is not requested.
    """
    n = max(0, int(point_count))
    per = BYTES_XYZ_F32
    if has_class:
        per += BYTES_CLASS
    if has_intensity:
        per += BYTES_INTENSITY
    if has_rgb:
        per += BYTES_RGB
    if needs_normal:
        per += BYTES_NORMAL
    if needs_source_id:
        per += BYTES_SOURCE_ID
    streams = per * n
    overhead = int(streams * DRAW_OVERHEAD_RATIO)
    total = streams + overhead
    if safe_budget_bytes is None:
        safe_budget_bytes, resolved = probe_safe_gpu_budget(None)
        budget_source = resolved
    est = GpuEstimate(
        point_count=n, bytes_per_point=per, stream_bytes=streams,
        overhead_bytes=overhead, total_bytes=total,
        safe_budget_bytes=int(safe_budget_bytes), budget_source=budget_source)
    est.mode = RENDER_FULL_RESIDENT if est.fits else RENDER_STREAMING_LOD
    return est


def estimate_from_capabilities(point_count: int, capabilities, *,
                               needs_normal: bool = False,
                               needs_source_id: bool = False,
                               adapter=None) -> GpuEstimate:
    """PART 11 - the estimate the app actually prints and acts on."""
    budget, source = probe_safe_gpu_budget(adapter)
    return estimate_resident_gpu_bytes(
        point_count,
        has_class=bool(getattr(capabilities, "has_classification", False)),
        has_intensity=bool(getattr(capabilities, "has_intensity", False)),
        has_rgb=bool(getattr(capabilities, "has_rgb", False)),
        needs_normal=bool(needs_normal),
        needs_source_id=bool(needs_source_id),
        safe_budget_bytes=budget, budget_source=source)


def force_render_mode(mode: Optional[str] = None) -> Optional[str]:
    """Operator override, so both paths can be tested deliberately."""
    raw = str(mode or os.environ.get(ENV_MODE) or "").strip().upper()
    return raw if raw in (RENDER_FULL_RESIDENT, RENDER_STREAMING_LOD) else None

