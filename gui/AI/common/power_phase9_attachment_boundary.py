from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
from scipy.spatial import cKDTree

from .power_phase1_state import PowerPhase1State
from .power_phase3_tracks import PowerPhase3Tracks
from .power_phase8_consistency import PowerPhase8Consistency


PHASE9_VERSION = "NAKSHA_POWER_PHASE9_ATTACHMENT_BOUNDARY_V3_15"


def _freeze(arr: np.ndarray) -> np.ndarray:
    arr.setflags(write=False)
    return arr


def _safe01(arr) -> np.ndarray:
    a = np.asarray(arr, dtype=np.float32).reshape(-1)
    return np.clip(np.nan_to_num(a, nan=0.0, posinf=1.0, neginf=0.0), 0.0, 1.0)


def _robust_span(v: np.ndarray, lo: float = 20.0, hi: float = 80.0) -> float:
    v = np.asarray(v, dtype=np.float64)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return 0.0
    if v.size < 5:
        return float(np.ptp(v))
    return float(np.percentile(v, hi) - np.percentile(v, lo))


def _groups_by_xy(indices: np.ndarray, xyz: np.ndarray, radius_m: float) -> Tuple[np.ndarray, ...]:
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


@dataclass(frozen=True)
class PowerPhase9AttachmentBoundary:
    """Resolve the physical Wire/Pole transition at conductor attachments.

    Phases 3/6 have already fitted the conductor tracks and Phases 5/7/8 have
    already validated the support.  Phase 9 therefore does *not* detect a new
    asset.  It only resolves points that are simultaneously confirmed as Wire
    and Pole/Pylon.

    The old Phase-4 rule gave Pole/Pylon precedence across the entire overlap.
    On real lattice towers that made several metres of genuine conductor near
    the crossarm turn into Pole.  Phase 9 keeps Pole precedence only inside a
    compact attachment station zone around the structural support core and
    returns the remaining fitted-conductor overlap to Wire.
    """

    version: str
    point_count: int
    status: str
    input_wire_mask: np.ndarray
    input_pole_context_mask: np.ndarray
    overlap_mask: np.ndarray
    reclaimed_wire_mask: np.ndarray
    retained_attachment_overlap_mask: np.ndarray
    final_wire_mask: np.ndarray
    final_pole_context_mask: np.ndarray
    final_pole_active_mask: np.ndarray
    diagnostics: Tuple[dict, ...]
    exact_fence_only: bool
    mode_independent: bool

    def report(self) -> dict:
        return {
            "version": self.version,
            "status": self.status,
            "input_wire_points": int(np.count_nonzero(self.input_wire_mask)),
            "input_pole_context_points": int(np.count_nonzero(self.input_pole_context_mask)),
            "input_overlap_points": int(np.count_nonzero(self.overlap_mask)),
            "wire_reclaimed_from_pole": int(np.count_nonzero(self.reclaimed_wire_mask)),
            "attachment_overlap_retained": int(np.count_nonzero(self.retained_attachment_overlap_mask)),
            "final_wire_points": int(np.count_nonzero(self.final_wire_mask)),
            "final_pole_context_points": int(np.count_nonzero(self.final_pole_context_mask)),
            "final_pole_active_points": int(np.count_nonzero(self.final_pole_active_mask)),
            "support_components": int(len(self.diagnostics)),
            "attachment_boundary_resolved": True,
            "creates_new_assets": False,
            "semantic_class_gate": False,
            "mode_independent": bool(self.mode_independent),
            "exact_fence_only": bool(self.exact_fence_only),
            "immutable": bool(
                not self.final_wire_mask.flags.writeable
                and not self.final_pole_context_mask.flags.writeable
                and not self.final_pole_active_mask.flags.writeable
                and not self.reclaimed_wire_mask.flags.writeable
                and not self.retained_attachment_overlap_mask.flags.writeable
            ),
            "support_reports": [dict(d) for d in self.diagnostics],
        }


def build_power_phase9_attachment_boundary(
    xyz,
    phase1: PowerPhase1State,
    effective_phase3: PowerPhase3Tracks,
    phase8: PowerPhase8Consistency,
    *,
    linearity_05,
    verticality_05,
    linearity_10,
    verticality_10,
    cl_station: Optional[np.ndarray] = None,
    config=None,
) -> PowerPhase9AttachmentBoundary:
    """Resolve Wire-vs-Pole overlap without changing any non-overlap asset.

    Safety contract
    ---------------
    * no new Wire or Pole points are created;
    * only points already in ``Wire AND Pole`` may change semantic ownership;
    * the exact Phase-1 writable scope is re-applied defensively;
    * Pole/Pylon remains authoritative inside a compact attachment/core zone;
    * fitted conductor overlap outside that zone is reclaimed as Wire;
    * if station geometry is unavailable, Phase 9 fails conservatively and
      leaves the Phase-8 masks unchanged.
    """
    cfg = dict(config or {})
    xyz = np.asarray(xyz, dtype=np.float64)
    n = int(phase1.point_count)
    if xyz.ndim != 2 or xyz.shape[1] < 3 or len(xyz) != n:
        raise ValueError("Phase9 xyz length mismatch")
    if effective_phase3.point_count != n or phase8.point_count != n:
        raise ValueError("Phase9 prior-phase length mismatch")

    active = np.asarray(phase1.active_mask, dtype=bool)
    protected = np.asarray(phase1.protected_mask, dtype=bool)
    wire_in = np.asarray(phase8.final_wire_mask, dtype=bool).copy()
    pole_in = np.asarray(phase8.final_pole_context_mask, dtype=bool).copy()

    wire = wire_in & active & (~protected)
    pole = pole_in & (~protected)
    overlap = wire & pole
    reclaimed = np.zeros(n, dtype=bool)
    retained = np.zeros(n, dtype=bool)
    diagnostics = []

    if not np.any(overlap):
        status = "PASS_NO_OVERLAP"
    elif cl_station is None or len(effective_phase3.tracks) == 0:
        # No reliable conductor station frame: never guess at the attachment.
        retained = overlap.copy()
        status = "PASS_CONSERVATIVE_NO_TRACK_STATION"
    else:
        station = np.asarray(cl_station, dtype=np.float64).reshape(-1)
        if len(station) != n:
            raise ValueError("Phase9 cl_station length mismatch")

        l05 = _safe01(linearity_05)
        v05 = _safe01(verticality_05)
        l10 = _safe01(linearity_10)
        v10 = _safe01(verticality_10)
        if any(len(a) != n for a in (l05, v05, l10, v10)):
            raise ValueError("Phase9 geometry length mismatch")
        line_strength = np.maximum(l05, l10)
        vert_strength = np.maximum(v05, v10)

        groups = _groups_by_xy(
            np.flatnonzero(pole),
            xyz,
            float(cfg.get("phase9_support_component_xy_link_m", 2.25)),
        )
        min_half = float(cfg.get("phase9_attachment_min_halfwidth_m", 0.45))
        max_half = float(cfg.get("phase9_attachment_max_halfwidth_m", 0.95))
        margin = float(cfg.get("phase9_attachment_station_margin_m", 0.20))
        seed_vert = float(cfg.get("phase9_core_verticality_min", 0.46))
        seed_line = float(cfg.get("phase9_core_linearity_min", 0.54))
        seed_vert_soft = float(cfg.get("phase9_core_verticality_soft_min", 0.28))

        for gid, gi in enumerate(groups):
            gi = np.asarray(gi, dtype=np.int64)
            if gi.size == 0:
                continue
            oi = gi[overlap[gi]]
            if oi.size == 0:
                continue

            pole_only = gi[~wire[gi]]
            finite_pole_only = pole_only[np.isfinite(station[pole_only])]
            structural_seed = finite_pole_only[
                (vert_strength[finite_pole_only] >= seed_vert)
                | (
                    (vert_strength[finite_pole_only] >= seed_vert_soft)
                    & (line_strength[finite_pole_only] >= seed_line)
                )
            ]
            if structural_seed.size < int(cfg.get("phase9_min_structural_seed_points", 20)):
                structural_seed = finite_pole_only
            if structural_seed.size == 0:
                structural_seed = gi[np.isfinite(station[gi]) & (~overlap[gi])]

            if structural_seed.size == 0:
                # Cannot locate the support core safely; keep old precedence.
                retained[oi] = True
                diagnostics.append({
                    "component": int(gid),
                    "input_overlap": int(oi.size),
                    "reclaimed_wire": 0,
                    "retained_attachment": int(oi.size),
                    "core_station": float("nan"),
                    "core_station_span": 0.0,
                    "attachment_halfwidth": 0.0,
                    "reason": "no_structural_station_seed_conservative",
                })
                continue

            core_station = float(np.median(station[structural_seed]))
            core_span = _robust_span(station[structural_seed], 20.0, 80.0)
            halfwidth = float(np.clip(0.5 * core_span + margin, min_half, max_half))

            finite_overlap = oi[np.isfinite(station[oi])]
            nonfinite_overlap = oi[~np.isfinite(station[oi])]
            if nonfinite_overlap.size:
                retained[nonfinite_overlap] = True

            if finite_overlap.size:
                ds = np.abs(station[finite_overlap] - core_station)
                keep_pole = ds <= halfwidth
                retain_idx = finite_overlap[keep_pole]
                reclaim_idx = finite_overlap[~keep_pole]
                retained[retain_idx] = True
                reclaimed[reclaim_idx] = True

            diagnostics.append({
                "component": int(gid),
                "input_overlap": int(oi.size),
                "reclaimed_wire": int(np.count_nonzero(reclaimed[oi])),
                "retained_attachment": int(np.count_nonzero(retained[oi])),
                "core_station": core_station,
                "core_station_span": float(core_span),
                "attachment_halfwidth": float(halfwidth),
                "structural_seed_points": int(structural_seed.size),
                "reason": "compact_attachment_boundary",
            })

        # Any overlap not associated with a support group is retained
        # conservatively.  In the normal pipeline every Pole point belongs to a
        # component, but this protects future refactors from silently converting
        # an unmodelled structural intersection into Wire.
        unresolved = overlap & (~reclaimed) & (~retained)
        if np.any(unresolved):
            retained[unresolved] = True

        # The only semantic ownership change in Phase 9: already-confirmed Wire
        # points outside the compact attachment zone stop being Pole/Pylon.
        pole[reclaimed] = False
        status = "RESOLVED" if np.any(reclaimed) else "PASS_ATTACHMENT_ONLY_OVERLAP"

    # Wire never expands: it remains the Phase-8 exact-writable Wire mask.
    wire_final = wire.copy()
    pole_active = pole & active & (~protected)
    exact = not bool(np.any(wire_final & (~active)) or np.any(pole_active & (~active)))

    return PowerPhase9AttachmentBoundary(
        version=PHASE9_VERSION,
        point_count=n,
        status=status,
        input_wire_mask=_freeze(wire_in.copy()),
        input_pole_context_mask=_freeze(pole_in.copy()),
        overlap_mask=_freeze(overlap.copy()),
        reclaimed_wire_mask=_freeze(reclaimed.copy()),
        retained_attachment_overlap_mask=_freeze(retained.copy()),
        final_wire_mask=_freeze(wire_final.copy()),
        final_pole_context_mask=_freeze(pole.copy()),
        final_pole_active_mask=_freeze(pole_active.copy()),
        diagnostics=tuple(diagnostics),
        exact_fence_only=bool(exact),
        mode_independent=True,
    )
