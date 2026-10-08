"""Small, dependency-free helpers for responsive camera navigation."""

from __future__ import annotations

import math
import os
import time


DEFAULT_WHEEL_ZOOM_FACTOR = 1.10

# Physical 2D wheel input uses the immediate controller instead of the legacy
# eased VTK path.  A 1.45 multiplier is deliberately quick while the bounded,
# continuous exponent keeps high-resolution wheels and trackpads controllable.
FAST_WHEEL_ZOOM_FACTOR = 1.45
MAX_WHEEL_STEPS_PER_EVENT = 4.0


def main_2d_wheel_legacy_zoom_allowed(*, is_3d_mode: bool) -> bool:
    """Only 3D trackball wheel input is allowed to use the legacy VTK callback.

    The main 2D view is zoom-owned by the Qt event filter. In 2D mode the
    legacy VTK wheel callback must not start smooth zoom animations or mutate
    the camera; a later wheel event already handled by the Qt filter should be
    consumed and ignored here.
    """
    return bool(is_3d_mode)


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
WHEEL_STEP_UNITS = 120.0

# DEV-safe: a single Qt event may carry at most this many notches. Rapid
# physical scrolling still produces many events, so this only truncates
# malformed / aggregated deltas.
try:
    MAX_STEPS_PER_EVENT = max(
        1, int(float(os.environ.get("NAKSHA_MAX_WHEEL_STEPS_PER_EVENT", "3")))
    )
except (TypeError, ValueError):
    MAX_STEPS_PER_EVENT = 3

# Zooming must never move the camera closer than this fraction of the
# dataset span (that is the "one pixel of the cloud" limit) nor further
# away than MAX_ZOOM_OUT_SPAN x the span.
MIN_ZOOM_IN_SPAN = 1.0e-4
MAX_ZOOM_OUT_SPAN = 2000.0

# Cursor-anchored zoom (keep the world point under the pointer fixed) is
# OPT-IN. The anchored path converts screen->world and back around the
# zoom; measured on the 1000x1000 high-precision dataset it drifted the
# view centre by 936 m over a single zoom-in/zoom-out pair (and flipped the
# Y sign of the correction), which is worse than not anchoring at all.
# Centre zoom is exact, reversible, and is what vtkCamera::Zoom does by
# default. Set NAKSHA_ZOOM_TO_CURSOR=1 to re-enable the anchored path.
CURSOR_ZOOM = os.environ.get(
    "NAKSHA_ZOOM_TO_CURSOR", "0").strip().lower() in ("1", "true", "yes", "on")

# PART 2: exactly ONE component may change ParallelScale per wheel input.
# In main-viewport mode the Vulkan surface sits ON TOP of the VTK canvas, and
# Qt can still deliver the same physical notch to both the canvas event filter
# and the surface - which applied 1.10 AND 1.331 to one notch (measured:
# 477.267955 -> 358.578478 -> 325.980435). A wheel notch is never delivered
# faster than a few ms, so rejecting a second transaction inside this window
# makes the owner idempotent per input without affecting real scrolling
# (a fast wheel generates events tens of ms apart).
ZOOM_DEBOUNCE_S = float(os.environ.get("NAKSHA_ZOOM_DEBOUNCE_S", "0.008"))
_last_zoom_at = [0.0]


def wheel_steps(raw_delta: float) -> float:
    """Raw Qt wheel delta -> notches, clamped to MAX_STEPS_PER_EVENT."""
    try:
        d = float(raw_delta)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(d):
        return 0.0
    steps = d / WHEEL_STEP_UNITS
    if steps > MAX_STEPS_PER_EVENT:
        steps = float(MAX_STEPS_PER_EVENT)
    elif steps < -MAX_STEPS_PER_EVENT:
        steps = float(-MAX_STEPS_PER_EVENT)
    return steps


class WheelAccumulator:
    """PART 7: fractional wheel deltas accumulate into whole notches.

    A high-resolution wheel reports 15-30 units at a time; only a full
    120-unit notch produces a step, and the remainder is carried so a long
    slow scroll zooms at exactly the same rate as discrete notches.
    """

    __slots__ = ("_acc",)

    def __init__(self) -> None:
        self._acc = 0.0

    def reset(self) -> None:
        self._acc = 0.0

    def add(self, raw_delta: float) -> float:
        """Feed a raw delta; return the signed number of notches to apply."""
        try:
            d = float(raw_delta)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(d):
            return 0.0
        # A large jump is treated as its own event, not as an accumulator
        # debt that would fire many notches later.
        if abs(d) > WHEEL_STEP_UNITS * MAX_STEPS_PER_EVENT:
            self._acc = 0.0
        self._acc += d
        steps = 0.0
        while abs(self._acc) >= WHEEL_STEP_UNITS:
            one = math.copysign(1.0, self._acc)
            steps += one
            self._acc -= WHEEL_STEP_UNITS * one
        return steps

    @property
    def pending(self) -> float:
        return self._acc


def zoom_factor_for(steps: float) -> float:
    """The VTK Zoom factor for `steps` notches (1.10 ** steps)."""
    return float(DEFAULT_WHEEL_ZOOM_FACTOR) ** float(steps)


def dataset_span(app) -> Optional[float]:
    """Largest XY span of the loaded dataset, or None when unknown."""
    for getter in ("_main_zoom_span",):
        fn = getattr(app, getter, None)
        if callable(fn):
            try:
                v = fn()
                if v and math.isfinite(float(v)) and float(v) > 0.0:
                    return float(v)
            except Exception:
                pass
    try:
        mgr = getattr(app, "naksha_stream", None)
        if mgr is not None:
            b = mgr.dataset_bounds()
            if b is not None:
                lo, hi = b
                span = max(abs(float(hi[0]) - float(lo[0])),
                           abs(float(hi[1]) - float(lo[1])))
                if span > 0.0:
                    return span
    except Exception:
        pass
    return None


def scale_limits(app) -> Tuple[Optional[float], Optional[float]]:
    """PART 17: (min_scale, max_scale) derived from the dataset span."""
    span = dataset_span(app)
    if not span or span <= 0.0:
        return None, None
    return (span * MIN_ZOOM_IN_SPAN, span * MAX_ZOOM_OUT_SPAN)


def _main_camera_owner(app, vtk_widget):
    """The render-backend owner when it owns the MAIN camera for this widget."""
    if vtk_widget is not getattr(app, "vtk_widget", None):
        return None                    # section / auxiliary widgets stay on VTK
    owner = getattr(app, "render_backend", None)
    fn = getattr(owner, "owns_main_camera", None)
    try:
        return owner if (fn is not None and fn()) else None
    except Exception:
        return None


def apply_zoom_steps(app, steps: float, *, vtk_widget=None,
                     display_position=None, render: bool = True,
                     label: str = "wheel", debounce: bool = True) -> bool:
    """Apply ONE absolute zoom for `steps` notches.  The only scale mutator.

    Returns True when the camera was changed.  Everything is done inside a
    single `_camera_mutation_in_progress` transaction (PART 11) so the VTK
    ModifiedEvent observer cannot start a second, competing change, and the
    caller renders exactly once afterwards (PART 12).
    """
    if vtk_widget is None:
        vtk_widget = getattr(app, "vtk_widget", None)
    if vtk_widget is None:
        return False
    renderer = getattr(vtk_widget, "renderer", None)
    if renderer is None:
        return False
    try:
        camera = renderer.GetActiveCamera()
    except Exception:
        return False
    if camera is None:
        return False
    if getattr(app, "is_3d_mode", False):
        return False          # native 3D wheel zoom is left untouched
    if getattr(app, "_shutdown_in_progress", False):
        return False
    if not camera.GetParallelProjection():
        return False

    steps = float(steps)
    if not math.isfinite(steps) or abs(steps) < 1e-9:
        return False
    # PART 16: never trust a single event to carry an unbounded burst.
    steps = max(-float(MAX_STEPS_PER_EVENT),
                min(float(MAX_STEPS_PER_EVENT), steps))
    if abs(steps) < 1e-9:
        return False

    factor = zoom_factor_for(steps)
    if not (math.isfinite(factor) and factor > 0.0):
        return False

    # PART 2: idempotent per physical input. The Vulkan surface is the single
    # physical receiver for the visible main viewport and passes debounce=False;
    # the Qt canvas filter (the OTHER receiver) keeps the window.
    now = time.monotonic()
    if debounce and ZOOM_DEBOUNCE_S > 0.0 \
            and (now - _last_zoom_at[0]) < ZOOM_DEBOUNCE_S:
        return False
    if debounce:
        _last_zoom_at[0] = now
    # Open ONE trace id for this physical input and carry it downstream.
    try:
        from gui.render_backend import cam_trace_input
        cam_trace_input("WHEEL")
    except Exception:
        pass

    old_scale = float(camera.GetParallelScale())
    if not (math.isfinite(old_scale) and old_scale > 0.0):
        return False

    # STAGE C: when the Vulkan MAIN viewport owns the camera, the zoom is a
    # MainCamera2D mutation (VTK only receives a passive mirror). Same factor,
    # same clamp window, one mutation, one push.
    owner = _main_camera_owner(app, vtk_widget)
    if owner is not None:
        lo_s, hi_s = scale_limits(app)
        return bool(owner.apply_main_zoom(
            factor, display_position=display_position if CURSOR_ZOOM else None,
            limits=(lo_s, hi_s), label=label))

    # PART 11: one transaction around every camera write.
    in_progress = getattr(app, "_camera_mutation_in_progress", False)
    app._camera_mutation_in_progress = True
    try:
        ok = False
        try:
            if CURSOR_ZOOM:
                ok = bool(app._zoom_widget_at_cursor(
                    vtk_widget, factor,
                    interactor=getattr(vtk_widget, "interactor", None),
                    display_position=display_position,
                    render=False,
                ))
            else:
                # Centre zoom: exactly one vtkCamera::Zoom, no anchor
                # correction, so the view centre is provably untouched.
                camera.Zoom(factor)
                ok = True
        except Exception:
            ok = False
        if not ok:
            # Cursor anchoring unavailable: still zoom exactly once, centred.
            try:
                camera.Zoom(factor)
                ok = True
            except Exception:
                ok = False
        if not ok:
            return False

        new_scale = float(camera.GetParallelScale())
        if os.environ.get("NAKSHA_DEV_CAMERA_TRACE", "").strip() not in (
                "", "0", "false", "False"):
            print(f"[ZOOM TXN] {label} steps={steps:+.4f} "
                  f"factor={factor:.6f} scale {old_scale:.6f} -> {new_scale:.6f}",
                  flush=True)

        # PART 15: the measured ratio must equal the requested one.
        expected = old_scale / factor
        tol = max(abs(expected) * 1e-6, 1e-9)
        if not math.isfinite(new_scale) or new_scale <= 0.0 \
                or abs(new_scale - expected) > tol:
            try:
                camera.SetParallelScale(old_scale)
            except Exception:
                pass
            print(f"[RUNAWAY ZOOM REJECTED] {label}: steps={steps:.4f} "
                  f"factor={factor:.6f} expected={expected:.6f} "
                  f"got={new_scale:.6f} -> restored {old_scale:.6f}", flush=True)
            return False

        # PART 17: clamp into the dataset-derived window.
        lo_s, hi_s = scale_limits(app)
        if lo_s is not None and hi_s is not None:
            clamped = min(max(new_scale, lo_s), hi_s)
            if abs(clamped - new_scale) > 0.0:
                try:
                    camera.SetParallelScale(clamped)
                    new_scale = clamped
                    print(f"[ZOOM SCALE CLAMPED] {label}: "
                          f"{new_scale:.6g} in [{lo_s:.6g}, {hi_s:.6g}]",
                          flush=True)
                except Exception:
                    pass
        return True
    finally:
        app._camera_mutation_in_progress = in_progress


def note_pan_scale(app, scale: float) -> None:
    """PART 14: remember ParallelScale at pan start."""
    app._pan_lock_scale = float(scale)


def enforce_pan_scale(app, who: str) -> bool:
    """PART 14: pan must never change ParallelScale.

    Returns True when the scale is intact; on a violation it restores the
    pan-start value and names the caller that changed it.
    """
    want = getattr(app, "_pan_lock_scale", None)
    if want is None:
        return True
    try:
        cam = app.vtk_widget.renderer.GetActiveCamera()
        cur = float(cam.GetParallelScale())
    except Exception:
        return True
    if abs(cur - float(want)) <= max(abs(float(want)) * 1e-12, 1e-12):
        return True
    print(f"[ILLEGAL SCALE MUTATION DURING PAN] by={who}: "
          f"{float(want):.9f} -> {cur:.9f}; restored {float(want):.9f}",
          flush=True)
    try:
        cam.SetParallelScale(float(want))
    except Exception:
        pass
    return False
