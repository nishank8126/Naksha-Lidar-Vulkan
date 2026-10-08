"""Per-stage CPU frame timing (PHASE 1 of the optimisation pass).

The native engine ALREADY measured cpuSubmitMs / presentMs internally but
exported nothing, so the largest single cost of a frame could not be
attributed to a stage. This module is pure OBSERVABILITY: it adds no state,
changes no behaviour, and reads counters the engine now maintains.

GPU time is deliberately NOT synthesised. Vulkan GPU timestamps are optional
and are UNAVAILABLE on this device (timestampPeriod == 0), so any "GPU ms"
produced here would be a fabrication. Callers wanting GPU time must use
nkv_get_last_frame_stats(), which reports validity explicitly.
"""
from __future__ import annotations

import ctypes

# Must match NKV_FRAME_STAGE_COUNT in naksha_vulkan_c_api.h.
STAGE_COUNT = 9

STAGE_FIELDS = (
    "frame_cpu_total_ms",   # 0 whole frame, CPU side
    "fence_wait_ms",        # 1 vkWaitForFences
    "acquire_ms",           # 2 vkAcquireNextImageKHR
    "ubo_update_ms",        # 3 camera UBO + aspect
    "begin_frame_ms",       # 4 cmd reset/begin + render-pass begin
    "command_record_ms",    # 5 point/surface Record()
    "queue_submit_ms",      # 6 vkQueueSubmit
    "present_ms",           # 7 vkQueuePresentKHR
    "end_frame_ms",         # 8 cmd end + fence reset
)


def bind(dll) -> bool:
    """Attach argtypes/restype to the exported getter. False if unavailable."""
    try:
        fn = dll.nkv_get_frame_timings
    except AttributeError:
        return False          # older DLL: timings unavailable, not an error
    fn.restype = ctypes.c_int
    fn.argtypes = [ctypes.c_uint64, ctypes.POINTER(ctypes.c_double)]
    return True


def available(dll) -> bool:
    return hasattr(dll, "nkv_get_frame_timings")


def read(dll, handle) -> dict:
    """Per-stage CPU ms for the last completed frame.

    Returns {} when the export or handle is unavailable. Callers MUST treat
    that as UNAVAILABLE, never as 0.0 ms.
    """
    buf = (ctypes.c_double * STAGE_COUNT)()
    rc = dll.nkv_get_frame_timings(ctypes.c_uint64(int(handle)), buf)
    if rc != 1:
        return {}
    return {name: float(buf[i]) for i, name in enumerate(STAGE_FIELDS)}


def gpu_note(dll, handle) -> str:
    """Honest GPU-time statement. Never invents a duration."""
    try:
        fn = dll.nkv_last_frame_ms
        fn.restype = ctypes.c_double
        fn.argtypes = [ctypes.c_uint64]
        v = float(fn(ctypes.c_uint64(int(handle))))
        return "GPU timestamps present" if v >= 0.0 else "GPU_TIMESTAMPS = UNAVAILABLE"
    except Exception:
        return "GPU_TIMESTAMPS = UNAVAILABLE"


def format_report(dll, handle, label: str = "") -> str:
    """Breakdown for humans."""
    t = read(dll, handle)
    if not t:
        return "[FRAME TIMINGS] UNAVAILABLE (export or handle missing)"
    lines = [f"[FRAME TIMINGS]{(' ' + label) if label else ''}"]
    for name in STAGE_FIELDS:
        lines.append(f"  {name:22s} {t[name]:8.3f} ms")
    lines.append(f"  {'gpu':22s} {gpu_note(dll, handle)}")
    return "\n".join(lines)