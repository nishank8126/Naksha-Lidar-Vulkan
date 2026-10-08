"""StreamTelemetry: structured JSONL telemetry for the Stage 3C streaming path.

One JSON object per line. Consumers (st3c_app_one.py, analyze_stage3c.py) read
``stage3c_viewport_telemetry.jsonl``. Each sample/event carries the fields the
spec's TELEMETRY FILE section lists where applicable:

    timestamp, dataset_mode, camera_generation, viewport (w,h,scale),
    visible_pages, visible_microcells, lod_distribution,
    requested_points, decoded_points, drawn_points,
    ram_resident_points, ram_resident_bytes,
    gpu_resident_points, gpu_resident_bytes,
    request_queue_depth, ram_hits, ram_misses,
    gpu_hits, gpu_misses, uploads, evictions,
    stale_cancellations, position_reuploads,
    streaming_full_point_upload_attempts,
    frame_time_ms, present_count, fps, process_rss
"""
from __future__ import annotations

import json
import os
import time
import threading

import numpy as np


class StreamTelemetry:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # truncate at session start so analyzers see only the current run
        with self._lock, open(self.path, "w", encoding="utf-8") as f:
            f.write("")
        self._start = time.perf_counter()
        self._last_present = None
        self._frames = 0
        self._present_count = 0
        self._frame_times = []

    def _rss(self):
        try:
            import psutil
            return psutil.Process().memory_info().rss
        except Exception:
            try:  # Windows fallback
                import ctypes
                k32 = ctypes.windll.kernel32
                p = ctypes.c_ulong()
                if k32.GetProcessMemoryInfo(ctypes.c_void_p(0),
                                             ctypes.byref(p), ctypes.sizeof(p)):
                    return p.value
            except Exception:
                pass
            return 0

    def sample(self, **fields) -> None:
        """Emit a telemetry sample (one JSONL line)."""
        ev = {
            "timestamp": time.perf_counter() - self._start,
            "process_rss": self._rss(),
        }
        ev.update(fields)
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(ev, default=_default) + "\n")
            except Exception:
                pass

    def event(self, name: str, **fields) -> None:
        """Emit a named event sample."""
        self.sample(event=name, **fields)

    def note_present(self, wall_ms: float, fps: float = 0.0) -> None:
        """Called by the render path on every PRESENTED frame. Feeds the
        frame-timing histogram used by analyze_stage3c.py."""
        self._frames += 1
        self._present_count += 1
        self._frame_times.append(wall_ms)
        if len(self._frame_times) > 200_000:
            self._frame_times = self._frame_times[-100_000:]
        self.sample(
            event="frame_presented",
            frame_time_ms=wall_ms,
            present_count=self._present_count,
            fps=fps,
        )

    @property
    def frame_times(self):
        return list(self._frame_times)


def _default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.ndarray,)):
        return o.tolist()
    return str(o)
