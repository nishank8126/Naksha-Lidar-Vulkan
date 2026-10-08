"""instant_shaded_telemetry.py - structured JSONL session recorder.

THIS IS INSTRUMENTATION ONLY. It observes; it never decides.

The recorder is deliberately incapable of affecting the renderer:

  * every write goes through gui.naksha_telemetry.safe_emit_telemetry, so a
    telemetry failure can NEVER change a render result, a fallback decision or
    a return value;
  * ``emit()`` swallows everything and returns False on failure;
  * no analyzer or verdict logic lives in this module or anywhere on the render
    path. The analyzer runs AFTER the session, on the file only.

SCHEMA (v1)
===========
One JSON object per line. Every event carries at minimum::

    schema_version  always 1
    session_id      unique per run (NAKSHA_TEST_SESSION_ID)
    timestamp       ISO-8601 UTC
    event           event name
    renderer        renderer that produced it

NULL SEMANTICS (critical - this is what stops telemetry inventing failures)
=========================================================================
``0``    = authoritatively measured, and nothing happened.
``null`` = the counter is unavailable on this path / not exported.

They are NOT interchangeable. An analyzer must never read ``null`` as
"non-zero" or "an upload happened". Unavailable metrics are emitted as JSON
``null`` - never as the strings "n/a", "None" or "unknown".
"""

from __future__ import annotations

import atexit
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from gui.naksha_telemetry import safe_emit_telemetry

SCHEMA_VERSION = 1
SESSION_ENV = "NAKSHA_TEST_SESSION_ID"
LOG_DIR_ENV = "NAKSHA_TEST_TELEMETRY_DIR"

# The renderer the Instant Shaded path actually implements. It reconstructs
# normals in SCREEN SPACE from the depth it just wrote, so there is no stored
# normal buffer at all: normal_represent is reported honestly instead of
# implying an oct16x2 cache that does not exist in this implementation.
NORMAL_REPRESENT = "screen_space_reconstruction"

LOCK = threading.RLock()
_RECORDER: Optional["SessionRecorder"] = None


def num(value: Any) -> Optional[float]:
    """Coerce to a real number, else None. Keeps "unavailable" honest and JSON
    null - never the string "n/a"."""
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def flag(value: Any) -> Optional[bool]:
    """Coerce to a real bool, else None (absent, not False)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    try:
        return bool(value)
    except Exception:
        return None


def new_session_id(now: Optional[datetime] = None, pid: Optional[int] = None) -> str:
    """Deterministic-ish unique id: 20261003_112455_12345."""
    now = now or datetime.now()
    pid = os.getpid() if pid is None else pid
    return f"{now.strftime('%Y%m%d_%H%M%S')}_{pid}"


class SessionRecorder:
    def __init__(self, session_id: str, log_path: Path, log_dir: Path):
        self.session_id = str(session_id)
        self.log_path = Path(log_path)
        self.log_dir = Path(log_dir)
        self.started_monotonic = time.monotonic()
        self.closed = False
        self.last_render_mode: Optional[str] = None
        self.counters: Dict[str, int] = {}
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, renderer: str = "naksha", **fields: Any) -> bool:
        """Append ONE event. Never raises. Returns True only when written."""
        if self is None or self.closed:
            return False
        fields.setdefault("normal_represent", NORMAL_REPRESENT)
        record: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": str(event),
            "renderer": str(renderer),
            "monotonic_ms": round((time.monotonic() - self.started_monotonic) * 1000.0, 3),
        }
        record.update(fields)
        return safe_emit_telemetry(
            "TELEMETRY WRITE",
            {"session": self.session_id, "event": record["event"]},
            printer=lambda _m: self._write(record),
            rate_limit=False,
        )

    def _write(self, record: Dict[str, Any]) -> None:
        with LOCK:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False,
                                    allow_nan=False, default=str) + "\n")

    def swap_render_mode(self, new_mode: Optional[str]) -> Optional[str]:
        """Returns the mode the renderer was in before this one. Records the
        mode; it does not decide anything."""
        previous = self.last_render_mode
        self.last_render_mode = new_mode
        return previous

    def note(self, key: str, delta: int = 1) -> None:
        with LOCK:
            self.counters[key] = int(self.counters.get(key, 0)) + int(delta)

    def _close_impl(self, reason: str) -> bool:
        try:
            with LOCK:
                with self.log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "schema_version": SCHEMA_VERSION,
                        "session_id": self.session_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(
                            timespec="milliseconds"),
                        "event": "session_end",
                        "renderer": "naksha",
                        "reason": str(reason),
                        "counters": dict(self.counters),
                    }, ensure_ascii=False, default=str) + "\n")
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    return bool(str(os.environ.get(SESSION_ENV, "")).strip())


def default_log_dir(root: Optional[Path] = None) -> Path:
    override = str(os.environ.get(LOG_DIR_ENV, "")).strip()
    if override:
        return Path(override)
    root = root or Path(__file__).resolve().parents[1]
    return root / "logs" / "instant_shaded"


def configure(session_id: Optional[str] = None,
               log_dir: Optional[Path] = None) -> Optional["SessionRecorder"]:
    """Create the session recorder. Idempotent: a second call returns the
    existing recorder so a reload cannot open a second log for one session."""
    global _RECORDER
    with LOCK:
        if _RECORDER is not None:
            return _RECORDER
        sid = str(session_id or os.environ.get(SESSION_ENV, "")).strip()
        if not sid:
            return None
        directory = Path(log_dir or default_log_dir())
        recorder = SessionRecorder(sid, directory / f"instant_shaded_{sid}.jsonl",
                                   directory)
        try:
            (directory / "latest.txt").write_text(str(recorder.log_path),
                                                  encoding="utf-8")
        except Exception:
            pass   # convenience pointer only - never fatal
        _RECORDER = recorder
        # Safety net: if the application dies without a graceful shutdown the
        # log still ends with session_end, so the analyzer can tell "closed
        # cleanly" from "cut short" instead of guessing.
        atexit.register(lambda: close("atexit"))
        return recorder


def get() -> Optional["SessionRecorder"]:
    if _RECORDER is None and is_enabled():
        return configure()
    return _RECORDER


def emit(event: str, renderer: str = "naksha", **fields: Any) -> bool:
    """Emit one event if a session is active. Always safe; False when inactive."""
    try:
        recorder = get()
        if recorder is None:
            return False
        return recorder.emit(event, renderer=renderer, **fields)
    except Exception:
        return False


def log_path() -> Optional[Path]:
    recorder = get()
    return recorder.log_path if recorder is not None else None


def close(reason: str = "normal") -> bool:
    global _RECORDER
    try:
        recorder = _RECORDER
        if recorder is None:
            return False
        _RECORDER = None
        return recorder._close_impl(reason)
    except Exception:
        return False


__all__ = [
    "SCHEMA_VERSION", "SESSION_ENV", "LOG_DIR_ENV", "SessionRecorder",
    "is_enabled", "configure", "get", "emit", "close", "log_path",
    "default_log_dir", "new_session_id", "num", "flag",
]
