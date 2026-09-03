# from __future__ import annotations

# from dataclasses import dataclass
# from typing import Optional, Tuple

# import numpy as np

# from .power_phase1_state import PowerPhase1State
# from .power_phase2_candidates import PowerPhase2Candidates
# from .power_phase3_tracks import ConductorTrack, PowerPhase3Tracks, build_power_phase3_tracks


# PHASE6_VERSION = "NAKSHA_POWER_PHASE6_CROSS_MODE_RECOVERY_V3_12"


# def _freeze(arr: np.ndarray) -> np.ndarray:
#     arr.setflags(write=False)
#     return arr


# def _track_value(track: ConductorTrack, station: float) -> Tuple[float, float]:
#     x = float(station) - float(track.station_center)
#     lat = float(track.lateral_slope) * x + float(track.lateral_intercept)
#     z = (
#         float(track.z_quadratic) * x * x
#         + float(track.z_slope) * x
#         + float(track.z_intercept)
#     )
#     return lat, z


# def _pair_overlap(a: ConductorTrack, b: ConductorTrack) -> float:
#     return max(0.0, min(float(a.station_max), float(b.station_max)) - max(float(a.station_min), float(b.station_min)))


# def _bundle_groups(tracks: Tuple[ConductorTrack, ...], cfg: dict):
#     """Find mutually compatible parallel conductor bundles.

#     Phase 6 is only allowed to rescue a failed Phase-3 run when there is
#     *bundle* evidence.  A single smooth tree branch or hedge line is not enough.
#     Tracks must overlap in station and remain within a realistic cross-section
#     separation envelope while still being physically distinct.
#     """
#     n = len(tracks)
#     if n == 0:
#         return [], []

#     min_overlap = float(cfg.get("phase6_bundle_min_pair_overlap_m", 3.5))
#     min_sep = float(cfg.get("phase6_bundle_min_track_separation_m", 0.25))
#     max_sep = float(cfg.get("phase6_bundle_max_track_separation_m", 9.0))

#     parent = np.arange(n, dtype=np.int32)

#     def find(a: int) -> int:
#         while parent[a] != a:
#             parent[a] = parent[parent[a]]
#             a = int(parent[a])
#         return a

#     def union(a: int, b: int) -> None:
#         ra, rb = find(a), find(b)
#         if ra != rb:
#             parent[rb] = ra

#     pair_diag = []
#     for i in range(n):
#         for j in range(i + 1, n):
#             a, b = tracks[i], tracks[j]
#             overlap = _pair_overlap(a, b)
#             if overlap < min_overlap:
#                 continue
#             s = 0.5 * (
#                 max(float(a.station_min), float(b.station_min))
#                 + min(float(a.station_max), float(b.station_max))
#             )
#             alat, az = _track_value(a, s)
#             blat, bz = _track_value(b, s)
#             sep = float(np.hypot(alat - blat, az - bz))
#             distinct = sep >= min_sep
#             compatible = distinct and sep <= max_sep
#             pair_diag.append({
#                 "i": int(i), "j": int(j), "overlap_m": overlap,
#                 "separation_m": sep, "compatible": bool(compatible),
#             })
#             if compatible:
#                 union(i, j)

#     groups = {}
#     for i in range(n):
#         groups.setdefault(find(i), []).append(i)
#     return list(groups.values()), pair_diag


# @dataclass(frozen=True)
# class PowerPhase6Recovery:
#     version: str
#     point_count: int
#     status: str
#     activated: bool
#     normal_path_success: bool
#     fallback_attempted: bool
#     fallback_accepted: bool
#     original_phase3: PowerPhase3Tracks
#     fallback_phase3: PowerPhase3Tracks
#     effective_phase3: PowerPhase3Tracks
#     bundle_track_ids: Tuple[int, ...]
#     bundle_pair_count: int
#     bundle_common_span_m: float
#     diagnostics: Tuple[dict, ...]

#     def report(self) -> dict:
#         return {
#             "version": self.version,
#             "status": self.status,
#             "activated": bool(self.activated),
#             "normal_path_success": bool(self.normal_path_success),
#             "fallback_attempted": bool(self.fallback_attempted),
#             "fallback_accepted": bool(self.fallback_accepted),
#             "original_tracks": int(len(self.original_phase3.tracks)),
#             "fallback_tracks": int(len(self.fallback_phase3.tracks)),
#             "effective_tracks": int(len(self.effective_phase3.tracks)),
#             "bundle_tracks": int(len(self.bundle_track_ids)),
#             "bundle_track_ids": [int(x) for x in self.bundle_track_ids],
#             "bundle_pair_count": int(self.bundle_pair_count),
#             "bundle_common_span_m": float(self.bundle_common_span_m),
#             "recovered_context_wire_points": int(np.count_nonzero(self.effective_phase3.confirmed_context_mask)) if self.fallback_accepted else 0,
#             "recovered_active_wire_points": int(np.count_nonzero(self.effective_phase3.projected_active_mask)) if self.fallback_accepted else 0,
#             "exact_fence_projection": bool(self.effective_phase3.exact_fence_projection),
#             "semantic_gate": False,
#             "classification_writes_in_module": False,
#             "immutable": bool(
#                 not self.effective_phase3.confirmed_context_mask.flags.writeable
#                 and not self.effective_phase3.projected_active_mask.flags.writeable
#                 and not self.effective_phase3.wire_support_mask.flags.writeable
#             ),
#             "diagnostics": [dict(x) for x in self.diagnostics],
#         }


# def _empty_phase3_like(phase3: PowerPhase3Tracks, status: str) -> PowerPhase3Tracks:
#     n = int(phase3.point_count)
#     z = np.zeros(n, dtype=bool)
#     return PowerPhase3Tracks(
#         version=phase3.version,
#         point_count=n,
#         status=str(status),
#         confirmed_context_mask=_freeze(z.copy()),
#         projected_active_mask=_freeze(z.copy()),
#         projection_rescue_mask=_freeze(z.copy()),
#         wire_support_mask=_freeze(z.copy()),
#         tracks=tuple(),
#         semantic_gate=False,
#         exact_fence_projection=True,
#     )


# def build_power_phase6_recovery(
#     xyz,
#     hag,
#     phase1: PowerPhase1State,
#     phase2: PowerPhase2Candidates,
#     phase3: PowerPhase3Tracks,
#     *,
#     cl_coords,
#     cl_width: float,
#     linearity_05,
#     planarity_05,
#     verticality_05,
#     linearity_10,
#     verticality_10,
#     cl_station: Optional[np.ndarray] = None,
#     cl_dist: Optional[np.ndarray] = None,
#     config=None,
# ) -> PowerPhase6Recovery:
#     """Cross-mode recovery when normal Phase 3 accepts no conductor tracks.

#     The successful Phase-3 path is never changed.  Only when Phase 3 has no
#     usable tracks does Phase 6 retry the same semantic-independent raw Phase-2
#     cloud with a short-span/sparse-data profile.  The retry is accepted only
#     when multiple compatible parallel conductor tracks form a physical bundle.
#     This prevents the fallback from turning an isolated tree branch into Wire.
#     """
#     cfg = dict(config or {})
#     n = int(phase1.point_count)
#     if phase2.point_count != n or phase3.point_count != n:
#         raise ValueError("Phase6 Phase1/Phase2/Phase3 length mismatch")

#     normal_ok = bool(
#         len(phase3.tracks) > 0
#         and np.count_nonzero(np.asarray(phase3.projected_active_mask, dtype=bool)) > 0
#     )
#     if normal_ok:
#         return PowerPhase6Recovery(
#             version=PHASE6_VERSION,
#             point_count=n,
#             status="NOT_REQUIRED_NORMAL_PATH_SUCCESS",
#             activated=False,
#             normal_path_success=True,
#             fallback_attempted=False,
#             fallback_accepted=False,
#             original_phase3=phase3,
#             fallback_phase3=_empty_phase3_like(phase3, "NOT_ATTEMPTED"),
#             effective_phase3=phase3,
#             bundle_track_ids=tuple(),
#             bundle_pair_count=0,
#             bundle_common_span_m=0.0,
#             diagnostics=tuple(),
#         )

#     if cl_coords is None or float(cl_width or 0.0) <= 0.0:
#         empty = _empty_phase3_like(phase3, "SKIPPED_NO_CL")
#         return PowerPhase6Recovery(
#             version=PHASE6_VERSION,
#             point_count=n,
#             status="SKIPPED_NO_CL",
#             activated=False,
#             normal_path_success=False,
#             fallback_attempted=False,
#             fallback_accepted=False,
#             original_phase3=phase3,
#             fallback_phase3=empty,
#             effective_phase3=phase3,
#             bundle_track_ids=tuple(),
#             bundle_pair_count=0,
#             bundle_common_span_m=0.0,
#             diagnostics=tuple(),
#         )

#     raw_wire_count = int(np.count_nonzero(np.asarray(phase2.raw_wire_mask, dtype=bool)))
#     if raw_wire_count < int(cfg.get("phase6_min_raw_wire_candidates", 30)):
#         empty = _empty_phase3_like(phase3, "SKIPPED_TOO_FEW_RAW_WIRE_CANDIDATES")
#         return PowerPhase6Recovery(
#             version=PHASE6_VERSION,
#             point_count=n,
#             status="SKIPPED_TOO_FEW_RAW_WIRE_CANDIDATES",
#             activated=False,
#             normal_path_success=False,
#             fallback_attempted=False,
#             fallback_accepted=False,
#             original_phase3=phase3,
#             fallback_phase3=empty,
#             effective_phase3=phase3,
#             bundle_track_ids=tuple(),
#             bundle_pair_count=0,
#             bundle_common_span_m=0.0,
#             diagnostics=tuple(),
#         )

#     # Explicit fallback values intentionally override any strict Phase-3
#     # settings supplied by a model-specific path. This is the cross-mode part:
#     # Advanced and Premium feed the same Phase-2 physical evidence into one
#     # common sparse/short-span conductor recovery profile.
#     fcfg = dict(cfg)
#     overrides = {
#         "phase3_seed_score_min": float(cfg.get("phase6_seed_score_min", 0.24)),
#         "phase3_min_raw_candidates": int(cfg.get("phase6_min_seed_candidates", 16)),
#         "phase3_min_nodes": int(cfg.get("phase6_min_nodes", 4)),
#         "phase3_min_span_m": float(cfg.get("phase6_min_span_m", 5.0)),
#         "phase3_min_bin_coverage": float(cfg.get("phase6_min_bin_coverage", 0.18)),
#         "phase3_min_median_score": float(cfg.get("phase6_min_median_score", 0.42)),
#         "phase3_node_lateral_radius_m": float(cfg.get("phase6_node_lateral_radius_m", 0.62)),
#         "phase3_node_z_radius_m": float(cfg.get("phase6_node_z_radius_m", 0.62)),
#         "phase3_node_max_lateral_span_m": float(cfg.get("phase6_node_max_lateral_span_m", 1.05)),
#         "phase3_node_max_z_span_m": float(cfg.get("phase6_node_max_z_span_m", 1.05)),
#         "phase3_link_max_station_gap_m": float(cfg.get("phase6_link_max_station_gap_m", 6.5)),
#         "phase3_link_lateral_tolerance_m": float(cfg.get("phase6_link_lateral_tolerance_m", 1.05)),
#         "phase3_link_z_tolerance_m": float(cfg.get("phase6_link_z_tolerance_m", 1.65)),
#         "phase3_max_lateral_residual_p90": float(cfg.get("phase6_max_lateral_residual_p90", 0.78)),
#         "phase3_max_z_residual_p90": float(cfg.get("phase6_max_z_residual_p90", 1.10)),
#         "phase3_max_lateral_slope": float(cfg.get("phase6_max_lateral_slope", 0.25)),
#         "phase3_max_abs_quadratic": float(cfg.get("phase6_max_abs_quadratic", 0.060)),
#         "phase3_min_candidate_points": int(cfg.get("phase6_min_candidate_points", 10)),
#         "phase3_context_tube_lateral_m": float(cfg.get("phase6_context_tube_lateral_m", 0.60)),
#         "phase3_context_tube_z_m": float(cfg.get("phase6_context_tube_z_m", 0.60)),
#         "phase3_projection_lateral_m": float(cfg.get("phase6_projection_lateral_m", 0.48)),
#         "phase3_projection_z_m": float(cfg.get("phase6_projection_z_m", 0.48)),
#         "phase3_projection_station_extension_m": float(cfg.get("phase6_projection_station_extension_m", 3.0)),
#     }
#     fcfg.update(overrides)

#     fallback = build_power_phase3_tracks(
#         xyz,
#         hag,
#         phase1,
#         phase2,
#         cl_coords=cl_coords,
#         cl_width=float(cl_width),
#         linearity_05=linearity_05,
#         planarity_05=planarity_05,
#         verticality_05=verticality_05,
#         linearity_10=linearity_10,
#         verticality_10=verticality_10,
#         cl_station=cl_station,
#         cl_dist=cl_dist,
#         config=fcfg,
#     )

#     diagnostics = [{
#         "raw_wire_candidates": raw_wire_count,
#         "normal_phase3_status": str(phase3.status),
#         "fallback_phase3_status": str(fallback.status),
#         "fallback_tracks": int(len(fallback.tracks)),
#     }]

#     groups, pair_diag = _bundle_groups(tuple(fallback.tracks), cfg)
#     min_tracks = int(cfg.get("phase6_bundle_min_tracks", 2))
#     max_tracks = int(cfg.get("phase6_bundle_max_tracks", 12))
#     min_common_span = float(cfg.get("phase6_bundle_min_common_span_m", 3.5))

#     best = []
#     best_span = 0.0
#     for g in groups:
#         if not (min_tracks <= len(g) <= max_tracks):
#             continue
#         ts = [fallback.tracks[i] for i in g]
#         common_min = max(float(t.station_min) for t in ts)
#         common_max = min(float(t.station_max) for t in ts)
#         common_span = max(0.0, common_max - common_min)
#         # If every track does not share one interval, allow a strong connected
#         # bundle when median span is substantial; this covers partial wires at
#         # a fence edge while still requiring at least two compatible pairs.
#         pair_count = sum(1 for p in pair_diag if p["compatible"] and p["i"] in g and p["j"] in g)
#         median_span = float(np.median([float(t.span) for t in ts]))
#         effective_span = max(common_span, min(median_span, float(cfg.get("phase6_bundle_span_cap_m", 30.0))))
#         if effective_span < min_common_span:
#             continue
#         if pair_count < max(1, len(g) - 1):
#             continue
#         score = (len(g), effective_span, pair_count)
#         if not best or score > (len(best), best_span, 0):
#             best = list(g)
#             best_span = effective_span

#     if not best:
#         diagnostics.append({
#             "bundle_reject": True,
#             "groups": [list(map(int, g)) for g in groups],
#             "compatible_pairs": int(sum(1 for p in pair_diag if p["compatible"])),
#         })
#         return PowerPhase6Recovery(
#             version=PHASE6_VERSION,
#             point_count=n,
#             status="FALLBACK_REJECTED_NO_PARALLEL_BUNDLE",
#             activated=True,
#             normal_path_success=False,
#             fallback_attempted=True,
#             fallback_accepted=False,
#             original_phase3=phase3,
#             fallback_phase3=fallback,
#             effective_phase3=phase3,
#             bundle_track_ids=tuple(),
#             bundle_pair_count=0,
#             bundle_common_span_m=0.0,
#             diagnostics=tuple(diagnostics),
#         )

#     pair_count = sum(1 for p in pair_diag if p["compatible"] and p["i"] in best and p["j"] in best)
#     # The fallback masks are accepted as one physical bundle. Phase 3's own
#     # thin-track geometry and Phase-2 raw mask remain the point-level guard;
#     # Phase 6 merely decides whether the fallback is trustworthy enough to use.
#     diagnostics.append({
#         "bundle_accept": True,
#         "track_ids": [int(x) for x in best],
#         "bundle_tracks": int(len(best)),
#         "compatible_pairs": int(pair_count),
#         "effective_span_m": float(best_span),
#         "projected_active_points": int(np.count_nonzero(fallback.projected_active_mask)),
#         "confirmed_context_points": int(np.count_nonzero(fallback.confirmed_context_mask)),
#     })

#     if np.count_nonzero(np.asarray(fallback.projected_active_mask, dtype=bool)) == 0:
#         return PowerPhase6Recovery(
#             version=PHASE6_VERSION,
#             point_count=n,
#             status="FALLBACK_REJECTED_ZERO_ACTIVE_PROJECTION",
#             activated=True,
#             normal_path_success=False,
#             fallback_attempted=True,
#             fallback_accepted=False,
#             original_phase3=phase3,
#             fallback_phase3=fallback,
#             effective_phase3=phase3,
#             bundle_track_ids=tuple(int(x) for x in best),
#             bundle_pair_count=int(pair_count),
#             bundle_common_span_m=float(best_span),
#             diagnostics=tuple(diagnostics),
#         )

#     return PowerPhase6Recovery(
#         version=PHASE6_VERSION,
#         point_count=n,
#         status="RECOVERY_APPLIED",
#         activated=True,
#         normal_path_success=False,
#         fallback_attempted=True,
#         fallback_accepted=True,
#         original_phase3=phase3,
#         fallback_phase3=fallback,
#         effective_phase3=fallback,
#         bundle_track_ids=tuple(int(x) for x in best),
#         bundle_pair_count=int(pair_count),
#         bundle_common_span_m=float(best_span),
#         diagnostics=tuple(diagnostics),
#     )
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

from .power_phase1_state import PowerPhase1State
from .power_phase2_candidates import PowerPhase2Candidates
from .power_phase3_tracks import ConductorTrack, PowerPhase3Tracks, build_power_phase3_tracks


PHASE6_VERSION = "NAKSHA_POWER_PHASE6_CROSS_MODE_RECOVERY_V3_26_SCALE_GUARD"


def _freeze(arr: np.ndarray) -> np.ndarray:
    arr.setflags(write=False)
    return arr


def _track_value(track: ConductorTrack, station: float) -> Tuple[float, float]:
    x = float(station) - float(track.station_center)
    lat = float(track.lateral_slope) * x + float(track.lateral_intercept)
    z = (
        float(track.z_quadratic) * x * x
        + float(track.z_slope) * x
        + float(track.z_intercept)
    )
    return lat, z


def _pair_overlap(a: ConductorTrack, b: ConductorTrack) -> float:
    return max(0.0, min(float(a.station_max), float(b.station_max)) - max(float(a.station_min), float(b.station_min)))


def _bundle_groups(tracks: Tuple[ConductorTrack, ...], cfg: dict):
    """Find mutually compatible parallel conductor bundles.

    Phase 6 is only allowed to rescue a failed Phase-3 run when there is
    *bundle* evidence.  A single smooth tree branch or hedge line is not enough.
    Tracks must overlap in station and remain within a realistic cross-section
    separation envelope while still being physically distinct.
    """
    n = len(tracks)
    if n == 0:
        return [], []

    min_overlap = float(cfg.get("phase6_bundle_min_pair_overlap_m", 3.5))
    min_sep = float(cfg.get("phase6_bundle_min_track_separation_m", 0.25))
    max_sep = float(cfg.get("phase6_bundle_max_track_separation_m", 9.0))

    parent = np.arange(n, dtype=np.int32)

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = int(parent[a])
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    pair_diag = []
    for i in range(n):
        for j in range(i + 1, n):
            a, b = tracks[i], tracks[j]
            overlap = _pair_overlap(a, b)
            if overlap < min_overlap:
                continue
            s = 0.5 * (
                max(float(a.station_min), float(b.station_min))
                + min(float(a.station_max), float(b.station_max))
            )
            alat, az = _track_value(a, s)
            blat, bz = _track_value(b, s)
            sep = float(np.hypot(alat - blat, az - bz))
            distinct = sep >= min_sep
            compatible = distinct and sep <= max_sep
            pair_diag.append({
                "i": int(i), "j": int(j), "overlap_m": overlap,
                "separation_m": sep, "compatible": bool(compatible),
            })
            if compatible:
                union(i, j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values()), pair_diag


@dataclass(frozen=True)
class PowerPhase6Recovery:
    version: str
    point_count: int
    status: str
    activated: bool
    normal_path_success: bool
    fallback_attempted: bool
    fallback_accepted: bool
    original_phase3: PowerPhase3Tracks
    fallback_phase3: PowerPhase3Tracks
    effective_phase3: PowerPhase3Tracks
    bundle_track_ids: Tuple[int, ...]
    bundle_pair_count: int
    bundle_common_span_m: float
    diagnostics: Tuple[dict, ...]

    def report(self) -> dict:
        return {
            "version": self.version,
            "status": self.status,
            "activated": bool(self.activated),
            "normal_path_success": bool(self.normal_path_success),
            "fallback_attempted": bool(self.fallback_attempted),
            "fallback_accepted": bool(self.fallback_accepted),
            "original_tracks": int(len(self.original_phase3.tracks)),
            "fallback_tracks": int(len(self.fallback_phase3.tracks)),
            "effective_tracks": int(len(self.effective_phase3.tracks)),
            "bundle_tracks": int(len(self.bundle_track_ids)),
            "bundle_track_ids": [int(x) for x in self.bundle_track_ids],
            "bundle_pair_count": int(self.bundle_pair_count),
            "bundle_common_span_m": float(self.bundle_common_span_m),
            "recovered_context_wire_points": int(np.count_nonzero(self.effective_phase3.confirmed_context_mask)) if self.fallback_accepted else 0,
            "recovered_active_wire_points": int(np.count_nonzero(self.effective_phase3.projected_active_mask)) if self.fallback_accepted else 0,
            "exact_fence_projection": bool(self.effective_phase3.exact_fence_projection),
            "semantic_gate": False,
            "classification_writes_in_module": False,
            "immutable": bool(
                not self.effective_phase3.confirmed_context_mask.flags.writeable
                and not self.effective_phase3.projected_active_mask.flags.writeable
                and not self.effective_phase3.wire_support_mask.flags.writeable
            ),
            "diagnostics": [dict(x) for x in self.diagnostics],
        }


def _empty_phase3_like(phase3: PowerPhase3Tracks, status: str) -> PowerPhase3Tracks:
    n = int(phase3.point_count)
    z = np.zeros(n, dtype=bool)
    return PowerPhase3Tracks(
        version=phase3.version,
        point_count=n,
        status=str(status),
        confirmed_context_mask=_freeze(z.copy()),
        projected_active_mask=_freeze(z.copy()),
        projection_rescue_mask=_freeze(z.copy()),
        wire_support_mask=_freeze(z.copy()),
        tracks=tuple(),
        semantic_gate=False,
        exact_fence_projection=True,
    )


def build_power_phase6_recovery(
    xyz,
    hag,
    phase1: PowerPhase1State,
    phase2: PowerPhase2Candidates,
    phase3: PowerPhase3Tracks,
    *,
    cl_coords,
    cl_width: float,
    linearity_05,
    planarity_05,
    verticality_05,
    linearity_10,
    verticality_10,
    cl_station: Optional[np.ndarray] = None,
    cl_dist: Optional[np.ndarray] = None,
    config=None,
) -> PowerPhase6Recovery:
    """Cross-mode recovery when normal Phase 3 accepts no conductor tracks.

    The successful Phase-3 path is never changed.  Only when Phase 3 has no
    usable tracks does Phase 6 retry the same semantic-independent raw Phase-2
    cloud with a short-span/sparse-data profile.  The retry is accepted only
    when multiple compatible parallel conductor tracks form a physical bundle.
    This prevents the fallback from turning an isolated tree branch into Wire.
    """
    cfg = dict(config or {})
    n = int(phase1.point_count)
    if phase2.point_count != n or phase3.point_count != n:
        raise ValueError("Phase6 Phase1/Phase2/Phase3 length mismatch")

    normal_has_output = bool(
        len(phase3.tracks) > 0
        and np.count_nonzero(np.asarray(phase3.projected_active_mask, dtype=bool)) > 0
    )

    # A single tiny accepted fragment must not mark a long selected corridor as
    # "normal success".  This was the exact long-fence regression: ~50k raw Wire
    # candidates across the selected CL, but one ~11 m track disabled recovery.
    # Measure union coverage only when the active raw-candidate station span is
    # large enough; short fences keep the original behaviour.
    station_coverage = 1.0
    candidate_station_span = 0.0
    covered_station_span = 0.0
    incomplete_long_selection = False
    if normal_has_output and cl_station is not None:
        st = np.asarray(cl_station, dtype=np.float64).reshape(-1)
        if len(st) == n:
            active = np.asarray(phase1.active_mask, dtype=bool)
            raw = np.asarray(phase2.raw_wire_mask, dtype=bool)
            ii = np.flatnonzero(active & raw & np.isfinite(st))
            if ii.size >= 20:
                sv = st[ii]
                lo, hi = np.percentile(sv, [2.0, 98.0])
                candidate_station_span = float(max(0.0, hi - lo))
                intervals = []
                for t in phase3.tracks:
                    a = max(float(lo), float(t.station_min))
                    b = min(float(hi), float(t.station_max))
                    if b > a:
                        intervals.append((a, b))
                intervals.sort()
                merged = []
                for a, b in intervals:
                    if not merged or a > merged[-1][1]:
                        merged.append([a, b])
                    else:
                        merged[-1][1] = max(merged[-1][1], b)
                covered_station_span = float(sum(b - a for a, b in merged))
                station_coverage = covered_station_span / max(candidate_station_span, 1e-6)
                trigger_span = float(cfg.get("phase6_incomplete_span_trigger_m", 30.0))
                min_cov = float(cfg.get("phase6_normal_min_station_coverage", 0.35))
                incomplete_long_selection = bool(
                    candidate_station_span >= trigger_span and station_coverage < min_cov
                )

    normal_ok = bool(normal_has_output and not incomplete_long_selection)
    if normal_ok:
        return PowerPhase6Recovery(
            version=PHASE6_VERSION,
            point_count=n,
            status="NOT_REQUIRED_NORMAL_PATH_SUCCESS",
            activated=False,
            normal_path_success=True,
            fallback_attempted=False,
            fallback_accepted=False,
            original_phase3=phase3,
            fallback_phase3=_empty_phase3_like(phase3, "NOT_ATTEMPTED"),
            effective_phase3=phase3,
            bundle_track_ids=tuple(),
            bundle_pair_count=0,
            bundle_common_span_m=0.0,
            diagnostics=({
                "candidate_station_span_m": float(candidate_station_span),
                "covered_station_span_m": float(covered_station_span),
                "normal_station_coverage": float(station_coverage),
                "incomplete_long_selection": False,
            },),
        )

    if cl_coords is None or float(cl_width or 0.0) <= 0.0:
        empty = _empty_phase3_like(phase3, "SKIPPED_NO_CL")
        return PowerPhase6Recovery(
            version=PHASE6_VERSION,
            point_count=n,
            status="SKIPPED_NO_CL",
            activated=False,
            normal_path_success=False,
            fallback_attempted=False,
            fallback_accepted=False,
            original_phase3=phase3,
            fallback_phase3=empty,
            effective_phase3=phase3,
            bundle_track_ids=tuple(),
            bundle_pair_count=0,
            bundle_common_span_m=0.0,
            diagnostics=tuple(),
        )

    raw_wire_count = int(np.count_nonzero(np.asarray(phase2.raw_wire_mask, dtype=bool)))
    if raw_wire_count < int(cfg.get("phase6_min_raw_wire_candidates", 30)):
        empty = _empty_phase3_like(phase3, "SKIPPED_TOO_FEW_RAW_WIRE_CANDIDATES")
        return PowerPhase6Recovery(
            version=PHASE6_VERSION,
            point_count=n,
            status="SKIPPED_TOO_FEW_RAW_WIRE_CANDIDATES",
            activated=False,
            normal_path_success=False,
            fallback_attempted=False,
            fallback_accepted=False,
            original_phase3=phase3,
            fallback_phase3=empty,
            effective_phase3=phase3,
            bundle_track_ids=tuple(),
            bundle_pair_count=0,
            bundle_common_span_m=0.0,
            diagnostics=tuple(),
        )

    # Explicit fallback values intentionally override any strict Phase-3
    # settings supplied by a model-specific path. This is the cross-mode part:
    # Advanced and Premium feed the same Phase-2 physical evidence into one
    # common sparse/short-span conductor recovery profile.
    fcfg = dict(cfg)
    overrides = {
        "phase3_seed_score_min": float(cfg.get("phase6_seed_score_min", 0.24)),
        "phase3_min_raw_candidates": int(cfg.get("phase6_min_seed_candidates", 16)),
        "phase3_min_nodes": int(cfg.get("phase6_min_nodes", 4)),
        "phase3_min_span_m": float(cfg.get("phase6_min_span_m", 5.0)),
        "phase3_min_bin_coverage": float(cfg.get("phase6_min_bin_coverage", 0.18)),
        "phase3_min_median_score": float(cfg.get("phase6_min_median_score", 0.42)),
        "phase3_node_lateral_radius_m": float(cfg.get("phase6_node_lateral_radius_m", 0.62)),
        "phase3_node_z_radius_m": float(cfg.get("phase6_node_z_radius_m", 0.62)),
        "phase3_node_max_lateral_span_m": float(cfg.get("phase6_node_max_lateral_span_m", 1.05)),
        "phase3_node_max_z_span_m": float(cfg.get("phase6_node_max_z_span_m", 1.05)),
        "phase3_link_max_station_gap_m": float(cfg.get("phase6_link_max_station_gap_m", 6.5)),
        "phase3_link_established_max_station_gap_m": float(cfg.get("phase6_link_established_max_station_gap_m", 9.0)),
        "phase3_link_gap_bridge_score_min": float(cfg.get("phase6_link_gap_bridge_score_min", 0.52)),
        "phase3_long_track_min_pass_fraction": float(cfg.get("phase6_long_track_min_pass_fraction", 0.58)),
        "phase3_link_lateral_tolerance_m": float(cfg.get("phase6_link_lateral_tolerance_m", 1.05)),
        "phase3_link_z_tolerance_m": float(cfg.get("phase6_link_z_tolerance_m", 1.65)),
        "phase3_max_lateral_residual_p90": float(cfg.get("phase6_max_lateral_residual_p90", 0.78)),
        "phase3_max_z_residual_p90": float(cfg.get("phase6_max_z_residual_p90", 1.10)),
        "phase3_max_lateral_slope": float(cfg.get("phase6_max_lateral_slope", 0.25)),
        "phase3_max_abs_quadratic": float(cfg.get("phase6_max_abs_quadratic", 0.060)),
        "phase3_min_candidate_points": int(cfg.get("phase6_min_candidate_points", 10)),
        "phase3_context_tube_lateral_m": float(cfg.get("phase6_context_tube_lateral_m", 0.60)),
        "phase3_context_tube_z_m": float(cfg.get("phase6_context_tube_z_m", 0.60)),
        "phase3_projection_lateral_m": float(cfg.get("phase6_projection_lateral_m", 0.48)),
        "phase3_projection_z_m": float(cfg.get("phase6_projection_z_m", 0.48)),
        "phase3_projection_station_extension_m": float(cfg.get("phase6_projection_station_extension_m", 3.0)),
    }
    fcfg.update(overrides)

    fallback = build_power_phase3_tracks(
        xyz,
        hag,
        phase1,
        phase2,
        cl_coords=cl_coords,
        cl_width=float(cl_width),
        linearity_05=linearity_05,
        planarity_05=planarity_05,
        verticality_05=verticality_05,
        linearity_10=linearity_10,
        verticality_10=verticality_10,
        cl_station=cl_station,
        cl_dist=cl_dist,
        config=fcfg,
    )

    diagnostics = [{
        "raw_wire_candidates": raw_wire_count,
        "normal_phase3_status": str(phase3.status),
        "normal_tracks": int(len(phase3.tracks)),
        "candidate_station_span_m": float(candidate_station_span),
        "covered_station_span_m": float(covered_station_span),
        "normal_station_coverage": float(station_coverage),
        "incomplete_long_selection": bool(incomplete_long_selection),
        "fallback_phase3_status": str(fallback.status),
        "fallback_tracks": int(len(fallback.tracks)),
    }]

    groups, pair_diag = _bundle_groups(tuple(fallback.tracks), cfg)
    min_tracks = int(cfg.get("phase6_bundle_min_tracks", 2))
    max_tracks = int(cfg.get("phase6_bundle_max_tracks", 12))
    min_common_span = float(cfg.get("phase6_bundle_min_common_span_m", 3.5))

    best = []
    best_span = 0.0
    for g in groups:
        if not (min_tracks <= len(g) <= max_tracks):
            continue
        ts = [fallback.tracks[i] for i in g]
        common_min = max(float(t.station_min) for t in ts)
        common_max = min(float(t.station_max) for t in ts)
        common_span = max(0.0, common_max - common_min)
        # If every track does not share one interval, allow a strong connected
        # bundle when median span is substantial; this covers partial wires at
        # a fence edge while still requiring at least two compatible pairs.
        pair_count = sum(1 for p in pair_diag if p["compatible"] and p["i"] in g and p["j"] in g)
        median_span = float(np.median([float(t.span) for t in ts]))
        effective_span = max(common_span, min(median_span, float(cfg.get("phase6_bundle_span_cap_m", 30.0))))
        if effective_span < min_common_span:
            continue
        if pair_count < max(1, len(g) - 1):
            continue
        score = (len(g), effective_span, pair_count)
        if not best or score > (len(best), best_span, 0):
            best = list(g)
            best_span = effective_span

    if not best:
        diagnostics.append({
            "bundle_reject": True,
            "groups": [list(map(int, g)) for g in groups],
            "compatible_pairs": int(sum(1 for p in pair_diag if p["compatible"])),
        })
        return PowerPhase6Recovery(
            version=PHASE6_VERSION,
            point_count=n,
            status="FALLBACK_REJECTED_NO_PARALLEL_BUNDLE",
            activated=True,
            normal_path_success=False,
            fallback_attempted=True,
            fallback_accepted=False,
            original_phase3=phase3,
            fallback_phase3=fallback,
            effective_phase3=phase3,
            bundle_track_ids=tuple(),
            bundle_pair_count=0,
            bundle_common_span_m=0.0,
            diagnostics=tuple(diagnostics),
        )

    pair_count = sum(1 for p in pair_diag if p["compatible"] and p["i"] in best and p["j"] in best)
    # The fallback masks are accepted as one physical bundle. Phase 3's own
    # thin-track geometry and Phase-2 raw mask remain the point-level guard;
    # Phase 6 merely decides whether the fallback is trustworthy enough to use.
    diagnostics.append({
        "bundle_accept": True,
        "track_ids": [int(x) for x in best],
        "bundle_tracks": int(len(best)),
        "compatible_pairs": int(pair_count),
        "effective_span_m": float(best_span),
        "projected_active_points": int(np.count_nonzero(fallback.projected_active_mask)),
        "confirmed_context_points": int(np.count_nonzero(fallback.confirmed_context_mask)),
    })

    if np.count_nonzero(np.asarray(fallback.projected_active_mask, dtype=bool)) == 0:
        return PowerPhase6Recovery(
            version=PHASE6_VERSION,
            point_count=n,
            status="FALLBACK_REJECTED_ZERO_ACTIVE_PROJECTION",
            activated=True,
            normal_path_success=False,
            fallback_attempted=True,
            fallback_accepted=False,
            original_phase3=phase3,
            fallback_phase3=fallback,
            effective_phase3=phase3,
            bundle_track_ids=tuple(int(x) for x in best),
            bundle_pair_count=int(pair_count),
            bundle_common_span_m=float(best_span),
            diagnostics=tuple(diagnostics),
        )

    return PowerPhase6Recovery(
        version=PHASE6_VERSION,
        point_count=n,
        status="RECOVERY_APPLIED",
        activated=True,
        normal_path_success=False,
        fallback_attempted=True,
        fallback_accepted=True,
        original_phase3=phase3,
        fallback_phase3=fallback,
        effective_phase3=fallback,
        bundle_track_ids=tuple(int(x) for x in best),
        bundle_pair_count=int(pair_count),
        bundle_common_span_m=float(best_span),
        diagnostics=tuple(diagnostics),
    )
