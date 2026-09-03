# from __future__ import annotations

# from dataclasses import dataclass
# from typing import Mapping, Optional, Tuple

# import numpy as np
# from scipy.spatial import cKDTree

# from .power_phase1_state import PowerPhase1State
# from .power_phase2_candidates import PowerPhase2Candidates
# from .power_phase3_tracks import PowerPhase3Tracks


# PHASE5_VERSION = "NAKSHA_POWER_PHASE5_CONDUCTOR_ANCHORED_SUPPORTS_V3_11"


# def _freeze(arr: np.ndarray) -> np.ndarray:
#     arr.setflags(write=False)
#     return arr


# def _safe01(arr) -> np.ndarray:
#     a = np.asarray(arr, dtype=np.float32).reshape(-1)
#     return np.clip(np.nan_to_num(a, nan=0.0, posinf=1.0, neginf=0.0), 0.0, 1.0)


# @dataclass(frozen=True)
# class SupportAnchor:
#     station: float
#     source: str
#     track_count: int
#     track_ids: Tuple[int, ...]
#     prominence: float = 0.0

#     def report(self) -> dict:
#         return {
#             "station": float(self.station),
#             "source": str(self.source),
#             "track_count": int(self.track_count),
#             "track_ids": [int(x) for x in self.track_ids],
#             "prominence": float(self.prominence),
#         }


# @dataclass(frozen=True)
# class PowerPhase5Supports:
#     version: str
#     point_count: int
#     status: str
#     support_context_mask: np.ndarray
#     support_active_mask: np.ndarray
#     anchors: Tuple[SupportAnchor, ...]
#     diagnostics: Tuple[Mapping[str, object], ...]
#     exact_fence_only: bool
#     conductor_anchored: bool

#     def report(self) -> dict:
#         accepted = [d for d in self.diagnostics if bool(d.get("accepted", False))]
#         return {
#             "version": self.version,
#             "status": self.status,
#             "classification_writes_in_module": False,
#             "conductor_anchored": bool(self.conductor_anchored),
#             "exact_fence_only": bool(self.exact_fence_only),
#             "anchors": int(len(self.anchors)),
#             "accepted_supports": int(len(accepted)),
#             "support_context_points": int(np.count_nonzero(self.support_context_mask)),
#             "support_active_points": int(np.count_nonzero(self.support_active_mask)),
#             "anchor_reports": [a.report() for a in self.anchors],
#             "support_reports": [dict(d) for d in self.diagnostics],
#             "immutable": bool(
#                 not self.support_context_mask.flags.writeable
#                 and not self.support_active_mask.flags.writeable
#             ),
#         }


# def _empty_state(n: int, status: str) -> PowerPhase5Supports:
#     z = np.zeros(int(n), dtype=bool)
#     return PowerPhase5Supports(
#         version=PHASE5_VERSION,
#         point_count=int(n),
#         status=str(status),
#         support_context_mask=_freeze(z.copy()),
#         support_active_mask=_freeze(z.copy()),
#         anchors=tuple(),
#         diagnostics=tuple(),
#         exact_fence_only=True,
#         conductor_anchored=True,
#     )


# def _cluster_track_endpoints(tracks, *, gap_m: float, min_tracks: int):
#     points = []
#     for t in tracks:
#         points.append((float(t.station_min), int(t.track_id), "min"))
#         points.append((float(t.station_max), int(t.track_id), "max"))
#     if not points:
#         return []
#     points.sort(key=lambda x: x[0])
#     groups = [[points[0]]]
#     for p in points[1:]:
#         if float(p[0] - groups[-1][-1][0]) <= float(gap_m):
#             groups[-1].append(p)
#         else:
#             groups.append([p])

#     anchors = []
#     for g in groups:
#         ids = tuple(sorted({int(x[1]) for x in g}))
#         if len(ids) < int(min_tracks):
#             continue
#         # Multiple endpoints from one physical support may be offset by one or
#         # two station bins. Median is stable and keeps the search centred.
#         anchors.append(SupportAnchor(
#             station=float(np.median([x[0] for x in g])),
#             source="track_endpoint_cluster",
#             track_count=len(ids),
#             track_ids=ids,
#             prominence=0.0,
#         ))
#     return anchors


# def _interior_wire_profile_anchors(
#     xyz: np.ndarray,
#     wire_mask: np.ndarray,
#     station: np.ndarray,
#     *,
#     track_count: int,
#     config: dict,
# ):
#     """Conservative interior support proposals from conductor sag high-points.

#     Endpoint clusters handle the common fence/tower case. This second path
#     covers a support inside a longer fitted span. It never classifies points;
#     it only proposes a station that still has to pass the structural support
#     validation below.
#     """
#     wi = np.flatnonzero(np.asarray(wire_mask, dtype=bool) & np.isfinite(station))
#     if wi.size < 40:
#         return []
#     st = station[wi]
#     z = xyz[wi, 2]
#     bin_m = float(config.get("phase5_profile_bin_m", 1.0))
#     s0 = float(np.min(st))
#     bins = np.floor((st - s0) / max(bin_m, 0.5)).astype(np.int64)
#     nbin = int(np.max(bins)) + 1 if bins.size else 0
#     if nbin < 10:
#         return []
#     count = np.bincount(bins, minlength=nbin)
#     prof = np.full(nbin, np.nan, dtype=np.float64)
#     ps = s0 + (np.arange(nbin, dtype=np.float64) + 0.5) * bin_m
#     order = np.argsort(bins, kind="stable")
#     starts = np.cumsum(np.r_[0, count[:-1]])
#     min_pts = int(config.get("phase5_profile_min_wire_points", 6))
#     for b in np.flatnonzero(count >= min_pts):
#         loc = order[starts[b]:starts[b] + count[b]]
#         prof[b] = float(np.median(z[loc]))
#     if np.count_nonzero(np.isfinite(prof)) < 8:
#         return []

#     near_m = float(config.get("phase5_profile_side_near_m", 5.0))
#     far_m = float(config.get("phase5_profile_side_far_m", 16.0))
#     min_prom = float(config.get("phase5_profile_min_prominence_m", 0.20))
#     candidates = []
#     for i in np.flatnonzero(np.isfinite(prof)):
#         si = ps[i]
#         left = (ps <= si - near_m) & (ps >= si - far_m) & np.isfinite(prof)
#         right = (ps >= si + near_m) & (ps <= si + far_m) & np.isfinite(prof)
#         if not np.any(left) or not np.any(right):
#             continue
#         lz = float(np.median(prof[left]))
#         rz = float(np.median(prof[right]))
#         expected = 0.5 * (lz + rz)
#         prom = float(prof[i] - expected)
#         if prom < min_prom:
#             continue
#         # Require a local high point, not a monotonically sloping profile.
#         hw = max(2, int(round(float(config.get("phase5_profile_peak_halfwidth_m", 3.0)) / bin_m)))
#         a = max(0, i - hw)
#         b = min(nbin, i + hw + 1)
#         pv = prof[a:b]
#         pv = pv[np.isfinite(pv)]
#         if pv.size and float(prof[i]) < float(np.max(pv)) - 0.05:
#             continue
#         candidates.append(SupportAnchor(
#             station=float(si),
#             source="interior_sag_highpoint",
#             track_count=int(track_count),
#             track_ids=tuple(range(int(track_count))),
#             prominence=prom,
#         ))

#     # Station-space NMS.
#     sep = float(config.get("phase5_anchor_min_separation_m", 6.0))
#     candidates.sort(key=lambda a: a.prominence, reverse=True)
#     kept = []
#     for a in candidates:
#         if all(abs(a.station - b.station) >= sep for b in kept):
#             kept.append(a)
#     return sorted(kept, key=lambda a: a.station)


# def _merge_anchors(anchors, *, separation_m: float):
#     if not anchors:
#         return []
#     anchors = sorted(anchors, key=lambda a: (a.station, -a.track_count, -a.prominence))
#     groups = [[anchors[0]]]
#     for a in anchors[1:]:
#         if abs(a.station - groups[-1][-1].station) <= float(separation_m):
#             groups[-1].append(a)
#         else:
#             groups.append([a])
#     out = []
#     for g in groups:
#         # Endpoint evidence outranks a profile-only proposal at the same site.
#         g2 = sorted(g, key=lambda a: (a.source != "track_endpoint_cluster", -a.track_count, -a.prominence))
#         best = g2[0]
#         ids = tuple(sorted({tid for a in g for tid in a.track_ids}))
#         out.append(SupportAnchor(
#             station=float(np.median([a.station for a in g])),
#             source=best.source,
#             track_count=max(int(best.track_count), len(ids)),
#             track_ids=ids if ids else best.track_ids,
#             prominence=max(float(a.prominence) for a in g),
#         ))
#     return out


# def _cell_structural_growth(
#     xyz: np.ndarray,
#     hag: np.ndarray,
#     local_idx: np.ndarray,
#     wire_idx: np.ndarray,
#     anchor_xy: np.ndarray,
#     wire_z_low: float,
#     *,
#     vstrength: np.ndarray,
#     lstrength: np.ndarray,
#     pole_score: np.ndarray,
#     config: dict,
# ):
#     """Grow a conductor-attached vertical/lattice support through XY cells."""
#     if local_idx.size < 8 or wire_idx.size < 4:
#         return np.empty(0, dtype=np.int64), {"cells": 0, "seed_cells": 0}

#     cell_size = float(config.get("phase5_cell_size_m", 0.80))
#     cell_size = min(max(cell_size, 0.55), 1.10)
#     cells = np.floor((xyz[local_idx, :2] - anchor_xy[None, :]) / cell_size).astype(np.int64)
#     uc, inv = np.unique(cells, axis=0, return_inverse=True)
#     counts = np.bincount(inv, minlength=len(uc))
#     order = np.argsort(inv, kind="stable")
#     starts = np.cumsum(np.r_[0, counts[:-1]])

#     wtree = cKDTree(xyz[wire_idx, :2])
#     cell_info = []
#     cell_points = {}
#     try:
#         for cid in range(len(uc)):
#             cnt = int(counts[cid])
#             if cnt < int(config.get("phase5_cell_min_points", 3)):
#                 continue
#             pos = order[starts[cid]:starts[cid] + cnt]
#             gi = local_idx[pos]
#             pts = xyz[gi]
#             qz = np.percentile(pts[:, 2], [5.0, 95.0])
#             qh = np.percentile(hag[gi], [10.0, 90.0])
#             hspan = float(max(qz[1] - qz[0], qh[1] - qh[0]))
#             if hspan < float(config.get("phase5_cell_min_height_m", 1.3)):
#                 continue
#             z0 = float(np.min(pts[:, 2]))
#             zb = np.floor((pts[:, 2] - z0) / float(config.get("phase5_cell_z_bin_m", 0.9))).astype(np.int32)
#             nbins = int(len(np.unique(zb)))
#             if nbins < int(config.get("phase5_cell_min_bins", 2)):
#                 continue
#             vf = float(np.mean(vstrength[gi] >= float(config.get("phase5_cell_vert_threshold", 0.14))))
#             lf = float(np.mean(lstrength[gi] >= float(config.get("phase5_cell_lin_threshold", 0.26))))
#             ps = float(np.median(pole_score[gi]))
#             structural = (
#                 (vf >= float(config.get("phase5_cell_min_vert_fraction", 0.12)) and
#                  lf >= float(config.get("phase5_cell_min_lin_fraction", 0.10)))
#                 or lf >= float(config.get("phase5_cell_strong_lin_fraction", 0.34))
#                 or ps >= float(config.get("phase5_cell_min_pole_score", 0.25))
#             )
#             if not structural:
#                 continue

#             center = np.median(pts[:, :2], axis=0)
#             center_dist = float(np.linalg.norm(center - anchor_xy))
#             top_mask = pts[:, 2] >= np.percentile(pts[:, 2], 65.0)
#             top_pts = pts[top_mask]
#             attach = 0
#             if len(top_pts):
#                 dd, nn = wtree.query(top_pts[:, :2], k=1, workers=-1)
#                 wz = xyz[wire_idx[np.asarray(nn, dtype=np.int64)], 2]
#                 attach = int(np.count_nonzero(
#                     (np.asarray(dd) <= float(config.get("phase5_top_link_xy_m", 4.8)))
#                     & (np.abs(top_pts[:, 2] - wz) <= float(config.get("phase5_top_link_dz_m", 4.8)))
#                 ))
#             seed = bool(
#                 attach >= int(config.get("phase5_seed_min_top_links", 1))
#                 or (
#                     float(qz[1]) >= float(wire_z_low) - float(config.get("phase5_seed_top_gap_m", 3.8))
#                     and center_dist <= float(config.get("phase5_seed_radius_m", 5.8))
#                 )
#             )
#             cell_info.append({
#                 "cid": int(cid), "center": np.asarray(center, dtype=np.float64),
#                 "vf": vf, "lf": lf, "pole_score": ps, "height": hspan,
#                 "attach": int(attach), "seed": seed,
#                 "base": float(qh[0]) <= float(config.get("phase5_cell_base_hag_max", 6.0)),
#                 "top_z": float(qz[1]), "center_dist": center_dist,
#             })
#             cell_points[int(cid)] = gi
#     finally:
#         del wtree

#     if not cell_info:
#         return np.empty(0, dtype=np.int64), {"cells": 0, "seed_cells": 0}

#     ids = np.asarray([c["cid"] for c in cell_info], dtype=np.int64)
#     centers = np.vstack([c["center"] for c in cell_info])
#     seeds = np.flatnonzero(np.asarray([bool(c["seed"]) for c in cell_info], dtype=bool))
#     if seeds.size == 0:
#         return np.empty(0, dtype=np.int64), {"cells": len(cell_info), "seed_cells": 0}

#     # Grow through nearby structural XY columns. This keeps a lattice body and
#     # its legs together while preventing a distant vegetation patch inside the
#     # same broad CL corridor from being absorbed.
#     tree = cKDTree(centers)
#     pairs = tree.query_pairs(float(config.get("phase5_cell_connect_radius_m", 2.25)), output_type="ndarray")
#     del tree
#     parent = np.arange(len(cell_info), dtype=np.int32)

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

#     seed_roots = {find(int(s)) for s in seeds}
#     selected_pos = [i for i in range(len(cell_info)) if find(i) in seed_roots]

#     # Companion-leg rescue: wide lattice towers can have legs separated by a
#     # small gap from the top seed component in sparse data. Include only very
#     # structural base-reaching cells near the anchor; this is far safer than
#     # widening the old whole-component threshold.
#     companion_radius = float(config.get("phase5_companion_radius_m", 6.8))
#     for i, c in enumerate(cell_info):
#         if i in selected_pos:
#             continue
#         if c["center_dist"] > companion_radius or not c["base"]:
#             continue
#         strong = (
#             c["vf"] >= float(config.get("phase5_companion_min_vert_fraction", 0.30))
#             or c["lf"] >= float(config.get("phase5_companion_min_lin_fraction", 0.42))
#             or (c["vf"] >= 0.20 and c["lf"] >= 0.24 and c["height"] >= 3.0)
#         )
#         if strong:
#             selected_pos.append(i)

#     if not selected_pos:
#         return np.empty(0, dtype=np.int64), {"cells": len(cell_info), "seed_cells": int(seeds.size)}

#     selected_ids = {int(ids[i]) for i in selected_pos}
#     gi = np.unique(np.concatenate([cell_points[cid] for cid in selected_ids])).astype(np.int64)

#     # Point-level containment. Retain lattice/vertical/diagonal support points,
#     # not foliage that merely shares one selected XY cell.
#     point_ok = (
#         ((vstrength[gi] >= float(config.get("phase5_write_vert_min", 0.28)))
#          & (lstrength[gi] >= float(config.get("phase5_write_lin_min", 0.34))))
#         | (vstrength[gi] >= float(config.get("phase5_write_strong_vert", 0.55)))
#         | (lstrength[gi] >= float(config.get("phase5_write_strong_lin", 0.65)))
#         | (pole_score[gi] >= float(config.get("phase5_write_pole_score", 0.50)))
#     )
#     gi = gi[point_ok]
#     return gi, {
#         "cells": int(len(cell_info)),
#         "seed_cells": int(seeds.size),
#         "selected_cells": int(len(selected_ids)),
#     }


# def build_power_phase5_supports(
#     xyz,
#     hag,
#     phase1: PowerPhase1State,
#     phase2: PowerPhase2Candidates,
#     phase3: PowerPhase3Tracks,
#     *,
#     linearity_05,
#     planarity_05,
#     verticality_05,
#     linearity_10,
#     verticality_10,
#     cl_station: Optional[np.ndarray],
#     cl_dist: Optional[np.ndarray] = None,
#     existing_pole_indices=None,
#     config=None,
# ) -> PowerPhase5Supports:
#     """Recover complete Pole/Pylon supports from confirmed conductor geometry.

#     Phase 5 is a rescue/confirmation phase, not a generic pole detector. It
#     requires Phase-3 conductor evidence, proposes physical support stations
#     from common track endpoints and interior sag high-points, then grows a
#     structural body downward through raw LiDAR geometry. Base AI semantic labels
#     are never an inclusion gate. Classification writes remain exact-fence-only
#     and are performed by the caller after this immutable state is built.
#     """
#     cfg = dict(config or {})
#     xyz = np.asarray(xyz, dtype=np.float64)
#     hag = np.asarray(hag, dtype=np.float32).reshape(-1)
#     n = len(hag)
#     if xyz.ndim != 2 or xyz.shape[1] < 3 or len(xyz) != n:
#         raise ValueError("Phase5 xyz/hag length mismatch")
#     if phase1.point_count != n or phase2.point_count != n or phase3.point_count != n:
#         raise ValueError("Phase5 Phase-state length mismatch")
#     if phase3.status != "OK" or len(phase3.tracks) < 2:
#         return _empty_state(n, "SKIPPED_NO_CONFIRMED_CONDUCTOR_BUNDLE")
#     if cl_station is None:
#         return _empty_state(n, "SKIPPED_NO_CL_STATION")

#     station = np.asarray(cl_station, dtype=np.float64).reshape(-1)
#     if len(station) != n:
#         raise ValueError("Phase5 cl_station length mismatch")
#     cdist = None
#     if cl_dist is not None:
#         cdist = np.asarray(cl_dist, dtype=np.float64).reshape(-1)
#         if len(cdist) != n:
#             raise ValueError("Phase5 cl_dist length mismatch")

#     l05 = _safe01(linearity_05)
#     l10 = _safe01(linearity_10)
#     v05 = _safe01(verticality_05)
#     v10 = _safe01(verticality_10)
#     _ = _safe01(planarity_05)  # retained in signature for geometry-contract parity
#     lstrength = np.maximum(l05, l10)
#     vstrength = np.maximum(v05, v10)
#     pole_score = np.asarray(phase2.pole_score, dtype=np.float32).reshape(-1)
#     raw_pole = np.asarray(phase2.raw_pole_mask, dtype=bool)
#     active = np.asarray(phase1.active_mask, dtype=bool)
#     protected = np.asarray(phase1.protected_mask, dtype=bool)
#     wire_support = np.asarray(phase3.wire_support_mask, dtype=bool)
#     confirmed_wire = np.asarray(phase3.confirmed_context_mask, dtype=bool) | np.asarray(phase3.projected_active_mask, dtype=bool)

#     min_tracks = int(cfg.get("phase5_endpoint_min_tracks", 3 if len(phase3.tracks) >= 4 else 2))
#     endpoints = _cluster_track_endpoints(
#         phase3.tracks,
#         gap_m=float(cfg.get("phase5_endpoint_cluster_gap_m", 3.2)),
#         min_tracks=min_tracks,
#     )
#     interior = _interior_wire_profile_anchors(
#         xyz, confirmed_wire, station,
#         track_count=len(phase3.tracks), config=cfg,
#     )
#     anchors = _merge_anchors(
#         endpoints + interior,
#         separation_m=float(cfg.get("phase5_anchor_merge_separation_m", 3.5)),
#     )
#     if not anchors:
#         return _empty_state(n, "NO_SUPPORT_ANCHORS")

#     existing = np.asarray(existing_pole_indices if existing_pole_indices is not None else [], dtype=np.int64)
#     existing = existing[(existing >= 0) & (existing < n)]
#     existing_tree = cKDTree(xyz[existing, :2]) if existing.size else None

#     support_context = np.zeros(n, dtype=bool)
#     diagnostics = []
#     finite = np.isfinite(station) & np.isfinite(hag) & np.isfinite(xyz[:, 2])

#     try:
#         for aid, anchor in enumerate(anchors):
#             near_wire = confirmed_wire & np.isfinite(station) & (
#                 np.abs(station - float(anchor.station)) <= float(cfg.get("phase5_anchor_wire_station_halfwidth_m", 2.4))
#             )
#             wi = np.flatnonzero(near_wire)
#             if wi.size < int(cfg.get("phase5_anchor_min_wire_points", 8)):
#                 diagnostics.append({
#                     "anchor": int(aid), **anchor.report(), "accepted": False,
#                     "reason": "too_few_local_wire_points", "wire_points": int(wi.size),
#                     "points": 0,
#                 })
#                 continue

#             anchor_xy = np.median(xyz[wi, :2], axis=0)
#             wire_z_low = float(np.percentile(xyz[wi, 2], 15.0))
#             wire_z_high = float(np.percentile(xyz[wi, 2], 85.0))

#             # If a legacy V3.3/V3.4 pole core already covers this physical
#             # support, Phase 5 remains a rescue-only phase and does not broaden
#             # that already-good classification.
#             if existing_tree is not None:
#                 d0, _ = existing_tree.query(anchor_xy, k=1)
#                 if float(d0) <= float(cfg.get("phase5_existing_support_radius_m", 3.0)):
#                     diagnostics.append({
#                         "anchor": int(aid), **anchor.report(), "accepted": False,
#                         "reason": "already_covered_by_existing_pole", "wire_points": int(wi.size),
#                         "points": 0,
#                     })
#                     continue

#             station_half = float(cfg.get(
#                 "phase5_station_halfwidth_multi_m" if anchor.track_count >= 4 else "phase5_station_halfwidth_m",
#                 6.2 if anchor.track_count >= 4 else 4.6,
#             ))
#             radius = float(cfg.get(
#                 "phase5_xy_radius_multi_m" if anchor.track_count >= 4 else "phase5_xy_radius_m",
#                 7.8 if anchor.track_count >= 4 else 5.5,
#             ))
#             dxy = np.linalg.norm(xyz[:, :2] - anchor_xy[None, :], axis=1)
#             semantic_free_structural = (
#                 raw_pole
#                 | ((vstrength >= float(cfg.get("phase5_extra_vert_min", 0.10)))
#                    & (lstrength >= float(cfg.get("phase5_extra_lin_min", 0.18))))
#             )
#             local_mask = (
#                 finite & (~protected) & semantic_free_structural
#                 & (hag >= float(cfg.get("phase5_hag_min", 0.5)))
#                 & (hag <= float(cfg.get("phase5_hag_max", 90.0)))
#                 & (np.abs(station - float(anchor.station)) <= station_half)
#                 & (dxy <= radius)
#                 & (xyz[:, 2] <= wire_z_high + float(cfg.get("phase5_above_wire_margin_m", 5.5)))
#             )
#             if cdist is not None:
#                 local_mask &= np.isfinite(cdist)

#             # Do not let the fitted conductor itself become the tower body.
#             # Crossarm/tower returns with meaningful verticality remain eligible.
#             pure_wire = wire_support & (vstrength < float(cfg.get("phase5_wire_exclusion_vert_max", 0.20)))
#             local_mask &= ~pure_wire
#             local_idx = np.flatnonzero(local_mask)
#             if local_idx.size < int(cfg.get("phase5_min_local_candidates", 24)):
#                 diagnostics.append({
#                     "anchor": int(aid), **anchor.report(), "accepted": False,
#                     "reason": "too_few_structural_candidates", "wire_points": int(wi.size),
#                     "candidates": int(local_idx.size), "points": 0,
#                 })
#                 continue

#             gi, cell_rep = _cell_structural_growth(
#                 xyz, hag, local_idx, wi, anchor_xy, wire_z_low,
#                 vstrength=vstrength, lstrength=lstrength, pole_score=pole_score,
#                 config=cfg,
#             )
#             if gi.size < int(cfg.get("phase5_min_support_points", 35)):
#                 diagnostics.append({
#                     "anchor": int(aid), **anchor.report(), "accepted": False,
#                     "reason": "no_connected_downward_structure", "wire_points": int(wi.size),
#                     "candidates": int(local_idx.size), "points": int(gi.size), **cell_rep,
#                 })
#                 continue

#             pts = xyz[gi]
#             qx = np.percentile(pts[:, 0], [5.0, 95.0])
#             qy = np.percentile(pts[:, 1], [5.0, 95.0])
#             qz = np.percentile(pts[:, 2], [2.0, 98.0])
#             qh = np.percentile(hag[gi], [10.0, 90.0])
#             xy_span = float(max(qx[1] - qx[0], qy[1] - qy[0]))
#             height = float(max(qz[1] - qz[0], qh[1] - qh[0]))
#             sv = station[gi]
#             station_span = float(np.percentile(sv, 95.0) - np.percentile(sv, 5.0)) if gi.size > 3 else float(np.ptp(sv))
#             z0 = float(np.min(pts[:, 2]))
#             bins = int(len(np.unique(np.floor((pts[:, 2] - z0) / 1.0).astype(np.int32))))
#             vf = float(np.mean(vstrength[gi] >= float(cfg.get("phase5_metric_vert_threshold", 0.18))))
#             lf = float(np.mean(lstrength[gi] >= float(cfg.get("phase5_metric_lin_threshold", 0.28))))
#             base_reach = float(qh[0]) <= float(cfg.get("phase5_base_hag_max", 5.5))
#             top_gap = float(wire_z_low - qz[1])
#             top_reach = top_gap <= float(cfg.get("phase5_top_gap_max_m", 5.0))

#             # Direct conductor attachment count on the upper structure.
#             wtree = cKDTree(xyz[wi, :2])
#             try:
#                 top = pts[:, 2] >= np.percentile(pts[:, 2], 65.0)
#                 tpts = pts[top]
#                 if len(tpts):
#                     dd, nn = wtree.query(tpts[:, :2], k=1, workers=-1)
#                     wz = xyz[wi[np.asarray(nn, dtype=np.int64)], 2]
#                     top_links = int(np.count_nonzero(
#                         (np.asarray(dd) <= float(cfg.get("phase5_top_link_xy_m", 4.8)))
#                         & (np.abs(tpts[:, 2] - wz) <= float(cfg.get("phase5_top_link_dz_m", 4.8)))
#                     ))
#                 else:
#                     top_links = 0
#             finally:
#                 del wtree

#             multi = anchor.track_count >= int(cfg.get("phase5_lattice_track_count", 3))
#             if multi:
#                 compact = (
#                     xy_span <= float(cfg.get("phase5_lattice_max_xy_m", 13.5))
#                     and station_span <= float(cfg.get("phase5_lattice_max_station_span_m", 12.5))
#                 )
#                 shape_ok = (
#                     height >= float(cfg.get("phase5_lattice_min_height_m", 5.0))
#                     and bins >= int(cfg.get("phase5_lattice_min_bins", 5))
#                     and (vf >= float(cfg.get("phase5_lattice_min_vert_fraction", 0.18))
#                          or lf >= float(cfg.get("phase5_lattice_min_lin_fraction", 0.28)))
#                     and (vf + lf) >= float(cfg.get("phase5_lattice_min_combined_fraction", 0.48))
#                 )
#                 link_ok = top_links >= int(cfg.get("phase5_lattice_min_top_links", max(4, anchor.track_count)))
#             else:
#                 compact = (
#                     xy_span <= float(cfg.get("phase5_pole_max_xy_m", 5.0))
#                     and station_span <= float(cfg.get("phase5_pole_max_station_span_m", 6.5))
#                 )
#                 shape_ok = (
#                     height >= float(cfg.get("phase5_pole_min_height_m", 4.0))
#                     and bins >= int(cfg.get("phase5_pole_min_bins", 4))
#                     and (vf >= float(cfg.get("phase5_pole_min_vert_fraction", 0.30))
#                          or lf >= float(cfg.get("phase5_pole_min_lin_fraction", 0.45)))
#                 )
#                 link_ok = top_links >= int(cfg.get("phase5_pole_min_top_links", 2))

#             accepted = bool(base_reach and top_reach and compact and shape_ok and link_ok)
#             reason = "conductor_anchored_full_support" if accepted else "support_guard_reject"
#             diagnostics.append({
#                 "anchor": int(aid), **anchor.report(), "accepted": accepted,
#                 "reason": reason, "wire_points": int(wi.size),
#                 "candidates": int(local_idx.size), "points": int(gi.size),
#                 "height": height, "xy": xy_span, "station_span": station_span,
#                 "bins": bins, "vert": vf, "linear": lf,
#                 "low_hag_p10": float(qh[0]), "top_gap": top_gap,
#                 "top_links": int(top_links), **cell_rep,
#             })
#             if accepted:
#                 support_context[gi] = True
#     finally:
#         if existing_tree is not None:
#             del existing_tree

#     support_active = support_context & active & (~protected)
#     accepted_count = sum(1 for d in diagnostics if bool(d.get("accepted", False)))
#     status = "OK" if accepted_count else "NO_ACCEPTED_SUPPORTS"
#     return PowerPhase5Supports(
#         version=PHASE5_VERSION,
#         point_count=n,
#         status=status,
#         support_context_mask=_freeze(support_context.copy()),
#         support_active_mask=_freeze(support_active.copy()),
#         anchors=tuple(anchors),
#         diagnostics=tuple(diagnostics),
#         exact_fence_only=True,
#         conductor_anchored=True,
#     )
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

import numpy as np
from scipy.spatial import cKDTree

from .power_phase1_state import PowerPhase1State
from .power_phase2_candidates import PowerPhase2Candidates
from .power_phase3_tracks import PowerPhase3Tracks


PHASE5_VERSION = "NAKSHA_POWER_PHASE5_CONDUCTOR_ANCHORED_SUPPORTS_V3_27_SINGLE_TRACK_SAFE"


def _freeze(arr: np.ndarray) -> np.ndarray:
    arr.setflags(write=False)
    return arr


def _safe01(arr) -> np.ndarray:
    a = np.asarray(arr, dtype=np.float32).reshape(-1)
    return np.clip(np.nan_to_num(a, nan=0.0, posinf=1.0, neginf=0.0), 0.0, 1.0)


@dataclass(frozen=True)
class SupportAnchor:
    station: float
    source: str
    track_count: int
    track_ids: Tuple[int, ...]
    prominence: float = 0.0

    def report(self) -> dict:
        return {
            "station": float(self.station),
            "source": str(self.source),
            "track_count": int(self.track_count),
            "track_ids": [int(x) for x in self.track_ids],
            "prominence": float(self.prominence),
        }


@dataclass(frozen=True)
class PowerPhase5Supports:
    version: str
    point_count: int
    status: str
    support_context_mask: np.ndarray
    support_active_mask: np.ndarray
    anchors: Tuple[SupportAnchor, ...]
    diagnostics: Tuple[Mapping[str, object], ...]
    exact_fence_only: bool
    conductor_anchored: bool

    def report(self) -> dict:
        accepted = [d for d in self.diagnostics if bool(d.get("accepted", False))]
        return {
            "version": self.version,
            "status": self.status,
            "classification_writes_in_module": False,
            "conductor_anchored": bool(self.conductor_anchored),
            "exact_fence_only": bool(self.exact_fence_only),
            "anchors": int(len(self.anchors)),
            "accepted_supports": int(len(accepted)),
            "support_context_points": int(np.count_nonzero(self.support_context_mask)),
            "support_active_points": int(np.count_nonzero(self.support_active_mask)),
            "anchor_reports": [a.report() for a in self.anchors],
            "support_reports": [dict(d) for d in self.diagnostics],
            "immutable": bool(
                not self.support_context_mask.flags.writeable
                and not self.support_active_mask.flags.writeable
            ),
        }


def _empty_state(n: int, status: str) -> PowerPhase5Supports:
    z = np.zeros(int(n), dtype=bool)
    return PowerPhase5Supports(
        version=PHASE5_VERSION,
        point_count=int(n),
        status=str(status),
        support_context_mask=_freeze(z.copy()),
        support_active_mask=_freeze(z.copy()),
        anchors=tuple(),
        diagnostics=tuple(),
        exact_fence_only=True,
        conductor_anchored=True,
    )


def _cluster_track_endpoints(tracks, *, gap_m: float, min_tracks: int):
    points = []
    for t in tracks:
        points.append((float(t.station_min), int(t.track_id), "min"))
        points.append((float(t.station_max), int(t.track_id), "max"))
    if not points:
        return []
    points.sort(key=lambda x: x[0])
    groups = [[points[0]]]
    for p in points[1:]:
        if float(p[0] - groups[-1][-1][0]) <= float(gap_m):
            groups[-1].append(p)
        else:
            groups.append([p])

    anchors = []
    for g in groups:
        ids = tuple(sorted({int(x[1]) for x in g}))
        if len(ids) < int(min_tracks):
            continue
        # Multiple endpoints from one physical support may be offset by one or
        # two station bins. Median is stable and keeps the search centred.
        anchors.append(SupportAnchor(
            station=float(np.median([x[0] for x in g])),
            source="track_endpoint_cluster",
            track_count=len(ids),
            track_ids=ids,
            prominence=0.0,
        ))
    return anchors


def _interior_wire_profile_anchors(
    xyz: np.ndarray,
    wire_mask: np.ndarray,
    station: np.ndarray,
    *,
    track_count: int,
    config: dict,
):
    """Conservative interior support proposals from conductor sag high-points.

    Endpoint clusters handle the common fence/tower case. This second path
    covers a support inside a longer fitted span. It never classifies points;
    it only proposes a station that still has to pass the structural support
    validation below.
    """
    wi = np.flatnonzero(np.asarray(wire_mask, dtype=bool) & np.isfinite(station))
    if wi.size < 40:
        return []
    st = station[wi]
    z = xyz[wi, 2]
    bin_m = float(config.get("phase5_profile_bin_m", 1.0))
    s0 = float(np.min(st))
    bins = np.floor((st - s0) / max(bin_m, 0.5)).astype(np.int64)
    nbin = int(np.max(bins)) + 1 if bins.size else 0
    if nbin < 10:
        return []
    count = np.bincount(bins, minlength=nbin)
    prof = np.full(nbin, np.nan, dtype=np.float64)
    ps = s0 + (np.arange(nbin, dtype=np.float64) + 0.5) * bin_m
    order = np.argsort(bins, kind="stable")
    starts = np.cumsum(np.r_[0, count[:-1]])
    min_pts = int(config.get("phase5_profile_min_wire_points", 6))
    for b in np.flatnonzero(count >= min_pts):
        loc = order[starts[b]:starts[b] + count[b]]
        prof[b] = float(np.median(z[loc]))
    if np.count_nonzero(np.isfinite(prof)) < 8:
        return []

    near_m = float(config.get("phase5_profile_side_near_m", 5.0))
    far_m = float(config.get("phase5_profile_side_far_m", 16.0))
    min_prom = float(config.get("phase5_profile_min_prominence_m", 0.20))
    candidates = []
    for i in np.flatnonzero(np.isfinite(prof)):
        si = ps[i]
        left = (ps <= si - near_m) & (ps >= si - far_m) & np.isfinite(prof)
        right = (ps >= si + near_m) & (ps <= si + far_m) & np.isfinite(prof)
        if not np.any(left) or not np.any(right):
            continue
        lz = float(np.median(prof[left]))
        rz = float(np.median(prof[right]))
        expected = 0.5 * (lz + rz)
        prom = float(prof[i] - expected)
        if prom < min_prom:
            continue
        # Require a local high point, not a monotonically sloping profile.
        hw = max(2, int(round(float(config.get("phase5_profile_peak_halfwidth_m", 3.0)) / bin_m)))
        a = max(0, i - hw)
        b = min(nbin, i + hw + 1)
        pv = prof[a:b]
        pv = pv[np.isfinite(pv)]
        if pv.size and float(prof[i]) < float(np.max(pv)) - 0.05:
            continue
        candidates.append(SupportAnchor(
            station=float(si),
            source="interior_sag_highpoint",
            track_count=int(track_count),
            track_ids=tuple(range(int(track_count))),
            prominence=prom,
        ))

    # Station-space NMS.
    sep = float(config.get("phase5_anchor_min_separation_m", 6.0))
    candidates.sort(key=lambda a: a.prominence, reverse=True)
    kept = []
    for a in candidates:
        if all(abs(a.station - b.station) >= sep for b in kept):
            kept.append(a)
    return sorted(kept, key=lambda a: a.station)


def _merge_anchors(anchors, *, separation_m: float):
    if not anchors:
        return []
    anchors = sorted(anchors, key=lambda a: (a.station, -a.track_count, -a.prominence))
    groups = [[anchors[0]]]
    for a in anchors[1:]:
        if abs(a.station - groups[-1][-1].station) <= float(separation_m):
            groups[-1].append(a)
        else:
            groups.append([a])
    out = []
    for g in groups:
        # Endpoint evidence outranks a profile-only proposal at the same site.
        g2 = sorted(g, key=lambda a: (a.source != "track_endpoint_cluster", -a.track_count, -a.prominence))
        best = g2[0]
        ids = tuple(sorted({tid for a in g for tid in a.track_ids}))
        out.append(SupportAnchor(
            station=float(np.median([a.station for a in g])),
            source=best.source,
            track_count=max(int(best.track_count), len(ids)),
            track_ids=ids if ids else best.track_ids,
            prominence=max(float(a.prominence) for a in g),
        ))
    return out


def _cell_structural_growth(
    xyz: np.ndarray,
    hag: np.ndarray,
    local_idx: np.ndarray,
    wire_idx: np.ndarray,
    anchor_xy: np.ndarray,
    wire_z_low: float,
    *,
    vstrength: np.ndarray,
    lstrength: np.ndarray,
    pole_score: np.ndarray,
    config: dict,
):
    """Grow a conductor-attached vertical/lattice support through XY cells."""
    if local_idx.size < 8 or wire_idx.size < 4:
        return np.empty(0, dtype=np.int64), {"cells": 0, "seed_cells": 0}

    cell_size = float(config.get("phase5_cell_size_m", 0.80))
    cell_size = min(max(cell_size, 0.55), 1.10)
    cells = np.floor((xyz[local_idx, :2] - anchor_xy[None, :]) / cell_size).astype(np.int64)
    uc, inv = np.unique(cells, axis=0, return_inverse=True)
    counts = np.bincount(inv, minlength=len(uc))
    order = np.argsort(inv, kind="stable")
    starts = np.cumsum(np.r_[0, counts[:-1]])

    wtree = cKDTree(xyz[wire_idx, :2])
    cell_info = []
    cell_points = {}
    try:
        for cid in range(len(uc)):
            cnt = int(counts[cid])
            if cnt < int(config.get("phase5_cell_min_points", 3)):
                continue
            pos = order[starts[cid]:starts[cid] + cnt]
            gi = local_idx[pos]
            pts = xyz[gi]
            qz = np.percentile(pts[:, 2], [5.0, 95.0])
            qh = np.percentile(hag[gi], [10.0, 90.0])
            hspan = float(max(qz[1] - qz[0], qh[1] - qh[0]))
            if hspan < float(config.get("phase5_cell_min_height_m", 1.3)):
                continue
            z0 = float(np.min(pts[:, 2]))
            zb = np.floor((pts[:, 2] - z0) / float(config.get("phase5_cell_z_bin_m", 0.9))).astype(np.int32)
            nbins = int(len(np.unique(zb)))
            if nbins < int(config.get("phase5_cell_min_bins", 2)):
                continue
            vf = float(np.mean(vstrength[gi] >= float(config.get("phase5_cell_vert_threshold", 0.14))))
            lf = float(np.mean(lstrength[gi] >= float(config.get("phase5_cell_lin_threshold", 0.26))))
            ps = float(np.median(pole_score[gi]))
            structural = (
                (vf >= float(config.get("phase5_cell_min_vert_fraction", 0.12)) and
                 lf >= float(config.get("phase5_cell_min_lin_fraction", 0.10)))
                or lf >= float(config.get("phase5_cell_strong_lin_fraction", 0.34))
                or ps >= float(config.get("phase5_cell_min_pole_score", 0.25))
            )
            if not structural:
                continue

            center = np.median(pts[:, :2], axis=0)
            center_dist = float(np.linalg.norm(center - anchor_xy))
            top_mask = pts[:, 2] >= np.percentile(pts[:, 2], 65.0)
            top_pts = pts[top_mask]
            attach = 0
            if len(top_pts):
                dd, nn = wtree.query(top_pts[:, :2], k=1, workers=-1)
                wz = xyz[wire_idx[np.asarray(nn, dtype=np.int64)], 2]
                attach = int(np.count_nonzero(
                    (np.asarray(dd) <= float(config.get("phase5_top_link_xy_m", 4.8)))
                    & (np.abs(top_pts[:, 2] - wz) <= float(config.get("phase5_top_link_dz_m", 4.8)))
                ))
            seed = bool(
                attach >= int(config.get("phase5_seed_min_top_links", 1))
                or (
                    float(qz[1]) >= float(wire_z_low) - float(config.get("phase5_seed_top_gap_m", 3.8))
                    and center_dist <= float(config.get("phase5_seed_radius_m", 5.8))
                )
            )
            cell_info.append({
                "cid": int(cid), "center": np.asarray(center, dtype=np.float64),
                "vf": vf, "lf": lf, "pole_score": ps, "height": hspan,
                "attach": int(attach), "seed": seed,
                "base": float(qh[0]) <= float(config.get("phase5_cell_base_hag_max", 6.0)),
                "top_z": float(qz[1]), "center_dist": center_dist,
            })
            cell_points[int(cid)] = gi
    finally:
        del wtree

    if not cell_info:
        return np.empty(0, dtype=np.int64), {"cells": 0, "seed_cells": 0}

    ids = np.asarray([c["cid"] for c in cell_info], dtype=np.int64)
    centers = np.vstack([c["center"] for c in cell_info])
    seeds = np.flatnonzero(np.asarray([bool(c["seed"]) for c in cell_info], dtype=bool))
    if seeds.size == 0:
        return np.empty(0, dtype=np.int64), {"cells": len(cell_info), "seed_cells": 0}

    # Grow through nearby structural XY columns. This keeps a lattice body and
    # its legs together while preventing a distant vegetation patch inside the
    # same broad CL corridor from being absorbed.
    tree = cKDTree(centers)
    pairs = tree.query_pairs(float(config.get("phase5_cell_connect_radius_m", 2.25)), output_type="ndarray")
    del tree
    parent = np.arange(len(cell_info), dtype=np.int32)

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

    seed_roots = {find(int(s)) for s in seeds}
    selected_pos = [i for i in range(len(cell_info)) if find(i) in seed_roots]

    # Companion-leg rescue: wide lattice towers can have legs separated by a
    # small gap from the top seed component in sparse data. Include only very
    # structural base-reaching cells near the anchor; this is far safer than
    # widening the old whole-component threshold.
    companion_radius = float(config.get("phase5_companion_radius_m", 6.8))
    for i, c in enumerate(cell_info):
        if i in selected_pos:
            continue
        if c["center_dist"] > companion_radius or not c["base"]:
            continue
        strong = (
            c["vf"] >= float(config.get("phase5_companion_min_vert_fraction", 0.30))
            or c["lf"] >= float(config.get("phase5_companion_min_lin_fraction", 0.42))
            or (c["vf"] >= 0.20 and c["lf"] >= 0.24 and c["height"] >= 3.0)
        )
        if strong:
            selected_pos.append(i)

    if not selected_pos:
        return np.empty(0, dtype=np.int64), {"cells": len(cell_info), "seed_cells": int(seeds.size)}

    selected_ids = {int(ids[i]) for i in selected_pos}
    gi = np.unique(np.concatenate([cell_points[cid] for cid in selected_ids])).astype(np.int64)

    # Point-level containment. Retain lattice/vertical/diagonal support points,
    # not foliage that merely shares one selected XY cell.
    point_ok = (
        ((vstrength[gi] >= float(config.get("phase5_write_vert_min", 0.28)))
         & (lstrength[gi] >= float(config.get("phase5_write_lin_min", 0.34))))
        | (vstrength[gi] >= float(config.get("phase5_write_strong_vert", 0.55)))
        | (lstrength[gi] >= float(config.get("phase5_write_strong_lin", 0.65)))
        | (pole_score[gi] >= float(config.get("phase5_write_pole_score", 0.50)))
    )
    gi = gi[point_ok]
    return gi, {
        "cells": int(len(cell_info)),
        "seed_cells": int(seeds.size),
        "selected_cells": int(len(selected_ids)),
    }


def build_power_phase5_supports(
    xyz,
    hag,
    phase1: PowerPhase1State,
    phase2: PowerPhase2Candidates,
    phase3: PowerPhase3Tracks,
    *,
    linearity_05,
    planarity_05,
    verticality_05,
    linearity_10,
    verticality_10,
    cl_station: Optional[np.ndarray],
    cl_dist: Optional[np.ndarray] = None,
    existing_pole_indices=None,
    config=None,
) -> PowerPhase5Supports:
    """Recover complete Pole/Pylon supports from confirmed conductor geometry.

    Phase 5 is a rescue/confirmation phase, not a generic pole detector. It
    requires Phase-3 conductor evidence, proposes physical support stations
    from common track endpoints and interior sag high-points, then grows a
    structural body downward through raw LiDAR geometry. Base AI semantic labels
    are never an inclusion gate. Classification writes remain exact-fence-only
    and are performed by the caller after this immutable state is built.
    """
    cfg = dict(config or {})
    xyz = np.asarray(xyz, dtype=np.float64)
    hag = np.asarray(hag, dtype=np.float32).reshape(-1)
    n = len(hag)
    if xyz.ndim != 2 or xyz.shape[1] < 3 or len(xyz) != n:
        raise ValueError("Phase5 xyz/hag length mismatch")
    if phase1.point_count != n or phase2.point_count != n or phase3.point_count != n:
        raise ValueError("Phase5 Phase-state length mismatch")
    if phase3.status != "OK" or len(phase3.tracks) < 1:
        return _empty_state(n, "SKIPPED_NO_CONFIRMED_CONDUCTOR")
    if cl_station is None:
        return _empty_state(n, "SKIPPED_NO_CL_STATION")

    station = np.asarray(cl_station, dtype=np.float64).reshape(-1)
    if len(station) != n:
        raise ValueError("Phase5 cl_station length mismatch")
    cdist = None
    if cl_dist is not None:
        cdist = np.asarray(cl_dist, dtype=np.float64).reshape(-1)
        if len(cdist) != n:
            raise ValueError("Phase5 cl_dist length mismatch")

    l05 = _safe01(linearity_05)
    l10 = _safe01(linearity_10)
    v05 = _safe01(verticality_05)
    v10 = _safe01(verticality_10)
    _ = _safe01(planarity_05)  # retained in signature for geometry-contract parity
    lstrength = np.maximum(l05, l10)
    vstrength = np.maximum(v05, v10)
    pole_score = np.asarray(phase2.pole_score, dtype=np.float32).reshape(-1)
    raw_pole = np.asarray(phase2.raw_pole_mask, dtype=bool)
    active = np.asarray(phase1.active_mask, dtype=bool)
    protected = np.asarray(phase1.protected_mask, dtype=bool)
    wire_support = np.asarray(phase3.wire_support_mask, dtype=bool)
    confirmed_wire = np.asarray(phase3.confirmed_context_mask, dtype=bool) | np.asarray(phase3.projected_active_mask, dtype=bool)

    default_min_tracks = 1 if len(phase3.tracks) == 1 else (3 if len(phase3.tracks) >= 4 else 2)
    min_tracks = int(cfg.get("phase5_endpoint_min_tracks", default_min_tracks))
    endpoints = _cluster_track_endpoints(
        phase3.tracks,
        gap_m=float(cfg.get("phase5_endpoint_cluster_gap_m", 3.2)),
        min_tracks=min_tracks,
    )
    interior = _interior_wire_profile_anchors(
        xyz, confirmed_wire, station,
        track_count=len(phase3.tracks), config=cfg,
    )
    anchors = _merge_anchors(
        endpoints + interior,
        separation_m=float(cfg.get("phase5_anchor_merge_separation_m", 3.5)),
    )
    if not anchors:
        return _empty_state(n, "NO_SUPPORT_ANCHORS")

    existing = np.asarray(existing_pole_indices if existing_pole_indices is not None else [], dtype=np.int64)
    existing = existing[(existing >= 0) & (existing < n)]
    existing_tree = cKDTree(xyz[existing, :2]) if existing.size else None

    support_context = np.zeros(n, dtype=bool)
    diagnostics = []
    finite = np.isfinite(station) & np.isfinite(hag) & np.isfinite(xyz[:, 2])

    try:
        for aid, anchor in enumerate(anchors):
            near_wire = confirmed_wire & np.isfinite(station) & (
                np.abs(station - float(anchor.station)) <= float(cfg.get("phase5_anchor_wire_station_halfwidth_m", 2.4))
            )
            wi = np.flatnonzero(near_wire)
            if wi.size < int(cfg.get("phase5_anchor_min_wire_points", 8)):
                diagnostics.append({
                    "anchor": int(aid), **anchor.report(), "accepted": False,
                    "reason": "too_few_local_wire_points", "wire_points": int(wi.size),
                    "points": 0,
                })
                continue

            anchor_xy = np.median(xyz[wi, :2], axis=0)
            wire_z_low = float(np.percentile(xyz[wi, 2], 15.0))
            wire_z_high = float(np.percentile(xyz[wi, 2], 85.0))

            # If a legacy V3.3/V3.4 pole core already covers this physical
            # support, Phase 5 remains a rescue-only phase and does not broaden
            # that already-good classification.
            if existing_tree is not None:
                d0, _ = existing_tree.query(anchor_xy, k=1)
                if float(d0) <= float(cfg.get("phase5_existing_support_radius_m", 3.0)):
                    diagnostics.append({
                        "anchor": int(aid), **anchor.report(), "accepted": False,
                        "reason": "already_covered_by_existing_pole", "wire_points": int(wi.size),
                        "points": 0,
                    })
                    continue

            station_half = float(cfg.get(
                "phase5_station_halfwidth_multi_m" if anchor.track_count >= 4 else "phase5_station_halfwidth_m",
                6.2 if anchor.track_count >= 4 else 4.6,
            ))
            radius = float(cfg.get(
                "phase5_xy_radius_multi_m" if anchor.track_count >= 4 else "phase5_xy_radius_m",
                7.8 if anchor.track_count >= 4 else 5.5,
            ))
            dxy = np.linalg.norm(xyz[:, :2] - anchor_xy[None, :], axis=1)
            semantic_free_structural = (
                raw_pole
                | ((vstrength >= float(cfg.get("phase5_extra_vert_min", 0.10)))
                   & (lstrength >= float(cfg.get("phase5_extra_lin_min", 0.18))))
            )
            local_mask = (
                finite & (~protected) & semantic_free_structural
                & (hag >= float(cfg.get("phase5_hag_min", 0.5)))
                & (hag <= float(cfg.get("phase5_hag_max", 90.0)))
                & (np.abs(station - float(anchor.station)) <= station_half)
                & (dxy <= radius)
                & (xyz[:, 2] <= wire_z_high + float(cfg.get("phase5_above_wire_margin_m", 5.5)))
            )
            if cdist is not None:
                local_mask &= np.isfinite(cdist)

            # Do not let the fitted conductor itself become the tower body.
            # Crossarm/tower returns with meaningful verticality remain eligible.
            pure_wire = wire_support & (vstrength < float(cfg.get("phase5_wire_exclusion_vert_max", 0.20)))
            local_mask &= ~pure_wire
            local_idx = np.flatnonzero(local_mask)
            if local_idx.size < int(cfg.get("phase5_min_local_candidates", 24)):
                diagnostics.append({
                    "anchor": int(aid), **anchor.report(), "accepted": False,
                    "reason": "too_few_structural_candidates", "wire_points": int(wi.size),
                    "candidates": int(local_idx.size), "points": 0,
                })
                continue

            gi, cell_rep = _cell_structural_growth(
                xyz, hag, local_idx, wi, anchor_xy, wire_z_low,
                vstrength=vstrength, lstrength=lstrength, pole_score=pole_score,
                config=cfg,
            )
            if gi.size < int(cfg.get("phase5_min_support_points", 35)):
                diagnostics.append({
                    "anchor": int(aid), **anchor.report(), "accepted": False,
                    "reason": "no_connected_downward_structure", "wire_points": int(wi.size),
                    "candidates": int(local_idx.size), "points": int(gi.size), **cell_rep,
                })
                continue

            pts = xyz[gi]
            qx = np.percentile(pts[:, 0], [5.0, 95.0])
            qy = np.percentile(pts[:, 1], [5.0, 95.0])
            qz = np.percentile(pts[:, 2], [2.0, 98.0])
            qh = np.percentile(hag[gi], [10.0, 90.0])
            xy_span = float(max(qx[1] - qx[0], qy[1] - qy[0]))
            height = float(max(qz[1] - qz[0], qh[1] - qh[0]))
            sv = station[gi]
            station_span = float(np.percentile(sv, 95.0) - np.percentile(sv, 5.0)) if gi.size > 3 else float(np.ptp(sv))
            z0 = float(np.min(pts[:, 2]))
            bins = int(len(np.unique(np.floor((pts[:, 2] - z0) / 1.0).astype(np.int32))))
            vf = float(np.mean(vstrength[gi] >= float(cfg.get("phase5_metric_vert_threshold", 0.18))))
            lf = float(np.mean(lstrength[gi] >= float(cfg.get("phase5_metric_lin_threshold", 0.28))))
            base_reach = float(qh[0]) <= float(cfg.get("phase5_base_hag_max", 5.5))
            top_gap = float(wire_z_low - qz[1])
            top_reach = top_gap <= float(cfg.get("phase5_top_gap_max_m", 5.0))

            # Direct conductor attachment count on the upper structure.
            wtree = cKDTree(xyz[wi, :2])
            try:
                top = pts[:, 2] >= np.percentile(pts[:, 2], 65.0)
                tpts = pts[top]
                if len(tpts):
                    dd, nn = wtree.query(tpts[:, :2], k=1, workers=-1)
                    wz = xyz[wi[np.asarray(nn, dtype=np.int64)], 2]
                    top_links = int(np.count_nonzero(
                        (np.asarray(dd) <= float(cfg.get("phase5_top_link_xy_m", 4.8)))
                        & (np.abs(tpts[:, 2] - wz) <= float(cfg.get("phase5_top_link_dz_m", 4.8)))
                    ))
                else:
                    top_links = 0
            finally:
                del wtree

            single_track = int(anchor.track_count) == 1
            pole_max_xy = float(cfg.get("phase5_pole_max_xy_m", 5.0))
            lattice_max_xy = float(cfg.get("phase5_lattice_max_xy_m", 13.5))
            # A real lattice pylon may be seen through only one recovered wire
            # track in a partial/long noisy selection. Do not force it through
            # narrow-pole dimensions; infer lattice shape from the support body.
            lattice_by_shape = bool(
                xy_span > pole_max_xy
                and xy_span <= lattice_max_xy
                and height >= float(cfg.get("phase5_single_track_lattice_min_height_m", 5.5))
                and bins >= int(cfg.get("phase5_single_track_lattice_min_bins", 5))
                and (vf + lf) >= float(cfg.get("phase5_single_track_lattice_min_combined_fraction", 0.70))
            )
            multi = bool(
                anchor.track_count >= int(cfg.get("phase5_lattice_track_count", 3))
                or (single_track and lattice_by_shape)
            )
            if multi:
                compact = (
                    xy_span <= lattice_max_xy
                    and station_span <= float(cfg.get("phase5_lattice_max_station_span_m", 12.5))
                )
                shape_ok = (
                    height >= float(cfg.get("phase5_lattice_min_height_m", 5.0))
                    and bins >= int(cfg.get("phase5_lattice_min_bins", 5))
                    and (vf >= float(cfg.get("phase5_lattice_min_vert_fraction", 0.18))
                         or lf >= float(cfg.get("phase5_lattice_min_lin_fraction", 0.28)))
                    and (vf + lf) >= float(cfg.get("phase5_lattice_min_combined_fraction", 0.48))
                )
                min_links = int(cfg.get("phase5_lattice_min_top_links", max(4, anchor.track_count)))
                if single_track:
                    min_links = max(min_links, int(cfg.get("phase5_single_track_lattice_min_top_links", 5)))
                    shape_ok = bool(
                        shape_ok
                        and (vf + lf) >= float(cfg.get("phase5_single_track_lattice_strong_combined_fraction", 0.70))
                        and (vf >= float(cfg.get("phase5_single_track_lattice_vert_fraction", 0.24))
                             or lf >= float(cfg.get("phase5_single_track_lattice_lin_fraction", 0.38)))
                    )
                link_ok = top_links >= min_links
            else:
                compact = (
                    xy_span <= pole_max_xy
                    and station_span <= float(cfg.get("phase5_pole_max_station_span_m", 6.5))
                )
                shape_ok = (
                    height >= float(cfg.get("phase5_pole_min_height_m", 4.0))
                    and bins >= int(cfg.get("phase5_pole_min_bins", 4))
                    and (vf >= float(cfg.get("phase5_pole_min_vert_fraction", 0.30))
                         or lf >= float(cfg.get("phase5_pole_min_lin_fraction", 0.45)))
                )
                min_links = int(cfg.get("phase5_pole_min_top_links", 2))
                if single_track:
                    min_links = max(min_links, int(cfg.get("phase5_single_track_pole_min_top_links", 3)))
                    shape_ok = bool(
                        shape_ok
                        and height >= float(cfg.get("phase5_single_track_pole_min_height_m", 4.5))
                        and (vf >= float(cfg.get("phase5_single_track_pole_vert_fraction", 0.36))
                             or lf >= float(cfg.get("phase5_single_track_pole_lin_fraction", 0.50)))
                    )
                link_ok = top_links >= min_links

            accepted = bool(base_reach and top_reach and compact and shape_ok and link_ok)
            reason = "conductor_anchored_full_support" if accepted else "support_guard_reject"
            diagnostics.append({
                "anchor": int(aid), **anchor.report(), "accepted": accepted,
                "reason": reason, "wire_points": int(wi.size),
                "candidates": int(local_idx.size), "points": int(gi.size),
                "height": height, "xy": xy_span, "station_span": station_span,
                "bins": bins, "vert": vf, "linear": lf,
                "low_hag_p10": float(qh[0]), "top_gap": top_gap,
                "top_links": int(top_links), **cell_rep,
            })
            if accepted:
                support_context[gi] = True
    finally:
        if existing_tree is not None:
            del existing_tree

    support_active = support_context & active & (~protected)
    accepted_count = sum(1 for d in diagnostics if bool(d.get("accepted", False)))
    status = "OK" if accepted_count else "NO_ACCEPTED_SUPPORTS"
    return PowerPhase5Supports(
        version=PHASE5_VERSION,
        point_count=n,
        status=status,
        support_context_mask=_freeze(support_context.copy()),
        support_active_mask=_freeze(support_active.copy()),
        anchors=tuple(anchors),
        diagnostics=tuple(diagnostics),
        exact_fence_only=True,
        conductor_anchored=True,
    )
