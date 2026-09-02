from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from .power_phase1_state import PowerPhase1State


PHASE4_VERSION = "NAKSHA_POWER_PHASE4_FINAL_ASSET_PROTECTION_V3_10"


def _freeze(arr: np.ndarray) -> np.ndarray:
    arr.setflags(write=False)
    return arr


def _mask_from_indices(n: int, indices) -> np.ndarray:
    mask = np.zeros(int(n), dtype=bool)
    if indices is None:
        return mask
    idx = np.asarray(indices, dtype=np.int64).ravel()
    idx = idx[(idx >= 0) & (idx < int(n))]
    if idx.size:
        mask[np.unique(idx)] = True
    return mask


@dataclass(frozen=True)
class PowerPhase4Protection:
    """Immutable final power-asset protection contract.

    Phase 4 starts only after Phase 3 conductor fitting and the structural
    Pole/Pylon detector have both finished. It resolves any Wire/Pole overlap
    once (Pole/Pylon is the more specific structural semantic and wins), freezes
    the final exact-writable masks, and provides one idempotent re-assertion
    operation for the final class array.

    This layer does not discover new power assets and does not loosen any
    geometry threshold. Its only job is to ensure already-confirmed assets are
    not silently changed by later cleanup/finalization stages.
    """

    version: str
    point_count: int
    wire_mask: np.ndarray
    pole_mask: np.ndarray
    protected_mask: np.ndarray
    input_wire_points: int
    input_pole_points: int
    overlap_points: int
    final_wire_points: int
    final_pole_points: int
    exact_fence_only: bool

    def report(self) -> dict:
        return {
            "version": self.version,
            "point_count": int(self.point_count),
            "input_wire_points": int(self.input_wire_points),
            "input_pole_points": int(self.input_pole_points),
            "wire_pole_overlap_points": int(self.overlap_points),
            "final_wire_points": int(self.final_wire_points),
            "final_pole_points": int(self.final_pole_points),
            "protected_power_points": int(np.count_nonzero(self.protected_mask)),
            "pole_precedence": True,
            "exact_fence_only": bool(self.exact_fence_only),
            "immutable": bool(
                (not self.wire_mask.flags.writeable)
                and (not self.pole_mask.flags.writeable)
                and (not self.protected_mask.flags.writeable)
            ),
        }


def build_power_phase4_protection(
    phase1: PowerPhase1State,
    *,
    wire_indices=None,
    pole_indices=None,
    wire_mask: Optional[np.ndarray] = None,
    pole_mask: Optional[np.ndarray] = None,
) -> PowerPhase4Protection:
    n = int(phase1.point_count)

    if wire_mask is None:
        wire = _mask_from_indices(n, wire_indices)
    else:
        wire = np.asarray(wire_mask, dtype=bool).reshape(-1).copy()
        if len(wire) != n:
            raise ValueError("Phase4 wire_mask length mismatch")

    if pole_mask is None:
        pole = _mask_from_indices(n, pole_indices)
    else:
        pole = np.asarray(pole_mask, dtype=bool).reshape(-1).copy()
        if len(pole) != n:
            raise ValueError("Phase4 pole_mask length mismatch")

    active = np.asarray(phase1.active_mask, dtype=bool)
    sem_protected = np.asarray(phase1.protected_mask, dtype=bool)

    # A confirmed power write is legal only inside the exact Phase-1 writable
    # scope and never on a Phase-1 semantically protected point.
    wire &= active & (~sem_protected)
    pole &= active & (~sem_protected)

    input_wire = int(np.count_nonzero(wire))
    input_pole = int(np.count_nonzero(pole))
    overlap = wire & pole
    overlap_count = int(np.count_nonzero(overlap))

    # A conductor physically meets the support. At that intersection the
    # support/tower semantic is more specific than Wire, so Pole/Pylon wins.
    if overlap_count:
        wire[overlap] = False

    protected = wire | pole
    exact = not bool(np.any(protected & (~active)))

    return PowerPhase4Protection(
        version=PHASE4_VERSION,
        point_count=n,
        wire_mask=_freeze(wire),
        pole_mask=_freeze(pole),
        protected_mask=_freeze(protected),
        input_wire_points=input_wire,
        input_pole_points=input_pole,
        overlap_points=overlap_count,
        final_wire_points=int(np.count_nonzero(wire)),
        final_pole_points=int(np.count_nonzero(pole)),
        exact_fence_only=exact,
    )


def apply_power_phase4_protection(
    classes,
    phase4: PowerPhase4Protection,
    *,
    wire_output_code: int,
    pole_output_code: int,
):
    """Idempotently re-assert the frozen final Wire/Pole semantics.

    Returns a private uint8 copy and the number of rows repaired. The function
    never writes outside the frozen Phase-4 masks.
    """
    cls = np.asarray(classes, dtype=np.uint8).reshape(-1).copy()
    if len(cls) != int(phase4.point_count):
        raise ValueError("Phase4 classification length mismatch")

    wire_code = np.uint8(int(wire_output_code))
    pole_code = np.uint8(int(pole_output_code))

    before_wire = cls[np.asarray(phase4.wire_mask, dtype=bool)].copy()
    before_pole = cls[np.asarray(phase4.pole_mask, dtype=bool)].copy()

    cls[np.asarray(phase4.wire_mask, dtype=bool)] = wire_code
    cls[np.asarray(phase4.pole_mask, dtype=bool)] = pole_code

    repaired = int(np.count_nonzero(before_wire != wire_code)) + int(
        np.count_nonzero(before_pole != pole_code)
    )
    return cls, repaired
