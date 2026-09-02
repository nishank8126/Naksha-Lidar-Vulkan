# from __future__ import annotations

# from dataclasses import dataclass
# from typing import Optional, Tuple

# import numpy as np
# from scipy.spatial import cKDTree

# from .power_phase1_state import PowerPhase1State
# from .power_phase2_candidates import PowerPhase2Candidates
# from .power_phase3_tracks import PowerPhase3Tracks


# PHASE7_VERSION = "NAKSHA_POWER_PHASE7_PRODUCTION_VALIDATION_V3_13"


# def _freeze(arr: np.ndarray) -> np.ndarray:
#     arr.setflags(write=False)
#     return arr


# def _safe01(arr) -> np.ndarray:
#     a = np.asarray(arr, dtype=np.float32).reshape(-1)
#     return np.clip(np.nan_to_num(a, nan=0.0, posinf=1.0, neginf=0.0), 0.0, 1.0)


# def _robust_span(v: np.ndarray, lo: float = 5.0, hi: float = 95.0) -> float:
#     v = np.asarray(v, dtype=np.float64)
#     v = v[np.isfinite(v)]
#     if v.size == 0:
#         return 0.0
#     if v.size < 5:
#         return float(np.ptp(v))
#     return float(np.percentile(v, hi) - np.percentile(v, lo))


# def _groups_by_station(indices: np.ndarray, station: np.ndarray, gap_m: float) -> Tuple[np.ndarray, ...]:
#     indices = np.asarray(indices, dtype=np.int64)
#     if indices.size == 0:
#         return tuple()
#     s = np.asarray(station, dtype=np.float64)[indices]
#     finite = np.isfinite(s)
#     good = indices[finite]
#     if good.size == 0:
#         return (indices,)
#     order = np.argsort(np.asarray(station)[good])
#     sorted_idx = good[order]
#     sv = np.asarray(station, dtype=np.float64)[sorted_idx]
#     cuts = np.flatnonzero(np.diff(sv) > float(gap_m)) + 1
#     groups = [g for g in np.split(sorted_idx, cuts) if g.size]
#     bad = indices[~finite]
#     if bad.size:
#         groups.append(bad)
#     return tuple(np.asarray(g, dtype=np.int64) for g in groups)


# def _groups_by_xy(indices: np.ndarray, xyz: np.ndarray, radius_m: float) -> Tuple[np.ndarray, ...]:
#     indices = np.asarray(indices, dtype=np.int64)
#     if indices.size == 0:
#         return tuple()
#     if indices.size == 1:
#         return (indices.copy(),)
#     pts = np.asarray(xyz, dtype=np.float64)[indices, :2]
#     tree = cKDTree(pts)
#     pairs = tree.query_pairs(float(radius_m), output_type="ndarray")
#     del tree
#     parent = np.arange(indices.size, dtype=np.int32)

#     def find(a: int) -> int:
#         while parent[a] != a:
#             parent[a] = parent[parent[a]]
#             a = int(parent[a])
#         return a

#     def union(a: int, b: int) -> None:
#         ra, rb = find(a), find(b)
#         if ra != rb:
#             parent[rb] = ra

#     if np.asarray(pairs).size:
#         for a, b in np.asarray(pairs, dtype=np.int64).reshape(-1, 2):
#             union(int(a), int(b))
#     roots = np.asarray([find(i) for i in range(indices.size)], dtype=np.int32)
#     groups = []
#     for r in np.unique(roots):
#         groups.append(indices[roots == r])
#     return tuple(groups)


# @dataclass(frozen=True)
# class PowerPhase7Validation:
#     version: str
#     point_count: int
#     status: str
#     input_wire_mask: np.ndarray
#     input_pole_context_mask: np.ndarray
#     validated_wire_mask: np.ndarray
#     validated_pole_context_mask: np.ndarray
#     validated_pole_active_mask: np.ndarray
#     rejected_pole_context_mask: np.ndarray
#     diagnostics: Tuple[dict, ...]
#     cl_active: bool
#     exact_fence_only: bool

#     def report(self) -> dict:
#         accepted_components = sum(1 for d in self.diagnostics if d.get("accepted"))
#         rejected_components = sum(1 for d in self.diagnostics if not d.get("accepted"))
#         return {
#             "version": self.version,
#             "status": self.status,
#             "input_wire_points": int(np.count_nonzero(self.input_wire_mask)),
#             "validated_wire_points": int(np.count_nonzero(self.validated_wire_mask)),
#             "input_pole_context_points": int(np.count_nonzero(self.input_pole_context_mask)),
#             "validated_pole_context_points": int(np.count_nonzero(self.validated_pole_context_mask)),
#             "validated_pole_active_points": int(np.count_nonzero(self.validated_pole_active_mask)),
#             "rejected_pole_context_points": int(np.count_nonzero(self.rejected_pole_context_mask)),
#             "accepted_support_components": int(accepted_components),
#             "rejected_support_components": int(rejected_components),
#             "false_positive_guard": True,
#             "semantic_class_gate": False,
#             "cl_active": bool(self.cl_active),
#             "exact_fence_only": bool(self.exact_fence_only),
#             "immutable": bool(
#                 not self.validated_wire_mask.flags.writeable
#                 and not self.validated_pole_context_mask.flags.writeable
#                 and not self.validated_pole_active_mask.flags.writeable
#                 and not self.rejected_pole_context_mask.flags.writeable
#             ),
#             "support_reports": [dict(d) for d in self.diagnostics],
#         }


# def build_power_phase7_validation(
#     xyz,
#     hag,
#     phase1: PowerPhase1State,
#     phase2: PowerPhase2Candidates,
#     effective_phase3: PowerPhase3Tracks,
#     *,
#     wire_indices,
#     pole_context_indices,
#     linearity_05,
#     verticality_05,
#     linearity_10,
#     verticality_10,
#     cl_station: Optional[np.ndarray] = None,
#     cl_dist: Optional[np.ndarray] = None,
#     cl_width: float = 0.0,
#     config=None,
# ) -> PowerPhase7Validation:
#     """Production validation / false-positive guard for confirmed Power assets.

#     Phase 7 is deliberately *not* another detector.  It validates the assets
#     produced by Phases 3/6/5 before Phase 4 freezes them.  The guard is designed
#     to reject clear tree/vegetation-shaped support leakage without changing a
#     physically coherent narrow pole or lattice tower.

#     Contract
#     --------
#     * Never uses the base semantic class as a hard inclusion gate.
#     * Never writes classification values itself.
#     * Wire is accepted only inside the exact Phase-1 writable scope.
#     * Pole/Pylon support is validated as physical components using height,
#       structural geometry, terrain reach and conductor attachment evidence.
#     * Context may validate a support, but the active Pole mask remains exact
#       fence only.
#     """
#     cfg = dict(config or {})
#     xyz = np.asarray(xyz, dtype=np.float64)
#     hag = np.asarray(hag, dtype=np.float32).reshape(-1)
#     n = int(phase1.point_count)
#     if xyz.ndim != 2 or xyz.shape[1] < 3 or len(xyz) != n or len(hag) != n:
#         raise ValueError("Phase7 xyz/hag length mismatch")
#     if phase2.point_count != n or effective_phase3.point_count != n:
#         raise ValueError("Phase7 prior-phase length mismatch")

#     l05 = _safe01(linearity_05)
#     v05 = _safe01(verticality_05)
#     l10 = _safe01(linearity_10)
#     v10 = _safe01(verticality_10)
#     for arr in (l05, v05, l10, v10):
#         if len(arr) != n:
#             raise ValueError("Phase7 geometry-feature length mismatch")

#     active = np.asarray(phase1.active_mask, dtype=bool)
#     protected = np.asarray(phase1.protected_mask, dtype=bool)

#     wire_in = np.zeros(n, dtype=bool)
#     wi = np.asarray(wire_indices, dtype=np.int64).reshape(-1)
#     wi = wi[(wi >= 0) & (wi < n)]
#     if wi.size:
#         wire_in[np.unique(wi)] = True
#     # Phase 7 can never expand Wire. It only enforces exact-fence / protection.
#     wire_valid = wire_in & active & (~protected)
#     if bool(phase2.cl_active):
#         # With CL active, conductor geometry must have survived Phase 3 or 6.
#         if len(effective_phase3.tracks) == 0:
#             wire_valid[:] = False
#         else:
#             wire_valid &= np.asarray(effective_phase3.projected_active_mask, dtype=bool)

#     pole_in = np.zeros(n, dtype=bool)
#     pi = np.asarray(pole_context_indices, dtype=np.int64).reshape(-1)
#     pi = pi[(pi >= 0) & (pi < n)]
#     if pi.size:
#         pole_in[np.unique(pi)] = True
#     pole_in &= ~protected

#     if not np.any(pole_in):
#         z = np.zeros(n, dtype=bool)
#         status = "NO_POLE_SUPPORT_TO_VALIDATE" if np.any(wire_valid) else "NO_POWER_ASSETS"
#         return PowerPhase7Validation(
#             version=PHASE7_VERSION,
#             point_count=n,
#             status=status,
#             input_wire_mask=_freeze(wire_in.copy()),
#             input_pole_context_mask=_freeze(pole_in.copy()),
#             validated_wire_mask=_freeze(wire_valid.copy()),
#             validated_pole_context_mask=_freeze(z.copy()),
#             validated_pole_active_mask=_freeze(z.copy()),
#             rejected_pole_context_mask=_freeze(z.copy()),
#             diagnostics=tuple(),
#             cl_active=bool(phase2.cl_active),
#             exact_fence_only=True,
#         )

#     station = None
#     if cl_station is not None:
#         station = np.asarray(cl_station, dtype=np.float64).reshape(-1)
#         if len(station) != n:
#             raise ValueError("Phase7 cl_station length mismatch")
#     cdist = None
#     if cl_dist is not None:
#         cdist = np.asarray(cl_dist, dtype=np.float32).reshape(-1)
#         if len(cdist) != n:
#             raise ValueError("Phase7 cl_dist length mismatch")

#     pidx = np.flatnonzero(pole_in)
#     # Physical support components are grouped in XY rather than by station
#     # alone.  A tree can sit at the same CL station as a tower; station-only
#     # grouping would merge both objects and defeat the false-positive guard.
#     groups = _groups_by_xy(pidx, xyz, float(cfg.get("phase7_component_xy_link_m", 2.0)))
#     grouping = "XY_CONNECTIVITY"

#     line_strength = np.maximum(l05, l10)
#     vert_strength = np.maximum(v05, v10)
#     pole_score = np.asarray(phase2.pole_score, dtype=np.float32)

#     wire_context = np.asarray(effective_phase3.confirmed_context_mask, dtype=bool)
#     wire_context_idx = np.flatnonzero(wire_context)
#     wire_tree = cKDTree(xyz[wire_context_idx, :2]) if wire_context_idx.size else None

#     validated_pole = np.zeros(n, dtype=bool)
#     rejected_pole = np.zeros(n, dtype=bool)
#     diagnostics = []

#     min_points = int(cfg.get("phase7_component_min_points", 25))
#     for gid, gi0 in enumerate(groups):
#         gi0 = np.asarray(gi0, dtype=np.int64)
#         if gi0.size == 0:
#             continue

#         # Keep structural lattice/vertical/diagonal points. This trims obvious
#         # leafy fringe but deliberately retains both vertical legs and diagonal
#         # braces of a pylon.
#         point_structural = (
#             (vert_strength[gi0] >= float(cfg.get("phase7_point_verticality_min", 0.30)))
#             | (line_strength[gi0] >= float(cfg.get("phase7_point_linearity_min", 0.42)))
#         )
#         gi = gi0[point_structural]
#         if gi.size < min_points:
#             gi = gi0

#         hv = hag[gi]
#         height = _robust_span(hv, 3.0, 98.0)
#         low_hag = float(np.percentile(hv[np.isfinite(hv)], 10)) if np.count_nonzero(np.isfinite(hv)) else float("inf")
#         xspan = _robust_span(xyz[gi, 0])
#         yspan = _robust_span(xyz[gi, 1])
#         xy_span = float(np.hypot(xspan, yspan))
#         station_span = _robust_span(station[gi]) if station is not None else 0.0
#         structural_fraction = float(np.mean(
#             (vert_strength[gi] >= float(cfg.get("phase7_structural_verticality", 0.32)))
#             | (line_strength[gi] >= float(cfg.get("phase7_structural_linearity", 0.52)))
#         )) if gi.size else 0.0
#         strong_fraction = float(np.mean(
#             (vert_strength[gi] >= float(cfg.get("phase7_strong_verticality", 0.45)))
#             | (line_strength[gi] >= float(cfg.get("phase7_strong_linearity", 0.68)))
#         )) if gi.size else 0.0
#         median_vert = float(np.median(vert_strength[gi])) if gi.size else 0.0
#         median_line = float(np.median(line_strength[gi])) if gi.size else 0.0
#         median_score = float(np.median(pole_score[gi])) if gi.size else 0.0

#         top_links = 0
#         top_points = 0
#         if wire_tree is not None and gi.size:
#             top_cut = float(np.percentile(hv, 65)) if hv.size else 0.0
#             top_idx = gi[hv >= top_cut]
#             top_points = int(top_idx.size)
#             if top_idx.size:
#                 dxy, nn = wire_tree.query(xyz[top_idx, :2], k=1, workers=-1)
#                 nn = np.asarray(nn, dtype=np.int64)
#                 dz = np.abs(xyz[top_idx, 2] - xyz[wire_context_idx[nn], 2])
#                 linked = (
#                     (np.asarray(dxy) <= float(cfg.get("phase7_top_link_xy_m", 2.75)))
#                     & (dz <= float(cfg.get("phase7_top_link_z_m", 3.0)))
#                 )
#                 top_links = int(np.count_nonzero(linked))
#         top_link_fraction = float(top_links / max(1, top_points))

#         center_cl_dist = float(np.median(cdist[gi])) if cdist is not None and gi.size else 0.0
#         base_ok = bool(
#             low_hag <= float(cfg.get("phase7_base_reach_hag_m", 6.5))
#             or height >= float(cfg.get("phase7_tall_support_height_m", 10.0))
#         )
#         attachment_ok = bool(
#             top_links >= int(cfg.get("phase7_min_top_links", 3))
#             or top_link_fraction >= float(cfg.get("phase7_min_top_link_fraction", 0.025))
#         )
#         corridor_ok = bool(
#             cdist is None
#             or center_cl_dist <= max(float(cl_width or 0.0) + float(cfg.get("phase7_cl_guard_margin_m", 2.5)), 4.0)
#         )

#         narrow = bool(
#             gi.size >= min_points
#             and height >= float(cfg.get("phase7_narrow_min_height_m", 4.0))
#             and xy_span <= float(cfg.get("phase7_narrow_max_xy_m", 4.8))
#             and median_vert >= float(cfg.get("phase7_narrow_min_verticality", 0.34))
#             and structural_fraction >= float(cfg.get("phase7_narrow_min_structural_fraction", 0.48))
#             and attachment_ok and base_ok and corridor_ok
#         )
#         lattice = bool(
#             gi.size >= int(cfg.get("phase7_lattice_min_points", 40))
#             and height >= float(cfg.get("phase7_lattice_min_height_m", 6.0))
#             and xy_span <= float(cfg.get("phase7_lattice_max_xy_m", 16.0))
#             and structural_fraction >= float(cfg.get("phase7_lattice_min_structural_fraction", 0.40))
#             and strong_fraction >= float(cfg.get("phase7_lattice_min_strong_fraction", 0.16))
#             and median_score >= float(cfg.get("phase7_lattice_min_pole_score", 0.22))
#             and attachment_ok and base_ok and corridor_ok
#         )
#         strong_attachment = bool(
#             gi.size >= min_points
#             and height >= float(cfg.get("phase7_rescue_min_height_m", 7.0))
#             and top_links >= int(cfg.get("phase7_rescue_min_top_links", 8))
#             and structural_fraction >= float(cfg.get("phase7_rescue_min_structural_fraction", 0.34))
#             and median_score >= float(cfg.get("phase7_rescue_min_pole_score", 0.20))
#             and base_ok and corridor_ok
#         )

#         accepted = bool(narrow or lattice or strong_attachment)
#         kind = "NARROW_POLE" if narrow else ("LATTICE_PYLON" if lattice else ("ATTACHMENT_RESCUE" if strong_attachment else "REJECT"))
#         reason = "physical_support_validated" if accepted else "false_positive_guard_reject"

#         # Reject only clear non-structural / non-attached support leakage. When
#         # a support is accepted, retain the structural subset rather than the
#         # full input fringe. This is the production containment step.
#         if accepted:
#             validated_pole[gi] = True
#         else:
#             rejected_pole[gi0] = True

#         diagnostics.append({
#             "component": int(gid),
#             "grouping": grouping,
#             "input_points": int(gi0.size),
#             "structural_points": int(gi.size),
#             "height": float(height),
#             "xy_span": float(xy_span),
#             "station_span": float(station_span),
#             "low_hag_p10": float(low_hag),
#             "median_verticality": float(median_vert),
#             "median_linearity": float(median_line),
#             "structural_fraction": float(structural_fraction),
#             "strong_fraction": float(strong_fraction),
#             "pole_score": float(median_score),
#             "top_links": int(top_links),
#             "top_points": int(top_points),
#             "top_link_fraction": float(top_link_fraction),
#             "center_cl_dist": float(center_cl_dist),
#             "base_ok": bool(base_ok),
#             "attachment_ok": bool(attachment_ok),
#             "corridor_ok": bool(corridor_ok),
#             "accepted": bool(accepted),
#             "kind": kind,
#             "reason": reason,
#         })

#     if wire_tree is not None:
#         del wire_tree

#     pole_active = validated_pole & active & (~protected)
#     rejected_pole |= pole_in & (~validated_pole)

#     rejected_count = int(np.count_nonzero(rejected_pole))
#     accepted_count = int(np.count_nonzero(validated_pole))
#     if accepted_count and rejected_count:
#         status = "PASS_WITH_FALSE_POSITIVE_REJECTIONS"
#     elif accepted_count:
#         status = "PASS"
#     elif np.any(pole_in):
#         status = "ALL_POLE_SUPPORT_REJECTED"
#     else:
#         status = "NO_POLE_SUPPORT_TO_VALIDATE"

#     return PowerPhase7Validation(
#         version=PHASE7_VERSION,
#         point_count=n,
#         status=status,
#         input_wire_mask=_freeze(wire_in.copy()),
#         input_pole_context_mask=_freeze(pole_in.copy()),
#         validated_wire_mask=_freeze(wire_valid.copy()),
#         validated_pole_context_mask=_freeze(validated_pole.copy()),
#         validated_pole_active_mask=_freeze(pole_active.copy()),
#         rejected_pole_context_mask=_freeze(rejected_pole.copy()),
#         diagnostics=tuple(diagnostics),
#         cl_active=bool(phase2.cl_active),
#         exact_fence_only=True,
#     )
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
from scipy.spatial import cKDTree

from .power_phase1_state import PowerPhase1State
from .power_phase2_candidates import PowerPhase2Candidates
from .power_phase3_tracks import PowerPhase3Tracks


PHASE7_VERSION = "NAKSHA_POWER_PHASE7_PRODUCTION_VALIDATION_V3_28_RAW_ATTACHMENT_RESCUE"


def _freeze(arr: np.ndarray) -> np.ndarray:
    arr.setflags(write=False)
    return arr


def _safe01(arr) -> np.ndarray:
    a = np.asarray(arr, dtype=np.float32).reshape(-1)
    return np.clip(np.nan_to_num(a, nan=0.0, posinf=1.0, neginf=0.0), 0.0, 1.0)


def _robust_span(v: np.ndarray, lo: float = 5.0, hi: float = 95.0) -> float:
    v = np.asarray(v, dtype=np.float64)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return 0.0
    if v.size < 5:
        return float(np.ptp(v))
    return float(np.percentile(v, hi) - np.percentile(v, lo))


def _groups_by_station(indices: np.ndarray, station: np.ndarray, gap_m: float) -> Tuple[np.ndarray, ...]:
    indices = np.asarray(indices, dtype=np.int64)
    if indices.size == 0:
        return tuple()
    s = np.asarray(station, dtype=np.float64)[indices]
    finite = np.isfinite(s)
    good = indices[finite]
    if good.size == 0:
        return (indices,)
    order = np.argsort(np.asarray(station)[good])
    sorted_idx = good[order]
    sv = np.asarray(station, dtype=np.float64)[sorted_idx]
    cuts = np.flatnonzero(np.diff(sv) > float(gap_m)) + 1
    groups = [g for g in np.split(sorted_idx, cuts) if g.size]
    bad = indices[~finite]
    if bad.size:
        groups.append(bad)
    return tuple(np.asarray(g, dtype=np.int64) for g in groups)


def _groups_by_xy(indices: np.ndarray, xyz: np.ndarray, radius_m: float) -> Tuple[np.ndarray, ...]:
    indices = np.asarray(indices, dtype=np.int64)
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
    groups = []
    for r in np.unique(roots):
        groups.append(indices[roots == r])
    return tuple(groups)


@dataclass(frozen=True)
class PowerPhase7Validation:
    version: str
    point_count: int
    status: str
    input_wire_mask: np.ndarray
    input_pole_context_mask: np.ndarray
    validated_wire_mask: np.ndarray
    validated_pole_context_mask: np.ndarray
    validated_pole_active_mask: np.ndarray
    rejected_pole_context_mask: np.ndarray
    diagnostics: Tuple[dict, ...]
    cl_active: bool
    exact_fence_only: bool

    def report(self) -> dict:
        accepted_components = sum(1 for d in self.diagnostics if d.get("accepted"))
        rejected_components = sum(1 for d in self.diagnostics if not d.get("accepted"))
        return {
            "version": self.version,
            "status": self.status,
            "input_wire_points": int(np.count_nonzero(self.input_wire_mask)),
            "validated_wire_points": int(np.count_nonzero(self.validated_wire_mask)),
            "input_pole_context_points": int(np.count_nonzero(self.input_pole_context_mask)),
            "validated_pole_context_points": int(np.count_nonzero(self.validated_pole_context_mask)),
            "validated_pole_active_points": int(np.count_nonzero(self.validated_pole_active_mask)),
            "rejected_pole_context_points": int(np.count_nonzero(self.rejected_pole_context_mask)),
            "accepted_support_components": int(accepted_components),
            "rejected_support_components": int(rejected_components),
            "false_positive_guard": True,
            "semantic_class_gate": False,
            "cl_active": bool(self.cl_active),
            "exact_fence_only": bool(self.exact_fence_only),
            "immutable": bool(
                not self.validated_wire_mask.flags.writeable
                and not self.validated_pole_context_mask.flags.writeable
                and not self.validated_pole_active_mask.flags.writeable
                and not self.rejected_pole_context_mask.flags.writeable
            ),
            "support_reports": [dict(d) for d in self.diagnostics],
        }


def build_power_phase7_validation(
    xyz,
    hag,
    phase1: PowerPhase1State,
    phase2: PowerPhase2Candidates,
    effective_phase3: PowerPhase3Tracks,
    *,
    wire_indices,
    pole_context_indices,
    linearity_05,
    verticality_05,
    linearity_10,
    verticality_10,
    cl_station: Optional[np.ndarray] = None,
    cl_dist: Optional[np.ndarray] = None,
    cl_width: float = 0.0,
    config=None,
) -> PowerPhase7Validation:
    """Production validation / false-positive guard for confirmed Power assets.

    Phase 7 is deliberately *not* another detector.  It validates the assets
    produced by Phases 3/6/5 before Phase 4 freezes them.  The guard is designed
    to reject clear tree/vegetation-shaped support leakage without changing a
    physically coherent narrow pole or lattice tower.

    Contract
    --------
    * Never uses the base semantic class as a hard inclusion gate.
    * Never writes classification values itself.
    * Wire is accepted only inside the exact Phase-1 writable scope.
    * Pole/Pylon support is validated as physical components using height,
      structural geometry, terrain reach and conductor attachment evidence.
    * Context may validate a support, but the active Pole mask remains exact
      fence only.
    """
    cfg = dict(config or {})
    xyz = np.asarray(xyz, dtype=np.float64)
    hag = np.asarray(hag, dtype=np.float32).reshape(-1)
    n = int(phase1.point_count)
    if xyz.ndim != 2 or xyz.shape[1] < 3 or len(xyz) != n or len(hag) != n:
        raise ValueError("Phase7 xyz/hag length mismatch")
    if phase2.point_count != n or effective_phase3.point_count != n:
        raise ValueError("Phase7 prior-phase length mismatch")

    l05 = _safe01(linearity_05)
    v05 = _safe01(verticality_05)
    l10 = _safe01(linearity_10)
    v10 = _safe01(verticality_10)
    for arr in (l05, v05, l10, v10):
        if len(arr) != n:
            raise ValueError("Phase7 geometry-feature length mismatch")

    active = np.asarray(phase1.active_mask, dtype=bool)
    protected = np.asarray(phase1.protected_mask, dtype=bool)

    wire_in = np.zeros(n, dtype=bool)
    wi = np.asarray(wire_indices, dtype=np.int64).reshape(-1)
    wi = wi[(wi >= 0) & (wi < n)]
    if wi.size:
        wire_in[np.unique(wi)] = True
    # Phase 7 can never expand Wire. It only enforces exact-fence / protection.
    wire_valid = wire_in & active & (~protected)
    if bool(phase2.cl_active):
        # With CL active, conductor geometry must have survived Phase 3 or 6.
        if len(effective_phase3.tracks) == 0:
            wire_valid[:] = False
        else:
            wire_valid &= np.asarray(effective_phase3.projected_active_mask, dtype=bool)

    pole_in = np.zeros(n, dtype=bool)
    pi = np.asarray(pole_context_indices, dtype=np.int64).reshape(-1)
    pi = pi[(pi >= 0) & (pi < n)]
    if pi.size:
        pole_in[np.unique(pi)] = True
    pole_in &= ~protected

    if not np.any(pole_in):
        z = np.zeros(n, dtype=bool)
        status = "NO_POLE_SUPPORT_TO_VALIDATE" if np.any(wire_valid) else "NO_POWER_ASSETS"
        return PowerPhase7Validation(
            version=PHASE7_VERSION,
            point_count=n,
            status=status,
            input_wire_mask=_freeze(wire_in.copy()),
            input_pole_context_mask=_freeze(pole_in.copy()),
            validated_wire_mask=_freeze(wire_valid.copy()),
            validated_pole_context_mask=_freeze(z.copy()),
            validated_pole_active_mask=_freeze(z.copy()),
            rejected_pole_context_mask=_freeze(z.copy()),
            diagnostics=tuple(),
            cl_active=bool(phase2.cl_active),
            exact_fence_only=True,
        )

    station = None
    if cl_station is not None:
        station = np.asarray(cl_station, dtype=np.float64).reshape(-1)
        if len(station) != n:
            raise ValueError("Phase7 cl_station length mismatch")
    cdist = None
    if cl_dist is not None:
        cdist = np.asarray(cl_dist, dtype=np.float32).reshape(-1)
        if len(cdist) != n:
            raise ValueError("Phase7 cl_dist length mismatch")

    pidx = np.flatnonzero(pole_in)
    # Physical support components are grouped in XY rather than by station
    # alone.  A tree can sit at the same CL station as a tower; station-only
    # grouping would merge both objects and defeat the false-positive guard.
    groups = _groups_by_xy(pidx, xyz, float(cfg.get("phase7_component_xy_link_m", 2.0)))
    grouping = "XY_CONNECTIVITY"

    line_strength = np.maximum(l05, l10)
    vert_strength = np.maximum(v05, v10)
    pole_score = np.asarray(phase2.pole_score, dtype=np.float32)

    wire_context = np.asarray(effective_phase3.confirmed_context_mask, dtype=bool)
    wire_context_idx = np.flatnonzero(wire_context)
    wire_tree = cKDTree(xyz[wire_context_idx, :2]) if wire_context_idx.size else None

    # Backup attachment evidence for the specific failure mode where Phase 2
    # sees the conductor but long-scale Phase 3 confirms only a distant fragment.
    # Exclude the proposed pole body itself so a lattice brace cannot "attach"
    # to another point in the same support component.
    raw_wire_external = (
        np.asarray(phase2.raw_wire_mask, dtype=bool)
        & (~pole_in)
        & (~protected)
    )
    raw_wire_idx = np.flatnonzero(raw_wire_external)
    raw_wire_tree = cKDTree(xyz[raw_wire_idx, :2]) if raw_wire_idx.size else None

    validated_pole = np.zeros(n, dtype=bool)
    rejected_pole = np.zeros(n, dtype=bool)
    diagnostics = []

    min_points = int(cfg.get("phase7_component_min_points", 25))
    for gid, gi0 in enumerate(groups):
        gi0 = np.asarray(gi0, dtype=np.int64)
        if gi0.size == 0:
            continue

        # Keep structural lattice/vertical/diagonal points. This trims obvious
        # leafy fringe but deliberately retains both vertical legs and diagonal
        # braces of a pylon.
        point_structural = (
            (vert_strength[gi0] >= float(cfg.get("phase7_point_verticality_min", 0.30)))
            | (line_strength[gi0] >= float(cfg.get("phase7_point_linearity_min", 0.42)))
        )
        gi = gi0[point_structural]
        if gi.size < min_points:
            gi = gi0

        hv = hag[gi]
        height = _robust_span(hv, 3.0, 98.0)
        low_hag = float(np.percentile(hv[np.isfinite(hv)], 10)) if np.count_nonzero(np.isfinite(hv)) else float("inf")
        xspan = _robust_span(xyz[gi, 0])
        yspan = _robust_span(xyz[gi, 1])
        xy_span = float(np.hypot(xspan, yspan))
        station_span = _robust_span(station[gi]) if station is not None else 0.0
        structural_fraction = float(np.mean(
            (vert_strength[gi] >= float(cfg.get("phase7_structural_verticality", 0.32)))
            | (line_strength[gi] >= float(cfg.get("phase7_structural_linearity", 0.52)))
        )) if gi.size else 0.0
        strong_fraction = float(np.mean(
            (vert_strength[gi] >= float(cfg.get("phase7_strong_verticality", 0.45)))
            | (line_strength[gi] >= float(cfg.get("phase7_strong_linearity", 0.68)))
        )) if gi.size else 0.0
        median_vert = float(np.median(vert_strength[gi])) if gi.size else 0.0
        median_line = float(np.median(line_strength[gi])) if gi.size else 0.0
        median_score = float(np.median(pole_score[gi])) if gi.size else 0.0

        top_links = 0
        raw_top_links = 0
        top_points = 0
        top_idx = np.empty(0, dtype=np.int64)
        if gi.size:
            top_cut = float(np.percentile(hv, 65)) if hv.size else 0.0
            top_idx = gi[hv >= top_cut]
            top_points = int(top_idx.size)
        if wire_tree is not None and top_idx.size:
            dxy, nn = wire_tree.query(xyz[top_idx, :2], k=1, workers=-1)
            nn = np.asarray(nn, dtype=np.int64)
            dz = np.abs(xyz[top_idx, 2] - xyz[wire_context_idx[nn], 2])
            linked = (
                (np.asarray(dxy) <= float(cfg.get("phase7_top_link_xy_m", 2.75)))
                & (dz <= float(cfg.get("phase7_top_link_z_m", 3.0)))
            )
            top_links = int(np.count_nonzero(linked))
        if raw_wire_tree is not None and top_idx.size:
            dxy2, nn2 = raw_wire_tree.query(xyz[top_idx, :2], k=1, workers=-1)
            nn2 = np.asarray(nn2, dtype=np.int64)
            dz2 = np.abs(xyz[top_idx, 2] - xyz[raw_wire_idx[nn2], 2])
            raw_linked = (
                (np.asarray(dxy2) <= float(cfg.get("phase7_raw_wire_link_xy_m", 2.0)))
                & (dz2 <= float(cfg.get("phase7_raw_wire_link_z_m", 2.2)))
            )
            raw_top_links = int(np.count_nonzero(raw_linked))
        top_link_fraction = float(top_links / max(1, top_points))
        raw_top_link_fraction = float(raw_top_links / max(1, top_points))

        center_cl_dist = float(np.median(cdist[gi])) if cdist is not None and gi.size else 0.0
        base_ok = bool(
            low_hag <= float(cfg.get("phase7_base_reach_hag_m", 6.5))
            or height >= float(cfg.get("phase7_tall_support_height_m", 10.0))
        )
        attachment_ok = bool(
            top_links >= int(cfg.get("phase7_min_top_links", 3))
            or top_link_fraction >= float(cfg.get("phase7_min_top_link_fraction", 0.025))
        )
        raw_attachment_ok = bool(
            raw_top_links >= int(cfg.get("phase7_raw_min_top_links", 6))
            or raw_top_link_fraction >= float(cfg.get("phase7_raw_min_top_link_fraction", 0.020))
        )
        corridor_ok = bool(
            cdist is None
            or center_cl_dist <= max(float(cl_width or 0.0) + float(cfg.get("phase7_cl_guard_margin_m", 2.5)), 4.0)
        )

        narrow = bool(
            gi.size >= min_points
            and height >= float(cfg.get("phase7_narrow_min_height_m", 4.0))
            and xy_span <= float(cfg.get("phase7_narrow_max_xy_m", 4.8))
            and median_vert >= float(cfg.get("phase7_narrow_min_verticality", 0.34))
            and structural_fraction >= float(cfg.get("phase7_narrow_min_structural_fraction", 0.48))
            and attachment_ok and base_ok and corridor_ok
        )
        lattice = bool(
            gi.size >= int(cfg.get("phase7_lattice_min_points", 40))
            and height >= float(cfg.get("phase7_lattice_min_height_m", 6.0))
            and xy_span <= float(cfg.get("phase7_lattice_max_xy_m", 16.0))
            and structural_fraction >= float(cfg.get("phase7_lattice_min_structural_fraction", 0.40))
            and strong_fraction >= float(cfg.get("phase7_lattice_min_strong_fraction", 0.16))
            and median_score >= float(cfg.get("phase7_lattice_min_pole_score", 0.22))
            and attachment_ok and base_ok and corridor_ok
        )
        strong_attachment = bool(
            gi.size >= min_points
            and height >= float(cfg.get("phase7_rescue_min_height_m", 7.0))
            and top_links >= int(cfg.get("phase7_rescue_min_top_links", 8))
            and structural_fraction >= float(cfg.get("phase7_rescue_min_structural_fraction", 0.34))
            and median_score >= float(cfg.get("phase7_rescue_min_pole_score", 0.20))
            and base_ok and corridor_ok
        )

        # If confirmed Wire attachment is absent only because Phase 3 was
        # incomplete, a very strong structural support may use tight raw Phase-2
        # conductor attachment as backup evidence.  This path is intentionally
        # much stricter than normal validation and cannot use self-support points.
        raw_attachment_rescue = bool(
            (not attachment_ok)
            and raw_attachment_ok
            and gi.size >= int(cfg.get("phase7_raw_rescue_min_points", 40))
            and height >= float(cfg.get("phase7_raw_rescue_min_height_m", 5.5))
            and xy_span <= float(cfg.get("phase7_raw_rescue_max_xy_m", 16.0))
            and low_hag <= float(cfg.get("phase7_raw_rescue_base_hag_m", 5.5))
            and structural_fraction >= float(cfg.get("phase7_raw_rescue_min_structural_fraction", 0.72))
            and strong_fraction >= float(cfg.get("phase7_raw_rescue_min_strong_fraction", 0.30))
            and median_score >= float(cfg.get("phase7_raw_rescue_min_pole_score", 0.34))
            and (
                median_vert >= float(cfg.get("phase7_raw_rescue_min_verticality", 0.42))
                or median_line >= float(cfg.get("phase7_raw_rescue_min_linearity", 0.55))
            )
            and base_ok and corridor_ok
        )

        accepted = bool(narrow or lattice or strong_attachment or raw_attachment_rescue)
        kind = (
            "NARROW_POLE" if narrow else
            ("LATTICE_PYLON" if lattice else
             ("ATTACHMENT_RESCUE" if strong_attachment else
              ("RAW_WIRE_ATTACHMENT_RESCUE" if raw_attachment_rescue else "REJECT")))
        )
        reason = "physical_support_validated" if accepted else "false_positive_guard_reject"

        # Reject only clear non-structural / non-attached support leakage. When
        # a support is accepted, retain the structural subset rather than the
        # full input fringe. This is the production containment step.
        if accepted:
            validated_pole[gi] = True
        else:
            rejected_pole[gi0] = True

        diagnostics.append({
            "component": int(gid),
            "grouping": grouping,
            "input_points": int(gi0.size),
            "structural_points": int(gi.size),
            "height": float(height),
            "xy_span": float(xy_span),
            "station_span": float(station_span),
            "low_hag_p10": float(low_hag),
            "median_verticality": float(median_vert),
            "median_linearity": float(median_line),
            "structural_fraction": float(structural_fraction),
            "strong_fraction": float(strong_fraction),
            "pole_score": float(median_score),
            "top_links": int(top_links),
            "raw_top_links": int(raw_top_links),
            "top_points": int(top_points),
            "top_link_fraction": float(top_link_fraction),
            "raw_top_link_fraction": float(raw_top_link_fraction),
            "raw_attachment_ok": bool(raw_attachment_ok),
            "raw_attachment_rescue": bool(raw_attachment_rescue),
            "center_cl_dist": float(center_cl_dist),
            "base_ok": bool(base_ok),
            "attachment_ok": bool(attachment_ok),
            "corridor_ok": bool(corridor_ok),
            "accepted": bool(accepted),
            "kind": kind,
            "reason": reason,
        })

    if wire_tree is not None:
        del wire_tree
    if raw_wire_tree is not None:
        del raw_wire_tree

    pole_active = validated_pole & active & (~protected)
    rejected_pole |= pole_in & (~validated_pole)

    rejected_count = int(np.count_nonzero(rejected_pole))
    accepted_count = int(np.count_nonzero(validated_pole))
    if accepted_count and rejected_count:
        status = "PASS_WITH_FALSE_POSITIVE_REJECTIONS"
    elif accepted_count:
        status = "PASS"
    elif np.any(pole_in):
        status = "ALL_POLE_SUPPORT_REJECTED"
    else:
        status = "NO_POLE_SUPPORT_TO_VALIDATE"

    return PowerPhase7Validation(
        version=PHASE7_VERSION,
        point_count=n,
        status=status,
        input_wire_mask=_freeze(wire_in.copy()),
        input_pole_context_mask=_freeze(pole_in.copy()),
        validated_wire_mask=_freeze(wire_valid.copy()),
        validated_pole_context_mask=_freeze(validated_pole.copy()),
        validated_pole_active_mask=_freeze(pole_active.copy()),
        rejected_pole_context_mask=_freeze(rejected_pole.copy()),
        diagnostics=tuple(diagnostics),
        cl_active=bool(phase2.cl_active),
        exact_fence_only=True,
    )