from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

from .power_phase1_state import PowerPhase1State
from .power_phase2_candidates import PowerPhase2Candidates
from .power_phase3_tracks import PowerPhase3Tracks, _signed_lateral


PHASE10_VERSION = "NAKSHA_POWER_PHASE10_CONDUCTOR_DENSIFICATION_V3_17"


def _freeze(arr: np.ndarray) -> np.ndarray:
    arr.setflags(write=False)
    return arr


def _safe01(arr) -> np.ndarray:
    a = np.asarray(arr, dtype=np.float32).reshape(-1)
    return np.clip(np.nan_to_num(a, nan=0.0, posinf=1.0, neginf=0.0), 0.0, 1.0)


def _base_distribution(indices: np.ndarray, base: np.ndarray) -> dict:
    indices = np.asarray(indices, dtype=np.int64).reshape(-1)
    if indices.size == 0:
        return {}
    vals, cnt = np.unique(np.asarray(base)[indices], return_counts=True)
    return {str(int(v)): int(c) for v, c in zip(vals, cnt)}


def _profile_from_seed(
    station: np.ndarray,
    lateral: np.ndarray,
    z: np.ndarray,
    seed_idx: np.ndarray,
    *,
    station_min: float,
    station_max: float,
    bin_m: float,
    min_bin_points: int,
):
    """Build a robust local conductor profile from already-confirmed points.

    Phase 3's accepted track proves the conductor exists.  Phase 10 uses the
    confirmed Phase-3 points only to recover the *local* sag profile; this avoids
    using a broad polynomial tube that could absorb nearby vegetation.
    """
    seed_idx = np.asarray(seed_idx, dtype=np.int64).reshape(-1)
    if seed_idx.size < max(3, min_bin_points):
        return None
    ss = station[seed_idx]
    good = np.isfinite(ss) & np.isfinite(lateral[seed_idx]) & np.isfinite(z[seed_idx])
    seed_idx = seed_idx[good]
    if seed_idx.size < max(3, min_bin_points):
        return None

    bin_m = max(float(bin_m), 0.25)
    s0 = float(station_min)
    bid = np.floor((station[seed_idx] - s0) / bin_m).astype(np.int64)
    centers_s = []
    centers_l = []
    centers_z = []
    counts = []
    for b in np.unique(bid):
        ii = seed_idx[bid == b]
        if ii.size < int(min_bin_points):
            continue
        centers_s.append(float(np.median(station[ii])))
        centers_l.append(float(np.median(lateral[ii])))
        centers_z.append(float(np.median(z[ii])))
        counts.append(int(ii.size))

    if len(centers_s) < 3:
        return None
    order = np.argsort(centers_s)
    return (
        np.asarray(centers_s, dtype=np.float64)[order],
        np.asarray(centers_l, dtype=np.float64)[order],
        np.asarray(centers_z, dtype=np.float64)[order],
        np.asarray(counts, dtype=np.int32)[order],
    )


@dataclass(frozen=True)
class PowerPhase10Densification:
    """Dense point completion around already-confirmed conductor tracks.

    Phase 10 does not discover a new conductor route.  It is allowed to add
    points only inside a tight 3D tube around a Phase-3/Phase-6 confirmed track.
    This is the missing step between accurate track fitting and complete point
    classification: model labels such as Ground/Vegetation do not veto a point
    once the physical conductor track has already been proven.
    """

    version: str
    point_count: int
    status: str
    input_context_mask: np.ndarray
    input_active_mask: np.ndarray
    densified_context_mask: np.ndarray
    densified_active_mask: np.ndarray
    added_context_mask: np.ndarray
    added_active_mask: np.ndarray
    effective_phase3: PowerPhase3Tracks
    diagnostics: Tuple[dict, ...]
    exact_fence_only: bool
    semantic_gate: bool
    mode_independent: bool
    base_class_distribution_added: dict

    def report(self) -> dict:
        return {
            "version": self.version,
            "status": self.status,
            "input_context_wire_points": int(np.count_nonzero(self.input_context_mask)),
            "input_active_wire_points": int(np.count_nonzero(self.input_active_mask)),
            "added_context_wire_points": int(np.count_nonzero(self.added_context_mask)),
            "added_active_wire_points": int(np.count_nonzero(self.added_active_mask)),
            "final_context_wire_points": int(np.count_nonzero(self.densified_context_mask)),
            "final_active_wire_points": int(np.count_nonzero(self.densified_active_mask)),
            "tracks_available": int(len(self.effective_phase3.tracks)),
            "tracks_densified": int(sum(1 for d in self.diagnostics if d.get("densified"))),
            "semantic_class_gate": bool(self.semantic_gate),
            "classification_writes_in_module": False,
            "exact_fence_only": bool(self.exact_fence_only),
            "mode_independent": bool(self.mode_independent),
            "added_base_class_distribution": dict(self.base_class_distribution_added),
            "immutable": bool(
                not self.densified_context_mask.flags.writeable
                and not self.densified_active_mask.flags.writeable
                and not self.added_context_mask.flags.writeable
                and not self.added_active_mask.flags.writeable
            ),
            "track_reports": [dict(d) for d in self.diagnostics],
        }


def _passthrough(
    phase1: PowerPhase1State,
    phase3: PowerPhase3Tracks,
    status: str,
) -> PowerPhase10Densification:
    n = int(phase1.point_count)
    ctx = np.asarray(phase3.confirmed_context_mask, dtype=bool).copy()
    act = np.asarray(phase3.projected_active_mask, dtype=bool).copy()
    z = np.zeros(n, dtype=bool)
    return PowerPhase10Densification(
        version=PHASE10_VERSION,
        point_count=n,
        status=str(status),
        input_context_mask=_freeze(ctx.copy()),
        input_active_mask=_freeze(act.copy()),
        densified_context_mask=_freeze(ctx.copy()),
        densified_active_mask=_freeze(act.copy()),
        added_context_mask=_freeze(z.copy()),
        added_active_mask=_freeze(z.copy()),
        effective_phase3=phase3,
        diagnostics=tuple(),
        exact_fence_only=True,
        semantic_gate=False,
        mode_independent=True,
        base_class_distribution_added={},
    )


def build_power_phase10_densification(
    xyz,
    hag,
    phase1: PowerPhase1State,
    phase2: PowerPhase2Candidates,
    effective_phase3: PowerPhase3Tracks,
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
) -> PowerPhase10Densification:
    """Complete all LiDAR returns belonging to already-confirmed conductors.

    Safety contract
    ---------------
    * no track = no densification;
    * only a tight local tube around an accepted Phase-3/6 track is eligible;
    * base semantic labels are evidence only and never an inclusion gate;
    * Ground/Vegetation points may become Wire only when they geometrically sit
      on that confirmed 3D conductor profile;
    * exact Phase-1 active/fence scope is the only classification-write scope;
    * protected points are never added;
    * nearby foliage outside the tight tube remains untouched.
    """
    cfg = dict(config or {})
    xyz = np.asarray(xyz, dtype=np.float64)
    hag = np.asarray(hag, dtype=np.float32).reshape(-1)
    n = int(phase1.point_count)
    if xyz.ndim != 2 or xyz.shape[1] < 3 or len(xyz) != n or len(hag) != n:
        raise ValueError("Phase10 xyz/hag length mismatch")
    if phase2.point_count != n or effective_phase3.point_count != n:
        raise ValueError("Phase10 prior-phase length mismatch")
    if cl_coords is None or float(cl_width or 0.0) <= 0.0:
        return _passthrough(phase1, effective_phase3, "SKIPPED_NO_CL")
    if len(effective_phase3.tracks) == 0:
        return _passthrough(phase1, effective_phase3, "SKIPPED_NO_CONFIRMED_TRACKS")

    # Signed lateral is required to distinguish parallel wires. Reuse station
    # and distance from the parent power stage when supplied.
    station_calc, lateral, dist_calc = _signed_lateral(
        xyz[:, :2],
        np.asarray(cl_coords, dtype=np.float64),
        step=float(cfg.get("phase10_cl_sample_step_m", 0.75)),
    )
    if cl_station is None:
        station = station_calc
    else:
        station = np.asarray(cl_station, dtype=np.float64).reshape(-1)
        if len(station) != n:
            raise ValueError("Phase10 cl_station length mismatch")
        station = np.where(np.isfinite(station), station, station_calc)
    if cl_dist is None:
        cdist = dist_calc
    else:
        cdist = np.asarray(cl_dist, dtype=np.float64).reshape(-1)
        if len(cdist) != n:
            raise ValueError("Phase10 cl_dist length mismatch")
        cdist = np.where(np.isfinite(cdist), cdist, dist_calc)

    l05 = _safe01(linearity_05)
    p05 = _safe01(planarity_05)
    v05 = _safe01(verticality_05)
    l10 = _safe01(linearity_10)
    v10 = _safe01(verticality_10)
    if any(len(a) != n for a in (l05, p05, v05, l10, v10)):
        raise ValueError("Phase10 geometry length mismatch")
    line_strength = np.maximum(l05, l10)
    vert_strength = np.maximum(v05, v10)

    active = np.asarray(phase1.active_mask, dtype=bool)
    protected = np.asarray(phase1.protected_mask, dtype=bool)
    raw = np.asarray(phase2.raw_wire_mask, dtype=bool)
    input_ctx = np.asarray(effective_phase3.confirmed_context_mask, dtype=bool).copy()
    input_act = np.asarray(effective_phase3.projected_active_mask, dtype=bool).copy()

    final_ctx = input_ctx.copy()
    final_act = input_act.copy()
    finite = (
        np.isfinite(station)
        & np.isfinite(lateral)
        & np.isfinite(cdist)
        & np.isfinite(xyz[:, 2])
        & np.isfinite(hag)
    )
    corridor = cdist <= float(cl_width) + float(cfg.get("phase10_cl_slack_m", 0.25))
    hag_ok = (
        (hag >= float(cfg.get("phase10_hag_min_m", 2.0)))
        & (hag <= float(cfg.get("phase10_hag_max_m", 90.0)))
    )

    # A very tight profile core is semantic/geometry agnostic. The slightly
    # wider shell requires line-like or existing Phase-2 evidence. This is the
    # central false-positive guard for leaves/branches close to a conductor.
    core_lat = float(cfg.get("phase10_core_lateral_m", 0.12))
    core_z = float(cfg.get("phase10_core_z_m", 0.12))
    shell_lat = float(cfg.get("phase10_shell_lateral_m", 0.30))
    shell_z = float(cfg.get("phase10_shell_z_m", 0.30))
    shell_line = float(cfg.get("phase10_shell_linearity_min", 0.18))
    shell_plan = float(cfg.get("phase10_shell_planarity_max", 0.82))
    shell_vert = float(cfg.get("phase10_shell_verticality_max", 0.72))
    span_ext = float(cfg.get("phase10_station_extension_m", 1.25))
    profile_bin = float(cfg.get("phase10_profile_bin_m", 0.75))
    min_bin_pts = int(cfg.get("phase10_profile_min_bin_points", 1))
    seed_lat_tol = float(cfg.get("phase10_seed_lateral_m", 0.70))
    min_track_span = float(cfg.get("phase10_min_track_span_m", 5.0))
    min_track_cov = float(cfg.get("phase10_min_track_coverage", 0.30))
    diagnostics = []

    for tr in effective_phase3.tracks:
        if float(tr.span) < min_track_span or float(tr.coverage) < min_track_cov:
            diagnostics.append({
                "track_id": int(tr.track_id),
                "densified": False,
                "reason": "track_quality_guard",
                "span_m": float(tr.span),
                "coverage": float(tr.coverage),
                "seed_points": 0,
                "added_context": 0,
                "added_active": 0,
            })
            continue

        in_seed_span = (
            (station >= float(tr.station_min) - 0.25)
            & (station <= float(tr.station_max) + 0.25)
        )
        # The union confirmed-context mask contains every accepted track. Use
        # the fitted lateral + broad adaptive Z gate to isolate this track's
        # own already-confirmed samples before constructing a local profile.
        z_seed_tol = max(
            float(cfg.get("phase10_seed_z_min_m", 0.80)),
            min(
                float(cfg.get("phase10_seed_z_max_m", 1.80)),
                float(tr.z_residual_p90) * 2.4 + 0.35,
            ),
        )
        seed_pool = np.flatnonzero(input_ctx & finite & in_seed_span)
        if seed_pool.size:
            xrel_seed = station[seed_pool] - float(tr.station_center)
            lat_poly_seed = float(tr.lateral_slope) * xrel_seed + float(tr.lateral_intercept)
            z_poly_seed = (
                float(tr.z_quadratic) * xrel_seed * xrel_seed
                + float(tr.z_slope) * xrel_seed
                + float(tr.z_intercept)
            )
            keep_seed = (
                (np.abs(lateral[seed_pool] - lat_poly_seed) <= seed_lat_tol)
                & (np.abs(xyz[seed_pool, 2] - z_poly_seed) <= z_seed_tol)
            )
            seed_idx = seed_pool[keep_seed]
        else:
            seed_idx = np.empty(0, dtype=np.int64)
        profile = _profile_from_seed(
            station,
            lateral,
            xyz[:, 2],
            seed_idx,
            station_min=float(tr.station_min),
            station_max=float(tr.station_max),
            bin_m=profile_bin,
            min_bin_points=min_bin_pts,
        )
        if profile is None:
            diagnostics.append({
                "track_id": int(tr.track_id),
                "densified": False,
                "reason": "insufficient_local_profile",
                "span_m": float(tr.span),
                "coverage": float(tr.coverage),
                "seed_points": int(seed_idx.size),
                "added_context": 0,
                "added_active": 0,
            })
            continue

        ps, plat, pz, pcount = profile
        in_span = (
            (station >= float(tr.station_min) - span_ext)
            & (station <= float(tr.station_max) + span_ext)
        )
        eligible = np.flatnonzero(finite & corridor & hag_ok & in_span & (~protected))
        if eligible.size:
            profile_lat = np.interp(station[eligible], ps, plat, left=plat[0], right=plat[-1])
            profile_z = np.interp(station[eligible], ps, pz, left=pz[0], right=pz[-1])
            dlat = np.abs(lateral[eligible] - profile_lat)
            dz = np.abs(xyz[eligible, 2] - profile_z)

            # Tight tube = essentially the physical wire centerline. Wider shell
            # is allowed only when Phase-2 or independent line geometry supports it.
            tight = (dlat <= core_lat) & (dz <= core_z)
            shell_geom = (
                (dlat <= shell_lat)
                & (dz <= shell_z)
                & (
                    raw[eligible]
                    | (
                        (line_strength[eligible] >= shell_line)
                        & (p05[eligible] <= shell_plan)
                        & (vert_strength[eligible] <= shell_vert)
                    )
                )
            )
            tube_idx = eligible[tight | shell_geom]
        else:
            tube_idx = np.empty(0, dtype=np.int64)

        # Context can support later pylon stages. Only active/fence points are
        # ever returned in densified_active_mask for actual classification.
        add_ctx_idx = tube_idx[~final_ctx[tube_idx]] if tube_idx.size else np.empty(0, dtype=np.int64)
        active_tube_idx = tube_idx[active[tube_idx]] if tube_idx.size else np.empty(0, dtype=np.int64)
        add_act_idx = active_tube_idx[~final_act[active_tube_idx]] if active_tube_idx.size else np.empty(0, dtype=np.int64)
        if tube_idx.size:
            final_ctx[tube_idx] = True
        if active_tube_idx.size:
            final_act[active_tube_idx] = True

        diagnostics.append({
            "track_id": int(tr.track_id),
            "densified": True,
            "reason": "confirmed_track_local_tube",
            "span_m": float(tr.span),
            "coverage": float(tr.coverage),
            "seed_points": int(seed_idx.size),
            "profile_bins": int(len(ps)),
            "profile_bin_min_points": int(np.min(pcount)) if len(pcount) else 0,
            "profile_bin_median_points": float(np.median(pcount)) if len(pcount) else 0.0,
            "added_context": int(add_ctx_idx.size),
            "added_active": int(add_act_idx.size),
            "core_lateral_m": float(core_lat),
            "core_z_m": float(core_z),
            "shell_lateral_m": float(shell_lat),
            "shell_z_m": float(shell_z),
        })

    # Defensive exact-scope and protection assertions.
    final_act &= active & (~protected)
    added_ctx = final_ctx & (~input_ctx)
    added_act = final_act & (~input_act)
    if np.any(added_act & (~active)):
        raise RuntimeError("Phase10 exact-fence safety assertion failed")
    if np.any(added_act & protected):
        raise RuntimeError("Phase10 protected-point assertion failed")

    # Carry the same accepted conductor routes forward, replacing only their
    # point masks. Phase 5/7/8/9 therefore see denser evidence without any
    # change to track identity or model weights.
    rescue = final_act & (~raw)
    support = final_ctx | final_act
    p3 = PowerPhase3Tracks(
        version=effective_phase3.version,
        point_count=n,
        status=effective_phase3.status,
        confirmed_context_mask=_freeze(final_ctx.copy()),
        projected_active_mask=_freeze(final_act.copy()),
        projection_rescue_mask=_freeze(rescue.copy()),
        wire_support_mask=_freeze(support.copy()),
        tracks=effective_phase3.tracks,
        semantic_gate=False,
        exact_fence_projection=True,
    )

    added_idx = np.flatnonzero(added_act)
    status = "DENSIFIED" if added_idx.size else "PASS_NO_ADDITIONAL_POINTS"
    return PowerPhase10Densification(
        version=PHASE10_VERSION,
        point_count=n,
        status=status,
        input_context_mask=_freeze(input_ctx.copy()),
        input_active_mask=_freeze(input_act.copy()),
        densified_context_mask=_freeze(final_ctx.copy()),
        densified_active_mask=_freeze(final_act.copy()),
        added_context_mask=_freeze(added_ctx.copy()),
        added_active_mask=_freeze(added_act.copy()),
        effective_phase3=p3,
        diagnostics=tuple(diagnostics),
        exact_fence_only=True,
        semantic_gate=False,
        mode_independent=True,
        base_class_distribution_added=_base_distribution(added_idx, phase1.base_classes),
    )
