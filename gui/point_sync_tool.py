"""
Point sync highlight tool.

Lets the user click a point in main, cross, or cut views and highlights the
same original dataset point across every open view that contains it.
"""

from PySide6.QtCore import QObject
import numpy as np
import vtk


class _TargetRingOverlay:
    """Screen-space target ring that tracks a world-space point."""

    RING_SEGMENTS = 40
    RING_RADIUS_PX = 11.0
    TICK_GAP_PX = 3.0
    TICK_LENGTH_PX = 5.0

    def __init__(self, vtk_widget, world_point):
        self.vtk_widget = vtk_widget
        self.renderer = getattr(vtk_widget, "renderer", None)
        self.render_window = vtk_widget.GetRenderWindow() if vtk_widget is not None else None
        self.world_point = np.asarray(world_point, dtype=float)
        self._render_observer = None

        self._points = vtk.vtkPoints()
        self._points.SetNumberOfPoints(self.RING_SEGMENTS + 8)

        lines = vtk.vtkCellArray()
        for idx in range(self.RING_SEGMENTS):
            lines.InsertNextCell(2)
            lines.InsertCellPoint(idx)
            lines.InsertCellPoint((idx + 1) % self.RING_SEGMENTS)

        tick_start = self.RING_SEGMENTS
        for offset in range(0, 8, 2):
            lines.InsertNextCell(2)
            lines.InsertCellPoint(tick_start + offset)
            lines.InsertCellPoint(tick_start + offset + 1)

        self._poly = vtk.vtkPolyData()
        self._poly.SetPoints(self._points)
        self._poly.SetLines(lines)

        display_coords = vtk.vtkCoordinate()
        display_coords.SetCoordinateSystemToDisplay()

        mapper = vtk.vtkPolyDataMapper2D()
        mapper.SetInputData(self._poly)
        mapper.SetTransformCoordinate(display_coords)

        self.actor = vtk.vtkActor2D()
        self.actor.SetMapper(mapper)
        prop = self.actor.GetProperty()
        prop.SetColor(1.0, 1.0, 1.0)
        prop.SetOpacity(0.95)
        prop.SetLineWidth(2.2)

        if self.renderer is not None:
            self.renderer.AddActor2D(self.actor)

        if self.render_window is not None:
            self._render_observer = self.render_window.AddObserver("RenderEvent", self._on_render)

        self.update_world_point(world_point)

    def update_world_point(self, world_point):
        self.world_point = np.asarray(world_point, dtype=float)
        self._update_geometry()

    def remove(self):
        try:
            if self.render_window is not None and self._render_observer is not None:
                self.render_window.RemoveObserver(self._render_observer)
        except Exception:
            pass
        self._render_observer = None

        try:
            if self.renderer is not None:
                self.renderer.RemoveActor2D(self.actor)
        except Exception:
            pass

    def _on_render(self, obj, event):
        self._update_geometry()

    def _update_geometry(self):
        if self.renderer is None or self.render_window is None or self.world_point is None:
            return

        try:
            self.renderer.SetWorldPoint(
                float(self.world_point[0]),
                float(self.world_point[1]),
                float(self.world_point[2]),
                1.0,
            )
            self.renderer.WorldToDisplay()
            display_point = self.renderer.GetDisplayPoint()
            if display_point is None or len(display_point) < 3:
                self.actor.VisibilityOff()
                return

            x_pos = float(display_point[0])
            y_pos = float(display_point[1])
            z_pos = float(display_point[2])
            width, height = self.render_window.GetSize()

            if (
                z_pos < 0.0
                or z_pos > 1.0
                or x_pos < -self.RING_RADIUS_PX
                or x_pos > width + self.RING_RADIUS_PX
                or y_pos < -self.RING_RADIUS_PX
                or y_pos > height + self.RING_RADIUS_PX
            ):
                self.actor.VisibilityOff()
                return

            angles = np.linspace(0.0, 2.0 * np.pi, self.RING_SEGMENTS, endpoint=False)
            for idx, angle in enumerate(angles):
                self._points.SetPoint(
                    idx,
                    x_pos + (self.RING_RADIUS_PX * np.cos(angle)),
                    y_pos + (self.RING_RADIUS_PX * np.sin(angle)),
                    0.0,
                )

            ring_edge = self.RING_RADIUS_PX + self.TICK_GAP_PX
            tick_edge = ring_edge + self.TICK_LENGTH_PX
            tick_points = (
                (x_pos, y_pos + ring_edge, 0.0),
                (x_pos, y_pos + tick_edge, 0.0),
                (x_pos, y_pos - ring_edge, 0.0),
                (x_pos, y_pos - tick_edge, 0.0),
                (x_pos - ring_edge, y_pos, 0.0),
                (x_pos - tick_edge, y_pos, 0.0),
                (x_pos + ring_edge, y_pos, 0.0),
                (x_pos + tick_edge, y_pos, 0.0),
            )

            tick_start = self.RING_SEGMENTS
            for idx, point in enumerate(tick_points):
                self._points.SetPoint(tick_start + idx, *point)

            self._points.Modified()
            self._poly.Modified()
            self.actor.VisibilityOn()

        except Exception:
            self.actor.VisibilityOff()


class PointSyncTool(QObject):
    """Synchronize a clicked point highlight across main/cross/cut views."""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.active = False
        self.selected_global_index = None
        self._main_observer = None
        self._section_observers = {}
        self._cut_observer = None
        self._overlays = {}
        print("✅ PointSyncTool initialized")

    def activate(self):
        """Activate synced point highlighting for every open view."""
        if self.active:
            print("⚠️ Point sync tool already active")
            return

        self._deactivate_conflicting_tools()

        self.active = True
        print("🎯 Point sync tool ACTIVATED")
        self._sync_shared_canvas_cursor(True)

        self._attach_main_observer()
        
        # ✅ NEW: Store observer IDs for cleanup
        if not hasattr(self, '_observer_ids'):
            self._observer_ids = {}

        for view_index, vtk_widget in getattr(self.app, "section_vtks", {}).items():
            self.activate_for_section(vtk_widget, view_index)

        cut_widget = getattr(getattr(self.app, "cut_section_controller", None), "cut_vtk", None)
        self.activate_for_cut_view(cut_widget)

        if self.selected_global_index is not None:
            self.highlight_global_point(self.selected_global_index)

        self._sync_footer_button_state(True)

    def deactivate(self):
        """
        ✅ FIXED: Complete deactivation with observer removal
        """
        if not self.active:
            return

        self.active = False
        print("🎯 Point sync tool DEACTIVATED")
        self._sync_shared_canvas_cursor(False)

        self._detach_main_observer()
        self.deactivate_all_sections()
        self.deactivate_for_cut_view()
        
        # ✅ NEW: Remove all stored observers
        if hasattr(self, '_observer_ids'):
            for view_idx, obs_id in list(self._observer_ids.items()):
                try:
                    vtk_widget = self.app.section_vtks.get(view_idx)
                    if vtk_widget is not None:
                        vtk_widget.interactor.RemoveObserver(obs_id)
                except Exception:
                    pass
            self._observer_ids.clear()
        
        self.clear_highlights()
        self._sync_footer_button_state(False)
        
        print("✅ Point sync tool fully deactivated")

    def _sync_shared_canvas_cursor(self, enabled):
        try:
            if hasattr(self.app, "set_cross_cursor_active"):
                self.app.set_cross_cursor_active(bool(enabled), "point_target_sync")
        except Exception:
            pass

    def _deactivate_conflicting_tools(self):
        """Release conflicting cross/cut input modes before point sync takes over."""
        cut_controller = getattr(self.app, "cut_section_controller", None)
        if cut_controller and hasattr(cut_controller, "deactivate_if_waiting"):
            try:
                cut_controller.deactivate_if_waiting()
            except Exception as e:
                print(f"   ⚠️ Failed to deactivate pending cut tool state: {e}")

        cross_action = getattr(self.app, "cross_action", None)
        cross_checked = bool(getattr(cross_action, "isChecked", lambda: False)())
        cross_active = bool(getattr(self.app, "cross_section_active", False))
        cross_interactor = getattr(self.app, "cross_interactor", None)

        if cross_checked or cross_active or cross_interactor is not None:
            try:
                if hasattr(self.app, "deactivate_cross_section_tool"):
                    self.app.deactivate_cross_section_tool()
                elif cross_action is not None and hasattr(cross_action, "setChecked"):
                    cross_action.setChecked(False)
            except Exception as e:
                print(f"   ⚠️ Failed to deactivate cross-section tool for point sync: {e}")

    def activate_for_section(self, section_vtk_widget, view_index):
        """Attach the tool to one cross-section view."""
        if section_vtk_widget is None:
            return

        info = self._section_observers.get(view_index)
        if info and info.get("vtk_widget") is section_vtk_widget:
            return

        if info:
            self.deactivate_for_section(view_index)

        try:
            observer_id = section_vtk_widget.interactor.AddObserver(
                "LeftButtonPressEvent",
                lambda obj, event: self._on_section_click(obj, event, section_vtk_widget, view_index),
                -1.0,
            )
            self._section_observers[view_index] = {
                "observer_id": observer_id,
                "vtk_widget": section_vtk_widget,
            }
            print(f"   ✅ Point sync observer attached to Cross Section View {view_index + 1}")

            if self.selected_global_index is not None:
                self._refresh_section_highlight(view_index, render=True)

        except Exception as e:
            print(f"   ⚠️ Failed to attach point sync to View {view_index + 1}: {e}")

    def deactivate_for_section(self, view_index):
        """Detach the tool from one cross-section view."""
        info = self._section_observers.get(view_index)
        if not info:
            return

        try:
            info["vtk_widget"].interactor.RemoveObserver(info["observer_id"])
        except Exception as e:
            print(f"   ⚠️ Failed to remove point sync observer for View {view_index + 1}: {e}")
        finally:
            self._clear_widget_highlight(info.get("vtk_widget"), render=False)
            self._section_observers.pop(view_index, None)

    def deactivate_all_sections(self):
        """Detach from every cross-section view."""
        for view_index in list(self._section_observers.keys()):
            self.deactivate_for_section(view_index)

    def activate_for_cut_view(self, cut_vtk_widget):
        """Attach the tool to the dedicated cut view."""
        if cut_vtk_widget is None:
            return

        if self._cut_observer and self._cut_observer.get("vtk_widget") is cut_vtk_widget:
            return

        if self._cut_observer:
            self.deactivate_for_cut_view()

        try:
            observer_id = cut_vtk_widget.interactor.AddObserver(
                "LeftButtonPressEvent",
                lambda obj, event: self._on_cut_click(obj, event, cut_vtk_widget),
                -1.0,
            )
            self._cut_observer = {
                "observer_id": observer_id,
                "vtk_widget": cut_vtk_widget,
            }
            print("   ✅ Point sync observer attached to Cut View")

            if self.selected_global_index is not None:
                self._refresh_cut_highlight(render=True)

        except Exception as e:
            print(f"   ⚠️ Failed to attach point sync to Cut View: {e}")

    def deactivate_for_cut_view(self):
        """Detach the tool from the cut view."""
        if not self._cut_observer:
            return

        try:
            self._cut_observer["vtk_widget"].interactor.RemoveObserver(self._cut_observer["observer_id"])
        except Exception as e:
            print(f"   ⚠️ Failed to remove cut point sync observer: {e}")
        finally:
            self._clear_widget_highlight(self._cut_observer.get("vtk_widget"), render=False)
            self._cut_observer = None

    def clear_highlights(self):
        """Remove synced highlight overlays from every view."""
        for overlay in list(self._overlays.values()):
            try:
                overlay.remove()
            except Exception:
                pass

        self._overlays.clear()
        self.selected_global_index = None

        self._safe_render(getattr(self.app, "vtk_widget", None))
        for vtk_widget in getattr(self.app, "section_vtks", {}).values():
            self._safe_render(vtk_widget)
        self._safe_render(getattr(getattr(self.app, "cut_section_controller", None), "cut_vtk", None))
        
        if hasattr(self.app, '_set_snt_layer_pick_footer_text'):
            self.app._set_snt_layer_pick_footer_text(None)

    def highlight_global_point(self, global_index):
        """Highlight one global dataset point everywhere it is visible."""
        xyz = self._get_dataset_xyz()
        if xyz is None:
            return False

        if global_index is None or global_index < 0 or global_index >= len(xyz):
            return False

        self.selected_global_index = int(global_index)
        main_point = xyz[self.selected_global_index]

        updated = False
        updated |= self._draw_highlight(getattr(self.app, "vtk_widget", None), main_point, render=False)

        for view_index in getattr(self.app, "section_vtks", {}).keys():
            updated |= self._refresh_section_highlight(view_index, render=False)

        updated |= self._refresh_cut_highlight(render=False)

        self._refresh_info_panel(self.selected_global_index)

        self._safe_render(getattr(self.app, "vtk_widget", None))
        for vtk_widget in getattr(self.app, "section_vtks", {}).values():
            self._safe_render(vtk_widget)
        self._safe_render(getattr(getattr(self.app, "cut_section_controller", None), "cut_vtk", None))

        if updated and hasattr(self.app, "statusBar"):
            self.app.statusBar().showMessage(
                f"🎯 Synced highlight for point #{self.selected_global_index:,}",
                3000,
            )

        print(f"🎯 Synced highlight updated for global point {self.selected_global_index}")
        return updated

    def _attach_main_observer(self):
        if self._main_observer is not None or not hasattr(self.app, "vtk_widget"):
            return

        try:
            self._main_observer = self.app.vtk_widget.interactor.AddObserver(
                "LeftButtonPressEvent",
                self._on_main_click,
                -1.0,
            )
            print("   ✅ Point sync observer attached to Main View")
        except Exception as e:
            print(f"   ⚠️ Failed to attach main point sync observer: {e}")

    def _detach_main_observer(self):
        if self._main_observer is None or not hasattr(self.app, "vtk_widget"):
            return

        try:
            self.app.vtk_widget.interactor.RemoveObserver(self._main_observer)
        except Exception as e:
            print(f"   ⚠️ Failed to remove main point sync observer: {e}")
        finally:
            self._main_observer = None

    def _on_main_click(self, obj, event):
        if not self.active:
            return

        if getattr(getattr(self.app, "cross_action", None), "isChecked", lambda: False)():
            return

        picked_pos = self._pick_position(getattr(self.app, "vtk_widget", None))
        if picked_pos is None:
            return

        global_index, distance = self._nearest_global_index(picked_pos, view_index=0)
        if global_index is None or distance is None or distance > 1.0:
            print(f"   ⚠️ Main point sync rejected pick (distance={distance})")
            return

        self.highlight_global_point(global_index)

    def _on_section_click(self, obj, event, section_vtk_widget, view_index):
        if not self.active:
            return

        picked_pos = self._pick_position(section_vtk_widget)
        if picked_pos is None:
            return

        section_points = getattr(self.app, f"section_{view_index}_points_transformed", None)
        section_indices = getattr(self.app, f"section_{view_index}_indices", None)
        
        if section_points is None or section_indices is None or len(section_points) == 0:
            return

        distances = np.linalg.norm(section_points - np.asarray(picked_pos, dtype=float), axis=1)
        sorted_indices = np.argsort(distances)
        
        local_index = None
        distance = None
        
        classification = self.app.data.get('classification') if hasattr(self.app, 'data') and self.app.data is not None else None
        
        for idx in sorted_indices:
            dist = distances[idx]
            if dist > 2.0:
                break
            
            if idx < len(section_indices):
                global_idx = int(section_indices[idx])
                if classification is not None and 0 <= global_idx < len(classification):
                    class_code = int(classification[global_idx])
                    # Check if visible in cross section (slot view_index + 1)
                    if self._is_class_visible(class_code, view_index=view_index + 1):
                        local_index = idx
                        distance = dist
                        break

        if local_index is None or distance is None:
            print(f"   ⚠️ Cross View {view_index + 1} point sync rejected pick (no visible points in range)")
            return

        self.highlight_global_point(int(section_indices[local_index]))

    def _on_cut_click(self, obj, event, cut_vtk_widget):
        if not self.active:
            return

        picked_pos = self._pick_position(cut_vtk_widget)
        if picked_pos is None:
            return

        cut_points, cut_indices = self._get_cut_data()
        if cut_points is None or cut_indices is None or len(cut_points) == 0:
            return

        distances = np.linalg.norm(cut_points - np.asarray(picked_pos, dtype=float), axis=1)
        sorted_indices = np.argsort(distances)
        
        local_index = None
        distance = None
        
        classification = self.app.data.get('classification') if hasattr(self.app, 'data') and self.app.data is not None else None
        
        for idx in sorted_indices:
            dist = distances[idx]
            if dist > 2.0:
                break
            
            if idx < len(cut_indices):
                global_idx = int(cut_indices[idx])
                if classification is not None and 0 <= global_idx < len(classification):
                    class_code = int(classification[global_idx])
                    # Check if visible in cut view (slot 5)
                    if self._is_class_visible(class_code, view_index=5):
                        local_index = idx
                        distance = dist
                        break

        if local_index is None or distance is None:
            print(f"   ⚠️ Cut View point sync rejected pick (no visible points in range)")
            return

        self.highlight_global_point(int(cut_indices[local_index]))

    def _refresh_section_highlight(self, view_index, render=False):
        vtk_widget = getattr(self.app, "section_vtks", {}).get(view_index)
        if vtk_widget is None:
            return False

        point = self._get_section_local_point(view_index, self.selected_global_index)
        if point is None:
            self._clear_widget_highlight(vtk_widget, render=render)
            return False

        return self._draw_highlight(vtk_widget, point, render=render)

    def _refresh_cut_highlight(self, render=False):
        vtk_widget = getattr(getattr(self.app, "cut_section_controller", None), "cut_vtk", None)
        if vtk_widget is None:
            return False

        point = self._get_cut_local_point(self.selected_global_index)
        if point is None:
            self._clear_widget_highlight(vtk_widget, render=render)
            return False

        return self._draw_highlight(vtk_widget, point, render=render)

    def _refresh_info_panel(self, global_index):
        try:
            if global_index is None:
                return

            xyz = self._get_dataset_xyz()
            classes = getattr(self.app, "data", {}).get("classification")
            if xyz is None or classes is None:
                return

            class_code = int(classes[global_index])
            world_xyz = tuple(xyz[global_index])

            identification_tool = getattr(self.app, "identification_tool", None)
            if identification_tool is not None:
                # Delegate to identification_tool — it handles ribbon + footer
                class_name = identification_tool.get_class_name(class_code)
                identification_tool.highlight_class(class_code)
                identification_tool._update_ribbon_info(class_code, class_name, world_xyz)
            else:
                # Fallback: update footer directly with "Class: code : name" format
                if hasattr(self.app, "snt_layer_pick_footer_label"):
                    class_name = f"Class {class_code}"
                    display_text = f"Class: {class_code} : {class_name}"
                    self.app.snt_layer_pick_footer_label.setText(display_text)
                    self.app.snt_layer_pick_footer_label.setToolTip(display_text)
                    self.app.snt_layer_pick_footer_label.setStatusTip(display_text)

        except Exception as e:
            print(f"   ⚠️ Failed to refresh point sync info panel: {e}")

    def _pick_position(self, vtk_widget):
        if vtk_widget is None or not hasattr(vtk_widget, "interactor") or not hasattr(vtk_widget, "renderer"):
            return None

        try:
            click_pos = vtk_widget.interactor.GetEventPosition()
        except Exception:
            return None

        try:
            import vtk

            pickers = []

            point_picker = vtk.vtkPointPicker()
            point_picker.SetTolerance(0.01)
            pickers.append((point_picker, lambda p: p.GetPointId() >= 0))

            cell_picker = vtk.vtkCellPicker()
            cell_picker.SetTolerance(0.01)
            pickers.append((cell_picker, lambda p: p.GetCellId() >= 0))

            for picker, is_valid in pickers:
                if picker.Pick(click_pos[0], click_pos[1], 0, vtk_widget.renderer) and is_valid(picker):
                    picked_pos = picker.GetPickPosition()
                    if picked_pos is not None and len(picked_pos) >= 3:
                        return np.asarray(picked_pos[:3], dtype=float)
        except Exception as e:
            print(f"   ⚠️ Point sync pick failed: {e}")

        return None

    def _is_class_visible(self, class_code, view_index=0):
        """Check if a classification code is visible in the given view/slot."""
        # Main view
        if view_index == 0:
            if hasattr(self.app, 'class_palette') and self.app.class_palette:
                class_info = self.app.class_palette.get(class_code)
                if class_info is not None:
                    return class_info.get('show', True)
        
        # Check specific view_palettes
        if hasattr(self.app, 'view_palettes') and self.app.view_palettes:
            slot_palette = self.app.view_palettes.get(view_index)
            if slot_palette is not None:
                class_info = slot_palette.get(class_code)
                if class_info is not None:
                    return class_info.get('show', True)
                    
        # Check display_mode_dialog backup
        if hasattr(self.app, 'display_mode_dialog') and self.app.display_mode_dialog:
            dialog = self.app.display_mode_dialog
            if hasattr(dialog, 'view_palettes') and dialog.view_palettes:
                slot_palette = dialog.view_palettes.get(view_index)
                if slot_palette is not None:
                    class_info = slot_palette.get(class_code)
                    if class_info is not None:
                        return class_info.get('show', True)
        
        # Default is visible
        return True

    def _nearest_global_index(self, picked_pos, view_index=0):
        xyz = self._get_dataset_xyz()
        if xyz is None or picked_pos is None:
            return None, None

        classification = self.app.data.get('classification') if hasattr(self.app, 'data') and self.app.data is not None else None

        try:
            spatial_index = getattr(self.app, "spatial_index", None)
            tree = getattr(spatial_index, "tree", None)
            if tree is not None:
                # Query k nearest points (e.g. k=100) to find one that is visible
                k_val = min(100, len(xyz))
                distances, indices = tree.query(np.asarray(picked_pos, dtype=float), k=k_val)
                
                if k_val == 1:
                    distances = [distances]
                    indices = [indices]
                
                for idx, dist in zip(indices, distances):
                    if idx >= len(xyz):
                        continue
                    if classification is not None:
                        class_code = int(classification[idx])
                        if not self._is_class_visible(class_code, view_index=view_index):
                            continue
                    return int(idx), float(dist)
                
                print("   ⚠️ No visible class points found in spatial index search window")
                return None, None
        except Exception as e:
            print(f"   ⚠️ Spatial index nearest query failed, falling back to numpy: {e}")

        try:
            distances = np.linalg.norm(xyz - np.asarray(picked_pos, dtype=float), axis=1)
            if classification is not None:
                unique_classes = np.unique(classification)
                hidden_classes = [c for c in unique_classes if not self._is_class_visible(int(c), view_index=view_index)]
                if hidden_classes:
                    hidden_mask = np.isin(classification, hidden_classes)
                    distances[hidden_mask] = np.inf
            
            index = int(np.argmin(distances))
            dist = float(distances[index])
            if dist == np.inf:
                return None, None
            return index, dist
        except Exception as e:
            print(f"   ⚠️ Numpy nearest query failed: {e}")
            return None, None

    def _nearest_local_index(self, local_points, picked_pos):
        if local_points is None or picked_pos is None or len(local_points) == 0:
            return None, None

        distances = np.linalg.norm(local_points - np.asarray(picked_pos, dtype=float), axis=1)
        index = int(np.argmin(distances))
        return index, float(distances[index])

    def _get_dataset_xyz(self):
        if not hasattr(self.app, "data") or self.app.data is None:
            return None
        return self.app.data.get("xyz")

    def _get_section_local_point(self, view_index, global_index):
        if global_index is None:
            return None

        section_points = getattr(self.app, f"section_{view_index}_points_transformed", None)
        section_indices = getattr(self.app, f"section_{view_index}_indices", None)
        if section_points is None or section_indices is None:
            return None

        matches = np.flatnonzero(section_indices == int(global_index))
        if matches.size == 0:
            return None

        return np.asarray(section_points[int(matches[0])], dtype=float)

    def _get_cut_data(self):
        controller = getattr(self.app, "cut_section_controller", None)
        if controller is None:
            return None, None

        try:
            if hasattr(controller, "get_cut_section_classification_data"):
                return controller.get_cut_section_classification_data()
        except Exception as e:
            print(f"   ⚠️ Failed to read cut section data: {e}")

        return getattr(controller, "cut_points", None), getattr(controller, "_cut_index_map", None)

    def _get_cut_local_point(self, global_index):
        if global_index is None:
            return None

        cut_points, cut_indices = self._get_cut_data()
        if cut_points is None or cut_indices is None:
            return None

        matches = np.flatnonzero(cut_indices == int(global_index))
        if matches.size == 0:
            return None

        return np.asarray(cut_points[int(matches[0])], dtype=float)

    def _draw_highlight(self, vtk_widget, point, render=False):
        if vtk_widget is None or point is None:
            return False

        try:
            overlay_key = id(vtk_widget)
            overlay = self._overlays.get(overlay_key)
            if overlay is None or overlay.vtk_widget is not vtk_widget:
                if overlay is not None:
                    try:
                        overlay.remove()
                    except Exception:
                        pass
                overlay = _TargetRingOverlay(vtk_widget, point)
                self._overlays[overlay_key] = overlay
            else:
                overlay.update_world_point(point)

            if render:
                self._safe_render(vtk_widget)
            return True

        except Exception as e:
            print(f"   ⚠️ Failed to draw point sync highlight: {e}")
            return False

    def _clear_widget_highlight(self, vtk_widget, render=False):
        if vtk_widget is None:
            return

        overlay = self._overlays.pop(id(vtk_widget), None)
        if overlay is not None:
            try:
                overlay.remove()
            except Exception:
                pass

        if render:
            self._safe_render(vtk_widget)

    def _safe_render(self, vtk_widget):
        if vtk_widget is None:
            return
        try:
            vtk_widget.render()
        except Exception:
            pass

    def _sync_footer_button_state(self, enabled):
        try:
            if hasattr(self.app, "_sync_point_sync_footer_button"):
                self.app._sync_point_sync_footer_button(enabled)
        except Exception:
            pass
