"""naksha_telemetry.py - the ONE safe telemetry boundary.

THE RULE THIS MODULE ENFORCES
=============================
Business / render logic determines success.  Telemetry only records it.

    operation_success = perform_real_operation()   # may legitimately be False
    telemetry_success = safe_emit_telemetry(...)    # can NEVER change the above
    return operation_success

Telemetry must NEVER control renderer success, the value a rendering call
returns, render-mode selection, whether a fallback renderer is chosen, Vulkan
handoff success, DatasetMode state, project state, or any UI success/failure.

A telemetry failure produces, at most, one ``[TELEMETRY WARNING]`` line plus a
counter. It never raises into a caller's render path and never turns a
successful render into a failure.

WHY IT EXISTS
=============
``gui/shading_display.py::_enter_instant_shaded`` was written as::

    try:
        ok = <renderer work>                        # succeeds, marks ACTIVE
        xyz_after = b.get_point_position_upload_count()    # telemetry
        print(f'... {b.get_instant_shaded_frame_count()}')  # telemetry
        return True
    except Exception:
        return False        # <-- telemetry failure became a RENDER failure

so any failure in a counter read, a frame-timing read, an f-string or the
``print`` itself - long after the renderer had already been activated - made the
whole handoff report FAILED and sent the caller down the legacy Delaunay path.
Every telemetry/logging hook on a render path now goes through
``safe_emit_telemetry``.

DEV-ONLY FAILURE INJECTION
==========================
``NAKSHA_TEST_TELEMETRY_FAIL`` forces the boundary to fail so the isolation is
proven rather than asserted: ``1``/``raise`` raises inside the boundary,
``return`` returns False, ``format`` blows up during payload formatting. It is
off unless explicitly set and never affects render output.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Dict, Optional

# Repeated identical telemetry failures are collapsed so a per-frame hook cannot
# flood the console. The counters below still count EVERY failure.
RATE_LIMIT_SECONDS = 2.0

_LOCK = threading.RLock()
_STATS: Dict[str, int] = {
    "emitted": 0,
    "failed": 0,
    "forced_failures": 0,
    "rate_limited": 0,
}
_LAST_WARNING: Dict[str, float] = {}


def telemetry_injection_mode() -> str:
    """Current NAKSHA_TEST_TELEMETRY_FAIL mode: '', 'raise', 'return', 'format'."""
    try:
        raw = str(os.environ.get("NAKSHA_TEST_TELEMETRY_FAIL", "")).strip().lower()
    except Exception:
        return ""
    if not raw:
        return ""
    if raw in ("1", "true", "yes", "on", "raise", "raise_on_error"):
        return "raise"
    if raw in ("return", "false", "0", "off", "no", "return_false"):
        return "return"
    if raw in ("format", "fmt"):
        return "format"
    return "raise"


def telemetry_stats() -> Dict[str, int]:
    """Health of the telemetry channel itself - deliberately NOT the renderer
    result. Anything that wants "did it render" must ask the renderer."""
    with _LOCK:
        return dict(_STATS)


def _bump(key: str) -> None:
    with _LOCK:
        _STATS[key] = _STATS.get(key, 0) + 1


def _rate_limited(event: str) -> bool:
    """True when this event was warned about recently enough to stay quiet."""
    now = time.monotonic()
    with _LOCK:
        if (now - _LAST_WARNING.get(event, -1e18)) < RATE_LIMIT_SECONDS:
            _STATS["rate_limited"] += 1
            return True
        _LAST_WARNING[event] = now
        return False


def safe_emit_telemetry(event: str,
                        payload: Optional[Dict[str, Any]] = None,
                        printer: Optional[Callable[[str], Any]] = print,
                        rate_limit: bool = True) -> bool:
    """Emit ONE telemetry record. NEVER raises. NEVER returns anything the
    renderer is meant to act on.

    Returns True when the record was emitted, False when telemetry failed.
    Callers on a render/business path MUST ignore that return value - it exists
    only so tests and the telemetry-health report can observe the channel.
    """
    mode = telemetry_injection_mode()
    try:
        if mode == "raise":
            _bump("forced_failures")
            raise RuntimeError(
                "injected telemetry failure (NAKSHA_TEST_TELEMETRY_FAIL)")
        if mode == "return":
            _bump("forced_failures")
            return False
        if mode == "format":
            _bump("forced_failures")
            # Hostile payload on purpose: formatting happens INSIDE this
            # boundary, so a bad value can never reach the renderer.
            payload = dict(payload or {})
            payload["__forced__"] = object()  # repr cannot fail, format can
            body = " ".join(f"{k}={'%d' % (v,)}" for k, v in sorted(payload.items()))
            if printer is not None:
                printer(f"[{event}] {body}")
            _bump("emitted")
            return True

        if printer is not None:
            body = " ".join(f"{k}={v}" for k, v in sorted(dict(payload or {}).items()))
            printer(f"[{event}] {body}".rstrip())
        _bump("emitted")
        return True
    except Exception as exc:  # noqa: BLE001 - this IS the boundary
        _bump("failed")
        if rate_limit and _rate_limited(str(event)):
            return False
        try:
            print(
                "[TELEMETRY WARNING]\n"
                f"event: {event}\n"
                f"exception: {type(exc).__name__}: {exc}\n"
                "renderer_state: UNCHANGED\n"
                "operation_result: UNCHANGED"
            )
        except Exception:  # pragma: no cover - stdout itself is gone
            pass
        return False


def operation_telemetry_result(operation_success: bool,
                               telemetry_ok: bool) -> str:
    """Keeps the two verdicts separate instead of collapsing them into one
    boolean. The renderer result is what acceptance depends on; telemetry health
    is a separate, non-blocking WARN.

    ``telemetry_ok`` is deliberately unused in the verdict - that is the point.
    """
    _ = telemetry_ok
    return "PASS" if operation_success else "FAIL"


__all__ = [
    "safe_emit_telemetry",
    "telemetry_stats",
    "telemetry_injection_mode",
    "operation_telemetry_result",
    "RATE_LIMIT_SECONDS",
]
