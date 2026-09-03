# from __future__ import annotations

# from dataclasses import dataclass
# from typing import Optional, Sequence, Tuple

# import numpy as np
# from scipy.spatial import cKDTree

# from .power_phase1_state import PowerPhase1State
# from .power_phase2_candidates import PowerPhase2Candidates


# PHASE3_VERSION = "NAKSHA_POWER_PHASE3_CONDUCTOR_TRACKS_V3_9"


# def _freeze(arr: np.ndarray) -> np.ndarray:
#     arr.setflags(write=False)
#     return arr


# def _safe01(arr) -> np.ndarray:
#     a = np.asarray(arr, dtype=np.float32).reshape(-1)
#     return np.clip(np.nan_to_num(a, nan=0.0, posinf=1.0, neginf=0.0), 0.0, 1.0)


# def _sample_centerline(coords: np.ndarray, step: float = 0.75):
#     line = np.asarray(coords, dtype=np.float64)
#     if line.ndim != 2 or line.shape[0] < 2 or line.shape[1] < 2:
#         raise ValueError("Phase3 requires a valid CL polyline")

#     sample_xy = []
#     sample_station = []
#     sample_normal = []
#     cumulative = 0.0
#     for a, b in zip(line[:-1, :2], line[1:, :2]):
#         d = b - a
#         seg = float(np.linalg.norm(d))
#         if seg <= 1e-9:
#             continue
#         tangent = d / seg
#         normal = np.array([-tangent[1], tangent[0]], dtype=np.float64)
#         n = max(1, int(np.ceil(seg / max(float(step), 0.25))))
#         # Avoid duplicating the first point of every segment except the first.
#         t = np.linspace(0.0, 1.0, n + 1, endpoint=True)
#         pts = a[None, :] + t[:, None] * d[None, :]
#         sample_xy.append(pts)
#         sample_station.append(cumulative + t * seg)
#         sample_normal.append(np.repeat(normal[None, :], len(t), axis=0))
#         cumulative += seg

#     if not sample_xy:
#         raise ValueError("Phase3 CL has no valid segments")
#     return (
#         np.vstack(sample_xy),
#         np.concatenate(sample_station),
#         np.vstack(sample_normal),
#     )


# def _signed_lateral(points_xy: np.ndarray, cl_coords: np.ndarray, step: float = 0.75):
#     sample_xy, sample_station, sample_normal = _sample_centerline(cl_coords, step=step)
#     tree = cKDTree(sample_xy)
#     dist, nn = tree.query(np.asarray(points_xy, dtype=np.float64), k=1, workers=-1)
#     nn = np.asarray(nn, dtype=np.int64)
#     delta = np.asarray(points_xy, dtype=np.float64) - sample_xy[nn]
#     lateral = np.sum(delta * sample_normal[nn], axis=1)
#     station = sample_station[nn]
#     del tree
#     return (
#         np.asarray(station, dtype=np.float64),
#         np.asarray(lateral, dtype=np.float64),
#         np.asarray(dist, dtype=np.float64),
#     )


# def _cluster_bin_nodes(
#     idx: np.ndarray,
#     station: np.ndarray,
#     lateral: np.ndarray,
#     z: np.ndarray,
#     score: np.ndarray,
#     *,
#     lat_radius: float,
#     z_radius: float,
#     max_lat_span: float,
#     max_z_span: float,
# ):
#     """Create compact cross-section nodes inside one station bin.

#     A conductor should appear as a very small cluster in lateral/Z space inside
#     a short station slice. Vegetation can enter the Phase-2 raw cloud, but its
#     local cross-section is usually much thicker and less coherent.
#     """
#     idx = np.asarray(idx, dtype=np.int64)
#     if idx.size == 0:
#         return []

#     coords = np.column_stack((
#         lateral[idx] / max(float(lat_radius), 1e-6),
#         z[idx] / max(float(z_radius), 1e-6),
#     ))
#     tree = cKDTree(coords)
#     pairs = tree.query_pairs(1.05, output_type="ndarray")
#     del tree

#     parent = np.arange(idx.size, dtype=np.int32)

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

#     roots = np.array([find(i) for i in range(idx.size)], dtype=np.int32)
#     _, labels = np.unique(roots, return_inverse=True)

#     nodes = []
#     for lab in range(int(labels.max()) + 1 if labels.size else 0):
#         loc = np.flatnonzero(labels == lab)
#         gi = idx[loc]
#         if gi.size == 0:
#             continue
#         latv = lateral[gi]
#         zv = z[gi]
#         lat_span = float(np.percentile(latv, 90) - np.percentile(latv, 10)) if gi.size > 2 else float(np.ptp(latv))
#         z_span = float(np.percentile(zv, 90) - np.percentile(zv, 10)) if gi.size > 2 else float(np.ptp(zv))
#         if lat_span > float(max_lat_span) or z_span > float(max_z_span):
#             continue
#         nodes.append({
#             "indices": gi,
#             "station": float(np.median(station[gi])),
#             "lateral": float(np.median(latv)),
#             "z": float(np.median(zv)),
#             "score": float(np.median(score[gi])),
#             "points": int(gi.size),
#             "lat_span": lat_span,
#             "z_span": z_span,
#         })
#     return nodes


# def _track_prediction(nodes, ds: float):
#     last = nodes[-1]
#     if len(nodes) < 2:
#         return float(last["lateral"]), float(last["z"])
#     prev = nodes[-2]
#     denom = max(float(last["station"] - prev["station"]), 1e-6)
#     lat_slope = np.clip((float(last["lateral"]) - float(prev["lateral"])) / denom, -0.25, 0.25)
#     z_slope = np.clip((float(last["z"]) - float(prev["z"])) / denom, -0.65, 0.65)
#     return (
#         float(last["lateral"]) + float(lat_slope) * ds,
#         float(last["z"]) + float(z_slope) * ds,
#     )


# def _fit_track_model(nodes, bin_size: float, cfg: dict):
#     if not nodes:
#         return None
#     s = np.asarray([n["station"] for n in nodes], dtype=np.float64)
#     lat = np.asarray([n["lateral"] for n in nodes], dtype=np.float64)
#     z = np.asarray([n["z"] for n in nodes], dtype=np.float64)
#     weights = np.asarray([max(1, int(n["points"])) for n in nodes], dtype=np.float64)
#     scores = np.asarray([n["score"] for n in nodes], dtype=np.float64)

#     span = float(np.max(s) - np.min(s)) if s.size else 0.0
#     if s.size < int(cfg.get("phase3_min_nodes", 5)):
#         return None
#     if span < float(cfg.get("phase3_min_span_m", 10.0)):
#         return None

#     expected_bins = max(1.0, span / max(float(bin_size), 1e-6) + 1.0)
#     coverage = float(len(np.unique(np.floor((s - np.min(s)) / max(float(bin_size), 1e-6)))) / expected_bins)
#     if coverage < float(cfg.get("phase3_min_bin_coverage", 0.24)):
#         return None
#     if float(np.median(scores)) < float(cfg.get("phase3_min_median_score", 0.50)):
#         return None

#     sc = float(np.median(s))
#     x = s - sc
#     try:
#         lat_coef = np.polyfit(x, lat, deg=1, w=np.sqrt(weights))
#         z_deg = 2 if len(nodes) >= 6 and span >= 12.0 else 1
#         z_coef = np.polyfit(x, z, deg=z_deg, w=np.sqrt(weights))
#     except Exception:
#         return None

#     lat_pred = np.polyval(lat_coef, x)
#     z_pred = np.polyval(z_coef, x)
#     lat_res = np.abs(lat - lat_pred)
#     z_res = np.abs(z - z_pred)
#     lat_p90 = float(np.percentile(lat_res, 90))
#     z_p90 = float(np.percentile(z_res, 90))

#     if lat_p90 > float(cfg.get("phase3_max_lateral_residual_p90", 0.55)):
#         return None
#     if z_p90 > float(cfg.get("phase3_max_z_residual_p90", 0.75)):
#         return None

#     lat_slope = float(lat_coef[0])
#     if abs(lat_slope) > float(cfg.get("phase3_max_lateral_slope", 0.18)):
#         return None

#     # Convert to a quadratic coefficient even for a linear fit so the caller
#     # can evaluate every track with one common representation.
#     if len(z_coef) == 2:
#         z_quad = np.array([0.0, float(z_coef[0]), float(z_coef[1])], dtype=np.float64)
#     else:
#         z_quad = np.asarray(z_coef, dtype=np.float64)
#     if abs(float(z_quad[0])) > float(cfg.get("phase3_max_abs_quadratic", 0.035)):
#         return None

#     point_count = int(sum(int(n["points"]) for n in nodes))
#     if point_count < int(cfg.get("phase3_min_candidate_points", 14)):
#         return None

#     return {
#         "station_center": sc,
#         "station_min": float(np.min(s)),
#         "node_station": s.copy(),
#         "node_lateral": lat.copy(),
#         "node_z": z.copy(),
#         "station_max": float(np.max(s)),
#         "span": span,
#         "nodes": int(len(nodes)),
#         "candidate_points": point_count,
#         "coverage": coverage,
#         "median_score": float(np.median(scores)),
#         "lat_coef": np.asarray(lat_coef, dtype=np.float64),
#         "z_coef": z_quad,
#         "lat_residual_p90": lat_p90,
#         "z_residual_p90": z_p90,
#     }


# @dataclass(frozen=True)
# class ConductorTrack:
#     track_id: int
#     station_center: float
#     station_min: float
#     station_max: float
#     span: float
#     nodes: int
#     candidate_points: int
#     coverage: float
#     median_score: float
#     lateral_slope: float
#     lateral_intercept: float
#     z_quadratic: float
#     z_slope: float
#     z_intercept: float
#     lateral_residual_p90: float
#     z_residual_p90: float

#     def report(self) -> dict:
#         return {
#             "track_id": int(self.track_id),
#             "station_min": float(self.station_min),
#             "station_max": float(self.station_max),
#             "span_m": float(self.span),
#             "nodes": int(self.nodes),
#             "candidate_points": int(self.candidate_points),
#             "coverage": float(self.coverage),
#             "median_score": float(self.median_score),
#             "lateral_residual_p90": float(self.lateral_residual_p90),
#             "z_residual_p90": float(self.z_residual_p90),
#         }


# @dataclass(frozen=True)
# class PowerPhase3Tracks:
#     version: str
#     point_count: int
#     status: str
#     confirmed_context_mask: np.ndarray
#     projected_active_mask: np.ndarray
#     projection_rescue_mask: np.ndarray
#     wire_support_mask: np.ndarray
#     tracks: Tuple[ConductorTrack, ...]
#     semantic_gate: bool
#     exact_fence_projection: bool

#     def report(self) -> dict:
#         return {
#             "version": self.version,
#             "status": self.status,
#             "semantic_gate": bool(self.semantic_gate),
#             "classification_writes_in_module": False,
#             "exact_fence_projection": bool(self.exact_fence_projection),
#             "tracks": int(len(self.tracks)),
#             "confirmed_context_points": int(np.count_nonzero(self.confirmed_context_mask)),
#             "projected_active_points": int(np.count_nonzero(self.projected_active_mask)),
#             "projection_rescue_points": int(np.count_nonzero(self.projection_rescue_mask)),
#             "wire_support_points": int(np.count_nonzero(self.wire_support_mask)),
#             "track_reports": [t.report() for t in self.tracks],
#             "immutable": bool(
#                 not self.confirmed_context_mask.flags.writeable
#                 and not self.projected_active_mask.flags.writeable
#                 and not self.wire_support_mask.flags.writeable
#             ),
#         }


# def _empty_state(n: int, status: str) -> PowerPhase3Tracks:
#     z = np.zeros(int(n), dtype=bool)
#     return PowerPhase3Tracks(
#         version=PHASE3_VERSION,
#         point_count=int(n),
#         status=str(status),
#         confirmed_context_mask=_freeze(z.copy()),
#         projected_active_mask=_freeze(z.copy()),
#         projection_rescue_mask=_freeze(z.copy()),
#         wire_support_mask=_freeze(z.copy()),
#         tracks=tuple(),
#         semantic_gate=False,
#         exact_fence_projection=True,
#     )


# def build_power_phase3_tracks(
#     xyz,
#     hag,
#     phase1: PowerPhase1State,
#     phase2: PowerPhase2Candidates,
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
# ) -> PowerPhase3Tracks:
#     """Fit conductor tracks in full context and project them onto exact fence.

#     Phase 3 is the first Power phase that creates a *confirmed conductor model*.
#     It does not inspect the base semantic class as an inclusion gate. Context
#     points can establish a track across the fence boundary; only the exact
#     Phase-1 active mask is returned for classification write-back.
#     """
#     cfg = dict(config or {})
#     xyz = np.asarray(xyz, dtype=np.float64)
#     hag = np.asarray(hag, dtype=np.float32).reshape(-1)
#     n = len(hag)
#     if xyz.ndim != 2 or xyz.shape[1] < 3 or len(xyz) != n:
#         raise ValueError("Phase3 xyz/hag length mismatch")
#     if phase1.point_count != n or phase2.point_count != n:
#         raise ValueError("Phase3 Phase1/Phase2 length mismatch")
#     if cl_coords is None or float(cl_width or 0.0) <= 0.0:
#         return _empty_state(n, "SKIPPED_NO_CL")

#     raw = np.asarray(phase2.raw_wire_mask, dtype=bool)
#     candidate_idx = np.flatnonzero(raw)
#     if candidate_idx.size < int(cfg.get("phase3_min_raw_candidates", 20)):
#         return _empty_state(n, "SKIPPED_TOO_FEW_RAW_WIRE_CANDIDATES")

#     l05 = _safe01(linearity_05)
#     l10 = _safe01(linearity_10)
#     p05 = _safe01(planarity_05)
#     v05 = _safe01(verticality_05)
#     v10 = _safe01(verticality_10)
#     if len(l05) != n or len(l10) != n or len(p05) != n or len(v05) != n or len(v10) != n:
#         raise ValueError("Phase3 geometry length mismatch")
#     line_strength = np.maximum(l05, l10)
#     vert_strength = np.maximum(v05, v10)

#     # Reuse Phase-2 station/distance when available, but compute signed lateral
#     # from the actual CL so parallel conductors on opposite sides are separated.
#     station_from_cl, lateral, distance_from_cl = _signed_lateral(
#         xyz[:, :2],
#         np.asarray(cl_coords, dtype=np.float64),
#         step=float(cfg.get("phase3_cl_sample_step_m", 0.75)),
#     )
#     if cl_station is not None:
#         station = np.asarray(cl_station, dtype=np.float64).reshape(-1)
#         if len(station) != n:
#             raise ValueError("Phase3 cl_station length mismatch")
#         station = np.where(np.isfinite(station), station, station_from_cl)
#     else:
#         station = station_from_cl
#     if cl_dist is not None:
#         cdist = np.asarray(cl_dist, dtype=np.float64).reshape(-1)
#         if len(cdist) != n:
#             raise ValueError("Phase3 cl_dist length mismatch")
#         cdist = np.where(np.isfinite(cdist), cdist, distance_from_cl)
#     else:
#         cdist = distance_from_cl

#     finite = np.isfinite(station) & np.isfinite(lateral) & np.isfinite(xyz[:, 2]) & np.isfinite(hag)
#     score = np.asarray(phase2.wire_score, dtype=np.float32)
#     seed_min = float(cfg.get("phase3_seed_score_min", 0.36))
#     fit_mask = raw & finite & (score >= seed_min)
#     fit_idx = np.flatnonzero(fit_mask)
#     if fit_idx.size < int(cfg.get("phase3_min_raw_candidates", 20)):
#         return _empty_state(n, "SKIPPED_TOO_FEW_SEED_CANDIDATES")

#     bin_size = float(cfg.get("phase3_station_bin_m", 1.0))
#     s0 = float(np.nanmin(station[fit_idx]))
#     bin_id = np.floor((station[fit_idx] - s0) / max(bin_size, 1e-6)).astype(np.int64)
#     order = np.argsort(bin_id, kind="stable")
#     sorted_idx = fit_idx[order]
#     sorted_bins = bin_id[order]
#     uniq_bins, starts, counts = np.unique(sorted_bins, return_index=True, return_counts=True)

#     nodes_by_bin = []
#     all_nodes = []
#     for b, st0, cnt in zip(uniq_bins, starts, counts):
#         gi = sorted_idx[st0:st0 + cnt]
#         nodes = _cluster_bin_nodes(
#             gi, station, lateral, xyz[:, 2], score,
#             lat_radius=float(cfg.get("phase3_node_lateral_radius_m", 0.48)),
#             z_radius=float(cfg.get("phase3_node_z_radius_m", 0.48)),
#             max_lat_span=float(cfg.get("phase3_node_max_lateral_span_m", 0.85)),
#             max_z_span=float(cfg.get("phase3_node_max_z_span_m", 0.85)),
#         )
#         for node in nodes:
#             node["bin"] = int(b)
#             node["node_id"] = int(len(all_nodes))
#             all_nodes.append(node)
#         if nodes:
#             nodes_by_bin.append((int(b), nodes))

#     if len(all_nodes) < int(cfg.get("phase3_min_nodes", 5)):
#         return _empty_state(n, "SKIPPED_TOO_FEW_NODES")

#     # Greedy one-to-one temporal association in station order. Prediction from
#     # the previous two nodes tolerates conductor sag while strongly penalising
#     # lateral/Z jumps typical of foliage.
#     tracks = []
#     max_gap = float(cfg.get("phase3_link_max_station_gap_m", 4.5))
#     lat_tol = float(cfg.get("phase3_link_lateral_tolerance_m", 0.80))
#     z_tol = float(cfg.get("phase3_link_z_tolerance_m", 1.25))

#     for _, nodes in nodes_by_bin:
#         # Reset per-bin assignment state before building candidate links.
#         for tr in tracks:
#             tr["assigned_this_bin"] = False
#         possible = []
#         for ti, tr in enumerate(tracks):
#             last = tr["nodes"][-1]
#             for ni, node in enumerate(nodes):
#                 ds = float(node["station"] - last["station"])
#                 if ds <= 0.05 or ds > max_gap:
#                     continue
#                 plat, pz = _track_prediction(tr["nodes"], ds)
#                 dlat = abs(float(node["lateral"]) - plat)
#                 dz = abs(float(node["z"]) - pz)
#                 if dlat <= lat_tol and dz <= z_tol:
#                     gap_pen = 0.10 * max(0.0, ds - bin_size) / max(max_gap, 1e-6)
#                     cost = (dlat / max(lat_tol, 1e-6)) ** 2 + (dz / max(z_tol, 1e-6)) ** 2 + gap_pen
#                     possible.append((float(cost), ti, ni))
#         used_t = set()
#         used_n = set()
#         for _, ti, ni in sorted(possible, key=lambda x: x[0]):
#             if ti in used_t or ni in used_n:
#                 continue
#             tracks[ti]["nodes"].append(nodes[ni])
#             tracks[ti]["assigned_this_bin"] = True
#             used_t.add(ti)
#             used_n.add(ni)
#         for ni, node in enumerate(nodes):
#             if ni not in used_n:
#                 tracks.append({"nodes": [node], "assigned_this_bin": True})

#     accepted_models = []
#     accepted_nodes = []
#     for tr in tracks:
#         model = _fit_track_model(tr["nodes"], bin_size, cfg)
#         if model is not None:
#             accepted_models.append(model)
#             accepted_nodes.append(tr["nodes"])

#     if not accepted_models:
#         return _empty_state(n, "NO_ACCEPTED_TRACKS")

#     confirmed_context = np.zeros(n, dtype=bool)
#     projected_active = np.zeros(n, dtype=bool)
#     active = np.asarray(phase1.active_mask, dtype=bool)
#     protected = np.asarray(phase1.protected_mask, dtype=bool)
#     strict_corridor = finite & (cdist <= float(cl_width) + float(cfg.get("phase3_projection_cl_slack_m", 0.20)))
#     wire_orientation = (
#         (vert_strength <= float(cfg.get("phase3_projection_verticality_max", 0.58)))
#         | (
#             (line_strength >= float(cfg.get("phase3_projection_vertical_high_linearity", 0.86)))
#             & (p05 <= float(cfg.get("phase3_projection_vertical_high_planarity", 0.16)))
#         )
#     )
#     projection_geom = (
#         raw | (
#             (line_strength >= float(cfg.get("phase3_projection_linearity_min", 0.30)))
#             & (p05 <= float(cfg.get("phase3_projection_planarity_max", 0.68)))
#         )
#     ) & wire_orientation
#     projection_base = (
#         active
#         & (~protected)
#         & strict_corridor
#         & projection_geom
#         & (hag >= float(cfg.get("phase3_projection_hag_min", 2.0)))
#         & (hag <= float(cfg.get("phase3_projection_hag_max", 90.0)))
#     )

#     tracks_out = []
#     ctx_lat_tol = float(cfg.get("phase3_context_tube_lateral_m", 0.52))
#     ctx_z_tol = float(cfg.get("phase3_context_tube_z_m", 0.52))
#     act_lat_tol = float(cfg.get("phase3_projection_lateral_m", 0.42))
#     act_z_tol = float(cfg.get("phase3_projection_z_m", 0.42))
#     ext = float(cfg.get("phase3_projection_station_extension_m", 2.5))

#     for tid, model in enumerate(accepted_models):
#         x = station - float(model["station_center"])
#         # Projection uses the accepted node profile itself rather than one
#         # global quadratic. This follows real catenary/sag geometry through
#         # support high-points and avoids losing valid conductor points near
#         # towers where a single polynomial can be off by >0.5 m.
#         ns = np.asarray(model["node_station"], dtype=np.float64)
#         nl = np.asarray(model["node_lateral"], dtype=np.float64)
#         nz = np.asarray(model["node_z"], dtype=np.float64)
#         oo = np.argsort(ns)
#         ns, nl, nz = ns[oo], nl[oo], nz[oo]
#         lat_pred = np.interp(station, ns, nl, left=nl[0], right=nl[-1])
#         z_pred = np.interp(station, ns, nz, left=nz[0], right=nz[-1])
#         in_span_context = (
#             station >= float(model["station_min"]) - ext
#         ) & (
#             station <= float(model["station_max"]) + ext
#         )

#         context_tube = (
#             raw
#             & wire_orientation
#             & in_span_context
#             & (np.abs(lateral - lat_pred) <= ctx_lat_tol)
#             & (np.abs(xyz[:, 2] - z_pred) <= ctx_z_tol)
#         )
#         active_tube = (
#             projection_base
#             & in_span_context
#             & (np.abs(lateral - lat_pred) <= act_lat_tol)
#             & (np.abs(xyz[:, 2] - z_pred) <= act_z_tol)
#         )
#         confirmed_context |= context_tube
#         projected_active |= active_tube

#         lat_coef = np.asarray(model["lat_coef"], dtype=np.float64)
#         z_coef = np.asarray(model["z_coef"], dtype=np.float64)
#         tracks_out.append(ConductorTrack(
#             track_id=int(tid),
#             station_center=float(model["station_center"]),
#             station_min=float(model["station_min"]),
#             station_max=float(model["station_max"]),
#             span=float(model["span"]),
#             nodes=int(model["nodes"]),
#             candidate_points=int(model["candidate_points"]),
#             coverage=float(model["coverage"]),
#             median_score=float(model["median_score"]),
#             lateral_slope=float(lat_coef[0]),
#             lateral_intercept=float(lat_coef[1]),
#             z_quadratic=float(z_coef[0]),
#             z_slope=float(z_coef[1]),
#             z_intercept=float(z_coef[2]),
#             lateral_residual_p90=float(model["lat_residual_p90"]),
#             z_residual_p90=float(model["z_residual_p90"]),
#         ))

#     # Confirmed raw context is the geometry anchor used by later pole phases.
#     # Projected active points may include points the broad Phase-2 candidate gate
#     # missed; those are the exact-fence rescue that fixes context-support -> zero
#     # write failures.
#     wire_support = confirmed_context | projected_active
#     projection_rescue = projected_active & (~raw)

#     return PowerPhase3Tracks(
#         version=PHASE3_VERSION,
#         point_count=n,
#         status="OK",
#         confirmed_context_mask=_freeze(confirmed_context.copy()),
#         projected_active_mask=_freeze(projected_active.copy()),
#         projection_rescue_mask=_freeze(projection_rescue.copy()),
#         wire_support_mask=_freeze(wire_support.copy()),
#         tracks=tuple(tracks_out),
#         semantic_gate=False,
#         exact_fence_projection=True,
#     )
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

import numpy as np
from scipy.spatial import cKDTree

from .power_phase1_state import PowerPhase1State
from .power_phase2_candidates import PowerPhase2Candidates


PHASE3_VERSION = "NAKSHA_POWER_PHASE3_CONDUCTOR_TRACKS_V3_25_SCALE_INVARIANT"


def _freeze(arr: np.ndarray) -> np.ndarray:
    arr.setflags(write=False)
    return arr


def _safe01(arr) -> np.ndarray:
    a = np.asarray(arr, dtype=np.float32).reshape(-1)
    return np.clip(np.nan_to_num(a, nan=0.0, posinf=1.0, neginf=0.0), 0.0, 1.0)


def _sample_centerline(coords: np.ndarray, step: float = 0.75):
    line = np.asarray(coords, dtype=np.float64)
    if line.ndim != 2 or line.shape[0] < 2 or line.shape[1] < 2:
        raise ValueError("Phase3 requires a valid CL polyline")

    sample_xy = []
    sample_station = []
    sample_normal = []
    cumulative = 0.0
    for a, b in zip(line[:-1, :2], line[1:, :2]):
        d = b - a
        seg = float(np.linalg.norm(d))
        if seg <= 1e-9:
            continue
        tangent = d / seg
        normal = np.array([-tangent[1], tangent[0]], dtype=np.float64)
        n = max(1, int(np.ceil(seg / max(float(step), 0.25))))
        # Avoid duplicating the first point of every segment except the first.
        t = np.linspace(0.0, 1.0, n + 1, endpoint=True)
        pts = a[None, :] + t[:, None] * d[None, :]
        sample_xy.append(pts)
        sample_station.append(cumulative + t * seg)
        sample_normal.append(np.repeat(normal[None, :], len(t), axis=0))
        cumulative += seg

    if not sample_xy:
        raise ValueError("Phase3 CL has no valid segments")
    return (
        np.vstack(sample_xy),
        np.concatenate(sample_station),
        np.vstack(sample_normal),
    )


def _signed_lateral(points_xy: np.ndarray, cl_coords: np.ndarray, step: float = 0.75):
    sample_xy, sample_station, sample_normal = _sample_centerline(cl_coords, step=step)
    tree = cKDTree(sample_xy)
    dist, nn = tree.query(np.asarray(points_xy, dtype=np.float64), k=1, workers=-1)
    nn = np.asarray(nn, dtype=np.int64)
    delta = np.asarray(points_xy, dtype=np.float64) - sample_xy[nn]
    lateral = np.sum(delta * sample_normal[nn], axis=1)
    station = sample_station[nn]
    del tree
    return (
        np.asarray(station, dtype=np.float64),
        np.asarray(lateral, dtype=np.float64),
        np.asarray(dist, dtype=np.float64),
    )


def _cluster_bin_nodes(
    idx: np.ndarray,
    station: np.ndarray,
    lateral: np.ndarray,
    z: np.ndarray,
    score: np.ndarray,
    *,
    lat_radius: float,
    z_radius: float,
    max_lat_span: float,
    max_z_span: float,
):
    """Create compact cross-section nodes inside one station bin.

    A conductor should appear as a very small cluster in lateral/Z space inside
    a short station slice. Vegetation can enter the Phase-2 raw cloud, but its
    local cross-section is usually much thicker and less coherent.
    """
    idx = np.asarray(idx, dtype=np.int64)
    if idx.size == 0:
        return []

    coords = np.column_stack((
        lateral[idx] / max(float(lat_radius), 1e-6),
        z[idx] / max(float(z_radius), 1e-6),
    ))
    tree = cKDTree(coords)
    pairs = tree.query_pairs(1.05, output_type="ndarray")
    del tree

    parent = np.arange(idx.size, dtype=np.int32)

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

    roots = np.array([find(i) for i in range(idx.size)], dtype=np.int32)
    _, labels = np.unique(roots, return_inverse=True)

    nodes = []
    for lab in range(int(labels.max()) + 1 if labels.size else 0):
        loc = np.flatnonzero(labels == lab)
        gi = idx[loc]
        if gi.size == 0:
            continue
        latv = lateral[gi]
        zv = z[gi]
        lat_span = float(np.percentile(latv, 90) - np.percentile(latv, 10)) if gi.size > 2 else float(np.ptp(latv))
        z_span = float(np.percentile(zv, 90) - np.percentile(zv, 10)) if gi.size > 2 else float(np.ptp(zv))
        if lat_span > float(max_lat_span) or z_span > float(max_z_span):
            continue
        nodes.append({
            "indices": gi,
            "station": float(np.median(station[gi])),
            "lateral": float(np.median(latv)),
            "z": float(np.median(zv)),
            "score": float(np.median(score[gi])),
            "points": int(gi.size),
            "lat_span": lat_span,
            "z_span": z_span,
        })
    return nodes


def _track_prediction(nodes, ds: float):
    last = nodes[-1]
    if len(nodes) < 2:
        return float(last["lateral"]), float(last["z"])
    prev = nodes[-2]
    denom = max(float(last["station"] - prev["station"]), 1e-6)
    lat_slope = np.clip((float(last["lateral"]) - float(prev["lateral"])) / denom, -0.25, 0.25)
    z_slope = np.clip((float(last["z"]) - float(prev["z"])) / denom, -0.65, 0.65)
    return (
        float(last["lateral"]) + float(lat_slope) * ds,
        float(last["z"]) + float(z_slope) * ds,
    )


def _long_track_local_quality(s, lat, z, weights, cfg: dict):
    """Validate a long conductor using overlapping short windows.

    The production regression this guards against is scale-dependent: a 15-25 m
    fence can fit one catenary cleanly, while a long fence can contain several
    catenary spans/support high-points.  A single global quadratic then rejects
    the real conductor even though every local section is physically smooth.

    We therefore judge long tracks at approximately the same local scale as the
    already-working short-fence case, while keeping the original global test for
    short tracks.  This does not relax point geometry; it changes only the scale
    at which longitudinal smoothness is validated.
    """
    order = np.argsort(s)
    s = np.asarray(s, dtype=np.float64)[order]
    lat = np.asarray(lat, dtype=np.float64)[order]
    z = np.asarray(z, dtype=np.float64)[order]
    weights = np.asarray(weights, dtype=np.float64)[order]

    if s.size < 6:
        return None

    window_m = max(12.0, float(cfg.get("phase3_long_track_window_m", 22.0)))
    stride_m = max(4.0, float(cfg.get("phase3_long_track_stride_m", 9.0)))
    min_nodes = max(4, int(cfg.get("phase3_long_track_min_window_nodes", 5)))
    min_window_span = float(cfg.get("phase3_long_track_min_window_span_m", 7.0))

    lat_lim = float(cfg.get("phase3_long_local_lateral_residual_p90", 0.48))
    z_lim = float(cfg.get("phase3_long_local_z_residual_p90", 0.72))
    slope_lim = float(cfg.get("phase3_long_local_max_lateral_slope", 0.22))
    quad_lim = float(cfg.get("phase3_long_local_max_abs_quadratic", 0.080))
    min_pass_fraction = float(cfg.get("phase3_long_track_min_pass_fraction", 0.65))

    smin = float(s[0])
    smax = float(s[-1])
    starts = list(np.arange(smin, max(smin, smax - window_m) + 1e-9, stride_m))
    last_start = max(smin, smax - window_m)
    if not starts or abs(starts[-1] - last_start) > 0.5 * stride_m:
        starts.append(last_start)

    records = []
    for a in starts:
        b = float(a) + window_m
        m = (s >= float(a)) & (s <= b)
        if int(np.count_nonzero(m)) < min_nodes:
            continue
        sw = s[m]
        lw = lat[m]
        zw = z[m]
        ww = weights[m]
        span = float(np.max(sw) - np.min(sw))
        if span < min_window_span:
            continue
        sc = float(np.median(sw))
        x = sw - sc
        try:
            lc = np.polyfit(x, lw, deg=1, w=np.sqrt(ww))
            zd = 2 if len(sw) >= 6 and span >= 10.0 else 1
            zc0 = np.polyfit(x, zw, deg=zd, w=np.sqrt(ww))
        except Exception:
            continue
        if len(zc0) == 2:
            zc = np.array([0.0, float(zc0[0]), float(zc0[1])], dtype=np.float64)
        else:
            zc = np.asarray(zc0, dtype=np.float64)
        lr = float(np.percentile(np.abs(lw - np.polyval(lc, x)), 90))
        zr = float(np.percentile(np.abs(zw - np.polyval(zc, x)), 90))
        ok = bool(
            lr <= lat_lim
            and zr <= z_lim
            and abs(float(lc[0])) <= slope_lim
            and abs(float(zc[0])) <= quad_lim
        )
        records.append({
            "center": sc,
            "lat_coef": np.asarray(lc, dtype=np.float64),
            "z_coef": zc,
            "lat_p90": lr,
            "z_p90": zr,
            "ok": ok,
        })

    if not records:
        return None
    passed = [r for r in records if r["ok"]]
    pass_fraction = float(len(passed) / len(records))
    if pass_fraction < min_pass_fraction:
        return None

    # Representative coefficients are taken from the passing local window
    # nearest the track midpoint.  Projection itself uses the node profile, so
    # these coefficients are only a stable summary for downstream bundle checks.
    target = float(np.median(s))
    rep = min(passed, key=lambda r: abs(float(r["center"]) - target))
    return {
        "pass_fraction": pass_fraction,
        "lat_p90": float(np.median([r["lat_p90"] for r in passed])),
        "z_p90": float(np.median([r["z_p90"] for r in passed])),
        "lat_coef": np.asarray(rep["lat_coef"], dtype=np.float64),
        "z_coef": np.asarray(rep["z_coef"], dtype=np.float64),
        "windows": int(len(records)),
        "windows_passed": int(len(passed)),
    }


def _fit_track_model(nodes, bin_size: float, cfg: dict):
    if not nodes:
        return None
    s = np.asarray([n["station"] for n in nodes], dtype=np.float64)
    lat = np.asarray([n["lateral"] for n in nodes], dtype=np.float64)
    z = np.asarray([n["z"] for n in nodes], dtype=np.float64)
    weights = np.asarray([max(1, int(n["points"])) for n in nodes], dtype=np.float64)
    scores = np.asarray([n["score"] for n in nodes], dtype=np.float64)

    span = float(np.max(s) - np.min(s)) if s.size else 0.0
    if s.size < int(cfg.get("phase3_min_nodes", 5)):
        return None
    if span < float(cfg.get("phase3_min_span_m", 10.0)):
        return None

    expected_bins = max(1.0, span / max(float(bin_size), 1e-6) + 1.0)
    coverage = float(len(np.unique(np.floor((s - np.min(s)) / max(float(bin_size), 1e-6)))) / expected_bins)
    if coverage < float(cfg.get("phase3_min_bin_coverage", 0.24)):
        return None
    if float(np.median(scores)) < float(cfg.get("phase3_min_median_score", 0.50)):
        return None

    sc = float(np.median(s))
    x = s - sc
    try:
        lat_coef = np.polyfit(x, lat, deg=1, w=np.sqrt(weights))
        z_deg = 2 if len(nodes) >= 6 and span >= 12.0 else 1
        z_coef0 = np.polyfit(x, z, deg=z_deg, w=np.sqrt(weights))
    except Exception:
        return None

    if len(z_coef0) == 2:
        z_quad = np.array([0.0, float(z_coef0[0]), float(z_coef0[1])], dtype=np.float64)
    else:
        z_quad = np.asarray(z_coef0, dtype=np.float64)

    long_trigger = float(cfg.get("phase3_long_track_trigger_m", 28.0))
    if span >= long_trigger:
        # Long selections are validated as overlapping short sections.  This is
        # the key scale-invariance fix: the exact geometry that succeeds in a
        # short fence is not rejected merely because more spans are selected.
        local = _long_track_local_quality(s, lat, z, weights, cfg)
        if local is None:
            return None
        lat_coef = np.asarray(local["lat_coef"], dtype=np.float64)
        z_quad = np.asarray(local["z_coef"], dtype=np.float64)
        lat_p90 = float(local["lat_p90"])
        z_p90 = float(local["z_p90"])
        if float(np.median(scores)) < float(cfg.get("phase3_long_min_median_score", 0.60)):
            return None
    else:
        lat_pred = np.polyval(lat_coef, x)
        z_pred = np.polyval(z_quad, x)
        lat_res = np.abs(lat - lat_pred)
        z_res = np.abs(z - z_pred)
        lat_p90 = float(np.percentile(lat_res, 90))
        z_p90 = float(np.percentile(z_res, 90))

        if lat_p90 > float(cfg.get("phase3_max_lateral_residual_p90", 0.55)):
            return None
        if z_p90 > float(cfg.get("phase3_max_z_residual_p90", 0.75)):
            return None

        lat_slope = float(lat_coef[0])
        if abs(lat_slope) > float(cfg.get("phase3_max_lateral_slope", 0.18)):
            return None
        if abs(float(z_quad[0])) > float(cfg.get("phase3_max_abs_quadratic", 0.035)):
            return None

    point_count = int(sum(int(n["points"]) for n in nodes))
    if point_count < int(cfg.get("phase3_min_candidate_points", 14)):
        return None

    return {
        "station_center": sc,
        "station_min": float(np.min(s)),
        "node_station": s.copy(),
        "node_lateral": lat.copy(),
        "node_z": z.copy(),
        "station_max": float(np.max(s)),
        "span": span,
        "nodes": int(len(nodes)),
        "candidate_points": point_count,
        "coverage": coverage,
        "median_score": float(np.median(scores)),
        "lat_coef": np.asarray(lat_coef, dtype=np.float64),
        "z_coef": np.asarray(z_quad, dtype=np.float64),
        "lat_residual_p90": lat_p90,
        "z_residual_p90": z_p90,
    }


@dataclass(frozen=True)
class ConductorTrack:
    track_id: int
    station_center: float
    station_min: float
    station_max: float
    span: float
    nodes: int
    candidate_points: int
    coverage: float
    median_score: float
    lateral_slope: float
    lateral_intercept: float
    z_quadratic: float
    z_slope: float
    z_intercept: float
    lateral_residual_p90: float
    z_residual_p90: float

    def report(self) -> dict:
        return {
            "track_id": int(self.track_id),
            "station_min": float(self.station_min),
            "station_max": float(self.station_max),
            "span_m": float(self.span),
            "nodes": int(self.nodes),
            "candidate_points": int(self.candidate_points),
            "coverage": float(self.coverage),
            "median_score": float(self.median_score),
            "lateral_residual_p90": float(self.lateral_residual_p90),
            "z_residual_p90": float(self.z_residual_p90),
        }


@dataclass(frozen=True)
class PowerPhase3Tracks:
    version: str
    point_count: int
    status: str
    confirmed_context_mask: np.ndarray
    projected_active_mask: np.ndarray
    projection_rescue_mask: np.ndarray
    wire_support_mask: np.ndarray
    tracks: Tuple[ConductorTrack, ...]
    semantic_gate: bool
    exact_fence_projection: bool

    def report(self) -> dict:
        return {
            "version": self.version,
            "status": self.status,
            "semantic_gate": bool(self.semantic_gate),
            "classification_writes_in_module": False,
            "exact_fence_projection": bool(self.exact_fence_projection),
            "tracks": int(len(self.tracks)),
            "confirmed_context_points": int(np.count_nonzero(self.confirmed_context_mask)),
            "projected_active_points": int(np.count_nonzero(self.projected_active_mask)),
            "projection_rescue_points": int(np.count_nonzero(self.projection_rescue_mask)),
            "wire_support_points": int(np.count_nonzero(self.wire_support_mask)),
            "track_reports": [t.report() for t in self.tracks],
            "immutable": bool(
                not self.confirmed_context_mask.flags.writeable
                and not self.projected_active_mask.flags.writeable
                and not self.wire_support_mask.flags.writeable
            ),
        }


def _empty_state(n: int, status: str) -> PowerPhase3Tracks:
    z = np.zeros(int(n), dtype=bool)
    return PowerPhase3Tracks(
        version=PHASE3_VERSION,
        point_count=int(n),
        status=str(status),
        confirmed_context_mask=_freeze(z.copy()),
        projected_active_mask=_freeze(z.copy()),
        projection_rescue_mask=_freeze(z.copy()),
        wire_support_mask=_freeze(z.copy()),
        tracks=tuple(),
        semantic_gate=False,
        exact_fence_projection=True,
    )


def build_power_phase3_tracks(
    xyz,
    hag,
    phase1: PowerPhase1State,
    phase2: PowerPhase2Candidates,
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
) -> PowerPhase3Tracks:
    """Fit conductor tracks in full context and project them onto exact fence.

    Phase 3 is the first Power phase that creates a *confirmed conductor model*.
    It does not inspect the base semantic class as an inclusion gate. Context
    points can establish a track across the fence boundary; only the exact
    Phase-1 active mask is returned for classification write-back.
    """
    cfg = dict(config or {})
    xyz = np.asarray(xyz, dtype=np.float64)
    hag = np.asarray(hag, dtype=np.float32).reshape(-1)
    n = len(hag)
    if xyz.ndim != 2 or xyz.shape[1] < 3 or len(xyz) != n:
        raise ValueError("Phase3 xyz/hag length mismatch")
    if phase1.point_count != n or phase2.point_count != n:
        raise ValueError("Phase3 Phase1/Phase2 length mismatch")
    if cl_coords is None or float(cl_width or 0.0) <= 0.0:
        return _empty_state(n, "SKIPPED_NO_CL")

    raw = np.asarray(phase2.raw_wire_mask, dtype=bool)
    candidate_idx = np.flatnonzero(raw)
    if candidate_idx.size < int(cfg.get("phase3_min_raw_candidates", 20)):
        return _empty_state(n, "SKIPPED_TOO_FEW_RAW_WIRE_CANDIDATES")

    l05 = _safe01(linearity_05)
    l10 = _safe01(linearity_10)
    p05 = _safe01(planarity_05)
    v05 = _safe01(verticality_05)
    v10 = _safe01(verticality_10)
    if len(l05) != n or len(l10) != n or len(p05) != n or len(v05) != n or len(v10) != n:
        raise ValueError("Phase3 geometry length mismatch")
    line_strength = np.maximum(l05, l10)
    vert_strength = np.maximum(v05, v10)

    # Reuse Phase-2 station/distance when available, but compute signed lateral
    # from the actual CL so parallel conductors on opposite sides are separated.
    station_from_cl, lateral, distance_from_cl = _signed_lateral(
        xyz[:, :2],
        np.asarray(cl_coords, dtype=np.float64),
        step=float(cfg.get("phase3_cl_sample_step_m", 0.75)),
    )
    if cl_station is not None:
        station = np.asarray(cl_station, dtype=np.float64).reshape(-1)
        if len(station) != n:
            raise ValueError("Phase3 cl_station length mismatch")
        station = np.where(np.isfinite(station), station, station_from_cl)
    else:
        station = station_from_cl
    if cl_dist is not None:
        cdist = np.asarray(cl_dist, dtype=np.float64).reshape(-1)
        if len(cdist) != n:
            raise ValueError("Phase3 cl_dist length mismatch")
        cdist = np.where(np.isfinite(cdist), cdist, distance_from_cl)
    else:
        cdist = distance_from_cl

    finite = np.isfinite(station) & np.isfinite(lateral) & np.isfinite(xyz[:, 2]) & np.isfinite(hag)
    score = np.asarray(phase2.wire_score, dtype=np.float32)
    seed_min = float(cfg.get("phase3_seed_score_min", 0.36))
    fit_mask = raw & finite & (score >= seed_min)
    fit_idx = np.flatnonzero(fit_mask)
    if fit_idx.size < int(cfg.get("phase3_min_raw_candidates", 20)):
        return _empty_state(n, "SKIPPED_TOO_FEW_SEED_CANDIDATES")

    bin_size = float(cfg.get("phase3_station_bin_m", 1.0))
    fit_station_span = float(np.nanmax(station[fit_idx]) - np.nanmin(station[fit_idx])) if fit_idx.size else 0.0
    long_scene = fit_station_span >= float(cfg.get("phase3_long_track_trigger_m", 28.0))
    s0 = float(np.nanmin(station[fit_idx]))
    bin_id = np.floor((station[fit_idx] - s0) / max(bin_size, 1e-6)).astype(np.int64)
    order = np.argsort(bin_id, kind="stable")
    sorted_idx = fit_idx[order]
    sorted_bins = bin_id[order]
    uniq_bins, starts, counts = np.unique(sorted_bins, return_index=True, return_counts=True)

    nodes_by_bin = []
    all_nodes = []
    for b, st0, cnt in zip(uniq_bins, starts, counts):
        gi = sorted_idx[st0:st0 + cnt]
        nodes = _cluster_bin_nodes(
            gi, station, lateral, xyz[:, 2], score,
            lat_radius=float(cfg.get("phase3_node_lateral_radius_m", 0.48)),
            z_radius=float(cfg.get("phase3_node_z_radius_m", 0.48)),
            max_lat_span=float(cfg.get("phase3_node_max_lateral_span_m", 0.85)),
            max_z_span=float(cfg.get("phase3_node_max_z_span_m", 0.85)),
        )
        # Large fences contain far more branch/vegetation micro-clusters per
        # station bin than short fences. Keep a generous number of the most
        # conductor-like compact nodes so spurious clusters cannot hijack the
        # greedy association simply because the selection is longer.
        max_nodes_bin = max(8, int(cfg.get("phase3_max_nodes_per_bin", 24)))
        if len(nodes) > max_nodes_bin:
            def _node_quality(nn):
                compact_pen = 0.10 * float(nn.get("lat_span", 0.0)) + 0.10 * float(nn.get("z_span", 0.0))
                return float(nn.get("score", 0.0)) + 0.035 * np.log1p(max(1, int(nn.get("points", 1)))) - compact_pen
            nodes = sorted(nodes, key=_node_quality, reverse=True)[:max_nodes_bin]
        for node in nodes:
            node["bin"] = int(b)
            node["node_id"] = int(len(all_nodes))
            all_nodes.append(node)
        if nodes:
            nodes_by_bin.append((int(b), nodes))

    if len(all_nodes) < int(cfg.get("phase3_min_nodes", 5)):
        return _empty_state(n, "SKIPPED_TOO_FEW_NODES")

    # Greedy one-to-one temporal association in station order. Prediction from
    # the previous two nodes tolerates conductor sag while strongly penalising
    # lateral/Z jumps typical of foliage.
    tracks = []
    max_gap = float(cfg.get("phase3_link_max_station_gap_m", 4.5))
    established_gap = (
        max(max_gap, float(cfg.get("phase3_link_established_max_station_gap_m", 7.5)))
        if long_scene else max_gap
    )
    bridge_score_min = float(cfg.get("phase3_link_gap_bridge_score_min", 0.58))
    score_cost_weight = float(cfg.get("phase3_link_score_cost_weight", 0.28))
    score_lock_min = float(cfg.get("phase3_link_score_lock_min", 0.64))
    score_drop_max = float(cfg.get("phase3_link_score_drop_max", 0.16))
    lat_tol = float(cfg.get("phase3_link_lateral_tolerance_m", 0.80))
    z_tol = float(cfg.get("phase3_link_z_tolerance_m", 1.25))

    for _, nodes in nodes_by_bin:
        # Reset per-bin assignment state before building candidate links.
        for tr in tracks:
            tr["assigned_this_bin"] = False
        possible = []
        for ti, tr in enumerate(tracks):
            last = tr["nodes"][-1]
            for ni, node in enumerate(nodes):
                ds = float(node["station"] - last["station"])
                if ds <= 0.05:
                    continue
                # Once a track is established, permit a small sparse-data bridge
                # only to a strong Phase-2 node. Short-fence behaviour is
                # unchanged because normal adjacent links still use max_gap.
                gap_limit = established_gap if len(tr["nodes"]) >= 4 else max_gap
                if ds > gap_limit:
                    continue
                node_score = float(node.get("score", 0.0))
                if ds > max_gap and node_score < bridge_score_min:
                    continue
                if long_scene and len(tr["nodes"]) >= 4:
                    hist_score = float(np.median([float(x.get("score", 0.0)) for x in tr["nodes"][-6:]]))
                    # Prevent a clean conductor from being hijacked by a lower-
                    # quality branch node in a long/noisy selection. Weak tracks
                    # are not given this privilege, so this is not a new way for
                    # vegetation to grow into Wire.
                    if hist_score >= score_lock_min and node_score < hist_score - score_drop_max:
                        continue
                plat, pz = _track_prediction(tr["nodes"], ds)
                dlat = abs(float(node["lateral"]) - plat)
                dz = abs(float(node["z"]) - pz)
                if dlat <= lat_tol and dz <= z_tol:
                    gap_pen = 0.12 * max(0.0, ds - bin_size) / max(gap_limit, 1e-6)
                    score_pen = score_cost_weight * (1.0 - float(np.clip(node_score, 0.0, 1.0)))
                    cost = (dlat / max(lat_tol, 1e-6)) ** 2 + (dz / max(z_tol, 1e-6)) ** 2 + gap_pen + score_pen
                    possible.append((float(cost), ti, ni))
        used_t = set()
        used_n = set()
        for _, ti, ni in sorted(possible, key=lambda x: x[0]):
            if ti in used_t or ni in used_n:
                continue
            tracks[ti]["nodes"].append(nodes[ni])
            tracks[ti]["assigned_this_bin"] = True
            used_t.add(ti)
            used_n.add(ni)
        for ni, node in enumerate(nodes):
            if ni not in used_n:
                tracks.append({"nodes": [node], "assigned_this_bin": True})

    accepted_models = []
    accepted_nodes = []
    for tr in tracks:
        model = _fit_track_model(tr["nodes"], bin_size, cfg)
        if model is not None:
            accepted_models.append(model)
            accepted_nodes.append(tr["nodes"])

    if not accepted_models:
        return _empty_state(n, "NO_ACCEPTED_TRACKS")

    confirmed_context = np.zeros(n, dtype=bool)
    projected_active = np.zeros(n, dtype=bool)
    active = np.asarray(phase1.active_mask, dtype=bool)
    protected = np.asarray(phase1.protected_mask, dtype=bool)
    strict_corridor = finite & (cdist <= float(cl_width) + float(cfg.get("phase3_projection_cl_slack_m", 0.20)))
    wire_orientation = (
        (vert_strength <= float(cfg.get("phase3_projection_verticality_max", 0.58)))
        | (
            (line_strength >= float(cfg.get("phase3_projection_vertical_high_linearity", 0.86)))
            & (p05 <= float(cfg.get("phase3_projection_vertical_high_planarity", 0.16)))
        )
    )
    projection_geom = (
        raw | (
            (line_strength >= float(cfg.get("phase3_projection_linearity_min", 0.30)))
            & (p05 <= float(cfg.get("phase3_projection_planarity_max", 0.68)))
        )
    ) & wire_orientation
    projection_base = (
        active
        & (~protected)
        & strict_corridor
        & projection_geom
        & (hag >= float(cfg.get("phase3_projection_hag_min", 2.0)))
        & (hag <= float(cfg.get("phase3_projection_hag_max", 90.0)))
    )

    tracks_out = []
    ctx_lat_tol = float(cfg.get("phase3_context_tube_lateral_m", 0.52))
    ctx_z_tol = float(cfg.get("phase3_context_tube_z_m", 0.52))
    act_lat_tol = float(cfg.get("phase3_projection_lateral_m", 0.42))
    act_z_tol = float(cfg.get("phase3_projection_z_m", 0.42))
    ext = float(cfg.get("phase3_projection_station_extension_m", 2.5))

    for tid, model in enumerate(accepted_models):
        x = station - float(model["station_center"])
        # Projection uses the accepted node profile itself rather than one
        # global quadratic. This follows real catenary/sag geometry through
        # support high-points and avoids losing valid conductor points near
        # towers where a single polynomial can be off by >0.5 m.
        ns = np.asarray(model["node_station"], dtype=np.float64)
        nl = np.asarray(model["node_lateral"], dtype=np.float64)
        nz = np.asarray(model["node_z"], dtype=np.float64)
        oo = np.argsort(ns)
        ns, nl, nz = ns[oo], nl[oo], nz[oo]
        lat_pred = np.interp(station, ns, nl, left=nl[0], right=nl[-1])
        z_pred = np.interp(station, ns, nz, left=nz[0], right=nz[-1])
        in_span_context = (
            station >= float(model["station_min"]) - ext
        ) & (
            station <= float(model["station_max"]) + ext
        )

        context_tube = (
            raw
            & wire_orientation
            & in_span_context
            & (np.abs(lateral - lat_pred) <= ctx_lat_tol)
            & (np.abs(xyz[:, 2] - z_pred) <= ctx_z_tol)
        )
        active_tube = (
            projection_base
            & in_span_context
            & (np.abs(lateral - lat_pred) <= act_lat_tol)
            & (np.abs(xyz[:, 2] - z_pred) <= act_z_tol)
        )
        confirmed_context |= context_tube
        projected_active |= active_tube

        lat_coef = np.asarray(model["lat_coef"], dtype=np.float64)
        z_coef = np.asarray(model["z_coef"], dtype=np.float64)
        tracks_out.append(ConductorTrack(
            track_id=int(tid),
            station_center=float(model["station_center"]),
            station_min=float(model["station_min"]),
            station_max=float(model["station_max"]),
            span=float(model["span"]),
            nodes=int(model["nodes"]),
            candidate_points=int(model["candidate_points"]),
            coverage=float(model["coverage"]),
            median_score=float(model["median_score"]),
            lateral_slope=float(lat_coef[0]),
            lateral_intercept=float(lat_coef[1]),
            z_quadratic=float(z_coef[0]),
            z_slope=float(z_coef[1]),
            z_intercept=float(z_coef[2]),
            lateral_residual_p90=float(model["lat_residual_p90"]),
            z_residual_p90=float(model["z_residual_p90"]),
        ))

    # Confirmed raw context is the geometry anchor used by later pole phases.
    # Projected active points may include points the broad Phase-2 candidate gate
    # missed; those are the exact-fence rescue that fixes context-support -> zero
    # write failures.
    wire_support = confirmed_context | projected_active
    projection_rescue = projected_active & (~raw)

    return PowerPhase3Tracks(
        version=PHASE3_VERSION,
        point_count=n,
        status="OK",
        confirmed_context_mask=_freeze(confirmed_context.copy()),
        projected_active_mask=_freeze(projected_active.copy()),
        projection_rescue_mask=_freeze(projection_rescue.copy()),
        wire_support_mask=_freeze(wire_support.copy()),
        tracks=tuple(tracks_out),
        semantic_gate=False,
        exact_fence_projection=True,
    )
