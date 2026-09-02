from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

import numpy as np

from .power_phase1_state import PowerPhase1State


PHASE2_VERSION = "NAKSHA_POWER_PHASE2_RAW_CANDIDATES_V3_8"


def _freeze(arr: np.ndarray) -> np.ndarray:
    arr.setflags(write=False)
    return arr


def _safe01(arr) -> np.ndarray:
    a = np.asarray(arr, dtype=np.float32).reshape(-1)
    return np.clip(np.nan_to_num(a, nan=0.0, posinf=1.0, neginf=0.0), 0.0, 1.0)


def _distribution(classes: np.ndarray, mask: np.ndarray) -> Mapping[str, int]:
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return {}
    u, c = np.unique(classes[idx], return_counts=True)
    return {str(int(k)): int(v) for k, v in zip(u, c)}


@dataclass(frozen=True)
class PowerPhase2Candidates:
    """Broad, immutable, semantic-agnostic Power candidate state.

    Phase 2 deliberately does *not* classify anything. It only identifies raw
    geometric support that later phases may fit into conductor tracks or
    support structures. Base-AI class labels are recorded as diagnostics only;
    they are never used as a hard inclusion gate.
    """

    version: str
    point_count: int
    support_mask: np.ndarray
    raw_wire_mask: np.ndarray
    raw_pole_mask: np.ndarray
    raw_power_mask: np.ndarray
    wire_score: np.ndarray
    pole_score: np.ndarray
    active_wire_mask: np.ndarray
    active_pole_mask: np.ndarray
    context_wire_mask: np.ndarray
    context_pole_mask: np.ndarray
    cl_active: bool
    class_distribution_wire: Mapping[str, int]
    class_distribution_pole: Mapping[str, int]

    def report(self) -> dict:
        return {
            "version": self.version,
            "point_count": int(self.point_count),
            "semantic_gate": False,
            "classification_writes": False,
            "cl_active": bool(self.cl_active),
            "support_points": int(np.count_nonzero(self.support_mask)),
            "raw_wire_candidates": int(np.count_nonzero(self.raw_wire_mask)),
            "raw_pole_candidates": int(np.count_nonzero(self.raw_pole_mask)),
            "raw_power_union": int(np.count_nonzero(self.raw_power_mask)),
            "active_wire_candidates": int(np.count_nonzero(self.active_wire_mask)),
            "active_pole_candidates": int(np.count_nonzero(self.active_pole_mask)),
            "context_only_wire_candidates": int(np.count_nonzero(self.context_wire_mask)),
            "context_only_pole_candidates": int(np.count_nonzero(self.context_pole_mask)),
            "wire_base_class_distribution": dict(self.class_distribution_wire),
            "pole_base_class_distribution": dict(self.class_distribution_pole),
            "immutable": bool(
                not self.raw_wire_mask.flags.writeable
                and not self.raw_pole_mask.flags.writeable
                and not self.wire_score.flags.writeable
                and not self.pole_score.flags.writeable
            ),
        }


def build_power_phase2_candidates(
    xyz,
    hag,
    phase1: PowerPhase1State,
    *,
    linearity_05,
    planarity_05,
    verticality_05,
    linearity_10,
    verticality_10,
    cl_mask: Optional[np.ndarray] = None,
    cl_dist: Optional[np.ndarray] = None,
    cl_station: Optional[np.ndarray] = None,
    config=None,
) -> PowerPhase2Candidates:
    """Generate broad raw Wire and Pole/Pylon candidate masks.

    Design contract
    ---------------
    * No classification writes.
    * No hard dependency on Ground/Vegetation/Building numeric codes.
    * Context outside a fence may be retained as support for Phase 3 track
      fitting, while exact writable/active masks remain separately available.
    * Protected semantic points from Phase 1 are excluded.
    * CL is a spatial prior, not a semantic class filter.
    """
    cfg = dict(config or {})

    xyz = np.asarray(xyz, dtype=np.float64)
    hag = np.asarray(hag, dtype=np.float32).reshape(-1)
    n = int(len(hag))
    if xyz.ndim != 2 or xyz.shape[1] < 3 or len(xyz) != n:
        raise ValueError("Phase2 xyz/hag length mismatch")
    if phase1.point_count != n:
        raise ValueError("Phase2 Phase1 state length mismatch")

    l05 = _safe01(linearity_05)
    p05 = _safe01(planarity_05)
    v05 = _safe01(verticality_05)
    l10 = _safe01(linearity_10)
    v10 = _safe01(verticality_10)
    for name, arr in {
        "linearity_05": l05,
        "planarity_05": p05,
        "verticality_05": v05,
        "linearity_10": l10,
        "verticality_10": v10,
    }.items():
        if len(arr) != n:
            raise ValueError(f"Phase2 {name} length mismatch")

    active = np.asarray(phase1.active_mask, dtype=bool)
    protected = np.asarray(phase1.protected_mask, dtype=bool)
    base_classes = np.asarray(phase1.base_classes, dtype=np.uint8)

    finite = np.isfinite(xyz[:, 0]) & np.isfinite(xyz[:, 1]) & np.isfinite(xyz[:, 2]) & np.isfinite(hag)

    cl_active = cl_mask is not None
    if cl_mask is None:
        corridor = np.ones(n, dtype=bool)
    else:
        corridor = np.asarray(cl_mask, dtype=bool).reshape(-1)
        if len(corridor) != n:
            raise ValueError("Phase2 cl_mask length mismatch")

    # Keep a configurable context fringe beyond the strict CL width if distance
    # is available. This matters for support structures that sit on the edge of
    # a conductor corridor and for Phase 3 fitting across a fence boundary.
    if cl_dist is not None:
        cdist = np.asarray(cl_dist, dtype=np.float32).reshape(-1)
        if len(cdist) != n:
            raise ValueError("Phase2 cl_dist length mismatch")
        finite_cd = cdist[np.isfinite(cdist)]
        if finite_cd.size:
            strict_width = float(np.nanmax(cdist[corridor])) if np.any(corridor) else 0.0
            fringe = float(cfg.get("phase2_cl_context_fringe_m", 2.5))
            support_corridor = np.isfinite(cdist) & (cdist <= strict_width + fringe)
        else:
            support_corridor = corridor.copy()
    else:
        cdist = None
        support_corridor = corridor.copy()

    support = finite & support_corridor & (~protected)

    line_strength = np.maximum(l05, l10)
    vert_strength = np.maximum(v05, v10)

    # ------------------------------
    # RAW WIRE CANDIDATES
    # ------------------------------
    # Broad by design: Phase 3 will impose track continuity, sag/profile and
    # local-thickness constraints. We intentionally do not gate by base class,
    # because real wires may have been called Vegetation or Building.
    wire_hag_min = float(cfg.get("phase2_wire_hag_min", 2.0))
    wire_hag_max = float(cfg.get("phase2_wire_hag_max", 90.0))
    wire_line_min = float(cfg.get("phase2_wire_linearity_min", 0.38))
    wire_plan_max = float(cfg.get("phase2_wire_planarity_max", 0.60))
    wire_strong_line = float(cfg.get("phase2_wire_strong_linearity", 0.62))

    wire_geom = (
        ((line_strength >= wire_line_min) & (p05 <= wire_plan_max))
        | (line_strength >= wire_strong_line)
    )
    raw_wire = support & (hag >= wire_hag_min) & (hag <= wire_hag_max) & wire_geom

    # Score is diagnostic/ranking only in Phase 2. Phase 3 may use it to choose
    # seeds while still fitting against the full raw candidate cloud.
    line_term = line_strength
    plan_term = 1.0 - p05
    if cdist is not None:
        cl_term = 1.0 / (1.0 + np.maximum(cdist, 0.0))
        cl_term = np.clip(cl_term, 0.0, 1.0).astype(np.float32)
    else:
        cl_term = np.full(n, 0.5, dtype=np.float32)
    wire_score = (
        0.50 * line_term
        + 0.25 * plan_term
        + 0.20 * cl_term
        + 0.05 * np.clip((hag - wire_hag_min) / max(1.0, wire_hag_max - wire_hag_min), 0.0, 1.0)
    ).astype(np.float32)
    wire_score[~raw_wire] = 0.0

    # ------------------------------
    # RAW POLE/PYLON CANDIDATES
    # ------------------------------
    # Supports can be partly lattice/diagonal and may not be perfectly vertical
    # point-by-point, so the raw gate is deliberately broad. Confirmation is a
    # later conductor-anchored structural-core job.
    pole_hag_min = float(cfg.get("phase2_pole_hag_min", 0.6))
    pole_hag_max = float(cfg.get("phase2_pole_hag_max", 90.0))
    pole_vert_min = float(cfg.get("phase2_pole_verticality_min", 0.14))
    pole_line_min = float(cfg.get("phase2_pole_linearity_min", 0.28))
    pole_combo_vert = float(cfg.get("phase2_pole_combo_verticality", 0.08))

    pole_geom = (
        (vert_strength >= pole_vert_min)
        | ((line_strength >= pole_line_min) & (vert_strength >= pole_combo_vert))
    )
    raw_pole = support & (hag >= pole_hag_min) & (hag <= pole_hag_max) & pole_geom

    pole_score = (
        0.55 * vert_strength
        + 0.25 * line_strength
        + 0.15 * cl_term
        + 0.05 * np.clip(hag / max(1.0, pole_hag_max), 0.0, 1.0)
    ).astype(np.float32)
    pole_score[~raw_pole] = 0.0

    raw_union = raw_wire | raw_pole
    active_wire = raw_wire & active
    active_pole = raw_pole & active
    context_wire = raw_wire & (~active)
    context_pole = raw_pole & (~active)

    # Optional station array is validated now because Phase 3 will depend on it.
    if cl_station is not None:
        station = np.asarray(cl_station, dtype=np.float64).reshape(-1)
        if len(station) != n:
            raise ValueError("Phase2 cl_station length mismatch")

    return PowerPhase2Candidates(
        version=PHASE2_VERSION,
        point_count=n,
        support_mask=_freeze(support.copy()),
        raw_wire_mask=_freeze(raw_wire.copy()),
        raw_pole_mask=_freeze(raw_pole.copy()),
        raw_power_mask=_freeze(raw_union.copy()),
        wire_score=_freeze(wire_score.copy()),
        pole_score=_freeze(pole_score.copy()),
        active_wire_mask=_freeze(active_wire.copy()),
        active_pole_mask=_freeze(active_pole.copy()),
        context_wire_mask=_freeze(context_wire.copy()),
        context_pole_mask=_freeze(context_pole.copy()),
        cl_active=bool(cl_active),
        class_distribution_wire=_distribution(base_classes, raw_wire),
        class_distribution_pole=_distribution(base_classes, raw_pole),
    )
