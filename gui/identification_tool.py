
"""
Identification Tool for NakshaAI
Allows clicking on points to identify their class and properties
✅ FIXED: Highlights identified class in Point Statistics widget
"""

from PySide6.QtCore import QObject, Signal
import numpy as np

try:
    from shiboken6 import isValid as _qt_object_is_valid
except Exception:
    def _qt_object_is_valid(obj):
        return obj is not None


class IdentificationTool(QObject):
    """Tool for identifying point properties on click"""
    
    point_identified = Signal(int, str, tuple)  # class_code, class_name, (x, y, z)
    
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.active = False
        self.picker = None
        self._click_observer = None
        self._section_observers = {}
        print("✅ IdentificationTool initialized")
    
    def activate(self):
        """Activate identification mode for main view and all open section views"""
        if self.active:
            print("⚠️ Identification tool already active")
            return
        
        self.active = True
        print("🔍 Identification tool ACTIVATED")
        
        # Activate for main view
        if hasattr(self.app, 'vtk_widget'):
            try:
                vtk_interactor = self.app.vtk_widget.interactor
                
                if self.picker is None:
                    import vtk
                    self.picker = vtk.vtkPointPicker()
                    self.picker.SetTolerance(0.005)
                
                # ✅ FIX: Use LOWER priority so Cross Section interactor runs first
                # Cross Section uses default priority (0.0), we use -1.0 to run AFTER it
                self._click_observer = vtk_interactor.AddObserver(
                    "LeftButtonPressEvent",
                    self._on_left_click,
                    -1.0  # Lower priority = runs after other tools
                )
                
                print(f"   ✅ Main view observer attached with LOW priority (ID: {self._click_observer})")
                
            except Exception as e:
                print(f"   ⚠️ Failed to attach main observer: {e}")
        
        # Activate for all open cross-section views
        if hasattr(self.app, 'section_vtks') and self.app.section_vtks:
            for view_index, vtk_widget in self.app.section_vtks.items():
                self.activate_for_section(vtk_widget, view_index)

    def deactivate(self):
        """Deactivate identification mode for all views"""
        if not self.active:
            return
        
        self.active = False
        print("🔍 Identification tool DEACTIVATED")
        
        # Deactivate main view
        if self._click_observer is not None and hasattr(self.app, 'vtk_widget'):
            try:
                vtk_interactor = self.app.vtk_widget.interactor
                vtk_interactor.RemoveObserver(self._click_observer)
                print(f"   ✅ Main view observer removed")
            except Exception as e:
                print(f"   ⚠️ Failed to remove main observer: {e}")
            
            self._click_observer = None
        
        # Deactivate all section views
        self.deactivate_all_sections()
        
        # ✅ NEW: Clear highlight when deactivating
        self.clear_highlight()
    
    def _cut_section_is_taking(self):
        """
        Return True if a cut section is currently being placed/taken (consuming
        left-click input). While in this state the IdentificationTool must NOT
        identify points, otherwise it visibly interferes with cut placement.

        CutSectionState: IDLE=0, WAITING_CENTER=1, WAITING_DEPTH=2, FINALIZED=3.
        We compare integer values to avoid importing the enum (keeps this tool
        free of a circular dependency on the cross_section package).
        """
        cut = getattr(self.app, "cut_section_controller", None)
        if cut is None:
            return False
        state = getattr(cut, "_state", 0)
        if state in (1, 2):  # WAITING_CENTER / WAITING_DEPTH
            return True
        return False

    def _on_left_click(self, obj, event):
        """
        Handle left click to identify point.

        ✅ FIX: Check if Cross Section tool is active and skip if it is
        """
        if not self.active:
            return
        
        # ✅ CRITICAL FIX: Don't interfere with Cross Section tool
        if hasattr(self.app, 'cross_interactor') and self.app.cross_interactor:
            # Check if Cross Section is currently active (drawing rectangle)
            if hasattr(self.app, 'cross_action') and self.app.cross_action:
                if self.app.cross_action.isChecked():
                    print("🚫 Cross Section active - skipping identification")
                    return

        # ✅ CRITICAL FIX: Don't interfere while a Cut Section is being taken
        if self._cut_section_is_taking():
            print("🚫 Cut Section being taken - skipping identification")
            return
        
        try:
            # Get click position
            vtk_interactor = self.app.vtk_widget.interactor
            click_pos = vtk_interactor.GetEventPosition()
            
            print(f"🔍 Click at screen position: {click_pos}")
            
            # Get renderer
            renderer = self.app.vtk_widget.renderer
            
            # Use cell picker instead of point picker
            import vtk
            picker = vtk.vtkCellPicker()
            picker.SetTolerance(0.01)  # Increased tolerance for better picking
            
            # Pick at the click location
            success = picker.Pick(click_pos[0], click_pos[1], 0, renderer)
            
            if success:
                # Get the picked 3D position
                picked_pos = picker.GetPickPosition()
                
                print(f"   ✅ Picked 3D position: {picked_pos}")
                
                # Find the closest point in the actual point cloud data
                if hasattr(self.app, 'data') and self.app.data is not None:
                    xyz = self.app.data.get('xyz')
                    classification = self.app.data.get('classification')
                    
                    if xyz is not None and classification is not None:
                        # Find closest point to picked position
                        picked_array = np.asarray(picked_pos, dtype=float)
                        closest_idx, closest_dist = self._nearest_global_index(picked_array, xyz, view_index=0)
                        if closest_idx is None or closest_dist is None:
                            print("   ⚠️ Failed to resolve nearest point")
                            return
                        
                        print(f"   📍 Closest point index: {closest_idx} (distance: {closest_dist:.3f})")
                        
                        # Only accept if reasonably close (within 1 unit)
                        if closest_dist < 1.0:
                            class_code = int(classification[closest_idx])
                            class_name = self.get_class_name(class_code)
                            actual_pos = tuple(xyz[closest_idx])
                            
                            print(f"   🏷️ Class: {class_code} ({class_name})")
                            
                            # ✅ NEW: Highlight in Point Statistics widget
                            self.highlight_class(class_code)
                            
                            # Update ribbon display
                            self._update_ribbon_info(class_code, class_name, actual_pos, point_index=closest_idx)

                            # Emit signal
                            self.point_identified.emit(class_code, class_name, actual_pos)
                        else:
                            print(f"   ⚠️ Closest point too far away ({closest_dist:.3f} units)")
                    else:
                        print("   ⚠️ No xyz or classification data")
                else:
                    print("   ⚠️ No point cloud data loaded")
            else:
                print("   ⚠️ Pick failed - no geometry at click location")
                    
        except Exception as e:
            print(f"   ❌ Error in click handler: {e}")
            import traceback
            traceback.print_exc()
    
    def identify_point_index(self, point_index):
        """
        Identify an already-known global point index using the same backend as
        an interactive 3D click.

        This is intentionally a public, index-based entry point so dialogs such
        as View Fields do not have to fake a VTK pick or duplicate identify
        ribbon/statistics logic.

        Returns a small result dict on success, otherwise None.
        """
        try:
            data = getattr(self.app, "data", None) or {}
            xyz = data.get("xyz")
            classification = data.get("classification")
            if xyz is None or classification is None:
                print("   ⚠️ identify_point_index: xyz/classification unavailable")
                return None

            idx = int(point_index)
            if idx < 0 or idx >= len(xyz) or idx >= len(classification):
                print(f"   ⚠️ identify_point_index: index out of range: {idx}")
                return None

            class_code = int(classification[idx])
            class_name = self.get_class_name(class_code)
            actual_pos = tuple(float(v) for v in xyz[idx])

            self.highlight_class(class_code)
            self._update_ribbon_info(
                class_code,
                class_name,
                actual_pos,
                point_index=idx,
            )
            self.point_identified.emit(class_code, class_name, actual_pos)

            print(
                "   ✅ Identified point index %d: class %d (%s) at "
                "(%.3f, %.3f, %.3f)" % (
                    idx, class_code, class_name,
                    actual_pos[0], actual_pos[1], actual_pos[2],
                )
            )
            return {
                "point_index": idx,
                "class_code": class_code,
                "class_name": class_name,
                "xyz": actual_pos,
            }
        except Exception as exc:
            print(f"   ❌ identify_point_index failed: {exc}")
            import traceback
            traceback.print_exc()
            return None

    def _collect_field_values(self, class_code, class_name, xyz, point_index=None):
        """
        Build an ordered {field_label: display_value} dict for the picked point,
        restricted to the fields checked in the View Fields dialog (Display ▸
        Fields) and to data actually kept in memory for the loaded cloud.
        """
        from gui.dialogs.view_fields_dialog import FIELD_SPECS, DEFAULT_CHECKED

        selected = getattr(self.app, "selected_view_fields", None)
        selected = set(selected) if selected is not None else set(DEFAULT_CHECKED)

        x, y, z = xyz
        class_lvl = self.get_class_lvl(class_code)
        raw_values = {
            "Class": str(class_code),
            "Description": class_lvl if class_lvl else class_name,
            "Easting": f"{x:.2f}",
            "Northing": f"{y:.2f}",
            "Elevation": f"{z:.2f}",
        }

        data = getattr(self.app, "data", None) or {}
        if point_index is not None:
            intensity = data.get("intensity")
            if intensity is not None and 0 <= point_index < len(intensity):
                raw_values["Intensity"] = f"{float(intensity[point_index]):.1f}"

            rgb = data.get("rgb")
            if rgb is not None and 0 <= point_index < len(rgb):
                r, g, b = (int(v) for v in rgb[point_index])
                raw_values["Color RGB"] = f"{r}, {g}, {b}"

        return {
            label: raw_values[label]
            for label, _dims, _always in FIELD_SPECS
            if label in selected and label in raw_values
        }

    def _update_ribbon_info(self, class_code, class_name, xyz, point_index=None):
        """Update the identification ribbon with point info"""
        try:
            # Sync the View Fields table (if open) with the identified point.
            if point_index is not None:
                table_dlg = getattr(self.app, "_view_fields_table_dialog", None)
                if table_dlg is not None and table_dlg.isVisible():
                    try:
                        print(f"   🔗 Syncing View Fields table (point {point_index})")
                        table_dlg.highlight_point(point_index)
                    except Exception as exc:
                        print(f"   ⚠️ View Fields table highlight failed: {exc}")
            # Get color and level regardless of ribbon availability
            class_color = self.get_class_color(class_code)
            class_lvl = self.get_class_lvl(class_code)
            display_name = class_lvl if class_lvl else class_name
            fields = self._collect_field_values(class_code, class_name, xyz, point_index)

            if hasattr(self.app, 'ribbon_manager'):
                identify_ribbon = self.app.ribbon_manager.ribbons.get('identify')
                if identify_ribbon:
                    identify_ribbon.update_info(
                        class_code,
                        class_name,
                        xyz,
                        color=class_color,
                        lvl=class_lvl,
                        fields=fields,
                    )

            # Update footer label with color dot + "Class: code : name"
            if hasattr(self.app, 'snt_layer_pick_footer_label'):
                lbl = self.app.snt_layer_pick_footer_label
                from PySide6.QtCore import Qt

                # Build color strings
                if class_color and len(class_color) >= 3:
                    r, g, b = int(class_color[0]), int(class_color[1]), int(class_color[2])
                else:
                    r, g, b = 160, 160, 160

                hex_color = f"#{r:02X}{g:02X}{b:02X}"

                # Use rich-text HTML: colored ● dot + class text
                plain_text = f"Class: {class_code} : {display_name}"
                html = (
                    f'<span style="color:{hex_color}; font-size:14px;">&#9679;</span>'
                    f'&nbsp;<span style="font-weight:600;">{plain_text}</span>'
                )

                lbl.setTextFormat(Qt.RichText)
                lbl.setText(html)

                # Colored left-border accent + subtle tinted background
                lbl.setStyleSheet(
                    f"""
                    QLabel {{
                        border-left: 3px solid {hex_color};
                        padding-left: 5px;
                        background: rgba({r},{g},{b},30);
                        border-radius: 3px;
                        font-size: 11px;
                    }}
                    """
                )
                lbl.setToolTip(plain_text)
                lbl.setStatusTip(plain_text)

        except Exception as e:
            print(f"   ⚠️ Failed to update ribbon: {e}")
    
    def highlight_class(self, class_code):
        """
        ✅ NEW: Highlight a class in the Point Statistics widget.
        
        Args:
            class_code: The classification code to highlight
        """
        if not hasattr(self.app, 'point_count_widget') or not self.app.point_count_widget:
            print("   ⚠️ Point Statistics widget not available")
            return
        
        try:
            # Get the widget's stats container
            stats_container = self.app.point_count_widget.stats_container
            layout = self.app.point_count_widget.stats_layout
            
            # Iterate through all stat widgets to find and highlight the matching class
            for i in range(layout.count()):
                widget = layout.itemAt(i).widget()
                if widget and hasattr(widget, 'property'):
                    # Check if this widget represents our class
                    stored_code = widget.property('class_code')
                    if stored_code == class_code:
                        # Apply highlight style
                        self._apply_highlight_style(widget, class_code)
                        
                        # Scroll to make it visible
                        if hasattr(self.app.point_count_widget, 'parent') and \
                           hasattr(self.app.point_count_widget.parent(), 'ensureWidgetVisible'):
                            self.app.point_count_widget.parent().ensureWidgetVisible(widget)
                        
                        print(f"   ✅ Highlighted class {class_code} in Point Statistics")
                        return
            
            print(f"   ⚠️ Class {class_code} not found in Point Statistics widget")
            
        except Exception as e:
            print(f"   ⚠️ Failed to highlight class: {e}")
            import traceback
            traceback.print_exc()
    
    def _apply_highlight_style(self, widget, class_code):
        """Apply a highlight animation/style to a widget."""
        try:
            from PySide6.QtCore import QPropertyAnimation, QEasingCurve
            from PySide6.QtGui import QColor
            
            # Get the original style
            original_style = widget.styleSheet()
            
            # Create a pulsing highlight effect
            def pulse_highlight():
                if widget is None or not _qt_object_is_valid(widget):
                    return
                # Bright highlight
                highlight_style = original_style.replace(
                    'background-color: rgba(',
                    'background-color: rgba(255, 255, 100, 0.4); /* background-color: rgba('
                )
                widget.setStyleSheet(highlight_style)
                
                # After 500ms, fade back
                from PySide6.QtCore import QTimer
                def _restore_style():
                    try:
                        if widget is None or not _qt_object_is_valid(widget):
                            return
                        widget.setStyleSheet(original_style)
                    except Exception:
                        pass
                QTimer.singleShot(500, _restore_style)
            
            # Trigger pulse
            pulse_highlight()
            
        except Exception as e:
            print(f"   ⚠️ Failed to apply highlight animation: {e}")
    
    def clear_highlight(self):
        """Clear all highlights from the Point Statistics widget."""
        if not hasattr(self.app, 'point_count_widget') or not self.app.point_count_widget:
            return
        
        try:
            # Simply refresh the widget to restore original styles
            self.app.point_count_widget.update_statistics()
        except Exception as e:
            print(f"   ⚠️ Failed to clear highlight: {e}")
            
        if hasattr(self.app, '_set_snt_layer_pick_footer_text'):
            self.app._set_snt_layer_pick_footer_text(None)
        # Also reset footer stylesheet and text format
        if hasattr(self.app, 'snt_layer_pick_footer_label'):
            from PySide6.QtCore import Qt
            lbl = self.app.snt_layer_pick_footer_label
            lbl.setTextFormat(Qt.PlainText)
            lbl.setStyleSheet("")

    
    def get_class_color(self, class_code):
        """
        Get the RGB color for a classification code from the app's class palette.
        Returns tuple (r, g, b) or None if not found.
        """
        # Try to get from app's class palette first
        if hasattr(self.app, 'class_palette') and self.app.class_palette:
            class_info = self.app.class_palette.get(class_code)
            if class_info and 'color' in class_info:
                return class_info['color']
        
        # Try Display Mode dialog palette as backup
        if hasattr(self.app, 'display_mode_dialog') and self.app.display_mode_dialog:
            dialog = self.app.display_mode_dialog
            if hasattr(dialog, 'view_palettes'):
                for view_palette in dialog.view_palettes.values():
                    if class_code in view_palette:
                        color = view_palette[class_code].get('color')
                        if color:
                            return color
        
        # Try Display Mode table directly
        if hasattr(self.app, 'display_dialog') and self.app.display_dialog:
            dialog = self.app.display_dialog
            if hasattr(dialog, 'table'):
                table = dialog.table
                for row in range(table.rowCount()):
                    try:
                        code = int(table.item(row, 1).text())
                        if code == class_code:
                            color_item = table.item(row, 5)
                            if color_item:
                                color = color_item.background().color().getRgb()[:3]
                                return color
                    except Exception:
                        continue
        
        # Fallback to default gray
        return (160, 160, 160)
    
    def get_class_name(self, class_code):
        """
        Get the name for a classification code from the app's class palette.
        Falls back to generic name if not found in active palettes.
        """
        if hasattr(self.app, 'display_mode_dialog') and self.app.display_mode_dialog:
            table = self.app.display_mode_dialog.table
            for row in range(table.rowCount()):
                try:
                    code = int(table.item(row, 1).text())
                    if code == class_code:
                        desc = table.item(row, 2).text()  # Description column
                        return desc
                except Exception:
                    continue
        # Try to get from app's class palette first
        if hasattr(self.app, 'class_palette') and self.app.class_palette:
            class_info = self.app.class_palette.get(class_code)
            if class_info and 'description' in class_info:
                return class_info['description']
        
        # Try Display Mode dialog palette as backup
        if hasattr(self.app, 'display_mode_dialog') and self.app.display_mode_dialog:
            dialog = self.app.display_mode_dialog
            if hasattr(dialog, 'view_palettes'):
                for view_palette in dialog.view_palettes.values():
                    if class_code in view_palette:
                        return view_palette[class_code].get('description', f"Class {class_code}")
        
        return f"Class {class_code}"
    
    def activate_for_section(self, section_vtk_widget, view_index):
        """
        Activate identification for a specific cross-section view.
        
        Args:
            section_vtk_widget: The QtInteractor widget for the section view
            view_index: Index of the section view (0-3)
        """
        if not hasattr(self, '_section_observers'):
            self._section_observers = {}
        
        # Don't re-attach if already active for this view
        if view_index in self._section_observers:
            print(f"⚠️ Identification already active for section view {view_index + 1}")
            return
        
        print(f"🔍 Activating identification for Cross Section View {view_index + 1}")
        
        try:
            vtk_interactor = section_vtk_widget.interactor
            
            # Add observer for left click (lower priority for section views too)
            observer_id = vtk_interactor.AddObserver(
                "LeftButtonPressEvent",
                lambda obj, event: self._on_section_click(obj, event, section_vtk_widget, view_index),
                -1.0  # Lower priority
            )
            
            self._section_observers[view_index] = {
                'observer_id': observer_id,
                'vtk_widget': section_vtk_widget
            }
            
            print(f"   ✅ Section click observer attached (ID: {observer_id})")
            
        except Exception as e:
            print(f"   ⚠️ Failed to attach section observer: {e}")

    def deactivate_for_section(self, view_index):
        """Deactivate identification for a specific cross-section view."""
        if not hasattr(self, '_section_observers'):
            return
        
        if view_index not in self._section_observers:
            return
        
        try:
            info = self._section_observers[view_index]
            vtk_interactor = info['vtk_widget'].interactor
            vtk_interactor.RemoveObserver(info['observer_id'])
            
            del self._section_observers[view_index]
            print(f"   ✅ Section observer removed for view {view_index + 1}")
            
        except Exception as e:
            print(f"   ⚠️ Failed to remove section observer: {e}")

    def deactivate_all_sections(self):
        """Deactivate identification for all cross-section views."""
        if not hasattr(self, '_section_observers'):
            return
        
        for view_index in list(self._section_observers.keys()):
            self.deactivate_for_section(view_index)

    def _on_section_click(self, obj, event, section_vtk_widget, view_index):
        """Handle left click in cross-section view to identify point"""
        if not self.active:
            return

        # ✅ CRITICAL FIX: Don't interfere while a Cut Section is being taken.
        # The cut placement observer is attached to the very same cross-section
        # view, so both handlers otherwise run on every click and the point
        # target "glitches" / fights with the cut tool during placement.
        if self._cut_section_is_taking():
            print(f"🚫 Cut Section being taken (view {view_index + 1}) - skipping identification")
            return

        # ✅ CRITICAL FIX: Don't interfere with the active Cross Section tool
        # (e.g. section-locate clicks) in the section view either.
        if hasattr(self.app, 'cross_interactor') and self.app.cross_interactor:
            if hasattr(self.app, 'cross_action') and self.app.cross_action:
                if self.app.cross_action.isChecked():
                    print(f"🚫 Cross Section active (view {view_index + 1}) - skipping identification")
                    return

        try:

            # Get click position
            vtk_interactor = section_vtk_widget.interactor
            click_pos = vtk_interactor.GetEventPosition()
            
            print(f"🔍 Section View {view_index + 1} - Click at: {click_pos}")
            
            # Get renderer
            renderer = section_vtk_widget.renderer
            
            # Use cell picker
            import vtk
            picker = vtk.vtkCellPicker()
            picker.SetTolerance(0.01)
            
            # Pick at the click location
            success = picker.Pick(click_pos[0], click_pos[1], 0, renderer)
            
            if success:
                # Get the picked 3D position
                picked_pos = picker.GetPickPosition()
                
                print(f"   ✅ Picked 3D position: {picked_pos}")
                
                # Get section data for this view
                section_xyz = self._get_section_xyz(view_index)
                
                if section_xyz is not None and len(section_xyz) > 0:
                    # Find closest point in section data
                    picked_array = np.array(picked_pos)
                    distances = np.linalg.norm(section_xyz - picked_array, axis=1)
                    
                    # Sort indices of distances in ascending order
                    sorted_indices = np.argsort(distances)
                    
                    closest_idx = None
                    closest_dist = None
                    original_idx = None
                    class_code = None
                    
                    # Look for the first closest point that is in a visible class
                    for local_idx in sorted_indices:
                        dist = distances[local_idx]
                        if dist >= 2.0:  # Larger tolerance for section views
                            break  # Since sorted, all subsequent distances are >= 2.0
                        
                        # Resolve original index
                        # Prefer explicit section index mapping
                        sec_indices = getattr(self.app, f"section_{view_index}_indices", None)
                        orig_idx = None
                        if sec_indices is not None:
                            try:
                                if local_idx < len(sec_indices):
                                    orig_idx = int(sec_indices[local_idx])
                            except Exception:
                                orig_idx = None

                        # Fallback to legacy core/buffer mask mapping
                        if orig_idx is None:
                            orig_idx = self._get_original_index(view_index, local_idx)
                            
                        if orig_idx is not None:
                            classification = self.app.data.get('classification')
                            if classification is not None and 0 <= orig_idx < len(classification):
                                c_code = int(classification[orig_idx])
                                # Check if this class is visible in this section view (slot_idx = view_index + 1)
                                if self._is_class_visible(c_code, view_index=view_index + 1):
                                    closest_idx = local_idx
                                    closest_dist = dist
                                    original_idx = orig_idx
                                    class_code = c_code
                                    break
                    
                    if closest_idx is not None and original_idx is not None and class_code is not None:
                        class_name = self.get_class_name(class_code)
                        actual_pos = tuple(section_xyz[closest_idx])
                        
                        print(f"   🏷️ Class: {class_code} ({class_name})")
                        
                        # ✅ Highlight in Point Statistics widget
                        self.highlight_class(class_code)

                        # Update ribbon display
                        self._update_ribbon_info(class_code, class_name, actual_pos, point_index=original_idx)

                        # Emit signal
                        self.point_identified.emit(class_code, class_name, actual_pos)
                    else:
                        print("   ⚠️ No visible point identified in range or failed to map index")
                else:
                    print("   ⚠️ No section data available")
            else:
                print("   ⚠️ Pick failed - no geometry at click location")
                    
        except Exception as e:
            print(f"   ❌ Error in section click handler: {e}")
            import traceback
            traceback.print_exc()

    def _get_section_xyz(self, view_index):
        """Get XYZ coordinates for a section view."""
        try:
            # Preferred source: transformed section points aligned with section_indices.
            transformed = getattr(self.app, f"section_{view_index}_points_transformed", None)
            if transformed is not None and len(transformed) > 0:
                return transformed

            # Try to get from stored section data
            core_pts = getattr(self.app, f'section_{view_index}_core_points', None)
            buf_pts = getattr(self.app, f'section_{view_index}_buffer_points', None)
            
            if core_pts is not None:
                if buf_pts is not None:
                    return np.vstack([core_pts, buf_pts])
                return core_pts
            
            return None
            
        except Exception as e:
            print(f"   ⚠️ Error getting section XYZ: {e}")
            return None

    def _get_original_index(self, view_index, section_local_idx):
        """
        Convert local section index to original dataset index.
        
        Args:
            view_index: Section view index (0-3)
            section_local_idx: Index within the section point array
        
        Returns:
            Original index in app.data, or None if not found
        """
        try:
            # Get masks for this section
            core_mask = getattr(self.app, f'section_{view_index}_core_mask', None)
            buffer_mask = getattr(self.app, f'section_{view_index}_buffer_mask', None)
            
            if core_mask is None:
                return None
            
            # Get core and buffer point counts
            core_pts = getattr(self.app, f'section_{view_index}_core_points', None)
            buf_pts = getattr(self.app, f'section_{view_index}_buffer_points', None)
            
            num_core = len(core_pts) if core_pts is not None else 0
            
            # Check if index is in core or buffer
            if section_local_idx < num_core:
                # It's in core - find the Nth true value in core_mask
                core_indices = np.where(core_mask)[0]
                if section_local_idx < len(core_indices):
                    return core_indices[section_local_idx]
            else:
                # It's in buffer
                if buffer_mask is not None:
                    buffer_local_idx = section_local_idx - num_core
                    # Find indices that are in buffer but not in core
                    buffer_only = buffer_mask & ~core_mask
                    buffer_indices = np.where(buffer_only)[0]
                    if buffer_local_idx < len(buffer_indices):
                        return buffer_indices[buffer_local_idx]
            
            return None
            
        except Exception as e:
            print(f"   ⚠️ Error mapping section index: {e}")
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

    def _nearest_global_index(self, picked_pos, xyz, view_index=0):
        """Resolve nearest global index of a visible class using spatial index when available."""
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
            print(f"   ⚠️ Spatial nearest query failed, falling back: {e}")

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
        
    def get_class_lvl(self, class_code):
        """
        Get the Level (Lvl) for a classification code from Display Mode table.
        Returns string or empty string if not found.
        """
        # Read from Display Mode dialog table (Column 4 = Lvl)
        if hasattr(self.app, 'display_mode_dialog') and self.app.display_mode_dialog:
            table = self.app.display_mode_dialog.table
            for row in range(table.rowCount()):
                try:
                    code = int(table.item(row, 1).text())
                    if code == class_code:
                        lvl_item = table.item(row, 4)  # Column 4 = Lvl
                        return lvl_item.text() if lvl_item else ""
                except Exception:
                    continue
        
        # Fallback
        return ""