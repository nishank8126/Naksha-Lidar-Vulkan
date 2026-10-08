"""PART 2/3/4 - dataset attribute capability detection.

XYZ IS THE ONLY MANDATORY POINT-CLOUD ATTRIBUTE. Everything else is optional and
must NEVER be fabricated, and must NEVER hold a block in a permanent "pending
replacement" state on a dataset that does not contain it.

THE DEFECT THIS FIXES
---------------------
`123.LAS` advertises `attribute_mask = 0x127`: XYZ + CLASSIFICATION +
INTENSITY, and NO RGB (bit 3 clear). A global "rgb" display preference was
applied to it anyway, producing:

    required_attrs = (1, 8, 2, 4)      # 8 == ATTR_RGB, which does not exist

Every replacement block then reported `blocking_attribute: rgb` FOREVER, so its
coarse parent could never legally retire, while other blocks did retire. That is
precisely the reported symptom: disconnected point islands with large black
gaps, showing 1,153,449 of 2,958,460 points (39%) at full FIT.

The capability set is read from the NKIDX header `attribute_mask`, so it is
ground truth for the file rather than a guess about a preference.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Tuple

from .format import (ATTR_CLASSIFICATION, ATTR_INTENSITY, ATTR_RGB, ATTR_XYZ)

# ATTR_NORMAL lives in normal_streaming, which imports .format; declaring it
# here as well keeps this module dependency-free and avoids an import cycle.
ATTR_NORMAL = "normal"


# Display modes that need only geometry, in the priority the user specified.
# PART 1 overrides the earlier PART 3 ranking: a freshly opened file is ALWAYS
# neutral gray, whatever it contains. A capability-driven choice between
# classification / rgb / intensity put the user straight into coloured modes on
# open, which is exactly the rainbow-on-load behaviour that was rejected.
MODE_NEUTRAL = "neutral"
MODE_CLASSIFICATION = "classification"
MODE_RGB = "rgb"
MODE_INTENSITY = "intensity"
MODE_ELEVATION = "elevation"
MODE_SHADED = "shaded"

DEFAULT_DISPLAY_MODE = MODE_NEUTRAL

# What the app asked for before capabilities were known, and what is actually
# used. Kept separate so the substitution is always REPORTED, never silent.
PREFERRED_DISPLAY_MODE = "rgb"


@dataclass(frozen=True)
class DatasetCapabilities:
    """What a dataset ACTUALLY contains. Never inferred from a preference."""

    point_count: int = 0
    attribute_mask: int = 0
    has_xyz: bool = True
    has_classification: bool = False
    has_intensity: bool = False
    has_rgb: bool = False
    has_normals: bool = False
    # Phase 8: streaming block-wise Surface triangulation is available when
    # the .nakshaidx hierarchy exists (which it does for any NAKSHA cache).
    # The legacy global Delaunay path is gated on app.data being non-empty.
    streaming_surface: bool = True

    @classmethod
    def from_index(cls, idx, has_normals: bool = False) -> "DatasetCapabilities":
        mask = 0
        count = 0
        try:
            mask = int(idx.header["attribute_mask"])
        except Exception:
            mask = 0
        try:
            count = int(idx.total_points)
        except Exception:
            count = 0
        # XYZ is mandatory for a point cloud; a mask of 0 means "unrecorded", and
        # assuming XYZ is present is the only safe reading for rendering.
        return cls(
            point_count=count,
            attribute_mask=mask,
            has_xyz=bool(mask & ATTR_XYZ) or mask == 0,
            has_classification=bool(mask & ATTR_CLASSIFICATION),
            has_intensity=bool(mask & ATTR_INTENSITY),
            has_rgb=bool(mask & ATTR_RGB),
            has_normals=bool(has_normals),
        )

    # ------------------------------------------------------------------ #
    def supports(self, attr) -> bool:
        if attr == ATTR_NORMAL:
            return bool(self.has_normals)
        if attr == ATTR_XYZ:
            return bool(self.has_xyz)
        if attr == ATTR_CLASSIFICATION:
            return bool(self.has_classification)
        if attr == ATTR_INTENSITY:
            return bool(self.has_intensity)
        if attr == ATTR_RGB:
            return bool(self.has_rgb)
        return True      # unknown/auxiliary attribute: leave it alone

    def intersect(self, attrs: Iterable) -> Tuple[int, ...]:
        """Drop every attribute the dataset does not actually contain.

        This is the single guarantee that an absent OPTIONAL attribute can never
        hold a block pending forever: if the file has no RGB, RGB is simply not
        requested, so there is nothing to wait for.
        """
        return tuple(a for a in (attrs or ()) if self.supports(a))

    def dropped(self, attrs: Iterable) -> Tuple[int, ...]:
        """The attributes `intersect` would remove - reported, not hidden."""
        return tuple(a for a in (attrs or ()) if not self.supports(a))

    # ------------------------------------------------------------------ #
    def default_display_mode(self) -> str:
        """PART 1 - GRAY FIRST, always.

        This deliberately does NOT pick between classification / rgb /
        intensity. Opening any LAS/LAZ shows a uniform neutral gray cloud, and
        the user chooses a colour mode explicitly. Neutral requires XYZ only,
        so an XYZ-only file opens correctly too.
        """
        return MODE_NEUTRAL

    def preferred_display_mode(self) -> str:
        """What the app would have asked for, kept for REPORTING only."""
        return PREFERRED_DISPLAY_MODE

    def mode_available(self, mode: str) -> bool:
        """Can this mode actually draw on this dataset?"""
        m = str(mode or "").strip().lower()
        if m == MODE_RGB:
            return bool(self.has_rgb)
        if m == MODE_CLASSIFICATION:
            return bool(self.has_classification)
        if m == MODE_INTENSITY:
            return bool(self.has_intensity)
        # elevation / shaded always have geometry to work from; shaded's real
        # gate is the normal store, which is reported separately.
        return True

    def effective_mode(self, requested: str) -> str:
        """PART 3: clicking an unavailable mode must NOT fail.

        The user keeps a valid mode instead. Pure function - this class is
        frozen, and a silent substitution would be worse than a failed one, so
        callers compare the result against what they asked for.
        """
        if self.mode_available(requested):
            return str(requested or DEFAULT_DISPLAY_MODE)
        return self.default_display_mode()

    def describe(self) -> str:
        def yn(v):
            return "YES" if v else "NO"
        return (f"points={self.point_count:,} "
                f"xyz={yn(self.has_xyz)} class={yn(self.has_classification)} "
                f"intensity={yn(self.has_intensity)} "
                f"rgb={yn(self.has_rgb)} normals={yn(self.has_normals)}")
