"""INTERACTION PROFILER - PART 1/44.

Measures ONE real interaction frame, stage by stage:

    mouse event -> camera modified -> stream manager -> renderer update
                -> command submission -> present

Design rule (PART 44/17): when disabled the cost is ONE module-global bool
test per stage and ZERO dict/timer allocation. Diagnostics are OFF unless
NAKSHA_DEV_DIAGNOSTICS is set, so production interaction pays nothing.

Stages measured
---------------
  event            Qt / mouse handling
  camera_update    VTK camera callback -> adopted pose
  camera2d         parallel/projection conversion
  lod_selection    LOD gate selection
  frontier         active/selected set reconciliation
  cpu_copy         NumPy point-array work (concat / copy / permute)
  gpu_upload       host->device buffer transfer
  command_record   command buffer recording
  queue_submit     vkQueueSubmit
  present_wait     acquire + present + fence wait
"""
from __future__ import annotations

import os
import time
from collections import defaultdict
from typing import Dict, Optional

# Single source of truth for DEV diagnostics (PART 7). Everything detailed is
# behind this; production is silent.
DEV_DIAGNOSTICS = os.environ.get("NAKSHA_DEV_DIAGNOSTICS", "") not in (
    "", "0", "false", "FALSE", "off", "OFF")

STAGES = ("event", "camera_update", "camera2d", "lod_selection", "frontier",
          "cpu_copy", "gpu_upload", "command_record", "queue_submit",
          "present_wait")

# Counters that must be provably ZERO for a FULL_RESIDENT camera frame
# (PART 2/39). These are cheap integers, always live.
COUNTERS = ("point_buffer_reuploads", "disk_reads", "decompressions",
            "point_array_copies", "vulkan_allocations", "vulkan_frees",
            "vk_device_wait_idle", "vk_queue_wait_idle", "duplicate_renders",
            "swapchain_rebuilds", "render_origin_changes", "camera_events",
            "camera_events_coalesced", "renders_submitted")

_state = {"on": False, "samples": [], "cur": None}
_counts = defaultdict(int)
_last = {}


def enabled() -> bool:
    """Hot-path check: one attribute load + one compare."""
    return _state["on"]


def dev_diagnostics() -> bool:
    """PART 7 - detailed logging is OFF unless explicitly requested."""
    return DEV_DIAGNOSTICS


def reset_counters() -> None:
    _counts.clear()


def bump(name: str, n: int = 1) -> None:
    """Counters are ALWAYS live (they are single dict lookups) so the
    acceptance telemetry in PART 2/27/39 can be gathered with zero overhead."""
    _counts[name] += n


def counter(name: str) -> int:
    return int(_counts.get(name, 0))


def snapshot() -> Dict[str, int]:
    return dict(_counts)


# --------------------------------------------------------------------------- #
# Stage timing - only when explicitly enabled                                     #
# --------------------------------------------------------------------------- #
def begin(event: str = "PAN") -> None:
    """Open a frame sample. No-op unless profiling is on."""
    if not _state["on"]:
        return
    _state["cur"] = {"event": event, "t0": time.perf_counter(), "stages": {}}
    _last.clear()


def stage(name: str) -> "_StageCtx":
    return _StageCtx(name)


class _StageCtx:
    __slots__ = ("_name", "_t0", "_on")

    def __init__(self, name: str):
        self._name = name
        self._on = _state["on"]
        self._t0 = time.perf_counter() if self._on else 0.0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self._on:
            cur = _state["cur"]
            if cur is not None:
                cur["stages"][self._name] = ((time.perf_counter() - self._t0)
                                             * 1000.0)
        return False


def end() -> Optional[dict]:
    """Close and store the frame sample. No-op unless profiling is on."""
    if not _state["on"]:
        return None
    cur = _state["cur"]
    _state["cur"] = None
    if cur is None:
        return None
    cur["total_cpu_frame_ms"] = ((time.perf_counter() - cur["t0"]) * 1000.0)
    _state["samples"].append(cur)
    return cur


def samples() -> list:
    return list(_state["samples"])


def clear() -> None:
    _state["samples"].clear()


def _pct(values, q):
    if not values:
        return 0.0
    s = sorted(values)
    i = min(len(s) - 1, max(0, int(round((len(s) - 1) * q))))
    return float(s[i])


def report(label: str = "PAN", samples_: Optional[list] = None) -> str:
    """PART 1 - the [INTERACTION PROFILE] block."""
    data = samples_ if samples_ is not None else _state["samples"]
    rows = [s for s in data if s.get("event") == label] or list(data)
    if not rows:
        return "[INTERACTION PROFILE]\nevent: (no samples captured)"
    out = ["[INTERACTION PROFILE]", f"event: {label}",
           f"frames_sampled: {len(rows)}"]
    means = {}
    for st in STAGES:
        vals = [r["stages"].get(st, 0.0) for r in rows]
        m = sum(vals) / len(vals)
        means[st] = m
        out.append(f"{st}_ms: {m:.3f} (p95 {_pct(vals, 0.95):.3f})")
    tot = sum(r.get("total_cpu_frame_ms", 0.0) for r in rows) / len(rows)
    out.append(f"total_cpu_frame_ms: {tot:.3f}")
    hot = max(means, key=lambda k: means[k])
    out.append(f"blocking_reason: {hot} ({means[hot]:.3f} ms)")
    return "\n".join(out)


def classify(mean_cpu_ms: float, mean_gpu_ms: float = 0.0,
             io_ms: float = 0.0, sync_ms: float = 0.0,
             event_rate: float = 0.0) -> str:
    """PART 45 - classify from MEASURED numbers, never from how the UI feels."""
    if io_ms > 0 and io_ms >= max(mean_cpu_ms, sync_ms, 1e-6):
        return "IO_BOUND"
    if sync_ms > 0 and sync_ms >= max(mean_cpu_ms, io_ms, 1e-6):
        return "SYNC_BOUND"
    if mean_gpu_ms > 0 and mean_gpu_ms > mean_cpu_ms * 1.5:
        return "GPU_BOUND"
    if mean_cpu_ms > 16.7:
        return "PYTHON_EVENT_BOUND"
    return "CPU_BOUND"