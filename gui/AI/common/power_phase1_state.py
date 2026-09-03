from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

import numpy as np


PHASE1_VERSION = "NAKSHA_POWER_PHASE1_BASELINE_V3_7"


def _unique_u8(values: Iterable[int]) -> np.ndarray:
    out = []
    for value in values or ():
        try:
            iv = int(value)
        except Exception:
            continue
        if 0 <= iv <= 255:
            out.append(iv)
    if not out:
        return np.empty(0, dtype=np.uint8)
    return np.asarray(sorted(set(out)), dtype=np.uint8)


def _active_mask(n: int, active_indices=None) -> np.ndarray:
    mask = np.ones(int(n), dtype=bool)
    if active_indices is None:
        return mask
    mask[:] = False
    idx = np.asarray(active_indices, dtype=np.int64).ravel()
    idx = idx[(idx >= 0) & (idx < int(n))]
    if idx.size:
        mask[np.unique(idx)] = True
    return mask


def _freeze(arr: np.ndarray) -> np.ndarray:
    arr.setflags(write=False)
    return arr


@dataclass(frozen=True)
class PowerPhase1State:
    """Immutable pre-Power baseline for the common power architecture.

    The snapshot is created at the *entry* of the common Power post-pass, after
    the selected Basic / Advanced / Premium base classifier has produced its
    normal semantic result and before Wire/Pole writes are allowed.

    Future power phases must use ``base_classes`` as evidence only. They may
    override a base semantic class only for geometrically confirmed power
    assets. This prevents the power engine from progressively corrupting the
    base Ground/Vegetation/Building result while still allowing a genuine
    conductor/pole to override an initial Building/Vegetation prediction.
    """

    version: str
    point_count: int
    base_classes: np.ndarray
    active_mask: np.ndarray
    protected_mask: np.ndarray
    protected_codes: tuple[int, ...]
    class_histogram: Mapping[int, int]
    active_points: int
    protected_points: int

    def report(self) -> dict:
        return {
            "version": self.version,
            "point_count": int(self.point_count),
            "active_points": int(self.active_points),
            "protected_points": int(self.protected_points),
            "protected_codes": [int(x) for x in self.protected_codes],
            "base_class_histogram": {
                str(int(k)): int(v) for k, v in self.class_histogram.items()
            },
            "immutable": bool(not self.base_classes.flags.writeable),
        }


def build_power_phase1_state(
    classes,
    *,
    active_indices=None,
    protected_codes: Iterable[int] = (),
    extra_protected_mask: Optional[np.ndarray] = None,
) -> PowerPhase1State:
    """Capture the authoritative pre-Power classification state.

    No numeric semantic assumptions are made here. The active PTC / worker has
    already resolved the numeric class codes. Phase 1 only freezes what the base
    model actually produced and records the exact writable/protected scope.
    """
    cls = np.asarray(classes, dtype=np.uint8).reshape(-1)
    n = int(cls.size)

    base = cls.copy()
    active = _active_mask(n, active_indices)

    p_codes = _unique_u8(protected_codes)
    if p_codes.size:
        protected = np.isin(base, p_codes)
    else:
        protected = np.zeros(n, dtype=bool)

    if extra_protected_mask is not None:
        extra = np.asarray(extra_protected_mask, dtype=bool).reshape(-1)
        if len(extra) != n:
            raise ValueError(
                "Phase1 extra_protected_mask length mismatch: "
                f"{len(extra)} != {n}"
            )
        protected |= extra

    # Outside the exact target is not a legal write location. Keep this as a
    # separate active mask rather than folding it into protected_mask so later
    # phases can distinguish semantic protection from fence/write protection.
    unique, counts = np.unique(base, return_counts=True)
    hist = {int(k): int(v) for k, v in zip(unique, counts)}

    return PowerPhase1State(
        version=PHASE1_VERSION,
        point_count=n,
        base_classes=_freeze(base),
        active_mask=_freeze(active),
        protected_mask=_freeze(protected),
        protected_codes=tuple(int(x) for x in p_codes.tolist()),
        class_histogram=hist,
        active_points=int(active.sum()),
        protected_points=int(protected.sum()),
    )


def phase1_override_mask(
    phase1: PowerPhase1State,
    confirmed_power_mask,
) -> np.ndarray:
    """Return the only points a later power phase is allowed to override.

    This is intentionally strict: a point must be inside the exact writable
    scope, not semantically protected, and positively confirmed as a power
    asset by a later geometry phase.
    """
    confirmed = np.asarray(confirmed_power_mask, dtype=bool).reshape(-1)
    if len(confirmed) != phase1.point_count:
        raise ValueError("Phase1 confirmed_power_mask length mismatch")
    return (
        np.asarray(phase1.active_mask, dtype=bool)
        & ~np.asarray(phase1.protected_mask, dtype=bool)
        & confirmed
    )
