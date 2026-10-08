"""CANONICAL DISPLAY MODES - the legacy VTK behaviour, encoded once.

THE OLD VTK RENDERER IS THE VISUAL SOURCE OF TRUTH. Every constant below was
traced from the legacy implementation, not invented:

  neutral gray      0.5 float / 128 uint8
                    gui/unified_actor_manager.py:192  `color_lut[:] = 0.5`
                    gui/pointcloud_display.py:272    `norm = 0.5`
  default class col (128,128,128); hidden class (0,0,0)
                    unified_actor_manager.py:222,506,510,511
  base point size   2.5
                    unified_actor_manager.py:15      `_BASE_POINT_SIZE = 2.5`
  intensity         grayscale, gray = pow(t, gamma)*255
                    pointcloud_display._nakshatech_intensity_rgb
                    intensity_clip_low default 0.5
  elevation         5-stop rainbow Blue->Cyan->Green->Yellow->Red,
                    percentile clip low_pct=1.0 high_pct=99.0
                    pointcloud_display._nakshatech_elevation_rgb
  depth             grayscale, depth_clip_low=1.0 depth_clip_high=99.0
                    depth_gamma=1.0 depth_color_scheme='grayscale'
                    gui/depth_settings_dialog.py:35-38

WHY NEUTRAL IS THE DEFAULT (PART 1/22)
--------------------------------------
Opening a LAS/LAZ must show a complete GRAY cloud. Classification colours,
elevation rainbows, intensity and RGB appear only when the user explicitly
asks. `neutral` requires XYZ only, so a file with nothing but XYZ still opens
correctly - which is the whole point of PART 2.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import numpy as np

from .format import ATTR_CLASSIFICATION, ATTR_INTENSITY, ATTR_RGB, ATTR_XYZ

ATTR_NORMAL = "normal"

# --------------------------------------------------------------------------- #
# Legacy constants (traced, not invented).                                     #
# --------------------------------------------------------------------------- #
NEUTRAL_GRAY = 0.5                     # float, unified_actor_manager.py:192
NEUTRAL_GRAY_U8 = 128                  # 0.5 * 255, the byte form VTK used
CLASS_DEFAULT_RGB = (128, 128, 128)    # no PTC entry for this code
CLASS_HIDDEN_RGB = (0, 0, 0)           # class visibility OFF
BASE_POINT_SIZE = 2.5                  # _BASE_POINT_SIZE
CLASS_WEIGHT_MIN = 0.1
CLASS_WEIGHT_MAX = 12.0

INTENSITY_CLIP_LOW = 0.5
ELEVATION_CLIP_LOW_PCT = 1.0
ELEVATION_CLIP_HIGH_PCT = 99.0
DEPTH_CLIP_LOW_PCT = 1.0
DEPTH_CLIP_HIGH_PCT = 99.0
DEPTH_GAMMA = 1.0
DEPTH_COLOR_SCHEME = "grayscale"

MODE_NEUTRAL = "neutral"
MODE_DEPTH = "depth"
MODE_RGB = "rgb"
MODE_INTENSITY = "intensity"
MODE_ELEVATION = "elevation"
MODE_CLASS = "class"
MODE_SHADING = "shaded"
MODE_SURFACE = "surface"


@dataclass(frozen=True)
class DisplayModeSpec:
    """One canonical mode: what it needs, and whether it can be used here."""
    name: str
    requires: tuple
    gate: Optional[str] = None          # capability key; "xyz" is always ok
    label: str = ""
    uses_class_lut: bool = False        # PTC change == LUT-only update

    def available(self, caps) -> bool:
        if self.gate is None or self.gate == "xyz":
            return True
        return bool(getattr(caps, self.gate, False))

    def requirement_reason(self, caps) -> str:
        """Text for a disabled toolbar button (PART 27) - explain, never fail."""
        if self.available(caps):
            return ""
        return (f"{self.label or self.name} needs {self.gate.upper()} which "
                f"this dataset does not contain")


MODES: Dict[str, DisplayModeSpec] = {
    MODE_NEUTRAL: DisplayModeSpec(MODE_NEUTRAL, (ATTR_XYZ,), "xyz", "Neutral"),
    MODE_DEPTH: DisplayModeSpec(MODE_DEPTH, (ATTR_XYZ,), "xyz", "Depth"),
    MODE_ELEVATION: DisplayModeSpec(MODE_ELEVATION, (ATTR_XYZ,), "xyz",
                                    "Elevation"),
    MODE_CLASS: DisplayModeSpec(MODE_CLASS, (ATTR_XYZ, ATTR_CLASSIFICATION),
                                "has_classification", "Class",
                                uses_class_lut=True),
    MODE_SHADING: DisplayModeSpec(MODE_SHADING,
                                  (ATTR_XYZ, ATTR_CLASSIFICATION, ATTR_NORMAL),
                                  "has_classification", "Shading",
                                  uses_class_lut=True),
    MODE_RGB: DisplayModeSpec(MODE_RGB, (ATTR_XYZ, ATTR_RGB), "has_rgb", "RGB"),
    MODE_INTENSITY: DisplayModeSpec(MODE_INTENSITY, (ATTR_XYZ, ATTR_INTENSITY),
                                    "has_intensity", "Intensity"),
    MODE_SURFACE: DisplayModeSpec(MODE_SURFACE, (ATTR_XYZ,), "xyz", "Surface"),
}

# PART 1/2: the mode a freshly opened file is ALWAYS in.
DEFAULT_MODE = MODE_NEUTRAL

MODE_ALIASES = {"default": MODE_NEUTRAL, "": MODE_NEUTRAL, "gray": MODE_NEUTRAL,
                "grey": MODE_NEUTRAL,
                # "classification" is the stream manager's canonical name and
                # "class" is the GUI/registry name; both must land on "class".
                "classification": MODE_CLASS,
                "class": MODE_CLASS,
                "shaded_class": MODE_SHADING,
                "shaded_class_instant": MODE_SHADING}


def canonical_mode(name: str) -> str:
    m = str(name or "").strip().lower()
    m = MODE_ALIASES.get(m, m)
    return m if m in MODES else DEFAULT_MODE


def mode_spec(name: str) -> DisplayModeSpec:
    return MODES[canonical_mode(name)]


def default_mode() -> str:
    """PART 1 - GRAY FIRST, always. Capability-driven, never a preference."""
    return DEFAULT_MODE


def available_modes(caps) -> Dict[str, bool]:
    return {name: spec.available(caps) for name, spec in MODES.items()}


def mode_requires(name: str, caps=None) -> tuple:
    """Required attributes, intersected with real capabilities.

    Neutral is XYZ only by construction, so it can never be blocked no matter
    what the file contains or lacks.
    """
    nominal = mode_spec(name).requires
    if caps is None:
        return nominal
    return tuple(a for a in nominal if caps.supports(a))


def resolve_mode(requested: str, caps) -> Tuple[str, Optional[Tuple[str, str]]]:
    """(effective_mode, substitution) - never fails, never fabricates."""
    want = canonical_mode(requested)
    if MODES.get(want, MODES[DEFAULT_MODE]).available(caps):
        return want, None
    return DEFAULT_MODE, (want, DEFAULT_MODE)


# --------------------------------------------------------------------------- #
# Legacy colour tables.                                                         #
# --------------------------------------------------------------------------- #
def neutral_lut(n: int = 256) -> np.ndarray:
    """PART 1/22 - ONE flat gray table.

    Every entry is identical, which is what makes the default view provably
    uniform: there is no per-point, per-tile, per-LOD or hashed variation
    because neutral mode reads no per-point attribute at all.
    """
    row = np.full((1, 3), NEUTRAL_GRAY_U8, dtype=np.uint8)
    return np.ascontiguousarray(np.repeat(row, int(n), axis=0))


def neutral_color() -> Tuple[int, int, int]:
    return (NEUTRAL_GRAY_U8, NEUTRAL_GRAY_U8, NEUTRAL_GRAY_U8)


def is_uniform_lut(lut) -> bool:
    """PART 22 helper: True when every entry is one colour.

    Proves the default view cannot vary by source id, tile, LOD, Morton order,
    block id, hash or uninitialised attribute memory.
    """
    arr = np.asarray(lut)
    if arr.ndim != 2 or arr.shape[0] < 2:
        return False
    return bool(np.all(arr == arr[0]))


def naksha_rainbow_5color(t) -> np.ndarray:
    """Legacy 5-stop elevation ramp, byte-for-byte (PART 12).

    Blue -> Cyan -> Green -> Yellow -> Red. This IS a rainbow, deliberately: it
    is the accepted VTK elevation appearance, used ONLY by elevation mode and
    never by the default view.
    """
    t = np.clip(np.asarray(t, dtype=np.float64), 0.0, 1.0)
    r = np.where(t <= 0.25, 0.0, np.where(t <= 0.50, 0.0,
                                          np.where(t <= 0.75,
                                                   (t - 0.50) / 0.25 * 255.0,
                                                   255.0)))
    g = np.where(t <= 0.25, np.clip(t / 0.25, 0.0, 1.0) * 255.0,
                 np.where(t <= 0.50, 255.0,
                          np.where(t <= 0.75, 255.0,
                                   np.clip(1.0 - (t - 0.75) / 0.25, 0.0,
                                           1.0) * 255.0)))
    b = np.where(t <= 0.25, 255.0,
                 np.where(t <= 0.50,
                          np.clip(1.0 - (t - 0.25) / 0.25, 0.0, 1.0) * 255.0,
                          0.0))
    return np.clip(np.stack([r, g, b], axis=1), 0, 255)


def intensity_lut(gamma: float = 1.0, n: int = 256) -> np.ndarray:
    """Legacy grayscale intensity table: gray = pow(t, gamma)*255 (PART 11).

    Grayscale, NOT a rainbow - that is what the old VTK path did.
    """
    t = np.linspace(0.0, 1.0, int(n))
    g = np.clip(np.power(t, float(gamma)) * 255.0, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(np.stack([g, g, g], axis=1))


def depth_lut(gamma: float = DEPTH_GAMMA, n: int = 256) -> np.ndarray:
    """Legacy depth table: grayscale, gamma, percentile clip 1-99 % (PART 10).

    Depth is CAMERA/VIEW dependent in the legacy path (it shades by distance
    from the eye), so the per-point scalar comes from the renderer while the
    colour table is the grayscale ramp.
    """
    return intensity_lut(gamma=gamma, n=n)


def elevation_lut(ramp=None, n: int = 256) -> np.ndarray:
    """Legacy elevation ramp table, custom ramp when the app supplies one."""
    t = np.linspace(0.0, 1.0, int(n))
    if ramp is not None and len(ramp) >= 2:
        stops = np.asarray([float(r[0]) for r in ramp], dtype=np.float64)
        cols = np.asarray([float(v) for r in ramp for v in r[1:4]],
                          dtype=np.float64).reshape(-1, 3)
        out = np.stack([np.interp(t, stops, cols[:, k]) for k in range(3)],
                       axis=1)
    else:
        out = naksha_rainbow_5color(t)
    return np.clip(out, 0, 255).astype(np.uint8)


def class_lut(palette: Optional[Dict] = None, weight: float = 1.0,
              n: int = 256) -> np.ndarray:
    """Legacy class LUT: (128,128,128) default, (0,0,0) when hidden.

    PART 7/8: maps EXISTING class ids to colours. It never changes XYZ, never
    reorders points and never regenerates a cache, so loading a PTC or toggling
    class visibility touches this 256x3 table and nothing else.
    """
    lut = np.full((int(n), 3), NEUTRAL_GRAY_U8, dtype=np.uint8)
    for code, entry in (palette or {}).items():
        try:
            idx = int(code) & 0xFF
            if isinstance(entry, dict):
                color = entry.get("color", CLASS_DEFAULT_RGB)
                show = bool(entry.get("show", True))
                w = float(entry.get("weight", 1.0))
            else:
                color, show, w = entry, True, 1.0
            if not show:
                lut[idx] = CLASS_HIDDEN_RGB
                continue
            w = max(CLASS_WEIGHT_MIN, min(w, CLASS_WEIGHT_MAX))
            rgb = np.asarray(color, dtype=np.float64).reshape(3) * float(weight) * w
            lut[idx] = np.clip(rgb, 0, 255).astype(np.uint8)
        except Exception:
            continue
    return np.ascontiguousarray(lut)


def compute_point_size(weight: float, base: float = BASE_POINT_SIZE) -> float:
    """Legacy per-class point size (PART 24). Base stays the VTK value; a class
    only scales it inside the legacy clamp. No oversized splats to hide holes."""
    w = max(CLASS_WEIGHT_MIN, min(float(weight), CLASS_WEIGHT_MAX))
    return float(base) * w


def build_luts(app=None, palette: Optional[Dict] = None,
               weight: float = 1.0) -> Dict[str, np.ndarray]:
    """Every display table for the current app/palette state.

    PART 20: identical in FULL_RESIDENT and STREAMING_LOD - the backend must not
    change the user-visible colour of a point.
    """
    pal = palette if palette is not None else getattr(app, "class_palette", None)
    w = weight if weight is not None else getattr(app, "class_weight", 1.0)
    elev = elevation_lut(getattr(app, "elevation_color_ramp", None))
    return {
        MODE_NEUTRAL: neutral_lut(),
        MODE_DEPTH: depth_lut(),
        MODE_CLASS: class_lut(pal, w),
        # PART 16: Shading colours by PTC/class LUT + lighting.
        MODE_SHADING: class_lut(pal, w),
        MODE_INTENSITY: intensity_lut(),
        MODE_ELEVATION: elev,
        # PART 17: Surface colours by ELEVATION + lighting, NOT by class.
        MODE_SURFACE: elev,
    }


