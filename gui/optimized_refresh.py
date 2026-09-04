"""
Optimized refresh pipeline for classification updates.

This module keeps the existing optimizer entry points intact while ensuring
cross-section visibility is always re-evaluated after class transitions.
"""

import time
from typing import List, Optional, Set

import numpy as np

from .classification_state import get_dirty_state, get_weight_cache
from .optimization_config import (
    ENABLE_BATCHED_RENDERING,
    ENABLE_DELTA_WEIGHT_SYNC,
    ENABLE_DIRTY_VIEW_TRACKING,
    ENABLE_OPTIMIZED_REFRESH,
    ENABLE_PERFORMANCE_LOGGING,
)


class OptimizedRefreshPipeline:
    """Optimized refresh pipeline that wraps existing view-refresh logic."""

    def __init__(self, app):
        self.app = app
        self._pending_renders: Set[int] = set()
        self._start_time: float = 0.0

    def refresh_after_classification(
        self,
        to_class: int,
        from_classes: Optional[List[int]] = None,
        active_view: Optional[int] = None,
        fallback_func=None,
    ):
        """
        Optimized refresh with automatic fallback.

        Fast injection may already update GPU colors, but class transitions
        still need per-view visibility refresh for section views.
        """
        if getattr(self.app, "_gpu_sync_done", False):
            needs_post_refresh = bool(
                getattr(self.app, "_section_visibility_refresh_required", False)
            )
            helper_rendered = False
            try:
                from gui.unified_actor_manager import guarantee_main_view_visual_refresh
                helper_rendered = bool(guarantee_main_view_visual_refresh(
                    self.app,
                    changed_mask=getattr(self.app, "_last_changed_mask", None),
                    to_class=to_class,
                    reason="optimized_refresh_fastpath",
                ))
            except Exception:
                pass

            self.app._gpu_sync_done = False
            self.app._section_visibility_refresh_required = False

            if helper_rendered and not needs_post_refresh:
                if ENABLE_PERFORMANCE_LOGGING:
                    print("⏭️ OptimizedRefresh skipped — helper flush completed")
                return

            if not needs_post_refresh:
                # The fast classification path may already have updated the
                # underlying GPU buffers, but the main view still needs a
                # guaranteed render flush so the Qt/VTK throttle cannot strand
                # it in a visually stale state.
                self._pending_renders.add(0)
                self._batched_render()
                if ENABLE_PERFORMANCE_LOGGING:
                    print("⏭️ OptimizedRefresh skipped — fast path already completed")
                return

            changed_mask = getattr(self.app, "_last_changed_mask", None)
            has_changed = (
                isinstance(changed_mask, np.ndarray)
                and changed_mask.size > 0
                and np.any(changed_mask)
            )

            if has_changed:
                changed_count = int(np.count_nonzero(changed_mask))
                print("[CLASS-COMMIT]")
                print(f"  changed_count={changed_count}")
                print(f"  to_class={to_class}")
                print("⚠️ Fast GPU sync done, but refreshing cross-section visibility")
                self._refresh_cross_sections_after_class_change(changed_mask, to_class)
                self._refresh_cut_section_after_class_change(changed_mask, to_class)
                self._batched_render()
            elif ENABLE_PERFORMANCE_LOGGING:
                print("⏭️ OptimizedRefresh skipped — no changed mask")
            return

        if not ENABLE_OPTIMIZED_REFRESH:
            if fallback_func:
                return fallback_func(to_class)
            return

        self._start_time = time.time()
        try:
            self._do_optimized_refresh(to_class, from_classes, active_view)
            if ENABLE_PERFORMANCE_LOGGING:
                elapsed = (time.time() - self._start_time) * 1000
                print(f"⚡ Optimized refresh: {elapsed:.0f}ms")
        except Exception as e:
            print(f"⚠️ Optimized refresh failed: {e}")
            import traceback

            traceback.print_exc()
            if fallback_func:
                fallback_func(to_class)

    def _do_optimized_refresh(self, to_class, from_classes, active_view):
        """Internal optimized refresh logic."""
        state = get_dirty_state()
        if not state.begin_refresh():
            print("⏭️ Refresh already in progress")
            return

        self.app._optimized_refresh_active = True
        try:
            state.mark_classes_dirty(to_class=to_class, from_classes=from_classes)

            changed_mask = getattr(self.app, "_last_changed_mask", None)
            if changed_mask is not None:
                state.set_changed_mask(changed_mask)

            state.mark_view_dirty(0)

            if hasattr(self.app, "section_vtks") and self.app.section_vtks:
                for view_idx in self.app.section_vtks.keys():
                    state.mark_view_dirty(view_idx + 1)

            if active_view is not None and active_view >= 0:
                state.mark_view_dirty(active_view + 1)

            if hasattr(self.app, "cut_section_controller") and self.app.cut_section_controller:
                if self.app.cut_section_controller.is_cut_view_active:
                    state.mark_view_dirty(5)

            self._weights_changed_this_cycle = False
            if ENABLE_DELTA_WEIGHT_SYNC:
                self._sync_weights_delta()
            else:
                self._sync_weights_full()
                self._weights_changed_this_cycle = True

            if ENABLE_DIRTY_VIEW_TRACKING:
                self._refresh_dirty_cross_sections(state)

            self._refresh_main_view_for_classification(to_class, state)
            self._pending_renders.add(0)

            # Rebuild any dirty section's Shaded/Surface mesh AFTER Main
            # View's own shading refresh above, not before. This step used
            # to live inside fast_cross_section_update (called earlier, by
            # _refresh_dirty_cross_sections at line ~154), which ran BEFORE
            # _refresh_main_view_for_classification -> _refresh_shaded_mode
            # patches Main View's shaded mesh's colors -- so a section
            # reusing Main View's mesh as its source captured a stale
            # pre-patch snapshot every time, then never updated again.
            # Placing it here, after Main View's own patch has already
            # applied, ensures the section rebuild reads fresh colors.
            if ENABLE_DIRTY_VIEW_TRACKING:
                self._refresh_dirty_section_meshes(state)

            if 5 in state.dirty_views:
                self._refresh_cut_section()
                self._pending_renders.add(5)

            if ENABLE_BATCHED_RENDERING:
                self._batched_render()
            else:
                self._individual_renders()

            if ENABLE_PERFORMANCE_LOGGING:
                self._update_statistics()
                self._show_status(to_class)
        finally:
            state.end_refresh()
            state.clear()
            from PySide6.QtCore import QTimer

            QTimer.singleShot(100, lambda: setattr(self.app, "_optimized_refresh_active", False))

    def _sync_weights_delta(self):
        """Delta sync only for slot 0 weights/colors/show flags."""
        weight_cache = get_weight_cache()
        dialog = getattr(self.app, "display_mode_dialog", None)
        if not dialog or not hasattr(dialog, "view_palettes"):
            return

        if 0 in dialog.view_palettes:
            if not hasattr(self.app, "class_palette"):
                self.app.class_palette = {}
            for cls, info in dialog.view_palettes[0].items():
                if cls in self.app.class_palette:
                    self.app.class_palette[cls]["weight"] = info.get("weight", 1.0)
                    self.app.class_palette[cls]["color"] = info.get("color", (128, 128, 128))
                    self.app.class_palette[cls]["show"] = info.get("show", True)
                else:
                    self.app.class_palette[cls] = {
                        "weight": info.get("weight", 1.0),
                        "color": info.get("color", (128, 128, 128)),
                        "show": info.get("show", True),
                        "description": info.get("description", f"Class {cls}"),
                    }

        if not weight_cache.has_changes(dialog.view_palettes):
            return
        changes = weight_cache.get_changed_weights(dialog.view_palettes)
        if not changes:
            return
        if 0 in changes:
            self._weights_changed_this_cycle = True
            self._pending_renders.add(0)
        weight_cache.update_cache(dialog.view_palettes)

    def _sync_weights_full(self):
        if hasattr(self.app, "interactor") and hasattr(self.app.interactor, "_sync_main_view_palette_weights"):
            self.app.interactor._sync_main_view_palette_weights()

    def _refresh_dirty_cross_sections(self, state):
        cross_section_slots = [v for v in state.dirty_views if 1 <= v <= 4]

        changed_mask = getattr(self.app, "_last_changed_mask", None)
        if (
            isinstance(changed_mask, np.ndarray)
            and changed_mask.size > 0
            and np.any(changed_mask)
            and hasattr(self.app, "section_vtks")
            and self.app.section_vtks
        ):
            for view_idx in self.app.section_vtks.keys():
                slot_idx = view_idx + 1
                if slot_idx not in cross_section_slots:
                    cross_section_slots.append(slot_idx)

        if not cross_section_slots:
            return

        for slot_idx in cross_section_slots:
            view_idx = slot_idx - 1
            if not hasattr(self.app, "section_vtks") or view_idx not in self.app.section_vtks:
                continue
            self._refresh_single_cross_section(view_idx, state)
            self._pending_renders.add(slot_idx)

    def _refresh_dirty_section_meshes(self, state):
        """Rebuild every open section's Shaded Classification/Surface mesh
        after a classify action, via the shared
        refresh_all_shaded_surface_sections_after_classify (also called
        from interactor_classify.py's _refresh_all_views_after_classification
        for the cross-section-targeted classify path, and centrally from
        guarantee_main_view_visual_refresh -- kept here too as a fallback
        for whatever cases reach this optimizer pipeline instead). Must run
        AFTER _refresh_main_view_for_classification so a section reusing
        Main View's mesh as its source picks up Main View's just-patched
        colors instead of a stale pre-patch snapshot.
        """
        del state
        try:
            from gui.cross_section.section_shaded_surface import (
                refresh_all_shaded_surface_sections_after_classify,
            )
            refresh_all_shaded_surface_sections_after_classify(self.app)
        except Exception as mesh_refresh_err:
            print(f"   ⚠️ Section Shaded/Surface refresh-after-classify "
                f"failed: {mesh_refresh_err}")

    def _refresh_single_cross_section(self, view_idx, state):
        del state
        if not hasattr(self.app, "section_vtks") or view_idx not in self.app.section_vtks:
            return

        vtk_widget = self.app.section_vtks[view_idx]
        slot_idx = view_idx + 1

        current_view_mode = getattr(self.app, "cross_view_mode", "side")
        if not hasattr(self, "_last_view_modes"):
            self._last_view_modes = {}
        last_view_mode = self._last_view_modes.get(view_idx)

        if last_view_mode is not None and last_view_mode != current_view_mode:
            self._last_view_modes[view_idx] = current_view_mode
            try:
                core_pts = getattr(self.app, f"section_{view_idx}_core_points", None)
                buffer_pts = getattr(self.app, f"section_{view_idx}_buffer_points", None)
                if core_pts is not None:
                    old_active = self.app.section_controller.active_view
                    self.app.section_controller.active_view = view_idx
                    self.app.section_controller.current_vtk = vtk_widget
                    self.app.section_controller._plot_section(core_pts, buffer_pts, view=current_view_mode)
                    self.app.section_controller.active_view = old_active
                    return
            except Exception:
                pass
        else:
            self._last_view_modes[view_idx] = current_view_mode

        try:
            from gui.unified_actor_manager import fast_cross_section_update, build_section_unified_actor

            changed_mask = getattr(self.app, "_last_changed_mask", None)
            palette = self._get_view_palette(slot_idx)
            ok = fast_cross_section_update(
                self.app,
                view_idx,
                changed_mask,
                palette=palette,
                skip_render=True,
                force_visibility_refresh=True,
            )
            if (not ok) and self._section_view_has_data(view_idx):
                border = float(getattr(self.app, "view_borders", {}).get(slot_idx, 0) or 0.0)
                view_mode = getattr(self.app, "cross_view_mode", "front")
                rebuilt = build_section_unified_actor(
                    self.app,
                    view_idx,
                    palette=palette,
                    border_percent=border,
                    view=view_mode,
                )
                if rebuilt is not None:
                    fast_cross_section_update(
                        self.app,
                        view_idx,
                        changed_mask,
                        palette=palette,
                        skip_render=True,
                        force_visibility_refresh=True,
                    )
        except Exception as e:
            print(f"      ⚠️ Unified cross-section update failed: {e}")

    def _refresh_main_view_for_classification(self, to_class, state):
        del state
        app = self.app
        display_mode = getattr(app, "display_mode", "class")

        if display_mode == "class":
            try:
                from gui.unified_actor_manager import fast_classify_update, is_unified_actor_ready

                if is_unified_actor_ready(app):
                    changed_mask = getattr(app, "_last_changed_mask", None)
                    if changed_mask is not None and np.any(changed_mask):
                        done = fast_classify_update(
                            app,
                            changed_mask=changed_mask,
                            to_class=to_class,
                            palette=getattr(app, "class_palette", {}),
                            border_percent=float(getattr(app, "point_border_percent", 0) or 0.0),
                            skip_render=True,
                        )
                        if done:
                            self._pending_renders.add(0)
                            return
            except Exception as e:
                print(f"   ⚠️ fast_classify_update failed: {e}")

        if display_mode == "class":
            # NOTE: `state` was deleted above; _refresh_main_view_class_mode
            # discards it anyway (`del to_class, state`), so pass None to
            # avoid UnboundLocalError on the first classification (before the
            # unified actor exists and the fast path returns early).
            self._refresh_main_view_class_mode(to_class, None)
        elif display_mode == "shaded_class":
            self._refresh_shaded_mode()
        else:
            # We are in depth, intensity, elevation, or rgb mode.
            # Do NOT call update_pointcloud as it is slow and would apply visibility filters or color changes.
            # Just keep the main view visualization completely intact.
            print(f"   📊 Mode '{display_mode}' – keeping main view completely intact (skipping pointcloud rebuild)...")

    def _refresh_main_view_class_mode(self, to_class, state):
        del to_class, state
        app = self.app
        plotter = getattr(app, "vtk_widget", None)
        if plotter is None:
            return
        try:
            from gui.pointcloud_display import update_pointcloud

            update_pointcloud(app, "class")
            self._pending_renders.add(0)
        except Exception:
            pass

    def _refresh_shaded_mode(self):
        try:
            from gui.shading_display import refresh_shaded_after_classification_fast

            changed_mask = getattr(self.app, "_last_changed_mask", None)
            refresh_shaded_after_classification_fast(self.app, changed_mask)
        except Exception:
            pass

    def _refresh_other_mode(self, display_mode):
        try:
            from gui.pointcloud_display import update_pointcloud

            update_pointcloud(self.app, display_mode)
        except Exception:
            pass

    def _refresh_cut_section(self):
        ctrl = getattr(self.app, "cut_section_controller", None)
        if not ctrl or not ctrl.is_cut_view_active:
            return
        try:
            ctrl._refresh_cut_colors_fast()
            ctrl.cut_vtk.render()
        except Exception:
            pass

    def _section_view_has_data(self, view_idx: int) -> bool:
        """
        Refresh eligibility for synced section views.
        A blank (0-visible) view is still refreshable if section data exists.
        """
        app = self.app
        if not hasattr(app, "section_vtks") or view_idx not in app.section_vtks:
            return False

        pts = getattr(app, f"section_{view_idx}_points_transformed", None)
        if pts is not None and len(pts) > 0:
            return True

        global_indices = getattr(app, f"_section_{view_idx}_global_indices", None)
        if global_indices is not None and len(global_indices) > 0:
            return True

        combined_mask = getattr(app, f"section_{view_idx}_combined_mask", None)
        if (
            isinstance(combined_mask, np.ndarray)
            and combined_mask.size > 0
            and np.any(combined_mask)
        ):
            return True

        vtk_widget = app.section_vtks.get(view_idx)
        if vtk_widget is not None and hasattr(vtk_widget, "actors"):
            actor_name = f"_section_{view_idx}_unified"
            if actor_name in vtk_widget.actors:
                return True

        return False

    def _refresh_cross_section_visibility(self, view_idx: int, changed_mask, to_class: int) -> bool:
        """
        Re-evaluate per-view visibility after class changes, even from blank state.
        """
        app = self.app
        if not self._section_view_has_data(view_idx):
            print(f"[REFRESH-SKIP] View {view_idx + 1}: no section data")
            return False

        if not hasattr(app, "data") or app.data is None or "classification" not in app.data:
            print(f"[REFRESH-SKIP] View {view_idx + 1}: no classification data")
            return False

        slot_idx = view_idx + 1
        palette = self._get_view_palette(slot_idx) or {}

        global_indices = getattr(app, f"_section_{view_idx}_global_indices", None)
        if global_indices is None or len(global_indices) == 0:
            combined_mask = getattr(app, f"section_{view_idx}_combined_mask", None)
            if (
                isinstance(combined_mask, np.ndarray)
                and combined_mask.dtype == bool
                and combined_mask.size == len(app.data["classification"])
            ):
                global_indices = np.flatnonzero(combined_mask)
                setattr(app, f"_section_{view_idx}_global_indices", global_indices)

        if global_indices is None or len(global_indices) == 0:
            print(f"[REFRESH-SKIP] View {view_idx + 1}: no global index map")
            return False

        section_classes = app.data["classification"][global_indices]

        old_visible_count = -1
        vtk_widget = app.section_vtks.get(view_idx)
        actor_name = f"_section_{view_idx}_unified"
        actor = vtk_widget.actors.get(actor_name) if (vtk_widget is not None and hasattr(vtk_widget, "actors")) else None
        if actor is not None and hasattr(actor, "_naksha_section_class"):
            try:
                old_cls = actor._naksha_section_class
                if old_cls is not None and len(old_cls) == len(global_indices):
                    old_visible_count = int(np.count_nonzero([
                        palette.get(int(c), {}).get("show", True) for c in old_cls
                    ]))
            except Exception:
                old_visible_count = -1

        visible_mask = np.array(
            [palette.get(int(cls), {}).get("show", True) for cls in section_classes],
            dtype=bool,
        )
        visible_count = int(np.count_nonzero(visible_mask))
        show_to_class = bool((palette or {}).get(int(to_class), {}).get("show", True))

        print(
            f"[SYNC-REFRESH] View {view_idx + 1} slot={slot_idx} "
            f"section_points={len(global_indices)} "
            f"visible_before={old_visible_count} "
            f"visible_after_classification={visible_count} "
            f"to_class={to_class} to_class_visible={show_to_class}"
        )

        from gui.unified_actor_manager import fast_cross_section_update, build_section_unified_actor

        ok = fast_cross_section_update(
            app,
            view_idx,
            changed_mask,
            palette=palette,
            skip_render=True,
            force_visibility_refresh=True,
        )

        if not ok:
            try:
                border = float(getattr(app, "view_borders", {}).get(slot_idx, 0) or 0.0)
                view_mode = getattr(app, "cross_view_mode", "front")
                rebuilt = build_section_unified_actor(
                    app,
                    view_idx,
                    palette=palette,
                    border_percent=border,
                    view=view_mode,
                )
                if rebuilt is not None:
                    ok = fast_cross_section_update(
                        app,
                        view_idx,
                        changed_mask,
                        palette=palette,
                        skip_render=True,
                        force_visibility_refresh=True,
                    )
            except Exception as e:
                print(f"❌ View {view_idx + 1} rebuild failed during refresh: {e}")

        if ok:
            if old_visible_count == 0:
                print(
                    f"[SYNC-REFRESH] View {view_idx + 1} refreshed even though previous visible count was zero"
                )
            self._pending_renders.add(slot_idx)
            return True

        print(f"[REFRESH-SKIP] View {view_idx + 1}: refresh failed after visibility re-eval")
        return False

    def _refresh_cross_sections_after_class_change(self, changed_mask, to_class):
        app = self.app
        if not hasattr(app, "section_vtks") or not app.section_vtks:
            return

        for view_idx, vtk_widget in app.section_vtks.items():
            if vtk_widget is None:
                continue

            try:
                if hasattr(app, "_sync_section_mirror_from_data"):
                    app._sync_section_mirror_from_data(view_idx)

                ok = self._refresh_cross_section_visibility(view_idx, changed_mask, to_class)
                if ok:
                    print(f"[CLASS-REFRESH] View {view_idx + 1} updated Classification array")
                    print(f"[CLASS-REFRESH] View {view_idx + 1} updated RGB array")
                    print(f"[CLASS-REFRESH] View {view_idx + 1} visibility refreshed")
                else:
                    print(f"⚠️ [CLASS-REFRESH] View {view_idx + 1} fast refresh returned False")
            except Exception as e:
                print(f"❌ Failed cross-section visibility refresh for View {view_idx + 1}: {e}")
                import traceback

                traceback.print_exc()

    def _refresh_cut_section_after_class_change(self, changed_mask, to_class):
        del changed_mask, to_class
        ctrl = getattr(self.app, "cut_section_controller", None)
        if not ctrl or not getattr(ctrl, "is_cut_view_active", False):
            return
        try:
            ctrl._refresh_cut_colors_fast()
            self._pending_renders.add(5)
        except Exception as e:
            print(f"❌ Failed cut-section visibility refresh: {e}")

    def _get_view_palette(self, slot_idx):
        if hasattr(self.app, "display_mode_dialog") and self.app.display_mode_dialog:
            dialog = self.app.display_mode_dialog
            if hasattr(dialog, "view_palettes") and slot_idx in dialog.view_palettes:
                if dialog.view_palettes[slot_idx]:
                    return dialog.view_palettes[slot_idx]
        if hasattr(self.app, "view_palettes") and isinstance(self.app.view_palettes, dict):
            slot_palette = self.app.view_palettes.get(slot_idx)
            if slot_palette:
                return slot_palette
        return getattr(self.app, "class_palette", {}) if slot_idx == 0 else {}

    def _batched_render(self):
        if not self._pending_renders:
            return

        weights_changed = getattr(self, "_weights_changed_this_cycle", False)
        force_palette_sync = bool(getattr(self.app, "_force_palette_sync_in_refresh", False))

        if 0 in self._pending_renders:
            vtk_widget = getattr(self.app, "vtk_widget", None)
            if vtk_widget:
                try:
                    if weights_changed or force_palette_sync:
                        from gui.unified_actor_manager import sync_palette_to_gpu

                        sync_palette_to_gpu(self.app, 0, render=False)
                    vtk_widget.render()
                except Exception:
                    pass

        cross_section_slots = [v for v in self._pending_renders if 1 <= v <= 4]
        if cross_section_slots and hasattr(self.app, "section_vtks"):
            for slot_idx in cross_section_slots:
                view_idx = slot_idx - 1
                if view_idx not in self.app.section_vtks:
                    continue
                vtk_widget = self.app.section_vtks[view_idx]
                try:
                    if weights_changed or force_palette_sync:
                        from gui.unified_actor_manager import sync_palette_to_gpu

                        sync_palette_to_gpu(self.app, slot_idx, render=False)
                    vtk_widget.render()
                    print(f"[CLASS-REFRESH] View {view_idx + 1} rendered")
                except Exception:
                    pass

        if 5 in self._pending_renders:
            ctrl = getattr(self.app, "cut_section_controller", None)
            if ctrl and hasattr(ctrl, "cut_vtk"):
                try:
                    ctrl.cut_vtk.render()
                except Exception:
                    pass

        self._pending_renders.clear()
        if hasattr(self.app, "_force_palette_sync_in_refresh"):
            self.app._force_palette_sync_in_refresh = False

    def _individual_renders(self):
        self._batched_render()

    def _update_statistics(self):
        try:
            from gui.point_count_widget import refresh_point_statistics

            refresh_point_statistics(self.app)
        except Exception:
            pass

    def _show_status(self, to_class):
        del to_class
        try:
            changed_mask = getattr(self.app, "_last_changed_mask", None)
            num_changed = int(np.sum(changed_mask)) if changed_mask is not None else 0
            if hasattr(self.app, "statusBar"):
                self.app.statusBar().showMessage(f"✅ {num_changed:,} points classified", 3000)
        except Exception:
            pass


_optimizer: Optional[OptimizedRefreshPipeline] = None


def get_optimizer(app) -> OptimizedRefreshPipeline:
    global _optimizer
    if _optimizer is None or _optimizer.app != app:
        _optimizer = OptimizedRefreshPipeline(app)
    return _optimizer


def install_optimized_refresh(app):
    app._optimized_refresh = OptimizedRefreshPipeline(app)
    print("✅ Optimized refresh pipeline installed")
