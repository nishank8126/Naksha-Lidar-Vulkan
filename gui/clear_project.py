
# from PySide6.QtWidgets import QMessageBox
# from PySide6.QtCore import QSettings
# import os
# from .shading_display import clear_shading_cache
# from .display_mode import clone_palette

# def _save_display_settings_before_clear(app):
#     """
#     Save current display mode settings to QSettings before clearing.
#     ✅ FIXED: Saves PTC, palettes, checkboxes, color mode, and display mode.
#     """
#     try:
#         settings = QSettings("NakshaAI", "LidarApp")
        
#         # Identify the best active dialog instance (prefer visible, then populated).
#         dialog = None
#         _candidates = []
#         for _attr in ("display_mode_dialog", "display_dialog"):
#             _d = getattr(app, _attr, None)
#             if _d is None:
#                 continue
#             if all(id(_d) != id(_x) for _x in _candidates):
#                 _candidates.append(_d)

#         for _d in _candidates:
#             try:
#                 if hasattr(_d, "isVisible") and _d.isVisible():
#                     dialog = _d
#                     break
#             except Exception:
#                 continue
#         if dialog is None:
#             for _d in _candidates:
#                 try:
#                     _vp = getattr(_d, "view_palettes", None)
#                     if isinstance(_vp, dict) and _vp:
#                         dialog = _d
#                         break
#                 except Exception:
#                     continue
#         if dialog is None and _candidates:
#             dialog = _candidates[0]

#         # Flush any in-progress table edits into dialog palette state so the
#         # latest user-facing values are captured before serialization.
#         if dialog and hasattr(dialog, "_save_slot_state"):
#             try:
#                 dialog._save_slot_state(int(getattr(dialog, "current_slot", 0)))
#             except Exception:
#                 pass

#         # Sync dialog palettes into app.view_palettes to avoid stale app/dialog
#         # divergence during grid-switch save-before-clear.
#         if dialog and hasattr(dialog, "view_palettes"):
#             if not hasattr(app, "view_palettes") or app.view_palettes is None:
#                 app.view_palettes = {}
#             try:
#                 for _slot_idx, _slot_palette in (dialog.view_palettes or {}).items():
#                     try:
#                         slot_i = int(_slot_idx)
#                     except Exception:
#                         continue
#                     if isinstance(_slot_palette, dict):
#                         app.view_palettes[slot_i] = clone_palette(_slot_palette)
#             except Exception:
#                 pass
            
#         # # ====================================================================
#         # # ✅ Save Global Last Used PTC (Independent of any file)
#         # # ====================================================================
#         # if dialog and hasattr(dialog, 'current_ptc_path') and dialog.current_ptc_path:
#         #     settings.setValue("global_last_ptc_path", dialog.current_ptc_path)
#         #     print(f"🌍 Saved GLOBAL PTC: {os.path.basename(dialog.current_ptc_path)}")

#         # # ====================================================================
#         # # ✅ NEW: Save GLOBAL display settings (independent of specific files)
#         # # These will be applied to ANY new file loaded
#         # # ====================================================================
#         # if dialog:
#         #     palette_source = None
#         #     if hasattr(dialog, 'view_palettes') and dialog.view_palettes:
#         #         palette_source = dialog.view_palettes
#         #     elif hasattr(app, 'view_palettes') and app.view_palettes:
#         #         palette_source = app.view_palettes

#         #     # Save ALL view palettes globally
#         #     if palette_source:
#         #         global_palette_data = {}
#         #         for view_idx, palette in palette_source.items():
#         #             global_palette_data[str(view_idx)] = {
#         #                 str(code): {
#         #                     'show': info.get('show', True),
#         #                     'description': info.get('description', ''),
#         #                     'lvl': info.get('lvl', ''),
#         #                     'color': list(info.get('color', (128, 128, 128))),
#         #                     'weight': info.get('weight', 1.0)
#         #                 }
#         #                 for code, info in palette.items()
#         #             }
#         #         settings.setValue("global_view_palettes", global_palette_data)
#         #         print(f"  ✅ Saved {len(global_palette_data)} GLOBAL view palettes")
            
#             # Save ALL checkbox states globally
#             if hasattr(dialog, 'slot_shows') and dialog.slot_shows:
#                 global_checkbox_data = {}
#                 for slot_idx, show_dict in dialog.slot_shows.items():
#                     global_checkbox_data[str(slot_idx)] = {
#                         str(code): checked
#                         for code, checked in show_dict.items()
#                     }
#                 settings.setValue("global_slot_shows", global_checkbox_data)
#                 print(f"  ✅ Saved GLOBAL checkbox states for {len(global_checkbox_data)} views")
            
#             # Save color mode globally
#             if hasattr(dialog, 'color_mode'):
#                 color_mode_idx = dialog.color_mode.currentIndex()
#                 settings.setValue("global_color_mode", color_mode_idx)
#                 print(f"  ✅ Saved GLOBAL color mode: {color_mode_idx}")
            
#             # Save border values globally
#             if hasattr(dialog, 'view_borders') and dialog.view_borders:
#                 settings.setValue("global_view_borders", dialog.view_borders)
#                 print(f"  ✅ Saved GLOBAL border values: {dialog.view_borders}")

#         # Save display mode globally
#         if hasattr(app, 'display_mode') and app.display_mode:
#             settings.setValue("global_display_mode", app.display_mode)
#             print(f"  ✅ Saved GLOBAL display mode: {app.display_mode}")

#         # ====================================================================
#         # File-Specific Settings (Only if a file is loaded)
#         # ====================================================================
#         if not hasattr(app, 'loaded_file') or not app.loaded_file:
#             settings.sync()
#             print("✅ GLOBAL display settings saved\n")
#             try:
#                 print("[RUNTIME-CHECK] save-before-clear scope=GLOBAL_ONLY loaded_file=None")
#             except Exception:
#                 pass
#             return
        
#         file_key = os.path.abspath(app.loaded_file) 

#         print(f"🧹 Clearing per-file display settings for: {os.path.basename(file_key)}")
#         for key_prefix in (
#             "file_ptc/",
#             "file_palettes/",
#             "file_slot_shows/",
#             "file_display_mode/",
#             "file_color_mode/",
#         ):
#             settings.remove(f"{key_prefix}{file_key}")
#         settings.sync()
#         print(f"✅ Per-file display settings cleared for: {os.path.basename(file_key)}\n")
#         try:
#             ptc_name = os.path.basename(dialog.current_ptc_path) if (dialog and getattr(dialog, "current_ptc_path", None)) else "None"
#             palette_slots = len(getattr(app, "view_palettes", {}) or {})
#             show_slots = len(getattr(dialog, "slot_shows", {}) or {}) if dialog else 0
#             print(
#                 f"[RUNTIME-CHECK] save-before-clear scope=FILE_DISABLED "
#                 f"file={os.path.basename(file_key)} ptc={ptc_name} "
#                 f"palette_slots={palette_slots} show_slots={show_slots}"
#             )
#         except Exception:
#             pass
#         return

#         print(f"💾 Saving display settings for: {os.path.basename(file_key)}")
        
#         # # 1️⃣ Save current PTC path for THIS specific file
#         # if dialog and hasattr(dialog, 'current_ptc_path') and dialog.current_ptc_path:
#         #     settings.setValue(f"file_ptc/{file_key}", dialog.current_ptc_path)
#         #     print(f"  ✅ PTC: {os.path.basename(dialog.current_ptc_path)}")
        
#         # # 2️⃣ Save view palettes (colors, weights, descriptions for all views)
#         # # Prefer app.view_palettes after slot-sync above (canonical runtime state).
#         # palette_source = None
#         # if dialog and hasattr(dialog, 'view_palettes') and dialog.view_palettes:
#         #     palette_source = dialog.view_palettes
#         # elif hasattr(app, 'view_palettes') and app.view_palettes:
#         #     palette_source = app.view_palettes

#         # if palette_source:
#         #     palette_data = {}
#         #     for view_idx, palette in palette_source.items():
#         #         palette_data[str(view_idx)] = {
#         #             str(code): {
#         #                 'show': info.get('show', True),
#         #                 'description': info.get('description', ''),
#         #                 'lvl': info.get('lvl', ''),
#         #                 'color': list(info.get('color', (128, 128, 128))),  # Convert tuple to list for JSON
#         #                 'weight': info.get('weight', 1.0)
#         #             }
#         #             for code, info in palette.items()
#         #         }
#         #     settings.setValue(f"file_palettes/{file_key}", palette_data)
#         #     print(f"  ✅ Saved {len(palette_data)} view palettes")
        
#         # 3️⃣ Save checkbox states (slot_shows)
#         if dialog and hasattr(dialog, 'slot_shows') and dialog.slot_shows:
#             checkbox_data = {}
#             for slot_idx, show_dict in dialog.slot_shows.items():
#                 checkbox_data[str(slot_idx)] = {
#                     str(code): checked
#                     for code, checked in show_dict.items()
#                 }
#             settings.setValue(f"file_slot_shows/{file_key}", checkbox_data)
#             print(f"  ✅ Saved checkbox states for {len(checkbox_data)} views")
        
#         # 4️⃣ Save display mode (rgb, class, intensity, etc.)
#         if hasattr(app, 'display_mode') and app.display_mode:
#             settings.setValue(f"file_display_mode/{file_key}", app.display_mode)
#             print(f"  ✅ Display mode: {app.display_mode}")
        
#         # 5️⃣ Save color mode index from dialog
#         if dialog and hasattr(dialog, 'color_mode'):
#             color_mode_idx = dialog.color_mode.currentIndex()
#             settings.setValue(f"file_color_mode/{file_key}", color_mode_idx)
#             print(f"  ✅ Color mode index: {color_mode_idx}")
        
#         settings.sync()
#         print(f"✅ Display settings saved for: {os.path.basename(file_key)}\n")
#         try:
#             ptc_name = os.path.basename(dialog.current_ptc_path) if (dialog and getattr(dialog, "current_ptc_path", None)) else "None"
#             palette_slots = len(getattr(app, "view_palettes", {}) or {})
#             show_slots = len(getattr(dialog, "slot_shows", {}) or {}) if dialog else 0
#             print(
#                 f"[RUNTIME-CHECK] save-before-clear scope=FILE "
#                 f"file={os.path.basename(file_key)} ptc={ptc_name} "
#                 f"palette_slots={palette_slots} show_slots={show_slots}"
#             )
#         except Exception:
#             pass
        
#     except Exception as e:
#         print(f"⚠️ Failed to save display settings: {e}")
#         import traceback
#         traceback.print_exc()


# def clear_project(app):
#     """
#     Clear all loaded files and reset state, with confirmation.
#     ✅ EXECUTION ORDER: Close cut view → Clear main view → Clear cross-section views
#     ✅ Display settings preserved, Display Mode dialog kept intact
#     """
#     # ✅ FIX: Explicitly clear the shading cache to prevent stale data between projects
#     clear_shading_cache(reason="project_clear")
#     # Also null out the rendered-cache-key and stale mesh actors so that
#     app._rendered_shading_cache_key = None
#     app._shaded_mesh_actor = None
#     app._shaded_mesh_polydata = None

#     # ============================================================================
#     # ✅ CHECK IF PROJECT HAS UNSAVED DATA
#     # ============================================================================
#     should_save = False
    
#     if hasattr(app, 'data') and app.data is not None:
#         # Create custom message box with Save/Don't Save/Cancel options
#         msg_box = QMessageBox(app)
#         msg_box.setIcon(QMessageBox.Question)
#         msg_box.setWindowTitle("Clear Project")
#         msg_box.setText("Do you want to save the current project before clearing?")
#         msg_box.setInformativeText("Your display settings will be preserved and restored when you reload the file.")
        
#         # Add custom buttons
#         save_button = msg_box.addButton("Save", QMessageBox.AcceptRole)
#         dont_save_button = msg_box.addButton("Don't Save", QMessageBox.DestructiveRole)
#         cancel_button = msg_box.addButton("Cancel", QMessageBox.RejectRole)
        
#         msg_box.setDefaultButton(save_button)
#         msg_box.setEscapeButton(cancel_button)
        
#         # Show dialog and get response
#         msg_box.exec()
#         clicked_button = msg_box.clickedButton()
        
#         if clicked_button == cancel_button:
#             print("❌ Clear project cancelled by user")
#             return
#         elif clicked_button == save_button:
#             should_save = True
#             print("✅ User chose to save before clearing")
#         else:  # don't_save_button
#             should_save = False
#             print("⚠️ User chose NOT to save before clearing")
#     else:
#         # No data loaded, just confirm clear
#         reply = QMessageBox.question(
#             app,
#             "Clear Project",
#             "Are you sure you want to clear the project?",
#             QMessageBox.Yes | QMessageBox.No,
#             QMessageBox.No
#         )
#         if reply == QMessageBox.No:
#             return

#     try:
#         print("\n" + "="*60)
#         if should_save:
#             print("🧹 CLEARING PROJECT (SAVING PROJECT & DISPLAY SETTINGS)")
#         else:
#             print("🧹 CLEARING PROJECT (NOT SAVING - DISPLAY SETTINGS WILL BE PRESERVED)")
#         print("="*60)

#         # ============================================================================
#         # ✅ SAVE THE PROJECT IF USER CHOSE TO SAVE
#         # ============================================================================
#         if should_save and hasattr(app, 'data') and app.data is not None:
#             save_path = None
            
#             # Determine save path
#             if hasattr(app, 'last_save_path') and app.last_save_path:
#                 save_path = app.last_save_path
#             elif hasattr(app, 'loaded_file') and app.loaded_file:
#                 save_path = app.loaded_file
            
#             if save_path:
#                 try:
#                     print(f"\n💾 Saving project to: {os.path.basename(save_path)}")
                    
#                     from .save_pointcloud import save_pointcloud_quick
                    
#                     # Save without showing dialog
#                     save_pointcloud_quick(app, save_path)
                    
#                     print(f"✅ Project saved successfully before clearing")
                    
#                     if hasattr(app, "statusBar"):
#                         app.statusBar().showMessage(f"✅ Saved: {os.path.basename(save_path)}", 3000)
                    
#                 except Exception as save_error:
#                     print(f"⚠️ Error saving: {save_error}")
#                     if hasattr(app, "statusBar"):
#                         app.statusBar().showMessage(f"⚠️ Save failed: {save_error}", 5000)
#             else:
#                 print("⚠️ No save path available - skipping save")

#         # ============================================================================
#         # ✅ SAVE DISPLAY SETTINGS BEFORE CLEARING
#         # IMPORTANT: Do this BEFORE any dialog reset/clear operations so
#         # current_ptc_path and palette state are preserved correctly.
#         # ============================================================================
#         _save_display_settings_before_clear(app)

#         # NOTE:
#         # We intentionally do NOT call display_mode_dialog.reset_dialog() here.
#         # Clearing that dialog before save can wipe active PTC/palette state and
#         # make the next file load lose expected display settings.
#         print("✅ Display mode state preserved (no pre-clear dialog reset)")

#         # ============================================================================
#         # STEP 1: Close Cut Section FIRST (let it complete naturally)
#         # ============================================================================
#         if hasattr(app, "cut_section_controller") and app.cut_section_controller:
#             ctrl = app.cut_section_controller
#             try:
#                 print(f"\n🔒 Closing cut section dock...")
                
#                 # Close dock - let closeEvent run naturally
#                 if ctrl.cut_dock is not None:
#                     try:
#                         ctrl.cut_dock.close()
#                         ctrl.cut_dock = None
#                         print("  ✅ Cut dock closed")
#                     except Exception as e:
#                         print(f"  ⚠️ Dock close: {e}")
#                         ctrl.cut_dock = None
                
#                 # Clear VTK
#                 if ctrl.cut_vtk is not None:
#                     try:
#                         renderer = ctrl.cut_vtk.renderer
#                         renderer.RemoveAllViewProps()
#                         ctrl.cut_vtk.close()
#                         ctrl.cut_vtk = None
#                         print("  ✅ Cut VTK cleared")
#                     except Exception as e:
#                         print(f"  ⚠️ VTK clear: {e}")
#                         ctrl.cut_vtk = None
                
#                 # Reset state
#                 ctrl.is_cut_view_active = False
#                 ctrl._state = 0
#                 ctrl.cut_points = None
                
#                 print("✅ Cut section closed")
                
#             except Exception as e:
#                 print(f"⚠️ Cut section close failed: {e}")

#         # ============================================================================
#         # STEP 2: Clear Main View
#         # ============================================================================
#         if hasattr(app, "vtk_widget") and app.vtk_widget:
#             try:
#                 renderer = app.vtk_widget.renderer
                
#                 # Store DXF actors if they exist (to preserve them)
#                 dxf_actors_backup = []
#                 if hasattr(app, 'dxf_actors') and app.dxf_actors:
#                     for dxf_data in app.dxf_actors:
#                         if 'actors' in dxf_data:
#                             dxf_actors_backup.extend(dxf_data['actors'])
#                     print(f"\n✅ Backing up {len(dxf_actors_backup)} DXF actors")
                
#                 # Store digitizer/drawing actors if they exist
#                 drawing_actors_backup = []
#                 if hasattr(app, 'digitizer') and app.digitizer:
#                     if hasattr(app.digitizer, 'actors') and app.digitizer.actors:
#                         drawing_actors_backup = list(app.digitizer.actors)
#                         print(f"✅ Backing up {len(drawing_actors_backup)} drawing actors")
                
#                 # Remove ALL actors from renderer
#                 renderer.RemoveAllViewProps()
#                 print("✅ Removed all point cloud actors from main view")
                
#                 # Re-add ONLY DXF actors
#                 for actor in dxf_actors_backup:
#                     renderer.AddActor(actor)
                
#                 # Re-add drawing actors
#                 for actor in drawing_actors_backup:
#                     renderer.AddActor(actor)
                
#                 if dxf_actors_backup or drawing_actors_backup:
#                     print(f"✅ Restored {len(dxf_actors_backup)} DXF + {len(drawing_actors_backup)} drawing actors")
                
#                 # Render to show cleared view
#                 app.vtk_widget.render()
#                 print(f"✅ Main viewer cleared (preserved: {len(dxf_actors_backup) + len(drawing_actors_backup)} actors)")
                    
#             except Exception as e:
#                 print(f"⚠️ VTK clear failed: {e}")
#                 import traceback
#                 traceback.print_exc()

#         # ============================================================================
#         # STEP 3: Clear Cross-Section Views (AFTER cut is closed)
#         # ============================================================================
#         if hasattr(app, 'section_vtks') and app.section_vtks:
#             print(f"\n🔄 Clearing point cloud data from {len(app.section_vtks)} cross-section views...")
            
#             for view_idx, vtk_widget in app.section_vtks.items():
#                 try:
#                     if vtk_widget and hasattr(vtk_widget, 'renderer'):
#                         vtk_widget.renderer.RemoveAllViewProps()
#                         vtk_widget.render()
#                     print(f"  ✅ Cleared cross-section view {view_idx}")
#                 except Exception as e:
#                     print(f"  ⚠️ Failed to clear view {view_idx}: {e}")
            
#             print(f"✅ All cross-section views cleared (docks remain open)")

#         # ============================================================================
#         # STEP 4: Clear Section Controller
#         # ============================================================================
#         if hasattr(app, "section_controller") and app.section_controller:
#             try:
#                 ctrl = app.section_controller
                
#                 # Clear sections data
#                 if hasattr(ctrl, 'sections'):
#                     ctrl.sections.clear()
                
#                 # Clear stored cross-section data
#                 for view_idx in range(4):
#                     section_view = getattr(ctrl, f"section_view_{view_idx}", None)
#                     if section_view and hasattr(section_view, 'vtk_widget'):
#                         try:
#                             if hasattr(section_view.vtk_widget, 'renderer'):
#                                 section_view.vtk_widget.renderer.RemoveAllViewProps()
#                                 section_view.vtk_widget.render()
#                         except Exception as e:
#                             print(f"  ⚠️ Failed to clear section_view_{view_idx}: {e}")
                
#                 # Reset state flags
#                 if hasattr(ctrl, 'active_view'):
#                     ctrl.active_view = 0
#                 if hasattr(ctrl, 'section_line'):
#                     ctrl.section_line = None
                
#                 print("✅ Section controller cleared")
#             except Exception as e:
#                 print(f"⚠️ Section controller clear failed: {e}")

#         # ============================================================================
#         # Clear Layers
#         # ============================================================================
#         app.layers = []
#         if hasattr(app, "layers_dock") and app.layers_dock:
#             try:
#                 app.layers_dock.clear_layers()
#                 print("✅ Layers cleared")
#             except Exception as e:
#                 print(f"⚠️ Layers clear failed: {e}")

#         # ============================================================================
#         # Clear Digitizer Drawings
#         # ============================================================================
#         if hasattr(app, "digitizer") and app.digitizer:
#             try:
#                 app.digitizer.clear_drawings()
#                 print("✅ Drawings cleared")
#             except Exception as e:
#                 print(f"⚠️ Drawings clear failed: {e}")

#         # Clear Internal State
#         try:
#             from .memory_manager import ObserverRegistry, release_data_arrays
#             from .unified_actor_manager import reset_uam

#             # ✅ Thoroughly reset the Unified Actor Manager (wipes GPU contexts, LUTs, and buffers)
#             reset_uam(app)
            
#             release_data_arrays(app)
#             ObserverRegistry.release_all()

#             mem_guard = getattr(app, "_mem_guard", None)
#             if mem_guard is not None:
#                 mem_guard.force_gc()
#         except Exception as e:
#             print(f"⚠️ Memory manager cleanup skipped: {e}")

#         app.data = None
#         app.project_crs_epsg = None
#         app.project_crs_wkt = None
#         app.last_save_path = None
#         app.loaded_file = None
#         # ✅ FIX: Clear stale Z-bounds cache so SNT actors are positioned correctly
#         # relative to the next LAZ file loaded. If this is not reset, _get_snt_z_offset
#         # reads old z_min/z_max and places the SNT grid at the wrong height.
#         app.data_bounds = None
        
#         # app.class_palette and app.view_palettes are GLOBAL PTC state.
#         # They are preserved across file clears so the current PTC stays
#         # active for the next file without requiring a restore from QSettings.
#         # Null out subsampling indices - MUST be recalculated for the new file
#         app._main_global_indices = None
#         app._main_global_mask = None
#         app._main_lod_step = None

#         print("✅ Internal state cleared (PTC/palette preserved as global state)")



#         # ============================================================================
#         # Clear Stored Section Data
#         # ============================================================================
#         for i in range(4):
#             for attr in [f"section_{i}_core_points", f"section_{i}_buffer_points",
#                         f"section_{i}_core_mask", f"section_{i}_buffer_mask"]:
#                 if hasattr(app, attr):
#                     try:
#                         delattr(app, attr)
#                     except Exception:
#                         pass
#         print("✅ Stored section data cleared")

#         # ============================================================================
#         # Clear Undo/Redo Stacks
#         # ============================================================================
#         if hasattr(app, "undo_stack"):
#             app.undo_stack.clear()
#         if hasattr(app, "redo_stack"):
#             app.redo_stack.clear()
#         print("✅ Undo/redo history cleared")

#         # ============================================================================
#         # ✅ KEEP Display Mode Dialog COMPLETELY INTACT
#         # Do NOT touch it - keep PTC loaded, keep table, keep all settings
#         # ============================================================================
#         if hasattr(app, "display_dialog") and app.display_dialog:
#             try:
#                 print("✅ Display Mode dialog kept intact (PTC and table preserved)")
#             except Exception as e:
#                 print(f"⚠️ Display Mode check failed: {e}")

#         if hasattr(app, "display_mode_dialog") and app.display_mode_dialog:
#             try:
#                 print("✅ Display Mode dialog kept intact (PTC and table preserved)")
#             except Exception as e:
#                 print(f"⚠️ Display Mode check failed: {e}")

#         # ============================================================================
#         # Close Class Picker
#         # ============================================================================
#         if hasattr(app, "class_picker") and app.class_picker:
#             try:
#                 app.class_picker.close()
#                 app.class_picker.deleteLater()
#                 app.class_picker = None
#                 print("✅ Class Picker closed")
#             except Exception as e:
#                 print(f"⚠️ Class Picker close failed: {e}")

#         # ============================================================================
#         # Close Shading Control Panel
#         # ============================================================================
#         if hasattr(app, "shading_dock") and app.shading_dock:
#             try:
#                 if hasattr(app, "removeDockWidget") and hasattr(app.shading_dock, "setWidget"):
#                     app.removeDockWidget(app.shading_dock)
#                 else:
#                     app.shading_dock.close()
#                 app.shading_dock.deleteLater()
#                 app.shading_dock = None
#                 if hasattr(app, "shading_panel"):
#                     app.shading_panel = None
#                 print("✅ Shading dock closed")
#             except Exception as e:
#                 print(f"⚠️ Shading dock close failed: {e}")

#         # ============================================================================
#         # Clear Point Statistics Widget
#         # ============================================================================
#         if hasattr(app, "point_count_widget") and app.point_count_widget:
#             try:
#                 if hasattr(app.point_count_widget, 'clear_statistics'):
#                     app.point_count_widget.clear_statistics()
#                 else:
#                     if hasattr(app.point_count_widget, 'total_label'):
#                         app.point_count_widget.total_label.setText("Total: 0")
#                     if hasattr(app.point_count_widget, 'visible_label'):
#                         app.point_count_widget.visible_label.setText("Visible: 0")
#                     if hasattr(app.point_count_widget, 'class_tree'):
#                         app.point_count_widget.class_tree.clear()
#                 print("✅ Point statistics cleared")
#             except Exception as e:
#                 print(f"⚠️ Statistics clear failed: {e}")

#         # ============================================================================
#         # Clear Measurement Tools
#         # ============================================================================
#         if hasattr(app, 'measurement_tool') and app.measurement_tool is not None:
#             try:
#                 app.measurement_tool.clear_all_measurements()
#                 print("✅ Measurement tools cleared")
#             except Exception as e:
#                 print(f"⚠️ Measurement clear failed: {e}")

#         # ============================================================================
#         # Reset Other States
#         # ============================================================================
#         app.active_classify_tool = None
#         if hasattr(app, "skip_main_view_refresh"):
#             app.skip_main_view_refresh = False
        
#         # ✅ Keep display_mode as is (don't reset to "rgb")
#         # User's preferred mode stays until next file loads
        
#         if hasattr(app, "spatial_index"):
#             app.spatial_index = None

#         # ============================================================================
#         # Update UI
#         # ============================================================================
#         app._update_window_title(None, None)

#         # ✅ CRITICAL: Restore digitizer overlay renderers (Layer 1/2) AFTER all cleanup
#         # Must run AFTER reset_uam, ObserverRegistry.release_all(), and clear_drawings()
#         # which may have detached or invalidated the overlay renderers.
#         if hasattr(app, "digitizer") and app.digitizer:
#             try:
#                 app.digitizer._check_and_update_renderers()
#                 app.digitizer._reinstall_all_observers()   # ← restore VTK observers wiped by release_all()
#                 print("✅ Digitizer fully restored (post-clear)")
#             except Exception as e:
#                 print(f"⚠️ Failed to restore digitizer: {e}")

#         if hasattr(app, "statusBar"):

#             if should_save:
#                 app.statusBar().showMessage("🧹 Project saved and cleared - Display settings preserved", 5000)
#             else:
#                 app.statusBar().showMessage("🧹 Project cleared - Display settings preserved", 5000)

#         print("="*60)
#         if should_save:
#             print("✅ PROJECT SAVED AND CLEARED (DISPLAY SETTINGS PRESERVED)")
#         else:
#             print("✅ PROJECT CLEARED (DISPLAY SETTINGS PRESERVED)")
#         print("="*60 + "\n")

#     except Exception as e:
#         error_msg = f"Failed to clear project: {e}"
#         print(f"❌ {error_msg}")
#         import traceback
#         traceback.print_exc()
        
#         if hasattr(app, "statusBar"):
#             app.statusBar().showMessage(f"⚠️ {error_msg}", 5000)
        
#         QMessageBox.critical(
#             app,
#             "Clear Project Error",
#             f"An error occurred while clearing the project:\n\n{e}\n\n"
#             "Some components may not have been cleared properly."
#         )


# def clear_point_cloud(app):
#     """
#     Clear only point cloud data and reset related state, with confirmation.
#     Preserves DXF, DWG, SNT attachments, drawings/digitizer layers, and PRJ data.
#     """
#     # Explicitly clear the shading cache to prevent stale data
#     clear_shading_cache(reason="point_cloud_clear")
#     app._rendered_shading_cache_key = None
#     app._shaded_mesh_actor = None
#     app._shaded_mesh_polydata = None

#     # Check if project has unsaved point cloud data
#     should_save = False
    
#     if hasattr(app, 'data') and app.data is not None:
#         msg_box = QMessageBox(app)
#         msg_box.setIcon(QMessageBox.Question)
#         msg_box.setWindowTitle("Clear Point Cloud")
#         msg_box.setText("Do you want to save the current point cloud before clearing?")
#         msg_box.setInformativeText("Your display settings will be preserved and restored when you reload the file.")
        
#         save_button = msg_box.addButton("Save", QMessageBox.AcceptRole)
#         dont_save_button = msg_box.addButton("Don't Save", QMessageBox.DestructiveRole)
#         cancel_button = msg_box.addButton("Cancel", QMessageBox.RejectRole)
        
#         msg_box.setDefaultButton(save_button)
#         msg_box.setEscapeButton(cancel_button)
        
#         msg_box.exec()
#         clicked_button = msg_box.clickedButton()
        
#         if clicked_button == cancel_button:
#             print("❌ Clear point cloud cancelled by user")
#             return
#         elif clicked_button == save_button:
#             should_save = True
#             print("✅ User chose to save before clearing")
#         else:
#             should_save = False
#             print("⚠️ User chose NOT to save before clearing")
#     else:
#         reply = QMessageBox.question(
#             app,
#             "Clear Point Cloud",
#             "Are you sure you want to clear the point cloud data?",
#             QMessageBox.Yes | QMessageBox.No,
#             QMessageBox.No
#         )
#         if reply == QMessageBox.No:
#             return

#     try:
#         print("\n" + "="*60)
#         if should_save:
#             print("🧹 CLEARING POINT CLOUD (SAVING POINT CLOUD & DISPLAY SETTINGS)")
#         else:
#             print("🧹 CLEARING POINT CLOUD (NOT SAVING - DISPLAY SETTINGS WILL BE PRESERVED)")
#         print("="*60)

#         # Save the project if user chose to save
#         if should_save and hasattr(app, 'data') and app.data is not None:
#             save_path = None
#             if hasattr(app, 'last_save_path') and app.last_save_path:
#                 save_path = app.last_save_path
#             elif hasattr(app, 'loaded_file') and app.loaded_file:
#                 save_path = app.loaded_file
            
#             if save_path:
#                 try:
#                     print(f"\n💾 Saving point cloud to: {os.path.basename(save_path)}")
#                     from .save_pointcloud import save_pointcloud_quick
#                     save_pointcloud_quick(app, save_path)
#                     print(f"✅ Point cloud saved successfully before clearing")
#                     if hasattr(app, "statusBar"):
#                         app.statusBar().showMessage(f"✅ Saved: {os.path.basename(save_path)}", 3000)
#                 except Exception as save_error:
#                     print(f"⚠️ Error saving: {save_error}")
#                     if hasattr(app, "statusBar"):
#                         app.statusBar().showMessage(f"⚠️ Save failed: {save_error}", 5000)
#             else:
#                 print("⚠️ No save path available - skipping save")

#         # Save display settings before clearing
#         _save_display_settings_before_clear(app)

#         # Close Cut Section
#         if hasattr(app, "cut_section_controller") and app.cut_section_controller:
#             ctrl = app.cut_section_controller
#             try:
#                 print(f"\n🔒 Closing cut section dock...")
#                 if ctrl.cut_dock is not None:
#                     try:
#                         ctrl.cut_dock.close()
#                         ctrl.cut_dock = None
#                         print("  ✅ Cut dock closed")
#                     except Exception as e:
#                         print(f"  ⚠️ Dock close: {e}")
#                         ctrl.cut_dock = None
#                 if ctrl.cut_vtk is not None:
#                     try:
#                         renderer = ctrl.cut_vtk.renderer
#                         renderer.RemoveAllViewProps()
#                         ctrl.cut_vtk.close()
#                         ctrl.cut_vtk = None
#                         print("  ✅ Cut VTK cleared")
#                     except Exception as e:
#                         print(f"  ⚠️ VTK clear: {e}")
#                         ctrl.cut_vtk = None
#                 ctrl.is_cut_view_active = False
#                 ctrl._state = 0
#                 ctrl.cut_points = None
#                 print("✅ Cut section closed")
#             except Exception as e:
#                 print(f"⚠️ Cut section close failed: {e}")

#         # Clear Main View (preserving DXF, DWG, SNT, and drawing/digitizer actors)
#         if hasattr(app, "vtk_widget") and app.vtk_widget:
#             try:
#                 renderer = app.vtk_widget.renderer
                
#                 # Backup DXF actors
#                 dxf_actors_backup = []
#                 if hasattr(app, 'dxf_actors') and app.dxf_actors:
#                     for dxf_data in app.dxf_actors:
#                         if 'actors' in dxf_data:
#                             dxf_actors_backup.extend(dxf_data['actors'])
                
#                 # Backup DWG actors
#                 dwg_actors_backup = []
#                 if hasattr(app, 'dwg_actors') and app.dwg_actors:
#                     for dwg_data in app.dwg_actors:
#                         if 'actors' in dwg_data:
#                             dwg_actors_backup.extend(dwg_data['actors'])

#                 # Backup SNT actors
#                 snt_actors_backup = []
#                 if hasattr(app, 'snt_actors') and app.snt_actors:
#                     for snt_data in app.snt_actors:
#                         if 'actors' in snt_data:
#                             snt_actors_backup.extend(snt_data['actors'])
                
#                 # Backup drawing actors
#                 drawing_actors_backup = []
#                 if hasattr(app, 'digitizer') and app.digitizer:
#                     if hasattr(app.digitizer, 'actors') and app.digitizer.actors:
#                         drawing_actors_backup = list(app.digitizer.actors)
                
#                 # Remove ALL actors from renderer
#                 renderer.RemoveAllViewProps()
#                 print("✅ Removed all actors from main view")
                
#                 # Re-add DXF actors
#                 for actor in dxf_actors_backup:
#                     renderer.AddActor(actor)
                
#                 # Re-add DWG actors
#                 for actor in dwg_actors_backup:
#                     renderer.AddActor(actor)

#                 # Re-add SNT actors
#                 # ✅ FIX: Reset _snt_z_offset on each preserved SNT actor so that
#                 # _apply_z_offset_to_actor computes the correct delta on the next
#                 # LAZ load. Without this reset, actors keep a stale offset from the
#                 # previous file and the grid appears at the wrong Z height.
#                 for actor in snt_actors_backup:
#                     actor._snt_z_offset = 0.0
#                     renderer.AddActor(actor)

#                 # Re-add drawing actors
#                 for actor in drawing_actors_backup:
#                     renderer.AddActor(actor)
                
#                 # Render to show cleared view
#                 app.vtk_widget.render()
#                 print(f"✅ Main viewer cleared point cloud (preserved DXF, DWG, SNT, and drawing actors)")
#             except Exception as e:
#                 print(f"⚠️ VTK clear failed: {e}")
#                 import traceback
#                 traceback.print_exc()

#         # Clear Cross-Section Views
#         if hasattr(app, 'section_vtks') and app.section_vtks:
#             print(f"\n🔄 Clearing point cloud data from {len(app.section_vtks)} cross-section views...")
#             for view_idx, vtk_widget in app.section_vtks.items():
#                 try:
#                     if vtk_widget and hasattr(vtk_widget, 'renderer'):
#                         vtk_widget.renderer.RemoveAllViewProps()
#                         vtk_widget.render()
#                     print(f"  ✅ Cleared cross-section view {view_idx}")
#                 except Exception as e:
#                     print(f"  ⚠️ Failed to clear view {view_idx}: {e}")
#             print(f"✅ All cross-section views cleared")

#         # Clear Section Controller
#         if hasattr(app, "section_controller") and app.section_controller:
#             try:
#                 ctrl = app.section_controller
#                 if hasattr(ctrl, 'sections'):
#                     ctrl.sections.clear()
#                 for view_idx in range(4):
#                     section_view = getattr(ctrl, f"section_view_{view_idx}", None)
#                     if section_view and hasattr(section_view, 'vtk_widget'):
#                         try:
#                             if hasattr(section_view.vtk_widget, 'renderer'):
#                                 section_view.vtk_widget.renderer.RemoveAllViewProps()
#                                 section_view.vtk_widget.render()
#                         except Exception as e:
#                             print(f"  ⚠️ Failed to clear section_view_{view_idx}: {e}")
#                 if hasattr(ctrl, 'active_view'):
#                     ctrl.active_view = 0
#                 if hasattr(ctrl, 'section_line'):
#                     ctrl.section_line = None
#                 print("✅ Section controller cleared")
#             except Exception as e:
#                 print(f"⚠️ Section controller clear failed: {e}")

#         # Clear point cloud layers
#         app.layers = []
#         if hasattr(app, "layers_dock") and app.layers_dock:
#             try:
#                 app.layers_dock.clear_layers()
#                 print("✅ Layers cleared")
#             except Exception as e:
#                 print(f"⚠️ Layers clear failed: {e}")

#         # Clear Internal State / Memory
#         try:
#             from .memory_manager import ObserverRegistry, release_data_arrays
#             from .unified_actor_manager import reset_uam

#             reset_uam(app)
#             release_data_arrays(app)
#             ObserverRegistry.release_all()

#             mem_guard = getattr(app, "_mem_guard", None)
#             if mem_guard is not None:
#                 mem_guard.force_gc()
#         except Exception as e:
#             print(f"⚠️ Memory manager cleanup skipped: {e}")

#         app.data = None
#         app.last_save_path = None
#         app.loaded_file = None
#         # ✅ FIX: Clear stale Z-bounds cache — same reason as in clear_project().
#         app.data_bounds = None
        

#         # app.class_palette and app.view_palettes are GLOBAL PTC state — preserved.
#         app._main_global_indices = None
#         app._main_global_mask = None
#         app._main_lod_step = None


#         # Clear Stored Section Data

#         for i in range(4):
#             for attr in [f"section_{i}_core_points", f"section_{i}_buffer_points",
#                         f"section_{i}_core_mask", f"section_{i}_buffer_mask"]:
#                 if hasattr(app, attr):
#                     try:
#                         delattr(app, attr)
#                     except Exception:
#                         pass

#         # Clear Undo/Redo Stacks
#         if hasattr(app, "undo_stack"):
#             app.undo_stack.clear()
#         if hasattr(app, "redo_stack"):
#             app.redo_stack.clear()
#         print("✅ Undo/redo history cleared")

#         # Close Class Picker
#         if hasattr(app, "class_picker") and app.class_picker:
#             try:
#                 app.class_picker.close()
#                 app.class_picker.deleteLater()
#                 app.class_picker = None
#                 print("✅ Class Picker closed")
#             except Exception as e:
#                 print(f"⚠️ Class Picker close failed: {e}")

#         # Close Shading Control Panel
#         if hasattr(app, "shading_dock") and app.shading_dock:
#             try:
#                 if hasattr(app, "removeDockWidget") and hasattr(app.shading_dock, "setWidget"):
#                     app.removeDockWidget(app.shading_dock)
#                 else:
#                     app.shading_dock.close()
#                 app.shading_dock.deleteLater()
#                 app.shading_dock = None
#                 if hasattr(app, "shading_panel"):
#                     app.shading_panel = None
#                 print("✅ Shading dock closed")
#             except Exception as e:
#                 print(f"⚠️ Shading dock close failed: {e}")

#         # Clear Point Statistics Widget
#         if hasattr(app, "point_count_widget") and app.point_count_widget:
#             try:
#                 if hasattr(app.point_count_widget, 'clear_statistics'):
#                     app.point_count_widget.clear_statistics()
#                 else:
#                     if hasattr(app.point_count_widget, 'total_label'):
#                         app.point_count_widget.total_label.setText("Total: 0")
#                     if hasattr(app.point_count_widget, 'visible_label'):
#                         app.point_count_widget.visible_label.setText("Visible: 0")
#                     if hasattr(app.point_count_widget, 'class_tree'):
#                         app.point_count_widget.class_tree.clear()
#                 print("✅ Point statistics cleared")
#             except Exception as e:
#                 print(f"⚠️ Statistics clear failed: {e}")

#         # Reset Other States
#         app.active_classify_tool = None
#         if hasattr(app, "skip_main_view_refresh"):
#             app.skip_main_view_refresh = False
        
#         if hasattr(app, "spatial_index"):
#             app.spatial_index = None

#         # Update UI
#         app._update_window_title(None, app.project_crs_epsg)

#         # ✅ CRITICAL: Restore digitizer overlay renderers (Layer 1/2) AFTER all cleanup
#         # Must run AFTER reset_uam, ObserverRegistry.release_all(), and clear_drawings()
#         # which may have detached or invalidated the overlay renderers.
#         if hasattr(app, "digitizer") and app.digitizer:
#             try:
#                 app.digitizer._check_and_update_renderers()
#                 app.digitizer._reinstall_all_observers()   # ← restore VTK observers wiped by release_all()
#                 print("✅ Digitizer fully restored (post-clear-pointcloud)")
#             except Exception as e:
#                 print(f"⚠️ Failed to restore digitizer: {e}")

#         if hasattr(app, "statusBar"):
#             if should_save:
#                 app.statusBar().showMessage("🧹 Point cloud saved and cleared - Display settings preserved", 5000)
#             else:
#                 app.statusBar().showMessage("🧹 Point cloud cleared - Display settings preserved", 5000)

#         print("="*60)
#         if should_save:
#             print("✅ POINT CLOUD SAVED AND CLEARED")
#         else:
#             print("✅ POINT CLOUD CLEARED")
#         print("="*60 + "\n")

#     except Exception as e:
#         error_msg = f"Failed to clear point cloud: {e}"
#         print(f"❌ {error_msg}")
#         import traceback
#         traceback.print_exc()
        
#         if hasattr(app, "statusBar"):
#             app.statusBar().showMessage(f"⚠️ {error_msg}", 5000)
        
#         QMessageBox.critical(
#             app,
#             "Clear Point Cloud Error",
#             f"An error occurred while clearing the point cloud:\n\n{e}\n\n"
#             "Some components may not have been cleared properly."
#         )




from PySide6.QtWidgets import QMessageBox
from PySide6.QtCore import QSettings
import os
from .shading_display import clear_shading_cache
from .display_mode import clone_palette
from .theme_manager import get_dialog_stylesheet


def _save_display_settings_before_clear(app):
    """
    Save current display mode settings to QSettings before clearing.
    ✅ FIXED: Saves PTC, palettes, checkboxes, color mode, and display mode.
    """
    try:
        settings = QSettings("NakshaAI", "LidarApp")
        
        # Identify the best active dialog instance (prefer visible, then populated).
        dialog = None
        _candidates = []
        for _attr in ("display_mode_dialog", "display_dialog"):
            _d = getattr(app, _attr, None)
            if _d is None:
                continue
            if all(id(_d) != id(_x) for _x in _candidates):
                _candidates.append(_d)

        for _d in _candidates:
            try:
                if hasattr(_d, "isVisible") and _d.isVisible():
                    dialog = _d
                    break
            except Exception:
                continue
        if dialog is None:
            for _d in _candidates:
                try:
                    _vp = getattr(_d, "view_palettes", None)
                    if isinstance(_vp, dict) and _vp:
                        dialog = _d
                        break
                except Exception:
                    continue
        if dialog is None and _candidates:
            dialog = _candidates[0]

        # Flush any in-progress table edits into dialog palette state so the
        # latest user-facing values are captured before serialization.
        if dialog and hasattr(dialog, "_save_slot_state"):
            try:
                dialog._save_slot_state(int(getattr(dialog, "current_slot", 0)))
            except Exception:
                pass

        # Sync dialog palettes into app.view_palettes to avoid stale app/dialog
        # divergence during grid-switch save-before-clear.
        if dialog and hasattr(dialog, "view_palettes"):
            if not hasattr(app, "view_palettes") or app.view_palettes is None:
                app.view_palettes = {}
            try:
                for _slot_idx, _slot_palette in (dialog.view_palettes or {}).items():
                    try:
                        slot_i = int(_slot_idx)
                    except Exception:
                        continue
                    if isinstance(_slot_palette, dict):
                        app.view_palettes[slot_i] = clone_palette(_slot_palette)
            except Exception:
                pass
            
        # # ====================================================================
        # # ✅ Save Global Last Used PTC (Independent of any file)
        # # ====================================================================
        # if dialog and hasattr(dialog, 'current_ptc_path') and dialog.current_ptc_path:
        #     settings.setValue("global_last_ptc_path", dialog.current_ptc_path)
        #     print(f"🌍 Saved GLOBAL PTC: {os.path.basename(dialog.current_ptc_path)}")

        # # ====================================================================
        # # ✅ NEW: Save GLOBAL display settings (independent of specific files)
        # # These will be applied to ANY new file loaded
        # # ====================================================================
        # if dialog:
        #     palette_source = None
        #     if hasattr(dialog, 'view_palettes') and dialog.view_palettes:
        #         palette_source = dialog.view_palettes
        #     elif hasattr(app, 'view_palettes') and app.view_palettes:
        #         palette_source = app.view_palettes

        #     # Save ALL view palettes globally
        #     if palette_source:
        #         global_palette_data = {}
        #         for view_idx, palette in palette_source.items():
        #             global_palette_data[str(view_idx)] = {
        #                 str(code): {
        #                     'show': info.get('show', True),
        #                     'description': info.get('description', ''),
        #                     'lvl': info.get('lvl', ''),
        #                     'color': list(info.get('color', (128, 128, 128))),
        #                     'weight': info.get('weight', 1.0)
        #                 }
        #                 for code, info in palette.items()
        #             }
        #         settings.setValue("global_view_palettes", global_palette_data)
        #         print(f"  ✅ Saved {len(global_palette_data)} GLOBAL view palettes")
            
            # Save ALL checkbox states globally
            if hasattr(dialog, 'slot_shows') and dialog.slot_shows:
                global_checkbox_data = {}
                for slot_idx, show_dict in dialog.slot_shows.items():
                    global_checkbox_data[str(slot_idx)] = {
                        str(code): checked
                        for code, checked in show_dict.items()
                    }
                settings.setValue("global_slot_shows", global_checkbox_data)
                print(f"  ✅ Saved GLOBAL checkbox states for {len(global_checkbox_data)} views")
            
            # Save color mode globally
            if hasattr(dialog, 'color_mode'):
                color_mode_idx = dialog.color_mode.currentIndex()
                settings.setValue("global_color_mode", color_mode_idx)
                print(f"  ✅ Saved GLOBAL color mode: {color_mode_idx}")
            
            # Save border values globally
            if hasattr(dialog, 'view_borders') and dialog.view_borders:
                settings.setValue("global_view_borders", dialog.view_borders)
                print(f"  ✅ Saved GLOBAL border values: {dialog.view_borders}")

        # Save display mode globally
        if hasattr(app, 'display_mode') and app.display_mode:
            settings.setValue("global_display_mode", app.display_mode)
            print(f"  ✅ Saved GLOBAL display mode: {app.display_mode}")

        # ====================================================================
        # File-Specific Settings (Only if a file is loaded)
        # ====================================================================
        if not hasattr(app, 'loaded_file') or not app.loaded_file:
            settings.sync()
            print("✅ GLOBAL display settings saved\n")
            try:
                print("[RUNTIME-CHECK] save-before-clear scope=GLOBAL_ONLY loaded_file=None")
            except Exception:
                pass
            return
        
        file_key = os.path.abspath(app.loaded_file) 

        print(f"🧹 Clearing per-file display settings for: {os.path.basename(file_key)}")
        for key_prefix in (
            "file_ptc/",
            "file_palettes/",
            "file_slot_shows/",
            "file_display_mode/",
            "file_color_mode/",
        ):
            settings.remove(f"{key_prefix}{file_key}")
        settings.sync()
        print(f"✅ Per-file display settings cleared for: {os.path.basename(file_key)}\n")
        try:
            ptc_name = os.path.basename(dialog.current_ptc_path) if (dialog and getattr(dialog, "current_ptc_path", None)) else "None"
            palette_slots = len(getattr(app, "view_palettes", {}) or {})
            show_slots = len(getattr(dialog, "slot_shows", {}) or {}) if dialog else 0
            print(
                f"[RUNTIME-CHECK] save-before-clear scope=FILE_DISABLED "
                f"file={os.path.basename(file_key)} ptc={ptc_name} "
                f"palette_slots={palette_slots} show_slots={show_slots}"
            )
        except Exception:
            pass
        return

        print(f"💾 Saving display settings for: {os.path.basename(file_key)}")
        
        # # 1️⃣ Save current PTC path for THIS specific file
        # if dialog and hasattr(dialog, 'current_ptc_path') and dialog.current_ptc_path:
        #     settings.setValue(f"file_ptc/{file_key}", dialog.current_ptc_path)
        #     print(f"  ✅ PTC: {os.path.basename(dialog.current_ptc_path)}")
        
        # # 2️⃣ Save view palettes (colors, weights, descriptions for all views)
        # # Prefer app.view_palettes after slot-sync above (canonical runtime state).
        # palette_source = None
        # if dialog and hasattr(dialog, 'view_palettes') and dialog.view_palettes:
        #     palette_source = dialog.view_palettes
        # elif hasattr(app, 'view_palettes') and app.view_palettes:
        #     palette_source = app.view_palettes

        # if palette_source:
        #     palette_data = {}
        #     for view_idx, palette in palette_source.items():
        #         palette_data[str(view_idx)] = {
        #             str(code): {
        #                 'show': info.get('show', True),
        #                 'description': info.get('description', ''),
        #                 'lvl': info.get('lvl', ''),
        #                 'color': list(info.get('color', (128, 128, 128))),  # Convert tuple to list for JSON
        #                 'weight': info.get('weight', 1.0)
        #             }
        #             for code, info in palette.items()
        #         }
        #     settings.setValue(f"file_palettes/{file_key}", palette_data)
        #     print(f"  ✅ Saved {len(palette_data)} view palettes")
        
        # 3️⃣ Save checkbox states (slot_shows)
        if dialog and hasattr(dialog, 'slot_shows') and dialog.slot_shows:
            checkbox_data = {}
            for slot_idx, show_dict in dialog.slot_shows.items():
                checkbox_data[str(slot_idx)] = {
                    str(code): checked
                    for code, checked in show_dict.items()
                }
            settings.setValue(f"file_slot_shows/{file_key}", checkbox_data)
            print(f"  ✅ Saved checkbox states for {len(checkbox_data)} views")
        
        # 4️⃣ Save display mode (rgb, class, intensity, etc.)
        if hasattr(app, 'display_mode') and app.display_mode:
            settings.setValue(f"file_display_mode/{file_key}", app.display_mode)
            print(f"  ✅ Display mode: {app.display_mode}")
        
        # 5️⃣ Save color mode index from dialog
        if dialog and hasattr(dialog, 'color_mode'):
            color_mode_idx = dialog.color_mode.currentIndex()
            settings.setValue(f"file_color_mode/{file_key}", color_mode_idx)
            print(f"  ✅ Color mode index: {color_mode_idx}")
        
        settings.sync()
        print(f"✅ Display settings saved for: {os.path.basename(file_key)}\n")
        try:
            ptc_name = os.path.basename(dialog.current_ptc_path) if (dialog and getattr(dialog, "current_ptc_path", None)) else "None"
            palette_slots = len(getattr(app, "view_palettes", {}) or {})
            show_slots = len(getattr(dialog, "slot_shows", {}) or {}) if dialog else 0
            print(
                f"[RUNTIME-CHECK] save-before-clear scope=FILE "
                f"file={os.path.basename(file_key)} ptc={ptc_name} "
                f"palette_slots={palette_slots} show_slots={show_slots}"
            )
        except Exception:
            pass
        
    except Exception as e:
        print(f"⚠️ Failed to save display settings: {e}")
        import traceback
        traceback.print_exc()


def clear_project(app):
    """
    Clear all loaded files and reset state, with confirmation.
    ✅ EXECUTION ORDER: Close cut view → Clear main view → Clear cross-section views
    ✅ Display settings preserved, Display Mode dialog kept intact
    """
    # ✅ FIX: Explicitly clear the shading cache to prevent stale data between projects
    clear_shading_cache(reason="project_clear")
    # Also null out the rendered-cache-key and stale mesh actors so that
    app._rendered_shading_cache_key = None
    app._shaded_mesh_actor = None
    app._shaded_mesh_polydata = None

    # ============================================================================
    # ✅ CHECK IF PROJECT HAS UNSAVED DATA
    # ============================================================================
    should_save = False
    
    if hasattr(app, 'data') and app.data is not None:
        # Create custom message box with Save/Don't Save/Cancel options
        msg_box = QMessageBox(app)
        msg_box.setStyleSheet(get_dialog_stylesheet())
        msg_box.setIcon(QMessageBox.Question)
        msg_box.setWindowTitle("Clear Project")
        msg_box.setText("Do you want to save the current project before clearing?")
        msg_box.setInformativeText("Your display settings will be preserved and restored when you reload the file.")
        
        # Add custom buttons
        save_button = msg_box.addButton("Save", QMessageBox.AcceptRole)
        dont_save_button = msg_box.addButton("Don't Save", QMessageBox.DestructiveRole)
        cancel_button = msg_box.addButton("Cancel", QMessageBox.RejectRole)
        
        msg_box.setDefaultButton(save_button)
        msg_box.setEscapeButton(cancel_button)
        
        # Show dialog and get response
        msg_box.exec()
        clicked_button = msg_box.clickedButton()
        
        if clicked_button == cancel_button:
            print("❌ Clear project cancelled by user")
            return
        elif clicked_button == save_button:
            should_save = True
            print("✅ User chose to save before clearing")
        else:  # don't_save_button
            should_save = False
            print("⚠️ User chose NOT to save before clearing")
    else:
        # No data loaded, just confirm clear
        reply = QMessageBox.question(
            app,
            "Clear Project",
            "Are you sure you want to clear the project?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.No:
            return

    try:
        print("\n" + "="*60)
        if should_save:
            print("🧹 CLEARING PROJECT (SAVING PROJECT & DISPLAY SETTINGS)")
        else:
            print("🧹 CLEARING PROJECT (NOT SAVING - DISPLAY SETTINGS WILL BE PRESERVED)")
        print("="*60)

        # ============================================================================
        # ✅ SAVE THE PROJECT IF USER CHOSE TO SAVE
        # ============================================================================
        if should_save and hasattr(app, 'data') and app.data is not None:
            save_path = None

            # A buffered/fenced dataset has multiple owning source files and
            # intentionally no single loaded_file. The quick-save routine
            # recognizes its ownership metadata before examining this marker.
            from .save_pointcloud import has_fenced_parent_writeback
            if has_fenced_parent_writeback(app):
                save_path = "__fenced_parent_writeback__"
            
            # Determine save path
            if save_path is None and hasattr(app, 'last_save_path') and app.last_save_path:
                save_path = app.last_save_path
            elif save_path is None and hasattr(app, 'loaded_file') and app.loaded_file:
                save_path = app.loaded_file
            
            if save_path:
                try:
                    print(f"\n💾 Saving project to: {os.path.basename(save_path)}")
                    
                    from .save_pointcloud import save_pointcloud_quick
                    
                    # Save without showing dialog
                    save_ok = save_pointcloud_quick(app, save_path)
                    if save_ok:
                        print(f"✅ Project saved successfully before clearing")
                        if hasattr(app, "statusBar"):
                            app.statusBar().showMessage(f"✅ Saved: {os.path.basename(save_path)}", 3000)
                    else:
                        print("⚠️ Project save failed before clearing")
                        if hasattr(app, "statusBar"):
                            app.statusBar().showMessage(f"⚠️ Save failed: {os.path.basename(save_path)}", 5000)
                    
                except Exception as save_error:
                    print(f"⚠️ Error saving: {save_error}")
                    if hasattr(app, "statusBar"):
                        app.statusBar().showMessage(f"⚠️ Save failed: {save_error}", 5000)
            else:
                print("⚠️ No save path available - skipping save")

        # ============================================================================
        # ✅ SAVE DISPLAY SETTINGS BEFORE CLEARING
        # IMPORTANT: Do this BEFORE any dialog reset/clear operations so
        # current_ptc_path and palette state are preserved correctly.
        # ============================================================================
        _save_display_settings_before_clear(app)

        # NOTE:
        # We intentionally do NOT call display_mode_dialog.reset_dialog() here.
        # Clearing that dialog before save can wipe active PTC/palette state and
        # make the next file load lose expected display settings.
        print("✅ Display mode state preserved (no pre-clear dialog reset)")

        # ============================================================================
        # STEP 1: Close Cut Section FIRST (let it complete naturally)
        # ============================================================================
        if hasattr(app, "cut_section_controller") and app.cut_section_controller:
            ctrl = app.cut_section_controller
            try:
                print(f"\n🔒 Closing cut section dock...")
                
                # Close dock - let closeEvent run naturally
                if ctrl.cut_dock is not None:
                    try:
                        ctrl.cut_dock.close()
                        ctrl.cut_dock = None
                        print("  ✅ Cut dock closed")
                    except Exception as e:
                        print(f"  ⚠️ Dock close: {e}")
                        ctrl.cut_dock = None
                
                # Clear VTK
                if ctrl.cut_vtk is not None:
                    try:
                        renderer = ctrl.cut_vtk.renderer
                        renderer.RemoveAllViewProps()
                        ctrl.cut_vtk.close()
                        ctrl.cut_vtk = None
                        print("  ✅ Cut VTK cleared")
                    except Exception as e:
                        print(f"  ⚠️ VTK clear: {e}")
                        ctrl.cut_vtk = None
                
                # Reset state
                ctrl.is_cut_view_active = False
                ctrl._state = 0
                ctrl.cut_points = None
                
                print("✅ Cut section closed")
                
            except Exception as e:
                print(f"⚠️ Cut section close failed: {e}")

        # ============================================================================
        # STEP 2: Clear Main View
        # ============================================================================
        if hasattr(app, "vtk_widget") and app.vtk_widget:
            try:
                renderer = app.vtk_widget.renderer
                
                # Store DXF actors if they exist (to preserve them)
                dxf_actors_backup = []
                if hasattr(app, 'dxf_actors') and app.dxf_actors:
                    for dxf_data in app.dxf_actors:
                        if 'actors' in dxf_data:
                            dxf_actors_backup.extend(dxf_data['actors'])
                    print(f"\n✅ Backing up {len(dxf_actors_backup)} DXF actors")
                
                # Store digitizer/drawing actors if they exist
                drawing_actors_backup = []
                if hasattr(app, 'digitizer') and app.digitizer:
                    if hasattr(app.digitizer, 'actors') and app.digitizer.actors:
                        drawing_actors_backup = list(app.digitizer.actors)
                        print(f"✅ Backing up {len(drawing_actors_backup)} drawing actors")
                
                # Remove ALL actors from renderer
                renderer.RemoveAllViewProps()
                print("✅ Removed all point cloud actors from main view")
                
                # Re-add ONLY DXF actors
                for actor in dxf_actors_backup:
                    renderer.AddActor(actor)
                
                # Re-add drawing actors
                for actor in drawing_actors_backup:
                    renderer.AddActor(actor)
                
                if dxf_actors_backup or drawing_actors_backup:
                    print(f"✅ Restored {len(dxf_actors_backup)} DXF + {len(drawing_actors_backup)} drawing actors")
                
                # Render to show cleared view
                app.vtk_widget.render()
                print(f"✅ Main viewer cleared (preserved: {len(dxf_actors_backup) + len(drawing_actors_backup)} actors)")
                    
            except Exception as e:
                print(f"⚠️ VTK clear failed: {e}")
                import traceback
                traceback.print_exc()

        # ============================================================================
        # STEP 3: Clear Cross-Section Views (AFTER cut is closed)
        # ============================================================================
        if hasattr(app, 'section_vtks') and app.section_vtks:
            print(f"\n🔄 Clearing point cloud data from {len(app.section_vtks)} cross-section views...")
            
            for view_idx, vtk_widget in app.section_vtks.items():
                try:
                    if vtk_widget and hasattr(vtk_widget, 'renderer'):
                        vtk_widget.renderer.RemoveAllViewProps()
                        vtk_widget.render()
                    print(f"  ✅ Cleared cross-section view {view_idx}")
                except Exception as e:
                    print(f"  ⚠️ Failed to clear view {view_idx}: {e}")
            
            print(f"✅ All cross-section views cleared (docks remain open)")

        # ============================================================================
        # STEP 4: Clear Section Controller
        # ============================================================================
        if hasattr(app, "section_controller") and app.section_controller:
            try:
                ctrl = app.section_controller
                
                # Clear sections data
                if hasattr(ctrl, 'sections'):
                    ctrl.sections.clear()
                
                # Clear stored cross-section data
                for view_idx in range(4):
                    section_view = getattr(ctrl, f"section_view_{view_idx}", None)
                    if section_view and hasattr(section_view, 'vtk_widget'):
                        try:
                            if hasattr(section_view.vtk_widget, 'renderer'):
                                section_view.vtk_widget.renderer.RemoveAllViewProps()
                                section_view.vtk_widget.render()
                        except Exception as e:
                            print(f"  ⚠️ Failed to clear section_view_{view_idx}: {e}")
                
                # Reset state flags
                if hasattr(ctrl, 'active_view'):
                    ctrl.active_view = 0
                if hasattr(ctrl, 'section_line'):
                    ctrl.section_line = None
                
                print("✅ Section controller cleared")
            except Exception as e:
                print(f"⚠️ Section controller clear failed: {e}")

        # ============================================================================
        # Clear Layers
        # ============================================================================
        app.layers = []
        if hasattr(app, "layers_dock") and app.layers_dock:
            try:
                app.layers_dock.clear_layers()
                print("✅ Layers cleared")
            except Exception as e:
                print(f"⚠️ Layers clear failed: {e}")

        # ============================================================================
        # Clear Digitizer Drawings
        # ============================================================================
        if hasattr(app, "digitizer") and app.digitizer:
            try:
                app.digitizer.clear_drawings()
                print("✅ Drawings cleared")
            except Exception as e:
                print(f"⚠️ Drawings clear failed: {e}")

        # Clear Internal State
        try:
            from .memory_manager import ObserverRegistry, release_data_arrays
            from .unified_actor_manager import reset_uam

            # ✅ Thoroughly reset the Unified Actor Manager (wipes GPU contexts, LUTs, and buffers)
            reset_uam(app)
            
            release_data_arrays(app)
            ObserverRegistry.release_all()

            mem_guard = getattr(app, "_mem_guard", None)
            if mem_guard is not None:
                mem_guard.force_gc()
        except Exception as e:
            print(f"⚠️ Memory manager cleanup skipped: {e}")

        app.data = None
        app.project_crs_epsg = None
        app.project_crs_wkt = None
        app.last_save_path = None
        app.loaded_file = None
        # ✅ FIX: Clear stale Z-bounds cache so SNT actors are positioned correctly
        # relative to the next LAZ file loaded. If this is not reset, _get_snt_z_offset
        # reads old z_min/z_max and places the SNT grid at the wrong height.
        app.data_bounds = None
        
        # app.class_palette and app.view_palettes are GLOBAL PTC state.
        # They are preserved across file clears so the current PTC stays
        # active for the next file without requiring a restore from QSettings.
        # Null out subsampling indices - MUST be recalculated for the new file
        app._main_global_indices = None
        app._main_global_mask = None
        app._main_lod_step = None

        print("✅ Internal state cleared (PTC/palette preserved as global state)")



        # ============================================================================
        # Clear Stored Section Data
        # ============================================================================
        for i in range(4):
            for attr in [f"section_{i}_core_points", f"section_{i}_buffer_points",
                        f"section_{i}_core_mask", f"section_{i}_buffer_mask"]:
                if hasattr(app, attr):
                    try:
                        delattr(app, attr)
                    except Exception:
                        pass
        print("✅ Stored section data cleared")

        # ============================================================================
        # Clear Undo/Redo Stacks
        # ============================================================================
        if hasattr(app, "undo_stack"):
            app.undo_stack.clear()
        if hasattr(app, "redo_stack"):
            app.redo_stack.clear()
        print("✅ Undo/redo history cleared")

        # ============================================================================
        # ✅ KEEP Display Mode Dialog COMPLETELY INTACT
        # Do NOT touch it - keep PTC loaded, keep table, keep all settings
        # ============================================================================
        if hasattr(app, "display_dialog") and app.display_dialog:
            try:
                print("✅ Display Mode dialog kept intact (PTC and table preserved)")
            except Exception as e:
                print(f"⚠️ Display Mode check failed: {e}")

        if hasattr(app, "display_mode_dialog") and app.display_mode_dialog:
            try:
                print("✅ Display Mode dialog kept intact (PTC and table preserved)")
            except Exception as e:
                print(f"⚠️ Display Mode check failed: {e}")

        # ============================================================================
        # Close Class Picker
        # ============================================================================
        if hasattr(app, "class_picker") and app.class_picker:
            try:
                app.class_picker.close()
                app.class_picker.deleteLater()
                app.class_picker = None
                print("✅ Class Picker closed")
            except Exception as e:
                print(f"⚠️ Class Picker close failed: {e}")

        # ============================================================================
        # Close Shading Control Panel
        # ============================================================================
        if hasattr(app, "shading_dock") and app.shading_dock:
            try:
                if hasattr(app, "removeDockWidget") and hasattr(app.shading_dock, "setWidget"):
                    app.removeDockWidget(app.shading_dock)
                else:
                    app.shading_dock.close()
                app.shading_dock.deleteLater()
                app.shading_dock = None
                if hasattr(app, "shading_panel"):
                    app.shading_panel = None
                print("✅ Shading dock closed")
            except Exception as e:
                print(f"⚠️ Shading dock close failed: {e}")

        # ============================================================================
        # Clear Point Statistics Widget
        # ============================================================================
        if hasattr(app, "point_count_widget") and app.point_count_widget:
            try:
                if hasattr(app.point_count_widget, 'clear_statistics'):
                    app.point_count_widget.clear_statistics()
                else:
                    if hasattr(app.point_count_widget, 'total_label'):
                        app.point_count_widget.total_label.setText("Total: 0")
                    if hasattr(app.point_count_widget, 'visible_label'):
                        app.point_count_widget.visible_label.setText("Visible: 0")
                    if hasattr(app.point_count_widget, 'class_tree'):
                        app.point_count_widget.class_tree.clear()
                print("✅ Point statistics cleared")
            except Exception as e:
                print(f"⚠️ Statistics clear failed: {e}")

        # ============================================================================
        # Clear Measurement Tools
        # ============================================================================
        if hasattr(app, 'measurement_tool') and app.measurement_tool is not None:
            try:
                app.measurement_tool.clear_all_measurements()
                print("✅ Measurement tools cleared")
            except Exception as e:
                print(f"⚠️ Measurement clear failed: {e}")

        # ============================================================================
        # Reset Other States
        # ============================================================================
        app.active_classify_tool = None
        if hasattr(app, "skip_main_view_refresh"):
            app.skip_main_view_refresh = False

        # ✅ Keep display_mode as is (don't reset to "rgb")
        # User's preferred mode stays until next file loads

        # Footer click-tools (point sync ring, layer identify, coordinate
        # pick) draw on-canvas overlays anchored to the cleared dataset -
        # leaving one active would keep showing a stale pick after clear.
        for tool_name in ("point_sync_tool", "snt_layer_pick_tool", "coordinate_pick_tool"):
            tool = getattr(app, tool_name, None)
            if tool is not None and getattr(tool, "active", False):
                try:
                    tool.deactivate()
                except Exception:
                    pass

        if hasattr(app, "spatial_index"):
            app.spatial_index = None

        # Reset canvas CRS state
        try:
            from gui.crs_manager import clear_canvas_crs
            clear_canvas_crs(app)
        except Exception:
            pass

        # ============================================================================
        # Update UI
        # ============================================================================
        app._update_window_title(None, None)

        # ✅ CRITICAL: Restore digitizer overlay renderers (Layer 1/2) AFTER all cleanup
        # Must run AFTER reset_uam, ObserverRegistry.release_all(), and clear_drawings()
        # which may have detached or invalidated the overlay renderers.
        if hasattr(app, "digitizer") and app.digitizer:
            try:
                app.digitizer._check_and_update_renderers()
                app.digitizer._reinstall_all_observers()   # ← restore VTK observers wiped by release_all()
                print("✅ Digitizer fully restored (post-clear)")
            except Exception as e:
                print(f"⚠️ Failed to restore digitizer: {e}")

        if hasattr(app, "statusBar"):

            if should_save:
                app.statusBar().showMessage("🧹 Project saved and cleared - Display settings preserved", 5000)
            else:
                app.statusBar().showMessage("🧹 Project cleared - Display settings preserved", 5000)

        print("="*60)
        if should_save:
            print("✅ PROJECT SAVED AND CLEARED (DISPLAY SETTINGS PRESERVED)")
        else:
            print("✅ PROJECT CLEARED (DISPLAY SETTINGS PRESERVED)")
        print("="*60 + "\n")

    except Exception as e:
        error_msg = f"Failed to clear project: {e}"
        print(f"❌ {error_msg}")
        import traceback
        traceback.print_exc()
        
        if hasattr(app, "statusBar"):
            app.statusBar().showMessage(f"⚠️ {error_msg}", 5000)
        
        QMessageBox.critical(
            app,
            "Clear Project Error",
            f"An error occurred while clearing the project:\n\n{e}\n\n"
            "Some components may not have been cleared properly."
        )


def clear_point_cloud(app):
    """
    Clear only point cloud data and reset related state, with confirmation.
    Preserves DXF, DWG, SNT attachments, drawings/digitizer layers, and PRJ data.
    """
    # Explicitly clear the shading cache to prevent stale data
    clear_shading_cache(reason="point_cloud_clear")
    app._rendered_shading_cache_key = None
    app._shaded_mesh_actor = None
    app._shaded_mesh_polydata = None

    # Check if project has unsaved point cloud data
    should_save = False
    
    if hasattr(app, 'data') and app.data is not None:
        msg_box = QMessageBox(app)
        msg_box.setStyleSheet(get_dialog_stylesheet())
        msg_box.setIcon(QMessageBox.Question)
        msg_box.setWindowTitle("Clear Point Cloud")
        msg_box.setText("Do you want to save the current point cloud before clearing?")
        msg_box.setInformativeText("Your display settings will be preserved and restored when you reload the file.")
        
        save_button = msg_box.addButton("Save", QMessageBox.AcceptRole)
        dont_save_button = msg_box.addButton("Don't Save", QMessageBox.DestructiveRole)
        cancel_button = msg_box.addButton("Cancel", QMessageBox.RejectRole)
        
        msg_box.setDefaultButton(save_button)
        msg_box.setEscapeButton(cancel_button)
        
        msg_box.exec()
        clicked_button = msg_box.clickedButton()
        
        if clicked_button == cancel_button:
            print("❌ Clear point cloud cancelled by user")
            return
        elif clicked_button == save_button:
            should_save = True
            print("✅ User chose to save before clearing")
        else:
            should_save = False
            print("⚠️ User chose NOT to save before clearing")
    else:
        reply = QMessageBox.question(
            app,
            "Clear Point Cloud",
            "Are you sure you want to clear the point cloud data?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.No:
            return

    try:
        print("\n" + "="*60)
        if should_save:
            print("🧹 CLEARING POINT CLOUD (SAVING POINT CLOUD & DISPLAY SETTINGS)")
        else:
            print("🧹 CLEARING POINT CLOUD (NOT SAVING - DISPLAY SETTINGS WILL BE PRESERVED)")
        print("="*60)

        # Save the project if user chose to save
        if should_save and hasattr(app, 'data') and app.data is not None:
            save_path = None
            if hasattr(app, 'last_save_path') and app.last_save_path:
                save_path = app.last_save_path
            elif hasattr(app, 'loaded_file') and app.loaded_file:
                save_path = app.loaded_file
            
            if save_path:
                try:
                    print(f"\n💾 Saving point cloud to: {os.path.basename(save_path)}")
                    from .save_pointcloud import save_pointcloud_quick
                    save_ok = save_pointcloud_quick(app, save_path)
                    if save_ok:
                        print(f"✅ Point cloud saved successfully before clearing")
                        if hasattr(app, "statusBar"):
                            app.statusBar().showMessage(f"✅ Saved: {os.path.basename(save_path)}", 3000)
                    else:
                        print("⚠️ Point cloud save failed before clearing")
                        if hasattr(app, "statusBar"):
                            app.statusBar().showMessage(f"⚠️ Save failed: {os.path.basename(save_path)}", 5000)
                except Exception as save_error:
                    print(f"⚠️ Error saving: {save_error}")
                    if hasattr(app, "statusBar"):
                        app.statusBar().showMessage(f"⚠️ Save failed: {save_error}", 5000)
            else:
                print("⚠️ No save path available - skipping save")

        # Save display settings before clearing
        _save_display_settings_before_clear(app)

        # Close Cut Section
        if hasattr(app, "cut_section_controller") and app.cut_section_controller:
            ctrl = app.cut_section_controller
            try:
                print(f"\n🔒 Closing cut section dock...")
                if ctrl.cut_dock is not None:
                    try:
                        ctrl.cut_dock.close()
                        ctrl.cut_dock = None
                        print("  ✅ Cut dock closed")
                    except Exception as e:
                        print(f"  ⚠️ Dock close: {e}")
                        ctrl.cut_dock = None
                if ctrl.cut_vtk is not None:
                    try:
                        renderer = ctrl.cut_vtk.renderer
                        renderer.RemoveAllViewProps()
                        ctrl.cut_vtk.close()
                        ctrl.cut_vtk = None
                        print("  ✅ Cut VTK cleared")
                    except Exception as e:
                        print(f"  ⚠️ VTK clear: {e}")
                        ctrl.cut_vtk = None
                ctrl.is_cut_view_active = False
                ctrl._state = 0
                ctrl.cut_points = None
                print("✅ Cut section closed")
            except Exception as e:
                print(f"⚠️ Cut section close failed: {e}")

        # Clear Main View (preserving DXF, DWG, SNT, and drawing/digitizer actors)
        if hasattr(app, "vtk_widget") and app.vtk_widget:
            try:
                renderer = app.vtk_widget.renderer
                
                # Backup DXF actors
                dxf_actors_backup = []
                if hasattr(app, 'dxf_actors') and app.dxf_actors:
                    for dxf_data in app.dxf_actors:
                        if 'actors' in dxf_data:
                            dxf_actors_backup.extend(dxf_data['actors'])
                
                # Backup DWG actors
                dwg_actors_backup = []
                if hasattr(app, 'dwg_actors') and app.dwg_actors:
                    for dwg_data in app.dwg_actors:
                        if 'actors' in dwg_data:
                            dwg_actors_backup.extend(dwg_data['actors'])

                # Backup SNT actors
                snt_actors_backup = []
                if hasattr(app, 'snt_actors') and app.snt_actors:
                    for snt_data in app.snt_actors:
                        if 'actors' in snt_data:
                            snt_actors_backup.extend(snt_data['actors'])
                
                # Backup drawing actors
                drawing_actors_backup = []
                if hasattr(app, 'digitizer') and app.digitizer:
                    if hasattr(app.digitizer, 'actors') and app.digitizer.actors:
                        drawing_actors_backup = list(app.digitizer.actors)
                
                # Remove ALL actors from renderer
                renderer.RemoveAllViewProps()
                print("✅ Removed all actors from main view")
                
                # Re-add DXF actors
                for actor in dxf_actors_backup:
                    renderer.AddActor(actor)
                
                # Re-add DWG actors
                for actor in dwg_actors_backup:
                    renderer.AddActor(actor)

                # Re-add SNT actors
                # ✅ FIX: Reset _snt_z_offset on each preserved SNT actor so that
                # _apply_z_offset_to_actor computes the correct delta on the next
                # LAZ load. Without this reset, actors keep a stale offset from the
                # previous file and the grid appears at the wrong Z height.
                for actor in snt_actors_backup:
                    actor._snt_z_offset = 0.0
                    renderer.AddActor(actor)

                # Re-add drawing actors
                for actor in drawing_actors_backup:
                    renderer.AddActor(actor)
                
                # Render to show cleared view
                app.vtk_widget.render()
                print(f"✅ Main viewer cleared point cloud (preserved DXF, DWG, SNT, and drawing actors)")
            except Exception as e:
                print(f"⚠️ VTK clear failed: {e}")
                import traceback
                traceback.print_exc()

        # Clear Cross-Section Views
        if hasattr(app, 'section_vtks') and app.section_vtks:
            print(f"\n🔄 Clearing point cloud data from {len(app.section_vtks)} cross-section views...")
            for view_idx, vtk_widget in app.section_vtks.items():
                try:
                    if vtk_widget and hasattr(vtk_widget, 'renderer'):
                        vtk_widget.renderer.RemoveAllViewProps()
                        vtk_widget.render()
                    print(f"  ✅ Cleared cross-section view {view_idx}")
                except Exception as e:
                    print(f"  ⚠️ Failed to clear view {view_idx}: {e}")
            print(f"✅ All cross-section views cleared")

        # Clear Section Controller
        if hasattr(app, "section_controller") and app.section_controller:
            try:
                ctrl = app.section_controller
                if hasattr(ctrl, 'sections'):
                    ctrl.sections.clear()
                for view_idx in range(4):
                    section_view = getattr(ctrl, f"section_view_{view_idx}", None)
                    if section_view and hasattr(section_view, 'vtk_widget'):
                        try:
                            if hasattr(section_view.vtk_widget, 'renderer'):
                                section_view.vtk_widget.renderer.RemoveAllViewProps()
                                section_view.vtk_widget.render()
                        except Exception as e:
                            print(f"  ⚠️ Failed to clear section_view_{view_idx}: {e}")
                if hasattr(ctrl, 'active_view'):
                    ctrl.active_view = 0
                if hasattr(ctrl, 'section_line'):
                    ctrl.section_line = None
                print("✅ Section controller cleared")
            except Exception as e:
                print(f"⚠️ Section controller clear failed: {e}")

        # Clear point cloud layers
        app.layers = []
        if hasattr(app, "layers_dock") and app.layers_dock:
            try:
                app.layers_dock.clear_layers()
                print("✅ Layers cleared")
            except Exception as e:
                print(f"⚠️ Layers clear failed: {e}")

        # Clear Internal State / Memory
        try:
            from .memory_manager import ObserverRegistry, release_data_arrays
            from .unified_actor_manager import reset_uam

            reset_uam(app)
            release_data_arrays(app)
            ObserverRegistry.release_all()

            mem_guard = getattr(app, "_mem_guard", None)
            if mem_guard is not None:
                mem_guard.force_gc()
        except Exception as e:
            print(f"⚠️ Memory manager cleanup skipped: {e}")

        app.data = None
        app.last_save_path = None
        app.loaded_file = None
        # ✅ FIX: Clear stale Z-bounds cache — same reason as in clear_project().
        app.data_bounds = None
        

        # app.class_palette and app.view_palettes are GLOBAL PTC state — preserved.
        app._main_global_indices = None
        app._main_global_mask = None
        app._main_lod_step = None


        # Clear Stored Section Data

        for i in range(4):
            for attr in [f"section_{i}_core_points", f"section_{i}_buffer_points",
                        f"section_{i}_core_mask", f"section_{i}_buffer_mask"]:
                if hasattr(app, attr):
                    try:
                        delattr(app, attr)
                    except Exception:
                        pass

        # Clear Undo/Redo Stacks
        if hasattr(app, "undo_stack"):
            app.undo_stack.clear()
        if hasattr(app, "redo_stack"):
            app.redo_stack.clear()
        print("✅ Undo/redo history cleared")

        # Close Class Picker
        if hasattr(app, "class_picker") and app.class_picker:
            try:
                app.class_picker.close()
                app.class_picker.deleteLater()
                app.class_picker = None
                print("✅ Class Picker closed")
            except Exception as e:
                print(f"⚠️ Class Picker close failed: {e}")

        # Close Shading Control Panel
        if hasattr(app, "shading_dock") and app.shading_dock:
            try:
                if hasattr(app, "removeDockWidget") and hasattr(app.shading_dock, "setWidget"):
                    app.removeDockWidget(app.shading_dock)
                else:
                    app.shading_dock.close()
                app.shading_dock.deleteLater()
                app.shading_dock = None
                if hasattr(app, "shading_panel"):
                    app.shading_panel = None
                print("✅ Shading dock closed")
            except Exception as e:
                print(f"⚠️ Shading dock close failed: {e}")

        # Clear Point Statistics Widget
        if hasattr(app, "point_count_widget") and app.point_count_widget:
            try:
                if hasattr(app.point_count_widget, 'clear_statistics'):
                    app.point_count_widget.clear_statistics()
                else:
                    if hasattr(app.point_count_widget, 'total_label'):
                        app.point_count_widget.total_label.setText("Total: 0")
                    if hasattr(app.point_count_widget, 'visible_label'):
                        app.point_count_widget.visible_label.setText("Visible: 0")
                    if hasattr(app.point_count_widget, 'class_tree'):
                        app.point_count_widget.class_tree.clear()
                print("✅ Point statistics cleared")
            except Exception as e:
                print(f"⚠️ Statistics clear failed: {e}")

        # Reset Other States
        app.active_classify_tool = None
        if hasattr(app, "skip_main_view_refresh"):
            app.skip_main_view_refresh = False

        # Footer click-tools (point sync ring, layer identify, coordinate
        # pick) draw on-canvas overlays anchored to the cleared dataset -
        # leaving one active would keep showing a stale pick after clear.
        for tool_name in ("point_sync_tool", "snt_layer_pick_tool", "coordinate_pick_tool"):
            tool = getattr(app, tool_name, None)
            if tool is not None and getattr(tool, "active", False):
                try:
                    tool.deactivate()
                except Exception:
                    pass

        if hasattr(app, "spatial_index"):
            app.spatial_index = None

        has_snt = bool(getattr(app, "snt_actors", None) or getattr(app, "snt_attachments", None))
        if not has_snt:
            try:
                from gui.crs_manager import clear_canvas_crs
                clear_canvas_crs(app)
            except Exception:
                pass

        # Update UI
        app._update_window_title(None, app.project_crs_epsg)

        # ✅ CRITICAL: Restore digitizer overlay renderers (Layer 1/2) AFTER all cleanup
        # Must run AFTER reset_uam, ObserverRegistry.release_all(), and clear_drawings()
        # which may have detached or invalidated the overlay renderers.
        if hasattr(app, "digitizer") and app.digitizer:
            try:
                app.digitizer._check_and_update_renderers()
                app.digitizer._reinstall_all_observers()   # ← restore VTK observers wiped by release_all()
                print("✅ Digitizer fully restored (post-clear-pointcloud)")
            except Exception as e:
                print(f"⚠️ Failed to restore digitizer: {e}")

        if hasattr(app, "statusBar"):
            if should_save:
                app.statusBar().showMessage("🧹 Point cloud saved and cleared - Display settings preserved", 5000)
            else:
                app.statusBar().showMessage("🧹 Point cloud cleared - Display settings preserved", 5000)

        print("="*60)
        if should_save:
            print("✅ POINT CLOUD SAVED AND CLEARED")
        else:
            print("✅ POINT CLOUD CLEARED")
        print("="*60 + "\n")

    except Exception as e:
        error_msg = f"Failed to clear point cloud: {e}"
        print(f"❌ {error_msg}")
        import traceback
        traceback.print_exc()
        
        if hasattr(app, "statusBar"):
            app.statusBar().showMessage(f"⚠️ {error_msg}", 5000)
        
        QMessageBox.critical(
            app,
            "Clear Point Cloud Error",
            f"An error occurred while clearing the point cloud:\n\n{e}\n\n"
            "Some components may not have been cleared properly."
        )
