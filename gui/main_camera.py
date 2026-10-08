"""MainCamera2D - the canonical state of the MAIN (plan-view) camera.

One object holds the truth about where the main viewport is looking:

    centre (x, y, z) + parallel_scale + viewport pixels
    -> world_width / world_height (derived)
    -> signature (quantised) -> generation

The generation advances ONLY when the signature changes, so raw events
(render_start, ModifiedEvent, mouse-move, paint) can never mint one. An invalid
sample (non-finite, zero scale, non-positive viewport) is rejected: it mutates
nothing and does not advance the generation.

MIGRATION NOTE: see gui/render_backend.py. Until the wheel / pan / fit writers
are moved here (stages C-E) this object is seeded from the working VTK camera;
afterwards navigation writes HERE and VTK is only a mirror.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Tuple

from gui.naksha_cache.visible_parity import camera_signature as _signature

EPSILON = 1e-9
_REJECT_LOG_LIMIT = 8


@dataclass
class MainCamera2D:
    center_x: float = 0.0
    center_y: float = 0.0
    center_z: float = 0.0
    parallel_scale: float = 1.0
    viewport_width: int = 0
    viewport_height: int = 0
    generation: int = 0
    signature: Optional[Tuple] = None
    rejected_samples: int = 0
    last_source: str = ""
    valid: bool = field(default=False)

    # ---- derived ---------------------------------------------------------
    @property
    def world_height(self) -> float:
        return 2.0 * float(self.parallel_scale)

    @property
    def world_width(self) -> float:
        if self.viewport_height <= 0:
            return 0.0
        return self.world_height * float(self.viewport_width) / float(self.viewport_height)

    @property
    def center(self) -> Tuple[float, float]:
        return (float(self.center_x), float(self.center_y))

    def visible_bounds(self) -> Tuple[float, float, float, float]:
        """(x0, y0, x1, y1) of the plan view."""
        hw, hh = self.world_width * 0.5, self.world_height * 0.5
        return (self.center_x - hw, self.center_y - hh,
                self.center_x + hw, self.center_y + hh)

    def units_per_pixel(self) -> float:
        return self.world_height / float(self.viewport_height) if self.viewport_height > 0 else 0.0

    # ---- validation + mutation ------------------------------------------
    @staticmethod
    def _reason(cx, cy, scale, vw, vh) -> Optional[str]:
        for name, v in (("center_x", cx), ("center_y", cy), ("parallel_scale", scale)):
            if v is None or not math.isfinite(float(v)):
                return f"non_finite_{name}"
        if float(scale) <= EPSILON:
            return "parallel_scale<=epsilon"
        if int(vw) <= 0 or int(vh) <= 0:
            return "non_positive_viewport"
        return None

    def set_state(self, cx, cy, scale, vw, vh, cz=None, source: str = "unknown") -> bool:
        """Adopt a camera state. Returns True when it CHANGED the camera.

        Rejected samples change nothing (not the state, not the generation).
        """
        why = self._reason(cx, cy, scale, vw, vh)
        if why is not None:
            self.rejected_samples += 1
            if self.rejected_samples <= _REJECT_LOG_LIMIT:
                print("[CAMERA SAMPLE REJECTED]")
                print(f"source: {source}")
                print(f"reason: {why}")
                print(f"signature: {(cx, cy, scale, vw, vh)}")
            return False
        world_h = 2.0 * float(scale)
        world_w = world_h * float(vw) / float(vh)
        sig = _signature((float(cx), float(cy)), (world_w, world_h), int(vw), int(vh))
        self.last_source = source
        if cz is not None and math.isfinite(float(cz)):
            self.center_z = float(cz)
        if sig == self.signature:
            return False
        self.center_x, self.center_y = float(cx), float(cy)
        self.parallel_scale = float(scale)
        self.viewport_width, self.viewport_height = int(vw), int(vh)
        self.signature = sig
        self.generation += 1
        self.valid = True
        return True

    def pan_pixels(self, dx_px: float, dy_px: float, source: str = "pan",
                   viewport_height: Optional[float] = None) -> bool:
        """Drag by (dx, dy) screen pixels: the scene follows the cursor, the
        SCALE is untouched. Screen +y is down, world +y is up."""
        # `viewport_height` lets the caller measure the drag in the pixels of the
        # widget it happened in (the surface can differ from the VTK canvas by 1px).
        upp = (self.world_height / float(viewport_height)\
               if viewport_height else self.units_per_pixel())
        if not self.valid or upp <= 0.0:
            return False
        return self.set_state(self.center_x - dx_px * upp, self.center_y + dy_px * upp,
                              self.parallel_scale, self.viewport_width,
                              self.viewport_height, source=source)

    def display_to_world(self, x: float, y: float) -> Tuple[float, float]:
        """World XY under a VTK-style display position (origin bottom-left)."""
        upp = self.units_per_pixel()
        return (self.center_x + (float(x) - self.viewport_width * 0.5) * upp,
                self.center_y + (float(y) - self.viewport_height * 0.5) * upp)

    def zoom_to_scale(self, new_scale: float, anchor_world=None, source: str = "zoom") -> bool:
        """Set the scale. With `anchor_world` (x, y) the world point under the
        cursor stays under the cursor (centre is shifted accordingly)."""
        if not self.valid:
            return False
        cx, cy = self.center_x, self.center_y
        if anchor_world is not None and self.parallel_scale > 0.0:
            k = float(new_scale) / self.parallel_scale
            cx = anchor_world[0] + (cx - anchor_world[0]) * k
            cy = anchor_world[1] + (cy - anchor_world[1]) * k
        return self.set_state(cx, cy, new_scale, self.viewport_width,
                              self.viewport_height, source=source)

    def describe(self) -> str:
        return (f"MainCamera2D(centre=({self.center_x:.3f},{self.center_y:.3f}) "
                f"scale={self.parallel_scale:.6f} world={self.world_width:.3f}x"
                f"{self.world_height:.3f} vp={self.viewport_width}x{self.viewport_height} "
                f"gen={self.generation} src={self.last_source})")
