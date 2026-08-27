# import numpy as np
# import pyvista as pv
# from vtkmodules.util import numpy_support
# from typing import Optional, Dict
# import time
# import vtk
# import os
# import threading
# import concurrent.futures
# UNIFIED_ACTOR_NAME = "_naksha_unified_cloud"

# # ─────────────────────────────────────────────────────────────────────────────
# # CONSTANTS
# # ─────────────────────────────────────────────────────────────────────────────
# _BASE_POINT_SIZE = 2.5
# _BORDER_GROWTH_SCALE_PX = 4.0
# _BORDER_GROWTH_CUBIC_PX = 8.0
# _MAX_BORDER_GROWTH_PX = 8.0

# # ── Per-border-type depth biases ─────────────────────────────────────────────
# # Each border mode needs a different depth nudge because the geometry it draws
# # (ring thickness, point-size growth) differs substantially between modes.
# #
# #   PER-POINT   — every point gets a ring; small bias keeps ring flush with core.
# #   STRUCTURED  — only boundary-flagged points grow; larger bias ensures the
# #                 grown shell reliably occludes the smaller interior points.
# #   HYBRID      — all points grow AND boundary points paint a ring; the bias
# #                 just needs to separate the ring fragment from the core
# #                 fragment of the same (already-grown) point sprite.
# # _BORDER_DEPTH_BIAS_PERPOINT    = 0.0002   # was the stable value before hybrid was added
# # _BORDER_DEPTH_BIAS_STRUCTURED  = 0.001    # larger — boundary shell must cover interior pts
# # _BORDER_DEPTH_BIAS_HYBRID      = 0.00005  # tiny — ring is on the same grown sprite

# # NOTE: These are now *world-space* bias values in metres/units.
# # The shader converts them to NDC at draw time using the camera near/far.
# # This makes the bias camera-range-invariant (fixes border disappearing inside SNT/DXF grid
# # where VTK tightens the clipping range around the Z-offset scene).
# _BORDER_DEPTH_BIAS_PERPOINT_WORLD    = 0.05   # ~5cm world-space
# _BORDER_DEPTH_BIAS_STRUCTURED_WORLD  = 0.25   # ~25cm — boundary shell
# _BORDER_DEPTH_BIAS_HYBRID_WORLD      = 0.01   # ~1cm — ring on same sprite

# # Fixed pixel-width for structured (object-edge) borders — stays constant at any zoom
# _STRUCTURED_BORDER_PX = 2.0

# # Progressive LOD for large section actors.
# _LOD_FIRST_FRAME_MAX = 1_500_000
# _LOD_SKIP_THRESHOLD = 2_000_000
# _section_build_executor = concurrent.futures.ThreadPoolExecutor(
#     max_workers=2,
#     thread_name_prefix="naksha_sec",
# )
# _section_build_locks: dict = {}

# def _section_render_point_cap(app) -> int:
#     """
#     Optional manual render cap for section views.
#     Default behavior is uncapped (render all section points).
#     Set env var NAKSHA_SECTION_POINT_BUDGET to enable a manual cap.
#     """
#     raw = str(os.getenv("NAKSHA_SECTION_POINT_BUDGET", "")).replace(",", "").strip()
#     if not raw:
#         return None
#     try:
#         env_cap = int(raw)
#         if env_cap >= 100_000:
#             return int(env_cap)
#     except Exception:
#         pass

#     return None


# def _uniform_pick_indices(n_points: int, keep_count: int) -> np.ndarray:
#     if keep_count <= 0 or n_points <= 0:
#         return np.empty((0,), dtype=np.int64)
#     if keep_count >= n_points:
#         return np.arange(n_points, dtype=np.int64)
#     return np.linspace(0, n_points - 1, num=keep_count, dtype=np.int64)


# def _get_section_build_lock(view_idx: int) -> threading.Lock:
#     if view_idx not in _section_build_locks:
#         _section_build_locks[view_idx] = threading.Lock()
#     return _section_build_locks[view_idx]


# def _build_section_numpy_data(all_pts, all_cls, palette, border_percent,
#                               lut_builder_fn, boundary_fn):
#     cls_f32 = all_cls.astype(np.float32, copy=False)
#     rgb_buffer = lut_builder_fn(all_cls, palette)
#     if border_percent > 0.0:
#         bf = boundary_fn(all_pts, all_cls)
#     else:
#         bf = np.zeros(len(all_pts), dtype=np.float32)
#     return {
#         "pts": all_pts,
#         "cls_f32": cls_f32,
#         "rgb": rgb_buffer,
#         "bf": bf,
#     }


# def _build_vtk_actor_from_arrays(vtk_widget, actor_name, arrays, actual_pt_size,
#                                  palette, border_percent, slot_idx,
#                                  attach_shader_fn, ensure_mapper_fn,
#                                  view_shader_ctx_cls):
#     cloud = pv.PolyData(arrays["pts"])
#     class_vtk = numpy_support.numpy_to_vtk(arrays["cls_f32"], deep=True)
#     class_vtk.SetName("Classification")
#     cloud.GetPointData().AddArray(class_vtk)

#     bf_vtk = numpy_support.numpy_to_vtk(arrays["bf"], deep=True)
#     bf_vtk.SetName("BoundaryFlag")
#     cloud.GetPointData().AddArray(bf_vtk)

#     rgb_vtk = numpy_support.numpy_to_vtk(arrays["rgb"], deep=True)
#     rgb_vtk.SetName("RGB")
#     cloud.GetPointData().SetScalars(rgb_vtk)

#     actor = vtk_widget.add_points(
#         cloud, scalars="RGB", rgb=True,
#         point_size=actual_pt_size,
#         render_points_as_spheres=False,
#         name=actor_name,
#         reset_camera=False, render=False,
#     )
#     if actor is None:
#         return None, None, None, None, None, None, None, None
#     actor.GetProperty().LightingOff()

#     try:
#         actor._naksha_render_window = vtk_widget.render_window
#         actor._naksha_renderer = vtk_widget.renderer
#     except Exception:
#         pass

#     ensure_mapper_fn(actor, cloud)
#     sbm = actor.GetMapper()
#     mesh = sbm.GetInput() if sbm is not None else None
#     if mesh is None:
#         return None, None, None, None, None, None, None, None

#     vtk_ca = mesh.GetPointData().GetScalars()
#     if vtk_ca is None:
#         return None, None, None, None, None, None, None, None

#     _vtk_rgb = numpy_support.vtk_to_numpy(vtk_ca)
#     np.copyto(_vtk_rgb, arrays["rgb"])
#     vtk_ca.Modified()

#     _class_vtk_arr = mesh.GetPointData().GetArray("Classification")
#     _vtk_cls = (numpy_support.vtk_to_numpy(_class_vtk_arr)
#                 if _class_vtk_arr is not None else arrays["cls_f32"].copy())

#     ctx = view_shader_ctx_cls(slot_idx=slot_idx)
#     ctx.load_from_palette(palette, border_percent, actual_pt_size)
#     attach_shader_fn(actor, ctx, actor_name)

#     return actor, ctx, mesh, vtk_ca, _vtk_rgb, _vtk_cls, class_vtk, bf_vtk


# def _wire_actor_metadata(actor, mesh, vtk_ca, _vtk_rgb, _vtk_cls, class_vtk,
#                          bf_vtk, combined_global_mask, actual_pt_size):
#     actor._naksha_rgb_ptr = _vtk_rgb
#     actor._naksha_vtk_array = vtk_ca
#     actor._naksha_vtk_rgb_ref = vtk_ca
#     actor._naksha_class_vtk_ref = class_vtk
#     actor._naksha_mesh = mesh
#     actor._naksha_section_class = _vtk_cls
#     actor._naksha_section_mask = combined_global_mask
#     actor._naksha_global_to_local_arr = None
#     actor._naksha_global_indices = None
#     actor._naksha_base_point_size = actual_pt_size
#     actor._naksha_boundary_vtk = bf_vtk


# # ─────────────────────────────────────────────────────────────────────────────
# # VIEW SHADER CONTEXT
# # ─────────────────────────────────────────────────────────────────────────────
# class ViewShaderContext:
#     __slots__ = (
#         "slot_idx", "visibility_mask", "weight_lut", "color_lut",
#         "border_ring", "_fingerprint", "_observer_id", "_generation",
#         "_has_vertex_attr_cache",
#         "_vis_list_cache", "_wt_list_cache", "structured_border_mode",
#     )

#     def __init__(self, slot_idx: int = 0):
#         self.slot_idx            = slot_idx
#         self.visibility_mask     = np.ones(256, dtype=np.float32)
#         self.weight_lut          = np.full(256, _BASE_POINT_SIZE, dtype=np.float32)
#         self.color_lut           = np.full(256 * 3, 0.5, dtype=np.float32)
#         self.border_ring         = np.float32(0.0)
#         self._fingerprint: Optional[int] = None
#         self._observer_id: Optional[int] = None
#         self._generation: int    = 0
#         self._vis_list_cache     = None
#         self._wt_list_cache      = None
#         self._has_vertex_attr_cache = False
#         self.structured_border_mode = 0.0

#     def load_from_palette(self, palette: dict, border_percent: float = 0.0,
#                           base_point_size: float = _BASE_POINT_SIZE) -> bool:
#         palette = palette or {}
#         fp = _palette_fingerprint_full(palette, border_percent)
#         if fp == self._fingerprint:
#             return False
#         self._fingerprint = fp

#         self.visibility_mask[:] = 1.0
#         self.weight_lut[:] = base_point_size
#         self.color_lut[:] = 0.5

#         for code, info in palette.items():
#             idx = int(code)
#             if idx < 0 or idx >= 256:
#                 continue
#             self.visibility_mask[idx] = 1.0 if info.get("show", True) else 0.0
#             raw_weight = float(info.get("weight", 1.0))
#             clamped_weight = max(0.1, min(raw_weight, 12.0))
#             self.weight_lut[idx] = compute_point_size(clamped_weight, base_point_size)
#             r, g, b = info.get("color", (128, 128, 128))
#             base = idx * 3
#             self.color_lut[base]     = r / 255.0
#             self.color_lut[base + 1] = g / 255.0
#             self.color_lut[base + 2] = b / 255.0

#         self.border_ring = np.float32(min(1.0, max(0.0, border_percent / 100.0)))
#         self._generation += 1
#         self._vis_list_cache = None
#         self._wt_list_cache  = None
#         return True

#     def force_reload(self):
#         self._fingerprint    = None
#         self._vis_list_cache = None
#         self._wt_list_cache  = None

#     def vis_as_list(self):
#         if self._vis_list_cache is None:
#             self._vis_list_cache = self.visibility_mask.tolist()
#         return self._vis_list_cache

#     def wt_as_list(self):
#         if self._wt_list_cache is None:
#             self._wt_list_cache = self.weight_lut.tolist()
#         return self._wt_list_cache

#     def clone_for_view(self, new_slot_idx: int) -> 'ViewShaderContext':
#         ctx = ViewShaderContext(new_slot_idx)
#         np.copyto(ctx.visibility_mask, self.visibility_mask)
#         np.copyto(ctx.weight_lut,      self.weight_lut)
#         np.copyto(ctx.color_lut,       self.color_lut)
#         ctx.border_ring             = self.border_ring
#         ctx._fingerprint            = self._fingerprint
#         ctx._has_vertex_attr_cache  = self._has_vertex_attr_cache
#         ctx.structured_border_mode  = self.structured_border_mode
#         return ctx


# def _palette_fingerprint_full(palette: dict, border_percent: float = 0.0) -> int:
#     try:
#         return hash((
#             tuple(
#                 (k, v.get("show", True),
#                  tuple(v.get("color", (128, 128, 128))),
#                  round(float(v.get("weight", 1.0)), 3))
#                 for k, v in sorted(palette.items())
#             ),
#             round(border_percent, 2),
#         ))
#     except Exception:
#         return id(palette) ^ int(border_percent * 100)


# # ─────────────────────────────────────────────────────────────────────────────
# # SHADER REGISTRY
# # ─────────────────────────────────────────────────────────────────────────────
# _shader_contexts: Dict[str, ViewShaderContext] = {}


# def get_shader_context(actor_name: str) -> Optional[ViewShaderContext]:
#     return _shader_contexts.get(actor_name)


# def _safe_direct_render(vtk_widget) -> bool:
#     """Bypass GPURenderManager throttling for a commit-time repaint."""
#     if vtk_widget is None:
#         return False
#     try:
#         wrapped = getattr(vtk_widget, "render", None)
#         original = None
#         mgr = None
#         try:
#             from gui.gpu_render_manager import GPURenderManager
#             original = getattr(vtk_widget, "render", None)
#             mgr = getattr(vtk_widget, "_naksha_gpu_render_manager", None)
#             if mgr is not None and hasattr(mgr, "_original_render") and mgr._original_render is not None:
#                 original = mgr._original_render
#         except Exception:
#             original = None

#         if original is not None and original is not wrapped:
#             original()
#             return True

#         rw = vtk_widget.GetRenderWindow() if hasattr(vtk_widget, "GetRenderWindow") else None
#         if rw is None:
#             return False
#         rw.Render()
#         return True
#     except Exception:
#         return False


# def guarantee_main_view_visual_refresh(app, changed_mask, to_class=None, reason="",
#                                        old_classes=None, new_classes=None,
#                                        origin_view="unknown") -> bool:
#     """
#     Commit-time safety net for main-view visual flushes.

#     Validates the changed mask, refreshes the active main actor in-place, forces
#     VTK dirty flags, and performs one immediate non-throttled render.
#     """
#     if app is None or not hasattr(app, "data") or not app.data:
#         return False

#     classification = app.data.get("classification")
#     if classification is None:
#         return False

#     changed_mask = np.asarray(changed_mask) if changed_mask is not None else None
#     if changed_mask is None or changed_mask.dtype != bool:
#         return False
#     if changed_mask.ndim != 1 or len(changed_mask) != len(classification):
#         return False

#     changed_count = int(np.count_nonzero(changed_mask))
#     if changed_count <= 0:
#         return False

#     changed_indices = np.flatnonzero(changed_mask).astype(np.int64, copy=False)
#     app._last_changed_mask = changed_mask
#     app._last_changed_indices = changed_indices.copy()

#     display_mode = str(getattr(app, "display_mode", "class") or "class").lower()
#     fast_update = False
#     dirty_flags = False
#     render_ok = False
#     actor = None

#     try:
#         if display_mode == "class":
#             # A commit can contain values produced by a height/fence operation,
#             # undo/redo, or another batched classifier.  ``app.to_class`` is a
#             # persistent shortcut selection and can therefore be stale here.
#             # Patch both GPU buffers from the canonical classification array;
#             # never repaint a committed mask from the shortcut hint.
#             actor = _get_unified_actor(app)
#             if actor is not None:
#                 global_indices = getattr(app, "_main_global_indices", None)
#                 if global_indices is not None:
#                     local_changed = np.flatnonzero(changed_mask[global_indices])
#                 else:
#                     local_changed = changed_indices
#                 if local_changed.size > 0:
#                     _patch_actor_memory(app, actor, local_changed, slot_idx=0)
#                 fast_update = True
#         elif display_mode == "shaded_class":
#             try:
#                 from gui.shading_display import (
#                     ClassificationDelta,
#                     refresh_shaded_after_classification_fast,
#                 )

#                 delta = None
#                 if old_classes is not None and new_classes is not None:
#                     delta = ClassificationDelta(
#                         changed_indices=changed_indices,
#                         old_classes=np.asarray(old_classes),
#                         new_classes=np.asarray(new_classes),
#                         operation=reason or "classification",
#                         origin_view=origin_view,
#                     )
#                 elif getattr(app, "_pending_shading_delta", None) is not None:
#                     pending = app._pending_shading_delta
#                     pending_idx = np.asarray(
#                         pending.changed_indices, dtype=np.int64
#                     ).ravel()
#                     if np.array_equal(pending_idx, changed_indices):
#                         delta = pending
#                         app._pending_shading_delta = None
#                 refresh_shaded_after_classification_fast(
#                     app, changed_mask, delta=delta
#                 )
#                 actor = getattr(app, "_shaded_mesh_actor", None)
#                 fast_update = True
#             except Exception:
#                 fast_update = False
#         elif display_mode == "surface":
#             try:
#                 refresh_fn = getattr(app, "refresh_surface_after_classification", None)
#                 if callable(refresh_fn):
#                     refresh_fn(reason="main_view_commit", changed_mask=changed_mask)
#                 else:
#                     from gui.surface_mode import refresh_surface_after_classification
#                     refresh_surface_after_classification(app, changed_mask, operation="main_view_commit", delay_ms=0)
#                 fast_update = True
#             except Exception:
#                 fast_update = False

#         actor = actor or _get_unified_actor(app)
#         if actor is not None:
#             mesh = getattr(actor, "_naksha_mesh", None)
#             vtk_ca = getattr(actor, "_naksha_vtk_array", None)
#             mapper = actor.GetMapper() if hasattr(actor, "GetMapper") else None
#             if vtk_ca is not None:
#                 vtk_ca.Modified()
#                 dirty_flags = True
#             if mesh is not None:
#                 mesh.GetPointData().Modified()
#                 mesh.Modified()
#                 dirty_flags = True
#             if mapper is not None:
#                 mapper.Modified()
#                 dirty_flags = True
#             actor.Modified()
#             dirty_flags = True
#     except Exception as e:
#         print(f"⚠️ guarantee_main_view_visual_refresh prep failed: {e}")

#     vtk_widget = getattr(app, "vtk_widget", None)
#     # A shaded local patch schedules a render for direct callers. This commit
#     # is about to render synchronously, so cancel that pending duplicate.
#     pending_present = getattr(app, "_shading_present_timer", None)
#     if pending_present is not None:
#         try:
#             pending_present.stop()
#             pending_present.deleteLater()
#         except Exception:
#             pass
#         app._shading_present_timer = None
#     render_started = time.perf_counter()
#     if display_mode not in ("class", "shaded_class", "surface"):
#         render_ok = _safe_direct_render(vtk_widget)
#     else:
#         render_ok = _safe_direct_render(vtk_widget)
#     render_ms = (time.perf_counter() - render_started) * 1000.0

#     if render_ok:
#         app._gpu_sync_done = True
#         app._section_visibility_refresh_required = bool(
#             getattr(app, "_section_visibility_refresh_required", False)
#         )

#     commit_id = int(getattr(app, "_main_refresh_commit_id", 0)) + 1
#     app._main_refresh_commit_id = commit_id
#     if commit_id <= 20:
#         print(
#             f"MAIN_REFRESH_GUARANTEE reason={reason} mode={display_mode} "
#             f"changed={changed_count} fast_update={bool(fast_update)} "
#             f"dirty_flags={bool(dirty_flags)} render={bool(render_ok)} "
#             f"render_ms={render_ms:.1f} "
#             f"gpu_sync_done_before={bool(getattr(app, '_gpu_sync_done', False))}"
#         )

#     return bool(render_ok)


# # ─────────────────────────────────────────────────────────────────────────────
# # HELPERS — defined first so every function below can call them safely
# # ─────────────────────────────────────────────────────────────────────────────

# def _mark_actor_dirty(actor) -> None:
#     """Full VTK dirty chain for any in-place buffer modification."""
#     mesh = getattr(actor, '_naksha_mesh', None)
#     if mesh is None:
#         return
#     mesh.GetPointData().Modified()
#     mesh.Modified()
#     mapper = actor.GetMapper()
#     if mapper is not None:
#         mapper.Modified()
#     actor.Modified()


# def _rewrite_rgb_from_palette(rgb_ptr: np.ndarray, classification: np.ndarray,
#                                palette: dict):
#     """Full buffer re-write (O(N)). Use only for initial load or full palette shifts."""
#     max_c = max(int(classification.max()) + 1, 256)
#     lut = _get_lut_array(palette, max_c)
#     np.copyto(rgb_ptr, lut[classification.clip(0, max_c - 1).astype(np.intp)])

# def _rewrite_rgb_partial(rgb_ptr: np.ndarray, classes_to_apply: np.ndarray,
#                          palette: dict, local_indices: np.ndarray):
#     """Partial buffer update (O(M) where M is changed points). Critical for 48M+ point sets."""
#     if local_indices is None or len(local_indices) == 0:
#         return
#     max_c = max(int(classes_to_apply.max()) + 1, 256)
#     lut = _get_lut_array(palette, max_c)
#     changed_classes = classes_to_apply.clip(0, max_c - 1).astype(np.intp)
#     rgb_ptr[local_indices] = lut[changed_classes]

# def _get_lut_array(palette: dict, max_c: int) -> np.ndarray:
#     """Helper to build color lookup table."""
#     lut = np.full((max_c, 3), 128, dtype=np.uint8)
#     for code, info in palette.items():
#         idx = int(code)
#         if 0 <= idx < max_c:
#             lut[idx] = (info.get("color", (128, 128, 128))
#                         if info.get("show", True) else (0, 0, 0))
#     return lut


# def _touch_vtk_arrays(actor):
#     vtk_ca = getattr(actor, '_naksha_vtk_array', None)
#     if vtk_ca:
#         vtk_ca.Modified()
#     _mark_actor_dirty(actor)


# def _is_writable(arr: np.ndarray) -> bool:
#     return arr.flags.writeable


# def _apply_border_once(actor, border_percent: float):
#     """
#     Fallback border shader for per-class actors that bypass
#     _attach_view_shader_context.
#     """
#     if border_percent <= 0:
#         return

#     ctx = getattr(actor, '_naksha_shader_ctx', None)
#     if ctx is not None:
#         new_ring = np.float32(min(0.50, max(0.0, border_percent / 100.0)))
#         if ctx.border_ring != new_ring:
#             ctx.border_ring = new_ring
#             ctx._fingerprint = None
#         return

#     cached = getattr(actor, "_naksha_border_percent", None)
#     if cached == border_percent:
#         return
#     try:
#         ring_val = min(1.0, max(0.0, border_percent / 100.0))
#         sp        = actor.GetShaderProperty()
#         if sp is None:
#             return

#         if ring_val <= 0.001:
#             frag_code = (
#                 "//VTK::Color::Impl\n"
#                 "if (length(gl_PointCoord.xy - vec2(0.5)) * 2.0 > 1.0) discard;\n"
#                 "opacity = 1.0;\n"
#             )
#         else:
#             ring_frac = min(0.25, ring_val * 0.5)
#             inner     = 1.0 - ring_frac
#             frag_code = (
#                 "//VTK::Color::Impl\n"
#                 "// Naksha per-class fallback border (round-circle)\n"
#                 "float r_pc = length(gl_PointCoord.xy - vec2(0.5)) * 2.0;\n"
#                 "if (r_pc > 1.0) discard;\n"
#                 f"if (r_pc >= {inner:.6f}) {{\n"
#                 "    diffuseColor = vec3(0.0, 0.0, 0.0);\n"
#                 "    ambientColor = vec3(0.0, 0.0, 0.0);\n"
#                 "}\n"
#                 "opacity = 1.0;\n"
#             )

#         sp.ClearAllFragmentShaderReplacements()
#         sp.AddFragmentShaderReplacement("//VTK::Color::Impl", True, frag_code, False)
#         sp.Modified()
#         actor.GetProperty().Modified()
#         mapper = actor.GetMapper()
#         if mapper:
#             mapper.Modified()
#         actor.Modified()
#         actor._naksha_border_percent = border_percent

#     except Exception as e:
#         print(f"      ⚠️ _apply_border_once failed: {e}")


# def update_visibility_lut(actor, palette, base_point_size=_BASE_POINT_SIZE):
#     ctx = getattr(actor, '_naksha_shader_ctx', None)
#     if ctx is not None:
#         ctx.force_reload()
#         ctx.load_from_palette(palette, float(ctx.border_ring * 100.0), base_point_size)
#         _push_uniforms_direct(actor, ctx)
#         return
#     vis_arr = np.zeros(256, dtype=np.float32)
#     for i in range(256):
#         info = (palette or {}).get(i, {})
#         vis_arr[i] = (compute_point_size(info.get("weight", 1.0), base_point_size)
#                       if info.get("show", True) else 0.0)
#     actor._local_vis_arr = vis_arr
#     try:
#         actor.GetMapper().Modified()
#     except Exception:
#         pass


# # ─────────────────────────────────────────────────────────────────────────────
# # GL_PROGRAM_POINT_SIZE — persistent observer helpers
# # ─────────────────────────────────────────────────────────────────────────────

# def _try_enable_program_point_size(render_window) -> bool:
#     """
#     Enable GL_PROGRAM_POINT_SIZE (0x8642) so vertex shaders can write gl_PointSize.
#     Must be called AFTER the OpenGL context is initialized (after at least one render).
#     Returns True if the state object was available and the call succeeded.
#     """
#     if render_window is None:
#         return False
#     try:
#         if hasattr(render_window, 'GetState'):
#             state = render_window.GetState()
#             if state and hasattr(state, 'vtkglEnable'):
#                 state.vtkglEnable(0x8642)          # GL_PROGRAM_POINT_SIZE
#                 return True
#     except Exception as e:
#         print(f"      ⚠️ GL_PROGRAM_POINT_SIZE enable: {e}")
#     return False


# def _install_program_point_size_observer(actor, render_window) -> bool:
#     """
#     Install a StartEvent observer on the render window so that
#     GL_PROGRAM_POINT_SIZE is re-enabled before EVERY draw call.

#     VTK's state machine can reset GL flags between renders. This observer
#     is the only guaranteed way to keep the flag set persistently.

#     Safe to call multiple times — the guard flag prevents duplicate observers.
#     """
#     if render_window is None or getattr(render_window, '_naksha_pps_observer_installed', False):
#         return getattr(render_window, '_naksha_pps_observer_installed', False)

#     def _pps_start_event(caller, event):
#         try:
#             state = caller.GetState()
#             if state and hasattr(state, 'vtkglEnable'):
#                 state.vtkglEnable(0x8642)
#         except Exception:
#             pass

#     try:
#         obs_id = render_window.AddObserver('StartEvent', _pps_start_event)
#         render_window._naksha_pps_observer_installed = True
#         render_window._naksha_pps_observer_id        = obs_id
#         print("      ✅ GL_PROGRAM_POINT_SIZE StartEvent observer installed ON WINDOW")
#         return True
#     except Exception as e:
#         print(f"      ⚠️ PPS observer install failed: {e}")
#         return False


# def _deferred_actor_gpu_init(actor, ctx, plotter, label: str = "actor"):
#     """
#     Called via QTimer.singleShot(~500 ms) after build_unified_actor /
#     build_section_unified_actor.

#     By the time this fires the VTK window has rendered at least once, so
#     GetState() is guaranteed to return a valid OpenGL state object.  We:

#       1. Enable GL_PROGRAM_POINT_SIZE immediately (one-shot).
#       2. Install the persistent StartEvent observer so it stays enabled.
#       3. Re-push all GPU uniforms (weight_lut, visibility_lut, border_ring_val).
#       4. Trigger one more render so the updated point sizes appear.
#     """
#     try:
#         rw = getattr(actor, '_naksha_render_window', None)
#         if rw is None:
#             try:
#                 rw = plotter.render_window
#                 actor._naksha_render_window = rw
#             except Exception:
#                 pass

#         enabled = _try_enable_program_point_size(rw)
#         _install_program_point_size_observer(actor, rw)

#         if enabled:
#             actor._naksha_needs_program_point_size = False
#             print(f"      ✅ Deferred GPU init: GL_PROGRAM_POINT_SIZE enabled for {label}")
#         else:
#             print(f"      ⚠️ Deferred GPU init: GL state still unavailable for {label}")

#         _push_uniforms_direct(actor, ctx)

#         try:
#             plotter.render()
#         except Exception:
#             pass

#     except Exception as e:
#         print(f"      ⚠️ _deferred_actor_gpu_init ({label}): {e}")


# # ─────────────────────────────────────────────────────────────────────────────
# # CORE: PUSH UNIFORMS DIRECT
# # ─────────────────────────────────────────────────────────────────────────────
# def _push_uniforms_direct(actor, ctx: 'ViewShaderContext') -> bool:
#     if actor is None or ctx is None:
#         return False
#     try:
#         rw = getattr(actor, '_naksha_render_window', None)

#         if rw:
#             if not getattr(rw, '_naksha_pps_observer_installed', False):
#                 if _try_enable_program_point_size(rw):
#                     _install_program_point_size_observer(actor, rw)
#                     actor._naksha_needs_program_point_size = False
#                     print("      ✅ GL_PROGRAM_POINT_SIZE enabled via _push_uniforms_direct")
#                 else:
#                     actor._naksha_needs_program_point_size = True
#             else:
#                 actor._naksha_needs_program_point_size = False

#         sp = actor.GetShaderProperty()
#         if sp is None:
#             return False

#         attached_ctx = getattr(actor, '_naksha_shader_ctx', ctx)
#         has_vertex   = attached_ctx._has_vertex_attr_cache

#         if has_vertex:
#             v_uni = sp.GetVertexCustomUniforms()
#             if v_uni:
#                 v_uni.SetUniform1fv("visibility_lut", 256, ctx.vis_as_list())
#                 v_uni.SetUniform1fv("weight_lut",     256, ctx.wt_as_list())
#                 v_uni.SetUniformf("border_ring_val", float(ctx.border_ring))
#                 v_uni.SetUniformf("structured_border_mode", float(getattr(ctx, 'structured_border_mode', 0.0)))
#                 # Pass camera near/far so fragment shader can compute camera-range-invariant depth bias
#                 try:
#                     renderer = actor.GetMapper().GetInput() and None  # unused
#                     ren = getattr(actor, '_naksha_renderer', None)
#                     if ren is not None:
#                         cam = ren.GetActiveCamera()
#                         cr = cam.GetClippingRange()
#                         v_uni.SetUniformf("naksha_near", float(cr[0]))
#                         v_uni.SetUniformf("naksha_far",  float(cr[1]))
#                     else:
#                         v_uni.SetUniformf("naksha_near", 0.1)
#                         v_uni.SetUniformf("naksha_far",  100000.0)
#                 except Exception:
#                     v_uni.SetUniformf("naksha_near", 0.1)
#                     v_uni.SetUniformf("naksha_far",  100000.0)
#                 v_uni.Modified()
#                 sp.Modified()

#         f_uni = sp.GetFragmentCustomUniforms()
#         if f_uni:
#             f_uni.SetUniformf("border_ring_val", float(ctx.border_ring))
#             f_uni.SetUniformf("structured_border_mode", float(getattr(ctx, 'structured_border_mode', 0.0)))

#         actor.GetMapper().Modified()
#         actor.GetProperty().Modified()
#         return True

#     except Exception as e:
#         print(f"⚠️ _push_uniforms_direct failed: {e}")
#         return False


# _push_shader_uniforms = _push_uniforms_direct


# # ─────────────────────────────────────────────────────────────────────────────
# # SHADER ATTACHMENT — called ONCE at actor build time
# # ─────────────────────────────────────────────────────────────────────────────
# def _attach_view_shader_context(actor, ctx, actor_name, use_sphere_shaders=True):
#     if actor is None:
#         return

#     actor._naksha_shader_ctx = ctx
#     _shader_contexts[actor_name] = ctx

#     mesh = getattr(actor, '_naksha_mesh', None)
#     _ensure_opengl_polydata_mapper(actor, mesh)

#     vertex_attr_wired = False
#     try:
#         mapper = actor.GetMapper()
#         raw_m  = mapper.GetMapper() if hasattr(mapper, 'GetMapper') else mapper
#         raw_m.MapDataArrayToVertexAttribute(
#             "class_code", "Classification",
#             vtk.vtkDataObject.FIELD_ASSOCIATION_POINTS, -1
#         )
#         raw_m.MapDataArrayToVertexAttribute(
#             "boundary_flag", "BoundaryFlag",
#             vtk.vtkDataObject.FIELD_ASSOCIATION_POINTS, -1
#         )
#         vertex_attr_wired = True
#         print(f"      🔗 Linked 'Classification' + 'BoundaryFlag' to shader")
#     except Exception as e:
#         print(f"      ⚠️ Shader Attribute Mapping failed: {e}")

#     ctx._has_vertex_attr_cache = vertex_attr_wired
#     actor.GetProperty().SetRenderPointsAsSpheres(False)

#     sp = actor.GetShaderProperty()

#     if not hasattr(actor, "_shaders_finalized_v28"):
#         sp.ClearAllVertexShaderReplacements()
#         sp.ClearAllFragmentShaderReplacements()

#         # ── Vertex declarations ──────────────────────────────────────────────
#         sp.AddVertexShaderReplacement(
#             "//VTK::PositionVC::Dec", True,
#             "//VTK::PositionVC::Dec\n"
#             "in  float class_code;\n"
#             "in  float boundary_flag;\n"
#             "out float v_point_size;\n"
#             "out float v_core_size;\n"
#             "out float v_boundary;\n",
#             False
#         )

#         # ── Vertex implementation ────────────────────────────────────────────
#         # Vertex logic is the same for all three modes — the difference is only
#         # which points GROW (all vs boundary-only) which is already branched on
#         # structured_border_mode.  No depth work happens here.
#         sp.AddVertexShaderReplacement(
#             "//VTK::PositionVC::Impl", True,
#             "//VTK::PositionVC::Impl\n"
#             "  int c_idx = clamp(int(class_code + 0.5), 0, 255);\n"
#             "  v_boundary = boundary_flag;\n"
#             "  if (visibility_lut[c_idx] <= 0.0) {\n"
#             "    gl_Position  = vec4(2.0, 2.0, 2.0, 1.0);\n"
#             "    gl_PointSize = 0.0;\n"
#             "    v_point_size = 0.0;\n"
#             "    v_core_size  = 0.0;\n"
#             "  } else {\n"
#             "    float ps = max(1.0, weight_lut[c_idx]);\n"
#             "    if (structured_border_mode > 1.5) {\n"
#             "      // HYBRID: every point grows (for the border ring on all points)\n"
#             f"      float border_growth = clamp((border_ring_val * {_BORDER_GROWTH_SCALE_PX:.1f}) + (border_ring_val * border_ring_val * border_ring_val * {_BORDER_GROWTH_CUBIC_PX:.1f}), 0.0, {_MAX_BORDER_GROWTH_PX:.1f});\n"
#             "      float total_ps = ps + border_growth;\n"
#             "      gl_PointSize = total_ps;\n"
#             "      v_point_size = total_ps;\n"
#             "      v_core_size  = ps;\n"
#             "    } else if (structured_border_mode > 0.5) {\n"
#             "      // STRUCTURED: only boundary-flagged points grow\n"
#             f"      float border_growth = (boundary_flag > 0.5)\n"
#             f"        ? clamp(\n"
#             f"            (border_ring_val * {_BORDER_GROWTH_SCALE_PX:.1f})\n"
#             f"            + (border_ring_val * border_ring_val * border_ring_val * {_BORDER_GROWTH_CUBIC_PX:.1f}),\n"
#             f"            0.0, {_MAX_BORDER_GROWTH_PX:.1f}\n"
#             f"          )\n"
#             "        : 0.0;\n"
#             "      float total_ps = ps + border_growth;\n"
#             "      gl_PointSize = total_ps;\n"
#             "      v_point_size = total_ps;\n"
#             "      v_core_size  = ps;\n"
#             "    } else {\n"
#             "      // PER-POINT: every point grows uniformly\n"
#             f"      float border_growth = clamp((border_ring_val * {_BORDER_GROWTH_SCALE_PX:.1f}) + (border_ring_val * border_ring_val * border_ring_val * {_BORDER_GROWTH_CUBIC_PX:.1f}), 0.0, {_MAX_BORDER_GROWTH_PX:.1f});\n"
#             "      float total_ps = ps + border_growth;\n"
#             "      gl_PointSize = total_ps;\n"
#             "      v_point_size = total_ps;\n"
#             "      v_core_size  = ps;\n"
#             "    }\n"
#             "  }\n",
#             False
#         )

#         # ── Fragment declarations ────────────────────────────────────────────
#         sp.AddFragmentShaderReplacement(
#             "//VTK::Color::Dec", True,
#             "//VTK::Color::Dec\n"
#             "in float v_point_size;\n"
#             "in float v_core_size;\n"
#             "in float v_boundary;\n"
#             "uniform float naksha_near;\n"
#             "uniform float naksha_far;\n"
#             "float world_to_ndc_bias(float world_bias) {\n"
#             "  float z_ndc = gl_FragCoord.z;\n"
#             "  float range = naksha_far - naksha_near;\n"
#             "  if (range < 0.001) return 0.0;\n"
#             "  return world_bias / range;\n"
#             "}\n",
#             False
#         )

#         # ── Fragment implementation — each mode uses its own depth bias ──────
#         #
#         #   All points have a grown sprite; the outer annulus is painted black
#         #   the ring fragment and the core fragment come from the same sprite —
#         #   they are already at almost identical depth.
#         #
#         #   Only boundary points are grown.  Their black shell must reliably
#         #   occlude the smaller un-grown interior points behind them.
#         #
#         # PER-POINT   (else)
#         #   Every point grows uniformly so the ring sits flush with its own
#         #   core fragment.  Bias = _BORDER_DEPTH_BIAS_PERPOINT (medium).
#         sp.AddFragmentShaderReplacement(
#             "//VTK::Color::Impl", True,
#             "//VTK::Color::Impl\n"
#             "vec2  uv25    = gl_PointCoord.xy - vec2(0.5);\n"
#             "float dist_px = length(uv25) * v_point_size;\n"
#             "if (dist_px > v_point_size * 0.5) discard;\n"
#             "\n"
#             "if (border_ring_val > 0.001) {\n"
#             "  if (structured_border_mode > 1.5) {\n"
#             "    // ── HYBRID: ring on every point, tiny depth nudge ──────────\n"
#             "    if (dist_px >= v_core_size * 0.5) {\n"
#             "      diffuseColor = vec3(0.0);\n"
#             "      ambientColor = vec3(0.0);\n"
#             f"      gl_FragDepth = clamp(gl_FragCoord.z + world_to_ndc_bias({_BORDER_DEPTH_BIAS_HYBRID_WORLD}), 0.0, 1.0);\n"
#             "    } else {\n"
#             "      gl_FragDepth = gl_FragCoord.z;\n"
#             "    }\n"
#             "  } else if (structured_border_mode > 0.5) {\n"
#             "    // ── STRUCTURED: ring only on boundary points, larger nudge ─\n"
#             "    if (v_boundary > 0.5) {\n"
#             "      if (dist_px >= v_core_size * 0.5) {\n"
#             "        diffuseColor = vec3(0.0);\n"
#             "        ambientColor = vec3(0.0);\n"
#             f"        gl_FragDepth = clamp(gl_FragCoord.z + world_to_ndc_bias({_BORDER_DEPTH_BIAS_STRUCTURED_WORLD}), 0.0, 1.0);\n"
#             "      } else {\n"
#             "        gl_FragDepth = gl_FragCoord.z;\n"
#             "      }\n"
#             "    } else {\n"
#             "      // interior point — no ring, no depth shift\n"
#             "      gl_FragDepth = gl_FragCoord.z;\n"
#             "    }\n"
#             "  } else {\n"
#             "    // ── PER-POINT: ring on every point, medium depth nudge ─────\n"
#             "    if (dist_px >= v_core_size * 0.5) {\n"
#             "      diffuseColor = vec3(0.0);\n"
#             "      ambientColor = vec3(0.0);\n"
#             f"      gl_FragDepth = clamp(gl_FragCoord.z + world_to_ndc_bias({_BORDER_DEPTH_BIAS_PERPOINT_WORLD}), 0.0, 1.0);\n"
#             "    } else {\n"
#             "      gl_FragDepth = gl_FragCoord.z;\n"
#             "    }\n"
#             "  }\n"
#             "} else {\n"
#             "  gl_FragDepth = gl_FragCoord.z;\n"
#             "}\n"
#             "opacity = 1.0;\n",
#             False
#         )

#         actor._shaders_finalized_v25 = True
#         actor._shaders_finalized_v26 = True
#         actor._shaders_finalized_v27 = True
#         actor._shaders_finalized_v28 = True
#         print(f"      ✅ GPU Shader v27 (per-mode depth bias): {actor_name}")

#     actor._naksha_needs_program_point_size = True
#     actor.GetProperty().Modified()
#     _push_uniforms_direct(actor, ctx)

# def _refresh_actor_boundary_flags(actor, reason="structured-border"):
#     """
#     Structured border depends on BoundaryFlag.
#     Recompute it from the actor's current Classification array after AI / fast refresh.
#     """
#     try:
#         if actor is None:
#             return False

#         mesh = getattr(actor, "_naksha_mesh", None)
#         if mesh is None or mesh.GetPoints() is None:
#             return False

#         point_data = mesh.GetPointData()
#         if point_data is None:
#             return False

#         cls_arr = point_data.GetArray("Classification")
#         if cls_arr is None:
#             return False

#         pts_vtk = mesh.GetPoints().GetData()
#         xyz = numpy_support.vtk_to_numpy(pts_vtk)
#         cls = numpy_support.vtk_to_numpy(cls_arr).astype(np.int32, copy=False)

#         if len(xyz) == 0 or len(xyz) != len(cls):
#             return False

#         cls_mtime = int(cls_arr.GetMTime())
#         cached_mtime = getattr(actor, "_naksha_boundary_class_mtime", None)
#         cached_count = getattr(actor, "_naksha_boundary_point_count", None)

#         if cached_count == len(cls):
#             # Sampled checksum — detects real class-value changes without being
#             # invalidated by VTK MTime bumps from palette/border/display operations.
#             # Sampling 1-in-2000 of 13M pts ≈ 6500 elements, ~0.1 ms, zero false
#             # negatives for any brush/polygon/line classification stroke.
#             _step = max(1, len(cls) // 2000)
#             _checksum = int(cls[::_step].sum())
#             if getattr(actor, "_naksha_boundary_checksum", None) == _checksum:
#                 actor._naksha_boundary_class_mtime = cls_mtime  # keep MTime in sync
#                 return True

#         # Cache miss: classification data changed. Run the expensive recompute in a
#         # background thread so this call returns immediately with the current
#         # (stale-but-valid) BoundaryFlag data. The background thread patches the
#         # VTK array and fires a re-render when done.
#         if getattr(actor, "_naksha_bf_building", False):
#             # Background compute already in flight — use stale flags for this render.
#             return True

#         actor._naksha_bf_building = True
#         _step          = max(1, len(cls) // 2000)
#         _new_checksum  = int(cls[::_step].sum())
#         # xyz: copy so the background thread is safe if the user loads a new file
#         # cls: .astype(np.int32) above already produced a fresh array — no extra copy
#         xyz_bg     = xyz.copy()
#         cls_bg     = cls
#         _actor_id  = id(actor)
#         _mesh_ref  = mesh
#         _reason    = reason
#         _cls_mtime = cls_mtime

#         def _bg_compute():
#             try:
#                 bf = _compute_boundary_flags(xyz_bg, cls_bg).astype(np.float32, copy=False)

#                 def _main_apply():
#                     try:
#                         # Guard: actor or mesh replaced (e.g. file reload during compute)
#                         if id(actor) != _actor_id or getattr(actor, "_naksha_mesh", None) is not _mesh_ref:
#                             actor._naksha_bf_building = False
#                             return
#                         pd = _mesh_ref.GetPointData()
#                         ba = pd.GetArray("BoundaryFlag")
#                         if ba is not None and ba.GetNumberOfTuples() == len(bf):
#                             np.copyto(numpy_support.vtk_to_numpy(ba), bf)
#                             ba.Modified()
#                         else:
#                             if ba is not None:
#                                 pd.RemoveArray("BoundaryFlag")
#                             ba = numpy_support.numpy_to_vtk(bf, deep=False)
#                             ba.SetName("BoundaryFlag")
#                             pd.AddArray(ba)
#                         actor._naksha_bf_np_ref       = bf
#                         actor._naksha_boundary_vtk    = ba
#                         ca = pd.GetArray("Classification")
#                         actor._naksha_boundary_class_mtime  = int(ca.GetMTime()) if ca else _cls_mtime
#                         actor._naksha_boundary_point_count  = len(bf)
#                         actor._naksha_boundary_checksum     = _new_checksum
#                         pd.Modified()
#                         _mesh_ref.Modified()
#                         m = actor.GetMapper()
#                         if m:
#                             m.Modified()
#                         actor.Modified()
#                         actor._naksha_bf_building = False
#                         print(
#                             f"      🔲 BoundaryFlag updated ({_reason}): "
#                             f"{int(bf.sum()):,}/{len(bf):,}"
#                         )
#                         rw = getattr(actor, "_naksha_render_window", None)
#                         if rw:
#                             rw.Render()
#                     except Exception as _ae:
#                         actor._naksha_bf_building = False
#                         print(f"      ⚠️ BoundaryFlag background apply failed: {_ae}")

#                 from PySide6.QtCore import QTimer
#                 QTimer.singleShot(0, _main_apply)
#             except Exception as _be:
#                 actor._naksha_bf_building = False
#                 print(f"      ⚠️ BoundaryFlag background compute failed: {_be}")

#         import threading as _threading
#         _t = _threading.Thread(target=_bg_compute, daemon=True, name="BoundaryFlagBG")
#         _t.start()
#         actor._naksha_bf_thread = _t  # keep reference; daemon thread, won't block exit

#         print(f"      ⏳ BoundaryFlag recomputing in background ({len(cls):,} pts)...")
#         return True

#     except Exception as e:
#         print(f"      ⚠️ BoundaryFlag refresh failed ({reason}): {e}")
#         return False

# # ─────────────────────────────────────────────────────────────────────────────
# # sync_palette_to_gpu — called by Display Mode dialog Apply
# # ─────────────────────────────────────────────────────────────────────────────
# def sync_palette_to_gpu(app, slot_idx: int = 0, palette: Optional[dict] = None,
#                         border: Optional[float] = None, render: bool = True,
#                         rewrite_rgb: Optional[bool] = None, **kwargs):
#     t0 = time.perf_counter()

#     border_explicitly_provided = (border is not None)
#     if border is None:
#         kw_border = kwargs.get('border_percent', None)
#         if kw_border is not None:
#             border = float(kw_border)
#             border_explicitly_provided = True
#         else:
#             border = 0.0

#     if not border_explicitly_provided and float(border) <= 0.0 and slot_idx == 0:
#         _ua = getattr(app, '_unified_actor', None)
#         _uc = getattr(_ua, '_naksha_shader_ctx', None) if _ua else None
#         if _uc is not None and float(_uc.border_ring) > 0.0:
#             border = float(_uc.border_ring) * 100.0
#         elif float(getattr(app, 'point_border_percent', 0) or 0.0) > 0.0:
#             border = float(app.point_border_percent)

#     if slot_idx == 0:
#         actor_name = UNIFIED_ACTOR_NAME
#         vtk_widget = getattr(app, "vtk_widget", None)
#     elif 1 <= slot_idx <= 4:
#         view_idx   = slot_idx - 1
#         actor_name = f"_section_{view_idx}_unified"
#         vtk_widget = app.section_vtks.get(view_idx) if hasattr(app, "section_vtks") else None
#     elif slot_idx == 5:
#         actor_name = "_cut_section_unified"
#         ctrl       = getattr(app, "cut_section_controller", None)
#         vtk_widget = getattr(ctrl, "cut_vtk", None) if ctrl else None
#     else:
#         return False

#     if vtk_widget is None:
#         return False

#     palette = palette or _get_slot_palette(app, slot_idx)

#     if 1 <= slot_idx <= 4:
#         if not hasattr(app, 'view_borders'):
#             app.view_borders = {}
#         if slot_idx in app.view_borders:
#             border = float(app.view_borders[slot_idx])
#         elif border_explicitly_provided:
#             app.view_borders[slot_idx] = float(border)
#         if section_requires_legacy_border_render(app, view_idx, palette, float(border)):
#             if hasattr(app, '_refresh_single_section_view'):
#                 app._refresh_single_section_view(view_idx, float(border))
#                 return True

#     actor = vtk_widget.actors.get(actor_name)
#     if actor is None:
#         if slot_idx == 0:
#             actor = _get_unified_actor(app)

#         if 1 <= slot_idx <= 4 and hasattr(app, '_refresh_single_section_view'):
#             app._refresh_single_section_view(view_idx, float(border))
#             return True

#         if actor is None:
#             return False

#     ctx = getattr(actor, '_naksha_shader_ctx', None)
#     if ctx is None:
#         ctx = ViewShaderContext(slot_idx)
#         _shader_contexts[actor_name] = ctx
#         _attach_view_shader_context(actor, ctx, actor_name)

#     dialog = getattr(app, 'display_mode_dialog', None) or getattr(app, 'display_dialog', None)
#     ctx.structured_border_mode = dialog.get_border_mode() if dialog and hasattr(dialog, 'get_border_mode') else 0.0

#     base_point_size = float(getattr(actor, '_naksha_base_point_size', _BASE_POINT_SIZE))

#     ctx.force_reload()
#     ctx.load_from_palette(palette, float(border), base_point_size)

#     if 0.5 < float(getattr(ctx, "structured_border_mode", 0.0) or 0.0) < 1.5:
#         _refresh_actor_boundary_flags(actor, f"slot {slot_idx} structured sync")

#     _push_uniforms_direct(actor, ctx)


#     if slot_idx == 0:
#         if "_naksha_unified_border" in (getattr(app, "vtk_widget", None) or {}).actors:
#             app.vtk_widget.remove_actor("_naksha_unified_border", render=False)

#     if slot_idx == 0 and float(border) > 0.0:
#         app.point_border_percent = float(border)
#     elif 1 <= slot_idx <= 5:
#         if not hasattr(app, 'view_borders'):
#             app.view_borders = {}
#         app.view_borders[slot_idx] = float(border)

#     if rewrite_rgb is None:
#         if slot_idx == 0:
#             # Keep main-view mode colors stable in non-class modes.
#             # Slot-0 palette sync should still update shader LUT uniforms (visibility/weights/border),
#             # but must not overwrite the current RGB buffer used by intensity/depth/rgb/elevation.
#             current_mode = str(getattr(app, "display_mode", "class") or "class").lower()
#             rewrite_rgb = current_mode in ("class", "shaded_class")
#         else:
#             rewrite_rgb = True

#     rgb_ptr = getattr(actor, '_naksha_rgb_ptr', None)
#     if bool(rewrite_rgb) and rgb_ptr is not None and _is_writable(rgb_ptr):
#         sc = getattr(actor, '_naksha_section_class', None)
#         if sc is not None:
#             _rewrite_rgb_from_palette(rgb_ptr, sc, palette)
#         else:
#             gi             = getattr(app, '_main_global_indices', None)
#             data_obj       = getattr(app, 'data', None)
#             classification = data_obj.get("classification") if isinstance(data_obj, dict) else None
#             if classification is not None:
#                 vis_class = classification[gi] if gi is not None else classification
#                 _rewrite_rgb_from_palette(rgb_ptr, vis_class, palette)

#                 # Keep the GPU's per-vertex "Classification" ID buffer in sync with
#                 # ground truth too, not just RGB. Points reclassified while working
#                 # in a Cross-Section view only get an incremental color+ID patch on
#                 # the points touched at that moment (fast_classify_update); this
#                 # full resync runs ONLY here — on a display-mode preset apply
#                 # (T/A shortcuts, dialog Apply, Ctrl+Shift+D) — which happens a
#                 # handful of times per session, never during live classification,
#                 # so it adds no cost to the classify hot path.
#                 try:
#                     mesh = getattr(actor, '_naksha_mesh', None)
#                     if mesh is not None:
#                         class_vtk_arr = mesh.GetPointData().GetArray("Classification")
#                         if class_vtk_arr is not None:
#                             cls_np = numpy_support.vtk_to_numpy(class_vtk_arr)
#                             if len(cls_np) == len(vis_class):
#                                 cls_np[:] = vis_class.astype(cls_np.dtype, copy=False)
#                                 class_vtk_arr.Modified()
#                                 mesh.Modified()
#                 except Exception:
#                     pass

#         vtk_ca = getattr(actor, '_naksha_vtk_array', None)
#         if vtk_ca:
#             vtk_ca.Modified()
#         _mark_actor_dirty(actor)

#     if slot_idx == 0:
#         _dlg = getattr(app, 'display_mode_dialog', None)
#         if _dlg is not None and hasattr(_dlg, 'view_palettes') and palette:
#             try:
#                 import copy as _copy
#                 _dlg.view_palettes[0] = _copy.deepcopy(palette)
#             except Exception:
#                 pass

#     if render:
#         try:
#             vtk_widget.render()
#         except Exception:
#             pass

#     elapsed = (time.perf_counter() - t0) * 1000
#     print(f"   ⚡ GPU Sync (Slot {slot_idx}): {elapsed:.1f} ms")
#     return True


# # ─────────────────────────────────────────────────────────────────────────────
# # connect_palette_signal
# # ─────────────────────────────────────────────────────────────────────────────
# def connect_palette_signal(app) -> bool:
#     dialog = (getattr(app, 'display_mode_dialog', None)
#               or getattr(app, 'display_dialog', None))
#     if dialog is None:
#         print("   ⚠️ connect_palette_signal: no dialog found")
#         return False
#     if not hasattr(dialog, 'palette_changed'):
#         print("   ⚠️ connect_palette_signal: no palette_changed signal")
#         return False

#     def _on_palette_changed(slot_idx: int):
#         palette = _get_slot_palette(app, slot_idx)
#         if slot_idx == 0:
#             border = float(getattr(app, 'point_border_percent', 0) or 0.0)
#             if border <= 0.0:
#                 _ua = getattr(app, '_unified_actor', None)
#                 _uc = getattr(_ua, '_naksha_shader_ctx', None) if _ua else None
#                 if _uc is not None and float(_uc.border_ring) > 0.0:
#                     border = float(_uc.border_ring) * 100.0
#         else:
#             dlg    = (getattr(app, 'display_mode_dialog', None)
#                       or getattr(app, 'display_dialog', None))
#             border = float(dlg.view_borders.get(slot_idx, 0)) if dlg else 0.0
#         sync_palette_to_gpu(app, slot_idx, palette, border, render=True)

#     try:
#         dialog.palette_changed.disconnect(_on_palette_changed)
#     except (TypeError, RuntimeError):
#         pass
#     dialog.palette_changed.connect(_on_palette_changed)
#     print("   ✅ connect_palette_signal: wired")
#     return True


# # ─────────────────────────────────────────────────────────────────────────────
# # refresh_section_after_weight_change
# # ─────────────────────────────────────────────────────────────────────────────
# def refresh_section_after_weight_change(
#     app,
#     view_idx: int,
#     palette: Optional[dict] = None,
#     border_percent: float = 0.0,
# ) -> bool:
#     slot_idx = view_idx + 1
#     palette  = palette or _get_slot_palette(app, slot_idx)

#     if hasattr(app, 'view_borders') and slot_idx in app.view_borders:
#         border_percent = float(app.view_borders[slot_idx])

#     if not hasattr(app, 'section_vtks') or view_idx not in app.section_vtks:
#         return False
#     vtk_widget = app.section_vtks[view_idx]
#     if vtk_widget is None:
#         return False

#     if section_requires_legacy_border_render(app, view_idx, palette, float(border_percent)):
#         if hasattr(app, '_refresh_single_section_view'):
#             app._refresh_single_section_view(view_idx, float(border_percent))
#             return True
#         return False

#     actor_name = f"_section_{view_idx}_unified"
#     actor = (vtk_widget.actors.get(actor_name)
#              if hasattr(vtk_widget, 'actors') else None)

#     if actor is None:
#         if hasattr(app, '_refresh_single_section_view'):
#             app._refresh_single_section_view(view_idx, float(border_percent))
#             return True
#         return False

#     rgb_ptr = getattr(actor, '_naksha_rgb_ptr', None)
#     if rgb_ptr is None or not _is_writable(rgb_ptr):
#         return False

#     base_point_size = float(getattr(actor, '_naksha_base_point_size', _BASE_POINT_SIZE))

#     ctx = getattr(actor, '_naksha_shader_ctx', None)
#     if ctx is not None:
#         ctx.force_reload()
#         ctx.load_from_palette(palette, border_percent, base_point_size)
#     else:
#         print(f"   ⚠️ Section {view_idx+1}: no shader context — attaching fresh")
#         ctx = ViewShaderContext(slot_idx=slot_idx)
#         ctx.load_from_palette(palette, border_percent, base_point_size)
#         _attach_view_shader_context(actor, ctx, actor_name)

#     dialog = getattr(app, 'display_mode_dialog', None) or getattr(app, 'display_dialog', None)
#     ctx.structured_border_mode = dialog.get_border_mode() if dialog and hasattr(dialog, 'get_border_mode') else 0.0

#     sc = getattr(actor, '_naksha_section_class', None)
#     if sc is not None:
#         _rewrite_rgb_from_palette(rgb_ptr, sc, palette)
#         vtk_ca = getattr(actor, '_naksha_vtk_array', None)
#         if vtk_ca:
#             vtk_ca.Modified()
#         _mark_actor_dirty(actor)

#     _push_uniforms_direct(actor, ctx)

#     try:
#         vtk_widget.render()
#     except Exception:
#         pass

#     print(f"   ✅ refresh_section_after_weight_change: view={view_idx+1} "
#           f"(slot={slot_idx}, border={border_percent}%, base_size={base_point_size})")
#     return True


# # ─────────────────────────────────────────────────────────────────────────────
# # fast_palette_refresh — main view weight/visibility/color update
# # ─────────────────────────────────────────────────────────────────────────────
# def fast_palette_refresh(
#     app,
#     palette: Optional[dict] = None,
#     border_percent: float = 0.0,
# ) -> bool:
#     t0 = time.perf_counter()

#     actor = _get_unified_actor(app)
#     if actor is None:
#         return False

#     rgb_ptr = getattr(actor, "_naksha_rgb_ptr", None)
#     vtk_ca  = getattr(actor, "_naksha_vtk_array", None)
#     if rgb_ptr is None or vtk_ca is None or not _is_writable(rgb_ptr):
#         return False

#     palette         = palette or getattr(app, "class_palette", {})
#     classification  = app.data["classification"]
#     base_point_size = float(getattr(actor, '_naksha_base_point_size', _BASE_POINT_SIZE))

#     ctx = getattr(actor, '_naksha_shader_ctx', None)

#     if border_percent <= 0.0:
#         if ctx is not None and float(ctx.border_ring) > 0.0:
#             border_percent = float(ctx.border_ring) * 100.0
#         else:
#             border_percent = float(getattr(app, 'point_border_percent', 0) or 0.0)

#     if ctx is not None:
#         ctx.force_reload()
#         ctx.load_from_palette(palette, border_percent, base_point_size)
#     else:
#         print(f"   ⚠️ Main view: shader context missing — recovering")
#         ctx = ViewShaderContext(slot_idx=0)
#         ctx.load_from_palette(palette, border_percent, base_point_size)
#         raw_mapper = actor.GetMapper()
#         if hasattr(raw_mapper, 'GetMapper'):
#             raw_mapper = raw_mapper.GetMapper()
#         ctx._has_vertex_attr_cache = hasattr(raw_mapper, 'MapDataArrayToVertexAttribute')
#         actor._naksha_shader_ctx   = ctx
#         _shader_contexts[UNIFIED_ACTOR_NAME] = ctx

#     dialog = getattr(app, 'display_mode_dialog', None) or getattr(app, 'display_dialog', None)
#     ctx.structured_border_mode = dialog.get_border_mode() if dialog and hasattr(dialog, 'get_border_mode') else 0.0

#     gi        = getattr(app, '_main_global_indices', None)
#     vis_class = classification[gi] if gi is not None else classification
#     _rewrite_rgb_from_palette(rgb_ptr, vis_class, palette)

#     # Sync Classification array on the mesh
#     mesh = getattr(actor, '_naksha_mesh', None)
#     if mesh is not None:
#         class_vtk_arr = mesh.GetPointData().GetArray("Classification")
#         if class_vtk_arr is not None:
#             cls_np = numpy_support.vtk_to_numpy(class_vtk_arr)
#             np.copyto(cls_np, vis_class.astype(np.float32, copy=False))
#             class_vtk_arr.Modified()
#             mesh.Modified()

#     if 0.5 < float(getattr(ctx, "structured_border_mode", 0.0) or 0.0) < 1.5:
#         _refresh_actor_boundary_flags(actor, "main structured fast refresh")

#     vtk_ca.Modified()


#     _mark_actor_dirty(actor)

#     _apply_border_once(actor, border_percent)
#     _push_uniforms_direct(actor, ctx)

#     try:
#         app.vtk_widget.render()
#     except Exception:
#         pass

#     elapsed = (time.perf_counter() - t0) * 1000
#     print(f"   ⚡ fast_palette_refresh: {len(vis_class):,} pts "
#           f"[{elapsed:.1f} ms] base_size={base_point_size}")
#     return True


# # ─────────────────────────────────────────────────────────────────────────────
# # COLOR LUT
# # ─────────────────────────────────────────────────────────────────────────────
# class ColorLUT:
#     def __init__(self):
#         self._lut: Optional[np.ndarray] = None
#         self._palette_id: Optional[int] = None
#         self._max_class: int = 0
#         self._hidden_color = np.array([0, 0, 0], dtype=np.uint8)

#     def _palette_fingerprint(self, palette: dict) -> int:
#         try:
#             return hash(tuple(
#                 (k, v.get("show", True), tuple(v.get("color", (128, 128, 128))))
#                 for k, v in sorted(palette.items())
#             ))
#         except Exception:
#             return id(palette)

#     def build(self, palette: dict, max_class_hint: int = 256) -> np.ndarray:
#         fp = self._palette_fingerprint(palette)
#         if fp == self._palette_id and self._lut is not None:
#             return self._lut
#         max_c = max(max_class_hint, max(palette.keys(), default=0) + 1)
#         lut = np.full((max_c, 3), 128, dtype=np.uint8)
#         for code, info in palette.items():
#             if code < max_c:
#                 lut[code] = (info.get("color", (128, 128, 128))
#                              if info.get("show", True) else self._hidden_color)
#         self._lut        = lut
#         self._palette_id = fp
#         self._max_class  = max_c
#         return lut

#     @property
#     def lut(self) -> Optional[np.ndarray]:
#         return self._lut

#     def load_from_palette(self, palette: dict) -> None:
#         self.build(palette, max(max(palette.keys(), default=0) + 1, 256))

#     def map_classes(self, classification: np.ndarray, palette: dict) -> np.ndarray:
#         lut = self.build(palette, int(classification.max()) + 1)
#         return lut[classification.clip(0, len(lut) - 1).astype(np.intp)]

#     def map_subset(self, classification: np.ndarray, indices: np.ndarray,
#                    palette: dict) -> np.ndarray:
#         lut = self.build(palette, int(classification.max()) + 1)
#         return lut[classification[indices].clip(0, len(lut) - 1).astype(np.intp)]


# _lut_cache: Dict[str, ColorLUT] = {}


# def _get_lut(view_key: str = "main") -> ColorLUT:
#     if view_key not in _lut_cache:
#         _lut_cache[view_key] = ColorLUT()
#     return _lut_cache[view_key]


# # ─────────────────────────────────────────────────────────────────────────────
# # SNT OVERLAY Z-OFFSET
# # ─────────────────────────────────────────────────────────────────────────────
# def _restore_snt_overlays(app):
#     if hasattr(app, 'snt_dialog') and app.snt_dialog is not None:
#         try:
#             app.snt_dialog.restore_snt_actors()
#             return
#         except Exception as e:
#             print(f"  ⚠️ SNT dialog restore: {e}")

#     try:
#         from gui.snt_attachment import (
#             _get_snt_z_offset, _apply_z_offset_to_actor,
#             _snt_enable_gl_point_size, _snt_push_border_uniforms,
#         )
#     except ImportError:
#         return

#     z_offset = _get_snt_z_offset(app)
#     if z_offset <= 0:
#         return

#     try:
#         renderer = app.vtk_widget.renderer
#     except Exception:
#         return

#     count = 0
#     prj_dlg = getattr(app, "block_identifier_dialog", None)
#     for store_name in ['snt_actors']:
#         att_name = store_name.replace("_actors", "_attachments")
#         attachments = getattr(app, att_name, [])
#         for entry in getattr(app, store_name, []):
#             target = os.path.basename(entry.get("filename", ""))
#             att = next((a for a in attachments if os.path.basename(a.get("filename", "")) == target), None)

#             actor_layer_map = {}
#             selected_layers = None
#             if att:
#                 selected_layers = att.get("selected_layers")
#                 cache_map = att.get("actor_cache_map", {})
#                 for layer_name, actors in cache_map.items():
#                     for a in actors:
#                         actor_layer_map[id(a)] = layer_name

#             for actor in entry.get('actors', []):
#                 try:
#                     _apply_z_offset_to_actor(actor, z_offset)
#                     renderer.AddActor(actor)
#                     actor._naksha_renderer = renderer

#                     keep_hidden = (
#                         prj_dlg is not None
#                         and hasattr(prj_dlg, "should_keep_actor_hidden")
#                         and prj_dlg.should_keep_actor_hidden(actor)
#                     )
#                     if keep_hidden:
#                         actor.SetVisibility(0)
#                     elif selected_layers is not None and id(actor) in actor_layer_map:
#                         layer_name = actor_layer_map[id(actor)]
#                         actor.SetVisibility(1 if layer_name in selected_layers else 0)

#                     count += 1
#                 except Exception:
#                     pass

#     if count > 0:
#         renderer.ResetCameraClippingRange()
#         _snt_enable_gl_point_size(app)
#         _snt_push_border_uniforms(app)
#         print(f"  🔄 SNT overlays: {count} actors restored (z_offset={z_offset:.1f})")


# # ─────────────────────────────────────────────────────────────────────────────
# # BUILD UNIFIED ACTOR  (main view)
# # ─────────────────────────────────────────────────────────────────────────────
# def build_unified_actor(
#     app,
#     palette: Optional[dict] = None,
#     border_percent: float = 0.0,
#     point_size: float = _BASE_POINT_SIZE,
# ) -> Optional[object]:
#     t0 = time.perf_counter()

#     app._unified_actor_building = True   # guard: block interactions during build
#     # Cancel any pending deferred init that references a previous actor
#     _pending = getattr(app, '_naksha_deferred_timer', None)
#     if _pending is not None:
#         try:
#             _pending.stop()
#         except Exception:
#             pass
#         app._naksha_deferred_timer = None

#     plotter = getattr(app, "vtk_widget", None)
#     if plotter is None:
#         return None
#     data = getattr(app, "data", None)
#     if data is None or "xyz" not in data:
#         return None
#     xyz            = data["xyz"]
#     classification = data.get("classification")
#     if classification is None:
#         return None

#     palette = palette or getattr(app, "class_palette", {})

#     if UNIFIED_ACTOR_NAME in plotter.actors:
#         plotter.remove_actor(UNIFIED_ACTOR_NAME, render=False)
#     for name in list(plotter.actors.keys()):
#         if str(name).startswith("class_"):
#             plotter.remove_actor(name, render=False)

#     N_total = len(xyz)
#     try:
#         from gui.optimization_config import MAIN_VIEW_RENDER_ALL_POINTS
#     except Exception:
#         MAIN_VIEW_RENDER_ALL_POINTS = True

#     if MAIN_VIEW_RENDER_ALL_POINTS:
#         # None is the established identity-map convention in refresh code.
#         # It avoids a 32M-entry arange and advanced-index copies of full XYZ.
#         step = 1
#         global_indices = None
#         vis_xyz = xyz
#         vis_class = classification
#     else:
#         target_points = 10_000_000
#         if N_total > target_points:
#             step = max(1, N_total // target_points)
#             global_indices = np.arange(0, N_total, step)
#         else:
#             step = 1
#             global_indices = None
#         vis_xyz = xyz if global_indices is None else xyz[global_indices]
#         vis_class = classification if global_indices is None else classification[global_indices]

#     app._main_global_indices = global_indices
#     # Store LOD step for O(M) brush index mapping (avoids intersect1d on 10M array).
#     # gi = arange(0, N, step), so global_idx is in LOD iff global_idx % step == 0
#     # and its local index is global_idx // step.
#     app._main_lod_step = step

#     cloud     = pv.PolyData(vis_xyz)
#     # Keep main classification array independent of temporary NumPy views.
#     class_vtk = numpy_support.numpy_to_vtk(vis_class.astype(np.float32, copy=False),
#                                             deep=True)
#     class_vtk.SetName("Classification")
#     cloud.GetPointData().AddArray(class_vtk)

#     _bf = _compute_boundary_flags(vis_xyz, vis_class)
#     _bf_vtk = numpy_support.numpy_to_vtk(_bf, deep=False)
#     _bf_vtk.SetName("BoundaryFlag")
#     cloud.GetPointData().AddArray(_bf_vtk)
#     print(f"      🔲 BoundaryFlag: {int(_bf.sum()):,}/{len(_bf):,} edge pts")

#     vis_count = len(vis_xyz)
#     rgb_buf = getattr(app, "_rgb_buffer", None)
#     if (
#         not isinstance(rgb_buf, np.ndarray)
#         or rgb_buf.dtype != np.uint8
#         or rgb_buf.ndim != 2
#         or rgb_buf.shape[0] != vis_count
#         or rgb_buf.shape[1] != 3
#     ):
#         app._rgb_buffer = np.zeros((vis_count, 3), dtype=np.uint8)
#     elif not rgb_buf.flags.c_contiguous:
#         app._rgb_buffer = np.ascontiguousarray(rgb_buf, dtype=np.uint8)

#     rgb_vtk = numpy_support.numpy_to_vtk(app._rgb_buffer, deep=False)
#     rgb_vtk.SetName("RGB")
#     cloud.GetPointData().SetScalars(rgb_vtk)

#     lut = _get_lut("main")
#     np.copyto(app._rgb_buffer, lut.map_classes(vis_class, palette))

#     n_pts             = len(xyz)
#     actual_point_size = _BASE_POINT_SIZE

#     actor = plotter.add_points(
#         cloud, scalars="RGB", rgb=True,
#         point_size=actual_point_size,
#         render_points_as_spheres=False,
#         name=UNIFIED_ACTOR_NAME,
#         reset_camera=False, render=False,
#     )
#     if actor:
#         actor.GetProperty().LightingOff()

#     if actor:
#         try:
#             actor._naksha_render_window = plotter.render_window
#             actor._naksha_renderer = plotter.renderer
#         except Exception:
#             pass

#     if actor:
#         try:
#             rw = plotter.render_window
#             if _try_enable_program_point_size(rw):
#                 _install_program_point_size_observer(actor, rw)
#                 actor._naksha_needs_program_point_size = False
#                 print("      ✅ GL_PROGRAM_POINT_SIZE enabled immediately (warm context)")
#         except Exception:
#             pass

#         _ensure_opengl_polydata_mapper(actor, cloud)
#         _bm = actor.GetMapper()
#         mesh   = _bm.GetInput() if _bm is not None else None
#         if mesh is None:
#             raise RuntimeError("build_unified_actor: mapper has no input after _ensure_opengl_polydata_mapper")
#         vtk_ca = mesh.GetPointData().GetScalars()
#         if vtk_ca is None:
#             raise RuntimeError("build_unified_actor: mesh scalars missing")

#         _vtk_rgb = numpy_support.vtk_to_numpy(vtk_ca)
#         np.copyto(_vtk_rgb, app._rgb_buffer)
#         vtk_ca.Modified()

#         _cls_np_f32 = vis_class.astype(np.float32)
#         _cls_vtk = numpy_support.numpy_to_vtk(_cls_np_f32, deep=False)
#         _cls_vtk.SetName("Classification")
#         mesh.GetPointData().AddArray(_cls_vtk)

#         actor._naksha_rgb_ptr         = _vtk_rgb
#         actor._naksha_point_count     = len(vis_xyz)
#         actor._naksha_vtk_array       = vtk_ca
#         actor._naksha_vtk_rgb_ref     = vtk_ca
#         actor._naksha_cls_np_ref      = _cls_np_f32
#         actor._naksha_bf_np_ref       = _bf
#         actor._naksha_mesh            = mesh
#         actor._naksha_base_point_size = actual_point_size
#         actor._naksha_boundary_vtk    = _bf_vtk
#         actor._naksha_data_id         = id(xyz)
#         # Seed the boundary-flag cache so the first sync_palette_to_gpu does
#         # not recompute boundary flags already computed above.
#         # Uses sampled-checksum key (not VTK MTime) so palette/display changes
#         # that call class_vtk_arr.Modified() never trigger a false recompute.
#         _cls_in_mesh = mesh.GetPointData().GetArray("Classification")
#         if _cls_in_mesh is not None:
#             actor._naksha_boundary_class_mtime = int(_cls_in_mesh.GetMTime())
#             actor._naksha_boundary_point_count = len(vis_class)
#             _step = max(1, len(vis_class) // 2000)
#             actor._naksha_boundary_checksum = int(
#                 vis_class.astype(np.int32)[::_step].sum()
#             )
#         app._unified_actor            = actor

#         # ── Precompute an interaction LOD subset (safe, no invariants broken) ──
#         # We keep the SAME vtkPolyData and its named arrays (Classification,
#         # BoundaryFlag, RGB) and only ever swap a *strided subset* of those
#         # arrays in/out during a pan/zoom/rotate gesture. Because the shader
#         # reads per-point arrays, a smaller strided subset renders with identical
#         # colours/boundaries — just fewer points — so palette sync, program point
#         # size, and the custom shader are all unaffected. Only engage for large
#         # clouds (cheap clouds rely on the frame-budget cap instead).
#         try:
#             _lod_target = 2_000_000
#             _n_vis = len(vis_xyz)
#             if _n_vis > _lod_target:
#                 _lod_k = max(2, _n_vis // _lod_target)
#                 _lod_idx = np.arange(0, _n_vis, _lod_k)
#                 actor._naksha_full_xyz   = vis_xyz
#                 # These must be the arrays actually owned by the VTK actor.
#                 # Classification and RGB are patched in place after every
#                 # classify/undo operation; keeping separate build-time copies
#                 # makes a later interaction-LOD swap resurrect stale colours.
#                 actor._naksha_full_class = _cls_np_f32
#                 actor._naksha_full_bf    = _bf
#                 actor._naksha_full_rgb   = _vtk_rgb
#                 actor._naksha_lod_idx    = _lod_idx
#                 actor._naksha_lod_xyz    = vis_xyz[_lod_idx]
#                 actor._naksha_lod_class  = _cls_np_f32[_lod_idx]
#                 actor._naksha_lod_bf     = _bf[_lod_idx]
#                 actor._naksha_lod_rgb    = _vtk_rgb[_lod_idx]
#                 actor._naksha_lod_active = False
#                 print(f"      🔻 Interaction LOD precomputed: {len(_lod_idx):,} pts "
#                       f"(stride {_lod_k}, full {_n_vis:,})")
#             else:
#                 actor._naksha_lod_idx = None  # LOD disabled for small clouds
#         except Exception as _lod_e:
#             actor._naksha_lod_idx = None
#             print(f"      ⚠️ Interaction LOD precompute skipped: {_lod_e}")

#         ctx = ViewShaderContext(slot_idx=0)

#         ctx.load_from_palette(palette, border_percent, actual_point_size)

#         structured_mode = 0.0
#         dialog = getattr(app, 'display_mode_dialog', None) or getattr(app, 'display_dialog', None)
#         if dialog:
#             if hasattr(dialog, 'border_logic_hybrid') and dialog.border_logic_hybrid.isChecked():
#                 structured_mode = 2.0
#             elif hasattr(dialog, 'border_logic_object') and dialog.border_logic_object.isChecked():
#                 structured_mode = 1.0
#         ctx.structured_border_mode = structured_mode

#         _attach_view_shader_context(actor, ctx, UNIFIED_ACTOR_NAME)
#         print(f"      ✅ Main view: {len(app._rgb_buffer):,} pts "
#               f"(base_size={actual_point_size}, LOD_step={step}, "
#               f"full_resolution={bool(MAIN_VIEW_RENDER_ALL_POINTS)})")

#         try:
#             from PySide6.QtCore import QTimer
#             _a, _c, _p = actor, ctx, plotter
#             # Capture the current data id so the deferred callback can
#             # abort if a newer file was loaded before the timer fires.
#             _expected_data_id = id(app.data.get("xyz"))

#             def _safe_deferred_init():
#                 current_xyz = (app.data.get("xyz")
#                                if hasattr(app, "data") and app.data else None)
#                 if current_xyz is None or id(current_xyz) != _expected_data_id:
#                     print("      ⏭️  Deferred GPU init skipped (data replaced)")
#                     return
#                 _deferred_actor_gpu_init(_a, _c, _p, "MainView")

#             t = QTimer()
#             t.setSingleShot(True)
#             t.timeout.connect(_safe_deferred_init)
#             t.start(500)
#             app._naksha_deferred_timer = t
#             print("      ⏱️  Deferred GPU init scheduled (500 ms)")
#         except Exception as _te:
#             print(f"      ⚠️ Could not schedule deferred init: {_te}")

#     elapsed = (time.perf_counter() - t0) * 1000
#     print(f"   🏗️ Unified actor built: {n_pts:,} pts in {elapsed:.1f} ms")

#     app._unified_actor_building = False   # ← clear guard

#     _restore_snt_overlays(app)
#     return actor


# def _swap_main_points(actor, xyz, classification, bf, rgb):
#     """Replace the unified actor's mesh points + point arrays in place.

#     Uses the SAME vtkPolyData and the SAME named arrays (Classification,
#     BoundaryFlag, RGB) as the full build — only the point count changes — so
#     the custom shader, program point size, and palette uniforms keep working.
#     Returns True on success.
#     """
#     try:
#         mapper = actor.GetMapper()
#         if mapper is None:
#             return False
#         mesh = mapper.GetInput()
#         if mesh is None:
#             return False

#         from vtkmodules.util import numpy_support as _ns

#         # Validate and construct every wrapper before mutating the live mesh.
#         # This keeps a failed swap atomic: VTK never sees points and attributes
#         # with different tuple counts.
#         xyz = np.ascontiguousarray(xyz)
#         classification = np.ascontiguousarray(classification, dtype=np.float32)
#         bf = np.ascontiguousarray(bf)
#         rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
#         point_count = len(xyz)
#         if xyz.ndim != 2 or xyz.shape[1] != 3:
#             raise ValueError(f"xyz must have shape (N, 3), got {xyz.shape}")
#         if classification.ndim != 1 or len(classification) != point_count:
#             raise ValueError("Classification length does not match points")
#         if bf.ndim != 1 or len(bf) != point_count:
#             raise ValueError("BoundaryFlag length does not match points")
#         if rgb.ndim != 2 or rgb.shape != (point_count, 3):
#             raise ValueError("RGB must have shape (N, 3)")

#         points_data = _ns.numpy_to_vtk(xyz, deep=False)
#         points_data.SetName("Points")
#         vtk_points = vtk.vtkPoints()
#         vtk_points.SetData(points_data)

#         cls_vtk = _ns.numpy_to_vtk(classification, deep=False)
#         cls_vtk.SetName("Classification")
#         bf_vtk = _ns.numpy_to_vtk(bf, deep=False)
#         bf_vtk.SetName("BoundaryFlag")
#         rgb_vtk = _ns.numpy_to_vtk(rgb, deep=False)
#         rgb_vtk.SetName("RGB")

#         # vtkPolyData.SetPoints requires vtkPoints, not the vtkDataArray
#         # returned by numpy_to_vtk.
#         mesh.SetPoints(vtk_points)

#         pd = mesh.GetPointData()
#         # Remove any existing arrays with these names first so repeated
#         # in/out swaps never accumulate duplicate-named arrays.
#         for _name in ("Classification", "BoundaryFlag", "RGB"):
#             _existing = pd.GetArray(_name)
#             if _existing is not None:
#                 pd.RemoveArray(_name)

#         pd.AddArray(cls_vtk)
#         pd.AddArray(bf_vtk)
#         pd.SetScalars(rgb_vtk)

#         pd.Modified()
#         mesh.Modified()
#         mapper.Modified()
#         actor.Modified()

#         # Rewire every cached pointer used by the fast classification paths.
#         # Keeping the old full-detail pointers while the LOD arrays are active
#         # would write into detached memory and strand stale colours on screen.
#         actor._naksha_mesh = mesh
#         actor._naksha_point_count = point_count
#         actor._naksha_rgb_ptr = rgb
#         actor._naksha_vtk_array = rgb_vtk
#         actor._naksha_vtk_rgb_ref = rgb_vtk
#         actor._naksha_cls_np_ref = classification
#         actor._naksha_class_vtk_ref = cls_vtk
#         actor._naksha_bf_np_ref = bf
#         actor._naksha_boundary_vtk = bf_vtk
#         actor._naksha_points_vtk_ref = vtk_points
#         actor._naksha_active_xyz_ref = xyz
#         return True
#     except Exception as _e:
#         print(f"      ⚠️ _swap_main_points failed: {_e}")
#         return False


# def apply_main_lod(app, factor: float = 0.25):
#     """Swap the main cloud to its precomputed coarse LOD subset for an
#     interaction gesture. No-op (returns False) if LOD isn't available or is
#     already active. Never affects classification/section/other behaviour."""
#     # SAFETY: Keep the main actor at full detail.
#     #
#     # This LOD path used to fail before touching the vtkPolyData because
#     # _swap_main_points passed a vtkDataArray to SetPoints.  Consequently the
#     # application's established behaviour was full-detail rendering.  Allowing
#     # the swap to execute exposes the live actor to point/attribute replacement
#     # while render and BoundaryFlag callbacks may still retain those buffers,
#     # which can produce a native Windows access violation.  A safe LOD needs a
#     # separate immutable actor/double buffer, not mutation of the displayed
#     # vtkPolyData.  Preserve the stable existing behaviour until that design is
#     # implemented.
#     return False


# def restore_main_full_detail(app):
#     """Restore the full-resolution main cloud after an interaction gesture.
#     No-op if LOD was not active. Returns True if a swap-back happened."""
#     if app is None or getattr(app, "_shutdown_in_progress", False):
#         return False
#     actor = getattr(app, "_unified_actor", None)
#     if actor is None or not getattr(actor, "_naksha_lod_active", False):
#         return False
#     if not _swap_main_points(
#         actor,
#         actor._naksha_full_xyz,
#         actor._naksha_full_class,
#         actor._naksha_full_bf,
#         actor._naksha_full_rgb,
#     ):
#         return False
#     actor._naksha_lod_active = False
#     return True

# def _clear_section_visual_actors(vtk_widget, view_idx: int) -> None:
#     actor_name = f"_section_{view_idx}_unified"
#     actors = getattr(vtk_widget, "actors", {})

#     if actor_name in actors:
#         vtk_widget.remove_actor(actor_name, render=False)

#     for name in list(actors.keys()):
#         name_str = str(name)
#         if name_str.startswith(("class_", "border_")) or name_str in ("border_layer", "color_layer"):
#             vtk_widget.remove_actor(name, render=False)


# def _section_draw_sizes(
#     weight: float,
#     border_percent: float,
#     base_point_size: float = _BASE_POINT_SIZE,
# ) -> tuple[float, float]:
#     clamped_weight = max(0.1, min(float(weight or 1.0), 12.0))
#     color_size = max(1.0, min(base_point_size * clamped_weight, 15.0))

#     if float(border_percent or 0.0) > 0.0:
#         border_scale = 1.0 + (float(border_percent) / 60.0)
#         border_size = max(1.0, min(color_size * border_scale, 15.0))
#     else:
#         border_size = color_size

#     return color_size, border_size


# def section_requires_legacy_border_render(
#     app,
#     view_idx: int,
#     palette: Optional[dict] = None,
#     border_percent: float = 0.0,
# ) -> bool:
#     slot_idx = view_idx + 1
#     palette = palette or _get_slot_palette(app, slot_idx)

#     if border_percent <= 0.0 and hasattr(app, 'view_borders'):
#         border_percent = float(app.view_borders.get(slot_idx, 0) or 0.0)

#     visible_classes = {
#         int(code) for code, info in (palette or {}).items()
#         if info.get("show", True)
#     }
#     recent_class = getattr(app, "_last_classified_to_class", None)
#     has_special_order = recent_class is not None and int(recent_class) in visible_classes
#     return has_special_order


# def build_section_legacy_border_actors(
#     app,
#     view_idx: int,
#     palette: Optional[dict] = None,
#     border_percent: float = 0.0,
#     point_size: float = _BASE_POINT_SIZE,
# ) -> bool:
#     slot_idx = view_idx + 1

#     if not hasattr(app, 'section_vtks') or view_idx not in app.section_vtks:
#         return False

#     vtk_widget = app.section_vtks[view_idx]
#     if vtk_widget is None:
#         return False

#     core_pts = getattr(app, f"section_{view_idx}_core_points", None)
#     buf_pts = getattr(app, f"section_{view_idx}_buffer_points", None)
#     core_mask = getattr(app, f"section_{view_idx}_core_mask", None)
#     buf_mask = getattr(app, f"section_{view_idx}_buffer_mask", None)

#     if core_pts is None or core_mask is None:
#         return False

#     data = getattr(app, "data", None)
#     if data is None or "classification" not in data:
#         return False

#     classification_full = data["classification"]
#     n_class = len(classification_full)

#     core_mask = np.asarray(core_mask, dtype=bool).ravel()
#     if core_mask.size != n_class:
#         print(
#             f"   ⚠️ Section {view_idx + 1}: stale core_mask "
#             f"(len={core_mask.size}, data={n_class}) - skipping refresh"
#         )
#         return False

#     core_global_idx = np.flatnonzero(core_mask)
#     if len(core_pts) != len(core_global_idx):
#         print(
#             f"   ⚠️ Section {view_idx + 1}: core_points/core_mask mismatch "
#             f"({len(core_pts)} vs {len(core_global_idx)}) - skipping refresh"
#         )
#         return False

#     if buf_pts is not None and buf_mask is not None and len(buf_pts) > 0:
#         buf_mask = np.asarray(buf_mask, dtype=bool).ravel()
#         if buf_mask.size != n_class:
#             print(
#                 f"   ⚠️ Section {view_idx + 1}: stale buffer_mask "
#                 f"(len={buf_mask.size}, data={n_class}) - ignoring buffer"
#             )
#             buf_pts = None
#             buf_mask = None
#         else:
#             buf_only_mask = buf_mask & ~core_mask
#             buf_global_idx = np.flatnonzero(buf_only_mask)
#             if len(buf_pts) != len(buf_global_idx):
#                 print(
#                     f"   ⚠️ Section {view_idx + 1}: buffer_points/buffer_mask mismatch "
#                     f"({len(buf_pts)} vs {len(buf_global_idx)}) - ignoring buffer"
#                 )
#                 buf_pts = None
#                 buf_mask = None

#     if buf_pts is not None and buf_mask is not None and len(buf_pts) > 0:
#         buf_only_mask = buf_mask & ~core_mask
#         buf_global_idx = np.flatnonzero(buf_only_mask)
#         all_pts = np.vstack([core_pts, buf_pts])
#         all_cls = np.concatenate([
#             classification_full[core_global_idx],
#             classification_full[buf_global_idx],
#         ])
#         all_global_idx = np.concatenate([core_global_idx, buf_global_idx])
#     else:
#         all_pts = core_pts
#         all_cls = classification_full[core_global_idx]
#         all_global_idx = core_global_idx

#     setattr(app, f"_section_{view_idx}_global_indices", all_global_idx)

#     palette = palette or _get_slot_palette(app, slot_idx)
#     if border_percent <= 0.0 and hasattr(app, 'view_borders'):
#         border_percent = float(app.view_borders.get(slot_idx, 0) or 0.0)

#     if not hasattr(app, 'view_borders'):
#         app.view_borders = {}
#     app.view_borders[slot_idx] = float(border_percent)

#     visible_classes = [
#         int(code) for code, info in (palette or {}).items()
#         if info.get("show", True)
#     ]

#     try:
#         cam_pos = vtk_widget.camera_position
#     except Exception:
#         cam_pos = None

#     _clear_section_visual_actors(vtk_widget, view_idx)

#     if len(all_pts) == 0 or not visible_classes:
#         vtk_widget._naksha_section_render_mode = "legacy"
#         if cam_pos is not None:
#             try:
#                 vtk_widget.camera_position = cam_pos
#             except Exception:
#                 pass
#         vtk_widget.render()
#         return True

#     visible_mask = np.isin(all_cls, visible_classes)
#     filtered_pts = all_pts[visible_mask]
#     filtered_cls = all_cls[visible_mask]

#     # Safety cap for legacy renderer path.
#     legacy_cap = _section_render_point_cap(app)
#     if legacy_cap is not None and len(filtered_pts) > legacy_cap:
#         sel = _uniform_pick_indices(len(filtered_pts), legacy_cap)
#         filtered_pts = filtered_pts[sel]
#         filtered_cls = filtered_cls[sel]
#         print(
#             f"   ⚡ Section {view_idx + 1} legacy cap: "
#             f"{len(all_pts):,} -> {len(filtered_pts):,} (manual budget={legacy_cap:,})"
#         )

#     if len(filtered_pts) == 0:
#         vtk_widget._naksha_section_render_mode = "legacy"
#         if cam_pos is not None:
#             try:
#                 vtk_widget.camera_position = cam_pos
#             except Exception:
#                 pass
#         vtk_widget.render()
#         return True

#     base_point_size = max(1.0, float(point_size or _BASE_POINT_SIZE))
#     recent_class = getattr(app, "_last_classified_to_class", None)
#     has_custom_weights = any(
#         abs(float(info.get("weight", 1.0)) - 1.0) > 1e-6
#         for info in (palette or {}).values()
#     )
#     use_special_order = recent_class is not None and int(recent_class) in visible_classes

#     if has_custom_weights or use_special_order:
#         class_weights = []
#         for code in visible_classes:
#             if use_special_order and int(code) == int(recent_class):
#                 continue
#             weight = float((palette or {}).get(int(code), {}).get("weight", 1.0))
#             class_weights.append((int(code), weight))

#         class_weights.sort(key=lambda item: item[1], reverse=True)
#         render_order = [code for code, _ in class_weights]
#         if use_special_order:
#             render_order.append(int(recent_class))

#         for code in render_order:
#             class_mask = (filtered_cls == code)
#             if not np.any(class_mask):
#                 continue

#             class_pts = filtered_pts[class_mask]
#             entry = (palette or {}).get(int(code), {})
#             weight = float(entry.get("weight", 1.0))
#             color = np.asarray(entry.get("color", (128, 128, 128)), dtype=np.uint8)
#             color_size, border_size = _section_draw_sizes(weight, border_percent, base_point_size)

#             if float(border_percent) > 0.0:
#                 border_cloud = pv.PolyData(class_pts)
#                 border_cloud["RGB"] = np.zeros((len(class_pts), 3), dtype=np.uint8)
#                 vtk_widget.add_points(
#                     border_cloud, scalars="RGB", rgb=True,
#                     point_size=border_size, render_points_as_spheres=True,
#                     name=f"border_{code}", reset_camera=False, render=False,
#                 )

#             cloud = pv.PolyData(class_pts)
#             cloud["RGB"] = np.tile(color, (len(class_pts), 1)).astype(np.uint8)
#             vtk_widget.add_points(
#                 cloud, scalars="RGB", rgb=True,
#                 point_size=color_size, render_points_as_spheres=True,
#                 name=f"class_{code}", reset_camera=False, render=False,
#             )
#     else:
#         lut_size = max(int(filtered_cls.max()) + 1, 256)
#         color_lut = np.full((lut_size, 3), 128, dtype=np.uint8)
#         for code, entry in (palette or {}).items():
#             idx = int(code)
#             if 0 <= idx < lut_size:
#                 color_lut[idx] = entry.get("color", (128, 128, 128))

#         _, border_size = _section_draw_sizes(1.0, border_percent, base_point_size)

#         if float(border_percent) > 0.0:
#             border_cloud = pv.PolyData(filtered_pts)
#             border_cloud["RGB"] = np.zeros((len(filtered_pts), 3), dtype=np.uint8)
#             vtk_widget.add_points(
#                 border_cloud, scalars="RGB", rgb=True,
#                 point_size=border_size, render_points_as_spheres=True,
#                 name="border_layer", reset_camera=False, render=False,
#             )

#         cloud = pv.PolyData(filtered_pts)
#         cloud["RGB"] = color_lut[filtered_cls.astype(np.intp)]
#         vtk_widget.add_points(
#             cloud, scalars="RGB", rgb=True,
#             point_size=base_point_size, render_points_as_spheres=True,
#             name="color_layer", reset_camera=False, render=False,
#         )

#     vtk_widget._naksha_section_render_mode = "legacy"
#     if cam_pos is not None:
#         try:
#             vtk_widget.camera_position = cam_pos
#         except Exception:
#             pass
#     try:
#         vtk_widget.renderer.ResetCameraClippingRange()
#     except Exception:
#         pass
#     vtk_widget.render()

#     print(
#         f"   Section {view_idx + 1} legacy border render: "
#         f"{len(filtered_pts):,} pts (border={border_percent}%)"
#     )
#     return True


# # ─────────────────────────────────────────────────────────────────────────────
# # BUILD SECTION UNIFIED ACTOR
# # ─────────────────────────────────────────────────────────────────────────────
# def build_section_unified_actor(
#     app,
#     view_idx: int,
#     palette: Optional[dict] = None,
#     border_percent: float = 0.0,
#     point_size: float = _BASE_POINT_SIZE,
#     **kwargs
# ) -> Optional[object]:
#     t0 = time.perf_counter()
#     slot_idx = view_idx + 1

#     if not hasattr(app, 'section_vtks') or view_idx not in app.section_vtks:
#         return None
#     vtk_widget = app.section_vtks[view_idx]
#     if vtk_widget is None:
#         return None

#     core_pts  = getattr(app, f"section_{view_idx}_core_points",  None)
#     buf_pts   = getattr(app, f"section_{view_idx}_buffer_points", None)
#     core_mask = getattr(app, f"section_{view_idx}_core_mask",    None)
#     buf_mask  = getattr(app, f"section_{view_idx}_buffer_mask",  None)

#     if core_pts is None or core_mask is None:
#         return None

#     data = getattr(app, 'data', None)
#     if data is None or 'classification' not in data:
#         return None

#     classification_full = data['classification']
#     n_class = len(classification_full)

#     core_mask = np.asarray(core_mask, dtype=bool).ravel()
#     if core_mask.size != n_class:
#         print(
#             f"   ⚠️ Section {view_idx + 1}: stale core_mask "
#             f"(len={core_mask.size}, data={n_class}) - skipping refresh"
#         )
#         return None

#     core_global_idx = np.flatnonzero(core_mask)
#     if len(core_pts) != len(core_global_idx):
#         print(
#             f"   ⚠️ Section {view_idx + 1}: core_points/core_mask mismatch "
#             f"({len(core_pts)} vs {len(core_global_idx)}) - skipping refresh"
#         )
#         return None

#     all_pts_prebuilt = getattr(app, f"section_{view_idx}_points_transformed", None)
#     has_buffer = False
#     buf_global_idx = np.empty((0,), dtype=np.int64)
#     if buf_pts is not None and buf_mask is not None and len(buf_pts) > 0:
#         buf_mask = np.asarray(buf_mask, dtype=bool).ravel()
#         if buf_mask.size != n_class:
#             print(
#                 f"   ⚠️ Section {view_idx + 1}: stale buffer_mask "
#                 f"(len={buf_mask.size}, data={n_class}) - ignoring buffer"
#             )
#             buf_pts = None
#             buf_mask = None
#         else:
#             buf_only_mask = buf_mask & ~core_mask
#             buf_global_idx = np.flatnonzero(buf_only_mask)
#             if len(buf_pts) != len(buf_global_idx):
#                 print(
#                     f"   ⚠️ Section {view_idx + 1}: buffer_points/buffer_mask mismatch "
#                     f"({len(buf_pts)} vs {len(buf_global_idx)}) - ignoring buffer"
#                 )
#                 buf_pts = None
#                 buf_mask = None
#             else:
#                 has_buffer = True

#     core_pts_use = core_pts
#     core_idx_use = core_global_idx
#     buf_pts_use = buf_pts if has_buffer else None
#     buf_idx_use = buf_global_idx if has_buffer else np.empty((0,), dtype=np.int64)

#     total_raw_points = int(len(core_pts_use) + (len(buf_pts_use) if buf_pts_use is not None else 0))
#     section_cap = _section_render_point_cap(app)
#     downsampled = False
#     if section_cap is not None and total_raw_points > section_cap:
#         downsampled = True
#         core_target = min(len(core_pts_use), max(1, int(round(section_cap * 0.8))))
#         remaining = max(0, section_cap - core_target)
#         buf_target = min(len(buf_pts_use) if buf_pts_use is not None else 0, remaining)

#         if core_target < len(core_pts_use) and (core_target + buf_target) < section_cap:
#             extra = min(len(core_pts_use) - core_target, section_cap - (core_target + buf_target))
#             core_target += max(0, extra)

#         if (core_target + buf_target) > section_cap:
#             overflow = (core_target + buf_target) - section_cap
#             if buf_target >= overflow:
#                 buf_target -= overflow
#             else:
#                 core_target = max(1 if len(core_pts_use) > 0 else 0, core_target - (overflow - buf_target))
#                 buf_target = 0

#         core_sel = _uniform_pick_indices(len(core_pts_use), core_target)
#         core_pts_use = core_pts_use[core_sel] if core_sel.size else core_pts_use[:0]
#         core_idx_use = core_idx_use[core_sel] if core_sel.size else core_idx_use[:0]

#         if buf_pts_use is not None and len(buf_pts_use) > 0:
#             buf_sel = _uniform_pick_indices(len(buf_pts_use), buf_target)
#             buf_pts_use = buf_pts_use[buf_sel] if buf_sel.size else buf_pts_use[:0]
#             buf_idx_use = buf_idx_use[buf_sel] if buf_sel.size else buf_idx_use[:0]

#         print(
#             f"   ⚡ Section {view_idx + 1} render cap: "
#             f"{total_raw_points:,} -> {len(core_pts_use) + (len(buf_pts_use) if buf_pts_use is not None else 0):,} "
#             f"(manual budget={section_cap:,}; set NAKSHA_SECTION_POINT_BUDGET to increase)"
#         )

#     if buf_pts_use is not None and len(buf_pts_use) > 0:
#         if (
#             not downsampled
#             and all_pts_prebuilt is not None
#             and len(all_pts_prebuilt) == (len(core_pts) + len(buf_pts))
#         ):
#             all_pts = all_pts_prebuilt
#         else:
#             all_pts = np.vstack([core_pts_use, buf_pts_use])
#         all_global_indices = np.concatenate([core_idx_use, buf_idx_use])
#     else:
#         all_pts = core_pts_use
#         all_global_indices = core_idx_use

#     all_cls = classification_full[all_global_indices]
#     combined_global_mask = getattr(app, f"section_{view_idx}_combined_mask", None)
#     if not (
#         isinstance(combined_global_mask, np.ndarray)
#         and combined_global_mask.dtype == bool
#         and combined_global_mask.size == n_class
#     ):
#         combined_global_mask = core_mask if (buf_pts_use is None or len(buf_pts_use) == 0) else None
#     if len(all_pts) == 0:
#         return None

#     setattr(app, f"_section_{view_idx}_global_indices", all_global_indices)
#     palette = palette or _get_slot_palette(app, slot_idx)

#     if hasattr(app, 'view_borders') and slot_idx in app.view_borders:
#         border_percent = float(app.view_borders[slot_idx])

#     actor_name = f"_section_{view_idx}_unified"
#     cam_pos = vtk_widget.camera_position
#     actual_pt_size = max(1.0, float(point_size or _BASE_POINT_SIZE))
#     n_pts = len(all_pts)

#     if actor_name in vtk_widget.actors:
#         vtk_widget.remove_actor(actor_name, render=False)
#     for name in list(vtk_widget.actors.keys()):
#         if str(name).startswith(("class_", "border_")) or str(name) in ("border_layer", "color_layer"):
#             vtk_widget.remove_actor(name, render=False)

#     lut_obj = _get_lut(f"section_{view_idx}")
#     use_lod = n_pts > _LOD_SKIP_THRESHOLD

#     def _build_arrays(pts, cls):
#         return _build_section_numpy_data(
#             pts, cls, palette, border_percent, lut_obj.map_classes, _compute_boundary_flags
#         )

#     if not use_lod:
#         arrays = _build_arrays(all_pts, all_cls)
#         actor, ctx, mesh, vtk_ca, _vtk_rgb, _vtk_cls, class_vtk, bf_vtk = _build_vtk_actor_from_arrays(
#             vtk_widget, actor_name, arrays, actual_pt_size, palette, border_percent, slot_idx,
#             _attach_view_shader_context, _ensure_opengl_polydata_mapper, ViewShaderContext
#         )
#         if actor is None:
#             return None
#         _wire_actor_metadata(actor, mesh, vtk_ca, _vtk_rgb, _vtk_cls, class_vtk, bf_vtk, combined_global_mask, actual_pt_size)
#         actor._naksha_global_indices = all_global_indices
#         try:
#             from PySide6.QtCore import QTimer
#             _a, _c, _w = actor, ctx, vtk_widget
#             QTimer.singleShot(500, lambda: _deferred_actor_gpu_init(_a, _c, _w, f"Section{view_idx+1}"))
#         except Exception:
#             pass
#         vtk_widget._naksha_section_render_mode = "unified"
#         vtk_widget.camera_position = cam_pos
#         vtk_widget.render()
#         elapsed = (time.perf_counter() - t0) * 1000
#         print(f"   🏗️ Section {view_idx+1} unified actor: {n_pts:,} pts in {elapsed:.1f} ms "
#               f"(slot={slot_idx}, border={border_percent}%, base_size={actual_pt_size})")
#         return actor

#     lod_count = min(n_pts, _LOD_FIRST_FRAME_MAX)
#     lod_sel = _uniform_pick_indices(n_pts, lod_count)
#     lod_pts = all_pts[lod_sel]
#     lod_cls = all_cls[lod_sel]
#     lod_arrays = _build_arrays(lod_pts, lod_cls)
#     lod_actor, lod_ctx, lod_mesh, lod_vtk_ca, lod_vtk_rgb, lod_vtk_cls, lod_class_vtk, lod_bf_vtk = _build_vtk_actor_from_arrays(
#         vtk_widget, actor_name, lod_arrays, actual_pt_size, palette, border_percent, slot_idx,
#         _attach_view_shader_context, _ensure_opengl_polydata_mapper, ViewShaderContext
#     )
#     if lod_actor is None:
#         return None
#     _wire_actor_metadata(lod_actor, lod_mesh, lod_vtk_ca, lod_vtk_rgb, lod_vtk_cls, lod_class_vtk, lod_bf_vtk, combined_global_mask, actual_pt_size)
#     lod_actor._naksha_global_indices = all_global_indices[lod_sel]
#     vtk_widget._naksha_section_render_mode = "unified_lod"
#     vtk_widget.camera_position = cam_pos
#     vtk_widget.render()

#     def _upgrade_full_res():
#         lock = _get_section_build_lock(view_idx)
#         if not lock.acquire(blocking=False):
#             return
#         try:
#             full_arrays = _build_arrays(all_pts, all_cls)
#             from PySide6.QtCore import QTimer

#             def _swap_on_main_thread():
#                 try:
#                     current = app.section_vtks.get(view_idx) if hasattr(app, "section_vtks") else None
#                     if current is not vtk_widget:
#                         return
#                     if actor_name not in vtk_widget.actors:
#                         return
#                     vtk_widget.remove_actor(actor_name, render=False)
#                     full_actor, full_ctx, full_mesh, full_vtk_ca, full_vtk_rgb, full_vtk_cls, full_class_vtk, full_bf_vtk = _build_vtk_actor_from_arrays(
#                         vtk_widget, actor_name, full_arrays, actual_pt_size, palette, border_percent, slot_idx,
#                         _attach_view_shader_context, _ensure_opengl_polydata_mapper, ViewShaderContext
#                     )
#                     if full_actor is None:
#                         return
#                     _wire_actor_metadata(full_actor, full_mesh, full_vtk_ca, full_vtk_rgb, full_vtk_cls, full_class_vtk, full_bf_vtk, combined_global_mask, actual_pt_size)
#                     full_actor._naksha_global_indices = all_global_indices
#                     vtk_widget._naksha_section_render_mode = "unified"
#                     vtk_widget.camera_position = cam_pos
#                     vtk_widget.render()
#                     try:
#                         _deferred_actor_gpu_init(full_actor, full_ctx, vtk_widget, f"Section{view_idx+1}")
#                     except Exception:
#                         pass
#                 finally:
#                     lock.release()

#             QTimer.singleShot(0, _swap_on_main_thread)
#         except Exception:
#             try:
#                 lock.release()
#             except Exception:
#                 pass

#     _section_build_executor.submit(_upgrade_full_res)

#     elapsed = (time.perf_counter() - t0) * 1000
#     print(f"   🏗️ Section {view_idx+1} LOD actor: {lod_count:,}/{n_pts:,} pts in {elapsed:.1f} ms "
#           f"(slot={slot_idx}, border={border_percent}%, base_size={actual_pt_size})")
#     return lod_actor


# # ─────────────────────────────────────────────────────────────────────────────
# # PARTIAL UPDATES (The "Real" Performance Fix for 48M+ points)
# # ─────────────────────────────────────────────────────────────────────────────
# def fast_partial_cross_section_update(app, view_idx: int, global_changed_mask: np.ndarray):
#     """Updates ONLY the points that changed within a specific cross-section."""
#     if not hasattr(app, 'section_vtks') or view_idx not in app.section_vtks:
#         return False

#     vtk_widget = app.section_vtks[view_idx]
#     if vtk_widget is None:
#         return False

#     actor_name = f"_section_{view_idx}_unified"
#     actor = vtk_widget.actors.get(actor_name) if hasattr(vtk_widget, 'actors') else None
#     if actor is None:
#         return False

#     rgb_ptr = getattr(actor, '_naksha_rgb_ptr', None)
#     if rgb_ptr is None or not _is_writable(rgb_ptr):
#         return False

#     # IMPORTANT:
#     # Section unified actors are built in this exact local order:
#     #   [core_global_indices, buffer_only_global_indices]
#     # (see build_section_unified_actor)
#     # So we must compute changed local indices using that same order.
#     # Using np.where(section_mask) order is incorrect and can miss/shift updates.
#     section_global_indices = getattr(app, f"_section_{view_idx}_global_indices", None)
#     if section_global_indices is not None and len(section_global_indices) > 0:
#         try:
#             max_idx = int(section_global_indices.max(initial=0))
#         except Exception:
#             max_idx = 0
#         if len(global_changed_mask) < (max_idx + 1):
#             return True
#         local_indices = np.flatnonzero(global_changed_mask[section_global_indices])
#     else:
#         # Fallback (older views): derive using combined mask and actor-local map.
#         section_mask = getattr(app, f"section_{view_idx}_combined_mask", None)
#         if section_mask is None:
#             return False

#         changed_in_view_global_indices = np.where(global_changed_mask & section_mask)[0]
#         if len(changed_in_view_global_indices) == 0:
#             return True

#         g_to_l_arr = getattr(actor, '_naksha_global_to_local_arr', None)
#         if g_to_l_arr is None:
#             full_indices = np.where(section_mask)[0]
#             g_to_l_arr = np.full(len(app.data["xyz"]), -1, dtype=np.int32)
#             g_to_l_arr[full_indices] = np.arange(len(full_indices), dtype=np.int32)
#             actor._naksha_global_to_local_arr = g_to_l_arr

#         local_indices = g_to_l_arr[changed_in_view_global_indices]
#         valid_mask = local_indices != -1
#         if not np.any(valid_mask):
#             return True
#         local_indices = local_indices[valid_mask]

#     if len(local_indices) == 0:
#         return True

#     try:
#         _patch_actor_memory(app, actor, local_indices, slot_idx=view_idx + 1)
#     except Exception as e:
#         print(f"⚠️ fast_partial_cross_section_update patch failed (view={view_idx}): {e}")
#         return False
#     return True


# def fast_partial_classify_update(app, global_changed_mask: np.ndarray):
#     """Updates ONLY the points that changed in the MAIN VIEW."""
#     actor = _get_unified_actor(app)
#     if actor is None:
#         return False

#     rgb_ptr = getattr(actor, "_naksha_rgb_ptr", None)
#     if rgb_ptr is None or not _is_writable(rgb_ptr):
#         return False

#     gi = getattr(app, '_main_global_indices', None)
#     palette = getattr(app, "class_palette", {})
#     classification = app.data["classification"]

#     if gi is None:
#         local_indices = np.where(global_changed_mask)[0]
#     else:
#         gi_mask = getattr(app, '_main_global_mask', None)
#         if gi_mask is None:
#             gi_mask = np.zeros(len(app.data["xyz"]), dtype=bool)
#             gi_mask[gi] = True
#             app._main_global_mask = gi_mask

#         changed_visible_global = np.where(global_changed_mask & gi_mask)[0]
#         if len(changed_visible_global) == 0:
#             return True

#         g_to_l_arr = getattr(actor, '_naksha_global_to_local_arr', None)
#         if g_to_l_arr is None:
#             g_to_l_arr = np.full(len(app.data["xyz"]), -1, dtype=np.int32)
#             g_to_l_arr[gi] = np.arange(len(gi), dtype=np.int32)
#             actor._naksha_global_to_local_arr = g_to_l_arr

#         local_indices = g_to_l_arr[changed_visible_global]
#         valid_mask = local_indices != -1
#         if not np.any(valid_mask):
#             return True
#         local_indices = local_indices[valid_mask]

#     try:
#         display_mode = str(getattr(app, "display_mode", "class") or "class").lower()
#         # Guard main-view RGB/class-buffer patching outside class/shaded_class modes.
#         if display_mode not in ("class", "shaded_class"):
#             return True
#         _patch_actor_memory(app, actor, local_indices, slot_idx=0)
#     except Exception as e:
#         print(f"⚠️ fast_partial_classify_update patch failed: {e}")
#         return False
#     return True


# # ─────────────────────────────────────────────────────────────────────────────
# # fast_classify_update — main view
# # ─────────────────────────────────────────────────────────────────────────────
# def fast_classify_update(
#     app,
#     changed_mask: np.ndarray,
#     to_class: int,
#     skip_render: bool = False,
#     **kwargs,
# ) -> bool:
#     actor = _get_unified_actor(app)
#     if actor is None:
#         return False

#     global_indices = getattr(app, '_main_global_indices', None)
#     if global_indices is not None:
#         local_changed = np.flatnonzero(changed_mask[global_indices])
#     else:
#         local_changed = np.flatnonzero(changed_mask)

#     if local_changed.size == 0:
#         return True

#     if to_class is None:
#         return True

#     # Only update slot-0 RGB/class buffers when main view is in class/shaded_class.
#     display_mode = str(getattr(app, "display_mode", "class") or "class").lower()
#     if display_mode in ("class", "shaded_class"):
#         palette   = kwargs.get("palette") or getattr(app, "class_palette", {}) or _get_slot_palette(app, 0)
#         entry     = palette.get(int(to_class), {})
#         new_color = entry.get("color", (128, 128, 128)) if entry.get("show", True) else (0, 0, 0)

#         rgb_ptr = getattr(actor, "_naksha_rgb_ptr", None)
#         if rgb_ptr is not None:
#             if local_changed[-1] >= len(rgb_ptr):
#                 local_changed = local_changed[local_changed < len(rgb_ptr)]
#             if local_changed.size > 0:
#                 rgb_ptr[local_changed] = new_color
#                 vtk_ca = getattr(actor, "_naksha_vtk_array", None)
#                 if vtk_ca is not None:
#                     vtk_ca.Modified()

#         mesh = getattr(actor, '_naksha_mesh', None)
#         if mesh is not None:
#             class_vtk_arr = mesh.GetPointData().GetArray("Classification")
#             if class_vtk_arr is not None:
#                 cls_np = numpy_support.vtk_to_numpy(class_vtk_arr)
#                 cls_np[local_changed] = cls_np.dtype.type(to_class)
#                 class_vtk_arr.Modified()
#                 mesh.Modified()

#         _mark_actor_dirty(actor)

#         ctx = getattr(actor, '_naksha_shader_ctx', None)
#         if ctx is not None:
#             last_gen = getattr(actor, '_last_uniform_generation', -1)
#             if ctx._generation != last_gen:
#                 _push_uniforms_direct(actor, ctx)
#                 actor._last_uniform_generation = ctx._generation

#     if not skip_render and display_mode in ("class", "shaded_class"):
#         try:
#             # Keep the throttled call: app.vtk_widget.render is wrapped by
#             # GPURenderManager which queues main-view renders to ~10 fps.
#             # When the user is classifying in a cross-section dock, the
#             # 39M-point main view does NOT need to repaint synchronously —
#             # the queue absorbs it within 100 ms. Forcing it sync here
#             # blocks the release path for 10–30 ms per click (regression
#             # observed). Throttle = non-blocking, that's the win.
#             app.vtk_widget.render()
#             app._gpu_sync_done = True
#         except Exception as _e:
#             print(f"⚠️ GPU render error (fast_classify_update): {_e}")

#     return True


# # ─────────────────────────────────────────────────────────────────────────────
# # fast_cross_section_update — section views
# # ─────────────────────────────────────────────────────────────────────────────
# def fast_cross_section_update(
#     app,
#     view_idx: int,
#     changed_mask_global: np.ndarray,
#     palette: Optional[dict] = None,
#     skip_render: bool = False,
#     force_visibility_refresh: bool = False,
# ) -> bool:
#     t0 = time.perf_counter()

#     if not hasattr(app, "section_vtks") or view_idx not in app.section_vtks:
#         return False

#     vtk_widget     = app.section_vtks[view_idx]
#     slot_idx       = view_idx + 1
#     palette        = palette or _get_slot_palette(app, slot_idx)
#     if palette is None:
#         palette = {}

#     border_percent = float(getattr(app, "view_borders", {}).get(slot_idx, 0) or 0.0)
#     if section_requires_legacy_border_render(app, view_idx, palette, border_percent):
#         # Legacy border mode requires full geometry — caller must call build_section_unified_actor
#         return False

#     actor_name     = f"_section_{view_idx}_unified"
#     global_indices = getattr(app, f"_section_{view_idx}_global_indices", None)

#     if global_indices is None or len(global_indices) == 0:
#         classification_ref = (app.data.get("classification")
#                               if hasattr(app, "data") and app.data else None)
#         combined_mask = getattr(app, f"section_{view_idx}_combined_mask", None)
#         if (
#             classification_ref is not None
#             and isinstance(combined_mask, np.ndarray)
#             and combined_mask.dtype == bool
#             and combined_mask.size == len(classification_ref)
#         ):
#             global_indices = np.flatnonzero(combined_mask)
#             setattr(app, f"_section_{view_idx}_global_indices", global_indices)

#     if actor_name not in vtk_widget.actors or global_indices is None:
#         # Actor not built yet — caller must call _plot_section / build_section_unified_actor
#         return False

#     actor = vtk_widget.actors.get(actor_name)
#     if actor is None:
#         return False

#     actor_global_indices = getattr(actor, "_naksha_global_indices", None)
#     if (
#         isinstance(actor_global_indices, np.ndarray)
#         and actor_global_indices.size > 0
#     ):
#         global_indices = actor_global_indices

#     rgb_ptr = getattr(actor, "_naksha_rgb_ptr", None)
#     if rgb_ptr is None or not _is_writable(rgb_ptr):
#         return False

#     classification = (app.data.get("classification")
#                       if hasattr(app, 'data') and app.data else None)
#     if classification is None:
#         return False

#     mapper = actor.GetMapper()
#     poly   = mapper.GetInput() if mapper else None
#     if poly is None:
#         return False

#     vtk_scalars = poly.GetPointData().GetScalars()
#     if vtk_scalars is None:
#         return False

#     changed_idx = None
#     cached_changed_idx = getattr(app, "_last_changed_indices", None)
#     cached_mask_ref = getattr(app, "_last_changed_mask", None)
#     can_use_sparse = (
#         isinstance(cached_changed_idx, np.ndarray)
#         and cached_changed_idx.size > 0
#         and (changed_mask_global is cached_mask_ref)
#     )

#     if can_use_sparse and isinstance(changed_mask_global, np.ndarray) and changed_mask_global.dtype == bool:
#         try:
#             _probe = cached_changed_idx[
#                 (cached_changed_idx >= 0) & (cached_changed_idx < len(changed_mask_global))
#             ]
#             if _probe.size == 0 or not np.all(changed_mask_global[_probe]):
#                 can_use_sparse = False
#         except Exception:
#             can_use_sparse = False

#     if can_use_sparse:
#         g_to_l_arr = getattr(actor, '_naksha_global_to_local_arr', None)
#         if g_to_l_arr is None or len(g_to_l_arr) != len(classification):
#             g_to_l_arr = np.full(len(classification), -1, dtype=np.int32)
#             g_to_l_arr[global_indices] = np.arange(len(global_indices), dtype=np.int32)
#             actor._naksha_global_to_local_arr = g_to_l_arr

#         _valid_changed = cached_changed_idx[
#             (cached_changed_idx >= 0) & (cached_changed_idx < len(g_to_l_arr))
#         ]
#         if _valid_changed.size > 0:
#             local_map = g_to_l_arr[_valid_changed]
#             keep = local_map >= 0
#             if np.any(keep):
#                 changed_idx = local_map[keep]

#     if changed_idx is None:
#         if (changed_mask_global is not None
#                 and len(changed_mask_global) >= (int(global_indices.max()) + 1 if len(global_indices) > 0 else 0)):
#             section_changed = changed_mask_global[global_indices]
#         else:
#             section_changed = np.ones(len(global_indices), dtype=bool)
#         changed_idx = np.flatnonzero(section_changed)

#     n_changed = int(changed_idx.size)

#     if n_changed > 0:
#         if changed_idx.max(initial=-1) >= len(rgb_ptr):
#             keep = changed_idx < len(rgb_ptr)
#             changed_idx = changed_idx[keep]
#             n_changed = int(changed_idx.size)
#             if n_changed == 0:
#                 return False
#         new_classes = classification[global_indices[changed_idx]]

#         lut_obj   = _get_lut(f"section_{view_idx}")
#         lut_obj.load_from_palette(palette)
#         color_lut = lut_obj.lut
#         rgb_ptr[changed_idx] = color_lut[new_classes.clip(0, len(color_lut) - 1).astype(np.intp)]

#         vtk_rgb_arr = getattr(actor, "_naksha_vtk_array", None)
#         if vtk_rgb_arr is not None:
#             vtk_rgb_arr.Modified()

#         cls_np = getattr(actor, "_naksha_section_class", None)
#         class_vtk_arr = getattr(actor, "_naksha_class_vtk_ref", None)
#         max_local = int(changed_idx.max()) if changed_idx.size > 0 else -1
#         if cls_np is not None and len(cls_np) > max_local:
#             try:
#                 cls_np[changed_idx] = new_classes.astype(cls_np.dtype, copy=False)
#             except Exception:
#                 cls_np[changed_idx] = new_classes
#             if class_vtk_arr is None:
#                 class_vtk_arr = poly.GetPointData().GetArray("Classification")
#                 actor._naksha_class_vtk_ref = class_vtk_arr
#             if class_vtk_arr is not None:
#                 class_vtk_arr.Modified()
#         else:
#             class_vtk_arr = poly.GetPointData().GetArray("Classification")
#             if class_vtk_arr is not None:
#                 cls_np = numpy_support.vtk_to_numpy(class_vtk_arr)
#                 cls_np[changed_idx] = new_classes.astype(cls_np.dtype, copy=False)
#                 class_vtk_arr.Modified()
#                 actor._naksha_section_class = cls_np
#                 actor._naksha_class_vtk_ref = class_vtk_arr

#         vtk_scalars.Modified()
#         poly.Modified()
#         _mark_actor_dirty(actor)

#     if force_visibility_refresh:
#         try:
#             # Re-push per-slot palette/LUT so hidden->visible class transitions
#             # are reflected immediately without waiting for Display Mode Apply.
#             sync_palette_to_gpu(
#                 app,
#                 slot_idx=slot_idx,
#                 palette=palette,
#                 border=border_percent,
#                 render=False,
#             )
#         except Exception as _vis_err:
#             print(f"   ⚠️ force_visibility_refresh failed (view={view_idx+1}): {_vis_err}")

#     ctx = getattr(actor, '_naksha_shader_ctx', None)
#     if ctx is not None:
#         _last_gen = getattr(actor, '_last_uniform_generation', -1)
#         if ctx._generation != _last_gen:
#             _push_uniforms_direct(actor, ctx)
#             actor._last_uniform_generation = ctx._generation
#     else:
#         print("   ⚠️  No shader context on actor — uniforms not pushed")

#     if not skip_render:
#         try:
#             if _safe_widget_render(vtk_widget):
#                 app._gpu_sync_done = True
#         except Exception as _e:
#             print(f"⚠️ GPU render error (fast_cross_section_update view={view_idx}): {_e}")

#     elapsed = (time.perf_counter() - t0) * 1000
#     print(f"   ⚡ Section {view_idx + 1} RGB inject: {n_changed} pts [{elapsed:.1f} ms]")
#     return True


# def fast_undo_update(app, changed_mask: np.ndarray, **kwargs) -> bool:
#     t0 = time.perf_counter()
#     actors_updated = 0
#     total_pts = 0

#     main_actor = _get_unified_actor(app)
#     if main_actor:
#         gi = getattr(app, '_main_global_indices', None)
#         if gi is not None:
#             local_changed = np.flatnonzero(changed_mask[gi])
#         else:
#             local_changed = np.flatnonzero(changed_mask)

#         if local_changed.size > 0:
#             _patch_actor_memory(app, main_actor, local_changed, slot_idx=0)
#             actors_updated += 1
#             total_pts += local_changed.size

#     if not (hasattr(app, 'section_vtks') and app.section_vtks):
#         elapsed = (time.perf_counter() - t0) * 1000
#         print(f"   ⚡ fast_undo_update: {total_pts:,} pts across "
#               f"{actors_updated} views [{elapsed:.1f} ms]")
#         return actors_updated > 0

#     sections_to_render = []
#     for view_idx, vtk_widget in app.section_vtks.items():
#         if vtk_widget is None:
#             continue
#         actor_name = f"_section_{view_idx}_unified"
#         sec_actor = vtk_widget.actors.get(actor_name)
#         if sec_actor is None:
#             continue
#         rgb_ptr = getattr(sec_actor, "_naksha_rgb_ptr", None)
#         if rgb_ptr is None or not _is_writable(rgb_ptr):
#             continue
#         gi_section = getattr(sec_actor, "_naksha_global_indices", None)
#         if gi_section is None or len(gi_section) == 0:
#             gi_section = getattr(app, f'_section_{view_idx}_global_indices', None)
#         if gi_section is None:
#             continue
#         max_gi = int(gi_section.max(initial=0))
#         if len(changed_mask) < max_gi + 1:
#             local_sec_changed = np.arange(len(gi_section), dtype=np.intp)
#         else:
#             local_sec_changed = np.flatnonzero(changed_mask[gi_section])
#         if local_sec_changed.size == 0:
#             continue
#         _patch_actor_memory(app, sec_actor, local_sec_changed, slot_idx=view_idx + 1)
#         sections_to_render.append((view_idx, vtk_widget, local_sec_changed.size))
#         actors_updated += 1
#         total_pts += local_sec_changed.size

#     for view_idx, vtk_widget, n_pts in sections_to_render:
#         try:
#             if _safe_widget_render(vtk_widget):
#                 print(f"   ⚡ Section {view_idx+1} RGB inject: {n_pts} pts")
#         except Exception as e:
#             print(f"   ⚠️  fast_undo_update: render failed section {view_idx+1}: {e}")

#     elapsed = (time.perf_counter() - t0) * 1000
#     print(f"   ⚡ fast_undo_update: {total_pts:,} pts across "
#           f"{actors_updated} views [{elapsed:.1f} ms]")
#     return actors_updated > 0


# def _patch_actor_memory(app, actor, local_indices: np.ndarray,
#                         slot_idx: int) -> None:
#     full_cls = app.data["classification"]
#     local_indices = np.asarray(local_indices, dtype=np.int64).ravel()
#     if local_indices.size == 0:
#         return

#     if slot_idx == 0:
#         gi = getattr(app, '_main_global_indices', None)
#         if gi is None or len(gi) == 0:
#             gi = getattr(actor, '_naksha_global_indices', None)
#         if gi is not None:
#             gi = np.asarray(gi, dtype=np.int64).ravel()
#             # Some legacy refresh callers supply global indices here. Convert
#             # them against the sorted actor map instead of indexing the LOD
#             # array (e.g. global 30M into a 12.7M actor buffer).
#             if np.any(local_indices >= len(gi)):
#                 pos = np.searchsorted(gi, local_indices)
#                 valid = pos < len(gi)
#                 matched = np.zeros(len(pos), dtype=bool)
#                 matched[valid] = gi[pos[valid]] == local_indices[valid]
#                 local_indices = pos[matched]
#                 if local_indices.size == 0:
#                     return
#             else:
#                 local_indices = local_indices[
#                     (local_indices >= 0) & (local_indices < len(gi))]
#                 if local_indices.size == 0:
#                     return
#             reverted_cls = full_cls[gi[local_indices]]
#         else:
#             local_indices = local_indices[
#                 (local_indices >= 0) & (local_indices < len(full_cls))]
#             if local_indices.size == 0:
#                 return
#             reverted_cls = full_cls[local_indices]
#     else:
#         view_idx = slot_idx - 1
#         gi_section = getattr(actor, "_naksha_global_indices", None)
#         if gi_section is None or len(gi_section) == 0:
#             gi_section = getattr(app, f'_section_{view_idx}_global_indices', None)
#         if gi_section is None:
#             return
#         gi_section = np.asarray(gi_section, dtype=np.int64).ravel()
#         local_indices = local_indices[
#             (local_indices >= 0) & (local_indices < len(gi_section))]
#         if local_indices.size == 0:
#             return
#         reverted_cls = full_cls[gi_section[local_indices]]

#     # Final actor-array guard. A stale mirror must never abort CPU undo.
#     rgb_guard = getattr(actor, '_naksha_rgb_ptr', None)
#     if rgb_guard is not None:
#         actor_valid = local_indices < len(rgb_guard)
#         local_indices = local_indices[actor_valid]
#         reverted_cls = reverted_cls[actor_valid]
#         if local_indices.size == 0:
#             return

#     # Guard slot-0 RGB/class-buffer patching when main view is not class/shaded_class.
#     display_mode = str(getattr(app, "display_mode", "class") or "class").lower()
#     if slot_idx > 0 or display_mode in ("class", "shaded_class"):
#         if hasattr(actor, "_naksha_section_class"):
#             actor._naksha_section_class[local_indices] = reverted_cls

#         mesh = getattr(actor, '_naksha_mesh', None)
#         if mesh is not None:
#             class_vtk = mesh.GetPointData().GetArray("Classification")
#             if class_vtk is not None:
#                 numpy_support.vtk_to_numpy(class_vtk)[local_indices] = (
#                     reverted_cls.astype(np.float32))
#                 class_vtk.Modified()

#         rgb_ptr = getattr(actor, "_naksha_rgb_ptr", None)
#         if rgb_ptr is not None and _is_writable(rgb_ptr):
#             # Keep main-view partial updates aligned with the canonical main palette.
#             # This avoids per-stroke color drift when slot-0 dialog overrides lag.
#             if slot_idx == 0:
#                 palette = getattr(app, "class_palette", {}) or _get_slot_palette(app, 0)
#             else:
#                 palette = _get_slot_palette(app, slot_idx)
#             max_hint  = max(int(reverted_cls.max()) + 1, 256) if reverted_cls.size else 256
#             lut_obj   = _get_lut(f"undo_{slot_idx}")
#             color_lut = lut_obj.build(palette, max_hint)
#             rgb_ptr[local_indices] = color_lut[
#                 reverted_cls.clip(0, len(color_lut) - 1).astype(np.intp)]

#             vtk_ca = getattr(actor, "_naksha_vtk_array", None)
#             if vtk_ca is not None:
#                 vtk_ca.Modified()

#         _mark_actor_dirty(actor)

# def _safe_widget_render(vtk_widget) -> bool:
#     """Best-effort safe render for PyVista/VTK widgets."""
#     if vtk_widget is None:
#         return False
#     try:
#         if hasattr(vtk_widget, "isVisible") and callable(vtk_widget.isVisible):
#             if not vtk_widget.isVisible():
#                 return False
#         rw = vtk_widget.GetRenderWindow()
#         if rw is None or rw.GetInteractor() is None:
#             return False
#         vtk_widget.render()
#         return True
#     except (RuntimeError, AttributeError, OSError, ReferenceError):
#         return False
#     except Exception:
#         return False


# # ─────────────────────────────────────────────────────────────────────────────
# # ACTOR LOOKUP HELPERS
# # ─────────────────────────────────────────────────────────────────────────────
# def is_unified_actor_ready(app) -> bool:
#     return _get_unified_actor(app) is not None


# def _expected_main_actor_point_count(current_n: int) -> int:
#     """Use the same full-resolution/LOD contract as build_unified_actor."""
#     try:
#         from gui.optimization_config import MAIN_VIEW_RENDER_ALL_POINTS
#     except Exception:
#         MAIN_VIEW_RENDER_ALL_POINTS = True
#     current_n = max(0, int(current_n))
#     if MAIN_VIEW_RENDER_ALL_POINTS:
#         return current_n
#     target_pts = 10_000_000
#     lod_step = max(1, current_n // target_pts) if current_n > target_pts else 1
#     return int(np.ceil(current_n / lod_step))


# def _get_unified_actor(app) -> Optional[object]:
#     # Block all callers while build_unified_actor is running
#     if getattr(app, '_unified_actor_building', False):
#         return None
#     actor = getattr(app, "_unified_actor", None)
#     if actor is not None:
#         try:
#             actor.GetVisibility()
#             rgb = getattr(actor, "_naksha_rgb_ptr", None)
#             if rgb is not None and _is_writable(rgb):
#                 if hasattr(app, 'data') and app.data is not None:
#                     xyz = app.data.get('xyz')
#                     if xyz is not None:
#                         current_data_id = id(xyz)
#                         actor_data_id   = getattr(actor, '_naksha_data_id', None)
#                         if actor_data_id is not None and actor_data_id != current_data_id:
#                             print(f"   DEBUG: _get_unified_actor STALE - Data changed (ID mismatch)")
#                             return None

#                         current_n = len(xyz)
#                         expected_actor_n = _expected_main_actor_point_count(current_n)

#                         actor_n = getattr(actor, '_naksha_point_count', 0)
#                         if actor_n > 0 and actor_n != expected_actor_n:
#                             print(f"   DEBUG: _get_unified_actor STALE - actor_n={actor_n} != expected={expected_actor_n}")
#                             return None
#                 return actor
#             else:
#                 if rgb is None:
#                     print(f"   DEBUG: _get_unified_actor FAILED - _naksha_rgb_ptr is None")
#                 elif not _is_writable(rgb):
#                     print(f"   DEBUG: _get_unified_actor FAILED - _naksha_rgb_ptr is NOT writable")
#         except (AttributeError, RuntimeError):
#             try:
#                 del app._unified_actor
#             except AttributeError:
#                 pass

#     plotter = getattr(app, "vtk_widget", None)
#     if plotter is None:
#         return None

#     if UNIFIED_ACTOR_NAME in plotter.actors:
#         actor = plotter.actors[UNIFIED_ACTOR_NAME]
#         try:
#             actor.GetVisibility()
#             rgb = getattr(actor, "_naksha_rgb_ptr", None)
#             if rgb is not None and _is_writable(rgb):
#                 if hasattr(app, 'data') and app.data is not None:
#                     xyz = app.data.get('xyz')
#                     if xyz is not None:
#                         current_n = len(xyz)
#                         expected_actor_n = _expected_main_actor_point_count(current_n)
#                         actor_n = getattr(actor, '_naksha_point_count', 0)
#                         if actor_n > 0 and actor_n != expected_actor_n:
#                             return None

#                 app._unified_actor = actor
#                 return actor
#         except (AttributeError, RuntimeError):
#             pass

#     return None


# def _get_all_unified_actors(app):
#     main_actor = _get_unified_actor(app)
#     vtk_widget = getattr(app, "vtk_widget", None)
#     if main_actor is not None and vtk_widget is not None:
#         yield (main_actor, 0, getattr(app, "_main_global_indices", None), vtk_widget)

#     if hasattr(app, "section_vtks"):
#         for view_idx, widget in app.section_vtks.items():
#             actor_name = f"_section_{view_idx}_unified"
#             if actor_name in widget.actors:
#                 actor = widget.actors[actor_name]
#                 rgb   = getattr(actor, "_naksha_rgb_ptr", None)
#                 if rgb is not None and _is_writable(rgb):
#                     gi = getattr(app, f"_section_{view_idx}_global_indices", None)
#                     yield (actor, view_idx + 1, gi, widget)


# _palette_resolve_cache: dict = {}


# def _get_slot_palette(app, slot_idx: int) -> dict:
#     """
#     Fingerprint-guarded palette resolver.
#     Returns a cached resolved dict when master + overrides have not changed.
#     """
#     master = getattr(app, "class_palette", {}) or {}
#     if not master:
#         return {}

#     overrides = {}
#     dlg = getattr(app, "display_mode_dialog", None)
#     if dlg and hasattr(dlg, "view_palettes"):
#         vp = getattr(dlg, "view_palettes", None)
#         if vp and slot_idx in vp:
#             overrides = vp[slot_idx] or {}
#     if not overrides:
#         vp = getattr(app, "view_palettes", {})
#         overrides = vp.get(slot_idx, {}) or {}

#     try:
#         fp = (
#             hash(tuple(
#                 (k, v.get("show", True), v.get("weight", 1.0),
#                  tuple(v.get("color", (128, 128, 128))))
#                 for k, v in sorted(master.items())
#             )),
#             hash(tuple(
#                 (k, v.get("show", True), v.get("weight", 1.0),
#                  tuple(v.get("color", (128, 128, 128))))
#                 for k, v in sorted(overrides.items())
#             )) if overrides else 0,
#         )
#     except Exception:
#         fp = (id(master), id(overrides))

#     cached = _palette_resolve_cache.get(slot_idx)
#     if cached is not None and cached[0] == fp:
#         return cached[1]

#     if not overrides:
#         if slot_idx == 0:
#             result = master
#         else:
#             # Keep cross/cut slots isolated: inherit color/weight only,
#             # never slot-0 visibility state.
#             result = {}
#             for code, info in master.items():
#                 if not isinstance(info, dict):
#                     continue
#                 row = dict(info)
#                 row["show"] = True
#                 row["color"] = tuple(row.get("color", (128, 128, 128)))
#                 row["weight"] = float(row.get("weight", 1.0))
#                 row["description"] = str(row.get("description", ""))
#                 result[code] = row
#     else:
#         # Slot 0 keeps canonical behavior (master + full override validation).
#         if slot_idx == 0:
#             if len(overrides) != len(master):
#                 print(f"   [WARN] _get_slot_palette: slot 0 override count mismatch "
#                       f"({len(overrides)} vs master {len(master)}) - ignoring stale override")
#                 result = master
#             else:
#                 result = {code: dict(info) for code, info in master.items()}
#                 for code, info in overrides.items():
#                     if code in result:
#                         result[code].update(info)
#                     else:
#                         result[code] = dict(info)
#         else:
#             # Slots 1..5 must NOT inherit slot-0 visibility for classes that are
#             # absent in this slot override map. Seed with show=True, then apply
#             # slot-local override visibility/weight/color.
#             result = {}
#             for code, info in master.items():
#                 if not isinstance(info, dict):
#                     continue
#                 row = dict(info)
#                 row["show"] = True
#                 row["color"] = tuple(row.get("color", (128, 128, 128)))
#                 row["weight"] = float(row.get("weight", 1.0))
#                 row["description"] = str(row.get("description", ""))
#                 result[code] = row

#             for code, info in overrides.items():
#                 if code in result:
#                     result[code].update(info)
#                 else:
#                     result[code] = dict(info)

#     _palette_resolve_cache[slot_idx] = (fp, result)
#     return result

# def invalidate_palette_cache(slot_idx: int = None):
#     """
#     ✅ FIX: Bust the _palette_resolve_cache for one or all slots.
#     Call this whenever class_palette colors are changed (PTC color edit) so that
#     _get_slot_palette does not serve the stale fingerprint-cached palette.

#     Args:
#         slot_idx: specific slot to bust (0-5), or None to bust all slots.
#     """
#     global _palette_resolve_cache
#     if slot_idx is None:
#         _palette_resolve_cache.clear()
#     else:
#         _palette_resolve_cache.pop(slot_idx, None)


# def invalidate_unified_actor(app):
#     """
#     Null out any cached references to the unified actor and its indices on the app.
#     """
#     for attr in ["_unified_actor", "_main_global_mask", "_main_global_indices", "_rgb_buffer"]:
#         if hasattr(app, attr):
#             try:
#                 setattr(app, attr, None)
#                 # print(f"      - {attr} cleared")
#             except Exception:
#                 pass
#     print("   🧹 Unified actor references invalidated")

# def reset_uam(app):
#     """
#     Global reset for Unified Actor Manager. 
#     Wipes all cached shader contexts, LUTs, and actor references.
#     Call this when clearing a project or switching datasets.
#     """
#     global _shader_contexts, _lut_cache, _palette_resolve_cache
#     _shader_contexts.clear()
#     _lut_cache.clear()
#     if "_palette_resolve_cache" in globals():
#         _palette_resolve_cache.clear()
    
#     invalidate_unified_actor(app)
    
#     # Also clear any section-specific global indices
#     for i in range(4):
#         attr = f"_section_{i}_global_indices"
#         if hasattr(app, attr):
#             setattr(app, attr, None)
            
#     print("   ✨ Unified Actor Manager state fully reset")


# def _ensure_opengl_polydata_mapper(actor, cloud, use_spheres=True):
#     if actor is None:
#         return
#     raw_mapper = actor.GetMapper()
#     if hasattr(raw_mapper, 'GetMapper'):
#         raw_mapper = raw_mapper.GetMapper()
#     if not hasattr(raw_mapper, 'MapDataArrayToVertexAttribute'):
#         new_mapper = vtk.vtkOpenGLPolyDataMapper()
#         new_mapper.SetInputData(cloud)
#         new_mapper.SetScalarModeToUsePointFieldData()
#         new_mapper.SelectColorArray("RGB")
#         new_mapper.SetScalarVisibility(raw_mapper.GetScalarVisibility())
#         actor.SetMapper(new_mapper)
#         print("      ℹ️ Swapped vtkDataSetMapper → vtkOpenGLPolyDataMapper")


# def compute_point_size(weight: float, base: float = _BASE_POINT_SIZE) -> float:
#     min_size = max(0.5, base * 0.1)
#     return float(max(min_size, min(base * weight, 30.0)))


# def _compute_boundary_flags(xyz: np.ndarray, classification: np.ndarray = None,
#                              resolution: float = 0.0) -> np.ndarray:
#     """
#     Computes per-point structural boundary flags using three criteria:
#       1. Height discontinuity  — main structural edge detector (roof→ground drop)
#       2. Class-change          — only when neighbour cell is OCCUPIED + different class
#       3. Scan-edge             — point has < 4 of 8 occupied neighbours (true cloud edge)

#     Empty neighbour cells are NOT treated as boundary — this prevents dark-roof
#     artefacts where interior points with scan gaps were wrongly flagged.
#     """
#     n = len(xyz)
#     if n == 0:
#         return np.zeros(0, dtype=np.float32)

#     xy    = xyz[:, :2]
#     z_arr = xyz[:, 2].astype(np.float64)

#     xy_min = xy.min(axis=0)
#     xy_max = xy.max(axis=0)

#     if resolution <= 0.0:
#         w     = max(float(xy_max[0] - xy_min[0]), 1.0)
#         h_dim = max(float(xy_max[1] - xy_min[1]), 1.0)
#         resolution = max(0.05, np.sqrt(w * h_dim / n) * 1.5)

#     gc = ((xy - xy_min) / resolution).astype(np.int32) + 1
#     gx, gy   = gc[:, 0], gc[:, 1]
#     gx_max   = int(gx.max()) + 2
#     gy_max   = int(gy.max()) + 2

#     z_sum   = np.zeros((gx_max, gy_max), dtype=np.float64)
#     z_count = np.zeros((gx_max, gy_max), dtype=np.int32)
#     np.add.at(z_sum,   (gx, gy), z_arr)
#     np.add.at(z_count, (gx, gy), 1)
#     z_mean = np.where(z_count > 0, z_sum / np.maximum(z_count, 1), 0.0)

#     my_z_cell = z_mean[gx, gy]

#     z_global_range = max(float(z_arr.max() - z_arr.min()), 1.0)
#     z_thresh = max(0.5, z_global_range * 0.03)

#     has_class = classification is not None
#     if has_class:
#         class_grid = np.full((gx_max, gy_max), -1, dtype=np.int32)
#         class_grid[gx, gy] = classification.astype(np.int32)
#         my_class = classification.astype(np.int32)

#     _DIRS = [(-1, -1), (-1, 0), (-1, 1),
#              ( 0, -1),          ( 0, 1),
#              ( 1, -1), ( 1, 0), ( 1, 1)]

#     boundary     = np.zeros(n, dtype=bool)
#     occ_nb_count = np.zeros(n, dtype=np.int32)

#     for dx, dy in _DIRS:
#         nx, ny = gx + dx, gy + dy
#         nb_occ = (z_count[nx, ny] > 0)
#         occ_nb_count += nb_occ.astype(np.int32)

#         nb_z   = z_mean[nx, ny]
#         z_diff = np.where(nb_occ, np.abs(my_z_cell - nb_z), 0.0)
#         boundary |= nb_occ & (z_diff > z_thresh)

#         if has_class:
#             nb_cls = class_grid[nx, ny]
#             boundary |= nb_occ & (nb_cls >= 0) & (nb_cls != my_class)

#     boundary |= (occ_nb_count < 4)

#     pct = int(boundary.sum()) * 100 // max(n, 1)
#     print(f"      🔲 BoundaryFlag: {int(boundary.sum()):,}/{n:,} edge pts "
#           f"({pct}%, z_thresh={z_thresh:.2f}m, res={resolution:.3f}m)")

#     return boundary.astype(np.float32)


# def diagnose_weight_pipeline(app, slot_idx=1):
#     print("\n" + "=" * 70)
#     print(f"🔬 WEIGHT PIPELINE DIAGNOSTIC  (slot={slot_idx})")
#     print("=" * 70)

#     if slot_idx == 0:
#         actor_name = UNIFIED_ACTOR_NAME
#         vtk_widget = getattr(app, "vtk_widget", None)
#     else:
#         view_idx   = slot_idx - 1
#         actor_name = f"_section_{view_idx}_unified"
#         vtk_widget = (app.section_vtks.get(view_idx)
#                       if hasattr(app, "section_vtks") else None)

#     print(f"\n[1] Actor: {actor_name}")
#     if vtk_widget is None:
#         print("    ❌ No vtk_widget"); return

#     actor = vtk_widget.actors.get(actor_name) if hasattr(vtk_widget, 'actors') else None
#     if actor is None:
#         print("    ❌ Actor not found"); return

#     ctx = getattr(actor, '_naksha_shader_ctx', None)
#     print(f"\n[2] shader_ctx: {'✅' if ctx else '❌ None'}")
#     if ctx:
#         print(f"    _has_vertex_attr_cache:  {ctx._has_vertex_attr_cache}")
#         print(f"    _naksha_base_point_size: {getattr(actor, '_naksha_base_point_size', '?')}")
#         print(f"    _naksha_pps_observer_installed: {getattr(actor, '_naksha_pps_observer_installed', False)}")
#         print(f"    structured_border_mode:  {ctx.structured_border_mode}  "
#               f"(0=per-point, 1=structured, 2=hybrid)")
#         for c in [2, 6, 7]:
#             print(f"    class {c}: weight_lut={ctx.weight_lut[c]:.2f}  "
#                   f"vis={ctx.visibility_mask[c]:.1f}")

#     sp = actor.GetShaderProperty() if actor else None
#     print(f"\n[3] ShaderProperty: {'✅' if sp else '❌'}")
#     if sp:
#         v_uni = sp.GetVertexCustomUniforms()
#         f_uni = sp.GetFragmentCustomUniforms()
#         print(f"    VertexCustomUniforms:   {'✅' if v_uni else '❌'}")
#         print(f"    FragmentCustomUniforms: {'✅' if f_uni else '❌'}")
#         print(f"    _shaders_finalized_v27:  {getattr(actor, '_shaders_finalized_v27', False)}")



# classification fix of cut section


import numpy as np
import pyvista as pv
from vtkmodules.util import numpy_support
from typing import Optional, Dict
import time
import vtk
import os
import threading
import concurrent.futures
UNIFIED_ACTOR_NAME = "_naksha_unified_cloud"
MAIN_INTERACTION_ACTOR_NAME = "_naksha_main_interaction_lod"

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
_BASE_POINT_SIZE = 2.5
_BORDER_GROWTH_SCALE_PX = 4.0
_BORDER_GROWTH_CUBIC_PX = 8.0
_MAX_BORDER_GROWTH_PX = 8.0

# ── Per-border-type depth biases ─────────────────────────────────────────────
# Each border mode needs a different depth nudge because the geometry it draws
# (ring thickness, point-size growth) differs substantially between modes.
#
#   PER-POINT   — every point gets a ring; small bias keeps ring flush with core.
#   STRUCTURED  — only boundary-flagged points grow; larger bias ensures the
#                 grown shell reliably occludes the smaller interior points.
#   HYBRID      — all points grow AND boundary points paint a ring; the bias
#                 just needs to separate the ring fragment from the core
#                 fragment of the same (already-grown) point sprite.
# _BORDER_DEPTH_BIAS_PERPOINT    = 0.0002   # was the stable value before hybrid was added
# _BORDER_DEPTH_BIAS_STRUCTURED  = 0.001    # larger — boundary shell must cover interior pts
# _BORDER_DEPTH_BIAS_HYBRID      = 0.00005  # tiny — ring is on the same grown sprite

# NOTE: These are now *world-space* bias values in metres/units.
# The shader converts them to NDC at draw time using the camera near/far.
# This makes the bias camera-range-invariant (fixes border disappearing inside SNT/DXF grid
# where VTK tightens the clipping range around the Z-offset scene).
_BORDER_DEPTH_BIAS_PERPOINT_WORLD    = 0.05   # ~5cm world-space
_BORDER_DEPTH_BIAS_STRUCTURED_WORLD  = 0.25   # ~25cm — boundary shell
_BORDER_DEPTH_BIAS_HYBRID_WORLD      = 0.01   # ~1cm — ring on same sprite

# Driver-safe clip-depth offsets used by the point-border shader.  Keep these
# independent of camera uniforms: the previous camera-range conversion made
# the complete main point-cloud shader fail to link on the production context.
_BORDER_DEPTH_BIAS_PERPOINT   = 0.0002
# Structured separation scales with the border percentage.  At the standard
# 50% setting this resolves to 0.006 behind the coloured core.
_BORDER_DEPTH_BIAS_STRUCTURED = 0.012
_BORDER_DEPTH_BIAS_HYBRID     = 0.00005

# Fixed pixel-width for structured (object-edge) borders — stays constant at any zoom
_STRUCTURED_BORDER_PX = 2.0

# Progressive LOD for large section actors.
_LOD_FIRST_FRAME_MAX = 1_500_000
_LOD_SKIP_THRESHOLD = 2_000_000
_MAIN_INTERACTION_POINT_MAX = 1_000_000
# A sampled actor changes screen coverage and therefore changes the apparent
# class colors, weights and borders. Production navigation requires exact
# visual continuity, so sampled actor switching is deliberately disabled.
INTERACTION_ACTOR_SWITCHING_ENABLED = False
_section_build_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="naksha_sec",
)
_section_build_locks: dict = {}

def _section_render_point_cap(app) -> int:
    """
    Optional manual render cap for section views.
    Default behavior is uncapped (render all section points).
    Set env var NAKSHA_SECTION_POINT_BUDGET to enable a manual cap.
    """
    raw = str(os.getenv("NAKSHA_SECTION_POINT_BUDGET", "")).replace(",", "").strip()
    if not raw:
        return None
    try:
        env_cap = int(raw)
        if env_cap >= 100_000:
            return int(env_cap)
    except Exception:
        pass

    return None


def _uniform_pick_indices(n_points: int, keep_count: int) -> np.ndarray:
    if keep_count <= 0 or n_points <= 0:
        return np.empty((0,), dtype=np.int64)
    if keep_count >= n_points:
        return np.arange(n_points, dtype=np.int64)
    return np.linspace(0, n_points - 1, num=keep_count, dtype=np.int64)


def _get_section_build_lock(view_idx: int) -> threading.Lock:
    if view_idx not in _section_build_locks:
        _section_build_locks[view_idx] = threading.Lock()
    return _section_build_locks[view_idx]


def _build_section_numpy_data(all_pts, all_cls, palette, border_percent,
                              lut_builder_fn, boundary_fn):
    cls_f32 = all_cls.astype(np.float32, copy=False)
    rgb_buffer = lut_builder_fn(all_cls, palette)
    if border_percent > 0.0:
        bf = boundary_fn(all_pts, all_cls)
    else:
        bf = np.zeros(len(all_pts), dtype=np.float32)
    return {
        "pts": all_pts,
        "cls_f32": cls_f32,
        "rgb": rgb_buffer,
        "bf": bf,
    }

def _build_vtk_actor_from_arrays(vtk_widget, actor_name, arrays, actual_pt_size,
                                 palette, border_percent, slot_idx,
                                 attach_shader_fn, ensure_mapper_fn,
                                 view_shader_ctx_cls):
    cloud = pv.PolyData(arrays["pts"])
    class_vtk = numpy_support.numpy_to_vtk(arrays["cls_f32"], deep=True)
    class_vtk.SetName("Classification")
    cloud.GetPointData().AddArray(class_vtk)

    bf_vtk = numpy_support.numpy_to_vtk(arrays["bf"], deep=True)
    bf_vtk.SetName("BoundaryFlag")
    cloud.GetPointData().AddArray(bf_vtk)

    rgb_vtk = numpy_support.numpy_to_vtk(arrays["rgb"], deep=True)
    rgb_vtk.SetName("RGB")
    cloud.GetPointData().SetScalars(rgb_vtk)

    # Optional per-point flight-line visibility. Keeping this as a shader
    # attribute lets the main actor stay allocated while line selections are
    # changed; interaction LOD actors use the same attribute so hidden lines
    # do not reappear during pan/zoom.
    flight_values = arrays.get("flight_visible")
    if flight_values is not None:
        flight_vtk = numpy_support.numpy_to_vtk(
            np.ascontiguousarray(flight_values, dtype=np.uint8), deep=True
        )
        flight_vtk.SetName("FlightVisible")
        cloud.GetPointData().AddArray(flight_vtk)

    actor = vtk_widget.add_points(
        cloud, scalars="RGB", rgb=True,
        point_size=actual_pt_size,
        render_points_as_spheres=False,
        name=actor_name,
        reset_camera=False, render=False,
    )
    if actor is None:
        return None, None, None, None, None, None, None, None
    actor.GetProperty().LightingOff()

    try:
        actor._naksha_render_window = vtk_widget.render_window
        actor._naksha_renderer = vtk_widget.renderer
    except Exception:
        pass

    ensure_mapper_fn(actor, cloud)
    sbm = actor.GetMapper()
    mesh = sbm.GetInput() if sbm is not None else None
    if mesh is None:
        return None, None, None, None, None, None, None, None

    vtk_ca = mesh.GetPointData().GetScalars()
    if vtk_ca is None:
        return None, None, None, None, None, None, None, None

    _vtk_rgb = numpy_support.vtk_to_numpy(vtk_ca)
    np.copyto(_vtk_rgb, arrays["rgb"])
    vtk_ca.Modified()

    _class_vtk_arr = mesh.GetPointData().GetArray("Classification")
    _vtk_cls = (numpy_support.vtk_to_numpy(_class_vtk_arr)
                if _class_vtk_arr is not None else arrays["cls_f32"].copy())

    ctx = view_shader_ctx_cls(slot_idx=slot_idx)
    ctx.load_from_palette(palette, border_percent, actual_pt_size)
    attach_shader_fn(actor, ctx, actor_name)

    return actor, ctx, mesh, vtk_ca, _vtk_rgb, _vtk_cls, class_vtk, bf_vtk


def _wire_actor_metadata(actor, mesh, vtk_ca, _vtk_rgb, _vtk_cls, class_vtk,
                         bf_vtk, combined_global_mask, actual_pt_size):
    actor._naksha_rgb_ptr = _vtk_rgb
    actor._naksha_vtk_array = vtk_ca
    actor._naksha_vtk_rgb_ref = vtk_ca
    actor._naksha_class_vtk_ref = class_vtk
    actor._naksha_mesh = mesh
    actor._naksha_section_class = _vtk_cls
    actor._naksha_section_mask = combined_global_mask
    actor._naksha_global_to_local_arr = None
    actor._naksha_global_indices = None
    actor._naksha_base_point_size = actual_pt_size
    actor._naksha_boundary_vtk = bf_vtk


# ─────────────────────────────────────────────────────────────────────────────
# VIEW SHADER CONTEXT
# ─────────────────────────────────────────────────────────────────────────────
class ViewShaderContext:
    __slots__ = (
        "slot_idx", "visibility_mask", "weight_lut", "color_lut",
        "border_ring", "_fingerprint", "_observer_id", "_generation",
        "_has_vertex_attr_cache",
        "_vis_list_cache", "_wt_list_cache", "structured_border_mode",
    )

    def __init__(self, slot_idx: int = 0):
        self.slot_idx            = slot_idx
        self.visibility_mask     = np.ones(256, dtype=np.float32)
        self.weight_lut          = np.full(256, _BASE_POINT_SIZE, dtype=np.float32)
        self.color_lut           = np.full(256 * 3, 0.5, dtype=np.float32)
        self.border_ring         = np.float32(0.0)
        self._fingerprint: Optional[int] = None
        self._observer_id: Optional[int] = None
        self._generation: int    = 0
        self._vis_list_cache     = None
        self._wt_list_cache      = None
        self._has_vertex_attr_cache = False
        # Every view starts in Structured mode.  A different value may only be
        # supplied by the per-view cache after an explicit user selection.
        self.structured_border_mode = 1.0

    def load_from_palette(self, palette: dict, border_percent: float = 0.0,
                          base_point_size: float = _BASE_POINT_SIZE) -> bool:
        palette = palette or {}
        fp = _palette_fingerprint_full(palette, border_percent)
        if fp == self._fingerprint:
            return False
        self._fingerprint = fp

        self.visibility_mask[:] = 1.0
        self.weight_lut[:] = base_point_size
        self.color_lut[:] = 0.5

        for code, info in palette.items():
            idx = int(code)
            if idx < 0 or idx >= 256:
                continue
            self.visibility_mask[idx] = 1.0 if info.get("show", True) else 0.0
            raw_weight = float(info.get("weight", 1.0))
            clamped_weight = max(0.1, min(raw_weight, 12.0))
            self.weight_lut[idx] = compute_point_size(clamped_weight, base_point_size)
            r, g, b = info.get("color", (128, 128, 128))
            base = idx * 3
            self.color_lut[base]     = r / 255.0
            self.color_lut[base + 1] = g / 255.0
            self.color_lut[base + 2] = b / 255.0

        self.border_ring = np.float32(min(1.0, max(0.0, border_percent / 100.0)))
        self._generation += 1
        self._vis_list_cache = None
        self._wt_list_cache  = None
        return True

    def force_reload(self):
        self._fingerprint    = None
        self._vis_list_cache = None
        self._wt_list_cache  = None

    def vis_as_list(self):
        if self._vis_list_cache is None:
            self._vis_list_cache = self.visibility_mask.tolist()
        return self._vis_list_cache

    def wt_as_list(self):
        if self._wt_list_cache is None:
            self._wt_list_cache = self.weight_lut.tolist()
        return self._wt_list_cache

    def clone_for_view(self, new_slot_idx: int) -> 'ViewShaderContext':
        ctx = ViewShaderContext(new_slot_idx)
        np.copyto(ctx.visibility_mask, self.visibility_mask)
        np.copyto(ctx.weight_lut,      self.weight_lut)
        np.copyto(ctx.color_lut,       self.color_lut)
        ctx.border_ring             = self.border_ring
        ctx._fingerprint            = self._fingerprint
        ctx._has_vertex_attr_cache  = self._has_vertex_attr_cache
        ctx.structured_border_mode  = self.structured_border_mode
        return ctx


def _palette_fingerprint_full(palette: dict, border_percent: float = 0.0) -> int:
    try:
        return hash((
            tuple(
                (k, v.get("show", True),
                 tuple(v.get("color", (128, 128, 128))),
                 round(float(v.get("weight", 1.0)), 3))
                for k, v in sorted(palette.items())
            ),
            round(border_percent, 2),
        ))
    except Exception:
        return id(palette) ^ int(border_percent * 100)


# ─────────────────────────────────────────────────────────────────────────────
# SHADER REGISTRY
# ─────────────────────────────────────────────────────────────────────────────
_shader_contexts: Dict[str, ViewShaderContext] = {}


def get_shader_context(actor_name: str) -> Optional[ViewShaderContext]:
    return _shader_contexts.get(actor_name)


def _safe_direct_render(vtk_widget) -> bool:
    """Bypass GPURenderManager throttling for a commit-time repaint."""
    if vtk_widget is None:
        return False
    try:
        wrapped = getattr(vtk_widget, "render", None)
        original = None
        mgr = None
        try:
            from gui.gpu_render_manager import GPURenderManager
            original = getattr(vtk_widget, "render", None)
            mgr = getattr(vtk_widget, "_naksha_gpu_render_manager", None)
            if mgr is not None and hasattr(mgr, "_original_render") and mgr._original_render is not None:
                original = mgr._original_render
        except Exception:
            original = None

        if original is not None and original is not wrapped:
            original()
            return True

        rw = vtk_widget.GetRenderWindow() if hasattr(vtk_widget, "GetRenderWindow") else None
        if rw is None:
            return False
        rw.Render()
        return True
    except Exception:
        return False


def guarantee_main_view_visual_refresh(app, changed_mask, to_class=None, reason="",
                                       old_classes=None, new_classes=None,
                                       origin_view="unknown") -> bool:
    """
    Commit-time safety net for main-view visual flushes.

    Validates the changed mask, refreshes the active main actor in-place, forces
    VTK dirty flags, and performs one immediate non-throttled render.
    """
    if app is None or not hasattr(app, "data") or not app.data:
        return False

    classification = app.data.get("classification")
    if classification is None:
        return False

    changed_mask = np.asarray(changed_mask) if changed_mask is not None else None
    if changed_mask is None or changed_mask.dtype != bool:
        return False
    if changed_mask.ndim != 1 or len(changed_mask) != len(classification):
        return False

    changed_count = int(np.count_nonzero(changed_mask))
    if changed_count <= 0:
        return False

    changed_indices = np.flatnonzero(changed_mask).astype(np.int64, copy=False)
    app._last_changed_mask = changed_mask
    app._last_changed_indices = changed_indices.copy()

    display_mode = str(getattr(app, "display_mode", "class") or "class").lower()
    fast_update = False
    dirty_flags = False
    render_ok = False
    actor = None
    surface_owns_present = False

    try:
        if display_mode == "class":
            # A commit can contain values produced by a height/fence operation,
            # undo/redo, or another batched classifier.  ``app.to_class`` is a
            # persistent shortcut selection and can therefore be stale here.
            # Patch both GPU buffers from the canonical classification array;
            # never repaint a committed mask from the shortcut hint.
            actor = _get_unified_actor(app)
            if actor is not None:
                global_indices = getattr(app, "_main_global_indices", None)
                if global_indices is not None:
                    local_changed = np.flatnonzero(changed_mask[global_indices])
                else:
                    local_changed = changed_indices
                if local_changed.size > 0:
                    _patch_actor_memory(app, actor, local_changed, slot_idx=0)
                fast_update = True
        elif display_mode == "shaded_class":
            try:
                from gui.shading_display import (
                    ClassificationDelta,
                    refresh_shaded_after_classification_fast,
                )

                delta = None
                if old_classes is not None and new_classes is not None:
                    delta = ClassificationDelta(
                        changed_indices=changed_indices,
                        old_classes=np.asarray(old_classes),
                        new_classes=np.asarray(new_classes),
                        operation=reason or "classification",
                        origin_view=origin_view,
                    )
                elif getattr(app, "_pending_shading_delta", None) is not None:
                    pending = app._pending_shading_delta
                    pending_idx = np.asarray(
                        pending.changed_indices, dtype=np.int64
                    ).ravel()
                    if np.array_equal(pending_idx, changed_indices):
                        delta = pending
                        app._pending_shading_delta = None
                refresh_shaded_after_classification_fast(
                    app, changed_mask, delta=delta
                )
                actor = getattr(app, "_shaded_mesh_actor", None)
                fast_update = True
            except Exception:
                fast_update = False
        elif display_mode == "surface":
            try:
                refresh_fn = getattr(app, "refresh_surface_after_classification", None)
                if callable(refresh_fn):
                    refresh_fn(reason="main_view_commit", changed_mask=changed_mask)
                else:
                    from gui.surface_mode import refresh_surface_after_classification
                    refresh_surface_after_classification(
                        app, changed_mask, operation="main_view_commit", delay_ms=0
                    )
                fast_update = True
                # Surface classification refresh owns Surface presentation.
                # - no-topology edit: nothing in main view changed, do NOT redraw
                # - exact rebuild: render_surface_mode already presents the mesh
                # - debounced topology edit: timer will present when ready
                surface_owns_present = True
                actor = getattr(app, "_surface_mesh_actor", None)
            except Exception:
                fast_update = False
                surface_owns_present = False

        # Never dirty the hidden unified point actor while Surface is active.
        # Doing so used to turn a metadata-only classification into a large VTK
        # upload/render even after Surface itself correctly skipped rebuilding.
        if display_mode != "surface":
            actor = actor or _get_unified_actor(app)
            if actor is not None:
                mesh = getattr(actor, "_naksha_mesh", None)
                vtk_ca = getattr(actor, "_naksha_vtk_array", None)
                mapper = actor.GetMapper() if hasattr(actor, "GetMapper") else None
                if vtk_ca is not None:
                    vtk_ca.Modified()
                    dirty_flags = True
                if mesh is not None:
                    mesh.GetPointData().Modified()
                    mesh.Modified()
                    dirty_flags = True
                if mapper is not None:
                    mapper.Modified()
                    dirty_flags = True
                actor.Modified()
                dirty_flags = True
    except Exception as e:
        print(f"⚠️ guarantee_main_view_visual_refresh prep failed: {e}")

    vtk_widget = getattr(app, "vtk_widget", None)
    defer_cross_shaded_present = (
        display_mode == "shaded_class"
        and reason == "cross_section_gpu_commit"
    )
    render_started = time.perf_counter()
    if display_mode == "surface" and surface_owns_present:
        # Surface refresh either changed nothing, already presented an exact
        # rebuild, or scheduled the exact rebuild.  No synchronous giant-mesh
        # redraw is required here.
        render_ok = True
    elif defer_cross_shaded_present:
        # A full faceted draw can take 100+ ms with every class visible. The
        # canonical buffers are already patched above; let the mouse event
        # finish, then coalesce rapid cross-section commits into one draw.
        try:
            from gui.shading_display import _schedule_fast_shaded_present
            pending_present = getattr(app, "_shading_present_timer", None)
            if pending_present is not None:
                try:
                    pending_present.stop()
                    pending_present.deleteLater()
                except Exception:
                    pass
                app._shading_present_timer = None
            _schedule_fast_shaded_present(
                app,
                delay_ms=80,
                restart=True,
                wait_while_preview=True,
            )
            render_ok = True
        except Exception:
            render_ok = _safe_direct_render(vtk_widget)
    else:
        # A shaded local patch may have scheduled a presentation for direct
        # callers. A synchronous commit owns presentation, so cancel its copy.
        pending_present = getattr(app, "_shading_present_timer", None)
        if pending_present is not None:
            try:
                pending_present.stop()
                pending_present.deleteLater()
            except Exception:
                pass
            app._shading_present_timer = None
        render_ok = _safe_direct_render(vtk_widget)
    render_ms = (time.perf_counter() - render_started) * 1000.0

    if render_ok:
        app._gpu_sync_done = True
        app._section_visibility_refresh_required = bool(
            getattr(app, "_section_visibility_refresh_required", False)
        )

    commit_id = int(getattr(app, "_main_refresh_commit_id", 0)) + 1
    app._main_refresh_commit_id = commit_id
    if commit_id <= 20:
        print(
            f"MAIN_REFRESH_GUARANTEE source=canonical_classification_array reason={reason} mode={display_mode} "
            f"changed={changed_count} fast_update={bool(fast_update)} "
            f"dirty_flags={bool(dirty_flags)} render={bool(render_ok)} "
            f"deferred={bool(defer_cross_shaded_present)} "
            f"render_ms={render_ms:.1f} "
            f"gpu_sync_done_before={bool(getattr(app, '_gpu_sync_done', False))}"
        )

    return bool(render_ok)


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS — defined first so every function below can call them safely
# ─────────────────────────────────────────────────────────────────────────────

def _mark_actor_dirty(actor) -> None:
    """Full VTK dirty chain for any in-place buffer modification."""
    mesh = getattr(actor, '_naksha_mesh', None)
    if mesh is None:
        return
    mesh.GetPointData().Modified()
    mesh.Modified()
    mapper = actor.GetMapper()
    if mapper is not None:
        mapper.Modified()
    actor.Modified()


def _rewrite_rgb_from_palette(rgb_ptr: np.ndarray, classification: np.ndarray,
                               palette: dict):
    """Full buffer re-write (O(N)). Use only for initial load or full palette shifts."""
    max_c = max(int(classification.max()) + 1, 256)
    lut = _get_lut_array(palette, max_c)
    np.copyto(rgb_ptr, lut[classification.clip(0, max_c - 1).astype(np.intp)])

def _rewrite_rgb_partial(rgb_ptr: np.ndarray, classes_to_apply: np.ndarray,
                         palette: dict, local_indices: np.ndarray):
    """Partial buffer update (O(M) where M is changed points). Critical for 48M+ point sets."""
    if local_indices is None or len(local_indices) == 0:
        return
    max_c = max(int(classes_to_apply.max()) + 1, 256)
    lut = _get_lut_array(palette, max_c)
    changed_classes = classes_to_apply.clip(0, max_c - 1).astype(np.intp)
    rgb_ptr[local_indices] = lut[changed_classes]

def _get_lut_array(palette: dict, max_c: int) -> np.ndarray:
    """Helper to build color lookup table."""
    lut = np.full((max_c, 3), 128, dtype=np.uint8)
    for code, info in palette.items():
        idx = int(code)
        if 0 <= idx < max_c:
            lut[idx] = (info.get("color", (128, 128, 128))
                        if info.get("show", True) else (0, 0, 0))
    return lut


def _touch_vtk_arrays(actor):
    vtk_ca = getattr(actor, '_naksha_vtk_array', None)
    if vtk_ca:
        vtk_ca.Modified()
    _mark_actor_dirty(actor)


def _is_writable(arr: np.ndarray) -> bool:
    return arr.flags.writeable


def _apply_border_once(actor, border_percent: float):
    """
    Fallback border shader for per-class actors that bypass
    _attach_view_shader_context.
    """
    if border_percent <= 0:
        return

    ctx = getattr(actor, '_naksha_shader_ctx', None)
    if ctx is not None:
        new_ring = np.float32(min(0.50, max(0.0, border_percent / 100.0)))
        if ctx.border_ring != new_ring:
            ctx.border_ring = new_ring
            ctx._fingerprint = None
        return

    cached = getattr(actor, "_naksha_border_percent", None)
    if cached == border_percent:
        return
    try:
        ring_val = min(1.0, max(0.0, border_percent / 100.0))
        sp        = actor.GetShaderProperty()
        if sp is None:
            return

        if ring_val <= 0.001:
            frag_code = (
                "//VTK::Color::Impl\n"
                "vec2 naksha_uv = gl_PointCoord.xy - vec2(0.5);\n"
                "if (dot(naksha_uv, naksha_uv) > 0.25) discard;\n"
                "opacity = 1.0;\n"
            )
        else:
            ring_frac = min(0.25, ring_val * 0.5)
            inner     = 1.0 - ring_frac
            inner_sq  = 0.25 * inner * inner
            frag_code = (
                "//VTK::Color::Impl\n"
                "// Naksha per-class fallback border (round-circle)\n"
                "vec2 naksha_uv = gl_PointCoord.xy - vec2(0.5);\n"
                "float naksha_radius_sq = dot(naksha_uv, naksha_uv);\n"
                "if (naksha_radius_sq > 0.25) discard;\n"
                f"if (naksha_radius_sq >= {inner_sq:.8f}) {{\n"
                "    diffuseColor = vec3(0.0, 0.0, 0.0);\n"
                "    ambientColor = vec3(0.0, 0.0, 0.0);\n"
                "}\n"
                "opacity = 1.0;\n"
            )

        sp.ClearAllFragmentShaderReplacements()
        sp.AddFragmentShaderReplacement("//VTK::Color::Impl", True, frag_code, False)
        sp.Modified()
        actor.GetProperty().Modified()
        mapper = actor.GetMapper()
        if mapper:
            mapper.Modified()
        actor.Modified()
        actor._naksha_border_percent = border_percent

    except Exception as e:
        print(f"      ⚠️ _apply_border_once failed: {e}")


def update_visibility_lut(actor, palette, base_point_size=_BASE_POINT_SIZE):
    ctx = getattr(actor, '_naksha_shader_ctx', None)
    if ctx is not None:
        ctx.force_reload()
        ctx.load_from_palette(palette, float(ctx.border_ring * 100.0), base_point_size)
        _push_uniforms_direct(actor, ctx)
        return
    vis_arr = np.zeros(256, dtype=np.float32)
    for i in range(256):
        info = (palette or {}).get(i, {})
        vis_arr[i] = (compute_point_size(info.get("weight", 1.0), base_point_size)
                      if info.get("show", True) else 0.0)
    actor._local_vis_arr = vis_arr
    try:
        actor.GetMapper().Modified()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# GL_PROGRAM_POINT_SIZE — persistent observer helpers
# ─────────────────────────────────────────────────────────────────────────────

def _try_enable_program_point_size(render_window) -> bool:
    """
    Enable GL_PROGRAM_POINT_SIZE (0x8642) so vertex shaders can write gl_PointSize.
    Must be called AFTER the OpenGL context is initialized (after at least one render).
    Returns True if the state object was available and the call succeeded.
    """
    if render_window is None:
        return False
    try:
        if hasattr(render_window, 'GetState'):
            state = render_window.GetState()
            if state and hasattr(state, 'vtkglEnable'):
                state.vtkglEnable(0x8642)          # GL_PROGRAM_POINT_SIZE
                return True
    except Exception as e:
        print(f"      ⚠️ GL_PROGRAM_POINT_SIZE enable: {e}")
    return False


def _update_actor_camera_uniforms(actor) -> bool:
    """Push the active camera range used only by the point-border shader."""
    if actor is None or not actor.GetVisibility():
        return False
    try:
        renderer = getattr(actor, "_naksha_renderer", None)
        if renderer is None:
            return False
        near, far = renderer.GetActiveCamera().GetClippingRange()
        uniforms = actor.GetShaderProperty().GetFragmentCustomUniforms()
        if uniforms is None:
            return False
        uniforms.SetUniformf("naksha_near", float(near))
        uniforms.SetUniformf("naksha_far", float(far))
        return True
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return False


def refresh_widget_camera_uniforms(vtk_widget) -> int:
    """Refresh camera-dependent shader uniforms before, never during, render."""
    if vtk_widget is None:
        return 0
    updated = 0
    try:
        actors = getattr(vtk_widget, "actors", {}) or {}
        for shader_actor in tuple(actors.values()):
            if _update_actor_camera_uniforms(shader_actor):
                updated += 1
    except (RuntimeError, AttributeError, ReferenceError):
        return updated
    except Exception:
        return updated
    return updated


def _install_program_point_size_observer(actor, render_window) -> bool:
    """
    Install a StartEvent observer on the render window so that
    GL_PROGRAM_POINT_SIZE is re-enabled before EVERY draw call.

    VTK's state machine can reset GL flags between renders. This observer
    is the only guaranteed way to keep the flag set persistently.

    Safe to call multiple times — the guard flag prevents duplicate observers.
    """
    if render_window is None or getattr(render_window, '_naksha_pps_observer_installed', False):
        return getattr(render_window, '_naksha_pps_observer_installed', False)

    def _pps_start_event(caller, event):
        try:
            state = caller.GetState()
            if state and hasattr(state, 'vtkglEnable'):
                state.vtkglEnable(0x8642)
        except Exception:
            pass


    try:
        obs_id = render_window.AddObserver('StartEvent', _pps_start_event)
        render_window._naksha_pps_observer_installed = True
        render_window._naksha_pps_observer_id        = obs_id
        print("      ✅ GL_PROGRAM_POINT_SIZE StartEvent observer installed ON WINDOW")
        return True
    except Exception as e:
        print(f"      ⚠️ PPS observer install failed: {e}")
        return False


def _deferred_actor_gpu_init(actor, ctx, plotter, label: str = "actor"):
    """
    Called from the first idle turn after build_unified_actor /
    build_section_unified_actor.

    By the time this fires the VTK window has rendered at least once, so
    GetState() is guaranteed to return a valid OpenGL state object.  We:

      1. Enable GL_PROGRAM_POINT_SIZE immediately (one-shot).
      2. Install the persistent StartEvent observer so it stays enabled.
      3. Re-push all GPU uniforms (weight_lut, visibility_lut, border_ring_val).
      4. Trigger one more render so the updated point sizes appear.
    """
    try:
        rw = getattr(actor, '_naksha_render_window', None)
        if rw is None:
            try:
                rw = plotter.render_window
                actor._naksha_render_window = rw
            except Exception:
                pass

        enabled = _try_enable_program_point_size(rw)
        _install_program_point_size_observer(actor, rw)

        if enabled:
            actor._naksha_needs_program_point_size = False
            print(f"      ✅ Deferred GPU init: GL_PROGRAM_POINT_SIZE enabled for {label}")
        else:
            print(f"      ⚠️ Deferred GPU init: GL state still unavailable for {label}")

        _push_uniforms_direct(actor, ctx)

        try:
            plotter.render()
        except Exception:
            pass

    except Exception as e:
        print(f"      ⚠️ _deferred_actor_gpu_init ({label}): {e}")


# ─────────────────────────────────────────────────────────────────────────────
# CORE: PUSH UNIFORMS DIRECT
# ─────────────────────────────────────────────────────────────────────────────
def _push_uniforms_direct(actor, ctx: 'ViewShaderContext') -> bool:
    if actor is None or ctx is None:
        return False
    try:
        rw = getattr(actor, '_naksha_render_window', None)

        if rw:
            if not getattr(rw, '_naksha_pps_observer_installed', False):
                if _try_enable_program_point_size(rw):
                    _install_program_point_size_observer(actor, rw)
                    actor._naksha_needs_program_point_size = False
                    print("      ✅ GL_PROGRAM_POINT_SIZE enabled via _push_uniforms_direct")
                else:
                    actor._naksha_needs_program_point_size = True
            else:
                actor._naksha_needs_program_point_size = False

        sp = actor.GetShaderProperty()
        if sp is None:
            return False

        attached_ctx = getattr(actor, '_naksha_shader_ctx', ctx)
        has_vertex   = attached_ctx._has_vertex_attr_cache

        if has_vertex:
            v_uni = sp.GetVertexCustomUniforms()
            if v_uni:
                v_uni.SetUniform1fv("visibility_lut", 256, ctx.vis_as_list())
                v_uni.SetUniform1fv("weight_lut",     256, ctx.wt_as_list())
                v_uni.SetUniformf("border_ring_val", float(ctx.border_ring))
                v_uni.SetUniformf("structured_border_mode", float(getattr(ctx, 'structured_border_mode', 0.0)))
                v_uni.Modified()
                sp.Modified()

        f_uni = sp.GetFragmentCustomUniforms()
        if f_uni:
            f_uni.SetUniformf("border_ring_val", float(ctx.border_ring))
            f_uni.SetUniformf("structured_border_mode", float(getattr(ctx, 'structured_border_mode', 0.0)))
            _update_actor_camera_uniforms(actor)

        actor.GetMapper().Modified()
        actor.GetProperty().Modified()
        return True

    except Exception as e:
        print(f"⚠️ _push_uniforms_direct failed: {e}")
        return False


_push_shader_uniforms = _push_uniforms_direct


def _coerce_border_logic_mode(value, default: float = 1.0) -> float:
    """Return the supported GPU border mode value (0, 1, or 2)."""
    try:
        mode = int(float(value))
    except (TypeError, ValueError, OverflowError):
        mode = int(default)
    return float(mode if mode in (0, 1, 2) else int(default))


def _read_saved_border_logic_mode() -> float:
    """Read border logic without requiring the Display Mode dialog to exist."""
    try:
        from PySide6.QtCore import QSettings

        settings = QSettings("NakshaAI", "LidarApp")
        saved_mode = settings.value("global_border_logic_mode")
        if saved_mode is not None:
            return _coerce_border_logic_mode(saved_mode)

        legacy = settings.value("global_structured_border")
        if legacy is not None:
            return 1.0 if str(legacy).strip().lower() == "true" else 0.0
    except Exception:
        pass
    return 1.0


def reset_border_logic_to_structured(app) -> None:
    """Start a newly loaded dataset/PTC with Structured borders in all views.

    This updates only border-logic state.  Palette, visibility, point size,
    camera and actor ownership are deliberately untouched.
    """
    if app is None:
        return

    structured_modes = {slot_idx: 1.0 for slot_idx in range(6)}
    app._naksha_border_logic_modes = structured_modes

    dialog = (
        getattr(app, "display_mode_dialog", None)
        or getattr(app, "display_dialog", None)
    )
    if dialog is not None:
        dialog.view_border_modes = {slot_idx: 1 for slot_idx in range(6)}
        if hasattr(dialog, "_select_border_mode"):
            dialog._select_border_mode(1, push_gpu=False)
        # A deferred QSettings value must not revive Per-Point mode after load.
        if hasattr(dialog, "_pending_border_logic_mode"):
            dialog._pending_border_logic_mode = None

    # PTC loads rebuild existing actors rather than clearing them, so update
    # their contexts immediately; the following palette sync pushes uniforms.
    for ctx in _shader_contexts.values():
        ctx.structured_border_mode = 1.0

    print("   Structured border mode initialized for all views")


def resolve_border_logic_mode(app, slot_idx: int) -> float:
    """Resolve border logic for the target slot, never another visible slot."""
    slot_idx = int(slot_idx)
    dialog = (
        getattr(app, "display_mode_dialog", None)
        or getattr(app, "display_dialog", None)
    )

    if dialog is not None:
        current_slot = int(getattr(dialog, "current_slot", -1))
        if current_slot == slot_idx and hasattr(dialog, "get_border_mode"):
            mode = _coerce_border_logic_mode(dialog.get_border_mode())
            modes = getattr(dialog, "view_border_modes", None)
            if isinstance(modes, dict):
                modes[slot_idx] = int(mode)
        else:
            modes = getattr(dialog, "view_border_modes", None)
            mode = (
                _coerce_border_logic_mode(modes[slot_idx])
                if isinstance(modes, dict) and slot_idx in modes
                else None
            )
        if mode is not None:
            cache = getattr(app, "_naksha_border_logic_modes", None)
            if not isinstance(cache, dict):
                cache = {}
                app._naksha_border_logic_modes = cache
            cache[slot_idx] = mode
            return mode

    cache = getattr(app, "_naksha_border_logic_modes", None)
    if isinstance(cache, dict) and slot_idx in cache:
        return _coerce_border_logic_mode(cache[slot_idx])

    mode = _read_saved_border_logic_mode()
    if app is not None:
        cache = cache if isinstance(cache, dict) else {}
        cache[slot_idx] = mode
        app._naksha_border_logic_modes = cache
    return mode


def _set_context_border_logic_mode(app, slot_idx: int, ctx) -> float:
    mode = resolve_border_logic_mode(app, slot_idx)
    previous = _coerce_border_logic_mode(
        getattr(ctx, "structured_border_mode", 0.0)
    )
    ctx.structured_border_mode = mode
    if previous != mode:
        labels = {0: "Per-Point", 1: "Structured", 2: "Hybrid"}
        print(
            f"      Border logic slot {slot_idx}: "
            f"{labels[int(previous)]} -> {labels[int(mode)]}"
        )
    return mode

# ─────────────────────────────────────────────────────────────────────────────
# SHADER ATTACHMENT — called ONCE at actor build time
# ─────────────────────────────────────────────────────────────────────────────
def _attach_view_shader_context(actor, ctx, actor_name, use_sphere_shaders=True):
    if actor is None:
        return

    actor._naksha_shader_ctx = ctx
    _shader_contexts[actor_name] = ctx

    mesh = getattr(actor, '_naksha_mesh', None)
    _ensure_opengl_polydata_mapper(actor, mesh)

    vertex_attr_wired = False
    try:
        mapper = actor.GetMapper()
        raw_m  = mapper.GetMapper() if hasattr(mapper, 'GetMapper') else mapper
        raw_m.MapDataArrayToVertexAttribute(
            "class_code", "Classification",
            vtk.vtkDataObject.FIELD_ASSOCIATION_POINTS, -1
        )
        raw_m.MapDataArrayToVertexAttribute(
            "boundary_flag", "BoundaryFlag",
            vtk.vtkDataObject.FIELD_ASSOCIATION_POINTS, -1
        )
        has_flight_visibility = bool(
            mesh is not None
            and mesh.GetPointData().GetArray("FlightVisible") is not None
        )
        if has_flight_visibility:
            raw_m.MapDataArrayToVertexAttribute(
                "flight_visible", "FlightVisible",
                vtk.vtkDataObject.FIELD_ASSOCIATION_POINTS, -1
            )
        vertex_attr_wired = True
        print(f"      🔗 Linked 'Classification' + 'BoundaryFlag' to shader")
    except Exception as e:
        print(f"      ⚠️ Shader Attribute Mapping failed: {e}")

    ctx._has_vertex_attr_cache = vertex_attr_wired
    actor.GetProperty().SetRenderPointsAsSpheres(False)

    sp = actor.GetShaderProperty()

    if not hasattr(actor, "_shaders_finalized_v29"):
        sp.ClearAllVertexShaderReplacements()
        sp.ClearAllFragmentShaderReplacements()

        # ── Vertex declarations ──────────────────────────────────────────────
        flight_decl = "in float flight_visible;\n" if has_flight_visibility else ""
        flight_hide = (
            "  if (flight_visible < 0.5) {\n"
            "    gl_Position = vec4(2.0, 2.0, 2.0, 1.0);\n"
            "    gl_PointSize = 0.0;\n"
            "    v_point_size = 0.0;\n"
            "    v_core_size = 0.0;\n"
            "  } else \n"
            if has_flight_visibility else ""
        )
        sp.AddVertexShaderReplacement(
            "//VTK::PositionVC::Dec", True,
            "//VTK::PositionVC::Dec\n"
            "in  float class_code;\n"
            "in  float boundary_flag;\n"
            + flight_decl +
            "out float v_point_size;\n"
            "out float v_core_size;\n"
            "out float v_boundary;\n",
            False
        )

        # ── Vertex implementation ────────────────────────────────────────────
        # Vertex logic is the same for all three modes — the difference is only
        # which points GROW (all vs boundary-only) which is already branched on
        # structured_border_mode.  No depth work happens here.
        sp.AddVertexShaderReplacement(
            "//VTK::PositionVC::Impl", True,
            "//VTK::PositionVC::Impl\n"
            "  int c_idx = clamp(int(class_code + 0.5), 0, 255);\n"
            "  v_boundary = boundary_flag;\n"
            + flight_hide +
            "  if (visibility_lut[c_idx] <= 0.0) {\n"
            "    gl_Position  = vec4(2.0, 2.0, 2.0, 1.0);\n"
            "    gl_PointSize = 0.0;\n"
            "    v_point_size = 0.0;\n"
            "    v_core_size  = 0.0;\n"
            "  } else {\n"
            "    float ps = max(1.0, weight_lut[c_idx]);\n"
            "    if (structured_border_mode > 1.5) {\n"
            "      // HYBRID: every point grows (for the border ring on all points)\n"
            f"      float border_growth = clamp((border_ring_val * {_BORDER_GROWTH_SCALE_PX:.1f}) + (border_ring_val * border_ring_val * border_ring_val * {_BORDER_GROWTH_CUBIC_PX:.1f}), 0.0, {_MAX_BORDER_GROWTH_PX:.1f});\n"
            "      float total_ps = ps + border_growth;\n"
            "      gl_PointSize = total_ps;\n"
            "      v_point_size = total_ps;\n"
            "      v_core_size  = ps;\n"
            "    } else if (structured_border_mode > 0.5) {\n"
            "      // STRUCTURED: only boundary-flagged points grow\n"
            f"      float border_growth = (boundary_flag > 0.5)\n"
            f"        ? clamp(\n"
            f"            (border_ring_val * {_BORDER_GROWTH_SCALE_PX:.1f})\n"
            f"            + (border_ring_val * border_ring_val * border_ring_val * {_BORDER_GROWTH_CUBIC_PX:.1f}),\n"
            f"            0.0, {_MAX_BORDER_GROWTH_PX:.1f}\n"
            f"          )\n"
            "        : 0.0;\n"
            "      float total_ps = ps + border_growth;\n"
            "      gl_PointSize = total_ps;\n"
            "      v_point_size = total_ps;\n"
            "      v_core_size  = ps;\n"
            "    } else {\n"
            "      // PER-POINT: every point grows uniformly\n"
            f"      float border_growth = clamp((border_ring_val * {_BORDER_GROWTH_SCALE_PX:.1f}) + (border_ring_val * border_ring_val * border_ring_val * {_BORDER_GROWTH_CUBIC_PX:.1f}), 0.0, {_MAX_BORDER_GROWTH_PX:.1f});\n"
            "      float total_ps = ps + border_growth;\n"
            "      gl_PointSize = total_ps;\n"
            "      v_point_size = total_ps;\n"
            "      v_core_size  = ps;\n"
            "    }\n"
            "  }\n",
            False
        )

        # ── Fragment declarations ────────────────────────────────────────────
        sp.AddFragmentShaderReplacement(
            "//VTK::Color::Dec", True,
            "//VTK::Color::Dec\n"
            "in float v_point_size;\n"
            "in float v_core_size;\n"
            "in float v_boundary;\n"
            "float world_to_ndc_bias(float world_bias) {\n"
            "  float range = naksha_far - naksha_near;\n"
            "  if (range < 0.001) return 0.0;\n"
            "  return world_bias / range;\n"
            "}\n",
            False
        )

        # ── Fragment implementation — each mode uses its own depth bias ──────
        #
        #   All points have a grown sprite; the outer annulus is painted black
        #   the ring fragment and the core fragment come from the same sprite —
        #   they are already at almost identical depth.
        #
        #   Only boundary points are grown.  Their black shell must reliably
        #   occlude the smaller un-grown interior points behind them.
        #
        # PER-POINT   (else)
        #   Every point grows uniformly so the ring sits flush with its own
        #   core fragment.  Bias = _BORDER_DEPTH_BIAS_PERPOINT (medium).
        sp.AddFragmentShaderReplacement(
            "//VTK::Color::Impl", True,
            "//VTK::Color::Impl\n"
            "vec2 uv25 = gl_PointCoord.xy - vec2(0.5);\n"
            "float radial_sq = dot(uv25, uv25);\n"
            "if (radial_sq > 0.25) discard;\n"
            "\n"
            "if (border_ring_val > 0.001) {\n"
            "  float sprite_dist_sq = radial_sq * v_point_size * v_point_size;\n"
            "  float core_radius_sq = 0.25 * v_core_size * v_core_size;\n"
            "  bool is_border_fragment = sprite_dist_sq >= core_radius_sq;\n"
            "  if (structured_border_mode > 1.5) {\n"
            "    // ── HYBRID: ring on every point, tiny depth nudge ──────────\n"
            "    if (is_border_fragment) {\n"
            "      diffuseColor = vec3(0.0);\n"
            "      ambientColor = vec3(0.0);\n"
            f"      gl_FragDepth = clamp(gl_FragCoord.z + world_to_ndc_bias({_BORDER_DEPTH_BIAS_HYBRID_WORLD}), 0.0, 1.0);\n"
            "    } else {\n"
            "      gl_FragDepth = gl_FragCoord.z;\n"
            "    }\n"
            "  } else if (structured_border_mode > 0.5) {\n"
            "    // ── STRUCTURED: ring only on boundary points, larger nudge ─\n"
            "    if (v_boundary > 0.5) {\n"
            "      if (is_border_fragment) {\n"
            "        diffuseColor = vec3(0.0);\n"
            "        ambientColor = vec3(0.0);\n"
            f"        gl_FragDepth = clamp(gl_FragCoord.z + world_to_ndc_bias({_BORDER_DEPTH_BIAS_STRUCTURED_WORLD}), 0.0, 1.0);\n"
            "      } else {\n"
            "        gl_FragDepth = gl_FragCoord.z;\n"
            "      }\n"
            "    } else {\n"
            "      gl_FragDepth = gl_FragCoord.z;\n"
            "    }\n"
            "  } else {\n"
            "    // ── PER-POINT: ring on every point, medium depth nudge ─────\n"
            "    if (is_border_fragment) {\n"
            "      diffuseColor = vec3(0.0);\n"
            "      ambientColor = vec3(0.0);\n"
            f"      gl_FragDepth = clamp(gl_FragCoord.z + world_to_ndc_bias({_BORDER_DEPTH_BIAS_PERPOINT_WORLD}), 0.0, 1.0);\n"
            "    } else {\n"
            "      gl_FragDepth = gl_FragCoord.z;\n"
            "    }\n"
            "  }\n"
            "} else {\n"
            "  gl_FragDepth = gl_FragCoord.z;\n"
            "}\n"
            "opacity = 1.0;\n",
            False
        )

        actor._shaders_finalized_v25 = True
        actor._shaders_finalized_v26 = True
        actor._shaders_finalized_v27 = True
        actor._shaders_finalized_v28 = True
        actor._shaders_finalized_v29 = True
        print(f"      ✅ GPU Shader v29 (squared-radius point sprites): {actor_name}")

    actor._naksha_needs_program_point_size = True
    actor.GetProperty().Modified()
    _push_uniforms_direct(actor, ctx)

def _refresh_actor_boundary_flags(actor, reason="structured-border"):
    """
    Structured border depends on BoundaryFlag.
    Recompute it from the actor's current Classification array after AI / fast refresh.
    """
    try:
        if actor is None:
            return False

        mesh = getattr(actor, "_naksha_mesh", None)
        if mesh is None or mesh.GetPoints() is None:
            return False

        point_data = mesh.GetPointData()
        if point_data is None:
            return False

        cls_arr = point_data.GetArray("Classification")
        if cls_arr is None:
            return False

        pts_vtk = mesh.GetPoints().GetData()
        xyz = numpy_support.vtk_to_numpy(pts_vtk)
        cls = numpy_support.vtk_to_numpy(cls_arr).astype(np.int32, copy=False)

        if len(xyz) == 0 or len(xyz) != len(cls):
            return False

        cls_mtime = int(cls_arr.GetMTime())
        cached_mtime = getattr(actor, "_naksha_boundary_class_mtime", None)
        cached_count = getattr(actor, "_naksha_boundary_point_count", None)

        if cached_count == len(cls):
            # Sampled checksum — detects real class-value changes without being
            # invalidated by VTK MTime bumps from palette/border/display operations.
            # Sampling 1-in-2000 of 13M pts ≈ 6500 elements, ~0.1 ms, zero false
            # negatives for any brush/polygon/line classification stroke.
            _step = max(1, len(cls) // 2000)
            _checksum = int(cls[::_step].sum())
            if getattr(actor, "_naksha_boundary_checksum", None) == _checksum:
                actor._naksha_boundary_class_mtime = cls_mtime  # keep MTime in sync
                return True

        # Cache miss: classification data changed. Run the expensive recompute in a
        # background thread so this call returns immediately with the current
        # (stale-but-valid) BoundaryFlag data. The background thread patches the
        # VTK array and fires a re-render when done.
        if getattr(actor, "_naksha_bf_building", False):
            # Background compute already in flight — use stale flags for this render.
            return True

        actor._naksha_bf_building = True
        _step          = max(1, len(cls) // 2000)
        _new_checksum  = int(cls[::_step].sum())
        # xyz: copy so the background thread is safe if the user loads a new file
        # cls: .astype(np.int32) above already produced a fresh array — no extra copy
        xyz_bg     = xyz.copy()
        cls_bg     = cls
        _actor_id  = id(actor)
        _mesh_ref  = mesh
        _reason    = reason
        _cls_mtime = cls_mtime

        def _bg_compute():
            try:
                bf = _compute_boundary_flags(xyz_bg, cls_bg).astype(np.float32, copy=False)

                def _main_apply():
                    try:
                        # Guard: actor or mesh replaced (e.g. file reload during compute)
                        if id(actor) != _actor_id or getattr(actor, "_naksha_mesh", None) is not _mesh_ref:
                            actor._naksha_bf_building = False
                            return
                        pd = _mesh_ref.GetPointData()
                        ba = pd.GetArray("BoundaryFlag")
                        if ba is not None and ba.GetNumberOfTuples() == len(bf):
                            np.copyto(numpy_support.vtk_to_numpy(ba), bf)
                            ba.Modified()
                        else:
                            if ba is not None:
                                pd.RemoveArray("BoundaryFlag")
                            ba = numpy_support.numpy_to_vtk(bf, deep=False)
                            ba.SetName("BoundaryFlag")
                            pd.AddArray(ba)
                        actor._naksha_bf_np_ref       = bf
                        actor._naksha_boundary_vtk    = ba
                        ca = pd.GetArray("Classification")
                        actor._naksha_boundary_class_mtime  = int(ca.GetMTime()) if ca else _cls_mtime
                        actor._naksha_boundary_point_count  = len(bf)
                        actor._naksha_boundary_checksum     = _new_checksum
                        pd.Modified()
                        _mesh_ref.Modified()
                        m = actor.GetMapper()
                        if m:
                            m.Modified()
                        actor.Modified()
                        actor._naksha_bf_building = False
                        print(
                            f"      🔲 BoundaryFlag updated ({_reason}): "
                            f"{int(bf.sum()):,}/{len(bf):,}"
                        )
                        rw = getattr(actor, "_naksha_render_window", None)
                        if rw:
                            rw.Render()
                    except Exception as _ae:
                        actor._naksha_bf_building = False
                        print(f"      ⚠️ BoundaryFlag background apply failed: {_ae}")

                from PySide6.QtCore import QTimer
                QTimer.singleShot(0, _main_apply)
            except Exception as _be:
                actor._naksha_bf_building = False
                print(f"      ⚠️ BoundaryFlag background compute failed: {_be}")

        import threading as _threading
        _t = _threading.Thread(target=_bg_compute, daemon=True, name="BoundaryFlagBG")
        _t.start()
        actor._naksha_bf_thread = _t  # keep reference; daemon thread, won't block exit

        print(f"      ⏳ BoundaryFlag recomputing in background ({len(cls):,} pts)...")
        return True

    except Exception as e:
        print(f"      ⚠️ BoundaryFlag refresh failed ({reason}): {e}")
        return False

# ─────────────────────────────────────────────────────────────────────────────
# sync_palette_to_gpu — called by Display Mode dialog Apply
# ─────────────────────────────────────────────────────────────────────────────
def sync_palette_to_gpu(app, slot_idx: int = 0, palette: Optional[dict] = None,
                        border: Optional[float] = None, render: bool = True,
                        rewrite_rgb: Optional[bool] = None, **kwargs):
    t0 = time.perf_counter()

    border_explicitly_provided = (border is not None)
    if border is None:
        kw_border = kwargs.get('border_percent', None)
        if kw_border is not None:
            border = float(kw_border)
            border_explicitly_provided = True
        else:
            border = 0.0

    if not border_explicitly_provided and float(border) <= 0.0 and slot_idx == 0:
        _ua = getattr(app, '_unified_actor', None)
        _uc = getattr(_ua, '_naksha_shader_ctx', None) if _ua else None
        if _uc is not None and float(_uc.border_ring) > 0.0:
            border = float(_uc.border_ring) * 100.0
        elif float(getattr(app, 'point_border_percent', 0) or 0.0) > 0.0:
            border = float(app.point_border_percent)

    if slot_idx == 0:
        actor_name = UNIFIED_ACTOR_NAME
        vtk_widget = getattr(app, "vtk_widget", None)
    elif 1 <= slot_idx <= 4:
        view_idx   = slot_idx - 1
        actor_name = f"_section_{view_idx}_unified"
        vtk_widget = app.section_vtks.get(view_idx) if hasattr(app, "section_vtks") else None
    elif slot_idx == 5:
        actor_name = "_cut_section_unified"
        ctrl       = getattr(app, "cut_section_controller", None)
        vtk_widget = getattr(ctrl, "cut_vtk", None) if ctrl else None
    else:
        return False

    if vtk_widget is None:
        return False

    palette = palette or _get_slot_palette(app, slot_idx)

    if 1 <= slot_idx <= 4:
        if not hasattr(app, 'view_borders'):
            app.view_borders = {}
        if slot_idx in app.view_borders:
            border = float(app.view_borders[slot_idx])
        elif border_explicitly_provided:
            app.view_borders[slot_idx] = float(border)
        if section_requires_legacy_border_render(app, view_idx, palette, float(border)):
            if hasattr(app, '_refresh_single_section_view'):
                app._refresh_single_section_view(view_idx, float(border))
                return True

    actor = vtk_widget.actors.get(actor_name)
    if actor is None:
        if slot_idx == 0:
            actor = _get_unified_actor(app)

        if 1 <= slot_idx <= 4 and hasattr(app, '_refresh_single_section_view'):
            app._refresh_single_section_view(view_idx, float(border))
            return True

        if actor is None:
            return False

    # Cut actors deliberately use VTK's standard point shader.  Reattaching
    # the unified shader here would revive the driver-specific compile loop
    # that blanks the cut view.  Its CPU-backed RGB/classification arrays are
    # refreshed by the controller instead.
    if slot_idx == 5 and getattr(actor, "_naksha_disable_custom_shader", False):
        ctrl = getattr(app, "cut_section_controller", None)
        if ctrl is not None and hasattr(ctrl, "_refresh_cut_colors_fast"):
            ctrl._refresh_cut_colors_fast()
        elif render:
            try:
                vtk_widget.render()
            except Exception:
                pass
        return True

    ctx = getattr(actor, '_naksha_shader_ctx', None)
    if ctx is None:
        ctx = ViewShaderContext(slot_idx)
        _shader_contexts[actor_name] = ctx
        _attach_view_shader_context(actor, ctx, actor_name)

    _set_context_border_logic_mode(app, slot_idx, ctx)

    base_point_size = float(getattr(actor, '_naksha_base_point_size', _BASE_POINT_SIZE))

    ctx.force_reload()
    ctx.load_from_palette(palette, float(border), base_point_size)

    if 0.5 < float(getattr(ctx, "structured_border_mode", 0.0) or 0.0) < 1.5:
        _refresh_actor_boundary_flags(actor, f"slot {slot_idx} structured sync")

    _push_uniforms_direct(actor, ctx)


    if slot_idx == 0:
        if "_naksha_unified_border" in (getattr(app, "vtk_widget", None) or {}).actors:
            app.vtk_widget.remove_actor("_naksha_unified_border", render=False)

    if slot_idx == 0 and float(border) > 0.0:
        app.point_border_percent = float(border)
    elif 1 <= slot_idx <= 5:
        if not hasattr(app, 'view_borders'):
            app.view_borders = {}
        app.view_borders[slot_idx] = float(border)

    if rewrite_rgb is None:
        if slot_idx == 0:
            # Keep main-view mode colors stable in non-class modes.
            # Slot-0 palette sync should still update shader LUT uniforms (visibility/weights/border),
            # but must not overwrite the current RGB buffer used by intensity/depth/rgb/elevation.
            current_mode = str(getattr(app, "display_mode", "class") or "class").lower()
            rewrite_rgb = current_mode in ("class", "shaded_class")
        else:
            rewrite_rgb = True

    rgb_ptr = getattr(actor, '_naksha_rgb_ptr', None)
    if bool(rewrite_rgb) and rgb_ptr is not None and _is_writable(rgb_ptr):
        sc = getattr(actor, '_naksha_section_class', None)
        if sc is not None:
            _rewrite_rgb_from_palette(rgb_ptr, sc, palette)
        else:
            gi             = getattr(app, '_main_global_indices', None)
            data_obj       = getattr(app, 'data', None)
            classification = data_obj.get("classification") if isinstance(data_obj, dict) else None
            if classification is not None:
                vis_class = classification[gi] if gi is not None else classification
                _rewrite_rgb_from_palette(rgb_ptr, vis_class, palette)

                # Keep the GPU's per-vertex "Classification" ID buffer in sync with
                # ground truth too, not just RGB. Points reclassified while working
                # in a Cross-Section view only get an incremental color+ID patch on
                # the points touched at that moment (fast_classify_update); this
                # full resync runs ONLY here — on a display-mode preset apply
                # (T/A shortcuts, dialog Apply, Ctrl+Shift+D) — which happens a
                # handful of times per session, never during live classification,
                # so it adds no cost to the classify hot path.
                try:
                    mesh = getattr(actor, '_naksha_mesh', None)
                    if mesh is not None:
                        class_vtk_arr = mesh.GetPointData().GetArray("Classification")
                        if class_vtk_arr is not None:
                            cls_np = numpy_support.vtk_to_numpy(class_vtk_arr)
                            if len(cls_np) == len(vis_class):
                                cls_np[:] = vis_class.astype(cls_np.dtype, copy=False)
                                class_vtk_arr.Modified()
                                mesh.Modified()
                except Exception:
                    pass

        vtk_ca = getattr(actor, '_naksha_vtk_array', None)
        if vtk_ca:
            vtk_ca.Modified()
        _mark_actor_dirty(actor)

    if slot_idx == 0:
        _dlg = getattr(app, 'display_mode_dialog', None)
        if _dlg is not None and hasattr(_dlg, 'view_palettes') and palette:
            try:
                import copy as _copy
                _dlg.view_palettes[0] = _copy.deepcopy(palette)
            except Exception:
                pass

    if render:
        try:
            vtk_widget.render()
        except Exception:
            pass

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"   ⚡ GPU Sync (Slot {slot_idx}): {elapsed:.1f} ms")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# connect_palette_signal
# ─────────────────────────────────────────────────────────────────────────────
def connect_palette_signal(app) -> bool:
    dialog = (getattr(app, 'display_mode_dialog', None)
              or getattr(app, 'display_dialog', None))
    if dialog is None:
        print("   ⚠️ connect_palette_signal: no dialog found")
        return False
    if not hasattr(dialog, 'palette_changed'):
        print("   ⚠️ connect_palette_signal: no palette_changed signal")
        return False

    def _on_palette_changed(slot_idx: int):
        palette = _get_slot_palette(app, slot_idx)
        if slot_idx == 0:
            border = float(getattr(app, 'point_border_percent', 0) or 0.0)
            if border <= 0.0:
                _ua = getattr(app, '_unified_actor', None)
                _uc = getattr(_ua, '_naksha_shader_ctx', None) if _ua else None
                if _uc is not None and float(_uc.border_ring) > 0.0:
                    border = float(_uc.border_ring) * 100.0
        else:
            dlg    = (getattr(app, 'display_mode_dialog', None)
                      or getattr(app, 'display_dialog', None))
            border = float(dlg.view_borders.get(slot_idx, 0)) if dlg else 0.0
        sync_palette_to_gpu(app, slot_idx, palette, border, render=True)

    try:
        dialog.palette_changed.disconnect(_on_palette_changed)
    except (TypeError, RuntimeError):
        pass
    dialog.palette_changed.connect(_on_palette_changed)
    print("   ✅ connect_palette_signal: wired")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# refresh_section_after_weight_change
# ─────────────────────────────────────────────────────────────────────────────
def refresh_section_after_weight_change(
    app,
    view_idx: int,
    palette: Optional[dict] = None,
    border_percent: float = 0.0,
) -> bool:
    slot_idx = view_idx + 1
    palette  = palette or _get_slot_palette(app, slot_idx)

    if hasattr(app, 'view_borders') and slot_idx in app.view_borders:
        border_percent = float(app.view_borders[slot_idx])

    if not hasattr(app, 'section_vtks') or view_idx not in app.section_vtks:
        return False
    vtk_widget = app.section_vtks[view_idx]
    if vtk_widget is None:
        return False

    if section_requires_legacy_border_render(app, view_idx, palette, float(border_percent)):
        if hasattr(app, '_refresh_single_section_view'):
            app._refresh_single_section_view(view_idx, float(border_percent))
            return True
        return False

    actor_name = f"_section_{view_idx}_unified"
    actor = (vtk_widget.actors.get(actor_name)
             if hasattr(vtk_widget, 'actors') else None)

    if actor is None:
        if hasattr(app, '_refresh_single_section_view'):
            app._refresh_single_section_view(view_idx, float(border_percent))
            return True
        return False

    rgb_ptr = getattr(actor, '_naksha_rgb_ptr', None)
    if rgb_ptr is None or not _is_writable(rgb_ptr):
        return False

    base_point_size = float(getattr(actor, '_naksha_base_point_size', _BASE_POINT_SIZE))

    ctx = getattr(actor, '_naksha_shader_ctx', None)
    if ctx is not None:
        ctx.force_reload()
        ctx.load_from_palette(palette, border_percent, base_point_size)
    else:
        print(f"   ⚠️ Section {view_idx+1}: no shader context — attaching fresh")
        ctx = ViewShaderContext(slot_idx=slot_idx)
        ctx.load_from_palette(palette, border_percent, base_point_size)
        _attach_view_shader_context(actor, ctx, actor_name)

    _set_context_border_logic_mode(app, slot_idx, ctx)

    sc = getattr(actor, '_naksha_section_class', None)
    if sc is not None:
        _rewrite_rgb_from_palette(rgb_ptr, sc, palette)
        vtk_ca = getattr(actor, '_naksha_vtk_array', None)
        if vtk_ca:
            vtk_ca.Modified()
        _mark_actor_dirty(actor)

    _push_uniforms_direct(actor, ctx)

    try:
        vtk_widget.render()
    except Exception:
        pass

    print(f"   ✅ refresh_section_after_weight_change: view={view_idx+1} "
          f"(slot={slot_idx}, border={border_percent}%, base_size={base_point_size})")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# fast_palette_refresh — main view weight/visibility/color update
# ─────────────────────────────────────────────────────────────────────────────
def fast_palette_refresh(
    app,
    palette: Optional[dict] = None,
    border_percent: float = 0.0,
) -> bool:
    t0 = time.perf_counter()

    actor = _get_unified_actor(app)
    if actor is None:
        return False

    rgb_ptr = getattr(actor, "_naksha_rgb_ptr", None)
    vtk_ca  = getattr(actor, "_naksha_vtk_array", None)
    if rgb_ptr is None or vtk_ca is None or not _is_writable(rgb_ptr):
        return False

    palette         = palette or getattr(app, "class_palette", {})
    classification  = app.data["classification"]
    base_point_size = float(getattr(actor, '_naksha_base_point_size', _BASE_POINT_SIZE))

    ctx = getattr(actor, '_naksha_shader_ctx', None)

    if border_percent <= 0.0:
        if ctx is not None and float(ctx.border_ring) > 0.0:
            border_percent = float(ctx.border_ring) * 100.0
        else:
            border_percent = float(getattr(app, 'point_border_percent', 0) or 0.0)

    if ctx is not None:
        ctx.force_reload()
        ctx.load_from_palette(palette, border_percent, base_point_size)
    else:
        print(f"   ⚠️ Main view: shader context missing — recovering")
        ctx = ViewShaderContext(slot_idx=0)
        ctx.load_from_palette(palette, border_percent, base_point_size)
        raw_mapper = actor.GetMapper()
        if hasattr(raw_mapper, 'GetMapper'):
            raw_mapper = raw_mapper.GetMapper()
        ctx._has_vertex_attr_cache = hasattr(raw_mapper, 'MapDataArrayToVertexAttribute')
        actor._naksha_shader_ctx   = ctx
        _shader_contexts[UNIFIED_ACTOR_NAME] = ctx

    _set_context_border_logic_mode(app, 0, ctx)

    gi        = getattr(app, '_main_global_indices', None)
    vis_class = classification[gi] if gi is not None else classification
    _rewrite_rgb_from_palette(rgb_ptr, vis_class, palette)

    # Sync Classification array on the mesh
    mesh = getattr(actor, '_naksha_mesh', None)
    if mesh is not None:
        class_vtk_arr = mesh.GetPointData().GetArray("Classification")
        if class_vtk_arr is not None:
            cls_np = numpy_support.vtk_to_numpy(class_vtk_arr)
            np.copyto(cls_np, vis_class.astype(np.float32, copy=False))
            class_vtk_arr.Modified()
            mesh.Modified()

    if 0.5 < float(getattr(ctx, "structured_border_mode", 0.0) or 0.0) < 1.5:
        _refresh_actor_boundary_flags(actor, "main structured fast refresh")

    vtk_ca.Modified()


    _mark_actor_dirty(actor)

    _apply_border_once(actor, border_percent)
    _push_uniforms_direct(actor, ctx)

    try:
        app.vtk_widget.render()
    except Exception:
        pass

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"   ⚡ fast_palette_refresh: {len(vis_class):,} pts "
          f"[{elapsed:.1f} ms] base_size={base_point_size}")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# COLOR LUT
# ─────────────────────────────────────────────────────────────────────────────
class ColorLUT:
    def __init__(self):
        self._lut: Optional[np.ndarray] = None
        self._palette_id: Optional[int] = None
        self._max_class: int = 0
        self._hidden_color = np.array([0, 0, 0], dtype=np.uint8)

    def _palette_fingerprint(self, palette: dict) -> int:
        try:
            return hash(tuple(
                (k, v.get("show", True), tuple(v.get("color", (128, 128, 128))))
                for k, v in sorted(palette.items())
            ))
        except Exception:
            return id(palette)

    def build(self, palette: dict, max_class_hint: int = 256) -> np.ndarray:
        fp = self._palette_fingerprint(palette)
        if fp == self._palette_id and self._lut is not None:
            return self._lut
        max_c = max(max_class_hint, max(palette.keys(), default=0) + 1)
        lut = np.full((max_c, 3), 128, dtype=np.uint8)
        for code, info in palette.items():
            if code < max_c:
                lut[code] = (info.get("color", (128, 128, 128))
                             if info.get("show", True) else self._hidden_color)
        self._lut        = lut
        self._palette_id = fp
        self._max_class  = max_c
        return lut

    @property
    def lut(self) -> Optional[np.ndarray]:
        return self._lut

    def load_from_palette(self, palette: dict) -> None:
        self.build(palette, max(max(palette.keys(), default=0) + 1, 256))

    def map_classes(self, classification: np.ndarray, palette: dict) -> np.ndarray:
        lut = self.build(palette, int(classification.max()) + 1)
        return lut[classification.clip(0, len(lut) - 1).astype(np.intp)]

    def map_subset(self, classification: np.ndarray, indices: np.ndarray,
                   palette: dict) -> np.ndarray:
        lut = self.build(palette, int(classification.max()) + 1)
        return lut[classification[indices].clip(0, len(lut) - 1).astype(np.intp)]


_lut_cache: Dict[str, ColorLUT] = {}


def _get_lut(view_key: str = "main") -> ColorLUT:
    if view_key not in _lut_cache:
        _lut_cache[view_key] = ColorLUT()
    return _lut_cache[view_key]


# ─────────────────────────────────────────────────────────────────────────────
# SNT OVERLAY Z-OFFSET
# ─────────────────────────────────────────────────────────────────────────────
def _restore_snt_overlays(app):
    if hasattr(app, 'snt_dialog') and app.snt_dialog is not None:
        try:
            app.snt_dialog.restore_snt_actors()
            return
        except Exception as e:
            print(f"  ⚠️ SNT dialog restore: {e}")

    try:
        from gui.snt_attachment import (
            _get_snt_z_offset, _apply_z_offset_to_actor,
            _snt_enable_gl_point_size, _snt_push_border_uniforms,
        )
    except ImportError:
        return

    z_offset = _get_snt_z_offset(app)
    if z_offset <= 0:
        return

    try:
        renderer = app.vtk_widget.renderer
    except Exception:
        return

    count = 0
    prj_dlg = getattr(app, "block_identifier_dialog", None)
    for store_name in ['snt_actors']:
        att_name = store_name.replace("_actors", "_attachments")
        attachments = getattr(app, att_name, [])
        for entry in getattr(app, store_name, []):
            target = os.path.basename(entry.get("filename", ""))
            att = next((a for a in attachments if os.path.basename(a.get("filename", "")) == target), None)

            actor_layer_map = {}
            selected_layers = None
            if att:
                selected_layers = att.get("selected_layers")
                cache_map = att.get("actor_cache_map", {})
                for layer_name, actors in cache_map.items():
                    for a in actors:
                        actor_layer_map[id(a)] = layer_name

            for actor in entry.get('actors', []):
                try:
                    _apply_z_offset_to_actor(actor, z_offset)
                    renderer.AddActor(actor)
                    actor._naksha_renderer = renderer

                    keep_hidden = (
                        prj_dlg is not None
                        and hasattr(prj_dlg, "should_keep_actor_hidden")
                        and prj_dlg.should_keep_actor_hidden(actor)
                    )
                    if keep_hidden:
                        actor.SetVisibility(0)
                    elif selected_layers is not None and id(actor) in actor_layer_map:
                        layer_name = actor_layer_map[id(actor)]
                        actor.SetVisibility(1 if layer_name in selected_layers else 0)

                    count += 1
                except Exception:
                    pass

    if count > 0:
        renderer.ResetCameraClippingRange()
        _snt_enable_gl_point_size(app)
        _snt_push_border_uniforms(app)
        print(f"  🔄 SNT overlays: {count} actors restored (z_offset={z_offset:.1f})")


# ─────────────────────────────────────────────────────────────────────────────
# BUILD UNIFIED ACTOR  (main view)
# ─────────────────────────────────────────────────────────────────────────────
def build_unified_actor(
    app,
    palette: Optional[dict] = None,
    border_percent: float = 0.0,
    point_size: float = _BASE_POINT_SIZE,
) -> Optional[object]:
    t0 = time.perf_counter()

    app._unified_actor_building = True   # guard: block interactions during build
    # Cancel any pending deferred init that references a previous actor
    _pending = getattr(app, '_naksha_deferred_timer', None)
    if _pending is not None:
        try:
            _pending.stop()
        except Exception:
            pass
        app._naksha_deferred_timer = None

    plotter = getattr(app, "vtk_widget", None)
    if plotter is None:
        return None
    data = getattr(app, "data", None)
    if data is None or "xyz" not in data:
        return None
    xyz            = data["xyz"]
    classification = data.get("classification")
    if classification is None:
        return None

    palette = palette or getattr(app, "class_palette", {})

    if UNIFIED_ACTOR_NAME in plotter.actors:
        plotter.remove_actor(UNIFIED_ACTOR_NAME, render=False)
    if MAIN_INTERACTION_ACTOR_NAME in plotter.actors:
        plotter.remove_actor(MAIN_INTERACTION_ACTOR_NAME, render=False)
    app._main_interaction_actor = None
    for name in list(plotter.actors.keys()):
        if str(name).startswith("class_"):
            plotter.remove_actor(name, render=False)

    N_total = len(xyz)
    from gui.flight_line_filter import flight_line_visibility_mask
    _flight_mask = flight_line_visibility_mask(app, N_total)
    try:
        from gui.optimization_config import MAIN_VIEW_RENDER_ALL_POINTS
    except Exception:
        MAIN_VIEW_RENDER_ALL_POINTS = True

    if MAIN_VIEW_RENDER_ALL_POINTS:
        # None is the established identity-map convention in refresh code.
        # It avoids a 32M-entry arange and advanced-index copies of full XYZ.
        step = 1
        global_indices = None
        vis_xyz = xyz
        vis_class = classification
    else:
        target_points = 10_000_000
        if N_total > target_points:
            step = max(1, N_total // target_points)
            global_indices = np.arange(0, N_total, step)
        else:
            step = 1
            global_indices = None
        vis_xyz = xyz if global_indices is None else xyz[global_indices]
        vis_class = classification if global_indices is None else classification[global_indices]

    app._main_global_indices = global_indices
    # Store LOD step for O(M) brush index mapping (avoids intersect1d on 10M array).
    # gi = arange(0, N, step), so global_idx is in LOD iff global_idx % step == 0
    # and its local index is global_idx // step.
    app._main_lod_step = step

    # "All off" is a valid MicroStation-style state. Keep overlays/camera but
    # remove the point-cloud actor instead of running min/max on empty arrays.
    if len(vis_xyz) == 0:
        app._main_global_indices = np.empty(0, dtype=np.int64)
        app._rgb_buffer = np.empty((0, 3), dtype=np.uint8)
        app._unified_actor = None
        app._unified_actor_building = False
        try:
            plotter.render()
        except Exception:
            pass
        _restore_snt_overlays(app)
        print("      ℹ️ All flight lines are off — main point cloud hidden")
        return None

    cloud     = pv.PolyData(vis_xyz)
    print(
        "      COORD_TRACE PRE-VTK/POLYDATA: "
        f"X=[{vis_xyz[:, 0].min():.3f}, {vis_xyz[:, 0].max():.3f}] "
        f"Y=[{vis_xyz[:, 1].min():.3f}, {vis_xyz[:, 1].max():.3f}] "
        f"polydata={tuple(float(v) for v in cloud.GetBounds())}"
    )
    # Keep main classification array independent of temporary NumPy views.
    class_vtk = numpy_support.numpy_to_vtk(vis_class.astype(np.float32, copy=False),
                                            deep=True)
    class_vtk.SetName("Classification")
    cloud.GetPointData().AddArray(class_vtk)

    flight_values = np.asarray(
        _flight_mask if global_indices is None else _flight_mask[global_indices],
        dtype=np.uint8,
    )
    flight_vtk = numpy_support.numpy_to_vtk(flight_values, deep=False)
    flight_vtk.SetName("FlightVisible")
    cloud.GetPointData().AddArray(flight_vtk)

    _bf = _compute_boundary_flags(vis_xyz, vis_class)
    _bf_vtk = numpy_support.numpy_to_vtk(_bf, deep=False)
    _bf_vtk.SetName("BoundaryFlag")
    cloud.GetPointData().AddArray(_bf_vtk)
    print(f"      🔲 BoundaryFlag: {int(_bf.sum()):,}/{len(_bf):,} edge pts")

    vis_count = len(vis_xyz)
    rgb_buf = getattr(app, "_rgb_buffer", None)
    if (
        not isinstance(rgb_buf, np.ndarray)
        or rgb_buf.dtype != np.uint8
        or rgb_buf.ndim != 2
        or rgb_buf.shape[0] != vis_count
        or rgb_buf.shape[1] != 3
    ):
        app._rgb_buffer = np.zeros((vis_count, 3), dtype=np.uint8)
    elif not rgb_buf.flags.c_contiguous:
        app._rgb_buffer = np.ascontiguousarray(rgb_buf, dtype=np.uint8)

    rgb_vtk = numpy_support.numpy_to_vtk(app._rgb_buffer, deep=False)
    rgb_vtk.SetName("RGB")
    cloud.GetPointData().SetScalars(rgb_vtk)

    lut = _get_lut("main")
    np.copyto(app._rgb_buffer, lut.map_classes(vis_class, palette))

    n_pts             = len(xyz)
    actual_point_size = _BASE_POINT_SIZE

    actor = plotter.add_points(
        cloud, scalars="RGB", rgb=True,
        point_size=actual_point_size,
        render_points_as_spheres=False,
        name=UNIFIED_ACTOR_NAME,
        reset_camera=False, render=False,
    )
    if actor:
        actor.GetProperty().LightingOff()
        # A main cloud is always supplied in world coordinates.  Keep the
        # actor transform explicitly identity so a future actor-reuse change
        # cannot silently apply a second coordinate system.
        actor.SetPosition(0.0, 0.0, 0.0)
        actor.SetOrigin(0.0, 0.0, 0.0)
        actor.SetScale(1.0, 1.0, 1.0)
        actor.SetUserTransform(None)

    if actor:
        try:
            actor._naksha_render_window = plotter.render_window
            actor._naksha_renderer = plotter.renderer
        except Exception:
            pass

    if actor:
        try:
            rw = plotter.render_window
            if _try_enable_program_point_size(rw):
                _install_program_point_size_observer(actor, rw)
                actor._naksha_needs_program_point_size = False
                print("      ✅ GL_PROGRAM_POINT_SIZE enabled immediately (warm context)")
        except Exception:
            pass

        _ensure_opengl_polydata_mapper(actor, cloud)
        _bm = actor.GetMapper()
        mesh   = _bm.GetInput() if _bm is not None else None
        if mesh is None:
            raise RuntimeError("build_unified_actor: mapper has no input after _ensure_opengl_polydata_mapper")
        print(
            "      COORD_TRACE MAPPER/ACTOR: "
            f"mapper={tuple(float(v) for v in mesh.GetBounds())} "
            f"actor={tuple(float(v) for v in actor.GetBounds())} "
            f"position={tuple(float(v) for v in actor.GetPosition())} "
            f"origin={tuple(float(v) for v in actor.GetOrigin())}"
        )
        vtk_ca = mesh.GetPointData().GetScalars()
        if vtk_ca is None:
            raise RuntimeError("build_unified_actor: mesh scalars missing")

        _vtk_rgb = numpy_support.vtk_to_numpy(vtk_ca)
        np.copyto(_vtk_rgb, app._rgb_buffer)
        vtk_ca.Modified()

        _cls_np_f32 = vis_class.astype(np.float32)
        _cls_vtk = numpy_support.numpy_to_vtk(_cls_np_f32, deep=False)
        _cls_vtk.SetName("Classification")
        mesh.GetPointData().AddArray(_cls_vtk)

        actor._naksha_rgb_ptr         = _vtk_rgb
        actor._naksha_point_count     = len(vis_xyz)
        actor._naksha_vtk_array       = vtk_ca
        actor._naksha_vtk_rgb_ref     = vtk_ca
        actor._naksha_cls_np_ref      = _cls_np_f32
        actor._naksha_bf_np_ref       = _bf
        actor._naksha_mesh            = mesh
        actor._naksha_base_point_size = actual_point_size
        actor._naksha_boundary_vtk    = _bf_vtk
        actor._naksha_flight_np_ref   = flight_values
        actor._naksha_flight_vtk      = mesh.GetPointData().GetArray("FlightVisible")
        actor._naksha_data_id         = id(xyz)
        from gui.flight_line_filter import flight_line_visibility_signature
        actor._naksha_flight_line_signature = flight_line_visibility_signature(app)
        # Seed the boundary-flag cache so the first sync_palette_to_gpu does
        # not recompute boundary flags already computed above.
        # Uses sampled-checksum key (not VTK MTime) so palette/display changes
        # that call class_vtk_arr.Modified() never trigger a false recompute.
        _cls_in_mesh = mesh.GetPointData().GetArray("Classification")
        if _cls_in_mesh is not None:
            actor._naksha_boundary_class_mtime = int(_cls_in_mesh.GetMTime())
            actor._naksha_boundary_point_count = len(vis_class)
            _step = max(1, len(vis_class) // 2000)
            actor._naksha_boundary_checksum = int(
                vis_class.astype(np.int32)[::_step].sum()
            )
        app._unified_actor            = actor

        # ── Precompute an interaction LOD subset (safe, no invariants broken) ──
        # We keep the SAME vtkPolyData and its named arrays (Classification,
        # BoundaryFlag, RGB) and only ever swap a *strided subset* of those
        # arrays in/out during a pan/zoom/rotate gesture. Because the shader
        # reads per-point arrays, a smaller strided subset renders with identical
        # colours/boundaries — just fewer points — so palette sync, program point
        # size, and the custom shader are all unaffected. Only engage for large
        # clouds (cheap clouds rely on the frame-budget cap instead).
        try:
            _lod_target = (
                _MAIN_INTERACTION_POINT_MAX
                if INTERACTION_ACTOR_SWITCHING_ENABLED
                else len(vis_xyz)
            )
            _n_vis = len(vis_xyz)
            if _n_vis > _lod_target:
                _lod_idx = _uniform_pick_indices(_n_vis, _lod_target)
                # These must be the arrays actually owned by the VTK actor.
                # Classification and RGB are patched in place after every
                # classify/undo operation; keeping separate build-time copies
                # makes a later interaction-LOD swap resurrect stale colours.
                actor._naksha_lod_idx    = _lod_idx
                actor._naksha_lod_active = False
                print(f"      🔻 Interaction LOD precomputed: {len(_lod_idx):,} pts "
                      f"(separate actor, full {_n_vis:,})")
            else:
                actor._naksha_lod_idx = None  # LOD disabled for small clouds
        except Exception as _lod_e:
            actor._naksha_lod_idx = None
            print(f"      ⚠️ Interaction LOD precompute skipped: {_lod_e}")

        ctx = ViewShaderContext(slot_idx=0)

        ctx.load_from_palette(palette, border_percent, actual_point_size)

        _set_context_border_logic_mode(app, 0, ctx)

        _attach_view_shader_context(actor, ctx, UNIFIED_ACTOR_NAME)
        try:
            if INTERACTION_ACTOR_SWITCHING_ENABLED and actor._naksha_lod_idx is not None:
                _build_main_interaction_actor(
                    app,
                    plotter,
                    actor,
                    vis_xyz,
                    _cls_np_f32,
                    _bf,
                    _vtk_rgb,
                    actor._naksha_lod_idx,
                    palette,
                    border_percent,
                    actual_point_size,
                )
        except Exception as _interaction_lod_error:
            app._main_interaction_actor = None
            actor._naksha_lod_active = False
            print(f"      Separate interaction actor skipped: {_interaction_lod_error}")
        print(f"      ✅ Main view: {len(app._rgb_buffer):,} pts "
              f"(base_size={actual_point_size}, LOD_step={step}, "
              f"full_resolution={bool(MAIN_VIEW_RENDER_ALL_POINTS)})")

        try:
            from PySide6.QtCore import QTimer
            _a, _c, _p = actor, ctx, plotter
            # Capture the current data id so the deferred callback can
            # abort if a newer file was loaded before the timer fires.
            _expected_data_id = id(app.data.get("xyz"))

            def _safe_deferred_init():
                current_xyz = (app.data.get("xyz")
                               if hasattr(app, "data") and app.data else None)
                if current_xyz is None or id(current_xyz) != _expected_data_id:
                    print("      ⏭️  Deferred GPU init skipped (data replaced)")
                    return
                _deferred_actor_gpu_init(_a, _c, _p, "MainView")

            t = QTimer()
            t.setSingleShot(True)
            t.timeout.connect(_safe_deferred_init)
            # The actor has already been attached. Warm its GL state on the
            # next event-loop turn so the first user pan never pays this cost.
            t.start(0)
            app._naksha_deferred_timer = t
            print("      ⏱️  GPU pan warm-up scheduled before first interaction")
        except Exception as _te:
            print(f"      ⚠️ Could not schedule deferred init: {_te}")

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"   🏗️ Unified actor built: {n_pts:,} pts in {elapsed:.1f} ms")

    app._unified_actor_building = False   # ← clear guard

    _restore_snt_overlays(app)
    return actor


def _build_main_interaction_actor(
    app,
    plotter,
    full_actor,
    full_xyz,
    full_class,
    full_boundary,
    full_rgb,
    lod_indices,
    palette,
    border_percent,
    point_size,
):
    """Build a separate immutable vtkPolyData used only while navigating."""
    lod_indices = np.asarray(lod_indices, dtype=np.int64).ravel()
    if lod_indices.size == 0:
        return None

    arrays = {
        "pts": np.ascontiguousarray(full_xyz[lod_indices]),
        "cls_f32": np.ascontiguousarray(full_class[lod_indices], dtype=np.float32),
        "bf": np.ascontiguousarray(full_boundary[lod_indices], dtype=np.float32),
        "rgb": np.ascontiguousarray(full_rgb[lod_indices], dtype=np.uint8),
    }
    full_flight_vtk = getattr(full_actor, "_naksha_flight_vtk", None)
    if full_flight_vtk is not None:
        full_flight = numpy_support.vtk_to_numpy(full_flight_vtk)
        arrays["flight_visible"] = np.ascontiguousarray(
            full_flight[lod_indices], dtype=np.uint8
        )
    result = _build_vtk_actor_from_arrays(
        plotter,
        MAIN_INTERACTION_ACTOR_NAME,
        arrays,
        point_size,
        palette,
        border_percent,
        0,
        _attach_view_shader_context,
        _ensure_opengl_polydata_mapper,
        ViewShaderContext,
    )
    lod_actor, lod_ctx, mesh, vtk_ca, vtk_rgb, vtk_cls, class_vtk, bf_vtk = result
    if lod_actor is None:
        return None

    _wire_actor_metadata(
        lod_actor,
        mesh,
        vtk_ca,
        vtk_rgb,
        vtk_cls,
        class_vtk,
        bf_vtk,
        None,
        point_size,
    )
    global_indices = getattr(app, "_main_global_indices", None)
    lod_actor._naksha_global_indices = (
        lod_indices
        if global_indices is None
        else np.asarray(global_indices, dtype=np.int64)[lod_indices]
    )
    lod_actor._naksha_lod_source_indices = lod_indices
    lod_actor._naksha_data_id = getattr(full_actor, "_naksha_data_id", None)
    lod_actor._naksha_point_count = int(lod_indices.size)
    # Keep the point source alive even with VTK builds that wrap NumPy memory.
    lod_actor._naksha_points_np_ref = arrays["pts"]
    lod_actor._naksha_flight_vtk = mesh.GetPointData().GetArray("FlightVisible")
    if lod_ctx is not None:
        full_ctx = getattr(full_actor, "_naksha_shader_ctx", None)
        if full_ctx is not None:
            lod_ctx.structured_border_mode = full_ctx.structured_border_mode
        _push_uniforms_direct(lod_actor, lod_ctx)

    lod_actor.SetVisibility(0)
    full_actor._naksha_interaction_actor = lod_actor
    app._main_interaction_actor = lod_actor
    print(
        f"      Separate main interaction actor ready: "
        f"{lod_indices.size:,}/{getattr(full_actor, '_naksha_point_count', 0):,} pts"
    )
    return lod_actor


def _sync_interaction_actor_subset(full_actor, lod_actor) -> bool:
    """Refresh the hidden LOD actor from full buffers before it is displayed."""
    try:
        indices = np.asarray(
            getattr(lod_actor, "_naksha_lod_source_indices", None),
            dtype=np.int64,
        ).ravel()
        if indices.size == 0:
            return False

        full_rgb = getattr(full_actor, "_naksha_rgb_ptr", None)
        lod_rgb = getattr(lod_actor, "_naksha_rgb_ptr", None)
        full_mesh = getattr(full_actor, "_naksha_mesh", None)
        lod_mesh = getattr(lod_actor, "_naksha_mesh", None)
        if full_rgb is None or lod_rgb is None or full_mesh is None or lod_mesh is None:
            return False
        if int(indices[-1]) >= len(full_rgb) or len(lod_rgb) != len(indices):
            return False

        lod_rgb[:] = full_rgb[indices]
        lod_rgb_vtk = getattr(lod_actor, "_naksha_vtk_array", None)
        if lod_rgb_vtk is not None:
            lod_rgb_vtk.Modified()

        full_pd = full_mesh.GetPointData()
        lod_pd = lod_mesh.GetPointData()
        for array_name in ("Classification", "BoundaryFlag", "FlightVisible"):
            source_vtk = full_pd.GetArray(array_name)
            target_vtk = lod_pd.GetArray(array_name)
            if source_vtk is None or target_vtk is None:
                continue
            source = numpy_support.vtk_to_numpy(source_vtk)
            target = numpy_support.vtk_to_numpy(target_vtk)
            if int(indices[-1]) >= len(source) or len(target) != len(indices):
                return False
            target[:] = source[indices].astype(target.dtype, copy=False)
            target_vtk.Modified()

        full_ctx = getattr(full_actor, "_naksha_shader_ctx", None)
        lod_ctx = getattr(lod_actor, "_naksha_shader_ctx", None)
        if full_ctx is not None and lod_ctx is not None:
            lod_ctx.visibility_mask[:] = full_ctx.visibility_mask
            lod_ctx.weight_lut[:] = full_ctx.weight_lut
            lod_ctx.color_lut[:] = full_ctx.color_lut
            lod_ctx.border_ring = full_ctx.border_ring
            lod_ctx.structured_border_mode = full_ctx.structured_border_mode
            lod_ctx._generation += 1
            _push_uniforms_direct(lod_actor, lod_ctx)

        lod_mesh.Modified()
        _mark_actor_dirty(lod_actor)
        return True
    except (RuntimeError, AttributeError, ValueError, IndexError):
        return False
    except Exception:
        return False


def _swap_main_points(actor, xyz, classification, bf, rgb):
    """Replace the unified actor's mesh points + point arrays in place.

    Uses the SAME vtkPolyData and the SAME named arrays (Classification,
    BoundaryFlag, RGB) as the full build — only the point count changes — so
    the custom shader, program point size, and palette uniforms keep working.
    Returns True on success.
    """
    try:
        mapper = actor.GetMapper()
        if mapper is None:
            return False
        mesh = mapper.GetInput()
        if mesh is None:
            return False

        from vtkmodules.util import numpy_support as _ns

        # Validate and construct every wrapper before mutating the live mesh.
        # This keeps a failed swap atomic: VTK never sees points and attributes
        # with different tuple counts.
        xyz = np.ascontiguousarray(xyz)
        classification = np.ascontiguousarray(classification, dtype=np.float32)
        bf = np.ascontiguousarray(bf)
        rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
        point_count = len(xyz)
        if xyz.ndim != 2 or xyz.shape[1] != 3:
            raise ValueError(f"xyz must have shape (N, 3), got {xyz.shape}")
        if classification.ndim != 1 or len(classification) != point_count:
            raise ValueError("Classification length does not match points")
        if bf.ndim != 1 or len(bf) != point_count:
            raise ValueError("BoundaryFlag length does not match points")
        if rgb.ndim != 2 or rgb.shape != (point_count, 3):
            raise ValueError("RGB must have shape (N, 3)")

        points_data = _ns.numpy_to_vtk(xyz, deep=False)
        points_data.SetName("Points")
        vtk_points = vtk.vtkPoints()
        vtk_points.SetData(points_data)

        cls_vtk = _ns.numpy_to_vtk(classification, deep=False)
        cls_vtk.SetName("Classification")
        bf_vtk = _ns.numpy_to_vtk(bf, deep=False)
        bf_vtk.SetName("BoundaryFlag")
        rgb_vtk = _ns.numpy_to_vtk(rgb, deep=False)
        rgb_vtk.SetName("RGB")

        # vtkPolyData.SetPoints requires vtkPoints, not the vtkDataArray
        # returned by numpy_to_vtk.
        mesh.SetPoints(vtk_points)

        pd = mesh.GetPointData()
        # Remove any existing arrays with these names first so repeated
        # in/out swaps never accumulate duplicate-named arrays.
        for _name in ("Classification", "BoundaryFlag", "RGB"):
            _existing = pd.GetArray(_name)
            if _existing is not None:
                pd.RemoveArray(_name)

        pd.AddArray(cls_vtk)
        pd.AddArray(bf_vtk)
        pd.SetScalars(rgb_vtk)

        pd.Modified()
        mesh.Modified()
        mapper.Modified()
        actor.Modified()

        # Rewire every cached pointer used by the fast classification paths.
        # Keeping the old full-detail pointers while the LOD arrays are active
        # would write into detached memory and strand stale colours on screen.
        actor._naksha_mesh = mesh
        actor._naksha_point_count = point_count
        actor._naksha_rgb_ptr = rgb
        actor._naksha_vtk_array = rgb_vtk
        actor._naksha_vtk_rgb_ref = rgb_vtk
        actor._naksha_cls_np_ref = classification
        actor._naksha_class_vtk_ref = cls_vtk
        actor._naksha_bf_np_ref = bf
        actor._naksha_boundary_vtk = bf_vtk
        actor._naksha_points_vtk_ref = vtk_points
        actor._naksha_active_xyz_ref = xyz
        return True
    except Exception as _e:
        print(f"      ⚠️ _swap_main_points failed: {_e}")
        return False


def apply_main_lod(app, factor: float = 0.25):
    """Show the immutable interaction actor without mutating full vtkPolyData."""
    if not INTERACTION_ACTOR_SWITCHING_ENABLED:
        return False
    if app is None or getattr(app, "_shutdown_in_progress", False):
        return False
    actor = getattr(app, "_unified_actor", None)
    lod_actor = getattr(app, "_main_interaction_actor", None)
    if actor is None or lod_actor is None:
        return False
    if getattr(actor, "_naksha_lod_active", False):
        return True

    try:
        full_visibility = int(actor.GetVisibility())
        if not full_visibility:
            # Respect modes that intentionally hide the unified cloud.
            return False
        if getattr(lod_actor, "_naksha_data_id", None) != getattr(actor, "_naksha_data_id", None):
            return False
        if not _sync_interaction_actor_subset(actor, lod_actor):
            return False

        actor._naksha_full_visibility_before_lod = full_visibility
        lod_actor.SetVisibility(full_visibility)
        actor.SetVisibility(0)
        actor._naksha_lod_active = True
        return True
    except (RuntimeError, AttributeError, ReferenceError):
        return False
    except Exception:
        return False


def apply_view_interaction_lod(vtk_widget) -> bool:
    """Switch a section/cut widget to its separate lightweight actor."""
    if not INTERACTION_ACTOR_SWITCHING_ENABLED:
        return False
    if vtk_widget is None or bool(getattr(vtk_widget, "_naksha_view_finalized", False)):
        return False
    full_actor = getattr(vtk_widget, "_naksha_full_detail_actor", None)
    lod_actor = getattr(vtk_widget, "_naksha_interaction_actor", None)
    if full_actor is None or lod_actor is None:
        return False
    if bool(getattr(vtk_widget, "_naksha_interaction_lod_active", False)):
        return True
    try:
        full_visibility = int(full_actor.GetVisibility())
        if not full_visibility:
            return False
        if not _sync_interaction_actor_subset(full_actor, lod_actor):
            return False
        vtk_widget._naksha_full_visibility_before_lod = full_visibility
        lod_actor.SetVisibility(full_visibility)
        full_actor.SetVisibility(0)
        vtk_widget._naksha_interaction_lod_active = True
        return True
    except (RuntimeError, AttributeError, ReferenceError):
        return False
    except Exception:
        return False


def restore_view_full_detail(vtk_widget) -> bool:
    """Restore a section/cut widget without changing either actor's buffers."""
    if vtk_widget is None:
        return False
    if not bool(getattr(vtk_widget, "_naksha_interaction_lod_active", False)):
        return False
    full_actor = getattr(vtk_widget, "_naksha_full_detail_actor", None)
    lod_actor = getattr(vtk_widget, "_naksha_interaction_actor", None)
    if full_actor is None or lod_actor is None:
        vtk_widget._naksha_interaction_lod_active = False
        return False
    try:
        lod_actor.SetVisibility(0)
        full_actor.SetVisibility(
            int(getattr(vtk_widget, "_naksha_full_visibility_before_lod", 1))
        )
        vtk_widget._naksha_interaction_lod_active = False
        return True
    except (RuntimeError, AttributeError, ReferenceError):
        vtk_widget._naksha_interaction_lod_active = False
        return False
    except Exception:
        vtk_widget._naksha_interaction_lod_active = False
        return False


def restore_main_full_detail(app):
    """Hide the interaction actor and reveal the untouched full actor."""
    if app is None or getattr(app, "_shutdown_in_progress", False):
        return False
    actor = getattr(app, "_unified_actor", None)
    if actor is None or not getattr(actor, "_naksha_lod_active", False):
        return False
    lod_actor = getattr(app, "_main_interaction_actor", None)
    if lod_actor is None:
        actor._naksha_lod_active = False
        return False
    try:
        lod_actor.SetVisibility(0)
        actor.SetVisibility(int(getattr(actor, "_naksha_full_visibility_before_lod", 1)))
        actor._naksha_lod_active = False
        return True
    except (RuntimeError, AttributeError, ReferenceError):
        actor._naksha_lod_active = False
        return False
    except Exception:
        actor._naksha_lod_active = False
        return False

def _clear_section_visual_actors(vtk_widget, view_idx: int) -> None:
    actor_name = f"_section_{view_idx}_unified"
    interaction_actor_name = f"_section_{view_idx}_interaction_lod"
    actors = getattr(vtk_widget, "actors", {})

    if actor_name in actors:
        vtk_widget.remove_actor(actor_name, render=False)
    if interaction_actor_name in actors:
        vtk_widget.remove_actor(interaction_actor_name, render=False)
    vtk_widget._naksha_full_detail_actor = None
    vtk_widget._naksha_interaction_actor = None
    vtk_widget._naksha_interaction_lod_active = False

    for name in list(actors.keys()):
        name_str = str(name)
        if name_str.startswith(("class_", "border_")) or name_str in ("border_layer", "color_layer"):
            vtk_widget.remove_actor(name, render=False)


def _section_draw_sizes(
    weight: float,
    border_percent: float,
    base_point_size: float = _BASE_POINT_SIZE,
) -> tuple[float, float]:
    clamped_weight = max(0.1, min(float(weight or 1.0), 12.0))
    color_size = max(1.0, min(base_point_size * clamped_weight, 15.0))

    if float(border_percent or 0.0) > 0.0:
        border_scale = 1.0 + (float(border_percent) / 60.0)
        border_size = max(1.0, min(color_size * border_scale, 15.0))
    else:
        border_size = color_size

    return color_size, border_size


def section_requires_legacy_border_render(
    app,
    view_idx: int,
    palette: Optional[dict] = None,
    border_percent: float = 0.0,
) -> bool:
    slot_idx = view_idx + 1
    palette = palette or _get_slot_palette(app, slot_idx)

    if border_percent <= 0.0 and hasattr(app, 'view_borders'):
        border_percent = float(app.view_borders.get(slot_idx, 0) or 0.0)

    visible_classes = {
        int(code) for code, info in (palette or {}).items()
        if info.get("show", True)
    }
    recent_class = getattr(app, "_last_classified_to_class", None)
    has_special_order = recent_class is not None and int(recent_class) in visible_classes
    return has_special_order


def build_section_legacy_border_actors(
    app,
    view_idx: int,
    palette: Optional[dict] = None,
    border_percent: float = 0.0,
    point_size: float = _BASE_POINT_SIZE,
) -> bool:
    slot_idx = view_idx + 1

    if not hasattr(app, 'section_vtks') or view_idx not in app.section_vtks:
        return False

    vtk_widget = app.section_vtks[view_idx]
    if vtk_widget is None:
        return False

    core_pts = getattr(app, f"section_{view_idx}_core_points", None)
    buf_pts = getattr(app, f"section_{view_idx}_buffer_points", None)
    core_mask = getattr(app, f"section_{view_idx}_core_mask", None)
    buf_mask = getattr(app, f"section_{view_idx}_buffer_mask", None)

    if core_pts is None or core_mask is None:
        return False

    data = getattr(app, "data", None)
    if data is None or "classification" not in data:
        return False

    classification_full = data["classification"]
    n_class = len(classification_full)

    core_mask = np.asarray(core_mask, dtype=bool).ravel()
    if core_mask.size != n_class:
        print(
            f"   ⚠️ Section {view_idx + 1}: stale core_mask "
            f"(len={core_mask.size}, data={n_class}) - skipping refresh"
        )
        return False

    core_global_idx = np.flatnonzero(core_mask)
    if len(core_pts) != len(core_global_idx):
        print(
            f"   ⚠️ Section {view_idx + 1}: core_points/core_mask mismatch "
            f"({len(core_pts)} vs {len(core_global_idx)}) - skipping refresh"
        )
        return False

    if buf_pts is not None and buf_mask is not None and len(buf_pts) > 0:
        buf_mask = np.asarray(buf_mask, dtype=bool).ravel()
        if buf_mask.size != n_class:
            print(
                f"   ⚠️ Section {view_idx + 1}: stale buffer_mask "
                f"(len={buf_mask.size}, data={n_class}) - ignoring buffer"
            )
            buf_pts = None
            buf_mask = None
        else:
            buf_only_mask = buf_mask & ~core_mask
            buf_global_idx = np.flatnonzero(buf_only_mask)
            if len(buf_pts) != len(buf_global_idx):
                print(
                    f"   ⚠️ Section {view_idx + 1}: buffer_points/buffer_mask mismatch "
                    f"({len(buf_pts)} vs {len(buf_global_idx)}) - ignoring buffer"
                )
                buf_pts = None
                buf_mask = None

    if buf_pts is not None and buf_mask is not None and len(buf_pts) > 0:
        buf_only_mask = buf_mask & ~core_mask
        buf_global_idx = np.flatnonzero(buf_only_mask)
        all_pts = np.vstack([core_pts, buf_pts])
        all_cls = np.concatenate([
            classification_full[core_global_idx],
            classification_full[buf_global_idx],
        ])
        all_global_idx = np.concatenate([core_global_idx, buf_global_idx])
    else:
        all_pts = core_pts
        all_cls = classification_full[core_global_idx]
        all_global_idx = core_global_idx

    setattr(app, f"_section_{view_idx}_global_indices", all_global_idx)

    palette = palette or _get_slot_palette(app, slot_idx)
    if border_percent <= 0.0 and hasattr(app, 'view_borders'):
        border_percent = float(app.view_borders.get(slot_idx, 0) or 0.0)

    if not hasattr(app, 'view_borders'):
        app.view_borders = {}
    app.view_borders[slot_idx] = float(border_percent)

    visible_classes = [
        int(code) for code, info in (palette or {}).items()
        if info.get("show", True)
    ]

    try:
        cam_pos = vtk_widget.camera_position
    except Exception:
        cam_pos = None

    _clear_section_visual_actors(vtk_widget, view_idx)

    if len(all_pts) == 0 or not visible_classes:
        vtk_widget._naksha_section_render_mode = "legacy"
        if cam_pos is not None:
            try:
                vtk_widget.camera_position = cam_pos
            except Exception:
                pass
        vtk_widget.render()
        return True

    visible_mask = np.isin(all_cls, visible_classes)
    filtered_pts = all_pts[visible_mask]
    filtered_cls = all_cls[visible_mask]

    # Safety cap for legacy renderer path.
    legacy_cap = _section_render_point_cap(app)
    if legacy_cap is not None and len(filtered_pts) > legacy_cap:
        sel = _uniform_pick_indices(len(filtered_pts), legacy_cap)
        filtered_pts = filtered_pts[sel]
        filtered_cls = filtered_cls[sel]
        print(
            f"   ⚡ Section {view_idx + 1} legacy cap: "
            f"{len(all_pts):,} -> {len(filtered_pts):,} (manual budget={legacy_cap:,})"
        )

    if len(filtered_pts) == 0:
        vtk_widget._naksha_section_render_mode = "legacy"
        if cam_pos is not None:
            try:
                vtk_widget.camera_position = cam_pos
            except Exception:
                pass
        vtk_widget.render()
        return True

    base_point_size = max(1.0, float(point_size or _BASE_POINT_SIZE))
    recent_class = getattr(app, "_last_classified_to_class", None)
    has_custom_weights = any(
        abs(float(info.get("weight", 1.0)) - 1.0) > 1e-6
        for info in (palette or {}).values()
    )
    use_special_order = recent_class is not None and int(recent_class) in visible_classes

    if has_custom_weights or use_special_order:
        class_weights = []
        for code in visible_classes:
            if use_special_order and int(code) == int(recent_class):
                continue
            weight = float((palette or {}).get(int(code), {}).get("weight", 1.0))
            class_weights.append((int(code), weight))

        class_weights.sort(key=lambda item: item[1], reverse=True)
        render_order = [code for code, _ in class_weights]
        if use_special_order:
            render_order.append(int(recent_class))

        for code in render_order:
            class_mask = (filtered_cls == code)
            if not np.any(class_mask):
                continue

            class_pts = filtered_pts[class_mask]
            entry = (palette or {}).get(int(code), {})
            weight = float(entry.get("weight", 1.0))
            color = np.asarray(entry.get("color", (128, 128, 128)), dtype=np.uint8)
            color_size, border_size = _section_draw_sizes(weight, border_percent, base_point_size)

            if float(border_percent) > 0.0:
                border_cloud = pv.PolyData(class_pts)
                border_cloud["RGB"] = np.zeros((len(class_pts), 3), dtype=np.uint8)
                vtk_widget.add_points(
                    border_cloud, scalars="RGB", rgb=True,
                    point_size=border_size, render_points_as_spheres=True,
                    name=f"border_{code}", reset_camera=False, render=False,
                )

            cloud = pv.PolyData(class_pts)
            cloud["RGB"] = np.tile(color, (len(class_pts), 1)).astype(np.uint8)
            vtk_widget.add_points(
                cloud, scalars="RGB", rgb=True,
                point_size=color_size, render_points_as_spheres=True,
                name=f"class_{code}", reset_camera=False, render=False,
            )
    else:
        lut_size = max(int(filtered_cls.max()) + 1, 256)
        color_lut = np.full((lut_size, 3), 128, dtype=np.uint8)
        for code, entry in (palette or {}).items():
            idx = int(code)
            if 0 <= idx < lut_size:
                color_lut[idx] = entry.get("color", (128, 128, 128))

        _, border_size = _section_draw_sizes(1.0, border_percent, base_point_size)

        if float(border_percent) > 0.0:
            border_cloud = pv.PolyData(filtered_pts)
            border_cloud["RGB"] = np.zeros((len(filtered_pts), 3), dtype=np.uint8)
            vtk_widget.add_points(
                border_cloud, scalars="RGB", rgb=True,
                point_size=border_size, render_points_as_spheres=True,
                name="border_layer", reset_camera=False, render=False,
            )

        cloud = pv.PolyData(filtered_pts)
        cloud["RGB"] = color_lut[filtered_cls.astype(np.intp)]
        vtk_widget.add_points(
            cloud, scalars="RGB", rgb=True,
            point_size=base_point_size, render_points_as_spheres=True,
            name="color_layer", reset_camera=False, render=False,
        )

    vtk_widget._naksha_section_render_mode = "legacy"
    if cam_pos is not None:
        try:
            vtk_widget.camera_position = cam_pos
        except Exception:
            pass
    try:
        vtk_widget.renderer.ResetCameraClippingRange()
    except Exception:
        pass
    vtk_widget.render()

    print(
        f"   Section {view_idx + 1} legacy border render: "
        f"{len(filtered_pts):,} pts (border={border_percent}%)"
    )
    return True


# ─────────────────────────────────────────────────────────────────────────────
# BUILD SECTION UNIFIED ACTOR
# ─────────────────────────────────────────────────────────────────────────────
def build_section_unified_actor(
    app,
    view_idx: int,
    palette: Optional[dict] = None,
    border_percent: float = 0.0,
    point_size: float = _BASE_POINT_SIZE,
    **kwargs
) -> Optional[object]:
    t0 = time.perf_counter()
    slot_idx = view_idx + 1

    if not hasattr(app, 'section_vtks') or view_idx not in app.section_vtks:
        return None
    vtk_widget = app.section_vtks[view_idx]
    if vtk_widget is None:
        return None

    core_pts  = getattr(app, f"section_{view_idx}_core_points",  None)
    buf_pts   = getattr(app, f"section_{view_idx}_buffer_points", None)
    core_mask = getattr(app, f"section_{view_idx}_core_mask",    None)
    buf_mask  = getattr(app, f"section_{view_idx}_buffer_mask",  None)

    if core_pts is None or core_mask is None:
        return None

    data = getattr(app, 'data', None)
    if data is None or 'classification' not in data:
        return None

    classification_full = data['classification']
    n_class = len(classification_full)

    core_mask = np.asarray(core_mask, dtype=bool).ravel()
    if core_mask.size != n_class:
        print(
            f"   ⚠️ Section {view_idx + 1}: stale core_mask "
            f"(len={core_mask.size}, data={n_class}) - skipping refresh"
        )
        return None

    core_global_idx = np.flatnonzero(core_mask)
    if len(core_pts) != len(core_global_idx):
        print(
            f"   ⚠️ Section {view_idx + 1}: core_points/core_mask mismatch "
            f"({len(core_pts)} vs {len(core_global_idx)}) - skipping refresh"
        )
        return None

    all_pts_prebuilt = getattr(app, f"section_{view_idx}_points_transformed", None)
    has_buffer = False
    buf_global_idx = np.empty((0,), dtype=np.int64)
    if buf_pts is not None and buf_mask is not None and len(buf_pts) > 0:
        buf_mask = np.asarray(buf_mask, dtype=bool).ravel()
        if buf_mask.size != n_class:
            print(
                f"   ⚠️ Section {view_idx + 1}: stale buffer_mask "
                f"(len={buf_mask.size}, data={n_class}) - ignoring buffer"
            )
            buf_pts = None
            buf_mask = None
        else:
            buf_only_mask = buf_mask & ~core_mask
            buf_global_idx = np.flatnonzero(buf_only_mask)
            if len(buf_pts) != len(buf_global_idx):
                print(
                    f"   ⚠️ Section {view_idx + 1}: buffer_points/buffer_mask mismatch "
                    f"({len(buf_pts)} vs {len(buf_global_idx)}) - ignoring buffer"
                )
                buf_pts = None
                buf_mask = None
            else:
                has_buffer = True

    core_pts_use = core_pts
    core_idx_use = core_global_idx
    buf_pts_use = buf_pts if has_buffer else None
    buf_idx_use = buf_global_idx if has_buffer else np.empty((0,), dtype=np.int64)

    # Flight-line visibility is global: section actors obey the same Point
    # Source ID filter as the main view in every display/colour mode.
    from gui.flight_line_filter import flight_line_visibility_mask
    line_mask = flight_line_visibility_mask(app, n_class, slot=slot_idx)
    core_line_keep = line_mask[core_idx_use]
    core_pts_use = np.asarray(core_pts_use)[core_line_keep]
    core_idx_use = core_idx_use[core_line_keep]
    if buf_pts_use is not None:
        buf_line_keep = line_mask[buf_idx_use]
        buf_pts_use = np.asarray(buf_pts_use)[buf_line_keep]
        buf_idx_use = buf_idx_use[buf_line_keep]

    total_raw_points = int(len(core_pts_use) + (len(buf_pts_use) if buf_pts_use is not None else 0))
    section_cap = _section_render_point_cap(app)
    downsampled = False
    if section_cap is not None and total_raw_points > section_cap:
        downsampled = True
        core_target = min(len(core_pts_use), max(1, int(round(section_cap * 0.8))))
        remaining = max(0, section_cap - core_target)
        buf_target = min(len(buf_pts_use) if buf_pts_use is not None else 0, remaining)

        if core_target < len(core_pts_use) and (core_target + buf_target) < section_cap:
            extra = min(len(core_pts_use) - core_target, section_cap - (core_target + buf_target))
            core_target += max(0, extra)

        if (core_target + buf_target) > section_cap:
            overflow = (core_target + buf_target) - section_cap
            if buf_target >= overflow:
                buf_target -= overflow
            else:
                core_target = max(1 if len(core_pts_use) > 0 else 0, core_target - (overflow - buf_target))
                buf_target = 0

        core_sel = _uniform_pick_indices(len(core_pts_use), core_target)
        core_pts_use = core_pts_use[core_sel] if core_sel.size else core_pts_use[:0]
        core_idx_use = core_idx_use[core_sel] if core_sel.size else core_idx_use[:0]

        if buf_pts_use is not None and len(buf_pts_use) > 0:
            buf_sel = _uniform_pick_indices(len(buf_pts_use), buf_target)
            buf_pts_use = buf_pts_use[buf_sel] if buf_sel.size else buf_pts_use[:0]
            buf_idx_use = buf_idx_use[buf_sel] if buf_sel.size else buf_idx_use[:0]

        print(
            f"   ⚡ Section {view_idx + 1} render cap: "
            f"{total_raw_points:,} -> {len(core_pts_use) + (len(buf_pts_use) if buf_pts_use is not None else 0):,} "
            f"(manual budget={section_cap:,}; set NAKSHA_SECTION_POINT_BUDGET to increase)"
        )

    if buf_pts_use is not None and len(buf_pts_use) > 0:
        if (
            not downsampled
            and bool(np.all(core_line_keep))
            and (buf_pts_use is None or bool(np.all(buf_line_keep)))
            and all_pts_prebuilt is not None
            and len(all_pts_prebuilt) == (len(core_pts) + len(buf_pts))
        ):
            all_pts = all_pts_prebuilt
        else:
            all_pts = np.vstack([core_pts_use, buf_pts_use])
        all_global_indices = np.concatenate([core_idx_use, buf_idx_use])
    else:
        all_pts = core_pts_use
        all_global_indices = core_idx_use

    all_cls = classification_full[all_global_indices]
    combined_global_mask = getattr(app, f"section_{view_idx}_combined_mask", None)
    if not (
        isinstance(combined_global_mask, np.ndarray)
        and combined_global_mask.dtype == bool
        and combined_global_mask.size == n_class
    ):
        combined_global_mask = core_mask if (buf_pts_use is None or len(buf_pts_use) == 0) else None
    if len(all_pts) == 0:
        actor_name = f"_section_{view_idx}_unified"
        interaction_actor_name = f"_section_{view_idx}_interaction_lod"
        for name in (actor_name, interaction_actor_name):
            if name in vtk_widget.actors:
                vtk_widget.remove_actor(name, render=False)
        setattr(app, f"_section_{view_idx}_global_indices", np.empty(0, dtype=np.int64))
        vtk_widget.render()
        return None

    setattr(app, f"_section_{view_idx}_global_indices", all_global_indices)
    palette = palette or _get_slot_palette(app, slot_idx)

    if hasattr(app, 'view_borders') and slot_idx in app.view_borders:
        border_percent = float(app.view_borders[slot_idx])

    actor_name = f"_section_{view_idx}_unified"
    interaction_actor_name = f"_section_{view_idx}_interaction_lod"
    cam_pos = vtk_widget.camera_position
    actual_pt_size = max(1.0, float(point_size or _BASE_POINT_SIZE))
    n_pts = len(all_pts)

    if actor_name in vtk_widget.actors:
        vtk_widget.remove_actor(actor_name, render=False)
    if interaction_actor_name in vtk_widget.actors:
        vtk_widget.remove_actor(interaction_actor_name, render=False)
    vtk_widget._naksha_full_detail_actor = None
    vtk_widget._naksha_interaction_actor = None
    vtk_widget._naksha_interaction_lod_active = False
    for name in list(vtk_widget.actors.keys()):
        if str(name).startswith(("class_", "border_")) or str(name) in ("border_layer", "color_layer"):
            vtk_widget.remove_actor(name, render=False)

    lut_obj = _get_lut(f"section_{view_idx}")
    use_lod = n_pts > _LOD_SKIP_THRESHOLD

    def _build_arrays(pts, cls):
        return _build_section_numpy_data(
            pts, cls, palette, border_percent, lut_obj.map_classes, _compute_boundary_flags
        )

    if not use_lod:
        arrays = _build_arrays(all_pts, all_cls)
        actor, ctx, mesh, vtk_ca, _vtk_rgb, _vtk_cls, class_vtk, bf_vtk = _build_vtk_actor_from_arrays(
            vtk_widget, actor_name, arrays, actual_pt_size, palette, border_percent, slot_idx,
            _attach_view_shader_context, _ensure_opengl_polydata_mapper, ViewShaderContext
        )
        if actor is None:
            return None
        _wire_actor_metadata(actor, mesh, vtk_ca, _vtk_rgb, _vtk_cls, class_vtk, bf_vtk, combined_global_mask, actual_pt_size)
        actor._naksha_global_indices = all_global_indices
        vtk_widget._naksha_full_detail_actor = actor
        try:
            from PySide6.QtCore import QTimer
            _a, _c, _w = actor, ctx, vtk_widget
            # The initial section render below creates the GL context. Queue
            # setup immediately afterward, before the first pan input arrives.
            QTimer.singleShot(0, lambda: _deferred_actor_gpu_init(_a, _c, _w, f"Section{view_idx+1}"))
        except Exception:
            pass
        vtk_widget._naksha_section_render_mode = "unified"
        vtk_widget.camera_position = cam_pos
        vtk_widget.render()
        elapsed = (time.perf_counter() - t0) * 1000
        print(f"   🏗️ Section {view_idx+1} unified actor: {n_pts:,} pts in {elapsed:.1f} ms "
              f"(slot={slot_idx}, border={border_percent}%, base_size={actual_pt_size})")
        return actor

    lod_count = min(n_pts, _LOD_FIRST_FRAME_MAX)
    lod_sel = _uniform_pick_indices(n_pts, lod_count)
    lod_pts = all_pts[lod_sel]
    lod_cls = all_cls[lod_sel]
    lod_arrays = _build_arrays(lod_pts, lod_cls)
    lod_actor, lod_ctx, lod_mesh, lod_vtk_ca, lod_vtk_rgb, lod_vtk_cls, lod_class_vtk, lod_bf_vtk = _build_vtk_actor_from_arrays(
        vtk_widget, actor_name, lod_arrays, actual_pt_size, palette, border_percent, slot_idx,
        _attach_view_shader_context, _ensure_opengl_polydata_mapper, ViewShaderContext
    )
    if lod_actor is None:
        return None
    _wire_actor_metadata(lod_actor, lod_mesh, lod_vtk_ca, lod_vtk_rgb, lod_vtk_cls, lod_class_vtk, lod_bf_vtk, combined_global_mask, actual_pt_size)
    lod_actor._naksha_global_indices = all_global_indices[lod_sel]
    lod_actor._naksha_lod_source_indices = lod_sel
    vtk_widget._naksha_section_render_mode = "unified_lod"
    vtk_widget.camera_position = cam_pos
    vtk_widget.render()

    def _upgrade_full_res():
        lock = _get_section_build_lock(view_idx)
        if not lock.acquire(blocking=False):
            return
        try:
            full_arrays = _build_arrays(all_pts, all_cls)
            from PySide6.QtCore import QTimer

            def _swap_on_main_thread():
                try:
                    current = app.section_vtks.get(view_idx) if hasattr(app, "section_vtks") else None
                    if current is not vtk_widget:
                        return
                    if actor_name not in vtk_widget.actors:
                        return
                    vtk_widget.remove_actor(actor_name, render=False)
                    if INTERACTION_ACTOR_SWITCHING_ENABLED:
                        try:
                            vtk_widget.add_actor(
                                lod_actor,
                                reset_camera=False,
                                name=interaction_actor_name,
                                render=False,
                            )
                            lod_actor.SetVisibility(0)
                        except Exception:
                            # Full detail can still be built safely.
                            pass
                    full_actor, full_ctx, full_mesh, full_vtk_ca, full_vtk_rgb, full_vtk_cls, full_class_vtk, full_bf_vtk = _build_vtk_actor_from_arrays(
                        vtk_widget, actor_name, full_arrays, actual_pt_size, palette, border_percent, slot_idx,
                        _attach_view_shader_context, _ensure_opengl_polydata_mapper, ViewShaderContext
                    )
                    if full_actor is None:
                        try:
                            if interaction_actor_name in vtk_widget.actors:
                                vtk_widget.remove_actor(interaction_actor_name, render=False)
                            vtk_widget.add_actor(
                                lod_actor,
                                reset_camera=False,
                                name=actor_name,
                                render=False,
                            )
                            lod_actor.SetVisibility(1)
                        except Exception:
                            pass
                        return
                    _wire_actor_metadata(full_actor, full_mesh, full_vtk_ca, full_vtk_rgb, full_vtk_cls, full_class_vtk, full_bf_vtk, combined_global_mask, actual_pt_size)
                    full_actor._naksha_global_indices = all_global_indices
                    if interaction_actor_name in vtk_widget.actors:
                        vtk_widget._naksha_full_detail_actor = full_actor
                        vtk_widget._naksha_interaction_actor = lod_actor
                        vtk_widget._naksha_interaction_lod_active = False
                    vtk_widget._naksha_section_render_mode = "unified"
                    vtk_widget.camera_position = cam_pos
                    vtk_widget.render()
                    try:
                        _deferred_actor_gpu_init(full_actor, full_ctx, vtk_widget, f"Section{view_idx+1}")
                    except Exception:
                        pass
                finally:
                    lock.release()

            QTimer.singleShot(0, _swap_on_main_thread)
        except Exception:
            try:
                lock.release()
            except Exception:
                pass

    _section_build_executor.submit(_upgrade_full_res)

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"   🏗️ Section {view_idx+1} LOD actor: {lod_count:,}/{n_pts:,} pts in {elapsed:.1f} ms "
          f"(slot={slot_idx}, border={border_percent}%, base_size={actual_pt_size})")
    return lod_actor


# ─────────────────────────────────────────────────────────────────────────────
# PARTIAL UPDATES (The "Real" Performance Fix for 48M+ points)
# ─────────────────────────────────────────────────────────────────────────────
def fast_partial_cross_section_update(app, view_idx: int, global_changed_mask: np.ndarray):
    """Updates ONLY the points that changed within a specific cross-section."""
    if not hasattr(app, 'section_vtks') or view_idx not in app.section_vtks:
        return False

    vtk_widget = app.section_vtks[view_idx]
    if vtk_widget is None:
        return False

    actor_name = f"_section_{view_idx}_unified"
    actor = vtk_widget.actors.get(actor_name) if hasattr(vtk_widget, 'actors') else None
    if actor is None:
        return False

    rgb_ptr = getattr(actor, '_naksha_rgb_ptr', None)
    if rgb_ptr is None or not _is_writable(rgb_ptr):
        return False

    # IMPORTANT:
    # Section unified actors are built in this exact local order:
    #   [core_global_indices, buffer_only_global_indices]
    # (see build_section_unified_actor)
    # So we must compute changed local indices using that same order.
    # Using np.where(section_mask) order is incorrect and can miss/shift updates.
    section_global_indices = getattr(app, f"_section_{view_idx}_global_indices", None)
    if section_global_indices is not None and len(section_global_indices) > 0:
        try:
            max_idx = int(section_global_indices.max(initial=0))
        except Exception:
            max_idx = 0
        if len(global_changed_mask) < (max_idx + 1):
            return True
        local_indices = np.flatnonzero(global_changed_mask[section_global_indices])
    else:
        # Fallback (older views): derive using combined mask and actor-local map.
        section_mask = getattr(app, f"section_{view_idx}_combined_mask", None)
        if section_mask is None:
            return False

        changed_in_view_global_indices = np.where(global_changed_mask & section_mask)[0]
        if len(changed_in_view_global_indices) == 0:
            return True

        g_to_l_arr = getattr(actor, '_naksha_global_to_local_arr', None)
        if g_to_l_arr is None:
            full_indices = np.where(section_mask)[0]
            g_to_l_arr = np.full(len(app.data["xyz"]), -1, dtype=np.int32)
            g_to_l_arr[full_indices] = np.arange(len(full_indices), dtype=np.int32)
            actor._naksha_global_to_local_arr = g_to_l_arr

        local_indices = g_to_l_arr[changed_in_view_global_indices]
        valid_mask = local_indices != -1
        if not np.any(valid_mask):
            return True
        local_indices = local_indices[valid_mask]

    if len(local_indices) == 0:
        return True

    try:
        _patch_actor_memory(app, actor, local_indices, slot_idx=view_idx + 1)
    except Exception as e:
        print(f"⚠️ fast_partial_cross_section_update patch failed (view={view_idx}): {e}")
        return False
    return True


def fast_partial_classify_update(app, global_changed_mask: np.ndarray):
    """Updates ONLY the points that changed in the MAIN VIEW."""
    actor = _get_unified_actor(app)
    if actor is None:
        return False

    rgb_ptr = getattr(actor, "_naksha_rgb_ptr", None)
    if rgb_ptr is None or not _is_writable(rgb_ptr):
        return False

    gi = getattr(app, '_main_global_indices', None)
    palette = getattr(app, "class_palette", {})
    classification = app.data["classification"]

    if gi is None:
        local_indices = np.where(global_changed_mask)[0]
    else:
        gi_mask = getattr(app, '_main_global_mask', None)
        if gi_mask is None:
            gi_mask = np.zeros(len(app.data["xyz"]), dtype=bool)
            gi_mask[gi] = True
            app._main_global_mask = gi_mask

        changed_visible_global = np.where(global_changed_mask & gi_mask)[0]
        if len(changed_visible_global) == 0:
            return True

        g_to_l_arr = getattr(actor, '_naksha_global_to_local_arr', None)
        if g_to_l_arr is None:
            g_to_l_arr = np.full(len(app.data["xyz"]), -1, dtype=np.int32)
            g_to_l_arr[gi] = np.arange(len(gi), dtype=np.int32)
            actor._naksha_global_to_local_arr = g_to_l_arr

        local_indices = g_to_l_arr[changed_visible_global]
        valid_mask = local_indices != -1
        if not np.any(valid_mask):
            return True
        local_indices = local_indices[valid_mask]

    try:
        display_mode = str(getattr(app, "display_mode", "class") or "class").lower()
        # Guard main-view RGB/class-buffer patching outside class/shaded_class modes.
        if display_mode not in ("class", "shaded_class"):
            return True
        _patch_actor_memory(app, actor, local_indices, slot_idx=0)
    except Exception as e:
        print(f"⚠️ fast_partial_classify_update patch failed: {e}")
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# fast_classify_update — main view
# ─────────────────────────────────────────────────────────────────────────────
def fast_classify_update(
    app,
    changed_mask: np.ndarray,
    to_class: int = None,
    skip_render: bool = False,
    **kwargs,
) -> bool:
    """
    Fast main-view classification buffer patch.

    ``to_class`` is retained for API compatibility only.  The authoritative
    class identity for every changed point is always read from
    ``app.data["classification"]``.  This prevents a stale changed mask from
    repainting old points with the currently selected shortcut class.
    """
    actor = _get_unified_actor(app)
    if actor is None:
        return False

    if not isinstance(changed_mask, np.ndarray) or changed_mask.dtype != bool:
        return False

    classification = None
    if hasattr(app, "data") and app.data:
        classification = app.data.get("classification")
    if classification is None or changed_mask.ndim != 1 or len(changed_mask) != len(classification):
        return False

    global_indices = getattr(app, '_main_global_indices', None)
    if global_indices is not None:
        section_changed = changed_mask[global_indices]
        local_changed = np.flatnonzero(section_changed)
        if local_changed.size == 0:
            return True
        changed_global = np.asarray(global_indices, dtype=np.int64)[local_changed]
    else:
        local_changed = np.flatnonzero(changed_mask)
        if local_changed.size == 0:
            return True
        changed_global = local_changed.astype(np.int64, copy=False)

    # Only update slot-0 RGB/class buffers when main view is in class/shaded_class.
    display_mode = str(getattr(app, "display_mode", "class") or "class").lower()
    if display_mode in ("class", "shaded_class"):
        rgb_ptr = getattr(actor, "_naksha_rgb_ptr", None)
        mesh = getattr(actor, '_naksha_mesh', None)

        # Clamp against the live actor buffer once.  Keep global/local mappings
        # aligned when a cropped/subset main actor is active.
        if rgb_ptr is not None and local_changed[-1] >= len(rgb_ptr):
            keep = local_changed < len(rgb_ptr)
            local_changed = local_changed[keep]
            changed_global = changed_global[keep]
        if local_changed.size == 0:
            return True

        canonical_classes = np.asarray(classification)[changed_global]

        if rgb_ptr is not None:
            palette = kwargs.get("palette") or getattr(app, "class_palette", {}) or _get_slot_palette(app, 0)
            max_class = max(256, int(np.max(canonical_classes, initial=0)) + 1)
            color_lut = _get_lut_array(palette, max_class)
            safe_classes = canonical_classes.clip(0, max_class - 1).astype(np.intp, copy=False)
            rgb_ptr[local_changed] = color_lut[safe_classes]
            vtk_ca = getattr(actor, "_naksha_vtk_array", None)
            if vtk_ca is not None:
                vtk_ca.Modified()

        if mesh is not None:
            class_vtk_arr = mesh.GetPointData().GetArray("Classification")
            if class_vtk_arr is not None:
                cls_np = numpy_support.vtk_to_numpy(class_vtk_arr)
                valid = local_changed < len(cls_np)
                if not np.all(valid):
                    local_for_class = local_changed[valid]
                    classes_for_class = canonical_classes[valid]
                else:
                    local_for_class = local_changed
                    classes_for_class = canonical_classes
                if local_for_class.size > 0:
                    cls_np[local_for_class] = classes_for_class.astype(cls_np.dtype, copy=False)
                    class_vtk_arr.Modified()
                    mesh.Modified()

        _mark_actor_dirty(actor)

        ctx = getattr(actor, '_naksha_shader_ctx', None)
        if ctx is not None:
            last_gen = getattr(actor, '_last_uniform_generation', -1)
            if ctx._generation != last_gen:
                _push_uniforms_direct(actor, ctx)
                actor._last_uniform_generation = ctx._generation

        print(
            f"MAIN_REFRESH source=canonical_classification_array "
            f"changed={int(local_changed.size)}"
        )

    if not skip_render and display_mode in ("class", "shaded_class"):
        try:
            # Keep the throttled call: app.vtk_widget.render is wrapped by
            # GPURenderManager which queues main-view renders to ~10 fps.
            app.vtk_widget.render()
            app._gpu_sync_done = True
        except Exception as _e:
            print(f"⚠️ GPU render error (fast_classify_update): {_e}")

    return True


# ─────────────────────────────────────────────────────────────────────────────
# fast_cross_section_update — section views
# ─────────────────────────────────────────────────────────────────────────────
def fast_cross_section_update(
    app,
    view_idx: int,
    changed_mask_global: np.ndarray,
    palette: Optional[dict] = None,
    skip_render: bool = False,
    force_visibility_refresh: bool = False,
) -> bool:
    t0 = time.perf_counter()

    if not hasattr(app, "section_vtks") or view_idx not in app.section_vtks:
        return False

    vtk_widget     = app.section_vtks[view_idx]
    slot_idx       = view_idx + 1
    palette        = palette or _get_slot_palette(app, slot_idx)
    if palette is None:
        palette = {}

    border_percent = float(getattr(app, "view_borders", {}).get(slot_idx, 0) or 0.0)
    if section_requires_legacy_border_render(app, view_idx, palette, border_percent):
        # Legacy border mode requires full geometry — caller must call build_section_unified_actor
        return False

    actor_name     = f"_section_{view_idx}_unified"
    global_indices = getattr(app, f"_section_{view_idx}_global_indices", None)

    if global_indices is None or len(global_indices) == 0:
        classification_ref = (app.data.get("classification")
                              if hasattr(app, "data") and app.data else None)
        combined_mask = getattr(app, f"section_{view_idx}_combined_mask", None)
        if (
            classification_ref is not None
            and isinstance(combined_mask, np.ndarray)
            and combined_mask.dtype == bool
            and combined_mask.size == len(classification_ref)
        ):
            global_indices = np.flatnonzero(combined_mask)
            setattr(app, f"_section_{view_idx}_global_indices", global_indices)

    if actor_name not in vtk_widget.actors or global_indices is None:
        # Actor not built yet — caller must call _plot_section / build_section_unified_actor
        return False

    actor = vtk_widget.actors.get(actor_name)
    if actor is None:
        return False

    actor_global_indices = getattr(actor, "_naksha_global_indices", None)
    if (
        isinstance(actor_global_indices, np.ndarray)
        and actor_global_indices.size > 0
    ):
        global_indices = actor_global_indices

    rgb_ptr = getattr(actor, "_naksha_rgb_ptr", None)
    if rgb_ptr is None or not _is_writable(rgb_ptr):
        return False

    classification = (app.data.get("classification")
                      if hasattr(app, 'data') and app.data else None)
    if classification is None:
        return False

    mapper = actor.GetMapper()
    poly   = mapper.GetInput() if mapper else None
    if poly is None:
        return False

    vtk_scalars = poly.GetPointData().GetScalars()
    if vtk_scalars is None:
        return False

    changed_idx = None
    cached_changed_idx = getattr(app, "_last_changed_indices", None)
    cached_mask_ref = getattr(app, "_last_changed_mask", None)
    can_use_sparse = (
        isinstance(cached_changed_idx, np.ndarray)
        and cached_changed_idx.size > 0
        and (changed_mask_global is cached_mask_ref)
    )

    if can_use_sparse and isinstance(changed_mask_global, np.ndarray) and changed_mask_global.dtype == bool:
        try:
            _probe = cached_changed_idx[
                (cached_changed_idx >= 0) & (cached_changed_idx < len(changed_mask_global))
            ]
            if _probe.size == 0 or not np.all(changed_mask_global[_probe]):
                can_use_sparse = False
        except Exception:
            can_use_sparse = False

    if can_use_sparse:
        g_to_l_arr = getattr(actor, '_naksha_global_to_local_arr', None)
        if g_to_l_arr is None or len(g_to_l_arr) != len(classification):
            g_to_l_arr = np.full(len(classification), -1, dtype=np.int32)
            g_to_l_arr[global_indices] = np.arange(len(global_indices), dtype=np.int32)
            actor._naksha_global_to_local_arr = g_to_l_arr

        _valid_changed = cached_changed_idx[
            (cached_changed_idx >= 0) & (cached_changed_idx < len(g_to_l_arr))
        ]
        if _valid_changed.size > 0:
            local_map = g_to_l_arr[_valid_changed]
            keep = local_map >= 0
            if np.any(keep):
                changed_idx = local_map[keep]

    if changed_idx is None:
        if (changed_mask_global is not None
                and len(changed_mask_global) >= (int(global_indices.max()) + 1 if len(global_indices) > 0 else 0)):
            section_changed = changed_mask_global[global_indices]
        else:
            section_changed = np.ones(len(global_indices), dtype=bool)
        changed_idx = np.flatnonzero(section_changed)

    n_changed = int(changed_idx.size)

    if n_changed > 0:
        if changed_idx.max(initial=-1) >= len(rgb_ptr):
            keep = changed_idx < len(rgb_ptr)
            changed_idx = changed_idx[keep]
            n_changed = int(changed_idx.size)
            if n_changed == 0:
                return False
        new_classes = classification[global_indices[changed_idx]]

        lut_obj   = _get_lut(f"section_{view_idx}")
        lut_obj.load_from_palette(palette)
        color_lut = lut_obj.lut
        rgb_ptr[changed_idx] = color_lut[new_classes.clip(0, len(color_lut) - 1).astype(np.intp)]

        vtk_rgb_arr = getattr(actor, "_naksha_vtk_array", None)
        if vtk_rgb_arr is not None:
            vtk_rgb_arr.Modified()

        cls_np = getattr(actor, "_naksha_section_class", None)
        class_vtk_arr = getattr(actor, "_naksha_class_vtk_ref", None)
        max_local = int(changed_idx.max()) if changed_idx.size > 0 else -1
        if cls_np is not None and len(cls_np) > max_local:
            try:
                cls_np[changed_idx] = new_classes.astype(cls_np.dtype, copy=False)
            except Exception:
                cls_np[changed_idx] = new_classes
            if class_vtk_arr is None:
                class_vtk_arr = poly.GetPointData().GetArray("Classification")
                actor._naksha_class_vtk_ref = class_vtk_arr
            if class_vtk_arr is not None:
                class_vtk_arr.Modified()
        else:
            class_vtk_arr = poly.GetPointData().GetArray("Classification")
            if class_vtk_arr is not None:
                cls_np = numpy_support.vtk_to_numpy(class_vtk_arr)
                cls_np[changed_idx] = new_classes.astype(cls_np.dtype, copy=False)
                class_vtk_arr.Modified()
                actor._naksha_section_class = cls_np
                actor._naksha_class_vtk_ref = class_vtk_arr

        vtk_scalars.Modified()
        poly.Modified()
        _mark_actor_dirty(actor)

    if force_visibility_refresh:
        try:
            # Re-push per-slot palette/LUT so hidden->visible class transitions
            # are reflected immediately without waiting for Display Mode Apply.
            sync_palette_to_gpu(
                app,
                slot_idx=slot_idx,
                palette=palette,
                border=border_percent,
                render=False,
            )
        except Exception as _vis_err:
            print(f"   ⚠️ force_visibility_refresh failed (view={view_idx+1}): {_vis_err}")

    ctx = getattr(actor, '_naksha_shader_ctx', None)
    if ctx is not None:
        _last_gen = getattr(actor, '_last_uniform_generation', -1)
        if ctx._generation != _last_gen:
            _push_uniforms_direct(actor, ctx)
            actor._last_uniform_generation = ctx._generation
    else:
        print("   ⚠️  No shader context on actor — uniforms not pushed")

    if not skip_render:
        try:
            if _safe_widget_render(vtk_widget):
                app._gpu_sync_done = True
        except Exception as _e:
            print(f"⚠️ GPU render error (fast_cross_section_update view={view_idx}): {_e}")

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"   ⚡ Section {view_idx + 1} RGB inject: {n_changed} pts [{elapsed:.1f} ms]")
    return True


def fast_undo_update(app, changed_mask: np.ndarray, **kwargs) -> bool:
    t0 = time.perf_counter()
    actors_updated = 0
    total_pts = 0

    main_actor = _get_unified_actor(app)
    if main_actor:
        gi = getattr(app, '_main_global_indices', None)
        if gi is not None:
            local_changed = np.flatnonzero(changed_mask[gi])
        else:
            local_changed = np.flatnonzero(changed_mask)

        if local_changed.size > 0:
            _patch_actor_memory(app, main_actor, local_changed, slot_idx=0)
            actors_updated += 1
            total_pts += local_changed.size

    if not (hasattr(app, 'section_vtks') and app.section_vtks):
        elapsed = (time.perf_counter() - t0) * 1000
        print(f"   ⚡ fast_undo_update: {total_pts:,} pts across "
              f"{actors_updated} views [{elapsed:.1f} ms]")
        return actors_updated > 0

    sections_to_render = []
    for view_idx, vtk_widget in app.section_vtks.items():
        if vtk_widget is None:
            continue
        actor_name = f"_section_{view_idx}_unified"
        sec_actor = vtk_widget.actors.get(actor_name)
        if sec_actor is None:
            continue
        rgb_ptr = getattr(sec_actor, "_naksha_rgb_ptr", None)
        if rgb_ptr is None or not _is_writable(rgb_ptr):
            continue
        gi_section = getattr(sec_actor, "_naksha_global_indices", None)
        if gi_section is None or len(gi_section) == 0:
            gi_section = getattr(app, f'_section_{view_idx}_global_indices', None)
        if gi_section is None:
            continue
        max_gi = int(gi_section.max(initial=0))
        if len(changed_mask) < max_gi + 1:
            local_sec_changed = np.arange(len(gi_section), dtype=np.intp)
        else:
            local_sec_changed = np.flatnonzero(changed_mask[gi_section])
        if local_sec_changed.size == 0:
            continue
        _patch_actor_memory(app, sec_actor, local_sec_changed, slot_idx=view_idx + 1)
        sections_to_render.append((view_idx, vtk_widget, local_sec_changed.size))
        actors_updated += 1
        total_pts += local_sec_changed.size

    for view_idx, vtk_widget, n_pts in sections_to_render:
        try:
            if _safe_widget_render(vtk_widget):
                print(f"   ⚡ Section {view_idx+1} RGB inject: {n_pts} pts")
        except Exception as e:
            print(f"   ⚠️  fast_undo_update: render failed section {view_idx+1}: {e}")

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"   ⚡ fast_undo_update: {total_pts:,} pts across "
          f"{actors_updated} views [{elapsed:.1f} ms]")
    return actors_updated > 0


def _patch_actor_memory(app, actor, local_indices: np.ndarray,
                        slot_idx: int) -> None:
    full_cls = app.data["classification"]
    local_indices = np.asarray(local_indices, dtype=np.int64).ravel()
    if local_indices.size == 0:
        return

    if slot_idx == 0:
        gi = getattr(app, '_main_global_indices', None)
        if gi is None or len(gi) == 0:
            gi = getattr(actor, '_naksha_global_indices', None)
        if gi is not None:
            gi = np.asarray(gi, dtype=np.int64).ravel()
            # Some legacy refresh callers supply global indices here. Convert
            # them against the sorted actor map instead of indexing the LOD
            # array (e.g. global 30M into a 12.7M actor buffer).
            if np.any(local_indices >= len(gi)):
                pos = np.searchsorted(gi, local_indices)
                valid = pos < len(gi)
                matched = np.zeros(len(pos), dtype=bool)
                matched[valid] = gi[pos[valid]] == local_indices[valid]
                local_indices = pos[matched]
                if local_indices.size == 0:
                    return
            else:
                local_indices = local_indices[
                    (local_indices >= 0) & (local_indices < len(gi))]
                if local_indices.size == 0:
                    return
            reverted_cls = full_cls[gi[local_indices]]
        else:
            local_indices = local_indices[
                (local_indices >= 0) & (local_indices < len(full_cls))]
            if local_indices.size == 0:
                return
            reverted_cls = full_cls[local_indices]
    else:
        view_idx = slot_idx - 1
        gi_section = getattr(actor, "_naksha_global_indices", None)
        if gi_section is None or len(gi_section) == 0:
            gi_section = getattr(app, f'_section_{view_idx}_global_indices', None)
        if gi_section is None:
            return
        gi_section = np.asarray(gi_section, dtype=np.int64).ravel()
        local_indices = local_indices[
            (local_indices >= 0) & (local_indices < len(gi_section))]
        if local_indices.size == 0:
            return
        reverted_cls = full_cls[gi_section[local_indices]]

    # Final actor-array guard. A stale mirror must never abort CPU undo.
    rgb_guard = getattr(actor, '_naksha_rgb_ptr', None)
    if rgb_guard is not None:
        actor_valid = local_indices < len(rgb_guard)
        local_indices = local_indices[actor_valid]
        reverted_cls = reverted_cls[actor_valid]
        if local_indices.size == 0:
            return

    # Guard slot-0 RGB/class-buffer patching when main view is not class/shaded_class.
    display_mode = str(getattr(app, "display_mode", "class") or "class").lower()
    if slot_idx > 0 or display_mode in ("class", "shaded_class"):
        if hasattr(actor, "_naksha_section_class"):
            actor._naksha_section_class[local_indices] = reverted_cls

        mesh = getattr(actor, '_naksha_mesh', None)
        if mesh is not None:
            class_vtk = mesh.GetPointData().GetArray("Classification")
            if class_vtk is not None:
                numpy_support.vtk_to_numpy(class_vtk)[local_indices] = (
                    reverted_cls.astype(np.float32))
                class_vtk.Modified()

        rgb_ptr = getattr(actor, "_naksha_rgb_ptr", None)
        if rgb_ptr is not None and _is_writable(rgb_ptr):
            # Keep main-view partial updates aligned with the canonical main palette.
            # This avoids per-stroke color drift when slot-0 dialog overrides lag.
            if slot_idx == 0:
                palette = getattr(app, "class_palette", {}) or _get_slot_palette(app, 0)
            else:
                palette = _get_slot_palette(app, slot_idx)
            max_hint  = max(int(reverted_cls.max()) + 1, 256) if reverted_cls.size else 256
            lut_obj   = _get_lut(f"undo_{slot_idx}")
            color_lut = lut_obj.build(palette, max_hint)
            rgb_ptr[local_indices] = color_lut[
                reverted_cls.clip(0, len(color_lut) - 1).astype(np.intp)]

            vtk_ca = getattr(actor, "_naksha_vtk_array", None)
            if vtk_ca is not None:
                vtk_ca.Modified()

        _mark_actor_dirty(actor)

def _safe_widget_render(vtk_widget) -> bool:
    """Best-effort safe render for PyVista/VTK widgets."""
    if vtk_widget is None:
        return False
    try:
        if hasattr(vtk_widget, "isVisible") and callable(vtk_widget.isVisible):
            if not vtk_widget.isVisible():
                return False
        rw = vtk_widget.GetRenderWindow()
        if rw is None or rw.GetInteractor() is None:
            return False
        vtk_widget.render()
        return True
    except (RuntimeError, AttributeError, OSError, ReferenceError):
        return False
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# ACTOR LOOKUP HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def is_unified_actor_ready(app) -> bool:
    return _get_unified_actor(app) is not None


def _expected_main_actor_point_count(current_n: int, app=None) -> int:
    """Use the same flight-filter/full-resolution/LOD contract as the builder."""
    try:
        from gui.optimization_config import MAIN_VIEW_RENDER_ALL_POINTS
    except Exception:
        MAIN_VIEW_RENDER_ALL_POINTS = True
    current_n = max(0, int(current_n))
    effective_n = current_n
    if MAIN_VIEW_RENDER_ALL_POINTS:
        return effective_n
    target_pts = 10_000_000
    lod_step = max(1, current_n // target_pts) if current_n > target_pts else 1
    return int(np.ceil(effective_n / lod_step))


def _get_unified_actor(app) -> Optional[object]:
    # Block all callers while build_unified_actor is running
    if getattr(app, '_unified_actor_building', False):
        return None
    plotter = getattr(app, "vtk_widget", None)
    actor = getattr(app, "_unified_actor", None)
    # A renderer clear can detach the VTK actor while its Python reference
    # remains alive. Do not write palette data into that orphan actor.
    if actor is not None:
        registered = (
            getattr(plotter, "actors", {}).get(UNIFIED_ACTOR_NAME)
            if plotter is not None else None
        )
        if registered is not actor:
            actor = None
            app._unified_actor = None
    if actor is not None:
        try:
            actor.GetVisibility()
            rgb = getattr(actor, "_naksha_rgb_ptr", None)
            if rgb is not None and _is_writable(rgb):
                if hasattr(app, 'data') and app.data is not None:
                    xyz = app.data.get('xyz')
                    if xyz is not None:
                        current_data_id = id(xyz)
                        actor_data_id   = getattr(actor, '_naksha_data_id', None)
                        if actor_data_id is not None and actor_data_id != current_data_id:
                            print(f"   DEBUG: _get_unified_actor STALE - Data changed (ID mismatch)")
                            return None

                        current_n = len(xyz)
                        expected_actor_n = _expected_main_actor_point_count(current_n, app)

                        from gui.flight_line_filter import flight_line_visibility_signature
                        current_line_sig = flight_line_visibility_signature(app)
                        actor_line_sig = getattr(actor, '_naksha_flight_line_signature', ())
                        if actor_line_sig != current_line_sig:
                            # FAST LINE MODE: flight-line selection is shader visibility,
                            # not a geometry change. Patch the existing FlightVisible
                            # VTK array in place instead of rebuilding millions of points.
                            if not fast_main_flight_line_visibility_update(app, render=False):
                                print(
                                    "   DEBUG: _get_unified_actor STALE - "
                                    "flight-line selection changed and in-place patch failed"
                                )
                                return None
                            print("   ⚡ _get_unified_actor: flight-line visibility patched in place")

                        actor_n = getattr(actor, '_naksha_point_count', 0)
                        if actor_n > 0 and actor_n != expected_actor_n:
                            print(f"   DEBUG: _get_unified_actor STALE - actor_n={actor_n} != expected={expected_actor_n}")
                            return None
                return actor
            else:
                if rgb is None:
                    print(f"   DEBUG: _get_unified_actor FAILED - _naksha_rgb_ptr is None")
                elif not _is_writable(rgb):
                    print(f"   DEBUG: _get_unified_actor FAILED - _naksha_rgb_ptr is NOT writable")
        except (AttributeError, RuntimeError):
            try:
                del app._unified_actor
            except AttributeError:
                pass

    if plotter is None:
        return None

    if UNIFIED_ACTOR_NAME in plotter.actors:
        actor = plotter.actors[UNIFIED_ACTOR_NAME]
        try:
            actor.GetVisibility()
            rgb = getattr(actor, "_naksha_rgb_ptr", None)
            if rgb is not None and _is_writable(rgb):
                if hasattr(app, 'data') and app.data is not None:
                    xyz = app.data.get('xyz')
                    if xyz is not None:
                        current_n = len(xyz)
                        expected_actor_n = _expected_main_actor_point_count(current_n, app)
                        from gui.flight_line_filter import flight_line_visibility_signature
                        if getattr(actor, '_naksha_flight_line_signature', ()) != flight_line_visibility_signature(app):
                            # Same rule as the cached app actor above: line selection
                            # changes only FlightVisible, so keep the geometry alive.
                            app._unified_actor = actor
                            if not fast_main_flight_line_visibility_update(app, render=False):
                                return None
                        actor_n = getattr(actor, '_naksha_point_count', 0)
                        if actor_n > 0 and actor_n != expected_actor_n:
                            return None

                app._unified_actor = actor
                return actor
        except (AttributeError, RuntimeError):
            pass

    return None


def _get_all_unified_actors(app):
    main_actor = _get_unified_actor(app)
    vtk_widget = getattr(app, "vtk_widget", None)
    if main_actor is not None and vtk_widget is not None:
        yield (main_actor, 0, getattr(app, "_main_global_indices", None), vtk_widget)

    if hasattr(app, "section_vtks"):
        for view_idx, widget in app.section_vtks.items():
            actor_name = f"_section_{view_idx}_unified"
            if actor_name in widget.actors:
                actor = widget.actors[actor_name]
                rgb   = getattr(actor, "_naksha_rgb_ptr", None)
                if rgb is not None and _is_writable(rgb):
                    gi = getattr(app, f"_section_{view_idx}_global_indices", None)
                    yield (actor, view_idx + 1, gi, widget)


_palette_resolve_cache: dict = {}


def _get_slot_palette(app, slot_idx: int) -> dict:
    """
    Fingerprint-guarded palette resolver.
    Returns a cached resolved dict when master + overrides have not changed.
    """
    master = getattr(app, "class_palette", {}) or {}
    if not master:
        return {}

    overrides = {}
    dlg = getattr(app, "display_mode_dialog", None)
    if dlg and hasattr(dlg, "view_palettes"):
        vp = getattr(dlg, "view_palettes", None)
        if vp and slot_idx in vp:
            overrides = vp[slot_idx] or {}
    if not overrides:
        vp = getattr(app, "view_palettes", {})
        overrides = vp.get(slot_idx, {}) or {}

    try:
        fp = (
            hash(tuple(
                (k, v.get("show", True), v.get("weight", 1.0),
                 tuple(v.get("color", (128, 128, 128))))
                for k, v in sorted(master.items())
            )),
            hash(tuple(
                (k, v.get("show", True), v.get("weight", 1.0),
                 tuple(v.get("color", (128, 128, 128))))
                for k, v in sorted(overrides.items())
            )) if overrides else 0,
        )
    except Exception:
        fp = (id(master), id(overrides))

    cached = _palette_resolve_cache.get(slot_idx)
    if cached is not None and cached[0] == fp:
        return cached[1]

    if not overrides:
        if slot_idx == 0:
            result = master
        else:
            # Keep cross/cut slots isolated: inherit color/weight only,
            # never slot-0 visibility state.
            result = {}
            for code, info in master.items():
                if not isinstance(info, dict):
                    continue
                row = dict(info)
                row["show"] = True
                row["color"] = tuple(row.get("color", (128, 128, 128)))
                row["weight"] = float(row.get("weight", 1.0))
                row["description"] = str(row.get("description", ""))
                result[code] = row
    else:
        # Slot 0 keeps canonical behavior (master + full override validation).
        if slot_idx == 0:
            if len(overrides) != len(master):
                print(f"   [WARN] _get_slot_palette: slot 0 override count mismatch "
                      f"({len(overrides)} vs master {len(master)}) - ignoring stale override")
                result = master
            else:
                result = {code: dict(info) for code, info in master.items()}
                for code, info in overrides.items():
                    if code in result:
                        result[code].update(info)
                    else:
                        result[code] = dict(info)
        else:
            # Slots 1..5 must NOT inherit slot-0 visibility for classes that are
            # absent in this slot override map. Seed with show=True, then apply
            # slot-local override visibility/weight/color.
            result = {}
            for code, info in master.items():
                if not isinstance(info, dict):
                    continue
                row = dict(info)
                row["show"] = True
                row["color"] = tuple(row.get("color", (128, 128, 128)))
                row["weight"] = float(row.get("weight", 1.0))
                row["description"] = str(row.get("description", ""))
                result[code] = row

            for code, info in overrides.items():
                if code in result:
                    result[code].update(info)
                else:
                    result[code] = dict(info)

    _palette_resolve_cache[slot_idx] = (fp, result)
    return result

def invalidate_palette_cache(slot_idx: int = None):
    """
    ✅ FIX: Bust the _palette_resolve_cache for one or all slots.
    Call this whenever class_palette colors are changed (PTC color edit) so that
    _get_slot_palette does not serve the stale fingerprint-cached palette.

    Args:
        slot_idx: specific slot to bust (0-5), or None to bust all slots.
    """
    global _palette_resolve_cache
    if slot_idx is None:
        _palette_resolve_cache.clear()
    else:
        _palette_resolve_cache.pop(slot_idx, None)


def invalidate_unified_actor(app):
    """
    Null out any cached references to the unified actor and its indices on the app.
    """
    for attr in [
        "_unified_actor",
        "_main_interaction_actor",
        "_main_global_mask",
        "_main_global_indices",
        "_rgb_buffer",
    ]:
        if hasattr(app, attr):
            try:
                setattr(app, attr, None)
                # print(f"      - {attr} cleared")
            except Exception:
                pass
    print("   🧹 Unified actor references invalidated")

def fast_main_flight_line_visibility_update(app, render: bool = True) -> bool:
    """
    Patch Main View flight visibility in place without rebuilding geometry.

    ``render=False`` is used by mode-switch code to batch FlightVisible,
    palette/uniform, and RGB changes into one final render. Existing callers
    retain the old immediate-render behavior because the default is True.
    """
    t0 = time.perf_counter()
    actor = getattr(app, "_unified_actor", None)
    plotter = getattr(app, "vtk_widget", None)
    if actor is None and plotter is not None:
        actor = getattr(plotter, "actors", {}).get(UNIFIED_ACTOR_NAME)
    if actor is None:
        return False

    mesh = getattr(actor, "_naksha_mesh", None)
    if mesh is None:
        try:
            mesh = actor.GetMapper().GetInput()
        except Exception:
            return False
    arr = mesh.GetPointData().GetArray("FlightVisible") if mesh is not None else None
    if arr is None:
        return False

    data = getattr(app, "data", None)
    xyz = data.get("xyz") if isinstance(data, dict) else None
    if xyz is None:
        return False

    from gui.flight_line_filter import (
        flight_line_visibility_mask,
        flight_line_visibility_signature,
    )
    full_mask = flight_line_visibility_mask(app, len(xyz), slot=0)
    global_indices = getattr(app, "_main_global_indices", None)
    local_mask = full_mask if global_indices is None else full_mask[global_indices]
    vtk_values = numpy_support.vtk_to_numpy(arr)
    if len(vtk_values) != len(local_mask):
        return False

    np.copyto(vtk_values, local_mask, casting="unsafe")
    arr.Modified()
    mesh.Modified()
    actor._naksha_flight_line_signature = flight_line_visibility_signature(app, 0)
    actor._naksha_flight_np_ref = vtk_values
    actor._naksha_flight_vtk = arr
    actor.SetVisibility(1)
    if render and plotter is not None:
        plotter.render()
    elapsed = (time.perf_counter() - t0) * 1000.0
    print(
        f"   Main flight-line GPU visibility: "
        f"{int(np.count_nonzero(local_mask)):,}/{len(local_mask):,} pts [{elapsed:.1f} ms]"
    )
    return True


def reset_uam(app):
    """
    Global reset for Unified Actor Manager. 
    Wipes all cached shader contexts, LUTs, and actor references.
    Call this when clearing a project or switching datasets.
    """
    global _shader_contexts, _lut_cache, _palette_resolve_cache
    _shader_contexts.clear()
    _lut_cache.clear()
    if "_palette_resolve_cache" in globals():
        _palette_resolve_cache.clear()
    
    invalidate_unified_actor(app)
    
    # Also clear any section-specific global indices
    for i in range(4):
        attr = f"_section_{i}_global_indices"
        if hasattr(app, attr):
            setattr(app, attr, None)
            
    print("   ✨ Unified Actor Manager state fully reset")


def _ensure_opengl_polydata_mapper(actor, cloud, use_spheres=True):
    if actor is None:
        return
    raw_mapper = actor.GetMapper()
    if hasattr(raw_mapper, 'GetMapper'):
        raw_mapper = raw_mapper.GetMapper()
    if not hasattr(raw_mapper, 'MapDataArrayToVertexAttribute'):
        new_mapper = vtk.vtkOpenGLPolyDataMapper()
        new_mapper.SetInputData(cloud)
        new_mapper.SetScalarModeToUsePointFieldData()
        new_mapper.SelectColorArray("RGB")
        new_mapper.SetScalarVisibility(raw_mapper.GetScalarVisibility())
        actor.SetMapper(new_mapper)
        print("      ℹ️ Swapped vtkDataSetMapper → vtkOpenGLPolyDataMapper")


def compute_point_size(weight: float, base: float = _BASE_POINT_SIZE) -> float:
    min_size = max(0.5, base * 0.1)
    return float(max(min_size, min(base * weight, 30.0)))

def _compute_boundary_flags(xyz: np.ndarray, classification: np.ndarray = None,
                             resolution: float = 0.0) -> np.ndarray:
    """
    Computes per-point structural boundary flags using three criteria:
      1. Height discontinuity  — main structural edge detector (roof→ground drop)
      2. Class-change          — only when neighbour cell is OCCUPIED + different class
      3. Scan-edge             — point has < 4 of 8 occupied neighbours (true cloud edge)

    Empty neighbour cells are NOT treated as boundary — this prevents dark-roof
    artefacts where interior points with scan gaps were wrongly flagged.
    """
    n = len(xyz)
    if n == 0:
        return np.zeros(0, dtype=np.float32)

    xy    = xyz[:, :2]
    z_arr = xyz[:, 2].astype(np.float64)

    xy_min = xy.min(axis=0)
    xy_max = xy.max(axis=0)

    if resolution <= 0.0:
        w     = max(float(xy_max[0] - xy_min[0]), 1.0)
        h_dim = max(float(xy_max[1] - xy_min[1]), 1.0)
        resolution = max(0.05, np.sqrt(w * h_dim / n) * 1.5)

    gc = ((xy - xy_min) / resolution).astype(np.int32) + 1
    gx, gy   = gc[:, 0], gc[:, 1]
    gx_max   = int(gx.max()) + 2
    gy_max   = int(gy.max()) + 2

    z_sum   = np.zeros((gx_max, gy_max), dtype=np.float64)
    z_count = np.zeros((gx_max, gy_max), dtype=np.int32)
    np.add.at(z_sum,   (gx, gy), z_arr)
    np.add.at(z_count, (gx, gy), 1)
    z_mean = np.where(z_count > 0, z_sum / np.maximum(z_count, 1), 0.0)

    my_z_cell = z_mean[gx, gy]

    z_global_range = max(float(z_arr.max() - z_arr.min()), 1.0)
    z_thresh = max(0.5, z_global_range * 0.03)

    has_class = classification is not None
    if has_class:
        class_grid = np.full((gx_max, gy_max), -1, dtype=np.int32)
        class_grid[gx, gy] = classification.astype(np.int32)
        my_class = classification.astype(np.int32)

    _DIRS = [(-1, -1), (-1, 0), (-1, 1),
             ( 0, -1),          ( 0, 1),
             ( 1, -1), ( 1, 0), ( 1, 1)]

    boundary     = np.zeros(n, dtype=bool)
    occ_nb_count = np.zeros(n, dtype=np.int32)

    for dx, dy in _DIRS:
        nx, ny = gx + dx, gy + dy
        nb_occ = (z_count[nx, ny] > 0)
        occ_nb_count += nb_occ.astype(np.int32)

        nb_z   = z_mean[nx, ny]
        z_diff = np.where(nb_occ, np.abs(my_z_cell - nb_z), 0.0)
        boundary |= nb_occ & (z_diff > z_thresh)

        if has_class:
            nb_cls = class_grid[nx, ny]
            boundary |= nb_occ & (nb_cls >= 0) & (nb_cls != my_class)

    boundary |= (occ_nb_count < 4)

    pct = int(boundary.sum()) * 100 // max(n, 1)
    print(f"      🔲 BoundaryFlag: {int(boundary.sum()):,}/{n:,} edge pts "
          f"({pct}%, z_thresh={z_thresh:.2f}m, res={resolution:.3f}m)")

    return boundary.astype(np.float32)


def diagnose_weight_pipeline(app, slot_idx=1):
    print("\n" + "=" * 70)
    print(f"🔬 WEIGHT PIPELINE DIAGNOSTIC  (slot={slot_idx})")
    print("=" * 70)

    if slot_idx == 0:
        actor_name = UNIFIED_ACTOR_NAME
        vtk_widget = getattr(app, "vtk_widget", None)
    else:
        view_idx   = slot_idx - 1
        actor_name = f"_section_{view_idx}_unified"
        vtk_widget = (app.section_vtks.get(view_idx)
                      if hasattr(app, "section_vtks") else None)

    print(f"\n[1] Actor: {actor_name}")
    if vtk_widget is None:
        print("    ❌ No vtk_widget"); return

    actor = vtk_widget.actors.get(actor_name) if hasattr(vtk_widget, 'actors') else None
    if actor is None:
        print("    ❌ Actor not found"); return

    ctx = getattr(actor, '_naksha_shader_ctx', None)
    print(f"\n[2] shader_ctx: {'✅' if ctx else '❌ None'}")
    if ctx:
        print(f"    _has_vertex_attr_cache:  {ctx._has_vertex_attr_cache}")
        print(f"    _naksha_base_point_size: {getattr(actor, '_naksha_base_point_size', '?')}")
        print(f"    _naksha_pps_observer_installed: {getattr(actor, '_naksha_pps_observer_installed', False)}")
        print(f"    structured_border_mode:  {ctx.structured_border_mode}  "
              f"(0=per-point, 1=structured, 2=hybrid)")
        for c in [2, 6, 7]:
            print(f"    class {c}: weight_lut={ctx.weight_lut[c]:.2f}  "
                  f"vis={ctx.visibility_mask[c]:.1f}")

    sp = actor.GetShaderProperty() if actor else None
    print(f"\n[3] ShaderProperty: {'✅' if sp else '❌'}")
    if sp:
        v_uni = sp.GetVertexCustomUniforms()
        f_uni = sp.GetFragmentCustomUniforms()
        print(f"    VertexCustomUniforms:   {'✅' if v_uni else '❌'}")
        print(f"    FragmentCustomUniforms: {'✅' if f_uni else '❌'}")
        print(f"    _shaders_finalized_v27:  {getattr(actor, '_shaders_finalized_v27', False)}")
