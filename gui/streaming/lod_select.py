"""Screen-space LOD, camera-priority selection and the frame-time governor.

The governing rule (the most important engineering rule in this project):

    DO NOT make Naksha process 1 billion points faster.
    MAKE THE VIEWPORT ALMOST NEVER NEED TO TOUCH 1 billion points at once.

Everything here is camera -> small working set -> bounded caches. The camera
path itself is O(visible cells) and allocates nothing proportional to the
dataset.
"""
import math

from .core import (GOVERNOR_HYSTERESIS_MS, GOVERNOR_MAX_STEP, MAX_POINT_BUDGET,
                   MIN_FRAME_MS, MIN_POINT_BUDGET, TARGET_FRAME_MS)
from .scheduler import (PRIO_BACKGROUND, PRIO_MISSING_COARSE, PRIO_NEIGHBOUR,
                        PRIO_PREDICTED, PRIO_SCREEN_CENTRE, PRIO_VISIBLE_DETAIL)


class ViewVolume:
    """The world-space rectangle a camera can see, plus a motion prediction.

    Kept deliberately plain: constructing this must stay free, because it runs
    on every camera change (Phase 20 forbids a full dataset scan there).
    """

    __slots__ = ("min_x", "min_y", "max_x", "max_y", "centre_x", "centre_y",
                 "vx", "vy", "speed", "zoom", "pixels")

    def __init__(self, min_x, min_y, max_x, max_y, vx=0.0, vy=0.0,
                 zoom=0.0, pixels=(1920, 1080)):
        self.min_x, self.min_y = min(min_x, max_x), min(min_y, max_y)
        self.max_x, self.max_y = max(min_x, max_x), max(min_y, max_y)
        self.centre_x = (self.min_x + self.max_x) * 0.5
        self.centre_y = (self.min_y + self.max_y) * 0.5
        self.vx, self.vy = vx, vy
        self.speed = math.hypot(vx, vy)
        self.zoom = zoom
        self.pixels = pixels

    def area(self):
        return max(1e-9, (self.max_x - self.min_x) * (self.max_y - self.min_y))


def tile_screen_error(tile_bbox, view, cell_size):
    """How many screen pixels one point of this tile covers.

    This is the screen-space density LOD rule (Phase 3): do not draw 5,000,000
    points if they project onto 200,000 useful pixels. The value is
    pixels-per-point, so a tile that already saturates the screen should be
    decimated rather than drawn in full.
    """
    w = view.pixels[0]
    span = max(view.max_x - view.min_x, 1e-9)
    # pixels per world metre across the view
    ppm = w / span
    pts_per_m2 = max(1e-9, 1.0 / (cell_size * cell_size))
    pts_per_m2_on_screen = pts_per_m2 * (ppm * ppm)
    return 1.0 / pts_per_m2_on_screen if pts_per_m2_on_screen > 0 else 0.0


def select_visible(index, view, cell_size, max_points, resident=None,
                   halo_fraction=0.25):
    """Choose tiles to display and the priority of each.

    Returns (draw_rows, requests) where requests is a list of (row, priority)
    already sorted by the scheduler's ordering rules. This is the ONLY spatial
    work performed per camera change and it is proportional to the number of
    cells in view, not to the point count.
    """
    resident = resident or set()
    # Predictive prefetch (Phase 7): push the request rectangle ahead of the
    # camera's motion, and widen it when zoomed out so a zoom-in finds data
    # already decoded.
    fx = view.vx * halo_fraction
    fy = view.vy * halo_fraction
    hw = (view.max_x - view.min_x) * 0.5 * halo_fraction
    hh = (view.max_y - view.min_y) * 0.5 * halo_fraction
    qmin_x = min(view.min_x, view.centre_x + fx - hw)
    qmax_x = max(view.max_x, view.centre_x + fx + hw)
    qmin_y = min(view.min_y, view.centre_y + fy - hh)
    qmax_y = max(view.max_y, view.centre_y + fy + hh)

    vis_rows = index.visible_cells(view.min_x, view.min_y, view.max_x, view.max_y)
    halo_rows = index.visible_cells(qmin_x, qmin_y, qmax_x, qmax_y)
    vis_set = set(vis_rows)
    halo_set = set(halo_rows)

    # Screen-centre ranking: distance from the middle of the view, normalised.
    diag = math.hypot(max(view.max_x - view.min_x, 1e-9),
                      max(view.max_y - view.min_y, 1e-9)) * 0.5

    requests = []
    draw = []
    budget = int(max_points)
    for row in vis_rows:
        bb = index.node_bbox(row)
        cx = (bb[0] + bb[3]) * 0.5
        cy = (bb[1] + bb[4]) * 0.5
        n = int(index.nodes["point_count"][row])
        if n <= 0:
            continue
        if row in resident:
            # Already GPU resident: count it, never re-request it. This is the
            # Phase 10 acceptance - panning must not re-upload anything.
            if n <= budget:
                budget -= n
                draw.append(row)
            continue
        d = math.hypot(cx - view.centre_x, cy - view.centre_y) / max(diag, 1e-9)
        if d < 0.18:
            prio = PRIO_SCREEN_CENTRE
        elif n > budget:
            # Too many points to afford at this zoom: ask for a decimated
            # version instead of the full tile.
            prio = PRIO_VISIBLE_DETAIL
        else:
            prio = PRIO_MISSING_COARSE
        budget -= n
        requests.append((row, prio))

    # Prefetch halo: adjacent, not yet visible. Cheap to ask for, invisible
    # until the camera arrives.
    for row in halo_rows:
        if row in vis_set:
            continue
        if row in resident:
            continue
        bb = index.node_bbox(row)
        cx = (bb[0] + bb[3]) * 0.5
        cy = (bb[1] + bb[4]) * 0.5
        if view.speed > 1e-9:
            # Ahead of motion beats behind it.
            ahead = ((cx - view.centre_x) * view.vx
                     + (cy - view.centre_y) * view.vy) > 0
            requests.append((row, PRIO_PREDICTED if ahead else PRIO_NEIGHBOUR))
        else:
            requests.append((row, PRIO_NEIGHBOUR))

    requests.sort(key=lambda rp: rp[1])
    return draw, requests, vis_rows
class FrameGovernor:
    """Keeps interaction smooth by trading detail for frame time (Phase 13/14).

    The goal is NOT maximum point density. The goal is a stable frame time. The
    budget moves only when frame time is clearly outside the target band, and
    only by a bounded step, so it cannot oscillate every frame.

    Thresholds are the MEASURED ones for this box: the T400 renders a surface
    frame in 0.56 ms and a 27M-point cloud frame in 20.57 ms, so the
    16.67 ms / 33.33 ms bands are the right targets and the gap between them is
    where detail is added back or removed.
    """

    def __init__(self, target_ms=TARGET_FRAME_MS, min_ms=MIN_FRAME_MS,
                 start_budget=4_000_000):
        self.target_ms = target_ms
        self.min_ms = min_ms
        self.budget = int(start_budget)
        self.min_budget = MIN_POINT_BUDGET
        self.max_budget = MAX_POINT_BUDGET
        self.moving = True
        self.last_reason = "initial"
        self.last_lod_action = "none"
        self._ema_ms = None
        self._cooldown = 0
        self.history = []

    def update(self, gpu_ms, visible_points, moving, frame=0):
        """Feed one measured frame. Returns True if the budget moved."""
        if gpu_ms is not None and gpu_ms > 0:
            # An EMA smooths single-frame spikes so one hitch cannot collapse
            # the budget, while still responding within a few frames.
            self._ema_ms = (gpu_ms if self._ema_ms is None
                            else self._ema_ms * 0.8 + gpu_ms * 0.2)
        ms = self._ema_ms
        self.moving = moving
        if ms is None:
            return False
        if self._cooldown > 0:
            self._cooldown -= 1
            return False

        old = self.budget
        if ms > self.min_ms + GOVERNOR_HYSTERESIS_MS:
            # Over budget: cut detail, harder the further over we are.
            factor = 0.5 if ms > self.min_ms * 2 else 1.0
            self.budget = max(self.min_budget,
                              int(self.budget * (1.0 - GOVERNOR_MAX_STEP) * factor))
            self.last_reason = f"GPU {ms:.1f} ms > {self.min_ms:.1f} ms; reduce budget"
            self.last_lod_action = "coarser LOD, pause refinement uploads"
        elif ms < self.target_ms - GOVERNOR_HYSTERESIS_MS and not moving:
            # Comfortably fast AND the camera is still: add detail back. Only
            # while idle, so panning never fights for budget.
            self.budget = min(self.max_budget,
                              int(self.budget * (1.0 + GOVERNOR_MAX_STEP)))
            self.last_reason = (f"GPU {ms:.1f} ms < {self.target_ms:.1f} ms "
                                f"while idle; raise budget")
            self.last_lod_action = "finer LOD, resume refinement"
        else:
            return False

        if self.budget != old:
            self._cooldown = 3          # hysteresis, in frames
            self.history.append((frame, ms, old, self.budget))
            if len(self.history) > 512:
                del self.history[:-512]
            return True
        return False

    def clamp_for_visibility(self, requested_points):
        """Never promise more than the budget allows."""
        return min(int(requested_points), self.budget)

    def describe(self):
        return {"target_ms": self.target_ms, "min_ms": self.min_ms,
                "measured_ms": self._ema_ms, "point_budget": self.budget,
                "lod": self.last_lod_action, "reason": self.last_reason,
                "moving": self.moving}