from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

import numpy as np
from scipy.spatial import cKDTree

from .power_phase1_state import PowerPhase1State
from .power_phase3_tracks import PowerPhase3Tracks
from .power_phase7_validation import PowerPhase7Validation


PHASE8_VERSION = "NAKSHA_POWER_PHASE8_PRODUCTION_CONSISTENCY_V3_14"


def _freeze(arr: np.ndarray) -> np.ndarray:
    arr.setflags(write=False)
    return arr


def _groups_by_xy(indices: np.ndarray, xyz: np.ndarray, radius_m: float) -> Tuple[np.ndarray, ...]:
    """Group already-validated support points into physical XY components.

    Phase 8 never uses these groups to discover a new Pole/Pylon. They are only
    used to describe whether an already validated support is fully inside the
    exact fence, clipped by the fence, or context-only.
    """
    indices = np.asarray(indices, dtype=np.int64).reshape(-1)
    if indices.size == 0:
        return tuple()
    if indices.size == 1:
        return (indices.copy(),)

    pts = np.asarray(xyz, dtype=np.float64)[indices, :2]
    tree = cKDTree(pts)
    pairs = tree.query_pairs(float(radius_m), output_type="ndarray")
    del tree

    parent = np.arange(indices.size, dtype=np.int32)

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = int(parent[a])
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    if np.asarray(pairs).size:
        for a, b in np.asarray(pairs, dtype=np.int64).reshape(-1, 2):
            union(int(a), int(b))

    roots = np.asarray([find(i) for i in range(indices.size)], dtype=np.int32)
    return tuple(indices[roots == r] for r in np.unique(roots))


def _track_quality(tracks) -> dict:
    tracks = tuple(tracks or ())
    if not tracks:
        return {
            "track_count": 0,
            "median_span_m": 0.0,
            "min_coverage": 0.0,
            "max_lateral_residual_p90_m": 0.0,
            "max_z_residual_p90_m": 0.0,
        }
    spans = np.asarray([float(t.span) for t in tracks], dtype=np.float64)
    cov = np.asarray([float(t.coverage) for t in tracks], dtype=np.float64)
    lr = np.asarray([float(t.lateral_residual_p90) for t in tracks], dtype=np.float64)
    zr = np.asarray([float(t.z_residual_p90) for t in tracks], dtype=np.float64)
    return {
        "track_count": int(len(tracks)),
        "median_span_m": float(np.median(spans)),
        "min_coverage": float(np.min(cov)),
        "max_lateral_residual_p90_m": float(np.max(lr)),
        "max_z_residual_p90_m": float(np.max(zr)),
    }


@dataclass(frozen=True)
class PowerPhase8Consistency:
    version: str
    point_count: int
    status: str
    scenario: str
    route: str
    input_wire_mask: np.ndarray
    input_pole_context_mask: np.ndarray
    final_wire_mask: np.ndarray
    final_pole_context_mask: np.ndarray
    final_pole_active_mask: np.ndarray
    rejected_orphan_wire_mask: np.ndarray
    rejected_orphan_pole_mask: np.ndarray
    diagnostics: Tuple[dict, ...]
    track_quality: dict
    exact_fence_only: bool
    mode_independent: bool

    def report(self) -> dict:
        boundary = sum(1 for d in self.diagnostics if d.get("boundary_clipped"))
        context_only = sum(1 for d in self.diagnostics if d.get("context_only"))
        active_supports = sum(1 for d in self.diagnostics if int(d.get("active_points", 0)) > 0)
        return {
            "version": self.version,
            "status": self.status,
            "scenario": self.scenario,
            "route": self.route,
            "input_wire_points": int(np.count_nonzero(self.input_wire_mask)),
            "input_pole_context_points": int(np.count_nonzero(self.input_pole_context_mask)),
            "final_wire_points": int(np.count_nonzero(self.final_wire_mask)),
            "final_pole_context_points": int(np.count_nonzero(self.final_pole_context_mask)),
            "final_pole_active_points": int(np.count_nonzero(self.final_pole_active_mask)),
            "orphan_wire_rejected": int(np.count_nonzero(self.rejected_orphan_wire_mask)),
            "orphan_pole_rejected": int(np.count_nonzero(self.rejected_orphan_pole_mask)),
            "support_components": int(len(self.diagnostics)),
            "active_support_components": int(active_supports),
            "boundary_clipped_supports": int(boundary),
            "context_only_supports": int(context_only),
            "track_quality": dict(self.track_quality),
            "production_consistency": True,
            "wire_only_allowed": True,
            "support_only_allowed": True,
            "boundary_clip_preserved": True,
            "creates_new_assets": False,
            "semantic_class_gate": False,
            "mode_independent": bool(self.mode_independent),
            "exact_fence_only": bool(self.exact_fence_only),
            "immutable": bool(
                not self.final_wire_mask.flags.writeable
                and not self.final_pole_context_mask.flags.writeable
                and not self.final_pole_active_mask.flags.writeable
                and not self.rejected_orphan_wire_mask.flags.writeable
                and not self.rejected_orphan_pole_mask.flags.writeable
            ),
            "support_reports": [dict(d) for d in self.diagnostics],
        }


def build_power_phase8_consistency(
    xyz,
    phase1: PowerPhase1State,
    effective_phase3: PowerPhase3Tracks,
    phase7: PowerPhase7Validation,
    *,
    recovery_used: bool = False,
    cl_active: bool = True,
    config=None,
) -> PowerPhase8Consistency:
    """Production consistency / multi-scene firewall.

    Phase 8 is intentionally conservative. It does not detect or grow Wire or
    Pole/Pylon. Instead it checks that the Phase-7 validated masks are
    internally consistent for production scenes and preserves legitimate
    special cases that used to be dangerous for threshold-based logic:

    * a fence containing Wire but no support;
    * a fence containing a support while the conductor is context-only;
    * a Pole/Pylon clipped by the exact fence boundary;
    * the normal Phase-3 route and the Phase-6 recovery route;
    * no-power scenes.

    Defensive orphan rejection is applied only when a validated power mask
    exists without any confirmed conductor-track context. With CL-driven power
    classification this state is inconsistent and must never be frozen by
    Phase 4.
    """
    cfg = dict(config or {})
    xyz = np.asarray(xyz, dtype=np.float64)
    n = int(phase1.point_count)
    if xyz.ndim != 2 or xyz.shape[1] < 3 or len(xyz) != n:
        raise ValueError("Phase8 xyz length mismatch")
    if effective_phase3.point_count != n or phase7.point_count != n:
        raise ValueError("Phase8 prior-phase length mismatch")

    active = np.asarray(phase1.active_mask, dtype=bool)
    protected = np.asarray(phase1.protected_mask, dtype=bool)
    if len(active) != n or len(protected) != n:
        raise ValueError("Phase8 Phase1 mask length mismatch")

    wire_in = np.asarray(phase7.validated_wire_mask, dtype=bool).copy()
    pole_context_in = np.asarray(phase7.validated_pole_context_mask, dtype=bool).copy()

    # Phase 8 never expands assets. Exact-fence and protected-point contracts
    # are re-applied defensively before the masks reach Phase 4.
    wire_final = wire_in & active & (~protected)
    pole_context_final = pole_context_in & (~protected)

    track_count = int(len(effective_phase3.tracks))
    confirmed_context = np.asarray(effective_phase3.confirmed_context_mask, dtype=bool)
    projected_active = np.asarray(effective_phase3.projected_active_mask, dtype=bool)
    confirmed_count = int(np.count_nonzero(confirmed_context))

    orphan_wire = np.zeros(n, dtype=bool)
    orphan_pole = np.zeros(n, dtype=bool)

    if bool(cl_active):
        # Wire is valid only if it belongs to the effective normal/fallback
        # conductor solution. This should already be true after Phase 7; the
        # check is a final production firewall against future regressions.
        keep_wire = projected_active if track_count > 0 and confirmed_count > 0 else np.zeros(n, dtype=bool)
        orphan_wire = wire_final & (~keep_wire)
        wire_final &= keep_wire

        # A Pole/Pylon with no confirmed conductor context is an orphan support
        # in the CL-driven workflow. Phase 8 removes only this impossible state;
        # it does NOT require the conductor itself to be inside the exact fence.
        if track_count == 0 or confirmed_count == 0:
            orphan_pole = pole_context_final.copy()
            pole_context_final[:] = False

    pole_active_final = pole_context_final & active & (~protected)

    diagnostics = []
    groups = _groups_by_xy(
        np.flatnonzero(pole_context_final),
        xyz,
        float(cfg.get("phase8_support_component_xy_link_m", 2.25)),
    )
    boundary_fraction = float(cfg.get("phase8_boundary_clip_fraction", 0.85))
    boundary_min_context = int(cfg.get("phase8_boundary_clip_min_context_points", 25))

    for gid, gi in enumerate(groups):
        gi = np.asarray(gi, dtype=np.int64)
        active_count = int(np.count_nonzero(active[gi]))
        context_count = int(gi.size)
        active_fraction = float(active_count / max(1, context_count))
        context_only = bool(active_count == 0)
        boundary_clipped = bool(
            active_count > 0
            and context_count >= boundary_min_context
            and active_fraction < boundary_fraction
        )
        cxy = np.median(xyz[gi, :2], axis=0) if gi.size else np.array([np.nan, np.nan])
        diagnostics.append({
            "component": int(gid),
            "context_points": context_count,
            "active_points": active_count,
            "active_fraction": active_fraction,
            "context_only": context_only,
            "boundary_clipped": boundary_clipped,
            "center_x": float(cxy[0]),
            "center_y": float(cxy[1]),
            "preserved": True,
            "reason": (
                "context_only_support_preserved"
                if context_only else (
                    "boundary_clipped_support_preserved"
                    if boundary_clipped else "active_support_consistent"
                )
            ),
        })

    wire_count = int(np.count_nonzero(wire_final))
    pole_active_count = int(np.count_nonzero(pole_active_final))
    pole_context_count = int(np.count_nonzero(pole_context_final))

    if wire_count > 0 and pole_active_count > 0:
        scenario = "WIRE_AND_SUPPORT"
    elif wire_count > 0 and pole_active_count == 0 and pole_context_count > 0:
        scenario = "WIRE_ONLY_ACTIVE_SUPPORT_CONTEXT_OUTSIDE"
    elif wire_count > 0:
        scenario = "WIRE_ONLY_ACTIVE_FENCE"
    elif pole_active_count > 0 and track_count > 0:
        scenario = "SUPPORT_ONLY_ACTIVE_FENCE"
    elif pole_context_count > 0:
        scenario = "CONTEXT_ONLY_SUPPORT"
    elif track_count > 0 and confirmed_count > 0:
        scenario = "CONTEXT_ONLY_CONDUCTOR"
    else:
        scenario = "NO_POWER_ASSETS"

    orphan_count = int(np.count_nonzero(orphan_wire)) + int(np.count_nonzero(orphan_pole))
    boundary_count = sum(1 for d in diagnostics if d.get("boundary_clipped"))
    if orphan_count > 0:
        status = "PASS_WITH_ORPHAN_REJECTIONS"
    elif boundary_count > 0:
        status = "PASS_BOUNDARY_CLIPPED_SUPPORT"
    else:
        status = "PASS"

    route = "PHASE6_RECOVERY" if bool(recovery_used) else "NORMAL_PHASE3"

    return PowerPhase8Consistency(
        version=PHASE8_VERSION,
        point_count=n,
        status=status,
        scenario=scenario,
        route=route,
        input_wire_mask=_freeze(wire_in.copy()),
        input_pole_context_mask=_freeze(pole_context_in.copy()),
        final_wire_mask=_freeze(wire_final.copy()),
        final_pole_context_mask=_freeze(pole_context_final.copy()),
        final_pole_active_mask=_freeze(pole_active_final.copy()),
        rejected_orphan_wire_mask=_freeze(orphan_wire.copy()),
        rejected_orphan_pole_mask=_freeze(orphan_pole.copy()),
        diagnostics=tuple(diagnostics),
        track_quality=_track_quality(effective_phase3.tracks),
        exact_fence_only=True,
        mode_independent=True,
    )
