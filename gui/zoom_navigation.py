"""Small, dependency-free helpers for responsive camera navigation."""

from __future__ import annotations

import math


DEFAULT_WHEEL_ZOOM_FACTOR = 1.10

# Physical 2D wheel input uses the immediate controller instead of the legacy
# eased VTK path.  A 1.45 multiplier is deliberately quick while the bounded,
# continuous exponent keeps high-resolution wheels and trackpads controllable.
FAST_WHEEL_ZOOM_FACTOR = 1.45
MAX_WHEEL_STEPS_PER_EVENT = 4.0


def fast_wheel_zoom_factor(
    wheel_delta: float,
    *,
    base_factor: float = FAST_WHEEL_ZOOM_FACTOR,
    max_steps: float = MAX_WHEEL_STEPS_PER_EVENT,
) -> float:
    """Convert raw Qt wheel units to a bounded reciprocal zoom multiplier."""
    delta = float(wheel_delta)
    base = float(base_factor)
    step_limit = float(max_steps)

    if not math.isfinite(delta):
        raise ValueError("wheel_delta must be finite")
    if not math.isfinite(base) or base <= 1.0:
        raise ValueError("base_factor must be finite and greater than one")
    if not math.isfinite(step_limit) or step_limit <= 0.0:
        raise ValueError("max_steps must be finite and greater than zero")

    steps = max(-step_limit, min(step_limit, delta / 120.0))
    return base**steps


def qt_position_to_vtk_display(
    position_x: float,
    position_y: float,
    qt_width: float,
    qt_height: float,
    render_width: float,
    render_height: float,
) -> tuple[float, float]:
    """Map a top-left Qt position to bottom-left VTK display coordinates.

    Using both widget and render-window sizes also handles high-DPI canvases
    without relying on a global device-pixel-ratio assumption.
    """
    values = (
        float(position_x),
        float(position_y),
        float(qt_width),
        float(qt_height),
        float(render_width),
        float(render_height),
    )
    if not all(math.isfinite(value) for value in values):
        raise ValueError("zoom position and viewport sizes must be finite")

    x, y, q_width, q_height, r_width, r_height = values
    if q_width <= 0.0 or q_height <= 0.0 or r_width <= 0.0 or r_height <= 0.0:
        raise ValueError("viewport sizes must be greater than zero")

    vtk_x = x * (r_width / q_width)
    vtk_y = (q_height - y) * (r_height / q_height)
    return (
        max(0.0, min(r_width - 1.0, vtk_x)),
        max(0.0, min(r_height - 1.0, vtk_y)),
    )


def accumulate_wheel_delta(
    accumulated_delta: int,
    event_delta: int,
    *,
    notch_delta: int = 120,
) -> tuple[int, int]:
    """
    Accumulate a Qt wheel delta and return ``(next_delta, direction)``.

    Direction is ``1`` for forward, ``-1`` for backward, or ``0`` until a
    complete notch has accumulated.  Resetting after one notch intentionally
    matches ``pyvistaqt.QtInteractor.wheelEvent``.
    """
    threshold = int(notch_delta)
    if threshold <= 0:
        raise ValueError("notch_delta must be greater than zero")

    total = int(accumulated_delta) + int(event_delta)
    if total >= threshold:
        return 0, 1
    if total <= -threshold:
        return 0, -1
    return total, 0


def eased_zoom_step(
    remaining_factor: float,
    *,
    ease_fraction: float = 0.45,
    settle_log_epsilon: float = 0.0015,
    max_step_factor: float = 1.15,
) -> tuple[float, float, bool]:
    """
    Return ``(step, next_remaining, settled)`` for a zoom animation frame.

    Easing in log space makes zoom-in and zoom-out exactly symmetric.  The
    per-frame cap keeps a fast wheel burst controllable, while the settle snap
    prevents a single wheel notch from producing dozens of near-no-op frames.
    """
    remaining = float(remaining_factor)
    if not math.isfinite(remaining) or remaining <= 0.0:
        raise ValueError("remaining_factor must be finite and greater than zero")
    if not 0.0 < ease_fraction <= 1.0:
        raise ValueError("ease_fraction must be in the range (0, 1]")
    if settle_log_epsilon < 0.0:
        raise ValueError("settle_log_epsilon cannot be negative")
    if not math.isfinite(max_step_factor) or max_step_factor <= 1.0:
        raise ValueError("max_step_factor must be finite and greater than one")

    log_remaining = math.log(remaining)
    if abs(log_remaining) <= settle_log_epsilon:
        return remaining, 1.0, True

    max_log_step = math.log(max_step_factor)
    log_step = max(
        -max_log_step,
        min(max_log_step, log_remaining * ease_fraction),
    )
    step = math.exp(log_step)
    next_remaining = remaining / step

    if abs(math.log(next_remaining)) <= settle_log_epsilon:
        return remaining, 1.0, True
    return step, next_remaining, False


def interaction_frame_interval_ms(
    point_count: int,
    *,
    base_interval_ms: int = 33,
) -> int:
    """
    Choose an interaction-only render interval for a point cloud.

    Camera updates continue at the UI timer rate; only costly intermediate
    repaints are coalesced.  Small and ordinary clouds retain the existing
    33 ms behavior.
    """
    count = max(0, int(point_count or 0))
    base = max(1, int(base_interval_ms))

    if count >= 30_000_000:
        return max(base, 80)
    if count >= 15_000_000:
        return max(base, 66)
    if count >= 5_000_000:
        return max(base, 50)
    return base


def pan_frame_interval_ms(
    point_count: int,
    *,
    base_interval_ms: int = 16,
) -> int:
    """Return a responsive main-pan frame target for full-fidelity clouds.

    The render scheduler always keeps only one pending frame, so requesting a
    faster target cannot build a render queue. If drawing the actor takes
    longer than this budget, the GPU's real render duration becomes the cap.
    """
    count = max(0, int(point_count or 0))
    base = max(1, int(base_interval_ms))

    if count >= 30_000_000:
        return max(base, 33)
    if count >= 15_000_000:
        return max(base, 30)
    if count >= 5_000_000:
        return max(base, 24)
    return base
