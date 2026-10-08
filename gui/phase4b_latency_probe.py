"""PHASE 4B-A - lightweight REAL-GUI camera / present latency probe.

THIS IS INSTRUMENTATION ONLY. It observes; it never decides.

Correlation chain measured (all timestamps ``time.perf_counter_ns()``, a
monotonic CPU clock - no GPU timestamp is synthesised anywhere):

    Qt input -> camera generation applied -> frame submitted -> present call

HONEST METRIC NAMES (critical)
==============================
``INPUT_TO_CAMERA_MS``       input event -> camera state committed to the
                            active render backend. The generation is the
                            EXISTING canonical ``camera_generation`` (stream
                            manager, else MainCamera2D) - never a second
                            competing generation system.
``INPUT_TO_SUBMIT_MS``       input event -> CPU-side frame submit call.
                            Vulkan: immediately BEFORE the synchronous
                            ``nkv_render()`` (build+submit+present happen in
                            that one native call). VTK: immediately before
                            the throttled ``Render()``.
``INPUT_TO_PRESENT_CALL_MS`` input event -> immediately AFTER ``nkv_render()``
                            returned 2, i.e. after ``vkQueuePresentKHR``
                            returned and the present counter advanced. A CALL
                            timestamp - NOT photon/scanout latency, and never
                            reported as such.

Backend honesty
===============
The *engine* backend and the *visible* main viewport are tracked separately.
A hidden Vulkan widget must never supply present latency for a view the user
cannot see: when the visible main viewport is VTK the present metric is
dropped and the run is labelled ``BACKEND = VTK`` /
``VULKAN_PRESENT_METRIC_VALID_FOR_VISIBLE_VIEW = NO``.

Disabled cost
=============
When the probe is not configured every hook is ``_PROBE is None`` plus one
lazy env check: no allocation, no clock read, no behaviour change. When
configured but not capturing, hooks return after one boolean test. Nothing is
printed per event or per frame - only startup / capture start / capture stop.

Environment switches (never hard-coded in production)
=====================================================
``NAKSHA_LATENCY_PROBE=1``           enable the probe (startup block prints).
``NAKSHA_LATENCY_SCENARIO=<name>``   default scenario label.
``NAKSHA_LATENCY_PROBE_CAPACITY=n``  ring capacity (default 100,000).
``Ctrl+Shift+L``                     start / stop one capture (dev-only).
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import weakref
from collections import deque
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# --------------------------------------------------------------------------- #
# Configuration                                                                 #
# --------------------------------------------------------------------------- #
ENABLE_ENV = "NAKSHA_LATENCY_PROBE"
SCENARIO_ENV = "NAKSHA_LATENCY_SCENARIO"
CAPACITY_ENV = "NAKSHA_LATENCY_PROBE_CAPACITY"
DEFAULT_CAPACITY = 100_000

SCHEMA = "phase4b_latency_probe/1"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "diagnostics" / "phase4b"

_ENV_TRUE = ("1", "true", "yes", "on")

# VkPresentModeKHR - reported even though the native C API does not export the
# ACTIVE mode (see LatencyProbe.detect). MAILBOX-preferred, FIFO-fallback is
# what native/.../VulkanSwapchain.cpp and Renderer.cpp actually implement.
PRESENT_MODE_ENUM = {
    0: "VK_PRESENT_MODE_IMMEDIATE_KHR",
    1: "VK_PRESENT_MODE_MAILBOX_KHR",
    2: "VK_PRESENT_MODE_FIFO_KHR",
    3: "VK_PRESENT_MODE_FIFO_RELAXED_KHR",
}
NATIVE_PRESENT_MODE_PREFERENCE = ("VK_PRESENT_MODE_MAILBOX_KHR (1) if supported, "
                                  "else VK_PRESENT_MODE_FIFO_KHR (2)")
SWAPCHAIN_FORMAT_DOCUMENTED = ("VK_FORMAT_B8G8R8A8_SRGB "
                              "(documented in naksha_vulkan_c_api.h)")


class RecordType(IntEnum):
    """Compact record ids (numeric so the ring stores no strings)."""
    INPUT = 1
    CAMERA_APPLY = 2
    FRAME_SUBMIT = 3
    PRESENT = 4
    FAST_PATH = 5
    SLOW_PATH = 6
    RANGE_PUSH = 7
    GPU_UPLOAD = 8
    MODE_CHANGE = 9
    SAFETY_CORRECTION = 10
    # Stream-manager emitted (gui/naksha_cache/stream_manager.py):
    #   SELECTOR     a0 = derivation ms, a1 = 1 compiled kernel / 0 reference
    #   FAST_PATH    camera-only frame (no flush)
    #   SLOW_PATH    a0 = reason the full flush ran
    #   RANGE_PUSH   a0 = number of draw ranges submitted
    #   GPU_UPLOAD   a0 = kind (UPLOAD_*), a1 = bytes (or count for normals)
    #   SAFETY_CORRECTION  the 0.5 s re-check found a missed invalidation
    SELECTOR = 11


# GPU_UPLOAD kinds. The adapter reports class + intensity + normal payloads
# through ONE attribute-byte counter, so they cannot be split honestly.
UPLOAD_XYZ_BYTES = 1
UPLOAD_ATTR_BYTES = 2        # class + intensity (+ normal) payload bytes
UPLOAD_NORMAL_COUNT = 3      # normal-tile uploads (a count, not bytes)


class InputType(IntEnum):
    PAN = 1
    WHEEL = 2
    OTHER_CAMERA = 3


BACKEND_UNKNOWN = 0
BACKEND_VTK = 1
BACKEND_VULKAN = 2
_BACKEND_NAMES = {BACKEND_UNKNOWN: "UNKNOWN", BACKEND_VTK: "VTK",
                  BACKEND_VULKAN: "VULKAN"}



# --------------------------------------------------------------------------- #
# Compact record                                                                #
# --------------------------------------------------------------------------- #
class ProbeRecord:
    """One telemetry sample. Numeric fields only - no per-record strings.

    Field meaning by record type::

        INPUT          a0=InputType  a1=x  a2=y  a3=wheel_delta
        CAMERA_APPLY   a0=<reserved> a1=cx a2=cy a3=world_width
        FRAME_SUBMIT   a0=present counter at submit time (or -1)
        PRESENT        a0=present counter (dup)  a1=native rendered count

    ``camera_generation`` is -1 when no existing generation source is bound.
    """
    __slots__ = ("ts_ns", "rtype", "event_seq", "camera_generation", "frame_id",
                 "present_counter", "backend", "a0", "a1", "a2", "a3")

    def __init__(self, ts_ns: int, rtype: int, event_seq: int = 0,
                 camera_generation: int = -1, frame_id: int = -1,
                 present_counter: int = -1, backend: int = BACKEND_UNKNOWN,
                 a0: Any = 0, a1: Any = 0, a2: Any = 0, a3: Any = 0) -> None:
        self.ts_ns = int(ts_ns)
        self.rtype = int(rtype)
        self.event_seq = int(event_seq)
        self.camera_generation = int(camera_generation)
        self.frame_id = int(frame_id)
        self.present_counter = int(present_counter)
        self.backend = int(backend)
        self.a0 = a0
        self.a1 = a1
        self.a2 = a2
        self.a3 = a3

    @property
    def type_name(self) -> str:
        try:
            return RecordType(self.rtype).name
        except ValueError:
            return str(self.rtype)

    def as_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "ts_ns": self.ts_ns,
            "record_type": self.type_name,
            "event_seq": self.event_seq,
            "camera_generation": self.camera_generation,
            "frame_id": self.frame_id,
            "present_counter": self.present_counter,
            "backend": _BACKEND_NAMES.get(self.backend, str(self.backend)),
        }
        if self.rtype == RecordType.INPUT:
            try:
                d["event_type"] = InputType(int(self.a0)).name
            except (ValueError, TypeError):
                d["event_type"] = str(self.a0)
            d["x"] = _num(self.a1)
            d["y"] = _num(self.a2)
            d["wheel_delta"] = _num(self.a3)
        elif self.rtype == RecordType.CAMERA_APPLY:
            d["center_x"] = _num(self.a1)
            d["center_y"] = _num(self.a2)
            d["world_width"] = _num(self.a3)
        elif self.rtype in (RecordType.SELECTOR, RecordType.FAST_PATH,
                            RecordType.SLOW_PATH, RecordType.RANGE_PUSH,
                            RecordType.GPU_UPLOAD,
                            RecordType.SAFETY_CORRECTION):
            d["a0"] = self.a0 if isinstance(self.a0, str) else _num(self.a0)
            d["a1"] = _num(self.a1)
        return d

    def __repr__(self) -> str:      # debugging aid only
        return (f"ProbeRecord({self.type_name}@{self.ts_ns} seq={self.event_seq} "
                f"gen={self.camera_generation} frame={self.frame_id} "
                f"present={self.present_counter})")


def _num(value: Any) -> Optional[float]:
    """Real number or None (JSON null == unavailable, never a fake 0)."""
    try:
        if value is None or isinstance(value, bool):
            return None
        v = float(value)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Statistics helpers                                                            #
# --------------------------------------------------------------------------- #
def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """Linear-interpolation percentile (numpy ``percentile`` convention).

    ``q`` is 0..100. Returns None for an empty sample - a missing metric is
    never reported as 0.
    """
    if not values:
        return None
    s = sorted(float(v) for v in values)
    if len(s) == 1:
        return s[0]
    pos = (float(q) / 100.0) * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def stats(values: Sequence[float], ndigits: int = 3) -> Optional[Dict[str, Any]]:
    """``{sample_count, p50, p95, p99, max}`` or None when there are no samples."""
    if not values:
        return None
    s = sorted(float(v) for v in values)
    return {
        "sample_count": len(s),
        "p50": round(percentile(s, 50), ndigits),
        "p95": round(percentile(s, 95), ndigits),
        "p99": round(percentile(s, 99), ndigits),
        "max": round(s[-1], ndigits),
    }


def sanitize_scenario(name: Optional[str]) -> str:
    """Filesystem-safe scenario label: ``[a-z0-9_]``, max 48 chars."""
    raw = str(name or "").strip().lower()
    if not raw:
        raw = str(os.environ.get(SCENARIO_ENV, "")).strip().lower()
    if not raw:
        raw = "adhoc"
    cleaned = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    return (cleaned or "adhoc")[:48]

# --------------------------------------------------------------------------- #
# Probe core                                                                  #
# --------------------------------------------------------------------------- #
class LatencyProbe:
    """Fixed-size ring buffer + generation-based correlation.

    All public ``record_*`` methods are safe to call from Qt / render paths:
    they never raise, never print per event, never write files, and do no
    work at all unless a capture is running.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        try:
            capacity = int(capacity)
        except (TypeError, ValueError):
            capacity = DEFAULT_CAPACITY
        self.capacity = max(1024, min(int(capacity), 1_000_000))
        self.records: deque = deque(maxlen=self.capacity)
        self.enabled = True
        self.capturing = False
        self.scenario = "adhoc"
        self.event_seq = 0
        self.capture_start_ns = 0
        self.capture_end_ns = 0
        self.capture_start_wall = ""
        self.dropped = 0
        self._startup_printed = False
        self._app_ref: Any = None
        self._last_frame_id = 0
        self._last_present = -1

    # -- lifecycle ------------------------------------------------------ #
    def start_capture(self, scenario: Optional[str] = None) -> str:
        self.scenario = sanitize_scenario(scenario)
        self.records.clear()
        self.dropped = 0
        self.capturing = True
        self.capture_start_ns = time.perf_counter_ns()
        self.capture_start_wall = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._print_backend_check()
        print("[GUI LATENCY]", flush=True)
        print(f"capture started\nscenario={self.scenario}", flush=True)
        return self.scenario

    def stop_capture(self) -> Dict[str, Any]:
        self.capture_end_ns = time.perf_counter_ns()
        self.capturing = False
        summary = self.correlate()
        path = self.write_json(summary)
        self.print_capture_summary(summary, path)
        return {"summary": summary, "path": str(path)}

    def _append(self, rec: ProbeRecord) -> None:
        if not self.capturing:
            return
        try:
            if len(self.records) == self.records.maxlen:
                self.dropped += 1
            self.records.append(rec)
        except Exception:
            pass

    # -- lightweight record hooks (never raise) ------------------------- #
    def record_input(self, event_type: int, x: float = 0.0, y: float = 0.0,
                     wheel_delta: float = 0.0,
                     camera_generation: int = -1) -> int:
        try:
            if not self.capturing:
                return -1
            self.event_seq += 1
            self._append(ProbeRecord(
                time.perf_counter_ns(), int(RecordType.INPUT), self.event_seq,
                int(camera_generation), -1, -1, BACKEND_UNKNOWN,
                int(event_type), float(x or 0.0), float(y or 0.0),
                float(wheel_delta or 0.0)))
            return self.event_seq
        except Exception:
            return -1

    def record_camera_apply(self, camera_generation: int, backend: int,
                            cx: float = 0.0, cy: float = 0.0,
                            world_width: float = 0.0) -> None:
        try:
            if not self.capturing:
                return
            self._append(ProbeRecord(
                time.perf_counter_ns(), int(RecordType.CAMERA_APPLY), 0,
                int(camera_generation), -1, -1, int(backend),
                0, float(cx or 0.0), float(cy or 0.0),
                float(world_width or 0.0)))
        except Exception:
            pass

    def record_frame_submit(self, frame_id: int, camera_generation: int,
                            backend: int) -> None:
        try:
            if not self.capturing:
                return
            try:
                self._last_frame_id = int(frame_id)
            except (TypeError, ValueError):
                pass
            self._append(ProbeRecord(
                time.perf_counter_ns(), int(RecordType.FRAME_SUBMIT), 0,
                int(camera_generation), int(frame_id), int(self._last_present),
                int(backend)))
        except Exception:
            pass

    def record_present(self, present_counter: int, frame_id: int = -1,
                       camera_generation: int = -1, backend: int = BACKEND_UNKNOWN,
                       native_count: int = -1) -> None:
        try:
            if not self.capturing:
                return
            try:
                self._last_present = int(present_counter)
            except (TypeError, ValueError):
                pass
            self._append(ProbeRecord(
                time.perf_counter_ns(), int(RecordType.PRESENT), 0,
                int(camera_generation), int(frame_id), int(present_counter),
                int(backend), int(native_count)))
        except Exception:
            pass

    def record_extra(self, rtype: int, camera_generation: int = -1,
                     frame_id: int = -1, a0: Any = 0, a1: Any = 0,
                     a2: Any = 0, a3: Any = 0,
                     backend: int = BACKEND_UNKNOWN) -> None:
        try:
            if not self.capturing:
                return
            self._append(ProbeRecord(
                time.perf_counter_ns(), int(rtype), 0, int(camera_generation),
                int(frame_id), int(self._last_present), int(backend),
                a0, a1, a2, a3))
        except Exception:
            pass
    # -- correlation ---------------------------------------------------- #
    def correlate(self) -> Dict[str, Any]:
        """Generation-based end-to-end correlation (PART 13).

        For each INPUT with a known generation, finds the first
        CAMERA_APPLY / FRAME_SUBMIT / PRESENT carrying a generation >= the
        input's generation. Inputs sharing one generation are COALESCED.
        """
        try:
            recs = list(self.records)
        except Exception:
            recs = []
        inputs = [r for r in recs if r.rtype == int(RecordType.INPUT)]
        applies = sorted(
            (r for r in recs if r.rtype == int(RecordType.CAMERA_APPLY)),
            key=lambda r: r.ts_ns)
        submits = sorted(
            (r for r in recs if r.rtype == int(RecordType.FRAME_SUBMIT)),
            key=lambda r: r.ts_ns)
        presents = sorted(
            (r for r in recs if r.rtype == int(RecordType.PRESENT)),
            key=lambda r: r.ts_ns)
        det = self.detect()
        vulkan_visible = bool(det.get("vulkan_main_viewport_active"))
        to_cam: List[float] = []
        to_sub: List[float] = []
        to_pre: List[float] = []
        coalesced = 0
        seen_gen: Dict[int, int] = {}
        correlated = 0
        for inp in inputs:
            gen = int(inp.camera_generation)
            if gen < 0:
                continue
            if gen in seen_gen:
                coalesced += 1
                continue
            seen_gen[gen] = 1
            a = next((r for r in applies
                      if int(r.camera_generation) >= gen), None)
            s = next((r for r in submits
                      if int(r.camera_generation) >= gen), None)
            p = None
            if vulkan_visible:
                if s is not None:
                    p = next((r for r in presents
                              if int(r.camera_generation) >= gen
                              or int(r.frame_id) >= int(s.frame_id)),
                             None)
                else:
                    p = next((r for r in presents
                              if int(r.camera_generation) >= gen), None)
            if a is not None:
                to_cam.append((a.ts_ns - inp.ts_ns) / 1e6)
            if s is not None:
                to_sub.append((s.ts_ns - inp.ts_ns) / 1e6)
            if p is not None:
                to_pre.append((p.ts_ns - inp.ts_ns) / 1e6)
            correlated += 1
        frame_iv = [ (submits[i].ts_ns - submits[i-1].ts_ns) / 1e6
                     for i in range(1, len(submits))]
        present_iv = [ (presents[i].ts_ns - presents[i-1].ts_ns) / 1e6
                       for i in range(1, len(presents))]
        n_fast = sum(1 for r in recs if r.rtype == int(RecordType.FAST_PATH))
        n_slow = sum(1 for r in recs if r.rtype == int(RecordType.SLOW_PATH))
        n_range = sum(1 for r in recs if r.rtype == int(RecordType.RANGE_PUSH))
        n_upl = sum(1 for r in recs if r.rtype == int(RecordType.GPU_UPLOAD))
        n_safe = sum(1 for r in recs
                     if r.rtype == int(RecordType.SAFETY_CORRECTION))
        sel = [r for r in recs if r.rtype == int(RecordType.SELECTOR)]
        sel_ms = [float(r.a0) for r in sel
                  if isinstance(r.a0, (int, float))]
        xyz_bytes = sum(int(r.a1) for r in recs
                        if r.rtype == int(RecordType.GPU_UPLOAD)
                        and r.a0 == UPLOAD_XYZ_BYTES)
        attr_bytes = sum(int(r.a1) for r in recs
                         if r.rtype == int(RecordType.GPU_UPLOAD)
                         and r.a0 == UPLOAD_ATTR_BYTES)
        normal_uploads = sum(int(r.a1) for r in recs
                             if r.rtype == int(RecordType.GPU_UPLOAD)
                             and r.a0 == UPLOAD_NORMAL_COUNT)
        slow_reasons: Dict[str, int] = {}
        for r in recs:
            if r.rtype == int(RecordType.SLOW_PATH):
                k = str(r.a0)
                slow_reasons[k] = slow_reasons.get(k, 0) + 1
        # None, not 0, when the stream manager never reported (e.g. a VTK-only
        # run): a missing metric must not look like a measured zero.
        emitted = bool(n_fast or n_slow or sel or n_upl or n_range)
        gens = sorted({int(r.camera_generation) for r in recs
                       if int(r.camera_generation) >= 0})
        return {
            "input_to_camera_ms": stats(to_cam),
            "input_to_submit_ms": stats(to_sub),
            "input_to_present_call_ms": (stats(to_pre)
                                         if vulkan_visible else None),
            "frame_interval_ms": stats(frame_iv),
            "present_interval_ms": (stats(present_iv)
                                    if vulkan_visible else None),
            "inputs": len(inputs),
            "correlated_inputs": correlated,
            "coalesced_inputs": coalesced,
            "camera_applies": len(applies),
            "frame_submits": len(submits),
            "presents": len(presents),
            "fast_frames": n_fast,
            "slow_frames": n_slow,
            "range_pushes": n_range,
            "gpu_uploads": n_upl,
            "safety_corrections": n_safe,
            "stream_manager_reported": emitted,
            "selector_derivations": len(sel) if emitted else None,
            "selector_ms": stats(sel_ms, 4),
            "xyz_upload_bytes": xyz_bytes if emitted else None,
            "attribute_upload_bytes": attr_bytes if emitted else None,
            "normal_uploads": normal_uploads if emitted else None,
            "slow_reasons": slow_reasons,
            "camera_generations": len(gens),
            "dropped": int(self.dropped),
        }
    # -- backend detection (PARTS 14/15/18/19) --------------------------- #
    def detect(self) -> Dict[str, Any]:
        """Backend honesty check. Never raises; missing app => VTK defaults."""
        out: Dict[str, Any] = {
            "requested_backend": "VTK",
            "active_backend": "VTK",
            "visible_main_viewport": "VTK",
            "vulkan_runtime_available": False,
            "vulkan_main_viewport_active": False,
            "camera_input_owner": "gui/app_window.py",
            "camera_mirror_owner": "gui/render_backend.py",
            "present_counter_available": False,
            "gpu_timestamp_available": False,
            "present_mode": "UNKNOWN",
            "present_mode_numeric": None,
            "swapchain_images": None,
            "swapchain_size": None,
            "swapchain_format": SWAPCHAIN_FORMAT_DOCUMENTED,
        }
        try:
            out["requested_backend"] = (
                "VULKAN" if os.environ.get(
                    "NAKSHA_VULKAN_MAIN_VIEWPORT", "").strip() in _ENV_TRUE
                else "VTK")
        except Exception:
            pass
        app = None
        try:
            app = self._app_ref() if self._app_ref is not None else None
        except Exception:
            app = None
        if app is None:
            return out
        try:
            rb = getattr(app, "render_backend", None)
            if rb is None:
                return out
            vb = getattr(rb, "vulkan_backend", None)
            out["vulkan_runtime_available"] = bool(
                vb is not None and getattr(vb, "_handle", None))
            owns = False
            try:
                fn = getattr(rb, "vulkan_owns_lidar_viewport", None)
                owns = bool(fn()) if callable(fn) else False
            except Exception:
                owns = False
            out["vulkan_main_viewport_active"] = owns
            if owns:
                out["active_backend"] = "VULKAN"
                out["visible_main_viewport"] = "VULKAN"
            try:
                pc = int(getattr(vb, "present_count", -1) or -1)
                out["present_counter_available"] = bool(
                    out["vulkan_runtime_available"] and pc >= 0)
            except Exception:
                pass
            try:
                from gui.naksha_cache import frame_timings as _ft
                note = getattr(_ft, "gpu_note", "")
                out["gpu_timestamp_available"] = bool(
                    note and "NOT available" not in str(note))
            except Exception:
                out["gpu_timestamp_available"] = False
        except Exception:
            pass
        return out
    def _print_backend_check(self) -> None:
        try:
            d = self.detect()
            print("[GUI LATENCY BACKEND CHECK]", flush=True)
            print("requested_backend:", flush=True)
            print(d["requested_backend"], flush=True)
            print("active_backend:", flush=True)
            print(d["active_backend"], flush=True)
            print("visible_main_viewport:", flush=True)
            print(d["visible_main_viewport"], flush=True)
            print("vulkan_runtime_available:", flush=True)
            print("YES" if d["vulkan_runtime_available"] else "NO",
                  flush=True)
            print("vulkan_main_viewport_active:", flush=True)
            print("YES" if d["vulkan_main_viewport_active"] else "NO",
                  flush=True)
            print("camera_input_owner:", flush=True)
            print(d["camera_input_owner"], flush=True)
            print("camera_mirror_owner:", flush=True)
            print(d["camera_mirror_owner"], flush=True)
            print("present_counter_available:", flush=True)
            print("YES" if d["present_counter_available"] else "NO",
                  flush=True)
            print("gpu_timestamp_available:", flush=True)
            print("YES" if d["gpu_timestamp_available"] else "NO",
                  flush=True)
            if d["vulkan_main_viewport_active"]:
                print("BACKEND = VULKAN", flush=True)
            else:
                print("BACKEND = VTK", flush=True)
        except Exception:
            pass

    def print_startup_block(self) -> None:
        if self._startup_printed:
            return
        self._startup_printed = True
        try:
            d = self.detect()
            print("=" * 60, flush=True)
            print("PHASE 4B LATENCY PROBE", flush=True)
            print("=" * 60, flush=True)
            print("probe enabled:", flush=True)
            print("YES", flush=True)
            print("visible backend:", flush=True)
            print(d["visible_main_viewport"], flush=True)
            print("Vulkan runtime available:", flush=True)
            print("YES" if d["vulkan_runtime_available"] else "NO",
                  flush=True)
            print("Vulkan main viewport:", flush=True)
            print("YES" if d["vulkan_main_viewport_active"] else "NO",
                  flush=True)
            print("camera owner:", flush=True)
            print(d["camera_input_owner"], flush=True)
            print("native present counter:", flush=True)
            print("YES" if d["present_counter_available"] else "NO",
                  flush=True)
            print("GPU timestamps:", flush=True)
            v = "AVAILABLE" if d["gpu_timestamp_available"] else "UNAVAILABLE"
            print(v, flush=True)
            print("present mode:", flush=True)
            print(d["present_mode"], flush=True)
            print("swapchain images:", flush=True)
            print(d["swapchain_images"], flush=True)
            print("capture:", flush=True)
            print("IDLE", flush=True)
            print("=" * 60, flush=True)
        except Exception:
            pass
    # -- JSON output (PART 11) + capture summary (PART 22) ------------- #
    def write_json(self, summary: Dict[str, Any],
                   out_dir: Optional[Path] = None) -> Path:
        det = self.detect()
        vulkan_visible = bool(det.get("vulkan_main_viewport_active"))
        try:
            dest = Path(out_dir) if out_dir is not None else DEFAULT_OUTPUT_DIR
        except Exception:
            dest = DEFAULT_OUTPUT_DIR
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        stamp = self.capture_start_wall or datetime.now().strftime(
            "%Y%m%d_%H%M%S")
        fname = f"phase4b_{self.scenario}_{stamp}.json"
        path = dest / fname
        try:
            events = [r.as_dict() for r in list(self.records)]
        except Exception:
            events = []
        payload = {
            "schema": SCHEMA,
            "metadata": {
                "scenario": self.scenario,
                "capture_start_ns": self.capture_start_ns,
                "capture_end_ns": self.capture_end_ns,
                "capture_start_wall": self.capture_start_wall,
                "duration_ms": ((self.capture_end_ns - self.capture_start_ns)
                                / 1e6 if self.capture_end_ns
                                > self.capture_start_ns else None),
                "sample_counts": {
                    "records": len(events),
                    "dropped": int(self.dropped),
                    "capacity": int(self.capacity),
                },
            },
            "runtime": {
                "backend": det,
                "visible_backend": det.get("visible_main_viewport"),
                "vulkan_present_valid": vulkan_visible,
                "gpu_timing": ("AVAILABLE" if det.get(
                    "gpu_timestamp_available") else "UNAVAILABLE"),
                "env": {
                    "NAKSHA_VULKAN_MAIN_VIEWPORT": os.environ.get(
                        "NAKSHA_VULKAN_MAIN_VIEWPORT", ""),
                    "NAKSHA_LATENCY_PROBE": os.environ.get(ENABLE_ENV, ""),
                    "NAKSHA_LATENCY_SCENARIO": os.environ.get(
                        SCENARIO_ENV, ""),
                },
            },
            "summary": summary,
            "events": events,
        }
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1, default=str)
        except Exception:
            pass
        return path

    @staticmethod
    def _fmt_block(block: Optional[Dict[str, Any]]) -> str:
        if not block:
            return "n/a"
        g = lambda k: block.get(k)
        return (f"n={g('sample_count')} p50={g('p50')} "
                f"p95={g('p95')} p99={g('p99')} max={g('max')}")

    def print_capture_summary(self, summary: Dict[str, Any],
                              path: Path) -> None:
        try:
            det = self.detect()
            backend = det.get("visible_main_viewport", "VTK")
            print("=" * 60, flush=True)
            print("PHASE 4B CAPTURE SUMMARY", flush=True)
            print("=" * 60, flush=True)
            print(f"scenario:\n{self.scenario}", flush=True)
            print(f"backend:\n{backend}", flush=True)
            print(f"samples:\n{summary.get('inputs')}", flush=True)
            print("input->camera:", flush=True)
            print(self._fmt_block(summary.get("input_to_camera_ms")),
                  flush=True)
            print("input->submit:", flush=True)
            print(self._fmt_block(summary.get("input_to_submit_ms")),
                  flush=True)
            print("input->present-call:", flush=True)
            print(self._fmt_block(summary.get("input_to_present_call_ms")),
                  flush=True)
            print("frame interval:", flush=True)
            print(self._fmt_block(summary.get("frame_interval_ms")),
                  flush=True)
            print(f"fast frames:\n{summary.get('fast_frames')}",
                  flush=True)
            print(f"slow frames:\n{summary.get('slow_frames')}",
                  flush=True)
            rep = summary.get("stream_manager_reported")
            na = "n/a (stream manager reported nothing)"
            print("selector derivations:", flush=True)
            print(summary.get("selector_derivations") if rep else na,
                  flush=True)
            print("selector ms:", flush=True)
            print(self._fmt_block(summary.get("selector_ms")), flush=True)
            print(f"range pushes:\n{summary.get('range_pushes')}",
                  flush=True)
            print("XYZ upload bytes:", flush=True)
            print(summary.get("xyz_upload_bytes") if rep else na, flush=True)
            print("attribute upload bytes (class+intensity+normal, one "
                  "native counter):", flush=True)
            print(summary.get("attribute_upload_bytes") if rep else na,
                  flush=True)
            print("normal tile uploads:", flush=True)
            print(summary.get("normal_uploads") if rep else na, flush=True)
            print("slow-path reasons:", flush=True)
            print(summary.get("slow_reasons") or {}, flush=True)
            print("safety corrections:", flush=True)
            print(summary.get("safety_corrections"), flush=True)
            print(f"output:\n{path}", flush=True)
            print("=" * 60, flush=True)
        except Exception:
            pass
    # -- timer audit (PART 20: output only, never modified) -------------- #
    def timer_audit(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            import inspect as _inspect
            from gui import gpu_render_manager as _grm
            src = _inspect.getsource(_grm)
            delay = None
            try:
                m = re.search(r"render_delay_ms\s*=\s*(\d+)", src)
                if m:
                    delay = int(m.group(1))
            except Exception:
                delay = None
            out.append({"timer": "gpu_render_manager debounce",
                        "interval_ms": delay,
                        "targets": "VTK",
                        "source": "gui/gpu_render_manager.py"})
        except Exception:
            pass
        # Read from gui/naksha_cache/app_streaming.py::_install_frame_tick: this
        # timer calls mgr.on_frame() - the stream manager that drives the Vulkan
        # point renderer - so it is NOT a VTK-only timer. (Qt's coarse timer may
        # stretch 16 ms on Windows; the real period is what the capture's frame
        # interval measures, not this label.)
        out.append({"timer": "stream QTimer (mgr.on_frame tick)",
                    "interval_ms": 16,
                    "targets": "stream manager / Vulkan point renderer",
                    "source": "gui/naksha_cache/app_streaming.py:408"})
        out.append({"timer": "interaction cadence",
                    "interval_ms": 33,
                    "targets": "both",
                    "source": "code-derived default"})
        return out
# --------------------------------------------------------------------------- #
# Singleton + zero-cost hook entry points                                     #
# --------------------------------------------------------------------------- #
_PROBE: Optional[LatencyProbe] = None


def _capacity_from_env() -> int:
    try:
        raw = str(os.environ.get(CAPACITY_ENV, "")).strip()
        if raw:
            return max(1024, min(int(raw), 1_000_000))
    except (TypeError, ValueError):
        pass
    return DEFAULT_CAPACITY


def _probe_enabled_in_env() -> bool:
    try:
        return os.environ.get(ENABLE_ENV, "").strip().lower() in _ENV_TRUE
    except Exception:
        return False


def get_probe() -> Optional[LatencyProbe]:
    """Configured probe or None. Enabling needs NAKSHA_LATENCY_PROBE=1."""
    global _PROBE
    if _PROBE is not None:
        return _PROBE if _PROBE.enabled else None
    if not _probe_enabled_in_env():
        return None
    try:
        _PROBE = LatencyProbe(capacity=_capacity_from_env())
    except Exception:
        _PROBE = None
    return _PROBE


def ensure_probe_for_tests(capacity: int = 4096) -> LatencyProbe:
    """Test-only constructor (bypasses the env gate)."""
    global _PROBE
    _PROBE = LatencyProbe(capacity=capacity)
    return _PROBE


def reset_probe_for_tests() -> None:
    global _PROBE
    _PROBE = None


def current_generation(app: Any = None) -> int:
    """Read the EXISTING canonical generation. Never mints a new one."""
    try:
        mgr = getattr(app, "naksha_stream", None)
        if mgr is not None and hasattr(mgr, "camera_generation"):
            return int(getattr(mgr, "camera_generation", -1) or -1)
    except Exception:
        pass
    try:
        if _PROBE is not None:
            probe_app = None
            try:
                probe_app = (_PROBE._app_ref()
                             if _PROBE._app_ref is not None else None)
            except Exception:
                probe_app = None
            mgr = getattr(probe_app, "naksha_stream", None)
            if mgr is not None and hasattr(mgr, "camera_generation"):
                return int(getattr(mgr, "camera_generation", -1) or -1)
    except Exception:
        pass
    return -1
def hook_input(event_type: int, x: float = 0.0, y: float = 0.0,
               wheel_delta: float = 0.0, app: Any = None) -> int:
    """INPUT hook: one int compare when the probe is off. Never raises."""
    p = _PROBE
    if p is None:
        if not _probe_enabled_in_env():
            return -1
        p = get_probe()
        if p is None:
            return -1
    try:
        if not p.capturing:
            return -1
        return p.record_input(int(event_type), float(x or 0.0),
                              float(y or 0.0), float(wheel_delta or 0.0),
                              current_generation(app))
    except Exception:
        return -1


def hook_camera_apply(camera_generation: int, backend: int,
                      cx: float = 0.0, cy: float = 0.0,
                      world_width: float = 0.0) -> None:
    p = _PROBE
    if p is None or not p.capturing:
        return
    try:
        p.record_camera_apply(int(camera_generation), int(backend),
                              float(cx or 0.0), float(cy or 0.0),
                              float(world_width or 0.0))
    except Exception:
        pass


def hook_frame_submit(frame_id: int, camera_generation: int,
                      backend: int) -> None:
    p = _PROBE
    if p is None or not p.capturing:
        return
    try:
        p.record_frame_submit(int(frame_id), int(camera_generation),
                              int(backend))
    except Exception:
        pass


def hook_present(present_counter: int, frame_id: int = -1,
                 camera_generation: int = -1, backend: int = BACKEND_UNKNOWN,
                 native_count: int = -1) -> None:
    p = _PROBE
    if p is None or not p.capturing:
        return
    try:
        p.record_present(int(present_counter), int(frame_id),
                         int(camera_generation), int(backend),
                         int(native_count))
    except Exception:
        pass


def hook_extra(rtype: int, camera_generation: int = -1,
               frame_id: int = -1, backend: int = BACKEND_UNKNOWN,
               a0: Any = 0, a1: Any = 0, a2: Any = 0, a3: Any = 0) -> None:
    p = _PROBE
    if p is None or not p.capturing:
        return
    try:
        p.record_extra(int(rtype), int(camera_generation), int(frame_id),
                       a0, a1, a2, a3, backend=int(backend))
    except Exception:
        pass
def toggle_capture(scenario: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Ctrl+Shift+L handler: start when idle, stop + report when running."""
    p = get_probe()
    if p is None:
        return None
    try:
        if p.capturing:
            return p.stop_capture()
        p.start_capture(scenario)
        return None
    except Exception:
        return None


def attach_app(app: Any) -> Optional[LatencyProbe]:
    """Bind the probe to the live app once. Prints startup block, idles."""
    p = get_probe()
    if p is None:
        return None
    try:
        p._app_ref = weakref.ref(app)
    except Exception:
        try:
            p._app_ref = lambda: app
        except Exception:
            pass
    try:
        from PySide6.QtGui import QAction as _QAction
        from PySide6.QtGui import QKeySequence as _QKS
        act = _QAction(app)
        try:
            act.setShortcut(_QKS("Ctrl+Shift+L"))
        except Exception:
            pass
        try:
            app.addAction(act)
        except Exception:
            pass
        try:
            act.triggered.connect(lambda _c=False: toggle_capture())
        except Exception:
            pass
    except Exception:
        pass
    p.print_startup_block()
    return p

