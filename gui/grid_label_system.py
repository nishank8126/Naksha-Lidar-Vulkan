# import os
# import re
# import time
# import traceback
# import uuid
# from typing import Dict, List, Optional, Tuple
# from pathlib import Path
# import vtk
# from PySide6.QtWidgets import QMessageBox, QFileDialog, QHBoxLayout, QProgressDialog
# from PySide6.QtCore import QSettings, QThread, Signal, Qt
# import numpy as np

# from gui.lidar_file_matcher import (
#     find_matching_lidar_file,
#     names_share_numeric_identity,
#     normalized_stem,
#     numeric_identity,
#     strip_lidar_extension,
# )


# def _normalize_ring_xy(points) -> List[Tuple[float, float]]:
#     ring: List[Tuple[float, float]] = []
#     if points is None:
#         return ring

#     for pt in points:
#         if pt is None:
#             continue
#         if len(pt) < 2:
#             continue
#         ring.append((float(pt[0]), float(pt[1])))

#     if len(ring) >= 2:
#         x0, y0 = ring[0]
#         x1, y1 = ring[-1]
#         if abs(x0 - x1) <= 1e-9 and abs(y0 - y1) <= 1e-9:
#             ring = ring[:-1]

#     dedup: List[Tuple[float, float]] = []
#     for pt in ring:
#         if not dedup:
#             dedup.append(pt)
#             continue
#         if abs(pt[0] - dedup[-1][0]) <= 1e-12 and abs(pt[1] - dedup[-1][1]) <= 1e-12:
#             continue
#         dedup.append(pt)

#     return dedup


# def _polygon_bounds_xy(points_xy: List[Tuple[float, float]]) -> Tuple[float, float, float, float]:
#     xs = [p[0] for p in points_xy]
#     ys = [p[1] for p in points_xy]
#     return (min(xs), min(ys), max(xs), max(ys))


# def _bbox_intersects(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float], eps: float = 1e-9) -> bool:
#     return not (
#         a[2] < b[0] - eps or
#         a[0] > b[2] + eps or
#         a[3] < b[1] - eps or
#         a[1] > b[3] + eps
#     )


# def _point_in_polygon_xy(px: float, py: float, polygon_xy: List[Tuple[float, float]], eps: float = 1e-9) -> bool:
#     n = len(polygon_xy)
#     if n < 3:
#         return False

#     inside = False
#     j = n - 1
#     for i in range(n):
#         xi, yi = polygon_xy[i]
#         xj, yj = polygon_xy[j]

#         dx = xj - xi
#         dy = yj - yi
#         seg_len2 = dx * dx + dy * dy
#         if seg_len2 > 0.0:
#             cross = (px - xi) * dy - (py - yi) * dx
#             if abs(cross) <= eps * (abs(dx) + abs(dy) + 1.0):
#                 dot = (px - xi) * dx + (py - yi) * dy
#                 if -eps <= dot <= seg_len2 + eps:
#                     return True

#         if ((yi > py) != (yj > py)):
#             safe_dy = dy if abs(dy) > 1e-20 else (1e-20 if dy >= 0 else -1e-20)
#             x_hit = (dx * (py - yi) / safe_dy) + xi
#             if px <= x_hit + eps:
#                 inside = not inside
#         j = i
#     return inside


# def _segments_intersect_2d(a1, a2, b1, b2, eps: float = 1e-9) -> bool:
#     def _orient(p, q, r):
#         return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

#     def _on_seg(p, q, r):
#         return (
#             min(p[0], r[0]) - eps <= q[0] <= max(p[0], r[0]) + eps and
#             min(p[1], r[1]) - eps <= q[1] <= max(p[1], r[1]) + eps
#         )

#     o1 = _orient(a1, a2, b1)
#     o2 = _orient(a1, a2, b2)
#     o3 = _orient(b1, b2, a1)
#     o4 = _orient(b1, b2, a2)

#     if (o1 > eps and o2 < -eps) or (o1 < -eps and o2 > eps):
#         if (o3 > eps and o4 < -eps) or (o3 < -eps and o4 > eps):
#             return True

#     if abs(o1) <= eps and _on_seg(a1, b1, a2):
#         return True
#     if abs(o2) <= eps and _on_seg(a1, b2, a2):
#         return True
#     if abs(o3) <= eps and _on_seg(b1, a1, b2):
#         return True
#     if abs(o4) <= eps and _on_seg(b1, a2, b2):
#         return True
#     return False


# def _polygons_intersect_xy(poly_a: List[Tuple[float, float]], poly_b: List[Tuple[float, float]]) -> bool:
#     if len(poly_a) < 3 or len(poly_b) < 3:
#         return False

#     if _point_in_polygon_xy(poly_a[0][0], poly_a[0][1], poly_b):
#         return True
#     if _point_in_polygon_xy(poly_b[0][0], poly_b[0][1], poly_a):
#         return True

#     for i in range(len(poly_a)):
#         a1 = poly_a[i]
#         a2 = poly_a[(i + 1) % len(poly_a)]
#         for j in range(len(poly_b)):
#             b1 = poly_b[j]
#             b2 = poly_b[(j + 1) % len(poly_b)]
#             if _segments_intersect_2d(a1, a2, b1, b2):
#                 return True
#     return False


# def _points_inside_polygon_mask(x: np.ndarray, y: np.ndarray, polygon_xy: List[Tuple[float, float]], eps: float = 1e-9) -> np.ndarray:
#     n_pts = len(x)
#     if n_pts == 0 or len(polygon_xy) < 3:
#         return np.zeros(n_pts, dtype=bool)

#     inside = np.zeros(n_pts, dtype=bool)
#     on_edge = np.zeros(n_pts, dtype=bool)

#     n = len(polygon_xy)
#     j = n - 1
#     for i in range(n):
#         xi, yi = polygon_xy[i]
#         xj, yj = polygon_xy[j]
#         dx = xj - xi
#         dy = yj - yi
#         seg_len2 = dx * dx + dy * dy

#         if seg_len2 > 0.0:
#             cross = (x - xi) * dy - (y - yi) * dx
#             line_tol = eps * (abs(dx) + abs(dy) + 1.0)
#             on_line = np.abs(cross) <= line_tol
#             dot = (x - xi) * dx + (y - yi) * dy
#             on_seg = on_line & (dot >= -eps) & (dot <= seg_len2 + eps)
#             on_edge |= on_seg

#         if abs(dy) <= 1e-20:
#             j = i
#             continue

#         crosses = ((yi > y) != (yj > y))
#         if np.any(crosses):
#             x_hit = dx * (y[crosses] - yi) / dy + xi
#             toggle = x[crosses] <= (x_hit + eps)
#             idx = np.flatnonzero(crosses)
#             inside[idx] ^= toggle
#         j = i

#     return inside | on_edge


# def _buffer_polygon_xy(points_xy, width: float) -> List[Tuple[float, float]]:
#     """Return a robust outward buffer ring for one SNT grid polygon."""
#     ring = _normalize_ring_xy(points_xy)
#     width = float(width)
#     if len(ring) < 3 or not np.isfinite(width) or width <= 0.0:
#         return []

#     from shapely.geometry import Polygon

#     polygon = Polygon(ring)
#     if not polygon.is_valid:
#         polygon = polygon.buffer(0)
#     if polygon.is_empty:
#         return []

#     buffered = polygon.buffer(width, join_style="mitre")
#     if buffered.is_empty:
#         return []
#     if buffered.geom_type == "MultiPolygon":
#         buffered = max(buffered.geoms, key=lambda geom: geom.area)
#     return _normalize_ring_xy(list(buffered.exterior.coords))


# class SNTFenceLoadWorker(QThread):
#     progress = Signal(int, str)
#     completed = Signal(dict)
#     failed = Signal(str)
#     cancelled = Signal(str)

#     def __init__(
#         self,
#         block_jobs: List[Dict[str, object]],
#         fence_polygon_xy: List[Tuple[float, float]],
#         fence_bbox: Tuple[float, float, float, float],
#         chunk_size: int = 1_000_000,
#         dedupe_boundary_points: bool = True,
#     ):
#         super().__init__()
#         self.block_jobs = list(block_jobs or [])
#         self.fence_polygon_xy = list(fence_polygon_xy or [])
#         self.fence_bbox = fence_bbox
#         self.chunk_size = max(10_000, int(chunk_size or 1_000_000))
#         self.dedupe_boundary_points = bool(dedupe_boundary_points)
#         self._cancelled = False
#         self.result_data: Optional[Dict[str, np.ndarray]] = None

#     def cancel(self):
#         self._cancelled = True

#     @staticmethod
#     def _has_naksha_user_data_marker(header) -> bool:
#         try:
#             vlrs = []
#             if hasattr(header, "vlrs") and header.vlrs:
#                 vlrs.extend(list(header.vlrs))
#             return any(
#                 (
#                     getattr(v, "user_id", "").strip() == "NakshaAI"
#                     and int(getattr(v, "record_id", -1)) == 1001
#                 )
#                 for v in vlrs
#             )
#         except Exception:
#             return False

#     @staticmethod
#     def _chunk_classification_array(chunk, *, prefer_user_data: bool, las_version: tuple[int, int]) -> np.ndarray:
#         if prefer_user_data and hasattr(chunk, "user_data"):
#             try:
#                 return np.asarray(chunk.user_data, dtype=np.uint8)
#             except Exception:
#                 pass

#         major, minor = las_version
#         if major == 1 and minor >= 4:
#             if hasattr(chunk, "classification"):
#                 return np.asarray(chunk.classification, dtype=np.uint8)
#             if hasattr(chunk, "raw_classification"):
#                 return np.asarray(chunk.raw_classification, dtype=np.uint8)
#             return np.zeros(len(chunk.x), dtype=np.uint8)

#         if hasattr(chunk, "raw_classification"):
#             raw = np.asarray(chunk.raw_classification, dtype=np.uint8)
#             if raw.size and int(raw.max()) > 31:
#                 return raw

#         if hasattr(chunk, "classification"):
#             return np.asarray(chunk.classification, dtype=np.uint8)
#         if hasattr(chunk, "raw_classification"):
#             return np.asarray(chunk.raw_classification, dtype=np.uint8)
#         return np.zeros(len(chunk.x), dtype=np.uint8)

#     def run(self):
#         try:
#             import laspy

#             started = time.time()
#             minx, miny, maxx, maxy = self.fence_bbox
#             total_jobs = max(1, len(self.block_jobs))
#             source_files: List[str] = []
#             for job in self.block_jobs:
#                 job_path = str(job.get("file_path") or "")
#                 if job_path:
#                     try:
#                         job_path = str(Path(job_path).resolve())
#                     except Exception:
#                         job_path = os.path.abspath(job_path)
#                 source_files.append(job_path)

#             xyz_parts: List[np.ndarray] = []
#             cls_parts: List[np.ndarray] = []
#             rgb_parts: List[np.ndarray] = []
#             intensity_parts: List[np.ndarray] = []
#             ret_no_parts: List[np.ndarray] = []
#             ret_cnt_parts: List[np.ndarray] = []
#             source_file_id_parts: List[np.ndarray] = []
#             source_point_idx_parts: List[np.ndarray] = []

#             has_rgb_any = False
#             has_intensity_any = False
#             has_return_no_any = False
#             has_return_cnt_any = False

#             block_logs: List[Dict[str, object]] = []
#             total_loaded_points = 0
#             total_inside_points = 0
#             failed_blocks: List[str] = []

#             self.progress.emit(10, "Loading selected blocks")

#             for block_idx, job in enumerate(self.block_jobs, start=1):
#                 if self._cancelled:
#                     self.cancelled.emit("Load-by-fence cancelled by user.")
#                     return

#                 file_path = str(job.get("file_path") or "")
#                 grid_name = str(job.get("grid_name") or Path(file_path).stem or f"Block {block_idx}")
#                 block_id = str(job.get("block_id") or grid_name)
#                 block_centroid = job.get("block_centroid")
#                 source_file_id = int(block_idx - 1)

#                 pct = int(10 + (70.0 * (block_idx - 1) / total_jobs))
#                 self.progress.emit(pct, f"Loading selected blocks ({block_idx}/{total_jobs})")

#                 block_loaded = 0
#                 block_inside = 0
#                 block_shift = (0.0, 0.0)

#                 try:
#                     with laspy.open(file_path) as reader:
#                         dims = {str(d).lower() for d in reader.header.point_format.dimension_names}
#                         has_cls = "classification" in dims
#                         has_user_data = "user_data" in dims
#                         has_intensity = "intensity" in dims
#                         has_ret_no = "return_number" in dims
#                         has_ret_cnt = "number_of_returns" in dims
#                         has_rgb = {"red", "green", "blue"}.issubset(dims)
#                         version = reader.header.version
#                         las_version = (
#                             tuple(map(int, str(version).split(".")))
#                             if isinstance(version, str)
#                             else (int(version.major), int(version.minor))
#                         )
#                         prefer_user_data = (
#                             has_user_data and self._has_naksha_user_data_marker(reader.header)
#                         )

#                         if block_centroid and len(block_centroid) >= 2:
#                             bx, by = float(block_centroid[0]), float(block_centroid[1])
#                             hx = (float(reader.header.x_min) + float(reader.header.x_max)) * 0.5
#                             hy = (float(reader.header.y_min) + float(reader.header.y_max)) * 0.5
#                             dx = bx - hx
#                             dy = by - hy
#                             if np.hypot(dx, dy) > 500.0:
#                                 block_shift = (dx, dy)

#                         for chunk in reader.chunk_iterator(self.chunk_size):
#                             if self._cancelled:
#                                 self.cancelled.emit("Load-by-fence cancelled by user.")
#                                 return

#                             x = np.asarray(chunk.x, dtype=np.float64)
#                             y = np.asarray(chunk.y, dtype=np.float64)
#                             z = np.asarray(chunk.z, dtype=np.float64)

#                             if block_shift != (0.0, 0.0):
#                                 x = x + block_shift[0]
#                                 y = y + block_shift[1]

#                             n_chunk = len(x)
#                             if n_chunk == 0:
#                                 continue

#                             chunk_start = block_loaded
#                             block_loaded += n_chunk
#                             total_loaded_points += n_chunk

#                             bbox_mask = (
#                                 (x >= minx) & (x <= maxx) &
#                                 (y >= miny) & (y <= maxy)
#                             )
#                             if not np.any(bbox_mask):
#                                 continue

#                             bbox_idx = np.flatnonzero(bbox_mask)
#                             inside_mask = _points_inside_polygon_mask(
#                                 x[bbox_idx], y[bbox_idx], self.fence_polygon_xy
#                             )
#                             if not np.any(inside_mask):
#                                 continue

#                             selected_idx = bbox_idx[inside_mask]
#                             n_sel = int(selected_idx.size)
#                             block_inside += n_sel
#                             total_inside_points += n_sel

#                             xyz_parts.append(
#                                 np.column_stack(
#                                     (x[selected_idx], y[selected_idx], z[selected_idx])
#                                 ).astype(np.float64, copy=False)
#                             )

#                             if has_cls or prefer_user_data:
#                                 cls_full = self._chunk_classification_array(
#                                     chunk,
#                                     prefer_user_data=prefer_user_data,
#                                     las_version=las_version,
#                                 )
#                                 cls_arr = cls_full[selected_idx]
#                             else:
#                                 cls_arr = np.zeros(n_sel, dtype=np.uint8)
#                             cls_parts.append(cls_arr)

#                             if has_intensity:
#                                 intensity_arr = np.asarray(chunk.intensity, dtype=np.float32)[selected_idx]
#                                 has_intensity_any = True
#                             else:
#                                 intensity_arr = np.zeros(n_sel, dtype=np.float32)
#                             intensity_parts.append(intensity_arr)

#                             if has_ret_no:
#                                 ret_no_arr = np.asarray(chunk.return_number, dtype=np.uint8)[selected_idx]
#                                 has_return_no_any = True
#                             else:
#                                 ret_no_arr = np.zeros(n_sel, dtype=np.uint8)
#                             ret_no_parts.append(ret_no_arr)

#                             if has_ret_cnt:
#                                 ret_cnt_arr = np.asarray(chunk.number_of_returns, dtype=np.uint8)[selected_idx]
#                                 has_return_cnt_any = True
#                             else:
#                                 ret_cnt_arr = np.zeros(n_sel, dtype=np.uint8)
#                             ret_cnt_parts.append(ret_cnt_arr)

#                             if has_rgb:
#                                 rr = np.asarray(chunk.red, dtype=np.uint32)[selected_idx]
#                                 gg = np.asarray(chunk.green, dtype=np.uint32)[selected_idx]
#                                 bb = np.asarray(chunk.blue, dtype=np.uint32)[selected_idx]
#                                 if rr.size > 0 and max(int(rr.max()), int(gg.max()), int(bb.max())) > 255:
#                                     rgb_arr = np.column_stack(
#                                         ((rr // 257).astype(np.uint8),
#                                          (gg // 257).astype(np.uint8),
#                                          (bb // 257).astype(np.uint8))
#                                     )
#                                 else:
#                                     rgb_arr = np.column_stack(
#                                         (rr.astype(np.uint8), gg.astype(np.uint8), bb.astype(np.uint8))
#                                     )
#                                 has_rgb_any = True
#                             else:
#                                 rgb_arr = np.zeros((n_sel, 3), dtype=np.uint8)
#                             rgb_parts.append(rgb_arr)
#                             source_file_id_parts.append(np.full(n_sel, source_file_id, dtype=np.int32))
#                             source_point_idx_parts.append(
#                                 selected_idx.astype(np.int64, copy=False) + np.int64(chunk_start)
#                             )

#                 except Exception as block_exc:
#                     failed_blocks.append(block_id)
#                     block_logs.append({
#                         "block_id": block_id,
#                         "grid_name": grid_name,
#                         "file_path": file_path,
#                         "error": str(block_exc),
#                         "loaded_points": int(block_loaded),
#                         "inside_points": int(block_inside),
#                     })
#                     continue

#                 block_logs.append({
#                     "block_id": block_id,
#                     "grid_name": grid_name,
#                     "file_path": file_path,
#                     "loaded_points": int(block_loaded),
#                     "inside_points": int(block_inside),
#                     "shift_xy": (float(block_shift[0]), float(block_shift[1])),
#                 })

#             self.progress.emit(88, "Filtering points")

#             if not xyz_parts:
#                 self.result_data = None
#                 self.completed.emit({
#                     "block_logs": block_logs,
#                     "failed_blocks": failed_blocks,
#                     "total_loaded_points": int(total_loaded_points),
#                     "total_inside_points": 0,
#                     "total_render_points": 0,
#                     "worker_seconds": float(time.time() - started),
#                 })
#                 return

#             xyz = np.concatenate(xyz_parts, axis=0)
#             classification = np.concatenate(cls_parts, axis=0).astype(np.uint8, copy=False)
#             intensity = np.concatenate(intensity_parts, axis=0).astype(np.float32, copy=False) if intensity_parts else None
#             rgb = np.concatenate(rgb_parts, axis=0).astype(np.uint8, copy=False) if rgb_parts else None
#             return_number = np.concatenate(ret_no_parts, axis=0).astype(np.uint8, copy=False) if ret_no_parts else None
#             number_of_returns = np.concatenate(ret_cnt_parts, axis=0).astype(np.uint8, copy=False) if ret_cnt_parts else None
#             source_file_ids = (
#                 np.concatenate(source_file_id_parts, axis=0).astype(np.int32, copy=False)
#                 if source_file_id_parts else np.zeros(xyz.shape[0], dtype=np.int32)
#             )
#             source_point_indices = (
#                 np.concatenate(source_point_idx_parts, axis=0).astype(np.int64, copy=False)
#                 if source_point_idx_parts else np.zeros(xyz.shape[0], dtype=np.int64)
#             )
#             duplicate_owner_indices = np.empty(0, dtype=np.int64)
#             duplicate_source_file_ids = np.empty(0, dtype=np.int32)
#             duplicate_source_point_indices = np.empty(0, dtype=np.int64)

#             if self.dedupe_boundary_points and xyz.shape[0] > 0 and len(self.block_jobs) > 1:
#                 try:
#                     xyz_key = np.ascontiguousarray(xyz).view(
#                         np.dtype([("x", np.float64), ("y", np.float64), ("z", np.float64)])
#                     ).reshape(-1)
#                     _, uniq_idx, inverse = np.unique(xyz_key, return_index=True, return_inverse=True)
#                     if uniq_idx.size != xyz.shape[0]:
#                         uniq_idx = np.sort(uniq_idx)
#                         keep_mask = np.zeros(xyz.shape[0], dtype=bool)
#                         keep_mask[uniq_idx] = True
#                         duplicate_idx = np.flatnonzero(~keep_mask)

#                         if duplicate_idx.size > 0:
#                             group_to_new_idx = np.empty(int(uniq_idx.size), dtype=np.int64)
#                             group_to_new_idx[inverse[uniq_idx]] = np.arange(uniq_idx.size, dtype=np.int64)
#                             duplicate_owner_indices = group_to_new_idx[inverse[duplicate_idx]]
#                             duplicate_source_file_ids = source_file_ids[duplicate_idx].astype(np.int32, copy=False)
#                             duplicate_source_point_indices = source_point_indices[duplicate_idx].astype(np.int64, copy=False)

#                         xyz = xyz[uniq_idx]
#                         classification = classification[uniq_idx]
#                         source_file_ids = source_file_ids[uniq_idx]
#                         source_point_indices = source_point_indices[uniq_idx]
#                         if intensity is not None:
#                             intensity = intensity[uniq_idx]
#                         if rgb is not None:
#                             rgb = rgb[uniq_idx]
#                         if return_number is not None:
#                             return_number = return_number[uniq_idx]
#                         if number_of_returns is not None:
#                             number_of_returns = number_of_returns[uniq_idx]
#                 except Exception:
#                     pass

#             self.result_data = {
#                 "xyz": xyz,
#                 "classification": classification,
#             }
#             if has_rgb_any and rgb is not None:
#                 self.result_data["rgb"] = rgb
#             if has_intensity_any and intensity is not None:
#                 self.result_data["intensity"] = intensity
#             if has_return_no_any and return_number is not None:
#                 self.result_data["return_number"] = return_number
#             if has_return_cnt_any and number_of_returns is not None:
#                 self.result_data["number_of_returns"] = number_of_returns
#             self.result_data["_fence_source_files"] = source_files
#             self.result_data["_fence_source_file_ids"] = source_file_ids
#             self.result_data["_fence_source_point_indices"] = source_point_indices
#             self.result_data["_fence_duplicate_owner_indices"] = duplicate_owner_indices
#             self.result_data["_fence_duplicate_source_file_ids"] = duplicate_source_file_ids
#             self.result_data["_fence_duplicate_source_point_indices"] = duplicate_source_point_indices

#             self.progress.emit(98, "Rendering selected area")
#             self.completed.emit({
#                 "block_logs": block_logs,
#                 "failed_blocks": failed_blocks,
#                 "total_loaded_points": int(total_loaded_points),
#                 "total_inside_points": int(total_inside_points),
#                 "total_render_points": int(xyz.shape[0]),
#                 "worker_seconds": float(time.time() - started),
#             })

#         except Exception as exc:
#             self.failed.emit(str(exc))

# class GridLabelManager:
#     """
#     Manages grid label clicking and automatic LAZ/LAS loading.
#     Implements hyperlink-style behavior for DXF grid labels.
#     """
    
#     def __init__(self, app):
#         self.app = app
#         self.settings = QSettings("NakshaAI", "LidarApp")
        
#         # Cache of DXF folder -> LAZ/LAS folder mappings
#         self.folder_cache = {}
#         self.loaded_grids = {}
#         # clicked label -> actual loaded label (file stem)
#         self.grid_aliases = {}
#         # ✨ NEW: Track highlighted labels
#         self.highlighted_label = None
#         self.original_colors = {}  # Store original colors for restoration
#         # Local undo history for grid/block-name label text & font-size
#         # edits (see _edit_grid_label / _edit_all_grid_labels_font_size).
#         # Deliberately separate from the digitizer's own undo_stack, which
#         # only knows how to snapshot digitizer "drawings" — grid labels are
#         # a different kind of object, so we keep a small dedicated stack
#         # here instead of risking a crash inside unrelated undo code.
#         self._label_edit_undo_stack = []
#         self._label_edit_undo_limit = 20
#         self._interactor_observer_ids = {}
#         self._fence_mode_active = False
#         self._fence_polyline_style_backup = None
#         self._fence_worker = None
#         self._fence_progress = None
#         self._fence_last_stats = None
#         self._fence_last_data_count = 0
#         self._fence_operation_label = "Load by Fence"
#         self._fence_chunk_size = max(100_000, int(os.getenv("NAKSHA_SNT_FENCE_CHUNK_SIZE", "1000000")))
        
#         print("✅ Grid Label Manager initialized")
    
#     def setup_interactor(self):
#         """Attach right-click observer to main VTK widget"""
#         if not hasattr(self.app, 'vtk_widget'):
#             print("⚠️ No VTK widget found")
#             return
        
#         self.ensure_interactor_observers()
        
#         # ✨ NEW: Add hover detection for visual feedback
        
#         print("✅ Grid label click detection enabled")
            
#     def ensure_interactor_observers(self):
#         """Re-install managed observers after another tool clears interactor callbacks."""
#         if getattr(self.app, "_shutdown_in_progress", False):
#             return
        
#         # ✅ FIX: Don't reinstall if element selection is active
#         if getattr(self, "_element_select_active", False):
#             print("⚠️ Grid label observers NOT reinstalled (element selection active)")
#             return

#         handles = self._get_main_interactor_handles()
#         if handles is None:
#             return

#         _, interactor, _ = handles
#         managed = {
#             "RightButtonPressEvent": self.on_right_click,
#             "MouseMoveEvent": self.on_mouse_move,
#         }
#         if hasattr(self, 'on_key_press'):
#             managed["KeyPressEvent"] = self.on_key_press

#         for event_name, callback in managed.items():
#             old_id = self._interactor_observer_ids.get(event_name)
#             if old_id is not None:
#                 try:
#                     interactor.RemoveObserver(old_id)
#                 except Exception:
#                     pass

#             priority = 2.0 if event_name == "RightButtonPressEvent" else 0.0
#             self._interactor_observer_ids[event_name] = interactor.AddObserver(
#                 event_name, callback, priority
#             )

#     def remove_interactor_observers(self):
#         """Best-effort detach of managed interactor observers."""
#         handles = self._get_main_interactor_handles()
#         interactor = handles[1] if handles is not None else None
#         for event_name, obs_id in list(self._interactor_observer_ids.items()):
#             if obs_id is None or interactor is None:
#                 continue
#             try:
#                 interactor.RemoveObserver(obs_id)
#             except Exception:
#                 pass
#         self._interactor_observer_ids.clear()

#     def _get_main_interactor_handles(self):
#         """
#         Return `(vtk_widget, interactor, renderer)` when all handles are valid.
#         Returns `None` during teardown or before widget initialization.
#         """
#         if getattr(self.app, "_shutdown_in_progress", False):
#             return None
#         vtk_widget = getattr(self.app, "vtk_widget", None)
#         if vtk_widget is None:
#             return None
#         interactor = getattr(vtk_widget, "interactor", None)
#         renderer = getattr(vtk_widget, "renderer", None)
#         if interactor is None or renderer is None:
#             return None
#         return vtk_widget, interactor, renderer

#     def _consume_vtk_event(self, obj):
#         if obj is None:
#             return

#         try:
#             if hasattr(obj, 'AbortFlagOn'):
#                 obj.AbortFlagOn()
#             elif hasattr(obj, 'SetAbortFlag'):
#                 try:
#                     obj.SetAbortFlag(1)
#                 except TypeError:
#                     obj.SetAbortFlag(True)
#         except Exception:
#             pass

#     def on_mouse_move(self, obj, event):
#         """Highlight labels on hover"""
#         handles = self._get_main_interactor_handles()
#         if handles is None:
#             return 0

#         # ✅ While a camera interaction (pan/zoom/rotate) is active, skip the
#         # hover area-pick — it runs on every mouse-move tick and its extra
#         # update() during a drag makes pan/zoom feel sluggish. Hover highlight
#         # resumes once the gesture ends.
#         mgr = getattr(self.app, "gpu_render_manager", None)
#         if mgr is not None and (
#             getattr(mgr, "_interaction_active", False)
#             or getattr(mgr, "_pan_in_progress", False)
#         ):
#             return 0

#         vtk_widget, interactor, renderer = handles
#         clickPos = interactor.GetEventPosition()
        
#         # Use area picker for better detection
#         area_picker = vtk.vtkAreaPicker()
#         x, y = clickPos
#         area_picker.AreaPick(x-10, y-10, x+10, y+10, renderer)
        
#         found_label = None
#         for prop in area_picker.GetProp3Ds():
#             if hasattr(prop, 'is_grid_label') and prop.is_grid_label:
#                 found_label = prop
#                 break
        
#         # Update highlighting
#         if found_label != self.highlighted_label:
#             try:
#                 if self.highlighted_label:
#                     self._unhighlight_label(self.highlighted_label)
#                 if found_label:
#                     self._highlight_label(found_label)
#                 self.highlighted_label = found_label
#                 vtk_widget.update()
#             except Exception:
#                 self.highlighted_label = None

#     def _highlight_label(self, actor):
#         """Apply highlight effect to label"""
#         try:
#             if actor is None or not hasattr(actor, "GetProperty"):
#                 return
#             prop = actor.GetProperty()
#             if prop is None:
#                 return
#             if actor not in self.original_colors:
#                 self.original_colors[actor] = {
#                     'color': prop.GetColor(),
#                     'opacity': prop.GetOpacity()
#                 }

#             # Apply bright highlight
#             prop.SetColor(1.0, 1.0, 0.0)  # Bright yellow
#             prop.SetOpacity(1.0)

#             # Make it slightly bigger/bolder if possible
#             if hasattr(actor, 'GetMapper'):
#                 mapper = actor.GetMapper()
#                 if mapper:
#                     mapper.ScalarVisibilityOff()
#         except Exception:
#             self.original_colors.pop(actor, None)

#     def _unhighlight_label(self, actor):
#         """Remove highlight effect from label"""
#         try:
#             if actor not in self.original_colors or actor is None or not hasattr(actor, "GetProperty"):
#                 return
#             prop = actor.GetProperty()
#             if prop is None:
#                 self.original_colors.pop(actor, None)
#                 return
#             orig = self.original_colors[actor]
#             prop.SetColor(*orig['color'])
#             prop.SetOpacity(orig['opacity'])
#         except Exception:
#             pass

#     def _ensure_snt_block_index(self) -> List[Dict[str, object]]:
#         """
#         Ensure app.snt_block_polygons is populated when SNT attachments exist.
#         Keeps existing behavior intact and only rebuilds lazily when index is empty.
#         """
#         block_polygons = getattr(self.app, "snt_block_polygons", None) or []
#         if block_polygons:
#             return block_polygons

#         attachments = list(getattr(self.app, "snt_attachments", []) or [])
#         if not attachments:
#             return []

#         try:
#             from gui.snt_attachment import build_snt_block_polygons
#         except Exception as exc:
#             print(f"[LoadByFence] Could not import block-index builder: {exc}")
#             return []

#         rebuilt = 0
#         for idx, att in enumerate(attachments, start=1):
#             if not isinstance(att, dict):
#                 continue
#             try:
#                 entities = att.get("entities") or []
#                 if not entities:
#                     parsed = att.get("parsed")
#                     if isinstance(parsed, dict):
#                         entities = parsed.get("entities") or []
#                 if not entities:
#                     continue

#                 snt_name = (
#                     str(att.get("full_path") or "").strip()
#                     or str(att.get("filename") or "").strip()
#                     or f"SNT_{idx:03d}"
#                 )
#                 build_snt_block_polygons(self.app, entities, snt_name)
#                 rebuilt += 1
#             except Exception as exc:
#                 print(f"[LoadByFence] Block-index rebuild skipped for attachment #{idx}: {exc}")

#         block_polygons = getattr(self.app, "snt_block_polygons", None) or []
#         if block_polygons:
#             print(
#                 f"[LoadByFence] Rebuilt SNT block index from {rebuilt} attachment(s): "
#                 f"{len(block_polygons)} block polygon(s)"
#             )
#         return block_polygons

#     def _resolve_primary_buffer_block(self, grid_name, snt_filename=None, file_path=None):
#         """Resolve the exact selected grid polygon, preferring owner/path matches."""
#         wanted_grid = str(grid_name or "").strip()
#         wanted_owner = self._normalize_snt_filename(snt_filename)
#         wanted_path = os.path.normcase(os.path.abspath(str(file_path))) if file_path else ""
#         ranked = []

#         for block in self._ensure_snt_block_index():
#             if not isinstance(block, dict):
#                 continue
#             block_grid = str(block.get("grid_name") or "").strip()
#             names = [block_grid, str(block.get("block_file") or "").strip()]
#             names.extend(str(name or "").strip() for name in (block.get("alt_names") or []))
#             identity_match = any(
#                 name and wanted_grid and (
#                     name.casefold() == wanted_grid.casefold()
#                     or names_share_numeric_identity(name, wanted_grid)
#                 )
#                 for name in names
#             )
#             if not identity_match:
#                 continue

#             score = 10
#             block_owner = self._normalize_snt_filename(block.get("snt_filename"))
#             if wanted_owner and block_owner == wanted_owner:
#                 score += 20
#             block_path = block.get("file_path")
#             if wanted_path and block_path:
#                 try:
#                     if os.path.normcase(os.path.abspath(str(block_path))) == wanted_path:
#                         score += 40
#                 except Exception:
#                     pass
#             if block_grid.casefold() == wanted_grid.casefold():
#                 score += 5
#             ranked.append((score, block))

#         if not ranked:
#             return None
#         ranked.sort(key=lambda item: item[0], reverse=True)
#         return ranked[0][1]

#     def _save_current_before_buffer_load(self) -> bool:
#         """Protect the current editable dataset before replacing it."""
#         data = getattr(self.app, "data", None)
#         if not isinstance(data, dict) or data.get("xyz") is None or len(data.get("xyz")) == 0:
#             return True

#         try:
#             from gui.save_pointcloud import has_fenced_parent_writeback, save_pointcloud, save_pointcloud_quick

#             if has_fenced_parent_writeback(self.app):
#                 saved = save_pointcloud(self.app, path=None, show_dialog=False)
#             else:
#                 save_path = getattr(self.app, "last_save_path", None) or getattr(self.app, "loaded_file", None)
#                 saved = save_pointcloud_quick(self.app, save_path) if save_path else False
#             if saved:
#                 return True
#         except Exception as exc:
#             print(f"[OpenWithBuffer] Current-data save failed: {exc}")

#         reply = QMessageBox.question(
#             self.app,
#             "Current Data Not Saved",
#             "The currently displayed point data could not be saved automatically.\n\n"
#             "Continue opening the buffered block and replace the current view?",
#             QMessageBox.Yes | QMessageBox.No,
#             QMessageBox.No,
#         )
#         return reply == QMessageBox.Yes

#     def open_grid_with_buffer_points(self, grid_name, snt_filename=None, alt_names=None, file_path=None):
#         """Load a primary grid plus neighboring edge strips with source ownership."""
#         from PySide6.QtWidgets import QInputDialog

#         if self._fence_worker is not None and self._fence_worker.isRunning():
#             QMessageBox.information(self.app, "Open Block with Buffer", "Another multi-file load is already running.")
#             return

#         primary = self._resolve_primary_buffer_block(grid_name, snt_filename, file_path)
#         if primary is None:
#             QMessageBox.warning(
#                 self.app,
#                 "Open Block with Buffer",
#                 f"Could not resolve the boundary polygon for block '{grid_name}'.",
#             )
#             return

#         default_width = float(self.settings.value("grid_neighbor_buffer_width", 10.0) or 10.0)
#         width, accepted = QInputDialog.getDouble(
#             self.app,
#             "Open Block with Buffer Points",
#             "Neighbor buffer width (project units / metres):",
#             default_width,
#             0.01,
#             10000.0,
#             2,
#         )
#         if not accepted:
#             return
#         width = float(width)
#         self.settings.setValue("grid_neighbor_buffer_width", width)

#         try:
#             buffered_polygon = _buffer_polygon_xy(primary.get("points_2d", []), width)
#         except Exception as exc:
#             QMessageBox.critical(self.app, "Open Block with Buffer", f"Could not create the grid buffer:\n\n{exc}")
#             return
#         if len(buffered_polygon) < 3:
#             QMessageBox.warning(self.app, "Open Block with Buffer", "The selected grid has an invalid boundary polygon.")
#             return

#         # This polygon was created from PRJ boundary coordinates and is already
#         # in the world coordinate system. Do not transform it a second time.
#         scan = self.get_snt_blocks_intersecting_polygon(
#             buffered_polygon,
#             coordinates_are_world=True,
#         )
#         candidate_blocks = list(scan.get("intersecting_blocks", []) or [])
#         if not candidate_blocks:
#             QMessageBox.information(self.app, "Open Block with Buffer", "No grid files intersect the requested buffer.")
#             return

#         las_folder = self._resolve_las_folder_for_fence()
#         if las_folder is None:
#             QMessageBox.warning(self.app, "Open Block with Buffer", "Could not locate the LAZ/LAS folder for these grids.")
#             return

#         jobs, missing_blocks = self._resolve_candidate_block_jobs(candidate_blocks, las_folder)
#         if not jobs:
#             QMessageBox.information(self.app, "Open Block with Buffer", "No matching LAZ/LAS files were found.")
#             return
#         if not self._save_current_before_buffer_load():
#             return

#         self._fence_operation_label = "Open Block with Buffer"
#         self._start_fence_worker(
#             jobs=jobs,
#             fence_polygon=buffered_polygon,
#             fence_bbox=scan.get("fence_bbox"),
#             base_stats={
#                 "operation": "buffered_grid",
#                 "primary_grid": str(grid_name or primary.get("grid_name") or ""),
#                 "buffer_width": width,
#                 "block_scan_seconds": 0.0,
#                 "total_blocks": int(scan.get("total_blocks", 0)),
#                 "bbox_candidates": int(scan.get("bbox_candidates", 0)),
#                 "intersecting_count": len(candidate_blocks),
#                 "missing_blocks": missing_blocks,
#             },
#         )

#     def activate_load_by_fence_tool(self):
#         """
#         Activate one-shot fence mode:
#         1) switch digitizer to polyline
#         2) wait for closed polygon
#         3) load only points inside that fence from intersecting SNT blocks
#         """
#         if self._fence_worker is not None and self._fence_worker.isRunning():
#             QMessageBox.information(self.app, "Load by Fence", "A fence load is already running.")
#             return

#         digitizer = getattr(self.app, "digitizer", None)
#         if digitizer is None:
#             QMessageBox.warning(self.app, "Load by Fence", "Digitizer tool is not available.")
#             return

#         # Mirror the user's manual workaround: entering the Draw tab re-enables
#         # the digitizer and restores the expected observer/input ownership.
#         try:
#             if hasattr(self.app, "_handle_menu_click"):
#                 self.app._handle_menu_click("draw")
#             elif hasattr(self.app, "_enter_draw_tab_mode"):
#                 self.app._enter_draw_tab_mode()
#             elif hasattr(self.app, "enable_digitizer_mode"):
#                 self.app.enable_digitizer_mode()
#         except Exception as draw_mode_exc:
#             print(f"[LoadByFence] Draw-mode activation fallback failed: {draw_mode_exc}")

#         try:
#             digitizer.enabled = True
#         except Exception:
#             pass

#         block_polygons = self._ensure_snt_block_index()
#         if not block_polygons:
#             QMessageBox.warning(
#                 self.app,
#                 "Load by Fence",
#                 "No SNT block index found.\nAttach an SNT first, then try Load by Fence.",
#             )
#             return

#         self.deactivate_load_by_fence_tool()
#         self._fence_mode_active = True

#         try:
#             styles = getattr(digitizer, "draw_tool_styles", {}) or {}
#             current = dict(styles.get("polyline", {}))
#             self._fence_polyline_style_backup = current
#             styles.setdefault("polyline", {})
#             styles["polyline"]["color"] = (1.0, 0.0, 0.0)
#             styles["polyline"]["width"] = max(2, int(styles["polyline"].get("width", 3)))
#             styles["polyline"]["style"] = "solid"
#         except Exception:
#             self._fence_polyline_style_backup = None

#         try:
#             digitizer.remove_drawing_finalized_callback(self._on_fence_drawing_finalized)
#         except Exception:
#             pass
#         digitizer.add_drawing_finalized_callback(self._on_fence_drawing_finalized)

#         digitizer.set_tool("Polyline")
#         if hasattr(self.app, "statusBar"):
#             self.app.statusBar().showMessage(
#                 "Load by Fence: left-click to draw fence polygon, right-click to finish.",
#                 7000,
#             )

#     def deactivate_load_by_fence_tool(self, keep_tool: bool = False):
#         digitizer = getattr(self.app, "digitizer", None)
#         if digitizer is not None:
#             try:
#                 digitizer.remove_drawing_finalized_callback(self._on_fence_drawing_finalized)
#             except Exception:
#                 pass
#             try:
#                 if self._fence_polyline_style_backup is not None:
#                     styles = getattr(digitizer, "draw_tool_styles", {}) or {}
#                     styles.setdefault("polyline", {})
#                     styles["polyline"].update(self._fence_polyline_style_backup)
#             except Exception:
#                 pass
#             if not keep_tool and getattr(digitizer, "active_tool", None) == "polyline":
#                 try:
#                     digitizer.set_tool(None)
#                 except Exception:
#                     pass

#         self._fence_mode_active = False
#         self._fence_polyline_style_backup = None

#     def _on_fence_drawing_finalized(self, drawing_entry):
#         if not self._fence_mode_active:
#             return
#         if not isinstance(drawing_entry, dict):
#             return

#         drawing_type = str(drawing_entry.get("type", "")).lower()
#         if drawing_type not in {"polyline", "polygon", "rectangle", "freehand", "circle"}:
#             return

#         fence_polygon = self.collect_fence_polygon(drawing_entry)
#         if len(fence_polygon) < 3:
#             QMessageBox.warning(
#                 self.app,
#                 "Load by Fence",
#                 "Fence needs at least 3 vertices.\nDraw a larger closed polygon.",
#             )
#             return

#         self._tint_fence_drawing_actor(drawing_entry.get("actor"))
#         self.deactivate_load_by_fence_tool(keep_tool=True)
#         try:
#             self.load_points_from_snt_by_fence(fence_polygon)
#         except Exception as exc:
#             print(f"[LoadByFence] callback error: {exc}")
#             print(traceback.format_exc())
#             QMessageBox.critical(
#                 self.app,
#                 "Load by Fence",
#                 f"Load by fence failed:\n\n{exc}",
#             )

#     def collect_fence_polygon(self, drawing_entry) -> List[Tuple[float, float]]:
#         coords = []
#         if isinstance(drawing_entry, dict):
#             coords = drawing_entry.get("coords", []) or []
#         return _normalize_ring_xy(coords)

#     def screen_to_world_polygon(self, polygon_points) -> List[Tuple[float, float]]:
#         # Digitizer stores coordinates in world space already.
#         return _normalize_ring_xy(polygon_points)

#     def _tint_fence_drawing_actor(self, actor):
#         if actor is None or not hasattr(actor, "GetProperty"):
#             return
#         try:
#             prop = actor.GetProperty()
#             prop.SetColor(1.0, 0.1, 0.1)
#             prop.SetLineWidth(max(3.0, float(prop.GetLineWidth() or 3.0)))
#             prop.SetOpacity(1.0)
#             if hasattr(self.app, "vtk_widget"):
#                 self.app.vtk_widget.render()
#         except Exception:
#             pass

#     def get_snt_blocks_intersecting_polygon(
#         self,
#         fence_polygon_world: List[Tuple[float, float]],
#         *,
#         coordinates_are_world: bool = False,
#     ):
#         fence_polygon = (
#             _normalize_ring_xy(fence_polygon_world)
#             if coordinates_are_world
#             else self.screen_to_world_polygon(fence_polygon_world)
#         )
#         if len(fence_polygon) < 3:
#             return {
#                 "fence_polygon": fence_polygon,
#                 "fence_bbox": None,
#                 "total_blocks": 0,
#                 "bbox_candidates": 0,
#                 "intersecting_blocks": [],
#             }

#         fence_bbox = _polygon_bounds_xy(fence_polygon)
#         blocks = getattr(self.app, "snt_block_polygons", None) or []
#         intersecting: List[Dict[str, object]] = []
#         bbox_candidates = 0

#         for idx, blk in enumerate(blocks):
#             block_poly = _normalize_ring_xy(blk.get("points_2d", []))
#             if len(block_poly) < 3:
#                 continue
#             block_bbox = _polygon_bounds_xy(block_poly)
#             if not _bbox_intersects(block_bbox, fence_bbox):
#                 continue
#             bbox_candidates += 1
#             if not _polygons_intersect_xy(fence_polygon, block_poly):
#                 continue

#             grid_name = str(blk.get("grid_name") or "").strip()
#             block_id = grid_name or f"BLOCK_{idx + 1:04d}"
#             xs = [p[0] for p in block_poly]
#             ys = [p[1] for p in block_poly]
#             centroid = (float(sum(xs) / len(xs)), float(sum(ys) / len(ys)))

#             intersecting.append({
#                 "block_id": block_id,
#                 "grid_name": grid_name,
#                 "snt_filename": blk.get("snt_filename"),
#                 "block_file": blk.get("block_file"),
#                 "file_path": blk.get("file_path"),
#                 "alt_names": list(blk.get("alt_names") or []),
#                 "points_2d": block_poly,
#                 "bbox": block_bbox,
#                 "centroid": centroid,
#             })

#         return {
#             "fence_polygon": fence_polygon,
#             "fence_bbox": fence_bbox,
#             "total_blocks": len(blocks),
#             "bbox_candidates": bbox_candidates,
#             "intersecting_blocks": intersecting,
#         }

#     def _find_las_folder_near_path(self, seed_path: Path):
#         if seed_path is None:
#             return None
#         if not seed_path.exists():
#             return None

#         root = seed_path if seed_path.is_dir() else seed_path.parent
#         if root is None or (not root.exists()):
#             return None

#         # Check root first, then common subfolder names
#         candidates = [root]
#         for sub_name in ("lazz", "LAZZ", "laz", "LAZ", "las", "LAS"):
#             candidates.append(root / sub_name)

#         # Also check ALL immediate subfolders for LAZ/LAS files
#         try:
#             for entry in root.iterdir():
#                 if entry.is_dir() and entry not in candidates:
#                     candidates.append(entry)
#         except Exception:
#             pass

#         for folder in candidates:
#             try:
#                 if folder.exists() and folder.is_dir():
#                     files = list(folder.glob("*.laz")) + list(folder.glob("*.las"))
#                     if files:
#                         return folder
#             except Exception:
#                 continue
#         return None

#     def _normalize_snt_filename(self, value):
#         """Normalize an SNT filename/path for safe comparison."""
#         if not value:
#             return ""
#         try:
#             return Path(str(value)).name.strip().lower()
#         except Exception:
#             return os.path.basename(str(value)).strip().lower()

#     def _normalize_snt_stem(self, value):
#         """Normalize SNT stem for fallback comparison."""
#         if not value:
#             return ""
#         try:
#             return Path(str(value)).stem.strip().lower()
#         except Exception:
#             return os.path.splitext(os.path.basename(str(value)))[0].strip().lower()

#     def _iter_snt_attachment_records(self):
#         """Yield all attached SNT records from app.snt_attachments and app.snt_actors."""
#         seen = set()
#         for store_name in ("snt_attachments", "snt_actors"):
#             for record in list(getattr(self.app, store_name, []) or []):
#                 if not isinstance(record, dict):
#                     continue
#                 filename = record.get("filename") or ""
#                 full_path = record.get("full_path") or ""
#                 key = (
#                     self._normalize_snt_filename(filename),
#                     self._normalize_snt_filename(full_path),
#                     self._normalize_snt_stem(filename),
#                     self._normalize_snt_stem(full_path),
#                 )
#                 if key in seen:
#                     continue
#                 seen.add(key)
#                 yield record

#     def _find_snt_attachment_record(self, snt_filename):
#         """Return the attachment record that owns the requested SNT filename."""
#         wanted_name = self._normalize_snt_filename(snt_filename)
#         wanted_stem = self._normalize_snt_stem(snt_filename)
#         if not wanted_name and not wanted_stem:
#             return None
#         for record in self._iter_snt_attachment_records():
#             record_name = self._normalize_snt_filename(record.get("filename"))
#             record_full_name = self._normalize_snt_filename(record.get("full_path"))
#             record_stem = self._normalize_snt_stem(record.get("filename"))
#             record_full_stem = self._normalize_snt_stem(record.get("full_path"))
#             if wanted_name and wanted_name in {record_name, record_full_name}:
#                 return record
#             if wanted_stem and wanted_stem in {record_stem, record_full_stem}:
#                 return record
#         return None

#     def _find_las_folder_from_snt(self, snt_filename):
#         """Resolve LAZ/LAS folder from the owning SNT file."""
#         record = self._find_snt_attachment_record(snt_filename)
#         if not record:
#             print(f"   ⚠️ SNT owner not found for: {snt_filename}")
#             return None
#         raw = record.get("full_path") or record.get("filename")
#         if not raw:
#             print(f"   ⚠️ SNT owner has no path: {snt_filename}")
#             return None
#         snt_path = Path(str(raw))
#         print("📋 STRATEGY SNT: Auto-detect from SNT owner")
#         print(f"   SNT owner: {snt_path.name}")
#         print(f"   SNT path: {snt_path}")
#         folder = self._find_las_folder_near_path(snt_path)
#         if folder is not None:
#             print(f"   ✅ FOUND LAZ/LAS folder for SNT: {folder}")
#             self._save_folder_to_settings(folder)
#             return folder
#         print(f"   ❌ No LAZ/LAS folder found near SNT: {snt_path}")
#         return None

#     def _infer_snt_filename_for_grid(self, grid_name):
#         """Infer SNT owner from app.snt_block_polygons when the label lacks metadata."""
#         if not grid_name:
#             return None
#         wanted = str(grid_name).strip().lower()
#         owners = []
#         for block in list(getattr(self.app, "snt_block_polygons", []) or []):
#             if not isinstance(block, dict):
#                 continue
#             block_grid = str(block.get("grid_name") or "").strip().lower()
#             if block_grid != wanted:
#                 continue
#             owner = block.get("snt_filename")
#             if owner and owner not in owners:
#                 owners.append(owner)
#         if len(owners) == 1:
#             return owners[0]
#         if len(owners) > 1:
#             print(f"   ⚠️ Ambiguous grid owner for '{grid_name}': {', '.join(str(x) for x in owners)}")
#         return None

#     def _resolve_las_folder_for_fence(self):
#         for att in self._iter_snt_attachment_records():
#             raw = att.get("full_path") or att.get("filename")
#             if not raw:
#                 continue
#             p = Path(str(raw))
#             folder = self._find_las_folder_near_path(p)
#             if folder is not None:
#                 self._save_folder_to_settings(folder)
#                 return folder

#         dxf_folder = self._find_las_folder_from_dxf()
#         if dxf_folder is not None:
#             return dxf_folder

#         saved = self.settings.value("last_las_folder", "")
#         if saved:
#             p = Path(str(saved))
#             if p.exists() and p.is_dir():
#                 files = list(p.glob("*.laz")) + list(p.glob("*.las"))
#                 if files:
#                     return p

#         return self._prompt_user_for_las_folder()

#     def _resolve_las_file_for_grid_name(self, all_files: List[Path], grid_name: str) -> Optional[Path]:
#         return find_matching_lidar_file(all_files, grid_name)

#     def _resolve_candidate_block_jobs(self, candidate_blocks: List[Dict[str, object]], las_folder: Path):
#         jobs: List[Dict[str, object]] = []
#         missing: List[str] = []
#         used_files: set = set()
#         folder_file_cache: Dict[str, List[Path]] = {}

#         def _files_for_folder(folder: Path) -> List[Path]:
#             if folder is None:
#                 return []
#             folder_key = str(Path(folder).resolve()).lower()
#             if folder_key not in folder_file_cache:
#                 folder_file_cache[folder_key] = list(Path(folder).glob("*.laz")) + list(Path(folder).glob("*.las"))
#             return folder_file_cache[folder_key]

#         for blk in candidate_blocks:
#             grid_name = str(blk.get("grid_name") or "").strip()
#             block_id = str(blk.get("block_id") or grid_name or "BLOCK")
#             snt_filename = str(blk.get("snt_filename") or "").strip()
#             snt_stem = Path(snt_filename).stem if snt_filename else ""
#             owner_folder = self._find_las_folder_from_snt(snt_filename) if snt_filename else None
#             search_folder = owner_folder or las_folder
#             all_files = _files_for_folder(search_folder)

#             laz_file = None
#             direct_path = str(blk.get("file_path") or "").strip()
#             if direct_path:
#                 direct_candidate = Path(direct_path)
#                 if direct_candidate.exists() and direct_candidate.suffix.lower() in {".las", ".laz"}:
#                     laz_file = direct_candidate
#             candidate_names = [grid_name, block_id, snt_stem]
#             block_file = str(blk.get("block_file") or "").strip()
#             if block_file and block_file not in candidate_names:
#                 candidate_names.append(block_file)
#             for alt_name in (blk.get("alt_names") or []):
#                 alt_name = str(alt_name or "").strip()
#                 if alt_name and alt_name not in candidate_names:
#                     candidate_names.append(alt_name)
#             for candidate_name in candidate_names:
#                 if laz_file is not None:
#                     break
#                 if not candidate_name:
#                     continue
#                 laz_file = self._resolve_las_file_for_grid_name(all_files, candidate_name)
#                 if laz_file is None and search_folder is not None:
#                     laz_file = self._find_matching_las_file(search_folder, candidate_name)
#                 if laz_file is not None:
#                     break
#             if laz_file is None:
#                 missing.append(block_id)
#                 continue

#             key = str(laz_file.resolve()).lower()
#             if key in used_files:
#                 continue
#             used_files.add(key)

#             jobs.append({
#                 "block_id": block_id,
#                 "grid_name": grid_name or laz_file.stem,
#                 "file_path": str(laz_file),
#                 "block_centroid": blk.get("centroid"),
#                 "snt_filename": snt_filename,
#             })

#         return jobs, missing

#     def load_points_from_snt_by_fence(self, fence_polygon_world):
#         self._fence_operation_label = "Load by Fence"
#         if not fence_polygon_world:
#             QMessageBox.warning(self.app, "Load by Fence", "Fence polygon is empty.")
#             return

#         t_scan = time.time()
#         scan = self.get_snt_blocks_intersecting_polygon(fence_polygon_world)
#         fence_polygon = scan.get("fence_polygon") or []
#         fence_bbox = scan.get("fence_bbox")
#         total_blocks = int(scan.get("total_blocks", 0))
#         bbox_candidates = int(scan.get("bbox_candidates", 0))
#         intersecting_blocks = list(scan.get("intersecting_blocks", []) or [])
#         block_scan_seconds = float(time.time() - t_scan)

#         print(f"[LoadByFence] SNT folder candidates scanned: {total_blocks}")
#         print(f"[LoadByFence] Fence polygon world coordinates: {fence_polygon}")
#         if fence_bbox:
#             print(
#                 f"[LoadByFence] Fence bbox: minx={fence_bbox[0]:.3f}, miny={fence_bbox[1]:.3f}, "
#                 f"maxx={fence_bbox[2]:.3f}, maxy={fence_bbox[3]:.3f}"
#             )
#         print(f"[LoadByFence] Candidate blocks by bbox: {bbox_candidates}")
#         print(f"[LoadByFence] Intersecting blocks: {len(intersecting_blocks)}")
#         if intersecting_blocks:
#             names = [str(b.get("block_id", "")) for b in intersecting_blocks]
#             print(f"[LoadByFence] Intersecting block IDs: {', '.join(names)}")

#         if not intersecting_blocks:
#             QMessageBox.information(self.app, "Load by Fence", "No SNT blocks intersect selected fence.")
#             return

#         las_folder = self._resolve_las_folder_for_fence()
#         if las_folder is None:
#             QMessageBox.warning(
#                 self.app,
#                 "Load by Fence",
#                 "Could not locate LAZ/LAS folder for the selected SNT blocks.",
#             )
#             return
#         print(f"[LoadByFence] SNT folder: {las_folder}")

#         jobs, missing_blocks = self._resolve_candidate_block_jobs(intersecting_blocks, las_folder)
#         if missing_blocks:
#             print(f"[LoadByFence] Missing block files: {', '.join(missing_blocks)}")

#         if not jobs:
#             QMessageBox.information(self.app, "Load by Fence", "No points found inside selected fence.")
#             return

#         self._start_fence_worker(
#             jobs=jobs,
#             fence_polygon=fence_polygon,
#             fence_bbox=fence_bbox,
#             base_stats={
#                 "block_scan_seconds": block_scan_seconds,
#                 "total_blocks": total_blocks,
#                 "bbox_candidates": bbox_candidates,
#                 "intersecting_count": len(intersecting_blocks),
#                 "missing_blocks": missing_blocks,
#             },
#         )

#     def _start_fence_worker(self, jobs, fence_polygon, fence_bbox, base_stats):
#         operation_label = str(getattr(self, "_fence_operation_label", "Load by Fence"))
#         if fence_bbox is None:
#             QMessageBox.warning(self.app, operation_label, "Invalid selection polygon.")
#             return

#         if self._fence_worker is not None and self._fence_worker.isRunning():
#             QMessageBox.information(self.app, operation_label, "A multi-file point load is already running.")
#             return

#         self._fence_last_stats = dict(base_stats or {})

#         progress = QProgressDialog(
#             "Scanning blocks",
#             "Cancel",
#             0,
#             100,
#             self.app,
#         )
#         progress.setWindowTitle(operation_label)
#         progress.setWindowModality(Qt.WindowModal)
#         progress.setMinimumDuration(0)
#         progress.setAutoClose(False)
#         progress.setAutoReset(False)
#         progress.setValue(0)
#         progress.show()
#         self._fence_progress = progress

#         worker = SNTFenceLoadWorker(
#             block_jobs=jobs,
#             fence_polygon_xy=fence_polygon,
#             fence_bbox=fence_bbox,
#             chunk_size=self._fence_chunk_size,
#             dedupe_boundary_points=True,
#         )
#         self._fence_worker = worker

#         worker.progress.connect(self._on_fence_worker_progress)
#         worker.completed.connect(self._on_fence_worker_completed)
#         worker.failed.connect(self._on_fence_worker_failed)
#         worker.cancelled.connect(self._on_fence_worker_cancelled)
#         progress.canceled.connect(worker.cancel)
#         worker.start()

#     def _on_fence_worker_progress(self, value: int, message: str):
#         if self._fence_progress is None:
#             return
#         try:
#             self._fence_progress.setLabelText(str(message))
#             self._fence_progress.setValue(int(max(0, min(100, value))))
#         except Exception:
#             pass

#     def _on_fence_worker_completed(self, worker_stats: Dict[str, object]):
#         render_seconds = 0.0
#         data = self._fence_worker.result_data if self._fence_worker is not None else None

#         if data is not None and len(data.get("xyz", [])) > 0:
#             try:
#                 render_seconds = self.render_fenced_points_in_main_view(data)
#             except Exception as render_exc:
#                 self._on_fence_worker_failed(f"Render failed: {render_exc}")
#                 return

#         stats = dict(self._fence_last_stats or {})
#         stats.update(worker_stats or {})
#         stats["render_seconds"] = float(render_seconds)
#         self._fence_last_stats = stats

#         block_logs = stats.get("block_logs", []) or []
#         for row in block_logs:
#             bid = row.get("block_id")
#             loaded = int(row.get("loaded_points", 0))
#             inside = int(row.get("inside_points", 0))
#             if row.get("error"):
#                 print(f"[LoadByFence] Block {bid} error: {row.get('error')}")
#                 continue
#             print(f"[LoadByFence] Block {bid} loaded points: {loaded:,}")
#             print(f"[LoadByFence] Block {bid} points inside fence: {inside:,}")

#         total_render = int(stats.get("total_render_points", 0))
#         print(f"[LoadByFence] Total rendered fenced points: {total_render:,}")
#         print(
#             f"[LoadByFence] Timings: scan={stats.get('block_scan_seconds', 0):.3f}s, "
#             f"load+filter={stats.get('worker_seconds', 0):.3f}s, render={stats.get('render_seconds', 0):.3f}s"
#         )

#         if self._fence_progress is not None:
#             try:
#                 self._fence_progress.setValue(100)
#                 self._fence_progress.close()
#             except Exception:
#                 pass
#             self._fence_progress = None

#         if total_render <= 0:
#             QMessageBox.information(self.app, "Load by Fence", "No points found inside selected fence.")
#             if hasattr(self.app, "statusBar"):
#                 self.app.statusBar().showMessage("No points found inside selected fence.", 5000)
#         else:
#             block_count = len([b for b in block_logs if not b.get("error")])
#             operation = str(stats.get("operation") or "fence")
#             if operation == "buffered_grid":
#                 info_text = (
#                     f"Loaded {total_render:,} points from {block_count} grid file(s).\n"
#                     f"Primary grid: {stats.get('primary_grid', '')}\n"
#                     f"Neighbor buffer: {float(stats.get('buffer_width', 0.0)):.2f} project units\n\n"
#                     "Classification edits will be saved back to each point's own source file."
#                 )
#                 info_title = "Open Block with Buffer"
#             else:
#                 info_text = f"Loaded {total_render:,} points from {block_count} SNT block(s)."
#                 info_title = "Load by Fence"
#             QMessageBox.information(self.app, info_title, info_text)
#             if hasattr(self.app, "statusBar"):
#                 self.app.statusBar().showMessage(
#                     f"Loaded {total_render:,} points from {block_count} SNT block(s).",
#                     6000,
#                 )

#         self._fence_worker = None

#     def _on_fence_worker_failed(self, error_text: str):
#         if self._fence_progress is not None:
#             try:
#                 self._fence_progress.close()
#             except Exception:
#                 pass
#             self._fence_progress = None

#         self._fence_worker = None
#         print(f"[LoadByFence] ERROR: {error_text}")
#         traceback.print_exc()
#         QMessageBox.critical(self.app, "Load by Fence", f"Load by fence failed:\n\n{error_text}")

#     def _on_fence_worker_cancelled(self, message: str):
#         if self._fence_progress is not None:
#             try:
#                 self._fence_progress.close()
#             except Exception:
#                 pass
#             self._fence_progress = None
#         self._fence_worker = None
#         if hasattr(self.app, "statusBar"):
#             self.app.statusBar().showMessage(message, 3000)

#     def render_fenced_points_in_main_view(self, fenced_data: Dict[str, np.ndarray]) -> float:
#         t0 = time.time()

#         xyz = np.asarray(fenced_data.get("xyz"), dtype=np.float64)
#         classification = np.asarray(fenced_data.get("classification"), dtype=np.uint8)
#         if xyz.ndim != 2 or xyz.shape[0] == 0:
#             return 0.0

#         point_count = int(xyz.shape[0])
#         # The combined multi-source dataset has a different index space from
#         # any previously loaded single grid. Never retain stale clear/reload
#         # indices that could delete unrelated buffered points.
#         self.loaded_grids.clear()
#         self.grid_aliases.clear()
#         self.app.data = {
#             "xyz": xyz,
#             "classification": classification,
#         }

#         try:
#             source_files = [str(p) for p in list(fenced_data.get("_fence_source_files") or []) if str(p)]
#             source_file_ids_raw = fenced_data.get("_fence_source_file_ids")
#             source_point_indices_raw = fenced_data.get("_fence_source_point_indices")
#             if source_files and source_file_ids_raw is not None and source_point_indices_raw is not None:
#                 source_file_ids = np.asarray(source_file_ids_raw, dtype=np.int32)
#                 source_point_indices = np.asarray(source_point_indices_raw, dtype=np.int64)
#                 if source_file_ids.shape[0] != point_count or source_point_indices.shape[0] != point_count:
#                     raise ValueError("Fence point-source metadata length mismatch.")
#                 if np.any(source_file_ids < 0) or np.any(source_file_ids >= len(source_files)):
#                     raise ValueError("Fence source file IDs are out of range.")

#                 dup_owner = np.asarray(
#                     fenced_data.get("_fence_duplicate_owner_indices", np.empty(0, dtype=np.int64)),
#                     dtype=np.int64,
#                 )
#                 dup_file_ids = np.asarray(
#                     fenced_data.get("_fence_duplicate_source_file_ids", np.empty(0, dtype=np.int32)),
#                     dtype=np.int32,
#                 )
#                 dup_point_idx = np.asarray(
#                     fenced_data.get("_fence_duplicate_source_point_indices", np.empty(0, dtype=np.int64)),
#                     dtype=np.int64,
#                 )
#                 if dup_owner.shape[0] != dup_file_ids.shape[0] or dup_owner.shape[0] != dup_point_idx.shape[0]:
#                     raise ValueError("Fence duplicate alias metadata lengths do not match.")
#                 if dup_owner.size > 0:
#                     if np.any(dup_owner < 0) or np.any(dup_owner >= point_count):
#                         raise ValueError("Fence duplicate owners are out of range.")
#                     if np.any(dup_file_ids < 0) or np.any(dup_file_ids >= len(source_files)):
#                         raise ValueError("Fence duplicate source file IDs are out of range.")

#                 session_id = uuid.uuid4().hex
#                 self.app.data["_fence_session_id"] = session_id
#                 self.app.data["_fence_source_file_ids"] = source_file_ids
#                 self.app.data["_fence_source_point_indices"] = source_point_indices
#                 self.app._fence_parent_session = {
#                     "session_id": session_id,
#                     "source_files": source_files,
#                     "operation": str((self._fence_last_stats or {}).get("operation") or "fence"),
#                     "primary_grid": (self._fence_last_stats or {}).get("primary_grid"),
#                     "buffer_width": (self._fence_last_stats or {}).get("buffer_width"),
#                     "duplicate_owner_indices": dup_owner,
#                     "duplicate_source_file_ids": dup_file_ids,
#                     "duplicate_source_point_indices": dup_point_idx,
#                 }
#             else:
#                 self.app._fence_parent_session = None
#         except Exception as map_exc:
#             self.app._fence_parent_session = None
#             print(f"⚠️ Fence source mapping disabled: {map_exc}")

#         if fenced_data.get("rgb") is not None:
#             self.app.data["rgb"] = np.asarray(fenced_data.get("rgb"), dtype=np.uint8)
#         if fenced_data.get("intensity") is not None:
#             self.app.data["intensity"] = np.asarray(fenced_data.get("intensity"), dtype=np.float32)
#         if fenced_data.get("return_number") is not None:
#             self.app.data["return_number"] = np.asarray(fenced_data.get("return_number"), dtype=np.uint8)
#         if fenced_data.get("number_of_returns") is not None:
#             self.app.data["number_of_returns"] = np.asarray(fenced_data.get("number_of_returns"), dtype=np.uint8)

#         self.app.loaded_file = None
#         self.app.last_save_path = None
#         self.app.display_mode = "class"

#         palette = getattr(self.app, "class_palette", None) or {}
#         if not palette:
#             try:
#                 from gui.class_display import build_class_palette
#                 palette = build_class_palette(self.app.data["classification"])
#                 self.app.class_palette = palette
#             except Exception:
#                 palette = {}

#         if palette:
#             self.app.apply_class_map({
#                 "classes": palette,
#                 "slot": 0,
#                 "target_view": 0,
#                 "color_mode": 0,
#             })
#         else:
#             from gui.pointcloud_display import update_pointcloud
#             update_pointcloud(self.app, "class")

#         if hasattr(self.app, "_ensure_overlay_actors"):
#             self.app._ensure_overlay_actors()

#         if hasattr(self.app, "point_count_widget") and self.app.point_count_widget:
#             try:
#                 from gui.point_count_widget import refresh_point_statistics
#                 refresh_point_statistics(self.app)
#             except Exception:
#                 pass

#         if hasattr(self.app, "_update_window_title"):
#             stats = self._fence_last_stats or {}
#             if stats.get("operation") == "buffered_grid":
#                 title = (
#                     f"{stats.get('primary_grid', 'Grid')} + "
#                     f"{float(stats.get('buffer_width', 0.0)):.2f}-unit buffer ({len(xyz):,} pts)"
#                 )
#             else:
#                 title = f"Fenced SNT Load ({len(xyz):,} pts)"
#             self.app._update_window_title(
#                 title,
#                 getattr(self.app, "project_crs_epsg", None),
#             )

#         self._fence_last_data_count = int(len(xyz))
#         return float(time.time() - t0)

#     def clear_fenced_load(self):
#         if self._fence_worker is not None and self._fence_worker.isRunning():
#             self._fence_worker.cancel()

#         had_data = bool(getattr(self.app, "data", None) is not None)
#         self.app.data = None
#         self.app._fence_parent_session = None
#         self.app.loaded_file = None
#         self.app.last_save_path = None

#         try:
#             if hasattr(self.app, "vtk_widget") and self.app.vtk_widget:
#                 self.app.vtk_widget.clear()
#                 self.app.vtk_widget.render()
#         except Exception:
#             try:
#                 ren = self.app.vtk_widget.renderer
#                 ren.RemoveAllViewProps()
#                 self.app.vtk_widget.render()
#             except Exception:
#                 pass

#         if hasattr(self.app, "_ensure_overlay_actors"):
#             self.app._ensure_overlay_actors()

#         if hasattr(self.app, "_update_window_title"):
#             self.app._update_window_title("No Data", getattr(self.app, "project_crs_epsg", None))

#         self._fence_last_data_count = 0
#         if hasattr(self.app, "statusBar"):
#             msg = "Fenced load cleared." if had_data else "No fenced points to clear."
#             self.app.statusBar().showMessage(msg, 3000)

#     def _show_file_selection_dialog(self, las_folder, grid_name):
#         """
#         Fallback dialog: let user pick a LAS/LAZ file from las_folder
#         when no automatic match is found for grid_name.
#         """
#         from PySide6.QtWidgets import QFileDialog, QMessageBox

#         # Make sure we have a string path
#         folder_str = str(las_folder) if las_folder is not None else ""

#         # Let user pick a file
#         file_path, _ = QFileDialog.getOpenFileName(
#             self.app,
#             f"Select LAZ/LAS file for grid {grid_name}",
#             folder_str,
#             "LiDAR Files (*.laz *.las);;All Files (*.*)",
#         )

#         if not file_path:
#             # User cancelled
#             return

#         # Optional: small confirmation
#         reply = QMessageBox.question(
#             self.app,
#             "Confirm Grid File",
#             f"Use this file for grid '{grid_name}'?\n\n{os.path.basename(file_path)}",
#             QMessageBox.Yes | QMessageBox.No,
#             QMessageBox.Yes,
#         )

#         if reply != QMessageBox.Yes:
#             return

#         # Reuse existing loading pipeline
#         self._load_las_file(Path(file_path), grid_name)

            
#     def _is_tool_operation_in_progress(self):
#         """Returns True if any user tool is currently performing an operation that uses right-click."""
#         app = self.app
#         if not app: return False

#         # 1. Digitize (Draw) Tools: active when a tool is selected and has points
#         digitizer = getattr(app, "digitizer", None)
#         if digitizer and (digitizer.active_tool or getattr(digitizer, "vertex_move_mode", False)):
#             # Delete Vertex uses right-click as confirmation only after a
#             # vertex has been selected. Once it is deleted, allow the normal
#             # grid/point-cloud context menu to handle the next right-click.
#             if (
#                 getattr(digitizer, "active_tool", None) == "deletevertex"
#                 and getattr(digitizer, "selected_vertex_idx", None) is not None
#             ):
#                 return True
#             if len(getattr(digitizer, "temp_points", [])) > 0:
#                 return True
#             if getattr(digitizer, "dragging_vertex", None) is not None:
#                 return True

#         # 1b. Ortho-Polygon tool keeps its own point list separate from
#         #     digitizer.temp_points, so check it explicitly. Right-click
#         #     finalises the polygon — yield once the user has placed >= 1 point.
#         if digitizer is not None and getattr(digitizer, "active_tool", None) == "orthopolygon":
#             ortho_tool = getattr(digitizer, "_ortho_polygon_tool", None)
#             if ortho_tool is not None and len(getattr(ortho_tool, "points", []) or []) > 0:
#                 return True

#         # 2. Measurement Tool: active during measurement drag
#         mtool = getattr(app, "measurement_tool", None)
#         if mtool and getattr(mtool, "is_measuring", False):
#             if len(getattr(mtool, "measurement_points", [])) > 0:
#                 return True

#         # 3. Curve Tool: active during curve path drawing
#         ctool = getattr(app, "curve_tool", None)
#         if ctool and ctool.active:
#             if len(getattr(ctool, "points", [])) > 0:
#                 return True

#         # 4. Zoom Rectangle Tool: active when drawing rectangle
#         ztool = getattr(app, "zoom_rectangle_tool", None)
#         if ztool and ztool.active and getattr(ztool, "start_pos", None) is not None:
#             return True

#         # 5. Select Rectangle Tool: active when drawing/processing selection
#         stool = getattr(app, "select_rectangle_tool", None)
#         if stool and stool.active:
#             if getattr(stool, "is_drawing", False) or getattr(stool, "start_pos", None) is not None:
#                 return True

#         # 6. Classification polygon: right-click closes/classifies an in-progress polygon.
#         # Yield only after the user has started drawing so normal grid-label menus still work.
#         if getattr(app, "active_classify_tool", None) == "polygon":
#             classify_tools = []
#             main_tool = getattr(app, "classify_interactor", None)
#             if main_tool is not None:
#                 classify_tools.append(main_tool)
#             cut_tool = getattr(app, "cut_classify_interactor", None)
#             if cut_tool is not None:
#                 classify_tools.append(cut_tool)
#             classify_tools.extend(getattr(app, "classify_interactors", {}).values())

#             for ctool in classify_tools:
#                 if getattr(ctool, "drawing_points", None):
#                     return True

#         return False

#     def _true_label_color(self, actor):
#         """Return an actor's real (non-hover-highlighted) color.

#         on_mouse_move's _highlight_label() temporarily overwrites a label's
#         color with bright yellow while hovered, and caches the real color in
#         self.original_colors so _unhighlight_label() can restore it later.
#         If the actor is currently mid-hover when an edit dialog is opened,
#         reading GetProperty()/GetTextProperty() directly would pick up that
#         temporary yellow instead of the real color. Prefer the cache.
#         """
#         try:
#             cached = getattr(self, 'original_colors', None)
#             if cached is not None and actor in cached:
#                 c = cached[actor].get('color')
#                 if c is not None:
#                     return tuple(c)
#         except Exception:
#             pass

#         try:
#             if isinstance(actor, vtk.vtkTextActor3D):
#                 return tuple(actor.GetTextProperty().GetColor())
#         except Exception:
#             pass
#         try:
#             return tuple(actor.GetProperty().GetColor())
#         except Exception:
#             return (1, 1, 1)

#     def _capture_label_state(self, actor):
#         """Snapshot a label actor's current text/size/color for a dialog
#         default or an undo entry. Never raises — falls back to safe
#         defaults on any VTK access error so a single bad actor can't crash
#         the caller."""
#         is_text_actor3d = False
#         try:
#             is_text_actor3d = isinstance(actor, vtk.vtkTextActor3D)
#         except Exception:
#             pass

#         try:
#             if is_text_actor3d:
#                 text_prop = actor.GetTextProperty()
#                 text = actor.GetInput() or getattr(actor, 'grid_name', '') or ''
#                 raw_size = text_prop.GetFontSize()
#                 size = int(75 if raw_size is None else raw_size)
#             else:
#                 text = (
#                     getattr(actor, 'display_text', None)
#                     or getattr(actor, 'text_content', None)
#                     or getattr(actor, 'grid_name', '')
#                     or ''
#                 )
#                 raw_size = getattr(actor, '_naksha_label_font_size', 75)
#                 size = int(75 if raw_size is None else raw_size)
#         except Exception:
#             text = getattr(actor, 'grid_name', '') or ''
#             size = 75

#         color = self._true_label_color(actor)
#         return {
#             'actor': actor,
#             'text': text,
#             'size': size,
#             'color': color,
#             'is_text_actor3d': is_text_actor3d,
#         }

#     def _apply_label_state(self, actor, text, size, color, is_text_actor3d=None):
#         """Set a grid-label actor's rendered text/font-size/color.

#         Used for edits, undo-revert, and bulk font-size changes alike, so
#         there is exactly one code path that can go wrong. Never raises —
#         every VTK call is individually guarded so a single failed actor
#         cannot crash a bulk operation or leave the app in a broken state.
#         ``actor.grid_name`` is never touched: it is the lookup key used
#         elsewhere (load_grid_las, loaded_grids, PRJ/SNT filename inference)
#         to associate the label with its LAS/PRJ data.
#         """
#         if actor is None:
#             return False
#         try:
#             if is_text_actor3d is None:
#                 is_text_actor3d = isinstance(actor, vtk.vtkTextActor3D)
#         except Exception:
#             is_text_actor3d = False

#         text = text if text else getattr(actor, 'grid_name', '') or ''
#         try:
#             size = max(0, min(999, int(75 if size is None else size)))
#         except (TypeError, ValueError):
#             size = 75
#         color = color if color else (1, 1, 1)

#         try:
#             if is_text_actor3d:
#                 text_prop = actor.GetTextProperty()
#                 actor.SetInput(text)
#                 text_prop.SetFontSize(size)
#                 text_prop.SetColor(*color)
#             else:
#                 # vtkFollower labels are built either from a live vtkVectorText
#                 # pipeline connection (DXF path: SetInputConnection) or from a
#                 # cached/shared vtkPolyData (SNT path: SetInputData, cached by
#                 # text content in self._snt_text_poly_cache — potentially
#                 # shared with OTHER actors that happen to have the same text).
#                 # We must never mutate a shared connection/polydata in place;
#                 # instead always build a fresh, private vtkVectorText output
#                 # and hand it to this actor's mapper only.
#                 try:
#                     # Bulk font-size edits pass the actor's own unchanged
#                     # text back in — skip the rebuild entirely in that case
#                     # so a 1500-label bulk resize doesn't recreate geometry
#                     # it doesn't need to.
#                     if getattr(actor, 'text_content', None) != text:
#                         mapper = actor.GetMapper()
#                         fresh_source = vtk.vtkVectorText()
#                         fresh_source.SetText(text)
#                         fresh_source.Update()
#                         fresh_poly = vtk.vtkPolyData()
#                         fresh_poly.DeepCopy(fresh_source.GetOutput())
#                         # SetInputData automatically clears any prior
#                         # SetInputConnection pipeline link on this port.
#                         mapper.SetInputData(fresh_poly)
#                 except Exception as _src_err:
#                     print(f"⚠️ Could not update grid label text source: {_src_err}")

#                 try:
#                     base_scale = getattr(actor, '_naksha_base_scale', None)
#                     if base_scale is None:
#                         base_scale = float(actor.GetScale()[0])
#                         actor._naksha_base_scale = base_scale
#                     base_size = float(getattr(actor, '_naksha_base_font_size', 75) or 75)
#                     if base_size > 0:
#                         # SNT/DXF follower glyphs are world geometry rather
#                         # than screen text. A strong display multiplier keeps
#                         # labels readable across kilometre-scale grid cells:
#                         # 999 pt is intentionally very large, while 50 pt is
#                         # still clearly visible.
#                         new_scale = base_scale * (float(size) / base_size) * 8.0
#                         actor.SetScale(new_scale, new_scale, new_scale)
#                 except Exception as _scale_err:
#                     print(f"⚠️ Could not rescale grid label: {_scale_err}")

#                 try:
#                     actor.GetProperty().SetColor(*color)
#                 except Exception:
#                     pass
#                 actor._naksha_label_font_size = size

#             actor.display_text = text
#             actor.text_content = text

#             # Keep the hover-highlight cache in sync so a later mouse-out
#             # doesn't silently revert this change back to the pre-edit color.
#             try:
#                 cached = getattr(self, 'original_colors', None)
#                 if cached is not None and actor in cached:
#                     cached[actor]['color'] = tuple(color)
#             except Exception:
#                 pass

#             return True
#         except Exception as e:
#             print(f"⚠️ Could not apply grid label state: {e}")
#             return False

#     def _push_label_undo(self, entries):
#         """Record a snapshot (list of _capture_label_state dicts) taken
#         BEFORE an edit, so _undo_last_label_edit can restore it."""
#         try:
#             if not entries:
#                 return
#             self._label_edit_undo_stack.append(entries)
#             limit = getattr(self, '_label_edit_undo_limit', 20)
#             while len(self._label_edit_undo_stack) > limit:
#                 self._label_edit_undo_stack.pop(0)
#         except Exception:
#             pass

#     def _undo_last_label_edit(self):
#         """Revert the most recent grid-label text/font-size/color edit
#         (single or bulk). Safe to call with an empty stack."""
#         try:
#             if not self._label_edit_undo_stack:
#                 print("↶ No grid label edit to undo")
#                 return
#             entries = self._label_edit_undo_stack.pop()
#             restored = 0
#             for entry in entries:
#                 actor = entry.get('actor')
#                 if actor is None:
#                     continue
#                 ok = self._apply_label_state(
#                     actor,
#                     entry.get('text'),
#                     entry.get('size'),
#                     entry.get('color'),
#                     entry.get('is_text_actor3d'),
#                 )
#                 if ok:
#                     restored += 1
#             self._force_render_after_label_edit()
#             print(f"↶ Undid grid label edit ({restored}/{len(entries)} label(s) restored)")
#         except Exception as e:
#             print(f"⚠️ Undo grid label edit failed: {e}")
#             import traceback
#             traceback.print_exc()

#     def _edit_grid_label(self, actor):
#         """Open a text-edit dialog for a single grid/block-name label actor.

#         Only the *rendered* text, font size and color are changed here.
#         ``actor.grid_name`` is deliberately left untouched (see
#         _apply_label_state's docstring).
#         """
#         try:
#             from gui.digitize_tools import TextEditDialog
#             try:
#                 from PySide6.QtWidgets import QDialog
#             except ImportError:
#                 from PyQt5.QtWidgets import QDialog

#             before = self._capture_label_state(actor)

#             dialog = TextEditDialog(
#                 current_text=before['text'],
#                 current_size=before['size'],
#                 current_font="Arial",
#                 current_bold=True,
#                 current_italic=False,
#                 current_color=before['color'],
#                 parent=self.app,
#             )
#             try:
#                 result = dialog.exec()
#             except AttributeError:
#                 result = dialog.exec_()

#             if result != QDialog.Accepted:
#                 return

#             values = dialog.get_values()
#             new_text = values.get('text', before['text']) or before['text']
#             raw_size = values.get('font_size', before['size'])
#             new_size = int(before['size'] if raw_size is None else raw_size)
#             new_color = values.get('color', before['color']) or before['color']

#             ok = self._apply_label_state(
#                 actor, new_text, new_size, new_color, before['is_text_actor3d']
#             )
#             if ok:
#                 self._push_label_undo([before])
#                 self._force_render_after_label_edit()
#                 print(f"✅ Grid label text updated: '{new_text}'")
#         except Exception as e:
#             print(f"⚠️ Grid label edit failed: {e}")
#             import traceback
#             traceback.print_exc()

#     def _iter_all_grid_label_actors(self):
#         """Yield every live grid/block-name label actor currently tracked
#         by the app (SNT + DXF stores), de-duplicated. Never raises."""
#         seen = set()
#         for store_name in ('snt_actors', 'dxf_actors'):
#             try:
#                 for data in getattr(self.app, store_name, []) or []:
#                     for actor in data.get('actors', []) or []:
#                         if not (hasattr(actor, 'is_grid_label') and actor.is_grid_label):
#                             continue
#                         key = id(actor)
#                         if key in seen:
#                             continue
#                         seen.add(key)
#                         yield actor
#             except Exception:
#                 continue

#     def edit_all_grid_labels_font_size(self):
#         """Bulk-apply a single font size to every block-name label in the
#         scene at once. Text and color are left exactly as each label
#         already has them — only size changes. Fully undoable in one step
#         via _undo_last_label_edit."""
#         try:
#             from PySide6.QtWidgets import QInputDialog

#             actors = list(self._iter_all_grid_label_actors())
#             if not actors:
#                 QMessageBox.information(
#                     self.app, "No Block Labels",
#                     "No block-name labels are currently loaded."
#                 )
#                 return

#             default_size = 75
#             try:
#                 default_size = self._capture_label_state(actors[0])['size'] or 75
#             except Exception:
#                 pass

#             # Positional args here — PySide6 and PyQt5 disagree on the
#             # keyword names for min/max (minValue/maxValue vs min/max), so
#             # positional is the only form that works reliably on both.
#             new_size, ok = QInputDialog.getInt(
#                 self.app,
#                 "Edit All Block Labels — Font Size",
#                 f"Font size (pt) for all {len(actors)} block label(s):",
#                 default_size,
#                 0,
#                 999,
#             )
#             if not ok:
#                 return

#             before_entries = []
#             updated = 0
#             for actor in actors:
#                 try:
#                     before = self._capture_label_state(actor)
#                     applied = self._apply_label_state(
#                         actor, before['text'], new_size, before['color'],
#                         before['is_text_actor3d'],
#                     )
#                     if applied:
#                         before_entries.append(before)
#                         updated += 1
#                 except Exception as _one_err:
#                     # One bad actor must never abort the whole batch.
#                     print(f"⚠️ Skipped one block label during bulk font-size edit: {_one_err}")
#                     continue

#             if before_entries:
#                 self._push_label_undo(before_entries)
#             self._force_render_after_label_edit()
#             print(f"✅ Bulk font-size edit: {updated}/{len(actors)} block label(s) updated to {new_size}pt")
#         except Exception as e:
#             print(f"⚠️ Bulk grid label font-size edit failed: {e}")
#             import traceback
#             traceback.print_exc()

#     def _force_render_after_label_edit(self):
#         try:
#             handles = self._get_main_interactor_handles()
#             if handles is not None:
#                 _, interactor, _ = handles
#                 interactor.GetRenderWindow().Render()
#         except Exception:
#             pass

#     def _block_polygon_hit_click(self, obj, clickPos, renderer) -> bool:
#         """
#         Global block-polygon priority check.

#         If the click's world position falls inside ANY BL-layer block
#         polygon, load that block directly via its already-resolved
#         file_path and return True — before any grid-label picking or
#         name-matching logic runs.

#         This makes block data authoritative for any click inside a
#         block, regardless of which label glyph happens to sit under the
#         pixel. Some SNTs render two overlapping label conventions at
#         the same block (e.g. an SNT-style 'DV..._000051.laz' name that
#         has no physical-file match, stacked on top of the real
#         'LAVARONE000002.laz' filename label). Picking whichever text
#         actor the hardware picker happens to grab is non-deterministic,
#         so relying on label-name matching after the fact is what caused
#         clicks to intermittently fall through to the manual file picker.
#         Testing the polygon first sidesteps that entirely.

#         Returns False if no polygon contains the click point, so the
#         caller can fall through to normal label / point-cloud handling.
#         """
#         from PySide6.QtWidgets import QMenu, QMessageBox
#         from PySide6.QtGui import QAction, QCursor

#         block_polygons = getattr(self.app, 'snt_block_polygons', None) or self._ensure_snt_block_index()
#         if not block_polygons:
#             return False

#         try:
#             coord = vtk.vtkCoordinate()
#             coord.SetCoordinateSystemToDisplay()
#             coord.SetValue(float(clickPos[0]), float(clickPos[1]), 0.0)
#             wx, wy, _ = coord.GetComputedWorldValue(renderer)
#             print(f"[block-click] world XY = ({wx:.2f}, {wy:.2f}), "
#                   f"testing {len(block_polygons)} BL polygons")

#             def _pip(px, py, poly):
#                 n = len(poly)
#                 inside = False
#                 j = n - 1
#                 for i in range(n):
#                     xi, yi = poly[i]
#                     xj, yj = poly[j]
#                     if ((yi > py) != (yj > py)) and (
#                         px < (xj - xi) * (py - yi) / ((yj - yi) or 1e-12) + xi
#                     ):
#                         inside = not inside
#                     j = i
#                 return inside

#             hit_block = None
#             all_hit_alt_names = []
#             best_edge_dist = float('inf')
#             for blk in block_polygons:
#                 pts = blk.get("points_2d", [])
#                 if len(pts) >= 3 and _pip(wx, wy, pts):
#                     # NEAREST BOUNDARY: pick the polygon whose edge is closest
#                     # to the click point. At corners where polygons overlap,
#                     # the click is physically closer to the correct block's
#                     # boundary because it sits at that block's corner edge.
#                     min_edge_dist = float('inf')
#                     n = len(pts)
#                     for i in range(n):
#                         x1, y1 = pts[i]
#                         x2, y2 = pts[(i + 1) % n]
#                         dx, dy = x2 - x1, y2 - y1
#                         len_sq = dx * dx + dy * dy
#                         if len_sq < 1e-12:
#                             t = 0.0
#                         else:
#                             t = max(0.0, min(1.0, ((wx - x1) * dx + (wy - y1) * dy) / len_sq))
#                         proj_x = x1 + t * dx
#                         proj_y = y1 + t * dy
#                         edge_dist = (wx - proj_x) ** 2 + (wy - proj_y) ** 2
#                         if edge_dist < min_edge_dist:
#                             min_edge_dist = edge_dist
#                     edge_m = min_edge_dist ** 0.5
#                     print(f"[block-click] polygon '{blk.get('grid_name')}': "
#                           f"nearest_edge={edge_m:.1f}m")

#                     if hit_block is None or min_edge_dist < best_edge_dist:
#                         hit_block = blk
#                         best_edge_dist = min_edge_dist
#                     # Collect alt_names from ALL overlapping polygons
#                     for an in blk.get("alt_names", []):
#                         if an not in all_hit_alt_names:
#                             all_hit_alt_names.append(an)
#                     # Also add grid_names from other overlapping polygons as alternatives
#                     other_gn = blk.get("grid_name")
#                     if other_gn and other_gn != hit_block.get("grid_name"):
#                         if other_gn not in all_hit_alt_names:
#                             all_hit_alt_names.append(other_gn)

#             if hit_block is None:
#                 print(f"[block-click] no BL polygon contains ({wx:.2f}, {wy:.2f})")
#                 return False

#             print(f"[block-click] WINNER: '{hit_block.get('grid_name')}' "
#                   f"(nearest edge={best_edge_dist**0.5:.1f}m)")

#             grid_name = hit_block.get("grid_name")
#             file_path = hit_block.get("file_path")
#             snt_filename = hit_block.get("snt_filename")
#             merged_alt = list(all_hit_alt_names)
#             for an in (hit_block.get("alt_names") or []):
#                 if an not in merged_alt:
#                     merged_alt.append(an)
#             print(f"[block-click] HIT block from '{hit_block.get('snt_filename')}', "
#                   f"grid_name='{grid_name}', file_path='{file_path}'")
#             if merged_alt:
#                 print(f"[block-click] All candidate names: {[grid_name] + merged_alt}")

#             if not (grid_name or file_path):
#                 return False

#             menu = QMenu(self.app)
#             menu.setStyleSheet("""
#                 QMenu {
#                     background-color: #2c2c2c;
#                     color: #f0f0f0;
#                     border: 1px solid #555;
#                     padding: 5px;
#                 }
#                 QMenu::item {
#                     padding: 8px 30px;
#                     border-radius: 3px;
#                 }
#                 QMenu::item:selected {
#                     background-color: #3c3c3c;
#                 }
#             """)
#             load_action = QAction("📂 Load Block Data", self.app)
#             clear_action = QAction("🧹 Clear Grid Data", self.app)

#             is_loaded = grid_name in self.loaded_grids
#             if is_loaded:
#                 load_action.setText("📂 Reload Block Data")
#                 point_count = len(self.loaded_grids[grid_name])
#                 clear_action.setText(f"🧹 Clear Grid ({point_count:,} pts)")
#             else:
#                 clear_action.setEnabled(False)
#                 clear_action.setText("🧹 (Grid not loaded)")

#             def _load_block_file(_=False, gn=grid_name, fp=file_path, an=None):
#                 if an is None:
#                     an = merged_alt
#                 if fp and Path(fp).exists():
#                     self._load_las_file(Path(fp), gn or Path(fp).stem)
#                     return
#                 self.load_grid_las(gn, snt_filename=snt_filename, alt_names=an)

#             load_action.triggered.connect(_load_block_file)

#             def _confirm_clear_block(_checked=False, gn=grid_name):
#                 if not gn:
#                     return
#                 reply = QMessageBox.question(
#                     self.app, "Confirm Clear",
#                     f"Clear all points from grid:\n\n{gn}\n\nContinue?",
#                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No
#                 )
#                 if reply == QMessageBox.Yes:
#                     self.clear_grid_data(gn)

#             clear_action.triggered.connect(_confirm_clear_block)
#             fence_action = QAction("🔺 Load Points Inside Fence", self.app)
#             fence_action.triggered.connect(self.activate_load_by_fence_tool)
#             buffer_action = QAction("🧩 Open Block with Buffer Points", self.app)
#             buffer_action.triggered.connect(
#                 lambda _=False, gn=grid_name, sf=snt_filename, an=tuple(merged_alt), fp=file_path:
#                     self.open_grid_with_buffer_points(gn, sf, list(an), fp)
#             )
#             menu.addAction(load_action)
#             menu.addAction(buffer_action)
#             menu.addAction(clear_action)
#             menu.addAction(fence_action)

#             self._consume_vtk_event(obj)
#             menu.exec(QCursor.pos())
#             return True
#         except Exception as _block_ex:
#             print(f"[block-click] ERROR during block polygon hit test: {_block_ex}")
#             import traceback
#             traceback.print_exc()
#             return False

#     def on_right_click(self, obj, event):
#         """Handle right-click on grid label OR point cloud"""
#         from PySide6.QtWidgets import QMenu, QInputDialog
#         from PySide6.QtGui import QAction, QCursor

#         if getattr(self.app, "_shutdown_in_progress", False):
#             return 0

#         # Move Vertex owns right-click while it is active.
#         # Right-click must be silent/idle here:
#         # no SNT/Grid menu, no block-click, no load grid data.
#         # After Move Vertex finishes and active_tool becomes None,
#         # normal right-click will work again automatically.
#         try:
#             digitizer = getattr(self.app, "digitizer", None)
#             if digitizer is not None:
#                 active_digitizer_tool = str(
#                     getattr(digitizer, "active_tool", "") or ""
#                 ).lower().strip()

#                 if (
#                     active_digitizer_tool == "movevertex"
#                     or getattr(digitizer, "moving_vertex_data", None) is not None
#                 ):
#                     self._consume_vtk_event(obj)
#                     return 1
#         except Exception:
#             pass

#         handles = self._get_main_interactor_handles()
#         if handles is None:
#             return 0
#         _, interactor, renderer = handles

#         # ✅ FIXED: If a tool operation is in progress (drawing, measuring, etc.),
#         # let the tool handle the right-click to finalize its operation.
#         # This prevents the 'Shading controls' dialog from appearing prematurely.
#         if self._is_tool_operation_in_progress():
#             return 0  # Allow other observers (the tools) to handle it

#         # ── TEXT DRAWING PRIORITY ──────────────────────────────────────────────
#         # If the cursor is over a placed text label, give the digitizer's own
#         # text context menu (Edit / Move / Copy / Delete) first priority.
#         # Only if the cursor is NOT over any text drawing do we fall through to
#         # the normal grid / point-cloud context menu below.
#         clickPos_check = interactor.GetEventPosition()
#         digitizer = getattr(self.app, "digitizer", None)
#         if digitizer is not None and getattr(digitizer, "enabled", False):
#             try:
#                 x_c, y_c = clickPos_check
#                 text_drawing = None

#                 # Hardware pick — fast, works for vtkTextActor3D
#                 hw_picker = vtk.vtkPropPicker()
#                 hw_picker.Pick(x_c, y_c, 0, renderer)
#                 picked_actor = hw_picker.GetActor()
#                 if picked_actor is not None:
#                     for d in getattr(digitizer, "drawings", []):
#                         if d.get("type") == "text" and d.get("actor") is picked_actor:
#                             text_drawing = d
#                             break

#                 # Math fallback — checks bounding-box / cursor proximity
#                 if text_drawing is None and hasattr(digitizer, "_get_drawing_under_cursor"):
#                     candidate = digitizer._get_drawing_under_cursor(x_c, y_c, tolerance=25.0)
#                     if candidate is not None and candidate.get("type") == "text":
#                         text_drawing = candidate

#                 if text_drawing is not None:
#                     # Hand off entirely to the digitizer — do NOT show grid menu.
#                     self._consume_vtk_event(obj)
#                     digitizer.clear_coordinate_labels()
#                     digitizer._unhighlight_all_lines()
#                     digitizer.multi_selected = []
#                     digitizer.selected_drawing = text_drawing
#                     digitizer._show_text_context_menu(text_drawing)
#                     digitizer._force_render()
#                     return 1
#             except Exception:
#                 pass  # Never block the normal flow on any error here
#         # ── END TEXT DRAWING PRIORITY ──────────────────────────────────────────

#         current_mode = str(
#             getattr(self.app, "display_mode", "")
#             or getattr(self.app, "current_display_mode", "")
#             or ""
#         ).lower()

#         clickPos = interactor.GetEventPosition()
#         click_x, click_y = clickPos

#         if current_mode == "surface":
#             try:
#                 surface_actor = getattr(self.app, "_surface_mesh_actor", None)
#                 hit_surface = False
#                 hit_tester = getattr(self.app, "_main_view_pick_hits_actor", None)
#                 if callable(hit_tester) and surface_actor is not None:
#                     hit_surface = bool(hit_tester(surface_actor, display_x=click_x, display_y=click_y))

#                 if hit_surface:
#                     from gui.surface_mode import show_surface_controls
#                     opened = bool(show_surface_controls(self.app))
#                 else:
#                     opened = False

#                 if opened:
#                     self._consume_vtk_event(obj)
#                     return 1
#             except Exception as _surface_menu_err:
#                 print(f"⚠️ Surface right-click priority skipped: {_surface_menu_err}")

#         if current_mode in ("shaded_class", "shading", "shadingmode"):
#             try:
#                 can_show_shading = getattr(self.app, "_can_show_shading_controls", None)

#                 if callable(can_show_shading) and can_show_shading():
#                     shaded_actor = getattr(self.app, "_shaded_mesh_actor", None)
#                     hit_shading = False
#                     hit_tester = getattr(self.app, "_main_view_pick_hits_actor", None)
#                     if callable(hit_tester) and shaded_actor is not None:
#                         hit_shading = bool(hit_tester(shaded_actor, display_x=click_x, display_y=click_y))

#                     opened = False
#                     if hit_shading and hasattr(self.app, "show_shading_controls"):
#                         opened = bool(self.app.show_shading_controls())

#                     if opened:
#                         self._consume_vtk_event(obj)
#                         return 1
#             except Exception as _shading_menu_err:
#                 print(f"⚠️ Shading right-click priority skipped: {_shading_menu_err}")

#         # ═══════════════════════════════════════════════════════
#         # STEP 0: Block polygon hit-test — GLOBAL PRIORITY.
#         # Any click whose world position lands inside a BL block
#         # polygon loads that block directly via its resolved
#         # file_path, before any label picking/name-matching runs.
#         # This is what guarantees block data wins even when the
#         # click happens to land on a label glyph belonging to a
#         # different naming convention (e.g. SNT-style
#         # 'DV..._000051.laz' vs the physical 'LAVARONE000002.laz'
#         # file) — that ambiguity previously fell through to
#         # name-matching and failed, forcing the manual file picker.
#         # ═══════════════════════════════════════════════════════
#         if self._block_polygon_hit_click(obj, clickPos, renderer):
#             return 1

#         # ═══════════════════════════════════════════════════════
#         # STEP 1: Try to find grid label first
#         # ═══════════════════════════════════════════════════════
#         picker = vtk.vtkPropPicker()
#         picker.Pick(clickPos[0], clickPos[1], 0, renderer)
#         actor = picker.GetActor()
        
#         if not actor or not (hasattr(actor, 'is_grid_label') and actor.is_grid_label):
#             # Try area picker with 10-pixel radius
#             area_picker = vtk.vtkAreaPicker()
#             x, y = clickPos
#             area_picker.AreaPick(x-10, y-10, x+10, y+10, renderer)
            
#             for prop in area_picker.GetProp3Ds():
#                 if hasattr(prop, 'is_grid_label') and prop.is_grid_label:
#                     actor = prop
#                     break

#         # Fallback: pick the nearest visible grid label by screen distance.
#         # Some text actors are not returned by vtkPropPicker/AreaPicker reliably,
#         # especially when a block boundary or line sits on top of the label.
#         if not actor or not (hasattr(actor, 'is_grid_label') and actor.is_grid_label):
#             try:
#                 best_actor = None
#                 best_dist2 = None
#                 for dxf_data in getattr(self.app, 'dxf_actors', []) or []:
#                     for prop in dxf_data.get('actors', []):
#                         if not (hasattr(prop, 'is_grid_label') and prop.is_grid_label):
#                             continue
#                         try:
#                             wx, wy, wz = prop.GetPosition()
#                             coord = vtk.vtkCoordinate()
#                             coord.SetCoordinateSystemToWorld()
#                             coord.SetValue(float(wx), float(wy), float(wz))
#                             sx, sy, _ = coord.GetComputedDisplayValue(renderer)
#                             dx = float(sx) - float(clickPos[0])
#                             dy = float(sy) - float(clickPos[1])
#                             dist2 = dx * dx + dy * dy
#                             if dist2 <= 400.0 and (best_dist2 is None or dist2 < best_dist2):
#                                 best_actor = prop
#                                 best_dist2 = dist2
#                         except Exception:
#                             pass
#                 if best_actor is not None:
#                     actor = best_actor
#             except Exception:
#                 pass

#         # NOTE: block-polygon priority is now handled globally in STEP 0
#         # (_block_polygon_hit_click), which runs before label picking even
#         # starts. If execution reaches here, STEP 0 already confirmed the
#         # click is NOT inside any block polygon, so treating this actor as
#         # a plain grid label is safe.

#         # ═══════════════════════════════════════════════════════
#         # CASE A: Found grid label - show label menu
#         # ═══════════════════════════════════════════════════════
#         if actor and hasattr(actor, 'is_grid_label') and actor.is_grid_label:
#             grid_name = getattr(actor, 'grid_name', '')
#             snt_filename = (
#                 getattr(actor, "_naksha_snt_filename", None)
#                 or getattr(actor, "snt_filename", None)
#                 or self._infer_snt_filename_for_grid(grid_name)
#             )
            
#             if grid_name:
#                 menu = QMenu(self.app)
#                 menu.setStyleSheet("""
#                     QMenu {
#                         background-color: #2c2c2c;
#                         color: #f0f0f0;
#                         border: 1px solid #555;
#                         padding: 5px;
#                     }
#                     QMenu::item {
#                         padding: 8px 30px;
#                         border-radius: 3px;
#                     }
#                     QMenu::item:selected {
#                         background-color: #3c3c3c;
#                     }
#                 """)
                
#                 load_action = QAction("📂 Load Grid Data", self.app)
#                 clear_action = QAction("🧹 Clear Grid Data", self.app)
                
#                 def confirm_clear():
#                     reply = QMessageBox.question(
#                         self.app,
#                         "Confirm Clear",
#                         f"Clear all points from grid:\n\n{grid_name}\n\n"
#                         f"Points will be removed from view.\n\nContinue?",
#                         QMessageBox.Yes | QMessageBox.No,
#                         QMessageBox.No
#                     )
#                     if reply == QMessageBox.Yes:
#                         self.clear_grid_data(grid_name)

#                 clear_action.triggered.connect(confirm_clear)
                
#                 owner_label = self._resolve_grid_owner(grid_name)
#                 is_loaded = grid_name in self.loaded_grids
                
#                 if is_loaded:
#                     load_action.setText("📂 Reload Grid Data")
#                     point_count = len(self.loaded_grids[grid_name])
#                     clear_action.setText(f"🧹 Clear Grid ({point_count:,} pts)")
#                 else:
#                     clear_action.setEnabled(False)
#                     clear_action.setText("🧹 (Grid not loaded)")

#                 load_action.triggered.connect(
#                     lambda _=False, gn=grid_name, sf=snt_filename: self.load_grid_las(
#                         gn,
#                         snt_filename=sf,
#                         alt_names=self._get_alt_names_for_grid(gn),
#                     )
#                 )
#                 clear_action.triggered.connect(lambda: self.clear_grid_data(grid_name))
#                 fence_action = QAction("🔺 Load Points Inside Fence", self.app)
#                 fence_action.triggered.connect(self.activate_load_by_fence_tool)
#                 buffer_action = QAction("🧩 Open Block with Buffer Points", self.app)
#                 buffer_action.triggered.connect(
#                     lambda _=False, gn=grid_name, sf=snt_filename:
#                         self.open_grid_with_buffer_points(
#                             gn, sf, self._get_alt_names_for_grid(gn)
#                         )
#                 )

#                 from PySide6.QtCore import QTimer
#                 edit_label_action = QAction("✏️ Edit Label Text", self.app)
#                 edit_label_action.triggered.connect(
#                     lambda _=False, a=actor: QTimer.singleShot(
#                         10, lambda a=a: self._edit_grid_label(a)
#                     )
#                 )

#                 bulk_font_action = QAction("🔠 Set Font Size — All Block Labels", self.app)
#                 bulk_font_action.triggered.connect(
#                     lambda _=False: QTimer.singleShot(10, self.edit_all_grid_labels_font_size)
#                 )

#                 menu.addAction(load_action)
#                 menu.addAction(buffer_action)
#                 menu.addAction(clear_action)
#                 menu.addAction(fence_action)
#                 menu.addSeparator()
#                 menu.addAction(edit_label_action)
#                 menu.addAction(bulk_font_action)

#                 if getattr(self, '_label_edit_undo_stack', None):
#                     undo_label_action = QAction("↶ Undo Last Label Edit", self.app)
#                     undo_label_action.triggered.connect(
#                         lambda _=False: QTimer.singleShot(10, self._undo_last_label_edit)
#                     )
#                     menu.addAction(undo_label_action)

#                 menu.exec(QCursor.pos())
#                 return

#         # ═══════════════════════════════════════════════════════
#         # CASE B: No label found - try picking point cloud
#         # ═══════════════════════════════════════════════════════
#         if not hasattr(self.app, 'data') or self.app.data is None:
#             return  # No data loaded
        
#         point_picker = vtk.vtkPointPicker()
#         point_picker.Pick(clickPos[0], clickPos[1], 0, renderer)
#         point_id = point_picker.GetPointId()
        
#         if point_id < 0:
#             return  # No point picked
        
#         # Find which grid this point belongs to
#         grid_name = self._find_grid_for_point(point_id)
        
#         # Create context menu
#         menu = QMenu(self.app)
#         menu.setStyleSheet("""
#             QMenu {
#                 background-color: #2c2c2c;
#                 color: #f0f0f0;
#                 border: 1px solid #555;
#                 padding: 5px;
#             }
#             QMenu::item {
#                 padding: 8px 30px;
#                 border-radius: 3px;
#             }
#             QMenu::item:selected {
#                 background-color: #3c3c3c;
#             }
#         """)
        
#         #     # Point belongs to a tracked grid
#         #     clear_action.triggered.connect(lambda: self.clear_grid_data(grid_name))
#         #     menu.addAction(clear_action)

#         menu.exec(QCursor.pos())
        
#     def _find_grid_for_point(self, point_id):
#         """Find which grid a point belongs to"""
#         for grid_name, indices in self.loaded_grids.items():
#             if point_id in indices:
#                 return grid_name
#         return None

#     def _resolve_grid_owner(self, grid_name):
#         """Map clicked grid label to actual loaded owner label when available."""
#         if not grid_name:
#             return grid_name
#         return self.grid_aliases.get(grid_name, grid_name)

#     def _get_alt_names_for_grid(self, grid_name):
#         """Get alternative names stored in BL polygons for a given grid_name."""
#         if not grid_name:
#             return []
#         alt = []
#         for blk in (getattr(self.app, 'snt_block_polygons', None) or []):
#             if blk.get("grid_name") == grid_name:
#                 alt.extend(blk.get("alt_names", []))
#         return alt

#     def _delete_area_by_point(self, point_id):
#         """Delete area around clicked point by selecting nearby points"""
#         from PySide6.QtWidgets import QInputDialog, QMessageBox
#         import numpy as np
        
#         # Ask for grid name
#         grid_name, ok = QInputDialog.getText(
#             self.app,
#             "Define Grid Area",
#             f"Clicked point: {point_id}\n\n"
#             f"Enter grid name to delete:"
#         )
        
#         if not ok or not grid_name:
#             return
        
#         # Ask for radius
#         radius, ok = QInputDialog.getDouble(
#             self.app,
#             "Selection Radius",
#             "Select points within radius (meters):",
#             10.0,  # default
#             1.0,   # min
#             100.0, # max
#             1      # decimals
#         )
        
#         if not ok:
#             return
        
#         # Find nearby points
#         point_xyz = self.app.data['xyz'][point_id]
#         all_xyz = self.app.data['xyz']
        
#         distances = np.linalg.norm(all_xyz - point_xyz, axis=1)
#         nearby_indices = np.where(distances <= radius)[0]
        
#         if len(nearby_indices) == 0:
#             QMessageBox.warning(self.app, "No Points", "No points found in selection area")
#             return
        
#         # Track and delete
#         self.loaded_grids[grid_name] = nearby_indices
        
#         reply = QMessageBox.question(
#             self.app,
#             "Confirm Selection",
#             f"Selected {len(nearby_indices):,} points\n"
#             f"within {radius}m radius\n\n"
#             f"Delete grid '{grid_name}'?",
#             QMessageBox.Yes | QMessageBox.No
#         )
        
#         if reply == QMessageBox.Yes:
#             self.delete_grid_data(grid_name)
    
#     def load_grid_las(self, grid_name, snt_filename=None, alt_names=None):
#         """
#         Main entry point: Load LAZ/LAS file for clicked grid.
#         Uses owner-aware detection before falling back to the existing DXF path.
#         """
#         print(f"\n{'='*60}")
#         print(f"📂 LOADING POINT CLOUD FOR GRID: {grid_name}")
#         if snt_filename:
#             print(f"   SNT owner: {snt_filename}")
#         if alt_names:
#             print(f"   Alt names: {alt_names}")
#         print(f"{'='*60}\n")

#         las_folder = None
#         if snt_filename:
#             las_folder = self._find_las_folder_from_snt(snt_filename)

#         if not las_folder and not snt_filename:
#             inferred_snt = self._infer_snt_filename_for_grid(grid_name)
#             if inferred_snt:
#                 print(f"   Inferred SNT owner: {inferred_snt}")
#                 snt_filename = inferred_snt
#                 las_folder = self._find_las_folder_from_snt(inferred_snt)

#         if not las_folder:
#             las_folder = self._find_las_folder_from_dxf()

#         if not las_folder:
#             las_folder = self._prompt_user_for_las_folder()

#         if not las_folder:
#             QMessageBox.warning(
#                 self.app,
#                 "Folder Not Found",
#                 "Could not locate LAZ/LAS folder.\n\n"
#                 "Please ensure LAZ/LAS files are in:\n"
#                 "- Same folder as the owning SNT/DXF\n"
#                 "- 'lazz' subfolder\n"
#                 "- 'laz' subfolder\n"
#                 "- 'las' subfolder"
#             )
#             return

#         print(f"   LAZ/LAS folder used: {las_folder}")

#         # Collect ALL candidate names: primary + direct aliases + block-file labels.
#         # The safe matcher already rejects ambiguous or incorrect identities, so
#         # we can try every block-local alias instead of filtering too early.
#         all_candidates = []
#         if grid_name:
#             all_candidates.append(grid_name)
#         block_file = ""
#         for blk in (getattr(self.app, 'snt_block_polygons', None) or []):
#             if blk.get("grid_name") == grid_name:
#                 block_file = str(blk.get("block_file") or "").strip()
#                 if block_file:
#                     break
#         for n in (block_file, *(alt_names or [])):
#             if n and n not in all_candidates:
#                 all_candidates.append(n)

#         # Also gather DXF grid label names inside any BL polygon for this area
#         for blk in (getattr(self.app, 'snt_block_polygons', None) or []):
#             if blk.get("grid_name") == grid_name:
#                 for an in blk.get("alt_names", []):
#                     if an and an not in all_candidates:
#                         all_candidates.append(an)

#         # Try each candidate against the folder's files
#         for candidate in all_candidates:
#             las_file = self._find_matching_las_file(las_folder, candidate)
#             if las_file:
#                 if candidate != grid_name:
#                     print(f"   ✅ Matched via alternative name '{candidate}'")
#                 self._load_las_file(las_file, grid_name)
#                 return

#         print(
#             f"\n   ❌ No exact file identity found for '{grid_name}'. "
#             "Opening the owning folder for manual selection."
#         )
#         self._show_file_selection_dialog(las_folder, grid_name)
    
#     def _find_las_folder_from_dxf(self):
#         """
#         ✅ UPDATED: Use full_path from dxf_actors
#         """
#         print("📋 STRATEGY 1: Auto-detect from DXF location")
        
#         if not hasattr(self.app, 'dxf_actors') or not self.app.dxf_actors:
#             print("   ❌ No DXF files loaded")
#             return None
        
#         for dxf_data in self.app.dxf_actors:
#             # ✅ Use full_path instead of filename
#             full_path = dxf_data.get('full_path')
            
#             if not full_path:
#                 # Fallback to filename (old code compatibility)
#                 filename = dxf_data.get('filename', '')
#                 if filename:
#                     dxf_path = Path(filename)
#                 else:
#                     continue
#             else:
#                 dxf_path = Path(full_path)
            
#             print(f"   DXF file: {dxf_path.name}")
#             print(f"   DXF folder: {dxf_path.parent}")
#             print(f"   Path exists: {dxf_path.exists()}")
            
#             if not dxf_path.exists():
#                 print(f"   ⚠️ Path doesn't exist: {dxf_path}")
#                 continue
            
#             dxf_folder = dxf_path.parent
            
#             # Check cache
#             if str(dxf_folder) in self.folder_cache:
#                 cached = self.folder_cache[str(dxf_folder)]
#                 print(f"   ✅ Using cached folder: {cached}")
#                 return cached
            
#             # Same folder as DXF
#             las_files = list(dxf_folder.glob("*.laz")) + list(dxf_folder.glob("*.las"))
#             if las_files:
#                 print(f"   ✅ FOUND {len(las_files)} LAZ/LAS files in DXF folder")
#                 self.folder_cache[str(dxf_folder)] = dxf_folder
#                 self._save_folder_to_settings(dxf_folder)
#                 return dxf_folder
            
#             # Check subfolders
#             for subfolder_name in ['lazz', 'LAZZ', 'laz', 'LAZ', 'las', 'LAS']:
#                 subfolder = dxf_folder / subfolder_name
                
#                 if subfolder.exists() and subfolder.is_dir():
#                     las_files = list(subfolder.glob("*.laz")) + list(subfolder.glob("*.las"))
                    
#                     if las_files:
#                         print(f"   ✅ FOUND {len(las_files)} files in '{subfolder_name}' subfolder")
#                         self.folder_cache[str(dxf_folder)] = subfolder
#                         self._save_folder_to_settings(subfolder)
#                         return subfolder

#             # Also check ALL immediate subfolders
#             try:
#                 for entry in dxf_folder.iterdir():
#                     if entry.is_dir():
#                         las_files = list(entry.glob("*.laz")) + list(entry.glob("*.las"))
#                         if las_files:
#                             print(f"   ✅ FOUND {len(las_files)} files in '{entry.name}' subfolder")
#                             self.folder_cache[str(dxf_folder)] = entry
#                             self._save_folder_to_settings(entry)
#                             return entry
#             except Exception:
#                 pass
        
#         return None
        
#     def _prompt_user_for_las_folder(self):
#         """
#         Strategy 2: Ask user to select LAZ/LAS folder
#         Only happens once - cached for future use
#         """
#         print("\n📋 STRATEGY 2: Prompt user for folder")
        
#         reply = QMessageBox.question(
#             self.app,
#             "Select LAZ/LAS Folder",
#             "Could not auto-detect LAZ/LAS folder.\n\n"
#             "Would you like to select the folder manually?\n\n"
#             "(This will be remembered for future clicks)",
#             QMessageBox.Yes | QMessageBox.No,
#             QMessageBox.Yes
#         )
        
#         if reply == QMessageBox.No:
#             return None
        
#         folder = QFileDialog.getExistingDirectory(
#             self.app,
#             "Select LAZ/LAS Folder",
#             "",
#             QFileDialog.ShowDirsOnly
#         )
        
#         if folder:
#             folder_path = Path(folder)
            
#             # Verify it contains LAZ/LAS files
#             las_files = list(folder_path.glob("*.laz")) + list(folder_path.glob("*.las"))
            
#             if not las_files:
#                 QMessageBox.warning(
#                     self.app,
#                     "No Files Found",
#                     f"Selected folder contains no LAZ/LAS files:\n{folder}"
#                 )
#                 return None
            
#             print(f"   ✅ User selected: {folder_path} ({len(las_files)} files)")
#             self._save_folder_to_settings(folder_path)
#             return folder_path
        
#         return None
    
#     def _find_matching_las_file(self, las_folder, grid_name):
#         """
#         Find the unique LAZ/LAS file with the same complete grid identity.

#         A partial coordinate (for example, matching only the northing) is never
#         accepted. Missing or ambiguous matches return None so the caller opens
#         the owning folder instead of loading a neighbouring grid.
#         """
#         print(f"\n🔍 Searching for file matching: {grid_name}")
        
#         las_folder_path = Path(las_folder)
        
#         # Get all LAZ/LAS files
#         all_files = list(las_folder_path.glob("*.laz")) + list(las_folder_path.glob("*.las"))
        
#         print(f"   Total files in folder: {len(all_files)}")
        
#         if not all_files:
#             return None

#         grid_name_clean = strip_lidar_extension(grid_name)
#         best = find_matching_lidar_file(all_files, grid_name_clean)

#         if best is None:
#             print(
#                 f"   ❌ No unique full-identity match for '{grid_name_clean}' "
#                 f"(norm='{normalized_stem(grid_name_clean)}', "
#                 f"numbers={numeric_identity(grid_name_clean)})"
#             )
#             print(f"   📄 Available files:")
#             for fp in all_files[:10]:
#                 print(
#                     f"      - {fp.name} "
#                     f"(norm='{normalized_stem(fp.stem)}', "
#                     f"numbers={numeric_identity(fp.stem)})"
#                 )
#             return None

#         print(f"   ✅ EXACT GRID MATCH FOUND: {best.name}")
#         return best
    
#     # def _extract_patterns(self, grid_name):
#     #     """
#     #     Extract search patterns from grid name
        
#     #     Example: "DW3032726_000005" produces:
#     #     - DW3032726_000005 (exact)
#     #     - DW3032726_5 (without leading zeros)
#     #     - 000005 (just number)
#     #     - 5 (number without zeros)
#     #     - DW3032726 (base prefix)
#     #     """
#     #     patterns = [grid_name]  # Always try exact match first
        
#     #     # Split by underscore or space
#     #     parts = grid_name.replace(' ', '_').split('_')
        
#     #     for part in parts:
#     #         if part:
#     #             patterns.append(part)
                
#     #             # Try removing leading zeros
#     #             if part.isdigit():
#     #                 patterns.append(str(int(part)))
        
#     #     # Try base prefix (before first underscore/space)
#     #     if '_' in grid_name:
#     #         base = grid_name.split('_')[0]
#     #         if base:
#     #             patterns.append(base)
        
#     #     # Remove duplicates while preserving order
#     #     seen = set()
#     #     unique_patterns = []
#     #     for p in patterns:
#     #         if p not in seen:
#     #             seen.add(p)
#     #             unique_patterns.append(p)
        
#     #     return unique_patterns

#     def _extract_patterns(self, grid_name):
#         patterns = [grid_name]  # Always try exact match first

#         # Split by underscore or space
#         parts = grid_name.replace(' ', '_').split('_')

#         numeric_parts = []
#         non_numeric_parts = []

#         for part in parts:
#             if part:
#                 if part.isdigit():
#                     numeric_parts.append(part)
#                     numeric_parts.append(str(int(part)))  # strip leading zeros variant
#                 else:
#                     non_numeric_parts.append(part)

#         # ✅ Numeric suffixes FIRST — most specific (e.g. '000021', '21')
#         patterns.extend(numeric_parts)

#         # ✅ Non-numeric / base prefix LAST — too broad, matches everything
#         patterns.extend(non_numeric_parts)

#         # Try base prefix (before first underscore/space) — also last
#         if '_' in grid_name:
#             base = grid_name.split('_')[0]
#             if base:
#                 patterns.append(base)

#         # Remove duplicates while preserving order
#         seen = set()
#         unique_patterns = []
#         for p in patterns:
#             if p not in seen:
#                 seen.add(p)
#                 unique_patterns.append(p)

#         return unique_patterns    
    
#     def _load_las_file(self, las_file, grid_name):
#         """
#         Load LAZ/LAS file using the EXACT same path as menu bar loading.
#         ✅ Mirrors open_file() logic exactly to ensure consistent behavior
#         """
#         from PySide6.QtCore import QCoreApplication
#         from PySide6.QtWidgets import QMessageBox
#         from gui.progress_dialog import LoadingProgressDialog
#         import os
#         import time
#         import numpy as np

#         print(f"\n{'='*70}")
#         print(f"📂 GRID LOAD - USING MENU BAR PATH")
#         print(f"   Grid: {grid_name}")
#         print(f"   File: {las_file.name}")
#         print(f"{'='*70}")

#         def _reset_interaction_state():
#             """Clear stale zoom/pan state before or after a grid swap."""
#             try:
#                 cancel_zoom = getattr(self.app, "_cancel_smooth_zoom_for_pan", None)
#                 if callable(cancel_zoom):
#                     cancel_zoom()
#             except Exception:
#                 pass

#             try:
#                 digitizer = getattr(self.app, "digitizer", None)
#                 if digitizer is not None:
#                     if hasattr(digitizer, "_reset_pan_state"):
#                         digitizer._reset_pan_state()
#                     if hasattr(digitizer, "_reset_stale_zoom_mouse_state"):
#                         digitizer._reset_stale_zoom_mouse_state()
#             except Exception as e:
#                 print(f"⚠️ Interaction reset skipped: {e}")

#             try:
#                 ztool = getattr(self.app, "zoom_rectangle_tool", None)
#                 if ztool is not None:
#                     ztool.is_dragging_zoom = False
#                     ztool.start_pos = None
#                     ztool.end_pos = None
#                     ztool.is_panning = False
#                     ztool.last_pan_pos = None
#                     if hasattr(ztool, "_block_middle_until"):
#                         ztool._block_middle_until = 0.0
#             except Exception:
#                 pass

#         # ============================================================================
#         # STEP 1: AUTO-SAVE CURRENT FILE (if exists) - SAME AS MENU BAR
#         # ============================================================================
#         _reset_interaction_state()
#         if hasattr(self.app, 'data') and self.app.data is not None:
#             save_path = getattr(self.app, 'last_save_path', None) or getattr(self.app, 'loaded_file', None)

#             # Only save if there are points (prevent saving empty cleared state)
#             current_point_count = len(self.app.data.get('xyz', [])) if self.app.data else 0

#             # Never overwrite the source file when the current load is class-filtered
#             # (partial data). Writing filtered points back would corrupt the original.
#             _class_filtered = getattr(self.app, '_loaded_with_class_filter', False)

#             if save_path and current_point_count > 0 and not _class_filtered:
#                 try:
#                     print(f"\n💾 AUTO-SAVING CURRENT FILE")
#                     print(f"   Path: {os.path.basename(save_path)}")
#                     print(f"   Points: {current_point_count:,}")
                    
#                     from gui.save_pointcloud import save_pointcloud_quick
#                     result = save_pointcloud_quick(self.app, save_path)
                    
#                     if result:
#                         print(f"✅ Saved successfully")
#                         if hasattr(self.app, "statusBar"):
#                             self.app.statusBar().showMessage(f"💾 Saved: {os.path.basename(save_path)}", 2000)
#                             QCoreApplication.processEvents()
#                     else:
#                         print(f"⚠️ Save returned False")
                        
#                 except Exception as e:
#                     print(f"❌ SAVE FAILED: {e}")
#                     pass

#         # ============================================================================
#         # STEP 2: CLEAR GRID TRACKING
#         # ============================================================================
#         try:
#             # Persist current file-specific display/PTC state before data clear.
#             from gui.clear_project import _save_display_settings_before_clear
#             _save_display_settings_before_clear(self.app)
#             print("[RUNTIME-CHECK] pre-clear-save caller=grid_label_system step=grid-switch")
#         except Exception as e:
#             print(f"⚠️ Display settings pre-save skipped: {e}")

#         # Stop queued debounced refresh callbacks from previous dataset.
#         try:
#             timer = getattr(self.app, "_update_debounce_timer", None)
#             if timer is not None and timer.isActive():
#                 timer.stop()
#             pending = getattr(self.app, "_pending_view_updates", None)
#             if hasattr(pending, "clear"):
#                 pending.clear()
#             if hasattr(self.app, "_last_changed_mask"):
#                 self.app._last_changed_mask = None
#             if hasattr(self.app, "_last_changed_indices"):
#                 self.app._last_changed_indices = None
#         except Exception:
#             pass

#         # Align grid-switch cleanup with open-file cleanup hooks.
#         try:
#             from gui.memory_manager import ObserverRegistry, release_data_arrays
#             from gui.unified_actor_manager import reset_uam
#             release_data_arrays(self.app)
#             ObserverRegistry.release_all()
#             reset_uam(self.app)
#             mem_guard = getattr(self.app, "_mem_guard", None)
#             if mem_guard is not None:
#                 mem_guard.force_gc()
#         except Exception as mem_exc:
#             print(f"⚠️ Memory manager clear hook skipped: {mem_exc}")

#         self.loaded_grids.clear()
#         self.grid_aliases.clear()

#        # ============================================================================
#         # SAVE CAMERA STATE BEFORE CLEARING
#         # ============================================================================
#         saved_camera_state = None
#         if hasattr(self.app, "vtk_widget") and self.app.vtk_widget:
#             try:
#                 camera = self.app.vtk_widget.renderer.GetActiveCamera()
#                 saved_camera_state = {
#                     'position': camera.GetPosition(),
#                     'focal_point': camera.GetFocalPoint(),
#                     'view_up': camera.GetViewUp(),
#                     'parallel_scale': camera.GetParallelScale(),
#                     'parallel_projection': camera.GetParallelProjection()
#                 }
#                 print(f"💾 Camera state saved (zoom: {saved_camera_state['parallel_scale']:.2f})")
#             except Exception as e:
#                 print(f"⚠️ Could not save camera state: {e}")
        
#         # Backup DXF actors
#         dxf_backup = []
#         if hasattr(self.app, 'dxf_actors') and self.app.dxf_actors:
#             for dxf_data in self.app.dxf_actors:
#                 for actor in dxf_data.get('actors', []):
#                     dxf_backup.append(actor)
            
#             if dxf_backup:
#                 renderer = self.app.vtk_widget.renderer
#                 for actor in dxf_backup:
#                     renderer.RemoveActor(actor)
#                 print(f"   💾 Backed up {len(dxf_backup)} DXF actors")
        
#         # Clear VTK completely
#         if hasattr(self.app, "vtk_widget") and self.app.vtk_widget:
#             renderer = self.app.vtk_widget.renderer
#             renderer.RemoveAllViewProps()
            
#             if hasattr(self.app.vtk_widget, 'actors'):
#                 self.app.vtk_widget.actors.clear()
#             if hasattr(self.app.vtk_widget, '_actors'):
#                 self.app.vtk_widget._actors.clear()
            
#             self.app.vtk_widget.render()
#             print(f"   ✅ VTK cleared")
        
#         # Clear cross-sections
#         if hasattr(self.app, 'section_vtks') and self.app.section_vtks:
#             for view_idx, vtk_widget in self.app.section_vtks.items():
#                 try:
#                     vtk_widget.renderer.RemoveAllViewProps()
#                     if hasattr(vtk_widget, 'actors'):
#                         vtk_widget.actors.clear()
#                     vtk_widget.render()
#                 except Exception:
#                     pass
#             print(f"   ✅ Cross-sections cleared")

#         # Clear cut section state/view to prevent stale index-map from previous file
#         if hasattr(self.app, 'cut_section_controller') and self.app.cut_section_controller:
#             try:
#                 self.app.cut_section_controller.clear()
#                 print(f"   ✅ Cut section cleared")
#             except Exception as e:
#                 print(f"   ⚠️ Cut section clear failed: {e}")
#                 try:
#                     ctrl = self.app.cut_section_controller
#                     ctrl.cut_points = None
#                     ctrl._cut_index_map = None
#                     ctrl.is_cut_view_active = False
#                     print("   ✅ Applied fallback cut-state reset")
#                 except Exception:
#                     pass
        
#         # Clear ALL internal state - SAME AS MENU BAR
#         self.app.data = None
#         self.app._loaded_with_class_filter = False
#         self.app.loaded_file = None
#         self.app.last_save_path = None
#         self.app.class_palette = {}
#         # ✅ FIX: Clear stale Z-bounds cache so SNT actors land at the correct Z
#         # for the incoming LAZ file. Without this, _get_snt_z_offset reads the old
#         # file's z_max and the SNT grid appears above the new point cloud.
#         self.app.data_bounds = None

#         # Clear layers so previous file arrays are not retained across grid switches.
#         if hasattr(self.app, "layers") and isinstance(self.app.layers, list):
#             self.app.layers.clear()
#         if hasattr(self.app, "layers_dock") and self.app.layers_dock:
#             try:
#                 if hasattr(self.app.layers_dock, "clear_layers"):
#                     self.app.layers_dock.clear_layers()
#             except Exception:
#                 pass

#         # Drop stale section caches/masks bound to previous dataset.
#         try:
#             import re
#             stale_section_attrs = [
#                 name for name in list(vars(self.app).keys())
#                 if re.match(r"^section_\d+_", name) or re.match(r"^_section_\d+_", name)
#             ]
#             for name in stale_section_attrs:
#                 try:
#                     delattr(self.app, name)
#                 except Exception:
#                     pass
#         except Exception:
#             pass
        
#         if hasattr(self.app, "view_palettes"):
#             self.app.view_palettes.clear()
        
#         if hasattr(self.app, 'undo_stack'):
#             self.app.undo_stack.clear()
#         if hasattr(self.app, 'redo_stack'):
#             self.app.redo_stack.clear()
        
#         if hasattr(self.app, 'spatial_index'):
#             self.app.spatial_index = None
        
#         print(f"   ✅ All data cleared")
        
#         QCoreApplication.processEvents()
        
#         # Restore DXF actors
#         if dxf_backup:
#             renderer = self.app.vtk_widget.renderer
#             for actor in dxf_backup:
#                 renderer.AddActor(actor)
#             self.app.vtk_widget.render()
#             QCoreApplication.processEvents()
#             print(f"   ✅ Restored {len(dxf_backup)} DXF actors")
        
#         print(f"{'='*60}")
#         print(f"✅ CLEAR COMPLETE")
#         print(f"{'='*60}\n")

#         # ============================================================================
#         # STEP 4: LOAD FILE - SAME AS MENU BAR
#         # ============================================================================
#         print(f"{'='*60}")
#         print(f"📂 LOADING FILE - SAME AS MENU BAR")
#         print(f"{'='*60}")
        
#         progress = LoadingProgressDialog(self.app, show_cancel=False)
#         progress.set_filename(os.path.basename(str(las_file)))
#         progress.show()

#         def update_progress(percent, status, force=False):
#             if force or not hasattr(update_progress, '_last_update'):
#                 progress.set_progress(percent)
#                 progress.set_status(status)
#                 QCoreApplication.processEvents()
#                 update_progress._last_update = time.time()
#             else:
#                 if time.time() - update_progress._last_update > 0.2:
#                     progress.set_progress(percent)
#                     progress.set_status(status)
#                     QCoreApplication.processEvents()
#                     update_progress._last_update = time.time()

#         load_start = time.time()

#         try:
#             # ============================================================================
#             # LOAD THE FILE - SAME AS MENU BAR
#             # ============================================================================
#             update_progress(10, "Loading file...", force=True)
            
#             from gui.data_loader import load_lidar_file

#             tile_data = load_lidar_file(str(las_file), parent=self.app)

#             if not tile_data:
#                 progress.finish_error("Load cancelled or failed")
#                 return

#             total_points = len(tile_data.get('xyz', []))
#             print(f"   ✅ Loaded {total_points:,} points")
            
            
#             # ============================================================================
#             # 🔍 DEBUG: VERIFY CORRECT FILE WAS LOADED
#             # ============================================================================
#             print(f"\n{'='*70}")
#             print(f"🔍 FILE LOAD VERIFICATION")
#             print(f"{'='*70}")
#             print(f"   Requested Grid: {grid_name}")
#             print(f"   Loaded File: {las_file.name}")
#             print(f"   Full Path: {las_file}")

#             # Calculate center of loaded data
#             center = np.mean(tile_data['xyz'], axis=0)
#             print(f"   Data Center: [{center[0]:.1f}, {center[1]:.1f}, {center[2]:.1f}]")

#             # Calculate bounds
#             xyz = tile_data['xyz']
#             bounds = {
#                 'x_min': np.min(xyz[:, 0]),
#                 'x_max': np.max(xyz[:, 0]),
#                 'y_min': np.min(xyz[:, 1]),
#                 'y_max': np.max(xyz[:, 1]),
#             }
#             print(f"   Bounds:")
#             print(f"      X: {bounds['x_min']:.1f} to {bounds['x_max']:.1f}")
#             print(f"      Y: {bounds['y_min']:.1f} to {bounds['y_max']:.1f}")

#             # Try to read file header directly to confirm
#             try:
#                 import laspy
#                 with laspy.open(str(las_file)) as f:
#                     header = f.header
#                     print(f"   LAS Header Info:")
#                     print(f"      Point Count: {header.point_count:,}")
#                     print(f"      X Range: {header.x_min:.1f} to {header.x_max:.1f}")
#                     print(f"      Y Range: {header.y_min:.1f} to {header.y_max:.1f}")
#             except Exception as e:
#                 print(f"   ⚠️ Could not read LAS header: {e}")

#             print(f"{'='*70}\n")
#             # =====================
            
#             # ============================================================================
#             # SET DATA - SAME AS MENU BAR
#             # ============================================================================
#             update_progress(50, "Setting data...", force=True)
            
#             # ✅ COORDINATE FIX: Detect if point cloud coords are wildly off from grid coords
#             # Only triggers for MASSIVE mismatches (>100km) caused by missing LAS header offsets
#             # e.g., point cloud at [5558, 45034] instead of [555800, 4503400]
#             loaded_xyz = tile_data["xyz"]
#             try:
#                 loaded_center = np.mean(loaded_xyz, axis=0)
                
#                 # Compute grid center from ALL actor bounds (not just first one)
#                 all_x_min, all_x_max = float('inf'), float('-inf')
#                 all_y_min, all_y_max = float('inf'), float('-inf')
#                 found_bounds = False
                
#                 for store_name in ('snt_actors', 'dxf_actors'):
#                     store = getattr(self.app, store_name, None)
#                     if store:
#                         for entry in store:
#                             for actor in entry.get('actors', []):
#                                 try:
#                                     b = actor.GetBounds()
#                                     if b and (b[1] - b[0]) > 0.1 and (b[3] - b[2]) > 0.1:
#                                         all_x_min = min(all_x_min, b[0])
#                                         all_x_max = max(all_x_max, b[1])
#                                         all_y_min = min(all_y_min, b[2])
#                                         all_y_max = max(all_y_max, b[3])
#                                         found_bounds = True
#                                 except Exception:
#                                     continue
                
#                 if found_bounds:
#                     grid_center = np.array([
#                         (all_x_min + all_x_max) / 2,
#                         (all_y_min + all_y_max) / 2,
#                         0
#                     ])
#                     dist_xy = np.sqrt((loaded_center[0] - grid_center[0])**2 + 
#                                      (loaded_center[1] - grid_center[1])**2)
                    
#                     # Only trigger for MASSIVE mismatches (>100km = missing LAS offset)
#                     # Normal DXF grids can span 10-20km, so 100km threshold prevents false positives
#                     if dist_xy > 100_000:
#                         print(f"   ⚠️ COORDINATE MISMATCH DETECTED!")
#                         print(f"      Point cloud center: [{loaded_center[0]:.1f}, {loaded_center[1]:.1f}]")
#                         print(f"      Grid center: [{grid_center[0]:.1f}, {grid_center[1]:.1f}]")
#                         print(f"      Distance: {dist_xy:.0f}m — applying LAS header offset...")
                        
#                         try:
#                             import laspy
#                             with laspy.open(str(las_file)) as f:
#                                 hdr = f.header
#                                 hdr_center_x = (hdr.x_min + hdr.x_max) / 2
#                                 hdr_center_y = (hdr.y_min + hdr.y_max) / 2
                                
#                                 hdr_dist = np.sqrt((hdr_center_x - grid_center[0])**2 + 
#                                                    (hdr_center_y - grid_center[1])**2)
                                
#                                 if hdr_dist < dist_xy:
#                                     offset_x = hdr_center_x - loaded_center[0]
#                                     offset_y = hdr_center_y - loaded_center[1]
                                    
#                                     # Only apply if offset is significant (>10km)
#                                     if abs(offset_x) > 10_000 or abs(offset_y) > 10_000:
#                                         print(f"      LAS header center: [{hdr_center_x:.1f}, {hdr_center_y:.1f}]")
#                                         print(f"      Applying offset: [{offset_x:.1f}, {offset_y:.1f}]")
#                                         loaded_xyz[:, 0] += offset_x
#                                         loaded_xyz[:, 1] += offset_y
#                                         tile_data["xyz"] = loaded_xyz
#                                         new_center = np.mean(loaded_xyz, axis=0)
#                                         print(f"      ✅ Corrected center: [{new_center[0]:.1f}, {new_center[1]:.1f}]")
#                                     else:
#                                         print(f"      ⚠️ Offset too small ({offset_x:.1f}, {offset_y:.1f}) — skipping")
#                                 else:
#                                     print(f"      ⚠️ Header coords also don't match grid — skipping")
#                         except Exception as e:
#                             print(f"      ⚠️ Could not read LAS header: {e}")
#                     else:
#                         print(f"   ✅ Coordinates match grid (distance: {dist_xy:.0f}m)")
#             except Exception as e:
#                 print(f"   ⚠️ Coordinate check failed: {e}")
            
#             self.app.data = {
#                 "xyz": tile_data["xyz"],
#                 "classification": tile_data["classification"]
#             }
#             xyz_app = self.app.data["xyz"]
#             print(
#                 "   COORD_TRACE APP.DATA: "
#                 f"X=[{xyz_app[:, 0].min():.3f}, {xyz_app[:, 0].max():.3f}] "
#                 f"Y=[{xyz_app[:, 1].min():.3f}, {xyz_app[:, 1].max():.3f}]"
#             )

#             # Track whether this load was class-filtered so auto-save can
#             # refuse to overwrite the source file with a partial dataset.
#             _load_opts = tile_data.get("import_options") or {}
#             self.app._loaded_with_class_filter = bool(
#                 _load_opts.get("only_class") and _load_opts.get("class_codes")
#             )

#             if tile_data.get("rgb") is not None:
#                 self.app.data["rgb"] = tile_data["rgb"]
#             if tile_data.get("intensity") is not None:
#                 self.app.data["intensity"] = tile_data["intensity"]
            
#             # Set CRS - SAME AS MENU BAR
#             if tile_data.get("crs_epsg"):
#                 self.app.project_crs_epsg = tile_data["crs_epsg"]
#                 self.app.project_crs_wkt = tile_data.get("crs_wkt")
                
#                 try:
#                     from pyproj import CRS
#                     self.app.crs = CRS.from_epsg(tile_data["crs_epsg"])
#                     print(f"   📐 CRS: {self.app.crs.name}")
#                 except Exception:
#                     pass
            
#             # Store as layer - SAME AS MENU BAR
#             layer = {
#                 "type": "laz_tile",
#                 "filename": str(las_file),
#                 "xyz": tile_data["xyz"],
#                 "classification": tile_data.get("classification"),
#                 "rgb": tile_data.get("rgb"),
#                 "intensity": tile_data.get("intensity"),
#                 "crs_epsg": tile_data.get("crs_epsg"),
#                 "visible": True,
#             }
            
#             if hasattr(self.app, 'layers'):
#                 self.app.layers.append(layer)
            
#             if hasattr(self.app, 'layers_dock') and self.app.layers_dock:
#                 self.app.layers_dock.add_layer(layer)
            
#             # Set file paths
#             self.app.loaded_file = str(las_file)
#             self.app.last_save_path = str(las_file)
            
#             # ============================================================================
#             # BUILD DEM FOR SHADING - SAME AS MENU BAR
#             # ============================================================================
#             try:
#                 from gui.shading_display import build_base_dem_mesh
#                 build_base_dem_mesh(self.app, percentile_filter=99.9, downsample=2)
#             except Exception:
#                 pass
            
#             # ============================================================================
#             # BUILD SPATIAL INDEX - SAME AS MENU BAR
#             # ============================================================================
#             if total_points > 50_000:
#                 try:
#                     update_progress(70, "Building spatial index...", force=True)
#                     from gui.performance_optimizations import SpatialIndex
#                     self.app.spatial_index = SpatialIndex(self.app.data["xyz"])
#                     print(f"   ✅ Spatial index built")
#                 except Exception as e:
#                     print(f"   ⚠️ Spatial index failed: {e}")
#                     self.app.spatial_index = None
            
#             # ============================================================================
#             # RESTORE DISPLAY SETTINGS - SAME AS MENU BAR
#             # ============================================================================
#             update_progress(75, "Restoring settings...", force=True)
            
#             # Set default display mode first
#             self.app.display_mode = "class"

#             try:
#                 from gui.display_mode import restore_display_settings_for_file
#                 self.app._prefer_session_display_restore = True
#                 try:
#                     restore_display_settings_for_file(self.app, str(las_file))
#                 finally:
#                     self.app._prefer_session_display_restore = False
#             except Exception:
#                 self.app._prefer_session_display_restore = False
#                 pass

#             # Grid switching is a dataset load too: all main/cross/cut slots
#             # must begin in Structured border mode.
#             from gui.unified_actor_manager import reset_border_logic_to_structured
#             reset_border_logic_to_structured(self.app)

#             # If the user loaded with class filter, force class display mode regardless
#             # of what restore_display_settings_for_file restored (e.g. "surface").
#             # This ensures Display Mode palette changes work immediately after load.
#             _load_opts = tile_data.get("import_options") or {}
#             if _load_opts.get("only_class"):
#                 self.app.display_mode = "class"
            
#             # ============================================================================
#             # LOAD PALETTE - SAME AS MENU BAR
#             # ============================================================================
#             update_progress(80, "Loading palette...", force=True)
            
#             palette_to_apply = None
#             if hasattr(self.app, '_get_palette_for_file'):
#                 palette_to_apply = self.app._get_palette_for_file(str(las_file))
            
#             # ============================================================================
#             # APPLY PALETTE AND RENDER - SAME AS MENU BAR
#             # ============================================================================
#             # This is critical for per-class visibility and updates!
            
#             if palette_to_apply:
#                 # If user selected specific classes on load, hide all others.
#                 # All points remain in memory for Display Mode re-apply.
#                 _load_opts = tile_data.get("import_options") or {}
#                 if _load_opts.get("only_class"):
#                     from gui.display_mode import clone_palette
#                     _sel = set(int(c) for c in (_load_opts.get("class_codes") or []))
#                     palette_to_apply = clone_palette(palette_to_apply)
#                     for _code, _entry in palette_to_apply.items():
#                         _entry["show"] = (_code in _sel)
#                     print(f"   👁 Initial visibility: showing classes {sorted(_sel)}")

#                 visible_count = len([c for c, v in palette_to_apply.items() if v.get("show")])
#                 update_progress(85, f"Rendering {visible_count} classes...", force=True)

#                 print(f"🎨 Applying palette with {visible_count} visible classes...")

#                 # Apply palette which will create Per-Class Actors
#                 self.app.apply_class_map({
#                     "classes": palette_to_apply,
#                     "slot": 0,
#                     "color_mode": 0,
#                     "target_view": 0,
#                 })
#             else:
#                 # Fallback: build palette from classification
#                 update_progress(85, "Building palette...", force=True)
                
#                 try:
#                     from gui.class_display import build_class_palette, update_class_mode
#                     self.app.class_palette = build_class_palette(tile_data['classification'])
#                     print(f"   ✅ Built palette: {len(self.app.class_palette)} classes")
                    
#                     # Use apply_class_map for consistent behavior (creates per-class actors)
#                     self.app.apply_class_map({
#                         "classes": self.app.class_palette,
#                         "slot": 0,
#                         "color_mode": 0,
#                         "target_view": 0
#                     })
#                 except Exception as e:
#                     print(f"   ⚠️ Palette build failed: {e}")
#                     # Ultimate fallback (creates unified cloud - NOT ideal but works)
#                     from gui.pointcloud_display import update_pointcloud
#                     update_pointcloud(self.app, "class")
            
#             # ============================================================================
#             # RESTORE CAMERA STATE (preserve zoom when loading into DXF)
#             # ============================================================================
#             if saved_camera_state:
#                 try:
#                     camera = self.app.vtk_widget.renderer.GetActiveCamera()
#                     camera.SetPosition(saved_camera_state['position'])
#                     camera.SetFocalPoint(saved_camera_state['focal_point'])
#                     camera.SetViewUp(saved_camera_state['view_up'])
#                     camera.SetParallelScale(saved_camera_state['parallel_scale'])
#                     camera.SetParallelProjection(saved_camera_state['parallel_projection'])
#                     self.app.vtk_widget.renderer.ResetCameraClippingRange()
#                     print(f"📷 Camera restored (zoom: {saved_camera_state['parallel_scale']:.2f})")
#                 except Exception as e:
#                     print(f"⚠️ Camera restore failed: {e}")
            
#             # ============================================================================
#             # FINALIZE - SAME AS MENU BAR
#             # ============================================================================
#             update_progress(95, "Finalizing...", force=True)
            
#             try:
#                 from gui.pointcloud_display import force_interactor_ready
#                 force_interactor_ready(self.app, delay_ms=300)
#             except Exception:
#                 pass
            
#             # Toggle view mode - BUT DON'T RESET CAMERA IF WE SAVED STATE
#             if hasattr(self.app, 'toggle_view_mode'):
#                 if saved_camera_state is None:
#                     # First load - reset to 2D view
#                     self.app.toggle_view_mode("2d")
#                 else:
#                     # Loading into DXF - preserve camera
#                     print("📷 Skipping view reset (preserving zoom)")
            
#             # ✅ RESTORE CAMERA STATE AFTER VIEW MODE WITH SMART ADJUSTMENT
#             if saved_camera_state:
#                 try:
#                     import numpy as np
#                     camera = self.app.vtk_widget.renderer.GetActiveCamera()

#                     # Guard: data must still be present (it could be None if load
#                     # failed after camera save but before we reach this point).
#                     xyz = (self.app.data or {}).get('xyz') if self.app.data is not None else None
#                     if xyz is None or len(xyz) == 0:
#                         raise ValueError("No point data available for camera adjustment")

#                     # Calculate the offset between old and new data centers
#                     new_data_center = np.mean(xyz, axis=0)
#                     old_focal_point = np.array(saved_camera_state['focal_point'])
                    
#                     # ✅ FIX: Only shift XY — do NOT shift Z.
#                     # The saved focal_point Z comes from the previous camera state
#                     # (often near 0 for a top-down 2D view), while new_data_center Z
#                     # is the actual elevation of the new LAZ file (e.g. 550 m).
#                     # Applying the Z shift moves the focal plane far away from the
#                     # actual geometry, which corrupts ResetCameraClippingRange() and
#                     # makes every pan event lag or "slide" during interaction.
#                     # Z must stay at the new data's median elevation set by fit_view.
#                     shift_xy = new_data_center[:2] - old_focal_point[:2]
#                     shift_3d = np.array([shift_xy[0], shift_xy[1], 0.0])
                    
#                     new_position  = np.array(saved_camera_state['position'])  + shift_3d
#                     new_focal_point = old_focal_point + shift_3d
                    
#                     # Restore camera with adjusted position
#                     camera.SetPosition(new_position[0], new_position[1], new_position[2])
#                     camera.SetFocalPoint(new_focal_point[0], new_focal_point[1], new_focal_point[2])
#                     camera.SetViewUp(saved_camera_state['view_up'])
#                     camera.SetParallelScale(saved_camera_state['parallel_scale'])  # ✅ Preserves zoom!
#                     camera.SetParallelProjection(saved_camera_state['parallel_projection'])
                    
#                     self.app.vtk_widget.renderer.ResetCameraClippingRange()
#                     self.app.vtk_widget.render()
                    
#                     print(f"📷 Camera restored with smart adjustment")
#                     print(f"   Zoom: {saved_camera_state['parallel_scale']:.2f}")
#                     print(f"   Old center: [{old_focal_point[0]:.1f}, {old_focal_point[1]:.1f}]")
#                     print(f"   New center: [{new_data_center[0]:.1f}, {new_data_center[1]:.1f}]")
#                     print(f"   Shift: [{shift_xy[0]:.1f}, {shift_xy[1]:.1f}]")
#                 except Exception as e:
#                     print(f"⚠️ Camera restore failed: {e}")
#                     import traceback
#                     traceback.print_exc()

            
#             loaded_label = Path(las_file).stem
#             owner_label = loaded_label if loaded_label else grid_name

#             if hasattr(self.app, 'ensure_main_view_2d_interaction'):
#                 self.app.ensure_main_view_2d_interaction(
#                     preserve_camera=True,
#                     reason=f"grid load: {owner_label}",
#                 )

#             try:
#                 digitizer = getattr(self.app, "digitizer", None)
#                 if digitizer is not None:
#                     if hasattr(digitizer, "_check_and_update_renderers"):
#                         digitizer._check_and_update_renderers()
#                     if hasattr(digitizer, "_reinstall_all_observers"):
#                         digitizer._reinstall_all_observers()
#                     print("   ✅ Digitizer fully restored (grid load)")
#             except Exception as dig_err:
#                 print(f"   ⚠️ Digitizer restore skipped: {dig_err}")

#             _reset_interaction_state()

#             # Update title with grid name
#             self.app._update_window_title(
#                 f"{owner_label} ({total_points:,} pts)", 
#                 getattr(self.app, 'project_crs_epsg', None)
#             )
            
#             # ============================================================================
#             # AUTO-LOAD DRAWINGS - SAME AS MENU BAR
#             # ============================================================================
#             if hasattr(self.app, "digitizer") and self.app.digitizer:
#                 try:
#                     self.app.digitizer.auto_load_drawings(str(las_file))
#                 except Exception:
#                     pass
            
#             # ============================================================================
#             # UPDATE STATISTICS - SAME AS MENU BAR
#             # ============================================================================
#             if hasattr(self.app, 'point_count_widget') and self.app.point_count_widget:
#                 try:
#                     from gui.point_count_widget import refresh_point_statistics
#                     refresh_point_statistics(self.app)
#                 except Exception:
#                     pass
            
#             # ============================================================================
#             # TRACK GRID (additional for grid system)
#             # ============================================================================
#             grid_indices = np.arange(total_points)
#             self.loaded_grids[owner_label] = grid_indices
#             if owner_label != grid_name:
#                 self.grid_aliases[grid_name] = owner_label
            
#             if not hasattr(self.app, 'original_file_paths'):
#                 self.app.original_file_paths = {}
#             self.app.original_file_paths[owner_label] = str(las_file)
            
#             if owner_label != grid_name:
#                 print(f"   📍 Tracked grid: {owner_label} (requested: {grid_name})")
#             else:
#                 print(f"   📍 Tracked grid: {owner_label}")
            
#             # ============================================================================
#             # COMPLETE
#             # ============================================================================
#             total_time = time.time() - load_start
            
#             print(f"\n{'='*60}")
#             print(f"✅ GRID LOAD COMPLETE - SAME AS MENU BAR")
#             print(f"   Grid: {owner_label}")
#             print(f"   Points: {total_points:,}")
#             print(f"   Time: {total_time:.1f}s")
#             print(f"{'='*60}\n")
            
#             # ✅ BULLETPROOF: Re-ensure all DXF/SNT overlay actors are in renderer
#             # Some code paths during load (build_unified_actor, apply_class_map, etc.)
#             # may have removed actors. This guarantees they're always visible.
#             if hasattr(self.app, '_ensure_overlay_actors'):
#                 self.app._ensure_overlay_actors()
            
#             progress.finish_success(f"Loaded {total_points:,} points in {total_time:.1f}s")
            
#             # Show success message
#             QMessageBox.information(
#                 self.app,
#                 "Grid Loaded",
#                 f"✅ Loaded: {owner_label}\n\n"
#                 f"File: {las_file.name}\n"
#                 f"Points: {total_points:,}"
#             )
            
#         except Exception as e:
#             print(f"❌ Load failed: {e}")
#             import traceback
#             traceback.print_exc()
#             progress.finish_error(f"Load failed: {e}")
#             QMessageBox.critical(self.app, "Load Error", f"Failed to load: {e}")

#     def _save_folder_to_settings(self, folder_path):
#         """Save LAZ/LAS folder to settings for future use"""
#         self.settings.setValue("last_las_folder", str(folder_path))
#         self.settings.sync()
#         print(f"💾 Saved LAZ folder: {folder_path}")
        
#     def clear_grid_data(self, grid_name):
#         """Delete points belonging to a specific grid from the loaded dataset"""
#         print(f"\n{'='*60}")
#         print(f"🗑️ DELETING GRID DATA: {grid_name}")
#         print(f"{'='*60}\n")
        
#         # Check if grid is tracked
#         if grid_name not in self.loaded_grids:
#             QMessageBox.warning(
#                 self.app,
#                 "Grid Not Found",
#                 f"Grid '{grid_name}' is not currently loaded.\n\n"
#                 f"Only grids loaded via grid label click can be deleted."
#             )
#             return
        
#         # Confirm deletion
#         points_to_delete = len(self.loaded_grids[grid_name])
#         reply = QMessageBox.question(
#             self.app,
#             "Confirm Clear",
#             f"Clear all points from grid:\n{grid_name}\n\n"
#             f"Points: {points_to_delete:,}\n\nContinue?",
#             QMessageBox.Yes | QMessageBox.No,
#             QMessageBox.No
#         )
        
#         if reply == QMessageBox.No:
#             return
        
#         try:
#             import numpy as np
#             from gui.pointcloud_display import update_pointcloud
#             from PySide6.QtCore import QCoreApplication
            
#             # Get total points before any operations
#             total_points = len(self.app.data['xyz'])
            
#             # ✅ FIX: Validate indices before using them
#             indices_to_delete = self.loaded_grids[grid_name]
#             indices_to_delete = self._validate_grid_indices(
#                 grid_name, indices_to_delete, total_points
#             )
            
#             if len(indices_to_delete) == 0:
#                 print(f"  ⚠️ No valid indices to delete")
#                 del self.loaded_grids[grid_name]
#                 QMessageBox.warning(
#                     self.app,
#                     "No Valid Points",
#                     f"Grid '{grid_name}' has no valid point indices.\n"
#                     f"The grid tracking has been cleared."
#                 )
#                 return
            
#             # ✅ OPTIMIZED: Use boolean mask (much faster than index manipulation)
#             print(f"  🔄 Creating deletion mask...")
#             QCoreApplication.processEvents()
            
#             keep_mask = np.ones(total_points, dtype=bool)
#             keep_mask[indices_to_delete] = False
            
#             remaining = np.sum(keep_mask)
#             print(f"  Remaining: {remaining:,}")
            
#             # ✅ OPTIMIZED: Filter all arrays in one pass
#             print(f"  🔄 Filtering point data...")
#             QCoreApplication.processEvents()
            
#             self.app.data['xyz'] = self.app.data['xyz'][keep_mask]
#             self.app.data['classification'] = self.app.data['classification'][keep_mask]
            
#             if 'rgb' in self.app.data and self.app.data['rgb'] is not None:
#                 self.app.data['rgb'] = self.app.data['rgb'][keep_mask]
            
#             if 'intensity' in self.app.data and self.app.data['intensity'] is not None:
#                 self.app.data['intensity'] = self.app.data['intensity'][keep_mask]
            
#             # ✅ OPTIMIZED: Vectorized index updating for remaining grids
#             print(f"  🔄 Updating grid tracking...")
#             QCoreApplication.processEvents()
            
#             if len(self.loaded_grids) > 1:
#                 # Create mapping: old_index -> new_index
#                 # This is MUCH faster than looping
#                 old_to_new = np.full(total_points, -1, dtype=np.int64)
#                 old_to_new[keep_mask] = np.arange(remaining)
                
#                 # Update all other grids in one vectorized operation
#                 for other_grid in list(self.loaded_grids.keys()):
#                     if other_grid == grid_name:
#                         continue
                    
#                     old_indices = self.loaded_grids[other_grid]
#                     new_indices = old_to_new[old_indices]
                    
#                     # Filter out any invalid indices (shouldn't happen, but safety check)
#                     valid = new_indices >= 0
#                     self.loaded_grids[other_grid] = new_indices[valid]
                    
#                     print(f"     Updated {other_grid}: {len(old_indices)} -> {len(new_indices[valid])} indices")
            
#             # Remove deleted grid
#             del self.loaded_grids[grid_name]
#             # Drop alias links pointing to deleted owner
#             if self.grid_aliases:
#                 stale_aliases = [k for k, v in self.grid_aliases.items() if v == grid_name]
#                 for alias in stale_aliases:
#                     try:
#                         del self.grid_aliases[alias]
#                     except Exception:
#                         pass
            
#             print(f"  🔄 Rebuilding spatial index...")
#             QCoreApplication.processEvents()
            
#             # Rebuild spatial index if it exists
#             if hasattr(self.app, 'spatial_index') and remaining > 50_000:
#                 try:
#                     from gui.performance_optimizations import SpatialIndex
#                     self.app.spatial_index = SpatialIndex(self.app.data["xyz"])
#                     print(f"  ✅ Spatial index rebuilt")
#                 except Exception as e:
#                     print(f"  ⚠️ Spatial index rebuild failed: {e}")
#                     self.app.spatial_index = None
            
#             # Update display
#             print(f"  🔄 Updating display...")
#             QCoreApplication.processEvents()
            
            
#             if remaining == 0:
#                 print(f"  ⚠️ No points remaining - clearing scene")

#                 # Surface mode can remain stale after last LAZ/grid is removed.
#                 # Clear Surface first so next SNT grid click loads data instead of opening Surface Settings.
#                 try:
#                     from gui.surface_mode import detach_surface_before_non_surface_mode
#                     detach_surface_before_non_surface_mode(self.app, requested_mode="grid_clear")
#                     print("  🧹 Surface state cleared after last grid removal")
#                 except Exception as e:
#                     print(f"  ⚠️ Surface cleanup after grid clear skipped: {e}")

#                 try:
#                     self.app.display_mode = "rgb"
#                     self.app.current_display_mode = "rgb"
#                     self.app._suspend_grid_clicks = False
#                     self.app._surface_mesh_actor = None
#                     self.app._surface_mesh_polydata = None
#                     self.app._surface_points = None
#                     self.app._surface_faces = None
#                     self.app._surface_global_to_unique = None
#                     self.app._surface_unique_global_indices = None
#                 except Exception:
#                     pass
                
#                 # Clear the renderer
#                 if hasattr(self.app, 'vtk_widget') and self.app.vtk_widget:
#                     self.app.vtk_widget.renderer.RemoveAllViewProps()
#                     self.app.vtk_widget.render()
                
#                 # Reset data
#                 self.app.data = None
#                 self.app.loaded_file = None
#                 self.app.last_save_path = None
                
#                 # Update title
#                 self.app._update_window_title("No Data", None)
                
#                 # Restore DXF if exists
#                 if hasattr(self.app, 'preserve_dxf_actors'):
#                     from PySide6.QtCore import QTimer
#                     QTimer.singleShot(100, self.app.preserve_dxf_actors)
                
#                 QMessageBox.information(
#                     self.app,
#                     "All Grids Cleared",
#                     f"✅ Cleared: {grid_name}\n\n"
#                     f"All points removed.\n"
#                     f"Load a new grid to continue."
#                 )
                
#                 print(f"✅ Scene cleared completely")
#                 return
            
#             update_pointcloud(self.app, self.app.display_mode)
            
#             # Restore DXF
#             if hasattr(self.app, 'preserve_dxf_actors'):
#                 from PySide6.QtCore import QTimer
#                 QTimer.singleShot(100, self.app.preserve_dxf_actors)
            
#             # Update UI
#             self.app._update_window_title(
#                 f"Multiple Grids ({remaining:,} pts)",
#                 self.app.project_crs_epsg
#             )
            
#             if hasattr(self.app, 'point_count_widget') and self.app.point_count_widget:
#                 from gui.point_count_widget import refresh_point_statistics
#                 refresh_point_statistics(self.app)
            
#             print(f"✅ Grid deleted successfully")
#             print(f"{'='*60}\n")
            
#             QMessageBox.information(
#                 self.app,
#                 "Grid Cleared",
#                 f"✅ Cleared: {grid_name}\n\n"
#                 f"Removed: {points_to_delete:,} points\n"
#                 f"Remaining: {remaining:,} points"
#             )
#             self.app.last_save_path = None  # Clear save path so auto-save won't overwrite
#             print(f"  ℹ️ Cleared save path - original file will not be overwritten")
#         except Exception as e:
#             print(f"❌ Delete failed: {e}")
#             import traceback
#             traceback.print_exc()
            
#             QMessageBox.critical(
#                 self.app,
#                 "Delete Error",
#                 f"Failed to delete grid '{grid_name}':\n\n{str(e)}"
#             )
            
            
#     def setup_interactor(self):
#         """Attach observers to main VTK widget"""
#         if not hasattr(self.app, 'vtk_widget'):
#             print("⚠️ No VTK widget found")
#             return
        
#         interactor = self.app.vtk_widget.interactor
        
#         # ✨ NEW: Add keyboard shortcut for rectangle selection
        
#         print("✅ Grid label system enabled (Press 'D' to delete grid by area)")

#     def setup_interactor(self):
#         """Attach observers to main VTK widget."""
#         if not hasattr(self.app, 'vtk_widget'):
#             print("No VTK widget found")
#             return

#         self.ensure_interactor_observers()
#         print("Grid label system enabled (Press 'D' to delete grid by area)")

#     def on_key_press(self, obj, event):
#         """Handle keyboard shortcuts"""
#         handles = self._get_main_interactor_handles()
#         if handles is None:
#             return 0
#         _, interactor, _ = handles
#         key = interactor.GetKeySym()
        
#         if key == 'd' or key == 'D':
#             self._start_rectangle_delete_mode()

#     def _start_rectangle_delete_mode(self):
#         """Start interactive rectangle selection for deletion"""
#         from PySide6.QtWidgets import QMessageBox
        
#         QMessageBox.information(
#             self.app,
#             "Rectangle Delete Mode",
#             "📦 RECTANGLE DELETE MODE\n\n"
#             "1. Click and drag to draw a rectangle\n"
#             "2. Release to select the grid area\n"
#             "3. Confirm deletion\n\n"
#             "Press ESC to cancel"
#         )
        
#         # Use VTK's rubber band picker
#         style = vtk.vtkInteractorStyleRubberBandPick()
#         self.app.vtk_widget.interactor.SetInteractorStyle(style)
        
#         # Add observer for selection complete
#         style.AddObserver("EndPickEvent", self._on_rectangle_selected)
        
#         self._temp_style = style  # Store to restore later

#     def _on_rectangle_selected(self, obj, event):
#         """Handle rectangle selection complete"""
#         import numpy as np
#         from PySide6.QtWidgets import QInputDialog
        
#         # Get selected area
#         style = obj
#         x1, y1, x2, y2 = style.GetStartPosition() + style.GetEndPosition()
        
#         # Use area picker
#         area_picker = vtk.vtkAreaPicker()
#         area_picker.AreaPick(x1, y1, x2, y2, self.app.vtk_widget.renderer)
        
#         # Get frustum (selection volume)
#         frustum = area_picker.GetFrustum()
        
#         if not frustum or not hasattr(self.app, 'data'):
#             self._restore_normal_interaction()
#             return
        
#         # Find points inside selection
#         points_xyz = self.app.data['xyz']
        
#         # Convert to VTK points for frustum testing
#         selected_indices = []
        
#         for i, point in enumerate(points_xyz):
#             vtk_point = vtk.vtkPoints()
#             vtk_point.InsertNextPoint(point[0], point[1], point[2])
            
#             # Check if point is inside frustum
#             if frustum.EvaluateFunction(point[0], point[1], point[2]) < 0:
#                 selected_indices.append(i)
        
#         selected_indices = np.array(selected_indices)
        
#         if len(selected_indices) == 0:
#             QMessageBox.warning(self.app, "No Points", "No points selected in this area")
#             self._restore_normal_interaction()
#             return
        
#         # Ask for grid name or auto-detect
#         grid_name, ok = QInputDialog.getText(
#             self.app,
#             "Delete Grid",
#             f"Selected: {len(selected_indices):,} points\n\n"
#             f"Enter grid name to delete:"
#         )
        
#         if ok and grid_name:
#             # Track and delete
#             self.loaded_grids[grid_name] = selected_indices
#             self.delete_grid_data(grid_name)
        
#         self._restore_normal_interaction()

#     def _restore_normal_interaction(self):
#         """Restore normal camera interaction"""
#         if hasattr(self.app, 'ensure_main_view_2d_interaction') and not getattr(self.app, 'is_3d_mode', False):
#             self.app.ensure_main_view_2d_interaction(
#                 preserve_camera=True,
#                 reason="grid_label_restore",
#             )
#             return

#         style = vtk.vtkInteractorStyleTrackballCamera()
#         self.app.vtk_widget.interactor.SetInteractorStyle(style)

#     def _validate_grid_indices(self, grid_name, indices, current_data_size):
#         """
#         Validate that grid indices are within bounds of current data.
#         Returns cleaned indices array.
#         """
#         import numpy as np
        
#         if indices is None or len(indices) == 0:
#             return np.array([], dtype=np.int64)
        
#         # Check if any indices are out of bounds
#         max_index = np.max(indices)
#         if max_index >= current_data_size:
#             print(f"  ⚠️ WARNING: Grid '{grid_name}' has invalid indices")
#             print(f"     Max index: {max_index}, Data size: {current_data_size}")
#             print(f"     Filtering out-of-bounds indices...")
            
#             # Keep only valid indices
#             valid_mask = indices < current_data_size
#             valid_indices = indices[valid_mask]
            
#             invalid_count = len(indices) - len(valid_indices)
#             if invalid_count > 0:
#                 print(f"     Removed {invalid_count} invalid indices")
            
#             return valid_indices
        
#         return indices        
    
#     def verify_dxf_labels(self):
#         """Debug tool: Compare DXF label positions with actual LAZ file centers"""
#         import laspy
#         from pathlib import Path
#         from PySide6.QtWidgets import QMessageBox, QTextEdit, QVBoxLayout, QDialog, QPushButton
        
#         # Find LAZ folder automatically
#         las_folder = self._find_las_folder_from_dxf()
        
#         if not las_folder:
#             QMessageBox.warning(
#                 self.app,
#                 "No LAZ Folder",
#                 "Could not locate LAZ/LAS folder.\n\n"
#                 "Please load a DXF file first."
#             )
#             return
        
#         print(f"\n{'='*70}")
#         print(f"🔍 DXF LABEL VERIFICATION")
#         print(f"{'='*70}\n")
        
#         # Get all label positions from DXF
#         label_positions = {}
#         if hasattr(self.app, 'dxf_actors') and self.app.dxf_actors:
#             for dxf_data in self.app.dxf_actors:
#                 for actor in dxf_data.get('actors', []):
#                     if hasattr(actor, 'is_grid_label') and actor.is_grid_label:
#                         grid_name = getattr(actor, 'grid_name', '')
#                         if grid_name:
#                             pos = actor.GetPosition()
#                             label_positions[grid_name] = pos
        
#         if not label_positions:
#             QMessageBox.warning(
#                 self.app,
#                 "No Labels Found",
#                 "No grid labels found in DXF.\n\n"
#                 "Make sure you've loaded a DXF with grid labels."
#             )
#             return
        
#         # Build verification report
#         report_lines = []
#         misaligned_count = 0
#         total_count = 0
        
#         # Get all LAZ file centers
#         las_folder_path = Path(las_folder)
#         for las_file in sorted(las_folder_path.glob("*.laz")):
#             try:
#                 with laspy.open(las_file) as f:
#                     header = f.header
#                     center_x = (header.x_min + header.x_max) / 2
#                     center_y = (header.y_min + header.y_max) / 2
                    
#                     grid_name = las_file.stem
#                     total_count += 1
                    
#                     if grid_name in label_positions:
#                         label_pos = label_positions[grid_name]
#                         distance = np.sqrt(
#                             (center_x - label_pos[0])**2 + 
#                             (center_y - label_pos[1])**2
#                         )
                        
#                         # Consider misaligned if >500m away
#                         is_misaligned = distance > 500
#                         if is_misaligned:
#                             misaligned_count += 1
                        
#                         status = "❌ MISALIGNED" if is_misaligned else "✅ OK"
                        
#                         report_lines.append(f"{status} {grid_name}:")
#                         report_lines.append(f"   Label Position: [{label_pos[0]:.1f}, {label_pos[1]:.1f}]")
#                         report_lines.append(f"   Data Center:    [{center_x:.1f}, {center_y:.1f}]")
#                         report_lines.append(f"   Distance: {distance:.1f}m")
#                         report_lines.append("")
                        
#                         # Print to console too
#                         print(f"{status} {grid_name}:")
#                         print(f"   Label: [{label_pos[0]:.1f}, {label_pos[1]:.1f}]")
#                         print(f"   Data:  [{center_x:.1f}, {center_y:.1f}]")
#                         print(f"   Distance: {distance:.1f}m\n")
#                     else:
#                         report_lines.append(f"⚠️ {grid_name}: No label found in DXF")
#                         report_lines.append("")
#                         print(f"⚠️ {grid_name}: No label found in DXF\n")
                        
#             except Exception as e:
#                 report_lines.append(f"❌ {las_file.name}: Error reading file - {e}")
#                 report_lines.append("")
#                 print(f"❌ {las_file.name}: {e}\n")
        
#         print(f"{'='*70}\n")
        
#         # Create dialog to show results
#         dialog = QDialog(self.app)
#         dialog.setWindowTitle("DXF Label Verification")
#         dialog.resize(700, 500)
        
#         layout = QVBoxLayout()
        
#         # Summary
#         summary = QTextEdit()
#         summary.setReadOnly(True)
#         summary.setMaximumHeight(80)
        
#         summary_text = f"""📊 VERIFICATION SUMMARY
#     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#     Total LAZ files: {total_count}
#     Labels in DXF: {len(label_positions)}
#     Misaligned (>500m): {misaligned_count}
#     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#     """
#         summary.setPlainText(summary_text)
#         layout.addWidget(summary)
        
#         # Detailed report
#         report = QTextEdit()
#         report.setReadOnly(True)
#         report.setPlainText("\n".join(report_lines))
#         layout.addWidget(report)
        
#         # Close button
#         close_btn = QPushButton("Close")
#         close_btn.clicked.connect(dialog.accept)
#         layout.addWidget(close_btn)
        
#         dialog.setLayout(layout)
#         dialog.exec()

    
#     def list_all_text_in_dxf(self):
#         """Debug: List all text actors in the scene - FIXED VERSION"""
#         from PySide6.QtWidgets import QTextEdit, QDialog, QVBoxLayout, QPushButton
#         import vtk
        
#         text_actors = []
        
#         if hasattr(self.app, 'dxf_actors') and self.app.dxf_actors:
#             for dxf_data in self.app.dxf_actors:
#                 for actor in dxf_data.get('actors', []):
#                     pos = actor.GetPosition()
                    
#                     # Try multiple methods to get text content
#                     text_content = "Unknown"
#                     is_text_actor = False
                    
#                     # Method 1: Check for custom properties we set
#                     if hasattr(actor, 'grid_name'):
#                         text_content = actor.grid_name
#                         is_text_actor = True
#                     elif hasattr(actor, 'text_content'):
#                         text_content = actor.text_content
#                         is_text_actor = True
                    
#                     # Method 2: Check if it's a vtkTextActor3D
#                     elif isinstance(actor, vtk.vtkTextActor3D):
#                         text_content = actor.GetInput()
#                         is_text_actor = True
                    
#                     # Method 3: Check if it's a vtkFollower with vector text source
#                     elif isinstance(actor, (vtk.vtkFollower, vtk.vtkActor)):
#                         mapper = actor.GetMapper()
#                         if mapper:
#                             input_data = mapper.GetInput()
                            
#                             # Check if it's from a vtkVectorText source
#                             if input_data and hasattr(mapper, 'GetInputAlgorithm'):
#                                 algo = mapper.GetInputAlgorithm()
#                                 if isinstance(algo, vtk.vtkVectorText):
#                                     text_content = algo.GetText()
#                                     is_text_actor = True
                            
#                             # Alternative: Check if it's a very small polydata (likely text)
#                             elif input_data and input_data.GetNumberOfPoints() < 1000:
#                                 # Small polydata might be text
#                                 # Check point data for clues
#                                 if hasattr(input_data, 'GetPointData'):
#                                     point_data = input_data.GetPointData()
#                                     if point_data.GetNumberOfArrays() > 0:
#                                         is_text_actor = True
#                                         # Still unknown text but we know it's text-like
                    
#                     # Method 4: Check property metadata
#                     if not is_text_actor and hasattr(actor, 'GetProperty'):
#                         prop = actor.GetProperty()
#                         # Text actors often have specific rendering properties
#                         if prop and prop.GetRepresentation() == vtk.VTK_SURFACE:
#                             mapper = actor.GetMapper()
#                             if mapper and mapper.GetInput():
#                                 num_points = mapper.GetInput().GetNumberOfPoints()
#                                 # Text typically has moderate point counts
#                                 if 10 < num_points < 2000:
#                                     is_text_actor = True
                    
#                     # Only add if we detected it as text
#                     if is_text_actor:
#                         is_label = hasattr(actor, 'is_grid_label') and actor.is_grid_label
                        
#                         text_actors.append({
#                             'text': text_content,
#                             'position': pos,
#                             'is_grid_label': is_label,
#                             'actor_type': type(actor).__name__
#                         })
        
#         # Show dialog
#         dialog = QDialog(self.app)
#         dialog.setWindowTitle("DXF Text Analysis")
#         dialog.resize(700, 500)
        
#         layout = QVBoxLayout()
        
#         report = QTextEdit()
#         report.setReadOnly(True)
        
#         lines = [f"Total text actors found: {len(text_actors)}\n"]
#         lines.append("=" * 70)
#         lines.append("")
        
#         grid_labels = [t for t in text_actors if t['is_grid_label']]
#         lines.append(f"Grid labels (is_grid_label=True): {len(grid_labels)}")
#         lines.append(f"Other text: {len(text_actors) - len(grid_labels)}")
#         lines.append("")
#         lines.append("=" * 70)
#         lines.append("")
        
#         for i, actor_info in enumerate(text_actors[:50], 1):  # Show first 50
#             status = "✅" if actor_info['is_grid_label'] else "❌"
#             pos = actor_info['position']
#             lines.append(f"{status} {i}. {actor_info['text']}")
#             lines.append(f"   Position: [{pos[0]:.1f}, {pos[1]:.1f}, {pos[2]:.1f}]")
#             lines.append(f"   Type: {actor_info['actor_type']}")
#             lines.append("")
        
#         if len(text_actors) > 50:
#             lines.append(f"... and {len(text_actors) - 50} more")
        
#         report.setPlainText("\n".join(lines))
#         layout.addWidget(report)
        
#         close_btn = QPushButton("Close")
#         close_btn.clicked.connect(dialog.accept)
#         layout.addWidget(close_btn)
        
#         dialog.setLayout(layout)
#         dialog.exec()

#     def _find_grid_label_actor(self, grid_name):
#         """Resolve the live vtk actor for a grid_name string.

#         CurveTool.eventFilter (gui/curve_tools.py) only passes the grid_name
#         string through to show_grid_label_menu, not the picked actor, so we
#         re-locate the actor here the same way _check_grid_label_at_click's
#         Method 3 fallback does: scan the known SNT/DXF actor stores for a
#         pickable grid-label actor whose grid_name matches.
#         """
#         if not grid_name:
#             return None
#         try:
#             for store_name in ('snt_actors', 'dxf_actors'):
#                 for data in getattr(self.app, store_name, []) or []:
#                     for actor in data.get('actors', []) or []:
#                         if not (hasattr(actor, 'is_grid_label') and actor.is_grid_label):
#                             continue
#                         if getattr(actor, 'grid_name', None) == grid_name:
#                             return actor
#         except Exception as e:
#             print(f"⚠️ Could not resolve grid label actor for '{grid_name}': {e}")
#         return None

#     def show_grid_label_menu(self, grid_name, snt_filename=None):
#         """
#         Show context menu for a grid label.
#         Called by CurveTool.eventFilter when it detects a right-click on a grid label.
#         Extracted from on_right_click() so it can be called externally.
#         """
#         from PySide6.QtWidgets import QMenu, QMessageBox
#         from PySide6.QtGui import QAction, QCursor

#         if not grid_name:
#             return

#         menu = QMenu(self.app)
#         menu.setStyleSheet("""
#             QMenu {
#                 background-color: #2c2c2c;
#                 color: #f0f0f0;
#                 border: 1px solid #555;
#                 padding: 5px;
#             }
#             QMenu::item {
#                 padding: 8px 30px;
#                 border-radius: 3px;
#             }
#             QMenu::item:selected {
#                 background-color: #3c3c3c;
#             }
#         """)

#         load_action = QAction("📂 Load Grid Data", self.app)
#         clear_action = QAction("🧹 Clear Grid Data", self.app)

#         if not snt_filename:
#             snt_filename = self._infer_snt_filename_for_grid(grid_name)

#         owner_label = self._resolve_grid_owner(grid_name)
#         is_loaded = grid_name in self.loaded_grids

#         if is_loaded:
#             load_action.setText("📂 Reload Grid Data")
#             point_count = len(self.loaded_grids[grid_name])
#             clear_action.setText(f"🧹 Clear Grid ({point_count:,} pts)")
#         else:
#             clear_action.setEnabled(False)
#             clear_action.setText("🧹 (Grid not loaded)")

#         load_action.triggered.connect(
#             lambda _=False, gn=grid_name, sf=snt_filename: self.load_grid_las(
#                 gn,
#                 snt_filename=sf,
#                 alt_names=self._get_alt_names_for_grid(gn),
#             )
#         )

#         def confirm_clear():
#             reply = QMessageBox.question(
#                 self.app,
#                 "Confirm Clear",
#                 f"Clear all points from grid:\n\n{grid_name}\n\n"
#                 f"Points will be removed from view.\n\nContinue?",
#                 QMessageBox.Yes | QMessageBox.No,
#                 QMessageBox.No
#             )
#             if reply == QMessageBox.Yes:
#                 self.clear_grid_data(grid_name)

#         clear_action.triggered.connect(confirm_clear)
#         fence_action = QAction("🔺 Load Points Inside Fence", self.app)
#         fence_action.triggered.connect(self.activate_load_by_fence_tool)
#         buffer_action = QAction("🧩 Open Block with Buffer Points", self.app)
#         buffer_action.triggered.connect(
#             lambda _=False, gn=grid_name, sf=snt_filename:
#                 self.open_grid_with_buffer_points(
#                     gn, sf, self._get_alt_names_for_grid(gn)
#                 )
#         )

#         menu.addAction(load_action)
#         menu.addAction(buffer_action)
#         menu.addAction(clear_action)
#         menu.addAction(fence_action)

#         edit_actor = self._find_grid_label_actor(grid_name)
#         if edit_actor is not None:
#             from PySide6.QtCore import QTimer
#             edit_label_action = QAction("✏️ Edit Label Text", self.app)
#             # Defer the dialog by one event-loop tick (mirrors
#             # digitize_tools._show_text_context_menu's _deferred_menu/QTimer
#             # pattern). Opening a modal QDialog synchronously inside this
#             # menu-action's triggered slot — itself already nested inside
#             # the CurveTool eventFilter's synchronous mouse-press handling —
#             # leaves VTK's render-window mouse grab from the original click
#             # unreleased, so the dialog paints but never receives mouse
#             # input. A short QTimer defer lets that grab release first.
#             edit_label_action.triggered.connect(
#                 lambda _=False, a=edit_actor: QTimer.singleShot(
#                     10, lambda a=a: self._edit_grid_label(a)
#                 )
#             )
#             menu.addSeparator()
#             menu.addAction(edit_label_action)

#             bulk_font_action = QAction("🔠 Set Font Size — All Block Labels", self.app)
#             bulk_font_action.triggered.connect(
#                 lambda _=False: QTimer.singleShot(10, self.edit_all_grid_labels_font_size)
#             )
#             menu.addAction(bulk_font_action)

#             if getattr(self, '_label_edit_undo_stack', None):
#                 undo_label_action = QAction("↶ Undo Last Label Edit", self.app)
#                 undo_label_action.triggered.connect(
#                     lambda _=False: QTimer.singleShot(10, self._undo_last_label_edit)
#                 )
#                 menu.addAction(undo_label_action)

#         menu.exec(QCursor.pos())

# def add_grid_label_system_to_app(app):
#         """Initialize grid label manager"""
#         if not hasattr(app, 'grid_label_manager'):
#             app.grid_label_manager = GridLabelManager(app)
#             app.grid_label_manager.setup_interactor()
#             print("✅ Grid label system activated")
            


import os
import re
import time
import traceback
import uuid
from typing import Dict, List, Optional, Tuple
from pathlib import Path
import vtk
from PySide6.QtWidgets import QMessageBox, QFileDialog, QHBoxLayout, QProgressDialog
from PySide6.QtCore import QSettings, QThread, Signal, Qt
import numpy as np

from gui.lidar_file_matcher import (
    find_matching_lidar_file,
    names_share_numeric_identity,
    normalized_stem,
    numeric_identity,
    strip_lidar_extension,
)


def _normalize_ring_xy(points) -> List[Tuple[float, float]]:
    ring: List[Tuple[float, float]] = []
    if points is None:
        return ring

    for pt in points:
        if pt is None:
            continue
        if len(pt) < 2:
            continue
        ring.append((float(pt[0]), float(pt[1])))

    if len(ring) >= 2:
        x0, y0 = ring[0]
        x1, y1 = ring[-1]
        if abs(x0 - x1) <= 1e-9 and abs(y0 - y1) <= 1e-9:
            ring = ring[:-1]

    dedup: List[Tuple[float, float]] = []
    for pt in ring:
        if not dedup:
            dedup.append(pt)
            continue
        if abs(pt[0] - dedup[-1][0]) <= 1e-12 and abs(pt[1] - dedup[-1][1]) <= 1e-12:
            continue
        dedup.append(pt)

    return dedup


def _polygon_bounds_xy(points_xy: List[Tuple[float, float]]) -> Tuple[float, float, float, float]:
    xs = [p[0] for p in points_xy]
    ys = [p[1] for p in points_xy]
    return (min(xs), min(ys), max(xs), max(ys))


def _bbox_intersects(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float], eps: float = 1e-9) -> bool:
    return not (
        a[2] < b[0] - eps or
        a[0] > b[2] + eps or
        a[3] < b[1] - eps or
        a[1] > b[3] + eps
    )


def _point_in_polygon_xy(px: float, py: float, polygon_xy: List[Tuple[float, float]], eps: float = 1e-9) -> bool:
    n = len(polygon_xy)
    if n < 3:
        return False

    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon_xy[i]
        xj, yj = polygon_xy[j]

        dx = xj - xi
        dy = yj - yi
        seg_len2 = dx * dx + dy * dy
        if seg_len2 > 0.0:
            cross = (px - xi) * dy - (py - yi) * dx
            if abs(cross) <= eps * (abs(dx) + abs(dy) + 1.0):
                dot = (px - xi) * dx + (py - yi) * dy
                if -eps <= dot <= seg_len2 + eps:
                    return True

        if ((yi > py) != (yj > py)):
            safe_dy = dy if abs(dy) > 1e-20 else (1e-20 if dy >= 0 else -1e-20)
            x_hit = (dx * (py - yi) / safe_dy) + xi
            if px <= x_hit + eps:
                inside = not inside
        j = i
    return inside


def _segments_intersect_2d(a1, a2, b1, b2, eps: float = 1e-9) -> bool:
    def _orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    def _on_seg(p, q, r):
        return (
            min(p[0], r[0]) - eps <= q[0] <= max(p[0], r[0]) + eps and
            min(p[1], r[1]) - eps <= q[1] <= max(p[1], r[1]) + eps
        )

    o1 = _orient(a1, a2, b1)
    o2 = _orient(a1, a2, b2)
    o3 = _orient(b1, b2, a1)
    o4 = _orient(b1, b2, a2)

    if (o1 > eps and o2 < -eps) or (o1 < -eps and o2 > eps):
        if (o3 > eps and o4 < -eps) or (o3 < -eps and o4 > eps):
            return True

    if abs(o1) <= eps and _on_seg(a1, b1, a2):
        return True
    if abs(o2) <= eps and _on_seg(a1, b2, a2):
        return True
    if abs(o3) <= eps and _on_seg(b1, a1, b2):
        return True
    if abs(o4) <= eps and _on_seg(b1, a2, b2):
        return True
    return False


def _polygons_intersect_xy(poly_a: List[Tuple[float, float]], poly_b: List[Tuple[float, float]]) -> bool:
    if len(poly_a) < 3 or len(poly_b) < 3:
        return False

    if _point_in_polygon_xy(poly_a[0][0], poly_a[0][1], poly_b):
        return True
    if _point_in_polygon_xy(poly_b[0][0], poly_b[0][1], poly_a):
        return True

    for i in range(len(poly_a)):
        a1 = poly_a[i]
        a2 = poly_a[(i + 1) % len(poly_a)]
        for j in range(len(poly_b)):
            b1 = poly_b[j]
            b2 = poly_b[(j + 1) % len(poly_b)]
            if _segments_intersect_2d(a1, a2, b1, b2):
                return True
    return False


def _points_inside_polygon_mask(x: np.ndarray, y: np.ndarray, polygon_xy: List[Tuple[float, float]], eps: float = 1e-9) -> np.ndarray:
    n_pts = len(x)
    if n_pts == 0 or len(polygon_xy) < 3:
        return np.zeros(n_pts, dtype=bool)

    inside = np.zeros(n_pts, dtype=bool)
    on_edge = np.zeros(n_pts, dtype=bool)

    n = len(polygon_xy)
    j = n - 1
    for i in range(n):
        xi, yi = polygon_xy[i]
        xj, yj = polygon_xy[j]
        dx = xj - xi
        dy = yj - yi
        seg_len2 = dx * dx + dy * dy

        if seg_len2 > 0.0:
            cross = (x - xi) * dy - (y - yi) * dx
            line_tol = eps * (abs(dx) + abs(dy) + 1.0)
            on_line = np.abs(cross) <= line_tol
            dot = (x - xi) * dx + (y - yi) * dy
            on_seg = on_line & (dot >= -eps) & (dot <= seg_len2 + eps)
            on_edge |= on_seg

        if abs(dy) <= 1e-20:
            j = i
            continue

        crosses = ((yi > y) != (yj > y))
        if np.any(crosses):
            x_hit = dx * (y[crosses] - yi) / dy + xi
            toggle = x[crosses] <= (x_hit + eps)
            idx = np.flatnonzero(crosses)
            inside[idx] ^= toggle
        j = i

    return inside | on_edge


def _buffer_polygon_xy(points_xy, width: float) -> List[Tuple[float, float]]:
    """Return a robust outward buffer ring for one SNT grid polygon."""
    ring = _normalize_ring_xy(points_xy)
    width = float(width)
    if len(ring) < 3 or not np.isfinite(width) or width <= 0.0:
        return []

    from shapely.geometry import Polygon

    polygon = Polygon(ring)
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    if polygon.is_empty:
        return []

    buffered = polygon.buffer(width, join_style="mitre")
    if buffered.is_empty:
        return []
    if buffered.geom_type == "MultiPolygon":
        buffered = max(buffered.geoms, key=lambda geom: geom.area)
    return _normalize_ring_xy(list(buffered.exterior.coords))


class SNTFenceLoadWorker(QThread):
    progress = Signal(int, str)
    completed = Signal(dict)
    failed = Signal(str)
    cancelled = Signal(str)

    def __init__(
        self,
        block_jobs: List[Dict[str, object]],
        fence_polygon_xy: List[Tuple[float, float]],
        fence_bbox: Tuple[float, float, float, float],
        chunk_size: int = 1_000_000,
        dedupe_boundary_points: bool = True,
    ):
        super().__init__()
        self.block_jobs = list(block_jobs or [])
        self.fence_polygon_xy = list(fence_polygon_xy or [])
        self.fence_bbox = fence_bbox
        self.chunk_size = max(10_000, int(chunk_size or 1_000_000))
        self.dedupe_boundary_points = bool(dedupe_boundary_points)
        self._cancelled = False
        self.result_data: Optional[Dict[str, np.ndarray]] = None

    def cancel(self):
        self._cancelled = True

    @staticmethod
    def _has_naksha_user_data_marker(header) -> bool:
        try:
            vlrs = []
            if hasattr(header, "vlrs") and header.vlrs:
                vlrs.extend(list(header.vlrs))
            return any(
                (
                    getattr(v, "user_id", "").strip() == "NakshaAI"
                    and int(getattr(v, "record_id", -1)) == 1001
                )
                for v in vlrs
            )
        except Exception:
            return False

    @staticmethod
    def _chunk_classification_array(chunk, *, prefer_user_data: bool, las_version: tuple[int, int]) -> np.ndarray:
        if prefer_user_data and hasattr(chunk, "user_data"):
            try:
                return np.asarray(chunk.user_data, dtype=np.uint8)
            except Exception:
                pass

        major, minor = las_version
        if major == 1 and minor >= 4:
            if hasattr(chunk, "classification"):
                return np.asarray(chunk.classification, dtype=np.uint8)
            if hasattr(chunk, "raw_classification"):
                return np.asarray(chunk.raw_classification, dtype=np.uint8)
            return np.zeros(len(chunk.x), dtype=np.uint8)

        if hasattr(chunk, "raw_classification"):
            raw = np.asarray(chunk.raw_classification, dtype=np.uint8)
            if raw.size and int(raw.max()) > 31:
                return raw

        if hasattr(chunk, "classification"):
            return np.asarray(chunk.classification, dtype=np.uint8)
        if hasattr(chunk, "raw_classification"):
            return np.asarray(chunk.raw_classification, dtype=np.uint8)
        return np.zeros(len(chunk.x), dtype=np.uint8)

    def run(self):
        try:
            import laspy

            started = time.time()
            minx, miny, maxx, maxy = self.fence_bbox
            total_jobs = max(1, len(self.block_jobs))
            source_files: List[str] = []
            for job in self.block_jobs:
                job_path = str(job.get("file_path") or "")
                if job_path:
                    try:
                        job_path = str(Path(job_path).resolve())
                    except Exception:
                        job_path = os.path.abspath(job_path)
                source_files.append(job_path)

            xyz_parts: List[np.ndarray] = []
            cls_parts: List[np.ndarray] = []
            rgb_parts: List[np.ndarray] = []
            intensity_parts: List[np.ndarray] = []
            ret_no_parts: List[np.ndarray] = []
            ret_cnt_parts: List[np.ndarray] = []
            source_file_id_parts: List[np.ndarray] = []
            source_point_idx_parts: List[np.ndarray] = []

            has_rgb_any = False
            has_intensity_any = False
            has_return_no_any = False
            has_return_cnt_any = False

            block_logs: List[Dict[str, object]] = []
            total_loaded_points = 0
            total_inside_points = 0
            failed_blocks: List[str] = []

            self.progress.emit(10, "Loading selected blocks")

            for block_idx, job in enumerate(self.block_jobs, start=1):
                if self._cancelled:
                    self.cancelled.emit("Load-by-fence cancelled by user.")
                    return

                file_path = str(job.get("file_path") or "")
                grid_name = str(job.get("grid_name") or Path(file_path).stem or f"Block {block_idx}")
                block_id = str(job.get("block_id") or grid_name)
                block_centroid = job.get("block_centroid")
                source_file_id = int(block_idx - 1)

                pct = int(10 + (70.0 * (block_idx - 1) / total_jobs))
                self.progress.emit(pct, f"Loading selected blocks ({block_idx}/{total_jobs})")

                block_loaded = 0
                block_inside = 0
                block_shift = (0.0, 0.0)

                try:
                    with laspy.open(file_path) as reader:
                        dims = {str(d).lower() for d in reader.header.point_format.dimension_names}
                        has_cls = "classification" in dims
                        has_user_data = "user_data" in dims
                        has_intensity = "intensity" in dims
                        has_ret_no = "return_number" in dims
                        has_ret_cnt = "number_of_returns" in dims
                        has_rgb = {"red", "green", "blue"}.issubset(dims)
                        version = reader.header.version
                        las_version = (
                            tuple(map(int, str(version).split(".")))
                            if isinstance(version, str)
                            else (int(version.major), int(version.minor))
                        )
                        prefer_user_data = (
                            has_user_data and self._has_naksha_user_data_marker(reader.header)
                        )

                        if block_centroid and len(block_centroid) >= 2:
                            bx, by = float(block_centroid[0]), float(block_centroid[1])
                            hx = (float(reader.header.x_min) + float(reader.header.x_max)) * 0.5
                            hy = (float(reader.header.y_min) + float(reader.header.y_max)) * 0.5
                            dx = bx - hx
                            dy = by - hy
                            if np.hypot(dx, dy) > 500.0:
                                block_shift = (dx, dy)

                        for chunk in reader.chunk_iterator(self.chunk_size):
                            if self._cancelled:
                                self.cancelled.emit("Load-by-fence cancelled by user.")
                                return

                            x = np.asarray(chunk.x, dtype=np.float64)
                            y = np.asarray(chunk.y, dtype=np.float64)
                            z = np.asarray(chunk.z, dtype=np.float64)

                            if block_shift != (0.0, 0.0):
                                x = x + block_shift[0]
                                y = y + block_shift[1]

                            n_chunk = len(x)
                            if n_chunk == 0:
                                continue

                            chunk_start = block_loaded
                            block_loaded += n_chunk
                            total_loaded_points += n_chunk

                            bbox_mask = (
                                (x >= minx) & (x <= maxx) &
                                (y >= miny) & (y <= maxy)
                            )
                            if not np.any(bbox_mask):
                                continue

                            bbox_idx = np.flatnonzero(bbox_mask)
                            inside_mask = _points_inside_polygon_mask(
                                x[bbox_idx], y[bbox_idx], self.fence_polygon_xy
                            )
                            if not np.any(inside_mask):
                                continue

                            selected_idx = bbox_idx[inside_mask]
                            n_sel = int(selected_idx.size)
                            block_inside += n_sel
                            total_inside_points += n_sel

                            xyz_parts.append(
                                np.column_stack(
                                    (x[selected_idx], y[selected_idx], z[selected_idx])
                                ).astype(np.float64, copy=False)
                            )

                            if has_cls or prefer_user_data:
                                cls_full = self._chunk_classification_array(
                                    chunk,
                                    prefer_user_data=prefer_user_data,
                                    las_version=las_version,
                                )
                                cls_arr = cls_full[selected_idx]
                            else:
                                cls_arr = np.zeros(n_sel, dtype=np.uint8)
                            cls_parts.append(cls_arr)

                            if has_intensity:
                                intensity_arr = np.asarray(chunk.intensity, dtype=np.float32)[selected_idx]
                                has_intensity_any = True
                            else:
                                intensity_arr = np.zeros(n_sel, dtype=np.float32)
                            intensity_parts.append(intensity_arr)

                            if has_ret_no:
                                ret_no_arr = np.asarray(chunk.return_number, dtype=np.uint8)[selected_idx]
                                has_return_no_any = True
                            else:
                                ret_no_arr = np.zeros(n_sel, dtype=np.uint8)
                            ret_no_parts.append(ret_no_arr)

                            if has_ret_cnt:
                                ret_cnt_arr = np.asarray(chunk.number_of_returns, dtype=np.uint8)[selected_idx]
                                has_return_cnt_any = True
                            else:
                                ret_cnt_arr = np.zeros(n_sel, dtype=np.uint8)
                            ret_cnt_parts.append(ret_cnt_arr)

                            if has_rgb:
                                rr = np.asarray(chunk.red, dtype=np.uint32)[selected_idx]
                                gg = np.asarray(chunk.green, dtype=np.uint32)[selected_idx]
                                bb = np.asarray(chunk.blue, dtype=np.uint32)[selected_idx]
                                if rr.size > 0 and max(int(rr.max()), int(gg.max()), int(bb.max())) > 255:
                                    rgb_arr = np.column_stack(
                                        ((rr // 257).astype(np.uint8),
                                         (gg // 257).astype(np.uint8),
                                         (bb // 257).astype(np.uint8))
                                    )
                                else:
                                    rgb_arr = np.column_stack(
                                        (rr.astype(np.uint8), gg.astype(np.uint8), bb.astype(np.uint8))
                                    )
                                has_rgb_any = True
                            else:
                                rgb_arr = np.zeros((n_sel, 3), dtype=np.uint8)
                            rgb_parts.append(rgb_arr)
                            source_file_id_parts.append(np.full(n_sel, source_file_id, dtype=np.int32))
                            source_point_idx_parts.append(
                                selected_idx.astype(np.int64, copy=False) + np.int64(chunk_start)
                            )

                except Exception as block_exc:
                    failed_blocks.append(block_id)
                    block_logs.append({
                        "block_id": block_id,
                        "grid_name": grid_name,
                        "file_path": file_path,
                        "error": str(block_exc),
                        "loaded_points": int(block_loaded),
                        "inside_points": int(block_inside),
                    })
                    continue

                block_logs.append({
                    "block_id": block_id,
                    "grid_name": grid_name,
                    "file_path": file_path,
                    "loaded_points": int(block_loaded),
                    "inside_points": int(block_inside),
                    "shift_xy": (float(block_shift[0]), float(block_shift[1])),
                })

            self.progress.emit(88, "Filtering points")

            if not xyz_parts:
                self.result_data = None
                self.completed.emit({
                    "block_logs": block_logs,
                    "failed_blocks": failed_blocks,
                    "total_loaded_points": int(total_loaded_points),
                    "total_inside_points": 0,
                    "total_render_points": 0,
                    "worker_seconds": float(time.time() - started),
                })
                return

            xyz = np.concatenate(xyz_parts, axis=0)
            classification = np.concatenate(cls_parts, axis=0).astype(np.uint8, copy=False)
            intensity = np.concatenate(intensity_parts, axis=0).astype(np.float32, copy=False) if intensity_parts else None
            rgb = np.concatenate(rgb_parts, axis=0).astype(np.uint8, copy=False) if rgb_parts else None
            return_number = np.concatenate(ret_no_parts, axis=0).astype(np.uint8, copy=False) if ret_no_parts else None
            number_of_returns = np.concatenate(ret_cnt_parts, axis=0).astype(np.uint8, copy=False) if ret_cnt_parts else None
            source_file_ids = (
                np.concatenate(source_file_id_parts, axis=0).astype(np.int32, copy=False)
                if source_file_id_parts else np.zeros(xyz.shape[0], dtype=np.int32)
            )
            source_point_indices = (
                np.concatenate(source_point_idx_parts, axis=0).astype(np.int64, copy=False)
                if source_point_idx_parts else np.zeros(xyz.shape[0], dtype=np.int64)
            )
            duplicate_owner_indices = np.empty(0, dtype=np.int64)
            duplicate_source_file_ids = np.empty(0, dtype=np.int32)
            duplicate_source_point_indices = np.empty(0, dtype=np.int64)

            if self.dedupe_boundary_points and xyz.shape[0] > 0 and len(self.block_jobs) > 1:
                try:
                    xyz_key = np.ascontiguousarray(xyz).view(
                        np.dtype([("x", np.float64), ("y", np.float64), ("z", np.float64)])
                    ).reshape(-1)
                    _, uniq_idx, inverse = np.unique(xyz_key, return_index=True, return_inverse=True)
                    if uniq_idx.size != xyz.shape[0]:
                        uniq_idx = np.sort(uniq_idx)
                        keep_mask = np.zeros(xyz.shape[0], dtype=bool)
                        keep_mask[uniq_idx] = True
                        duplicate_idx = np.flatnonzero(~keep_mask)

                        if duplicate_idx.size > 0:
                            group_to_new_idx = np.empty(int(uniq_idx.size), dtype=np.int64)
                            group_to_new_idx[inverse[uniq_idx]] = np.arange(uniq_idx.size, dtype=np.int64)
                            duplicate_owner_indices = group_to_new_idx[inverse[duplicate_idx]]
                            duplicate_source_file_ids = source_file_ids[duplicate_idx].astype(np.int32, copy=False)
                            duplicate_source_point_indices = source_point_indices[duplicate_idx].astype(np.int64, copy=False)

                        xyz = xyz[uniq_idx]
                        classification = classification[uniq_idx]
                        source_file_ids = source_file_ids[uniq_idx]
                        source_point_indices = source_point_indices[uniq_idx]
                        if intensity is not None:
                            intensity = intensity[uniq_idx]
                        if rgb is not None:
                            rgb = rgb[uniq_idx]
                        if return_number is not None:
                            return_number = return_number[uniq_idx]
                        if number_of_returns is not None:
                            number_of_returns = number_of_returns[uniq_idx]
                except Exception:
                    pass

            self.result_data = {
                "xyz": xyz,
                "classification": classification,
            }
            if has_rgb_any and rgb is not None:
                self.result_data["rgb"] = rgb
            if has_intensity_any and intensity is not None:
                self.result_data["intensity"] = intensity
            if has_return_no_any and return_number is not None:
                self.result_data["return_number"] = return_number
            if has_return_cnt_any and number_of_returns is not None:
                self.result_data["number_of_returns"] = number_of_returns
            self.result_data["_fence_source_files"] = source_files
            self.result_data["_fence_source_file_ids"] = source_file_ids
            self.result_data["_fence_source_point_indices"] = source_point_indices
            self.result_data["_fence_duplicate_owner_indices"] = duplicate_owner_indices
            self.result_data["_fence_duplicate_source_file_ids"] = duplicate_source_file_ids
            self.result_data["_fence_duplicate_source_point_indices"] = duplicate_source_point_indices

            self.progress.emit(98, "Rendering selected area")
            self.completed.emit({
                "block_logs": block_logs,
                "failed_blocks": failed_blocks,
                "total_loaded_points": int(total_loaded_points),
                "total_inside_points": int(total_inside_points),
                "total_render_points": int(xyz.shape[0]),
                "worker_seconds": float(time.time() - started),
            })

        except Exception as exc:
            self.failed.emit(str(exc))

class GridLabelManager:
    """
    Manages grid label clicking and automatic LAZ/LAS loading.
    Implements hyperlink-style behavior for DXF grid labels.
    """
    
    def __init__(self, app):
        self.app = app
        self.settings = QSettings("NakshaAI", "LidarApp")
        
        # Cache of DXF folder -> LAZ/LAS folder mappings
        self.folder_cache = {}
        self.loaded_grids = {}
        # clicked label -> actual loaded label (file stem)
        self.grid_aliases = {}
        # ✨ NEW: Track highlighted labels
        self.highlighted_label = None
        self.original_colors = {}  # Store original colors for restoration
        # Local undo history for grid/block-name label text & font-size
        # edits (see _edit_grid_label / _edit_all_grid_labels_font_size).
        # Deliberately separate from the digitizer's own undo_stack, which
        # only knows how to snapshot digitizer "drawings" — grid labels are
        # a different kind of object, so we keep a small dedicated stack
        # here instead of risking a crash inside unrelated undo code.
        self._label_edit_undo_stack = []
        self._label_edit_undo_limit = 20
        self._interactor_observer_ids = {}
        self._fence_mode_active = False
        self._fence_polyline_style_backup = None
        self._fence_worker = None
        self._fence_progress = None
        self._fence_last_stats = None
        self._fence_last_data_count = 0
        self._fence_operation_label = "Load by Fence"
        self._fence_chunk_size = max(100_000, int(os.getenv("NAKSHA_SNT_FENCE_CHUNK_SIZE", "1000000")))
        
        print("✅ Grid Label Manager initialized")
    
    def setup_interactor(self):
        """Attach right-click observer to main VTK widget"""
        if not hasattr(self.app, 'vtk_widget'):
            print("⚠️ No VTK widget found")
            return
        
        self.ensure_interactor_observers()
        
        # ✨ NEW: Add hover detection for visual feedback
        
        print("✅ Grid label click detection enabled")
            
    def ensure_interactor_observers(self):
        """Re-install managed observers after another tool clears interactor callbacks."""
        if getattr(self.app, "_shutdown_in_progress", False):
            return
        
        # ✅ FIX: Don't reinstall if element selection is active
        if getattr(self, "_element_select_active", False):
            print("⚠️ Grid label observers NOT reinstalled (element selection active)")
            return

        handles = self._get_main_interactor_handles()
        if handles is None:
            return

        _, interactor, _ = handles
        managed = {
            "RightButtonPressEvent": self.on_right_click,
            "MouseMoveEvent": self.on_mouse_move,
        }
        if hasattr(self, 'on_key_press'):
            managed["KeyPressEvent"] = self.on_key_press

        for event_name, callback in managed.items():
            old_id = self._interactor_observer_ids.get(event_name)
            if old_id is not None:
                try:
                    interactor.RemoveObserver(old_id)
                except Exception:
                    pass

            priority = 2.0 if event_name == "RightButtonPressEvent" else 0.0
            self._interactor_observer_ids[event_name] = interactor.AddObserver(
                event_name, callback, priority
            )

    def remove_interactor_observers(self):
        """Best-effort detach of managed interactor observers."""
        handles = self._get_main_interactor_handles()
        interactor = handles[1] if handles is not None else None
        for event_name, obs_id in list(self._interactor_observer_ids.items()):
            if obs_id is None or interactor is None:
                continue
            try:
                interactor.RemoveObserver(obs_id)
            except Exception:
                pass
        self._interactor_observer_ids.clear()

    def _get_main_interactor_handles(self):
        """
        Return `(vtk_widget, interactor, renderer)` when all handles are valid.
        Returns `None` during teardown or before widget initialization.
        """
        if getattr(self.app, "_shutdown_in_progress", False):
            return None
        vtk_widget = getattr(self.app, "vtk_widget", None)
        if vtk_widget is None:
            return None
        interactor = getattr(vtk_widget, "interactor", None)
        renderer = getattr(vtk_widget, "renderer", None)
        if interactor is None or renderer is None:
            return None
        return vtk_widget, interactor, renderer

    def _consume_vtk_event(self, obj):
        if obj is None:
            return

        try:
            if hasattr(obj, 'AbortFlagOn'):
                obj.AbortFlagOn()
            elif hasattr(obj, 'SetAbortFlag'):
                try:
                    obj.SetAbortFlag(1)
                except TypeError:
                    obj.SetAbortFlag(True)
        except Exception:
            pass

    def on_mouse_move(self, obj, event):
        """Highlight labels on hover"""
        handles = self._get_main_interactor_handles()
        if handles is None:
            return 0

        # ✅ While a camera interaction (pan/zoom/rotate) is active, skip the
        # hover area-pick — it runs on every mouse-move tick and its extra
        # update() during a drag makes pan/zoom feel sluggish. Hover highlight
        # resumes once the gesture ends.
        mgr = getattr(self.app, "gpu_render_manager", None)
        if mgr is not None and (
            getattr(mgr, "_interaction_active", False)
            or getattr(mgr, "_pan_in_progress", False)
        ):
            return 0

        vtk_widget, interactor, renderer = handles
        clickPos = interactor.GetEventPosition()
        
        # Use area picker for better detection
        area_picker = vtk.vtkAreaPicker()
        x, y = clickPos
        area_picker.AreaPick(x-10, y-10, x+10, y+10, renderer)
        
        found_label = None
        for prop in area_picker.GetProp3Ds():
            if hasattr(prop, 'is_grid_label') and prop.is_grid_label:
                found_label = prop
                break
        
        # Update highlighting
        if found_label != self.highlighted_label:
            try:
                if self.highlighted_label:
                    self._unhighlight_label(self.highlighted_label)
                if found_label:
                    self._highlight_label(found_label)
                self.highlighted_label = found_label
                vtk_widget.update()
            except Exception:
                self.highlighted_label = None

    def _highlight_label(self, actor):
        """Apply highlight effect to label"""
        try:
            if actor is None or not hasattr(actor, "GetProperty"):
                return
            prop = actor.GetProperty()
            if prop is None:
                return
            if actor not in self.original_colors:
                self.original_colors[actor] = {
                    'color': prop.GetColor(),
                    'opacity': prop.GetOpacity()
                }

            # Apply bright highlight
            prop.SetColor(1.0, 1.0, 0.0)  # Bright yellow
            prop.SetOpacity(1.0)

            # Make it slightly bigger/bolder if possible
            if hasattr(actor, 'GetMapper'):
                mapper = actor.GetMapper()
                if mapper:
                    mapper.ScalarVisibilityOff()
        except Exception:
            self.original_colors.pop(actor, None)

    def _unhighlight_label(self, actor):
        """Remove highlight effect from label"""
        try:
            if actor not in self.original_colors or actor is None or not hasattr(actor, "GetProperty"):
                return
            prop = actor.GetProperty()
            if prop is None:
                self.original_colors.pop(actor, None)
                return
            orig = self.original_colors[actor]
            prop.SetColor(*orig['color'])
            prop.SetOpacity(orig['opacity'])
        except Exception:
            pass

    def _ensure_snt_block_index(self) -> List[Dict[str, object]]:
        """
        Ensure app.snt_block_polygons is populated when SNT attachments exist.
        Keeps existing behavior intact and only rebuilds lazily when index is empty.
        """
        block_polygons = getattr(self.app, "snt_block_polygons", None) or []
        if block_polygons:
            return block_polygons

        attachments = list(getattr(self.app, "snt_attachments", []) or [])
        if not attachments:
            # No SNT attachments — fall back to PRJ Block Identifier data so
            # that fence / buffer loading works with a .prj loaded on its own
            # (without any SNT attachment). Returned fresh (not cached into
            # app.snt_block_polygons) to avoid polluting the SNT block cache.
            prj_polygons = self._get_prj_block_polygons()
            if prj_polygons:
                print(
                    f"[LoadByFence] Populated {len(prj_polygons)} block "
                    f"polygon(s) from PRJ data"
                )
                return prj_polygons
            return []

        try:
            from gui.snt_attachment import build_snt_block_polygons
        except Exception as exc:
            print(f"[LoadByFence] Could not import block-index builder: {exc}")
            return []

        rebuilt = 0
        for idx, att in enumerate(attachments, start=1):
            if not isinstance(att, dict):
                continue
            try:
                entities = att.get("entities") or []
                if not entities:
                    parsed = att.get("parsed")
                    if isinstance(parsed, dict):
                        entities = parsed.get("entities") or []
                if not entities:
                    continue

                snt_name = (
                    str(att.get("full_path") or "").strip()
                    or str(att.get("filename") or "").strip()
                    or f"SNT_{idx:03d}"
                )
                build_snt_block_polygons(self.app, entities, snt_name, attachment=att)
                rebuilt += 1
            except Exception as exc:
                print(f"[LoadByFence] Block-index rebuild skipped for attachment #{idx}: {exc}")

        block_polygons = getattr(self.app, "snt_block_polygons", None) or []
        if block_polygons:
            print(
                f"[LoadByFence] Rebuilt SNT block index from {rebuilt} attachment(s): "
                f"{len(block_polygons)} block polygon(s)"
            )
        else:
            # Attachments existed but produced no polygons — try PRJ data.
            prj_polygons = self._get_prj_block_polygons()
            if prj_polygons:
                print(
                    f"[LoadByFence] Populated {len(prj_polygons)} block "
                    f"polygon(s) from PRJ data"
                )
                return prj_polygons

        return block_polygons

    def _get_prj_block_polygons(self) -> List[Dict[str, object]]:
        """
        Convert PRJ Block Identifier dialog block data into the
        snt_block_polygons format used for hit-testing and loading.

        This is the bridge that lets a block identified directly from a
        .prj file (without any SNT attachment) respond to the viewport
        right-click context menu, fence loading, and buffer loading.
        Returns [] when no PRJ block data is available.
        """
        try:
            prj_dialog = getattr(self.app, "block_identifier_dialog", None)
            if prj_dialog is None:
                return []
            prj_data = getattr(prj_dialog, "prj_data", None)
            if not prj_data:
                return []

            # Only the blocks the user has actually run "Identify Selected
            # Block" on are eligible for the viewport right-click menu — a
            # .prj can contain thousands of blocks tiling the whole project,
            # and without this restriction a right-click anywhere inside any
            # (un-highlighted, invisible) neighboring block's boundary would
            # also show the load-options menu, not just inside the block the
            # user actually identified/highlighted.
            identified_labels = getattr(prj_dialog, "_identified_prj_block_labels", None)
            if not identified_labels:
                return []

            # Cache the result. PRJ block data is immutable until the .prj is
            # reloaded, but rebuilding it on every viewport right-click ran
            # ~tens of thousands of stat() calls against the (often remote) PRJ
            # directory, so the right-click context menu took hundreds of ms to
            # appear. _ensure_snt_block_index() also calls this during PRJ load
            # (the "[LoadByFence] Populated ... from PRJ data" log), which warms
            # the cache before the first right-click.
            cache_key = (
                getattr(prj_dialog, "current_prj_path", None) or "",
                id(prj_data),
                frozenset(identified_labels),
            )
            cached = getattr(self, "_prj_block_polygons_cache", None)
            if cached is not None and getattr(self, "_prj_block_polygons_cache_key", None) == cache_key:
                return cached

            prj_path = getattr(prj_dialog, "current_prj_path", None) or ""
            prj_dir = None
            if prj_path and os.path.isfile(prj_path):
                prj_dir = os.path.dirname(os.path.abspath(prj_path))

            # Gather available LAZ/LAS basenames by RECURSIVELY scanning the PRJ
            # directory. The .laz/.las for these blocks live in subfolders of the
            # PRJ path (any subfolder name), not only laz/lazz/las, so a single
            # recursive traversal (cached) finds them all in ONE network pass
            # instead of tens of thousands of per-block stat calls. The result is
            # cached, so this runs only when the PRJ is (re)loaded.
            avail = {}
            if prj_dir:
                prj_dir_path = Path(prj_dir)
                try:
                    for ext in ("*.laz", "*.las"):
                        for fp in prj_dir_path.rglob(ext):
                            try:
                                avail[fp.stem.upper()] = str(fp)
                            except Exception:
                                pass
                except Exception as _scan_exc:
                    print(f"[prj-block] PRJ LAZ recursive scan failed: {_scan_exc}")

            result = []
            for block in prj_data:
                if not isinstance(block, dict):
                    continue
                label = (block.get("label") or "").strip()
                if not label or label not in identified_labels:
                    continue
                coords = block.get("boundary_coords") or []
                if len(coords) < 3:
                    continue

                points_2d = []
                ok = True
                for xy in coords:
                    try:
                        points_2d.append((float(xy[0]), float(xy[1])))
                    except Exception:
                        ok = False
                        break
                if not ok or len(points_2d) < 3:
                    continue

                file_path = None
                if avail:
                    fp = avail.get(label.upper())
                    if fp:
                        file_path = os.path.abspath(fp)

                result.append({
                    "points_2d": points_2d,
                    "grid_name": label,
                    "block_file": label,
                    "alt_names": [],
                    "file_path": file_path,
                    "source": "prj_boundary",
                    "snt_filename": prj_path,
                    "poly_layer": "PRJ",
                })

            self._prj_block_polygons_cache = result
            self._prj_block_polygons_cache_key = cache_key
            return result
        except Exception as exc:
            print(f"[prj-block] Could not build PRJ block polygons: {exc}")
            return []

    def _resolve_primary_buffer_block(self, grid_name, snt_filename=None, file_path=None):
        """Resolve the exact selected grid polygon, preferring owner/path matches."""
        wanted_grid = str(grid_name or "").strip()
        wanted_owner = self._normalize_snt_filename(snt_filename)
        wanted_path = os.path.normcase(os.path.abspath(str(file_path))) if file_path else ""
        ranked = []

        for block in self._ensure_snt_block_index():
            if not isinstance(block, dict):
                continue
            block_grid = str(block.get("grid_name") or "").strip()
            names = [block_grid, str(block.get("block_file") or "").strip()]
            names.extend(str(name or "").strip() for name in (block.get("alt_names") or []))
            identity_match = any(
                name and wanted_grid and (
                    name.casefold() == wanted_grid.casefold()
                    or names_share_numeric_identity(name, wanted_grid)
                )
                for name in names
            )
            if not identity_match:
                continue

            score = 10
            block_owner = self._normalize_snt_filename(block.get("snt_filename"))
            if wanted_owner and block_owner == wanted_owner:
                score += 20
            block_path = block.get("file_path")
            if wanted_path and block_path:
                try:
                    if os.path.normcase(os.path.abspath(str(block_path))) == wanted_path:
                        score += 40
                except Exception:
                    pass
            if block_grid.casefold() == wanted_grid.casefold():
                score += 5
            ranked.append((score, block))

        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked[0][1]

    def _save_current_before_buffer_load(self) -> bool:
        """Protect the current editable dataset before replacing it."""
        data = getattr(self.app, "data", None)
        if not isinstance(data, dict) or data.get("xyz") is None or len(data.get("xyz")) == 0:
            return True

        try:
            from gui.save_pointcloud import has_fenced_parent_writeback, save_pointcloud, save_pointcloud_quick

            if has_fenced_parent_writeback(self.app):
                saved = save_pointcloud(self.app, path=None, show_dialog=False)
            else:
                save_path = getattr(self.app, "last_save_path", None) or getattr(self.app, "loaded_file", None)
                saved = save_pointcloud_quick(self.app, save_path) if save_path else False
            if saved:
                return True
        except Exception as exc:
            print(f"[OpenWithBuffer] Current-data save failed: {exc}")

        reply = QMessageBox.question(
            self.app,
            "Current Data Not Saved",
            "The currently displayed point data could not be saved automatically.\n\n"
            "Continue opening the buffered block and replace the current view?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return reply == QMessageBox.Yes

    def open_grid_with_buffer_points(self, grid_name, snt_filename=None, alt_names=None, file_path=None):
        """Load a primary grid plus neighboring edge strips with source ownership."""
        from PySide6.QtWidgets import QInputDialog

        if self._fence_worker is not None and self._fence_worker.isRunning():
            QMessageBox.information(self.app, "Open Block with Buffer", "Another multi-file load is already running.")
            return

        primary = self._resolve_primary_buffer_block(grid_name, snt_filename, file_path)
        if primary is None:
            QMessageBox.warning(
                self.app,
                "Open Block with Buffer",
                f"Could not resolve the boundary polygon for block '{grid_name}'.",
            )
            return

        default_width = float(self.settings.value("grid_neighbor_buffer_width", 10.0) or 10.0)
        width, accepted = QInputDialog.getDouble(
            self.app,
            "Open Block with Buffer Points",
            "Neighbor buffer width (project units / metres):",
            default_width,
            0.01,
            10000.0,
            2,
        )
        if not accepted:
            return
        width = float(width)
        self.settings.setValue("grid_neighbor_buffer_width", width)

        try:
            buffered_polygon = _buffer_polygon_xy(primary.get("points_2d", []), width)
        except Exception as exc:
            QMessageBox.critical(self.app, "Open Block with Buffer", f"Could not create the grid buffer:\n\n{exc}")
            return
        if len(buffered_polygon) < 3:
            QMessageBox.warning(self.app, "Open Block with Buffer", "The selected grid has an invalid boundary polygon.")
            return

        # This polygon was created from PRJ boundary coordinates and is already
        # in the world coordinate system. Do not transform it a second time.
        scan = self.get_snt_blocks_intersecting_polygon(
            buffered_polygon,
            coordinates_are_world=True,
        )
        candidate_blocks = list(scan.get("intersecting_blocks", []) or [])
        if not candidate_blocks:
            QMessageBox.information(self.app, "Open Block with Buffer", "No grid files intersect the requested buffer.")
            return

        las_folder = self._resolve_las_folder_for_fence()
        if las_folder is None:
            QMessageBox.warning(self.app, "Open Block with Buffer", "Could not locate the LAZ/LAS folder for these grids.")
            return

        jobs, missing_blocks = self._resolve_candidate_block_jobs(candidate_blocks, las_folder)
        if not jobs:
            QMessageBox.information(self.app, "Open Block with Buffer", "No matching LAZ/LAS files were found.")
            return
        if not self._save_current_before_buffer_load():
            return

        self._fence_operation_label = "Open Block with Buffer"
        self._start_fence_worker(
            jobs=jobs,
            fence_polygon=buffered_polygon,
            fence_bbox=scan.get("fence_bbox"),
            base_stats={
                "operation": "buffered_grid",
                "primary_grid": str(grid_name or primary.get("grid_name") or ""),
                "buffer_width": width,
                "block_scan_seconds": 0.0,
                "total_blocks": int(scan.get("total_blocks", 0)),
                "bbox_candidates": int(scan.get("bbox_candidates", 0)),
                "intersecting_count": len(candidate_blocks),
                "missing_blocks": missing_blocks,
            },
        )

    def activate_load_by_fence_tool(self):
        """
        Activate one-shot fence mode:
        1) switch digitizer to polyline
        2) wait for closed polygon
        3) load only points inside that fence from intersecting SNT blocks
        """
        if self._fence_worker is not None and self._fence_worker.isRunning():
            QMessageBox.information(self.app, "Load by Fence", "A fence load is already running.")
            return

        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            QMessageBox.warning(self.app, "Load by Fence", "Digitizer tool is not available.")
            return

        # Mirror the user's manual workaround: entering the Draw tab re-enables
        # the digitizer and restores the expected observer/input ownership.
        try:
            if hasattr(self.app, "_handle_menu_click"):
                self.app._handle_menu_click("draw")
            elif hasattr(self.app, "_enter_draw_tab_mode"):
                self.app._enter_draw_tab_mode()
            elif hasattr(self.app, "enable_digitizer_mode"):
                self.app.enable_digitizer_mode()
        except Exception as draw_mode_exc:
            print(f"[LoadByFence] Draw-mode activation fallback failed: {draw_mode_exc}")

        try:
            digitizer.enabled = True
        except Exception:
            pass

        block_polygons = self._ensure_snt_block_index()
        if not block_polygons:
            QMessageBox.warning(
                self.app,
                "Load by Fence",
                "No SNT block index found.\nAttach an SNT first, then try Load by Fence.",
            )
            return

        self.deactivate_load_by_fence_tool()
        self._fence_mode_active = True

        try:
            styles = getattr(digitizer, "draw_tool_styles", {}) or {}
            current = dict(styles.get("polyline", {}))
            self._fence_polyline_style_backup = current
            styles.setdefault("polyline", {})
            styles["polyline"]["color"] = (1.0, 0.0, 0.0)
            styles["polyline"]["width"] = max(2, int(styles["polyline"].get("width", 3)))
            styles["polyline"]["style"] = "solid"
        except Exception:
            self._fence_polyline_style_backup = None

        try:
            digitizer.remove_drawing_finalized_callback(self._on_fence_drawing_finalized)
        except Exception:
            pass
        digitizer.add_drawing_finalized_callback(self._on_fence_drawing_finalized)

        digitizer.set_tool("Polyline")
        if hasattr(self.app, "statusBar"):
            self.app.statusBar().showMessage(
                "Load by Fence: left-click to draw fence polygon, right-click to finish.",
                7000,
            )

    def deactivate_load_by_fence_tool(self, keep_tool: bool = False):
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is not None:
            try:
                digitizer.remove_drawing_finalized_callback(self._on_fence_drawing_finalized)
            except Exception:
                pass
            try:
                if self._fence_polyline_style_backup is not None:
                    styles = getattr(digitizer, "draw_tool_styles", {}) or {}
                    styles.setdefault("polyline", {})
                    styles["polyline"].update(self._fence_polyline_style_backup)
            except Exception:
                pass
            if not keep_tool and getattr(digitizer, "active_tool", None) == "polyline":
                try:
                    digitizer.set_tool(None)
                except Exception:
                    pass

        self._fence_mode_active = False
        self._fence_polyline_style_backup = None

    def _on_fence_drawing_finalized(self, drawing_entry):
        if not self._fence_mode_active:
            return
        if not isinstance(drawing_entry, dict):
            return

        drawing_type = str(drawing_entry.get("type", "")).lower()
        if drawing_type not in {"polyline", "polygon", "rectangle", "freehand", "circle"}:
            return

        fence_polygon = self.collect_fence_polygon(drawing_entry)
        if len(fence_polygon) < 3:
            QMessageBox.warning(
                self.app,
                "Load by Fence",
                "Fence needs at least 3 vertices.\nDraw a larger closed polygon.",
            )
            return

        self._tint_fence_drawing_actor(drawing_entry.get("actor"))
        self.deactivate_load_by_fence_tool(keep_tool=True)
        try:
            self.load_points_from_snt_by_fence(fence_polygon)
        except Exception as exc:
            print(f"[LoadByFence] callback error: {exc}")
            print(traceback.format_exc())
            QMessageBox.critical(
                self.app,
                "Load by Fence",
                f"Load by fence failed:\n\n{exc}",
            )

    def collect_fence_polygon(self, drawing_entry) -> List[Tuple[float, float]]:
        coords = []
        if isinstance(drawing_entry, dict):
            coords = drawing_entry.get("coords", []) or []
        return _normalize_ring_xy(coords)

    def screen_to_world_polygon(self, polygon_points) -> List[Tuple[float, float]]:
        # Digitizer stores coordinates in world space already.
        return _normalize_ring_xy(polygon_points)

    def _tint_fence_drawing_actor(self, actor):
        if actor is None or not hasattr(actor, "GetProperty"):
            return
        try:
            prop = actor.GetProperty()
            prop.SetColor(1.0, 0.1, 0.1)
            prop.SetLineWidth(max(3.0, float(prop.GetLineWidth() or 3.0)))
            prop.SetOpacity(1.0)
            if hasattr(self.app, "vtk_widget"):
                self.app.vtk_widget.render()
        except Exception:
            pass

    def get_snt_blocks_intersecting_polygon(
        self,
        fence_polygon_world: List[Tuple[float, float]],
        *,
        coordinates_are_world: bool = False,
    ):
        fence_polygon = (
            _normalize_ring_xy(fence_polygon_world)
            if coordinates_are_world
            else self.screen_to_world_polygon(fence_polygon_world)
        )
        if len(fence_polygon) < 3:
            return {
                "fence_polygon": fence_polygon,
                "fence_bbox": None,
                "total_blocks": 0,
                "bbox_candidates": 0,
                "intersecting_blocks": [],
            }

        fence_bbox = _polygon_bounds_xy(fence_polygon)
        blocks = getattr(self.app, "snt_block_polygons", None) or []
        intersecting: List[Dict[str, object]] = []
        bbox_candidates = 0

        for idx, blk in enumerate(blocks):
            block_poly = _normalize_ring_xy(blk.get("points_2d", []))
            if len(block_poly) < 3:
                continue
            block_bbox = _polygon_bounds_xy(block_poly)
            if not _bbox_intersects(block_bbox, fence_bbox):
                continue
            bbox_candidates += 1
            if not _polygons_intersect_xy(fence_polygon, block_poly):
                continue

            grid_name = str(blk.get("grid_name") or "").strip()
            block_id = grid_name or f"BLOCK_{idx + 1:04d}"
            xs = [p[0] for p in block_poly]
            ys = [p[1] for p in block_poly]
            centroid = (float(sum(xs) / len(xs)), float(sum(ys) / len(ys)))

            intersecting.append({
                "block_id": block_id,
                "grid_name": grid_name,
                "snt_filename": blk.get("snt_filename"),
                "block_file": blk.get("block_file"),
                "file_path": blk.get("file_path"),
                "alt_names": list(blk.get("alt_names") or []),
                "points_2d": block_poly,
                "bbox": block_bbox,
                "centroid": centroid,
            })

        return {
            "fence_polygon": fence_polygon,
            "fence_bbox": fence_bbox,
            "total_blocks": len(blocks),
            "bbox_candidates": bbox_candidates,
            "intersecting_blocks": intersecting,
        }

    def _find_las_folder_near_path(self, seed_path: Path):
        if seed_path is None:
            return None
        if not seed_path.exists():
            return None

        root = seed_path if seed_path.is_dir() else seed_path.parent
        if root is None or (not root.exists()):
            return None

        # Check root first, then common subfolder names
        candidates = [root]
        for sub_name in ("lazz", "LAZZ", "laz", "LAZ", "las", "LAS"):
            candidates.append(root / sub_name)

        # Also check ALL immediate subfolders for LAZ/LAS files
        try:
            for entry in root.iterdir():
                if entry.is_dir() and entry not in candidates:
                    candidates.append(entry)
        except Exception:
            pass

        for folder in candidates:
            try:
                if folder.exists() and folder.is_dir():
                    files = list(folder.glob("*.laz")) + list(folder.glob("*.las"))
                    if files:
                        return folder
            except Exception:
                continue

        return None

    def _find_las_file_recursive(self, root_dir, candidates):
        """
        Search the full subtree under root_dir for a LAZ/LAS file whose
        stem exactly matches one of `candidates` (case-insensitive).

        Used as a targeted fallback when the specific block's file lives
        more than one folder level deep in an arbitrarily-named subfolder
        (common for PRJ-identified blocks) — unlike
        _find_las_folder_near_path, this matches by filename, not just
        "any LAZ file present", so it never returns an unrelated block's
        folder.
        """
        if not root_dir or not candidates:
            return None
        wanted = {str(c).strip().upper() for c in candidates if c}
        if not wanted:
            return None
        try:
            root_path = Path(str(root_dir))
        except Exception:
            return None
        if not root_path.exists():
            return None
        try:
            for ext in ("*.laz", "*.las"):
                for fp in root_path.rglob(ext):
                    if fp.stem.strip().upper() in wanted:
                        return fp
        except Exception:
            pass
        return None

    def _normalize_snt_filename(self, value):
        """Normalize an SNT filename/path for safe comparison."""
        if not value:
            return ""
        try:
            return Path(str(value)).name.strip().lower()
        except Exception:
            return os.path.basename(str(value)).strip().lower()

    def _normalize_snt_stem(self, value):
        """Normalize SNT stem for fallback comparison."""
        if not value:
            return ""
        try:
            return Path(str(value)).stem.strip().lower()
        except Exception:
            return os.path.splitext(os.path.basename(str(value)))[0].strip().lower()

    def _iter_snt_attachment_records(self):
        """Yield all attached SNT records from app.snt_attachments and app.snt_actors."""
        seen = set()
        for store_name in ("snt_attachments", "snt_actors"):
            for record in list(getattr(self.app, store_name, []) or []):
                if not isinstance(record, dict):
                    continue
                filename = record.get("filename") or ""
                full_path = record.get("full_path") or ""
                key = (
                    self._normalize_snt_filename(filename),
                    self._normalize_snt_filename(full_path),
                    self._normalize_snt_stem(filename),
                    self._normalize_snt_stem(full_path),
                )
                if key in seen:
                    continue
                seen.add(key)
                yield record

    def _find_snt_attachment_record(self, snt_filename):
        """Return the attachment record that owns the requested SNT filename."""
        wanted_name = self._normalize_snt_filename(snt_filename)
        wanted_stem = self._normalize_snt_stem(snt_filename)
        if not wanted_name and not wanted_stem:
            return None
        for record in self._iter_snt_attachment_records():
            record_name = self._normalize_snt_filename(record.get("filename"))
            record_full_name = self._normalize_snt_filename(record.get("full_path"))
            record_stem = self._normalize_snt_stem(record.get("filename"))
            record_full_stem = self._normalize_snt_stem(record.get("full_path"))
            if wanted_name and wanted_name in {record_name, record_full_name}:
                return record
            if wanted_stem and wanted_stem in {record_stem, record_full_stem}:
                return record
        return None

    def _find_las_folder_from_snt(self, snt_filename):
        """Resolve LAZ/LAS folder from the owning SNT file."""
        record = self._find_snt_attachment_record(snt_filename)
        if not record:
            # No SNT attachment matches this owner — the owner may still be a
            # real path on disk that just isn't an "attached" SNT, e.g. a
            # .prj file loaded directly via the PRJ Block Identifier (its
            # snt_filename is the .prj path itself, which is never recorded
            # in app.snt_attachments/app.snt_actors). Try resolving a
            # LAZ/LAS folder directly from that path before giving up.
            print(f"   ⚠️ SNT owner not found for: {snt_filename}")
            try:
                direct_path = Path(str(snt_filename)) if snt_filename else None
            except Exception:
                direct_path = None
            if direct_path is not None and direct_path.exists():
                print("📋 STRATEGY SNT (direct path): Auto-detect from owner path")
                print(f"   Owner path: {direct_path}")
                folder = self._find_las_folder_near_path(direct_path)
                if folder is not None:
                    print(f"   ✅ FOUND LAZ/LAS folder near owner path: {folder}")
                    self._save_folder_to_settings(folder)
                    return folder
                print(f"   ❌ No LAZ/LAS folder found near owner path: {direct_path}")
            return None
        raw = record.get("full_path") or record.get("filename")
        if not raw:
            print(f"   ⚠️ SNT owner has no path: {snt_filename}")
            return None
        snt_path = Path(str(raw))
        print("📋 STRATEGY SNT: Auto-detect from SNT owner")
        print(f"   SNT owner: {snt_path.name}")
        print(f"   SNT path: {snt_path}")
        folder = self._find_las_folder_near_path(snt_path)
        if folder is not None:
            print(f"   ✅ FOUND LAZ/LAS folder for SNT: {folder}")
            self._save_folder_to_settings(folder)
            return folder
        print(f"   ❌ No LAZ/LAS folder found near SNT: {snt_path}")
        return None

    def _infer_snt_filename_for_grid(self, grid_name):
        """Infer SNT owner from app.snt_block_polygons when the label lacks metadata."""
        if not grid_name:
            return None
        wanted = str(grid_name).strip().lower()
        owners = []
        candidate_blocks = list(getattr(self.app, "snt_block_polygons", []) or [])
        # Also consider PRJ Block Identifier polygons — a block identified
        # directly from a .prj (no SNT attachment) is never cached into
        # app.snt_block_polygons, so without this it can never be inferred.
        candidate_blocks.extend(self._get_prj_block_polygons())
        for block in candidate_blocks:
            if not isinstance(block, dict):
                continue
            block_grid = str(block.get("grid_name") or "").strip().lower()
            if block_grid != wanted:
                continue
            owner = block.get("snt_filename")
            if owner and owner not in owners:
                owners.append(owner)
        if len(owners) == 1:
            return owners[0]
        if len(owners) > 1:
            print(f"   ⚠️ Ambiguous grid owner for '{grid_name}': {', '.join(str(x) for x in owners)}")
        return None

    def _resolve_las_folder_for_fence(self):
        for att in self._iter_snt_attachment_records():
            raw = att.get("full_path") or att.get("filename")
            if not raw:
                continue
            p = Path(str(raw))
            folder = self._find_las_folder_near_path(p)
            if folder is not None:
                self._save_folder_to_settings(folder)
                return folder

        dxf_folder = self._find_las_folder_from_dxf()
        if dxf_folder is not None:
            return dxf_folder

        saved = self.settings.value("last_las_folder", "")
        if saved:
            p = Path(str(saved))
            if p.exists() and p.is_dir():
                files = list(p.glob("*.laz")) + list(p.glob("*.las"))
                if files:
                    return p

        return self._prompt_user_for_las_folder()

    def _resolve_las_file_for_grid_name(self, all_files: List[Path], grid_name: str) -> Optional[Path]:
        return find_matching_lidar_file(all_files, grid_name)

    def _resolve_candidate_block_jobs(self, candidate_blocks: List[Dict[str, object]], las_folder: Path):
        jobs: List[Dict[str, object]] = []
        missing: List[str] = []
        used_files: set = set()
        folder_file_cache: Dict[str, List[Path]] = {}

        def _files_for_folder(folder: Path) -> List[Path]:
            if folder is None:
                return []
            folder_key = str(Path(folder).resolve()).lower()
            if folder_key not in folder_file_cache:
                folder_file_cache[folder_key] = list(Path(folder).glob("*.laz")) + list(Path(folder).glob("*.las"))
            return folder_file_cache[folder_key]

        for blk in candidate_blocks:
            grid_name = str(blk.get("grid_name") or "").strip()
            block_id = str(blk.get("block_id") or grid_name or "BLOCK")
            snt_filename = str(blk.get("snt_filename") or "").strip()
            snt_stem = Path(snt_filename).stem if snt_filename else ""
            owner_folder = self._find_las_folder_from_snt(snt_filename) if snt_filename else None
            search_folder = owner_folder or las_folder
            all_files = _files_for_folder(search_folder)

            laz_file = None
            direct_path = str(blk.get("file_path") or "").strip()
            if direct_path:
                direct_candidate = Path(direct_path)
                if direct_candidate.exists() and direct_candidate.suffix.lower() in {".las", ".laz"}:
                    laz_file = direct_candidate
            candidate_names = [grid_name, block_id, snt_stem]
            block_file = str(blk.get("block_file") or "").strip()
            if block_file and block_file not in candidate_names:
                candidate_names.append(block_file)
            for alt_name in (blk.get("alt_names") or []):
                alt_name = str(alt_name or "").strip()
                if alt_name and alt_name not in candidate_names:
                    candidate_names.append(alt_name)
            for candidate_name in candidate_names:
                if laz_file is not None:
                    break
                if not candidate_name:
                    continue
                laz_file = self._resolve_las_file_for_grid_name(all_files, candidate_name)
                if laz_file is None and search_folder is not None:
                    laz_file = self._find_matching_las_file(search_folder, candidate_name)
                if laz_file is not None:
                    break
            if laz_file is None:
                missing.append(block_id)
                continue

            key = str(laz_file.resolve()).lower()
            if key in used_files:
                continue
            used_files.add(key)

            jobs.append({
                "block_id": block_id,
                "grid_name": grid_name or laz_file.stem,
                "file_path": str(laz_file),
                "block_centroid": blk.get("centroid"),
                "snt_filename": snt_filename,
            })

        return jobs, missing

    def load_points_from_snt_by_fence(self, fence_polygon_world):
        self._fence_operation_label = "Load by Fence"
        if not fence_polygon_world:
            QMessageBox.warning(self.app, "Load by Fence", "Fence polygon is empty.")
            return

        t_scan = time.time()
        scan = self.get_snt_blocks_intersecting_polygon(fence_polygon_world)
        fence_polygon = scan.get("fence_polygon") or []
        fence_bbox = scan.get("fence_bbox")
        total_blocks = int(scan.get("total_blocks", 0))
        bbox_candidates = int(scan.get("bbox_candidates", 0))
        intersecting_blocks = list(scan.get("intersecting_blocks", []) or [])
        block_scan_seconds = float(time.time() - t_scan)

        print(f"[LoadByFence] SNT folder candidates scanned: {total_blocks}")
        print(f"[LoadByFence] Fence polygon world coordinates: {fence_polygon}")
        if fence_bbox:
            print(
                f"[LoadByFence] Fence bbox: minx={fence_bbox[0]:.3f}, miny={fence_bbox[1]:.3f}, "
                f"maxx={fence_bbox[2]:.3f}, maxy={fence_bbox[3]:.3f}"
            )
        print(f"[LoadByFence] Candidate blocks by bbox: {bbox_candidates}")
        print(f"[LoadByFence] Intersecting blocks: {len(intersecting_blocks)}")
        if intersecting_blocks:
            names = [str(b.get("block_id", "")) for b in intersecting_blocks]
            print(f"[LoadByFence] Intersecting block IDs: {', '.join(names)}")

        if not intersecting_blocks:
            QMessageBox.information(self.app, "Load by Fence", "No SNT blocks intersect selected fence.")
            return

        las_folder = self._resolve_las_folder_for_fence()
        if las_folder is None:
            QMessageBox.warning(
                self.app,
                "Load by Fence",
                "Could not locate LAZ/LAS folder for the selected SNT blocks.",
            )
            return
        print(f"[LoadByFence] SNT folder: {las_folder}")

        jobs, missing_blocks = self._resolve_candidate_block_jobs(intersecting_blocks, las_folder)
        if missing_blocks:
            print(f"[LoadByFence] Missing block files: {', '.join(missing_blocks)}")

        if not jobs:
            QMessageBox.information(self.app, "Load by Fence", "No points found inside selected fence.")
            return

        self._start_fence_worker(
            jobs=jobs,
            fence_polygon=fence_polygon,
            fence_bbox=fence_bbox,
            base_stats={
                "block_scan_seconds": block_scan_seconds,
                "total_blocks": total_blocks,
                "bbox_candidates": bbox_candidates,
                "intersecting_count": len(intersecting_blocks),
                "missing_blocks": missing_blocks,
            },
        )

    def _start_fence_worker(self, jobs, fence_polygon, fence_bbox, base_stats):
        operation_label = str(getattr(self, "_fence_operation_label", "Load by Fence"))
        if fence_bbox is None:
            QMessageBox.warning(self.app, operation_label, "Invalid selection polygon.")
            return

        if self._fence_worker is not None and self._fence_worker.isRunning():
            QMessageBox.information(self.app, operation_label, "A multi-file point load is already running.")
            return

        self._fence_last_stats = dict(base_stats or {})

        progress = QProgressDialog(
            "Scanning blocks",
            "Cancel",
            0,
            100,
            self.app,
        )
        progress.setWindowTitle(operation_label)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setValue(0)
        progress.show()
        self._fence_progress = progress

        worker = SNTFenceLoadWorker(
            block_jobs=jobs,
            fence_polygon_xy=fence_polygon,
            fence_bbox=fence_bbox,
            chunk_size=self._fence_chunk_size,
            dedupe_boundary_points=True,
        )
        self._fence_worker = worker

        worker.progress.connect(self._on_fence_worker_progress)
        worker.completed.connect(self._on_fence_worker_completed)
        worker.failed.connect(self._on_fence_worker_failed)
        worker.cancelled.connect(self._on_fence_worker_cancelled)
        progress.canceled.connect(worker.cancel)
        worker.start()

    def _on_fence_worker_progress(self, value: int, message: str):
        if self._fence_progress is None:
            return
        try:
            self._fence_progress.setLabelText(str(message))
            self._fence_progress.setValue(int(max(0, min(100, value))))
        except Exception:
            pass

    def _on_fence_worker_completed(self, worker_stats: Dict[str, object]):
        render_seconds = 0.0
        data = self._fence_worker.result_data if self._fence_worker is not None else None

        if data is not None and len(data.get("xyz", [])) > 0:
            try:
                render_seconds = self.render_fenced_points_in_main_view(data)
            except Exception as render_exc:
                self._on_fence_worker_failed(f"Render failed: {render_exc}")
                return

        stats = dict(self._fence_last_stats or {})
        stats.update(worker_stats or {})
        stats["render_seconds"] = float(render_seconds)
        self._fence_last_stats = stats

        block_logs = stats.get("block_logs", []) or []
        for row in block_logs:
            bid = row.get("block_id")
            loaded = int(row.get("loaded_points", 0))
            inside = int(row.get("inside_points", 0))
            if row.get("error"):
                print(f"[LoadByFence] Block {bid} error: {row.get('error')}")
                continue
            print(f"[LoadByFence] Block {bid} loaded points: {loaded:,}")
            print(f"[LoadByFence] Block {bid} points inside fence: {inside:,}")

        total_render = int(stats.get("total_render_points", 0))
        print(f"[LoadByFence] Total rendered fenced points: {total_render:,}")
        print(
            f"[LoadByFence] Timings: scan={stats.get('block_scan_seconds', 0):.3f}s, "
            f"load+filter={stats.get('worker_seconds', 0):.3f}s, render={stats.get('render_seconds', 0):.3f}s"
        )

        if self._fence_progress is not None:
            try:
                self._fence_progress.setValue(100)
                self._fence_progress.close()
            except Exception:
                pass
            self._fence_progress = None

        if total_render <= 0:
            QMessageBox.information(self.app, "Load by Fence", "No points found inside selected fence.")
            if hasattr(self.app, "statusBar"):
                self.app.statusBar().showMessage("No points found inside selected fence.", 5000)
        else:
            block_count = len([b for b in block_logs if not b.get("error")])
            operation = str(stats.get("operation") or "fence")
            if operation == "buffered_grid":
                info_text = (
                    f"Loaded {total_render:,} points from {block_count} grid file(s).\n"
                    f"Primary grid: {stats.get('primary_grid', '')}\n"
                    f"Neighbor buffer: {float(stats.get('buffer_width', 0.0)):.2f} project units\n\n"
                    "Classification edits will be saved back to each point's own source file."
                )
                info_title = "Open Block with Buffer"
            else:
                info_text = f"Loaded {total_render:,} points from {block_count} SNT block(s)."
                info_title = "Load by Fence"
            QMessageBox.information(self.app, info_title, info_text)
            if hasattr(self.app, "statusBar"):
                self.app.statusBar().showMessage(
                    f"Loaded {total_render:,} points from {block_count} SNT block(s).",
                    6000,
                )

        self._fence_worker = None

    def _on_fence_worker_failed(self, error_text: str):
        if self._fence_progress is not None:
            try:
                self._fence_progress.close()
            except Exception:
                pass
            self._fence_progress = None

        self._fence_worker = None
        print(f"[LoadByFence] ERROR: {error_text}")
        traceback.print_exc()
        QMessageBox.critical(self.app, "Load by Fence", f"Load by fence failed:\n\n{error_text}")

    def _on_fence_worker_cancelled(self, message: str):
        if self._fence_progress is not None:
            try:
                self._fence_progress.close()
            except Exception:
                pass
            self._fence_progress = None
        self._fence_worker = None
        if hasattr(self.app, "statusBar"):
            self.app.statusBar().showMessage(message, 3000)

    def render_fenced_points_in_main_view(self, fenced_data: Dict[str, np.ndarray]) -> float:
        t0 = time.time()

        xyz = np.asarray(fenced_data.get("xyz"), dtype=np.float64)
        classification = np.asarray(fenced_data.get("classification"), dtype=np.uint8)
        if xyz.ndim != 2 or xyz.shape[0] == 0:
            return 0.0

        point_count = int(xyz.shape[0])
        # The combined multi-source dataset has a different index space from
        # any previously loaded single grid. Never retain stale clear/reload
        # indices that could delete unrelated buffered points.
        self.loaded_grids.clear()
        self.grid_aliases.clear()
        self.app.data = {
            "xyz": xyz,
            "classification": classification,
        }

        try:
            source_files = [str(p) for p in list(fenced_data.get("_fence_source_files") or []) if str(p)]
            source_file_ids_raw = fenced_data.get("_fence_source_file_ids")
            source_point_indices_raw = fenced_data.get("_fence_source_point_indices")
            if source_files and source_file_ids_raw is not None and source_point_indices_raw is not None:
                source_file_ids = np.asarray(source_file_ids_raw, dtype=np.int32)
                source_point_indices = np.asarray(source_point_indices_raw, dtype=np.int64)
                if source_file_ids.shape[0] != point_count or source_point_indices.shape[0] != point_count:
                    raise ValueError("Fence point-source metadata length mismatch.")
                if np.any(source_file_ids < 0) or np.any(source_file_ids >= len(source_files)):
                    raise ValueError("Fence source file IDs are out of range.")

                dup_owner = np.asarray(
                    fenced_data.get("_fence_duplicate_owner_indices", np.empty(0, dtype=np.int64)),
                    dtype=np.int64,
                )
                dup_file_ids = np.asarray(
                    fenced_data.get("_fence_duplicate_source_file_ids", np.empty(0, dtype=np.int32)),
                    dtype=np.int32,
                )
                dup_point_idx = np.asarray(
                    fenced_data.get("_fence_duplicate_source_point_indices", np.empty(0, dtype=np.int64)),
                    dtype=np.int64,
                )
                if dup_owner.shape[0] != dup_file_ids.shape[0] or dup_owner.shape[0] != dup_point_idx.shape[0]:
                    raise ValueError("Fence duplicate alias metadata lengths do not match.")
                if dup_owner.size > 0:
                    if np.any(dup_owner < 0) or np.any(dup_owner >= point_count):
                        raise ValueError("Fence duplicate owners are out of range.")
                    if np.any(dup_file_ids < 0) or np.any(dup_file_ids >= len(source_files)):
                        raise ValueError("Fence duplicate source file IDs are out of range.")

                session_id = uuid.uuid4().hex
                self.app.data["_fence_session_id"] = session_id
                self.app.data["_fence_source_file_ids"] = source_file_ids
                self.app.data["_fence_source_point_indices"] = source_point_indices
                self.app._fence_parent_session = {
                    "session_id": session_id,
                    "source_files": source_files,
                    "operation": str((self._fence_last_stats or {}).get("operation") or "fence"),
                    "primary_grid": (self._fence_last_stats or {}).get("primary_grid"),
                    "buffer_width": (self._fence_last_stats or {}).get("buffer_width"),
                    "duplicate_owner_indices": dup_owner,
                    "duplicate_source_file_ids": dup_file_ids,
                    "duplicate_source_point_indices": dup_point_idx,
                }
            else:
                self.app._fence_parent_session = None
        except Exception as map_exc:
            self.app._fence_parent_session = None
            print(f"⚠️ Fence source mapping disabled: {map_exc}")

        if fenced_data.get("rgb") is not None:
            self.app.data["rgb"] = np.asarray(fenced_data.get("rgb"), dtype=np.uint8)
        if fenced_data.get("intensity") is not None:
            self.app.data["intensity"] = np.asarray(fenced_data.get("intensity"), dtype=np.float32)
        if fenced_data.get("return_number") is not None:
            self.app.data["return_number"] = np.asarray(fenced_data.get("return_number"), dtype=np.uint8)
        if fenced_data.get("number_of_returns") is not None:
            self.app.data["number_of_returns"] = np.asarray(fenced_data.get("number_of_returns"), dtype=np.uint8)

        self.app.loaded_file = None
        self.app.last_save_path = None
        self.app.display_mode = "class"

        palette = getattr(self.app, "class_palette", None) or {}
        if not palette:
            try:
                from gui.class_display import build_class_palette
                palette = build_class_palette(self.app.data["classification"])
                self.app.class_palette = palette
            except Exception:
                palette = {}

        if palette:
            self.app.apply_class_map({
                "classes": palette,
                "slot": 0,
                "target_view": 0,
                "color_mode": 0,
            })
        else:
            from gui.pointcloud_display import update_pointcloud
            update_pointcloud(self.app, "class")

        if hasattr(self.app, "_ensure_overlay_actors"):
            self.app._ensure_overlay_actors()

        if hasattr(self.app, "point_count_widget") and self.app.point_count_widget:
            try:
                from gui.point_count_widget import refresh_point_statistics
                refresh_point_statistics(self.app)
            except Exception:
                pass

        if hasattr(self.app, "_update_window_title"):
            stats = self._fence_last_stats or {}
            if stats.get("operation") == "buffered_grid":
                title = (
                    f"{stats.get('primary_grid', 'Grid')} + "
                    f"{float(stats.get('buffer_width', 0.0)):.2f}-unit buffer ({len(xyz):,} pts)"
                )
            else:
                title = f"Fenced SNT Load ({len(xyz):,} pts)"
            self.app._update_window_title(
                title,
                getattr(self.app, "project_crs_epsg", None),
            )

        self._fence_last_data_count = int(len(xyz))
        return float(time.time() - t0)

    def clear_fenced_load(self):
        if self._fence_worker is not None and self._fence_worker.isRunning():
            self._fence_worker.cancel()

        had_data = bool(getattr(self.app, "data", None) is not None)
        self.app.data = None
        self.app._fence_parent_session = None
        self.app.loaded_file = None
        self.app.last_save_path = None

        try:
            if hasattr(self.app, "vtk_widget") and self.app.vtk_widget:
                self.app.vtk_widget.clear()
                self.app.vtk_widget.render()
        except Exception:
            try:
                ren = self.app.vtk_widget.renderer
                ren.RemoveAllViewProps()
                self.app.vtk_widget.render()
            except Exception:
                pass

        if hasattr(self.app, "_ensure_overlay_actors"):
            self.app._ensure_overlay_actors()

        if hasattr(self.app, "_update_window_title"):
            self.app._update_window_title("No Data", getattr(self.app, "project_crs_epsg", None))

        self._fence_last_data_count = 0
        if hasattr(self.app, "statusBar"):
            msg = "Fenced load cleared." if had_data else "No fenced points to clear."
            self.app.statusBar().showMessage(msg, 3000)

    def _show_file_selection_dialog(self, las_folder, grid_name):
        """
        Fallback dialog: let user pick a LAS/LAZ file from las_folder
        when no automatic match is found for grid_name.
        """
        from PySide6.QtWidgets import QFileDialog, QMessageBox

        # Make sure we have a string path
        folder_str = str(las_folder) if las_folder is not None else ""

        # Let user pick a file
        file_path, _ = QFileDialog.getOpenFileName(
            self.app,
            f"Select LAZ/LAS file for grid {grid_name}",
            folder_str,
            "LiDAR Files (*.laz *.las);;All Files (*.*)",
        )

        if not file_path:
            # User cancelled
            return

        # Optional: small confirmation
        reply = QMessageBox.question(
            self.app,
            "Confirm Grid File",
            f"Use this file for grid '{grid_name}'?\n\n{os.path.basename(file_path)}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )

        if reply != QMessageBox.Yes:
            return

        # Reuse existing loading pipeline
        self._load_las_file(Path(file_path), grid_name)

            
    def _is_tool_operation_in_progress(self):
        """Returns True if any user tool is currently performing an operation that uses right-click."""
        app = self.app
        if not app: return False

        # 1. Digitize (Draw) Tools: active when a tool is selected and has points
        digitizer = getattr(app, "digitizer", None)
        if digitizer and (digitizer.active_tool or getattr(digitizer, "vertex_move_mode", False)):
            # Delete Vertex uses right-click as confirmation only after a
            # vertex has been selected. Once it is deleted, allow the normal
            # grid/point-cloud context menu to handle the next right-click.
            if (
                getattr(digitizer, "active_tool", None) == "deletevertex"
                and getattr(digitizer, "selected_vertex_idx", None) is not None
            ):
                return True
            if len(getattr(digitizer, "temp_points", [])) > 0:
                return True
            if getattr(digitizer, "dragging_vertex", None) is not None:
                return True

        # 1b. Ortho-Polygon tool keeps its own point list separate from
        #     digitizer.temp_points, so check it explicitly. Right-click
        #     finalises the polygon — yield once the user has placed >= 1 point.
        if digitizer is not None and getattr(digitizer, "active_tool", None) == "orthopolygon":
            ortho_tool = getattr(digitizer, "_ortho_polygon_tool", None)
            if ortho_tool is not None and len(getattr(ortho_tool, "points", []) or []) > 0:
                return True

        # 1c. Temp Fence tool: right-click finalises the fence — yield once
        #     the user has placed at least one vertex, same as ortho-polygon.
        tf_tool = getattr(app, "temp_fence_tool", None)
        if tf_tool is not None and getattr(tf_tool, "active", False):
            if getattr(tf_tool, "_drawing", False) and len(getattr(tf_tool, "_points", []) or []) > 0:
                return True

        # 2. Measurement Tool: active during measurement drag
        mtool = getattr(app, "measurement_tool", None)
        if mtool and getattr(mtool, "is_measuring", False):
            if len(getattr(mtool, "measurement_points", [])) > 0:
                return True

        # 3. Curve Tool: active during curve path drawing
        ctool = getattr(app, "curve_tool", None)
        if ctool and ctool.active:
            if len(getattr(ctool, "points", [])) > 0:
                return True

        # 4. Zoom Rectangle Tool: active when drawing rectangle
        ztool = getattr(app, "zoom_rectangle_tool", None)
        if ztool and ztool.active and getattr(ztool, "start_pos", None) is not None:
            return True

        # 5. Select Rectangle Tool: active when drawing/processing selection
        stool = getattr(app, "select_rectangle_tool", None)
        if stool and stool.active:
            if getattr(stool, "is_drawing", False) or getattr(stool, "start_pos", None) is not None:
                return True

        # 6. Classification polygon: right-click closes/classifies an in-progress polygon.
        # Yield only after the user has started drawing so normal grid-label menus still work.
        if getattr(app, "active_classify_tool", None) == "polygon":
            classify_tools = []
            main_tool = getattr(app, "classify_interactor", None)
            if main_tool is not None:
                classify_tools.append(main_tool)
            cut_tool = getattr(app, "cut_classify_interactor", None)
            if cut_tool is not None:
                classify_tools.append(cut_tool)
            classify_tools.extend(getattr(app, "classify_interactors", {}).values())

            for ctool in classify_tools:
                if getattr(ctool, "drawing_points", None):
                    return True

        return False

    def _true_label_color(self, actor):
        """Return an actor's real (non-hover-highlighted) color.

        on_mouse_move's _highlight_label() temporarily overwrites a label's
        color with bright yellow while hovered, and caches the real color in
        self.original_colors so _unhighlight_label() can restore it later.
        If the actor is currently mid-hover when an edit dialog is opened,
        reading GetProperty()/GetTextProperty() directly would pick up that
        temporary yellow instead of the real color. Prefer the cache.
        """
        try:
            cached = getattr(self, 'original_colors', None)
            if cached is not None and actor in cached:
                c = cached[actor].get('color')
                if c is not None:
                    return tuple(c)
        except Exception:
            pass

        try:
            if isinstance(actor, vtk.vtkTextActor3D):
                return tuple(actor.GetTextProperty().GetColor())
        except Exception:
            pass
        try:
            return tuple(actor.GetProperty().GetColor())
        except Exception:
            return (1, 1, 1)

    def _capture_label_state(self, actor):
        """Snapshot a label actor's current text/size/color for a dialog
        default or an undo entry. Never raises — falls back to safe
        defaults on any VTK access error so a single bad actor can't crash
        the caller."""
        is_text_actor3d = False
        try:
            is_text_actor3d = isinstance(actor, vtk.vtkTextActor3D)
        except Exception:
            pass

        try:
            if is_text_actor3d:
                text_prop = actor.GetTextProperty()
                text = actor.GetInput() or getattr(actor, 'grid_name', '') or ''
                raw_size = text_prop.GetFontSize()
                size = int(75 if raw_size is None else raw_size)
            else:
                text = (
                    getattr(actor, 'display_text', None)
                    or getattr(actor, 'text_content', None)
                    or getattr(actor, 'grid_name', '')
                    or ''
                )
                raw_size = getattr(actor, '_naksha_label_font_size', 75)
                size = int(75 if raw_size is None else raw_size)
        except Exception:
            text = getattr(actor, 'grid_name', '') or ''
            size = 75

        color = self._true_label_color(actor)
        return {
            'actor': actor,
            'text': text,
            'size': size,
            'color': color,
            'is_text_actor3d': is_text_actor3d,
        }

    def _apply_label_state(self, actor, text, size, color, is_text_actor3d=None):
        """Set a grid-label actor's rendered text/font-size/color.

        Used for edits, undo-revert, and bulk font-size changes alike, so
        there is exactly one code path that can go wrong. Never raises —
        every VTK call is individually guarded so a single failed actor
        cannot crash a bulk operation or leave the app in a broken state.
        ``actor.grid_name`` is never touched: it is the lookup key used
        elsewhere (load_grid_las, loaded_grids, PRJ/SNT filename inference)
        to associate the label with its LAS/PRJ data.
        """
        if actor is None:
            return False
        try:
            if is_text_actor3d is None:
                is_text_actor3d = isinstance(actor, vtk.vtkTextActor3D)
        except Exception:
            is_text_actor3d = False

        text = text if text else getattr(actor, 'grid_name', '') or ''
        try:
            size = max(0, min(999, int(75 if size is None else size)))
        except (TypeError, ValueError):
            size = 75
        color = color if color else (1, 1, 1)

        try:
            if is_text_actor3d:
                text_prop = actor.GetTextProperty()
                actor.SetInput(text)
                text_prop.SetFontSize(size)
                text_prop.SetColor(*color)
            else:
                # vtkFollower labels are built either from a live vtkVectorText
                # pipeline connection (DXF path: SetInputConnection) or from a
                # cached/shared vtkPolyData (SNT path: SetInputData, cached by
                # text content in self._snt_text_poly_cache — potentially
                # shared with OTHER actors that happen to have the same text).
                # We must never mutate a shared connection/polydata in place;
                # instead always build a fresh, private vtkVectorText output
                # and hand it to this actor's mapper only.
                try:
                    # Bulk font-size edits pass the actor's own unchanged
                    # text back in — skip the rebuild entirely in that case
                    # so a 1500-label bulk resize doesn't recreate geometry
                    # it doesn't need to.
                    if getattr(actor, 'text_content', None) != text:
                        mapper = actor.GetMapper()
                        fresh_source = vtk.vtkVectorText()
                        fresh_source.SetText(text)
                        fresh_source.Update()
                        fresh_poly = vtk.vtkPolyData()
                        fresh_poly.DeepCopy(fresh_source.GetOutput())
                        # SetInputData automatically clears any prior
                        # SetInputConnection pipeline link on this port.
                        mapper.SetInputData(fresh_poly)
                except Exception as _src_err:
                    print(f"⚠️ Could not update grid label text source: {_src_err}")

                try:
                    base_scale = getattr(actor, '_naksha_base_scale', None)
                    if base_scale is None:
                        base_scale = float(actor.GetScale()[0])
                        actor._naksha_base_scale = base_scale
                    base_size = float(getattr(actor, '_naksha_base_font_size', 75) or 75)
                    if base_size > 0:
                        # SNT/DXF follower glyphs are world geometry rather
                        # than screen text. A strong display multiplier keeps
                        # labels readable across kilometre-scale grid cells:
                        # 999 pt is intentionally very large, while 50 pt is
                        # still clearly visible.
                        new_scale = base_scale * (float(size) / base_size) * 8.0
                        actor.SetScale(new_scale, new_scale, new_scale)
                except Exception as _scale_err:
                    print(f"⚠️ Could not rescale grid label: {_scale_err}")

                try:
                    actor.GetProperty().SetColor(*color)
                except Exception:
                    pass
                actor._naksha_label_font_size = size

            actor.display_text = text
            actor.text_content = text

            # Keep the hover-highlight cache in sync so a later mouse-out
            # doesn't silently revert this change back to the pre-edit color.
            try:
                cached = getattr(self, 'original_colors', None)
                if cached is not None and actor in cached:
                    cached[actor]['color'] = tuple(color)
            except Exception:
                pass

            return True
        except Exception as e:
            print(f"⚠️ Could not apply grid label state: {e}")
            return False

    def _push_label_undo(self, entries):
        """Record a snapshot (list of _capture_label_state dicts) taken
        BEFORE an edit, so _undo_last_label_edit can restore it."""
        try:
            if not entries:
                return
            self._label_edit_undo_stack.append(entries)
            limit = getattr(self, '_label_edit_undo_limit', 20)
            while len(self._label_edit_undo_stack) > limit:
                self._label_edit_undo_stack.pop(0)
        except Exception:
            pass

    def _undo_last_label_edit(self):
        """Revert the most recent grid-label text/font-size/color edit
        (single or bulk). Safe to call with an empty stack."""
        try:
            if not self._label_edit_undo_stack:
                print("↶ No grid label edit to undo")
                return
            entries = self._label_edit_undo_stack.pop()
            restored = 0
            for entry in entries:
                actor = entry.get('actor')
                if actor is None:
                    continue
                ok = self._apply_label_state(
                    actor,
                    entry.get('text'),
                    entry.get('size'),
                    entry.get('color'),
                    entry.get('is_text_actor3d'),
                )
                if ok:
                    restored += 1
            self._force_render_after_label_edit()
            print(f"↶ Undid grid label edit ({restored}/{len(entries)} label(s) restored)")
        except Exception as e:
            print(f"⚠️ Undo grid label edit failed: {e}")
            import traceback
            traceback.print_exc()

    def _edit_grid_label(self, actor):
        """Open a text-edit dialog for a single grid/block-name label actor.

        Only the *rendered* text, font size and color are changed here.
        ``actor.grid_name`` is deliberately left untouched (see
        _apply_label_state's docstring).
        """
        try:
            from gui.digitize_tools import TextEditDialog
            try:
                from PySide6.QtWidgets import QDialog
            except ImportError:
                from PyQt5.QtWidgets import QDialog

            before = self._capture_label_state(actor)

            dialog = TextEditDialog(
                current_text=before['text'],
                current_size=before['size'],
                current_font="Arial",
                current_bold=True,
                current_italic=False,
                current_color=before['color'],
                parent=self.app,
            )
            try:
                result = dialog.exec()
            except AttributeError:
                result = dialog.exec_()

            if result != QDialog.Accepted:
                return

            values = dialog.get_values()
            new_text = values.get('text', before['text']) or before['text']
            raw_size = values.get('font_size', before['size'])
            new_size = int(before['size'] if raw_size is None else raw_size)
            new_color = values.get('color', before['color']) or before['color']

            ok = self._apply_label_state(
                actor, new_text, new_size, new_color, before['is_text_actor3d']
            )
            if ok:
                self._push_label_undo([before])
                self._force_render_after_label_edit()
                print(f"✅ Grid label text updated: '{new_text}'")
        except Exception as e:
            print(f"⚠️ Grid label edit failed: {e}")
            import traceback
            traceback.print_exc()

    def _iter_all_grid_label_actors(self):
        """Yield every live grid/block-name label actor currently tracked
        by the app (SNT + DXF stores), de-duplicated. Never raises."""
        seen = set()
        for store_name in ('snt_actors', 'dxf_actors'):
            try:
                for data in getattr(self.app, store_name, []) or []:
                    for actor in data.get('actors', []) or []:
                        if not (hasattr(actor, 'is_grid_label') and actor.is_grid_label):
                            continue
                        key = id(actor)
                        if key in seen:
                            continue
                        seen.add(key)
                        yield actor
            except Exception:
                continue

    def edit_all_grid_labels_font_size(self):
        """Bulk-apply a single font size to every block-name label in the
        scene at once. Text and color are left exactly as each label
        already has them — only size changes. Fully undoable in one step
        via _undo_last_label_edit."""
        try:
            from PySide6.QtWidgets import QInputDialog

            actors = list(self._iter_all_grid_label_actors())
            if not actors:
                QMessageBox.information(
                    self.app, "No Block Labels",
                    "No block-name labels are currently loaded."
                )
                return

            default_size = 75
            try:
                default_size = self._capture_label_state(actors[0])['size'] or 75
            except Exception:
                pass

            # Positional args here — PySide6 and PyQt5 disagree on the
            # keyword names for min/max (minValue/maxValue vs min/max), so
            # positional is the only form that works reliably on both.
            new_size, ok = QInputDialog.getInt(
                self.app,
                "Edit All Block Labels — Font Size",
                f"Font size (pt) for all {len(actors)} block label(s):",
                default_size,
                0,
                999,
            )
            if not ok:
                return

            before_entries = []
            updated = 0
            for actor in actors:
                try:
                    before = self._capture_label_state(actor)
                    applied = self._apply_label_state(
                        actor, before['text'], new_size, before['color'],
                        before['is_text_actor3d'],
                    )
                    if applied:
                        before_entries.append(before)
                        updated += 1
                except Exception as _one_err:
                    # One bad actor must never abort the whole batch.
                    print(f"⚠️ Skipped one block label during bulk font-size edit: {_one_err}")
                    continue

            if before_entries:
                self._push_label_undo(before_entries)
            self._force_render_after_label_edit()
            print(f"✅ Bulk font-size edit: {updated}/{len(actors)} block label(s) updated to {new_size}pt")
        except Exception as e:
            print(f"⚠️ Bulk grid label font-size edit failed: {e}")
            import traceback
            traceback.print_exc()

    def _force_render_after_label_edit(self):
        try:
            handles = self._get_main_interactor_handles()
            if handles is not None:
                _, interactor, _ = handles
                interactor.GetRenderWindow().Render()
        except Exception:
            pass

    def _block_polygon_hit_click(self, obj, clickPos, renderer) -> bool:
        """
        Global block-polygon priority check.

        If the click's world position falls inside ANY BL-layer block
        polygon, load that block directly via its already-resolved
        file_path and return True — before any grid-label picking or
        name-matching logic runs.

        This makes block data authoritative for any click inside a
        block, regardless of which label glyph happens to sit under the
        pixel. Some SNTs render two overlapping label conventions at
        the same block (e.g. an SNT-style 'DV..._000051.laz' name that
        has no physical-file match, stacked on top of the real
        'LAVARONE000002.laz' filename label). Picking whichever text
        actor the hardware picker happens to grab is non-deterministic,
        so relying on label-name matching after the fact is what caused
        clicks to intermittently fall through to the manual file picker.
        Testing the polygon first sidesteps that entirely.

        Returns False if no polygon contains the click point, so the
        caller can fall through to normal label / point-cloud handling.
        """
        from PySide6.QtWidgets import QMenu, QMessageBox
        from PySide6.QtGui import QAction, QCursor

        # Start with the normal SNT-derived block set.  This remains the
        # authority when the user is working from SNT only.
        block_polygons = list(getattr(self.app, 'snt_block_polygons', None) or [])
        if not block_polygons:
            block_polygons = list(self._ensure_snt_block_index())

        # If the user explicitly identified block(s) through PRJ Block Identifier,
        # those PRJ records become authoritative for SAME-NAME block loading.
        # This is intentionally narrower than a global PRJ override: SNT-only
        # workflows are unchanged, while an identified PRJ block always resolves
        # its LAZ/LAS from the currently loaded PRJ directory instead of an SNT
        # attachment directory from another location.
        prj_polygons = self._get_prj_block_polygons()
        if prj_polygons:
            prj_keys = {
                str(p.get("grid_name") or "").strip().casefold()
                for p in prj_polygons
                if str(p.get("grid_name") or "").strip()
            }

            snt_aliases = {}
            kept = []
            replaced = 0
            for blk in block_polygons:
                key = str(blk.get("grid_name") or "").strip().casefold()
                if key and key in prj_keys:
                    replaced += 1
                    aliases = snt_aliases.setdefault(key, [])
                    for name in [blk.get("grid_name"), blk.get("block_file"), *(blk.get("alt_names") or [])]:
                        name = str(name or "").strip()
                        if name and name not in aliases:
                            aliases.append(name)
                    continue
                kept.append(blk)

            authoritative_prj = []
            for p in prj_polygons:
                item = dict(p)
                key = str(item.get("grid_name") or "").strip().casefold()
                merged_alt = list(item.get("alt_names") or [])
                for name in snt_aliases.get(key, []):
                    if name and name != item.get("grid_name") and name not in merged_alt:
                        merged_alt.append(name)
                item["alt_names"] = merged_alt
                item["source"] = "prj_boundary"
                authoritative_prj.append(item)

            block_polygons = kept + authoritative_prj
            print(
                f"[block-click] PRJ authority active: {len(authoritative_prj)} "
                f"identified PRJ block(s), replaced {replaced} same-name "
                f"SNT block source(s)"
            )

        if not block_polygons:
            return False

        try:
            coord = vtk.vtkCoordinate()
            coord.SetCoordinateSystemToDisplay()
            coord.SetValue(float(clickPos[0]), float(clickPos[1]), 0.0)
            wx, wy, _ = coord.GetComputedWorldValue(renderer)
            print(f"[block-click] world XY = ({wx:.2f}, {wy:.2f}), "
                  f"testing {len(block_polygons)} BL polygons")

            def _pip(px, py, poly):
                n = len(poly)
                inside = False
                j = n - 1
                for i in range(n):
                    xi, yi = poly[i]
                    xj, yj = poly[j]
                    if ((yi > py) != (yj > py)) and (
                        px < (xj - xi) * (py - yi) / ((yj - yi) or 1e-12) + xi
                    ):
                        inside = not inside
                    j = i
                return inside

            hit_block = None
            all_hit_alt_names = []
            best_edge_dist = float('inf')
            for blk in block_polygons:
                pts = blk.get("points_2d", [])
                if len(pts) >= 3 and _pip(wx, wy, pts):
                    # NEAREST BOUNDARY: pick the polygon whose edge is closest
                    # to the click point. At corners where polygons overlap,
                    # the click is physically closer to the correct block's
                    # boundary because it sits at that block's corner edge.
                    min_edge_dist = float('inf')
                    n = len(pts)
                    for i in range(n):
                        x1, y1 = pts[i]
                        x2, y2 = pts[(i + 1) % n]
                        dx, dy = x2 - x1, y2 - y1
                        len_sq = dx * dx + dy * dy
                        if len_sq < 1e-12:
                            t = 0.0
                        else:
                            t = max(0.0, min(1.0, ((wx - x1) * dx + (wy - y1) * dy) / len_sq))
                        proj_x = x1 + t * dx
                        proj_y = y1 + t * dy
                        edge_dist = (wx - proj_x) ** 2 + (wy - proj_y) ** 2
                        if edge_dist < min_edge_dist:
                            min_edge_dist = edge_dist
                    edge_m = min_edge_dist ** 0.5
                    print(f"[block-click] polygon '{blk.get('grid_name')}': "
                          f"nearest_edge={edge_m:.1f}m")

                    if hit_block is None or min_edge_dist < best_edge_dist:
                        hit_block = blk
                        best_edge_dist = min_edge_dist
                    # Collect alt_names from ALL overlapping polygons
                    for an in blk.get("alt_names", []):
                        if an not in all_hit_alt_names:
                            all_hit_alt_names.append(an)
                    # Also add grid_names from other overlapping polygons as alternatives
                    other_gn = blk.get("grid_name")
                    if other_gn and other_gn != hit_block.get("grid_name"):
                        if other_gn not in all_hit_alt_names:
                            all_hit_alt_names.append(other_gn)

            if hit_block is None:
                print(f"[block-click] no BL polygon contains ({wx:.2f}, {wy:.2f})")
                return False

            print(f"[block-click] WINNER: '{hit_block.get('grid_name')}' "
                  f"(nearest edge={best_edge_dist**0.5:.1f}m)")

            grid_name = hit_block.get("grid_name")
            file_path = hit_block.get("file_path")
            snt_filename = hit_block.get("snt_filename")
            block_source = str(hit_block.get("source") or "").strip().lower()
            merged_alt = list(all_hit_alt_names)
            for an in (hit_block.get("alt_names") or []):
                if an not in merged_alt:
                    merged_alt.append(an)
            print(f"[block-click] HIT block from '{hit_block.get('snt_filename')}', "
                  f"grid_name='{grid_name}', file_path='{file_path}'")
            if merged_alt:
                print(f"[block-click] All candidate names: {[grid_name] + merged_alt}")

            if not (grid_name or file_path):
                return False

            menu = QMenu(self.app)
            menu.setStyleSheet("""
                QMenu {
                    background-color: #2c2c2c;
                    color: #f0f0f0;
                    border: 1px solid #555;
                    padding: 5px;
                }
                QMenu::item {
                    padding: 8px 30px;
                    border-radius: 3px;
                }
                QMenu::item:selected {
                    background-color: #3c3c3c;
                }
            """)
            load_action = QAction("📂 Load Block Data", self.app)
            clear_action = QAction("🧹 Clear Grid Data", self.app)

            is_loaded = grid_name in self.loaded_grids
            if is_loaded:
                load_action.setText("📂 Reload Block Data")
                point_count = len(self.loaded_grids[grid_name])
                clear_action.setText(f"🧹 Clear Grid ({point_count:,} pts)")
            else:
                clear_action.setEnabled(False)
                clear_action.setText("🧹 (Grid not loaded)")

            def _load_block_file(
                _=False,
                gn=grid_name,
                fp=file_path,
                an=None,
                source=block_source,
                owner_ref=snt_filename,
            ):
                if an is None:
                    an = merged_alt

                if fp and Path(fp).exists():
                    print(f"[block-click] Loading authoritative file: {fp}")
                    self._load_las_file(Path(fp), gn or Path(fp).stem)
                    return

                # An explicitly identified PRJ block must never silently fall
                # back to a loaded SNT folder.  If its file is absent from the
                # PRJ directory tree, keep the lookup scoped to the PRJ root and
                # let the user choose there (or cancel).
                if source == "prj_boundary":
                    prj_dir = None
                    try:
                        if owner_ref:
                            owner_path = Path(str(owner_ref))
                            prj_dir = owner_path.parent if owner_path.suffix.lower() == ".prj" else owner_path
                    except Exception:
                        prj_dir = None

                    if prj_dir is None:
                        prj_dialog = getattr(self.app, "block_identifier_dialog", None)
                        current_prj = getattr(prj_dialog, "current_prj_path", None) if prj_dialog else None
                        if current_prj:
                            prj_dir = Path(str(current_prj)).parent

                    print(
                        f"[block-click] PRJ block '{gn}' has no resolved file; "
                        f"SNT fallback disabled; PRJ root={prj_dir}"
                    )
                    self._show_file_selection_dialog(prj_dir, gn)
                    return

                # SNT-only workflow: preserve the existing owner-aware search.
                self.load_grid_las(gn, snt_filename=owner_ref, alt_names=an)

            load_action.triggered.connect(_load_block_file)

            def _confirm_clear_block(_checked=False, gn=grid_name):
                if not gn:
                    return
                reply = QMessageBox.question(
                    self.app, "Confirm Clear",
                    f"Clear all points from grid:\n\n{gn}\n\nContinue?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No
                )
                if reply == QMessageBox.Yes:
                    self.clear_grid_data(gn)

            clear_action.triggered.connect(_confirm_clear_block)
            fence_action = QAction("🔺 Load Points Inside Fence", self.app)
            fence_action.triggered.connect(self.activate_load_by_fence_tool)
            buffer_action = QAction("🧩 Open Block with Buffer Points", self.app)
            buffer_action.triggered.connect(
                lambda _=False, gn=grid_name, sf=snt_filename, an=tuple(merged_alt), fp=file_path:
                    self.open_grid_with_buffer_points(gn, sf, list(an), fp)
            )
            menu.addAction(load_action)
            menu.addAction(buffer_action)
            menu.addAction(clear_action)
            menu.addAction(fence_action)

            self._consume_vtk_event(obj)
            print(
                f"[block-click] showing block context menu for "
                f"'{grid_name}' (loaded={is_loaded})"
            )
            # Use a NON-MODAL popup, never menu.exec(). exec() runs its own
            # modal event loop from inside a VTK right-click observer, which
            # either closes instantly on the in-progress button-release OR
            # deadlocks the GUI thread (the "hang" you saw). popup() returns
            # immediately and the menu only dismisses on a *subsequent* outside
            # press / action pick, so it survives the invoking right-click.
            # A short QTimer defer lets the VTK right-click event chain
            # (press + release) finish first — mirrors digitize_tools.
            # _show_text_context_menu's _deferred_menu pattern. The reference
            # is kept on self so the menu isn't garbage-collected before the
            # user interacts with it.
            self._block_context_menu = menu
            try:
                menu.aboutToHide.connect(
                    lambda: setattr(self, "_block_context_menu", None)
                )
            except Exception:
                pass
            # Defer to the next event-loop turn (delay 0). This runs the popup
            # AFTER the full synchronous right-click handling completes — both
            # our handler AND digitize's observer (_force_render / interactor
            # state reset) — so VTK's handling can't interfere with the menu
            # (avoids the synchronous "glitch"). 0ms means it shows on the very
            # next loop turn, i.e. instantly from the user's perspective (the
            # 10ms value felt laggy; a longer block would hang, as exec() did).
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: menu.popup(QCursor.pos()))
            return True
        except Exception as _block_ex:
            print(f"[block-click] ERROR during block polygon hit test: {_block_ex}")
            import traceback
            traceback.print_exc()
            return False

    def on_right_click(self, obj, event):
        """Handle right-click on grid label OR point cloud"""
        from PySide6.QtWidgets import QMenu, QInputDialog
        from PySide6.QtGui import QAction, QCursor

        if getattr(self.app, "_shutdown_in_progress", False):
            return 0

        # Move Vertex owns right-click while it is active.
        # Right-click must be silent/idle here:
        # no SNT/Grid menu, no block-click, no load grid data.
        # After Move Vertex finishes and active_tool becomes None,
        # normal right-click will work again automatically.
        try:
            digitizer = getattr(self.app, "digitizer", None)
            if digitizer is not None:
                active_digitizer_tool = str(
                    getattr(digitizer, "active_tool", "") or ""
                ).lower().strip()

                if (
                    active_digitizer_tool == "movevertex"
                    or getattr(digitizer, "moving_vertex_data", None) is not None
                ):
                    self._consume_vtk_event(obj)
                    return 1
        except Exception:
            pass

        handles = self._get_main_interactor_handles()
        if handles is None:
            return 0
        _, interactor, renderer = handles

        # ✅ FIXED: If a tool operation is in progress (drawing, measuring, etc.),
        # let the tool handle the right-click to finalize its operation.
        # This prevents the 'Shading controls' dialog from appearing prematurely.
        if self._is_tool_operation_in_progress():
            return 0  # Allow other observers (the tools) to handle it

        # ── TEXT DRAWING PRIORITY ──────────────────────────────────────────────
        # If the cursor is over a placed text label, give the digitizer's own
        # text context menu (Edit / Move / Copy / Delete) first priority.
        # Only if the cursor is NOT over any text drawing do we fall through to
        # the normal grid / point-cloud context menu below.
        clickPos_check = interactor.GetEventPosition()
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is not None and getattr(digitizer, "enabled", False):
            try:
                x_c, y_c = clickPos_check
                text_drawing = None

                # Hardware pick — fast, works for vtkTextActor3D
                hw_picker = vtk.vtkPropPicker()
                hw_picker.Pick(x_c, y_c, 0, renderer)
                picked_actor = hw_picker.GetActor()
                if picked_actor is not None:
                    for d in getattr(digitizer, "drawings", []):
                        if d.get("type") == "text" and d.get("actor") is picked_actor:
                            text_drawing = d
                            break

                # Math fallback — checks bounding-box / cursor proximity
                if text_drawing is None and hasattr(digitizer, "_get_drawing_under_cursor"):
                    candidate = digitizer._get_drawing_under_cursor(x_c, y_c, tolerance=25.0)
                    if candidate is not None and candidate.get("type") == "text":
                        text_drawing = candidate

                if text_drawing is not None:
                    # Hand off entirely to the digitizer — do NOT show grid menu.
                    self._consume_vtk_event(obj)
                    digitizer.clear_coordinate_labels()
                    digitizer._unhighlight_all_lines()
                    digitizer.multi_selected = []
                    digitizer.selected_drawing = text_drawing
                    digitizer._show_text_context_menu(text_drawing)
                    digitizer._force_render()
                    return 1
            except Exception:
                pass  # Never block the normal flow on any error here
        # ── END TEXT DRAWING PRIORITY ──────────────────────────────────────────

        current_mode = str(
            getattr(self.app, "display_mode", "")
            or getattr(self.app, "current_display_mode", "")
            or ""
        ).lower()

        clickPos = interactor.GetEventPosition()
        click_x, click_y = clickPos

        if current_mode == "surface":
            try:
                surface_actor = getattr(self.app, "_surface_mesh_actor", None)
                hit_surface = False
                hit_tester = getattr(self.app, "_main_view_pick_hits_actor", None)
                if callable(hit_tester) and surface_actor is not None:
                    hit_surface = bool(hit_tester(surface_actor, display_x=click_x, display_y=click_y))

                if hit_surface:
                    from gui.surface_mode import show_surface_controls
                    opened = bool(show_surface_controls(self.app))
                else:
                    opened = False

                if opened:
                    self._consume_vtk_event(obj)
                    return 1
            except Exception as _surface_menu_err:
                print(f"⚠️ Surface right-click priority skipped: {_surface_menu_err}")

        if current_mode in ("shaded_class", "shading", "shadingmode"):
            try:
                can_show_shading = getattr(self.app, "_can_show_shading_controls", None)

                if callable(can_show_shading) and can_show_shading():
                    shaded_actor = getattr(self.app, "_shaded_mesh_actor", None)
                    hit_shading = False
                    hit_tester = getattr(self.app, "_main_view_pick_hits_actor", None)
                    if callable(hit_tester) and shaded_actor is not None:
                        hit_shading = bool(hit_tester(shaded_actor, display_x=click_x, display_y=click_y))

                    opened = False
                    if hit_shading and hasattr(self.app, "show_shading_controls"):
                        opened = bool(self.app.show_shading_controls())

                    if opened:
                        self._consume_vtk_event(obj)
                        return 1
            except Exception as _shading_menu_err:
                print(f"⚠️ Shading right-click priority skipped: {_shading_menu_err}")

        # ═══════════════════════════════════════════════════════
        # STEP 0: Block polygon hit-test — GLOBAL PRIORITY.
        # Any click whose world position lands inside a BL block
        # polygon loads that block directly via its resolved
        # file_path, before any label picking/name-matching runs.
        # This is what guarantees block data wins even when the
        # click happens to land on a label glyph belonging to a
        # different naming convention (e.g. SNT-style
        # 'DV..._000051.laz' vs the physical 'LAVARONE000002.laz'
        # file) — that ambiguity previously fell through to
        # name-matching and failed, forcing the manual file picker.
        # ═══════════════════════════════════════════════════════
        if self._block_polygon_hit_click(obj, clickPos, renderer):
            return 1

        # ═══════════════════════════════════════════════════════
        # STEP 1: Try to find grid label first
        # ═══════════════════════════════════════════════════════
        picker = vtk.vtkPropPicker()
        picker.Pick(clickPos[0], clickPos[1], 0, renderer)
        actor = picker.GetActor()
        
        if not actor or not (hasattr(actor, 'is_grid_label') and actor.is_grid_label):
            # Try area picker with 10-pixel radius
            area_picker = vtk.vtkAreaPicker()
            x, y = clickPos
            area_picker.AreaPick(x-10, y-10, x+10, y+10, renderer)
            
            for prop in area_picker.GetProp3Ds():
                if hasattr(prop, 'is_grid_label') and prop.is_grid_label:
                    actor = prop
                    break

        # Fallback: pick the nearest visible grid label by screen distance.
        # Some text actors are not returned by vtkPropPicker/AreaPicker reliably,
        # especially when a block boundary or line sits on top of the label.
        if not actor or not (hasattr(actor, 'is_grid_label') and actor.is_grid_label):
            try:
                best_actor = None
                best_dist2 = None
                for dxf_data in getattr(self.app, 'dxf_actors', []) or []:
                    for prop in dxf_data.get('actors', []):
                        if not (hasattr(prop, 'is_grid_label') and prop.is_grid_label):
                            continue
                        try:
                            wx, wy, wz = prop.GetPosition()
                            coord = vtk.vtkCoordinate()
                            coord.SetCoordinateSystemToWorld()
                            coord.SetValue(float(wx), float(wy), float(wz))
                            sx, sy, _ = coord.GetComputedDisplayValue(renderer)
                            dx = float(sx) - float(clickPos[0])
                            dy = float(sy) - float(clickPos[1])
                            dist2 = dx * dx + dy * dy
                            if dist2 <= 400.0 and (best_dist2 is None or dist2 < best_dist2):
                                best_actor = prop
                                best_dist2 = dist2
                        except Exception:
                            pass
                if best_actor is not None:
                    actor = best_actor
            except Exception:
                pass

        # NOTE: block-polygon priority is now handled globally in STEP 0
        # (_block_polygon_hit_click), which runs before label picking even
        # starts. If execution reaches here, STEP 0 already confirmed the
        # click is NOT inside any block polygon, so treating this actor as
        # a plain grid label is safe.

        # ═══════════════════════════════════════════════════════
        # CASE A: Found grid label - show label menu
        # ═══════════════════════════════════════════════════════
        if actor and hasattr(actor, 'is_grid_label') and actor.is_grid_label:
            grid_name = getattr(actor, 'grid_name', '')
            snt_filename = (
                getattr(actor, "_naksha_snt_filename", None)
                or getattr(actor, "snt_filename", None)
                or self._infer_snt_filename_for_grid(grid_name)
            )
            
            if grid_name:
                menu = QMenu(self.app)
                menu.setStyleSheet("""
                    QMenu {
                        background-color: #2c2c2c;
                        color: #f0f0f0;
                        border: 1px solid #555;
                        padding: 5px;
                    }
                    QMenu::item {
                        padding: 8px 30px;
                        border-radius: 3px;
                    }
                    QMenu::item:selected {
                        background-color: #3c3c3c;
                    }
                """)
                
                load_action = QAction("📂 Load Grid Data", self.app)
                clear_action = QAction("🧹 Clear Grid Data", self.app)
                
                def confirm_clear():
                    reply = QMessageBox.question(
                        self.app,
                        "Confirm Clear",
                        f"Clear all points from grid:\n\n{grid_name}\n\n"
                        f"Points will be removed from view.\n\nContinue?",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.No
                    )
                    if reply == QMessageBox.Yes:
                        self.clear_grid_data(grid_name)

                clear_action.triggered.connect(confirm_clear)
                
                owner_label = self._resolve_grid_owner(grid_name)
                is_loaded = grid_name in self.loaded_grids
                
                if is_loaded:
                    load_action.setText("📂 Reload Grid Data")
                    point_count = len(self.loaded_grids[grid_name])
                    clear_action.setText(f"🧹 Clear Grid ({point_count:,} pts)")
                else:
                    clear_action.setEnabled(False)
                    clear_action.setText("🧹 (Grid not loaded)")

                load_action.triggered.connect(
                    lambda _=False, gn=grid_name, sf=snt_filename: self.load_grid_las(
                        gn,
                        snt_filename=sf,
                        alt_names=self._get_alt_names_for_grid(gn),
                    )
                )
                clear_action.triggered.connect(lambda: self.clear_grid_data(grid_name))
                fence_action = QAction("🔺 Load Points Inside Fence", self.app)
                fence_action.triggered.connect(self.activate_load_by_fence_tool)
                buffer_action = QAction("🧩 Open Block with Buffer Points", self.app)
                buffer_action.triggered.connect(
                    lambda _=False, gn=grid_name, sf=snt_filename:
                        self.open_grid_with_buffer_points(
                            gn, sf, self._get_alt_names_for_grid(gn)
                        )
                )

                from PySide6.QtCore import QTimer
                edit_label_action = QAction("✏️ Edit Label Text", self.app)
                edit_label_action.triggered.connect(
                    lambda _=False, a=actor: QTimer.singleShot(
                        10, lambda a=a: self._edit_grid_label(a)
                    )
                )

                bulk_font_action = QAction("🔠 Set Font Size — All Block Labels", self.app)
                bulk_font_action.triggered.connect(
                    lambda _=False: QTimer.singleShot(10, self.edit_all_grid_labels_font_size)
                )

                menu.addAction(load_action)
                menu.addAction(buffer_action)
                menu.addAction(clear_action)
                menu.addAction(fence_action)
                menu.addSeparator()
                menu.addAction(edit_label_action)
                menu.addAction(bulk_font_action)

                if getattr(self, '_label_edit_undo_stack', None):
                    undo_label_action = QAction("↶ Undo Last Label Edit", self.app)
                    undo_label_action.triggered.connect(
                        lambda _=False: QTimer.singleShot(10, self._undo_last_label_edit)
                    )
                    menu.addAction(undo_label_action)

                menu.exec(QCursor.pos())
                return

        # ═══════════════════════════════════════════════════════
        # CASE B: No label found - try picking point cloud
        # ═══════════════════════════════════════════════════════
        if not hasattr(self.app, 'data') or self.app.data is None:
            return  # No data loaded
        
        point_picker = vtk.vtkPointPicker()
        point_picker.Pick(clickPos[0], clickPos[1], 0, renderer)
        point_id = point_picker.GetPointId()
        
        if point_id < 0:
            return  # No point picked
        
        # Find which grid this point belongs to
        grid_name = self._find_grid_for_point(point_id)
        
        # Create context menu
        menu = QMenu(self.app)
        menu.setStyleSheet("""
            QMenu {
                background-color: #2c2c2c;
                color: #f0f0f0;
                border: 1px solid #555;
                padding: 5px;
            }
            QMenu::item {
                padding: 8px 30px;
                border-radius: 3px;
            }
            QMenu::item:selected {
                background-color: #3c3c3c;
            }
        """)
        
        #     # Point belongs to a tracked grid
        #     clear_action.triggered.connect(lambda: self.clear_grid_data(grid_name))
        #     menu.addAction(clear_action)

        menu.exec(QCursor.pos())
        
    def _find_grid_for_point(self, point_id):
        """Find which grid a point belongs to"""
        for grid_name, indices in self.loaded_grids.items():
            if point_id in indices:
                return grid_name
        return None

    def _resolve_grid_owner(self, grid_name):
        """Map clicked grid label to actual loaded owner label when available."""
        if not grid_name:
            return grid_name
        return self.grid_aliases.get(grid_name, grid_name)

    def _get_alt_names_for_grid(self, grid_name):
        """Get alternative names stored in BL polygons for a given grid_name."""
        if not grid_name:
            return []
        alt = []
        for blk in (getattr(self.app, 'snt_block_polygons', None) or []):
            if blk.get("grid_name") == grid_name:
                alt.extend(blk.get("alt_names", []))
        return alt

    def _delete_area_by_point(self, point_id):
        """Delete area around clicked point by selecting nearby points"""
        from PySide6.QtWidgets import QInputDialog, QMessageBox
        import numpy as np
        
        # Ask for grid name
        grid_name, ok = QInputDialog.getText(
            self.app,
            "Define Grid Area",
            f"Clicked point: {point_id}\n\n"
            f"Enter grid name to delete:"
        )
        
        if not ok or not grid_name:
            return
        
        # Ask for radius
        radius, ok = QInputDialog.getDouble(
            self.app,
            "Selection Radius",
            "Select points within radius (meters):",
            10.0,  # default
            1.0,   # min
            100.0, # max
            1      # decimals
        )
        
        if not ok:
            return
        
        # Find nearby points
        point_xyz = self.app.data['xyz'][point_id]
        all_xyz = self.app.data['xyz']
        
        distances = np.linalg.norm(all_xyz - point_xyz, axis=1)
        nearby_indices = np.where(distances <= radius)[0]
        
        if len(nearby_indices) == 0:
            QMessageBox.warning(self.app, "No Points", "No points found in selection area")
            return
        
        # Track and delete
        self.loaded_grids[grid_name] = nearby_indices
        
        reply = QMessageBox.question(
            self.app,
            "Confirm Selection",
            f"Selected {len(nearby_indices):,} points\n"
            f"within {radius}m radius\n\n"
            f"Delete grid '{grid_name}'?",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            self.delete_grid_data(grid_name)
    
    def load_grid_las(self, grid_name, snt_filename=None, alt_names=None):
        """
        Main entry point: Load LAZ/LAS file for clicked grid.
        Uses owner-aware detection before falling back to the existing DXF path.
        """
        print(f"\n{'='*60}")
        print(f"📂 LOADING POINT CLOUD FOR GRID: {grid_name}")
        if snt_filename:
            print(f"   SNT owner: {snt_filename}")
        if alt_names:
            print(f"   Alt names: {alt_names}")
        print(f"{'='*60}\n")

        las_folder = None
        if snt_filename:
            las_folder = self._find_las_folder_from_snt(snt_filename)

        if not las_folder:
            inferred_snt = self._infer_snt_filename_for_grid(grid_name)
            if inferred_snt and inferred_snt != snt_filename:
                print(f"   Inferred SNT owner: {inferred_snt}")
                snt_filename = inferred_snt
                las_folder = self._find_las_folder_from_snt(inferred_snt)

        if not las_folder:
            las_folder = self._find_las_folder_from_dxf()

        if not las_folder:
            las_folder = self._prompt_user_for_las_folder()

        if not las_folder:
            QMessageBox.warning(
                self.app,
                "Folder Not Found",
                "Could not locate LAZ/LAS folder.\n\n"
                "Please ensure LAZ/LAS files are in:\n"
                "- Same folder as the owning SNT/DXF\n"
                "- 'lazz' subfolder\n"
                "- 'laz' subfolder\n"
                "- 'las' subfolder"
            )
            return

        print(f"   LAZ/LAS folder used: {las_folder}")

        # Collect ALL candidate names: primary + direct aliases + block-file labels.
        # The safe matcher already rejects ambiguous or incorrect identities, so
        # we can try every block-local alias instead of filtering too early.
        all_candidates = []
        if grid_name:
            all_candidates.append(grid_name)
        block_file = ""
        for blk in (getattr(self.app, 'snt_block_polygons', None) or []):
            if blk.get("grid_name") == grid_name:
                block_file = str(blk.get("block_file") or "").strip()
                if block_file:
                    break
        for n in (block_file, *(alt_names or [])):
            if n and n not in all_candidates:
                all_candidates.append(n)

        # Also gather DXF grid label names inside any BL polygon for this area
        for blk in (getattr(self.app, 'snt_block_polygons', None) or []):
            if blk.get("grid_name") == grid_name:
                for an in blk.get("alt_names", []):
                    if an and an not in all_candidates:
                        all_candidates.append(an)

        # Try each candidate against the folder's files
        for candidate in all_candidates:
            las_file = self._find_matching_las_file(las_folder, candidate)
            if las_file:
                if candidate != grid_name:
                    print(f"   ✅ Matched via alternative name '{candidate}'")
                self._load_las_file(las_file, grid_name)
                return

        # STRATEGY: targeted recursive search. A project directory can
        # contain many arbitrarily-named per-batch subfolders, each
        # holding a different set of block files — las_folder above is
        # just the FIRST folder found containing any LAZ/LAS files, which
        # may not be the one holding THIS block. Search the whole tree
        # under the owner directory for an exact filename match before
        # falling back to manual selection.
        search_root = None
        if snt_filename:
            try:
                p = Path(str(snt_filename))
                search_root = p if p.is_dir() else p.parent
            except Exception:
                search_root = None
        if (not search_root or not search_root.exists()) and las_folder:
            search_root = Path(las_folder).parent

        if search_root:
            las_file = self._find_las_file_recursive(search_root, all_candidates)
            if las_file:
                print(f"   ✅ Found '{las_file.name}' via recursive search under {search_root}")
                self._load_las_file(las_file, grid_name)
                return

        print(
            f"\n   ❌ No exact file identity found for '{grid_name}'. "
            "Opening the owning folder for manual selection."
        )
        self._show_file_selection_dialog(las_folder, grid_name)
    
    def _find_las_folder_from_dxf(self):
        """
        ✅ UPDATED: Use full_path from dxf_actors
        """
        print("📋 STRATEGY 1: Auto-detect from DXF location")
        
        if not hasattr(self.app, 'dxf_actors') or not self.app.dxf_actors:
            print("   ❌ No DXF files loaded")
            return None
        
        for dxf_data in self.app.dxf_actors:
            # ✅ Use full_path instead of filename
            full_path = dxf_data.get('full_path')
            
            if not full_path:
                # Fallback to filename (old code compatibility)
                filename = dxf_data.get('filename', '')
                if filename:
                    dxf_path = Path(filename)
                else:
                    continue
            else:
                dxf_path = Path(full_path)
            
            print(f"   DXF file: {dxf_path.name}")
            print(f"   DXF folder: {dxf_path.parent}")
            print(f"   Path exists: {dxf_path.exists()}")
            
            if not dxf_path.exists():
                print(f"   ⚠️ Path doesn't exist: {dxf_path}")
                continue
            
            dxf_folder = dxf_path.parent
            
            # Check cache
            if str(dxf_folder) in self.folder_cache:
                cached = self.folder_cache[str(dxf_folder)]
                print(f"   ✅ Using cached folder: {cached}")
                return cached
            
            # Same folder as DXF
            las_files = list(dxf_folder.glob("*.laz")) + list(dxf_folder.glob("*.las"))
            if las_files:
                print(f"   ✅ FOUND {len(las_files)} LAZ/LAS files in DXF folder")
                self.folder_cache[str(dxf_folder)] = dxf_folder
                self._save_folder_to_settings(dxf_folder)
                return dxf_folder
            
            # Check subfolders
            for subfolder_name in ['lazz', 'LAZZ', 'laz', 'LAZ', 'las', 'LAS']:
                subfolder = dxf_folder / subfolder_name
                
                if subfolder.exists() and subfolder.is_dir():
                    las_files = list(subfolder.glob("*.laz")) + list(subfolder.glob("*.las"))
                    
                    if las_files:
                        print(f"   ✅ FOUND {len(las_files)} files in '{subfolder_name}' subfolder")
                        self.folder_cache[str(dxf_folder)] = subfolder
                        self._save_folder_to_settings(subfolder)
                        return subfolder

            # Also check ALL immediate subfolders
            try:
                for entry in dxf_folder.iterdir():
                    if entry.is_dir():
                        las_files = list(entry.glob("*.laz")) + list(entry.glob("*.las"))
                        if las_files:
                            print(f"   ✅ FOUND {len(las_files)} files in '{entry.name}' subfolder")
                            self.folder_cache[str(dxf_folder)] = entry
                            self._save_folder_to_settings(entry)
                            return entry
            except Exception:
                pass
        
        return None
        
    def _prompt_user_for_las_folder(self):
        """
        Strategy 2: Ask user to select LAZ/LAS folder
        Only happens once - cached for future use
        """
        print("\n📋 STRATEGY 2: Prompt user for folder")
        
        reply = QMessageBox.question(
            self.app,
            "Select LAZ/LAS Folder",
            "Could not auto-detect LAZ/LAS folder.\n\n"
            "Would you like to select the folder manually?\n\n"
            "(This will be remembered for future clicks)",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes
        )
        
        if reply == QMessageBox.No:
            return None
        
        folder = QFileDialog.getExistingDirectory(
            self.app,
            "Select LAZ/LAS Folder",
            "",
            QFileDialog.ShowDirsOnly
        )
        
        if folder:
            folder_path = Path(folder)
            
            # Verify it contains LAZ/LAS files
            las_files = list(folder_path.glob("*.laz")) + list(folder_path.glob("*.las"))
            
            if not las_files:
                QMessageBox.warning(
                    self.app,
                    "No Files Found",
                    f"Selected folder contains no LAZ/LAS files:\n{folder}"
                )
                return None
            
            print(f"   ✅ User selected: {folder_path} ({len(las_files)} files)")
            self._save_folder_to_settings(folder_path)
            return folder_path
        
        return None
    
    def _find_matching_las_file(self, las_folder, grid_name):
        """
        Find the unique LAZ/LAS file with the same complete grid identity.

        A partial coordinate (for example, matching only the northing) is never
        accepted. Missing or ambiguous matches return None so the caller opens
        the owning folder instead of loading a neighbouring grid.
        """
        print(f"\n🔍 Searching for file matching: {grid_name}")
        
        las_folder_path = Path(las_folder)
        
        # Get all LAZ/LAS files
        all_files = list(las_folder_path.glob("*.laz")) + list(las_folder_path.glob("*.las"))
        
        print(f"   Total files in folder: {len(all_files)}")
        
        if not all_files:
            return None

        grid_name_clean = strip_lidar_extension(grid_name)
        best = find_matching_lidar_file(all_files, grid_name_clean)

        if best is None:
            print(
                f"   ❌ No unique full-identity match for '{grid_name_clean}' "
                f"(norm='{normalized_stem(grid_name_clean)}', "
                f"numbers={numeric_identity(grid_name_clean)})"
            )
            print(f"   📄 Available files:")
            for fp in all_files[:10]:
                print(
                    f"      - {fp.name} "
                    f"(norm='{normalized_stem(fp.stem)}', "
                    f"numbers={numeric_identity(fp.stem)})"
                )
            return None

        print(f"   ✅ EXACT GRID MATCH FOUND: {best.name}")
        return best
    
    # def _extract_patterns(self, grid_name):
    #     """
    #     Extract search patterns from grid name
        
    #     Example: "DW3032726_000005" produces:
    #     - DW3032726_000005 (exact)
    #     - DW3032726_5 (without leading zeros)
    #     - 000005 (just number)
    #     - 5 (number without zeros)
    #     - DW3032726 (base prefix)
    #     """
    #     patterns = [grid_name]  # Always try exact match first
        
    #     # Split by underscore or space
    #     parts = grid_name.replace(' ', '_').split('_')
        
    #     for part in parts:
    #         if part:
    #             patterns.append(part)
                
    #             # Try removing leading zeros
    #             if part.isdigit():
    #                 patterns.append(str(int(part)))
        
    #     # Try base prefix (before first underscore/space)
    #     if '_' in grid_name:
    #         base = grid_name.split('_')[0]
    #         if base:
    #             patterns.append(base)
        
    #     # Remove duplicates while preserving order
    #     seen = set()
    #     unique_patterns = []
    #     for p in patterns:
    #         if p not in seen:
    #             seen.add(p)
    #             unique_patterns.append(p)
        
    #     return unique_patterns

    def _extract_patterns(self, grid_name):
        patterns = [grid_name]  # Always try exact match first

        # Split by underscore or space
        parts = grid_name.replace(' ', '_').split('_')

        numeric_parts = []
        non_numeric_parts = []

        for part in parts:
            if part:
                if part.isdigit():
                    numeric_parts.append(part)
                    numeric_parts.append(str(int(part)))  # strip leading zeros variant
                else:
                    non_numeric_parts.append(part)

        # ✅ Numeric suffixes FIRST — most specific (e.g. '000021', '21')
        patterns.extend(numeric_parts)

        # ✅ Non-numeric / base prefix LAST — too broad, matches everything
        patterns.extend(non_numeric_parts)

        # Try base prefix (before first underscore/space) — also last
        if '_' in grid_name:
            base = grid_name.split('_')[0]
            if base:
                patterns.append(base)

        # Remove duplicates while preserving order
        seen = set()
        unique_patterns = []
        for p in patterns:
            if p not in seen:
                seen.add(p)
                unique_patterns.append(p)

        return unique_patterns    
    
    def _load_las_file(self, las_file, grid_name):
        """
        Load LAZ/LAS file using the EXACT same path as menu bar loading.
        ✅ Mirrors open_file() logic exactly to ensure consistent behavior
        """
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtWidgets import QMessageBox
        from gui.progress_dialog import LoadingProgressDialog
        import os
        import time
        import numpy as np

        print(f"\n{'='*70}")
        print(f"📂 GRID LOAD - USING MENU BAR PATH")
        print(f"   Grid: {grid_name}")
        print(f"   File: {las_file.name}")
        print(f"{'='*70}")

        def _reset_interaction_state():
            """Clear stale zoom/pan state before or after a grid swap."""
            try:
                cancel_zoom = getattr(self.app, "_cancel_smooth_zoom_for_pan", None)
                if callable(cancel_zoom):
                    cancel_zoom()
            except Exception:
                pass

            try:
                digitizer = getattr(self.app, "digitizer", None)
                if digitizer is not None:
                    if hasattr(digitizer, "_reset_pan_state"):
                        digitizer._reset_pan_state()
                    if hasattr(digitizer, "_reset_stale_zoom_mouse_state"):
                        digitizer._reset_stale_zoom_mouse_state()
            except Exception as e:
                print(f"⚠️ Interaction reset skipped: {e}")

            try:
                ztool = getattr(self.app, "zoom_rectangle_tool", None)
                if ztool is not None:
                    ztool.is_dragging_zoom = False
                    ztool.start_pos = None
                    ztool.end_pos = None
                    ztool.is_panning = False
                    ztool.last_pan_pos = None
                    if hasattr(ztool, "_block_middle_until"):
                        ztool._block_middle_until = 0.0
            except Exception:
                pass

        # ============================================================================
        # STEP 1: AUTO-SAVE CURRENT FILE (if exists) - SAME AS MENU BAR
        # ============================================================================
        _reset_interaction_state()
        if hasattr(self.app, 'data') and self.app.data is not None:
            save_path = getattr(self.app, 'last_save_path', None) or getattr(self.app, 'loaded_file', None)

            # Only save if there are points (prevent saving empty cleared state)
            current_point_count = len(self.app.data.get('xyz', [])) if self.app.data else 0

            # Never overwrite the source file when the current load is class-filtered
            # (partial data). Writing filtered points back would corrupt the original.
            _class_filtered = getattr(self.app, '_loaded_with_class_filter', False)

            if save_path and current_point_count > 0 and not _class_filtered:
                try:
                    print(f"\n💾 AUTO-SAVING CURRENT FILE")
                    print(f"   Path: {os.path.basename(save_path)}")
                    print(f"   Points: {current_point_count:,}")
                    
                    from gui.save_pointcloud import save_pointcloud_quick
                    result = save_pointcloud_quick(self.app, save_path)
                    
                    if result:
                        print(f"✅ Saved successfully")
                        if hasattr(self.app, "statusBar"):
                            self.app.statusBar().showMessage(f"💾 Saved: {os.path.basename(save_path)}", 2000)
                            QCoreApplication.processEvents()
                    else:
                        print(f"⚠️ Save returned False")
                        
                except Exception as e:
                    print(f"❌ SAVE FAILED: {e}")
                    pass

        # ============================================================================
        # STEP 2: CLEAR GRID TRACKING
        # ============================================================================
        try:
            # Persist current file-specific display/PTC state before data clear.
            from gui.clear_project import _save_display_settings_before_clear
            _save_display_settings_before_clear(self.app)
            print("[RUNTIME-CHECK] pre-clear-save caller=grid_label_system step=grid-switch")
        except Exception as e:
            print(f"⚠️ Display settings pre-save skipped: {e}")

        # Stop queued debounced refresh callbacks from previous dataset.
        try:
            timer = getattr(self.app, "_update_debounce_timer", None)
            if timer is not None and timer.isActive():
                timer.stop()
            pending = getattr(self.app, "_pending_view_updates", None)
            if hasattr(pending, "clear"):
                pending.clear()
            if hasattr(self.app, "_last_changed_mask"):
                self.app._last_changed_mask = None
            if hasattr(self.app, "_last_changed_indices"):
                self.app._last_changed_indices = None
        except Exception:
            pass

        # Align grid-switch cleanup with open-file cleanup hooks.
        try:
            from gui.memory_manager import ObserverRegistry, release_data_arrays
            from gui.unified_actor_manager import reset_uam
            release_data_arrays(self.app)
            ObserverRegistry.release_all()
            reset_uam(self.app)
            mem_guard = getattr(self.app, "_mem_guard", None)
            if mem_guard is not None:
                mem_guard.force_gc()
        except Exception as mem_exc:
            print(f"⚠️ Memory manager clear hook skipped: {mem_exc}")

        self.loaded_grids.clear()
        self.grid_aliases.clear()

       # ============================================================================
        # SAVE CAMERA STATE BEFORE CLEARING
        # ============================================================================
        saved_camera_state = None
        if hasattr(self.app, "vtk_widget") and self.app.vtk_widget:
            try:
                camera = self.app.vtk_widget.renderer.GetActiveCamera()
                saved_camera_state = {
                    'position': camera.GetPosition(),
                    'focal_point': camera.GetFocalPoint(),
                    'view_up': camera.GetViewUp(),
                    'parallel_scale': camera.GetParallelScale(),
                    'parallel_projection': camera.GetParallelProjection()
                }
                print(f"💾 Camera state saved (zoom: {saved_camera_state['parallel_scale']:.2f})")
            except Exception as e:
                print(f"⚠️ Could not save camera state: {e}")
        
        # Backup DXF actors
        dxf_backup = []
        if hasattr(self.app, 'dxf_actors') and self.app.dxf_actors:
            for dxf_data in self.app.dxf_actors:
                for actor in dxf_data.get('actors', []):
                    dxf_backup.append(actor)
            
            if dxf_backup:
                renderer = self.app.vtk_widget.renderer
                for actor in dxf_backup:
                    renderer.RemoveActor(actor)
                print(f"   💾 Backed up {len(dxf_backup)} DXF actors")
        
        # Clear VTK completely
        if hasattr(self.app, "vtk_widget") and self.app.vtk_widget:
            renderer = self.app.vtk_widget.renderer
            renderer.RemoveAllViewProps()
            
            if hasattr(self.app.vtk_widget, 'actors'):
                self.app.vtk_widget.actors.clear()
            if hasattr(self.app.vtk_widget, '_actors'):
                self.app.vtk_widget._actors.clear()
            
            self.app.vtk_widget.render()
            print(f"   ✅ VTK cleared")
        
        # Clear cross-sections
        if hasattr(self.app, 'section_vtks') and self.app.section_vtks:
            for view_idx, vtk_widget in self.app.section_vtks.items():
                try:
                    vtk_widget.renderer.RemoveAllViewProps()
                    if hasattr(vtk_widget, 'actors'):
                        vtk_widget.actors.clear()
                    vtk_widget.render()
                except Exception:
                    pass
            print(f"   ✅ Cross-sections cleared")

        # Clear cut section state/view to prevent stale index-map from previous file
        if hasattr(self.app, 'cut_section_controller') and self.app.cut_section_controller:
            try:
                self.app.cut_section_controller.clear()
                print(f"   ✅ Cut section cleared")
            except Exception as e:
                print(f"   ⚠️ Cut section clear failed: {e}")
                try:
                    ctrl = self.app.cut_section_controller
                    ctrl.cut_points = None
                    ctrl._cut_index_map = None
                    ctrl.is_cut_view_active = False
                    print("   ✅ Applied fallback cut-state reset")
                except Exception:
                    pass
        
        # Clear ALL internal state - SAME AS MENU BAR
        self.app.data = None
        self.app._loaded_with_class_filter = False
        self.app.loaded_file = None
        self.app.last_save_path = None
        self.app.class_palette = {}
        # ✅ FIX: Clear stale Z-bounds cache so SNT actors land at the correct Z
        # for the incoming LAZ file. Without this, _get_snt_z_offset reads the old
        # file's z_max and the SNT grid appears above the new point cloud.
        self.app.data_bounds = None

        # Clear layers so previous file arrays are not retained across grid switches.
        if hasattr(self.app, "layers") and isinstance(self.app.layers, list):
            self.app.layers.clear()
        if hasattr(self.app, "layers_dock") and self.app.layers_dock:
            try:
                if hasattr(self.app.layers_dock, "clear_layers"):
                    self.app.layers_dock.clear_layers()
            except Exception:
                pass

        # Drop stale section caches/masks bound to previous dataset.
        try:
            import re
            stale_section_attrs = [
                name for name in list(vars(self.app).keys())
                if re.match(r"^section_\d+_", name) or re.match(r"^_section_\d+_", name)
            ]
            for name in stale_section_attrs:
                try:
                    delattr(self.app, name)
                except Exception:
                    pass
        except Exception:
            pass
        
        if hasattr(self.app, "view_palettes"):
            self.app.view_palettes.clear()
        
        if hasattr(self.app, 'undo_stack'):
            self.app.undo_stack.clear()
        if hasattr(self.app, 'redo_stack'):
            self.app.redo_stack.clear()
        
        if hasattr(self.app, 'spatial_index'):
            self.app.spatial_index = None
        
        print(f"   ✅ All data cleared")
        
        QCoreApplication.processEvents()
        
        # Restore DXF actors
        if dxf_backup:
            renderer = self.app.vtk_widget.renderer
            for actor in dxf_backup:
                renderer.AddActor(actor)
            self.app.vtk_widget.render()
            QCoreApplication.processEvents()
            print(f"   ✅ Restored {len(dxf_backup)} DXF actors")
        
        print(f"{'='*60}")
        print(f"✅ CLEAR COMPLETE")
        print(f"{'='*60}\n")

        # ============================================================================
        # STEP 4: LOAD FILE - SAME AS MENU BAR
        # ============================================================================
        print(f"{'='*60}")
        print(f"📂 LOADING FILE - SAME AS MENU BAR")
        print(f"{'='*60}")
        
        progress = LoadingProgressDialog(self.app, show_cancel=False)
        progress.set_filename(os.path.basename(str(las_file)))
        progress.show()

        def update_progress(percent, status, force=False):
            if force or not hasattr(update_progress, '_last_update'):
                progress.set_progress(percent)
                progress.set_status(status)
                QCoreApplication.processEvents()
                update_progress._last_update = time.time()
            else:
                if time.time() - update_progress._last_update > 0.2:
                    progress.set_progress(percent)
                    progress.set_status(status)
                    QCoreApplication.processEvents()
                    update_progress._last_update = time.time()

        load_start = time.time()
        self.app._dataset_load_in_progress = True

        try:
            # ============================================================================
            # LOAD THE FILE - SAME AS MENU BAR
            # ============================================================================
            update_progress(10, "Loading file...", force=True)
            
            from gui.data_loader import load_lidar_file

            tile_data = load_lidar_file(str(las_file), parent=self.app)

            if not tile_data:
                progress.finish_error("Load cancelled or failed")
                return

            total_points = len(tile_data.get('xyz', []))
            print(f"   ✅ Loaded {total_points:,} points")
            
            
            # ============================================================================
            # 🔍 DEBUG: VERIFY CORRECT FILE WAS LOADED
            # ============================================================================
            print(f"\n{'='*70}")
            print(f"🔍 FILE LOAD VERIFICATION")
            print(f"{'='*70}")
            print(f"   Requested Grid: {grid_name}")
            print(f"   Loaded File: {las_file.name}")
            print(f"   Full Path: {las_file}")

            # Calculate center of loaded data
            center = np.mean(tile_data['xyz'], axis=0)
            print(f"   Data Center: [{center[0]:.1f}, {center[1]:.1f}, {center[2]:.1f}]")

            # Calculate bounds
            xyz = tile_data['xyz']
            bounds = {
                'x_min': np.min(xyz[:, 0]),
                'x_max': np.max(xyz[:, 0]),
                'y_min': np.min(xyz[:, 1]),
                'y_max': np.max(xyz[:, 1]),
            }
            print(f"   Bounds:")
            print(f"      X: {bounds['x_min']:.1f} to {bounds['x_max']:.1f}")
            print(f"      Y: {bounds['y_min']:.1f} to {bounds['y_max']:.1f}")

            # Try to read file header directly to confirm
            try:
                import laspy
                with laspy.open(str(las_file)) as f:
                    header = f.header
                    print(f"   LAS Header Info:")
                    print(f"      Point Count: {header.point_count:,}")
                    print(f"      X Range: {header.x_min:.1f} to {header.x_max:.1f}")
                    print(f"      Y Range: {header.y_min:.1f} to {header.y_max:.1f}")
            except Exception as e:
                print(f"   ⚠️ Could not read LAS header: {e}")

            print(f"{'='*70}\n")
            # =====================
            
            # ============================================================================
            # SET DATA - SAME AS MENU BAR
            # ============================================================================
            update_progress(50, "Setting data...", force=True)
            
            # ✅ COORDINATE FIX: Detect if point cloud coords are wildly off from grid coords
            # Only triggers for MASSIVE mismatches (>100km) caused by missing LAS header offsets
            # e.g., point cloud at [5558, 45034] instead of [555800, 4503400]
            loaded_xyz = tile_data["xyz"]
            try:
                loaded_center = np.mean(loaded_xyz, axis=0)
                
                # Compute grid center from ALL actor bounds (not just first one)
                all_x_min, all_x_max = float('inf'), float('-inf')
                all_y_min, all_y_max = float('inf'), float('-inf')
                found_bounds = False
                
                for store_name in ('snt_actors', 'dxf_actors'):
                    store = getattr(self.app, store_name, None)
                    if store:
                        for entry in store:
                            for actor in entry.get('actors', []):
                                try:
                                    b = actor.GetBounds()
                                    if b and (b[1] - b[0]) > 0.1 and (b[3] - b[2]) > 0.1:
                                        all_x_min = min(all_x_min, b[0])
                                        all_x_max = max(all_x_max, b[1])
                                        all_y_min = min(all_y_min, b[2])
                                        all_y_max = max(all_y_max, b[3])
                                        found_bounds = True
                                except Exception:
                                    continue
                
                if found_bounds:
                    grid_center = np.array([
                        (all_x_min + all_x_max) / 2,
                        (all_y_min + all_y_max) / 2,
                        0
                    ])
                    dist_xy = np.sqrt((loaded_center[0] - grid_center[0])**2 + 
                                     (loaded_center[1] - grid_center[1])**2)
                    
                    # Only trigger for MASSIVE mismatches (>100km = missing LAS offset)
                    # Normal DXF grids can span 10-20km, so 100km threshold prevents false positives
                    if dist_xy > 100_000:
                        print(f"   ⚠️ COORDINATE MISMATCH DETECTED!")
                        print(f"      Point cloud center: [{loaded_center[0]:.1f}, {loaded_center[1]:.1f}]")
                        print(f"      Grid center: [{grid_center[0]:.1f}, {grid_center[1]:.1f}]")
                        print(f"      Distance: {dist_xy:.0f}m — applying LAS header offset...")
                        
                        try:
                            import laspy
                            with laspy.open(str(las_file)) as f:
                                hdr = f.header
                                hdr_center_x = (hdr.x_min + hdr.x_max) / 2
                                hdr_center_y = (hdr.y_min + hdr.y_max) / 2
                                
                                hdr_dist = np.sqrt((hdr_center_x - grid_center[0])**2 + 
                                                   (hdr_center_y - grid_center[1])**2)
                                
                                if hdr_dist < dist_xy:
                                    offset_x = hdr_center_x - loaded_center[0]
                                    offset_y = hdr_center_y - loaded_center[1]
                                    
                                    # Only apply if offset is significant (>10km)
                                    if abs(offset_x) > 10_000 or abs(offset_y) > 10_000:
                                        print(f"      LAS header center: [{hdr_center_x:.1f}, {hdr_center_y:.1f}]")
                                        print(f"      Applying offset: [{offset_x:.1f}, {offset_y:.1f}]")
                                        loaded_xyz[:, 0] += offset_x
                                        loaded_xyz[:, 1] += offset_y
                                        tile_data["xyz"] = loaded_xyz
                                        new_center = np.mean(loaded_xyz, axis=0)
                                        print(f"      ✅ Corrected center: [{new_center[0]:.1f}, {new_center[1]:.1f}]")
                                    else:
                                        print(f"      ⚠️ Offset too small ({offset_x:.1f}, {offset_y:.1f}) — skipping")
                                else:
                                    print(f"      ⚠️ Header coords also don't match grid — skipping")
                        except Exception as e:
                            print(f"      ⚠️ Could not read LAS header: {e}")
                    else:
                        print(f"   ✅ Coordinates match grid (distance: {dist_xy:.0f}m)")
            except Exception as e:
                print(f"   ⚠️ Coordinate check failed: {e}")
            
            self.app.data = {
                "xyz": tile_data["xyz"],
                "classification": tile_data["classification"]
            }
            xyz_app = self.app.data["xyz"]
            print(
                "   COORD_TRACE APP.DATA: "
                f"X=[{xyz_app[:, 0].min():.3f}, {xyz_app[:, 0].max():.3f}] "
                f"Y=[{xyz_app[:, 1].min():.3f}, {xyz_app[:, 1].max():.3f}]"
            )

            # Track whether this load was class-filtered so auto-save can
            # refuse to overwrite the source file with a partial dataset.
            _load_opts = tile_data.get("import_options") or {}
            self.app._loaded_with_class_filter = bool(
                _load_opts.get("only_class") and _load_opts.get("class_codes")
            )

            if tile_data.get("rgb") is not None:
                self.app.data["rgb"] = tile_data["rgb"]
            if tile_data.get("intensity") is not None:
                self.app.data["intensity"] = tile_data["intensity"]
            
            # Set CRS - SAME AS MENU BAR
            if tile_data.get("crs_epsg"):
                self.app.project_crs_epsg = tile_data["crs_epsg"]
                self.app.project_crs_wkt = tile_data.get("crs_wkt")
                
                try:
                    from pyproj import CRS
                    self.app.crs = CRS.from_epsg(tile_data["crs_epsg"])
                    print(f"   📐 CRS: {self.app.crs.name}")
                except Exception:
                    pass
            
            # Store as layer - SAME AS MENU BAR
            layer = {
                "type": "laz_tile",
                "filename": str(las_file),
                "xyz": tile_data["xyz"],
                "classification": tile_data.get("classification"),
                "rgb": tile_data.get("rgb"),
                "intensity": tile_data.get("intensity"),
                "crs_epsg": tile_data.get("crs_epsg"),
                "visible": True,
            }
            
            if hasattr(self.app, 'layers'):
                self.app.layers.append(layer)
            
            if hasattr(self.app, 'layers_dock') and self.app.layers_dock:
                self.app.layers_dock.add_layer(layer)
            
            # Set file paths
            self.app.loaded_file = str(las_file)
            self.app.last_save_path = str(las_file)
            
            # ============================================================================
            # BUILD DEM FOR SHADING - SAME AS MENU BAR
            # ============================================================================
            try:
                from gui.shading_display import build_base_dem_mesh
                build_base_dem_mesh(self.app, percentile_filter=99.9, downsample=2)
            except Exception:
                pass
            
            # ============================================================================
            # BUILD SPATIAL INDEX - SAME AS MENU BAR
            # ============================================================================
            if total_points > 50_000:
                # Build the same full-resolution KD-tree after the visible load.
                # Existing tools retain their vectorized fallback until ready.
                self.app.spatial_index = None
                indexed_xyz = self.app.data["xyz"]
                indexed_xyz_id = id(indexed_xyz)

                def _start_deferred_spatial_index():
                    import threading

                    def _worker():
                        try:
                            from gui.performance_optimizations import SpatialIndex
                            built_index = SpatialIndex(indexed_xyz)
                            current_data = getattr(self.app, "data", None) or {}
                            if id(current_data.get("xyz")) == indexed_xyz_id:
                                self.app.spatial_index = built_index
                                print("   Deferred spatial index installed")
                            else:
                                print("   Deferred spatial index discarded: data replaced")
                        except Exception as exc:
                            print(f"   Deferred spatial index failed: {exc}")

                    threading.Thread(
                        target=_worker, name="NakshaSpatialIndex", daemon=True
                    ).start()

                from PySide6.QtCore import QTimer
                QTimer.singleShot(1500, _start_deferred_spatial_index)
                print("   Full-resolution spatial index scheduled after load")

            # ============================================================================
            # RESTORE DISPLAY SETTINGS - SAME AS MENU BAR
            # ============================================================================
            update_progress(75, "Restoring settings...", force=True)
            
            # Set default display mode first
            self.app.display_mode = "class"

            try:
                from gui.display_mode import restore_display_settings_for_file
                self.app._prefer_session_display_restore = True
                try:
                    restore_display_settings_for_file(self.app, str(las_file), refresh=False)
                finally:
                    self.app._prefer_session_display_restore = False
            except Exception:
                self.app._prefer_session_display_restore = False
                pass

            # Grid switching is a dataset load too: all main/cross/cut slots
            # must begin in Structured border mode.
            from gui.unified_actor_manager import reset_border_logic_to_structured
            reset_border_logic_to_structured(self.app)

            # If the user loaded with class filter, force class display mode regardless
            # of what restore_display_settings_for_file restored (e.g. "surface").
            # This ensures Display Mode palette changes work immediately after load.
            _load_opts = tile_data.get("import_options") or {}
            if _load_opts.get("only_class"):
                self.app.display_mode = "class"
            
            # ============================================================================
            # LOAD PALETTE - SAME AS MENU BAR
            # ============================================================================
            update_progress(80, "Loading palette...", force=True)
            
            palette_to_apply = None
            if hasattr(self.app, '_get_palette_for_file'):
                palette_to_apply = self.app._get_palette_for_file(str(las_file))
            
            # ============================================================================
            # APPLY PALETTE AND RENDER - SAME AS MENU BAR
            # ============================================================================
            # This is critical for per-class visibility and updates!
            
            if palette_to_apply:
                # If user selected specific classes on load, hide all others.
                # All points remain in memory for Display Mode re-apply.
                _load_opts = tile_data.get("import_options") or {}
                if _load_opts.get("only_class"):
                    from gui.display_mode import clone_palette
                    _sel = set(int(c) for c in (_load_opts.get("class_codes") or []))
                    palette_to_apply = clone_palette(palette_to_apply)
                    for _code, _entry in palette_to_apply.items():
                        _entry["show"] = (_code in _sel)
                    print(f"   👁 Initial visibility: showing classes {sorted(_sel)}")

                visible_count = len([c for c, v in palette_to_apply.items() if v.get("show")])
                update_progress(85, f"Rendering {visible_count} classes...", force=True)

                print(f"🎨 Applying palette with {visible_count} visible classes...")

                # Apply palette which will create Per-Class Actors
                self.app.apply_class_map({
                    "classes": palette_to_apply,
                    "slot": 0,
                    "color_mode": 0,
                    "target_view": 0,
                })
            else:
                # Fallback: build palette from classification
                update_progress(85, "Building palette...", force=True)
                
                try:
                    from gui.class_display import build_class_palette, update_class_mode
                    self.app.class_palette = build_class_palette(tile_data['classification'])
                    print(f"   ✅ Built palette: {len(self.app.class_palette)} classes")
                    
                    # Use apply_class_map for consistent behavior (creates per-class actors)
                    self.app.apply_class_map({
                        "classes": self.app.class_palette,
                        "slot": 0,
                        "color_mode": 0,
                        "target_view": 0
                    })
                except Exception as e:
                    print(f"   ⚠️ Palette build failed: {e}")
                    # Ultimate fallback (creates unified cloud - NOT ideal but works)
                    from gui.pointcloud_display import update_pointcloud
                    update_pointcloud(self.app, "class")
            
            # ============================================================================
            # RESTORE CAMERA STATE (preserve zoom when loading into DXF)
            # ============================================================================
            if saved_camera_state:
                try:
                    camera = self.app.vtk_widget.renderer.GetActiveCamera()
                    camera.SetPosition(saved_camera_state['position'])
                    camera.SetFocalPoint(saved_camera_state['focal_point'])
                    camera.SetViewUp(saved_camera_state['view_up'])
                    camera.SetParallelScale(saved_camera_state['parallel_scale'])
                    camera.SetParallelProjection(saved_camera_state['parallel_projection'])
                    self.app.vtk_widget.renderer.ResetCameraClippingRange()
                    print(f"📷 Camera restored (zoom: {saved_camera_state['parallel_scale']:.2f})")
                except Exception as e:
                    print(f"⚠️ Camera restore failed: {e}")
            
            # ============================================================================
            # FINALIZE - SAME AS MENU BAR
            # ============================================================================
            update_progress(95, "Finalizing...", force=True)
            
            try:
                from gui.pointcloud_display import force_interactor_ready
                force_interactor_ready(self.app, delay_ms=300)
            except Exception:
                pass
            
            # Toggle view mode - BUT DON'T RESET CAMERA IF WE SAVED STATE
            if hasattr(self.app, 'toggle_view_mode'):
                if saved_camera_state is None:
                    # First load - reset to 2D view
                    self.app.toggle_view_mode("2d")
                else:
                    # Loading into DXF - preserve camera
                    print("📷 Skipping view reset (preserving zoom)")
            
            # ✅ RESTORE CAMERA STATE AFTER VIEW MODE WITH SMART ADJUSTMENT
            if saved_camera_state:
                try:
                    import numpy as np
                    camera = self.app.vtk_widget.renderer.GetActiveCamera()

                    # Guard: data must still be present (it could be None if load
                    # failed after camera save but before we reach this point).
                    xyz = (self.app.data or {}).get('xyz') if self.app.data is not None else None
                    if xyz is None or len(xyz) == 0:
                        raise ValueError("No point data available for camera adjustment")

                    # Calculate the offset between old and new data centers
                    new_data_center = np.mean(xyz, axis=0)
                    old_focal_point = np.array(saved_camera_state['focal_point'])
                    
                    # ✅ FIX: Only shift XY — do NOT shift Z.
                    # The saved focal_point Z comes from the previous camera state
                    # (often near 0 for a top-down 2D view), while new_data_center Z
                    # is the actual elevation of the new LAZ file (e.g. 550 m).
                    # Applying the Z shift moves the focal plane far away from the
                    # actual geometry, which corrupts ResetCameraClippingRange() and
                    # makes every pan event lag or "slide" during interaction.
                    # Z must stay at the new data's median elevation set by fit_view.
                    shift_xy = new_data_center[:2] - old_focal_point[:2]
                    shift_3d = np.array([shift_xy[0], shift_xy[1], 0.0])
                    
                    new_position  = np.array(saved_camera_state['position'])  + shift_3d
                    new_focal_point = old_focal_point + shift_3d
                    
                    # Restore camera with adjusted position
                    camera.SetPosition(new_position[0], new_position[1], new_position[2])
                    camera.SetFocalPoint(new_focal_point[0], new_focal_point[1], new_focal_point[2])
                    camera.SetViewUp(saved_camera_state['view_up'])
                    camera.SetParallelScale(saved_camera_state['parallel_scale'])  # ✅ Preserves zoom!
                    camera.SetParallelProjection(saved_camera_state['parallel_projection'])
                    
                    self.app.vtk_widget.renderer.ResetCameraClippingRange()
                    self.app.vtk_widget.render()
                    
                    print(f"📷 Camera restored with smart adjustment")
                    print(f"   Zoom: {saved_camera_state['parallel_scale']:.2f}")
                    print(f"   Old center: [{old_focal_point[0]:.1f}, {old_focal_point[1]:.1f}]")
                    print(f"   New center: [{new_data_center[0]:.1f}, {new_data_center[1]:.1f}]")
                    print(f"   Shift: [{shift_xy[0]:.1f}, {shift_xy[1]:.1f}]")
                except Exception as e:
                    print(f"⚠️ Camera restore failed: {e}")
                    import traceback
                    traceback.print_exc()

            
            loaded_label = Path(las_file).stem
            owner_label = loaded_label if loaded_label else grid_name

            if hasattr(self.app, 'ensure_main_view_2d_interaction'):
                self.app.ensure_main_view_2d_interaction(
                    preserve_camera=True,
                    reason=f"grid load: {owner_label}",
                )

            try:
                digitizer = getattr(self.app, "digitizer", None)
                if digitizer is not None:
                    if hasattr(digitizer, "_check_and_update_renderers"):
                        digitizer._check_and_update_renderers()
                    if hasattr(digitizer, "_reinstall_all_observers"):
                        digitizer._reinstall_all_observers()
                    print("   ✅ Digitizer fully restored (grid load)")
            except Exception as dig_err:
                print(f"   ⚠️ Digitizer restore skipped: {dig_err}")

            _reset_interaction_state()

            # Update title with grid name
            self.app._update_window_title(
                f"{owner_label} ({total_points:,} pts)", 
                getattr(self.app, 'project_crs_epsg', None)
            )
            
            # ============================================================================
            # AUTO-LOAD DRAWINGS - SAME AS MENU BAR
            # ============================================================================
            if hasattr(self.app, "digitizer") and self.app.digitizer:
                try:
                    self.app.digitizer.auto_load_drawings(str(las_file))
                except Exception:
                    pass
            
            # ============================================================================
            # UPDATE STATISTICS - SAME AS MENU BAR
            # ============================================================================
            if hasattr(self.app, 'point_count_widget') and self.app.point_count_widget:
                try:
                    from gui.point_count_widget import refresh_point_statistics
                    refresh_point_statistics(self.app)
                except Exception:
                    pass
            
            # ============================================================================
            # TRACK GRID (additional for grid system)
            # ============================================================================
            grid_indices = np.arange(total_points, dtype=np.int32)
            self.loaded_grids[owner_label] = grid_indices
            if owner_label != grid_name:
                self.grid_aliases[grid_name] = owner_label
            
            if not hasattr(self.app, 'original_file_paths'):
                self.app.original_file_paths = {}
            self.app.original_file_paths[owner_label] = str(las_file)
            
            if owner_label != grid_name:
                print(f"   📍 Tracked grid: {owner_label} (requested: {grid_name})")
            else:
                print(f"   📍 Tracked grid: {owner_label}")
            
            # ============================================================================
            # COMPLETE
            # ============================================================================
            total_time = time.time() - load_start
            
            print(f"\n{'='*60}")
            print(f"✅ GRID LOAD COMPLETE - SAME AS MENU BAR")
            print(f"   Grid: {owner_label}")
            print(f"   Points: {total_points:,}")
            print(f"   Time: {total_time:.1f}s")
            print(f"{'='*60}\n")
            
            # ✅ BULLETPROOF: Re-ensure all DXF/SNT overlay actors are in renderer
            # Some code paths during load (build_unified_actor, apply_class_map, etc.)
            # may have removed actors. This guarantees they're always visible.
            if hasattr(self.app, '_ensure_overlay_actors'):
                self.app._ensure_overlay_actors()
            
            progress.finish_success(f"Loaded {total_points:,} points in {total_time:.1f}s")
            
            # Show success message
            QMessageBox.information(
                self.app,
                "Grid Loaded",
                f"✅ Loaded: {owner_label}\n\n"
                f"File: {las_file.name}\n"
                f"Points: {total_points:,}"
            )
            
        except Exception as e:
            print(f"❌ Load failed: {e}")
            import traceback
            traceback.print_exc()
            progress.finish_error(f"Load failed: {e}")
            QMessageBox.critical(self.app, "Load Error", f"Failed to load: {e}")
        finally:
            self.app._dataset_load_in_progress = False

    def _save_folder_to_settings(self, folder_path):
        """Save LAZ/LAS folder to settings for future use"""
        self.settings.setValue("last_las_folder", str(folder_path))
        self.settings.sync()
        print(f"💾 Saved LAZ folder: {folder_path}")
        
    def clear_grid_data(self, grid_name):
        """Delete points belonging to a specific grid from the loaded dataset"""
        print(f"\n{'='*60}")
        print(f"🗑️ DELETING GRID DATA: {grid_name}")
        print(f"{'='*60}\n")
        
        # Check if grid is tracked
        if grid_name not in self.loaded_grids:
            QMessageBox.warning(
                self.app,
                "Grid Not Found",
                f"Grid '{grid_name}' is not currently loaded.\n\n"
                f"Only grids loaded via grid label click can be deleted."
            )
            return
        
        # Confirm deletion
        points_to_delete = len(self.loaded_grids[grid_name])
        reply = QMessageBox.question(
            self.app,
            "Confirm Clear",
            f"Clear all points from grid:\n{grid_name}\n\n"
            f"Points: {points_to_delete:,}\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.No:
            return
        
        try:
            import numpy as np
            from gui.pointcloud_display import update_pointcloud
            from PySide6.QtCore import QCoreApplication
            
            # Get total points before any operations
            total_points = len(self.app.data['xyz'])
            
            # ✅ FIX: Validate indices before using them
            indices_to_delete = self.loaded_grids[grid_name]
            indices_to_delete = self._validate_grid_indices(
                grid_name, indices_to_delete, total_points
            )
            
            if len(indices_to_delete) == 0:
                print(f"  ⚠️ No valid indices to delete")
                del self.loaded_grids[grid_name]
                QMessageBox.warning(
                    self.app,
                    "No Valid Points",
                    f"Grid '{grid_name}' has no valid point indices.\n"
                    f"The grid tracking has been cleared."
                )
                return
            
            # ✅ OPTIMIZED: Use boolean mask (much faster than index manipulation)
            print(f"  🔄 Creating deletion mask...")
            QCoreApplication.processEvents()
            
            keep_mask = np.ones(total_points, dtype=bool)
            keep_mask[indices_to_delete] = False
            
            remaining = np.sum(keep_mask)
            print(f"  Remaining: {remaining:,}")
            
            # ✅ OPTIMIZED: Filter all arrays in one pass
            print(f"  🔄 Filtering point data...")
            QCoreApplication.processEvents()
            
            self.app.data['xyz'] = self.app.data['xyz'][keep_mask]
            self.app.data['classification'] = self.app.data['classification'][keep_mask]
            
            if 'rgb' in self.app.data and self.app.data['rgb'] is not None:
                self.app.data['rgb'] = self.app.data['rgb'][keep_mask]
            
            if 'intensity' in self.app.data and self.app.data['intensity'] is not None:
                self.app.data['intensity'] = self.app.data['intensity'][keep_mask]
            
            # ✅ OPTIMIZED: Vectorized index updating for remaining grids
            print(f"  🔄 Updating grid tracking...")
            QCoreApplication.processEvents()
            
            if len(self.loaded_grids) > 1:
                # Create mapping: old_index -> new_index
                # This is MUCH faster than looping
                old_to_new = np.full(total_points, -1, dtype=np.int64)
                old_to_new[keep_mask] = np.arange(remaining)
                
                # Update all other grids in one vectorized operation
                for other_grid in list(self.loaded_grids.keys()):
                    if other_grid == grid_name:
                        continue
                    
                    old_indices = self.loaded_grids[other_grid]
                    new_indices = old_to_new[old_indices]
                    
                    # Filter out any invalid indices (shouldn't happen, but safety check)
                    valid = new_indices >= 0
                    self.loaded_grids[other_grid] = new_indices[valid]
                    
                    print(f"     Updated {other_grid}: {len(old_indices)} -> {len(new_indices[valid])} indices")
            
            # Remove deleted grid
            del self.loaded_grids[grid_name]
            # Drop alias links pointing to deleted owner
            if self.grid_aliases:
                stale_aliases = [k for k, v in self.grid_aliases.items() if v == grid_name]
                for alias in stale_aliases:
                    try:
                        del self.grid_aliases[alias]
                    except Exception:
                        pass
            
            print(f"  🔄 Rebuilding spatial index...")
            QCoreApplication.processEvents()
            
            # Rebuild spatial index if it exists
            if hasattr(self.app, 'spatial_index') and remaining > 50_000:
                try:
                    from gui.performance_optimizations import SpatialIndex
                    self.app.spatial_index = SpatialIndex(self.app.data["xyz"])
                    print(f"  ✅ Spatial index rebuilt")
                except Exception as e:
                    print(f"  ⚠️ Spatial index rebuild failed: {e}")
                    self.app.spatial_index = None
            
            # Update display
            print(f"  🔄 Updating display...")
            QCoreApplication.processEvents()
            
            
            if remaining == 0:
                print(f"  ⚠️ No points remaining - clearing scene")

                # Surface mode can remain stale after last LAZ/grid is removed.
                # Clear Surface first so next SNT grid click loads data instead of opening Surface Settings.
                try:
                    from gui.surface_mode import detach_surface_before_non_surface_mode
                    detach_surface_before_non_surface_mode(self.app, requested_mode="grid_clear")
                    print("  🧹 Surface state cleared after last grid removal")
                except Exception as e:
                    print(f"  ⚠️ Surface cleanup after grid clear skipped: {e}")

                try:
                    self.app.display_mode = "rgb"
                    self.app.current_display_mode = "rgb"
                    self.app._suspend_grid_clicks = False
                    self.app._surface_mesh_actor = None
                    self.app._surface_mesh_polydata = None
                    self.app._surface_points = None
                    self.app._surface_faces = None
                    self.app._surface_global_to_unique = None
                    self.app._surface_unique_global_indices = None
                except Exception:
                    pass
                
                # Clear the renderer
                if hasattr(self.app, 'vtk_widget') and self.app.vtk_widget:
                    self.app.vtk_widget.renderer.RemoveAllViewProps()
                    self.app.vtk_widget.render()
                
                # Reset data
                self.app.data = None
                self.app.loaded_file = None
                self.app.last_save_path = None
                
                # Update title
                self.app._update_window_title("No Data", None)
                
                # Restore DXF if exists
                if hasattr(self.app, 'preserve_dxf_actors'):
                    from PySide6.QtCore import QTimer
                    QTimer.singleShot(100, self.app.preserve_dxf_actors)
                
                QMessageBox.information(
                    self.app,
                    "All Grids Cleared",
                    f"✅ Cleared: {grid_name}\n\n"
                    f"All points removed.\n"
                    f"Load a new grid to continue."
                )
                
                print(f"✅ Scene cleared completely")
                return
            
            update_pointcloud(self.app, self.app.display_mode)
            
            # Restore DXF
            if hasattr(self.app, 'preserve_dxf_actors'):
                from PySide6.QtCore import QTimer
                QTimer.singleShot(100, self.app.preserve_dxf_actors)
            
            # Update UI
            self.app._update_window_title(
                f"Multiple Grids ({remaining:,} pts)",
                self.app.project_crs_epsg
            )
            
            if hasattr(self.app, 'point_count_widget') and self.app.point_count_widget:
                from gui.point_count_widget import refresh_point_statistics
                refresh_point_statistics(self.app)
            
            print(f"✅ Grid deleted successfully")
            print(f"{'='*60}\n")
            
            QMessageBox.information(
                self.app,
                "Grid Cleared",
                f"✅ Cleared: {grid_name}\n\n"
                f"Removed: {points_to_delete:,} points\n"
                f"Remaining: {remaining:,} points"
            )
            self.app.last_save_path = None  # Clear save path so auto-save won't overwrite
            print(f"  ℹ️ Cleared save path - original file will not be overwritten")
        except Exception as e:
            print(f"❌ Delete failed: {e}")
            import traceback
            traceback.print_exc()
            
            QMessageBox.critical(
                self.app,
                "Delete Error",
                f"Failed to delete grid '{grid_name}':\n\n{str(e)}"
            )
            
            
    def setup_interactor(self):
        """Attach observers to main VTK widget"""
        if not hasattr(self.app, 'vtk_widget'):
            print("⚠️ No VTK widget found")
            return
        
        interactor = self.app.vtk_widget.interactor
        
        # ✨ NEW: Add keyboard shortcut for rectangle selection
        
        print("✅ Grid label system enabled (Press 'D' to delete grid by area)")

    def setup_interactor(self):
        """Attach observers to main VTK widget."""
        if not hasattr(self.app, 'vtk_widget'):
            print("No VTK widget found")
            return

        self.ensure_interactor_observers()
        print("Grid label system enabled (Press 'D' to delete grid by area)")

    def on_key_press(self, obj, event):
        """Handle keyboard shortcuts"""
        handles = self._get_main_interactor_handles()
        if handles is None:
            return 0
        _, interactor, _ = handles
        key = interactor.GetKeySym()
        
        if key == 'd' or key == 'D':
            self._start_rectangle_delete_mode()

    def _start_rectangle_delete_mode(self):
        """Start interactive rectangle selection for deletion"""
        from PySide6.QtWidgets import QMessageBox
        
        QMessageBox.information(
            self.app,
            "Rectangle Delete Mode",
            "📦 RECTANGLE DELETE MODE\n\n"
            "1. Click and drag to draw a rectangle\n"
            "2. Release to select the grid area\n"
            "3. Confirm deletion\n\n"
            "Press ESC to cancel"
        )
        
        # Use VTK's rubber band picker
        style = vtk.vtkInteractorStyleRubberBandPick()
        self.app.vtk_widget.interactor.SetInteractorStyle(style)
        
        # Add observer for selection complete
        style.AddObserver("EndPickEvent", self._on_rectangle_selected)
        
        self._temp_style = style  # Store to restore later

    def _on_rectangle_selected(self, obj, event):
        """Handle rectangle selection complete"""
        import numpy as np
        from PySide6.QtWidgets import QInputDialog
        
        # Get selected area
        style = obj
        x1, y1, x2, y2 = style.GetStartPosition() + style.GetEndPosition()
        
        # Use area picker
        area_picker = vtk.vtkAreaPicker()
        area_picker.AreaPick(x1, y1, x2, y2, self.app.vtk_widget.renderer)
        
        # Get frustum (selection volume)
        frustum = area_picker.GetFrustum()
        
        if not frustum or not hasattr(self.app, 'data'):
            self._restore_normal_interaction()
            return
        
        # Find points inside selection
        points_xyz = self.app.data['xyz']
        
        # Convert to VTK points for frustum testing
        selected_indices = []
        
        for i, point in enumerate(points_xyz):
            vtk_point = vtk.vtkPoints()
            vtk_point.InsertNextPoint(point[0], point[1], point[2])
            
            # Check if point is inside frustum
            if frustum.EvaluateFunction(point[0], point[1], point[2]) < 0:
                selected_indices.append(i)
        
        selected_indices = np.array(selected_indices)
        
        if len(selected_indices) == 0:
            QMessageBox.warning(self.app, "No Points", "No points selected in this area")
            self._restore_normal_interaction()
            return
        
        # Ask for grid name or auto-detect
        grid_name, ok = QInputDialog.getText(
            self.app,
            "Delete Grid",
            f"Selected: {len(selected_indices):,} points\n\n"
            f"Enter grid name to delete:"
        )
        
        if ok and grid_name:
            # Track and delete
            self.loaded_grids[grid_name] = selected_indices
            self.delete_grid_data(grid_name)
        
        self._restore_normal_interaction()

    def _restore_normal_interaction(self):
        """Restore normal camera interaction"""
        if hasattr(self.app, 'ensure_main_view_2d_interaction') and not getattr(self.app, 'is_3d_mode', False):
            self.app.ensure_main_view_2d_interaction(
                preserve_camera=True,
                reason="grid_label_restore",
            )
            return

        style = vtk.vtkInteractorStyleTrackballCamera()
        self.app.vtk_widget.interactor.SetInteractorStyle(style)

    def _validate_grid_indices(self, grid_name, indices, current_data_size):
        """
        Validate that grid indices are within bounds of current data.
        Returns cleaned indices array.
        """
        import numpy as np
        
        if indices is None or len(indices) == 0:
            return np.array([], dtype=np.int64)
        
        # Check if any indices are out of bounds
        max_index = np.max(indices)
        if max_index >= current_data_size:
            print(f"  ⚠️ WARNING: Grid '{grid_name}' has invalid indices")
            print(f"     Max index: {max_index}, Data size: {current_data_size}")
            print(f"     Filtering out-of-bounds indices...")
            
            # Keep only valid indices
            valid_mask = indices < current_data_size
            valid_indices = indices[valid_mask]
            
            invalid_count = len(indices) - len(valid_indices)
            if invalid_count > 0:
                print(f"     Removed {invalid_count} invalid indices")
            
            return valid_indices
        
        return indices        
    
    def verify_dxf_labels(self):
        """Debug tool: Compare DXF label positions with actual LAZ file centers"""
        import laspy
        from pathlib import Path
        from PySide6.QtWidgets import QMessageBox, QTextEdit, QVBoxLayout, QDialog, QPushButton
        
        # Find LAZ folder automatically
        las_folder = self._find_las_folder_from_dxf()
        
        if not las_folder:
            QMessageBox.warning(
                self.app,
                "No LAZ Folder",
                "Could not locate LAZ/LAS folder.\n\n"
                "Please load a DXF file first."
            )
            return
        
        print(f"\n{'='*70}")
        print(f"🔍 DXF LABEL VERIFICATION")
        print(f"{'='*70}\n")
        
        # Get all label positions from DXF
        label_positions = {}
        if hasattr(self.app, 'dxf_actors') and self.app.dxf_actors:
            for dxf_data in self.app.dxf_actors:
                for actor in dxf_data.get('actors', []):
                    if hasattr(actor, 'is_grid_label') and actor.is_grid_label:
                        grid_name = getattr(actor, 'grid_name', '')
                        if grid_name:
                            pos = actor.GetPosition()
                            label_positions[grid_name] = pos
        
        if not label_positions:
            QMessageBox.warning(
                self.app,
                "No Labels Found",
                "No grid labels found in DXF.\n\n"
                "Make sure you've loaded a DXF with grid labels."
            )
            return
        
        # Build verification report
        report_lines = []
        misaligned_count = 0
        total_count = 0
        
        # Get all LAZ file centers
        las_folder_path = Path(las_folder)
        for las_file in sorted(las_folder_path.glob("*.laz")):
            try:
                with laspy.open(las_file) as f:
                    header = f.header
                    center_x = (header.x_min + header.x_max) / 2
                    center_y = (header.y_min + header.y_max) / 2
                    
                    grid_name = las_file.stem
                    total_count += 1
                    
                    if grid_name in label_positions:
                        label_pos = label_positions[grid_name]
                        distance = np.sqrt(
                            (center_x - label_pos[0])**2 + 
                            (center_y - label_pos[1])**2
                        )
                        
                        # Consider misaligned if >500m away
                        is_misaligned = distance > 500
                        if is_misaligned:
                            misaligned_count += 1
                        
                        status = "❌ MISALIGNED" if is_misaligned else "✅ OK"
                        
                        report_lines.append(f"{status} {grid_name}:")
                        report_lines.append(f"   Label Position: [{label_pos[0]:.1f}, {label_pos[1]:.1f}]")
                        report_lines.append(f"   Data Center:    [{center_x:.1f}, {center_y:.1f}]")
                        report_lines.append(f"   Distance: {distance:.1f}m")
                        report_lines.append("")
                        
                        # Print to console too
                        print(f"{status} {grid_name}:")
                        print(f"   Label: [{label_pos[0]:.1f}, {label_pos[1]:.1f}]")
                        print(f"   Data:  [{center_x:.1f}, {center_y:.1f}]")
                        print(f"   Distance: {distance:.1f}m\n")
                    else:
                        report_lines.append(f"⚠️ {grid_name}: No label found in DXF")
                        report_lines.append("")
                        print(f"⚠️ {grid_name}: No label found in DXF\n")
                        
            except Exception as e:
                report_lines.append(f"❌ {las_file.name}: Error reading file - {e}")
                report_lines.append("")
                print(f"❌ {las_file.name}: {e}\n")
        
        print(f"{'='*70}\n")
        
        # Create dialog to show results
        dialog = QDialog(self.app)
        dialog.setWindowTitle("DXF Label Verification")
        dialog.resize(700, 500)
        
        layout = QVBoxLayout()
        
        # Summary
        summary = QTextEdit()
        summary.setReadOnly(True)
        summary.setMaximumHeight(80)
        
        summary_text = f"""📊 VERIFICATION SUMMARY
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    Total LAZ files: {total_count}
    Labels in DXF: {len(label_positions)}
    Misaligned (>500m): {misaligned_count}
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    """
        summary.setPlainText(summary_text)
        layout.addWidget(summary)
        
        # Detailed report
        report = QTextEdit()
        report.setReadOnly(True)
        report.setPlainText("\n".join(report_lines))
        layout.addWidget(report)
        
        # Close button
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.accept)
        layout.addWidget(close_btn)
        
        dialog.setLayout(layout)
        dialog.exec()

    
    def list_all_text_in_dxf(self):
        """Debug: List all text actors in the scene - FIXED VERSION"""
        from PySide6.QtWidgets import QTextEdit, QDialog, QVBoxLayout, QPushButton
        import vtk
        
        text_actors = []
        
        if hasattr(self.app, 'dxf_actors') and self.app.dxf_actors:
            for dxf_data in self.app.dxf_actors:
                for actor in dxf_data.get('actors', []):
                    pos = actor.GetPosition()
                    
                    # Try multiple methods to get text content
                    text_content = "Unknown"
                    is_text_actor = False
                    
                    # Method 1: Check for custom properties we set
                    if hasattr(actor, 'grid_name'):
                        text_content = actor.grid_name
                        is_text_actor = True
                    elif hasattr(actor, 'text_content'):
                        text_content = actor.text_content
                        is_text_actor = True
                    
                    # Method 2: Check if it's a vtkTextActor3D
                    elif isinstance(actor, vtk.vtkTextActor3D):
                        text_content = actor.GetInput()
                        is_text_actor = True
                    
                    # Method 3: Check if it's a vtkFollower with vector text source
                    elif isinstance(actor, (vtk.vtkFollower, vtk.vtkActor)):
                        mapper = actor.GetMapper()
                        if mapper:
                            input_data = mapper.GetInput()
                            
                            # Check if it's from a vtkVectorText source
                            if input_data and hasattr(mapper, 'GetInputAlgorithm'):
                                algo = mapper.GetInputAlgorithm()
                                if isinstance(algo, vtk.vtkVectorText):
                                    text_content = algo.GetText()
                                    is_text_actor = True
                            
                            # Alternative: Check if it's a very small polydata (likely text)
                            elif input_data and input_data.GetNumberOfPoints() < 1000:
                                # Small polydata might be text
                                # Check point data for clues
                                if hasattr(input_data, 'GetPointData'):
                                    point_data = input_data.GetPointData()
                                    if point_data.GetNumberOfArrays() > 0:
                                        is_text_actor = True
                                        # Still unknown text but we know it's text-like
                    
                    # Method 4: Check property metadata
                    if not is_text_actor and hasattr(actor, 'GetProperty'):
                        prop = actor.GetProperty()
                        # Text actors often have specific rendering properties
                        if prop and prop.GetRepresentation() == vtk.VTK_SURFACE:
                            mapper = actor.GetMapper()
                            if mapper and mapper.GetInput():
                                num_points = mapper.GetInput().GetNumberOfPoints()
                                # Text typically has moderate point counts
                                if 10 < num_points < 2000:
                                    is_text_actor = True
                    
                    # Only add if we detected it as text
                    if is_text_actor:
                        is_label = hasattr(actor, 'is_grid_label') and actor.is_grid_label
                        
                        text_actors.append({
                            'text': text_content,
                            'position': pos,
                            'is_grid_label': is_label,
                            'actor_type': type(actor).__name__
                        })
        
        # Show dialog
        dialog = QDialog(self.app)
        dialog.setWindowTitle("DXF Text Analysis")
        dialog.resize(700, 500)
        
        layout = QVBoxLayout()
        
        report = QTextEdit()
        report.setReadOnly(True)
        
        lines = [f"Total text actors found: {len(text_actors)}\n"]
        lines.append("=" * 70)
        lines.append("")
        
        grid_labels = [t for t in text_actors if t['is_grid_label']]
        lines.append(f"Grid labels (is_grid_label=True): {len(grid_labels)}")
        lines.append(f"Other text: {len(text_actors) - len(grid_labels)}")
        lines.append("")
        lines.append("=" * 70)
        lines.append("")
        
        for i, actor_info in enumerate(text_actors[:50], 1):  # Show first 50
            status = "✅" if actor_info['is_grid_label'] else "❌"
            pos = actor_info['position']
            lines.append(f"{status} {i}. {actor_info['text']}")
            lines.append(f"   Position: [{pos[0]:.1f}, {pos[1]:.1f}, {pos[2]:.1f}]")
            lines.append(f"   Type: {actor_info['actor_type']}")
            lines.append("")
        
        if len(text_actors) > 50:
            lines.append(f"... and {len(text_actors) - 50} more")
        
        report.setPlainText("\n".join(lines))
        layout.addWidget(report)
        
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.accept)
        layout.addWidget(close_btn)
        
        dialog.setLayout(layout)
        dialog.exec()

    def _find_grid_label_actor(self, grid_name):
        """Resolve the live vtk actor for a grid_name string.

        CurveTool.eventFilter (gui/curve_tools.py) only passes the grid_name
        string through to show_grid_label_menu, not the picked actor, so we
        re-locate the actor here the same way _check_grid_label_at_click's
        Method 3 fallback does: scan the known SNT/DXF actor stores for a
        pickable grid-label actor whose grid_name matches.
        """
        if not grid_name:
            return None
        try:
            for store_name in ('snt_actors', 'dxf_actors'):
                for data in getattr(self.app, store_name, []) or []:
                    for actor in data.get('actors', []) or []:
                        if not (hasattr(actor, 'is_grid_label') and actor.is_grid_label):
                            continue
                        if getattr(actor, 'grid_name', None) == grid_name:
                            return actor
        except Exception as e:
            print(f"⚠️ Could not resolve grid label actor for '{grid_name}': {e}")
        return None

    def show_grid_label_menu(self, grid_name, snt_filename=None):
        """
        Show context menu for a grid label.
        Called by CurveTool.eventFilter when it detects a right-click on a grid label.
        Extracted from on_right_click() so it can be called externally.
        """
        from PySide6.QtWidgets import QMenu, QMessageBox
        from PySide6.QtGui import QAction, QCursor

        if not grid_name:
            return

        menu = QMenu(self.app)
        menu.setStyleSheet("""
            QMenu {
                background-color: #2c2c2c;
                color: #f0f0f0;
                border: 1px solid #555;
                padding: 5px;
            }
            QMenu::item {
                padding: 8px 30px;
                border-radius: 3px;
            }
            QMenu::item:selected {
                background-color: #3c3c3c;
            }
        """)

        load_action = QAction("📂 Load Grid Data", self.app)
        clear_action = QAction("🧹 Clear Grid Data", self.app)

        if not snt_filename:
            snt_filename = self._infer_snt_filename_for_grid(grid_name)

        owner_label = self._resolve_grid_owner(grid_name)
        is_loaded = grid_name in self.loaded_grids

        if is_loaded:
            load_action.setText("📂 Reload Grid Data")
            point_count = len(self.loaded_grids[grid_name])
            clear_action.setText(f"🧹 Clear Grid ({point_count:,} pts)")
        else:
            clear_action.setEnabled(False)
            clear_action.setText("🧹 (Grid not loaded)")

        load_action.triggered.connect(
            lambda _=False, gn=grid_name, sf=snt_filename: self.load_grid_las(
                gn,
                snt_filename=sf,
                alt_names=self._get_alt_names_for_grid(gn),
            )
        )

        def confirm_clear():
            reply = QMessageBox.question(
                self.app,
                "Confirm Clear",
                f"Clear all points from grid:\n\n{grid_name}\n\n"
                f"Points will be removed from view.\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                self.clear_grid_data(grid_name)

        clear_action.triggered.connect(confirm_clear)
        fence_action = QAction("🔺 Load Points Inside Fence", self.app)
        fence_action.triggered.connect(self.activate_load_by_fence_tool)
        buffer_action = QAction("🧩 Open Block with Buffer Points", self.app)
        buffer_action.triggered.connect(
            lambda _=False, gn=grid_name, sf=snt_filename:
                self.open_grid_with_buffer_points(
                    gn, sf, self._get_alt_names_for_grid(gn)
                )
        )

        menu.addAction(load_action)
        menu.addAction(buffer_action)
        menu.addAction(clear_action)
        menu.addAction(fence_action)

        edit_actor = self._find_grid_label_actor(grid_name)
        if edit_actor is not None:
            from PySide6.QtCore import QTimer
            edit_label_action = QAction("✏️ Edit Label Text", self.app)
            # Defer the dialog by one event-loop tick (mirrors
            # digitize_tools._show_text_context_menu's _deferred_menu/QTimer
            # pattern). Opening a modal QDialog synchronously inside this
            # menu-action's triggered slot — itself already nested inside
            # the CurveTool eventFilter's synchronous mouse-press handling —
            # leaves VTK's render-window mouse grab from the original click
            # unreleased, so the dialog paints but never receives mouse
            # input. A short QTimer defer lets that grab release first.
            edit_label_action.triggered.connect(
                lambda _=False, a=edit_actor: QTimer.singleShot(
                    10, lambda a=a: self._edit_grid_label(a)
                )
            )
            menu.addSeparator()
            menu.addAction(edit_label_action)

            bulk_font_action = QAction("🔠 Set Font Size — All Block Labels", self.app)
            bulk_font_action.triggered.connect(
                lambda _=False: QTimer.singleShot(10, self.edit_all_grid_labels_font_size)
            )
            menu.addAction(bulk_font_action)

            if getattr(self, '_label_edit_undo_stack', None):
                undo_label_action = QAction("↶ Undo Last Label Edit", self.app)
                undo_label_action.triggered.connect(
                    lambda _=False: QTimer.singleShot(10, self._undo_last_label_edit)
                )
                menu.addAction(undo_label_action)

        menu.exec(QCursor.pos())

def add_grid_label_system_to_app(app):
        """Initialize grid label manager"""
        if not hasattr(app, 'grid_label_manager'):
            app.grid_label_manager = GridLabelManager(app)
            app.grid_label_manager.setup_interactor()
            print("✅ Grid label system activated")
            
